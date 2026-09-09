"""ReAct 主循环 — 统一入口，集成所有工程化模块。

用法:
    from common.loop import run_loop
    from common.llm import AnthropicLLM

    llm = AnthropicLLM()
    tracer = run_loop("查 DAU", tools, tool_impl, llm=llm)
    tracer.print_summary()
    tracer.save("trace.json")
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from common.budget import BudgetExceededError, GraduatedBudget, TokenBudget
from common.errors import call_tool_with_retry
from common.llm import AnthropicLLM, LLMBackend
from common.state import sliding_window_state, summarize_history
from common.tracer import LoopTracer


def _as_dict(block: Any) -> dict[str, Any]:
    """SDK content block → 可序列化 dict（使用 model_dump 保留完整结构）。"""
    if hasattr(block, "model_dump"):
        block = block.model_dump(exclude_unset=True)
    if not isinstance(block, dict):
        raise TypeError("SDK content block must serialize to an object")
    return dict(block)


def _call_sig(name: str, args: dict[str, Any]) -> str:
    return f"{name}({json.dumps(args, sort_keys=True, ensure_ascii=False)})"


def run_loop(
    user_message: str,
    tools: list[dict[str, Any]],
    tool_impl: Callable[[str, dict[str, Any]], str],
    *,
    llm: LLMBackend | None = None,
    model: str | None = None,
    max_iterations: int = 15,
    budget: TokenBudget | GraduatedBudget | None = None,
    state_strategy: str = "full",
    dedup_threshold: int = 2,
    tracer: LoopTracer | None = None,
) -> LoopTracer:
    """ReAct 循环 — 集成预算/状态/重试/可观测性。

    state_strategy: "full" | "window" | "summary"
    """
    if llm is None:
        llm = AnthropicLLM()
    if model is None:
        model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    if tracer is None:
        tracer = LoopTracer()

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    call_history: dict[str, int] = {}

    for i in range(1, max_iterations + 1):
        tracer.start_round()

        # ── 1. 预算检查（每轮开头） ──
        if budget is not None:
            try:
                budget.check()
            except BudgetExceededError as e:
                tracer.record(
                    iteration=i,
                    stop_reason="budget_exhausted",
                    model_output_text=str(e),
                    tool_calls=[],
                    input_tokens=0,
                    output_tokens=0,
                    tool_results=[],
                )
                break

        # ── 2. 状态管理 ──
        if i > 1:
            if state_strategy == "window":
                messages = sliding_window_state(messages)
            elif state_strategy == "summary":
                messages, overhead = summarize_history(messages, llm, model)
                if budget and overhead:
                    budget.consume(overhead)

        # ── 3. 分级预算提示 ──
        api_messages = list(messages)
        if isinstance(budget, GraduatedBudget):
            hint = budget.hint()
            if hint:
                api_messages.append({"role": "user", "content": hint})

        # ── 4. 调用 LLM ──
        resp = llm.create_message(
            model=model,
            max_tokens=1024,
            tools=tools,
            messages=api_messages,
        )

        if budget:
            budget.consume(resp.usage.total)

        # ── 5. 工具调用 ──
        if resp.has_tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": [_as_dict(b) for b in resp.raw_blocks],
                }
            )

            tool_results: list[str] = []
            for tc in resp.tool_calls:
                sig = _call_sig(tc.name, tc.input)
                call_history[sig] = call_history.get(sig, 0) + 1

                if call_history[sig] > dedup_threshold:
                    result = json.dumps(
                        {
                            "error": True,
                            "message": (
                                f"工具 [{sig}] 已重复 {call_history[sig]} 次且持续失败。"
                                "请换方式或直接基于已有数据给结论。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                else:
                    result = call_tool_with_retry(
                        lambda inp: tool_impl(inp["name"], inp["args"]),
                        {"name": tc.name, "args": tc.input},
                    )
                    if not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False)

                tool_results.append(result)

            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tc.id, "content": r}
                        for tc, r in zip(resp.tool_calls, tool_results)
                    ],
                }
            )

            tracer.record(
                iteration=i,
                stop_reason=resp.stop_reason,
                model_output_text=resp.content,
                tool_calls=[
                    {"id": tc.id, "name": tc.name, "input": tc.input, "result": r}
                    for tc, r in zip(resp.tool_calls, tool_results)
                ],
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                tool_results=tool_results,
            )
            continue

        # ── 6. 最终回复 ──
        tracer.record(
            iteration=i,
            stop_reason=resp.stop_reason,
            model_output_text=resp.content,
            tool_calls=[],
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            tool_results=[],
        )
        return tracer

    return tracer
