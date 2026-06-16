import pytest

from polybot.config import bot_config_from_mapping


def base_config() -> dict:
    return {
        "poll_interval_minutes": 5,
        "dry_run": True,
        "markets": [
            {
                "id": "market-1",
                "token_id": "token-1",
                "outcome": "YES",
                "min_price": 0.35,
                "max_price": 0.55,
                "buy_usd": 10,
                "enabled": True,
            }
        ],
    }


def test_loads_valid_config() -> None:
    config = bot_config_from_mapping(base_config())

    assert config.poll_interval_minutes == 5
    assert config.dry_run is True
    assert len(config.enabled_markets) == 1
    assert config.enabled_markets[0].buy_usd == 10


def test_rejects_empty_market_list() -> None:
    raw = base_config()
    raw["markets"] = []

    with pytest.raises(ValueError, match="markets"):
        bot_config_from_mapping(raw)


def test_accepts_recurring_markets_without_static_markets() -> None:
    raw = {
        "poll_interval_minutes": 0.5,
        "dry_run": True,
        "recurring_markets": [
            {
                "asset": "btc",
                "timeframe": "5m",
                "outcome": "UP",
                "min_price": 0.35,
                "max_price": 0.55,
                "buy_usd": 10,
                "avoid_buy_within_end_minutes": 1,
            }
        ],
    }

    config = bot_config_from_mapping(raw)

    assert config.enabled_markets == ()
    assert len(config.enabled_recurring_markets) == 1
    assert config.enabled_recurring_markets[0].timeframe == "5m"


def test_rejects_invalid_threshold_order() -> None:
    raw = base_config()
    raw["markets"][0]["min_price"] = 0.60

    with pytest.raises(ValueError, match="less than max_price"):
        bot_config_from_mapping(raw)


def test_filters_disabled_markets() -> None:
    raw = base_config()
    raw["markets"][0]["enabled"] = False

    config = bot_config_from_mapping(raw)

    assert config.enabled_markets == ()
