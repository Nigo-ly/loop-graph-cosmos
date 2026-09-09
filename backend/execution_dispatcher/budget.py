"""Pre-action budget reservation and post-action settlement (contract §6).

The supervisor's ``_budget_stop`` is the post-hoc backstop; this enforcer
is the pre-action hard gate: a node is never dispatched when the
projected usage (settled actuals + this reservation) would exceed any
proposal limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from execution_dispatcher.ledger import ExecutionLedger


@dataclass(frozen=True)
class CostPlan:
    tokens: int
    tool_calls: int


class BudgetExhaustedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetEnforcer:
    def __init__(self, ledger: ExecutionLedger) -> None:
        self.ledger = ledger

    def reserve(
        self,
        execution_id: str,
        node: str,
        plan: CostPlan,
        limits: dict[str, Any],
    ) -> int:
        """Open a reservation or raise BudgetExhaustedError before any action."""
        totals = self.ledger.budget_totals(execution_id)
        token_limit = limits.get("tokens")
        call_limit = limits.get("tool_calls")
        if token_limit is not None and (
            totals["settled_tokens"] + totals["open_tokens"] + plan.tokens
            > int(token_limit)
        ):
            self.ledger.add_event(
                execution_id,
                "budget_exhausted",
                {
                    "kind": "tokens",
                    "projected": totals["settled_tokens"] + totals["open_tokens"] + plan.tokens,
                    "limit": int(token_limit),
                    "node": node,
                },
            )
            raise BudgetExhaustedError("token_budget_exhausted")
        if call_limit is not None and (
            totals["settled_tool_calls"] + totals["open_tool_calls"] + plan.tool_calls
            > int(call_limit)
        ):
            self.ledger.add_event(
                execution_id,
                "budget_exhausted",
                {
                    "kind": "tool_calls",
                    "projected": (
                        totals["settled_tool_calls"] + totals["open_tool_calls"] + plan.tool_calls
                    ),
                    "limit": int(call_limit),
                    "node": node,
                },
            )
            raise BudgetExhaustedError("tool_call_budget_exhausted")
        return self.ledger.open_reservation(
            execution_id, node, plan.tokens, plan.tool_calls
        )

    def settle(
        self,
        reservation_id: int,
        execution_id: str,
        *,
        actual_tokens: int,
        actual_tool_calls: int,
    ) -> None:
        self.ledger.settle_reservation(
            reservation_id,
            execution_id,
            actual_tokens=actual_tokens,
            actual_tool_calls=actual_tool_calls,
        )

    def void_open(self, execution_id: str, reason: str) -> None:
        self.ledger.void_open_reservations(execution_id, reason)
