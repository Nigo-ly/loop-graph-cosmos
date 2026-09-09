"""Current subscription capability projections must not rewrite historical scope."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.execution import translate_view_edit
from fragment_loop.governed_research import GovernedResearchRunner, GovernedResearchVerifyAdapter
from fragment_loop.repository_trial import run_repository_trial
from fragment_loop.subscription_research import MAX_ROUNDS, MAX_TOOLS, SubscriptionResearch
from tests.test_fragment_intent_service import decision_body, proposal_body, result, service
from tests.test_raw_subscription_intake import CUTOFF, Env


def configured(tmp_path: Path):
    env = Env(tmp_path, "Assess the public project with evidence and clear limits")
    env.scan()
    alignment_id = env.service.list_alignments()[0]["alignment_id"]
    env.service.maybe_auto_advance(alignment_id)
    run_id = env.service.get_alignment(alignment_id)["execution_run_id"]
    engine = SubscriptionResearch(SimpleNamespace(provider="kimi_subscription"),
        run_ids=frozenset(), trial=run_repository_trial,
        library=SimpleNamespace(save_research=lambda **_: None), prospective_eligibility=(
            lambda selected: env.service.prospective_subscription_eligible(selected, CUTOFF)))
    runner = GovernedResearchRunner(env.service.store, subscription_research=engine)
    env.service.verify_adapter = GovernedResearchVerifyAdapter(runner)
    return env, alignment_id, run_id, engine


def test_current_scope_is_additive_read_only_and_not_evidence_of_execution(tmp_path: Path) -> None:
    env, alignment_id, run_id, _engine = configured(tmp_path)
    original = deepcopy(env.service.store.latest(alignment_id).eval_results["alignment"])
    before = {key: env.service.store.latest_sequence(key) for key in (alignment_id, run_id)}
    projected = env.service.get_alignment(alignment_id)
    scope = projected["effective_execution_scope"]
    assert scope == projected["execution"]["effective_execution_scope"]
    assert projected["execution"]["subscription_selected"] is True
    assert projected["execution_scope"] == original["execution_scope"]
    assert projected["execution_scope"]["model_provider"] == "deepseek"
    assert projected["execution_scope_basis"] == "original_alignment_proposal"
    assert projected["exclusions"] == original["exclusions"]
    assert projected["exclusions_basis"] == "original_alignment_proposal"
    assert scope["model_provider"] == "kimi_subscription" and scope["model_name"] is None
    assert scope["basis"] == "currently_selected_subscription_configuration"
    assert scope["selection_basis"] == "prospective_public_capture"
    assert scope["billing_mode"] == "existing_subscription"
    assert scope["model_call_cap"] == MAX_ROUNDS == 10
    assert scope["tool_call_cap"] == MAX_TOOLS == 16
    assert scope["model_call_cap_basis"] == (
        "total_agent_invocation_slots_including_unknown_and_review")
    assert scope["tool_call_cap_basis"] == "total_new_tool_receipts_per_run"
    assert scope["repository_trial_allowed"] is True
    assert scope["automatic_knowledge_save_allowed"] is True
    assert scope["observed_model_provider"] is None
    assert scope["knowledge_publication_status"] == "not_recorded"
    assert before == {key: env.service.store.latest_sequence(key) for key in before}
    assert env.service.store.latest(alignment_id).eval_results["alignment"] == original


def test_current_configuration_does_not_relabel_historical_receipts(tmp_path: Path) -> None:
    env, alignment_id, run_id, engine = configured(tmp_path)
    raw, seq = env.service.store.latest_raw_with_sequence(run_id)
    values = {**env.service.store.latest(run_id).eval_results,
        "research_synthesis": {"status": "output_invalid", "provider": "codex_subscription"},
        "knowledge_publication": {"knowledge_id": "knowledge-" + "a" * 24,
            "revision": 1, "status": "unavailable"}}
    env.service.store.compare_and_append(replace(raw, eval_results=translate_view_edit(
        existing=raw.eval_results, edited_flat=values)), expected_sequence=seq)
    scope = env.service.get_alignment(alignment_id)["effective_execution_scope"]
    assert scope["model_provider"] == "kimi_subscription"
    assert scope["observed_model_provider"] == "codex_subscription"
    assert scope["knowledge_publication_status"] == "unavailable"
    engine.trial = lambda *_: None  # Arbitrary adapter does not imply production isolation.
    engine.library = None
    scope = env.service.get_alignment(alignment_id)["effective_execution_scope"]
    assert scope["repository_trial_allowed"] is False
    assert scope["automatic_knowledge_save_allowed"] is False
    assert "Obsidian" not in " ".join(scope["write_scope"])


def test_current_unselected_run_has_no_inferred_subscription_scope(tmp_path: Path) -> None:
    env, alignment_id, _run_id, _engine = configured(tmp_path)
    env.raw.write_text(env.raw.read_text().replace("nigo-loop: true", "nigo-loop: false"))
    projected = env.service.get_alignment(alignment_id)
    assert projected["execution"]["subscription_selected"] is False
    assert projected["execution"]["effective_execution_scope"] is None
    assert projected["effective_execution_scope"] is None
    assert projected["execution_scope"]["model_provider"] == "deepseek"


@pytest.mark.parametrize("count,passed", [(5, True), (MAX_ROUNDS, True), (MAX_ROUNDS + 1, False)])
def test_initial_subscription_execution_uses_current_total_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, passed: bool,
) -> None:
    payload = {**result(), "model_calls": count}
    target = service(tmp_path, verify_adapter=lambda _: payload)
    _, proposed = target.propose(proposal_body())
    engine = SubscriptionResearch(SimpleNamespace(provider="kimi_subscription"),
        run_ids=frozenset(), prospective_eligibility=lambda _: True)
    monkeypatch.setattr(target, "_research_runner", lambda: SimpleNamespace(
        subscription_research=engine))
    _, confirmed = target.decide(str(proposed["alignment_id"]), decision_body(proposed))
    output = target.run_execution(str(confirmed["execution_run_id"]))
    assert (output["status"] == "passed") is passed
    assert (output["result"] is not None) is passed
