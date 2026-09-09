from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.intent_production import (
    PublicDiscoveryIntentAdapter,
    direct_intent_adapter,
    production_intent_resolver,
)
from fragment_loop.intent_service import FragmentIntentService
from fragment_loop.phone_alignment_sync import PhoneAlignmentConnector


def source(_fragment_id: str, input_digest: str) -> dict[str, object]:
    return {
        "title": "MiniMax H3 权重本地部署可行性",
        "literal_summary": "需要确认是否正式发布，并判断本地硬件能否运行。",
        "memory_basis": [],
        "input_digest": input_digest,
    }


def test_resolver_chooses_shortest_route_without_graph() -> None:
    verify = production_intent_resolver(source("fragment-1", "a" * 64))
    assert verify["recommended_route"] == "verify"
    assert verify["suggested_intents"] == [
        "verify",
        "evaluate_relevance",
        "deploy_or_build",
    ]
    assert verify["execution_scope"] == {
        "capabilities": ["公开来源发现", "安全网页抓取", "受治理研究合成"],
        "external_scope": ["官方资料", "GitHub、模型社区与可信技术社区"],
        "model_call_cap": 1,
        "cost_cap_cny": 2.0,
        "side_effect": "只读，无外部写入",
        "model_provider": "deepseek",
        "model_name": "deepseek-v4-pro",
        "write_scope": ["Loop Checkpoint 研究结果"],
    }
    direct = production_intent_resolver(
        {
            "title": "一个灵感",
            "literal_summary": "记录一个关于工作方法的想法。",
            "memory_basis": [],
            "input_digest": "a" * 64,
        }
    )
    assert direct["recommended_route"] == "direct"


def test_public_source_routes_marker_free_research_to_verify() -> None:
    proposal = production_intent_resolver(
        {
            "title": "Trafilatura",
            "literal_summary": "网页正文提取工具。",
            "goal": "是否适合用于我们的网页正文提取流程？",
            "source_seed_url": "https://github.com/adbar/trafilatura",
        }
    )
    assert proposal["recommended_route"] == "verify"
    assert proposal["suggested_intents"] == ["verify", "evaluate_relevance"]


def test_source_goal_participates_in_route_and_intent_selection() -> None:
    proposal = production_intent_resolver(
        {
            "title": "一个工具",
            "literal_summary": "一条需要继续处理的资料。",
            "goal": "验证它是否支持本地安装。",
        }
    )
    assert proposal["recommended_route"] == "verify"
    assert "deploy_or_build" in proposal["suggested_intents"]


def test_note_organization_without_external_source_keeps_direct_route() -> None:
    proposal = production_intent_resolver(
        {
            "title": "我的工作方法",
            "literal_summary": "整理已有笔记中的时间管理心得。",
            "goal": "把现有笔记按主题整理。",
        }
    )
    assert proposal["recommended_route"] == "direct"
    assert proposal["suggested_intents"] == ["learn", "explore"]


def test_non_public_or_invalid_seed_does_not_force_verify() -> None:
    for seed in ("http://example.com/page", "https://127.0.0.1/page", "not-a-url", None):
        proposal = production_intent_resolver(
            {"title": "一条笔记", "literal_summary": "整理工作心得。", "source_seed_url": seed}
        )
        assert proposal["recommended_route"] == "direct", seed


def test_public_source_preserves_complex_continuation_route() -> None:
    proposal = production_intent_resolver(
        {
            "title": "故障处理",
            "literal_summary": "上一步结果：任务失败，需要修改配置。",
            "source_seed_url": "https://example.com/project",
        }
    )
    assert proposal["recommended_route"] == "graph"


def test_direct_reuses_organized_projection_with_zero_calls() -> None:
    result = direct_intent_adapter(
        {"literal_summary": "这是一份已经整理好的直接结论。"}
    )
    assert result["summary"] == "这是一份已经整理好的直接结论。"
    assert result["model_calls"] == 0
    assert result["tool_calls"] == 0


