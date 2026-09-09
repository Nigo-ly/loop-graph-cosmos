"""Read-only Projection API provider for the P1 contract (v2).

Serves /loop/v1/* from the live Loop SQLite checkpoint store without ever
mutating it. Every database connection is opened with mode=ro and
PRAGMA query_only=ON.
"""

from __future__ import annotations

from pathlib import Path

import os

PROVIDER_VERSION = "p1a-2026-07-19.1"
CONTRACT_VERSION = "2"

DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "loop_state.sqlite3")

# Overridable for embedding/tests via the PROJECTION_API_DB environment
# variable; the CLI --db flag takes precedence over both.
DB_PATH = os.environ.get("PROJECTION_API_DB", DEFAULT_DB_PATH)
