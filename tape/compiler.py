"""
tape/compiler.py
────────────────
Natural-language brief → Opus 4.8 → Python strategy file.

Pipeline:
  1. Read the user brief.
  2. Read prompts/compile.md (the system prompt) + strategy_base.py (the
     contract). Both go into the Opus 4.8 context window.
  3. Call Opus 4.8 with brief as the user message.
  4. Strip markdown fences if any, run structural validation (catches
     lookahead bias, network calls, print statements), syntax-compile.
  5. Write to strategies/<slug>.py and import via importlib to verify
     the Strategy class loads cleanly with no side effects.

Usage (programmatic):

    from tape.compiler import compile_strategy
    result = compile_strategy(
        brief="Buy NO on geopolitical markets at >$0.92, ≤14 days to resolve",
        out_dir=Path("strategies"),
    )
    print(result.strategy_path, result.import_ok)

Usage (CLI):
    python -m tape.compiler "Buy NO on geo markets at >$0.92"
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover — anthropic is a hard dependency
    Anthropic = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = REPO_ROOT / "tape" / "prompts" / "compile.md"
TEMPLATE_PATH = REPO_ROOT / "tape" / "templates" / "strategy_base.py"

DEFAULT_MODEL = os.environ.get("TAPE_LLM_MODEL", "claude-opus-4-8")
DEFAULT_MAX_TOKENS = 3000  # plenty for one strategy module


# ════════════════════════════════════════════════════════════════════════════
#  Result type
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class CompileResult:
    """The outcome of one compile call. JSON-serializable for the UI."""

    success: bool
    strategy_path: Optional[str] = None  # absolute path to the generated .py
    strategy_src: str = ""               # the source, in-memory
    strategy_name: str = ""              # parsed from META.name
    raw_response: str = ""               # what Opus actually returned
    import_ok: bool = False
    import_error: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "strategy_path": self.strategy_path,
            "strategy_name": self.strategy_name,
            "import_ok": self.import_ok,
            "import_error": self.import_error,
            "model": self.model,
            "tokens": {"in": self.input_tokens, "out": self.output_tokens},
            "error": self.error,
        }


# ════════════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════════════

def compile_strategy(
    brief: str,
    out_dir: Path | str = REPO_ROOT / "strategies",
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    write: bool = True,
) -> CompileResult:
    """Turn a natural-language brief into a Python strategy file.

    Args:
      brief:      one or two sentences of English describing the strategy
      out_dir:    where to write the generated .py
      model:      Opus deployment name (default: claude-opus-4-8)
      max_tokens: response cap
      write:      if False, run compile but skip writing to disk (useful for tests)

    Returns:
      CompileResult with .success and .strategy_path on the happy path.
      On failure, .error explains what broke.
    """
    out_dir = Path(out_dir)

    # 1. Load the system prompt + the contract
    if not PROMPT_PATH.exists():
        return CompileResult(success=False, model=model,
                             error=f"compile.md not found at {PROMPT_PATH}")
    if not TEMPLATE_PATH.exists():
        return CompileResult(success=False, model=model,
                             error=f"strategy_base.py not found at {TEMPLATE_PATH}")

    system_prompt = PROMPT_PATH.read_text()
    template_src = TEMPLATE_PATH.read_text()

    # 2. Call Opus 4.8
    if Anthropic is None:
        return CompileResult(success=False, model=model,
                             error="anthropic SDK not installed (pip install anthropic)")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return CompileResult(success=False, model=model,
                             error="ANTHROPIC_API_KEY not set")

    client = Anthropic(api_key=api_key, timeout=60.0, max_retries=2)

    user_msg = (
        f"## Full strategy_base.py contract (for reference)\n\n"
        f"```python\n{template_src}\n```\n\n"
        f"## User brief\n\n{brief.strip()}\n"
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return CompileResult(success=False, model=model,
                             error=f"Opus call failed: {e}")

    raw = "".join(b.text for b in resp.content if b.type == "text")
    code = _strip_markdown_fences(raw).strip() + "\n"

    in_toks = getattr(resp.usage, "input_tokens", 0)
    out_toks = getattr(resp.usage, "output_tokens", 0)

    # 3. Structural validation — cheap pre-checks before disk + import
    valid, why = _validate_structure(code)
    if not valid:
        return CompileResult(
            success=False, model=model, raw_response=raw,
            strategy_src=code, input_tokens=in_toks, output_tokens=out_toks,
            error=f"Generated code failed validation: {why}",
        )

    # 4. Pick a slug + decide path
    slug = _slug_for_brief(brief)
    out_path = out_dir / f"{slug}.py"
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(code)

    # 5. Import via importlib to verify it actually loads
    if write:
        import_target = out_path
    else:
        # Write to a temp file just for import verification
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, dir=str(out_dir.parent),
        ) as tf:
            tf.write(code)
            import_target = Path(tf.name)

    import_ok, import_error, strategy_name = _smoke_import_file(import_target)

    if not write:
        import_target.unlink(missing_ok=True)

    return CompileResult(
        success=import_ok,
        strategy_path=str(out_path) if write else None,
        strategy_src=code,
        strategy_name=strategy_name,
        raw_response=raw,
        import_ok=import_ok,
        import_error=import_error,
        model=model,
        input_tokens=in_toks,
        output_tokens=out_toks,
        error="" if import_ok else f"strategy compiled but does not import: {import_error}",
    )


# ════════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ════════════════════════════════════════════════════════════════════════════

def _strip_markdown_fences(text: str) -> str:
    """Strip the most common LLM-rendering artifacts from the response.

    Three artifacts we routinely see from Opus when generating long code:
      1. ```python ... ``` fences (despite the prompt asking for raw code)
      2. UI render leakage: literal "Copy code" / "复制" / "コピー" tokens
         that come from the model imagining a code-block copy button
      3. Trailing chatty commentary after the code

    We strip all three so the downstream compile() + importlib steps don't
    fail on what is otherwise valid Python.
    """
    text = text.strip()

    # 1. Markdown fences
    m = re.match(r"^```(?:python)?\n?(.*?)\n?```$", text, re.DOTALL)
    if m:
        text = m.group(1)

    # 2. UI-render artifacts — single tokens that look like "copy button" leakage.
    # These tend to appear on their own line, with no surrounding code.
    JUNK_TOKENS = {
        "copy", "copy code", "复制", "复制代码", "コピー",
        "copy_code", "copy-code",
    }
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped in JUNK_TOKENS:
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines).rstrip()

    return text


def _validate_structure(code: str) -> tuple[bool, str]:
    """Cheap structural checks before we go any deeper.

    Catches the most common failure modes (no Strategy class, import bug,
    syntax error) without spending time on disk + import.
    """
    if "class Strategy" not in code:
        return False, "no `class Strategy` definition found"
    if "META" not in code:
        return False, "no META class attribute found"
    if "def decide" not in code:
        return False, "no decide() method found"

    # Parse-check (compile builtin throws SyntaxError on bad code; doesn't run anything)
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"

    # Forbidden runtime patterns (lookahead bias, side effects, network)
    forbidden = [
        ("datetime.now",   "uses datetime.now() — lookahead bias"),
        ("datetime.today", "uses datetime.today() — lookahead bias"),
        ("time.time",      "uses time.time() — non-deterministic"),
        ("random.",        "uses random — non-deterministic"),
        ("requests.",      "uses requests — strategies must not make network calls"),
        ("print(",         "contains print() — pollutes audit log"),
    ]
    for needle, msg in forbidden:
        if needle in code:
            return False, msg

    return True, "ok"


def _smoke_import_file(strategy_path: Path) -> tuple[bool, str, str]:
    """Import the strategy via importlib in a unique module namespace.

    Uses spec_from_file_location + loader.exec_module — the standard
    safe Python pattern for "load a .py file as a module without using
    Python's exec() builtin on the source string."

    Returns (ok, error_message, strategy_name).
    """
    spec = importlib.util.spec_from_file_location("tape_generated_strategy", strategy_path)
    if spec is None or spec.loader is None:
        return False, "could not create import spec", ""

    module = importlib.util.module_from_spec(spec)

    try:
        # exec_module is importlib's standard loader call — it's what
        # `import x` does internally. NOT the same as builtin exec().
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — any failure here is user-facing
        return False, f"{type(e).__name__}: {e}", ""

    Strategy = getattr(module, "Strategy", None)
    if Strategy is None:
        return False, "module has no `Strategy` attribute after import", ""

    meta = getattr(Strategy, "META", None)
    if meta is None:
        return False, "Strategy.META is missing", ""

    name = getattr(meta, "name", "unnamed")
    return True, "", str(name)


def _slug_for_brief(brief: str) -> str:
    """Derive a filename slug from the brief.

    Lowercased, alphanumeric + underscore only, truncated to 40 chars.
    Adds a short hash suffix to avoid collisions on similar briefs.
    """
    import hashlib
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", brief.lower()).strip("_")
    cleaned = cleaned[:40] or "strategy"
    suffix = hashlib.sha1(brief.encode("utf-8")).hexdigest()[:6]
    return f"{cleaned}_{suffix}"


# ════════════════════════════════════════════════════════════════════════════
#  CLI entrypoint
# ════════════════════════════════════════════════════════════════════════════

def _cli() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="Compile a natural-language brief into a strategy.")
    p.add_argument("brief", help="The strategy brief, in plain English.")
    p.add_argument("--out", default=str(REPO_ROOT / "strategies"),
                   help="Output directory (default: strategies/)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Opus model (default: {DEFAULT_MODEL})")
    p.add_argument("--no-write", action="store_true",
                   help="Compile but don't write file to disk")
    p.add_argument("--json", action="store_true", help="Emit JSON result instead of text")
    args = p.parse_args()

    result = compile_strategy(
        brief=args.brief, out_dir=args.out, model=args.model, write=not args.no_write,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        if result.success:
            print(f"✅ Compiled strategy '{result.strategy_name}'")
            print(f"   File:   {result.strategy_path}")
            print(f"   Model:  {result.model}")
            print(f"   Tokens: {result.input_tokens} in, {result.output_tokens} out")
        else:
            print(f"❌ Compile failed: {result.error}")
            if result.raw_response:
                print("--- raw model response (first 400 chars) ---")
                print(result.raw_response[:400])

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    _cli()
