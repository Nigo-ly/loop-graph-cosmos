"""Unified pre-handler execution reservation/lease and strict usage validation.

Loop/Graph Quality V1, Gate 1 slice 1. One persistent reservation primitive
shared by LoopSupervisor and GraphRuntime: the actual handler entry is
linearized here, before any handler code runs. Every successful first entry
(or crash-recovery takeover entry) is one actual handler call; concurrent
losers never reach the handler. ``max_iterations`` (Loop) and
``max_node_executions`` (Graph) are enforced against these actual calls, so
failures, feedback re-entries and recovery attempts all consume budget and
are never refunded.

Classification inventory (G1-25/26): every classified pre-handler reservation
carries a non-NULL, digest-bound execution class; the side table also carries
the nullable ``agent_session_id`` / ``agent_link_digest`` association columns
with a partial unique index (attach lives in ``graph_runtime/agent_ledger``).

Historical tables are untouched: this module only adds the side table
``execution_reservations_v1`` (CREATE IF NOT EXISTS). No ALTER/UPDATE/DELETE
of ``checkpoints``, ``idempotency_keys`` or ``graph_agent_calls_v1``.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# eval_results namespace rules live in common.types (this task's added
# responsibility split out of the >600-line file per the complexity gate);
# re-exported here so every existing import path keeps working unchanged.
from common.types import SYSTEM_EVAL_KEYS as SYSTEM_EVAL_KEYS
from common.types import EvalNamespaceError as EvalNamespaceError
from common.types import eval_results_canonical as eval_results_canonical
from common.types import eval_results_digest as eval_results_digest
from common.types import legacy_flat_eval_view as legacy_flat_eval_view
from common.types import merge_plugin_eval_results as merge_plugin_eval_results
from common.types import merge_runtime_eval_results as merge_runtime_eval_results
from common.types import plugin_eval as plugin_eval
from common.types import raw_eval_value as raw_eval_value
from common.types import translate_view_edit as translate_view_edit

EXECUTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS execution_reservations_v1 (
    execution_id TEXT PRIMARY KEY,
    runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ('loop', 'graph')),
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    spec_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    logical_attempt INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'reserved', 'running', 'committed', 'failed', 'failed_invalid_usage'
    )),
    owner_token TEXT NOT NULL,
    handler_entry_count INTEGER NOT NULL,
    execution_class TEXT,
    classification_digest TEXT,
    agent_session_id TEXT,
    agent_link_digest TEXT,
    lease_expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

# Indexes are created only AFTER the in-place column upgrade in
# ensure_schema: on a pre-R2 sidecar the partial unique index references
# agent_session_id, which does not exist yet, so building indexes first
# fails with OperationalError before the ALTER ever runs.
EXECUTION_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS execution_reservations_v1_identity
    ON execution_reservations_v1(
        runtime_kind, run_id, node_id, input_digest, logical_attempt
    );
CREATE INDEX IF NOT EXISTS execution_reservations_v1_run
    ON execution_reservations_v1(runtime_kind, run_id);
-- One agent session binds at most one execution; multiple NULLs stay legal.
CREATE UNIQUE INDEX IF NOT EXISTS execution_reservations_v1_agent_session
    ON execution_reservations_v1(agent_session_id)
    WHERE agent_session_id IS NOT NULL;
"""

NON_TERMINAL_STATUSES = frozenset({"reserved", "running"})
TERMINAL_FAILED_STATUSES = frozenset({"failed", "failed_invalid_usage"})

DEFAULT_LEASE_SECONDS = 30.0

# Handler execution classes (closed set). The lease is never a retry permit:
# an expired lease on an external non-idempotent execution is held for a
# human, never silently taken over.
EXECUTION_CLASSES = frozenset({"pure", "idempotent", "external_non_idempotent"})
CLASS_EXTERNAL_NON_IDEMPOTENT = "external_non_idempotent"
CLASS_IDEMPOTENT = "idempotent"

CLASSIFICATION_METADATA_VERSION = "execution-classes-v1"


