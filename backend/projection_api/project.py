"""Pure projection from checkpoint payloads to contract v2 shapes.

The *_out and *_item/_summary/_detail functions are pure; the compute_*
helpers combine the read-only store with those projections and are shared
by the HTTP server and the snapshot export tool.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fragment_loop.cognitive_draft_view import build_cognitive_draft_view
from fragment_loop.spec import FRAGMENT_COGNITIVE_LOCAL_V1, PHONE_FRAGMENT_LINK_V1

from . import store
from .mapping import display_state_for, next_step_for
from .sanitize import sanitize_refs
from .store import HistoryRow, RunRow

# A family reaches resolution when any attempt lands in one of these
# statuses: the task succeeded (passed), a human closed it (cancelled),
# or it was replaced explicitly (superseded).
FAMILY_RESOLVED_STATUSES = frozenset({"passed", "cancelled", "superseded"})


@dataclass(frozen=True)
class AttemptInfo:
    attempt_group_id: str
    attempt_episode: int
    is_current: bool
    superseded_by_run_id: str | None


def attempt_group_id(loop_id: str, fragment_id: str) -> str:
    """Deterministic attempt-family id from (loop_id, fragment_id)."""
    canonical = "\x1f".join((loop_id, fragment_id))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_attempt_info(rows: list[RunRow]) -> dict[str, AttemptInfo]:
    """Authoritative attempt-family currentness from PERSISTED episodes.

    `attempt_episode` is assigned atomically at registration (ordinary
    registrations join the open episode; only an explicit reopen advances
    it), so concurrent pre-resolution attempts always share one episode
    and resolve together — no read-time inference. Inside one episode: a
    `passed`/`cancelled`/`superseded` attempt resolves it (no current
    card, non-resolving attempts point at the resolver); otherwise only
    the latest attempt is current. Older episodes are never current.
    Payloads predating the field carry episode 0, which preserves the
    revision-5 behavior for historical families.
    """
    families: dict[tuple[str, str], list[RunRow]] = {}
    for row in rows:
        families.setdefault((row.loop_id, row.fragment_id), []).append(row)

    info: dict[str, AttemptInfo] = {}
    for (loop_id, fragment_id), attempts in families.items():
        group = attempt_group_id(loop_id, fragment_id)
        episode_of = {
            attempt.run_id: int(attempt.payload.get("attempt_episode") or 0)
            for attempt in attempts
        }
        max_episode = max(episode_of.values(), default=0)
        resolver_of: dict[int, RunRow] = {}
        latest_of: dict[int, RunRow] = {}
        for attempt in sorted(attempts, key=lambda item: item.sequence):
            run_episode = episode_of[attempt.run_id]
            if (
                run_episode not in resolver_of
                and attempt.status in FAMILY_RESOLVED_STATUSES
            ):
                resolver_of[run_episode] = attempt
            latest_of[run_episode] = attempt

        for attempt in attempts:
            run_episode = episode_of[attempt.run_id]
            resolver = resolver_of.get(run_episode)
            if run_episode < max_episode or resolver is not None:
                info[attempt.run_id] = AttemptInfo(
                    attempt_group_id=group,
                    attempt_episode=run_episode,
                    is_current=False,
                    superseded_by_run_id=(
                        None if resolver is not None and attempt.run_id == resolver.run_id
                        else (resolver.run_id if resolver is not None else None)
                    ),
                )
                continue
            is_current = attempt.run_id == latest_of[run_episode].run_id
            info[attempt.run_id] = AttemptInfo(
                attempt_group_id=group,
                attempt_episode=run_episode,
                is_current=is_current,
                superseded_by_run_id=None if is_current else latest_of[run_episode].run_id,
            )
    return info


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def budget_used_out(payload_budget_used: object, elapsed_seconds: int = 0) -> dict[str, int]:
    """Project budget_used: active tokens exclude all cache_* keys."""
    used = payload_budget_used if isinstance(payload_budget_used, dict) else {}
    if "input_tokens" in used or "output_tokens" in used:
        active_tokens = int(used.get("input_tokens") or 0) + int(used.get("output_tokens") or 0)
    elif "tokens" in used:
        active_tokens = int(used.get("tokens") or 0)
    else:
        active_tokens = 0
    return {
        "active_tokens": active_tokens,
        "calls": int(used.get("tool_calls") or 0),
        "elapsed_seconds": max(0, int(elapsed_seconds)),
    }


def budget_limits_out(payload_budget_limits: object) -> dict[str, int]:
    """Project budget_limits; keys absent from the payload are omitted."""
    limits = payload_budget_limits if isinstance(payload_budget_limits, dict) else {}
    out: dict[str, int] = {}
    for source_key, out_key in (
        ("tokens", "active_tokens"),
        ("tool_calls", "calls"),
        ("seconds", "seconds"),
        ("iterations", "iterations"),
    ):
        if source_key in limits and limits[source_key] is not None:
            out[out_key] = int(limits[source_key])
    return out


def elapsed_seconds_for(history: list[HistoryRow]) -> int:
    """int(latest updated_at - earliest created_at) over the run, clamped >= 0."""
    created: list[datetime] = [
        ts
        for row in history
        if (ts := _parse_ts(row.payload.get("created_at"))) is not None
    ]
    updated: list[datetime] = [
        ts
        for row in history
        if (ts := _parse_ts(row.payload.get("updated_at"))) is not None
    ]
    if not created or not updated:
        return 0
    return max(0, int((max(updated) - min(created)).total_seconds()))


def queue_item(row: RunRow) -> dict[str, Any]:
    payload = row.payload
    refs = sanitize_refs(payload.get("input_refs"))
    eval_results = payload.get("eval_results")
    cognitive_input = (
        eval_results.get("cognitive_input")
        if isinstance(eval_results, dict)
        else None
    )
    route_pending = (
        row.loop_id == FRAGMENT_COGNITIVE_LOCAL_V1.loop_id
        and isinstance(cognitive_input, dict)
        and cognitive_input.get("expected_route") is None
    )
    return {
        "run_id": payload.get("run_id") or row.run_id,
        "fragment_id": payload.get("fragment_id") or row.fragment_id,
        "loop_id": payload.get("loop_id") or row.loop_id,
        # Additive optional fields (contract v2, additive-fields rule):
        # verbatim payload values for the semantic queue title.
        "subject": payload.get("fragment_title") or None,
        "subject_source": payload.get("fragment_title_source") or None,
        "goal": payload.get("goal") or None,
        "status": row.status,
        "display_state": display_state_for(row.status),
        "source_file": refs[0] if refs else None,
        "next_step": (
            "等待语义展开与路线判断"
            if row.status == "approved" and route_pending
            else next_step_for(row.status)
        ),
        "updated_at": payload.get("updated_at") or row.committed_at,
    }


def run_summary(
    row: RunRow,
    parent_run_id: str | None,
    elapsed_seconds: int,
    attempt: AttemptInfo | None = None,
) -> dict[str, Any]:
    payload = row.payload
    attempt = attempt or AttemptInfo(
        attempt_group_id=attempt_group_id(row.loop_id, row.fragment_id),
        attempt_episode=0,
        is_current=True,
        superseded_by_run_id=None,
    )
    return {
        "run_id": payload.get("run_id") or row.run_id,
        "parent_run_id": parent_run_id,
        "loop_id": payload.get("loop_id") or row.loop_id,
        "fragment_id": payload.get("fragment_id") or row.fragment_id,
        # Additive optional fields (contract v2, additive-fields rule):
        # verbatim payload values; consumers must tolerate their absence.
        "goal": payload.get("goal") or None,
        "subject": payload.get("fragment_title") or None,
        "subject_source": payload.get("fragment_title_source") or None,
        "attempt_group_id": attempt.attempt_group_id,
        "attempt_episode": attempt.attempt_episode,
        "is_current": attempt.is_current,
        "superseded_by_run_id": attempt.superseded_by_run_id,
        "status": row.status,
        "display_state": display_state_for(row.status),
        "current_node": row.current_node or None,
        "iterations": int(payload.get("iteration") or 0),
        "events": row.events,
        "evaluator_version": payload.get("evaluator_version") or "",
        "budget_used": budget_used_out(payload.get("budget_used"), elapsed_seconds),
        "stop_reason": payload.get("stop_reason"),
        "updated_at": payload.get("updated_at") or row.committed_at,
    }


def timeline_entry(row: HistoryRow) -> dict[str, Any]:
    summary = f"{row.event_type}: status={row.status} node={row.current_node}"
    if row.revision_reason:
        summary += f" reason={row.revision_reason}"
    return {
        "sequence": row.sequence,
        "event_type": row.event_type,
        "node": row.current_node or None,
        "created_at": row.committed_at,
        "summary": summary,
    }


def run_detail(
    history: list[HistoryRow],
    parent_run_id: str | None,
) -> dict[str, Any]:
    latest = history[-1]
    payload = latest.payload
    elapsed = elapsed_seconds_for(history)
    eval_results = payload.get("eval_results") or {}
    detail = {
        "run_id": payload.get("run_id") or latest.run_id,
        "parent_run_id": parent_run_id,
        "loop_id": payload.get("loop_id") or latest.loop_id,
        "loopspec_version": payload.get("loopspec_version") or "1",
        "fragment_id": payload.get("fragment_id") or latest.fragment_id,
        "goal": payload.get("goal") or "",
        "status": latest.status,
        "display_state": display_state_for(latest.status),
        "current_node": latest.current_node or None,
        "iterations": int(payload.get("iteration") or 0),
        "events": len(history),
        "worker_version": payload.get("worker_version") or "",
        "evaluator_version": payload.get("evaluator_version") or "",
        "budget_limits": budget_limits_out(payload.get("budget_limits")),
        "budget_used": budget_used_out(payload.get("budget_used"), elapsed),
        "stop_reason": payload.get("stop_reason"),
        "resume_condition": payload.get("resume_condition"),
        "updated_at": payload.get("updated_at") or latest.committed_at,
        "eval_results": eval_results,
        "unresolved_issues": payload.get("unresolved_issues") or [],
        "timeline": [timeline_entry(row) for row in history],
        "evidence_refs": sanitize_refs(payload.get("evidence_refs")),
        "artifact_refs": sanitize_refs(payload.get("artifact_refs")),
        # The checkpoint schema has no asset_refs field; documented limitation.
        "asset_refs": [],
    }
    # Package B (additive, contract v2 additive-fields rule): a recomputed,
    # allowlisted display projection of a cognitive draft. It is attached
    # only when the persisted eval_results revalidate cleanly; any drift
    # fails closed to absence, and non-cognitive runs never carry the key.
    if isinstance(eval_results, dict) and "cognitive_contract_status" in eval_results:
        draft_view = build_cognitive_draft_view(eval_results)
        if draft_view is not None:
            detail["cognitive_draft_view"] = draft_view
    return detail


def _resolve_parent_run_id(db_path: str, payload: dict[str, Any]) -> str | None:
    parent_loop_id = payload.get("parent_loop_id")
    if not parent_loop_id:
        return None
    resolved: str | None = store.latest_run_id_for_loop(db_path, str(parent_loop_id))
    return resolved


def compute_queue_items(db_path: str) -> list[dict[str, Any]]:
    """Queue = runs whose display_state is queued, oldest waiting first."""
    rows = store.latest_runs(db_path)
    cognitive_fragments = {
        row.fragment_id
        for row in rows
        if row.loop_id == FRAGMENT_COGNITIVE_LOCAL_V1.loop_id
    }
    items = [
        queue_item(row)
        for row in rows
        if display_state_for(row.status) == "queued"
        and not (
            row.loop_id == PHONE_FRAGMENT_LINK_V1.loop_id
            and row.fragment_id in cognitive_fragments
        )
    ]
    items.sort(key=lambda item: (str(item["updated_at"]), str(item["run_id"])))
    return items


def compute_run_summaries(db_path: str) -> list[dict[str, Any]]:
    """Full run summary list, newest updated first."""
    rows = store.latest_runs(db_path)
    attempt_info = compute_attempt_info(rows)
    summaries = []
    for row in rows:
        history = store.run_history(db_path, row.run_id)
        summaries.append(
            run_summary(
                row,
                _resolve_parent_run_id(db_path, row.payload),
                elapsed_seconds_for(history),
                attempt_info.get(row.run_id),
            )
        )
    summaries.sort(
        key=lambda item: (str(item["updated_at"]), str(item["run_id"])),
        reverse=True,
    )
    return summaries


def compute_run_detail(db_path: str, run_id: str) -> dict[str, Any] | None:
    history = store.run_history(db_path, run_id)
    if not history:
        return None
    return run_detail(history, _resolve_parent_run_id(db_path, history[-1].payload))
