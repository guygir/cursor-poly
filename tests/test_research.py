from datetime import datetime, timezone

from polybot.research import PriceSample, analyze_double_targets


def sample(slug: str, outcome: str, second: int, bid: float, ask: float) -> PriceSample:
    return PriceSample(
        sampled_at=datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc),
        market_id=f"{slug}-condition",
        slug=slug,
        outcome=outcome,
        token_id=f"{slug}-{outcome}",
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2,
        end_time=None,
    )


def test_analyze_double_targets_counts_later_bid_success() -> None:
    samples = [
        sample("btc-updown-5m-100", "UP", 0, bid=0.09, ask=0.10),
        sample("btc-updown-5m-100", "UP", 1, bid=0.21, ask=0.22),
        sample("btc-updown-5m-200", "UP", 0, bid=0.09, ask=0.10),
        sample("btc-updown-5m-200", "UP", 1, bid=0.15, ask=0.16),
    ]

    result = analyze_double_targets(samples, thresholds=[0.10])[0]

    assert result.outcome == "UP"
    assert result.windows_seen == 2
    assert result.opportunities == 2
    assert result.successes == 1
    assert result.success_rate == 0.5


def test_analyze_double_targets_requires_target_after_entry() -> None:
    samples = [
        sample("btc-updown-5m-100", "DOWN", 0, bid=0.21, ask=0.22),
        sample("btc-updown-5m-100", "DOWN", 1, bid=0.09, ask=0.10),
    ]

    result = analyze_double_targets(samples, thresholds=[0.10])[0]

    assert result.opportunities == 1
    assert result.successes == 0
