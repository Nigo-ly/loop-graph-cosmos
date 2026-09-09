"""Approved interactive-knowledge child Loop and parent reconciliation."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from common.checkpoint import LoopCheckpoint
from common.execution import translate_view_edit
from common.supervisor import AdmissionState
from fragment_loop.artifact_adapter import artifact_handlers
from fragment_loop.runtime import DEFAULT_DB_PATH, build_supervisor
from fragment_loop.spec import INTERACTIVE_KNOWLEDGE_PATTERN_V1


def interactive_child_run_id(parent_run_id: str, fragment_id: str) -> str:
    identity = ":".join(
        (
            INTERACTIVE_KNOWLEDGE_PATTERN_V1.loop_id,
            INTERACTIVE_KNOWLEDGE_PATTERN_V1.version,
            parent_run_id,
            fragment_id,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def register_interactive_knowledge(
    parent_run_id: str,
    fragment_id: str,
    *,
    input_refs: list[str],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(db_path, spec=INTERACTIVE_KNOWLEDGE_PATTERN_V1)
    run_id = interactive_child_run_id(parent_run_id, fragment_id)
    existing = supervisor.store.latest(run_id)
    if existing is not None:
        return existing
    checkpoint = supervisor.register(
        fragment_id,
        admission_state=AdmissionState.REQUESTED,
        input_refs=input_refs,
        run_id=run_id,
        parent_loop_id=parent_run_id,
    )
    checkpoint = supervisor.transition_admission(
        checkpoint.run_id, AdmissionState.PREFLIGHT_PASSED
    )
    return supervisor.transition_admission(checkpoint.run_id, AdmissionState.APPROVED)


def run_interactive_knowledge(
    run_id: str,
    outcomes_path: str | Path,
    *,
    max_steps: int | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(
        db_path,
        artifact_handlers(outcomes_path),
        spec=INTERACTIVE_KNOWLEDGE_PATTERN_V1,
    )
    return supervisor.run(run_id, max_steps=max_steps)


def reconcile_interactive_parent(
    child_run_id: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(db_path, spec=INTERACTIVE_KNOWLEDGE_PATTERN_V1)
    child = supervisor.store.latest(child_run_id)
    if child is None or child.parent_loop_id is None or child.status != "passed":
        raise ValueError("Passed interactive-knowledge child checkpoint with parent is required")
    parent = supervisor.store.latest(child.parent_loop_id)
    if parent is None:
        raise ValueError("Parent checkpoint is missing")
    child_runs = list(parent.eval_results.get("child_runs", []))
    if child.run_id not in child_runs:
        child_runs.append(child.run_id)
    raw_parent = supervisor.store.latest_raw(parent.run_id)
    if raw_parent is None:
        raise ValueError("Parent checkpoint is missing")
    return supervisor.store.save(
        replace(
            raw_parent,
            status="passed",
            stop_reason="interactive_knowledge_pattern_completed",
            resume_condition="Run the smallest approved prototype before raising maturity",
            unresolved_issues=[],
            eval_results=translate_view_edit(
                existing=raw_parent.eval_results,
                edited_flat={
                    **parent.eval_results,
                    "interactive_knowledge_pattern": "passed",
                    "child_runs": child_runs,
                    "asset_created": bool(child.eval_results.get("asset_created", False)),
                    "asset_ref": child.eval_results.get("asset_ref"),
                },
            ),
        ),
        event_type="parent_reconciled",
        revision_reason="approved child Loop completed",
    )
