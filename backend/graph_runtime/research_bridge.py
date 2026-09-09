"""fragment-research-escalation-v1 的独立 Research Creation Bridge 与执行触发。

DESIGN §5.2/§5.3（rev5 冻结）：

- 端点 ``POST /graph/v1/research-runs``，固定写头 ``X-Graph-Research-Run-Create: 1``
  + 可信 Origin（cognitive_server 在桥之前校验）；请求体恰好七字段
  （spec_id/spec_digest/alignment_id/episode_id/evidence_bundle_digest/
  escalation_id/requester），未知字段拒绝。
- 确定性 run_id 五元绑定：``exec:graph-research:`` + sha256(
  "graph-research-escalation-v1" ␟ spec_digest ␟ alignment_id ␟ episode_id ␟
  evidence_bundle_digest ␟ escalation_id)[:24]。
- 创建前逐项归属核验：alignment∈episode、escalation∈alignment/episode 且未建过
  Run、evidence bundle 同谱系 TOCTOU（逐条复算 evidence_digest）、spec digest 在
  Research 专用 allowlist；任何漂移 4xx 零 Run。完全相同请求 200 幂等；同
  escalation 不同绑定 409 ``already_bridged``；并发 CAS 单赢。
- 安全前缀（§5.3）：节点/边/adapter 精确允许清单，漂移即 ``unsafe_prefix``。
- 执行触发语义：一次受治理创建操作完成 注册→缺口补采→授权检查；自动派生
  Receipt 成功（Checkpoint 原子落盘并返回 digest）→ 允许同一次受治理操作经
  ``authorization_auto_pass`` → ready join 执行一次 synthesis（模型发送 ≤1），
  响应如实返回 ``model_calls: 0|1``、``receipt_digest``、``status``；
  ``manual_required`` 停在授权闸门等人工签发；``blocked`` 经 block_pass →
  abort join 进入 ``aborted_output``。``--research-live=false`` 时采集失败关闭、
  Run 诚实 blocked、模型恒 0。
- 人工恢复走既有公开决定路径：``ResearchExecution.decide`` 只对 Research Run
  生效；``approve_synthesis`` 必须先经独立 Receipt 端点签发并携带精确 digest，
  决定本身绝不签发或替代 Receipt。其他 Run 原样委托注入的 fallback（pilot
  路径行为零变化）。

采集/Receipt/合成全部复用包 B 的 ``GovernedResearchRunner``；本模块不实现第二套。
不扩大 Pilot Bridge allowlist；历史 ``fragment-pilot-v1`` 行为零变化。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, cast

from common.checkpoint import SQLiteCheckpointStore
from common.execution import merge_runtime_eval_results, raw_eval_value
from fragment_loop.governed_research import (
    MAX_RESEARCH_ROUNDS,
    GovernedResearchError,
    GovernedResearchRunner,
    bounded_research_goal,
    build_synthesis_request,
    coverage_gaps,
    detect_conflict_types,
    evidence_digest,
)
from fragment_loop.intent_service import escalation_id
from graph_runtime.agent_ledger import AgentLedgerStore
from graph_runtime.runtime import (
    RUN_PAUSED,
    GraphRuntime,
    GraphRuntimeError,
    NodeRequest,
    NodeResult,
    reopen_request_id,
)
from graph_runtime.service import GraphService
from graph_runtime.spec import digest_of
from graph_runtime.specs.fragment_research_escalation_v1 import (
    ABORTED_OUTPUT,
    ADAPTER_AUTHORIZATION_CHECK,
    ADAPTER_BRANCH_MARKER,
    ADAPTER_COMMUNITY,
    ADAPTER_INPUT,
    ADAPTER_OFFICIAL,
    ADAPTER_PREPARATION,
    ADAPTER_SYNTHESIS,
    ADAPTER_VALIDATOR,
    AUTHORIZATION_CHECK_NODE,
    AUTHORIZATION_GATE,
    COMMUNITY_NODE,
    EVIDENCE_JOIN,
    INPUT_NODE,
    OFFICIAL_NODE,
    PREFIX_ADAPTERS,
    PREFIX_EDGES,
    PREFIX_NODES,
    PREPARATION_NODE,
    RESEARCH_GRAPH_ID,
    RESEARCH_SPEC,
    SPEC_DIGEST,
)
from graph_runtime.specs.fragment_research_macro_v3 import (
    ADAPTER_MACRO_AUTHZ_CHECK,
    ADAPTER_MACRO_COLLECT,
    ADAPTER_MACRO_COVERAGE,
    ADAPTER_MACRO_INPUT,
    ADAPTER_MACRO_JUDGE,
    ADAPTER_MACRO_PRODUCE,
    COVERAGE_NODE,
    RESEARCH_MACRO_V3_GRAPH_ID,
    RESEARCH_MACRO_V3_SPEC,
    RESEARCH_MACRO_V3_SPEC_DIGEST,
)

# 写资源固定头（cognitive_server 在桥之前已校验 Origin + 写头）。
RESEARCH_CREATE_HEADER = "X-Graph-Research-Run-Create"

CREATE_BODY_KEYS = frozenset(
    (
        "spec_id",
        "spec_digest",
        "alignment_id",
        "episode_id",
        "evidence_bundle_digest",
        "escalation_id",
        "requester",
    )
)

RUN_ID_NAMESPACE = "exec:graph-research:"
RUN_ID_DOMAIN = "graph-research-escalation-v1"

# Research 专用 spec allowlist（rev5 冻结）：与 Pilot allowlist 完全分离，互不扩大。
RESEARCH_SPEC_ALLOWLIST = frozenset((SPEC_DIGEST,))
# v1（result_human_review）与 v3（human_review）候选复核闸门 id 集合。
_REVIEW_GATE_IDS = frozenset(("result_human_review", "human_review"))

_EVIDENCE_BUNDLE_VERSION = "fragment-research-evidence-bundle-v1"
_ACTIVE_MARKERS = ("inherited", "newly_collected")
_ALIGNMENT_ID = re.compile(r"^align:[0-9a-f]{24}$")
_EPISODE_ID = re.compile(r"^episode:[0-9a-f]{24}$")
_ESCALATION_ID = re.compile(r"^escalation:[0-9a-f]{24}$")


class ResearchBridgeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _append_research_audit(
    store: SQLiteCheckpointStore, run_id: str, key: str, value: Mapping[str, Any]
) -> None:
    for _attempt in range(3):
        # 写路径一律从 raw 存储形态开始，绝不基于 compat view 回写。
        found = store.latest_raw_with_sequence(run_id)
        if found is None:
            raise KeyError(run_id)
        checkpoint, sequence = found

        def build(assigned: int) -> Any:
            content: dict[str, Any] = {key: dict(value)}
            state = raw_eval_value(checkpoint.eval_results, "graph_state")
            if isinstance(state, dict) and isinstance(state.get("human_gates"), dict):
                gates = {
                    gate_id: (
                        {**gate, "expected_sequence": assigned}
                        if isinstance(gate, dict) and gate.get("status") == "pending"
                        else gate
                    )
                    for gate_id, gate in state["human_gates"].items()
                }
                content["graph_state"] = {**state, "human_gates": gates}
            # form-preserving：旧 flat 保持 flat；namespaced 行写 system，
            # 顶层持续仅 system/plugins。
            eval_results = merge_runtime_eval_results(
                existing=checkpoint.eval_results, content=content
            )
            return replace(checkpoint, eval_results=eval_results)

        if (
            store.append_with_assigned_sequence(
                run_id,
                expected_sequence=sequence,
                build=build,
                event_type="research_result_projected",
            )
            is not None
        ):
            return
    raise ResearchBridgeError("cas_conflict")


def _project_research_result(
    store: SQLiteCheckpointStore, ledger: AgentLedgerStore, run_id: str
) -> None:
    completed = [row for row in ledger.list_for_run(run_id) if row.status == "completed"]
    if not completed:
        return
    raw = ledger.get(completed[-1].session_id)
    if raw is None or not raw.get("result_json") or not raw.get("result_digest"):
        return
    try:
        result = json.loads(str(raw["result_json"]))
    except json.JSONDecodeError as error:
        raise ResearchBridgeError("result_invalid") from error
    projection = {
        "available": True,
        "result_digest": str(raw["result_digest"]),
        "result": result,
    }
    current = store.latest(run_id)
    if current is not None and current.eval_results.get("research_result") == projection:
        return
    _append_research_audit(store, run_id, "research_result", projection)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def deterministic_run_id(
    spec_digest: str,
    alignment_id: str,
    episode_id: str,
    evidence_bundle_digest: str,
    escalation_id_value: str,
) -> str:
    """五元绑定的确定性 Run ID（DESIGN §5.3 逐字）。"""
    material = "\x1f".join(
        (
            RUN_ID_DOMAIN,
            spec_digest,
            alignment_id,
            episode_id,
            evidence_bundle_digest,
            escalation_id_value,
        )
    )
    return RUN_ID_NAMESPACE + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def validate_create_body(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != CREATE_BODY_KEYS:
        raise ResearchBridgeError("invalid_body")
    # rev6 P0-1：按请求中已冻结的 spec id/digest 选择版本——v1 旧请求
    # 走 v1（只读兼容重放），v3 新请求走 v3；未知/过期 digest 失败关闭，
    # 绝不把旧请求静默改成新 spec，也不允许不同 spec 共用同一 run identity。
    spec_id = raw.get("spec_id")
    if spec_id == RESEARCH_GRAPH_ID:
        if not _is_sha256(raw.get("spec_digest")):
            raise ResearchBridgeError("invalid_body")
        if raw["spec_digest"] not in RESEARCH_SPEC_ALLOWLIST:
            raise ResearchBridgeError("plan_expired")
    elif spec_id == RESEARCH_MACRO_V3_GRAPH_ID:
        if raw.get("spec_digest") != RESEARCH_MACRO_V3_SPEC_DIGEST:
            raise ResearchBridgeError("plan_expired")
    else:
        raise ResearchBridgeError("spec_not_allowed")
    alignment_id = raw.get("alignment_id")
    if not isinstance(alignment_id, str) or _ALIGNMENT_ID.fullmatch(alignment_id) is None:
        raise ResearchBridgeError("invalid_body")
    episode_id = raw.get("episode_id")
    if not isinstance(episode_id, str) or _EPISODE_ID.fullmatch(episode_id) is None:
        raise ResearchBridgeError("invalid_body")
    escalation = raw.get("escalation_id")
    if not isinstance(escalation, str) or _ESCALATION_ID.fullmatch(escalation) is None:
        raise ResearchBridgeError("invalid_body")
    if not _is_sha256(raw.get("evidence_bundle_digest")):
        raise ResearchBridgeError("invalid_body")
    if raw.get("requester") != "nigo":
        raise ResearchBridgeError("invalid_body")
    return dict(raw)


def active_bundle_records(records: object) -> list[dict[str, Any]]:
    """证据包中可继承的 active 记录（inherited/newly_collected；逐条 digest 复核）。"""
    if not isinstance(records, list):
        return []
    active: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("marker") not in _ACTIVE_MARKERS:
            continue
        if record.get("evidence_digest") != evidence_digest(record):
            # TOCTOU：持久化记录内容漂移 → 拒绝创建（零 Run）。
            raise ResearchBridgeError("evidence_bundle_drift")
        active.append(dict(record))
    return active


def evidence_bundle_digest_of(records: list[dict[str, Any]]) -> str:
    """证据包摘要：版本域 + 排序后的 active evidence_digest 列表（可独立复算）。"""
    digests = sorted(str(record["evidence_digest"]) for record in records)
    return digest_of([_EVIDENCE_BUNDLE_VERSION, *digests])


def check_prefix_allowlist() -> None:
    """推进至授权检查的精确结构允许清单（§5.3）；任何漂移 → unsafe_prefix。"""
    spec = RESEARCH_SPEC
    if spec.entry_node != PREFIX_NODES[0]:
        raise ResearchBridgeError("unsafe_prefix")
    expected_kinds = {
        INPUT_NODE: "input",
        OFFICIAL_NODE: "capability",
        COMMUNITY_NODE: "capability",
        EVIDENCE_JOIN: "join",
        PREPARATION_NODE: "capability",
        AUTHORIZATION_CHECK_NODE: "capability",
    }
    expected_adapters = {
        INPUT_NODE: ADAPTER_INPUT,
        OFFICIAL_NODE: ADAPTER_OFFICIAL,
        COMMUNITY_NODE: ADAPTER_COMMUNITY,
        PREPARATION_NODE: ADAPTER_PREPARATION,
        AUTHORIZATION_CHECK_NODE: ADAPTER_AUTHORIZATION_CHECK,
    }
    if tuple(expected_adapters.values()) != PREFIX_ADAPTERS:
        raise ResearchBridgeError("unsafe_prefix")
    for node_id in PREFIX_NODES:
        node = spec.node_map.get(node_id)
        if node is None or node.kind != expected_kinds[node_id]:
            raise ResearchBridgeError("unsafe_prefix")
        if node_id == EVIDENCE_JOIN:
            join = node.join or {}
            if (
                join.get("mode") != "all_success"
                or join.get("on_partial_failure") != "continue"
                or join.get("cancel_remaining") is not False
            ):
                raise ResearchBridgeError("unsafe_prefix")
        elif node.adapter != expected_adapters[node_id]:
            raise ResearchBridgeError("unsafe_prefix")
    expected_edges = {
        "e_input_official": (INPUT_NODE, OFFICIAL_NODE),
        "e_input_community": (INPUT_NODE, COMMUNITY_NODE),
        "e_official_join": (OFFICIAL_NODE, EVIDENCE_JOIN),
        "e_community_join": (COMMUNITY_NODE, EVIDENCE_JOIN),
        "e_join_preparation": (EVIDENCE_JOIN, PREPARATION_NODE),
        "e_preparation_authz": (PREPARATION_NODE, AUTHORIZATION_CHECK_NODE),
    }
    if tuple(expected_edges) != PREFIX_EDGES:
        raise ResearchBridgeError("unsafe_prefix")
    for edge_id, (from_node, to_node) in expected_edges.items():
        edge = spec.edge_map.get(edge_id)
        if (
            edge is None
            or edge.type != "sequence"
            or edge.from_node != from_node
            or edge.to_node != to_node
        ):
            raise ResearchBridgeError("unsafe_prefix")


# ---------------------------------------------------------------------------
# Graph 节点 adapter（全部复用 GovernedResearchRunner；前缀 adapter 恒确定性）
# ---------------------------------------------------------------------------


def _active_records(collection: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(record)
        for record in collection.get("records", [])
        if isinstance(record, dict) and record.get("marker") in _ACTIVE_MARKERS
    ]


def _macro_inherited_seed(
    run_inputs: Mapping[str, Any],
) -> list[dict[str, Any]] | NodeResult:
    """宏 v3 首进 collect 的继承证据 seed（rev8 P0-2）：来自注册材料
    （受绑定、随 run 持久化、可重启恢复——非进程内临时 map）；逐条
    digest 复核并复算 evidence_bundle_digest 绑定校验，任何漂移/非法
    形状失败关闭，绝不把未绑定客户端内容当证据。"""
    raw = run_inputs.get("inherited_records")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return NodeResult(output={}, failure="inherited_seed_invalid")
    try:
        active = active_bundle_records(list(raw))
    except ResearchBridgeError as error:
        return NodeResult(output={}, failure=error.code)
    expected = run_inputs.get("evidence_bundle_digest")
    if isinstance(expected, str) and expected:
        if evidence_bundle_digest_of(active) != expected:
            return NodeResult(output={}, failure="evidence_bundle_drift")
    return active


def research_adapters(runner: GovernedResearchRunner) -> dict[str, Any]:
    """Research Graph 的节点 handler 表；handler 只来自此显式注册表。"""

    def _collection(run_id: str) -> Mapping[str, Any] | NodeResult:
        collection = runner.collection_outcome(run_id)
        if collection is None:
            return NodeResult(output={}, failure="collection_missing")
        return collection

    def input_adapter(request: NodeRequest) -> NodeResult:
        inputs = request.run_inputs
        return NodeResult(
            output={
                "research_goal": str(inputs.get("research_goal", "")),
                "alignment_id": str(inputs.get("alignment_id", "")),
                "episode_id": str(inputs.get("episode_id", "")),
                "escalation_id": str(inputs.get("escalation_id", "")),
                "evidence_bundle_digest": str(inputs.get("evidence_bundle_digest", "")),
            }
        )

    def fetch_adapter(scope: str) -> Any:
        def handler(request: NodeRequest) -> NodeResult:
            collection = _collection(request.run_id)
            if isinstance(collection, NodeResult):
                return collection
            if scope == "official":
                scoped = [
                    record
                    for record in _active_records(collection)
                    if record.get("source_target") == "official"
                ]
            else:
                # 社区分支覆盖 community 与 independent 两类来源目标（§8 闭集）。
                scoped = [
                    record
                    for record in _active_records(collection)
                    if record.get("source_target") in ("community", "independent")
                ]
            newly = sum(1 for record in scoped if record.get("marker") == "newly_collected")
            return NodeResult(
                output={
                    "scope": scope,
                    "record_count": len(scoped),
                    "inherited_count": len(scoped) - newly,
                    "newly_collected_count": newly,
                    "evidence_digests": sorted(str(record["evidence_digest"]) for record in scoped),
                    # 证据足够分支：确定性标记「复用已有证据」，该分支网络请求为 0。
                    "reused": bool(scoped) and newly == 0,
                }
            )

        return handler

    def preparation_adapter(request: NodeRequest) -> NodeResult:
        collection = _collection(request.run_id)
        if isinstance(collection, NodeResult):
            return collection
        active = _active_records(collection)
        try:
            synthesis_request = build_synthesis_request(
                str(collection.get("goal", "")),
                active,
                claim_types=[str(item) for item in collection.get("claim_types", [])],
            )
        except GovernedResearchError as error:
            return NodeResult(output={}, failure=error.code)
        return NodeResult(
            output={
                "input_digest": synthesis_request.input_digest,
                "included_count": synthesis_request.included_count,
                "omitted_count": synthesis_request.omitted_count,
                "request_bytes": len(synthesis_request.request_bytes),
                "evidence_count": len(active),
            }
        )

    def authorization_check_adapter(request: NodeRequest) -> NodeResult:
        decision = runner.check_authorization(request.run_id)
        return NodeResult(
            output={
                "outcome": str(decision.get("outcome", "blocked")),
                "reason": str(decision.get("reason", "")),
                "authorization_digest": str(decision.get("authorization_digest", "")),
            }
        )

    def branch_marker_adapter(request: NodeRequest) -> NodeResult:
        # 无副作用纯确定性分支标记（rev6 冻结）：四个 marker 共用同一 adapter。
        return NodeResult(output={"branch": request.node_id, "passed": True})

    def synthesis_adapter(request: NodeRequest) -> NodeResult:
        try:
            outcome = runner.run_synthesis(request.run_id)
        except GovernedResearchError as error:
            return NodeResult(output={}, failure=error.code)
        result = outcome.get("result")
        summary = ""
        if outcome.get("status") == "synthesized" and isinstance(result, dict):
            summary = str(result.get("summary", ""))
        if not summary:
            summary = str(outcome.get("note", ""))
        return NodeResult(
            output={
                "status": str(outcome.get("status", "")),
                "model_calls": int(outcome.get("model_calls", 0)),
                "stop_reason": str(outcome.get("stop_reason", "")),
                "summary": summary,
                "result_digest": str(outcome.get("result_digest", "")),
                "receipt_digest": str(outcome.get("receipt_digest", "")),
                "reason": str(outcome.get("reason", "")),
            }
        )

    def validator_adapter(request: NodeRequest) -> NodeResult:
        flattened: dict[str, Any] = {}
        for source in sorted(request.inputs):
            output = request.inputs[source]
            if isinstance(output, dict):
                flattened.update(output)
        status = str(flattened.get("status", ""))
        result_digest = str(flattened.get("result_digest", ""))
        # §3.9 严格输出契约已在 run_synthesis 内全量执行；此处确定性复核
        # 「成功且具备结果摘要」才允许进入人工复核的成功语义。
        valid = status == "synthesized" and bool(result_digest)
        return NodeResult(
            output={
                "valid": valid,
                "status": status,
                "summary": str(flattened.get("summary", "")),
                "result_digest": result_digest,
            }
        )

    def macro_input_adapter(request: NodeRequest) -> NodeResult:
        # TASK E 宏 v3：界定问题（离线接缝；与 v1 input 同形，独立 adapter id）。
        return NodeResult(
            output={"research_goal": str(request.run_inputs.get("research_goal", ""))}
        )

    def macro_collect_adapter(request: NodeRequest) -> NodeResult:
        # 收集证据：首进做首轮（max_rounds=1 + inherited seed——受绑定、
        # 可重启的注册材料）；反馈边再进时做第二轮
        # （max_rounds=2——round-1 动作经幂等账本零重发，只新增改述/反证
        # 查询）。rev8 P0-2：collection 只在本节点域真实发生。
        collection = runner.collection_outcome(request.run_id)
        if collection is None:
            seed = _macro_inherited_seed(request.run_inputs)
            if isinstance(seed, NodeResult):
                return seed
            inherited_seed: list[Mapping[str, Any]] = list(seed)
            collection = runner.collect(
                request.run_id,
                str(request.run_inputs.get("research_goal", "")),
                max_rounds=1,
                inherited=inherited_seed,
            )
            runner.persist_collection(request.run_id, collection)
        else:
            active = _active_records(collection)
            claim_types = [str(item) for item in collection.get("claim_types", [])]
            needs_more = bool(
                coverage_gaps(active, claim_types) or detect_conflict_types(active)
            )
            if needs_more and len(collection.get("rounds", [])) < MAX_RESEARCH_ROUNDS:
                # 反馈段（rev8 P0-2）：首段有效 records（含 seed）经 inherited
                # 重验证带入，沿用首轮冻结 claim_types——证据持续累积，绝不
                # 在第二段丢失首段成果；round-1 动作经幂等账本零重发。
                inherited_active: list[Mapping[str, Any]] = list(active)
                collection = runner.collect(
                    request.run_id,
                    str(collection["goal"]),
                    claim_types=claim_types or None,
                    inherited=inherited_active,
                    max_rounds=MAX_RESEARCH_ROUNDS,
                )
                runner.persist_collection(request.run_id, collection)
        active = _active_records(collection)
        return NodeResult(
            output={
                "evidence_count": len(active),
                "evidence_digests": sorted(
                    str(record["evidence_digest"]) for record in active
                ),
                "stop_reason": str(collection.get("stop_reason", "")),
            }
        )

    def macro_coverage_adapter(request: NodeRequest) -> NodeResult:
        # 覆盖度判断：无缺口且无冲突 → ready 进授权检查；真实耗尽或轮数
        # 用尽仍有缺口 → persist_watch 持久观察条件，并同样以业务失败
        # 返回——GraphRuntime 语义中 feedback 边只在节点失败时遍历：
        # 第一次 traversal 回采集段，轮尽后 feedback exhausted 以 pause
        # 策略把 run 置于非终态 paused（rev6 P0-2：可 reopen 恢复，绝不
        # 是不可恢复的 terminal output，绝不进人工闸门、不成用户待办）。
        collection = runner.collection_outcome(request.run_id)
        if collection is None:
            return NodeResult(output={}, failure="collection_missing")
        active = _active_records(collection)
        claim_types = [str(item) for item in collection.get("claim_types", [])]
        gaps = coverage_gaps(active, claim_types)
        conflicts = detect_conflict_types(active)
        rounds_done = len(collection.get("rounds", []))
        covered = not gaps and not conflicts
        watching = bool(
            collection.get("plan_exhausted")
            or (rounds_done >= MAX_RESEARCH_ROUNDS and (gaps or conflicts) and not covered)
        )
        if covered:
            return NodeResult(
                output={
                    "ready": True,
                    "watching": False,
                    "needs_more": False,
                    "gaps": gaps,
                    "conflicts": conflicts,
                    # 透传给下游授权检查的输入契约（spec input_schema 绑定）。
                    "evidence_count": len(active),
                }
            )
        if watching:
            runner.persist_watch(
                request.run_id,
                reason="plan_exhausted",
                details={"goal": str(collection.get("goal", ""))},
            )
        return NodeResult(
            output={
                "ready": False,
                "watching": watching,
                # needs_more 必须保持 True：feedback 边 condition 据此匹配；
                # 轮尽后由 exhausted pause 进入可恢复观察暂停。
                "needs_more": True,
                "gaps": gaps,
                "conflicts": conflicts,
                "evidence_count": len(active),
            },
            failure="coverage_needs_more",
        )

    def macro_authorization_check_adapter(request: NodeRequest) -> NodeResult:
        # 宏 v3 授权检查（rev6 P0-3）：synthesis 能力未开启
        # （research_live_disabled）归并为 manual_required——证据充分但
        # 模型未授权即 awaiting_model_authorization，进入治理闸门；
        # 其余 blocked（材料漂移/输入不可用）保持 blocked 无出边，
        # 诚实 blocked，绝不进闸门也绝不进候选复核。
        decision = runner.check_authorization(request.run_id)
        outcome = str(decision.get("outcome", "blocked"))
        reason = str(decision.get("reason", ""))
        if outcome == "blocked" and reason == "research_live_disabled":
            outcome = "manual_required"
        return NodeResult(
            output={
                "outcome": outcome,
                "reason": reason,
                "authorization_digest": str(decision.get("authorization_digest", "")),
            }
        )

    def macro_judge_adapter(request: NodeRequest) -> NodeResult:
        # 形成判断（宏 v3 输出契约）：复用受治理 synthesis 路径——模型恒受
        # 精确授权/预算/Receipt 约束。synthesized 必须具备契约验证的
        # result digest 与 summary 才是候选（supported=True）；synthesis
        # 关闭/未授权时诚实 supported=False（rev6 P0-3：绝不把空结果
        # 交给候选人工复核）。
        try:
            outcome = runner.run_synthesis(request.run_id)
        except GovernedResearchError as error:
            return NodeResult(output={}, failure=error.code)
        status = str(outcome.get("status", ""))
        result = outcome.get("result")
        summary = ""
        if status == "synthesized" and isinstance(result, dict):
            summary = str(result.get("summary", ""))
        result_digest = str(outcome.get("result_digest", ""))
        judgment_status = (
            "synthesized"
            if status == "synthesized"
            else "awaiting_model_authorization"
            if status in ("synthesis_disabled", "awaiting_authorization")
            else status or "unknown"
        )
        return NodeResult(
            output={
                "claim_summary": summary,
                "supported": bool(status == "synthesized" and result_digest and summary),
                "judgment_status": judgment_status,
            }
        )

    def macro_produce_adapter(request: NodeRequest) -> NodeResult:
        # 产出资产（离线接缝，rev7 P0）：不得依赖跨节点隐式输入——
        # human_review 的 decision 输出固定只有 decision 字段，inputs 里
        # 永远没有 synthesis 结果。从 checkpoint 读取并验证该 run 已
        # 持久化的受治理 research_result（_project_research_result 从
        # Agent 账本同源投影）：available is True、result_digest 为合法
        # SHA-256、summary 取自已验证结果；任何缺失/漂移失败关闭，绝不
        # 产出。真实资产写入仍冻结在既有产品确认协议之后（人类闸门）。
        stored = runner.store.latest(request.run_id)
        research_result = (
            stored.eval_results.get("research_result") if stored is not None else None
        )
        if not isinstance(research_result, Mapping) or research_result.get("available") is not True:
            return NodeResult(output={}, failure="asset_requires_synthesized_result")
        result_digest = research_result.get("result_digest")
        if not _is_sha256(result_digest):
            return NodeResult(output={}, failure="asset_requires_synthesized_result")
        result = research_result.get("result")
        summary = ""
        if isinstance(result, Mapping):
            summary = str(result.get("summary", ""))
        if not summary:
            return NodeResult(output={}, failure="asset_requires_synthesized_result")
        return NodeResult(output={"artifact_digest": str(result_digest), "summary": summary})

    return {
        ADAPTER_INPUT: input_adapter,
        ADAPTER_OFFICIAL: fetch_adapter("official"),
        ADAPTER_COMMUNITY: fetch_adapter("community"),
        ADAPTER_PREPARATION: preparation_adapter,
        ADAPTER_AUTHORIZATION_CHECK: authorization_check_adapter,
        ADAPTER_BRANCH_MARKER: branch_marker_adapter,
        ADAPTER_SYNTHESIS: synthesis_adapter,
        ADAPTER_VALIDATOR: validator_adapter,
        ADAPTER_MACRO_INPUT: macro_input_adapter,
        ADAPTER_MACRO_COLLECT: macro_collect_adapter,
        ADAPTER_MACRO_COVERAGE: macro_coverage_adapter,
        ADAPTER_MACRO_AUTHZ_CHECK: macro_authorization_check_adapter,
        ADAPTER_MACRO_JUDGE: macro_judge_adapter,
        ADAPTER_MACRO_PRODUCE: macro_produce_adapter,
    }


# ---------------------------------------------------------------------------
# Research Creation Bridge
# ---------------------------------------------------------------------------


class ResearchBridge:
    """创建编排：七字段契约 → 归属核验 → 前缀 → 注册 → 缺口补采 → 授权检查。"""

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        intent_store: SQLiteCheckpointStore,
        ledger: AgentLedgerStore,
        runner: GovernedResearchRunner,
    ):
        if runner.store is not store:
            raise ResearchBridgeError("runner_store_mismatch")
        self.store = store
        self.intent_store = intent_store
        self.ledger = ledger
        self.runner = runner
        self._runtime = GraphRuntime(
            store,
            {RESEARCH_GRAPH_ID: RESEARCH_SPEC, RESEARCH_MACRO_V3_GRAPH_ID: RESEARCH_MACRO_V3_SPEC},
            research_adapters(runner),
        )

    # -- 归属核验（alignment∈episode、escalation∈alignment/episode、证据 TOCTOU） --

    def _verify_lineage(self, body: Mapping[str, Any]) -> dict[str, Any]:
        alignment_checkpoint = self.intent_store.latest(str(body["alignment_id"]))
        if alignment_checkpoint is None:
            raise ResearchBridgeError("lineage_not_found")
        alignment = alignment_checkpoint.eval_results.get("alignment")
        if not isinstance(alignment, Mapping):
            raise ResearchBridgeError("lineage_drift")
        if alignment.get("episode_id") != body["episode_id"]:
            raise ResearchBridgeError("lineage_drift")
        execution_run_id = alignment.get("execution_run_id")
        if not isinstance(execution_run_id, str) or not execution_run_id:
            raise ResearchBridgeError("escalation_not_available")
        execution_checkpoint = self.intent_store.latest(execution_run_id)
        if execution_checkpoint is None:
            raise ResearchBridgeError("lineage_not_found")
        binding = execution_checkpoint.eval_results.get("execution_binding")
        if not isinstance(binding, Mapping):
            raise ResearchBridgeError("lineage_drift")
        if (
            binding.get("alignment_id") != body["alignment_id"]
            or binding.get("episode_id") != body["episode_id"]
        ):
            raise ResearchBridgeError("lineage_drift")
        alignment_scope = binding.get("execution_scope")
        source_alignment_digest = binding.get("alignment_digest")
        if not isinstance(alignment_scope, Mapping) or not _is_sha256(
            source_alignment_digest
        ):
            raise ResearchBridgeError("lineage_drift")
        result_digest = execution_checkpoint.eval_results.get("result_digest")
        proposal = execution_checkpoint.eval_results.get("graph_escalation")
        if (
            not _is_sha256(result_digest)
            or not isinstance(proposal, Mapping)
            or proposal.get("source_result_digest") != result_digest
        ):
            raise ResearchBridgeError("escalation_not_available")
        expected_escalation = escalation_id(str(body["alignment_id"]), str(result_digest))
        if body["escalation_id"] != expected_escalation:
            raise ResearchBridgeError("escalation_binding_changed")
        records = active_bundle_records(execution_checkpoint.eval_results.get("research_evidence"))
        if evidence_bundle_digest_of(records) != body["evidence_bundle_digest"]:
            raise ResearchBridgeError("evidence_bundle_drift")
        title = str(binding.get("title", ""))
        supplement = str(binding.get("supplement", ""))
        if not title.strip() and not supplement.strip():
            raise ResearchBridgeError("lineage_drift")
        goal = bounded_research_goal(title, supplement)
        return {
            "goal": goal,
            "title": title[:120],
            "records": records,
            "fragment_id": alignment_checkpoint.fragment_id,
            "execution_run_id": execution_run_id,
            "alignment_scope": dict(alignment_scope),
            "source_alignment_digest": str(source_alignment_digest),
        }

    def _escalation_already_bridged(self, run_id: str, escalation: str) -> bool:
        for other_id in self.store.run_ids_with_prefix("graph:"):
            if other_id == run_id:
                continue
            found = self.store.latest(other_id)
            if found is None:
                continue
            registration = found.eval_results.get("registration")
            if not isinstance(registration, Mapping):
                continue
            inputs = registration.get("run_inputs")
            if isinstance(inputs, Mapping) and inputs.get("escalation_id") == escalation:
                return True
        return False

    def _model_calls(self, run_id: str) -> int:
        """如实模型发送计数：只计 request_sent=true 的账本行（恒 0|1）。"""
        return sum(1 for row in self.ledger.list_for_run(run_id) if str(row.request_sent) == "true")

    def _repair_presettle_goal_binding(
        self, run_id: str, expected_inputs: Mapping[str, Any]
    ) -> None:
        """Repair the one safe pre-settle drift caused by the old unbounded goal.

        The external seven-field identity is unchanged.  Repair is allowed
        only before any Graph step or model reservation, and only when the
        bounded ``research_goal`` and its safe ``task_label`` are the sole
        registration differences.  This keeps
        the already-created deterministic Run auditable instead of replacing
        it after a production-safe failure.
        """
        for _attempt in range(3):
            # 写路径一律从 raw 存储形态开始，绝不基于 compat view 回写。
            found = self.store.latest_raw_with_sequence(run_id)
            if found is None:
                return
            checkpoint, sequence = found
            state = raw_eval_value(checkpoint.eval_results, "graph_state")
            registration = raw_eval_value(checkpoint.eval_results, "registration")
            if not isinstance(state, dict) or not isinstance(registration, dict):
                return
            current_inputs = registration.get("run_inputs")
            if not isinstance(current_inputs, dict) or current_inputs == dict(expected_inputs):
                return
            comparable_current = {
                **current_inputs,
                "research_goal": expected_inputs["research_goal"],
                "task_label": expected_inputs["task_label"],
            }
            if (
                comparable_current != dict(expected_inputs)
                or state.get("run_status") != "running"
                or int(state.get("step_count", -1)) != 0
                or self._model_calls(run_id) != 0
            ):
                return
            state_inputs = state.get("run_inputs")
            if not isinstance(state_inputs, dict) or state_inputs != current_inputs:
                return
            repaired_state = {**state, "run_inputs": dict(expected_inputs)}
            repaired_registration = {**registration, "run_inputs": dict(expected_inputs)}
            # form-preserving：旧 flat 保持 flat；namespaced 行三键都写
            # system，顶层持续仅 system/plugins。
            repaired = replace(
                checkpoint,
                eval_results=merge_runtime_eval_results(
                    existing=checkpoint.eval_results,
                    content={
                        "graph_state": repaired_state,
                        "registration": repaired_registration,
                        "registration_repair": {
                            "fields": ["research_goal", "task_label"],
                            "reason": "bounded_goal_contract",
                        },
                    },
                ),
            )
            committed = self.store.compare_and_append(
                repaired,
                expected_sequence=sequence,
                event_type="registration_binding_repaired",
                revision_reason="bounded_goal_contract",
            )
            if committed is not None:
                return

    # -- 创建编排 -----------------------------------------------------------

    def create_run(self, raw_body: object) -> tuple[int, dict[str, Any]]:
        """返回 (HTTP 状态, 响应体)。语义：201 首建 / 200 幂等重放 /
        4xx 注册前拒绝（零 Run）/ 201+blocked 推进异常（Run 可见）。"""
        body = validate_create_body(raw_body)
        # 按请求冻结的 spec id 选择注册版本；前缀结构允许清单只适用于
        # v1 线性 spec（v3 宏图结构不同，由自身 spec 校验冻结）。
        spec = (
            RESEARCH_MACRO_V3_SPEC
            if body["spec_id"] == RESEARCH_MACRO_V3_GRAPH_ID
            else RESEARCH_SPEC
        )
        if spec is RESEARCH_SPEC:
            check_prefix_allowlist()
        lineage = self._verify_lineage(body)

        run_id = deterministic_run_id(
            str(body["spec_digest"]),
            str(body["alignment_id"]),
            str(body["episode_id"]),
            str(body["evidence_bundle_digest"]),
            str(body["escalation_id"]),
        )
        escalation = str(body["escalation_id"])
        if self._escalation_already_bridged(run_id, escalation):
            raise ResearchBridgeError("already_bridged")
        claim_key = hashlib.sha256(
            f"graph-research-escalation-claim-v1\x1f{escalation}".encode()
        ).hexdigest()
        _claimed, claimed_run_id, claimed_binding = self.store.claim_run_identity(
            idempotency_key=claim_key,
            run_id=run_id,
            action_type="graph_research_registration",
            binding_digest=run_id,
        )
        if claimed_run_id != run_id or claimed_binding != run_id:
            raise ResearchBridgeError("already_bridged")
        run_inputs = {
            "spec_id": str(body["spec_id"]),
            "spec_digest": str(body["spec_digest"]),
            "alignment_id": str(body["alignment_id"]),
            "episode_id": str(body["episode_id"]),
            "evidence_bundle_digest": str(body["evidence_bundle_digest"]),
            "escalation_id": str(body["escalation_id"]),
            "requester": "nigo",
            "research_goal": lineage["goal"],
            "task_label": lineage["title"],
            "alignment_scope": lineage["alignment_scope"],
            "source_alignment_digest": lineage["source_alignment_digest"],
        }
        if spec is RESEARCH_MACRO_V3_SPEC:
            # rev8 P0-2：继承证据以受绑定、可重启的 seed 随注册材料持久化
            # （active_bundle_records 已逐条 digest 复核）；collection 必须由
            # 宏 evidence_collection 节点真实驱动，bridge 不做网络 collection。
            run_inputs["inherited_records"] = [
                dict(record) for record in lineage["records"]
            ]
        self._repair_presettle_goal_binding(run_id, run_inputs)
        try:
            _checkpoint, created = self._runtime.register_run(
                spec,
                run_id,
                fragment_ref=str(lineage["fragment_id"]),
                run_inputs=run_inputs,
            )
        except GraphRuntimeError as error:
            if error.code == "run_id_conflict":
                # 同 run_id 不同绑定，或同 escalation 已有 Run（一升级一 Run）。
                raise ResearchBridgeError("already_bridged") from error
            raise

        replaying_incomplete = False
        if not created:
            current = self.store.latest(run_id)
            state = current.eval_results.get("graph_state", {}) if current is not None else {}
            if not isinstance(state, Mapping) or state.get("run_status") != "running":
                return 200, self._status_payload(run_id, idempotent=True)
            # A crash/adapter failure between registration and the first
            # settled state must resume the same deterministic Run.  The
            # collection journal, Checkpoint CAS and Agent ledger absorb
            # duplicate work/sends; creating a replacement Run is forbidden.
            replaying_incomplete = True

        try:
            if spec is RESEARCH_SPEC:
                # v1 历史语义不变：缺口补采在 Graph 外完成；证据足够分支零
                # 网络；使用本 Run 自己的新预算，completed 搜索/抓取零重发。
                collection = self.runner.collection_outcome(run_id)
                if collection is None:
                    collection = self.runner.collect(
                        run_id, str(lineage["goal"]), inherited=lineage["records"]
                    )
                    self.runner.persist_collection(run_id, collection)
                if self.runner.live_enabled and _active_records(collection):
                    # 自动派生 Receipt（v1 历史兼容）：只原子写 Checkpoint；
                    # 派生失败诚实落空 → 授权检查 manual_required 停在闸门。
                    try:
                        self.runner.issue_receipt(
                            run_id,
                            source_alignment_digest=str(lineage["source_alignment_digest"]),
                            alignment_scope=cast(Mapping[str, Any], lineage["alignment_scope"]),
                        )
                    except GovernedResearchError:
                        pass
            # rev8 P0-2/P0-3：v3 分支——collection 必须由宏 evidence_collection
            # 节点真实驱动（首进 max_rounds=1 + inherited seed，feedback 再进
            # 扩到全局上限）；不得在 Graph 节点外做网络 collection，不得在
            # 模型授权闸门前自动签发 Receipt 或调用模型（model_calls=0 到闸）。
            outcome = self._runtime.run_until_settled(run_id)
            _project_research_result(self.store, self.ledger, run_id)
        except Exception as error:
            # 注册后推进异常：Run 已可见，诚实返回 blocked，不诱导重建。
            payload = self._blocked_payload(run_id, f"advance_error:{type(error).__name__}")
            if replaying_incomplete:
                payload["idempotent"] = True
            return (200 if replaying_incomplete else 201), payload

        payload = self._created_payload(run_id, outcome.event)
        if replaying_incomplete:
            payload["idempotent"] = True
        return (200 if replaying_incomplete else 201), payload

    # -- 响应投影 -----------------------------------------------------------

    def _receipt_digest(self, run_id: str) -> str | None:
        stored = self.runner.stored_receipt(run_id)
        if not isinstance(stored, dict):
            return None
        digest = stored.get("authorization_digest")
        return str(digest) if isinstance(digest, str) else None

    def _blocked_payload(self, run_id: str, code: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "blocked",
            "blocked_code": code,
            "sequence": int(self.store.latest_sequence(run_id) or 0),
            "model_calls": self._model_calls(run_id),
        }

    def _created_payload(self, run_id: str, event: str) -> dict[str, Any]:
        found = self.store.latest_with_sequence(run_id)
        if found is None:  # pragma: no cover - register 刚成功，防御性兜底
            raise ResearchBridgeError("run_lost")
        checkpoint, sequence = found
        state = checkpoint.eval_results.get("graph_state", {})
        run_status = str(state.get("run_status", "unknown"))
        payload: dict[str, Any] = {
            "run_id": run_id,
            "sequence": int(sequence),
            "model_calls": self._model_calls(run_id),
        }
        receipt_digest = self._receipt_digest(run_id)
        if receipt_digest is not None:
            payload["receipt_digest"] = receipt_digest
        if run_status == "human_wait":
            payload["status"] = "human_wait"
            payload["current_node"] = checkpoint.current_node
            return payload
        if run_status == "completed":
            nodes = state.get("nodes", {})
            aborted = (
                isinstance(nodes, Mapping)
                and isinstance(nodes.get(ABORTED_OUTPUT), Mapping)
                and nodes[ABORTED_OUTPUT].get("status") == "succeeded"
            )
            if aborted:
                # blocked / live 关闭 → 经 abort join 诚实进入 aborted_output。
                reason = ""
                check = nodes.get(AUTHORIZATION_CHECK_NODE) if isinstance(nodes, Mapping) else None
                if isinstance(check, Mapping):
                    output = check.get("output")
                    if isinstance(output, Mapping):
                        reason = str(output.get("reason", ""))
                payload["status"] = "blocked"
                payload["current_node"] = ABORTED_OUTPUT
                if reason:
                    payload["reason"] = reason
                return payload
            payload["status"] = "completed"
            payload["current_node"] = checkpoint.current_node
            return payload
        # 推进异常（adapter 缺失、输入漂移等）：Run 可见，诚实 blocked。
        payload["status"] = "blocked"
        payload["blocked_code"] = event
        payload["current_node"] = checkpoint.current_node
        return payload

    def _status_payload(self, run_id: str, *, idempotent: bool) -> dict[str, Any]:
        found = self.store.latest_with_sequence(run_id)
        if found is None:  # pragma: no cover - register 刚成功，防御性兜底
            raise ResearchBridgeError("run_lost")
        checkpoint, sequence = found
        state = checkpoint.eval_results.get("graph_state", {})
        payload: dict[str, Any] = {
            "run_id": run_id,
            "status": str(state.get("run_status", "unknown")),
            "current_node": checkpoint.current_node,
            "sequence": int(sequence),
            "model_calls": self._model_calls(run_id),
            "idempotent": idempotent,
        }
        receipt_digest = self._receipt_digest(run_id)
        if receipt_digest is not None:
            payload["receipt_digest"] = receipt_digest
        return payload


class ResearchExecution:
    """Research Run 的人工决定钩子：授权闸门签发恢复 + 有界推进。

    仅对 fragment-research-escalation-v1 精确生效；其他 Run 原样委托注入的
    fallback（pilot_execution.decide 或 graph_service.decide），行为零变化。
    不新增第二套调度器：推进复用同一 GraphRuntime 与公开决定绑定核验。
    """

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        service: GraphService,
        ledger: AgentLedgerStore,
        runner: GovernedResearchRunner,
        *,
        fallback: Callable[[str, str, Mapping[str, Any]], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.service = service
        self.ledger = ledger
        self.runner = runner
        self._fallback = fallback
        self._runtime = GraphRuntime(
            store,
            {RESEARCH_GRAPH_ID: RESEARCH_SPEC, RESEARCH_MACRO_V3_GRAPH_ID: RESEARCH_MACRO_V3_SPEC},
            research_adapters(runner),
        )

    def is_research_run(self, run_id: str) -> bool:
        found = self.store.latest(run_id)
        if found is None:
            return False
        state = found.eval_results.get("graph_state")
        if not isinstance(state, dict):
            return False
        graph_id = state.get("graph_id")
        digest = state.get("spec_digest")
        # rev6 P0-1：v1 旧 run 与 v3 新 run 都识别为 Research Run。
        return (graph_id == RESEARCH_GRAPH_ID and digest == SPEC_DIGEST) or (
            graph_id == RESEARCH_MACRO_V3_GRAPH_ID
            and digest == RESEARCH_MACRO_V3_SPEC_DIGEST
        )

    def issue_receipt(self, run_id: str) -> tuple[int, dict[str, Any]]:
        if not self.is_research_run(run_id):
            raise ResearchBridgeError("run_not_research")
        return self.runner.issue_receipt(run_id)

    def evaluate_due_watches(self) -> dict[str, int]:
        """v3 Graph watch 的 due 恢复（rev6 P0-2）：与 intent coordinator
        同线程承载（不新增第二线程/调度系统）；cycle 语义与并发单飞由
        runner.resume_due_watch 的 CAS 领取保证；证据完整后用 runtime
        既有 reopen_node 恢复同一 run 重进 coverage——不创建替代 run、
        不改写历史 edge。collection 能力关闭时零副作用。"""
        scanned = 0
        resumed = 0
        for run_id in self.store.run_ids_with_prefix(f"graph:{RESEARCH_MACRO_V3_GRAPH_ID}"):
            scanned += 1
            try:
                if self._resume_graph_watch(run_id):
                    resumed += 1
            except Exception:
                # 失败关闭：单个 Run 的异常绝不中断扫描循环。
                continue
        return {"scanned": scanned, "resumed": resumed}

    def _resume_graph_watch(self, run_id: str) -> bool:
        found = self.store.latest(run_id)
        if found is None:
            return False
        state = found.eval_results.get("graph_state")
        if not isinstance(state, dict):
            return False
        if state.get("graph_id") != RESEARCH_MACRO_V3_GRAPH_ID:
            return False
        # 只有非终态 paused（观察暂停）才可恢复；terminal run 绝不重开。
        if str(state.get("run_status")) != RUN_PAUSED:
            return False
        if not self.runner.collection_enabled:
            return False
        if not self.runner.watch_due(run_id):
            return False
        goal = ""
        collection = self.runner.collection_outcome(run_id)
        if isinstance(collection, Mapping) and isinstance(collection.get("goal"), str):
            goal = str(collection["goal"])
        if not goal:
            registration = found.eval_results.get("registration")
            if isinstance(registration, Mapping):
                inputs = registration.get("run_inputs")
                if isinstance(inputs, Mapping):
                    goal = str(inputs.get("research_goal", ""))
        if not goal:
            return False
        outcome = self.runner.resume_due_watch(run_id, goal)
        if outcome is None:
            return False
        coverage = outcome.get("candidate_coverage")
        gaps = coverage.get("gaps") if isinstance(coverage, Mapping) else None
        if not isinstance(gaps, list) or gaps:
            # 仍无完整证据：保持 paused/watching（attempts 已恰好 +1、
            # due 已顺延），run 状态不变。
            return True
        # 证据完整（watch 已清除）：以 deterministic reopen_id + 期望
        # sequence 恢复同一 run 重进 coverage——并发/重启单飞。
        latest = self.store.latest_with_sequence(run_id)
        if latest is None:
            return True
        _checkpoint, sequence = latest
        reason = "watch_evidence_arrived"
        self._runtime.reopen_node(
            run_id,
            COVERAGE_NODE,
            expected_sequence=sequence,
            requester="nigo",
            reason=reason,
            reopen_id=reopen_request_id(
                requester="nigo",
                run_id=run_id,
                node_id=COVERAGE_NODE,
                expected_sequence=sequence,
                reason=reason,
            ),
        )
        self._runtime.run_until_settled(run_id)
        return True

    def decide(self, run_id: str, node_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.is_research_run(run_id):
            # 非 Research Run：原样委托（pilot 钩子或既有 service.decide），零变化。
            if self._fallback is not None:
                return self._fallback(run_id, node_id, body)
            return self.service.decide(run_id, node_id, body)
        if node_id == AUTHORIZATION_GATE and str(body.get("decision", "")) == "approve_synthesis":
            authorization = self.runner.check_authorization(run_id)
            if authorization.get("outcome") != "derived" or body.get(
                "authorization_digest"
            ) != authorization.get("authorization_digest"):
                raise ResearchBridgeError("authorization_required")
        # v1（result_human_review）与 v3（human_review）的候选复核闸门
        # 共用同一 result_digest 绑定校验。
        if node_id in _REVIEW_GATE_IDS and str(body.get("decision", "")) in {
            "accept_result",
            "reject",
        }:
            checkpoint = self.store.latest(run_id)
            stored = (
                checkpoint.eval_results.get("research_result") if checkpoint is not None else None
            )
            if not isinstance(stored, dict) or body.get("result_digest") != stored.get(
                "result_digest"
            ):
                raise ResearchBridgeError("result_digest_mismatch")
        outcome = self.service.decide(run_id, node_id, body)
        if outcome.get("idempotent"):
            return outcome
        self._runtime.run_until_settled(run_id)
        _project_research_result(self.store, self.ledger, run_id)
        found = self.store.latest(run_id)
        state = found.eval_results.get("graph_state", {}) if found is not None else {}
        current_node = found.current_node if found is not None else None
        model_calls = sum(
            1 for row in self.ledger.list_for_run(run_id) if str(row.request_sent) == "true"
        )
        return {
            **outcome,
            "run_status": str(state.get("run_status", "unknown")),
            "current_node": current_node,
            "model_calls": model_calls,
        }


__all__ = [
    "CREATE_BODY_KEYS",
    "PREFIX_ADAPTERS",
    "PREFIX_EDGES",
    "PREFIX_NODES",
    "RESEARCH_CREATE_HEADER",
    "RESEARCH_SPEC_ALLOWLIST",
    "RUN_ID_NAMESPACE",
    "ResearchBridge",
    "ResearchBridgeError",
    "ResearchExecution",
    "active_bundle_records",
    "check_prefix_allowlist",
    "deterministic_run_id",
    "evidence_bundle_digest_of",
    "research_adapters",
    "validate_create_body",
]
