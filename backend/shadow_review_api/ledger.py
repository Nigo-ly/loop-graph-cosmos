"""Append-only shadow review ledger (its own ``shadow_review.sqlite3``).

Every decision is one immutable row in ``decisions``; changing a decision
appends a new row whose ``supersedes_decision_id`` points at the previous
current one. History is never overwritten or deleted. ``decision_events``
mirrors the transitions for audit. Idempotency keys are unique: a replayed
submission returns the original receipt instead of duplicating.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DECISIONS = ("accepted", "deferred", "rejected")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL,
    source_sequence INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accepted','deferred','rejected')),
    reason TEXT,
    resume_condition TEXT,
    note TEXT,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    supersedes_decision_id TEXT
);
CREATE INDEX IF NOT EXISTS decisions_proposal ON decisions(proposal_id, decided_at DESC);

CREATE TABLE IF NOT EXISTS decision_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decision_events_proposal ON decision_events(proposal_id, event_id);

CREATE TABLE IF NOT EXISTS dispatch_permits (
    permit_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    node TEXT NOT NULL,
    pre_iteration INTEGER NOT NULL DEFAULT 0,
    run_sequence INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'started'
        CHECK(status IN ('started', 'committed', 'voided')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS dispatch_permits_attempt
    ON dispatch_permits(execution_id, node, pre_iteration);
CREATE INDEX IF NOT EXISTS dispatch_permits_decision
    ON dispatch_permits(decision_id);
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_receipt(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "decision_id": str(row["decision_id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "proposal_id": str(row["proposal_id"]),
        "run_id": str(row["run_id"]),
        "fragment_id": str(row["fragment_id"]),
        "proposal_fingerprint": str(row["proposal_fingerprint"]),
        "source_sequence": int(row["source_sequence"]),
        "decision": str(row["decision"]),
        "reason": None if row["reason"] is None else str(row["reason"]),
        "resume_condition": (
            None if row["resume_condition"] is None else str(row["resume_condition"])
        ),
        "note": None if row["note"] is None else str(row["note"]),
        "decided_by": str(row["decided_by"]),
        "decided_at": str(row["decided_at"]),
        "supersedes_decision_id": (
            None if row["supersedes_decision_id"] is None else str(row["supersedes_decision_id"])
        ),
    }


def _row_to_permit(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "permit_id": str(row["permit_id"]),
        "execution_id": str(row["execution_id"]),
        "decision_id": str(row["decision_id"]),
        "proposal_id": str(row["proposal_id"]),
        "run_id": str(row["run_id"]),
        "node": str(row["node"]),
        "pre_iteration": int(row["pre_iteration"]),
        "run_sequence": int(row["run_sequence"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


class ReviewConflictError(Exception):
    """The ledger's current decision is not the one the client expected."""


class DispatchPermitError(Exception):
    """The current decision changed before the dispatch permit landed."""


