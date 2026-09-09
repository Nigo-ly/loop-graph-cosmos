"""自主认知闭环（TASK-LOOP-GRAPH-AUTONOMOUS-COGNITION）反例：全离线。

覆盖任务书 §G 的后端项：双轴认知态、能力解耦、反馈环（缺口第二轮/冲突
反证/真实耗尽/watching 恢复幂等）、安全自动路线（system_policy + 人类
闸门）、旧 run 兼容与既有资产零改写。所有 transport/凭据/价格均为 fake。
"""
# mypy: disable-error-code="no-untyped-def,untyped-decorator"

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop import governed_research as gr
from fragment_loop.governed_research import (
    GovernedResearchRunner,
    detect_conflict_types,
    research_cognitive_stage,
)
from fragment_loop.research_fetch import (
    build_counterexample_queries,
    build_replan_queries,
)
from graph_runtime.research_bridge import research_adapters
from graph_runtime.runtime import GraphRuntime
from graph_runtime.specs.fragment_research_macro_v3 import (
    RESEARCH_MACRO_V3_GRAPH_ID,
    RESEARCH_MACRO_V3_SPEC,
    RESEARCH_MACRO_V3_SPEC_DIGEST,
)

from .test_fragment_governed_research import (
    DDG_PAGE,
    GOAL,
    ExplodingCredentialReader,
    ExplodingSearchTransport,
    FakeFetchTransport,
    FakeSearchTransport,
    FrozenClock,
    FrozenMonotonic,
    make_run,
)

NOW = datetime(2026, 8, 9, 0, 0, 0, tzinfo=UTC)
EMPTY_DDG = b"<html><body>no results</body></html>"
COUNTER_DDG = b'<a class="result__a" href="https://example.com/issues">Example Issues</a>'
ISSUE_BODY = (
    b"<html><body><h1>Example Issues</h1>"
    b"<p>example unsupported failing incompatible deprecated avoid risk security</p></body></html>"
)
RELEASE_BODY = (
    b"<html><body><h1>Example Release Notes</h1>"
    b"<p>example released supports available compatible recommended</p></body></html>"
)


def make_autonomy_runner(
    store: SQLiteCheckpointStore,
    tmp_path: Path,
    *,
    live: bool = False,
    collection_enabled: bool | None = None,
    synthesis_enabled: bool | None = None,
    search=None,
    fetch=None,
    clock=None,
    monotonic=None,
    ledger=None,
) -> GovernedResearchRunner:
    from graph_runtime.agent_ledger import AgentLedgerStore

    return GovernedResearchRunner(
        store,
        live_enabled=live,
        collection_enabled=collection_enabled,
        synthesis_enabled=synthesis_enabled,
        search_transport=search if search is not None else FakeSearchTransport(DDG_PAGE),
        fetch_transport=fetch if fetch is not None else FakeFetchTransport(),
        ledger=ledger or AgentLedgerStore(tmp_path / "ledger.sqlite3"),
        credential_reader=ExplodingCredentialReader(),
        clock=clock or FrozenClock(),
        monotonic=monotonic or FrozenMonotonic(),
    )


def active_records(outcome: dict) -> list[dict]:
    return [
        record
        for record in outcome["records"]
        if record.get("marker") in ("inherited", "newly_collected")
    ]


# -- G1：live_disabled + 0 网络 → capability_offline，绝不是「来源不足」 ------


