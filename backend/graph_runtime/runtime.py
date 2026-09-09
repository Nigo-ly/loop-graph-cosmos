"""Checkpoint-backed executable Graph runtime (Phase 1, frozen slice).

Every Graph run is a controlled use of ``common.checkpoint.LoopCheckpoint``:
``loop_id="graph:<graph_id>"``, ``loopspec_version=<GraphSpec version>``, and
``eval_results["graph_state"]`` carries the spec digest, per-node status,
attempts, ready set, edge decisions, joins, feedback counters, human gates,
and input/output references plus digests. Every scheduling boundary is one
``compare_and_append`` commit; on conflict the state is re-read and the step
is re-evaluated, at most 8 times. Node handlers come only from the explicit
adapter registry — a GraphSpec can never name an import path or arbitrary
code. One ``step`` commits at most one node boundary; parallel branches are
multiple ``ready`` entries that callers may race for, and the CAS single
winner plus stable node-ID order keep the join deterministic.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from common.checkpoint import (
    LoopCheckpoint,
    SQLiteCheckpointStore,
    checkpoint_compat_view,
    utc_now,
)
from common.execution import (
    ExecutionReservations,
    Reservation,
    build_adapter_inventory,
    new_owner_token,
)
from common.types import (
    ACTUAL_REGISTRY_CLASSIFICATIONS,
    EXECUTION_RECEIPT_KEYS,
    EXECUTION_RECEIPT_SCHEMA,
    eval_results_digest,
)
from graph_runtime.agent_ledger import (
    STATUS_COMPLETED,
    AgentExecutionLink,
    AgentLedgerStore,
    validate_resume_intent,
)
from graph_runtime.node_state_v2 import EDGE_DELTA_SCHEMA
from graph_runtime.spec import (
    EXECUTION_NAMESPACE,
    GraphEdge,
    GraphNode,
    GraphSpec,
    canonical_json,
    digest_of,
    plain_copy,
)
from graph_runtime.storage_v2 import (
    OBJECT_MEDIA_TYPE,
    STORAGE_SCHEMA,
    StorageV2,
    StorageV2Error,
    compact_state,
    object_digest,
)

GRAPH_STATE_SCHEMA = "graph-state-v1"
# G3 ResultUnit 边界写入的显式 opt-in graph（gate_41b46efbf4c2）；
# 其他 v2/v1 run 零行为变化。
RESULT_UNIT_ENABLED_GRAPHS = frozenset({"fragment-research-macro-v2", "fragment-research-macro-v3"})
# handler 声明只允许业务字段；source/creator/identity 由 trusted context 补。
RESULT_UNIT_DECLARATION_FIELDS = frozenset(
    {
        "kind",
        "status",
        "summary",
        "local_key",
        "evidence_refs",
        "depends_on",
        "valid_until",
        "next_step",
        "content_ref",
    }
)
MAX_CAS_RETRIES = 8
# Hard cap on one node's canonical-JSON output bytes. Sized to carry the
# frozen Phase 1 full local read-only memory projection (54 nodes /
# 72 relations, 57,582 canonical bytes) with headroom — it is a bounded
# ceiling, not an unlimited allowance.
MAX_OUTPUT_BYTES = 64 * 1024

NODE_PENDING = "pending"
NODE_READY = "ready"
NODE_SUCCEEDED = "succeeded"
NODE_FAILED = "failed"
NODE_WAITING_HUMAN = "waiting_human"
NODE_SKIPPED = "skipped"
NODE_CANCELLED = "cancelled"
NODE_TERMINAL = frozenset((NODE_SUCCEEDED, NODE_FAILED, NODE_SKIPPED, NODE_CANCELLED))

RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_HUMAN_WAIT = "human_wait"
RUN_BLOCKED = "blocked"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"

GATE_PENDING = "pending"
GATE_RESOLVED = "resolved"
GATE_EXPIRED = "expired"

MemoryProvider = Callable[[tuple[str, ...]], Mapping[str, Any]]
NodeHandler = Callable[["NodeRequest"], "NodeResult"]


class GraphRuntimeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GraphDecisionError(GraphRuntimeError):
    """Human-gate binding mismatch: the write is refused with zero writes."""


@dataclass(frozen=True)
class NodeRequest:
    run_id: str
    node_id: str
    attempt: int
    spec_digest: str
    fragment_ref: str
    inputs: dict[str, Any]
    input_refs: tuple[str, ...]
    run_inputs: dict[str, Any]
    memory: MemoryProvider | None
    # 前置层 reservation 权威身份，runtime 在 reserve 成功后注入；
    # 仅 Agent adapter 用于旁路 attach。
    execution_id: str | None = None


@dataclass(frozen=True)
class NodeResult:
    output: dict[str, Any]
    output_refs: tuple[str, ...] = ()
    failure: str | None = None
    material_insufficient: bool = False
    route: str | None = None
    # G3 ResultUnit 声明（opt-in graph；不得自报 source/creator/identity，
    # 由 commit 边界以 trusted context 补全并同事务写入）。
    result_units: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class StepOutcome:
    committed: bool
    event: str
    node_id: str | None = None
    sequence: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def human_decision_id(
    *,
    requester: str,
    decision: str,
    run_id: str,
    node_id: str,
    spec_digest: str,
    input_digest: str,
    expected_sequence: int,
) -> str:
    """Server-recomputed human-gate decision identity."""
    return digest_of(
        [
            "graph-human-decision-v1",
            requester,
            decision,
            run_id,
            node_id,
            spec_digest,
            input_digest,
            str(expected_sequence),
        ]
    )


def reopen_request_id(
    *,
    requester: str,
    run_id: str,
    node_id: str,
    expected_sequence: int,
    reason: str,
) -> str:
    return digest_of(
        ["graph-node-reopen-v1", requester, run_id, node_id, str(expected_sequence), reason]
    )


def evaluate_condition(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """Deterministic closed-world condition evaluation."""
    field_name = str(condition["field"])
    if field_name not in context:
        return False
    actual = context[field_name]
    expected = condition["value"]
    op = condition["op"]
    if op == "equals":
        return bool(actual == expected)
    if op == "not_equals":
        return bool(actual != expected)
    if op == "in":
        return any(actual == item for item in expected)
    return False


def _schema_type_ok(expected: str, value: Any) -> bool:
    """Strict runtime type check for one declared schema type."""
    if expected == "any":
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False  # unknown declared types never pass


def schema_mismatch(schema: Mapping[str, str], value: Mapping[str, Any]) -> str | None:
    """Minimal runtime validation of ``value`` against a declared field schema.

    Every field in ``schema`` is required; extra fields in ``value`` are
    allowed. Returns the first stable reason — ``missing:<field>`` or
    ``type:<field>`` — or ``None`` when the value conforms.
    """
    for field_name in sorted(schema):
        if field_name not in value:
            return f"missing:{field_name}"
        if not _schema_type_ok(str(schema[field_name]), value[field_name]):
            return f"type:{field_name}"
    return None


# Conflict marker: flatten_inputs returns a string instead of a merged dict
# when two upstream outputs declare the same key with different values. The
# marker can never pass schema_mismatch, so the run blocks fail-closed.
def flatten_inputs(inputs: Mapping[str, Any]) -> dict[str, Any] | str:
    """Merge upstream output dicts keyed by upstream node id into one dict.

    Deterministic (ascending upstream node id). Same key with an equal value
    is fine; same key with different values returns the conflict marker
    ``conflict:<field>``.
    """
    merged: dict[str, Any] = {}
    for source in sorted(inputs):
        output = inputs[source]
        if not isinstance(output, dict):
            continue
        for key, value in output.items():
            if key in merged and merged[key] != value:
                return f"conflict:{key}"
            merged[key] = value
    return merged


def _initial_state(spec: GraphSpec, run_inputs: dict[str, Any]) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for node in spec.nodes:
        nodes[node.id] = {
            "status": NODE_READY if node.id == spec.entry_node else NODE_PENDING,
            "attempts": 0,
            "force_rerun": False,
            "input_digest": None,
            "output_digest": None,
            "output": None,
            "output_refs": [],
            "input_refs": [],
            "error": None,
            "completed_at": None,
            "child_run_id": None,
            "absorbed_failures": [],
            "last_succeeded": None,
        }
    return {
        "schema": GRAPH_STATE_SCHEMA,
        "graph_id": spec.graph_id,
        "spec_version": spec.version,
        "spec_digest": spec.digest,
        "run_status": RUN_RUNNING,
        "run_inputs": run_inputs,
        "step_count": 0,
        "started_at": utc_now(),
        "nodes": nodes,
        "edges_taken": [],
        "feedback_counts": {},
        "human_gates": {},
        "blocked_reason": None,
    }


class GraphRuntime:
    """Stateless scheduler: recovery is a fresh instance over the same store."""

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        specs: Mapping[str, GraphSpec],
        adapters: Mapping[str, NodeHandler],
        memory_provider: MemoryProvider | None = None,
        execution_classes: Mapping[str, str] | None = None,
        receipt_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
        storage_version: str = "v1",
    ):
        if storage_version not in ("v1", "v2"):
            raise ValueError(f"unknown storage_version: {storage_version}")
        self.store = store
        self.specs = dict(specs)
        self.adapters = dict(adapters)
        self.memory_provider = memory_provider
        self._reservations = ExecutionReservations(store.path)
        # G2：v2 仅显式选择的合成/新测试 run；默认 v1 writer（生产兼容）。
        self._storage_v2 = (
            StorageV2(store.path) if storage_version == "v2" else None
        )
        if self._storage_v2 is not None:
            self._storage_v2.ensure_schema()
        # G1-15: idempotent recovery reconciles through this caller-provided
        # receipt lookup keyed by the stable execution id — never a new key.
        self._receipt_provider = receipt_provider
        # G1-25/26 强制面：显式 execution_classes 优先；None 时消费冻结的
        # ACTUAL_REGISTRY_CLASSIFICATIONS——handlers 中任何未知 key 在构造期
        # fail closed（missing），绝不隐式默认、绝不 NULL 分类进 handler。
        if execution_classes is None:
            execution_classes = {
                key: ACTUAL_REGISTRY_CLASSIFICATIONS[key]["execution_class"]
                for key in self.adapters
                if key in ACTUAL_REGISTRY_CLASSIFICATIONS
            }
        self._inventory = build_adapter_inventory(
            self.adapters,
            runtime_kind="graph",
            classifications=execution_classes,
        )
        self._wire_agent_execution_links()

    def _wire_agent_execution_links(self) -> None:
        """显式关联协议接线：只对声明 EXECUTION_LINK_PROTOCOL 的 adapter
        实例调其显式 bind_execution_link；digest 一律来自 runtime
        inventory（与分类 metadata 同一权威来源）。actual registry 标注
        需要关联但 handler 未实现/未接线协议的 Agent key 不在此接线——
        其节点由 step 在 handler/transport 前稳定停止（P0-4）。"""
        for key, handler in self.adapters.items():
            target = getattr(handler, "__self__", None)
            if target is None or getattr(target, "EXECUTION_LINK_PROTOCOL", None) is None:
                continue
            entry = self._inventory.get(key)
            if entry is None:  # 防御：构造期 inventory 已全覆盖
                continue
            target.bind_execution_link(
                AgentExecutionLink(self.store.path), entry["metadata_digest"]
            )

    # -- state access -----------------------------------------------------

    def _load(self, run_id: str) -> tuple[LoopCheckpoint, dict[str, Any], int]:
        # G2 v2：head 存在即走 dual-reader 重建（stub 行 payload 不解析）；
        # 否则回退 v1 raw 读取（Legacy flat/namespaced 行不变）。
        if self._storage_v2 is not None:
            with self._storage_v2._session() as connection:
                if self._storage_v2.head_of(connection, run_id) is not None:
                    state = self._storage_v2.latest(connection, run_id)
                    if state is None:
                        raise GraphRuntimeError("not_a_graph_run")
                    # 快照跳过未触碰 pending 节点：按 spec 补全默认 pending，
                    # 恢复完整 graph_state（下游 _propagate/_inputs_for 依赖
                    # 全节点集合）。
                    spec = self.specs.get(str(state["graph_id"]))
                    if spec is not None:
                        template = _initial_state(spec, {})["nodes"]
                        nodes = state.setdefault("nodes", {})
                        for node in spec.nodes:
                            existing = nodes.setdefault(node.id, template[node.id])
                            if existing is not template[node.id]:
                                for key, value in template[node.id].items():
                                    existing.setdefault(key, value)
                            # v2 投影不含 child_run_id：child id 是
                            # run_id+node_id 的确定性函数（与注册路径同一
                            # 表达式），注册事实由已持久化 input_digest 指示
                            # （template 默认 None，故显式判空而非 setdefault）。
                            if (
                                node.kind == "subgraph"
                                and existing.get("input_digest") is not None
                                and existing.get("child_run_id") is None
                            ):
                                existing["child_run_id"] = f"{run_id}:sub:{node.id}"
                    row = connection.execute(
                        "SELECT loop_id, fragment_id, iteration, current_node,"
                        " status FROM checkpoints WHERE run_id = ?"
                        " ORDER BY sequence DESC LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise GraphRuntimeError("not_a_graph_run")
                    stub_checkpoint = LoopCheckpoint(
                        loop_id=str(row["loop_id"]),
                        run_id=run_id,
                        fragment_id=str(row["fragment_id"]),
                        current_node=str(row["current_node"]),
                        status=str(row["status"]),
                        iteration=int(row["iteration"]),
                        eval_results={"system": {}, "plugins": {}},
                    )
                    return stub_checkpoint, state, int(state.get("head_sequence", 0))
        # Raw stored form: _commit must write back into the row's own
        # namespace form, never into a compat view.
        found = self.store.latest_raw_with_sequence(run_id)
        if found is None:
            raise KeyError(f"Unknown run: {run_id}")
        checkpoint, sequence = found
        eval_results = checkpoint.eval_results
        state = eval_results.get("graph_state")  # legacy flat row
        if state is None:
            system = eval_results.get("system")
            if isinstance(system, dict):
                state = system.get("graph_state")  # namespaced row
        if state is None and self._storage_v2 is not None:
            # G2 v2 stub 行：状态全部由 v2 表重建（head + snapshot + 有界
            # replay）；succeeded 节点 output body 按需经完整性校验载入。
            with self._storage_v2._session() as connection:
                state = self._storage_v2.latest(connection, run_id)
        if not isinstance(state, dict) or state.get("schema") != GRAPH_STATE_SCHEMA:
            raise GraphRuntimeError("not_a_graph_run")
        return checkpoint, state, sequence

    def _spec_for(self, state: Mapping[str, Any]) -> GraphSpec:
        spec = self.specs.get(str(state["graph_id"]))
        if spec is None or spec.digest != state["spec_digest"]:
            raise GraphRuntimeError("spec_unavailable")
        return spec

    # -- registration -----------------------------------------------------

    @staticmethod
    def _validate_registration(
        existing: LoopCheckpoint | None,
        spec: GraphSpec,
        fragment_ref: str,
        run_inputs: dict[str, Any],
    ) -> LoopCheckpoint | None:
        """Idempotent-replay check: an existing checkpoint is returned only when
        its recorded registration binding matches graph_id, spec_digest,
        fragment_ref and run_inputs exactly; any mismatch is a conflict."""
        if existing is None:
            return None
        binding = existing.eval_results.get("registration")
        if isinstance(binding, dict) and binding == {
            "graph_id": spec.graph_id,
            "spec_digest": spec.digest,
            "fragment_ref": fragment_ref,
            "run_inputs": run_inputs,
        }:
            return existing
        raise GraphRuntimeError("run_id_conflict")

    def register_run(
        self,
        spec: GraphSpec,
        run_id: str,
        *,
        fragment_ref: str,
        run_inputs: dict[str, Any] | None = None,
    ) -> tuple[LoopCheckpoint, bool]:
        """Register a run; replaying the same run_id+spec is idempotent."""
        if not run_id.startswith(EXECUTION_NAMESPACE):
            raise GraphRuntimeError("run_id_namespace")
        if len(run_id) > 256 or any(
            ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F or ch in "/\\"
            for ch in run_id
        ):
            raise GraphRuntimeError("run_id_invalid")
        inputs = dict(run_inputs or {})
        existing = self._validate_registration(
            self.store.latest(run_id), spec, fragment_ref, inputs
        )
        if existing is not None:
            return existing, False
        checkpoint = LoopCheckpoint(
            loop_id=f"graph:{spec.graph_id}",
            run_id=run_id,
            fragment_id=fragment_ref,
            current_node=spec.entry_node,
            status=RUN_RUNNING,
            loopspec_version=spec.version,
            goal=spec.goal,
            # Gate 1：新 Graph 登记行的 runtime 材料一律写入 system 命名
            # 空间并带空 plugins 壳；无 flat 业务键。插件（adapter）没有
            # eval 写通道，永远无法覆盖 system。
            eval_results={
                "system": {
                    "graph_state": _initial_state(spec, inputs),
                    "registration": {
                        "graph_id": spec.graph_id,
                        "spec_digest": spec.digest,
                        "fragment_ref": fragment_ref,
                        "run_inputs": inputs,
                    },
                },
                "plugins": {},
            },
        )
        if self._storage_v2 is not None:
            # G2 v2 注册单事务：stub checkpoint + anchor + head + 初始节点
            # 投影同一 commit——两事务崩溃窗口不存在。
            assert self._storage_v2 is not None
            initial_state = _initial_state(spec, inputs)
            stub = {
                "run_id": run_id,
                "loop_id": checkpoint.loop_id,
                "fragment_id": fragment_ref,
                "iteration": 0,
                "current_node": spec.entry_node,
                "status": RUN_RUNNING,
                "event_type": "run_registered",
                "revision_reason": None,
                "expected_sequence": 0,
            }
            head = {
                "runtime_kind": "graph",
                "loop_id": checkpoint.loop_id,
                "fragment_id": fragment_ref,
                "run_family_id": run_id,
                "episode": 0,
                "pending_human": 0,
            }
            event = {
                "node_id": spec.entry_node,
                "status": "ready",
                "attempt": 0,
                "detail": {},
            }
            try:
                self._storage_v2.commit_boundary(
                    stub=stub,
                    head=head,
                    event=event,
                    compacted_state=compact_state(initial_state),
                    snapshot_forced=True,
                )
            except StorageV2Error as error:
                if error.code != "sequence_conflict":
                    raise
                raced = self._validate_registration(
                    self.store.latest(run_id), spec, fragment_ref, inputs
                )
                if raced is not None:
                    return raced, False
                raise GraphRuntimeError("cas_conflict") from error
            return checkpoint, True
        committed = self.store.compare_and_append(
            checkpoint, expected_sequence=0, event_type="run_registered"
        )
        if committed is None:
            raced = self._validate_registration(
                self.store.latest(run_id), spec, fragment_ref, inputs
            )
            if raced is not None:
                return raced, False
            raise GraphRuntimeError("cas_conflict")
        return committed, True

    # -- scheduling -------------------------------------------------------

    def step(self, run_id: str) -> StepOutcome:
        """Advance at most one node boundary, CAS-committed."""
        owner = new_owner_token()
        # 结果重提交 memo（Gate 1 P0-1）：adapter 一旦入场，本 step() 内的
        # CAS 冲突只重放提交同一份内存结果，绝不二次进入 handler。
        memo: dict[str, Any] = {}
        for _attempt in range(MAX_CAS_RETRIES):
            checkpoint, state, sequence = self._load(run_id)
            spec = self._spec_for(state)
            status = str(state["run_status"])
            parked: tuple[str, dict[str, Any]] | None = None
            if status in (RUN_HUMAN_WAIT, RUN_PAUSED, RUN_BLOCKED):
                # A parent run parked only because its subgraph child waited,
                # paused or blocked re-checks the child on the next step; a
                # real pending human gate or an ordinary pause/block on this
                # run can never be stepped past. The original status/reason is
                # kept so an unchanged child wait can noop with zero writes.
                pending_gates = [
                    gate for gate in state["human_gates"].values() if gate["status"] == GATE_PENDING
                ]
                reason = state.get("blocked_reason") or {}
                if not pending_gates and str(reason.get("code", "")).startswith("child_"):
                    parked = (status, reason)
                    state = self._with_run_status(state, RUN_RUNNING, None)
                else:
                    return StepOutcome(False, f"noop_{status}", sequence=sequence)
            elif status != RUN_RUNNING:
                return StepOutcome(False, f"noop_{status}", sequence=sequence)
            if spec.integrity_digest() != spec.digest:
                # Gate 1: 运行前完整性重算。任何绕过深冻结的内存破坏都在
                # handler reservation 之前安全停止：零 handler 调用，稳定
                # blocked 事件。终态 run 不进入调度，在上方 noop 返回，不
                # 会被此处改写。
                new_state = self._with_run_status(
                    state,
                    RUN_BLOCKED,
                    {
                        "code": "spec_digest_mismatch",
                        "node_id": None,
                        "message": "spec content digest differs from the pinned digest",
                    },
                )
                outcome = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="loop_stopped",
                    current_node=checkpoint.current_node,
                    revision_reason="spec_digest_mismatch",
                )
                if outcome is None:
                    continue
                return StepOutcome(
                    True,
                    "blocked",
                    sequence=outcome[1],
                    detail={"code": "spec_digest_mismatch"},
                )
            budget = spec.budgets.get("max_node_executions")
            if budget is not None:
                # Gate 1: the budget limits actual handler calls (persistent
                # reservation entries), not committed successes — failures,
                # feedback re-entries and recovery attempts all consume it.
                # state["step_count"] stays as the legacy floor so runs
                # registered before reservations existed keep their guard.
                actual_calls = max(
                    int(state["step_count"]),
                    self._reservations.entry_count("graph", run_id),
                )
                if actual_calls >= budget:
                    new_state = self._with_run_status(
                        state,
                        RUN_FAILED,
                        {
                            "code": "budget_exhausted",
                            "node_id": None,
                            "message": "max_node_executions",
                        },
                    )
                    outcome = self._commit(
                        checkpoint,
                        new_state,
                        sequence,
                        event_type="loop_stopped",
                        current_node=checkpoint.current_node,
                    )
                    if outcome is None:
                        continue
                    return StepOutcome(True, "failed", sequence=outcome[1])

            node_id = self._next_executable(spec, state)
            if node_id is None:
                return self._settle(spec, checkpoint, state, sequence)
            step_outcome = self._execute_node(
                spec, checkpoint, state, sequence, node_id, parked, owner, memo
            )
            if step_outcome is not None:
                return step_outcome
        raise GraphRuntimeError("cas_conflict")

    def run_until_settled(self, run_id: str, *, max_steps: int = 512) -> StepOutcome:
        """Deterministic driver for fixtures/tests; never loops forever."""
        last = StepOutcome(False, "noop")
        for _ in range(max_steps):
            last = self.step(run_id)
            if last.event.startswith("noop_") or last.event in (
                "completed",
                "failed",
                "blocked",
                "paused",
                "human_gate_requested",
            ):
                return last
        raise GraphRuntimeError("driver_budget_exhausted")

    def _next_executable(self, spec: GraphSpec, state: Mapping[str, Any]) -> str | None:
        nodes = state["nodes"]
        for node in spec.nodes:
            if nodes[node.id]["status"] != NODE_READY:
                continue
            # A ready subgraph is never skipped because its child waits:
            # _step_subgraph projects the child status onto the parent.
            return node.id
        return None

    def _child_status(self, child_run_id: str) -> str:
        if self._storage_v2 is not None:
            # v2 child 是 stub 行：run_status 由 v2 head 提供（dual-reader），
            # 绝不走 v1 payload 解析。
            with self._storage_v2._session() as connection:
                head = self._storage_v2.head_of(connection, child_run_id)
            if head is None:
                return RUN_RUNNING
            return str(head["status"])
        found = self.store.latest_with_sequence(child_run_id)
        if found is None:
            return RUN_RUNNING
        checkpoint, _sequence = found
        state = checkpoint.eval_results.get("graph_state", {})
        return str(state.get("run_status", RUN_RUNNING))

    def _commit(
        self,
        checkpoint: LoopCheckpoint,
        new_state: dict[str, Any],
        expected_sequence: int,
        *,
        event_type: str,
        current_node: str,
        revision_reason: str | None = None,
        execution_id: str | None = None,
        owner_token: str = "",
    ) -> tuple[LoopCheckpoint, int] | None:
        new_state = dict(new_state)
        # 瞬态 edge delta（不入任何持久化状态）：由 _propagate/
        # _traverse_feedback/_feedback_exhausted 随 new_state 携带，
        # 仅此 commit 消费一次；v1 payload 与 v2 anchor 均不可见。
        edge_deltas = new_state.pop("_edge_deltas", None)
        status_resets = new_state.pop("_status_resets", None)
        declared_result_units = new_state.pop("_result_units", None)
        new_state["step_count"] = int(new_state.get("step_count", 0)) + (
            1 if event_type == "node_completed" else 0
        )
        if self._storage_v2 is not None:
            return self._commit_v2(
                checkpoint,
                new_state,
                expected_sequence,
                event_type=event_type,
                current_node=current_node,
                revision_reason=revision_reason,
                execution_id=execution_id,
                owner_token=owner_token,
                edge_deltas=edge_deltas,
                status_resets=status_resets,
                declared_result_units=declared_result_units,
            )
        current_eval = checkpoint.eval_results
        if "system" in current_eval or "plugins" in current_eval:
            # Namespaced row: graph_state is runtime system material.
            system = dict(current_eval.get("system") or {})
            system["graph_state"] = new_state
            eval_results = {**current_eval, "system": system}
        else:
            # Legacy flat row keeps its flat form: never migrated.
            eval_results = {**current_eval, "graph_state": new_state}
        committed = self.store.compare_and_append(
            replace(
                checkpoint,
                current_node=current_node,
                status=str(new_state["run_status"]),
                iteration=int(new_state["step_count"]),
                eval_results=eval_results,
            ),
            expected_sequence=expected_sequence,
            event_type=event_type,
            revision_reason=revision_reason,
        )
        if committed is None:
            return None
        latest = self.store.latest_sequence(checkpoint.run_id)
        return committed, int(latest or 0)

    def _commit_v2(
        self,
        checkpoint: LoopCheckpoint,
        new_state: dict[str, Any],
        expected_sequence: int,
        *,
        event_type: str,
        current_node: str,
        revision_reason: str | None,
        execution_id: str | None,
        owner_token: str,
        edge_deltas: list[dict[str, Any]] | None = None,
        status_resets: dict[str, str] | None = None,
        declared_result_units: list[dict[str, Any]] | None = None,
    ) -> tuple[LoopCheckpoint, int] | None:
        """G2 v2 边界提交：stub checkpoint 行（CAS）+ 单事务 object/event/
        snapshot/head/reservation。状态全部由 v2 表重建（dual-reader）。"""
        assert self._storage_v2 is not None
        node_state = (new_state.get("nodes") or {}).get(current_node) or {}
        output = node_state.get("output") if event_type == "node_completed" else None
        output_body = (
            canonical_json(output) if isinstance(output, dict) and output else None
        )
        if output_body is not None:
            # object_ref 随 graph_state 持久化：周期快照之后的 replay 能按
            # 需为 snapshot 内的 succeeded 节点恢复 output body；
            # output 与 last_succeeded 显式指向同一 object_ref（零正文复制）。
            node_state["output_ref"] = object_digest(
                schema_version=STORAGE_SCHEMA,
                media_type=OBJECT_MEDIA_TYPE,
                body=output_body,
            )
            if isinstance(node_state.get("last_succeeded"), dict):
                node_state["last_succeeded"]["output_ref"] = node_state["output_ref"]
        detail: dict[str, Any] = {"run_status": new_state["run_status"]}
        # 就绪集是调度关键状态：persist 本边界后全部 ready 节点（幂等
        # 增量 upsert/replay），覆盖 sequence 扇出、condition 选中、
        # feedback 回退目标与 exhausted route 激活——不只 node_completed
        # 的 sequence 下游，否则 feedback/condition 的 ready 事实丢失。
        ready_now = sorted(
            node_id
            for node_id, node in (new_state.get("nodes") or {}).items()
            if isinstance(node, dict) and node.get("status") == "ready"
        )
        if ready_now:
            detail["ready_nodes"] = ready_now
        # condition 未选中分支的 skip 事实同样是调度关键状态：缺失时
        # v2 恢复把 skipped 当成 pending，run 无法推进（no_progress）。
        skipped_now = sorted(
            node_id
            for node_id, node in (new_state.get("nodes") or {}).items()
            if isinstance(node, dict) and node.get("status") == "skipped"
        )
        if skipped_now:
            detail["skipped_nodes"] = skipped_now
        if status_resets:
            # feedback span / reopen 的显式 pending 重置（保留执行事实列）。
            detail["status_resets"] = dict(status_resets)
        if revision_reason:
            detail["revision_reason"] = revision_reason
        if node_state.get("error") is not None:
            detail["error"] = node_state["error"]
        if event_type == "feedback_traversed" and revision_reason:
            detail["feedback_edge"] = revision_reason
        stub = {
            "run_id": checkpoint.run_id,
            "loop_id": checkpoint.loop_id,
            "fragment_id": checkpoint.fragment_id,
            "iteration": int(new_state.get("step_count", 0)),
            "current_node": current_node,
            "status": str(new_state["run_status"]),
            "event_type": event_type,
            "revision_reason": revision_reason,
            "expected_sequence": expected_sequence,
        }
        head = {
            "runtime_kind": "graph",
            "loop_id": checkpoint.loop_id,
            "fragment_id": checkpoint.fragment_id,
            "run_family_id": checkpoint.run_id,
            "episode": 0,
            "pending_human": sum(
                1
                for gate in (new_state.get("human_gates") or {}).values()
                if gate.get("status") == "pending"
            ),
        }
        event = {
            "node_id": current_node,
            "status": node_state.get("status"),
            "output_digest": node_state.get("output_digest"),
            "input_digest": (node_state.get("last_succeeded") or {}).get("input_digest")
            or node_state.get("input_digest"),
            "attempt": node_state.get("attempts"),
            "last_succeeded": node_state.get("last_succeeded"),
            "error": node_state.get("error"),
            "detail": detail,
        }
        if edge_deltas:
            # 真实 runtime 产生的精确版本化 edge delta（sequence/
            # condition/feedback/exhausted route/retry）：与 edges_taken
            # 条目完全同源，由 immutable event detail 持久化。
            event["edges"] = [
                {"schema": EDGE_DELTA_SCHEMA, **delta} for delta in edge_deltas
            ]
        prepared_units = self._prepare_result_units(
            checkpoint,
            new_state,
            current_node=current_node,
            event_type=event_type,
            execution_id=execution_id,
            node_state=node_state,
            declared=declared_result_units,
        )
        try:
            sequence = self._storage_v2.commit_boundary(
                stub=stub,
                head=head,
                event=event,
                output_body=output_body,
                compacted_state=compact_state(new_state),
                # gate 决策与 reopen 是 run 级里程碑：强制 anchor 使
                # resolved/expired gate 状态可恢复（否则恢复回 pending）。
                snapshot_forced=event_type
                in (
                    "loop_stopped",
                    "human_gate_requested",
                    "human_decision_applied",
                    "node_reopened",
                ),
                execution_id=execution_id,
                owner_token=owner_token,
                result_units=prepared_units,
            )
        except StorageV2Error as error:
            if error.code == "sequence_conflict":
                return None
            raise
        return checkpoint, sequence

    def _prepare_result_units(
        self,
        checkpoint: LoopCheckpoint,
        new_state: dict[str, Any],
        *,
        current_node: str,
        event_type: str,
        execution_id: str | None,
        node_state: dict[str, Any],
        declared: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """G3 ResultUnit 边界准备（gate_41b46efbf4c2）：handler 声明只含
        业务字段（禁止自报 source/creator/identity），source/creator 由
        trusted context 补全；opt-in 仅限 RESULT_UNIT_ENABLED_GRAPHS，
        其他 v2/v1 run 零行为变化；human_decision_applied 自动生成绑定
        decision_id 的 Decision unit（requester 来自 gate trusted 绑定）。"""
        graph_id = str(new_state.get("graph_id"))
        opt_in = graph_id in RESULT_UNIT_ENABLED_GRAPHS
        if declared and not opt_in:
            raise GraphRuntimeError("result_units_not_enabled")
        prepared: list[dict[str, Any]] = []
        if declared and opt_in:
            spec = self.specs[graph_id]
            node = spec.node_map.get(current_node)
            adapter = str(node.adapter) if node is not None and node.adapter else ""
            input_digest = (node_state.get("last_succeeded") or {}).get(
                "input_digest"
            ) or str(node_state.get("input_digest") or "")
            for declaration in declared:
                if not isinstance(declaration, dict) or not (
                    set(declaration) <= RESULT_UNIT_DECLARATION_FIELDS
                ):
                    raise GraphRuntimeError("result_unit_declaration_override")
                prepared.append(
                    {
                        **declaration,
                        "source": {
                            "run_id": checkpoint.run_id,
                            "node_id": current_node,
                            "execution_id": str(execution_id or ""),
                            "input_digest": input_digest,
                            "source_refs": [],
                        },
                        "creator": {
                            "type": "adapter",
                            "id": adapter,
                            "version": spec.version,
                        },
                        "created_at": utc_now(),
                    }
                )
        if opt_in and event_type == "human_decision_applied":
            gate = (new_state.get("human_gates") or {}).get(current_node) or {}
            decision_id = gate.get("decision_id")
            if decision_id:
                prepared.append(
                    {
                        "kind": "Decision",
                        "status": "decided",
                        "local_key": str(decision_id),
                        "summary": f"human decision: {gate.get('decision')}",
                        "source": {
                            "run_id": checkpoint.run_id,
                            "node_id": current_node,
                            "execution_id": str(decision_id),
                            "input_digest": str(gate.get("input_digest") or ""),
                            "source_refs": [str(gate.get("spec_digest") or "")],
                        },
                        "creator": {
                            "type": "human",
                            "id": str(gate.get("requester") or ""),
                            "version": "1.0.0",
                        },
                        "created_at": str(gate.get("decided_at") or utc_now()),
                    }
                )
        return prepared or None

    # -- node execution ---------------------------------------------------

    def _inputs_for(
        self, spec: GraphSpec, state: Mapping[str, Any], node_id: str
    ) -> tuple[dict[str, Any], list[str], str]:
        inputs: dict[str, Any] = {}
        refs: list[str] = []
        digests: dict[str, str | None] = {}
        for edge in spec.in_edges(node_id):
            if edge.type == "feedback":
                continue
            source = state["nodes"][edge.from_node]
            if source["status"] != NODE_SUCCEEDED:
                continue
            inputs[edge.from_node] = source["output"]
            digests[edge.from_node] = source["output_digest"]
            for ref in source["output_refs"]:
                if ref not in refs:
                    refs.append(ref)
        return inputs, refs, digest_of(digests)

    def _execute_node(
        self,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node_id: str,
        parked: tuple[str, dict[str, Any]] | None = None,
        owner_token: str = "",
        memo: dict[str, Any] | None = None,
    ) -> StepOutcome | None:
        node = spec.node_map[node_id]
        node_state = dict(state["nodes"][node_id])
        inputs, input_refs, input_digest = self._inputs_for(spec, state, node_id)

        # Declared input_schema is validated for every node kind — special
        # (join / human_decision / subgraph / output) and adapter-driven
        # alike — right after inputs are resolved and before the
        # last_succeeded short-circuit, so no node can run or be replayed
        # with inputs that violate its declared schema.
        if node.input_schema:
            if any(edge.type != "feedback" for edge in spec.in_edges(node_id)):
                flattened = flatten_inputs(inputs)
                input_mismatch = (
                    flattened
                    if isinstance(flattened, str)
                    else schema_mismatch(node.input_schema, flattened)
                )
            else:
                input_mismatch = schema_mismatch(
                    node.input_schema, state.get("run_inputs") or {}
                )
            if input_mismatch is not None:
                block_detail = {
                    "code": "input_schema_mismatch",
                    "node_id": node_id,
                    "message": input_mismatch,
                }
                new_state = self._with_run_status(state, RUN_BLOCKED, block_detail)
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="loop_stopped",
                    current_node=node_id,
                )
                if committed is None:
                    return None
                return StepOutcome(True, "blocked", node_id, committed[1], block_detail)

        # Input digest unchanged since the last success: never re-run.
        # Human gates and action nodes are never short-circuited: a human
        # confirmation can never be bypassed by a deterministic replay.
        last = node_state.get("last_succeeded")
        if (
            last
            and node.kind not in ("human_decision", "action")
            and not node_state.get("force_rerun")
            and last["input_digest"] == input_digest
        ):
            node_state.update(
                {
                    "status": NODE_SUCCEEDED,
                    "output": last["output"],
                    "output_digest": last["output_digest"],
                    "output_refs": list(last["output_refs"]),
                    "error": None,
                    "completed_at": utc_now(),
                }
            )
            new_state = self._with_node(state, node_id, node_state)
            new_state = self._propagate(spec, new_state, node_id, last["output"])
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="node_completed",
                current_node=node_id,
                revision_reason="input_unchanged",
            )
            if committed is None:
                return None
            return StepOutcome(True, "node_succeeded", node_id, committed[1])

        if node.kind == "human_decision":
            return self._enter_human_gate(
                checkpoint, state, sequence, node, input_digest, input_refs
            )
        if node.kind == "join":
            outcome = self._evaluate_join(spec, checkpoint, state, sequence, node)
            return outcome
        if node.kind == "action":
            gate_block = self._action_gate_block(spec, state, node)
            if gate_block is not None:
                new_state = self._with_run_status(state, RUN_BLOCKED, gate_block)
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="loop_stopped",
                    current_node=node_id,
                )
                if committed is None:
                    return None
                return StepOutcome(True, "blocked", node_id, committed[1], gate_block)
        if node.kind == "subgraph":
            return self._step_subgraph(
                spec, checkpoint, state, sequence, node, inputs, input_refs, input_digest, parked
            )
        if node.kind == "output":
            node_state.update(
                {
                    "status": NODE_SUCCEEDED,
                    "attempts": int(node_state["attempts"]) + 1,
                    "input_digest": input_digest,
                    "input_refs": input_refs,
                    "output": inputs,
                    "output_digest": digest_of(inputs),
                    "output_refs": input_refs,
                    "error": None,
                    "completed_at": utc_now(),
                    "last_succeeded": {
                        "input_digest": input_digest,
                        "output": inputs,
                        "output_digest": digest_of(inputs),
                        "output_refs": input_refs,
                    },
                }
            )
            new_state = self._propagate(
                spec, self._with_node(state, node_id, node_state), node_id, None
            )
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="node_completed",
                current_node=node_id,
            )
            if committed is None:
                return None
            return StepOutcome(True, "node_succeeded", node_id, committed[1])

        # input / router / capability / validator / action: handler-driven.
        # input_schema was already validated above for every node kind.
        assert node.adapter is not None
        handler = self.adapters.get(node.adapter)
        if handler is None:
            return self._commit_blocked(
                checkpoint,
                state,
                sequence,
                node_id,
                {
                    "code": "adapter_unavailable",
                    "node_id": node_id,
                    "message": f"adapter not registered: {node.adapter}",
                },
            )

        attempt = int(node_state["attempts"]) + 1
        # 结果重提交：本 step() 内同一绑定的 CAS 冲突重试直接复用内存结果，
        # 不二次 reserve、不二次进入 handler。
        if (
            memo is not None
            and memo.get("node_id") == node_id
            and memo.get("input_digest") == input_digest
            and memo.get("attempt") == attempt
        ):
            result = memo["result"]
            execution_id = memo["execution_id"]
        else:
            # G1-25/26: a classified runtime resolves the adapter's frozen
            # class + metadata digest before the reservation; an unclassified
            # adapter is blocked HERE, never silently executed.
            classification: str | None = None
            classification_digest: str | None = None
            inventory_entry: dict[str, str] | None = None
            if self._inventory is not None:
                inventory_entry = self._inventory.get(node.adapter)
                if inventory_entry is None:
                    return self._commit_blocked(
                        checkpoint,
                        state,
                        sequence,
                        node_id,
                        {
                            "code": "execution_classification_missing",
                            "node_id": node_id,
                            "message": f"adapter not classified: {node.adapter}",
                        },
                    )
                classification = inventory_entry["execution_class"]
                classification_digest = inventory_entry["metadata_digest"]
            # P0-4：actual registry 标注需要关联协议的 Agent adapter，其
            # handler 未实现/未接线协议时，在 handler/transport 前稳定停止。
            registry_entry = ACTUAL_REGISTRY_CLASSIFICATIONS.get(node.adapter)
            if registry_entry is not None and "link_protocol" in registry_entry:
                protocol_target = getattr(handler, "__self__", None)
                if (
                    protocol_target is None
                    or getattr(protocol_target, "EXECUTION_LINK_PROTOCOL", None)
                    != registry_entry["link_protocol"]
                    or getattr(protocol_target, "execution_link", None) is None
                ):
                    return self._commit_blocked(
                        checkpoint,
                        state,
                        sequence,
                        node_id,
                        {
                            "code": "agent_execution_link_missing",
                            "node_id": node_id,
                            "message": f"agent adapter not linked: {node.adapter}",
                        },
                    )
            request = NodeRequest(
                run_id=checkpoint.run_id,
                node_id=node_id,
                attempt=attempt,
                spec_digest=spec.digest,
                fragment_ref=checkpoint.fragment_id,
                inputs=inputs,
                input_refs=tuple(input_refs),
                run_inputs=dict(state.get("run_inputs", {})),
                memory=self.memory_provider,
            )
            # Gate 1: the actual adapter call is linearized on a persistent
            # reservation before any handler code runs. Concurrent losers get a
            # conflict noop and never enter the handler; they also never ride
            # along to advance successor nodes in this step.
            resume_intent = node_state.pop("resume_intent", None)
            if resume_intent is not None:
                # 闭集版本化 resume intent（R8）：结构验证 + 当前
                # checkpoint/spec/input/inventory 补全期望绑定；任何漂移
                # 零计数零发送。标记随本次消费清除（本地副本）。
                reservation = self._consume_resume_intent(
                    resume_intent,
                    spec=spec,
                    checkpoint=checkpoint,
                    node_id=node_id,
                    input_digest=input_digest,
                    inventory_entry=inventory_entry,
                    owner_token=owner_token,
                )
            else:
                reservation = self._reservations.reserve(
                    runtime_kind="graph",
                    run_id=checkpoint.run_id,
                    node_id=node_id,
                    spec_digest=spec.digest,
                    input_digest=input_digest,
                    owner_token=owner_token,
                    logical_attempt=attempt,
                    execution_class=classification,
                    classification_digest=classification_digest,
                )
            if not reservation.acquired:
                if reservation.reason in (
                    "external_hold",
                    "reconcile_required",
                    "already_committed",
                ):
                    # G1-14/G1-15: before giving up, try to commit a VERIFIED
                    # stored result (agent ledger completed row / idempotent
                    # receipt) — zero new handler entry, zero transport.
                    # already_committed covers the legacy half-finalized
                    # state: reservation committed while the node is still
                    # ready; recovery completes the checkpoint append.
                    recovered = self._recover_verified_result(
                        reservation,
                        spec,
                        checkpoint,
                        state,
                        sequence,
                        node,
                        node_id,
                        node_state,
                        input_digest,
                        input_refs,
                        attempt,
                    )
                    if recovered is not None:
                        return recovered
                return StepOutcome(
                    False, f"noop_execution_{reservation.reason}", node_id=node_id
                )
            execution_id = reservation.execution_id
            assert execution_id is not None  # acquired implies identity
            # the trusted reservation identity reaches the handler only
            # through the request (Agent adapter attaches on it).
            request = replace(request, execution_id=execution_id)
            # 入场即 running：同 owner 重入被拒，崩溃恢复可区分 pre-send。
            self._reservations.mark_running(execution_id, owner_token)
            try:
                result = handler(request)
            except Exception as error:  # handler failure is a node failure
                result = NodeResult(
                    output={},
                    failure=f"handler_exception:{type(error).__name__}",
                )
            except BaseException:
                # Interrupted mid-handler: the attempt is still consumed. Record
                # the failure so recovery opens the next logical attempt instead
                # of waiting out this owner's lease, then propagate.
                self._reservations.fail(execution_id, owner_token)
                raise
            if memo is not None:
                memo.update(
                    node_id=node_id,
                    input_digest=input_digest,
                    attempt=attempt,
                    result=result,
                    execution_id=execution_id,
                )
        encoded_size = len(canonical_json(result.output).encode("utf-8"))
        if result.failure is None and encoded_size > MAX_OUTPUT_BYTES:
            result = NodeResult(output={}, failure="output_too_large")

        if result.failure is not None:
            outcome = self._handle_failure(
                spec,
                checkpoint,
                state,
                sequence,
                node,
                node_state,
                input_digest,
                input_refs,
                attempt,
                result,
            )
            if outcome is not None:
                # The failed attempt committed its terminal/route state; it
                # stays a consumed actual call and is never refunded.
                self._reservations.fail(execution_id, owner_token)
            return outcome

        output = dict(result.output)
        if node.kind == "router":
            output.setdefault("route", result.route)
        # Declared output_schema is validated after the router route default
        # is merged in, so "route" itself can be declared. A mismatch becomes
        # an ordinary node failure through the existing _handle_failure path
        # (feedback edges, exhaustion policy, fail-closed) — same helper as
        # the input side. Special nodes (join / human_decision / subgraph /
        # output) never reach here: their outputs are runtime-built and the
        # spec pins their output_schema to an exact frozen shape.
        if node.output_schema:
            output_mismatch = schema_mismatch(node.output_schema, output)
            if output_mismatch is not None:
                result = NodeResult(output=output, failure="output_schema_mismatch")
                outcome = self._handle_failure(
                    spec,
                    checkpoint,
                    state,
                    sequence,
                    node,
                    node_state,
                    input_digest,
                    input_refs,
                    attempt,
                    result,
                )
                if outcome is not None:
                    self._reservations.fail(execution_id, owner_token)
                return outcome
        output_digest = digest_of(output)
        node_state.update(
            {
                "status": NODE_SUCCEEDED,
                "attempts": attempt,
                "force_rerun": False,
                "input_digest": input_digest,
                "input_refs": input_refs,
                "output": output,
                "output_digest": output_digest,
                "output_refs": list(result.output_refs),
                "error": None,
                "completed_at": utc_now(),
                "last_succeeded": {
                    "input_digest": input_digest,
                    "output": output,
                    "output_digest": output_digest,
                    "output_refs": list(result.output_refs),
                },
            }
        )
        if result.material_insufficient:
            node_state["material_insufficient"] = True
        new_state = self._with_node(state, node_id, node_state)
        if result.result_units:
            # G3 ResultUnit 声明随 new_state 瞬态携带，仅此 commit 消费。
            new_state["_result_units"] = list(result.result_units)
        if node.kind == "router":
            routed_state, route_block = self._select_condition_edge(
                spec, new_state, node, {**output, "route": result.route}
            )
            if route_block is not None:
                new_state = self._with_run_status(routed_state, RUN_BLOCKED, route_block)
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="loop_stopped",
                    current_node=node_id,
                    execution_id=execution_id,
                    owner_token=owner_token,
                )
                if committed is None:
                    return None
                if self._storage_v2 is None:
                    self._reservations.complete(execution_id, owner_token)
                return StepOutcome(True, "blocked", node_id, committed[1], route_block)
            new_state = routed_state
        else:
            new_state = self._propagate(spec, new_state, node_id, output)
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="node_completed",
            current_node=node_id,
            execution_id=execution_id,
            owner_token=owner_token,
        )
        if committed is None:
            return None
        if self._storage_v2 is None:
            self._reservations.complete(execution_id, owner_token)
        return StepOutcome(True, "node_succeeded", node_id, committed[1])

    # -- recovery from verified stored evidence -------------------------------

    def _consume_resume_intent(
        self,
        intent: Any,
        *,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        node_id: str,
        input_digest: str,
        inventory_entry: dict[str, str] | None,
        owner_token: str,
    ) -> Reservation:
        """闭集 resume intent 消费（R8）：结构验证后，以当前
        checkpoint/spec/input/inventory 补全期望绑定字段，转发全绑定
        resume_presend；intent 结构非法即 resume_intent_invalid。"""
        parsed = validate_resume_intent(intent)
        if parsed is None:
            return Reservation(False, "resume_intent_invalid")
        return AgentExecutionLink(self.store.path).resume_presend(
            execution_id=parsed["execution_id"],
            owner_token=owner_token,
            runtime_kind="graph",
            run_id=checkpoint.run_id,
            node_id=node_id,
            spec_digest=spec.digest,
            input_digest=input_digest,
            execution_class=(
                None if inventory_entry is None else inventory_entry["execution_class"]
            ),
            classification_digest=(
                None if inventory_entry is None else inventory_entry["metadata_digest"]
            ),
            session_id=parsed["session_id"],
            adapter_metadata_digest=(
                "" if inventory_entry is None else inventory_entry["metadata_digest"]
            ),
            authorization_digest=parsed["authorization_digest"],
        )

    def _commit_blocked(
        self,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node_id: str,
        detail: dict[str, Any],
    ) -> StepOutcome | None:
        """Shared blocked-at-node commit (adapter missing / unclassified)."""
        new_state = self._with_run_status(state, RUN_BLOCKED, detail)
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="loop_stopped",
            current_node=node_id,
        )
        if committed is None:
            return None
        return StepOutcome(True, "blocked", node_id, committed[1], detail)

    def _recover_verified_result(
        self,
        reservation: Reservation,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node: GraphNode,
        node_id: str,
        node_state: dict[str, Any],
        input_digest: str,
        input_refs: list[str],
        attempt: int,
    ) -> StepOutcome | None:
        """G1-14/G1-15: commit only a VERIFIED stored result.

        Order is the recoverable atomic protocol: (1) fetch and fully verify
        the evidence (binding identity, digests, output schema); (2) CAS the
        checkpoint append; (3) only then finalize the reservation
        (idempotent). A CAS failure at (2) leaves the reservation untouched,
        so the next recovery retries the same evidence commit — zero new
        handler entries, zero transport sends. Without verified evidence the
        hold stands for a human."""
        execution_id = reservation.execution_id
        if execution_id is None:
            return None
        binding = self._reservations.binding_of(execution_id)
        if binding is None:
            return None
        output = self._verified_evidence_output(
            execution_id, binding, node, reservation.reason
        )
        if output is None:
            return None
        committed = self._commit_recovered_output(
            spec,
            checkpoint,
            state,
            sequence,
            node,
            node_id,
            node_state,
            input_digest,
            input_refs,
            attempt,
            output,
        )
        if committed is None:
            return None  # CAS conflict: reservation untouched, retry later
        self._reservations.complete_from_verified_receipt(execution_id)
        return committed

    def _verified_evidence_output(
        self,
        execution_id: str,
        binding: dict[str, Any],
        node: GraphNode,
        reason: str,
    ) -> dict[str, Any] | None:
        """Fully verified recovery output or None (fail closed).

        Agent ledger branch (G1-14): completed row, result_json parses,
        stored result_digest recomputes, node output schema passes. Receipt
        branch (G1-15): the frozen execution-receipt-v1 envelope — exact key
        set, stable execution_id, run/node/spec/input/classification binding
        fields equal to the reservation row, recomputed result_digest, node
        output schema. Any mismatch returns None: zero checkpoint commit."""
        session_id = binding.get("agent_session_id")
        if session_id:
            ledger_row = AgentLedgerStore(self.store.path).get(str(session_id))
            if (
                ledger_row is not None
                and str(ledger_row["status"]) == STATUS_COMPLETED
                and isinstance(ledger_row.get("result_json"), str)
            ):
                candidate = json.loads(str(ledger_row["result_json"]))
                stored_digest = ledger_row.get("result_digest")
                if (
                    isinstance(candidate, dict)
                    and stored_digest is not None
                    and digest_of(candidate) == str(stored_digest)
                    and not (
                        node.output_schema
                        and schema_mismatch(node.output_schema, candidate)
                    )
                ):
                    return dict(candidate)
        if reason == "reconcile_required" and self._receipt_provider is not None:
            receipt = self._receipt_provider(execution_id)
            if not isinstance(receipt, Mapping):
                return None
            if set(receipt) != EXECUTION_RECEIPT_KEYS:
                return None
            if receipt.get("schema") != EXECUTION_RECEIPT_SCHEMA:
                return None
            if receipt.get("execution_id") != execution_id:
                return None
            for field_name in (
                "run_id",
                "node_id",
                "spec_digest",
                "input_digest",
                "classification_digest",
            ):
                if receipt.get(field_name) != binding.get(field_name):
                    return None
            candidate = receipt.get("output")
            if not isinstance(candidate, dict):
                return None
            if receipt.get("result_digest") != eval_results_digest(candidate):
                return None
            if node.output_schema and schema_mismatch(node.output_schema, candidate):
                return None
            return dict(candidate)
        return None

    def _commit_recovered_output(
        self,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node: GraphNode,
        node_id: str,
        node_state: dict[str, Any],
        input_digest: str,
        input_refs: list[str],
        attempt: int,
        output: dict[str, Any],
    ) -> StepOutcome | None:
        """Checkpoint commit of a verified recovered output. The output was
        schema-validated when first produced under the SAME spec/input
        digest binding, so recovery only appends the checkpoint."""
        output_digest = digest_of(output)
        node_state.update(
            {
                "status": NODE_SUCCEEDED,
                "attempts": attempt,
                "force_rerun": False,
                "input_digest": input_digest,
                "input_refs": input_refs,
                "output": output,
                "output_digest": output_digest,
                "output_refs": [],
                "error": None,
                "completed_at": utc_now(),
                "last_succeeded": {
                    "input_digest": input_digest,
                    "output": output,
                    "output_digest": output_digest,
                    "output_refs": [],
                },
            }
        )
        new_state = self._with_node(state, node_id, node_state)
        new_state = self._propagate(spec, new_state, node_id, output)
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="node_completed",
            current_node=node_id,
        )
        if committed is None:
            return None
        return StepOutcome(True, "node_succeeded", node_id, committed[1])

    # -- failure, feedback --------------------------------------------------

    def _handle_failure(
        self,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node: GraphNode,
        node_state: dict[str, Any],
        input_digest: str,
        input_refs: list[str],
        attempt: int,
        result: NodeResult,
    ) -> StepOutcome | None:
        node_state.update(
            {
                "attempts": attempt,
                "force_rerun": False,
                "input_digest": input_digest,
                "input_refs": input_refs,
                "error": {"code": result.failure, "message": result.failure},
                "completed_at": utc_now(),
            }
        )
        context = {**result.output, "error": result.failure}
        feedback_edges = sorted(
            (edge for edge in spec.out_edges(node.id) if edge.type == "feedback"),
            key=lambda edge: edge.id,
        )
        counts = dict(state["feedback_counts"])
        for edge in feedback_edges:
            if not evaluate_condition(edge.condition or {}, context):
                continue
            used = int(counts.get(edge.id, 0))
            assert edge.max_traversals is not None
            if used < edge.max_traversals:
                counts[edge.id] = used + 1
                new_state = self._traverse_feedback(spec, state, node, node_state, edge, used + 1)
                new_state["feedback_counts"] = counts
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="feedback_traversed",
                    current_node=node.id,
                    revision_reason=edge.id,
                )
                if committed is None:
                    return None
                return StepOutcome(
                    True,
                    "feedback_traversed",
                    node.id,
                    committed[1],
                    {"edge_id": edge.id, "traversal": used + 1},
                )
            # Exhausted: fail | pause | route — never a silent retry.
            return self._feedback_exhausted(
                spec,
                checkpoint,
                state,
                sequence,
                node,
                node_state,
                edge,
                counts,
            )
        node_state["status"] = NODE_FAILED
        new_state = self._with_node(state, node.id, node_state)
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="node_failed",
            current_node=node.id,
            revision_reason=result.failure,
        )
        if committed is None:
            return None
        return StepOutcome(True, "node_failed", node.id, committed[1])

    def _traverse_feedback(
        self,
        spec: GraphSpec,
        state: dict[str, Any],
        node: GraphNode,
        node_state: dict[str, Any],
        edge: GraphEdge,
        traversal: int,
    ) -> dict[str, Any]:
        """Bounded loop back: target force-reruns; the span between target and
        source re-evaluates, short-circuiting on unchanged input digests."""
        target = edge.to_node
        downstream = set(spec.downstream_closure(target))
        upstream_of_source = self._upstream_closure(spec, node.id)
        span = downstream & upstream_of_source
        new_state = self._with_node(state, node.id, {**node_state, "status": NODE_PENDING})
        # v2 调度事实：span 与 source 的 pending 重置随本边界持久化（投影
        # 只重置 status/error，保留 attempts/output/last_succeeded）。
        resets: dict[str, str] = {node.id: NODE_PENDING}
        target_state = dict(new_state["nodes"][target])
        target_state.update({"status": NODE_READY, "force_rerun": True, "error": None})
        new_state = self._with_node(new_state, target, target_state)
        for member in sorted(span):
            if member in (target, node.id):
                continue
            member_state = dict(new_state["nodes"][member])
            if member_state["status"] in NODE_TERMINAL or member_state["status"] in (
                NODE_WAITING_HUMAN,
            ):
                member_state["status"] = NODE_PENDING
                member_state["error"] = None
                new_state = self._with_node(new_state, member, member_state)
                resets[member] = NODE_PENDING
            gate = new_state["human_gates"].get(member)
            if gate and gate["status"] == GATE_RESOLVED:
                new_state["human_gates"] = {
                    **new_state["human_gates"],
                    member: {**gate, "status": GATE_EXPIRED},
                }
        new_state["edges_taken"] = [
            *new_state["edges_taken"],
            {
                "edge_id": edge.id,
                "from": edge.from_node,
                "to": edge.to_node,
                "type": edge.type,
                "decision_source": edge.decision_source,
                "reason": f"feedback_traversal_{traversal}_of_{edge.max_traversals}",
            },
        ]
        new_state["_edge_deltas"] = [
            *new_state.get("_edge_deltas", []),
            *new_state["edges_taken"][-1:],
        ]
        new_state["_status_resets"] = {
            **new_state.get("_status_resets", {}),
            **resets,
        }
        return new_state

    def _feedback_exhausted(
        self,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node: GraphNode,
        node_state: dict[str, Any],
        edge: GraphEdge,
        counts: dict[str, int],
    ) -> StepOutcome | None:
        policy = edge.on_exhausted
        new_state = self._with_node(state, node.id, node_state)
        new_state["feedback_counts"] = counts
        if policy == "pause":
            new_state = self._with_node(new_state, node.id, {**node_state, "status": NODE_FAILED})
            new_state = self._with_run_status(
                new_state,
                RUN_PAUSED,
                {
                    "code": "feedback_exhausted",
                    "node_id": node.id,
                    "message": f"edge {edge.id} exhausted; run paused for a human",
                },
            )
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="loop_stopped",
                current_node=node.id,
                revision_reason="feedback_exhausted_pause",
            )
            if committed is None:
                return None
            return StepOutcome(True, "paused", node.id, committed[1])
        if policy == "route" and edge.exhausted_to is not None:
            new_state = self._with_node(new_state, node.id, {**node_state, "status": NODE_FAILED})
            target_state = dict(new_state["nodes"][edge.exhausted_to])
            target_state["status"] = NODE_READY
            new_state = self._with_node(new_state, edge.exhausted_to, target_state)
            new_state["edges_taken"] = [
                *new_state["edges_taken"],
                {
                    "edge_id": edge.id,
                    "from": edge.from_node,
                    "to": edge.exhausted_to,
                    "type": edge.type,
                    "decision_source": edge.decision_source,
                    "reason": "feedback_exhausted_route",
                },
            ]
            new_state["_edge_deltas"] = [
                *new_state.get("_edge_deltas", []),
                *new_state["edges_taken"][-1:],
            ]
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="node_failed",
                current_node=node.id,
                revision_reason="feedback_exhausted_route",
            )
            if committed is None:
                return None
            return StepOutcome(True, "node_failed", node.id, committed[1])
        new_state = self._with_node(new_state, node.id, {**node_state, "status": NODE_FAILED})
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="node_failed",
            current_node=node.id,
            revision_reason="feedback_exhausted_fail",
        )
        if committed is None:
            return None
        return StepOutcome(True, "node_failed", node.id, committed[1])

    # -- condition edges, propagation --------------------------------------

    def _select_condition_edge(
        self,
        spec: GraphSpec,
        state: dict[str, Any],
        node: GraphNode,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Pick the first matching condition edge by ascending priority.

        Returns ``(state, None)`` on a selection, or ``(state, detail)`` when
        no edge matched — a safe block with the rules and inputs recorded.
        """
        edges = sorted(
            (edge for edge in spec.out_edges(node.id) if edge.type == "condition"),
            key=lambda edge: (edge.priority if edge.priority is not None else 0, edge.id),
        )
        rules = [
            {"edge_id": edge.id, "condition": plain_copy(edge.condition), "priority": edge.priority}
            for edge in edges
        ]
        selected: GraphEdge | None = None
        for edge in edges:
            if evaluate_condition(edge.condition or {}, context):
                selected = edge
                break
        if selected is None:
            return state, {
                "code": "no_matching_edge",
                "node_id": node.id,
                "message": "no condition edge matched the router output",
                "rules": rules,
                "input_digest": digest_of(context),
            }
        return self._apply_edge_choice(spec, state, selected, edges, context), None

    def _apply_edge_choice(
        self,
        spec: GraphSpec,
        state: dict[str, Any],
        selected: GraphEdge,
        candidates: list[GraphEdge],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        new_state = dict(state)
        new_state["edges_taken"] = [
            *new_state["edges_taken"],
            {
                "edge_id": selected.id,
                "from": selected.from_node,
                "to": selected.to_node,
                "type": selected.type,
                "decision_source": selected.decision_source,
                "reason": f"condition_matched:{canonical_condition(selected)}",
            },
        ]
        new_state["_edge_deltas"] = [
            *new_state.get("_edge_deltas", []),
            *new_state["edges_taken"][-1:],
        ]
        new_state = self._mark_ready(spec, new_state, selected.to_node)
        for edge in candidates:
            if edge.id == selected.id:
                continue
            new_state = self._skip_branch(spec, new_state, edge.to_node)
        return new_state

    def _propagate(
        self,
        spec: GraphSpec,
        state: dict[str, Any],
        node_id: str,
        output: dict[str, Any] | None,
    ) -> dict[str, Any]:
        new_state = dict(state)
        condition_edges = [edge for edge in spec.out_edges(node_id) if edge.type == "condition"]
        if condition_edges:
            context = dict(output or {})
            gate = new_state["human_gates"].get(node_id)
            if gate and gate.get("decision") is not None:
                context["decision"] = gate["decision"]
            edges = sorted(
                condition_edges,
                key=lambda edge: (edge.priority if edge.priority is not None else 0, edge.id),
            )
            selected: GraphEdge | None = None
            for edge in edges:
                if evaluate_condition(edge.condition or {}, context):
                    selected = edge
                    break
            if selected is None:
                return self._with_run_status(
                    new_state,
                    RUN_BLOCKED,
                    {
                        "code": "no_matching_edge",
                        "node_id": node_id,
                        "message": "no condition edge matched the node output",
                        "rules": [
                            {"edge_id": edge.id, "condition": plain_copy(edge.condition)}
                            for edge in edges
                        ],
                        "input_digest": digest_of(context),
                    },
                )
            return self._apply_edge_choice(spec, new_state, selected, edges, context)
        for edge in spec.out_edges(node_id):
            if edge.type != "sequence":
                continue
            new_state["edges_taken"] = [
                *new_state["edges_taken"],
                {
                    "edge_id": edge.id,
                    "from": edge.from_node,
                    "to": edge.to_node,
                    "type": edge.type,
                    "decision_source": edge.decision_source,
                    "reason": "sequence",
                },
            ]
            new_state["_edge_deltas"] = [
                *new_state.get("_edge_deltas", []),
                *new_state["edges_taken"][-1:],
            ]
            new_state = self._mark_ready(spec, new_state, edge.to_node)
        return new_state

    def _mark_ready(self, spec: GraphSpec, state: dict[str, Any], node_id: str) -> dict[str, Any]:
        node_state = dict(state["nodes"][node_id])
        if node_state["status"] != NODE_PENDING:
            return state
        inbound = [
            edge for edge in spec.in_edges(node_id) if edge.type in ("sequence", "condition")
        ]
        sources = [state["nodes"][edge.from_node]["status"] for edge in inbound]
        if sources and all(status == NODE_SKIPPED for status in sources):
            node_state["status"] = NODE_SKIPPED
            new_state = self._with_node(state, node_id, node_state)
            for edge in spec.out_edges(node_id):
                if edge.type == "feedback":
                    continue
                new_state = self._mark_ready(spec, new_state, edge.to_node)
            return new_state
        node = spec.node_map[node_id]
        if node.kind == "join":
            assert node.join is not None
            if sources and all(status in NODE_TERMINAL for status in sources):
                # Both modes fire once every in-source is terminal; the
                # frozen partial-failure policy then decides the outcome.
                node_state["status"] = NODE_READY
                return self._with_node(state, node_id, node_state)
            if node.join["mode"] == "minimum_success" and node.join["cancel_remaining"]:
                # Threshold already met: cancel the remaining not-yet-started
                # direct in-sources in the same checkpoint and fire now.
                # Sources already running a child or in any other state are
                # never touched — the join simply keeps waiting.
                succeeded = sum(1 for status in sources if status == NODE_SUCCEEDED)
                if succeeded >= int(node.join["threshold"]):
                    remaining = [
                        edge.from_node
                        for edge in inbound
                        if state["nodes"][edge.from_node]["status"] not in NODE_TERMINAL
                    ]
                    if remaining and all(
                        state["nodes"][source]["status"] in (NODE_PENDING, NODE_READY)
                        and not state["nodes"][source].get("child_run_id")
                        for source in remaining
                    ):
                        new_state = self._cancel_remaining(spec, state, node_id)
                        join_state = dict(new_state["nodes"][node_id])
                        join_state["status"] = NODE_READY
                        return self._with_node(new_state, node_id, join_state)
            return state
        if all(status in (NODE_SUCCEEDED, NODE_SKIPPED) for status in sources):
            node_state["status"] = NODE_READY
            return self._with_node(state, node_id, node_state)
        return state

    def _skip_branch(self, spec: GraphSpec, state: dict[str, Any], node_id: str) -> dict[str, Any]:
        node_state = dict(state["nodes"][node_id])
        if node_state["status"] != NODE_PENDING:
            return state
        inbound = [
            edge for edge in spec.in_edges(node_id) if edge.type in ("sequence", "condition")
        ]
        sources = [state["nodes"][edge.from_node]["status"] for edge in inbound]
        if not all(status in (NODE_SKIPPED, NODE_CANCELLED, NODE_SUCCEEDED) for status in sources):
            return state
        node_state["status"] = NODE_SKIPPED
        new_state = self._with_node(state, node_id, node_state)
        for edge in spec.out_edges(node_id):
            if edge.type == "feedback":
                continue
            target = dict(new_state["nodes"][edge.to_node])
            if target["status"] != NODE_PENDING:
                continue
            target_inbound = [
                item
                for item in spec.in_edges(edge.to_node)
                if item.type in ("sequence", "condition")
            ]
            target_sources = [
                new_state["nodes"][item.from_node]["status"] for item in target_inbound
            ]
            if target_sources and all(status == NODE_SKIPPED for status in target_sources):
                new_state = self._skip_branch(spec, new_state, edge.to_node)
            elif all(status in (NODE_SUCCEEDED, NODE_SKIPPED) for status in target_sources):
                new_state = self._mark_ready(spec, new_state, edge.to_node)
        return new_state

    # -- join ---------------------------------------------------------------

    def _evaluate_join(
        self,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node: GraphNode,
    ) -> StepOutcome | None:
        assert node.join is not None
        inbound = [
            edge for edge in spec.in_edges(node.id) if edge.type in ("sequence", "condition")
        ]
        sources = {edge.from_node: state["nodes"][edge.from_node] for edge in inbound}
        if not all(source["status"] in NODE_TERMINAL for source in sources.values()):
            # Not settled yet: keep pending; another branch must advance first.
            new_state = self._with_run_status(
                state,
                RUN_BLOCKED,
                {
                    "code": "join_unsettled",
                    "node_id": node.id,
                    "message": "join became ready with unsettled sources",
                },
            )
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="loop_stopped",
                current_node=node.id,
            )
            if committed is None:
                return None
            return StepOutcome(True, "blocked", node.id, committed[1])
        succeeded = [
            source_id for source_id, source in sources.items() if source["status"] == NODE_SUCCEEDED
        ]
        skipped = [
            source_id for source_id, source in sources.items() if source["status"] == NODE_SKIPPED
        ]
        failed = [
            source_id
            for source_id, source in sources.items()
            if source["status"] in (NODE_FAILED, NODE_CANCELLED)
        ]
        mode = node.join["mode"]
        if mode == "all_success":
            satisfied = not failed
        else:
            satisfied = len(succeeded) >= int(node.join["threshold"])
        if not satisfied and mode == "minimum_success":
            # Every source is already terminal, so the threshold is a hard
            # contract no partial-failure policy may waive — "continue"
            # included. "pause" parks the run for a human; every other
            # policy fails the node fail-closed.
            detail = {
                "code": "join_threshold_unmet",
                "node_id": node.id,
                "message": (
                    f"join threshold unmet: {len(succeeded)} succeeded, "
                    f"need {node.join['threshold']}"
                ),
            }
            if node.join["on_partial_failure"] == "pause":
                node_state = dict(state["nodes"][node.id])
                node_state["status"] = NODE_PENDING
                new_state = self._with_node(state, node.id, node_state)
                new_state = self._with_run_status(new_state, RUN_PAUSED, detail)
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="loop_stopped",
                    current_node=node.id,
                )
                if committed is None:
                    return None
                return StepOutcome(True, "paused", node.id, committed[1])
            node_state = dict(state["nodes"][node.id])
            node_state.update(
                {
                    "status": NODE_FAILED,
                    "error": {"code": detail["code"], "message": detail["message"]},
                    "completed_at": utc_now(),
                }
            )
            new_state = self._with_node(state, node.id, node_state)
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="node_failed",
                current_node=node.id,
            )
            if committed is None:
                return None
            return StepOutcome(True, "node_failed", node.id, committed[1])
        if failed and not satisfied:
            # all_success keeps the frozen partial-failure policy.
            policy = node.join["on_partial_failure"]
            if policy == "pause":
                node_state = dict(state["nodes"][node.id])
                node_state["status"] = NODE_PENDING
                new_state = self._with_node(state, node.id, node_state)
                new_state = self._with_run_status(
                    new_state,
                    RUN_PAUSED,
                    {
                        "code": "join_partial_failure",
                        "node_id": node.id,
                        "message": f"join paused on failed sources: {failed}",
                    },
                )
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="loop_stopped",
                    current_node=node.id,
                )
                if committed is None:
                    return None
                return StepOutcome(True, "paused", node.id, committed[1])
            if policy == "continue":
                satisfied = True
            else:  # fail
                node_state = dict(state["nodes"][node.id])
                node_state.update(
                    {
                        "status": NODE_FAILED,
                        "error": {
                            "code": "join_partial_failure",
                            "message": f"failed sources: {failed}",
                        },
                        "completed_at": utc_now(),
                    }
                )
                new_state = self._with_node(state, node.id, node_state)
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="node_failed",
                    current_node=node.id,
                )
                if committed is None:
                    return None
                return StepOutcome(True, "node_failed", node.id, committed[1])
        node_state = dict(state["nodes"][node.id])
        inputs, input_refs, input_digest = self._inputs_for(spec, state, node.id)
        node_state.update(
            {
                "status": NODE_SUCCEEDED,
                "attempts": int(node_state["attempts"]) + 1,
                "input_digest": input_digest,
                "input_refs": input_refs,
                "output": {"joined": succeeded, "skipped": skipped},
                "output_digest": digest_of({"joined": succeeded, "skipped": skipped}),
                "output_refs": input_refs,
                "absorbed_failures": failed
                if failed
                and (
                    node.join["on_partial_failure"] == "continue"
                    or node.join["mode"] == "minimum_success"
                )
                else [],
                "error": None,
                "completed_at": utc_now(),
                "last_succeeded": {
                    "input_digest": input_digest,
                    "output": {"joined": succeeded, "skipped": skipped},
                    "output_digest": digest_of({"joined": succeeded, "skipped": skipped}),
                    "output_refs": input_refs,
                },
            }
        )
        new_state = self._with_node(state, node.id, node_state)
        new_state = self._propagate(spec, new_state, node.id, node_state["output"])
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="node_completed",
            current_node=node.id,
        )
        if committed is None:
            return None
        return StepOutcome(True, "node_succeeded", node.id, committed[1])

    def _cancel_remaining(
        self, spec: GraphSpec, state: dict[str, Any], join_id: str
    ) -> dict[str, Any]:
        """Cancel only this join's own not-yet-started direct in-sources.

        Never walks the whole graph: only the join's sequence/condition
        in-edges are considered, and only pending/ready sources without an
        active child run are cancelled.
        """
        new_state = dict(state)
        for edge in spec.in_edges(join_id):
            if edge.type not in ("sequence", "condition"):
                continue
            node_state = dict(new_state["nodes"][edge.from_node])
            if (
                node_state["status"] in (NODE_PENDING, NODE_READY)
                and not node_state.get("child_run_id")
            ):
                node_state["status"] = NODE_CANCELLED
                new_state = self._with_node(new_state, edge.from_node, node_state)
        return new_state

    # -- human gate ---------------------------------------------------------

    def _enter_human_gate(
        self,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node: GraphNode,
        input_digest: str,
        input_refs: list[str],
    ) -> StepOutcome | None:
        node_state = dict(state["nodes"][node.id])
        node_state.update(
            {
                "status": NODE_WAITING_HUMAN,
                "attempts": int(node_state["attempts"]) + 1,
                "input_digest": input_digest,
                "input_refs": input_refs,
            }
        )
        new_state = self._with_node(state, node.id, node_state)
        new_state["human_gates"] = {
            **new_state["human_gates"],
            node.id: {
                "status": GATE_PENDING,
                "decision": None,
                "decision_id": None,
                "input_digest": input_digest,
                "spec_digest": str(state["spec_digest"]),
                "expected_sequence": sequence + 1,
                "requester": "nigo",
                "decided_at": None,
                "allowed_decisions": list((node.human_gate or {})["allowed_decisions"]),
            },
        }
        new_state = self._with_run_status(new_state, RUN_HUMAN_WAIT, None)
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="human_gate_requested",
            current_node=node.id,
        )
        if committed is None:
            return None
        return StepOutcome(True, "human_gate_requested", node.id, committed[1])

    def apply_human_decision(
        self,
        run_id: str,
        node_id: str,
        *,
        decision: str,
        spec_digest: str,
        input_digest: str,
        expected_sequence: int,
        requester: str,
        decision_id: str,
    ) -> tuple[LoopCheckpoint, bool]:
        """Apply a human decision; any binding drift fails with zero writes."""
        checkpoint, state, sequence = self._load(run_id)
        spec = self._spec_for(state)
        gate = state["human_gates"].get(node_id)
        node = spec.node_map.get(node_id)
        if node is None or node.kind != "human_decision" or gate is None:
            raise GraphDecisionError("unknown_gate")
        if requester != "nigo":
            raise GraphDecisionError("invalid_requester")
        allowed = list((node.human_gate or {})["allowed_decisions"])
        if decision not in allowed:
            raise GraphDecisionError("invalid_decision")
        if spec_digest != state["spec_digest"]:
            raise GraphDecisionError("spec_digest_mismatch")
        if gate["status"] == GATE_RESOLVED:
            if gate["decision_id"] == decision_id and gate["decision"] == decision:
                return checkpoint_compat_view(checkpoint), False  # idempotent replay
            raise GraphDecisionError("gate_already_resolved")
        if gate["status"] != GATE_PENDING:
            raise GraphDecisionError("gate_not_pending")
        if gate["input_digest"] != input_digest:
            raise GraphDecisionError("input_digest_mismatch")
        if sequence != expected_sequence or gate["expected_sequence"] != expected_sequence:
            raise GraphDecisionError("sequence_mismatch")
        expected_id = human_decision_id(
            requester=requester,
            decision=decision,
            run_id=run_id,
            node_id=node_id,
            spec_digest=str(state["spec_digest"]),
            input_digest=input_digest,
            expected_sequence=expected_sequence,
        )
        if decision_id != expected_id:
            raise GraphDecisionError("decision_id_mismatch")

        output = {"decision": decision}
        node_state = dict(state["nodes"][node_id])
        node_state.update(
            {
                "status": NODE_SUCCEEDED,
                "output": output,
                "output_digest": digest_of(output),
                "output_refs": [],
                "error": None,
                "completed_at": utc_now(),
                "last_succeeded": {
                    "input_digest": input_digest,
                    "output": output,
                    "output_digest": digest_of(output),
                    "output_refs": [],
                },
            }
        )
        new_state = self._with_node(state, node_id, node_state)
        new_state["human_gates"] = {
            **new_state["human_gates"],
            node_id: {
                **gate,
                "status": GATE_RESOLVED,
                "decision": decision,
                "decision_id": decision_id,
                "decided_at": utc_now(),
            },
        }
        new_state = self._with_run_status(new_state, RUN_RUNNING, None)
        new_state = self._propagate(spec, new_state, node_id, output)
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="human_decision_applied",
            current_node=node_id,
            revision_reason=decision,
        )
        if committed is None:
            raise GraphDecisionError("sequence_mismatch")
        return committed[0], True

    # -- reopen / invalidation ----------------------------------------------

    def reopen_node(
        self,
        run_id: str,
        node_id: str,
        *,
        expected_sequence: int,
        requester: str,
        reason: str,
        reopen_id: str,
    ) -> LoopCheckpoint:
        """Reopen one node: the node force-reruns; downstream re-evaluates and
        short-circuits on unchanged input digests; resolved human gates on the
        affected span expire immediately."""
        checkpoint, state, sequence = self._load(run_id)
        spec = self._spec_for(state)
        if requester != "nigo":
            raise GraphDecisionError("invalid_requester")
        if node_id not in spec.node_map:
            raise GraphDecisionError("unknown_node")
        if sequence != expected_sequence:
            raise GraphDecisionError("sequence_mismatch")
        expected_id = reopen_request_id(
            requester=requester,
            run_id=run_id,
            node_id=node_id,
            expected_sequence=expected_sequence,
            reason=reason,
        )
        if reopen_id != expected_id:
            raise GraphDecisionError("reopen_id_mismatch")
        if str(state["run_status"]) in (RUN_COMPLETED, RUN_FAILED):
            raise GraphDecisionError("run_terminal")
        node_status = str(state["nodes"][node_id]["status"])
        if node_status not in (NODE_SUCCEEDED, NODE_FAILED):
            # Only a terminal node may be reopened; pending/ready/skipped/
            # cancelled/waiting_human nodes are refused with zero writes.
            raise GraphDecisionError("node_not_reopenable")

        affected = [node_id, *spec.downstream_closure(node_id)]
        new_state = dict(state)
        resets: dict[str, str] = {}
        for member in affected:
            member_state = dict(new_state["nodes"][member])
            if member_state["status"] in (NODE_SUCCEEDED, NODE_FAILED, NODE_WAITING_HUMAN):
                member_state["status"] = NODE_READY if member == node_id else NODE_PENDING
                if member == node_id:
                    member_state["force_rerun"] = True
                else:
                    resets[member] = NODE_PENDING  # v2 调度事实：pending 重置
                member_state["error"] = None
                new_state = self._with_node(new_state, member, member_state)
            gate = new_state["human_gates"].get(member)
            if gate and gate["status"] in (GATE_RESOLVED, GATE_PENDING):
                new_state["human_gates"] = {
                    **new_state["human_gates"],
                    member: {**gate, "status": GATE_EXPIRED},
                }
        new_state = self._with_run_status(new_state, RUN_RUNNING, None)
        new_state["blocked_reason"] = None
        if resets:
            new_state["_status_resets"] = resets
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="node_reopened",
            current_node=node_id,
            revision_reason=reason,
        )
        if committed is None:
            raise GraphDecisionError("sequence_mismatch")
        return committed[0]

    # -- subgraph -----------------------------------------------------------

    def _step_subgraph(
        self,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
        node: GraphNode,
        inputs: dict[str, Any],
        input_refs: list[str],
        input_digest: str,
        parked: tuple[str, dict[str, Any]] | None = None,
    ) -> StepOutcome | None:
        assert node.subgraph_ref is not None
        node_state = dict(state["nodes"][node.id])
        child_id = node_state.get("child_run_id")
        if not child_id:
            child_spec = self.specs.get(node.subgraph_ref["graph_id"])
            if child_spec is None or child_spec.digest != node.subgraph_ref["digest"]:
                new_state = self._with_run_status(
                    state,
                    RUN_BLOCKED,
                    {
                        "code": "subgraph_unavailable",
                        "node_id": node.id,
                        "message": "referenced subgraph spec is missing or digest drifted",
                    },
                )
                committed = self._commit(
                    checkpoint,
                    new_state,
                    sequence,
                    event_type="loop_stopped",
                    current_node=node.id,
                )
                if committed is None:
                    return None
                return StepOutcome(True, "blocked", node.id, committed[1])
            child_id = f"{checkpoint.run_id}:sub:{node.id}"
            self.register_run(
                child_spec,
                child_id,
                fragment_ref=checkpoint.fragment_id,
                run_inputs={
                    "parent_run_id": checkpoint.run_id,
                    "parent_node_id": node.id,
                    "inputs": inputs,
                },
            )
            node_state["child_run_id"] = child_id
            node_state["input_digest"] = input_digest
            node_state["input_refs"] = input_refs
            new_state = self._with_node(state, node.id, node_state)
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="subgraph_registered",
                current_node=node.id,
                revision_reason=child_id,
            )
            if committed is None:
                return None
            return StepOutcome(
                True, "child_registered", node.id, committed[1], {"child_run_id": child_id}
            )

        child_status = self._child_status(child_id)
        if child_status == RUN_RUNNING:
            child_outcome = self.step(child_id)  # one child boundary per parent step
            # Only the child moved; the parent checkpoint is not written, so
            # the parent reports its own unchanged sequence.
            return StepOutcome(
                False,
                "child_stepped",
                node.id,
                sequence,
                {"child_run_id": child_id, "child_event": child_outcome.event},
            )
        if child_status == RUN_COMPLETED:
            if self._storage_v2 is not None:
                # v2 child：状态由 v2 表重建（stub 行无 v1 payload）。
                _, child_state, _ = self._load(child_id)
            else:
                child_checkpoint = self.store.latest(child_id)
                child_state = (
                    child_checkpoint.eval_results.get("graph_state", {})
                    if child_checkpoint is not None
                    else {}
                )
            outputs: dict[str, Any] = {}
            child_refs: list[str] = []
            for child_node_id, child_node in child_state.get("nodes", {}).items():
                if child_node.get("status") == NODE_SUCCEEDED:
                    outputs[child_node_id] = child_node.get("output")
                    for ref in child_node.get("output_refs", []):
                        if ref not in child_refs:
                            child_refs.append(ref)
            node_state.update(
                {
                    "status": NODE_SUCCEEDED,
                    "attempts": int(node_state["attempts"]) + 1,
                    "output": {"subgraph": node.subgraph_ref["graph_id"], "outputs": outputs},
                    "output_digest": digest_of(outputs),
                    "output_refs": child_refs,
                    "error": None,
                    "completed_at": utc_now(),
                    "last_succeeded": {
                        "input_digest": input_digest,
                        "output": {
                            "subgraph": node.subgraph_ref["graph_id"],
                            "outputs": outputs,
                        },
                        "output_digest": digest_of(outputs),
                        "output_refs": child_refs,
                    },
                }
            )
            new_state = self._with_node(state, node.id, node_state)
            new_state = self._propagate(spec, new_state, node.id, node_state["output"])
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="node_completed",
                current_node=node.id,
            )
            if committed is None:
                return None
            return StepOutcome(True, "node_succeeded", node.id, committed[1])
        if child_status == RUN_FAILED:
            node_state.update(
                {
                    "status": NODE_FAILED,
                    "error": {
                        "code": "subgraph_failed",
                        "message": f"child run {child_id} failed",
                    },
                    "completed_at": utc_now(),
                }
            )
            new_state = self._with_node(state, node.id, node_state)
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="node_failed",
                current_node=node.id,
                revision_reason="subgraph_failed",
            )
            if committed is None:
                return None
            return StepOutcome(True, "node_failed", node.id, committed[1])
        # Child waits on a human / paused / blocked: the parent projects the
        # child status onto itself (human_wait -> human_wait, paused -> paused,
        # blocked -> blocked).
        parent_status, event = {
            RUN_HUMAN_WAIT: (RUN_HUMAN_WAIT, "human_gate_requested"),
            RUN_PAUSED: (RUN_PAUSED, "paused"),
            RUN_BLOCKED: (RUN_BLOCKED, "blocked"),
        }[child_status]
        if (
            parked is not None
            and parked[0] == parent_status
            and str(parked[1].get("code", "")) == f"child_{child_status}"
        ):
            # The child wait is unchanged since the parent parked on it: zero
            # writes, no duplicate checkpoint.
            return StepOutcome(
                False,
                f"noop_child_{child_status}",
                node.id,
                sequence,
                {"child_run_id": child_id},
            )
        new_state = self._with_run_status(
            state,
            parent_status,
            {
                "code": f"child_{child_status}",
                "node_id": node.id,
                "message": f"child run {child_id} is {child_status}",
            },
        )
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="loop_stopped",
            current_node=node.id,
        )
        if committed is None:
            return None
        return StepOutcome(True, event, node.id, committed[1])

    # -- settle -------------------------------------------------------------

    def _settle(
        self,
        spec: GraphSpec,
        checkpoint: LoopCheckpoint,
        state: dict[str, Any],
        sequence: int,
    ) -> StepOutcome:
        """No ready node: either finish, or explain exactly why not."""
        nodes = state["nodes"]
        statuses = {node_id: node["status"] for node_id, node in nodes.items()}
        waiting = [node_id for node_id, status in statuses.items() if status == NODE_WAITING_HUMAN]
        if waiting:
            new_state = self._with_run_status(state, RUN_HUMAN_WAIT, None)
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="human_gate_requested",
                current_node=checkpoint.current_node,
            )
            if committed is None:
                return self.step(checkpoint.run_id)
            return StepOutcome(True, "human_gate_requested", waiting[0], committed[1])
        unsettled = [node_id for node_id, status in statuses.items() if status not in NODE_TERMINAL]

        def absorbed_failures() -> set[str]:
            absorbed: set[str] = set()
            for _node_id, node in nodes.items():
                for item in node.get("absorbed_failures", []):
                    absorbed.add(item)
            return absorbed

        if not unsettled:
            failed = [node_id for node_id, status in statuses.items() if status == NODE_FAILED]
            absorbed = absorbed_failures()
            unabsorbed = [node_id for node_id in failed if node_id not in absorbed]
            final_status = RUN_COMPLETED if not unabsorbed else RUN_FAILED
            new_state = self._with_run_status(
                state,
                final_status,
                None
                if final_status == RUN_COMPLETED
                else {
                    "code": "unabsorbed_failure",
                    "node_id": unabsorbed[0],
                    "message": f"unabsorbed failed nodes: {unabsorbed}",
                },
            )
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="loop_stopped",
                current_node=checkpoint.current_node,
            )
            if committed is None:
                return self.step(checkpoint.run_id)
            return StepOutcome(
                True,
                "completed" if final_status == RUN_COMPLETED else "failed",
                sequence=committed[1],
            )
        # Pending nodes whose sources are all terminal but no edge fired:
        # safe stop with the exact reason, never a silent stall. An
        # unabsorbed failed node settles the run as failed.
        pending = [node_id for node_id in unsettled if statuses[node_id] == NODE_PENDING]
        failed_nodes = [node_id for node_id, status in statuses.items() if status == NODE_FAILED]
        absorbed_set = absorbed_failures()
        unabsorbed = [node_id for node_id in failed_nodes if node_id not in absorbed_set]
        if unabsorbed:
            detail = {
                "code": "unabsorbed_failure",
                "node_id": unabsorbed[0],
                "message": f"unabsorbed failed nodes: {unabsorbed}",
            }
            new_state = self._with_run_status(state, RUN_FAILED, detail)
            committed = self._commit(
                checkpoint,
                new_state,
                sequence,
                event_type="loop_stopped",
                current_node=checkpoint.current_node,
            )
            if committed is None:
                return self.step(checkpoint.run_id)
            return StepOutcome(True, "failed", sequence=committed[1], detail=detail)
        detail = {
            "code": "no_progress",
            "node_id": pending[0] if pending else unsettled[0],
            "message": f"unsettled nodes cannot advance: {unsettled}",
        }
        new_state = self._with_run_status(state, RUN_BLOCKED, detail)
        committed = self._commit(
            checkpoint,
            new_state,
            sequence,
            event_type="loop_stopped",
            current_node=checkpoint.current_node,
        )
        if committed is None:
            return self.step(checkpoint.run_id)
        return StepOutcome(True, "blocked", sequence=committed[1], detail=detail)

    # -- small state helpers --------------------------------------------------

    @staticmethod
    def _with_node(
        state: dict[str, Any], node_id: str, node_state: dict[str, Any]
    ) -> dict[str, Any]:
        new_state = dict(state)
        new_state["nodes"] = {**state["nodes"], node_id: node_state}
        return new_state

    @staticmethod
    def _with_run_status(
        state: dict[str, Any], status: str, reason: dict[str, Any] | None
    ) -> dict[str, Any]:
        new_state = dict(state)
        new_state["run_status"] = status
        new_state["blocked_reason"] = reason
        return new_state

    @staticmethod
    def _upstream_closure(spec: GraphSpec, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for edge in spec.in_edges(current):
                if edge.from_node not in seen:
                    seen.add(edge.from_node)
                    stack.append(edge.from_node)
        return seen

    def _action_gate_block(
        self, spec: GraphSpec, state: Mapping[str, Any], node: GraphNode
    ) -> dict[str, Any] | None:
        assert node.action_policy is not None
        gate_id = node.action_policy["required_gate"]
        gate = state["human_gates"].get(gate_id)
        if gate is None or gate["status"] != GATE_RESOLVED:
            return {
                "code": "human_gate_not_resolved",
                "node_id": node.id,
                "message": f"action requires a resolved human gate: {gate_id}",
            }
        if node.action_policy["side_effect_level"] != "none":
            return {
                "code": "unsupported_side_effect_level",
                "node_id": node.id,
                "message": "phase 1 executes only side-effect-free synthetic actions",
            }
        return None


def canonical_condition(edge: GraphEdge) -> str:
    condition = edge.condition or {}
    return f"{condition.get('field')}{condition.get('op')}{condition.get('value')!r}"
