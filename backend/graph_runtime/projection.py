"""Stable read-only observation projection for Graph runs.

The projection exposes only stable semantic fields — run/node/edge status,
reasons, input/output references and digests, checkpoint sequences, pending
human gates, blocking reasons, and available actions. Coordinates, colors,
layout, animation, and visualization libraries never enter this contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore
from graph_runtime.agent_ledger import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RESERVED,
    AgentCallRow,
)
from graph_runtime.agent_live_pilot import PILOT2B_GRAPH_ID
from graph_runtime.runtime import (
    GATE_PENDING,
    GRAPH_STATE_SCHEMA,
    NODE_TERMINAL,
)
from graph_runtime.spec import GraphSpec


def _state_of(checkpoint: LoopCheckpoint) -> dict[str, Any]:
    state = checkpoint.eval_results.get("graph_state")
    if not isinstance(state, dict) or state.get("schema") != GRAPH_STATE_SCHEMA:
        raise ValueError("not_a_graph_run")
    return state


# Read-only agent-call observation states (Graph Phase 2A), aligned with the
# frozen console input contract in GRAPH-PHASE2A-CONSOLE-RECEIPT.md §2. The
# projection exposes only call status, provider/model, reserved/actual usage,
# the session digest, the error class and the available action — never prompt
# text, response text, authorization phrases or credentials.
AGENT_NOT_CALLED = "not_called"
AGENT_PRE_CALL_PENDING = "pre_call_pending"
AGENT_COMPLETED_PENDING = "completed_pending_confirmation"
AGENT_COMPLETED = "completed"
AGENT_FAILED = "failed"
AGENT_UNKNOWN_SEND = "unknown_send"

ACTION_NONE = "none"
ACTION_STOP_AND_INSPECT = "stop_and_inspect"
ACTION_HUMAN_REVIEW = "human_review"

# G1-16：unknown-send 的唯一投影文案——发送状态未知，等待人工；
# 投影层绝不出现自动重试或 exactly-once 承诺。
UNKNOWN_SEND_LABEL = "发送状态未知，等待人工"


def agent_row_send_state_copy(row: Mapping[str, Any]) -> str | None:
    """G1-16 消费点：unknown-send 投影行经此函数得到唯一冻结文案。

    投影消费者（只读投影服务／UI）对 ``status == unknown_send`` 的行必须
    展示且只展示该文案；其他状态返回 None。投影行字段集合保持冻结，
    文案经本函数提供，不进入行 envelope。"""
    if row.get("status") == AGENT_UNKNOWN_SEND:
        return UNKNOWN_SEND_LABEL
    return None

RESULT_CLASSIFICATION = "draft_unverified_no_external_write"

# Phase 2B Pilot 图的只读投影 fail-closed 脱敏：Prompt（sanitized_text）、
# 模型响应正文（summary/unknowns/next_checks）、授权短语与凭据永不进入
# 5684 只读投影或 DOM。只脱敏投影层；SQLite/Checkpoint 原始历史不变。
# Phase 1 与 Phase 2A 图的投影保持字节/语义兼容（不在此集合内）。
SANITIZED_OUTPUT_GRAPHS = frozenset((PILOT2B_GRAPH_ID,))


def _sanitize_v2_node_output(
    spec: GraphSpec | None, node_id: str, output: Any
) -> dict[str, Any] | None:
    """Phase 2B 节点输出脱敏：只保留人工闸 decision；其余一律 None。"""
    if spec is not None:
        spec_node = spec.node_map.get(node_id)
        if spec_node is not None and spec_node.kind == "human_decision":
            if isinstance(output, dict) and isinstance(output.get("decision"), str):
                return {"decision": output["decision"]}
            return None
    return None


def _project_agent_row(row: AgentCallRow, *, human_gate_pending: bool) -> dict[str, Any]:
    if row.status == STATUS_RESERVED:
        # A reserved-but-unfinished row is an unknown send to any observer:
        # the projection runs in a separate process and can never prove a
        # send is still in flight, so every observed ``reserved`` is treated
        # as ``unknown_send`` — stop and inspect by hand, never a retry.
        status = AGENT_UNKNOWN_SEND
        action = ACTION_STOP_AND_INSPECT
    elif row.status == STATUS_FAILED:
        status = AGENT_UNKNOWN_SEND if row.request_sent == "unknown" else AGENT_FAILED
        action = (
            ACTION_STOP_AND_INSPECT if row.request_sent == "unknown" else ACTION_HUMAN_REVIEW
        )
    else:  # completed
        status = AGENT_COMPLETED_PENDING if human_gate_pending else AGENT_COMPLETED
        action = ACTION_NONE
    reserved_tokens = row.max_input_tokens + row.max_output_tokens
    actual_tokens = None
    if row.actual_input_tokens is not None and row.actual_output_tokens is not None:
        actual_tokens = row.actual_input_tokens + row.actual_output_tokens
    return {
        "status": status,
        "provider": row.provider,
        "model": row.model,
        "max_calls": row.max_calls,
        "reserved_tokens": reserved_tokens,
        "actual_tokens": actual_tokens,
        "error_category": row.error_code,
        # Additional read-only fields the console whitelist ignores: session
        # digest, send state and the single available action.
        "session_digest": row.session_id,
        "request_sent": row.request_sent,
        "action": action,
        "result_classification": (
            RESULT_CLASSIFICATION if row.status == STATUS_COMPLETED else None
        ),
    }


def summarize_run(checkpoint: LoopCheckpoint, sequence: int) -> dict[str, Any]:
    state = _state_of(checkpoint)
    pending_human = sorted(
        node_id for node_id, gate in state["human_gates"].items() if gate["status"] == GATE_PENDING
    )
    return {
        "run_id": checkpoint.run_id,
        "graph_id": state["graph_id"],
        "spec_version": state["spec_version"],
        "spec_digest": state["spec_digest"],
        "fragment_ref": checkpoint.fragment_id,
        "status": state["run_status"],
        "current_node": checkpoint.current_node,
        "step_count": state["step_count"],
        "sequence": sequence,
        "pending_human": pending_human,
        "blocked_reason": state["blocked_reason"],
        "started_at": state["started_at"],
        "updated_at": checkpoint.updated_at,
    }


def list_runs(store: SQLiteCheckpointStore) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for run_id in store.run_ids_with_prefix("graph:"):
        found = store.latest_with_sequence(run_id)
        if found is None:
            continue
        checkpoint, sequence = found
        try:
            summaries.append(summarize_run(checkpoint, sequence))
        except ValueError:
            continue
    return summaries


def run_detail(
    checkpoint: LoopCheckpoint,
    sequence: int,
    *,
    spec: GraphSpec | None = None,
    agent_calls: Mapping[str, AgentCallRow] | None = None,
) -> dict[str, Any]:
    state = _state_of(checkpoint)
    sanitize_outputs = str(state["graph_id"]) in SANITIZED_OUTPUT_GRAPHS
    human_gate_pending = any(
        gate["status"] == GATE_PENDING for gate in state["human_gates"].values()
    )
    rows = dict(agent_calls or {})
    nodes: list[dict[str, Any]] = []
    for node_id in sorted(state["nodes"]):
        node = state["nodes"][node_id]
        memory_refs = sorted(
            ref
            for ref in [*(node.get("input_refs") or []), *(node.get("output_refs") or [])]
            if isinstance(ref, str) and ref.startswith("mem:")
        )
        agent_call: dict[str, Any] | None = None
        row = rows.get(node_id)
        if row is not None:
            agent_call = _project_agent_row(row, human_gate_pending=human_gate_pending)
        elif spec is not None and agent_calls is not None:
            spec_node = spec.node_map.get(node_id)
            if spec_node is not None and (spec_node.adapter or "").startswith("agent."):
                # 等待调用前确认：存在一个待决人工闸门，其下游闭包覆盖该
                # Agent 节点（闸门与 Agent 节点之间允许有确定性准备节点）。
                awaiting_precall = any(
                    gate.get("status") == GATE_PENDING
                    and node_id in spec.downstream_closure(gate_id)
                    for gate_id, gate in state["human_gates"].items()
                )
                agent_call = {
                    "status": (
                        AGENT_PRE_CALL_PENDING if awaiting_precall else AGENT_NOT_CALLED
                    ),
                    # Pre-call there is no ledger row yet: provider/model are
                    # governance data held by the injected policy, not by the
                    # core — the projection reports them as empty rather than
                    # inventing values.
                    "provider": "",
                    "model": "",
                    "max_calls": 1,
                    "reserved_tokens": None,
                    "actual_tokens": None,
                    "error_category": None,
                    "session_digest": None,
                    "request_sent": None,
                    "action": ACTION_NONE,
                    "result_classification": None,
                }
        nodes.append(
            {
                "node_id": node_id,
                "status": node["status"],
                "attempts": node["attempts"],
                "terminal": node["status"] in NODE_TERMINAL,
                "input_digest": node["input_digest"],
                "output_digest": node["output_digest"],
                "input_refs": list(node.get("input_refs") or []),
                "output_refs": list(node.get("output_refs") or []),
                "output": (
                    _sanitize_v2_node_output(spec, node_id, node.get("output"))
                    if sanitize_outputs
                    else node.get("output")
                ),
                "error": node.get("error"),
                "completed_at": node.get("completed_at"),
                "child_run_id": node.get("child_run_id"),
                "absorbed_failures": list(node.get("absorbed_failures") or []),
                "memory_refs": memory_refs,
                # Phase 1 runs carry no agent metadata: this stays None and
                # the UI renders exactly as before.
                "agent": agent_call,
            }
        )
    human_gates = {node_id: dict(gate) for node_id, gate in state["human_gates"].items()}
    return {
        "run": summarize_run(checkpoint, sequence),
        "nodes": nodes,
        "edges_taken": list(state["edges_taken"]),
        "feedback_counts": dict(state["feedback_counts"]),
        "human_gates": human_gates,
        "ready": sorted(
            node_id for node_id, node in state["nodes"].items() if node["status"] == "ready"
        ),
    }


def run_history(store: SQLiteCheckpointStore, run_id: str) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    for sequence, checkpoint in store.history_with_sequence(run_id):
        state = checkpoint.eval_results.get("graph_state", {})
        history.append(
            {
                "sequence": sequence,
                "event_type": checkpoint.event_type,
                "current_node": checkpoint.current_node,
                "status": checkpoint.status,
                "run_status": state.get("run_status") if isinstance(state, dict) else None,
                "step_count": state.get("step_count") if isinstance(state, dict) else None,
                "revision_reason": checkpoint.revision_reason,
                "committed_at": checkpoint.updated_at,
            }
        )
    return history


def run_path(spec: GraphSpec, checkpoint: LoopCheckpoint, sequence: int) -> dict[str, Any]:
    """Key path plus per-edge selection reasons (explain)."""
    state = _state_of(checkpoint)
    edges_taken = list(state["edges_taken"])
    frontier = sorted(
        node_id
        for node_id, node in state["nodes"].items()
        if node["status"] in ("ready", "waiting_human")
    )
    failed = sorted(
        node_id for node_id, node in state["nodes"].items() if node["status"] == "failed"
    )
    return {
        "run_id": checkpoint.run_id,
        "sequence": sequence,
        "entry_node": spec.entry_node,
        "status": state["run_status"],
        "path": edges_taken,
        "frontier": frontier,
        "failed_nodes": failed,
        "blocked_reason": state["blocked_reason"],
        "feedback_counts": dict(state["feedback_counts"]),
    }


def run_affected(spec: GraphSpec, checkpoint: LoopCheckpoint, node_id: str) -> dict[str, Any]:
    """Static downstream closure plus the affected human gates."""
    if node_id not in spec.node_map:
        raise KeyError(f"Unknown node: {node_id}")
    state = _state_of(checkpoint)
    downstream = spec.downstream_closure(node_id)
    affected_gates = sorted(
        gate_id for gate_id in [node_id, *downstream] if gate_id in state["human_gates"]
    )
    return {
        "run_id": checkpoint.run_id,
        "node_id": node_id,
        "affected_nodes": downstream,
        "affected_human_gates": affected_gates,
    }
