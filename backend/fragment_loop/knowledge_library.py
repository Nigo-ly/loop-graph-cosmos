"""Versioned research notes and derived indexes in the existing Obsidian assets root.

No model or network calls occur here. Semantic grouping comes from the validated
research result; a file lock and immutable revision directories commit each note.
The execution checkpoint records the returned receipt, never a fictitious human
approval. Indexes can be rebuilt from committed notes after an interrupted write.
"""

from __future__ import annotations

import fcntl
import hashlib
import html
import ipaddress
import json
import os
import re
import shlex
import shutil
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fragment_loop.product_review import (
    ProductReviewError,
    _atomic_write,
    _frontmatter,
    _json_object,
    _real_directory,
    _regular_bytes,
)

_ID = re.compile(r"^(?:knowledge|topic|period)-[0-9a-f]{24}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_RELATIONS = {"supports", "partially_supports", "conflicts", "irrelevant"}
DERIVATION_POLICY_VERSION = "canonical-research-structure-v2"


class KnowledgeLibraryError(ProductReviewError):
    """The supplied research or stored revision cannot be trusted."""


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _text(value: object, label: str, limit: int = 50000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise KnowledgeLibraryError(f"{label}_invalid")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise KnowledgeLibraryError(f"{label}_invalid")
    return value.strip()


def _time(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value, "timestamp", 80))
    except ValueError as error:
        raise KnowledgeLibraryError("timestamp_invalid") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise KnowledgeLibraryError("timestamp_timezone_required")
    return result.astimezone(UTC)


def _strings(value: object, label: str, limit: int = 5000) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise KnowledgeLibraryError(f"{label}_invalid")
    return [_text(item, label, limit) for item in value]


def _public_url(value: object) -> str:
    url = _text(value, "source_url", 3000)
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or not host or parts.username or parts.password:
            raise ValueError
        if host == "localhost" or host.endswith((".local", ".localhost")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except ValueError as error:
        raise KnowledgeLibraryError("source_url_invalid") from error
    return url


# Closed display-language set: host plugins can execute arbitrary named fences.
_STATIC_CODE_LANGUAGES = frozenset({
    "", "text", "plaintext", "python", "py", "javascript", "js", "typescript", "ts",
    "json", "jsonc", "yaml", "yml", "toml", "ini", "bash", "sh", "shell", "zsh",
    "html", "xml", "css", "sql", "diff", "markdown", "md", "mermaid",
})


def _reject_active_inline_code(value: str) -> None:
    # Dataview trims code.innerText; this also catches generated code wrappers.
    # Conservative even in escaped examples: research must not query the Vault.
    if re.search(r"`+[\s\ufeff]*(?:\$=|=)", value):
        raise KnowledgeLibraryError("active_markdown_code_forbidden")


def _markdown_prose(value: str) -> str:
    # Escape incomplete HTML starters too: a tag/comment can straddle code spans.
    value = re.sub(
        r"<(?=[A-Za-z!/])[^>]*>", lambda match: html.escape(match.group(), quote=False), value
    )
    return value.replace("<", "&lt;")


def _markdown_inline(value: str) -> str:
    parts: list[str] = []
    start = position = 0
    while position < len(value):
        if value[position] != "`":
            position += 1
            continue
        preceding = position - 1
        while preceding >= 0 and value[preceding] == "\\":
            preceding -= 1
        if (position - preceding - 1) % 2:
            position += 1
            continue
        opener = re.match(r"`+", value[position:])
        assert opener is not None
        length = len(opener.group())
        closing = next(
            (match for match in re.finditer(r"`+", value[position + length:])
             if len(match.group()) == length),
            None,
        )
        if closing is None:
            raise KnowledgeLibraryError("markdown_code_span_not_closed_on_line")
        end = position + length + closing.end()
        parts.extend((_markdown_prose(value[start:position]), value[position:end]))
        start = position = end
    parts.append(_markdown_prose(value[start:]))
    return "".join(parts)


def _markdown(value: str, *, code_domains: bool = False) -> str:
    _reject_active_inline_code(value)
    # Keep the active-link rejection global, including literal code examples.
    if re.search(
        r"(?:\]\(\s*<?|^\s*\[[^\]\n]+\]:\s*<?)(?:javascript|vbscript|data|file)\s*:",
        value,
        re.I | re.M,
    ):
        raise KnowledgeLibraryError("active_markdown_url_forbidden")
    # Only the complete answer is inserted without a heading/list prefix.
    # Other fields retain their prose treatment; prefixes can change code parsing.
    if not code_domains:
        # Excerpts can contain nested/truncated fences. Do not reinterpret them
        # as complete answers, but never admit a host plugin or a query prefix.
        for candidate in re.finditer(
            r"(?:^|[\r\n])(?:[ \t>]|[-+*][ \t]+|[0-9]+[.)][ \t]+)*"
            r"(?:`{3,}|~{3,})([^\r\n]*)",
            value, re.M,
        ):
            info = candidate[1].strip().split()
            if info and info[0].lower() not in _STATIC_CODE_LANGUAGES:
                raise KnowledgeLibraryError("markdown_code_language_forbidden")
            if re.match(r"[\s\ufeff>]*(?:\$=|=)", value[candidate.end():]):
                raise KnowledgeLibraryError("active_markdown_code_forbidden")
        return _markdown_prose(value)
    # ponytail: preserve a conservative CommonMark subset, not a new renderer:
    # top-level closed fences with a plain language token, and single-line code
    # spans. Reject ambiguous/container fences and unclosed spans; supporting
    # them needs a real block parser. Never let a fence swallow the audit footer.
    parts: list[str] = []
    fence = ""
    first_content = False
    for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", value):
        body = line.rstrip("\r\n")
        if fence:
            if not first_content and re.search(r"[^\s\ufeff]", body):
                if re.match(r"[\s\ufeff]*(?:\$=|=)", body):
                    raise KnowledgeLibraryError("active_markdown_code_forbidden")
                first_content = True
            parts.append(line if code_domains else _markdown_prose(line))
            closing = r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*"
            if re.fullmatch(closing, body):
                fence = ""
            continue
        opener = re.match(r"(`{3,}|~{3,})(.*)$", body)
        if opener:
            if not re.fullmatch(r"[A-Za-z0-9_+.-]*", opener[2].strip()):
                raise KnowledgeLibraryError("markdown_code_fence_info_invalid")
            if opener[2].strip().lower() not in _STATIC_CODE_LANGUAGES:
                raise KnowledgeLibraryError("markdown_code_language_forbidden")
            fence = opener[1]
            first_content = False
            parts.append(line)
        elif re.match(r"(?:[ \t>]|[-+*][ \t]+|[0-9]+[.)][ \t]+)+(?:`{3,}|~{3,})", body):
            raise KnowledgeLibraryError("markdown_code_fence_container_unsupported")
        else:
            parts.append(_markdown_inline(line) if code_domains else _markdown_prose(line))
    if fence:
        raise KnowledgeLibraryError("markdown_code_fence_not_closed")
    return "".join(parts)


def _references(value: object, allowed: set[str], label: str) -> list[str]:
    refs = _strings(value, label)
    if not refs or len(set(refs)) != len(refs) or not set(refs) <= allowed:
        raise KnowledgeLibraryError(f"{label}_not_closed")
    return refs


def _evidence(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in records:
        if not isinstance(raw, Mapping):
            raise KnowledgeLibraryError("evidence_record_invalid")
        if raw.get("marker") in {"omitted", "stale"}:
            continue
        identifier = _text(raw.get("evidence_id"), "evidence_id", 200)
        if identifier in seen:
            raise KnowledgeLibraryError("duplicate_evidence_id")
        record: dict[str, Any] = {
            "evidence_id": identifier,
            "title": _text(raw.get("title"), "evidence_title", 1000),
            "source_type": _text(raw.get("source_type", "public_source"), "source_type", 100),
        }
        excerpts = raw.get("excerpts", raw.get("excerpt_windows"))
        if excerpts is None and isinstance(raw.get("excerpt"), str):
            excerpts = [raw["excerpt"]]
        record["excerpts"] = _strings(excerpts, "excerpts", 50000)
        if not record["excerpts"]:
            raise KnowledgeLibraryError("evidence_content_missing")
        digest = raw.get(
            "evidence_digest",
            raw.get("digest", raw.get("content_digest", raw.get("content_sha256"))),
        )
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise KnowledgeLibraryError("evidence_digest_required")
        record["digest"] = digest
        for digest_field in ("evidence_digest", "page_digest"):
            if digest_field in raw:
                if not isinstance(raw[digest_field], str) or not _SHA.fullmatch(raw[digest_field]):
                    raise KnowledgeLibraryError("evidence_digest_invalid")
                record[digest_field] = raw[digest_field]
        if record["source_type"] == "local_contract":
            source_ref = _text(raw.get("source_ref"), "source_ref", 1000)
            if (
                not source_ref.startswith("project:")
                or any(part in {"", ".", ".."} for part in source_ref[8:].split("/"))
                or "\\" in source_ref
            ):
                raise KnowledgeLibraryError("source_ref_invalid")
            record.update(url="", source_ref=source_ref)
        else:
            record["url"] = _public_url(raw.get("url"))
        if record["source_type"] == "repository_trial":
            exit_code = raw.get("exit_code")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                raise KnowledgeLibraryError("trial_exit_code_required")
            output_digest = raw.get("output_digest")
            if not isinstance(output_digest, str) or not _SHA.fullmatch(output_digest):
                raise KnowledgeLibraryError("trial_output_digest_required")
            command = raw.get("command")
            if isinstance(command, list):
                _strings(command, "trial_argv", 4000)
                if not command:
                    raise KnowledgeLibraryError("trial_command_invalid")
                argv = list(command)  # Keep fixture whitespace; this is a receipt, not execution.
                command = shlex.join(argv)
                record["argv"] = argv
            record.update(
                # Shell quoting expands one quote to five characters. This also
                # covers the tool's 24 x 500-character relative-script arguments.
                command=_text(command, "trial_command", 64000),
                revision=_text(raw.get("revision"), "trial_revision", 200),
                exit_code=exit_code,
                output_digest=output_digest,
            )
        for key in ("fetched_at", "identity_status", "source_ref", "revision"):
            if key in raw and key not in record:
                record[key] = _text(raw[key], key, 1000)
        for key in ("provenance", "provenance_closed", "text_truncated"):
            if key in raw:
                record[key] = json.loads(_json(raw[key]))
        if len(_json(record)) > 200000:
            raise KnowledgeLibraryError("evidence_record_too_large")
        seen.add(identifier)
        result.append(record)
    if not result:
        raise KnowledgeLibraryError("evidence_required")
    return result


def _result(value: Mapping[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        key: _text(value.get(key), key) for key in ("summary", "recommendation")
    }
    ids = {record["evidence_id"] for record in evidence}
    for field in ("confirmed", "claims", "conflicts", "coverage"):
        entries = value.get(field, [])
        if not isinstance(entries, list) or len(entries) > 100:
            raise KnowledgeLibraryError(f"{field}_invalid")
    if not isinstance(value.get("answer_markdown", ""), str):
        raise KnowledgeLibraryError("answer_markdown_invalid")
    confirmed: list[dict[str, Any]] = []
    for item in value.get("confirmed", []):
        if not isinstance(item, dict):
            raise KnowledgeLibraryError("confirmed_invalid")
        confirmed.append(
            {
                "claim": _text(item.get("claim"), "claim"),
                "evidence_ids": _references(item.get("evidence_ids"), ids, "evidence_ids"),
            }
        )
    if not confirmed:
        raise KnowledgeLibraryError("substantiated_conclusion_required")
    claims: list[dict[str, Any]] = []
    for item in value.get("claims", []):
        if not isinstance(item, dict) or item.get("relation") not in _RELATIONS:
            raise KnowledgeLibraryError("claim_relation_invalid")
        _references([item.get("evidence_id")], ids, "evidence_ids")
        claims.append(
            {key: _text(item.get(key), key) for key in ("claim", "evidence_id", "relation")}
        )
    conflicts: list[dict[str, Any]] = []
    for item in value.get("conflicts", []):
        if not isinstance(item, dict):
            raise KnowledgeLibraryError("conflict_invalid")
        conflicts.append(
            {
                "topic": _text(item.get("topic"), "conflict_topic"),
                "dimensions": _strings(item.get("dimensions"), "dimensions"),
                "evidence_ids": _references(item.get("evidence_ids"), ids, "evidence_ids"),
            }
        )
    usage = value.get("agent_usage", {})
    if not isinstance(usage, dict):
        raise KnowledgeLibraryError("agent_usage_invalid")
    limitations = _strings(usage.get("limitations", []), "limitations")
    result.update(
        confirmed=confirmed,
        claims=claims,
        conflicts=conflicts,
        unknowns=_strings(value.get("unknowns"), "unknowns"),
        limitations=limitations,
        answer_markdown=_markdown(str(value.get("answer_markdown", "")), code_domains=True),
        agent_usage={
            "when_to_use": _text(usage.get("when_to_use", value["summary"]), "when_to_use"),
            "steps": _strings(usage.get("steps", []), "steps"),
            "limitations": limitations,
        },
    )
    coverage: list[dict[str, Any]] = []
    for item in value.get("coverage", []):
        if not isinstance(item, dict) or item.get("status") not in {"answered", "unknown"}:
            raise KnowledgeLibraryError("coverage_invalid")
        refs = item.get("evidence_ids", [])
        if item["status"] == "answered" or refs:
            refs = _references(refs, ids, "evidence_ids")
        coverage.append(
            {
                "question": _text(item.get("question"), "question"),
                "answer": _text(item.get("answer"), "answer"),
                "evidence_ids": refs,
                "status": item["status"],
            }
        )
    result["coverage"] = coverage
    # Bound total payload, including nested fields, before writing anything.
    if len(_json(result)) > 250000:
        raise KnowledgeLibraryError("result_too_large")
    return result


class KnowledgeLibrary:
    """Automatic publication with full-text reads and persistent topic identity."""

    def __init__(self, assets_root: str | Path, *, vault_root: str | Path) -> None:
        self.assets_root = _real_directory(assets_root, "assets_root")
        self.vault_root = _real_directory(vault_root, "vault_root")
        if not self.assets_root.is_relative_to(self.vault_root):
            raise KnowledgeLibraryError("root_outside_vault")
        self.root = self._directory(self.assets_root / "研究知识")

    def _directory(self, path: Path) -> Path:
        path.mkdir(exist_ok=True)
        return _real_directory(path, "knowledge_directory")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock = self.root / ".write.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _versions(self, knowledge_id: str) -> list[Path]:
        if not _ID.fullmatch(knowledge_id) or knowledge_id.startswith("topic-"):
            raise KnowledgeLibraryError("knowledge_id_invalid")
        path = self.root / knowledge_id
        if not path.exists() and not path.is_symlink():
            return []
        _real_directory(path, "knowledge_directory")
        versions = sorted(item for item in path.iterdir() if re.fullmatch(r"[0-9]{8}", item.name))
        if [int(item.name) for item in versions] != list(range(1, len(versions) + 1)):
            raise KnowledgeLibraryError("revision_chain_broken")
        return versions

    def _read_version(self, path: Path) -> dict[str, Any]:
        _real_directory(path, "revision_directory")
        doc = _json_object(_regular_bytes(path / "record.json", "knowledge"), "knowledge")
        if doc.get("knowledge_id") != path.parent.name or doc.get("revision") != int(path.name):
            raise KnowledgeLibraryError("revision_binding_invalid")
        note_digest = doc.pop("note_sha256", None)
        digest = doc.pop("content_sha256", None)
        if digest != _digest(doc):
            raise KnowledgeLibraryError("knowledge_digest_mismatch")
        doc["content_sha256"] = digest
        markdown = _regular_bytes(path / "note.md", "knowledge_note")
        if hashlib.sha256(markdown).hexdigest() != note_digest:
            raise KnowledgeLibraryError("knowledge_note_modified")
        doc["note_sha256"] = note_digest
        return doc

    def _read_raw(self, knowledge_id: str, revision: int | None = None) -> dict[str, Any]:
        versions = self._versions(knowledge_id)
        if not versions:
            raise KnowledgeLibraryError("knowledge_not_found")
        if revision is not None and (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or revision > len(versions)
        ):
            raise KnowledgeLibraryError("revision_not_found")
        position = len(versions) - 1 if revision is None else revision - 1
        doc = self._read_version(versions[position])
        previous = self._read_version(versions[position - 1]) if position else None
        if doc.get("previous_sha256") != (previous["content_sha256"] if previous else None):
            raise KnowledgeLibraryError("revision_chain_broken")
        return doc

    def _with_freshness(
        self,
        doc: dict[str, Any],
        *,
        latest: dict[str, dict[str, Any]] | None = None,
        cache: dict[tuple[str, int], dict[str, Any]] | None = None,
        trail: tuple[str, ...] = (),
        budget: list[int] | None = None,
    ) -> dict[str, Any]:
        # Read-only dependency projection: immutable notes are never rewritten.
        latest = {} if latest is None else latest
        cache = {} if cache is None else cache
        budget = [1024] if budget is None else budget
        identifier = doc["knowledge_id"]
        key = (identifier, doc["revision"])
        if key in cache:
            return cache[key]
        reasons: list[str] = []
        canonical: dict[str, dict[str, Any]] = {}
        if identifier not in latest:
            latest[identifier] = self._read_raw(identifier)
        current = latest[identifier]
        if current["revision"] != doc["revision"]:
            reasons.append("研究或摘要已有更新版本")
        derived = doc["kind"] != "research"
        if derived:
            if doc.get("derivation_policy_version") != DERIVATION_POLICY_VERSION:
                reasons.append("结构整理策略已更新，等待重新综合")
            refs = doc.get("knowledge_refs", [])
            if not refs:
                reasons.append("缺少研究来源版本")
            if identifier in trail or len(trail) >= 32:
                reasons.append("来源依赖循环或层数超限")
            else:
                for ref in refs:
                    budget[0] -= 1
                    if budget[0] < 0:
                        reasons.append("来源依赖检查数量超限")
                        break
                    try:
                        source = self._read_raw(ref["knowledge_id"], ref["revision"])
                        checked = self._with_freshness(
                            source,
                            latest=latest,
                            cache=cache,
                            trail=(*trail, identifier),
                            budget=budget,
                        )
                        if checked["freshness"]["status"] != "current":
                            reasons.append(
                                f"来源 {ref['knowledge_id']}@{ref['revision']} 已修订或未复核"
                            )
                        current_source = latest[ref["knowledge_id"]]
                        if current_source["kind"] == "research":
                            authoritative = self._with_freshness(
                                current_source, latest=latest, cache=cache
                            )
                            canonical[ref["knowledge_id"]] = {
                                key: authoritative.get(key)
                                for key in (
                                    "knowledge_id",
                                    "revision",
                                    "title",
                                    "path",
                                    "freshness",
                                    "usable_as_current",
                                    "conclusion_authority",
                                    "claims_semantics",
                                    "result",
                                    "independent_review",
                                )
                            }
                        else:
                            canonical.update(
                                {
                                    item["knowledge_id"]: item
                                    for item in checked["canonical_research"]
                                }
                            )
                    except (ProductReviewError, KeyError, TypeError):
                        reasons.append("来源版本无法读取或校验")
        reviewed = bool(doc.get("independent_review"))
        state = "stale" if reasons else "unreviewed" if not derived and not reviewed else "current"
        if state == "unreviewed":
            reasons.append("尚未完成独立证据复核；不能作为已核验事实")
        projected = {
            **doc,
            "freshness": {
                "status": state,
                "reasons": list(dict.fromkeys(reasons)),
                "latest_revision": current["revision"],
            },
            "usable_as_current": not derived and state == "current",
            "conclusion_authority": "canonical_research" if derived else "research_result",
            "canonical_research": list(canonical.values()),
            "claims_semantics": "evaluated_propositions",
        }
        cache[key] = projected
        return projected

    def read(self, knowledge_id: str, revision: int | None = None) -> dict[str, Any]:
        return self._with_freshness(self._read_raw(knowledge_id, revision))

    def validate_publication(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a checkpoint receipt against its immutable note; never repair user edits."""
        identifier = _text(receipt.get("knowledge_id"), "knowledge_id", 100)
        revision = receipt.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise KnowledgeLibraryError("publication_revision_invalid")
        doc = self.read(identifier, revision)
        if receipt.get("path") != doc["path"]:
            raise KnowledgeLibraryError("publication_path_mismatch")
        for field in ("content_sha256", "run_id", "fragment_id"):
            if field in receipt and receipt[field] != doc.get(field):
                raise KnowledgeLibraryError("publication_binding_mismatch")
        return doc

    def history(self, knowledge_id: str) -> list[dict[str, Any]]:
        """Full immutable versions, including evidence and the reason for each revision."""
        return [self.read(knowledge_id, int(path.name)) for path in self._versions(knowledge_id)]

    def _scan(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        documents: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        for path in sorted(self.root.iterdir()):
            if not _ID.fullmatch(path.name) or path.name.startswith("topic-"):
                continue
            versions: list[Path] = []
            try:
                versions = self._versions(path.name)
                if versions:
                    documents.append(self._read_raw(path.name))
            except ProductReviewError as error:
                issues.append(
                    {
                        "knowledge_id": path.name,
                        "reason": str(error),
                        "path": str((versions[-1] / "note.md").relative_to(self.vault_root))
                        if versions
                        else None,
                    }
                )
        latest = {doc["knowledge_id"]: doc for doc in documents}
        cache: dict[tuple[str, int], dict[str, Any]] = {}
        return [self._with_freshness(doc, latest=latest, cache=cache) for doc in documents], issues

    def _all(self) -> list[dict[str, Any]]:
        return self._scan()[0]

    def read_issues(self) -> list[dict[str, Any]]:
        """Per-note integrity failures; healthy topics remain readable and writable."""
        return self._scan()[1]

    def catalog(self) -> list[dict[str, Any]]:
        return self._group_topics(self._all())

    @staticmethod
    def _group_topics(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        topics: dict[str, dict[str, Any]] = {}
        for doc in documents:
            if doc.get("summary_scope"):
                continue  # A period scope is not another knowledge topic.
            topic = doc["topic"]
            entry = topics.setdefault(topic["topic_id"], {**topic, "notes": []})
            entry["notes"].append(
                {
                    key: doc[key]
                    for key in (
                        "knowledge_id",
                        "revision",
                        "title",
                        "path",
                        "updated_at",
                        "kind",
                        "freshness",
                        "usable_as_current",
                        "conclusion_authority",
                    )
                }
            )
        return sorted(
            topics.values(), key=lambda row: (row["category"], row["subcategory"], row["title"])
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        topic_id: str | None = None,
        include_unusable: bool = False,
    ) -> list[dict[str, Any]]:
        query = _text(query, "query", 1000).casefold()
        if not isinstance(include_unusable, bool):
            raise KnowledgeLibraryError("include_unusable_invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise KnowledgeLibraryError("limit_invalid")
        # ponytail: full-text substring search is transparent at this personal
        # library scale; it is not semantic retrieval or a confidence score.
        matches = [
            doc
            for doc in self._all()
            if (
                (topic_id is None or doc.get("topic", {}).get("topic_id") == topic_id)
                and (include_unusable or doc["usable_as_current"])
                and query in self._render(doc).decode().casefold()
            )
        ]
        return sorted(matches, key=lambda doc: doc["updated_at"], reverse=True)[:limit]

    def _topic(self, value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            raise KnowledgeLibraryError("topic_required")
        existing_id = value.get("existing_topic_id", value.get("topic_id"))
        if existing_id:
            for existing in self.catalog():
                if existing["topic_id"] == existing_id:
                    return {
                        key: existing[key]
                        for key in ("topic_id", "category", "subcategory", "title")
                    }
            raise KnowledgeLibraryError("existing_topic_not_found")
        topic = {
            key: _text(value.get(key), f"topic_{key}", 120)
            for key in ("category", "subcategory", "title")
        }
        for text in topic.values():
            _markdown(text)  # Validate before commit; the index renders topic fields later.
        normalized = [unicodedata.normalize("NFKC", text).casefold() for text in topic.values()]
        topic["topic_id"] = "topic-" + _digest(normalized)[:24]
        return topic

    def save_research(
        self,
        *,
        run_id: str,
        fragment_id: str,
        title: str,
        result: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        status: str = "synthesized",
        expected_revision: int | None = None,
        changed_at: str | None = None,
        revision_reason: str = "研究结论自动沉淀",
        independent_review: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"synthesized", "conflicted"}:
            raise KnowledgeLibraryError("synthesis_required")
        records = _evidence(evidence)
        validated = _result(result, records)
        review: dict[str, Any] | None = None
        if independent_review is not None:
            if (
                set(independent_review) - {
                    "output_normalization", "source_output_sha256", "consistency_contract",
                    "claim_normalization",
                }
                != {
                    "policy_version",
                    "draft_digest",
                    "reviewed_result_digest",
                    "reviewed_at",
                    "verdict",
                    "reason",
                    "findings",
                }
                or independent_review.get("policy_version") != "independent-evidence-review-v1"
            ):
                raise KnowledgeLibraryError("independent_review_invalid")
            if ("consistency_contract" in independent_review
                    and independent_review["consistency_contract"] != "claims-consistency-v1"):
                raise KnowledgeLibraryError("independent_review_consistency_contract_invalid")
            if {"output_normalization", "source_output_sha256"} & set(independent_review):
                if (
                    independent_review.get("output_normalization")
                    not in {"closed_final_object", "opened_array_object"}
                    or not isinstance(independent_review.get("source_output_sha256"), str)
                    or not _SHA.fullmatch(str(independent_review["source_output_sha256"]))
                ):
                    raise KnowledgeLibraryError("independent_review_normalization_invalid")
            if "claim_normalization" in independent_review:
                normalization = independent_review["claim_normalization"]
                if (not isinstance(normalization, dict) or set(normalization) != {
                    "kind", "discarded_uncited_claims", "source_review_digest"
                } or normalization.get("kind") != "uncited_confirmed_to_unknowns_v1"
                    or not isinstance(normalization.get("source_review_digest"), str)
                    or not _SHA.fullmatch(normalization["source_review_digest"])):
                    raise KnowledgeLibraryError("independent_review_claim_normalization_invalid")
                discarded = _strings(normalization["discarded_uncited_claims"],
                                     "discarded_uncited_claims", 24000)
                if not discarded or len(discarded) > 24 or any(
                    "未附证据引用，未作为已确认事实：" + claim not in result.get("unknowns", [])
                    or any(item["claim"] == claim for item in result.get("confirmed", []))
                    for claim in discarded
                ):
                    raise KnowledgeLibraryError("independent_review_claim_normalization_invalid")
            if independent_review.get("reviewed_result_digest") != _digest(result):
                raise KnowledgeLibraryError("independent_review_result_mismatch")
            if not isinstance(independent_review.get("draft_digest"), str) or not _SHA.fullmatch(
                str(independent_review["draft_digest"])
            ):
                raise KnowledgeLibraryError("independent_review_digest_invalid")
            _time(independent_review.get("reviewed_at"))
            if independent_review.get("verdict") not in {
                "supported_with_limits",
                "revised",
                "insufficient_evidence",
            }:
                raise KnowledgeLibraryError("independent_review_verdict_invalid")
            _text(independent_review.get("reason"), "independent_review_reason", 3000)
            findings = independent_review.get("findings")
            if not isinstance(findings, list) or len(findings) > 24:
                raise KnowledgeLibraryError("independent_review_findings_invalid")
            ids = {item["evidence_id"] for item in records}
            for item in findings:
                if not isinstance(item, dict) or set(item) != {
                    "statement",
                    "issue",
                    "correction",
                    "evidence_ids",
                }:
                    raise KnowledgeLibraryError("independent_review_finding_invalid")
                for key in ("statement", "issue", "correction"):
                    _text(item.get(key), key, 3000)
                refs = _strings(item.get("evidence_ids"), "independent_review_evidence_ids")
                if not set(refs) <= ids:
                    raise KnowledgeLibraryError("independent_review_evidence_not_closed")
            review = json.loads(_json(independent_review))
        with self._locked():
            content = {
                "kind": "research",
                "run_id": _text(run_id, "run_id", 1000),
                "fragment_id": _text(fragment_id, "fragment_id", 1000),
                "title": _text(title, "title", 1000),
                "topic": self._topic(result.get("topic")),
                "result": validated,
                "evidence": records,
                "content_status": "qualified_conclusion"
                if not validated["conflicts"]
                else "conflicted",
                "publication_source": "system_policy",
                "human_reviewed": False,
                **({"independent_review": review} if review is not None else {}),
            }
            return self._commit(
                "knowledge-" + _digest(run_id)[:24],
                content,
                expected_revision,
                changed_at,
                revision_reason,
            )

    def _commit(
        self,
        identifier: str,
        content: dict[str, Any],
        expected_revision: int | None,
        changed_at: str | None,
        revision_reason: str,
    ) -> dict[str, Any]:
        versions = self._versions(identifier)
        latest = self._read_version(versions[-1]) if versions else None
        payload_digest = _digest(content)
        if latest and latest["payload_sha256"] == payload_digest:
            self._write_index()
            return {**latest, "idempotent": True}
        current_revision = len(versions)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision != current_revision
        ):
            raise KnowledgeLibraryError("revision_conflict")
        if latest is not None and expected_revision is None:
            raise KnowledgeLibraryError("expected_revision_required")
        updated = _time(changed_at or datetime.now(UTC).isoformat()).isoformat()
        if latest and _time(updated) < _time(latest["updated_at"]):
            raise KnowledgeLibraryError("revision_time_regression")
        revision = current_revision + 1
        directory = self._directory(self.root / identifier)
        destination = directory / f"{revision:08d}"
        doc = {
            **content,
            "schema_version": "loop-knowledge-v1",
            "knowledge_id": identifier,
            "revision": revision,
            "previous_sha256": latest["content_sha256"] if latest else None,
            "created_at": latest["created_at"] if latest else updated,
            "updated_at": updated,
            "revision_reason": _text(revision_reason, "revision_reason", 3000),
            "payload_sha256": payload_digest,
            "path": str((destination / "note.md").relative_to(self.vault_root)),
        }
        doc["content_sha256"] = _digest(doc)
        markdown = self._render(doc)
        doc["note_sha256"] = hashlib.sha256(markdown).hexdigest()
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=directory))
        try:
            _atomic_write(staging / "record.json", _json(doc))
            _atomic_write(staging / "note.md", markdown)
            staging_fd = os.open(staging, os.O_RDONLY)
            try:
                os.fsync(staging_fd)
            finally:
                os.close(staging_fd)
            os.rename(staging, destination)
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        self._write_index()
        return {**doc, "idempotent": False}

    def _render(self, doc: Mapping[str, Any]) -> bytes:
        fields = [
            (key, json.loads(_json(doc[key])))
            for key in (
                "title",
                "knowledge_id",
                "revision",
                "publication_source",
                "human_reviewed",
                "updated_at",
                "content_sha256",
                "topic",
                "summary_scope",
                "preview",
                "content_status",
            )
            if key in doc
        ]
        result = doc["result"]
        lines = [
            f"# {_markdown(doc['title'])}",
            "",
            *(["验收预览；非已到期自然周期，仅包含预览时已保存的指定真实材料。", ""]
              if doc.get("preview") else []),
            *([f"预览材料快照时间：{doc['updated_at']}（含明确时区）。", ""]
              if doc.get("preview") else []),
            *(
                ["结构整理（未经独立事实复核）；实际结论及限制以所引用的当前研究原文为准。", ""]
                if doc["kind"] != "research"
                else []
            ),
            *(["本次仅使用有界研究摘要组织结构；所有研究身份均保留，未读完整正文与全部限制。", ""]
              if doc.get("input_summary_only") else []),
            "## 核心结论" if doc["kind"] == "research" else "## 结构摘要",
            "",
            _markdown(result["summary"]),
            "",
            "## 建议",
            "",
            _markdown(result["recommendation"]),
            "",
        ]
        if result.get("answer_markdown"):
            lines.extend(["## 详细依据", "", result["answer_markdown"], ""])
        lines.extend(["## 已有依据", ""])
        for item in result.get("confirmed", []):
            lines.append(f"- {_markdown(item['claim'])}（{', '.join(item['evidence_ids'])}）")
        if result.get("conflicts"):
            lines.extend(["", "## 证据冲突", ""])
            for item in result["conflicts"]:
                dimensions = "；".join(item["dimensions"])
                lines.append(f"- {_markdown(item['topic'])}：{_markdown(dimensions)}")
        lines.extend(["", "## 本题尚未解决的问题", ""])
        unknowns = list(dict.fromkeys(result.get("unknowns", [])))
        lines.extend(f"- {_markdown(item)}" for item in unknowns)
        if not unknowns:
            lines.append("本次未列出影响本题结论的未解问题；不代表穷尽所有相关问题。")
        lines.extend(["", "## 已知限制", ""])
        limitations = list(dict.fromkeys(result.get("limitations", [])))
        lines.extend(f"- {_markdown(item)}" for item in limitations)
        if not limitations:
            lines.append("本次综合未单列已知限制；不代表适用于所有情况。")
        review = doc.get("independent_review")
        if isinstance(review, dict):
            lines.extend(
                [
                    "",
                    "## 独立证据复核",
                    "",
                    f"复核时间：{_markdown(review['reviewed_at'])}",
                    _markdown(review["reason"]),
                    "本轮复核依据已取得材料；缺少的现实验证仍以正文限制为准。",
                ]
            )
            if review.get("output_normalization") in {"closed_final_object", "opened_array_object"}:
                lines.append(
                    "输出格式记录：仅补齐一个缺失结构括号；未补写字段或事实，原始回执摘要保留。"
                )
            for finding in review["findings"]:
                lines.append(
                    f"- 原断言：{_markdown(finding['statement'])}；"
                    f"调整：{_markdown(finding['correction'])}"
                )
        lines.extend(
            [
                "",
                "## 供 Agent 继续使用",
                "",
                _markdown(result.get("agent_usage", {}).get("when_to_use", "")),
            ]
        )
        lines.extend(
            f"- {_markdown(item)}" for item in result.get("agent_usage", {}).get("steps", [])
        )
        analysis = doc.get("analysis", {})
        for field, heading in (
            ("known_structure", "已有知识结构"),
            ("connections", "证据支持的关联"),
        ):
            if analysis.get(field):
                lines.extend(["", f"## {heading}", ""])
                for item in analysis[field]:
                    refs = ", ".join(
                        f"{ref['knowledge_id']}@{ref['revision']}" for ref in item["knowledge_refs"]
                    )
                    lines.append(f"- {_markdown(item['statement'])}（{refs}）")
        if analysis.get("revisions"):
            lines.extend(["", "## 本次修订", ""])
            for item in analysis["revisions"]:
                lines.append(f"- 原判断：{_markdown(item['previous_statement'])}")
                lines.append(
                    f"  新判断：{_markdown(item['updated_statement'])}；"
                    f"依据：{_markdown(item['reason'])}"
                )
        lines.extend(["", "## 来源与版本", ""])
        for record in doc.get("evidence", []):
            lines.append(f"- **{_markdown(record['title'])}** · {record['evidence_id']}")
            source = record.get("url") or record.get("source_ref", "")
            lines.append(f"  来源：{_markdown(source)} · 证据摘要：`{record['digest']}`")
            if record["source_type"] == "repository_trial":
                lines.append(
                    f"  试跑：`{_markdown(record['command'])}` · "
                    f"版本 {_markdown(record['revision'])} · 退出码 {record['exit_code']}"
                )
            lines.extend(f"  > {_markdown(text)}" for text in record["excerpts"])
        if doc.get("knowledge_refs"):
            lines.extend(["", "## 本期依据", ""])
            lines.extend(
                f"- {ref['knowledge_id']} · 版本 {ref['revision']}" for ref in doc["knowledge_refs"]
            )
        lines.extend(
            [
                "",
                f"版本 {doc['revision']} · {_markdown(doc['revision_reason'])}",
                "自动保存身份：system_policy；未声称人工审核。",
                "",
            ]
        )
        markdown = _frontmatter(fields) + "\n".join(lines)
        _reject_active_inline_code(markdown)
        return markdown.encode()

    def _write_index(self) -> None:
        rows = ["# 研究知识目录", "", "> 自动生成的目录投影；历史研究原文与各版本保留。", ""]
        category = subcategory = None
        documents, issues = self._scan()
        overviews = [doc for doc in documents if doc.get("summary_scope")]
        if overviews:
            rows.extend(["## 周/月总览（汇总范围，不计入知识主题）", ""])
            for doc in sorted(overviews, key=lambda item: item["updated_at"], reverse=True):
                relative = os.path.relpath(self.vault_root / doc["path"], self.root)
                label = _markdown(doc["title"]).replace("[", "\\[").replace("]", "\\]")
                state = (
                    "已过期，勿作当前结论" if doc["freshness"]["status"] == "stale"
                    else "结构整理，结论以原研究为准"
                )
                rows.append(f"- [{label}](<{relative}>) · v{doc['revision']} · {state}")
            rows.append("")
        for topic in self._group_topics(documents):
            if topic["category"] != category:
                category, subcategory = topic["category"], None
                rows.extend([f"## {_markdown(category)}", ""])
            if topic["subcategory"] != subcategory:
                subcategory = topic["subcategory"]
                rows.extend([f"### {_markdown(subcategory)}", ""])
            rows.append(f"#### {_markdown(topic['title'])} · `{topic['topic_id']}`")
            for note in topic["notes"]:
                relative = os.path.relpath(self.vault_root / note["path"], self.root)
                label = _markdown(note["title"]).replace("[", "\\[").replace("]", "\\]")
                rows.append(
                    f"- [{label}](<{relative}>) · v{note['revision']} · {note['updated_at']} · "
                    + (
                        "已过期，勿作当前结论"
                        if note["freshness"]["status"] == "stale"
                        else "尚未独立复核"
                        if note["freshness"]["status"] == "unreviewed"
                        else "结构整理，结论以原研究为准"
                        if note["kind"] != "research"
                        else "已复核，保留正文限制"
                    )
                )
            rows.append("")
        if issues:
            rows.extend(
                ["## 无法读取的既有记录", "", "原始文件保留；这些记录没有被计为可用知识。", ""]
            )
            rows.extend(f"- `{item['knowledge_id']}`：{item['reason']}" for item in issues)
        _atomic_write(self.root / "目录.md", "\n".join(rows).encode())

    def rebuild_index(self) -> str:
        with self._locked():
            self._write_index()
        return str((self.root / "目录.md").relative_to(self.vault_root))

    def period_input(self, *, start: str, end: str) -> dict[str, Any]:
        first, last = _time(start), _time(end)
        if first >= last:
            raise KnowledgeLibraryError("period_invalid")
        # Keep period membership, but use current corrections to its research.
        selected: list[dict[str, Any]] = []
        for latest in self._all():
            if latest["kind"] != "research":
                continue
            revisions = [
                self._read_version(path) for path in self._versions(latest["knowledge_id"])
            ]
            eligible = [doc for doc in revisions if first <= _time(doc["updated_at"]) < last]
            if eligible:
                selected.append({**latest, "period_membership_revision": eligible[-1]["revision"]})
        return {
            "start": first.isoformat(),
            "end": last.isoformat(),
            "notes": [doc for doc in selected if doc["usable_as_current"]],
            "excluded_notes": [
                {"knowledge_id": doc["knowledge_id"], "freshness": doc["freshness"]}
                for doc in selected
                if not doc["usable_as_current"]
            ],
            "existing_topics": self.catalog(),
            "semantic_summary_generated": False,
        }

    def period_summaries(self, *, include_previews: bool = False) -> list[dict[str, Any]]:
        """Cross-topic period overviews, separate from the knowledge taxonomy."""
        if not isinstance(include_previews, bool):
            raise KnowledgeLibraryError("include_previews_invalid")
        return [doc for doc in self._all() if doc.get("summary_scope")
                and (include_previews or not doc.get("preview"))]

    def save_period_summary(
        self,
        *,
        period: str,
        start: str,
        end: str,
        topic_id: str | None = None,
        title: str,
        summary: str,
        knowledge_refs: list[dict[str, Any]],
        gaps: list[str],
        analysis: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        changed_at: str | None = None,
        preview: bool = False,
        summary_only: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(summary_only, bool):
            raise KnowledgeLibraryError("summary_only_invalid")
        if not isinstance(preview, bool):
            raise KnowledgeLibraryError("preview_invalid")
        if period not in {"week", "month"}:
            raise KnowledgeLibraryError("period_kind_invalid")
        first, last = _time(start), _time(end)
        if first >= last or not knowledge_refs:
            raise KnowledgeLibraryError("period_invalid")
        with self._locked():
            topic = self._topic({"existing_topic_id": topic_id}) if topic_id is not None else None
            carried_goals = self.unresolved_goals(
                topic_id=topic_id, include_previews=preview
            )
            seen: set[str] = set()
            for ref in knowledge_refs:
                identifier, revision = ref.get("knowledge_id"), ref.get("revision")
                if not isinstance(identifier, str) or not isinstance(revision, int):
                    raise KnowledgeLibraryError("knowledge_ref_invalid")
                doc = self.read(identifier, revision)
                if doc["kind"] != "research" or (
                    topic_id is not None and doc["topic"]["topic_id"] != topic_id
                ):
                    raise KnowledgeLibraryError("period_topic_mismatch")
                if (
                    not any(
                        first <= _time(version["updated_at"]) < last
                        for version in self.history(identifier)
                    )
                    or identifier in seen
                ):
                    raise KnowledgeLibraryError("period_reference_invalid")
                if not doc["usable_as_current"]:
                    raise KnowledgeLibraryError("summary_source_not_current")
                seen.add(identifier)
            content = {
                "kind": "period",
                "period": period,
                "start": first.isoformat(),
                "end": last.isoformat(),
                "title": _text(title, "title", 1000),
                **({"topic": topic} if topic is not None else {
                    "summary_scope": {"scope_id": "library", "title": "全部当前已复核研究"}
                }),
                **({"preview": True} if preview else {}),
                "knowledge_refs": knowledge_refs,
                "evidence": [],
                "result": {
                    "summary": _text(summary, "summary"),
                    "recommendation": "缺口列入目标；未选择优先级时自然收集，不自动主动研究。",
                    "unknowns": _strings(gaps, "gaps"),
                    "limitations": [],
                },
                "content_status": "theme_summary",
                "input_summary_only": summary_only,
                "derivation_policy_version": DERIVATION_POLICY_VERSION,
                "conclusion_authority": "canonical_research",
                "publication_source": "system_policy",
                "human_reviewed": False,
                "proactive_research_authorized": False,
                "analysis": dict(analysis or {}),
                "goals": self._goals(topic_id or "library", gaps, analysis, carried_goals),
            }
            return self._commit(
                "period-" + _digest(
                    (["preview"] if preview else [])
                    + [period, first.isoformat(), last.isoformat(), topic_id or "library"]
                )[:24],
                content,
                expected_revision,
                changed_at,
                "验收预览；非已到期自然周期" if preview else "周/月知识归纳；保留依据版本",
            )

    def unresolved_goals(
        self, *, topic_id: str | None = None, include_previews: bool = False
    ) -> list[dict[str, Any]]:
        """A missing mention never closes an earlier goal; no priority is granted."""
        goals: dict[str, dict[str, Any]] = {}
        for doc in sorted(self._all(), key=lambda item: (item["updated_at"], item["revision"])):
            if doc.get("preview") and not include_previews:
                continue
            if topic_id is not None and doc.get("topic", {}).get("topic_id") != topic_id:
                continue
            for goal in doc.get("goals", []):
                projected = dict(goal)
                if "knowledge_refs" not in projected:
                    matching = next((gap for gap in doc.get("analysis", {}).get("gaps", [])
                                     if gap.get("goal") == goal["goal"]), None)
                    if matching:
                        projected["knowledge_refs"] = matching["knowledge_refs"]
                goals[goal["goal_id"]] = projected
        return list(goals.values())

    @staticmethod
    def _goals(
        topic_id: str, gaps: list[str], analysis: Mapping[str, Any] | None = None,
        carried_goals: Sequence[Mapping[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        goals = {item["goal_id"]: dict(item) for item in carried_goals}
        gap_items = list((analysis or {}).get("gaps", []))
        if gap_items and [item.get("goal") for item in gap_items] != gaps:
            raise KnowledgeLibraryError("goal_analysis_mismatch")
        reused: set[str] = set()
        for index, goal in enumerate(gaps):
            existing_id = gap_items[index].get("existing_goal_id") if gap_items else None
            if existing_id is not None:
                if existing_id not in goals or existing_id in reused:
                    raise KnowledgeLibraryError("existing_goal_invalid")
                reused.add(existing_id)
                goals[existing_id] = {**goals[existing_id], "goal": goal,
                                      "knowledge_refs": gap_items[index]["knowledge_refs"]}
                continue
            # Literal equality is deterministic; semantic identity must cite an existing ID.
            if any(item["goal"] == goal for item in goals.values()):
                continue
            identifier = "goal-" + _digest([topic_id, goal])[:24]
            goals[identifier] = {
                "goal_id": identifier, "goal": goal,
                "collection_mode": "natural_collection", "priority": None,
                "proactive_research_authorized": False,
                **({"knowledge_refs": gap_items[index]["knowledge_refs"]} if gap_items else {}),
            }
        return list(goals.values())

    def save_topic_summary(
        self,
        *,
        topic_id: str,
        title: str,
        summary: str,
        knowledge_refs: list[dict[str, Any]],
        gaps: list[str],
        analysis: Mapping[str, Any],
        expected_revision: int | None = None,
        changed_at: str | None = None,
        summary_only: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(summary_only, bool):
            raise KnowledgeLibraryError("summary_only_invalid")
        with self._locked():
            topic = self._topic({"existing_topic_id": topic_id})
            carried_goals = self.unresolved_goals(topic_id=topic_id)
            if not knowledge_refs:
                raise KnowledgeLibraryError("knowledge_refs_required")
            seen: set[str] = set()
            for ref in knowledge_refs:
                doc = self.read(ref["knowledge_id"], ref["revision"])
                if doc["kind"] != "research" or doc["topic"]["topic_id"] != topic_id:
                    raise KnowledgeLibraryError("summary_topic_mismatch")
                if doc["knowledge_id"] in seen:
                    raise KnowledgeLibraryError("duplicate_knowledge_ref")
                if not doc["usable_as_current"]:
                    raise KnowledgeLibraryError("summary_source_not_current")
                seen.add(doc["knowledge_id"])
            content = {
                "kind": "topic_summary",
                "title": _text(title, "title", 1000),
                "topic": topic,
                "knowledge_refs": knowledge_refs,
                "evidence": [],
                "result": {
                    "summary": _text(summary, "summary"),
                    "unknowns": _strings(gaps, "gaps"),
                    "limitations": [],
                    "recommendation": "沿已有主题自然积累；具体缺口等待优先级授权。",
                },
                "analysis": dict(analysis),
                "goals": self._goals(topic_id, gaps, analysis, carried_goals),
                "content_status": "theme_summary",
                "input_summary_only": summary_only,
                "derivation_policy_version": DERIVATION_POLICY_VERSION,
                "conclusion_authority": "canonical_research",
                "publication_source": "system_policy",
                "human_reviewed": False,
                "proactive_research_authorized": False,
            }
            return self._commit(
                "knowledge-" + _digest(["topic_summary", topic_id])[:24],
                content,
                expected_revision,
                changed_at,
                "新证据触发主题复核；原研究与此前主题结论保留",
            )

    def notifications(self, *, since: str | None = None) -> list[dict[str, Any]]:
        threshold = _time(since) if since is not None else None
        notifications = []
        for latest in self._all():
            if latest["kind"] not in {"topic_summary", "period", "research"}:
                continue
            for doc in self.history(latest["knowledge_id"]):
                if doc["kind"] == "research" and (
                    doc["revision"] < 2 or not doc.get("independent_review")
                ):
                    continue
                if threshold is not None and _time(doc["updated_at"]) <= threshold:
                    continue
                notifications.append(
                    {
                        "notification_id": f"{doc['knowledge_id']}:{doc['revision']}",
                        "knowledge_id": doc["knowledge_id"],
                        "kind": "research_revised"
                        if doc["kind"] == "research"
                        else "topic_revised"
                        if doc["revision"] > 1
                        else doc["kind"],
                        "title": doc["title"],
                        "message": doc["revision_reason"],
                        "path": doc["path"],
                        "revision": doc["revision"],
                        "updated_at": doc["updated_at"],
                        "revisions": doc.get("analysis", {}).get("revisions", []),
                    }
                )
        return sorted(notifications, key=lambda item: item["updated_at"], reverse=True)
