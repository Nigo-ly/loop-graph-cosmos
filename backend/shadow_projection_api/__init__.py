"""P3B Shadow Proposal Projection API: read-only display of P3A proposals.

Serves the merged authoritative view of the shadow ledger over a
loopback-only, GET-only stdlib HTTP surface. It never creates a ledger,
never writes anywhere, never calls a model, and never touches the
production Loop database or the P2 intent ledger.
"""

from __future__ import annotations

PROVIDER_VERSION = "p3b-2026-07-20.0"
CONTRACT_VERSION = "1"
