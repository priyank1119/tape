# Tape — 60-Second Demo Video Script

Record your screen at **https://interesting-worship-usb-petition.trycloudflare.com**
(fallback http://134.209.201.187:8500). Keep the whole flow under 60s — practice
once so the Opus compile (~15-20s) doesn't feel slow on camera.

---

## Shot list (target: 55s)

**[0:00–0:08] Hook — say while the page loads**
> "This is Tape. It turns a plain-English trading idea into a deployed
>  Polymarket bot in about a minute. Watch — I'll type a strategy."

**[0:08–0:15] Type / click the strategy**
- Click the **"Geo bond"** example chip (reliably passes), OR type:
  *"Buy NO on geopolitical conflict markets above 0.90 that resolve within 21 days."*
- Click **Compile & Backtest**.
> "Claude Opus 4.8 is now writing the actual Python strategy."

**[0:15–0:30] Narrate the live pipeline as stages light up**
> "Stage one — Opus compiles my sentence into a real strategy class.
>  Stage two — it backtests against 90 days of markets: positive return,
>  Sharpe above 6, dozens of trades. Stage three — it grades against a
>  rubric file, and every clause goes green."

**[0:30–0:38] The verdict**
- Point at the green **✅ DEPLOYABLE** banner.
> "It only deploys if it passes the rubric. This one passed."

**[0:38–0:50] The swarm — the showpiece**
- Click **"Refine with Opus 4.8 swarm (5 agents + judge)"**.
> "Now five Opus agents each refine the strategy a different way — in
>  parallel — and a sixth Opus judges all the backtests and picks the winner."
- Let the variant table fill in; point at the highlighted winner + judge rationale.

**[0:50–0:58] Close**
> "Everything you saw was built today, runs live, and the rubric Claude
>  grades against is the same one another team could rerun tomorrow. That's Tape."

---

## Tips
- **Pre-warm it**: run the exact flow once right before recording so any cold
  state is warm and you've confirmed the tunnel URL is alive.
- If the swarm shows 1-2 "compile failed" variants (transient API timeouts),
  that's fine — it demonstrates fault tolerance; the judge still picks a winner.
- Toggle **"skip Opus critique (faster)"** ON for the main run to keep it snappy;
  optionally show it OFF once to reveal the Opus critique text.
- Record at 1280×720+; zoom the browser to ~110% so text is legible.
- Upload to YouTube as **unlisted or public**, paste the link in the form.
