from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Protocol

from polybot.binance import BinanceClient, Kline

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectionPrediction:
    side: str
    confidence: float
    source: str
    detail: str


class DirectionPredictor(Protocol):
    def predict_next_hour(self, symbol: str = "BTCUSDT") -> DirectionPrediction: ...


class LastCandleMomentumPredictor:
    """Heuristic stand-in until Kronos is installed and wired.

    Uses the most recently *closed* Binance 1h candle: UP if close >= open.
    This is intentionally simple and only for dry-run research scaffolding.
    """

    def __init__(self, binance: BinanceClient | None = None) -> None:
        self._binance = binance or BinanceClient()

    def predict_next_hour(self, symbol: str = "BTCUSDT") -> DirectionPrediction:
        klines = self._binance.get_klines(symbol=symbol, interval="1h", limit=3)
        if len(klines) < 2:
            raise ValueError("need at least two Binance 1h candles for momentum prediction")
        closed = klines[-2]
        side = "UP" if closed.close >= closed.open else "DOWN"
        move = abs(closed.close - closed.open) / closed.open if closed.open else 0.0
        confidence = min(0.5 + move * 10, 0.95)
        return DirectionPrediction(
            side=side,
            confidence=confidence,
            source="binance_last_closed_candle",
            detail=(
                f"closed_open={closed.open:.2f} closed_close={closed.close:.2f} "
                f"open_time={closed.open_time.isoformat()}"
            ),
        )


class KronosPredictor:
    """Optional Kronos wrapper.

    Requires the Kronos package and torch to be installed separately.
    Fallback is handled by ``build_direction_predictor``.
    """

    def __init__(
        self,
        binance: BinanceClient | None = None,
        lookback: int = 360,
        sample_count: int = 8,
        device: str = "cpu",
    ) -> None:
        self._binance = binance or BinanceClient()
        self._lookback = lookback
        self._sample_count = sample_count
        self._device = device
        self._predictor = None

    def predict_next_hour(self, symbol: str = "BTCUSDT") -> DirectionPrediction:
        predictor = self._load_predictor()
        klines = self._binance.get_klines(symbol=symbol, interval="1h", limit=self._lookback + 2)
        if len(klines) < self._lookback:
            raise ValueError(f"need {self._lookback} Binance 1h candles for Kronos")

        import pandas as pd

        history = klines[-self._lookback :]
        frame = _klines_to_frame(history)
        x_timestamp = frame["timestamps"]
        last_close = history[-1].close_time
        y_timestamp = pd.Series([last_close + (history[-1].close_time - history[-1].open_time)])

        pred_df = predictor.predict(
            df=frame[["open", "high", "low", "close", "volume", "amount"]],
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=1,
            T=1.0,
            top_p=0.9,
            sample_count=self._sample_count,
        )
        predicted_close = float(pred_df["close"].iloc[0])
        last_close_price = float(history[-1].close)
        side = "UP" if predicted_close >= last_close_price else "DOWN"
        move = abs(predicted_close - last_close_price) / last_close_price if last_close_price else 0.0
        confidence = min(0.5 + move * 5, 0.95)
        return DirectionPrediction(
            side=side,
            confidence=confidence,
            source="kronos",
            detail=f"pred_close={predicted_close:.2f} last_close={last_close_price:.2f}",
        )

    def _load_predictor(self):
        if self._predictor is not None:
            return self._predictor
        try:
            from model import Kronos, KronosPredictor as UpstreamKronosPredictor, KronosTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Kronos is not installed. Clone https://github.com/shiyu-coder/Kronos "
                "and install its dependencies, or use --predictor heuristic."
            ) from exc

        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
        self._predictor = UpstreamKronosPredictor(model, tokenizer, device=self._device, max_context=2048)
        return self._predictor


def build_direction_predictor(kind: str = "auto", binance: BinanceClient | None = None) -> DirectionPredictor:
    kind = kind.lower()
    client = binance or BinanceClient()
    if kind == "heuristic":
        return LastCandleMomentumPredictor(client)
    if kind == "kronos":
        return KronosPredictor(client)
    if kind != "auto":
        raise ValueError(f"unsupported predictor kind: {kind}")

    try:
        predictor = KronosPredictor(client)
        predictor._load_predictor()
        LOGGER.info("Using Kronos predictor")
        return predictor
    except Exception as exc:
        LOGGER.warning("Kronos unavailable (%s); using heuristic predictor", exc)
        return LastCandleMomentumPredictor(client)


def _klines_to_frame(klines: list[Kline]):
    import pandas as pd

    return pd.DataFrame(
        {
            "timestamps": [item.open_time for item in klines],
            "open": [item.open for item in klines],
            "high": [item.high for item in klines],
            "low": [item.low for item in klines],
            "close": [item.close for item in klines],
            "volume": [item.volume for item in klines],
            "amount": [item.quote_volume for item in klines],
        }
    )
