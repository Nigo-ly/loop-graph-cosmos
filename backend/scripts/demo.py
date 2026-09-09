#!/usr/bin/env python3
"""Offline synthetic demonstration of publication, correction, history and reuse."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fragment_loop.knowledge_library import KnowledgeLibrary, _digest


def run(vault: Path) -> dict:
    if vault.exists():
        raise ValueError("Demo destination must not exist; choose a new directory.")
    vault.mkdir(parents=True)
    assets = vault / "assets"
    assets.mkdir()
    (vault / "candidates").mkdir()
    (vault / "Notes/散记/碎片想法").mkdir(parents=True)
    library = KnowledgeLibrary(assets, vault_root=vault)
    excerpt = "Synthetic fixture: the example renders a static workflow; runtime updates are supplied externally."
    evidence = [{"evidence_id": "ev-synthetic", "title": "SYNTHETIC example, never fetched",
                 "url": "https://example.org/workflow", "source_type": "official_docs",
                 "excerpts": [excerpt], "digest": hashlib.sha256(excerpt.encode()).hexdigest()}]
    result = {
        "summary": "演示结论：可展示静态流程，运行状态需外部提供。",
        "recommendation": "合成演示数据，不可作为真实工具选型依据。",
        "confirmed": [{"claim": "夹具描述静态流程", "evidence_ids": ["ev-synthetic"]}],
        "unknowns": ["真实项目的运行能力尚未验证"], "conflicts": [],
        "claims": [{"claim": "夹具描述静态流程", "evidence_id": "ev-synthetic", "relation": "supports"}],
        "topic": {"category": "技术", "subcategory": "Agent 工具", "title": "运行可视化"},
        "answer_markdown": "## 核心结论\n\n这是离线合成演示。\n\n| 问题 | 当前答案 |\n| --- | --- |\n| 静态展示 | 夹具支持 |\n| 实时状态 | 需要外部输入 |",
        "coverage": [{"question": "夹具支持什么", "answer": "静态流程", "evidence_ids": ["ev-synthetic"], "status": "answered"}],
        "agent_usage": {"when_to_use": "学习本地知识接口", "steps": ["搜索主题", "读取当前结论及证据", "需要时读取历史版本"], "limitations": ["合成数据不可冒充真实验证"]},
    }
    def save(**kwargs):
        review = {"policy_version": "independent-evidence-review-v1", "draft_digest": _digest(result),
                  "reviewed_result_digest": _digest(result), "reviewed_at": "2026-09-01T00:00:00Z",
                  "verdict": "supported_with_limits", "reason": "SYNTHETIC receipt: no model or independent reviewer was invoked.", "findings": []}
        return library.save_research(run_id="exec:synthetic-demo", fragment_id="synthetic-demo", title="合成演示：流程可视化判断",
            result=result, evidence=evidence, independent_review=review, **kwargs)
    first = save(changed_at="2026-09-01T00:00:00Z")
    result["recommendation"] = "演示修订：必须核对外部状态来源，历史版本保持可读。"
    second = save(expected_revision=1, changed_at="2026-09-02T00:00:00Z", revision_reason="合成演示：追加限制")
    assert first["knowledge_id"] == second["knowledge_id"]
    assert len(library.history(first["knowledge_id"])) == 2
    assert library.search("可视化")
    assert library.read(first["knowledge_id"], 1)["result"]["recommendation"] != result["recommendation"]
    return {"synthetic": True, "network_calls": 0, "model_calls": 0, "knowledge_id": first["knowledge_id"],
            "current_revision": 2, "history_length": 2, "note": str(vault / second["path"]),
            "agent_usage": library.read(first["knowledge_id"])["result"]["agent_usage"]}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True, help="new directory, never an existing vault")
    args = parser.parse_args()
    print(json.dumps(run(args.vault.expanduser().resolve()), ensure_ascii=False, indent=2))
