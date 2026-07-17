from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
import yaml


SellShares = Literal["all"]
BotMode = Literal["classic", "first_minute"]


@dataclass(frozen=True)
class MarketConfig:
    id: str
    token_id: str
    outcome: str
    min_price: float
    max_price: float
    buy_usd: float
    end_time: datetime | None = None
    avoid_buy_within_end_minutes: float | None = None
    sell_shares: SellShares = "all"
    enabled: bool = True


@dataclass(frozen=True)
class RecurringMarketConfig:
    asset: str
    timeframe: str
    outcome: str
    min_price: float
    max_price: float
    buy_usd: float
    avoid_buy_within_end_minutes: float | None = None
    sell_shares: SellShares = "all"
    enabled: bool = True


@dataclass(frozen=True)
class FirstMinuteRule:
    outcome: str
    entry_ask: float
    exit_bid: float
    enabled: bool = True
    note: str | None = None


@dataclass(frozen=True)
class FirstMinuteConfig:
    asset: str
    timeframe: str
    poll_interval_seconds: float
    entry_window_seconds: float
    min_time_left_seconds: float
    buy_usd: float
    rules: tuple[FirstMinuteRule, ...]

    @property
    def enabled_rules(self) -> tuple[FirstMinuteRule, ...]:
        return tuple(rule for rule in self.rules if rule.enabled)


@dataclass(frozen=True)
class BotConfig:
    poll_interval_minutes: float
    dry_run: bool
    markets: tuple[MarketConfig, ...]
    recurring_markets: tuple[RecurringMarketConfig, ...] = ()
    mode: BotMode = "classic"
    first_minute: FirstMinuteConfig | None = None

    @property
    def enabled_markets(self) -> tuple[MarketConfig, ...]:
        return tuple(market for market in self.markets if market.enabled)

    @property
    def enabled_recurring_markets(self) -> tuple[RecurringMarketConfig, ...]:
        return tuple(market for market in self.recurring_markets if market.enabled)


@dataclass(frozen=True)
class EnvConfig:
    config_path: Path
    db_path: Path
    enable_trading: bool
    user_address: str | None
    private_key: str | None
    funder_address: str | None
    signature_type: int | None
    clob_api_key: str | None
    clob_api_secret: str | None
    clob_api_passphrase: str | None
    chain_id: int
    clob_host: str
    gamma_api_url: str
    data_api_url: str


def load_env(dotenv_path: str | Path | None = None) -> EnvConfig:
    load_dotenv(dotenv_path=dotenv_path)

    return EnvConfig(
        config_path=Path(os.getenv("POLYBOT_CONFIG_PATH", "config.yaml")),
        db_path=Path(os.getenv("POLYBOT_DB_PATH", "data/polybot.sqlite3")),
        enable_trading=_parse_bool(os.getenv("POLYBOT_ENABLE_TRADING", "false")),
        user_address=_empty_to_none(os.getenv("POLYMARKET_USER_ADDRESS"))
        or _empty_to_none(os.getenv("POLYMARKET_FUNDER_ADDRESS")),
        private_key=_empty_to_none(os.getenv("POLYMARKET_PRIVATE_KEY")),
        funder_address=_empty_to_none(os.getenv("POLYMARKET_FUNDER_ADDRESS")),
        signature_type=_optional_int(os.getenv("POLYMARKET_SIGNATURE_TYPE")),
        clob_api_key=_empty_to_none(os.getenv("POLYMARKET_CLOB_API_KEY")),
        clob_api_secret=_empty_to_none(os.getenv("POLYMARKET_CLOB_API_SECRET")),
        clob_api_passphrase=_empty_to_none(os.getenv("POLYMARKET_CLOB_API_PASSPHRASE")),
        chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
        clob_host=os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
        gamma_api_url=os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com"),
        data_api_url=os.getenv("POLYMARKET_DATA_API", "https://data-api.polymarket.com"),
    )


def load_bot_config(path: str | Path) -> BotConfig:
    with Path(path).open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    return bot_config_from_mapping(raw)


def bot_config_from_mapping(raw: Any) -> BotConfig:
    if not isinstance(raw, dict):
        raise ValueError("config must be a mapping")

    mode = str(raw.get("mode", "classic")).strip().lower()
    if mode not in {"classic", "first_minute"}:
        raise ValueError("mode must be 'classic' or 'first_minute'")

    poll_interval_minutes = float(raw.get("poll_interval_minutes", 5))
    if poll_interval_minutes <= 0:
        raise ValueError("poll_interval_minutes must be greater than 0")

    dry_run = _parse_bool(raw.get("dry_run", True))
    raw_markets = raw.get("markets", [])
    if raw_markets is None:
        raw_markets = []
    if not isinstance(raw_markets, list):
        raise ValueError("markets must be a list")

    raw_recurring_markets = raw.get("recurring_markets", [])
    if raw_recurring_markets is None:
        raw_recurring_markets = []
    if not isinstance(raw_recurring_markets, list):
        raise ValueError("recurring_markets must be a list")

    first_minute = None
    if mode == "first_minute":
        first_minute = _first_minute_from_mapping(raw.get("first_minute"))
    elif not raw_markets and not raw_recurring_markets:
        raise ValueError("markets or recurring_markets must be a non-empty list")

    markets = tuple(_market_config_from_mapping(item, index) for index, item in enumerate(raw_markets))
    recurring_markets = tuple(
        _recurring_market_config_from_mapping(item, index)
        for index, item in enumerate(raw_recurring_markets)
    )
    return BotConfig(
        poll_interval_minutes=poll_interval_minutes,
        dry_run=dry_run,
        markets=markets,
        recurring_markets=recurring_markets,
        mode=mode,  # type: ignore[arg-type]
        first_minute=first_minute,
    )


