"""Registration and preflight runtime for the first real fragment Loop."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore
from common.execution import translate_view_edit
from common.supervisor import AdmissionState, LoopSpec, LoopSupervisor, NodeHandler
from fragment_loop.cognitive_loop import LocalCognitiveV1Loop
from fragment_loop.intake import (
    FragmentEnvelope,
    _split_frontmatter,
    fragment_title_for,
    load_fragment,
    preflight_reasons,
)
from fragment_loop.spec import PHONE_FRAGMENT_LINK_V1

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "loop_state.sqlite3"


def _intake_does_not_execute(
    _fragment_text: str, _route: str
) -> Mapping[str, object]:
    raise RuntimeError("cognitive intake never executes a producer")


def capture_identity_sha256(fragment_id: str, raw_text: str) -> str:
    """Bind user content/metadata; only named organizer runtime fields are excluded."""
    fields, body = _split_frontmatter(raw_text)
    for key in ("pipeline_status", "processed_at", "organized_at"):
        fields.pop(key, None)
    canonical = {"fragment_id": fragment_id, "frontmatter": fields, "body": body}
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def register_cognitive_intake(
    source_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> LoopCheckpoint:
    """Register one explicitly selected fragment in the current V1 intake."""
    source = Path(source_path).expanduser().resolve()
    original_bytes = source.read_bytes()
    fragment = load_fragment(source)
    if source.read_bytes() != original_bytes:
        raise ValueError("fragment changed during intake")
    if fragment.admission_state is not AdmissionState.REQUESTED:
        raise PermissionError("Current nigo-loop: true approval is required")
    title, title_source = fragment_title_for(fragment)
    return LocalCognitiveV1Loop(
        SQLiteCheckpointStore(db_path), _intake_does_not_execute
    ).register_intake(
        fragment_id=fragment.fragment_id,
        fragment_text=fragment.raw_content,
        source_ref=fragment.source_path,
        privacy_level=fragment.privacy_level,
        fragment_title=title,
        fragment_title_source=title_source,
        captured_at=fragment.captured_at,
        raw_sha256=hashlib.sha256(original_bytes).hexdigest(),
        identity_sha256=capture_identity_sha256(fragment.fragment_id,
                                               original_bytes.decode("utf-8")),
    )


def build_supervisor(
    db_path: str | Path = DEFAULT_DB_PATH,
    handlers: dict[str, NodeHandler] | None = None,
    spec: LoopSpec = PHONE_FRAGMENT_LINK_V1,
) -> LoopSupervisor:
    return LoopSupervisor(
        SQLiteCheckpointStore(db_path),
        spec,
        handlers or {},
    )


def deterministic_run_id(fragment: FragmentEnvelope) -> str:
    identity = ":".join(
        (
            PHONE_FRAGMENT_LINK_V1.loop_id,
            PHONE_FRAGMENT_LINK_V1.version,
            fragment.fragment_id,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def reopened_run_id(fragment: FragmentEnvelope, episode: int) -> str:
    identity = ":".join(
        (
            PHONE_FRAGMENT_LINK_V1.loop_id,
            PHONE_FRAGMENT_LINK_V1.version,
            fragment.fragment_id,
            f"episode:{episode}",
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def register_and_preflight(
    source_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[LoopCheckpoint, list[str]]:
    fragment = load_fragment(source_path)
    supervisor = build_supervisor(db_path)
    existing = supervisor.store.latest_for_fragment(
        PHONE_FRAGMENT_LINK_V1.loop_id, fragment.fragment_id
    )
    if existing is not None:
        if (
            fragment.admission_state is AdmissionState.REQUESTED
            and existing.status == AdmissionState.PREFLIGHT_PASSED.value
            and not existing.unresolved_issues
        ):
            existing = supervisor.transition_admission(
                existing.run_id, AdmissionState.APPROVED
            )
            raw_existing = supervisor.store.latest_raw(existing.run_id)
            assert raw_existing is not None
            existing = supervisor.store.save(
                replace(
                    raw_existing,
                    eval_results=translate_view_edit(
                        existing=raw_existing.eval_results,
                        edited_flat={
                            **existing.eval_results,
                            "approval": {
                                "approved": True,
                                "source": "nigo-loop",
                            },
                        },
                    ),
                    resume_condition="Approved by nigo-loop; ready for Supervisor execution",
                )
            )
        return existing, list(existing.unresolved_issues)

    title, title_source = fragment_title_for(fragment)
    checkpoint = supervisor.register(
        fragment.fragment_id,
        admission_state=fragment.admission_state,
        input_refs=[fragment.source_path],
        run_id=deterministic_run_id(fragment),
        fragment_title=title,
        fragment_title_source=title_source,
    )
    reasons = preflight_reasons(fragment)
    if reasons:
        raw_checkpoint = supervisor.store.latest_raw(checkpoint.run_id)
        assert raw_checkpoint is not None
        checkpoint = supervisor.store.save(
            replace(
                raw_checkpoint,
                unresolved_issues=reasons,
                eval_results=translate_view_edit(
                    existing=raw_checkpoint.eval_results,
                    edited_flat={
                        "preflight": {
                            "passed": False,
                            "reasons": reasons,
                        }
                    },
                ),
            )
        )
        return checkpoint, reasons

    checkpoint = supervisor.transition_admission(checkpoint.run_id, AdmissionState.PREFLIGHT_PASSED)
    raw_checkpoint = supervisor.store.latest_raw(checkpoint.run_id)
    assert raw_checkpoint is not None
    checkpoint = supervisor.store.save(
        replace(
            raw_checkpoint,
            eval_results=translate_view_edit(
                existing=raw_checkpoint.eval_results,
                edited_flat={
                    "preflight": {
                        "passed": True,
                        "input_type": fragment.input_type,
                        "privacy_level": fragment.privacy_level,
                        "source_path": fragment.source_path,
                    }
                },
            ),
            unresolved_issues=[],
            resume_condition="nigo-loop approval accepted; finalize admission",
        )
    )
    checkpoint = supervisor.transition_admission(checkpoint.run_id, AdmissionState.APPROVED)
    raw_checkpoint = supervisor.store.latest_raw(checkpoint.run_id)
    assert raw_checkpoint is not None
    checkpoint = supervisor.store.save(
        replace(
            raw_checkpoint,
            eval_results=translate_view_edit(
                existing=raw_checkpoint.eval_results,
                edited_flat={
                    **checkpoint.eval_results,
                    "approval": {
                        "approved": True,
                        "source": "nigo-loop",
                    },
                },
            ),
            resume_condition="Approved by nigo-loop; ready for Supervisor execution",
        )
    )
    return checkpoint, []


def reopen_fragment(
    source_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    opened_by: str = "nigo",
) -> LoopCheckpoint:
    """Explicit reopen of a resolved fragment family (nigo's trusted intake
    action only). The frozen `nigo-loop` approval marker must be present in
    the CURRENT source at reopen time — a withdrawn approval rejects the
    reopen with zero writes. Registers the next episode atomically with the
    fragment's current title; ordinary rescans must keep using
    register_and_preflight, which never reopens.
    """
    fragment = load_fragment(source_path)
    if fragment.admission_state is not AdmissionState.REQUESTED:
        raise PermissionError(
            "Reopen requires the current frozen nigo-loop approval marker in the source"
        )
    supervisor = build_supervisor(db_path)
    attempts = supervisor.store.family_attempts(
        PHONE_FRAGMENT_LINK_V1.loop_id, fragment.fragment_id
    )
    if not attempts:
        raise KeyError(f"Unknown fragment family: {fragment.fragment_id}")
    next_episode = max(attempt.attempt_episode for attempt in attempts) + 1
    title, title_source = fragment_title_for(fragment)
    return supervisor.reopen_episode(
        fragment.fragment_id,
        run_id=reopened_run_id(fragment, next_episode),
        input_refs=[fragment.source_path],
        fragment_title=title or None,
        fragment_title_source=title_source or None,
        opened_by=opened_by,
        approval_evidence={"approved": True, "source": "nigo-loop", "reopened_by": opened_by},
    )
