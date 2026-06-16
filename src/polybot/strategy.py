from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from polybot.config import MarketConfig


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"
    PLACE_SELL_LIMIT = "place_sell_limit"
    HOLD = "hold"


@dataclass(frozen=True)
class MarketSnapshot:
    market_id: str
    token_id: str
    outcome: str
    current_price: float
    holdings: float
    time_to_end_minutes: float | None = None
    has_open_sell_limit: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    market_id: str
    token_id: str
    outcome: str
    price: float
    buy_usd: float | None = None
    sell_shares: float | None = None
    limit_price: float | None = None

    @property
    def should_trade(self) -> bool:
        return self.action in {Action.BUY, Action.SELL, Action.PLACE_SELL_LIMIT}


def decide(market: MarketConfig, snapshot: MarketSnapshot) -> Decision:
    if snapshot.current_price <= 0 or snapshot.current_price >= 1:
        return _hold(market, snapshot, "price outside tradable range")

    if snapshot.holdings > 0:
        if snapshot.has_open_sell_limit:
            return _hold(market, snapshot, "sell limit already open")
        return Decision(
            action=Action.PLACE_SELL_LIMIT,
            reason="position held; place resting sell limit at max price",
            market_id=market.id,
            token_id=market.token_id,
            outcome=market.outcome,
            price=snapshot.current_price,
            sell_shares=snapshot.holdings,
            limit_price=market.max_price,
        )

    if snapshot.current_price < market.min_price:
        if not _has_enough_time_to_buy(market, snapshot):
            return _hold(market, snapshot, "too close to market end to buy")
        return Decision(
            action=Action.BUY,
            reason="price below min and no position held",
            market_id=market.id,
            token_id=market.token_id,
            outcome=market.outcome,
            price=snapshot.current_price,
            buy_usd=market.buy_usd,
        )

    return _hold(market, snapshot, "price inside threshold band")


def _has_enough_time_to_buy(market: MarketConfig, snapshot: MarketSnapshot) -> bool:
    if market.avoid_buy_within_end_minutes is None:
        return True
    if snapshot.time_to_end_minutes is None:
        return False
    return snapshot.time_to_end_minutes > market.avoid_buy_within_end_minutes


def _hold(market: MarketConfig, snapshot: MarketSnapshot, reason: str) -> Decision:
    return Decision(
        action=Action.HOLD,
        reason=reason,
        market_id=market.id,
        token_id=market.token_id,
        outcome=market.outcome,
        price=snapshot.current_price,
    )
