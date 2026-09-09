"""Frozen shadow-route policy: the only source of routing facts besides the
registries. A Loop without an explicit policy entry is never guessed at —
the dispatcher emits ``needs_human_routing`` instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.orchestrator import AgentRegistry, HealthSnapshot

ALLOWED_SIDE_EFFECTS = frozenset({"none", "read_only", "write", "unknown"})


@dataclass(frozen=True)
class LoopPolicy:
    loop_id: str
    worker_agent: str
    worker_required_capabilities: frozenset[str]
    evaluator_agent: str | None
    verifier: str
    verifier_level: str
    required_tools: tuple[str, ...]
    external_side_effects: str
    untrusted_web_input: bool
    privacy_content: bool
    route_reason: str


@dataclass(frozen=True)
class RoutePolicy:
    policy_version: str
    proposal_ttl_seconds: int
    no_gain_round_limit: int
    loops: dict[str, LoopPolicy]

    @classmethod
    def load(cls, path: str | Path) -> RoutePolicy:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        loops: dict[str, LoopPolicy] = {}
        for loop_id, value in payload.get("loops", {}).items():
            side_effects = str(value["external_side_effects"])
            if side_effects not in ALLOWED_SIDE_EFFECTS:
                raise ValueError(
                    f"invalid external_side_effects for {loop_id}: {side_effects}"
                )
            loops[str(loop_id)] = LoopPolicy(
                loop_id=str(loop_id),
                worker_agent=str(value["worker_agent"]),
                worker_required_capabilities=frozenset(
                    str(item) for item in value.get("worker_required_capabilities", [])
                ),
                evaluator_agent=(
                    str(value["evaluator_agent"])
                    if value.get("evaluator_agent") is not None
                    else None
                ),
                verifier=str(value["verifier"]),
                verifier_level=str(value["verifier_level"]),
                required_tools=tuple(str(item) for item in value.get("required_tools", [])),
                external_side_effects=side_effects,
                untrusted_web_input=bool(value.get("untrusted_web_input", False)),
                privacy_content=bool(value.get("privacy_content", False)),
                route_reason=str(value.get("route_reason", "")),
            )
        return cls(
            policy_version=str(payload["policy_version"]),
            proposal_ttl_seconds=int(payload["proposal_ttl_seconds"]),
            no_gain_round_limit=int(payload["no_gain_round_limit"]),
            loops=loops,
        )


def _canonical_policy(policy: RoutePolicy) -> dict[str, Any]:
    """The full effective policy content a proposal is built from.

    The manually maintained ``policy_version`` is kept for readability but
    is *not* trusted as a content hash: any edit to side effects, verifier,
    tools, budget/stop inputs, or rationale must change the fingerprint
    even when the version label stays the same.
    """
    return {
        "version": policy.policy_version,
        "proposal_ttl_seconds": policy.proposal_ttl_seconds,
        "no_gain_round_limit": policy.no_gain_round_limit,
        "loops": {
            loop_id: {
                "worker_agent": loop_policy.worker_agent,
                "worker_required_capabilities": sorted(
                    loop_policy.worker_required_capabilities
                ),
                "evaluator_agent": loop_policy.evaluator_agent,
                "verifier": loop_policy.verifier,
                "verifier_level": loop_policy.verifier_level,
                "required_tools": list(loop_policy.required_tools),
                "external_side_effects": loop_policy.external_side_effects,
                "untrusted_web_input": loop_policy.untrusted_web_input,
                "privacy_content": loop_policy.privacy_content,
                "route_reason": loop_policy.route_reason,
            }
            for loop_id, loop_policy in sorted(policy.loops.items())
        },
    }


def _route_relevant_agent_ids(policy: RoutePolicy) -> set[str]:
    """Agents any proposal may name: the policy's workers and evaluators."""
    ids: set[str] = set()
    for loop_policy in policy.loops.values():
        ids.add(loop_policy.worker_agent)
        if loop_policy.evaluator_agent is not None:
            ids.add(loop_policy.evaluator_agent)
    return ids


def _canonical_agent_facts(
    policy: RoutePolicy, registry: AgentRegistry, health: HealthSnapshot
) -> dict[str, Any]:
    """Route-relevant registry facts plus *semantic* effective health.

    Wall-clock observation metadata (``observed_at``, ``source``) is
    deliberately excluded: a fresh probe that reaches the same availability
    and reason must not invalidate existing proposals.
    """
    facts: dict[str, Any] = {}
    for agent_id in sorted(_route_relevant_agent_ids(policy)):
        spec = registry.agents.get(agent_id)
        if spec is None:
            facts[agent_id] = {"registered": False}
            continue
        effective = health.effective(spec)
        facts[agent_id] = {
            "registered": True,
            "enabled": spec.enabled,
            "model": spec.model,
            "quota_pool": spec.quota_pool,
            "capabilities": sorted(spec.capabilities),
            "allowed_phases": sorted(spec.allowed_phases),
            "health": {
                "availability": effective.availability.value,
                "reason": effective.reason,
            },
        }
    return facts


def registry_fingerprint(
    policy: RoutePolicy,
    loopspec_payload: dict[str, Any],
    registry: AgentRegistry,
    health: HealthSnapshot,
) -> str:
    """Stable fingerprint of every registry fact a proposal depends on.

    Covers the full effective route policy content (not merely its version
    label), the LoopSpec registry, and the route-relevant Agent Registry
    facts with their effective semantic health. Any change to those facts
    produces a different fingerprint, which is what makes the ledger
    supersede the old proposal deterministically — including a blocked
    agent becoming available (or vice versa) or a same-version policy
    content edit at the same checkpoint sequence.
    """
    import hashlib

    canonical = json.dumps(
        {
            "policy": _canonical_policy(policy),
            "loopspec": loopspec_payload,
            "agents": _canonical_agent_facts(policy, registry, health),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
