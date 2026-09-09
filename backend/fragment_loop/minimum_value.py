"""手机碎片最小价值链的供应商无关状态流与内容校验。

默认配置仍只接受合成 FragmentEnvelope；第三步只有通过独立 live wiring 显式开启
user_selected_local 并注入冻结 adapter 才能处理用户选择的本地文本。运行状态复用
现有 LoopSupervisor 与 SQLiteCheckpointStore，不新建状态机、数据库、账本或事件
总线；草稿与发布结果只写调用方传入的目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from common.checkpoint import (
    LoopCheckpoint,
    SQLiteCheckpointStore,
    checkpoint_compat_view,
    utc_now,
)
from common.execution import plugin_eval, translate_view_edit
from common.supervisor import AdmissionState, LoopSupervisor, SupervisorContext
from common.types import LoopResult
from fragment_loop.intake import FragmentEnvelope, fragment_title_for
from fragment_loop.spec import PHONE_FRAGMENT_MINIMUM_VALUE_V1

TARGET_PROVIDER = "synthetic"
TARGET_MODEL = "deterministic-draft-fixture"
TARGET_PROFILE = "phone-fragment-synthetic-draft-v1"

SOURCE_NOTE_REQUIRED = "来源：用户提供的原始碎片"
RESULT_DOC_TYPE = "用户确认的整理结果"
FORBIDDEN_NAMING = ("已验证资产", "validated")

STOP_AWAITING_SEND = "awaiting_send_consent"
STOP_AWAITING_DRAFT = "awaiting_draft_confirmation"
STOP_PRIVACY_BLOCKED = "privacy_blocked"

CONTENT_MARKERS = frozenset(
    {
        "directly_supported",
        "normalized_transform",
        "grounded_inference",
        "unverified_inference",
        "conflicted_with_source",
    }
)

EDITABLE_FIELDS = frozenset(
    {
        "title",
        "summary",
        "key_information",
        "uncertainties",
        "source_note",
        "suggested_tags",
        "suggested_next_step",
    }
)

SYNTHETIC_PRIVACY_MARKERS = {
    "[SYNTHETIC_ID]": "id_number",
    "[SYNTHETIC_PHONE]": "phone_number",
}

_JSON_BLOCK_RE = re.compile(r"<!-- minimum-value-json\n(.*?)\n-->", re.DOTALL)

PrivacyScan = Callable[[str], tuple[str, ...]]

_MAX_CAS_ATTEMPTS = 8


class PreSendFailureError(Exception):
    """Adapter 用来明确表示「确认的发送前失败」（request_sent=false）。"""


@dataclass(frozen=True)
class ModelCallEvidence:
    provider: str
    response_model_claim: str
    http_status: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "response_model_claim": self.response_model_claim,
            "http_status": self.http_status,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_status": self.cost_status,
        }


@dataclass(frozen=True)
class DraftAdapterResult:
    draft: Mapping[str, Any]
    evidence: ModelCallEvidence


DraftAdapter = Callable[[str], Mapping[str, Any] | DraftAdapterResult]


class PublishConflictError(RuntimeError):
    """固定结果路径上的内容冲突：进入安全失败，不覆盖。"""


class DraftConflictError(RuntimeError):
    """固定草稿路径上的内容冲突：保持现状，不覆盖。"""


@dataclass(frozen=True)
class SendPreview:
    run_id: str
    status: str
    stop_reason: str | None
    outbound_payload: str | None
    outbound_payload_sha256: str
    target_provider: str
    target_model: str
    target_profile: str
    privacy_blocked: bool
    privacy_field_categories: tuple[str, ...]


@dataclass(frozen=True)
class PublishOutcome:
    result_id: str
    path: str
    content_sha256: str
    decision: str
    edited_fields: tuple[str, ...]
    already_published: bool


@dataclass(frozen=True)
class WithdrawOutcome:
    result_id: str
    path: str
    lifecycle: str
    withdrawn_at: str
    already_withdrawn: bool


def default_privacy_scan(text: str) -> tuple[str, ...]:
    """确定性合成隐私检查：命中标记只返回字段类别。"""
    categories = {
        category for marker, category in SYNTHETIC_PRIVACY_MARKERS.items() if marker in text
    }
    return tuple(sorted(categories))


def build_outbound_payload(fragment_text: str) -> str:
    """由碎片原文确定性地生成待发送正文（第一版固定模板）。"""
    return (
        "请把以下用户手机碎片整理为结构化草稿。\n\n"
        "用户碎片原文：\n"
        f"{fragment_text}\n\n"
        "要求：只依据原文，不确定的内容显式列出，禁止虚构来源；"
        "没有外部来源时来源说明必须写「来源：用户提供的原始碎片」。"
    )


def build_local_candidate(fragment_text: str) -> dict[str, Any]:
    """第一步的最小确定性整理：规范化、片段内去重、时间提取和来源标记。"""
    normalized = re.sub(r"\s+", " ", fragment_text).strip()
    raw_segments = [
        segment.strip()
        for segment in re.split(r"(?<=[；;。！？\n])", normalized)
        if segment.strip()
    ]
    action_candidates = list(dict.fromkeys(raw_segments))
    time_mentions = list(
        dict.fromkeys(
            re.findall(
                r"(?:下周|本周|这周|周|星期)[一二三四五六日天]"
                r"|(?:[01]?\d|2[0-3]):[0-5]\d"
                r"|\d{1,2}月\d{1,2}日",
                normalized,
            )
        )
    )
    return {
        "source_type": "user_statement",
        "normalized_text": normalized,
        "content_sha256": payload_sha256(normalized),
        "time_mentions": time_mentions,
        "action_candidates": action_candidates,
        "duplicate_segments_removed": len(raw_segments) - len(action_candidates),
    }


def payload_sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_sha256(content: Mapping[str, Any]) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _chain_claim_errors(
    claimed_text: object,
    claim: object,
    fragment_text: str,
    label: str,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(claimed_text, str) or not claimed_text.strip():
        return [f"{label} 缺少声明文本"]
    if not isinstance(claim, dict):
        return [f"{label} 缺少内容链标记对象"]
    marker = claim.get("marker")
    if marker not in CONTENT_MARKERS:
        return [f"{label} 未知内容链标记: {marker!r}"]
    quote = claim.get("source_quote")
    if marker == "directly_supported":
        if (
            not isinstance(quote, str)
            or not quote
            or quote not in fragment_text
            or claimed_text.strip() != quote.strip()
        ):
            errors.append(f"{label} directly_supported 必须与原文 source_quote 逐字一致")
    elif marker == "normalized_transform":
        if not isinstance(quote, str) or not quote or quote not in fragment_text:
            errors.append(f"{label} normalized_transform 必须提供原文 quote")
        elif label == "summary" and quote.strip() != fragment_text.strip():
            errors.append("summary normalized_transform 必须引用完整原文")
        note = claim.get("transformation_note")
        if not isinstance(note, str) or not note.strip():
            errors.append(f"{label} normalized_transform 必须提供 transformation_note")
    else:
        note = claim.get("note")
        if not isinstance(note, str) or not note.strip():
            errors.append(f"{label} {marker} 必须提供 note 说明")
    return errors


def _content_field_errors(raw: Mapping[str, Any], fragment_text: str) -> list[str]:
    """七个必需内容字段、来源说明、内容链标记与禁用命名的确定性校验。"""
    errors: list[str] = []
    for field_name in ("title", "summary", "source_note", "suggested_next_step"):
        value = raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"缺少必需文本字段或为空: {field_name}")
    source_note = raw.get("source_note")
    if isinstance(source_note, str) and source_note.strip() != SOURCE_NOTE_REQUIRED:
        errors.append("source_note 必须精确为「来源：用户提供的原始碎片」")
    errors.extend(
        _chain_claim_errors(
            raw.get("summary"), raw.get("summary_evidence"), fragment_text, "summary"
        )
    )
    key_information = raw.get("key_information")
    if not isinstance(key_information, list):
        errors.append("key_information 必须是列表")
        key_information = []
    for index, item in enumerate(key_information):
        if not isinstance(item, dict):
            errors.append(f"key_information[{index}] 必须是对象")
            continue
        claim = dict(item)
        errors.extend(
            _chain_claim_errors(
                item.get("text"), claim, fragment_text, f"key_information[{index}]"
            )
        )
    uncertainties = raw.get("uncertainties")
    if not isinstance(uncertainties, list):
        errors.append("uncertainties 必须是列表")
    elif any(
        not isinstance(entry, str) or not entry.strip() for entry in uncertainties
    ):
        errors.append("uncertainties 的每一项必须是非空白字符串")
    suggested_tags = raw.get("suggested_tags")
    if not isinstance(suggested_tags, list) or any(
        not isinstance(entry, str) for entry in suggested_tags
    ):
        errors.append("suggested_tags 必须是字符串列表")
    serialized = json.dumps(raw, ensure_ascii=False)
    for forbidden in FORBIDDEN_NAMING:
        if forbidden in serialized:
            errors.append(f"出现禁用命名: {forbidden}")
    return errors


def validate_model_draft(raw: Mapping[str, Any], fragment_text: str) -> list[str]:
    """模型草稿校验：内容字段 + 只能是 draft + unverified。"""
    errors = _content_field_errors(raw, fragment_text)
    if raw.get("lifecycle", "draft") != "draft":
        errors.append("模型草稿的 lifecycle 必须是 draft")
    if raw.get("evidence_level", "unverified") != "unverified":
        errors.append("模型草稿的 evidence_level 必须是 unverified，不得自动升级")
    return errors


def validate_publish_content(raw: Mapping[str, Any], fragment_text: str) -> list[str]:
    """发布内容校验：字段与命名规则不变，证据等级仍必须是 unverified。"""
    errors = _content_field_errors(raw, fragment_text)
    if raw.get("evidence_level", "unverified") != "unverified":
        errors.append("发布内容的 evidence_level 必须是 unverified")
    return errors


def normalize_draft_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把 adapter 原始输出规范化为固定字段集合的草稿内容。"""
    key_information: list[dict[str, Any]] = []
    for item in raw.get("key_information") or []:
        if not isinstance(item, dict):
            continue
        key_information.append(
            {
                "text": item.get("text"),
                "marker": item.get("marker"),
                "source_quote": item.get("source_quote"),
                "transformation_note": item.get("transformation_note"),
                "note": item.get("note"),
            }
        )
    return {
        "title": raw.get("title"),
        "summary": raw.get("summary"),
        "summary_evidence": {
            "marker": raw.get("summary_evidence", {}).get("marker"),
            "source_quote": raw.get("summary_evidence", {}).get("source_quote"),
            "transformation_note": raw.get("summary_evidence", {}).get(
                "transformation_note"
            ),
            "note": raw.get("summary_evidence", {}).get("note"),
        },
        "key_information": key_information,
        "uncertainties": list(raw.get("uncertainties") or []),
        "source_note": raw.get("source_note"),
        "suggested_tags": list(raw.get("suggested_tags") or []),
        "suggested_next_step": raw.get("suggested_next_step"),
    }