def _first_minute_from_mapping(raw: Any) -> FirstMinuteConfig:
    if not isinstance(raw, dict):
        raise ValueError("first_minute must be a mapping when mode is first_minute")

    poll_interval_seconds = float(raw.get("poll_interval_seconds", 1))
    entry_window_seconds = float(raw.get("entry_window_seconds", 60))
    min_time_left_seconds = float(raw.get("min_time_left_seconds", 240))
    buy_usd = float(raw.get("buy_usd", 10))
    if poll_interval_seconds <= 0:
        raise ValueError("first_minute.poll_interval_seconds must be greater than 0")
    if entry_window_seconds <= 0:
        raise ValueError("first_minute.entry_window_seconds must be greater than 0")
    if min_time_left_seconds < 0:
        raise ValueError("first_minute.min_time_left_seconds must be zero or greater")
    if buy_usd <= 0:
        raise ValueError("first_minute.buy_usd must be greater than 0")

    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("first_minute.rules must be a non-empty list")

    rules = tuple(_first_minute_rule_from_mapping(item, index) for index, item in enumerate(raw_rules))
    if not any(rule.enabled for rule in rules):
        raise ValueError("first_minute.rules must include at least one enabled rule")

    asset = raw.get("asset")
    timeframe = raw.get("timeframe")
    if not isinstance(asset, str) or not asset.strip():
        raise ValueError("first_minute.asset must be a non-empty string")
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("first_minute.timeframe must be a non-empty string")
    _timeframe_to_seconds(timeframe.strip(), 0)

    return FirstMinuteConfig(
        asset=asset.strip(),
        timeframe=timeframe.strip(),
        poll_interval_seconds=poll_interval_seconds,
        entry_window_seconds=entry_window_seconds,
        min_time_left_seconds=min_time_left_seconds,
        buy_usd=buy_usd,
        rules=rules,
    )


def _first_minute_rule_from_mapping(raw: Any, index: int) -> FirstMinuteRule:
    if not isinstance(raw, dict):
        raise ValueError(f"first_minute.rules[{index}] must be a mapping")

    enabled = _parse_bool(raw.get("enabled", True))
    outcome = _required_str(raw, "outcome", index, prefix="first_minute.rules").upper()
    note = raw.get("note")
    if note is not None:
        note = str(note)

    if not enabled:
        entry_ask = float(raw.get("entry_ask", 0.5))
        exit_bid = float(raw.get("exit_bid", 0.99))
        if "exit_multiplier" in raw and "entry_ask" in raw:
            exit_bid = float(raw["entry_ask"]) * float(raw["exit_multiplier"])
        return FirstMinuteRule(
            outcome=outcome,
            entry_ask=entry_ask,
            exit_bid=min(exit_bid, 0.99),
            enabled=False,
            note=note,
        )

    entry_ask = _required_float(raw, "entry_ask", index, prefix="first_minute.rules")
    if "exit_bid" in raw:
        exit_bid = float(raw["exit_bid"])
    elif "exit_multiplier" in raw:
        exit_bid = entry_ask * float(raw["exit_multiplier"])
    else:
        raise ValueError(
            f"first_minute.rules[{index}] requires exit_bid or exit_multiplier"
        )

    exit_bid = min(round(exit_bid, 2), 0.99)
    if not 0 < entry_ask < 1:
        raise ValueError(f"first_minute.rules[{index}].entry_ask must be between 0 and 1")
    if not 0 < exit_bid < 1:
        raise ValueError(f"first_minute.rules[{index}].exit_bid must be between 0 and 1")
    if entry_ask >= exit_bid:
        raise ValueError(f"first_minute.rules[{index}].entry_ask must be less than exit_bid")

    return FirstMinuteRule(
        outcome=outcome,
        entry_ask=entry_ask,
        exit_bid=exit_bid,
        enabled=True,
        note=note,
    )


