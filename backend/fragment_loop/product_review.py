"""Local human review and reversible asset publication for Package E products.

This module deliberately stays file based.  A Package E candidate remains
immutable; one small state sidecar records the current human decision and
published assets are new deterministic Markdown files.  It performs no
network, model, credential, Vault scan, or automatic evidence upgrade.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = "fragment-cognitive-candidate-state-v1"
REVIEW_SCHEMA_VERSION = "fragment-cognitive-product-review-v1"
STATE_FILENAME = "candidate-state-v1.json"
CONTENT_STATUSES = {
    "pending_confirmation",
    "kept_draft",
    "published_asset",
    "rejected",
    "withdrawn_asset",
    "conflict",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProductReviewError(ValueError):
    """A candidate, binding, transition, or publication failed closed."""


@dataclass(frozen=True)
class ProductDecisionOutcome:
    candidate_id: str
    content_status: str
    revision: int
    decision_id: str
    idempotent: bool
    review_path: str | None = None
    asset_paths: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def product_decision_id(body: Mapping[str, object]) -> str:
    cards = body.get("selected_card_ids")
    card_values = cards if isinstance(cards, list) else []
    canonical = "\x1f".join(
        (
            str(body.get("requester", "")),
            str(body.get("action", "")),
            str(body.get("candidate_id", "")),
            str(body.get("fragment_ref", "")),
            str(body.get("content_sha256", "")),
            str(body.get("expected_revision", "")),
            ",".join(str(item) for item in card_values),
        )
    )
    return _sha256(canonical.encode())


def _real_directory(path: str | Path, label: str) -> Path:
    requested = Path(path).expanduser()
    try:
        resolved = requested.resolve(strict=True)
        info = requested.lstat()
    except OSError as error:
        raise ProductReviewError(f"{label}_unavailable") from error
    if requested.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ProductReviewError(f"{label}_unsafe")
    if Path(os.path.abspath(requested)) != resolved:
        raise ProductReviewError(f"{label}_unsafe")
    return resolved


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise ProductReviewError(f"{label}_missing") from error
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ProductReviewError(f"{label}_unsafe")
    return path.read_bytes()


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        loaded: object = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductReviewError(f"{label}_invalid") from error
    if not isinstance(loaded, dict):
        raise ProductReviewError(f"{label}_invalid")
    return dict(loaded)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise ProductReviewError("path_outside_vault") from error


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ProductReviewError("publication_path_unsafe")
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _publish_exact(path: Path, payload: bytes) -> bool:
    if path.exists() or path.is_symlink():
        if _regular_bytes(path, "publication") != payload:
            raise ProductReviewError("publication_conflict")
        return True
    _atomic_write(path, payload)
    return False


def _split_markdown(payload: bytes) -> tuple[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProductReviewError("candidate_markdown_invalid") from error
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ProductReviewError("candidate_markdown_invalid")
    header, body = text[4:].split("\n---\n", 1)
    return header, body.lstrip("\n")


def _frontmatter_string(payload: bytes, key: str) -> str:
    header, _body = _split_markdown(payload)
    prefix = f"{key}: "
    matches = [line[len(prefix) :] for line in header.splitlines() if line.startswith(prefix)]
    if len(matches) != 1:
        raise ProductReviewError("publication_conflict")
    try:
        loaded: object = json.loads(matches[0])
    except json.JSONDecodeError as error:
        raise ProductReviewError("publication_conflict") from error
    if not isinstance(loaded, str) or not loaded:
        raise ProductReviewError("publication_conflict")
    return loaded


def _frontmatter(fields: Sequence[tuple[str, object]]) -> str:
    rows = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in fields
    )
    return f"---\n{rows}\n---\n\n"


def _replace_asset_lifecycle(payload: bytes, status: str, changed_at: str) -> bytes:
    header, body = _split_markdown(payload)
    lines = header.splitlines()
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.startswith("content_lifecycle:"):
            output.append(f"content_lifecycle: {json.dumps(status)}")
            replaced = True
        elif not line.startswith("withdrawn_at:") and not line.startswith("republished_at:"):
            output.append(line)
    if not replaced:
        raise ProductReviewError("asset_lifecycle_missing")
    field = "withdrawn_at" if status == "withdrawn" else "republished_at"
    output.append(f"{field}: {json.dumps(changed_at)}")
    rendered_header = "\n".join(output)
    return f"---\n{rendered_header}\n---\n\n{body}".encode()


class ProductReviewService:
    """A single-process, loopback-facing review service over frozen roots."""

    def __init__(
        self,
        candidates_root: str | Path,
        assets_root: str | Path,
        *,
        vault_root: str | Path | None = None,
    ) -> None:
        self.candidates_root = _real_directory(candidates_root, "candidates_root")
        self.assets_root = _real_directory(assets_root, "assets_root")
        self.vault_root = _real_directory(
            vault_root if vault_root is not None else self.candidates_root.parent,
            "vault_root",
        )
        for root in (self.candidates_root, self.assets_root):
            try:
                root.relative_to(self.vault_root)
            except ValueError as error:
                raise ProductReviewError("root_outside_vault") from error
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, candidate_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(candidate_id, threading.Lock())

    def _candidate_dir(self, candidate_id: str) -> Path:
        if not _SAFE_ID.fullmatch(candidate_id):
            raise ProductReviewError("candidate_id_invalid")
        path = self.candidates_root / candidate_id
        try:
            info = path.lstat()
        except OSError as error:
            raise ProductReviewError("candidate_not_found") from error
        if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise ProductReviewError("candidate_unsafe")
        return path

    def _load_candidate(self, candidate_id: str) -> dict[str, Any]:
        directory = self._candidate_dir(candidate_id)
        markdown_files = sorted(directory.glob("fragment-product-*.md"))
        json_files = sorted(directory.glob("fragment-product-*.json"))
        if len(markdown_files) != 1 or len(json_files) != 1:
            raise ProductReviewError("candidate_bundle_invalid")
        markdown_path = markdown_files[0]
        machine_path = json_files[0]
        if markdown_path.stem != machine_path.stem:
            raise ProductReviewError("candidate_bundle_invalid")
        markdown = _regular_bytes(markdown_path, "candidate_markdown")
        machine = _json_object(_regular_bytes(machine_path, "candidate_json"), "candidate_json")
        digest = _sha256(markdown)
        if (
            machine.get("lifecycle_status") != "draft"
            or machine.get("evidence_level") != "unverified"
            or machine.get("user_confirmed") is not False
            or machine.get("promoted_to_asset") is not False
            or machine.get("draft_sha256") != digest
        ):
            raise ProductReviewError("candidate_contract_invalid")
        cards = machine.get("candidate_cards")
        home = machine.get("home_projection")
        fragment_id = machine.get("fragment_id")
        if not isinstance(cards, list) or not cards or not isinstance(home, dict):
            raise ProductReviewError("candidate_contract_invalid")
        if not isinstance(fragment_id, str) or not fragment_id.strip():
            raise ProductReviewError("candidate_contract_invalid")
        card_ids: list[str] = []
        for card in cards:
            if not isinstance(card, dict):
                raise ProductReviewError("candidate_card_invalid")
            card_id = card.get("card_id")
            if (
                not isinstance(card_id, str)
                or not _SAFE_ID.fullmatch(card_id)
                or card_id in card_ids
                or card.get("status") != "candidate"
                or card.get("evidence_level") != "unverified"
                or card.get("promoted_to_asset") is not False
            ):
                raise ProductReviewError("candidate_card_invalid")
            card_ids.append(card_id)
        title = home.get("title")
        fragment_ref = home.get("source_fragment") or f"fragments/{fragment_id}.md"
        if not isinstance(title, str) or not title.strip() or not isinstance(fragment_ref, str):
            raise ProductReviewError("candidate_contract_invalid")
        return {
            "candidate_id": candidate_id,
            "directory": directory,
            "markdown_path": markdown_path,
            "markdown": markdown,
            "candidate_sha256": digest,
            "machine": machine,
            "cards": cards,
            "card_ids": card_ids,
            "title": title.strip(),
            "fragment_ref": fragment_ref,
            "evidence_level": "unverified",
        }

    def _initial_state(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "candidate_id": candidate["candidate_id"],
            "fragment_ref": candidate["fragment_ref"],
            "candidate_sha256": candidate["candidate_sha256"],
            "review_path": None,
            "review_sha256": None,
            "content_status": "pending_confirmation",
            "evidence_level": "unverified",
            "revision": 0,
            "decided_at": None,
            "requester": None,
            "decision_id": None,
            "selected_card_ids": [],
            "asset_files": [],
            "withdrawn_at": None,
            "pending_asset_operation": None,
        }

    def _state_path(self, candidate: Mapping[str, Any]) -> Path:
        return Path(candidate["directory"]) / STATE_FILENAME

    def _existing_vault_file(self, relative: object, label: str) -> Path:
        if not isinstance(relative, str) or "\\" in relative:
            raise ProductReviewError(f"{label}_path_invalid")
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise ProductReviewError(f"{label}_path_invalid")
        requested = self.vault_root / relative_path
        try:
            resolved = requested.resolve(strict=True)
            resolved.relative_to(self.vault_root)
        except (OSError, ValueError) as error:
            raise ProductReviewError(f"{label}_path_invalid") from error
        if Path(os.path.abspath(requested)) != resolved:
            raise ProductReviewError(f"{label}_path_invalid")
        return resolved

    def _load_state(self, candidate: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        path = self._state_path(candidate)
        if not path.exists() and not path.is_symlink():
            return self._initial_state(candidate), False
        state = _json_object(_regular_bytes(path, "candidate_state"), "candidate_state")
        expected_keys = set(self._initial_state(candidate))
        legacy_keys = expected_keys - {"pending_asset_operation"}
        if set(state) == legacy_keys:
            state["pending_asset_operation"] = None
        elif set(state) != expected_keys:
            raise ProductReviewError("candidate_state_invalid")
        if (
            state.get("schema_version") != STATE_SCHEMA_VERSION
            or state.get("candidate_id") != candidate["candidate_id"]
            or state.get("fragment_ref") != candidate["fragment_ref"]
            or state.get("candidate_sha256") != candidate["candidate_sha256"]
            or state.get("evidence_level") != "unverified"
            or state.get("content_status") not in CONTENT_STATUSES - {"conflict"}
            or not isinstance(state.get("revision"), int)
            or isinstance(state.get("revision"), bool)
            or int(state["revision"]) < 0
        ):
            raise ProductReviewError("candidate_state_invalid")
        for key in ("selected_card_ids", "asset_files"):
            if not isinstance(state.get(key), list):
                raise ProductReviewError("candidate_state_invalid")
        pending = state.get("pending_asset_operation")
        if int(state["revision"]) == 0 and pending is None:
            raise ProductReviewError("candidate_state_invalid")
        if pending is not None:
            if (
                not isinstance(pending, dict)
                or set(pending)
                != {
                    "action",
                    "decision_id",
                    "expected_revision",
                    "changed_at",
                    "targets",
                }
                or pending.get("action") not in {"confirm_asset", "withdraw_asset"}
                or not isinstance(pending.get("decision_id"), str)
                or not isinstance(pending.get("expected_revision"), int)
                or isinstance(pending.get("expected_revision"), bool)
                or not isinstance(pending.get("changed_at"), str)
                or not isinstance(pending.get("targets"), list)
            ):
                raise ProductReviewError("candidate_state_invalid")
            for target in pending["targets"]:
                if (
                    not isinstance(target, dict)
                    or set(target) != {"path", "source_sha256", "target_sha256"}
                    or not isinstance(target.get("path"), str)
                    or (
                        target.get("source_sha256") is not None
                        and not _SHA256.fullmatch(str(target.get("source_sha256")))
                    )
                    or not _SHA256.fullmatch(str(target.get("target_sha256")))
                ):
                    raise ProductReviewError("candidate_state_invalid")
        return state, True

    def _write_state(self, candidate: Mapping[str, Any], state: Mapping[str, Any]) -> None:
        payload = (
            json.dumps(dict(state), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        _atomic_write(self._state_path(candidate), payload)

    def _pending_operation(
        self,
        state: Mapping[str, Any],
        *,
        action: str,
        decision_id: object,
        expected_revision: object,
    ) -> dict[str, Any] | None:
        pending = state.get("pending_asset_operation")
        if pending is None:
            return None
        if (
            not isinstance(pending, dict)
            or pending.get("action") != action
            or pending.get("decision_id") != decision_id
            or pending.get("expected_revision") != expected_revision
        ):
            raise ProductReviewError("asset_operation_pending")
        return dict(pending)

    def _journal_asset_operation(
        self,
        candidate: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        action: str,
        decision_id: object,
        expected_revision: object,
        changed_at: str,
        targets: list[dict[str, str | None]],
    ) -> dict[str, Any]:
        pending = self._pending_operation(
            state,
            action=action,
            decision_id=decision_id,
            expected_revision=expected_revision,
        )
        expected = {
            "action": action,
            "decision_id": decision_id,
            "expected_revision": expected_revision,
            "changed_at": changed_at,
            "targets": targets,
        }
        if pending is not None:
            if pending != expected:
                raise ProductReviewError("asset_operation_changed")
            return pending
        journaled = dict(state)
        journaled["pending_asset_operation"] = expected
        self._write_state(candidate, journaled)
        return expected

    def _asset_records_valid(self, state: Mapping[str, Any]) -> bool:
        records = state.get("asset_files")
        if not isinstance(records, list):
            return False
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
                return False
            relative = record.get("path")
            digest = record.get("sha256")
            if not isinstance(relative, str) or not _SHA256.fullmatch(str(digest)):
                return False
            try:
                path = self._existing_vault_file(relative, "asset")
                if _sha256(_regular_bytes(path, "asset")) != digest:
                    return False
            except ProductReviewError:
                return False
        return True

    def _projection(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        try:
            state, _persisted = self._load_state(candidate)
            status = str(state["content_status"])
            has_conflict = status in {
                "published_asset",
                "withdrawn_asset",
            } and not self._asset_records_valid(state)
            if has_conflict:
                status = "conflict"
        except ProductReviewError:
            state = self._initial_state(candidate)
            status = "conflict"
            has_conflict = True
        actions = {
            "pending_confirmation": [
                "open_detail",
                "confirm_asset",
                "create_edit_copy",
                "keep_draft",
                "reject",
            ],
            "kept_draft": ["open_detail", "reopen"],
            "published_asset": ["open_detail", "withdraw_asset"],
            "rejected": ["open_detail", "reopen"],
            "withdrawn_asset": ["open_detail", "reopen"],
            "conflict": ["open_detail"],
        }[status]
        home = candidate["machine"].get("home_projection", {})
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "candidate_id": candidate["candidate_id"],
            "fragment_ref": candidate["fragment_ref"],
            "title": candidate["title"],
            "note_path": _relative(candidate["markdown_path"], self.vault_root),
            "content_status": status,
            "evidence_level": "unverified",
            "candidate_card_count": len(candidate["cards"]),
            "updated_at": state.get("decided_at") or "",
            "available_actions": actions,
            "has_conflict": has_conflict,
            "revision": state.get("revision", 0),
            "content_sha256": state.get("review_sha256") or candidate["candidate_sha256"],
            "selected_card_ids": list(state.get("selected_card_ids", [])),
            "asset_paths": [
                str(record.get("path", ""))
                for record in state.get("asset_files", [])
                if isinstance(record, dict)
            ],
            "core_judgment": home.get("core_judgment", ""),
            "user_value": home.get("user_value", ""),
        }

    def list_reviews(self) -> list[dict[str, Any]]:
        reviews: list[dict[str, Any]] = []
        for entry in sorted(self.candidates_root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("."):
                continue
            if not entry.is_dir() or entry.is_symlink() or not _SAFE_ID.fullmatch(entry.name):
                continue
            if not any(entry.glob("fragment-product-*")):
                continue
            try:
                candidate = self._load_candidate(entry.name)
                reviews.append(self._projection(candidate))
            except ProductReviewError:
                reviews.append(
                    {
                        "schema_version": REVIEW_SCHEMA_VERSION,
                        "candidate_id": entry.name,
                        "fragment_ref": "",
                        "title": entry.name,
                        "note_path": "",
                        "content_status": "conflict",
                        "evidence_level": "unverified",
                        "candidate_card_count": 0,
                        "updated_at": "",
                        "available_actions": [],
                        "has_conflict": True,
                        "revision": 0,
                        "content_sha256": "",
                        "core_judgment": "",
                        "user_value": "",
                    }
                )
        return reviews

    def detail(self, candidate_id: str) -> dict[str, Any]:
        candidate = self._load_candidate(candidate_id)
        projection = self._projection(candidate)
        projection["candidate_cards"] = [
            {
                "card_id": card["card_id"],
                "title": card.get("title", ""),
                "knowledge": card.get("knowledge", ""),
                "scope": card.get("scope", ""),
                "evidence_level": "unverified",
            }
            for card in candidate["cards"]
        ]
        return projection

    def _review_content(
        self, candidate: Mapping[str, Any], state: Mapping[str, Any]
    ) -> tuple[bytes, str | None]:
        review_path = state.get("review_path")
        if review_path is None:
            return candidate["markdown"], None
        if not isinstance(review_path, str):
            raise ProductReviewError("review_path_invalid")
        path = self._existing_vault_file(review_path, "review")
        try:
            path.relative_to(candidate["directory"])
        except ValueError as error:
            raise ProductReviewError("review_path_invalid") from error
        return _regular_bytes(path, "review_copy"), review_path

    def _render_result(
        self, candidate: Mapping[str, Any], content: bytes, decided_at: str
    ) -> bytes:
        _header, body = _split_markdown(content)
        body = body.replace(
            "> [!note] 状态：draft 草稿 · 证据等级 unverified · 尚未人工确认",
            "> [!success] 用户已确认并形成资产 · 证据等级仍为 unverified（事实未验证）",
            1,
        )
        fields = (
            ("type", "用户确认的认知整理结果"),
            ("title", candidate["title"]),
            ("content_lifecycle", "published"),
            ("evidence_level", "unverified"),
            ("user_confirmed", True),
            ("promoted_to_asset", True),
            ("candidate_id", candidate["candidate_id"]),
            ("source_fragment", candidate["fragment_ref"]),
            ("confirmed_at", decided_at),
        )
        return (_frontmatter(fields) + body).encode()

    def _render_card(
        self, candidate: Mapping[str, Any], card: Mapping[str, Any], decided_at: str
    ) -> bytes:
        fields = (
            ("type", "Loop知识卡"),
            ("title", card.get("title", card["card_id"])),
            ("asset_kind", "knowledge_card"),
            ("content_lifecycle", "published"),
            ("evidence_level", "unverified"),
            ("user_confirmed", True),
            ("promoted_to_asset", True),
            ("candidate_id", candidate["candidate_id"]),
            ("source_fragment", candidate["fragment_ref"]),
            ("card_id", card["card_id"]),
            ("confirmed_at", decided_at),
        )
        evidence = "、".join(str(item) for item in card.get("evidence_refs", [])) or "无"
        body = (
            f"# {card.get('title', card['card_id'])}\n\n"
            "> [!warning] 用户确认的知识卡 · 证据等级 unverified（事实未验证）\n\n"
            f"## 知识点\n\n{card.get('knowledge', '')}\n\n"
            f"## 为什么重要\n\n{card.get('why_important', '')}\n\n"
            f"## 适用范围\n\n{card.get('scope', '')}\n\n"
            f"## 证据引用\n\n{evidence}\n"
        )
        return (_frontmatter(fields) + body).encode()

    def decide(self, candidate_id: str, body: Mapping[str, object]) -> ProductDecisionOutcome:
        if body.get("candidate_id") != candidate_id or body.get("requester") != "nigo":
            raise ProductReviewError("decision_binding_invalid")
        action = body.get("action")
        if action not in {"keep_draft", "reject", "reopen", "create_edit_copy", "confirm_asset"}:
            raise ProductReviewError("decision_action_invalid")
        if body.get("decision_id") != product_decision_id(body):
            raise ProductReviewError("decision_id_invalid")
        expected_revision = body.get("expected_revision")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ProductReviewError("expected_revision_invalid")
        candidate = self._load_candidate(candidate_id)
        if body.get("fragment_ref") != candidate["fragment_ref"]:
            raise ProductReviewError("decision_binding_invalid")
        with self._lock(candidate_id):
            state, _persisted = self._load_state(candidate)
            if state.get("decision_id") == body.get("decision_id"):
                return ProductDecisionOutcome(
                    candidate_id,
                    str(state["content_status"]),
                    int(state["revision"]),
                    str(state["decision_id"]),
                    True,
                    state.get("review_path"),
                    tuple(str(item["path"]) for item in state.get("asset_files", [])),
                )
            pending_operation = state.get("pending_asset_operation")
            if pending_operation is not None and (
                action != "confirm_asset"
                or not isinstance(pending_operation, dict)
                or pending_operation.get("decision_id") != body.get("decision_id")
                or pending_operation.get("expected_revision") != expected_revision
            ):
                raise ProductReviewError("asset_operation_pending")
            if int(state["revision"]) != expected_revision:
                raise ProductReviewError("decision_revision_conflict")
            current = str(state["content_status"])
            if action == "reopen":
                if current not in {"kept_draft", "rejected", "withdrawn_asset"}:
                    raise ProductReviewError("decision_transition_invalid")
            elif current != "pending_confirmation":
                raise ProductReviewError("decision_transition_invalid")
            cards = body.get("selected_card_ids")
            if not isinstance(cards, list) or not all(isinstance(item, str) for item in cards):
                raise ProductReviewError("selected_cards_invalid")
            if len(cards) != len(set(cards)) or any(
                item not in candidate["card_ids"] for item in cards
            ):
                raise ProductReviewError("selected_cards_invalid")
            content, review_path = self._review_content(candidate, state)
            content_sha256 = _sha256(content)
            if body.get("content_sha256") != content_sha256:
                raise ProductReviewError("decision_content_changed")
            pending = (
                self._pending_operation(
                    state,
                    action="confirm_asset",
                    decision_id=body.get("decision_id"),
                    expected_revision=expected_revision,
                )
                if action == "confirm_asset"
                else None
            )
            now = str(pending["changed_at"]) if pending is not None else _now()
            next_state = dict(state)
            next_state.update(
                {
                    "revision": int(state["revision"]) + 1,
                    "decided_at": now,
                    "requester": "nigo",
                    "decision_id": body["decision_id"],
                }
            )
            asset_paths: tuple[str, ...] = ()
            if action == "keep_draft":
                next_state["content_status"] = "kept_draft"
            elif action == "reject":
                next_state["content_status"] = "rejected"
            elif action == "reopen":
                next_state["content_status"] = "pending_confirmation"
            elif action == "create_edit_copy":
                if cards:
                    raise ProductReviewError("selected_cards_invalid")
                filename = f"review-v{next_state['revision']}.md"
                destination = Path(candidate["directory"]) / filename
                _publish_exact(destination, content)
                next_state["review_path"] = _relative(destination, self.vault_root)
                next_state["review_sha256"] = content_sha256
                next_state["content_status"] = "pending_confirmation"
            else:
                if not cards:
                    raise ProductReviewError("selected_cards_invalid")
                selected = [card for card in candidate["cards"] if card["card_id"] in cards]
                output = self.assets_root / candidate_id
                if output.exists() or output.is_symlink():
                    try:
                        output_info = output.lstat()
                    except OSError as error:
                        raise ProductReviewError("publication_path_unsafe") from error
                    if output.is_symlink() or not stat.S_ISDIR(output_info.st_mode):
                        raise ProductReviewError("publication_path_unsafe")
                output.mkdir(parents=True, exist_ok=True)
                digest = content_sha256[:16]
                result_path = output / f"result-{digest}.md"
                previous_records = {
                    str(record["path"]): str(record["sha256"])
                    for record in state.get("asset_files", [])
                    if isinstance(record, dict)
                    and isinstance(record.get("path"), str)
                    and isinstance(record.get("sha256"), str)
                }
                result_relative = _relative(result_path, self.vault_root)
                if (
                    pending is None
                    and result_path.exists()
                    and result_relative not in previous_records
                ):
                    now = _frontmatter_string(_regular_bytes(result_path, "asset"), "confirmed_at")
                rendered_payloads: list[tuple[Path, bytes]] = [
                    (result_path, self._render_result(candidate, content, now))
                ]
                for card in selected:
                    rendered_payloads.append(
                        (
                            output / f"card-{card['card_id']}-{digest}.md",
                            self._render_card(candidate, card, now),
                        )
                    )
                pending_targets = (
                    {
                        str(item["path"]): item
                        for item in pending.get("targets", [])
                        if isinstance(item, dict) and isinstance(item.get("path"), str)
                    }
                    if pending is not None
                    else {}
                )
                payloads: list[tuple[Path, bytes]] = []
                targets: list[dict[str, str | None]] = []
                for path, rendered in rendered_payloads:
                    relative = _relative(path, self.vault_root)
                    source_sha256 = previous_records.get(relative)
                    payload = rendered
                    if source_sha256 is not None:
                        existing = _regular_bytes(path, "asset")
                        existing_sha256 = _sha256(existing)
                        pending_target = pending_targets.get(relative)
                        if existing_sha256 == source_sha256:
                            payload = _replace_asset_lifecycle(existing, "published", now)
                        elif pending_target is not None and existing_sha256 == pending_target.get(
                            "target_sha256"
                        ):
                            payload = existing
                        else:
                            raise ProductReviewError("asset_changed")
                    payloads.append((path, payload))
                    targets.append(
                        {
                            "path": relative,
                            "source_sha256": source_sha256,
                            "target_sha256": _sha256(payload),
                        }
                    )
                pending = self._journal_asset_operation(
                    candidate,
                    state,
                    action="confirm_asset",
                    decision_id=body.get("decision_id"),
                    expected_revision=expected_revision,
                    changed_at=now,
                    targets=targets,
                )
                records: list[dict[str, str]] = []
                for path, payload in payloads:
                    relative = _relative(path, self.vault_root)
                    if path.exists() and _sha256(_regular_bytes(path, "asset")) == _sha256(payload):
                        pass
                    elif relative in previous_records:
                        _atomic_write(path, payload)
                    else:
                        _publish_exact(path, payload)
                    records.append({"path": relative, "sha256": _sha256(payload)})
                next_state.update(
                    {
                        "content_status": "published_asset",
                        "review_path": review_path,
                        "review_sha256": content_sha256 if review_path else None,
                        "selected_card_ids": list(cards),
                        "asset_files": records,
                        "withdrawn_at": None,
                        "pending_asset_operation": None,
                    }
                )
                asset_paths = tuple(record["path"] for record in records)
            self._write_state(candidate, next_state)
            return ProductDecisionOutcome(
                candidate_id,
                str(next_state["content_status"]),
                int(next_state["revision"]),
                str(next_state["decision_id"]),
                False,
                next_state.get("review_path"),
                asset_paths,
            )

    def withdraw(self, candidate_id: str, body: Mapping[str, object]) -> ProductDecisionOutcome:
        if body.get("action") != "withdraw_asset":
            raise ProductReviewError("decision_action_invalid")
        return self._withdraw_or_reject(candidate_id, body)

    def _withdraw_or_reject(
        self, candidate_id: str, body: Mapping[str, object]
    ) -> ProductDecisionOutcome:
        if body.get("candidate_id") != candidate_id or body.get("requester") != "nigo":
            raise ProductReviewError("decision_binding_invalid")
        if body.get("decision_id") != product_decision_id(body):
            raise ProductReviewError("decision_id_invalid")
        candidate = self._load_candidate(candidate_id)
        if body.get("fragment_ref") != candidate["fragment_ref"]:
            raise ProductReviewError("decision_binding_invalid")
        expected_revision = body.get("expected_revision")
        with self._lock(candidate_id):
            state, _ = self._load_state(candidate)
            if state.get("decision_id") == body.get("decision_id"):
                return ProductDecisionOutcome(
                    candidate_id,
                    str(state["content_status"]),
                    int(state["revision"]),
                    str(state["decision_id"]),
                    True,
                    asset_paths=tuple(str(item["path"]) for item in state["asset_files"]),
                )
            pending = self._pending_operation(
                state,
                action="withdraw_asset",
                decision_id=body.get("decision_id"),
                expected_revision=expected_revision,
            )
            if (
                expected_revision != state.get("revision")
                or state.get("content_status") != "published_asset"
            ):
                raise ProductReviewError("decision_transition_invalid")
            if body.get("content_sha256") != (
                state.get("review_sha256") or candidate["candidate_sha256"]
            ):
                raise ProductReviewError("decision_content_changed")
            cards = body.get("selected_card_ids")
            if cards != state.get("selected_card_ids"):
                raise ProductReviewError("selected_cards_invalid")
            now = str(pending["changed_at"]) if pending is not None else _now()
            pending_targets = (
                {
                    str(item["path"]): item
                    for item in pending.get("targets", [])
                    if isinstance(item, dict) and isinstance(item.get("path"), str)
                }
                if pending is not None
                else {}
            )
            payloads: list[tuple[Path, bytes]] = []
            targets: list[dict[str, str | None]] = []
            for record in state["asset_files"]:
                path = self._existing_vault_file(record["path"], "asset")
                current = _regular_bytes(path, "asset")
                current_sha256 = _sha256(current)
                pending_target = pending_targets.get(str(record["path"]))
                if current_sha256 == record["sha256"]:
                    withdrawn = _replace_asset_lifecycle(current, "withdrawn", now)
                elif pending_target is not None and current_sha256 == pending_target.get(
                    "target_sha256"
                ):
                    withdrawn = current
                else:
                    raise ProductReviewError("asset_changed")
                payloads.append((path, withdrawn))
                targets.append(
                    {
                        "path": str(record["path"]),
                        "source_sha256": str(record["sha256"]),
                        "target_sha256": _sha256(withdrawn),
                    }
                )
            self._journal_asset_operation(
                candidate,
                state,
                action="withdraw_asset",
                decision_id=body.get("decision_id"),
                expected_revision=expected_revision,
                changed_at=now,
                targets=targets,
            )
            records: list[dict[str, str]] = []
            for path, withdrawn in payloads:
                if _sha256(_regular_bytes(path, "asset")) != _sha256(withdrawn):
                    _atomic_write(path, withdrawn)
                records.append(
                    {"path": _relative(path, self.vault_root), "sha256": _sha256(withdrawn)}
                )
            next_state = dict(state)
            next_state.update(
                {
                    "content_status": "withdrawn_asset",
                    "revision": int(state["revision"]) + 1,
                    "decided_at": now,
                    "withdrawn_at": now,
                    "decision_id": body["decision_id"],
                    "requester": "nigo",
                    "asset_files": records,
                    "pending_asset_operation": None,
                }
            )
            self._write_state(candidate, next_state)
            return ProductDecisionOutcome(
                candidate_id,
                "withdrawn_asset",
                int(next_state["revision"]),
                str(body["decision_id"]),
                False,
                asset_paths=tuple(record["path"] for record in records),
            )
