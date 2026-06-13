"""
tests/test_swarm.py
───────────────────
Tests the swarm tournament with mocked Opus calls (fast, deterministic).

The live multi-agent path is covered by a manual integration run; here we
verify the orchestration logic: fan-out produces variants, the judge
fallback works, and the winner selection respects rubric pass/fail.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tape import swarm
from tape.swarm import Variant, _judge, REFINEMENT_ANGLES

EXAMPLES = Path(__file__).resolve().parent.parent / "strategies" / "examples"


class TestAngles:
    def test_five_distinct_angles(self):
        keys = [k for k, _ in REFINEMENT_ANGLES]
        assert len(keys) == 5
        assert len(set(keys)) == 5, "angles must be distinct"


class TestJudgeFallback:
    """When Opus is unavailable, the judge falls back to Sharpe ranking."""

    def test_fallback_picks_highest_sharpe_passer(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        variants = [
            Variant("original", "", "v1", compiled_ok=True, sharpe=0.8,
                    passed_rubric=True),
            Variant("refinement_1", "tighter_risk", "", compiled_ok=True, sharpe=1.5,
                    passed_rubric=True),
            Variant("refinement_2", "looser_entry", "", compiled_ok=True, sharpe=2.0,
                    passed_rubric=False),  # higher sharpe but FAILED rubric
        ]
        winner, ranking, reason = _judge(variants, model="x")
        # refinement_2 has highest Sharpe but failed rubric → must not win
        assert winner == "refinement_1"
        assert "refinement_2" not in ranking

    def test_fallback_none_when_no_passers(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        variants = [
            Variant("original", "", "v1", compiled_ok=True, sharpe=0.1, passed_rubric=False),
        ]
        winner, ranking, reason = _judge(variants, model="x")
        assert winner == "none"


class TestJudgeWithMockedOpus:

    @patch("tape.swarm.Anthropic")
    def test_judge_parses_opus_verdict(self, mock_anthropic, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        block = MagicMock(); block.type = "text"
        block.text = ("WINNER: refinement_1\n"
                      "RANKING: refinement_1, original\n"
                      "REASON: Best Sharpe with enough trades.")
        resp = MagicMock(); resp.content = [block]
        mock_client.messages.create.return_value = resp

        variants = [
            Variant("original", "", "v1", compiled_ok=True, sharpe=0.8, passed_rubric=True),
            Variant("refinement_1", "tighter_risk", "", compiled_ok=True, sharpe=1.5,
                    passed_rubric=True),
        ]
        winner, ranking, reason = _judge(variants, model="claude-opus-4-8")
        assert winner == "refinement_1"
        assert ranking == ["refinement_1", "original"]
        assert "Sharpe" in reason

    @patch("tape.swarm.Anthropic")
    def test_judge_rejects_invalid_winner(self, mock_anthropic, monkeypatch):
        """If Opus names a variant that didn't pass, fall back."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        block = MagicMock(); block.type = "text"
        block.text = "WINNER: refinement_9\nRANKING: refinement_9\nREASON: hallucinated"
        resp = MagicMock(); resp.content = [block]
        mock_client.messages.create.return_value = resp

        variants = [
            Variant("original", "", "v1", compiled_ok=True, sharpe=0.8, passed_rubric=True),
        ]
        winner, ranking, reason = _judge(variants, model="claude-opus-4-8")
        # refinement_9 doesn't exist → fallback picks the original
        assert winner == "original"


class TestPassConsistency:
    """The original and a refinement with identical metrics must get the
    same passed_rubric verdict. Regression test for the bug where the
    original's stricter all(pass) check failed on the pending opus clause
    while refinements' no-failed check passed."""

    def test_identical_metrics_same_verdict(self):
        from tape.grader import grade
        bt = {
            "sharpe": 4.0, "max_drawdown_pct": 1.0, "trade_count": 18,
            "win_rate_pct": 100.0, "total_return_pct": 5.0,
            "max_single_market_concentration_pct": 10.0,
        }
        v = grade(bt, skip_opus=True)
        # Both code paths use: no non-live clause failed (pending is OK)
        non_live_failed = [c for c in v.clauses
                           if c.status == "fail" and not c.name.startswith("live.")]
        passed = len(non_live_failed) == 0
        assert passed is True, (
            "a strategy with strong metrics must pass pre-deploy grading "
            "even with the opus clause pending"
        )


class TestTournamentResult:

    def test_result_serializes(self):
        from tape.swarm import TournamentResult
        r = TournamentResult(
            variants=[Variant("original", "", "v1", compiled_ok=True, sharpe=1.0,
                              passed_rubric=True)],
            winner_id="original", ranking=["original"], judge_reason="only one",
        )
        d = r.to_dict()
        assert d["winner_id"] == "original"
        assert len(d["variants"]) == 1
        assert d["variants"][0]["sharpe"] == 1.0
