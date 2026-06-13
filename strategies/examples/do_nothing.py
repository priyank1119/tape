"""Compiled strategy: do_nothing — verifies edge cases.

Returns HOLD on every market. Useful for testing the backtester handles
zero-trade strategies cleanly (no division by zero, etc.).
"""

from tape.templates.strategy_base import (
    TemplateStrategy, StrategyMeta, Market, Position, Decision,
)


class Strategy(TemplateStrategy):
    META = StrategyMeta(
        name="do_nothing",
        description="Never trades. Used for backtest edge case coverage.",
        risk_tolerance="conservative",
        max_position_usd=5.0,
        target_horizon_days=30,
    )

    def decide(self, market: Market, position):
        return Decision.hold("Never trading")
