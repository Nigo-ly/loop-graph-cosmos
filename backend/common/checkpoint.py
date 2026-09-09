"""SQLite checkpoint store and idempotency ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from common.execution import ExecutionReservations, legacy_flat_eval_view

SCHEMA_VERSION = 1

# A resolution closes an attempt episode (not every terminal state does:
# failed attempts keep the episode open for retry).
EPISODE_RESOLVED_STATUSES = frozenset({"passed", "cancelled", "superseded"})

ChildRegisterResult = Literal["committed", "already_applied", "conflict"]


class CheckpointSequenceError(RuntimeError):
    """AUTOINCREMENT 分配值与事务内预测不一致（写入已回滚）。"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class LoopCheckpoint:
    loop_id: str
    run_id: str
    fragment_id: str
    current_node: str
    status: str
    schema_version: int = SCHEMA_VERSION
    parent_loop_id: str | None = None
    loopspec_version: str = "1"
    iteration: int = 0
    goal: str = ""
    # Per-fragment semantic subject persisted at intake (2026-07-19, P2B
    # revision 5). Defaults to "" so every pre-existing payload still
    # deserializes; the Projection API falls back to goal/loop_id then.
    fragment_title: str = ""
    # Provenance of fragment_title: "user_note" | "user_text" |
    # "verified_source_title" | "" (revision 6). UI and Evaluator must be
    # able to distinguish user-authored titles from extracted ones.
    fragment_title_source: str = ""
    # Persisted attempt-family generation (revision 7): assigned atomically
    # at registration. Ordinary registrations join the family's open
    # episode; only an explicit reopen starts the next one. Defaults to 0
    # so pre-existing payloads still deserialize.
    attempt_episode: int = 0
    input_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    completed_actions: list[str] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    idempotency_keys: list[str] = field(default_factory=list)
    eval_results: dict[str, Any] = field(default_factory=dict)
    unresolved_issues: list[str] = field(default_factory=list)
    last_error: dict[str, Any] | None = None
    budget_limits: dict[str, int | float] = field(default_factory=dict)
    budget_used: dict[str, int | float] = field(default_factory=dict)
    next_wake_at: str | None = None
    stop_reason: str | None = None
    resume_condition: str | None = None
    worker_version: str = ""
    evaluator_version: str = ""
    event_type: str = "snapshot"
    revision_reason: str | None = None
    supersedes_sequence: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LoopCheckpoint:
        return cls(**value)


def _compat_view(checkpoint: LoopCheckpoint) -> LoopCheckpoint:
    """legacy_flat_v1 read-boundary projection (Gate 1): consumer reads see
    the detached flat view for old and namespaced rows alike; storage is
    never rewritten. Runtime write paths use the raw reads instead."""
    return replace(
        checkpoint,
        eval_results=dict(legacy_flat_eval_view(checkpoint.eval_results)),
    )


def checkpoint_compat_view(checkpoint: LoopCheckpoint) -> LoopCheckpoint:
    """Public consumer-facing legacy-flat projection of one checkpoint."""
    return _compat_view(checkpoint)


def normalize_target(target: str) -> str:
    """Normalize an action target without changing URL path semantics."""
    value = " ".join(target.strip().split())
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""))
    return value


