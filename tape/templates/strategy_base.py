"""
tape/templates/strategy_base.py
───────────────────────────────
The interface every compiled strategy MUST implement.

The strategy compiler (`tape/compiler.py`) feeds this file to Opus 4.8 as
the contract. Whatever Opus produces must be a drop-in replacement for the
TemplateStrategy class — same method signatures, same return types.

Why a class instead of a single function?
  - Strategies need state across cycles (cooldowns, last decisions, running
    P&L). A class makes that natural without globals.
  - The backtester and the live deployer both instantiate the class once
    and call .decide() per cycle — identical interface for both.
  - Compiled strategies can override any of the optional hooks below to
    customize behavior (sizing, filters, cooldown logic) without rewriting
    the whole class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ════════════════════════════════════════════════════════════════════════════
#  Data types passed in / out of the strategy
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Market:
    """A snapshot of one Polymarket market at decision time."""

    key: str                 # internal id (slug or condition_id prefix)
    title: str               # human-readable question
    our_side: str            # "YES" or "NO" — the side this market is configured for
    current_price: float     # CLOB midpoint of our side, 0..1
    liquidity_usd: float     # market depth in USD on our side
    resolve_date: str        # ISO date string, e.g. "2026-06-30"
    days_to_resolve: int     # >0 = future, 0 = today, <0 = past

    # Optional context the strategy may use
    news_sentiment: Optional[str] = None    # "bullish" | "bearish" | "neutral"
    news_strength: Optional[float] = None   # 0..1
    whale_aligned: Optional[bool] = None    # True if a tracked whale holds same side


@dataclass
class Position:
    """The strategy's current position in one market. None means we don't hold."""

    key: str
    entry_price: float       # avg cost basis, 0..1
    qty: int                 # tokens held
    entry_date: str          # ISO date
    unrealized_pnl: float    # current_value - cost_basis


@dataclass
class Decision:
    """What the strategy decides to do for one market this cycle."""

    action: str              # "BUY" | "SELL" | "HOLD"
    qty: int = 0             # tokens to trade (BUY/SELL)
    confidence: float = 0.0  # 0..1; weights downstream risk sizing
    reason: str = ""         # one-line explanation (logged for audit)

    @classmethod
    def hold(cls, reason: str = "") -> "Decision":
        return cls(action="HOLD", qty=0, confidence=0.0, reason=reason)


@dataclass
class StrategyMeta:
    """Self-describing metadata for the strategy.

    Used by the rubric grader, the dashboard, and the audit log.
    """

    name: str                # short identifier
    description: str         # one-line human description
    risk_tolerance: str      # "conservative" | "moderate" | "aggressive"
    max_position_usd: float  # hard cap per market (enforced by risk_controls)
    target_horizon_days: int # typical hold time; affects market filtering


# ════════════════════════════════════════════════════════════════════════════
#  Base class
# ════════════════════════════════════════════════════════════════════════════

class TemplateStrategy:
    """The interface Opus 4.8 fills in.

    The COMPILED strategy must override at minimum:
      - META (class attr) — the StrategyMeta describing this strategy
      - decide(...)       — the per-market decision function

    The COMPILED strategy MAY override:
      - filter_markets(...)  — pre-decision filter (default: trade everything)
      - size_position(...)   — convert confidence into a qty (default: linear)
      - on_cycle_start(...)  — bookkeeping at the start of each cycle
      - on_cycle_end(...)    — bookkeeping at the end of each cycle
    """

    # Strategy compiler fills this in. The grader reads it.
    META: StrategyMeta = StrategyMeta(
        name="template",
        description="Override me in your compiled strategy.",
        risk_tolerance="moderate",
        max_position_usd=10.0,
        target_horizon_days=14,
    )

    # ── Required hook — the compiler always overrides this ──────────────────

    def decide(self, market: Market, position: Optional[Position]) -> Decision:
        """Return BUY / SELL / HOLD for ONE market this cycle.

        The contract:
          - Look at `market` (current price, news, days to resolve)
          - Look at `position` (None if not held)
          - Return a Decision with action + qty + confidence + reason
          - DO NOT make any external calls (no requests, no chain RPC)
          - DO NOT use the current date/time directly — use market.days_to_resolve
            (avoids backtest lookahead bias)
        """
        raise NotImplementedError("compiled strategy must override decide()")

    # ── Optional hooks — sensible defaults ──────────────────────────────────

    def filter_markets(self, markets: list[Market]) -> list[Market]:
        """Pre-filter markets before calling decide() on each.

        Default: trade everything that:
          - is in the future (days_to_resolve > 0)
          - has reasonable liquidity (>= $500)
        """
        return [
            m for m in markets
            if m.days_to_resolve > 0 and m.liquidity_usd >= 500
        ]

    def size_position(self, confidence: float, market: Market) -> int:
        """Convert confidence (0..1) to a token quantity.

        Default: linear in confidence, capped at max_position_usd.
        """
        max_tokens = int(self.META.max_position_usd / max(market.current_price, 0.01))
        return max(1, int(max_tokens * confidence))

    def on_cycle_start(self, cycle_id: str, markets: list[Market]) -> None:
        """Bookkeeping hook called once per cycle. Default: no-op."""

    def on_cycle_end(self, cycle_id: str, decisions: list[Decision]) -> None:
        """Bookkeeping hook called after all decisions for this cycle. Default: no-op."""


# ════════════════════════════════════════════════════════════════════════════
#  Example of a minimal compiled strategy (for the compiler's prompt)
# ════════════════════════════════════════════════════════════════════════════

class _ExampleNearCertaintyBond(TemplateStrategy):
    """Reference example — buys near-certain NO bets and holds to resolution.

    Used by the compiler as a few-shot example. Not actually loaded by the
    runtime; lives here as the canonical "this is what a good output looks like."
    """

    META = StrategyMeta(
        name="near_certainty_bond",
        description="Buy NO when price >= 0.92 and <= 14 days to resolve.",
        risk_tolerance="conservative",
        max_position_usd=10.0,
        target_horizon_days=14,
    )

    def decide(self, market: Market, position: Optional[Position]) -> Decision:
        # Entry: market priced as near-certain NO, with short resolution horizon
        if position is None:
            if (
                market.our_side == "NO"
                and market.current_price >= 0.92
                and 0 < market.days_to_resolve <= 14
            ):
                return Decision(
                    action="BUY",
                    confidence=0.85,
                    reason=f"Bond entry: NO at {market.current_price:.3f}, "
                           f"resolves in {market.days_to_resolve}d",
                )
            return Decision.hold("No position; entry criteria not met")

        # Exit: take profit if price moved 5% in our favor
        unrealized_pct = (market.current_price - position.entry_price) / position.entry_price
        if unrealized_pct >= 0.05:
            return Decision(
                action="SELL", qty=position.qty, confidence=0.9,
                reason=f"Take profit: +{unrealized_pct:.1%}",
            )

        # Stop loss: cut if price moved 10% against us
        if unrealized_pct <= -0.10:
            return Decision(
                action="SELL", qty=position.qty, confidence=0.95,
                reason=f"Stop loss: {unrealized_pct:.1%}",
            )

        return Decision.hold(f"Hold to resolution: P&L {unrealized_pct:+.1%}")
