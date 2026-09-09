"""Submit-time validation and proposal revalidation for review decisions.

Every check is deterministic over the read-only proposal ledger and the
review ledger — no model, no guessing. Any proposal drift since the user
opened the dialog rejects the submission with ``proposal_changed`` and
writes nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

from shadow_dispatcher.proposals import ACTIVE_STATUSES
from shadow_projection_api import store as proposal_store

from .ledger import DECISIONS, ReviewConflictError, SQLiteReviewLedger

REVIEW_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "loop-engineering/shadow-review")


class ReviewError(Exception):
    def __init__(self, http_status: int, code: str, message: str):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def actions_for(
    proposal_ledger_path: str, review_ledger: SQLiteReviewLedger, proposal_id: str
) -> dict[str, Any]:
    """Authoritative action matrix for one proposal (console dialog data)."""
    proposal = proposal_store.get_proposal(proposal_ledger_path, proposal_id)
    current = review_ledger.current_for(proposal_id)
    if proposal is None or proposal["proposal_status"] not in ACTIVE_STATUSES:
        return {
            "proposal_id": proposal_id,
            "submittable": False,
            "reason_code": "proposal_changed",
            "proposal_fingerprint": None,
            "source_sequence": None,
            "proposal_status": None if proposal is None else str(proposal["proposal_status"]),
            "current_decision": current,
        }
    return {
        "proposal_id": proposal_id,
        "submittable": True,
        "reason_code": None,
        "proposal_fingerprint": str(proposal["fingerprint"]),
        "source_sequence": int(proposal["source_sequence"]),
        "proposal_status": str(proposal["proposal_status"]),
        "current_decision": current,
    }


def submit_decision(
    proposal_ledger_path: str,
    review_ledger: SQLiteReviewLedger,
    payload: dict[str, Any],
    *,
    decided_by: str,
) -> tuple[dict[str, Any], bool]:
    """Validate, revalidate, and persist one decision.

    Returns (receipt, is_new). Raises ReviewError with a stable code on
    any validation/revalidation failure; nothing is written in that case.
    """
    proposal_id = _text(payload.get("proposal_id"))
    decision = _text(payload.get("decision"))
    reason = _text(payload.get("reason"))
    resume_condition = _text(payload.get("resume_condition"))
    note = _text(payload.get("note"))
    idempotency_key = _text(payload.get("idempotency_key"))
    expected_fingerprint = _text(payload.get("expected_fingerprint"))
    expected_sequence = payload.get("expected_source_sequence")
    expected_status = _text(payload.get("expected_proposal_status"))
    expected_current_raw = payload.get("expected_current_decision_id")
    expected_current = _text(expected_current_raw)

    if not proposal_id:
        raise ReviewError(400, "missing_proposal_id", "proposal_id is required")
    if decision not in DECISIONS:
        raise ReviewError(
            400, "invalid_decision", "decision must be accepted, deferred, or rejected"
        )
    if not idempotency_key:
        raise ReviewError(400, "missing_idempotency_key", "idempotency_key is required")
    if not expected_fingerprint or expected_sequence is None or not expected_status:
        raise ReviewError(400, "missing_expected_facts", "expected proposal facts are required")
    try:
        expected_sequence_int = int(expected_sequence)
    except (TypeError, ValueError):
        raise ReviewError(
            400, "invalid_expected_facts", "expected_source_sequence must be an integer"
        ) from None
    if decision == "rejected" and not reason:
        raise ReviewError(400, "missing_reason", "rejected requires a reason")
    if decision == "deferred" and not reason and not resume_condition:
        raise ReviewError(400, "missing_reason", "deferred requires a reason or a resume condition")

    # An idempotent replay returns the original receipt as recorded —
    # even if the proposal has since drifted. Revalidation only gates NEW
    # submissions.
    existing = review_ledger.get_by_key(idempotency_key)
    if existing is not None:
        return existing, False

    # Submit-time revalidation against the CURRENT proposal ledger. Any
    # drift rejects the submission; decisions are never migrated.
    proposal = proposal_store.get_proposal(proposal_ledger_path, proposal_id)
    changed = (
        proposal is None
        or proposal["proposal_status"] not in ACTIVE_STATUSES
        or str(proposal["fingerprint"]) != expected_fingerprint
        or int(proposal["source_sequence"]) != expected_sequence_int
        or str(proposal["proposal_status"]) != expected_status
    )
    if changed:
        raise ReviewError(409, "proposal_changed", "建议已经变化，请重新查看")

    assert proposal is not None  # narrowed by the check above
    decision_id = str(uuid.uuid5(REVIEW_NAMESPACE, f"{proposal_id}|{idempotency_key}"))
    try:
        receipt, is_new = review_ledger.submit(
            decision_id=decision_id,
            idempotency_key=idempotency_key,
            proposal_id=proposal_id,
            run_id=str(proposal["run_id"]),
            fragment_id=str(proposal["fragment_id"]),
            proposal_fingerprint=expected_fingerprint,
            source_sequence=expected_sequence_int,
            decision=str(decision),
            reason=reason,
            resume_condition=resume_condition,
            note=note,
            decided_by=decided_by,
            expected_current_decision_id=expected_current,
        )
    except ReviewConflictError as error:
        raise ReviewError(409, "conflict", "已有其他审核决定，请刷新后重试") from error
    return receipt, is_new
