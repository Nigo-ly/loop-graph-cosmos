"""Shadow proposal ledger: immutable payloads plus an append-only event log.

Truthful storage model (frozen in P3A revision 2):

- Proposal rows are never deleted, and the JSON ``payload`` is immutable
  once inserted.
- The ``status`` column is a *mutable current-status index*, updated only
  in the same transaction that appends the ``proposal_events`` rows
  recording every transition. Readers must merge the two via
  ``payload_of()`` — the payload's embedded ``proposal_status`` is the
  as-scanned value and is not authoritative after a stale/superseded
  transition.
- One ``BEGIN IMMEDIATE`` transaction covers the whole scan commit so two
  concurrent dispatchers cannot create duplicate active proposals.
- Dispatcher run bookkeeping uses an owner token plus a heartbeat lease
  renewed for the whole scan: only a provably expired run (heartbeat older
  than the lease) is recovered as ``failed``; a live scanner — however
  slow — is never marked crashed, and finalization only succeeds for the
  run's owner.
- All initialization, startup, and commit writes go through a bounded
  SQLITE_BUSY retry so concurrent scanners cannot fail with
  ``database is locked``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict, TypeVar

from .proposals import ACTIVE_STATUSES, Proposal


class ScanCommitResult(TypedDict):
    counts: dict[str, int]
    supersedes: dict[str, str]

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_sequence INTEGER NOT NULL,
    dispatcher_version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_proposal_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS proposals_idempotent_triple
    ON proposals(run_id, source_sequence, dispatcher_version, fingerprint);
CREATE INDEX IF NOT EXISTS proposals_active_run
    ON proposals(run_id, status);
CREATE TABLE IF NOT EXISTS proposal_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatcher_runs (
    dispatcher_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatcher_version TEXT NOT NULL,
    owner TEXT,
    status TEXT NOT NULL,
    scanned_runs INTEGER,
    admitted_runs INTEGER,
    proposals_created INTEGER,
    proposals_reused INTEGER,
    proposals_staled INTEGER,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    finished_at TEXT
);
"""

DEFAULT_LEASE_SECONDS = 300.0

_T = TypeVar("_T")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _run_with_retry(  # noqa: UP047 - PEP 695 generics need Python 3.12+, gate runs 3.11
    operation: Callable[[], _T], *, attempts: int = 12, base_delay: float = 0.02
) -> _T:
    """Run one write operation, retrying transient SQLite lock contention.

    SQLite raises ``OperationalError: database is locked`` immediately (not
    after the connection timeout) when a lock upgrade would deadlock, which
    is exactly what racing scanner processes hit at startup. A bounded
    exponential backoff turns that into a reliable single-writer protocol.
    Non-lock errors are raised untouched.
    """
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if "lock" not in str(error).lower() or attempt == attempts - 1:
                raise
            time.sleep(min(base_delay * (2**attempt), 1.0))
    raise AssertionError("unreachable: retry loop always returns or raises")


