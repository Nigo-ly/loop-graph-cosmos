"""R1-J synthetic and R1-R public source verification, separated from claims.

The verifier assesses acquired material and produces source cards.  It cannot
change the retrieved excerpt or locator, and its response has no claim verdict,
confidence, or evidence-relation fields.

The R1-J synthetic v1 boundary is byte-compatible: same API, fields, hashes,
error codes, and fixed vectors.  The R1-R public v2 boundary is separate: a
public source card's ``evidence_excerpt`` may only come from the independent
verifier's fetch/check record for the actual locator — never from the
retrieval discovery snippet, which stays inside ``retrieval_result`` only.
The verifier keeps a bounded ``evidence_context`` window (at most 16,384
characters) that must contain the excerpt; both window and excerpt hashes are
exactly recomputable from the persisted text.  ``source_content_sha256`` is
only the verifier-recorded, persistently bound digest of the full response
body — the body itself is not persisted, so that digest cannot be
independently recomputed from the persisted evidence and never proves page
content.  At this stage a public assessment's ``authenticity`` is always
``unverified``: one HTTP 200 fetch record with a self-consistent evidence
window proves nothing about publisher identity, domain ownership, page
stability, or claim truth.  Each runner accepts exactly its own retrieval
binding profile; synthetic and public results can never impersonate each
other.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime

from fragment_loop.cognitive_retrieval import (
    PUBLIC_RETRIEVAL_BINDING_VERSION,
    RETRIEVAL_BINDING_VERSION,
    validate_retrieval_result,
)

VERIFICATION_BINDING_VERSION = "fragment-cognitive-source-verification-r1j-v1"
PUBLIC_VERIFICATION_BINDING_VERSION = "fragment-cognitive-source-verification-r1r-public-v2"
SourceVerifier = Callable[[Mapping[str, object]], Mapping[str, object]]

RESPONSE_KEYS = {"status", "error_category", "assessments"}
ASSESSMENT_KEYS = {
    "retrieval_id",
    "origin_id",
    "source_tier",
    "version",
    "authenticity",
    "relevance",
    "independence",
    "source_type",
    "published_at",
    "verification_basis",
    "scope",
}
SOURCE_TIERS = {"primary", "secondary"}
AUTHENTICITIES = {"verified", "unverified"}
RELEVANCES = {"direct", "indirect"}
INDEPENDENCES = {"independent", "non_independent"}
SOURCE_TYPES = {
    "paper",
    "official_document",
    "industry_practice",
    "media_report",
    "dataset",
}
PUBLIC_ASSESSMENT_EXTRA_KEYS = {
    "final_locator",
    "http_status",
    "fetched_at",
    "source_content_sha256",
    "evidence_excerpt",
    "evidence_excerpt_sha256",
    "evidence_context",
    "evidence_context_sha256",
}
PUBLIC_ASSESSMENT_KEYS = ASSESSMENT_KEYS | PUBLIC_ASSESSMENT_EXTRA_KEYS
RESULT_KEYS = {
    "contract_status",
    "verification_status",
    "error_category",
    "retrieval_result",
    "assessments",
    "source_cards",
    "verification_binding",
}
BINDING_KEYS = {
    "binding_version",
    "retrieval_sha256",
    "assessments_sha256",
    "source_cards_sha256",
    "source_count",
}


class CognitiveSourceVerificationError(ValueError):
    """The retrieval input cannot enter source verification."""


def _text(value: object, maximum: int = 4096) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_tz_aware_iso8601(value: object) -> bool:
    if not _text(value, 64):
        return False
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.tzinfo.utcoffset(parsed) is not None


def _validated_assessments(
    response: Mapping[str, object],
    retrieval_ids: list[str],
) -> list[dict[str, object]]:
    if (
        set(response) != RESPONSE_KEYS
        or response.get("status") != "complete"
        or response.get("error_category") != "none"
    ):
        raise ValueError("verification_response_invalid")
    raw_assessments = response.get("assessments")
    if not isinstance(raw_assessments, list) or len(raw_assessments) != len(
        retrieval_ids
    ):
        raise ValueError("verification_assessments_invalid")
    assessments: list[dict[str, object]] = []
    for index, raw in enumerate(raw_assessments):
        if not isinstance(raw, Mapping) or set(raw) != ASSESSMENT_KEYS:
            raise ValueError("verification_assessment_fields_invalid")
        if (
            raw.get("retrieval_id") != retrieval_ids[index]
            or not _text(raw.get("origin_id"), 256)
            or raw.get("source_tier") not in SOURCE_TIERS
            or not _text(raw.get("version"), 512)
            or raw.get("authenticity") not in AUTHENTICITIES
            or raw.get("relevance") not in RELEVANCES
            or raw.get("independence") not in INDEPENDENCES
            or raw.get("source_type") not in SOURCE_TYPES
            or not _text(raw.get("published_at"), 128)
            or not _text(raw.get("verification_basis"))
            or not _text(raw.get("scope"))
        ):
            raise ValueError("verification_assessment_invalid")
        assessments.append(deepcopy(dict(raw)))
    independent_origins = [
        str(item["origin_id"])
        for item in assessments
        if item["independence"] == "independent"
    ]
    if len(independent_origins) != len(set(independent_origins)):
        raise ValueError("independent_origins_duplicate")
    return assessments


def _validated_public_assessments(
    response: Mapping[str, object],
    retrieval_ids: list[str],
    materials: list[object],
) -> list[dict[str, object]]:
    """Validate one R1-R public response, including its fetch/check record.

    The excerpt must sit inside the bounded ``evidence_context`` window and
    both hashes must recompute exactly from the persisted text.
    ``source_content_sha256`` is only checked for shape: it is the verifier's
    record of the full response body, which is not persisted and therefore
    cannot be recomputed here.  ``authenticity`` must stay ``unverified``.
    """
    if (
        set(response) != RESPONSE_KEYS
        or response.get("status") != "complete"
        or response.get("error_category") != "none"
    ):
        raise ValueError("verification_response_invalid")
    raw_assessments = response.get("assessments")
    if not isinstance(raw_assessments, list) or len(raw_assessments) != len(
        retrieval_ids
    ):
        raise ValueError("verification_assessments_invalid")
    assessments: list[dict[str, object]] = []
    for index, raw in enumerate(raw_assessments):
        if not isinstance(raw, Mapping) or set(raw) != PUBLIC_ASSESSMENT_KEYS:
            raise ValueError("verification_assessment_fields_invalid")
        material = materials[index]
        if not isinstance(material, Mapping):
            raise ValueError("retrieval_material_invalid")
        http_status = raw.get("http_status")
        evidence_excerpt = raw.get("evidence_excerpt")
        evidence_context = raw.get("evidence_context")
        if (
            raw.get("retrieval_id") != retrieval_ids[index]
            or not _text(raw.get("origin_id"), 256)
            or raw.get("source_tier") not in SOURCE_TIERS
            or not _text(raw.get("version"), 512)
            or raw.get("authenticity") != "unverified"
            or raw.get("relevance") not in RELEVANCES
            or raw.get("independence") not in INDEPENDENCES
            or raw.get("source_type") not in SOURCE_TYPES
            or not _text(raw.get("published_at"), 128)
            or not _text(raw.get("verification_basis"))
            or not _text(raw.get("scope"))
            or raw.get("final_locator") != material.get("locator")
            or not isinstance(http_status, int)
            or isinstance(http_status, bool)
            or http_status != 200
            or not _is_tz_aware_iso8601(raw.get("fetched_at"))
            or not _is_sha256_hex(raw.get("source_content_sha256"))
            or not _text(evidence_excerpt, 16_384)
            or raw.get("evidence_excerpt_sha256")
            != hashlib.sha256(str(evidence_excerpt).encode()).hexdigest()
            or not _text(evidence_context, 16_384)
            or raw.get("evidence_context_sha256")
            != hashlib.sha256(str(evidence_context).encode()).hexdigest()
        ):
            raise ValueError("verification_assessment_invalid")
        assert isinstance(evidence_excerpt, str)
        assert isinstance(evidence_context, str)
        if evidence_excerpt not in evidence_context:
            raise ValueError("verification_assessment_invalid")
        assessments.append(deepcopy(dict(raw)))
    independent_origins = [
        str(item["origin_id"])
        for item in assessments
        if item["independence"] == "independent"
    ]
    if len(independent_origins) != len(set(independent_origins)):
        raise ValueError("independent_origins_duplicate")
    return assessments


def _failed_result(
    retrieval: Mapping[str, object],
    category: str,
    binding_version: str = VERIFICATION_BINDING_VERSION,
) -> dict[str, object]:
    assessments: list[dict[str, object]] = []
    cards: list[dict[str, object]] = []
    return {
        "contract_status": "rejected",
        "verification_status": "failed_safe",
        "error_category": category,
        "retrieval_result": deepcopy(dict(retrieval)),
        "assessments": assessments,
        "source_cards": cards,
        "verification_binding": {
            "binding_version": binding_version,
            "retrieval_sha256": _canonical_sha256(retrieval),
            "assessments_sha256": _canonical_sha256(assessments),
            "source_cards_sha256": _canonical_sha256(cards),
            "source_count": 0,
        },
    }


def _build_cards(
    materials: list[object],
    assessments: list[dict[str, object]],
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for material, assessment in zip(materials, assessments, strict=True):
        if not isinstance(material, dict):
            raise ValueError("retrieval_material_invalid")
        cards.append(
            {
                "source_id": material["retrieval_id"],
                "name": material["title"],
                "origin_id": assessment["origin_id"],
                "source_tier": assessment["source_tier"],
                "version": assessment["version"],
                "authenticity": assessment["authenticity"],
                "relevance": assessment["relevance"],
                "independence": assessment["independence"],
                "source_type": assessment["source_type"],
                "published_at": assessment["published_at"],
                "locator": material["locator"],
                "acquisition_method": material["acquisition_method"],
                "verification_basis": assessment["verification_basis"],
                "evidence_excerpt": material["excerpt"],
                "excerpt_sha256": material["excerpt_sha256"],
                "scope": assessment["scope"],
            }
        )
    return cards


def _build_public_cards(
    materials: list[object],
    assessments: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Build R1-R public cards; evidence comes only from the assessment."""
    cards: list[dict[str, object]] = []
    for material, assessment in zip(materials, assessments, strict=True):
        if not isinstance(material, dict):
            raise ValueError("retrieval_material_invalid")
        cards.append(
            {
                "source_id": material["retrieval_id"],
                "name": material["title"],
                "origin_id": assessment["origin_id"],
                "source_tier": assessment["source_tier"],
                "version": assessment["version"],
                "authenticity": assessment["authenticity"],
                "relevance": assessment["relevance"],
                "independence": assessment["independence"],
                "source_type": assessment["source_type"],
                "published_at": assessment["published_at"],
                "locator": material["locator"],
                "acquisition_method": material["acquisition_method"],
                "verification_basis": assessment["verification_basis"],
                "evidence_excerpt": assessment["evidence_excerpt"],
                "excerpt_sha256": assessment["evidence_excerpt_sha256"],
                "scope": assessment["scope"],
            }
        )
    return cards


