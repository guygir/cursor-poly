from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from polybot.binance import BinanceClient
from polybot.config import load_env
from polybot.kronos_predictor import DirectionPrediction, DirectionPredictor, build_direction_predictor
from polybot.polymarket_client import MarketMetadata, PolymarketClient
from polybot.research import DEFAULT_RESEARCH_DB, _from_iso, _to_iso


@dataclass(frozen=True)
class HourlyObservation:
    observed_at: datetime
    asset: str
    timeframe: str
    slug: str
    market_id: str
    start_time: datetime | None
    end_time: datetime | None
    prediction_side: str | None
    prediction_confidence: float | None
    prediction_source: str | None
    max_entry_price: float
    up_bid: float | None
    up_ask: float | None
    down_bid: float | None
    down_ask: float | None
    selected_side: str | None
    selected_ask: float | None
    entry_available: bool
    latest_binance_close: float | None
    resolved_winner: str | None = None
    simulated_pnl: float | None = None


@dataclass(frozen=True)
class HourlyScoreSummary:
    windows: int
    entries: int
    wins: int
    losses: int
    skipped_no_entry: int
    avg_entry_ask: float | None
    total_simulated_pnl: float
    avg_simulated_pnl: float | None


class HourlyResearchStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def insert_observation(self, observation: HourlyObservation) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO hourly_observations (
                    observed_at,
                    asset,
                    timeframe,
                    slug,
                    market_id,
                    start_time,
                    end_time,
                    prediction_side,
                    prediction_confidence,
                    prediction_source,
                    max_entry_price,
                    up_bid,
                    up_ask,
                    down_bid,
                    down_ask,
                    selected_side,
                    selected_ask,
                    entry_available,
                    latest_binance_close,
                    resolved_winner,
                    simulated_pnl
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _to_iso(observation.observed_at),
                    observation.asset,
                    observation.timeframe,
                    observation.slug,
                    observation.market_id,
                    _to_iso(observation.start_time) if observation.start_time else None,
                    _to_iso(observation.end_time) if observation.end_time else None,
                    observation.prediction_side,
                    observation.prediction_confidence,
                    observation.prediction_source,
                    observation.max_entry_price,
                    observation.up_bid,
                    observation.up_ask,
                    observation.down_bid,
                    observation.down_ask,
                    observation.selected_side,
                    observation.selected_ask,
                    int(observation.entry_available),
                    observation.latest_binance_close,
                    observation.resolved_winner,
                    observation.simulated_pnl,
                ),
            )

    def load_observations(self) -> list[HourlyObservation]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    observed_at,
                    asset,
                    timeframe,
                    slug,
                    market_id,
                    start_time,
                    end_time,
                    prediction_side,
                    prediction_confidence,
                    prediction_source,
                    max_entry_price,
                    up_bid,
                    up_ask,
                    down_bid,
                    down_ask,
                    selected_side,
                    selected_ask,
                    entry_available,
                    latest_binance_close,
                    resolved_winner,
                    simulated_pnl
                FROM hourly_observations
                ORDER BY observed_at
                """
            ).fetchall()
        return [_observation_from_row(row) for row in rows]

    def update_resolution(self, slug: str, winner: str, simulated_pnl: float | None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE hourly_observations
                SET resolved_winner = ?, simulated_pnl = ?
                WHERE slug = ?
                """,
                (winner, simulated_pnl, slug),
            )
            return int(cursor.rowcount)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hourly_observations (
                    observed_at TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    prediction_side TEXT,
                    prediction_confidence REAL,
                    prediction_source TEXT,
                    max_entry_price REAL NOT NULL,
                    up_bid REAL,
                    up_ask REAL,
                    down_bid REAL,
                    down_ask REAL,
                    selected_side TEXT,
                    selected_ask REAL,
                    entry_available INTEGER NOT NULL,
                    latest_binance_close REAL,
                    resolved_winner TEXT,
                    simulated_pnl REAL,
                    PRIMARY KEY (observed_at, slug)
                )
                """
            )
            # Best-effort migrations for earlier WIP schemas.
            for column, typedef in (
                ("prediction_confidence", "REAL"),
                ("prediction_source", "TEXT"),
                ("resolved_winner", "TEXT"),
                ("simulated_pnl", "REAL"),
            ):
                try:
                    conn.execute(f"ALTER TABLE hourly_observations ADD COLUMN {column} {typedef}")
                except sqlite3.OperationalError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)


def monitor_hourly_main(argv: list[str] | None = None) -> int:
    args = _parse_monitor_args(argv)
    env = load_env(args.env_file)
    polymarket = PolymarketClient(env)
    binance = BinanceClient()
    predictor = build_direction_predictor(args.predictor, binance=binance)
    store = HourlyResearchStore(Path(args.db))

    end_at = time.monotonic() + (args.duration_minutes * 60)
    while time.monotonic() < end_at:
        started = time.monotonic()
        try:
            observation = collect_hourly_observation(
                polymarket=polymarket,
                binance=binance,
                predictor=predictor,
                asset=args.asset,
                timeframe=args.timeframe,
                prediction_side=args.prediction_side,
                max_entry_price=args.max_entry_price,
            )
            store.insert_observation(observation)
            print(_format_observation(observation), flush=True)
        except Exception as exc:
            print(f"hourly sample failed: {exc}", flush=True)

        elapsed = time.monotonic() - started
        time.sleep(max(args.interval_seconds - elapsed, 0))
    return 0


def analyze_hourly_main(argv: list[str] | None = None) -> int:
    args = _parse_analyze_args(argv)
    store = HourlyResearchStore(Path(args.db))
    observations = store.load_observations()
    if args.resolve:
        env = load_env(args.env_file)
        polymarket = PolymarketClient(env)
        observations = resolve_finished_windows(store, polymarket, observations)

    summary = score_hourly_observations(observations)
    print(_format_summary(summary), flush=True)
    return 0


def collect_hourly_observation(
    polymarket: PolymarketClient,
    binance: BinanceClient,
    predictor: DirectionPredictor | None,
    asset: str,
    timeframe: str,
    prediction_side: str | None,
    max_entry_price: float,
) -> HourlyObservation:
    metadata = resolve_active_hourly_market(polymarket, asset=asset, timeframe=timeframe)
    prices = {}
    for outcome, token_id in metadata.tokens_by_outcome.items():
        prices[outcome.upper()] = polymarket.get_price(token_id)

    latest_close = _latest_binance_close(binance)
    prediction = _resolve_prediction(predictor, prediction_side)
    selected_side = prediction.side if prediction else None
    selected_ask = prices[selected_side].ask if selected_side in prices else None
    entry_available = selected_ask is not None and selected_ask <= max_entry_price

    return HourlyObservation(
        observed_at=datetime.now(timezone.utc),
        asset=asset.lower(),
        timeframe=timeframe.lower(),
        slug=metadata.slug or "",
        market_id=metadata.market_id,
        start_time=metadata.start_time,
        end_time=metadata.end_time,
        prediction_side=selected_side,
        prediction_confidence=prediction.confidence if prediction else None,
        prediction_source=prediction.source if prediction else None,
        max_entry_price=max_entry_price,
        up_bid=prices.get("UP").bid if "UP" in prices else None,
        up_ask=prices.get("UP").ask if "UP" in prices else None,
        down_bid=prices.get("DOWN").bid if "DOWN" in prices else None,
        down_ask=prices.get("DOWN").ask if "DOWN" in prices else None,
        selected_side=selected_side,
        selected_ask=selected_ask,
        entry_available=entry_available,
        latest_binance_close=latest_close,
    )


def resolve_active_hourly_market(polymarket: PolymarketClient, asset: str, timeframe: str) -> MarketMetadata:
    try:
        metadata = polymarket.resolve_updown_market(asset=asset, timeframe=timeframe)
        if _is_tradeable(metadata):
            return metadata
    except Exception:
        pass

    candidates = [metadata for metadata in polymarket.find_active_updown_markets(asset, timeframe) if _is_tradeable(metadata)]
    if not candidates:
        raise ValueError(f"could not resolve active {asset} {timeframe} Up/Down market")
    return candidates[0]


def resolve_finished_windows(
    store: HourlyResearchStore,
    polymarket: PolymarketClient,
    observations: list[HourlyObservation],
) -> list[HourlyObservation]:
    now = datetime.now(timezone.utc)
    by_slug: dict[str, list[HourlyObservation]] = {}
    for observation in observations:
        by_slug.setdefault(observation.slug, []).append(observation)

    for slug, rows in by_slug.items():
        if any(row.resolved_winner for row in rows):
            continue
        end_time = rows[0].end_time
        if end_time is None or end_time > now:
            continue
        try:
            metadata = polymarket.resolve_market_slug(slug)
            prices = {}
            for outcome, token_id in metadata.tokens_by_outcome.items():
                prices[outcome.upper()] = polymarket.get_price(token_id)
            winner = infer_winner_from_prices(
                up_mid=_mid(prices.get("UP")),
                down_mid=_mid(prices.get("DOWN")),
            )
            if winner is None:
                continue
            for row in rows:
                pnl = simulate_entry_pnl(row, winner)
                store.update_resolution(slug, winner, pnl)
        except Exception as exc:
            print(f"resolve failed for {slug}: {exc}", flush=True)

    return store.load_observations()


def score_hourly_observations(observations: list[HourlyObservation]) -> HourlyScoreSummary:
    # One decision per slug: first observation in that window.
    first_by_slug: dict[str, HourlyObservation] = {}
    for observation in observations:
        first_by_slug.setdefault(observation.slug, observation)

    windows = list(first_by_slug.values())
    entries = [row for row in windows if row.entry_available and row.selected_side]
    wins = [row for row in entries if row.resolved_winner and row.selected_side == row.resolved_winner]
    losses = [row for row in entries if row.resolved_winner and row.selected_side != row.resolved_winner]
    skipped = [row for row in windows if not row.entry_available]
    pnls = [row.simulated_pnl for row in entries if row.simulated_pnl is not None]
    asks = [row.selected_ask for row in entries if row.selected_ask is not None]
    total_pnl = sum(pnls) if pnls else 0.0
    return HourlyScoreSummary(
        windows=len(windows),
        entries=len(entries),
        wins=len(wins),
        losses=len(losses),
        skipped_no_entry=len(skipped),
        avg_entry_ask=(sum(asks) / len(asks)) if asks else None,
        total_simulated_pnl=total_pnl,
        avg_simulated_pnl=(total_pnl / len(pnls)) if pnls else None,
    )


def simulate_entry_pnl(observation: HourlyObservation, winner: str, stake: float = 1.0) -> float | None:
    if not observation.entry_available or not observation.selected_side or observation.selected_ask is None:
        return None
    ask = observation.selected_ask
    if ask <= 0:
        return None
    if observation.selected_side == winner:
        return (stake / ask) - stake
    return -stake


def infer_winner_from_prices(up_mid: float | None, down_mid: float | None) -> str | None:
    if up_mid is None or down_mid is None:
        return None
    if up_mid >= 0.9 and down_mid <= 0.1:
        return "UP"
    if down_mid >= 0.9 and up_mid <= 0.1:
        return "DOWN"
    if up_mid > down_mid and up_mid >= 0.7:
        return "UP"
    if down_mid > up_mid and down_mid >= 0.7:
        return "DOWN"
    return None


def _resolve_prediction(
    predictor: DirectionPredictor | None,
    manual_side: str | None,
) -> DirectionPrediction | None:
    if manual_side:
        return DirectionPrediction(
            side=manual_side.upper(),
            confidence=1.0,
            source="manual",
            detail="cli --prediction-side",
        )
    if predictor is None:
        return None
    return predictor.predict_next_hour()


def _is_tradeable(metadata: MarketMetadata) -> bool:
    return not metadata.closed and metadata.accepting_orders is not False and bool(metadata.tokens_by_outcome)


def _latest_binance_close(binance: BinanceClient) -> float | None:
    klines = binance.get_klines(symbol="BTCUSDT", interval="1h", limit=2)
    if not klines:
        return None
    return klines[-1].close


def _mid(price) -> float | None:
    if price is None:
        return None
    if price.bid is None or price.ask is None:
        return price.mid
    return (price.bid + price.ask) / 2


def _format_observation(observation: HourlyObservation) -> str:
    selected = (
        f" selected={observation.selected_side} ask={observation.selected_ask} "
        f"entry_available={observation.entry_available} "
        f"src={observation.prediction_source} conf={observation.prediction_confidence}"
        if observation.selected_side
        else " selected=NONE"
    )
    return (
        f"{_to_iso(observation.observed_at)} {observation.slug} "
        f"UP bid={observation.up_bid} ask={observation.up_ask} "
        f"DOWN bid={observation.down_bid} ask={observation.down_ask} "
        f"binance_close={observation.latest_binance_close}"
        f"{selected}"
    )


def _format_summary(summary: HourlyScoreSummary) -> str:
    return (
        f"windows={summary.windows} entries={summary.entries} "
        f"wins={summary.wins} losses={summary.losses} skipped_no_entry={summary.skipped_no_entry} "
        f"avg_entry_ask={summary.avg_entry_ask} "
        f"total_simulated_pnl={summary.total_simulated_pnl} "
        f"avg_simulated_pnl={summary.avg_simulated_pnl}"
    )


def _parse_monitor_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor BTC 1H Polymarket windows for Kronos research.")
    parser.add_argument("--asset", default="btc")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--duration-minutes", type=float, default=180)
    parser.add_argument("--prediction-side", choices=["UP", "DOWN"], help="Manual override instead of predictor.")
    parser.add_argument(
        "--predictor",
        choices=["auto", "heuristic", "kronos"],
        default="auto",
        help="auto tries Kronos then falls back to Binance last-candle heuristic.",
    )
    parser.add_argument("--max-entry-price", type=float, default=0.52)
    parser.add_argument("--db", default=str(DEFAULT_RESEARCH_DB), help="SQLite output path.")
    parser.add_argument("--env-file", help="Optional .env file for endpoint overrides.")
    return parser.parse_args(argv)


def _parse_analyze_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score hourly Kronos/entry research windows.")
    parser.add_argument("--db", default=str(DEFAULT_RESEARCH_DB), help="SQLite research DB.")
    parser.add_argument("--resolve", action="store_true", help="Fetch finished-window winners from Polymarket.")
    parser.add_argument("--env-file", help="Optional .env file for endpoint overrides.")
    return parser.parse_args(argv)


def _observation_from_row(row: tuple) -> HourlyObservation:
    return HourlyObservation(
        observed_at=_from_iso(row[0]),
        asset=row[1],
        timeframe=row[2],
        slug=row[3],
        market_id=row[4],
        start_time=_from_iso(row[5]) if row[5] else None,
        end_time=_from_iso(row[6]) if row[6] else None,
        prediction_side=row[7],
        prediction_confidence=row[8],
        prediction_source=row[9],
        max_entry_price=row[10],
        up_bid=row[11],
        up_ask=row[12],
        down_bid=row[13],
        down_ask=row[14],
        selected_side=row[15],
        selected_ask=row[16],
        entry_available=bool(row[17]),
        latest_binance_close=row[18],
        resolved_winner=row[19],
        simulated_pnl=row[20],
    )
