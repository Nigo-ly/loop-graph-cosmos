"""Loop V1 Package B: one-command offline local experience.

One explicit entry assembles the complete local closed loop — a handwritten
synthetic fragment enters the Package A public research simulation with the
Package C explicit personal context snapshot (one synthetic confirmed user
fact, one explicit profile inference, one synthetic obsidian record), parks
at the human decision, serves the existing read-only projection and the
decision API wired to a temporary local notes directory, and supports
keep / reject / withdraw with restart recovery that never repeats a
research call.

- No network, no real Vault, no credentials, no private data, no model call:
  the producer is the Package A ``build_simulate_pipeline`` handwritten
  fixture chain, and the notes directory is a caller-chosen or temporary
  local directory.
- ``--live`` is hard-blocked with the fixed error
  ``local_experience_live_blocked`` before any file, store, server, or
  credential side effect.
- The printed summary is minimal and recomputable: ids, states, ports, call
  counts, and the restart check — never draft bodies, credentials, or raw
  HTTP bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.cognitive_loop import (
    CognitiveDecisionError,
    SyntheticCognitiveLoop,
)
from fragment_loop.cognitive_note import (
    CognitiveNoteWithdrawalOutcome,
    publish_kept_cognitive_note,
    withdraw_kept_cognitive_note,
)
from fragment_loop.cognitive_server import (
    decision_idempotency_key,
    make_cognitive_decision_server,
    withdrawal_idempotency_key,
)
from fragment_loop.public_research_service import (
    SIMULATE_FRAGMENT_ID,
    SIMULATE_FRAGMENT_TEXT,
    build_simulate_pipeline,
    simulate_personal_context,
)
from fragment_loop.spec import FRAGMENT_COGNITIVE_SYNTHETIC_R1B
from projection_api.project import compute_run_detail
from projection_api.server import make_server as make_projection_server
from projection_api.store import latest_runs

LIVE_BLOCKED_ERROR = "local_experience_live_blocked"
DB_NAME_PREFIX = "fragment-cognitive-package-b-"
EXPERIENCE_FRAGMENT_ID = SIMULATE_FRAGMENT_ID
EXPERIENCE_FRAGMENT_TEXT = SIMULATE_FRAGMENT_TEXT
EXPERIENCE_RUN_ID = "cognitive-r1b-" + hashlib.sha256(
    EXPERIENCE_FRAGMENT_ID.encode()
).hexdigest()
SELFCHECK_THOUGHT_CATEGORY = "离线合成体验"

_ALLOWED_STATES = {
    ("paused", "awaiting_cognitive_decision"),
    ("passed", "cognitive_draft_kept"),
    ("cancelled", "cognitive_result_rejected"),
    ("passed", "cognitive_withdrawal_pending"),
    ("passed", "cognitive_note_withdrawn"),
}


class LocalExperienceError(ValueError):
    """The local experience input or database failed a fixed boundary."""


def _reject_symlink_segments(path: Path) -> None:
    """Fail closed when ANY caller-controlled path segment is a symlink.

    ``Path.resolve`` would silently follow a symlinked parent directory, so
    the check walks every segment of the caller-supplied absolute path before
    any file, store, or server side effect. The only exemption is the
    platform temporary-directory chain itself (macOS temp roots are reached
    through the ``/var`` -> ``/private/var`` symlink): segments on the
    ``tempfile.gettempdir()`` prefix are not caller-controlled, while every
    segment below the temp root is still checked, so a symlinked parent
    inside a temp directory is rejected all the same.
    """
    temp_anchor = Path(tempfile.gettempdir())
    for segment in (path, *path.parents):
        if not segment.is_symlink():
            continue
        try:
            # A segment ON the system temp prefix (e.g. /var) is the
            # platform's own redirect, never a caller-planted link.
            temp_anchor.relative_to(segment)
            continue
        except ValueError:
            pass
        raise LocalExperienceError(
            "Package B paths must not traverse a symbolic link"
        )


def _absolute(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _validate_db_path(path: Path) -> Path:
    raw = _absolute(path)
    _reject_symlink_segments(raw)
    resolved = raw.resolve()
    if not resolved.name.startswith(DB_NAME_PREFIX) or resolved.suffix != ".sqlite3":
        raise LocalExperienceError(
            "Package B database filename must be clearly synthetic "
            f"({DB_NAME_PREFIX}*.sqlite3)"
        )
    return resolved


def _existing_database_is_experience(path: Path) -> bool:
    """Revalidate an existing DB: exactly our one run, unaltered, in a known state."""
    try:
        rows = latest_runs(str(path))
    except sqlite3.Error:
        return False
    if not (
        len(rows) == 1
        and rows[0].run_id == EXPERIENCE_RUN_ID
        and rows[0].fragment_id == EXPERIENCE_FRAGMENT_ID
        and rows[0].loop_id == FRAGMENT_COGNITIVE_SYNTHETIC_R1B.loop_id
    ):
        return False
    if (rows[0].status, rows[0].payload.get("stop_reason")) not in _ALLOWED_STATES:
        return False
    checkpoint = SQLiteCheckpointStore(path).latest(EXPERIENCE_RUN_ID)
    if checkpoint is None:
        return False
    try:
        return bool(SyntheticCognitiveLoop._decision_material_is_valid(checkpoint))
    except (TypeError, ValueError):
        return False


def _no_generation(_fragment_text: str, _route: str) -> Mapping[str, object]:
    raise RuntimeError("a recovered local experience cannot regenerate content")


def prepare_experience(db_path: str | Path) -> dict[str, object]:
    """Create or recover the one fixed Package B experience database.

    A fresh database runs the Package A handwritten simulation exactly once
    and parks at the human decision; an existing valid experience database is
    only recovered — recovery provably repeats no search, fetch, classifier,
    or analyst call.
    """
    path = _validate_db_path(Path(db_path))
    counters = {
        "search_transport": 0,
        "fetch_transport": 0,
        "classifier": 0,
        "analyst": 0,
    }
    if path.exists():
        if not _existing_database_is_experience(path):
            raise LocalExperienceError(
                "Package B refuses an existing non-experience database"
            )
        service = SyntheticCognitiveLoop(SQLiteCheckpointStore(path), _no_generation)
        waiting = service.run(EXPERIENCE_RUN_ID)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise LocalExperienceError(
                "Package B database appeared concurrently"
            ) from error
        else:
            os.close(descriptor)
        pipeline = build_simulate_pipeline(
            counters, personal_context=simulate_personal_context()
        )
        service = SyntheticCognitiveLoop(SQLiteCheckpointStore(path), pipeline)
        registered = service.register(
            fragment_id=EXPERIENCE_FRAGMENT_ID,
            fragment_text=EXPERIENCE_FRAGMENT_TEXT,
            route="research",
        )
        if registered.run_id != EXPERIENCE_RUN_ID:
            raise LocalExperienceError("Package B run binding drifted")
        waiting = service.run(registered.run_id)
    calls_after_run = dict(counters)
    # Restart recovery proof: a fresh service whose producer must never be
    # called again resumes the same checkpoint without repeating research.
    restarted = SyntheticCognitiveLoop(SQLiteCheckpointStore(path), _no_generation)
    resumed = restarted.run(EXPERIENCE_RUN_ID)
    restart_extra_calls = {
        key: counters[key] - calls_after_run[key] for key in counters
    }
    return {
        "db_path": str(path),
        "run_id": waiting.run_id,
        "fragment_id": waiting.fragment_id,
        "status": waiting.status,
        "stop_reason": waiting.stop_reason,
        "resumed_status": resumed.status,
        "cognitive_decision": waiting.eval_results.get("cognitive_decision"),
        "content_lifecycle": waiting.eval_results.get("content_lifecycle"),
        "evidence_level": waiting.eval_results.get("evidence_level"),
        "call_counts": calls_after_run,
        "restart_extra_calls": restart_extra_calls,
    }


def _validated_notes_root(notes_dir: str | Path) -> Path:
    raw = _absolute(Path(notes_dir))
    # Reject any symlink segment before mkdir ever touches the filesystem.
    _reject_symlink_segments(raw)
    raw.mkdir(parents=True, exist_ok=True)
    # Canonicalize up front (macOS temp roots live behind /var symlinks); the
    # note bridge re-validates the real directory again at write time.
    return raw.resolve(strict=True)


def make_experience_servers(
    db_path: str | Path,
    notes_dir: str | Path,
    *,
    projection_port: int = 0,
    decision_port: int = 0,
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """Bind the existing projection and decision APIs to one validated DB.

    Both ports default to 0 (operator-assigned random loopback ports); the
    production 5679/5684 ports are never used unless the caller explicitly
    injects them.
    """
    path = _validate_db_path(Path(db_path))
    if not path.is_file() or not _existing_database_is_experience(path):
        raise LocalExperienceError("Package B refuses to serve a non-experience database")
    root = _validated_notes_root(notes_dir)
    store = SQLiteCheckpointStore(path)
    service = SyntheticCognitiveLoop(store, _no_generation)

    def publish_note(run_id: str) -> object:
        return publish_kept_cognitive_note(store, run_id, root)

    def withdraw_note(
        run_id: str, fragment_id: str, expected_sequence: int, withdrawal_id: str
    ) -> CognitiveNoteWithdrawalOutcome:
        return withdraw_kept_cognitive_note(
            store,
            run_id,
            root,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        )

    projection = make_projection_server(str(path), port=projection_port)
    try:
        decisions = make_cognitive_decision_server(
            service,
            port=decision_port,
            note_publisher=publish_note,
            note_withdrawer=withdraw_note,
        )
    except Exception:
        projection.server_close()
        raise
    return projection, decisions


def selfcheck() -> dict[str, object]:
    """Deterministic end-to-end self-check inside one temporary directory.

    Covers the full local loop: prepare, restart zero-repeat, sanitized
    draft-view projection, bound keep with an explicit category, idempotent
    replay, idempotent note publishing, wrong-hash refusal, withdrawal that
    keeps the note file and original content, and the fail-closed state after
    withdrawal.  No server, no network, no real Vault.
    """
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="fragment-local-experience-") as directory:
        base = Path(directory).resolve()
        db_path = base / f"{DB_NAME_PREFIX}selfcheck.sqlite3"
        notes_dir = base / "vault"
        notes_dir.mkdir()
        summary = prepare_experience(db_path)
        checks["prepare_parked_for_decision"] = (
            summary["status"] == "paused"
            and summary["stop_reason"] == "awaiting_cognitive_decision"
        )
        call_counts = summary["call_counts"]
        assert isinstance(call_counts, dict)
        checks["research_ran_exactly_once"] = (
            call_counts.get("search_transport") == 1
            and call_counts.get("classifier") == 1
            and call_counts.get("analyst") == 1
            and call_counts.get("fetch_transport") == 2
        )
        restart_extra = summary["restart_extra_calls"]
        assert isinstance(restart_extra, dict)
        checks["restart_repeats_no_research_call"] = all(
            restart_extra[key] == 0 for key in restart_extra
        )
        run_id = str(summary["run_id"])
        store = SQLiteCheckpointStore(db_path)
        service = SyntheticCognitiveLoop(store, _no_generation)

        detail = compute_run_detail(str(db_path), run_id)
        view = detail.get("cognitive_draft_view") if isinstance(detail, dict) else None
        checks["draft_view_projected"] = isinstance(view, dict)
        if isinstance(view, dict):
            research = view.get("research")
            sources = (
                research.get("sources") if isinstance(research, Mapping) else None
            )
            claims = research.get("v1_claims") if isinstance(research, Mapping) else None
            first_source = sources[0] if isinstance(sources, list) and sources else None
            discovery = (
                first_source.get("discovery")
                if isinstance(first_source, Mapping)
                else None
            )
            checks["discovery_and_page_evidence_separate"] = (
                isinstance(discovery, Mapping)
                and isinstance(first_source, Mapping)
                and discovery.get("excerpt") != first_source.get("evidence_excerpt")
                and first_source.get("authenticity") == "unverified"
            )
            first_claim = claims[0] if isinstance(claims, list) and claims else None
            checks["claims_stay_not_covered"] = (
                isinstance(first_claim, Mapping)
                and first_claim.get("verdict") == "not_covered"
                and view.get("evidence_level") == "unverified"
            )
            checks["draft_view_has_no_internal_trace"] = "retrieval_trace" not in view
            perspectives = view.get("perspectives")
            by_name = (
                {
                    str(p.get("perspective")): p
                    for p in perspectives
                    if isinstance(p, Mapping)
                }
                if isinstance(perspectives, list)
                else {}
            )
            memory = by_name.get("memory")
            knowledge = by_name.get("knowledge_base")
            frontier = by_name.get("frontier")
            memory_materials = (
                memory.get("materials") if isinstance(memory, Mapping) else None
            )
            knowledge_materials = (
                knowledge.get("materials") if isinstance(knowledge, Mapping) else None
            )
            frontier_materials = (
                frontier.get("materials") if isinstance(frontier, Mapping) else None
            )
            material_types = (
                [m.get("material_type") for m in memory_materials]
                if isinstance(memory_materials, list)
                else []
            ) + (
                [m.get("material_type") for m in knowledge_materials]
                if isinstance(knowledge_materials, list)
                else []
            )
            checks["three_perspective_materials_projected"] = (
                sorted(str(t) for t in material_types)
                == ["confirmed_user_fact", "obsidian_record", "profile_inference"]
                and frontier_materials == []
            )
            all_materials = [
                m
                for group in (memory_materials, knowledge_materials)
                if isinstance(group, list)
                for m in group
                if isinstance(m, Mapping)
            ]
            texts = [str(m.get("text")) for m in all_materials]
            checks["perspective_materials_are_distinct"] = (
                len(all_materials) == 3 and len(set(texts)) == 3
            )
            inference = next(
                (
                    m
                    for m in all_materials
                    if m.get("material_type") == "profile_inference"
                ),
                None,
            )
            confirmed = next(
                (
                    m
                    for m in all_materials
                    if m.get("material_type") == "confirmed_user_fact"
                ),
                None,
            )
            record = next(
                (m for m in all_materials if m.get("material_type") == "obsidian_record"),
                None,
            )
            checks["material_views_carry_their_own_refs"] = (
                isinstance(inference, Mapping)
                and isinstance(inference.get("basis"), str)
                and isinstance(inference.get("uncertainty"), str)
                and isinstance(confirmed, Mapping)
                and isinstance(confirmed.get("confirmation_ref"), str)
                and isinstance(record, Mapping)
                and isinstance(record.get("record_ref"), str)
            )

        found = store.latest_with_sequence(run_id)
        assert found is not None
        parked, parked_sequence = found
        markdown = parked.eval_results.get("cognitive_markdown")
        assert isinstance(markdown, str)
        markdown_sha256 = hashlib.sha256(markdown.encode()).hexdigest()
        decision_body = {
            "requester": "nigo",
            "decision": "keep_draft",
            "run_id": run_id,
            "fragment_id": EXPERIENCE_FRAGMENT_ID,
            "markdown_sha256": markdown_sha256,
            "expected_sequence": parked_sequence,
            "thought_category": SELFCHECK_THOUGHT_CATEGORY,
        }
        decision_id = decision_idempotency_key(decision_body)
        kept = service.decide_bound(
            run_id,
            "keep_draft",
            fragment_id=EXPERIENCE_FRAGMENT_ID,
            expected_sequence=parked_sequence,
            markdown_sha256=markdown_sha256,
            decision_id=decision_id,
            thought_category=SELFCHECK_THOUGHT_CATEGORY,
        )
        checks["keep_records_draft_unverified"] = (
            kept.status == "passed"
            and kept.eval_results.get("content_lifecycle") == "draft"
            and kept.eval_results.get("evidence_level") == "unverified"
        )
        outcome = publish_kept_cognitive_note(store, run_id, notes_dir)
        note_path = Path(outcome.path)
        note_text = note_path.read_text(encoding="utf-8")
        checks["kept_note_written"] = (
            note_path.is_file()
            and 'type: "碎片认知结果"' in note_text
            and 'content_lifecycle: "draft"' in note_text
            and f'thought_category: "{SELFCHECK_THOUGHT_CATEGORY}"' in note_text
        )
        replayed = service.decide_bound(
            run_id,
            "keep_draft",
            fragment_id=EXPERIENCE_FRAGMENT_ID,
            expected_sequence=parked_sequence,
            markdown_sha256=markdown_sha256,
            decision_id=decision_id,
            thought_category=SELFCHECK_THOUGHT_CATEGORY,
        )
        checks["duplicate_decision_is_idempotent"] = (
            replayed.eval_results.get("cognitive_decision_receipt")
            == kept.eval_results.get("cognitive_decision_receipt")
        )
        republication = publish_kept_cognitive_note(store, run_id, notes_dir)
        checks["note_publish_is_idempotent"] = republication.already_published
        try:
            service.decide_bound(
                run_id,
                "keep_draft",
                fragment_id=EXPERIENCE_FRAGMENT_ID,
                expected_sequence=parked_sequence,
                markdown_sha256=hashlib.sha256(b"drifted").hexdigest(),
                decision_id=hashlib.sha256(b"other").hexdigest(),
                thought_category=SELFCHECK_THOUGHT_CATEGORY,
            )
            checks["wrong_hash_decision_refused"] = False
        except CognitiveDecisionError:
            checks["wrong_hash_decision_refused"] = True

        found = store.latest_with_sequence(run_id)
        assert found is not None
        kept_checkpoint, kept_sequence = found
        withdrawal_body = {
            "requester": "nigo",
            "action": "withdraw",
            "run_id": run_id,
            "fragment_id": EXPERIENCE_FRAGMENT_ID,
            "expected_sequence": kept_sequence,
        }
        withdrawal = withdraw_kept_cognitive_note(
            store,
            run_id,
            notes_dir,
            fragment_id=EXPERIENCE_FRAGMENT_ID,
            expected_sequence=kept_sequence,
            withdrawal_id=withdrawal_idempotency_key(withdrawal_body),
        )
        withdrawn_text = note_path.read_text(encoding="utf-8")
        checks["withdrawal_marks_note_withdrawn"] = (
            withdrawal.status == "withdrawn"
            and 'content_lifecycle: "withdrawn"' in withdrawn_text
        )
        checks["withdrawal_keeps_note_and_body"] = (
            note_path.is_file()
            and str(kept_checkpoint.eval_results.get("cognitive_markdown"))
            in withdrawn_text
        )
        try:
            service.decide_bound(
                run_id,
                "keep_draft",
                fragment_id=EXPERIENCE_FRAGMENT_ID,
                expected_sequence=kept_sequence,
                markdown_sha256=markdown_sha256,
                decision_id=decision_id,
                thought_category=SELFCHECK_THOUGHT_CATEGORY,
            )
            checks["decision_after_withdrawal_refused"] = False
        except CognitiveDecisionError:
            checks["decision_after_withdrawal_refused"] = True
    checks["live_entry_blocked"] = False
    try:
        live()
    except LocalExperienceLiveBlockedError:
        checks["live_entry_blocked"] = True
    return {
        "status": "selfcheck_ok" if all(checks.values()) else "selfcheck_failed",
        "fragment_id": EXPERIENCE_FRAGMENT_ID,
        "run_id": EXPERIENCE_RUN_ID,
        "checks": checks,
    }


class LocalExperienceLiveBlockedError(RuntimeError):
    """The live entry is hard-blocked before any real-world side effect."""


def live() -> None:
    """Hard-block before any network, credential, file, or server side effect."""
    raise LocalExperienceLiveBlockedError(LIVE_BLOCKED_ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragment_loop.local_experience",
        description="Loop V1 Package B offline local experience",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--selfcheck",
        action="store_true",
        help="run the deterministic end-to-end self-check and exit",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="hard-blocked before any real-world side effect",
    )
    parser.add_argument(
        "--db",
        help=f"dedicated {DB_NAME_PREFIX}*.sqlite3 path (default: a temp file)",
    )
    parser.add_argument(
        "--notes-dir",
        help="local notes directory for kept drafts (default: a temp vault)",
    )
    parser.add_argument(
        "--projection-port",
        type=int,
        default=0,
        help="loopback projection port (default 0 = random; never production 5679 "
        "unless explicitly injected)",
    )
    parser.add_argument(
        "--decision-port",
        type=int,
        default=0,
        help="loopback decision port (default 0 = random; never production 5684 "
        "unless explicitly injected)",
    )
    args = parser.parse_args(argv)

    if args.live:
        # Hard block: no store, no file, no server, no credential, no network.
        try:
            live()
        except LocalExperienceLiveBlockedError as error:
            print(
                json.dumps(
                    {"status": "blocked", "error_category": str(error)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
    if args.selfcheck:
        summary = selfcheck()
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if summary["status"] == "selfcheck_ok" else 1

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.db:
        db_path: str | Path = args.db
    else:
        temporary = tempfile.TemporaryDirectory(prefix="fragment-local-experience-")
        # macOS temp roots live behind the /var symlink; normalize to the real
        # path first so the fail-closed symlink check never rejects the
        # package's own temporary workspace.
        db_path = (
            Path(temporary.name).resolve() / f"{DB_NAME_PREFIX}local.sqlite3"
        )
    notes_dir = args.notes_dir or str(Path(db_path).parent / "vault")
    try:
        prepared = prepare_experience(db_path)
        projection, decisions = make_experience_servers(
            db_path,
            notes_dir,
            projection_port=args.projection_port,
            decision_port=args.decision_port,
        )
    except (LocalExperienceError, OSError, sqlite3.Error) as error:
        print(
            json.dumps(
                {"status": "refused", "error_category": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if temporary is not None:
            temporary.cleanup()
        return 1
    projection_port = int(projection.server_address[1])
    decision_port = int(decisions.server_address[1])
    projection_thread = threading.Thread(target=projection.serve_forever, daemon=True)
    projection_thread.start()
    print(
        json.dumps(
            {
                **prepared,
                "notes_dir": str(Path(notes_dir).resolve()),
                "projection_url": f"http://127.0.0.1:{projection_port}/loop/v1",
                "decision_url": (
                    f"http://127.0.0.1:{decision_port}/fragment-cognitive/v1"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        decisions.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        decisions.server_close()
        projection.shutdown()
        projection.server_close()
        projection_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
