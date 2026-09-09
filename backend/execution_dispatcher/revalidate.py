"""Dispatch-time linearized revalidation (frozen contract §4).

Every check re-derives its fact from live sources; any drift is a
zero-write rejection. The caller holds ``store.sequence_guard`` so the
run cannot advance while the reservation is committed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from common.checkpoint import SQLiteCheckpointStore
from common.orchestrator import Availability
from common.supervisor import LoopSpec
from execution_dispatcher.candidates import Candidate, parse_instant
from execution_dispatcher.facts import DispatchFacts
from projection_api.project import attempt_group_id, compute_attempt_info
from projection_api.store import latest_runs
from shadow_dispatcher.policy import registry_fingerprint
from shadow_dispatcher.proposals import ACTIVE_STATUSES


def revalidate(
    candidate: Candidate,
    *,
    store: SQLiteCheckpointStore,
    loop_db_path: str,
    review_current_decision_id: str | None,
    proposal_now: dict[str, Any],
    facts: DispatchFacts,
    spec: LoopSpec,
    expected_statuses: frozenset[str] = frozenset({"approved"}),
) -> list[str]:
    """Return the list of drift reasons; empty means safe to reserve."""
    reasons: list[str] = []
    decision = candidate.decision
    proposal = candidate.proposal
    run_id = str(decision["run_id"])

    status_now = str(proposal_now.get("proposal_status"))
    if status_now not in ACTIVE_STATUSES:
        reasons.append(f"proposal_not_active:{status_now}")

    expires_at = proposal_now.get("expires_at")
    if expires_at and parse_instant(str(expires_at)) < datetime.now(UTC):
        reasons.append(f"proposal_expired:{expires_at}")

    live_fingerprint = registry_fingerprint(
        facts.policy, facts.loopspec_registry, facts.agent_registry, facts.health
    )
    if live_fingerprint != str(proposal.get("fingerprint")):
        reasons.append("fingerprint_changed")

    if review_current_decision_id != str(decision["decision_id"]):
        reasons.append("decision_superseded")

    # LoopSpec identity chain, verbatim: proposal → checkpoint → spec → registry
    proposed_loopspec = str(proposal.get("proposed_loopspec"))
    if proposed_loopspec != str(spec.loop_id):
        reasons.append(
            f"loopspec_id_mismatch:proposal={proposed_loopspec},spec={spec.loop_id}"
        )
    proposed_version = str(proposal.get("loopspec_version"))
    if proposed_version != str(spec.version):
        reasons.append(
            f"loopspec_version_mismatch:proposal={proposed_version},spec={spec.version}"
        )
    registry_entry = facts.loopspec_registry.get(proposed_loopspec)
    if registry_entry is None:
        reasons.append("loopspec_not_registered")
    else:
        registry_version = str(registry_entry.get("spec", {}).get("version"))
        if registry_version != str(spec.version):
            reasons.append(
                "loopspec_version_mismatch:"
                f"registry={registry_version},spec={spec.version}"
            )

    latest = store.latest(run_id)
    if latest is None:
        reasons.append("run_missing")
    else:
        if str(latest.loop_id) != str(spec.loop_id):
            reasons.append(
                f"loopspec_id_mismatch:checkpoint={latest.loop_id},spec={spec.loop_id}"
            )
        if latest.status not in expected_statuses:
            reasons.append(f"run_not_runnable:{latest.status}")
        if str(latest.loopspec_version) != str(spec.version):
            reasons.append(
                f"loopspec_version_mismatch:checkpoint={latest.loopspec_version},spec={spec.version}"
            )

    rows = latest_runs(loop_db_path)
    attempts = compute_attempt_info(rows)
    loop_id = str(latest.loop_id) if latest else proposed_loopspec
    group = attempt_group_id(loop_id, str(decision["fragment_id"]))
    current_episode = None
    for row in rows:
        if attempt_group_id(row.loop_id, row.fragment_id) == group:
            info = attempts.get(row.run_id)
            if info is not None and info.is_current:
                current_episode = info.attempt_episode
    expected_episode = int(decision.get("attempt_episode", proposal.get("attempt_episode", -1)))
    if current_episode is None:
        reasons.append("attempt_not_current")
    elif int(current_episode) != expected_episode:
        reasons.append(f"attempt_superseded:current={current_episode}")

    loop_policy = facts.policy.loops.get(proposed_loopspec)
    if loop_policy is None:
        reasons.append("route_policy_missing")
    else:
        worker = facts.agent_registry.agents.get(loop_policy.worker_agent)
        if worker is None:
            reasons.append("worker_not_registered")
        else:
            if not loop_policy.worker_required_capabilities.issubset(worker.capabilities):
                reasons.append("worker_capabilities_missing")
            if "P3" not in worker.allowed_phases:
                reasons.append("worker_phase_not_allowed")
            worker_health = facts.health.effective(worker)
            if worker_health.availability is not Availability.AVAILABLE:
                reasons.append(f"worker_unavailable:{worker_health.reason}")
        if loop_policy.evaluator_agent is not None:
            evaluator = facts.agent_registry.agents.get(loop_policy.evaluator_agent)
            if evaluator is None:
                reasons.append("evaluator_not_registered")
            else:
                if evaluator.agent_id == loop_policy.worker_agent:
                    reasons.append("evaluator_not_independent")
                evaluator_health = facts.health.effective(evaluator)
                if evaluator_health.availability is not Availability.AVAILABLE:
                    reasons.append(f"evaluator_unavailable:{evaluator_health.reason}")

    return reasons
