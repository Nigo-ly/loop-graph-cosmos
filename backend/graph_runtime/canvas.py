"""Read-only, deterministic canvas projection for Graph runs.

Implements the GRAPH-CANVAS-V1-DESIGN.md §1.1 (rev2) contract: the complete
node/edge structure of the frozen GraphSpec merged with the run's current
Checkpoint state, plus registry-bound safe Chinese labels. The projection is
pure: it reads the checkpoint store via the service, never writes, and the
same run state always yields byte-identical output (spec tuples and sorted
state maps give a stable order).

Never projected: GraphSpec goal text, prompts, response bodies, raw node
outputs, edge reasons, condition values, authorization phrases, credentials,
absolute paths, or private note bodies.
"""

from __future__ import annotations

from typing import Any

from graph_runtime import canvas_labels, pilot_adapters
from graph_runtime.projection import _state_of
from graph_runtime.spec import GraphSpec
from graph_runtime.specs.fragment_pilot_v1 import PILOT_GRAPH_ID, SPEC_DIGEST
from graph_runtime.specs.fragment_research_escalation_v1 import (
    RESEARCH_GRAPH_ID,
)
from graph_runtime.specs.fragment_research_escalation_v1 import (
    SPEC_DIGEST as RESEARCH_GRAPH_SPEC_DIGEST,
)

# Node statuses the runtime can produce; anything else is passed through
# verbatim so the client can render it as an honest unknown state.
_FRONTIER_STATUSES = frozenset(("ready", "waiting_human"))


def _join_projection(spec_node: Any) -> dict[str, Any] | None:
    join = spec_node.join
    if join is None:
        return None
    return {
        "mode": join.get("mode"),
        "threshold": join.get("threshold"),
        "on_partial_failure": join.get("on_partial_failure"),
        "cancel_remaining": join.get("cancel_remaining"),
    }


def _gate_projection(gate: dict[str, Any] | None) -> dict[str, Any] | None:
    if gate is None:
        return None
    allowed = gate.get("allowed_decisions")
    return {
        "status": gate.get("status"),
        "allowed_decisions": sorted(allowed) if isinstance(allowed, list) else [],
    }


# Pilot 闸门扩展（GRAPH-PILOT-ENTRY-BRIDGE-V1-DESIGN.md §3.4/§3.7）：闸门节点
# id → checkpoint eval_results 中的安全投影键。只投影已过滤的安全字段；
# Prompt、候选正文映射之外的材料、响应正文、价格页原文永不进入。
_GATE_EXTENSION_KEYS = {
    "pre_call_gate": "pilot_authorization",
    "post_call_gate": "pilot_result",
}


# Pilot 输入字段上限（与 pilot_adapters 冻结值一致）：超限整体省略，不截断。
_INPUT_FIELD_CAPS = {"title": 100, "core_judgment": 500, "user_value": 300}
_MAX_CARDS = 8
_MAX_CARD = 100
_PILOT_WRITE_SCOPE = "Graph 检查点 + Agent 账本；不写笔记、不写资产"
_RESEARCH_WRITE_SCOPE = "Graph 检查点 + Agent 账本；不写知识资产"


def _input_fields_projection(candidate: Any) -> dict[str, Any] | None:
    """首闸展示「将发送的安全字段」：与实发同一 canonical 对象（rev6）。
    任何字段超限或形状漂移 → 整体省略（None），绝不截断、绝不伪造。"""
    if not isinstance(candidate, dict):
        return None
    fields: dict[str, Any] = {}
    for key, cap in _INPUT_FIELD_CAPS.items():
        value = candidate.get(key)
        if not isinstance(value, str) or not value or len(value) > cap:
            return None
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            return None
        fields[key] = value
    cards = candidate.get("card_titles")
    if (
        not isinstance(cards, list)
        or len(cards) > _MAX_CARDS
        or not all(
            isinstance(item, str)
            and item
            and len(item) <= _MAX_CARD
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in item)
            for item in cards
        )
    ):
        return None
    fields["card_titles"] = list(cards)
    return fields


def _authorization_projection(stored: Any, candidate: Any = None) -> dict[str, Any] | None:
    if not isinstance(stored, dict):
        return None
    snapshot = stored.get("price_snapshot")
    projection: dict[str, Any] = {
        "authorization_digest": stored.get("authorization_digest"),
        "provider": stored.get("provider"),
        "model": stored.get("model"),
        "max_total_calls": stored.get("max_total_calls"),
        "cost_cap_cny": stored.get("cost_cap_cny"),
        "input_digest": stored.get("input_digest"),
        "agent_input_digest": stored.get("agent_input_digest"),
        "candidate_content_sha256": stored.get("candidate_content_sha256"),
        "expected_sequence": stored.get("expected_sequence"),
        "issued_at": stored.get("issued_at"),
        "expires_at": stored.get("expires_at"),
        "price_snapshot_source": (snapshot.get("source") if isinstance(snapshot, dict) else None),
    }
    input_fields = _input_fields_projection(candidate)
    if input_fields is not None:
        projection["input_fields"] = input_fields
    return projection


