"""受控 Agent Adapter 与内存假 Provider Transport（Graph Phase 2A，冻结切片）。

Adapter 的职责固定为：读取构造方注入的不可变 ``AgentPolicy``，校验授权收据，
先在持久账本预留、再调用显式注入的 Transport，校验响应身份与预算，先把完成
结果原子写入账本、再把 ``NodeResult`` 交回 Graph Runtime 提交 Checkpoint。

核心绝不提供 HTTP、Shell、动态 import、命令模板或凭据读取；Transport 只能显式
注入。Phase 2A 唯一可用的 Transport 是 ``InMemoryFakeTransport``：Policy 固定
``live_enabled=False`` 时，任何其他 Transport 对象都在发送前失败。已预留但未
完成、发送状态未知、响应身份漂移或超预算时固定阻断，绝不自动重试或 fallback。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from graph_runtime.agent_ledger import (
    ATTACH_OK,
    MAX_DB_BYTES,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RESERVED,
    AgentCallRecord,
    AgentExecutionLink,
    AgentLedgerStore,
    RequestSent,
)
from graph_runtime.agent_policy import (
    AgentPolicy,
    AuthorizationReceipt,
    validate_receipt_binding,
)
from graph_runtime.runtime import NodeRequest, NodeResult, flatten_inputs
from graph_runtime.spec import canonical_json, digest_of

# Phase 2A 唯一已知的 provider/model：内存假 Provider。
FAKE_PROVIDER = "fake-provider"
FAKE_MODEL = "fake-model-v1"
PHASE2A_ALLOWED_MODELS = frozenset(((FAKE_PROVIDER, FAKE_MODEL),))

# 响应正文大小硬顶（canonical JSON 字节）；固定的有界上限，不是无限额度。
MAX_RESPONSE_BYTES = 4 * 1024
# 响应 payload 的固定 JSON schema：精确键集合与类型。
RESPONSE_PAYLOAD_SCHEMA = {"draft_text": "string", "agent_note": "string"}
MAX_RESPONSE_FIELD_CHARS = 4096


class TransportError(RuntimeError):
    """Transport 在发送前抛出的确定性错误；账本记 request_sent=false。"""


class Transport(Protocol):
    """Transport 协议：只能由构造方显式注入，核心没有任何默认实现。"""

    def send(self, request: TransportRequest) -> TransportResponse: ...


@dataclass(frozen=True)
class TransportRequest:
    session_id: str
    prompt: str
    max_output_tokens: int
    timeout_seconds: int


@dataclass(frozen=True)
class TransportResponse:
    declared_provider: str
    declared_model: str
    input_tokens: int
    output_tokens: int
    payload: dict[str, Any]


class InMemoryFakeTransport:
    """Phase 2A 内存假 Provider Transport：无网络、无子进程、无凭据。

    响应由构造方以确定性脚本注入；``calls`` 记录每次真实 send，测试用它
    断言「恰好一次发送」。
    """

    def __init__(self, responder: Callable[[TransportRequest], TransportResponse]):
        self._responder = responder
        self.calls: list[TransportRequest] = []

    def send(self, request: TransportRequest) -> TransportResponse:
        self.calls.append(request)
        return self._responder(request)


# ReceiptSource：由构造方注入的授权收据来源，键为 (run_id, node_id)。
ReceiptSource = Mapping[tuple[str, str], AuthorizationReceipt]


def _validate_response_payload(payload: Any) -> str | None:
    """固定 JSON schema：精确键集合、类型与字段长度。"""
    if not isinstance(payload, dict):
        return "invalid_response"
    if set(payload) != set(RESPONSE_PAYLOAD_SCHEMA):
        return "invalid_response"
    for field_name in sorted(RESPONSE_PAYLOAD_SCHEMA):
        value = payload[field_name]
        if not isinstance(value, str) or len(value) > MAX_RESPONSE_FIELD_CHARS:
            return "invalid_response"
    if len(canonical_json(payload).encode("utf-8")) > MAX_RESPONSE_BYTES:
        return "response_too_large"
    return None


class ControlledAgentAdapter:
    """单个受控 capability Agent 节点的完整发送控制面。

    显式关联协议（``EXECUTION_LINK_PROTOCOL``）：GraphRuntime 在分类
    inventory 可用时经 :meth:`bind_execution_link` 注入旁路关联；未接线
    时保持 Phase 2A 兼容语义（既有测试锁定），接线后 attach 先于
    ``ledger.reserve``，四类关联错误在 transport 前 fail closed。"""

    EXECUTION_LINK_PROTOCOL = "agent-execution-link-v1"

    def __init__(
        self,
        policy: AgentPolicy,
        ledger: AgentLedgerStore,
        receipts: ReceiptSource,
        transport: Transport,
        *,
        allowed_models: frozenset[tuple[str, str]] = PHASE2A_ALLOWED_MODELS,
        clock: Callable[[], datetime] | None = None,
        execution_link: AgentExecutionLink | None = None,
        adapter_metadata_digest: str | None = None,
    ):
        if (execution_link is None) != (adapter_metadata_digest is None):
            # 关联与其 digest 绑定必须同时提供；半配置等于身份可漂移。
            raise ValueError(
                "execution_link and adapter_metadata_digest must be set together"
            )
        self.policy = policy
        self.ledger = ledger
        self.receipts = receipts
        self.transport = transport
        self.allowed_models = allowed_models
        self._clock = clock or (lambda: datetime.now(UTC))
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        # Gate 1（§3.5）：唯一旁路关联。None = Phase 2A 未接线兼容路径；
        # 提供时在既有 ledger.reserve 前以 CAS attach，四类错误 fail closed。
        self.execution_link = execution_link
        self.adapter_metadata_digest = adapter_metadata_digest

    def bind_execution_link(
        self, link: AgentExecutionLink, metadata_digest: str
    ) -> None:
        """显式关联协议（GraphRuntime 接线入口）。

        未接线时绑定；同 digest 重复绑定幂等；与既有绑定冲突即拒绝
        （fail closed），绝不静默改写既有关联。"""
        if self.execution_link is not None:
            if self.adapter_metadata_digest == metadata_digest:
                return  # exact replay of the same binding
            raise ValueError("execution_link_conflict")
        if self.adapter_metadata_digest not in (None, metadata_digest):
            raise ValueError("execution_link_conflict")
        self.execution_link = link
        self.adapter_metadata_digest = metadata_digest

    # -- 观察 -------------------------------------------------------------

    def observe(self, session_id: str) -> dict[str, Any] | None:
        return self.ledger.observe(session_id)

    # -- 主流程 -------------------------------------------------------------

    def handler(self, request: NodeRequest) -> NodeResult:
        """Graph Runtime 节点处理器；任何失败都在 Transport 前或账本项目内。"""
        policy = self.policy
        flattened = flatten_inputs(request.inputs)
        if isinstance(flattened, str):
            return NodeResult(output={}, failure=f"input_conflict:{flattened}")
        input_bytes = len(canonical_json(flattened).encode("utf-8"))
        if input_bytes > policy.max_input_bytes:
            return NodeResult(output={}, failure="input_too_large")
        input_digest = digest_of(flattened)

        receipt = self.receipts.get((request.run_id, request.node_id))
        if receipt is None:
            return NodeResult(output={}, failure="authorization_missing")
        binding_error = validate_receipt_binding(
            receipt,
            policy,
            run_id=request.run_id,
            node_id=request.node_id,
            spec_digest=request.spec_digest,
            input_digest=input_digest,
            allowed_models=self.allowed_models,
            now=self._clock(),
        )
        if binding_error is not None:
            return NodeResult(output={}, failure=binding_error)

        session_id = receipt.session_id()
        if self.execution_link is not None:
            # §3.5：在既有 ledger.reserve 前把 session 以 CAS 附着到前置
            # reservation（execution_id 只来自可信 NodeRequest）。已配置
            # execution-link 时缺 execution identity 必须在 ledger.reserve
            # 与 transport 前 fail closed；只有未配置链接的 Phase 2A 兼容
            # 路径保留旧直调语义。link digest 由 attach 内部以 reservation
            # 行的权威 run/node/spec/input 字段计算。
            execution_id = request.execution_id
            if execution_id is None:
                return NodeResult(output={}, failure="execution_reservation_missing")
            assert self.adapter_metadata_digest is not None  # 构造时已成对校验
            attached = self.execution_link.attach(
                execution_id=execution_id,
                session_id=session_id,
                adapter_metadata_digest=self.adapter_metadata_digest,
                authorization_digest=receipt.authorization_digest,
            )
            if attached not in ATTACH_OK:
                return NodeResult(output={}, failure=attached)

        existing = self.ledger.get(session_id)
        if existing is not None:
            status = str(existing["status"])
            if status == STATUS_COMPLETED:
                # 崩溃/反馈恢复：确定性重放账本中的已完成结果，绝不重复发送。
                result_json = existing.get("result_json")
                if not isinstance(result_json, str):
                    return NodeResult(output={}, failure="ledger_result_missing")
                replayed = json.loads(result_json)
                return NodeResult(output=dict(replayed))
            if status == STATUS_FAILED:
                # 已消费额度（含 unknown_send）：禁止重发，交人工以新 run 处理。
                return NodeResult(
                    output={},
                    failure=f"session_consumed:{existing.get('error_category')}",
                )
            if status == STATUS_RESERVED:
                with self._inflight_lock:
                    inflight = session_id in self._inflight
                if inflight:
                    return NodeResult(output={}, failure="reservation_in_progress")
                # 进程中断留下的 reserved：视为 unknown_send，原子改写为 failed，
                # 额度不回收，绝不重发。
                self.ledger.fail(
                    session_id, error_category="unknown_send", request_sent="unknown"
                )
                return NodeResult(output={}, failure="unknown_send")

        if self.ledger.db_bytes() >= MAX_DB_BYTES:
            return NodeResult(output={}, failure="db_size_limit")

        if not policy.live_enabled and not isinstance(self.transport, InMemoryFakeTransport):
            # live_enabled=false 时只有内存假 Transport 可用；任何真实
            # Transport 在发送前失败。
            return NodeResult(output={}, failure="live_disabled")

        reservation = self.ledger.reserve(
            AgentCallRecord(
                session_id=session_id,
                run_id=request.run_id,
                node_id=request.node_id,
                spec_digest=request.spec_digest,
                input_digest=input_digest,
                adapter=policy.adapter,
                provider=policy.provider,
                model=policy.model,
                authorization_digest=receipt.authorization_digest,
                max_input_tokens=policy.max_input_tokens,
                max_output_tokens=policy.max_output_tokens,
            )
        )
        if reservation != "reserved":
            # 授权已消耗或并发竞争失败：零发送。
            return NodeResult(output={}, failure=reservation)

        with self._inflight_lock:
            self._inflight.add(session_id)
        try:
            started = time.monotonic()
            try:
                response = self.transport.send(
                    TransportRequest(
                        session_id=session_id,
                        prompt=canonical_json(flattened),
                        max_output_tokens=policy.max_output_tokens,
                        timeout_seconds=policy.timeout_seconds,
                    )
                )
            except TransportError:
                self.ledger.fail(
                    session_id, error_category="transport_error", request_sent="false"
                )
                return NodeResult(output={}, failure="transport_error")
            except Exception:
                # 非协议异常无法证明发送未发生：request_sent=unknown，禁止重发。
                self.ledger.fail(
                    session_id, error_category="transport_exception", request_sent="unknown"
                )
                return NodeResult(output={}, failure="transport_exception")
            elapsed = time.monotonic() - started
            if elapsed > policy.timeout_seconds:
                self.ledger.fail(session_id, error_category="timeout", request_sent="true")
                return NodeResult(output={}, failure="timeout")
            failure = self._validate_response(response)
            if failure is not None:
                category, request_sent = failure
                self.ledger.fail(
                    session_id, error_category=category, request_sent=request_sent
                )
                return NodeResult(output={}, failure=category)
            # 先把完成结果原子写入账本，再交回 Graph Runtime 提交 Checkpoint。
            completed = self.ledger.complete(
                session_id,
                declared_provider=response.declared_provider,
                declared_model=response.declared_model,
                actual_input_tokens=response.input_tokens,
                actual_output_tokens=response.output_tokens,
                result=response.payload,
                result_digest=digest_of(response.payload),
            )
            if not completed:
                # 账本行已被并发改写：结果不得使用，禁止重发。
                return NodeResult(output={}, failure="ledger_write_conflict")
            return NodeResult(output=dict(response.payload))
        finally:
            with self._inflight_lock:
                self._inflight.discard(session_id)

    def _validate_response(self, response: TransportResponse) -> tuple[str, RequestSent] | None:
        """响应身份、预算与固定 schema 校验；返回 (错误类别, request_sent)。"""
        if response.declared_provider != self.policy.provider:
            return ("provider_mismatch", "true")
        if response.declared_model != self.policy.model:
            return ("model_mismatch", "true")
        if (
            not isinstance(response.input_tokens, int)
            or isinstance(response.input_tokens, bool)
            or response.input_tokens < 0
            or response.input_tokens > self.policy.max_input_tokens
        ):
            return ("budget_exceeded", "true")
        if (
            not isinstance(response.output_tokens, int)
            or isinstance(response.output_tokens, bool)
            or response.output_tokens < 0
            or response.output_tokens > self.policy.max_output_tokens
        ):
            return ("budget_exceeded", "true")
        payload_error = _validate_response_payload(response.payload)
        if payload_error is not None:
            return (payload_error, "true")
        return None
