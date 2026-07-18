from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from polybot.research import PriceSample, ResearchStore

ExitMode = Literal["hold", "tp"]


@dataclass(frozen=True)
class WindowSide:
    samples: list[PriceSample]

    @property
    def slug(self) -> str:
        return self.samples[0].slug

    @property
    def outcome(self) -> str:
        return self.samples[0].outcome

    def window_start_ts(self) -> int | None:
        # slug like btc-updown-5m-1784363400
        try:
            return int(self.slug.rsplit("-", 1)[-1])
        except ValueError:
            return None

    def end_time(self) -> datetime | None:
        for sample in reversed(self.samples):
            if sample.end_time is not None:
                return sample.end_time
        return None


@dataclass(frozen=True)
class TradeResult:
    slug: str
    outcome: str
    entry_ask: float
    stake: float
    payout: float
    exited_via: str

    @property
    def roi(self) -> float:
        if self.stake <= 0:
            return 0.0
        return self.payout / self.stake - 1.0


@dataclass(frozen=True)
class StrategyScore:
    name: str
    windows: int
    trades: int
    total_stake: float
    total_payout: float
    win_trades: int

    @property
    def capital_roi(self) -> float | None:
        if self.total_stake <= 0:
            return None
        return self.total_payout / self.total_stake - 1.0

    @property
    def win_rate(self) -> float | None:
        if self.trades <= 0:
            return None
        return self.win_trades / self.trades


def load_windows(db_path: Path, asset: str = "btc", timeframe: str = "5m") -> dict[str, dict[str, WindowSide]]:
    store = ResearchStore(db_path)
    samples = store.load_samples(asset=asset, timeframe=timeframe)
    by_slug: dict[str, dict[str, list[PriceSample]]] = defaultdict(lambda: defaultdict(list))
    for sample in samples:
        by_slug[sample.slug][sample.outcome.upper()].append(sample)
    windows: dict[str, dict[str, WindowSide]] = {}
    for slug, sides in by_slug.items():
        windows[slug] = {
            outcome: WindowSide(samples=sorted(rows, key=lambda s: s.sampled_at))
            for outcome, rows in sides.items()
            if rows
        }
    return windows


def infer_winner(sides: dict[str, WindowSide]) -> str | None:
    """Infer window winner from the latest mid on each side (UP/DOWN ~1)."""
    finals: dict[str, float] = {}
    for outcome, side in sides.items():
        for sample in reversed(side.samples):
            if sample.mid is not None:
                finals[outcome] = sample.mid
                break
    if not finals:
        return None
    # Winner has final mid closer to 1; require a decisive print.
    best = max(finals.items(), key=lambda item: item[1])
    if best[1] < 0.7:
        return None
    return best[0]


def seconds_into_window(sample: PriceSample, window_start_ts: int | None) -> float | None:
    if window_start_ts is None:
        return None
    return sample.sampled_at.timestamp() - float(window_start_ts)


def time_left_seconds(sample: PriceSample, end_time: datetime | None) -> float | None:
    if end_time is None:
        return None
    end = end_time if end_time.tzinfo else end_time.replace(tzinfo=timezone.utc)
    return (end - sample.sampled_at).total_seconds()


def simulate_single_entry(
    side: WindowSide,
    *,
    entry_index: int,
    stake: float,
    exit_mode: ExitMode,
    tp_bid: float | None,
    winner: str | None,
) -> TradeResult:
    entry = side.samples[entry_index]
    assert entry.ask is not None and entry.ask > 0
    shares = stake / entry.ask
    exited_via = "settle"
    payout = 0.0

    if exit_mode == "tp" and tp_bid is not None:
        for sample in side.samples[entry_index + 1 :]:
            if sample.bid is not None and sample.bid >= tp_bid:
                payout = shares * tp_bid
                exited_via = "tp"
                break

    if exited_via == "settle":
        if winner == side.outcome:
            payout = shares * 1.0
            exited_via = "settle_win"
        else:
            payout = 0.0
            exited_via = "settle_loss"

    return TradeResult(
        slug=side.slug,
        outcome=side.outcome,
        entry_ask=entry.ask,
        stake=stake,
        payout=payout,
        exited_via=exited_via,
    )


def score_trades(name: str, windows: int, trades: Iterable[TradeResult]) -> StrategyScore:
    trade_list = list(trades)
    return StrategyScore(
        name=name,
        windows=windows,
        trades=len(trade_list),
        total_stake=sum(t.stake for t in trade_list),
        total_payout=sum(t.payout for t in trade_list),
        win_trades=sum(1 for t in trade_list if t.payout > t.stake),
    )


