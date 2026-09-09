from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.continuation_bridge import FragmentContinuationBridge
from fragment_loop.intent_service import (
    FragmentIntentService,
    ResearchWatchCoordinator,
    propose_organized_vault_intents,
)
from fragment_loop.product_review import ProductReviewService

FRAGMENT_ID = "2026-09-06-19-56-31-242661-desktop"
RAW_REF = f"Notes/散记/碎片想法/{FRAGMENT_ID}.md"
ORGANIZED_REF_SANJI = f"Notes/散记/已整理碎片/{FRAGMENT_ID}.md"


def _raw(*, approved: bool = True, privacy: str | None = None) -> bytes:
    privacy_line = f'privacy_level: "{privacy}"\n' if privacy else ""
    return (
        "---\n"
        'type: "碎片想法"\n'
        'captured_at: "2026-09-06T11:56:31.242Z"\n'
        'pipeline_status: "priority_queued"\n'
        f"nigo-loop: {'true' if approved else 'false'}\n"
        f"{privacy_line}"
        "---\n\n"
        "https://example.com/archify 这个部署以后，能看清我们自己的 Loop Graph 状态吗？\n"
    ).encode()


def _organized(
    *, organized_at: str = "2026-09-06T12:02:04.970Z", status: str = "ready_to_review"
) -> bytes:
    return (
        "---\n"
        'type: "已整理碎片"\n'
        'title: "Archify 能否可视化我们的 Loop Graph 状态？"\n'
        'category: "散记"\n'
        f'source_file: "{FRAGMENT_ID}.md"\n'
        f'organized_at: "{organized_at}"\n'
        f'pipeline_status: "{status}"\n'
        'experiment_status: "ready"\n'
        'evidence_level: "source_only"\n'
        'goal: "判断 Archify 是否能及如何呈现 Loop Graph 运行结构，并决定是否引入。"\n'
        'next_action: "核对仓库与 SKILL 文档的 IR 结构。"\n'
        "---\n\n"
        "# 未验证整理结果\n"
    ).encode()


def _resolver(_source: object) -> dict[str, object]:
    return {
        "suggested_intents": ["verify"],
        "dynamic_intents": [],
        "reasoning": "这条信息包含可核验的外部事实。",
        "plan": "先核验必要事实。",
        "expected_result": "形成可信结论与下一步。",
        "exclusions": [],
        "recommended_route": "verify",
        "execution_scope": {
            "capabilities": [],
            "external_scope": [],
            "model_call_cap": 0,
            "cost_cap_cny": 0,
            "side_effect": "none",
        },
    }


class Env:
    def __init__(self, tmp_path: Path, *, approved: bool = True) -> None:
        self.vault = tmp_path / "vault"
        self.raw_path = self.vault / RAW_REF
        self.organized_path = self.vault / ORGANIZED_REF_SANJI
        candidates = self.vault / "Notes/散记/碎片认知结果"
        assets = self.vault / "Notes/Loop知识资产"
        for path in (self.raw_path.parent, self.organized_path.parent, candidates, assets):
            path.mkdir(parents=True, exist_ok=True)
        self.raw_path.write_bytes(_raw(approved=approved))
        self.organized_path.write_bytes(_organized())
        review = ProductReviewService(candidates, assets, vault_root=self.vault)
        self.bridge = FragmentContinuationBridge(review, loop_db=tmp_path / "loop.sqlite3")
        self.service = FragmentIntentService(
            SQLiteCheckpointStore(tmp_path / "state.sqlite3"),
            self.bridge.load_intent_source,
            _resolver,
        )

    def scan(self) -> dict[str, int]:
        return propose_organized_vault_intents(
            vault_root=self.vault,
            continuation_bridge=self.bridge,
            intent_service=self.service,
        )


def test_ready_approved_fragment_is_auto_proposed_without_ui(tmp_path: Path) -> None:
    env = Env(tmp_path)
    counts = env.scan()
    assert counts["proposed"] == 1
    alignments = env.service.list_alignments()
    assert len(alignments) == 1
    item = alignments[0]
    assert item["fragment_id"] == FRAGMENT_ID
    assert item["status"] == "suggested"
    assert item["nigo_loop"] is True
    # 方向建议由系统生成，不是等待用户先找按钮。
    assert item["recommended_route"] == "verify"


