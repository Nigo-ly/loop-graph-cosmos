"""公共类型定义。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str | list[dict[str, Any]]


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens" | "stop_sequence"
    content: str  # 模型文本输出
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw_blocks: list[Any] = field(default_factory=list)  # 原始 SDK content blocks

    @property
    def has_tool_calls(self) -> bool:
        return self.stop_reason == "tool_use" and len(self.tool_calls) > 0


@dataclass
class LoopRecord:
    """单轮 trace 记录。"""

    iteration: int
    timestamp: str
    duration_ms: int
    stop_reason: str
    model_text: str
    input_tokens: int
    output_tokens: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LoopResult:
    """Micro Loop 节点的统一、可持久化输出。"""

    status: str
    output_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    eval_results: dict[str, Any] = field(default_factory=dict)
    next_node: str | None = None
    next_recommended_loop: str | None = None
    stop_reason: str | None = None
    resume_condition: str | None = None
    unresolved_issues: list[str] = field(default_factory=list)
    tokens_used: int = 0
    tool_calls_used: int = 0
# ---------------------------------------------------------------------------
# eval_results namespace (Gate 1)
#
# New registration/run rows are always namespaced: runtime-owned material
# under ``system``, handler output under ``plugins.<adapter_id>`` — never a
# flat business key. Handler payloads are accepted only as
# ``{"plugins": {"<own trusted adapter_id>": {...}}}``; flat payloads,
# unknown top-level keys, protected system keys and foreign plugin
# namespaces are refused by ``merge_plugin_eval_results`` BEFORE any merge
# (zero commit). Legacy flat rows stay untouched: they accept no eval
# content writes and are exposed only through the read-only
# ``legacy_flat_v1`` compat projection — never migrated, never rewritten,
# never double-written.
#
# Canonical form: ``eval_results_canonical`` is the deterministic compact
# JSON (sorted keys, UTF-8, no whitespace) of the merged mapping;
# ``eval_results_digest`` is its SHA-256. Both are recomputation-stable.
# ---------------------------------------------------------------------------

SYSTEM_EVAL_KEYS = frozenset(
    {
        "system",
        "registration",
        "authorization",
        "bindings",
        "budget",
        "receipts",
        "graph_state",
    }
)


class EvalNamespaceError(ValueError):
    """Plugin eval write hit a protected or foreign namespace; zero commit."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_namespaced(eval_results: Mapping[str, Any]) -> bool:
    return "system" in eval_results or "plugins" in eval_results


def raw_eval_value(eval_results: Mapping[str, Any], key: str) -> Any:
    """Read one runtime key from the raw stored form (write-path reads).

    Namespaced rows hold runtime material under ``system``; legacy flat rows
    hold it at the top level. Write paths merge against the raw stored form,
    so they must read from it too — never from the compat view.
    """
    if _is_namespaced(eval_results):
        system = eval_results.get("system")
        if isinstance(system, Mapping):
            return system.get(key)
        return None
    return eval_results.get(key)


