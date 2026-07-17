from polybot.config import FirstMinuteRule, bot_config_from_mapping
from polybot.strategy import Action, decide_first_minute_entry, decide_first_minute_exit


def down_rule() -> FirstMinuteRule:
    return FirstMinuteRule(outcome="DOWN", entry_ask=0.36, exit_bid=0.97, enabled=True)


def test_loads_first_minute_config() -> None:
    config = bot_config_from_mapping(
        {
            "dry_run": True,
            "mode": "first_minute",
            "first_minute": {
                "asset": "btc",
                "timeframe": "5m",
                "poll_interval_seconds": 1,
                "entry_window_seconds": 60,
                "min_time_left_seconds": 240,
                "buy_usd": 10,
                "rules": [
                    {
                        "outcome": "DOWN",
                        "entry_ask": 0.36,
                        "exit_multiplier": 2.7,
                        "enabled": True,
                    },
                    {
                        "outcome": "UP",
                        "entry_ask": 0.36,
                        "exit_multiplier": 2.7,
                        "enabled": False,
                    },
                ],
            },
        }
    )

    assert config.mode == "first_minute"
    assert config.first_minute is not None
    assert len(config.first_minute.enabled_rules) == 1
    rule = config.first_minute.enabled_rules[0]
    assert rule.outcome == "DOWN"
    assert rule.entry_ask == 0.36
    assert rule.exit_bid == 0.97


def test_enters_when_ask_at_or_below_entry() -> None:
    decision = decide_first_minute_entry(
        rule=down_rule(),
        market_id="m1",
        token_id="t1",
        best_ask=0.36,
        time_left_seconds=270,
        min_time_left_seconds=240,
        buy_usd=10,
    )
    assert decision.action == Action.BUY
    assert decision.buy_usd == 10


def test_holds_when_ask_above_entry() -> None:
    decision = decide_first_minute_entry(
        rule=down_rule(),
        market_id="m1",
        token_id="t1",
        best_ask=0.40,
        time_left_seconds=270,
        min_time_left_seconds=240,
        buy_usd=10,
    )
    assert decision.action == Action.HOLD


def test_holds_when_under_four_minutes_left() -> None:
    decision = decide_first_minute_entry(
        rule=down_rule(),
        market_id="m1",
        token_id="t1",
        best_ask=0.30,
        time_left_seconds=230,
        min_time_left_seconds=240,
        buy_usd=10,
    )
    assert decision.action == Action.HOLD


def test_places_exit_sell_limit() -> None:
    decision = decide_first_minute_exit(
        rule=down_rule(),
        market_id="m1",
        token_id="t1",
        holdings=12.5,
        has_open_sell_limit=False,
        mark_price=0.40,
    )
    assert decision.action == Action.PLACE_SELL_LIMIT
    assert decision.limit_price == 0.97
    assert decision.sell_shares == 12.5
