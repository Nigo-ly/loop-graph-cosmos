"""Read-only access layer for the Loop checkpoint SQLite store.

Every public function opens its own short-lived connection via _connect(),
which uses ``mode=ro`` and immediately issues ``PRAGMA query_only=ON``.
Only SELECT/PRAGMA statements are ever executed. A missing or unreadable
database raises sqlite3.Error; the provider maps that to the contract error
shape and never creates the file.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from common.execution import legacy_flat_eval_view

CONNECT_TIMEOUT_SECONDS = 2


def _connect(path: str) -> sqlite3.Connection:
    """Open the database strictly read-only. Tests may assert query_only==1."""
    connection = sqlite3.connect(
        "file:" + str(path) + "?mode=ro",
        uri=True,
        timeout=CONNECT_TIMEOUT_SECONDS,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


@dataclass(frozen=True)
class RunRow:
    """The latest checkpoint row for one run_id plus its event count."""

    sequence: int
    run_id: str
    loop_id: str
    fragment_id: str
    status: str
    current_node: str | None
    event_type: str
    revision_reason: str | None
    committed_at: str
    payload: dict[str, Any]
    events: int


@dataclass(frozen=True)
class HistoryRow:
    """One checkpoint row in committed order."""

    sequence: int
    run_id: str
    loop_id: str
    fragment_id: str
    event_type: str
    current_node: str | None
    status: str
    revision_reason: str | None
    committed_at: str
    payload: dict[str, Any]


def _parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    eval_results = value.get("eval_results")
    if isinstance(eval_results, dict):
        # Gate 1：投影边界统一暴露 legacy_flat_v1 只读兼容视图；存储态
        # （flat 旧行或 system/plugins 新行）永不改写。
        value = {
            **value,
            "eval_results": dict(legacy_flat_eval_view(eval_results)),
        }
    return value


def source_marker(path: str) -> tuple[int, str | None]:
    """Return (source_sequence, source_committed_at) for the latest row.

    An empty checkpoints table yields (0, None).
    """
    with closing(_connect(path)) as connection:
        row = connection.execute(
            "SELECT sequence, committed_at FROM checkpoints "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return (0, None)
    return (int(row["sequence"]), str(row["committed_at"]))


def latest_runs(path: str) -> list[RunRow]:
    """Return the latest checkpoint row per run_id with the run's event count."""
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            """
            SELECT c.sequence, c.run_id, c.loop_id, c.fragment_id, c.status,
                   c.current_node, c.event_type, c.revision_reason,
                   c.committed_at, c.payload_json,
                   (SELECT COUNT(*) FROM checkpoints x
                     WHERE x.run_id = c.run_id) AS events
            FROM checkpoints c
            JOIN (
                SELECT run_id, MAX(sequence) AS max_sequence
                FROM checkpoints GROUP BY run_id
            ) latest
              ON latest.run_id = c.run_id AND latest.max_sequence = c.sequence
            ORDER BY c.sequence ASC
            """
        ).fetchall()
    return [
        RunRow(
            sequence=int(row["sequence"]),
            run_id=str(row["run_id"]),
            loop_id=str(row["loop_id"]),
            fragment_id=str(row["fragment_id"]),
            status=str(row["status"]),
            current_node=(
                None if row["current_node"] is None else str(row["current_node"])
            ),
            event_type=str(row["event_type"]),
            revision_reason=(
                None if row["revision_reason"] is None else str(row["revision_reason"])
            ),
            committed_at=str(row["committed_at"]),
            payload=_parse_payload(row["payload_json"]),
            events=int(row["events"]),
        )
        for row in rows
    ]


def run_history(path: str, run_id: str) -> list[HistoryRow]:
    """Return every checkpoint row for run_id, ordered by sequence ASC."""
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            """
            SELECT sequence, run_id, loop_id, fragment_id, event_type,
                   current_node, status, revision_reason, committed_at,
                   payload_json
            FROM checkpoints
            WHERE run_id = ?
            ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
    return [
        HistoryRow(
            sequence=int(row["sequence"]),
            run_id=str(row["run_id"]),
            loop_id=str(row["loop_id"]),
            fragment_id=str(row["fragment_id"]),
            event_type=str(row["event_type"]),
            current_node=(
                None if row["current_node"] is None else str(row["current_node"])
            ),
            status=str(row["status"]),
            revision_reason=(
                None if row["revision_reason"] is None else str(row["revision_reason"])
            ),
            committed_at=str(row["committed_at"]),
            payload=_parse_payload(row["payload_json"]),
        )
        for row in rows
    ]


def latest_run_id_for_loop(path: str, loop_id: str) -> str | None:
    """Return the run_id of the latest checkpoint row for a loop_id, if any."""
    with closing(_connect(path)) as connection:
        row = connection.execute(
            "SELECT run_id FROM checkpoints WHERE loop_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (loop_id,),
        ).fetchone()
    return None if row is None else str(row["run_id"])
