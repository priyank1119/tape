"""
tape/swarm.py
─────────────
Opus 4.8 strategy tournament — the multi-agent showpiece.

Pipeline:
  1. Start from a compiled v1 strategy + its backtest.
  2. Spawn N Opus 4.8 refiners IN PARALLEL, each given a different
     refinement angle (tighter stops, looser entry, news weighting, etc.).
  3. Backtest every refinement (deterministic, same seed as v1).
  4. A final Opus 4.8 "tournament judge" reads the original + all
     refinements (metrics, angles, rubric pass/fail) and picks the winner.

Why this is a genuine multi-agent pattern, not just N chat calls:
  - The refiners run concurrently and independently (a real fan-out).
  - Each gets a *distinct* objective — they explore different regions of
    strategy space, not the same prompt N times.
  - The judge is a *separate* Opus role that consumes all their outputs and
    makes one decision — a fan-in. Fan-out + fan-in = a swarm.

The whole thing is deterministic on the backtest side (fixed seed) so the
demo doesn't surprise the presenter; only the Opus text generation varies.

Cost: N refiner calls + 1 judge call (default N=5 → 6 Opus calls, ~$1).
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
REFINE_PROMPT = REPO_ROOT / "tape" / "prompts" / "refine.md"
TOURNAMENT_PROMPT = REPO_ROOT / "tape" / "prompts" / "tournament.md"

DEFAULT_MODEL = os.environ.get("TAPE_LLM_MODEL", "claude-opus-4-8")

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover
    Anthropic = None  # type: ignore


# The five refinement angles. Each is a distinct exploration direction so the
# swarm covers different regions of strategy space rather than repeating.
REFINEMENT_ANGLES = [
    ("tighter_risk",
     "Tighten risk control: reduce position sizing and/or pull stop-losses "
     "closer so max drawdown drops. Aim to raise Sharpe even if total return "
     "dips slightly."),
    ("looser_entry",
     "Loosen the entry criteria so the strategy makes more trades (more "
     "statistical reliability), while keeping each trade's edge positive. "
     "Good if the original made too few trades."),
    ("news_weighted",
     "Incorporate the news signal: require bullish news (news_sentiment + "
     "news_strength) to confirm entries, or trim positions on adverse news. "
     "Use the Market.news_sentiment / news_strength fields."),
    ("horizon_focus",
     "Focus on resolution timing: prefer markets closer to resolution where "
     "near-certain outcomes converge to $1.00 (bond-style edge), and avoid "
     "long-dated positions that tie up capital."),
    ("liquidity_filter",
     "Add a stricter liquidity / quality filter in filter_markets so the "
     "strategy only trades deep, reliable markets and skips thin ones that "
     "add noise and slippage risk."),
]


# ════════════════════════════════════════════════════════════════════════════
#  Result types
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Variant:
    """One strategy variant in the tournament (original or a refinement)."""

    variant_id: str          # "original" | "refinement_1" ...
    angle_key: str           # "" for original, else the angle name
    angle_desc: str
    strategy_path: Optional[str] = None
    strategy_src: str = ""
    compiled_ok: bool = False
    compile_error: str = ""
    # backtest metrics
    sharpe: Optional[float] = None
    total_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    trade_count: Optional[int] = None
    win_rate_pct: Optional[float] = None
    passed_rubric: bool = False

    def metrics_line(self) -> str:
        if not self.compiled_ok:
            return f"{self.variant_id} [{self.angle_key}]: COMPILE FAILED ({self.compile_error})"
        return (f"{self.variant_id} [{self.angle_key or 'baseline'}]: "
                f"Sharpe={self.sharpe}  return={self.total_return_pct}%  "
                f"drawdown={self.max_drawdown_pct}%  trades={self.trade_count}  "
                f"win={self.win_rate_pct}%  rubric={'PASS' if self.passed_rubric else 'FAIL'}")

    def to_dict(self) -> dict:
        return {
            "variant_id": self.variant_id,
            "angle": self.angle_key,
            "angle_desc": self.angle_desc,
            "compiled_ok": self.compiled_ok,
            "sharpe": self.sharpe,
            "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "trade_count": self.trade_count,
            "win_rate_pct": self.win_rate_pct,
            "passed_rubric": self.passed_rubric,
        }


@dataclass
class TournamentResult:
    variants: list[Variant]
    winner_id: str
    ranking: list[str]
    judge_reason: str
    winner_strategy_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "variants": [v.to_dict() for v in self.variants],
            "winner_id": self.winner_id,
            "ranking": self.ranking,
            "judge_reason": self.judge_reason,
            "winner_strategy_path": self.winner_strategy_path,
        }


# ════════════════════════════════════════════════════════════════════════════
#  Refiner (one parallel agent)
# ════════════════════════════════════════════════════════════════════════════

def _refine_one(
    original_src: str, original_bt: dict, angle_key: str, angle_desc: str,
    out_dir: Path, idx: int, model: str,
) -> Variant:
    """Run one Opus 4.8 refinement + backtest. Designed to run in a thread."""
    from tape.compiler import (
        _strip_markdown_fences, _validate_structure, _smoke_import_file,
    )
    from tape.backtester import run as backtest_run
    from tape.grader import grade

    variant = Variant(
        variant_id=f"refinement_{idx}", angle_key=angle_key, angle_desc=angle_desc,
    )

    if Anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        variant.compile_error = "no Anthropic API key"
        return variant

    system_prompt = REFINE_PROMPT.read_text().replace("{ANGLE}", angle_desc)
    user_msg = (
        f"## Original strategy\n```python\n{original_src}\n```\n\n"
        f"## Original backtest\n"
        f"Sharpe={original_bt.get('sharpe')} return={original_bt.get('total_return_pct')}% "
        f"drawdown={original_bt.get('max_drawdown_pct')}% trades={original_bt.get('trade_count')} "
        f"win={original_bt.get('win_rate_pct')}%\n"
    )

    try:
        # Higher timeout for refiners: 5 fire in parallel and each generates
        # a full strategy (~1500 tokens). The parallel burst runs slower than
        # a single call, so a 60s ceiling caused spurious timeouts. 120s gives
        # headroom; since they're parallel, the higher ceiling doesn't add
        # wall-clock unless a call is genuinely slow.
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=120.0, max_retries=2)
        resp = client.messages.create(
            model=model, max_tokens=3000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:  # noqa: BLE001
        variant.compile_error = f"Opus call failed: {e}"
        return variant

    code = _strip_markdown_fences(raw).strip() + "\n"
    valid, why = _validate_structure(code)
    if not valid:
        variant.compile_error = f"validation failed: {why}"
        variant.strategy_src = code
        return variant

    out_path = out_dir / f"refinement_{idx}_{angle_key}.py"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(code)

    ok, err, _ = _smoke_import_file(out_path)
    if not ok:
        variant.compile_error = f"import failed: {err}"
        variant.strategy_src = code
        return variant

    variant.compiled_ok = True
    variant.strategy_path = str(out_path)
    variant.strategy_src = code

    # Backtest with the same seed as the original for fair comparison
    try:
        bt = backtest_run(out_path, seed=42)
        variant.sharpe = bt["sharpe"]
        variant.total_return_pct = bt["total_return_pct"]
        variant.max_drawdown_pct = bt["max_drawdown_pct"]
        variant.trade_count = bt["trade_count"]
        variant.win_rate_pct = bt["win_rate_pct"]
        verdict = grade(bt, skip_opus=True)
        non_live_failed = [c for c in verdict.clauses
                           if c.status == "fail" and not c.name.startswith("live.")]
        variant.passed_rubric = len(non_live_failed) == 0
    except Exception as e:  # noqa: BLE001
        variant.compile_error = f"backtest failed: {e}"
        variant.compiled_ok = False

    return variant


# ════════════════════════════════════════════════════════════════════════════
#  Tournament judge (fan-in)
# ════════════════════════════════════════════════════════════════════════════

def _judge(variants: list[Variant], model: str) -> tuple[str, list[str], str]:
    """Opus 4.8 reads all variants and picks the winner.

    Returns (winner_id, ranking, reason). Falls back to a deterministic
    Sharpe-based pick if the Opus call fails.
    """
    # Deterministic fallback ranking (best Sharpe among rubric-passers)
    def _fallback() -> tuple[str, list[str], str]:
        passers = [v for v in variants if v.passed_rubric and v.sharpe is not None]
        ranked = sorted(passers, key=lambda v: (v.sharpe or -99), reverse=True)
        if not ranked:
            return "none", [], "No variant passed the rubric."
        order = [v.variant_id for v in ranked]
        return order[0], order, f"Highest Sharpe ({ranked[0].sharpe}) among rubric-passers."

    if Anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return _fallback()

    variants_block = "\n".join(v.metrics_line() for v in variants)
    system_prompt = TOURNAMENT_PROMPT.read_text().replace("{VARIANTS}", variants_block)

    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=60.0, max_retries=2)
        resp = client.messages.create(
            model=model, max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": "Pick the winner."}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"tournament judge call failed: {e}")
        return _fallback()

    winner, ranking, reason = "none", [], ""
    for line in text.splitlines():
        u = line.strip()
        if u.upper().startswith("WINNER:"):
            winner = u.split(":", 1)[1].strip()
        elif u.upper().startswith("RANKING:"):
            ranking = [x.strip() for x in u.split(":", 1)[1].split(",") if x.strip()]
        elif u.upper().startswith("REASON:"):
            reason = u.split(":", 1)[1].strip()

    # Validate the judge picked a real, passing variant; else fall back
    valid_ids = {v.variant_id for v in variants if v.passed_rubric}
    if winner not in valid_ids and winner != "none":
        logger.warning(f"judge picked invalid/failing winner {winner!r}; using fallback")
        return _fallback()

    return winner, ranking, reason


# ════════════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════════════

def run_tournament(
    original_strategy_path: Path | str,
    original_backtest: dict,
    *,
    out_dir: Path | str = REPO_ROOT / "strategies" / "swarm",
    model: str = DEFAULT_MODEL,
    max_workers: int = 5,
) -> TournamentResult:
    """Run the full swarm tournament.

    Args:
      original_strategy_path: the compiled v1 strategy file
      original_backtest:      v1's backtest result dict
      out_dir:                where refinement files land
      model:                  Opus model
      max_workers:            parallelism for the refiner fan-out

    Returns:
      TournamentResult with the winner, ranking, judge reasoning, and all
      variant metrics.
    """
    out_dir = Path(out_dir)
    original_path = Path(original_strategy_path)
    original_src = original_path.read_text()

    # Build the "original" variant from the v1 backtest
    original = Variant(
        variant_id="original", angle_key="", angle_desc="the user's compiled v1",
        strategy_path=str(original_path), strategy_src=original_src, compiled_ok=True,
        sharpe=original_backtest.get("sharpe"),
        total_return_pct=original_backtest.get("total_return_pct"),
        max_drawdown_pct=original_backtest.get("max_drawdown_pct"),
        trade_count=original_backtest.get("trade_count"),
        win_rate_pct=original_backtest.get("win_rate_pct"),
    )
    # Grade the original too
    try:
        from tape.grader import grade
        v = grade(original_backtest, skip_opus=True)
        # Use the SAME pass criterion as the refinements (_refine_one): a
        # variant passes if no non-live clause FAILED. Pending clauses (e.g.
        # opus_critique when skip_opus=True) don't count against it. Previously
        # this used all(status=="pass"), which treated the pending opus clause
        # as a failure — making the original FAIL while an identical-metrics
        # refinement PASSED. That inconsistency is now removed.
        non_live_failed = [c for c in v.clauses
                           if c.status == "fail" and not c.name.startswith("live.")]
        original.passed_rubric = len(non_live_failed) == 0
    except Exception:  # noqa: BLE001
        pass

    # ── Fan-out: refine in parallel ──────────────────────────────────────
    refinements: list[Variant] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_refine_one, original_src, original_backtest,
                        key, desc, out_dir, i + 1, model): key
            for i, (key, desc) in enumerate(REFINEMENT_ANGLES)
        }
        for fut in as_completed(futures):
            try:
                refinements.append(fut.result())
            except Exception as e:  # noqa: BLE001
                logger.warning(f"refiner thread crashed: {e}")

    # Keep refinements in stable order (refinement_1..N)
    refinements.sort(key=lambda v: v.variant_id)
    all_variants = [original] + refinements

    # ── Fan-in: judge ────────────────────────────────────────────────────
    winner_id, ranking, reason = _judge(all_variants, model)

    winner_path = None
    for v in all_variants:
        if v.variant_id == winner_id:
            winner_path = v.strategy_path
            break

    return TournamentResult(
        variants=all_variants,
        winner_id=winner_id,
        ranking=ranking or [v.variant_id for v in all_variants],
        judge_reason=reason,
        winner_strategy_path=winner_path,
    )


# ════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════

def _cli() -> None:
    import argparse
    import json
    from tape.backtester import run as backtest_run

    p = argparse.ArgumentParser(description="Run an Opus 4.8 strategy tournament.")
    p.add_argument("strategy_path", help="Path to the compiled v1 strategy")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    bt = backtest_run(args.strategy_path, seed=42)
    result = run_tournament(args.strategy_path, bt)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print("═" * 70)
        print("  OPUS 4.8 STRATEGY TOURNAMENT")
        print("═" * 70)
        for v in result.variants:
            mark = "🏆" if v.variant_id == result.winner_id else "  "
            print(f"{mark} {v.metrics_line()}")
        print("─" * 70)
        print(f"WINNER: {result.winner_id}")
        print(f"RANKING: {' > '.join(result.ranking)}")
        print(f"REASON: {result.judge_reason}")


if __name__ == "__main__":
    _cli()
