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
| `tape/templates/strategy_base.py` | Stub the compiler fills in | Plugs into pave-capital strategy interface |
| `web/server.py` | FastAPI demo UI | NO Streamlit (banned), HTMX for streaming |
| `web/stream.py` | SSE for Opus-generation live view | Watch-Opus-think effect |
| `web/static/index.html` | Single-page demo | 3 pre-baked examples + textarea |
| `tests/test_grader.py` | Verifies rubric correctness | The rubric must be itself verifiable |

---

## Prior work used as substrate

The hackathon rules permit augmenting prior projects with Claude during
the event (§Technologies & Projects). Tape uses `pave-capital` as its
trading runtime — but does not display, demo, or take credit for any
pave-capital functionality.

| Substrate repo | What we use it for |
|----------------|---------------------|
| [`pave-capital@65edb96`](https://github.com/priyank1119/pave-capital) | Trading execution library: order placement, risk controls, portfolio reconciliation, auto-redemption, Polymarket API wrappers |

**Note on the wallet**: the existing pave-capital wallet
(`0xf136157E...`) is referenced as a **benchmark only** for the README's
"+18% verified on-chain return" credibility claim. **Strategies deployed
via Tape spawn fresh sandbox wallets** with hard budget caps — they do
NOT trade against the existing capital.

**Note on the dashboard**: pave-capital includes a real-time dashboard.
This is NOT featured in Tape's demo. The Tape UI is a strategy-compilation
interface; if a dashboard is shown briefly, it is to verify a deployed bot
is alive, not as the product itself. (Hackathon rules disqualify projects
where "a dashboard is the main feature.")

---

## How to verify

```bash
# Read the file-level provenance:
git -C tape log --pretty=format:"%h %ai %s" -- '*.py' '*.md' '*.yaml'

# Compare against pave-capital (substrate):
git -C ../pave-capital log --before="2026-MM-DD HH:MM" --oneline | head
```

Every commit in this repo is dated within the Claude Build Day window.
