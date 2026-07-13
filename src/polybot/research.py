from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from polybot.config import load_env
from polybot.polymarket_client import MarketMetadata, PolymarketClient


DEFAULT_RESEARCH_DB = Path("data/research.sqlite3")


@dataclass(frozen=True)
class PriceSample:
    sampled_at: datetime
    market_id: str
    slug: str
    outcome: str
    token_id: str
    bid: float | None
    ask: float | None
    mid: float | None
    end_time: datetime | None


@dataclass(frozen=True)
class ThresholdResult:
    outcome: str
    threshold: float
    target: float
    windows_seen: int
    opportunities: int
    successes: int

    @property
    def success_rate(self) -> float | None:
        if self.opportunities == 0:
            return None
        return self.successes / self.opportunities


class ResearchStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def insert_samples(self, samples: Iterable[PriceSample]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO price_samples (
                    sampled_at,
                    market_id,
                    slug,
                    outcome,
                    token_id,
                    bid,
                    ask,
                    mid,
                    end_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        _to_iso(sample.sampled_at),
                        sample.market_id,
                        sample.slug,
                        sample.outcome,
                        sample.token_id,
                        sample.bid,
                        sample.ask,
                        sample.mid,
                        _to_iso(sample.end_time) if sample.end_time else None,
                    )
                    for sample in samples
                ],
            )

    def load_samples(self, asset: str, timeframe: str) -> list[PriceSample]:
        slug_prefix = f"{asset.lower()}-updown-{timeframe}-"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sampled_at, market_id, slug, outcome, token_id, bid, ask, mid, end_time
                FROM price_samples
                WHERE slug LIKE ?
                ORDER BY slug, outcome, sampled_at
                """,
                (f"{slug_prefix}%",),
            ).fetchall()
        return [
            PriceSample(
                sampled_at=_from_iso(row[0]),
                market_id=row[1],
                slug=row[2],
                outcome=row[3],
                token_id=row[4],
                bid=row[5],
                ask=row[6],
                mid=row[7],
                end_time=_from_iso(row[8]) if row[8] else None,
            )
            for row in rows
        ]

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_samples (
                    sampled_at TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    bid REAL,
                    ask REAL,
                    mid REAL,
                    end_time TEXT,
                    PRIMARY KEY (sampled_at, token_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_price_samples_slug_outcome_time
                ON price_samples (slug, outcome, sampled_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)


def collect_updown_once(client: PolymarketClient, asset: str, timeframe: str) -> list[PriceSample]:
    metadata = client.resolve_updown_market(asset=asset, timeframe=timeframe)
    if metadata.closed or metadata.accepting_orders is False:
        return []

    sampled_at = datetime.now(timezone.utc)
    samples = []
    for outcome, token_id in sorted(metadata.tokens_by_outcome.items()):
        price = client.get_price(token_id)
        samples.append(
            PriceSample(
                sampled_at=sampled_at,
                market_id=metadata.market_id,
                slug=metadata.slug or "",
                outcome=outcome,
                token_id=token_id,
                bid=price.bid,
                ask=price.ask,
                mid=price.current,
                end_time=metadata.end_time,
            )
        )
    return samples


def analyze_double_targets(
    samples: Iterable[PriceSample],
    thresholds: Iterable[float],
) -> list[ThresholdResult]:
    sample_list = list(samples)
    outcomes = sorted({sample.outcome for sample in sample_list})
    windows_seen = len({sample.slug for sample in sample_list})
    results = []

    for outcome in outcomes:
        outcome_samples = [sample for sample in sample_list if sample.outcome == outcome]
        by_slug: dict[str, list[PriceSample]] = {}
        for sample in outcome_samples:
            by_slug.setdefault(sample.slug, []).append(sample)

        for threshold in thresholds:
            target = round(threshold * 2, 4)
            opportunities = 0
            successes = 0
            for window_samples in by_slug.values():
                ordered = sorted(window_samples, key=lambda sample: sample.sampled_at)
                entry_index = _first_entry_index(ordered, threshold)
                if entry_index is None:
                    continue
                opportunities += 1
                if _hits_target_after_entry(ordered[entry_index + 1 :], target):
                    successes += 1
            results.append(
                ThresholdResult(
                    outcome=outcome,
                    threshold=threshold,
                    target=target,
                    windows_seen=windows_seen,
                    opportunities=opportunities,
                    successes=successes,
                )
            )
    return results


def monitor_main(argv: list[str] | None = None) -> int:
    args = _parse_monitor_args(argv)
    env = load_env(args.env_file)
    client = PolymarketClient(env)
    store = ResearchStore(Path(args.db))

    end_at = time.monotonic() + (args.duration_minutes * 60)
    while time.monotonic() < end_at:
        started = time.monotonic()
        try:
            samples = collect_updown_once(client, args.asset, args.timeframe)
            store.insert_samples(samples)
            if samples:
                summary = ", ".join(
                    f"{sample.outcome} bid={sample.bid} ask={sample.ask} mid={sample.mid:.4f}"
                    for sample in samples
                    if sample.mid is not None
                )
                print(f"{_to_iso(samples[0].sampled_at)} {samples[0].slug} {summary}", flush=True)
            else:
                print("No samples collected; market is closed or not accepting orders.", flush=True)
        except Exception as exc:
            print(f"sample failed: {exc}", flush=True)

        elapsed = time.monotonic() - started
        time.sleep(max(args.interval_seconds - elapsed, 0))
    return 0


def analyze_main(argv: list[str] | None = None) -> int:
    args = _parse_analyze_args(argv)
    store = ResearchStore(Path(args.db))
    thresholds = _thresholds(args.min_threshold, args.max_threshold, args.step)
    results = analyze_double_targets(
        samples=store.load_samples(asset=args.asset, timeframe=args.timeframe),
        thresholds=thresholds,
    )
    print(_format_results_table(results))
    return 0


def _first_entry_index(samples: list[PriceSample], threshold: float) -> int | None:
    for index, sample in enumerate(samples):
        if sample.ask is not None and sample.ask <= threshold:
            return index
    return None


def _hits_target_after_entry(samples: list[PriceSample], target: float) -> bool:
    return any(sample.bid is not None and sample.bid >= target for sample in samples)


def _thresholds(min_threshold: float, max_threshold: float, step: float) -> list[float]:
    thresholds = []
    current = min_threshold
    while current <= max_threshold + 1e-9:
        thresholds.append(round(current, 4))
        current += step
    return thresholds


def _format_results_table(results: list[ThresholdResult]) -> str:
    lines = [
        "| outcome | a | 2a target | windows | opportunities | successes | success % |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        success_rate = "" if result.success_rate is None else f"{result.success_rate * 100:.1f}%"
        lines.append(
            "| "
            f"{result.outcome} | "
            f"{result.threshold:.2f} | "
            f"{result.target:.2f} | "
            f"{result.windows_seen} | "
            f"{result.opportunities} | "
            f"{result.successes} | "
            f"{success_rate} |"
        )
    return "\n".join(lines)


def _parse_monitor_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor public BTC/crypto Up/Down market prices.")
    parser.add_argument("--asset", default="btc", help="Up/Down asset, e.g. btc.")
    parser.add_argument("--timeframe", default="5m", help="Up/Down timeframe, e.g. 5m.")
    parser.add_argument("--interval-seconds", type=float, default=1.0, help="Sampling interval.")
    parser.add_argument("--duration-minutes", type=float, default=180, help="How long to collect.")
    parser.add_argument("--db", default=str(DEFAULT_RESEARCH_DB), help="SQLite output path.")
    parser.add_argument("--env-file", help="Optional .env file for endpoint overrides.")
    return parser.parse_args(argv)


def _parse_analyze_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze whether threshold a later reached 2a.")
    parser.add_argument("--asset", default="btc", help="Up/Down asset, e.g. btc.")
    parser.add_argument("--timeframe", default="5m", help="Up/Down timeframe, e.g. 5m.")
    parser.add_argument("--min-threshold", type=float, default=0.05)
    parser.add_argument("--max-threshold", type=float, default=0.50)
    parser.add_argument("--step", type=float, default=0.01)
    parser.add_argument("--db", default=str(DEFAULT_RESEARCH_DB), help="SQLite input path.")
    return parser.parse_args(argv)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
