"""fragment-pilot-v1 的休眠生产接缝（GRAPH-PILOT-ENTRY-BRIDGE-V1 rev2 §2/§3/§5）。

- 冻结 Prompt 的真实发送路径：请求体 = system Prompt（完整冻结）+ user
  Prompt（冻结模板 + canonical candidate JSON），绝不发送通用 flattened inputs。
- 独立 Pilot Live Transport：显式开关默认关闭；关闭时零 Keychain、零网络。
  启用后也必须先有有效逐 Run Receipt、价格门通过、账本原子预留，才允许
  读取精确凭据并发送。
- 通用安全基础复用 Phase2B 已验收件（结构化价格表解析、价格页抓取），
  但 Pilot 身份独立：PROVIDER `deepseek` + MODEL `deepseek-v4-pro` 逐字一致。
"""

from __future__ import annotations

import hashlib
import json
import ssl
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from graph_runtime.agent_adapter import TransportError, TransportResponse
from graph_runtime.agent_live_pilot import (
    CNY_PER_USD_CEILING,
    PEAK_MULTIPLIER,
    _default_connection_factory,
    _default_resolver,
    extract_model_prices_usd,
    fetch_pricing_page,
)
from graph_runtime.spec import canonical_json

# Pilot 身份（rev2 §8：Receipt、Transport 声明、Policy 三处逐字一致）。
PROVIDER = "deepseek"
MODEL = "deepseek-v4-pro"
OFFICIAL_HOST = "api.deepseek.com"
OFFICIAL_PATH = "/v1/chat/completions"
KEYCHAIN_SERVICE = "graph-pilot-v1-provider-deepseek"

MAX_INPUT_TOKENS = 2000
MAX_OUTPUT_TOKENS = 1000
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 10
CALL_WALL_CLOCK_SECONDS = 120
# 决定处理总预算（rev2 §6）：两次调用各 ≤120s，整个决定处理 ≤300s。
DECIDE_TOTAL_BUDGET_SECONDS = 300

# 价格门：单次最坏 ≤¥1（总上限 ¥2 = 单次 ¥1 × 最多 2 次）。
MAX_COST_CNY_PER_CALL = 1.0
MAX_TOTAL_CALLS = 2


class PilotLiveError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# 冻结 Prompt 请求体
# ---------------------------------------------------------------------------


def build_user_prompt(template: str, candidate_json: str) -> str:
    """user Prompt = 冻结模板 + canonical candidate JSON（唯一占位符替换）。"""
    if template.count("{candidate_json}") != 1:
        raise PilotLiveError("prompt_template_drift")
    return template.replace("{candidate_json}", candidate_json)


def build_pilot_request_body(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
) -> bytes:
    """冻结请求体：精确模型、system+user 双消息、JSON Object、不流式、无工具。"""
    body = canonical_json(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_output_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
            "thinking": {"type": "disabled"},
        }
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise TransportError("request_too_large")
    return body


# ---------------------------------------------------------------------------
# Live Transport（生产接缝；离线测试注入假连接）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotSendRequest:
    """一次 Pilot 发送的完整材料；Prompt 逐字进入，绝不退化为通用输入。"""

    session_id: str
    system_prompt: str
    user_prompt: str
    candidate_json: str
    input_digest: str
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class PilotSendOutcome:
    """一次发送尝试的完整可归因结果；response 为 None 时必有错误类别。"""

    request_sent: str  # "true" | "false" | "unknown"
    response: TransportResponse | None
    error_category: str | None
    http_status: int | None = None


@dataclass(frozen=True)
class DeepSeekRawResponse:
    """Verified chat.completions envelope before product-schema validation."""

    declared_model: str
    input_tokens: int
    output_tokens: int
    content: str


@dataclass(frozen=True)
class DeepSeekRawOutcome:
    request_sent: str
    response: DeepSeekRawResponse | None
    error_category: str | None
    http_status: int | None = None


