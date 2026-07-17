from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
import signal
import sys
import time

from polybot.config import (
    BotConfig,
    EnvConfig,
    FirstMinuteConfig,
    FirstMinuteRule,
    MarketConfig,
    RecurringMarketConfig,
    load_bot_config,
    load_env,
)
from polybot.polymarket_client import PolymarketClient, timeframe_to_seconds, updown_slug
from polybot.portfolio import Portfolio
from polybot.state import BotState, order_result_to_dict
from polybot.strategy import (
    Action,
    Decision,
    MarketSnapshot,
    decide,
    decide_first_minute_entry,
    decide_first_minute_exit,
)

LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.log_level)

    env = load_env(args.env_file)
    config = load_bot_config(args.config or env.config_path)

    runner = Runner(env=env, config=config, once=args.once)
    runner.run()
    return 0


class Runner:
    def __init__(self, env: EnvConfig, config: BotConfig, once: bool = False) -> None:
        self._env = env
        self._config = config
        self._once = once
        self._stop = False
        self._client = PolymarketClient(env)
        self._state = BotState(env.db_path)
        self._portfolio: Portfolio | None = None
        self._dry_run_holdings: dict[str, float] = {}
        self._dry_run_sell_limits: set[str] = set()
        if env.user_address:
            self._portfolio = Portfolio(self._client, env.user_address)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._request_stop)
        signal.signal(signal.SIGINT, self._request_stop)

        if self._config.mode == "first_minute":
            assert self._config.first_minute is not None
            self._run_first_minute(self._config.first_minute)
            return

        LOGGER.info(
            "Starting polybot: mode=classic markets=%s recurring_markets=%s interval=%sm dry_run=%s trading_enabled=%s",
            len(self._config.enabled_markets),
            len(self._config.enabled_recurring_markets),
            self._config.poll_interval_minutes,
            self._config.dry_run,
            self._env.enable_trading,
        )

        while not self._stop:
            started = time.monotonic()
            self.run_once()
            if self._once:
                return

            elapsed = time.monotonic() - started
            sleep_for = max((self._config.poll_interval_minutes * 60) - elapsed, 0)
            LOGGER.info("Sleeping %.1f seconds", sleep_for)
            self._sleep(sleep_for)

    def _run_first_minute(self, fm: FirstMinuteConfig) -> None:
        LOGGER.info(
            "Starting polybot: mode=first_minute asset=%s timeframe=%s poll=%ss entry_window=%ss "
            "min_time_left=%ss buy_usd=%s rules=%s dry_run=%s trading_enabled=%s",
            fm.asset,
            fm.timeframe,
            fm.poll_interval_seconds,
            fm.entry_window_seconds,
            fm.min_time_left_seconds,
            fm.buy_usd,
            [(r.outcome, r.entry_ask, r.exit_bid) for r in fm.enabled_rules],
            self._config.dry_run,
            self._env.enable_trading,
        )
        if self._config.dry_run and not self._env.user_address:
            LOGGER.info("Dry-run without wallet address: assuming zero live holdings")

        active_slug: str | None = None
        entry_taken = False
        active_rule: FirstMinuteRule | None = None
        active_token_id: str | None = None
        active_market_id: str | None = None

        while not self._stop:
            now_ts = int(time.time())
            duration = timeframe_to_seconds(fm.timeframe)
            window_start = (now_ts // duration) * duration
            seconds_into_window = now_ts - window_start
            time_left_seconds = float(duration - seconds_into_window)
            slug = updown_slug(fm.asset, fm.timeframe, now_ts=now_ts)

            if slug != active_slug:
                LOGGER.info(
                    "New window slug=%s start=%s seconds_into=%s",
                    slug,
                    window_start,
                    seconds_into_window,
                )
                active_slug = slug
                entry_taken = False
                active_rule = None
                active_token_id = None
                active_market_id = None
                self._dry_run_holdings.clear()
                self._dry_run_sell_limits.clear()

            try:
                metadata = self._client.resolve_updown_market(
                    asset=fm.asset,
                    timeframe=fm.timeframe,
                    now_ts=now_ts,
                )
            except Exception:
                LOGGER.exception("Failed resolving %s %s", fm.asset, fm.timeframe)
                if self._once:
                    return
                self._sleep(fm.poll_interval_seconds)
                continue

            if metadata.closed or metadata.accepting_orders is False:
                LOGGER.warning(
                    "Window not tradable slug=%s closed=%s accepting_orders=%s",
                    metadata.slug,
                    metadata.closed,
                    metadata.accepting_orders,
                )
                if self._once:
                    return
                self._sleep(_seconds_until_next_window(now_ts, duration))
                continue

            in_entry_window = seconds_into_window < fm.entry_window_seconds
            can_enter = (
                in_entry_window
                and not entry_taken
                and time_left_seconds >= fm.min_time_left_seconds
            )

            if can_enter:
                for rule in fm.enabled_rules:
                    token_id = metadata.tokens_by_outcome.get(rule.outcome.upper())
                    if token_id is None:
                        LOGGER.warning(
                            "Missing token for outcome=%s available=%s",
                            rule.outcome,
                            sorted(metadata.tokens_by_outcome),
                        )
                        continue
                    try:
                        price = self._client.get_price(token_id)
                    except Exception:
                        LOGGER.exception("Failed fetching price token=%s", token_id)
                        continue

                    decision = decide_first_minute_entry(
                        rule=rule,
                        market_id=metadata.market_id,
                        token_id=token_id,
                        best_ask=price.ask,
                        time_left_seconds=time_left_seconds,
                        min_time_left_seconds=fm.min_time_left_seconds,
                        buy_usd=fm.buy_usd,
                    )
                    LOGGER.info(
                        "entry_check slug=%s outcome=%s ask=%s bid=%s time_left=%.1fs action=%s reason=%s",
                        metadata.slug,
                        rule.outcome,
                        price.ask,
                        price.bid,
                        time_left_seconds,
                        decision.action.value,
                        decision.reason,
                    )
                    if decision.action != Action.BUY:
                        continue

                    self._handle_decision(decision, buy_price=price.ask, sell_price=price.bid)
                    entry_taken = True
                    active_rule = rule
                    active_token_id = token_id
                    active_market_id = metadata.market_id
                    if self._config.dry_run and decision.buy_usd is not None and price.ask:
                        # Approximate shares for dry-run exit management.
                        self._dry_run_holdings[token_id] = decision.buy_usd / price.ask
                    break

            # Manage exit if we entered (or still hold from a live fill).
            if active_rule and active_token_id and active_market_id:
                self._manage_first_minute_exit(
                    rule=active_rule,
                    market_id=active_market_id,
                    token_id=active_token_id,
                )
            elif not in_entry_window and not entry_taken:
                LOGGER.info(
                    "No entry this window slug=%s; waiting for next window (%.1fs)",
                    metadata.slug,
                    _seconds_until_next_window(now_ts, duration),
                )
                if self._once:
                    return
                self._sleep(_seconds_until_next_window(now_ts, duration))
                continue

            if self._once and (entry_taken or not in_entry_window):
                if active_rule and active_token_id and active_market_id:
                    self._manage_first_minute_exit(
                        rule=active_rule,
                        market_id=active_market_id,
                        token_id=active_token_id,
                    )
                return

            if not in_entry_window and entry_taken:
                # Keep managing sell limit until window ends.
                sleep_for = min(fm.poll_interval_seconds, max(time_left_seconds, 0.0))
                self._sleep(sleep_for)
                if time_left_seconds <= 0:
                    continue
                continue

            self._sleep(fm.poll_interval_seconds)

    def _manage_first_minute_exit(
        self,
        *,
        rule: FirstMinuteRule,
        market_id: str,
        token_id: str,
    ) -> None:
        holdings = self._held_shares(token_id=token_id, market_id=market_id)
        if holdings <= 0:
            return

        has_open_sell_limit = False
        if self._config.dry_run:
            has_open_sell_limit = token_id in self._dry_run_sell_limits
        elif self._env.enable_trading:
            try:
                has_open_sell_limit = self._client.has_open_sell_limit(
                    token_id=token_id,
                    price=rule.exit_bid,
                )
            except Exception:
                LOGGER.exception("Failed checking open sell limit token=%s", token_id)
                return

        try:
            price = self._client.get_price(token_id)
            mark = price.current
        except Exception:
            mark = rule.exit_bid

        decision = decide_first_minute_exit(
            rule=rule,
            market_id=market_id,
            token_id=token_id,
            holdings=holdings,
            has_open_sell_limit=has_open_sell_limit,
            mark_price=mark,
        )
        LOGGER.info(
            "exit_check outcome=%s holdings=%.4f exit_bid=%.2f action=%s reason=%s",
            rule.outcome,
            holdings,
            rule.exit_bid,
            decision.action.value,
            decision.reason,
        )
        self._handle_decision(decision, buy_price=None, sell_price=None)
        if (
            self._config.dry_run
            and decision.action == Action.PLACE_SELL_LIMIT
        ):
            self._dry_run_sell_limits.add(token_id)

    def run_once(self) -> None:
        for market in self._markets_for_current_cycle():
            try:
                price = self._client.get_price(market.token_id)
                holdings = self._held_shares(token_id=market.token_id, market_id=market.id)
                time_to_end = self._time_to_end_minutes(market.id, market.end_time)
                has_open_sell_limit = False
                if holdings > 0 and not self._config.dry_run and self._env.enable_trading:
                    has_open_sell_limit = self._client.has_open_sell_limit(
                        token_id=market.token_id,
                        price=market.max_price,
                    )
                snapshot = MarketSnapshot(
                    market_id=market.id,
                    token_id=market.token_id,
                    outcome=market.outcome,
                    current_price=price.current,
                    holdings=holdings,
                    time_to_end_minutes=time_to_end,
                    has_open_sell_limit=has_open_sell_limit,
                    best_ask=price.ask,
                    best_bid=price.bid,
                )
                decision = decide(market, snapshot)
                LOGGER.info(
                    "market=%s outcome=%s price=%.4f bid=%s ask=%s holdings=%.4f time_to_end=%s action=%s reason=%s",
                    market.id,
                    market.outcome,
                    snapshot.current_price,
                    price.bid,
                    price.ask,
                    holdings,
                    f"{time_to_end:.1f}m" if time_to_end is not None else "unknown",
                    decision.action.value,
                    decision.reason,
                )
                self._handle_decision(decision, buy_price=price.ask, sell_price=price.bid)
            except Exception:
                LOGGER.exception("Failed processing market=%s token=%s", market.id, market.token_id)

    def _handle_decision(
        self,
        decision: Decision,
        buy_price: float | None,
        sell_price: float | None,
    ) -> None:
        if not decision.should_trade:
            self._state.record_decision(decision, dry_run=self._config.dry_run)
            return

        if self._config.dry_run:
            LOGGER.info("DRY RUN: would %s token=%s", decision.action.value, decision.token_id)
            self._state.record_decision(decision, dry_run=True)
            return

        if not self._env.enable_trading:
            raise RuntimeError("Refusing real order because POLYBOT_ENABLE_TRADING is not true")

        if not self._env.private_key:
            raise RuntimeError("Refusing real order because POLYMARKET_PRIVATE_KEY is not set")

        if self._state.has_recent_trade_action(decision.token_id, decision.action):
            LOGGER.warning(
                "Skipping duplicate recent %s for token=%s",
                decision.action.value,
                decision.token_id,
            )
            self._state.record_decision(decision, dry_run=False, order_result={"skipped": "recent duplicate"})
            return

        if decision.action == Action.BUY:
            if decision.buy_usd is None:
                raise ValueError("buy decision missing buy_usd")
            order_result = self._client.buy(
                token_id=decision.token_id,
                usd_amount=decision.buy_usd,
                max_price=buy_price or decision.price,
            )
        elif decision.action == Action.SELL:
            if decision.sell_shares is None:
                raise ValueError("sell decision missing sell_shares")
            order_result = self._client.sell(
                token_id=decision.token_id,
                shares=decision.sell_shares,
                min_price=sell_price or decision.price,
            )
        elif decision.action == Action.PLACE_SELL_LIMIT:
            if decision.sell_shares is None or decision.limit_price is None:
                raise ValueError("sell-limit decision missing shares or limit_price")
            order_result = self._client.place_sell_limit(
                token_id=decision.token_id,
                shares=decision.sell_shares,
                price=decision.limit_price,
            )
        else:
            raise ValueError(f"unsupported action {decision.action}")

        LOGGER.info(
            "Submitted %s token=%s status=%s order_id=%s",
            decision.action.value,
            decision.token_id,
            order_result.status,
            order_result.order_id,
        )
        self._state.record_decision(
            decision,
            dry_run=False,
            order_result=order_result_to_dict(order_result),
        )

    def _held_shares(self, token_id: str, market_id: str) -> float:
        if self._config.dry_run:
            return self._dry_run_holdings.get(token_id, 0.0)
        if self._portfolio is None:
            raise ValueError("POLYMARKET_USER_ADDRESS or POLYMARKET_FUNDER_ADDRESS is required")
        return self._portfolio.held_shares(token_id=token_id, market_id=market_id)

    def _markets_for_current_cycle(self) -> tuple[MarketConfig, ...]:
        markets = list(self._config.enabled_markets)
        for recurring in self._config.enabled_recurring_markets:
            try:
                markets.append(self._resolve_recurring_market(recurring))
            except Exception:
                LOGGER.exception(
                    "Failed resolving recurring market asset=%s timeframe=%s",
                    recurring.asset,
                    recurring.timeframe,
                )
        return tuple(markets)

    def _resolve_recurring_market(self, recurring: RecurringMarketConfig) -> MarketConfig:
        metadata = self._client.resolve_updown_market(
            asset=recurring.asset,
            timeframe=recurring.timeframe,
        )
        if metadata.closed:
            raise ValueError(f"Resolved recurring market {metadata.slug} but it is closed")
        if metadata.accepting_orders is False:
            raise ValueError(f"Resolved recurring market {metadata.slug} but it is not accepting orders")
        token_id = metadata.tokens_by_outcome.get(recurring.outcome.upper())
        if token_id is None:
            raise ValueError(
                f"Resolved {metadata.slug} but did not find outcome {recurring.outcome!r}; "
                f"available outcomes: {sorted(metadata.tokens_by_outcome)}"
            )
        return MarketConfig(
            id=metadata.market_id,
            token_id=token_id,
            outcome=recurring.outcome.upper(),
            min_price=recurring.min_price,
            max_price=recurring.max_price,
            buy_usd=recurring.buy_usd,
            end_time=metadata.end_time,
            avoid_buy_within_end_minutes=recurring.avoid_buy_within_end_minutes,
            sell_shares=recurring.sell_shares,
            enabled=recurring.enabled,
        )

    def _time_to_end_minutes(self, market_id: str, configured_end_time: datetime | None) -> float | None:
        end_time = configured_end_time
        if end_time is None:
            metadata = self._client.get_market_metadata(market_id)
            end_time = metadata.end_time
        if end_time is None:
            return None
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        return (end_time - datetime.now(timezone.utc)).total_seconds() / 60

    def _request_stop(self, signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s, stopping after current work", signum)
        self._stop = True

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(seconds, 0)
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.25, deadline - time.monotonic()))


def _seconds_until_next_window(now_ts: int, duration: int) -> float:
    return float(duration - (now_ts % duration))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Polymarket threshold bot.")
    parser.add_argument("--config", help="Path to config YAML. Defaults to POLYBOT_CONFIG_PATH.")
    parser.add_argument("--env-file", help="Path to a .env file.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle / window pass and exit.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level.")
    return parser.parse_args(argv)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
