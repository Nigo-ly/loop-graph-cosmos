"""自主认知闭环 Research 宏图 spec `fragment-research-macro-v3`（TASK E）。

主图只表达业务阶段宏节点：界定问题 → 收集证据 → 覆盖度判断 →（缺口/冲突
反馈边，冻结 max_traversals=1，耗尽去 watching）→ 形成判断 → 人工决定 →
产出资产。transport/receipt/reservation/validator 等技术节点一律保留为
可展开 micro timeline，不占主图卡片。

反馈语义：`e_coverage_feedback` 把「覆盖度不足或来源冲突」反馈回采集段
（第二轮改述/反证，有界）；耗尽路由到 research_watch 观察出口（持久
watching + due 条件，绝不转用户待办）。人工 gate 只出现在候选结果/价值
选择（human_review）；模型授权、预算、出域、不可逆动作、正式资产发布
全部仍在该闸门之后。

旧 Research v1（fragment-research-escalation-v1）与宏 v2 只读不变；
本 spec 只用于新 run，旧 run 绝不反向补造。
"""

from __future__ import annotations

from typing import Any

from graph_runtime.spec import (
    HUMAN_GATE_BINDING_FIELDS,
    GraphSpec,
    validate_graph_spec,
)

RESEARCH_MACRO_V3_GRAPH_ID = "fragment-research-macro-v3"
RESEARCH_MACRO_V3_VERSION = "3.0.0"

ADAPTER_MACRO_INPUT = "research.macro.input"
ADAPTER_MACRO_COLLECT = "research.macro.evidence_collection"
ADAPTER_MACRO_COVERAGE = "research.macro.coverage_check"
ADAPTER_MACRO_JUDGE = "research.macro.judgment"
ADAPTER_MACRO_PRODUCE = "research.macro.asset_production"
ADAPTER_MACRO_AUTHZ_CHECK = "research.macro.authorization_check"
# 与 v1 同一 adapter id（复用 research_adapters 注册表中的既有 handler）。
ADAPTER_BRANCH_MARKER = "research.branch_marker"

INPUT_NODE = "research_input"
COLLECT_NODE = "evidence_collection"
COVERAGE_NODE = "coverage_check"
AUTHORIZATION_CHECK_NODE = "synthesis_authorization_check"
AUTHZ_DERIVED_PASS = "authorization_derived_pass"
AUTHZ_APPROVE_PASS = "authorization_approve_pass"
READY_JOIN = "authorization_ready_join"
JUDGE_NODE = "judgment"
AUTHORIZATION_GATE = "synthesis_authorization_gate"
REVIEW_GATE = "human_review"
PRODUCE_NODE = "asset_production"
RESEARCH_OUTPUT = "research_output"
REJECTED_OUTPUT = "rejected_output"

MACRO_V3_NODES = (
    INPUT_NODE,
    COLLECT_NODE,
    COVERAGE_NODE,
    AUTHORIZATION_CHECK_NODE,
    AUTHZ_DERIVED_PASS,
    AUTHZ_APPROVE_PASS,
    READY_JOIN,
    JUDGE_NODE,
    AUTHORIZATION_GATE,
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
    max_traversals: int | None = None,
    on_exhausted: str | None = None,
    exhausted_to: str | None = None,
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
        "decision_source": "declared",
    }


