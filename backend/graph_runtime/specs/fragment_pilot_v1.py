"""真实碎片 Pilot GraphSpec `fragment-pilot-v1`（GRAPH-PILOT-ENTRY-BRIDGE-V1-DESIGN.md §2）。

独立于 Phase2B 合成 Pilot（fragment-cognitive-agent-pilot-v2）：真实候选输入、
双人工闸门、单受控 Agent 节点、Validator、最多一次 feedback、三个出口。
任何节点/边/adapter 漂移都会形成新的 spec_digest 并在创建时被拒绝。
"""

from __future__ import annotations

from typing import Any

from graph_runtime.spec import GraphSpec, validate_graph_spec

PILOT_GRAPH_ID = "fragment-pilot-v1"
PILOT_VERSION = "1.0.0"

ADAPTER_INPUT = "pilot.candidate_input"
ADAPTER_PREPARATION = "pilot.draft_preparation"
ADAPTER_AGENT = "pilot.agent_drafter"
ADAPTER_VALIDATOR = "pilot.output_validator"

PRE_GATE = "pre_call_gate"
POST_GATE = "post_call_gate"
PREP_NODE = "draft_preparation"
AGENT_NODE = "agent_drafter"
VALIDATOR_NODE = "output_validator"
DRAFT_OUTPUT = "draft_output"
REJECT_OUTPUT = "rejected_output"
ABORT_OUTPUT = "aborted_output"

# 安全前缀精确允许清单（rev6 冻结）：推进至首闸只允许经过这些节点/边/adapter。
PREFIX_NODES = ("pilot_input",)
PREFIX_EDGES = ("e_input_pre_gate",)
PREFIX_ADAPTERS = (ADAPTER_INPUT,)

_GATE_BINDING = [
    "run_id",
    "node_id",
    "decision",
    "spec_digest",
    "input_digest",
    "expected_sequence",
    "requester",
    "decision_id",
]



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
    max_traversals: int | None = None,
    on_exhausted: str | None = None,
    exhausted_to: str | None = None,
    decision_source: str = "declared",
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "from": from_node,
        "to": to_node,
        "type": edge_type,
        "condition": condition,
        "priority": priority,
        "max_traversals": max_traversals,
        "on_exhausted": on_exhausted,
        "exhausted_to": exhausted_to,
        "decision_source": decision_source,
    }


def _decision(value: str) -> dict[str, Any]:
    return {"field": "decision", "op": "equals", "value": value}


def pilot_raw() -> dict[str, Any]:
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": PILOT_GRAPH_ID,
        "version": PILOT_VERSION,
        "goal": "真实碎片 Pilot：候选认知投影 → 双人工闸门 → 受控草稿生成（未验证）",
        "entry_node": "pilot_input",
        "policy_profile": "graph-pilot-entry-bridge-v1",
        "nodes": [
            _node(
                "pilot_input",
                "input",
                adapter=ADAPTER_INPUT,
                output_schema={
                    "title": "string",
                    "core_judgment": "string",
                    "user_value": "string",
                    "card_titles": "array",
                },
            ),
            _node(
                PRE_GATE,
                "human_decision",
                input_schema={"title": "string"},
                output_schema={"decision": "string"},
                human_gate={
                    "allowed_decisions": ["approve_call", "abort"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
            ),
            _node(
                PREP_NODE,
                "capability",
                adapter=ADAPTER_PREPARATION,
                input_schema={"decision": "string"},
                output_schema={
                    "decision": "string",
                    "candidate_json": "string",
                    "input_digest": "string",
                },
            ),
            _node(
                AGENT_NODE,
                "capability",
                adapter=ADAPTER_AGENT,
                input_schema={"candidate_json": "string"},
                output_schema={
                    "summary": "string",
                    "unknowns": "array",
                    "next_checks": "array",
                },
            ),
            _node(
                VALIDATOR_NODE,
                "validator",
                adapter=ADAPTER_VALIDATOR,
                input_schema={"summary": "string"},
                output_schema={
                    "valid": "boolean",
                    "summary": "string",
                    "unknowns": "array",
                    "next_checks": "array",
                    "result_digest": "string",
                },
            ),
            _node(
                POST_GATE,
                "human_decision",
                output_schema={"decision": "string"},
                human_gate={
                    "allowed_decisions": ["accept_draft", "reject"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
            ),
            _node(DRAFT_OUTPUT, "output", input_schema={"decision": "string"}),
            _node(REJECT_OUTPUT, "output", input_schema={"decision": "string"}),
            _node(ABORT_OUTPUT, "output", input_schema={"decision": "string"}),
        ],
        "edges": [
            _edge("e_input_pre_gate", "pilot_input", PRE_GATE),
            _edge(
                "e_pre_gate_approve", PRE_GATE, PREP_NODE,
                edge_type="condition", condition=_decision("approve_call"), priority=1,
            ),
            _edge(
                "e_pre_gate_abort", PRE_GATE, ABORT_OUTPUT,
                edge_type="condition", condition=_decision("abort"), priority=2,
            ),
            _edge("e_preparation_agent", PREP_NODE, AGENT_NODE),
            _edge("e_agent_validator", AGENT_NODE, VALIDATOR_NODE),
            _edge(
                "e_validator_feedback",
                VALIDATOR_NODE,
                AGENT_NODE,
                edge_type="feedback",
                condition={"field": "error", "op": "equals", "value": "validation_failed"},
                max_traversals=1,
                on_exhausted="route",
                exhausted_to=POST_GATE,
                decision_source="rule_evaluated",
            ),
            _edge("e_validator_post_gate", VALIDATOR_NODE, POST_GATE),
            _edge(
                "e_post_accept", POST_GATE, DRAFT_OUTPUT,
                edge_type="condition", condition=_decision("accept_draft"), priority=1,
            ),
            _edge(
                "e_post_reject", POST_GATE, REJECT_OUTPUT,
                edge_type="condition", condition=_decision("reject"), priority=2,
            ),
        ],
        "subgraphs": [],
        "budgets": {"max_node_executions": 16},
    }


def build_fragment_pilot_spec() -> GraphSpec:
    return validate_graph_spec(pilot_raw())


PILOT_SPEC = build_fragment_pilot_spec()
SPEC_DIGEST = PILOT_SPEC.digest