class ExecutionClassificationError(ValueError):
    """Missing/orphan/invalid classification: fail closed before the handler."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def implementation_symbol(handler: Any) -> str:
    """Stable ``module:qualname`` of one registered handler callable."""
    target = getattr(handler, "__self__", None)
    if target is not None:  # bound method: pin the instance class + method name
        owner = type(target)
        return f"{owner.__module__}:{owner.__qualname__}.{handler.__name__}"
    module = getattr(handler, "__module__", None) or type(handler).__module__
    qualname = getattr(handler, "__qualname__", None) or type(handler).__qualname__
    return f"{module}:{qualname}"


def adapter_metadata_digest(
    *,
    registry_key: str,
    runtime_kind: str,
    execution_class: str,
    implementation_symbol: str,
    metadata_version: str = CLASSIFICATION_METADATA_VERSION,
) -> str:
    """Digest binding one inventory entry: any change to the class, symbol,
    version or registry key yields a different digest, so an old reservation
    is never reused against drifted classification metadata."""
    canonical = "\x1f".join(
        (
            "adapter-classification-v1",
            registry_key,
            runtime_kind,
            execution_class,
            implementation_symbol,
            metadata_version,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_adapter_inventory(
    handlers: Mapping[str, Any],
    *,
    runtime_kind: str,
    classifications: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """Cross-check the live handler registry against explicit classifications.

    100% of registered keys must carry an explicit closed-set class and every
    classification key must resolve to a registered handler — no orphans, no
    implicit defaults. Raises ``ExecutionClassificationError`` with a stable
    code (``execution_classification_missing:<key>`` /
    ``execution_classification_orphan:<key>`` /
    ``execution_classification_invalid:<key>``) before any handler may run.
    """
    inventory: dict[str, dict[str, str]] = {}
    for key in sorted(handlers):
        execution_class = classifications.get(key)
        if execution_class is None:
            raise ExecutionClassificationError(f"execution_classification_missing:{key}")
        if execution_class not in EXECUTION_CLASSES:
            raise ExecutionClassificationError(f"execution_classification_invalid:{key}")
        symbol = implementation_symbol(handlers[key])
        inventory[key] = {
            "execution_class": execution_class,
            "implementation_symbol": symbol,
            "metadata_version": CLASSIFICATION_METADATA_VERSION,
            "metadata_digest": adapter_metadata_digest(
                registry_key=key,
                runtime_kind=runtime_kind,
                execution_class=execution_class,
                implementation_symbol=symbol,
            ),
        }
    for key in sorted(classifications):
        if key not in handlers:
            raise ExecutionClassificationError(f"execution_classification_orphan:{key}")
    return inventory


class InvalidUsageError(ValueError):
    """Strict usage rejection: the attempt consumed its call but commits nothing."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_strict_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_strict_duration(value: object) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)  # type: ignore[arg-type]
        and value >= 0  # type: ignore[operator]
    )


def validate_usage(
    *,
    tokens_used: object,
    tool_calls_used: object,
    duration_seconds: object,
) -> None:
    """Reject negative, bool, non-integer counts and non-finite/negative durations.

    token/tool counts: ``type(value) is int`` (bool is not int here) and >= 0.
    duration: int or float, never bool, finite, >= 0. Any violation raises
    ``InvalidUsageError`` with a stable code; callers must not apply the
    result to any checkpoint or refund the consumed call.
    """
    if not _is_strict_count(tokens_used):
        raise InvalidUsageError("invalid_usage_type:tokens_used")
    if not _is_strict_count(tool_calls_used):
        raise InvalidUsageError("invalid_usage_type:tool_calls_used")
    if not _is_strict_duration(duration_seconds):
        raise InvalidUsageError("invalid_usage_type:duration_seconds")


