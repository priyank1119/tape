"""
tests/test_compiler.py
──────────────────────
Tests for the compiler that don't require a network call.

We mock anthropic.Anthropic so the tests are deterministic and run in
milliseconds. The end-to-end "real Opus" path is covered by an integration
test (run manually with ANTHROPIC_API_KEY set).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tape.compiler import (
    CompileResult,
    _slug_for_brief,
    _strip_markdown_fences,
    _validate_structure,
    compile_strategy,
)


# ── Fence stripping ─────────────────────────────────────────────────────────

class TestFenceStripping:

    def test_strips_python_fence(self):
        text = "```python\nclass Strategy: pass\n```"
        assert _strip_markdown_fences(text) == "class Strategy: pass"

    def test_strips_bare_fence(self):
        text = "```\nclass Strategy: pass\n```"
        assert _strip_markdown_fences(text) == "class Strategy: pass"

    def test_passes_through_unfenced(self):
        text = "class Strategy: pass"
        assert _strip_markdown_fences(text) == "class Strategy: pass"

    def test_handles_leading_trailing_whitespace(self):
        text = "  \n```python\nclass Strategy: pass\n```\n  "
        assert _strip_markdown_fences(text) == "class Strategy: pass"

    def test_strips_copy_button_artifact_chinese(self):
        # Opus 4.8 sometimes appends '复制' (Chinese 'copy') — UI render leakage
        text = "class Strategy: pass\n\n复制"
        assert _strip_markdown_fences(text) == "class Strategy: pass"

    def test_strips_copy_button_artifact_english(self):
        text = "class Strategy: pass\n\nCopy code"
        assert _strip_markdown_fences(text) == "class Strategy: pass"

    def test_strips_artifact_inside_fence(self):
        text = "```python\nclass Strategy: pass\n复制\n```"
        assert _strip_markdown_fences(text) == "class Strategy: pass"


# ── Structural validation ──────────────────────────────────────────────────

class TestStructuralValidation:

    def test_rejects_missing_strategy_class(self):
        ok, why = _validate_structure("class NotStrategy: pass")
        assert not ok and "Strategy" in why

    def test_rejects_missing_meta(self):
        code = "class Strategy:\n    def decide(self, m, p): pass"
        ok, why = _validate_structure(code)
        assert not ok and "META" in why

    def test_rejects_missing_decide(self):
        code = "class Strategy:\n    META = None\n    def other(self): pass"
        ok, why = _validate_structure(code)
        assert not ok and "decide" in why

    def test_rejects_syntax_error(self):
        code = "class Strategy:\n    META = None\n    def decide(self, m, p:\n        pass"
        ok, why = _validate_structure(code)
        assert not ok and "SyntaxError" in why

    def test_rejects_datetime_now_lookahead(self):
        code = (
            "from datetime import datetime\n"
            "class Strategy:\n"
            "    META = None\n"
            "    def decide(self, m, p):\n"
            "        x = datetime.now()\n"
            "        return None\n"
        )
        ok, why = _validate_structure(code)
        assert not ok and "lookahead" in why.lower()

    def test_rejects_random_calls(self):
        code = (
            "import random\n"
            "class Strategy:\n"
            "    META = None\n"
            "    def decide(self, m, p):\n"
            "        x = random.random()\n"
            "        return None\n"
        )
        ok, why = _validate_structure(code)
        assert not ok and "random" in why.lower()

    def test_rejects_network_calls(self):
        code = (
            "import requests\n"
            "class Strategy:\n"
            "    META = None\n"
            "    def decide(self, m, p):\n"
            "        x = requests.get('https://example.com')\n"
            "        return None\n"
        )
        ok, why = _validate_structure(code)
        assert not ok and "requests" in why.lower()

    def test_rejects_print_statements(self):
        code = (
            "class Strategy:\n"
            "    META = None\n"
            "    def decide(self, m, p):\n"
            "        print('side effect')\n"
            "        return None\n"
        )
        ok, why = _validate_structure(code)
        assert not ok and "print" in why.lower()

    def test_accepts_minimal_valid_strategy(self):
        code = (
            "class Strategy:\n"
            "    META = 'ok'\n"
            "    def decide(self, m, p):\n"
            "        return None\n"
        )
        ok, why = _validate_structure(code)
        assert ok, f"should accept minimal strategy but said: {why}"


# ── Slug derivation ─────────────────────────────────────────────────────────

class TestSlug:

    def test_alphanumeric_with_hash_suffix(self):
        slug = _slug_for_brief("Buy NO on geopolitical markets")
        assert slug.startswith("buy_no_on_geopolitical_markets")
        # 6-char hash suffix
        parts = slug.split("_")
        assert len(parts[-1]) == 6

    def test_different_briefs_get_different_slugs(self):
        a = _slug_for_brief("Buy NO at 0.92")
        b = _slug_for_brief("Buy NO at 0.93")
        assert a != b, "different briefs must produce different slugs"

    def test_handles_punctuation(self):
        slug = _slug_for_brief("Buy NO @ >$0.90 & sell @ <$0.50!")
        # No special chars in slug
        assert all(c.isalnum() or c == "_" for c in slug)

    def test_caps_length(self):
        long_brief = "Buy NO on markets " * 30
        slug = _slug_for_brief(long_brief)
        # Pre-hash part is capped at 40
        pre_hash = slug.rsplit("_", 1)[0]
        assert len(pre_hash) <= 40


# ── End-to-end with mocked Opus ────────────────────────────────────────────

class TestCompileWithMockedOpus:

    GOOD_RESPONSE = '''"""Compiled strategy: dummy_geo_bond"""

from tape.templates.strategy_base import (
    TemplateStrategy, StrategyMeta, Market, Position, Decision,
)


class Strategy(TemplateStrategy):
    META = StrategyMeta(
        name="dummy_geo_bond",
        description="Test strategy",
        risk_tolerance="moderate",
        max_position_usd=10.0,
        target_horizon_days=14,
    )

    def decide(self, market, position):
        if position is None and market.current_price >= 0.92:
            return Decision(action="BUY", confidence=0.8, reason="entry")
        return Decision.hold("no signal")
'''

    @patch("tape.compiler.Anthropic")
    def test_happy_path(self, mock_anthropic_class, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Build a mock response shaped like a real Anthropic SDK response
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = self.GOOD_RESPONSE
        mock_response.content = [text_block]
        mock_response.usage = MagicMock(input_tokens=1500, output_tokens=300)
        mock_client.messages.create.return_value = mock_response

        result = compile_strategy(
            brief="Buy NO on geo markets at >$0.92",
            out_dir=tmp_path,
            write=True,
        )

        assert result.success, f"expected success, got error: {result.error}"
        assert result.strategy_name == "dummy_geo_bond"
        assert result.import_ok
        assert result.input_tokens == 1500
        assert result.output_tokens == 300
        assert Path(result.strategy_path).exists()

    @patch("tape.compiler.Anthropic")
    def test_rejects_malformed_response(self, mock_anthropic_class, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client

        bad_block = MagicMock()
        bad_block.type = "text"
        bad_block.text = "I cannot help with that request."
        bad_response = MagicMock()
        bad_response.content = [bad_block]
        bad_response.usage = MagicMock(input_tokens=100, output_tokens=20)
        mock_client.messages.create.return_value = bad_response

        result = compile_strategy(
            brief="Build me a strategy",
            out_dir=tmp_path,
            write=True,
        )

        assert not result.success
        assert "class Strategy" in result.error or "validation" in result.error.lower()

    def test_missing_api_key_fails_fast(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        result = compile_strategy(
            brief="any brief",
            out_dir=tmp_path,
        )

        assert not result.success
        assert "ANTHROPIC_API_KEY" in result.error