def _authorization_preview(
    graph_id: str, spec_digest: str, candidate: Any
) -> dict[str, Any] | None:
    """签发前的知情授权材料；只对冻结 Pilot spec 投影。

    它与 Receipt 共用同一组 Policy 常量和候选 canonical 安全字段，但不包含
    价格快照、授权摘要或任何可执行凭据。GET 保持纯只读、零外网。
    """
    if graph_id != PILOT_GRAPH_ID or spec_digest != SPEC_DIGEST:
        return None
    input_fields = _input_fields_projection(candidate)
    if input_fields is None:
        return None
    return {
        "provider": pilot_adapters.PILOT_PROVIDER,
        "model": pilot_adapters.PILOT_MODEL,
        "max_total_calls": pilot_adapters.MAX_TOTAL_CALLS,
        "cost_cap_cny": pilot_adapters.COST_CAP_CNY,
        "input_fields": input_fields,
        "write_scope": _PILOT_WRITE_SCOPE,
    }


def _result_projection(stored: Any) -> dict[str, Any] | None:
    if not isinstance(stored, dict):
        return None
    projection = {
        "available": stored.get("available") is True,
        "result_digest": stored.get("result_digest"),
    }
    if projection["available"]:
        projection["summary"] = stored.get("summary")
        projection["unknowns"] = stored.get("unknowns")
        projection["next_checks"] = stored.get("next_checks")
    return projection


def _research_preview(collection: Any) -> dict[str, Any] | None:
    if not isinstance(collection, dict):
        return None
    goal = collection.get("goal")
    records = collection.get("records")
    if not isinstance(goal, str) or not goal or len(goal.encode("utf-8")) > 200:
        return None
    if not isinstance(records, list):
        return None
    task_label = goal.split("（补充：", 1)[0].strip()
    if "https://" in task_label:
        task_label = task_label.split("https://", 1)[0].rstrip()
    if not task_label:
        return None
    sources = []
    for record in records:
        if not isinstance(record, dict) or record.get("marker") not in {
            "inherited",
            "newly_collected",
        }:
            continue
        title, url = record.get("title"), record.get("url")
        if isinstance(title, str) and isinstance(url, str):
            sources.append({"title": title[:300], "url": url[:2048]})
    return {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "max_total_calls": 1,
        "cost_cap_cny": 2,
        "research_goal": goal,
        "task_label": task_label[:120],
        "evidence_count": len(sources),
        "sources": sources[:8],
        "write_scope": _RESEARCH_WRITE_SCOPE,
    }


def _research_authorization(stored: Any) -> dict[str, Any] | None:
    if not isinstance(stored, dict):
        return None
    snapshot = stored.get("price_snapshot")
    return {
        "authorization_digest": stored.get("authorization_digest"),
        "provider": stored.get("provider"),
        "model": stored.get("model"),
        "max_total_calls": stored.get("max_total_calls"),
        "input_digest": stored.get("input_digest"),
        "expected_sequence": stored.get("expected_sequence"),
        "issued_at": stored.get("issued_at"),
        "expires_at": stored.get("expires_at"),
        "price_snapshot_source": (snapshot.get("page_url") if isinstance(snapshot, dict) else None),
    }


def _research_result(stored: Any) -> dict[str, Any] | None:
    if not isinstance(stored, dict) or stored.get("available") is not True:
        return None
    result = stored.get("result")
    if not isinstance(result, dict):
        return None
    return {
        "available": True,
        "result_digest": stored.get("result_digest"),
        "summary": result.get("summary"),
        "confirmed": result.get("confirmed"),
        "unknowns": result.get("unknowns"),
        "conflicts": result.get("conflicts"),
        "recommendation": result.get("recommendation"),
        "claims": result.get("claims"),
    }


