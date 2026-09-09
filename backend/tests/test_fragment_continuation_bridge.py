from __future__ import annotations

import hashlib
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from fragment_loop.cognitive_server import make_cognitive_decision_server
from fragment_loop.continuation_bridge import (
    CONTINUATION_HEADER,
    ContinuationBridgeError,
    FragmentContinuationBridge,
)
from fragment_loop.product_review import ProductReviewService
from fragment_loop.runtime import register_cognitive_intake

FRAGMENT_ID = "2026-08-08-19-41-45-1c8e88-desktop"
RAW_REF = f"Notes/散记/碎片想法/{FRAGMENT_ID}.md"
ORGANIZED_REF = f"Notes/AI创业/碎片整理/{FRAGMENT_ID}.md"
RAW_BODY = (
    "minimax H3权重已经开源了，我想要研究下是否能够在我的本地部署成功并能够跑起来，"
    "先研究下给我一个本地是否能跑的方案。"
)


def _raw(*, approved: bool = True) -> bytes:
    return (
        "---\n"
        'type: "碎片想法"\n'
        'captured_at: "2026-08-08T11:41:45.823Z"\n'
        'pipeline_status: "priority_queued"\n'
        f"nigo-loop: {'true' if approved else 'false'}\n"
        "---\n\n"
        f"{RAW_BODY}\n"
    ).encode()


def _organized(*, source_file: str | None = None, status: str = "ready_to_review") -> bytes:
    return (
        "---\n"
        'type: "已整理碎片"\n'
        'title: "Minimax H3权重本地部署可行性调研"\n'
        'category: "AI创业"\n'
        f'source_file: "{source_file or f"{FRAGMENT_ID}.md"}"\n'
        f'pipeline_status: "{status}"\n'
        'experiment_status: "ready"\n'
        'evidence_level: "source_only"\n'
        'goal: "确认权重能否在本地部署并成功运行，产出可执行方案。"\n'
        'next_action: "搜索官方仓库与技术博客，核验权重、许可证和硬件要求。"\n'
        "---\n\n"
        "# 未验证整理结果\n"
    ).encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.vault = tmp_path / "vault"
        self.raw_path = self.vault / RAW_REF
        self.organized_path = self.vault / ORGANIZED_REF
        self.candidates = self.vault / "Notes/散记/碎片认知结果"
        self.assets = self.vault / "Notes/Loop知识资产"
        for path in (
            self.raw_path.parent,
            self.organized_path.parent,
            self.candidates,
            self.assets,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.raw_path.write_bytes(_raw())
        self.organized_path.write_bytes(_organized())
        self.loop_db = tmp_path / "loop.sqlite3"
        register_cognitive_intake(self.raw_path, db_path=self.loop_db)
        self.review = ProductReviewService(
            self.candidates, self.assets, vault_root=self.vault
        )
        self.bridge = FragmentContinuationBridge(self.review, loop_db=self.loop_db)

    def body(self) -> dict[str, str]:
        raw = self.raw_path.read_bytes()
        organized = self.organized_path.read_bytes()
        return {
            "fragment_id": FRAGMENT_ID,
            "raw_ref": RAW_REF,
            "organized_ref": ORGANIZED_REF,
            "raw_sha256": _sha(raw),
            "organized_sha256": _sha(organized),
            "route": "research",
            "requester": "nigo",
        }


def test_continuation_advances_existing_loop_and_publishes_existing_candidate_format(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    status, outcome = env.bridge.continue_fragment(env.body())
    assert status == 201
    assert outcome["content_status"] == "pending_confirmation"
    assert outcome["evidence_level"] == "unverified"
    assert outcome["has_conflict"] is False
    assert outcome["fragment_ref"] == f"fragments/{FRAGMENT_ID}.md"
    assert outcome["candidate_card_count"] == 1
    assert outcome["continuation_status"] == "published"

    checkpoint = env.bridge.store.latest_for_fragment(
        "fragment-cognitive-local-v1", FRAGMENT_ID
    )
    assert checkpoint is not None
    assert checkpoint.status == "paused"
    assert checkpoint.current_node == "human_decision"
    assert checkpoint.eval_results["cognitive_contract_status"] == "validated"
    result = checkpoint.eval_results["cognitive_result"]
    assert result["research"]["sources"] == []
    assert result["research"]["v1_claims"][0]["verdict"] == "not_covered"

    detail = env.review.detail(str(outcome["candidate_id"]))
    assert detail["title"] == "Minimax H3权重本地部署可行性调研"
    assert detail["candidate_cards"][0]["evidence_level"] == "unverified"
    serialized = json.dumps(detail, ensure_ascii=False)
    assert RAW_BODY not in serialized
    assert "run_id" not in serialized


def test_continuation_is_idempotent_and_concurrent_single_winner(tmp_path: Path) -> None:
    env = Env(tmp_path)
    body = env.body()
    barrier = threading.Barrier(2)

    def invoke() -> tuple[int, dict[str, Any]]:
        barrier.wait()
        return env.bridge.continue_fragment(body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: invoke(), range(2)))
    assert sorted(status for status, _payload in outcomes) == [200, 201]
    assert len({payload["candidate_id"] for _status, payload in outcomes}) == 1
    candidate_dirs = [path for path in env.candidates.iterdir() if path.is_dir()]
    assert len(candidate_dirs) == 1
    assert len(list(candidate_dirs[0].glob("fragment-product-*.json"))) == 1


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("mutation", "code"),
    [
        ("digest", "source_changed"),
        ("source_file", "organized_not_ready"),
        ("status", "organized_not_ready"),
        ("route", "route_not_allowed"),
    ],
)
def test_continuation_fails_closed_before_candidate(
    tmp_path: Path, mutation: str, code: str
) -> None:
    env = Env(tmp_path)
    body = env.body()
    if mutation == "digest":
        body["organized_sha256"] = "0" * 64
    elif mutation == "source_file":
        env.organized_path.write_bytes(_organized(source_file="other.md"))
        body["organized_sha256"] = _sha(env.organized_path.read_bytes())
    elif mutation == "status":
        env.organized_path.write_bytes(_organized(status="needs_retry"))
        body["organized_sha256"] = _sha(env.organized_path.read_bytes())
    else:
        body["route"] = "direct"
    with pytest.raises(ContinuationBridgeError, match=code):
        env.bridge.continue_fragment(body)
    assert list(env.candidates.iterdir()) == []


