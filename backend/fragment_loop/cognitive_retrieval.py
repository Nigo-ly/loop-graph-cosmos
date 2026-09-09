"""R1-I/R1-P vendor-agnostic retrieval boundaries for fragment cognition.

Retrieval is deliberately separated from source verification.  A validated
record only proves what the injected adapter returned for a frozen request;
it cannot declare authenticity, relevance, independence, or support for a
claim.  One minimal core serves two frozen profiles selected by binding
version: the R1-I synthetic fixture profile and the R1-P public adapter
profile.  The core knows no search vendor names.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import NamedTuple
from urllib.parse import urlsplit

RETRIEVAL_BINDING_VERSION = "fragment-cognitive-retrieval-r1i-v1"
PUBLIC_RETRIEVAL_BINDING_VERSION = "fragment-cognitive-retrieval-r1p-public-v1"
RetrievalAdapter = Callable[[Mapping[str, object]], Mapping[str, object]]

REQUEST_KEYS = {"request_id", "queries", "search_dimensions"}
QUERY_KEYS = {"query_id", "question"}
RESPONSE_KEYS = {"status", "error_category", "materials"}
MATERIAL_KEYS = {
    "retrieval_id",
    "query_refs",
    "title",
    "locator",
    "published_at_claim",
    "source_type_hint",
    "acquisition_method",
    "excerpt",
    "excerpt_sha256",
}
SOURCE_TYPE_HINTS = {
    "paper",
    "official_document",
    "industry_practice",
    "media_report",
    "dataset",
    "unknown",
}
RESULT_KEYS = {
    "contract_status",
    "retrieval_status",
    "verification_status",
    "error_category",
    "request",
    "materials",
    "retrieval_binding",
}
BINDING_KEYS = {
    "binding_version",
    "request_sha256",
    "materials_sha256",
    "material_count",
}


class CognitiveRetrievalError(ValueError):
    """The request itself is invalid and no adapter was called."""


class _RetrievalProfile(NamedTuple):
    """One frozen retrieval profile; the core never learns vendor names."""

    binding_version: str
    request_id_prefix: str
    acquisition_method: str
    locator_is_valid: Callable[[object], bool]


def _nonempty_text(value: object, *, maximum: int = 4096) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _is_synthetic_reference(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("synthetic/")
        and "://" not in value
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def _is_numeric_ipv4_label(label: str) -> bool:
    """Match one ASCII decimal or 0x hexadecimal IPv4 literal label."""
    if not label:
        return False
    if label.startswith(("0x", "0X")):
        try:
            int(label, 16)
        except ValueError:
            return False
        return True
    return all(char in "0123456789" for char in label)


def _is_public_https_locator(value: object) -> bool:
    """Accept only canonical HTTPS locators, offline and without DNS lookups."""
    if not _nonempty_text(value, maximum=2048):
        return False
    assert isinstance(value, str)
    if value != value.strip() or any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port  # accessing the port validates its syntax and range
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not value.startswith("https://")
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    host = parsed.hostname
    if not host:
        return False
    if "%" in host or "\\" in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return False
    if normalized.endswith(".local"):
        return False
    # Reject numeric IPv4 literal spellings that ipaddress does not parse:
    # a single decimal or 0x hexadecimal integer, or dotted labels that are
    # all decimal or 0x hexadecimal (abbreviated or mixed forms).
    if all(_is_numeric_ipv4_label(label) for label in normalized.split(".")):
        return False
    return True


def is_public_https_locator(value: object) -> bool:
    """Public alias of the frozen R1-P locator check for adapter reuse.

    Vendor adapters (search, fetch) must apply exactly the same offline
    canonical-HTTPS rule as the core profile instead of growing their own
    URL logic; passing it never upgrades authenticity or trust.
    """
    return _is_public_https_locator(value)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


_SYNTHETIC_PROFILE = _RetrievalProfile(
    binding_version=RETRIEVAL_BINDING_VERSION,
    request_id_prefix="synthetic-",
    acquisition_method="synthetic_fixture",
    locator_is_valid=_is_synthetic_reference,
)
_PUBLIC_PROFILE = _RetrievalProfile(
    binding_version=PUBLIC_RETRIEVAL_BINDING_VERSION,
    request_id_prefix="public-",
    acquisition_method="public_search_adapter",
    locator_is_valid=_is_public_https_locator,
)
_PROFILES = {
    profile.binding_version: profile
    for profile in (_SYNTHETIC_PROFILE, _PUBLIC_PROFILE)
}


def _validate_request(
    request: Mapping[str, object],
    profile: _RetrievalProfile,
) -> tuple[str, ...]:
    errors: list[str] = []
    if set(request) != REQUEST_KEYS:
        errors.append("request_fields_invalid")
    request_id = request.get("request_id")
    if not _nonempty_text(request_id, maximum=256) or not str(request_id).startswith(
        profile.request_id_prefix
    ):
        errors.append("request_id_invalid")
    queries = request.get("queries")
    query_ids: list[str] = []
    if not isinstance(queries, list) or not 1 <= len(queries) <= 10:
        errors.append("queries_invalid")
    else:
        for query in queries:
            if not isinstance(query, Mapping) or set(query) != QUERY_KEYS:
                errors.append("query_invalid")
                continue
            query_id = query.get("query_id")
            if not _nonempty_text(query_id, maximum=64) or not _nonempty_text(
                query.get("question")
            ):
                errors.append("query_invalid")
                continue
            query_ids.append(str(query_id))
        if len(query_ids) != len(set(query_ids)):
            errors.append("query_ids_duplicate")
    dimensions = request.get("search_dimensions")
    if (
        not isinstance(dimensions, list)
        or not 1 <= len(dimensions) <= 10
        or any(not _nonempty_text(item, maximum=512) for item in dimensions)
    ):
        errors.append("search_dimensions_invalid")
    return tuple(errors)


def _validated_materials(
    response: Mapping[str, object],
    query_ids: set[str],
    profile: _RetrievalProfile,
) -> tuple[str, list[dict[str, object]]]:
    if set(response) != RESPONSE_KEYS:
        raise ValueError("response_fields_invalid")
    status = response.get("status")
    if status not in {"complete", "not_found"} or response.get("error_category") != "none":
        raise ValueError("response_status_invalid")
    raw_materials = response.get("materials")
    if not isinstance(raw_materials, list) or len(raw_materials) > 20:
        raise ValueError("materials_invalid")
    if (status == "complete" and not raw_materials) or (
        status == "not_found" and raw_materials
    ):
        raise ValueError("materials_status_mismatch")
    materials: list[dict[str, object]] = []
    retrieval_ids: list[str] = []
    locators: list[str] = []
    for raw in raw_materials:
        if not isinstance(raw, Mapping) or set(raw) != MATERIAL_KEYS:
            raise ValueError("material_fields_invalid")
        retrieval_id = raw.get("retrieval_id")
        query_refs = raw.get("query_refs")
        excerpt = raw.get("excerpt")
        excerpt_sha256 = raw.get("excerpt_sha256")
        if (
            not _nonempty_text(retrieval_id, maximum=64)
            or not isinstance(query_refs, list)
            or not query_refs
            or any(not isinstance(ref, str) or ref not in query_ids for ref in query_refs)
            or len(set(query_refs)) != len(query_refs)
            or not _nonempty_text(raw.get("title"), maximum=1024)
            or not profile.locator_is_valid(raw.get("locator"))
            or not _nonempty_text(raw.get("published_at_claim"), maximum=128)
            or raw.get("source_type_hint") not in SOURCE_TYPE_HINTS
            or raw.get("acquisition_method") != profile.acquisition_method
            or not _nonempty_text(excerpt, maximum=16_384)
            or not isinstance(excerpt_sha256, str)
            or excerpt_sha256 != hashlib.sha256(str(excerpt).encode()).hexdigest()
        ):
            raise ValueError("material_invalid")
        retrieval_ids.append(str(retrieval_id))
        locators.append(str(raw["locator"]))
        materials.append(deepcopy(dict(raw)))
    if len(retrieval_ids) != len(set(retrieval_ids)):
        raise ValueError("retrieval_ids_duplicate")
    if len(locators) != len(set(locators)):
        raise ValueError("locators_duplicate")
    return str(status), materials


def _failed_result(
    request: Mapping[str, object],
    category: str,
    profile: _RetrievalProfile,
) -> dict[str, object]:
    materials: list[dict[str, object]] = []
    return {
        "contract_status": "rejected",
        "retrieval_status": "failed_safe",
        "verification_status": "not_started",
        "error_category": category,
        "request": deepcopy(dict(request)),
        "materials": materials,
        "retrieval_binding": {
            "binding_version": profile.binding_version,
            "request_sha256": _canonical_sha256(request),
            "materials_sha256": _canonical_sha256(materials),
            "material_count": 0,
        },
    }


def validate_retrieval_result(result: Mapping[str, object]) -> tuple[str, ...]:
    """Recompute a persisted retrieval result without calling its adapter.

    The frozen profile is selected from the persisted binding version; an
    unknown version or a cross-profile splice is rejected.
    """
    if set(result) != RESULT_KEYS:
        return ("retrieval_result_fields_invalid",)
    request = result.get("request")
    materials = result.get("materials")
    binding = result.get("retrieval_binding")
    if not isinstance(materials, list) or not isinstance(binding, Mapping):
        return ("retrieval_result_shape_invalid",)
    if set(binding) != BINDING_KEYS:
        return ("retrieval_binding_fields_invalid",)
    profile = _PROFILES.get(str(binding.get("binding_version")))
    if profile is None:
        return ("retrieval_binding_mismatch",)
    if not isinstance(request, Mapping) or _validate_request(request, profile):
        return ("retrieval_request_invalid",)
    if (
        binding.get("request_sha256") != _canonical_sha256(request)
        or binding.get("materials_sha256") != _canonical_sha256(materials)
        or binding.get("material_count") != len(materials)
    ):
        return ("retrieval_binding_mismatch",)
    contract_status = result.get("contract_status")
    retrieval_status = result.get("retrieval_status")
    verification_status = result.get("verification_status")
    error_category = result.get("error_category")
    if contract_status == "rejected":
        if (
            retrieval_status != "failed_safe"
            or verification_status != "not_started"
            or error_category
            not in {"retrieval_adapter_failed", "retrieval_result_invalid"}
            or materials
        ):
            return ("retrieval_failure_state_invalid",)
        return ()
    expected_verification = (
        "pending" if retrieval_status == "complete" else "not_applicable"
    )
    if (
        contract_status != "validated"
        or retrieval_status not in {"complete", "not_found"}
        or verification_status != expected_verification
        or error_category != "none"
    ):
        return ("retrieval_success_state_invalid",)
    queries = request.get("queries")
    assert isinstance(queries, list)
    query_ids = {
        str(query["query_id"])
        for query in queries
        if isinstance(query, dict)
    }
    try:
        _validated_materials(
            {
                "status": retrieval_status,
                "error_category": error_category,
                "materials": materials,
            },
            query_ids,
            profile,
        )
    except (TypeError, ValueError):
        return ("retrieval_materials_invalid",)
    return ()


def _run_retrieval(
    request: Mapping[str, object],
    adapter: RetrievalAdapter,
    profile: _RetrievalProfile,
) -> dict[str, object]:
    """Call one injected adapter once and bind its unverified material records."""
    request_copy = deepcopy(dict(request))
    request_errors = _validate_request(request_copy, profile)
    if request_errors:
        raise CognitiveRetrievalError(request_errors[0])
    raw_queries = request_copy["queries"]
    assert isinstance(raw_queries, list)
    query_ids = {
        str(query["query_id"])
        for query in raw_queries
        if isinstance(query, dict)
    }
    try:
        response = adapter(deepcopy(request_copy))
    except Exception:
        return _failed_result(request_copy, "retrieval_adapter_failed", profile)
    try:
        retrieval_status, materials = _validated_materials(response, query_ids, profile)
    except (TypeError, ValueError):
        return _failed_result(request_copy, "retrieval_result_invalid", profile)
    return {
        "contract_status": "validated",
        "retrieval_status": retrieval_status,
        "verification_status": (
            "pending" if retrieval_status == "complete" else "not_applicable"
        ),
        "error_category": "none",
        "request": deepcopy(request_copy),
        "materials": materials,
        "retrieval_binding": {
            "binding_version": profile.binding_version,
            "request_sha256": _canonical_sha256(request_copy),
            "materials_sha256": _canonical_sha256(materials),
            "material_count": len(materials),
        },
    }


def run_synthetic_retrieval(
    request: Mapping[str, object],
    adapter: RetrievalAdapter,
) -> dict[str, object]:
    """R1-I synthetic profile: call one injected adapter once."""
    return _run_retrieval(request, adapter, _SYNTHETIC_PROFILE)


def run_public_retrieval(
    request: Mapping[str, object],
    adapter: RetrievalAdapter,
) -> dict[str, object]:
    """R1-P public profile: vendor-agnostic, one injected adapter call.

    The public locator remains an unverified locator; passing the HTTPS
    shape check never upgrades authenticity, relevance, independence, or
    support for any claim.
    """
    return _run_retrieval(request, adapter, _PUBLIC_PROFILE)
