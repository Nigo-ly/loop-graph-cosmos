"""Research publication must preserve evidence, identity, history and bounded goals."""

from __future__ import annotations

import hashlib
import html
import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from fragment_loop.knowledge_library import KnowledgeLibrary, KnowledgeLibraryError, _digest
from fragment_loop.product_review import ProductReviewError


def library(tmp_path: Path) -> KnowledgeLibrary:
    root = tmp_path / "vault"
    root.mkdir(exist_ok=True)
    assets = root / "Loop知识资产"
    assets.mkdir(exist_ok=True)
    return KnowledgeLibrary(assets, vault_root=root)


def evidence() -> list[dict[str, Any]]:
    excerpt = "Archify 示例验证支持有向循环，实时状态必须由外部准确提供。"
    return [
        {
            "evidence_id": "ev-example",
            "title": "Lifecycle example",
            "url": "https://example.org/lifecycle",
            "source_type": "official_docs",
            "excerpts": [excerpt],
            "digest": hashlib.sha256(excerpt.encode()).hexdigest(),
        }
    ]


def result() -> dict[str, Any]:
    return {
        "summary": "Archify 可以表达生命周期重试，仍需映射真实运行数据。",
        "recommendation": "限定试用，先验证运行快照再扩大接入。",
        "confirmed": [{"claim": "示例包含重试路径", "evidence_ids": ["ev-example"]}],
        "unknowns": ["尚未完成本地 GraphSpec 全量映射"],
        "conflicts": [],
        "claims": [{"claim": "可表达重试", "evidence_id": "ev-example", "relation": "supports"}],
        "topic": {"category": "技术", "subcategory": "Agent工具", "title": "运行可视化"},
        "answer_markdown": (
            "## 实用判断\n\n先完成一张真实运行图。\n\n"
            "| 能力 | 判断 |\n| --- | --- |\n| 重试 | 支持 |"
        ),
        "coverage": [
            {
                "question": "能否表达重试",
                "answer": "示例支持",
                "evidence_ids": ["ev-example"],
                "status": "answered",
            }
        ],
        "agent_usage": {
            "when_to_use": "需要画运行流程时",
            "steps": ["读取验证过的示例", "生成后校验"],
            "limitations": ["图表结构有效不证明业务事实有效"],
        },
    }


def review(value: dict[str, Any]) -> dict[str, Any]:
    """Synthetic completed review receipt; no real model call is implied."""
    return {
        "policy_version": "independent-evidence-review-v1",
        "draft_digest": _digest(value),
        "reviewed_result_digest": _digest(value),
        "reviewed_at": "2026-09-01T00:00:00+00:00",
        "verdict": "supported_with_limits",
        "reason": "隔离测试的复核夹具，不是真实研究",
        "findings": [],
    }


def save(service: KnowledgeLibrary, **kwargs: Any) -> dict[str, Any]:
    fields = {
        "run_id": "exec:fragment-intent:example",
        "fragment_id": "fragment-one",
        "title": "Archify 适配判断",
        "result": result(),
        "evidence": evidence(),
        "changed_at": "2026-09-01T08:00:00+08:00",
        **kwargs,
    }
    fields.setdefault("independent_review", review(fields["result"]))
    return service.save_research(**fields)


def test_saves_actual_body_sources_and_system_identity(tmp_path: Path) -> None:
    service = library(tmp_path)
    saved = save(service)
    loaded = service.read(saved["knowledge_id"])
    assert loaded["result"]["recommendation"] == result()["recommendation"]
    assert loaded["result"]["agent_usage"] == result()["agent_usage"]
    assert loaded["evidence"] == evidence()
    assert loaded["publication_source"] == "system_policy"
    assert loaded["human_reviewed"] is False
    markdown = (service.vault_root / saved["path"]).read_text()
    assert "## 实用判断" in markdown and "ev-example" in markdown
    assert "尚未完成本地 GraphSpec 全量映射" in markdown
    assert service.search("全量映射")[0]["knowledge_id"] == saved["knowledge_id"]
    index = (service.root / "目录.md").read_text()
    assert "## 技术" in index and "### Agent工具" in index and "运行可视化" in index


def test_same_result_concurrent_instances_commit_once(tmp_path: Path) -> None:
    service = library(tmp_path)

    def publish(_: int) -> dict[str, Any]:
        other = KnowledgeLibrary(service.assets_root, vault_root=service.vault_root)
        return save(other)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(publish, range(20)))
    assert sum(not row["idempotent"] for row in outputs) == 1
    assert {row["revision"] for row in outputs} == {1}
    assert len(service.history(outputs[0]["knowledge_id"])) == 1


