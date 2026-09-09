"""Real legacy recovery fixtures with fake public transports; no subscription calls."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from common.execution import merge_runtime_eval_results
from tests.test_research_autonomy import (
    _rev10_bridge,
    _rev10_children,
    _rev10_seed_offline_lineage,
    _rev10_service,
    _rev10_vault,
    make_autonomy_runner,
)


class SelectedSubscription:
    def __init__(self, *run_ids: str) -> None:
        self.run_ids = frozenset(run_ids)
        self.calls = 0

    def enabled_for(self, run_id: str) -> bool:
        return run_id in self.run_ids

    def run(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("legacy recovery must never invoke a subscription")


def fixture(tmp_path: Path):
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "fixture.sqlite3")
    store = bridge.store
    _rev10_seed_offline_lineage(
        store, bridge, stop_reason="capability_unavailable", searches=1, fetches=1,
        search_fingerprint="ddg:public-search-v1",
    )
    runner = make_autonomy_runner(
        store, tmp_path / "runner", live=True,
        collection_enabled=True, synthesis_enabled=False,
    )
    runner.search_fingerprint = "bing-cn:public-search-v1"
    return store, runner, _rev10_service(store, bridge, runner)


def test_selected_original_skips_competing_offline_child(tmp_path: Path) -> None:
    store, runner, service = fixture(tmp_path)
    subscription = SelectedSubscription("exec:old-1")
    runner.subscription_research = subscription
    before = store.latest_sequence("exec:old-1")
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 0}
    assert service.recover_offline_executions()["recovered"] == 0
    assert _rev10_children(store) == []
    assert store.latest_sequence("exec:old-1") == before
    assert subscription.calls == 0


def test_unselected_history_retains_legacy_recovery_and_no_model_scope(tmp_path: Path) -> None:
    store, runner, service = fixture(tmp_path)
    subscription = SelectedSubscription("exec:some-other-original")
    runner.subscription_research = subscription
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 1}
    children = _rev10_children(store)
    assert len(children) == 1
    projected = next(item for item in service.list_alignments()
                     if item["alignment_id"] == children[0])
    assert projected["recovery_reason"] == "offline_zero_evidence"
    assert projected["parent_run_id"] == "exec:old-1"
    assert projected["execution_run_id"] not in subscription.run_ids
    assert subscription.calls == 0


def test_existing_child_cannot_fork_again_beside_selected_original(tmp_path: Path) -> None:
    store, runner, service = fixture(tmp_path)
    assert service.recover_offline_executions()["recovered"] == 1
    children_before = _rev10_children(store)
    child_alignment = store.latest(children_before[0])
    child_run_id = child_alignment.eval_results["alignment"]["execution_run_id"]
    raw = store.latest_raw(child_run_id)
    # A later outage makes the existing recovery child eligible again. The
    # selected original still owns resumption even though its sequence is older.
    updated = replace(raw, eval_results=merge_runtime_eval_results(
        existing=raw.eval_results,
        content={"research_collection": {
            "stop_reason": "capability_unavailable", "records": [],
            "searches": 1, "fetches": 1, "search_fingerprint": "ddg:public-search-v1",
        }},
    ))
    store.save(updated)
    assert service._is_offline_recovery_candidate(store.latest(child_run_id))
    subscription = SelectedSubscription("exec:old-1")
    runner.subscription_research = subscription
    parent_before = store.latest_sequence("exec:old-1")
    child_before = store.latest_sequence(child_run_id)
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 0}
    assert _rev10_children(store) == children_before
    assert store.latest_sequence("exec:old-1") == parent_before
    assert store.latest_sequence(child_run_id) == child_before
    assert subscription.calls == 0


def test_nonterminal_selected_original_also_owns_fragment(tmp_path: Path) -> None:
    store, runner, service = fixture(tmp_path)
    assert service.recover_offline_executions()["recovered"] == 1
    children_before = _rev10_children(store)
    child_alignment = store.latest(children_before[0])
    child_run_id = child_alignment.eval_results["alignment"]["execution_run_id"]
    child = store.latest_raw(child_run_id)
    store.save(replace(child, eval_results=merge_runtime_eval_results(
        existing=child.eval_results,
        content={"research_collection": {
            "stop_reason": "capability_unavailable", "records": [],
            "searches": 1, "fetches": 1, "search_fingerprint": "ddg:public-search-v1",
        }},
    )))
    assert service._is_offline_recovery_candidate(store.latest(child_run_id))
    raw = store.latest_raw("exec:old-1")
    store.save(replace(raw, status="running"))
    runner.subscription_research = SelectedSubscription("exec:old-1")
    # The owner set must be built before filtering for legacy terminal candidates.
    assert service.recover_offline_executions()["recovered"] == 0
    assert _rev10_children(store) == children_before
