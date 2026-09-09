"""Execution dispatcher (frozen contract §5–§8).

Turns an accepted candidate into a single-winner, linearized execution
reservation, then advances exactly one node per dispatch through the
existing ``LoopSupervisor`` with lease, budget reservation, dispatch
permit, and honest receipts. Recovery reconciles only marker-matched
commits and never re-runs a committed node.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from common.checkpoint import SQLiteCheckpointStore
from common.supervisor import LoopSpec, LoopSupervisor, NodeHandler, SupervisorContext
from execution_dispatcher.budget import BudgetEnforcer, BudgetExhaustedError, CostPlan
from execution_dispatcher.candidates import Candidate
from execution_dispatcher.facts import DispatchFacts, FactsProvider
from execution_dispatcher.ledger import ExecutionLedger
from execution_dispatcher.revalidate import revalidate
from shadow_review_api.ledger import DispatchPermitError

RUNNABLE_STATUSES = frozenset({"approved", "running"})
TERMINAL_STATUSES = LoopSupervisor.TERMINAL_STATUSES


def canonical_idempotency_key(decision_id: str, fingerprint: str, source_sequence: int) -> str:
    canonical = "\x1f".join(("p3d-execution", decision_id, fingerprint, str(source_sequence)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def commit_marker(execution_id: str, reservation_id: int, node: str, pre_iteration: int) -> str:
    return (
        f"p3d:exec={execution_id}:res={reservation_id}"
        f":node={node}:pre_iter={pre_iteration}"
    )


def redact_eval_context(handler: NodeHandler) -> NodeHandler:
    """Independent evaluators never see the worker's self-evaluation."""

    def redacting(context: SupervisorContext) -> Any:
        redacted = replace(context.checkpoint, eval_results={})
        return handler(
            SupervisorContext(checkpoint=redacted, store=context.store, spec=context.spec)
        )

    return redacting


