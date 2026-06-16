from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Any

from polybot.strategy import Action, Decision


class BotState:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def record_decision(
        self,
        decision: Decision,
        dry_run: bool,
        order_result: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO decisions (
                    created_at,
                    market_id,
                    token_id,
                    outcome,
                    action,
                    reason,
                    price,
                    dry_run,
                    order_result
                )
                VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.market_id,
                    decision.token_id,
                    decision.outcome,
                    decision.action.value,
                    decision.reason,
                    decision.price,
                    int(dry_run),
                    json.dumps(order_result) if order_result is not None else None,
                ),
            )

    def has_recent_trade_action(self, token_id: str, action: Action) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM decisions
                WHERE token_id = ?
                  AND action = ?
                  AND dry_run = 0
                  AND created_at >= datetime('now', '-2 minutes')
                LIMIT 1
                """,
                (token_id, action.value),
            ).fetchone()
        return row is not None

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    price REAL NOT NULL,
                    dry_run INTEGER NOT NULL,
                    order_result TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_decisions_token_action_created
                ON decisions (token_id, action, created_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)


def order_result_to_dict(order_result: Any) -> dict[str, Any]:
    if order_result is None:
        return {}
    if hasattr(order_result, "__dataclass_fields__"):
        return asdict(order_result)
    if isinstance(order_result, dict):
        return order_result
    return {"result": str(order_result)}
