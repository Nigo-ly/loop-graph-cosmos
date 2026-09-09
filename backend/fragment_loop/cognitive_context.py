"""R1-D synthetic adapter binding for cognitive source and reference material.

The caller injects resolvers. This module itself has no network, file,
credential, model, or private-data adapter. A binding proves only that the
result exactly matched the material returned by those resolvers at binding
time; it does not prove that a resolver's material is true.

Sources keep the safe ``synthetic/...`` reference rule.  The only exception is
an R1-P ``public_search_adapter`` source card whose HTTPS locator is bound
solely because the result carries a ``retrieval_trace`` that passes the
existing R1-J revalidation and whose persisted ``source_cards`` are exactly
the result's ``research.sources``; the locator's safe shape was already
verified by the persisted R1-P retrieval record and the R1-J card chain.
Without that trace, with a drifted trace, with mismatched sources, or with
any other acquisition method the binding fails closed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy

from fragment_loop.cognitive_contract import validate_cognitive_result
from fragment_loop.cognitive_retrieval import validate_retrieval_result
from fragment_loop.cognitive_source_verification import (
    validate_source_verification_result,
)

# r1d-v2: profile_inference materials and the optional personal context
# snapshot summary now join the persisted projection (Package C); any drift
# in their text/basis/uncertainty or in the snapshot summary fails the
# recomputed binding.  Validation stays version-aware: a persisted r1d-v1
# binding is always recomputed with the exact legacy v1 projection (no
# profile inferences, no personal context summary), so Package B/v1 drafts
# keep validating and displaying after the upgrade; unknown versions fail
# closed.  New bindings are always written as r1d-v2.
CONTEXT_BINDING_VERSION = "fragment-cognitive-context-r1d-v2"
LEGACY_CONTEXT_BINDING_VERSION = "fragment-cognitive-context-r1d-v1"

SourceResolver = Callable[[str], Mapping[str, object] | None]
MaterialResolver = Callable[[str, str], Mapping[str, object] | None]


class CognitiveContextError(ValueError):
    """A proposed result does not match the injected context adapters."""


def bind_cognitive_context(
    result: Mapping[str, object],
    fragment_text: str,
    *,
    source_resolver: SourceResolver,
    material_resolver: MaterialResolver,
) -> dict[str, object]:
    """Resolve every reference-bearing material and attach a stable binding."""
    if validate_cognitive_result(result, fragment_text):
        raise CognitiveContextError("cognitive_result_invalid")
    projection = _context_projection(result)
    sources = projection["sources"]
    materials = projection["materials"]
    if not isinstance(sources, list) or not isinstance(materials, list):
        raise CognitiveContextError("context_projection_invalid")
    public_sources_trace_bound = _public_trace_binds_sources(result, sources)
    for source in sources:
        if not isinstance(source, dict):
            raise CognitiveContextError("source_projection_invalid")
        locator = source.get("locator")
        if not isinstance(locator, str):
            raise CognitiveContextError("source_locator_invalid")
        if not _is_synthetic_reference(locator) and not (
            public_sources_trace_bound
            and source.get("acquisition_method") == "public_search_adapter"
        ):
            raise CognitiveContextError("non_synthetic_reference")
        try:
            resolved = source_resolver(locator)
        except Exception:
            raise CognitiveContextError("source_resolver_failed") from None
        if resolved is None:
            raise CognitiveContextError("source_not_resolved")
        if dict(resolved) != source:
            raise CognitiveContextError("source_resolution_mismatch")
    for entry in materials:
        if not isinstance(entry, dict):
            raise CognitiveContextError("material_projection_invalid")
        material_type = entry.get("material_type")
        ref = entry.get("ref")
        material = entry.get("material")
        if not isinstance(material_type, str) or not isinstance(material, dict):
            raise CognitiveContextError("material_reference_invalid")
        if material_type == "profile_inference":
            # A profile inference has no external resolver: it is bound solely
            # by joining the persisted projection/hash, so any text, basis, or
            # uncertainty drift fails `validate_context_binding`.
            if ref is not None:
                raise CognitiveContextError("material_reference_invalid")
            continue
        if not isinstance(ref, str):
            raise CognitiveContextError("material_reference_invalid")
        if not _is_synthetic_reference(ref):
            raise CognitiveContextError("non_synthetic_reference")
        try:
            resolved = material_resolver(material_type, ref)
        except Exception:
            raise CognitiveContextError("material_resolver_failed") from None
        if resolved is None:
            raise CognitiveContextError("material_not_resolved")
        if dict(resolved) != material:
            raise CognitiveContextError("material_resolution_mismatch")
    bound = deepcopy(dict(result))
    bound["context_binding"] = _binding_for_projection(projection)
    return bound


def validate_context_binding(result: Mapping[str, object]) -> tuple[str, ...]:
    """Recompute the persisted projection for its own binding version.

    The persisted ``binding_version`` strictly selects the projection
    algorithm: r1d-v1 replays the exact legacy projection (reference-bearing
    materials only, no profile inferences, no personal context summary),
    r1d-v2 uses the Package C projection, and any other version fails
    closed.  Legacy Package B/v1 drafts therefore keep validating after the
    upgrade, while v2 drift is still caught by the v2 projection.
    """
    binding = result.get("context_binding")
    if not isinstance(binding, Mapping):
        return ("context_binding 必须是对象",)
    version = binding.get("binding_version")
    if version == LEGACY_CONTEXT_BINDING_VERSION:
        include_inferences = False
    elif version == CONTEXT_BINDING_VERSION:
        include_inferences = True
    else:
        return ("context_binding.binding_version 未知",)
    try:
        expected = _binding_for_projection(
            _context_projection(result, include_inferences=include_inferences),
            binding_version=str(version),
        )
    except CognitiveContextError as error:
        return (str(error),)
    if set(binding) != set(expected):
        return ("context_binding 字段集合不匹配",)
    for key, expected_value in expected.items():
        actual = binding.get(key)
        if type(actual) is not type(expected_value) or actual != expected_value:
            return (f"context_binding.{key} 与持久材料投影不一致",)
    return ()


def _context_projection(
    result: Mapping[str, object], *, include_inferences: bool = True
) -> dict[str, object]:
    sources: list[dict[str, object]] = []
    research = result.get("research")
    if isinstance(research, Mapping):
        raw_sources = research.get("sources")
        if isinstance(raw_sources, (list, tuple)):
            for source in raw_sources:
                if isinstance(source, Mapping):
                    sources.append(deepcopy(dict(source)))
    materials: list[dict[str, object]] = []
    perspectives = result.get("perspectives")
    if isinstance(perspectives, (list, tuple)):
        for perspective in perspectives:
            if not isinstance(perspective, Mapping):
                continue
            name = perspective.get("perspective")
            raw_materials = perspective.get("materials")
            if not isinstance(name, str) or not isinstance(raw_materials, (list, tuple)):
                continue
            for material_raw in raw_materials:
                if not isinstance(material_raw, Mapping):
                    continue
                material = deepcopy(dict(material_raw))
                material_type = material.get("material_type")
                ref: object = None
                if material_type == "confirmed_user_fact":
                    ref = material.get("confirmation_ref")
                elif material_type == "obsidian_record":
                    ref = material.get("record_ref")
                elif material_type == "profile_inference" and include_inferences:
                    # Inferred material joins the v2 projection without an
                    # external reference so persisted text/basis/uncertainty
                    # drift is caught by the recomputed context hash.  The
                    # legacy v1 projection skips it exactly as before.
                    ref = None
                else:
                    continue
                if not isinstance(material_type, str):
                    continue
                if material_type != "profile_inference" and not isinstance(ref, str):
                    continue
                materials.append(
                    {
                        "perspective": name,
                        "material_type": material_type,
                        "ref": ref,
                        "material": material,
                    }
                )
    projection: dict[str, object] = {"sources": sources, "materials": materials}
    if include_inferences:
        personal_context = result.get("personal_context")
        if personal_context is not None:
            projection["personal_context"] = _personal_context_summary(personal_context)
    retrieval_trace = result.get("retrieval_trace")
    if retrieval_trace is not None:
        if not isinstance(retrieval_trace, Mapping):
            raise CognitiveContextError("retrieval_trace_invalid")
        if not validate_source_verification_result(retrieval_trace):
            trace_sources = retrieval_trace.get("source_cards")
        elif (
            not validate_retrieval_result(retrieval_trace)
            and retrieval_trace.get("contract_status") == "validated"
            and retrieval_trace.get("retrieval_status") == "not_found"
        ):
            trace_sources = []
        else:
            raise CognitiveContextError("retrieval_trace_invalid")
        if trace_sources != sources:
            raise CognitiveContextError("retrieval_trace_sources_mismatch")
        projection["retrieval_trace"] = deepcopy(dict(retrieval_trace))
    return projection


def _binding_for_projection(
    projection: Mapping[str, object],
    binding_version: str = CONTEXT_BINDING_VERSION,
) -> dict[str, object]:
    try:
        canonical = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise CognitiveContextError("context_not_json_serializable") from None
    sources = projection.get("sources")
    materials = projection.get("materials")
    if not isinstance(sources, list) or not isinstance(materials, list):
        raise CognitiveContextError("context_projection_invalid")
    binding: dict[str, object] = {
        "binding_version": binding_version,
        "source_count": len(sources),
        "material_count": len(materials),
        "context_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    if "retrieval_trace" in projection:
        binding["retrieval_trace_bound"] = True
    return binding


def _personal_context_summary(value: object) -> dict[str, object]:
    """Validate the persisted snapshot summary (version, count, SHA only).

    The summary never carries private text; its strict shape joins the
    recomputable projection so any tampering fails the binding.
    """
    if not isinstance(value, Mapping) or set(value) != {
        "snapshot_version",
        "material_count",
        "snapshot_sha256",
    }:
        raise CognitiveContextError("personal_context_invalid")
    version = value.get("snapshot_version")
    count = value.get("material_count")
    sha256 = value.get("snapshot_sha256")
    if (
        not isinstance(version, str)
        or not version.strip()
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise CognitiveContextError("personal_context_invalid")
    return {
        "snapshot_version": version,
        "material_count": count,
        "snapshot_sha256": sha256,
    }


def _is_synthetic_reference(value: str) -> bool:
    if not value.startswith("synthetic/") or "://" in value or "\\" in value:
        return False
    return all(part not in ("", ".", "..") for part in value.split("/"))


def _public_trace_binds_sources(
    result: Mapping[str, object], sources: list[dict[str, object]]
) -> bool:
    """Fail closed unless a valid R1-J trace binds exactly these source cards.

    The trace must pass the existing offline revalidation and its persisted
    ``source_cards`` must equal the result's ``research.sources``; only then
    may a ``public_search_adapter`` card bind its HTTPS locator.  Anything
    less — no trace, a forged or drifted trace, or spliced sources — keeps
    every non-synthetic locator rejected.
    """
    trace = result.get("retrieval_trace")
    if not isinstance(trace, Mapping) or validate_source_verification_result(trace):
        return False
    return bool(trace.get("source_cards") == sources)
