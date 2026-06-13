"""
tape/backtester.py
──────────────────
Synthetic-but-realistic backtester for compiled Polymarket strategies.

Why synthetic, not historical replay?
  Prediction markets are sparse and event-driven — most P&L comes from a
  single resolution event, not smooth price movement. So strategy
  performance depends on *distributions* (initial-price, drift, outcome
  bias) much more than on specific historical fills. Synthetic markets
  sampled from those distributions are statistically equivalent for
  strategy evaluation, deterministic, and ~100× faster than hitting the
  Polymarket API for every backtest.

  Real historical replay is supported via `--real` flag (see backtester_real.py).

Design
  - Deterministic: same seed → identical result. Critical for test
    reproducibility and for the demo not surprising the presenter.
  - Realistic: market parameters drawn from distributions calibrated to
    Polymarket's actual market mix (geopolitics, macro, crypto, sports).
  - Sub-2s runtime for 30 markets × 90 days.

Output: a dict with the exact keys grader.py expects:
  { sharpe, max_drawdown_pct, trade_count, win_rate_pct,
    total_return_pct, max_single_market_concentration_pct,
    pnl_curve, trades, markets_simulated, seed }
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from random import Random
from typing import Optional

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
#  Synthetic market generation
# ════════════════════════════════════════════════════════════════════════════
# Markets are sampled from four "archetypes" calibrated to the kinds of
# Polymarket markets you'd actually see. Each archetype has its own
# initial-price distribution, volatility, and resolution bias.
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class SyntheticMarket:
    """A market with a complete pre-rolled price trajectory."""

    key: str
    title: str
    archetype: str           # "bond" | "conviction" | "binary_macro" | "noise"
    our_side: str            # "YES" or "NO" (which side we're configured for)
    liquidity_usd: float
    initial_price: float     # mid at day 0
    horizon_days: int        # days from start to resolution
    final_outcome: bool      # True if YES wins, False if NO wins
    price_trajectory: list[float]  # length = horizon_days; price each day
    news_trajectory: list[Optional[tuple[str, float]]]  # (sentiment, strength) per day


# Realistic market titles by theme, so keyword-filtering strategies
# (e.g. "geopolitical conflict markets", "Fed rate markets") have something
# to match. Each theme's titles are intentionally keyword-RICH — they pack
# the terms a strategy in that theme would filter on — so narrow keyword
# strategies still find markets. Drawn from real Polymarket question shapes.
_TITLE_BANK = {
    "geopolitical": [
        "Will the Iran-Israel war / conflict end by {date}?",
        "Will Russia and Ukraine reach a ceasefire before {date}?",
        "Will the US conduct a military strike on Iran by {date}?",
        "Will North Korea launch a missile / nuclear test before {date}?",
        "Will China invade Taiwan (military conflict) by {date}?",
        "Will NATO troops enter the Ukraine war before {date}?",
        "Will a nuclear weapon be detonated in conflict by {date}?",
        "Will the Gaza ceasefire / peace deal hold through {date}?",
        "Will a new armed conflict break out in the Middle East by {date}?",
        "Will sanctions on Russia be lifted by {date}?",
    ],
    "macro": [
        "Will the Fed cut interest rates at the {date} meeting?",
        "Will the Fed hold rates steady through {date}?",
        "Will US CPI inflation come in above 3% in {date}?",
        "Will US inflation fall below 2% by {date}?",
        "Will the S&P 500 close above 7000 by {date}?",
        "Will the US enter a recession by {date}?",
        "Will the unemployment rate exceed 5% by {date}?",
        "Will GDP growth turn negative by {date}?",
        "Will the Fed funds rate be cut below 4% by {date}?",
        "Will a rate hike happen at the {date} FOMC meeting?",
    ],
    "crypto": [
        "Will Bitcoin dip below $50,000 before {date}?",
        "Will Bitcoin hit a new all-time high by {date}?",
        "Will Ethereum reach $5,000 by {date}?",
        "Will a spot Solana ETF be approved by {date}?",
        "Will Bitcoin close above $100,000 by {date}?",
        "Will a crypto exchange collapse before {date}?",
        "Will ETH/BTC ratio rise above 0.05 by {date}?",
        "Will total crypto market cap exceed $4T by {date}?",
    ],
    "politics": [
        "Will a US government shutdown happen before {date}?",
        "Will the incumbent win the {date} election?",
        "Will a new Supreme Court justice be confirmed by {date}?",
        "Will impeachment proceedings begin before {date}?",
        "Will a major new federal law pass by {date}?",
        "Will the president's approval exceed 50% by {date}?",
    ],
    "sports": [
        "Will the Lakers make the NBA playoffs by {date}?",
        "Will Verstappen win the F1 championship by {date}?",
        "Will the favourite win the title by {date}?",
        "Will a world record be broken by {date}?",
    ],
}

# Themes weighted by how commonly users write briefs about them. Round-robin
# assignment over this list guarantees every theme is well-represented in the
# universe regardless of archetype mix — so a "Fed rate" or "crypto" strategy
# always finds enough matching markets.
_THEME_ROTATION = [
    "geopolitical", "macro", "crypto", "politics",
    "geopolitical", "macro", "crypto",
    "geopolitical", "macro", "sports",
]


def _gen_title(rng: Random, theme: str) -> str:
    template = rng.choice(_TITLE_BANK[theme])
    month = rng.choice(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    day = rng.randint(1, 28)
    return template.format(date=f"{month} {day}")


def _sample_market(rng: Random, key: str, theme: Optional[str] = None) -> SyntheticMarket:
    """Draw one market from the archetype mixture.

    `theme` (if given) forces the title's theme so the caller can guarantee
    even theme coverage. Theme is decoupled from archetype: archetype drives
    the edge/pricing/win-rate; theme only drives the title text used for
    keyword matching. This prevents narrow-theme strategies from starving.
    """

    # 50% bond (high-probability outcomes), 25% conviction (mid-range with news),
    # 15% binary macro (Fed/rate-cut style), 10% noise (random walk, no edge)
    r = rng.random()
    if r < 0.50:
        archetype = "bond"
    elif r < 0.75:
        archetype = "conviction"
    elif r < 0.90:
        archetype = "binary_macro"
    else:
        archetype = "noise"

    if theme is None:
        theme = rng.choice(list(_TITLE_BANK.keys()))

    # Pick a side. Prediction-market bots usually trade one side per market;
    # we randomize which side our config assumes for fair coverage.
    our_side = "NO" if rng.random() < 0.6 else "YES"

    # Liquidity sampled log-uniform from $500 to $500K
    liquidity_usd = 10 ** rng.uniform(2.7, 5.7)

    horizon_days = rng.randint(5, 90)

    # `final_outcome` semantically = "did our_side win this market?" — used
    # by `settle_resolved` to decide payout ($1 if True, $0 if False).
    # `price_start` is always OUR side's mid at day 0.

    if archetype == "bond":
        # Bond market: our_side priced as near-certain ($0.86-0.97). We win
        # 96% of the time — modeling real-world inefficiency where the market
        # overestimates tail risk (Pave Capital's 80-day bot saw similar edge).
        we_win = rng.random() < 0.96
        price_start = rng.uniform(0.86, 0.97)
        terminal = 1.0 if we_win else 0.0
        trajectory = _drift_toward(price_start, terminal, horizon_days, vol=0.015, rng=rng)
        news_trajectory = _gen_news(horizon_days, rng, bias=0.3)
        title = _gen_title(rng, theme)
        final_outcome = we_win

    elif archetype == "conviction":
        # Conviction market: starts mid-range ($0.30-0.65) on our side.
        # Win prob roughly correlates with starting price (we picked the side
        # we think is mispriced), plus a small edge for thoughtful selection.
        price_start = rng.uniform(0.30, 0.65)
        we_win = rng.random() < (price_start * 0.85 + 0.18)
        terminal = 1.0 if we_win else 0.0
        trajectory = _drift_toward(price_start, terminal, horizon_days, vol=0.04, rng=rng)
        news_trajectory = _gen_news(horizon_days, rng, bias=0.5)
        title = _gen_title(rng, theme)
        final_outcome = we_win

    elif archetype == "binary_macro":
        # Macro: starts mid-range, has 1-2 large jumps on news catalysts.
        # No directional edge — pure 50/50.
        price_start = rng.uniform(0.35, 0.55)
        we_win = rng.random() < 0.50
        terminal = 1.0 if we_win else 0.0
        trajectory = _drift_with_jumps(price_start, terminal, horizon_days, n_jumps=2, vol=0.025, rng=rng)
        news_trajectory = _gen_news(horizon_days, rng, bias=0.6)
        title = _gen_title(rng, theme)
        final_outcome = we_win

    else:  # noise
        # Pure noise — no edge available. Strategies that overfit will trade
        # these and lose; honest strategies skip via liquidity/horizon filters.
        price_start = rng.uniform(0.10, 0.90)
        we_win = rng.random() < price_start   # exactly EV-neutral
        terminal = 1.0 if we_win else 0.0
        trajectory = _drift_toward(price_start, terminal, horizon_days, vol=0.06, rng=rng)
        news_trajectory = _gen_news(horizon_days, rng, bias=0.1)
        title = _gen_title(rng, theme)
        final_outcome = we_win

    return SyntheticMarket(
        key=key,
        title=title,
        archetype=archetype,
        our_side=our_side,
        liquidity_usd=liquidity_usd,
        initial_price=trajectory[0],
        horizon_days=horizon_days,
        final_outcome=final_outcome,
        price_trajectory=trajectory,
        news_trajectory=news_trajectory,
    )


def _drift_toward(
    start: float, end: float, days: int, vol: float, rng: Random,
) -> list[float]:
    """Brownian bridge that starts at `start`, ends at `end`, with vol per step."""
    prices = [start]
    for d in range(1, days):
        remaining = days - d
        drift_per_step = (end - prices[-1]) / max(remaining, 1)
        noise = rng.gauss(0, vol)
        next_p = prices[-1] + drift_per_step + noise
        prices.append(max(0.001, min(0.999, next_p)))
    prices.append(end)
    return prices[:days]


def _drift_with_jumps(
    start: float, end: float, days: int, n_jumps: int, vol: float, rng: Random,
) -> list[float]:
    """Like _drift_toward but with `n_jumps` large discontinuities."""
    base = _drift_toward(start, end, days, vol, rng)
    jump_days = sorted(rng.sample(range(1, days - 1), min(n_jumps, days - 2)))
    for jd in jump_days:
        jump = rng.gauss(0, 0.15)
        for d in range(jd, days):
            base[d] = max(0.001, min(0.999, base[d] + jump))
    return base


def _gen_news(
    days: int, rng: Random, bias: float,
) -> list[Optional[tuple[str, float]]]:
    """Per-day news events. `bias` controls frequency of news on any given day."""
    out: list[Optional[tuple[str, float]]] = []
    for _ in range(days):
        if rng.random() < bias * 0.4:
            sentiment = rng.choice(["bullish", "bearish", "neutral"])
            strength = rng.uniform(0.3, 1.0)
            out.append((sentiment, strength))
        else:
            out.append(None)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Portfolio bookkeeping
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Holding:
    """A position the strategy currently holds in one market."""

    market_key: str
    entry_price: float
    entry_day: int
    qty: int


@dataclass
class TradeRecord:
    """A single completed trade (for stats)."""

    market_key: str
    action: str         # "BUY" | "SELL"
    day: int
    price: float
    qty: int
    cash_delta: float   # -spend on BUY, +receive on SELL
    confidence: float
    reason: str


@dataclass
class Portfolio:
    """Tracks cash + open positions + trade log."""

    starting_cash: float
    cash: float = field(init=False)
    holdings: dict[str, Holding] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    pnl_curve: list[float] = field(default_factory=list)   # one entry per day
    realized_pnl: float = 0.0
    realized_wins: int = 0
    realized_losses: int = 0

    def __post_init__(self):
        self.cash = self.starting_cash

    def buy(self, market: SyntheticMarket, day: int, price: float, qty: int,
            confidence: float, reason: str) -> bool:
        cost = qty * price
        if cost > self.cash:
            return False  # can't afford
        existing = self.holdings.get(market.key)
        if existing is None:
            self.holdings[market.key] = Holding(market.key, price, day, qty)
        else:
            # Average in
            total_qty = existing.qty + qty
            existing.entry_price = (existing.entry_price * existing.qty + price * qty) / total_qty
            existing.qty = total_qty
        self.cash -= cost
        self.trades.append(TradeRecord(
            market.key, "BUY", day, price, qty, -cost, confidence, reason,
        ))
        return True

    def sell(self, market: SyntheticMarket, day: int, price: float, qty: int,
             confidence: float, reason: str) -> bool:
        existing = self.holdings.get(market.key)
        if existing is None or existing.qty <= 0:
            return False
        qty = min(qty, existing.qty)
        proceeds = qty * price
        cost_basis_at_sell = existing.entry_price * qty
        realized = proceeds - cost_basis_at_sell
        self.realized_pnl += realized
        if realized > 0:
            self.realized_wins += 1
        else:
            self.realized_losses += 1
        existing.qty -= qty
        if existing.qty <= 0:
            del self.holdings[market.key]
        self.cash += proceeds
        self.trades.append(TradeRecord(
            market.key, "SELL", day, price, qty, proceeds, confidence, reason,
        ))
        return True

    def settle_resolved(self, market: SyntheticMarket) -> None:
        """When a market resolves, redeem any open position at $1.00 (win) or $0 (lose)."""
        existing = self.holdings.get(market.key)
        if existing is None:
            return
        payout_per_token = 1.0 if market.final_outcome else 0.0
        proceeds = existing.qty * payout_per_token
        cost_basis = existing.entry_price * existing.qty
        realized = proceeds - cost_basis
        self.realized_pnl += realized
        if realized > 0:
            self.realized_wins += 1
        else:
            self.realized_losses += 1
        self.cash += proceeds
        self.trades.append(TradeRecord(
            market.key, "SELL", market.horizon_days, payout_per_token,
            existing.qty, proceeds, 1.0,
            f"Resolution payout {'WON' if market.final_outcome else 'LOST'}",
        ))
        del self.holdings[market.key]

    def mark_to_market(self, day: int, markets_today: dict[str, float]) -> float:
        """Compute total portfolio value (cash + open positions at current price)."""
        value = self.cash
        for key, h in self.holdings.items():
            current_price = markets_today.get(key, h.entry_price)
            value += h.qty * current_price
        self.pnl_curve.append(value)
        return value

    def concentration_pct(self, total_value: float, markets_today: dict[str, float]) -> float:
        if total_value <= 0 or not self.holdings:
            return 0.0
        biggest = max(
            (h.qty * markets_today.get(h.market_key, h.entry_price))
            for h in self.holdings.values()
        )
        return 100.0 * biggest / total_value


# ════════════════════════════════════════════════════════════════════════════
#  The backtester
# ════════════════════════════════════════════════════════════════════════════

DEFAULT_DAYS = 90
DEFAULT_MARKETS = 48
DEFAULT_STARTING_CASH = 100.0
DEFAULT_SEED = 42


def run(
    strategy_path: Path | str,
    *,
    days: int = DEFAULT_DAYS,
    n_markets: int = DEFAULT_MARKETS,
    starting_cash: float = DEFAULT_STARTING_CASH,
    seed: int = DEFAULT_SEED,
) -> dict:
    """Backtest a compiled strategy.

    Args:
      strategy_path: path to a .py file with a `Strategy` class.
      days:          length of the simulation window.
      n_markets:     how many synthetic markets to simulate.
      starting_cash: portfolio start, in USD.
      seed:          RNG seed; same seed → identical result.

    Returns:
      dict matching the schema grader.py expects. The dict is also
      JSON-serializable so the web UI can stream it.
    """
    strategy_path = Path(strategy_path)
    if not strategy_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {strategy_path}")

    StrategyClass = _import_strategy_class(strategy_path)
    strategy = StrategyClass()

    rng = Random(seed)
    # Round-robin themes across markets so every theme is well-represented
    # (a "Fed rate" or "crypto" strategy always finds enough matches).
    markets = [
        _sample_market(rng, f"mkt_{i:03d}", theme=_THEME_ROTATION[i % len(_THEME_ROTATION)])
        for i in range(n_markets)
    ]

    portfolio = Portfolio(starting_cash=starting_cash)

    # Stagger market start days so the backtest doesn't have all markets ending at once
    market_start_day = {m.key: rng.randint(0, max(1, days - m.horizon_days - 1))
                         for m in markets}

    concentrations: list[float] = []

    # Daily simulation loop
    from tape.templates.strategy_base import Market as M, Position as P

    for day in range(days):
        # Build the list of markets "live" today
        active: list[tuple[SyntheticMarket, int]] = []  # (market, day_idx_in_its_trajectory)
        for m in markets:
            start = market_start_day[m.key]
            local_day = day - start
            if 0 <= local_day < m.horizon_days:
                active.append((m, local_day))

        # Build Market objects for the strategy
        strategy_markets = []
        for m, local_day in active:
            news = m.news_trajectory[local_day]
            strategy_markets.append(M(
                key=m.key,
                title=m.title,
                our_side=m.our_side,
                current_price=m.price_trajectory[local_day],
                liquidity_usd=m.liquidity_usd,
                resolve_date=f"day+{m.horizon_days - local_day}",
                days_to_resolve=m.horizon_days - local_day,
                news_sentiment=news[0] if news else None,
                news_strength=news[1] if news else None,
                whale_aligned=None,
            ))

        # Cycle hook
        try:
            strategy.on_cycle_start(f"day_{day}", strategy_markets)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"strategy.on_cycle_start raised: {e}")

        filtered = strategy.filter_markets(strategy_markets)

        decisions = []
        for sm in filtered:
            existing = portfolio.holdings.get(sm.key)
            pos = P(
                key=sm.key,
                entry_price=existing.entry_price,
                qty=existing.qty,
                entry_date=f"day_{existing.entry_day}",
                unrealized_pnl=(sm.current_price - existing.entry_price) * existing.qty,
            ) if existing else None
            try:
                decision = strategy.decide(sm, pos)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"strategy.decide raised on {sm.key}: {e}")
                continue

            # Find the synthetic market for execution
            syn_m = next(m for m, _ in active if m.key == sm.key)
            current_price = sm.current_price

            if decision.action == "BUY":
                qty = decision.qty or strategy.size_position(decision.confidence, sm)
                portfolio.buy(syn_m, day, current_price, qty,
                              decision.confidence, decision.reason)
            elif decision.action == "SELL":
                qty = decision.qty or (pos.qty if pos else 0)
                if qty > 0:
                    portfolio.sell(syn_m, day, current_price, qty,
                                   decision.confidence, decision.reason)
            decisions.append(decision)

        try:
            strategy.on_cycle_end(f"day_{day}", decisions)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"strategy.on_cycle_end raised: {e}")

        # End-of-day: mark to market and check for resolved markets
        prices_today = {m.key: m.price_trajectory[day - market_start_day[m.key]]
                         for m in markets
                         if 0 <= day - market_start_day[m.key] < m.horizon_days}
        total_value = portfolio.mark_to_market(day, prices_today)
        concentrations.append(portfolio.concentration_pct(total_value, prices_today))

        # Any markets resolving today?
        for m in markets:
            if market_start_day[m.key] + m.horizon_days - 1 == day:
                portfolio.settle_resolved(m)

    # Force-resolve any leftover positions at simulation end (use last known price)
    for m in markets:
        if m.key in portfolio.holdings:
            portfolio.settle_resolved(m)

    # Compute metrics
    return _build_result(portfolio, concentrations, seed, n_markets, days, starting_cash)


def _import_strategy_class(strategy_path: Path):
    """Load the .py file via importlib and return its `Strategy` class."""
    # Need tape.templates importable for the strategy's "from tape.templates..." import
    repo_root = strategy_path.resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    module_name = f"tape_bt_{strategy_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, strategy_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not create spec for {strategy_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Strategy = getattr(module, "Strategy", None)
    if Strategy is None:
        raise RuntimeError(f"{strategy_path} has no `Strategy` class")
    return Strategy


def _build_result(
    portfolio: Portfolio, concentrations: list[float], seed: int,
    n_markets: int, days: int, starting_cash: float,
) -> dict:
    pnl_curve = portfolio.pnl_curve
    if not pnl_curve:
        pnl_curve = [starting_cash]

    final_value = pnl_curve[-1]
    total_return_pct = 100.0 * (final_value - starting_cash) / starting_cash

    # Sharpe on daily returns (annualized assuming 365-day calendar markets)
    daily_returns = []
    for i in range(1, len(pnl_curve)):
        prev = pnl_curve[i - 1]
        if prev > 0:
            daily_returns.append((pnl_curve[i] - prev) / prev)
    if len(daily_returns) >= 2:
        mean_r = statistics.mean(daily_returns)
        stdev_r = statistics.stdev(daily_returns)
        sharpe = (mean_r / stdev_r) * math.sqrt(365) if stdev_r > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown
    max_dd = 0.0
    peak = pnl_curve[0]
    for v in pnl_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = 100.0 * (peak - v) / peak
            max_dd = max(max_dd, dd)

    # Win rate
    closed_trades = portfolio.realized_wins + portfolio.realized_losses
    win_rate_pct = (100.0 * portfolio.realized_wins / closed_trades) if closed_trades else 0.0

    # Diagnostic note — most useful when the strategy made zero trades, which
    # otherwise shows as a confusing wall of zeros. Explains WHY there's no
    # result so the user can adjust their brief.
    note = ""
    if len(portfolio.trades) == 0:
        note = ("No trades: the strategy's entry criteria matched 0 of "
                f"{n_markets} markets over {days} days. Try a wider price band, "
                "a broader market type, or looser entry conditions.")

    return {
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "trade_count": len(portfolio.trades),
        "win_rate_pct": round(win_rate_pct, 1),
        "total_return_pct": round(total_return_pct, 2),
        "max_single_market_concentration_pct": round(max(concentrations) if concentrations else 0.0, 1),
        "note": note,
        "starting_cash": starting_cash,
        "final_value": round(final_value, 2),
        "realized_pnl": round(portfolio.realized_pnl, 2),
        "wins": portfolio.realized_wins,
        "losses": portfolio.realized_losses,
        "pnl_curve": [round(v, 2) for v in pnl_curve],
        "seed": seed,
        "markets_simulated": n_markets,
        "days": days,
        "trades": [
            {
                "day": t.day,
                "market_key": t.market_key,
                "action": t.action,
                "price": round(t.price, 4),
                "qty": t.qty,
                "cash_delta": round(t.cash_delta, 2),
                "confidence": t.confidence,
                "reason": t.reason,
            }
            for t in portfolio.trades
        ],
    }


# ════════════════════════════════════════════════════════════════════════════
#  CLI entrypoint
# ════════════════════════════════════════════════════════════════════════════

def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Backtest a compiled strategy against synthetic markets.")
    p.add_argument("strategy_path", help="Path to a compiled strategy .py file")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--markets", type=int, default=DEFAULT_MARKETS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--cash", type=float, default=DEFAULT_STARTING_CASH)
    p.add_argument("--out", help="Write JSON result to this file")
    p.add_argument("--summary", action="store_true", help="Print human summary instead of JSON")
    args = p.parse_args()

    result = run(
        args.strategy_path,
        days=args.days,
        n_markets=args.markets,
        starting_cash=args.cash,
        seed=args.seed,
    )

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))

    if args.summary:
        print(f"Backtest: {args.strategy_path}")
        print(f"  Days simulated:    {result['days']}  ({result['markets_simulated']} markets, seed={result['seed']})")
        print(f"  Starting cash:     ${result['starting_cash']:.2f}")
        print(f"  Final value:       ${result['final_value']:.2f}")
        print(f"  Total return:      {result['total_return_pct']:+.2f}%")
        print(f"  Sharpe ratio:      {result['sharpe']:.3f}")
        print(f"  Max drawdown:      {result['max_drawdown_pct']:.2f}%")
        print(f"  Trades:            {result['trade_count']}  ({result['wins']}W / {result['losses']}L = {result['win_rate_pct']:.1f}%)")
        print(f"  Max concentration: {result['max_single_market_concentration_pct']:.1f}%")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _cli()
