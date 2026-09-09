"""Live dispatch facts (Codex R2 supplement).

A dispatcher never revalidates against a constructor-time snapshot:
every reserve/dispatch reloads policy, LoopSpec registry, agent
registry, and health from their source files through a provider.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.orchestrator import AgentRegistry, HealthSnapshot
from shadow_dispatcher.dispatcher import load_loopspec_registry
from shadow_dispatcher.policy import RoutePolicy


@dataclass(frozen=True)
class DispatchFacts:
    policy: RoutePolicy
    loopspec_registry: dict[str, Any]
    agent_registry: AgentRegistry
    health: HealthSnapshot


FactsProvider = Callable[[], DispatchFacts]


def file_facts_provider(
    *,
    policy_path: str | Path,
    loopspec_registry_path: str | Path,
    agent_registry_path: str | Path,
    health_path: str | Path,
) -> FactsProvider:
    """Reload all four fact sources from disk on every call."""

    def provide() -> DispatchFacts:
        return DispatchFacts(
            policy=RoutePolicy.load(policy_path),
            loopspec_registry=load_loopspec_registry(loopspec_registry_path),
            agent_registry=AgentRegistry.load(agent_registry_path),
            health=HealthSnapshot.load(health_path),
        )

    return provide