def test_current_approval_is_revalidated(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.raw_path.write_bytes(_raw(approved=False))
    body = env.body()
    with pytest.raises(ContinuationBridgeError, match="fragment_not_approved"):
        env.bridge.continue_fragment(body)
    assert list(env.candidates.iterdir()) == []


def test_intent_source_is_exactly_bound_to_the_two_trusted_notes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    source = env.bridge.discover_intent_source(FRAGMENT_ID)
    expected = hashlib.sha256(
        json.dumps(
            {
                "raw_text": env.raw_path.read_text(encoding="utf-8"),
                "organized_text": env.organized_path.read_text(encoding="utf-8"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert source == {
        "goal": "确认权重能否在本地部署并成功运行，产出可执行方案。",
        "title": "Minimax H3权重本地部署可行性调研",
        "literal_summary": (
            "目标：确认权重能否在本地部署并成功运行，产出可执行方案。；"
            "下一步：搜索官方仓库与技术博客，核验权重、许可证和硬件要求。"
        ),
        "memory_basis": [],
        "input_digest": expected,
        # rev2 P1-4：bridge 已真实校验 frontmatter nigo-loop: true，
        # 资格事实显式传递给 alignment 契约。
        "nigo_loop": True,
        # 自动承接 cutover 判断用；夹具 frontmatter 无 organized_at 时为空串。
        "organized_at": "",
    }
    assert env.bridge.load_intent_source(FRAGMENT_ID, expected) == source
    with pytest.raises(ContinuationBridgeError, match="source_changed"):
        env.bridge.load_intent_source(FRAGMENT_ID, "0" * 64)


def test_intent_source_rejects_ambiguous_organized_note(tmp_path: Path) -> None:
    env = Env(tmp_path)
    duplicate = (
        env.vault
        / "Notes"
        / "散记"
        / "碎片整理"
        / f"{FRAGMENT_ID}.md"
    )
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(_organized())
    with pytest.raises(ContinuationBridgeError, match="source_mismatch"):
        env.bridge.discover_intent_source(FRAGMENT_ID)


def test_path_traversal_and_symlink_sources_fail_before_loop_or_candidate(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    original = env.bridge.store.latest_sequence(
        env.bridge.store.run_ids_with_prefix("fragment-cognitive-local-v1")[0]
    )
    traversal = env.body()
    traversal["raw_ref"] = f"Notes/散记/碎片想法/../{FRAGMENT_ID}.md"
    with pytest.raises(ContinuationBridgeError, match="invalid_body"):
        env.bridge.continue_fragment(traversal)

    outside = tmp_path / "outside.md"
    outside.write_bytes(_raw())
    env.raw_path.unlink()
    env.raw_path.symlink_to(outside)
    linked = env.body()
    with pytest.raises(ContinuationBridgeError, match="source_unsafe"):
        env.bridge.continue_fragment(linked)
    assert list(env.candidates.iterdir()) == []
    assert env.bridge.store.latest_sequence(
        env.bridge.store.run_ids_with_prefix("fragment-cognitive-local-v1")[0]
    ) == original


def test_status_projection_contains_no_body_path_or_run_id(tmp_path: Path) -> None:
    env = Env(tmp_path)
    before = env.bridge.list_statuses()
    assert before == [
        {
            "fragment_id": FRAGMENT_ID,
            "status": "approved",
            "current_node": "cognitive_contract",
            "sequence": 1,
            "stage": "loop_registered",
            "updated_at": before[0]["updated_at"],
        }
    ]
    env.bridge.continue_fragment(env.body())
    after = env.bridge.list_statuses()
    assert after[0]["stage"] == "candidate_source_ready"
    serialized = json.dumps(after, ensure_ascii=False)
    assert RAW_BODY not in serialized
    assert "run_id" not in serialized
    assert str(env.vault) not in serialized


def _request(
    server: Any,
    method: str,
    path: str,
    *,
    body: dict[str, str] | None = None,
    header: bool = True,
) -> tuple[int, dict[str, Any]]:
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Origin": "app://obsidian.md"}
    payload = None
    if body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
        if header:
            headers[CONTINUATION_HEADER] = "1"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    decoded = json.loads(response.read().decode())
    connection.close()
    return response.status, decoded


def test_http_route_get_post_and_header_guard(tmp_path: Path) -> None:
    env = Env(tmp_path)
    server = make_cognitive_decision_server(
        None, port=0, continuation_bridge=env.bridge
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, envelope = _request(server, "GET", "/fragment/v1/continuations")
        assert status == 200
        assert envelope["data"][0]["stage"] == "loop_registered"

        status, envelope = _request(
            server,
            "POST",
            "/fragment/v1/continuations",
            body=env.body(),
            header=False,
        )
        assert status == 403
        assert envelope["error"]["code"] == "missing_fragment_continuation_header"

        status, envelope = _request(
            server, "POST", "/fragment/v1/continuations", body=env.body()
        )
        assert status == 201
        assert envelope["data"]["content_status"] == "pending_confirmation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
