"""fragment-pilot-v1 的 Authorization Receipt 与双闸门执行钩子（rev7 §3.4–§3.9）。

- Receipt 首次签发 / 过期重签：只有这个动作允许通过注入的 price_reader
  只读核验官方价格；创建、预案、推进路径零外网。核验失败一律关闭（零调用）。
- 双闸门钩子：pre_call_gate 授权并执行（绑定 authorization_digest）→ 有界
  推进至 post_call_gate；post_call_gate 接受/拒绝（绑定 result_digest）→
  只执行确定性节点到出口，第二阶段模型调用恒为 0。
- 钩子仅对 fragment-pilot-v1 精确生效；其他 Graph 的决定原样委托既有
  GraphService.decide，行为零变化。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from common.checkpoint import SQLiteCheckpointStore
from common.execution import merge_runtime_eval_results, raw_eval_value
from graph_runtime import pilot_adapters
from graph_runtime.agent_ledger import (
    RESUME_INTENT_SCHEMA,
    AgentExecutionLink,
    AgentLedgerStore,
)
from graph_runtime.pilot_live import (
    PilotLiveError,
    forbidden_credential_reader,
    verify_pilot_price_snapshot,
)
from graph_runtime.runtime import GraphRuntime
from graph_runtime.service import GraphService
from graph_runtime.spec import canonical_json, digest_of
from graph_runtime.specs.fragment_pilot_v1 import (
    PILOT_GRAPH_ID,
    PILOT_SPEC,
    POST_GATE,
    PRE_GATE,
    SPEC_DIGEST,
    VALIDATOR_NODE,
)

RECEIPT_HEADER = "X-Graph-Authorization-Receipt"
RECEIPT_TTL = timedelta(hours=24)

_AUDIT_ISSUED = "authorization_issued"
_AUDIT_RENEWED = "authorization_renewed"
_AUDIT_RESULT = "pilot_result_projected"


class PilotExecutionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise PilotExecutionError("price_unavailable")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PilotExecutionError("price_unavailable") from error
    if parsed.tzinfo is None:
        raise PilotExecutionError("price_unavailable")
    return parsed.astimezone(UTC)


# -- 价格门（唯一外网动作的注入点；rev2 §4 结构化严格验证） ----------------


def _verify_price(snapshot: object, *, now: datetime) -> dict[str, Any]:
    """签发前最终价格门：URL、快照时效、模型、三类价格与总成本严格核验。"""
    try:
        return verify_pilot_price_snapshot(snapshot, now=now)
    except PilotLiveError as error:
        raise PilotExecutionError(error.code) from error


# -- Receipt 材料与摘要 -----------------------------------------------------


def _candidate_canonical(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": candidate["title"],
        "core_judgment": candidate["core_judgment"],
        "user_value": candidate["user_value"],
        "card_titles": list(candidate["card_titles"]),
    }


def agent_input_digest(candidate: Mapping[str, Any]) -> str:
    """Adapter 级绑定：Agent 节点扁平化输入（= 准备节点确定性输出）的摘要。"""
    canonical = _candidate_canonical(candidate)
    preparation_output = {
        "decision": "approve_call",
        "candidate_json": canonical_json(canonical),
        "input_digest": digest_of(canonical),
    }
    return digest_of(preparation_output)


def receipt_material(
    *,
    run_id: str,
    candidate: Mapping[str, Any],
    candidate_id: str,
    candidate_content_sha256: str,
    expected_sequence: int,
    price_snapshot: Mapping[str, Any],
    issued_at: datetime,
) -> dict[str, Any]:
    canonical = _candidate_canonical(candidate)
    return {
        "run_id": run_id,
        "spec_digest": SPEC_DIGEST,
        "candidate_id": candidate_id,
        "candidate_content_sha256": candidate_content_sha256,
        "input_digest": digest_of(canonical),
        "agent_input_digest": agent_input_digest(candidate),
        "expected_sequence": expected_sequence,
        "max_total_calls": pilot_adapters.MAX_TOTAL_CALLS,
        "cost_cap_cny": pilot_adapters.COST_CAP_CNY,
        "provider": pilot_adapters.PILOT_PROVIDER,
        "model": pilot_adapters.PILOT_MODEL,
        "authorized_by": "nigo",
        "issued_at": _iso(issued_at),
        "expires_at": _iso(issued_at + RECEIPT_TTL),
        "price_snapshot": dict(price_snapshot),
    }


def authorization_digest_of_material(material: Mapping[str, Any]) -> str:
    return digest_of(dict(material))


def _material_of(stored: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in stored.items() if key != "authorization_digest"}


# rev3 §3：安全重试写资源（仅 fragment-pilot-v1 的 Agent 节点）。
RETRY_HEADER = "X-Graph-Pilot-Retry"
RETRY_NODE = "agent_drafter"
RETRY_BODY_KEYS = frozenset(
    (
        "run_id",
        "node_id",
        "spec_digest",
        "input_digest",
        "authorization_digest",
        "expected_sequence",
        "requester",
        "retry_id",
    )
)


def pilot_retry_id(
    *,
    requester: str,
    run_id: str,
    node_id: str,
    spec_digest: str,
    input_digest: str,
    authorization_digest: str,
    expected_sequence: int,
) -> str:
    """重试幂等身份：全部绑定材料的 canonical 摘要；任何漂移都是新身份。"""
    return digest_of(
        [
            "graph-pilot-retry-v1",
            requester,
            run_id,
            node_id,
            spec_digest,
            input_digest,
            authorization_digest,
            str(expected_sequence),
        ]
    )


class PilotExecution:
    """双闸门执行钩子 + Authorization Receipt 签发/重签。"""

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        service: GraphService,
        review_service: Any,
        ledger: AgentLedgerStore,
        transport: Any,
        *,
        price_reader: Callable[[], dict[str, Any]] | None = None,
        live_enabled: bool = False,
        credential_reader: Callable[[str], str] | None = None,
        monotonic: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.service = service
        self.review_service = review_service
        self.ledger = ledger
        self.price_reader = price_reader
        self._clock = clock or (lambda: datetime.now(UTC))
        # 决定处理 300 秒总预算（rev2 §6 / rev4 §2 按 Run 隔离）：可注入单调
        # 时钟；每个 decide/安全重试独立 start/clear，并发 Run 互不影响。
        self._budgets = pilot_adapters.PilotBudgetBook(monotonic)
        self._runtime = GraphRuntime(
            store,
            {PILOT_GRAPH_ID: PILOT_SPEC},
            pilot_adapters.pilot_adapters(
                ledger,
                self._stored_receipt,
                transport,
                live_enabled=live_enabled,
                credential_reader=(
                    credential_reader
                    if credential_reader is not None
                    else forbidden_credential_reader
                ),
                budget=self._budgets,
                clock=clock,
            ),
        )

    # -- 内部读取 ------------------------------------------------------------

    def _stored_receipt(self, run_id: str) -> dict[str, Any] | None:
        found = self.store.latest(run_id)
        if found is None:
            return None
        stored = found.eval_results.get("pilot_authorization")
        return dict(stored) if isinstance(stored, dict) else None

    def _load_pilot_state(self, run_id: str) -> tuple[Any, dict[str, Any], int]:
        found = self.store.latest_with_sequence(run_id)
        if found is None:
            raise KeyError(run_id)
        checkpoint, sequence = found
        state = checkpoint.eval_results.get("graph_state")
        if not isinstance(state, dict):
            raise PilotExecutionError("not_a_pilot_run")
        if (
            state.get("graph_id") != PILOT_GRAPH_ID
            or state.get("spec_digest") != SPEC_DIGEST
        ):
            raise PilotExecutionError("not_a_pilot_run")
        return checkpoint, state, int(sequence)

    def _load_pilot_state_raw(self, run_id: str) -> tuple[Any, dict[str, Any], int]:
        """Raw stored form for write paths: same pilot binding checks, but
        reads and merges happen against the stored row — never the compat
        view, so a write can never flatten a namespaced row."""
        found = self.store.latest_raw_with_sequence(run_id)
        if found is None:
            raise KeyError(run_id)
        checkpoint, sequence = found
        state = raw_eval_value(checkpoint.eval_results, "graph_state")
        if not isinstance(state, dict):
            raise PilotExecutionError("not_a_pilot_run")
        if (
            state.get("graph_id") != PILOT_GRAPH_ID
            or state.get("spec_digest") != SPEC_DIGEST
        ):
            raise PilotExecutionError("not_a_pilot_run")
        return checkpoint, state, int(sequence)

    def _candidate_projection(self, candidate_id: str) -> dict[str, Any]:
        try:
            reviews = self.review_service.list_reviews()
        except Exception as error:
            raise PilotExecutionError("candidate_source_unavailable") from error
        matched = [
            review
            for review in reviews
            if isinstance(review, dict) and review.get("candidate_id") == candidate_id
        ]
        if len(matched) != 1:
            raise PilotExecutionError("candidate_version_drift")
        return matched[0]

    def _check_candidate_version(self, run_id: str) -> dict[str, Any]:
        latest = self.store.latest(run_id)
        inputs = (
            latest.eval_results.get("registration", {}).get("run_inputs", {})
            if latest is not None
            else {}
        )
        candidate_id = str(inputs.get("candidate_id", ""))
        expected_sha = str(inputs.get("candidate_content_sha256", ""))
        projection = self._candidate_projection(candidate_id)
        if projection.get("content_sha256") != expected_sha:
            raise PilotExecutionError("candidate_version_drift")
        return projection

    # -- Receipt 签发 / 过期重签 --------------------------------------------

    def issue_receipt(self, run_id: str) -> tuple[int, dict[str, Any]]:
        checkpoint, state, _sequence = self._load_pilot_state(run_id)
        gate = state["human_gates"].get(PRE_GATE)
        if (
            state.get("run_status") != "human_wait"
            or checkpoint.current_node != PRE_GATE
            or not isinstance(gate, dict)
            or gate.get("status") != "pending"
        ):
            raise PilotExecutionError("gate_not_pending")
        if self.ledger.list_for_run(run_id):
            # 已有 reservation 禁止签发/重签（反例 34）。
            raise PilotExecutionError("reservation_exists")
        registration = checkpoint.eval_results.get("registration", {})
        inputs = registration.get("run_inputs", {}) if isinstance(registration, dict) else {}
        candidate = inputs.get("candidate")
        candidate_id = str(inputs.get("candidate_id", ""))
        content_sha = str(inputs.get("candidate_content_sha256", ""))
        if not isinstance(candidate, dict) or not candidate_id or not content_sha:
            raise PilotExecutionError("registration_invalid")
        projection = self._candidate_projection(candidate_id)
        if projection.get("content_sha256") != content_sha:
            # 候选漂移禁止签发/重签（反例 35）。
            raise PilotExecutionError("candidate_version_drift")

        existing = checkpoint.eval_results.get("pilot_authorization")
        renewal = isinstance(existing, dict)
        if renewal:
            expires_at = _parse_iso(existing.get("expires_at"))
            if self._clock() < expires_at:
                raise PilotExecutionError("authorization_still_valid")

        if self.price_reader is None:
            raise PilotExecutionError("price_unavailable")
        try:
            snapshot = _verify_price(self.price_reader(), now=self._clock())
        except PilotExecutionError:
            raise
        except Exception as error:
            raise PilotExecutionError("price_unavailable") from error

        issued: dict[str, Any] = {}

        def receipt_factory(assigned: int) -> Mapping[str, Any]:
            # 材料绑定的是审计追加实际分配的 sequence（全局 AUTOINCREMENT，
            # 跨 Run 交错时与「本地序号 + 1」不同），与 _append_audit 的闸门
            # 重同步在同一个事务内保持一致。
            material = receipt_material(
                run_id=run_id,
                candidate=candidate,
                candidate_id=candidate_id,
                candidate_content_sha256=content_sha,
                expected_sequence=assigned,
                price_snapshot=snapshot,
                issued_at=self._clock(),
            )
            digest = authorization_digest_of_material(material)
            issued["material"] = material
            issued["digest"] = digest
            return {"pilot_authorization": {**material, "authorization_digest": digest}}

        self._append_audit(
            run_id,
            receipt_factory,
            event_type=_AUDIT_RENEWED if renewal else _AUDIT_ISSUED,
        )
        material = issued["material"]
        return 201, {
            "run_id": run_id,
            "authorization_digest": issued["digest"],
            "issued_at": material["issued_at"],
            "expires_at": material["expires_at"],
            "status": "renewed" if renewal else "issued",
            "model_calls": 0,
        }

    def _append_audit(
        self,
        run_id: str,
        extra_factory: Callable[[int], Mapping[str, Any]],
        *,
        event_type: str,
    ) -> int:
        """审计追加：经 SQLiteCheckpointStore.append_with_assigned_sequence
        在同一个 BEGIN IMMEDIATE 事务内取得将被分配的精确序号（rev5：
        基于 sqlite_sequence，兼容空表、恢复库与删行空洞），用它生成授权
        材料并同步 pending gate，插入后断言分配值与预测一致。
        返回实际分配的 sequence。"""
        for _attempt in range(3):
            # 写路径一律从 raw 存储形态开始，绝不基于 compat view 回写。
            found = self.store.latest_raw_with_sequence(run_id)
            if found is None:
                raise KeyError(run_id)
            checkpoint, sequence = found

            def build(assigned: int) -> Any:
                content: dict[str, Any] = dict(extra_factory(assigned))
                state = raw_eval_value(checkpoint.eval_results, "graph_state")
                if isinstance(state, dict):
                    gates = state.get("human_gates")
                    if isinstance(gates, dict):
                        new_gates: dict[str, Any] = {}
                        bumped = False
                        for gate_id, gate in gates.items():
                            if isinstance(gate, dict) and gate.get("status") == "pending":
                                gate = {**gate, "expected_sequence": assigned}
                                bumped = True
                            new_gates[gate_id] = gate
                        if bumped:
                            content["graph_state"] = {
                                **state,
                                "human_gates": new_gates,
                            }
                # form-preserving：旧 flat 保持 flat；namespaced 行写
                # system，顶层持续仅 system/plugins。
                eval_results = merge_runtime_eval_results(
                    existing=checkpoint.eval_results, content=content
                )
                return replace(checkpoint, eval_results=eval_results)

            committed = self.store.append_with_assigned_sequence(
                run_id,
                expected_sequence=sequence,
                build=build,
                event_type=event_type,
            )
            if committed is not None:
                return committed[1]
        raise PilotExecutionError("cas_conflict")

    # -- 安全重试（rev3 §3） --------------------------------------------------

    def retry_failed_agent(self, run_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """发送前失败（request_sent=false）的安全重试，仅限 pilot Agent 节点。

        复用既有 reopen 的受影响区间语义，但收敛到 pilot 精确范围：run 阻断
        在 agent_drafter、最新账本行 failed 且 request_sent=false、Receipt
        重新核验通过才复位并推进；true/unknown、reserved、completed 或任何
        绑定漂移一律拒绝，绝不重发。
        """
        checkpoint, state, sequence = self._load_pilot_state_raw(run_id)
        if str(body.get("node_id", "")) != RETRY_NODE:
            raise PilotExecutionError("retry_not_allowed")
        # 运行状态条件：阻断在该 Agent 节点。
        if str(state.get("run_status")) not in ("failed", "blocked"):
            raise PilotExecutionError("retry_not_allowed")
        if checkpoint.current_node != RETRY_NODE:
            raise PilotExecutionError("retry_not_allowed")
        node_state = state["nodes"].get(RETRY_NODE, {})
        if node_state.get("status") != "failed":
            raise PilotExecutionError("retry_not_allowed")
        # 账本条件：最新行必须 failed 且 request_sent=false。
        rows = [
            row
            for row in self.ledger.list_for_run(run_id)
            if str(row.node_id) == RETRY_NODE
        ]
        if not rows:
            raise PilotExecutionError("retry_not_allowed")
        latest_row = rows[-1]
        if str(latest_row.status) != "failed":
            # reserved / completed：绝不重发。
            raise PilotExecutionError("retry_never_resend")
        if str(latest_row.request_sent) != "false":
            # request_sent=true/unknown：永久禁止重发。
            raise PilotExecutionError("retry_never_resend")

        # 绑定核验：spec、input、授权、sequence、requester、幂等身份。
        stored = raw_eval_value(checkpoint.eval_results, "pilot_authorization")
        if not isinstance(stored, dict):
            raise PilotExecutionError("authorization_missing")
        expected_auth = authorization_digest_of_material(_material_of(stored))
        if body.get("authorization_digest") != expected_auth:
            raise PilotExecutionError("authorization_digest_mismatch")
        if self._clock() >= _parse_iso(stored.get("expires_at")):
            raise PilotExecutionError("authorization_expired")
        if body.get("spec_digest") != SPEC_DIGEST:
            raise PilotExecutionError("spec_digest_mismatch")
        if body.get("input_digest") != stored.get("agent_input_digest"):
            raise PilotExecutionError("input_digest_mismatch")
        if not isinstance(body.get("expected_sequence"), int) or int(
            body["expected_sequence"]
        ) != sequence:
            raise PilotExecutionError("sequence_mismatch")
        if body.get("requester") != "nigo":
            raise PilotExecutionError("invalid_requester")
        expected_id = pilot_retry_id(
            requester="nigo",
            run_id=run_id,
            node_id=RETRY_NODE,
            spec_digest=SPEC_DIGEST,
            input_digest=str(stored["agent_input_digest"]),
            authorization_digest=expected_auth,
            expected_sequence=sequence,
        )
        if body.get("retry_id") != expected_id:
            raise PilotExecutionError("retry_id_mismatch")
        self._check_candidate_version(run_id)

        # 受影响区间复位（仅 agent_drafter 及其下游；pre_call_gate 保持已决）。
        # R8 闭集 resume intent：以已验证 latest ledger session 与授权生成；
        # 原 execution 按精确 binding/session 查询（runtime 的 input_digest
        # 是上游 output_digest 映射的 digest，_inputs_for 同算法）；由
        # GraphRuntime 以当前 checkpoint/spec/input/inventory 补全期望并走
        # 全绑定 verified pre-send resume。
        upstream_digests = {
            edge.from_node: state["nodes"][edge.from_node]["output_digest"]
            for edge in PILOT_SPEC.in_edges(RETRY_NODE)
            if edge.type != "feedback"
            and state["nodes"][edge.from_node]["status"] == "succeeded"
        }
        latest_execution = AgentExecutionLink(self.store.path).latest_execution_for(
            runtime_kind="graph",
            run_id=run_id,
            node_id=RETRY_NODE,
            spec_digest=SPEC_DIGEST,
            input_digest=digest_of(upstream_digests),
            session_id=str(latest_row.session_id),
        )
        if latest_execution is None:
            raise PilotExecutionError("retry_not_allowed")
        ledger_row = self.ledger.get(latest_row.session_id)
        if ledger_row is None:
            raise PilotExecutionError("retry_not_allowed")
        affected = [RETRY_NODE, *PILOT_SPEC.downstream_closure(RETRY_NODE)]
        new_state = dict(state)
        for member in affected:
            member_state = dict(new_state["nodes"][member])
            if member_state["status"] in ("succeeded", "failed", "waiting_human"):
                member_state["status"] = "ready" if member == RETRY_NODE else "pending"
                if member == RETRY_NODE:
                    member_state["force_rerun"] = True
                    member_state["resume_intent"] = {
                        "schema": RESUME_INTENT_SCHEMA,
                        "execution_id": str(latest_execution["execution_id"]),
                        "session_id": str(latest_row.session_id),
                        "authorization_digest": str(
                            ledger_row["authorization_digest"]
                        ),
                    }
                member_state["error"] = None
                new_state = {**new_state, "nodes": {**new_state["nodes"], member: member_state}}
        new_state["run_status"] = "running"
        new_state["blocked_reason"] = None
        # 写路径一律从 raw 存储形态开始，绝不基于 compat view 回写；重读
        # raw 并与校验时的 sequence 对齐，漂移即 fail closed。
        raw_found = self.store.latest_raw_with_sequence(run_id)
        if raw_found is None:
            raise KeyError(run_id)
        raw_checkpoint, raw_sequence = raw_found
        if raw_sequence != sequence:
            raise PilotExecutionError("sequence_mismatch")
        committed = self.store.compare_and_append(
            replace(
                raw_checkpoint,
                current_node=RETRY_NODE,
                status="running",
                # form-preserving：旧 flat 保持 flat；namespaced 行写
                # system，顶层持续仅 system/plugins。
                eval_results=merge_runtime_eval_results(
                    existing=raw_checkpoint.eval_results,
                    content={"graph_state": new_state},
                ),
            ),
            expected_sequence=sequence,
            event_type="pilot_agent_retry",
            revision_reason="safe_retry",
        )
        if committed is None:
            raise PilotExecutionError("sequence_mismatch")

        # 与 decide 同一预算语义：本操作独立的 300 秒预算；真实发送仍受逐
        # attempt 会话结构限制（总 ≤2，重试不新增会话）。
        self._budgets.start(run_id)
        try:
            self._runtime.run_until_settled(run_id)
        finally:
            self._budgets.clear(run_id)
        final_checkpoint, final_state, _ = self._load_pilot_state(run_id)
        if (
            final_state.get("run_status") == "human_wait"
            and final_checkpoint.current_node == POST_GATE
        ):
            self._project_result(run_id, final_state)
            final_checkpoint, final_state, _ = self._load_pilot_state(run_id)
        # rev4 §1：只有确实完成恢复推进才报 retried；仍阻断在 Agent 节点时
        # 返回诚实的 retry_failed，绝不虚报成功。
        agent_state = final_state["nodes"].get(RETRY_NODE, {})
        recovered = agent_state.get("status") == "succeeded"
        outcome: dict[str, Any] = {
            "run_id": run_id,
            "node_id": RETRY_NODE,
            "status": "retried" if recovered else "retry_failed",
            "run_status": str(final_state.get("run_status", "unknown")),
            "current_node": final_checkpoint.current_node,
        }
        if not recovered:
            error = agent_state.get("error")
            outcome["error_code"] = (
                str(error.get("code")) if isinstance(error, dict) else "unknown"
            )
        return outcome

    # -- 双闸门决定钩子 -------------------------------------------------------

    def decide(self, run_id: str, node_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            checkpoint, state, _sequence = self._load_pilot_state(run_id)
        except PilotExecutionError:
            # 非 Pilot Run：原样委托既有路径，行为零变化。
            return self.service.decide(run_id, node_id, body)
        gate = state["human_gates"].get(node_id)
        if not isinstance(gate, dict):
            raise PilotExecutionError("unknown_gate")
        decision = str(body.get("decision", ""))

        if node_id == PRE_GATE and decision == "approve_call":
            self._verify_authorization(checkpoint, state, gate, body)
        elif node_id == POST_GATE:
            self._verify_result_digest(checkpoint, body)
        # 其他闸门/决定（如 pre_call_gate 的 abort）不需要额外绑定。

        outcome = self.service.decide(run_id, node_id, body)
        if outcome.get("idempotent"):
            return outcome

        # 决定处理总预算开始计时（300 秒；两次调用各 ≤120 秒；按 Run 隔离，
        # 结束或异常都清理，rev4 §2）。
        self._budgets.start(run_id)
        try:
            self._runtime.run_until_settled(run_id)
        finally:
            self._budgets.clear(run_id)
        final_checkpoint, final_state, _ = self._load_pilot_state(run_id)
        if (
            final_state.get("run_status") == "human_wait"
            and final_checkpoint.current_node == POST_GATE
        ):
            self._project_result(run_id, final_state)
            final_checkpoint, final_state, _ = self._load_pilot_state(run_id)
        return {
            **outcome,
            "run_status": str(final_state.get("run_status", "unknown")),
            "current_node": final_checkpoint.current_node,
        }

    def _verify_authorization(
        self,
        checkpoint: Any,
        state: Mapping[str, Any],
        gate: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> None:
        stored = checkpoint.eval_results.get("pilot_authorization")
        if not isinstance(stored, dict):
            raise PilotExecutionError("authorization_missing")
        expected = authorization_digest_of_material(_material_of(stored))
        if body.get("authorization_digest") != expected:
            # 伪造或漂移的授权摘要：零调用、零写入（反例 22）。
            raise PilotExecutionError("authorization_digest_mismatch")
        if self._clock() >= _parse_iso(stored.get("expires_at")):
            raise PilotExecutionError("authorization_expired")
        if int(stored.get("expected_sequence", -1)) != int(gate["expected_sequence"]):
            raise PilotExecutionError("sequence_mismatch")
        self._check_candidate_version(str(checkpoint.run_id))

    def _verify_result_digest(self, checkpoint: Any, body: Mapping[str, Any]) -> None:
        stored = checkpoint.eval_results.get("pilot_result")
        result_digest = stored.get("result_digest") if isinstance(stored, dict) else None
        if not isinstance(result_digest, str) or not result_digest:
            raise PilotExecutionError("result_missing")
        if body.get("result_digest") != result_digest:
            # 结果已变化，旧决定失效：零写入冲突（反例 31）。
            raise PilotExecutionError("result_digest_mismatch")

    def _project_result(self, run_id: str, state: Mapping[str, Any]) -> None:
        """推进至次闸时把安全结构化结果与 result_digest 写入 Run 状态（§3.8）。"""
        validator = state["nodes"].get(VALIDATOR_NODE, {})
        output = validator.get("output")
        if (
            validator.get("status") == "succeeded"
            and isinstance(output, dict)
            and output.get("valid") is True
        ):
            content: dict[str, Any] = {
                "available": True,
                "summary": output["summary"],
                "unknowns": list(output["unknowns"]),
                "next_checks": list(output["next_checks"]),
            }
        else:
            content = {"available": False}
        result = {**content, "result_digest": digest_of(content)}
        existing = self.store.latest(run_id)
        if existing is not None and existing.eval_results.get("pilot_result") == result:
            return
        self._append_audit(
            run_id,
            lambda _assigned: {"pilot_result": result},
            event_type=_AUDIT_RESULT,
        )
