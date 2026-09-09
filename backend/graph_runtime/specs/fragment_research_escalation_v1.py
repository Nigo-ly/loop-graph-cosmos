"""Fragment Governed Research v1 的升级 GraphSpec `fragment-research-escalation-v1`。

拓扑逐字按 FRAGMENT-GOVERNED-RESEARCH-PROGRESSIVE-ESCALATION-V1-DESIGN.md §5.1
（rev6 冻结）：19 节点 / 21 边、无 feedback 边、模型发送恒 ≤1。

关键语义（rev6 冻结）：
- 四个 ``research.branch_marker`` 无副作用纯确定性分支标记节点是两个 join 的唯一直接
  来源；失败侧 join 的全部来源均为 skipped → join 整体 skipped 不被评估，从结构上
  消除 rev5 无 marker 拓扑的双触发（ready_join 与 abort_join 同时 succeeded）。
- 两个人工闸门：合成授权闸门（approve_synthesis / abort）与结果人工复核
  （accept_result / reject）；不伪造 human decision。
- 任何节点/边/adapter 漂移都会形成新的 spec_digest 并在创建时被拒绝。
"""

from __future__ import annotations

from typing import Any

from graph_runtime.spec import (
    HUMAN_GATE_BINDING_FIELDS,
    GraphSpec,
    validate_graph_spec,
)

RESEARCH_GRAPH_ID = "fragment-research-escalation-v1"
RESEARCH_GRAPH_VERSION = "1.0.0"

ADAPTER_INPUT = "research.input"
ADAPTER_OFFICIAL = "research.official_fetch"
ADAPTER_COMMUNITY = "research.community_fetch"
ADAPTER_PREPARATION = "research.synthesis_preparation"
ADAPTER_AUTHORIZATION_CHECK = "research.authorization_check"
ADAPTER_BRANCH_MARKER = "research.branch_marker"
ADAPTER_SYNTHESIS = "research.evidence_synthesizer"
ADAPTER_VALIDATOR = "research.output_validator"

INPUT_NODE = "research_input"
OFFICIAL_NODE = "official_research"
COMMUNITY_NODE = "community_research"
EVIDENCE_JOIN = "evidence_join"
PREPARATION_NODE = "synthesis_preparation"
AUTHORIZATION_CHECK_NODE = "synthesis_authorization_check"
AUTO_PASS_NODE = "authorization_auto_pass"
BLOCK_PASS_NODE = "authorization_block_pass"
MANUAL_APPROVE_PASS_NODE = "authorization_manual_approve_pass"
MANUAL_ABORT_PASS_NODE = "authorization_manual_abort_pass"
AUTHORIZATION_GATE = "synthesis_authorization_gate"
READY_JOIN = "authorization_ready_join"
ABORT_JOIN = "authorization_abort_join"
SYNTHESIS_NODE = "synthesis_agent"
VALIDATOR_NODE = "output_validator"
REVIEW_GATE = "result_human_review"
RESEARCH_OUTPUT = "research_output"
REJECTED_OUTPUT = "rejected_output"
ABORTED_OUTPUT = "aborted_output"

# 安全前缀精确允许清单（DESIGN §5.3 rev5 冻结）：一次受治理创建操作推进至授权
# 检查只允许经过这些节点/边，且全部 adapter 为确定性（零模型、零网络发送）。
PREFIX_NODES = (
    INPUT_NODE,
    OFFICIAL_NODE,
    COMMUNITY_NODE,
    EVIDENCE_JOIN,
    PREPARATION_NODE,
    AUTHORIZATION_CHECK_NODE,
)
PREFIX_EDGES = (
    "e_input_official",
    "e_input_community",
    "e_official_join",
    "e_community_join",
    "e_join_preparation",
    "e_preparation_authz",
)
PREFIX_ADAPTERS = (
    ADAPTER_INPUT,
    ADAPTER_OFFICIAL,
    ADAPTER_COMMUNITY,
    ADAPTER_PREPARATION,
    ADAPTER_AUTHORIZATION_CHECK,
)

_GATE_BINDING = list(HUMAN_GATE_BINDING_FIELDS)


def _node(
    node_id: str,
    kind: str,
    *,
    adapter: str | None = None,
    input_schema: dict[str, str] | None = None,
    output_schema: dict[str, str] | None = None,
    join: dict[str, Any] | None = None,
    human_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "adapter": adapter,
        "input_schema": input_schema or {},
        "output_schema": output_schema or {},
        "join": join,
        "human_gate": human_gate,
        "action_policy": None,
        "subgraph_ref": None,
    }