def _parse_deepseek_envelope(decoded: Any) -> DeepSeekRawResponse:
    if not isinstance(decoded, dict):
        raise TransportError("envelope_not_object")
    model = decoded.get("model")
    usage = decoded.get("usage")
    choices = decoded.get("choices")
    if not isinstance(model, str) or not isinstance(usage, dict):
        raise TransportError("envelope_invalid")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise TransportError("envelope_invalid")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if (
        not isinstance(prompt_tokens, int)
        or isinstance(prompt_tokens, bool)
        or prompt_tokens < 0
        or not isinstance(completion_tokens, int)
        or isinstance(completion_tokens, bool)
        or completion_tokens < 0
    ):
        raise TransportError("envelope_invalid")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise TransportError("envelope_invalid")
    return DeepSeekRawResponse(
        declared_model=model,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        content=message["content"],
    )


def _parse_pilot_envelope(decoded: Any) -> TransportResponse:
    """DeepSeek chat.completions 信封：模型声明、usage 与 content JSON。

    结构失败归入 invalid_response；身份逐字校验由调用方完成。
    """
    raw = _parse_deepseek_envelope(decoded)
    try:
        payload = json.loads(raw.content)
    except json.JSONDecodeError as error:
        raise TransportError("envelope_content_not_json") from error
    if not isinstance(payload, dict):
        raise TransportError("envelope_content_not_json")
    return TransportResponse(
        declared_provider=PROVIDER,
        declared_model=raw.declared_model,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
        payload=payload,
    )


class PilotLiveTransport:
    """生产 HTTPS Transport：一个请求、零重试、不 redirect、不代理、不流式。

    host/path 是模块常量而非参数；DNS 解析与 TLS 对端地址交叉核验；响应
    超过 64 KiB、非 200、重定向、无法解析或内容不是 JSON Object 都以固定
    类别拒绝。默认装配中它永远不会被触达（live_enabled=False 先失败）。
    """

    def __init__(
        self,
        *,
        resolver: Callable[[str], frozenset[str]] = _default_resolver,
        connection_factory: Callable[
            [str, int, float, ssl.SSLContext], Any
        ] = _default_connection_factory,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._monotonic = monotonic

    def send_once(self, request: PilotSendRequest, credential: str) -> PilotSendOutcome:
        if not isinstance(credential, str) or not credential:
            raise TransportError("credential_unavailable")
        body = build_pilot_request_body(
            model=MODEL,
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            max_output_tokens=request.max_output_tokens,
        )
        outcome = self.send_canonical_once(
            body, credential, timeout_seconds=request.timeout_seconds
        )
        if outcome.response is None:
            return PilotSendOutcome(
                outcome.request_sent,
                None,
                outcome.error_category,
                outcome.http_status,
            )
        try:
            payload = json.loads(outcome.response.content)
        except json.JSONDecodeError:
            return PilotSendOutcome("true", None, "invalid_response", outcome.http_status)
        if not isinstance(payload, dict):
            return PilotSendOutcome("true", None, "invalid_response", outcome.http_status)
        return PilotSendOutcome(
            "true",
            TransportResponse(
                declared_provider=PROVIDER,
                declared_model=outcome.response.declared_model,
                input_tokens=outcome.response.input_tokens,
                output_tokens=outcome.response.output_tokens,
                payload=payload,
            ),
            None,
            outcome.http_status,
        )

    def send_canonical_once(
        self, body: bytes, credential: str, *, timeout_seconds: float
    ) -> DeepSeekRawOutcome:
        """Send already-canonical bytes through the shared verified HTTPS boundary."""
        if not isinstance(body, bytes) or not body or len(body) > MAX_REQUEST_BYTES:
            raise TransportError("request_too_large")
        if not isinstance(credential, str) or not credential:
            raise TransportError("credential_unavailable")
        total_budget = float(timeout_seconds)
        try:
            addresses = self._resolver(OFFICIAL_HOST)
        except Exception:
            return DeepSeekRawOutcome("false", None, "transport_error")
        if not addresses:
            return DeepSeekRawOutcome("false", None, "transport_error")
        context = ssl.create_default_context()
        started = self._monotonic()

        def remaining() -> float:
            return total_budget - (self._monotonic() - started)

        connection = self._connection_factory(
            OFFICIAL_HOST, 443, float(CONNECT_TIMEOUT_SECONDS), context
        )
        import http.client as _http

        try:
            try:
                connection.connect()
            except ssl.SSLError:
                return DeepSeekRawOutcome("false", None, "transport_error")
            except (OSError, _http.HTTPException):
                # 连接期异常无法证明发送未发生：unknown。
                return DeepSeekRawOutcome("unknown", None, "transport_exception")
            sock = connection.sock
            peer = sock.getpeername()[0] if sock is not None else None
            if peer not in addresses:
                return DeepSeekRawOutcome("false", None, "transport_error")
            if remaining() <= 0:
                return DeepSeekRawOutcome("false", None, "timeout")
            if sock is not None:
                sock.settimeout(remaining())
            try:
                connection.request(
                    "POST",
                    OFFICIAL_PATH,
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {credential}",
                    },
                )
            except (OSError, _http.HTTPException):
                return DeepSeekRawOutcome("unknown", None, "transport_exception")
            if remaining() <= 0:
                return DeepSeekRawOutcome("true", None, "timeout")
            if sock is not None:
                sock.settimeout(remaining())
            try:
                response = connection.getresponse()
            except (OSError, _http.HTTPException):
                # 请求已发出但响应未知：发送事实成立。
                return DeepSeekRawOutcome("true", None, "transport_exception")
            status = int(response.status)
            if status in (301, 302, 303, 307, 308):
                return DeepSeekRawOutcome("true", None, "invalid_response", status)
            if status != 200:
                return DeepSeekRawOutcome("true", None, "invalid_response", status)
            if remaining() <= 0:
                return DeepSeekRawOutcome("true", None, "timeout", status)
            if sock is not None:
                sock.settimeout(remaining())
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, _http.HTTPException):
                return DeepSeekRawOutcome("true", None, "transport_exception", status)
            if len(raw) > MAX_RESPONSE_BYTES:
                return DeepSeekRawOutcome("true", None, "response_too_large", status)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return DeepSeekRawOutcome("true", None, "invalid_response", status)
            try:
                parsed = _parse_deepseek_envelope(decoded)
            except TransportError:
                return DeepSeekRawOutcome("true", None, "invalid_response", status)
            if self._monotonic() - started > total_budget:
                return DeepSeekRawOutcome("true", None, "timeout", status)
            return DeepSeekRawOutcome("true", parsed, None, status)
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# 凭据读取（精确 service；只在账本原子预留之后允许调用）
# ---------------------------------------------------------------------------


