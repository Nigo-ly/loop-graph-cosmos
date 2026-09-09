"""LLM 抽象封装 — AnthropicLLM（生产）+ FakeLLM（测试）。"""

from __future__ import annotations

import os
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from common.types import LLMResponse, TokenUsage, ToolCall

load_dotenv()


class LLMBackend:
    """LLM 后端抽象基类。"""

    def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        thinking_disabled: bool = True,
    ) -> LLMResponse:
        raise NotImplementedError


class AnthropicLLM(LLMBackend):
    """通过 Anthropic SDK 调用 Claude 或 DeepSeek Anthropic 兼容接口。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        base_url = base_url or os.getenv("ANTHROPIC_BASE_URL")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY 未设置")
        kw: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kw["base_url"] = base_url
        self._client = Anthropic(**kw)

    def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        thinking_disabled: bool = True,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if thinking_disabled:
            kwargs["thinking"] = {"type": "disabled"}
        if tools:
            kwargs["tools"] = tools

        resp = self._client.messages.create(**kwargs)

        model_text = ""
        tool_calls: list[ToolCall] = []
        raw_blocks: list[Any] = []
        for b in resp.content:
            raw_blocks.append(b)
            t = getattr(b, "type", "")
            if t == "text":
                model_text += b.text
            elif t == "tool_use":
                tool_calls.append(ToolCall(id=b.id, name=b.name, input=b.input))

        usage = TokenUsage()
        if resp.usage:
            usage = TokenUsage(
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )

        return LLMResponse(
            stop_reason=resp.stop_reason,
            content=model_text,
            tool_calls=tool_calls,
            usage=usage,
            raw_blocks=raw_blocks,
        )


class FakeLLM(LLMBackend):
    """测试用假 LLM — 返回预设响应，不产生 API 费用。

    Usage:
        fake = FakeLLM(responses=[LLMResponse(...), LLMResponse(...)])
        # 每次 create_message 依次弹出一个预设响应
    """

    def __init__(self, responses: list[LLMResponse] | None = None):
        self.responses = responses or []
        self._idx = 0
        self.calls: list[dict[str, Any]] = []  # 记录每次调用参数

    def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        thinking_disabled: bool = True,
    ) -> LLMResponse:
        self.calls.append({
            "model": model,
            "max_tokens": max_tokens,
            "tools": tools,
            "messages": messages,
        })
        if self._idx < len(self.responses):
            r = self.responses[self._idx]
            self._idx += 1
            return r
        # 默认：直接结束
        return LLMResponse(
            stop_reason="end_turn",
            content="[FakeLLM] no more responses",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
