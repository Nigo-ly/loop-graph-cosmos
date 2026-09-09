"""Loop V1 Package A: offline public research closed-loop service + CLI.

One service/factory wires the vendor adapter (GLM Web Search), the public
fetch + source verification adapter, and one injected public analyst into
the existing ``PublicResearchPipeline`` and ``SyntheticCognitiveLoop`` — no
second state machine, ledger, or evidence directory is created.

Package C adds one explicit personal context entry: the caller may pass the
frozen ``simulate_personal_context()`` snapshot to
``build_simulate_pipeline`` to obtain a ``PersonalContextResearchPipeline``
whose analyst sees the snapshot's minimal material projection and must echo
it item-for-item — memory carries one synthetic confirmed user fact and one
explicit profile inference, knowledge_base one synthetic obsidian record,
and frontier stays on the public source cards alone.  Calling without a
snapshot keeps the exact previous behavior (empty perspective materials).

- ``--selfcheck``: verifies only the frozen GLM profile/request vector;
  zero transport calls, zero file writes.
- ``--simulate``: runs the complete loop with fully handwritten search,
  page, classifier, and analyst fixtures; parks at
  ``awaiting_cognitive_decision``, proves a restart repeats nothing, then
  applies the human ``keep_draft`` decision, staying ``draft`` +
  ``unverified``.  Prints one minimal JSON summary.
- ``--live``: hard-blocked with the fixed error ``public_research_live_blocked``
  before any network, credential, ledger, or file write.

The public analyst boundary: the analyst callable sees only the fragment
text and the R1-R source cards — never credentials, HTTP bodies, raw
search responses, or transport objects.  Because every public source stays
``unverified``, the existing cognitive contract keeps every V1 claim
``not_covered``; the analyst may form research questions, semantic
expansion, source inventory, conflicts, and open questions only.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.cognitive_loop import SyntheticCognitiveLoop
from fragment_loop.cognitive_research_pipeline import (
    PersonalContextResearchPipeline,
    PublicResearchPipeline,
)
from fragment_loop.cognitive_retrieval import RetrievalAdapter, run_public_retrieval
from fragment_loop.cognitive_source_verification import (
    SourceVerifier,
    run_public_source_verification,
)
from fragment_loop.personal_context import (
    PersonalContextSnapshot,
    build_personal_context_snapshot,
)
from fragment_loop.public_search_glm import (
    SearchTransport,
    glm_search_selfcheck,
    make_glm_search_adapter,
)
from fragment_loop.public_source_fetch import (
    ALLOWED_CONTENT_TYPES,
    CONNECT_TIMEOUT_SECONDS,
    FETCH_PROFILE_VERSION,
    MAX_BODY_BYTES,
    TOTAL_TIMEOUT_SECONDS,
    FetchTransport,
    SourceClassifier,
    make_public_fetch_verifier,
)

LIVE_BLOCKED_ERROR = "public_research_live_blocked"

SIMULATE_FRAGMENT_ID = "synthetic-package-a-public-research"
SIMULATE_FRAGMENT_TEXT = "合成研究团队计划用通用工具研究咖啡烘焙行业。"

# Package C: handwritten synthetic personal context.  The three material
# texts are deliberately distinct so tests can prove no cross-perspective
# reuse; every reference is synthetic and nothing here is a real user fact,
# real note, or real path.
SIMULATE_PERSONAL_CONTEXT_MATERIALS: list[dict[str, object]] = [
    {
        "material_type": "confirmed_user_fact",
        "text": (
            "合成用户曾在合成对话中明确确认：咖啡烘焙行业研究类碎片一律走研究路线"
        ),
        "inferred": False,
        "confirmation_ref": "synthetic/dialogue/package-c-confirm-001",
    },
    {
        "material_type": "profile_inference",
        "text": "合成画像推断：合成用户可能偏好先核对行业证据再决定是否深入",
        "basis": "合成交互记录中合成用户多次要求先展示证据再作决定",
        "uncertainty": "该偏好可能只适用于行业研究类碎片，强度与适用范围未知",
    },
    {
        "material_type": "obsidian_record",
        "text": "合成知识库中有一份虚构的咖啡烘焙行业调研提纲笔记可以衔接",
        "record_ref": "synthetic/notes/package-c-coffee-research.md",
    },
]
SIMULATE_REQUEST: dict[str, object] = {
    "request_id": "public-package-a-simulate-search",
    "queries": [{"query_id": "Q1", "question": "通用工具是否能支持咖啡烘焙行业研究？"}],
    "search_dimensions": ["公开咖啡烘焙行业材料"],
}
SIMULATE_FETCHED_AT = datetime(2026, 8, 1, 10, 30, tzinfo=timezone(timedelta(hours=8)))
SIMULATE_PEER_IP = "93.184.216.34"

_PAGE_1_LOCATOR = "https://example.com/coffee-roasting-research"
_PAGE_2_LOCATOR = "https://example.org/general-tool-industry-fit"
_PAGE_1_TEXT_1 = "来源页面原文：某虚构团队确实使用通用工具研究咖啡烘焙行业。"
_PAGE_1_TEXT_2 = "合成补充段落，仅作证据窗口外的页面上下文。"
_PAGE_2_TEXT_1 = "来源页面原文：另一份合成材料讨论通用工具在咖啡烘焙行业的适配情况。"
_PAGES = {
    _PAGE_1_LOCATOR: (
        "<html><head><title>合成公开材料：咖啡烘焙行业研究实践</title>"
        '<script>console.log("must be ignored")</script>'
        "<style>body { color: red; }</style></head>"
        "<body><h1>咖啡烘焙行业研究实践</h1>"
        f"<p>{_PAGE_1_TEXT_1}</p><p>{_PAGE_1_TEXT_2}</p></body></html>"
    ),
    _PAGE_2_LOCATOR: (
        "<html><head><title>合成公开材料：通用工具行业适配讨论</title></head>"
        f"<body><p>{_PAGE_2_TEXT_1}</p></body></html>"
    ),
}
_GLM_RESULTS: list[dict[str, object]] = [
    {
        "title": "合成公开材料：咖啡烘焙行业研究实践",
        "content": "搜索摘要：合成公开材料声称某虚构团队使用通用工具研究咖啡烘焙行业。",
        "link": _PAGE_1_LOCATOR,
        "media": "合成行业媒体",
        "publish_date": "2026-07",
        "official_extra_field": "绝不进入持久证据",
    },
    {
        "title": "合成公开材料：通用工具行业适配讨论",
        "content": "搜索摘要：另一份合成公开材料讨论通用工具在咖啡烘焙行业的适配。",
        "link": _PAGE_2_LOCATOR,
        "media": "合成技术博客",
        "publish_date": "2026-06",
    },
]
_SELECTIONS = {
    _PAGE_1_LOCATOR: {
        "origin_id": "public-origin-package-a-p1",
        "source_tier": "secondary",
        "version": "2026-07 公开版",
        "relevance": "direct",
        "independence": "non_independent",
        "source_type": "industry_practice",
        "published_at": "2026-07",
        "verification_basis": (
            "对冻结 locator 单次 200 获取，记录完整响应内容摘要并保留可复算"
            "证据窗口；不代表发布者身份、域名所有权、页面长期稳定或主张真实"
        ),
        "scope": (
            "仅证明该 locator 当次获取返回 200 且片段取自保留的证据窗口，"
            "不证明发布者身份、域名所有权、页面长期稳定或主张真实"
        ),
        "evidence_context": _PAGE_1_TEXT_1,
        "evidence_excerpt": "某虚构团队确实使用通用工具研究咖啡烘焙行业。",
    },
    _PAGE_2_LOCATOR: {
        "origin_id": "public-origin-package-a-p2",
        "source_tier": "secondary",
        "version": "2026-06 公开版",
        "relevance": "indirect",
        "independence": "non_independent",
        "source_type": "media_report",
        "published_at": "2026-06",
        "verification_basis": (
            "对冻结 locator 单次 200 获取，记录完整响应内容摘要并保留可复算"
            "证据窗口；不代表发布者身份、域名所有权、页面长期稳定或主张真实"
        ),
        "scope": (
            "仅证明该 locator 当次获取返回 200 且片段取自保留的证据窗口，"
            "不证明发布者身份、域名所有权、页面长期稳定或主张真实"
        ),
        "evidence_context": _PAGE_2_TEXT_1,
        "evidence_excerpt": "通用工具在咖啡烘焙行业的适配情况。",
    },
}


class PublicResearchLiveBlockedError(RuntimeError):
    """The live entry is hard-blocked before any real-world side effect."""


def _simulate_search_transport(counters: dict[str, int]) -> SearchTransport:
    def transport(_request: Mapping[str, object]) -> Mapping[str, object]:
        counters["search_transport"] += 1
        body = json.dumps(
            {"id": "official-response-ignored", "search_result": _GLM_RESULTS},
            ensure_ascii=False,
        ).encode("utf-8")
        return {"http_status": 200, "body_bytes": body}

    return transport


def _simulate_fetch_transport(counters: dict[str, int]) -> FetchTransport:
    def transport(request: Mapping[str, object]) -> Mapping[str, object]:
        counters["fetch_transport"] += 1
        locator = str(request["locator"])
        return {
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "body_bytes": _PAGES[locator].encode("utf-8"),
            "final_locator": locator,
            "peer_ip": SIMULATE_PEER_IP,
            "resolved_ips": [SIMULATE_PEER_IP],
        }

    return transport


def _simulate_classifier(counters: dict[str, int]) -> SourceClassifier:
    def classifier(request: Mapping[str, object]) -> Mapping[str, object]:
        counters["classifier"] += 1
        materials = request["materials"]
        assert isinstance(materials, list)
        selections: list[dict[str, object]] = []
        for material in materials:
            assert isinstance(material, dict)
            locator = str(material["locator"])
            selection = dict(_SELECTIONS[locator])
            canonical_text = str(material["canonical_text"])
            assert str(selection["evidence_context"]) in canonical_text
            selections.append(
                {"retrieval_id": str(material["retrieval_id"]), **selection}
            )
        return {"selections": selections}

    return classifier


SimulateAnalyst = Callable[
    [str, list[dict[str, object]], list[dict[str, object]] | None],
    Mapping[str, object],
]


def simulate_personal_context() -> PersonalContextSnapshot:
    """Freeze the handwritten synthetic personal context as one snapshot."""
    return build_personal_context_snapshot(SIMULATE_PERSONAL_CONTEXT_MATERIALS)


def _simulate_analyst(
    counters: dict[str, int],
) -> SimulateAnalyst:
    def analyst(
        fragment_text: str,
        source_cards: list[dict[str, object]],
        context_materials: list[dict[str, object]] | None = None,
    ) -> Mapping[str, object]:
        counters["analyst"] += 1
        assert fragment_text == SIMULATE_FRAGMENT_TEXT
        personal = context_materials or []
        memory_materials = [
            dict(material)
            for material in personal
            if material.get("material_type")
            in ("confirmed_user_fact", "profile_inference")
        ]
        knowledge_materials = [
            dict(material)
            for material in personal
            if material.get("material_type") == "obsidian_record"
        ]
        if memory_materials or knowledge_materials:
            memory_summary = (
                "memory 视角依据一条已确认合成用户事实与一条明确画像推断"
            )
            memory_association = "已确认事实要求研究路线，画像推断提示先核对证据"
            knowledge_summary = "knowledge_base 视角依据一条合成可追溯知识记录"
            knowledge_association = "合成调研提纲笔记与咖啡烘焙研究主题衔接"
        else:
            memory_summary = "memory 视角只依据公开来源卡与合成原文"
            memory_association = "只与合成原文及公开来源卡相关"
            knowledge_summary = "knowledge_base 视角只依据公开来源卡与合成原文"
            knowledge_association = "只与合成原文及公开来源卡相关"
        perspectives: list[dict[str, object]] = [
            {
                "perspective": "memory",
                "summary": memory_summary,
                "association": memory_association,
                "conflicts": [],
                "value": "保留待验证问题",
                "uncertainty": "公开来源真实性未核验",
                "materials": memory_materials,
            },
            {
                "perspective": "knowledge_base",
                "summary": knowledge_summary,
                "association": knowledge_association,
                "conflicts": [],
                "value": "保留待验证问题",
                "uncertainty": "公开来源真实性未核验",
                "materials": knowledge_materials,
            },
            {
                "perspective": "frontier",
                "summary": "frontier 视角只依据公开来源卡与合成原文",
                "association": "只与合成原文及公开来源卡相关",
                "conflicts": [],
                "value": "保留待验证问题",
                "uncertainty": "公开来源真实性未核验",
                "materials": [],
            },
        ]
        return {
            "route": "research",
            "route_reason": "包含行业研究能力主张，需要研究路线",
            "route_change_allowed": True,
            "summary": {
                "text": fragment_text,
                "source_quote": fragment_text,
                "transformation_basis": "摘要与合成原文一致",
            },
            "semantic_expansion": {
                "literal_facts": [
                    {"text": "合成研究团队", "source_quote": "合成研究团队"},
                    {"text": "研究咖啡烘焙行业", "source_quote": "研究咖啡烘焙行业"},
                ],
                "inferences": [],
                "uncertainties": ["通用工具的行业研究能力尚未被证据覆盖"],
            },
            "research": {
                "questions": ["通用工具是否能支持咖啡烘焙行业研究？"],
                "search_dimensions": ["公开咖啡烘焙行业材料"],
                "sources": source_cards,
                "counter_evidence_search": {
                    "status": "not_found",
                    "scope": "公开来源卡中未发现相反观点",
                    "findings": [],
                },
                "v1_claims": [
                    {
                        "claim_id": "C1",
                        "claim_kind": "general_fact",
                        "text": "通用工具已经具备咖啡烘焙行业研究能力",
                        "source_quote": "通用工具",
                        "verdict": "not_covered",
                        "confidence": "low",
                        "evidence_refs": [],
                        "evidence_support": [],
                        "claim_derivation": "从合成原文的工具计划拆出待验证能力主张",
                        "verdict_basis": (
                            "公开来源本阶段恒为 unverified，不能覆盖该能力主张"
                        ),
                        "independent_verification": {
                            "status": "not_verified",
                            "mode": "none",
                            "basis": "没有已核验公开证据，不能独立验证",
                            "evidence_refs": [],
                        },
                    }
                ],
                "v2_revisions": [
                    {
                        "claim_id": "C1",
                        "revision_status": "unchanged",
                        "deviation": "V1 已明确标记为未覆盖",
                        "revision_reason": "公开来源未核验，不能改变判定",
                        "revised_text": "通用工具已经具备咖啡烘焙行业研究能力",
                    }
                ],
            },
            "perspectives": perspectives,
            "synthesis": {
                "value": "可作为后续研究问题",
                "weakest_link": "公开来源真实性未核验",
                "conflicts": [],
                "open_questions": ["通用工具的行业研究能力是否存在？"],
                "next_step": "等待人工决定是否保留",
                "credibility": "insufficient",
                "credibility_basis": "公开来源未核验真实性，唯一主张未被证据覆盖",
                "claim_counts": {
                    "supported": 0,
                    "partially_supported": 0,
                    "contradicted": 0,
                    "not_covered": 1,
                },
            },
        }

    return analyst


def build_simulate_pipeline(
    counters: dict[str, int],
    personal_context: PersonalContextSnapshot | None = None,
) -> PublicResearchPipeline | PersonalContextResearchPipeline:
    """Wire the three handwritten fixtures into the frozen public pipeline.

    Without a snapshot the exact previous context-free behavior is kept;
    with one explicit snapshot the Package C facade binds the frozen public
    runners to the snapshot's material projection and resolver.
    """
    retrieval_adapter: RetrievalAdapter = make_glm_search_adapter(
        _simulate_search_transport(counters)
    )
    verifier: SourceVerifier = make_public_fetch_verifier(
        _simulate_fetch_transport(counters),
        _simulate_classifier(counters),
        clock=lambda: SIMULATE_FETCHED_AT,
    )
    if personal_context is None:
        simulate_analyst = _simulate_analyst(counters)

        def analyst(
            fragment_text: str, source_cards: list[dict[str, object]]
        ) -> Mapping[str, object]:
            """Minimal two-argument wrapper keeping the context-free behavior."""
            return simulate_analyst(fragment_text, source_cards, None)

        return PublicResearchPipeline(
            retrieval_request=SIMULATE_REQUEST,
            retrieval_adapter=retrieval_adapter,
            source_verifier=verifier,
            analyst=analyst,
        )
    simulate_analyst = _simulate_analyst(counters)

    def contextual_analyst(
        fragment_text: str,
        source_cards: list[dict[str, object]],
        context_materials: list[dict[str, object]],
    ) -> Mapping[str, object]:
        return simulate_analyst(fragment_text, source_cards, context_materials)

    return PersonalContextResearchPipeline(
        retrieval_request=SIMULATE_REQUEST,
        retrieval_adapter=retrieval_adapter,
        source_verifier=verifier,
        analyst=contextual_analyst,
        personal_context=personal_context,
    )


def selfcheck() -> dict[str, object]:
    """Verify frozen profile/request vectors only; zero side effects."""
    return {
        "status": "selfcheck_ok",
        "search": glm_search_selfcheck(),
        "fetch_profile": {
            "profile_version": FETCH_PROFILE_VERSION,
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "total_timeout_seconds": TOTAL_TIMEOUT_SECONDS,
            "max_body_bytes": MAX_BODY_BYTES,
            "allowed_content_types": list(ALLOWED_CONTENT_TYPES),
            "allow_redirects": False,
            "allow_proxy": False,
        },
        "pipeline_runners": [
            run_public_retrieval.__name__,
            run_public_source_verification.__name__,
        ],
        "transport_calls": 0,
    }


def simulate(db_path: str | Path) -> dict[str, object]:
    """Run the full handwritten loop: pause, resume without repeat, keep."""
    counters = {
        "search_transport": 0,
        "fetch_transport": 0,
        "classifier": 0,
        "analyst": 0,
    }
    pipeline = build_simulate_pipeline(counters)
    store = SQLiteCheckpointStore(db_path)
    service = SyntheticCognitiveLoop(store, pipeline)
    registered = service.register(
        fragment_id=SIMULATE_FRAGMENT_ID,
        fragment_text=SIMULATE_FRAGMENT_TEXT,
        route="research",
    )
    waiting = service.run(registered.run_id)
    calls_after_first_run = dict(counters)

    def forbidden_producer(_text: str, _route: str) -> Mapping[str, object]:
        raise AssertionError("restart must not repeat the research pipeline")

    restarted = SyntheticCognitiveLoop(
        SQLiteCheckpointStore(db_path),
        forbidden_producer,
    )
    resumed = restarted.run(registered.run_id)
    calls_after_restart = dict(counters)
    kept = restarted.decide(registered.run_id, "keep_draft")
    cognitive_result = kept.eval_results.get("cognitive_result")
    research = (
        cognitive_result.get("research")
        if isinstance(cognitive_result, Mapping)
        else None
    )
    sources = research.get("sources") if isinstance(research, Mapping) else None
    return {
        "status": "simulate_complete",
        "run_id": registered.run_id,
        "paused_status": waiting.status,
        "paused_stop_reason": waiting.stop_reason,
        "resumed_status": resumed.status,
        "resumed_stop_reason": resumed.stop_reason,
        "final_status": kept.status,
        "final_stop_reason": kept.stop_reason,
        "content_lifecycle": kept.eval_results.get("content_lifecycle"),
        "evidence_level": kept.eval_results.get("evidence_level"),
        "source_count": len(sources) if isinstance(sources, list) else 0,
        "claim_verdicts": {"not_covered": 1},
        "call_counts": calls_after_first_run,
        "restart_extra_calls": {
            key: calls_after_restart[key] - calls_after_first_run[key]
            for key in counters
        },
    }


def live() -> None:
    """Hard-block before any network, credential, ledger, or file write."""
    raise PublicResearchLiveBlockedError(LIVE_BLOCKED_ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="public_research_loop",
        description="Loop V1 Package A offline public research closed loop",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selfcheck", action="store_true")
    mode.add_argument("--simulate", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--db", help="SQLite path for --simulate (else a temp file)")
    args = parser.parse_args(argv)

    if args.selfcheck:
        print(json.dumps(selfcheck(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.live:
        # Hard block: no store, no transport, no credential, no file write.
        try:
            live()
        except PublicResearchLiveBlockedError as error:
            print(
                json.dumps(
                    {"status": "blocked", "error_category": str(error)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
    if args.db:
        print(json.dumps(simulate(args.db), ensure_ascii=False, sort_keys=True))
        return 0
    with tempfile.TemporaryDirectory(prefix="public-research-simulate-") as directory:
        summary = simulate(Path(directory) / "loop.sqlite3")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
