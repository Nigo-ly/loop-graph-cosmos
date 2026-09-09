"""P2A Control API: audited human control intents for the Loop Supervisor.

Separate from the read-only Projection API (port 5679, GET-only). This
package serves the loopback Control API on port 5680, persists an append-only
``control_intents.sqlite3`` ledger, and applies validated intents
(pause/resume/terminate/retry/priority) through the existing checkpoint
store. The Loop database is only ever appended to through
``SQLiteCheckpointStore``; history is never rewritten or deleted.
"""

from __future__ import annotations

from pathlib import Path

import os

SERVICE_VERSION = "p2a-2026-07-19.0"
CONTRACT_VERSION = "1"

DEFAULT_LOOP_DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "loop_state.sqlite3")
DEFAULT_LEDGER_PATH = str(Path(__file__).resolve().parents[1] / "data" / "control_intents.sqlite3")

# Overridable for embedding/tests; the CLI flags take precedence.
LOOP_DB_PATH = os.environ.get("CONTROL_API_LOOP_DB", DEFAULT_LOOP_DB_PATH)
LEDGER_PATH = os.environ.get("CONTROL_API_LEDGER", DEFAULT_LEDGER_PATH)
