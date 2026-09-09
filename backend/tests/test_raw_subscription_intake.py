"""Real registrar/bridge/coordinator wiring; isolated vault, no model or network."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from fragment_loop.continuation_bridge import ContinuationBridgeError, FragmentContinuationBridge
from fragment_loop.governed_research import GovernedResearchRunner, GovernedResearchVerifyAdapter
from fragment_loop.intent_production import production_intent_resolver
from fragment_loop.intent_service import (
    FragmentIntentError,
    FragmentIntentService,
    ResearchWatchCoordinator,
    propose_organized_vault_intents,
)
from fragment_loop.product_review import ProductReviewService
from fragment_loop.subscription_research import SubscriptionResearch
from scripts.register_nigo_loops import scan_fragments

FRAGMENT = "2026-02-01-03-00-52-abcdef-desktop"
CUTOFF = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
CAPTURE = "2026-02-01T00:00:00Z"
SEED = "https://example.org/public-project"


class Env:
    def __init__(self, root: Path, question: str, *, captured_at: str = CAPTURE,
                 approved: bool = True, privacy: str = "personal", seed: str = SEED,
                 intake: bool = True, enabled: bool = True) -> None:
        self.vault = root / "vault"
        self.raw = self.vault / f"Notes/散记/碎片想法/{FRAGMENT}.md"
        self.organized = self.vault / f"Notes/散记/已整理碎片/{FRAGMENT}.md"
        self.raw.parent.mkdir(parents=True)
        self.body = f"{seed}\n\n{question}"
        self.raw.write_text(f'---\ntype: "碎片想法"\ncaptured_at: "{captured_at}"\n'
            f'pipeline_status: queued\nnigo-loop: {str(approved).lower()}\n'
            f'privacy_level: {privacy}\nsource_url: "{seed}"\n---\n\n{self.body}\n')
        self.db = root / "state.sqlite3"
        if intake:
            scan_fragments(self.raw.parent, self.db)
        candidates, assets = self.vault / "candidates", self.vault / "assets"
        candidates.mkdir()
        assets.mkdir()
        review = ProductReviewService(candidates, assets, vault_root=self.vault)
        self.bridge = FragmentContinuationBridge(review, loop_db=self.db,
            prospective_public_after=CUTOFF if enabled else None)
        self.service = FragmentIntentService(self.bridge.store, self.bridge.load_intent_source,
            production_intent_resolver, continuation_bridge=self.bridge)

    def scan(self):
        return propose_organized_vault_intents(vault_root=self.vault,
            continuation_bridge=self.bridge, intent_service=self.service)


@pytest.mark.parametrize("question", [
    "Assess whether this project is suitable for local extraction. " * 18
    + "Required final constraint: preserve literal &lt; and &amp;lt; without decoding them.\n"
    + "Python example:\nif True:\n    print('literal &lt;')\n\t# retain indentation",
    "请判断这个公开项目能否作为本地页面处理的基础，说明证据支持的范围与限制。" * 20
    + "最后必须回答：中文及混合编码是否保留，未实测时明确说明。",
])
def test_native_raw_flows_automatically_to_verify_with_the_complete_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, question: str,
) -> None:
    env = Env(tmp_path, question)
    source = env.bridge.discover_intent_source(FRAGMENT)
    assert source["source_origin"] == "raw_capture" and "organized_at" not in source
    assert source["goal"] == env.body and len(source["goal"]) > 500
    assert len(source["literal_summary"]) <= 800
    coordinator = ResearchWatchCoordinator(env.service, auto_propose_scanner=env.scan)
    counts = coordinator.scan_once()
    assert counts["auto_proposed"] == 1
    alignments = env.service.list_alignments()
    assert len(alignments) == 1
    alignment = alignments[0]
    assert alignment["source_origin"] == "raw_capture"
    assert alignment["recommended_route"] == "verify" and alignment["route"] == "verify"
    assert alignment["decision_source"] == "system_policy"
    run_id = alignment["execution_run_id"]
    cp = env.service.store.latest(run_id)
    assert cp.fragment_title_source == "raw_capture"
    binding = cp.eval_results["execution_binding"]
    assert binding["source_origin"] == "raw_capture" and binding["goal"] == source["goal"]
    assert env.service.prospective_subscription_eligible(run_id, CUTOFF)
    assert not env.organized.exists() and list(env.vault.rglob("*.md")) == [env.raw]
    engine = SubscriptionResearch(object(), run_ids=frozenset(), prospective_eligibility=(
        lambda selected: env.service.prospective_subscription_eligible(selected, CUTOFF)))
    runner = GovernedResearchRunner(env.service.store, subscription_research=engine)
    seen: list[str] = []

    class ReachedSubscriptionError(Exception):
        pass

    def no_model(_runner, selected, goal):
        assert selected == run_id
        seen.append(goal)
        raise ReachedSubscriptionError

    monkeypatch.setattr(engine, "run", no_model)
    with pytest.raises(ReachedSubscriptionError):
        GovernedResearchVerifyAdapter(runner)({**binding, "execution_run_id": run_id})
    assert len(seen) == 1 and source["goal"] in seen[0]
    assert question in seen[0]


def test_later_organized_and_runtime_metadata_do_not_replace_raw_task(tmp_path: Path) -> None:
    env = Env(tmp_path, "Evaluate original question, including the original final constraint.")
    source = env.bridge.discover_intent_source(FRAGMENT)
    assert env.scan()["proposed"] == 1
    first = env.service.list_alignments()[0]
    assert env.service.maybe_auto_advance(first["alignment_id"])
    run_id = env.service.get_alignment(first["alignment_id"])["execution_run_id"]
    env.organized.parent.mkdir(parents=True)
    env.organized.write_text('---\ntype: 已整理碎片\n'
        f'source_file: {FRAGMENT}.md\norganized_at: 2026-02-02T00:00:00Z\n'
        'title: Different organizer title\ngoal: Different and incomplete goal\n'
        'next_action: Read elsewhere\npipeline_status: ready_to_review\n'
        'experiment_status: ready\n---\nExtra organizer conclusions\n')
    env.raw.write_text(env.raw.read_text().replace('pipeline_status: queued',
        'pipeline_status: organized\nprocessed_at: 2026-02-02T00:00:00Z'))
    assert env.scan()["proposed"] == 0 and len(env.service.list_alignments()) == 1
    assert env.bridge.load_intent_source(FRAGMENT, source["input_digest"]) == source
    restarted = FragmentContinuationBridge(env.bridge.review_service, loop_db=env.db,
        prospective_public_after=CUTOFF)
    assert restarted.load_intent_source(FRAGMENT, source["input_digest"]) == source
    assert env.service.prospective_subscription_eligible(run_id, CUTOFF)
    assert env.service.get_alignment(first["alignment_id"])["execution_run_id"] == run_id
    assert env.organized.read_text().endswith('Extra organizer conclusions\n')


def test_concurrent_raw_scan_creates_one_alignment(tmp_path: Path) -> None:
    env = Env(tmp_path, "Compare this public project with the actual requirement.")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: env.scan(), range(2)))
    assert len(env.service.list_alignments()) == 1
    assert not env.organized.exists()


@pytest.mark.parametrize("options", [
    {"captured_at": "2025-12-31T23:59:59Z"}, {"captured_at": ""},
    {"captured_at": "invalid"}, {"captured_at": "2026-02-01T00:00:00"},
    {"captured_at": "2099-01-01T00:00:00Z"}, {"approved": False},
    {"privacy": "restricted"}, {"seed": "https://127.0.0.1/private"},
    {"intake": False}, {"enabled": False},
])
def test_ineligible_raw_is_never_promoted_to_a_research_source(tmp_path: Path, options) -> None:
    env = Env(tmp_path, "Public research question", **options)
    with pytest.raises(ContinuationBridgeError):
        env.bridge.discover_intent_source(FRAGMENT)
    assert env.scan()["proposed"] == 0 and env.service.list_alignments() == []
    assert not env.organized.exists()


def test_client_cannot_supply_raw_origin_or_create_missing_intake_receipt(tmp_path: Path) -> None:
    env = Env(tmp_path, "Public research question", intake=False)
    with pytest.raises(FragmentIntentError, match="invalid_body"):
        env.service.propose({"fragment_id": FRAGMENT, "input_digest": "a" * 64,
            "requester": "nigo", "source_origin": "raw_capture"})
    assert env.service.list_alignments() == []


def test_changed_original_question_fails_source_reload_and_never_gets_new_task(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path, "Original question")
    source = env.bridge.discover_intent_source(FRAGMENT)
    assert env.scan()["proposed"] == 1
    env.raw.write_text(env.raw.read_text().replace("Original question", "Changed question"))
    with pytest.raises(ContinuationBridgeError):
        env.bridge.load_intent_source(FRAGMENT, source["input_digest"])
    assert env.scan()["proposed"] == 0 and len(env.service.list_alignments()) == 1


@pytest.mark.parametrize("bad", ["", "x" * 20001, "code\x00bad", "code\x7fbad"])
def test_raw_goal_preserves_format_without_relaxing_size_or_control_guard(bad: str) -> None:
    from fragment_loop.intent_service import _validate_source
    with pytest.raises(FragmentIntentError, match="goal_invalid"):
        _validate_source({"title": "Raw title", "literal_summary": "Raw preview",
            "memory_basis": [], "goal": bad, "input_digest": "a" * 64,
            "source_origin": "raw_capture", "source_seed_url": SEED, "nigo_loop": True},
            "a" * 64)


def test_revoked_raw_capture_does_not_fall_back_to_public_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fragment_loop.governed_research import GovernedResearchError
    env = Env(tmp_path, "Original public question")
    assert env.scan()["proposed"] == 1
    alignment = env.service.list_alignments()[0]
    assert env.service.maybe_auto_advance(alignment["alignment_id"])
    run_id = env.service.get_alignment(alignment["alignment_id"])["execution_run_id"]
    binding = env.service.store.latest(run_id).eval_results["execution_binding"]
    engine = SubscriptionResearch(object(), run_ids=frozenset(), prospective_eligibility=(
        lambda selected: env.service.prospective_subscription_eligible(selected, CUTOFF)))
    runner = GovernedResearchRunner(env.service.store, subscription_research=engine)
    env.raw.write_text(env.raw.read_text().replace("nigo-loop: true", "nigo-loop: false"))
    calls = []
    monkeypatch.setattr(runner, "collect", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(GovernedResearchError, match="subscription_not_authorized"):
        GovernedResearchVerifyAdapter(runner)({**binding, "execution_run_id": run_id})
    assert calls == []
