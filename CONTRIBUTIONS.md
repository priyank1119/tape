# Contributions

This file maps every component to "built today" (during Claude Build Day) vs
"prior work used as substrate." Required by hackathon rules — see
[hackathon rules](https://claude.com/build-day) §Demo Requirements.

---

## Built during Claude Build Day (in this repo)

Every file under `tape/`, `web/`, `strategies/`, `sandbox/`, and `tests/` was
written during the Build Day hackathon.

| File | What | Why it's new |
|------|------|-------------|
| `rubric.yaml` | Pass/fail spec for compiled strategies | First model-verifiable rubric for prediction-market bots |
| `tape/compiler.py` | Wraps Anthropic SDK; injects strategy template + market data | Bridges NL → Python with Opus 4.8 1M context |
| `tape/backtester.py` | Replays 90 days of Polymarket fills | Pulls from CLOB `/trades` endpoint, walk-forward sim |
| `tape/grader.py` | Reads backtest JSON + live probe; applies `rubric.yaml` | Returns pass/fail with explanation |
| `tape/swarm.py` | 5 parallel Opus 4.8 refinements → 6th Opus picks winner | Tournament-of-strategies pattern |
| `tape/deployer.py` | Spawns Docker container with compiled strategy injected | Sandbox isolation w/ $25 budget cap |
| `tape/prompts/compile.md` | Opus 4.8 strategy-compiler prompt | The IP — designed to leverage 1M context |
| `tape/prompts/critique.md` | Self-review prompt | Self-improving loop |
| `tape/prompts/tournament.md` | Pick-winner prompt | Multi-agent judging |
| `tape/templates/strategy_base.py` | The strategy interface compiled strategies implement | Self-contained contract; no external deps |
| `web/server.py` | FastAPI demo UI | NO Streamlit (banned), HTMX for streaming |
| `web/stream.py` | SSE for Opus-generation live view | Watch-Opus-think effect |
| `web/static/index.html` | Single-page demo | 3 pre-baked examples + textarea |
| `tests/test_grader.py` | Verifies rubric correctness | The rubric must be itself verifiable |

---

## Prior work (NOT part of this submission)

Tape was motivated by a personal Polymarket trading bot I've run for 80+
days. **Tape shares no code with that bot** — it imports nothing from it and
is fully self-contained. The prior bot is a private project; it is not
demoed, not displayed, and Tape takes no credit for it. It's mentioned only
to explain where the idea came from.

You can confirm there's no shared code:

```bash
# Tape imports nothing from any external trading project:
grep -rn "import" tape/ web/ | grep -iE "pave|external" || echo "no external trading imports"
```

**Note on trading safety**: Tape's deployer is a PAPER-TRADING sandbox
(`tape/deployer.py`). The "$10 budget" is an in-memory number — no real
wallet, no on-chain transaction, no real capital at risk.

**Note on the dashboard**: Tape's UI is a strategy-compilation interface,
not a dashboard. (Hackathon rules disqualify projects where "a dashboard is
the main feature.") The deployed-bot state view is a small verification
surface, not the product.

---

## How to verify everything is hackathon work

```bash
# Every commit in this repo is dated within the Claude Build Day window:
git -C tape log --pretty=format:"%h %ai %s"
```