class ShadowLedger:
    def __init__(self, path: str | Path, *, lease_seconds: float = DEFAULT_LEASE_SECONDS):
        self.path = Path(path).expanduser()
        self.lease_seconds = float(lease_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def initialize() -> None:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(SCHEMA)

        _run_with_retry(initialize)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    # --- dispatcher run bookkeeping (owner + heartbeat lease) -------------

    def fail_interrupted_runs(self) -> int:
        """Fail provably expired runs (heartbeat older than the lease).

        A run owned by a live concurrent scanner has a fresh heartbeat and
        is never touched; only a scanner that crashed (or is stuck beyond
        the lease) is recovered. Idempotent.
        """
        cutoff = (datetime.now(UTC) - timedelta(seconds=self.lease_seconds)).isoformat()

        def fail() -> int:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE dispatcher_runs SET status = 'failed', finished_at = ?"
                    " WHERE status = 'running'"
                    " AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                    (_utc_now(), cutoff),
                )
                return cursor.rowcount

        return _run_with_retry(fail)

    def begin_run(self, dispatcher_version: str, *, owner: str) -> int:
        def begin() -> int:
            now = _utc_now()
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO dispatcher_runs"
                    " (dispatcher_version, owner, status, started_at, heartbeat_at)"
                    " VALUES (?, ?, 'running', ?, ?)",
                    (dispatcher_version, owner, now, now),
                )
                row_id = cursor.lastrowid
                if row_id is None:  # pragma: no cover - sqlite always sets it
                    raise RuntimeError("dispatcher_runs insert did not return a row id")
                return int(row_id)

        return _run_with_retry(begin)

    def touch_run(self, dispatcher_run_id: int, *, owner: str) -> None:
        """Refresh the lease heartbeat for a live run owned by ``owner``."""

        def touch() -> None:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE dispatcher_runs SET heartbeat_at = ?"
                    " WHERE dispatcher_run_id = ? AND owner = ? AND status = 'running'",
                    (_utc_now(), dispatcher_run_id, owner),
                )

        _run_with_retry(touch)

    @contextmanager
    def heartbeat(
        self, dispatcher_run_id: int, owner: str, *, interval: float | None = None
    ) -> Iterator[None]:
        """Context manager: renew the run's lease while the scan is alive.

        A daemon thread touches the heartbeat every ``interval`` seconds
        (default: a third of the lease) so a legitimately slow scan is never
        recovered as crashed by another scanner. Heartbeat failures are
        swallowed on purpose — finalization is owner-checked, so a lost beat
        can delay recovery but cannot corrupt the audit trail.
        """
        beat_interval = interval if interval is not None else max(self.lease_seconds / 3, 0.05)
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(beat_interval):
                try:
                    self.touch_run(dispatcher_run_id, owner=owner)
                except sqlite3.Error:
                    pass

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    # --- queries ----------------------------------------------------------

    def active_proposals(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM proposals WHERE status IN "
                    f"({','.join(repr(s) for s in sorted(ACTIVE_STATUSES))})"
                )
            )

    def proposals_for_run(self, run_id: str) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM proposals WHERE run_id = ? ORDER BY created_at, proposal_id",
                    (run_id,),
                )
            )

    def proposal_by_id(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return None if row is None else payload_of(row)

    def events_for(self, proposal_id: str) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM proposal_events WHERE proposal_id = ? ORDER BY event_id",
                    (proposal_id,),
                )
            )

    def run_rows(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM dispatcher_runs ORDER BY dispatcher_run_id"
                )
            )

    # --- the scan commit ---------------------------------------------------

    def commit_scan(
        self,
        dispatcher_run_id: int,
        proposals: list[Proposal],
        *,
        scanned_runs: int,
        admitted_run_ids: set[str],
        owner: str,
        preserved_run_ids: set[str] | None = None,
    ) -> ScanCommitResult:
        """Atomically commit one scan: insert new proposals, stale or
        supersede outdated ones, stale proposals whose runs left the queue,
        and finish the dispatcher run record. All in one IMMEDIATE txn,
        retried on transient lock contention. Finalization is owner-aware:
        a scanner can only finish a run it owns.

        ``preserved_run_ids`` are runs currently in execution (``running``):
        already routed, so they receive no new proposals and their active
        proposals are never staled while the execution is mid-flight.
        """
        return _run_with_retry(
            lambda: self._commit_scan_once(
                dispatcher_run_id,
                proposals,
                scanned_runs=scanned_runs,
                admitted_run_ids=admitted_run_ids,
                owner=owner,
                preserved_run_ids=preserved_run_ids,
            )
        )

    def _commit_scan_once(
        self,
        dispatcher_run_id: int,
        proposals: list[Proposal],
        *,
        scanned_runs: int,
        admitted_run_ids: set[str],
        owner: str,
        preserved_run_ids: set[str] | None = None,
    ) -> ScanCommitResult:
        counts = {"created": 0, "reused": 0, "staled": 0}
        supersedes: dict[str, str] = {}
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            by_run: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                "SELECT * FROM proposals WHERE status IN "
                f"({','.join(repr(s) for s in sorted(ACTIVE_STATUSES))})"
            ):
                by_run.setdefault(str(row["run_id"]), []).append(row)

            incoming_runs = {p.run_id for p in proposals}
            for proposal in proposals:
                actives = by_run.get(proposal.run_id, [])
                same = [
                    row
                    for row in actives
                    if int(row["source_sequence"]) == proposal.source_sequence
                    and str(row["dispatcher_version"]) == proposal.dispatcher_version
                    and str(row["fingerprint"]) == proposal.fingerprint
                ]
                if same:
                    counts["reused"] += 1
                    continue

                superseded_id: str | None = None
                for row in actives:
                    old_status = str(row["status"])
                    if int(row["source_sequence"]) < proposal.source_sequence:
                        connection.execute(
                            "UPDATE proposals SET status = 'stale' WHERE proposal_id = ?",
                            (row["proposal_id"],),
                        )
                        connection.execute(
                            "INSERT INTO proposal_events"
                            " (proposal_id, from_status, to_status, reason, created_at)"
                            " VALUES (?, ?, 'stale', 'sequence_advanced', ?)",
                            (row["proposal_id"], old_status, now),
                        )
                    else:
                        connection.execute(
                            "UPDATE proposals SET status = 'superseded' WHERE proposal_id = ?",
                            (row["proposal_id"],),
                        )
                        connection.execute(
                            "INSERT INTO proposal_events"
                            " (proposal_id, from_status, to_status, reason, created_at)"
                            " VALUES (?, ?, 'superseded', 'registry_or_policy_changed', ?)",
                            (row["proposal_id"], old_status, now),
                        )
                        superseded_id = str(row["proposal_id"])
                    counts["staled"] += 1

                stored = replace(proposal, supersedes_proposal_id=superseded_id)
                if superseded_id is not None:
                    supersedes[stored.proposal_id] = superseded_id
                connection.execute(
                    "INSERT INTO proposals (proposal_id, run_id, source_sequence,"
                    " dispatcher_version, fingerprint, status, payload, created_at,"
                    " supersedes_proposal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stored.proposal_id,
                        stored.run_id,
                        stored.source_sequence,
                        stored.dispatcher_version,
                        stored.fingerprint,
                        stored.proposal_status,
                        json.dumps(stored.to_dict(), ensure_ascii=False, sort_keys=True),
                        now,
                        stored.supersedes_proposal_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO proposal_events"
                    " (proposal_id, from_status, to_status, reason, created_at)"
                    " VALUES (?, NULL, ?, 'scanned', ?)",
                    (stored.proposal_id, stored.proposal_status, now),
                )
                counts["created"] += 1

            # Proposals whose runs are no longer admitted (resolved,
            # superseded by a newer attempt, or otherwise left the queue).
            # Preserved runs (currently executing) keep their route open.
            preserved = preserved_run_ids or set()
            for run_id, rows in by_run.items():
                if (
                    run_id in incoming_runs
                    or run_id in admitted_run_ids
                    or run_id in preserved
                ):
                    continue
                for row in rows:
                    connection.execute(
                        "UPDATE proposals SET status = 'stale' WHERE proposal_id = ?",
                        (row["proposal_id"],),
                    )
                    connection.execute(
                        "INSERT INTO proposal_events"
                        " (proposal_id, from_status, to_status, reason, created_at)"
                        " VALUES (?, ?, 'stale', 'run_left_queue', ?)",
                        (row["proposal_id"], str(row["status"]), now),
                    )
                    counts["staled"] += 1

            cursor = connection.execute(
                "UPDATE dispatcher_runs SET status = 'finished', scanned_runs = ?,"
                " admitted_runs = ?, proposals_created = ?, proposals_reused = ?,"
                " proposals_staled = ?, heartbeat_at = ?, finished_at = ?"
                " WHERE dispatcher_run_id = ? AND owner = ? AND status = 'running'",
                (
                    scanned_runs,
                    len(admitted_run_ids),
                    counts["created"],
                    counts["reused"],
                    counts["staled"],
                    now,
                    now,
                    dispatcher_run_id,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"dispatcher run {dispatcher_run_id} is not a live run owned by"
                    f" this scanner; refusing to finalize (owner check failed)"
                )
            connection.commit()
        return {"counts": counts, "supersedes": supersedes}


def payload_of(row: sqlite3.Row) -> dict[str, Any]:
    """Merge the immutable payload with the authoritative current status.

    The stored JSON payload is immutable after insert; its embedded
    ``proposal_status``/``supersedes_proposal_id`` are the as-scanned
    values. The ``status`` and ``supersedes_proposal_id`` columns are the
    documented mutable current-status index, maintained in the same
    transaction as the append-only events. Readers must use this merge —
    never the raw payload — when they need the effective status.
    """
    payload = dict(json.loads(str(row["payload"])))
    payload["proposal_status"] = str(row["status"])
    supersedes = row["supersedes_proposal_id"]
    payload["supersedes_proposal_id"] = None if supersedes is None else str(supersedes)
    return payload
