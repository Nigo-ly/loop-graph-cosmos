"""Deterministic proposal construction. Every field comes from a registry
fact or a frozen policy entry; nothing is inferred by a model and nothing
is guessed. Missing facts degrade to ``needs_human_routing``/``blocked``.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from common.orchestrator import AgentRegistry, AgentSpec, Availability, HealthSnapshot
from projection_api.store import RunRow

from . import DISPATCHER_VERSION
from .policy import LoopPolicy, RoutePolicy

PROPOSED = "proposed"
NEEDS_HUMAN_ROUTING = "needs_human_routing"
BLOCKED = "blocked"
STALE = "stale"
SUPERSEDED = "superseded"
ACTIVE_STATUSES = frozenset({PROPOSED, NEEDS_HUMAN_ROUTING, BLOCKED})
ALL_STATUSES = ACTIVE_STATUSES | {STALE, SUPERSEDED}

_PROPOSAL_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "loop-engineering/shadow-dispatcher")

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    run_id: str
    fragment_id: str
    attempt_episode: int
    source_sequence: int
    subject: str
    loop_status: str
    proposed_loopspec: str
    loopspec_version: str
    route_reason: str
    worker_agent: str
    worker_model: str
    independent_evaluator: str
    verifier: str
    budget: dict[str, Any]
    risk_level: str
    risk_reasons: list[str]
    required_tools: list[str]
    external_side_effects: str
    stop_conditions: list[dict[str, Any]]
    execution_ready: bool
    blocked_reasons: list[str]
    dispatcher_version: str
    fingerprint: str
    created_at: str
    expires_at: str
    proposal_status: str
    supersedes_proposal_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def proposal_id_for(run_id: str, source_sequence: int, fingerprint: str) -> str:
    """Deterministic id: the same inputs always name the same proposal."""
    raw = f"{run_id}|{source_sequence}|{DISPATCHER_VERSION}|{fingerprint}"
    return str(uuid.uuid5(_PROPOSAL_NAMESPACE, raw))


def _agent_block_reason(
    registry: AgentRegistry,
    health: HealthSnapshot,
    agent_id: str,
    required_capabilities: frozenset[str],
    *,
    require_evaluation: bool = False,
) -> str | None:
    """None when the agent may be proposed; otherwise the precise reason.

    There is deliberately no fallback lookup anywhere in this function.
    """
    agent = registry.agents.get(agent_id)
    if agent is None:
        return "agent_not_registered"
    if not agent.enabled:
        return "agent_disabled"
    missing = sorted(required_capabilities - agent.capabilities)
    if missing:
        return "capabilities_missing:" + ",".join(missing)
    if require_evaluation and "independent_evaluation" not in agent.capabilities:
        return "capabilities_missing:independent_evaluation"
    if "P3" not in agent.allowed_phases:
        return "phase_not_allowed"
    effective = health.effective(agent)
    if effective.availability is not Availability.AVAILABLE:
        return f"health_{effective.availability.value}:{effective.reason}"
    return None


def _agent_model(registry: AgentRegistry, agent_id: str) -> str:
    agent: AgentSpec | None = registry.agents.get(agent_id)
    return agent.model if agent is not None else ""


def _risk(loop_policy: LoopPolicy) -> tuple[str, list[str]]:
    level = "low"
    reasons: list[str] = []
    if loop_policy.external_side_effects == "write":
        level = "high"
        reasons.append("external_write_side_effects")
    if loop_policy.external_side_effects == "unknown":
        level = "high"
        reasons.append("side_effect_unknown")
    if loop_policy.untrusted_web_input and _RISK_ORDER[level] < _RISK_ORDER["medium"]:
        level = "medium"
        reasons.append("untrusted_web_input")
    if loop_policy.privacy_content and "privacy_content" not in reasons:
        if _RISK_ORDER[level] < _RISK_ORDER["medium"]:
            level = "medium"
        reasons.append("privacy_content")
    if not reasons:
        reasons.append("no_external_side_effects")
    return level, reasons


def build_proposal(
    row: RunRow,
    *,
    attempt_episode: int,
    loopspec_entry: dict[str, Any] | None,
    policy: RoutePolicy,
    registry: AgentRegistry,
    health: HealthSnapshot,
    fingerprint: str,
    now: datetime,
) -> Proposal:
    """Build one proposal for one admitted current-episode approved run."""
    payload = row.payload
    loop_id = row.loop_id
    # Subject is the verified per-fragment title only. The shared Loop goal
    # must never impersonate a fragment subject (unrelated fragments would
    # become indistinguishable cards); it stays available in `extra.goal`
    # for the detail view, and the UI applies an explicit distinguishable
    # fallback when the subject is absent.
    subject = str(payload.get("fragment_title") or "")
    loop_policy = policy.loops.get(loop_id)
    blocked: list[str] = []
    status = PROPOSED

    spec: dict[str, Any] = {}
    handlers: frozenset[str] = frozenset()
    if loopspec_entry is None:
        status = NEEDS_HUMAN_ROUTING
        blocked.append("loopspec_not_registered")
    else:
        spec = dict(loopspec_entry.get("spec", {}))
        handlers = frozenset(str(item) for item in loopspec_entry.get("handlers", []))

    if loop_policy is None and status is PROPOSED:
        status = NEEDS_HUMAN_ROUTING
        blocked.append("route_policy_missing")

    pinned_version = str(payload.get("loopspec_version") or "")
    if status is PROPOSED and pinned_version != str(spec.get("version", "")):
        status = BLOCKED
        blocked.append(
            "loopspec_version_mismatch:"
            f"checkpoint={pinned_version},registry={spec.get('version', '')}"
        )

    if status is PROPOSED and loop_policy is not None:
        if loop_policy.verifier != str(spec.get("evaluator_version", "")):
            status = BLOCKED
            blocked.append(
                f"verifier_mismatch:policy={loop_policy.verifier},"
                f"loopspec={spec.get('evaluator_version', '')}"
            )
        missing_handlers = sorted(set(str(n) for n in spec.get("nodes", [])) - handlers)
        if status is PROPOSED and missing_handlers:
            status = BLOCKED
            blocked.append("handlers_missing:" + ",".join(missing_handlers))

    worker_agent = loop_policy.worker_agent if loop_policy is not None else ""
    evaluator_agent = loop_policy.evaluator_agent if loop_policy is not None else None
    if status is PROPOSED and loop_policy is not None:
        worker_reason = _agent_block_reason(
            registry, health, worker_agent, loop_policy.worker_required_capabilities
        )
        if worker_reason is not None:
            status = BLOCKED
            blocked.append(f"worker_unavailable:{worker_reason}")
        if evaluator_agent is not None:
            if evaluator_agent == worker_agent:
                status = BLOCKED
                blocked.append("evaluator_not_independent")
            else:
                evaluator_reason = _agent_block_reason(
                    registry, health, evaluator_agent, frozenset(), require_evaluation=True
                )
                if evaluator_reason is not None:
                    status = BLOCKED
                    blocked.append(f"evaluator_unavailable:{evaluator_reason}")

    budget_limits = {
        "iterations": spec.get("max_iterations"),
        "seconds": spec.get("max_seconds"),
        "tokens": spec.get("token_limit"),
        "tool_calls": spec.get("tool_call_limit"),
    }
    budget_used = dict(payload.get("budget_used") or {})
    if status is PROPOSED:
        for key, limit_key in (("tokens", "tokens"), ("tool_calls", "tool_calls")):
            limit = budget_limits.get(limit_key)
            used = budget_used.get(key)
            if isinstance(limit, int) and isinstance(used, (int, float)) and used >= limit:
                status = BLOCKED
                blocked.append(f"budget_exceeded:{key}")

    if loop_policy is not None:
        risk_level, risk_reasons = _risk(loop_policy)
        if loop_policy.external_side_effects == "unknown" and status is PROPOSED:
            status = BLOCKED
            blocked.append("side_effect_unknown")
        external_side_effects = loop_policy.external_side_effects
        required_tools = list(loop_policy.required_tools)
        verifier = loop_policy.verifier
        route_reason = loop_policy.route_reason
    else:
        risk_level, risk_reasons = "unknown", ["route_policy_missing"]
        external_side_effects = "unknown"
        required_tools = []
        verifier = str(spec.get("evaluator_version", ""))
        route_reason = ""

    stop_conditions: list[dict[str, Any]] = [
        {"kind": "budget_hard_gate", "limits": budget_limits},
        {"kind": "verifier_disagreement"},
        {"kind": "safety"},
        {"kind": "side_effect_unknown"},
        {"kind": "no_gain", "max_consecutive_no_gain": policy.no_gain_round_limit},
    ]

    created = now.astimezone(UTC)
    expires = created + timedelta(seconds=policy.proposal_ttl_seconds)
    return Proposal(
        proposal_id=proposal_id_for(row.run_id, row.sequence, fingerprint),
        run_id=row.run_id,
        fragment_id=row.fragment_id,
        attempt_episode=attempt_episode,
        source_sequence=row.sequence,
        subject=subject,
        loop_status=row.status,
        proposed_loopspec=loop_id,
        loopspec_version=pinned_version,
        route_reason=route_reason,
        worker_agent=worker_agent,
        worker_model=_agent_model(registry, worker_agent) if worker_agent else "",
        independent_evaluator=evaluator_agent or "",
        verifier=verifier,
        budget={"limits": budget_limits, "used": budget_used},
        risk_level=risk_level,
        risk_reasons=risk_reasons,
        required_tools=required_tools,
        external_side_effects=external_side_effects,
        stop_conditions=stop_conditions,
        execution_ready=status is PROPOSED,
        blocked_reasons=blocked,
        dispatcher_version=DISPATCHER_VERSION,
        fingerprint=fingerprint,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        proposal_status=status,
        extra={
            "goal": str(payload.get("goal") or ""),
            **(
                {"verifier_level": loop_policy.verifier_level}
                if loop_policy is not None
                else {}
            ),
        },
    )