def _render_draft_body(content: Mapping[str, Any], lifecycle: str = "draft") -> str:
    summary_evidence = content["summary_evidence"]
    summary_chain = f"> 摘要内容链：{summary_evidence['marker']}"
    if summary_evidence.get("source_quote"):
        summary_chain += f"（原文：{summary_evidence['source_quote']}）"
    if summary_evidence.get("transformation_note"):
        summary_chain += f"（转换说明：{summary_evidence['transformation_note']}）"
    if summary_evidence.get("note"):
        summary_chain += f"（说明：{summary_evidence['note']}）"
    lines = [
        f"> 生命周期：{lifecycle}",
        "> 证据等级：unverified",
        "",
        f"# 草稿：{content['title']}",
        "",
        "## 简短整理结果",
        "",
        str(content["summary"]),
        "",
        summary_chain,
    ]
    lines += ["", "## 关键信息", ""]
    for item in content["key_information"]:
        entry = f"- [{item['marker']}] {item['text']}"
        if item.get("source_quote"):
            entry += f"（原文：{item['source_quote']}）"
        if item.get("transformation_note"):
            entry += f"（转换说明：{item['transformation_note']}）"
        if item.get("note"):
            entry += f"（说明：{item['note']}）"
        lines.append(entry)
    if not content["key_information"]:
        lines.append("- （无）")
    lines += ["", "## 不确定内容", ""]
    lines += [f"- {entry}" for entry in content["uncertainties"]] or ["- （无）"]
    lines += ["", "## 信息来源说明", "", str(content["source_note"])]
    lines += ["", "## 建议标签", ""]
    lines += [f"- {tag}" for tag in content["suggested_tags"]] or ["- （无）"]
    lines += ["", "## 建议下一步", "", str(content["suggested_next_step"])]
    return "\n".join(lines)


def _render_result_body(
    content: Mapping[str, Any], doc_type: str, lifecycle: str = "published"
) -> str:
    body = _render_draft_body(content, lifecycle).replace("# 草稿：", "# ", 1)
    return f"> 类型：{doc_type}\n{body}"


def _write_doc(path: Path, payload: Mapping[str, Any], body: str) -> None:
    """同目录临时文件加原子替换写入文档。"""
    _write_doc_bytes(path, _doc_text(payload, body), replace_existing=True)


def _create_doc(path: Path, payload: Mapping[str, Any], body: str) -> None:
    """以原子 no-clobber 方式首次创建文档；目标已存在时绝不覆盖。"""
    _write_doc_bytes(path, _doc_text(payload, body), replace_existing=False)


def _write_doc_bytes(path: Path, text: str, *, replace_existing: bool) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        if replace_existing:
            os.replace(temporary_path, path)
            temporary_path = None
        else:
            os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _doc_text(payload: Mapping[str, Any], body: str) -> str:
    return (
        "<!-- minimum-value-json\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n-->\n\n"
        + body
        + "\n"
    )


def _parse_doc(text: str) -> dict[str, Any]:
    match = _JSON_BLOCK_RE.search(text)
    if match is None:
        raise ValueError("文档缺少 minimum-value-json 元数据块")
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("minimum-value-json 元数据块必须是对象")
    return data


def _read_doc(path: Path) -> dict[str, Any]:
    return _parse_doc(path.read_text(encoding="utf-8"))


