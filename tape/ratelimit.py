"""
tape/ratelimit.py
─────────────────
Simple persistent spend guard for the public demo.

The Tape demo runs on a public URL with a real Anthropic API key behind it.
Each /api/run call costs ~$0.20 of Opus 4.8 tokens. Without a guard, anyone
who finds the URL could script thousands of calls and drain the key.

This module provides a per-UTC-day run cap that:
  - Persists to a small JSON file so it survives systemd restarts
    (an in-memory counter would reset on every crash/restart)
  - Resets automatically at UTC midnight (a daily budget, not a hard
    lifetime cap — friendlier and self-healing)
  - Is cheap: one file read + write per run

Configure via env:
  TAPE_DAILY_RUN_CAP   max /api/run calls per UTC day (default 200)
  TAPE_RATELIMIT_FILE  path to the counter file (default .ratelimit.json)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAP = int(os.environ.get("TAPE_DAILY_RUN_CAP", "200"))
COUNTER_FILE = Path(os.environ.get(
    "TAPE_RATELIMIT_FILE", str(REPO_ROOT / ".ratelimit.json")))

_LOCK = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    if not COUNTER_FILE.exists():
        return {"date": _today(), "count": 0}
    try:
        data = json.loads(COUNTER_FILE.read_text())
        # Reset if the stored date isn't today (daily budget rollover)
        if data.get("date") != _today():
            return {"date": _today(), "count": 0}
        return data
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ratelimit: could not read counter, resetting: {e}")
        return {"date": _today(), "count": 0}


def _save(data: dict) -> None:
    try:
        tmp = COUNTER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(COUNTER_FILE)   # atomic
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ratelimit: could not persist counter: {e}")


def check_and_increment(cap: int = DEFAULT_CAP) -> tuple[bool, int, int]:
    """Atomically check the daily cap and increment the counter.

    Returns (allowed, used, cap):
      - allowed: True if this call is within the cap (and was counted)
      - used:    how many runs have been used today (after this one if allowed)
      - cap:     the configured daily cap

    Thread-safe via a module lock (the demo server is single-process).
    """
    with _LOCK:
        data = _load()
        if data["count"] >= cap:
            return False, data["count"], cap
        data["count"] += 1
        _save(data)
        return True, data["count"], cap


def status(cap: int = DEFAULT_CAP) -> dict:
    """Read current usage without incrementing (for a status endpoint)."""
    with _LOCK:
        data = _load()
    return {"date": data["date"], "used": data["count"], "cap": cap,
            "remaining": max(0, cap - data["count"])}
