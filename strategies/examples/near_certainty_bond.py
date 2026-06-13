"""Compiled strategy: near_certainty_bond — reference winner.

Buys NO at >=0.90 with <=21 days to resolve. Holds to resolution.
This is what a "good" Tape compilation should look like.
"""

from tape.templates.strategy_base import (
    TemplateStrategy, StrategyMeta, Market, Position, Decision,
)


class Strategy(TemplateStrategy):
    META = StrategyMeta(
        name="near_certainty_bond",
        description="Buy NO at >=0.90, <=21 days to resolve. Hold to resolution.",
        risk_tolerance="conservative",
        max_position_usd=10.0,
        target_horizon_days=21,
    )

    def decide(self, market: Market, position):
        if position is None:
            if (
                market.our_side == "NO"
                and market.current_price >= 0.90
                and 0 < market.days_to_resolve <= 21
            ):
                return Decision(
                    action="BUY",
                    confidence=0.85,
                    reason=(
                        f"Entry: NO @ {market.current_price:.3f} >= 0.90, "
                        f"resolves in {market.days_to_resolve}d"
                    ),
                )
            return Decision.hold("No entry signal")

        return Decision.hold("Holding to resolution")
