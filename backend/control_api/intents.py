"""Append-only control intent ledger (separate ``control_intents.sqlite3``).

The ledger is the audit source of truth for P2 control intents:

- every reservation is one immutable row in ``intents`` (content fields never
  change after insert; only ``status``/``reason_code``/``result_run_id``/
  ``applied_sequence``/``updated_at`` advance through the lifecycle);
- every lifecycle transition is additionally appended to ``intent_events``
  so corrections never erase the original reservation;
- ``run_priorities`` holds dispatcher priority metadata only; it never
  rewrites Loop checkpoints.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from common.checkpoint import utc_now

INTENT_STATUSES = ("pending", "accepted", "applied", "rejected", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    requester TEXT NOT NULL,
    expected_sequence INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','applied','rejected','failed')),
    reason_code TEXT,
    result_run_id TEXT,
    applied_sequence INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS intents_run ON intents(run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS intent_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS intent_events_intent ON intent_events(intent_id, event_id);

CREATE TABLE IF NOT EXISTS run_priorities (
    run_id TEXT PRIMARY KEY,
    priority INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _row_to_receipt(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "intent_id": str(row["intent_id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "run_id": str(row["run_id"]),
        "action": str(row["action"]),
        "arguments": json.loads(str(row["arguments_json"])),
        "requester": str(row["requester"]),
        "expected_sequence": int(row["expected_sequence"]),
        "status": str(row["status"]),
        "reason_code": None if row["reason_code"] is None else str(row["reason_code"]),
        "result_run_id": None if row["result_run_id"] is None else str(row["result_run_id"]),
        "applied_sequence": (
            None if row["applied_sequence"] is None else int(row["applied_sequence"])
        ),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class SQLiteIntentLedger:
    """Append-only audited intent store on its own SQLite file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def reserve(
        self,
        *,
        intent_id: str,
        idempotency_key: str,
        run_id: str,
        action: str,
        arguments: dict[str, Any],
        requester: str,
        expected_sequence: int,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a pending intent. Returns (receipt, is_new).

        A concurrent or repeated reservation with the same idempotency key
        returns the original receipt with is_new=False.
        """
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO intents (
                    intent_id, idempotency_key, run_id, action, arguments_json,
                    requester, expected_sequence, status, reason_code,
                    result_run_id, applied_sequence, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?)
                """,
                (
                    intent_id,
                    idempotency_key,
                    run_id,
                    action,
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    requester,
                    expected_sequence,
                    now,
                    now,
                ),
            )
            is_new = cursor.rowcount == 1
            if is_new:
                connection.execute(
                    """
                    INSERT INTO intent_events (intent_id, from_status, to_status,
                                               reason_code, created_at)
                    VALUES (?, NULL, 'pending', NULL, ?)
                    """,
                    (intent_id, now),
                )
            row = connection.execute(
                "SELECT * FROM intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive; insert/select are atomic
            raise RuntimeError("intent reservation vanished")
        return _row_to_receipt(row), is_new

    def get(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        return None if row is None else _row_to_receipt(row)

    def get_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else _row_to_receipt(row)

    def list_for_run(self, run_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM intents WHERE run_id = ?
                ORDER BY created_at DESC, intent_id DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [_row_to_receipt(row) for row in rows]

    def list_resumable(self) -> list[dict[str, Any]]:
        """Intents whose lifecycle was interrupted before a terminal state."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM intents WHERE status IN ('pending', 'accepted')
                ORDER BY created_at ASC, intent_id ASC
                """,
            ).fetchall()
        return [_row_to_receipt(row) for row in rows]

    def transition(
        self,
        intent_id: str,
        *,
        from_statuses: tuple[str, ...],
        to_status: str,
        reason_code: str | None = None,
        result_run_id: str | None = None,
        applied_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Advance the lifecycle and append the audit event atomically."""
        if to_status not in INTENT_STATUSES:
            raise ValueError(f"Unknown intent status: {to_status}")
        now = utc_now()
        placeholders = ", ".join("?" for _ in from_statuses)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if current is None or str(current["status"]) not in from_statuses:
                raise ValueError(f"Intent {intent_id} not in {from_statuses}")
            from_status = str(current["status"])
            connection.execute(
                f"""
                UPDATE intents
                SET status = ?, reason_code = ?, result_run_id = ?,
                    applied_sequence = ?, updated_at = ?
                WHERE intent_id = ? AND status IN ({placeholders})
                """,
                (
                    to_status,
                    reason_code,
                    result_run_id,
                    applied_sequence,
                    now,
                    intent_id,
                    *from_statuses,
                ),
            )
            connection.execute(
                """
                INSERT INTO intent_events (intent_id, from_status, to_status,
                                           reason_code, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (intent_id, from_status, to_status, reason_code, now),
            )
            row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise KeyError(f"Unknown intent: {intent_id}")
        return _row_to_receipt(row)

    def apply_priority_and_receipt(
        self,
        *,
        intent_id: str,
        run_id: str,
        priority: int,
        applied_sequence: int,
    ) -> dict[str, Any]:
        """Atomically upsert the priority and mark the intent applied.

        Both writes commit in one control-ledger transaction, so a crash can
        never leave the priority set without an applied receipt, or an
        applied receipt without the priority. Callers must hold the Loop
        database sequence guard for the linearization point.
        """
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if current is None or str(current["status"]) != "accepted":
                raise ValueError(f"Intent {intent_id} not accepted")
            connection.execute(
                """
                INSERT INTO run_priorities (run_id, priority, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET priority = excluded.priority,
                    updated_at = excluded.updated_at
                """,
                (run_id, priority, now),
            )
            connection.execute(
                """
                UPDATE intents
                SET status = 'applied', applied_sequence = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (applied_sequence, now, intent_id),
            )
            connection.execute(
                """
                INSERT INTO intent_events (intent_id, from_status, to_status,
                                           reason_code, created_at)
                VALUES (?, 'accepted', 'applied', NULL, ?)
                """,
                (intent_id, now),
            )
            row = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise KeyError(f"Unknown intent: {intent_id}")
        return _row_to_receipt(row)

    def get_priority(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT priority FROM run_priorities WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return 0 if row is None else int(row["priority"])