def production_credential_reader(
    service: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """生产 Keychain 边界：只读 Pilot 精确 service，凭据只在内存。"""
    if service != KEYCHAIN_SERVICE:
        raise PilotLiveError("credential_service_rejected")
    try:
        completed = run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PilotLiveError("credential_unavailable") from error
    if completed.returncode != 0:
        raise PilotLiveError("credential_unavailable")
    credential = completed.stdout.removesuffix("\n")
    if not isinstance(credential, str) or not credential:
        raise PilotLiveError("credential_unavailable")
    return credential


def forbidden_credential_reader(_service: str) -> str:
    """离线/默认装配接缝：任何凭据读取企图都 fail-closed。"""
    raise PilotLiveError("credential_read_forbidden")


# ---------------------------------------------------------------------------
# 价格门（结构化美元价格表 + 保守汇率上界；rev2 §4 严格验证）
# ---------------------------------------------------------------------------


def evaluate_pilot_price_gate(
    page_text: str,
    *,
    fetched_at: str,
    page_url: str,
) -> dict[str, Any]:
    """价格门核算与快照：三类价格严格解析 → 最坏单价 → 单次最坏人民币成本。

    单次最坏 = 最坏美元单价 × (输入+输出上限) × 高峰两倍 × 汇率上界；
    单次 < ¥1 才 within_budget（总上限 ¥2 = 单次 ¥1 × 最多 2 次）。
    快照时间、模型列、三类价格、金额与总成本全部严格验证；任何歧义
    在 extract_model_prices_usd 内 fail-closed。
    """
    from datetime import datetime as _datetime

    try:
        parsed_at = _datetime.fromisoformat(fetched_at)
    except ValueError as error:
        raise PilotLiveError("price_snapshot_invalid") from error
    if parsed_at.tzinfo is None:
        raise PilotLiveError("price_snapshot_invalid")
    try:
        prices = extract_model_prices_usd(page_text, model=MODEL)
    except Exception as error:
        # 归一化 Phase2B 解析层的稳定错误码为 Pilot 失败关闭语义。
        raise PilotLiveError(str(getattr(error, "code", "price_unavailable"))) from error
    worst_price = max(prices.values())
    per_call_usd = (
        worst_price * (MAX_INPUT_TOKENS + MAX_OUTPUT_TOKENS) / 1_000_000 * PEAK_MULTIPLIER
    )
    per_call_cny = per_call_usd * CNY_PER_USD_CEILING
    total_cap = MAX_COST_CNY_PER_CALL * MAX_TOTAL_CALLS
    return {
        "schema": "graph-pilot-v1-price-snapshot-v1",
        "fetched_at_utc": fetched_at,
        "page_url": page_url,
        "page_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
        "model": MODEL,
        "prices_usd_per_million": dict(prices),
        "worst_price_usd_per_million": worst_price,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "peak_multiplier": PEAK_MULTIPLIER,
        "cny_per_usd_ceiling": CNY_PER_USD_CEILING,
        "per_call_cny": per_call_cny,
        "max_cost_cny_per_call": MAX_COST_CNY_PER_CALL,
        "max_total_calls": MAX_TOTAL_CALLS,
        "total_cost_cap_cny": total_cap,
        "within_budget": per_call_cny < MAX_COST_CNY_PER_CALL,
    }


def verify_pilot_price_snapshot(snapshot: object, *, now: datetime | None = None) -> dict[str, Any]:
    """签发前最终价格门（rev3 §2）：一切派生结论都由服务端重算。

    - page_url 必须逐字等于冻结官方价格页；
    - fetched_at 必须带时区、不来自未来、年龄 ≤24h（可注入 UTC 时钟）；
    - 模型与三类美元价格精确验证；
    - 最坏成本按冻结上限重算：最坏单价 × (2000+1000) × 高峰两倍 ×
      汇率上界；快照自带的 worst_price / per_call_cny / within_budget
      等派生字段一律不信任、不参与判定。
    """
    from graph_runtime.agent_live_pilot import PRICING_PAGE_URL

    if not isinstance(snapshot, dict):
        raise PilotLiveError("price_unavailable")
    if snapshot.get("schema") != "graph-pilot-v1-price-snapshot-v1":
        raise PilotLiveError("price_unavailable")
    if snapshot.get("page_url") != PRICING_PAGE_URL:
        raise PilotLiveError("price_unavailable")
    if snapshot.get("model") != MODEL:
        raise PilotLiveError("price_unavailable")
    fetched_at_raw = snapshot.get("fetched_at_utc")
    if not isinstance(fetched_at_raw, str):
        raise PilotLiveError("price_unavailable")
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError as error:
        raise PilotLiveError("price_unavailable") from error
    if fetched_at.tzinfo is None:
        raise PilotLiveError("price_unavailable")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    fetched_utc = fetched_at.astimezone(UTC)
    if fetched_utc > current:
        raise PilotLiveError("price_snapshot_future")
    if current - fetched_utc > timedelta(hours=24):
        raise PilotLiveError("price_snapshot_stale")
    prices = snapshot.get("prices_usd_per_million")
    if (
        not isinstance(prices, dict)
        or set(prices) != {"cache_hit", "cache_miss", "output"}
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 < float(value) < 1000
            for value in prices.values()
        )
    ):
        raise PilotLiveError("price_unavailable")
    # rev4 §3：全部冻结字段必须存在且与冻结/重算值逐字一致。
    page_sha = snapshot.get("page_sha256")
    if not (
        isinstance(page_sha, str)
        and len(page_sha) == 64
        and all(character in "0123456789abcdef" for character in page_sha)
    ):
        raise PilotLiveError("price_snapshot_tampered")
    frozen_expectations = {
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "peak_multiplier": PEAK_MULTIPLIER,
        "cny_per_usd_ceiling": CNY_PER_USD_CEILING,
        "max_cost_cny_per_call": MAX_COST_CNY_PER_CALL,
        "max_total_calls": MAX_TOTAL_CALLS,
        "total_cost_cap_cny": MAX_COST_CNY_PER_CALL * MAX_TOTAL_CALLS,
    }
    for key, expected in frozen_expectations.items():
        if snapshot.get(key) != expected:
            raise PilotLiveError("price_snapshot_tampered")
    # 服务端重算最坏成本；快照派生字段必须存在且与重算结果逐字一致——
    # 不一致即派生篡改（失败关闭），一致也只采信重算值。
    clean_prices = {key: float(value) for key, value in prices.items()}
    worst_price = max(clean_prices.values())
    per_call_cny = (
        worst_price
        * (MAX_INPUT_TOKENS + MAX_OUTPUT_TOKENS)
        / 1_000_000
        * PEAK_MULTIPLIER
        * CNY_PER_USD_CEILING
    )
    if (
        snapshot.get("worst_price_usd_per_million") != worst_price
        or snapshot.get("per_call_cny") != per_call_cny
        or snapshot.get("within_budget") is not True
    ):
        raise PilotLiveError("price_snapshot_tampered")
    if per_call_cny >= MAX_COST_CNY_PER_CALL:
        raise PilotLiveError("cost_cap_exceeded")
    return {
        "schema": "graph-pilot-v1-price-snapshot-v1",
        "fetched_at_utc": fetched_at_raw,
        "page_url": PRICING_PAGE_URL,
        "page_sha256": snapshot.get("page_sha256"),
        "model": MODEL,
        "prices_usd_per_million": clean_prices,
        "worst_price_usd_per_million": worst_price,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "peak_multiplier": PEAK_MULTIPLIER,
        "cny_per_usd_ceiling": CNY_PER_USD_CEILING,
        "per_call_cny": per_call_cny,
        "max_cost_cny_per_call": MAX_COST_CNY_PER_CALL,
        "max_total_calls": MAX_TOTAL_CALLS,
        "total_cost_cap_cny": MAX_COST_CNY_PER_CALL * MAX_TOTAL_CALLS,
        "within_budget": True,
    }


