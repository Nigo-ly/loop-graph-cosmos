"""Loop V1：plan v5 真实结果到 Package E 定版产品投影的桥接（纯离线）。

填补的缺口：plan v5 的真实单次 DeepSeek 结果停留在长篇审计草稿（标题是
碎片截断文本，逐字引用目录占据主阅读流），而 Package E 的产品层只接受经
``build_cognitive_draft_view`` 重投影的持久化认知结果。二者没有稳定桥接。

本模块就是那条桥：输入严格的 v5 manifest 与已完成 ledger，在零模型、零
网络、零凭据下输出独立的 Package E 定版候选 bundle。它不生成第二套产品
格式——全部人类阅读版、候选知识卡、Graph-ready 投影、主页投影与机器
sidecar 都复用 ``fragment_loop.cognitive_product`` 的既有 contract /
render / graph / home 逻辑，经由其 view 级入口
``build_cognitive_product_from_view``。

为什么不是把 v5 结果伪装成 R1-A 认知契约对象：R1-A 要求非「未覆盖」主张
至少引用一个已核验（authenticity=verified）来源，而 v5 的公开来源是媒体
二手报道，诚实状态只能是 unverified。把媒体来源标成 verified 就是证据升
级，被硬边界明确禁止。因此本桥接自建一个与 draft view 同形的 bridge
view，其证据保证来自 v5 自己的严格校验链（``validate_combined_result``
+ 确定性 quote catalog + session identity），而不是伪造一套契约对象。

失败关闭（任何一项不满足都零输出抛 ``ProductBridgeError``）：

- manifest 未通过 ``load_manifest`` 的既有严格校验；
- ledger 缺失、损坏、计划版本或 session identity 漂移（由 ``SessionLedger``
  既有加载逻辑拒绝）、没有恰好一条 ``combined`` reservation、该
  reservation 不是 completed（含失败账本与 reserved 未完成的未知发送）、
  provider 不是 deepseek_v4_pro 或 model_claim 不是 deepseek-v4-pro；
- ledger 中的 result 未通过既有 ``validate_combined_result``（用从
  manifest 重建的 catalog 与 context ids 复验）；
- 输出目录与 manifest 的草稿目录相同（旧审计草稿必须字节不变）；
- 关键事实只能来自有本地 quote/source 绑定的 claim view（统一标注
  「媒体报道声称」），不足 3 条即失败关闭而不编造；未绑定的
  literal_facts／structural_inferences 展示字段绝不进入关键事实；
- supported 主张的数字／型号标记未被其绑定 quote 逐字覆盖时，先降为
  partially_supported 并生成专门 fidelity 纠偏，且不得生成候选卡；
- 候选知识卡最多 2 张，只能来自仍为 supported 且有来源绑定的主张；
- Package E 产品层自身的全部契约校验与表面扫描。

重放纪律：输出写入调用方明确指定的新目录；同一输入重放时字节一致即返回
``already_published``，任何文件名或内容冲突都失败关闭。本桥接从不调用模
型、从不读取凭据、从不修改 manifest / ledger / 旧审计草稿（ledger 只经
``SessionLedger`` 的既有只读视图访问）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fragment_loop.cognitive_final_acceptance import (
    AUTHORIZATION,
    PLAN_VERSION,
    AssembledInput,
    FinalAcceptanceBudgetError,
    FinalAcceptanceInputError,
    QuoteCatalog,
    SessionLedger,
    build_quote_catalog,
    check_local_path_safety,
    context_card_ids,
    derived_claim_binding,
    load_manifest,
    session_identity,
    validate_combined_result,
)
from fragment_loop.cognitive_product import (
    PRODUCT_VERSION,
    CognitiveProductError,
    build_cognitive_product_from_view,
)

BRIDGE_VERSION = "loop-v1-v5-product-bridge-v1"

_PROVIDER = "deepseek_v4_pro"
_MODEL = "deepseek-v4-pro"
_STAGE = "combined"
_MAX_TITLE_LENGTH = 120

# 媒体二手来源的诚实标注：它只能证明“媒体这样报道／主张”，不能升级成论文
# 或技术事实已独立验证。所有 claim 的独立验证状态固定为未验证。
MEDIA_INDEPENDENT_VERIFICATION: dict[str, object] = {
    "status": "not_verified",
    "mode": "none",
    "basis": "公开来源是媒体二手报道，只能证明媒体这样报道，未独立验证原论文或技术事实",
}

_CARD_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
# Numeric/model-token fidelity：主张文本中的数字（含小数与百分号）与型号
# 标记（如 Model3.5-1B）必须被其绑定 quote 逐字覆盖，否则该主张不得保持
# supported——媒体引文没有逐字给出的数字不能当作引文支持的事实。
_NUMERIC_TOKEN = re.compile(r"\d+(?:\.\d+)?%?")
_MODEL_TOKEN = re.compile(r"[A-Za-z]+\d+(?:[.-]\d+[A-Za-z]*)+")
# 媒体二手来源支持的事实只能表述为“媒体这样声称”，绝不表述为已独立验证。
_MEDIA_CLAIM_PREFIX = "媒体报道声称："
_MAX_CANDIDATE_CARDS = 2


class ProductBridgeError(RuntimeError):
    """v5 → Package E 桥接的任何输入、身份或输出冲突；零输出的失败关闭。"""


# ------------------------------------------------------------ 输入加载与校验


def load_verified_v5_session(
    manifest_path: Path,
) -> tuple[AssembledInput, QuoteCatalog, str, dict[str, object]]:
    """加载严格 v5 manifest 与已完成 ledger；任何漂移零输出失败关闭。

    返回 ``(assembled_input, quote_catalog, session_identity, result)``。
    本函数只读 manifest 与 ledger（经 ``SessionLedger`` 既有只读视图），
    不写任何文件、不调用模型、不读取凭据。
    """
    try:
        value = load_manifest(manifest_path)
    except FinalAcceptanceInputError as error:
        raise ProductBridgeError(f"manifest_rejected:{error}") from None
    catalog = build_quote_catalog(value)
    session = session_identity(value, AUTHORIZATION)
    ledger = SessionLedger(
        value.ledger_path,
        session,
        catalog=catalog,
        context_ids=context_card_ids(value),
    )
    try:
        view = ledger.view()
    except FinalAcceptanceBudgetError as error:
        # 账本缺失、损坏、计划版本或 session identity 漂移都在这里失败关闭。
        raise ProductBridgeError(f"ledger_rejected:{error}") from None
    if view.get("version") != PLAN_VERSION:
        raise ProductBridgeError("ledger_plan_version_mismatch")
    reservations = view.get("reservations")
    if not isinstance(reservations, list):
        raise ProductBridgeError("ledger_invalid")
    if len(reservations) != 1:
        raise ProductBridgeError("ledger_reservation_count_invalid")
    reservation = reservations[0]
    if not isinstance(reservation, dict):
        raise ProductBridgeError("ledger_invalid")
    if reservation.get("stage") != _STAGE:
        raise ProductBridgeError("ledger_stage_mismatch")
    # 失败账本与 reserved 未完成的未知发送都在这里失败关闭。
    if reservation.get("status") != "completed":
        raise ProductBridgeError("ledger_combined_not_completed")
    if reservation.get("provider") != _PROVIDER:
        raise ProductBridgeError("ledger_provider_mismatch")
    if reservation.get("model_claim") != _MODEL:
        raise ProductBridgeError("ledger_model_mismatch")
    result = reservation.get("result")
    if not isinstance(result, dict):
        raise ProductBridgeError("ledger_result_missing")
    errors = validate_combined_result(
        result,
        catalog=catalog,
        context_ids=context_card_ids(value),
    )
    if errors:
        raise ProductBridgeError(f"result_invalid:{errors[0]}")
    return value, catalog, session, result


# ------------------------------------------------------------ bridge view 构造


def _claims(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = result.get("claims")
    if not isinstance(raw, list):
        return []
    return [claim for claim in raw if isinstance(claim, Mapping)]


def _fidelity_failures(text: str, quote_text: str) -> list[str]:
    """主张文本中未被绑定 quote 逐字覆盖的数字／型号标记（按出现顺序去重）。"""
    failures: list[str] = []
    for pattern in (_MODEL_TOKEN, _NUMERIC_TOKEN):
        for token in pattern.findall(text):
            if token not in quote_text and token not in failures:
                failures.append(token)
    return failures


def _claim_views(
    catalog: QuoteCatalog, result: Mapping[str, object]
) -> list[dict[str, object]]:
    """v5 claims 的 bridge view 形态：evidence_refs 来自本地推导绑定，
    媒体边界固定 confidence=low、独立验证 not_verified+none。

    Numeric/model-token fidelity：supported 主张的数字或型号标记若未被其
    绑定 quote 逐字覆盖，降为 partially_supported 并记录失败标记——该
    主张不得生成候选知识卡，且必须生成专门的 fidelity 纠偏。
    """
    claims: list[dict[str, object]] = []
    for claim in _claims(result):
        binding = derived_claim_binding(catalog, dict(claim))
        verdict = str(claim["verdict"])
        evidence_refs = list(binding["source_refs"]) if verdict != "not_covered" else []
        entry = catalog.lookup(claim["quote_id"])
        quote_text = entry.text if entry is not None else ""
        failures = (
            _fidelity_failures(str(claim["text"]), quote_text)
            if verdict == "supported"
            else []
        )
        if failures:
            verdict = "partially_supported"
        claims.append(
            {
                "claim_id": str(claim["claim_id"]),
                "text": str(claim["text"]),
                "verdict": verdict,
                "confidence": "low",
                "evidence_refs": evidence_refs,
                "fidelity_failures": failures,
                "independent_verification": dict(MEDIA_INDEPENDENT_VERIFICATION),
            }
        )
    return claims


def build_bridge_view(
    value: AssembledInput,
    catalog: QuoteCatalog,
    result: Mapping[str, object],
) -> dict[str, object]:
    """把已验证的 v5 结果投影成与 draft view 同形的 bridge view（纯函数）。

    每条 claim 的 ``evidence_refs`` 来自 v5 本地推导绑定（quote layer →
    source_id），媒体边界固定：confidence=low、独立验证 not_verified+none；
    本地 context_refs 只进入机器 sidecar，不进入来源层（本地材料不是外部
    事实来源）。
    """
    source = value.public_source
    claims = _claim_views(catalog, result)
    cited_quote_ids: list[str] = []
    for claim in _claims(result):
        binding = derived_claim_binding(catalog, dict(claim))
        quote_id = str(claim["quote_id"])
        if binding["source_refs"] and quote_id not in cited_quote_ids:
            cited_quote_ids.append(quote_id)
    cited_entry = catalog.lookup(cited_quote_ids[0]) if cited_quote_ids else None
    if cited_entry is not None:
        excerpt = cited_entry.text
    else:
        source_segments = [
            entry.text for entry in catalog.entries if entry.layer == source.source_id
        ]
        excerpt = source_segments[0] if source_segments else source.title
    source_view: dict[str, object] = {
        "source_id": source.source_id,
        "name": source.title,
        "locator": value.sanitized_public_url,
        "authenticity": "unverified",
        "relevance": "direct",
        "independence": "non_independent",
        "source_tier": "secondary",
        "evidence_excerpt": excerpt,
    }
    return {
        "view_version": "loop-v1-v5-bridge-view-v1",
        "route": "research",
        "fragment_text": value.fragment_text,
        "evidence_level": "unverified",
        "research": {
            "sources": [source_view],
            "v1_claims": claims,
        },
    }


# ------------------------------------------------------------ 产品对象组装


def _display_strings(result: Mapping[str, object], field: str) -> list[str]:
    raw = result.get(field)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


def _card_id(claim_id: str) -> str:
    cleaned = _CARD_ID_SAFE.sub("-", claim_id).strip("-") or "claim"
    return f"card-{cleaned}"[:64]


def _corrections(claims: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """确定性关键纠偏：先逐条覆盖部分支持／被反对主张（产品层强制），再
    补一条媒体边界纠偏；总数超过产品层上限时失败关闭，不裁剪强制项。"""
    corrections: list[dict[str, object]] = []
    for claim in claims:
        verdict = str(claim["verdict"])
        text = str(claim["text"])
        failures = claim.get("fidelity_failures")
        if isinstance(failures, list) and failures:
            # 专门 fidelity 纠偏：数字／型号未被绑定引文逐字覆盖的降级。
            corrections.append(
                {
                    "misreading": f"把「{text}」中的数字／型号标记当成引文逐字覆盖的事实",
                    "correction": "标记 "
                    + "、".join(str(token) for token in failures)
                    + " 未在绑定引文中逐字出现，该主张已从支持降为部分支持",
                    "basis": "本地 fidelity 校验：主张的数字／型号标记必须被绑定 "
                    "quote 逐字覆盖，媒体引文未给出的数字不得当作引文支持的事实",
                    "claim_refs": [str(claim["claim_id"])],
                }
            )
        elif verdict == "partially_supported":
            corrections.append(
                {
                    "misreading": f"把「{text}」当成已被公开来源完全证实",
                    "correction": "公开来源只部分支持该主张，其余部分本轮没有证据覆盖",
                    "basis": "本地推导判定为部分支持，证据仅来自单一媒体二手报道，未独立验证",
                    "claim_refs": [str(claim["claim_id"])],
                }
            )
        elif verdict == "contradicted":
            corrections.append(
                {
                    "misreading": f"把「{text}」当成与公开来源一致",
                    "correction": "公开来源与该主张相矛盾，该主张不成立",
                    "basis": "本地推导判定为反对，证据仅来自单一媒体二手报道，未独立验证",
                    "claim_refs": [str(claim["claim_id"])],
                }
            )
    bound_ids = [
        str(claim["claim_id"])
        for claim in claims
        if claim.get("evidence_refs")
    ]
    if bound_ids:
        corrections.append(
            {
                "misreading": "把媒体报道的转述当成原论文或技术事实已被独立核验",
                "correction": "该来源是媒体二手报道，只能证明媒体这样报道／主张；"
                "报道转述的数字与效果未独立核验原论文",
                "basis": "全部来源绑定主张的独立验证状态均为未验证（媒体二手来源）",
                "claim_refs": bound_ids,
            }
        )
    return corrections


def assemble_bridge_product(
    value: AssembledInput,
    catalog: QuoteCatalog,
    result: Mapping[str, object],
) -> dict[str, object]:
    """把已验证的 v5 结果确定性组装成 Package E 产品对象（纯函数）。

    标题取公开来源的语义标题（绝不使用 URL 截断或碎片截断）；关键事实只
    来自有本地 quote/source 绑定的 claim view 并统一标注「媒体报道声
    称」，不足 3 条即失败关闭而不编造；候选知识卡最多 2 张，只能来自仍
    为 supported（fidelity 通过）且有来源绑定的主张，没有时诚实为 0
    张；memory_view／personal_view／project_view 的模型猜测完全不进入本
    组装——本地上下文为 0 时连接段必然为空，模型对记忆／个人／项目的任
    何猜测都不会泄漏到阅读面。fragment_ref 固定为安全的合成相对引用
    ``fragments/<fragment_id>.md``，供 frontmatter 与主页同源去重。
    """
    claim_list = _claim_views(catalog, result)
    corrections = _corrections(claim_list)
    # 关键事实只能来自有本地 quote/source 绑定的 claim view（按文本去重，
    # 只取 distinct 绑定主张），统一标注「媒体报道声称」；未绑定的
    # literal_facts／structural_inferences 展示字段绝不进入关键事实。不足
    # 3 条失败关闭，不编造凑数。
    key_facts: list[str] = []
    for claim in claim_list:
        if not claim.get("evidence_refs"):
            continue
        fact = f"{_MEDIA_CLAIM_PREFIX}{claim['text']}"
        if fact not in key_facts:
            key_facts.append(fact)
    key_facts = key_facts[:5]
    if len(key_facts) < 3:
        raise ProductBridgeError("insufficient_honest_key_facts")
    synthesis = result.get("synthesis")
    synthesis_map: Mapping[str, object] = (
        synthesis if isinstance(synthesis, Mapping) else {}
    )
    judgment = synthesis_map.get("judgment")
    statement = (
        str(judgment)
        if isinstance(judgment, str) and judgment.strip()
        else "本次单次调用未生成综合判断；全部内容保持草稿口径，必须人工确认"
    )
    next_steps = _display_strings(synthesis_map, "next_steps")
    # 单一媒体二手来源 + 单次调用：可信度上限就是 low；完全没有来源绑定
    # 时进一步降为 insufficient。
    bound = any(claim.get("evidence_refs") for claim in claim_list)
    credibility = "low" if bound else "insufficient"
    cards: list[dict[str, object]] = []
    for claim in claim_list:
        # 候选卡最多 2 张，只能来自仍为 supported（fidelity 通过）且有
        # 来源绑定的主张；not_covered／partial（含 fidelity 降级）一律排除。
        if (
            str(claim["verdict"]) != "supported"
            or not claim.get("evidence_refs")
            or len(cards) >= _MAX_CANDIDATE_CARDS
        ):
            continue
        text = str(claim["text"])
        cards.append(
            {
                "card_id": _card_id(str(claim["claim_id"])),
                "title": text[:40],
                "knowledge": f"{text}（依据仅为单一媒体二手报道，未独立验证）",
                "why_important": "该主张是碎片主题下被公开来源支持的核心事实判断，"
                "值得作为候选沉淀，待人工确认",
                "scope": "仅适用于该媒体报道语境，不得外推为普遍结论",
                "evidence_refs": [str(claim["claim_id"])],
            }
        )
    connections: list[dict[str, object]] = []
    for card in value.context_cards:
        connections.append(
            {
                "text": f"碎片与本地材料《{card.title}》相关"
                "（用户既有认知材料，不是外部事实来源）",
                "target_kind": "local_note",
                "target_ref": card.ref,
                "relation": "references",
            }
        )
    return {
        "title": value.public_source.title[:_MAX_TITLE_LENGTH],
        "core_judgment": {
            "statement": statement,
            "user_value": "提供一条媒体二手报道的草稿级解读与证据边界，"
            "供你判断是否与自身研究相关",
            "credibility": credibility,
            "suggested_action": "人工核对媒体原文与本地材料后，再决定是否确认该草稿",
        },
        "key_facts": key_facts,
        "critical_corrections": corrections,
        "personal_connections": connections,
        "blind_spots": _display_strings(result, "unknowns"),
        "conclusion": {
            "summary": statement,
            "next_step": next_steps[0]
            if next_steps
            else "人工确认草稿后，再决定是否独立核验报道转述的原始论文",
        },
        "candidate_cards": cards,
        "topics": [],
        "fragment_ref": f"fragments/{value.fragment_id}.md",
    }


# ------------------------------------------------------------ 发布（幂等、原子）


def _publish_payloads(output_dir: Path, payloads: dict[str, bytes]) -> str:
    """与 ``publish_draft`` 相同的原子幂等纪律：重放同字节返回
    ``already_published``，任何文件名或内容冲突失败关闭。"""
    path_errors = check_local_path_safety(output_dir, expect="dir")
    if path_errors:
        raise ProductBridgeError(f"path_unsafe:{path_errors[0]}")
    if output_dir.exists():
        names = {path.name for path in output_dir.iterdir()}
        if names != set(payloads):
            raise ProductBridgeError("publish_conflict")
        for name, payload in payloads.items():
            path = output_dir / name
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ProductBridgeError("publish_conflict")
        return "already_published"
    parent = output_dir.parent
    prefix = f".{output_dir.name}.staging-"
    if parent.exists():
        for leftover in parent.iterdir():
            if leftover.name.startswith(prefix):
                raise ProductBridgeError("publish_staging_residue")
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        for name, payload in payloads.items():
            path = staging / name
            path.write_bytes(payload)
            path.chmod(0o600)
        os.replace(staging, output_dir)
    except Exception:
        # 清理必须是 best-effort：重复或失败的 unlink/rmdir 不得抛出二次
        # 错误掩盖原始异常（与 SessionLedger._write 的清理纪律一致）。
        for path in staging.iterdir():
            try:
                path.unlink()
            except OSError:
                pass
        try:
            staging.rmdir()
        except OSError:
            pass
        raise
    return "published"


def bridge_v5_to_product(manifest_path: Path, output_dir: Path) -> dict[str, Any]:
    """桥接入口：严格 v5 manifest + 已完成 ledger → Package E 定版候选。

    零模型、零网络、零凭据；输出进入调用方明确指定的新目录，旧审计草稿
    字节不变；同一输入重放返回 ``already_published``。
    """
    value, catalog, session, result = load_verified_v5_session(manifest_path)
    if output_dir.resolve() == value.output_dir.resolve():
        raise ProductBridgeError("output_dir_conflicts_with_draft_dir")
    view = build_bridge_view(value, catalog, result)
    product = assemble_bridge_product(value, catalog, result)
    try:
        bundle = build_cognitive_product_from_view(view, product)
    except CognitiveProductError as error:
        raise ProductBridgeError(f"product_rejected:{error}") from None
    base = f"fragment-product-{value.fragment_id}-{session[:16]}"
    markdown_bytes = str(bundle["human_markdown"]).encode("utf-8")
    v5_claim_bindings: dict[str, object] = {}
    for claim in _claims(result):
        binding = derived_claim_binding(catalog, dict(claim))
        v5_claim_bindings[str(claim["claim_id"])] = {
            "quote_id": str(claim["quote_id"]),
            "source_refs": list(binding["source_refs"]),
            "context_refs": list(binding["context_refs"]),
        }
    machine_bundle: dict[str, object] = {
        "bridge_version": BRIDGE_VERSION,
        "product_version": PRODUCT_VERSION,
        "plan_version": PLAN_VERSION,
        "session_identity": session,
        "quote_catalog_sha256": catalog.sha256,
        "fragment_id": value.fragment_id,
        "lifecycle_status": "draft",
        "evidence_level": "unverified",
        "user_confirmed": False,
        "promoted_to_asset": False,
        "paper_independently_verified": False,
        "source_basis": "media_second_hand",
        "draft_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "candidate_cards": bundle["candidate_cards"],
        "graph_projection": bundle["graph_projection"],
        "home_projection": bundle["home_projection"],
        "sidecar": bundle["sidecar"],
        "v5_claim_bindings": v5_claim_bindings,
    }
    payloads = {
        f"{base}.md": markdown_bytes,
        f"{base}.json": (
            json.dumps(machine_bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8"),
    }
    status = _publish_payloads(output_dir, payloads)
    return {
        "status": status,
        "bridge_version": BRIDGE_VERSION,
        "files": sorted(payloads),
        "draft_sha256": machine_bundle["draft_sha256"],
        "session_identity": session,
    }
