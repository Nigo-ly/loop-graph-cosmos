"""Frozen one-call GLM-4.7 adapter for the minimum-value fragment trial."""

from __future__ import annotations

import http.client
import json
import re
import ssl
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from fragment_loop.minimum_value import (
    DraftAdapterResult,
    ModelCallEvidence,
    PreSendFailureError,
)

PROVIDER_ID = "glm"
MODEL_ID = "glm-4.7"
TARGET_PROFILE = "phone-fragment-glm47-draft-v1"
OFFICIAL_HOST = "open.bigmodel.cn"
ENDPOINT_PATH = "/api/paas/v4/chat/completions"
KEYCHAIN_SERVICE = "p4a-gate2-r9-provider-glm47"
MAX_PROMPT_TOKENS = 2_000
MAX_COMPLETION_TOKENS = 512
MAX_REQUEST_BYTES = 6_000
MAX_RESPONSE_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = (
    "你只负责把用户主动选择的一条手机碎片整理成 JSON 草稿，不执行工具，不采取外部行动。"
    "只能依据用户正文；不得补写事实、来源、人物、日期或因果。"
    "输出必须是一个 JSON 对象，且只包含：title(string)、summary(string)、"
    "summary_evidence({marker,source_quote,transformation_note,note})、"
    "key_information(array，每项为{text,marker,source_quote,transformation_note,note})、"
    "uncertainties(string array，可为空列表，非空时每项必须是非空白字符串)、"
    "source_note(string)、suggested_tags(string array)、"
    "suggested_next_step(string)。marker 只允许 directly_supported、normalized_transform、"
    "grounded_inference、unverified_inference、conflicted_with_source。"
    "directly_supported 的 text 必须与用户正文中的 source_quote 逐字一致；"
    "normalized_transform 必须提供用户正文中的逐字 source_quote 和非空 transformation_note；"
    "summary 使用 normalized_transform 时 source_quote 必须是完整用户碎片原文；"
    "其他 marker 必须提供非空 note。"
    "没有外部来源时 source_note 必须精确为「来源：用户提供的原始碎片」。"
    "不确定或推断内容必须明确标记并写依据；不要输出 Markdown 代码围栏。"
)

CredentialReader = Callable[[], str]
Transport = Callable[[bytes, str], Mapping[str, Any]]


def build_request_body(payload: str) -> dict[str, Any]:
    return {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        "thinking": {"type": "disabled"},
        "stream": False,
        "max_tokens": MAX_COMPLETION_TOKENS,
        "response_format": {"type": "json_object"},
    }


def canonical_request_bytes(payload: str) -> bytes:
    body = json.dumps(
        build_request_body(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise PreSendFailureError("glm_request_too_large")
    return body


def selected_fragment_privacy_scan(text: str) -> tuple[str, ...]:
    patterns = {
        "bank_card": r"(?<!\d)\d{16,19}(?!\d)",
        "credential": r"(?i)(?:api[ _-]?key|token|secret|password)\s*[:=]",
        "email": r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "id_number": r"(?<!\d)\d{17}[\dXx](?!\d)",
        "phone_number": r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "private_path": r"(?:^|\s)/Users/[^\s]+",
        "url": r"https?://\S+",
    }
    return tuple(sorted(name for name, pattern in patterns.items() if re.search(pattern, text)))


def read_glm_keychain_credential() -> str:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PreSendFailureError("glm_credential_unavailable") from error
    credential = completed.stdout.strip() if completed.returncode == 0 else ""
    if not credential:
        raise PreSendFailureError("glm_credential_unavailable")
    return credential


def https_once_transport(body: bytes, credential: str) -> Mapping[str, Any]:
    connection = http.client.HTTPSConnection(
        OFFICIAL_HOST,
        443,
        timeout=TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "POST",
            ENDPOINT_PATH,
            body=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if response.status != 200:
            raise RuntimeError(f"glm_http_{response.status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("glm_response_too_large")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise RuntimeError("glm_response_not_object")
        return decoded
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("glm_response_invalid_json") from error
    finally:
        connection.close()


def _required_nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"glm_usage_invalid:{field}")
    return value


def _parse_response(response: Mapping[str, Any]) -> DraftAdapterResult:
    if response.get("model") != MODEL_ID:
        raise RuntimeError("glm_response_model_mismatch")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise RuntimeError("glm_choices_invalid")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise RuntimeError("glm_finish_reason_invalid")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("glm_content_missing")
    try:
        draft = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError("glm_content_invalid_json") from error
    if not isinstance(draft, dict):
        raise RuntimeError("glm_content_not_object")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise RuntimeError("glm_usage_missing")
    prompt_tokens = _required_nonnegative_int(usage.get("prompt_tokens"), "prompt_tokens")
    completion_tokens = _required_nonnegative_int(
        usage.get("completion_tokens"), "completion_tokens"
    )
    total_tokens = _required_nonnegative_int(usage.get("total_tokens"), "total_tokens")
    if (
        prompt_tokens > MAX_PROMPT_TOKENS
        or completion_tokens > MAX_COMPLETION_TOKENS
        or prompt_tokens + completion_tokens != total_tokens
    ):
        raise RuntimeError("glm_usage_out_of_bounds")
    return DraftAdapterResult(
        draft=draft,
        evidence=ModelCallEvidence(
            provider=PROVIDER_ID,
            response_model_claim=MODEL_ID,
            http_status=200,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_status="unknown",
        ),
    )


class Glm47DraftAdapter:
    def __init__(
        self,
        credential_reader: CredentialReader = read_glm_keychain_credential,
        *,
        transport: Transport = https_once_transport,
    ) -> None:
        self._credential_reader = credential_reader
        self._transport = transport

    def __call__(self, payload: str) -> DraftAdapterResult:
        body = canonical_request_bytes(payload)
        credential = self._credential_reader().strip()
        if not credential:
            raise PreSendFailureError("glm_credential_unavailable")
        try:
            response = self._transport(body, credential)
        finally:
            credential = ""
        return _parse_response(response)