def run_canvas(checkpoint: Any, sequence: int, *, spec: GraphSpec) -> dict[str, Any]:
    state = _state_of(checkpoint)
    graph_id = str(state["graph_id"])
    digest = spec.digest
    nodes_state = state["nodes"]
    run_inputs = state.get("run_inputs")
    candidate = run_inputs.get("candidate") if isinstance(run_inputs, dict) else None
    authorization_preview = _authorization_preview(graph_id, digest, candidate)
    research_collection = checkpoint.eval_results.get("research_collection")
    research_preview = (
        _research_preview(research_collection)
        if graph_id == RESEARCH_GRAPH_ID and digest == RESEARCH_GRAPH_SPEC_DIGEST
        else None
    )
    research_authorization = _research_authorization(
        checkpoint.eval_results.get("research_authorization")
    )
    research_result = _research_result(checkpoint.eval_results.get("research_result"))
    stored_research_label = (
        run_inputs.get("task_label") if isinstance(run_inputs, dict) else None
    )
    if (
        not isinstance(stored_research_label, str)
        or not stored_research_label
        or len(stored_research_label) > 120
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in stored_research_label
        )
    ):
        stored_research_label = None
    if stored_research_label is not None:
        projected_task_label = stored_research_label
    elif research_preview is not None:
        projected_task_label = research_preview["task_label"]
    else:
        projected_task_label = canvas_labels.task_label(graph_id, digest)
    gate_extensions: dict[str, dict[str, Any]] = {}
    for gate_node_id, eval_key in _GATE_EXTENSION_KEYS.items():
        stored_ext = checkpoint.eval_results.get(eval_key)
        projection = (
            _authorization_projection(stored_ext, candidate)
            if eval_key == "pilot_authorization"
            else _result_projection(stored_ext)
        )
        if projection is not None:
            gate_extensions[gate_node_id] = projection
    frontier = sorted(
        node_id for node_id, node in nodes_state.items() if node["status"] in _FRONTIER_STATUSES
    )
    frontier_set = set(frontier)
    current_node = checkpoint.current_node

    taken_records: dict[str, dict[str, Any]] = {}
    for edge_record in state["edges_taken"]:
        taken_records[edge_record["edge_id"]] = edge_record
    feedback_counts = state["feedback_counts"]

    nodes: list[dict[str, Any]] = []
    for ordinal, spec_node in enumerate(spec.nodes, start=1):
        node_state = nodes_state.get(spec_node.id) or {}
        gate = _gate_projection(state["human_gates"].get(spec_node.id))
        if (
            gate is not None
            and spec_node.id == "pre_call_gate"
            and authorization_preview is not None
        ):
            gate["authorization_preview"] = authorization_preview
        if gate is not None and spec_node.id == "synthesis_authorization_gate":
            if research_preview is not None:
                gate["research_authorization_preview"] = research_preview
            if research_authorization is not None:
                gate["research_authorization"] = research_authorization
        if (
            gate is not None
            and spec_node.id == "result_human_review"
            and research_result is not None
        ):
            gate["research_result"] = research_result
        if gate is not None and spec_node.id in gate_extensions:
            extension = gate_extensions[spec_node.id]
            if "authorization_digest" in extension:
                gate["authorization"] = extension
            else:
                gate["result"] = extension
        node: dict[str, Any] = {
            "node_id": spec_node.id,
            "display_label": canvas_labels.node_label(
                graph_id, digest, spec_node.id, spec_node.kind, ordinal
            ),
            "kind": spec_node.kind,
            "status": str(node_state.get("status", "unknown")),
            "is_entry": spec_node.id == spec.entry_node,
            "is_current": spec_node.id == current_node,
            "is_frontier": spec_node.id in frontier_set,
            "join": _join_projection(spec_node),
            "human_gate": gate,
            "output_digest": node_state.get("output_digest"),
            "error_code": (
                node_state["error"].get("code")
                if isinstance(node_state.get("error"), dict)
                else None
            ),
            "completed_at": node_state.get("completed_at"),
        }
        summary = None
        # 结果摘要门控：只有节点真实完成（succeeded）且具备可信完成证据
        # （output_digest）时才允许输出已审定的 result_summary；pending /
        # ready / waiting_human / skipped / cancelled / failed 一律不得显示
        # 成功摘要（rev2：提前显示结果是阻塞缺陷）。
        if node_state.get("status") == "succeeded" and node_state.get("output_digest"):
            summary = canvas_labels.result_summary(graph_id, digest, spec_node.id)
        if summary is not None:
            node["result_summary"] = summary
        nodes.append(node)

    edges: list[dict[str, Any]] = []
    for spec_edge in spec.edges:
        taken = taken_records.get(spec_edge.id)
        edges.append(
            {
                "edge_id": spec_edge.id,
                "from": spec_edge.from_node,
                "to": spec_edge.to_node,
                "type": spec_edge.type,
                "taken": taken is not None,
                "label": canvas_labels.edge_label(graph_id, digest, spec_edge.id),
                "decision_source": (
                    taken.get("decision_source") if taken else spec_edge.decision_source
                ),
                "taken_count": (
                    int(feedback_counts.get(spec_edge.id, 0))
                    if spec_edge.type == "feedback"
                    else (1 if taken else 0)
                ),
                "max_traversals": spec_edge.max_traversals,
                "on_exhausted": spec_edge.on_exhausted,
                "exhausted_to": spec_edge.exhausted_to,
            }
        )

    return {
        "canvas_version": canvas_labels.CANVAS_VERSION,
        "run_id": checkpoint.run_id,
        "graph_id": graph_id,
        "spec_digest": digest,
        "sequence": sequence,
        "status": state["run_status"],
        "current_node": current_node,
        "task_label": (
            authorization_preview["input_fields"]["title"]
            if authorization_preview is not None
            else projected_task_label
        ),
        "nodes": nodes,
        "edges": edges,
    }
