"""场景4：知识库问答 — 全量历史 + 8k token 预算 + 诚实拒答。"""

import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import AnthropicLLM, TokenBudget, LoopTracer, run_loop

SEARCH_TOOL = {
    "name": "search_kb",
    "description": "在内部知识库中搜索文档。如果搜索不到相关结果，返回 'no_results'。支持主题: product_price(产品定价), refund_policy(退款政策), sla(服务水平协议), onboarding(新客户接入流程)",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词: product_price, refund_policy, sla, onboarding"}}, "required": ["query"]},
}
TOOLS = [SEARCH_TOOL]

KB = {
    "product_price": "企业版 ¥9,999/月(含50席位), 专业版 ¥2,999/月(含10席位), 免费版 5人以下免费。",
    "refund_policy": "购买后7天内无条件全额退款。企业版支持按剩余天数比例退款。退款至原支付方式，3-5个工作日到账。",
    "sla": "企业版 SLA 99.9%，故障响应时间<15分钟。专业版 SLA 99.5%，响应<2小时。免费版无SLA承诺。",
    "onboarding": "新客户接入流程：1)注册认证 2)配置SSO 3)导入用户 4)培训(企业版含2小时专属培训)。平均接入周期3-5个工作日。",
}

def tool_impl(name: str, args: dict) -> str:
    if name == "search_kb":
        query = args["query"].lower()
        for key, content in KB.items():
            if query in key or query in key.replace("_", " "):
                return json.dumps({"query": query, "found": True, "content": content}, ensure_ascii=False)
        return json.dumps({"query": query, "found": False, "content": "no_results"}, ensure_ascii=False)
    return json.dumps({"error": f"未知工具: {name}"})

if __name__ == "__main__":
    llm = AnthropicLLM()
    budget = TokenBudget(limit=8_000)
    tracer = run_loop(
        "客户问：你们的产品价格是多少？退款政策是什么？支持SSO吗？",
        TOOLS, tool_impl, llm=llm, budget=budget, state_strategy="full", max_iterations=10,
    )
    tracer.print_summary()
    tracer.save(os.path.join(os.path.dirname(__file__), "..", "traces", "kb_qa.json"))
