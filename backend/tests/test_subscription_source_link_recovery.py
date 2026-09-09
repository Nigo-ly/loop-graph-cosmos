"""Historical source-bound seed recovery; isolated stores, no I/O execution."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from common.checkpoint import checkpoint_compat_view
from common.execution import translate_view_edit
from fragment_loop.continuation_bridge import ContinuationBridgeError
from fragment_loop.governed_research import GovernedResearchVerifyAdapter
from fragment_loop.intent_service import FragmentIntentError

from .test_fragment_continuation_bridge import FRAGMENT_ID, Env
from .test_subscription_closure import assembled
from .test_subscription_research import RUN

SEED = "https://view.inews.qq.com/a/20260826A09MC600"
PRIVATE = "PRIVATE_RAW_CANARY_DO_NOT_SEND"


class ReachedResearchError(Exception):
    pass


def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, started: bool):
    runner_path = tmp_path / "runner"
    runner_path.mkdir()
    service, runner, engine, agent, fetch = assembled(runner_path)
    env = Env(tmp_path / "source")
    env.raw_path.write_text(
        env.raw_path.read_text().replace(
            'pipeline_status: "priority_queued"',
            f'pipeline_status: "priority_queued"\nsource_url: "{SEED}"',
        )
        + f"\n{PRIVATE}\n"
    )
    source = env.bridge.discover_intent_source(FRAGMENT_ID)
    assert source["source_seed_url"] == SEED
    raw, sequence = runner.store.latest_raw_with_sequence(RUN)
    binding = {"route": "verify", "title": "历史问题标题", "input_digest": source["input_digest"]}
    values = {**checkpoint_compat_view(raw).eval_results, "execution_binding": binding}
    original_goal = "历史问题标题\n原始整理目标，保留这个问题。\n  与原空白。"
    if started:
        values["research_collection"] = {
            "goal": original_goal,
            "records": [],
            "searches": 0,
            "fetches": 0,
        }
        values["research_synthesis"] = {
            "status": "subscription_in_progress",
            "provider": "kimi_subscription",
        }
    runner.store.compare_and_append(
        replace(
            raw,
            fragment_id=FRAGMENT_ID,
            eval_results=translate_view_edit(existing=raw.eval_results, edited_flat=values),
        ),
        expected_sequence=sequence,
    )
    loaded = []

    def loader(fragment_id, input_digest):
        loaded.append((fragment_id, input_digest))
        return env.bridge.load_intent_source(fragment_id, input_digest)

    service.source_loader = loader
    received = []

    def no_execution(_runner, run_id, goal):
        assert run_id == RUN
        received.append(goal)
        raise ReachedResearchError

    monkeypatch.setattr(engine, "run", no_execution)
    return (
        service,
        runner,
        engine,
        env,
        source,
        binding,
        original_goal,
        received,
        loaded,
        agent,
        fetch,
    )


@pytest.mark.parametrize("started", [False, True])
def test_historical_verified_source_seed_reaches_research_without_private_raw_or_history_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started: bool,
) -> None:
    service, runner, engine, _, source, binding, original, received, loaded, agent, fetch = legacy(
        tmp_path, monkeypatch, started=started
    )
    history = runner.store.history(RUN)
    for _ in range(2):
        with pytest.raises(ReachedResearchError):
            service._resume_subscription_execution(runner, engine, RUN)
    assert received[0] == received[1]
    assert received[0].count(SEED) == 1
    if started:
        assert received[0] == original + "\n" + SEED
        assert source["goal"] not in received[0], (
            "a recovered URL cannot replace the recorded question with organizer text"
        )
    else:
        assert source["goal"] in received[0]
    assert PRIVATE not in received[0]
    assert loaded == [(FRAGMENT_ID, binding["input_digest"])] * 2
    assert runner.store.history(RUN) == history
    assert agent.inputs == [] and fetch.calls == []


@pytest.mark.parametrize("which", ["raw", "organized"])
def test_old_collection_does_not_bypass_original_input_digest_when_recovering_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
) -> None:
    service, runner, engine, env, _, _, _, received, _, agent, fetch = legacy(
        tmp_path, monkeypatch, started=True
    )
    path = env.raw_path if which == "raw" else env.organized_path
    path.write_text(path.read_text() + "\nchanged after original input binding\n")
    history = runner.store.history(RUN)
    with pytest.raises(ContinuationBridgeError, match="source_changed"):
        service._resume_subscription_execution(runner, engine, RUN)
    assert received == [] and agent.inputs == [] and fetch.calls == []
    assert runner.store.history(RUN) == history


@pytest.mark.parametrize(
    "seed",
    [
        "https://127.0.0.1/private",
        "https://localhost/private",
        "https://user:secret@example.com/path",
        "https://example.com/path?token=private",
        "https://example.com/path#private",
    ],
)
def test_recovered_seed_keeps_existing_source_locator_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: str,
) -> None:
    service, runner, engine, _, source, _, _, received, _, _, fetch = legacy(
        tmp_path, monkeypatch, started=True
    )
    service.source_loader = lambda *_: {**source, "source_seed_url": seed}
    with pytest.raises(FragmentIntentError, match="source_invalid"):
        service._resume_subscription_execution(runner, engine, RUN)
    assert received == [] and fetch.calls == []


@pytest.mark.parametrize("recorded", [None, "目标\n" + SEED, "目标\n[来源](" + SEED + ")"])
def test_already_bound_or_raw_question_source_is_not_duplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded: str | None,
) -> None:
    service, runner, engine, _, _, _, _, received, _, _, _ = legacy(
        tmp_path, monkeypatch, started=False
    )
    with pytest.raises(ReachedResearchError):
        GovernedResearchVerifyAdapter(runner)(
            {
                "execution_run_id": RUN,
                "title": "标题",
                "source_origin": "raw_capture",
                "goal": "原问题\n" + SEED,
                "source_seed_url": SEED,
            },
            recorded_goal=recorded,
        )
    assert received[0].count(SEED) == 1
    if recorded is not None:
        assert received[0] == recorded
