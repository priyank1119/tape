"""Compiled strategy: yolo_buy_everything — reference loser.

Buys at any price, any side, any market. Used to verify the rubric
correctly *fails* nonsense strategies.
"""

from tape.templates.strategy_base import (
    TemplateStrategy, StrategyMeta, Market, Position, Decision,
)


class Strategy(TemplateStrategy):
    META = StrategyMeta(
        name="yolo_buy_everything",
        description="Buys every market every day. Designed to lose.",
        risk_tolerance="aggressive",
        max_position_usd=25.0,
        target_horizon_days=30,
    )

    def decide(self, market: Market, position):
        if position is None:
            return Decision(
                action="BUY", confidence=0.95,
                reason="YOLO",
            )
        return Decision.hold("Already in")