def _retrieval_profile_mismatch(retrieval: Mapping[str, object], expected: str) -> bool:
    binding = retrieval.get("retrieval_binding")
    return not isinstance(binding, Mapping) or binding.get("binding_version") != expected


def validate_source_verification_result(
    result: Mapping[str, object],
) -> tuple[str, ...]:
    """Recompute persisted cards and binding without calling a verifier.

    Dispatches on the persisted binding version: the R1-J synthetic v1 rules
    stay byte-compatible, while an R1-R public v2 result is recomputed by the
    public rules.  Neither profile accepts the other's persisted result.
    """
    binding = result.get("verification_binding")
    if (
        isinstance(binding, Mapping)
        and binding.get("binding_version") == PUBLIC_VERIFICATION_BINDING_VERSION
    ):
        return validate_public_source_verification_result(result)
    return _validate_synthetic_source_verification_result(result)


def _validate_synthetic_source_verification_result(
    result: Mapping[str, object],
) -> tuple[str, ...]:
    """Recompute persisted R1-J cards and binding without calling a verifier."""
    if set(result) != RESULT_KEYS:
        return ("source_verification_fields_invalid",)
    retrieval = result.get("retrieval_result")
    assessments = result.get("assessments")
    cards = result.get("source_cards")
    binding = result.get("verification_binding")
    if (
        not isinstance(retrieval, Mapping)
        or validate_retrieval_result(retrieval)
        or not isinstance(assessments, list)
        or not isinstance(cards, list)
        or not isinstance(binding, Mapping)
    ):
        return ("source_verification_shape_invalid",)
    if (
        retrieval.get("contract_status") != "validated"
        or retrieval.get("retrieval_status") != "complete"
        or retrieval.get("verification_status") != "pending"
    ):
        return ("source_verification_retrieval_state_invalid",)
    if _retrieval_profile_mismatch(retrieval, RETRIEVAL_BINDING_VERSION):
        return ("source_verification_profile_mismatch",)
    if set(binding) != BINDING_KEYS:
        return ("source_verification_binding_fields_invalid",)
    if (
        binding.get("binding_version") != VERIFICATION_BINDING_VERSION
        or binding.get("retrieval_sha256") != _canonical_sha256(retrieval)
        or binding.get("assessments_sha256") != _canonical_sha256(assessments)
        or binding.get("source_cards_sha256") != _canonical_sha256(cards)
        or binding.get("source_count") != len(cards)
    ):
        return ("source_verification_binding_mismatch",)
    if result.get("contract_status") == "rejected":
        if (
            result.get("verification_status") != "failed_safe"
            or result.get("error_category")
            not in {"source_verifier_failed", "source_verification_result_invalid"}
            or assessments
            or cards
        ):
            return ("source_verification_failure_state_invalid",)
        return ()
    if (
        result.get("contract_status") != "validated"
        or result.get("verification_status") != "complete"
        or result.get("error_category") != "none"
    ):
        return ("source_verification_success_state_invalid",)
    materials = retrieval.get("materials")
    if not isinstance(materials, list):
        return ("source_verification_materials_invalid",)
    retrieval_ids = [
        str(material["retrieval_id"])
        for material in materials
        if isinstance(material, dict)
    ]
    try:
        validated = _validated_assessments(
            {"status": "complete", "error_category": "none", "assessments": assessments},
            retrieval_ids,
        )
        expected_cards = _build_cards(materials, validated)
    except (KeyError, TypeError, ValueError):
        return ("source_verification_content_invalid",)
    if cards != expected_cards:
        return ("source_verification_cards_mismatch",)
    return ()


