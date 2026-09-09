"""Isolated calendar replay, semantic-reference and restart/duplicate-send tests."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fragment_loop.knowledge_consolidation import KnowledgeConsolidation, _validate_summary
from fragment_loop.knowledge_library import KnowledgeLibraryError
from tests.test_knowledge_library import library, result, save


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


class FakeAgent:
    def __init__(self) -> None:
        self.packets: list[dict[str, Any]] = []
        self.mutate: Any = None

    def run(self, system: str, text: str, schema: Any, timeout_seconds: int) -> dict[str, Any]:
        assert "没有执行工具、外搜" in system
        packet = json.loads(text)
        self.packets.append(packet)
        refs = [
            {"knowledge_id": note["knowledge_id"], "revision": note["revision"]}
            for note in packet["research_notes"]
        ]
        previous = packet["previous_summary"]
        body = {
            "summary": "现有材料仅支持静态表达。",
            "known_structure": [
                {"statement": "已有输入契约、验证和展示三个环节。", "knowledge_refs": refs}
            ],
            "connections": (
                [{"statement": "两个研究在输入契约处衔接。", "knowledge_refs": refs}]
                if len(refs) > 1
                else []
            ),
            "gaps": [
                {
                    "goal": "补齐真实运行状态的映射",
                    "basis": "输入映射尚未验证",
                    "knowledge_refs": refs,
                }
            ],
            "revisions": [],
        }
        if previous:
            old_refs = {
                (ref["knowledge_id"], ref["revision"]) for ref in previous["knowledge_refs"]
            }
            new_refs = [
                ref for ref in refs if (ref["knowledge_id"], ref["revision"]) not in old_refs
            ]
            if new_refs:
                body["summary"] = "新增材料支持文件快照更新，仍须接入真实状态。"
                body["revisions"] = [
                    {
                        "previous_statement": previous["result"]["summary"],
                        "updated_statement": body["summary"],
                        "reason": "新研究提供了更新机制证据。",
                        "knowledge_refs": new_refs,
                    }
                ]
        outcome = {
            "status": "completed",
            "provider": "kimi_subscription",
            "request_sent": "true",
            "agent_invocations": 1,
            "model_calls": 1,
            "usage": {"input_tokens": 10},
            "result_json": body,
        }
        return self.mutate(outcome) if self.mutate else outcome


def test_topic_synthesis_has_no_self_trigger_and_exposes_goals_notification(tmp_path: Path) -> None:
    store = library(tmp_path)
    source = save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    completed = coordinator.scan_once(now=now)
    assert completed["status"] == "completed" and completed["kind"] == "topic"
    summary = store.read(completed["knowledge_id"])
    assert summary["kind"] == "topic_summary"
    assert summary["knowledge_refs"] == [{"knowledge_id": source["knowledge_id"], "revision": 1}]
    assert summary["goals"][0]["collection_mode"] == "natural_collection"
    assert summary["goals"][0]["proactive_research_authorized"] is False
    committed_mtime = coordinator.state_path.stat().st_mtime_ns
    for _ in range(3):
        assert coordinator.scan_once(now=now)["status"] == "idle"
    assert coordinator.state_path.stat().st_mtime_ns == committed_mtime
    assert len(agent.packets) == 1 and len(store.notifications()) == 1
    persisted = json.loads(coordinator.state_path.read_text())
    assert len(persisted["calls"]) == 1 and persisted["calls"][0]["status"] == "completed"
    assert persisted["calls"][0]["usage"] == {"input_tokens": 10}


def test_week_runs_at_monday_eight_and_month_runs_at_first_eight(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    before = at("2026-09-07T07:59:59+08:00")
    assert not any(job["kind"] == "week" for job in coordinator._jobs(before))
    coordinator.scan_once(now=before)
    completed = coordinator.scan_once(now=at("2026-09-07T08:00:00+08:00"))
    assert completed["kind"] == "week"
    week = store.read(completed["knowledge_id"])
    assert at(week["start"]) == at("2026-08-31T00:00:00+08:00")
    assert at(week["end"]) == at("2026-09-07T00:00:00+08:00")
    assert not any(
        job["kind"] == "month" for job in coordinator._jobs(at("2026-10-01T07:59:59+08:00"))
    )
    month = coordinator.scan_once(now=at("2026-10-01T08:00:00+08:00"))
    assert month["kind"] == "month"
    assert len(agent.packets) == 3


def test_cross_week_theme_revision_preserves_research_and_previous_summary(tmp_path: Path) -> None:
    store = library(tmp_path)
    original = save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    first = coordinator.scan_once(now=at("2026-09-02T08:00:00+08:00"))
    later = result()
    later["topic"] = {"existing_topic_id": original["topic"]["topic_id"]}
    new = save(
        store,
        run_id="third-week",
        fragment_id="third-week",
        result=later,
        changed_at="2026-09-21T08:00:00+08:00",
    )
    outputs = [coordinator.scan_once(now=at("2026-09-21T09:00:00+08:00")) for _ in range(4)]
    amended = store.read(first["knowledge_id"])
    assert amended["revision"] == 2
    assert amended["analysis"]["revisions"][0]["knowledge_refs"] == [
        {"knowledge_id": new["knowledge_id"], "revision": 1}
    ]
    assert store.read(first["knowledge_id"], 1)["result"]["summary"] == "现有材料仅支持静态表达。"
    assert len(store.history(original["knowledge_id"])) == 1
    assert any(item["kind"] == "topic_revised" for item in store.notifications())
    assert sum(item.get("kind") == "topic" for item in outputs) == 1


def test_failed_semantic_citation_is_bounded_and_never_published(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()

    def tamper(outcome: dict[str, Any]) -> dict[str, Any]:
        outcome["result_json"]["known_structure"][0]["knowledge_refs"][0]["knowledge_id"] = (
            "knowledge-" + "f" * 24
        )
        return outcome

    agent.mutate = tamper
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    assert coordinator.scan_once(now=now)["status"] == "failed"
    assert coordinator.scan_once(now=now + timedelta(minutes=59))["status"] == "idle"
    assert coordinator.scan_once(now=now + timedelta(hours=1))["status"] == "failed"
    assert coordinator.scan_once(now=now + timedelta(hours=2))["status"] == "idle"
    assert len(agent.packets) == 2
    assert store.notifications() == []
    assert len(json.loads(coordinator.state_path.read_text())["calls"]) == 2


def test_unknown_send_survives_restart_and_new_inputs_do_not_reset_it(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    agent.mutate = lambda outcome: {**outcome, "status": "failed", "request_sent": "unknown"}
    coordinator = KnowledgeConsolidation(store, agent)
    assert coordinator.scan_once(now=at("2026-09-02T08:00:00+08:00"))["status"] == "unknown_send"
    save(store, run_id="another", fragment_id="another", changed_at="2026-09-03T08:00:00+08:00")
    restarted = KnowledgeConsolidation(store, agent)
    assert restarted.scan_once(now=at("2026-09-03T09:00:00+08:00"))["status"] == "idle"
    assert len(agent.packets) == 1


def test_concurrent_scans_allow_only_one_call_even_for_different_topics(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    other = result()
    other["topic"]["title"] = "另一主题"
    save(store, run_id="other-topic", result=other)
    entered, finish = threading.Event(), threading.Event()
    agent = FakeAgent()

    def hold(outcome: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        assert finish.wait(5)
        return outcome

    agent.mutate = hold
    first = KnowledgeConsolidation(store, agent)
    second = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    with ThreadPoolExecutor(max_workers=2) as executor:
        running = executor.submit(first.scan_once, now=now)
        assert entered.wait(5)
        overlapping = second.scan_once(now=now)
        assert overlapping == {"status": "in_flight", "model_calls": 0}
        finish.set()
        assert running.result()["status"] == "completed"
    assert len(agent.packets) == 1


def test_interrupted_reservation_becomes_unknown_without_resend(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    job = coordinator._jobs(now)[0]
    coordinator._write(
        {
            "schema_version": "knowledge-consolidation-v1",
            "cursor": "",
            "calls": [],
            "jobs": {
                job["job_id"]: {
                    "status": "in_flight",
                    "input_digest": job["input_digest"],
                    "started_at": now.isoformat(),
                }
            },
        }
    )
    resumed = coordinator.scan_once(now=now + timedelta(minutes=6))
    assert resumed["status"] == "idle"
    assert resumed["outcomes"][0]["status"] == "unknown_send"
    assert agent.packets == []


def test_completed_model_result_republishes_after_disk_failure_without_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    original = store.save_topic_summary

    def unavailable(**kwargs: Any) -> dict[str, Any]:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_topic_summary", unavailable)
    now = at("2026-09-02T08:00:00+08:00")
    assert coordinator.scan_once(now=now)["status"] == "publication_pending"
    monkeypatch.setattr(store, "save_topic_summary", original)
    restarted = KnowledgeConsolidation(store, agent)
    finished = restarted.scan_once(now=now + timedelta(minutes=1))
    assert finished["status"] == "completed" and finished["model_calls"] == 0
    assert len(agent.packets) == 1


def test_summary_cannot_cite_itself_or_revise_without_new_evidence(tmp_path: Path) -> None:
    store = library(tmp_path)
    research = save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    completed = coordinator.scan_once(now=at("2026-09-02T08:00:00+08:00"))
    prior = store.read(completed["knowledge_id"])
    body = deepcopy(prior["analysis"])
    body["known_structure"][0]["knowledge_refs"] = [
        {"knowledge_id": prior["knowledge_id"], "revision": 1}
    ]
    with pytest.raises(KnowledgeLibraryError, match="references_not_closed"):
        _validate_summary(body, [research], prior)
    body = deepcopy(prior["analysis"])
    body["revisions"] = [
        {
            "previous_statement": prior["result"]["summary"],
            "updated_statement": "不能凭空修订",
            "reason": "无新证据",
            "knowledge_refs": [{"knowledge_id": research["knowledge_id"], "revision": 1}],
        }
    ]
    with pytest.raises(KnowledgeLibraryError, match="revision_requires_new_evidence"):
        _validate_summary(body, [research], prior)
    body["revisions"] = []
    body["connections"] = [
        {
            "statement": "不能一个研究自我关联",
            "knowledge_refs": [{"knowledge_id": research["knowledge_id"], "revision": 1}],
        }
    ]
    with pytest.raises(KnowledgeLibraryError, match="connection_requires_distinct_research"):
        _validate_summary(body, [research], prior)


def test_new_topic_inputs_do_not_starve_week_or_month(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store, changed_at="2026-08-12T08:00:00+08:00")
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    completed = []
    for number in range(5):
        save(
            store,
            run_id=f"incoming-{number}",
            fragment_id=f"incoming-{number}",
            changed_at=now.isoformat(),
        )
        output = coordinator.scan_once(now=now)
        if output["status"] == "completed":
            completed.append(output["kind"])
    assert "week" in completed and "month" in completed and "topic" in completed


def test_oversized_input_is_explicitly_blocked_before_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    monkeypatch.setattr("fragment_loop.knowledge_consolidation.MAX_INPUT_BYTES", 50)
    outcome = coordinator.scan_once(now=at("2026-09-02T08:00:00+08:00"))
    assert outcome["outcomes"][0]["status"] == "input_limit"
    assert agent.packets == []


def test_no_materials_and_naive_time_never_send_model(tmp_path: Path) -> None:
    store = library(tmp_path)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    assert coordinator.scan_once(now=at("2026-09-02T08:00:00+08:00"))["known_jobs"] == 0
    with pytest.raises(KnowledgeLibraryError, match="timestamp_timezone_required"):
        coordinator.scan_once(now=datetime(2026, 9, 2))
    assert agent.packets == []


def test_policy_migration_resynthesizes_once_with_full_review_and_canonical_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_knowledge_library import review

    store = library(tmp_path)
    answer = result()
    answer["unknowns"] = ["个人条件尚未核实，不能推出无需操作"]
    receipt = review(answer)
    receipt["verdict"] = "revised"
    receipt["findings"] = [
        {
            "statement": "无需立即处理",
            "issue": "来源未支持此适用条件",
            "correction": "应保持有条件判断",
            "evidence_ids": ["ev-example"],
        }
    ]
    source = save(store, result=answer, independent_review=receipt)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    with monkeypatch.context() as old:
        old.setattr("fragment_loop.knowledge_library.DERIVATION_POLICY_VERSION", "old-policy")
        old.setattr("fragment_loop.knowledge_consolidation.DERIVATION_POLICY_VERSION", "old-policy")
        first = coordinator.scan_once(now=now)
    old_bytes = (store.vault_root / store.read(first["knowledge_id"])["path"]).read_bytes()
    assert store.read(first["knowledge_id"])["freshness"]["status"] == "stale"
    second = coordinator.scan_once(now=now + timedelta(minutes=1))
    assert second["status"] == "completed" and second["revision"] == 2
    packet = agent.packets[-1]
    material = packet["research_notes"][0]
    assert material["independent_review"] == receipt
    assert material["result"]["confirmed"] == source["result"]["confirmed"]
    assert material["result"]["unknowns"] == answer["unknowns"]
    assert packet["conclusion_authority"] == "canonical_research"
    assert packet["previous_summary"]["is_evidence"] is False
    assert packet["previous_summary"]["freshness"]["status"] == "stale"
    current = store.read(first["knowledge_id"])
    assert current["freshness"]["status"] == "current" and current["usable_as_current"] is False
    assert current["canonical_research"][0]["independent_review"] == receipt
    assert (
        store.vault_root / store.read(first["knowledge_id"], 1)["path"]
    ).read_bytes() == old_bytes
    assert coordinator.scan_once(now=now + timedelta(minutes=2))["status"] == "idle"
    assert len(agent.packets) == 2


def test_failed_or_absent_review_does_not_reenter_topic_model(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store, independent_review=None)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    outcome = coordinator.scan_once(now=at("2026-10-01T08:00:00+08:00"))
    assert outcome["status"] == "idle" and outcome["known_jobs"] == 0
    assert agent.packets == []


def test_past_week_recomputes_from_current_correction_without_new_period_identity(
    tmp_path: Path,
) -> None:
    store = library(tmp_path)
    source = save(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-07T08:00:00+08:00")
    first_outputs = [coordinator.scan_once(now=now) for _ in range(2)]
    week = next(item for item in first_outputs if item["kind"] == "week")
    amended = result()
    amended["recommendation"] = "原建议撤回，补充适用条件"
    save(store, result=amended, expected_revision=1, changed_at="2026-09-08T08:00:00+08:00")
    assert store.read(week["knowledge_id"])["freshness"]["status"] == "stale"
    for _ in range(2):
        coordinator.scan_once(now=at("2026-09-08T09:00:00+08:00"))
    corrected_week = store.read(week["knowledge_id"])
    assert corrected_week["revision"] == 2 and corrected_week["freshness"]["status"] == "current"
    assert corrected_week["knowledge_refs"] == [
        {"knowledge_id": source["knowledge_id"], "revision": 2}
    ]
    week_packet = next(packet for packet in reversed(agent.packets) if packet["kind"] == "week")
    assert week_packet["research_notes"][0]["period_membership_revision"] == 1
    assert week_packet["research_notes"][0]["result"]["recommendation"] == amended["recommendation"]


def distinct_topics(store: Any) -> list[dict[str, Any]]:
    rows = []
    for number in range(3):
        value = result()
        value["topic"] = {
            "category": "科学与技术",
            "subcategory": f"方向{number}",
            "title": f"长期主题{number}",
        }
        rows.append(
            save(store, run_id=f"cross-{number}", fragment_id=f"cross-{number}", result=value)
        )
    return rows


def test_one_period_synthesizes_across_topics_without_inventing_a_topic(tmp_path: Path) -> None:
    store = library(tmp_path)
    research = distinct_topics(store)
    original_ids = {row["topic_id"] for row in store.catalog()}
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-07T08:00:00+08:00")
    jobs = coordinator._jobs(now)
    assert sum(job["kind"] == "topic" for job in jobs) == 3
    assert sum(job["kind"] == "week" for job in jobs) == 1
    for _ in range(4):
        assert coordinator.scan_once(now=now)["status"] == "completed"
    weeks = store.period_summaries()
    assert len(weeks) == 1 and weeks[0]["summary_scope"]["scope_id"] == "library"
    assert "topic" not in weeks[0]
    assert len(weeks[0]["canonical_research"]) == 3
    assert {ref["knowledge_id"] for ref in weeks[0]["knowledge_refs"]} == {
        row["knowledge_id"] for row in research
    }
    assert {row["topic_id"] for row in store.catalog()} == original_ids
    assert len(store.catalog()) == 3
    packet = next(packet for packet in agent.packets if packet["kind"] == "week")
    assert packet["summary_scope"] == "library" and packet["topic"] is None
    assert {row["topic"]["topic_id"] for row in packet["research_notes"]} == original_ids
    assert packet["preview"] is False
    assert "周/月总览（汇总范围，不计入知识主题）" in (store.root / "目录.md").read_text()
    assert store.search("现有材料") == []
    assert coordinator.scan_once(now=now)["status"] == "idle"


def test_preview_uses_current_window_and_separate_identity_without_claiming_due(
    tmp_path: Path,
) -> None:
    store = library(tmp_path)
    research = distinct_topics(store)
    original_catalog = store.catalog()
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T13:12:00+08:00")
    assert not any(job["kind"] in ("week", "month") for job in coordinator._jobs(now))
    week = coordinator.scan_once(now=now, preview_period="week")
    month = coordinator.scan_once(now=now, preview_period="month")
    assert week["status"] == month["status"] == "completed"
    assert len(agent.packets) == 2 and all(packet["preview"] for packet in agent.packets)
    assert all(at(packet["as_of"]) == now for packet in agent.packets)
    saved = store.read(week["knowledge_id"])
    assert saved["preview"] is True
    assert at(saved["start"]) == at("2026-08-31T00:00:00+08:00")
    assert at(saved["end"]) == at("2026-09-07T00:00:00+08:00")
    assert at(saved["updated_at"]) == now
    assert "验收预览" in saved["title"]
    assert "非已到期自然周期" in (store.vault_root / saved["path"]).read_text()
    assert store.period_summaries() == []
    assert len(store.period_summaries(include_previews=True)) == 2
    assert store.catalog() == original_catalog
    assert all(len(store.history(row["knowledge_id"])) == 1 for row in research)
    assert coordinator.scan_once(now=now, preview_period="week")["status"] == "idle"
    assert coordinator.scan_once(now=now, preview_period="month")["status"] == "idle"
    assert len(agent.packets) == 2
    future = at("2026-09-07T08:00:00+08:00")
    for _ in range(4):
        coordinator.scan_once(now=future)
    natural = store.period_summaries()
    assert len(natural) == 1 and natural[0]["knowledge_id"] != week["knowledge_id"]
    assert "preview" not in natural[0]
    with pytest.raises(KnowledgeLibraryError, match="preview_period_invalid"):
        coordinator.scan_once(now=now, preview_period="year")


def test_global_period_correction_keeps_period_identity_and_topic_ids(tmp_path: Path) -> None:
    store = library(tmp_path)
    sources = distinct_topics(store)
    agent = FakeAgent()
    coordinator = KnowledgeConsolidation(store, agent)
    now = at("2026-09-07T08:00:00+08:00")
    for _ in range(4):
        coordinator.scan_once(now=now)
    original = store.period_summaries()[0]
    original_topics = {row["topic_id"] for row in store.catalog()}
    amended = result()
    amended["topic"] = {"existing_topic_id": sources[0]["topic"]["topic_id"]}
    amended["recommendation"] = "当前研究限制已修订，不能沿用旧建议"
    save(
        store,
        run_id="cross-0",
        fragment_id="cross-0",
        result=amended,
        expected_revision=1,
        changed_at="2026-09-08T08:00:00+08:00",
    )
    assert store.read(original["knowledge_id"])["freshness"]["status"] == "stale"
    for _ in range(2):
        coordinator.scan_once(now=at("2026-09-08T09:00:00+08:00"))
    changed = store.period_summaries()[0]
    assert changed["knowledge_id"] == original["knowledge_id"] and changed["revision"] == 2
    assert {row["topic_id"] for row in store.catalog()} == original_topics
    assert any(
        ref["knowledge_id"] == sources[0]["knowledge_id"] and ref["revision"] == 2
        for ref in changed["knowledge_refs"]
    )
    assert len(store.history(original["knowledge_id"])) == 2


def test_three_hundred_month_inputs_keep_every_reference_with_explicit_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fragment_loop.knowledge_consolidation import MAX_INPUT_BYTES, SYSTEM_PROMPT

    store = library(tmp_path)
    source_ids = set()
    # Synthetic volume fixture; publication rules remain real, only derived index
    # writes are batched here to avoid 300 redundant test index rebuilds.
    with monkeypatch.context() as fast:
        fast.setattr(store, "_write_index", lambda: None)
        for index in range(300):
            item = result()
            item["topic"]["title"] = f"固定长期主题 {index % 8}"
            item["summary"] = "仅为隔离规模夹具，并非真实研究。" * 120
            item["answer_markdown"] = "正文不能默默丢失或冒充已读全文。" * 120
            source = save(store, run_id=f"volume-{index}", fragment_id=f"volume-{index}",
                          result=item,
                          changed_at=f"2026-09-{index % 28 + 1:02d}T08:00:00+08:00")
            source_ids.add(source["knowledge_id"])

    class VolumeAgent:
        packets: list[dict[str, Any]] = []
        input_bytes: list[int] = []

        def run(self, system: str, text: str, schema: Any,
                timeout_seconds: int) -> dict[str, Any]:
            packet = json.loads(text)
            self.packets.append(packet)
            self.input_bytes.append(len((system + text).encode()))
            assert self.input_bytes[-1] <= MAX_INPUT_BYTES
            assert packet["summary_only"] and packet["input_note_count"] == 300
            assert len(packet["research_notes"]) == 300
            assert len(packet["existing_topics"]) == 8
            assert {note["knowledge_id"] for note in packet["research_notes"]} == source_ids
            assert all(note["summary_only"] and note["details_omitted"]
                       and note["has_unknowns"] and note["truncated_fields"]
                       for note in packet["research_notes"])
            assert "不是完整证据" in packet["read_contract"]
            groups: dict[str, list[dict[str, Any]]] = {}
            for note in packet["research_notes"]:
                groups.setdefault(note["topic_id"], []).append({
                    "knowledge_id": note["knowledge_id"], "revision": note["revision"]})
            return {"status": "completed", "provider": "kimi_subscription",
                    "request_sent": True, "model_calls": 1, "agent_invocations": 1,
                    "result_json": {"summary": "模拟归纳：保留八个主题，并未验证语义质量。",
                                    "known_structure": [
                                        {"statement": "模拟既有主题归类；全体资料均有引用。",
                                         "knowledge_refs": refs} for refs in groups.values()],
                                    "connections": [], "gaps": [], "revisions": []}}

    agent = VolumeAgent()
    engine = KnowledgeConsolidation(store, agent)
    def unavailable(**kwargs: Any) -> dict[str, Any]:
        raise OSError("synthetic publication interruption")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(store, "save_period_summary", unavailable)
        pending = engine.scan_once(now=at("2026-09-30T12:00:00+08:00"), preview_period="month")
        assert pending["status"] == "publication_pending"
    completed = KnowledgeConsolidation(store, agent).scan_once(
        now=at("2026-09-30T12:01:00+08:00"), preview_period="month")
    assert completed["status"] == "completed" and completed["model_calls"] == 0
    summary = store.read(completed["knowledge_id"])
    assert len(summary["knowledge_refs"]) == len(summary["canonical_research"]) == 300
    assert summary["freshness"]["status"] == "current"
    assert summary["usable_as_current"] is False
    assert summary["input_summary_only"] is True
    assert "所有研究身份均保留" in (store.vault_root / summary["path"]).read_text()
    assert len(store.catalog()) == 8
    assert all(store.read(identifier)["result"]["answer_markdown"].count("正文") == 120
               for identifier in source_ids)
    assert len(agent.packets) == 1
    assert "summary_only" in SYSTEM_PROMPT
    (tmp_path / "scale-proof.json").write_text(json.dumps({
        "fixture": "synthetic-only; not live semantic acceptance", "research_count": 300,
        "topic_count": 8, "input_bytes": agent.input_bytes[0], "input_limit": MAX_INPUT_BYTES,
        "omitted_research_count": 0, "source_refs": len(summary["knowledge_refs"]),
        "canonical_refs": len(summary["canonical_research"]),
        "mock_model_calls": len(agent.packets), "real_model_calls": 0,
        "publication_recovery_calls": completed["model_calls"],
        "input_summary_only": summary["input_summary_only"],
        "freshness": summary["freshness"], "usable_as_current": summary["usable_as_current"],
    }, ensure_ascii=False, indent=2))


def test_existing_goal_id_survives_paraphrase_new_period_and_omission(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    engine = KnowledgeConsolidation(store, agent)
    first = engine.scan_once(now=at("2026-09-02T08:00:00+08:00"), preview_period="week")
    old = store.read(first["knowledge_id"])
    old_id = old["goals"][0]["goal_id"]
    save(store, run_id="later-week", fragment_id="later-week",
         changed_at="2026-09-15T08:00:00+08:00")

    def paraphrase(outcome: dict[str, Any]) -> dict[str, Any]:
        gap = outcome["result_json"]["gaps"][0]
        gap.update(goal="验证当前运行状态与展示快照之间的对应关系", existing_goal_id=old_id)
        return outcome

    agent.mutate = paraphrase
    second = engine.scan_once(now=at("2026-09-20T08:00:00+08:00"), preview_period="month")
    assert second["status"] == "completed"
    month = store.read(second["knowledge_id"])
    assert month["goals"][0]["goal_id"] == old_id
    assert month["goals"][0]["knowledge_refs"]
    assert agent.packets[-1]["existing_goals"][0]["goal_id"] == old_id
    assert agent.packets[-1]["as_of_local"] == "2026-09-20T08:00:00+08:00"
    assert month["goals"][0]["goal"] != old["goals"][0]["goal"]
    save(store, run_id="third-week-goal", fragment_id="third-week-goal",
         changed_at="2026-09-21T08:00:00+08:00")

    def omit(outcome: dict[str, Any]) -> dict[str, Any]:
        outcome["result_json"]["gaps"] = []
        return outcome

    agent.mutate = omit
    engine.scan_once(now=at("2026-09-22T08:00:00+08:00"), preview_period="month")
    current = store.read(second["knowledge_id"])
    assert current["revision"] == 2
    assert current["analysis"]["gaps"] == []
    assert [goal["goal_id"] for goal in current["goals"]] == [old_id]
    assert current["goals"][0]["priority"] is None
    assert not current["goals"][0]["proactive_research_authorized"]
    assert store.read(first["knowledge_id"])["goals"][0]["goal"] == old["goals"][0]["goal"]


def test_fabricated_goal_identity_is_rejected_before_publication(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()

    def forge(outcome: dict[str, Any]) -> dict[str, Any]:
        outcome["result_json"]["gaps"][0]["existing_goal_id"] = "goal-" + "a" * 24
        return outcome

    agent.mutate = forge
    engine = KnowledgeConsolidation(store, agent)
    failed = engine.scan_once(now=at("2026-09-02T08:00:00+08:00"), preview_period="month")
    assert failed["status"] == "failed" and failed["error_category"] == "existing_goal_invalid"
    assert store.period_summaries(include_previews=True) == []
    assert len(agent.packets) == 1


def test_revision_choices_match_exact_validator_contract() -> None:
    from fragment_loop.knowledge_consolidation import _previous_statements

    old_ref = {"knowledge_id": "knowledge-old", "revision": 1}
    new_ref = {"knowledge_id": "knowledge-new", "revision": 1}
    prior = {
        "result": {"summary": " 原摘要\n保留完整限定条件。 ", "recommendation": "原建议。"},
        "analysis": {
            "known_structure": [{"statement": "原结构。"}, {"statement": "原建议。"}],
            "connections": [{"statement": "原联系。"}],
            "revisions": [{"updated_statement": "上一轮修订后的判断。"}],
        },
        "knowledge_refs": [old_ref],
    }
    choices = _previous_statements(prior)
    assert choices == [
        " 原摘要\n保留完整限定条件。 ", "原建议。", "原结构。", "原联系。", "上一轮修订后的判断。",
    ]
    body = {
        "summary": "当前有条件结论。",
        "known_structure": [{"statement": "当前结构。", "knowledge_refs": [new_ref]}],
        "connections": [], "gaps": [],
        "revisions": [{"previous_statement": "", "updated_statement": "修订后。",
                       "reason": "新增证据。", "knowledge_refs": [new_ref]}],
    }
    for choice in choices:
        body["revisions"][0]["previous_statement"] = choice
        assert _validate_summary(body, [new_ref], prior) == body
    for invalid in ("原摘要", choices[0].strip(), "改写后的原结构。", choices[1] + choices[2]):
        body["revisions"][0]["previous_statement"] = invalid
        with pytest.raises(KnowledgeLibraryError, match="previous_statement_not_found"):
            _validate_summary(body, [new_ref], prior)
    body["revisions"][0]["previous_statement"] = choices[0]
    with pytest.raises(KnowledgeLibraryError, match="previous_statement_not_found"):
        _validate_summary(body, [new_ref], None)
    assert _previous_statements(None) == []


def test_exact_revision_choices_survive_summary_only_without_clipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fragment_loop.knowledge_consolidation import (
        REVISION_QUOTE_CONTRACT,
        SYSTEM_PROMPT,
        _previous_statements,
    )

    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    engine = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    first = engine.scan_once(now=now)
    job = engine._jobs(now)[0]
    expected = _previous_statements(store.read(first["knowledge_id"]))
    full = json.loads(engine._packet(job))
    assert full["previous_statement_choices"] == expected
    assert full["revision_quote_contract"] == REVISION_QUOTE_CONTRACT
    assert "一条完整字符串" in SYSTEM_PROMPT and "不得摘录、拼接或改写" in SYSTEM_PROMPT
    job["notes"][0]["result"]["answer_markdown"] = "synthetic detail " * 2000
    monkeypatch.setattr("fragment_loop.knowledge_consolidation.MAX_INPUT_BYTES", 12000)
    compressed = json.loads(engine._packet(job))
    assert compressed["summary_only"]
    assert "analysis" not in compressed["previous_summary"]
    assert compressed["previous_statement_choices"] == expected
    job["prior"]["analysis"]["known_structure"][0]["statement"] = "字" * 5000
    with pytest.raises(KnowledgeLibraryError, match="consolidation_input_limit"):
        engine._packet(job)
    assert len(agent.packets) == 1


def legacy_quote_failure(
    tmp_path: Path, attempts: int,
) -> tuple[KnowledgeConsolidation, FakeAgent, datetime, dict[str, Any], str]:
    """Synthetic pre-fix ledger; never uses a real agent or production library."""
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    engine = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    engine.scan_once(now=now)
    save(store, run_id="quote-new", fragment_id="quote-new",
         changed_at="2026-09-02T08:01:00+08:00")

    def bad_quote(outcome: dict[str, Any]) -> dict[str, Any]:
        outcome["result_json"]["revisions"][0]["previous_statement"] = "现有材料仅支持"
        return outcome

    agent.mutate = bad_quote
    for attempt in range(attempts):
        failed_at = now + timedelta(minutes=2, hours=attempt)
        failed = engine.scan_once(now=failed_at)
        assert failed["error_category"] == "previous_statement_not_found"
    state = json.loads(engine.state_path.read_text())
    job_id = failed["job_id"]
    for entry in [*state["calls"], *state["jobs"].values()]:
        entry.pop("revision_quote_contract", None)
        entry.pop("contract_correction_of", None)
    engine._write(state)
    return engine, agent, failed_at, deepcopy(state), job_id


@pytest.mark.parametrize("attempts", [1, 2])
def test_legacy_quote_failure_gets_one_immediate_contract_repair(
    tmp_path: Path, attempts: int,
) -> None:
    from fragment_loop.knowledge_consolidation import REVISION_QUOTE_CONTRACT

    engine, agent, now, before, job_id = legacy_quote_failure(tmp_path, attempts)
    old = before["jobs"][job_id]
    packets_before = len(agent.packets)
    agent.mutate = None
    outcome = engine.scan_once(now=now + timedelta(seconds=1))
    assert outcome["status"] == "completed" and outcome["revision"] == 2
    after = json.loads(engine.state_path.read_text())
    assert after["calls"][:-1] == before["calls"]
    entry, call = after["jobs"][job_id], after["calls"][-1]
    assert entry["attempts"] == call["attempt"] == attempts + 1
    assert entry["input_digest"] == call["input_digest"] == old["input_digest"]
    assert entry["operation_id"] != old["operation_id"]
    assert entry["revision_quote_contract"] == call["revision_quote_contract"]
    assert call["revision_quote_contract"] == REVISION_QUOTE_CONTRACT
    assert entry["contract_correction_of"] == call["contract_correction_of"] == old["operation_id"]
    assert agent.packets[-1]["previous_statement_choices"]
    assert len(agent.packets) == packets_before + 1
    assert engine.scan_once(now=now + timedelta(hours=2))["status"] == "idle"
    assert len(agent.packets) == packets_before + 1


@pytest.mark.parametrize("attempts", [1, 2])
def test_failed_contract_repair_stops_without_another_send(tmp_path: Path, attempts: int) -> None:
    engine, agent, now, before, job_id = legacy_quote_failure(tmp_path, attempts)
    packets_before = len(agent.packets)
    failed = engine.scan_once(now=now + timedelta(seconds=1))
    assert failed["error_category"] == "previous_statement_not_found"
    for later in (timedelta(seconds=2), timedelta(hours=2), timedelta(days=1)):
        assert engine.scan_once(now=now + later)["status"] == "idle"
    after = json.loads(engine.state_path.read_text())
    assert after["calls"][:-1] == before["calls"]
    assert after["jobs"][job_id]["attempts"] == attempts + 1
    assert len(agent.packets) == packets_before + 1


@pytest.mark.parametrize("guard", [
    "unknown_job", "in_flight_job", "unknown_call", "in_flight_call", "no_call",
    "other_operation", "other_input", "other_call_error", "unknown_job_send",
    "unknown_call_send", "current_contract", "current_call_contract", "other_error",
    "existing_contract_attempt",
])
def test_contract_repair_requires_matching_closed_legacy_failure(
    tmp_path: Path, guard: str,
) -> None:
    from fragment_loop.knowledge_consolidation import REVISION_QUOTE_CONTRACT

    engine, agent, now, state, job_id = legacy_quote_failure(tmp_path, 1)
    entry, call = state["jobs"][job_id], state["calls"][-1]
    if guard == "unknown_job":
        entry["status"] = "unknown_send"
    elif guard == "in_flight_job":
        entry["status"] = "in_flight"
    elif guard == "unknown_call":
        call["status"] = "unknown_send"
    elif guard == "in_flight_call":
        call["status"] = "in_flight"
    elif guard == "no_call":
        state["calls"].pop()
    elif guard == "other_operation":
        call["operation_id"] = "other"
    elif guard == "other_input":
        call["input_digest"] = "other"
    elif guard == "other_call_error":
        call["error_category"] = "references_not_closed"
    elif guard == "unknown_job_send":
        entry["request_sent"] = "unknown"
    elif guard == "unknown_call_send":
        call["request_sent"] = "unknown"
    elif guard == "current_contract":
        entry["revision_quote_contract"] = REVISION_QUOTE_CONTRACT
    elif guard == "current_call_contract":
        call["revision_quote_contract"] = REVISION_QUOTE_CONTRACT
    elif guard == "existing_contract_attempt":
        state["calls"].append({**call, "operation_id": "corrected-operation",
                               "revision_quote_contract": REVISION_QUOTE_CONTRACT})
    else:
        entry["error_category"] = "references_not_closed"
    engine._write(state)
    packets_before = len(agent.packets)
    assert engine.scan_once(now=now + timedelta(seconds=1))["status"] in {"idle", "in_flight"}
    assert len(agent.packets) == packets_before


def test_concurrent_contract_repairs_append_only_one_call(tmp_path: Path) -> None:
    engine, agent, now, before, job_id = legacy_quote_failure(tmp_path, 2)
    entered, finish = threading.Event(), threading.Event()

    def hold(outcome: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        assert finish.wait(5)
        return outcome

    agent.mutate = hold
    other = KnowledgeConsolidation(engine.library, agent)
    with ThreadPoolExecutor(max_workers=2) as executor:
        running = executor.submit(engine.scan_once, now=now + timedelta(seconds=1))
        assert entered.wait(5)
        assert other.scan_once(now=now + timedelta(seconds=1))["status"] == "in_flight"
        finish.set()
        assert running.result()["status"] == "completed"
    after = json.loads(engine.state_path.read_text())
    assert after["calls"][:-1] == before["calls"]
    assert after["jobs"][job_id]["attempts"] == 3


def test_legacy_completed_job_is_not_regenerated_for_quote_contract(tmp_path: Path) -> None:
    store = library(tmp_path)
    save(store)
    agent = FakeAgent()
    engine = KnowledgeConsolidation(store, agent)
    now = at("2026-09-02T08:00:00+08:00")
    assert engine.scan_once(now=now)["status"] == "completed"
    state = json.loads(engine.state_path.read_text())
    for entry in [*state["calls"], *state["jobs"].values()]:
        entry.pop("revision_quote_contract", None)
        entry.pop("contract_correction_of", None)
    engine._write(state)
    before = engine.state_path.read_bytes()
    assert engine.scan_once(now=now + timedelta(seconds=1))["status"] == "idle"
    assert engine.state_path.read_bytes() == before
    assert len(agent.packets) == 1
