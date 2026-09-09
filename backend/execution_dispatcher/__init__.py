"""P3D execution dispatcher: accepted proposal → leased, budgeted,
recoverable node advancement through the existing LoopSupervisor.

Gate 1 is isolated-only: temporary databases, registries, health
snapshots, and a deterministic side-effect-free test LoopSpec.
"""

from execution_dispatcher.budget import BudgetEnforcer, BudgetExhaustedError, CostPlan
from execution_dispatcher.candidates import Candidate, Rejection, select_candidates
from execution_dispatcher.executor import ExecutionDispatcher, canonical_idempotency_key
from execution_dispatcher.facts import DispatchFacts, file_facts_provider
from execution_dispatcher.ledger import ExecutionLedger

EXECUTION_DISPATCHER_VERSION = "p3d-2026-07-21.0"

__all__ = [
    "EXECUTION_DISPATCHER_VERSION",
    "BudgetEnforcer",
    "BudgetExhaustedError",
    "Candidate",
    "CostPlan",
    "DispatchFacts",
    "ExecutionDispatcher",
    "ExecutionLedger",
    "Rejection",
    "canonical_idempotency_key",
    "file_facts_provider",
    "select_candidates",
]
