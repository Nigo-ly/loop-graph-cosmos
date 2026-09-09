"""Publish a user-kept cognitive draft as one local Obsidian-compatible note.

The thought category is never accepted from the caller: it can only be
recovered from the binding decision receipt atomically persisted in the final
checkpoint. Only receipts revalidated against the exact allowlisted
``FRAGMENT_COGNITIVE_SYNTHETIC_R1B`` / ``FRAGMENT_COGNITIVE_LOCAL_R1N``
loop/version pairs are publishable, and the final checkpoint must also prove
the append-only causal chain of the real atomic ``human_decision`` commit
(event identity, revision reason, and the store-computed supersede edge onto
the displayed paused draft). Legacy receipts without a category, plain
``decide()`` results, forged or drifted receipts, and later snapshot appends
masquerading as the terminal state are all refused without any file write.

R1-OB adds the bound withdrawal of such a kept draft. A withdrawal first
appends a ``withdrawal_pending`` receipt through the same checkpoint CAS,
then rewrites only the deterministic note file (user-edited Markdown body
preserved verbatim; the sole frontmatter changes are
``content_lifecycle: draft -> withdrawn`` plus a timezone-aware
``withdrawn_at``), and finally appends the ``withdrawn`` terminal receipt.
The file update goes through a same-directory temporary file, flush, fsync,
and an atomic replace guarded by a fresh lstat/content identity check; any
observable change, protected-field drift, symlink, non-regular file, or
structural damage aborts without overwriting. Replay with the same
withdrawal binding reconciles the pending or withdrawn state; any other ID,
sequence, fragment, or forged receipt is refused with zero file writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore, checkpoint_compat_view
from common.execution import translate_view_edit
from fragment_loop.cognitive_loop import (
    CognitiveDecisionError,
    LocalCognitiveLoop,
    SyntheticCognitiveLoop,
    normalize_thought_category,
)
from fragment_loop.spec import (
    FRAGMENT_COGNITIVE_LOCAL_R1N,
    FRAGMENT_COGNITIVE_SYNTHETIC_R1B,
)


class CognitiveNotePublishError(ValueError):
    """The kept draft or publication target is not safe to publish."""


class CognitiveNoteWithdrawalError(ValueError):
    """The kept draft, withdrawal binding, or note file is not withdrawable."""


@dataclass(frozen=True)
class CognitiveNotePublishOutcome:
    path: str
    content_sha256: str
    already_published: bool


@dataclass(frozen=True)
class CognitiveNoteWithdrawalOutcome:
    """Terminal withdrawal result; never carries note body or private paths."""

    withdrawal_id: str
    status: str
    note_status: str
    withdrawn_at: str | None
    note_sha256: str | None
    already_withdrawn: bool


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _note_unknowns(result: Mapping[str, object]) -> list[str]:
    unknowns = []
    semantic = result.get("semantic_expansion")
    synthesis = result.get("synthesis")
    if isinstance(semantic, Mapping):
        unknowns.extend(_strings(semantic.get("uncertainties")))
    if isinstance(synthesis, Mapping):
        unknowns.extend(_strings(synthesis.get("open_questions")))
    return list(dict.fromkeys(unknowns))


def _note_title(result: Mapping[str, object]) -> str:
    summary = result.get("summary")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("text"), str):
        raise CognitiveNotePublishError("认知草稿标题无效")
    title = str(summary["text"]).strip()
    if not title:
        raise CognitiveNotePublishError("认知草稿标题无效")
    return title


def _draft_note_fields(
    checkpoint: LoopCheckpoint,
    thought_category: str,
    *,
    thought_updated_at: str | None = None,
) -> tuple[tuple[str, object], ...]:
    """The exact ordered protected frontmatter of a draft note.

    ``thought_updated_at`` defaults to the checkpoint timestamp; the
    withdrawal path pins it to the value the pending receipt bound when the
    kept checkpoint was still the latest one.
    """
    values = checkpoint.eval_results
    result = values.get("cognitive_result")
    if not isinstance(result, Mapping):
        raise CognitiveNotePublishError("认知草稿内容无效")
    unknowns = _note_unknowns(result)
    return (
        ("type", "碎片认知结果"),
        ("title", _note_title(result)),
        ("thought_category", thought_category),
        ("thought_status", "provisional" if unknowns else "concluded"),
        ("thought_unknowns", unknowns),
        (
            "thought_updated_at",
            checkpoint.updated_at if thought_updated_at is None else thought_updated_at,
        ),
        ("source_fragment", checkpoint.fragment_id),
        ("fragment_id", checkpoint.fragment_id),
        ("run_id", checkpoint.run_id),
        ("content_lifecycle", "draft"),
        ("evidence_level", "unverified"),
    )


def _render_note(fields: tuple[tuple[str, object], ...], body: str) -> bytes:
    header = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in fields
    )
    return f"---\n{header}\n---\n\n{body}".encode()


def _note_content(checkpoint: LoopCheckpoint, thought_category: str) -> bytes:
    values = checkpoint.eval_results
    markdown = values.get("cognitive_markdown")
    if not isinstance(markdown, str):
        raise CognitiveNotePublishError("认知草稿内容无效")
    preserved_markdown = markdown if markdown.endswith("\n") else markdown + "\n"
    return _render_note(
        _draft_note_fields(checkpoint, thought_category), preserved_markdown
    )


def _receipt_thought_category(checkpoint: LoopCheckpoint) -> str:
    """Recover the normalized category from the revalidated binding receipt."""
    values = checkpoint.eval_results
    receipt = values.get("cognitive_decision_receipt")
    markdown = values.get("cognitive_markdown")
    decision_id = receipt.get("decision_id") if isinstance(receipt, Mapping) else None
    expected_sequence = (
        receipt.get("expected_sequence") if isinstance(receipt, Mapping) else None
    )
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("decision") != "keep_draft"
        or receipt.get("fragment_id") != checkpoint.fragment_id
        or not isinstance(markdown, str)
        or receipt.get("markdown_sha256")
        != hashlib.sha256(markdown.encode()).hexdigest()
        or not isinstance(decision_id, str)
        or not decision_id.strip()
        or len(decision_id) > 256
        or not isinstance(expected_sequence, int)
        or isinstance(expected_sequence, bool)
        or expected_sequence < 1
    ):
        raise CognitiveNotePublishError("认知草稿缺少可发布的绑定分类回执")
    try:
        category = normalize_thought_category(receipt.get("thought_category"))
    except CognitiveDecisionError as error:
        raise CognitiveNotePublishError("认知草稿缺少可发布的绑定分类回执") from error
    if receipt.get("thought_category") != category:
        raise CognitiveNotePublishError("认知草稿缺少可发布的绑定分类回执")
    return category


def _committed_decision_chain_is_intact_in(
    history: list[LoopCheckpoint], checkpoint: LoopCheckpoint
) -> bool:
    """Verify the receipt comes from the atomic ``human_decision`` CAS commit.

    A genuine keep commit is appended by ``compare_and_append`` with
    ``event_type="human_decision"`` and ``revision_reason="keep_draft"``, and
    the store itself recomputes ``supersedes_sequence`` at insert time, so a
    later ``save()`` append can neither reuse the event identity nor fake the
    supersede edge. The receipt is only trustworthy when the final checkpoint
    carries that event identity, its supersede edge points exactly at the
    receipt's ``expected_sequence``, and the checkpoint it supersedes is the
    displayed paused draft whose Markdown matches the receipt digest.
    """
    receipt = checkpoint.eval_results.get("cognitive_decision_receipt")
    if (
        not isinstance(receipt, Mapping)
        or checkpoint.event_type != "human_decision"
        or checkpoint.revision_reason != "keep_draft"
        or checkpoint.supersedes_sequence is None
        or checkpoint.supersedes_sequence != receipt.get("expected_sequence")
    ):
        return False
    if len(history) < 2 or history[-1] != checkpoint:
        return False
    displayed = history[-2]
    displayed_markdown = displayed.eval_results.get("cognitive_markdown")
    return (
        displayed.status == "paused"
        and displayed.stop_reason == "awaiting_cognitive_decision"
        and displayed.fragment_id == checkpoint.fragment_id
        and displayed.run_id == checkpoint.run_id
        and isinstance(displayed_markdown, str)
        and hashlib.sha256(displayed_markdown.encode()).hexdigest()
        == receipt.get("markdown_sha256")
    )


def _committed_decision_chain_is_intact(
    store: SQLiteCheckpointStore, checkpoint: LoopCheckpoint
) -> bool:
    return _committed_decision_chain_is_intact_in(
        store.history(checkpoint.run_id), checkpoint
    )


_PUBLISHABLE_REVALIDATORS: dict[tuple[str, str], Callable[[LoopCheckpoint], bool]] = {
    (
        FRAGMENT_COGNITIVE_SYNTHETIC_R1B.loop_id,
        FRAGMENT_COGNITIVE_SYNTHETIC_R1B.version,
    ): SyntheticCognitiveLoop._decision_material_is_valid,
    (
        FRAGMENT_COGNITIVE_LOCAL_R1N.loop_id,
        FRAGMENT_COGNITIVE_LOCAL_R1N.version,
    ): LocalCognitiveLoop._decision_material_is_valid,
}


def _kept_checkpoint_state_is_valid(checkpoint: LoopCheckpoint) -> bool:
    """Pure revalidation of a kept ``draft + unverified`` terminal state."""
    revalidator = _PUBLISHABLE_REVALIDATORS.get(
        (checkpoint.loop_id, checkpoint.loopspec_version)
    )
    values = checkpoint.eval_results
    return (
        revalidator is not None
        and checkpoint.status == "passed"
        and checkpoint.stop_reason == "cognitive_draft_kept"
        and values.get("cognitive_decision") == "keep_draft"
        and values.get("content_lifecycle") == "draft"
        and values.get("evidence_level") == "unverified"
        and revalidator(checkpoint)
    )


def _validated_kept_checkpoint(
    store: SQLiteCheckpointStore, run_id: str
) -> tuple[LoopCheckpoint, str]:
    checkpoint = store.latest(run_id)
    if checkpoint is None:
        raise CognitiveNotePublishError("认知运行不存在")
    if not _kept_checkpoint_state_is_valid(checkpoint):
        raise CognitiveNotePublishError("只有重新校验通过的已保留认知草稿才能发布笔记")
    if not _committed_decision_chain_is_intact(store, checkpoint):
        raise CognitiveNotePublishError("认知草稿缺少可发布的绑定分类回执")
    return checkpoint, _receipt_thought_category(checkpoint)


def _validated_notes_dir(notes_dir: str | Path) -> Path:
    requested = Path(notes_dir).expanduser()
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise CognitiveNotePublishError("笔记目录不存在") from error
    if not resolved.is_dir() or resolved != Path(os.path.abspath(requested)):
        raise CognitiveNotePublishError("笔记目录必须是真实目录，不能经过符号链接")
    return resolved


def publish_kept_cognitive_note(
    store: SQLiteCheckpointStore,
    run_id: str,
    notes_dir: str | Path,
) -> CognitiveNotePublishOutcome:
    """Idempotently publish one kept draft without upgrading its evidence level.

    The thought category is recovered only from the binding decision receipt
    in the final checkpoint; callers can no longer supply one.
    """
    checkpoint, category = _validated_kept_checkpoint(store, run_id)
    root = _validated_notes_dir(notes_dir)
    digest = hashlib.sha256(checkpoint.fragment_id.encode()).hexdigest()[:20]
    destination = root / f"cognitive-{digest}.md"
    content = _note_content(checkpoint, category)
    content_sha256 = hashlib.sha256(content).hexdigest()

    try:
        destination_stat = destination.lstat()
    except FileNotFoundError:
        destination_stat = None
    if destination_stat is not None:
        if not stat.S_ISREG(destination_stat.st_mode) or destination.is_symlink():
            raise CognitiveNotePublishError("目标笔记不是普通文件")
        if destination.read_bytes() != content:
            raise CognitiveNotePublishError("目标笔记已存在且内容冲突")
        return CognitiveNotePublishOutcome(
            path=str(destination),
            content_sha256=content_sha256,
            already_published=True,
        )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=root, prefix=".cognitive-note-", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            if destination.is_symlink() or not destination.is_file():
                raise CognitiveNotePublishError("目标笔记不是普通文件") from None
            if destination.read_bytes() != content:
                raise CognitiveNotePublishError("目标笔记已存在且内容冲突") from None
            already_published = True
        else:
            already_published = False
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return CognitiveNotePublishOutcome(
        path=str(destination),
        content_sha256=content_sha256,
        already_published=already_published,
    )


# ---------------------------------------------------------------------------
# R1-OB: bound, recoverable withdrawal of a kept local cognitive draft note.
# ---------------------------------------------------------------------------

MAX_WITHDRAWAL_CAS_CONFLICTS = 8
WITHDRAWAL_PENDING_STOP_REASON = "cognitive_withdrawal_pending"
WITHDRAWN_STOP_REASON = "cognitive_note_withdrawn"
_PENDING_RECEIPT_KEY = "cognitive_withdrawal_pending_receipt"
_TERMINAL_RECEIPT_KEY = "cognitive_withdrawal_receipt"
_HEX = frozenset("0123456789abcdef")


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _validate_withdrawal_binding(
    fragment_id: object, expected_sequence: object, withdrawal_id: object
) -> None:
    if (
        not isinstance(fragment_id, str)
        or not fragment_id.strip()
        or len(fragment_id) > 256
        or not isinstance(expected_sequence, int)
        or isinstance(expected_sequence, bool)
        or expected_sequence < 1
        or not _is_sha256(withdrawal_id)
    ):
        raise CognitiveNoteWithdrawalError("撤回请求绑定无效")


def _receipt_matches_withdrawal_binding(
    receipt: object,
    checkpoint: LoopCheckpoint,
    *,
    fragment_id: str,
    expected_sequence: int,
    withdrawal_id: str,
) -> bool:
    return (
        isinstance(receipt, Mapping)
        and receipt.get("action") == "withdraw"
        and receipt.get("withdrawal_id") == withdrawal_id
        and receipt.get("fragment_id") == fragment_id
        and receipt.get("expected_sequence") == expected_sequence
        and checkpoint.fragment_id == fragment_id
    )


def _validate_pending_checkpoint(
    store: SQLiteCheckpointStore,
    checkpoint: LoopCheckpoint,
    receipt: object,
    *,
    fragment_id: str,
    expected_sequence: int,
    withdrawal_id: str,
) -> None:
    """Re-validate a persisted pending receipt and its kept causal chain."""
    if (
        not _receipt_matches_withdrawal_binding(
            receipt,
            checkpoint,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        )
        or not isinstance(receipt, Mapping)
        or not isinstance(receipt.get("thought_updated_at"), str)
        or checkpoint.status != "passed"
        or checkpoint.stop_reason != WITHDRAWAL_PENDING_STOP_REASON
        or checkpoint.event_type != "withdrawal_pending"
        or checkpoint.revision_reason != "withdraw"
        or checkpoint.supersedes_sequence != expected_sequence
        or _TERMINAL_RECEIPT_KEY in checkpoint.eval_results
    ):
        raise CognitiveNoteWithdrawalError("撤回 pending 回执与请求绑定冲突")
    history = store.history(checkpoint.run_id)
    if len(history) < 2 or history[-1] != checkpoint:
        raise CognitiveNoteWithdrawalError("撤回 pending 回执与请求绑定冲突")
    kept = history[-2]
    if not _kept_checkpoint_state_is_valid(
        kept
    ) or not _committed_decision_chain_is_intact_in(history[:-1], kept):
        raise CognitiveNoteWithdrawalError("撤回 pending 回执与请求绑定冲突")


def _terminal_chain_is_intact(
    store: SQLiteCheckpointStore,
    checkpoint: LoopCheckpoint,
    *,
    fragment_id: str,
    expected_sequence: int,
    withdrawal_id: str,
) -> bool:
    """Verify the terminal CAS-appended directly on the genuine pending.

    The store recomputes ``supersedes_sequence`` at insert time, so a
    ``save()`` snapshot clone or a forged ``withdrawal_completed`` append on
    top of the real terminal can neither point its supersede edge at the
    genuine pending nor sit between the pending and this terminal without
    moving that edge. The pending row itself must carry the exact event
    identity and the same binding receipt the terminal persisted.
    """
    if checkpoint.supersedes_sequence is None:
        return False
    history = store.history(checkpoint.run_id)
    if len(history) < 3 or history[-1] != checkpoint:
        return False
    pending = history[-2]
    pending_receipt = pending.eval_results.get(_PENDING_RECEIPT_KEY)
    if (
        pending.status != "passed"
        or pending.stop_reason != WITHDRAWAL_PENDING_STOP_REASON
        or pending.event_type != "withdrawal_pending"
        or pending.revision_reason != "withdraw"
        or pending.supersedes_sequence != expected_sequence
        or pending_receipt != checkpoint.eval_results.get(_PENDING_RECEIPT_KEY)
        or not _receipt_matches_withdrawal_binding(
            pending_receipt,
            pending,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        )
    ):
        return False
    events = store.events_after(checkpoint.run_id, expected_sequence)
    return (
        len(events) >= 1
        and events[0][1] == "withdrawal_pending"
        and events[0][0] == checkpoint.supersedes_sequence
    )


def _terminal_withdrawal_outcome(
    store: SQLiteCheckpointStore,
    checkpoint: LoopCheckpoint,
    receipt: object,
    *,
    fragment_id: str,
    expected_sequence: int,
    withdrawal_id: str,
) -> CognitiveNoteWithdrawalOutcome:
    """Return the stored terminal state for an exact same-binding replay."""
    values = checkpoint.eval_results
    if (
        not _receipt_matches_withdrawal_binding(
            receipt,
            checkpoint,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        )
        or not isinstance(receipt, Mapping)
        or not _receipt_matches_withdrawal_binding(
            values.get(_PENDING_RECEIPT_KEY),
            checkpoint,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        )
        or checkpoint.status != "passed"
        or checkpoint.stop_reason != WITHDRAWN_STOP_REASON
        or checkpoint.event_type != "withdrawal_completed"
        or checkpoint.revision_reason != "withdraw"
        or values.get("content_lifecycle") != "withdrawn"
        or values.get("evidence_level") != "unverified"
        or not _terminal_chain_is_intact(
            store,
            checkpoint,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        )
    ):
        raise CognitiveNoteWithdrawalError("撤回终态与请求绑定冲突")
    note_status = receipt.get("note_status")
    withdrawn_at = receipt.get("withdrawn_at")
    note_sha256 = receipt.get("note_sha256")
    if note_status == "absent":
        if withdrawn_at is not None or note_sha256 is not None:
            raise CognitiveNoteWithdrawalError("撤回终态与请求绑定冲突")
    elif note_status == "withdrawn":
        if not _valid_withdrawn_at(withdrawn_at) or not _is_sha256(note_sha256):
            raise CognitiveNoteWithdrawalError("撤回终态与请求绑定冲突")
    else:
        raise CognitiveNoteWithdrawalError("撤回终态与请求绑定冲突")
    return CognitiveNoteWithdrawalOutcome(
        withdrawal_id=withdrawal_id,
        status="withdrawn",
        note_status=str(note_status),
        withdrawn_at=None if withdrawn_at is None else str(withdrawn_at),
        note_sha256=None if note_sha256 is None else str(note_sha256),
        already_withdrawn=True,
    )


def _valid_withdrawn_at(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _parse_note(raw: bytes) -> tuple[list[tuple[str, object]], str]:
    """Split a note into ordered frontmatter fields and the verbatim body."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CognitiveNoteWithdrawalError("笔记 frontmatter 结构损坏") from error
    closing = text.find("\n---\n", 4) if text.startswith("---\n") else -1
    if closing == -1:
        raise CognitiveNoteWithdrawalError("笔记 frontmatter 结构损坏")
    remainder = text[closing + len("\n---\n"):]
    if not remainder.startswith("\n"):
        raise CognitiveNoteWithdrawalError("笔记 frontmatter 结构损坏")
    fields: list[tuple[str, object]] = []
    for line in text[4:closing].split("\n"):
        key, separator, raw_value = line.partition(": ")
        if not separator or not key.strip():
            raise CognitiveNoteWithdrawalError("笔记 frontmatter 结构损坏")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise CognitiveNoteWithdrawalError("笔记 frontmatter 结构损坏") from error
        fields.append((key, value))
    keys = [key for key, _ in fields]
    if len(set(keys)) != len(keys):
        raise CognitiveNoteWithdrawalError("笔记 frontmatter 结构损坏")
    return fields, remainder[1:]


