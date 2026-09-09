"""Fragment cognitive ten-step capability chain as Graph Phase 1 config.

The business chain is configuration, not core hardcoding. Every node runs a
deterministic synthetic adapter — no model calls, no network, no Vault
scans, and no writes to Loop V1 history. The fixture demonstrates sequence,
a research/direct condition branch, research and memory fan-out, explicit
all-success fan-in, one bounded validation feedback, a human gate pause,
failure isolation, and resume, without replaying any real model chain.
"""

from __future__ import annotations

import hashlib
from typing import Any

from graph_runtime.runtime import NodeRequest, NodeResult
from graph_runtime.spec import GraphSpec, validate_graph_spec

RESEARCH_GRAPH_ID = "fragment-research-v1"
COGNITIVE_GRAPH_ID = "fragment-cognitive-v1"
POLICY_PROFILE = "graph-phase1-synthetic"

_EDGE_SEQUENCE = {"type": "sequence", "decision_source": "declared"}


def fragment_research_v1_raw() -> dict[str, Any]:
    """Controlled research subgraph: questions → fetch → verify → claims."""
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": RESEARCH_GRAPH_ID,
        "version": "1.0.0",
        "goal": "研究子图：研究问题、检索获取、来源核验、主张映射（全合成确定性）",
        "entry_node": "research_questions",
        "nodes": [
            {
                "id": "research_questions",
                "kind": "capability",
                "adapter": "fixture.research_questions",
                "input_schema": {"inputs": "object"},
                "output_schema": {"questions": "array"},
            },
            {
                "id": "source_fetch",
                "kind": "capability",
                "adapter": "fixture.source_fetch",
                "input_schema": {"questions": "array"},
                "output_schema": {"sources": "array"},
            },
            {
                "id": "source_verification",
                "kind": "validator",
                "adapter": "fixture.source_verification",
                "input_schema": {"sources": "array"},
                "output_schema": {"verified": "boolean", "basis": "string"},
            },
            {
                "id": "claim_mapping",
                "kind": "capability",
                "adapter": "fixture.claim_mapping",
                "input_schema": {"verified": "boolean"},
                "output_schema": {"claims": "array"},
            },
            {
                "id": "research_done",
                "kind": "output",
                "input_schema": {"claims": "array"},
                "output_schema": {},
            },
        ],
        "edges": [
            {
                "id": "e_questions_fetch",
                "from": "research_questions",
                "to": "source_fetch",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_fetch_verify",
                "from": "source_fetch",
                "to": "source_verification",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_verify_claims",
                "from": "source_verification",
                "to": "claim_mapping",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_claims_done",
                "from": "claim_mapping",
                "to": "research_done",
                **_EDGE_SEQUENCE,
            },
        ],
        "budgets": {"max_node_executions": 32},
        "policy_profile": POLICY_PROFILE,
        "subgraphs": [],
    }


