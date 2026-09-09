"""场景1：自动化运营分析 — 滑动窗口 + 30k token 预算。"""

import json, os, random, sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import AnthropicLLM, GraduatedBudget, LoopTracer, run_loop

TOOLS = [{
    "name": "fetch_metric",
    "description": "查询实时运营指标。支持: DAU, new_users, conversion_rate, revenue, retention_d1, retention_d7, churn_rate",
    "input_schema": {"type": "object", "properties": {"metric_name": {"type": "string", "description": "DAU, new_users, conversion_rate, revenue, retention_d1, retention_d7, churn_rate"}}, "required": ["metric_name"]},
}]

DETAILS = {
    "DAU": "日活 32,451(+2.3%), iOS55% Android45%",
    "new_users": "新用户 5,812(+1.8%), CPA ¥18.50",
    "conversion_rate": "转化率 3.71%(-0.3pp), ROAS 3.21",
    "revenue": "收入 ¥123,700(+8.2%), ARPU ¥3.81",
    "retention_d1": "次日留存 68.2%(+2.1pp)",
    "retention_d7": "7日留存 42.1%(持平)",
    "churn_rate": "月流失率 5.4%(-0.8pp), 主要流失在第2-3周",
}
_cnt: dict[str, int] = {}

def tool_impl(name: str, args: dict) -> str:
    m = args["metric_name"]
    _cnt[m] = _cnt.get(m, 0) + 1
    if _cnt[m] <= 2:
        return json.dumps({"error": "数据库超时，请重试", "status": "failed"}, ensure_ascii=False)
    return json.dumps({"metric": m, "value": DETAILS.get(m, f"未知:{m}"), "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False)

if __name__ == "__main__":
    random.seed(42)
    llm = AnthropicLLM()
    budget = GraduatedBudget(limit=30_000)
    tracer = run_loop(
        "全面分析今天运营数据：查询 DAU, new_users, conversion_rate, revenue, retention_d1, retention_d7, churn_rate，逐个查询后给综合运营报告。",
        TOOLS, tool_impl, llm=llm, budget=budget, state_strategy="window", max_iterations=30,
    )
    tracer.print_summary()
    tracer.save(os.path.join(os.path.dirname(__file__), "..", "traces", "ops_analysis.json"))
    print(f"\n预算: {budget.consumed}/{budget.limit} tok")
    if budget.history:
        print(f"降级: {' → '.join(f'{t}@{c}' for c, t in budget.history)}")
