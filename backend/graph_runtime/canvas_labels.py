"""Safe display labels for the Graph canvas projection (GRAPH-CANVAS-V1-DESIGN.md §1.2).

Every user-facing label is registered here, bound to an exact
``(graph_id, spec_digest)`` pair captured from the frozen in-process spec
registry: if a spec changes, its digest changes and the stale labels are
withheld — the projection degrades to generic names instead of presenting
outdated wording. Nothing here is derived from prompts, goals, outputs,
condition values, or note bodies; unknown graphs/nodes/edges degrade
honestly ("未命名工作流" / kind label / "未知节点" / "条件").
"""

from __future__ import annotations

from typing import Any

CANVAS_VERSION = "1"

# (graph_id, spec_digest) → 安全任务名。digest 必须与冻结 spec 完全一致。
_TASK_LABELS: dict[tuple[str, str], str] = {
    (
        "fragment-cognitive-v1",
        "bc015e424ff34a24fa70f306cd721371c1ed4a3a5094d05149140d181552fc9f",
    ): "碎片认知整理",
    (
        "fragment-cognitive-agent-pilot-v2",
        "b6487f379d46c32a5f1aa52883c544e3516b893c8a14ae5577bc9cbc92602e16",
    ): "碎片认知 Agent Pilot",
    (
        "fragment-research-v1",
        "562a74ef1cee8df4d18593c59ef8fea0754d64a5c7ce95dc2fb4d2a152874b0f",
    ): "碎片研究子图",
    (
        "fragment-pilot-v1",
        "7b8d2a1fbd46893b1b2fa02b64ed6a5b09b4fafc4d80ba8e49e11df649019354",
    ): "真实碎片 Pilot",
    (
        "fragment-research-escalation-v1",
        "3b7b4fd731a09dd3949b944d04430badb52038f170ad97f73545ff0f6b7f94e0",
    ): "碎片受治理研究升级",
}

# graph_id → {node_id: 中文名}。仅在 spec_digest 匹配时生效。
_NODE_LABELS: dict[str, dict[str, str]] = {
    "fragment-cognitive-v1": {
        "input_fragment": "碎片输入",
        "semantic_expansion": "语义展开",
        "route_decision": "路线判断",
        "research_branch": "研究子图",
        "direct_perspective": "直接视角",
        "route_join": "路线汇合",
        "synthesis_v1": "综合初稿",
        "calibration_v2": "校准验证",
        "memory_context": "记忆上下文",
        "personal_perspective": "个人化视角",
        "final_join": "最终汇合",
        "human_confirmation": "人工确认",
        "synthetic_output": "产出动作",
        "run_rejected": "已拒绝结束",
    },
    "fragment-cognitive-agent-pilot-v2": {
        "pilot_input": "Pilot 输入",
        "pre_call_gate": "调用前人工闸门",
        "call_preparation": "调用准备",
        "agent_live_drafter": "Agent 草稿生成",
        "output_validator": "输出验证",
        "post_call_gate": "调用后人工闸门",
        "draft_output": "草稿产出",
        "run_aborted": "已中止结束",
        "run_rejected": "已拒绝结束",
    },
    "fragment-research-v1": {
        "research_questions": "研究问题",
        "source_fetch": "来源获取",
        "source_verification": "来源核验",
        "claim_mapping": "主张映射",
        "research_done": "研究完成",
    },
    "fragment-pilot-v1": {
        "pilot_input": "候选输入",
        "pre_call_gate": "调用前人工闸门",
        "draft_preparation": "调用准备",
        "agent_drafter": "Agent 草稿生成",
        "output_validator": "输出验证",
        "post_call_gate": "调用后人工闸门",
        "draft_output": "未验证草稿",
        "rejected_output": "已拒绝结束",
        "aborted_output": "已中止结束",
    },
    "fragment-research-escalation-v1": {
        "research_input": "研究输入",
        "official_research": "官方来源研究",
        "community_research": "社区来源研究",
        "evidence_join": "证据汇合",
        "synthesis_preparation": "合成准备",
        "synthesis_authorization_check": "合成授权检查",
        "authorization_auto_pass": "自动派生通过标记",
        "authorization_block_pass": "授权阻断标记",
        "authorization_manual_approve_pass": "人工批准标记",
        "authorization_manual_abort_pass": "人工中止标记",
        "synthesis_authorization_gate": "合成授权闸门",
        "authorization_ready_join": "授权就绪汇合",
        "authorization_abort_join": "授权中止汇合",
        "synthesis_agent": "证据合成",
        "output_validator": "输出验证",
        "result_human_review": "结果人工复核",
        "research_output": "研究结果",
        "rejected_output": "已拒绝结束",
        "aborted_output": "已中止结束",
    },
}

