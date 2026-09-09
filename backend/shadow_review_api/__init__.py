"""P3C Shadow Review API: nigo's review decisions on shadow proposals.

Accept / defer / reject bookkeeping over an append-only review ledger.
A decision never starts a Worker, advances a node, changes real task
state, calls a model, or touches the production Loop DB, the P2 intent
ledger, the P3A proposal ledger, or any Obsidian note.
"""

from __future__ import annotations

SERVICE_VERSION = "p3c-gate1-2026-07-20.0"
CONTRACT_VERSION = "1"
