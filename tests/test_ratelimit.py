"""
tests/test_ratelimit.py
───────────────────────
Tests the daily spend guard: cap enforcement, persistence, daily rollover.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tape import ratelimit


@pytest.fixture
def tmp_counter(tmp_path, monkeypatch):
    """Point the rate limiter at a temp counter file."""
    counter = tmp_path / "rl.json"
    monkeypatch.setattr(ratelimit, "COUNTER_FILE", counter)
    return counter


class TestRateLimit:

    def test_allows_under_cap(self, tmp_counter):
        allowed, used, cap = ratelimit.check_and_increment(cap=5)
        assert allowed
        assert used == 1
        assert cap == 5

    def test_increments_across_calls(self, tmp_counter):
        for expected in range(1, 4):
            allowed, used, _ = ratelimit.check_and_increment(cap=5)
            assert allowed
            assert used == expected

    def test_blocks_at_cap(self, tmp_counter):
        for _ in range(3):
            ratelimit.check_and_increment(cap=3)
        allowed, used, cap = ratelimit.check_and_increment(cap=3)
        assert not allowed
        assert used == 3  # not incremented past the cap

    def test_persists_to_disk(self, tmp_counter):
        ratelimit.check_and_increment(cap=10)
        ratelimit.check_and_increment(cap=10)
        # Simulate a restart: file should hold the count
        data = json.loads(tmp_counter.read_text())
        assert data["count"] == 2

    def test_daily_rollover(self, tmp_counter):
        # Write a counter dated yesterday
        tmp_counter.write_text(json.dumps({"date": "2000-01-01", "count": 99}))
        # Next call should see a stale date and reset
        allowed, used, _ = ratelimit.check_and_increment(cap=5)
        assert allowed
        assert used == 1  # reset, not 100

    def test_status_does_not_increment(self, tmp_counter):
        ratelimit.check_and_increment(cap=10)
        s1 = ratelimit.status(cap=10)
        s2 = ratelimit.status(cap=10)
        assert s1["used"] == 1
        assert s2["used"] == 1  # status is read-only
        assert s1["remaining"] == 9

    def test_corrupt_counter_file_recovers(self, tmp_counter):
        tmp_counter.write_text("not valid json{{{")
        # Should not crash — resets gracefully
        allowed, used, _ = ratelimit.check_and_increment(cap=5)
        assert allowed
        assert used == 1