def _fields_match(
    parsed: list[tuple[str, object]], expected: tuple[tuple[str, object], ...]
) -> bool:
    return len(parsed) == len(expected) and all(
        parsed_key == expected_key and parsed_value == expected_value
        for (parsed_key, parsed_value), (expected_key, expected_value) in zip(
            parsed, expected
        )
    )


def _withdrawn_fields(
    draft_fields: tuple[tuple[str, object], ...], withdrawn_at: object
) -> tuple[tuple[str, object], ...]:
    """The only allowed drift: lifecycle flip plus one new ``withdrawn_at``."""
    fields: list[tuple[str, object]] = []
    for key, value in draft_fields:
        fields.append((key, "withdrawn" if key == "content_lifecycle" else value))
        if key == "content_lifecycle":
            fields.append(("withdrawn_at", withdrawn_at))
    return tuple(fields)


def _write_temporary_note(root: Path, content: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=root, prefix=".cognitive-note-withdrawal-", delete=False
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        return Path(temporary.name)


def _atomic_replace_note(
    destination: Path,
    root: Path,
    content: bytes,
    observed: os.stat_result,
    original: bytes,
) -> None:
    """Atomically replace the note, re-checking its identity right before.

    The final lstat/content check and ``os.replace`` are still two separate
    syscalls, so this does not claim to eliminate every external race; it
    only guarantees that any change observable at the last check aborts the
    replace without overwriting.
    """
    temporary_path = _write_temporary_note(root, content)
    try:
        try:
            current = destination.lstat()
        except FileNotFoundError as error:
            raise CognitiveNoteWithdrawalError(
                "笔记在撤回过程中被外部修改"
            ) from error
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != observed.st_dev
            or current.st_ino != observed.st_ino
            or current.st_size != observed.st_size
            or current.st_mtime_ns != observed.st_mtime_ns
            or hashlib.sha256(destination.read_bytes()).digest()
            != hashlib.sha256(original).digest()
        ):
            raise CognitiveNoteWithdrawalError("笔记在撤回过程中被外部修改")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _apply_withdrawal_to_note(
    root: Path, checkpoint: LoopCheckpoint, thought_category: str
) -> tuple[str, str | None, str | None]:
    """Withdraw the deterministic note file, or honestly report it absent.

    Returns ``(note_status, withdrawn_at, note_sha256)``. A draft note keeps
    its user-edited body verbatim; a note already carrying the withdrawn
    frontmatter is re-validated and reused as-is (recovery after an
    interruption between the file write and the terminal commit).
    """
    receipt = checkpoint.eval_results.get(_PENDING_RECEIPT_KEY)
    if not isinstance(receipt, Mapping) or not isinstance(
        receipt.get("thought_updated_at"), str
    ):
        raise CognitiveNoteWithdrawalError("撤回 pending 回执与请求绑定冲突")
    digest = hashlib.sha256(checkpoint.fragment_id.encode()).hexdigest()[:20]
    destination = root / f"cognitive-{digest}.md"
    try:
        observed = destination.lstat()
    except FileNotFoundError:
        return "absent", None, None
    if not stat.S_ISREG(observed.st_mode):
        raise CognitiveNoteWithdrawalError("目标笔记不是普通文件")
    original = destination.read_bytes()
    parsed_fields, body = _parse_note(original)
    draft_fields = _draft_note_fields(
        checkpoint,
        thought_category,
        thought_updated_at=str(receipt["thought_updated_at"]),
    )
    if _fields_match(parsed_fields, draft_fields):
        withdrawn_at = _iso_now()
        content = _render_note(_withdrawn_fields(draft_fields, withdrawn_at), body)
        _atomic_replace_note(destination, root, content, observed, original)
        return "withdrawn", withdrawn_at, hashlib.sha256(content).hexdigest()
    recovered_at: object = None
    for key, value in parsed_fields:
        if key == "withdrawn_at":
            recovered_at = value
    if _valid_withdrawn_at(recovered_at) and _fields_match(
        parsed_fields, _withdrawn_fields(draft_fields, recovered_at)
    ):
        return (
            "withdrawn",
            str(recovered_at),
            hashlib.sha256(original).hexdigest(),
        )
    raise CognitiveNoteWithdrawalError(
        "笔记受保护字段与 Checkpoint 或撤回回执不一致"
    )