class SQLiteReviewLedger:
    """Append-only audited review store on its own SQLite file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # migrate legacy permit tables BEFORE the schema runs: the new
            # unique index references columns that legacy tables lack.
            self._migrate_permits(connection)
            connection.executescript(_SCHEMA)

    @staticmethod
    def _migrate_permits(connection: sqlite3.Connection) -> None:
        """Upgrade a pre-lifecycle permit table in one transaction.

        Order: add lifecycle columns → fail closed on legacy rows
        (unprovable in-flight permits become ``voided`` with a
        ``migrated-voided`` marker, never ``started``) → drop the legacy
        ``(execution_id, node)`` unique index → create the per-attempt
        ``(execution_id, node, pre_iteration)`` unique index. Everything
        is idempotent; any failure rolls the whole transaction back, so
        no half-migrated shape can persist.
        """
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dispatch_permits'"
        ).fetchone()
        if table is None:
            return  # fresh database: _SCHEMA creates the new table
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(dispatch_permits)")
        }
        indexes = {
            str(row["name"])
            for row in connection.execute("PRAGMA index_list(dispatch_permits)")
        }
        needs_columns = (
            "pre_iteration" not in columns
            or "status" not in columns
            or "updated_at" not in columns
        )
        legacy_index = "dispatch_permits_execution_node" in indexes
        attempt_index = "dispatch_permits_attempt" in indexes
        if not needs_columns and not legacy_index and attempt_index:
            return  # already migrated
        connection.execute("BEGIN IMMEDIATE")
        try:
            if "pre_iteration" not in columns:
                connection.execute(
                    "ALTER TABLE dispatch_permits"
                    " ADD COLUMN pre_iteration INTEGER NOT NULL DEFAULT 0"
                )
            if "status" not in columns:
                connection.execute(
                    "ALTER TABLE dispatch_permits"
                    " ADD COLUMN status TEXT NOT NULL DEFAULT 'started'"
                )
            if "updated_at" not in columns:
                connection.execute(
                    "ALTER TABLE dispatch_permits"
                    " ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
                )
            # fail closed: pre-lifecycle permits can never be proven
            # in-flight, so they must never come back as active passes.
            connection.execute(
                "UPDATE dispatch_permits SET status = 'voided', updated_at = ?"
                " WHERE status = 'started'",
                ("migrated-voided",),
            )
            connection.execute("DROP INDEX IF EXISTS dispatch_permits_execution_node")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS dispatch_permits_attempt"
                " ON dispatch_permits(execution_id, node, pre_iteration)"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def submit(
        self,
        *,
        decision_id: str,
        idempotency_key: str,
        proposal_id: str,
        run_id: str,
        fragment_id: str,
        proposal_fingerprint: str,
        source_sequence: int,
        decision: str,
        reason: str | None,
        resume_condition: str | None,
        note: str | None,
        decided_by: str,
        expected_current_decision_id: str | None,
        decided_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert one immutable decision. Returns (receipt, is_new).

        A replayed idempotency key returns the original receipt with
        is_new=False. When the proposal's current decision is not
        ``expected_current_decision_id``, raises ReviewConflictError — one
        ``BEGIN IMMEDIATE`` transaction, so exactly one concurrent
        submission becomes current.

        ``decided_at`` defaults to the wall clock; tests inject a frozen
        value so time-boundary checks never depend on the system date.
        """
        if decision not in DECISIONS:
            raise ValueError(f"Unknown decision: {decision}")
        now = decided_at or _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return _row_to_receipt(existing), False
            current = connection.execute(
                "SELECT decision_id FROM decisions WHERE proposal_id = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
            current_id = None if current is None else str(current["decision_id"])
            if current_id != expected_current_decision_id:
                raise ReviewConflictError(
                    f"current decision is {current_id}, expected {expected_current_decision_id}"
                )
            connection.execute(
                """
                INSERT INTO decisions (
                    decision_id, idempotency_key, proposal_id, run_id, fragment_id,
                    proposal_fingerprint, source_sequence, decision, reason,
                    resume_condition, note, decided_by, decided_at,
                    supersedes_decision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    idempotency_key,
                    proposal_id,
                    run_id,
                    fragment_id,
                    proposal_fingerprint,
                    source_sequence,
                    decision,
                    reason,
                    resume_condition,
                    note,
                    decided_by,
                    now,
                    current_id,
                ),
            )
            connection.execute(
                "INSERT INTO decision_events (decision_id, proposal_id, event_type,"
                " created_at) VALUES (?, ?, 'created', ?)",
                (decision_id, proposal_id, now),
            )
            if current_id is not None:
                connection.execute(
                    "INSERT INTO decision_events (decision_id, proposal_id,"
                    " event_type, created_at) VALUES (?, ?, 'superseded', ?)",
                    (current_id, proposal_id, now),
                )
            active_permits = [
                str(permit["permit_id"])
                for permit in connection.execute(
                    "SELECT permit_id FROM dispatch_permits"
                    " WHERE decision_id = ? AND status = 'started'",
                    (current_id,),
                ).fetchall()
            ] if current_id is not None else []
            if active_permits:
                connection.execute(
                    "INSERT INTO decision_events (decision_id, proposal_id,"
                    " event_type, created_at) VALUES (?, ?, 'permit_superseded', ?)",
                    (current_id, proposal_id, now),
                )
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - defensive; insert/select are atomic
            raise RuntimeError("decision insert vanished")
        receipt = _row_to_receipt(row)
        if active_permits:
            # The superseded decision had live dispatch permits: this new
            # decision takes effect from the next node boundary.
            receipt["superseded_active_permits"] = active_permits
        return receipt, True

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        return None if row is None else _row_to_receipt(row)

    def get_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else _row_to_receipt(row)

    def issue_dispatch_permit(
        self,
        *,
        permit_id: str,
        execution_id: str,
        decision_id: str,
        proposal_id: str,
        run_id: str,
        node: str,
        pre_iteration: int,
        run_sequence: int,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically record a dispatch_started permit for one attempt.

        A permit belongs to exactly one node boundary:
        ``(execution_id, node, pre_iteration)``. One BEGIN IMMEDIATE:

        - same attempt, every pinned field identical, status ``started``
          → idempotent replay (returns the existing permit, False);
        - same attempt already ``committed`` → DispatchPermitError (the
          node already ran, never re-run);
        - same attempt ``voided`` with identical pinned fields → re-open
          to ``started`` (idempotent crash recovery), returns (row, False);
        - the current decision for the proposal is not exactly
          ``decision_id`` and ``accepted`` → DispatchPermitError, no write.
        """
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM dispatch_permits"
                " WHERE execution_id = ? AND node = ? AND pre_iteration = ?",
                (execution_id, node, int(pre_iteration)),
            ).fetchone()
            if existing is not None:
                identical = (
                    str(existing["permit_id"]) == permit_id
                    and str(existing["decision_id"]) == decision_id
                    and int(existing["run_sequence"]) == int(run_sequence)
                )
                if not identical:
                    raise DispatchPermitError("permit_attempt_conflict")
                if str(existing["status"]) == "committed":
                    raise DispatchPermitError("permit_already_committed")
                if str(existing["status"]) == "voided":
                    if str(existing["updated_at"]) == "migrated-voided":
                        # fail closed: pre-lifecycle permits never re-open
                        raise DispatchPermitError("permit_attempt_conflict")
                    connection.execute(
                        "UPDATE dispatch_permits SET status = 'started', updated_at = ?"
                        " WHERE permit_id = ?",
                        (now, permit_id),
                    )
                return _row_to_permit(
                    connection.execute(
                        "SELECT * FROM dispatch_permits WHERE permit_id = ?",
                        (permit_id,),
                    ).fetchone()
                ), False
            current = connection.execute(
                "SELECT decision_id, decision FROM decisions"
                " WHERE proposal_id = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
            if current is None:
                raise DispatchPermitError("decision_missing")
            if str(current["decision_id"]) != decision_id:
                if str(current["decision"]) != "accepted":
                    raise DispatchPermitError(f"decision_not_accepted:{current['decision']}")
                raise DispatchPermitError("decision_superseded_before_permit")
            if str(current["decision"]) != "accepted":
                raise DispatchPermitError(f"decision_not_accepted:{current['decision']}")
            connection.execute(
                "INSERT INTO dispatch_permits ("
                " permit_id, execution_id, decision_id, proposal_id, run_id,"
                " node, pre_iteration, run_sequence, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started', ?, ?)",
                (
                    permit_id,
                    execution_id,
                    decision_id,
                    proposal_id,
                    run_id,
                    node,
                    int(pre_iteration),
                    int(run_sequence),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM dispatch_permits WHERE permit_id = ?", (permit_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise RuntimeError("permit insert vanished")
        return _row_to_permit(row), True

    def mark_permit_committed(self, permit_id: str) -> None:
        """Lifecycle: a permitted node committed at its boundary."""
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE dispatch_permits SET status = 'committed', updated_at = ?"
                " WHERE permit_id = ? AND status = 'started'",
                (now, permit_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"permit {permit_id} is not started")

    def void_permit(self, permit_id: str, *, reason: str) -> None:
        """Lifecycle: a permitted dispatch ended without a node commit."""
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE dispatch_permits SET status = 'voided', updated_at = ?"
                " WHERE permit_id = ? AND status = 'started'",
                (now, permit_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"permit {permit_id} is not started")

    def permit_for_attempt(
        self, execution_id: str, node: str, pre_iteration: int
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatch_permits"
                " WHERE execution_id = ? AND node = ? AND pre_iteration = ?",
                (execution_id, node, int(pre_iteration)),
            ).fetchone()
        return None if row is None else _row_to_permit(row)

    def permits_for_decision(self, decision_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dispatch_permits WHERE decision_id = ?"
                " ORDER BY created_at, permit_id",
                (decision_id,),
            ).fetchall()
        return [_row_to_permit(row) for row in rows]

    def permits_for_execution(self, execution_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dispatch_permits WHERE execution_id = ?"
                " ORDER BY created_at, permit_id",
                (execution_id,),
            ).fetchall()
        return [_row_to_permit(row) for row in rows]

    def current_for(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM decisions WHERE proposal_id = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
        return None if row is None else _row_to_receipt(row)

    def current_map(self) -> dict[str, dict[str, Any]]:
        """Latest decision per proposal, for console badges."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.* FROM decisions d
                JOIN (SELECT proposal_id, MAX(decided_at) AS latest
                      FROM decisions GROUP BY proposal_id) latest_rows
                ON d.proposal_id = latest_rows.proposal_id
                   AND d.decided_at = latest_rows.latest
                """
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            receipt = _row_to_receipt(row)
            current = result.get(receipt["proposal_id"])
            if current is None or receipt["decision_id"] > current["decision_id"]:
                result[receipt["proposal_id"]] = receipt
        return result

    def history_for(self, proposal_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM decisions WHERE proposal_id = ?"
                " ORDER BY decided_at DESC, decision_id DESC LIMIT ?",
                (proposal_id, limit),
            ).fetchall()
        return [_row_to_receipt(row) for row in rows]
