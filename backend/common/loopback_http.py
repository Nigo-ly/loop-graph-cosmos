"""G4 后端 loopback HTTP 公共安全逻辑（Design §11.2，additive only）。

只含无业务知识的安全并集：loopback peer、精确 Host、Content-Length
上限、UTF-8 JSON 解析（不判形态）、稳定无正文错误 envelope、
log_message 静默。各 server 保留 method/path/header/body schema 与
服务特有 envelope；control 的 trusted-origin 双模式、intent header、
keep-alive 防毒、400/400/413 分级与 projection 的 contract/db_mode/stale
envelope 全部留在各自本地层——本模块不内置任何业务字段或文案。

R25 P2-3：曾定义的 origin_allowed / parse_json_object 始终零调用
（control 的 Origin 双模式与 JSON 分级在本地层且语义更严格），已按
零语义删除移除，不弱化各 server 本地校验。
"""

from __future__ import annotations

import json
from typing import Any

# 与两 server 现有集合逐字一致（含 IPv4-mapped IPv6）。
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def is_loopback_peer(client_ip: str) -> bool:
    """仅 loopback peer；非 loopback 一律拒绝。"""
    return client_ip in LOOPBACK_CLIENTS


def host_header_allowed(headers: Any, server_address: Any) -> bool:
    """精确 Host：仅 127.0.0.1/localhost 配本服务端口。"""
    port = server_address[1]
    allowed = headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")
    return bool(allowed)


def read_bounded_body(
    headers: Any,
    rfile: Any,
    *,
    max_bytes: int,
    min_bytes: int = 0,
    connection: Any = None,
    timeout_seconds: float = 2.0,
) -> tuple[str, bytes | None]:
    """Read one unambiguous, bounded HTTP body exactly.

    Transfer-Encoding and duplicate Content-Length fields are rejected to
    avoid request-smuggling ambiguity.  A caller that supplies ``connection``
    also gets a finite partial-body deadline.  Stable status strings let each
    service retain its own public error envelope.
    """
    if headers.get("Transfer-Encoding") is not None:
        return "transfer_encoding", None
    values = headers.get_all("Content-Length") if hasattr(headers, "get_all") else None
    if values is None:
        raw = headers.get("Content-Length")
        values = [] if raw is None else [raw]
    if not values:
        return "missing", None
    if len(values) != 1:
        return "invalid", None
    raw = values[0]
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        return "invalid", None
    length = int(raw, 10)
    if length < min_bytes:
        return "invalid_size", None
    if length > max_bytes:
        return "too_large", None
    if connection is not None:
        connection.settimeout(timeout_seconds)
    try:
        body = rfile.read(length)
    except TimeoutError:
        return "timeout", None
    if len(body) != length:
        return "truncated", None
    return "ok", body


def parse_json(raw: bytes) -> tuple[bool, Any]:
    """UTF-8 JSON 解析（不判形态）：(True, value) 或 (False, None)。"""
    try:
        return True, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False, None


def close_without_response(handler: Any) -> None:
    """稳定无正文错误 envelope：不发送任何响应体并关闭连接——
    用于非 loopback peer 等绝不泄漏服务存在的拒绝场景。"""
    handler.close_connection = True


class QuietLogMixin:
    """log_message 静默（与两 server 现有实现逐字一致）。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return
