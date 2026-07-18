from datetime import datetime, timedelta, timezone

from polybot.capital_roi import (
    WindowSide,
    baseline_down_a036,
    infer_winner,
    thesis_a_race,
    thesis_b_scale_in,
)
from polybot.research import PriceSample


def _sample(
    slug: str,
    outcome: str,
    seconds: int,
    ask: float | None,
    bid: float | None,
    mid: float | None,
    start_ts: int,
) -> PriceSample:
    t0 = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    return PriceSample(
        sampled_at=t0 + timedelta(seconds=seconds),
        market_id="m",
        slug=slug,
        outcome=outcome,
        token_id=f"{outcome}-t",
        bid=bid,
        ask=ask,
        mid=mid,
        end_time=t0 + timedelta(seconds=300),
    )


def test_thesis_a_picks_first_side_in_window() -> None:
    start = 1_700_000_000
    slug = f"btc-updown-5m-{start}"
    up = WindowSide(
        samples=[
            _sample(slug, "UP", 5, 0.45, 0.44, 0.445, start),
            _sample(slug, "UP", 10, 0.29, 0.28, 0.285, start),  # hits first
            _sample(slug, "UP", 280, 0.95, 0.94, 0.945, start),
        ]
    )
    down = WindowSide(
        samples=[
            _sample(slug, "DOWN", 12, 0.28, 0.27, 0.275, start),  # later than UP
            _sample(slug, "DOWN", 280, 0.05, 0.04, 0.045, start),
        ]
    )
    windows = {slug: {"UP": up, "DOWN": down}}
    score = thesis_a_race(windows, stake=1.0, entry_ask=0.30, entry_window_seconds=60, exit_mode="hold")
    assert score.trades == 1
    # UP wins settlement -> payout ~ 1/0.29
    assert score.total_payout > 3.0


def test_thesis_b_scales_levels() -> None:
    start = 1_700_000_100
    slug = f"btc-updown-5m-{start}"
    down = WindowSide(
        samples=[
            _sample(slug, "DOWN", 1, 0.40, 0.39, 0.395, start),
            _sample(slug, "DOWN", 5, 0.34, 0.33, 0.335, start),  # 0.35
            _sample(slug, "DOWN", 10, 0.29, 0.28, 0.285, start),  # 0.30
            _sample(slug, "DOWN", 15, 0.24, 0.23, 0.235, start),  # 0.25
            _sample(slug, "DOWN", 280, 0.98, 0.97, 0.975, start),
        ]
    )
    up = WindowSide(
        samples=[
            _sample(slug, "UP", 1, 0.60, 0.59, 0.595, start),
            _sample(slug, "UP", 280, 0.02, 0.01, 0.015, start),
        ]
    )
    windows = {slug: {"UP": up, "DOWN": down}}
    score = thesis_b_scale_in(
        windows,
        unit_stake=1.0,
        levels=(0.35, 0.30, 0.25),
        entry_window_seconds=60,
        exit_mode="hold",
        both_sides=True,
    )
    assert score.trades == 3


def test_baseline_requires_time_left() -> None:
    start = 1_700_000_200
    slug = f"btc-updown-5m-{start}"
    down = WindowSide(
        samples=[
            # late dump: ask cheap but little time left
            _sample(slug, "DOWN", 290, 0.20, 0.19, 0.195, start),
            _sample(slug, "DOWN", 295, 0.10, 0.09, 0.095, start),
        ]
    )
    up = WindowSide(samples=[_sample(slug, "UP", 295, 0.90, 0.89, 0.895, start)])
    windows = {slug: {"UP": up, "DOWN": down}}
    score = baseline_down_a036(windows, stake=1.0, min_time_left=240)
    assert score.trades == 0
    assert infer_winner(windows[slug]) == "UP"
