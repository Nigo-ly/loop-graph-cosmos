"""Deterministic agent routing and shell-free CLI invocation contracts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class RouteStatus(StrEnum):
    SELECTED = "selected"
    PENDING_APPROVAL = "pending_approval"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    harness: str
    model: str
    quota_pool: str
    capabilities: frozenset[str]
    allowed_phases: frozenset[str]
    command_template: tuple[str, ...]
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentSpec:
        return cls(
            agent_id=str(value["agent_id"]),
            harness=str(value["harness"]),
            model=str(value["model"]),
            quota_pool=str(value["quota_pool"]),
            capabilities=frozenset(str(item) for item in value["capabilities"]),
            allowed_phases=frozenset(str(item) for item in value["allowed_phases"]),
            command_template=tuple(str(item) for item in value["command_template"]),
            enabled=bool(value.get("enabled", True)),
        )


@dataclass(frozen=True)
class AgentRegistry:
    agents: dict[str, AgentSpec]

    @classmethod
    def load(cls, path: str | Path) -> AgentRegistry:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        agents: dict[str, AgentSpec] = {}
        for raw in payload["agents"]:
            spec = AgentSpec.from_dict(raw)
            if spec.agent_id in agents:
                raise ValueError(f"Duplicate agent id: {spec.agent_id}")
            if not spec.command_template:
                raise ValueError(f"Agent has no command template: {spec.agent_id}")
            agents[spec.agent_id] = spec
        return cls(agents)


@dataclass(frozen=True)
class HealthStatus:
    availability: Availability
    reason: str
    observed_at: str
    source: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HealthStatus:
        return cls(
            availability=Availability(str(value["availability"])),
            reason=str(value["reason"]),
            observed_at=str(value["observed_at"]),
            source=str(value["source"]),
        )


@dataclass(frozen=True)
class HealthSnapshot:
    quota_pools: dict[str, HealthStatus]
    agents: dict[str, HealthStatus]

    @classmethod
    def load(cls, path: str | Path) -> HealthSnapshot:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            quota_pools={
                str(key): HealthStatus.from_dict(value)
                for key, value in payload.get("quota_pools", {}).items()
            },
            agents={
                str(key): HealthStatus.from_dict(value)
                for key, value in payload.get("agents", {}).items()
            },
        )

    def effective(self, agent: AgentSpec) -> HealthStatus:
        if not agent.enabled:
            return HealthStatus(
                Availability.DISABLED,
                "agent_disabled_by_registry",
                "unknown",
                "registry",
            )
        pool = self.quota_pools.get(agent.quota_pool)
        if pool is not None and pool.availability is not Availability.AVAILABLE:
            return pool
        return self.agents.get(
            agent.agent_id,
            HealthStatus(Availability.UNKNOWN, "health_not_checked", "unknown", "default"),
        )


@dataclass(frozen=True)
class TaskRouteSpec:
    task_id: str
    phase: str
    required_capabilities: frozenset[str]
    preferred_agent: str
    allowed_fallbacks: tuple[str, ...]
    workspace: str
    task_file: str
    prompt: str
    author_agent: str | None = None
    independent_evaluator: bool = False

    @classmethod
    def load(cls, path: str | Path) -> TaskRouteSpec:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            task_id=str(payload["task_id"]),
            phase=str(payload["phase"]),
            required_capabilities=frozenset(
                str(item) for item in payload["required_capabilities"]
            ),
            preferred_agent=str(payload["preferred_agent"]),
            allowed_fallbacks=tuple(str(item) for item in payload["allowed_fallbacks"]),
            workspace=str(payload["workspace"]),
            task_file=str(payload["task_file"]),
            prompt=str(payload["prompt"]),
            author_agent=(
                str(payload["author_agent"])
                if payload.get("author_agent") is not None
                else None
            ),
            independent_evaluator=bool(payload.get("independent_evaluator", False)),
        )


@dataclass(frozen=True)
class RouteDecision:
    task_id: str
    status: RouteStatus
    preferred_agent: str
    selected_agent: str | None
    reason: str
    requires_approval: bool
    approved_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


def _eligible(agent: AgentSpec, task: TaskRouteSpec) -> bool:
    if not task.required_capabilities.issubset(agent.capabilities):
        return False
    if task.phase not in agent.allowed_phases:
        return False
    return not (
        task.independent_evaluator
        and task.author_agent is not None
        and agent.agent_id == task.author_agent
    )


def route_task(
    registry: AgentRegistry,
    health: HealthSnapshot,
    task: TaskRouteSpec,
) -> RouteDecision:
    preferred = registry.agents.get(task.preferred_agent)
    if preferred is None:
        return RouteDecision(
            task.task_id,
            RouteStatus.BLOCKED,
            task.preferred_agent,
            None,
            "preferred_agent_not_registered",
            False,
        )
    preferred_health = health.effective(preferred)
    if _eligible(preferred, task) and preferred_health.availability is Availability.AVAILABLE:
        return RouteDecision(
            task.task_id,
            RouteStatus.SELECTED,
            task.preferred_agent,
            task.preferred_agent,
            "preferred_agent_available",
            False,
        )

    for fallback_id in task.allowed_fallbacks:
        fallback = registry.agents.get(fallback_id)
        if fallback is None or not _eligible(fallback, task):
            continue
        fallback_health = health.effective(fallback)
        if fallback_health.availability is Availability.AVAILABLE:
            return RouteDecision(
                task.task_id,
                RouteStatus.PENDING_APPROVAL,
                task.preferred_agent,
                fallback_id,
                (
                    f"preferred_unavailable:{preferred_health.reason};"
                    f"fallback_candidate:{fallback_id}"
                ),
                True,
            )

    return RouteDecision(
        task.task_id,
        RouteStatus.BLOCKED,
        task.preferred_agent,
        None,
        f"no_available_agent:preferred={preferred_health.reason}",
        False,
    )


def approve_fallback(decision: RouteDecision, approved_by: str) -> RouteDecision:
    if decision.status is not RouteStatus.PENDING_APPROVAL:
        raise ValueError("Only a pending fallback may be approved")
    if not approved_by.strip():
        raise ValueError("approved_by is required")
    return replace(
        decision,
        status=RouteStatus.SELECTED,
        requires_approval=False,
        approved_by=approved_by.strip(),
        reason=f"{decision.reason};fallback_approved_by:{approved_by.strip()}",
    )


@dataclass(frozen=True)
class CliInvocation:
    agent_id: str
    harness: str
    model: str
    cwd: str
    argv: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_cli_invocation(
    registry: AgentRegistry,
    task: TaskRouteSpec,
    decision: RouteDecision,
) -> CliInvocation:
    if decision.status is not RouteStatus.SELECTED or decision.selected_agent is None:
        raise PermissionError("Route must be selected and any fallback explicitly approved")
    agent = registry.agents[decision.selected_agent]
    values = {
        "workspace": str(Path(task.workspace).expanduser().resolve()),
        "task_file": str(Path(task.task_file).expanduser().resolve()),
        "prompt": task.prompt,
        "model": agent.model,
    }
    argv = tuple(part.format_map(values) for part in agent.command_template)
    if any("{" in part or "}" in part for part in argv):
        raise ValueError("Unresolved command template placeholder")
    return CliInvocation(
        agent_id=agent.agent_id,
        harness=agent.harness,
        model=agent.model,
        cwd=values["workspace"],
        argv=argv,
    )
