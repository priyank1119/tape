"""
web/server.py
─────────────
FastAPI server for the Tape demo.

The UI is intentionally minimal — a single page where you type a strategy
brief and watch the pipeline run live via Server-Sent Events (SSE):

    compile (Opus 4.8) → backtest → grade → deploy

This is NOT a dashboard product (hackathon rule: no projects where a
dashboard is the main feature). The main feature is the compile→deploy
pipeline; the page is just the surface that drives it and streams progress.

Endpoints:
  GET  /                      the single-page UI
  GET  /api/run?brief=...     SSE stream of pipeline progress
  POST /api/deploy            deploy a graded strategy to the sandbox
  GET  /api/bots/{id}         probe a deployed bot
  GET  /health               liveness for the deploy platform
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tape.web")

app = FastAPI(title="Tape", description="Plain English → deployed Polymarket bot")

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ════════════════════════════════════════════════════════════════════════════
#  Pages
# ════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "tape"}


@app.get("/api/limit")
async def api_limit():
    """Show today's demo run usage vs the daily cap."""
    from tape.ratelimit import status
    return JSONResponse(status())


# ════════════════════════════════════════════════════════════════════════════
#  Pipeline streaming (SSE)
# ════════════════════════════════════════════════════════════════════════════

def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/run")
async def api_run(brief: str, skip_opus: bool = False):
    """Stream the full pipeline as SSE so the UI shows live progress.

    Events emitted:
      step      — a pipeline stage started ({stage, message})
      compile   — compile result ({success, strategy_name, tokens, ...})
      backtest  — backtest metrics
      grade     — verdict with clauses
      done      — final verdict ({deployable, strategy_path})
      error     — something broke
    """

    async def stream():
        # Run blocking work in a thread so the event loop stays responsive
        loop = asyncio.get_event_loop()

        # ── Spend guard ──────────────────────────────────────────────────
        # Each run costs ~$0.20 of Opus tokens on a public URL. Cap daily
        # usage so a scripted client can't drain the API key.
        from tape.ratelimit import check_and_increment
        allowed, used, cap = check_and_increment()
        if not allowed:
            yield _sse("error", {
                "message": f"Daily demo limit reached ({used}/{cap} runs today). "
                           f"This protects the shared API key. Try again tomorrow, "
                           f"or run Tape locally with your own key — see the GitHub repo."
            })
            yield _sse("done", {"deployable": False, "reason": "rate_limited"})
            return

        try:
            from tape.compiler import compile_strategy
            from tape.backtester import run as backtest_run
            from tape.grader import grade

            # ── Compile ──────────────────────────────────────────────────
            yield _sse("step", {"stage": "compile",
                                "message": "Compiling strategy with Opus 4.8…"})
            comp = await loop.run_in_executor(
                None, lambda: compile_strategy(brief))
            if not comp.success:
                yield _sse("compile", {"success": False, "error": comp.error})
                yield _sse("done", {"deployable": False, "reason": comp.error})
                return
            yield _sse("compile", {
                "success": True,
                "strategy_name": comp.strategy_name,
                "strategy_src": comp.strategy_src,
                "tokens_in": comp.input_tokens,
                "tokens_out": comp.output_tokens,
                "model": comp.model,
            })

            # ── Backtest ─────────────────────────────────────────────────
            yield _sse("step", {"stage": "backtest",
                                "message": "Backtesting against 90 days of markets…"})
            bt = await loop.run_in_executor(
                None, lambda: backtest_run(comp.strategy_path))
            yield _sse("backtest", {
                "sharpe": bt["sharpe"],
                "total_return_pct": bt["total_return_pct"],
                "max_drawdown_pct": bt["max_drawdown_pct"],
                "trade_count": bt["trade_count"],
                "win_rate_pct": bt["win_rate_pct"],
                "pnl_curve": bt["pnl_curve"],
                "final_value": bt["final_value"],
            })

            # ── Grade ────────────────────────────────────────────────────
            yield _sse("step", {"stage": "grade",
                                "message": "Grading against rubric.yaml…"})
            src = Path(comp.strategy_path).read_text()
            verdict = await loop.run_in_executor(
                None, lambda: grade(bt, strategy_src=src, skip_opus=skip_opus))
            yield _sse("grade", {
                "passed": verdict.passed,
                "summary": verdict.summary,
                "clauses": [{"name": c.name, "status": c.status,
                             "actual": c.actual, "threshold": c.threshold}
                            for c in verdict.clauses],
                "opus_verdict": verdict.opus_verdict,
                "opus_critique": verdict.opus_critique,
            })

            non_live_failed = [c for c in verdict.clauses
                               if c.status == "fail" and not c.name.startswith("live.")]
            deployable = len(non_live_failed) == 0

            yield _sse("done", {
                "deployable": deployable,
                "strategy_path": comp.strategy_path,
                "strategy_name": comp.strategy_name,
            })

        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline error")
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(stream(), media_type="text/event-stream")


# ════════════════════════════════════════════════════════════════════════════
#  Deployment
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/swarm")
async def api_swarm(strategy_path: str):
    """Stream an Opus 4.8 tournament for an already-compiled strategy.

    Events:
      step      — stage updates ({stage, message})
      variant   — one variant's backtest result (streamed as each completes)
      winner    — the judge's final pick ({winner_id, ranking, reason})
      error
    """
    async def stream():
        loop = asyncio.get_event_loop()
        try:
            from tape.backtester import run as backtest_run
            from tape.swarm import run_tournament

            yield _sse("step", {"stage": "swarm",
                                "message": "Backtesting v1, then spawning 5 Opus 4.8 refiners…"})

            def _work():
                bt = backtest_run(strategy_path, seed=42)
                return run_tournament(strategy_path, bt)

            result = await loop.run_in_executor(None, _work)

            for v in result.variants:
                yield _sse("variant", v.to_dict())

            yield _sse("winner", {
                "winner_id": result.winner_id,
                "ranking": result.ranking,
                "reason": result.judge_reason,
            })
        except Exception as e:  # noqa: BLE001
            logger.exception("swarm error")
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/deploy")
async def api_deploy(request: Request):
    body = await request.json()
    strategy_path = body.get("strategy_path")
    budget = float(body.get("budget", 25.0))
    if not strategy_path:
        return JSONResponse({"success": False, "error": "missing strategy_path"}, status_code=400)

    from tape.deployer import deploy_strategy
    result = deploy_strategy(strategy_path, budget_usd=budget)
    return JSONResponse(result)


@app.get("/api/bots/{bot_id}")
async def api_bot(bot_id: str):
    from tape.deployer import probe
    state = probe(bot_id)
    if state is None:
        return JSONResponse({"error": "bot not found"}, status_code=404)
    return JSONResponse(state)


@app.get("/api/bots")
async def api_bots():
    from tape.deployer import list_bots
    return JSONResponse({"bots": list_bots()})


# ════════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ════════════════════════════════════════════════════════════════════════════

def main():
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
