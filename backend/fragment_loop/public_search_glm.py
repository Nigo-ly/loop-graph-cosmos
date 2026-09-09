"""Loop V1 Package A: minimal versioned GLM Web Search adapter.

The vendor name lives only in this adapter; the R1-P retrieval core stays
vendor-agnostic.  The adapter accepts one R1-P public retrieval request,
freezes exactly one official Web Search API request (V1: exactly one query,
one search transport call per research run), and maps the official response
into the R1-P public retrieval response.  Discovery snippets
(``search_result[].content``) are only retrieval excerpts — they can never
become source-text evidence.

Frozen official protocol baseline (this package does not read credentials
and never builds an Authorization header; a future independent credential
boundary owns that):

- endpoint ``https://open.bigmodel.cn/api/paas/v4/web_search``, POST JSON;
- frozen fields only: ``search_query``, ``search_engine="search_std"``,
  ``search_intent=false``, ``count`` (1-5, frozen default 5),
  ``search_recency_filter="noLimit"``, and the internal ``request_id``;
- never sends user_id, private metadata, or raw fragment text;
- consumes only ``title``, ``content``, ``link``, ``media``,
  ``publish_date`` from each ``search_result[]`` item; unknown or extra
  fields never enter persistent evidence.

Every failure — transport exception, non-200 HTTP status, invalid JSON,
invalid fields, more results than the frozen count, or a locator that fails
the R1-P HTTPS rule — fails closed with no retry and no fallback.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping

from fragment_loop.cognitive_retrieval import (
    QUERY_KEYS,
    REQUEST_KEYS,
    is_public_https_locator,
)

GLM_SEARCH_PROFILE_VERSION = "glm-web-search-package-a-v1"
GLM_SEARCH_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/web_search"
GLM_SEARCH_ENGINE = "search_std"
GLM_SEARCH_RECENCY_FILTER = "noLimit"
GLM_SEARCH_COUNT = 5
GLM_FROZEN_REQUEST_FIELDS = (
    "search_query",
    "search_engine",
    "search_intent",
    "count",
    "search_recency_filter",
    "request_id",
)

# One fixed selfcheck vector; --selfcheck verifies only this frozen
# profile/request encoding and never touches a transport, file, or ledger.
GLM_SELFCHECK_REQUEST: dict[str, object] = {
    "request_id": "public-glm-search-selfcheck-v1",
    "queries": [{"query_id": "Q1", "question": "GLM 官方搜索冻结自检问题？"}],
    "search_dimensions": ["冻结自检维度"],
}

SearchTransport = Callable[[Mapping[str, object]], Mapping[str, object]]
"""One injected search transport, called at most once per adapter call.

