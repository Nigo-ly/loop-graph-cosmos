"""Shared fragment alignment, lightweight execution, continuation and harvest.

The service is dormant unless explicitly injected into the loopback 5684
server.  It never reads Vault files, calls a model, uses the network, or
creates a Graph run by itself.  Source loading, intent resolution, and the
direct/verify capabilities are injected adapters.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, cast

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore, checkpoint_compat_view, utc_now
from common.execution import merge_runtime_eval_results, plugin_eval, translate_view_edit
from common.supervisor import LoopSpec, LoopSupervisor, SupervisorContext
from common.types import LoopResult
from fragment_loop.cognitive_retrieval import is_public_https_locator
from fragment_loop.governed_research import (
    AUTO_DECISION_SOURCE,
    RUN_DEADLINE_SECONDS,
    GovernedResearchRunner,
    GovernedResearchVerifyAdapter,
    bounded_research_goal,
    research_progress_projection,
)
from fragment_loop.subscription_research import MAX_ROUNDS as MAX_SUBSCRIPTION_ROUNDS
from fragment_loop.subscription_research import MAX_TOOLS as MAX_SUBSCRIPTION_TOOLS
from fragment_loop.subscription_research import digest as research_result_digest
from graph_runtime.specs.fragment_research_macro_v3 import (
    RESEARCH_MACRO_V3_GRAPH_ID,
    RESEARCH_MACRO_V3_SPEC_DIGEST,
)

ALIGNMENT_VERSION = "fragment-intent-alignment-v1"
EXECUTION_VERSION = "fragment-intent-execution-v1"
ALIGNMENT_HEADER = "X-Fragment-Alignment"
ALIGNMENT_DECISION_HEADER = "X-Fragment-Alignment-Decision"
ALIGNMENT_ESCALATION_HEADER = "X-Fragment-Alignment-Escalation"
EPISODE_CONTINUATION_HEADER = "X-Fragment-Episode-Continuation"
ALIGNMENTS_PATH = "/fragment/v1/alignments"
EPISODES_PATH = "/fragment/v1/episodes"
CASES_PATH = "/fragment/v1/cases"

ALIGNMENT_SPEC = LoopSpec(
    loop_id=ALIGNMENT_VERSION,
    version="1.0.0",
    goal="与用户对齐碎片处理目的、方向、范围和预计结果",
    first_node="alignment",
    nodes=("alignment",),
    max_iterations=1,
    max_seconds=60,
    token_limit=1,
    tool_call_limit=1,
    worker_version="injected-intent-resolver-v1",
    evaluator_version="fragment-alignment-contract-v1",
)

EXECUTION_SPEC = LoopSpec(
    loop_id=EXECUTION_VERSION,
    version="1.0.0",
    goal="按已确认范围以最短可靠路径处理碎片并检查经验收获",
    first_node="execute",
    nodes=("execute", "harvest"),
    max_iterations=2,
    max_seconds=5 * 60,
    token_limit=8_000,
    tool_call_limit=8,
    worker_version="injected-fragment-capability-v1",
    evaluator_version="fragment-intent-result-contract-v1",
)

STABLE_INTENTS = frozenset(
    {
        "save",
        "verify",
        "learn",
        "evaluate_relevance",
        "explore",
        "deploy_or_build",
        "plan_action",
        "track",
    }
)
ROUTES = frozenset({"save_only", "direct", "verify", "graph"})
MATURITY = frozenset({"candidate", "qualified", "reusable"})
EVENT_ROLES = frozenset(
    {
        "intent",
        "plan",
        "evidence",
        "fact",
        "decision",
        "action",
        "result",
        "failure",
        "correction",
        "lesson",
        "continuation",
    }
)
QUALIFICATION_BASIS = frozenset(
    {"user_confirmed", "source_verified", "practice_validated"}
)
_FRAGMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

SourceLoader = Callable[[str, str], Mapping[str, object]]
IntentResolver = Callable[[Mapping[str, object]], Mapping[str, object]]
CapabilityAdapter = Callable[[Mapping[str, object]], Mapping[str, object]]


class FragmentIntentError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text(value: object, field: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise FragmentIntentError(f"{field}_invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise FragmentIntentError(f"{field}_invalid")
    cleaned = " ".join(value.split())
    if (not cleaned and not empty) or len(cleaned) > limit:
        raise FragmentIntentError(f"{field}_invalid")
    return cleaned


def _string_list(value: object, field: str, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise FragmentIntentError(f"{field}_invalid")
    return [_text(item, field, item_limit) for item in value]


def _validate_source(value: Mapping[str, object], input_digest: str) -> dict[str, object]:
    required = {"title", "literal_summary", "memory_basis", "input_digest"}
    if (
        not required
        <= set(value)
        <= required | {"nigo_loop", "source_seed_url", "organized_at", "goal", "source_origin"}
    ):
        raise FragmentIntentError("source_invalid")
    if value.get("input_digest") != input_digest:
        raise FragmentIntentError("source_changed")
    origin = value.get("source_origin")
    if origin is not None and origin != "raw_capture":
        raise FragmentIntentError("source_invalid")
    raw_goal = value.get("goal")
    if origin == "raw_capture" and (
        not isinstance(raw_goal, str) or not raw_goal.strip() or len(raw_goal) > 20000
        or any((ord(char) < 0x20 and char not in "\n\t") or ord(char) == 0x7F
               for char in raw_goal)
    ):
        raise FragmentIntentError("goal_invalid")
    # organized_at 仅用于自动承接的 cutover 判断，必须是短 ISO 字符串；
    # 不透传到 alignment 投影，不参与任何信任决策。
    organized_at = value.get("organized_at", None)
    if organized_at is not None and (
        not isinstance(organized_at, str) or len(organized_at) > 40
    ):
        raise FragmentIntentError("source_invalid")
    # nigo_loop 资格事实（TASK D rev2）：仅严格 True 才保留；出现但非
    # True 是 loader 模糊传值，失败关闭。旧 loader 缺失该键一律兼容，
    # 对应 alignment 默认不自动。
    nigo_loop = value.get("nigo_loop", None)
    if nigo_loop is not None and nigo_loop is not True:
        raise FragmentIntentError("source_invalid")
    # source_seed_url（rev14/15）：必须复用既有 public HTTPS/SSRF 校验
    # （私网/userinfo/fragment 拒绝），且拒绝仍含 query/fragment 的非
    # 规范 seed；bridge 产生的规范 URL（scheme/host/path）继续通过。
    seed_url = value.get("source_seed_url", None)
    if seed_url is not None:
        if not isinstance(seed_url, str) or not is_public_https_locator(seed_url):
            raise FragmentIntentError("source_invalid")
        parsed_seed = urllib.parse.urlsplit(seed_url)
        if parsed_seed.query or parsed_seed.fragment:
            raise FragmentIntentError("source_invalid")
    basis = value.get("memory_basis")
    if not isinstance(basis, list) or len(basis) > 3:
        raise FragmentIntentError("source_invalid")
    safe_basis: list[dict[str, str]] = []
    for item in basis:
        if not isinstance(item, Mapping) or set(item) != {"label", "maturity"}:
            raise FragmentIntentError("source_invalid")
        maturity = item.get("maturity")
        if maturity not in MATURITY:
            raise FragmentIntentError("source_invalid")
        safe_basis.append(
            {
                "label": _text(item.get("label"), "source", 120),
                "maturity": str(maturity),
            }
        )
    return {
        "title": _text(value.get("title"), "source", 120),
        "literal_summary": _text(value.get("literal_summary"), "source", 800),
        "memory_basis": safe_basis,
        **({"goal": raw_goal if origin == "raw_capture" else _text(value["goal"], "goal", 500)}
           if "goal" in value else {}),
        **({"source_origin": origin} if origin is not None else {}),
        "input_digest": input_digest,
        **({"nigo_loop": True} if nigo_loop is True else {}),
        **({"source_seed_url": seed_url} if isinstance(seed_url, str) else {}),
    }


def _validate_scope(value: object) -> dict[str, object]:
    legacy_keys = {
        "capabilities",
        "external_scope",
        "model_call_cap",
        "cost_cap_cny",
        "side_effect",
    }
    policy_keys = {"model_provider", "model_name", "write_scope"}
    if not isinstance(value, Mapping):
        raise FragmentIntentError("scope_invalid")
    keys = frozenset(value)
    if keys not in {frozenset(legacy_keys), frozenset(legacy_keys | policy_keys)}:
        raise FragmentIntentError("scope_invalid")
    model_cap = value.get("model_call_cap")
    cost_cap = value.get("cost_cap_cny")
    if isinstance(model_cap, bool) or not isinstance(model_cap, int) or not 0 <= model_cap <= 8:
        raise FragmentIntentError("scope_invalid")
    if (
        isinstance(cost_cap, bool)
        or not isinstance(cost_cap, (int, float))
        or not 0 <= float(cost_cap) <= 100
    ):
        raise FragmentIntentError("scope_invalid")
    model_provider = value.get("model_provider", "")
    model_name = value.get("model_name", "")
    write_scope = value.get("write_scope", [])
    if not isinstance(model_provider, str) or not isinstance(model_name, str):
        raise FragmentIntentError("scope_invalid")
    if keys == frozenset(legacy_keys | policy_keys):
        if model_cap == 0 and (model_provider or model_name):
            raise FragmentIntentError("scope_invalid")
        if model_cap > 0 and (not model_provider or not model_name):
            raise FragmentIntentError("scope_invalid")
    return {
        "capabilities": _string_list(value.get("capabilities"), "scope", limit=8, item_limit=64),
        "external_scope": _string_list(
            value.get("external_scope"), "scope", limit=8, item_limit=120
        ),
        "model_call_cap": model_cap,
        "cost_cap_cny": float(cost_cap),
        "side_effect": _text(value.get("side_effect"), "scope", 80),
        "model_provider": _text(model_provider, "scope", 40, empty=True),
        "model_name": _text(model_name, "scope", 80, empty=True),
        "write_scope": _string_list(write_scope, "scope", limit=8, item_limit=80),
    }


def _validate_proposal(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) != {
        "suggested_intents",
        "dynamic_intents",
        "reasoning",
        "plan",
        "expected_result",
        "exclusions",
        "recommended_route",
        "execution_scope",
    }:
        raise FragmentIntentError("resolver_invalid")
    intents = _string_list(
        value.get("suggested_intents"), "intents", limit=8, item_limit=40
    )
    if not intents or any(intent not in STABLE_INTENTS for intent in intents):
        raise FragmentIntentError("resolver_invalid")
    dynamic = value.get("dynamic_intents")
    if not isinstance(dynamic, list) or len(dynamic) > 3:
        raise FragmentIntentError("resolver_invalid")
    safe_dynamic: list[dict[str, str]] = []
    seen_dynamic: set[str] = set()
    for item in dynamic:
        if not isinstance(item, Mapping) or set(item) != {"id", "label", "basis"}:
            raise FragmentIntentError("resolver_invalid")
        item_id = _text(item.get("id"), "dynamic_intent", 40)
        if item_id in STABLE_INTENTS or item_id in seen_dynamic:
            raise FragmentIntentError("resolver_invalid")
        seen_dynamic.add(item_id)
        safe_dynamic.append(
            {
                "id": item_id,
                "label": _text(item.get("label"), "dynamic_intent", 80),
                "basis": _text(item.get("basis"), "dynamic_intent", 160),
            }
        )
    route = value.get("recommended_route")
    if route not in ROUTES:
        raise FragmentIntentError("resolver_invalid")
    return {
        "suggested_intents": intents,
        "dynamic_intents": safe_dynamic,
        "reasoning": _text(value.get("reasoning"), "reasoning", 300),
        "plan": _text(value.get("plan"), "plan", 500),
        "expected_result": _text(value.get("expected_result"), "expected_result", 300),
        "exclusions": _string_list(value.get("exclusions"), "exclusions", limit=8, item_limit=120),
        "recommended_route": str(route),
        "execution_scope": _validate_scope(value.get("execution_scope")),
    }


def _validate_result(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) != {
        "summary",
        "unknowns",
        "next_checks",
        "needs_escalation",
        "escalation_reason",
        "model_calls",
        "tool_calls",
        "harvest",
    }:
        raise FragmentIntentError("result_invalid")
    if not isinstance(value.get("needs_escalation"), bool):
        raise FragmentIntentError("result_invalid")
    model_calls = value.get("model_calls")
    tool_calls = value.get("tool_calls")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in (model_calls, tool_calls)
    ):
        raise FragmentIntentError("result_invalid")
    harvest = value.get("harvest")
    if not isinstance(harvest, list) or len(harvest) > 8:
        raise FragmentIntentError("result_invalid")
    safe_harvest: list[dict[str, object]] = []
    for item in harvest:
        if not isinstance(item, Mapping):
            raise FragmentIntentError("result_invalid")
        base_keys = {
            "role",
            "summary",
            "maturity",
            "qualification_basis",
        }
        maturity = item.get("maturity")
        reusable_keys = {
            "evidence",
            "applicability",
            "unknowns",
            "invalidates_when",
            "review_trigger",
            "relation",
        }
        expected_keys = base_keys | reusable_keys if maturity == "reusable" else base_keys
        if set(item) != expected_keys:
            raise FragmentIntentError("result_invalid")
        qualification = item.get("qualification_basis")
        if maturity not in MATURITY:
            raise FragmentIntentError("result_invalid")
        if maturity in {"qualified", "reusable"}:
            if qualification not in QUALIFICATION_BASIS:
                raise FragmentIntentError("result_invalid")
        elif qualification is not None:
            raise FragmentIntentError("result_invalid")
        role = _text(item.get("role"), "harvest", 40)
        if role not in EVENT_ROLES:
            raise FragmentIntentError("result_invalid")
        safe_item: dict[str, object] = {
                "role": role,
                "summary": _text(item.get("summary"), "harvest", 500),
                "maturity": maturity,
                "qualification_basis": qualification,
        }
        if maturity == "reusable":
            relation = item.get("relation")
            if relation not in {"new", "duplicates", "supplements", "conflicts", "supersedes"}:
                raise FragmentIntentError("result_invalid")
            safe_item.update(
                {
                    "evidence": _string_list(
                        item.get("evidence"), "harvest", limit=8, item_limit=200
                    ),
                    "applicability": _text(item.get("applicability"), "harvest", 300),
                    "unknowns": _string_list(
                        item.get("unknowns"), "harvest", limit=8, item_limit=200
                    ),
                    "invalidates_when": _text(
                        item.get("invalidates_when"), "harvest", 300
                    ),
                    "review_trigger": _text(
                        item.get("review_trigger"), "harvest", 300
                    ),
                    "relation": relation,
                }
            )
        safe_harvest.append(safe_item)
    return {
        "summary": _text(value.get("summary"), "result", 1000),
        "unknowns": _string_list(value.get("unknowns"), "result", limit=8, item_limit=300),
        "next_checks": _string_list(value.get("next_checks"), "result", limit=8, item_limit=300),
        "needs_escalation": value["needs_escalation"],
        "escalation_reason": _text(value.get("escalation_reason"), "result", 300, empty=True),
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "harvest": safe_harvest,
    }


def _alignment_id(fragment_id: str, input_digest: str) -> str:
    return f"align:{_digest([ALIGNMENT_VERSION, fragment_id, input_digest])[:24]}"


def _case_id(fragment_id: str) -> str:
    identity = ["fragment-case-v1", fragment_id]
    return f"case:{_digest(identity)[:24]}"


def _episode_id(alignment_id: str) -> str:
    identity = ["fragment-episode-v1", alignment_id]
    return f"episode:{_digest(identity)[:24]}"


def _execution_id(alignment_digest: str) -> str:
    identity = [EXECUTION_VERSION, alignment_digest]
    return f"exec:fragment-intent:{_digest(identity)[:24]}"


def continuation_id(
    parent_episode_id: str, parent_run_id: str, result_digest: str, goal: str
) -> str:
    identity = [
        "fragment-continuation-v1",
        parent_episode_id,
        parent_run_id,
        result_digest,
        goal,
    ]
    return f"continuation:{_digest(identity)[:24]}"


def escalation_id(alignment_id: str, result_digest: str) -> str:
    return f"escalation:{_digest(['fragment-escalation-v1', alignment_id, result_digest])[:24]}"


# -- 研究投影辅助（只读、只来自 Checkpoint；任何漂移诚实缺省，绝不抛错） ------

_RESEARCH_EVIDENCE_MARKERS = frozenset(
    {"inherited", "newly_collected", "omitted", "stale"}
)
_RESEARCH_RELATIONS = frozenset(
    {"supports", "partially_supports", "conflicts", "irrelevant"}
)
_EVIDENCE_BUNDLE_VERSION = "fragment-research-evidence-bundle-v1"


def _projection_text(value: object, limit: int) -> str | None:
    """投影级安全文本：合法返回原文，任何漂移返回 None（调用方诚实缺省）。"""
    if not isinstance(value, str):
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    if not value.strip() or len(value) > limit:
        return None
    return value


def _projection_id_list(value: object, *, minimum: int, maximum: int) -> list[str] | None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        return None
    items: list[str] = []
    for item in value:
        text = _projection_text(item, 80)
        if text is None:
            return None
        items.append(text)
    return items


def _project_research_result_extras(stored: object) -> dict[str, object]:
    """九段映射附加字段（§3.9 形状）：只承认验证通过的合成结果，失败关闭缺省。"""
    if not isinstance(stored, Mapping) or stored.get("status") != "synthesized":
        return {}
    payload = stored.get("result")
    if not isinstance(payload, Mapping):
        return {}
    if stored.get("provider") in ("codex_subscription", "kimi_subscription"):
        return {key: payload[key] for key in (
            "summary", "recommendation", "confirmed", "conflicts", "claims",
            "answer_markdown", "coverage", "agent_usage", "topic", "unknowns"
        ) if key in payload}
    extras: dict[str, object] = {}
    recommendation = _projection_text(payload.get("recommendation"), 400)
    if recommendation is None:
        return {}
    extras["recommendation"] = recommendation
    confirmed = payload.get("confirmed")
    if not isinstance(confirmed, list) or len(confirmed) > 8:
        return {}
    safe_confirmed: list[dict[str, object]] = []
    for item in confirmed:
        if not isinstance(item, Mapping):
            return {}
        claim = _projection_text(item.get("claim"), 200)
        ids = _projection_id_list(item.get("evidence_ids"), minimum=1, maximum=4)
        if claim is None or ids is None:
            return {}
        safe_confirmed.append({"claim": claim, "evidence_ids": ids})
    extras["confirmed"] = safe_confirmed
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list) or len(conflicts) > 4:
        return {}
    safe_conflicts: list[dict[str, object]] = []
    for item in conflicts:
        if not isinstance(item, Mapping):
            return {}
        topic = _projection_text(item.get("topic"), 100)
        dimensions = _projection_id_list(item.get("dimensions"), minimum=1, maximum=4)
        ids = _projection_id_list(item.get("evidence_ids"), minimum=1, maximum=4)
        if topic is None or dimensions is None or ids is None:
            return {}
        safe_conflicts.append(
            {"topic": topic, "dimensions": dimensions, "evidence_ids": ids}
        )
    extras["conflicts"] = safe_conflicts
    claims = payload.get("claims")
    if not isinstance(claims, list) or len(claims) > 16:
        return {}
    safe_claims: list[dict[str, object]] = []
    for item in claims:
        if not isinstance(item, Mapping):
            return {}
        claim = _projection_text(item.get("claim"), 200)
        evidence_id = _projection_text(item.get("evidence_id"), 80)
        relation = item.get("relation")
        if claim is None or evidence_id is None or relation not in _RESEARCH_RELATIONS:
            return {}
        safe_claims.append(
            {"claim": claim, "evidence_id": evidence_id, "relation": relation}
        )
    extras["claims"] = safe_claims
    return extras


def _project_research_evidence(raw: object) -> list[dict[str, object]] | None:
    """证据记录安全投影：只带安全字段（不投影正文），非法记录整条剔除。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    safe: list[dict[str, object]] = []
    for record in raw[:16]:
        if not isinstance(record, Mapping):
            continue
        title = _projection_text(record.get("title"), 200)
        url = _projection_text(record.get("url"), 300)
        marker = record.get("marker")
        if title is None or url is None or marker not in _RESEARCH_EVIDENCE_MARKERS:
            continue
        projected: dict[str, object] = {
            "title": title,
            "url": url,
            "marker": marker,
        }
        source_target = record.get("source_target")
        if source_target is not None:
            text = _projection_text(source_target, 80)
            if text is None:
                continue
            projected["source_target"] = text
        evidence_id = record.get("evidence_id")
        if evidence_id is not None:
            text = _projection_text(evidence_id, 80)
            if text is None:
                continue
            projected["evidence_id"] = text
        digest = record.get("evidence_digest")
        if digest is not None:
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                continue
            projected["evidence_digest"] = digest
        safe.append(projected)
    return safe


