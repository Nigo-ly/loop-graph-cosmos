"""R1-K/R1-Q/R1-R research assembly over the R1-I/J/P boundaries.

One module-private core assembles retrieval, source verification, analysis,
and context binding exactly once per run.  Each public wrapper freezes its
own retrieval runner and its own source-verification runner:
``SyntheticResearchPipeline`` keeps the exact R1-K synthetic behavior, while
``PublicResearchPipeline`` (R1-Q/R1-R) is pinned to the R1-P
``run_public_retrieval`` boundary and the R1-R
``run_public_source_verification`` boundary.  The core never learns a vendor
name, never guesses a profile from the request_id, and no CLI, environment
variable, or caller-supplied option can override either frozen runner.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy

from fragment_loop.cognitive_context import (
    CognitiveContextError,
    bind_cognitive_context,
)
from fragment_loop.cognitive_contract import validate_cognitive_result
from fragment_loop.cognitive_retrieval import (
    RetrievalAdapter,
    run_public_retrieval,
    run_synthetic_retrieval,
)
from fragment_loop.cognitive_source_verification import (
    SourceVerifier,
    run_public_source_verification,
    run_synthetic_source_verification,
)
from fragment_loop.personal_context import PersonalContextSnapshot

ResearchAnalyst = Callable[[str, list[dict[str, object]]], Mapping[str, object]]
ContextualResearchAnalyst = Callable[
    [str, list[dict[str, object]], list[dict[str, object]]], Mapping[str, object]
]
_AnyAnalyst = Callable[
    [str, list[dict[str, object]], list[dict[str, object]] | None], Mapping[str, object]
]
RetrievalRunner = Callable[[Mapping[str, object], RetrievalAdapter], dict[str, object]]
VerificationRunner = Callable[[Mapping[str, object], SourceVerifier], dict[str, object]]


class SyntheticResearchPipelineError(ValueError):
    """A synthetic research stage failed closed."""


class _ResearchPipelineCore:
    """Assemble retrieval, source verification, and the R1-A result once each."""

    def __init__(
        self,
        *,
        retrieval_request: Mapping[str, object],
        retrieval_adapter: RetrievalAdapter,
        source_verifier: SourceVerifier,
        analyst: _AnyAnalyst,
        retrieval_runner: RetrievalRunner,
        verification_runner: VerificationRunner,
        personal_context: PersonalContextSnapshot | None = None,
    ):
        self._request = deepcopy(dict(retrieval_request))
        self._retrieval_adapter = retrieval_adapter
        self._source_verifier = source_verifier
        self._analyst = analyst
        self._retrieval_runner = retrieval_runner
        self._verification_runner = verification_runner
        self._personal_context = personal_context

    def __call__(self, fragment_text: str, route: str) -> Mapping[str, object]:
        if route != "research":
            raise SyntheticResearchPipelineError("research_route_required")
        retrieval = self._retrieval_runner(self._request, self._retrieval_adapter)
        if retrieval.get("contract_status") != "validated":
            raise SyntheticResearchPipelineError("retrieval_not_complete")
        retrieval_status = retrieval.get("retrieval_status")
        trace: Mapping[str, object]
        if retrieval_status == "complete":
            verification = self._verification_runner(
                retrieval,
                self._source_verifier,
            )
            if verification.get("contract_status") != "validated":
                raise SyntheticResearchPipelineError("source_verification_not_complete")
            trace = verification
            raw_cards = verification.get("source_cards")
        elif retrieval_status == "not_found":
            trace = retrieval
            raw_cards = []
        else:
            raise SyntheticResearchPipelineError("retrieval_not_complete")
        if not isinstance(raw_cards, list) or any(
            not isinstance(card, dict) for card in raw_cards
        ):
            raise SyntheticResearchPipelineError("source_cards_invalid")
        cards = [deepcopy(card) for card in raw_cards]
        # The analyst receives exactly three deep-copied inputs: the fragment
        # text, the verified source cards, and the snapshot's minimal material
        # projection (None when no personal context was selected).  Mutating
        # any of them in place can never pollute the frozen inputs.
        materials_projection = (
            self._personal_context.material_projection()
            if self._personal_context is not None
            else None
        )
        try:
            raw_result = self._analyst(fragment_text, deepcopy(cards), materials_projection)
        except Exception:
            raise SyntheticResearchPipelineError("research_analyst_failed") from None
        if not isinstance(raw_result, Mapping):
            raise SyntheticResearchPipelineError("cognitive_result_invalid")
        result = deepcopy(dict(raw_result))
        if validate_cognitive_result(result, fragment_text):
            raise SyntheticResearchPipelineError("cognitive_result_invalid")
        research = result.get("research")
        queries = self._request.get("queries")
        dimensions = self._request.get("search_dimensions")
        if not isinstance(research, Mapping) or not isinstance(queries, list):
            raise SyntheticResearchPipelineError("research_binding_invalid")
        expected_questions = [
            query.get("question") for query in queries if isinstance(query, Mapping)
        ]
        if (
            result.get("route") != "research"
            or research.get("questions") != expected_questions
            or research.get("search_dimensions") != dimensions
            or research.get("sources") != cards
        ):
            raise SyntheticResearchPipelineError("research_binding_invalid")
        if self._personal_context is not None:
            self._validate_personal_materials(result)
            result["personal_context"] = self._personal_context.summary()
        result["retrieval_trace"] = deepcopy(dict(trace))
        sources_by_locator = {str(card["locator"]): card for card in cards}
        material_resolver = (
            self._personal_context.resolve
            if self._personal_context is not None
            else _empty_material_resolver
        )
        try:
            return bind_cognitive_context(
                result,
                fragment_text,
                source_resolver=lambda locator: sources_by_locator.get(locator),
                material_resolver=material_resolver,
            )
        except CognitiveContextError:
            raise SyntheticResearchPipelineError("research_context_binding_failed") from None

    def _validate_personal_materials(self, result: Mapping[str, object]) -> None:
        """The analyst must echo the snapshot materials item-for-item.

        Memory carries exactly the snapshot's confirmed user facts and profile
        inferences, knowledge_base exactly its obsidian records, and frontier
        stays free of personal materials — the analyst can never invent a
        confirmed fact or record ref, drop or rewrite a material, promote an
        inference into a fact, or borrow materials across perspectives.
        """
        assert self._personal_context is not None
        projection = self._personal_context.material_projection()
        expected_memory = [
            material
            for material in projection
            if material["material_type"] in ("confirmed_user_fact", "profile_inference")
        ]
        expected_knowledge = [
            material
            for material in projection
            if material["material_type"] == "obsidian_record"
        ]
        perspectives = result.get("perspectives")
        if not isinstance(perspectives, list):
            raise SyntheticResearchPipelineError("personal_context_materials_mismatch")
        by_name: dict[str, Mapping[str, object]] = {}
        for perspective in perspectives:
            if isinstance(perspective, Mapping) and isinstance(
                perspective.get("perspective"), str
            ):
                by_name[str(perspective["perspective"])] = perspective
        memory = by_name.get("memory")
        knowledge = by_name.get("knowledge_base")
        frontier = by_name.get("frontier")
        if memory is None or knowledge is None or frontier is None:
            raise SyntheticResearchPipelineError("personal_context_materials_mismatch")
        if (
            memory.get("materials") != expected_memory
            or knowledge.get("materials") != expected_knowledge
            or frontier.get("materials") != []
        ):
            raise SyntheticResearchPipelineError("personal_context_materials_mismatch")


def _empty_material_resolver(
    _material_type: str, _ref: str
) -> Mapping[str, object] | None:
    return None


class SyntheticResearchPipeline(_ResearchPipelineCore):
    """R1-K facade pinned to the frozen synthetic retrieval runner.

    Both the R1-I synthetic retrieval runner and the R1-J synthetic v1
    source-verification runner are frozen inside this class; no caller can
    override either one.
    """

    def __init__(
        self,
        *,
        retrieval_request: Mapping[str, object],
        retrieval_adapter: RetrievalAdapter,
        source_verifier: SourceVerifier,
        analyst: ResearchAnalyst,
    ):
        super().__init__(
            retrieval_request=retrieval_request,
            retrieval_adapter=retrieval_adapter,
            source_verifier=source_verifier,
            analyst=lambda text, cards, _materials: analyst(text, cards),
            retrieval_runner=run_synthetic_retrieval,
            verification_runner=run_synthetic_source_verification,
        )


class PublicResearchPipeline(_ResearchPipelineCore):
    """R1-Q/R1-R facade pinned to the frozen public runners.

    The public profile is selected only by this class binding
    ``run_public_retrieval`` and ``run_public_source_verification``; the
    request_id is never used to guess a profile, and no caller, CLI,
    environment, or configuration input can override either runner.  Public
    materials pass the R1-R public source-verification boundary — whose
    evidence excerpt comes only from the verifier's fetch/check record for
    the actual locator, never from the retrieval discovery snippet, and
    whose ``authenticity`` stays ``unverified`` — before the analyst sees
    any source card, and the result stays a draft with unverified evidence.
    """

    def __init__(
        self,
        *,
        retrieval_request: Mapping[str, object],
        retrieval_adapter: RetrievalAdapter,
        source_verifier: SourceVerifier,
        analyst: ResearchAnalyst,
    ):
        super().__init__(
            retrieval_request=retrieval_request,
            retrieval_adapter=retrieval_adapter,
            source_verifier=source_verifier,
            analyst=lambda text, cards, _materials: analyst(text, cards),
            retrieval_runner=run_public_retrieval,
            verification_runner=run_public_source_verification,
        )


class PersonalContextResearchPipeline(_ResearchPipelineCore):
    """Package C facade: the frozen public runners plus one explicit snapshot.

    The retrieval and source-verification runners stay pinned to the existing
    public boundaries; the only addition is the caller-supplied
    ``PersonalContextSnapshot`` whose minimal material projection reaches the
    analyst as a deep copy, whose materials must be echoed item-for-item, and
    whose frozen resolver plus summary join the persisted context binding.
    No request_id, CLI, environment, or configuration input ever selects or
    overrides the profile or the snapshot.
    """

    def __init__(
        self,
        *,
        retrieval_request: Mapping[str, object],
        retrieval_adapter: RetrievalAdapter,
        source_verifier: SourceVerifier,
        analyst: ContextualResearchAnalyst,
        personal_context: PersonalContextSnapshot,
    ):
        if not isinstance(personal_context, PersonalContextSnapshot):
            raise SyntheticResearchPipelineError("personal_context_invalid")
        super().__init__(
            retrieval_request=retrieval_request,
            retrieval_adapter=retrieval_adapter,
            source_verifier=source_verifier,
            analyst=lambda text, cards, materials: analyst(
                text, cards, materials if materials is not None else []
            ),
            retrieval_runner=run_public_retrieval,
            verification_runner=run_public_source_verification,
            personal_context=personal_context,
        )