def validate_public_source_verification_result(
    result: Mapping[str, object],
) -> tuple[str, ...]:
    """Recompute persisted R1-R public cards and binding offline.

    Every locator, status, fetch time, evidence window, excerpt, both
    recomputable hashes, card, and binding field is recomputed; any drift is
    rejected.  ``source_content_sha256`` is the verifier-recorded digest of
    the unpersisted full response body: it is persistently bound and
    shape-checked, but cannot be independently recomputed from the persisted
    evidence and is never treated as cryptographic proof of page content.
    """
    if set(result) != RESULT_KEYS:
        return ("source_verification_fields_invalid",)
    retrieval = result.get("retrieval_result")
    assessments = result.get("assessments")
    cards = result.get("source_cards")
    binding = result.get("verification_binding")
    if (
        not isinstance(retrieval, Mapping)
        or validate_retrieval_result(retrieval)
        or not isinstance(assessments, list)
        or not isinstance(cards, list)
        or not isinstance(binding, Mapping)
    ):
        return ("source_verification_shape_invalid",)
    if (
        retrieval.get("contract_status") != "validated"
        or retrieval.get("retrieval_status") != "complete"
        or retrieval.get("verification_status") != "pending"
    ):
        return ("source_verification_retrieval_state_invalid",)
    if _retrieval_profile_mismatch(retrieval, PUBLIC_RETRIEVAL_BINDING_VERSION):
        return ("source_verification_profile_mismatch",)
    if set(binding) != BINDING_KEYS:
        return ("source_verification_binding_fields_invalid",)
    if (
        binding.get("binding_version") != PUBLIC_VERIFICATION_BINDING_VERSION
        or binding.get("retrieval_sha256") != _canonical_sha256(retrieval)
        or binding.get("assessments_sha256") != _canonical_sha256(assessments)
        or binding.get("source_cards_sha256") != _canonical_sha256(cards)
        or binding.get("source_count") != len(cards)
    ):
        return ("source_verification_binding_mismatch",)
    if result.get("contract_status") == "rejected":
        if (
            result.get("verification_status") != "failed_safe"
            or result.get("error_category")
            not in {"source_verifier_failed", "source_verification_result_invalid"}
            or assessments
            or cards
        ):
            return ("source_verification_failure_state_invalid",)
        return ()
    if (
        result.get("contract_status") != "validated"
        or result.get("verification_status") != "complete"
        or result.get("error_category") != "none"
    ):
        return ("source_verification_success_state_invalid",)
    materials = retrieval.get("materials")
    if not isinstance(materials, list):
        return ("source_verification_materials_invalid",)
    retrieval_ids = [
        str(material["retrieval_id"])
        for material in materials
        if isinstance(material, dict)
    ]
    try:
        validated = _validated_public_assessments(
            {"status": "complete", "error_category": "none", "assessments": assessments},
            retrieval_ids,
            materials,
        )
        expected_cards = _build_public_cards(materials, validated)
    except (KeyError, TypeError, ValueError):
        return ("source_verification_content_invalid",)
    if cards != expected_cards:
        return ("source_verification_cards_mismatch",)
    return ()


