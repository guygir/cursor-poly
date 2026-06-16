from __future__ import annotations

import argparse
from urllib.parse import urlparse

from polybot.config import load_env
from polybot.polymarket_client import PolymarketClient


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    env = load_env(args.env_file)
    client = PolymarketClient(env)

    slug = args.slug or (_slug_from_url(args.url) if args.url else None)
    if args.updown:
        asset, timeframe = _parse_updown(args.updown)
        metadata = client.resolve_updown_market(asset=asset, timeframe=timeframe)
        slug = metadata.slug
    elif slug:
        metadata = client.resolve_market_slug(slug)
    else:
        raise ValueError("expected slug, url, or updown series")

    print(f"# {metadata.question or slug}")
    print(f"# slug: {metadata.slug or slug}")
    print(f"# end_time: {metadata.end_time.isoformat() if metadata.end_time else 'unknown'}")
    print("markets:")
    for outcome, token_id in sorted(metadata.tokens_by_outcome.items()):
        print(f'  - id: "{metadata.market_id}"')
        print(f'    token_id: "{token_id}"')
        print(f'    outcome: "{outcome}"')
        print('    end_time: "' + (metadata.end_time.isoformat() if metadata.end_time else "") + '"')
        print("    min_price: 0.35")
        print("    max_price: 0.55")
        print("    buy_usd: 10")
        print("    avoid_buy_within_end_minutes: 1")
        print('    sell_shares: "all"')
        print("    enabled: true")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve Polymarket URL/slug to config values.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--slug", help="Polymarket market slug.")
    source.add_argument("--url", help="Polymarket market or event URL.")
    source.add_argument("--updown", help="Recurring Up/Down series as ASSET:TIMEFRAME, for example btc:5m.")
    parser.add_argument("--env-file", help="Path to a .env file.")
    return parser.parse_args(argv)


def _slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    pieces = [piece for piece in parsed.path.split("/") if piece]
    if not pieces:
        raise ValueError(f"Could not find slug in URL: {url}")
    return pieces[-1]


def _parse_updown(value: str) -> tuple[str, str]:
    pieces = value.split(":", maxsplit=1)
    if len(pieces) != 2 or not pieces[0] or not pieces[1]:
        raise ValueError("--updown must look like btc:5m")
    return pieces[0].lower(), pieces[1].lower()


if __name__ == "__main__":
    raise SystemExit(main())
