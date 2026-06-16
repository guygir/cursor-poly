from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
import signal
import sys
import time

from polybot.config import BotConfig, EnvConfig, MarketConfig, RecurringMarketConfig, load_bot_config, load_env
from polybot.polymarket_client import PolymarketClient
from polybot.portfolio import Portfolio
from polybot.state import BotState, order_result_to_dict
from polybot.strategy import Action, Decision, MarketSnapshot, decide

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
        self._portfolio = Portfolio(self._client, self._require_user_address())

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._request_stop)
        signal.signal(signal.SIGINT, self._request_stop)

        LOGGER.info(
            "Starting polybot: markets=%s recurring_markets=%s interval=%sm dry_run=%s trading_enabled=%s",
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

    def run_once(self) -> None:
        for market in self._markets_for_current_cycle():
            try:
                price = self._client.get_price(market.token_id)
                holdings = self._portfolio.held_shares(
                    token_id=market.token_id,
                    market_id=market.id,
                )
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

    def _require_user_address(self) -> str:
        if not self._env.user_address:
            raise ValueError("POLYMARKET_USER_ADDRESS or POLYMARKET_FUNDER_ADDRESS is required")
        return self._env.user_address

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
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(1, deadline - time.monotonic()))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Polymarket threshold bot.")
    parser.add_argument("--config", help="Path to config YAML. Defaults to POLYBOT_CONFIG_PATH.")
    parser.add_argument("--env-file", help="Path to a .env file.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
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
