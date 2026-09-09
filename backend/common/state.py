"""三种上下文历史状态管理方案。

- full_history: 全量历史（对照组）
- trim_messages: 滑动窗口（保留初始请求 + 最近 N 轮，按轮裁剪不可拆散）
- summarize_history: 摘要压缩（额外 API 调用压缩早期历史为 ≤3 句话）
"""

from __future__ import annotations

from typing import Any

from common.llm import LLMBackend


def full_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """方案一：全量历史，不做任何裁剪。"""
    return messages


def trim_messages(
    messages: list[dict[str, Any]],
    keep_last_n_rounds: int = 3,
) -> list[dict[str, Any]]:
    """方案二：滑动窗口。

    保留 messages[0]（初始用户请求）+ 最近 keep_last_n_rounds 轮。
    每轮 = 1 assistant + 1 user，tool_use 和 tool_result 不可拆散。
    """
    if len(messages) <= 3:
        return messages

    initial = [messages[0]]
    tail = messages[1:]
    n_to_keep = keep_last_n_rounds * 2
    if len(tail) > n_to_keep:
        return initial + tail[-n_to_keep:]
    return initial + tail


def sliding_window_state(
    messages: list[dict[str, Any]],
    keep_last_n_rounds: int = 3,
) -> list[dict[str, Any]]:
    """滑动窗口策略入口（兼容旧接口）。"""
    return trim_messages(messages, keep_last_n_rounds)


def summarize_history(
    messages: list[dict[str, Any]],
    llm: LLMBackend,
    model: str,
) -> tuple[list[dict[str, Any]], int]:
    """方案三：摘要压缩。

    把初始请求 + 最近一轮之外的历史，用一次 LLM 调用压缩成摘要。
    返回 (新消息列表, 摘要消耗的 token 数)。
    """
    if len(messages) <= 4:
        return messages, 0

    initial = [messages[0]]
    to_summarize = messages[1:-2]
    recent = messages[-2:]

    history_text_parts: list[str] = []
    for m in to_summarize:
        role = m["role"]
        c = m.get("content", "")
        if isinstance(c, list):
            texts = [
                b.get("text", "")
                for b in c
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            c = " | ".join(texts) if texts else str(c)[:200]
        else:
            c = str(c)[:200]
        history_text_parts.append(f"[{role}] {c}")

    summary_prompt = (
        "请将以下 AI 助手与用户的工具调用对话历史压缩成 3 句话以内的中文摘要，"
        "保留：查询了哪些指标、结果如何、关键决策。只输出摘要，不要多余内容。\n\n"
        + "\n".join(history_text_parts)
    )

    resp = llm.create_message(
        model=model,
        max_tokens=300,
        tools=[],
        messages=[{"role": "user", "content": summary_prompt}],
    )

    summary = resp.content or str(history_text_parts)[:200]
    overhead = resp.usage.total

    return initial + [
        {"role": "user", "content": f"[历史摘要] {summary}"}
    ] + recent, overhead