def baseline_down_a036(
    windows: dict[str, dict[str, WindowSide]],
    *,
    stake: float = 1.0,
    entry_ask: float = 0.36,
    tp_bid: float = 0.97,
    min_time_left: float = 240.0,
) -> StrategyScore:
    trades: list[TradeResult] = []
    for sides in windows.values():
        down = sides.get("DOWN")
        if down is None:
            continue
        winner = infer_winner(sides)
        end_time = down.end_time()
        for idx, sample in enumerate(down.samples):
            if sample.ask is None or sample.ask > entry_ask:
                continue
            left = time_left_seconds(sample, end_time)
            if left is None or left < min_time_left:
                continue
            trades.append(
                simulate_single_entry(
                    down,
                    entry_index=idx,
                    stake=stake,
                    exit_mode="tp",
                    tp_bid=tp_bid,
                    winner=winner,
                )
            )
            break
    return score_trades("baseline_DOWN_a0.36_x2.7_tleft>=240s", len(windows), trades)


def thesis_a_race(
    windows: dict[str, dict[str, WindowSide]],
    *,
    stake: float = 1.0,
    entry_ask: float = 0.30,
    entry_window_seconds: float = 60.0,
    exit_mode: ExitMode = "tp",
    tp_bid: float = 0.95,
) -> StrategyScore:
    """First of UP/DOWN to print ask<=a in the first T seconds; one trade."""
    trades: list[TradeResult] = []
    for sides in windows.values():
        winner = infer_winner(sides)
        candidates: list[tuple[datetime, str, int]] = []
        for outcome in ("UP", "DOWN"):
            side = sides.get(outcome)
            if side is None:
                continue
            start_ts = side.window_start_ts()
            for idx, sample in enumerate(side.samples):
                into = seconds_into_window(sample, start_ts)
                if into is None or into > entry_window_seconds:
                    continue
                if sample.ask is not None and sample.ask <= entry_ask:
                    candidates.append((sample.sampled_at, outcome, idx))
                    break
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0])
        _, outcome, idx = candidates[0]
        trades.append(
            simulate_single_entry(
                sides[outcome],
                entry_index=idx,
                stake=stake,
                exit_mode=exit_mode,
                tp_bid=tp_bid,
                winner=winner,
            )
        )
    name = f"thesisA_race_a{entry_ask}_first{int(entry_window_seconds)}s_{exit_mode}_{tp_bid}"
    return score_trades(name, len(windows), trades)


def thesis_b_scale_in(
    windows: dict[str, dict[str, WindowSide]],
    *,
    unit_stake: float = 1.0,
    levels: tuple[float, ...] = (0.35, 0.30, 0.25),
    entry_window_seconds: float = 60.0,
    exit_mode: ExitMode = "hold",
    tp_bid: float | None = None,
    both_sides: bool = True,
) -> StrategyScore:
    """
    In first T seconds, for each side (or both): each time ask first crosses a lower
    level, buy another unit. Levels are independent first-touch thresholds.
    """
    trades: list[TradeResult] = []
    for sides in windows.values():
        winner = infer_winner(sides)
        outcomes = ("UP", "DOWN") if both_sides else ("DOWN",)
        for outcome in outcomes:
            side = sides.get(outcome)
            if side is None:
                continue
            start_ts = side.window_start_ts()
            hit_levels: set[float] = set()
            for idx, sample in enumerate(side.samples):
                into = seconds_into_window(sample, start_ts)
                if into is None or into > entry_window_seconds:
                    continue
                if sample.ask is None:
                    continue
                for level in levels:
                    if level in hit_levels:
                        continue
                    if sample.ask <= level:
                        hit_levels.add(level)
                        trades.append(
                            simulate_single_entry(
                                side,
                                entry_index=idx,
                                stake=unit_stake,
                                exit_mode=exit_mode,
                                tp_bid=tp_bid,
                                winner=winner,
                            )
                        )
    name = (
        f"thesisB_scale_{'+'.join(str(x) for x in levels)}"
        f"_first{int(entry_window_seconds)}s_{'both' if both_sides else 'DOWN'}_{exit_mode}"
    )
    return score_trades(name, len(windows), trades)


