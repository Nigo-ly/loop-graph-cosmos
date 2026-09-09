"""SQLite checkpoint and idempotency tests."""

from dataclasses import replace
from pathlib import Path

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore, make_idempotency_key


def checkpoint() -> LoopCheckpoint:
    return LoopCheckpoint(
        loop_id="phone-fragment-link-v1",
        loopspec_version="1.0.0",
        run_id="run-1",
        fragment_id="fragment-1",
        current_node="intake",
        status="approved",
        goal="test",
    )


def test_checkpoint_round_trip_and_history(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    first = store.save(checkpoint())
    second = store.save(replace(first, status="running", iteration=1))

    assert store.latest("run-1") == second
    assert [item.status for item in store.history("run-1")] == ["approved", "running"]
    assert first.event_type == "snapshot"
    assert second.supersedes_sequence is not None


def test_prepare_action_is_atomic_and_idempotent(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    saved = store.save(checkpoint())

    prepared, key, is_new = store.prepare_action(
        saved,
        action_type="fetch",
        target="HTTPS://EXAMPLE.COM/a?b=2&a=1#fragment",
    )
    repeated, repeated_key, repeated_is_new = store.prepare_action(
        prepared,
        action_type="fetch",
        target="https://example.com/a?a=1&b=2",
    )

    assert is_new is True
    assert repeated_is_new is False
    assert repeated_key == key
    assert store.latest("run-1") == repeated
    assert store.action_status(key) == "reserved"

    completed = store.complete_action(repeated, key, "artifact://response")
    assert store.action_status(key) == "completed"
    assert completed.pending_action is None
    assert key in completed.completed_actions
    record = store.action_record(key)
    assert record is not None
    assert record["reservation_mode"] == "pre_execution"
    assert completed.event_type == "action_completed"
    assert completed.revision_reason == "fetch"


def test_reconcile_action_is_explicitly_post_execution(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    saved = store.save(checkpoint())

    reconciled, key, is_new = store.reconcile_action(
        saved,
        action_type="write_wiki",
        target="/vault/wiki/example.md",
        result_ref="wiki://example",
    )

    assert is_new is True
    assert key in reconciled.completed_actions
    assert store.action_status(key) == "completed"
    record = store.action_record(key)
    assert record is not None
    assert record["reservation_mode"] == "reconciled"


def test_idempotency_key_separates_actions() -> None:
    first = make_idempotency_key("loop", "fragment", "node", "fetch", "https://a.test")
    second = make_idempotency_key("loop", "fragment", "node", "write", "https://a.test")
    assert first != second
