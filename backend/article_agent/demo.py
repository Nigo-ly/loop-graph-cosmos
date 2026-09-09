"""场景2：文章写作与自检 — 全量历史 + 15k token 预算。"""

import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import AnthropicLLM, TokenBudget, LoopTracer, run_loop

WRITE_TOOL = {
    "name": "draft_section",
    "description": "撰写文章的一个章节。调用后获得该章节草稿。支持: intro(引言), analysis(分析), recommendation(建议), conclusion(结论)",
    "input_schema": {"type": "object", "properties": {"section": {"type": "string", "description": "intro, analysis, recommendation, conclusion"}}, "required": ["section"]},
}
REVIEW_TOOL = {
    "name": "review_draft",
    "description": "自检当前全部草稿的质量。返回评分(1-10)和改进建议。必须在写作结束后调用。",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}
TOOLS = [WRITE_TOOL, REVIEW_TOOL]

_written: set[str] = set()

def tool_impl(name: str, args: dict) -> str:
    if name == "draft_section":
        s = args["section"]
        _written.add(s)
        drafts = {
            "intro": "2026年上半年，AI Agent技术从概念验证走向工程落地，Loop Engineering作为新范式正在重塑AI编程的工作方式。本文将从数据出发，分析这一趋势对运营效率的实际影响。",
            "analysis": "数据显示，采用Loop Engineering后，团队的人均产出提升约40%，但初期学习曲线陡峭。超过60%的团队在前两周遇到工具链集成问题。",
            "recommendation": "建议分三步推进：1)先用现成框架跑通最小闭环 2)逐步引入预算控制和错误恢复 3)建立可观测性体系。每阶段设明确的OKR指标。",
            "conclusion": "Loop Engineering不是银弹，但它确实解决了Prompt Engineering时代'人在等AI'的核心痛点。关键成功因素是工程化配套（预算/状态/监控）而非模型本身。",
        }
        return json.dumps({"section": s, "draft": drafts.get(s, "无此章节"), "word_count": len(drafts.get(s, ""))}, ensure_ascii=False)
    if name == "review_draft":
        score = min(10, 5 + len(_written))
        issues = ["引言数据引用可更具体"] if len(_written) < 3 else []
        return json.dumps({"score": score, "issues": issues, "suggestion": "可以补充更多定量数据" if issues else "整体质量良好"}, ensure_ascii=False)
    return json.dumps({"error": f"未知工具: {name}"})

if __name__ == "__main__":
    llm = AnthropicLLM()
    budget = TokenBudget(limit=15_000)
    tracer = run_loop(
        "写一篇关于'AI Agent Loop Engineering对运营效率影响'的短文。先写引言、分析、建议、结论四个章节，每写一章后自查草稿质量，最后给出终稿。",
        TOOLS, tool_impl, llm=llm, budget=budget, state_strategy="full", max_iterations=15,
    )
    tracer.print_summary()
    tracer.save(os.path.join(os.path.dirname(__file__), "..", "traces", "article.json"))
