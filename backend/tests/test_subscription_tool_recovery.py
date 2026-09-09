"""Closed-failure recovery and ownership boundaries; no models or real network."""

from __future__ import annotations

import threading
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop import subscription_research as research
from fragment_loop.governed_research import GovernedResearchRunner
from fragment_loop.repository_trial import run_repository_trial
from fragment_loop.subscription_research import SubscriptionResearch, _PendingActionError

from .test_fragment_governed_research import FakeFetchTransport, make_run

RUN = "exec:fragment-intent:test"


@pytest.fixture
def pair(tmp_path: Path) -> tuple[GovernedResearchRunner, SubscriptionResearch]:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    make_run(store)
    runner = GovernedResearchRunner(
        store, collection_enabled=True, synthesis_enabled=False,
        fetch_transport=FakeFetchTransport(),
    )
    engine = SubscriptionResearch(
        object(), run_ids=frozenset({RUN}), trial=run_repository_trial,
    )
    return runner, engine


def action(kind: str = "repository_trial") -> dict[str, Any]:
    return {
        "kind": kind,
        "target": "https://github.com/example/project",
        "argv": ["node", "probe.js"] if kind == "repository_trial" else [],
        "reason": "isolated recovery boundary test",
    }


@pytest.mark.parametrize("kind", ["read_url", "search", "repository_trial"])
def test_second_closed_failure_is_final_and_original_and_two_slots_are_preserved(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    runner, engine = pair
    request = action(kind)
    sends: list[dict[str, Any]] = []

    def fail(_runner: Any, item: dict[str, Any]) -> Any:
        sends.append(deepcopy(item))
        raise ValueError("repository_download_failed_55")

    monkeypatch.setattr(engine, "_action", fail)
    first = engine._tool(runner, RUN, request)
    key = engine._tool_key(request)
    original = deepcopy(runner._journal_entries(RUN)[key])
    assert engine._tool_budget(runner, RUN)["used"] == 1
    second = engine._tool(runner, RUN, request)
    final_journal = deepcopy(runner._journal_entries(RUN))
    third = engine._tool(runner, RUN, {**request, "reason": "reworded explanation"})
    assert first["status"] == second["status"] == third["status"] == "failed"
    assert second["error"] == "repository_download_failed_55"
    assert second["retry_of"] == key and third == second
    assert len(sends) == 2
    assert set(final_journal) == {key, key + ":retry:1"}
    assert final_journal[key] == original
    assert runner._journal_entries(RUN) == final_journal
    budget = engine._tool_budget(runner, RUN)
    assert budget["used"] == 2 and budget["remaining"] == budget["limit"] - 2


def test_retry_needs_its_own_budget_slot(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch], monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, engine = pair
    monkeypatch.setattr(research, "MAX_TOOLS", 1)
    sends = []

    def fail(_runner: Any, item: dict[str, Any]) -> Any:
        sends.append(item)
        raise ValueError("repository_download_timeout")

    monkeypatch.setattr(engine, "_action", fail)
    request = action()
    engine._tool(runner, RUN, request)
    original = deepcopy(runner._journal_entries(RUN))
    refused = engine._tool(runner, RUN, request)
    assert refused["status"] == "blocked" and refused["request_sent"] == "false"
    assert refused["error"] == "subscription_tool_limit"
    assert refused["tool_budget"]["used"] == 1
    assert len(sends) == 1 and runner._journal_entries(RUN) == original


@pytest.mark.parametrize("kind", ["read_url", "repository_trial"])
def test_concurrent_workers_share_one_retry_and_one_retry_slot(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    runner, engine = pair
    request = action(kind)

    def failed_first(_runner: Any, _item: dict[str, Any]) -> Any:
        raise ValueError("repository_download_timeout")

    monkeypatch.setattr(engine, "_action", failed_first)
    engine._tool(runner, RUN, request)
    key = engine._tool_key(request)
    original = deepcopy(runner._journal_entries(RUN)[key])
    entered, release = threading.Event(), threading.Event()
    sends: list[str] = []
    outcomes: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def delayed(_runner: Any, item: dict[str, Any]) -> dict[str, Any]:
        sends.append(item["target"])
        entered.set()
        assert release.wait(3)
        return {"probe": "single completed retry"}

    def worker() -> None:
        try:
            outcomes.append(engine._tool(runner, RUN, request))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(engine, "_action", delayed)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(_PendingActionError, match="subscription_in_progress"):
            engine._tool(runner, RUN, request)
        assert engine._tool_budget(runner, RUN)["used"] == 2
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert len(sends) == 1 and outcomes[0]["status"] == "completed"
    assert engine._tool(runner, RUN, request) == outcomes[0]
    assert len(sends) == 1 and runner._journal_entries(RUN)[key] == original
    assert engine._tool_budget(runner, RUN)["used"] == 2


@pytest.mark.parametrize("stage", ["original", "retry"])
@pytest.mark.parametrize("has_claim", [False, True])
def test_reserved_native_trial_is_never_taken_over_even_with_legacy_journal_only(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
    monkeypatch: pytest.MonkeyPatch, stage: str, has_claim: bool,
) -> None:
    runner, engine = pair
    request = action()
    key = engine._tool_key(request)
    if stage == "retry":
        runner._journal_step(RUN, key, {
            "status": "completed", "outcome": {"status": "failed", "action": request},
        })
        key += ":retry:1"
    if has_claim:
        _, reference = engine._claim(runner, RUN, key)
        engine._release(reference)
    runner._journal_step(RUN, key, {"status": "reserved", "action": request})
    original = deepcopy(runner._journal_entries(RUN))
    sends = []

    def unexpected(_runner: Any, _item: dict[str, Any]) -> dict[str, Any]:
        sends.append(True)
        return {"unexpected": "must not execute"}

    monkeypatch.setattr(engine, "_action", unexpected)
    with pytest.raises(_PendingActionError, match="subscription_interrupted"):
        engine._tool(runner, RUN, request)
    assert sends == [] and runner._journal_entries(RUN) == original


@pytest.mark.parametrize(
    "implementation", [None, lambda *args: None, partial(run_repository_trial)]
)
def test_closed_failure_of_unknown_trial_implementation_is_not_retried(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
    monkeypatch: pytest.MonkeyPatch, implementation: Any,
) -> None:
    runner, engine = pair
    engine.trial = implementation
    request = action()
    key = engine._tool_key(request)
    first = {"action": request, "status": "failed", "error": "unknown implementation failure"}
    runner._journal_step(RUN, key, {"status": "completed", "outcome": first})
    original = deepcopy(runner._journal_entries(RUN))
    monkeypatch.setattr(engine, "_action", lambda *args: pytest.fail("must not resend"))
    assert engine._tool(runner, RUN, request) == first
    assert runner._journal_entries(RUN) == original
    assert engine._tool_budget(runner, RUN)["used"] == 1


def test_unknown_tool_kind_is_unavailable_and_cannot_gain_retry(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
) -> None:
    runner, engine = pair
    request = action("unknown_external_tool")
    first = engine._tool(runner, RUN, request)
    assert first["status"] == "failed"
    assert first["error"] == "subscription_tool_unavailable"
    original = deepcopy(runner._journal_entries(RUN))
    assert engine._tool(runner, RUN, request) == first
    assert runner._journal_entries(RUN) == original
    assert engine._tool_budget(runner, RUN)["used"] == 1


@pytest.mark.parametrize("code", ["repository_download_timeout", "repository_download_failed_56"])
def test_native_transport_recovery_stops_after_three_closed_attempts(pair, monkeypatch, code):
    runner, engine = pair
    request = action()
    sends = []

    def fail(_runner, item):
        sends.append(item)
        raise ValueError(code)

    monkeypatch.setattr(engine, "_action", fail)
    first = engine._tool(runner, RUN, request)
    key = engine._tool_key(request)
    original = deepcopy(runner._journal_entries(RUN)[key])
    second = engine._tool(runner, RUN, request)
    third = engine._tool(runner, RUN, request)
    snapshot = deepcopy(runner._journal_entries(RUN))
    assert engine._tool(runner, RUN, request) == third
    assert len(sends) == 3
    assert first["error"] == second["error"] == third["error"] == code
    assert snapshot[key] == original
    assert set(snapshot) == {key, key + ":retry:1", key + ":retry:2"}
    assert runner._journal_entries(RUN) == snapshot
    assert engine._tool_budget(runner, RUN)["used"] == 3


@pytest.mark.parametrize("new_model_repeats", [False, True])
def test_old_model_plan_reuses_failure_and_only_new_decision_can_retry(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
    monkeypatch: pytest.MonkeyPatch, new_model_repeats: bool,
) -> None:
    from .test_subscription_research import Agent, answer, empty_result

    runner, engine = pair
    request = action()
    old_plan = {"phase": "research", "reason": "旧计划", "actions": [request],
                "result": empty_result()}
    decisions = [old_plan, answer] if new_model_repeats else [answer]
    engine.agent = Agent(decisions)
    sends = []
    original_action = engine._action

    def trial(_runner: Any, item: dict[str, Any]) -> dict[str, Any]:
        if item["kind"] != "repository_trial":
            return original_action(_runner, item)
        sends.append(item)
        if len(sends) == 1:
            raise ValueError("repository_download_timeout")
        return {"probe": "explicitly requested isolated retry"}

    monkeypatch.setattr(engine, "_action", trial)
    failed = engine._tool(runner, RUN, request)
    assert failed["status"] == "failed"
    model_key = "subscription:model:0"
    runner._journal_step(RUN, model_key, {"status": "completed", "outcome": {
        "status": "completed", "provider": engine.agent.provider, "request_sent": "true",
        "model_calls": 1, "agent_invocations": 1, "result_json": old_plan}})
    # A later independent read already supplied useful evidence before restart.
    other = {"kind": "read_url", "target": "https://example.com/official-package", "argv": [],
             "reason": "later actual source"}
    engine._tool(runner, RUN, other)
    old_rows = deepcopy(runner._journal_entries(RUN))
    outcome = engine.run(runner, RUN, other["target"])
    assert outcome["status"] == "synthesized"
    assert len(sends) == (2 if new_model_repeats else 1)
    assert all(runner._journal_entries(RUN)[key] == value for key, value in old_rows.items())
    key = engine._tool_key(request)
    assert (key + ":retry:1" in runner._journal_entries(RUN)) is new_model_repeats
    assert runner._journal_entries(RUN)[key]["outcome"] == failed
    assert len(runner.fetch_transport.calls) == 1


@pytest.mark.parametrize("latest_retry", [1, 2])
def test_replay_selects_existing_latest_retry_without_creating_next(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
    monkeypatch: pytest.MonkeyPatch, latest_retry: int,
) -> None:
    runner, engine = pair
    request = action()
    sends = []

    def fail(_runner: Any, item: dict[str, Any]) -> Any:
        sends.append(item)
        raise ValueError("repository_download_timeout")

    monkeypatch.setattr(engine, "_action", fail)
    first = engine._tool(runner, RUN, request, retry_closed_failure=False)
    assert first["status"] == "failed"  # No previous receipt: normal first execution.
    for _ in range(latest_retry):
        latest = engine._tool(runner, RUN, request)
    before = deepcopy(runner._journal_entries(RUN))
    replay = engine._tool(runner, RUN, request, retry_closed_failure=False)
    assert replay == latest and len(sends) == latest_retry + 1
    assert runner._journal_entries(RUN) == before


@pytest.mark.parametrize("journal_written", [False, True])
def test_replay_does_not_hide_live_retry_claim_behind_old_failure(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
    monkeypatch: pytest.MonkeyPatch, journal_written: bool,
) -> None:
    runner, engine = pair
    request = action()
    original_key = engine._tool_key(request)
    runner._journal_step(RUN, original_key, {"status": "completed", "outcome": {
        "action": request, "status": "failed", "error": "repository_download_timeout"}})
    retry_key = original_key + ":retry:1"
    _, reference = engine._claim(runner, RUN, retry_key)
    if journal_written:
        runner._journal_step(RUN, retry_key, {"status": "reserved", "action": request})
    before = deepcopy(runner._journal_entries(RUN))
    monkeypatch.setattr(engine, "_action", lambda *args: pytest.fail("must not send"))
    try:
        with pytest.raises(_PendingActionError, match="subscription_in_progress"):
            engine._tool(runner, RUN, request, retry_closed_failure=False)
    finally:
        engine._release(reference)
    assert runner._journal_entries(RUN) == before


def test_historical_plan_without_any_tool_receipt_executes_first_request(
    pair: tuple[GovernedResearchRunner, SubscriptionResearch],
) -> None:
    from .test_subscription_research import Agent, answer, empty_result

    runner, engine = pair
    engine.agent = Agent([answer])
    request = action("read_url")
    plan = {"phase": "research", "reason": "旧计划尚未执行", "actions": [request],
            "result": empty_result()}
    runner._journal_step(RUN, "subscription:model:0", {"status": "completed", "outcome": {
        "status": "completed", "provider": engine.agent.provider, "request_sent": "true",
        "model_calls": 1, "agent_invocations": 1, "result_json": plan}})
    assert engine.run(runner, RUN, "继续原问题")["status"] == "synthesized"
    assert len(runner.fetch_transport.calls) == 1
    assert engine._tool_key(request) in runner._journal_entries(RUN)