def test_revision_requires_cas_and_preserves_prior_truth(tmp_path: Path) -> None:
    service = library(tmp_path)
    first = save(service)
    amended = result()
    amended["recommendation"] = "发现映射限制，暂缓接入。"
    with pytest.raises(KnowledgeLibraryError, match="expected_revision_required"):
        save(service, result=amended)
    second = save(
        service,
        result=amended,
        expected_revision=1,
        changed_at="2026-09-03T08:00:00+08:00",
        revision_reason="新增证据修订",
    )
    assert second["previous_sha256"] == first["content_sha256"]
    assert (
        service.read(first["knowledge_id"], 1)["result"]["recommendation"]
        == result()["recommendation"]
    )
    assert second["revision"] == 2 and len(service.history(first["knowledge_id"])) == 2
    with pytest.raises(KnowledgeLibraryError, match="revision_conflict"):
        save(service, result=result(), expected_revision=1)
    assert save(service, result=amended, expected_revision=1)["idempotent"] is True


def test_conflicting_writers_do_not_overwrite_each_other(tmp_path: Path) -> None:
    service = library(tmp_path)
    first = save(service)

    def revise(index: int) -> str:
        other = KnowledgeLibrary(service.assets_root, vault_root=service.vault_root)
        amended = result()
        amended["recommendation"] = f"新增判断 {index}"
        try:
            save(other, result=amended, expected_revision=1)
        except KnowledgeLibraryError as error:
            return str(error)
        return "saved"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(revise, range(4)))
    assert outcomes.count("saved") == 1
    assert outcomes.count("revision_conflict") == 3
    assert len(service.history(first["knowledge_id"])) == 2


def test_later_fragment_joins_existing_topic_without_restarting_it(tmp_path: Path) -> None:
    service = library(tmp_path)
    first = save(service)
    later = result()
    later["topic"] = {"existing_topic_id": first["topic"]["topic_id"]}
    second = save(
        service,
        run_id="another-run",
        fragment_id="third-week-fragment",
        result=later,
        changed_at="2026-09-21T08:00:00+08:00",
    )
    assert first["topic"] == second["topic"]
    assert first["knowledge_id"] != second["knowledge_id"]
    catalog = service.catalog()
    assert len(catalog) == 1 and len(catalog[0]["notes"]) == 2
    assert all("completion" not in topic for topic in catalog)


def test_unknown_topic_id_is_not_silently_created(tmp_path: Path) -> None:
    service = library(tmp_path)
    invalid = result()
    invalid["topic"] = {"existing_topic_id": "topic-" + "f" * 24}
    with pytest.raises(KnowledgeLibraryError, match="existing_topic_not_found"):
        save(service, result=invalid)
    assert service.catalog() == []


@pytest.mark.parametrize("status", ["passed", "evidence_ready", "completed", "synthesis_disabled"])
def test_collection_success_never_becomes_a_knowledge_result(tmp_path: Path, status: str) -> None:
    service = library(tmp_path)
    with pytest.raises(KnowledgeLibraryError, match="synthesis_required"):
        save(service, status=status)
    assert service.catalog() == []


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("confirmed", [], "substantiated_conclusion_required"),
        ("confirmed", [{"claim": "fabricated", "evidence_ids": ["ev-invented"]}], "not_closed"),
        ("unknowns", None, "unknowns_invalid"),
        ("claims", None, "claims_invalid"),
        ("answer_markdown", {"body": "bad"}, "answer_markdown_invalid"),
    ],
)
def test_unsubstantiated_or_invalid_conclusions_fail_closed(
    tmp_path: Path,
    field: str,
    value: Any,
    message: str,
) -> None:
    invalid = result()
    invalid[field] = value
    service = library(tmp_path)
    with pytest.raises(KnowledgeLibraryError, match=message):
        save(service, result=invalid)
    assert service.catalog() == []


def test_raw_html_is_inert_and_active_links_rejected(tmp_path: Path) -> None:
    service = library(tmp_path)
    safe = result()
    safe["answer_markdown"] = '<img src="https://example.org/a" onerror="alert(1)">'
    saved = save(service, result=safe)
    markdown = (service.vault_root / saved["path"]).read_text()
    assert "<img" not in markdown and "&lt;img" in markdown
    unsafe = result()
    unsafe["answer_markdown"] = "[open](javascript:alert(1))"
    with pytest.raises(KnowledgeLibraryError, match="active_markdown_url_forbidden"):
        save(service, result=unsafe, expected_revision=1)


def test_real_local_contract_and_trial_evidence_are_not_fake_web_sources(tmp_path: Path) -> None:
    service = library(tmp_path)
    contract = {
        **evidence()[0],
        "source_type": "local_contract",
        "url": "",
        "source_ref": "project:graph_runtime/spec.py",
    }
    saved = save(service, evidence=[contract])
    assert saved["evidence"][0]["url"] == ""
    assert saved["evidence"][0]["source_ref"] == "project:graph_runtime/spec.py"
    trial = {
        **evidence()[0],
        "source_type": "repository_trial",
        "revision": "abc123",
        "command": "node bin/archify.mjs doctor",
        "exit_code": 0,
        "output_digest": "a" * 64,
    }
    second = save(service, run_id="trial-run", evidence=[trial])
    assert second["evidence"][0]["exit_code"] == 0
    del trial["output_digest"]
    with pytest.raises(KnowledgeLibraryError, match="trial_output_digest_required"):
        save(service, run_id="bad-trial", evidence=[trial])


