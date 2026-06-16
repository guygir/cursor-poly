from polybot.polymarket_client import MarketMetadata, _merge_gamma_metadata, timeframe_to_seconds, updown_slug


def test_timeframe_to_seconds_for_5m() -> None:
    assert timeframe_to_seconds("5m") == 300


def test_updown_slug_uses_current_5m_window_start() -> None:
    assert updown_slug("btc", "5m", now_ts=1781606999) == "btc-updown-5m-1781606700"


def test_updown_slug_rolls_to_next_5m_window() -> None:
    assert updown_slug("btc", "5m", now_ts=1781607000) == "btc-updown-5m-1781607000"


def test_updown_slug_supports_15m() -> None:
    assert updown_slug("ETH", "15m", now_ts=1781607299) == "eth-updown-15m-1781606700"


def test_gamma_event_market_metadata_includes_orderability() -> None:
    metadata = _merge_gamma_metadata(
        MarketMetadata(
            market_id="",
            slug=None,
            question=None,
            end_time=None,
            tokens_by_outcome={},
        ),
        {
            "conditionId": "0xcondition",
            "slug": "btc-updown-5m-1781606700",
            "question": "Bitcoin Up or Down",
            "endDate": "2026-06-16T10:50:00Z",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up-token", "down-token"]',
            "closed": True,
            "acceptingOrders": False,
        },
    )

    assert metadata.market_id == "0xcondition"
    assert metadata.tokens_by_outcome == {"UP": "up-token", "DOWN": "down-token"}
    assert metadata.closed is True
    assert metadata.accepting_orders is False
