"""G3 版本化 Research 宏图 spec `fragment-research-macro-v2`（Design §9.3）。

主图只表达业务阶段宏节点：界定问题 → 收集证据 → 形成判断 → 人工决定 →
产出资产。authorization check／receipt、branch marker／condition choice、
fan-in join／cancel、tool call／transport、reservation／lease／unknown
send、validator receipt／checkpoint commit 一律保留为可展开 micro
timeline（`graph_runtime.result_unit.micro_events`），不占主图卡片。

旧 Research v1（fragment-research-escalation-v1，19 节点／21 边）只读
不变；本 spec 只用于 G3 新 run，旧 run 绝不反向补造 ResultUnit。
"""

from __future__ import annotations

from typing import Any

from graph_runtime.spec import (
    HUMAN_GATE_BINDING_FIELDS,
    GraphSpec,
    validate_graph_spec,
)

RESEARCH_MACRO_GRAPH_ID = "fragment-research-macro-v2"
RESEARCH_MACRO_GRAPH_VERSION = "2.0.0"

ADAPTER_MACRO_INPUT = "research.macro.input"
ADAPTER_MACRO_COLLECT = "research.macro.evidence_collection"
ADAPTER_MACRO_JUDGE = "research.macro.judgment"
ADAPTER_MACRO_PRODUCE = "research.macro.asset_production"

INPUT_NODE = "research_input"
COLLECT_NODE = "evidence_collection"
JUDGE_NODE = "judgment"
REVIEW_GATE = "human_review"
PRODUCE_NODE = "asset_production"
RESEARCH_OUTPUT = "research_output"
REJECTED_OUTPUT = "rejected_output"

MACRO_NODES = (
    INPUT_NODE,
    COLLECT_NODE,
    JUDGE_NODE,
    REVIEW_GATE,
    PRODUCE_NODE,
    RESEARCH_OUTPUT,
    REJECTED_OUTPUT,
)


def _node(
    node_id: str,
    kind: str,
    *,
    adapter: str | None = None,
    input_schema: dict[str, str] | None = None,
    output_schema: dict[str, str] | None = None,
    human_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "adapter": adapter,
        "input_schema": input_schema or {},
        "output_schema": output_schema or {},
        "join": None,
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


def research_macro_raw() -> dict[str, Any]:
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": RESEARCH_MACRO_GRAPH_ID,
        "version": RESEARCH_MACRO_GRAPH_VERSION,
        "goal": "受治理研究宏图：界定问题 → 收集证据 → 形成判断 → 人工决定 → 产出资产",
        "entry_node": INPUT_NODE,
        "policy_profile": "fragment-research-escalation-v1",
        "nodes": [
            _node(
                INPUT_NODE,
                "input",
                adapter=ADAPTER_MACRO_INPUT,
                output_schema={"research_goal": "string"},
            ),
            _node(
                COLLECT_NODE,
                "capability",
                adapter=ADAPTER_MACRO_COLLECT,
                input_schema={"research_goal": "string"},
                output_schema={
                    "evidence_count": "integer",
                    "evidence_digests": "array",
                    "gap_needed": "boolean",
                },
            ),
            _node(
                JUDGE_NODE,
                "capability",
                adapter=ADAPTER_MACRO_JUDGE,
                input_schema={"evidence_count": "integer"},
                output_schema={"claim_summary": "string", "supported": "boolean"},
            ),
            _node(
                REVIEW_GATE,
                "human_decision",
                output_schema={"decision": "string"},
                human_gate={
                    "allowed_decisions": ["accept_result", "reject"],
                    "timeout_policy": "pause",
                    "binding": list(HUMAN_GATE_BINDING_FIELDS),
                },
            ),
            _node(
                PRODUCE_NODE,
                "capability",
                adapter=ADAPTER_MACRO_PRODUCE,
                input_schema={"decision": "string"},
                output_schema={"artifact_digest": "string", "summary": "string"},
            ),
            _node(RESEARCH_OUTPUT, "output", input_schema={"artifact_digest": "string"}),
            _node(REJECTED_OUTPUT, "output", input_schema={"decision": "string"}),
        ],
        "edges": [
            _edge("e_input_collect", INPUT_NODE, COLLECT_NODE),
            _edge("e_collect_judge", COLLECT_NODE, JUDGE_NODE),
            _edge("e_judge_review", JUDGE_NODE, REVIEW_GATE),
            _edge(
                "e_review_accept",
                REVIEW_GATE,
                PRODUCE_NODE,
                edge_type="condition",
                condition={"field": "decision", "op": "equals", "value": "accept_result"},
                priority=1,
            ),
            _edge(
                "e_review_reject",
                REVIEW_GATE,
                REJECTED_OUTPUT,
                edge_type="condition",
                condition={"field": "decision", "op": "equals", "value": "reject"},
                priority=2,
            ),
            _edge("e_produce_output", PRODUCE_NODE, RESEARCH_OUTPUT),
        ],
        "subgraphs": [],
        "budgets": {"max_node_executions": 16},
    }


def build_research_macro_spec() -> GraphSpec:
    return validate_graph_spec(research_macro_raw())


RESEARCH_MACRO_SPEC = build_research_macro_spec()
RESEARCH_MACRO_SPEC_DIGEST = RESEARCH_MACRO_SPEC.digest
