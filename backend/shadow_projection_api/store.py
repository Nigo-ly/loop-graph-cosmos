"""Read-only access to a P3A shadow ledger.

Every connection is opened ``mode=ro`` with ``PRAGMA query_only=ON``. A
missing ledger is a first-class state (``ledger_present=False``), never an
error that would create the file. Status semantics follow the frozen
ledger contract: the immutable JSON payload merged with the mutable
current-status index via ``shadow_dispatcher.ledger.payload_of``.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from shadow_dispatcher.ledger import payload_of
from shadow_dispatcher.proposals import STALE, SUPERSEDED

HISTORY_STATUSES = frozenset({STALE, SUPERSEDED})


def ledger_present(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.R_OK)


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def source_marker(path: str) -> int:
    """Max committed proposal event id; 0 for an empty ledger."""
    with closing(_connect(path)) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) AS marker FROM proposal_events"
        ).fetchone()
    return int(row["marker"])


def _last_events(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in connection.execute(
        "SELECT e.* FROM proposal_events e"
        " JOIN (SELECT proposal_id, MAX(event_id) AS max_id"
        "       FROM proposal_events GROUP BY proposal_id) latest"
        " ON e.proposal_id = latest.proposal_id AND e.event_id = latest.max_id"
    ):
        latest[str(row["proposal_id"])] = row
    return latest


def _item(row: sqlite3.Row, last_event: sqlite3.Row | None) -> dict[str, Any]:
    item = payload_of(row)
    item["last_event"] = (
        None
        if last_event is None
        else {
            "to_status": str(last_event["to_status"]),
            "reason": str(last_event["reason"]),
            "created_at": str(last_event["created_at"]),
        }
    )
    return item


def list_proposals(path: str, scope: str, limit: int) -> list[dict[str, Any]]:
    """Merged proposal items. ``scope``: current | history | all."""
    with closing(_connect(path)) as connection:
        rows = list(connection.execute("SELECT * FROM proposals"))
        last_events = _last_events(connection)
    items = [_item(row, last_events.get(str(row["proposal_id"]))) for row in rows]
    current = [item for item in items if item["proposal_status"] not in HISTORY_STATUSES]
    history = [item for item in items if item["proposal_status"] in HISTORY_STATUSES]
    current.sort(key=lambda item: (str(item["subject"]), str(item["proposal_id"])))
    history.sort(
        key=lambda item: (
            str((item["last_event"] or {}).get("created_at") or ""),
            str(item["proposal_id"]),
        ),
        reverse=True,
    )
    if scope == "current":
        ordered = current
    elif scope == "history":
        ordered = history
    else:  # all: current first, then history
        ordered = current + history
    return ordered[:limit]


def get_proposal(path: str, proposal_id: str) -> dict[str, Any] | None:
    """One merged proposal with its full append-only event list."""
    with closing(_connect(path)) as connection:
        row = connection.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        events = [
            {
                "from_status": (
                    None if event["from_status"] is None else str(event["from_status"])
                ),
                "to_status": str(event["to_status"]),
                "reason": str(event["reason"]),
                "created_at": str(event["created_at"]),
            }
            for event in connection.execute(
                "SELECT * FROM proposal_events WHERE proposal_id = ? ORDER BY event_id",
                (proposal_id,),
            )
        ]
        last_events = _last_events(connection)
    item = _item(row, last_events.get(proposal_id))
    item["events"] = events
    return item