def make_idempotency_key(
    loop_id: str,
    fragment_id: str,
    node: str,
    action_type: str,
    target: str,
) -> str:
    canonical = "\x1f".join((loop_id, fragment_id, node, action_type, normalize_target(target)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SQLiteCheckpointStore:
    """Append-only checkpoints plus an atomic action reservation ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback exactly like ``with conn:`` and always close (a
        GC-timed late close could checkpoint the WAL at an arbitrary later
        moment, drifting backup digest assertions)."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # Gate 1 side table (additive only; historical tables untouched).
            ExecutionReservations.ensure_schema(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_version INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    loop_id TEXT NOT NULL,
                    fragment_id TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    current_node TEXT NOT NULL,
                    status TEXT NOT NULL,
                    event_type TEXT NOT NULL DEFAULT 'snapshot',
                    revision_reason TEXT,
                    supersedes_sequence INTEGER,
                    payload_json TEXT NOT NULL,
                    committed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoints_latest
                    ON checkpoints(run_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    normalized_target TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('reserved', 'completed')),
                    reservation_mode TEXT NOT NULL DEFAULT 'pre_execution'
                        CHECK(reservation_mode IN ('pre_execution', 'reconciled')),
                    result_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            checkpoint_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(checkpoints)")
            }
            for column, definition in (
                ("event_type", "TEXT NOT NULL DEFAULT 'snapshot'"),
                ("revision_reason", "TEXT"),
                ("supersedes_sequence", "INTEGER"),
            ):
                if column not in checkpoint_columns:
                    connection.execute(f"ALTER TABLE checkpoints ADD COLUMN {column} {definition}")
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(idempotency_keys)")
            }
            if "reservation_mode" not in columns:
                connection.execute(
                    """
                    ALTER TABLE idempotency_keys
                    ADD COLUMN reservation_mode TEXT NOT NULL DEFAULT 'pre_execution'
                    CHECK(reservation_mode IN ('pre_execution', 'reconciled'))
                    """
                )

    @staticmethod
    def _insert_checkpoint(
        connection: sqlite3.Connection,
        checkpoint: LoopCheckpoint,
        *,
        event_type: str | None = None,
        revision_reason: str | None = None,
    ) -> LoopCheckpoint:
        previous = connection.execute(
            "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
            (checkpoint.run_id,),
        ).fetchone()
        committed = replace(
            checkpoint,
            event_type=event_type or checkpoint.event_type,
            revision_reason=(
                revision_reason if revision_reason is not None else checkpoint.revision_reason
            ),
            supersedes_sequence=(
                int(previous["sequence"])
                if previous is not None and previous["sequence"] is not None
                else None
            ),
            updated_at=utc_now(),
        )
        connection.execute(
            """
            INSERT INTO checkpoints (
                schema_version, run_id, loop_id, fragment_id, iteration,
                current_node, status, event_type, revision_reason,
                supersedes_sequence, payload_json, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                committed.schema_version,
                committed.run_id,
                committed.loop_id,
                committed.fragment_id,
                committed.iteration,
                committed.current_node,
                committed.status,
                committed.event_type,
                committed.revision_reason,
                committed.supersedes_sequence,
                json.dumps(committed.to_dict(), ensure_ascii=False, sort_keys=True),
                committed.updated_at,
            ),
        )
        return committed

    def save(
        self,
        checkpoint: LoopCheckpoint,
        *,
        event_type: str = "snapshot",
        revision_reason: str | None = None,
    ) -> LoopCheckpoint:
        with self._session() as connection:
            committed = self._insert_checkpoint(
                connection,
                checkpoint,
                event_type=event_type,
                revision_reason=revision_reason,
            )
            return _compat_view(committed)

    def latest(self, run_id: str) -> LoopCheckpoint | None:
        checkpoint = self.latest_raw(run_id)
        return None if checkpoint is None else _compat_view(checkpoint)

    def latest_raw(self, run_id: str) -> LoopCheckpoint | None:
        """Latest checkpoint WITHOUT the legacy-flat view (runtime writes)."""
        found = self.latest_raw_with_sequence(run_id)
        return None if found is None else found[0]

    def latest_sequence(self, run_id: str) -> int | None:
        """Committed sequence of the latest checkpoint for run_id, if any."""
        with self._session() as connection:
            row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None or row["sequence"] is None:
            return None
        return int(row["sequence"])

    def latest_with_sequence(self, run_id: str) -> tuple[LoopCheckpoint, int] | None:
        """Latest checkpoint plus its committed sequence, read atomically."""
        found = self.latest_raw_with_sequence(run_id)
        if found is None:
            return None
        checkpoint, sequence = found
        return _compat_view(checkpoint), sequence

    def latest_raw_with_sequence(self, run_id: str) -> tuple[LoopCheckpoint, int] | None:
        """Raw latest checkpoint + sequence (no legacy-flat view): runtime
        write paths merge against the stored form, never a compat view."""
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT sequence, payload_json FROM checkpoints
                WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload_json"])
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported checkpoint schema: {data.get('schema_version')}")
        return LoopCheckpoint.from_dict(data), int(row["sequence"])

    def compare_and_append(
        self,
        checkpoint: LoopCheckpoint,
        *,
        expected_sequence: int,
        event_type: str = "snapshot",
        revision_reason: str | None = None,
    ) -> LoopCheckpoint | None:
        """Atomically append only if the run's latest sequence matches.

        The check and the insert run inside one BEGIN IMMEDIATE transaction,
        so no other writer can interleave. Pass ``expected_sequence=0`` to
        require that the run has no checkpoints yet. Returns the committed
        checkpoint, or None on conflict (nothing was written).
        """
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
                (checkpoint.run_id,),
            ).fetchone()
            current = int(row["sequence"]) if row is not None and row["sequence"] is not None else 0
            if current != expected_sequence:
                connection.rollback()
                return None
            committed = self._insert_checkpoint(
                connection,
                checkpoint,
                event_type=event_type,
                revision_reason=revision_reason,
            )
            return _compat_view(committed)

    def append_with_assigned_sequence(
        self,
        run_id: str,
        *,
        expected_sequence: int,
        build: Any,
        event_type: str,
    ) -> tuple[LoopCheckpoint, int] | None:
        """Atomically append with the sequence the row will actually receive.

        ``sequence`` is a table-level AUTOINCREMENT shared by every run, so
        ``MAX(sequence) + 1`` is wrong whenever the highest row was deleted
        (AUTOINCREMENT never reuses values). Inside one BEGIN IMMEDIATE
        transaction this method:

        1. verifies the run's own latest sequence matches ``expected_sequence``;
        2. computes ``next = max(sqlite_sequence.seq, MAX(sequence), 0) + 1``
           (tolerating a fresh empty table and restored databases where the
           ``sqlite_sequence`` row is missing or unreadable);
        3. invokes ``build(next)`` so the caller can bind that exact value into
           the checkpoint payload;
        4. inserts and asserts the assigned sequence equals the prediction
           (mismatch rolls back and raises ``CheckpointSequenceError``).

        Returns ``(committed checkpoint, assigned sequence)``, or ``None`` on
        conflict (nothing written, ``build`` never called).
        """
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            current = int(row["sequence"]) if row is not None and row["sequence"] is not None else 0
            if current != expected_sequence:
                connection.rollback()
                return None
            table_max_row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints"
            ).fetchone()
            table_max = (
                int(table_max_row["sequence"])
                if table_max_row is not None and table_max_row["sequence"] is not None
                else 0
            )
            try:
                seq_row = connection.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = 'checkpoints'"
                ).fetchone()
                autoincrement_seq = (
                    int(seq_row["seq"]) if seq_row is not None and seq_row["seq"] is not None else 0
                )
            except sqlite3.Error:
                # 恢复库 sqlite_sequence 缺失/异常：回退到 0，由 max() 兜底。
                autoincrement_seq = 0
            next_sequence = max(autoincrement_seq, table_max, 0) + 1
            checkpoint = build(next_sequence)
            committed = self._insert_checkpoint(connection, checkpoint, event_type=event_type)
            assigned_row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assigned = (
                int(assigned_row["sequence"])
                if assigned_row is not None and assigned_row["sequence"] is not None
                else 0
            )
            if assigned != next_sequence:
                connection.rollback()
                raise CheckpointSequenceError(
                    f"assigned sequence drifted: predicted {next_sequence}, got {assigned}"
                )
            return _compat_view(committed), assigned

    def find_revision_marker(self, run_id: str, marker: str) -> int | None:
        """Sequence of the newest checkpoint with revision_reason == marker."""
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT sequence FROM checkpoints
                WHERE run_id = ? AND revision_reason = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (run_id, marker),
            ).fetchone()
        return None if row is None else int(row["sequence"])

    def register_child_atomic(
        self,
        child: LoopCheckpoint,
        *,
        parent_run_id: str,
        expected_parent_sequence: int,
        marker: str,
    ) -> ChildRegisterResult:
        """Check the parent sequence and register a deterministic child atomically.

        Both reads and the child insert run in one BEGIN IMMEDIATE
        transaction on the Loop database, so no run writer can advance the
        parent in between. Returns:
        - ``committed``: the child was registered with the marker;
        - ``already_applied``: the child already existed with this marker;
        - ``conflict``: the parent advanced past expected_parent_sequence,
          or the child id exists with a different marker (a collision is
          never mistaken for an application).
        """
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
                (parent_run_id,),
            ).fetchone()
            current = int(row["sequence"]) if row is not None and row["sequence"] is not None else 0
            if current != expected_parent_sequence:
                connection.rollback()
                return "conflict"
            child_row = connection.execute(
                """
                SELECT revision_reason FROM checkpoints
                WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (child.run_id,),
            ).fetchone()
            if child_row is not None:
                connection.rollback()
                if child_row["revision_reason"] == marker:
                    return "already_applied"
                return "conflict"
            self._insert_checkpoint(
                connection,
                child,
                event_type="run_registered",
                revision_reason=marker,
            )
            return "committed"

    @contextmanager
    def sequence_guard(self, run_id: str, expected_sequence: int) -> Iterator[bool]:
        """Hold a BEGIN IMMEDIATE write guard while verifying a run sequence.

        While the guard is held, no run writer can append to the Loop
        database, giving a two-store operation (Loop sequence check +
        control-ledger write) a real linearization point. Yields True when
        the run's latest committed sequence equals expected_sequence. The
        guard itself never writes; it is always rolled back on exit.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            current = int(row["sequence"]) if row is not None and row["sequence"] is not None else 0
            yield current == expected_sequence
        finally:
            connection.rollback()
            connection.close()

    def latest_for_fragment(self, loop_id: str, fragment_id: str) -> LoopCheckpoint | None:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM checkpoints
                WHERE loop_id = ? AND fragment_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (loop_id, fragment_id),
            ).fetchone()
        if row is None:
            return None
        return _compat_view(LoopCheckpoint.from_dict(json.loads(row["payload_json"])))

    def family_attempts(self, loop_id: str, fragment_id: str) -> list[LoopCheckpoint]:
        """Latest checkpoint per run for one attempt family, oldest first."""
        with self._session() as connection:
            return self._family_attempts_in(connection, loop_id, fragment_id)

    def _family_attempts_in(
        self, connection: sqlite3.Connection, loop_id: str, fragment_id: str
    ) -> list[LoopCheckpoint]:
        rows = connection.execute(
            """
            SELECT c.payload_json FROM checkpoints c
            JOIN (
                SELECT run_id, MAX(sequence) AS max_sequence
                FROM checkpoints GROUP BY run_id
            ) latest
              ON latest.run_id = c.run_id AND latest.max_sequence = c.sequence
            WHERE c.loop_id = ? AND c.fragment_id = ?
            ORDER BY c.sequence ASC
            """,
            (loop_id, fragment_id),
        ).fetchall()
        return [
            _compat_view(LoopCheckpoint.from_dict(json.loads(row["payload_json"])))
            for row in rows
        ]

    def register_attempt(
        self,
        checkpoint: LoopCheckpoint,
        *,
        allow_reopen: bool,
        opened_by: str | None = None,
    ) -> tuple[LoopCheckpoint, str]:
        """Atomically decide the family episode and insert the new run.

        One BEGIN IMMEDIATE transaction reads the family's attempts,
        enforces the frozen episode rules, and inserts — no other writer
        can interleave a resolution between the decision and the insert.
        Ordinary registration joins the open episode; after the family's
        highest episode is resolved, only allow_reopen may register the
        next one. Returns (committed, "registered" | "reopened").
        """
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempts = self._family_attempts_in(
                connection, checkpoint.loop_id, checkpoint.fragment_id
            )
            existing = connection.execute(
                "SELECT 1 FROM checkpoints WHERE run_id = ? LIMIT 1",
                (checkpoint.run_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Run already exists: {checkpoint.run_id}")
            if not attempts:
                if allow_reopen:
                    raise KeyError(f"Unknown fragment family: {checkpoint.fragment_id}")
                episode = 0
                outcome = "registered"
            else:
                highest = max(attempt.attempt_episode for attempt in attempts)
                latest = [attempt for attempt in attempts if attempt.attempt_episode == highest]
                resolved = any(attempt.status in EPISODE_RESOLVED_STATUSES for attempt in latest)
                if not resolved:
                    if allow_reopen:
                        raise ValueError(
                            "Latest episode is not resolved; an explicit reopen is unnecessary"
                        )
                    episode = highest
                    outcome = "registered"
                else:
                    if not allow_reopen:
                        raise ValueError(
                            "Attempt family episode is resolved; an explicit reopen is "
                            "required to register a new attempt"
                        )
                    episode = highest + 1
                    outcome = "reopened"
            committed = self._insert_checkpoint(
                connection,
                replace(checkpoint, attempt_episode=episode),
                event_type="run_registered",
                revision_reason=(
                    f"episode_reopened_by:{opened_by}" if outcome == "reopened" else None
                ),
            )
        return _compat_view(committed), outcome

    def history(self, run_id: str) -> list[LoopCheckpoint]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM checkpoints
                WHERE run_id = ? ORDER BY sequence ASC
                """,
                (run_id,),
            ).fetchall()
        return [
            _compat_view(LoopCheckpoint.from_dict(json.loads(row["payload_json"])))
            for row in rows
        ]

    def run_ids_with_prefix(self, loop_id_prefix: str) -> list[str]:
        """Read-only enumeration of run IDs whose loop_id carries the prefix.

        Graph Phase 1 uses ``graph:``; the prefix is matched literally (the
        caller passes no LIKE wildcards).
        """
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT run_id FROM checkpoints
                WHERE loop_id LIKE ? ESCAPE '\\' ORDER BY run_id ASC
                """,
                (
                    loop_id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + "%",
                ),
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def history_with_sequence(self, run_id: str) -> list[tuple[int, LoopCheckpoint]]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT sequence, payload_json FROM checkpoints
                WHERE run_id = ? ORDER BY sequence ASC
                """,
                (run_id,),
            ).fetchall()
        return [
            (
                int(row["sequence"]),
                _compat_view(LoopCheckpoint.from_dict(json.loads(row["payload_json"]))),
            )
            for row in rows
        ]

    def events_after(
        self, run_id: str, sequence: int
    ) -> list[tuple[int, str, str, int, str | None]]:
        """(sequence, event_type, current_node, iteration, revision_reason)
        for rows after ``sequence``.

        Recovery code uses this to distinguish real node commits
        (``node_completed``/``loop_stopped`` with an iteration advance)
        from control-plane state writes that merely move the sequence,
        and to bind a commit to its execution marker.
        """
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_type, current_node, iteration, revision_reason
                FROM checkpoints
                WHERE run_id = ? AND sequence > ? ORDER BY sequence ASC
                """,
                (run_id, int(sequence)),
            ).fetchall()
        return [
            (
                int(row["sequence"]),
                str(row["event_type"]),
                str(row["current_node"]),
                int(row["iteration"]),
                None if row["revision_reason"] is None else str(row["revision_reason"]),
            )
            for row in rows
        ]

    def prepare_action(
        self,
        checkpoint: LoopCheckpoint,
        *,
        action_type: str,
        target: str,
        node: str | None = None,
    ) -> tuple[LoopCheckpoint, str, bool]:
        """Atomically reserve an idempotency key and persist the pending action.

        ``node`` defaults to ``checkpoint.current_node`` (legacy behavior);
        callers whose logical action outlives scheduler-driven node advances
        (e.g. research collection across feedback/observation cycles) pass a
        stable component so the key does not drift with ``current_node``."""
        normalized = normalize_target(target)
        key = make_idempotency_key(
            checkpoint.loop_id,
            checkpoint.fragment_id,
            node if node is not None else checkpoint.current_node,
            action_type,
            normalized,
        )
        now = utc_now()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO idempotency_keys (
                    idempotency_key, run_id, node, action_type, normalized_target,
                    status, reservation_mode, result_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', 'pre_execution', NULL, ?, ?)
                """,
                (
                    key,
                    checkpoint.run_id,
                    checkpoint.current_node,
                    action_type,
                    normalized,
                    now,
                    now,
                ),
            )
            is_new = cursor.rowcount == 1
            keys = list(checkpoint.idempotency_keys)
            if key not in keys:
                keys.append(key)
            prepared = replace(
                checkpoint,
                pending_action={
                    "key": key,
                    "action_type": action_type,
                    "target": normalized,
                },
                idempotency_keys=keys,
            )
            committed = self._insert_checkpoint(
                connection,
                prepared,
                event_type="action_reserved",
                revision_reason=action_type,
            )
        return _compat_view(committed), key, is_new

    def claim_run_identity(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        action_type: str,
        binding_digest: str,
    ) -> tuple[bool, str, str]:
        """Atomically claim a deterministic external run identity.

        This reuses the existing idempotency ledger without creating a second
        lock or database.  The winner may safely retry registration with the
        same run/binding; a different binding observes the original claim.
        """
        if not all(
            isinstance(value, str) and value and len(value) <= 256
            for value in (idempotency_key, run_id, action_type, binding_digest)
        ):
            raise ValueError("invalid_run_identity_claim")
        now = utc_now()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO idempotency_keys (
                    idempotency_key, run_id, node, action_type, normalized_target,
                    status, reservation_mode, result_ref, created_at, updated_at
                ) VALUES (?, ?, 'registration', ?, ?, 'reserved',
                          'pre_execution', NULL, ?, ?)
                """,
                (idempotency_key, run_id, action_type, binding_digest, now, now),
            )
            row = connection.execute(
                "SELECT run_id, normalized_target FROM idempotency_keys WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("run_identity_claim_missing")
            return cursor.rowcount == 1, str(row["run_id"]), str(row["normalized_target"])

    def claim_action_once(
        self,
        *,
        run_id: str,
        loop_id: str,
        fragment_id: str,
        node: str,
        action_type: str,
        target: str,
        result_ref: str,
    ) -> tuple[str, bool]:
        """Atomically claim a one-shot action key; returns ``(key, is_new)``.

        A single ``INSERT OR IGNORE`` against the PRIMARY KEY constraint —
        the claim is decided inside one SQL statement, so concurrent
        connections cannot both win through a read-check-write window.
        No checkpoint row is appended (zero sequence side effects); the
        ``result_ref`` carries the caller's claim facts (claimed_at etc.).
        """
        normalized = normalize_target(target)
        key = make_idempotency_key(loop_id, fragment_id, node, action_type, normalized)
        now = utc_now()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO idempotency_keys (
                    idempotency_key, run_id, node, action_type, normalized_target,
                    status, reservation_mode, result_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'completed', 'pre_execution', ?, ?, ?)
                """,
                (key, run_id, node, action_type, normalized, result_ref, now, now),
            )
            return key, cursor.rowcount == 1

    def cas_action_result_ref(
        self, key: str, *, expected_ref: str, new_ref: str
    ) -> bool:
        """Atomically replace an action's ``result_ref`` only when it still
        equals ``expected_ref`` (single UPDATE ... WHERE); exactly one
        concurrent takeover can win."""
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE idempotency_keys
                SET result_ref = ?, updated_at = ?
                WHERE idempotency_key = ? AND result_ref = ?
                """,
                (new_ref, utc_now(), key, expected_ref),
            )
            return cursor.rowcount == 1

    def complete_action(
        self,
        checkpoint: LoopCheckpoint,
        key: str,
        result_ref: str | None = None,
    ) -> LoopCheckpoint:
        """Commit an already reserved action and its corrected checkpoint atomically."""
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE idempotency_keys
                SET status = 'completed', result_ref = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (result_ref, utc_now(), key),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown idempotency key: {key}")
            action = connection.execute(
                "SELECT action_type FROM idempotency_keys WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            completed = list(checkpoint.completed_actions)
            if key not in completed:
                completed.append(key)
            committed = self._insert_checkpoint(
                connection,
                replace(
                    checkpoint,
                    completed_actions=completed,
                    pending_action=None,
                ),
                event_type="action_completed",
                revision_reason=str(action["action_type"]) if action is not None else None,
            )
        return _compat_view(committed)

    def reconcile_action(
        self,
        checkpoint: LoopCheckpoint,
        *,
        action_type: str,
        target: str,
        result_ref: str | None = None,
    ) -> tuple[LoopCheckpoint, str, bool]:
        """Record a durable write discovered after execution without faking a preflight."""
        normalized = normalize_target(target)
        key = make_idempotency_key(
            checkpoint.loop_id,
            checkpoint.fragment_id,
            checkpoint.current_node,
            action_type,
            normalized,
        )
        now = utc_now()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO idempotency_keys (
                    idempotency_key, run_id, node, action_type, normalized_target,
                    status, reservation_mode, result_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'completed', 'reconciled', ?, ?, ?)
                """,
                (
                    key,
                    checkpoint.run_id,
                    checkpoint.current_node,
                    action_type,
                    normalized,
                    result_ref,
                    now,
                    now,
                ),
            )
            is_new = cursor.rowcount == 1
            keys = list(checkpoint.idempotency_keys)
            completed = list(checkpoint.completed_actions)
            if not is_new and key in keys and key in completed:
                return checkpoint, key, False
            if key not in keys:
                keys.append(key)
            if key not in completed:
                completed.append(key)
            committed = self._insert_checkpoint(
                connection,
                replace(
                    checkpoint,
                    idempotency_keys=keys,
                    completed_actions=completed,
                    pending_action=None,
                ),
                event_type="action_reconciled",
                revision_reason=action_type,
            )
        return _compat_view(committed), key, is_new

    def action_status(self, key: str) -> str | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT status FROM idempotency_keys WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["status"])

    def action_record(self, key: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return None if row is None else dict(row)