def test_g1_live_disabled_zero_network_is_capability_offline(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    search = FakeSearchTransport(DDG_PAGE)
    runner = make_autonomy_runner(store, tmp_path, live=False, search=search)
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert outcome["stop_reason"] == "live_disabled"
    assert outcome["searches"] == 0 and outcome["fetches"] == 0
    assert search.calls == []
    checkpoint = store.latest("exec:fragment-intent:test")
    assert research_cognitive_stage(checkpoint) == "capability_offline"
    # 0 次网络请求绝不得判耗尽。
    assert outcome["plan_exhausted"] is False
    progress = gr.research_progress_projection(checkpoint)
    assert progress["cognitive"] == "capability_offline"
    assert progress["plan_exhausted"] is False


# -- G2：技术 passed + 认知未完成 → 认知轴绝不显示已核验 ----------------------


def test_g2_technical_passed_never_implies_cognitive_done(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    runner = make_autonomy_runner(store, tmp_path, live=False)
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    checkpoint = store.latest("exec:fragment-intent:test")
    # 技术状态与认知状态正交：checkpoint 技术状态可以是 running/passed，
    # 认知轴只由研究事实推导——本例能力关闭。
    assert checkpoint.status == "running"
    assert research_cognitive_stage(checkpoint) == "capability_offline"
    assert research_cognitive_stage(checkpoint) != "synthesized"


# -- G3：公开 collection 在模型/Keychain 关闭时照常工作 ------------------------


def test_g3_public_collection_works_with_model_keychain_off(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    runner = make_autonomy_runner(
        store, tmp_path, live=False, collection_enabled=True, synthesis_enabled=False
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert active_records(outcome), "collection 独立开启时必须能取到证据"
    assert outcome["searches"] >= 1 and outcome["fetches"] >= 1
    synthesis = runner.run_synthesis("exec:fragment-intent:test")
    assert synthesis["status"] == "synthesis_disabled"
    assert synthesis["model_calls"] == 0
    checkpoint = store.latest("exec:fragment-intent:test")
    # 有证据但模型未开启 → 直接 evidence_ready（任务 B；rev2 P1-1），
    # 绝不退化成「无证据」，也不停留在 collecting。
    assert research_cognitive_stage(checkpoint) == "evidence_ready"
    progress = gr.research_progress_projection(checkpoint)
    assert progress["collected_sources"] >= 1


def test_g3b_decoupled_synthesis_disabled_keeps_evidence_ready(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    runner = make_autonomy_runner(
        store, tmp_path, live=False, collection_enabled=True, synthesis_enabled=False
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    synthesis = runner.run_synthesis("exec:fragment-intent:test")
    # synthesis_disabled 事实入 checkpoint 后（适配器侧信道语义），认知为
    # evidence_ready；这里直接验证派生规则。
    assert synthesis["status"] == "synthesis_disabled"
    assert active_records(outcome)


# -- G4：有证据但模型未授权 → awaiting_model_authorization，不丢证据 ----------


def test_g4_evidence_without_receipt_awaits_authorization(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    # synthesis 开启但无 price_reader（Receipt 无法签发）。
    runner = make_autonomy_runner(
        store, tmp_path, live=True, synthesis_enabled=True, collection_enabled=True
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert active_records(outcome)
    synthesis = runner.run_synthesis("exec:fragment-intent:test")
    assert synthesis["status"] == "awaiting_authorization"
    assert synthesis["model_calls"] == 0
    # 证据零丢失：collection 记录仍在。
    again = runner.collection_outcome("exec:fragment-intent:test")
    assert active_records(again)


# -- G5：只有冻结检索真实耗尽才 search_exhausted；0 请求永不耗尽 ---------------


def test_g5_only_real_bounded_exhaustion_is_search_exhausted(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    search = FakeSearchTransport(EMPTY_DDG)
    fetch = FakeFetchTransport()
    runner = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True, search=search, fetch=fetch
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert outcome["stop_reason"] == "not_found"
    assert outcome["plan_exhausted"] is True
    assert outcome["searches"] > 0
    checkpoint = store.latest("exec:fragment-intent:test")
    assert research_cognitive_stage(checkpoint) == "search_exhausted"
    # 适配器语义：耗尽后持久 watching（due 条件，不是用户待办）。
    runner.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    checkpoint = store.latest("exec:fragment-intent:test")
    assert research_cognitive_stage(checkpoint) == "watching"
    watch = runner.research_watch("exec:fragment-intent:test")
    assert watch["status"] == "watching" and watch["reason"] == "plan_exhausted"


# -- G6：缺口自动第二轮查询，无人工点击 ---------------------------------------


def test_g6_gap_triggers_bounded_second_round_without_human(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    # 首轮 EMPTY（无候选）；第二轮改述查询返回候选（每轮各 1 个查询）。
    search = FakeSearchTransport([EMPTY_DDG, DDG_PAGE])
    runner = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True, search=search
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    rounds = outcome["rounds"]
    assert len(rounds) == 2, f"缺口必须自动触发第二轮：{rounds}"
    assert rounds[0]["kind"] == "initial"
    assert rounds[1]["kind"] == "replan"
    assert rounds[0]["queries"] != rounds[1]["queries"], "第二轮必须是改述查询"
    assert active_records(outcome), "第二轮必须真实取得证据"
    # 覆盖度随之更新。
    assert outcome["candidate_coverage"]["gaps"] == []


def test_replan_and_counterexample_queries_are_deterministic() -> None:
    replan = build_replan_queries(GOAL, ["release"], round_no=2)
    assert replan and all(step["query"] != "" for step in replan)
    with pytest.raises(Exception):
        build_replan_queries(GOAL, ["release"], round_no=3)
    counter = build_counterexample_queries(GOAL, ["release", "risk"])
    assert len(counter) == 2
    assert all("问题" in step["query"] for step in counter)


# -- G7（rev3 重写）：到期观察开启新的有界 observation cycle ------------------


def test_g7_due_watch_opens_new_bounded_observation_cycle(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    # 首轮 2 次搜索皆空（耗尽）；第二周期 transport 返回后来出现的来源。
    search = FakeSearchTransport([EMPTY_DDG, EMPTY_DDG, DDG_PAGE])
    fetch = FakeFetchTransport()
    runner = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True,
        search=search, fetch=fetch, clock=clock,
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert outcome["plan_exhausted"] is True
    runner.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    # due 前：绝不恢复，0 新请求。
    assert runner.resume_due_watch("exec:fragment-intent:test", GOAL) is None
    assert len(search.calls) == 2 and len(fetch.calls) == 0
    # due 后：新 cycle 恰好一次新的有界搜索——不是重放旧 journal。
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    resumed = runner.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert resumed is not None
    assert len(search.calls) == 3, "新 cycle 必须真实重新搜索，绝不只重放旧日志"
    assert len(fetch.calls) == 1, "后来出现的来源必须真实抓取"
    assert active_records(resumed)
    # 发现有效来源：持久化新 collection、清除 watch、进入后续链。
    assert runner.research_watch("exec:fragment-intent:test") is None
    assert runner.collection_outcome("exec:fragment-intent:test") is not None
    # 再次评估：无 watch，0 新请求。
    assert runner.resume_due_watch("exec:fragment-intent:test", GOAL) is None
    assert len(search.calls) == 3 and len(fetch.calls) == 1


def test_g7b_cycle_without_evidence_increments_attempts_exactly_once(
    tmp_path: Path,
) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    search = FakeSearchTransport(EMPTY_DDG)
    runner = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True, search=search, clock=clock
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    runner.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    first = runner.research_watch("exec:fragment-intent:test")
    assert first["attempts"] == 1
    # 第一个到期 cycle：仍无来源 → attempts 恰好 +1，since 保留，
    # last_checked_at 与 due_after 更新，active_cycle 清除。
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    resumed = runner.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert resumed is not None
    assert not active_records(resumed)
    watch = runner.research_watch("exec:fragment-intent:test")
    assert watch["attempts"] == 2, "attempts 恰好 +1"
    assert watch["since"] == first["since"], "首次 since 必须保留"
    assert watch["last_checked_at"] != first["last_checked_at"]
    assert watch["due_after"] != first["due_after"]
    assert "active_cycle" not in watch
    calls_after_cycle = len(search.calls)
    # 重入（due 未到）：0 新请求，attempts 不再加。
    assert runner.resume_due_watch("exec:fragment-intent:test", GOAL) is None
    assert len(search.calls) == calls_after_cycle
    assert runner.research_watch("exec:fragment-intent:test")["attempts"] == 2
    # 第二个到期 cycle：再次恰好 +1。
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    assert runner.resume_due_watch("exec:fragment-intent:test", GOAL) is not None
    assert runner.research_watch("exec:fragment-intent:test")["attempts"] == 3


class _ExplodingOnceFetchTransport:
    """第一次抓取即抛非 ResearchFetchError（模拟提交前后进程崩溃）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request):
        self.calls.append(str(request["locator"]))
        raise RuntimeError("simulated crash before cycle completes")


def test_g7c_crash_resume_same_cycle_never_repeats_completed_actions(
    tmp_path: Path,
) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    search = FakeSearchTransport([EMPTY_DDG, EMPTY_DDG, DDG_PAGE])
    crash_fetch = _ExplodingOnceFetchTransport()
    runner_a = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True,
        search=search, fetch=crash_fetch, clock=clock,
    )
    outcome = runner_a.collect("exec:fragment-intent:test", GOAL)
    runner_a.persist_collection("exec:fragment-intent:test", outcome)
    runner_a.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    # cycle 2：搜索完成（completed + journal）后抓取前「崩溃」。
    with pytest.raises(RuntimeError, match="simulated crash"):
        runner_a.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert len(search.calls) == 3 and len(crash_fetch.calls) == 1
    # 崩溃现场：active_cycle 持久化；未超 run deadline 阈值时任何并发/
    # 重入 evaluator 绝不重复发送。
    watch = runner_a.research_watch("exec:fragment-intent:test")
    assert watch["active_cycle"]["cycle"] == 2
    runner_b = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True,
        search=ExplodingSearchTransport(), fetch=FakeFetchTransport(), clock=clock,
    )
    assert runner_b.resume_due_watch("exec:fragment-intent:test", GOAL) is None
    # 超过 run deadline（领取者必已死亡）后接管恢复同一 cycle：
    # 已完成的搜索经 journal 重放零重发（ExplodingSearchTransport 未炸即
    # 证明），只补发未完成的抓取，随后清 watch。
    clock.advance(gr.RUN_DEADLINE_SECONDS + 1)
    resumed = runner_b.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert resumed is not None and active_records(resumed)
    assert len(search.calls) == 3, "已完成搜索绝不重发"
    assert runner_b.research_watch("exec:fragment-intent:test") is None


def test_g7d_concurrent_due_evaluators_single_flight(tmp_path: Path) -> None:
    """并发单飞（rev9：唯一键语句级原子领取，20 轮确定性重复，不依赖
    调度偶然）：每轮两个 evaluator 线程 barrier 同时 resume，恰好一个
    cycle 产生网络副作用，loser 返回 None、零网络、attempts 恰好 +1。"""
    for trial in range(20):
        store = SQLiteCheckpointStore(tmp_path / f"g{ trial }.sqlite3")
        make_run(store)
        clock = FrozenClock()
        runner_seed = make_autonomy_runner(
            store, tmp_path, live=True, collection_enabled=True,
            search=FakeSearchTransport(EMPTY_DDG), clock=clock,
        )
        outcome = runner_seed.collect("exec:fragment-intent:test", GOAL)
        runner_seed.persist_collection("exec:fragment-intent:test", outcome)
        runner_seed.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
        clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
        search_a = FakeSearchTransport(EMPTY_DDG)
        search_b = FakeSearchTransport(EMPTY_DDG)
        runner_a = make_autonomy_runner(
            store, tmp_path, live=True, collection_enabled=True, search=search_a, clock=clock
        )
        runner_b = make_autonomy_runner(
            store, tmp_path, live=True, collection_enabled=True, search=search_b, clock=clock
        )
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def work(name: str, runner) -> None:
            barrier.wait(timeout=10)
            results[name] = runner.resume_due_watch("exec:fragment-intent:test", GOAL)

        threads = [
            threading.Thread(target=work, args=("a", runner_a)),
            threading.Thread(target=work, args=("b", runner_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        outcomes = [value for value in results.values() if value is not None]
        assert len(outcomes) == 1, f"trial {trial}: 恰好一个 evaluator 领取 cycle：{results}"
        total_calls = len(search_a.calls) + len(search_b.calls)
        assert total_calls <= 4, f"trial {trial}: 只有赢家的有界 cycle 搜索真实发生"
        assert min(len(search_a.calls), len(search_b.calls)) == 0, (
            f"trial {trial}: loser 必须零网络"
        )
        watch = runner_a.research_watch("exec:fragment-intent:test")
        assert watch["attempts"] == 2, f"trial {trial}: attempts 恰好 +1"
        assert "active_cycle" not in watch


def test_rev9_concurrent_cycle_claim_exactly_one_winner(tmp_path: Path) -> None:
    """rev9：直接并发 _claim_watch_cycle（10 轮）——唯一键领取恰好一个
    True，loser 零副作用；同 cycle 重入领取方重复调用也返回 False。"""
    for trial in range(10):
        store = SQLiteCheckpointStore(tmp_path / f"c{trial}.sqlite3")
        make_run(store)
        clock = FrozenClock()
        runner_seed = make_autonomy_runner(
            store, tmp_path, live=True, collection_enabled=True,
            search=FakeSearchTransport(EMPTY_DDG), clock=clock,
        )
        outcome = runner_seed.collect("exec:fragment-intent:test", GOAL)
        runner_seed.persist_collection("exec:fragment-intent:test", outcome)
        runner_seed.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
        runner_a = make_autonomy_runner(store, tmp_path, clock=clock)
        runner_b = make_autonomy_runner(store, tmp_path, clock=clock)
        barrier = threading.Barrier(2)
        claims: dict[str, bool] = {}

        def work(name: str, runner) -> None:
            barrier.wait(timeout=10)
            claims[name] = runner._claim_watch_cycle("exec:fragment-intent:test", 2, GOAL)

        threads = [
            threading.Thread(target=work, args=("a", runner_a)),
            threading.Thread(target=work, args=("b", runner_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert sorted(claims.values()) == [False, True], (
            f"trial {trial}: 恰好一个 winner：{claims}"
        )
        # winner 未超 deadline 前，任何重入（含 winner 自己）都不得再次领取。
        assert runner_a._claim_watch_cycle("exec:fragment-intent:test", 2, GOAL) is False
        assert runner_b._claim_watch_cycle("exec:fragment-intent:test", 2, GOAL) is False


def test_rev9_concurrent_takeover_after_deadline_single_winner(tmp_path: Path) -> None:
    """rev9：winner 崩溃超 run deadline 后，两个并发接管者经单条
    UPDATE ... WHERE 原子竞争，恰好一个接管成功，另一个返回 False。"""
    for trial in range(10):
        store = SQLiteCheckpointStore(tmp_path / f"t{trial}.sqlite3")
        make_run(store)
        clock = FrozenClock()
        runner_seed = make_autonomy_runner(
            store, tmp_path, live=True, collection_enabled=True,
            search=FakeSearchTransport(EMPTY_DDG), clock=clock,
        )
        outcome = runner_seed.collect("exec:fragment-intent:test", GOAL)
        runner_seed.persist_collection("exec:fragment-intent:test", outcome)
        runner_seed.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
        # 原始 winner 领取后「崩溃」（claim 记录已持久化，active_cycle 在）。
        assert runner_seed._claim_watch_cycle("exec:fragment-intent:test", 2, GOAL) is True
        clock.advance(gr.RUN_DEADLINE_SECONDS + 1)
        runner_a = make_autonomy_runner(store, tmp_path, clock=clock)
        runner_b = make_autonomy_runner(store, tmp_path, clock=clock)
        barrier = threading.Barrier(2)
        claims: dict[str, bool] = {}

        def work(name: str, runner) -> None:
            barrier.wait(timeout=10)
            claims[name] = runner._claim_watch_cycle("exec:fragment-intent:test", 2, GOAL)

        threads = [
            threading.Thread(target=work, args=("a", runner_a)),
            threading.Thread(target=work, args=("b", runner_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert sorted(claims.values()) == [False, True], (
            f"trial {trial}: 并发接管恰好一个 winner：{claims}"
        )


def test_g7e_collection_disabled_never_claims_network_or_mutates_watch(
    tmp_path: Path,
) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    runner_seed = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True,
        search=FakeSearchTransport(EMPTY_DDG), clock=clock,
    )
    outcome = runner_seed.collect("exec:fragment-intent:test", GOAL)
    runner_seed.persist_collection("exec:fragment-intent:test", outcome)
    runner_seed.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    search = FakeSearchTransport(DDG_PAGE)
    runner_off = make_autonomy_runner(
        store, tmp_path, live=False, collection_enabled=False, search=search, clock=clock
    )
    watch_before = runner_off.research_watch("exec:fragment-intent:test")
    assert runner_off.resume_due_watch("exec:fragment-intent:test", GOAL) is None
    assert search.calls == [], "能力关闭绝不发网络"
    assert runner_off.research_watch("exec:fragment-intent:test") == watch_before, (
        "能力关闭绝不改变 watch"
    )


# -- rev4 P0：观察 cycle 的证据累积（不丢、不任意停止） ------------------------


def _first_release_only_run(store: SQLiteCheckpointStore, tmp_path: Path, clock):
    """首轮：release 官方证据覆盖、risk 缺口 → 耗尽进入 watching。"""
    search = FakeSearchTransport([DDG_PAGE, EMPTY_DDG, EMPTY_DDG])
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        search=search,
        fetch=FakeFetchTransport(body=RELEASE_BODY),
        clock=clock,
    )
    outcome = runner.collect(
        "exec:fragment-intent:test", GOAL, claim_types=["release", "risk"]
    )
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert outcome["candidate_coverage"]["gaps"] == ["risk"]
    assert outcome["plan_exhausted"] is True
    runner.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    release = [
        record
        for record in outcome["records"]
        if record.get("marker") == "newly_collected"
    ][0]
    return runner, search, release


def test_rev4_cycle_keeps_release_when_risk_still_missing(tmp_path: Path) -> None:
    """首轮只有 release、缺 risk；cycle 仍只找到 release（risk 搜索为空）：
    旧 release 不丢、risk gap 仍在、watch 不清、attempts 恰好 +1。"""
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    runner, search, release = _first_release_only_run(store, tmp_path, clock)
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    # cycle transport 仍然 EMPTY（risk 证据后来也没出现）。
    resumed = runner.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert resumed is not None
    # 旧 release 经 inherited 重验证保留在新 collection（不覆盖丢失）。
    active = active_records(resumed)
    assert [record["evidence_digest"] for record in active] == [
        release["evidence_digest"]
    ]
    assert active[0]["marker"] == "inherited"
    assert resumed["candidate_coverage"]["gaps"] == ["risk"], "risk 缺口必须仍在"
    # watch 保留、attempts 恰好 +1（绝不「任意一条来源即停止」）。
    watch = runner.research_watch("exec:fragment-intent:test")
    assert watch["status"] == "watching" and watch["attempts"] == 2
    stored = runner.collection_outcome("exec:fragment-intent:test")
    assert release["evidence_digest"] in [
        record["evidence_digest"] for record in active_records(stored)
    ]


def test_rev4_cycle_adds_risk_and_clears_watch_with_full_coverage(
    tmp_path: Path,
) -> None:
    """首轮只有 release；cycle 只新增 risk：最终 collection 同时保留
    release+risk、coverage 无 gap、watch 清除。"""
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    runner, search, release = _first_release_only_run(store, tmp_path, clock)
    # cycle 阶段：independent 搜索返回后来出现的 issues 页。
    search.pages = [DDG_PAGE, EMPTY_DDG, EMPTY_DDG, COUNTER_DDG]
    runner.fetch_transport = _PerUrlFetchTransport(
        {
            "https://example.com/release": RELEASE_BODY,
            "https://example.com/issues": ISSUE_BODY,
        }
    )
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    resumed = runner.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert resumed is not None
    digests = {record["source_target"] for record in active_records(resumed)}
    assert digests == {"official", "independent"}, "release+risk 必须同时保留"
    assert resumed["candidate_coverage"]["gaps"] == []
    # coverage 无 gap 才退出来源补全观察。
    assert runner.research_watch("exec:fragment-intent:test") is None
    stored = runner.collection_outcome("exec:fragment-intent:test")
    assert release["evidence_digest"] in [
        record["evidence_digest"] for record in active_records(stored)
    ]


def test_rev4_expired_history_becomes_stale_and_watch_survives(tmp_path: Path) -> None:
    """首轮历史 record 到期：cycle 中如实降为 stale；未重新取得该类型则
    watch 保留，绝不借旧证据清 watch。"""
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    runner, search, release = _first_release_only_run(store, tmp_path, clock)
    # 到期（超过 EVIDENCE_TTL_SECONDS）后恢复：release 必须降 stale，
    # release/risk 两个 gap 重新打开；cycle 搜索仍为空 → watch 保留。
    search.pages = [DDG_PAGE] + [EMPTY_DDG] * 6
    clock.advance(7 * 24 * 3600 + 1)
    resumed = runner.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert resumed is not None
    markers = {
        record["evidence_digest"]: record["marker"] for record in resumed["records"]
    }
    assert markers[release["evidence_digest"]] == "stale", "过期证据如实降 stale"
    assert active_records(resumed) == [], "stale 不算有效证据"
    assert resumed["candidate_coverage"]["gaps"] == ["release", "risk"]
    watch = runner.research_watch("exec:fragment-intent:test")
    assert watch["status"] == "watching" and watch["attempts"] == 2


def test_rev4_crash_resume_preserves_accumulated_set_with_zero_resend(
    tmp_path: Path,
) -> None:
    """cycle 崩溃恢复：仍维持 release+risk 累积集合，且已完成动作零重发。"""
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    clock = FrozenClock()
    _runner, search, release = _first_release_only_run(store, tmp_path, clock)
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    # cycle 2：independent 搜索完成（journal）后抓取前「崩溃」。
    crash_fetch = _ExplodingOnceFetchTransport()
    runner_a = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True,
        search=FakeSearchTransport(COUNTER_DDG), fetch=crash_fetch, clock=clock,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        runner_a.resume_due_watch("exec:fragment-intent:test", GOAL)
    # 超 run deadline 后接管恢复：搜索 journal 重放零重发
    # （ExplodingSearchTransport 未炸即证），fetch 补发成功。
    clock.advance(gr.RUN_DEADLINE_SECONDS + 1)
    fetch_b = _PerUrlFetchTransport({"https://example.com/issues": ISSUE_BODY})
    runner_b = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True,
        search=ExplodingSearchTransport(), fetch=fetch_b, clock=clock,
    )
    resumed = runner_b.resume_due_watch("exec:fragment-intent:test", GOAL)
    assert resumed is not None
    digests = {record["source_target"] for record in active_records(resumed)}
    assert digests == {"official", "independent"}, "崩溃恢复后累积集合完整"
    assert len(fetch_b.calls) == 1
    assert runner_b.research_watch("exec:fragment-intent:test") is None


# -- rev3 自主触发边界：最低频同进程 due coordinator ----------------------------


def _service_with_runner(store: SQLiteCheckpointStore, runner) -> Any:
    from fragment_loop.intent_service import FragmentIntentService

    return FragmentIntentService(
        store,
        lambda fragment_id, input_digest: {},
        lambda alignment: alignment,
        verify_adapter=gr.GovernedResearchVerifyAdapter(runner),
    )


def test_rev3_coordinator_scan_resumes_due_watch_without_ui(tmp_path: Path) -> None:
    """无人打开 UI 也到期推进：coordinator 扫描即触发有界观察 cycle。"""
    from fragment_loop.intent_service import ResearchWatchCoordinator

    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    make_run(store)
    clock = FrozenClock()
    search = FakeSearchTransport(EMPTY_DDG)
    runner = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True, search=search, clock=clock
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    runner.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    service = _service_with_runner(store, runner)
    coordinator = ResearchWatchCoordinator(service)
    # 不经 get_alignment（无任何 UI 读路径），扫描即推进。
    result = coordinator.scan_once()
    assert result == {"scanned": 1, "resumed": 1}
    assert len(search.calls) == 4, "首轮 2 次 + 新 cycle 2 次真实搜索"
    assert runner.research_watch("exec:fragment-intent:test")["attempts"] == 2
    # 重启恢复幂等：第二轮扫描 due 未到，零新请求。
    assert coordinator.scan_once() == {"scanned": 1, "resumed": 0}
    assert len(search.calls) == 4


def test_rev3_coordinator_start_close_is_clean(tmp_path: Path) -> None:
    from fragment_loop.intent_service import ResearchWatchCoordinator

    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    make_run(store)
    runner = make_autonomy_runner(store, tmp_path, live=False, collection_enabled=False)
    service = _service_with_runner(store, runner)
    coordinator = ResearchWatchCoordinator(service, interval_seconds=3600)
    assert coordinator.start() is True
    assert coordinator.start() is False, "重复 start 幂等"
    assert coordinator.close() is True
    assert coordinator._thread is None, "关闭必须干净 join"


def test_rev4_coordinator_close_never_fakes_shutdown_while_scan_blocked(
    tmp_path: Path,
) -> None:
    """scan 正在阻塞时 close 不伪装完成：不清空线程引用、不谎报关闭。"""
    from fragment_loop.intent_service import ResearchWatchCoordinator

    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    make_run(store)
    runner = make_autonomy_runner(store, tmp_path, live=False, collection_enabled=False)
    service = _service_with_runner(store, runner)
    coordinator = ResearchWatchCoordinator(service, interval_seconds=3600)
    block = threading.Event()
    entered = threading.Event()

    def blocked_scan() -> dict[str, int]:
        entered.set()
        block.wait(timeout=30)
        return {"scanned": 0, "resumed": 0}

    service.evaluate_due_watches = blocked_scan
    assert coordinator.start() is True
    assert entered.wait(timeout=5), "scan 必须已开始"
    # worker 仍存活：close 返回 False 且保留线程引用（绝不谎报）。
    assert coordinator.close(timeout_seconds=0.2) is False
    assert coordinator._thread is not None and coordinator._thread.is_alive()
    # 放行后真正停止：返回 True 且引用清空。
    block.set()
    assert coordinator.close(timeout_seconds=5) is True
    assert coordinator._thread is None


def test_rev3_coordinator_capability_off_zero_side_effects(tmp_path: Path) -> None:
    from fragment_loop.intent_service import ResearchWatchCoordinator

    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    make_run(store)
    clock = FrozenClock()
    runner_seed = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True,
        search=FakeSearchTransport(EMPTY_DDG), clock=clock,
    )
    outcome = runner_seed.collect("exec:fragment-intent:test", GOAL)
    runner_seed.persist_collection("exec:fragment-intent:test", outcome)
    runner_seed.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    search = FakeSearchTransport(DDG_PAGE)
    runner_off = make_autonomy_runner(
        store, tmp_path, live=False, collection_enabled=False, search=search, clock=clock
    )
    service = _service_with_runner(store, runner_off)
    coordinator = ResearchWatchCoordinator(service)
    watch_before = runner_off.research_watch("exec:fragment-intent:test")
    assert coordinator.scan_once() == {"scanned": 1, "resumed": 0}
    assert search.calls == []
    assert runner_off.research_watch("exec:fragment-intent:test") == watch_before


# -- G8：冲突触发反证搜索 -----------------------------------------------------


def test_g8_conflict_triggers_counterexample_round(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    body = (
        b"<html><body><h1>Example</h1><p>example released supports available "
        b"compatible recommended unsupported failing incompatible "
        b"deprecated avoid</p></body></html>"
    )
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        fetch=FakeFetchTransport(body=body),
    )
    outcome = runner.collect("exec:fragment-intent:test", GOAL)
    runner.persist_collection("exec:fragment-intent:test", outcome)
    conflicts = detect_conflict_types(active_records(outcome))
    assert conflicts, "正负极性同时出现必须判冲突"
    rounds = outcome["rounds"]
    assert any(item["kind"] == "counterexample" for item in rounds), rounds
    counter_round = [item for item in rounds if item["kind"] == "counterexample"][0]
    assert any("问题" in query for query in counter_round["queries"])


def _polarity_record(text: str, claim_type: str = "release") -> dict:
    return {
        "marker": "newly_collected",
        "title": text,
        "excerpt_windows": [],
        "claim_types": [claim_type],
    }


def test_rev5_negation_phrases_never_create_false_conflicts() -> None:
    """rev5 P1：否定短语不得同时命中正向——单条纯负面来源零冲突。"""
    for text in ("not released", "incompatible", "unavailable"):
        assert detect_conflict_types([_polarity_record(text)]) == [], text
    # 中文否定同样遮蔽。
    assert detect_conflict_types([_polarity_record("该产品不支持该协议")]) == []
    # 负向命中本身仍保留（用于诚实展示/反证触发）。
    assert detect_conflict_types(
        [
            _polarity_record("officially released"),
            _polarity_record("not released"),
        ]
    ) == ["release"], "真正独立正负来源仍必须判冲突"


def test_rev5_independent_positive_and_negative_sources_still_conflict() -> None:
    """真正独立的正负来源（同 claim type）仍触发冲突与反证查询。"""
    records = [
        _polarity_record("example released and compatible"),
        _polarity_record("example incompatible and unavailable"),
    ]
    assert detect_conflict_types(records) == ["release"]


# -- rev2 P1-2：部分覆盖仍有缺口 → 缺口维度判定耗尽，绝不永久悬空 ---------------


def test_rev2_partial_coverage_with_gaps_is_plan_exhausted(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    # R1：official 查询命中 release 页（release 覆盖），independent 查询空；
    # R2：replan 针对 risk 缺口仍空 → 轮次尽、缺口仍在、但有部分 records。
    search = FakeSearchTransport([DDG_PAGE, EMPTY_DDG, EMPTY_DDG])
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        search=search,
        fetch=FakeFetchTransport(body=RELEASE_BODY),
    )
    outcome = runner.collect(
        "exec:fragment-intent:test", GOAL, claim_types=["release", "risk"]
    )
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert active_records(outcome), "部分覆盖：release 证据必须真实取得"
    assert outcome["candidate_coverage"]["gaps"] == ["risk"], outcome["candidate_coverage"]
    assert outcome["stop_reason"] == "completed"
    # 关键断言：缺口维度判定冻结计划耗尽——不得形成
    # completed + plan_exhausted=false + cognitive=collecting 的永久悬空。
    assert outcome["plan_exhausted"] is True
    checkpoint = store.latest("exec:fragment-intent:test")
    assert research_cognitive_stage(checkpoint) == "search_exhausted"
    # 耗尽默认转 watching（持久观察），不是用户待办。
    runner.persist_watch("exec:fragment-intent:test", reason="plan_exhausted")
    checkpoint = store.latest("exec:fragment-intent:test")
    assert research_cognitive_stage(checkpoint) == "watching"


# -- rev2 P1-3：冲突第二轮才出现 → 仍自动一次有界反证搜索 ---------------------


class _PerUrlFetchTransport:
    """按 locator 返回不同页面体的 fake fetch（同 FakeFetchTransport 形状）。"""

    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies
        self.calls: list[str] = []

    def __call__(self, request):
        locator = str(request["locator"])
        self.calls.append(locator)
        return {
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "body_bytes": self.bodies[locator],
            "final_locator": locator,
            "peer_ip": "93.184.216.34",
            "resolved_ips": ["93.184.216.34"],
        }


def test_rev2_conflict_in_second_round_still_triggers_counterexample(
    tmp_path: Path,
) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    mixed_body = (
        b"<html><body><h1>Example Issues</h1><p>example released supports available "
        b"compatible recommended unsupported failing incompatible "
        b"deprecated avoid risk security</p></body></html>"
    )
    # R1：official → release 页（正极性，无冲突），independent → 空，
    #     gaps=["risk"] → replan；
    # R2：replan → issues 页（正负极性混合）→ risk 冲突才出现；
    # R3：即使已到 max_rounds，仍必须自动执行一次有界 counterexample。
    search = FakeSearchTransport([DDG_PAGE, EMPTY_DDG, COUNTER_DDG, EMPTY_DDG])
    fetch = _PerUrlFetchTransport(
        {
            "https://example.com/release": RELEASE_BODY,
            "https://example.com/issues": mixed_body,
        }
    )
    runner = make_autonomy_runner(
        store, tmp_path, live=True, collection_enabled=True, search=search, fetch=fetch
    )
    outcome = runner.collect(
        "exec:fragment-intent:test", GOAL, claim_types=["release", "risk"]
    )
    runner.persist_collection("exec:fragment-intent:test", outcome)
    assert outcome["conflicts"] == ["risk"], outcome["conflicts"]
    rounds = outcome["rounds"]
    assert [item["kind"] for item in rounds] == ["initial", "replan", "counterexample"], rounds
    counter_round = rounds[2]
    assert any("问题" in query for query in counter_round["queries"])
    # 总预算不破：action 上限、每轮预算检查与首轮完全一致。
    assert outcome["searches"] + outcome["fetches"] <= gr.MAX_COLLECTION_ACTIONS
    assert outcome["stop_reason"] == "completed"


# -- G9：普通公开碎片到候选前人工操作 0 次（system_policy 自动路线） ----------


def _proposed_alignment(store: SQLiteCheckpointStore, tmp_path: Path, *, nigo_loop: bool = True):
    from common.checkpoint import LoopCheckpoint
    from fragment_loop.intent_service import FragmentIntentService

    checkpoint = LoopCheckpoint(
        loop_id="fragment-intent-alignment-v1",
        run_id="align:auto-1",
        fragment_id="frag-auto",
        current_node="alignment",
        status="suggested",
        fragment_title="示例产品公开发布信息",
        eval_results={
            "alignment": {
                "alignment_digest": "a" * 64,
                "input_digest": "b" * 64,
                # TASK D rev2：可验证的 nigo-loop 来源事实（bridge 校验
                # frontmatter 标记后显式传递）；缺失则默认不自动。
                **({"nigo_loop": True} if nigo_loop else {}),
                "case_id": "case-1",
                "episode_id": "ep-1",
                "revision": 1,
                "literal_summary": "一个公开产品信息碎片",
                "suggested_intents": ["verify"],
                "dynamic_intents": [],
                "recommended_route": "verify",
                "execution_scope": {
                    "capabilities": ["受治理研究"],
                    "external_scope": ["官方与公开社区来源"],
                    "model_call_cap": 1,
                    "cost_cap_cny": 2.0,
                    "side_effect": "none",
                },
            }
        },
    )
    assert store.compare_and_append(checkpoint, expected_sequence=0) is not None
    runner = GovernedResearchRunner(
        store,
        live_enabled=True,
        collection_enabled=True,
        synthesis_enabled=True,
        search_transport=FakeSearchTransport(DDG_PAGE),
        fetch_transport=FakeFetchTransport(),
        clock=FrozenClock(),
        monotonic=FrozenMonotonic(),
        credential_reader=ExplodingCredentialReader(),
    )
    service = FragmentIntentService(
        store,
        lambda fragment_id, input_digest: {},
        lambda alignment: alignment,
        verify_adapter=gr.GovernedResearchVerifyAdapter(runner),
    )
    return service


def test_g9_public_fragment_auto_advances_with_zero_human_ops(tmp_path: Path) -> None:
    service = _proposed_alignment(SQLiteCheckpointStore(tmp_path / "state.sqlite3"), tmp_path)
    projection = service.get_alignment("align:auto-1")
    # get 读路径的 due 评估：eligible 公开碎片自动登记并启动公开 collection。
    assert projection["status"] == "passed"
    assert projection["decision_source"] == gr.AUTO_DECISION_SOURCE
    assert projection["route"] == "verify"
    execution = projection.get("execution")
    assert isinstance(execution, dict)
    progress = execution.get("research_progress")
    assert progress["collected_sources"] >= 1, "系统必须自动完成公开收集"
    # system_policy 路线：模型永远停在人类闸门——零 Receipt、零发送、零账本。
    assert progress["cognitive"] == "awaiting_model_authorization"


def test_g9b_auto_advance_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    service = _proposed_alignment(store, tmp_path)
    assert service.maybe_auto_advance("align:auto-1") is True
    assert service.maybe_auto_advance("align:auto-1") is False
    # 未触碰其它 alignment run。
    others = [
        rid for rid in store.run_ids_with_prefix("fragment-intent-alignment-v1")
        if rid != "align:auto-1"
    ]
    assert others == []


# -- G10：私人出域/模型/预算/外部动作/资产仍被人类闸门阻断 ---------------------


def test_g10_human_gates_hold_for_private_model_budget_external(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    service = _proposed_alignment(store, tmp_path)
    base = {
        "capabilities": ["受治理研究"],
        "external_scope": ["官方与公开社区来源"],
        "model_call_cap": 1,
        "cost_cap_cny": 2.0,
        "side_effect": "none",
    }
    # 私有出域标记 → 不自动。
    assert not service._auto_route_eligible({
        "recommended_route": "verify",
        "execution_scope": {**base, "capabilities": ["读取私人笔记"]},
    })
    # 写入/发布/部署/安装 → 不自动。
    for bad in ("写入资产", "发布知识资产", "部署服务", "安装依赖"):
        assert not service._auto_route_eligible(
            {
                "recommended_route": "verify",
                "execution_scope": {**base, "capabilities": [bad]},
            }
        )
    # 凭据/密钥 → 不自动。
    assert not service._auto_route_eligible({
        "recommended_route": "verify",
        "execution_scope": {**base, "external_scope": ["Keychain 凭据"]},
    })
    # 英文安全标记大小写变体同样不得绕过（rev5）。
    for variant in ("PRIVATE notes", "KeyChain access", "SECRET token"):
        assert not service._auto_route_eligible({
            "recommended_route": "verify",
            "execution_scope": {**base, "capabilities": [variant]},
        }), variant
    # 非只读副作用 → 不自动；非 verify 推荐 → 不自动。
    assert not service._auto_route_eligible({
        "recommended_route": "verify",
        "execution_scope": {**base, "side_effect": "write"},
    })
    assert not service._auto_route_eligible(
        {"recommended_route": "direct", "execution_scope": base}
    )
    # system_policy 执行：adapter 绝不自动签发 Receipt（auto_synthesis_allowed=False）。
    assert service.maybe_auto_advance("align:auto-1") is True
    exec_runs = store.run_ids_with_prefix("fragment-intent-execution-v1")
    assert exec_runs, "自动路线必须已登记执行 Run"
    checkpoint = store.latest(exec_runs[0])
    binding = checkpoint.eval_results.get("execution_binding")
    assert binding["auto_synthesis_allowed"] is False


def test_rev2_non_nigo_loop_alignment_never_auto_advances(tmp_path: Path) -> None:
    """rev2 P1-4：同形但缺 nigo_loop 事实的 alignment 默认不自动，零副作用。"""
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    service = _proposed_alignment(store, tmp_path, nigo_loop=False)
    sequence_before = store.latest_sequence("align:auto-1")
    # _auto_route_eligible 硬条件：缺失 nigo_loop 事实一律不 eligible，
    # 即使推荐 verify、scope 完全合规（绝不凭标题或文案猜测）。
    alignment = store.latest("align:auto-1").eval_results["alignment"]
    assert "nigo_loop" not in alignment
    assert not service._auto_route_eligible(alignment)
    # maybe_auto_advance 与 get 读路径都不得推进。
    assert service.maybe_auto_advance("align:auto-1") is False
    projection = service.get_alignment("align:auto-1")
    assert projection["status"] == "suggested"
    assert projection.get("decision_source") != gr.AUTO_DECISION_SOURCE
    # 零副作用：alignment 序列不变、无 execution run 登记。
    assert store.latest_sequence("align:auto-1") == sequence_before
    assert store.run_ids_with_prefix("fragment-intent-execution-v1") == []


# -- P1-2 修复：coordinator 扫描承载 suggested 自动推进（无需任何 UI 读/人工 decision） --


def test_p12_coordinator_scan_auto_advances_eligible_alignment(tmp_path: Path) -> None:
    """仅靠 coordinator scan_once（零人工 GET、零 decide），eligible
    nigo_loop suggested alignment 自动 passed 并开始公开 collection。"""
    from fragment_loop.intent_service import ResearchWatchCoordinator

    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    service = _proposed_alignment(store, tmp_path)
    coordinator = ResearchWatchCoordinator(service)
    # 触发面只有 coordinator 扫描：不调 get_alignment、不调 decide。
    result = coordinator.scan_once()
    assert result == {"scanned": 1, "resumed": 1}
    checkpoint = store.latest("align:auto-1")
    assert checkpoint.status == "passed"
    alignment = checkpoint.eval_results["alignment"]
    assert alignment["decision_source"] == gr.AUTO_DECISION_SOURCE
    assert alignment["route"] == "verify"
    # 公开 collection 已真实开始并完成；模型永远停在人类闸门。
    exec_runs = store.run_ids_with_prefix("fragment-intent-execution-v1")
    assert len(exec_runs) == 1, "自动路线必须已登记执行 Run"
    execution = store.latest(exec_runs[0])
    collection = execution.eval_results.get("research_collection")
    assert isinstance(collection, dict) and collection.get("searches", 0) >= 1
    assert collection.get("records"), "系统必须自动完成公开收集"
    progress = execution.eval_results.get("research_progress")
    assert progress["cognitive"] == "awaiting_model_authorization"
    # 重扫幂等：已 passed 不再推进，零新 execution run、零新 collection。
    assert coordinator.scan_once() == {"scanned": 2, "resumed": 0}
    assert store.run_ids_with_prefix("fragment-intent-execution-v1") == exec_runs
    assert store.latest("align:auto-1").status == "passed"


def test_p12_coordinator_scan_never_advances_non_nigo_loop(tmp_path: Path) -> None:
    """非 nigo_loop 的 suggested alignment 不被扫描推进，重扫同样零动作。"""
    from fragment_loop.intent_service import ResearchWatchCoordinator

    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    service = _proposed_alignment(store, tmp_path, nigo_loop=False)
    coordinator = ResearchWatchCoordinator(service)
    sequence_before = store.latest_sequence("align:auto-1")
    assert coordinator.scan_once() == {"scanned": 1, "resumed": 0}
    assert store.latest("align:auto-1").status == "suggested"
    assert store.run_ids_with_prefix("fragment-intent-execution-v1") == []
    # 重扫幂等：alignment 序列不变、零 execution run。
    assert coordinator.scan_once() == {"scanned": 1, "resumed": 0}
    assert store.latest_sequence("align:auto-1") == sequence_before
    assert store.latest("align:auto-1").status == "suggested"


# -- G12：旧 run 兼容投影（legacy 无新字段也能安全推导） -----------------------


def test_g12_legacy_collection_projects_safely(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    # 手工构造 legacy 形状的 research_collection（无 rounds/coverage/conflicts/
    # plan_exhausted 字段），投影必须不炸且推导正确。
    legacy = {
        "goal": GOAL,
        "claim_types": ["release"],
        "stop_reason": "live_disabled",
        "records": [],
        "searches": 0,
        "fetches": 0,
        "notes": [],
    }
    from dataclasses import replace

    checkpoint = store.latest("exec:fragment-intent:test")
    edited = replace(
        checkpoint,
        eval_results={**checkpoint.eval_results, "research_collection": legacy},
    )
    assert store.compare_and_append(edited, expected_sequence=1) is not None
    latest = store.latest("exec:fragment-intent:test")
    progress = gr.research_progress_projection(latest)
    assert progress["cognitive"] == "capability_offline"
    assert progress["rounds"] == []
    assert progress["plan_exhausted"] is False


# -- G13：自动推进不触碰既有 run（其它 Checkpoint 零改写） ----------------------


def test_g13_auto_advance_never_rewrites_other_runs(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    # 既有无关 run。
    make_run(store, run_id="exec:fragment-intent:other")
    before = store.latest_sequence("exec:fragment-intent:other")
    service = _proposed_alignment(store, tmp_path)
    assert service.maybe_auto_advance("align:auto-1") is True
    after = store.latest_sequence("exec:fragment-intent:other")
    assert before == after, "既有无关 run 的 Checkpoint 序列零变化"


# -- rev5 P0：Graph 宏 v3 反馈边真实 traversal（GraphRuntime 集成） -------------

MACRO_GOAL = "核验示例产品公开发布风险"  # infer_claim_types → ["release", "risk"]


def _macro_v3_runtime(store: SQLiteCheckpointStore, runner) -> GraphRuntime:
    return GraphRuntime(
        store,
        {RESEARCH_MACRO_V3_GRAPH_ID: RESEARCH_MACRO_V3_SPEC},
        research_adapters(runner),
    )


def test_rev5_macro_v3_feedback_edge_real_traversal_then_ready(tmp_path: Path) -> None:
    """第一轮有缺口必须真实走 e_coverage_feedback 回采集段；第二轮补齐后
    ready 进 judgment，全程无人工 gate 直到 human_review。"""
    store = SQLiteCheckpointStore(tmp_path / "macro.sqlite3")
    # 第一段（max_rounds=1）：official 命中 release、independent 空 →
    # gaps=["risk"]；feedback 后第二段 replan 改述查询真实新增搜索，
    # 返回后来出现的 issues 页 → risk 覆盖。
    search = FakeSearchTransport([DDG_PAGE, EMPTY_DDG, COUNTER_DDG])
    fetch = _PerUrlFetchTransport(
        {
            "https://example.com/release": RELEASE_BODY,
            "https://example.com/issues": ISSUE_BODY,
        }
    )
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=search,
        fetch=fetch,
    )
    runtime = _macro_v3_runtime(store, runner)
    run_id = "exec:macro-v3-ready"
    runtime.register_run(
        RESEARCH_MACRO_V3_SPEC,
        run_id,
        fragment_ref="fixture:macro-v3",
        run_inputs={"research_goal": MACRO_GOAL},
    )
    outcome = runtime.run_until_settled(run_id)
    # 全自动推进到人工闸门（judge 后的 human_review），此前零人工 gate。
    assert outcome.event == "human_gate_requested"
    _, state, _sequence = runtime._load(run_id)
    edges = [str(item["edge_id"]) for item in state["edges_taken"]]
    # 反馈边真实 traversal=1（不再只是静态 spec）。
    assert edges.count("e_coverage_feedback") == 1, edges
    assert "e_coverage_judge" in edges, edges
    assert "e_coverage_watch" not in edges, edges
    # 第二轮 query 真实新增（改述查询）；首轮 completed 动作零重发。
    assert len(search.calls) == 3, search.calls
    assert search.calls[0] != search.calls[2], "第二轮必须是改述查询"
    # 分段结束不得冒充全局耗尽；最终 coverage 无缺口。
    collection = runner.collection_outcome(run_id)
    assert collection["candidate_coverage"]["gaps"] == []
    assert collection["plan_exhausted"] is False


def test_rev5_macro_v3_second_round_still_gaps_routes_to_watch(tmp_path: Path) -> None:
    """第二轮后仍有缺口进入观察暂停（rev6：非终态 paused + 持久
    watching），绝不进人工闸门、不成用户待办、绝不是 terminal output。"""
    store = SQLiteCheckpointStore(tmp_path / "macro.sqlite3")
    search = FakeSearchTransport(EMPTY_DDG)
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=search,
    )
    runtime = _macro_v3_runtime(store, runner)
    run_id = "exec:macro-v3-watch"
    runtime.register_run(
        RESEARCH_MACRO_V3_SPEC,
        run_id,
        fragment_ref="fixture:macro-v3",
        run_inputs={"research_goal": MACRO_GOAL},
    )
    outcome = runtime.run_until_settled(run_id)
    # rev6 P0-2：观察出口是非终态 paused（runtime 既有 pause 契约），
    # 不是 completed/failed——coordinator 的 due cycle 可 reopen 恢复。
    assert outcome.event == "paused", outcome
    _, state, _sequence = runtime._load(run_id)
    assert state["run_status"] == "paused"
    edges = [str(item["edge_id"]) for item in state["edges_taken"]]
    assert edges.count("e_coverage_feedback") == 1, edges
    assert "e_coverage_judge" not in edges, edges
    assert not state.get("human_gates"), "观察暂停绝不进人工闸门"
    # 第二段 replan 两条改述查询真实新增（首轮 2 + 第二段 2）。
    assert len(search.calls) == 4, search.calls
    # 全局轮尽仍判耗尽并持久 watching（due 条件）。
    collection = runner.collection_outcome(run_id)
    assert collection["plan_exhausted"] is True
    watch = runner.research_watch(run_id)
    assert watch["status"] == "watching" and watch["reason"] == "plan_exhausted"


def test_rev5_segmented_max_rounds_never_fakes_global_exhaustion(
    tmp_path: Path,
) -> None:
    """runner 级锁定：分段（max_rounds=1）结束不冒充全局耗尽；
    全局轮尽仍判耗尽。"""
    store = SQLiteCheckpointStore(tmp_path / "g.sqlite3")
    make_run(store)
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        search=FakeSearchTransport(EMPTY_DDG),
    )
    outcome1 = runner.collect("exec:fragment-intent:test", GOAL, max_rounds=1)
    assert outcome1["stop_reason"] == "not_found"
    assert outcome1["searches"] > 0
    assert outcome1["plan_exhausted"] is False, "分段结束不得冒充全局计划耗尽"
    outcome2 = runner.collect("exec:fragment-intent:test", GOAL, max_rounds=2)
    assert outcome2["plan_exhausted"] is True, "冻结全局轮尽仍判耗尽"


# -- rev6 P0-1 跨层：intent 投影 → 宏 v3 spec identity --------------------------


def test_rev6_intent_projection_switches_new_proposal_to_macro_v3(
    tmp_path: Path,
) -> None:
    """rev6 P0-1 跨层：新 intent 升级提案投影的 spec_id/spec_digest 为宏
    v3（真实创建链上游）；材料字段齐全、失败关闭不缺省。"""
    from dataclasses import replace as dc_replace

    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    service = _proposed_alignment(store, tmp_path)
    assert service.maybe_auto_advance("align:auto-1") is True
    exec_run_id = store.run_ids_with_prefix("fragment-intent-execution-v1")[0]
    checkpoint = store.latest(exec_run_id)
    binding = dict(checkpoint.eval_results.get("execution_binding", {}))
    binding["alignment_id"] = "align:auto-1"
    result_digest = "d" * 64
    record = {
        "title": "Example Release",
        "url": "https://example.com/release",
        "marker": "newly_collected",
        "source_target": "official",
        "evidence_digest": "c" * 64,
    }
    edited = dc_replace(
        checkpoint,
        eval_results={
            **checkpoint.eval_results,
            "execution_binding": binding,
            "result_digest": result_digest,
            "graph_escalation": {
                "status": "proposed",
                "source_result_digest": result_digest,
            },
            "research_evidence": [record],
        },
    )
    assert store.compare_and_append(
        edited, expected_sequence=store.latest_sequence(exec_run_id)
    ) is not None
    projection = service.get_alignment("align:auto-1")
    escalation = projection["execution"]["graph_escalation"]
    assert escalation["spec_id"] == RESEARCH_MACRO_V3_GRAPH_ID
    assert escalation["spec_digest"] == RESEARCH_MACRO_V3_SPEC_DIGEST
    assert escalation["status"] == "proposed"
    assert escalation["evidence_bundle_digest"]
    assert escalation["escalation_id"].startswith("escalation:")


# -- rev6 P0-2：Graph watch 的 due 恢复与 reopen --------------------------------


def _graph_execution(store: SQLiteCheckpointStore, runner, ledger_path: Path):
    from graph_runtime.agent_ledger import AgentLedgerStore
    from graph_runtime.research_bridge import ResearchExecution
    from graph_runtime.service import GraphService

    return ResearchExecution(
        store,
        GraphService(store, {RESEARCH_MACRO_V3_GRAPH_ID: RESEARCH_MACRO_V3_SPEC}),
        AgentLedgerStore(ledger_path),
        runner,
    )


def _paused_macro_watch_run(
    store: SQLiteCheckpointStore, tmp_path: Path, clock, search, fetch=None
):
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=search,
        fetch=fetch,
        clock=clock,
    )
    runtime = _macro_v3_runtime(store, runner)
    run_id = "exec:macro-v3-reopen"
    runtime.register_run(
        RESEARCH_MACRO_V3_SPEC,
        run_id,
        fragment_ref="fixture:macro-v3",
        run_inputs={"research_goal": MACRO_GOAL},
    )
    outcome = runtime.run_until_settled(run_id)
    assert outcome.event == "paused", outcome
    return runner, runtime, run_id


def test_rev6_graph_watch_due_scan_reopens_same_run(tmp_path: Path) -> None:
    """rev6 P0-2 端到端：两轮无结果 → paused/watching；无人打开 UI 的
    due scan → 新 observation cycle 取得后来出现的来源 → 同一 run
    reopen → coverage 重判离开 watch；重复扫描零新动作。"""
    store = SQLiteCheckpointStore(tmp_path / "macro.sqlite3")
    clock = FrozenClock()
    search = FakeSearchTransport(
        [EMPTY_DDG, EMPTY_DDG, EMPTY_DDG, EMPTY_DDG, DDG_PAGE, COUNTER_DDG]
    )
    fetch = _PerUrlFetchTransport(
        {
            "https://example.com/release": RELEASE_BODY,
            "https://example.com/issues": ISSUE_BODY,
        }
    )
    runner, runtime, run_id = _paused_macro_watch_run(store, tmp_path, clock, search, fetch)
    _, paused_state, _ = runtime._load(run_id)
    assert paused_state["run_status"] == "paused"
    assert runner.research_watch(run_id)["status"] == "watching"
    execution = _graph_execution(store, runner, tmp_path / "ledger2.sqlite3")
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    calls_before = list(search.calls)
    result = execution.evaluate_due_watches()
    assert result == {"scanned": 1, "resumed": 1}
    # 新 cycle 真实搜索 2 次（首轮 completed 零重发），后来出现的来源被抓取。
    assert len(search.calls) == len(calls_before) + 2, search.calls
    # 同一 run reopen：不创建替代 run；coverage 重判进授权检查；证据完整
    # 且 synthesis 关闭 → 模型授权治理闸门，绝不是候选人工复核。
    assert store.run_ids_with_prefix("graph:fragment-research-macro-v3") == [run_id]
    _, state, _ = runtime._load(run_id)
    assert state["run_status"] != "paused"
    edges = [str(item["edge_id"]) for item in state["edges_taken"]]
    assert "e_coverage_judge" in edges, edges
    assert "synthesis_authorization_gate" in state["human_gates"]
    assert "human_review" not in state["human_gates"]
    assert runner.research_watch(run_id) is None
    # 重启/重复扫描：due 未到 → 零新动作、零新 edge。
    calls_after = list(search.calls)
    edges_after = len(state["edges_taken"])
    assert execution.evaluate_due_watches() == {"scanned": 1, "resumed": 0}
    assert search.calls == calls_after
    _, state3, _ = runtime._load(run_id)
    assert len(state3["edges_taken"]) == edges_after


def test_rev6_graph_watch_concurrent_scans_single_flight(tmp_path: Path) -> None:
    """rev6 P0-2：两个并发 due 扫描至多一个产生网络/reopen 副作用。"""
    store = SQLiteCheckpointStore(tmp_path / "macro.sqlite3")
    clock = FrozenClock()
    seed_search = FakeSearchTransport(EMPTY_DDG)
    runner, runtime, run_id = _paused_macro_watch_run(store, tmp_path, clock, seed_search)
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    search_a = FakeSearchTransport(EMPTY_DDG)
    search_b = FakeSearchTransport(EMPTY_DDG)
    runner_a = make_autonomy_runner(
        store, tmp_path / "a", live=True, collection_enabled=True,
        synthesis_enabled=False, search=search_a, clock=clock,
    )
    runner_b = make_autonomy_runner(
        store, tmp_path / "b", live=True, collection_enabled=True,
        synthesis_enabled=False, search=search_b, clock=clock,
    )
    execution_a = _graph_execution(store, runner_a, tmp_path / "la.sqlite3")
    execution_b = _graph_execution(store, runner_b, tmp_path / "lb.sqlite3")
    barrier = threading.Barrier(2)
    results: dict[str, dict[str, int]] = {}

    def work(name: str, execution) -> None:
        barrier.wait(timeout=10)
        results[name] = execution.evaluate_due_watches()

    threads = [
        threading.Thread(target=work, args=("a", execution_a)),
        threading.Thread(target=work, args=("b", execution_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    resumed_total = sum(item["resumed"] for item in results.values())
    assert resumed_total == 1, f"并发扫描至多一个 cycle 生效：{results}"
    total_calls = len(search_a.calls) + len(search_b.calls)
    # 至多一个 evaluator 的完整有界 cycle（2 轮 × 2 查询）真实搜索；
    # 败者零网络。
    assert total_calls <= 4, search_a.calls + search_b.calls
    assert min(len(search_a.calls), len(search_b.calls)) == 0
    # attempts 恰好 +1（cycle 完成写幂等），无重复 edge。
    watch = runner.research_watch(run_id)
    assert watch["attempts"] == 2


def test_rev6_graph_watch_collection_disabled_zero_side_effects(tmp_path: Path) -> None:
    """rev6 P0-2：collection 关闭时不领取、不网络、不改 watch、run 保持
    paused 原样。"""
    store = SQLiteCheckpointStore(tmp_path / "macro.sqlite3")
    clock = FrozenClock()
    runner, runtime, run_id = _paused_macro_watch_run(
        store, tmp_path, clock, FakeSearchTransport(EMPTY_DDG)
    )
    clock.advance(gr.WATCH_RECHECK_SECONDS + 1)
    search_off = FakeSearchTransport(DDG_PAGE)
    runner_off = make_autonomy_runner(
        store, tmp_path / "off", live=False, collection_enabled=False,
        search=search_off, clock=clock,
    )
    execution_off = _graph_execution(store, runner_off, tmp_path / "loff.sqlite3")
    watch_before = runner_off.research_watch(run_id)
    assert execution_off.evaluate_due_watches() == {"scanned": 1, "resumed": 0}
    assert search_off.calls == []
    assert runner_off.research_watch(run_id) == watch_before
    _, state, _ = runtime._load(run_id)
    assert state["run_status"] == "paused"


# -- rev6 P0-3：synthesis 闸门与候选复核边界 ------------------------------------


def test_rev6_synthesis_off_goes_to_authorization_gate_not_review(
    tmp_path: Path,
) -> None:
    """rev6 P0-3：证据充分但 synthesis 关闭 → 模型授权治理闸门（认知态
    awaiting_model_authorization）；零 model_calls、judge 未执行、绝不
    把空结果交给候选人工复核。"""
    store = SQLiteCheckpointStore(tmp_path / "macro.sqlite3")
    search = FakeSearchTransport([DDG_PAGE, COUNTER_DDG])
    fetch = _PerUrlFetchTransport(
        {
            "https://example.com/release": RELEASE_BODY,
            "https://example.com/issues": ISSUE_BODY,
        }
    )
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=search,
        fetch=fetch,
    )
    runtime = _macro_v3_runtime(store, runner)
    run_id = "exec:macro-v3-authz"
    runtime.register_run(
        RESEARCH_MACRO_V3_SPEC,
        run_id,
        fragment_ref="fixture:macro-v3",
        run_inputs={"research_goal": MACRO_GOAL},
    )
    outcome = runtime.run_until_settled(run_id)
    assert outcome.event == "human_gate_requested", outcome
    _, state, _ = runtime._load(run_id)
    gates = state["human_gates"]
    assert "synthesis_authorization_gate" in gates, gates
    assert "human_review" not in gates, "supported=false 空结果绝不交给用户决定"
    assert state["nodes"]["judgment"]["status"] in ("pending", "ready"), (
        "synthesis 关闭时 judge 不得提前执行"
    )
    assert runner.collection_outcome(run_id)["candidate_coverage"]["gaps"] == []
    checkpoint = store.latest(run_id)
    assert "research_synthesis" not in checkpoint.eval_results, "零模型调用"


def test_rev6_authorized_synthesis_enters_human_review(tmp_path: Path) -> None:
    """rev6 P0-3：只有经 Receipt 人闸授权且契约验证的 synthesized 候选
    结果才进入 human_review（离线合成 fixture，零真实模型/凭据）。"""
    from graph_runtime.agent_ledger import AgentLedgerStore
    from graph_runtime.runtime import human_decision_id

    from .graph_pilot_support import fresh_price_snapshot
    from .test_fragment_governed_research import (
        FakeSynthesisTransport,
        collected_evidence_id,
        fixture_credential_reader,
        output_bytes,
        valid_output,
    )

    store = SQLiteCheckpointStore(tmp_path / "macro.sqlite3")
    clock = FrozenClock()
    prepared: dict[str, Any] = {}

    class LazyTransport(FakeSynthesisTransport):
        def send_once(self, request: Any, credential: str) -> Any:
            from fragment_loop.governed_research import ResearchSendOutcome

            self.outcome = ResearchSendOutcome(
                "true",
                output_bytes(valid_output(prepared["evidence_id"])),
                None,
                200,
                gr.RESEARCH_MODEL,
                128,
                64,
            )
            return super().send_once(request, credential)

    transport = LazyTransport(gr.ResearchSendOutcome("false", None, "transport_error"))
    runner = GovernedResearchRunner(
        store,
        live_enabled=True,
        collection_enabled=True,
        synthesis_enabled=True,
        search_transport=FakeSearchTransport(DDG_PAGE),
        fetch_transport=FakeFetchTransport(),
        ledger=AgentLedgerStore(tmp_path / "ledger.sqlite3"),
        price_reader=lambda: fresh_price_snapshot(clock),
        credential_reader=fixture_credential_reader,
        synthesis_transport=transport,
        clock=clock,
        monotonic=FrozenMonotonic(),
    )
    runtime = _macro_v3_runtime(store, runner)
    run_id = "exec:macro-v3-authorized"
    runtime.register_run(
        RESEARCH_MACRO_V3_SPEC,
        run_id,
        fragment_ref="fixture:macro-v3",
        run_inputs={"research_goal": GOAL},
    )
    outcome = runtime.run_until_settled(run_id)
    assert outcome.event == "human_gate_requested", outcome
    _, state, sequence = runtime._load(run_id)
    gate = state["human_gates"]["synthesis_authorization_gate"]
    # 采集已完成：准备 synthesis 输出材料并签发 Receipt（人闸路径）。
    collection = runner.collection_outcome(run_id)
    prepared["evidence_id"] = collected_evidence_id(collection)
    status, receipt = runner.issue_receipt(run_id)
    assert status == 201
    # Receipt 签发会推进 checkpoint 序列：重新读取最新 sequence/gate 绑定。
    _, state, sequence = runtime._load(run_id)
    gate = state["human_gates"]["synthesis_authorization_gate"]
    decision_id = human_decision_id(
        requester="nigo",
        decision="approve_synthesis",
        run_id=run_id,
        node_id="synthesis_authorization_gate",
        spec_digest=gate["spec_digest"],
        input_digest=gate["input_digest"],
        expected_sequence=sequence,
    )
    _, applied = runtime.apply_human_decision(
        run_id,
        "synthesis_authorization_gate",
        decision="approve_synthesis",
        spec_digest=gate["spec_digest"],
        input_digest=gate["input_digest"],
        expected_sequence=sequence,
        requester="nigo",
        decision_id=decision_id,
    )
    assert applied is True
    outcome2 = runtime.run_until_settled(run_id)
    assert outcome2.event == "human_gate_requested", outcome2
    _, state2, _ = runtime._load(run_id)
    assert "human_review" in state2["human_gates"], (
        "契约验证的 synthesized 候选结果才进入人工复核"
    )
    judge = state2["nodes"]["judgment"]
    assert judge["status"] == "succeeded"
    assert judge["output"]["supported"] is True
    assert judge["output"]["claim_summary"]
    assert transport.credentials == ["fixture-credential"], (
        "模型发送必须发生在 Receipt 人闸授权之后"
    )


# -- rev10：旧离线零来源执行恢复 -------------------------------------------------

REV10_FRAGMENT_ID = "2026-08-08-19-41-45-1c8e88-desktop"
REV10_TITLE = "示例产品公开发布信息"


def _rev10_vault(
    root: Path, *, approved: bool = True, raw_url: str = "https://example.com/release"
) -> Path:
    vault = root / "vault"
    raw = vault / "Notes/散记/碎片想法" / f"{REV10_FRAGMENT_ID}.md"
    organized = vault / "Notes/AI创业/碎片整理" / f"{REV10_FRAGMENT_ID}.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    organized.parent.mkdir(parents=True, exist_ok=True)
    (vault / "candidates").mkdir(parents=True, exist_ok=True)
    (vault / "assets").mkdir(parents=True, exist_ok=True)
    raw.write_text(
        "---\n"
        'type: "碎片想法"\n'
        'captured_at: "2026-08-08T11:41:45.823Z"\n'
        'pipeline_status: "priority_queued"\n'
        f"nigo-loop: {'true' if approved else 'false'}\n"
        "---\n\n"
        f"{raw_url} 公开发布信息\n",
        encoding="utf-8",
    )
    organized.write_text(
        "---\n"
        'type: "已整理碎片"\n'
        f'title: "{REV10_TITLE}"\n'
        'category: "AI创业"\n'
        f'source_file: "{REV10_FRAGMENT_ID}.md"\n'
        'pipeline_status: "ready_to_review"\n'
        'experiment_status: "ready"\n'
        'goal: "核验示例产品是否已公开发布"\n'
        'next_action: "搜索官方来源核验"\n'
        "---\n\n"
        "# 未验证整理结果\n",
        encoding="utf-8",
    )
    return vault


def _rev10_bridge(vault: Path, db_path: Path):
    from fragment_loop.continuation_bridge import FragmentContinuationBridge
    from fragment_loop.product_review import ProductReviewService

    review = ProductReviewService(
        vault / "candidates",
        vault / "assets",
        vault_root=vault,
    )
    return FragmentContinuationBridge(review, loop_db=db_path)


def _rev10_proposal(source: dict) -> dict:
    return {
        "suggested_intents": ["verify"],
        "dynamic_intents": [],
        "reasoning": "公开事实核验",
        "plan": "先检查官方与公开社区来源",
        "expected_result": "得到来源线索",
        "exclusions": [],
        "recommended_route": "verify",
        "execution_scope": {
            "capabilities": ["受治理研究"],
            "external_scope": ["官方与公开社区来源"],
            "model_call_cap": 1,
            "cost_cap_cny": 2.0,
            "side_effect": "none",
        },
    }


def _rev10_service(store: SQLiteCheckpointStore, bridge, runner):
    from fragment_loop.intent_service import FragmentIntentService

    return FragmentIntentService(
        store,
        lambda fragment_id, input_digest: {},
        _rev10_proposal,
        verify_adapter=gr.GovernedResearchVerifyAdapter(runner),
        continuation_bridge=bridge,
    )


def _rev10_seed_offline_lineage(
    store: SQLiteCheckpointStore,
    bridge,
    *,
    scope: dict | None = None,
    searches: int = 0,
    fetches: int = 0,
    with_active_evidence: bool = False,
    input_digest: str | None = None,
    stop_reason: str = "live_disabled",
    search_fingerprint: str | None = None,
) -> None:
    from common.checkpoint import LoopCheckpoint

    if input_digest is not None:
        digest = input_digest
    else:
        try:
            digest = str(
                bridge.discover_intent_source(REV10_FRAGMENT_ID)["input_digest"]
            )
        except Exception:
            # 非 nigo-loop 场景：桥拒绝时种子仍用占位 digest 构造旧 lineage。
            digest = "a" * 64
    alignment = LoopCheckpoint(
        loop_id="fragment-intent-alignment-v1",
        run_id="align:old-1",
        fragment_id=REV10_FRAGMENT_ID,
        current_node="alignment",
        status="passed",
        fragment_title=REV10_TITLE,
        eval_results={
            "alignment": {
                "alignment_digest": "a" * 64,
                "input_digest": digest,
                "episode_id": "ep:old-1",
                "recommended_route": "verify",
                "execution_run_id": "exec:old-1",
            }
        },
    )
    assert store.compare_and_append(alignment, expected_sequence=0) is not None
    records = (
        [
            {
                "marker": "newly_collected",
                "evidence_digest": "e" * 64,
                "title": "Example Release",
                "url": "https://example.com/release",
                "source_target": "official",
            }
        ]
        if with_active_evidence
        else []
    )
    execution = LoopCheckpoint(
        loop_id="fragment-intent-execution-v1",
        run_id="exec:old-1",
        fragment_id=REV10_FRAGMENT_ID,
        current_node="harvest",
        status="passed",
        fragment_title=REV10_TITLE,
        eval_results={
            "execution_binding": {
                "alignment_id": "align:old-1",
                "episode_id": "ep:old-1",
                "execution_scope": scope
                or {
                    "capabilities": ["受治理研究"],
                    "external_scope": ["官方与公开社区来源"],
                    "side_effect": "none",
                },
            },
            "result_digest": "b" * 64,
            "research_collection": {
                "goal": "核验示例产品是否已公开发布",
                "stop_reason": stop_reason,
                "records": records,
                "searches": searches,
                "fetches": fetches,
                "notes": [],
                **(
                    {"search_fingerprint": search_fingerprint}
                    if search_fingerprint is not None
                    else {}
                ),
            },
        },
    )
    assert store.compare_and_append(execution, expected_sequence=0) is not None


def _rev10_children(store: SQLiteCheckpointStore) -> list[str]:
    return [
        run_id
        for run_id in store.run_ids_with_prefix("fragment-intent-alignment-v1")
        if run_id != "align:old-1"
    ]


def test_rev10_offline_terminal_auto_recovers_with_child_and_collection(
    tmp_path: Path,
) -> None:
    """rev10 核心：历史 live_disabled + 0 网络的旧 terminal lineage，经
    当前 frontmatter 重验证后自动创建确定性 child（lineage + recovery
    reason + nigo_loop=true），system_policy 自动 verify 并只执行公开
    collection；旧 checkpoint 原样保留；模型/凭据恒 0。"""
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    _rev10_seed_offline_lineage(store, bridge)
    search = FakeSearchTransport(DDG_PAGE)
    fetch = FakeFetchTransport()
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=search,
        fetch=fetch,
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    old_sequence = store.latest_sequence("exec:old-1")
    old_collection = store.latest("exec:old-1").eval_results["research_collection"]
    result = service.recover_offline_executions()
    assert result == {"scanned": 1, "recovered": 1}
    children = _rev10_children(store)
    assert len(children) == 1, "确定性 child 恰好一个"
    child = store.latest(children[0])
    child_alignment = child.eval_results["alignment"]
    assert child_alignment["parent_run_id"] == "exec:old-1"
    assert child_alignment["parent_episode_id"] == "ep:old-1"
    assert child_alignment["recovery_reason"] == "offline_zero_evidence"
    assert child_alignment["nigo_loop"] is True
    assert child.status == "passed", "system_policy 自动选 verify 并登记"
    assert child_alignment["decision_source"] == gr.AUTO_DECISION_SOURCE
    child_execution = store.latest(str(child_alignment["execution_run_id"]))
    collection = child_execution.eval_results["research_collection"]
    assert collection["stop_reason"] == "completed"
    # rev14：入口链接成为 seed candidate 时可零搜索直达抓取；fetches ≥ 1
    # 且必须真实取得证据（搜索或 seed 任一来源路径）。
    assert collection["fetches"] >= 1
    assert any(
        record.get("marker") == "newly_collected" for record in collection["records"]
    ), "公开 collection 必须真实取得证据"
    binding = child_execution.eval_results["execution_binding"]
    assert binding["auto_synthesis_allowed"] is False, "模型/Keychain 硬关闭"
    assert fetch.calls, "公开 collection 必须真实执行（seed 抓取或搜索抓取）"
    # 旧 terminal run 不重开、不覆盖：sequence 与 collection 逐字原样。
    assert store.latest_sequence("exec:old-1") == old_sequence
    assert (
        store.latest("exec:old-1").eval_results["research_collection"] == old_collection
    )
    # 幂等重扫：claim 已消耗，零新增 child、零新增搜索。
    calls_before = list(search.calls)
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 0}
    assert _rev10_children(store) == children
    assert search.calls == calls_before


def test_rev10_recovery_zero_action_matrix(tmp_path: Path) -> None:
    """rev10 零动作矩阵：非 nigo-loop / 来源漂移 / 非零旧网络 / 私人能力
    标记 / 已有新证据 / collection 关闭 / 无桥——均零恢复、零 child。"""
    vault = _rev10_vault(tmp_path)

    def run_case(
        name: str,
        *,
        vault_override: Path | None = None,
        scope: dict | None = None,
        searches: int = 0,
        with_active_evidence: bool = False,
        input_digest: str | None = None,
        collection_enabled: bool = True,
        with_bridge: bool = True,
    ) -> None:
        case_vault = vault_override or vault
        bridge = _rev10_bridge(case_vault, tmp_path / f"{name}.sqlite3")
        store = bridge.store
        _rev10_seed_offline_lineage(
            store,
            bridge,
            scope=scope,
            searches=searches,
            with_active_evidence=with_active_evidence,
            input_digest=input_digest,
        )
        runner = make_autonomy_runner(
            store,
            tmp_path / name,
            live=collection_enabled,
            collection_enabled=collection_enabled,
            search=FakeSearchTransport(DDG_PAGE),
            clock=FrozenClock(),
        )
        service = _rev10_service(store, bridge if with_bridge else bridge, runner)
        if not with_bridge:
            service.continuation_bridge = None
        result = service.recover_offline_executions()
        assert result["recovered"] == 0, f"{name}: 必须零恢复"
        assert _rev10_children(store) == [], f"{name}: 必须零 child"

    run_case("non_nigo_loop", vault_override=_rev10_vault(tmp_path / "v2", approved=False))
    run_case("source_drift", input_digest="0" * 64)
    run_case("nonzero_network", searches=2)
    run_case(
        "private_capability",
        scope={
            "capabilities": ["读取私人笔记"],
            "external_scope": ["官方与公开社区来源"],
            "side_effect": "none",
        },
    )
    run_case("fresh_evidence", with_active_evidence=True)
    run_case("collection_off", collection_enabled=False)
    run_case("no_bridge", with_bridge=False)


def test_rev10_concurrent_recovery_single_flight(tmp_path: Path) -> None:
    """rev10：两个并发恢复扫描（唯一键领取）恰好一个生效，child 唯一。"""
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    _rev10_seed_offline_lineage(store, bridge)
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=FakeSearchTransport(DDG_PAGE),
        clock=FrozenClock(),
    )
    service_a = _rev10_service(store, bridge, runner)
    service_b = _rev10_service(store, bridge, runner)
    barrier = threading.Barrier(2)
    results: dict[str, dict[str, int]] = {}

    def work(name: str, service) -> None:
        barrier.wait(timeout=10)
        results[name] = service.recover_offline_executions()

    threads = [
        threading.Thread(target=work, args=("a", service_a)),
        threading.Thread(target=work, args=("b", service_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    recovered_total = sum(item["recovered"] for item in results.values())
    assert recovered_total == 1, f"并发恢复恰好一个生效：{results}"
    assert len(_rev10_children(store)) == 1, "并发恢复 child 必须唯一"


# -- rev11：continuation 谱系回溯后的旧离线恢复 ---------------------------------

R0_DIGEST = "b0" * 32
R1_DIGEST = "b1" * 32
R2_DIGEST = "b2" * 32

_CLEAN_SCOPE = {
    "capabilities": ["受治理研究"],
    "external_scope": ["官方与公开社区来源"],
    "side_effect": "none",
}


def _rev11_execution(
    *,
    run_id: str,
    fragment_id: str,
    episode_id: str,
    alignment_id: str,
    result_digest: str,
    parent_run_id: str | None = None,
    parent_episode_id: str | None = None,
    source_result_digest: str | None = None,
    searches: object = 0,
    fetches: object = 0,
):
    from common.checkpoint import LoopCheckpoint

    binding: dict[str, object] = {
        "alignment_id": alignment_id,
        "episode_id": episode_id,
        "execution_scope": dict(_CLEAN_SCOPE),
    }
    if parent_run_id is not None:
        binding["parent_run_id"] = parent_run_id
        binding["parent_episode_id"] = parent_episode_id
        binding["source_result_digest"] = source_result_digest
    return LoopCheckpoint(
        loop_id="fragment-intent-execution-v1",
        run_id=run_id,
        fragment_id=fragment_id,
        current_node="harvest",
        status="passed",
        fragment_title=REV10_TITLE,
        eval_results={
            "execution_binding": binding,
            "result_digest": result_digest,
            "research_collection": {
                "goal": "核验示例产品是否已公开发布",
                "stop_reason": "live_disabled",
                "records": [],
                "searches": searches,
                "fetches": fetches,
                "notes": [],
            },
        },
    )


def _rev11_alignment(
    *,
    run_id: str,
    input_digest: str,
    episode_id: str,
    execution_run_id: str,
    parent_episode_id: str | None = None,
):
    from common.checkpoint import LoopCheckpoint

    alignment: dict[str, object] = {
        "alignment_digest": "a" * 64,
        "input_digest": input_digest,
        "episode_id": episode_id,
        "recommended_route": "verify",
        "execution_run_id": execution_run_id,
    }
    if parent_episode_id is not None:
        alignment["parent_episode_id"] = parent_episode_id
    return LoopCheckpoint(
        loop_id="fragment-intent-alignment-v1",
        run_id=run_id,
        fragment_id=REV10_FRAGMENT_ID,
        current_node="alignment",
        status="passed",
        fragment_title=REV10_TITLE,
        eval_results={"alignment": alignment},
    )


def _rev11_seed_two_level_lineage(
    store: SQLiteCheckpointStore,
    bridge,
    *,
    forged_parent_digest: bool = False,
    cross_fragment: bool = False,
    self_cycle: bool = False,
    root_drift: bool = False,
) -> str:
    """root → c1 → c2（最新 terminal，live_disabled 0 网络）两级
    continuation 谱系；root source digest = 当前 bridge digest（除非
    root_drift）。返回当前 bridge source digest。"""
    root_source_digest = str(
        bridge.discover_intent_source(REV10_FRAGMENT_ID)["input_digest"]
    )
    def append(checkpoint) -> None:
        # compare_and_append 的期望序列按 run 计：每个新 run 的首条为 0。
        assert store.compare_and_append(checkpoint, expected_sequence=0) is not None

    # root alignment + root execution（root source digest 或伪造漂移）
    append(
        _rev11_alignment(
            run_id="align:root",
            input_digest=("0" * 64) if root_drift else root_source_digest,
            episode_id="ep:root",
            execution_run_id="exec:root",
        )
    )
    append(
        _rev11_execution(
            run_id="exec:root",
            fragment_id=REV10_FRAGMENT_ID,
            episode_id="ep:root",
            alignment_id="align:root",
            result_digest=R0_DIGEST,
        )
    )
    # c1（continuation lineage digest ≠ root source digest）
    c1_input = _digest_marker("c1")
    append(
        _rev11_alignment(
            run_id="align:c1",
            input_digest=c1_input,
            episode_id="ep:c1",
            execution_run_id="exec:c1",
            parent_episode_id="ep:root",
        )
    )
    append(
        _rev11_execution(
            run_id="exec:c1",
            fragment_id=("other-fragment" if cross_fragment else REV10_FRAGMENT_ID),
            episode_id="ep:c1",
            alignment_id="align:c1",
            result_digest=R1_DIGEST,
            parent_run_id="exec:root",
            parent_episode_id="ep:root",
            source_result_digest=R0_DIGEST,
        )
    )
    # c2（最新 terminal execution，live_disabled 0 网络）
    c2_input = _digest_marker("c2")
    append(
        _rev11_alignment(
            run_id="align:c2",
            input_digest=c2_input,
            episode_id="ep:c2",
            execution_run_id="exec:c2",
            parent_episode_id="ep:c1",
        )
    )
    append(
        _rev11_execution(
            run_id="exec:c2",
            fragment_id=REV10_FRAGMENT_ID,
            episode_id="ep:c2",
            alignment_id="align:c2",
            result_digest=R2_DIGEST,
            parent_run_id=("exec:c2" if self_cycle else "exec:c1"),
            parent_episode_id=("ep:c2" if self_cycle else "ep:c1"),
            source_result_digest=(
                R2_DIGEST if self_cycle else ("ff" * 32 if forged_parent_digest else R1_DIGEST)
            ),
        )
    )
    return root_source_digest


def _digest_marker(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def test_rev11_two_level_continuation_recovers_latest_child(tmp_path: Path) -> None:
    """rev11 核心：root（source digest = 当前 bridge digest）+ 两级
    continuation（child input_digest 是 lineage digest）后，最新 terminal
    child（live_disabled + 0 网络）沿谱系回溯验证通过并自动恢复；
    新 recovery child 挂在最新 terminal run 上，不挂 root。"""
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    _rev11_seed_two_level_lineage(store, bridge)
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=FakeSearchTransport(DDG_PAGE),
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    result = service.recover_offline_executions()
    assert result == {"scanned": 1, "recovered": 1}, result
    children = [
        run_id
        for run_id in store.run_ids_with_prefix("fragment-intent-alignment-v1")
        if run_id not in {"align:root", "align:c1", "align:c2"}
    ]
    assert len(children) == 1, "确定性 recovery child 恰好一个"
    child_alignment = store.latest(children[0]).eval_results["alignment"]
    # 新 recovery child 挂在最新 terminal episode/run（exec:c2），不挂 root。
    assert child_alignment["parent_run_id"] == "exec:c2"
    assert child_alignment["parent_episode_id"] == "ep:c2"
    assert child_alignment["recovery_reason"] == "offline_zero_evidence"
    assert child_alignment["nigo_loop"] is True
    # 旧历史不改写：root/c1/c2 的 alignment 仍是原 6 个 run。
    assert len(store.run_ids_with_prefix("fragment-intent-alignment-v1")) == 4


def test_rev11_lineage_tampering_zero_action_matrix(tmp_path: Path) -> None:
    """rev11 零动作矩阵：伪造 parent digest / 跨 fragment / 循环 / 超过
    上限 / root source drift / 畸形 searches-fetches——均零恢复。"""
    vault = _rev10_vault(tmp_path)

    def run_case(name: str, **seed_kwargs) -> None:
        bridge = _rev10_bridge(vault, tmp_path / f"{name}.sqlite3")
        store = bridge.store
        _rev11_seed_two_level_lineage(store, bridge, **seed_kwargs)
        runner = make_autonomy_runner(
            store,
            tmp_path / name,
            live=True,
            collection_enabled=True,
            search=FakeSearchTransport(DDG_PAGE),
            clock=FrozenClock(),
        )
        service = _rev10_service(store, bridge, runner)
        result = service.recover_offline_executions()
        assert result["recovered"] == 0, f"{name}: 必须零恢复（{result}）"

    run_case("forged_parent", forged_parent_digest=True)
    run_case("cross_fragment", cross_fragment=True)
    run_case("self_cycle", self_cycle=True)
    run_case("root_drift", root_drift=True)


def test_rev11_lineage_hop_limit_zero_action(tmp_path: Path) -> None:
    """rev11：谱系超过冻结上限（8 跳）→ 零动作，绝不无界回溯。"""
    from fragment_loop.intent_service import OFFLINE_RECOVERY_LINEAGE_MAX_HOPS

    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    root_source_digest = str(
        bridge.discover_intent_source(REV10_FRAGMENT_ID)["input_digest"]
    )
    def append(checkpoint) -> None:
        # compare_and_append 的期望序列按 run 计：每个新 run 的首条为 0。
        assert store.compare_and_append(checkpoint, expected_sequence=0) is not None

    append(
        _rev11_alignment(
            run_id="align:root",
            input_digest=root_source_digest,
            episode_id="ep:root",
            execution_run_id="exec:root",
        )
    )
    append(
        _rev11_execution(
            run_id="exec:root",
            fragment_id=REV10_FRAGMENT_ID,
            episode_id="ep:root",
            alignment_id="align:root",
            result_digest=R0_DIGEST,
        )
    )
    # 构造超过上限的 continuation 链（MAX_HOPS + 2 级）。
    parent_run, parent_episode, parent_digest = "exec:root", "ep:root", R0_DIGEST
    for depth in range(1, OFFLINE_RECOVERY_LINEAGE_MAX_HOPS + 3):
        run_id = f"exec:c{depth}"
        append(
            _rev11_alignment(
                run_id=f"align:c{depth}",
                input_digest=_digest_marker(f"c{depth}"),
                episode_id=f"ep:c{depth}",
                execution_run_id=run_id,
                parent_episode_id=parent_episode,
            )
        )
        append(
            _rev11_execution(
                run_id=run_id,
                fragment_id=REV10_FRAGMENT_ID,
                episode_id=f"ep:c{depth}",
                alignment_id=f"align:c{depth}",
                result_digest=_digest_marker(f"r{depth}"),
                parent_run_id=parent_run,
                parent_episode_id=parent_episode,
                source_result_digest=parent_digest,
            )
        )
        parent_run, parent_episode, parent_digest = (
            run_id,
            f"ep:c{depth}",
            _digest_marker(f"r{depth}"),
        )
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        search=FakeSearchTransport(DDG_PAGE),
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    result = service.recover_offline_executions()
    assert result["recovered"] == 0, f"超界谱系必须零动作（{result}）"


def test_rev11_malformed_counters_never_abort_scan(tmp_path: Path) -> None:
    """rev11：searches/fetches 为字符串/None/bool 等畸形值时按零动作
    跳过且不中断整轮扫描（另一个合法 candidate 仍正常恢复）。"""
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    # 畸形 candidate（三个不同畸形形态）。
    from common.checkpoint import LoopCheckpoint

    for index, bad in enumerate(["0", None, True]):
        malformed = LoopCheckpoint(
            loop_id="fragment-intent-execution-v1",
            run_id=f"exec:bad-{index}",
            fragment_id=f"frag-bad-{index}",
            current_node="harvest",
            status="passed",
            fragment_title="t",
            eval_results={
                "execution_binding": {},
                "result_digest": "b" * 64,
                "research_collection": {
                    "goal": "g",
                    "stop_reason": "live_disabled",
                    "records": [],
                    "searches": bad,
                    "fetches": bad,
                    "notes": [],
                },
            },
        )
        assert store.compare_and_append(malformed, expected_sequence=0) is not None
    # 合法 candidate（root 形态，rev10 语义）。
    _rev10_seed_offline_lineage(store, bridge)
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=FakeSearchTransport(DDG_PAGE),
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    result = service.recover_offline_executions()
    assert result["recovered"] == 1, f"畸形值不得中断扫描（{result}）"


# -- rev12：最新 terminal 选择顺序与 root alignment 校验 -------------------------


def _rev12_seed_mixed_history(
    store: SQLiteCheckpointStore,
    bridge,
    *,
    newer_stop_reason: str,
    newer_searches: object,
    newer_fetches: object,
) -> None:
    """同 fragment 混合历史：旧 0/0 live_disabled run（较早 sequence）+
    一个更晚 terminal run（形态由参数决定）。"""
    _rev10_seed_offline_lineage(store, bridge)
    from common.checkpoint import LoopCheckpoint

    newer = LoopCheckpoint(
        loop_id="fragment-intent-execution-v1",
        run_id="exec:new-1",
        fragment_id=REV10_FRAGMENT_ID,
        current_node="harvest",
        status="passed",
        fragment_title=REV10_TITLE,
        eval_results={
            "execution_binding": {
                "alignment_id": "align:old-1",
                "episode_id": "ep:old-1",
                "execution_scope": dict(_CLEAN_SCOPE),
            },
            "result_digest": "c" * 64,
            "research_collection": {
                "goal": "核验示例产品是否已公开发布",
                "stop_reason": newer_stop_reason,
                "records": [],
                "searches": newer_searches,
                "fetches": newer_fetches,
                "notes": [],
            },
        },
    )
    assert store.compare_and_append(newer, expected_sequence=0) is not None


def test_rev12_mixed_history_never_falls_back_to_older_zero_network_run(
    tmp_path: Path,
) -> None:
    """rev12：同 fragment 旧 0/0 run + 更晚 terminal run（非零无证据 /
    completed-not_found / 畸形计数）——最新项不合格即零动作，绝不回退
    恢复更老 0-network run、绝不重复消费公开搜索预算。"""
    vault = _rev10_vault(tmp_path)
    cases = [
        ("nonzero_no_records", "not_found", 2, 1),
        ("completed_not_found", "completed", 2, 0),
        ("malformed_counters", "live_disabled", "0", "0"),
    ]
    for name, stop_reason, searches, fetches in cases:
        bridge = _rev10_bridge(vault, tmp_path / f"{name}.sqlite3")
        store = bridge.store
        _rev12_seed_mixed_history(
            store,
            bridge,
            newer_stop_reason=stop_reason,
            newer_searches=searches,
            newer_fetches=fetches,
        )
        search = FakeSearchTransport(DDG_PAGE)
        runner = make_autonomy_runner(
            store,
            tmp_path / name,
            live=True,
            collection_enabled=True,
            search=search,
            clock=FrozenClock(),
        )
        service = _rev10_service(store, bridge, runner)
        result = service.recover_offline_executions()
        assert result["recovered"] == 0, f"{name}: 更晚 terminal 不合格必须零动作（{result}）"
        assert _rev10_children(store) == [], f"{name}: 必须零 child"
        assert search.calls == [], f"{name}: 必须零搜索（不重复消费预算）"


def test_rev12_running_newer_run_does_not_mask_older_terminal(tmp_path: Path) -> None:
    """rev12 终态语义：更晚的 run 仍 running（非终态）时不参与"最新
    terminal"竞选——最新 terminal 仍是旧 0/0 run，可正常恢复。"""
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    _rev10_seed_offline_lineage(store, bridge)
    from common.checkpoint import LoopCheckpoint

    running = LoopCheckpoint(
        loop_id="fragment-intent-execution-v1",
        run_id="exec:running-1",
        fragment_id=REV10_FRAGMENT_ID,
        current_node="execute",
        status="running",
        fragment_title=REV10_TITLE,
        eval_results={},
    )
    assert store.compare_and_append(running, expected_sequence=0) is not None
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=FakeSearchTransport(DDG_PAGE),
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    result = service.recover_offline_executions()
    assert result == {"scanned": 1, "recovered": 1}, result


def test_rev12_root_alignment_validation_zero_action(tmp_path: Path) -> None:
    """rev12 root alignment 校验：跨 alignment loop / alignment-execution
    episode 不一致 → 零动作。"""
    from common.checkpoint import LoopCheckpoint

    vault = _rev10_vault(tmp_path)

    def run_case(name: str, alignment_checkpoint: LoopCheckpoint) -> None:
        bridge = _rev10_bridge(vault, tmp_path / f"{name}.sqlite3")
        store = bridge.store
        assert store.compare_and_append(alignment_checkpoint, expected_sequence=0) is not None
        execution = _rev11_execution(
            run_id="exec:root",
            fragment_id=REV10_FRAGMENT_ID,
            episode_id="ep:root",
            alignment_id="align:root",
            result_digest=R0_DIGEST,
        )
        assert store.compare_and_append(execution, expected_sequence=0) is not None
        runner = make_autonomy_runner(
            store,
            tmp_path / name,
            live=True,
            collection_enabled=True,
            search=FakeSearchTransport(DDG_PAGE),
            clock=FrozenClock(),
        )
        service = _rev10_service(store, bridge, runner)
        result = service.recover_offline_executions()
        assert result["recovered"] == 0, f"{name}: 必须零恢复（{result}）"

    root_digest = str(
        _rev10_bridge(vault, tmp_path / "digest.sqlite3").discover_intent_source(
            REV10_FRAGMENT_ID
        )["input_digest"]
    )
    # 跨 alignment loop：alignment_id 指向非 alignment loop 的 checkpoint。
    wrong_loop = LoopCheckpoint(
        loop_id="fragment-intent-execution-v1",
        run_id="align:root",
        fragment_id=REV10_FRAGMENT_ID,
        current_node="alignment",
        status="passed",
        fragment_title=REV10_TITLE,
        eval_results={
            "alignment": {
                "alignment_digest": "a" * 64,
                "input_digest": root_digest,
                "episode_id": "ep:root",
            }
        },
    )
    run_case("wrong_loop", wrong_loop)
    # alignment/execution episode 不一致。
    episode_mismatch = _rev11_alignment(
        run_id="align:root",
        input_digest=root_digest,
        episode_id="ep:other",
        execution_run_id="exec:root",
    )
    run_case("episode_mismatch", episode_mismatch)


# -- rev14：公开来源 seed、Bing CN provider、capability fingerprint 恢复 ---------


def test_rev14_public_seed_url_extraction_and_normalization() -> None:
    """rev14 要求 1：入口元数据公开链接的 seed 提取——去查询参数与
    fragment、SSRF 校验、私网拒绝、无链接 None。"""
    from fragment_loop.research_fetch import public_https_seed_url

    assert public_https_seed_url("看 https://example.com/a/b?x=1&y=2#frag 这个") == (
        "https://example.com/a/b"
    )
    assert public_https_seed_url("https://192.168.1.1/x 私网") is None
    assert public_https_seed_url("https://10.0.0.1/internal") is None
    assert public_https_seed_url("没有链接") is None


def test_rev14_bing_cn_parse_and_security_boundaries() -> None:
    """rev14 要求 2/5：Bing CN 解析只产 title/url 候选、私网候选拒绝、
    畸形编码失败关闭；transport live_disabled 零网络失败关闭；provider
    fingerprint 闭集。"""
    import pytest as _pytest

    from fragment_loop.research_fetch import (
        BingCNSearchTransport,
        ResearchFetchError,
        parse_bing_cn_candidates,
        search_provider_fingerprint,
    )

    page = (
        b'<html><body><li class="b_algo"><h2><a href="https://example.com/release" '
        b'target="_blank">Example Release</a></h2></li>'
        b'<li class="b_algo"><h2><a href="https://10.0.0.1/internal">Internal</a></h2></li>'
        b"</body></html>"
    )
    candidates = parse_bing_cn_candidates(page)
    assert candidates == [{"title": "Example Release", "url": "https://example.com/release"}]
    with _pytest.raises(ResearchFetchError):
        parse_bing_cn_candidates(b"\xff\xfe\x00\x1f")
    with _pytest.raises(ResearchFetchError, match="research_live_disabled"):
        BingCNSearchTransport(live_enabled=False)("任何查询")
    assert search_provider_fingerprint("ddg") == "ddg:public-search-v1"
    assert search_provider_fingerprint("bing-cn") == "bing-cn:public-search-v1"
    with _pytest.raises(Exception):
        search_provider_fingerprint("evil")


def test_rev14_bridge_seed_flows_into_recovery_collection(tmp_path: Path) -> None:
    """rev14 要求 1/4：bridge 产出去查询参数的 source_seed_url → 恢复后
    child execution 的公开 collection 以 seed candidate 抓取（seed 目标
    URL 零搜索直达抓取；非人工闸、双轴真实）。"""
    vault = _rev10_vault(tmp_path, raw_url="https://example.com/seed-page?utm=x#top")
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    source = bridge.discover_intent_source(REV10_FRAGMENT_ID)
    assert source["source_seed_url"] == "https://example.com/seed-page", source
    store = bridge.store
    _rev10_seed_offline_lineage(store, bridge)
    search = FakeSearchTransport(DDG_PAGE)
    fetch = _PerUrlFetchTransport(
        {
            "https://example.com/seed-page": RELEASE_BODY,
            "https://example.com/release": RELEASE_BODY,
        }
    )
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=search,
        fetch=fetch,
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 1}
    children = _rev10_children(store)
    assert len(children) == 1
    child_alignment = store.latest(children[0]).eval_results["alignment"]
    child_execution = store.latest(str(child_alignment["execution_run_id"]))
    binding = child_execution.eval_results["execution_binding"]
    assert binding["source_seed_url"] == "https://example.com/seed-page"
    collection = child_execution.eval_results["research_collection"]
    urls = [record["url"] for record in collection["records"]]
    assert "https://example.com/seed-page" in urls, collection["records"]
    assert collection["stop_reason"] == "completed"
    # seed candidate 直达抓取：目标页零搜索即覆盖（DDG 调用计数不增）。
    assert search.calls == [], search.calls
    # 非人工闸：system_policy 路线、认知轴无 awaiting_model_authorization。
    assert child_alignment["decision_source"] == gr.AUTO_DECISION_SOURCE
    progress = child_execution.eval_results.get("research_progress", {})
    assert progress.get("cognitive") != "awaiting_model_authorization"


def test_rev14_seed_fetch_failure_falls_back_to_bounded_search(tmp_path: Path) -> None:
    """rev14 要求 4：seed 抓取失败继续走有界搜索（诚实记录、不伪造）；
    搜索取得证据后不得误报零来源。"""
    from fragment_loop.research_fetch import ResearchFetchError

    vault = _rev10_vault(tmp_path, raw_url="https://example.com/seed-page")
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    _rev10_seed_offline_lineage(store, bridge)

    class FailSeedFetch:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, request):
            locator = str(request["locator"])
            self.calls.append(locator)
            if locator == "https://example.com/seed-page":
                raise ResearchFetchError("research_fetch_unavailable")
            return {
                "http_status": 200,
                "content_type": "text/html; charset=utf-8",
                "body_bytes": RELEASE_BODY,
                "final_locator": locator,
                "peer_ip": "93.184.216.34",
                "resolved_ips": ["93.184.216.34"],
            }

    fetch = FailSeedFetch()
    search = FakeSearchTransport(DDG_PAGE)
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        synthesis_enabled=False,
        search=search,
        fetch=fetch,
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 1}
    children = _rev10_children(store)
    child_alignment = store.latest(children[0]).eval_results["alignment"]
    collection = store.latest(
        str(child_alignment["execution_run_id"])
    ).eval_results["research_collection"]
    # seed 失败后继续有界搜索：搜索真实发生且取得 evidence（非零来源误报）。
    assert search.calls, "seed 失败后必须继续走搜索"
    assert any(
        record.get("marker") == "newly_collected" for record in collection["records"]
    ), collection["records"]
    assert collection["stop_reason"] == "completed"


def test_rev14_fingerprint_change_recovers_once_same_fingerprint_zero_retry(
    tmp_path: Path,
) -> None:
    """rev14 要求 3：历史 capability_unavailable run 仅在 fingerprint
    变化（含旧无 fingerprint）时 append-only 恢复一次；同一 fingerprint
    零重试；重启/重扫幂等，旧 run 不变。"""
    vault = _rev10_vault(tmp_path)

    def make(name: str, *, old_fingerprint: str | None, current_fingerprint: str):
        bridge = _rev10_bridge(vault, tmp_path / f"{name}.sqlite3")
        store = bridge.store
        _rev10_seed_offline_lineage(
            store,
            bridge,
            stop_reason="capability_unavailable",
            search_fingerprint=old_fingerprint,
        )
        runner = make_autonomy_runner(
            store,
            tmp_path / name,
            live=True,
            collection_enabled=True,
            synthesis_enabled=False,
            search=FakeSearchTransport(DDG_PAGE),
            clock=FrozenClock(),
        )
        runner.search_fingerprint = current_fingerprint
        service = _rev10_service(store, bridge, runner)
        return store, service

    # fingerprint 变化（ddg → bing-cn）：恢复一次，重扫/重启幂等。
    store, service = make(
        "changed",
        old_fingerprint="ddg:public-search-v1",
        current_fingerprint="bing-cn:public-search-v1",
    )
    old_sequence = store.latest_sequence("exec:old-1")
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 1}
    assert service.recover_offline_executions() == {"scanned": 1, "recovered": 0}, "重扫幂等"
    assert store.latest_sequence("exec:old-1") == old_sequence, "旧 run 不变"
    # 旧无 fingerprint（未知出口形态 → 当前出口形态）：允许恢复一次。
    store2, service2 = make(
        "missing", old_fingerprint=None, current_fingerprint="bing-cn:public-search-v1"
    )
    assert service2.recover_offline_executions() == {"scanned": 1, "recovered": 1}
    # 同一 fingerprint：零重试。
    store3, service3 = make(
        "same",
        old_fingerprint="ddg:public-search-v1",
        current_fingerprint="ddg:public-search-v1",
    )
    assert service3.recover_offline_executions() == {"scanned": 1, "recovered": 0}
    assert _rev10_children(store3) == [], "同 fingerprint 必须零 child"


def test_rev14_provider_cli_closed_set_rejects_unknown(tmp_path: Path) -> None:
    """rev14 要求 2/5：--research-search-provider 闭集——未知 provider
    在装配前失败关闭。"""
    import pytest as _pytest

    from fragment_loop.cognitive_server import main

    with _pytest.raises(SystemExit):
        main(
            [
                "--graph-db",
                str(tmp_path / "g.sqlite3"),
                "--research-search-provider",
                "evil-search",
            ]
        )


# -- rev15：已消费计数分流恢复、fingerprint 失败关闭、seed 信任边界、Bing 形态 --


def test_rev15_consumed_counters_fingerprint_change_recovers(tmp_path: Path) -> None:
    """rev15 要求 1（真实 Qwen 形态）：capability_unavailable +
    searches=2/fetches=0（出口失败已消费）+ 旧 fingerprint=ddg、当前
    bing-cn → 必须恢复一次（旧 fingerprint-change 逻辑不再被 0/0 预检
    拦截）；旧 run 不变；同 fingerprint 零动作。"""
    vault = _rev10_vault(tmp_path)

    def make(name: str, current: str):
        bridge = _rev10_bridge(vault, tmp_path / f"{name}.sqlite3")
        store = bridge.store
        _rev10_seed_offline_lineage(
            store,
            bridge,
            stop_reason="capability_unavailable",
            searches=2,
            fetches=0,
            search_fingerprint="ddg:public-search-v1",
        )
        runner = make_autonomy_runner(
            store,
            tmp_path / name,
            live=True,
            collection_enabled=True,
            synthesis_enabled=False,
            search=FakeSearchTransport(DDG_PAGE),
            clock=FrozenClock(),
        )
        runner.search_fingerprint = current
        return store, _rev10_service(store, bridge, runner)

    store, service = make("fp-change", "bing-cn:public-search-v1")
    old_sequence = store.latest_sequence("exec:old-1")
    result = service.recover_offline_executions()
    assert result == {"scanned": 1, "recovered": 1}, result
    assert store.latest_sequence("exec:old-1") == old_sequence, "旧 run 不变"
    store2, service2 = make("fp-same", "ddg:public-search-v1")
    assert service2.recover_offline_executions()["recovered"] == 0, (
        "同 fingerprint 必须零动作"
    )
    assert _rev10_children(store2) == []


def test_rev15_live_disabled_still_requires_zero_network(tmp_path: Path) -> None:
    """rev15 要求 1 不退化：live_disabled 的已消费计数（2/0）仍严格
    零动作（能力关闭语义不接受网络事实）。"""
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    _rev10_seed_offline_lineage(store, bridge, stop_reason="live_disabled", searches=2)
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        search=FakeSearchTransport(DDG_PAGE),
        clock=FrozenClock(),
    )
    service = _rev10_service(store, bridge, runner)
    assert service.recover_offline_executions()["recovered"] == 0
    assert _rev10_children(store) == []


def test_rev15_negative_counters_zero_action(tmp_path: Path) -> None:
    """rev15 要求 1：负数 searches/fetches 属畸形，零动作不中断扫描。"""
    vault = _rev10_vault(tmp_path)
    bridge = _rev10_bridge(vault, tmp_path / "loop.sqlite3")
    store = bridge.store
    _rev10_seed_offline_lineage(
        store, bridge, stop_reason="capability_unavailable", searches=-1
    )
    runner = make_autonomy_runner(
        store,
        tmp_path,
        live=True,
        collection_enabled=True,
        search=FakeSearchTransport(DDG_PAGE),
        clock=FrozenClock(),
    )
    runner.search_fingerprint = "bing-cn:public-search-v1"
    service = _rev10_service(store, bridge, runner)
    assert service.recover_offline_executions()["recovered"] == 0


def test_rev15_fingerprint_fail_closed_matrix(tmp_path: Path) -> None:
    """rev15 要求 2：current fingerprint 缺失/空/非字符串时
    capability_unavailable 一律零恢复（None→None 不形成跨 child 无限
    恢复链）；旧 fingerprint 缺失仅在当前合法时允许一次。"""
    vault = _rev10_vault(tmp_path)

    def make(name: str, *, old: str | None, current) -> tuple:
        bridge = _rev10_bridge(vault, tmp_path / f"{name}.sqlite3")
        store = bridge.store
        _rev10_seed_offline_lineage(
            store,
            bridge,
            stop_reason="capability_unavailable",
            search_fingerprint=old,
        )
        runner = make_autonomy_runner(
            store,
            tmp_path / name,
            live=True,
            collection_enabled=True,
            search=FakeSearchTransport(DDG_PAGE),
            clock=FrozenClock(),
        )
        runner.search_fingerprint = current
        return store, _rev10_service(store, bridge, runner)

    # 旧无 fingerprint + 当前合法：允许一次（变化语义）。
    store, service = make("old-missing", old=None, current="bing-cn:public-search-v1")
    assert service.recover_offline_executions()["recovered"] == 1
    # 旧无 fingerprint + 当前缺失/空/非字符串：全部失败关闭。
    for name, current in (
        ("current-none", None),
        ("current-empty", ""),
        ("current-non-str", 42),
    ):
        store_n, service_n = make(name, old=None, current=current)
        assert service_n.recover_offline_executions()["recovered"] == 0, name
        assert _rev10_children(store_n) == [], name
    # 旧 fingerprint 非字符串（畸形）：零动作。
    store_m, service_m = make(
        "old-non-str",
        old="ddg:public-search-v1",
        current="bing-cn:public-search-v1",
    )
    checkpoint = store_m.latest("exec:old-1")
    assert checkpoint is not None
    from dataclasses import replace as dc_replace

    drifted = dc_replace(
        checkpoint,
        eval_results={
            **checkpoint.eval_results,
            "research_collection": {
                **checkpoint.eval_results["research_collection"],
                "search_fingerprint": 42,
            },
        },
    )
    assert store_m.compare_and_append(
        drifted, expected_sequence=store_m.latest_sequence("exec:old-1")
    ) is not None
    assert service_m.recover_offline_executions()["recovered"] == 0


def test_rev15_seed_url_trust_boundary() -> None:
    """rev15 要求 3：_validate_source 的 seed 校验复用 SSRF locator 并
    拒绝 query/fragment；规范 URL 通过。"""
    import pytest as _pytest

    from fragment_loop.intent_service import FragmentIntentError, _validate_source

    base = {
        "title": "t",
        "literal_summary": "s",
        "memory_basis": [],
        "input_digest": "b" * 64,
    }
    accepted = _validate_source(
        {**base, "source_seed_url": "https://example.com/a/b"}, "b" * 64
    )
    assert accepted["source_seed_url"] == "https://example.com/a/b"
    for bad in (
        "https://192.168.1.1/a",
        "https://user:pw@example.com/a",
        "https://example.com/a?x=1",
        "https://example.com/a#frag",
    ):
        with _pytest.raises(FragmentIntentError, match="source_invalid"):
            _validate_source({**base, "source_seed_url": bad}, "b" * 64)


def test_rev15_bing_parser_limit_dedup_and_common_forms() -> None:
    """rev15 要求 4：Bing parser 每查询最多 MAX_RESULTS_PER_QUERY、稳定
    去重、h2 带属性/空白常见形态。"""
    from fragment_loop.research_fetch import (
        MAX_RESULTS_PER_QUERY,
        parse_bing_cn_candidates,
    )

    blocks = []
    for index in range(MAX_RESULTS_PER_QUERY + 2):
        blocks.append(
            f'<li class="b_algo"><h2 class="b_title"> <a href="https://example.com/r{index}" '
            f'target="_blank" h="ID=SERP,{index}">Result {index}</a></h2></li>'
        )
    # 重复候选与 h2 带属性/空白形态混入。
    blocks.append(
        '<li class="b_algo"><h2><a href="https://example.com/r0">Dup Zero</a></h2></li>'
    )
    page = ("<html><body>" + "".join(blocks) + "</body></html>").encode()
    candidates = parse_bing_cn_candidates(page)
    assert len(candidates) == MAX_RESULTS_PER_QUERY, candidates
    urls = [item["url"] for item in candidates]
    assert len(set(urls)) == len(urls), "必须稳定去重"
    assert urls[0] == "https://example.com/r0"
    assert all(item["title"].startswith("Result") for item in candidates)