def run_synthetic_source_verification(
    retrieval_result: Mapping[str, object],
    verifier: SourceVerifier,
) -> dict[str, object]:
    """Verify one valid synthetic retrieval batch with one injected call."""
    retrieval = deepcopy(dict(retrieval_result))
    if validate_retrieval_result(retrieval):
        raise CognitiveSourceVerificationError("retrieval_result_invalid")
    if (
        retrieval.get("contract_status") != "validated"
        or retrieval.get("retrieval_status") != "complete"
        or retrieval.get("verification_status") != "pending"
    ):
        raise CognitiveSourceVerificationError("retrieval_not_verifiable")
    if _retrieval_profile_mismatch(retrieval, RETRIEVAL_BINDING_VERSION):
        raise CognitiveSourceVerificationError("retrieval_profile_mismatch")
    materials = retrieval.get("materials")
    assert isinstance(materials, list)
    retrieval_ids = [
        str(material["retrieval_id"])
        for material in materials
        if isinstance(material, dict)
    ]
    try:
        response = verifier(deepcopy(retrieval))
    except Exception:
        return _failed_result(retrieval, "source_verifier_failed")
    try:
        assessments = _validated_assessments(response, retrieval_ids)
    except (TypeError, ValueError):
        return _failed_result(retrieval, "source_verification_result_invalid")
    cards = _build_cards(materials, assessments)
    return {
        "contract_status": "validated",
        "verification_status": "complete",
        "error_category": "none",
        "retrieval_result": retrieval,
        "assessments": assessments,
        "source_cards": cards,
        "verification_binding": {
            "binding_version": VERIFICATION_BINDING_VERSION,
            "retrieval_sha256": _canonical_sha256(retrieval),
            "assessments_sha256": _canonical_sha256(assessments),
            "source_cards_sha256": _canonical_sha256(cards),
            "source_count": len(cards),
        },
    }


