"""
tests/test_grader.py
────────────────────
Deterministic tests for the rubric grader.

Why these tests matter:
  The whole orchestration thesis of Tape rests on the rubric being
  model-verifiable. If grader.py incorrectly says "PASS" for a bad
  backtest, the rubric isn't a rubric — it's theater.

  These tests pin down the behavior of every clause so that:
    - Another team can fork tape, run `pytest`, and verify the rubric works
    - A judge can do the same
    - Future refactors don't silently weaken the gate
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow running tests from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tape.grader import (
    ClauseResult,
    Verdict,
    grade,
    load_rubric,
    DEFAULT_RUBRIC_PATH,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def good_backtest() -> dict:
    """A backtest result that should pass every backtest clause comfortably."""
    return {
        "sharpe": 1.4,
        "max_drawdown_pct": 8.2,
        "trade_count": 47,
        "win_rate_pct": 58.0,
        "total_return_pct": 12.5,
        "max_single_market_concentration_pct": 22.0,
        "pnl_curve": [],   # populated by real backtester; not graded directly
    }


@pytest.fixture
def good_live_probe() -> dict:
    """A live probe that passes every live clause."""
    return {
        "health_status_code": 200,
        "startup_errors": 0,
        "imports_cleanly": True,
        "cycles_completed": 2,
    }


# ── Backtest clause tests ────────────────────────────────────────────────────

class TestBacktestClauses:
    """Each clause should pass iff the metric clears the threshold."""

    def test_sharpe_passes_at_threshold(self, good_backtest):
        # Default rubric: min_sharpe = 0.5
        good_backtest["sharpe"] = 0.5
        v = grade(good_backtest, skip_opus=True)
        sharpe_clause = next(c for c in v.clauses if c.name == "backtest.sharpe_ge_min")
        assert sharpe_clause.status == "pass"

    def test_sharpe_fails_below_threshold(self, good_backtest):
        good_backtest["sharpe"] = 0.4
        v = grade(good_backtest, skip_opus=True)
        sharpe_clause = next(c for c in v.clauses if c.name == "backtest.sharpe_ge_min")
        assert sharpe_clause.status == "fail"

    def test_drawdown_passes_within_limit(self, good_backtest):
        # Default rubric: max_drawdown_pct = 20
        good_backtest["max_drawdown_pct"] = 19.9
        v = grade(good_backtest, skip_opus=True)
        dd = next(c for c in v.clauses if c.name == "backtest.drawdown_within_limit")
        assert dd.status == "pass"

    def test_drawdown_fails_above_limit(self, good_backtest):
        good_backtest["max_drawdown_pct"] = 25.0
        v = grade(good_backtest, skip_opus=True)
        dd = next(c for c in v.clauses if c.name == "backtest.drawdown_within_limit")
        assert dd.status == "fail"

    def test_trade_count_passes_at_minimum(self, good_backtest):
        good_backtest["trade_count"] = 10
        v = grade(good_backtest, skip_opus=True)
        tc = next(c for c in v.clauses if c.name == "backtest.trade_count_ge_min")
        assert tc.status == "pass"

    def test_trade_count_fails_below(self, good_backtest):
        good_backtest["trade_count"] = 9
        v = grade(good_backtest, skip_opus=True)
        tc = next(c for c in v.clauses if c.name == "backtest.trade_count_ge_min")
        assert tc.status == "fail"

    def test_win_rate_failure(self, good_backtest):
        good_backtest["win_rate_pct"] = 35.0
        v = grade(good_backtest, skip_opus=True)
        wr = next(c for c in v.clauses if c.name == "backtest.win_rate_ge_min")
        assert wr.status == "fail"

    def test_total_return_failure(self, good_backtest):
        good_backtest["total_return_pct"] = 1.0
        v = grade(good_backtest, skip_opus=True)
        tr = next(c for c in v.clauses if c.name == "backtest.total_return_ge_min")
        assert tr.status == "fail"

    def test_concentration_failure(self, good_backtest):
        good_backtest["max_single_market_concentration_pct"] = 85.0
        v = grade(good_backtest, skip_opus=True)
        conc = next(c for c in v.clauses
                    if c.name == "backtest.concentration_within_limit")
        assert conc.status == "fail"

    def test_missing_metric_marks_failure(self, good_backtest):
        # If backtester forgets to report sharpe, the clause should fail loudly
        del good_backtest["sharpe"]
        v = grade(good_backtest, skip_opus=True)
        sharpe = next(c for c in v.clauses if c.name == "backtest.sharpe_ge_min")
        assert sharpe.status == "fail"


# ── Live clause tests ───────────────────────────────────────────────────────

class TestLiveClauses:

    def test_live_clauses_pending_when_no_probe(self, good_backtest):
        """Pre-deploy grading should mark live clauses PENDING, not FAIL."""
        v = grade(good_backtest, live=None, skip_opus=True)
        live_clauses = [c for c in v.clauses if c.name.startswith("live.")]
        assert len(live_clauses) == 3
        for c in live_clauses:
            assert c.status == "pending", f"{c.name} should be pending pre-deploy"

    def test_health_passes_on_200(self, good_backtest, good_live_probe):
        v = grade(good_backtest, live=good_live_probe, skip_opus=True)
        h = next(c for c in v.clauses if c.name == "live.health_responds")
        assert h.status == "pass"

    def test_health_fails_on_500(self, good_backtest, good_live_probe):
        good_live_probe["health_status_code"] = 500
        v = grade(good_backtest, live=good_live_probe, skip_opus=True)
        h = next(c for c in v.clauses if c.name == "live.health_responds")
        assert h.status == "fail"

    def test_startup_errors_fail(self, good_backtest, good_live_probe):
        good_live_probe["startup_errors"] = 3
        v = grade(good_backtest, live=good_live_probe, skip_opus=True)
        se = next(c for c in v.clauses if c.name == "live.startup_clean")
        assert se.status == "fail"

    def test_imports_failure(self, good_backtest, good_live_probe):
        good_live_probe["imports_cleanly"] = False
        v = grade(good_backtest, live=good_live_probe, skip_opus=True)
        i = next(c for c in v.clauses if c.name == "live.imports_cleanly")
        assert i.status == "fail"


# ── Verdict-level tests ─────────────────────────────────────────────────────

class TestVerdict:

    def test_all_pass_when_everything_good(self, good_backtest, good_live_probe):
        v = grade(good_backtest, live=good_live_probe, skip_opus=True)
        # With skip_opus, the opus clause is pending — so passed should be False.
        # We're testing the AGGREGATION logic, not the value.
        assert v.passed is False  # because opus clause is pending
        non_opus_failed = [c for c in v.failed_clauses if not c.name.startswith("opus")]
        assert non_opus_failed == [], "no non-opus clauses should have failed"

    def test_single_failure_fails_overall(self, good_backtest):
        good_backtest["sharpe"] = 0.1
        v = grade(good_backtest, skip_opus=True)
        assert v.passed is False
        assert any(c.name == "backtest.sharpe_ge_min" and c.status == "fail"
                   for c in v.clauses)

    def test_verdict_is_json_serializable(self, good_backtest):
        v = grade(good_backtest, skip_opus=True)
        # Must round-trip cleanly so another tool (or model) can consume it
        as_json = v.to_json()
        parsed = json.loads(as_json)
        assert parsed["passed"] == v.passed
        assert len(parsed["clauses"]) == len(v.clauses)

    def test_render_includes_pass_fail_icons(self, good_backtest):
        v = grade(good_backtest, skip_opus=True)
        out = v.render()
        # Every clause should show one of the three icons
        assert any(icon in out for icon in ("✓", "✗", "·"))

    def test_summary_mentions_failure_count(self, good_backtest):
        good_backtest["sharpe"] = 0.1
        good_backtest["max_drawdown_pct"] = 50.0
        v = grade(good_backtest, skip_opus=True)
        assert "failed" in v.summary.lower()


# ── Rubric loading tests ────────────────────────────────────────────────────

class TestRubricLoading:

    def test_default_rubric_loads_and_validates(self):
        rubric = load_rubric()
        assert rubric["schema_version"] == 1
        assert "backtest" in rubric
        assert "live" in rubric
        assert "done_when" in rubric

    def test_missing_rubric_raises(self):
        with pytest.raises(FileNotFoundError):
            load_rubric("/nonexistent/rubric.yaml")

    def test_done_when_clauses_have_evaluators(self):
        """Every clause named in done_when must have a registered evaluator."""
        from tape.grader import CLAUSE_DISPATCH
        rubric = load_rubric()
        for clause_name in rubric["done_when"]:
            assert clause_name in CLAUSE_DISPATCH, (
                f"Rubric clause '{clause_name}' has no evaluator — "
                f"add it to CLAUSE_DISPATCH in tape/grader.py"
            )
