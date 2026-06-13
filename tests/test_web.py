"""
tests/test_web.py
─────────────────
Tests for the FastAPI web server.

The /api/run SSE endpoint calls the real compiler (Opus), so we don't
exercise it in unit tests — it's covered by the live integration smoke
test. Here we verify the static routes, the deploy/probe API, and the
error handling, all without network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi.testclient import TestClient
    from web.server import app
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")

EXAMPLES = Path(__file__).resolve().parent.parent / "strategies" / "examples"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup_bots():
    yield
    try:
        from tape import deployer
        for bot in deployer.list_bots():
            deployer.stop_bot(bot["bot_id"])
    except ImportError:
        pass


class TestStaticRoutes:

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_index_serves_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Tape" in r.text
        assert "text/html" in r.headers["content-type"]

    def test_index_has_no_streamlit(self, client):
        """Hackathon rule: no Streamlit. Sanity-check the UI is hand-built."""
        r = client.get("/")
        assert "streamlit" not in r.text.lower()


class TestDeployApi:

    def test_deploy_and_probe(self, client):
        # Deploy a known-good example strategy
        r = client.post("/api/deploy", json={
            "strategy_path": str(EXAMPLES / "near_certainty_bond.py"),
            "budget": 25,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"]
        bot_id = data["bot_id"]

        # Probe it
        r2 = client.get(f"/api/bots/{bot_id}")
        assert r2.status_code == 200
        state = r2.json()
        assert state["health_status_code"] == 200
        assert state["strategy_name"] == "near_certainty_bond"

    def test_deploy_missing_path(self, client):
        r = client.post("/api/deploy", json={})
        assert r.status_code == 400

    def test_probe_unknown_bot_404(self, client):
        r = client.get("/api/bots/does_not_exist")
        assert r.status_code == 404

    def test_list_bots(self, client):
        client.post("/api/deploy", json={
            "strategy_path": str(EXAMPLES / "near_certainty_bond.py"),
        })
        r = client.get("/api/bots")
        assert r.status_code == 200
        assert "bots" in r.json()
        assert len(r.json()["bots"]) >= 1