def _edge(
    edge_id: str,
    from_node: str,
    to_node: str,
    *,
    edge_type: str = "sequence",
    condition: dict[str, Any] | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "from": from_node,
        "to": to_node,
        "type": edge_type,
        "condition": condition,
        "priority": priority,
        "max_traversals": None,
        "on_exhausted": None,
        "exhausted_to": None,
        "decision_source": "declared",
    }


def _outcome(value: str) -> dict[str, Any]:
    return {"field": "outcome", "op": "equals", "value": value}


def _decision(value: str) -> dict[str, Any]:
    return {"field": "decision", "op": "equals", "value": value}


def _join(mode: str, threshold: int | None, on_partial_failure: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "threshold": threshold,
        "on_partial_failure": on_partial_failure,
        "cancel_remaining": False,
    }


def _marker_node(node_id: str) -> dict[str, Any]:
    return _node(
        node_id,
        "capability",
        adapter=ADAPTER_BRANCH_MARKER,
        output_schema={"branch": "string", "passed": "boolean"},
    )


def research_raw() -> dict[str, Any]:
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": RESEARCH_GRAPH_ID,
        "version": RESEARCH_GRAPH_VERSION,
        "goal": "受治理研究升级：证据继承与缺口补采 → 授权检查 → 最多一次受治理合成 → 人工复核",
        "entry_node": INPUT_NODE,
        "policy_profile": "fragment-research-escalation-v1",
        "nodes": [
            _node(
                INPUT_NODE,
                "input",
                adapter=ADAPTER_INPUT,
                output_schema={
                    "research_goal": "string",
                    "alignment_id": "string",
                    "episode_id": "string",
                    "escalation_id": "string",
                    "evidence_bundle_digest": "string",
                },
            ),
            _node(
                OFFICIAL_NODE,
                "capability",
                adapter=ADAPTER_OFFICIAL,
                input_schema={"research_goal": "string"},
                output_schema={
                    "scope": "string",
                    "record_count": "integer",
                    "inherited_count": "integer",
                    "newly_collected_count": "integer",
                    "evidence_digests": "array",
                    "reused": "boolean",
                },
            ),
            _node(
                COMMUNITY_NODE,
                "capability",
                adapter=ADAPTER_COMMUNITY,
                input_schema={"research_goal": "string"},
                output_schema={
                    "scope": "string",
                    "record_count": "integer",
                    "inherited_count": "integer",
                    "newly_collected_count": "integer",
                    "evidence_digests": "array",
                    "reused": "boolean",
                },
            ),
            _node(
                EVIDENCE_JOIN,
                "join",
                join=_join("all_success", None, "continue"),
                output_schema={"joined": "array", "skipped": "array"},
            ),
            _node(
                PREPARATION_NODE,
                "capability",
                adapter=ADAPTER_PREPARATION,
                input_schema={"joined": "array", "skipped": "array"},
                output_schema={
                    "input_digest": "string",
                    "included_count": "integer",
                    "omitted_count": "integer",
                    "request_bytes": "integer",
                    "evidence_count": "integer",
                },
            ),
            _node(
                AUTHORIZATION_CHECK_NODE,
                "capability",
                adapter=ADAPTER_AUTHORIZATION_CHECK,
                input_schema={"input_digest": "string"},
                output_schema={
                    "outcome": "string",
                    "reason": "string",
                    "authorization_digest": "string",
                },
            ),
            _marker_node(AUTO_PASS_NODE),
            _marker_node(BLOCK_PASS_NODE),
            _marker_node(MANUAL_APPROVE_PASS_NODE),
            _marker_node(MANUAL_ABORT_PASS_NODE),
            _node(
                AUTHORIZATION_GATE,
                "human_decision",
                output_schema={"decision": "string"},
                human_gate={
                    "allowed_decisions": ["approve_synthesis", "abort"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
            ),
            _node(
                READY_JOIN,
                "join",
                join=_join("minimum_success", 1, "fail"),
                output_schema={"joined": "array", "skipped": "array"},
            ),
            _node(
                ABORT_JOIN,
                "join",
                join=_join("minimum_success", 1, "fail"),
                output_schema={"joined": "array", "skipped": "array"},
            ),
            _node(
                SYNTHESIS_NODE,
                "capability",
                adapter=ADAPTER_SYNTHESIS,
                input_schema={"joined": "array", "skipped": "array"},
                output_schema={
                    "status": "string",
                    "model_calls": "integer",
                    "stop_reason": "string",
                    "summary": "string",
                    "result_digest": "string",
                    "receipt_digest": "string",
                    "reason": "string",
                },
            ),
            _node(
                VALIDATOR_NODE,
                "validator",
                adapter=ADAPTER_VALIDATOR,
                input_schema={"status": "string"},
                output_schema={
                    "valid": "boolean",
                    "status": "string",
                    "summary": "string",
                    "result_digest": "string",
                },
            ),
            _node(
                REVIEW_GATE,
                "human_decision",
                output_schema={"decision": "string"},
                human_gate={
                    "allowed_decisions": ["accept_result", "reject"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
            ),
            _node(RESEARCH_OUTPUT, "output", input_schema={"decision": "string"}),
            _node(REJECTED_OUTPUT, "output", input_schema={"decision": "string"}),
            _node(
                ABORTED_OUTPUT,
                "output",
                input_schema={"joined": "array", "skipped": "array"},
            ),
        ],
        "edges": [
            _edge("e_input_official", INPUT_NODE, OFFICIAL_NODE),
            _edge("e_input_community", INPUT_NODE, COMMUNITY_NODE),
            _edge("e_official_join", OFFICIAL_NODE, EVIDENCE_JOIN),
            _edge("e_community_join", COMMUNITY_NODE, EVIDENCE_JOIN),
            _edge("e_join_preparation", EVIDENCE_JOIN, PREPARATION_NODE),
            _edge("e_preparation_authz", PREPARATION_NODE, AUTHORIZATION_CHECK_NODE),
            _edge(
                "e_authz_auto", AUTHORIZATION_CHECK_NODE, AUTO_PASS_NODE,
                edge_type="condition", condition=_outcome("derived"), priority=1,
            ),
            _edge(
                "e_authz_manual", AUTHORIZATION_CHECK_NODE, AUTHORIZATION_GATE,
                edge_type="condition", condition=_outcome("manual_required"), priority=2,
            ),
            _edge(
                "e_authz_block", AUTHORIZATION_CHECK_NODE, BLOCK_PASS_NODE,
                edge_type="condition", condition=_outcome("blocked"), priority=3,
            ),
            _edge(
                "e_gate_approve", AUTHORIZATION_GATE, MANUAL_APPROVE_PASS_NODE,
                edge_type="condition", condition=_decision("approve_synthesis"), priority=1,
            ),
            _edge(
                "e_gate_abort", AUTHORIZATION_GATE, MANUAL_ABORT_PASS_NODE,
                edge_type="condition", condition=_decision("abort"), priority=2,
            ),
            _edge("e_auto_ready", AUTO_PASS_NODE, READY_JOIN),
            _edge("e_manual_ready", MANUAL_APPROVE_PASS_NODE, READY_JOIN),
            _edge("e_block_abort", BLOCK_PASS_NODE, ABORT_JOIN),
            _edge("e_manual_abort", MANUAL_ABORT_PASS_NODE, ABORT_JOIN),
            _edge("e_ready_agent", READY_JOIN, SYNTHESIS_NODE),
            _edge("e_agent_validator", SYNTHESIS_NODE, VALIDATOR_NODE),
            _edge("e_validator_review", VALIDATOR_NODE, REVIEW_GATE),
            _edge(
                "e_review_accept", REVIEW_GATE, RESEARCH_OUTPUT,
                edge_type="condition", condition=_decision("accept_result"), priority=1,
            ),
            _edge(
                "e_review_reject", REVIEW_GATE, REJECTED_OUTPUT,
                edge_type="condition", condition=_decision("reject"), priority=2,
            ),
            _edge("e_abort_output", ABORT_JOIN, ABORTED_OUTPUT),
        ],
        "subgraphs": [],
        "budgets": {"max_node_executions": 32},
    }


def build_fragment_research_escalation_spec() -> GraphSpec:
    return validate_graph_spec(research_raw())


RESEARCH_SPEC = build_fragment_research_escalation_spec()
SPEC_DIGEST = RESEARCH_SPEC.digest
