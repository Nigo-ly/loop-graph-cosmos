"""Approved child Loop for the first isolated capability-mapping experiment."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from common.checkpoint import LoopCheckpoint
from common.execution import translate_view_edit
from common.supervisor import AdmissionState
from fragment_loop.artifact_adapter import artifact_handlers
from fragment_loop.runtime import DEFAULT_DB_PATH, build_supervisor
from fragment_loop.spec import AGENTS_CLI_SKILL_MAPPING_V1


def child_run_id(parent_run_id: str, fragment_id: str) -> str:
    identity = ":".join(
        (
            AGENTS_CLI_SKILL_MAPPING_V1.loop_id,
            AGENTS_CLI_SKILL_MAPPING_V1.version,
            parent_run_id,
            fragment_id,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def register_approved_mapping(
    parent_run_id: str,
    fragment_id: str,
    *,
    input_refs: list[str],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(db_path, spec=AGENTS_CLI_SKILL_MAPPING_V1)
    run_id = child_run_id(parent_run_id, fragment_id)
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


def run_mapping(
    run_id: str,
    outcomes_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(
        db_path,
        artifact_handlers(outcomes_path),
        spec=AGENTS_CLI_SKILL_MAPPING_V1,
    )
    return supervisor.run(run_id)


def reconcile_parent(
    child_run_id_value: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(db_path, spec=AGENTS_CLI_SKILL_MAPPING_V1)
    child = supervisor.store.latest(child_run_id_value)
    if child is None or child.parent_loop_id is None:
        raise ValueError("Child checkpoint or parent reference is missing")
    if child.status != "passed":
        raise ValueError(f"Child Loop has not passed: {child.status}")
    parent = supervisor.store.latest(child.parent_loop_id)
    if parent is None:
        raise ValueError("Parent checkpoint is missing")
    child_runs = list(parent.eval_results.get("child_runs", []))
    if child.run_id not in child_runs:
        child_runs.append(child.run_id)
    merged_eval_results = {
        **parent.eval_results,
        "child_runs": child_runs,
        "capability_mapping": "passed",
        "asset_maturity": "validated_with_limits",
        "asset_created": bool(child.eval_results.get("asset_created", False)),
        "asset_ref": child.eval_results.get("asset_ref"),
    }
    if (
        parent.status == "passed"
        and parent.stop_reason == "approved_child_loop_completed"
        and parent.eval_results == merged_eval_results
    ):
        return parent
    raw_parent = supervisor.store.latest_raw(parent.run_id)
    if raw_parent is None:
        raise ValueError("Parent checkpoint is missing")
    return supervisor.store.save(
        replace(
            raw_parent,
            status="passed",
            stop_reason="approved_child_loop_completed",
            resume_condition=(
                "Start a new approved Loop only for a concrete ADK project "
                "or isolated installation test"
            ),
            unresolved_issues=[],
            eval_results=translate_view_edit(
                existing=raw_parent.eval_results,
                edited_flat=merged_eval_results,
            ),
        ),
        event_type="parent_reconciled",
        revision_reason="approved child Loop completed",
    )