def live_pilot_price_reader() -> dict[str, Any]:
    """生产价格门读取：抓取官方价格页并核算快照；任何异常失败关闭。

    这是整个 Pilot 中唯一允许的外网动作，只在用户点击「生成/重新签发
    授权单」时由服务端触发；本轮绝不执行。
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from graph_runtime.agent_live_pilot import PRICING_PAGE_URL

    page_text = fetch_pricing_page()
    return evaluate_pilot_price_gate(
        page_text,
        fetched_at=_datetime.now(_UTC).isoformat(timespec="seconds"),
        page_url=PRICING_PAGE_URL,
    )


__all__ = [
    "PROVIDER",
    "MODEL",
    "OFFICIAL_HOST",
    "OFFICIAL_PATH",
    "KEYCHAIN_SERVICE",
    "CALL_WALL_CLOCK_SECONDS",
    "DECIDE_TOTAL_BUDGET_SECONDS",
    "MAX_TOTAL_CALLS",
    "MAX_COST_CNY_PER_CALL",
    "PilotLiveError",
    "PilotLiveTransport",
    "PilotSendRequest",
    "PilotSendOutcome",
    "DeepSeekRawResponse",
    "DeepSeekRawOutcome",
    "build_user_prompt",
    "build_pilot_request_body",
    "production_credential_reader",
    "forbidden_credential_reader",
    "evaluate_pilot_price_gate",
    "verify_pilot_price_snapshot",
    "live_pilot_price_reader",
]
