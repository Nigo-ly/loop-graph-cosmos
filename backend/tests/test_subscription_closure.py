"""Same-task research recovery and automatic knowledge publication."""

from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from common.checkpoint import checkpoint_compat_view
from common.execution import translate_view_edit
from fragment_loop.cognitive_server import make_cognitive_decision_server
from fragment_loop.governed_research import GovernedResearchVerifyAdapter
from fragment_loop.intent_service import FragmentIntentService
from fragment_loop.knowledge_library import KnowledgeLibrary

from .test_fragment_intent_service import request
from .test_subscription_research import RUN, Agent, ResponseAgent, answer, format_failure, setup


def assembled(tmp_path: Path) -> tuple[Any, Any, Any, Any, Any]:
    agent = Agent([answer])
    agent.provider = "kimi_subscription"
    runner, engine, fetch = setup(tmp_path, agent)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "assets").mkdir()
    engine.library = KnowledgeLibrary(vault / "assets", vault_root=vault)
    found = runner.store.latest_raw_with_sequence(RUN)
    assert found is not None
    raw, sequence = found
    values = {
        **checkpoint_compat_view(raw).eval_results,
        "execution_binding": {
            "route": "verify",
            "title": "是否适配",
            "source_seed_url": "https://example.com/project",
        },
    }
    runner.store.compare_and_append(
        replace(
            raw, eval_results=translate_view_edit(existing=raw.eval_results, edited_flat=values)
        ),
        expected_sequence=sequence,
    )
    service = FragmentIntentService(
        runner.store,
        lambda *_: {},
        lambda *_: {},
        verify_adapter=GovernedResearchVerifyAdapter(runner),
    )
    return service, runner, engine, agent, fetch


def test_original_run_completes_and_publishes_once_without_manual_review(tmp_path: Path) -> None:
    service, runner, engine, agent, fetch = assembled(tmp_path)
    assert service.resume_subscription_executions()["resumed"] == 1
    cp = runner.store.latest(RUN)
    assert cp.status == "passed"
    assert cp.eval_results["research_synthesis"]["provider"] == "kimi_subscription"
    receipt = cp.eval_results["knowledge_publication"]
    record = engine.library.read(receipt["knowledge_id"])
    assert record["human_reviewed"] is False
    assert receipt["revision"] == 1
    projection = service._project_execution(cp)
    assert projection["result"]["answer_markdown"]
    assert projection["research_progress"]["coverage"]["status"] == "assessed"
    before = runner.store.latest_sequence(RUN)
    reads = len(fetch.calls)
    assert service.resume_subscription_executions()["resumed"] == 0
    assert runner.store.latest_sequence(RUN) == before
    assert len(agent.inputs) == 2 and len(fetch.calls) == reads


def test_completed_journal_recovers_projection_and_write_without_new_calls(tmp_path: Path) -> None:
    service, runner, engine, agent, fetch = assembled(tmp_path)
    done = engine.run(runner, RUN, "是否适配 https://example.com/project")
    assert done["status"] == "synthesized"
    assert not runner.store.latest(RUN).eval_results.get("research_synthesis")
    assert service.resume_subscription_executions()["resumed"] == 1
    assert runner.store.latest(RUN).eval_results["knowledge_publication"]["revision"] == 1
    assert len(agent.inputs) == 2 and len(fetch.calls) == 1


def test_interrupted_send_is_visible_and_not_retried_by_scanner(tmp_path: Path) -> None:
    service, runner, engine, agent, fetch = assembled(tmp_path)
    agent.decisions = [RuntimeError("interrupted")]
    service.resume_subscription_executions()
    cp = runner.store.latest(RUN)
    assert cp.status == "blocked"
    projection = service._project_execution(cp)
    assert projection["research_progress"]["model_calls_unknown"] is True
    assert projection["research_progress"]["blocker"] == "subscription_unavailable"
    assert not cp.eval_results.get("knowledge_publication")
    service.resume_subscription_executions()
    assert len(agent.inputs) == 1