def test_sanji_organized_directory_is_discovered(tmp_path: Path) -> None:
    env = Env(tmp_path)
    source = env.bridge.discover_intent_source(FRAGMENT_ID)
    assert source["input_digest"]
    assert source["organized_at"] == "2026-09-06T12:02:04.970Z"
    assert source["nigo_loop"] is True


def test_not_approved_fragment_is_never_proposed(tmp_path: Path) -> None:
    env = Env(tmp_path, approved=False)
    counts = env.scan()
    assert counts["proposed"] == 0
    assert env.service.list_alignments() == []


def test_unready_organized_waits_without_proposing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.organized_path.write_bytes(_organized(status="organizing"))
    counts = env.scan()
    assert counts["proposed"] == 0
    assert counts["waiting"] == 1
    assert env.service.list_alignments() == []


def test_missing_organized_waits_without_proposing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.organized_path.unlink()
    counts = env.scan()
    assert counts["proposed"] == 0
    assert counts["waiting"] == 1
    assert env.service.list_alignments() == []


def test_historical_fragment_before_cutover_is_deferred(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.organized_path.write_bytes(_organized(organized_at="2026-08-01T12:00:00.000Z"))
    counts = env.scan()
    assert counts["proposed"] == 0
    assert counts["deferred"] == 1
    assert env.service.list_alignments() == []


def test_repeat_and_concurrent_scans_create_one_alignment(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _i: env.scan(), range(4)))
    assert sum(counts["proposed"] for counts in results) <= 4
    assert len(env.service.list_alignments()) == 1
    # 已有 alignment 后，后续扫描零动作。
    again = env.scan()
    assert again["proposed"] == 0
    assert len(env.service.list_alignments()) == 1


def test_scan_once_runs_auto_propose_first_and_stays_fail_closed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    coordinator = ResearchWatchCoordinator(
        env.service,
        auto_propose_scanner=env.scan,
    )
    outcome = coordinator.scan_once()
    assert outcome["auto_proposed"] == 1
    assert outcome["resumed"] >= 1
    assert len(env.service.list_alignments()) == 1

    def broken() -> dict[str, int]:
        raise RuntimeError("boom")

    failing = ResearchWatchCoordinator(env.service, auto_propose_scanner=broken)
    outcome = failing.scan_once()
    # scanner 异常不吞掉既有计数、不抛出。
    assert outcome["scanned"] >= 0
    assert "auto_proposed" not in outcome


def _write_fragment(vault: Path, fragment_id: str, *, approved: bool) -> None:
    raw = vault / "Notes" / "散记" / "碎片想法" / f"{fragment_id}.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(_raw(approved=approved))


def test_scan_window_never_starves_new_fragment(tmp_path: Path) -> None:
    """反例①：截取必须发生在过滤之后——大量不合格旧碎片不得挤占窗口。"""
    env = Env(tmp_path)
    for index in range(120):
        _write_fragment(env.vault, f"2026-07-01-00-00-{index:06d}-oldjunk", approved=False)
    counts = env.scan()
    # 120 条未勾选碎片被过滤；窗口内唯一候选是今天的新碎片，必须被承接。
    assert counts["proposed"] == 1
    assert [item["fragment_id"] for item in env.service.list_alignments()] == [FRAGMENT_ID]


def test_invalid_organized_at_never_passes_cutover(tmp_path: Path) -> None:
    """反例③：非法/无时区/缺失日期一律 fail-closed，且与「历史碎片」分开标记。"""
    for bad in ("not-a-date", "2026-13-45T99:99:99Z", "", "2026-09-07T00:00:00"):
        env = Env(tmp_path / f"case-{len(bad)}-{abs(hash(bad)) % 997}")
        env.organized_path.write_bytes(_organized(organized_at=bad))
        counts = env.scan()
        assert counts["proposed"] == 0, f"非法日期被放行: {bad!r}"
        assert counts["deferred"] == 0, f"非法日期被误标为历史碎片: {bad!r}"
        assert counts["indeterminate"] == 1, f"非法日期未计入 indeterminate: {bad!r}"
        (entry,) = env.service.last_auto_propose_report
        assert entry["outcome"] == "indeterminate"
        assert entry["reason"] == "organized_at_missing_or_invalid"
        assert env.service.list_alignments() == []
    env = Env(tmp_path / "case-valid")
    env.organized_path.write_bytes(_organized(organized_at="2026-09-06T12:00:00.000Z"))
    assert env.scan()["proposed"] == 1


