"""Loop V1 Package A: public page fetch + source verification adapter.

One injected fetch transport performs at most one fetch per locator — no
retry, no redirect, no proxy, no automatic fallback.  The frozen future
production boundary is fixed here as constants and as the exact descriptor
every transport is called with: HTTPS only, DNS/peer IP double check
rejecting loopback/private/link-local/reserved/multicast/unspecified
addresses, no userinfo, no fragment, connect 10s, total 30s, response body
at most 1 MiB, only ``text/html`` / ``text/plain``.  Visible text is
extracted deterministically with the standard library (script/style and
similar elements are ignored); the full body is never persisted — only its
SHA-256 and a bounded evidence window chosen from the extracted text.

The injected source classifier is called exactly once per batch.  It only
*selects* an ``evidence_context`` from the actual extracted text and an
``evidence_excerpt`` inside it, plus the R1-R v2 assessment fields it is
allowed to judge.  This adapter recomputes every fact itself — context,
excerpt and content hashes, fetch time, HTTP status, final locator — and
never trusts the classifier for them.  ``authenticity`` is always
``unverified``: one HTTP 200 fetch with a self-consistent evidence window
proves nothing about publisher identity, domain ownership, page stability,
or claim truth.  Any single material failure fails the whole batch closed
before any classifier call.
"""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime
from html.parser import HTMLParser

from fragment_loop.cognitive_retrieval import is_public_https_locator

FETCH_PROFILE_VERSION = "public-source-fetch-package-a-v1"
CONNECT_TIMEOUT_SECONDS = 10
TOTAL_TIMEOUT_SECONDS = 30
MAX_BODY_BYTES = 1_048_576  # 1 MiB
ALLOWED_CONTENT_TYPES = ("text/html", "text/plain")
MAX_EVIDENCE_CONTEXT_CHARS = 16_384

FETCH_REQUEST_KEYS = {
    "locator",
    "connect_timeout_seconds",
    "total_timeout_seconds",
    "max_body_bytes",
    "allow_redirects",
    "allow_proxy",
}
FETCH_RESULT_KEYS = {
    "http_status",
    "content_type",
    "body_bytes",
    "final_locator",
    "peer_ip",
    "resolved_ips",
}
TRANSPORT_FACT_KEYS = {
    "http_status",
    "content_type",
    "final_locator",
    "peer_ip",
    "resolved_ips",
    "fetched_at",
    "body_sha256",
}
CLASSIFIER_INPUT_KEYS = {
    "retrieval_id",
    "locator",
    "title",
    "snippet",
    "canonical_text",
    "transport_facts",
}
SELECTION_KEYS = {
    "retrieval_id",
    "origin_id",
    "source_tier",
    "version",
    "relevance",
    "independence",
    "source_type",
    "published_at",
    "verification_basis",
    "scope",
    "evidence_context",
    "evidence_excerpt",
}
SOURCE_TIERS = ("primary", "secondary")
RELEVANCES = ("direct", "indirect")
INDEPENDENCES = ("independent", "non_independent")
SOURCE_TYPES = (
    "paper",
    "official_document",
    "industry_practice",
    "media_report",
    "dataset",
)
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})

FetchTransport = Callable[[Mapping[str, object]], Mapping[str, object]]
"""One injected page fetch; called at most once per locator.

It receives exactly the frozen ``FETCH_REQUEST_KEYS`` descriptor and must
return exactly ``FETCH_RESULT_KEYS``.  DNS resolution, TLS, timeouts, the
1 MiB cap, and the no-proxy/no-redirect rules belong to the future
production transport; this adapter re-checks every reported fact and fails
closed on any violation.
"""

SourceClassifier = Callable[[Mapping[str, object]], Mapping[str, object]]
"""One injected classifier call per batch: ``{"materials": [...]}`` in,
``{"selections": [...]}`` out.  It may only choose evidence windows from
the extracted text and fill the allowed assessment fields.
"""


class PublicFetchError(ValueError):
    """The fetch/verification step failed closed; the batch is aborted."""


