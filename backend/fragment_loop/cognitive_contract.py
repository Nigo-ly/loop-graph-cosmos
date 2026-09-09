"""碎片信息认知链 R1-A：离线纯内容契约（revision 5）。

只负责认知结果的契约校验与呈现。输入完全由调用方手写合成，本模块不生成
任何语义内容、不调用模型、不联网、不读写文件，也不接入
`MinimumValueService` 或 Loop Core。对应权威设计
`FRAGMENT-INFORMATION-COGNITIVE-LOOP.md` 第十二节第 1 阶段（离线合成验证）。

契约要点：

- 原文逐字保留：任何 `source_quote`（摘要、结构推断、逐条字面信息与每条
  V1 主张）都必须是 `fragment_text` 的逐字子串；字面信息 text 必须与
  source_quote 完全一致；摘要必须带非空 transformation_basis；
- 研究路线必须携带研究字段；直接路线合法省略，但若夹带 `research` 字段
  必须拒绝，禁止隐藏研究结果；
- 所有证据引用、V1 引用、V2 引用必须能在同一结果对象内解析；每条 V1
  主张的推导拆为非空 claim_derivation（原文→主张）与 verdict_basis
  （主张→判定）；
- 非「未覆盖」主张至少引用一个已核验（authenticity=verified）且直接相关
  （relevance=direct）的来源；效果类高置信主张至少引用一个已核验、直接
  相关且独立的来源；「未覆盖」主张必须 evidence_refs 为空且
  confidence=low；
- 来源卡必须显式携带 source_tier（primary／secondary）、version、非空
  locator、acquisition_method（封闭枚举：synthetic_fixture 或 R1-P 的
  public_search_adapter；新增枚举不自动提升真实性，公开来源仍只能携带
  unverified 或经 R1-J 核验链明确给出的状态）、verification_basis、非空
  evidence_excerpt 及其精确 excerpt_sha256；
  verified 不得只是自我声明；
- 每条 V1 主张的 evidence_support 必须与 evidence_refs 顺序精确一致，并把
  每个来源绑定到 evidence_excerpt 中的逐字 source_quote、判定关系与依据；
  supported／partially_supported／contradicted 的关系必须与判定一致，
  not_covered 必须保持空映射；
- 反证发现与独立验证新增引用也必须通过 evidence_support 绑定到来源卡中的
  逐字 evidence_excerpt；独立验证复用主张已绑定来源时无需重复保存同一映射；
- 每条 V1 主张必须携带 independent_verification：status（verified／
  partial／not_verified）、mode（independent_source／recalculation／
  evidence_chain_reconstruction／independent_review／none）、非空 basis
  与可解析 evidence_refs；mode=none 当且仅当 status=not_verified 且引用
  为空；independent_source 必须至少引用一个已核验、直接相关且独立的来
  源；recalculation 必须至少引用一个已核验、直接相关且
  source_type=dataset 的来源；evidence_chain_reconstruction 必须至少引用
  两个已核验、直接相关且 origin_id 不同的来源；independent_review 必须
  带非空 reviewer_ref（其余模式可省略，但若提供必须非空）；所有非 none
  模式的 evidence_refs 必须非空，其余非 none 模式的 basis 必须明确标注
  synthetic；效果类高置信主
  张必须 status=verified 且 mode!=none；not_covered 主张必须
  not_verified＋none＋空引用；
- 相反观点检索的 found 发现必须是带 evidence_refs 的对象，每项至少一个
  可解析来源且至少一个已核验＋直接相关；not_found 时 findings 必须为空；
- 研究路线对每个 V1 主张必须恰有一个 V2 逐项复核项；unchanged 时
  revised_text 必须与 V1 text 完全相同，revised 时必须与 V1 text 不同，
  并保留偏离与理由；
- 同一 `origin_id` 的多个转载不得冒充独立来源；
- 每个视角必须显式含 association、conflicts 列表、value 与 uncertainty；
  用户画像推断必须带依据与不确定性；已确认用户事实必须显式
  inferred=false 且带非空合成 confirmation_ref；
- 研究路线综合判断必须含 credibility（high／medium／low／insufficient）、
  credibility_basis 与四种判定的 claim_counts，计数与 V1 实际完全一致；
- Obsidian 记录只允许安全的合成相对逻辑 record_ref：拒绝绝对路径、~、
  URI、反斜杠与空／.／.. 路径分量；
- `uncertainties` 允许空列表，不得强造不确定项。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

ROUTES = ("research", "direct")
AUTHENTICITIES = ("verified", "unverified")
RELEVANCES = ("direct", "indirect")
INDEPENDENCES = ("independent", "non_independent")
SOURCE_TYPES = (
    "paper",
    "official_document",
    "industry_practice",
    "media_report",
    "dataset",
)
SOURCE_TIERS = ("primary", "secondary")
CLAIM_KINDS = ("official_statement", "real_world_effect", "general_fact")
VERDICTS = ("supported", "partially_supported", "contradicted", "not_covered")
EVIDENCE_RELATIONS = ("supports", "partially_supports", "contradicts")
CONFIDENCES = ("high", "medium", "low")
CREDIBILITIES = ("high", "medium", "low", "insufficient")
COUNTER_STATUSES = ("found", "not_found")
REVISION_STATUSES = ("unchanged", "revised")
ACQUISITION_METHODS = ("synthetic_fixture", "public_search_adapter")
VERIFICATION_STATUSES = ("verified", "partial", "not_verified")
VERIFICATION_MODES = (
    "independent_source",
    "recalculation",
    "evidence_chain_reconstruction",
    "independent_review",
    "none",
)
PERSPECTIVES = ("memory", "knowledge_base", "frontier")
RESEARCH_REQUIRED_PERSPECTIVES = ("memory", "knowledge_base", "frontier")
MATERIAL_TYPES = ("confirmed_user_fact", "obsidian_record", "profile_inference")

ROUTE_LABELS = {"research": "研究路线", "direct": "直接路线"}
PERSPECTIVE_LABELS = {
    "memory": "现有记忆视角",
    "knowledge_base": "Obsidian 知识库与项目进展视角",
    "frontier": "前沿行业／技术／科学视角",
}
MATERIAL_LABELS = {
    "confirmed_user_fact": "已确认用户事实或偏好",
    "obsidian_record": "可追溯知识库／项目记录",
    "profile_inference": "用户画像推断",
}
VERDICT_LABELS = {
    "supported": "支持",
    "partially_supported": "部分支持",
    "contradicted": "反对",
    "not_covered": "未覆盖",
}
REVISION_LABELS = {"unchanged": "未修改", "revised": "已修改"}
CREDIBILITY_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "insufficient": "不足",
}
VERIFICATION_STATUS_LABELS = {
    "verified": "已验证",
    "partial": "部分验证",
    "not_verified": "未验证",
}
VERIFICATION_MODE_LABELS = {
    "independent_source": "独立来源交叉支持",
    "recalculation": "原始数据重算",
    "evidence_chain_reconstruction": "不同证据链重构",
    "independent_review": "独立审查者复核",
    "none": "无",
}


class CognitiveContractError(ValueError):
    """认知结果违反离线内容契约。"""


def validate_cognitive_result(
    result: Mapping[str, object],
    fragment_text: str,
) -> tuple[str, ...]:
    """校验认知结果是否满足离线内容契约；合法时返回空 tuple。"""
    if not isinstance(fragment_text, str) or not fragment_text.strip():
        return ("fragment_text 必须是非空字符串",)
    route = result.get("route")
    if not isinstance(route, str) or route not in ROUTES:
        return (f"route 必须是以下之一: {', '.join(ROUTES)}",)
    errors: list[str] = []
    errors.extend(_text_error("route_reason", result.get("route_reason")))
    if result.get("route_change_allowed") is not True:
        errors.append("route_change_allowed 必须为 true（必须允许用户更改路线）")
    errors.extend(_summary_errors(result.get("summary"), fragment_text))
    errors.extend(_expansion_errors(result.get("semantic_expansion"), fragment_text))
    claim_counts: dict[str, int] = {}
    if route == "research":
        research_errors, claim_counts = _research_errors(
            result.get("research"), fragment_text
        )
        errors.extend(research_errors)
    elif "research" in result:
        errors.append("直接路线不得夹带 research 研究字段")
    errors.extend(_perspective_errors(result.get("perspectives"), route))
    errors.extend(_synthesis_errors(result.get("synthesis"), route, claim_counts))
    return tuple(errors)


def validate_semantic_expansion(
    expansion: object, fragment_text: str
) -> tuple[str, ...]:
    """Validate step-2 material before a research/direct route is bound."""
    if not isinstance(fragment_text, str) or not fragment_text.strip():
        return ("fragment_text 必须是非空字符串",)
    return tuple(_expansion_errors(expansion, fragment_text))


def render_cognitive_result(
    result: Mapping[str, object],
    fragment_text: str,
) -> str:
    """先校验同一契约，非法时抛 CognitiveContractError；合法时返回 Markdown。"""
    errors = validate_cognitive_result(result, fragment_text)
    if errors:
        raise CognitiveContractError("认知结果违反离线内容契约：" + "；".join(errors))
    route = str(result.get("route"))
    summary = _as_mapping(result.get("summary")) or {}
    expansion = _as_mapping(result.get("semantic_expansion")) or {}
    synthesis = _as_mapping(result.get("synthesis")) or {}
    lines: list[str] = [
        "# 碎片认知结果（离线合成契约候选 R1-A）",
        "",
        "## 原始碎片",
        "",
        f"> {fragment_text}",
        "",
        "## 路线判断",
        "",
        f"- 路线：{ROUTE_LABELS.get(route, route)}（{route}）",
        f"- 路线理由：{result.get('route_reason')}",
        "- 用户可以更改路线：是",
        "",
        "## 原文摘要",
        "",
        str(summary.get("text", "")),
        "",
        f"> 逐字引用：{summary.get('source_quote', '')}",
        f"> 转换依据：{summary.get('transformation_basis', '')}",
        "",
        "## 语义展开",
        "",
        "### 字面信息",
        "",
    ]
    for fact in _as_mapping_list(expansion.get("literal_facts")):
        lines.append(f"- {fact.get('text')}")
    lines += ["", "### 高可信结构推断", ""]
    inferences = _as_mapping_list(expansion.get("inferences"))
    if inferences:
        for inference in inferences:
            lines.append(
                f"- {inference.get('text')}"
                f"（原文：{inference.get('source_quote')}；"
                f"变化依据：{inference.get('transformation_basis')}）"
            )
    else:
        lines.append("- （无）")
    lines += ["", "### 真正未知项", ""]
    uncertainties = _as_str_list(expansion.get("uncertainties")) or ()
    if uncertainties:
        lines.extend(f"- {entry}" for entry in uncertainties)
    else:
        lines.append("- 未发现实质不确定项")
    if route == "research":
        lines.extend(_render_research(_as_mapping(result.get("research")) or {}))
    lines.extend(_render_perspectives(result.get("perspectives")))
    lines.extend(_render_synthesis(synthesis, route))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ 类型收窄工具


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return value
    return None


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in (_as_mapping(entry) for entry in value) if item is not None]


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_str_list(value: object) -> tuple[str, ...] | None:
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def _text_error(label: str, value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{label} 必须是非空字符串"]
    return []


def _enum_error(label: str, value: object, allowed: tuple[str, ...]) -> list[str]:
    if not isinstance(value, str) or value not in allowed:
        return [f"{label} 必须是以下之一: {', '.join(allowed)}"]
    return []


def _quote_errors(label: str, value: object, fragment_text: str) -> list[str]:
    text = _as_str(value)
    if text is None or not text:
        return [f"{label} 必须提供逐字 source_quote"]
    if text not in fragment_text:
        return [f"{label} 的 source_quote 必须是原文逐字子串"]
    return []


def _str_list_field_errors(label: str, value: object) -> list[str]:
    entries = _as_str_list(value)
    if entries is None:
        return [f"{label} 必须是字符串列表（允许空列表）"]
    if any(not entry.strip() for entry in entries):
        return [f"{label} 不得包含空白条目"]
    return []


# ------------------------------------------------------------ 各段校验


def _summary_errors(summary_raw: object, fragment_text: str) -> list[str]:
    summary = _as_mapping(summary_raw)
    if summary is None:
        return ["summary 必须是对象"]
    errors = _text_error("summary.text", summary.get("text"))
    errors.extend(_quote_errors("summary", summary.get("source_quote"), fragment_text))
    errors.extend(
        _text_error("summary.transformation_basis", summary.get("transformation_basis"))
    )
    return errors


def _expansion_errors(expansion_raw: object, fragment_text: str) -> list[str]:
    expansion = _as_mapping(expansion_raw)
    if expansion is None:
        return ["semantic_expansion 必须是对象"]
    errors: list[str] = []
    literal_facts_raw = expansion.get("literal_facts")
    if not isinstance(literal_facts_raw, (list, tuple)) or not literal_facts_raw:
        errors.append("semantic_expansion.literal_facts 必须是非空列表")
    else:
        for index, fact_raw in enumerate(literal_facts_raw):
            label = f"semantic_expansion.literal_facts[{index}]"
            fact = _as_mapping(fact_raw)
            if fact is None:
                errors.append(f"{label} 必须是包含 text 与逐字 source_quote 的对象")
                continue
            errors.extend(_text_error(f"{label}.text", fact.get("text")))
            errors.extend(_quote_errors(label, fact.get("source_quote"), fragment_text))
            text = _as_str(fact.get("text"))
            quote = _as_str(fact.get("source_quote"))
            if text is not None and quote is not None and text != quote:
                errors.append(f"{label} 字面信息 text 必须与逐字 source_quote 完全一致")
    inferences_raw = expansion.get("inferences")
    if not isinstance(inferences_raw, (list, tuple)):
        errors.append("semantic_expansion.inferences 必须是列表")
    else:
        for index, item_raw in enumerate(inferences_raw):
            label = f"semantic_expansion.inferences[{index}]"
            item = _as_mapping(item_raw)
            if item is None:
                errors.append(f"{label} 必须是对象")
                continue
            errors.extend(_text_error(f"{label}.text", item.get("text")))
            errors.extend(_quote_errors(label, item.get("source_quote"), fragment_text))
            errors.extend(
                _text_error(f"{label}.transformation_basis", item.get("transformation_basis"))
            )
    uncertainties = _as_str_list(expansion.get("uncertainties"))
    if uncertainties is None:
        errors.append("semantic_expansion.uncertainties 必须是字符串列表（允许空列表）")
    elif any(not entry.strip() for entry in uncertainties):
        errors.append("semantic_expansion.uncertainties 不得包含空白条目")
    return errors


def _research_errors(
    research_raw: object, fragment_text: str
) -> tuple[list[str], dict[str, int]]:
    counts = {verdict: 0 for verdict in VERDICTS}
    research = _as_mapping(research_raw)
    if research is None:
        return ["研究路线缺少 research 研究字段"], counts
    errors: list[str] = []
    questions = _as_str_list(research.get("questions"))
    if questions is None or not questions or any(not item.strip() for item in questions):
        errors.append("research.questions 必须是非空字符串列表")
    dimensions = _as_str_list(research.get("search_dimensions"))
    if dimensions is None or not dimensions or any(not item.strip() for item in dimensions):
        errors.append("research.search_dimensions 必须是非空字符串列表")
    source_cards, source_errors = _source_card_errors(research.get("sources"))
    errors.extend(source_errors)
    errors.extend(
        _counter_search_errors(research.get("counter_evidence_search"), source_cards)
    )
    claims_raw = research.get("v1_claims")
    if isinstance(claims_raw, (list, tuple)):
        for claim_raw in claims_raw:
            claim = _as_mapping(claim_raw)
            if claim is not None and claim.get("verdict") in VERDICTS:
                counts[str(claim["verdict"])] += 1
    claim_cards, claim_errors = _v1_claim_errors(claims_raw, source_cards, fragment_text)
    errors.extend(claim_errors)
    errors.extend(_v2_revision_errors(research.get("v2_revisions"), claim_cards))
    return errors, counts


def _source_card_errors(
    sources_raw: object,
) -> tuple[dict[str, Mapping[str, object]], list[str]]:
    errors: list[str] = []
    source_cards: dict[str, Mapping[str, object]] = {}
    if not isinstance(sources_raw, (list, tuple)):
        return source_cards, ["research.sources 必须是列表"]
    independent_origins: dict[str, list[str]] = {}
    for index, source_raw in enumerate(sources_raw):
        label = f"research.sources[{index}]"
        source = _as_mapping(source_raw)
        if source is None:
            errors.append(f"{label} 必须是对象")
            continue
        source_id = _as_str(source.get("source_id"))
        if source_id is None or not source_id.strip():
            errors.append(f"{label}.source_id 必须是非空字符串")
        elif source_id in source_cards:
            errors.append(f"{label}.source_id 重复: {source_id}")
        else:
            source_cards[source_id] = source
        errors.extend(_text_error(f"{label}.name", source.get("name")))
        errors.extend(_text_error(f"{label}.origin_id", source.get("origin_id")))
        errors.extend(
            _enum_error(f"{label}.source_tier", source.get("source_tier"), SOURCE_TIERS)
        )
        errors.extend(_text_error(f"{label}.version", source.get("version")))
        errors.extend(
            _enum_error(f"{label}.authenticity", source.get("authenticity"), AUTHENTICITIES)
        )
        errors.extend(_enum_error(f"{label}.relevance", source.get("relevance"), RELEVANCES))
        errors.extend(
            _enum_error(f"{label}.independence", source.get("independence"), INDEPENDENCES)
        )
        errors.extend(_enum_error(f"{label}.source_type", source.get("source_type"), SOURCE_TYPES))
        errors.extend(_text_error(f"{label}.published_at", source.get("published_at")))
        errors.extend(_text_error(f"{label}.locator", source.get("locator")))
        errors.extend(
            _enum_error(
                f"{label}.acquisition_method",
                source.get("acquisition_method"),
                ACQUISITION_METHODS,
            )
        )
        errors.extend(
            _text_error(f"{label}.verification_basis", source.get("verification_basis"))
        )
        errors.extend(_text_error(f"{label}.evidence_excerpt", source.get("evidence_excerpt")))
        excerpt = _as_str(source.get("evidence_excerpt"))
        excerpt_sha256 = _as_str(source.get("excerpt_sha256"))
        if excerpt_sha256 is None or excerpt is None or excerpt_sha256 != hashlib.sha256(
            excerpt.encode("utf-8")
        ).hexdigest():
            errors.append(f"{label}.excerpt_sha256 必须精确匹配 evidence_excerpt 的 SHA-256")
        errors.extend(_text_error(f"{label}.scope", source.get("scope")))
        if source_id is not None and source.get("independence") == "independent":
            origin_id = _as_str(source.get("origin_id"))
            if origin_id:
                independent_origins.setdefault(origin_id, []).append(source_id)
    for origin_id, grouped in independent_origins.items():
        if len(grouped) > 1:
            errors.append(
                f"同一 origin_id={origin_id} 的多个转载不能都标记为独立来源: "
                f"{', '.join(grouped)}"
            )
    return source_cards, errors


def _counter_search_errors(
    counter_raw: object, source_cards: Mapping[str, Mapping[str, object]]
) -> list[str]:
    counter = _as_mapping(counter_raw)
    if counter is None:
        return ["research.counter_evidence_search 必须是对象"]
    errors = _enum_error(
        "research.counter_evidence_search.status", counter.get("status"), COUNTER_STATUSES
    )
    errors.extend(_text_error("research.counter_evidence_search.scope", counter.get("scope")))
    status = _as_str(counter.get("status"))
    findings_raw = counter.get("findings")
    if not isinstance(findings_raw, (list, tuple)):
        errors.append("research.counter_evidence_search.findings 必须是列表")
        return errors
    if status == "not_found" and findings_raw:
        errors.append("research.counter_evidence_search 为 not_found 时 findings 必须为空")
    if status == "found" and not findings_raw:
        errors.append("research.counter_evidence_search 标记 found 时 findings 必须非空")
    for index, finding_raw in enumerate(findings_raw):
        label = f"research.counter_evidence_search.findings[{index}]"
        finding = _as_mapping(finding_raw)
        if finding is None:
            errors.append(f"{label} 必须是包含 text 与 evidence_refs 的对象")
            continue
        errors.extend(_text_error(f"{label}.text", finding.get("text")))
        refs = _as_str_list(finding.get("evidence_refs"))
        if refs is None or not refs:
            errors.append(f"{label}.evidence_refs 必须至少引用一个来源")
            continue
        cited: list[Mapping[str, object]] = []
        for ref in refs:
            if ref not in source_cards:
                errors.append(f"{label}.evidence_refs 引用了不存在的来源: {ref}")
            else:
                cited.append(source_cards[ref])
        if not any(
            card.get("authenticity") == "verified" and card.get("relevance") == "direct"
            for card in cited
        ):
            errors.append(f"{label} 至少引用一个已核验且直接相关的来源")
        errors.extend(
            _source_material_support_errors(
                label,
                finding.get("evidence_support"),
                refs,
                source_cards,
            )
        )
    return errors


def _v1_claim_errors(
    claims_raw: object,
    source_cards: Mapping[str, Mapping[str, object]],
    fragment_text: str,
) -> tuple[dict[str, Mapping[str, object]], list[str]]:
    errors: list[str] = []
    claim_cards: dict[str, Mapping[str, object]] = {}
    if not isinstance(claims_raw, (list, tuple)) or not claims_raw:
        return claim_cards, ["research.v1_claims 必须是非空列表"]
    for index, claim_raw in enumerate(claims_raw):
        label = f"research.v1_claims[{index}]"
        claim = _as_mapping(claim_raw)
        if claim is None:
            errors.append(f"{label} 必须是对象")
            continue
        claim_id = _as_str(claim.get("claim_id"))
        if claim_id is None or not claim_id.strip():
            errors.append(f"{label}.claim_id 必须是非空字符串")
        elif claim_id in claim_cards:
            errors.append(f"{label}.claim_id 重复: {claim_id}")
        else:
            claim_cards[claim_id] = claim
        errors.extend(_text_error(f"{label}.text", claim.get("text")))
        errors.extend(_quote_errors(label, claim.get("source_quote"), fragment_text))
        errors.extend(_enum_error(f"{label}.claim_kind", claim.get("claim_kind"), CLAIM_KINDS))
        verdict = _as_str(claim.get("verdict"))
        errors.extend(_enum_error(f"{label}.verdict", claim.get("verdict"), VERDICTS))
        confidence = _as_str(claim.get("confidence"))
        errors.extend(_enum_error(f"{label}.confidence", claim.get("confidence"), CONFIDENCES))
        evidence_refs = _as_str_list(claim.get("evidence_refs"))
        if evidence_refs is None:
            errors.append(f"{label}.evidence_refs 必须是字符串列表")
            evidence_refs = ()
        else:
            for ref in evidence_refs:
                if ref not in source_cards:
                    errors.append(f"{label}.evidence_refs 引用了不存在的来源: {ref}")
        cited = [source_cards[ref] for ref in evidence_refs if ref in source_cards]
        if verdict is not None and verdict != "not_covered":
            if not evidence_refs:
                errors.append(f"{label} 判定为 {verdict} 时必须提供证据引用")
            if not any(
                card.get("authenticity") == "verified" and card.get("relevance") == "direct"
                for card in cited
            ):
                errors.append(
                    f"{label} 判定为 {verdict} 时至少引用一个已核验且直接相关的来源"
                )
            if (
                _as_str(claim.get("claim_kind")) == "real_world_effect"
                and confidence == "high"
                and not any(
                    card.get("authenticity") == "verified"
                    and card.get("relevance") == "direct"
                    and card.get("independence") == "independent"
                    for card in cited
                )
            ):
                errors.append(
                    f"{label} 效果类高置信主张至少引用一个已核验、直接相关且独立的来源；"
                    "仅有企业自身宣传或未核验材料不足以支撑"
                )
        if verdict == "not_covered":
            if evidence_refs:
                errors.append(f"{label} 判定为 not_covered 时 evidence_refs 必须为空列表")
            if confidence != "low":
                errors.append(f"{label} 判定为 not_covered 时 confidence 必须为 low")
        errors.extend(
            _evidence_support_errors(label, claim, verdict, evidence_refs, source_cards)
        )
        errors.extend(
            _text_error(f"{label}.claim_derivation", claim.get("claim_derivation"))
        )
        errors.extend(_text_error(f"{label}.verdict_basis", claim.get("verdict_basis")))
        errors.extend(
            _independent_verification_errors(
                label, claim, verdict, confidence, source_cards
            )
        )
    return claim_cards, errors


def _evidence_support_errors(
    label: str,
    claim: Mapping[str, object],
    verdict: str | None,
    evidence_refs: tuple[str, ...],
    source_cards: Mapping[str, Mapping[str, object]],
) -> list[str]:
    support_raw = claim.get("evidence_support")
    if not isinstance(support_raw, (list, tuple)):
        return [f"{label}.evidence_support 必须是列表"]
    if verdict == "not_covered" and support_raw:
        return [f"{label} 判定为 not_covered 时 evidence_support 必须为空列表"]
    if verdict != "not_covered" and not support_raw:
        return [f"{label}.evidence_support 必须逐项绑定 evidence_refs 的来源证据片段"]
    errors: list[str] = []
    mapped_refs: list[str] = []
    relations: list[str] = []
    for index, support_entry_raw in enumerate(support_raw):
        entry_label = f"{label}.evidence_support[{index}]"
        support_entry = _as_mapping(support_entry_raw)
        if support_entry is None:
            errors.append(f"{entry_label} 必须是对象")
            continue
        source_id = _as_str(support_entry.get("source_id"))
        if source_id is None or not source_id.strip():
            errors.append(f"{entry_label}.source_id 必须是非空字符串")
        else:
            mapped_refs.append(source_id)
        source_quote = _as_str(support_entry.get("source_quote"))
        errors.extend(_text_error(f"{entry_label}.source_quote", source_quote))
        relation = _as_str(support_entry.get("relation"))
        errors.extend(
            _enum_error(
                f"{entry_label}.relation",
                support_entry.get("relation"),
                EVIDENCE_RELATIONS,
            )
        )
        if relation is not None:
            relations.append(relation)
        errors.extend(_text_error(f"{entry_label}.basis", support_entry.get("basis")))
        source = source_cards.get(source_id or "")
        if source is None:
            if source_id:
                errors.append(f"{entry_label}.source_id 引用了不存在的来源: {source_id}")
            continue
        excerpt = _as_str(source.get("evidence_excerpt"))
        if source_quote is not None and (excerpt is None or source_quote not in excerpt):
            errors.append(
                f"{entry_label}.source_quote 必须是来源 evidence_excerpt 的逐字子串"
            )
    if mapped_refs != list(evidence_refs):
        errors.append(f"{label}.evidence_support 的 source_id 顺序必须与 evidence_refs 精确一致")
    if len(set(mapped_refs)) != len(mapped_refs):
        errors.append(f"{label}.evidence_support 不得重复绑定同一 source_id")
    if verdict == "supported" and relations and any(
        relation != "supports" for relation in relations
    ):
        errors.append(f"{label} supported 判定的 evidence_support.relation 必须均为 supports")
    if verdict == "partially_supported" and "partially_supports" not in relations:
        errors.append(
            f"{label} partially_supported 判定至少需要一个 partially_supports 关系"
        )
    if verdict == "contradicted" and "contradicts" not in relations:
        errors.append(f"{label} contradicted 判定至少需要一个 contradicts 关系")
    return errors


def _source_material_support_errors(
    label: str,
    support_raw: object,
    expected_refs: tuple[str, ...],
    source_cards: Mapping[str, Mapping[str, object]],
) -> list[str]:
    if not isinstance(support_raw, (list, tuple)):
        return [f"{label}.evidence_support 必须是列表"]
    if not expected_refs and support_raw:
        return [f"{label}.evidence_support 没有对应引用时必须为空"]
    if expected_refs and not support_raw:
        return [f"{label}.evidence_support 必须绑定来源中的逐字证据片段"]
    errors: list[str] = []
    mapped_refs: list[str] = []
    for index, entry_raw in enumerate(support_raw):
        entry_label = f"{label}.evidence_support[{index}]"
        entry = _as_mapping(entry_raw)
        if entry is None:
            errors.append(f"{entry_label} 必须是对象")
            continue
        source_id = _as_str(entry.get("source_id"))
        if source_id is None or not source_id.strip():
            errors.append(f"{entry_label}.source_id 必须是非空字符串")
        else:
            mapped_refs.append(source_id)
        source_quote = _as_str(entry.get("source_quote"))
        errors.extend(_text_error(f"{entry_label}.source_quote", source_quote))
        errors.extend(_text_error(f"{entry_label}.basis", entry.get("basis")))
        source = source_cards.get(source_id or "")
        if source is None:
            if source_id:
                errors.append(f"{entry_label}.source_id 引用了不存在的来源: {source_id}")
            continue
        excerpt = _as_str(source.get("evidence_excerpt"))
        if source_quote is not None and (excerpt is None or source_quote not in excerpt):
            errors.append(
                f"{entry_label}.source_quote 必须是来源 evidence_excerpt 的逐字子串"
            )
    if mapped_refs != list(expected_refs):
        errors.append(f"{label}.evidence_support 的 source_id 顺序必须与待绑定引用精确一致")
    if len(set(mapped_refs)) != len(mapped_refs):
        errors.append(f"{label}.evidence_support 不得重复绑定同一 source_id")
    return errors


def _independent_verification_errors(
    label: str,
    claim: Mapping[str, object],
    verdict: str | None,
    confidence: str | None,
    source_cards: Mapping[str, Mapping[str, object]],
) -> list[str]:
    iv_label = f"{label}.independent_verification"
    verification = _as_mapping(claim.get("independent_verification"))
    if verification is None:
        return [f"{iv_label} 必须是对象"]
    errors: list[str] = []
    status = _as_str(verification.get("status"))
    mode = _as_str(verification.get("mode"))
    errors.extend(
        _enum_error(f"{iv_label}.status", verification.get("status"), VERIFICATION_STATUSES)
    )
    errors.extend(
        _enum_error(f"{iv_label}.mode", verification.get("mode"), VERIFICATION_MODES)
    )
    errors.extend(_text_error(f"{iv_label}.basis", verification.get("basis")))
    refs = _as_str_list(verification.get("evidence_refs"))
    if refs is None:
        errors.append(f"{iv_label}.evidence_refs 必须是字符串列表")
        refs = ()
    else:
        for ref in refs:
            if ref not in source_cards:
                errors.append(f"{iv_label}.evidence_refs 引用了不存在的来源: {ref}")
    cited = [source_cards[ref] for ref in refs if ref in source_cards]
    main_support_ids = {
        str(entry.get("source_id"))
        for entry in _as_mapping_list(claim.get("evidence_support"))
        if isinstance(entry.get("source_id"), str)
    }
    unbound_refs = tuple(ref for ref in refs if ref not in main_support_ids)
    errors.extend(
        _source_material_support_errors(
            iv_label,
            verification.get("evidence_support", []),
            unbound_refs,
            source_cards,
        )
    )
    if mode == "none":
        if status != "not_verified" or refs:
            errors.append(
                f"{iv_label} mode=none 时 status 必须为 not_verified 且 evidence_refs 为空"
            )
    elif mode in VERIFICATION_MODES:
        if status not in ("verified", "partial"):
            errors.append(f"{iv_label} 非 none 模式时 status 必须为 verified 或 partial")
        if not refs:
            errors.append(f"{iv_label} 非 none 模式时 evidence_refs 必须非空")
        if mode == "independent_source":
            if not any(
                card.get("authenticity") == "verified"
                and card.get("relevance") == "direct"
                and card.get("independence") == "independent"
                for card in cited
            ):
                errors.append(
                    f"{iv_label} independent_source 模式至少引用一个"
                    "已核验、直接相关且独立的来源"
                )
        if mode == "recalculation":
            if not any(
                card.get("authenticity") == "verified"
                and card.get("relevance") == "direct"
                and card.get("source_type") == "dataset"
                for card in cited
            ):
                errors.append(
                    f"{iv_label} recalculation 模式至少引用一个"
                    "已核验、直接相关且 source_type=dataset 的来源"
                )
        if mode == "evidence_chain_reconstruction":
            qualified_origins = {
                card.get("origin_id")
                for card in cited
                if card.get("authenticity") == "verified"
                and card.get("relevance") == "direct"
            }
            if len(qualified_origins) < 2:
                errors.append(
                    f"{iv_label} evidence_chain_reconstruction 模式至少引用两个"
                    "已核验、直接相关且 origin_id 不同的来源"
                )
        if mode != "independent_source":
            basis = _as_str(verification.get("basis")) or ""
            if "synthetic" not in basis and "合成" not in basis:
                errors.append(
                    f"{iv_label}.basis 必须明确标注 synthetic（离线合成，不伪造外部执行）"
                )
        reviewer_ref = _as_str(verification.get("reviewer_ref"))
        if mode == "independent_review":
            if reviewer_ref is None or not reviewer_ref.strip():
                errors.append(f"{iv_label}.reviewer_ref 必须是非空字符串（独立审查者引用）")
        elif verification.get("reviewer_ref") is not None and (
            reviewer_ref is None or not reviewer_ref.strip()
        ):
            errors.append(f"{iv_label}.reviewer_ref 若提供必须非空")
    if (
        _as_str(claim.get("claim_kind")) == "real_world_effect"
        and confidence == "high"
        and (status != "verified" or mode == "none")
    ):
        errors.append(f"{label} 效果类高置信主张必须 status=verified 且 mode!=none 的独立验证")
    if verdict == "not_covered" and (status != "not_verified" or mode != "none" or refs):
        errors.append(
            f"{label} not_covered 主张的独立验证必须为 not_verified + none + 空 evidence_refs"
        )
    return errors


def _v2_revision_errors(
    revisions_raw: object, claim_cards: Mapping[str, Mapping[str, object]]
) -> list[str]:
    errors: list[str] = []
    if not isinstance(revisions_raw, (list, tuple)):
        return ["research.v2_revisions 必须是列表"]
    seen: set[str] = set()
    for index, revision_raw in enumerate(revisions_raw):
        label = f"research.v2_revisions[{index}]"
        revision = _as_mapping(revision_raw)
        if revision is None:
            errors.append(f"{label} 必须是对象")
            continue
        claim_id = _as_str(revision.get("claim_id"))
        if claim_id is None or claim_id not in claim_cards:
            errors.append(f"{label}.claim_id 必须引用 V1 中存在的 claim_id")
        elif claim_id in seen:
            errors.append(f"{label}.claim_id 重复: {claim_id}（每个 V1 主张恰有一个 V2 复核项）")
        else:
            seen.add(claim_id)
        revision_status = _as_str(revision.get("revision_status"))
        errors.extend(
            _enum_error(
                f"{label}.revision_status",
                revision.get("revision_status"),
                REVISION_STATUSES,
            )
        )
        errors.extend(_text_error(f"{label}.deviation", revision.get("deviation")))
        errors.extend(_text_error(f"{label}.revision_reason", revision.get("revision_reason")))
        errors.extend(_text_error(f"{label}.revised_text", revision.get("revised_text")))
        if (
            revision_status == "unchanged"
            and claim_id is not None
            and claim_id in claim_cards
            and revision.get("revised_text") != claim_cards[claim_id].get("text")
        ):
            errors.append(
                f"{label} revision_status=unchanged 时 revised_text "
                "必须与 V1 主张 text 完全相同"
            )
        if (
            revision_status == "revised"
            and claim_id is not None
            and claim_id in claim_cards
            and revision.get("revised_text") == claim_cards[claim_id].get("text")
        ):
            errors.append(
                f"{label} revision_status=revised 时 revised_text "
                "必须与 V1 主张 text 不同"
            )
    for missing in sorted(set(claim_cards) - seen):
        errors.append(f"research.v2_revisions 缺少对 V1 主张 {missing} 的逐项复核")
    return errors


def _perspective_errors(perspectives_raw: object, route: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(perspectives_raw, (list, tuple)) or not perspectives_raw:
        return ["perspectives 必须是非空列表"]
    seen: list[str] = []
    for index, perspective_raw in enumerate(perspectives_raw):
        label = f"perspectives[{index}]"
        perspective = _as_mapping(perspective_raw)
        if perspective is None:
            errors.append(f"{label} 必须是对象")
            continue
        name = _as_str(perspective.get("perspective"))
        errors.extend(
            _enum_error(f"{label}.perspective", perspective.get("perspective"), PERSPECTIVES)
        )
        if name is not None:
            if name in seen:
                errors.append(f"{label}.perspective 重复: {name}")
            else:
                seen.append(name)
        errors.extend(_text_error(f"{label}.summary", perspective.get("summary")))
        errors.extend(_text_error(f"{label}.association", perspective.get("association")))
        errors.extend(_str_list_field_errors(f"{label}.conflicts", perspective.get("conflicts")))
        errors.extend(_text_error(f"{label}.value", perspective.get("value")))
        errors.extend(_text_error(f"{label}.uncertainty", perspective.get("uncertainty")))
        materials_raw = perspective.get("materials")
        if not isinstance(materials_raw, (list, tuple)):
            errors.append(f"{label}.materials 必须是列表")
            continue
        for material_index, material_raw in enumerate(materials_raw):
            errors.extend(_material_errors(f"{label}.materials[{material_index}]", material_raw))
    if route == "research":
        for required in RESEARCH_REQUIRED_PERSPECTIVES:
            if required not in seen:
                errors.append(f"研究路线缺少视角: {required}")
    return errors


def _material_errors(label: str, material_raw: object) -> list[str]:
    material = _as_mapping(material_raw)
    if material is None:
        return [f"{label} 必须是对象"]
    errors = _enum_error(f"{label}.material_type", material.get("material_type"), MATERIAL_TYPES)
    errors.extend(_text_error(f"{label}.text", material.get("text")))
    material_type = _as_str(material.get("material_type"))
    if material_type == "profile_inference":
        errors.extend(_text_error(f"{label}.basis", material.get("basis")))
        errors.extend(_text_error(f"{label}.uncertainty", material.get("uncertainty")))
    if material_type == "confirmed_user_fact":
        if material.get("inferred") is not False:
            errors.append(f"{label} 已确认用户事实必须显式标记 inferred=false")
        errors.extend(
            _text_error(f"{label}.confirmation_ref", material.get("confirmation_ref"))
        )
    if material_type == "obsidian_record":
        record_ref = _as_str(material.get("record_ref"))
        if record_ref is None or not record_ref.strip():
            errors.append(f"{label} obsidian_record 必须提供合成的相对 record_ref")
        else:
            errors.extend(_record_ref_errors(label, record_ref))
    return errors


def _record_ref_errors(label: str, record_ref: str) -> list[str]:
    if (
        record_ref.startswith(("/", "~"))
        or "://" in record_ref
        or "\\" in record_ref
    ):
        return [f"{label}.record_ref 必须是合成相对逻辑路径，禁止绝对路径、~、URI 或反斜杠"]
    components = record_ref.split("/")
    if any(component in ("", ".", "..") for component in components):
        return [f"{label}.record_ref 含有空、. 或 .. 路径分量，不是安全的合成相对路径"]
    return []


def _synthesis_errors(
    synthesis_raw: object, route: str, claim_counts: Mapping[str, int]
) -> list[str]:
    synthesis = _as_mapping(synthesis_raw)
    if synthesis is None:
        return ["synthesis 必须是对象"]
    errors = _text_error("synthesis.value", synthesis.get("value"))
    errors.extend(_text_error("synthesis.weakest_link", synthesis.get("weakest_link")))
    errors.extend(_text_error("synthesis.next_step", synthesis.get("next_step")))
    for field_name in ("conflicts", "open_questions"):
        errors.extend(
            _str_list_field_errors(f"synthesis.{field_name}", synthesis.get(field_name))
        )
    if route == "research":
        errors.extend(
            _enum_error("synthesis.credibility", synthesis.get("credibility"), CREDIBILITIES)
        )
        errors.extend(
            _text_error("synthesis.credibility_basis", synthesis.get("credibility_basis"))
        )
        errors.extend(_claim_count_errors(synthesis.get("claim_counts"), claim_counts))
    return errors


def _claim_count_errors(counts_raw: object, expected: Mapping[str, int]) -> list[str]:
    counts = _as_mapping(counts_raw)
    if counts is None:
        return ["synthesis.claim_counts 必须是四种判定的整数计数对象"]
    if set(counts) != set(VERDICTS):
        return [
            "synthesis.claim_counts 必须恰好包含 "
            + "／".join(VERDICTS)
            + " 四个键"
        ]
    for verdict in VERDICTS:
        value = counts.get(verdict)
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"synthesis.claim_counts.{verdict} 必须是整数"]
    if any(counts[verdict] != expected.get(verdict, 0) for verdict in VERDICTS):
        return ["synthesis.claim_counts 与 V1 实际计数不一致"]
    return []


# ------------------------------------------------------------ 各段呈现


def _render_research(research: Mapping[str, object]) -> list[str]:
    lines = ["", "## 研究问题与检索维度", "", "### 研究问题", ""]
    for question in _as_str_list(research.get("questions")) or ():
        lines.append(f"- {question}")
    lines += ["", "### 检索维度", ""]
    for dimension in _as_str_list(research.get("search_dimensions")) or ():
        lines.append(f"- {dimension}")
    lines += ["", "## 来源核验卡", ""]
    for source in _as_mapping_list(research.get("sources")):
        lines += [
            f"### {source.get('source_id')}：{source.get('name')}",
            "",
            f"- 真实性：{source.get('authenticity')}",
            f"- 相关性：{source.get('relevance')}",
            f"- 独立性：{source.get('independence')}",
            f"- 一手／二手：{source.get('source_tier')}",
            f"- 来源类型：{source.get('source_type')}",
            f"- 版本：{source.get('version')}",
            f"- 时间：{source.get('published_at')}",
            f"- 定位：{source.get('locator')}",
            f"- 获取方式：{source.get('acquisition_method')}",
            f"- 核验依据：{source.get('verification_basis')}",
            f"- 证据片段：{source.get('evidence_excerpt')}",
            f"- 证据片段 SHA-256：{source.get('excerpt_sha256')}",
            f"- 适用范围：{source.get('scope')}",
            f"- 来源原点 origin_id：{source.get('origin_id')}",
            "",
        ]
    counter = _as_mapping(research.get("counter_evidence_search")) or {}
    status = "未找到" if counter.get("status") == "not_found" else "已找到"
    lines += [
        "## 相反观点与失败案例检索",
        "",
        f"- 检索状态：{status}",
        f"- 检索范围：{counter.get('scope')}",
    ]
    for finding in _as_mapping_list(counter.get("findings")):
        refs = _as_str_list(finding.get("evidence_refs")) or ()
        lines.append(f"- {finding.get('text')}（来源：{', '.join(refs)}）")
        for support in _as_mapping_list(finding.get("evidence_support")):
            lines.append(
                f"  - 反证材料绑定：{support.get('source_id')} / "
                f"「{support.get('source_quote')}」 / {support.get('basis')}"
            )
    lines += ["", "## V1 主张级信息盘点", ""]
    for claim in _as_mapping_list(research.get("v1_claims")):
        verdict = str(claim.get("verdict"))
        refs = _as_str_list(claim.get("evidence_refs")) or ()
        verification = _as_mapping(claim.get("independent_verification")) or {}
        v_status = str(verification.get("status"))
        v_mode = str(verification.get("mode"))
        v_refs = _as_str_list(verification.get("evidence_refs")) or ()
        lines += [
            f"### {claim.get('claim_id')}：{claim.get('text')}",
            "",
            f"- 原文引用：{claim.get('source_quote')}",
            f"- 主张类型：{claim.get('claim_kind')}",
            f"- 判定：{VERDICT_LABELS.get(verdict, verdict)}（{verdict}）",
            f"- 置信度：{claim.get('confidence')}",
            f"- 证据引用：{', '.join(refs) if refs else '（无）'}",
            f"- 主张推导（原文→主张）：{claim.get('claim_derivation')}",
            f"- 判定依据（主张→判定）：{claim.get('verdict_basis')}",
            f"- 独立验证状态：{VERIFICATION_STATUS_LABELS.get(v_status, v_status)}（{v_status}）",
            f"- 独立验证形式：{VERIFICATION_MODE_LABELS.get(v_mode, v_mode)}（{v_mode}）",
            f"- 独立验证依据：{verification.get('basis')}",
            f"- 独立验证引用：{', '.join(v_refs) if v_refs else '（无）'}",
        ]
        reviewer_ref = _as_str(verification.get("reviewer_ref"))
        if reviewer_ref:
            lines.append(f"- 独立审查者：{reviewer_ref}")
        for support in _as_mapping_list(verification.get("evidence_support")):
            lines.append(
                f"- 独立验证材料绑定：{support.get('source_id')} / "
                f"「{support.get('source_quote')}」 / {support.get('basis')}"
            )
        for support in _as_mapping_list(claim.get("evidence_support")):
            lines.append(
                f"- 证据片段绑定：{support.get('source_id')} / "
                f"{support.get('relation')} / 「{support.get('source_quote')}」 / "
                f"{support.get('basis')}"
            )
        lines.append("")
    lines += ["## V2 原文校准", ""]
    revisions = _as_mapping_list(research.get("v2_revisions"))
    if revisions:
        for revision in revisions:
            revision_status = str(revision.get("revision_status"))
            lines += [
                f"### 对 {revision.get('claim_id')} 的校准",
                "",
                f"- 复核状态：{REVISION_LABELS.get(revision_status, revision_status)}"
                f"（{revision_status}）",
                f"- V1 偏离点：{revision.get('deviation')}",
                f"- 修改理由：{revision.get('revision_reason')}",
                f"- 校准后：{revision.get('revised_text')}",
                "",
            ]
    else:
        lines.append("- 逐项复核后无修改")
    return lines


def _render_perspectives(perspectives_raw: object) -> list[str]:
    lines = ["", "## 三视角分析", ""]
    for perspective in _as_mapping_list(perspectives_raw):
        name = str(perspective.get("perspective"))
        lines += [
            f"### {PERSPECTIVE_LABELS.get(name, name)}",
            "",
            str(perspective.get("summary", "")),
            "",
            f"- 核心关联点：{perspective.get('association')}",
            f"- 视角价值：{perspective.get('value')}",
            f"- 不确定性：{perspective.get('uncertainty')}",
            "- 冲突：",
        ]
        conflicts = _as_str_list(perspective.get("conflicts")) or ()
        if conflicts:
            lines.extend(f"  - {entry}" for entry in conflicts)
        else:
            lines.append("  - （无）")
        lines.append("材料清单：")
        materials = _as_mapping_list(perspective.get("materials"))
        if materials:
            for material in materials:
                material_type = str(material.get("material_type"))
                label = MATERIAL_LABELS.get(material_type, material_type)
                entry = f"- [{label}] {material.get('text')}"
                if material_type == "profile_inference":
                    entry += (
                        f"（依据：{material.get('basis')}；"
                        f"不确定性：{material.get('uncertainty')}）"
                    )
                if material_type == "confirmed_user_fact":
                    entry += f"（确认引用：{material.get('confirmation_ref')}）"
                if material_type == "obsidian_record":
                    entry += f"（记录引用：{material.get('record_ref')}）"
                lines.append(entry)
        else:
            lines.append("- 材料不足")
        lines.append("")
    return lines


def _render_synthesis(synthesis: Mapping[str, object], route: str) -> list[str]:
    lines = [
        "",
        "## 综合判断",
        "",
        f"- 价值判断：{synthesis.get('value')}",
    ]
    if route == "research":
        credibility = str(synthesis.get("credibility"))
        counts = _as_mapping(synthesis.get("claim_counts")) or {}
        lines += [
            f"- 可信程度：{CREDIBILITY_LABELS.get(credibility, credibility)}（{credibility}）",
            f"- 可信依据：{synthesis.get('credibility_basis')}",
            "- 主张统计："
            f"支持 {counts.get('supported')}／"
            f"部分支持 {counts.get('partially_supported')}／"
            f"反对 {counts.get('contradicted')}／"
            f"未覆盖 {counts.get('not_covered')}",
        ]
    lines += [
        f"- 可信度最弱环节：{synthesis.get('weakest_link')}",
        "- 冲突清单：",
    ]
    conflicts = _as_str_list(synthesis.get("conflicts")) or ()
    if conflicts:
        lines.extend(f"  - {entry}" for entry in conflicts)
    else:
        lines.append("  - 未发现实质冲突")
    lines.append("- 待验证问题：")
    open_questions = _as_str_list(synthesis.get("open_questions")) or ()
    if open_questions:
        lines.extend(f"  - {entry}" for entry in open_questions)
    else:
        lines.append("  - 未发现实质待验证问题")
    lines.append(f"- 建议下一步：{synthesis.get('next_step')}")
    return lines
