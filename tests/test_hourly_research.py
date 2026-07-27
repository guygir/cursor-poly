from __future__ import annotations

from datetime import datetime, timezone

from polybot.binance import Kline
from polybot.hourly_research import (
    HourlyObservation,
    infer_winner_from_prices,
    score_hourly_observations,
    simulate_entry_pnl,
)
from polybot.kronos_predictor import LastCandleMomentumPredictor
from polybot.polymarket_client import _matches_updown_event


class FakeBinance:
    def get_klines(self, symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 512):
        base = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
        return [
            Kline(
                open_time=base,
                close_time=base.replace(hour=11),
                open=100.0,
                high=110.0,
                low=99.0,
                close=108.0,
                volume=1.0,
                quote_volume=108.0,
            ),
            Kline(
                open_time=base.replace(hour=11),
                close_time=base.replace(hour=12),
                open=108.0,
                high=109.0,
                low=107.0,
                close=107.5,
                volume=1.0,
                quote_volume=107.5,
            ),
        ]


def test_heuristic_predictor_uses_last_closed_candle() -> None:
    prediction = LastCandleMomentumPredictor(FakeBinance()).predict_next_hour()
    assert prediction.side == "UP"
    assert prediction.source == "binance_last_closed_candle"
    assert 0.5 <= prediction.confidence <= 0.95


def test_matches_hourly_bitcoin_event() -> None:
    event = {
        "slug": "bitcoin-up-or-down-july-27-2pm-et",
        "title": "Bitcoin Up or Down - July 27, 2PM ET",
        "seriesSlug": "btc-up-or-down-hourly",
    }
    assert _matches_updown_event(event, "btc", "1h") is True
    assert _matches_updown_event(event, "btc", "5m") is False


def test_infer_winner_and_simulate_pnl() -> None:
    assert infer_winner_from_prices(0.97, 0.03) == "UP"
    assert infer_winner_from_prices(0.04, 0.96) == "DOWN"
    assert infer_winner_from_prices(0.55, 0.45) is None

    win = HourlyObservation(
        observed_at=datetime.now(timezone.utc),
        asset="btc",
        timeframe="1h",
        slug="btc-1h-a",
        market_id="m1",
        start_time=None,
        end_time=None,
        prediction_side="UP",
        prediction_confidence=0.7,
        prediction_source="manual",
        max_entry_price=0.52,
        up_bid=0.49,
        up_ask=0.50,
        down_bid=0.49,
        down_ask=0.51,
        selected_side="UP",
        selected_ask=0.50,
        entry_available=True,
        latest_binance_close=100.0,
    )
    assert simulate_entry_pnl(win, "UP") == 1.0
    assert simulate_entry_pnl(win, "DOWN") == -1.0


def test_score_hourly_observations_one_decision_per_slug() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        HourlyObservation(
            observed_at=now,
            asset="btc",
            timeframe="1h",
            slug="window-1",
            market_id="m1",
            start_time=None,
            end_time=None,
            prediction_side="UP",
            prediction_confidence=0.6,
            prediction_source="heuristic",
            max_entry_price=0.52,
            up_bid=0.48,
            up_ask=0.50,
            down_bid=0.48,
            down_ask=0.52,
            selected_side="UP",
            selected_ask=0.50,
            entry_available=True,
            latest_binance_close=1.0,
            resolved_winner="UP",
            simulated_pnl=1.0,
        ),
        HourlyObservation(
            observed_at=now,
            asset="btc",
            timeframe="1h",
            slug="window-1",
            market_id="m1",
            start_time=None,
            end_time=None,
            prediction_side="UP",
            prediction_confidence=0.6,
            prediction_source="heuristic",
            max_entry_price=0.52,
            up_bid=0.60,
            up_ask=0.62,
            down_bid=0.38,
            down_ask=0.40,
            selected_side="UP",
            selected_ask=0.62,
            entry_available=False,
            latest_binance_close=1.0,
            resolved_winner="UP",
            simulated_pnl=None,
        ),
        HourlyObservation(
            observed_at=now,
            asset="btc",
            timeframe="1h",
            slug="window-2",
            market_id="m2",
            start_time=None,
            end_time=None,
            prediction_side="DOWN",
            prediction_confidence=0.6,
            prediction_source="heuristic",
            max_entry_price=0.52,
            up_bid=0.55,
            up_ask=0.57,
            down_bid=0.43,
            down_ask=0.45,
            selected_side="DOWN",
            selected_ask=0.45,
            entry_available=True,
            latest_binance_close=1.0,
            resolved_winner="UP",
            simulated_pnl=-1.0,
        ),
    ]
    summary = score_hourly_observations(rows)
    assert summary.windows == 2
    assert summary.entries == 2
    assert summary.wins == 1
    assert summary.losses == 1
    assert summary.total_simulated_pnl == 0.0
