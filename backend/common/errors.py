"""工具调用错误恢复 — 指数退避重试 + 结构化错误返回。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    SAFETY = "safety"
    SIDE_EFFECT_UNKNOWN = "side_effect_unknown"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ErrorDecision:
    category: ErrorCategory
    retryable: bool
    resume_condition: str


def classify_error(error: Exception) -> ErrorDecision:
    """Return a stable recovery decision instead of handler-specific guesswork."""
    if isinstance(error, (TimeoutError, ConnectionError)):
        return ErrorDecision(
            ErrorCategory.TRANSIENT,
            True,
            "Retry with bounded exponential backoff",
        )
    if isinstance(error, PermissionError):
        return ErrorDecision(
            ErrorCategory.SAFETY,
            False,
            "Obtain explicit authority or reduce the requested side effect",
        )
    if isinstance(error, (FileNotFoundError, ValueError, KeyError)):
        return ErrorDecision(
            ErrorCategory.PERMANENT,
            False,
            "Correct the input, source, or LoopSpec before resuming",
        )
    return ErrorDecision(
        ErrorCategory.INTERNAL,
        False,
        "Fix or replace the failed handler",
    )


def call_tool_with_retry(
    tool_func: Callable[[dict[str, Any]], Any],
    tool_input: dict[str, Any],
    max_retries: int = 2,
) -> Any:
    """执行工具调用，失败自动重试（指数退避），最终兜底返回结构化错误。

    重试间隔: 0.5s → 1s → 2s → ...
    重试耗尽后不抛异常，返回 {"error": True, "message": "..."} 喂给模型。
    """
    last_error: str | None = None

    for attempt in range(1, max_retries + 2):  # 1 次初调 + max_retries 次重试
        try:
            return tool_func(tool_input)
        except Exception as e:
            last_error = str(e)
            if attempt <= max_retries:
                wait = 0.5 * (2 ** (attempt - 1))
                time.sleep(wait)
            # 最后一轮不 sleep，直接 fall through

    return {
        "error": True,
        "message": f"工具调用失败，已重试 {max_retries} 次。最后错误: {last_error}",
    }
