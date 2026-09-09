"""Execution candidate selection (frozen contract §3).

A candidate exists only for the current ``accepted`` decision of a still
active, executable proposal whose decision post-dates the activation
epoch. Everything else is a zero-write rejection with an explicit reason.
Times are compared as real timezone-aware instants; naive or invalid
timestamps are rejected, never guessed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from shadow_dispatcher.proposals import ACTIVE_STATUSES

RESOLVED_RUN_STATUSES = frozenset({"passed", "cancelled", "superseded"})


def parse_instant(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp into an aware datetime.

    Rejects missing, malformed, and naive values: epoch isolation is a
    security boundary and must never be decided by string comparison.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid timestamp: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid timestamp: {value!r}") from error
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(f"naive timestamp: {value!r}")
    return parsed


@dataclass(frozen=True)
class Candidate:
    decision: dict[str, Any]
    proposal: dict[str, Any]


@dataclass(frozen=True)
class Rejection:
    decision_id: str
    proposal_id: str
    reason: str


def select_candidates(
    *,
    decisions: list[dict[str, Any]],
    proposal_by_id: Callable[[str], dict[str, Any] | None],
    run_status_of: Callable[[str], str | None],
    activation_epoch: str,
) -> tuple[list[Candidate], list[Rejection]]:
    """Split current review decisions into candidates and rejections.

    ``decisions`` are the current decisions per proposal (already deduped
    by the review ledger). ``proposal_by_id`` returns the merged shadow
    proposal payload (authoritative status). ``run_status_of`` returns the
    run's latest Loop status or None when the run is unknown.
    """
    candidates: list[Candidate] = []
    rejections: list[Rejection] = []
    epoch = parse_instant(activation_epoch)
    for decision in decisions:
        decision_id = str(decision["decision_id"])
        proposal_id = str(decision["proposal_id"])

        def reject(reason: str) -> None:
            rejections.append(
                Rejection(decision_id=decision_id, proposal_id=proposal_id, reason=reason)
            )

        if str(decision["decision"]) != "accepted":
            reject(f"decision_not_accepted:{decision['decision']}")
            continue
        try:
            decided_at = parse_instant(decision["decided_at"])
        except ValueError as error:
            reject(str(error))
            continue
        if decided_at < epoch:
            reject("pre_activation_epoch")
            continue
        proposal = proposal_by_id(proposal_id)
        if proposal is None:
            reject("proposal_missing")
            continue
        expires_at = proposal.get("expires_at")
        if expires_at and parse_instant(str(expires_at)) < datetime.now(UTC):
            reject(f"proposal_expired:{expires_at}")
            continue
        status = str(proposal.get("proposal_status"))
        if status not in ACTIVE_STATUSES:
            reject(f"proposal_not_active:{status}")
            continue
        if not proposal.get("execution_ready", False):
            reasons = proposal.get("blocked_reasons") or []
            reject("proposal_not_executable:" + ",".join(str(r) for r in reasons))
            continue
        run_status = run_status_of(str(decision["run_id"]))
        if run_status is None:
            reject("run_missing")
            continue
        if run_status in RESOLVED_RUN_STATUSES:
            reject(f"run_resolved:{run_status}")
            continue
        candidates.append(Candidate(decision=dict(decision), proposal=dict(proposal)))
    return candidates, rejections