@pytest.mark.parametrize(
    "mutation",
    [
        {"digest": ""},
        {"excerpts": []},
        {"url": "https://localhost/private"},
        {"url": "https://user:secret@example.org/"},
        {"marker": "omitted"},
        {"source_type": "local_contract", "source_ref": "project:../secret.txt", "url": ""},
    ],
)
def test_unavailable_or_unsafe_evidence_is_not_published(
    tmp_path: Path, mutation: dict[str, Any]
) -> None:
    service = library(tmp_path)
    with pytest.raises(KnowledgeLibraryError):
        save(service, evidence=[{**evidence()[0], **mutation}])
    assert service.catalog() == []


def test_period_keeps_membership_but_uses_current_correction(tmp_path: Path) -> None:
    service = library(tmp_path)
    first = save(service)
    future = result()
    future["recommendation"] = "十月才得到的新判断"
    save(service, result=future, expected_revision=1, changed_at="2026-10-01T08:00:00+08:00")
    period = service.period_input(
        start="2026-09-01T00:00:00+08:00", end="2026-10-01T00:00:00+08:00"
    )
    assert period["semantic_summary_generated"] is False
    assert period["notes"][0]["revision"] == 2
    assert period["notes"][0]["period_membership_revision"] == 1
    assert period["notes"][0]["result"]["recommendation"] == future["recommendation"]
    summarized = service.save_period_summary(
        period="month",
        start=period["start"],
        end=period["end"],
        topic_id=first["topic"]["topic_id"],
        title="九月运行可视化主题",
        summary="已验证表达力，待验证真实映射。",
        knowledge_refs=[{"knowledge_id": first["knowledge_id"], "revision": 2}],
        gaps=["补齐真实状态到图形的映射"],
        changed_at="2026-10-01T09:00:00+08:00",
    )
    assert summarized["proactive_research_authorized"] is False
    assert summarized["result"]["unknowns"] == ["补齐真实状态到图形的映射"]
    assert summarized["topic"]["topic_id"] == first["topic"]["topic_id"]


def test_period_rejects_cross_topic_and_outside_window_sources(tmp_path: Path) -> None:
    service = library(tmp_path)
    first = save(service)
    other = result()
    other["topic"]["title"] = "另一主题"
    second = save(service, run_id="another-topic", result=other)
    arguments = dict(
        period="week",
        start="2026-09-01T00:00:00+08:00",
        end="2026-09-08T00:00:00+08:00",
        topic_id=first["topic"]["topic_id"],
        title="周主题",
        summary="当前归纳",
        gaps=[],
    )
    with pytest.raises(KnowledgeLibraryError, match="period_topic_mismatch"):
        service.save_period_summary(
            **arguments, knowledge_refs=[{"knowledge_id": second["knowledge_id"], "revision": 1}]
        )
    with pytest.raises(KnowledgeLibraryError, match="period_reference_invalid"):
        service.save_period_summary(
            **{**arguments, "start": "2026-09-02T00:00:00+08:00"},
            knowledge_refs=[{"knowledge_id": first["knowledge_id"], "revision": 1}],
        )


def test_index_failure_after_commit_is_recoverable_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = library(tmp_path)
    original = service._write_index
    monkeypatch.setattr(
        service, "_write_index", lambda: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        save(service)
    monkeypatch.setattr(service, "_write_index", original)
    replay = save(service)
    assert replay["idempotent"] is True and replay["revision"] == 1
    assert (service.root / "目录.md").exists()


def test_modified_note_preserved_and_reported_not_overwritten(tmp_path: Path) -> None:
    service = library(tmp_path)
    first = save(service)
    path = service.vault_root / first["path"]
    path.write_text("user added content")
    with pytest.raises(KnowledgeLibraryError, match="knowledge_note_modified"):
        save(service)
    assert path.read_text() == "user added content"


def test_symlink_asset_root_and_revision_rejected(tmp_path: Path) -> None:
    service = library(tmp_path)
    alias = service.vault_root / "alias"
    alias.symlink_to(service.assets_root, target_is_directory=True)
    with pytest.raises(ProductReviewError, match="assets_root_unsafe"):
        KnowledgeLibrary(alias, vault_root=service.vault_root)
    saved = save(service)
    directory = (service.vault_root / saved["path"]).parent
    moved = directory.with_name("moved")
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ProductReviewError, match="revision_directory_unsafe"):
        service.read(saved["knowledge_id"])


def test_scan_300_notes_keeps_large_small_topic_index_and_full_read(tmp_path: Path) -> None:
    service = library(tmp_path)
    for number in range(300):
        answer = deepcopy(result())
        answer["summary"] = f"第 {number} 条合成规模样本"
        save(service, run_id=f"synthetic-{number}", fragment_id=f"fragment-{number}", result=answer)
    service.rebuild_index()
    assert len(service.catalog()) == 1
    assert len(service.catalog()[0]["notes"]) == 300
    matches = service.search("第 299 条合成规模样本")
    assert len(matches) == 1 and matches[0]["result"]["agent_usage"]["steps"]


