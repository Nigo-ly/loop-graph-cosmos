"""Behavioral tests for the subscription research path; no real model calls."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.governed_research import GovernedResearchRunner, GovernedResearchVerifyAdapter
from fragment_loop.subscription_research import SubscriptionResearch, validate_decision

from .test_fragment_governed_research import FakeFetchTransport, make_run

RUN = "exec:fragment-intent:test"


def empty_result() -> dict[str, Any]:
    return {
        "summary": "",
        "recommendation": "",
        "confirmed": [],
        "unknowns": [],
        "conflicts": [],
        "claims": [],
        "answer_markdown": "",
        "topic": {
            "category": "技术",
            "subcategory": "工具",
            "title": "架构可视化",
            "existing_topic_id": "",
        },
        "coverage": [],
        "agent_usage": {"when_to_use": "", "steps": [], "limitations": []},
    }


def answer(material: dict[str, Any]) -> dict[str, Any]:
    eid = material["evidence"][-1]["evidence_id"]
    result = empty_result()
    result.update(
        summary="现有示例说明可表达流程。",
        recommendation="限定试用。",
        confirmed=[{"claim": "说明中存在流程示例。", "evidence_ids": [eid]}],
        answer_markdown="结论：限定试用。\n\n尚未验证部署集成。",
        agent_usage={
            "when_to_use": "评估流程图工具时",
            "steps": ["依据已读示例判断基本表达能力"],
            "limitations": ["部署集成尚未验证"],
        },
        unknowns=["部署集成尚未验证"],
        coverage=[
            {
                "question": "有何依据",
                "answer": "示例内容",
                "evidence_ids": [eid],
                "status": "answered",
            }
        ],
    )
    return {"phase": "answer", "reason": "已有来源支持限定判断", "actions": [], "result": result}


class Agent:
    provider = "codex_subscription"

    def __init__(self, decisions: list[Any]):
        self.decisions = decisions
        self.inputs: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> dict[str, Any]:
        material = json.loads(kwargs["user_text"])
        self.inputs.append(material)
        next_decision = (
            {
                "verdict": "supported_with_limits",
                "reason": "隔离测试复核回执",
                "findings": [],
                "result": material["draft"],
            }
            if material.get("operation") == "independent_evidence_review"
            else self.decisions.pop(0)
        )
        if isinstance(next_decision, Exception):
            raise next_decision
        return {
            "status": "completed",
            "provider": self.provider,
            "model_calls": 1,
            "request_sent": "true",
            "agent_invocations": 1,
            "diagnostics": {"cli_exit_code": 0},
            "result_json": next_decision(material) if callable(next_decision) else next_decision,
        }


def setup(
    tmp_path: Path, agent: Agent
) -> tuple[GovernedResearchRunner, SubscriptionResearch, FakeFetchTransport]:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    make_run(store)
    fetch = FakeFetchTransport()
    runner = GovernedResearchRunner(
        store, collection_enabled=True, synthesis_enabled=False, fetch_transport=fetch
    )
    engine = SubscriptionResearch(agent, run_ids=frozenset([RUN]))
    runner.subscription_research = engine
    return runner, engine, fetch


def test_seed_then_actual_gap_read_and_replay_without_model_or_network(tmp_path: Path) -> None:
    ask = {
        "phase": "research",
        "reason": "首页不足以判断，需契约",
        "result": empty_result(),
        "actions": [
            {
                "kind": "read_url",
                "target": "https://example.com/schema",
                "argv": [],
                "reason": "读取实际结构",
            }
        ],
    }
    agent = Agent([ask, answer])
    runner, engine, fetch = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "适配判断 https://example.com/project")
    assert first["status"] == "synthesized" and first["model_calls"] == 3
    assert len(agent.inputs) == 3
    assert len(agent.inputs[0]["evidence"]) == 1
    assert {r["url"] for r in agent.inputs[1]["evidence"]} == {
        "https://example.com/project",
        "https://example.com/schema",
    }
    reads = len(fetch.calls)
    replayed = engine.run(runner, RUN, "适配判断 https://example.com/project")
    assert replayed["result"] == first["result"] and replayed["replayed"] is True
    assert replayed["model_calls"] == replayed["agent_invocations"] == 0
    assert replayed["total_model_calls_observed"] == 3
    assert len(agent.inputs) == 3 and len(fetch.calls) == reads


def test_scope_denies_every_call(tmp_path: Path) -> None:
    runner, engine, fetch = setup(tmp_path, Agent([]))
    assert (
        engine.run(runner, "exec:other", "https://example.com/")["status"]
        == "subscription_not_authorized"
    )
    assert fetch.calls == []


def test_unknown_citation_cannot_become_answer(tmp_path: Path) -> None:
    def bad(material: dict[str, Any]) -> dict[str, Any]:
        result = answer(material)
        result["result"]["confirmed"][0]["evidence_ids"] = ["invented"]
        return result

    runner, engine, _ = setup(tmp_path, Agent([bad, bad, bad]))
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "output_invalid" and "result" not in result


def test_model_interrupt_is_never_automatically_resent(tmp_path: Path) -> None:
    agent = Agent([RuntimeError("process died")])
    runner, engine, _ = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "https://example.com/project")
    assert first["status"] == "subscription_unavailable"
    assert first["agent_invocations"] == 1 and first["model_calls_unknown"] is True
    replayed = engine.run(runner, RUN, "https://example.com/project")
    assert replayed["status"] == "subscription_unavailable"
    assert replayed["agent_invocations"] == replayed["model_calls"] == 0
    assert replayed["model_calls_unknown"] is True
    assert len(agent.inputs) == 1


def test_auto_verify_uses_subscription_without_deepseek_or_human_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, _ = setup(tmp_path, Agent([answer]))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("DeepSeek path must remain unused")

    runner.price_reader = forbidden
    runner.credential_reader = forbidden
    monkeypatch.setattr(runner, "run_synthesis", forbidden)
    adapter = GovernedResearchVerifyAdapter(runner)
    result = adapter(
        {
            "execution_run_id": RUN,
            "title": "是否适配",
            "source_seed_url": "https://example.com/project",
            "supplement": "",
            "auto_synthesis_allowed": False,
        }
    )
    assert result["summary"] == "现有示例说明可表达流程。"
    assert adapter.last_research_synthesis is not None
    assert adapter.last_research_synthesis["provider"] == "codex_subscription"
    assert result["model_calls"] == 2


def test_rendering_content_and_empty_answers_rejected() -> None:
    material = {"evidence": [{"evidence_id": "ev-1"}]}
    value = answer(material)
    malicious = deepcopy(value)
    malicious["result"]["answer_markdown"] = "<script>bad()</script>"
    with pytest.raises(ValueError, match="active_content"):
        validate_decision(malicious, material["evidence"])
    value["result"]["confirmed"] = []
    with pytest.raises(ValueError, match="unsupported"):
        validate_decision(value, material["evidence"])


def test_kimi_identity_survives_persistence_and_zero_call_replay(tmp_path: Path) -> None:
    agent = Agent([answer])
    agent.provider = "kimi_subscription"
    runner, engine, fetch = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "https://example.com/project")
    assert first["provider"] == "kimi_subscription"
    assert first["model_calls"] == first["agent_invocations"] == 2
    second = engine.run(runner, RUN, "https://example.com/project")
    assert second["provider"] == "kimi_subscription" and second["replayed"] is True
    assert second["model_calls"] == second["agent_invocations"] == 0
    assert second["total_agent_invocations_observed"] == 2
    assert len(fetch.calls) == 1 and len(agent.inputs) == 2


def test_claim_before_journal_crash_is_interrupted_not_waiting_forever(tmp_path: Path) -> None:
    agent = Agent([])
    runner, engine, _ = setup(tmp_path, agent)
    _, reference = engine._claim(runner, RUN, "subscription:model:0")
    engine._release(reference)  # Process left without a journal row.
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "subscription_interrupted"
    assert result["model_calls_unknown"] is True
    assert result["model_calls"] == result["agent_invocations"] == 0
    assert agent.inputs == []


def test_legacy_reserved_model_journal_is_not_resent(tmp_path: Path) -> None:
    agent = Agent([])
    runner, engine, _ = setup(tmp_path, agent)
    runner._journal_step(RUN, "subscription:model:0", {"status": "reserved"})
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "subscription_interrupted"
    assert result["model_calls_unknown"] is True and agent.inputs == []


def test_two_workers_make_one_model_invocation(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def delayed(material: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        assert release.wait(3)
        return answer(material)

    agent = Agent([delayed])
    runner, engine, fetch = setup(tmp_path, agent)
    results: list[dict[str, Any]] = []
    worker = threading.Thread(
        target=lambda: results.append(engine.run(runner, RUN, "https://example.com/project"))
    )
    worker.start()
    try:
        assert entered.wait(3)
        busy = engine.run(runner, RUN, "https://example.com/project")
        assert busy["status"] == "subscription_in_progress"
        assert busy["model_calls"] == busy["agent_invocations"] == 0
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results[0]["status"] == "synthesized"
    assert len(agent.inputs) == 2 and len(fetch.calls) == 1


def test_two_workers_make_one_seed_read(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    agent = Agent([answer])
    runner, engine, fetch = setup(tmp_path, agent)

    def delayed(request: dict[str, Any]) -> Any:
        entered.set()
        assert release.wait(3)
        return fetch(request)

    runner.fetch_transport = delayed
    results: list[dict[str, Any]] = []
    worker = threading.Thread(
        target=lambda: results.append(engine.run(runner, RUN, "https://example.com/project"))
    )
    worker.start()
    try:
        assert entered.wait(3)
        busy = engine.run(runner, RUN, "https://example.com/project")
        assert busy["status"] == "subscription_in_progress"
        assert agent.inputs == []
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results[0]["status"] == "synthesized"
    assert len(agent.inputs) == 2 and len(fetch.calls) == 1


def test_get_recovery_is_bounded_to_one_additional_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, engine, _ = setup(tmp_path, Agent([]))
    sends = []

    def crash(_runner: Any, action: dict[str, Any]) -> Any:
        sends.append(action["target"])
        raise KeyboardInterrupt("simulated process death after GET start")

    monkeypatch.setattr(engine, "_action", crash)
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            engine.run(runner, RUN, "https://example.com/project")
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "subscription_interrupted"
    assert len(sends) == 2


def test_completed_get_without_journal_can_recover_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent([answer])
    runner, engine, fetch = setup(tmp_path, agent)
    original = runner._journal_step

    def interrupted(run_id: str, key: str, entry: dict[str, Any]) -> None:
        if key.startswith("subscription:tool:") and entry.get("status") == "completed":
            raise RuntimeError("process left between claim completion and journal")
        return original(run_id, key, entry)

    monkeypatch.setattr(runner, "_journal_step", interrupted)
    with pytest.raises(RuntimeError):
        engine.run(runner, RUN, "https://example.com/project")
    monkeypatch.setattr(runner, "_journal_step", original)
    assert engine.run(runner, RUN, "https://example.com/project")["status"] == "synthesized"
    assert len(fetch.calls) == 2 and len(agent.inputs) == 2


def test_unknown_repository_trial_is_never_repeated(tmp_path: Path) -> None:
    action = {
        "kind": "repository_trial",
        "target": "https://github.com/example/project",
        "argv": ["node", "bin/check.js"],
        "reason": "检查真实可运行性",
    }
    decision = {
        "phase": "research",
        "reason": "需要试跑",
        "actions": [action],
        "result": empty_result(),
    }
    agent = Agent([decision])
    runner, engine, _ = setup(tmp_path, agent)
    trials = []

    def trial(target: str, argv: list[str]) -> Any:
        trials.append((target, argv))
        raise KeyboardInterrupt("trial started, process lost")

    engine.trial = trial
    with pytest.raises(KeyboardInterrupt):
        engine.run(runner, RUN, "https://example.com/project")
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "subscription_interrupted"
    assert len(trials) == len(agent.inputs) == 1
    assert result["model_calls"] == 0 and result["total_model_calls_observed"] == 1


def test_rephrasing_reason_does_not_repeat_io_or_duplicate_material(tmp_path: Path) -> None:
    decision = {
        "phase": "research",
        "reason": "重看相同来源",
        "result": empty_result(),
        "actions": [
            {
                "kind": "read_url",
                "target": "https://example.com/project",
                "argv": [],
                "reason": "换一种说法",
            }
        ],
    }
    agent = Agent([decision, answer])
    runner, engine, fetch = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "synthesized" and len(fetch.calls) == 1
    for material in agent.inputs:
        assert len(material["evidence"]) == 1
        assert len(material["tool_results"]) == 1
        assert "record" not in material["tool_results"][0]
        assert json.dumps(material).count('"excerpt_windows"') == 1


def test_reopened_run_has_independent_claim_identity(tmp_path: Path) -> None:
    runner, engine, _ = setup(tmp_path, Agent([]))
    cp = runner.store.latest(RUN)
    assert cp is not None
    second_run = RUN + ":episode-2"
    runner.store.save(replace(cp, run_id=second_run))
    first_key, first_ref = engine._claim(runner, RUN, "subscription:model:0")
    second_key, second_ref = engine._claim(runner, second_run, "subscription:model:0")
    try:
        assert first_key != second_key
    finally:
        engine._release(first_ref)
        engine._release(second_ref)


def test_long_document_can_be_read_past_the_first_window(tmp_path: Path) -> None:
    runner, engine, fetch = setup(tmp_path, Agent([]))
    fetch.body = (
        "<html><body><p>" + "正文" * 12000 + "METHODS_AND_LIMITATIONS" + "</p></body></html>"
    ).encode()
    first = engine._action(
        runner, {"kind": "read_url", "target": "https://example.com/paper", "argv": []}
    )["record"]
    later = engine._action(
        runner, {"kind": "read_url", "target": "https://example.com/paper", "argv": ["18000"]}
    )["record"]
    assert first["text_truncated"] is True
    assert "METHODS_AND_LIMITATIONS" not in first["excerpt_windows"][0]
    assert "METHODS_AND_LIMITATIONS" in later["excerpt_windows"][0]
    assert first["evidence_id"] != later["evidence_id"]
    assert first["page_digest"] == later["page_digest"]


def format_failure(**changes: Any) -> dict[str, Any]:
    return {
        "status": "blocked",
        "provider": "codex_subscription",
        "model_calls": 1,
        "agent_invocations": 1,
        "request_sent": "true",
        "error_category": "output_invalid",
        "diagnostics": {"cli_exit_code": 0},
        "result_json": None,
        "output_receipt": {
            "status": "invalid",
            "text": '{"phase":',
            "parse_error": {"kind": "json_syntax", "line": 1, "column": 10},
        },
        **changes,
    }


class ResponseAgent(Agent):
    def run(self, **kwargs: Any) -> dict[str, Any]:
        material = json.loads(kwargs["user_text"])
        if material.get("operation") == "independent_evidence_review":
            return super().run(**kwargs)
        self.inputs.append(material)
        response = self.decisions.pop(0)
        if callable(response):
            return {
                **format_failure(),
                "status": "completed",
                "provider": self.provider,
                "error_category": None,
                "result_json": response(material),
            }
        return deepcopy(response)


def closed_timeout(**changes: Any) -> dict[str, Any]:
    return {
        "status": "blocked", "provider": "kimi_subscription",
        "request_sent": "unknown", "model_calls": None, "agent_invocations": 1,
        "error_category": "timeout", "diagnostics": {"cli_exit_code": 143},
        "tool_activity": False, "internal_retries": 0,
        "execution_profile": "isolated_no_tools_v1",
        **changes,
    }


def test_terminated_model_timeout_resumes_original_with_new_key_and_unknown_history(
    tmp_path: Path,
) -> None:
    agent = ResponseAgent([closed_timeout()])
    agent.provider = "kimi_subscription"
    runner, engine, fetch = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "https://example.com/project")
    assert first["status"] == "subscription_unavailable"
    before = deepcopy(runner._journal_entries(RUN))
    assert engine.can_resume_model_failure(runner, RUN)
    successor = ResponseAgent([answer])
    successor.provider = "kimi_subscription"
    resumed = SubscriptionResearch(successor, run_ids=frozenset([RUN]))
    result = resumed.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "synthesized"
    assert result["model_calls_unknown"] is True
    assert result["total_agent_invocations_observed"] == 3
    assert result["total_model_calls_observed"] == 2
    after = runner._journal_entries(RUN)
    assert after["subscription:model:0"] == before["subscription:model:0"]
    assert after["subscription:model:1"]["outcome"]["status"] == "completed"
    assert [k for k in before if k.startswith("subscription:tool:")] == [
        k for k in after if k.startswith("subscription:tool:")]
    assert not resumed.can_resume_model_failure(runner, RUN)
    replay = resumed.run(runner, RUN, "https://example.com/project")
    assert replay["model_calls"] == replay["agent_invocations"] == 0
    assert len(successor.inputs) == 2


@pytest.mark.parametrize("changes", [
    {"error_category": "agent_exception"},
    {"diagnostics": {"cli_exit_code": None}},
    {"diagnostics": {"cli_exit_code": 0}},
    {"tool_activity": True}, {"internal_retries": 1},
    {"agent_invocations": True}, {"agent_invocations": None},
])
def test_unknown_or_tool_effect_is_not_a_closed_model_failure(
    tmp_path: Path, changes: dict[str, Any],
) -> None:
    agent = ResponseAgent([closed_timeout(**changes)])
    agent.provider = "kimi_subscription"
    runner, engine, _ = setup(tmp_path, agent)
    engine.run(runner, RUN, "https://example.com/project")
    assert not engine.can_resume_model_failure(runner, RUN)
    replay = engine.run(runner, RUN, "https://example.com/project")
    assert replay["model_calls"] == replay["agent_invocations"] == 0
    assert len(agent.inputs) == 1


def test_third_closed_timeout_exhausts_retry_after_restart(tmp_path: Path) -> None:
    agent = ResponseAgent([closed_timeout(), closed_timeout(), closed_timeout()])
    agent.provider = "kimi_subscription"
    runner, engine, _ = setup(tmp_path, agent)
    engine.run(runner, RUN, "https://example.com/project")
    second = engine.run(runner, RUN, "https://example.com/project")
    assert second["total_agent_invocations_observed"] == 2
    assert second["model_calls_unknown"] is True
    third = engine.run(runner, RUN, "https://example.com/project")
    assert third["total_agent_invocations_observed"] == 3
    assert not engine.can_resume_model_failure(runner, RUN)
    successor = ResponseAgent([])
    successor.provider = "kimi_subscription"
    replay = SubscriptionResearch(successor, run_ids=frozenset([RUN])).run(
        runner, RUN, "https://example.com/project")
    assert replay["agent_invocations"] == 0 and successor.inputs == []


def test_completed_format_error_gets_one_new_round_and_preserves_original(tmp_path: Path) -> None:
    failed = format_failure()
    agent = ResponseAgent([failed, answer])
    runner, engine, _ = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "synthesized"
    assert result["model_calls"] == result["total_model_calls_observed"] == 3
    journals = runner._journal_entries(RUN)
    assert journals["subscription:model:0"]["outcome"] == failed
    assert journals["subscription:model:1"]["outcome"]["status"] == "completed"
    correction = agent.inputs[1]["format_correction"]
    assert correction["error"] == "output_invalid"
    assert correction["output_receipt"] == failed["output_receipt"]
    restarted = SubscriptionResearch(Agent([]), run_ids=frozenset([RUN]))
    replay = restarted.run(runner, RUN, "https://example.com/project")
    assert replay["replayed"] and replay["model_calls"] == 0
    assert replay["total_model_calls_observed"] == 3


def test_third_format_failure_stops_even_after_restart(tmp_path: Path) -> None:
    agent = ResponseAgent([format_failure(), format_failure(), format_failure()])
    runner, engine, _ = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "subscription_unavailable"
    assert len(agent.inputs) == 3
    restarted_agent = Agent([])
    restarted = SubscriptionResearch(restarted_agent, run_ids=frozenset([RUN]))
    assert not restarted.can_resume_format_failure(runner, RUN)
    replay = restarted.run(runner, RUN, "https://example.com/project")
    assert replay["model_calls"] == 0 and replay["total_model_calls_observed"] == 3
    assert restarted_agent.inputs == []


@pytest.mark.parametrize(
    "changes",
    [
        {"request_sent": "unknown"},
        {"error_category": "timeout"},
        {"diagnostics": {"cli_exit_code": 1}},
        {"diagnostics": {}},
        {"model_calls": None},
        {"agent_invocations": None},
    ],
)
def test_unproven_format_failure_never_retries(tmp_path: Path, changes: dict[str, Any]) -> None:
    agent = ResponseAgent([format_failure(**changes)])
    runner, engine, _ = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "https://example.com/project")
    assert first["status"] == "subscription_unavailable"
    assert not engine.can_resume_format_failure(runner, RUN)
    engine.run(runner, RUN, "https://example.com/project")
    assert len(agent.inputs) == 1


def test_local_shape_error_can_be_corrected_but_citations_are_not_format(tmp_path: Path) -> None:
    agent = Agent([{"phase": "answer"}, answer])
    runner, engine, _ = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "synthesized" and result["model_calls"] == 3
    assert agent.inputs[1]["format_correction"]["error"] == "subscription_output_schema"


def test_generation_correction_and_independent_review_fit_eight_calls(tmp_path: Path) -> None:
    research = {
        "phase": "research",
        "reason": "检查同一来源",
        "result": empty_result(),
        "actions": [
            {
                "kind": "read_url",
                "target": "https://example.com/project",
                "argv": [],
                "reason": "核对",
            }
        ],
    }
    agent = ResponseAgent([lambda _: research] * 5 + [format_failure(), answer])
    runner, engine, _ = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "synthesized" and result["total_model_calls_observed"] == 8
    assert result["total_model_budget_slots"] == 8
    assert len(agent.inputs) == 8 and agent.inputs[-2]["last_round"] is True
    assert {
        key
        for key in runner._journal_entries(RUN)
        if key.startswith("subscription:model:")
        and not key.startswith("subscription:model:review:")
    } == {f"subscription:model:{index}" for index in range(7)}
    assert agent.inputs[-1]["operation"] == "independent_evidence_review"
    replay = engine.run(runner, RUN, "https://example.com/project")
    assert replay["model_calls"] == 0 and len(agent.inputs) == 8


def test_observed_multi_call_receipt_preserves_review_reservation(tmp_path: Path) -> None:
    agent = ResponseAgent([format_failure(model_calls=7)])
    runner, engine, _ = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "research_round_limit"
    assert result["total_model_calls_observed"] == 7 and len(agent.inputs) == 1
    assert result["total_model_budget_slots"] == 7
    assert not engine.can_resume_format_failure(runner, RUN)


def test_last_generation_failure_cannot_spend_reserved_review_round(tmp_path: Path) -> None:
    research = {
        "phase": "research",
        "reason": "核对",
        "result": empty_result(),
        "actions": [
            {
                "kind": "read_url",
                "target": "https://example.com/project",
                "argv": [],
                "reason": "核对",
            }
        ],
    }
    agent = ResponseAgent([lambda _: research] * 6 + [format_failure()])
    runner, engine, _ = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "subscription_unavailable"
    assert result["total_model_calls_observed"] == len(agent.inputs) == 7
    assert not engine.can_resume_format_failure(runner, RUN)
    assert not any(item.get("operation") == "independent_evidence_review" for item in agent.inputs)
    engine.run(runner, RUN, "https://example.com/project")
    assert len(agent.inputs) == 7


@pytest.mark.parametrize("failed_position", [5, 7])
def test_fifth_timeout_recovers_but_seventh_preserves_review_slot(
    tmp_path: Path, failed_position: int,
) -> None:
    research = {"phase": "research", "reason": "隔离重复来源检查",
                "result": empty_result(), "actions": [
                    {"kind": "read_url", "target": "https://example.com/project",
                     "argv": [], "reason": "检查实际材料"}]}
    agent = ResponseAgent([lambda _: research] * (failed_position - 1) + [closed_timeout()])
    agent.provider = "kimi_subscription"
    runner, engine, fetch = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "https://example.com/project")
    assert first["status"] == "subscription_unavailable"
    assert first["total_model_budget_slots"] == failed_position
    assert first["model_calls_unknown"] is True
    before = deepcopy(runner._journal_entries(RUN))
    fetch_count = len(fetch.calls)
    assert engine.can_resume_model_failure(runner, RUN) is (failed_position == 5)
    successor = ResponseAgent([answer] if failed_position == 5 else [])
    successor.provider = "kimi_subscription"
    restarted = SubscriptionResearch(successor, run_ids=frozenset([RUN]))
    final = restarted.run(runner, RUN, "https://example.com/project")
    after = runner._journal_entries(RUN)
    assert all(after[key] == value for key, value in before.items()
               if key.startswith("subscription:model:"))
    assert len(fetch.calls) == fetch_count
    assert final["model_calls_unknown"] is True
    if failed_position == 5:
        assert final["status"] == "synthesized"
        assert len(successor.inputs) == 2
        assert successor.inputs[-1]["operation"] == "independent_evidence_review"
        assert final["independent_review"]["verdict"] == "supported_with_limits"
        assert final["total_model_calls_observed"] == 6
        assert final["total_agent_invocations_observed"] == final["total_model_budget_slots"] == 7
        assert after["subscription:model:4"]["outcome"]["request_sent"] == "unknown"
        assert after["subscription:model:5"]["outcome"]["status"] == "completed"
        replay = restarted.run(runner, RUN, "https://example.com/project")
        assert replay["model_calls"] == replay["agent_invocations"] == 0
        assert len(successor.inputs) == 2
    else:
        assert final["status"] == "subscription_unavailable"
        assert final["total_model_budget_slots"] == 7 and successor.inputs == []
        assert "result" not in final


def legacy_startup_timeout() -> dict[str, Any]:
    response = closed_timeout(diagnostics={"cli_exit_code": 143, "stdout_bytes": 59,
                                          "stderr_bytes": 0})
    for field in ("tool_activity", "internal_retries", "execution_profile"):
        response.pop(field)
    return response


@pytest.mark.parametrize("change", [
    {"diagnostics": {"cli_exit_code": 143}},
    {"diagnostics": {"cli_exit_code": 143, "stdout_bytes": 58, "stderr_bytes": 0}},
    {"diagnostics": {"cli_exit_code": 143, "stdout_bytes": 59, "stderr_bytes": 1}},
    {"diagnostics": {"cli_exit_code": 137, "stdout_bytes": 59, "stderr_bytes": 0}},
    {"tool_activity": False}, {"internal_retries": 0},
    {"execution_profile": "tools_enabled"},
])
def test_missing_flags_without_exact_legacy_profile_never_resume(
    tmp_path: Path, change: dict[str, Any],
) -> None:
    response = {**legacy_startup_timeout(), **change}
    agent = ResponseAgent([response])
    agent.provider = "kimi_subscription"
    runner, engine, _ = setup(tmp_path, agent)
    engine.run(runner, RUN, "https://example.com/project")
    before = deepcopy(runner._journal_entries(RUN)["subscription:model:0"])
    assert not engine.can_resume_model_failure(runner, RUN)
    replay = engine.run(runner, RUN, "https://example.com/project")
    assert replay["model_calls"] == replay["agent_invocations"] == 0
    assert len(agent.inputs) == 1
    assert runner._journal_entries(RUN)["subscription:model:0"] == before


def test_exact_audited_legacy_startup_is_compatible_and_keeps_unknown(tmp_path: Path) -> None:
    agent = ResponseAgent([legacy_startup_timeout(), answer])
    agent.provider = "kimi_subscription"
    runner, engine, _ = setup(tmp_path, agent)
    engine.run(runner, RUN, "https://example.com/project")
    before = deepcopy(runner._journal_entries(RUN)["subscription:model:0"])
    assert engine.can_resume_model_failure(runner, RUN)
    resumed = engine.run(runner, RUN, "https://example.com/project")
    assert resumed["status"] == "synthesized" and resumed["model_calls_unknown"] is True
    assert resumed["total_model_budget_slots"] == 3
    assert resumed["total_model_calls_observed"] == 2
    assert resumed["total_agent_invocations_observed"] == 3
    assert runner._journal_entries(RUN)["subscription:model:0"] == before


def test_explicit_different_profile_is_not_tool_free_recovery(tmp_path: Path) -> None:
    agent = ResponseAgent([closed_timeout(execution_profile="tools_enabled")])
    agent.provider = "kimi_subscription"
    runner, engine, _ = setup(tmp_path, agent)
    engine.run(runner, RUN, "https://example.com/project")
    assert not engine.can_resume_model_failure(runner, RUN)
    engine.run(runner, RUN, "https://example.com/project")
    assert len(agent.inputs) == 1
