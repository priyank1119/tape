# Tape — Claude Build Day Submission

**Team:** Pave Capital · **Member:** Priyank Mishra (priyank1119)
**Repo:** https://github.com/priyank1119/tape
**Live demo:** https://interesting-worship-usb-petition.trycloudflare.com
(stable fallback: http://134.209.201.187:8500)

---

## Project Description

Tape turns a plain-English trading idea into a backtested, rubric-graded,
deployed Polymarket bot in 60 seconds.

Prediction-market traders have a discipline problem: they know what their
strategy should be, but can't code a bot, can't watch markets 24/7, and
panic-trade at the worst moments. Tape closes that gap. You type a strategy
in plain English — "Buy NO on geopolitical conflict markets above $0.90 that
resolve within 21 days" — and Claude Opus 4.8 compiles it into a real Python
strategy, backtests it against 90 days of markets, grades it against a
machine-verifiable rubric (`rubric.yaml`), and — only if it passes — deploys
it as a live paper-trading bot you can watch cycle in real time.

The standout feature is the Opus 4.8 swarm tournament: five Opus agents each
refine your strategy along a different axis (tighter risk, looser entry,
news-weighting, resolution-timing, liquidity filtering) in parallel, every
variant is backtested, and a sixth Opus agent judges all six and picks the
winner with a metrics-cited rationale.

Tape is honest — it refuses to deploy a losing strategy and tells you why
("only 2 trades — too thin to be statistically reliable"). The rubric is a
model-verifiable "done" check another team could rerun on a new problem
tomorrow.

---

## How was Opus 4.8 used?

Opus 4.8 is the engine at three distinct layers, not a single chat call:

1. **Strategy compiler** (`tape/compiler.py` + `tape/prompts/compile.md`) —
   translates a natural-language brief into a complete, importable Python
   strategy class implementing a fixed interface. Enforces hard constraints
   (no lookahead bias, no network calls, deterministic); every output is
   validated and smoke-imported.

2. **Rubric critic** (`tape/grader.py`) — reads the strategy source +
   backtest metrics + rubric and returns a structured approve/revise/reject
   verdict, catching issues thresholds can't (overfitting, gaming the
   backtest, lookahead bugs).

3. **Swarm tournament** (`tape/swarm.py` + `prompts/refine.md` +
   `prompts/tournament.md`) — a genuine multi-agent fan-out/fan-in: 5 Opus
   refiners run in parallel with distinct objectives, then a 6th Opus judge
   consumes all backtests and picks the winner. In a live run it correctly
   disqualified a higher-Sharpe variant that failed the rubric and flagged a
   variant as "meaningless on just 2 trades."

Surprising capability: Opus reliably emits runnable, side-effect-free Python
that imports and passes a real test rubric on the first try — tight enough to
put straight into an execution loop.

---

## Orchestration strategy

- **Rubric-first / test-driven**: wrote `rubric.yaml` (machine-checkable
  pass/fail) and `grader.py` before any generation code. Every component
  generates toward that spec. 88 tests; 23 prove the rubric discriminates
  good strategies from bad.
- **Verifiable "done"**: `tape run "<brief>"` chains compile → backtest →
  grade and exits 0/1. Another team can clone, `pytest`, and run it on their
  own brief tomorrow.
- **Multi-agent pipeline**: swarm = 5 parallel refiner agents (distinct
  objectives) + 1 judge agent, with a deterministic Sharpe fallback and a
  guard against hallucinated winners.
- **Prompts + rubric as repo artifacts**: `tape/prompts/{compile,refine,tournament}.md`,
  `rubric.yaml`, `CONTRIBUTIONS.md`.
- **Iteration loop**: diagnose → fix → re-verify against a trade matrix
  (e.g. found a 0-trade theme-coupling bug via a 7-pattern × 5-seed matrix,
  fixed the synthetic universe, confirmed across all seeds).

---

## Feedback on Opus 4.8

Genuinely strong points from building Tape:
- **Code generation is execution-ready.** Opus produced importable,
  side-effect-free Python strategies that passed a strict structural +
  runtime rubric on the first attempt the large majority of the time. We
  could feed its output straight into a backtest+deploy loop.
- **It reasons about its own outputs well as a judge.** The tournament judge
  and rubric critic gave coherent, metrics-cited verdicts — correctly
  disqualifying rubric-failing variants and distrusting low-sample results
  — not just rubber-stamping.
- **Instruction adherence on hard constraints** (no `datetime.now`, no
  network calls, exact numeric thresholds from the brief) was reliable.

Rough edges:
- **Occasional UI-render artifacts in long code blocks** — Opus sometimes
  appended a stray "copy"/"复制" token (copy-button leakage) into generated
  code; we strip it in `compiler.py`, but it caused a runtime `NameError`
  before we did.
- **Latency variance under parallel bursts** — firing 5 refiner calls at
  once occasionally hit request timeouts that single calls didn't; we raised
  the per-call timeout to compensate.
- A `max_tokens`-vs-reasoning interaction would be nice to tune more directly
  for "write me one tight function" vs "think hard then answer."

Net: for an agentic code-gen + self-critique loop, Opus 4.8 was reliable
enough to build the whole product around.
