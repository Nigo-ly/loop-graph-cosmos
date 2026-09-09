"""受控 Agent Policy 与授权收据（Graph Phase 2A，冻结切片）。

``AgentPolicy`` 是由构造方注入的不可变预算与身份契约：精确的 adapter、
provider、model、能力、`max_calls=1`、输入/输出 token 上限、最大输入字节、
超时与 `live_enabled`。Phase 2A 固定 `live_enabled=False`，核心不提供任何
HTTP、Shell、动态 import、命令模板或凭据读取。

``AuthorizationReceipt`` 是一次性授权收据：它绑定 `run_id`、`node_id`、
`spec_digest`、规范化输入摘要、adapter、provider、model、预算与授权摘要。
任何字段漂移都会形成新的 session identity，旧同意与旧结果不能拼接。收据只
保存授权短语的 SHA-256 摘要，短语本身从不进入账本、观察投影或 DOM。
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

from graph_runtime.spec import canonical_json

RECEIPT_SCHEMA = "graph-agent-authorization-v1"
SESSION_SCHEMA = "graph-agent-session-v1"

MAX_TEXT_FIELD = 128
MAX_PHRASE = 256


class AgentPolicyError(ValueError):
    """Fail-closed policy/receipt rejection with a stable machine code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise AgentPolicyError(code)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _text_field(value: object, *, what: str) -> str:
    """NFC 规范化、无控制字符、无路径语义的有界文本字段。"""
    if not isinstance(value, str):
        _fail(f"invalid_{what}")
    if not value or len(value) > MAX_TEXT_FIELD:
        _fail(f"invalid_{what}")
    if unicodedata.normalize("NFC", value) != value:
        _fail(f"invalid_{what}")
    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 or codepoint == 0x7F:
            _fail(f"invalid_{what}")
    if "/" in value or "\\" in value:
        _fail(f"invalid_{what}")
    return value


def _positive_int(value: object, *, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail(f"invalid_{what}")
    return value


def _iso_utc(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        _fail(f"invalid_{what}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail(f"invalid_{what}")
    if parsed.tzinfo is None:
        _fail(f"invalid_{what}")
    return value


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def authorization_digest_of(phrase: str, *, authorized_by: str) -> str:
    """授权短语的 SHA-256 摘要；短语本体从不持久化。"""
    if not isinstance(phrase, str) or not phrase or len(phrase) > MAX_PHRASE:
        _fail("invalid_authorization_phrase")
    return hashlib.sha256(
        canonical_json([RECEIPT_SCHEMA, authorized_by, phrase]).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class AgentPolicy:
    """不可变受控 Agent 预算与身份契约；只由构造方注入。"""

    adapter: str
    provider: str
    model: str
    capability: str
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_input_bytes: int
    timeout_seconds: int
    live_enabled: bool

    def __post_init__(self) -> None:
        _text_field(self.adapter, what="adapter")
        _text_field(self.provider, what="provider")
        _text_field(self.model, what="model")
        _text_field(self.capability, what="capability")
        if self.max_calls != 1:
            # Phase 2A 冻结 max_calls=1；一次授权只覆盖一次发送。
            _fail("invalid_max_calls")
        _positive_int(self.max_input_tokens, what="max_input_tokens")
        _positive_int(self.max_output_tokens, what="max_output_tokens")
        _positive_int(self.max_input_bytes, what="max_input_bytes")
        _positive_int(self.timeout_seconds, what="timeout_seconds")
        if not isinstance(self.live_enabled, bool):
            _fail("invalid_live_enabled")

    def budget_key(self) -> dict[str, Any]:
        """参与 session identity 的预算字段，一个权威排序实现。"""
        return {
            "max_calls": self.max_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_input_bytes": self.max_input_bytes,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class AuthorizationReceipt:
    """一次性授权收据；缺字段、形状漂移在构造时即失败。"""

    run_id: str
    node_id: str
    spec_digest: str
    input_digest: str
    adapter: str
    provider: str
    model: str
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_input_bytes: int
    authorization_digest: str
    authorized_by: str
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        _text_field(self.run_id, what="run_id")
        _text_field(self.node_id, what="node_id")
        if not _is_sha256(self.spec_digest):
            _fail("invalid_spec_digest")
        if not _is_sha256(self.input_digest):
            _fail("invalid_input_digest")
        _text_field(self.adapter, what="adapter")
        _text_field(self.provider, what="provider")
        _text_field(self.model, what="model")
        if self.max_calls != 1:
            _fail("invalid_max_calls")
        _positive_int(self.max_input_tokens, what="max_input_tokens")
        _positive_int(self.max_output_tokens, what="max_output_tokens")
        _positive_int(self.max_input_bytes, what="max_input_bytes")
        if not _is_sha256(self.authorization_digest):
            _fail("invalid_authorization_digest")
        _text_field(self.authorized_by, what="authorized_by")
        _iso_utc(self.issued_at, what="issued_at")
        _iso_utc(self.expires_at, what="expires_at")
        if _parse_iso(self.expires_at) <= _parse_iso(self.issued_at):
            _fail("invalid_validity_window")

    def session_id(self) -> str:
        """唯一 session identity：任何绑定字段漂移都是全新会话。

        旧 run、旧节点、旧规范、旧输入、旧预算或旧授权的结果与同意
        永远不能拼接到一个新 identity 上。
        """
        return hashlib.sha256(
            canonical_json(
                [
                    SESSION_SCHEMA,
                    self.run_id,
                    self.node_id,
                    self.spec_digest,
                    self.input_digest,
                    self.adapter,
                    self.provider,
                    self.model,
                    self.max_calls,
                    self.max_input_tokens,
                    self.max_output_tokens,
                    self.max_input_bytes,
                    self.authorization_digest,
                ]
            ).encode("utf-8")
        ).hexdigest()


def validate_receipt_binding(
    receipt: AuthorizationReceipt,
    policy: AgentPolicy,
    *,
    run_id: str,
    node_id: str,
    spec_digest: str,
    input_digest: str,
    allowed_models: frozenset[tuple[str, str]],
    now: datetime,
) -> str | None:
    """Transport 前的完整绑定校验；返回稳定错误码或 None。

    缺字段与形状错误在构造期已失败；这里校验时效、身份漂移、预算
    漂移、未知 provider/model 与授权人。任何不匹配都在发送前失败。
    """
    if receipt.authorized_by != "nigo":
        return "invalid_authorized_by"
    if now.astimezone(UTC) >= _parse_iso(receipt.expires_at):
        return "authorization_expired"
    if now.astimezone(UTC) < _parse_iso(receipt.issued_at):
        return "authorization_not_yet_valid"
    if receipt.run_id != run_id:
        return "authorization_mismatch:run_id"
    if receipt.node_id != node_id:
        return "authorization_mismatch:node_id"
    if receipt.spec_digest != spec_digest:
        return "authorization_mismatch:spec_digest"
    if receipt.input_digest != input_digest:
        return "authorization_mismatch:input_digest"
    if receipt.adapter != policy.adapter:
        return "authorization_mismatch:adapter"
    if receipt.provider != policy.provider:
        return "authorization_mismatch:provider"
    if receipt.model != policy.model:
        return "authorization_mismatch:model"
    budget = policy.budget_key()
    for field_name in ("max_calls", "max_input_tokens", "max_output_tokens", "max_input_bytes"):
        if getattr(receipt, field_name) != budget[field_name]:
            return f"authorization_mismatch:{field_name}"
    if (receipt.provider, receipt.model) not in allowed_models:
        return "unknown_provider_model"
    return None
