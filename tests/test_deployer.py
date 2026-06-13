"""
tests/test_deployer.py
──────────────────────
Tests the sandbox deployer: deploy → probe → stop lifecycle.

These verify the live-clause inputs the grader consumes are produced
correctly, without needing real Polymarket connectivity (the deployer
uses synthetic markets in sandbox mode).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tape import deployer

EXAMPLES = Path(__file__).resolve().parent.parent / "strategies" / "examples"


@pytest.fixture(autouse=True)
def _cleanup_bots():
    """Stop all bots after each test so threads don't leak."""
    yield
    for bot in deployer.list_bots():
        deployer.stop_bot(bot["bot_id"])


class TestDeployLifecycle:

    def test_deploy_returns_bot_id(self):
        result = deployer.deploy_strategy(EXAMPLES / "near_certainty_bond.py", budget_usd=25.0)
        assert result["success"]
        assert "bot_id" in result
        assert result["strategy_name"] == "near_certainty_bond"
        assert result["health_url"].startswith("/bots/")

    def test_probe_returns_health(self):
        result = deployer.deploy_strategy(EXAMPLES / "near_certainty_bond.py")
        bot_id = result["bot_id"]
        state = deployer.probe(bot_id)
        assert state is not None
        assert state["health_status_code"] == 200
        assert state["imports_cleanly"] is True
        assert state["startup_errors"] == 0

    def test_probe_unknown_bot_returns_none(self):
        assert deployer.probe("nonexistent_bot_id") is None

    def test_deploy_nonexistent_strategy_fails(self):
        result = deployer.deploy_strategy("/no/such/strategy.py")
        assert not result["success"]
        assert "not found" in result["error"]

    def test_budget_becomes_paper_cash(self):
        result = deployer.deploy_strategy(EXAMPLES / "near_certainty_bond.py", budget_usd=40.0)
        state = deployer.probe(result["bot_id"])
        # Paper cash starts at the budget (may have dipped if a cycle already bought)
        assert state["paper_cash"] <= 40.0

    def test_bot_completes_cycles(self):
        """A deployed bot should complete at least one cycle quickly."""
        # Temporarily shorten the cycle interval so the test is fast
        original = deployer.CYCLE_INTERVAL_SECS
        deployer.CYCLE_INTERVAL_SECS = 1
        try:
            result = deployer.deploy_strategy(EXAMPLES / "near_certainty_bond.py")
            bot_id = result["bot_id"]
            # Wait up to 8s for the first cycle
            deadline = time.time() + 8
            cycles = 0
            while time.time() < deadline:
                state = deployer.probe(bot_id)
                cycles = state["cycles_completed"]
                if cycles >= 1:
                    break
                time.sleep(0.5)
            assert cycles >= 1, "bot did not complete a cycle within 8s"
        finally:
            deployer.CYCLE_INTERVAL_SECS = original

    def test_stop_bot(self):
        result = deployer.deploy_strategy(EXAMPLES / "near_certainty_bond.py")
        assert deployer.stop_bot(result["bot_id"]) is True
        assert deployer.stop_bot("unknown") is False

    def test_list_bots(self):
        deployer.deploy_strategy(EXAMPLES / "near_certainty_bond.py")
        deployer.deploy_strategy(EXAMPLES / "do_nothing.py")
        bots = deployer.list_bots()
        assert len(bots) >= 2


class TestDeployGraderIntegration:
    """The probe output must satisfy the grader's live clauses."""

    def test_probe_output_passes_live_clauses(self):
        from tape.grader import grade

        # A passing backtest (so backtest clauses pass)
        good_bt = {
            "sharpe": 1.5, "max_drawdown_pct": 5.0, "trade_count": 20,
            "win_rate_pct": 80.0, "total_return_pct": 8.0,
            "max_single_market_concentration_pct": 15.0,
        }
        result = deployer.deploy_strategy(EXAMPLES / "near_certainty_bond.py")
        live = deployer.probe(result["bot_id"])

        verdict = grade(good_bt, live=live, skip_opus=True)
        # Every live clause should now PASS (not pending, not fail)
        live_clauses = [c for c in verdict.clauses if c.name.startswith("live.")]
        for c in live_clauses:
            assert c.status == "pass", f"{c.name} should pass, got {c.status}: {c.detail}"
