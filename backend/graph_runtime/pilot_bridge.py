"""fragment-pilot-v1 的创建桥（GRAPH-PILOT-ENTRY-BRIDGE-V1-DESIGN.md rev7 §3.1–§3.3）。

职责边界：
- 六字段请求体精确校验（未知字段拒绝）；
- 候选资格三字段 + fragment_ref 受信绑定 + content_sha256 逐字 TOCTOU 核验；
- 输入字段上限预检（超限零创建，绝不静默截断）；
- 安全前缀结构允许清单校验（任何漂移注册前拒绝）；
- 确定性 run_id + register + 推进至 pre_call_gate 的编排。

不做：模型调用（创建/推进路径模型调用恒为 0，Agent 账本零行证明）、
价格核验（只在 Authorization Receipt 签发时发生，见 pilot_execution）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from common.checkpoint import SQLiteCheckpointStore
from graph_runtime import pilot_adapters
from graph_runtime.agent_ledger import AgentLedgerStore
from graph_runtime.runtime import GraphRuntime, GraphRuntimeError
from graph_runtime.specs.fragment_pilot_v1 import (
    ADAPTER_INPUT,
    PILOT_GRAPH_ID,
    PILOT_SPEC,
    PREFIX_ADAPTERS,
    PREFIX_EDGES,
    PREFIX_NODES,
    SPEC_DIGEST,
)

# 写资源固定头（cognitive_server 在桥之前已校验 Origin + 写头）。
RUN_CREATE_HEADER = "X-Graph-Run-Create"

CREATE_BODY_KEYS = frozenset(
    (
        "spec_id",
        "spec_digest",
        "fragment_ref",
        "candidate_id",
        "candidate_content_sha256",
        "requester",
    )
)

RUN_ID_NAMESPACE = "exec:graph-pilot:"
RUN_ID_DOMAIN = "graph-pilot-entry-bridge-v1"

# 候选资格三字段（product_review.py 真实取值，rev4 冻结）。
_REQUIRED_CONTENT_STATUS = "pending_confirmation"
_REQUIRED_EVIDENCE_LEVEL = "unverified"


class PilotBridgeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def deterministic_run_id(spec_digest: str, fragment_ref: str) -> str:
    """spec_digest + canonical fragment_ref 的确定性 Run ID（rev2 冻结）。"""
    material = f"{RUN_ID_DOMAIN}\x1f{spec_digest}\x1f{fragment_ref}"
    return RUN_ID_NAMESPACE + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def validate_create_body(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != CREATE_BODY_KEYS:
        raise PilotBridgeError("invalid_body")
    if raw.get("spec_id") != PILOT_GRAPH_ID:
        # 合成 pilot-v2 或任意其他 graph_id 一律在此拒绝（反例 3）。
        raise PilotBridgeError("spec_not_allowed")
    if not _is_sha256(raw.get("spec_digest")):
        raise PilotBridgeError("invalid_body")
    if raw["spec_digest"] != SPEC_DIGEST:
        raise PilotBridgeError("plan_expired")
    fragment_ref = raw.get("fragment_ref")
    if (
        not isinstance(fragment_ref, str)
        or not fragment_ref.strip()
        or len(fragment_ref) > 256
        or any(ord(ch) < 0x20 or ch == "\x7f" for ch in fragment_ref)
    ):
        raise PilotBridgeError("invalid_body")
    candidate_id = raw.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id.strip()
        or len(candidate_id) > 128
        or any(ord(ch) < 0x20 or ch == "\x7f" for ch in candidate_id)
    ):
        raise PilotBridgeError("invalid_body")
    if not _is_sha256(raw.get("candidate_content_sha256")):
        raise PilotBridgeError("invalid_body")
    if raw.get("requester") != "nigo":
        raise PilotBridgeError("invalid_body")
    return dict(raw)


def pilot_plan() -> dict[str, Any]:
    """`GET /graph/v1/pilot-plans/fragment-pilot-v1` 的确定性预案投影。

    确认页全部字段来自此处与候选投影；零外网、零模型、零写入。
    """
    return {
        "plan_version": "1",
        "spec_id": PILOT_GRAPH_ID,
        "spec_digest": SPEC_DIGEST,
        "task_label": "真实碎片 Pilot",
        "node_count": len(PILOT_SPEC.nodes),
        "node_flow": "输入→调用前闸门→准备→草稿生成→验证→调用后闸门→产出/拒绝/中止",
        "expected_output": "Graph 内未验证草稿（不写入知识资产）",
        "human_gates": 2,
        "max_feedback": 1,
        "max_total_calls": pilot_adapters.MAX_TOTAL_CALLS,
        "cost_cap_cny": pilot_adapters.COST_CAP_CNY,
        "provider": pilot_adapters.PILOT_PROVIDER,
        "model": pilot_adapters.PILOT_MODEL,
        "write_scope": "Graph 检查点 + Agent 账本；不写笔记、不写资产",
        "create_behavior": "创建后推进到第一道人工闸门并停下；不会调用模型。",
    }


def check_prefix_allowlist() -> None:
    """推进至首闸的精确结构允许清单（§3.3）；任何漂移 → unsafe_prefix。"""
    entry = PILOT_SPEC.node_map.get(PILOT_SPEC.entry_node)
    if entry is None or entry.id != PREFIX_NODES[0] or entry.kind != "input":
        raise PilotBridgeError("unsafe_prefix")
    if entry.adapter != PREFIX_ADAPTERS[0] or PREFIX_ADAPTERS[0] != ADAPTER_INPUT:
        raise PilotBridgeError("unsafe_prefix")
    out_edges = [edge for edge in PILOT_SPEC.edges if edge.from_node == entry.id]
    if len(out_edges) != 1 or out_edges[0].id != PREFIX_EDGES[0]:
        raise PilotBridgeError("unsafe_prefix")
    if out_edges[0].type != "sequence" or out_edges[0].to_node != "pre_call_gate":
        raise PilotBridgeError("unsafe_prefix")
    gate = PILOT_SPEC.node_map.get("pre_call_gate")
    if gate is None or gate.kind != "human_decision":
        raise PilotBridgeError("unsafe_prefix")


class _ExplodingTransport:
    """创建/推进路径的零网络证明：任何 send_once 都是实现缺陷。"""

    def send_once(self, _request: Any, _credential: str = "") -> Any:
        raise AssertionError("pilot create/advance path must never send")


class PilotBridge:
    """创建编排：资格 → 绑定 → 前缀 → register → 推进至首闸。"""

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        review_service: Any,
        ledger: AgentLedgerStore,
    ):
        self.store = store
        self.review_service = review_service
        self.ledger = ledger
        # 桥自己的 Runtime：带 Pilot Adapter，但 transport 是爆炸桩——
        # 推进只允许经过 pilot_input（确定性 adapter），永远触不到 Agent 节点。
        self._runtime = GraphRuntime(
            store,
            {PILOT_GRAPH_ID: PILOT_SPEC},
            pilot_adapters.pilot_adapters(
                ledger, lambda _run_id: None, _ExplodingTransport()
            ),
        )

    # -- 候选读取与资格 ----------------------------------------------------

    def _candidate_projection(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            reviews = self.review_service.list_reviews()
        except Exception as error:  # 候选目录不可读等 → 诚实降级
            raise PilotBridgeError("candidate_source_unavailable") from error
        matched = [
            review
            for review in reviews
            if isinstance(review, dict)
            and review.get("candidate_id") == body["candidate_id"]
        ]
        if len(matched) != 1:
            raise PilotBridgeError("fragment_not_trusted")
        projection = matched[0]
        if projection.get("fragment_ref") != body["fragment_ref"]:
            raise PilotBridgeError("fragment_not_trusted")
        return projection

    @staticmethod
    def _check_eligibility(projection: dict[str, Any]) -> None:
        if (
            projection.get("content_status") != _REQUIRED_CONTENT_STATUS
            or projection.get("evidence_level") != _REQUIRED_EVIDENCE_LEVEL
            or projection.get("has_conflict") is not False
        ):
            raise PilotBridgeError("candidate_not_ready")

    def _candidate_input(self, body: dict[str, Any]) -> dict[str, Any]:
        """读取候选安全字段并做上限预检；超限零创建（反例 39）。"""
        try:
            detail = self.review_service.detail(body["candidate_id"])
        except Exception as error:
            raise PilotBridgeError("candidate_source_unavailable") from error
        cards = detail.get("candidate_cards")
        card_titles = [
            str(card.get("title", ""))
            for card in cards
            if isinstance(card, dict)
        ] if isinstance(cards, list) else []
        candidate = {
            "title": str(detail.get("title", "")),
            "core_judgment": str(detail.get("core_judgment", "")),
            "user_value": str(detail.get("user_value", "")),
            "card_titles": card_titles,
        }
        if (
            not candidate["title"]
            or not candidate["core_judgment"]
            or not candidate["user_value"]
            or len(candidate["title"]) > pilot_adapters.MAX_TITLE
            or len(candidate["core_judgment"]) > pilot_adapters.MAX_CORE_JUDGMENT
            or len(candidate["user_value"]) > pilot_adapters.MAX_USER_VALUE
            or len(card_titles) > pilot_adapters.MAX_CARD_TITLES
            or any(len(title) > pilot_adapters.MAX_CARD_TITLE for title in card_titles)
        ):
            raise PilotBridgeError("candidate_input_over_limit")
        return candidate

    # -- 创建编排 -----------------------------------------------------------

    def create_run(self, raw_body: object) -> tuple[int, dict[str, Any]]:
        """返回 (HTTP 状态, 响应体)。语义：201 首建 / 200 幂等重放 /
        4xx 注册前拒绝（零 Run）/ 201+blocked 推进异常（Run 可见）。"""
        body = validate_create_body(raw_body)
        projection = self._candidate_projection(body)
        self._check_eligibility(projection)
        # TOCTOU：确认页与创建之间候选内容变化 → plan_expired（反例 8）。
        if projection.get("content_sha256") != body["candidate_content_sha256"]:
            raise PilotBridgeError("plan_expired")
        candidate = self._candidate_input(body)
        check_prefix_allowlist()

        run_id = deterministic_run_id(SPEC_DIGEST, body["fragment_ref"])
        run_inputs = {
            "candidate": candidate,
            "candidate_id": body["candidate_id"],
            "candidate_content_sha256": body["candidate_content_sha256"],
        }
        try:
            _checkpoint, created = self._runtime.register_run(
                PILOT_SPEC,
                run_id,
                fragment_ref=body["fragment_ref"],
                run_inputs=run_inputs,
            )
        except GraphRuntimeError as error:
            if error.code == "run_id_conflict":
                # 同 fragment_ref 已有不同绑定的 Run（一碎片一 Run）。
                raise PilotBridgeError("already_bridged") from error
            raise

        if not created:
            return 200, self._status_payload(run_id, idempotent=True)

        outcome = self._runtime.run_until_settled(run_id)
        model_calls = len(self.ledger.list_for_run(run_id))
        sequence = int(self.store.latest_sequence(run_id) or 0)
        if outcome.event != "human_gate_requested" or outcome.node_id != "pre_call_gate":
            # 注册后推进异常：Run 已可见，诚实返回 blocked，不诱导重建（反例 10）。
            return 201, {
                "run_id": run_id,
                "status": "blocked",
                "blocked_code": str(outcome.event),
                "sequence": sequence,
                "model_calls": model_calls,
            }
        return 201, {
            "run_id": run_id,
            "status": "human_wait",
            "current_node": "pre_call_gate",
            "sequence": sequence,
            "model_calls": model_calls,
        }

    def _status_payload(self, run_id: str, *, idempotent: bool) -> dict[str, Any]:
        found = self.store.latest_with_sequence(run_id)
        if found is None:  # pragma: no cover - register 刚成功，防御性兜底
            raise PilotBridgeError("run_lost")
        checkpoint, sequence = found
        state = checkpoint.eval_results.get("graph_state", {})
        return {
            "run_id": run_id,
            "status": str(state.get("run_status", "unknown")),
            "current_node": checkpoint.current_node,
            "sequence": int(sequence),
            "model_calls": len(self.ledger.list_for_run(run_id)),
            "idempotent": idempotent,
        }