def _is_text(value: object, maximum: int = 4096) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _is_public_ip(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _parse_content_type(value: object) -> str:
    if not isinstance(value, str):
        raise PublicFetchError("public_fetch_content_type_invalid")
    parts = value.split(";")
    media_type = parts[0].strip().lower()
    if media_type not in ALLOWED_CONTENT_TYPES:
        raise PublicFetchError("public_fetch_content_type_invalid")
    for parameter in parts[1:]:
        name, separator, raw = parameter.strip().partition("=")
        if separator and name.strip().lower() == "charset":
            if raw.strip().strip('"').lower() != "utf-8":
                raise PublicFetchError("public_fetch_charset_invalid")
    return media_type


class _VisibleTextExtractor(HTMLParser):
    """Deterministically collect visible text, ignoring script/style etc."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in _SKIPPED_TAGS:
            self._skip_depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag, attrs
        return

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIPPED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        normalized = " ".join(data.split())
        if normalized:
            self.chunks.append(normalized)


def extract_visible_text(media_type: str, text: str) -> str:
    """Extract the canonical visible text; ``text/plain`` passes through."""
    if media_type == "text/plain":
        return text
    extractor = _VisibleTextExtractor()
    try:
        extractor.feed(text)
        extractor.close()
    except Exception:
        raise PublicFetchError("public_fetch_text_extraction_failed") from None
    return "\n".join(extractor.chunks)


def build_fetch_request(locator: str) -> dict[str, object]:
    """Freeze the exact descriptor every fetch transport is called with."""
    if not is_public_https_locator(locator):
        raise PublicFetchError("public_fetch_locator_invalid")
    return {
        "locator": locator,
        "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
        "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
        "max_body_bytes": MAX_BODY_BYTES,
        "allow_redirects": False,
        "allow_proxy": False,
    }


def _validated_fetch_result(
    locator: str,
    raw: object,
    clock: Callable[[], datetime],
) -> dict[str, object]:
    """Re-check every transport fact; nothing the transport says is trusted."""
    if not isinstance(raw, Mapping) or set(raw) != FETCH_RESULT_KEYS:
        raise PublicFetchError("public_fetch_result_invalid")
    http_status = raw.get("http_status")
    if not isinstance(http_status, int) or isinstance(http_status, bool):
        raise PublicFetchError("public_fetch_result_invalid")
    if http_status != 200:
        raise PublicFetchError("public_fetch_http_status_not_200")
    if raw.get("final_locator") != locator:
        raise PublicFetchError("public_fetch_redirect_forbidden")
    media_type = _parse_content_type(raw.get("content_type"))
    body = raw.get("body_bytes")
    if not isinstance(body, (bytes, bytearray)):
        raise PublicFetchError("public_fetch_body_invalid")
    body_bytes = bytes(body)
    if len(body_bytes) > MAX_BODY_BYTES:
        raise PublicFetchError("public_fetch_body_too_large")
    try:
        decoded = body_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PublicFetchError("public_fetch_decode_failed") from None
    peer_ip = raw.get("peer_ip")
    resolved_ips = raw.get("resolved_ips")
    if (
        not _is_public_ip(peer_ip)
        or not isinstance(resolved_ips, list)
        or not resolved_ips
        or any(not _is_public_ip(item) for item in resolved_ips)
        or str(peer_ip) not in {str(item) for item in resolved_ips}
    ):
        raise PublicFetchError("public_fetch_ip_check_failed")
    fetched_at = clock()
    if (
        not isinstance(fetched_at, datetime)
        or fetched_at.tzinfo is None
        or fetched_at.tzinfo.utcoffset(fetched_at) is None
    ):
        raise PublicFetchError("public_fetch_clock_invalid")
    canonical_text = extract_visible_text(media_type, decoded)
    if not canonical_text.strip():
        raise PublicFetchError("public_fetch_no_visible_text")
    return {
        "canonical_text": canonical_text,
        "transport_facts": {
            "http_status": http_status,
            "content_type": media_type,
            "final_locator": locator,
            "peer_ip": str(peer_ip),
            "resolved_ips": [str(item) for item in resolved_ips],
            "fetched_at": fetched_at.isoformat(),
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        },
    }


def _validated_selections(
    raw: object,
    fetched: list[dict[str, object]],
) -> list[Mapping[str, object]]:
    if not isinstance(raw, Mapping) or set(raw) != {"selections"}:
        raise PublicFetchError("public_classifier_response_invalid")
    selections = raw.get("selections")
    if not isinstance(selections, list) or len(selections) != len(fetched):
        raise PublicFetchError("public_classifier_response_invalid")
    validated: list[Mapping[str, object]] = []
    for index, selection in enumerate(selections):
        material = fetched[index]
        if not isinstance(selection, Mapping) or set(selection) != SELECTION_KEYS:
            raise PublicFetchError("public_classifier_selection_invalid")
        if selection.get("retrieval_id") != material["retrieval_id"]:
            raise PublicFetchError("public_classifier_selection_invalid")
        context = selection.get("evidence_context")
        excerpt = selection.get("evidence_excerpt")
        canonical_text = str(material["canonical_text"])
        if (
            not _is_text(context, MAX_EVIDENCE_CONTEXT_CHARS)
            or not isinstance(context, str)
            or context not in canonical_text
        ):
            raise PublicFetchError("public_classifier_context_not_in_text")
        if (
            not _is_text(excerpt, MAX_EVIDENCE_CONTEXT_CHARS)
            or not isinstance(excerpt, str)
            or excerpt not in context
        ):
            raise PublicFetchError("public_classifier_excerpt_not_in_context")
        if (
            not _is_text(selection.get("origin_id"), 256)
            or selection.get("source_tier") not in SOURCE_TIERS
            or not _is_text(selection.get("version"), 512)
            or selection.get("relevance") not in RELEVANCES
            or selection.get("independence") not in INDEPENDENCES
            or selection.get("source_type") not in SOURCE_TYPES
            or not _is_text(selection.get("published_at"), 128)
            or not _is_text(selection.get("verification_basis"))
            or not _is_text(selection.get("scope"))
        ):
            raise PublicFetchError("public_classifier_selection_invalid")
        validated.append(selection)
    return validated


def make_public_fetch_verifier(
    fetch_transport: FetchTransport,
    classifier: SourceClassifier,
    *,
    clock: Callable[[], datetime],
) -> Callable[[Mapping[str, object]], Mapping[str, object]]:
    """Bind fetch transport + classifier into one R1-R source verifier.

    Each material is fetched exactly once; any material failure aborts the
    whole batch before the classifier runs.  The classifier is called
    exactly once with frozen locators, discovery snippets, extracted
    canonical text, and transport facts — never with raw bodies, retrieval
    internals, credentials, or transport objects.  The adapter itself
    recomputes every hash, timestamp, and status.
    """

    def verifier(retrieval_result: Mapping[str, object]) -> Mapping[str, object]:
        materials = retrieval_result.get("materials")
        if not isinstance(materials, list):
            raise PublicFetchError("public_fetch_materials_invalid")
        fetched: list[dict[str, object]] = []
        for material in materials:
            if not isinstance(material, Mapping):
                raise PublicFetchError("public_fetch_materials_invalid")
            locator = material.get("locator")
            retrieval_id = material.get("retrieval_id")
            title = material.get("title")
            snippet = material.get("excerpt")
            if (
                not isinstance(locator, str)
                or not _is_text(retrieval_id, 64)
                or not _is_text(title, 1024)
                or not _is_text(snippet, 16_384)
            ):
                raise PublicFetchError("public_fetch_materials_invalid")
            request = build_fetch_request(locator)
            try:
                raw_result = fetch_transport(request)
            except PublicFetchError:
                raise
            except Exception:
                raise PublicFetchError("public_fetch_transport_failed") from None
            checked = _validated_fetch_result(locator, raw_result, clock)
            fetched.append(
                {
                    "retrieval_id": str(retrieval_id),
                    "locator": locator,
                    "title": str(title),
                    "snippet": str(snippet),
                    "canonical_text": str(checked["canonical_text"]),
                    "transport_facts": checked["transport_facts"],
                }
            )
        try:
            raw_response = classifier({"materials": deepcopy(fetched)})
        except PublicFetchError:
            raise
        except Exception:
            raise PublicFetchError("public_classifier_failed") from None
        selections = _validated_selections(raw_response, fetched)
        assessments: list[dict[str, object]] = []
        for material, selection in zip(fetched, selections, strict=True):
            facts = material["transport_facts"]
            assert isinstance(facts, dict)
            context = str(selection["evidence_context"])
            excerpt = str(selection["evidence_excerpt"])
            assessments.append(
                {
                    "retrieval_id": material["retrieval_id"],
                    "origin_id": str(selection["origin_id"]),
                    "source_tier": str(selection["source_tier"]),
                    "version": str(selection["version"]),
                    "authenticity": "unverified",
                    "relevance": str(selection["relevance"]),
                    "independence": str(selection["independence"]),
                    "source_type": str(selection["source_type"]),
                    "published_at": str(selection["published_at"]),
                    "verification_basis": str(selection["verification_basis"]),
                    "scope": str(selection["scope"]),
                    "final_locator": material["locator"],
                    "http_status": facts["http_status"],
                    "fetched_at": facts["fetched_at"],
                    "source_content_sha256": facts["body_sha256"],
                    "evidence_excerpt": excerpt,
                    "evidence_excerpt_sha256": hashlib.sha256(
                        excerpt.encode()
                    ).hexdigest(),
                    "evidence_context": context,
                    "evidence_context_sha256": hashlib.sha256(
                        context.encode()
                    ).hexdigest(),
                }
            )
        return {"status": "complete", "error_category": "none", "assessments": assessments}

    return verifier
