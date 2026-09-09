"""Append-only execution ledger for P3D (frozen contract §2/§5/§8).

Tables: ``meta`` (activation epoch, set once), ``executions`` (one
authoritative reservation per review decision), ``execution_events``
(append-only audit), and ``budget_reservations`` (pre-action reservation,
post-action settlement). Rows are never deleted; status columns are the
mutable current-state index maintained in the same transaction as the
event appends, mirroring the shadow dispatcher ledger conventions.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from execution_dispatcher.candidates import parse_instant

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    loopspec_id TEXT NOT NULL,
    loopspec_version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    source_sequence INTEGER NOT NULL,
    activation_epoch TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS executions_run ON executions(run_id, status);
CREATE TABLE IF NOT EXISTS execution_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_events_execution
    ON execution_events(execution_id, event_id);
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    node TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    reserved_tool_calls INTEGER NOT NULL,
    actual_tokens INTEGER,
    actual_tool_calls INTEGER,
    status TEXT NOT NULL CHECK(status IN ('open', 'settled', 'voided')),
    created_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE INDEX IF NOT EXISTS budget_reservations_execution
    ON budget_reservations(execution_id, status);
"""

DEFAULT_LEASE_SECONDS = 300.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _run_with_retry(  # noqa: UP047 - PEP 695 syntax requires Python 3.12+
    operation: Callable[[], T], *, attempts: int = 12, base_delay: float = 0.02
) -> T:
    """Bounded exponential backoff for transient SQLite lock contention."""
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


class ExecutionLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        activation_epoch: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = float(lease_seconds)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        existing = self._meta_get("activation_epoch")
        if activation_epoch is not None:
            # epoch isolation is a security boundary: real instants only
            parse_instant(activation_epoch)
            if existing is not None and existing != activation_epoch:
                raise ValueError(
                    "activation_epoch mismatch: ledger already frozen at "
                    f"{existing}, got {activation_epoch}"
                )
            if existing is None:
                self._meta_set("activation_epoch", activation_epoch)
                existing = activation_epoch
        if existing is None:
            raise ValueError("activation_epoch is required for a fresh execution ledger")
        self._epoch = existing

    @property
    def activation_epoch(self) -> str:
        return self._epoch

    def close(self) -> None:
        self._connection.close()

    def _meta_get(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)", (key, value)
        )
        self._connection.commit()

    # -- executions ---------------------------------------------------------

    def reserve_execution(
        self,
        *,
        decision_id: str,
        idempotency_key: str,
        proposal_id: str,
        run_id: str,
        fragment_id: str,
        loopspec_id: str,
        loopspec_version: str,
        fingerprint: str,
        source_sequence: int,
    ) -> tuple[sqlite3.Row, bool]:
        """Single-winner reservation: one authoritative row per decision_id."""
        now = _utc_now()

        def _once() -> tuple[sqlite3.Row, bool]:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM executions WHERE decision_id = ?", (decision_id,)
                ).fetchone()
                if existing is not None:
                    self._connection.commit()
                    if str(existing["idempotency_key"]) != idempotency_key:
                        raise ValueError(
                            "idempotency key mismatch for existing execution reservation"
                        )
                    return existing, False
                execution_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"p3d-execution:{decision_id}")
                )
                self._connection.execute(
                    "INSERT INTO executions("
                    "execution_id, decision_id, idempotency_key, proposal_id, run_id,"
                    " fragment_id, loopspec_id, loopspec_version, fingerprint,"
                    " source_sequence, activation_epoch, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
                    (
                        execution_id,
                        decision_id,
                        idempotency_key,
                        proposal_id,
                        run_id,
                        fragment_id,
                        loopspec_id,
                        loopspec_version,
                        fingerprint,
                        int(source_sequence),
                        self._epoch,
                        now,
                        now,
                    ),
                )
                self._add_event_locked(
                    execution_id,
                    "reserved",
                    {"decision_id": decision_id, "source_sequence": int(source_sequence)},
                    now,
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            row = self._connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            return row, True

        return _run_with_retry(_once)

    def get(self, execution_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._connection.execute(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return row

    def for_decision(self, decision_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._connection.execute(
            "SELECT * FROM executions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return row

    def list_active(self) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM executions WHERE status IN ('reserved', 'dispatched')"
                " ORDER BY created_at"
            )
        )

    def update_status(
        self,
        execution_id: str,
        status: str,
        *,
        owner: str | None = None,
        require_owner: bool = False,
    ) -> None:
        now = _utc_now()
        if require_owner:
            cursor = self._connection.execute(
                "UPDATE executions SET status = ?, updated_at = ?"
                " WHERE execution_id = ? AND owner = ?",
                (status, now, execution_id, owner),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"owner check failed for {execution_id}")
        else:
            self._connection.execute(
                "UPDATE executions SET status = ?, updated_at = ? WHERE execution_id = ?",
                (status, now, execution_id),
            )
        self._connection.commit()

    # -- events -------------------------------------------------------------

    def _add_event_locked(
        self, execution_id: str, event_type: str, detail: dict[str, Any], now: str
    ) -> None:
        self._connection.execute(
            "INSERT INTO execution_events(execution_id, event_type, detail_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (execution_id, event_type, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    def add_event(self, execution_id: str, event_type: str, detail: dict[str, Any]) -> None:
        now = _utc_now()

        def _once() -> None:
            self._add_event_locked(execution_id, event_type, detail, now)
            self._connection.commit()

        _run_with_retry(_once)

    def events_for(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM execution_events WHERE execution_id = ? ORDER BY event_id",
            (execution_id,),
        ).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "event_type": str(row["event_type"]),
                "detail": json.loads(str(row["detail_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    # -- lease (owner + heartbeat, same shape as the shadow dispatcher) -----

    def claim_lease(self, execution_id: str, *, owner: str) -> sqlite3.Row:
        """Atomically claim the lease and return the authoritative row.

        One BEGIN IMMEDIATE: the execution must still be in a claimable
        status (``reserved``/``dispatched``) with no other owner.
        Completed, stopped, rejected, or budget-exhausted executions can
        never be claimed. Raises RuntimeError otherwise; callers must use
        the returned row, never a pre-claim snapshot.
        """
        now = _utc_now()

        def _once() -> sqlite3.Row:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "UPDATE executions SET owner = ?, heartbeat_at = ?, updated_at = ?"
                    " WHERE execution_id = ? AND owner IS NULL"
                    " AND status IN ('reserved', 'dispatched')",
                    (owner, now, now, execution_id),
                )
                if cursor.rowcount != 1:
                    self._connection.rollback()
                    raise RuntimeError(
                        f"lease already held or not claimable: {execution_id}"
                    )
                row = self._connection.execute(
                    "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
                ).fetchone()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            if row is None:  # pragma: no cover - defensive; update/select are atomic
                raise RuntimeError(f"claimed execution vanished: {execution_id}")
            claimed: sqlite3.Row = row
            return claimed

        return _run_with_retry(_once)

    def touch(self, execution_id: str, *, owner: str) -> None:
        now = _utc_now()
        cursor = self._connection.execute(
            "UPDATE executions SET heartbeat_at = ? WHERE execution_id = ? AND owner = ?",
            (now, execution_id, owner),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"heartbeat owner check failed for {execution_id}")
        self._connection.commit()

    @contextmanager
    def heartbeat(
        self, execution_id: str, owner: str, *, interval: float | None = None
    ) -> Iterator[None]:
        beat = interval if interval is not None else max(self.lease_seconds / 3.0, 0.05)
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(beat):
                try:
                    self.touch(execution_id, owner=owner)
                except Exception:  # noqa: BLE001 - heartbeat must never kill the node
                    pass

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    def release_lease(self, execution_id: str, *, owner: str) -> None:
        now = _utc_now()
        cursor = self._connection.execute(
            "UPDATE executions SET owner = NULL, heartbeat_at = ?, updated_at = ?"
            " WHERE execution_id = ? AND owner = ?",
            (now, now, execution_id, owner),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"release owner check failed for {execution_id}")
        self._connection.commit()

    def expire_lease(
        self, execution_id: str, *, expected_owner: str, expected_heartbeat: str
    ) -> bool:
        """Atomic CAS lease takeover: expires the lease only when the owner
        and heartbeat still exactly match the stale values read earlier.
        A lease renewed after that read is never killed."""
        now = _utc_now()

        def _once() -> bool:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "UPDATE executions SET owner = NULL, updated_at = ?"
                    " WHERE execution_id = ? AND owner = ? AND heartbeat_at = ?",
                    (now, execution_id, expected_owner, expected_heartbeat),
                )
                if cursor.rowcount != 1:
                    self._connection.rollback()
                    return False
                self._add_event_locked(
                    execution_id,
                    "lease_expired",
                    {"owner": expected_owner, "heartbeat_at": expected_heartbeat},
                    now,
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

        return _run_with_retry(_once)

    def fail_interrupted_executions(self) -> list[str]:
        """CAS-expire active executions whose held lease is older than the lease."""
        now = datetime.now(UTC).timestamp()
        recovered: list[str] = []
        for row in self.list_active():
            heartbeat = row["heartbeat_at"]
            owner = row["owner"]
            if owner is None or heartbeat is None:
                continue
            beat = datetime.fromisoformat(str(heartbeat)).timestamp()
            if now - beat <= self.lease_seconds:
                continue
            if self.expire_lease(
                str(row["execution_id"]),
                expected_owner=str(owner),
                expected_heartbeat=str(heartbeat),
            ):
                recovered.append(str(row["execution_id"]))
        return recovered

    # -- budget reservations --------------------------------------------------

    def open_reservation(
        self, execution_id: str, node: str, tokens: int, tool_calls: int
    ) -> int:
        now = _utc_now()
        cursor = self._connection.execute(
            "INSERT INTO budget_reservations("
            "execution_id, node, reserved_tokens, reserved_tool_calls, status, created_at)"
            " VALUES (?, ?, ?, ?, 'open', ?)",
            (execution_id, node, int(tokens), int(tool_calls), now),
        )
        self._add_event_locked(
            execution_id,
            "budget_reserved",
            {"node": node, "reserved_tokens": int(tokens), "reserved_tool_calls": int(tool_calls)},
            now,
        )
        self._connection.commit()
        return int(cursor.lastrowid or 0)

    def settle_reservation(
        self, reservation_id: int, execution_id: str, *, actual_tokens: int, actual_tool_calls: int
    ) -> None:
        now = _utc_now()
        cursor = self._connection.execute(
            "UPDATE budget_reservations"
            " SET status = 'settled', actual_tokens = ?, actual_tool_calls = ?, settled_at = ?"
            " WHERE reservation_id = ? AND status = 'open'",
            (int(actual_tokens), int(actual_tool_calls), now, reservation_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"reservation {reservation_id} is not open")
        self._add_event_locked(
            execution_id,
            "budget_settled",
            {
                "reservation_id": reservation_id,
                "actual_tokens": int(actual_tokens),
                "actual_tool_calls": int(actual_tool_calls),
            },
            now,
        )
        self._connection.commit()

    def void_open_reservations(self, execution_id: str, reason: str) -> None:
        now = _utc_now()
        self._connection.execute(
            "UPDATE budget_reservations SET status = 'voided', settled_at = ?"
            " WHERE execution_id = ? AND status = 'open'",
            (now, execution_id),
        )
        self._add_event_locked(execution_id, "budget_voided", {"reason": reason}, now)
        self._connection.commit()

    def budget_totals(self, execution_id: str) -> dict[str, int]:
        row = self._connection.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN status = 'settled' THEN actual_tokens END), 0)"
            " AS settled_tokens,"
            " COALESCE(SUM(CASE WHEN status = 'settled' THEN actual_tool_calls END), 0)"
            " AS settled_calls,"
            " COALESCE(SUM(CASE WHEN status = 'open' THEN reserved_tokens END), 0)"
            " AS open_tokens,"
            " COALESCE(SUM(CASE WHEN status = 'open' THEN reserved_tool_calls END), 0)"
            " AS open_calls"
            " FROM budget_reservations WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return {
            "settled_tokens": int(row["settled_tokens"]),
            "settled_tool_calls": int(row["settled_calls"]),
            "open_tokens": int(row["open_tokens"]),
            "open_tool_calls": int(row["open_calls"]),
        }

    def open_reservations(self, execution_id: str) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM budget_reservations"
                " WHERE execution_id = ? AND status = 'open' ORDER BY reservation_id",
                (execution_id,),
            )
        )