# (graph_id, edge_id) → 固定中文边说明（注册时审定，不由条件值动态生成）。
_EDGE_LABELS: dict[tuple[str, str], str] = {
    ("fragment-cognitive-v1", "e_route_research"): "需要研究",
    ("fragment-cognitive-v1", "e_route_direct"): "直接整理",
    ("fragment-cognitive-v1", "e_human_approve"): "人工确认通过",
    ("fragment-cognitive-v1", "e_human_reject"): "人工拒绝",
    ("fragment-cognitive-agent-pilot-v2", "e_pre_gate_approve"): "批准调用",
    ("fragment-cognitive-agent-pilot-v2", "e_pre_gate_abort"): "中止运行",
    ("fragment-cognitive-agent-pilot-v2", "e_post_gate_accept"): "接受草稿",
    ("fragment-cognitive-agent-pilot-v2", "e_post_gate_reject"): "拒绝草稿",
    ("fragment-pilot-v1", "e_pre_gate_approve"): "授权并执行",
    ("fragment-pilot-v1", "e_pre_gate_abort"): "中止运行",
    ("fragment-pilot-v1", "e_post_accept"): "接受为草稿",
    ("fragment-pilot-v1", "e_post_reject"): "拒绝草稿",
    ("fragment-pilot-v1", "e_validator_feedback"): "验证返修",
    ("fragment-research-escalation-v1", "e_authz_auto"): "授权自动派生",
    ("fragment-research-escalation-v1", "e_authz_manual"): "需要人工签发",
    ("fragment-research-escalation-v1", "e_authz_block"): "授权阻断",
    ("fragment-research-escalation-v1", "e_gate_approve"): "批准合成",
    ("fragment-research-escalation-v1", "e_gate_abort"): "中止研究",
    ("fragment-research-escalation-v1", "e_review_accept"): "接受研究结果",
    ("fragment-research-escalation-v1", "e_review_reject"): "拒绝研究结果",
}

# (graph_id, node_id) → 已审定的一句话安全结果摘要；不从 output 正文生成。
_RESULT_SUMMARIES: dict[tuple[str, str], str] = {
    ("fragment-cognitive-agent-pilot-v2", "draft_output"): "已生成未验证草稿",
    ("fragment-cognitive-v1", "synthetic_output"): "已生成待确认产出",
    ("fragment-pilot-v1", "draft_output"): "已接受为 Graph 内未验证草稿",
    ("fragment-pilot-v1", "rejected_output"): "草稿已拒绝",
    ("fragment-pilot-v1", "aborted_output"): "已在调用前中止",
    ("fragment-research-escalation-v1", "research_output"): "已接受受治理研究结果",
    ("fragment-research-escalation-v1", "rejected_output"): "研究结果已拒绝",
    ("fragment-research-escalation-v1", "aborted_output"): "研究已在授权处中止",
}

_KIND_LABELS = {
    "input": "输入",
    "router": "路由",
    "capability": "能力",
    "validator": "验证",
    "human_decision": "人工决定",
    "action": "动作",
    "subgraph": "子图",
    "join": "汇合",
    "output": "输出",
}

MAX_RESULT_SUMMARY_LENGTH = 120


def _digest_matches(graph_id: str, spec_digest: str) -> bool:
    return any(
        key_graph == graph_id and key_digest == spec_digest
        for key_graph, key_digest in _TASK_LABELS
    )


def task_label(graph_id: str, spec_digest: str) -> str:
    """安全任务名；未注册或 digest 漂移时诚实降级。"""
    label = _TASK_LABELS.get((graph_id, spec_digest))
    return label if label is not None else "未命名工作流"


def kind_label(kind: Any) -> str:
    return _KIND_LABELS.get(kind if isinstance(kind, str) else "", "未知类型")


def node_label(graph_id: str, spec_digest: str, node_id: str, kind: Any, ordinal: int) -> str:
    """节点中文名；digest 不匹配 → 类型名 + 短序号；kind 未知 →「未知节点」。"""
    if _digest_matches(graph_id, spec_digest):
        label = _NODE_LABELS.get(graph_id, {}).get(node_id)
        if label:
            return label
    if isinstance(kind, str) and kind in _KIND_LABELS:
        return f"{_KIND_LABELS[kind]}节点 {ordinal}"
    return "未知节点"


def edge_label(graph_id: str, spec_digest: str, edge_id: str) -> str | None:
    """固定映射边说明；无法安全映射时返回 None（UI 显示「条件」）。"""
    if not _digest_matches(graph_id, spec_digest):
        return None
    return _EDGE_LABELS.get((graph_id, edge_id))


def result_summary(graph_id: str, spec_digest: str, node_id: str) -> str | None:
    """已审定的一句话安全结果摘要；无映射时返回 None（字段省略）。"""
    if not _digest_matches(graph_id, spec_digest):
        return None
    summary = _RESULT_SUMMARIES.get((graph_id, node_id))
    if summary is None:
        return None
    # 注册表自身防护：超长或带控制字符的注册值视为未注册。
    if len(summary) > MAX_RESULT_SUMMARY_LENGTH or any(ord(ch) < 32 for ch in summary):
        return None
    return summary
