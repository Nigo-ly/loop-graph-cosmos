"""Minimal production adapters for fragment intent v1.

The resolver is deterministic.  ``direct`` reuses the already organized
projection; ``verify`` performs at most two zero-model public discovery calls
and reports only source candidates and honest gaps.  It never writes assets or
creates Graph runs.

DDG 搜索出口已迁移到 ``fragment_loop.research_fetch`` 标准：DNS+TLS peer
交叉核验、URL 离线校验拒 IP 字面量（``is_public_https_locator``）。
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Callable, Mapping

from fragment_loop.cognitive_retrieval import is_public_https_locator
from fragment_loop.research_fetch import DDGSearchTransport

SEARCH_HOST = "html.duckduckgo.com"
SEARCH_PATH = "/html/"
SEARCH_TIMEOUT_SECONDS = 20
SEARCH_MAX_BYTES = 2 * 1024 * 1024
MAX_RESULTS_PER_QUERY = 5

SearchTransport = Callable[[str], bytes]

# 默认出口带 live 门（M5）：未显式启用 --research-live 时失败关闭、零网络；
# 生产装配在 cognitive_server 显式注入 DDGSearchTransport(live_enabled=...)。
_default_search_transport = DDGSearchTransport(live_enabled=False)


def duckduckgo_search_transport(query: str) -> bytes:
    """DDG 出口 = research_fetch 标准（DNS+TLS peer 交叉核验 + live 门）。"""
    return _default_search_transport(query)


def production_intent_resolver(source: Mapping[str, object]) -> dict[str, object]:
    title = str(source.get("title", ""))
    summary = str(source.get("literal_summary", ""))
    goal = str(source.get("goal", ""))
    text = f"{title} {summary} {goal}".lower()
    verify_markers = (
        "真假",
        "真实",
        "核实",
        "验证",
        "开源",
        "发布",
        "许可证",
        "部署",
        "下载",
        "本地",
        "硬件",
        "github",
        "模型",
        "权重",
    )
    build_markers = ("部署", "下载", "安装", "开发", "搭建", "本地", "硬件")
    continuation_is_complex = "上一步结果：" in summary and any(
        marker in text
        for marker in ("失败", "报错", "显存", "冲突", "回滚", "修改配置", "真实执行")
    )
    if continuation_is_complex:
        return {
            "suggested_intents": ["plan_action"],
            "dynamic_intents": [],
            "reasoning": "这是基于既有结果出现的新执行问题，包含多步处理、失败恢复或副作用风险。",
            "plan": "先形成绑定当前结果的 Graph 升级提案；是否创建和执行仍由既有人工闸门决定。",
            "expected_result": "得到一个可审计、可暂停恢复的 Graph 工作流提案。",
            "exclusions": ["不自动创建 Graph Run", "不继承旧授权", "不直接执行副作用"],
            "recommended_route": "graph",
            "execution_scope": {
                "capabilities": ["Graph 升级提案"],
                "external_scope": [],
                "model_call_cap": 0,
                "cost_cap_cny": 0.0,
                "side_effect": "仅生成提案",
                "model_provider": "",
                "model_name": "",
                "write_scope": ["Loop Checkpoint 升级提案"],
            },
        }
    seed_url = source.get("source_seed_url")
    has_public_source = isinstance(seed_url, str) and is_public_https_locator(seed_url)
    needs_verify = has_public_source or any(marker in text for marker in verify_markers)
    needs_build = any(marker in text for marker in build_markers)
    if needs_verify:
        intents = ["verify", "evaluate_relevance"]
        if needs_build:
            intents.append("deploy_or_build")
        return {
            "suggested_intents": intents,
            "dynamic_intents": [],
            "reasoning": (
                "这条信息包含可核验的外部事实或可行性判断，"
                "先查公开来源比直接进入复杂工作流更可靠。"
            ),
            "plan": (
                "先检查官方与公开社区来源；证据不足时诚实列出缺口，"
                "只有出现复杂执行或副作用才提出 Graph 升级。"
            ),
            "expected_result": "得到来源线索、当前可确认的范围、未知项和最短下一步。",
            "exclusions": ["不安装软件", "不执行部署", "不写知识资产"],
            "recommended_route": "verify",
            "execution_scope": {
                "capabilities": ["公开来源发现", "安全网页抓取", "受治理研究合成"],
                "external_scope": ["官方资料", "GitHub、模型社区与可信技术社区"],
                "model_call_cap": 1,
                "cost_cap_cny": 2.0,
                "side_effect": "只读，无外部写入",
                "model_provider": "deepseek",
                "model_name": "deepseek-v4-pro",
                "write_scope": ["Loop Checkpoint 研究结果"],
            },
        }
    return {
        "suggested_intents": ["learn", "explore"],
        "dynamic_intents": [],
        "reasoning": "现有整理材料足以先给出一次直接回应，不需要提前升级为 Graph。",
        "plan": "先基于已整理内容给出简短答案；若答案暴露新的证据缺口，再续接核验。",
        "expected_result": "得到一个直接答案、仍未知的边界和可选下一步。",
        "exclusions": ["不联网", "不执行外部操作", "不写知识资产"],
        "recommended_route": "direct",
        "execution_scope": {
            "capabilities": ["本地确定性整理"],
            "external_scope": [],
            "model_call_cap": 0,
            "cost_cap_cny": 0.0,
            "side_effect": "仅生成 Loop 内结果",
            "model_provider": "",
            "model_name": "",
            "write_scope": ["Loop Checkpoint 处理结果"],
        },
    }


def direct_intent_adapter(binding: Mapping[str, object]) -> dict[str, object]:
    summary = _clean(str(binding.get("literal_summary", "")), 1000)
    return {
        "summary": summary,
        "unknowns": [],
        "next_checks": ["如果这份直接回答没有覆盖你的真实目的，可基于结果继续并补充目标。"],
        "needs_escalation": False,
        "escalation_reason": "",
        "model_calls": 0,
        "tool_calls": 0,
        "harvest": [
            {
                "role": "result",
                "summary": summary[:500],
                "maturity": "candidate",
                "qualification_basis": None,
            }
        ],
    }


def _clean(value: str, limit: int) -> str:
    text = " ".join(html.unescape(value).split())
    text = "".join(character for character in text if ord(character) >= 0x20)
    return text[:limit]


_RESULT_LINK = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    flags=re.I | re.S,
)
_TAG = re.compile(r"<[^>]+>")


def _parse_results(payload: bytes) -> list[dict[str, str]]:
    try:
        page = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("public_search_invalid_encoding") from error
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url, raw_title in _RESULT_LINK.findall(page):
        url = html.unescape(raw_url)
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname == "duckduckgo.com":
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [])
            if target:
                url = target[0]
        if not is_public_https_locator(url) or url in seen:
            continue
        title = _clean(_TAG.sub(" ", raw_title), 160)
        if not title:
            continue
        seen.add(url)
        results.append({"title": title, "url": url})
        if len(results) == MAX_RESULTS_PER_QUERY:
            break
    return results


class PublicDiscoveryIntentAdapter:
    def __init__(self, transport: SearchTransport = duckduckgo_search_transport) -> None:
        self.transport = transport

    def __call__(self, binding: Mapping[str, object]) -> dict[str, object]:
        title = _clean(str(binding.get("title", "")), 120)
        if not title:
            raise RuntimeError("intent_title_missing")
        queries = (f"{title} 官方", f"{title} GitHub Hugging Face 社区")
        found: list[dict[str, str]] = []
        errors: list[str] = []
        for query in queries:
            try:
                found.extend(_parse_results(self.transport(query)))
            except Exception as error:
                errors.append(_clean(str(error), 120) or "公开检索失败")
        unique = {item["url"]: item for item in found}
        ordered = [unique[key] for key in sorted(unique)][:8]
        if not ordered:
            return {
                "summary": "本次公开来源发现没有取得可复核结果，不能据此判断原始说法为真。",
                "unknowns": ["官方发布状态与社区可复现情况仍未核实"],
                "next_checks": ["稍后重试公开来源发现，或补充一个明确的官方／仓库链接。"],
                "needs_escalation": False,
                "escalation_reason": "",
                "model_calls": 0,
                "tool_calls": len(queries),
                "harvest": [
                    {
                        "role": "failure",
                        "summary": "公开来源发现未得到可复核结果。",
                        "maturity": "candidate",
                        "qualification_basis": None,
                    }
                ],
            }
        source_lines = [
            f"{index + 1}. {_clean(item['title'], 100)}"
            f"（{urllib.parse.urlsplit(item['url']).hostname}）"
            for index, item in enumerate(ordered[:5])
        ]
        next_checks = [item["url"] for item in ordered[:4]]
        unknowns = ["检索结果证明的是来源存在，具体主张仍需打开原文逐项核对。"]
        if errors:
            unknowns.append("部分检索请求失败，结果可能不完整。")
        return {
            "summary": "已按官方与社区两个方向发现公开来源：" + "；".join(source_lines),
            "unknowns": unknowns,
            "next_checks": next_checks,
            "needs_escalation": False,
            "escalation_reason": "",
            "model_calls": 0,
            "tool_calls": len(queries),
            "harvest": [
                {
                    "role": "evidence",
                    "summary": "已发现官方／社区公开来源候选，尚待逐项内容核验。",
                    "maturity": "candidate",
                    "qualification_basis": None,
                }
            ],
        }


__all__ = [
    "PublicDiscoveryIntentAdapter",
    "direct_intent_adapter",
    "duckduckgo_search_transport",
    "production_intent_resolver",
]
