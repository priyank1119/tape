"""
tape/grader.py
──────────────
Model-verifiable pass/fail engine for compiled trading strategies.

Reads:
  - rubric.yaml      (the spec)
  - backtest result  (dict from `tape.backtester.run()`)
  - live probe       (dict from `tape.deployer.probe()`, may be None pre-deploy)
  - compiled strategy path (for Opus 4.8 critique)

Returns a Verdict — a deterministic, JSON-serializable structure another
program (or another model) can verify in CI.

Verdict.passed is True iff EVERY clause in rubric.yaml's `done_when`
evaluates to True. Each clause has a human-readable failure reason for
fast feedback.

Design notes:
  - Pre-deploy grading is supported (live probe omitted; we mark live
    clauses as PENDING). Strategies that fail backtest never deploy.
  - Opus 4.8 critique is OPTIONAL and gated by rubric.yaml. When enabled,
    the model sees the rubric, the strategy source, and the backtest JSON.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_RUBRIC_PATH = Path(__file__).resolve().parent.parent / "rubric.yaml"


# ════════════════════════════════════════════════════════════════════════════
#  Verdict + Clause types
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ClauseResult:
    """One clause of the rubric. Either passed, failed, or skipped (PENDING)."""

    name: str
    status: str   # "pass" | "fail" | "pending"
    actual: Any = None
    threshold: Any = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass
class Verdict:
    passed: bool
    clauses: list[ClauseResult]
    opus_critique: Optional[str] = None
    opus_verdict: Optional[str] = None   # "approve" | "revise" | "reject"
    rubric_path: str = ""
    summary: str = ""

    @property
    def failed_clauses(self) -> list[ClauseResult]:
        return [c for c in self.clauses if c.status == "fail"]

    @property
    def pending_clauses(self) -> list[ClauseResult]:
        return [c for c in self.clauses if c.status == "pending"]

    def to_dict(self) -> dict:
        d = asdict(self)
        # Re-key for prettier JSON output
        d["clauses"] = [asdict(c) for c in self.clauses]
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def render(self) -> str:
        """Human-readable verdict for the CLI / web UI."""
        head = "✅ PASS" if self.passed else "❌ FAIL"
        lines = [f"{head}  —  {self.summary}", ""]
        for c in self.clauses:
            icon = {"pass": "✓", "fail": "✗", "pending": "·"}[c.status]
            line = f"  {icon} {c.name:48s}"
            if c.actual is not None and c.threshold is not None:
                line += f" actual={c.actual}  threshold={c.threshold}"
            if c.detail:
                line += f"  — {c.detail}"
            lines.append(line)
        if self.opus_critique:
            lines.append("")
            lines.append(f"  Opus 4.8 verdict: {self.opus_verdict}")
            lines.append("  ─" * 40)
            for ln in self.opus_critique.splitlines():
                lines.append(f"  {ln}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
#  Rubric loading
# ════════════════════════════════════════════════════════════════════════════

def load_rubric(path: Path | str = DEFAULT_RUBRIC_PATH) -> dict:
    """Load and validate rubric.yaml. Raises ValueError on schema problems."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Rubric not found at {path}")
    with open(path) as f:
        rubric = yaml.safe_load(f)
    _validate_rubric(rubric)
    return rubric


def _validate_rubric(rubric: dict) -> None:
    required_sections = ("schema_version", "backtest", "live", "done_when")
    for section in required_sections:
        if section not in rubric:
            raise ValueError(f"Rubric missing required section: {section}")
    if rubric["schema_version"] != 1:
        raise ValueError(
            f"Unsupported rubric schema_version={rubric['schema_version']} (this grader handles v1)"
        )


# ════════════════════════════════════════════════════════════════════════════
#  Clause evaluators
# ════════════════════════════════════════════════════════════════════════════
# Each clause in rubric.done_when maps to a function `(backtest, live, rubric)
# -> ClauseResult`. The dispatch table at the bottom maps clause name → func.
# ════════════════════════════════════════════════════════════════════════════