def test_subscription_record_keeps_existing_digest_and_full_document(tmp_path: Path) -> None:
    from fragment_loop.subscription_research import SubscriptionResearch

    text = "研究原文实质材料。" * 1000
    record = SubscriptionResearch._record(
        "https://example.org/lifecycle",
        text,
        {
            "fetched_at": "2026-09-01T00:00:00+00:00",
            "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
        },
        title="完整原文",
    )
    answer = result()
    for item in answer["confirmed"] + answer["coverage"]:
        item["evidence_ids"] = [record["evidence_id"]]
    answer["claims"][0]["evidence_id"] = record["evidence_id"]
    saved = save(library(tmp_path), result=answer, evidence=[record])
    source = saved["evidence"][0]
    assert source["excerpts"] == [text]
    assert source["evidence_digest"] == record["evidence_digest"]
    assert source["page_digest"] == record["page_digest"]
    assert source["provenance"] == record["provenance"]


def test_later_renderer_change_does_not_mislabel_immutable_note_as_tampered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = library(tmp_path)
    saved = save(service)
    monkeypatch.setattr(service, "_render", lambda doc: b"future renderer format")
    loaded = service.read(saved["knowledge_id"])
    assert loaded["note_sha256"] == saved["note_sha256"]
    assert loaded["revision"] == 1


def test_trial_argv_is_preserved_and_only_formatted_for_display(tmp_path: Path) -> None:
    store = library(tmp_path)
    trial = {
        **evidence()[0],
        "source_type": "repository_trial",
        "revision": "abc123",
        "command": ["node", "bin/archify.mjs", "validate", "example space.json"],
        "exit_code": 0,
        "output_digest": "a" * 64,
    }
    saved = save(store, evidence=[trial])
    assert saved["evidence"][0]["argv"] == trial["command"]
    assert saved["evidence"][0]["command"] == "node bin/archify.mjs validate 'example space.json'"


def test_plain_text_url_scheme_is_data_not_an_active_markdown_link(tmp_path: Path) -> None:
    answer = result()
    answer["answer_markdown"] = "研究 data: 与 file: 形式的区别，不执行任何命令。"
    saved = save(library(tmp_path), result=answer)
    assert "data:" in saved["result"]["answer_markdown"]


def test_publication_receipt_validates_file_and_identity_without_repair(tmp_path: Path) -> None:
    store = library(tmp_path)
    doc = save(store)
    receipt = {key: doc[key] for key in ("knowledge_id", "revision", "path", "run_id")}
    assert store.validate_publication(receipt)["note_sha256"] == doc["note_sha256"]
    with pytest.raises(KnowledgeLibraryError, match="publication_path_mismatch"):
        store.validate_publication({**receipt, "path": "other.md"})
    with pytest.raises(KnowledgeLibraryError, match="publication_binding_mismatch"):
        store.validate_publication({**receipt, "run_id": "another-run"})
    path = store.vault_root / doc["path"]
    path.write_text("用户修订")
    with pytest.raises(KnowledgeLibraryError, match="knowledge_note_modified"):
        store.validate_publication(receipt)
    assert path.read_text() == "用户修订"


def test_one_modified_note_does_not_block_other_topics_or_hide_integrity_issue(
    tmp_path: Path,
) -> None:
    store = library(tmp_path)
    broken = save(store)
    path = store.vault_root / broken["path"]
    path.write_text("用户有意修改")
    other = result()
    other["topic"]["title"] = "另一正常主题"
    healthy = save(store, run_id="healthy-run", result=other)
    assert store.read(healthy["knowledge_id"])["revision"] == 1
    assert len(store.catalog()) == 1
    assert store.read_issues() == [
        {
            "knowledge_id": broken["knowledge_id"],
            "reason": "knowledge_note_modified",
            "path": broken["path"],
        }
    ]
    assert "无法读取的既有记录" in (store.root / "目录.md").read_text()
    with pytest.raises(KnowledgeLibraryError, match="knowledge_note_modified"):
        store.read(broken["knowledge_id"])
    assert path.read_text() == "用户有意修改"


def test_mermaid_arrows_survive_safe_markdown_storage(tmp_path: Path) -> None:
    store = library(tmp_path)
    answer = result()
    answer["answer_markdown"] = "```mermaid\nflowchart LR\nA[输入] --> B[验证]\nB --> C[结论]\n```"
    doc = save(store, result=answer)
    text = (store.vault_root / doc["path"]).read_text()
    assert "A[输入] --> B[验证]" in text
    assert "--&gt;" not in text


