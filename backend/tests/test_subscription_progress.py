"""Read-only active progress derives from actual per-run journal receipts."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_subscription_closure import assembled
from tests.test_subscription_research import RUN


def running(runner: Any) -> None:
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    runner.store.compare_and_append(replace(raw, status="running"), expected_sequence=sequence)


def model(runner: Any, key: str, count: int | None) -> None:
    runner._journal_step(
        RUN,
        key,
        {
            "status": "completed",
            "outcome": {
                "provider": "kimi_subscription",
                "model_calls": count,
                "agent_invocations": 1,
            },
        },
    )


def test_no_collection_review_uses_one_history_read_and_known_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, runner, _, agent, fetch = assembled(tmp_path)
    running(runner)
    model(runner, "subscription:model:0", 1)
    model(runner, "subscription:model:1", 2)
    for number in range(2):
        runner._journal_step(
            RUN,
            f"subscription:tool:window{number}",
            {
                "status": "completed",
                "outcome": {
                    "status": "completed",
                    "action": {"kind": "read_url"},
                    "record": {
                        "url": "https://example.com/same-page",
                        "evidence_id": f"window{number}",
                    },
                },
            },
        )
    runner._journal_step(RUN, "subscription:model:review:draft", {"status": "reserved"})
    before = runner.store.latest_sequence(RUN)
    original = runner._journal_entries
    history_calls = []

    def once(identifier: str) -> Any:
        history_calls.append(identifier)
        return original(identifier)

    monkeypatch.setattr(runner, "_journal_entries", once)
    checkpoint = runner.store.latest(RUN)
    assert "research_collection" not in checkpoint.eval_results
    projected = service._project_execution(checkpoint)
    progress = projected["research_progress"]
    assert history_calls == [RUN]
    assert progress["stage"] == "independent_review_in_progress"
    assert progress["model_calls"] == 3 and progress["agent_invocations"] == 2
    assert progress["model_calls_unknown"] is True
    assert progress["model_call_count_basis"] == "observed_cli_turns_lower_bound"
    assert progress["collected_sources"] == 1 and progress["network_requests"] == 2
    assert progress["provider"] == "kimi_subscription"
    assert projected["result"] is None and projected["status"] == "running"
    assert runner.store.latest_sequence(RUN) == before
    assert agent.inputs == [] and fetch.calls == []


def test_reserved_tool_preserves_prior_known_and_unknown_model_usage(tmp_path: Path) -> None:
    service, runner, *_ = assembled(tmp_path)
    running(runner)
    model(runner, "subscription:model:0", 2)
    model(runner, "subscription:model:1", None)
    runner._journal_step(
        RUN,
        "subscription:tool:trial",
        {"status": "reserved", "action": {"kind": "repository_trial"}},
    )
    progress = service._project_execution(runner.store.latest(RUN))["research_progress"]
    assert progress["stage"] == "researching"
    assert progress["model_calls"] == 2 and progress["model_calls_unknown"] is True
    assert progress["collected_sources"] == 0 and progress["network_requests"] == 0


def test_first_reserved_model_is_zero_known_lower_bound_not_zero_calls(tmp_path: Path) -> None:
    service, runner, *_ = assembled(tmp_path)
    running(runner)
    runner._journal_step(RUN, "subscription:model:0", {"status": "reserved"})
    progress = service._project_execution(runner.store.latest(RUN))["research_progress"]
    assert progress["stage"] == "researching"
    assert progress["model_calls"] == 0 and progress["model_calls_unknown"] is True
    assert progress["model_call_count_basis"] == "observed_cli_turns_lower_bound"
    assert "尚未完成" in progress["note"]


def test_completed_action_without_final_result_is_not_completed_research(tmp_path: Path) -> None:
    service, runner, *_ = assembled(tmp_path)
    running(runner)
    model(runner, "subscription:model:0", 1)
    progress = service._project_execution(runner.store.latest(RUN))["research_progress"]
    assert progress["stage"] == "researching" and progress["model_calls"] == 1
    assert progress["model_calls_unknown"] is False


def test_finished_research_does_not_rescan_history_for_each_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, runner, *_ = assembled(tmp_path)
    service.resume_subscription_executions()
    checkpoint = runner.store.latest(RUN)

    def forbidden(_: str) -> Any:
        raise AssertionError("terminal projection must use persisted totals")

    monkeypatch.setattr(runner, "_journal_entries", forbidden)
    progress = service._project_execution(checkpoint)["research_progress"]
    assert progress["stage"] == "synthesized" and progress["model_calls"] == 2