def research_macro_v3_raw() -> dict[str, Any]:
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": RESEARCH_MACRO_V3_GRAPH_ID,
        "version": RESEARCH_MACRO_V3_VERSION,
        "goal": "自主研究宏图：收集证据 → 覆盖度判断 →（有界反馈）→ 形成判断 → 人工决定 → 产出资产",
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
                    "stop_reason": "string",
                },
            ),
            _node(
                COVERAGE_NODE,
                "capability",
                adapter=ADAPTER_MACRO_COVERAGE,
                input_schema={"evidence_count": "integer"},
                output_schema={
                    "ready": "boolean",
                    "watching": "boolean",
                    "needs_more": "boolean",
                    "gaps": "array",
                    "conflicts": "array",
                    "evidence_count": "integer",
                },
            ),
            _node(
                AUTHORIZATION_CHECK_NODE,
                "capability",
                adapter=ADAPTER_MACRO_AUTHZ_CHECK,
                input_schema={"evidence_count": "integer"},
                output_schema={
                    "outcome": "string",
                    "reason": "string",
                    "authorization_digest": "string",
                },
            ),
            # 授权路径 marker（v1 同构）：derived 直通与人工批准分别经
            # marker 汇聚到 READY_JOIN，再单入边进入判断。
            _node(
                AUTHZ_DERIVED_PASS,
                "capability",
                adapter=ADAPTER_BRANCH_MARKER,
                output_schema={"branch": "string", "passed": "boolean"},
            ),
            _node(
                AUTHZ_APPROVE_PASS,
                "capability",
                adapter=ADAPTER_BRANCH_MARKER,
                output_schema={"branch": "string", "passed": "boolean"},
            ),
            _node(
                READY_JOIN,
                "join",
                join={
                    "mode": "minimum_success",
                    "threshold": 1,
                    "on_partial_failure": "fail",
                    "cancel_remaining": False,
                },
                output_schema={"joined": "array", "skipped": "array"},
            ),
            _node(
                JUDGE_NODE,
                "capability",
                adapter=ADAPTER_MACRO_JUDGE,
                # judge 经授权检查/授权闸门两条入边到达（拍平后无稳定
                # evidence_count）；适配器不消费 inputs（只读 runner
                # 持久化事实），input_schema 保持空契约。
                input_schema={},
                output_schema={
                    "claim_summary": "string",
                    "supported": "boolean",
                    "judgment_status": "string",
                },
            ),
            # 模型授权治理闸门（rev6 P0）：证据充分但 synthesis/model 未开启
            # 时进入——认知态 awaiting_model_authorization；只有精确正文、
            # 预算、Receipt 经人闸授权后才重进 judgment。绝不是候选结果复核。
            _node(
                AUTHORIZATION_GATE,
                "human_decision",
                output_schema={"decision": "string"},
                human_gate={
                    "allowed_decisions": ["approve_synthesis"],
                    "timeout_policy": "pause",
                    "binding": list(HUMAN_GATE_BINDING_FIELDS),
                },
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
            _edge("e_collect_coverage", COLLECT_NODE, COVERAGE_NODE),
            # 覆盖度分支：covered（无缺口且无冲突）进入授权检查；真实耗尽或
            # 轮数用尽仍有缺口 → 观察出口（绝不进人工闸门、不成用户待办）。
            _edge(
                "e_coverage_judge",
                COVERAGE_NODE,
                AUTHORIZATION_CHECK_NODE,
                edge_type="condition",
                condition={"field": "ready", "op": "equals", "value": True},
                priority=1,
            ),
            # 缺口/冲突反馈边：回采集段做第二轮（改述或反证）；冻结有界。
            # 观察出口（rev6 P0-2）：轮尽/耗尽后 feedback exhausted 以
            # pause 策略把 run 置于非终态 paused（runtime 既有契约）——
            # coordinator 的 due cycle 取得证据后经 reopen_node 恢复同一
            # run 重进 coverage；绝不创建替代 run、不改写历史 edge、绝不
            # 是不可恢复的 terminal output。
            _edge(
                "e_coverage_feedback",
                COVERAGE_NODE,
                COLLECT_NODE,
                edge_type="feedback",
                condition={"field": "needs_more", "op": "equals", "value": True},
                max_traversals=1,
                on_exhausted="pause",
                exhausted_to=None,
            ),
            # 授权分支（rev6 P0-3，v1 同构线性结构）：derived（Receipt 已
            # 派生且核验通过）经 marker 汇聚进入判断；manual_required（无/
            # 过期 Receipt；macro 适配器把 research_live_disabled 即
            # synthesis 能力未开启归并到此——认知态
            # awaiting_model_authorization）进入治理闸门；其余 blocked
            # （材料漂移/输入不可用）无出边 → 诚实 blocked，绝不进闸门也
            # 绝不进候选复核。
            _edge(
                "e_authz_derived",
                AUTHORIZATION_CHECK_NODE,
                AUTHZ_DERIVED_PASS,
                edge_type="condition",
                condition={"field": "outcome", "op": "equals", "value": "derived"},
                priority=1,
            ),
            _edge(
                "e_authz_manual",
                AUTHORIZATION_CHECK_NODE,
                AUTHORIZATION_GATE,
                edge_type="condition",
                condition={"field": "outcome", "op": "equals", "value": "manual_required"},
                priority=2,
            ),
            _edge("e_derived_ready", AUTHZ_DERIVED_PASS, READY_JOIN),
            # 授权闸门：approve_synthesis 后经 marker 汇聚进入判断（冻结
            # 精确正文/预算/Receipt 人闸不变）；abort 进入拒绝出口。
            _edge(
                "e_authz_approve",
                AUTHORIZATION_GATE,
                AUTHZ_APPROVE_PASS,
                edge_type="condition",
                condition={"field": "decision", "op": "equals", "value": "approve_synthesis"},
                priority=1,
            ),
            _edge("e_approve_ready", AUTHZ_APPROVE_PASS, READY_JOIN),
            _edge("e_ready_judge", READY_JOIN, JUDGE_NODE),
            # 判断分支：只有 synthesized 且契约验证的候选结果才进入人工
            # 复核；其余（未授权/契约不完整）无出边 → 诚实 blocked。
            _edge(
                "e_judge_review",
                JUDGE_NODE,
                REVIEW_GATE,
                edge_type="condition",
                condition={"field": "supported", "op": "equals", "value": True},
                priority=1,
            ),
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


def build_research_macro_v3_spec() -> GraphSpec:
    return validate_graph_spec(research_macro_v3_raw())


RESEARCH_MACRO_V3_SPEC = build_research_macro_v3_spec()
RESEARCH_MACRO_V3_SPEC_DIGEST = RESEARCH_MACRO_V3_SPEC.digest