It receives exactly ``{"endpoint", "method", "profile_version",
"request_id", "body_bytes", "body_sha256"}`` and must return exactly
``{"http_status", "body_bytes"}``.  Any deviation fails closed.
"""

TRANSPORT_REQUEST_KEYS = {
    "endpoint",
    "method",
    "profile_version",
    "request_id",
    "body_bytes",
    "body_sha256",
}
TRANSPORT_RESPONSE_KEYS = {"http_status", "body_bytes"}
_SEARCH_RESULT_ITEM_TEXT_KEYS = {"title", "content", "link", "media", "publish_date"}


class GlmSearchAdapterError(ValueError):
    """The GLM search step failed closed before or after one transport call."""


def _is_nonempty_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def canonical_request_json(frozen_request: Mapping[str, object]) -> str:
    """Encode one frozen request deterministically (sorted keys, UTF-8)."""
    return json.dumps(
        dict(frozen_request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_glm_search_request(request: Mapping[str, object]) -> dict[str, object]:
    """Freeze one official V1 request from one R1-P public request.

    V1 accepts exactly one query; anything else is rejected here, before
    any transport descriptor is built.
    """
    if set(request) != REQUEST_KEYS:
        raise GlmSearchAdapterError("glm_search_request_fields_invalid")
    request_id = request.get("request_id")
    if not _is_nonempty_text(request_id, 256) or not str(request_id).startswith("public-"):
        raise GlmSearchAdapterError("glm_search_request_id_invalid")
    queries = request.get("queries")
    if not isinstance(queries, list) or len(queries) != 1:
        raise GlmSearchAdapterError("glm_search_requires_exactly_one_query")
    query = queries[0]
    if not isinstance(query, Mapping) or set(query) != QUERY_KEYS:
        raise GlmSearchAdapterError("glm_search_query_invalid")
    question = query.get("question")
    if not _is_nonempty_text(question, 4096):
        raise GlmSearchAdapterError("glm_search_query_invalid")
    return {
        "search_query": str(question),
        "search_engine": GLM_SEARCH_ENGINE,
        "search_intent": False,
        "count": GLM_SEARCH_COUNT,
        "search_recency_filter": GLM_SEARCH_RECENCY_FILTER,
        "request_id": str(request_id),
    }


def build_transport_request(frozen_request: Mapping[str, object]) -> dict[str, object]:
    """Build the exact descriptor the injected transport is called with."""
    if set(frozen_request) != set(GLM_FROZEN_REQUEST_FIELDS):
        raise GlmSearchAdapterError("glm_search_frozen_fields_invalid")
    body = canonical_request_json(frozen_request).encode("utf-8")
    return {
        "endpoint": GLM_SEARCH_ENDPOINT,
        "method": "POST",
        "profile_version": GLM_SEARCH_PROFILE_VERSION,
        "request_id": str(frozen_request["request_id"]),
        "body_bytes": body,
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def _validated_transport_response(raw: object) -> bytes:
    if not isinstance(raw, Mapping) or set(raw) != TRANSPORT_RESPONSE_KEYS:
        raise GlmSearchAdapterError("glm_search_transport_response_invalid")
    http_status = raw.get("http_status")
    body = raw.get("body_bytes")
    if (
        not isinstance(http_status, int)
        or isinstance(http_status, bool)
        or not isinstance(body, (bytes, bytearray))
    ):
        raise GlmSearchAdapterError("glm_search_transport_response_invalid")
    if http_status != 200:
        raise GlmSearchAdapterError("glm_search_http_status_not_200")
    return bytes(body)


def _parse_official_response(body: bytes) -> list[Mapping[str, object]]:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GlmSearchAdapterError("glm_search_response_invalid_json") from None
    if not isinstance(parsed, dict):
        raise GlmSearchAdapterError("glm_search_response_invalid_json")
    # Only ``search_result`` is consumed; any other official field (id,
    # created, usage, error payloads, future additions) never enters
    # persistent evidence.
    results = parsed.get("search_result")
    if not isinstance(results, list):
        raise GlmSearchAdapterError("glm_search_results_invalid")
    if len(results) > GLM_SEARCH_COUNT:
        raise GlmSearchAdapterError("glm_search_results_exceeded")
    items: list[Mapping[str, object]] = []
    for item in results:
        if not isinstance(item, Mapping):
            raise GlmSearchAdapterError("glm_search_result_item_invalid")
        consumed: dict[str, object] = {}
        for key in _SEARCH_RESULT_ITEM_TEXT_KEYS:
            value = item.get(key)
            if value is not None and not isinstance(value, str):
                raise GlmSearchAdapterError("glm_search_result_item_invalid")
            consumed[key] = value
        if (
            not _is_nonempty_text(consumed["title"], 1024)
            or not _is_nonempty_text(consumed["content"], 16_384)
            or not is_public_https_locator(consumed["link"])
        ):
            raise GlmSearchAdapterError("glm_search_result_item_invalid")
        items.append(consumed)
    return items


def glm_search_selfcheck() -> dict[str, object]:
    """Verify only the frozen profile/request vector; zero side effects."""
    frozen = build_glm_search_request(GLM_SELFCHECK_REQUEST)
    descriptor = build_transport_request(frozen)
    canonical = canonical_request_json(frozen)
    body = descriptor["body_bytes"]
    assert isinstance(body, bytes)
    return {
        "profile_version": GLM_SEARCH_PROFILE_VERSION,
        "endpoint": GLM_SEARCH_ENDPOINT,
        "frozen_fields": list(GLM_FROZEN_REQUEST_FIELDS),
        "frozen_request": frozen,
        "canonical_json": canonical,
        "body_byte_count": len(canonical.encode("utf-8")),
        "body_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "selfcheck_request": dict(GLM_SELFCHECK_REQUEST),
        "transport_calls": 0,
    }


def make_glm_search_adapter(
    transport: SearchTransport,
) -> Callable[[Mapping[str, object]], Mapping[str, object]]:
    """Bind one injected transport into an R1-P public retrieval adapter.

    The returned adapter freezes the official request, calls the transport
    exactly once, and maps the official response into the R1-P retrieval
    response with ``source_type_hint="unknown"`` — the adapter never guesses
    a source type; the later public verifier assigns the final
    ``source_type`` and the unknown hint never reaches a source card.
    """

    def adapter(request: Mapping[str, object]) -> Mapping[str, object]:
        frozen = build_glm_search_request(request)
        descriptor = build_transport_request(frozen)
        try:
            raw_response = transport(descriptor)
        except GlmSearchAdapterError:
            raise
        except Exception:
            raise GlmSearchAdapterError("glm_search_transport_failed") from None
        body = _validated_transport_response(raw_response)
        items = _parse_official_response(body)
        queries = request["queries"]
        assert isinstance(queries, list)
        query = queries[0]
        assert isinstance(query, Mapping)
        query_id = str(query["query_id"])
        materials: list[dict[str, object]] = []
        for index, item in enumerate(items):
            excerpt = str(item["content"])
            published = item.get("publish_date")
            materials.append(
                {
                    "retrieval_id": f"R{index + 1}",
                    "query_refs": [query_id],
                    "title": str(item["title"]),
                    "locator": str(item["link"]),
                    "published_at_claim": (
                        str(published)
                        if _is_nonempty_text(published, 128)
                        else "unknown"
                    ),
                    "source_type_hint": "unknown",
                    "acquisition_method": "public_search_adapter",
                    "excerpt": excerpt,
                    "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                }
            )
        return {
            "status": "complete" if materials else "not_found",
            "error_category": "none",
            "materials": materials,
        }

    return adapter
