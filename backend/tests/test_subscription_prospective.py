"""New opted-in public captures; every file/database is isolated, no model calls."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from common.checkpoint import SQLiteCheckpointStore
from common.execution import translate_view_edit
from fragment_loop.cognitive_loop import LocalCognitiveV1Loop
from fragment_loop.governed_research import GovernedResearchRunner, GovernedResearchVerifyAdapter
from fragment_loop.intent_production import production_intent_resolver
from fragment_loop.intent_service import FragmentIntentService
from fragment_loop.runtime import register_cognitive_intake
from fragment_loop.subscription_research import SubscriptionResearch

CUTOFF = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
CAPTURE = "2026-02-01T00:00:00+00:00"
FRAGMENT = "2026-02-01-public-prospective"
SEED = "https://example.org/project"


def fixture(tmp_path: Path, captured_at: str | None = CAPTURE, *, intake: bool | str = True):
    vault = tmp_path / "vault"
    inbox = vault / "Notes/散记/碎片想法"
    inbox.mkdir(parents=True)
    path = inbox / f"{FRAGMENT}.md"
    captured = f'captured_at: "{captured_at}"\n' if captured_at is not None else ""
    path.write_text(f'---\n{captured}nigo-loop: true\nsource_url: "{SEED}"\n'
                    f'---\n\n验证此公开项目是否适合正文提取 {SEED}\n')
    database = tmp_path / "state.sqlite3"
    if intake == "registrar":
        from scripts.register_nigo_loops import scan_fragments
        scanned = scan_fragments(inbox, database)
        assert len(scanned["registered"]) == 1 and not scanned["errors"]
    elif intake:
        register_cognitive_intake(path, db_path=database)
    store = SQLiteCheckpointStore(database)
    source = {"title": "公开项目适配验证", "literal_summary": "验证正文提取能力",
              "goal": "核对公开项目README与适配限制", "input_digest": "a" * 64,
              "memory_basis": [], "nigo_loop": True, "source_seed_url": SEED}
    bridge = SimpleNamespace(review_service=SimpleNamespace(vault_root=vault))
    service = FragmentIntentService(store, lambda *_: source, production_intent_resolver,
                                    continuation_bridge=bridge)
    _, alignment = service.propose({"fragment_id": FRAGMENT, "input_digest": "a" * 64,
                                    "requester": "nigo"})
    assert service.maybe_auto_advance(alignment["alignment_id"])
    current = store.latest(alignment["alignment_id"])
    run_id = current.eval_results["alignment"]["execution_run_id"]
    return service, path, run_id


def intake_id() -> str:
    import hashlib
    return "cognitive-local-v1-" + hashlib.sha256(FRAGMENT.encode()).hexdigest()


def test_new_public_capture_is_durable_but_not_a_mutable_memory_allowlist(tmp_path: Path) -> None:
    service, path, run_id = fixture(tmp_path)
    first = service.store.history(intake_id())[0]
    receipt = deepcopy(first.eval_results["cognitive_input"]["capture_receipt"])
    assert receipt["captured_at"] == CAPTURE and receipt["received_at"]
    assert service.prospective_subscription_eligible(run_id, CUTOFF)
    before = service.store.latest_sequence(intake_id())
    same = register_cognitive_intake(path, db_path=service.store.path)
    assert same.run_id == intake_id() and service.store.latest_sequence(intake_id()) == before
    assert same.eval_results["cognitive_input"]["capture_receipt"] == receipt
    restarted = FragmentIntentService(SQLiteCheckpointStore(service.store.path),
        service.source_loader, service.resolver, continuation_bridge=service.continuation_bridge)
    engine = SubscriptionResearch(object(), run_ids=frozenset(), prospective_eligibility=(
        lambda selected: restarted.prospective_subscription_eligible(selected, CUTOFF)))
    assert engine.enabled_for(run_id) and engine.run_ids == frozenset()
    assert not engine.enabled_for("exec:fragment-intent:forged")
    exact = SubscriptionResearch(object(), run_ids=frozenset({"old-exact"}))
    assert exact.enabled_for("old-exact") and not exact.enabled_for(run_id)
    with pytest.raises(ValueError, match="prospective_context_forbidden"):
        SubscriptionResearch(object(), run_ids=frozenset(),
            prospective_eligibility=lambda _: True, project_context=[{"private": "do not send"}])


@pytest.mark.parametrize("captured_at", [None, "", "bad", "2026-02-30T00:00:00Z",
    "2026-02-01T00:00:00", "2025-12-31T23:59:59Z", "2099-01-01T00:00:00Z"])
def test_missing_invalid_naive_old_or_future_capture_never_qualifies(
    tmp_path: Path, captured_at: str | None,
) -> None:
    service, _, run_id = fixture(tmp_path, captured_at)
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)


def test_legacy_first_intake_is_never_backfilled_by_new_registration_or_later_receipt(
    tmp_path: Path,
) -> None:
    service, path, run_id = fixture(tmp_path, intake=False)
    from fragment_loop.intake import fragment_title_for, load_fragment
    fragment = load_fragment(path)
    title, title_source = fragment_title_for(fragment)
    LocalCognitiveV1Loop(service.store, lambda *_: {}).register_intake(
        fragment_id=FRAGMENT, fragment_text=fragment.raw_content, source_ref=str(path.resolve()),
        fragment_title=title, fragment_title_source=title_source)
    first = service.store.history(intake_id())[0]
    assert "capture_receipt" not in first.eval_results["cognitive_input"]
    repeated = register_cognitive_intake(path, db_path=service.store.path)
    assert repeated.run_id == first.run_id
    assert "capture_receipt" not in repeated.eval_results["cognitive_input"]
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)
    # Even a later checkpoint carrying a receipt does not replace the first intake.
    current = service.store.latest_raw(intake_id())
    edited = deepcopy(service.store.latest(intake_id()).eval_results)
    edited["cognitive_input"]["capture_receipt"] = {
        "version": "fragment-capture-v1", "captured_at": CAPTURE,
        "received_at": "2026-02-01T01:00:00Z", "raw_sha256": "f" * 64,
        "source_ref": str(path.resolve())}
    service.store.save(replace(current, eval_results=translate_view_edit(
        existing=current.eval_results, edited_flat=edited)))
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)


@pytest.mark.parametrize("raw_change", [
    lambda raw: raw.replace("nigo-loop: true", "nigo-loop: false"),
    lambda raw: raw.replace("nigo-loop: true", "nigo-loop: true\nprivacy_level: restricted"),
    lambda raw: raw.replace(SEED, "https://127.0.0.1/private"),
    lambda raw: raw.replace(CAPTURE, "2026-03-01T00:00:00+00:00"),
])
def test_changed_current_authorization_or_capture_is_not_sent(
    tmp_path: Path, raw_change: Any,
) -> None:
    service, path, run_id = fixture(tmp_path)
    assert service.prospective_subscription_eligible(run_id, CUTOFF)
    path.write_text(raw_change(path.read_text()))
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)


@pytest.mark.parametrize("field,value", [
    ("alignment_id", "align:unknown"), ("alignment_digest", "b" * 64),
    ("input_digest", "b" * 64), ("source_seed_url", "https://example.org/other"),
    ("route", "direct"), ("parent_run_id", "exec:old"),
])
def test_execution_binding_drift_or_child_lineage_is_not_authorization(
    tmp_path: Path, field: str, value: str,
) -> None:
    service, _, run_id = fixture(tmp_path)
    raw = service.store.latest_raw(run_id)
    values = deepcopy(service.store.latest(run_id).eval_results)
    values["execution_binding"][field] = value
    service.store.save(replace(raw, eval_results=translate_view_edit(
        existing=raw.eval_results, edited_flat=values)))
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)


def test_old_capture_remains_old_when_new_alignment_is_created(tmp_path: Path) -> None:
    service, _, run_id = fixture(tmp_path, "2025-12-31T23:00:00Z")
    source = {**service.source_loader(FRAGMENT, "a" * 64), "input_digest": "b" * 64}
    service.source_loader = lambda *_: source
    _, new = service.propose({"fragment_id": FRAGMENT, "input_digest": "b" * 64,
                              "requester": "nigo"})
    assert service.maybe_auto_advance(new["alignment_id"])
    new_alignment = service.store.latest(new["alignment_id"]).eval_results["alignment"]
    new_run = new_alignment["execution_run_id"]
    assert new_run != run_id
    assert not service.prospective_subscription_eligible(new_run, CUTOFF)


def test_resume_scans_current_eligible_runs_without_expanding_stored_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, run_id = fixture(tmp_path)
    runner = GovernedResearchRunner(service.store)
    engine = SubscriptionResearch(object(), run_ids=frozenset({"exact-existing"}),
        prospective_eligibility=lambda selected: service.prospective_subscription_eligible(
            selected, CUTOFF))
    runner.subscription_research = engine
    service.verify_adapter = GovernedResearchVerifyAdapter(runner)
    called = []
    monkeypatch.setattr(service, "_resume_subscription_execution",
                        lambda _runner, _engine, selected: called.append(selected) or 0)
    result = service.resume_subscription_executions()
    assert set(called) == {run_id, "exact-existing"} and result["scanned"] == 2
    assert engine.run_ids == frozenset({"exact-existing"})


@pytest.mark.parametrize("stamp", ["bad", "2026-02-30T00:00:00Z", "2026-02-01T00:00:00"])
def test_cli_cutover_is_explicit_and_requires_timezone(stamp: str, capsys: Any) -> None:
    from fragment_loop.cognitive_server import main
    with pytest.raises(SystemExit) as exit_info:
        main(["--subscription-new-public-after", stamp])
    assert exit_info.value.code == 2
    assert "requires valid ISO time with timezone" in capsys.readouterr().err


def test_prospective_switch_cannot_enable_paid_api_flags(capsys: Any) -> None:
    from fragment_loop.cognitive_server import main
    with pytest.raises(SystemExit) as exit_info:
        main(["--subscription-new-public-after", CAPTURE, "--research-model-live"])
    assert exit_info.value.code == 2
    assert "cannot be combined with paid API live flags" in capsys.readouterr().err


def test_named_runtime_metadata_can_change_without_changing_capture_identity(
    tmp_path: Path,
) -> None:
    service, path, run_id = fixture(tmp_path)
    before = deepcopy(service.store.history(intake_id())[0].eval_results["cognitive_input"])
    raw = path.read_text().replace("nigo-loop: true", "nigo-loop: true\n"
        "pipeline_status: organized\nprocessed_at: 2026-02-02T00:00:00Z\n"
        "organized_at: 2026-02-02T00:00:00Z")
    path.write_text(raw)
    assert service.prospective_subscription_eligible(run_id, CUTOFF)
    again = register_cognitive_intake(path, db_path=service.store.path)
    assert again.eval_results["cognitive_input"] == before
    # Unrecognized metadata is not silently discarded from the identity.
    path.write_text(raw.replace("nigo-loop: true",
                                "nigo-loop: true\nuser_note: a changed question"))
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)


def test_pre_cutover_server_receipt_cannot_become_a_new_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fragment_loop.cognitive_loop.utc_now", lambda: "2025-12-31T23:00:00Z")
    service, path, run_id = fixture(tmp_path)
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)
    monkeypatch.setattr("fragment_loop.cognitive_loop.utc_now", lambda: "2026-03-01T00:00:00Z")
    again = register_cognitive_intake(path, db_path=service.store.path)
    assert again.eval_results["cognitive_input"]["capture_receipt"]["received_at"] == (
        "2025-12-31T23:00:00Z")
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)


def test_production_registrar_creates_first_receipt_and_preserves_it_on_rescan(
    tmp_path: Path,
) -> None:
    from scripts.register_nigo_loops import scan_fragments
    service, path, run_id = fixture(tmp_path, intake="registrar")
    original = deepcopy(service.store.history(intake_id())[0].eval_results["cognitive_input"])
    assert service.prospective_subscription_eligible(run_id, CUTOFF)
    assert original["fragment_text"] == f"验证此公开项目是否适合正文提取 {SEED}"
    assert original["capture_receipt"]["source_ref"] == str(path.resolve())
    # UI uses two newlines after frontmatter; registrar and eligibility both
    # preserve the same stripped body. Normal organizer metadata is not identity.
    path.write_text(path.read_text().replace("nigo-loop: true", "nigo-loop: true\n"
        "pipeline_status: organized\nprocessed_at: 2026-02-02T00:00:00Z"))
    scanned = scan_fragments(path.parent, service.store.path)
    assert len(scanned["existing"]) == 1 and not scanned["registered"]
    assert not scanned["errors"] and not scanned["binding_conflicts"]
    assert service.store.history(intake_id())[0].eval_results["cognitive_input"] == original
    assert service.prospective_subscription_eligible(run_id, CUTOFF)


def test_registrar_never_adds_receipt_to_existing_legacy_intake(tmp_path: Path) -> None:
    from fragment_loop.intake import fragment_title_for, load_fragment
    from scripts.register_nigo_loops import scan_fragments
    service, path, run_id = fixture(tmp_path, intake=False)
    fragment = load_fragment(path)
    title, title_source = fragment_title_for(fragment)
    LocalCognitiveV1Loop(service.store, lambda *_: {}).register_intake(
        fragment_id=FRAGMENT, fragment_text=fragment.raw_content, source_ref=str(path.resolve()),
        privacy_level=fragment.privacy_level, fragment_title=title,
        fragment_title_source=title_source)
    scanned = scan_fragments(path.parent, service.store.path)
    assert len(scanned["existing"]) == 1 and not scanned["errors"]
    current_input = service.store.latest(intake_id()).eval_results["cognitive_input"]
    assert "capture_receipt" not in current_input
    assert not service.prospective_subscription_eligible(run_id, CUTOFF)


@pytest.mark.parametrize("extra", ["received_at", "version"])
def test_internal_capture_metadata_cannot_override_server_receipt(
    tmp_path: Path, extra: str,
) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    loop = LocalCognitiveV1Loop(store, lambda *_: {})
    with pytest.raises(ValueError, match="capture metadata fields invalid"):
        loop._register(fragment_id=FRAGMENT, fragment_text="Public source question", route=None,
            input_refs=["/isolated/source.md"], capture_metadata={
                "captured_at": CAPTURE, "raw_sha256": "a" * 64,
                "identity_sha256": "b" * 64, "source_ref": "/isolated/source.md",
                extra: "2099-01-01T00:00:00Z"})
    assert store.latest(intake_id()) is None