def test_catalog_read_and_notification_api_use_real_written_records(tmp_path: Path) -> None:
    service, runner, engine, agent, fetch = assembled(tmp_path)
    service.resume_subscription_executions()
    receipt = runner.store.latest(RUN).eval_results["knowledge_publication"]
    server = make_cognitive_decision_server(None, port=0, knowledge_library=engine.library)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = request(server, "GET", "/fragment/v1/knowledge")
        assert status == 200 and len(body["data"]) == 1
        status, body = request(server, "GET", "/fragment/v1/knowledge/" + receipt["knowledge_id"])
        assert status == 200 and body["data"]["result"]["answer_markdown"]
        status, body = request(server, "GET", "/fragment/v1/knowledge/notifications")
        assert status == 200 and isinstance(body["data"], list)
        status, body = request(server, "GET", "/fragment/v1/knowledge/periods")
        assert status == 200 and body["data"] == []
        status, body = request(
            server, "GET", "/fragment/v1/knowledge", headers={"Origin": "https://untrusted.example"}
        )
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_saved_note_modification_is_visible_without_overwriting(tmp_path: Path) -> None:
    service, runner, engine, agent, fetch = assembled(tmp_path)
    service.resume_subscription_executions()
    receipt = runner.store.latest(RUN).eval_results["knowledge_publication"]
    note = engine.library.vault_root / receipt["path"]
    note.write_text("用户自己的修改", encoding="utf-8")
    service.resume_subscription_executions()
    changed = service._project_execution(runner.store.latest(RUN))["knowledge_publication"]
    assert changed["status"] == "unavailable"
    assert note.read_text() == "用户自己的修改"
    assert len(agent.inputs) == 2


def test_full_bound_goal_not_organizer_summary_is_sent(tmp_path: Path) -> None:
    service, runner, engine, agent, fetch = assembled(tmp_path)
    adapter = GovernedResearchVerifyAdapter(runner)
    adapter(
        {
            "execution_run_id": RUN,
            "title": "短标题",
            "goal": "完整原问题与具体判断要求",
            "recorded_goal": "外部字段不能替换已绑定原问题",
            "literal_summary": "整理器未验证推断",
            "source_seed_url": "https://example.com/project",
        }
    )
    assert "完整原问题与具体判断要求" in agent.inputs[0]["goal"]
    assert "整理器未验证推断" not in agent.inputs[0]["goal"]
    assert "外部字段不能替换已绑定原问题" not in agent.inputs[0]["goal"]


def test_historical_completed_format_failure_resumes_same_run_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, engine, _, fetch = assembled(tmp_path)
    failed = format_failure(provider="kimi_subscription")
    agent = ResponseAgent([failed, answer])
    agent.provider = "kimi_subscription"
    engine.agent = agent
    # Reproduce the previous release stopping after the completed invalid text.
    with monkeypatch.context() as patch:
        patch.setattr(engine, "_format_failure", lambda *_: None)
        assert service.resume_subscription_executions()["resumed"] == 1
    before = runner.store.latest(RUN)
    assert before.status == "blocked" and len(agent.inputs) == 1
    old_response = runner._journal_entries(RUN)["subscription:model:0"]
    old_sequence = runner.store.latest_sequence(RUN)
    original_goal = agent.inputs[0]["goal"]
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    values = dict(checkpoint_compat_view(raw).eval_results)
    values["execution_binding"] = {**values["execution_binding"], "input_digest": "old-digest"}
    runner.store.compare_and_append(
        replace(
            raw, eval_results=translate_view_edit(existing=raw.eval_results, edited_flat=values)
        ),
        expected_sequence=sequence,
    )

    def changed_organizer(*_: object) -> dict[str, object]:
        raise AssertionError("must keep the original recorded goal after organizer refresh")

    service.source_loader = changed_organizer
    assert engine.can_resume_format_failure(runner, RUN)
    assert service.resume_subscription_executions()["resumed"] == 1
    after = runner.store.latest(RUN)
    assert after.run_id == RUN and after.status == "passed"
    assert runner.store.latest_sequence(RUN) > old_sequence
    assert runner._journal_entries(RUN)["subscription:model:0"] == old_response
    synthesis = after.eval_results["research_synthesis"]
    assert synthesis["model_calls"] == 2 and synthesis["total_model_calls_observed"] == 3
    receipt = after.eval_results["knowledge_publication"]
    assert engine.library.read(receipt["knowledge_id"])["result"]["summary"]
    assert len(agent.inputs) == 3 and len(fetch.calls) == 1
    assert " ".join(original_goal.split()) in " ".join(agent.inputs[1]["goal"].split())
    assert service.resume_subscription_executions()["resumed"] == 0
    assert len(agent.inputs) == 3