def _evidence_bundle_digest(records: list[dict[str, object]]) -> str | None:
    """证据束摘要：active 记录 digest 的确定性摘要；无有效 digest 即 None。

    S5：只纳入 marker ∈ {inherited, newly_collected}，与 Research Creation
    Bridge 的 ``active_bundle_records`` 逐字对齐——stale/omitted 记录绝不
    计入 bundle，否则含 stale 的 episode 升级必被判 evidence_bundle_drift。
    """
    digests = sorted(
        str(record["evidence_digest"])
        for record in records
        if record.get("marker") in ("inherited", "newly_collected")
        and "evidence_digest" in record
    )
    if not digests:
        return None
    # 与 Research Creation Bridge 的冻结契约逐字一致：版本域后直接展开
    # 排序后的 evidence_digest。这里若多包一层列表，UI 投影出的提案会被
    # Bridge 如实判为 evidence_bundle_drift，导致所有真实升级都无法创建。
    return _digest([_EVIDENCE_BUNDLE_VERSION, *digests])


def _project_graph_escalation(
    stored: object,
    binding: object,
    result_digest: object,
    evidence_records: list[dict[str, object]] | None,
) -> dict[str, object] | None:
    """升级提案投影：材料字段（spec/bundle/escalation）齐全才附，缺一失败关闭。

    提案本身保持「未获准执行」状态原样投影；绑定漂移或证据束不可摘要时
    只投影 status/reason/source_result_digest，绝不出现半成品材料。
    spec_id/spec_digest 从 spec 模块导入，不硬编码。
    """
    if stored is None:
        return None
    if not isinstance(stored, Mapping):
        return None
    proposal: dict[str, object] = {
        key: stored[key]
        for key in ("status", "reason", "source_result_digest")
        if key in stored
    }
    alignment_id = binding.get("alignment_id") if isinstance(binding, Mapping) else None
    bundle = _evidence_bundle_digest(evidence_records or [])
    if (
        not isinstance(alignment_id, str)
        or not alignment_id
        or not isinstance(result_digest, str)
        or _SHA256.fullmatch(result_digest) is None
        or stored.get("source_result_digest") != result_digest
        or bundle is None
    ):
        # 绑定漂移/材料不齐：失败关闭，只投影提案状态本体。
        return proposal
    return {
        **proposal,
        # rev6 P0-1：新升级提案端到端切换到宏 v3（真实创建链）；旧 v1
        # 提案/run 由桥按请求中冻结的 id/digest 走旧路径只读重放。
        "spec_id": RESEARCH_MACRO_V3_GRAPH_ID,
        "spec_digest": RESEARCH_MACRO_V3_SPEC_DIGEST,
        "evidence_bundle_digest": bundle,
        "escalation_id": escalation_id(alignment_id, result_digest),
    }


def _route_for(intents: list[str], recommended: str) -> str:
    if intents == ["save"]:
        return "save_only"
    if any(intent in {"verify", "deploy_or_build"} for intent in intents):
        return "verify"
    if recommended == "graph":
        return "graph"
    return "direct"


