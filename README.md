# Tape

**Plain English → backtested → deployed Polymarket trading bot in 60 seconds.**

Type a strategy. Watch Claude Opus 4.8 compile it to Python.
See it backtested against 90 days of real market data.
Deploy it as a live bot with a hard $25 budget cap.

Built for [Claude Build Day](https://claude.com/build-day) hackathon.

---

## What problem does Tape solve?

Active prediction-market traders have a **discipline problem**: they know what
their strategy should be, but can't execute it consistently — because they
can't code a bot, can't watch the markets 24/7, and panic-second-guess at the
worst moments.

Tape lets them lock a strategy into runnable code by typing it in plain
English. Once compiled, the bot trades the strategy without them.

This is the productized version of the trading bot I've been running on my
own wallet for 80 days at **+$64.79 / +18% verified on-chain return**. That
substrate is open source as well: [pave-capital](https://github.com/priyank1119/pave-capital).

---

## Demo

```text
You type:
  "Buy NO on geopolitical conflict markets at >$0.92 that resolve
   within 14 days. Skip markets below $50k liquidity."

Tape does (~60s):
  1. Opus 4.8 compiles your sentence into a Python strategy module
  2. Backtester replays 90 days of Polymarket fills → JSON metrics
  3. Rubric grader applies rubric.yaml → PASS or FAIL with reasoning
  4. (PASS) Sandbox deployer spawns a budgeted bot container
  5. Bot's first cycle log streams back to you in real time
```

Live URL: **TBD (deploying tonight)**

---

## Repo structure

```
tape/
├── rubric.yaml         # Model-verifiable pass/fail criteria
├── tape/
│   ├── compiler.py     # NL → Python strategy (Opus 4.8)
│   ├── backtester.py   # Historical CLOB replay
│   ├── grader.py       # Reads rubric → pass/fail
│   ├── swarm.py        # 5-agent strategy tournament
│   ├── deployer.py     # Spawns isolated bot container
│   └── prompts/
│       ├── compile.md      # The Opus 4.8 prompt (the IP)
│       ├── critique.md     # Self-review prompt
│       └── tournament.md   # Pick-winner prompt
├── web/
│   └── server.py       # FastAPI + HTMX demo UI (no Streamlit)
├── strategies/         # Compiled strategies land here
└── sandbox/            # Docker isolation for deployed bots
```

---

## Built today (Claude Build Day) vs prior work

**Built during the hackathon** (in this repo):
- Strategy compiler (Opus 4.8 NL → Python)
- Backtester (historical CLOB replay)
- Rubric grader (pass/fail spec)
- Opus 4.8 swarm tournament
- Sandbox deployer (Docker, budget-capped)
- Demo web UI

**Used as substrate** (prior open-source work):
- [pave-capital](https://github.com/priyank1119/pave-capital) — trading
  engine, Polymarket integration, auto-redeemer, risk controls

See [CONTRIBUTIONS.md](CONTRIBUTIONS.md) for a file-by-file map.

---

## Quickstart

```bash
git clone https://github.com/priyank1119/tape
cd tape

# Configure (uses Anthropic + Polymarket sandbox)
cp .env.example .env  # fill in keys

# Run the compiler + backtester
pip install -e .
python -m tape.cli compile "Buy NO on geopolitical markets at >$0.90"

# Or run the demo UI
python -m web.server
# Visit http://localhost:8000
```

---

## Why Tape uses Opus 4.8 specifically

Three layers, not just chat-completion:

1. **Compile**: Opus 4.8's 1M-token context window absorbs the user's brief,
   the strategy template, AND 90 days of market data, then emits Python.
2. **Critique**: Opus reads its own output + backtest result, proposes a v2.
3. **Tournament**: 5 parallel Opus instances each refine differently;
   a 6th Opus reads all 5 verdicts and picks the winner.

See [`tape/prompts/`](tape/prompts/) for the prompts that drive each layer.

---

## License

[AGPL-3.0](LICENSE). Fork, learn, modify. If you run Tape as a hosted service
for others, share your modifications.