def test_verify_discovers_official_and_community_sources_with_zero_models() -> None:
    html = b"""
    <a class="result__a" href="https://www.minimaxi.com/news">Official release</a>
    <a class="result__a" href="https://github.com/MiniMax-AI/model">GitHub model</a>
    <a class="result__a" href="http://unsafe.example/model">unsafe</a>
    """
    calls: list[str] = []
    def transport(query: str) -> bytes:
        calls.append(query)
        return html

    adapter = PublicDiscoveryIntentAdapter(transport)
    result = adapter({"title": "MiniMax H3", "literal_summary": "核实开源"})
    assert calls == ["MiniMax H3 官方", "MiniMax H3 GitHub Hugging Face 社区"]
    assert result["model_calls"] == 0
    assert result["tool_calls"] == 2
    assert "www.minimaxi.com" in str(result["summary"])
    assert "github.com" in str(result["summary"])
    next_checks = result["next_checks"]
    assert isinstance(next_checks, list)
    assert all(str(item).startswith("https://") for item in next_checks)


def test_verify_fails_honestly_when_discovery_is_unavailable() -> None:
    def failed(_query: str) -> bytes:
        raise RuntimeError("offline")

    result = PublicDiscoveryIntentAdapter(failed)(
        {"title": "MiniMax H3", "literal_summary": "核实开源"}
    )
    assert result["summary"] == "本次公开来源发现没有取得可复核结果，不能据此判断原始说法为真。"
    assert result["model_calls"] == 0
    assert result["harvest"][0]["maturity"] == "candidate"  # type: ignore[index]


class SourceBridge:
    def discover_intent_source(self, fragment_id: str) -> dict[str, object]:
        return source(fragment_id, "a" * 64)


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()


def test_phone_connector_uses_worker_only_as_projection_and_command_queue(
    tmp_path: Path,
) -> None:
    service = FragmentIntentService(
        SQLiteCheckpointStore(tmp_path / "state.sqlite3"),
        source,
        production_intent_resolver,
        verify_adapter=lambda _binding: {
            "summary": "已完成核验",
            "unknowns": [],
            "next_checks": [],
            "needs_escalation": False,
            "escalation_reason": "",
            "model_calls": 0,
            "tool_calls": 0,
            "harvest": [],
        },
    )
    secret = "test-secret"
    captured: list[dict[str, Any]] = []

    def transport(body: bytes, timestamp: str, signature: str) -> dict[str, object]:
        assert hmac.compare_digest(signature, _signature(secret, timestamp, body))
        captured.append(json.loads(body))
        if len(captured) == 1:
            payload = {
                "fragments": [{"fragment_id": "fragment-1", "nigo_loop": True}],
                "actions": [],
            }
        else:
            item = captured[-1]["alignments"][0]
            payload = {
                "fragments": [],
                "actions": [
                    {
                        "alignment_id": item["alignment_id"],
                        "revision": item["revision"],
                        "input_digest": item["input_digest"],
                        "intents": ["verify", "evaluate_relevance"],
                        "supplement": "",
                        "action": "confirm",
                        "requester": "nigo",
                    }
                ],
            }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return {
            "body": raw,
            "timestamp": timestamp,
            "signature": _signature(secret, timestamp, raw),
        }

    connector = PhoneAlignmentConnector(
        service,
        SourceBridge(),  # type: ignore[arg-type]
        credential_reader=lambda: secret,
        transport=transport,
        clock=lambda: 1_700_000_000.0,
    )
    assert connector.sync_once() == {
        "status": "ok",
        "proposed": 1,
        "decided": 0,
        "continued": 0,
    }
    assert connector.sync_once() == {
        "status": "ok",
        "proposed": 0,
        "decided": 1,
        "continued": 0,
    }
    alignment = service.list_alignments()[0]
    assert alignment["status"] == "passed"
    assert alignment["execution"]["status"] == "passed"  # type: ignore[index]
    serialized = json.dumps(captured, ensure_ascii=False)
    assert "MiniMax H3 权重本地部署可行性" in serialized
    assert "碎片正文" not in serialized
