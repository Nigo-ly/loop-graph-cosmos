"""Trusted frontmatter intake for untrusted fragment bodies."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from common.supervisor import AdmissionState

URL_RE = re.compile(r"https?://[^\s)\]>]+")


@dataclass(frozen=True)
class FragmentEnvelope:
    fragment_id: str
    captured_at: str | None
    raw_content: str
    input_type: str
    source_hint: str | None
    user_note: str | None
    attachments: tuple[str, ...]
    privacy_level: str
    processing_status: str
    admission_state: AdmissionState
    source_path: str


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return {}, text
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip("\"'")
    return fields, "\n".join(lines[end + 1 :]).strip()


def _input_type(body: str) -> str:
    links = URL_RE.findall(body)
    if not links:
        return "text"
    remaining = URL_RE.sub("", body).strip()
    return "link" if not remaining else "mixed"


def load_fragment(path: str | Path) -> FragmentEnvelope:
    source = Path(path).expanduser().resolve()
    text = source.read_text(encoding="utf-8")
    fields, body = _split_frontmatter(text)
    requested = fields.get("nigo-loop") == "true"
    fragment_id = fields.get("fragment_id") or source.stem
    if not fragment_id:
        fragment_id = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    return FragmentEnvelope(
        fragment_id=fragment_id,
        captured_at=fields.get("captured_at"),
        raw_content=body,
        input_type=_input_type(body),
        source_hint=fields.get("source"),
        user_note=fields.get("user_note"),
        attachments=(),
        privacy_level=fields.get("privacy_level", "personal"),
        processing_status=fields.get("pipeline_status", "captured"),
        admission_state=(AdmissionState.REQUESTED if requested else AdmissionState.NOT_REQUESTED),
        source_path=str(source),
    )


def preflight_reasons(fragment: FragmentEnvelope) -> list[str]:
    reasons: list[str] = []
    if fragment.admission_state is not AdmissionState.REQUESTED:
        reasons.append("nigo-loop is not strictly true in frontmatter")
    if fragment.input_type not in {"link", "mixed"}:
        reasons.append("phone-fragment-link-v1 requires an http(s) link")
    if fragment.privacy_level in {"sensitive", "restricted"}:
        reasons.append("sensitive or restricted fragments require manual handling")
    return reasons


MAX_FRAGMENT_TITLE_LENGTH = 80

# Lines that are never meaningful user-authored subjects: raw URLs and the
# control marker. Timestamp filenames are never promoted either (the file
# stem is no longer a fallback).
_CONTROL_MARKER_RE = re.compile(r"^\s*nigo-loop\s*:?\s*(true)?\s*$", re.IGNORECASE)


def _truncate_title(title: str) -> str:
    if len(title) > MAX_FRAGMENT_TITLE_LENGTH:
        return title[: MAX_FRAGMENT_TITLE_LENGTH - 1].rstrip() + "…"
    return title


def _user_text_in_line(line: str) -> str:
    """User-authored text remaining after stripping URLs and markup."""
    stripped = line.strip().lstrip("#").strip()
    if not stripped or _CONTROL_MARKER_RE.match(stripped):
        return ""
    without_urls = URL_RE.sub("", stripped)
    cleaned = " ".join(without_urls.replace("·", " ").split())
    return cleaned.strip(" -:—|·")


def fragment_title_for(fragment: FragmentEnvelope) -> tuple[str, str]:
    """Derive (title, provenance) for the per-fragment subject.

    Meaningful user text only: `user_note` first, then the first body line
    with user-authored text after URLs, the `nigo-loop` marker, and heading
    marks are ignored. Link-only records yield ("", "") — no semantic
    subject exists until Content Acquisition persists a verified page
    title. Nothing is ever synthesized.
    """
    if fragment.user_note:
        title = " ".join(fragment.user_note.split())
        if title:
            return _truncate_title(title), "user_note"
    for line in fragment.raw_content.splitlines():
        text = _user_text_in_line(line)
        if text:
            return _truncate_title(text), "user_text"
    return "", ""
