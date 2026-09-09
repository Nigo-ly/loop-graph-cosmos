"""Loop V1 Package B: sanitized display projection of one cognitive draft.

The persisted ``cognitive_result`` is an internal fact object: it carries the
full retrieval trace, verifier records, hashes, and context binding that a
console must never need.  This module derives the only display projection the
console may render — a strict allowlist of human-readable fields, recomputed
from the persisted checkpoint ``eval_results`` on every read.  Any drift
(invalid contract, invalid trace, trace/source mismatch, non-verbatim quote)
fails closed to ``None``; the projection never invents content, never upgrades
evidence, and never includes raw HTTP bodies, credentials, verifier-internal
records, or model-internal text.

The projection deliberately keeps ``retrieval discovery`` (the search
summary snippet recorded by the retrieval boundary) and ``page evidence``
(the verbatim excerpt from the verifier's fetch record) as two separate
per-source fields: retrieval discovery is not page evidence, and both stay
``unverified``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fragment_loop.cognitive_context import validate_context_binding
from fragment_loop.cognitive_contract import validate_cognitive_result
from fragment_loop.cognitive_retrieval import validate_retrieval_result
from fragment_loop.cognitive_source_verification import (
    validate_source_verification_result,
)

DRAFT_VIEW_VERSION = "fragment-cognitive-draft-view-v1"

_SOURCE_VIEW_KEYS = (
    "source_id",
    "name",
    "locator",
    "authenticity",
    "relevance",
    "independence",
    "source_tier",
    "source_type",
    "published_at",
    "acquisition_method",
    "verification_basis",
    "scope",
    "evidence_excerpt",
)
_DISCOVERY_VIEW_KEYS = (
    "title",
    "excerpt",
    "published_at_claim",
    "source_type_hint",
    "query_refs",
)
_CLAIM_VIEW_KEYS = (
    "claim_id",
    "claim_kind",
    "text",
    "verdict",
    "confidence",
    "claim_derivation",
    "verdict_basis",
    "evidence_refs",
)
_VERIFICATION_VIEW_KEYS = ("status", "mode", "basis")
_REVISION_VIEW_KEYS = (
    "claim_id",
    "revision_status",
    "deviation",
    "revision_reason",
    "revised_text",
)
_PERSPECTIVE_VIEW_KEYS = (
    "perspective",
    "summary",
    "association",
    "conflicts",
    "value",
    "uncertainty",
)
# Package C: each material type projects its own strict allowlist — the
# material_type, the text, and the one reference/basis shape allowed for
# that type.  Any illegal combination, unknown field, or material that
# disagrees with the persisted context binding fails the whole draft view.
_MATERIAL_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "confirmed_user_fact": frozenset(
        {"material_type", "text", "inferred", "confirmation_ref"}
    ),
    "obsidian_record": frozenset({"material_type", "text", "record_ref"}),
    "profile_inference": frozenset({"material_type", "text", "basis", "uncertainty"}),
}
_MATERIAL_OUTPUT_KEYS: dict[str, tuple[str, ...]] = {
    "confirmed_user_fact": ("material_type", "text", "confirmation_ref"),
    "obsidian_record": ("material_type", "text", "record_ref"),
    "profile_inference": ("material_type", "text", "basis", "uncertainty"),
}
_SYNTHESIS_VIEW_KEYS = (
    "value",
    "weakest_link",
    "conflicts",
    "open_questions",
    "next_step",
    "credibility",
    "credibility_basis",
    "claim_counts",
)


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _pick(source: Mapping[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    return {key: source[key] for key in keys if key in source}


def _list_items(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _discovery_materials(
    result: Mapping[str, object],
) -> dict[str, Mapping[str, object]] | None:
    """Return retrieval_id → material when the persisted trace is fully valid.

    ``None`` means "fail closed": a present but invalid or mismatched trace
    must hide the whole draft view.  An absent trace yields an empty mapping —
    the cognitive contract does not require one, and sources then honestly
    show no retrieval discovery.
    """
    if "retrieval_trace" not in result:
        return {}
    trace = result["retrieval_trace"]
    if not _is_mapping(trace):
        return None
    assert isinstance(trace, Mapping)
    if "verification_binding" in trace:
        if validate_source_verification_result(trace):
            return None
        retrieval = trace.get("retrieval_result")
    elif "retrieval_binding" in trace:
        if validate_retrieval_result(trace):
            return None
        retrieval = trace
    else:
        return None
    if not _is_mapping(retrieval):
        return None
    assert isinstance(retrieval, Mapping)
    materials = retrieval.get("materials")
    if not isinstance(materials, list):
        return None
    by_id: dict[str, Mapping[str, object]] = {}
    for material in materials:
        if not _is_mapping(material):
            return None
        assert isinstance(material, Mapping)
        retrieval_id = material.get("retrieval_id")
        if not isinstance(retrieval_id, str) or retrieval_id in by_id:
            return None
        by_id[retrieval_id] = material
    return by_id


def _source_view(
    source: Mapping[str, object],
    materials: Mapping[str, Mapping[str, object]],
) -> dict[str, object] | None:
    card = _pick(source, _SOURCE_VIEW_KEYS)
    if any(key not in card for key in ("source_id", "locator", "evidence_excerpt")):
        return None
    discovery: dict[str, object] | None = None
    source_id = str(card["source_id"])
    if materials:
        material = materials.get(source_id)
        if material is None or material.get("locator") != card["locator"]:
            return None
        discovery = _pick(material, _DISCOVERY_VIEW_KEYS)
    card["discovery"] = discovery
    return card


def _research_view(
    research: Mapping[str, object],
    materials: Mapping[str, Mapping[str, object]],
) -> dict[str, object] | None:
    sources_raw = research.get("sources")
    if not isinstance(sources_raw, list):
        return None
    sources: list[dict[str, object]] = []
    for source_raw in sources_raw:
        if not _is_mapping(source_raw):
            return None
        assert isinstance(source_raw, Mapping)
        view = _source_view(source_raw, materials)
        if view is None:
            return None
        sources.append(view)
    claims: list[dict[str, object]] = []
    for claim_raw in _list_items(research.get("v1_claims")):
        if not _is_mapping(claim_raw):
            return None
        assert isinstance(claim_raw, Mapping)
        claim = _pick(claim_raw, _CLAIM_VIEW_KEYS)
        verification = claim_raw.get("independent_verification")
        if not _is_mapping(verification):
            return None
        assert isinstance(verification, Mapping)
        claim["independent_verification"] = _pick(
            verification, _VERIFICATION_VIEW_KEYS
        )
        claims.append(claim)
    counter_raw = research.get("counter_evidence_search")
    if not _is_mapping(counter_raw):
        return None
    assert isinstance(counter_raw, Mapping)
    counter = _pick(counter_raw, ("status", "scope"))
    findings: list[dict[str, object]] = []
    for finding_raw in _list_items(counter_raw.get("findings")):
        if not _is_mapping(finding_raw):
            return None
        assert isinstance(finding_raw, Mapping)
        findings.append(_pick(finding_raw, ("text", "evidence_refs")))
    counter["findings"] = findings
    revisions: list[dict[str, object]] = []
    for revision_raw in _list_items(research.get("v2_revisions")):
        if not _is_mapping(revision_raw):
            return None
        assert isinstance(revision_raw, Mapping)
        revisions.append(_pick(revision_raw, _REVISION_VIEW_KEYS))
    return {
        "questions": research.get("questions"),
        "search_dimensions": research.get("search_dimensions"),
        "sources": sources,
        "counter_evidence_search": counter,
        "v1_claims": claims,
        "v2_revisions": revisions,
    }


def _material_view(material_raw: object) -> dict[str, object] | None:
    """Project one perspective material through its strict allowlist."""
    if not _is_mapping(material_raw):
        return None
    assert isinstance(material_raw, Mapping)
    material_type = material_raw.get("material_type")
    if not isinstance(material_type, str) or material_type not in _MATERIAL_ALLOWED_KEYS:
        return None
    if not set(material_raw) <= _MATERIAL_ALLOWED_KEYS[material_type]:
        return None
    if material_type == "confirmed_user_fact" and material_raw.get("inferred") is not False:
        return None
    view = _pick(material_raw, _MATERIAL_OUTPUT_KEYS[material_type])
    for key in _MATERIAL_OUTPUT_KEYS[material_type]:
        value = view.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    return view


def _perspective_view(perspective_raw: object) -> dict[str, object] | None:
    if not _is_mapping(perspective_raw):
        return None
    assert isinstance(perspective_raw, Mapping)
    view = _pick(perspective_raw, _PERSPECTIVE_VIEW_KEYS)
    materials_raw = perspective_raw.get("materials")
    if not isinstance(materials_raw, list):
        return None
    materials: list[dict[str, object]] = []
    for material_raw in materials_raw:
        material_view = _material_view(material_raw)
        if material_view is None:
            return None
        materials.append(material_view)
    view["materials"] = materials
    return view


def build_cognitive_draft_view(
    eval_results: Mapping[str, object],
) -> dict[str, Any] | None:
    """Recompute the sanitized display projection; ``None`` on any drift."""
    if eval_results.get("cognitive_contract_status") != "validated":
        return None
    if eval_results.get("evidence_level") != "unverified":
        return None
    route = eval_results.get("cognitive_route")
    if route not in ("research", "direct"):
        return None
    cognitive_input = eval_results.get("cognitive_input")
    result = eval_results.get("cognitive_result")
    if not _is_mapping(cognitive_input) or not _is_mapping(result):
        return None
    assert isinstance(cognitive_input, Mapping) and isinstance(result, Mapping)
    fragment_text = cognitive_input.get("fragment_text")
    if (
        not isinstance(fragment_text, str)
        or not fragment_text.strip()
        or cognitive_input.get("expected_route") != route
        or result.get("route") != route
    ):
        return None
    # The backend re-validates the full persisted fact object before any
    # display field is derived; an invalid result is never projected.  A
    # persisted context binding that no longer matches the recomputed
    # projection (including any personal material drift) fails closed too.
    if validate_cognitive_result(result, fragment_text):
        return None
    if "context_binding" in result and validate_context_binding(result):
        return None
    materials = _discovery_materials(result)
    if materials is None:
        return None
    summary = result.get("summary")
    expansion = result.get("semantic_expansion")
    synthesis = result.get("synthesis")
    perspectives_raw = result.get("perspectives")
    if (
        not _is_mapping(summary)
        or not _is_mapping(expansion)
        or not _is_mapping(synthesis)
        or not isinstance(perspectives_raw, list)
    ):
        return None
    assert isinstance(summary, Mapping)
    assert isinstance(expansion, Mapping)
    assert isinstance(synthesis, Mapping)
    literal_facts: list[dict[str, object]] = []
    for fact_raw in _list_items(expansion.get("literal_facts")):
        if not _is_mapping(fact_raw):
            return None
        assert isinstance(fact_raw, Mapping)
        literal_facts.append(_pick(fact_raw, ("text", "source_quote")))
    inferences: list[dict[str, object]] = []
    for inference_raw in _list_items(expansion.get("inferences")):
        if not _is_mapping(inference_raw):
            return None
        assert isinstance(inference_raw, Mapping)
        inferences.append(
            _pick(inference_raw, ("text", "source_quote", "transformation_basis"))
        )
    perspectives: list[dict[str, object]] = []
    for perspective_raw in perspectives_raw:
        perspective_view = _perspective_view(perspective_raw)
        if perspective_view is None:
            return None
        perspectives.append(perspective_view)
    research_view: dict[str, object] | None = None
    if route == "research":
        research = result.get("research")
        if not _is_mapping(research):
            return None
        assert isinstance(research, Mapping)
        research_view = _research_view(research, materials)
        if research_view is None:
            return None
    return {
        "view_version": DRAFT_VIEW_VERSION,
        "route": route,
        "route_reason": result.get("route_reason"),
        "fragment_text": fragment_text,
        "evidence_level": "unverified",
        "summary": _pick(summary, ("text", "source_quote", "transformation_basis")),
        "semantic_expansion": {
            "literal_facts": literal_facts,
            "inferences": inferences,
            "uncertainties": expansion.get("uncertainties"),
        },
        "research": research_view,
        "perspectives": perspectives,
        "synthesis": _pick(synthesis, _SYNTHESIS_VIEW_KEYS),
    }
