from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from polybot.config import FirstMinuteRule, MarketConfig


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
    best_ask: float | None = None
    best_bid: float | None = None


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
    """Classic threshold strategy (mid/current price)."""
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

    entry_price = snapshot.best_ask if snapshot.best_ask is not None else snapshot.current_price
    if entry_price < market.min_price:
        if not _has_enough_time_to_buy(market, snapshot):
            return _hold(market, snapshot, "too close to market end to buy")
        return Decision(
            action=Action.BUY,
            reason="ask below min and no position held",
            market_id=market.id,
            token_id=market.token_id,
            outcome=market.outcome,
            price=entry_price,
            buy_usd=market.buy_usd,
        )

    return _hold(market, snapshot, "price inside threshold band")


def decide_first_minute_entry(
    *,
    rule: FirstMinuteRule,
    market_id: str,
    token_id: str,
    best_ask: float | None,
    time_left_seconds: float | None,
    min_time_left_seconds: float,
    buy_usd: float,
) -> Decision:
    """Enter when best ask <= entry_ask and enough time remains."""
    if best_ask is None or best_ask <= 0 or best_ask >= 1:
        return Decision(
            action=Action.HOLD,
            reason="ask unavailable or outside tradable range",
            market_id=market_id,
            token_id=token_id,
            outcome=rule.outcome,
            price=best_ask or 0.0,
        )

    if time_left_seconds is None:
        return Decision(
            action=Action.HOLD,
            reason="unknown time left; refusing entry",
            market_id=market_id,
            token_id=token_id,
            outcome=rule.outcome,
            price=best_ask,
        )

    if time_left_seconds < min_time_left_seconds:
        return Decision(
            action=Action.HOLD,
            reason="too close to market end to buy",
            market_id=market_id,
            token_id=token_id,
            outcome=rule.outcome,
            price=best_ask,
        )

    if best_ask <= rule.entry_ask:
        return Decision(
            action=Action.BUY,
            reason=f"ask {best_ask:.4f} <= entry_ask {rule.entry_ask:.4f}",
            market_id=market_id,
            token_id=token_id,
            outcome=rule.outcome,
            price=best_ask,
            buy_usd=buy_usd,
        )

    return Decision(
        action=Action.HOLD,
        reason=f"ask {best_ask:.4f} above entry_ask {rule.entry_ask:.4f}",
        market_id=market_id,
        token_id=token_id,
        outcome=rule.outcome,
        price=best_ask,
    )


def decide_first_minute_exit(
    *,
    rule: FirstMinuteRule,
    market_id: str,
    token_id: str,
    holdings: float,
    has_open_sell_limit: bool,
    mark_price: float,
) -> Decision:
    if holdings <= 0:
        return Decision(
            action=Action.HOLD,
            reason="no position",
            market_id=market_id,
            token_id=token_id,
            outcome=rule.outcome,
            price=mark_price,
        )
    if has_open_sell_limit:
        return Decision(
            action=Action.HOLD,
            reason="sell limit already open",
            market_id=market_id,
            token_id=token_id,
            outcome=rule.outcome,
            price=mark_price,
        )
    return Decision(
        action=Action.PLACE_SELL_LIMIT,
        reason=f"position held; place resting sell limit at {rule.exit_bid:.2f}",
        market_id=market_id,
        token_id=token_id,
        outcome=rule.outcome,
        price=mark_price,
        sell_shares=holdings,
        limit_price=rule.exit_bid,
    )


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
