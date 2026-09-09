"""Deterministic P2A control intent validator and executor adapter.

Flow for one submission:
1. strict payload validation (types, ranges, fixed action vocabulary);
2. canonical idempotency key recomputed and compared — duplicates return the
   original receipt;
3. freshness: expected_sequence must equal the run's latest committed
   checkpoint sequence, else 409 with no writes;
4. frozen state matrix plus resume/retry LoopSpec feasibility checks;
5. reserve (pending -> accepted), then apply synchronously.

Applied actions only APPEND checkpoints through ``SQLiteCheckpointStore``
(except priority, which writes dispatcher metadata to the ledger only).
Every application is idempotent via a per-intent ``revision_reason`` marker
and, for retry, a deterministic child run id, so a crash between the
checkpoint append and the ledger transition recovers safely via
``recover()``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any, cast

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore
from common.supervisor import AdmissionState, LoopSpec

from .intents import SQLiteIntentLedger

ACTIONS = ("pause", "resume", "terminate", "retry", "priority")

# Frozen state matrix from TASK-P2.md. Do not extend without a new contract.
STATE_MATRIX: dict[str, frozenset[str]] = {
    "pause": frozenset({"running"}),
    "resume": frozenset({"paused"}),
    "terminate": frozenset({"approved", "running", "paused", "blocked", "escalated"}),
    "retry": frozenset({"blocked", "failed_safe", "escalated", "exhausted"}),
    "priority": frozenset({"approved", "paused"}),
}

PRIORITY_MIN = -10
PRIORITY_MAX = 10
MAX_RUN_ID_LENGTH = 200

_IDEMPOTENCY_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class ControlError(Exception):
    """Stable public error: HTTP status plus machine-readable code."""

    def __init__(self, http_status: int, code: str, message: str):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RegisteredLoop:
    """A pinned LoopSpec plus the node names with an available handler."""

    spec: LoopSpec
    handler_nodes: frozenset[str]


@dataclass(frozen=True)
class IntentPayload:
    run_id: str
    action: str
    expected_sequence: int
    idempotency_key: str
    arguments: dict[str, Any]
    requester: str


def canonical_idempotency_key(
    requester: str,
    run_id: str,
    action: str,
    expected_sequence: int,
    arguments: dict[str, Any],
) -> str:
    """Deterministic key from requester, run, action, sequence, and arguments."""
    canonical = "\x1f".join(
        (
            requester,
            run_id,
            action,
            str(expected_sequence),
            json.dumps(arguments, sort_keys=True, separators=(",", ":")),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def retry_child_run_id(run_id: str, intent_id: str) -> str:
    """Deterministic child run id, so re-applying a retry is idempotent."""
    return f"{run_id}--retry--{intent_id}"


def parse_intent_payload(body: object, *, trusted_requester: str) -> IntentPayload:
    """Strictly validate the POST body. Raises ControlError(400) on any flaw."""
    if not isinstance(body, dict):
        raise ControlError(400, "invalid_body", "JSON object body required")
    allowed = {"run_id", "action", "expected_sequence", "idempotency_key", "arguments", "requester"}
    unknown = set(body) - allowed
    if unknown:
        raise ControlError(400, "invalid_body", "Unknown body fields")
    missing = allowed - set(body)
    if missing:
        raise ControlError(400, "invalid_body", "Missing body fields")

    run_id = body["run_id"]
    if (
        not isinstance(run_id, str)
        or not run_id
        or len(run_id) > MAX_RUN_ID_LENGTH
        or "\x00" in run_id
    ):
        raise ControlError(400, "invalid_run_id", "run_id must be a non-empty string")

    action = body["action"]
    if not isinstance(action, str) or action not in STATE_MATRIX:
        raise ControlError(400, "invalid_action", "Unknown action")

    expected_sequence = body["expected_sequence"]
    if isinstance(expected_sequence, bool) or not isinstance(expected_sequence, int):
        raise ControlError(400, "invalid_expected_sequence", "expected_sequence must be an integer")
    if expected_sequence < 1:
        raise ControlError(400, "invalid_expected_sequence", "expected_sequence must be >= 1")

    idempotency_key = body["idempotency_key"]
    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise ControlError(
            400, "invalid_idempotency_key", "idempotency_key must be 64 lowercase hex chars"
        )

    arguments = body["arguments"]
    if not isinstance(arguments, dict):
        raise ControlError(400, "invalid_arguments", "arguments must be an object")
    if action == "priority":
        if set(arguments) != {"priority"}:
            raise ControlError(400, "invalid_arguments", "priority action requires {priority}")
        priority = arguments["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ControlError(400, "invalid_arguments", "priority must be an integer")
        if priority < PRIORITY_MIN or priority > PRIORITY_MAX:
            raise ControlError(400, "invalid_arguments", "priority out of range")
    elif arguments:
        raise ControlError(400, "invalid_arguments", "arguments must be {} for this action")

    requester = body["requester"]
    if not isinstance(requester, str) or requester != trusted_requester:
        raise ControlError(400, "invalid_requester", "Untrusted requester")

    return IntentPayload(
        run_id=run_id,
        action=action,
        expected_sequence=expected_sequence,
        idempotency_key=idempotency_key,
        arguments=dict(arguments),
        requester=requester,
    )


class ControlService:
    """Validate, reserve, apply, and recover control intents."""

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        ledger: SQLiteIntentLedger,
        registry: dict[str, RegisteredLoop] | None = None,
        *,
        trusted_requester: str = "nigo",
        allowed_loop_ids: frozenset[str] | None = None,
    ):
        self.store = store
        self.ledger = ledger
        self.registry = registry if registry is not None else {}
        self.trusted_requester = trusted_requester
        self.allowed_loop_ids = allowed_loop_ids

    # -- public API ------------------------------------------------------

    def submit(self, body: object) -> tuple[int, dict[str, Any]]:
        """Validate and synchronously apply one intent.

        Returns (http_status, receipt). Raises ControlError for stable client
        errors. Duplicates return (200, original receipt); a new applied
        reservation returns (202, receipt).
        """
        payload = self._validated_payload(body)
        self.assert_run_allowed(payload.run_id)
        duplicate = self.ledger.get_by_key(payload.idempotency_key)
        if duplicate is not None:
            return 200, duplicate
        receipt = self._reserve(payload)
        applied = self._apply(receipt)
        return 202, applied

    def reserve_only(self, body: object) -> dict[str, Any]:
        """Reserve and accept without applying. Test/recovery entry point.

        Enforces the exact same payload and canonical idempotency-key
        validation as submit(); it only skips the apply step.
        """
        payload = self._validated_payload(body)
        self.assert_run_allowed(payload.run_id)
        duplicate = self.ledger.get_by_key(payload.idempotency_key)
        if duplicate is not None:
            return duplicate
        return self._reserve(payload)

    def recover(self) -> int:
        """Re-drive intents left in pending/accepted by an interruption."""
        recovered = 0
        for receipt in self.ledger.list_resumable():
            run = self.store.latest(str(receipt["run_id"]))
            if run is None or not self._checkpoint_allowed(run):
                continue
            if receipt["status"] == "pending":
                receipt = self.ledger.transition(
                    receipt["intent_id"], from_statuses=("pending",), to_status="accepted"
                )
            self._apply(receipt)
            recovered += 1
        return recovered

    def actions_for(self, run_id: str) -> dict[str, Any] | None:
        """Allowed/disabled actions with reasons, latest sequence, priority."""
        run = self.store.latest(run_id)
        if run is None:
            return None
        self._assert_checkpoint_allowed(run)
        sequence = self.store.latest_sequence(run_id)
        actions: dict[str, Any] = {}
        for action in ACTIONS:
            reason = self._disabled_reason(run, action)
            actions[action] = {"enabled": reason is None, "reason_code": reason}
        return {
            "run_id": run_id,
            "status": run.status,
            "latest_sequence": sequence,
            "priority": self.ledger.get_priority(run_id),
            "actions": actions,
        }

    def assert_run_allowed(self, run_id: str) -> LoopCheckpoint:
        """Return the run or reject access outside an optional control scope."""
        run = self.store.latest(run_id)
        if run is None:
            raise ControlError(404, "run_not_found", "Unknown run_id")
        self._assert_checkpoint_allowed(run)
        return run

    def _checkpoint_allowed(self, run: LoopCheckpoint) -> bool:
        return self.allowed_loop_ids is None or run.loop_id in self.allowed_loop_ids

    def _assert_checkpoint_allowed(self, run: LoopCheckpoint) -> None:
        if not self._checkpoint_allowed(run):
            raise ControlError(
                403,
                "run_not_allowed",
                "Run is outside the allowed control scope",
            )

    # -- validation ------------------------------------------------------

    def _validated_payload(self, body: object) -> IntentPayload:
        payload = parse_intent_payload(body, trusted_requester=self.trusted_requester)
        expected_key = canonical_idempotency_key(
            payload.requester,
            payload.run_id,
            payload.action,
            payload.expected_sequence,
            payload.arguments,
        )
        if payload.idempotency_key != expected_key:
            raise ControlError(
                400, "invalid_idempotency_key", "idempotency_key does not match the canonical key"
            )
        return payload

    # -- reservation -----------------------------------------------------

    def _reserve(self, payload: IntentPayload) -> dict[str, Any]:
        run = self.assert_run_allowed(payload.run_id)
        sequence = self.store.latest_sequence(payload.run_id)
        if sequence != payload.expected_sequence:
            raise ControlError(409, "stale_sequence", "expected_sequence is stale")
        reason = self._disabled_reason(run, payload.action)
        if reason is not None:
            raise ControlError(409, reason, f"Action {payload.action} is not allowed now")
        receipt, is_new = self.ledger.reserve(
            intent_id=str(uuid.uuid4()),
            idempotency_key=payload.idempotency_key,
            run_id=payload.run_id,
            action=payload.action,
            arguments=payload.arguments,
            requester=payload.requester,
            expected_sequence=payload.expected_sequence,
        )
        if not is_new:
            return receipt
        return self.ledger.transition(
            receipt["intent_id"], from_statuses=("pending",), to_status="accepted"
        )

    # -- application -----------------------------------------------------

    def _applied_marker_sequence(
        self,
        receipt: dict[str, Any],
        run_id: str,
        marker: str,
    ) -> int | None:
        """Detect an already-applied intent from checkpoint history.

        pause/resume/terminate mark the run's own history; a retry marks its
        deterministic child run instead. Returns the applied sequence on the
        parent run, or None when the intent was never executed.
        """
        if str(receipt["action"]) == "retry":
            child_id = retry_child_run_id(run_id, str(receipt["intent_id"]))
            child_marker = self.store.find_revision_marker(child_id, marker)
            if child_marker is None:
                return None
            return self.store.latest_sequence(run_id)
        return self.store.find_revision_marker(run_id, marker)

    def _apply(self, receipt: dict[str, Any]) -> dict[str, Any]:
        intent_id = str(receipt["intent_id"])
        run_id = str(receipt["run_id"])
        action = str(receipt["action"])
        marker = f"control-intent:{intent_id}"
        self.assert_run_allowed(run_id)
        try:
            run = self.store.latest(run_id)
            if run is None:
                return self.ledger.transition(
                    intent_id,
                    from_statuses=("accepted",),
                    to_status="failed",
                    reason_code="run_not_found",
                )
            # Case (a): already executed, only the receipt lagged behind.
            applied_seq = self._applied_marker_sequence(receipt, run_id, marker)
            if applied_seq is not None:
                result_run_id = receipt["result_run_id"]
                if action == "retry":
                    result_run_id = retry_child_run_id(run_id, intent_id)
                return self.ledger.transition(
                    intent_id,
                    from_statuses=("accepted",),
                    to_status="applied",
                    result_run_id=result_run_id,
                    applied_sequence=applied_seq,
                )
            # Case (b): the run advanced past the reservation; the intent is
            # stale and was never executed.
            current_seq = self.store.latest_sequence(run_id)
            if current_seq != receipt["expected_sequence"]:
                return self.ledger.transition(
                    intent_id,
                    from_statuses=("accepted",),
                    to_status="rejected",
                    reason_code="stale_sequence",
                )
            reason = self._disabled_reason(run, action)
            if reason is not None:
                return self.ledger.transition(
                    intent_id,
                    from_statuses=("accepted",),
                    to_status="rejected",
                    reason_code=reason,
                )
            committed = self._apply_action(receipt, run, marker)
            if committed is None:
                # CAS conflict: another writer advanced the run between the
                # freshness check and the append. Re-classify exactly.
                applied_seq = self._applied_marker_sequence(receipt, run_id, marker)
                if applied_seq is not None:
                    result_run_id = receipt["result_run_id"]
                    if action == "retry":
                        result_run_id = retry_child_run_id(run_id, intent_id)
                    return self.ledger.transition(
                        intent_id,
                        from_statuses=("accepted",),
                        to_status="applied",
                        result_run_id=result_run_id,
                        applied_sequence=applied_seq,
                    )
                reason = (
                    "stale_sequence"
                    if self.store.latest_sequence(run_id) != receipt["expected_sequence"]
                    else "conflict"
                )
                return self.ledger.transition(
                    intent_id,
                    from_statuses=("accepted",),
                    to_status="rejected",
                    reason_code=reason,
                )
            final_receipt = committed.get("receipt")
            if final_receipt is not None:
                # priority commits receipt and side effect in one ledger
                # transaction under the sequence guard.
                return cast(dict[str, Any], final_receipt)
            return self.ledger.transition(
                intent_id,
                from_statuses=("accepted",),
                to_status="applied",
                result_run_id=committed.get("result_run_id"),
                applied_sequence=committed.get("applied_sequence"),
            )
        except Exception:
            return self.ledger.transition(
                intent_id,
                from_statuses=("accepted",),
                to_status="failed",
                reason_code="internal_error",
            )

    def _apply_action(
        self,
        receipt: dict[str, Any],
        run: LoopCheckpoint,
        marker: str,
    ) -> dict[str, Any] | None:
        """Apply via compare-and-append. Returns None on CAS conflict."""
        intent_id = str(receipt["intent_id"])
        run_id = str(receipt["run_id"])
        action = str(receipt["action"])
        expected_sequence = int(receipt["expected_sequence"])
        result_run_id: str | None = None
        raw_run: LoopCheckpoint | None = None

        if action in ("pause", "resume", "terminate"):
            # 写前重读 raw 存储形态：传入 run 可能来自 compat 投影，直接
            # replace 回写会把新 namespaced 行扁平化。sequence 漂移视同
            # CAS 冲突，交给上层重新分类。
            found = self.store.latest_raw_with_sequence(run_id)
            if found is None or found[1] != expected_sequence:
                return None
            raw_run, _raw_sequence = found
        if action == "pause":
            assert raw_run is not None
            committed = self.store.compare_and_append(
                replace(raw_run, status="pause_requested"),
                expected_sequence=expected_sequence,
                event_type="state_transition",
                revision_reason=marker,
            )
            if committed is None:
                return None
        elif action == "resume":
            assert raw_run is not None
            committed = self.store.compare_and_append(
                replace(raw_run, status="running", stop_reason=None),
                expected_sequence=expected_sequence,
                event_type="state_transition",
                revision_reason=marker,
            )
            if committed is None:
                return None
        elif action == "terminate":
            assert raw_run is not None
            committed = self.store.compare_and_append(
                replace(raw_run, status="cancelled", stop_reason="cancelled_by_human"),
                expected_sequence=expected_sequence,
                event_type="state_transition",
                revision_reason=marker,
            )
            if committed is None:
                return None
        elif action == "retry":
            result_run_id = retry_child_run_id(run_id, intent_id)
            registered = self.registry[run.loop_id]
            child = LoopCheckpoint(
                loop_id=registered.spec.loop_id,
                loopspec_version=registered.spec.version,
                run_id=result_run_id,
                fragment_id=run.fragment_id,
                parent_loop_id=run.run_id,
                current_node=registered.spec.first_node,
                status=AdmissionState.APPROVED.value,
                goal=run.goal,
                fragment_title=run.fragment_title,
                fragment_title_source=run.fragment_title_source,
                attempt_episode=run.attempt_episode,
                input_refs=list(run.input_refs),
                budget_limits={
                    "iterations": registered.spec.max_iterations,
                    "seconds": registered.spec.max_seconds,
                    "tokens": registered.spec.token_limit,
                    "tool_calls": registered.spec.tool_call_limit,
                },
                budget_used={"tokens": 0, "tool_calls": 0},
                worker_version=registered.spec.worker_version,
                evaluator_version=registered.spec.evaluator_version,
            )
            outcome = self.store.register_child_atomic(
                child,
                parent_run_id=run_id,
                expected_parent_sequence=expected_sequence,
                marker=marker,
            )
            if outcome == "conflict":
                return None
            # "committed" and "already_applied" both reach applied; the
            # terminal parent was never modified.
        elif action == "priority":
            # Priority lives only in the control ledger, but its
            # linearization point is the Loop database sequence guard: while
            # the guard is held, no run writer can advance the parent, and
            # the priority upsert plus the applied receipt commit in one
            # control-ledger transaction.
            with self.store.sequence_guard(run_id, expected_sequence) as fresh:
                if not fresh:
                    return None
                final_receipt = self.ledger.apply_priority_and_receipt(
                    intent_id=intent_id,
                    run_id=run_id,
                    priority=int(receipt["arguments"]["priority"]),
                    applied_sequence=expected_sequence,
                )
            return {"receipt": final_receipt}
        else:  # pragma: no cover - action vocabulary is validated upstream
            raise ValueError(f"Unknown action: {action}")

        return {
            "result_run_id": result_run_id,
            "applied_sequence": self.store.latest_sequence(run_id),
        }

    # -- feasibility -----------------------------------------------------

    def _disabled_reason(self, run: LoopCheckpoint, action: str) -> str | None:
        if run.status not in STATE_MATRIX[action]:
            return "invalid_state"
        if action in ("resume", "retry"):
            registered = self.registry.get(run.loop_id)
            if registered is None:
                return "loopspec_unavailable"
            # The pinned LoopSpec version must match exactly; retry never
            # silently upgrades the LoopSpec of the new attempt.
            if run.loopspec_version != registered.spec.version:
                return "loopspec_version_mismatch"
            missing = set(registered.spec.nodes) - set(registered.handler_nodes)
            if missing:
                return "handler_unavailable"
        return None