class ExecutionDispatcher:
    def __init__(
        self,
        *,
        store: SQLiteCheckpointStore,
        loop_db_path: str,
        ledger: ExecutionLedger,
        review_ledger: Any,
        proposal_by_id: Callable[[str], dict[str, Any] | None],
        facts_provider: FactsProvider,
        spec: LoopSpec,
        handlers: dict[str, NodeHandler],
        evaluator_nodes: frozenset[str] = frozenset(),
    ) -> None:
        missing = set(spec.nodes) - set(handlers)
        extra = set(handlers) - set(spec.nodes)
        if missing or extra:
            raise ValueError(
                "handlers must cover exactly the spec nodes: "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        self.store = store
        self.loop_db_path = str(loop_db_path)
        self.ledger = ledger
        self.review_ledger = review_ledger
        self.proposal_by_id = proposal_by_id
        self.facts_provider = facts_provider
        self.spec = spec
        self.enforcer = BudgetEnforcer(ledger)
        wrapped = dict(handlers)
        for node in evaluator_nodes:
            if node in wrapped:
                wrapped[node] = redact_eval_context(wrapped[node])
        self.supervisor = LoopSupervisor(store, spec, wrapped)

    def _facts(self) -> DispatchFacts:
        """Live facts: reloaded from source on every reserve/dispatch."""
        return self.facts_provider()

    # -- reservation (linearized) ------------------------------------------

    def reserve(self, candidate: Candidate) -> tuple[dict[str, Any] | None, bool, list[str]]:
        """Return (execution row or None, created, rejection reasons)."""
        decision = candidate.decision
        proposal = candidate.proposal
        decision_id = str(decision["decision_id"])
        proposal_id = str(decision["proposal_id"])
        run_id = str(decision["run_id"])
        source_sequence = int(decision["source_sequence"])
        key = canonical_idempotency_key(
            decision_id, str(proposal.get("fingerprint")), source_sequence
        )
        existing = self.ledger.for_decision(decision_id)
        if existing is not None:
            if str(existing["idempotency_key"]) != key:
                return None, False, ["idempotency_key_mismatch"]
            return _row_dict(existing), False, []

        facts = self._facts()
        with self.store.sequence_guard(run_id, source_sequence) as sequence_ok:
            if not sequence_ok:
                return None, False, ["sequence_mismatch"]
            proposal_now = self.proposal_by_id(proposal_id)
            if proposal_now is None:
                return None, False, ["proposal_missing"]
            current = self.review_ledger.current_for(proposal_id)
            current_decision_id = None if current is None else str(current["decision_id"])
            reasons = revalidate(
                candidate,
                store=self.store,
                loop_db_path=self.loop_db_path,
                review_current_decision_id=current_decision_id,
                proposal_now=proposal_now,
                facts=facts,
                spec=self.spec,
            )
            if reasons:
                return None, False, reasons
            row, created = self.ledger.reserve_execution(
                decision_id=decision_id,
                idempotency_key=key,
                proposal_id=proposal_id,
                run_id=run_id,
                fragment_id=str(decision["fragment_id"]),
                loopspec_id=str(proposal.get("proposed_loopspec")),
                loopspec_version=str(proposal.get("loopspec_version")),
                fingerprint=str(proposal.get("fingerprint")),
                source_sequence=source_sequence,
            )
            if created:
                self.ledger.add_event(
                    str(row["execution_id"]),
                    "revalidated",
                    {"checks": "all", "source_sequence": source_sequence},
                )
            return _row_dict(row), created, []

    # -- pre-dispatch revalidation (linearized, every dispatch) --------------

    def _expected_sequence(self, row: Any) -> int:
        """The sequence the run must still be at before this dispatch."""
        events = self.ledger.events_for(str(row["execution_id"]))
        committed = [e for e in events if e["event_type"] == "node_committed"]
        if committed:
            return int(committed[-1]["detail"]["new_sequence"])
        requeued = [
            e
            for e in events
            if e["event_type"] == "recovered" and e["detail"].get("mode") == "requeue"
        ]
        if requeued and requeued[-1]["detail"].get("baseline_sequence") is not None:
            return int(requeued[-1]["detail"]["baseline_sequence"])
        return int(row["source_sequence"])

    def _revalidate_for_dispatch(self, row: Any) -> tuple[str, Any]:
        """("stop", status) | ("reject", reasons) | ("ok", checkpoint)."""
        run_id = str(row["run_id"])
        base_sequence = self._expected_sequence(row)
        # A foreign node commit (any node_completed/loop_stopped after our
        # last receipt that does not carry THIS execution's marker) means
        # someone else advanced the run — reject. Control-plane writes
        # (pause/resume transitions) are not commits and never cause a
        # false mismatch.
        marker_prefix = f"p3d:exec={row['execution_id']}"
        foreign_commits = [
            event
            for event in self.store.events_after(run_id, base_sequence)
            if event[1] in ("node_completed", "loop_stopped")
            and (event[4] is None or marker_prefix not in event[4])
        ]
        facts = self._facts()
        latest_sequence = self.store.latest_sequence(run_id) or base_sequence
        with self.store.sequence_guard(run_id, latest_sequence) as sequence_ok:
            latest = self.store.latest(run_id)
            if latest is None or latest.status not in RUNNABLE_STATUSES:
                return "stop", ("missing" if latest is None else latest.status)
            if not sequence_ok:
                return "reject", ["sequence_mismatch"]
            if foreign_commits:
                return "reject", ["sequence_mismatch:foreign_commit"]
            decision = self.review_ledger.get(str(row["decision_id"]))
            if decision is None:
                return "reject", ["decision_missing"]
            current = self.review_ledger.current_for(str(row["proposal_id"]))
            if current is None:
                return "reject", ["decision_missing"]
            if str(current["decision_id"]) != str(row["decision_id"]):
                if str(current["decision"]) != "accepted":
                    return "reject", [f"decision_not_accepted:{current['decision']}"]
                return "reject", ["decision_superseded"]
            if str(decision["decision"]) != "accepted":
                return "reject", [f"decision_not_accepted:{decision['decision']}"]
            proposal = self.proposal_by_id(str(row["proposal_id"]))
            if proposal is None:
                return "reject", ["proposal_missing"]
            candidate = Candidate(decision=dict(decision), proposal=dict(proposal))
            reasons = revalidate(
                candidate,
                store=self.store,
                loop_db_path=self.loop_db_path,
                review_current_decision_id=str(current["decision_id"]),
                proposal_now=proposal,
                facts=facts,
                spec=self.spec,
                expected_statuses=RUNNABLE_STATUSES,
            )
            # the execution reservation's pinned identity must match verbatim
            if str(row["loopspec_id"]) != str(self.spec.loop_id):
                reasons.append(
                    f"loopspec_id_mismatch:execution={row['loopspec_id']},spec={self.spec.loop_id}"
                )
            if str(row["loopspec_version"]) != str(self.spec.version):
                reasons.append(
                    "loopspec_version_mismatch:"
                    f"execution={row['loopspec_version']},spec={self.spec.version}"
                )
            if reasons:
                return "reject", reasons
            return "ok", latest

    # -- dispatch (exactly one node; lease spans the full accounting) ---------

    def dispatch_once(
        self,
        execution_id: str,
        *,
        cost_plan_of: Callable[[str], CostPlan],
        owner: str | None = None,
        step_observer: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Advance exactly one node.

        ``step_observer`` is an optional test seam invoked with a step
        name right after each durable write (``budget_reserved``,
        ``permit_issued``, ``dispatched``, ``node_run``, ``settled``,
        ``permit_committed``, ``receipt_written``). It enables real
        process-crash interleavings; production callers pass nothing.
        """
        row = self.ledger.get(execution_id)
        if row is None:
            raise KeyError(f"unknown execution {execution_id}")
        if str(row["status"]) not in ("reserved", "dispatched"):
            return {"execution_id": execution_id, "status": str(row["status"]), "advanced": False}
        run_id = str(row["run_id"])

        # The lease is the mutual-exclusion primitive for the WHOLE dispatch
        # cycle: revalidation, execution, and accounting. Claiming is an
        # atomic status-checked CAS; the returned row is authoritative.
        owner = owner or f"exec-{uuid.uuid4().hex}"
        row = self.ledger.claim_lease(execution_id, owner=owner)
        try:
            outcome, payload = self._revalidate_for_dispatch(row)
            if outcome == "stop":
                run_status = payload
                if run_status in ("pause_requested", "paused"):
                    # finalize the pause through the supervisor (no node
                    # runs) so a later resume is possible, and keep the
                    # execution resumable instead of terminally stopped
                    finalized = self.supervisor.run(run_id, max_steps=1)
                    run_status = finalized.status
                    self.ledger.void_open_reservations(
                        execution_id, f"run_not_runnable:{run_status}"
                    )
                    self.ledger.add_event(
                        execution_id, "stop_honored", {"run_status": run_status}
                    )
                    self.ledger.update_status(
                        execution_id, "reserved", owner=owner, require_owner=True
                    )
                    return {
                        "execution_id": execution_id,
                        "status": "paused",
                        "advanced": False,
                        "run_status": run_status,
                    }
                self.ledger.void_open_reservations(execution_id, f"run_not_runnable:{payload}")
                self.ledger.add_event(execution_id, "stop_honored", {"run_status": payload})
                self.ledger.update_status(execution_id, "stopped", owner=owner, require_owner=True)
                return {"execution_id": execution_id, "status": "stopped", "advanced": False}
            if outcome == "reject":
                self.ledger.add_event(execution_id, "dispatch_rejected", {"reasons": payload})
                self.ledger.update_status(execution_id, "stopped", owner=owner, require_owner=True)
                return {
                    "execution_id": execution_id,
                    "status": "rejected",
                    "reasons": payload,
                    "advanced": False,
                }
            checkpoint = payload

            node = checkpoint.current_node
            proposal = self.proposal_by_id(str(row["proposal_id"])) or {}
            limits = dict((proposal.get("budget") or {}).get("limits") or {})
            plan = cost_plan_of(node)
            with self.ledger.heartbeat(execution_id, owner):
                try:
                    reservation_id = self.enforcer.reserve(execution_id, node, plan, limits)
                except BudgetExhaustedError as error:
                    self.ledger.update_status(
                        execution_id, "budget_exhausted", owner=owner, require_owner=True
                    )
                    return {
                        "execution_id": execution_id,
                        "status": "budget_exhausted",
                        "reason": error.reason,
                        "advanced": False,
                    }
                if step_observer is not None:
                    step_observer("budget_reserved")

                pre_sequence = self.store.latest_sequence(run_id)
                pre_used = dict(checkpoint.budget_used or {})
                pre_iteration = int(checkpoint.iteration or 0)
                # Immutable dispatch_started permit in the review ledger:
                # if the current decision moved first, the dispatch dies here;
                # if the permit lands first, a later decision takes effect
                # from the NEXT node boundary.
                try:
                    permit, _ = self.review_ledger.issue_dispatch_permit(
                        permit_id=f"permit-{execution_id}-{node}-{pre_iteration}",
                        execution_id=execution_id,
                        decision_id=str(row["decision_id"]),
                        proposal_id=str(row["proposal_id"]),
                        run_id=run_id,
                        node=node,
                        pre_iteration=pre_iteration,
                        run_sequence=pre_sequence or 0,
                    )
                except DispatchPermitError as error:
                    # blocker 1 (R3): a permit failure AFTER the budget
                    # reservation must deterministically void it with an
                    # auditable reason — never leave it open.
                    self.ledger.void_open_reservations(
                        execution_id, f"permit_rejected:{error}"
                    )
                    self.ledger.add_event(
                        execution_id, "dispatch_rejected", {"reasons": [str(error)]}
                    )
                    self.ledger.update_status(
                        execution_id, "stopped", owner=owner, require_owner=True
                    )
                    return {
                        "execution_id": execution_id,
                        "status": "rejected",
                        "reasons": [str(error)],
                        "advanced": False,
                    }
                if step_observer is not None:
                    step_observer("permit_issued")
                self.ledger.add_event(
                    execution_id,
                    "dispatched",
                    {
                        "node": node,
                        "pre_sequence": pre_sequence,
                        "pre_used": pre_used,
                        "pre_iteration": pre_iteration,
                        "reservation_id": reservation_id,
                        "permit_id": permit["permit_id"],
                    },
                )
                self.ledger.update_status(
                    execution_id, "dispatched", owner=owner, require_owner=True
                )
                if step_observer is not None:
                    step_observer("dispatched")
                marker = commit_marker(execution_id, reservation_id, node, pre_iteration)
                result = self.supervisor.run(run_id, max_steps=1, revision_suffix=marker)
                if step_observer is not None:
                    step_observer("node_run")

                post_used = dict(result.budget_used or {})
                delta_tokens = int(post_used.get("tokens", 0) or 0) - int(
                    pre_used.get("tokens", 0) or 0
                )
                delta_calls = int(post_used.get("tool_calls", 0) or 0) - int(
                    pre_used.get("tool_calls", 0) or 0
                )
                new_sequence = self.store.latest_sequence(run_id)
                node_committed = int(result.iteration or 0) > pre_iteration
                if not node_committed and result.status not in RUNNABLE_STATUSES:
                    # A pause/terminate/stop applied during execution won over
                    # the handler result; the node did not commit. Honor it,
                    # never overwrite it.
                    self.enforcer.settle(
                        reservation_id, execution_id, actual_tokens=0, actual_tool_calls=0
                    )
                    self.review_ledger.void_permit(
                        permit["permit_id"], reason=f"stop_honored:{result.status}"
                    )
                    self.ledger.add_event(
                        execution_id, "stop_honored", {"run_status": result.status}
                    )
                    self.ledger.update_status(
                        execution_id, "stopped", owner=owner, require_owner=True
                    )
                    return {
                        "execution_id": execution_id,
                        "status": "stopped",
                        "advanced": False,
                        "run_status": result.status,
                    }
                self.enforcer.settle(
                    reservation_id,
                    execution_id,
                    actual_tokens=delta_tokens,
                    actual_tool_calls=delta_calls,
                )
                if step_observer is not None:
                    step_observer("settled")
                self.review_ledger.mark_permit_committed(permit["permit_id"])
                if step_observer is not None:
                    step_observer("permit_committed")
                self.ledger.add_event(
                    execution_id,
                    "node_committed",
                    {
                        "node": node,
                        "new_sequence": new_sequence,
                        "status": result.status,
                        "budget_delta": {"tokens": delta_tokens, "tool_calls": delta_calls},
                        "marker": marker,
                    },
                )
                if step_observer is not None:
                    step_observer("receipt_written")
                if result.status in TERMINAL_STATUSES:
                    self.ledger.update_status(
                        execution_id, "completed", owner=owner, require_owner=True
                    )
                else:
                    self.ledger.update_status(
                        execution_id, "reserved", owner=owner, require_owner=True
                    )
                return {
                    "execution_id": execution_id,
                    "status": "completed" if result.status in TERMINAL_STATUSES else "advanced",
                    "advanced": True,
                    "node": node,
                    "run_status": result.status,
                    "new_sequence": new_sequence,
                    "budget_delta": {"tokens": delta_tokens, "tool_calls": delta_calls},
                }
        finally:
            try:
                self.ledger.release_lease(execution_id, owner=owner)
            except RuntimeError:
                self.ledger.add_event(
                    execution_id, "lease_release_conflict", {"owner": owner}
                )

    def run_until_done(
        self,
        execution_id: str,
        *,
        cost_plan_of: Callable[[str], CostPlan],
        max_nodes: int = 32,
    ) -> dict[str, Any]:
        receipts = []
        for _ in range(max_nodes):
            receipt = self.dispatch_once(execution_id, cost_plan_of=cost_plan_of)
            receipts.append(receipt)
            if not receipt.get("advanced") or receipt.get("status") != "advanced":
                break
        row = self.ledger.get(execution_id)
        return {
            "execution_id": execution_id,
            "final_status": None if row is None else str(row["status"]),
            "nodes_advanced": sum(1 for r in receipts if r.get("advanced")),
            "receipts": receipts,
        }

    # -- deterministic crash recovery (contract §8) --------------------------

    def recover(self) -> dict[str, Any]:
        expired = self.ledger.fail_interrupted_executions()
        report: list[dict[str, Any]] = []
        for execution_id in expired:
            row = self.ledger.get(execution_id)
            if row is None:
                continue
            run_id = str(row["run_id"])
            events = self.ledger.events_for(execution_id)
            dispatched = next(
                (e for e in reversed(events) if e["event_type"] == "dispatched"), None
            )
            latest = self.store.latest(run_id)
            pre_sequence = (
                int(dispatched["detail"].get("pre_sequence") or 0)
                if dispatched is not None
                else int(row["source_sequence"])
            )
            pre_iteration = (
                int(dispatched["detail"].get("pre_iteration") or 0)
                if dispatched is not None
                else 0
            )
            # A commit counts only when it carries THIS execution's marker:
            # event type + iteration advance + the execution/reservation
            # marker in revision_reason. Anything else (control writes,
            # another supervisor, manual recovery) is not ours.
            marker_prefix = f"p3d:exec={execution_id}"
            reservation_id = (
                None
                if dispatched is None
                else dispatched["detail"].get("reservation_id")
            )
            commits = self.store.events_after(run_id, pre_sequence)
            ours = any(
                event_type in ("node_completed", "loop_stopped")
                and iteration > pre_iteration
                and revision_reason is not None
                and marker_prefix in revision_reason
                and (
                    reservation_id is None
                    or f":res={reservation_id}:" in revision_reason
                )
                for _, event_type, _, iteration, revision_reason in commits
            )
            open_reservations = self.ledger.open_reservations(execution_id)
            # settle-from-history may only close reservations of the
            # dispatched boundary itself. An open reservation for a LATER
            # attempt (permit issued, dispatched never written — the
            # crash-after-permit_issued window on any boundary after the
            # first) must requeue, never settle against history: its node
            # never ran, so history cannot price it.
            open_is_dispatched_boundary = (
                reservation_id is not None
                and all(
                    int(reservation["reservation_id"]) == int(reservation_id)
                    for reservation in open_reservations
                )
            )
            if (
                dispatched is not None
                and ours
                and latest is not None
                and (not open_reservations or open_is_dispatched_boundary)
            ):
                pre_used = dict(dispatched["detail"].get("pre_used") or {})
                post_used = dict(latest.budget_used or {})
                delta_tokens = int(post_used.get("tokens", 0) or 0) - int(
                    pre_used.get("tokens", 0) or 0
                )
                delta_calls = int(post_used.get("tool_calls", 0) or 0) - int(
                    pre_used.get("tool_calls", 0) or 0
                )
                for reservation in open_reservations:
                    self.ledger.settle_reservation(
                        int(reservation["reservation_id"]),
                        execution_id,
                        actual_tokens=delta_tokens,
                        actual_tool_calls=delta_calls,
                    )
                new_sequence = self.store.latest_sequence(run_id)
                attempt_permit = self.review_ledger.permit_for_attempt(
                    execution_id,
                    str(dispatched["detail"].get("node")),
                    pre_iteration,
                )
                if attempt_permit is not None and attempt_permit["status"] == "started":
                    self.review_ledger.mark_permit_committed(attempt_permit["permit_id"])
                receipt_exists = any(
                    e["event_type"] == "node_committed"
                    and e["detail"].get("node") == dispatched["detail"].get("node")
                    for e in events
                )
                if not receipt_exists:
                    self.ledger.add_event(
                        execution_id,
                        "node_committed",
                        {
                            "node": dispatched["detail"].get("node"),
                            "new_sequence": new_sequence,
                            "status": latest.status,
                            "budget_delta": {"tokens": delta_tokens, "tool_calls": delta_calls},
                            "recovered": True,
                        },
                    )
                if latest.status in TERMINAL_STATUSES:
                    self.ledger.update_status(execution_id, "completed")
                else:
                    self.ledger.update_status(execution_id, "reserved")
                self.ledger.add_event(
                    execution_id, "recovered", {"mode": "settled_from_history"}
                )
                report.append({"execution_id": execution_id, "mode": "settled_from_history"})
            elif latest is not None and latest.status not in RUNNABLE_STATUSES:
                # a pure control-plane stop (pause/terminate) moved the
                # sequence without committing any node: honor it, never
                # fabricate a node completion.
                self.ledger.void_open_reservations(execution_id, "stop_before_commit")
                self.ledger.add_event(
                    execution_id, "stop_honored", {"run_status": latest.status}
                )
                self.ledger.update_status(execution_id, "stopped")
                self.ledger.add_event(execution_id, "recovered", {"mode": "stop_honored"})
                report.append({"execution_id": execution_id, "mode": "stop_honored"})
            else:
                self.ledger.void_open_reservations(execution_id, "crash_before_commit")
                self.ledger.update_status(execution_id, "reserved")
                self.ledger.add_event(
                    execution_id,
                    "recovered",
                    {
                        "mode": "requeue",
                        "baseline_sequence": self.store.latest_sequence(run_id),
                    },
                )
                report.append({"execution_id": execution_id, "mode": "requeue"})
        return {"recovered": report, "count": len(report)}


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}