def test_historical_failure_third_error_stops_daemon_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, engine, _, _ = assembled(tmp_path)
    failed = format_failure(provider="kimi_subscription")
    agent = ResponseAgent([failed, failed, failed])
    agent.provider = "kimi_subscription"
    engine.agent = agent
    with monkeypatch.context() as patch:
        patch.setattr(engine, "_format_failure", lambda *_: None)
        service.resume_subscription_executions()
    assert service.resume_subscription_executions()["resumed"] == 1
    assert runner.store.latest(RUN).status == "blocked"
    sequence = runner.store.latest_sequence(RUN)
    for _ in range(3):
        assert service.resume_subscription_executions()["resumed"] == 0
    assert runner.store.latest_sequence(RUN) == sequence
    assert len(agent.inputs) == 3
    assert not runner.store.latest(RUN).eval_results.get("knowledge_publication")


def test_corrected_result_commit_interrupt_replays_without_another_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, engine, _, _ = assembled(tmp_path)
    agent = ResponseAgent([format_failure(provider="kimi_subscription"), answer])
    agent.provider = "kimi_subscription"
    engine.agent = agent
    with monkeypatch.context() as patch:
        patch.setattr(engine, "_format_failure", lambda *_: None)
        service.resume_subscription_executions()
    original = runner._journal_step

    def die_after_result(run_id: str, key: str, entry: dict[str, Any]) -> None:
        original(run_id, key, entry)
        if (key.startswith("subscription:review-result:")
                and entry.get("outcome", {}).get("status") == "synthesized"):
            raise KeyboardInterrupt("completed result persisted; projection not yet committed")

    with monkeypatch.context() as patch:
        patch.setattr(runner, "_journal_step", die_after_result)
        with pytest.raises(KeyboardInterrupt):
            service.resume_subscription_executions()
    assert len(agent.inputs) == 3 and runner.store.latest(RUN).status == "blocked"
    assert engine.can_resume_format_failure(runner, RUN)
    assert service.resume_subscription_executions()["resumed"] == 1
    cp = runner.store.latest(RUN)
    assert cp.status == "passed" and cp.eval_results["knowledge_publication"]["revision"] == 1
    assert cp.eval_results["research_synthesis"]["model_calls"] == 0
    assert len(agent.inputs) == 3


