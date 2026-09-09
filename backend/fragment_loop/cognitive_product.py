"""Loop V1 Package E: human-readable cognitive product projection.

The persisted cognitive result stays the internal fact object and
``cognitive_draft_view.build_cognitive_draft_view`` stays the only sanitized
display projection.  This module adds the *product* layer on top of that
safe projection — it never touches the internal result, never weakens the
fact contract, and never upgrades evidence:

- input is the persisted ``eval_results`` (re-projected through
  ``build_cognitive_draft_view`` inside this module, so this layer cannot
  bypass the existing evidence checks) plus a caller-assembled synthetic
  product object (title, core judgment, key facts, critical corrections,
  personal connections, blind spots, conclusion, candidate cards, topics);
- output is ``human_markdown`` (fixed section order, Obsidian foldable
  evidence callout, progressive disclosure), ``candidate_cards`` (always
  ``status=candidate`` + ``evidence_level=unverified`` +
  ``promoted_to_asset=False``), a ``graph_projection`` (closed node/edge
  enums, fail-closed on unknown types, duplicate ids, dangling edges, and
  self-loops), a style-independent ``home_projection`` for the console, and
  a structured ``sidecar`` carrying the machine material (hashes, source
  catalog, claim bindings) that must never enter the main reading flow.

Machine material is refused from every human-facing surface: hashes, run
ids, quote catalogs, session identity, machine-form fields and values
(``provider_id``, ``prompt_tokens``, ``ledger_path``, signed URL token
parameters, ...) and internal absolute paths are rejected in the
caller-assembled product fields, and the rendered Markdown plus the home
projection are scanned again before being returned.  Normal prose that
discusses providers, tokens, or ledgers as research subjects is allowed —
only machine shapes are forbidden.  Dynamic text (title, key facts,
corrections, conclusion, source names, locators, excerpts) is normalized
onto a single line before rendering so newlines or control characters can
never break the fixed Markdown/frontmatter structure or inject extra
headings.  This module is pure: no network, no disk scan, no model, no
filesystem writes.  Plan v5 constants and history are not touched; the
product layer versions itself independently.

The validation/build core is also exposed as
``build_cognitive_product_from_view`` so that a trusted upstream
projection other than the persisted ``eval_results`` — currently the
plan v5 → Package E bridge view built by
``fragment_loop.cognitive_product_bridge`` — can reuse exactly this
contract, renderer, graph builder, home projection, and sidecar instead
of growing a second product format.  That seam changes nothing about the
checks below: the caller remains responsible for the evidence discipline
of the view it supplies (the draft view re-validates the persisted fact
object; the bridge view is validated by the strict plan v5 chain), and
every product-field, graph, and surface rule applies identically.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from fragment_loop.cognitive_contract import CREDIBILITIES
from fragment_loop.cognitive_draft_view import build_cognitive_draft_view

PRODUCT_VERSION = "fragment-cognitive-product-v1"
HOME_SCHEMA_VERSION = "fragment-cognitive-home-v1"
GRAPH_VERSION = "fragment-cognitive-graph-v1"

COMPONENT_SLOTS = (
    "summary",
    "key_facts",
    "critical_corrections",
    "personal_connections",
    "blind_spots",
    "candidate_cards",
    "evidence_drawer",
)
AVAILABLE_ACTIONS = ("open_detail", "confirm_draft", "withdraw_draft")

NODE_TYPES = (
    "fragment",
    "source",
    "claim",
    "candidate_knowledge_card",
    "local_note",
    "project",
    "topic",
)
EDGE_TYPES = (
    "references",
    "cites",
    "supports",
    "partially_supports",
    "contradicts",
    "qualifies",
    "mechanism_related_to",
    "architecture_related_to",
    "evaluation_related_to",
    "goal_related_to",
    "applies_to_project",
    "supersedes",
)
# Edges that assert external-fact evidence; they may only point at a
# ``source`` node from a ``claim`` or ``candidate_knowledge_card`` node.
# A local note / project / topic association is never external evidence.
EVIDENTIAL_EDGE_TYPES = ("cites", "supports", "partially_supports", "contradicts")
EVIDENTIAL_FROM_TYPES = ("claim", "candidate_knowledge_card")
CONNECTION_RELATIONS = (
    "references",
    "qualifies",
    "mechanism_related_to",
    "architecture_related_to",
    "evaluation_related_to",
    "goal_related_to",
    "applies_to_project",
)
CONNECTION_KINDS = ("local_note", "project")

VERDICT_EDGE = {
    "supported": "supports",
    "partially_supported": "partially_supports",
    "contradicted": "contradicts",
}
VERDICT_LABELS = {
    "supported": "支持",
    "partially_supported": "部分支持",
    "contradicted": "反对",
    "not_covered": "未覆盖",
}
CREDIBILITY_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "insufficient": "不足",
}
CONNECTION_KIND_LABELS = {"local_note": "本地笔记", "project": "项目"}

MAX_CANDIDATE_CARDS = 5
MAX_CORRECTIONS = 5
MAX_TITLE_LENGTH = 120

_ID_LIKE = re.compile(r"^[A-Za-z0-9_-]+$")
_CARD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HASH_HEX = re.compile(r"[0-9a-fA-F]{64}")
_CJK = re.compile(r"[一-鿿]")

# Patterns that mark machine-only material.  They are forbidden in the
# caller-assembled human-facing fields and re-scanned on the rendered
# Markdown and the home projection (defense in depth); the structured
# sidecar is the only place they may appear.  Only machine *forms* are
# forbidden — hash values, run/session/quote-catalog identifiers,
# snake_case machine fields and values (``provider_id``, ``prompt_tokens``,
# ``ledger_path``, ...), signed URL credential parameters, and internal
# absolute paths.  Normal semantic sentences that discuss providers,
# tokens, or ledgers as research subjects must remain printable.
_FORBIDDEN_SURFACE_PATTERNS = (
    _HASH_HEX,
    re.compile(r"\b[0-9a-fA-F]{40}\b"),
    re.compile(r"\bsha256\s*[:=]", re.IGNORECASE),
    re.compile(r"\brun[_-]?id\b", re.IGNORECASE),
    re.compile(r"\bsession[_-]?identity\b", re.IGNORECASE),
    re.compile(r"\bquote[_-]?catalog\b", re.IGNORECASE),
    re.compile(r"\bprovider[_-](?:id|name|key)\b", re.IGNORECASE),
    re.compile(r"\b(?:prompt|completion|total)[_-]tokens\b", re.IGNORECASE),
    re.compile(r"\btoken[_-](?:count|usage|id|key|secret)\b", re.IGNORECASE),
    re.compile(r"\b(?:access|refresh|api|bearer)[_-](?:token|key|secret)\b", re.IGNORECASE),
    re.compile(r"\bledger[_-](?:path|id|file|entry)\b", re.IGNORECASE),
    re.compile(r"[?&](?:token|key|sig|signature|credential)=", re.IGNORECASE),
    re.compile(r"/(?:Users|home|private)/"),
    re.compile(r"~/"),
    re.compile(r"\b[A-Za-z]:[\\/]"),
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]+")
_WHITESPACE_RUN = re.compile(r"\s+")


class CognitiveProductError(ValueError):
    """The cognitive product object violates the product projection contract."""


# ------------------------------------------------------------ narrowing tools


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if _is_mapping(value):
        assert isinstance(value, Mapping)
        return value
    return None


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in (_as_mapping(entry) for entry in value) if item is not None]


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _str_list(value: object) -> tuple[str, ...] | None:
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, str) and item.strip() for item in value
    ):
        return tuple(item.strip() for item in value if isinstance(item, str))
    return None


def _surface_is_clean(value: object) -> bool:
    """True when a human-facing string carries no machine-only material."""
    if not isinstance(value, str):
        return False
    return not any(pattern.search(value) for pattern in _FORBIDDEN_SURFACE_PATTERNS)


def _inline_text(value: object) -> str:
    """Collapse dynamic text onto a single structural line.

    Control characters are dropped and every whitespace run (newlines,
    tabs, ...) becomes one space, so caller-assembled text and evidence
    text (source names, locators, excerpts) can never break the fixed
    Markdown/frontmatter structure or inject extra headings.
    """
    return _WHITESPACE_RUN.sub(" ", _CONTROL_CHARS.sub("", str(value))).strip()


def _surface_error(label: str, value: object) -> list[str]:
    text = _text(value)
    if text is None:
        return [f"{label} 必须是非空字符串"]
    if not _surface_is_clean(text):
        return [
            f"{label} 夹带机器材料（哈希、运行标识、机器形态字段如 "
            "provider_id/prompt_tokens/ledger_path、带凭据的 URL 参数或内部绝对路径），"
            "不得进入人类可读面"
        ]
    return []


def _safe_ref(value: object) -> str | None:
    """A synthetic relative logical reference, same shape as record_ref."""
    ref = _text(value)
    if ref is None:
        return None
    if ref.startswith(("/", "~")) or "://" in ref or "\\" in ref:
        return None
    if any(component in ("", ".", "..") for component in ref.split("/")):
        return None
    return ref


# ------------------------------------------------------------ field validation


def _title_errors(title_raw: object, fragment_text: str) -> list[str]:
    title = _text(title_raw)
    if title is None:
        return ["title 必须是非空的语义标题"]
    errors: list[str] = []
    if len(title) > MAX_TITLE_LENGTH:
        errors.append(f"title 不得超过 {MAX_TITLE_LENGTH} 字")
    if "://" in title or title.startswith("www."):
        errors.append("title 不得用 URL 充当语义标题")
    if _HASH_HEX.search(title):
        errors.append("title 不得夹带哈希")
    if fragment_text.startswith(title) and (
        len(title) <= 30 or title == fragment_text[:30]
    ):
        errors.append("title 不得直接用碎片截断文本充当语义标题")
    if _ID_LIKE.match(title) and any(char.isdigit() for char in title) and not _CJK.search(title):
        errors.append("title 不得用机器 ID 充当语义标题")
    if not _surface_is_clean(title):
        errors.append("title 夹带机器材料，不得进入人类可读面")
    return errors


def _core_judgment_errors(raw: object) -> list[str]:
    judgment = _as_mapping(raw)
    if judgment is None:
        return ["core_judgment 必须是对象"]
    errors: list[str] = []
    for field in ("statement", "user_value", "suggested_action"):
        errors.extend(_surface_error(f"core_judgment.{field}", judgment.get(field)))
    credibility = judgment.get("credibility")
    if not isinstance(credibility, str) or credibility not in CREDIBILITIES:
        errors.append(f"core_judgment.credibility 必须是以下之一: {', '.join(CREDIBILITIES)}")
    return errors


def _key_fact_errors(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)) or not 3 <= len(raw) <= 5:
        return ["key_facts 必须是 3～5 条关键事实"]
    errors: list[str] = []
    for index, item in enumerate(raw):
        errors.extend(_surface_error(f"key_facts[{index}]", item))
    return errors


def _correction_errors(
    raw: object, claims: list[Mapping[str, object]]
) -> list[str]:
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_CORRECTIONS:
        return [f"critical_corrections 必须是 0～{MAX_CORRECTIONS} 条关键纠偏"]
    claim_ids = {
        str(claim["claim_id"]) for claim in claims if isinstance(claim.get("claim_id"), str)
    }
    errors: list[str] = []
    covered: set[str] = set()
    for index, entry_raw in enumerate(raw):
        label = f"critical_corrections[{index}]"
        entry = _as_mapping(entry_raw)
        if entry is None:
            errors.append(f"{label} 必须是对象")
            continue
        unknown_keys = set(entry) - {"misreading", "correction", "basis", "claim_refs"}
        if unknown_keys:
            errors.append(f"{label} 含未知字段: {', '.join(sorted(unknown_keys))}")
        for field in ("misreading", "correction", "basis"):
            errors.extend(_surface_error(f"{label}.{field}", entry.get(field)))
        refs = _str_list(entry.get("claim_refs"))
        if refs is None:
            errors.append(f"{label}.claim_refs 必须是字符串列表（允许空列表）")
            continue
        for ref in refs:
            if ref not in claim_ids:
                errors.append(f"{label}.claim_refs 引用了不存在的主张: {ref}")
            else:
                covered.add(ref)
    # A partially supported or contradicted claim is exactly where mainstream
    # misreadings grow; every such claim must carry at least one correction.
    for claim in claims:
        verdict = claim.get("verdict")
        claim_id = claim.get("claim_id")
        if verdict in ("partially_supported", "contradicted") and isinstance(
            claim_id, str
        ):
            if claim_id not in covered:
                errors.append(
                    f"主张 {claim_id} 判定为 {verdict}，必须至少有一条关键纠偏引用它，"
                    "不得让常见误读（如把小样本专家判定扩大成真实完成）无人纠正"
                )
    return errors


def _connection_errors(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return ["personal_connections 必须是列表（允许空列表）"]
    errors: list[str] = []
    for index, entry_raw in enumerate(raw):
        label = f"personal_connections[{index}]"
        entry = _as_mapping(entry_raw)
        if entry is None:
            errors.append(f"{label} 必须是对象")
            continue
        unknown_keys = set(entry) - {"text", "target_kind", "target_ref", "relation"}
        if unknown_keys:
            errors.append(f"{label} 含未知字段: {', '.join(sorted(unknown_keys))}")
        errors.extend(_surface_error(f"{label}.text", entry.get("text")))
        kind = entry.get("target_kind")
        if not isinstance(kind, str) or kind not in CONNECTION_KINDS:
            errors.append(f"{label}.target_kind 必须是以下之一: {', '.join(CONNECTION_KINDS)}")
        if _safe_ref(entry.get("target_ref")) is None:
            errors.append(f"{label}.target_ref 必须是安全的合成相对逻辑引用")
        relation = entry.get("relation")
        if not isinstance(relation, str) or relation not in CONNECTION_RELATIONS:
            errors.append(
                f"{label}.relation 必须是非证据型关联: {', '.join(CONNECTION_RELATIONS)}"
            )
        elif relation == "applies_to_project" and kind != "project":
            errors.append(f"{label}.relation=applies_to_project 时 target_kind 必须是 project")
        if isinstance(relation, str) and relation in EVIDENTIAL_EDGE_TYPES:
            errors.append(f"{label} 本地关联不得使用证据型边（本地笔记不是外部事实证据）")
    return errors


def _blind_spot_errors(raw: object) -> list[str]:
    if _str_list(raw) is None:
        return ["blind_spots 必须是非空字符串列表（允许空列表）"]
    return []


def _conclusion_errors(raw: object) -> list[str]:
    conclusion = _as_mapping(raw)
    if conclusion is None:
        return ["conclusion 必须是对象"]
    errors: list[str] = []
    for field in ("summary", "next_step"):
        errors.extend(_surface_error(f"conclusion.{field}", conclusion.get(field)))
    return errors


def _candidate_card_errors(
    raw: object, resolvable_refs: set[str]
) -> list[str]:
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_CANDIDATE_CARDS:
        return [f"candidate_cards 必须是 0～{MAX_CANDIDATE_CARDS} 张候选知识卡"]
    allowed_keys = {
        "card_id",
        "title",
        "knowledge",
        "why_important",
        "scope",
        "evidence_refs",
        "supersedes",
    }
    errors: list[str] = []
    seen: set[str] = set()
    supersede_pairs: list[tuple[str, str]] = []
    for index, entry_raw in enumerate(raw):
        label = f"candidate_cards[{index}]"
        entry = _as_mapping(entry_raw)
        if entry is None:
            errors.append(f"{label} 必须是对象")
            continue
        unknown_keys = set(entry) - allowed_keys
        if unknown_keys:
            # status / evidence_level / promoted_to_asset are forced by this
            # layer; a caller supplying them is trying to upgrade a candidate
            # into an asset and fails closed.
            errors.append(
                f"{label} 含未知或被强制的字段: {', '.join(sorted(unknown_keys))}"
                "（候选状态由本层固定，不得由调用方标成已验证资产）"
            )
        card_id = entry.get("card_id")
        if not isinstance(card_id, str) or not _CARD_ID.match(card_id):
            errors.append(f"{label}.card_id 必须是稳定的短标识符")
        elif card_id in seen:
            errors.append(f"{label}.card_id 重复: {card_id}")
        else:
            seen.add(card_id)
        for field in ("title", "knowledge", "why_important", "scope"):
            errors.extend(_surface_error(f"{label}.{field}", entry.get(field)))
        refs = _str_list(entry.get("evidence_refs"))
        if refs is None or not refs:
            errors.append(f"{label}.evidence_refs 必须至少引用一个可解析依据")
        else:
            for ref in refs:
                if ref not in resolvable_refs:
                    errors.append(f"{label}.evidence_refs 引用了不存在的依据: {ref}")
        supersedes = entry.get("supersedes")
        if supersedes is not None:
            if not isinstance(supersedes, str) or not _CARD_ID.match(supersedes):
                errors.append(f"{label}.supersedes 必须是另一张候选卡的 card_id")
            elif isinstance(card_id, str) and supersedes == card_id:
                errors.append(f"{label}.supersedes 不得指向自身（非法自环）")
            elif isinstance(card_id, str):
                supersede_pairs.append((card_id, supersedes))
    card_ids = seen
    for card_id, target in supersede_pairs:
        if target not in card_ids:
            errors.append(
                f"candidate_cards 中 {card_id} 的 supersedes 指向不存在的候选卡: {target}"
            )
    return errors


def _topic_errors(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return ["topics 必须是列表（允许空列表）"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry_raw in enumerate(raw):
        label = f"topics[{index}]"
        entry = _as_mapping(entry_raw)
        if entry is None:
            errors.append(f"{label} 必须是对象")
            continue
        unknown_keys = set(entry) - {"topic_id", "label"}
        if unknown_keys:
            errors.append(f"{label} 含未知字段: {', '.join(sorted(unknown_keys))}")
        topic_id = _text(entry.get("topic_id"))
        if topic_id is None or len(topic_id) > 64 or any(ch.isspace() for ch in topic_id):
            errors.append(f"{label}.topic_id 必须是不含空白的短标识符")
        elif topic_id in seen:
            errors.append(f"{label}.topic_id 重复: {topic_id}")
        else:
            seen.add(topic_id)
        errors.extend(_surface_error(f"{label}.label", entry.get("label")))
    return errors


# ------------------------------------------------------------ graph validation


def validate_graph_projection(graph: object) -> tuple[str, ...]:
    """Validate one graph-ready projection; legal input returns an empty tuple.

    Fail-closed on: unknown node or edge types, duplicate node ids, dangling
    edges, self-loops, evidential edges that do not point at a ``source``
    node (a local note association is never external evidence), and any
    candidate card node that does not carry the fixed candidate attributes.
    """
    mapping = _as_mapping(graph)
    if mapping is None:
        return ("graph_projection 必须是对象",)
    errors: list[str] = []
    if mapping.get("graph_version") != GRAPH_VERSION:
        errors.append(f"graph_projection.graph_version 必须是 {GRAPH_VERSION}")
    nodes_raw = mapping.get("nodes")
    edges_raw = mapping.get("edges")
    if not isinstance(nodes_raw, (list, tuple)):
        return tuple(errors + ["graph_projection.nodes 必须是列表"])
    if not isinstance(edges_raw, (list, tuple)):
        return tuple(errors + ["graph_projection.edges 必须是列表"])
    nodes: dict[str, Mapping[str, object]] = {}
    for index, node_raw in enumerate(nodes_raw):
        label = f"graph_projection.nodes[{index}]"
        node = _as_mapping(node_raw)
        if node is None:
            errors.append(f"{label} 必须是对象")
            continue
        node_id = node.get("id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(f"{label}.id 必须是非空字符串")
        elif node_id in nodes:
            errors.append(f"{label}.id 重复: {node_id}")
        else:
            nodes[node_id] = node
        if not isinstance(node_type, str) or node_type not in NODE_TYPES:
            errors.append(f"{label}.type 未知节点类型: {node_type}")
        if node_type == "candidate_knowledge_card":
            if (
                node.get("status") != "candidate"
                or node.get("evidence_level") != "unverified"
                or node.get("promoted_to_asset") is not False
            ):
                errors.append(
                    f"{label} 候选知识卡必须固定 status=candidate、"
                    "evidence_level=unverified、promoted_to_asset=false，"
                    "不得被表示为正式资产"
                )
    for index, edge_raw in enumerate(edges_raw):
        label = f"graph_projection.edges[{index}]"
        edge = _as_mapping(edge_raw)
        if edge is None:
            errors.append(f"{label} 必须是对象")
            continue
        edge_type = edge.get("type")
        if not isinstance(edge_type, str) or edge_type not in EDGE_TYPES:
            errors.append(f"{label}.type 未知边类型: {edge_type}")
        from_id = edge.get("from")
        to_id = edge.get("to")
        from_node = nodes.get(from_id) if isinstance(from_id, str) else None
        to_node = nodes.get(to_id) if isinstance(to_id, str) else None
        if from_node is None:
            errors.append(f"{label}.from 悬空边: {from_id}")
        if to_node is None:
            errors.append(f"{label}.to 悬空边: {to_id}")
        if isinstance(from_id, str) and from_id == to_id:
            errors.append(f"{label} 非法自环: {from_id}")
        if (
            isinstance(edge_type, str)
            and edge_type in EVIDENTIAL_EDGE_TYPES
            and from_node is not None
            and to_node is not None
        ):
            if from_node.get("type") not in EVIDENTIAL_FROM_TYPES:
                errors.append(
                    f"{label} 证据型边只能从主张或候选知识卡出发，"
                    f"不能从 {from_node.get('type')} 出发（本地关联不是外部事实证据）"
                )
            # 候选卡的 cites 可以指向主张（主张自身已绑定来源）；其余证据型
            # 边必须指向 source 节点，本地笔记不得被当作外部事实证据。
            to_type = to_node.get("type")
            cites_card_to_claim = (
                edge_type == "cites"
                and from_node.get("type") == "candidate_knowledge_card"
                and to_type == "claim"
            )
            if to_type != "source" and not cites_card_to_claim:
                errors.append(
                    f"{label} 证据型边必须指向 source 节点，"
                    f"不能指向 {to_type}（本地笔记不得被当作外部事实证据）"
                )
        if edge_type == "supersedes" and from_node is not None and to_node is not None:
            if from_node.get("type") != to_node.get("type") or from_node.get(
                "type"
            ) not in ("candidate_knowledge_card", "claim"):
                errors.append(f"{label} supersedes 只能连接同类型的候选卡或主张")
    return tuple(errors)


# ------------------------------------------------------------ view extraction


def _view_claims(view: Mapping[str, object]) -> list[Mapping[str, object]]:
    research = _as_mapping(view.get("research"))
    if research is None:
        return []
    return _as_mapping_list(research.get("v1_claims"))


def _view_sources(view: Mapping[str, object]) -> list[Mapping[str, object]]:
    research = _as_mapping(view.get("research"))
    if research is None:
        return []
    return _as_mapping_list(research.get("sources"))


def _product_errors(
    view: Mapping[str, object], product: Mapping[str, object]
) -> list[str]:
    fragment_text = str(view.get("fragment_text", ""))
    claims = _view_claims(view)
    sources = _view_sources(view)
    resolvable_refs = {
        str(claim["claim_id"]) for claim in claims if isinstance(claim.get("claim_id"), str)
    } | {
        str(source["source_id"])
        for source in sources
        if isinstance(source.get("source_id"), str)
    }
    errors: list[str] = []
    errors.extend(_title_errors(product.get("title"), fragment_text))
    errors.extend(_core_judgment_errors(product.get("core_judgment")))
    errors.extend(_key_fact_errors(product.get("key_facts")))
    errors.extend(_correction_errors(product.get("critical_corrections"), claims))
    errors.extend(_connection_errors(product.get("personal_connections")))
    errors.extend(_blind_spot_errors(product.get("blind_spots")))
    errors.extend(_conclusion_errors(product.get("conclusion")))
    errors.extend(_candidate_card_errors(product.get("candidate_cards"), resolvable_refs))
    errors.extend(_topic_errors(product.get("topics")))
    fragment_ref = product.get("fragment_ref")
    if fragment_ref is not None and _safe_ref(fragment_ref) is None:
        errors.append("fragment_ref 若提供必须是安全的合成相对逻辑引用")
    updated_at = product.get("updated_at")
    if updated_at is not None and _text(updated_at) is None:
        errors.append("updated_at 若提供必须是非空字符串")
    return errors


# ------------------------------------------------------------ graph building


def _build_graph(
    view: Mapping[str, object],
    product: Mapping[str, object],
    cards: list[dict[str, object]],
) -> dict[str, object]:
    nodes: list[dict[str, object]] = [
        {"id": "fragment", "type": "fragment", "label": product["title"]}
    ]
    edges: list[dict[str, object]] = []
    claims = _view_claims(view)
    for source in _view_sources(view):
        nodes.append(
            {
                "id": f"source:{source['source_id']}",
                "type": "source",
                "label": source.get("name"),
            }
        )
    for claim in claims:
        claim_id = str(claim["claim_id"])
        nodes.append(
            {
                "id": f"claim:{claim_id}",
                "type": "claim",
                "label": claim.get("text"),
                "verdict": claim.get("verdict"),
            }
        )
        edges.append({"from": "fragment", "to": f"claim:{claim_id}", "type": "references"})
        verdict = str(claim.get("verdict"))
        edge_type = VERDICT_EDGE.get(verdict)
        if edge_type is not None:
            refs = claim.get("evidence_refs")
            for ref in refs if isinstance(refs, list) else []:
                edges.append(
                    {"from": f"claim:{claim_id}", "to": f"source:{ref}", "type": edge_type}
                )
    for card in cards:
        card_id = str(card["card_id"])
        nodes.append(
            {
                "id": f"card:{card_id}",
                "type": "candidate_knowledge_card",
                "label": card["title"],
                "status": "candidate",
                "evidence_level": "unverified",
                "promoted_to_asset": False,
            }
        )
        refs = card.get("evidence_refs")
        for ref in refs if isinstance(refs, list) else []:
            target_prefix = "claim" if any(
                str(claim["claim_id"]) == str(ref) for claim in claims
            ) else "source"
            edges.append(
                {"from": f"card:{card_id}", "to": f"{target_prefix}:{ref}", "type": "cites"}
            )
        supersedes = card.get("supersedes")
        if isinstance(supersedes, str):
            edges.append(
                {"from": f"card:{card_id}", "to": f"card:{supersedes}", "type": "supersedes"}
            )
    for connection in _as_mapping_list(product.get("personal_connections")):
        kind = str(connection["target_kind"])
        node_id = f"{kind}:{connection['target_ref']}"
        if not any(node["id"] == node_id for node in nodes):
            nodes.append(
                {"id": node_id, "type": kind, "label": connection.get("text")}
            )
        edges.append(
            {
                "from": "fragment",
                "to": node_id,
                "type": str(connection["relation"]),
            }
        )
    for topic in _as_mapping_list(product.get("topics")):
        node_id = f"topic:{topic['topic_id']}"
        nodes.append({"id": node_id, "type": "topic", "label": topic.get("label")})
        edges.append({"from": "fragment", "to": node_id, "type": "goal_related_to"})
    graph: dict[str, object] = {
        "graph_version": GRAPH_VERSION,
        "nodes": nodes,
        "edges": edges,
    }
    errors = validate_graph_projection(graph)
    if errors:  # unreachable for builder output; fail closed anyway
        raise CognitiveProductError("graph 投影构建失败：" + "；".join(errors))
    return graph


# ------------------------------------------------------------ markdown render


def _frontmatter(fields: tuple[tuple[str, object], ...]) -> str:
    header = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in fields
    )
    return f"---\n{header}\n---\n"


def _render_markdown(
    view: Mapping[str, object],
    product: Mapping[str, object],
    cards: list[dict[str, object]],
    home: Mapping[str, object],
) -> str:
    title = _inline_text(product["title"])
    judgment = product["core_judgment"]
    assert isinstance(judgment, Mapping)
    conclusion = product["conclusion"]
    assert isinstance(conclusion, Mapping)
    blind_spots = [
        _inline_text(spot) for spot in (_str_list(product.get("blind_spots")) or ())
    ]
    primary_label = _inline_text(home["primary_topic"])
    statement = _inline_text(judgment["statement"])
    user_value = _inline_text(judgment["user_value"])
    suggested_action = _inline_text(judgment["suggested_action"])
    fields: list[tuple[str, object]] = [
        ("type", "碎片认知结果"),
        ("title", title),
        ("thought_category", primary_label or "未归类"),
        ("thought_status", "provisional" if blind_spots else "concluded"),
        ("thought_unknowns", blind_spots),
        ("content_lifecycle", "draft"),
        ("lifecycle_status", "draft"),
        ("evidence_level", "unverified"),
        ("user_confirmed", False),
        ("promoted_to_asset", False),
        ("home_schema_version", HOME_SCHEMA_VERSION),
        ("core_judgment", statement),
        ("user_value", user_value),
        ("credibility", str(judgment["credibility"])),
        ("candidate_card_count", len(cards)),
        ("unresolved_question_count", len(blind_spots)),
        ("primary_topic", primary_label),
        ("topic_ids", list(home["topic_ids"]) if isinstance(home["topic_ids"], list) else []),
        ("available_actions", list(AVAILABLE_ACTIONS)),
        ("component_slots", list(COMPONENT_SLOTS)),
        ("product_version", PRODUCT_VERSION),
    ]
    fragment_ref = _safe_ref(product.get("fragment_ref"))
    if fragment_ref is not None:
        fields.append(("source_fragment", fragment_ref))
    updated_at = _text(product.get("updated_at"))
    if updated_at is not None:
        fields.append(("thought_updated_at", updated_at))

    credibility = str(judgment["credibility"])
    lines: list[str] = [
        f"# {title}",
        "",
        "> [!note] 状态：draft 草稿 · 证据等级 unverified · 尚未人工确认",
        "> 本页只保留人类可读内容；机器标识、哈希与运行元数据在结构化 sidecar 中。",
        "",
        "## 核心判断",
        "",
        f"- 一句话判断：{statement}",
        f"- 对你的价值：{user_value}",
        f"- 可信程度：{CREDIBILITY_LABELS.get(credibility, credibility)}（{credibility}）",
        f"- 建议动作：{suggested_action}",
        "",
        "## 关键事实",
        "",
    ]
    facts = product["key_facts"]
    assert isinstance(facts, (list, tuple))
    lines.extend(f"- {_inline_text(fact)}" for fact in facts)
    lines += ["", "## 关键纠偏", ""]
    corrections = _as_mapping_list(product.get("critical_corrections"))
    if corrections:
        for index, correction in enumerate(corrections, start=1):
            lines += [
                f"### 纠偏 {index}：{_inline_text(correction['misreading'])}",
                "",
                f"- 更准确的结论：{_inline_text(correction['correction'])}",
                f"- 依据：{_inline_text(correction['basis'])}",
                "",
            ]
    else:
        lines.append("- 本条没有需要纠偏的常见误读。")
    lines += ["", "## 与你已有的记忆／知识库／项目的连接", ""]
    connections = _as_mapping_list(product.get("personal_connections"))
    if connections:
        for connection in connections:
            kind = str(connection["target_kind"])
            lines.append(
                f"- {_inline_text(connection['text'])}"
                f"（{CONNECTION_KIND_LABELS.get(kind, kind)}：{connection['target_ref']}）"
            )
    else:
        lines.append("- 暂未发现与既有记忆／知识库／项目的连接。")
    lines += ["", "## 盲区与未知", ""]
    if blind_spots:
        lines.extend(f"- {spot}" for spot in blind_spots)
    else:
        lines.append("- 未发现实质盲区与未知。")
    lines += [
        "",
        "## 当前结论与下一步",
        "",
        f"- 当前结论：{_inline_text(conclusion['summary'])}",
        f"- 下一步：{_inline_text(conclusion['next_step'])}",
        "",
        "> [!evidence]- 来源与证据（默认折叠，点击展开）",
    ]
    drawer: list[str] = ["> #### 来源"]
    sources = _view_sources(view)
    if sources:
        for source in sources:
            # 来源名称、locator、excerpt 等证据文本同样归一化为单行，
            # 不得注入额外标题、callout 或 frontmatter 边界。
            drawer.append(
                f"> - **{_inline_text(source.get('name'))}**"
                f"（真实性 {_inline_text(source.get('authenticity'))} · "
                f"{_inline_text(source.get('relevance'))} 相关 · "
                f"{_inline_text(source.get('source_tier'))}）："
                f"{_inline_text(source.get('locator'))}"
            )
            drawer.append(f">   - 证据片段：{_inline_text(source.get('evidence_excerpt'))}")
    else:
        drawer.append("> - 本条没有外部来源（直接路线或未接入检索）。")
    drawer.append("> #### 主张判定")
    claims = _view_claims(view)
    if claims:
        for claim in claims:
            verdict = str(claim.get("verdict"))
            drawer.append(
                f"> - {_inline_text(claim.get('text'))}："
                f"{VERDICT_LABELS.get(verdict, verdict)}"
                f"（置信度 {_inline_text(claim.get('confidence'))}）"
            )
    else:
        drawer.append("> - 本条没有主张级判定（直接路线）。")
    if cards:
        drawer.append("> #### 候选知识卡依据")
        for card in cards:
            refs = card.get("evidence_refs")
            ref_text = "、".join(str(ref) for ref in refs) if isinstance(refs, list) else ""
            drawer.append(f"> - {card['title']} → 依据 {ref_text}（候选，未验证）")
    lines.extend(drawer)
    return _frontmatter(tuple(fields)) + "\n" + "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------ public entry


def build_cognitive_product(
    eval_results: Mapping[str, object],
    product: Mapping[str, object],
) -> dict[str, Any]:
    """Validate and project one cognitive product bundle; fail closed on drift.

    The persisted ``eval_results`` is first re-projected through
    ``build_cognitive_draft_view`` — a drifted, non-unverified, or invalid
    persisted result can never reach this layer.  The caller-assembled
    ``product`` object is then validated against that safe view (semantic
    title, correction coverage of every partially supported or contradicted
    claim, resolvable card evidence refs, non-evidential local connections).
    Any violation raises ``CognitiveProductError`` with zero output.
    """
    view = build_cognitive_draft_view(eval_results)
    if view is None:
        raise CognitiveProductError("持久化认知结果未通过现有安全投影，拒绝生成产品投影")
    return build_cognitive_product_from_view(view, product)


def build_cognitive_product_from_view(
    view: Mapping[str, object],
    product: Mapping[str, object],
) -> dict[str, Any]:
    """Validate and project one product bundle from an already-sanitized view.

    The ``view`` must come from a trusted upstream projection — either
    ``build_cognitive_draft_view`` (which re-validates the persisted fact
    object) or the plan v5 bridge view (validated by the strict plan v5
    chain in ``fragment_loop.cognitive_product_bridge``).  This function
    applies the full product contract unchanged: field validation against
    the view, fixed candidate state, graph validation, and the final
    machine-material surface scans.  Any violation raises
    ``CognitiveProductError`` with zero output.
    """
    if not _is_mapping(view):
        raise CognitiveProductError("view 必须是上游安全投影对象")
    if not _is_mapping(product):
        raise CognitiveProductError("product 必须是调用方组装的产品对象")
    errors = _product_errors(view, product)
    if errors:
        raise CognitiveProductError("认知产品对象违反产品投影契约：" + "；".join(errors))

    fragment_text = str(view["fragment_text"])
    item_id = f"cognitive-{hashlib.sha256(fragment_text.encode('utf-8')).hexdigest()[:20]}"
    topics = _as_mapping_list(product.get("topics"))
    topic_ids = [str(topic["topic_id"]) for topic in topics]
    primary_topic = _inline_text(topics[0]["label"]) if topics else ""
    blind_spots = list(_str_list(product.get("blind_spots")) or ())
    judgment = product["core_judgment"]
    assert isinstance(judgment, Mapping)

    cards: list[dict[str, object]] = []
    for entry in _as_mapping_list(product.get("candidate_cards")):
        # _str_list 接受 list 与 tuple；候选卡构建必须使用同一契约，
        # 否则合法的 tuple evidence_refs 会被静默清空。
        card_refs = [str(ref) for ref in (_str_list(entry.get("evidence_refs")) or ())]
        card: dict[str, object] = {
            "card_id": str(entry["card_id"]),
            "title": _inline_text(entry["title"]),
            "knowledge": _inline_text(entry["knowledge"]),
            "why_important": _inline_text(entry["why_important"]),
            "scope": _inline_text(entry["scope"]),
            "evidence_refs": card_refs,
            "status": "candidate",
            "evidence_level": "unverified",
            "promoted_to_asset": False,
        }
        if isinstance(entry.get("supersedes"), str):
            card["supersedes"] = str(entry["supersedes"])
        cards.append(card)

    home_projection: dict[str, object] = {
        "schema_version": HOME_SCHEMA_VERSION,
        "item_id": item_id,
        "title": _inline_text(product["title"]),
        "core_judgment": _inline_text(judgment["statement"]),
        "user_value": _inline_text(judgment["user_value"]),
        "credibility": str(judgment["credibility"]),
        "lifecycle_status": "draft",
        "evidence_level": "unverified",
        "candidate_card_count": len(cards),
        "unresolved_question_count": len(blind_spots),
        "primary_topic": primary_topic,
        "topic_ids": topic_ids,
        "available_actions": list(AVAILABLE_ACTIONS),
        "component_slots": list(COMPONENT_SLOTS),
    }
    # 同源键：主页投影携带安全的合成相对 fragment_ref，供主页按来源碎片去重。
    fragment_ref = _safe_ref(product.get("fragment_ref"))
    if fragment_ref is not None:
        home_projection["source_fragment"] = fragment_ref

    graph_projection = _build_graph(view, product, cards)
    human_markdown = _render_markdown(view, product, cards, home_projection)

    # Final surface scan: the human-readable Markdown and the home projection
    # must carry no machine-only material, however the caller assembled it.
    for pattern in _FORBIDDEN_SURFACE_PATTERNS:
        if pattern.search(human_markdown):
            raise CognitiveProductError(
                f"human_markdown 夹带机器材料（{pattern.pattern}），拒绝输出"
            )
    serialized_home = json.dumps(home_projection, ensure_ascii=False)
    for pattern in _FORBIDDEN_SURFACE_PATTERNS:
        if pattern.search(serialized_home):
            raise CognitiveProductError(
                f"home_projection 夹带机器材料（{pattern.pattern}），拒绝输出"
            )

    claims = _view_claims(view)
    sidecar: dict[str, object] = {
        "sidecar_version": PRODUCT_VERSION,
        "item_id": item_id,
        "fragment_sha256": hashlib.sha256(fragment_text.encode("utf-8")).hexdigest(),
        "route": view.get("route"),
        "source_catalog": [
            {
                "source_id": source.get("source_id"),
                "name": source.get("name"),
                "locator": source.get("locator"),
                "authenticity": source.get("authenticity"),
                "relevance": source.get("relevance"),
                "independence": source.get("independence"),
                "source_tier": source.get("source_tier"),
                "excerpt_sha256": hashlib.sha256(
                    str(source.get("evidence_excerpt", "")).encode("utf-8")
                ).hexdigest(),
            }
            for source in _view_sources(view)
        ],
        "claim_bindings": [
            {
                "claim_id": claim.get("claim_id"),
                "verdict": claim.get("verdict"),
                "confidence": claim.get("confidence"),
                "evidence_refs": claim.get("evidence_refs"),
                "independent_verification": claim.get("independent_verification"),
            }
            for claim in claims
        ],
        "candidate_card_refs": [
            {"card_id": card["card_id"], "evidence_refs": card["evidence_refs"]}
            for card in cards
        ],
    }
    return {
        "product_version": PRODUCT_VERSION,
        "human_markdown": human_markdown,
        "candidate_cards": cards,
        "graph_projection": graph_projection,
        "home_projection": home_projection,
        "sidecar": sidecar,
    }