def creative_variants(windows: dict[str, dict[str, WindowSide]], stake: float = 1.0) -> list[StrategyScore]:
    scores: list[StrategyScore] = []
    # Race variants
    for a in (0.25, 0.28, 0.30, 0.32, 0.35):
        for t in (30.0, 60.0):
            for exit_mode, tp in (("hold", None), ("tp", 0.95), ("tp", 0.97)):
                if exit_mode == "hold":
                    scores.append(
                        thesis_a_race(
                            windows,
                            stake=stake,
                            entry_ask=a,
                            entry_window_seconds=t,
                            exit_mode="hold",
                            tp_bid=0.95,
                        )
                    )
                    # overwrite name semantics: hold ignores tp
                    s = scores[-1]
                    scores[-1] = StrategyScore(
                        name=f"thesisA_race_a{a}_first{int(t)}s_hold",
                        windows=s.windows,
                        trades=s.trades,
                        total_stake=s.total_stake,
                        total_payout=s.total_payout,
                        win_trades=s.win_trades,
                    )
                else:
                    assert tp is not None
                    scores.append(
                        thesis_a_race(
                            windows,
                            stake=stake,
                            entry_ask=a,
                            entry_window_seconds=t,
                            exit_mode="tp",
                            tp_bid=tp,
                        )
                    )
    # Scale-in variants
    for t in (30.0, 60.0):
        for both in (True, False):
            for exit_mode, tp in (("hold", None), ("tp", 0.95)):
                scores.append(
                    thesis_b_scale_in(
                        windows,
                        unit_stake=stake,
                        levels=(0.35, 0.30, 0.25),
                        entry_window_seconds=t,
                        exit_mode=exit_mode,  # type: ignore[arg-type]
                        tp_bid=tp,
                        both_sides=both,
                    )
                )
            scores.append(
                thesis_b_scale_in(
                    windows,
                    unit_stake=stake,
                    levels=(0.40, 0.35, 0.30),
                    entry_window_seconds=t,
                    exit_mode="tp",
                    tp_bid=0.95,
                    both_sides=both,
                )
            )
    # Early DOWN-only leader-like with tighter a
    for a, x, tleft in ((0.32, 3.0, 270.0), (0.30, 3.0, 240.0), (0.36, 2.7, 270.0)):
        trades: list[TradeResult] = []
        for sides in windows.values():
            down = sides.get("DOWN")
            if down is None:
                continue
            winner = infer_winner(sides)
            end_time = down.end_time()
            tp = min(round(a * x, 2), 0.99)
            for idx, sample in enumerate(down.samples):
                if sample.ask is None or sample.ask > a:
                    continue
                left = time_left_seconds(sample, end_time)
                if left is None or left < tleft:
                    continue
                trades.append(
                    simulate_single_entry(
                        down,
                        entry_index=idx,
                        stake=stake,
                        exit_mode="tp",
                        tp_bid=tp,
                        winner=winner,
                    )
                )
                break
        scores.append(score_trades(f"down_only_a{a}_x{x}_tleft>={int(tleft)}", len(windows), trades))
    return scores


def format_score_table(scores: Iterable[StrategyScore], min_trades: int = 1) -> str:
    rows = [s for s in scores if s.trades >= min_trades and s.capital_roi is not None]
    rows.sort(key=lambda s: s.capital_roi or -999, reverse=True)
    lines = [
        "| rank | strategy | windows | trades | capital ROI | win% |",
        "| ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for idx, score in enumerate(rows, start=1):
        roi = score.capital_roi
        win = score.win_rate
        lines.append(
            f"| {idx} | `{score.name}` | {score.windows} | {score.trades} | "
            f"{roi * 100:+.1f}% | {(win or 0) * 100:.1f}% |"
        )
    if len(lines) == 2:
        lines.append("| | _(no strategies with enough trades)_ | | | | |")
    return "\n".join(lines)


def analyze_db(db_path: Path, stake: float = 1.0) -> list[StrategyScore]:
    windows = load_windows(db_path)
    scores = [
        baseline_down_a036(windows, stake=stake),
        thesis_a_race(windows, stake=stake, entry_ask=0.30, entry_window_seconds=30, exit_mode="tp", tp_bid=0.95),
        thesis_a_race(windows, stake=stake, entry_ask=0.30, entry_window_seconds=60, exit_mode="tp", tp_bid=0.95),
        thesis_a_race(windows, stake=stake, entry_ask=0.30, entry_window_seconds=60, exit_mode="hold", tp_bid=0.95),
        thesis_b_scale_in(windows, unit_stake=stake, entry_window_seconds=30, exit_mode="hold"),
        thesis_b_scale_in(windows, unit_stake=stake, entry_window_seconds=60, exit_mode="hold"),
        thesis_b_scale_in(windows, unit_stake=stake, entry_window_seconds=60, exit_mode="tp", tp_bid=0.95),
    ]
    scores.extend(creative_variants(windows, stake=stake))
    # de-dupe by name keeping first
    seen: set[str] = set()
    unique: list[StrategyScore] = []
    for score in scores:
        if score.name in seen:
            continue
        seen.add(score.name)
        unique.append(score)
    return unique