def test_repeated_resume_keeps_first_complete_goal_after_legacy_accumulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, runner, engine, _, fetch = assembled(tmp_path)
    agent = ResponseAgent([format_failure(provider="kimi_subscription"), answer])
    agent.provider = "kimi_subscription"
    engine.agent = agent
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    values = dict(checkpoint_compat_view(raw).eval_results)
    binding = {
        **values["execution_binding"], "input_digest": "original-input-digest",
        "supplement": "保留完整限制与对照条件。" * 60 + " https://example.com/context",
    }
    values["execution_binding"] = binding
    runner.store.compare_and_append(
        replace(raw, eval_results=translate_view_edit(
            existing=raw.eval_results, edited_flat=values)), expected_sequence=sequence)
    source_reads = []

    def original_source(fragment_id: str, input_digest: str) -> dict[str, str]:
        source_reads.append((fragment_id, input_digest))
        return {"goal": "AIFS 能否提高预测质量？保留原问题中的重复强调：验证，验证。"}

    service.source_loader = original_source
    with monkeypatch.context() as patch:
        patch.setattr(engine, "_format_failure", lambda *_: None)
        assert service.resume_subscription_executions()["resumed"] == 1
    collection = runner.collection_outcome(RUN)
    original_goal = collection["goal"]
    assert len(original_goal) > 500 and len(source_reads) == 1
    original_history = runner.store.history(RUN)
    original_response = runner._journal_entries(RUN)["subscription:model:0"]
    # Reproduce an old release that already appended the title and seed twice.
    runner.persist_collection(RUN, {
        **collection,
        "goal": f"{binding['title']}\n{original_goal}\n{binding['source_seed_url']}",
    })

    def changed_source(*_: object) -> dict[str, object]:
        raise AssertionError("the refreshed source must not replace this run's recorded question")

    service.source_loader = changed_source
    append = runner.store.compare_and_append

    def interrupt_result(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("event_type") == "subscription_research_result":
            raise KeyboardInterrupt("result persisted, projection commit interrupted")
        return append(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runner.store, "compare_and_append", interrupt_result)
        for _ in range(2):
            with pytest.raises(KeyboardInterrupt):
                service.resume_subscription_executions()
            assert runner.collection_outcome(RUN)["goal"] == original_goal
    assert service.resume_subscription_executions()["resumed"] == 1
    current = runner.store.latest(RUN)
    assert current.run_id == RUN and current.status == "passed"
    assert current.eval_results["execution_binding"] == binding
    assert current.eval_results["research_collection"]["goal"] == original_goal
    assert runner.store.history(RUN)[:len(original_history)] == original_history
    assert runner._journal_entries(RUN)["subscription:model:0"] == original_response
    assert agent.inputs[1]["goal"] == original_goal
    assert len(agent.inputs) == 3 and len(fetch.calls) == 1


@pytest.mark.parametrize("fail_review", [False, True])
def test_service_reviews_published_v1_once_preserving_old_note(
    tmp_path: Path, fail_review: bool,
) -> None:
    from fragment_loop.subscription_research import digest

    from .test_subscription_independent_review import ReviewAgent, seed_legacy, supported

    service, runner, engine, _, fetch = assembled(tmp_path)
    agent = ReviewAgent([], [RuntimeError("unknown send") if fail_review else supported])
    engine.agent = agent
    cached = seed_legacy(runner, engine)
    evidence = [entry["outcome"]["record"] for key, entry in runner._journal_entries(RUN).items()
                if key.startswith("subscription:tool:")]
    runner.persist_collection(RUN, {"goal": "原问题", "records": evidence,
                                   "searches": 0, "fetches": 1})
    cp = runner.store.latest(RUN)
    old = engine.library.save_research(
        run_id=RUN, fragment_id=cp.fragment_id, title="是否适配",
        result=cached["result"], evidence=evidence,
    )
    old_path = engine.library.vault_root / old["path"]
    old_bytes = old_path.read_bytes()
    receipt = {"knowledge_id": old["knowledge_id"], "revision": 1, "path": old["path"],
               "result_digest": digest(cached["result"]), "publication_source": "system_policy"}
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    values = {**checkpoint_compat_view(raw).eval_results, "research_synthesis": cached,
              "knowledge_publication": receipt}
    runner.store.compare_and_append(
        replace(raw, status="passed", eval_results=translate_view_edit(
            existing=raw.eval_results, edited_flat=values)), expected_sequence=sequence)
    before = runner._journal_entries(RUN)["subscription:result"]
    assert service.resume_subscription_executions()["resumed"] == 1
    latest = runner.store.latest(RUN)
    if fail_review:
        assert latest.status == "blocked"
        assert latest.eval_results["research_synthesis"]["status"] == "independent_review_unknown"
        assert latest.eval_results["knowledge_publication"] == receipt
        assert len(engine.library.history(old["knowledge_id"])) == 1
    else:
        assert latest.status == "passed"
        assert latest.eval_results["knowledge_publication"]["revision"] == 2
        assert service._project_execution(latest)["independent_review"]["verdict"]
        assert len(engine.library.history(old["knowledge_id"])) == 2
        assert engine.library.notifications()[0]["kind"] == "research_revised"
    assert old_path.read_bytes() == old_bytes
    assert runner._journal_entries(RUN)["subscription:result"] == before
    assert len(fetch.calls) == len(agent.inputs) == 1
    assert service.resume_subscription_executions()["resumed"] == 0
    assert len(agent.inputs) == 1


def test_active_review_projection_keeps_known_usage_and_does_not_count_windows_as_sources(
    tmp_path: Path,
) -> None:
    from fragment_loop.governed_research import research_progress_projection
    service, runner, engine, _, _ = assembled(tmp_path)
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    values = {**checkpoint_compat_view(raw).eval_results,
              "research_synthesis": {"provider": "kimi_subscription", "model_calls": 4},
              "research_collection": {"records": [{"url": "https://example.com/paper"},
                                                    {"url": "https://example.com/paper"}]}}
    runner.store.compare_and_append(
        replace(raw, eval_results=translate_view_edit(
            existing=raw.eval_results, edited_flat=values)), expected_sequence=sequence)
    runner._journal_step(RUN, "subscription:model:review:fixture", {"status": "reserved"})
    progress = research_progress_projection(runner.store.latest(RUN))
    assert progress["stage"] == "independent_review_in_progress"
    assert progress["model_calls"] == 4 and progress["model_calls_unknown"] is True
    assert progress["collected_sources"] == 1
    runner._journal_step(RUN, "subscription:tool:fixture", {"status": "reserved"})
    progress = research_progress_projection(runner.store.latest(RUN))
    assert progress["model_calls_unknown"] is False


def test_recorded_review_recovers_complete_fields_with_zero_new_model_and_v2(
    tmp_path: Path,
) -> None:
    import hashlib
    import json

    from fragment_loop.subscription_research import REVIEW_POLICY_VERSION, digest

    from .test_subscription_independent_review import ReviewAgent, seed_legacy, supported

    service, runner, engine, _, fetch = assembled(tmp_path)
    agent = ReviewAgent([], [])  # any new call would fail
    engine.agent = agent
    draft = seed_legacy(runner, engine)
    records = [v["outcome"]["record"] for k, v in runner._journal_entries(RUN).items()
               if k.startswith("subscription:tool:")]
    runner.persist_collection(RUN, {"goal": "原问题", "records": records,
                                   "searches": 0, "fetches": 1})
    cp = runner.store.latest(RUN)
    v1 = engine.library.save_research(run_id=RUN, fragment_id=cp.fragment_id, title="是否适配",
                                     result=draft["result"], evidence=records)
    v1_bytes = (engine.library.vault_root / v1["path"]).read_bytes()
    raw_text = json.dumps(supported({"draft": draft["result"]}), ensure_ascii=False)[:-1]
    response = {"provider": agent.provider, "status": "blocked", "request_sent": "true",
                "model_calls": 1, "agent_invocations": 1, "error_category": "output_invalid",
                "diagnostics": {"cli_exit_code": 0},
                "output_receipt": {"text": raw_text, "bytes": len(raw_text.encode()),
                                   "sha256": hashlib.sha256(raw_text.encode()).hexdigest()}}
    identity = digest([REVIEW_POLICY_VERSION, digest(draft["result"])])
    key = "subscription:model:review:" + identity
    runner._journal_step(RUN, key, {"status": "completed", "outcome": response})
    failed = {"provider": agent.provider, "status": "independent_review_failed", "model_calls": 1}
    runner._journal_step(RUN, "subscription:review-result:" + identity,
                         {"status": "completed", "outcome": failed})
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    values = {**checkpoint_compat_view(raw).eval_results, "research_synthesis": failed,
              "knowledge_publication": {"knowledge_id": v1["knowledge_id"], "revision": 1,
                                        "path": v1["path"],
                                        "result_digest": digest(draft["result"])}}
    runner.store.compare_and_append(replace(raw, status="blocked", eval_results=translate_view_edit(
        existing=raw.eval_results, edited_flat=values)), expected_sequence=sequence)
    old_model = runner._journal_entries(RUN)[key]
    assert engine.can_resume_recorded_output(runner, RUN)
    assert service.resume_subscription_executions()["resumed"] == 1
    current = runner.store.latest(RUN)
    assert current.status == "passed"
    assert current.eval_results["knowledge_publication"]["revision"] == 2
    projection = service._project_execution(current)
    assert projection["research_result_digest"] == (
        current.eval_results["knowledge_publication"]["result_digest"]
    )
    assert projection["result_digest"] == current.eval_results["result_digest"]
    review = current.eval_results["research_synthesis"]["independent_review"]
    assert review["output_normalization"] == "closed_final_object"
    assert review["source_output_sha256"] == response["output_receipt"]["sha256"]
    assert runner._journal_entries(RUN)[key] == old_model
    assert (engine.library.vault_root / v1["path"]).read_bytes() == v1_bytes
    assert len(fetch.calls) == 1 and agent.inputs == []
    assert service.resume_subscription_executions()["resumed"] == 0
    assert agent.inputs == []


@pytest.mark.parametrize("unknown", [False, True])
def test_recorded_answer_bypasses_old_format_errors_but_requires_one_review(
    tmp_path: Path, unknown: bool,
) -> None:
    import hashlib
    import json

    from .test_subscription_independent_review import ReviewAgent, supported

    service, runner, engine, _, fetch = assembled(tmp_path)
    agent = ReviewAgent([], [supported])
    engine.agent = agent
    observed = engine._tool(runner, RUN, {
        "kind": "read_url", "target": "https://example.com/project", "argv": [],
        "reason": "retained original evidence",
    })
    decision = answer({"evidence": [observed["record"]]})
    raw_text = json.dumps(decision, ensure_ascii=False)
    position = raw_text.index('{', raw_text.index('"confirmed"'))
    raw_text = raw_text[:position] + raw_text[position + 1:]
    response = {"provider": agent.provider, "status": "blocked", "request_sent": "true",
                "model_calls": 1, "agent_invocations": 1, "error_category": "output_invalid",
                "diagnostics": {"cli_exit_code": 0},
                "output_receipt": {"text": raw_text, "bytes": len(raw_text.encode()),
                                   "sha256": hashlib.sha256(raw_text.encode()).hexdigest()}}
    for index in range(2):
        runner._journal_step(RUN, f"subscription:model:{index}", {
            "status": "completed", "outcome": {**response, "output_receipt": {}}})
    runner._journal_step(RUN, "subscription:model:2", {"status": "completed", "outcome": response})
    if unknown:
        runner._journal_step(RUN, "subscription:model:3", {"status": "completed", "outcome": {
            **response, "request_sent": "unknown", "model_calls": None}})
    failed = {"provider": agent.provider, "status": "output_invalid"}
    runner._journal_step(RUN, "subscription:result", {"status": "completed", "outcome": failed})
    runner.persist_collection(RUN, {"records": [observed["record"]], "goal": "原问题"})
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    runner.store.compare_and_append(replace(raw, status="blocked", eval_results=translate_view_edit(
        existing=raw.eval_results,
        edited_flat={**checkpoint_compat_view(raw).eval_results, "research_synthesis": failed},
    )), expected_sequence=sequence)
    before = runner._journal_entries(RUN)
    assert engine.can_resume_recorded_output(runner, RUN) is not unknown
    assert service.resume_subscription_executions()["resumed"] == (0 if unknown else 1)
    assert len(fetch.calls) == 1
    assert len(agent.inputs) == (0 if unknown else 1)
    after = runner._journal_entries(RUN)
    for key, entry in before.items():
        if key.startswith("subscription:model:"):
            assert after[key] == entry
    if not unknown:
        cp = runner.store.latest(RUN)
        assert cp.status == "passed"
        assert cp.eval_results["knowledge_publication"]["revision"] == 1
        assert agent.inputs[0]["operation"] == "independent_evidence_review"
    assert service.resume_subscription_executions()["resumed"] == 0


def test_knowledge_revision_query_returns_pinned_version_and_rejects_bad_revision(tmp_path):
    service, runner, engine, _, _ = assembled(tmp_path)
    service.resume_subscription_executions()
    cp = runner.store.latest(RUN)
    receipt = cp.eval_results['knowledge_publication']
    synthesis = cp.eval_results['research_synthesis']
    new = engine.library.save_research(
        run_id=RUN, fragment_id=cp.fragment_id, title='第二版标题',
        result=synthesis['result'], evidence=cp.eval_results['research_collection']['records'],
        status='synthesized', independent_review=synthesis['independent_review'],
        expected_revision=1,
    )
    assert new['revision'] == 2
    server = make_cognitive_decision_server(None, port=0, knowledge_library=engine.library)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = '/fragment/v1/knowledge/' + receipt['knowledge_id']
    try:
        status, body = request(server, 'GET', path)
        assert status == 200 and body['data']['revision'] == 2
        status, body = request(server, 'GET', path + '?revision=1')
        assert status == 200 and body['data']['revision'] == 1
        assert body['data']['title'] != '第二版标题'
        for query in ('0', '-1', '1.0', 'abc', '', '1000000000', '1&revision=2'):
            status, _ = request(server, 'GET', path + '?revision=' + query)
            assert status == 400, query
        status, _ = request(server, 'GET', '/fragment/v1/knowledge/periods?revision=1')
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