def merge_plugin_eval_results(
    *,
    existing: Mapping[str, Any],
    adapter_id: str,
    returned: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and merge handler-returned eval content, fail-closed.

    Contract (Gate 1, frozen): on a namespaced row the ONLY accepted payload
    shape is ``{"plugins": {"<adapter_id>": {...}}}`` — flat business keys,
    unknown top-level keys, protected system keys and foreign plugin
    namespaces all raise ``EvalNamespaceError`` with a stable code BEFORE
    anything merges, so a violation commits nothing. ``adapter_id`` is the
    trusted registry key (the current node from the pinned spec), never
    self-reported. An empty payload is no eval write and returns the
    existing mapping unchanged. A legacy flat row (no ``system``/``plugins``
    top level) never accepts eval content writes — that would mix forms
    (double-write/back-write); it fails closed with
    ``eval_namespace_violation:legacy_flat_row``.

    Namespace exclusivity (Matrix G1-23): a plugin write ONLY mutates
    ``plugins.<adapter_id>`` — every other plugin namespace (and all
    digest-bound history) stays byte-identical; a write never deletes or
    replaces foreign content. Same-named keys across namespaces are resolved
    deterministically by the read-only ``legacy_flat_v1`` projection
    (system/legacy material wins, then sorted plugin ids, first wins) —
    never by mutating foreign namespaces at write time. A plugin can NEVER
    delete or replace system material: any content key in
    ``SYSTEM_EVAL_KEYS`` or already present in the
    current ``system`` mapping is refused before the merge with a stable
    ``eval_namespace_violation:system[:_conflict]:<key>`` code and zero
    commit. Older values remain in the checkpoint history; nothing is
    rewritten.
    """
    for key in sorted(returned):
        if key in SYSTEM_EVAL_KEYS:
            raise EvalNamespaceError(f"eval_namespace_violation:{key}")
        if key != "plugins":
            raise EvalNamespaceError(f"eval_namespace_violation:top_level:{key}")
    plugins_payload = returned.get("plugins")
    own_content: dict[str, Any] = {}
    if plugins_payload is not None:
        if not isinstance(plugins_payload, Mapping):
            raise EvalNamespaceError("eval_namespace_violation:plugins")
        for target in sorted(plugins_payload):
            if target != adapter_id:
                raise EvalNamespaceError(f"eval_namespace_violation:plugins:{target}")
        content = plugins_payload.get(adapter_id)
        if content is not None and not isinstance(content, Mapping):
            raise EvalNamespaceError(f"eval_namespace_violation:plugins:{adapter_id}")
        own_content = dict(content or {})
    if plugins_payload is None:
        return dict(existing)  # 无 eval 写入：原样（旧 flat row 可无写推进）
    if not _is_namespaced(existing):
        raise EvalNamespaceError("eval_namespace_violation:legacy_flat_row")
    merged = dict(existing)
    system = dict(merged.get("system") or {})
    # 插件内容键逐键检查（payload 第二层的 plugins.<self>.<key>）：
    # 受保护键或与既有 system 材料冲突的键一律合并前拒绝，零提交。
    for key in sorted(own_content):
        if key in SYSTEM_EVAL_KEYS:
            raise EvalNamespaceError(f"eval_namespace_violation:system:{key}")
        if key in system:
            raise EvalNamespaceError(f"eval_namespace_violation:system_conflict:{key}")
    plugins = {
        str(owner): dict(content)
        for owner, content in (merged.get("plugins") or {}).items()
        if isinstance(content, Mapping)
    }
    own = dict(plugins.get(adapter_id) or {})
    own.update(own_content)
    plugins[adapter_id] = own
    # 命名空间排他（G1-23）：只写 plugins.<adapter_id>；其他插件命名空间
    # 与其 digest 历史逐字节不动。同名键冲突由 legacy_flat_v1 只读投影在
    # 读取侧确定性裁决（system/legacy 恒胜，插件按 id 排序先见者胜）。
    merged["system"] = system
    merged["plugins"] = plugins
    return merged


def legacy_flat_eval_view(eval_results: Mapping[str, Any]) -> Mapping[str, Any]:
    """legacy_flat_v1 read-only compat projection of stored eval_results.

    Old flat rows are returned with identical content; namespaced rows are
    flattened deterministically (sorted ``system`` keys first, then sorted
    plugin ids; existing flat keys always win collisions, so plugin content
    can never shadow system/legacy material). The result is a read-only
    Mapping over a DEEP detached copy (stdlib ``copy.deepcopy`` of plain
    JSON values): top-level assignment raises ``TypeError``, and mutating
    any nested dict/list reaches nothing shared with the stored row or any
    other reader. The view is never written back to the database.
    """
    return MappingProxyType(deepcopy(_legacy_flat_copy(eval_results)))


def _legacy_flat_copy(eval_results: Mapping[str, Any]) -> dict[str, Any]:
    if not _is_namespaced(eval_results):
        return dict(eval_results)
    flat: dict[str, Any] = {}
    system = eval_results.get("system")
    if isinstance(system, Mapping):
        for key in sorted(system):
            flat[key] = system[key]
    plugins = eval_results.get("plugins")
    if isinstance(plugins, Mapping):
        for owner in sorted(plugins):
            content = plugins[owner]
            if not isinstance(content, Mapping):
                continue
            for key in sorted(content):
                if key not in flat:
                    flat[key] = content[key]
    return flat


def plugin_eval(adapter_id: str, content: Mapping[str, Any]) -> dict[str, Any]:
    """The only legal handler eval payload: own-namespace plugins write."""
    return {"plugins": {adapter_id: dict(content)}}


def merge_runtime_eval_results(
    *,
    existing: Mapping[str, Any],
    content: Mapping[str, Any] | None = None,
    remove: Iterable[str] = (),
) -> dict[str, Any]:
    """Runtime/control-plane eval write, form-preserving and fail-closed.

    Namespaced rows write ``content`` under ``system`` (runtime-only);
    legacy flat rows keep their flat form — never migrated, never gaining
    namespace keys implicitly. Single-owner rule: every key in ``content``
    or ``remove`` is deleted from every plugin namespace (and ``remove``
    keys also from ``system``), so the new row keeps exactly one owner per
    key — the legacy flat last-write-wins semantics, with provenance.
    ``content`` may never address the ``system``/``plugins`` namespace keys
    directly; other keys (including runtime material such as
    ``graph_state``/``registration``) land under ``system`` on namespaced
    rows — runtime and control-plane writers only, never handlers.
    """
    content = content or {}
    for key in sorted(content):
        if key in ("system", "plugins"):
            raise EvalNamespaceError(f"eval_namespace_violation:{key}")
    if not _is_namespaced(existing):
        merged = dict(existing)
        for key in remove:
            merged.pop(key, None)
        merged.update(content)
        return merged
    merged = dict(existing)
    system = dict(merged.get("system") or {})
    plugins = {
        str(owner): dict(plugin_content)
        for owner, plugin_content in (merged.get("plugins") or {}).items()
        if isinstance(plugin_content, Mapping)
    }
    for key in remove:
        system.pop(key, None)
        for plugin_content in plugins.values():
            plugin_content.pop(key, None)
    for key, value in content.items():
        system[key] = value
        for plugin_content in plugins.values():
            plugin_content.pop(key, None)
    merged["system"] = system
    merged["plugins"] = plugins
    return merged


def translate_view_edit(
    *,
    existing: Mapping[str, Any],
    edited_flat: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate a legacy-flat read-modify-write into the row's own form.

    Control-plane services compute the next eval state as a flat dict
    derived from the ``legacy_flat_v1`` view. This helper diffs that edited
    flat dict against the current view and applies the delta to the raw
    stored form with owner-preserving routing: a changed key currently
    owned by exactly one plugin namespace is written back into that
    namespace (so a later handler write of the same key never collides
    with system material); every other changed/added key is a runtime
    ``system`` write; removed keys disappear everywhere. Each key keeps
    exactly one owner — the legacy flat last-write-wins semantics. Legacy
    flat rows keep the flat result verbatim: never migrated, never
    flattened back from namespaced form. Namespace-addressing keys
    (``system``/``plugins``) in the delta are refused.
    """
    if not _is_namespaced(existing):
        return dict(edited_flat)
    view = _legacy_flat_copy(existing)
    removed = tuple(key for key in view if key not in edited_flat)
    changed = {
        key: value
        for key, value in edited_flat.items()
        if key not in view or view[key] != value
    }
    for key in ("system", "plugins"):
        if key in changed:
            raise EvalNamespaceError(f"eval_namespace_violation:{key}")
    merged = dict(existing)
    system = dict(merged.get("system") or {})
    plugins = {
        str(owner): dict(content)
        for owner, content in (merged.get("plugins") or {}).items()
        if isinstance(content, Mapping)
    }
    for key in removed:
        system.pop(key, None)
        for content in plugins.values():
            content.pop(key, None)
    for key, value in changed.items():
        owners = [owner for owner, content in plugins.items() if key in content]
        system.pop(key, None)
        for owner in owners:
            plugins[owner].pop(key, None)
        if len(owners) == 1:
            plugins[owners[0]][key] = value
        else:
            system[key] = value
    merged["system"] = system
    merged["plugins"] = plugins
    return merged


def eval_results_canonical(eval_results: Mapping[str, Any]) -> str:
    """Deterministic compact canonical JSON of an eval_results mapping."""
    return json.dumps(eval_results, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def eval_results_digest(eval_results: Mapping[str, Any]) -> str:
    return hashlib.sha256(eval_results_canonical(eval_results).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Verified receipt envelope（G1-15，R4 冻结）
#
# idempotent 恢复对账只接受该精确闭集 envelope：stable key（execution_id）、
# run/node/spec/input/classification 绑定字段、output 与其 result_digest。
# 任何字段缺失/多余/错配都在 checkpoint 提交前 fail closed。
# ---------------------------------------------------------------------------

EXECUTION_RECEIPT_SCHEMA = "execution-receipt-v1"
EXECUTION_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "execution_id",
        "run_id",
        "node_id",
        "spec_digest",
        "input_digest",
        "classification_digest",
        "output",
        "result_digest",
    }
)


# ---------------------------------------------------------------------------
# Actual registry 执行分类盘点（G1-25，R3 显式冻结）
#
# 覆盖当前仓内生产与 pilot 注册表可达的 registry key：每条目显式给出闭集
# execution_class、runtime_kind 与可达实现符号（符号经专项测试 cross-check
# 与实际注册工厂一致）；digest 由 adapter_metadata_digest 绑定，任何
# class/symbol/version/key 漂移都会改变 digest。未知 handler 不在此表：
# runtime 对未知分类在 handler 前安全停止，无隐式默认。
# link_protocol="missing" 的条目是 §13 外未实现显式关联协议的 Agent
# adapter（scope blocker，见 k3-r3-result.md）。
# ---------------------------------------------------------------------------

ACTUAL_REGISTRY_CLASSIFICATIONS: dict[str, dict[str, str]] = {
    # Graph — Agent adapters（外部发送语义；关联协议状态逐条标注）
    "agent.summarizer": {
        "runtime_kind": "graph",
        "execution_class": "external_non_idempotent",
        "implementation_symbol": (
            "graph_runtime.agent_adapter:ControlledAgentAdapter.handler"
        ),
        "link_protocol": "agent-execution-link-v1",
    },
    "agent.live_drafter": {
        "runtime_kind": "graph",
        "execution_class": "external_non_idempotent",
        "implementation_symbol": (
            "graph_runtime.agent_live_pilot:LivePilotAdapter.handler"
        ),
        "link_protocol": "agent-execution-link-v1",
    },
    # Graph — pilot fixture adapters（agent_pilot，确定性，零模型零网络）
    "fixture.pilot_input": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.agent_pilot:make_pilot_fixture_adapters.<locals>.pilot_input"
        ),
    },
    "fixture.call_preparation": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.agent_pilot:"
            "make_pilot_fixture_adapters.<locals>.call_preparation"
        ),
    },
    "fixture.pilot_output_validator": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.agent_pilot:"
            "make_pilot_fixture_adapters.<locals>.pilot_output_validator"
        ),
    },
    # Graph — pilot2b fixture adapters（确定性，零模型零网络）
    "fixture.pilot2b_input": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.agent_live_pilot:"
            "make_pilot2b_fixture_adapters.<locals>.pilot_input"
        ),
    },
    "fixture.pilot2b_preparation": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.agent_live_pilot:"
            "make_pilot2b_fixture_adapters.<locals>.call_preparation"
        ),
    },
    "fixture.pilot2b_validator": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.agent_live_pilot:"
            "make_pilot2b_fixture_adapters.<locals>.output_validator"
        ),
    },
    # Graph — governed research fixture adapters（本地确定性，无网络发送）
    "research.input": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.input_adapter"
        ),
    },
    "research.official_fetch": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:"
            "research_adapters.<locals>.fetch_adapter.<locals>.handler"
        ),
    },
    "research.community_fetch": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:"
            "research_adapters.<locals>.fetch_adapter.<locals>.handler"
        ),
    },
    "research.synthesis_preparation": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.preparation_adapter"
        ),
    },
    "research.authorization_check": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.authorization_check_adapter"
        ),
    },
    "research.branch_marker": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.branch_marker_adapter"
        ),
    },
    "research.evidence_synthesizer": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.synthesis_adapter"
        ),
    },
    "research.output_validator": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.validator_adapter"
        ),
    },
    # Graph — 自主闭环宏 v3 fixture adapters（TASK E；本地确定性，无网络发送）
    "research.macro.input": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.macro_input_adapter"
        ),
    },
    "research.macro.evidence_collection": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.macro_collect_adapter"
        ),
    },
    "research.macro.coverage_check": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.macro_coverage_adapter"
        ),
    },
    "research.macro.judgment": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.macro_judge_adapter"
        ),
    },
    "research.macro.authorization_check": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.macro_authorization_check_adapter"
        ),
    },
    "research.macro.asset_production": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.research_bridge:research_adapters.<locals>.macro_produce_adapter"
        ),
    },
    # Loop — fragment_loop 生产注册 handler
    "cognitive_contract": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": (
            "fragment_loop.cognitive_loop:_CognitiveLoopCore._validate_contract"
        ),
    },
    "local_organize": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": (
            "fragment_loop.minimum_value:"
            "MinimumValueService._local_organize_handler"
        ),
    },
    "draft_generation": {
        "runtime_kind": "loop",
        "execution_class": "external_non_idempotent",
        "implementation_symbol": (
            "fragment_loop.minimum_value:"
            "MinimumValueService._draft_generation_handler"
        ),
    },
    "execute": {
        "runtime_kind": "loop",
        "execution_class": "external_non_idempotent",
        "implementation_symbol": (
            "fragment_loop.intent_service:"
            "FragmentIntentService.run_execution.<locals>.execute"
        ),
    },
    "harvest": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": (
            "fragment_loop.intent_service:"
            "FragmentIntentService.run_execution.<locals>.harvest"
        ),
    },
    # Graph — pilot_execution 生产链（graph_runtime/pilot_adapters.py）
    "pilot.candidate_input": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": "graph_runtime.pilot_adapters:candidate_input",
    },
    "pilot.draft_preparation": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": "graph_runtime.pilot_adapters:draft_preparation",
    },
    "pilot.agent_drafter": {
        "runtime_kind": "graph",
        "execution_class": "external_non_idempotent",
        "implementation_symbol": (
            "graph_runtime.pilot_adapters:PilotAgentDrafter.handler"
        ),
        "link_protocol": "agent-execution-link-v1",
    },
    "pilot.output_validator": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": "graph_runtime.pilot_adapters:output_validator",
    },
    # Graph — fragment_cognitive fixture adapters（合成确定性，零模型零网络）
    "fixture.input_fragment": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.input_fragment"
        ),
    },
    "fixture.semantic_expansion": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.semantic_expansion"
        ),
    },
    "fixture.route_decision": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.route_decision"
        ),
    },
    "fixture.memory_context": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.memory_context"
        ),
    },
    "fixture.source_fetch": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.source_fetch"
        ),
    },
    "fixture.claim_mapping": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.claim_mapping"
        ),
    },
    "fixture.research_questions": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.research_questions"
        ),
    },
    "fixture.direct_perspective": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.direct_perspective"
        ),
    },
    "fixture.personal_perspective": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.personal_perspective"
        ),
    },
    "fixture.calibration_v2": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.calibration_v2"
        ),
    },
    "fixture.synthesis_v1": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.synthesis_v1"
        ),
    },
    "fixture.synthetic_output": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.synthetic_output"
        ),
    },
    "fixture.source_verification": {
        "runtime_kind": "graph",
        "execution_class": "pure",
        "implementation_symbol": (
            "graph_runtime.fragment_cognitive:"
            "make_fragment_cognitive_adapters.<locals>.source_verification"
        ),
    },
    # Loop — canary/seed 确定性 handler（零模型零副作用）
    "intake": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": "scripts.p3d_gate2_canary:_handlers.<locals>.intake",
    },
    "worker": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": "scripts.p3d_gate2_canary:_handlers.<locals>.worker",
    },
    "evaluator": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": (
            "scripts.p3d_gate2_canary:_handlers.<locals>.evaluator"
        ),
    },
    "feedback": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": (
            "scripts.p3d_gate2_canary:_handlers.<locals>.feedback"
        ),
    },
    "noop": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": "scripts.p2c_gate2_canary:noop",
    },
    "first": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": "scripts.p2c_seed:first",
    },
    "second": {
        "runtime_kind": "loop",
        "execution_class": "pure",
        "implementation_symbol": "scripts.p2c_seed:second",
    },
}
