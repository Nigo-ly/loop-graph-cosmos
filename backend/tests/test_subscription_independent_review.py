"""Independent review control-flow tests with synthetic answers and zero live calls."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from fragment_loop.knowledge_library import KnowledgeLibraryError
from fragment_loop.subscription_research import (
    CONTRACT_VERSION,
    DECISION_SCHEMA,
    MAX_GENERATION_ROUNDS,
    MAX_ROUNDS,
    REVIEW_POLICY_VERSION,
    SubscriptionResearch,
    digest,
)
from tests.test_knowledge_library import library
from tests.test_subscription_research import RUN, Agent, answer, closed_timeout, empty_result, setup


class ReviewAgent(Agent):
    def __init__(self, decisions: list[Any], reviews: list[Any]):
        super().__init__(decisions)
        self.reviews = reviews
        self.review_prompts: list[str] = []

    def run(self, **kwargs: Any) -> dict[str, Any]:
        material = json.loads(kwargs["user_text"])
        if material.get("operation") != "independent_evidence_review":
            return super().run(**kwargs)
        self.inputs.append(material)
        self.review_prompts.append(kwargs["system_prompt"])
        response = self.reviews.pop(0)
        if isinstance(response, BaseException):
            raise response
        return {
            "provider": self.provider,
            "status": "completed",
            "request_sent": "true",
            "model_calls": 1,
            "agent_invocations": 1,
            "diagnostics": {"cli_exit_code": 0},
            "result_json": response(material) if callable(response) else response,
        }


def supported(material: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": "supported_with_limits",
        "reason": "此为隔离复核测试回执。",
        "findings": [],
        "result": material["draft"],
    }


def seed_legacy(runner: Any, engine: SubscriptionResearch, calls: int = 1) -> dict[str, Any]:
    observed = engine._tool(
        runner,
        RUN,
        {
            "kind": "read_url",
            "target": "https://example.com/project",
            "argv": [],
            "reason": "隔离旧来源",
        },
    )
    decision = answer({"evidence": [observed["record"]]})
    cached = {
        "provider": engine.agent.provider,
        "status": "synthesized",
        "result": decision["result"],
        "result_digest": digest(decision["result"]),
        "model_calls": calls,
        "agent_invocations": 1,
    }
    runner._journal_step(
        RUN,
        "subscription:model:0",
        {
            "status": "completed",
            "outcome": {
                "provider": engine.agent.provider,
                "status": "completed",
                "request_sent": "true",
                "model_calls": calls,
                "agent_invocations": 1,
                "diagnostics": {"cli_exit_code": 0},
                "result_json": decision,
            },
        },
    )
    runner._journal_step(RUN, "subscription:result", {"status": "completed", "outcome": cached})
    return cached


def test_review_replaces_overconfident_candidate_using_only_real_tool_material(
    tmp_path: Path,
) -> None:
    def overconfident(material: dict[str, Any]) -> dict[str, Any]:
        decision = answer(material)
        decision["result"]["confirmed"][0]["claim"] = "所有个人都无需迁移，十年内安全。"
        decision["result"]["recommendation"] = "立即采用，已是最佳基线。"
        return decision

    def correction(material: dict[str, Any]) -> dict[str, Any]:
        assert material["draft"]["confirmed"][0]["claim"] == "所有个人都无需迁移，十年内安全。"
        assert (
            len(material["evidence"]) == 1 and material["tool_results"][0]["status"] == "completed"
        )
        assert material["research_time"] and material["evidence"][0]["fetched_at"]
        corrected = answer(material)["result"]
        return {
            "verdict": "revised",
            "reason": "来源没有证明排他阈值或用户场景适配。",
            "findings": [
                {
                    "statement": material["draft"]["confirmed"][0]["claim"],
                    "issue": "inference_as_fact",
                    "correction": "保留直接事实，适用性列未知。",
                    "evidence_ids": [],
                }
            ],
            "result": corrected,
        }

    agent = ReviewAgent([overconfident], [correction])
    runner, engine, fetch = setup(tmp_path, agent)
    result = engine.run(runner, RUN, "系统适用性 https://example.com/project")
    assert result["status"] == "synthesized" and result["model_calls"] == 2
    assert result["result"]["recommendation"] == "限定试用。"
    assert result["independent_review"]["verdict"] == "revised"
    assert result["independent_review"]["reviewed_result_digest"] == digest(result["result"])
    assert len(fetch.calls) == 1
    assert agent.inputs[0]["contract_version"] == CONTRACT_VERSION
    assert "fetched_at只是抓取时刻" in agent.review_prompts[0]
    journals = runner._journal_entries(RUN)
    original = journals["subscription:model:0"]["outcome"]["result_json"]["result"]
    assert original["recommendation"] == "立即采用，已是最佳基线。"
    assert any(k.startswith("subscription:draft:") for k in journals)
    assert len([k for k in journals if k.startswith("subscription:model:review:")]) == 1
    assert engine.run(runner, RUN, "系统适用性 https://example.com/project")["model_calls"] == 0
    assert len(agent.inputs) == 2 and len(fetch.calls) == 1


def test_existing_v1_unchanged_review_creates_one_new_version_and_notification(
    tmp_path: Path,
) -> None:
    agent = ReviewAgent([], [supported])
    runner, engine, fetch = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine)
    journals_before = deepcopy(runner._journal_entries(RUN))
    evidence = next(
        e["outcome"]["record"]
        for k, e in journals_before.items()
        if k.startswith("subscription:tool:")
    )
    vault = library(tmp_path)
    publication = {
        "run_id": RUN,
        "fragment_id": "fixture",
        "title": "测试研究",
        "result": cached["result"],
        "evidence": [evidence],
    }
    v1 = vault.save_research(**publication)
    old_bytes = (vault.vault_root / v1["path"]).read_bytes()
    reviewed = engine.review_existing(runner, RUN, "原问题", cached)
    assert reviewed["result"] == cached["result"] and reviewed["model_calls"] == 1
    assert reviewed["total_model_calls_observed"] == 2 and len(fetch.calls) == 1
    v2 = vault.save_research(
        **publication,
        expected_revision=1,
        independent_review=reviewed["independent_review"],
        revision_reason="原结果完成独立证据复核",
    )
    assert v2["knowledge_id"] == v1["knowledge_id"] and v2["revision"] == 2
    assert (vault.vault_root / v1["path"]).read_bytes() == old_bytes
    assert "独立证据复核" in (vault.vault_root / v2["path"]).read_text()
    replay = vault.save_research(
        **publication, expected_revision=1, independent_review=reviewed["independent_review"]
    )
    assert replay["idempotent"] and replay["revision"] == 2
    assert len(vault.history(v1["knowledge_id"])) == 2
    assert vault.notifications()[0]["kind"] == "research_revised"
    assert len(vault.notifications()) == 1
    assert (
        runner._journal_entries(RUN)["subscription:result"]
        == journals_before["subscription:result"]
    )
    assert (
        runner._journal_entries(RUN)["subscription:model:0"]
        == journals_before["subscription:model:0"]
    )
    assert not engine.needs_review(reviewed)
    altered = deepcopy(publication)
    altered["result"]["summary"] = "未经同轮复核的新断言"
    with pytest.raises(KnowledgeLibraryError, match="independent_review_result_mismatch"):
        vault.save_research(
            **altered, expected_revision=2, independent_review=reviewed["independent_review"]
        )


@pytest.mark.parametrize("bad_review", [RuntimeError("unknown send"), {"invented": "shape"}])
def test_unknown_or_invalid_review_never_publishes_or_resends(
    tmp_path: Path, bad_review: Any
) -> None:
    agent = ReviewAgent([answer], [bad_review])
    runner, engine, _ = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "https://example.com/project")
    assert first["status"] in {"independent_review_unknown", "independent_review_failed"}
    assert "result" not in first
    replay = engine.run(runner, RUN, "https://example.com/project")
    assert replay["model_calls"] == replay["agent_invocations"] == 0
    assert len(agent.inputs) == 2


@pytest.mark.parametrize("retry_times_out", [False, True])
def test_review_closed_timeout_uses_one_new_claim_preserves_failed_receipt(
    tmp_path: Path, retry_times_out: bool,
) -> None:
    class TimeoutReviewAgent(ReviewAgent):
        provider = "kimi_subscription"

        def run(self, **kwargs: Any) -> dict[str, Any]:
            material = json.loads(kwargs["user_text"])
            if (material.get("operation") == "independent_evidence_review"
                    and self.reviews[0] == "timeout"):
                self.reviews.pop(0)
                self.inputs.append(material)
                return closed_timeout()
            return super().run(**kwargs)

    agent = TimeoutReviewAgent([answer], ["timeout"])
    runner, engine, _ = setup(tmp_path, agent)
    first = engine.run(runner, RUN, "https://example.com/project")
    assert first["status"] == "independent_review_unknown"
    before = deepcopy(runner._journal_entries(RUN))
    assert engine.can_resume_model_failure(runner, RUN)
    successor = TimeoutReviewAgent([], ["timeout", "timeout"] if retry_times_out else [supported])
    restarted = SubscriptionResearch(successor, run_ids=frozenset([RUN]))
    result = restarted.run(runner, RUN, "https://example.com/project")
    assert result["status"] == ("independent_review_unknown" if retry_times_out else "synthesized")
    assert result["model_calls_unknown"] is True
    assert result["total_agent_invocations_observed"] == 3
    after = runner._journal_entries(RUN)
    for key in before:
        if key.startswith(("subscription:model:", "subscription:review-result:")):
            assert after[key] == before[key]
    assert sum(key.startswith("subscription:model:review:") for key in after) == 2
    if retry_times_out:
        assert restarted.can_resume_model_failure(runner, RUN)
        third = restarted.run(runner, RUN, "https://example.com/project")
        assert third["status"] == "independent_review_unknown"
        assert third["total_agent_invocations_observed"] == 4
    assert not restarted.can_resume_model_failure(runner, RUN)
    replay = restarted.run(runner, RUN, "https://example.com/project")
    assert replay["agent_invocations"] == 0
    assert len(successor.inputs) == (2 if retry_times_out else 1)


def test_review_cannot_invent_citation(tmp_path: Path) -> None:
    def false_citation(material: dict[str, Any]) -> dict[str, Any]:
        response = supported(material)
        response["result"] = deepcopy(response["result"])
        response["result"]["confirmed"][0]["evidence_ids"] = ["ev-never-read"]
        return response

    agent = ReviewAgent([answer], [false_citation])
    runner, engine, _ = setup(tmp_path, agent)
    response = engine.run(runner, RUN, "https://example.com/project")
    assert response["status"] == "independent_review_failed" and "result" not in response


@pytest.mark.parametrize(
    "prior_calls, expected",
    [(MAX_ROUNDS - 1, "synthesized"), (MAX_ROUNDS, "independent_review_limit")],
)
def test_review_obeys_cumulative_call_limit(
    tmp_path: Path, prior_calls: int, expected: str
) -> None:
    agent = ReviewAgent([], [supported])
    runner, engine, _ = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine, prior_calls)
    result = engine.review_existing(runner, RUN, "原问题", cached)
    assert result["status"] == expected
    assert result["total_model_calls_observed"] <= MAX_ROUNDS
    assert len(agent.inputs) == (1 if prior_calls < MAX_ROUNDS else 0)


def test_review_unknown_prior_usage_prevents_new_send(tmp_path: Path) -> None:
    agent = ReviewAgent([], [supported])
    runner, engine, _ = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine)
    runner._journal_step(RUN, "subscription:model:1", {"status": "reserved"})
    result = engine.review_existing(runner, RUN, "原问题", cached)
    assert result["status"] == "independent_review_unknown" and agent.inputs == []
    assert (
        engine.review_existing(runner, "exec:unselected", "原问题", cached)["status"]
        == "subscription_not_authorized"
    )


@pytest.mark.parametrize("closed_previous", [False, True])
def test_two_workers_make_only_one_independent_review(
    tmp_path: Path, closed_previous: bool,
) -> None:
    entered, release = threading.Event(), threading.Event()

    def wait_review(material: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        assert release.wait(3)
        return supported(material)

    agent = ReviewAgent([], [wait_review])
    if closed_previous:
        agent.provider = "kimi_subscription"
    runner, engine, _ = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine)
    if closed_previous:
        identity = digest([REVIEW_POLICY_VERSION, digest(cached["result"])])
        runner._journal_step(RUN, f"subscription:model:review:{identity}", {
            "status": "completed", "outcome": closed_timeout()})
    results = []
    worker = threading.Thread(
        target=lambda: results.append(engine.review_existing(runner, RUN, "原问题", cached))
    )
    worker.start()
    try:
        assert entered.wait(3)
        busy = engine.review_existing(runner, RUN, "原问题", cached)
        assert busy["status"] == "independent_review_in_progress" and busy["model_calls"] == 0
    finally:
        release.set()
        worker.join(3)
    assert len(agent.inputs) == 1 and results[0]["status"] == "synthesized"


def test_unknown_timeout_keeps_a_budget_slot_in_addition_to_known_usage(tmp_path: Path) -> None:
    agent = ReviewAgent([], [supported])
    agent.provider = "kimi_subscription"
    runner, engine, _ = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine, MAX_ROUNDS - 1)
    identity = digest([REVIEW_POLICY_VERSION, digest(cached["result"])])
    runner._journal_step(RUN, f"subscription:model:review:{identity}", {
        "status": "completed", "outcome": closed_timeout()})
    assert not engine.can_resume_model_failure(runner, RUN)
    result = engine.review_existing(runner, RUN, "原问题", cached)
    assert result["status"] == "independent_review_limit" and agent.inputs == []
    assert result["total_model_budget_slots"] == MAX_ROUNDS
    assert result["total_model_calls_observed"] == MAX_ROUNDS - 1
    assert result["model_calls_unknown"] is True


@pytest.mark.parametrize("argv", [["bad"], ["216001"], ["1", "2"], [1], "0", None])
def test_invalid_read_offset_does_not_fetch(tmp_path: Path, argv: Any) -> None:
    runner, engine, fetch = setup(tmp_path, Agent([]))
    with pytest.raises(ValueError, match="subscription_read_offset"):
        engine._action(runner, {"kind": "read_url", "target": "https://example.com/", "argv": argv})
    assert fetch.calls == []


def test_only_known_error_codes_reach_model_tool_material(tmp_path: Path) -> None:
    runner, engine, _ = setup(tmp_path, Agent([]))

    def fail(_target: str, argv: list[str]) -> Any:
        raise ValueError(argv[1])

    engine.trial = fail
    for text, expected in [
        ("repository_command_invalid", "repository_command_invalid"),
        ("repository_secret_credential", "ValueError"),
        ("password=private", "ValueError"),
    ]:
        result = engine._tool(
            runner,
            RUN,
            {
                "kind": "repository_trial",
                "target": "https://github.com/example/project",
                "argv": ["node", text],
                "reason": "test",
            },
        )
        assert result["error"] == expected


def seed_action_limit_history(runner: Any, count: int, *, current: bool = False) -> None:
    actions = [
        {"kind": "read_url", "target": "https://example.com/", "argv": [], "reason": "test"}
    ] * 6
    decision = {
        "phase": "research",
        "reason": "old malformed actions",
        "actions": actions,
        "result": empty_result(),
    }
    for index in range(count):
        runner._journal_step(
            RUN,
            f"subscription:model:{index}",
            {
                "status": "completed",
                **({"contract_version": CONTRACT_VERSION} if current else {}),
                "outcome": {
                    "provider": "codex_subscription",
                    "status": "completed",
                    "request_sent": "true",
                    "diagnostics": {"cli_exit_code": 0},
                    "model_calls": 1,
                    "agent_invocations": 1,
                    "result_json": decision,
                },
            },
        )


def test_old_action_contract_gets_one_migration_then_review_without_resetting_history(
    tmp_path: Path,
) -> None:
    agent = ReviewAgent([answer], [supported])
    runner, engine, _ = setup(tmp_path, agent)
    seed_action_limit_history(runner, 6)
    before = deepcopy(runner._journal_entries(RUN))
    assert engine.can_resume_contract_upgrade(runner, RUN)
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "synthesized" and result["total_model_calls_observed"] == 8
    assert result["total_model_budget_slots"] == 8
    assert result["model_calls"] == 2 and len(agent.inputs) == 2
    assert agent.inputs[0]["contract_migration"]["actual"] == 6
    assert agent.inputs[0]["contract_migration"]["allowed"] == 4
    assert agent.inputs[0]["last_round"] is True
    assert not engine.can_resume_contract_upgrade(runner, RUN)
    after = runner._journal_entries(RUN)
    assert all(after[key] == value for key, value in before.items())
    assert engine.run(runner, RUN, "https://example.com/project")["model_calls"] == 0
    assert len(agent.inputs) == 2


@pytest.mark.parametrize("count,current", [(7, False), (2, True)])
def test_contract_migration_rejects_exhausted_or_current_contract(
    tmp_path: Path, count: int, current: bool
) -> None:
    runner, engine, _ = setup(tmp_path, Agent([]))
    seed_action_limit_history(runner, count, current=current)
    assert not engine.can_resume_contract_upgrade(runner, RUN)


def test_contract_schema_declares_exact_bounds() -> None:
    assert MAX_GENERATION_ROUNDS == 7 and MAX_ROUNDS == 10
    assert DECISION_SCHEMA["properties"]["actions"]["maxItems"] == 4
    assert DECISION_SCHEMA["properties"]["result"]["properties"]["confirmed"]["maxItems"] == 24
    assert REVIEW_POLICY_VERSION == "independent-evidence-review-v1"


def tool_action(index: int) -> dict[str, Any]:
    return {
        "kind": "read_url",
        "target": f"https://example.com/{index}",
        "argv": [],
        "reason": "隔离额度测试",
    }


def test_tool_cap_stops_io_before_seventeenth_request_and_replays_cached(tmp_path: Path) -> None:
    runner, engine, fetch = setup(tmp_path, Agent([answer]))
    outcomes = [engine._tool(runner, RUN, tool_action(i)) for i in range(16)]
    assert all(item["status"] == "completed" for item in outcomes)
    blocked = engine._tool(runner, RUN, tool_action(16))
    assert blocked["error"] == "subscription_tool_limit" and blocked["request_sent"] == "false"
    assert len(fetch.calls) == 16
    assert engine._tool(runner, RUN, tool_action(0)) == outcomes[0]
    assert len(fetch.calls) == 16
    assert (
        len([k for k in runner._journal_entries(RUN) if k.startswith("subscription:tool:")]) == 16
    )
    result = engine.run(runner, RUN, "利用已采集证据回答")
    assert result["status"] == "synthesized"
    assert engine.agent.inputs[0]["tool_budget"] == {
        "limit": 16,
        "used": 16,
        "remaining": 0,
        "allowed": True,
    }
    assert len(fetch.calls) == 16


def test_existing_failed_or_reserved_tools_consume_cap_without_network(tmp_path: Path) -> None:
    runner, engine, fetch = setup(tmp_path, Agent([]))
    for index in range(16):
        action = tool_action(index)
        runner._journal_step(
            RUN,
            engine._tool_key(action),
            {
                "status": "reserved" if index % 2 else "completed",
                "outcome": {"status": "failed", "action": action},
            },
        )
    result = engine._tool(runner, RUN, tool_action(17))
    assert result["error"] == "subscription_tool_limit" and fetch.calls == []
    assert engine._tool_budget(runner, RUN)["remaining"] == 0


def test_concurrent_distinct_tools_cannot_both_take_last_slot(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    runner, engine, fetch = setup(tmp_path, Agent([]))
    for index in range(15):
        assert engine._tool(runner, RUN, tool_action(index))["status"] == "completed"
    barrier = threading.Barrier(2)

    def execute(index: int) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return engine._tool(runner, RUN, tool_action(index))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, [15, 16]))
    assert sorted(item["status"] for item in outcomes) == ["blocked", "completed"]
    assert len(fetch.calls) == 16
    assert engine._tool_budget(runner, RUN)["used"] == 16


@pytest.mark.parametrize(
    "code,candidate,expected",
    [
        (
            "research_fetch_redirect_forbidden",
            "https://example.com/canonical",
            "https://example.com/canonical",
        ),
        ("research_fetch_redirect_forbidden", None, None),
        ("research_fetch_unavailable", "https://example.com/canonical", None),
    ],
)
def test_redirect_candidate_is_data_not_evidence_or_automatic_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    candidate: str | None,
    expected: str | None,
) -> None:
    from fragment_loop.research_fetch import ResearchFetchError

    runner, engine, fetch = setup(tmp_path, Agent([]))
    calls = []

    def action(*args: Any) -> Any:
        calls.append(args)
        raise ResearchFetchError(code, redirect_candidate=candidate)

    monkeypatch.setattr(engine, "_action", action)
    result = engine._tool(runner, RUN, tool_action(1))
    assert result["status"] == "failed" and "record" not in result
    assert result.get("redirect_candidate") == expected
    assert engine._tool_summary(result).get("redirect_candidate") == expected
    assert len(calls) == 1 and fetch.calls == []


def test_interrupted_get_takeover_cannot_escape_total_tool_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, engine, fetch = setup(tmp_path, Agent([]))
    for index in range(16):
        action = tool_action(index)
        runner._journal_step(
            RUN, engine._tool_key(action), {"status": "reserved", "action": action}
        )
    monkeypatch.setattr(
        engine,
        "_claim",
        lambda *args, **kwargs: ("test-claim", json.dumps({"attempt": 2, "owner": "test-owner"})),
    )
    result = engine._tool(runner, RUN, tool_action(0))
    assert result["error"] == "subscription_tool_limit" and fetch.calls == []
    assert engine._tool_budget(runner, RUN)["used"] == 16


def test_contract_migration_keeps_remaining_research_and_review_budget(tmp_path: Path) -> None:
    research = {"phase": "research", "reason": "合同已修正，继续必要证据读取",
                "actions": [tool_action(17)], "result": empty_result()}
    agent = ReviewAgent([research, answer], [supported])
    runner, engine, fetch = setup(tmp_path, agent)
    seed_action_limit_history(runner, 2)
    original = deepcopy(runner._journal_entries(RUN))
    result = engine.run(runner, RUN, "https://example.com/project")
    assert result["status"] == "synthesized"
    assert result["total_model_calls_observed"] == 5
    assert len(agent.inputs) == 3 and len(fetch.calls) == 2
    assert agent.inputs[0]["last_round"] is False
    assert not engine.can_resume_contract_upgrade(runner, RUN)
    assert all(runner._journal_entries(RUN)[key] == value for key, value in original.items())
    assert engine.run(runner, RUN, "https://example.com/project")["model_calls"] == 0


@pytest.mark.parametrize("relation", ["supports", "partially_supports"])
def test_review_retraction_must_update_structured_claim_as_well_as_prose(
    tmp_path: Path, relation: str,
) -> None:
    bad = "Readability 是已验证的首选正文提取基线"

    def draft(material: dict[str, Any]) -> dict[str, Any]:
        response = answer(material)
        response["result"]["claims"] = [{
            "claim": bad, "evidence_id": material["evidence"][0]["evidence_id"],
            "relation": relation}]
        return response

    def incomplete_fix(material: dict[str, Any]) -> dict[str, Any]:
        result = supported(material)
        result.update(verdict="revised", findings=[{
            "statement": bad, "issue": "unverified_trial",
            "correction": "仅有文档支持候选用途，未取得实测或比较证据。",
            "evidence_ids": [material["evidence"][0]["evidence_id"]]}])
        result["result"]["summary"] = "只可作为候选组件评估，未验证为首选。"
        result["result"]["recommendation"] = "结论收窄，保持未试跑的限制。"
        # The old structured positive proposition accidentally survives.
        return result

    agent = ReviewAgent([draft], [incomplete_fix])
    runner, engine, _ = setup(tmp_path, agent)
    outcome = engine.run(runner, RUN, "https://example.com/project")
    assert outcome["status"] == "independent_review_failed" and "result" not in outcome
    assert len(agent.inputs) == 2
    assert "claims每项命题与relation" in agent.review_prompts[0]
    assert "findings.statement复制该claim精确原文" in agent.review_prompts[0]


def test_review_narrowed_claim_and_partial_support_are_not_automatic_refutation(
    tmp_path: Path,
) -> None:
    from fragment_loop.subscription_research import validate_review

    agent = ReviewAgent([], [])
    runner, engine, _ = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine)
    evidence = [entry["outcome"]["record"] for key, entry in runner._journal_entries(RUN).items()
                if key.startswith("subscription:tool:")]
    reviewed = supported({"draft": deepcopy(cached["result"])})
    old = "候选工具是已实测的最佳选择"
    reviewed.update(verdict="revised", findings=[{
        "statement": old, "issue": "overconfident", "correction": "缩窄为文档支持的候选。",
        "evidence_ids": [evidence[0]["evidence_id"]]}])
    reviewed["result"]["claims"] = [{
        "claim": "文档支持把工具列为候选，实际质量待验证。",
        "evidence_id": evidence[0]["evidence_id"], "relation": "partially_supports"}]
    assert validate_review(reviewed, evidence) == reviewed
    reviewed["result"]["confirmed"][0]["claim"] = old
    with pytest.raises(ValueError, match="retraction_not_applied"):
        validate_review(reviewed, evidence)


def test_same_claim_same_source_cannot_be_confirmed_and_irrelevant(tmp_path: Path) -> None:
    from fragment_loop.subscription_research import validate_review

    runner, engine, _ = setup(tmp_path, ReviewAgent([], []))
    cached = seed_legacy(runner, engine)
    evidence = [entry["outcome"]["record"] for key, entry in runner._journal_entries(RUN).items()
                if key.startswith("subscription:tool:")]
    reviewed = supported({"draft": deepcopy(cached["result"])})
    fact = reviewed["result"]["confirmed"][0]
    reviewed["result"]["claims"] = [{
        "claim": fact["claim"], "evidence_id": fact["evidence_ids"][0], "relation": "irrelevant"}]
    with pytest.raises(ValueError, match="claim_relation_conflict"):
        validate_review(reviewed, evidence)
    other = {**evidence[0], "evidence_id": "ev-other-source"}
    reviewed["result"]["claims"][0]["evidence_id"] = other["evidence_id"]
    # A different source may be irrelevant without refuting the first source.
    assert validate_review(reviewed, [*evidence, other]) == reviewed


def test_explicit_consistency_review_has_new_cas_identity_and_replays_only_new_receipt(
    tmp_path: Path,
) -> None:
    agent = ReviewAgent([], [supported, supported])
    runner, engine, fetch = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine)
    original = engine.review_existing(runner, RUN, "原问题", cached)
    assert original["status"] == "synthesized" and not engine.needs_review(original)
    old_rows = deepcopy(runner._journal_entries(RUN))
    library_service = library(tmp_path)
    first = library_service.save_research(
        run_id=RUN, fragment_id="same-fragment", title="原研究",
        result=original["result"], evidence=runner.collection_outcome(RUN)["records"],
        independent_review=original["independent_review"])
    refreshed = engine.review_existing(
        runner, RUN, "原问题", original, force_consistency_review=True)
    assert refreshed["status"] == "synthesized"
    assert refreshed["independent_review"]["consistency_contract"] == "claims-consistency-v1"
    assert refreshed["model_calls"] == 1 and len(agent.inputs) == 2
    assert agent.inputs[-1]["consistency_contract"] == "claims-consistency-v1"
    after = runner._journal_entries(RUN)
    assert all(after[key] == value for key, value in old_rows.items())
    assert sum(key.startswith("subscription:model:review:") for key in after) == 2
    second = library_service.save_research(
        run_id=RUN, fragment_id="same-fragment", title="原研究", result=refreshed["result"],
        evidence=runner.collection_outcome(RUN)["records"],
        independent_review=refreshed["independent_review"], expected_revision=first["revision"])
    assert second["knowledge_id"] == first["knowledge_id"] and second["revision"] == 2
    assert (library_service.read(second["knowledge_id"])["claims_semantics"]
            == "evaluated_propositions")
    assert (library_service.read(first["knowledge_id"], 1)["independent_review"]
            == original["independent_review"])
    replay = engine.review_existing(runner, RUN, "原问题", original, force_consistency_review=True)
    assert replay["model_calls"] == replay["agent_invocations"] == 0
    assert len(agent.inputs) == 2 and len(fetch.calls) == 1
    assert engine.review_existing(runner, RUN, "原问题", original)["replayed"]
    assert len(agent.inputs) == 2


def test_forced_consistency_review_still_respects_original_total_budget(tmp_path: Path) -> None:
    agent = ReviewAgent([], [])
    runner, engine, _ = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine, calls=MAX_ROUNDS)
    result = engine.review_existing(runner, RUN, "原问题", cached, force_consistency_review=True)
    assert result["status"] == "independent_review_limit"
    assert result["total_model_budget_slots"] == MAX_ROUNDS and agent.inputs == []


def test_uncited_review_fact_is_demoted_without_mutating_model_result(tmp_path: Path) -> None:
    from fragment_loop.subscription_research import validate_review

    runner, engine, _ = setup(tmp_path, ReviewAgent([], []))
    cached = seed_legacy(runner, engine)
    evidence = [entry["outcome"]["record"] for key, entry in runner._journal_entries(RUN).items()
                if key.startswith("subscription:tool:")]
    raw = supported({"draft": deepcopy(cached["result"])})
    statement = "本轮试跑失败，没有实测输出"
    raw["result"]["confirmed"].append({"claim": statement, "evidence_ids": []})
    original = deepcopy(raw)
    normalized = validate_review(raw, evidence)
    assert raw == original
    assert len(normalized["result"]["confirmed"]) == len(raw["result"]["confirmed"]) - 1
    assert ("未附证据引用，未作为已确认事实：" + statement
            in normalized["result"]["unknowns"])
    assert normalized["result"]["claims"] == raw["result"]["claims"]
    assert normalized["result"]["answer_markdown"] == raw["result"]["answer_markdown"]
    assert validate_review(normalized, evidence) == normalized

    unknown = deepcopy(raw)
    unknown["result"]["confirmed"][-1]["evidence_ids"] = ["ev-invented"]
    with pytest.raises(ValueError, match="subscription_citation_unknown"):
        validate_review(unknown, evidence)
    empty = deepcopy(raw)
    empty["result"]["confirmed"] = [empty["result"]["confirmed"][-1]]
    with pytest.raises(ValueError, match="subscription_answer_unsupported"):
        validate_review(empty, evidence)
    malformed = deepcopy(raw)
    malformed["result"]["confirmed"][-1]["extra"] = "cannot erase malformed fields"
    with pytest.raises(ValueError, match="subscription_output_schema"):
        validate_review(malformed, evidence)


def test_completed_failed_consistency_receipt_revalidates_without_send_or_ledger_reset(
    tmp_path: Path,
) -> None:
    from fragment_loop.subscription_research import CLAIM_CONSISTENCY_CONTRACT

    agent = ReviewAgent([], [])
    runner, engine, fetch = setup(tmp_path, agent)
    cached = seed_legacy(runner, engine, calls=MAX_ROUNDS - 1)
    raw_review = supported({"draft": deepcopy(cached["result"])})
    uncited = "工具失败回执未作为证据编号入库"
    raw_review["result"]["confirmed"].append({"claim": uncited, "evidence_ids": []})
    identity = digest([REVIEW_POLICY_VERSION, digest(cached["result"]), CLAIM_CONSISTENCY_CONTRACT])
    model_key = "subscription:model:review:" + identity
    result_key = "subscription:review-result:" + identity
    receipt = {"provider": agent.provider, "status": "completed", "request_sent": "true",
               "model_calls": 1, "agent_invocations": 1,
               "result_json": raw_review, "diagnostics": {"cli_exit_code": 0}}
    runner._journal_step(RUN, model_key, {"status": "completed", "outcome": receipt})
    runner._journal_step(RUN, result_key, {"status": "completed", "outcome": {
        "provider": agent.provider, "status": "independent_review_failed"}})
    old_rows = deepcopy(runner._journal_entries(RUN))
    final = engine.review_existing(runner, RUN, "原问题", cached, force_consistency_review=True)
    assert final["status"] == "synthesized"
    assert final["model_calls"] == final["agent_invocations"] == 0
    assert final["total_model_budget_slots"] == MAX_ROUNDS
    after = runner._journal_entries(RUN)
    assert all(after[key] == value for key, value in old_rows.items())
    assert after[result_key + ":uncited-normalization-v1"]["outcome"]["status"] == "synthesized"
    normalization = final["independent_review"]["claim_normalization"]
    assert normalization == {
        "kind": "uncited_confirmed_to_unknowns_v1", "discarded_uncited_claims": [uncited],
        "source_review_digest": digest(raw_review)}
    store = library(tmp_path)
    publication = store.save_research(
        run_id=RUN, fragment_id="same-fragment", title="原研究", result=final["result"],
        evidence=runner.collection_outcome(RUN)["records"],
        independent_review=final["independent_review"])
    assert store.read(publication["knowledge_id"])["independent_review"]["claim_normalization"] == (
        normalization)
    forged = deepcopy(final["independent_review"])
    forged["claim_normalization"]["kind"] = "allow_uncited_confirmed"
    with pytest.raises(KnowledgeLibraryError, match="claim_normalization_invalid"):
        store.save_research(
            run_id="forged", fragment_id="same-fragment", title="原研究", result=final["result"],
            evidence=runner.collection_outcome(RUN)["records"], independent_review=forged)
    replay = engine.review_existing(runner, RUN, "原问题", cached, force_consistency_review=True)
    assert replay["replayed"] and replay["model_calls"] == replay["agent_invocations"] == 0
    assert agent.inputs == [] and len(fetch.calls) == 1