def _final_note_consistency_check(
    root: Path, fragment_id: str, note_status: str, note_sha256: str | None
) -> None:
    """Last read-only check between the note observation and the terminal CAS.

    An ``absent`` verdict requires the deterministic note path to still be
    missing; a ``withdrawn`` verdict requires it to still be a non-symlink
    regular file whose current bytes hash to the SHA-256 about to be
    recorded. Any observable change retains the pending state and exits with
    a fixed conflict so the same request can reconcile later. The window
    between this final check and the CAS insert remains a documented race;
    this does not claim to eliminate it.
    """
    digest = hashlib.sha256(fragment_id.encode()).hexdigest()[:20]
    destination = root / f"cognitive-{digest}.md"
    try:
        current = destination.lstat()
    except FileNotFoundError:
        if note_status != "absent":
            raise CognitiveNoteWithdrawalError("笔记在终态提交前被外部修改")
        return
    if (
        note_status != "withdrawn"
        or not stat.S_ISREG(current.st_mode)
        or note_sha256 is None
        or hashlib.sha256(destination.read_bytes()).hexdigest() != note_sha256
    ):
        raise CognitiveNoteWithdrawalError("笔记在终态提交前被外部修改")


def withdraw_kept_cognitive_note(
    store: SQLiteCheckpointStore,
    run_id: str,
    notes_dir: str | Path,
    *,
    fragment_id: str,
    expected_sequence: int,
    withdrawal_id: str,
) -> CognitiveNoteWithdrawalOutcome:
    """Withdraw one kept draft note through the pending/file/terminal chain.

    The caller supplies only the store, the run, a verified notes root, and
    the fixed binding (fragment, displayed sequence, recomputed withdrawal
    ID) — never a file name, note body, category, or lifecycle override.
    Replays with the same binding reconcile an interrupted pending state or
    return the stored terminal state; any other binding is refused with zero
    file writes.
    """
    _validate_withdrawal_binding(fragment_id, expected_sequence, withdrawal_id)
    pending_checkpoint: LoopCheckpoint | None = None
    for _ in range(MAX_WITHDRAWAL_CAS_CONFLICTS):
        found = store.latest_raw_with_sequence(run_id)
        if found is None:
            raise KeyError(f"Unknown run: {run_id}")
        raw_checkpoint, sequence = found
        # 读走 legacy_flat_v1 投影；写回基于 raw 形态做视图增量翻译。
        checkpoint = checkpoint_compat_view(raw_checkpoint)
        values = checkpoint.eval_results
        terminal = values.get(_TERMINAL_RECEIPT_KEY)
        if terminal is not None:
            return _terminal_withdrawal_outcome(
                store,
                checkpoint,
                terminal,
                fragment_id=fragment_id,
                expected_sequence=expected_sequence,
                withdrawal_id=withdrawal_id,
            )
        pending = values.get(_PENDING_RECEIPT_KEY)
        if pending is not None:
            _validate_pending_checkpoint(
                store,
                checkpoint,
                pending,
                fragment_id=fragment_id,
                expected_sequence=expected_sequence,
                withdrawal_id=withdrawal_id,
            )
            pending_checkpoint = checkpoint
            break
        if not _kept_checkpoint_state_is_valid(
            checkpoint
        ) or not _committed_decision_chain_is_intact(store, checkpoint):
            raise CognitiveNoteWithdrawalError(
                "只有重新校验通过的已保留认知草稿才能撤回"
            )
        if checkpoint.fragment_id != fragment_id or sequence != expected_sequence:
            raise CognitiveNoteWithdrawalError("撤回绑定与最新 Checkpoint 不一致")
        try:
            _receipt_thought_category(checkpoint)
        except CognitiveNotePublishError as error:
            raise CognitiveNoteWithdrawalError(
                "认知草稿缺少绑定分类回执，不能撤回"
            ) from error
        pending_receipt = {
            "action": "withdraw",
            "withdrawal_id": withdrawal_id,
            "fragment_id": fragment_id,
            "expected_sequence": expected_sequence,
            "thought_updated_at": checkpoint.updated_at,
        }
        committed = store.compare_and_append(
            replace(
                raw_checkpoint,
                stop_reason=WITHDRAWAL_PENDING_STOP_REASON,
                eval_results=translate_view_edit(
                    existing=raw_checkpoint.eval_results,
                    edited_flat={**values, _PENDING_RECEIPT_KEY: pending_receipt},
                ),
            ),
            expected_sequence=sequence,
            event_type="withdrawal_pending",
            revision_reason="withdraw",
        )
        if committed is not None:
            pending_checkpoint = committed
            break
    if pending_checkpoint is None:
        raise RuntimeError("Repeated checkpoint conflicts during withdrawal")

    try:
        category = _receipt_thought_category(pending_checkpoint)
    except CognitiveNotePublishError as error:
        raise CognitiveNoteWithdrawalError(
            "认知草稿缺少绑定分类回执，不能撤回"
        ) from error
    root = _validated_notes_dir(notes_dir)
    note_status, withdrawn_at, note_sha256 = _apply_withdrawal_to_note(
        root, pending_checkpoint, category
    )

    for _ in range(MAX_WITHDRAWAL_CAS_CONFLICTS):
        found = store.latest_raw_with_sequence(run_id)
        if found is None:
            raise KeyError(f"Unknown run: {run_id}")
        raw_checkpoint, sequence = found
        # 读走 legacy_flat_v1 投影；写回基于 raw 形态做视图增量翻译。
        checkpoint = checkpoint_compat_view(raw_checkpoint)
        values = checkpoint.eval_results
        terminal = values.get(_TERMINAL_RECEIPT_KEY)
        if terminal is not None:
            return _terminal_withdrawal_outcome(
                store,
                checkpoint,
                terminal,
                fragment_id=fragment_id,
                expected_sequence=expected_sequence,
                withdrawal_id=withdrawal_id,
            )
        pending = values.get(_PENDING_RECEIPT_KEY)
        if not isinstance(pending, Mapping) or not _receipt_matches_withdrawal_binding(
            pending,
            checkpoint,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        ):
            raise CognitiveNoteWithdrawalError("撤回 pending 回执与请求绑定冲突")
        terminal_receipt = {
            **dict(pending),
            "note_status": note_status,
            "withdrawn_at": withdrawn_at,
            "note_sha256": note_sha256,
        }
        _final_note_consistency_check(root, fragment_id, note_status, note_sha256)
        committed = store.compare_and_append(
            replace(
                raw_checkpoint,
                stop_reason=WITHDRAWN_STOP_REASON,
                eval_results=translate_view_edit(
                    existing=raw_checkpoint.eval_results,
                    edited_flat={
                        **values,
                        _TERMINAL_RECEIPT_KEY: terminal_receipt,
                        "content_lifecycle": "withdrawn",
                    },
                ),
            ),
            expected_sequence=sequence,
            event_type="withdrawal_completed",
            revision_reason="withdraw",
        )
        if committed is not None:
            return CognitiveNoteWithdrawalOutcome(
                withdrawal_id=withdrawal_id,
                status="withdrawn",
                note_status=note_status,
                withdrawn_at=withdrawn_at,
                note_sha256=note_sha256,
                already_withdrawn=False,
            )
    raise RuntimeError("Repeated checkpoint conflicts during withdrawal")
