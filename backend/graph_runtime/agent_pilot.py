"""Pilot GraphSpec `fragment-cognitive-agent-pilot-v1`（Graph Phase 2A，冻结切片）。

独立的 Pilot 图，不修改 `fragment-cognitive-v1` 或 GraphSpec v1 schema（规范
中没有任何供应商字段；供应商治理只属于注册 Adapter 的 Policy）。结构：

    确定性输入与清理后文本摘要
    → 调用前人工闸门（approve_call / abort）
    → 确定性调用准备节点（把闸门决定与清理文本绑定为规范化输入）
    → 单个 capability Agent 节点（注册表中的受控 Agent Adapter）
    → 确定性输出 schema 校验器（失败最多反馈一次，耗尽后暂停，不自动再发）
    → 调用后人工闸门（accept_draft / reject）
    → 无副作用输出节点（结果固定草稿且 unverified）

Phase 2A 只通过内存假 Provider 运行这张图；不产生任何外部写入、资产发布或
证据等级提升。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from graph_runtime.agent_adapter import (
    FAKE_MODEL,
    FAKE_PROVIDER,
    TransportRequest,
    TransportResponse,
)
from graph_runtime.agent_policy import (
    AgentPolicy,
    AuthorizationReceipt,
    authorization_digest_of,
)
from graph_runtime.runtime import NodeRequest, NodeResult
from graph_runtime.spec import GraphSpec, digest_of, validate_graph_spec

PILOT_GRAPH_ID = "fragment-cognitive-agent-pilot-v1"
PILOT_POLICY_PROFILE = "graph-phase2a-agent-pilot"
PILOT_AGENT_ADAPTER = "agent.summarizer"

PRE_GATE = "pre_call_gate"
PREP_NODE = "call_preparation"
AGENT_NODE = "agent_summarizer"
VALIDATOR_NODE = "output_validator"
POST_GATE = "post_call_gate"
DRAFT_OUTPUT = "draft_output"
ABORT_OUTPUT = "run_aborted"
REJECT_OUTPUT = "run_rejected"

# 调用后闸门只接受草稿或拒绝；结果永远是草稿且未验证。
DRAFT_EVIDENCE_NOTE = "草稿、未验证、无外部写入"

_EDGE_SEQUENCE = {"type": "sequence", "decision_source": "declared"}
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


def agent_pilot_v1_raw() -> dict[str, Any]:
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": PILOT_GRAPH_ID,
        "version": "1.0.0",
        "goal": "受控 Agent 接入隔离 Pilot：双人工闸门、单能力 Agent 节点、有界反馈、草稿输出",
        "entry_node": "pilot_input",
        "nodes": [
            {
                "id": "pilot_input",
                "kind": "input",
                "adapter": "fixture.pilot_input",
                "input_schema": {},
                "output_schema": {"sanitized_text": "string", "text_digest": "string"},
            },
            {
                "id": PRE_GATE,
                "kind": "human_decision",
                "human_gate": {
                    "allowed_decisions": ["approve_call", "abort"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
                "input_schema": {"sanitized_text": "string"},
                "output_schema": {"decision": "string"},
            },
            {
                "id": PREP_NODE,
                "kind": "capability",
                "adapter": "fixture.call_preparation",
                "input_schema": {"decision": "string"},
                "output_schema": {
                    "sanitized_text": "string",
                    "text_digest": "string",
                    "decision": "string",
                },
            },
            {
                "id": AGENT_NODE,
                "kind": "capability",
                "adapter": PILOT_AGENT_ADAPTER,
                "input_schema": {"decision": "string"},
                "output_schema": {"draft_text": "string", "agent_note": "string"},
            },
            {
                "id": VALIDATOR_NODE,
                "kind": "validator",
                "adapter": "fixture.pilot_output_validator",
                "input_schema": {"draft_text": "string"},
                "output_schema": {
                    "valid": "boolean",
                    "draft_text": "string",
                    "draft_digest": "string",
                    "evidence_note": "string",
                },
            },
            {
                "id": POST_GATE,
                "kind": "human_decision",
                "human_gate": {
                    "allowed_decisions": ["accept_draft", "reject"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
                "input_schema": {"valid": "boolean"},
                "output_schema": {"decision": "string"},
            },
            {
                "id": DRAFT_OUTPUT,
                "kind": "output",
                "input_schema": {"decision": "string"},
                "output_schema": {},
            },
            {
                "id": ABORT_OUTPUT,
                "kind": "output",
                "input_schema": {"decision": "string"},
                "output_schema": {},
            },
            {
                "id": REJECT_OUTPUT,
                "kind": "output",
                "input_schema": {"decision": "string"},
                "output_schema": {},
            },
        ],
        "edges": [
            {
                "id": "e_input_pre_gate",
                "from": "pilot_input",
                "to": PRE_GATE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_pre_gate_approve",
                "from": PRE_GATE,
                "to": PREP_NODE,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "approve_call"},
                "priority": 0,
                "decision_source": "human_selected",
            },
            {
                "id": "e_pre_gate_abort",
                "from": PRE_GATE,
                "to": ABORT_OUTPUT,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "abort"},
                "priority": 1,
                "decision_source": "human_selected",
            },
            {
                "id": "e_preparation_agent",
                "from": PREP_NODE,
                "to": AGENT_NODE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_agent_validator",
                "from": AGENT_NODE,
                "to": VALIDATOR_NODE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_validator_feedback",
                "from": VALIDATOR_NODE,
                "to": AGENT_NODE,
                "type": "feedback",
                "condition": {"field": "error", "op": "equals", "value": "agent_output_invalid"},
                "max_traversals": 1,
                "on_exhausted": "pause",
                "decision_source": "rule_evaluated",
            },
            {
                "id": "e_validator_post_gate",
                "from": VALIDATOR_NODE,
                "to": POST_GATE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_post_gate_accept",
                "from": POST_GATE,
                "to": DRAFT_OUTPUT,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "accept_draft"},
                "priority": 0,
                "decision_source": "human_selected",
            },
            {
                "id": "e_post_gate_reject",
                "from": POST_GATE,
                "to": REJECT_OUTPUT,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "reject"},
                "priority": 1,
                "decision_source": "human_selected",
            },
        ],
        "budgets": {"max_node_executions": 32},
        "policy_profile": PILOT_POLICY_PROFILE,
        "subgraphs": [],
    }


def build_agent_pilot_spec() -> GraphSpec:
    return validate_graph_spec(agent_pilot_v1_raw())


def make_pilot_fixture_adapters(
    sanitized_text: str,
    *,
    validator_failures: int = 0,
) -> dict[str, Any]:
    """Pilot 图的确定性合成节点（输入与校验器）；Agent 节点本身由
    ``ControlledAgentAdapter`` 提供，不在此注册。

    ``validator_failures`` 是显式的有界失败计数：校验器先失败这么多次
    （触发反馈边），随后对相同输入确定性通过。
    """
    state = {"validator_failures_left": validator_failures}

    def pilot_input(_request: NodeRequest) -> NodeResult:
        digest = hashlib.sha256(sanitized_text.encode("utf-8")).hexdigest()
        return NodeResult(output={"sanitized_text": sanitized_text, "text_digest": digest})

    def call_preparation(request: NodeRequest) -> NodeResult:
        """把调用前闸门的决定与清理文本绑定为 Agent 节点的规范化输入。"""
        decision = str(request.inputs.get(PRE_GATE, {}).get("decision", ""))
        digest = hashlib.sha256(sanitized_text.encode("utf-8")).hexdigest()
        return NodeResult(
            output={
                "sanitized_text": sanitized_text,
                "text_digest": digest,
                "decision": decision,
            }
        )

    def pilot_output_validator(request: NodeRequest) -> NodeResult:
        if state["validator_failures_left"] > 0:
            state["validator_failures_left"] -= 1
            return NodeResult(
                output={"weakest_link": "合成校验缺口：草稿未通过确定性 schema 校验"},
                failure="agent_output_invalid",
            )
        draft_text = str(request.inputs.get(AGENT_NODE, {}).get("draft_text", ""))
        if not draft_text.strip():
            return NodeResult(
                output={"weakest_link": "草稿为空"},
                failure="agent_output_invalid",
            )
        draft_digest = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
        return NodeResult(
            output={
                "valid": True,
                "draft_text": draft_text,
                "draft_digest": draft_digest,
                "evidence_note": DRAFT_EVIDENCE_NOTE,
            }
        )

    return {
        "fixture.pilot_input": pilot_input,
        "fixture.call_preparation": call_preparation,
        "fixture.pilot_output_validator": pilot_output_validator,
    }


# -- Phase 2A 假 Provider fixture 助手 ----------------------------------------
#
# 以下助手只构造离线、确定性的测试资产：固定 live_enabled=False 的 Policy、
# 内存假 Provider responder 与一次性授权收据。它们不读取任何真实数据、
# 不联网、不构造真实 Transport。

DEFAULT_DRAFT_TEXT = "Pilot 草稿（内存假 Provider 合成，未验证）"
DEFAULT_AGENT_NOTE = "草稿、未验证、无外部写入；仅供 Phase 2A 离线运行"
DEFAULT_PHRASE = "phase2a-fake-authorization-phrase"


def make_pilot_policy() -> AgentPolicy:
    """Phase 2A 固定假 Provider Policy：live_enabled=False，max_calls=1。"""
    return AgentPolicy(
        adapter=PILOT_AGENT_ADAPTER,
        provider=FAKE_PROVIDER,
        model=FAKE_MODEL,
        capability="draft_synthesis",
        max_calls=1,
        max_input_tokens=512,
        max_output_tokens=512,
        max_input_bytes=8192,
        timeout_seconds=30,
        live_enabled=False,
    )


def make_fake_provider_responder(
    *,
    draft_text: str = DEFAULT_DRAFT_TEXT,
    agent_note: str = DEFAULT_AGENT_NOTE,
    input_tokens: int = 12,
    output_tokens: int = 24,
    declared_provider: str = FAKE_PROVIDER,
    declared_model: str = FAKE_MODEL,
) -> Callable[[TransportRequest], TransportResponse]:
    """确定性内存假 Provider 响应脚本；同样输入永远同样响应。"""

    def respond(_request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            declared_provider=declared_provider,
            declared_model=declared_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            payload={"draft_text": draft_text, "agent_note": agent_note},
        )

    return respond


def expected_agent_input_digest(sanitized_text: str, decision: str) -> str:
    """Agent 节点规范化输入摘要（flatten 后的精确形状）。"""
    result: str = digest_of(
        {
            "sanitized_text": sanitized_text,
            "text_digest": hashlib.sha256(sanitized_text.encode("utf-8")).hexdigest(),
            "decision": decision,
        }
    )
    return result


def make_pilot_receipt(
    policy: AgentPolicy,
    *,
    run_id: str,
    spec_digest: str,
    input_digest: str,
    phrase: str = DEFAULT_PHRASE,
    node_id: str = AGENT_NODE,
    issued_at: str | None = None,
    expires_at: str | None = None,
) -> AuthorizationReceipt:
    """一次性授权收据；只保存短语摘要，短语本体从不持久化。"""
    now = datetime.now(UTC)
    return AuthorizationReceipt(
        run_id=run_id,
        node_id=node_id,
        spec_digest=spec_digest,
        input_digest=input_digest,
        adapter=policy.adapter,
        provider=policy.provider,
        model=policy.model,
        max_calls=policy.max_calls,
        max_input_tokens=policy.max_input_tokens,
        max_output_tokens=policy.max_output_tokens,
        max_input_bytes=policy.max_input_bytes,
        authorization_digest=authorization_digest_of(phrase, authorized_by="nigo"),
        authorized_by="nigo",
        issued_at=issued_at or (now - timedelta(minutes=1)).isoformat(),
        expires_at=expires_at or (now + timedelta(minutes=10)).isoformat(),
    )