class FragmentIntentService:
    """Checkpoint-backed alignment and the smallest direct/verify execution."""

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        source_loader: SourceLoader,
        resolver: IntentResolver,
        *,
        direct_adapter: CapabilityAdapter | None = None,
        verify_adapter: CapabilityAdapter | None = None,
        continuation_bridge: Any | None = None,
    ) -> None:
        self.store = store
        self.source_loader = source_loader
        self.resolver = resolver
        self.direct_adapter = direct_adapter
        self.verify_adapter = verify_adapter
        # rev10：旧离线零来源执行恢复对当前 raw frontmatter 的重验证桥；
        # None 时恢复路径全程零动作（无桥不猜资格）。
        self.continuation_bridge = continuation_bridge
        # 自动承接最近一轮逐条结果（内存投影，非权威状态）：供展示层如实
        # 呈现每条碎片的承接 outcome/reason；重启为空，首轮扫描后重建。
        self.last_auto_propose_report: list[dict[str, str]] = []
        self._auto_propose_cursor = 0
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, identity: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(identity, threading.Lock())

    def list_alignments(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for run_id in self.store.run_ids_with_prefix(ALIGNMENT_SPEC.loop_id):
            found = self.store.latest_with_sequence(run_id)
            if found is None:
                continue
            checkpoint, sequence = found
            if checkpoint.loop_id != ALIGNMENT_SPEC.loop_id:
                continue
            rows.append(self._project(checkpoint, sequence))
        return sorted(rows, key=lambda item: (str(item["updated_at"]), str(item["alignment_id"])))

    def propose(self, raw: object) -> tuple[int, dict[str, object]]:
        if not isinstance(raw, dict) or set(raw) != {"fragment_id", "input_digest", "requester"}:
            raise FragmentIntentError("invalid_body")
        fragment_id = raw.get("fragment_id")
        input_digest = raw.get("input_digest")
        if (
            not isinstance(fragment_id, str)
            or _FRAGMENT_ID.fullmatch(fragment_id) is None
            or not isinstance(input_digest, str)
            or _SHA256.fullmatch(input_digest) is None
            or raw.get("requester") != "nigo"
        ):
            raise FragmentIntentError("invalid_body")
        alignment_id = _alignment_id(fragment_id, input_digest)
        with self._lock(alignment_id):
            existing = self.store.latest_with_sequence(alignment_id)
            if existing is not None:
                return 200, self._project(*existing)
            source = _validate_source(self.source_loader(fragment_id, input_digest), input_digest)
            proposal = _validate_proposal(self.resolver(source))
            return self._commit_alignment(fragment_id, input_digest, source, proposal)

    def _commit_alignment(
        self,
        fragment_id: str,
        input_digest: str,
        source: Mapping[str, object],
        proposal: Mapping[str, object],
        *,
        lineage: Mapping[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        alignment_id = _alignment_id(fragment_id, input_digest)
        existing = self.store.latest_with_sequence(alignment_id)
        if existing is not None:
            return 200, self._project(*existing)
        alignment_digest = _digest(
            {
                "version": ALIGNMENT_VERSION,
                "fragment_id": fragment_id,
                "input_digest": input_digest,
                "source": source,
                "proposal": proposal,
                "lineage": lineage,
            }
        )
        checkpoint = LoopCheckpoint(
            loop_id=ALIGNMENT_SPEC.loop_id,
            loopspec_version=ALIGNMENT_SPEC.version,
            run_id=alignment_id,
            fragment_id=fragment_id,
            current_node="alignment",
            status="suggested",
            goal=ALIGNMENT_SPEC.goal,
            fragment_title=str(source["title"]),
            fragment_title_source=("raw_capture" if source.get("source_origin") == "raw_capture"
                                   else "organized_projection"),
            # Gate 1：登记材料属 runtime system 命名空间，无 flat 业务键。
            eval_results={
                "system": {
                    "alignment": {
                        "version": ALIGNMENT_VERSION,
                        "revision": 1,
                        "input_digest": input_digest,
                        "alignment_digest": alignment_digest,
                        "case_id": _case_id(fragment_id),
                        "episode_id": _episode_id(alignment_id),
                        "literal_summary": source["literal_summary"],
                        **({"goal": source["goal"]} if "goal" in source else {}),
                        **({"source_origin": source["source_origin"]}
                           if "source_origin" in source else {}),
                        "memory_basis": source["memory_basis"],
                        # TASK D rev2：来源资格事实随 alignment 持久化；
                        # 旧 alignment 无此键，默认不自动。
                        **({"nigo_loop": True} if source.get("nigo_loop") is True else {}),
                        # rev14：公开来源 seed candidate（已 SSRF 校验、去
                        # 查询参数）随 alignment 持久化，供执行期 goal 绑定。
                        **(
                            {"source_seed_url": source["source_seed_url"]}
                            if isinstance(source.get("source_seed_url"), str)
                            else {}
                        ),
                        **proposal,
                        **(dict(lineage) if lineage is not None else {}),
                    }
                },
                "plugins": {},
            },
            budget_limits={"iterations": 1, "seconds": 60, "tokens": 1, "tool_calls": 1},
            budget_used={"tokens": 0, "tool_calls": 0, "seconds": 0.0},
            worker_version=ALIGNMENT_SPEC.worker_version,
            evaluator_version=ALIGNMENT_SPEC.evaluator_version,
        )
        committed = self.store.compare_and_append(
            checkpoint,
            expected_sequence=0,
            event_type="alignment_proposed",
            revision_reason="continuation" if lineage is not None else "intent_resolved",
        )
        if committed is None:
            found = self.store.latest_with_sequence(alignment_id)
            if found is None:
                raise FragmentIntentError("alignment_conflict")
            return 200, self._project(*found)
        sequence = self.store.latest_sequence(alignment_id)
        if sequence is None:
            raise FragmentIntentError("alignment_state_lost")
        return 201, self._project(committed, sequence)

    def decide(self, alignment_id: str, raw: object) -> tuple[int, dict[str, object]]:
        if not isinstance(raw, dict) or set(raw) != {
            "alignment_id",
            "revision",
            "input_digest",
            "intents",
            "supplement",
            "action",
            "requester",
        }:
            raise FragmentIntentError("invalid_body")
        if raw.get("alignment_id") != alignment_id or raw.get("requester") != "nigo":
            raise FragmentIntentError("invalid_body")
        if raw.get("action") not in {"confirm", "save_only"}:
            raise FragmentIntentError("invalid_body")
        if isinstance(raw.get("revision"), bool) or not isinstance(raw.get("revision"), int):
            raise FragmentIntentError("invalid_body")
        input_digest = raw.get("input_digest")
        if not isinstance(input_digest, str) or _SHA256.fullmatch(input_digest) is None:
            raise FragmentIntentError("invalid_body")
        intents = _string_list(raw.get("intents"), "intents", limit=11, item_limit=40)
        supplement = _text(raw.get("supplement"), "supplement", 500, empty=True)
        if not intents or len(set(intents)) != len(intents):
            raise FragmentIntentError("intents_invalid")
        with self._lock(alignment_id):
            found = self.store.latest_raw_with_sequence(alignment_id)
            if found is None:
                raise FragmentIntentError("alignment_not_found")
            raw_checkpoint, sequence = found
            # 读取走 legacy_flat_v1 投影；写回基于 raw 存储形态。
            checkpoint = checkpoint_compat_view(raw_checkpoint)
            alignment = checkpoint.eval_results.get("alignment")
            if not isinstance(alignment, Mapping):
                raise FragmentIntentError("alignment_invalid")
            if checkpoint.status == "passed":
                decision = alignment.get("decision")
                expected = {
                    "action": raw["action"],
                    "intents": intents,
                    "supplement": supplement,
                }
                if decision != expected:
                    raise FragmentIntentError("decision_conflict")
                # A process may stop after the alignment decision and execution
                # registration are committed but before the approved Run starts.
                # An exact decision replay resumes only that untouched approved
                # Run; running, blocked and terminal Runs are never retried here.
                execution_run_id = alignment.get("execution_run_id")
                if isinstance(execution_run_id, str):
                    execution = self.store.latest(execution_run_id)
                    if execution is not None and execution.status == "approved":
                        self.run_execution(execution_run_id)
                    refreshed = self.store.latest_with_sequence(alignment_id)
                    if refreshed is None:
                        raise FragmentIntentError("alignment_state_lost")
                    checkpoint, sequence = refreshed
                return 200, self._project(checkpoint, sequence)
            if checkpoint.status != "suggested":
                raise FragmentIntentError("alignment_not_decidable")
            if (
                alignment.get("revision") != raw["revision"]
                or alignment.get("input_digest") != input_digest
            ):
                raise FragmentIntentError("alignment_changed")
            dynamic_ids = {
                str(item.get("id"))
                for item in alignment.get("dynamic_intents", [])
                if isinstance(item, Mapping)
            }
            if any(
                intent not in STABLE_INTENTS and intent not in dynamic_ids
                for intent in intents
            ):
                raise FragmentIntentError("intents_invalid")
            if raw["action"] == "save_only":
                intents = ["save"]
            route = _route_for(intents, str(alignment.get("recommended_route")))
            decision = {"action": raw["action"], "intents": intents, "supplement": supplement}
            confirmed_digest = _digest(
                {
                    "alignment_digest": alignment.get("alignment_digest"),
                    "decision": decision,
                    "execution_scope": alignment.get("execution_scope"),
                }
            )
            execution_run_id = (
                None
                if route in {"save_only", "graph"}
                else _execution_id(confirmed_digest)
            )
            updated_alignment = {
                **alignment,
                "decision": decision,
                "route": route,
                "decision_source": "human",
                "confirmed_digest": confirmed_digest,
                "execution_run_id": execution_run_id,
            }
            stop_reason = (
                "saved_only"
                if route == "save_only"
                else "graph_proposal_required"
                if route == "graph"
                else "alignment_confirmed"
            )
            committed = self.store.compare_and_append(
                replace(
                    raw_checkpoint,
                    status="passed",
                    eval_results=merge_runtime_eval_results(
                        existing=raw_checkpoint.eval_results,
                        content={"alignment": updated_alignment},
                    ),
                    stop_reason=stop_reason,
                ),
                expected_sequence=sequence,
                event_type="alignment_confirmed",
                revision_reason=route,
            )
            if committed is None:
                raise FragmentIntentError("decision_conflict")
            if execution_run_id is not None:
                self._ensure_execution(committed)
                self.run_execution(execution_run_id)
            final = self.store.latest_with_sequence(alignment_id)
            if final is None:
                raise FragmentIntentError("alignment_state_lost")
            return 201, self._project(*final)

    def get_alignment(self, alignment_id: str) -> dict[str, object]:
        self.evaluate_alignment_autonomy(alignment_id)
        found = self.store.latest_with_sequence(alignment_id)
        if found is None or found[0].loop_id != ALIGNMENT_SPEC.loop_id:
            raise FragmentIntentError("alignment_not_found")
        return self._project(*found)

    # -- 自主闭环钩子（TASK C/D：同进程、幂等、非调度器） ----------------------

    _AUTO_ALLOWED_SIDE_EFFECTS = frozenset({"none", "只读，无外部写入", "无"})
    # 英文标记统一小写后匹配（PRIVATE/KeyChain 等大小写变体不得绕过）；
    # 中文标记保持原文匹配（无大小写语义）。
    _AUTO_FORBIDDEN_MARKERS_EN = ("secret", "keychain", "private")
    _AUTO_FORBIDDEN_MARKERS_ZH = ("私人", "密钥", "凭据", "写入", "发布", "部署", "安装")

    def _auto_scope_allowed(self, scope: object) -> bool:
        """自动路线能力闭集（rev5 英文小写归一 + 中文原义；rev10 恢复路径
        复用）：零/只读副作用 + 能力与外部范围零私人/凭据/写/发布/部署/
        安装标记。"""
        if not isinstance(scope, Mapping):
            return False
        if str(scope.get("side_effect", "")) not in self._AUTO_ALLOWED_SIDE_EFFECTS:
            return False
        for key in ("capabilities", "external_scope"):
            items = scope.get(key)
            if not isinstance(items, (list, tuple)):
                return False
            for item in items:
                text = str(item)
                lowered = text.lower()
                if any(marker in lowered for marker in self._AUTO_FORBIDDEN_MARKERS_EN):
                    return False
                if any(marker in text for marker in self._AUTO_FORBIDDEN_MARKERS_ZH):
                    return False
        return True

    def _auto_route_eligible(self, alignment: Mapping[str, object]) -> bool:
        """普通公开碎片自动路线资格（确定性闭集，任一不符即需人工）：

        可验证的 nigo-loop 来源事实（intake frontmatter 标记经 bridge
        校验后显式传递；缺失/旧 alignment 一律默认不自动，绝不凭标题或
        文案猜测）+ 推荐 verify 路线 + 能力闭集。model_call_cap 不影响
        资格——自动路线只跑公开 collection，模型永远停在人类闸门。"""
        if alignment.get("nigo_loop") is not True:
            return False
        if str(alignment.get("recommended_route", "")) != "verify":
            return False
        return self._auto_scope_allowed(alignment.get("execution_scope"))

    def maybe_auto_advance(self, alignment_id: str) -> bool:
        """TASK D 安全自动路线推进：eligible 的 suggested alignment 以
        decision_source=system_policy 登记推荐路线并启动公开 collection。

        幂等（CAS + 确定性 execution_run_id）；模型、预算、出域、不可逆
        动作、正式资产发布仍全部停在人类闸门。"""
        with self._lock(alignment_id):
            found = self.store.latest_raw_with_sequence(alignment_id)
            if found is None:
                return False
            raw_checkpoint, sequence = found
            checkpoint = checkpoint_compat_view(raw_checkpoint)
            if checkpoint.status != "suggested":
                return False
            alignment = checkpoint.eval_results.get("alignment")
            if not isinstance(alignment, Mapping) or not self._auto_route_eligible(alignment):
                return False
            intents = [
                str(item)
                for item in alignment.get("suggested_intents", [])
                if str(item) in STABLE_INTENTS
            ]
            if not intents:
                return False
            decision = {"action": "confirm", "intents": intents, "supplement": ""}
            confirmed_digest = _digest(
                {
                    "alignment_digest": alignment.get("alignment_digest"),
                    "decision": decision,
                    "execution_scope": alignment.get("execution_scope"),
                }
            )
            execution_run_id = _execution_id(confirmed_digest)
            updated_alignment = {
                **alignment,
                "decision": decision,
                "route": "verify",
                "decision_source": AUTO_DECISION_SOURCE,
                "confirmed_digest": confirmed_digest,
                "execution_run_id": execution_run_id,
            }
            committed = self.store.compare_and_append(
                replace(
                    raw_checkpoint,
                    status="passed",
                    eval_results=merge_runtime_eval_results(
                        existing=raw_checkpoint.eval_results,
                        content={"alignment": updated_alignment},
                    ),
                    stop_reason="alignment_confirmed",
                ),
                expected_sequence=sequence,
                event_type="alignment_confirmed",
                revision_reason="system_policy",
            )
            if committed is None:
                return False
            self._ensure_execution(committed)
            self.run_execution(execution_run_id)
            return True

    def _research_runner(self) -> GovernedResearchRunner | None:
        runner = getattr(self.verify_adapter, "runner", None)
        return runner if isinstance(runner, GovernedResearchRunner) else None

    def _evaluate_research_due(self, execution_run_id: str) -> bool:
        """TASK C：watching 的 due 恢复——同进程、幂等、最多一次有界观察
        cycle；collection 能力关闭时绝不恢复。返回是否真实触发了 resume。"""
        runner = self._research_runner()
        if runner is None or not getattr(runner, "collection_enabled", False):
            return False
        if not runner.watch_due(execution_run_id):
            return False
        execution = self.store.latest(execution_run_id)
        if execution is None:
            return False
        goal = ""
        collection = execution.eval_results.get("research_collection")
        if isinstance(collection, Mapping) and isinstance(collection.get("goal"), str):
            goal = str(collection["goal"])
        if not goal:
            binding = execution.eval_results.get("execution_binding")
            if isinstance(binding, Mapping):
                goal = bounded_research_goal(
                    str(binding.get("title", "")), str(binding.get("supplement", ""))
                )
        if not goal:
            return False
        return runner.resume_due_watch(execution_run_id, goal) is not None

    def evaluate_alignment_autonomy(self, alignment_id: str) -> None:
        """自主钩子：get 读路径的 due 条件评估——先自动路线登记（仅 suggested
        且 eligible），再对执行 Run 做 watching due 恢复；全部幂等。"""
        if self.maybe_auto_advance(alignment_id):
            return
        found = self.store.latest_with_sequence(alignment_id)
        if found is None:
            return
        alignment = found[0].eval_results.get("alignment")
        if isinstance(alignment, Mapping):
            execution_run_id = alignment.get("execution_run_id")
            if isinstance(execution_run_id, str):
                self._evaluate_research_due(execution_run_id)

    def evaluate_due_watches(self) -> dict[str, int]:
        """TASK C rev3 自主触发边界：扫描全部 execution Run，只对已持久化
        且 due 的 watching 触发一次有界观察 cycle（幂等、并发单飞由 cycle
        CAS 领取保证）。不读取私人正文、不执行模型；collection 能力关闭
        时零副作用。返回扫描/恢复计数。"""
        scanned = 0
        resumed = 0
        for run_id in self.store.run_ids_with_prefix(EXECUTION_SPEC.loop_id):
            scanned += 1
            try:
                if self._evaluate_research_due(run_id):
                    resumed += 1
            except Exception:
                # 失败关闭：单个 Run 的异常绝不中断扫描循环。
                continue
        return {"scanned": scanned, "resumed": resumed}

    # -- suggested alignment 自动推进扫描（P1-2 修复） ---------------------------

    def advance_suggested_alignments(self) -> dict[str, int]:
        """suggested alignments 的幂等安全扫描（fragment-autonomy P1-2 修复）：
        逐个调用既有 maybe_auto_advance——资格闭集（nigo_loop 事实 + 推荐
        verify + 能力闭集）、per-alignment 锁、CAS 与确定性
        execution_run_id 幂等全部不变；单项异常失败关闭、绝不中断扫描。
        不读取私人正文、不执行模型、不新增线程或调度器；模型、预算、
        出域、不可逆动作仍全部停在人类闸门。返回扫描/推进计数。"""
        scanned = 0
        advanced = 0
        for alignment_id in self.store.run_ids_with_prefix(ALIGNMENT_SPEC.loop_id):
            scanned += 1
            try:
                if self.maybe_auto_advance(alignment_id):
                    advanced += 1
            except Exception:
                # 失败关闭：单条 alignment 的异常绝不中断扫描。
                continue
        return {"scanned": scanned, "advanced": advanced}

    # -- 旧离线零来源执行恢复（rev10） -----------------------------------------

    def recover_offline_executions(self) -> dict[str, int]:
        """旧离线零来源执行恢复（rev10）：append-only、幂等、并发
        single-flight。仅处理 research_collection.stop_reason ∈
        {live_disabled, capability_unavailable} 且 searches=fetches=0 的
        旧 terminal execution；经 ContinuationBridge 对当前 raw
        frontmatter 重新验证 nigo-loop:true、当前来源绑定未漂移后，创建
        确定性新 child alignment（明确 lineage 和 recovery reason，持久化
        nigo_loop=true），由 system_policy 自动选 verify 并只执行公开
        collection。不重开/覆盖旧 terminal run，不继承旧人工决定、授权、
        预算或模型许可；collection 能力关闭或无桥时全程零动作。"""
        runner = self._research_runner()
        if runner is None or not getattr(runner, "collection_enabled", False):
            return {"scanned": 0, "recovered": 0}
        if self.continuation_bridge is None:
            return {"scanned": 0, "recovered": 0}
        # rev12 选择顺序修正：先按 checkpoint sequence 选每个 fragment 的
        # 最新 terminal execution（status 终态语义真实），再只对该最新项
        # 做资格预检。任何更晚 terminal run 为非零网络、其它
        # stop_reason、畸形计数或已有证据，都不得回退恢复更老
        # 0-network run（防重复消费公开搜索预算）。
        latest_by_fragment: dict[str, tuple[int, LoopCheckpoint]] = {}
        subscription = getattr(runner, "subscription_research", None)
        subscription_owned_fragments: set[str] = set()
        for run_id in self.store.run_ids_with_prefix(EXECUTION_SPEC.loop_id):
            found = self.store.latest_with_sequence(run_id)
            if found is None:
                continue
            checkpoint, sequence = found
            if checkpoint.loop_id != EXECUTION_SPEC.loop_id:
                continue
            if subscription is not None and subscription.enabled_for(run_id):
                subscription_owned_fragments.add(checkpoint.fragment_id)
            if str(checkpoint.status) not in _TERMINAL_EXECUTION_STATUSES:
                continue
            current = latest_by_fragment.get(checkpoint.fragment_id)
            if current is None or sequence > current[0]:
                latest_by_fragment[checkpoint.fragment_id] = (sequence, checkpoint)
        recovered = 0
        for fragment_id, (_sequence, checkpoint) in sorted(latest_by_fragment.items()):
            # A selected original run is resumed in place by the subscription path.
            # Do not create competing legacy children, including below an existing
            # recovery child; this suppresses work, never grants child model scope.
            if fragment_id in subscription_owned_fragments:
                continue
            if not self._is_offline_recovery_candidate(checkpoint):
                continue  # 最新 terminal 不合格：零动作，绝不回退更老 run
            try:
                if self._recover_offline_execution(checkpoint):
                    recovered += 1
            except Exception:
                # 失败关闭：单个 fragment 的异常绝不中断恢复扫描。
                continue
        return {"scanned": len(latest_by_fragment), "recovered": recovered}

    @staticmethod
    def _is_offline_recovery_candidate(checkpoint: LoopCheckpoint) -> bool:
        """最新 terminal execution 的恢复资格预检（rev12/rev15 分流）：

        live_disabled 继续严格要求 searches=fetches=0（能力当时关闭，
        绝无网络事实）；capability_unavailable 允许非 bool、非负整数的
        已消费 searches/fetches（出口失败的网络尝试确已发生，由 rev14
        的 fingerprint 变化决定是否恢复一次）；畸形（str/None/float/
        bool）或负数一律零动作；无 active evidence 是共同前提。绝不
        回退选择更旧 terminal。"""
        collection = checkpoint.eval_results.get("research_collection")
        if not isinstance(collection, Mapping):
            return False
        stop_reason = str(collection.get("stop_reason", ""))
        if stop_reason not in ("live_disabled", "capability_unavailable"):
            return False
        searches = collection.get("searches")
        fetches = collection.get("fetches")
        if (
            isinstance(searches, bool)
            or not isinstance(searches, int)
            or isinstance(fetches, bool)
            or not isinstance(fetches, int)
        ):
            return False
        if searches < 0 or fetches < 0:
            return False  # 负数畸形零动作
        if stop_reason == "live_disabled" and (searches != 0 or fetches != 0):
            return False  # live_disabled 必须 0/0（能力关闭无网络事实）
        return not any(
            isinstance(record, Mapping)
            and record.get("marker") in ("inherited", "newly_collected")
            for record in collection.get("records", [])
        )

    def _fragment_has_fresh_evidence(self, fragment_id: str, *, exclude_run_id: str) -> bool:
        """同 fragment 的其它 execution 已有有效证据（后来已成功）。"""
        for other_id in self.store.run_ids_with_prefix(EXECUTION_SPEC.loop_id):
            if other_id == exclude_run_id:
                continue
            other = self.store.latest(other_id)
            if other is None or other.fragment_id != fragment_id:
                continue
            collection = other.eval_results.get("research_collection")
            if not isinstance(collection, Mapping):
                continue
            if any(
                isinstance(record, Mapping)
                and record.get("marker") in ("inherited", "newly_collected")
                for record in collection.get("records", [])
            ):
                return True
        return False

    def _lineage_root_input_digest(
        self, execution_checkpoint: LoopCheckpoint
    ) -> str | None:
        """沿已持久化 continuation 谱系向 root 有界回溯（rev11），返回
        可信 root source alignment 的 input_digest；任何断链/跨
        fragment/摘要漂移/环/超界返回 None（调用方零动作）。

        逐跳验证：same fragment、父 execution 存在且属 execution loop、
        父 binding.episode_id 与本跳 parent_episode_id 逐字一致、父
        result_digest 与本跳 source_result_digest 逐字一致、无环
        （parent_run_id 不重复）、跳数不超 OFFLINE_RECOVERY_LINEAGE_MAX_HOPS。
        无 parent_run_id 的执行即 root：取其 source alignment 的
        input_digest（frontmatter 来源摘要）。"""
        fragment_id = execution_checkpoint.fragment_id
        visited: set[str] = set()
        current = execution_checkpoint
        for _hop in range(OFFLINE_RECOVERY_LINEAGE_MAX_HOPS):
            if current.fragment_id != fragment_id:
                return None  # 跨 fragment
            binding = current.eval_results.get("execution_binding")
            if not isinstance(binding, Mapping):
                return None
            parent_run_id = binding.get("parent_run_id")
            if parent_run_id is None:
                # root execution：其 source alignment 的 input_digest 才是
                # 可与当前 bridge source digest 比较的根来源事实。root
                # alignment 校验（rev12）：必须属 ALIGNMENT_SPEC.loop_id、
                # same fragment，且 alignment.episode_id 与 root
                # execution binding.episode_id 逐字一致，否则零动作。
                alignment_id = binding.get("alignment_id")
                if not isinstance(alignment_id, str):
                    return None
                alignment_checkpoint = self.store.latest(alignment_id)
                if alignment_checkpoint is None:
                    return None
                if alignment_checkpoint.loop_id != ALIGNMENT_SPEC.loop_id:
                    return None  # 跨 alignment loop
                if alignment_checkpoint.fragment_id != fragment_id:
                    return None  # 跨 fragment
                alignment = alignment_checkpoint.eval_results.get("alignment")
                if not isinstance(alignment, Mapping):
                    return None
                if alignment.get("episode_id") != binding.get("episode_id"):
                    return None  # alignment/execution episode 不一致
                digest = alignment.get("input_digest")
                return str(digest) if isinstance(digest, str) else None
            parent_episode_id = binding.get("parent_episode_id")
            source_result_digest = binding.get("source_result_digest")
            if (
                not isinstance(parent_run_id, str)
                or not isinstance(parent_episode_id, str)
                or not isinstance(source_result_digest, str)
            ):
                return None
            if parent_run_id in visited:
                return None  # 环
            visited.add(parent_run_id)
            parent = self.store.latest(parent_run_id)
            if parent is None or parent.loop_id != EXECUTION_SPEC.loop_id:
                return None  # 断链
            parent_binding = parent.eval_results.get("execution_binding")
            if not isinstance(parent_binding, Mapping):
                return None
            if parent_binding.get("episode_id") != parent_episode_id:
                return None  # episode 绑定不一致
            if parent.eval_results.get("result_digest") != source_result_digest:
                return None  # 父摘要漂移（伪造 parent digest）
            current = parent
        return None  # 超过上限

    def _recover_offline_execution(self, execution_checkpoint: LoopCheckpoint) -> bool:
        run_id = execution_checkpoint.run_id
        fragment_id = execution_checkpoint.fragment_id
        evals = execution_checkpoint.eval_results
        collection = evals.get("research_collection")
        if not isinstance(collection, Mapping):
            return False
        active = [
            record
            for record in collection.get("records", [])
            if isinstance(record, Mapping)
            and record.get("marker") in ("inherited", "newly_collected")
        ]
        if active:
            return False  # 已有有效证据：零动作
        # rev14/15 capability fingerprint：历史 capability_unavailable run
        # 只有在 fingerprint 变化（含旧无 fingerprint）时才允许恢复一次；
        # 同一 fingerprint 零重试。失败关闭：当前 fingerprint 缺失/空/
        # 非字符串一律零恢复（防 None→None 形成跨 child 无限恢复链）；
        # 旧 fingerprint 缺失仅在当前合法时允许一次。
        if str(collection.get("stop_reason", "")) == "capability_unavailable":
            runner = self._research_runner()
            current_fingerprint = (
                getattr(runner, "search_fingerprint", None) if runner is not None else None
            )
            if not isinstance(current_fingerprint, str) or not current_fingerprint:
                return False
            old_fingerprint = collection.get("search_fingerprint")
            if old_fingerprint is not None and (
                not isinstance(old_fingerprint, str)
                or old_fingerprint == current_fingerprint
            ):
                return False  # 旧 fingerprint 畸形或与当前相同：零动作
        binding = evals.get("execution_binding")
        result_digest = evals.get("result_digest")
        alignment_id = binding.get("alignment_id") if isinstance(binding, Mapping) else None
        if (
            not isinstance(binding, Mapping)
            or not isinstance(alignment_id, str)
            or not isinstance(result_digest, str)
            or _SHA256.fullmatch(result_digest) is None
        ):
            return False
        # 能力闭集：私人/凭据/写入/发布/部署/安装标记 → 零动作。
        if not self._auto_scope_allowed(binding.get("execution_scope")):
            return False
        alignment_checkpoint = self.store.latest(alignment_id)
        old_alignment = (
            alignment_checkpoint.eval_results.get("alignment")
            if alignment_checkpoint is not None
            else None
        )
        episode_id = (
            old_alignment.get("episode_id") if isinstance(old_alignment, Mapping) else None
        )
        if not isinstance(episode_id, str):
            return False
        # 同 fragment 已存在新证据（后续 execution 已成功）：零动作。
        if self._fragment_has_fresh_evidence(fragment_id, exclude_run_id=run_id):
            return False
        # 谱系有界回溯到可信 root source alignment（rev11）：continuation
        # child 的 input_digest 是 lineage digest，只有 root 的 source
        # input_digest 才能与当前 bridge digest 比较；断链/跨 fragment/
        # 摘要漂移/环/超界均零动作。
        root_input_digest = self._lineage_root_input_digest(execution_checkpoint)
        if root_input_digest is None:
            return False
        # ContinuationBridge 对当前 raw frontmatter 重新验证 nigo-loop:true
        # （bridge 内部 admission_state 硬校验）；任何桥失败 → 零动作。
        bridge = self.continuation_bridge
        if bridge is None:
            return False
        try:
            source_now = bridge.discover_intent_source(fragment_id)
        except Exception:
            return False
        # 当前来源绑定未漂移才 eligible（与可信 root source digest 逐字
        # 一致）；不得凭旧标题/文案猜资格。
        if (
            not isinstance(source_now, Mapping)
            or source_now.get("input_digest") != root_input_digest
        ):
            return False
        goal = _text(f"继续公开核验：{source_now['title']}", "goal", 200)
        # 并发 single-flight + 幂等（唯一键领取，rev9 原语）：同 fragment
        # 只恢复一次；已存在即已恢复/他人在恢复 → 零动作。
        claim_ref = json.dumps(
            {
                "fragment_id": fragment_id,
                "parent_run_id": run_id,
                "goal_digest": _digest([goal]),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _key, is_new = self.store.claim_action_once(
            run_id=run_id,
            loop_id=EXECUTION_SPEC.loop_id,
            fragment_id=fragment_id,
            node="offline_recovery",
            action_type="offline_recovery_claim",
            target=f"offline-recovery:{fragment_id}",
            result_ref=claim_ref,
        )
        if not is_new:
            return False
        input_digest = _digest(
            {
                "offline_recovery": "v1",
                "parent_run_id": run_id,
                "source_result_digest": result_digest,
                "goal": goal,
            }
        )
        source = {
            "title": str(source_now["title"]),
            "literal_summary": str(source_now["literal_summary"]),
            "memory_basis": [],
            "input_digest": input_digest,
            "nigo_loop": True,
            # rev14：公开来源 seed candidate 随 child source 传递（bridge
            # 已 SSRF 校验并去查询参数；缺失则不带）。
            **(
                {"source_seed_url": source_now["source_seed_url"]}
                if isinstance(source_now.get("source_seed_url"), str)
                else {}
            ),
        }
        proposal = _validate_proposal(self.resolver(source))
        if proposal.get("recommended_route") != "verify":
            # 恢复只走 verify 公开 collection；resolver 其它结论零动作
            # （claim 已消耗，保守不再尝试，绝不悬挂半恢复）。
            return False
        child_alignment_id = _alignment_id(fragment_id, input_digest)
        status, _projection = self._commit_alignment(
            fragment_id,
            input_digest,
            source,
            proposal,
            lineage={
                "parent_episode_id": episode_id,
                "parent_run_id": run_id,
                "source_result_digest": result_digest,
                "continuation_id": continuation_id(episode_id, run_id, result_digest, goal),
                "continuation_goal": goal,
                "recovery_reason": "offline_zero_evidence",
            },
        )
        # system_policy 自动选 verify 并只执行公开 collection
        # （auto_synthesis_allowed=False：模型/Keychain 硬关闭）。
        self.maybe_auto_advance(child_alignment_id)
        return status in (200, 201)

    def continue_episode(
        self, parent_episode_id: str, raw: object
    ) -> tuple[int, dict[str, object]]:
        required = {
            "parent_episode_id",
            "parent_run_id",
            "source_result_digest",
            "goal",
            "continuation_id",
            "requester",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise FragmentIntentError("invalid_body")
        if raw.get("parent_episode_id") != parent_episode_id or raw.get("requester") != "nigo":
            raise FragmentIntentError("invalid_body")
        parent_run_id = _text(raw.get("parent_run_id"), "parent_run_id", 96)
        result_digest = raw.get("source_result_digest")
        goal = _text(raw.get("goal"), "goal", 200)
        if not isinstance(result_digest, str) or _SHA256.fullmatch(result_digest) is None:
            raise FragmentIntentError("invalid_body")
        expected_id = continuation_id(parent_episode_id, parent_run_id, result_digest, goal)
        if raw.get("continuation_id") != expected_id:
            raise FragmentIntentError("continuation_binding_changed")
        parent = self.store.latest(parent_run_id)
        if parent is None or parent.loop_id != EXECUTION_SPEC.loop_id:
            raise FragmentIntentError("parent_run_not_found")
        binding = parent.eval_results.get("execution_binding")
        result = parent.eval_results.get("intent_result")
        actual_digest = parent.eval_results.get("result_digest")
        if (
            not isinstance(binding, Mapping)
            or binding.get("episode_id") != parent_episode_id
            or not isinstance(result, Mapping)
            or actual_digest != result_digest
        ):
            raise FragmentIntentError("continuation_binding_changed")
        fragment_id = parent.fragment_id
        input_digest = _digest(
            {
                "parent_episode_id": parent_episode_id,
                "parent_run_id": parent_run_id,
                "source_result_digest": result_digest,
                "goal": goal,
            }
        )
        new_alignment_id = _alignment_id(fragment_id, input_digest)
        with self._lock(new_alignment_id):
            harvest = parent.eval_results.get("harvest", [])
            basis: list[dict[str, str]] = []
            if isinstance(harvest, list):
                for item in harvest:
                    if not isinstance(item, Mapping) or item.get("maturity") not in MATURITY:
                        continue
                    basis.append(
                        {
                            "label": _text(item.get("summary"), "harvest", 120),
                            "maturity": str(item["maturity"]),
                        }
                    )
                    if len(basis) == 3:
                        break
            source = {
                "title": _text(parent.fragment_title, "source", 120),
                "literal_summary": _text(
                    f"上一步结果：{result.get('summary', '')}；本次目标：{goal}",
                    "source",
                    800,
                ),
                "memory_basis": basis,
                "input_digest": input_digest,
            }
            proposal = _validate_proposal(self.resolver(source))
            return self._commit_alignment(
                fragment_id,
                input_digest,
                source,
                proposal,
                lineage={
                    "parent_episode_id": parent_episode_id,
                    "parent_run_id": parent_run_id,
                    "source_result_digest": result_digest,
                    "continuation_id": expected_id,
                    "continuation_goal": goal,
                },
            )

    def case(self, case_id: str) -> dict[str, object]:
        episodes = [
            item for item in self.list_alignments() if item.get("case_id") == case_id
        ]
        if not episodes:
            raise FragmentIntentError("case_not_found")
        return {
            "case_id": case_id,
            "fragment_id": episodes[0]["fragment_id"],
            "title": episodes[0]["title"],
            "episodes": [
                {
                    key: item[key]
                    for key in (
                        "episode_id",
                        "parent_episode_id",
                        "source_result_digest",
                        "status",
                        "route",
                        "execution",
                        "updated_at",
                    )
                    if key in item
                }
                for item in episodes
            ],
        }

    def harvest(self, episode_id: str) -> dict[str, object]:
        alignment = next(
            (item for item in self.list_alignments() if item.get("episode_id") == episode_id),
            None,
        )
        if alignment is None:
            raise FragmentIntentError("episode_not_found")
        execution = alignment.get("execution")
        return {
            "episode_id": episode_id,
            "status": (
                execution.get("status")
                if isinstance(execution, Mapping)
                else alignment["status"]
            ),
            "items": execution.get("harvest", []) if isinstance(execution, Mapping) else [],
        }

    def compile_context(self, case_id: str, *, limit: int = 8) -> list[dict[str, object]]:
        if not 1 <= limit <= 32:
            raise FragmentIntentError("context_limit_invalid")
        items: list[dict[str, object]] = []
        episodes = self.case(case_id).get("episodes")
        if not isinstance(episodes, list):
            raise FragmentIntentError("case_invalid")
        for episode in episodes:
            if not isinstance(episode, Mapping):
                continue
            episode_id = episode.get("episode_id")
            if not isinstance(episode_id, str):
                continue
            harvested = self.harvest(episode_id)["items"]
            if isinstance(harvested, list):
                items.extend(item for item in harvested if isinstance(item, dict))
        priority = {"reusable": 0, "qualified": 1, "candidate": 2}
        ordered = sorted(
            items,
            key=lambda item: (
                priority.get(str(item.get("maturity")), 3),
                str(item.get("role")),
                str(item.get("summary")),
            ),
        )
        stronger = [item for item in ordered if item.get("maturity") != "candidate"]
        selected = stronger[:limit] if stronger else ordered[:limit]
        return [
            {
                **item,
                "usage": (
                    "verified_context"
                    if item.get("maturity") == "reusable"
                    else "qualified_context"
                    if item.get("maturity") == "qualified"
                    else "unverified_clue"
                ),
            }
            for item in selected
        ]

    def escalate(self, alignment_id: str, raw: object) -> tuple[int, dict[str, object]]:
        required = {
            "alignment_id",
            "revision",
            "input_digest",
            "source_result_digest",
            "escalation_id",
            "requester",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise FragmentIntentError("invalid_body")
        alignment = self.get_alignment(alignment_id)
        result_digest = raw.get("source_result_digest")
        if (
            raw.get("alignment_id") != alignment_id
            or raw.get("requester") != "nigo"
            or raw.get("revision") != alignment.get("revision")
            or raw.get("input_digest") != alignment.get("input_digest")
            or not isinstance(result_digest, str)
            or _SHA256.fullmatch(result_digest) is None
            or raw.get("escalation_id") != escalation_id(alignment_id, result_digest)
        ):
            raise FragmentIntentError("escalation_binding_changed")
        execution = alignment.get("execution")
        proposal = execution.get("graph_escalation") if isinstance(execution, Mapping) else None
        if (
            not isinstance(proposal, Mapping)
            or proposal.get("source_result_digest") != result_digest
        ):
            raise FragmentIntentError("escalation_not_available")
        return 200, {
            "escalation_id": raw["escalation_id"],
            "status": "proposed",
            "reason": proposal.get("reason"),
            "capability_status": "template_required",
            "graph_run_created": False,
        }

    def _ensure_execution(self, alignment_checkpoint: LoopCheckpoint) -> LoopCheckpoint:
        alignment = alignment_checkpoint.eval_results["alignment"]
        run_id = str(alignment["execution_run_id"])
        existing = self.store.latest(run_id)
        if existing is not None:
            return existing
        route = str(alignment["route"])
        binding: dict[str, object] = {
            "alignment_id": alignment_checkpoint.run_id,
            "alignment_digest": alignment["confirmed_digest"],
            "input_digest": alignment["input_digest"],
            "case_id": alignment["case_id"],
            "episode_id": alignment["episode_id"],
            "route": route,
            "intents": alignment["decision"]["intents"],
            "supplement": alignment["decision"]["supplement"],
            "title": alignment_checkpoint.fragment_title,
            "literal_summary": alignment["literal_summary"],
            **({"goal": alignment["goal"]} if "goal" in alignment else {}),
            **({"source_origin": alignment["source_origin"]}
               if "source_origin" in alignment else {}),
            "execution_scope": alignment["execution_scope"],
        }
        # continuation 子 episode 的谱系字段随绑定传递（DESIGN §6）：仅供
        # 证据继承定位父 Run；授权/Receipt/预算/人工决定/幂等键零继承。
        for lineage_key in (
            "parent_episode_id",
            "parent_run_id",
            "source_result_digest",
            "continuation_id",
        ):
            if alignment.get(lineage_key) is not None:
                binding[lineage_key] = alignment[lineage_key]
        # TASK D：system_policy 自动路线只跑公开 collection——模型/Receipt
        # 永远停在人类闸门（适配器读取该标记，缺省视为人工路线允许）。
        binding["auto_synthesis_allowed"] = alignment.get("decision_source") != "system_policy"
        # rev14：公开来源 seed candidate 随绑定传递（待核验候选，非来源身份）。
        if isinstance(alignment.get("source_seed_url"), str):
            binding["source_seed_url"] = alignment["source_seed_url"]
        checkpoint = LoopCheckpoint(
            loop_id=EXECUTION_SPEC.loop_id,
            loopspec_version=EXECUTION_SPEC.version,
            run_id=run_id,
            fragment_id=alignment_checkpoint.fragment_id,
            parent_loop_id=alignment_checkpoint.run_id,
            current_node=EXECUTION_SPEC.first_node,
            status="approved",
            goal=EXECUTION_SPEC.goal,
            fragment_title=alignment_checkpoint.fragment_title,
            fragment_title_source=alignment_checkpoint.fragment_title_source,
            eval_results={"system": {"execution_binding": binding}, "plugins": {}},
            budget_limits={
                "iterations": EXECUTION_SPEC.max_iterations,
                "seconds": EXECUTION_SPEC.max_seconds,
                "tokens": EXECUTION_SPEC.token_limit,
                "tool_calls": EXECUTION_SPEC.tool_call_limit,
            },
            budget_used={"tokens": 0, "tool_calls": 0, "seconds": 0.0},
            worker_version=EXECUTION_SPEC.worker_version,
            evaluator_version=EXECUTION_SPEC.evaluator_version,
        )
        committed = self.store.compare_and_append(
            checkpoint,
            expected_sequence=0,
            event_type="run_registered",
            revision_reason="confirmed_alignment",
        )
        return committed or self.store.latest(run_id) or checkpoint

    def run_execution(self, run_id: str) -> dict[str, object]:
        checkpoint = self.store.latest(run_id)
        if checkpoint is None or checkpoint.loop_id != EXECUTION_SPEC.loop_id:
            raise FragmentIntentError("execution_not_found")

        runner = self._research_runner()
        subscription = getattr(runner, "subscription_research", None)
        subscribed = subscription is not None and subscription.enabled_for(run_id)
        # Explicit subscription authorization applies only to this selected run.
        # Paid API limits and every unselected run keep their previous policy.
        execution_spec = (
            replace(EXECUTION_SPEC, max_seconds=2400, tool_call_limit=16)
            if subscribed else EXECUTION_SPEC
        )

        def execute(context: SupervisorContext) -> LoopResult:
            binding = context.checkpoint.eval_results.get("execution_binding")
            if not isinstance(binding, Mapping):
                raise FragmentIntentError("execution_binding_invalid")
            route = str(binding.get("route"))
            adapter = self.direct_adapter if route == "direct" else self.verify_adapter
            if adapter is None:
                return LoopResult(
                    status="blocked",
                    stop_reason="capability_unavailable",
                    resume_condition=f"Install and authorize the {route} capability adapter",
                    unresolved_issues=[f"{route}_capability_unavailable"],
                )
            payload = dict(binding)
            if route != "direct":
                # verify 路线挂载受治理研究链：执行 Run 身份绑定给研究适配器。
                payload["execution_run_id"] = context.checkpoint.run_id
            result = _validate_result(adapter(payload))
            extra_evals: dict[str, object] = {}
            if route != "direct":
                # 研究进度与 candidate evidence 只经适配器只读侧信道并入
                # Checkpoint；投影永远只来自 execution Run Checkpoint。
                progress = getattr(adapter, "last_research_progress", None)
                if isinstance(progress, Mapping):
                    extra_evals["research_progress"] = dict(progress)
                records = getattr(adapter, "last_research_records", None)
                if isinstance(records, list):
                    extra_evals["research_evidence"] = [
                        dict(item) for item in records if isinstance(item, Mapping)
                    ]
                # research_collection 已由受治理研究链控制面持久化到
                # system；插件侧信道不再重复携带（否则与 system 冲突）。
                synthesis = getattr(adapter, "last_research_synthesis", None)
                if isinstance(synthesis, Mapping):
                    extra_evals["research_synthesis"] = dict(synthesis)
            scope = binding.get("execution_scope")
            if not isinstance(scope, Mapping):
                raise FragmentIntentError("execution_scope_invalid")
            if (
                cast(int, result["model_calls"])
                > (MAX_SUBSCRIPTION_ROUNDS if subscribed else cast(int, scope["model_call_cap"]))
                or cast(int, result["tool_calls"]) > execution_spec.tool_call_limit
            ):
                raise FragmentIntentError("result_budget_exceeded")
            return LoopResult(
                status="continue",
                next_node="harvest",
                eval_results=plugin_eval("execute", {"intent_result": result, **extra_evals}),
                tokens_used=0,
                tool_calls_used=cast(int, result["tool_calls"]),
            )

        def harvest(context: SupervisorContext) -> LoopResult:
            result = context.checkpoint.eval_results.get("intent_result")
            if not isinstance(result, Mapping):
                raise FragmentIntentError("result_missing")
            escalation = (
                {
                    "status": "proposed",
                    "reason": result["escalation_reason"],
                    "source_result_digest": _digest(result),
                }
                if result.get("needs_escalation") is True
                else None
            )
            return LoopResult(
                status="passed",
                eval_results=plugin_eval(
                    "harvest",
                    {
                        "harvest": list(result.get("harvest", [])),
                        "graph_escalation": escalation,
                        "result_digest": _digest(result),
                    },
                ),
                stop_reason="completed",
            )

        supervisor = LoopSupervisor(
            self.store,
            execution_spec,
            {"execute": execute, "harvest": harvest},
        )
        final = supervisor.run(run_id)
        return self._project_execution(final)

    def _project(self, checkpoint: LoopCheckpoint, sequence: int) -> dict[str, object]:
        alignment = checkpoint.eval_results.get("alignment")
        if not isinstance(alignment, Mapping):
            raise FragmentIntentError("alignment_invalid")
        keys = {
            "revision",
            "input_digest",
            "alignment_digest",
            "case_id",
            "episode_id",
            "literal_summary",
            "goal",
            "source_origin",
            "memory_basis",
            "nigo_loop",
            "suggested_intents",
            "dynamic_intents",
            "reasoning",
            "plan",
            "expected_result",
            "exclusions",
            "recommended_route",
            "execution_scope",
            "decision",
            "route",
            "decision_source",
            "execution_run_id",
            "parent_episode_id",
            "parent_run_id",
            "source_result_digest",
            "continuation_id",
            "continuation_goal",
            "recovery_reason",
        }
        projection = {
            "alignment_id": checkpoint.run_id,
            "fragment_id": checkpoint.fragment_id,
            "title": checkpoint.fragment_title,
            "status": checkpoint.status,
            "sequence": sequence,
            "updated_at": checkpoint.updated_at,
            "execution_scope_basis": "original_alignment_proposal",
            "exclusions_basis": "original_alignment_proposal",
            **{
                key: alignment[key]
                for key in keys
                if key in alignment and alignment[key] is not None
            },
        }
        execution_run_id = alignment.get("execution_run_id")
        if isinstance(execution_run_id, str):
            execution = self.store.latest(execution_run_id)
            if execution is not None:
                projected_execution = self._project_execution(execution)
                projection["execution"] = projected_execution
                projection["effective_execution_scope"] = projected_execution.get(
                    "effective_execution_scope")
        return projection

    def prospective_subscription_eligible(self, run_id: str, cutover: datetime) -> bool:
        """Current public-source authorization plus immutable first-capture proof.

        Old intakes are never upgraded by a new organizer/alignment. This reads
        only the exact bound raw file and existing checkpoints, and sends no data.
        """
        from fragment_loop.continuation_bridge import prospective_raw_source

        try:
            if (self.continuation_bridge is None or not isinstance(cutover, datetime)
                    or cutover.tzinfo is None or cutover.utcoffset() is None):
                return False
            execution = self.store.latest(run_id)
            if execution is None or execution.loop_id != EXECUTION_SPEC.loop_id:
                return False
            binding = execution.eval_results.get("execution_binding")
            if not isinstance(binding, Mapping) or binding.get("route") != "verify":
                return False
            alignment_id = binding.get("alignment_id")
            if not isinstance(alignment_id, str):
                return False
            parent = self.store.latest(alignment_id)
            if (parent is None or parent.loop_id != ALIGNMENT_SPEC.loop_id
                    or parent.fragment_id != execution.fragment_id or parent.status != "passed"
                    or execution.parent_loop_id != alignment_id):
                return False
            alignment = parent.eval_results.get("alignment")
            if not isinstance(alignment, Mapping) or not self._auto_route_eligible(alignment):
                return False
            if (any(alignment.get(key) or binding.get(key) for key in (
                    "parent_run_id", "continuation_id", "recovery_reason"))
                    or alignment.get("execution_run_id") != run_id
                    or alignment.get("route") != "verify"
                    or binding.get("input_digest") != alignment.get("input_digest")
                    or _alignment_id(execution.fragment_id, str(binding.get("input_digest")))
                    != alignment_id):
                return False
            confirmed = _digest({"alignment_digest": alignment.get("alignment_digest"),
                                 "decision": alignment.get("decision"),
                                 "execution_scope": alignment.get("execution_scope")})
            if (alignment.get("confirmed_digest") != confirmed
                    or binding.get("alignment_digest") != confirmed
                    or _execution_id(confirmed) != run_id):
                return False
            source = prospective_raw_source(
                self.store, self.continuation_bridge.review_service.vault_root,
                execution.fragment_id, cutover)
            return bool(source and source["source_seed_url"] == alignment.get("source_seed_url")
                        and source["source_seed_url"] == binding.get("source_seed_url")
                        and (alignment.get("source_origin") != "raw_capture" or (
                            binding.get("source_origin") == "raw_capture"
                            and source["input_digest"] == binding.get("input_digest"))))
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def resume_subscription_executions(self) -> dict[str, int]:
        """Continue selected original tasks and publish verified results; no new episodes."""
        runner = self._research_runner()
        engine = getattr(runner, "subscription_research", None)
        if engine is None:
            return {"scanned": 0, "resumed": 0}
        completed = 0
        candidates = set(engine.run_ids)
        if getattr(engine, "prospective_eligibility", None) is not None:
            candidates.update(self.store.run_ids_with_prefix(EXECUTION_SPEC.loop_id))
        selected = [run_id for run_id in sorted(candidates) if engine.enabled_for(run_id)]
        for run_id in selected:
            # Share execution identity with normal dispatch, and never let one
            # broken task starve the rest of the explicitly authorized set.
            with self._lock(run_id):
                try:
                    completed += self._resume_subscription_execution(runner, engine, run_id)
                except Exception as error:
                    self.last_subscription_error = {
                        "run_id": run_id,
                        "reason": type(error).__name__,
                    }
        return {"scanned": len(selected), "resumed": completed}

    def _resume_subscription_execution(self, runner: Any, engine: Any, run_id: str) -> int:
        checkpoint = self.store.latest(run_id)
        if checkpoint is None or checkpoint.loop_id != EXECUTION_SPEC.loop_id:
            return 0
        binding = checkpoint.eval_results.get("execution_binding")
        if not isinstance(binding, Mapping) or binding.get("route") != "verify":
            return 0
        previous = checkpoint.eval_results.get("research_synthesis")
        succeeded = isinstance(previous, Mapping) and previous.get("status") == "synthesized"
        completed = 0
        review_existing = succeeded and engine.needs_review(previous)
        if not succeeded or review_existing:
            # Corrections and a closed model-only timeout use remaining original
            # budget. Live claims and unknown tool effects remain fail-closed.
            if (
                not review_existing and isinstance(previous, Mapping)
                and previous.get("provider") in ("codex_subscription", "kimi_subscription")
                and previous.get("status") != "subscription_in_progress"
            ):
                if (
                    previous.get("status") not in (
                        "subscription_unavailable", "output_invalid", "independent_review_failed",
                        "independent_review_unknown"
                    )
                    or not (engine.can_resume_format_failure(runner, run_id)
                            or engine.can_resume_contract_upgrade(runner, run_id)
                            or engine.can_resume_model_failure(runner, run_id)
                            or engine.can_resume_recorded_output(runner, run_id))
                ):
                    return 0
            adapter = GovernedResearchVerifyAdapter(runner)
            payload = {**binding, "execution_run_id": run_id}
            collection = checkpoint.eval_results.get("research_collection")
            recorded_goal = None
            if (
                not payload.get("goal")
                and isinstance(previous, Mapping)
                and previous.get("provider") in ("codex_subscription", "kimi_subscription")
                and isinstance(collection, Mapping)
                and isinstance(collection.get("goal"), str)
                and collection["goal"].strip()
            ):
                # Legacy runs may lack a bound raw goal. Their first collection
                # retains the complete question before older resumes appended
                # title/seed again; never reload a refreshed organizer instead.
                for historical in self.store.history(run_id):
                    first_collection = historical.eval_results.get("research_collection")
                    if (isinstance(first_collection, Mapping)
                            and isinstance(first_collection.get("goal"), str)
                            and first_collection["goal"].strip()):
                        recorded_goal = first_collection["goal"]
                        break
            if (not payload.get("goal")
                    and (recorded_goal is None or not payload.get("source_seed_url"))
                    and isinstance(binding.get("input_digest"), str)):
                source = self.source_loader(checkpoint.fragment_id, str(binding["input_digest"]))
                if recorded_goal is None and source.get("goal"):
                    payload["goal"] = source["goal"]
                # Legacy bindings can predate seed projection. Recover only the
                # source-bound public candidate, never private raw text or a new goal.
                seed = source.get("source_seed_url")
                if not payload.get("source_seed_url") and seed is not None:
                    if not isinstance(seed, str) or not is_public_https_locator(seed):
                        raise FragmentIntentError("source_invalid")
                    parsed = urllib.parse.urlsplit(seed)
                    if parsed.query or parsed.fragment:
                        raise FragmentIntentError("source_invalid")
                    payload["source_seed_url"] = seed
            result = _validate_result(adapter(
                payload, review_existing=previous if review_existing else None,
                recorded_goal=recorded_goal,
            ))
            synthesis = adapter.last_research_synthesis
            if not isinstance(synthesis, Mapping):
                return 0
            if synthesis.get("status") in (
                "subscription_in_progress", "independent_review_in_progress"
            ):
                return 0
            if (
                int(cast(Any, result["model_calls"])) > MAX_SUBSCRIPTION_ROUNDS
                or int(cast(Any, result["tool_calls"])) > 16
            ):
                raise FragmentIntentError("subscription_result_budget_exceeded")
            for _attempt in range(3):
                found = self.store.latest_raw_with_sequence(run_id)
                if found is None:
                    return 0
                raw, sequence = found
                view = checkpoint_compat_view(raw)
                values = {**view.eval_results, "intent_result": result,
                          "research_synthesis": dict(synthesis),
                          "research_evidence": adapter.last_research_records,
                          "harvest": list(cast(Any, result.get("harvest", []))),
                          "graph_escalation": None, "result_digest": _digest(result),
                          "subscription_authorization": {
                              "source": "user_authorized_subscription", "run_id": run_id,
                              "provider": synthesis.get("provider"),
                              "max_agent_invocations": MAX_SUBSCRIPTION_ROUNDS, "max_tools": 16}}
                committed = self.store.compare_and_append(
                    replace(
                        raw,
                        status="passed" if synthesis.get("status") == "synthesized" else "blocked",
                        stop_reason=str(synthesis.get("status")),
                        updated_at=utc_now(),
                        eval_results=translate_view_edit(
                            existing=raw.eval_results, edited_flat=values
                        ),
                    ),
                    expected_sequence=sequence,
                    event_type="subscription_research_result",
                    revision_reason="continue_original_authorized_task",
                )
                if committed is not None:
                    checkpoint = self.store.latest(run_id)
                    completed = 1
                    break
        if checkpoint is None or engine.library is None:
            return completed
        synthesis = checkpoint.eval_results.get("research_synthesis")
        collection = checkpoint.eval_results.get("research_collection")
        if (
            not isinstance(synthesis, Mapping)
            or synthesis.get("status") != "synthesized"
            or not isinstance(collection, Mapping)
        ):
            return completed
        receipt = checkpoint.eval_results.get("knowledge_publication")
        result_digest = _digest(synthesis["result"])
        review_digest = _digest(synthesis.get("independent_review"))
        if (isinstance(receipt, Mapping) and receipt.get("result_digest") == result_digest
                and receipt.get("independent_review_digest", _digest(None)) == review_digest):
            checked = dict(receipt)
            try:
                engine.library.validate_publication(receipt)
                checked.pop("status", None)
                checked.pop("reason", None)
            except Exception as error:
                checked.update(status="unavailable", reason=str(error)[:120])
            if checked != receipt:
                found = self.store.latest_raw_with_sequence(run_id)
                if found is not None:
                    raw, sequence = found
                    view = checkpoint_compat_view(raw)
                    self.store.compare_and_append(
                        replace(
                            raw,
                            updated_at=utc_now(),
                            eval_results=translate_view_edit(
                                existing=raw.eval_results,
                                edited_flat={**view.eval_results, "knowledge_publication": checked},
                            ),
                        ),
                        expected_sequence=sequence,
                        event_type="knowledge_publication_checked",
                        revision_reason="saved_note_integrity_checked",
                    )
            return completed
        doc = engine.library.save_research(
            run_id=run_id, fragment_id=checkpoint.fragment_id,
            title=str(binding.get("title", "研究结论")), result=synthesis["result"],
            evidence=collection.get("records", []), status="synthesized",
            independent_review=synthesis.get("independent_review"),
            expected_revision=receipt.get("revision") if isinstance(receipt, Mapping) else None)
        for _attempt in range(3):
            found = self.store.latest_raw_with_sequence(run_id)
            if found is None:
                return completed
            raw, sequence = found
            view = checkpoint_compat_view(raw)
            publication = {"knowledge_id": doc["knowledge_id"], "revision": doc["revision"],
                           "path": doc["path"], "result_digest": result_digest,
                           "independent_review_digest": review_digest,
                           "publication_source": "system_policy"}
            committed = self.store.compare_and_append(
                replace(
                    raw,
                    updated_at=utc_now(),
                    eval_results=translate_view_edit(
                        existing=raw.eval_results,
                        edited_flat={**view.eval_results, "knowledge_publication": publication},
                    ),
                ),
                expected_sequence=sequence,
                event_type="knowledge_publication",
                revision_reason="automatic_research_knowledge",
            )
            if committed is not None:
                break
        return completed

    def _project_execution(self, checkpoint: LoopCheckpoint) -> dict[str, object]:
        binding = checkpoint.eval_results.get("execution_binding", {})
        result = checkpoint.eval_results.get("intent_result")
        synthesis = checkpoint.eval_results.get("research_synthesis")
        research_progress = checkpoint.eval_results.get("research_progress")
        collection = checkpoint.eval_results.get("research_collection")
        runner = self._research_runner() if self is not None else None
        journal = checkpoint.eval_results.get("research_journal_entry")
        subscription_journal = isinstance(journal, Mapping) and str(
            journal.get("key", "")
        ).startswith("subscription:")
        if isinstance(collection, Mapping) or subscription_journal:
            # New subscription runs persist action receipts before collection exists.
            # One history read for this run; never scan all executions for each row.
            entries = (
                runner._journal_entries(checkpoint.run_id)
                if runner is not None and subscription_journal and checkpoint.status == "running"
                else None
            )
            research_progress = research_progress_projection(checkpoint, journal_entries=entries)
        projected_result = dict(result) if isinstance(result, Mapping) else None
        if projected_result is not None:
            # 九段映射附加字段（§3.9 形状）：只在合成验证通过时出现，否则诚实缺省。
            projected_result.update(
                _project_research_result_extras(
                    checkpoint.eval_results.get("research_synthesis")
                )
            )
        research_evidence = _project_research_evidence(
            collection.get("records") if isinstance(collection, Mapping)
            else checkpoint.eval_results.get("research_evidence")
        )
        subscription = getattr(runner, "subscription_research", None)
        selected = bool(subscription is not None and subscription.enabled_for(checkpoint.run_id))
        effective_scope = None
        provider = getattr(getattr(subscription, "agent", None), "provider", None)
        if subscription is not None and selected and provider in {
            "kimi_subscription", "codex_subscription"
        }:
            from fragment_loop.repository_trial import run_repository_trial

            trial_allowed = subscription.trial is run_repository_trial
            save_allowed = callable(getattr(subscription.library, "save_research", None))
            capabilities = ["公开来源研究", "套餐模型综合判断", "独立证据复核"]
            if trial_allowed:
                capabilities.append("必要时安装依赖并隔离试跑公开仓库")
            write_scope = ["本任务 Loop Checkpoint 与工具回执"]
            if save_allowed:
                capabilities.append("复核通过后自动沉淀结论到 Obsidian")
                write_scope.append("Obsidian 研究结论、修订历史与知识目录")
            observed_provider = (synthesis.get("provider")
                if isinstance(synthesis, Mapping) else None)
            if observed_provider not in {"kimi_subscription", "codex_subscription"}:
                observed_provider = (research_progress.get("provider")
                    if isinstance(research_progress, Mapping) else None)
            publication = checkpoint.eval_results.get("knowledge_publication")
            effective_scope = {
                "basis": "currently_selected_subscription_configuration",
                "selection_basis": (
                    "exact_run_allowlist" if checkpoint.run_id in subscription.run_ids
                    else "prospective_public_capture"),
                "model_provider": provider, "model_name": None,
                "billing_mode": "existing_subscription",
                "model_call_cap": MAX_SUBSCRIPTION_ROUNDS,
                "model_call_cap_basis": "total_agent_invocation_slots_including_unknown_and_review",
                "tool_call_cap": MAX_SUBSCRIPTION_TOOLS,
                "tool_call_cap_basis": "total_new_tool_receipts_per_run",
                "capabilities": capabilities,
                "external_scope": ["用户本任务问题与经安全校验的公开来源"],
                "write_scope": write_scope,
                "repository_trial_allowed": trial_allowed,
                "automatic_knowledge_save_allowed": save_allowed,
                "exclusions": ["不启用新增付费 API", "不自动部署到用户系统",
                    "试跑进程不可联网、读取凭据或启动子进程",
                    "未通过独立证据复核的内容不保存为可用研究结论"],
                "observed_model_provider": (observed_provider if observed_provider in
                    {"kimi_subscription", "codex_subscription"} else None),
                "knowledge_publication_status": (
                    "unavailable" if isinstance(publication, Mapping)
                    and publication.get("status") == "unavailable"
                    else "recorded" if isinstance(publication, Mapping)
                    and publication.get("knowledge_id") else "not_recorded"),
            }
        return {
            "run_id": checkpoint.run_id,
            "fragment_id": checkpoint.fragment_id,
            "subscription_selected": selected,
            "effective_execution_scope": effective_scope,
            "subscription_authorization": checkpoint.eval_results.get("subscription_authorization"),
            "independent_review": (
                checkpoint.eval_results["research_synthesis"].get("independent_review")
                if isinstance(checkpoint.eval_results.get("research_synthesis"), Mapping)
                else None
            ),
            "status": checkpoint.status,
            "current_node": checkpoint.current_node,
            "route": binding.get("route") if isinstance(binding, Mapping) else None,
            "result": projected_result,
            "harvest": checkpoint.eval_results.get("harvest", []),
            "graph_escalation": _project_graph_escalation(
                checkpoint.eval_results.get("graph_escalation"),
                binding,
                checkpoint.eval_results.get("result_digest"),
                research_evidence,
            ),
            "result_digest": checkpoint.eval_results.get("result_digest"),
            "research_result_digest": (
                research_result_digest(synthesis["result"])
                if (isinstance(synthesis, Mapping)
                    and synthesis.get("status") == "synthesized"
                    and isinstance(synthesis.get("result"), Mapping))
                else None
            ),
            "research_progress": (
                dict(research_progress)
                if isinstance(research_progress, Mapping)
                else None
            ),
            "research_evidence": research_evidence,
            "knowledge_publication": checkpoint.eval_results.get("knowledge_publication"),
            "budget_used": dict(checkpoint.budget_used),
            "stop_reason": checkpoint.stop_reason,
            "updated_at": checkpoint.updated_at,
        }


WATCH_SCAN_INTERVAL_SECONDS = 60
# rev10/11：旧离线恢复沿 continuation 谱系向 root 回溯的跳数上限
# （超出即零动作；真实 continuation 链远小于此）。
OFFLINE_RECOVERY_LINEAGE_MAX_HOPS = 8
# rev12：execution 的终态语义（recover 扫描只对终态 run 选最新；
# approved/running 中的 run 不参与"最新 terminal"竞选）。
_TERMINAL_EXECUTION_STATUSES = frozenset({"passed", "blocked"})
# close 的等待上限：进行中的观察 cycle 受 run deadline 约束必然在此
# 时间内安全返回；加 margin 覆盖 checkpoint 写入。
WATCH_CYCLE_CLOSE_TIMEOUT_SECONDS = RUN_DEADLINE_SECONDS + 30

# 自动承接（碎片进入 Loop 后的系统接管）：仅承接本时刻之后整理完成的
# 碎片；更早的属历史补接范围，由用户显式触发，后台扫描绝不批量激活。
AUTO_PROPOSE_ORGANIZED_CUTOVER_AT = "2026-09-06T00:00:00+00:00"
AUTO_PROPOSE_SCAN_LIMIT = 100


def _auto_propose_cutover() -> datetime:
    return datetime.fromisoformat(AUTO_PROPOSE_ORGANIZED_CUTOVER_AT)


def _parse_organized_at(value: str) -> datetime | None:
    """整理时刻解析（fail-closed）：非法/缺失/无时区一律返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def propose_organized_vault_intents(
    *,
    vault_root: Any,
    continuation_bridge: Any,
    intent_service: FragmentIntentService,
    limit: int = AUTO_PROPOSE_SCAN_LIMIT,
) -> dict[str, int]:
    """后台自动承接：为已选择进入 Loop 且整理完成的碎片幂等创建 intent。

    用户勾选 ``nigo-loop: true`` 即表示希望系统接管；接管动作（创建
    suggested alignment、生成处理方向建议）不需要也不能再设人工闸门。
    复用既有 ``propose``（确定性 alignment ID + per-fragment 锁 + CAS），
    重复扫描、并发与重启不会重复创建；整理未完成或材料无效时跳过并计数，
    下轮自动重试（等待上游）；早于 cutover 或日期非法的碎片只计数不承接。
    单项失败不中断整轮扫描（失败关闭）。

    枚举不过滤即截取会造成饥饿：此处先完成资格过滤，再按轮转游标对
    候选取 ``limit`` 条处理——waiting/deferred/failed 不能永久占满窗口，
    有限轮内覆盖全部候选；同轮内文件名降序（新碎片优先）。逐条结果写入
    ``intent_service.last_auto_propose_report`` 供展示层如实呈现责任。"""
    from common.supervisor import AdmissionState
    from fragment_loop.continuation_bridge import ContinuationBridgeError
    from fragment_loop.intake import load_fragment

    counts = {
        "scanned": 0,
        "proposed": 0,
        "deferred": 0,
        "waiting": 0,
        "failed": 0,
        "indeterminate": 0,
    }
    report: list[dict[str, str]] = []
    inbox = vault_root / "Notes" / "散记" / "碎片想法"
    if not inbox.is_dir():
        intent_service.last_auto_propose_report = report
        return counts
    known = {str(item.get("fragment_id")) for item in intent_service.list_alignments()}
    candidates: list[Any] = []
    for path in sorted(inbox.glob("*.md"), reverse=True):
        try:
            fragment = load_fragment(path)
        except Exception:
            counts["failed"] += 1
            report.append(
                {
                    "fragment_id": path.stem,
                    "outcome": "failed",
                    "reason": "fragment_unreadable",
                }
            )
            continue
        # 跳过原因逐条可见：未勾选/隐私排除与「无记录」是不同事实，
        # 展示层不得凭缺失记录推断「已选择进入 Loop」。
        if fragment.admission_state is not AdmissionState.REQUESTED:
            report.append(
                {
                    "fragment_id": fragment.fragment_id,
                    "outcome": "skipped",
                    "reason": "not_approved",
                }
            )
            continue
        if fragment.privacy_level in {"sensitive", "restricted"}:
            report.append(
                {
                    "fragment_id": fragment.fragment_id,
                    "outcome": "skipped",
                    "reason": "privacy_excluded",
                }
            )
            continue
        if fragment.fragment_id in known:
            continue
        candidates.append(fragment)
    # 轮转游标：waiting/deferred/failed 不得永久占满窗口——每轮从上次
    # 位置继续取 limit 条，有限轮内覆盖全部候选（同轮内新碎片优先）。
    total = len(candidates)
    if total:
        cursor = int(getattr(intent_service, "_auto_propose_cursor", 0)) % total
        window = [
            candidates[(cursor + offset) % total]
            for offset in range(min(limit, total))
        ]
        intent_service._auto_propose_cursor = (cursor + limit) % total
    else:
        window = []
    for fragment in window:
        counts["scanned"] += 1
        fragment_id = fragment.fragment_id
        try:
            source = continuation_bridge.discover_intent_source(fragment_id)
        except ContinuationBridgeError as error:
            # organized_not_ready / source_mismatch 等：等待上游整理完成或
            # 材料修正，本轮跳过，下轮自动重试；原因逐条可见。
            counts["waiting"] += 1
            report.append(
                {
                    "fragment_id": fragment_id,
                    "outcome": "waiting",
                    "reason": str(error)[:80],
                }
            )
            continue
        # Raw origin is produced only by the server-configured first-intake
        # gate. It has no organized_at: do not invent a completed organizer.
        if source.get("source_origin") != "raw_capture":
            organized_at = _parse_organized_at(str(source.get("organized_at") or ""))
            if organized_at is None:
                # 日期缺失/非法：无法判断承接资格，与「早于 cutover」是不同事实。
                counts["indeterminate"] += 1
                report.append(
                    {
                        "fragment_id": fragment_id,
                        "outcome": "indeterminate",
                        "reason": "organized_at_missing_or_invalid",
                    }
                )
                continue
            if organized_at < _auto_propose_cutover():
                counts["deferred"] += 1
                report.append(
                    {
                        "fragment_id": fragment_id,
                        "outcome": "deferred",
                        "reason": "before_cutover",
                    }
                )
                continue
        try:
            intent_service.propose(
                {
                    "fragment_id": fragment_id,
                    "input_digest": source["input_digest"],
                    "requester": "nigo",
                }
            )
            counts["proposed"] += 1
            report.append(
                {"fragment_id": fragment_id, "outcome": "proposed", "reason": ""}
            )
        except FragmentIntentError as error:
            counts["failed"] += 1
            report.append(
                {
                    "fragment_id": fragment_id,
                    "outcome": "failed",
                    "reason": str(error)[:80],
                }
            )
    intent_service.last_auto_propose_report = report
    return counts


class ResearchWatchCoordinator:
    """最低频同进程 due 协调器（TASK C rev3 自主触发边界）。

    无人打开 UI 也到期推进：daemon 线程按冻结间隔扫描已持久化的
    execution Run，只对 watching 且 due 的 Run 触发一次有界观察 cycle；
    不读取私人正文、不执行模型、不新增服务/数据库/依赖/第二套调度系统。
    并发单飞由 cycle 的 CAS 领取保证（多 evaluator/多进程安全）；
    collection 能力关闭时零副作用；close() 在 runner 冻结上限内等待
    当前 cycle 安全结束，worker 尚存活时绝不谎报关闭。"""

    def __init__(
        self,
        intent_service: FragmentIntentService,
        *,
        interval_seconds: float = WATCH_SCAN_INTERVAL_SECONDS,
        graph_scanner: Callable[[], dict[str, int]] | None = None,
        auto_propose_scanner: Callable[[], dict[str, int]] | None = None,
        knowledge_scanner: Callable[[], object] | None = None,
    ) -> None:
        self.intent_service = intent_service
        self.interval_seconds = interval_seconds
        # rev6 P0-2：同一线程同时承载 intent execution watch 与 v3 Graph
        # watch（不新增第二线程/调度器）；None 时只扫 intent 侧。
        self.graph_scanner = graph_scanner
        # 自动承接：同一线程承载 vault 碎片 intent 自动创建（幂等），
        # 不依赖 UI 打开或人工点击；None 时不扫描。
        self.auto_propose_scanner = auto_propose_scanner
        self.knowledge_scanner = knowledge_scanner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def scan_once(self) -> dict[str, int]:
        """单轮扫描（同步、可测试）：合并 suggested 自动推进、due 恢复、
        旧离线恢复与 Graph 两侧计数；返回计数契约（scanned/resumed）不变。"""
        result = dict(self.intent_service.evaluate_due_watches())
        auto_counts: dict[str, int] = {}
        if self.auto_propose_scanner is not None:
            try:
                # 自动承接先行：本轮新创建的 suggested alignment 可随即由
                # 下方 advance 扫描推进；逐段失败关闭，互不吞没计数。
                auto = self.auto_propose_scanner()
                result["scanned"] += int(auto.get("scanned", 0))
                result["resumed"] += int(auto.get("proposed", 0))
                for key in ("proposed", "deferred", "waiting", "failed", "indeterminate"):
                    auto_counts[f"auto_{key}"] = int(auto.get(key, 0))
            except Exception:
                # 失败关闭：自动承接扫描异常不吞掉 due 结果，下轮再试。
                pass
        try:
            # P1-2 修复：生产桌面链路没有任何单条 alignment GET，自动推进
            # 触发面由本扫描承载（复用既有 maybe_auto_advance，逐项失败关闭）。
            advance = self.intent_service.advance_suggested_alignments()
            result = {
                "scanned": result["scanned"] + advance["scanned"],
                "resumed": result["resumed"] + advance["advanced"],
            }
        except Exception:
            # 失败关闭：suggested 扫描异常不吞掉 due 结果，下轮再试。
            pass
        try:
            recovery = self.intent_service.recover_offline_executions()
            result = {
                "scanned": result["scanned"] + recovery["scanned"],
                "resumed": result["resumed"] + recovery["recovered"],
            }
        except Exception:
            # 失败关闭：恢复扫描异常不吞掉 due 结果，下轮再试。
            pass
        if self.graph_scanner is not None:
            try:
                graph = self.graph_scanner()
                result = {
                    "scanned": result["scanned"] + int(graph.get("scanned", 0)),
                    "resumed": result["resumed"] + int(graph.get("resumed", 0)),
                }
            except Exception:
                # 失败关闭：Graph 侧异常不吞掉 intent 侧结果，下轮再试。
                pass
        try:
            subscription = self.intent_service.resume_subscription_executions()
            result["resumed"] += subscription["resumed"]
        except Exception as error:
            result["subscription_failed"] = 1
            self.last_subscription_error = type(error).__name__
        if self.knowledge_scanner is not None:
            try:
                self.knowledge_scanner()
            except Exception:
                result["knowledge_failed"] = 1
        result.update(auto_counts)
        return result

    def start(self) -> bool:
        if self._thread is not None:
            return False

        def run() -> None:
            while not self._stop.is_set():
                try:
                    self.scan_once()
                except Exception:
                    pass
                self._stop.wait(self.interval_seconds)

        self._thread = threading.Thread(
            target=run, name="fragment-research-watch-due", daemon=True
        )
        self._thread.start()
        return True

    def close(self, *, timeout_seconds: float | None = None) -> bool:
        """干净停止：等待进行中的 scan/cycle 安全结束（默认等待 runner
        冻结上限 run deadline + margin）。

        worker 仍存活时绝不清空线程引用、绝不谎报关闭——返回 False 并
        保留 _thread；真正停止才返回 True。不强杀线程。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(
                timeout=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else WATCH_CYCLE_CLOSE_TIMEOUT_SECONDS
                )
            )
            if self._thread.is_alive():
                return False
            self._thread = None
        return True


__all__ = [
    "ALIGNMENTS_PATH",
    "ALIGNMENT_DECISION_HEADER",
    "ALIGNMENT_ESCALATION_HEADER",
    "ALIGNMENT_HEADER",
    "ALIGNMENT_SPEC",
    "AUTO_PROPOSE_ORGANIZED_CUTOVER_AT",
    "AUTO_PROPOSE_SCAN_LIMIT",
    "CASES_PATH",
    "EPISODES_PATH",
    "EPISODE_CONTINUATION_HEADER",
    "EXECUTION_SPEC",
    "FragmentIntentError",
    "FragmentIntentService",
    "ResearchWatchCoordinator",
    "WATCH_CYCLE_CLOSE_TIMEOUT_SECONDS",
    "WATCH_SCAN_INTERVAL_SECONDS",
    "continuation_id",
    "escalation_id",
    "propose_organized_vault_intents",
]
