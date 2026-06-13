"""
tests/test_backtester.py
────────────────────────
Tests the backtester:
  1. Determinism (same seed → identical result)
  2. The bond strategy beats the YOLO strategy on rubric metrics
  3. Edge cases (do_nothing strategy doesn't crash)
  4. Output schema matches what grader.py expects
  5. Backtester + grader integrate cleanly end-to-end

These tests are the critical proof that Tape's pitch works: a well-formed
strategy passes the rubric, a nonsense one fails. If this test fails, the
rubric is theater and the whole project is broken.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tape.backtester import run as backtest_run, DEFAULT_DAYS
from tape.grader import grade

EXAMPLES = Path(__file__).resolve().parent.parent / "strategies" / "examples"


# ── Determinism ─────────────────────────────────────────────────────────────

class TestDeterminism:

    def test_same_seed_same_result(self):
        a = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        b = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        # Numerical metrics must be identical
        assert a["sharpe"] == b["sharpe"]
        assert a["total_return_pct"] == b["total_return_pct"]
        assert a["trade_count"] == b["trade_count"]
        assert a["final_value"] == b["final_value"]

    def test_different_seed_different_result(self):
        a = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        b = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=43)
        # At least one metric should differ
        assert (a["sharpe"], a["trade_count"]) != (b["sharpe"], b["trade_count"])


# ── Output schema (grader compatibility) ────────────────────────────────────

class TestSchema:

    def test_result_has_all_keys_grader_needs(self):
        result = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        required = [
            "sharpe", "max_drawdown_pct", "trade_count", "win_rate_pct",
            "total_return_pct", "max_single_market_concentration_pct",
        ]
        for k in required:
            assert k in result, f"backtest output missing key: {k}"

    def test_pnl_curve_present_and_correct_length(self):
        result = backtest_run(EXAMPLES / "near_certainty_bond.py", days=30, seed=42)
        assert "pnl_curve" in result
        assert len(result["pnl_curve"]) == 30

    def test_metrics_have_sensible_ranges(self):
        result = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        assert -100 <= result["total_return_pct"] <= 10000
        assert 0 <= result["max_drawdown_pct"] <= 100
        assert 0 <= result["win_rate_pct"] <= 100
        assert 0 <= result["max_single_market_concentration_pct"] <= 100
        assert result["trade_count"] >= 0


# ── The CRITICAL test — rubric discrimination ───────────────────────────────

class TestRubricDiscrimination:
    """If a good strategy passes the rubric and a bad one fails, Tape works.

    If both pass or both fail, the rubric isn't doing its job.
    """

    def test_good_strategy_outperforms_yolo_on_metrics(self):
        """Bond strategy should out-Sharpe the YOLO strategy."""
        bond = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        yolo = backtest_run(EXAMPLES / "yolo_buy_everything.py", seed=42)
        # Bond should outperform YOLO on at least 2 of: return, sharpe, drawdown
        wins = 0
        if bond["total_return_pct"] > yolo["total_return_pct"]:
            wins += 1
        if bond["sharpe"] > yolo["sharpe"]:
            wins += 1
        if bond["max_drawdown_pct"] < yolo["max_drawdown_pct"]:
            wins += 1
        assert wins >= 2, (
            f"Bond should beat YOLO on most metrics. "
            f"Bond: return={bond['total_return_pct']} sharpe={bond['sharpe']} dd={bond['max_drawdown_pct']}. "
            f"YOLO: return={yolo['total_return_pct']} sharpe={yolo['sharpe']} dd={yolo['max_drawdown_pct']}"
        )

    def test_yolo_strategy_fails_at_least_one_rubric_clause(self):
        """A buy-everything strategy must fail SOMETHING in the rubric."""
        yolo = backtest_run(EXAMPLES / "yolo_buy_everything.py", seed=42)
        verdict = grade(yolo, skip_opus=True)
        failed = [c for c in verdict.clauses if c.status == "fail"]
        assert len(failed) > 0, (
            "YOLO strategy passed every rubric clause — rubric isn't strict enough!"
        )


# ── Edge cases ──────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_do_nothing_strategy_doesnt_crash(self):
        """Strategy that returns HOLD always should still produce valid output."""
        result = backtest_run(EXAMPLES / "do_nothing.py", seed=42)
        assert result["trade_count"] == 0
        assert result["total_return_pct"] == 0.0  # no trades = no P&L
        assert result["wins"] == 0
        assert result["losses"] == 0
        # Win rate should be 0% (or be safely handled, not NaN)
        assert result["win_rate_pct"] == 0.0

    def test_runs_in_under_5_seconds(self):
        """Demo viability: backtests must be fast."""
        import time
        start = time.time()
        backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        elapsed = time.time() - start
        assert elapsed < 5.0, f"Backtest took {elapsed:.2f}s — too slow for demo"

    def test_strategy_exceptions_dont_crash_backtester(self):
        """If user's strategy throws, backtester should log and continue."""
        # Create a strategy that throws on decide()
        crash_strategy = (Path(__file__).parent / "crash_strategy.py")
        crash_strategy.write_text(
            "from tape.templates.strategy_base import TemplateStrategy, StrategyMeta\n"
            "class Strategy(TemplateStrategy):\n"
            "    META = StrategyMeta(name='crasher', description='crashes',\n"
            "                         risk_tolerance='moderate', max_position_usd=10.0,\n"
            "                         target_horizon_days=14)\n"
            "    def decide(self, market, position):\n"
            "        raise ValueError('intentional crash for testing')\n"
        )
        try:
            result = backtest_run(crash_strategy, seed=42)
            # Should produce a valid result (no trades, since every decide crashed)
            assert result["trade_count"] == 0
        finally:
            crash_strategy.unlink(missing_ok=True)


# ── End-to-end integration: backtester + grader ─────────────────────────────

class TestIntegration:

    def test_bond_strategy_grades_cleanly(self):
        """End-to-end: backtest bond strategy, grade against rubric."""
        result = backtest_run(EXAMPLES / "near_certainty_bond.py", seed=42)
        verdict = grade(result, skip_opus=True)
        # JSON-serializable
        json_str = verdict.to_json()
        assert "passed" in json_str
        assert "clauses" in json_str
        # The verdict must have the same 10 clauses as rubric.done_when
        assert len(verdict.clauses) == 10

    def test_full_pipeline_does_not_raise(self):
        """Sanity: run all three example strategies through backtest + grade."""
        for name in ("near_certainty_bond.py", "yolo_buy_everything.py", "do_nothing.py"):
            result = backtest_run(EXAMPLES / name, seed=42)
            verdict = grade(result, skip_opus=True)
            # Just verify the call doesn't throw
            assert verdict is not None
            assert hasattr(verdict, "passed")