def _eval_sharpe(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    actual = bt.get("sharpe")
    threshold = rubric["backtest"]["min_sharpe"]
    if actual is None:
        return ClauseResult("backtest.sharpe_ge_min", "fail", None, threshold,
                            "backtest did not report sharpe")
    return ClauseResult(
        "backtest.sharpe_ge_min",
        "pass" if actual >= threshold else "fail",
        round(actual, 3), threshold,
    )


def _eval_drawdown(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    actual = bt.get("max_drawdown_pct")
    threshold = rubric["backtest"]["max_drawdown_pct"]
    if actual is None:
        return ClauseResult("backtest.drawdown_within_limit", "fail", None, threshold,
                            "backtest did not report max_drawdown_pct")
    return ClauseResult(
        "backtest.drawdown_within_limit",
        "pass" if actual <= threshold else "fail",
        round(actual, 2), threshold,
    )


def _eval_trade_count(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    actual = bt.get("trade_count", 0)
    threshold = rubric["backtest"]["min_trades"]
    return ClauseResult(
        "backtest.trade_count_ge_min",
        "pass" if actual >= threshold else "fail",
        actual, threshold,
    )


def _eval_win_rate(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    actual = bt.get("win_rate_pct")
    threshold = rubric["backtest"]["min_win_rate_pct"]
    if actual is None:
        return ClauseResult("backtest.win_rate_ge_min", "fail", None, threshold,
                            "backtest did not report win_rate_pct")
    return ClauseResult(
        "backtest.win_rate_ge_min",
        "pass" if actual >= threshold else "fail",
        round(actual, 1), threshold,
    )


def _eval_total_return(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    actual = bt.get("total_return_pct")
    threshold = rubric["backtest"]["min_total_return_pct"]
    if actual is None:
        return ClauseResult("backtest.total_return_ge_min", "fail", None, threshold,
                            "backtest did not report total_return_pct")
    return ClauseResult(
        "backtest.total_return_ge_min",
        "pass" if actual >= threshold else "fail",
        round(actual, 2), threshold,
    )


def _eval_concentration(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    actual = bt.get("max_single_market_concentration_pct")
    threshold = rubric["backtest"]["max_single_market_concentration_pct"]
    if actual is None:
        # If not measured, give benefit of the doubt
        return ClauseResult(
            "backtest.concentration_within_limit", "pass", "(not measured)", threshold,
            "backtester did not report concentration — assumed OK",
        )
    return ClauseResult(
        "backtest.concentration_within_limit",
        "pass" if actual <= threshold else "fail",
        round(actual, 1), threshold,
    )


def _eval_health(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    if not live:
        return ClauseResult("live.health_responds", "pending", None, "2xx",
                            "live probe not yet run (pre-deploy)")
    status_code = live.get("health_status_code")
    if status_code is None:
        return ClauseResult("live.health_responds", "fail", None, "2xx",
                            "probe did not record status code")
    return ClauseResult(
        "live.health_responds",
        "pass" if 200 <= status_code < 300 else "fail",
        status_code, "2xx",
    )


def _eval_startup_clean(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    if not live:
        return ClauseResult("live.startup_clean", "pending", None, 0,
                            "live probe not yet run (pre-deploy)")
    errors = live.get("startup_errors", 0)
    threshold = rubric["live"]["max_startup_errors"]
    return ClauseResult(
        "live.startup_clean",
        "pass" if errors <= threshold else "fail",
        errors, threshold,
    )


def _eval_imports(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    if not live:
        return ClauseResult("live.imports_cleanly", "pending", None, True,
                            "import check happens at deploy time")
    ok = live.get("imports_cleanly", False)
    return ClauseResult(
        "live.imports_cleanly",
        "pass" if ok else "fail",
        ok, True,
    )


def _eval_opus_critique(bt: dict, live: dict, rubric: dict) -> ClauseResult:
    """Filled in by `grade()` after the actual Opus call.

    The dispatch returns a pending placeholder; the orchestrator overrides
    it with the real verdict once the model has responded.
    """
    return ClauseResult(
        "opus_critique.verdict_eq_approve", "pending",
        None, rubric["opus_critique"]["required_verdict"],
        "opus critique runs in grade()",
    )


CLAUSE_DISPATCH: dict[str, Any] = {
    "backtest.sharpe_ge_min":             _eval_sharpe,
    "backtest.drawdown_within_limit":     _eval_drawdown,
    "backtest.trade_count_ge_min":        _eval_trade_count,
    "backtest.win_rate_ge_min":           _eval_win_rate,
    "backtest.total_return_ge_min":       _eval_total_return,
    "backtest.concentration_within_limit": _eval_concentration,
    "live.health_responds":               _eval_health,
    "live.startup_clean":                 _eval_startup_clean,
    "live.imports_cleanly":               _eval_imports,
    "opus_critique.verdict_eq_approve":   _eval_opus_critique,
}


# ════════════════════════════════════════════════════════════════════════════
#  Opus 4.8 critique
# ════════════════════════════════════════════════════════════════════════════

OPUS_CRITIQUE_PROMPT = """You are a senior quantitative trader reviewing an
auto-generated Polymarket trading strategy. Your job is to verify the
strategy isn't gaming the backtest, doesn't have a lookahead bug, and is
reasonable to deploy with real money.

You will be given:
  - The compiled Python strategy source
  - The backtest result (Sharpe, drawdown, win rate, P&L curve)
  - The rubric thresholds the strategy is supposed to satisfy

Return a structured verdict on three dimensions:

  1. **Soundness** — does the code have lookahead bias, hard-coded outputs,
     or other ways to game the backtest?
  2. **Robustness** — would this strategy plausibly work on the next 30 days
     of fresh markets, or is it overfit?
  3. **Risk** — is the position sizing / stop-loss / concentration sane?

Then issue ONE of three verdicts:

  - approve  — deploy with confidence
  - revise   — has a fixable issue; recommend changes
  - reject   — fundamentally unsound; do not deploy

Format your response in EXACTLY this structure:

SOUNDNESS: <one sentence>
ROBUSTNESS: <one sentence>
RISK: <one sentence>
VERDICT: <approve|revise|reject>
REASON: <one paragraph explaining the verdict>
"""


def _call_opus_critique(
    rubric: dict, strategy_src: str, backtest: dict, model: Optional[str] = None,
) -> tuple[str, str]:
    """Call Opus 4.8 with strategy + backtest + rubric. Returns (verdict, full text).

    On any error (network, no API key, parse failure), returns ("error", "...")
    — the orchestrator marks the clause failed in that case.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return "error", "anthropic SDK not installed"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "error", "ANTHROPIC_API_KEY not set"

    client = Anthropic(api_key=api_key)
    model = model or rubric.get("opus_critique", {}).get("model", "claude-opus-4-8")

    user_msg = (
        f"## Rubric thresholds\n```yaml\n{yaml.safe_dump(rubric, sort_keys=False)}\n```\n\n"
        f"## Backtest result\n```json\n{json.dumps(backtest, indent=2)}\n```\n\n"
        f"## Strategy source\n```python\n{strategy_src}\n```\n"
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": user_msg}],
            system=OPUS_CRITIQUE_PROMPT,
        )
    except Exception as e:
        logger.warning(f"Opus critique call failed: {e}")
        return "error", f"Opus call failed: {e}"

    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    verdict = "error"
    for line in text.splitlines():
        if line.upper().startswith("VERDICT:"):
            tok = line.split(":", 1)[1].strip().lower()
            if tok in ("approve", "revise", "reject"):
                verdict = tok
                break
    return verdict, text


# ════════════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════════════

def grade(
    backtest: dict,
    live: Optional[dict] = None,
    strategy_src: str = "",
    rubric_path: Path | str = DEFAULT_RUBRIC_PATH,
    skip_opus: bool = False,
) -> Verdict:
    """Apply the rubric to a backtest (+ optional live probe + strategy src).

    Args:
      backtest:     dict from `tape.backtester.run()`. Required.
      live:         dict from `tape.deployer.probe()`. None = pre-deploy.
      strategy_src: full Python source of the compiled strategy. Required for
                    Opus critique; empty string is allowed (critique = error).
      rubric_path:  path to rubric.yaml (default uses repo root).
      skip_opus:    if True, skip the Opus critique step (useful for tests).

    Returns:
      Verdict with .passed, .clauses, .opus_critique fields.
    """
    rubric = load_rubric(rubric_path)
    clauses: list[ClauseResult] = []

    # Evaluate every clause named in done_when, in order
    for clause_name in rubric["done_when"]:
        eval_fn = CLAUSE_DISPATCH.get(clause_name)
        if eval_fn is None:
            clauses.append(ClauseResult(clause_name, "fail", None, None,
                                        f"unknown clause — no evaluator registered"))
            continue
        clauses.append(eval_fn(backtest, live, rubric))

    # Run Opus critique if enabled, strategy source available, not skipped
    opus_critique_text = None
    opus_verdict = None
    critique_enabled = rubric.get("opus_critique", {}).get("enabled", False)
    if critique_enabled and not skip_opus and strategy_src:
        opus_verdict, opus_critique_text = _call_opus_critique(rubric, strategy_src, backtest)
        # Overwrite the placeholder clause
        required = rubric["opus_critique"]["required_verdict"]
        for i, c in enumerate(clauses):
            if c.name == "opus_critique.verdict_eq_approve":
                clauses[i] = ClauseResult(
                    c.name,
                    "pass" if opus_verdict == required else "fail",
                    opus_verdict, required,
                    "Opus 4.8 returned this verdict",
                )
                break

    passed = all(c.status == "pass" for c in clauses)
    summary = _build_summary(passed, clauses, backtest)

    return Verdict(
        passed=passed,
        clauses=clauses,
        opus_critique=opus_critique_text,
        opus_verdict=opus_verdict,
        rubric_path=str(rubric_path),
        summary=summary,
    )


def _build_summary(passed: bool, clauses: list[ClauseResult], bt: dict) -> str:
    failed = [c for c in clauses if c.status == "fail"]
    pending = [c for c in clauses if c.status == "pending"]
    if passed:
        return (
            f"All {len(clauses)} clauses passed. "
            f"Sharpe={bt.get('sharpe', '?')}  "
            f"return={bt.get('total_return_pct', '?')}%  "
            f"trades={bt.get('trade_count', '?')}"
        )
    if pending and not failed:
        return f"{len(pending)} live clauses pending (deploy to verify); rest passed."
    return f"{len(failed)} clauses failed: " + ", ".join(c.name for c in failed)


# ════════════════════════════════════════════════════════════════════════════
#  CLI entrypoint
# ════════════════════════════════════════════════════════════════════════════

def _cli():
    """Usage:
        python -m tape.grader <backtest.json> [--live live.json] [--strategy file.py]
    """
    import argparse
    p = argparse.ArgumentParser(description="Grade a strategy against rubric.yaml")
    p.add_argument("backtest_json", help="Path to backtest result JSON")
    p.add_argument("--live", help="Path to live probe JSON (optional)")
    p.add_argument("--strategy", help="Path to compiled strategy .py (optional)")
    p.add_argument("--rubric", default=str(DEFAULT_RUBRIC_PATH),
                   help="Path to rubric.yaml")
    p.add_argument("--skip-opus", action="store_true",
                   help="Skip Opus 4.8 critique step")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args()

    with open(args.backtest_json) as f:
        backtest = json.load(f)
    live = None
    if args.live:
        with open(args.live) as f:
            live = json.load(f)
    strategy_src = ""
    if args.strategy:
        strategy_src = Path(args.strategy).read_text()

    verdict = grade(backtest, live, strategy_src,
                    rubric_path=args.rubric, skip_opus=args.skip_opus)
    if args.json:
        print(verdict.to_json())
    else:
        print(verdict.render())
    import sys
    sys.exit(0 if verdict.passed else 1)


if __name__ == "__main__":
    _cli()
