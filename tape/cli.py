"""
tape/cli.py
───────────
The orchestration entrypoint. Chains the full pipeline:

    compile → backtest → grade → (deploy)

and exits 0 if the strategy passes the rubric, 1 otherwise. This is the
model-verifiable "done" predicate: another team (or a CI job, or a judge)
can run

    tape run "Buy NO on geopolitical markets at >0.90"

and the exit code + JSON output tell them, with no human in the loop,
whether the compiled strategy is deployable.

Commands:
    tape run "<brief>"        full pipeline: compile → backtest → grade
    tape compile "<brief>"    just compile
    tape backtest <path>      just backtest a compiled strategy
    tape grade <bt.json>      just grade a backtest result
    tape deploy <path>        deploy a strategy to the sandbox

Every command supports --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Load .env if present (local dev convenience)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from tape import __version__


# ════════════════════════════════════════════════════════════════════════════
#  `tape run` — the full pipeline
# ════════════════════════════════════════════════════════════════════════════

def cmd_run(args) -> int:
    """compile → backtest → grade. The headline command.

    Returns exit code: 0 if strategy passes rubric, 1 otherwise.
    """
    from tape.compiler import compile_strategy
    from tape.backtester import run as backtest_run
    from tape.grader import grade

    steps = []  # for --json structured output

    # ── Step 1: compile ──────────────────────────────────────────────────
    _say(args, "▸ Compiling strategy with Opus 4.8…")
    comp = compile_strategy(args.brief, out_dir=args.out, model=args.model)
    steps.append({"step": "compile", "success": comp.success,
                  "strategy": comp.strategy_name, "error": comp.error,
                  "tokens": {"in": comp.input_tokens, "out": comp.output_tokens}})
    if not comp.success:
        _say(args, f"  ✗ Compile failed: {comp.error}")
        return _finish(args, steps, passed=False, exit_code=1)
    _say(args, f"  ✓ Compiled '{comp.strategy_name}' "
               f"({comp.input_tokens}+{comp.output_tokens} tokens)")

    # ── Step 2: backtest ─────────────────────────────────────────────────
    _say(args, "▸ Backtesting against 90 days of synthetic markets…")
    bt = backtest_run(comp.strategy_path, days=args.days, seed=args.seed)
    steps.append({"step": "backtest", "metrics": {
        "sharpe": bt["sharpe"], "total_return_pct": bt["total_return_pct"],
        "max_drawdown_pct": bt["max_drawdown_pct"], "trade_count": bt["trade_count"],
        "win_rate_pct": bt["win_rate_pct"],
    }})
    _say(args, f"  ✓ Return {bt['total_return_pct']:+.2f}%  "
               f"Sharpe {bt['sharpe']:.2f}  "
               f"Drawdown {bt['max_drawdown_pct']:.1f}%  "
               f"{bt['trade_count']} trades ({bt['win_rate_pct']:.0f}% win)")

    # ── Step 3: grade ────────────────────────────────────────────────────
    _say(args, "▸ Grading against rubric.yaml…")
    strategy_src = Path(comp.strategy_path).read_text()
    verdict = grade(bt, strategy_src=strategy_src, skip_opus=args.skip_opus)
    steps.append({"step": "grade", "passed": verdict.passed,
                  "summary": verdict.summary,
                  "clauses": [{"name": c.name, "status": c.status} for c in verdict.clauses],
                  "opus_verdict": verdict.opus_verdict})

    if not args.json:
        print()
        print(verdict.render())

    # Pre-deploy grading: live clauses are PENDING, so "passed" can't be True
    # yet. We treat "all non-live clauses pass" as the deployable signal.
    non_live_failed = [c for c in verdict.clauses
                       if c.status == "fail" and not c.name.startswith("live.")]
    deployable = len(non_live_failed) == 0

    return _finish(args, steps, passed=deployable,
                   exit_code=0 if deployable else 1,
                   extra={"strategy_path": comp.strategy_path,
                          "deployable": deployable})


# ════════════════════════════════════════════════════════════════════════════
#  Individual commands (thin wrappers)
# ════════════════════════════════════════════════════════════════════════════

def cmd_compile(args) -> int:
    from tape.compiler import compile_strategy
    result = compile_strategy(args.brief, out_dir=args.out, model=args.model)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    elif result.success:
        print(f"✓ Compiled '{result.strategy_name}' → {result.strategy_path}")
    else:
        print(f"✗ {result.error}")
    return 0 if result.success else 1


def cmd_backtest(args) -> int:
    from tape.backtester import run as backtest_run
    result = backtest_run(args.strategy_path, days=args.days, seed=args.seed)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Return {result['total_return_pct']:+.2f}%  "
              f"Sharpe {result['sharpe']:.2f}  "
              f"{result['trade_count']} trades")
    return 0


def cmd_grade(args) -> int:
    from tape.grader import grade
    with open(args.backtest_json) as f:
        bt = json.load(f)
    strategy_src = Path(args.strategy).read_text() if args.strategy else ""
    verdict = grade(bt, strategy_src=strategy_src, skip_opus=args.skip_opus)
    if args.json:
        print(verdict.to_json())
    else:
        print(verdict.render())
    return 0 if verdict.passed else 1


def cmd_swarm(args) -> int:
    """Run the Opus 4.8 tournament: compile v1 → 5 refinements → judge."""
    from tape.compiler import compile_strategy
    from tape.backtester import run as backtest_run
    from tape.swarm import run_tournament

    _say(args, "▸ Compiling v1 with Opus 4.8…")
    comp = compile_strategy(args.brief, out_dir=args.out, model=args.model)
    if not comp.success:
        print(f"✗ Compile failed: {comp.error}")
        return 1
    _say(args, f"  ✓ {comp.strategy_name}")

    _say(args, "▸ Backtesting v1…")
    bt = backtest_run(comp.strategy_path, seed=args.seed)
    _say(args, f"  ✓ Sharpe {bt['sharpe']}  return {bt['total_return_pct']}%")

    _say(args, "▸ Running tournament: 5 parallel Opus 4.8 refinements + judge…")
    result = run_tournament(comp.strategy_path, bt, model=args.model)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print()
        for v in result.variants:
            mark = "🏆" if v.variant_id == result.winner_id else "  "
            print(f"{mark} {v.metrics_line()}")
        print(f"\nWINNER: {result.winner_id}")
        print(f"REASON: {result.judge_reason}")
    return 0


def cmd_deploy(args) -> int:
    from tape.deployer import deploy_strategy, probe
    result = deploy_strategy(args.strategy_path, budget_usd=args.budget)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["success"]:
            print(f"✓ Deployed '{result['strategy_name']}' (bot id: {result['bot_id']})")
            print(f"  Health: {result['health_url']}")
        else:
            print(f"✗ Deploy failed: {result['error']}")
    return 0 if result["success"] else 1


# ════════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════════

def _say(args, msg: str) -> None:
    """Print human-readable progress unless in --json mode."""
    if not getattr(args, "json", False):
        print(msg)


def _finish(args, steps, passed: bool, exit_code: int, extra: dict = None) -> int:
    if args.json:
        out = {"passed": passed, "steps": steps}
        if extra:
            out.update(extra)
        print(json.dumps(out, indent=2))
    else:
        print()
        verdict_str = "✅ DEPLOYABLE" if passed else "❌ NOT DEPLOYABLE"
        print(f"{verdict_str}")
    return exit_code


# ════════════════════════════════════════════════════════════════════════════
#  Argument parser
# ════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    from tape.compiler import DEFAULT_MODEL, REPO_ROOT

    p = argparse.ArgumentParser(prog="tape", description="Plain English → deployed Polymarket bot.")
    p.add_argument("--version", action="version", version=f"tape {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    # run
    r = sub.add_parser("run", help="Full pipeline: compile → backtest → grade")
    r.add_argument("brief", help="Strategy brief in plain English")
    r.add_argument("--out", default=str(REPO_ROOT / "strategies"))
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--days", type=int, default=90)
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--skip-opus", action="store_true", help="Skip Opus critique in grading")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    # compile
    c = sub.add_parser("compile", help="Compile a brief into a strategy")
    c.add_argument("brief")
    c.add_argument("--out", default=str(REPO_ROOT / "strategies"))
    c.add_argument("--model", default=DEFAULT_MODEL)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_compile)

    # backtest
    b = sub.add_parser("backtest", help="Backtest a compiled strategy")
    b.add_argument("strategy_path")
    b.add_argument("--days", type=int, default=90)
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_backtest)

    # grade
    g = sub.add_parser("grade", help="Grade a backtest result against rubric.yaml")
    g.add_argument("backtest_json")
    g.add_argument("--strategy", help="Path to strategy .py (for Opus critique)")
    g.add_argument("--skip-opus", action="store_true")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_grade)

    # swarm
    s = sub.add_parser("swarm", help="Opus 4.8 tournament: compile → 5 refine → judge")
    s.add_argument("brief", help="Strategy brief in plain English")
    s.add_argument("--out", default=str(REPO_ROOT / "strategies"))
    s.add_argument("--model", default=DEFAULT_MODEL)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_swarm)

    # deploy
    d = sub.add_parser("deploy", help="Deploy a strategy to the sandbox")
    d.add_argument("strategy_path")
    d.add_argument("--budget", type=float, default=25.0)
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_deploy)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