def _market_config_from_mapping(raw: Any, index: int) -> MarketConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"markets[{index}] must be a mapping")

    market = MarketConfig(
        id=_required_str(raw, "id", index),
        token_id=_required_str(raw, "token_id", index),
        outcome=_required_str(raw, "outcome", index),
        min_price=_required_float(raw, "min_price", index),
        max_price=_required_float(raw, "max_price", index),
        buy_usd=_required_float(raw, "buy_usd", index),
        end_time=_optional_datetime(raw.get("end_time"), index),
        avoid_buy_within_end_minutes=_optional_float(
            raw.get("avoid_buy_within_end_minutes"),
            index,
            "avoid_buy_within_end_minutes",
        ),
        sell_shares=raw.get("sell_shares", "all"),
        enabled=_parse_bool(raw.get("enabled", True)),
    )
    _validate_market(market, index)
    return market


def _recurring_market_config_from_mapping(raw: Any, index: int) -> RecurringMarketConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"recurring_markets[{index}] must be a mapping")

    market = RecurringMarketConfig(
        asset=_required_str(raw, "asset", index, prefix="recurring_markets"),
        timeframe=_required_str(raw, "timeframe", index, prefix="recurring_markets"),
        outcome=_required_str(raw, "outcome", index, prefix="recurring_markets").upper(),
        min_price=_required_float(raw, "min_price", index, prefix="recurring_markets"),
        max_price=_required_float(raw, "max_price", index, prefix="recurring_markets"),
        buy_usd=_required_float(raw, "buy_usd", index, prefix="recurring_markets"),
        avoid_buy_within_end_minutes=_optional_float(
            raw.get("avoid_buy_within_end_minutes"),
            index,
            "avoid_buy_within_end_minutes",
            prefix="recurring_markets",
        ),
        sell_shares=raw.get("sell_shares", "all"),
        enabled=_parse_bool(raw.get("enabled", True)),
    )
    _validate_recurring_market(market, index)
    return market


def _validate_market(market: MarketConfig, index: int) -> None:
    if not 0 < market.min_price < 1:
        raise ValueError(f"markets[{index}].min_price must be between 0 and 1")
    if not 0 < market.max_price < 1:
        raise ValueError(f"markets[{index}].max_price must be between 0 and 1")
    if market.min_price >= market.max_price:
        raise ValueError(f"markets[{index}].min_price must be less than max_price")
    if market.buy_usd <= 0:
        raise ValueError(f"markets[{index}].buy_usd must be greater than 0")
    if market.avoid_buy_within_end_minutes is not None and market.avoid_buy_within_end_minutes < 0:
        raise ValueError(f"markets[{index}].avoid_buy_within_end_minutes must be zero or greater")
    if market.sell_shares != "all":
        raise ValueError(f"markets[{index}].sell_shares currently only supports 'all'")


def _validate_recurring_market(market: RecurringMarketConfig, index: int) -> None:
    if not 0 < market.min_price < 1:
        raise ValueError(f"recurring_markets[{index}].min_price must be between 0 and 1")
    if not 0 < market.max_price < 1:
        raise ValueError(f"recurring_markets[{index}].max_price must be between 0 and 1")
    if market.min_price >= market.max_price:
        raise ValueError(f"recurring_markets[{index}].min_price must be less than max_price")
    if market.buy_usd <= 0:
        raise ValueError(f"recurring_markets[{index}].buy_usd must be greater than 0")
    if market.avoid_buy_within_end_minutes is not None and market.avoid_buy_within_end_minutes < 0:
        raise ValueError(f"recurring_markets[{index}].avoid_buy_within_end_minutes must be zero or greater")
    if market.sell_shares != "all":
        raise ValueError(f"recurring_markets[{index}].sell_shares currently only supports 'all'")
    _timeframe_to_seconds(market.timeframe, index)


def _required_str(raw: dict[str, Any], key: str, index: int, prefix: str = "markets") -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{prefix}[{index}].{key} must be a non-empty string")
    return value.strip()


def _required_float(raw: dict[str, Any], key: str, index: int, prefix: str = "markets") -> float:
    if key not in raw:
        raise ValueError(f"{prefix}[{index}].{key} is required")
    try:
        return float(raw[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{prefix}[{index}].{key} must be numeric") from exc


def _optional_float(value: Any, index: int, key: str, prefix: str = "markets") -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{prefix}[{index}].{key} must be numeric") from exc


def _optional_datetime(value: Any, index: int) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"markets[{index}].end_time must be ISO-8601") from exc
    raise ValueError(f"markets[{index}].end_time must be ISO-8601")


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"expected boolean value, got {value!r}")


def _empty_to_none(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def _timeframe_to_seconds(timeframe: str, index: int) -> int:
    unit = timeframe[-1]
    raw_amount = timeframe[:-1]
    if unit not in {"m", "h"} or not raw_amount.isdigit():
        raise ValueError(f"recurring_markets[{index}].timeframe must look like '5m' or '1h'")
    amount = int(raw_amount)
    seconds = amount * (60 if unit == "m" else 3600)
    if seconds <= 0:
        raise ValueError(f"recurring_markets[{index}].timeframe must be greater than zero")
    return seconds