def test_rotating_window_covers_waiting_backlog_within_two_rounds(tmp_path: Path) -> None:
    """复核反例①：100 条较新的 waiting 碎片 + 1 条较旧的合格碎片——
    等待/失败/历史不得永久占满窗口，第二轮必须轮到合格碎片。"""
    env = Env(tmp_path)
    for index in range(100):
        newer_id = f"2026-09-06-21-{index:02d}-00-{index:06x}-desktop"
        _write_fragment(env.vault, newer_id, approved=True)
    first = env.scan()
    assert first["proposed"] == 0
    assert first["waiting"] == 100
    second = env.scan()
    # 轮转游标推进：合格碎片（最旧，排在候选末尾）第二轮进入窗口。
    assert second["proposed"] == 1
    assert [item["fragment_id"] for item in env.service.list_alignments()] == [FRAGMENT_ID]
    report = {entry["fragment_id"]: entry for entry in env.service.last_auto_propose_report}
    assert report[FRAGMENT_ID]["outcome"] == "proposed"


def test_skipped_fragments_are_reported_not_silent(tmp_path: Path) -> None:
    """复核②：未勾选/隐私排除必须逐条可见，与「无记录」是不同事实。"""
    env = Env(tmp_path)
    not_approved_id = "2026-09-06-20-02-00-bbbbbb-desktop"
    privacy_id = "2026-09-06-20-03-00-cccccc-desktop"
    _write_fragment(env.vault, not_approved_id, approved=False)
    raw_privacy = env.vault / "Notes/散记/碎片想法" / f"{privacy_id}.md"
    raw_privacy.write_bytes(_raw(approved=True, privacy="sensitive"))
    counts = env.scan()
    assert counts["proposed"] == 1  # 只有本合格碎片被承接
    report = {entry["fragment_id"]: entry for entry in env.service.last_auto_propose_report}
    assert report[not_approved_id] == {
        "fragment_id": not_approved_id,
        "outcome": "skipped",
        "reason": "not_approved",
    }
    assert report[privacy_id] == {
        "fragment_id": privacy_id,
        "outcome": "skipped",
        "reason": "privacy_excluded",
    }


def test_per_fragment_report_exposes_waiting_failed_and_proposed(tmp_path: Path) -> None:
    """反例②：逐条结果必须可查——等待/失败/已承接各有 outcome 与原因。"""
    env = Env(tmp_path)
    waiting_id = "2026-09-06-20-01-00-aaaaaa-desktop"
    _write_fragment(env.vault, waiting_id, approved=True)
    counts = env.scan()
    assert counts["proposed"] == 1
    assert counts["waiting"] == 1
    report = {entry["fragment_id"]: entry for entry in env.service.last_auto_propose_report}
    assert report[FRAGMENT_ID]["outcome"] == "proposed"
    assert report[waiting_id]["outcome"] == "waiting"
    assert report[waiting_id]["reason"]


def test_alignments_get_exposes_auto_propose_report_in_meta(tmp_path: Path) -> None:
    """反例②：GET alignments 的 envelope meta 透传逐条承接结果。"""
    from http.client import HTTPConnection
    from threading import Thread

    from fragment_loop.cognitive_server import make_cognitive_decision_server

    env = Env(tmp_path)
    env.scan()
    server = make_cognitive_decision_server(
        None, port=0, require_trusted_origin=True, intent_service=env.service
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "GET",
            "/fragment/v1/alignments",
            headers={"Host": f"127.0.0.1:{port}", "Origin": "app://obsidian.md"},
        )
        import json as _json

        payload = _json.loads(connection.getresponse().read().decode())
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    report = payload["meta"]["auto_propose"]
    assert isinstance(report, list) and len(report) == 1
    assert report[0]["fragment_id"] == FRAGMENT_ID
    assert report[0]["outcome"] == "proposed"
