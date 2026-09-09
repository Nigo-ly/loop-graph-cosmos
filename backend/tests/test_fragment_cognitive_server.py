"""R1-G loopback decision adapter tests (synthetic data only)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.cognitive_loop import SyntheticCognitiveLoop
from fragment_loop.cognitive_note import (
    CognitiveNoteWithdrawalOutcome,
    publish_kept_cognitive_note,
    withdraw_kept_cognitive_note,
)
from fragment_loop.cognitive_server import (
    decision_idempotency_key,
    make_cognitive_decision_server,
    withdrawal_idempotency_key,
)

FRAGMENT = "周五下午整理书桌。"


def direct_result() -> dict[str, object]:
    return {
        "route": "direct",
        "route_reason": "简单事务无需外部研究",
        "route_change_allowed": True,
        "summary": {
            "text": FRAGMENT,
            "source_quote": FRAGMENT,
            "transformation_basis": "摘要与原文一致",
        },
        "semantic_expansion": {
            "literal_facts": [
                {"text": "周五下午", "source_quote": "周五下午"},
                {"text": "整理书桌", "source_quote": "整理书桌"},
            ],
            "inferences": [],
            "uncertainties": [],
        },
        "perspectives": [
            {
                "perspective": "memory",
                "summary": "没有相关合成材料",
                "association": "无已知关联",
                "conflicts": [],
                "value": "按原文保留",
                "uncertainty": "没有合成上下文",
                "materials": [],
            }
        ],
        "synthesis": {
            "value": "简单事务提醒",
            "weakest_link": "只依据原文",
            "conflicts": [],
            "open_questions": [],
            "next_step": "等待人工选择",
        },
    }


def waiting_service(tmp_path: Path) -> tuple[SyntheticCognitiveLoop, dict[str, Any]]:
    service = SyntheticCognitiveLoop(
        SQLiteCheckpointStore(tmp_path / "loop.sqlite3"),
        lambda _text, _route: direct_result(),
    )
    registered = service.register(
        fragment_id="synthetic-api-decision-1",
        fragment_text=FRAGMENT,
        route="direct",
    )
    waiting = service.run(registered.run_id)
    sequence = service.store.latest_sequence(waiting.run_id)
    markdown = waiting.eval_results["cognitive_markdown"]
    assert sequence is not None and isinstance(markdown, str)
    body: dict[str, Any] = {
        "decision": "keep_draft",
        "run_id": waiting.run_id,
        "fragment_id": waiting.fragment_id,
        "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
        "expected_sequence": sequence,
        "requester": "nigo",
        "thought_category": "行业研究",
    }
    body["idempotency_key"] = decision_idempotency_key(body)
    return service, body


def request(
    port: int,
    body: dict[str, Any],
    *,
    origin: str | None = "app://obsidian.md",
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "X-Fragment-Cognitive-Decision": "1",
    }
    if origin is not None:
        headers["Origin"] = origin
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(
        "POST",
        "/fragment-cognitive/v1/decisions",
        body=json.dumps(body),
        headers=headers,
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def test_decision_idempotency_key_matches_the_console_fixed_vector() -> None:
    body = {
        "requester": "nigo",
        "decision": "keep_draft",
        "run_id": "run-fixed",
        "fragment_id": "fragment-fixed",
        "markdown_sha256": "a" * 64,
        "expected_sequence": 7,
        "thought_category": "行业研究",
    }

    assert decision_idempotency_key(body) == (
        "49f917a03ba5fdc678e5c46003a3045a767fee6faa5729d22180feed120881ab"
    )


def test_changing_the_category_produces_a_different_idempotency_key() -> None:
    body = {
        "requester": "nigo",
        "decision": "keep_draft",
        "run_id": "run-fixed",
        "fragment_id": "fragment-fixed",
        "markdown_sha256": "a" * 64,
        "expected_sequence": 7,
        "thought_category": "行业研究",
    }

    changed = decision_idempotency_key({**body, "thought_category": "生活整理"})

    assert changed == (
        "ea6c701afad4003ae278a4864f536d8c612be215328f0040e05af2e7b90633bf"
    )
    assert changed != decision_idempotency_key(body)


@pytest.mark.parametrize("decision", ["keep_draft", "reject"])  # type: ignore[untyped-decorator]
def test_loopback_api_records_the_exact_display_binding_idempotently(
    tmp_path: Path, decision: str
) -> None:
    service, body = waiting_service(tmp_path)
    body["decision"] = decision
    body["thought_category"] = "行业研究" if decision == "keep_draft" else ""
    body["idempotency_key"] = decision_idempotency_key(body)
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        first_status, first = request(port, body)
        history_size = len(service.store.history(str(body["run_id"])))
        second_status, second = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert first_status == second_status == 202
    assert first["contract_version"] == second["contract_version"] == "2"
    assert first["data"] == second["data"]
    expected_data: dict[str, Any] = {
        "decision": decision,
        "decision_id": body["idempotency_key"],
        "status": "recorded",
    }
    if decision == "keep_draft":
        expected_data["thought_category"] = "行业研究"
    assert first["data"] == expected_data
    assert len(service.store.history(str(body["run_id"]))) == history_size


def test_keep_draft_decision_can_publish_the_local_note_in_the_same_flow(
    tmp_path: Path,
) -> None:
    service, body = waiting_service(tmp_path)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    published_paths: list[str] = []

    def publish(run_id: str) -> None:
        outcome = publish_kept_cognitive_note(service.store, run_id, notes_dir)
        published_paths.append(outcome.path)

    server = make_cognitive_decision_server(service, port=0, note_publisher=publish)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 202
    assert response["data"]["note_status"] == "published"
    assert response["data"]["thought_category"] == "行业研究"
    assert len(published_paths) == 1
    assert Path(published_paths[0]).is_file()
    note_text = Path(published_paths[0]).read_text(encoding="utf-8")
    assert 'type: "碎片认知结果"' in note_text
    assert 'thought_category: "行业研究"' in note_text
    final = service.store.latest(str(body["run_id"]))
    assert final is not None
    receipt = final.eval_results["cognitive_decision_receipt"]
    assert isinstance(receipt, dict)
    assert receipt["thought_category"] == "行业研究"
    assert receipt["decision_id"] == body["idempotency_key"]


def test_the_trimmed_category_is_bound_into_the_receipt_and_note(
    tmp_path: Path,
) -> None:
    service, body = waiting_service(tmp_path)
    body["thought_category"] = "  行业研究  "
    body["idempotency_key"] = decision_idempotency_key(
        {**body, "thought_category": "行业研究"}
    )
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    received: list[str] = []

    def publish(run_id: str) -> None:
        received.append(run_id)
        publish_kept_cognitive_note(service.store, run_id, notes_dir)

    server = make_cognitive_decision_server(service, port=0, note_publisher=publish)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 202
    assert received == [str(body["run_id"])]
    assert response["data"]["thought_category"] == "行业研究"
    notes = list(notes_dir.glob("*.md"))
    assert len(notes) == 1
    assert 'thought_category: "行业研究"' in notes[0].read_text(encoding="utf-8")
    final = service.store.latest(str(body["run_id"]))
    assert final is not None
    receipt = final.eval_results["cognitive_decision_receipt"]
    assert isinstance(receipt, dict)
    assert receipt["thought_category"] == "行业研究"


def test_note_publication_failure_is_recoverable_without_redeciding(
    tmp_path: Path,
) -> None:
    service, body = waiting_service(tmp_path)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    attempts = 0

    def publish(run_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic publication interruption")
        publish_kept_cognitive_note(service.store, run_id, notes_dir)

    server = make_cognitive_decision_server(service, port=0, note_publisher=publish)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        first_status, first = request(port, body)
        history_size = len(service.store.history(str(body["run_id"])))
        second_status, second = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert first_status == 500
    assert first["error"]["code"] == "internal_error"
    assert second_status == 202
    assert second["data"]["note_status"] == "published"
    assert attempts == 2
    assert len(service.store.history(str(body["run_id"]))) == history_size
    recovered_notes = list(notes_dir.glob("*.md"))
    assert len(recovered_notes) == 1
    assert 'thought_category: "行业研究"' in recovered_notes[0].read_text(
        encoding="utf-8"
    )


def test_loopback_api_rejects_untrusted_stale_or_ambiguous_requests_without_writes(
    tmp_path: Path,
) -> None:
    service, body = waiting_service(tmp_path)
    history_before = service.store.history(str(body["run_id"]))
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        cases = (
            (body, None, 403, "forbidden_origin"),
            (
                {**body, "idempotency_key": "0" * 64},
                "app://obsidian.md",
                400,
                "invalid_idempotency_key",
            ),
            (
                {
                    **body,
                    "run_id": f"{body['run_id']}\x1fambiguous",
                    "idempotency_key": decision_idempotency_key(
                        {**body, "run_id": f"{body['run_id']}\x1fambiguous"}
                    ),
                },
                "app://obsidian.md",
                400,
                "invalid_run_id",
            ),
            (
                {
                    **body,
                    "expected_sequence": int(body["expected_sequence"]) - 1,
                    "idempotency_key": decision_idempotency_key(
                        {
                            **body,
                            "expected_sequence": int(body["expected_sequence"]) - 1,
                        }
                    ),
                },
                "app://obsidian.md",
                409,
                "decision_conflict",
            ),
        )
        for request_body, origin, expected_status, expected_code in cases:
            status, response = request(port, request_body, origin=origin)
            assert status == expected_status
            assert response["error"]["code"] == expected_code
            assert service.store.history(str(body["run_id"])) == history_before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "thought_category",
    [
        "",
        "   ",
        "行业\n研究",
        "行业\r研究",
        "行业\x1f研究",
        "\n行业研究",
        "行业研究\n",
        "\r行业研究",
        "行业研究\r",
        "a" * 129,
    ],
)
def test_keep_draft_with_an_invalid_category_is_blocked_before_any_write(
    tmp_path: Path, thought_category: str
) -> None:
    service, body = waiting_service(tmp_path)
    body["thought_category"] = thought_category
    body["idempotency_key"] = decision_idempotency_key(body)
    history_before = service.store.history(str(body["run_id"]))
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 400
    assert response["error"]["code"] == "invalid_thought_category"
    assert service.store.history(str(body["run_id"])) == history_before


def test_reject_only_accepts_an_empty_category(tmp_path: Path) -> None:
    service, body = waiting_service(tmp_path)
    body["decision"] = "reject"
    body["thought_category"] = "行业研究"
    body["idempotency_key"] = decision_idempotency_key(body)
    history_before = service.store.history(str(body["run_id"]))
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 400
    assert response["error"]["code"] == "invalid_thought_category"
    assert service.store.history(str(body["run_id"])) == history_before


def test_a_missing_thought_category_key_is_an_invalid_body(tmp_path: Path) -> None:
    service, body = waiting_service(tmp_path)
    del body["thought_category"]
    body["idempotency_key"] = "0" * 64
    history_before = service.store.history(str(body["run_id"]))
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 400
    assert response["error"]["code"] == "invalid_body"
    assert service.store.history(str(body["run_id"])) == history_before


def test_reject_with_a_configured_publisher_writes_no_note_or_category(
    tmp_path: Path,
) -> None:
    service, body = waiting_service(tmp_path)
    body["decision"] = "reject"
    body["thought_category"] = ""
    body["idempotency_key"] = decision_idempotency_key(body)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    published: list[str] = []

    def publish(run_id: str) -> None:
        published.append(run_id)

    server = make_cognitive_decision_server(service, port=0, note_publisher=publish)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = request(port, body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 202
    assert response["data"] == {
        "decision": "reject",
        "decision_id": body["idempotency_key"],
        "status": "recorded",
    }
    assert published == []
    assert list(notes_dir.iterdir()) == []
    final = service.store.latest(str(body["run_id"]))
    assert final is not None
    receipt = final.eval_results["cognitive_decision_receipt"]
    assert isinstance(receipt, dict)
    assert "thought_category" not in receipt


# ---------------------------------------------------------------------------
# R1-OB: fixed POST /fragment-cognitive/v1/withdrawals resource.
# ---------------------------------------------------------------------------


def kept_service(tmp_path: Path) -> tuple[SyntheticCognitiveLoop, Path]:
    service, body = waiting_service(tmp_path)
    service.decide_bound(
        str(body["run_id"]),
        "keep_draft",
        fragment_id=str(body["fragment_id"]),
        expected_sequence=int(body["expected_sequence"]),
        markdown_sha256=str(body["markdown_sha256"]),
        decision_id=str(body["idempotency_key"]),
        thought_category=str(body["thought_category"]),
    )
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    return service, notes_dir


def withdrawal_body(service: SyntheticCognitiveLoop, run_id: str) -> dict[str, Any]:
    latest = service.store.latest(run_id)
    sequence = service.store.latest_sequence(run_id)
    assert latest is not None and sequence is not None
    body: dict[str, Any] = {
        "action": "withdraw",
        "run_id": run_id,
        "fragment_id": latest.fragment_id,
        "expected_sequence": sequence,
        "requester": "nigo",
    }
    body["withdrawal_id"] = withdrawal_idempotency_key(body)
    return body


def withdraw_request(
    port: int,
    body: dict[str, Any],
    *,
    origin: str | None = "app://obsidian.md",
    header: str = "X-Fragment-Cognitive-Withdrawal",
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        header: "1",
    }
    if origin is not None:
        headers["Origin"] = origin
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(
        "POST",
        "/fragment-cognitive/v1/withdrawals",
        body=json.dumps(body),
        headers=headers,
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def make_withdrawer(
    service: SyntheticCognitiveLoop, notes_dir: Path
) -> Callable[[str, str, int, str], CognitiveNoteWithdrawalOutcome]:
    def withdraw(
        run_id: str, fragment_id: str, expected_sequence: int, withdrawal_id: str
    ) -> CognitiveNoteWithdrawalOutcome:
        return withdraw_kept_cognitive_note(
            service.store,
            run_id,
            notes_dir,
            fragment_id=fragment_id,
            expected_sequence=expected_sequence,
            withdrawal_id=withdrawal_id,
        )

    return withdraw


def serve(
    service: SyntheticCognitiveLoop, notes_dir: Path
) -> tuple[ThreadingHTTPServer, Thread, int]:
    server = make_cognitive_decision_server(
        service, port=0, note_withdrawer=make_withdrawer(service, notes_dir)
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, int(server.server_address[1])


def stop(server: ThreadingHTTPServer, thread: Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_withdrawal_idempotency_key_matches_the_fixed_vector() -> None:
    body = {
        "requester": "nigo",
        "action": "withdraw",
        "run_id": "run-fixed",
        "fragment_id": "fragment-fixed",
        "expected_sequence": 7,
    }

    assert withdrawal_idempotency_key(body) == (
        "902f072890921190820b187ff18e2a3099ed78fd1b61b55b314ef43ff3c0b7ec"
    )


def test_withdrawal_api_withdraws_a_published_note_idempotently(
    tmp_path: Path,
) -> None:
    service, notes_dir = kept_service(tmp_path)
    run_id = service.store.latest_for_fragment(
        "fragment-cognitive-synthetic-r1b", "synthetic-api-decision-1"
    ).run_id  # type: ignore[union-attr]
    publish_kept_cognitive_note(service.store, run_id, notes_dir)
    body = withdrawal_body(service, run_id)
    server, thread, port = serve(service, notes_dir)
    try:
        first_status, first = withdraw_request(port, body)
        history_size = len(service.store.history(run_id))
        second_status, second = withdraw_request(port, body)
    finally:
        stop(server, thread)

    assert first_status == second_status == 202
    assert first["data"] == {
        "action": "withdraw",
        "withdrawal_id": body["withdrawal_id"],
        "status": "withdrawn",
        "note_status": "withdrawn",
        "idempotent": False,
    }
    assert second["data"] == {**first["data"], "idempotent": True}
    assert len(service.store.history(run_id)) == history_size
    final = service.store.latest(run_id)
    assert final is not None
    assert final.eval_results["content_lifecycle"] == "withdrawn"
    assert final.eval_results["evidence_level"] == "unverified"
    notes = list(notes_dir.glob("*.md"))
    assert len(notes) == 1
    note_text = notes[0].read_text(encoding="utf-8")
    assert 'content_lifecycle: "withdrawn"' in note_text
    assert "withdrawn_at:" in note_text


def test_withdrawal_api_reports_an_absent_note_honestly(tmp_path: Path) -> None:
    service, notes_dir = kept_service(tmp_path)
    run_id = service.store.latest_for_fragment(
        "fragment-cognitive-synthetic-r1b", "synthetic-api-decision-1"
    ).run_id  # type: ignore[union-attr]
    body = withdrawal_body(service, run_id)
    server, thread, port = serve(service, notes_dir)
    try:
        status, response = withdraw_request(port, body)
    finally:
        stop(server, thread)

    assert status == 202
    assert response["data"]["status"] == "withdrawn"
    assert response["data"]["note_status"] == "absent"
    assert response["data"]["idempotent"] is False
    assert list(notes_dir.iterdir()) == []


def test_withdrawal_api_rejects_invalid_bodies_before_any_write(
    tmp_path: Path,
) -> None:
    service, notes_dir = kept_service(tmp_path)
    run_id = service.store.latest_for_fragment(
        "fragment-cognitive-synthetic-r1b", "synthetic-api-decision-1"
    ).run_id  # type: ignore[union-attr]
    body = withdrawal_body(service, run_id)
    history_before = service.store.history(run_id)
    server, thread, port = serve(service, notes_dir)
    changed_key = withdrawal_idempotency_key({**body, "expected_sequence": 900})
    withdraw_header = "X-Fragment-Cognitive-Withdrawal"
    trusted = "app://obsidian.md"
    missing_fragment = {
        key: value for key, value in body.items() if key != "fragment_id"
    }
    cases: tuple[tuple[dict[str, Any], str | None, str, int, str], ...] = (
        ({**body, "action": "keep"}, trusted, withdraw_header, 400, "invalid_action"),
        (
            {**body, "requester": "mallory"},
            trusted,
            withdraw_header,
            400,
            "invalid_requester",
        ),
        ({**body, "extra": "field"}, trusted, withdraw_header, 400, "invalid_body"),
        (missing_fragment, trusted, withdraw_header, 400, "invalid_body"),
        (
            {**body, "withdrawal_id": "0" * 64},
            trusted,
            withdraw_header,
            400,
            "invalid_withdrawal_id",
        ),
        (
            {**body, "expected_sequence": 0},
            trusted,
            withdraw_header,
            400,
            "invalid_expected_sequence",
        ),
        (
            {**body, "expected_sequence": 900, "withdrawal_id": changed_key},
            trusted,
            withdraw_header,
            409,
            "withdrawal_conflict",
        ),
        (body, None, withdraw_header, 403, "forbidden_origin"),
        (body, "https://evil.example", withdraw_header, 403, "forbidden_origin"),
        (
            body,
            trusted,
            "X-Fragment-Cognitive-Decision",
            403,
            "missing_withdrawal_header",
        ),
    )
    try:
        for request_body, origin, header, expected_status, expected_code in cases:
            status, response = withdraw_request(
                port, request_body, origin=origin, header=header
            )
            assert status == expected_status, (expected_code, response)
            assert response["error"]["code"] == expected_code
            assert service.store.history(run_id) == history_before
    finally:
        stop(server, thread)
    assert list(notes_dir.iterdir()) == []


def test_withdrawal_api_unknown_run_is_a_fixed_404(tmp_path: Path) -> None:
    service, notes_dir = kept_service(tmp_path)
    body = {
        "action": "withdraw",
        "run_id": "cognitive-r1b-" + "0" * 64,
        "fragment_id": "synthetic-api-decision-1",
        "expected_sequence": 1,
        "requester": "nigo",
    }
    body["withdrawal_id"] = withdrawal_idempotency_key(body)
    server, thread, port = serve(service, notes_dir)
    try:
        status, response = withdraw_request(port, body)
    finally:
        stop(server, thread)

    assert status == 404
    assert response["error"]["code"] == "run_not_found"


def test_withdrawal_api_without_a_configured_withdrawer_is_unavailable(
    tmp_path: Path,
) -> None:
    service, notes_dir = kept_service(tmp_path)
    run_id = service.store.latest_for_fragment(
        "fragment-cognitive-synthetic-r1b", "synthetic-api-decision-1"
    ).run_id  # type: ignore[union-attr]
    body = withdrawal_body(service, run_id)
    history_before = service.store.history(run_id)
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = withdraw_request(port, body)
    finally:
        stop(server, thread)

    assert status == 403
    assert response["error"]["code"] == "withdrawal_unavailable"
    assert service.store.history(run_id) == history_before
    assert list(notes_dir.iterdir()) == []


def test_decisions_endpoint_still_requires_its_own_header(tmp_path: Path) -> None:
    service, body = waiting_service(tmp_path)
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        headers = {
            "Content-Type": "application/json",
            "X-Fragment-Cognitive-Withdrawal": "1",
            "Origin": "app://obsidian.md",
        }
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "POST",
            "/fragment-cognitive/v1/decisions",
            body=json.dumps(body),
            headers=headers,
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        status = response.status
    finally:
        stop(server, thread)

    assert status == 403
    assert payload["error"]["code"] == "missing_decision_header"


# ---------------------------------------------------------------------------
# R1-OB revision 3: a literal ``null`` JSON body must not hang the request.
# ---------------------------------------------------------------------------


def raw_request(
    port: int,
    path: str,
    raw_body: str,
    *,
    header: str,
    origin: str | None = "app://obsidian.md",
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        header: "1",
    }
    if origin is not None:
        headers["Origin"] = origin
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("POST", path, body=raw_body, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def test_a_literal_null_body_on_decisions_gets_an_immediate_fixed_400(
    tmp_path: Path,
) -> None:
    service, body = waiting_service(tmp_path)
    history_before = service.store.history(str(body["run_id"]))
    server = make_cognitive_decision_server(service, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        status, response = raw_request(
            port,
            "/fragment-cognitive/v1/decisions",
            "null",
            header="X-Fragment-Cognitive-Decision",
        )
    finally:
        stop(server, thread)

    assert status == 400
    assert response["error"]["code"] == "invalid_body"
    assert service.store.history(str(body["run_id"])) == history_before


def test_a_literal_null_body_on_withdrawals_gets_an_immediate_fixed_400(
    tmp_path: Path,
) -> None:
    service, notes_dir = kept_service(tmp_path)
    run_id = service.store.latest_for_fragment(
        "fragment-cognitive-synthetic-r1b", "synthetic-api-decision-1"
    ).run_id  # type: ignore[union-attr]
    publish_kept_cognitive_note(service.store, run_id, notes_dir)
    note = next(notes_dir.glob("*.md"))
    note_bytes = note.read_bytes()
    history_before = service.store.history(run_id)
    server, thread, port = serve(service, notes_dir)
    try:
        status, response = raw_request(
            port,
            "/fragment-cognitive/v1/withdrawals",
            "null",
            header="X-Fragment-Cognitive-Withdrawal",
        )
    finally:
        stop(server, thread)

    assert status == 400
    assert response["error"]["code"] == "invalid_body"
    assert service.store.history(run_id) == history_before
    assert note.read_bytes() == note_bytes


# -- rev8：生产形状 5684 同构装配的 v3 全链路 -----------------------------------


def _post_json(
    port: int,
    path: str,
    body: dict[str, Any],
    write_header: tuple[str, str],
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Origin": "app://obsidian.md",
        write_header[0]: write_header[1],
    }
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("POST", path, body=json.dumps(body), headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    # 5684 envelope：业务载荷在 data 字段（错误时 data 为 null，error 承载）。
    data = payload.get("data")
    return response.status, data if isinstance(data, dict) else payload


def test_rev8_production_shaped_v3_full_gate_chain(tmp_path: Path) -> None:
    """rev8 P0-1/P0-2/P0-3 生产形状验收：真实 build_graph_service（注册
    v3）+ ResearchBridge/ResearchExecution + 5684 handler 同构装配——
    v3 创建（Graph 节点驱动 collection、闸门前零 Receipt/零模型）→
    真实 receipt 路由显式签发 → decide approve_synthesis → human_review
    → accept_result → completed；未知 digest 失败关闭；v1 不变。"""
    from fragment_loop.cognitive_server import (
        GRAPH_RESEARCH_RECEIPT_HEADER,
        build_graph_service,
    )
    from fragment_loop.governed_research import GovernedResearchRunner
    from graph_runtime.agent_ledger import AgentCallLedger
    from graph_runtime.research_bridge import ResearchBridge, ResearchExecution
    from graph_runtime.runtime import human_decision_id
    from graph_runtime.specs.fragment_research_escalation_v1 import (
        RESEARCH_GRAPH_ID as V1_GRAPH_ID,
    )
    from graph_runtime.specs.fragment_research_macro_v3 import (
        RESEARCH_MACRO_V3_GRAPH_ID,
        RESEARCH_MACRO_V3_SPEC_DIGEST,
    )

    from .graph_pilot_support import fresh_price_snapshot
    from .graph_research_support import (
        FakeFetchTransport,
        FakeSearchTransport,
        FakeSynthesisTransport,
        fixture_credential_reader,
        good_payload,
        make_record,
        seed_lineage,
        valid_synthesis_outcome,
    )
    from .test_fragment_governed_research import DDG_PAGE

    graph_db = str(tmp_path / "graph.sqlite3")
    # 真实生产装配函数（rev8 P0-1：注册表含 v3 且保留 v1）。
    graph_service = build_graph_service(graph_db)
    store = SQLiteCheckpointStore(graph_db)
    intent_store = SQLiteCheckpointStore(tmp_path / "intent.sqlite3")
    ledger = AgentCallLedger(graph_db)
    runner = GovernedResearchRunner(
        store,
        live_enabled=True,
        collection_enabled=True,
        synthesis_enabled=True,
        search_transport=FakeSearchTransport(DDG_PAGE),
        fetch_transport=FakeFetchTransport(),
        ledger=ledger,
        price_reader=fresh_price_snapshot,
        credential_reader=fixture_credential_reader,
        synthesis_transport=FakeSynthesisTransport(valid_synthesis_outcome("ev-001")),
    )
    bridge = ResearchBridge(store, intent_store, ledger, runner)
    execution = ResearchExecution(
        store, graph_service, ledger, runner, fallback=graph_service.decide
    )
    server = make_cognitive_decision_server(
        None,
        port=0,
        graph_service=graph_service,
        research_bridge=bridge,
        research_execution=execution,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        record = make_record(
            "ev-001",
            "https://example.com/release",
            "official",
            ["release"],
            marker="inherited",
        )
        lineage = seed_lineage(intent_store, records=[dict(record)])
        body = good_payload(
            lineage,
            spec_id=RESEARCH_MACRO_V3_GRAPH_ID,
            spec_digest=RESEARCH_MACRO_V3_SPEC_DIGEST,
        )
        # 未知/错配 digest → 409 plan_expired 失败关闭（零 Run）。
        status, _payload = _post_json(
            port,
            "/graph/v1/research-runs",
            {**body, "spec_digest": "0" * 64},
            ("X-Graph-Research-Run-Create", "1"),
        )
        assert status == 409
        assert graph_service.list_runs() == []
        # v3 创建：Graph 节点驱动 collection（seed 覆盖即证据足够零网络），
        # 停在模型授权闸门——零 Receipt、零模型发送。
        status, payload = _post_json(
            port,
            "/graph/v1/research-runs",
            body,
            ("X-Graph-Research-Run-Create", "1"),
        )
        assert status == 201, payload
        run_id = str(payload["run_id"])
        assert payload["model_calls"] == 0
        checkpoint = store.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.eval_results["graph_state"]
        assert state["graph_id"] == RESEARCH_MACRO_V3_GRAPH_ID
        assert state["spec_digest"] == RESEARCH_MACRO_V3_SPEC_DIGEST
        assert "synthesis_authorization_gate" in state["human_gates"]
        assert "human_review" not in state["human_gates"]
        assert checkpoint.eval_results.get("research_authorization") is None
        # 真实 receipt 路由：显式签发精确绑定 Receipt。
        status, receipt = _post_json(
            port,
            f"/graph/v1/runs/{run_id}/research-authorization-receipt",
            {"run_id": run_id, "requester": "nigo"},
            (GRAPH_RESEARCH_RECEIPT_HEADER, "1"),
        )
        assert status == 201, receipt
        # decide approve_synthesis（真实 5684 decide 路由 + 绑定字段）。
        checkpoint = store.latest(run_id)
        assert checkpoint is not None
        gate = checkpoint.eval_results["graph_state"]["human_gates"][
            "synthesis_authorization_gate"
        ]
        approve = {
            "run_id": run_id,
            "node_id": "synthesis_authorization_gate",
            "decision": "approve_synthesis",
            "spec_digest": gate["spec_digest"],
            "input_digest": gate["input_digest"],
            "expected_sequence": gate["expected_sequence"],
            "requester": "nigo",
            "decision_id": human_decision_id(
                requester="nigo",
                decision="approve_synthesis",
                run_id=run_id,
                node_id="synthesis_authorization_gate",
                spec_digest=gate["spec_digest"],
                input_digest=gate["input_digest"],
                expected_sequence=gate["expected_sequence"],
            ),
            "authorization_digest": receipt["authorization_digest"],
        }
        status, decided = _post_json(
            port,
            f"/graph/v1/runs/{run_id}/human-decisions",
            approve,
            ("X-Graph-Human-Decision", "1"),
        )
        assert status == 202, decided
        assert decided["model_calls"] == 1, "授权决定后恰好一次 synthesis"
        checkpoint = store.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.eval_results["graph_state"]
        assert "human_review" in state["human_gates"], state["human_gates"]
        # accept_result → research_output/completed，asset 绑定同一 digest。
        stored = checkpoint.eval_results["research_result"]
        result_digest = str(stored["result_digest"])
        review_gate = state["human_gates"]["human_review"]
        accept = {
            "run_id": run_id,
            "node_id": "human_review",
            "decision": "accept_result",
            "spec_digest": review_gate["spec_digest"],
            "input_digest": review_gate["input_digest"],
            "expected_sequence": review_gate["expected_sequence"],
            "requester": "nigo",
            "decision_id": human_decision_id(
                requester="nigo",
                decision="accept_result",
                run_id=run_id,
                node_id="human_review",
                spec_digest=review_gate["spec_digest"],
                input_digest=review_gate["input_digest"],
                expected_sequence=review_gate["expected_sequence"],
            ),
            "result_digest": result_digest,
        }
        status, accepted = _post_json(
            port,
            f"/graph/v1/runs/{run_id}/human-decisions",
            accept,
            ("X-Graph-Human-Decision", "1"),
        )
        assert status == 202, accepted
        assert accepted["run_status"] == "completed"
        checkpoint = store.latest(run_id)
        assert checkpoint is not None
        state = checkpoint.eval_results["graph_state"]
        produce = state["nodes"]["asset_production"]
        assert produce["status"] == "succeeded"
        assert produce["output"]["artifact_digest"] == result_digest
        # v1 不变（rev8 P0-1 直接证据）：生产注册表同时保留 v1 与 v3
        # spec；v1 创建/重放/历史 collection+Receipt 语义由
        # test_graph_research_bridge 的 rev6/rev8 反例覆盖（避免与 v3
        # 共享 escalation 的 already_bridged 语义冲突）。
        specs = getattr(graph_service, "specs", None) or getattr(
            graph_service, "_specs", {}
        )
        assert V1_GRAPH_ID in specs, "生产注册表必须保留 v1"
        assert specs[V1_GRAPH_ID].graph_id == V1_GRAPH_ID
        assert RESEARCH_MACRO_V3_GRAPH_ID in specs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
