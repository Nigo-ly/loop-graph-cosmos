"""Loop 可观测性 — LoopTracer 记录每轮完整执行轨迹。

提供:
  print_summary() — 控制台打印简洁轨迹总览
  save(path)       — 导出完整 trace.json
"""

import json
import time
from datetime import datetime
from typing import Any


class LoopTracer:
    """每轮记录: 轮次、时间戳、stop_reason、工具调用（含返回）、token 明细。"""

    def __init__(self) -> None:
        self.rounds: list[dict[str, Any]] = []
        self._started_at = datetime.now().isoformat()
        self._t_start: float | None = None

    def start_round(self) -> None:
        self._t_start = time.monotonic()

    def record(
        self,
        *,
        iteration: int,
        stop_reason: str,
        model_output_text: str,
        tool_calls: list[dict[str, Any]],
        input_tokens: int,
        output_tokens: int,
        tool_results: list[Any],
    ) -> None:
        duration_ms = round((time.monotonic() - self._t_start) * 1000) if self._t_start else 0
        self.rounds.append(
            {
                "iteration": iteration,
                "timestamp": datetime.now().isoformat(),
                "duration_ms": duration_ms,
                "stop_reason": stop_reason,
                "model_text": model_output_text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["input"],
                        "result": tr,
                    }
                    for tc, tr in zip(tool_calls, tool_results)
                ],
            }
        )

    def print_summary(self) -> None:
        if not self.rounds:
            print("[LoopTracer] 无记录")
            return

        print(f"\n{'=' * 80}")
        print("📋 执行轨迹总览")
        print(f"{'=' * 80}")
        print(
            f"  {'轮':<3} {'耗时':>7} {'stop':<12} {'input':>7} {'out':>7} {'工具调用 & 结果':<35}"
        )
        print(f"  {'─' * 80}")

        total_in = total_out = total_calls = 0
        for r in self.rounds:
            for i, tc in enumerate(r["tool_calls"]):
                prefix = (
                    f"  {r['iteration']:<3} {r['duration_ms']:>6}ms "
                    f"{'tool_use':<12} {r['input_tokens']:>7} "
                    f"{r['output_tokens']:>7}"
                    if i == 0
                    else " " * 46
                )
                result_preview = str(tc["result"])[:60].replace("\n", " ")
                print(f"{prefix} {tc['name']}({json.dumps(tc['input'], ensure_ascii=False)})")
                print(f"{' ' * 46}  └─ {result_preview}")

            if not r["tool_calls"]:
                print(
                    f"  {r['iteration']:<3} {r['duration_ms']:>6}ms "
                    f"{r['stop_reason']:<12} {r['input_tokens']:>7} "
                    f"{r['output_tokens']:>7}  (最终回复)"
                )
            total_in += r["input_tokens"]
            total_out += r["output_tokens"]
            total_calls += len(r["tool_calls"])

        print(f"  {'─' * 80}")
        print(
            f"  {len(self.rounds)} 轮 · {total_calls} 次工具调用 · "
            f"input={total_in} output={total_out} 合计={total_in + total_out} tok"
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "started_at": self._started_at,
                    "summary": {
                        "total_rounds": len(self.rounds),
                        "total_tool_calls": sum(len(r["tool_calls"]) for r in self.rounds),
                        "total_input_tokens": sum(r["input_tokens"] for r in self.rounds),
                        "total_output_tokens": sum(r["output_tokens"] for r in self.rounds),
                    },
                    "rounds": self.rounds,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"  💾 完整轨迹已保存: {path}")
