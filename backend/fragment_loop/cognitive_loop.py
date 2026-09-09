"""R1-B/R1-N: connect the R1-A cognitive contract to the existing Loop Core.

This module is deliberately offline-only. It has no model, network, file,
knowledge-base, credential, or publication adapter. A module-private generic
core runs one internally frozen profile per public wrapper:
``SyntheticCognitiveLoop`` keeps the exact R1-B synthetic behavior, while
``LocalCognitiveLoop`` (R1-N) accepts one explicit local user fragment. The
validated Markdown is kept in the existing append-only checkpoint and can only
be kept as a draft or rejected by an explicit human decision.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore, checkpoint_compat_view, utc_now
from common.execution import plugin_eval, translate_view_edit
from common.supervisor import (
    AdmissionState,
    LoopSpec,
    LoopSupervisor,
    SupervisorContext,
)
from common.types import LoopResult
from fragment_loop.cognitive_context import validate_context_binding
from fragment_loop.cognitive_contract import (
    render_cognitive_result,
    validate_cognitive_result,
    validate_semantic_expansion,
)
from fragment_loop.intake import MAX_FRAGMENT_TITLE_LENGTH
from fragment_loop.spec import (
    FRAGMENT_COGNITIVE_LOCAL_R1N,
    FRAGMENT_COGNITIVE_LOCAL_V1,
    FRAGMENT_COGNITIVE_SYNTHETIC_R1B,
)

CognitiveProducer = Callable[[str, str], Mapping[str, object]]
CognitiveDecision = Literal["keep_draft", "reject"]


class CognitiveDecisionError(ValueError):
    """The requested human decision is invalid for the current checkpoint."""


def normalize_thought_category(value: object) -> str:
    """Validate and normalize the explicit human thought category.

    The category is 1-128 characters after trimming surrounding whitespace;
    newlines, carriage returns, any C0/DEL control character, and lone
    surrogates are rejected on the raw string so trimming can never silently
    launder a forbidden character. The normalized value is what gets bound
    into the decision receipt; it can never carry a profile, LoopSpec, path,
    or lifecycle override.
    """
    if (
        not isinstance(value, str)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or not _utf8_encodable(value)
    ):
        raise CognitiveDecisionError("思考分类无效")
    category = value.strip()
    if not category or len(category) > 128:
        raise CognitiveDecisionError("思考分类无效")
    return category


def _utf8_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validate_synthetic_fragment_id(fragment_id: str) -> None:
    if not isinstance(fragment_id, str) or not fragment_id.startswith("synthetic-"):
        raise ValueError("R1-B 只接受 synthetic- 前缀的合成 fragment_id")


def _validate_local_fragment_id(fragment_id: str) -> None:
    if (
        not isinstance(fragment_id, str)
        or not 1 <= len(fragment_id) <= 256
        or fragment_id != fragment_id.strip()
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in fragment_id
        )
        or not _utf8_encodable(fragment_id)
    ):
        raise ValueError(
            "R1-N 本地 fragment_id 必须是 1-256 个已去除首尾空白、"
            "无控制字符且可严格 UTF-8 编码的显式标识"
        )


def _validate_synthetic_fragment_text(fragment_text: str) -> None:
    if not isinstance(fragment_text, str) or not fragment_text.strip():
        raise ValueError("fragment_text 必须是非空合成文本")


def _validate_local_fragment_text(fragment_text: str) -> None:
    if (
        not isinstance(fragment_text, str)
        or not 1 <= len(fragment_text) <= 20_000
        or any(
            (ord(character) < 0x20 and character not in "\n\t")
            or ord(character) == 0x7F
            for character in fragment_text
        )
        or not _utf8_encodable(fragment_text)
    ):
        raise ValueError(
            "R1-N 本地 fragment_text 必须是 1-20000 个字符的原文，"
            "只允许换行与制表符，且可严格 UTF-8 编码"
        )


def _validate_fragment_text_for_profile(fragment_text: str, source_kind: str) -> None:
    """Fixed internal dispatch on the frozen profile source kind."""
    if source_kind == "synthetic_fixture":
        _validate_synthetic_fragment_text(fragment_text)
        return
    if source_kind == "local_user_fragment":
        _validate_local_fragment_text(fragment_text)
        return
    raise ValueError(f"未知认知来源类别: {source_kind}")


def _fragment_id_is_valid_for_profile(
    fragment_id: object, profile: _CognitiveProfile
) -> bool:
    if not isinstance(fragment_id, str):
        return False
    try:
        profile.validate_fragment_id(fragment_id)
    except (ValueError, UnicodeEncodeError):
        return False
    return True


def _fragment_text_is_valid_for_profile(
    fragment_text: object, source_kind: str
) -> bool:
    if not isinstance(fragment_text, str):
        return False
    try:
        _validate_fragment_text_for_profile(fragment_text, source_kind)
    except (ValueError, UnicodeEncodeError):
        return False
    return True


def _binding_conflict_message(source_kind: str) -> str:
    if source_kind == "synthetic_fixture":
        return "fragment_id 已绑定不同的合成认知输入"
    return "fragment_id 已绑定不同的本地认知输入"


def _invalid_decision_message(source_kind: str) -> str:
    if source_kind == "synthetic_fixture":
        return "R1-B decision 必须是 keep_draft 或 reject"
    return "decision 必须是 keep_draft 或 reject"


def _awaiting_decision_resume_condition(source_kind: str) -> str:
    if source_kind == "synthetic_fixture":
        return "等待用户保留草稿或拒绝该合成认知结果"
    return "等待用户保留草稿或拒绝该认知结果"


@dataclass(frozen=True)
class _CognitiveProfile:
    """Internally frozen profile; callers can never supply or override it.

    The frozen configuration is exactly: the pinned LoopSpec, the pinned
    source kind, the pinned run ID namespace, the fragment ID validation
    rule, and the user-visible local title.
    """

    loopspec: LoopSpec
    source_kind: str
    run_id_prefix: str
    fragment_title: str
    validate_fragment_id: Callable[[str], None]
    allow_pending_route: bool = False
    allow_dynamic_title: bool = False


_SYNTHETIC_PROFILE_R1B = _CognitiveProfile(
    loopspec=FRAGMENT_COGNITIVE_SYNTHETIC_R1B,
    source_kind="synthetic_fixture",
    run_id_prefix="cognitive-r1b-",
    fragment_title="R1-B 合成认知碎片",
    validate_fragment_id=_validate_synthetic_fragment_id,
)

_LOCAL_PROFILE_R1N = _CognitiveProfile(
    loopspec=FRAGMENT_COGNITIVE_LOCAL_R1N,
    source_kind="local_user_fragment",
    run_id_prefix="cognitive-local-r1n-",
    fragment_title="本地用户认知碎片",
    validate_fragment_id=_validate_local_fragment_id,
)

_LOCAL_PROFILE_V1 = _CognitiveProfile(
    loopspec=FRAGMENT_COGNITIVE_LOCAL_V1,
    source_kind="local_user_fragment",
    run_id_prefix="cognitive-local-v1-",
    fragment_title="本地用户认知碎片",
    validate_fragment_id=_validate_local_fragment_id,
    allow_pending_route=True,
    allow_dynamic_title=True,
)


def _title_matches_profile(
    checkpoint: LoopCheckpoint, profile: _CognitiveProfile
) -> bool:
    title_value = checkpoint.fragment_title
    source_value = checkpoint.fragment_title_source
    if not isinstance(title_value, str) or not isinstance(source_value, str):
        return False
    if not profile.allow_dynamic_title:
        return (
            title_value == profile.fragment_title and source_value == "user_text"
        )
    return (
        bool(title_value) == bool(source_value)
        and source_value in {"", "user_note", "user_text"}
        and len(title_value) <= MAX_FRAGMENT_TITLE_LENGTH
        and not any(ord(character) < 0x20 for character in title_value)
        and _utf8_encodable(title_value)
    )


class _CognitiveLoopCore:
    """Generic offline cognitive loop core pinned to one frozen profile."""

    MAX_CAS_CONFLICTS = 8

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        producer: CognitiveProducer,
        profile: _CognitiveProfile,
    ):
        self.store = store
        self.producer = producer
        self._profile = profile
        self.supervisor = LoopSupervisor(
            store,
            profile.loopspec,
            {"cognitive_contract": self._validate_contract},
        )

    def register(
        self,
        *,
        fragment_id: str,
        fragment_text: str,
        route: Literal["research", "direct"],
    ) -> LoopCheckpoint:
        """Register one explicit fragment as already approved."""
        return self._register(
            fragment_id=fragment_id,
            fragment_text=fragment_text,
            route=route,
            input_refs=[],
        )

    def _register(
        self,
        *,
        fragment_id: str,
        fragment_text: str,
        route: Literal["research", "direct"] | None,
        input_refs: list[str],
        privacy_level: str | None = None,
        fragment_title: str | None = None,
        fragment_title_source: str | None = None,
        capture_metadata: Mapping[str, str] | None = None,
    ) -> LoopCheckpoint:
        self._profile.validate_fragment_id(fragment_id)
        _validate_fragment_text_for_profile(fragment_text, self._profile.source_kind)
        if route not in ("research", "direct") and not (
            route is None and self._profile.allow_pending_route
        ):
            raise ValueError("route 必须是 research 或 direct")
        cognitive_input: dict[str, object] = {
            "source_kind": self._profile.source_kind,
            "fragment_text": fragment_text,
            "expected_route": route,
        }
        if privacy_level is not None:
            cognitive_input["privacy_level"] = privacy_level
        title, title_source = self._registration_title(
            fragment_title, fragment_title_source
        )
        run_id = self._profile.run_id_prefix + hashlib.sha256(
            fragment_id.encode("utf-8")
        ).hexdigest()
        existing = self.store.latest(run_id)
        if existing is not None:
            original_input = existing.eval_results.get("cognitive_input", {})
            if isinstance(original_input, Mapping) and "capture_receipt" in original_input:
                cognitive_input["capture_receipt"] = original_input["capture_receipt"]
            return self._same_binding(
                existing,
                fragment_id,
                run_id,
                cognitive_input,
                self._profile,
                expected_input_refs=input_refs,
                expected_title=title,
                expected_title_source=title_source,
            )
        if capture_metadata is not None:
            if set(capture_metadata) != {
                "captured_at", "raw_sha256", "identity_sha256", "source_ref",
            }:
                raise ValueError("capture metadata fields invalid")
            cognitive_input["capture_receipt"] = {
                **capture_metadata, "version": "fragment-capture-v1", "received_at": utc_now(),
            }
        try:
            registered = self.supervisor.register(
                fragment_id,
                run_id=run_id,
                admission_state=AdmissionState.APPROVED,
                input_refs=input_refs,
                fragment_title=title,
                fragment_title_source=title_source,
                eval_results={"cognitive_input": cognitive_input},
            )
        except ValueError:
            raced = self.store.latest(run_id)
            if raced is None:
                raise
            original_input = raced.eval_results.get("cognitive_input", {})
            cognitive_input.pop("capture_receipt", None)
            if isinstance(original_input, Mapping) and "capture_receipt" in original_input:
                cognitive_input["capture_receipt"] = original_input["capture_receipt"]
            return self._same_binding(
                raced,
                fragment_id,
                run_id,
                cognitive_input,
                self._profile,
                expected_input_refs=input_refs,
                expected_title=title,
                expected_title_source=title_source,
            )
        if registered.eval_results.get("cognitive_input") != cognitive_input:
            raise ValueError(_binding_conflict_message(self._profile.source_kind))
        return registered

    def _registration_title(
        self,
        fragment_title: str | None,
        fragment_title_source: str | None,
    ) -> tuple[str, str]:
        if not self._profile.allow_dynamic_title:
            if fragment_title is not None or fragment_title_source is not None:
                raise ValueError("该认知入口不允许覆盖冻结标题")
            return self._profile.fragment_title, "user_text"
        title = fragment_title or ""
        title_source = fragment_title_source or ""
        if (
            bool(title) != bool(title_source)
            or title_source not in {"", "user_note", "user_text"}
            or len(title) > MAX_FRAGMENT_TITLE_LENGTH
            or any(ord(character) < 0x20 for character in title)
            or not _utf8_encodable(title)
        ):
            raise ValueError("认知入口标题或来源无效")
        return title, title_source

    def run(self, run_id: str) -> LoopCheckpoint:
        """Validate once, then durably park before any human decision."""
        latest = self._require(run_id)
        cognitive_input = latest.eval_results.get("cognitive_input")
        if (
            self._profile.allow_pending_route
            and isinstance(cognitive_input, Mapping)
            and cognitive_input.get("expected_route") is None
        ):
            return latest
        if latest.current_node == "human_decision" and latest.status == "running":
            return self._park_for_decision(run_id)
        if latest.status in {"paused", *LoopSupervisor.TERMINAL_STATUSES}:
            return latest
        progressed = self.supervisor.run(run_id, max_steps=1)
        if progressed.current_node == "human_decision" and progressed.status == "running":
            return self._park_for_decision(run_id)
        return progressed

    def decide(self, run_id: str, decision: CognitiveDecision) -> LoopCheckpoint:
        """Apply the only decisions: keep an unverified draft or reject it."""
        return self._decide(run_id, decision, binding=None)

    def decide_bound(
        self,
        run_id: str,
        decision: CognitiveDecision,
        *,
        fragment_id: str,
        expected_sequence: int,
        markdown_sha256: str,
        decision_id: str,
        thought_category: str | None = None,
    ) -> LoopCheckpoint:
        """Apply a decision only to the exact draft and sequence shown to the user.

        ``thought_category`` is the explicit human category for ``keep_draft``;
        when given it is normalized and atomically persisted inside the binding
        receipt. Older callers may omit it, producing a legacy receipt without
        a category that can never publish a local note. ``reject`` never
        persists a non-empty category.
        """
        if (
            not isinstance(fragment_id, str)
            or not fragment_id.strip()
            or not isinstance(expected_sequence, int)
            or isinstance(expected_sequence, bool)
            or expected_sequence < 1
            or not isinstance(markdown_sha256, str)
            or len(markdown_sha256) != 64
            or any(character not in "0123456789abcdef" for character in markdown_sha256)
            or not isinstance(decision_id, str)
            or not decision_id.strip()
            or len(decision_id) > 256
        ):
            raise CognitiveDecisionError("认知草稿决定绑定无效")
        category: str | None = None
        if decision == "keep_draft":
            if thought_category is not None:
                category = normalize_thought_category(thought_category)
        elif thought_category not in (None, ""):
            raise CognitiveDecisionError("思考分类无效")
        binding: dict[str, object] = {
            "decision_id": decision_id,
            "decision": decision,
            "fragment_id": fragment_id,
            "expected_sequence": expected_sequence,
            "markdown_sha256": markdown_sha256,
        }
        if category is not None:
            binding["thought_category"] = category
        return self._decide(run_id, decision, binding=binding)

    def _decide(
        self,
        run_id: str,
        decision: CognitiveDecision,
        *,
        binding: Mapping[str, object] | None,
    ) -> LoopCheckpoint:
        if decision not in ("keep_draft", "reject"):
            raise CognitiveDecisionError(
                _invalid_decision_message(self._profile.source_kind)
            )
        for _ in range(self.MAX_CAS_CONFLICTS):
            found = self.store.latest_raw_with_sequence(run_id)
            if found is None:
                raise KeyError(f"Unknown run: {run_id}")
            raw_checkpoint, sequence = found
            # 读走 legacy_flat_v1 投影；写回基于 raw 形态做视图增量翻译。
            checkpoint = checkpoint_compat_view(raw_checkpoint)
            if (
                checkpoint.loop_id != self._profile.loopspec.loop_id
                or checkpoint.loopspec_version != self._profile.loopspec.version
            ):
                raise CognitiveDecisionError("Run belongs to a different LoopSpec")
            if not self._decision_material_matches_profile(checkpoint, self._profile):
                raise CognitiveDecisionError("持久认知结果未通过重新校验")
            if (
                "cognitive_withdrawal_pending_receipt" in checkpoint.eval_results
                or "cognitive_withdrawal_receipt" in checkpoint.eval_results
            ):
                raise CognitiveDecisionError(
                    "认知结果已撤回或正在撤回，不能再保留或拒绝"
                )
            prior = checkpoint.eval_results.get("cognitive_decision")
            if checkpoint.status in LoopSupervisor.TERMINAL_STATUSES:
                if prior == decision and (
                    binding is None
                    or checkpoint.eval_results.get("cognitive_decision_receipt")
                    == binding
                ):
                    return checkpoint
                raise CognitiveDecisionError("认知结果已经作出不同的终态决策")
            if binding is not None:
                stored_markdown = checkpoint.eval_results.get("cognitive_markdown")
                if (
                    checkpoint.fragment_id != binding["fragment_id"]
                    or sequence != binding["expected_sequence"]
                    or not isinstance(stored_markdown, str)
                    or hashlib.sha256(stored_markdown.encode()).hexdigest()
                    != binding["markdown_sha256"]
                    or "cognitive_decision_receipt" in checkpoint.eval_results
                ):
                    raise CognitiveDecisionError("展示的认知草稿或状态已经变化")
            if (
                checkpoint.status != "paused"
                or checkpoint.stop_reason != "awaiting_cognitive_decision"
            ):
                raise CognitiveDecisionError("认知结果尚未进入人工决策点")
            eval_results = {
                **checkpoint.eval_results,
                "cognitive_decision": decision,
                "content_lifecycle": "draft" if decision == "keep_draft" else "rejected",
                "evidence_level": "unverified",
            }
            if binding is not None:
                eval_results["cognitive_decision_receipt"] = dict(binding)
            committed = self.store.compare_and_append(
                replace(
                    raw_checkpoint,
                    status="passed" if decision == "keep_draft" else "cancelled",
                    stop_reason=(
                        "cognitive_draft_kept"
                        if decision == "keep_draft"
                        else "cognitive_result_rejected"
                    ),
                    resume_condition=None,
                    eval_results=translate_view_edit(
                        existing=raw_checkpoint.eval_results,
                        edited_flat=eval_results,
                    ),
                ),
                expected_sequence=sequence,
                event_type="human_decision",
                revision_reason=decision,
            )
            if committed is not None:
                return committed
        raise RuntimeError("Repeated checkpoint conflicts during cognitive decision")

    def _validate_contract(self, context: SupervisorContext) -> LoopResult:
        input_value = context.checkpoint.eval_results.get("cognitive_input")
        if not isinstance(input_value, Mapping):
            return self._failed_safe("cognitive_input_invalid")
        fragment_text = input_value.get("fragment_text")
        expected_route = input_value.get("expected_route")
        bound_expansion = input_value.get("semantic_expansion")
        if (
            input_value.get("source_kind") != self._profile.source_kind
            or not isinstance(fragment_text, str)
            or expected_route not in ("research", "direct")
            or (
                self._profile.allow_pending_route
                and (
                    input_value.get("privacy_level") not in {"public", "personal"}
                    or validate_semantic_expansion(bound_expansion, fragment_text)
                )
            )
        ):
            return self._failed_safe("cognitive_input_invalid")
        try:
            produced = self.producer(fragment_text, str(expected_route))
        except Exception:
            return self._failed_safe("cognitive_producer_failed")
        if not isinstance(produced, Mapping):
            return self._failed_safe("cognitive_result_invalid")
        result = dict(produced)
        errors = validate_cognitive_result(result, fragment_text)
        if errors:
            return self._failed_safe("cognitive_contract_invalid", len(errors))
        if result.get("route") != expected_route:
            return self._failed_safe("cognitive_route_mismatch")
        if (
            self._profile.allow_pending_route
            and result.get("semantic_expansion") != bound_expansion
        ):
            return self._failed_safe("cognitive_semantic_expansion_mismatch")
        if (
            self._profile.allow_pending_route
            and result.get("route_reason") != input_value.get("route_reason")
        ):
            return self._failed_safe("cognitive_route_reason_mismatch")
        if "context_binding" in result and validate_context_binding(result):
            return self._failed_safe("cognitive_context_binding_invalid")
        markdown = render_cognitive_result(result, fragment_text)
        return LoopResult(
            status="continue",
            next_node="human_decision",
            eval_results=plugin_eval(
                "cognitive_contract",
                {
                    "cognitive_contract_status": "validated",
                    "cognitive_route": expected_route,
                    "cognitive_result": result,
                    "cognitive_markdown": markdown,
                    "cognitive_decision": "pending",
                    "content_lifecycle": "draft",
                    "evidence_level": "unverified",
                },
            ),
        )

    @staticmethod
    def _failed_safe(category: str, error_count: int = 1) -> LoopResult:
        return LoopResult(
            status="failed_safe",
            stop_reason=category,
            eval_results=plugin_eval(
                "cognitive_contract",
                {
                    "cognitive_contract_status": "rejected",
                    "cognitive_error_category": category,
                    "cognitive_error_count": error_count,
                },
            ),
        )

    def _park_for_decision(self, run_id: str) -> LoopCheckpoint:
        for _ in range(self.MAX_CAS_CONFLICTS):
            found = self.store.latest_raw_with_sequence(run_id)
            if found is None:
                raise KeyError(f"Unknown run: {run_id}")
            raw_checkpoint, sequence = found
            checkpoint = checkpoint_compat_view(raw_checkpoint)
            if checkpoint.status == "paused":
                return checkpoint
            if checkpoint.status in LoopSupervisor.TERMINAL_STATUSES:
                return checkpoint
            if checkpoint.current_node != "human_decision" or checkpoint.status != "running":
                return checkpoint
            committed = self.store.compare_and_append(
                replace(
                    raw_checkpoint,
                    status="paused",
                    stop_reason="awaiting_cognitive_decision",
                    resume_condition=_awaiting_decision_resume_condition(
                        self._profile.source_kind
                    ),
                ),
                expected_sequence=sequence,
                event_type="human_decision_required",
                revision_reason="awaiting_cognitive_decision",
            )
            if committed is not None:
                return committed
        raise RuntimeError("Repeated checkpoint conflicts while parking cognitive result")

    def _require(self, run_id: str) -> LoopCheckpoint:
        checkpoint = self.store.latest(run_id)
        if checkpoint is None:
            raise KeyError(f"Unknown run: {run_id}")
        if checkpoint.loop_id != self._profile.loopspec.loop_id:
            raise ValueError("Run belongs to a different LoopSpec")
        if checkpoint.loopspec_version != self._profile.loopspec.version:
            raise ValueError("Run is pinned to a different LoopSpec version")
        return checkpoint

    @staticmethod
    def _decision_material_matches_profile(
        checkpoint: LoopCheckpoint,
        profile: _CognitiveProfile,
    ) -> bool:
        if not _fragment_id_is_valid_for_profile(checkpoint.fragment_id, profile):
            return False
        if not _title_matches_profile(checkpoint, profile):
            return False
        try:
            expected_run_id = profile.run_id_prefix + hashlib.sha256(
                checkpoint.fragment_id.encode("utf-8")
            ).hexdigest()
        except (AttributeError, UnicodeEncodeError):
            return False
        if checkpoint.run_id != expected_run_id:
            return False
        values = checkpoint.eval_results
        cognitive_input = values.get("cognitive_input")
        result = values.get("cognitive_result")
        if not isinstance(cognitive_input, Mapping) or not isinstance(result, Mapping):
            return False
        fragment_text = cognitive_input.get("fragment_text")
        expected_route = cognitive_input.get("expected_route")
        if (
            cognitive_input.get("source_kind") != profile.source_kind
            or not _fragment_text_is_valid_for_profile(
                fragment_text, profile.source_kind
            )
            or expected_route not in ("research", "direct")
            or result.get("route") != expected_route
            or values.get("cognitive_contract_status") != "validated"
            or values.get("cognitive_route") != expected_route
            or values.get("evidence_level") != "unverified"
            or (
                profile.allow_pending_route
                and (
                    cognitive_input.get("privacy_level") not in {"public", "personal"}
                    or result.get("semantic_expansion")
                    != cognitive_input.get("semantic_expansion")
                    or result.get("route_reason")
                    != cognitive_input.get("route_reason")
                )
            )
        ):
            return False
        if not isinstance(fragment_text, str):
            return False
        if validate_cognitive_result(result, fragment_text):
            return False
        if "context_binding" in result and validate_context_binding(result):
            return False
        try:
            rendered = render_cognitive_result(result, fragment_text)
        except (TypeError, ValueError):
            return False
        stored_markdown = values.get("cognitive_markdown")
        return isinstance(stored_markdown, str) and stored_markdown == rendered

    @staticmethod
    def _same_binding(
        checkpoint: LoopCheckpoint,
        fragment_id: str,
        run_id: str,
        cognitive_input: Mapping[str, object],
        profile: _CognitiveProfile,
        *,
        expected_input_refs: list[str] | None = None,
        expected_title: str | None = None,
        expected_title_source: str | None = None,
    ) -> LoopCheckpoint:
        if (
            checkpoint.loop_id != profile.loopspec.loop_id
            or checkpoint.loopspec_version != profile.loopspec.version
            or checkpoint.fragment_id != fragment_id
            or checkpoint.run_id != run_id
            or checkpoint.fragment_title
            != (profile.fragment_title if expected_title is None else expected_title)
            or checkpoint.fragment_title_source
            != ("user_text" if expected_title_source is None else expected_title_source)
            or checkpoint.eval_results.get("cognitive_input") != cognitive_input
            or (
                expected_input_refs is not None
                and checkpoint.input_refs != expected_input_refs
            )
        ):
            raise ValueError(_binding_conflict_message(profile.source_kind))
        return checkpoint


class SyntheticCognitiveLoop(_CognitiveLoopCore):
    """Minimal R1-B facade over ``LoopSupervisor`` and its checkpoint store."""

    def __init__(self, store: SQLiteCheckpointStore, producer: CognitiveProducer):
        super().__init__(store, producer, _SYNTHETIC_PROFILE_R1B)

    @staticmethod
    def _decision_material_is_valid(checkpoint: LoopCheckpoint) -> bool:
        """R1-H compatibility entry pinned to the frozen synthetic profile.

        Delegates to the internal validator with the synthetic profile only;
        checkpoints of any other profile (including the R1-N local profile)
        are rejected.
        """
        return _CognitiveLoopCore._decision_material_matches_profile(
            checkpoint, _SYNTHETIC_PROFILE_R1B
        )


class LocalCognitiveLoop(_CognitiveLoopCore):
    """R1-N facade pinned to the frozen local-input profile.

    The local profile only uses the explicit ``fragment_id`` /
    ``fragment_text`` / ``route`` arguments plus the checkpoint store; it never
    reads files, knowledge bases, credentials, or process state.
    """

    def __init__(self, store: SQLiteCheckpointStore, producer: CognitiveProducer):
        super().__init__(store, producer, _LOCAL_PROFILE_R1N)

    @staticmethod
    def _decision_material_is_valid(checkpoint: LoopCheckpoint) -> bool:
        """Revalidation entry pinned to the frozen local profile.

        Delegates to the internal validator with the local profile only;
        checkpoints of any other profile (including the R1-B synthetic
        profile) are rejected.
        """
        return _CognitiveLoopCore._decision_material_matches_profile(
            checkpoint, _LOCAL_PROFILE_R1N
        )


class LocalCognitiveV1Loop(_CognitiveLoopCore):
    """Single production intake for new local nigo-loop fragments.

    Registration binds the original local source and deliberately leaves the
    route pending. Merely discovering a URL is not a route decision; a later
    semantic-expansion step must bind research/direct before execution.
    """

    def __init__(self, store: SQLiteCheckpointStore, producer: CognitiveProducer):
        super().__init__(store, producer, _LOCAL_PROFILE_V1)

    def register_intake(
        self,
        *,
        fragment_id: str,
        fragment_text: str,
        source_ref: str,
        privacy_level: str = "personal",
        fragment_title: str = "",
        fragment_title_source: str = "",
        captured_at: str | None = None,
        raw_sha256: str | None = None,
        identity_sha256: str | None = None,
    ) -> LoopCheckpoint:
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("source_ref 必须是非空本地来源引用")
        if privacy_level in {"sensitive", "restricted"}:
            raise PermissionError("敏感或受限碎片必须人工处理")
        if privacy_level not in {"public", "personal"}:
            raise ValueError("privacy_level 必须是 public 或 personal")
        capture_metadata = None
        try:
            timestamp = (datetime.fromisoformat(captured_at)
                         if isinstance(captured_at, str) else None)
            if (timestamp is not None and timestamp.tzinfo is not None
                    and timestamp.utcoffset() is not None and len(captured_at or "") <= 80
                    and isinstance(raw_sha256, str) and len(raw_sha256) == 64
                    and all(char in "0123456789abcdef" for char in raw_sha256)
                    and isinstance(identity_sha256, str) and len(identity_sha256) == 64
                    and all(char in "0123456789abcdef" for char in identity_sha256)):
                capture_metadata = {"captured_at": str(captured_at), "raw_sha256": raw_sha256,
                                    "identity_sha256": identity_sha256, "source_ref": source_ref}
        except ValueError:
            pass  # Registration may proceed, but absent capture proof grants no subscription scope.
        return self._register(
            capture_metadata=capture_metadata,
            fragment_id=fragment_id,
            fragment_text=fragment_text,
            route=None,
            input_refs=[source_ref],
            privacy_level=privacy_level,
            fragment_title=fragment_title,
            fragment_title_source=fragment_title_source,
        )

    def bind_route(
        self,
        run_id: str,
        route: Literal["research", "direct"],
        *,
        reason: str,
        semantic_expansion: Mapping[str, object],
    ) -> LoopCheckpoint:
        """Bind the post-expansion route exactly once, then allow execution."""
        if route not in ("research", "direct"):
            raise ValueError("route 必须是 research 或 direct")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1_000
            or any(
                ord(character) < 0x20 and character not in "\n\t"
                for character in reason
            )
            or not _utf8_encodable(reason)
        ):
            raise ValueError("route reason 必须是非空、可审阅的 UTF-8 文本")
        normalized_reason = reason.strip()
        frozen_expansion = deepcopy(dict(semantic_expansion))
        for _ in range(self.MAX_CAS_CONFLICTS):
            found = self.store.latest_raw_with_sequence(run_id)
            if found is None:
                raise KeyError(f"Unknown run: {run_id}")
            raw_checkpoint, sequence = found
            checkpoint = checkpoint_compat_view(raw_checkpoint)
            if (
                checkpoint.loop_id != self._profile.loopspec.loop_id
                or checkpoint.loopspec_version != self._profile.loopspec.version
            ):
                raise ValueError("Run belongs to a different LoopSpec")
            input_value = checkpoint.eval_results.get("cognitive_input")
            if not isinstance(input_value, Mapping):
                raise ValueError("认知入口绑定无效")
            fragment_text = input_value.get("fragment_text")
            if (
                set(input_value)
                - {
                    "source_kind",
                    "fragment_text",
                    "expected_route",
                    "privacy_level",
                    "route_reason",
                    "semantic_expansion",
                    "capture_receipt",
                }
                or input_value.get("source_kind") != self._profile.source_kind
                or input_value.get("privacy_level") not in {"public", "personal"}
                or not isinstance(fragment_text, str)
                or validate_semantic_expansion(frozen_expansion, fragment_text)
                or not _title_matches_profile(checkpoint, self._profile)
            ):
                raise ValueError("认知入口或语义展开绑定无效")
            current_route = input_value.get("expected_route")
            current_reason = input_value.get("route_reason")
            if current_route is not None:
                if (
                    current_route == route
                    and current_reason == normalized_reason
                    and input_value.get("semantic_expansion") == frozen_expansion
                ):
                    return checkpoint
                raise ValueError("认知路线已经绑定，不能改写")
            if (
                checkpoint.status != "approved"
                or checkpoint.current_node != "cognitive_contract"
            ):
                raise ValueError("认知入口已离开路线判断边界")
            eval_results = {
                **checkpoint.eval_results,
                "cognitive_input": {
                    **dict(input_value),
                    "expected_route": route,
                    "route_reason": normalized_reason,
                    "semantic_expansion": frozen_expansion,
                },
            }
            committed = self.store.compare_and_append(
                replace(
                    raw_checkpoint,
                    eval_results=translate_view_edit(
                        existing=raw_checkpoint.eval_results,
                        edited_flat=eval_results,
                    ),
                ),
                expected_sequence=sequence,
                event_type="cognitive_route_bound",
                revision_reason="semantic_route_decision",
            )
            if committed is not None:
                return committed
        raise RuntimeError("Repeated checkpoint conflicts during route binding")
