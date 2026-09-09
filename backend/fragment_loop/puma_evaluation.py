"""Approved PUMA stop-policy child Loop and parent reconciliation."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from common.checkpoint import LoopCheckpoint
from common.execution import translate_view_edit
from common.supervisor import AdmissionState
from fragment_loop.artifact_adapter import artifact_handlers
from fragment_loop.runtime import DEFAULT_DB_PATH, build_supervisor
from fragment_loop.spec import PUMA_STOP_POLICY_EVAL_V1


def puma_child_run_id(parent_run_id: str, fragment_id: str) -> str:
    identity = ":".join(
        (
            PUMA_STOP_POLICY_EVAL_V1.loop_id,
            PUMA_STOP_POLICY_EVAL_V1.version,
            parent_run_id,
            fragment_id,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def register_puma_evaluation(
    parent_run_id: str,
    fragment_id: str,
    *,
    input_refs: list[str],
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(db_path, spec=PUMA_STOP_POLICY_EVAL_V1)
    run_id = puma_child_run_id(parent_run_id, fragment_id)
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
    checkpoint = supervisor.transition_admission(checkpoint.run_id, AdmissionState.PREFLIGHT_PASSED)
    return supervisor.transition_admission(checkpoint.run_id, AdmissionState.APPROVED)


def run_puma_evaluation(
    run_id: str,
    outcomes_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(
        db_path, artifact_handlers(outcomes_path), spec=PUMA_STOP_POLICY_EVAL_V1
    )
    return supervisor.run(run_id)


def reconcile_puma_parent(
    child_run_id: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    supervisor = build_supervisor(db_path, spec=PUMA_STOP_POLICY_EVAL_V1)
    child = supervisor.store.latest(child_run_id)
    if child is None or child.parent_loop_id is None or child.status != "passed":
        raise ValueError("Passed PUMA child checkpoint with parent is required")
    parent = supervisor.store.latest(child.parent_loop_id)
    if parent is None:
        raise ValueError("Parent checkpoint is missing")
    raw_parent = supervisor.store.latest_raw(parent.run_id)
    if raw_parent is None:
        raise ValueError("Parent checkpoint is missing")
    return supervisor.store.save(
        replace(
            raw_parent,
            status="passed",
            stop_reason="puma_stop_policy_eval_completed",
            resume_condition="Harvest real Loop traces before changing production stop policy",
            unresolved_issues=[],
            eval_results=translate_view_edit(
                existing=raw_parent.eval_results,
                edited_flat={
                    **parent.eval_results,
                    "puma_stop_policy_eval": "passed",
                    "child_runs": [
                        *parent.eval_results.get("child_runs", []),
                        child.run_id,
                    ],
                    "asset_created": bool(child.eval_results.get("asset_created", False)),
                    "asset_ref": child.eval_results.get("asset_ref"),
                },
            ),
        ),
        event_type="parent_reconciled",
        revision_reason="approved child Loop completed",
    )