def fragment_cognitive_v1_raw(research_digest: str) -> dict[str, Any]:
    """Main chain: input → expansion → route → branch → V1/V2 → join → human."""
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": COGNITIVE_GRAPH_ID,
        "version": "1.0.0",
        "goal": "碎片认知十步能力链的确定性合成重放：分支、并行、汇合、反馈与人工确认",
        "entry_node": "input_fragment",
        "nodes": [
            {
                "id": "input_fragment",
                "kind": "input",
                "adapter": "fixture.input_fragment",
                "input_schema": {},
                "output_schema": {"fragment_text": "string", "immutable_ref": "string"},
            },
            {
                "id": "semantic_expansion",
                "kind": "capability",
                "adapter": "fixture.semantic_expansion",
                "input_schema": {"fragment_text": "string"},
                "output_schema": {"literal_facts": "array", "expansion_basis": "string"},
            },
            {
                "id": "route_decision",
                "kind": "router",
                "adapter": "fixture.route_decision",
                "input_schema": {"literal_facts": "array"},
                "output_schema": {"route": "string", "route_reason": "string"},
            },
            {
                "id": "research_branch",
                "kind": "subgraph",
                "subgraph_ref": {
                    "graph_id": RESEARCH_GRAPH_ID,
                    "version": "1.0.0",
                    "digest": research_digest,
                },
                "input_schema": {"route": "string"},
                "output_schema": {"subgraph": "string", "outputs": "object"},
            },
            {
                "id": "direct_perspective",
                "kind": "capability",
                "adapter": "fixture.direct_perspective",
                "input_schema": {"route": "string"},
                "output_schema": {"perspective": "string", "basis": "string"},
            },
            {
                "id": "route_join",
                "kind": "join",
                "join": {
                    "mode": "all_success",
                    "threshold": None,
                    "on_partial_failure": "fail",
                    "cancel_remaining": False,
                },
                "input_schema": {},
                "output_schema": {"joined": "array", "skipped": "array"},
            },
            {
                "id": "synthesis_v1",
                "kind": "capability",
                "adapter": "fixture.synthesis_v1",
                "input_schema": {"joined": "array"},
                "output_schema": {"draft_v1": "string", "synthesis_basis": "string"},
            },
            {
                "id": "calibration_v2",
                "kind": "validator",
                "adapter": "fixture.calibration_v2",
                "input_schema": {"draft_v1": "string"},
                "output_schema": {"calibrated": "boolean", "calibration_basis": "string"},
            },
            {
                "id": "memory_context",
                "kind": "capability",
                "adapter": "fixture.memory_context",
                "input_schema": {"fragment_text": "string"},
                "output_schema": {"memory_findings": "array", "note": "string"},
            },
            {
                "id": "personal_perspective",
                "kind": "capability",
                "adapter": "fixture.personal_perspective",
                "input_schema": {"fragment_text": "string"},
                "output_schema": {"perspective": "string", "basis": "string"},
            },
            {
                "id": "final_join",
                "kind": "join",
                "join": {
                    "mode": "all_success",
                    "threshold": None,
                    "on_partial_failure": "fail",
                    "cancel_remaining": False,
                },
                "input_schema": {},
                "output_schema": {"joined": "array", "skipped": "array"},
            },
            {
                "id": "human_confirmation",
                "kind": "human_decision",
                "human_gate": {
                    "allowed_decisions": ["approve", "reject"],
                    "timeout_policy": "pause",
                    "binding": [
                        "run_id",
                        "node_id",
                        "decision",
                        "spec_digest",
                        "input_digest",
                        "expected_sequence",
                        "requester",
                        "decision_id",
                    ],
                },
                "input_schema": {"joined": "array"},
                "output_schema": {"decision": "string"},
            },
            {
                "id": "synthetic_output",
                "kind": "action",
                "adapter": "fixture.synthetic_output",
                "action_policy": {
                    "policy_profile": POLICY_PROFILE,
                    "required_gate": "human_confirmation",
                    "side_effect_level": "none",
                },
                "input_schema": {"decision": "string"},
                "output_schema": {"action": "string", "delivered": "boolean"},
            },
            {
                "id": "run_rejected",
                "kind": "output",
                "input_schema": {"decision": "string"},
                "output_schema": {},
            },
        ],
        "edges": [
            {
                "id": "e_input_expansion",
                "from": "input_fragment",
                "to": "semantic_expansion",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_expansion_router",
                "from": "semantic_expansion",
                "to": "route_decision",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_route_research",
                "from": "route_decision",
                "to": "research_branch",
                "type": "condition",
                "condition": {"field": "route", "op": "equals", "value": "research"},
                "priority": 0,
                "decision_source": "rule_evaluated",
            },
            {
                "id": "e_route_direct",
                "from": "route_decision",
                "to": "direct_perspective",
                "type": "condition",
                "condition": {"field": "route", "op": "equals", "value": "direct"},
                "priority": 1,
                "decision_source": "rule_evaluated",
            },
            {
                "id": "e_research_join",
                "from": "research_branch",
                "to": "route_join",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_direct_join",
                "from": "direct_perspective",
                "to": "route_join",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_join_synthesis",
                "from": "route_join",
                "to": "synthesis_v1",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_synthesis_calibration",
                "from": "synthesis_v1",
                "to": "calibration_v2",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_calibration_feedback",
                "from": "calibration_v2",
                "to": "semantic_expansion",
                "type": "feedback",
                "condition": {"field": "error", "op": "equals", "value": "calibration_failed"},
                "max_traversals": 1,
                "on_exhausted": "pause",
                "decision_source": "rule_evaluated",
            },
            {
                "id": "e_calibration_final",
                "from": "calibration_v2",
                "to": "final_join",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_input_memory",
                "from": "input_fragment",
                "to": "memory_context",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_input_personal",
                "from": "input_fragment",
                "to": "personal_perspective",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_memory_final",
                "from": "memory_context",
                "to": "final_join",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_personal_final",
                "from": "personal_perspective",
                "to": "final_join",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_final_human",
                "from": "final_join",
                "to": "human_confirmation",
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_human_approve",
                "from": "human_confirmation",
                "to": "synthetic_output",
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "approve"},
                "priority": 0,
                "decision_source": "human_selected",
            },
            {
                "id": "e_human_reject",
                "from": "human_confirmation",
                "to": "run_rejected",
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "reject"},
                "priority": 1,
                "decision_source": "human_selected",
            },
        ],
        "budgets": {"max_node_executions": 64},
        "policy_profile": POLICY_PROFILE,
        "subgraphs": [
            {
                "graph_id": RESEARCH_GRAPH_ID,
                "version": "1.0.0",
                "digest": research_digest,
            }
        ],
    }


