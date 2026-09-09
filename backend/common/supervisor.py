"""Minimal persistent Micro Loop Supervisor."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from time import monotonic
from typing import Any

from common.checkpoint import (
    LoopCheckpoint,
    SQLiteCheckpointStore,
    checkpoint_compat_view,
)
from common.errors import classify_error
from common.execution import (
    ExecutionReservations,
    InvalidUsageError,
    Reservation,
    build_adapter_inventory,
    legacy_flat_eval_view,
    merge_plugin_eval_results,
    new_owner_token,
    validate_usage,
)
from common.types import (
    ACTUAL_REGISTRY_CLASSIFICATIONS,
    EXECUTION_RECEIPT_KEYS,
    EXECUTION_RECEIPT_SCHEMA,
    LoopResult,
    eval_results_digest,
)


class AdmissionState(str, Enum):  # noqa: UP042 - project supports Python 3.10
    NOT_REQUESTED = "not_requested"
    SUGGESTED = "suggested"
    REQUESTED = "requested"
    PREFLIGHT_PASSED = "preflight_passed"
    APPROVED = "approved"


ADMISSION_TRANSITIONS: dict[AdmissionState, set[AdmissionState]] = {
    AdmissionState.NOT_REQUESTED: {
        AdmissionState.SUGGESTED,
        AdmissionState.REQUESTED,
    },
    AdmissionState.SUGGESTED: {
        AdmissionState.NOT_REQUESTED,
        AdmissionState.REQUESTED,
    },
    AdmissionState.REQUESTED: {
        AdmissionState.NOT_REQUESTED,
        AdmissionState.PREFLIGHT_PASSED,
    },
    AdmissionState.PREFLIGHT_PASSED: {
        AdmissionState.NOT_REQUESTED,
        AdmissionState.APPROVED,
    },
    AdmissionState.APPROVED: set(),
}


@dataclass(frozen=True)
class LoopSpec:
    loop_id: str
    version: str
    goal: str
    first_node: str
    nodes: tuple[str, ...]
    max_iterations: int
    max_seconds: int
    token_limit: int
    tool_call_limit: int
    worker_version: str = "unassigned"
    evaluator_version: str = "unassigned"


@dataclass(frozen=True)
class SupervisorContext:
    checkpoint: LoopCheckpoint
    store: SQLiteCheckpointStore
    spec: LoopSpec


NodeHandler = Callable[[SupervisorContext], LoopResult]


def _loop_spec_digest(spec: LoopSpec) -> str:
    # loopspec-v2 强绑定：canonical JSON 覆盖 LoopSpec 全部字段（含执行语义与
    # 角色版本）；旧 v1（仅 id/version）弱 digest 行只读不回填，恢复由
    # reservation 层 spec_digest_mismatch fail closed（人工停止）。
    canonical = json.dumps(
        {"schema": "loopspec-v2", **asdict(spec)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _loop_input_digest(checkpoint: LoopCheckpoint) -> str:
    """Binding input identity of one handler entry: node plus iteration."""
    canonical = "\x1f".join(
        (
            "loop-input-v1",
            checkpoint.loop_id,
            checkpoint.loopspec_version,
            checkpoint.current_node,
            str(checkpoint.iteration),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LoopSupervisor:
    """Persist every transition and resume from the latest committed node.

    All commits use compare-and-append: on conflict the latest state is
    reloaded and re-evaluated, and externally applied control state
    (pause_requested / paused / terminal) is never overwritten by stale
    node results or error blocks.
    """

    TERMINAL_STATUSES = {
        "passed",
        "exhausted",
        "blocked",
        "escalated",
        "cancelled",
        "superseded",
        "failed_safe",
    }

    MAX_CAS_CONFLICTS = 8

    def __init__(
        self,
        store: SQLiteCheckpointStore,
        spec: LoopSpec,
        handlers: dict[str, NodeHandler],
        execution_classes: Mapping[str, str] | None = None,
        receipt_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
    ):
        unknown = set(handlers) - set(spec.nodes)
        if unknown:
            raise ValueError(f"Handlers reference unknown nodes: {sorted(unknown)}")
        self.store = store
        self.spec = spec
        self.handlers = handlers
        self._reservations = ExecutionReservations(store.path)
        # G1-15（Loop 侧）：idempotent 恢复经该原键 receipt 查询对账。
        self._receipt_provider = receipt_provider
        # G1-25/26 强制面：显式 execution_classes 优先；None 时消费冻结的
        # ACTUAL_REGISTRY_CLASSIFICATIONS——handlers 中任何未知 key 在构造期
        # fail closed（missing），绝不隐式默认、绝不 NULL 分类进 handler。
        if execution_classes is None:
            execution_classes = {
                key: ACTUAL_REGISTRY_CLASSIFICATIONS[key]["execution_class"]
                for key in handlers
                if key in ACTUAL_REGISTRY_CLASSIFICATIONS
            }
        self._inventory = build_adapter_inventory(
            handlers,
            runtime_kind="loop",
            classifications=execution_classes,
        )

    def _registration_checkpoint(
        self,
        fragment_id: str,
        *,
        admission_state: AdmissionState,
        run_id: str | None,
        parent_loop_id: str | None,
        input_refs: list[str] | None,
        fragment_title: str | None,
        fragment_title_source: str | None,
        eval_results: dict[str, Any] | None = None,
    ) -> LoopCheckpoint:
        return LoopCheckpoint(
            loop_id=self.spec.loop_id,
            loopspec_version=self.spec.version,
            run_id=run_id or str(uuid.uuid4()),
            fragment_id=fragment_id,
            parent_loop_id=parent_loop_id,
            current_node=self.spec.first_node,
            status=admission_state.value,
            goal=self.spec.goal,
            fragment_title=fragment_title or "",
            fragment_title_source=fragment_title_source or "",
            input_refs=input_refs or [],
            # Gate 1：新登记行的受信登记材料一律写入 system 命名空间并创建
            # 空 plugins 壳；绝不生成 flat 业务键。
            eval_results={"system": dict(eval_results or {}), "plugins": {}},
            budget_limits={
                "iterations": self.spec.max_iterations,
                "seconds": self.spec.max_seconds,
                "tokens": self.spec.token_limit,
                "tool_calls": self.spec.tool_call_limit,
            },
            budget_used={"tokens": 0, "tool_calls": 0, "seconds": 0.0},
            worker_version=self.spec.worker_version,
            evaluator_version=self.spec.evaluator_version,
        )

    def register(
        self,
        fragment_id: str,
        *,
        admission_state: AdmissionState,
        input_refs: list[str] | None = None,
        run_id: str | None = None,
        parent_loop_id: str | None = None,
        fragment_title: str | None = None,
        fragment_title_source: str | None = None,
        eval_results: dict[str, Any] | None = None,
    ) -> LoopCheckpoint:
        checkpoint = self._registration_checkpoint(
            fragment_id,
            admission_state=admission_state,
            run_id=run_id,
            parent_loop_id=parent_loop_id,
            input_refs=input_refs,
            fragment_title=fragment_title,
            fragment_title_source=fragment_title_source,
            eval_results=eval_results,
        )
        committed, _ = self.store.register_attempt(checkpoint, allow_reopen=False)
        return committed

    def reopen_episode(
        self,
        fragment_id: str,
        *,
        run_id: str | None = None,
        input_refs: list[str] | None = None,
        fragment_title: str | None = None,
        fragment_title_source: str | None = None,
        opened_by: str = "nigo",
        approval_evidence: dict[str, Any] | None = None,
    ) -> LoopCheckpoint:
        """Explicit, auditable reopen of a resolved attempt family.

        Registers the next episode atomically — the family read, episode
        decision, and insert happen in one BEGIN IMMEDIATE, so a
        simultaneous resolver or reopen can never slip between them. The
        reopened checkpoint keeps the ordinary registration contract:
        budget limits, initial usage, worker/evaluator versions, and the
        frozen approval evidence. Only the trusted intake path may call
        this, and only after the family's highest episode is resolved.
        """
        latest = self.store.latest_for_fragment(self.spec.loop_id, fragment_id)
        evidence = approval_evidence or {"approved": True, "source": "nigo-loop"}
        checkpoint = self._registration_checkpoint(
            fragment_id,
            admission_state=AdmissionState.APPROVED,
            run_id=run_id,
            parent_loop_id=None,
            input_refs=input_refs or (list(latest.input_refs) if latest else None),
            fragment_title=fragment_title or (latest.fragment_title if latest else None),
            fragment_title_source=(
                fragment_title_source or (latest.fragment_title_source if latest else None)
            ),
            eval_results={"approval": evidence},
        )
        committed, _ = self.store.register_attempt(
            checkpoint, allow_reopen=True, opened_by=opened_by
        )
        return committed

    def transition_admission(self, run_id: str, target: AdmissionState) -> LoopCheckpoint:
        for _ in range(self.MAX_CAS_CONFLICTS):
            checkpoint, sequence = self._require_seq(run_id)
            try:
                current = AdmissionState(checkpoint.status)
            except ValueError as error:
                raise ValueError(f"Run is no longer in admission: {checkpoint.status}") from error
            if target not in ADMISSION_TRANSITIONS[current]:
                raise ValueError(
                    f"Invalid admission transition: {current.value} -> {target.value}"
                )
            committed = self._cas(
                replace(
                    checkpoint,
                    status=target.value,
                    resume_condition=(
                        None if target is AdmissionState.APPROVED else checkpoint.resume_condition
                    ),
                ),
                sequence,
                event_type="admission_transition",
                revision_reason=f"{current.value}->{target.value}",
            )
            if committed is not None:
                return committed
        raise RuntimeError("Repeated checkpoint conflicts during admission transition")

    def run(
        self,
        run_id: str,
        *,
        max_steps: int | None = None,
        revision_suffix: str | None = None,
    ) -> LoopCheckpoint:
        steps = 0
        attempts = 0
        owner = new_owner_token()
        # 结果重提交 memo（Gate 1 P0-1）：handler 一旦入场，本 run() 内的
        # CAS 冲突只重放提交同一份内存结果/错误，绝不二次进入 handler；
        # 同 owner 的 running reservation 也不会再发新执行许可。
        pending: tuple[LoopResult, float, str] | None = None
        pending_error: tuple[Exception, str] | None = None
        while attempts < self.MAX_CAS_CONFLICTS:
            checkpoint, sequence = self._require_seq(run_id)
            if checkpoint.status in self.TERMINAL_STATUSES:
                return checkpoint_compat_view(checkpoint)
            if checkpoint.status == "pause_requested":
                if pending is None and pending_error is None:
                    return self._finalize_pause(run_id)
                # 有待提交结果：落入下方 pause 分支合并提交。
            elif checkpoint.status == "paused":
                return checkpoint_compat_view(checkpoint)
            elif checkpoint.status not in {AdmissionState.APPROVED.value, "running"}:
                raise PermissionError("Loop may run only after explicit approved admission state")
            elif checkpoint.status == AdmissionState.APPROVED.value:
                committed = self._cas(
                    replace(checkpoint, status="running"),
                    sequence,
                    event_type="state_transition",
                    revision_reason="approved->running",
                )
                if committed is None:
                    attempts += 1
                    continue
                checkpoint, sequence = self._require_seq(run_id)

            if pending is None and pending_error is None:
                stop = self._budget_stop(checkpoint)
                if stop is not None:
                    committed = self._cas(
                        stop,
                        sequence,
                        event_type="loop_stopped",
                        revision_reason=stop.stop_reason,
                    )
                    if committed is not None:
                        return committed
                    attempts += 1
                    continue
                if max_steps is not None and steps >= max_steps:
                    return checkpoint_compat_view(checkpoint)

                handler = self.handlers.get(checkpoint.current_node)
                if handler is None:
                    committed = self._cas(
                        replace(
                            checkpoint,
                            status="blocked",
                            stop_reason="missing_handler",
                            last_error={
                                "category": "internal",
                                "message": f"No handler for {checkpoint.current_node}",
                            },
                            resume_condition="Install the missing node handler",
                        ),
                        sequence,
                        event_type="loop_blocked",
                        revision_reason="missing_handler",
                    )
                    if committed is not None:
                        return committed
                    attempts += 1
                    continue

                # G1-25/26：分类 runtime 在 reservation 前解析节点冻结
                # class+metadata digest；未分类节点在此阻断，绝不静默执行。
                classification: str | None = None
                classification_digest: str | None = None
                if self._inventory is not None:
                    inventory_entry = self._inventory.get(checkpoint.current_node)
                    if inventory_entry is None:
                        committed = self._cas(
                            replace(
                                checkpoint,
                                status="blocked",
                                stop_reason="execution_classification_missing",
                                last_error={
                                    "category": "internal",
                                    "message": f"Node not classified: {checkpoint.current_node}",
                                },
                                resume_condition="Classify the node execution class",
                            ),
                            sequence,
                            event_type="loop_blocked",
                            revision_reason="execution_classification_missing",
                        )
                        if committed is not None:
                            return committed
                        attempts += 1
                        continue
                    classification = inventory_entry["execution_class"]
                    classification_digest = inventory_entry["metadata_digest"]

                # Gate 1：handler 入场由持久 reservation 串行化；并发失败者
                # 不进 handler，只观察最新状态。
                reservation = self._reservations.reserve(
                    runtime_kind="loop",
                    run_id=run_id,
                    node_id=checkpoint.current_node,
                    spec_digest=_loop_spec_digest(self.spec),
                    input_digest=_loop_input_digest(checkpoint),
                    owner_token=owner,
                    execution_class=classification,
                    classification_digest=classification_digest,
                )
                if not reservation.acquired:
                    if reservation.reason == "reconcile_required":
                        # G1-15：只对原稳定键的 verified receipt 提交恢复；无证据保持人工等待。
                        recovered = self._recover_verified_receipt(run_id, reservation)
                        if recovered is not None:
                            return recovered
                    return checkpoint_compat_view(self._require(run_id))
                execution_id = reservation.execution_id
                assert execution_id is not None  # acquired implies identity
                # 入场即 running：同 owner 重入被拒，崩溃恢复可区分 pre-send。
                self._reservations.mark_running(execution_id, owner)

                handler_started = monotonic()
                try:
                    # Handler 是插件：只见 legacy_flat_v1 只读兼容投影，绝不接触存储态。
                    handler_checkpoint = replace(
                        checkpoint,
                        eval_results=dict(legacy_flat_eval_view(checkpoint.eval_results)),
                    )
                    result = handler(SupervisorContext(handler_checkpoint, self.store, self.spec))
                except BaseException as error:
                    # handler 无论如何退出都消费本次 attempt：先记 failed，
                    # 后续 resume 开下一 logical attempt，而不是干等中断者的 lease。
                    self._reservations.fail(execution_id, owner)
                    if not isinstance(error, Exception):
                        raise
                    pending_error = (error, execution_id)
                else:
                    handler_seconds = max(0.0, monotonic() - handler_started)
                    # Strict usage：非法计数/时长不进 checkpoint 与预算；
                    # 已消费调用记 failed_invalid_usage，绝不退还。
                    try:
                        validate_usage(
                            tokens_used=result.tokens_used,
                            tool_calls_used=result.tool_calls_used,
                            duration_seconds=handler_seconds,
                        )
                    except InvalidUsageError as error:
                        self._reservations.fail(execution_id, owner, invalid_usage=True)
                        pending_error = (error, execution_id)
                    else:
                        pending = (result, handler_seconds, execution_id)

            if pending_error is not None:
                handler_error, _error_execution_id = pending_error
                committed = self._persist_handler_error(run_id, checkpoint, handler_error)
                if committed is None:
                    attempts += 1
                    continue  # 只重放错误持久化，不重进 handler
                return committed

            assert pending is not None
            result, handler_seconds, execution_id = pending

            # The handler returned normally. Reload again: control intents
            # applied during execution win over the stale local state.
            latest, latest_seq = self._require_seq(run_id)
            if latest.status == "pause_requested":
                if result.status == "continue":
                    try:
                        merged = replace(
                            self._apply_result(
                                latest,
                                result,
                                handler_seconds=handler_seconds,
                            ),
                            status="paused",
                        )
                    except ValueError as error:
                        self._reservations.fail(
                            execution_id,
                            owner,
                            invalid_usage=isinstance(error, InvalidUsageError),
                        )
                        pending = None
                        pending_error = (error, execution_id)
                        continue
                    committed = self._cas(
                        merged,
                        latest_seq,
                        event_type="node_completed",
                        revision_reason="pause_honored_at_node_boundary",
                    )
                    if committed is not None:
                        self._reservations.complete(execution_id, owner)
                        return committed
                    attempts += 1
                    continue  # pending 保留：结果重提交
                return self._finalize_pause(run_id)
            if latest.status == "paused":
                return checkpoint_compat_view(latest)
            if latest.status in self.TERMINAL_STATUSES and latest.status != checkpoint.status:
                return checkpoint_compat_view(latest)
            if latest.status != "running":
                return checkpoint_compat_view(latest)

            try:
                next_checkpoint = self._apply_result(
                    latest,
                    result,
                    handler_seconds=handler_seconds,
                )
            except ValueError as error:
                self._reservations.fail(
                    execution_id,
                    owner,
                    invalid_usage=isinstance(error, InvalidUsageError),
                )
                pending = None
                pending_error = (error, execution_id)
                continue
            committed = self._cas(
                next_checkpoint,
                latest_seq,
                event_type=(
                    "loop_stopped"
                    if result.status in self.TERMINAL_STATUSES
                    else "node_completed"
                ),
                revision_reason=(
                    f"{latest.current_node}|{revision_suffix}"
                    if revision_suffix
                    else latest.current_node
                ),
            )
            if committed is not None:
                self._reservations.complete(execution_id, owner)
                checkpoint = committed
                steps += 1
                attempts = 0
                pending = None
                continue
            attempts += 1
            # CAS 冲突：pending 保留，下一轮只对最新状态重放提交同一结果。
        raise RuntimeError("Repeated checkpoint conflicts while running the loop")

    def resume(self, run_id: str, *, max_steps: int | None = None) -> LoopCheckpoint:
        return self.run(run_id, max_steps=max_steps)

    def _recover_verified_receipt(
        self, run_id: str, reservation: Reservation
    ) -> LoopCheckpoint | None:
        """G1-15（Loop 侧）：只对原稳定键的 verified receipt 提交恢复。

        与 Graph 同一冻结 envelope（execution-receipt-v1）：键集、
        execution_id、run/node/spec/input/classification 绑定、
        result_digest 复算、LoopResult 形态与 usage 严格校验。先 CAS 提交
        再幂等 finalize；CAS 冲突或任何错配返回 None（可重试／人工等待，
        零 handler 入场）。"""
        if self._receipt_provider is None:
            return None
        execution_id = reservation.execution_id
        if execution_id is None:
            return None
        binding = self._reservations.binding_of(execution_id)
        if binding is None:
            return None
        receipt = self._receipt_provider(execution_id)
        if not isinstance(receipt, Mapping) or set(receipt) != EXECUTION_RECEIPT_KEYS:
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
        output = receipt.get("output")
        if not isinstance(output, dict):
            return None
        if receipt.get("result_digest") != eval_results_digest(output):
            return None
        allowed = {field.name for field in fields(LoopResult)}
        if not set(output) <= allowed:
            return None
        status = output.get("status")
        if status != "continue" and status not in self.TERMINAL_STATUSES:
            return None
        try:
            result = LoopResult(**output)
            validate_usage(
                tokens_used=result.tokens_used,
                tool_calls_used=result.tool_calls_used,
                duration_seconds=0.0,
            )
        except (TypeError, ValueError):
            return None
        latest, latest_seq = self._require_seq(run_id)
        if latest.status != "running":
            return checkpoint_compat_view(latest)
        try:
            next_checkpoint = self._apply_result(latest, result, handler_seconds=0.0)
        except ValueError:
            return None
        committed = self._cas(
            next_checkpoint,
            latest_seq,
            event_type=(
                "loop_stopped"
                if result.status in self.TERMINAL_STATUSES
                else "node_completed"
            ),
            revision_reason=f"{latest.current_node}|reconciled",
        )
        if committed is None:
            return None
        self._reservations.complete_from_verified_receipt(execution_id)
        return committed

    def _require(self, run_id: str) -> LoopCheckpoint:
        return self._require_seq(run_id)[0]

    def _require_seq(self, run_id: str) -> tuple[LoopCheckpoint, int]:
        # Raw stored form: the write path must merge against the namespaced
        # storage, never against a compat view.
        found = self.store.latest_raw_with_sequence(run_id)
        if found is None:
            raise KeyError(f"Unknown run: {run_id}")
        checkpoint, sequence = found
        if checkpoint.loopspec_version != self.spec.version:
            raise ValueError("Running instance is pinned to a different LoopSpec version")
        return checkpoint, sequence

    def _cas(
        self,
        checkpoint: LoopCheckpoint,
        base_sequence: int,
        *,
        event_type: str,
        revision_reason: str | None,
    ) -> LoopCheckpoint | None:
        return self.store.compare_and_append(
            checkpoint,
            expected_sequence=base_sequence,
            event_type=event_type,
            revision_reason=revision_reason,
        )

    def _finalize_pause(self, run_id: str) -> LoopCheckpoint:
        for _ in range(self.MAX_CAS_CONFLICTS):
            latest, sequence = self._require_seq(run_id)
            if latest.status != "pause_requested":
                return checkpoint_compat_view(latest)
            committed = self._cas(
                replace(latest, status="paused"),
                sequence,
                event_type="state_transition",
                revision_reason="pause_honored_at_node_boundary",
            )
            if committed is not None:
                return committed
        raise RuntimeError("Repeated checkpoint conflicts finalizing a pause")

    def _persist_handler_error(
        self,
        run_id: str,
        checkpoint: LoopCheckpoint,
        error: Exception,
    ) -> LoopCheckpoint | None:
        """Persist a handler failure as blocked, preserving control state.

        Reloads first: a pause/terminate intent accepted while the handler
        ran must survive; the error path never overwrites it with blocked.
        Returns None on a CAS conflict so the caller reloads and retries.
        """
        latest, latest_seq = self._require_seq(run_id)
        if latest.status == "pause_requested":
            return self._finalize_pause(run_id)
        if latest.status == "paused":
            return checkpoint_compat_view(latest)
        if latest.status in self.TERMINAL_STATUSES and latest.status != checkpoint.status:
            return checkpoint_compat_view(latest)
        decision = classify_error(error)
        return self._cas(
            replace(
                latest,
                status="blocked",
                stop_reason="internal_error",
                last_error={
                    "category": decision.category.value,
                    "retryable": decision.retryable,
                    "type": type(error).__name__,
                    "message": str(error),
                },
                resume_condition=decision.resume_condition,
            ),
            latest_seq,
            event_type="loop_blocked",
            revision_reason=f"{decision.category.value}:{type(error).__name__}",
        )

    def _budget_stop(self, checkpoint: LoopCheckpoint) -> LoopCheckpoint | None:
        used = checkpoint.budget_used
        reason = None
        # max_iterations limits actual handler calls (reservation entries:
        # first entries plus crash-recovery takeovers), not committed
        # successes; failures and retries consume budget and are never
        # refunded. checkpoint.iteration stays as the legacy floor so
        # pre-Gate-1 checkpoints without reservations keep their semantics.
        actual_calls = max(
            self._reservations.entry_count("loop", checkpoint.run_id),
            checkpoint.iteration,
        )
        if actual_calls >= self.spec.max_iterations:
            reason = "iteration_budget_exhausted"
        elif float(used.get("seconds", 0.0)) >= self.spec.max_seconds:
            reason = "time_budget_exhausted"
        elif int(used.get("tokens", 0)) >= self.spec.token_limit:
            reason = "token_budget_exhausted"
        elif int(used.get("tool_calls", 0)) >= self.spec.tool_call_limit:
            reason = "tool_budget_exhausted"
        if reason is None:
            return None
        return replace(
            checkpoint,
            status="exhausted",
            stop_reason=reason,
            resume_condition="Increase budget or narrow the Loop goal",
        )

    def _apply_result(
        self,
        checkpoint: LoopCheckpoint,
        result: LoopResult,
        *,
        handler_seconds: float = 0.0,
    ) -> LoopCheckpoint:
        # Final strict-usage guard (also enforced by run() right after the
        # handler returns): invalid usage raises InvalidUsageError, a
        # ValueError, so callers persist a blocked state with zero result
        # commit and zero budget drift.
        validate_usage(
            tokens_used=result.tokens_used,
            tool_calls_used=result.tool_calls_used,
            duration_seconds=handler_seconds,
        )
        if result.status == "continue":
            if result.next_node not in self.spec.nodes:
                raise ValueError(f"Invalid next node: {result.next_node}")
            status = "running"
            current_node = str(result.next_node)
        elif result.status in self.TERMINAL_STATUSES:
            status = result.status
            current_node = checkpoint.current_node
        else:
            raise ValueError(f"Invalid LoopResult status: {result.status}")

        used = dict(checkpoint.budget_used)
        used["tokens"] = int(used.get("tokens", 0)) + result.tokens_used
        used["tool_calls"] = int(used.get("tool_calls", 0)) + result.tool_calls_used
        used["seconds"] = float(used.get("seconds", 0.0)) + handler_seconds
        # Gate 1 eval namespace: the handler is a plugin keyed by its trusted
        # registry id (the current node from the pinned spec, never the
        # payload). Protected system keys and foreign plugins.* targets raise
        # EvalNamespaceError (a ValueError) before anything merges, so the
        # run blocks with zero result commit and system material stays
        # byte-identical. Flat legacy content merges exactly as before.
        merged_eval = merge_plugin_eval_results(
            existing=checkpoint.eval_results,
            adapter_id=checkpoint.current_node,
            returned=result.eval_results,
        )
        return replace(
            checkpoint,
            iteration=checkpoint.iteration + 1,
            current_node=current_node,
            status=status,
            evidence_refs=[*checkpoint.evidence_refs, *result.evidence_refs],
            artifact_refs=[*checkpoint.artifact_refs, *result.output_refs],
            eval_results=merged_eval,
            unresolved_issues=result.unresolved_issues,
            budget_used=used,
            stop_reason=result.stop_reason,
            resume_condition=result.resume_condition,
            pending_action=None,
        )