def _deterministic_run_id(fragment_id: str) -> str:
    identity = ":".join(
        (
            PHONE_FRAGMENT_MINIMUM_VALUE_V1.loop_id,
            PHONE_FRAGMENT_MINIMUM_VALUE_V1.version,
            fragment_id,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


class MinimumValueService:
    """手机碎片最小价值链服务，默认保持离线合成边界。

    运行状态只写现有 SQLiteCheckpointStore；草稿与结果只写 workspace 下的
    drafts/ 与 results/ 目录。合成 adapter 每次同意最多放行一次调用；
    request_sent=true 或未知即永久消耗该组合的调用名额。
    """

    def __init__(
        self,
        db_path: str | Path,
        workspace: str | Path,
        adapter: DraftAdapter,
        privacy_scan: PrivacyScan | None = None,
        *,
        allow_selected_local: bool = False,
        target_provider: str = TARGET_PROVIDER,
        target_model: str = TARGET_MODEL,
        target_profile: str = TARGET_PROFILE,
    ) -> None:
        for label, value in (
            ("target_provider", target_provider),
            ("target_model", target_model),
            ("target_profile", target_profile),
        ):
            if not value.strip() or len(value) > 256 or "\x1f" in value:
                raise ValueError(f"{label} 无效")
        self.store = SQLiteCheckpointStore(db_path)
        self.workspace = Path(workspace)
        self.drafts_dir = self.workspace / "drafts"
        self.results_dir = self.workspace / "results"
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._adapter = adapter
        self._privacy_scan = privacy_scan or default_privacy_scan
        self._allow_selected_local = allow_selected_local
        self.target_provider = target_provider
        self.target_model = target_model
        self.target_profile = target_profile
        self.supervisor = LoopSupervisor(
            self.store,
            PHONE_FRAGMENT_MINIMUM_VALUE_V1,
            {
                "local_organize": self._local_organize_handler,
                "draft_generation": self._draft_generation_handler,
            },
        )

    # ------------------------------------------------------------------ 登记

    def register_fragment(self, envelope: FragmentEnvelope) -> LoopCheckpoint:
        if envelope.admission_state is not AdmissionState.REQUESTED:
            raise PermissionError("第一步只处理用户主动选择并请求处理的碎片")
        synthetic_source = envelope.source_hint == "synthetic" and envelope.source_path.startswith(
            "synthetic://"
        )
        selected_local_source = (
            self._allow_selected_local
            and envelope.source_hint == "user_selected_local"
            and bool(envelope.source_path)
            and not envelope.source_path.startswith(("http://", "https://"))
        )
        if not synthetic_source and not selected_local_source:
            raise PermissionError("第一步离线候选只接受 synthetic 合成输入")
        if envelope.input_type != "text":
            raise ValueError("第一版只处理 input_type=text 的纯文本碎片")
        if envelope.attachments:
            raise ValueError("第一版不处理附件")
        if not envelope.raw_content.strip():
            raise ValueError("第一版不处理空白碎片")
        title, title_source = fragment_title_for(envelope)
        run_id = _deterministic_run_id(envelope.fragment_id)
        initial_eval_results = {
            "intake": {
                "fragment_text": envelope.raw_content,
                "title_candidate": title,
                "title_candidate_source": title_source,
            },
            "minimum_value": {
                "content_lifecycle": "none",
                "evidence_level": "unverified",
                "source_type": "user_statement",
                "target_provider": self.target_provider,
                "target_model": self.target_model,
                "target_profile": self.target_profile,
            },
        }

        def validate_identity(checkpoint: LoopCheckpoint) -> None:
            if (
                checkpoint.run_id != run_id
                or checkpoint.fragment_id != envelope.fragment_id
                or checkpoint.input_refs != [envelope.source_path]
                or checkpoint.fragment_title != title
                or checkpoint.fragment_title_source != title_source
                or checkpoint.eval_results != initial_eval_results
            ):
                raise ValueError("同一 fragment_id 的内容或来源发生冲突")

        checkpoint = self.store.latest_for_fragment(
            PHONE_FRAGMENT_MINIMUM_VALUE_V1.loop_id, envelope.fragment_id
        )
        if checkpoint is None:
            try:
                checkpoint = self.supervisor.register(
                    envelope.fragment_id,
                    admission_state=AdmissionState.REQUESTED,
                    input_refs=[envelope.source_path],
                    run_id=run_id,
                    fragment_title=title,
                    fragment_title_source=title_source,
                    eval_results=initial_eval_results,
                )
            except ValueError:
                checkpoint = self.store.latest(run_id)
                if checkpoint is None:
                    raise

        for _ in range(3):
            validate_identity(checkpoint)
            if checkpoint.status == AdmissionState.APPROVED.value:
                return checkpoint
            if checkpoint.status == AdmissionState.REQUESTED.value:
                target = AdmissionState.PREFLIGHT_PASSED
            elif checkpoint.status == AdmissionState.PREFLIGHT_PASSED.value:
                target = AdmissionState.APPROVED
            else:
                raise PermissionError("碎片登记已离开准入流程")
            try:
                checkpoint = self.supervisor.transition_admission(run_id, target)
            except ValueError:
                latest = self.store.latest(run_id)
                if latest is None:
                    raise RuntimeError("碎片登记状态丢失")
                checkpoint = latest
        raise RuntimeError("碎片登记未能稳定进入 approved")

    # ------------------------------------------------------- 本地整理与等待

    def run_local_organize(self, run_id: str) -> LoopCheckpoint:
        current = self._require(run_id)
        target_errors = self._target_configuration_errors(current)
        if target_errors:
            raise PermissionError("; ".join(target_errors))
        organize = current.eval_results.get("local_organize_result")
        if (
            current.status == "running"
            and current.current_node == "draft_generation"
            and isinstance(organize, dict)
            and organize.get("payload_sha256")
            and "send_consent" not in current.eval_results
        ):
            return self._park_after_local_organize(run_id)
        checkpoint = self.supervisor.run(run_id, max_steps=1)
        if checkpoint.status != "running":
            return checkpoint
        return self._park_after_local_organize(run_id)

    def _park_after_local_organize(self, run_id: str) -> LoopCheckpoint:
        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            organize = current.eval_results.get("local_organize_result")
            if (
                current.status != "running"
                or current.current_node != "draft_generation"
                or not isinstance(organize, dict)
                or not organize.get("payload_sha256")
                or "send_consent" in current.eval_results
            ):
                raise PermissionError("本地整理恢复期间运行状态已变化")
            blocked = bool(organize.get("privacy_blocked"))
            if not blocked and not isinstance(current.eval_results.get("outbound"), dict):
                raise PermissionError("本地整理结果缺少待发送正文")
            # 控制面把插件产物固化为 system 投影（runtime 写，无冲突）。
            projection_update = {
                "payload_sha256": organize["payload_sha256"],
                "privacy_blocked": blocked,
                "privacy_field_categories": list(organize["privacy_field_categories"]),
                "source_type": str(organize["source_type"]),
                "target_provider": str(organize["target_provider"]),
                "target_model": str(organize["target_model"]),
                "target_profile": str(organize["target_profile"]),
            }
            return self._replace_with_projection(
                current,
                projection_update=projection_update,
                status="paused",
                stop_reason=STOP_PRIVACY_BLOCKED if blocked else STOP_AWAITING_SEND,
                resume_condition=(
                    "隐私命中：用户本地编辑或确认替换后的发送正文"
                    if blocked
                    else "等待用户查看发送正文并明确同意"
                ),
            )

        return self._cas(
            run_id,
            mutate,
            event_type="state_transition",
            revision_reason="local_organize_parked",
        )

    def _local_organize_handler(self, context: SupervisorContext) -> LoopResult:
        checkpoint = context.checkpoint
        fragment_text = str(checkpoint.eval_results["intake"]["fragment_text"])
        payload = build_outbound_payload(fragment_text)
        categories = self._privacy_scan(fragment_text)
        blocked = bool(categories)
        # Gate 1：插件不写 system 拥有的 “minimum_value” 投影键；投影更新
        # 由控制面在 park 步骤应用。插件只写自己命名空间的产物。
        organize_result = {
            "payload_sha256": payload_sha256(payload),
            "privacy_blocked": blocked,
            "privacy_field_categories": list(categories),
            "source_type": "user_statement",
            "target_provider": self.target_provider,
            "target_model": self.target_model,
            "target_profile": self.target_profile,
        }
        plugin_content: dict[str, Any] = {
            "local_organize_result": organize_result,
            "local_candidate": build_local_candidate(fragment_text),
        }
        if not blocked:
            plugin_content["outbound"] = {
                "payload_text": payload,
                "fragment_text": fragment_text,
            }
        return LoopResult(
            status="continue",
            next_node="draft_generation",
            eval_results=plugin_eval("local_organize", plugin_content),
        )

    # ------------------------------------------------------------- 预览

    def preview(self, run_id: str) -> SendPreview:
        checkpoint = self._require(run_id)
        projection = self._projection(checkpoint)
        target_provider, target_model, target_profile = self._persisted_target(checkpoint)
        blocked = bool(projection.get("privacy_blocked"))
        outbound = checkpoint.eval_results.get("outbound", {})
        payload_text = None if blocked else outbound.get("payload_text")
        return SendPreview(
            run_id=run_id,
            status=checkpoint.status,
            stop_reason=checkpoint.stop_reason,
            outbound_payload=payload_text if isinstance(payload_text, str) else None,
            outbound_payload_sha256=str(projection.get("payload_sha256", "")),
            target_provider=target_provider,
            target_model=target_model,
            target_profile=target_profile,
            privacy_blocked=blocked,
            privacy_field_categories=tuple(
                str(category) for category in projection.get("privacy_field_categories", [])
            ),
        )

    # ------------------------------------------------------------- 同意

    def consent(
        self,
        run_id: str,
        consent_id: str | None = None,
        *,
        expected_sequence: int | None = None,
        expected_binding: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if (expected_sequence is None) != (expected_binding is None):
            raise ValueError("expected_sequence 与 expected_binding 必须同时提供")
        for _ in range(_MAX_CAS_ATTEMPTS):
            raw_checkpoint, sequence = self._require_raw_seq(run_id)
            checkpoint = checkpoint_compat_view(raw_checkpoint)
            if checkpoint.status != "paused" or checkpoint.stop_reason != STOP_AWAITING_SEND:
                raise PermissionError("运行不在等待发送同意状态，不能记录同意")
            target_provider, target_model, target_profile = self._persisted_target(checkpoint)
            target_errors = self._target_configuration_errors(checkpoint)
            if target_errors:
                raise PermissionError("; ".join(target_errors))
            existing = checkpoint.eval_results.get("send_consent")
            if (
                isinstance(existing, dict)
                and existing.get("consumed") is False
                and not self._consent_binding_errors(checkpoint, existing)
            ):
                if expected_binding is not None and existing.get("consent_id") != consent_id:
                    raise PermissionError("发送决定已由另一条同意记录占用")
                return dict(existing)
            if expected_sequence is not None and sequence != expected_sequence:
                raise PermissionError("Checkpoint 序号已变化，必须刷新后重新决定")
            if expected_binding is not None:
                self._assert_display_binding(checkpoint, expected_binding)
            sha = str(self._projection(checkpoint)["payload_sha256"])
            record: dict[str, Any] = {
                "fragment_id": checkpoint.fragment_id,
                "run_id": run_id,
                "outbound_payload_sha256": sha,
                "target_provider": target_provider,
                "target_model": target_model,
                "target_profile": target_profile,
                "consent_id": consent_id or uuid.uuid4().hex,
                "consented_at": utc_now(),
                "consumed": False,
            }
            committed = self.store.compare_and_append(
                self._replace(
                    checkpoint,
                    eval_results=translate_view_edit(
                        existing=raw_checkpoint.eval_results,
                        edited_flat={**checkpoint.eval_results, "send_consent": record},
                    ),
                ),
                expected_sequence=sequence,
                event_type="snapshot",
                revision_reason="send_consent_recorded",
            )
            if committed is not None:
                return record
            if expected_sequence is not None:
                raise PermissionError("Checkpoint 序号已变化，必须刷新后重新决定")
        raise RuntimeError("Repeated checkpoint conflicts while recording consent")

    def decline_send(
        self,
        run_id: str,
        *,
        decision_id: str | None = None,
        expected_sequence: int | None = None,
        expected_binding: Mapping[str, str] | None = None,
    ) -> LoopCheckpoint:
        if (expected_sequence is None) != (expected_binding is None):
            raise ValueError("expected_sequence 与 expected_binding 必须同时提供")
        declined_at = utc_now()
        for _ in range(_MAX_CAS_ATTEMPTS):
            raw_current, sequence = self._require_raw_seq(run_id)
            current = checkpoint_compat_view(raw_current)
            projection = self._projection(current)
            if (
                current.status == "cancelled"
                and current.stop_reason == "user_declined_send"
                and projection.get("content_lifecycle") == "rejected"
            ):
                existing = current.eval_results.get("send_decision")
                if (
                    decision_id is not None
                    and isinstance(existing, dict)
                    and existing.get("decision_id") != decision_id
                ):
                    raise PermissionError("发送决定已经完成")
                return current
            if current.status != "paused" or current.stop_reason != STOP_AWAITING_SEND:
                raise PermissionError("只有等待发送同意时才能拒绝发送")
            if expected_sequence is not None and sequence != expected_sequence:
                raise PermissionError("Checkpoint 序号已变化，必须刷新后重新决定")
            if expected_binding is not None:
                self._assert_display_binding(current, expected_binding)
            eval_results = {
                key: value for key, value in current.eval_results.items() if key != "send_consent"
            }
            eval_results["send_decision"] = {
                "decision": "declined",
                "decision_id": decision_id or uuid.uuid4().hex,
                "decided_at": declined_at,
            }
            eval_results["minimum_value"] = {
                **self._projection(current),
                "content_lifecycle": "rejected",
            }
            candidate = self._replace(
                current,
                status="cancelled",
                stop_reason="user_declined_send",
                resume_condition="用户拒绝发送，历史保留",
                eval_results=translate_view_edit(
                    existing=raw_current.eval_results,
                    edited_flat=eval_results,
                ),
            )
            committed = self.store.compare_and_append(
                candidate,
                expected_sequence=sequence,
                event_type="loop_stopped",
                revision_reason="user_declined_send",
            )
            if committed is not None:
                return committed
            if expected_sequence is not None:
                raise PermissionError("Checkpoint 序号已变化，必须刷新后重新决定")
        raise RuntimeError("Repeated checkpoint conflicts while declining send")

    # ---------------------------------------------------- 隐私阻断与替换

    def submit_replacement_text(self, run_id: str, new_text: str) -> LoopCheckpoint:
        checkpoint = self._require(run_id)
        if checkpoint.status != "paused" or checkpoint.stop_reason != STOP_PRIVACY_BLOCKED:
            raise PermissionError("只有隐私阻断状态才能提交替换正文")
        payload = build_outbound_payload(new_text)
        categories = self._privacy_scan(new_text)
        blocked = bool(categories)

        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            if current.status != "paused" or current.stop_reason != STOP_PRIVACY_BLOCKED:
                raise PermissionError("隐私替换期间运行状态已变化")
            projection = {
                **self._projection(current),
                "payload_sha256": payload_sha256(payload),
                "privacy_blocked": blocked,
                "privacy_field_categories": list(categories),
            }
            eval_results = {
                key: value
                for key, value in current.eval_results.items()
                if key not in {"outbound", "send_consent"}
            }
            eval_results["minimum_value"] = projection
            eval_results["local_candidate"] = build_local_candidate(new_text)
            if not blocked:
                eval_results["outbound"] = {
                    "payload_text": payload,
                    "fragment_text": new_text,
                }
            return self._replace(
                current,
                status="paused",
                stop_reason=STOP_PRIVACY_BLOCKED if blocked else STOP_AWAITING_SEND,
                eval_results=eval_results,
            )

        return self._cas(
            run_id,
            mutate,
            event_type="state_transition",
            revision_reason="privacy_replacement_submitted",
        )

    # ------------------------------------------------------- 草稿生成

    def generate_draft(self, run_id: str) -> LoopCheckpoint:
        checkpoint = self._require(run_id)
        if checkpoint.status == "running":
            attempts = checkpoint.eval_results.get("model_call_attempts", [])
            draft_ref = checkpoint.eval_results.get("draft_ref")
            if (
                any(attempt.get("request_sent") is True for attempt in attempts)
                and isinstance(draft_ref, dict)
            ):
                self._validated_draft_doc(checkpoint)
                return self._park_completed_draft(run_id)
        if checkpoint.status in {"approved", "running"}:
            consent = checkpoint.eval_results.get("send_consent")
            if isinstance(consent, dict) and consent.get("consumed") is True:
                return self._fail_interrupted_generation(run_id)

        for _ in range(_MAX_CAS_ATTEMPTS):
            raw_checkpoint, sequence = self._require_raw_seq(run_id)
            checkpoint = checkpoint_compat_view(raw_checkpoint)
            reasons = self._send_block_reasons(checkpoint)
            if reasons:
                raise PermissionError("; ".join(reasons))
            consent = checkpoint.eval_results["send_consent"]
            consumed = {**consent, "consumed": True}
            committed = self.store.compare_and_append(
                self._replace(
                    checkpoint,
                    status="approved",
                    stop_reason=None,
                    eval_results=translate_view_edit(
                        existing=raw_checkpoint.eval_results,
                        edited_flat={**checkpoint.eval_results, "send_consent": consumed},
                    ),
                ),
                expected_sequence=sequence,
                event_type="state_transition",
                revision_reason="consent_consumed",
            )
            if committed is not None:
                break
        else:
            raise RuntimeError("Repeated checkpoint conflicts while consuming consent")
        checkpoint = self.supervisor.run(run_id, max_steps=1)
        if checkpoint.status != "running":
            return checkpoint

        def _park_draft(current: LoopCheckpoint) -> LoopCheckpoint:
            # 控制面把草稿插件产物固化为 system 投影（runtime 写，无冲突）。
            draft_projection = current.eval_results.get("draft_projection")
            update: dict[str, Any] = {"content_lifecycle": "draft"}
            if isinstance(draft_projection, dict):
                update.update(draft_projection)
            return self._replace_with_projection(
                current,
                status="paused",
                stop_reason=STOP_AWAITING_DRAFT,
                resume_condition="等待用户对草稿作出确认发布／编辑发布／保留／拒绝决策",
                projection_update=update,
            )

        return self._cas(
            run_id,
            _park_draft,
            event_type="state_transition",
            revision_reason=STOP_AWAITING_DRAFT,
        )

    def _park_completed_draft(self, run_id: str) -> LoopCheckpoint:
        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            attempts = current.eval_results.get("model_call_attempts", [])
            draft_ref = current.eval_results.get("draft_ref")
            if (
                current.status != "running"
                or not any(attempt.get("request_sent") is True for attempt in attempts)
                or not isinstance(draft_ref, dict)
            ):
                raise PermissionError("草稿完成恢复期间运行状态已变化")
            self._validated_draft_doc(current)
            return self._replace_with_projection(
                current,
                status="paused",
                stop_reason=STOP_AWAITING_DRAFT,
                resume_condition="等待用户对草稿作出确认发布／编辑发布／保留／拒绝决策",
                projection_update={"content_lifecycle": "draft"},
            )

        return self._cas(
            run_id,
            mutate,
            event_type="state_transition",
            revision_reason="completed_draft_recovered",
        )

    def _fail_interrupted_generation(self, run_id: str) -> LoopCheckpoint:
        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            consent = current.eval_results.get("send_consent")
            if (
                current.status not in {"approved", "running"}
                or not isinstance(consent, dict)
                or consent.get("consumed") is not True
            ):
                raise PermissionError("草稿中断恢复期间运行状态已变化")
            attempts = [
                dict(item)
                for item in current.eval_results.get("model_call_attempts", [])
            ]
            sha = str(self._projection(current)["payload_sha256"])
            if any(
                item.get("payload_sha256") == sha
                and item.get("request_sent") in (True, "unknown")
                for item in attempts
            ):
                raise PermissionError("已有发送终态，不能改写为中断状态")
            attempts.append({"payload_sha256": sha, "request_sent": "unknown"})
            return self._replace(
                current,
                status="failed_safe",
                stop_reason="send_outcome_unknown",
                resume_condition="发送状态未知：该组合唯一调用名额已永久消耗，不得重发",
                eval_results={
                    **current.eval_results,
                    "model_call_attempts": attempts,
                },
            )

        return self._cas(
            run_id,
            mutate,
            event_type="loop_stopped",
            revision_reason="interrupted_generation_unknown",
        )

    def _draft_generation_handler(self, context: SupervisorContext) -> LoopResult:
        checkpoint = context.checkpoint
        projection = self._projection(checkpoint)
        sha = str(projection["payload_sha256"])
        attempts = [
            dict(item) for item in checkpoint.eval_results.get("model_call_attempts", [])
        ]
        consent = checkpoint.eval_results.get("send_consent")
        if (
            not isinstance(consent, dict)
            or consent.get("consumed") is not True
            or self._consent_binding_errors(checkpoint, consent)
        ):
            return LoopResult(
                status="failed_safe",
                stop_reason="consent_invalid",
                eval_results=plugin_eval("draft_generation", {"model_call_attempts": attempts}),
                resume_condition="同意记录无效：不得调用，由用户处理",
            )
        outbound = checkpoint.eval_results.get("outbound")
        payload_text = outbound.get("payload_text") if isinstance(outbound, dict) else None
        fragment_text = outbound.get("fragment_text") if isinstance(outbound, dict) else None
        valid_outbound = (
            isinstance(payload_text, str)
            and isinstance(fragment_text, str)
            and bool(fragment_text.strip())
            and payload_text == build_outbound_payload(fragment_text)
            and payload_sha256(payload_text) == sha
            and not self._privacy_scan(fragment_text)
        )
        if not valid_outbound:
            attempts.append({"payload_sha256": sha, "request_sent": False})
            return LoopResult(
                status="failed_safe",
                stop_reason="outbound_validation_failed",
                eval_results=plugin_eval("draft_generation", {"model_call_attempts": attempts}),
                resume_condition="实际发送正文绑定或隐私校验失败：不得调用，由用户处理",
            )
        assert isinstance(payload_text, str)
        assert isinstance(fragment_text, str)
        try:
            adapter_result = self._adapter(payload_text)
        except PreSendFailureError:
            attempts.append({"payload_sha256": sha, "request_sent": False})
            return LoopResult(
                status="failed_safe",
                stop_reason="pre_send_failure",
                eval_results=plugin_eval("draft_generation", {"model_call_attempts": attempts}),
                resume_condition="确认的发送前失败：修正问题后重新展示正文并取得新同意",
            )
        except Exception as error:  # 发送状态未知：保守处理
            attempts.append({"payload_sha256": sha, "request_sent": "unknown"})
            return LoopResult(
                status="failed_safe",
                stop_reason="send_outcome_unknown",
                eval_results=plugin_eval("draft_generation", {"model_call_attempts": attempts}),
                unresolved_issues=[f"adapter_exception:{type(error).__name__}"],
                resume_condition="发送状态未知：该组合唯一调用名额已永久消耗，不得重发",
            )
        raw: Mapping[str, Any]
        call_evidence: dict[str, Any] = {}
        evidence_errors: list[str] = []
        if isinstance(adapter_result, DraftAdapterResult):
            raw = adapter_result.draft
            call_evidence = adapter_result.evidence.to_dict()
            evidence_errors = self._model_call_evidence_errors(adapter_result.evidence)
        else:
            raw = adapter_result
        attempts.append(
            {"payload_sha256": sha, "request_sent": True, **call_evidence}
        )
        if evidence_errors:
            return LoopResult(
                status="failed_safe",
                stop_reason="model_call_evidence_invalid",
                eval_results=plugin_eval("draft_generation", {"model_call_attempts": attempts}),
                unresolved_issues=evidence_errors,
                resume_condition="模型调用证据与冻结目标不一致：调用名额已消耗，由用户处理",
            )
        errors = validate_model_draft(raw, fragment_text)
        if errors:
            return LoopResult(
                status="failed_safe",
                stop_reason="draft_validation_failed",
                eval_results=plugin_eval("draft_generation", {"model_call_attempts": attempts}),
                unresolved_issues=errors,
                resume_condition="草稿校验失败：该组合调用名额已消耗，由用户处理",
            )
        content = normalize_draft_content(raw)
        draft_path = self._draft_path(checkpoint.run_id)
        draft_doc = {
                "kind": "draft",
                "run_id": checkpoint.run_id,
                "fragment_id": checkpoint.fragment_id,
                "lifecycle": "draft",
                "evidence_level": "unverified",
                "created_at": utc_now(),
                "content_sha256": content_sha256(content),
                "content": content,
            }
        try:
            _create_doc(draft_path, draft_doc, _render_draft_body(content))
        except FileExistsError:
            return LoopResult(
                status="failed_safe",
                stop_reason="draft_conflict",
                eval_results=plugin_eval("draft_generation", {"model_call_attempts": attempts}),
                resume_condition="固定草稿路径已存在：不覆盖，由用户处理",
            )
        conflict_warning = any(
            item["marker"] == "conflicted_with_source" for item in content["key_information"]
        )
        return LoopResult(
            status="continue",
            next_node="draft_generation",
            output_refs=[str(draft_path)],
            eval_results=plugin_eval(
                "draft_generation",
                {
                    "model_call_attempts": attempts,
                    "draft_projection": {
                        "content_lifecycle": "draft",
                        "conflict_warning": conflict_warning,
                    },
                    "draft_ref": {
                        "path": str(draft_path),
                        "content_sha256": content_sha256(content),
                    },
                },
            ),
        )

    def represent_after_pre_send_failure(self, run_id: str) -> LoopCheckpoint:
        def pre_send_state(current: LoopCheckpoint) -> tuple[str, list[dict[str, Any]]]:
            sha = str(self._projection(current)["payload_sha256"])
            attempts = [
                dict(attempt)
                for attempt in current.eval_results.get("model_call_attempts", [])
            ]
            if any(
                attempt.get("payload_sha256") == sha
                and attempt.get("request_sent") is not False
                for attempt in attempts
            ):
                raise PermissionError("该组合调用名额已永久消耗，不得重新展示")
            ordinary = (
                current.status == "failed_safe"
                and current.stop_reason == "pre_send_failure"
            )
            consent = current.eval_results.get("send_consent")
            legacy_wall_clock = (
                current.status == "exhausted"
                and current.stop_reason == "time_budget_exhausted"
                and isinstance(consent, dict)
                and consent.get("consumed") is True
            )
            if not ordinary and not legacy_wall_clock:
                raise PermissionError("只有确认的发送前失败才能重新展示")
            if legacy_wall_clock:
                attempts.append({"payload_sha256": sha, "request_sent": False})
            return sha, attempts

        checkpoint = self._require(run_id)
        pre_send_state(checkpoint)

        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            try:
                _, attempts = pre_send_state(current)
            except PermissionError as error:
                raise PermissionError("重新展示期间运行状态已变化") from error
            eval_results = {
                key: value
                for key, value in current.eval_results.items()
                if key != "send_consent"
            }
            eval_results["model_call_attempts"] = attempts
            return self._replace(
                current,
                status="paused",
                stop_reason=STOP_AWAITING_SEND,
                resume_condition="重新展示发送正文，等待新的明确同意",
                eval_results=eval_results,
            )

        return self._cas(
            run_id,
            mutate,
            event_type="state_transition",
            revision_reason="represent_after_pre_send_failure",
        )

    # ------------------------------------------------------- 用户决策

    def publish(self, run_id: str, edits: Mapping[str, Any] | None = None) -> PublishOutcome:
        checkpoint = self._require(run_id)
        projection = self._projection(checkpoint)
        result_id = f"mv-result-{run_id}"
        result_path = self.results_dir / f"{result_id}.md"
        if checkpoint.status == "passed" and projection.get("content_lifecycle") == "published":
            if result_path.exists():
                try:
                    existing = self._validated_result_for_withdraw(
                        checkpoint, result_id, result_path
                    )
                except PublishConflictError:
                    pass
                else:
                    if existing.get("lifecycle") == "withdrawn":
                        raise PermissionError("结果已撤回，不能在原任务中重复发布")
            return self._replay_publish(checkpoint, result_id, result_path, edits)
        if checkpoint.status != "paused" or checkpoint.stop_reason != STOP_AWAITING_DRAFT:
            raise PermissionError("草稿不得绕过用户决策进入发布")
        content, fragment_text = self._load_draft_content(run_id)
        final, edited_fields = self._apply_edits(content, edits)
        errors = validate_publish_content(final, fragment_text)
        if errors:
            raise ValueError("; ".join(errors))
        sha = content_sha256(final)
        decision = "edited_publish" if edited_fields else "publish"

        def commit_publish(current: LoopCheckpoint) -> LoopCheckpoint:
            if current.status != "paused" or current.stop_reason != STOP_AWAITING_DRAFT:
                raise PermissionError("发布期间运行状态已变化")
            return self._replace_with_projection(
                current,
                status="passed",
                stop_reason="published_with_edits" if edited_fields else "published",
                resume_condition="结果已发布，可撤回",
                eval_results_update={
                    "result_ref": {
                        "result_id": result_id,
                        "path": str(result_path),
                        "content_sha256": sha,
                        "decision": decision,
                        "edited_fields": list(edited_fields),
                    }
                },
                projection_update={"content_lifecycle": "published"},
            )

        if result_path.exists():
            try:
                self._validated_result_doc(
                    checkpoint,
                    result_id,
                    result_path,
                    final,
                    sha,
                    decision,
                    edited_fields,
                )
            except PublishConflictError:
                self._mark_publish_conflict(run_id)
                raise
            self._cas(
                run_id,
                commit_publish,
                event_type="loop_stopped",
                revision_reason="published_recovered",
            )
            return PublishOutcome(
                result_id=result_id,
                path=str(result_path),
                content_sha256=sha,
                decision=decision,
                edited_fields=edited_fields,
                already_published=True,
            )

        result_doc = {
                "kind": "result",
                "result_id": result_id,
                "run_id": run_id,
                "fragment_id": checkpoint.fragment_id,
                "doc_type": RESULT_DOC_TYPE,
                "decision": decision,
                "edited_fields": list(edited_fields),
                "lifecycle": "published",
                "evidence_level": "unverified",
                "published_at": utc_now(),
                "withdrawn_at": None,
                "content_sha256": sha,
                "content": final,
            }
        try:
            _create_doc(
                result_path,
                result_doc,
                _render_result_body(final, RESULT_DOC_TYPE),
            )
        except FileExistsError:
            return self.publish(run_id, edits)
        try:
            self._cas(
                run_id,
                commit_publish,
                event_type="loop_stopped",
                revision_reason="published",
            )
        except Exception:
            latest = self._require(run_id)
            latest_projection = self._projection(latest)
            latest_ref = latest.eval_results.get("result_ref")
            published = (
                latest.status == "passed"
                and latest_projection.get("content_lifecycle") == "published"
                and isinstance(latest_ref, dict)
                and latest_ref.get("content_sha256") == sha
            )
            if not published:
                try:
                    self._validated_result_doc(
                        checkpoint,
                        result_id,
                        result_path,
                        final,
                        sha,
                        decision,
                        edited_fields,
                    )
                except PublishConflictError:
                    pass
                else:
                    result_path.unlink(missing_ok=True)
            raise
        return PublishOutcome(
            result_id=result_id,
            path=str(result_path),
            content_sha256=sha,
            decision=decision,
            edited_fields=edited_fields,
            already_published=False,
        )

    def keep_draft(self, run_id: str) -> LoopCheckpoint:
        checkpoint = self._require(run_id)
        if checkpoint.status != "paused" or checkpoint.stop_reason != STOP_AWAITING_DRAFT:
            raise PermissionError("运行不在等待草稿确认状态")
        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            if current.status != "paused" or current.stop_reason != STOP_AWAITING_DRAFT:
                raise PermissionError("保留草稿期间运行状态已变化")
            self._validated_draft_doc(current)
            return self._replace_with_projection(
                current,
                status="passed",
                stop_reason="draft_kept",
                resume_condition="草稿已保留，未发布",
            )

        return self._cas(
            run_id,
            mutate,
            event_type="loop_stopped",
            revision_reason="draft_kept",
        )

    def reopen_kept_draft(self, run_id: str) -> LoopCheckpoint:
        checkpoint = self._require(run_id)
        projection = self._projection(checkpoint)
        if (
            checkpoint.status != "passed"
            or checkpoint.stop_reason != "draft_kept"
            or projection.get("content_lifecycle") != "draft"
        ):
            raise PermissionError("只有已保留的草稿才能重新打开")
        self._validated_draft_doc(checkpoint)

        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            current_projection = self._projection(current)
            if (
                current.status != "passed"
                or current.stop_reason != "draft_kept"
                or current_projection.get("content_lifecycle") != "draft"
            ):
                raise PermissionError("草稿状态已变化，不能重新打开")
            self._validated_draft_doc(current)
            return self._replace_with_projection(
                current,
                status="paused",
                stop_reason=STOP_AWAITING_DRAFT,
                resume_condition="等待用户对草稿作出确认发布／编辑发布／保留／拒绝决策",
            )

        return self._cas(
            run_id,
            mutate,
            event_type="state_transition",
            revision_reason="draft_reopened",
        )

    def reject(self, run_id: str) -> LoopCheckpoint:
        checkpoint = self._require(run_id)
        projection = self._projection(checkpoint)
        if (
            checkpoint.status == "cancelled"
            and checkpoint.stop_reason == "user_rejected"
            and projection.get("content_lifecycle") == "rejected"
        ):
            self._reconcile_rejected_draft(checkpoint)
            return checkpoint
        if checkpoint.status != "paused" or checkpoint.stop_reason != STOP_AWAITING_DRAFT:
            raise PermissionError("运行不在等待草稿确认状态")

        draft_doc = self._validated_draft_doc(checkpoint)
        rejected_content_sha256 = content_sha256(draft_doc["content"])
        rejected_at = utc_now()

        def mutate(current: LoopCheckpoint) -> LoopCheckpoint:
            if current.status != "paused" or current.stop_reason != STOP_AWAITING_DRAFT:
                raise PermissionError("拒绝草稿期间运行状态已变化")
            return self._replace_with_projection(
                current,
                status="cancelled",
                stop_reason="user_rejected",
                resume_condition="用户已拒绝，历史保留",
                projection_update={"content_lifecycle": "rejected"},
                eval_results_update={
                    "rejection_ref": {
                        "path": str(self._draft_path(run_id)),
                        "rejected_at": rejected_at,
                        "content_sha256": rejected_content_sha256,
                    }
                },
            )

        committed = self._cas(
            run_id,
            mutate,
            event_type="loop_stopped",
            revision_reason="user_rejected",
        )
        self._reconcile_rejected_draft(committed)
        return committed

    def withdraw(self, run_id: str) -> WithdrawOutcome:
        checkpoint = self._require(run_id)
        result_id = f"mv-result-{run_id}"
        result_path = self.results_dir / f"{result_id}.md"
        if not result_path.exists():
            raise PermissionError("没有已发布的结果文档，无法撤回")
        doc = self._validated_result_for_withdraw(checkpoint, result_id, result_path)
        if doc.get("lifecycle") == "withdrawn":
            return WithdrawOutcome(
                result_id=result_id,
                path=str(result_path),
                lifecycle="withdrawn",
                withdrawn_at=str(doc["withdrawn_at"]),
                already_withdrawn=True,
            )
        projection = self._projection(checkpoint)
        if (
            checkpoint.status != "passed"
            or projection.get("content_lifecycle") != "published"
            or doc.get("lifecycle") != "published"
        ):
            raise PermissionError("只有已发布的结果才能撤回")
        content = dict(doc["content"])
        updated = dict(doc)
        updated["lifecycle"] = "withdrawn"
        updated["withdrawn_at"] = utc_now()
        _write_doc(
            result_path,
            updated,
            _render_result_body(content, RESULT_DOC_TYPE, lifecycle="withdrawn"),
        )
        return WithdrawOutcome(
            result_id=result_id,
            path=str(result_path),
            lifecycle="withdrawn",
            withdrawn_at=str(updated["withdrawn_at"]),
            already_withdrawn=False,
        )

    # ---------------------------------------------------------- 内部工具

    def _replay_publish(
        self,
        checkpoint: LoopCheckpoint,
        result_id: str,
        result_path: Path,
        edits: Mapping[str, Any] | None,
    ) -> PublishOutcome:
        content, fragment_text = self._load_draft_content(checkpoint.run_id)
        final, edited_fields = self._apply_edits(content, edits)
        errors = validate_publish_content(final, fragment_text)
        if errors:
            raise ValueError("; ".join(errors))
        sha = content_sha256(final)
        try:
            self._validated_result_doc(
                checkpoint,
                result_id,
                result_path,
                final,
                sha,
                "edited_publish" if edited_fields else "publish",
                edited_fields,
            )
        except PublishConflictError:
            self._mark_publish_conflict(checkpoint.run_id)
            raise
        ref = checkpoint.eval_results.get("result_ref", {})
        decision = str(ref.get("decision", "publish"))
        recorded_fields = tuple(str(field) for field in ref.get("edited_fields", []))
        return PublishOutcome(
            result_id=result_id,
            path=str(result_path),
            content_sha256=sha,
            decision=decision,
            edited_fields=edited_fields or recorded_fields,
            already_published=True,
        )

    def _mark_publish_conflict(self, run_id: str) -> None:
        def mark_conflict(current: LoopCheckpoint) -> LoopCheckpoint:
            projection = self._projection(current)
            allowed = (
                current.status == "paused" and current.stop_reason == STOP_AWAITING_DRAFT
            ) or (
                current.status == "passed"
                and projection.get("content_lifecycle") == "published"
            )
            if not allowed:
                raise PermissionError("发布冲突处理期间运行状态已变化")
            return self._replace_with_projection(
                current,
                status="failed_safe",
                stop_reason="publish_conflict",
                resume_condition="固定路径内容冲突：不覆盖，等待用户处理",
            )

        self._cas(
            run_id,
            mark_conflict,
            event_type="loop_stopped",
            revision_reason="publish_conflict",
        )

    def _validated_result_doc(
        self,
        checkpoint: LoopCheckpoint,
        result_id: str,
        result_path: Path,
        content: Mapping[str, Any],
        sha: str,
        decision: str,
        edited_fields: tuple[str, ...],
    ) -> dict[str, Any]:
        if not result_path.exists():
            raise PublishConflictError(f"结果文档缺失，不得重写: {result_path}")
        try:
            doc = _read_doc(result_path)
        except (ValueError, json.JSONDecodeError) as error:
            raise PublishConflictError(f"结果文档无法解析，不得覆盖: {result_path}") from error
        published_at = doc.get("published_at")
        expected = {
            "kind": "result",
            "result_id": result_id,
            "run_id": checkpoint.run_id,
            "fragment_id": checkpoint.fragment_id,
            "doc_type": RESULT_DOC_TYPE,
            "decision": decision,
            "edited_fields": list(edited_fields),
            "lifecycle": "published",
            "evidence_level": "unverified",
            "withdrawn_at": None,
            "content_sha256": sha,
            "content": dict(content),
        }
        if not isinstance(published_at, str) or not published_at:
            raise PublishConflictError(f"结果文档缺少发布时间，不得覆盖: {result_path}")
        if set(doc) != {*expected, "published_at"} or any(
            doc.get(field) != value for field, value in expected.items()
        ):
            raise PublishConflictError(f"结果文档绑定或内容冲突，不得覆盖: {result_path}")
        expected_text = _doc_text(doc, _render_result_body(content, RESULT_DOC_TYPE))
        if result_path.read_text(encoding="utf-8") != expected_text:
            raise PublishConflictError(f"结果文档正文冲突，不得覆盖: {result_path}")
        return doc

    def _validated_result_for_withdraw(
        self, checkpoint: LoopCheckpoint, result_id: str, result_path: Path
    ) -> dict[str, Any]:
        try:
            doc = _read_doc(result_path)
        except (ValueError, json.JSONDecodeError) as error:
            raise PublishConflictError("结果文档无法解析，不得撤回或覆盖") from error
        result_ref = checkpoint.eval_results.get("result_ref")
        content = doc.get("content")
        if not isinstance(result_ref, dict) or not isinstance(content, dict):
            raise PublishConflictError("结果引用或内容无效，不得撤回或覆盖")
        lifecycle = doc.get("lifecycle")
        withdrawn_at = doc.get("withdrawn_at")
        if lifecycle == "published":
            valid_withdrawn_at = withdrawn_at is None
        elif lifecycle == "withdrawn":
            valid_withdrawn_at = isinstance(withdrawn_at, str) and bool(withdrawn_at)
        else:
            valid_withdrawn_at = False
        expected = {
            "kind": "result",
            "result_id": result_id,
            "run_id": checkpoint.run_id,
            "fragment_id": checkpoint.fragment_id,
            "doc_type": RESULT_DOC_TYPE,
            "decision": result_ref.get("decision"),
            "edited_fields": result_ref.get("edited_fields"),
            "evidence_level": "unverified",
            "content_sha256": result_ref.get("content_sha256"),
        }
        if (
            result_ref.get("result_id") != result_id
            or result_ref.get("path") != str(result_path)
            or not isinstance(doc.get("published_at"), str)
            or not doc.get("published_at")
            or not valid_withdrawn_at
            or any(doc.get(field) != value for field, value in expected.items())
            or doc.get("content_sha256") != content_sha256(content)
        ):
            raise PublishConflictError("结果文档绑定或内容冲突，不得撤回或覆盖")
        expected_keys = {
            *expected,
            "lifecycle",
            "published_at",
            "withdrawn_at",
            "content",
        }
        if set(doc) != expected_keys:
            raise PublishConflictError("结果文档字段集合冲突，不得撤回或覆盖")
        expected_text = _doc_text(
            doc, _render_result_body(content, RESULT_DOC_TYPE, lifecycle=str(lifecycle))
        )
        if result_path.read_text(encoding="utf-8") != expected_text:
            raise PublishConflictError("结果文档正文冲突，不得撤回或覆盖")
        return doc

    def _apply_edits(
        self, content: Mapping[str, Any], edits: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        final = dict(content)
        if not edits:
            return final, ()
        unknown = set(edits) - EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"不允许编辑的字段: {sorted(unknown)}")
        edited_fields = tuple(
            sorted(key for key, value in edits.items() if value != content.get(key))
        )
        for key in edited_fields:
            final[key] = edits[key]
        if "summary" in edited_fields:
            final["summary_evidence"] = {
                "marker": "unverified_inference",
                "source_quote": None,
                "transformation_note": None,
                "note": "用户编辑后的摘要，尚未独立验证",
            }
        return final, edited_fields

    def _load_draft_content(self, run_id: str) -> tuple[dict[str, Any], str]:
        checkpoint = self._require(run_id)
        doc = self._validated_draft_doc(checkpoint)
        content = doc.get("content")
        if not isinstance(content, dict):
            raise ValueError("草稿文档缺少 content")
        fragment_text = str(checkpoint.eval_results.get("outbound", {}).get("fragment_text", ""))
        return dict(content), fragment_text

    def _validated_draft_doc(self, checkpoint: LoopCheckpoint) -> dict[str, Any]:
        draft_path = self._draft_path(checkpoint.run_id)
        if not draft_path.exists():
            raise DraftConflictError("草稿文档不存在，不得改变用户决策状态")
        try:
            doc = _read_doc(draft_path)
        except (ValueError, json.JSONDecodeError) as error:
            raise DraftConflictError("草稿文档无法解析，不得覆盖") from error
        content = doc.get("content")
        expected = {
            "kind": "draft",
            "run_id": checkpoint.run_id,
            "fragment_id": checkpoint.fragment_id,
        }
        if any(doc.get(field) != value for field, value in expected.items()):
            raise DraftConflictError("草稿文档绑定字段冲突，不得覆盖")
        if not isinstance(content, dict) or doc.get("content_sha256") != content_sha256(content):
            raise DraftConflictError("草稿文档内容摘要冲突，不得覆盖")
        draft_ref = checkpoint.eval_results.get("draft_ref")
        if not isinstance(draft_ref, dict) or draft_ref.get("path") != str(draft_path):
            raise DraftConflictError("草稿引用绑定冲突，不得覆盖")
        lifecycle = doc.get("lifecycle")
        if lifecycle not in {"draft", "rejected"}:
            raise DraftConflictError("草稿生命周期冲突，不得覆盖")
        if doc.get("evidence_level") != "unverified":
            raise DraftConflictError("草稿证据等级冲突，不得覆盖")
        if draft_ref.get("content_sha256") != doc.get("content_sha256"):
            raise DraftConflictError("草稿内容已偏离原始引用，不得覆盖")
        expected_text = _doc_text(doc, _render_draft_body(content, str(lifecycle)))
        if draft_path.read_text(encoding="utf-8") != expected_text:
            raise DraftConflictError("草稿可见正文与固定元数据不一致，不得覆盖")
        return doc

    def _reconcile_rejected_draft(self, checkpoint: LoopCheckpoint) -> None:
        doc = self._validated_draft_doc(checkpoint)
        rejection_ref = checkpoint.eval_results.get("rejection_ref")
        if not isinstance(rejection_ref, dict):
            raise DraftConflictError("拒绝记录缺少固定时间，不得覆盖草稿")
        rejected_at = rejection_ref.get("rejected_at")
        rejected_sha = rejection_ref.get("content_sha256")
        if not isinstance(rejected_at, str) or not rejected_at:
            raise DraftConflictError("拒绝记录时间无效，不得覆盖草稿")
        if not isinstance(rejected_sha, str) or not rejected_sha:
            raise DraftConflictError("拒绝记录内容摘要无效，不得覆盖草稿")
        content = dict(doc["content"])
        if doc.get("lifecycle") == "rejected":
            if doc.get("rejected_at") != rejected_at or doc.get("content_sha256") != rejected_sha:
                raise DraftConflictError("草稿拒绝记录冲突，不得覆盖")
            return
        if content_sha256(content) != rejected_sha:
            raise DraftConflictError("草稿拒绝内容偏离固定摘要，不得覆盖")
        updated = {
            **doc,
            "lifecycle": "rejected",
            "content": content,
            "content_sha256": content_sha256(content),
            "rejected_at": rejected_at,
        }
        _write_doc(
            self._draft_path(checkpoint.run_id),
            updated,
            _render_draft_body(content, lifecycle="rejected"),
        )

    def _draft_path(self, run_id: str) -> Path:
        return self.drafts_dir / f"draft-{run_id}.md"

    def _send_block_reasons(self, checkpoint: LoopCheckpoint) -> list[str]:
        reasons: list[str] = []
        if checkpoint.status != "paused" or checkpoint.stop_reason != STOP_AWAITING_SEND:
            reasons.append("运行不在等待发送同意状态")
            return reasons
        reasons.extend(self._target_configuration_errors(checkpoint))
        consent = checkpoint.eval_results.get("send_consent")
        if not isinstance(consent, dict):
            return ["缺少有效发送同意记录"]
        reasons.extend(self._consent_binding_errors(checkpoint, consent))
        if consent.get("consumed") is not False:
            reasons.append("同意已消费")
        sha = str(self._projection(checkpoint)["payload_sha256"])
        for attempt in checkpoint.eval_results.get("model_call_attempts", []):
            if attempt.get("payload_sha256") == sha and attempt.get("request_sent") in (
                True,
                "unknown",
            ):
                reasons.append("该组合唯一调用名额已永久消耗，不得重发")
        return reasons

    def _consent_binding_errors(
        self, checkpoint: LoopCheckpoint, consent: Mapping[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        target_provider, target_model, target_profile = self._persisted_target(checkpoint)
        expected = {
            "fragment_id": checkpoint.fragment_id,
            "run_id": checkpoint.run_id,
            "outbound_payload_sha256": self._projection(checkpoint).get("payload_sha256"),
            "target_provider": target_provider,
            "target_model": target_model,
            "target_profile": target_profile,
        }
        for field_name, wanted in expected.items():
            if consent.get(field_name) != wanted:
                errors.append(f"同意绑定字段漂移: {field_name}")
        if not consent.get("consent_id") or not consent.get("consented_at"):
            errors.append("同意缺少 consent_id 或时间")
        return errors

    def _model_call_evidence_errors(self, evidence: ModelCallEvidence) -> list[str]:
        errors: list[str] = []
        if evidence.provider != self.target_provider:
            errors.append("provider 与冻结目标不一致")
        if evidence.response_model_claim != self.target_model:
            errors.append("response model claim 与冻结目标不一致")
        if evidence.http_status != 200:
            errors.append("HTTP 状态不是 200")
        tokens = (
            evidence.prompt_tokens,
            evidence.completion_tokens,
            evidence.total_tokens,
        )
        if any(isinstance(value, bool) or value < 0 for value in tokens):
            errors.append("usage Token 必须是非负整数")
        elif evidence.prompt_tokens + evidence.completion_tokens != evidence.total_tokens:
            errors.append("usage Token 求和不一致")
        if evidence.cost_status not in {"known", "unknown"}:
            errors.append("cost status 无效")
        return errors

    def _assert_display_binding(
        self, checkpoint: LoopCheckpoint, binding: Mapping[str, str]
    ) -> None:
        projection = self._projection(checkpoint)
        expected = {
            "fragment_id": checkpoint.fragment_id,
            "outbound_payload_sha256": str(projection.get("payload_sha256", "")),
            "target_provider": str(projection.get("target_provider", "")),
            "target_model": str(projection.get("target_model", "")),
            "target_profile": str(projection.get("target_profile", "")),
        }
        if dict(binding) != expected:
            raise PermissionError("发送目标绑定与当前展示内容不一致")

    def _projection(self, checkpoint: LoopCheckpoint) -> dict[str, Any]:
        projection = checkpoint.eval_results.get("minimum_value", {})
        return dict(projection) if isinstance(projection, dict) else {}

    def _persisted_target(self, checkpoint: LoopCheckpoint) -> tuple[str, str, str]:
        projection = self._projection(checkpoint)
        values = tuple(
            projection.get(field_name)
            for field_name in ("target_provider", "target_model", "target_profile")
        )
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or "\x1f" in value
            for value in values
        ):
            raise PermissionError("持久化目标配置无效")
        provider, model, profile = values
        assert isinstance(provider, str)
        assert isinstance(model, str)
        assert isinstance(profile, str)
        return provider, model, profile

    def _target_configuration_errors(self, checkpoint: LoopCheckpoint) -> list[str]:
        persisted = self._persisted_target(checkpoint)
        configured = (self.target_provider, self.target_model, self.target_profile)
        return [] if persisted == configured else ["目标配置与持久化记录不一致"]

    def _require(self, run_id: str) -> LoopCheckpoint:
        checkpoint = self.store.latest(run_id)
        if checkpoint is None:
            raise KeyError(f"Unknown run: {run_id}")
        return checkpoint

    def _require_seq(self, run_id: str) -> tuple[LoopCheckpoint, int]:
        found = self.store.latest_with_sequence(run_id)
        if found is None:
            raise KeyError(f"Unknown run: {run_id}")
        checkpoint, sequence = found
        return checkpoint, sequence

    def _replace(self, checkpoint: LoopCheckpoint, **changes: Any) -> LoopCheckpoint:
        return LoopCheckpoint.from_dict({**checkpoint.to_dict(), **changes})

    def _replace_with_projection(
        self,
        checkpoint: LoopCheckpoint,
        *,
        projection_update: Mapping[str, Any] | None = None,
        eval_results_update: Mapping[str, Any] | None = None,
        **changes: Any,
    ) -> LoopCheckpoint:
        eval_results = dict(checkpoint.eval_results)
        projection = {**self._projection(checkpoint), **(projection_update or {})}
        eval_results["minimum_value"] = projection
        eval_results.update(eval_results_update or {})
        return self._replace(checkpoint, eval_results=eval_results, **changes)

    def _cas(
        self,
        run_id: str,
        mutate: Callable[[LoopCheckpoint], LoopCheckpoint],
        *,
        event_type: str,
        revision_reason: str,
    ) -> LoopCheckpoint:
        for _ in range(_MAX_CAS_ATTEMPTS):
            raw_checkpoint, sequence = self._require_raw_seq(run_id)
            # mutate 在 legacy_flat_v1 视图上做旧式 flat 手术；写回时把
            # 视图增量翻译回本行的存储形态（namespaced → system 写 +
            # 单属主清理；旧 flat 行保持 flat，绝不迁移/双写）。
            candidate = mutate(checkpoint_compat_view(raw_checkpoint))
            stored_eval = translate_view_edit(
                existing=raw_checkpoint.eval_results,
                edited_flat=candidate.eval_results,
            )
            committed = self.store.compare_and_append(
                replace(candidate, eval_results=stored_eval),
                expected_sequence=sequence,
                event_type=event_type,
                revision_reason=revision_reason,
            )
            if committed is not None:
                return committed
        raise RuntimeError(f"Repeated checkpoint conflicts for run: {run_id}")

    def _require_raw_seq(self, run_id: str) -> tuple[LoopCheckpoint, int]:
        found = self.store.latest_raw_with_sequence(run_id)
        if found is None:
            raise KeyError(f"Unknown run: {run_id}")
        return found
