"""Lexical sanitization for *_refs and source_file values.

Rules (contract v2 §"Refs path safety"):

- drop any value containing a NUL byte;
- map the absolute vault prefix ``/example/vault/`` to a
  vault-relative path by stripping the prefix;
- map the absolute artifacts prefix
  ``/Users/example/loop-engineering/artifacts/`` to ``artifacts/<run_id>/...``;
- the resulting (or already-relative) value is allowed only if it is
  relative (no leading ``/``), contains no ``..``/``.``/empty segments, and
  is either a vault-relative note path or starts with ``artifacts/<run_id>/``;
- everything else is dropped.

All checks are purely lexical; the filesystem is never touched.
"""

from __future__ import annotations

VAULT_ABSOLUTE_PREFIX = "/example/vault/"
ARTIFACTS_ABSOLUTE_PREFIX = "/Users/example/loop-engineering/artifacts/"
ARTIFACTS_RELATIVE_PREFIX = "artifacts/"


def _sanitize_one(ref: object) -> str | None:
    if not isinstance(ref, str):
        return None
    if "\x00" in ref:
        return None
    value = ref
    if value.startswith(VAULT_ABSOLUTE_PREFIX):
        value = value[len(VAULT_ABSOLUTE_PREFIX) :]
    elif value.startswith(ARTIFACTS_ABSOLUTE_PREFIX):
        value = ARTIFACTS_RELATIVE_PREFIX + value[len(ARTIFACTS_ABSOLUTE_PREFIX) :]
    if not value or value.startswith("/"):
        return None
    # Validate segments, tolerating a single trailing slash such as
    # "artifacts/<run_id>/".
    segments = value.rstrip("/").split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return None
    if segments[0] == "artifacts":
        # Must be artifacts/<run_id>/... — at least one segment below it.
        if len(segments) < 2:
            return None
        return value
    # Any other clean relative path is a vault-relative note path.
    return value


def sanitize_refs(refs: object) -> list[str]:
    """Return the allowed subset of refs, order preserved, dupes kept."""
    if not isinstance(refs, list):
        return []
    sanitized: list[str] = []
    for ref in refs:
        value = _sanitize_one(ref)
        if value is not None:
            sanitized.append(value)
    return sanitized
