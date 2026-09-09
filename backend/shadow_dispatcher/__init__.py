"""P3A Shadow Dispatcher: deterministic routing proposals for the approved queue.

Reads the Loop queue strictly read-only, applies frozen registry/policy
facts, and appends shadow routing proposals to its own ledger. It never
calls a model, never starts a Worker, never advances a node, and never
writes to the Loop database or the P2 intent ledger. Proposals are advice
only; nothing here executes anything.
"""

from __future__ import annotations

DISPATCHER_VERSION = "p3a-2026-07-20.3"