def topic_summary(store: KnowledgeLibrary, source: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return store.save_topic_summary(
        topic_id=source["topic"]["topic_id"],
        title="主题结构测试",
        summary="结构层不应替代研究原结论",
        knowledge_refs=[{"knowledge_id": source["knowledge_id"], "revision": source["revision"]}],
        gaps=["仍需核实实际条件"],
        analysis={},
        **kwargs,
    )


def test_research_revision_invalidates_derived_but_preserves_original_bytes(tmp_path: Path) -> None:
    store = library(tmp_path)
    first = save(store)
    theme = topic_summary(store, first)
    period = store.save_period_summary(
        period="week",
        start="2026-08-31T00:00:00+08:00",
        end="2026-09-07T00:00:00+08:00",
        topic_id=first["topic"]["topic_id"],
        title="上周结构",
        summary="待源研究复核",
        knowledge_refs=[{"knowledge_id": first["knowledge_id"], "revision": 1}],
        gaps=[],
    )
    original = {
        row["knowledge_id"]: (store.vault_root / row["path"]).read_bytes()
        for row in (first, theme, period)
    }
    changed = result()
    changed["recommendation"] = "旧的无条件建议已撤回，个人场景尚未核实。"
    second = save(
        store, result=changed, expected_revision=1, changed_at="2026-09-08T08:00:00+08:00"
    )
    for doc in (theme, period):
        loaded = store.read(doc["knowledge_id"])
        assert loaded["freshness"]["status"] == "stale"
        assert loaded["usable_as_current"] is False
        canonical = loaded["canonical_research"]
        assert len(canonical) == 1 and canonical[0]["revision"] == 2
        assert canonical[0]["result"]["recommendation"] == changed["recommendation"]
        assert canonical[0]["usable_as_current"] is True
        assert canonical[0]["conclusion_authority"] == "research_result"
        assert (store.vault_root / doc["path"]).read_bytes() == original[doc["knowledge_id"]]
    assert (store.vault_root / first["path"]).read_bytes() == original[first["knowledge_id"]]
    assert store.read(first["knowledge_id"], 1)["freshness"]["status"] == "stale"
    assert store.search("结构层") == []
    assert store.search("结构层", include_unusable=True)[0]["freshness"]["status"] == "stale"
    assert "已过期，勿作当前结论" in (store.root / "目录.md").read_text()
    refreshed = topic_summary(store, second, expected_revision=1)
    assert refreshed["knowledge_id"] == theme["knowledge_id"] and refreshed["revision"] == 2
    current = store.read(theme["knowledge_id"])
    assert current["freshness"]["status"] == "current" and current["usable_as_current"] is False
    assert current["conclusion_authority"] == "canonical_research"
    assert store.search("结构层") == []


def test_old_policy_summary_v2_is_stale_even_when_references_are_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = library(tmp_path)
    source = save(store)
    with monkeypatch.context() as old:
        old.setattr("fragment_loop.knowledge_library.DERIVATION_POLICY_VERSION", "legacy-structure")
        first = topic_summary(store, source)
        second = store.save_topic_summary(
            topic_id=source["topic"]["topic_id"],
            title="主题结构测试",
            summary="旧策略仍强化未核实判断",
            knowledge_refs=[{"knowledge_id": source["knowledge_id"], "revision": 1}],
            gaps=[],
            analysis={},
            expected_revision=1,
        )
    loaded = store.read(second["knowledge_id"])
    assert second["revision"] == 2 and loaded["freshness"]["status"] == "stale"
    assert loaded["freshness"]["reasons"] == ["结构整理策略已更新，等待重新综合"]
    assert loaded["canonical_research"][0]["revision"] == source["revision"]
    assert store.history(first["knowledge_id"])[0]["freshness"]["status"] == "stale"


def test_unreviewed_research_is_visible_but_not_reliable_input(tmp_path: Path) -> None:
    store = library(tmp_path)
    source = save(store, independent_review=None)
    read = store.read(source["knowledge_id"])
    assert read["freshness"]["status"] == "unreviewed" and not read["usable_as_current"]
    assert store.catalog()[0]["notes"][0]["freshness"]["status"] == "unreviewed"
    assert store.search("Archify") == []
    assert len(store.search("Archify", include_unusable=True)) == 1
    material = store.period_input(
        start="2026-09-01T00:00:00+08:00", end="2026-09-07T00:00:00+08:00"
    )
    assert (
        material["notes"] == []
        and material["excluded_notes"][0]["knowledge_id"] == source["knowledge_id"]
    )
    with pytest.raises(KnowledgeLibraryError, match="summary_source_not_current"):
        topic_summary(store, source)


def test_invalidated_source_cannot_be_published_after_model_finishes(tmp_path: Path) -> None:
    store = library(tmp_path)
    old_input = save(store)
    amended = result()
    amended["recommendation"] = "条件更新"
    save(store, result=amended, expected_revision=1)
    with pytest.raises(KnowledgeLibraryError, match="summary_source_not_current"):
        topic_summary(store, old_input)
    assert all(note["kind"] == "research" for note in store.catalog()[0]["notes"])


def test_derived_dependency_damage_is_stale_without_hiding_healthy_catalog(tmp_path: Path) -> None:
    store = library(tmp_path)
    source = save(store)
    theme = topic_summary(store, source)
    (store.vault_root / source["path"]).write_text("用户修订的文件，不得覆盖")
    loaded = store.read(theme["knowledge_id"])
    assert loaded["freshness"]["status"] == "stale"
    assert loaded["canonical_research"] == []
    assert (
        len(store.catalog()) == 1
        and store.read_issues()[0]["knowledge_id"] == source["knowledge_id"]
    )


@pytest.mark.parametrize("normalization", ["closed_final_object", "opened_array_object"])
def test_review_structure_recovery_preserves_source_digest(
    tmp_path: Path, normalization: str
) -> None:
    store = library(tmp_path)
    receipt = {
        **review(result()),
        "output_normalization": normalization,
        "source_output_sha256": "d" * 64,
    }
    saved = save(store, independent_review=receipt)
    assert (
        store.read(saved["knowledge_id"])["independent_review"]["source_output_sha256"] == "d" * 64
    )
    assert "未补写字段或事实" in (store.vault_root / saved["path"]).read_text()
    with pytest.raises(KnowledgeLibraryError, match="normalization_invalid"):
        save(store, independent_review={**receipt, "source_output_sha256": "invalid"})


def test_read_time_dependency_cycle_and_wide_graph_fail_closed_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fragment_loop.knowledge_library import DERIVATION_POLICY_VERSION

    store = library(tmp_path)
    documents = {
        name: {
            "knowledge_id": name,
            "revision": 1,
            "kind": "topic_summary",
            "derivation_policy_version": DERIVATION_POLICY_VERSION,
            "knowledge_refs": [{"knowledge_id": target, "revision": 1}],
        }
        for name, target in (("a", "b"), ("b", "a"))
    }
    calls = []

    def read_raw(identifier: str, revision: int | None = None) -> dict[str, Any]:
        calls.append(identifier)
        return documents[identifier]

    monkeypatch.setattr(store, "_read_raw", read_raw)
    assert store._with_freshness(documents["a"])["freshness"]["status"] == "stale"
    assert len(calls) <= 6
    source = {
        "knowledge_id": "research",
        "revision": 1,
        "kind": "research",
        "independent_review": {"fixture": True},
    }
    documents["research"] = source
    documents["wide"] = {
        **documents["a"],
        "knowledge_id": "wide",
        "knowledge_refs": [{"knowledge_id": "research", "revision": 1} for _ in range(1100)],
    }
    calls.clear()
    wide = store._with_freshness(documents["wide"])
    assert wide["freshness"]["status"] == "stale"
    assert "来源依赖检查数量超限" in wide["freshness"]["reasons"]
    assert len(calls) <= 1026


@pytest.mark.parametrize("runtime, flag", [("node", "-e"), ("python3", "-c")])
def test_maximum_quoted_trial_fixture_preserves_exact_argv(
    tmp_path: Path, runtime: str, flag: str,
) -> None:
    # Synthetic receipt: no fixture or external tool is executed by this test.
    fixture = "#" + "'" * 3998 + " "
    assert len(fixture) == 4000
    argv = [runtime, flag, fixture]
    trial = {
        **evidence()[0], "source_type": "repository_trial", "revision": "abc123",
        "command": argv, "exit_code": 0, "output_digest": "a" * 64,
    }
    store = library(tmp_path)
    saved = save(store, evidence=[trial])
    loaded = store.read(saved["knowledge_id"])["evidence"][0]
    assert loaded["argv"] == argv
    assert loaded["argv"][2].endswith(" ")
    assert len(loaded["command"]) > 19000
    assert shlex.split(loaded["command"]) == argv
    assert loaded["output_digest"] == trial["output_digest"]
    with pytest.raises(KnowledgeLibraryError, match="trial_argv_invalid"):
        save(store, run_id="too-long", evidence=[{**trial, "command": [runtime, flag,
                                                                    fixture + "x"]}])
    with pytest.raises(KnowledgeLibraryError, match="evidence_record_too_large"):
        save(store, run_id="too-large", evidence=[{**trial, "excerpts": ["x" * 50000] * 4}])


@pytest.mark.parametrize("answer", [
    '```python\nhtml = Markup(\'<article><a href="{}">{}</a></article>\')\n```',
    '~~~python\nvalue = "<script>alert(1)</script> &lt;"\n~~~~\t',
    '````python\n```literal\n<img onerror="alert(1)">\n`````',
    '```python\nprint("<x>")\n   ```\t',
    'Use `<article>` and `` ` <tag>&lt; `` exactly.',
    r'Escaped \` text and `<safe>`.',
    r'Even \\`<safe>` is a code span.',
    r'Escaped first tick \``<safe>`.',
])
def test_markdown_code_domains_preserve_exact_source(tmp_path: Path, answer: str) -> None:
    source = result()
    source["answer_markdown"] = answer
    saved = save(library(tmp_path), result=source)
    assert saved["result"]["answer_markdown"] == answer
    markdown = (tmp_path / "vault" / saved["path"]).read_text()
    assert answer in markdown
    assert "## 已知限制" in markdown and "## 本题尚未解决的问题" in markdown


@pytest.mark.parametrize(("answer", "error"), [
    ('```python\n<img onerror="alert(1)">', "fence_not_closed"),
    ('````python\n<img>\n```', "fence_not_closed"),
    ('```python\n<img>\n~~~', "fence_not_closed"),
    ('```python`x\n<img>\n```', "fence_info_invalid"),
    ('~~~{onclick=alert(1)}\n<img>\n~~~', "fence_info_invalid"),
    ('```<img>\ntext\n```', "fence_info_invalid"),
    (' ```python\n<img>\n ```', "fence_container_unsupported"),
    ('> ~~~\n> <img>\n> ~~~', "fence_container_unsupported"),
    ('- ```python\n  <img>\n  ```', "fence_container_unsupported"),
    ('Use `<img>\nonerror=1>`.', "span_not_closed_on_line"),
    ('<!-- ` -->\n<img onerror=1>\n<!-- ` -->', "span_not_closed_on_line"),
])
def test_ambiguous_markdown_code_is_rejected_before_publication(
    tmp_path: Path, answer: str, error: str,
) -> None:
    store = library(tmp_path)
    source = result()
    source["answer_markdown"] = answer
    with pytest.raises(KnowledgeLibraryError, match=error):
        save(store, result=source)
    assert store.catalog() == []


@pytest.mark.parametrize("answer", [
    '[bad](javascript:alert(1))',
    '[bad](<data:text/html,evil>)',
    '[x]: file:/etc/passwd',
    '```text\n[bad](vbscript:evil)\n```',
    '`[bad](javascript:alert(1))`',
    '`safe` [bad](javascript:alert(1)) `<safe>`',
])
def test_code_detection_never_masks_active_markdown_urls(tmp_path: Path, answer: str) -> None:
    source = result()
    source["answer_markdown"] = answer
    with pytest.raises(KnowledgeLibraryError, match="active_markdown_url_forbidden"):
        save(library(tmp_path), result=source)


def test_html_starters_cannot_span_preserved_inline_code(tmp_path: Path) -> None:
    source = result()
    source["answer_markdown"] = '<a title="`<img onerror=1>`">text</a>\n<!-- `safe` -->'
    saved = save(library(tmp_path), result=source)
    answer = saved["result"]["answer_markdown"]
    assert '&lt;a title="`<img onerror=1>`">text&lt;/a&gt;' in answer
    assert '&lt;!-- `safe` -->' in answer
    assert '<a ' not in answer and '<!--' not in answer


def test_same_reviewed_source_republishes_code_fix_with_immutable_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fragment_loop.knowledge_library as module

    store = library(tmp_path)
    source = result()
    source["answer_markdown"] = "```python\nhtml = '<article>{}</article>'\n```"
    raw_digest = _digest(source)
    original_markdown = module._markdown
    monkeypatch.setattr(module, "_markdown", lambda value, **_kwargs: re.sub(
        r"<(?=[A-Za-z!/])[^>]*>", lambda match: html.escape(match.group(), quote=False), value
    ))
    first = save(store, result=source)
    old_path = store.vault_root / first["path"]
    old_bytes = old_path.read_bytes()
    monkeypatch.setattr(module, "_markdown", original_markdown)
    second = save(
        store, result=source, expected_revision=1,
        revision_reason="仅修复 Markdown 代码保真，沿用原研究与复核",
    )
    assert second["revision"] == 2
    assert second["previous_sha256"] == first["content_sha256"]
    assert second["independent_review"] == first["independent_review"]
    assert second["independent_review"]["reviewed_result_digest"] == raw_digest
    assert _digest(source) == raw_digest
    assert second["result"]["answer_markdown"] == source["answer_markdown"]
    assert old_path.read_bytes() == old_bytes
    assert store.read(first["knowledge_id"], 1)["note_sha256"] == first["note_sha256"]
    assert store.read(first["knowledge_id"])["revision"] == 2
    repeated = save(store, result=source, expected_revision=1)
    assert repeated["revision"] == 2 and repeated["idempotent"]
    assert len(store.history(first["knowledge_id"])) == 2
    with pytest.raises(KnowledgeLibraryError, match="independent_review_result_mismatch"):
        save(store, result=first["result"], independent_review=review(source), expected_revision=2)


@pytest.mark.parametrize("separator", ["\v", "\f", "\x85", "\u2028", "\u2029"])
def test_only_commonmark_line_endings_can_open_a_fence(tmp_path: Path, separator: str) -> None:
    source = result()
    source["answer_markdown"] = f'before{separator}```python\n<img onerror=1>\n```'
    with pytest.raises(KnowledgeLibraryError, match="code_span_not_closed_on_line"):
        save(library(tmp_path), result=source)


def test_rendered_list_prefix_cannot_turn_metadata_into_unsafe_code(tmp_path: Path) -> None:
    source = result()
    source["confirmed"][0]["claim"] = '```python\n<img onerror=1>\n```'
    saved = save(library(tmp_path), result=source)
    rendered = (tmp_path / "vault" / saved["path"]).read_text()
    assert '<img' not in rendered
    assert '&lt;img' in rendered


@pytest.mark.parametrize("language", ["dataview", "dataviewjs", "DataviewJS", "unknown-plugin"])
def test_host_plugin_fence_languages_are_rejected(tmp_path: Path, language: str) -> None:
    source = result()
    source["answer_markdown"] = f'```{language}\nfixture_only\n```'
    with pytest.raises(KnowledgeLibraryError, match="markdown_code_language_forbidden"):
        save(library(tmp_path), result=source)


@pytest.mark.parametrize("answer", [
    '`= fixture_only`', '` $= fixture_only`', '``\n $= fixture_only ``',
    '```text\n\n\t= fixture_only\n```', '~~~python\n  $= fixture_only\n~~~',
])
def test_dataview_inline_and_codeblock_queries_are_rejected(tmp_path: Path, answer: str) -> None:
    source = result()
    source["answer_markdown"] = answer
    with pytest.raises(KnowledgeLibraryError, match="active_markdown_code_forbidden"):
        save(library(tmp_path), result=source)


@pytest.mark.parametrize("field", ["summary", "recommendation", "unknowns", "confirmed", "topic"])
def test_metadata_cannot_smuggle_host_plugin_inline_code(tmp_path: Path, field: str) -> None:
    source = result()
    active = '`  = fixture_only`'
    if field == 'unknowns':
        source[field] = [active]
    elif field == 'confirmed':
        source[field][0]['claim'] = active
    elif field == 'topic':
        source[field]['title'] = active
    else:
        source[field] = active
    with pytest.raises(KnowledgeLibraryError, match="active_markdown_code_forbidden"):
        save(library(tmp_path), result=source)


def test_generated_inline_command_wrapper_is_also_guarded(tmp_path: Path) -> None:
    record = {
        **evidence()[0], 'source_type': 'repository_trial', 'revision': 'abc123',
        'command': '= fixture_only', 'exit_code': 0, 'output_digest': 'a' * 64,
    }
    with pytest.raises(KnowledgeLibraryError, match="active_markdown_code_forbidden"):
        save(library(tmp_path), evidence=[record])


@pytest.mark.parametrize('language', ['python', 'javascript', 'html', 'mermaid'])
def test_static_display_fences_remain_available(tmp_path: Path, language: str) -> None:
    source = result()
    source['answer_markdown'] = f'```{language}\nfixture_only\n```'
    saved = save(library(tmp_path), result=source)
    assert saved['result']['answer_markdown'] == source['answer_markdown']


@pytest.mark.parametrize('answer', [
    '` \ufeff\t= fixture_only`', '```text\n\ufeff\n \ufeff\t$= fixture_only\n```',
])
def test_host_trim_bom_cannot_hide_active_code_prefix(tmp_path: Path, answer: str) -> None:
    source = result()
    source['answer_markdown'] = answer
    with pytest.raises(KnowledgeLibraryError, match='active_markdown_code_forbidden'):
        save(library(tmp_path), result=source)


def test_topic_inline_guard_runs_before_note_commit(tmp_path: Path) -> None:
    store = library(tmp_path)
    source = result()
    source['topic']['title'] = '`\n = fixture_only`'
    with pytest.raises(KnowledgeLibraryError, match='active_markdown_code_forbidden'):
        save(store, result=source)
    assert list(store.root.glob('knowledge-*/00000001')) == []


@pytest.mark.parametrize('excerpt', [
    '> ```dataviewjs\n> fixture_only\n> ```',
    '  ~~~text\n  \ufeff\n  = fixture_only\n  ~~~',
])
def test_nested_excerpt_fences_cannot_enable_host_plugins(tmp_path: Path, excerpt: str) -> None:
    record = evidence()[0]
    record['excerpts'] = [excerpt]
    with pytest.raises(KnowledgeLibraryError, match='(?:language_forbidden|code_forbidden)'):
        save(library(tmp_path), evidence=[record])


def test_historical_excerpt_container_fence_keeps_prose_compatibility(tmp_path: Path) -> None:
    record = evidence()[0]
    record['excerpts'] = ['  ```bash\n  fixture_only <sample>\n  ```']
    saved = save(library(tmp_path), evidence=[record])
    rendered = (tmp_path / 'vault' / saved['path']).read_text()
    assert '&lt;sample&gt;' in rendered


@pytest.mark.parametrize(('excerpt', 'error'), [
    ('> - > ```dataviewjs\n>   > fixture_only\n>   > ```', 'markdown_code_language_forbidden'),
    ('prefix\r~~~dataviewjs\rfixture_only\r~~~', 'excerpts_invalid'),
])
def test_nested_and_cr_metadata_fences_use_the_same_language_gate(
    tmp_path: Path, excerpt: str, error: str,
) -> None:
    record = evidence()[0]
    record['excerpts'] = [excerpt]
    with pytest.raises(KnowledgeLibraryError, match=error):
        save(library(tmp_path), evidence=[record])


def test_tab_list_tilde_fence_is_conservatively_rejected(tmp_path: Path) -> None:
    source = result()
    source['answer_markdown'] = '-\t~~~dataviewjs\n\tfixture_only\n\t~~~'
    with pytest.raises(KnowledgeLibraryError, match='markdown_code_fence_container_unsupported'):
        save(library(tmp_path), result=source)