def execution_identity(
    *,
    runtime_kind: str,
    run_id: str,
    node_id: str,
    spec_digest: str,
    input_digest: str,
    logical_attempt: int,
) -> str:
    canonical = "\x1f".join(
        (
            "handler-execution-v1",
            runtime_kind,
            run_id,
            node_id,
            spec_digest,
            input_digest,
            str(logical_attempt),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Reservation:
    """Outcome of one reserve() call.

    ``acquired`` is True only for ``reserved`` / ``reentered`` /
    ``taken_over``; ``reentered`` refreshes the lease of a still-reserved
    row without a new call count. ``in_progress`` / ``already_running`` /
    ``already_committed`` / ``attempt_conflict`` / ``spec_digest_mismatch`` /
    ``classification_mismatch`` / ``external_hold`` / ``reconcile_required``
    are conflicts: no handler entry.
    """

    acquired: bool
    reason: str
    execution_id: str | None = None
    logical_attempt: int | None = None
    handler_entry_count: int = 0


class ExecutionReservations:
    """Persistent reservation/lease ledger over the checkpoint database file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback exactly like ``with conn:`` and always close."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(EXECUTION_SCHEMA_SQL)
        # Idempotent in-place upgrade of THIS TASK'S sidecar only (never any
        # historical table): pre-R2 databases predate these columns. Column
        # names are a fixed closed set — never external data. Columns must
        # exist before any index that references them is created.
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(execution_reservations_v1)")
        }
        for column in (
            "execution_class",
            "classification_digest",
            "agent_session_id",
            "agent_link_digest",
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE execution_reservations_v1 ADD COLUMN {column} TEXT"
                )
        connection.executescript(EXECUTION_INDEX_SQL)

    def reserve(
        self,
        *,
        runtime_kind: str,
        run_id: str,
        node_id: str,
        spec_digest: str,
        input_digest: str,
        owner_token: str,
        logical_attempt: int | None = None,
        execution_class: str | None = None,
        classification_digest: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> Reservation:
        """Atomically claim the execution identity before the handler runs:
        one BEGIN IMMEDIATE transaction serializes all contenders, exactly
        one winner per live lease; a terminal-failed row opens the next
        attempt; committed rows and live foreign leases conflict."""
        if runtime_kind not in ("loop", "graph"):
            raise ValueError(f"unknown runtime_kind: {runtime_kind}")
        if execution_class is not None and execution_class not in EXECUTION_CLASSES:
            raise ValueError(f"unknown execution_class: {execution_class}")
        now = time.time() if now is None else now
        lease_expires_at = now + lease_seconds
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if logical_attempt is None:
                row = connection.execute(
                    """
                    SELECT * FROM execution_reservations_v1
                    WHERE runtime_kind = ? AND run_id = ? AND node_id = ?
                      AND input_digest = ?
                    ORDER BY logical_attempt DESC LIMIT 1
                    """,
                    (runtime_kind, run_id, node_id, input_digest),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM execution_reservations_v1
                    WHERE runtime_kind = ? AND run_id = ? AND node_id = ?
                      AND input_digest = ? AND logical_attempt = ?
                    """,
                    (runtime_kind, run_id, node_id, input_digest, logical_attempt),
                ).fetchone()
            if row is not None:
                # 绑定身份含 spec_digest：不一致 fail closed（旧行只读不回填）。
                if str(row["spec_digest"]) != spec_digest:
                    return Reservation(False, "spec_digest_mismatch")
                # Class/digest drift on an existing binding is never reused; a
                # legacy NULL-classified row fails closed too (never reused,
                # never backfilled).
                if execution_class is not None and (
                    row["execution_class"] is None or str(row["execution_class"]) != execution_class
                ):
                    return Reservation(False, "classification_mismatch")
                if classification_digest is not None and (
                    row["classification_digest"] is None
                    or str(row["classification_digest"]) != classification_digest
                ):
                    return Reservation(False, "classification_mismatch")
                status = str(row["status"])
                if status in NON_TERMINAL_STATUSES:
                    if str(row["owner_token"]) == owner_token:
                        if status == "running":
                            # The handler was already entered on this lease;
                            # re-entering now would be a free extra call.
                            # The runtime must re-submit its in-memory result
                            # or stop safely instead.
                            return Reservation(False, "already_running")
                        connection.execute(
                            """
                            UPDATE execution_reservations_v1
                            SET lease_expires_at = ?, updated_at = ?
                            WHERE execution_id = ?
                            """,
                            (lease_expires_at, now, row["execution_id"]),
                        )
                        return Reservation(
                            True,
                            "reentered",
                            str(row["execution_id"]),
                            int(row["logical_attempt"]),
                            int(row["handler_entry_count"]),
                        )
                    if float(row["lease_expires_at"]) > now:
                        return Reservation(False, "in_progress")
                    if str(row["execution_class"] or "") == CLASS_EXTERNAL_NON_IDEMPOTENT:
                        # Expired lease on an external non-idempotent
                        # execution: the send state is unknown, so the lease
                        # is held for a human — never silently taken over.
                        return Reservation(
                            False,
                            "external_hold",
                            str(row["execution_id"]),
                            int(row["logical_attempt"]),
                        )
                    if str(row["execution_class"] or "") == CLASS_IDEMPOTENT:
                        # Expired lease on an idempotent execution: reconcile
                        # against the stable idempotency key FIRST. Takeover
                        # without a receipt is not reconciliation — the caller
                        # must query the receipt or hold for a human.
                        return Reservation(
                            False,
                            "reconcile_required",
                            str(row["execution_id"]),
                            int(row["logical_attempt"]),
                        )
                    # Expired lease on a pure (or legacy unclassified)
                    # binding: the same binding takes over (recomputable);
                    # the recovery entry is one more actual call, never free.
                    connection.execute(
                        """
                        UPDATE execution_reservations_v1
                        SET owner_token = ?, status = 'reserved',
                            handler_entry_count = handler_entry_count + 1,
                            lease_expires_at = ?, updated_at = ?
                        WHERE execution_id = ?
                        """,
                        (owner_token, lease_expires_at, now, row["execution_id"]),
                    )
                    return Reservation(
                        True,
                        "taken_over",
                        str(row["execution_id"]),
                        int(row["logical_attempt"]),
                        int(row["handler_entry_count"]) + 1,
                    )
                if status == "committed":
                    return Reservation(False, "already_committed")
                if logical_attempt is not None:
                    # A state-derived attempt must never collide with a
                    # terminal row for the same attempt; fail closed.
                    return Reservation(False, "attempt_conflict")
                logical_attempt = int(row["logical_attempt"]) + 1
            else:
                logical_attempt = 1 if logical_attempt is None else logical_attempt
            execution_id = execution_identity(
                runtime_kind=runtime_kind,
                run_id=run_id,
                node_id=node_id,
                spec_digest=spec_digest,
                input_digest=input_digest,
                logical_attempt=logical_attempt,
            )
            connection.execute(
                """
                INSERT INTO execution_reservations_v1 (
                    execution_id, runtime_kind, run_id, node_id, spec_digest,
                    input_digest, logical_attempt, status, owner_token,
                    handler_entry_count, execution_class, classification_digest,
                    lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    runtime_kind,
                    run_id,
                    node_id,
                    spec_digest,
                    input_digest,
                    logical_attempt,
                    owner_token,
                    execution_class,
                    classification_digest,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
            return Reservation(True, "reserved", execution_id, logical_attempt, 1)

    def mark_running(self, execution_id: str, owner_token: str) -> bool:
        """Transition reserved → running at the moment the handler is entered.

        After this point the same owner can never re-enter for free: a later
        reserve() on this row returns ``already_running`` and crash recovery
        sees a started (not pre-send) execution.
        """
        now = time.time()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE execution_reservations_v1
                SET status = 'running', updated_at = ?
                WHERE execution_id = ? AND owner_token = ? AND status = 'reserved'
                """,
                (now, execution_id, owner_token),
            )
            return cursor.rowcount == 1

    def _finalize(self, execution_id: str, owner_token: str, status: str) -> bool:
        """Move an owned non-terminal reservation to a terminal status."""
        now = time.time()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE execution_reservations_v1
                SET status = ?, updated_at = ?
                WHERE execution_id = ? AND owner_token = ?
                  AND status IN ('reserved', 'running')
                """,
                (status, now, execution_id, owner_token),
            )
            return cursor.rowcount == 1

    def complete(self, execution_id: str, owner_token: str) -> bool:
        return self._finalize(execution_id, owner_token, "committed")

    def fail(self, execution_id: str, owner_token: str, *, invalid_usage: bool = False) -> bool:
        return self._finalize(
            execution_id,
            owner_token,
            "failed_invalid_usage" if invalid_usage else "failed",
        )

    def entry_count(self, runtime_kind: str, run_id: str) -> int:
        """Actual handler calls for one run: first entries plus takeovers."""
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(handler_entry_count), 0) AS entries
                FROM execution_reservations_v1
                WHERE runtime_kind = ? AND run_id = ?
                """,
                (runtime_kind, run_id),
            ).fetchone()
        return int(row["entries"]) if row is not None else 0

    def status_of(self, execution_id: str) -> str | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT status FROM execution_reservations_v1 WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return None if row is None else str(row["status"])

    def binding_of(self, execution_id: str) -> dict[str, Any] | None:
        """Binding facts for recovery: runtime/run/node, digests, class,
        agent session — the authoritative fields a verified receipt is
        checked against."""
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT runtime_kind, run_id, node_id, spec_digest,
                       input_digest, execution_class, classification_digest,
                       agent_session_id
                FROM execution_reservations_v1 WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def complete_from_verified_receipt(self, execution_id: str) -> bool:
        """Mark a non-terminal reservation committed after the CALLER has
        verified durable external evidence for the same binding (a completed
        agent-ledger row or an idempotent receipt for the same stable key).
        Recovery commits evidence only — it never re-enters the handler.
        Idempotent: an already-committed row reports True (finalize after a
        crashed recovery attempt)."""
        now = time.time()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE execution_reservations_v1
                SET status = 'committed', updated_at = ?
                WHERE execution_id = ? AND status IN ('reserved', 'running')
                """,
                (now, execution_id),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                "SELECT status FROM execution_reservations_v1 WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            return row is not None and str(row["status"]) == "committed"


def new_owner_token() -> str:
    return uuid.uuid4().hex
