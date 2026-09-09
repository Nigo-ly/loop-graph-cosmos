"""Codex artifact adapter for auditable, replayable first-run outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.supervisor import LoopSupervisor, NodeHandler, SupervisorContext
from common.types import LoopResult
from fragment_loop.runtime import DEFAULT_DB_PATH, build_supervisor


def load_outcomes(path: str | Path) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Outcome artifact must be a JSON object")
    return data


def artifact_handlers(path: str | Path) -> dict[str, NodeHandler]:
    outcomes = load_outcomes(path)

    def handler_for(node: str) -> NodeHandler:
        def handle(context: SupervisorContext) -> LoopResult:
            value = outcomes.get(node)
            if not isinstance(value, dict):
                raise ValueError(f"Missing recorded outcome for node: {node}")
            return LoopResult(**value)

        return handle

    return {node: handler_for(node) for node in outcomes}


def run_recorded_outcomes(
    run_id: str,
    outcomes_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopSupervisor:
    supervisor = build_supervisor(db_path, artifact_handlers(outcomes_path))
    supervisor.run(run_id)
    return supervisor