def run_public_source_verification(
    retrieval_result: Mapping[str, object],
    verifier: SourceVerifier,
) -> dict[str, object]:
    """Verify one valid R1-P public retrieval batch with one injected call.

    The verifier is called exactly once with a deep copy of the retrieval
    result — no retry, no fallback.  Its per-material fetch/check record
    (frozen locator, HTTP 200, timezone-aware fetch time, the verifier-recorded
    digest of the full response body, and a recomputable evidence window that
    contains the evidence excerpt) is the only allowed origin of a public
    card's evidence; the retrieval discovery snippet stays inside
    ``retrieval_result`` and is never copied into a card.  Only the R1-P
    public retrieval binding is accepted; a synthetic retrieval is rejected
    before any verifier call.  ``authenticity`` stays ``unverified``: one
    200-fetch record does not prove publisher identity, domain ownership,
    page stability, or claim truth.
    """
    retrieval = deepcopy(dict(retrieval_result))
    if validate_retrieval_result(retrieval):
        raise CognitiveSourceVerificationError("retrieval_result_invalid")
    if (
        retrieval.get("contract_status") != "validated"
        or retrieval.get("retrieval_status") != "complete"
        or retrieval.get("verification_status") != "pending"
    ):
        raise CognitiveSourceVerificationError("retrieval_not_verifiable")
    if _retrieval_profile_mismatch(retrieval, PUBLIC_RETRIEVAL_BINDING_VERSION):
        raise CognitiveSourceVerificationError("retrieval_profile_mismatch")
    materials = retrieval.get("materials")
    assert isinstance(materials, list)
    retrieval_ids = [
        str(material["retrieval_id"])
        for material in materials
        if isinstance(material, dict)
    ]
    try:
        response = verifier(deepcopy(retrieval))
    except Exception:
        return _failed_result(
            retrieval, "source_verifier_failed", PUBLIC_VERIFICATION_BINDING_VERSION
        )
    try:
        assessments = _validated_public_assessments(response, retrieval_ids, materials)
    except (TypeError, ValueError):
        return _failed_result(
            retrieval,
            "source_verification_result_invalid",
            PUBLIC_VERIFICATION_BINDING_VERSION,
        )
    cards = _build_public_cards(materials, assessments)
    return {
        "contract_status": "validated",
        "verification_status": "complete",
        "error_category": "none",
        "retrieval_result": retrieval,
        "assessments": assessments,
        "source_cards": cards,
        "verification_binding": {
            "binding_version": PUBLIC_VERIFICATION_BINDING_VERSION,
            "retrieval_sha256": _canonical_sha256(retrieval),
            "assessments_sha256": _canonical_sha256(assessments),
            "source_cards_sha256": _canonical_sha256(cards),
            "source_count": len(cards),
        },
    }