def build_fragment_cognitive_specs() -> dict[str, GraphSpec]:
    research = validate_graph_spec(fragment_research_v1_raw())
    parent = validate_graph_spec(fragment_cognitive_v1_raw(research.digest))
    return {research.graph_id: research, parent.graph_id: parent}


def make_fragment_cognitive_adapters(
    fragment_text: str,
    *,
    calibration_failures: int = 0,
    route_override: str | None = None,
) -> dict[str, Any]:
    """Deterministic synthetic handlers; identical inputs always replay
    identically, and the bounded calibration retry is an explicit counter."""
    state = {"calibration_failures_left": calibration_failures}

    def input_fragment(_request: NodeRequest) -> NodeResult:
        digest = hashlib.sha256(fragment_text.encode("utf-8")).hexdigest()
        ref = f"fixture:fragment:{digest[:16]}"
        return NodeResult(
            output={"fragment_text": fragment_text, "immutable_ref": ref},
            output_refs=(ref,),
        )

    def semantic_expansion(request: NodeRequest) -> NodeResult:
        text = str(request.inputs.get("input_fragment", {}).get("fragment_text", fragment_text))
        facts = [part.strip() for part in text.replace("。", "，").split("，") if part.strip()]
        return NodeResult(
            output={
                "literal_facts": facts,
                "expansion_basis": "逐句对齐原文的合成语义展开",
            }
        )

    def route_decision(_request: NodeRequest) -> NodeResult:
        route = route_override or (
            "research"
            if any(token in fragment_text for token in ("文章", "链接", "http", "读"))
            else "direct"
        )
        reason = (
            "碎片涉及外部材料，走研究路线"
            if route == "research"
            else "简单事务无需外部研究，走直接路线"
        )
        return NodeResult(output={"route": route, "route_reason": reason}, route=route)

    def direct_perspective(_request: NodeRequest) -> NodeResult:
        return NodeResult(output={"perspective": "direct", "basis": "直接路线只做适用视角（合成）"})

    def synthesis_v1(_request: NodeRequest) -> NodeResult:
        return NodeResult(
            output={
                "draft_v1": f"合成 V1 草稿：{fragment_text[:24]}",
                "synthesis_basis": "基于路线分支输出的合成综合",
            }
        )

    def calibration_v2(_request: NodeRequest) -> NodeResult:
        if state["calibration_failures_left"] > 0:
            state["calibration_failures_left"] -= 1
            return NodeResult(
                output={"weakest_link": "合成校准缺口：V1 草稿与原文存在偏差"},
                failure="calibration_failed",
            )
        return NodeResult(
            output={
                "calibrated": True,
                "calibration_basis": "V2 与原文逐句核对一致（合成）",
            }
        )

    def memory_context(request: NodeRequest) -> NodeResult:
        refs = tuple(str(ref) for ref in request.run_inputs.get("memory_refs", ()))
        if not refs or request.memory is None:
            return NodeResult(
                output={"memory_findings": [], "note": "材料不足：无获准记忆输入"},
                material_insufficient=True,
            )
        view = request.memory(refs)
        # Asset and graph nodes are projected through one uniform field
        # set; fields a node kind does not carry stay None rather than
        # being invented.
        findings: list[dict[str, Any]] = [
            {
                "ref": node["id"],
                "title": node.get("title"),
                "content_lifecycle": node.get("content_lifecycle"),
                "evidence_level": node.get("evidence_level"),
                "digest": node.get("digest"),
                # Read-only, non-evidential provenance of a selected asset.
                "source_fragment": node.get("source_fragment"),
                "candidate_ref": node.get("candidate_ref"),
                "candidate_id": node.get("candidate_id"),
                # Graph-projection node fields (None for Loop V1 assets).
                "node_type": node.get("node_type"),
                "source_node_ref": node.get("source_node_ref"),
                "label": node.get("label"),
            }
            for node in view["nodes"]
        ]
        # All memory relations — asset derivation records and extracted
        # graph knowledge edges alike — are projected as ref-only records;
        # none of them can ever become an execution edge.
        findings.extend(dict(relation) for relation in view["relations"])
        return NodeResult(
            output={
                "memory_findings": findings,
                "note": "只读记忆上下文（不复制正文，含派生溯源与知识关系引用）",
            },
            output_refs=tuple(str(node["id"]) for node in view["nodes"]),
        )

    def personal_perspective(_request: NodeRequest) -> NodeResult:
        return NodeResult(output={"perspective": "personal", "basis": "个性化视角（合成只读分支）"})

    def synthetic_output(request: NodeRequest) -> NodeResult:
        artifact = f"exec:artifact:synthetic:{request.run_id[-12:]}"
        return NodeResult(
            output={
                "action": "synthetic_output",
                "delivered": True,
                "artifact_ref": artifact,
            },
            output_refs=(artifact,),
        )

    def research_questions(request: NodeRequest) -> NodeResult:
        return NodeResult(
            output={"questions": ["合成研究问题：来源是什么？", "合成研究问题：主张是否可核验？"]}
        )

    def source_fetch(_request: NodeRequest) -> NodeResult:
        ref = "fixture:source:research-1"
        return NodeResult(output={"sources": [ref]}, output_refs=(ref,))

    def source_verification(_request: NodeRequest) -> NodeResult:
        return NodeResult(output={"verified": True, "basis": "合成来源核验通过"})

    def claim_mapping(_request: NodeRequest) -> NodeResult:
        return NodeResult(output={"claims": ["合成主张：来源支持碎片中的核心说法"]})

    return {
        "fixture.input_fragment": input_fragment,
        "fixture.semantic_expansion": semantic_expansion,
        "fixture.route_decision": route_decision,
        "fixture.direct_perspective": direct_perspective,
        "fixture.synthesis_v1": synthesis_v1,
        "fixture.calibration_v2": calibration_v2,
        "fixture.memory_context": memory_context,
        "fixture.personal_perspective": personal_perspective,
        "fixture.synthetic_output": synthetic_output,
        "fixture.research_questions": research_questions,
        "fixture.source_fetch": source_fetch,
        "fixture.source_verification": source_verification,
        "fixture.claim_mapping": claim_mapping,
    }
