from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any

import requests

from polybot.config import EnvConfig

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketPrice:
    token_id: str
    bid: float | None
    ask: float | None

    @property
    def current(self) -> float:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        if self.ask is not None:
            return self.ask
        if self.bid is not None:
            return self.bid
        raise ValueError(f"no bid or ask available for token {self.token_id}")


@dataclass(frozen=True)
class Position:
    token_id: str
    market_id: str | None
    outcome: str | None
    size: float


@dataclass(frozen=True)
class MarketMetadata:
    market_id: str
    slug: str | None
    question: str | None
    start_time: datetime | None
    end_time: datetime | None
    tokens_by_outcome: dict[str, str]
    closed: bool | None = None
    accepting_orders: bool | None = None


@dataclass(frozen=True)
class OrderResult:
    order_id: str | None
    status: str
    raw: dict[str, Any]


class PolymarketClient:
    def __init__(self, env: EnvConfig, timeout_seconds: float = 15.0) -> None:
        self._env = env
        self._timeout_seconds = timeout_seconds
        self._read_client: Any | None = None
        self._trade_client: Any | None = None
        self._metadata_cache: dict[str, MarketMetadata] = {}

    def get_price(self, token_id: str) -> MarketPrice:
        book = self._get_read_client().get_order_book(token_id)
        bids = _extract_levels(book, "bids")
        asks = _extract_levels(book, "asks")

        best_bid = max((level[0] for level in bids), default=None)
        best_ask = min((level[0] for level in asks), default=None)
        return MarketPrice(token_id=token_id, bid=best_bid, ask=best_ask)

    def get_market_metadata(self, market_id: str) -> MarketMetadata:
        if market_id in self._metadata_cache:
            return self._metadata_cache[market_id]

        response = requests.get(
            f"{self._env.clob_host.rstrip('/')}/markets/{market_id}",
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()
        metadata = _metadata_from_clob_market(raw)

        if metadata.end_time is None or not metadata.slug:
            gamma = self._get_gamma_market_by_condition_id(market_id)
            if gamma:
                metadata = _merge_gamma_metadata(metadata, gamma)

        self._metadata_cache[market_id] = metadata
        return metadata

    def resolve_market_slug(self, slug: str) -> MarketMetadata:
        response = requests.get(
            f"{self._env.gamma_api_url.rstrip('/')}/markets",
            params={"slug": slug},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        markets = response.json()
        if not isinstance(markets, list) or not markets:
            event_market = self._get_gamma_event_market_by_slug(slug)
            if event_market is None:
                raise ValueError(f"No Polymarket market found for slug {slug!r}")
            gamma_market = event_market
        else:
            gamma_market = markets[0]

        condition_id = str(gamma_market.get("conditionId") or gamma_market.get("condition_id") or "")
        if not condition_id:
            raise ValueError(f"Market {slug!r} did not include a conditionId")

        metadata = _merge_gamma_metadata(
            MarketMetadata(
                market_id=condition_id,
                slug=slug,
                question=None,
                start_time=None,
                end_time=None,
                tokens_by_outcome={},
            ),
            gamma_market,
        )
        if not metadata.tokens_by_outcome:
            clob_metadata = self.get_market_metadata(condition_id)
            metadata = _merge_gamma_metadata(clob_metadata, gamma_market)
        self._metadata_cache[metadata.market_id] = metadata
        return metadata

    def resolve_updown_market(
        self,
        asset: str,
        timeframe: str,
        now_ts: int | None = None,
    ) -> MarketMetadata:
        slug = updown_slug(asset=asset, timeframe=timeframe, now_ts=now_ts)
        return self.resolve_market_slug(slug)

    def find_active_updown_markets(
        self,
        asset: str,
        timeframe: str,
        limit: int = 200,
    ) -> list[MarketMetadata]:
        response = requests.get(
            f"{self._env.gamma_api_url.rstrip('/')}/events",
            params={"active": "true", "closed": "false", "limit": limit},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        events = response.json()
        if not isinstance(events, list):
            raise ValueError("Gamma events returned an unexpected response")

        matches: list[MarketMetadata] = []
        for event in events:
            if not isinstance(event, dict) or not _matches_updown_event(event, asset, timeframe):
                continue
            markets = event.get("markets")
            if not isinstance(markets, list):
                continue
            for raw_market in markets:
                if not isinstance(raw_market, dict):
                    continue
                market = dict(raw_market)
                market.setdefault("slug", event.get("slug"))
                market.setdefault("question", event.get("title") or event.get("question"))
                market.setdefault("startDate", event.get("startTime") or event.get("startDate"))
                market.setdefault("endDate", event.get("endDate"))
                metadata = _merge_gamma_metadata(
                    MarketMetadata(
                        market_id=str(market.get("conditionId") or ""),
                        slug=market.get("slug"),
                        question=None,
                        start_time=None,
                        end_time=None,
                        tokens_by_outcome={},
                    ),
                    market,
                )
                if metadata.market_id and metadata.tokens_by_outcome:
                    matches.append(metadata)

        return sorted(matches, key=lambda item: item.end_time or datetime.max.replace(tzinfo=timezone.utc))

    def get_position(self, user_address: str, token_id: str, market_id: str | None = None) -> Position:
        params: dict[str, Any] = {
            "user": user_address,
            "sizeThreshold": 0,
            "limit": 500,
        }
        if market_id:
            params["market"] = market_id

        response = requests.get(
            f"{self._env.data_api_url.rstrip('/')}/positions",
            params=params,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        positions = response.json()
        if not isinstance(positions, list):
            raise ValueError("positions API returned an unexpected response")

        for raw in positions:
            if str(raw.get("asset")) == token_id:
                return Position(
                    token_id=token_id,
                    market_id=raw.get("conditionId"),
                    outcome=raw.get("outcome"),
                    size=float(raw.get("size", 0)),
                )

        return Position(token_id=token_id, market_id=market_id, outcome=None, size=0)

    def buy(self, token_id: str, usd_amount: float, max_price: float, slippage: float = 0.01) -> OrderResult:
        price = min(round(max_price + slippage, 2), 0.99)
        size = usd_amount / price
        return self._post_limit_order(token_id=token_id, side="BUY", price=price, size=size)

    def sell(self, token_id: str, shares: float, min_price: float, slippage: float = 0.01) -> OrderResult:
        price = max(round(min_price - slippage, 2), 0.01)
        return self._post_limit_order(token_id=token_id, side="SELL", price=price, size=shares)

    def place_sell_limit(self, token_id: str, shares: float, price: float) -> OrderResult:
        return self._post_limit_order(token_id=token_id, side="SELL", price=round(price, 2), size=shares)

    def has_open_sell_limit(self, token_id: str, price: float, tolerance: float = 0.0001) -> bool:
        client = self._get_trade_client()
        sdk = _load_clob_sdk()
        orders = client.get_open_orders(sdk.OpenOrderParams(asset_id=token_id))
        for order in orders:
            side = str(_get_field(order, "side") or "").upper()
            order_price = _get_field(order, "price")
            if side == "SELL" and order_price is not None and abs(float(order_price) - price) <= tolerance:
                return True
        return False

    def _post_limit_order(self, token_id: str, side: str, price: float, size: float) -> OrderResult:
        client = self._get_trade_client()
        sdk = _load_clob_sdk()

        order = sdk.OrderArgs(
            token_id=token_id,
            price=price,
            side=getattr(sdk.Side, side),
            size=size,
        )
        response = client.create_and_post_order(
            order_args=order,
            options=sdk.PartialCreateOrderOptions(tick_size="0.01"),
            order_type=sdk.OrderType.GTC,
        )
        raw = response if isinstance(response, dict) else {"response": response}
        return OrderResult(
            order_id=_first_present(raw, "orderID", "order_id", "id"),
            status=str(_first_present(raw, "status", "success", default="submitted")),
            raw=raw,
        )

    def _get_read_client(self) -> Any:
        if self._read_client is None:
            sdk = _load_clob_sdk()
            self._read_client = sdk.ClobClient(host=self._env.clob_host, chain_id=self._env.chain_id)
        return self._read_client

    def _get_trade_client(self) -> Any:
        if self._trade_client is not None:
            return self._trade_client
        if not self._env.private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY is required for real trading")

        sdk = _load_clob_sdk()
        kwargs: dict[str, Any] = {
            "host": self._env.clob_host,
            "chain_id": self._env.chain_id,
            "key": self._env.private_key,
        }
        if self._env.funder_address:
            kwargs["funder"] = self._env.funder_address
        if self._env.signature_type is not None:
            kwargs["signature_type"] = self._env.signature_type

        if self._env.clob_api_key and self._env.clob_api_secret and self._env.clob_api_passphrase:
            creds = sdk.ApiCreds(
                api_key=self._env.clob_api_key,
                api_secret=self._env.clob_api_secret,
                api_passphrase=self._env.clob_api_passphrase,
            )
        else:
            bootstrap_client = sdk.ClobClient(**kwargs)
            creds = bootstrap_client.create_or_derive_api_key()

        kwargs["creds"] = creds
        self._trade_client = sdk.ClobClient(**kwargs)
        LOGGER.info("Initialized authenticated Polymarket CLOB client")
        return self._trade_client

    def _get_gamma_market_by_condition_id(self, market_id: str) -> dict[str, Any] | None:
        response = requests.get(
            f"{self._env.gamma_api_url.rstrip('/')}/markets",
            params={"condition_ids": market_id},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        markets = response.json()
        if isinstance(markets, list) and markets:
            return markets[0]
        return None

    def _get_gamma_event_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        response = requests.get(
            f"{self._env.gamma_api_url.rstrip('/')}/events",
            params={"slug": slug},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        events = response.json()
        if not isinstance(events, list) or not events:
            return None
        event = events[0]
        markets = event.get("markets")
        if not isinstance(markets, list) or not markets:
            return None
        market = dict(markets[0])
        market.setdefault("slug", event.get("slug") or slug)
        market.setdefault("question", event.get("title") or event.get("question"))
        market.setdefault("startDate", event.get("startTime") or event.get("startDate"))
        market.setdefault("endDate", event.get("endDate"))
        return market


def _load_clob_sdk() -> Any:
    try:
        import py_clob_client_v2 as sdk
    except ImportError as exc:
        raise RuntimeError("Install py-clob-client-v2 to use Polymarket trading APIs") from exc
    return sdk


def _extract_levels(book: Any, side: str) -> list[tuple[float, float]]:
    raw_levels = _get_field(book, side) or []
    levels = []
    for level in raw_levels:
        price = _get_field(level, "price")
        size = _get_field(level, "size")
        if price is None or size is None:
            continue
        levels.append((float(price), float(size)))
    return levels


def _metadata_from_clob_market(raw: dict[str, Any]) -> MarketMetadata:
    market_id = str(_first_present(raw, "condition_id", "conditionId", "market", default=""))
    tokens_by_outcome = {}
    for token in raw.get("tokens", []) or []:
        outcome = token.get("outcome")
        token_id = token.get("token_id") or token.get("tokenId")
        if outcome and token_id:
            tokens_by_outcome[str(outcome).upper()] = str(token_id)

    return MarketMetadata(
        market_id=market_id,
        slug=raw.get("slug"),
        question=raw.get("question"),
        start_time=_parse_datetime(_first_present(raw, "start_time", "startTime", "startDate")),
        end_time=_parse_datetime(_first_present(raw, "end_time", "endTime", "endDate")),
        tokens_by_outcome=tokens_by_outcome,
        closed=_parse_optional_bool(raw.get("closed")),
        accepting_orders=_parse_optional_bool(raw.get("acceptingOrders")),
    )


def _merge_gamma_metadata(metadata: MarketMetadata, gamma: dict[str, Any]) -> MarketMetadata:
    tokens_by_outcome = dict(metadata.tokens_by_outcome)
    outcomes = _parse_json_field(gamma.get("outcomes"))
    token_ids = _parse_json_field(gamma.get("clobTokenIds"))
    if isinstance(outcomes, list) and isinstance(token_ids, list):
        for outcome, token_id in zip(outcomes, token_ids, strict=False):
            tokens_by_outcome[str(outcome).upper()] = str(token_id)

    market_id = str(gamma.get("conditionId") or gamma.get("condition_id") or metadata.market_id)
    return MarketMetadata(
        market_id=market_id,
        slug=gamma.get("slug") or metadata.slug,
        question=gamma.get("question") or metadata.question,
        start_time=(
            _parse_datetime(gamma.get("eventStartTime") or gamma.get("startTime") or gamma.get("startDate"))
            or metadata.start_time
        ),
        end_time=_parse_datetime(gamma.get("endDate") or gamma.get("endDateIso")) or metadata.end_time,
        tokens_by_outcome=tokens_by_outcome,
        closed=_parse_optional_bool(gamma.get("closed")) if "closed" in gamma else metadata.closed,
        accepting_orders=(
            _parse_optional_bool(gamma.get("acceptingOrders"))
            if "acceptingOrders" in gamma
            else metadata.accepting_orders
        ),
    )


def _parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def timeframe_to_seconds(timeframe: str) -> int:
    unit = timeframe[-1]
    amount = int(timeframe[:-1])
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    raise ValueError(f"unsupported timeframe {timeframe!r}")


def updown_slug(asset: str, timeframe: str, now_ts: int | None = None) -> str:
    duration_seconds = timeframe_to_seconds(timeframe)
    now = int(time.time()) if now_ts is None else now_ts
    window_start = (now // duration_seconds) * duration_seconds
    return f"{asset.lower()}-updown-{timeframe}-{window_start}"


def _matches_updown_event(event: dict[str, Any], asset: str, timeframe: str) -> bool:
    haystack_parts = [
        event.get("slug"),
        event.get("ticker"),
        event.get("title"),
        event.get("seriesSlug"),
    ]
    for series in event.get("series") or []:
        if isinstance(series, dict):
            haystack_parts.extend([series.get("slug"), series.get("ticker"), series.get("recurrence")])
    haystack = " ".join(str(part).lower() for part in haystack_parts if part)
    asset = asset.lower()
    timeframe = timeframe.lower()
    asset_matches = asset in haystack or (asset == "btc" and "bitcoin" in haystack)
    timeframe_matches = timeframe in haystack or (timeframe == "1h" and "hour" in haystack)
    return asset_matches and "up" in haystack and "down" in haystack and timeframe_matches


def _get_field(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _first_present(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return default
