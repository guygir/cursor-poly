from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests


BINANCE_API_URL = "https://api.binance.com"


@dataclass(frozen=True)
class Kline:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


class BinanceClient:
    def __init__(self, base_url: str = BINANCE_API_URL, timeout_seconds: float = 15.0) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def get_klines(self, symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 512) -> list[Kline]:
        response = requests.get(
            f"{self._base_url.rstrip('/')}/api/v3/klines",
            params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Binance klines returned an unexpected response")
        return [_kline_from_row(row) for row in rows]


def _kline_from_row(row: list[Any]) -> Kline:
    return Kline(
        open_time=_from_ms(int(row[0])),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=_from_ms(int(row[6])),
        quote_volume=float(row[7]),
    )


def _from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
