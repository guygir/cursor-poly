from polybot.config import MarketConfig
from polybot.strategy import Action, MarketSnapshot, decide


def market() -> MarketConfig:
    return MarketConfig(
        id="market-1",
        token_id="token-1",
        outcome="YES",
        min_price=0.35,
        max_price=0.55,
        buy_usd=10,
    )


def snapshot(price: float, holdings: float = 0) -> MarketSnapshot:
    return MarketSnapshot(
        market_id="market-1",
        token_id="token-1",
        outcome="YES",
        current_price=price,
        holdings=holdings,
    )


def test_buys_below_min_when_no_position() -> None:
    decision = decide(market(), snapshot(price=0.30, holdings=0))

    assert decision.action == Action.BUY
    assert decision.buy_usd == 10


def test_does_not_buy_again_when_position_already_held() -> None:
    decision = decide(market(), snapshot(price=0.30, holdings=4.2))

    assert decision.action == Action.PLACE_SELL_LIMIT


def test_places_resting_sell_limit_when_position_held() -> None:
    decision = decide(market(), snapshot(price=0.60, holdings=4.2))

    assert decision.action == Action.PLACE_SELL_LIMIT
    assert decision.sell_shares == 4.2
    assert decision.limit_price == 0.55


def test_holds_when_sell_limit_already_open() -> None:
    current = snapshot(price=0.60, holdings=4.2)
    current = MarketSnapshot(
        market_id=current.market_id,
        token_id=current.token_id,
        outcome=current.outcome,
        current_price=current.current_price,
        holdings=current.holdings,
        has_open_sell_limit=True,
    )

    decision = decide(market(), current)

    assert decision.action == Action.HOLD


def test_holds_inside_band() -> None:
    decision = decide(market(), snapshot(price=0.45, holdings=0))

    assert decision.action == Action.HOLD


def test_buys_only_when_enough_time_remains() -> None:
    timed_market = MarketConfig(
        id="market-1",
        token_id="token-1",
        outcome="YES",
        min_price=0.35,
        max_price=0.55,
        buy_usd=10,
        avoid_buy_within_end_minutes=1,
    )

    enough_time = MarketSnapshot(
        market_id="market-1",
        token_id="token-1",
        outcome="YES",
        current_price=0.30,
        holdings=0,
        time_to_end_minutes=3.7,
    )
    too_late = MarketSnapshot(
        market_id="market-1",
        token_id="token-1",
        outcome="YES",
        current_price=0.30,
        holdings=0,
        time_to_end_minutes=0.8,
    )

    assert decide(timed_market, enough_time).action == Action.BUY
    assert decide(timed_market, too_late).action == Action.HOLD
