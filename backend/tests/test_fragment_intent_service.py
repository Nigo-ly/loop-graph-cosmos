from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, cast

import pytest

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.cognitive_server import make_cognitive_decision_server
from fragment_loop.intent_service import (
    ALIGNMENT_DECISION_HEADER,
    ALIGNMENT_ESCALATION_HEADER,
    ALIGNMENT_HEADER,
    EPISODE_CONTINUATION_HEADER,
    CapabilityAdapter,
    FragmentIntentError,
    FragmentIntentService,
    continuation_id,
    escalation_id,
)

INPUT_DIGEST = "a" * 64


def source_loader(fragment_id: str, input_digest: str) -> dict[str, object]:
    assert fragment_id == "fragment-1"
    return {
        "title": "一个待理解的技术碎片",
        "literal_summary": "用户希望先判断信息真实性和个人价值。",
        "memory_basis": [{"label": "既有技术核验案例", "maturity": "reusable"}],
        "input_digest": input_digest,
    }


def resolver(_source: object) -> dict[str, object]:
    return {
        "suggested_intents": ["verify", "evaluate_relevance"],
        "dynamic_intents": [
            {
                "id": "check_local_fit",
                "label": "判断本机是否适用",
                "basis": "原始问题包含本地可行性判断",
            }
        ],
        "reasoning": "这是一条需要先核实再判断个人价值的技术信息。",
        "plan": "先核验必要事实；若出现安装或复杂副作用，再提出 Graph 升级。",
        "expected_result": "形成可信结论、未知项和下一步。",
        "exclusions": ["不安装", "不写知识资产"],
        "recommended_route": "verify",
        "execution_scope": {
            "capabilities": ["本地分析", "只读核验"],
            "external_scope": ["官方与公开社区来源"],
            "model_call_cap": 0,
            "cost_cap_cny": 0,
            "side_effect": "none",
        },
    }


def result(*, escalation: bool = False) -> dict[str, object]:
    return {
        "summary": "当前材料支持先做轻量核验。",
        "unknowns": ["真实效果尚未验证"],
        "next_checks": ["核对一手资料"],
        "needs_escalation": escalation,
        "escalation_reason": "需要安装验证" if escalation else "",
        "model_calls": 0,
        "tool_calls": 1,
        "harvest": [
            {
                "role": "lesson",
                "summary": "先核实发布与许可，再评估部署。",
                "maturity": "qualified",
                "qualification_basis": "source_verified",
            }
        ],
    }


def service(
    tmp_path: Path,
    *,
    direct_adapter: CapabilityAdapter | None = None,
    verify_adapter: CapabilityAdapter | None = None,
) -> FragmentIntentService:
    return FragmentIntentService(
        SQLiteCheckpointStore(tmp_path / "state.sqlite3"),
        source_loader,
        resolver,
        direct_adapter=direct_adapter,
        verify_adapter=verify_adapter,
    )


def request(
    server: ThreadingHTTPServer,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    port = int(server.server_address[1])
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    merged = {"Host": f"127.0.0.1:{port}", "Origin": "app://obsidian.md"}
    if payload is not None:
        merged.update(
            {"Content-Type": "application/json", "Content-Length": str(len(payload))}
        )
    merged.update(headers or {})
    connection.request(method, path, body=payload, headers=merged)
    response = connection.getresponse()
    data = json.loads(response.read().decode())
    connection.close()
    return response.status, data


@pytest.fixture()  # type: ignore[untyped-decorator]
def intent_server(tmp_path: Path) -> Iterator[ThreadingHTTPServer]:
    target = service(tmp_path, verify_adapter=lambda _binding: result())
    server = make_cognitive_decision_server(
        None,
        port=0,
        require_trusted_origin=True,
        intent_service=target,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def proposal_body() -> dict[str, object]:
    return {
        "fragment_id": "fragment-1",
        "input_digest": INPUT_DIGEST,
        "requester": "nigo",
    }


def decision_body(item: dict[str, object], **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "alignment_id": item["alignment_id"],
        "revision": item["revision"],
        "input_digest": item["input_digest"],
        "intents": ["verify", "evaluate_relevance"],
        "supplement": "优先给出最短可靠结论",
        "action": "confirm",
        "requester": "nigo",
    }
    body.update(overrides)
    return body


def test_proposal_is_safe_deterministic_and_idempotent(tmp_path: Path) -> None:
    target = service(tmp_path)
    first_status, first = target.propose(proposal_body())
    second_status, second = target.propose(proposal_body())
    assert first_status == 201
    assert second_status == 200
    assert first == second
    assert first["status"] == "suggested"
    assert first["suggested_intents"] == ["verify", "evaluate_relevance"]
    assert "prompt" not in first
    assert "path" not in first
    assert len(target.list_alignments()) == 1


def test_parallel_proposal_has_one_checkpoint_family(tmp_path: Path) -> None:
    target = service(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _unused: target.propose(proposal_body()), range(2)))
    assert sorted(status for status, _item in outcomes) == [200, 201]
    assert outcomes[0][1]["alignment_id"] == outcomes[1][1]["alignment_id"]


def test_source_drift_and_malformed_resolver_fail_closed(tmp_path: Path) -> None:
    drift = FragmentIntentService(
        SQLiteCheckpointStore(tmp_path / "drift.sqlite3"),
        lambda _fragment, _digest: {
            **source_loader("fragment-1", INPUT_DIGEST),
            "input_digest": "b" * 64,
        },
        resolver,
    )
    with pytest.raises(FragmentIntentError, match="source_changed"):
        drift.propose(proposal_body())

    malformed = FragmentIntentService(
        SQLiteCheckpointStore(tmp_path / "bad.sqlite3"),
        source_loader,
        lambda _source: {**resolver(_source), "recommended_route": "surprise"},
    )
    with pytest.raises(FragmentIntentError, match="resolver_invalid"):
        malformed.propose(proposal_body())


def test_confirm_verify_creates_loop_run_and_executes_with_harvest(tmp_path: Path) -> None:
    target = service(tmp_path, verify_adapter=lambda _binding: result())
    _status, proposed = target.propose(proposal_body())
    status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    assert status == 201
    assert confirmed["route"] == "verify"
    run_id = str(confirmed["execution_run_id"])
    outcome = target.run_execution(run_id)
    assert outcome["status"] == "passed"
    assert outcome["route"] == "verify"
    assert outcome["result"]["model_calls"] == 0  # type: ignore[index]
    assert outcome["harvest"][0]["maturity"] == "qualified"  # type: ignore[index]
    assert outcome["graph_escalation"] is None


def test_direct_route_uses_direct_adapter_and_zero_graph_state(tmp_path: Path) -> None:
    direct_resolver = lambda source: {  # noqa: E731
        **resolver(source),
        "suggested_intents": ["learn"],
        "recommended_route": "direct",
    }
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    target = FragmentIntentService(
        store,
        source_loader,
        direct_resolver,
        direct_adapter=lambda _binding: result(),
    )
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]),
        decision_body(proposed, intents=["learn"]),
    )
    outcome = target.run_execution(str(confirmed["execution_run_id"]))
    assert outcome["status"] == "passed"
    assert outcome["route"] == "direct"
    assert store.run_ids_with_prefix("graph:") == []


def test_save_only_creates_no_execution_run(tmp_path: Path) -> None:
    target = service(tmp_path)
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]),
        decision_body(
            proposed,
            action="save_only",
            intents=["save"],
            supplement="",
        ),
    )
    assert confirmed["route"] == "save_only"
    assert "execution_run_id" not in confirmed


def test_graph_recommendation_only_creates_a_proposal_state(tmp_path: Path) -> None:
    graph_resolver = lambda source: {  # noqa: E731
        **resolver(source),
        "suggested_intents": ["plan_action"],
        "recommended_route": "graph",
    }
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    target = FragmentIntentService(store, source_loader, graph_resolver)
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]),
        decision_body(proposed, intents=["plan_action"]),
    )
    assert confirmed["route"] == "graph"
    assert "execution_run_id" not in confirmed
    assert store.run_ids_with_prefix("graph:") == []


def test_stale_or_changed_decision_is_rejected(tmp_path: Path) -> None:
    target = service(tmp_path)
    _status, proposed = target.propose(proposal_body())
    with pytest.raises(FragmentIntentError, match="alignment_changed"):
        target.decide(
            str(proposed["alignment_id"]),
            decision_body(proposed, revision=2),
        )
    target.decide(str(proposed["alignment_id"]), decision_body(proposed))
    with pytest.raises(FragmentIntentError, match="decision_conflict"):
        target.decide(
            str(proposed["alignment_id"]),
            decision_body(proposed, supplement="changed"),
        )


def test_exact_decision_replay_resumes_only_an_untouched_approved_run(
    tmp_path: Path,
) -> None:
    """Crash after registration is recoverable without creating a second Run."""
    target = service(tmp_path, verify_adapter=lambda _binding: result())
    _status, proposed = target.propose(proposal_body())
    original_run_execution = target.run_execution
    target.run_execution = lambda _run_id: {}  # type: ignore[method-assign,assignment]
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    run_id = str(confirmed["execution_run_id"])
    assert target.store.latest(run_id).status == "approved"  # type: ignore[union-attr]

    target.run_execution = original_run_execution  # type: ignore[method-assign]
    status, replayed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    assert status == 200
    assert replayed["execution"]["status"] == "passed"  # type: ignore[index]
    assert replayed["execution_run_id"] == run_id
    assert target.store.latest(run_id) is not None


def test_missing_adapter_blocks_honestly_without_calls(tmp_path: Path) -> None:
    target = service(tmp_path)
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    outcome = target.run_execution(str(confirmed["execution_run_id"]))
    assert outcome["status"] == "blocked"
    assert outcome["stop_reason"] == "capability_unavailable"
    assert outcome["result"] is None


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("maturity", "basis"),
    [("trace", None), ("qualified", None), ("candidate", "source_verified")],
)
def test_harvest_maturity_and_basis_are_strict(
    tmp_path: Path, maturity: str, basis: str | None
) -> None:
    invalid = result()
    invalid["harvest"] = [
        {
            "role": "lesson",
            "summary": "invalid",
            "maturity": maturity,
            "qualification_basis": basis,
        }
    ]
    target = service(tmp_path, verify_adapter=lambda _binding: invalid)
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    outcome = target.run_execution(str(confirmed["execution_run_id"]))
    assert outcome["status"] == "blocked"
    assert outcome["result"] is None


def test_event_roles_and_reusable_metadata_are_strict(tmp_path: Path) -> None:
    invalid_role = result()
    invalid_role["harvest"] = [
        {
            "role": "random_topic",
            "summary": "invalid",
            "maturity": "candidate",
            "qualification_basis": None,
        }
    ]
    target = service(tmp_path, verify_adapter=lambda _binding: invalid_role)
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    assert target.run_execution(str(confirmed["execution_run_id"]))["status"] == "blocked"

    incomplete = result()
    incomplete["harvest"] = [
        {
            "role": "lesson",
            "summary": "missing applicability metadata",
            "maturity": "reusable",
            "qualification_basis": "practice_validated",
        }
    ]
    other_path = tmp_path / "other"
    other_path.mkdir()
    other = service(other_path, verify_adapter=lambda _binding: incomplete)
    _status, proposed = other.propose(proposal_body())
    _status, confirmed = other.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    assert other.run_execution(str(confirmed["execution_run_id"]))["status"] == "blocked"


def test_context_compiler_prefers_qualified_over_unverified_clues(tmp_path: Path) -> None:
    mixed = result()
    mixed["harvest"] = [
        {
            "role": "evidence",
            "summary": "需要继续验证的社区线索",
            "maturity": "candidate",
            "qualification_basis": None,
        },
        {
            "role": "lesson",
            "summary": "先核实许可再部署",
            "maturity": "qualified",
            "qualification_basis": "source_verified",
        },
        {
            "role": "correction",
            "summary": "某版本下需要替换旧参数",
            "maturity": "reusable",
            "qualification_basis": "practice_validated",
            "evidence": ["本机反例通过"],
            "applicability": "仅适用于已验证版本与同类硬件",
            "unknowns": [],
            "invalidates_when": "版本或硬件条件变化",
            "review_trigger": "升级版本时复查",
            "relation": "supersedes",
        },
    ]
    target = service(tmp_path, verify_adapter=lambda _binding: mixed)
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    context = target.compile_context(str(confirmed["case_id"]))
    assert [item["maturity"] for item in context] == ["reusable", "qualified"]
    assert [item["usage"] for item in context] == [
        "verified_context",
        "qualified_context",
    ]


def test_adapter_cannot_exceed_confirmed_model_or_tool_budget(tmp_path: Path) -> None:
    over_budget = result()
    over_budget["model_calls"] = 1
    target = service(tmp_path, verify_adapter=lambda _binding: over_budget)
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    outcome = target.run_execution(str(confirmed["execution_run_id"]))
    assert outcome["status"] == "blocked"
    assert outcome["result"] is None


def test_escalation_is_only_a_proposal_and_preserves_result(tmp_path: Path) -> None:
    target = service(tmp_path, verify_adapter=lambda _binding: result(escalation=True))
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    outcome = target.run_execution(str(confirmed["execution_run_id"]))
    assert outcome["status"] == "passed"
    assert outcome["graph_escalation"]["status"] == "proposed"  # type: ignore[index]
    assert outcome["result"]["summary"] == "当前材料支持先做轻量核验。"  # type: ignore[index]


def test_continuation_creates_child_episode_without_reopening_parent(tmp_path: Path) -> None:
    target = service(tmp_path, verify_adapter=lambda _binding: result())
    _status, proposed = target.propose(proposal_body())
    _status, parent = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    parent_run = cast(dict[str, object], parent["execution"])
    goal = "继续处理部署后的显存溢出问题"
    identifier = continuation_id(
        str(parent["episode_id"]),
        str(parent_run["run_id"]),
        str(parent_run["result_digest"]),
        goal,
    )
    body = {
        "parent_episode_id": parent["episode_id"],
        "parent_run_id": parent_run["run_id"],
        "source_result_digest": parent_run["result_digest"],
        "goal": goal,
        "continuation_id": identifier,
        "requester": "nigo",
    }
    first_status, child = target.continue_episode(str(parent["episode_id"]), body)
    second_status, repeated = target.continue_episode(str(parent["episode_id"]), body)
    assert (first_status, second_status) == (201, 200)
    assert child == repeated
    assert child["parent_episode_id"] == parent["episode_id"]
    assert child["source_result_digest"] == parent_run["result_digest"]
    assert child["episode_id"] != parent["episode_id"]
    assert child["status"] == "suggested"
    assert target.get_alignment(str(parent["alignment_id"]))["status"] == "passed"


def test_continuation_binding_drift_fails_closed(tmp_path: Path) -> None:
    target = service(tmp_path, verify_adapter=lambda _binding: result())
    _status, proposed = target.propose(proposal_body())
    _status, parent = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    execution = cast(dict[str, object], parent["execution"])
    body = {
        "parent_episode_id": parent["episode_id"],
        "parent_run_id": execution["run_id"],
        "source_result_digest": execution["result_digest"],
        "goal": "继续测试",
        "continuation_id": "continuation:wrong",
        "requester": "nigo",
    }
    with pytest.raises(FragmentIntentError, match="continuation_binding_changed"):
        target.continue_episode(str(parent["episode_id"]), body)


def test_case_and_harvest_are_safe_checkpoint_projections(tmp_path: Path) -> None:
    target = service(tmp_path, verify_adapter=lambda _binding: result())
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    case = target.case(str(confirmed["case_id"]))
    harvested = target.harvest(str(confirmed["episode_id"]))
    assert case["fragment_id"] == "fragment-1"
    assert len(cast(list[object], case["episodes"])) == 1
    items = cast(list[dict[str, object]], harvested["items"])
    assert items[0]["maturity"] == "qualified"
    assert "prompt" not in json.dumps(case)
    assert "credential" not in json.dumps(harvested)


def test_explicit_escalation_returns_proposal_and_never_creates_graph_run(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "state.sqlite3")
    target = FragmentIntentService(
        store,
        source_loader,
        resolver,
        verify_adapter=lambda _binding: result(escalation=True),
    )
    _status, proposed = target.propose(proposal_body())
    _status, confirmed = target.decide(
        str(proposed["alignment_id"]), decision_body(proposed)
    )
    execution = cast(dict[str, object], confirmed["execution"])
    digest = str(execution["result_digest"])
    status, escalation = target.escalate(
        str(confirmed["alignment_id"]),
        {
            "alignment_id": confirmed["alignment_id"],
            "revision": confirmed["revision"],
            "input_digest": confirmed["input_digest"],
            "source_result_digest": digest,
            "escalation_id": escalation_id(str(confirmed["alignment_id"]), digest),
            "requester": "nigo",
        },
    )
    assert status == 200
    assert escalation["capability_status"] == "template_required"
    assert escalation["graph_run_created"] is False
    assert store.run_ids_with_prefix("graph:") == []


def test_alignment_http_contract_and_header_guards(intent_server: ThreadingHTTPServer) -> None:
    status, envelope = request(intent_server, "GET", "/fragment/v1/alignments")
    assert status == 200
    assert envelope["data"] == []

    status, envelope = request(
        intent_server,
        "POST",
        "/fragment/v1/alignments",
        proposal_body(),
    )
    assert status == 403
    assert envelope["error"]["code"] == "missing_fragment_alignment_header"

    status, envelope = request(
        intent_server,
        "POST",
        "/fragment/v1/alignments",
        proposal_body(),
        {ALIGNMENT_HEADER: "1"},
    )
    assert status == 201
    item = envelope["data"]

    status, envelope = request(
        intent_server,
        "POST",
        f"/fragment/v1/alignments/{item['alignment_id']}/decisions",
        decision_body(item),
    )
    assert status == 403
    assert envelope["error"]["code"] == "missing_fragment_alignment_decision_header"

    status, envelope = request(
        intent_server,
        "POST",
        f"/fragment/v1/alignments/{item['alignment_id']}/decisions",
        decision_body(item),
        {ALIGNMENT_DECISION_HEADER: "1"},
    )
    assert status == 201
    confirmed = envelope["data"]
    assert confirmed["execution"]["status"] == "passed"

    status, detail = request(
        intent_server,
        "GET",
        f"/fragment/v1/alignments/{confirmed['alignment_id']}",
    )
    assert status == 200
    assert detail["data"]["episode_id"] == confirmed["episode_id"]

    status, case = request(
        intent_server, "GET", f"/fragment/v1/cases/{confirmed['case_id']}"
    )
    assert status == 200
    assert len(case["data"]["episodes"]) == 1

    status, harvested = request(
        intent_server,
        "GET",
        f"/fragment/v1/episodes/{confirmed['episode_id']}/harvest",
    )
    assert status == 200
    assert harvested["data"]["items"][0]["maturity"] == "qualified"

    execution = confirmed["execution"]
    goal = "继续排查新的部署问题"
    continuation = {
        "parent_episode_id": confirmed["episode_id"],
        "parent_run_id": execution["run_id"],
        "source_result_digest": execution["result_digest"],
        "goal": goal,
        "continuation_id": continuation_id(
            str(confirmed["episode_id"]),
            str(execution["run_id"]),
            str(execution["result_digest"]),
            goal,
        ),
        "requester": "nigo",
    }
    status, missing_header = request(
        intent_server,
        "POST",
        f"/fragment/v1/episodes/{confirmed['episode_id']}/continuations",
        continuation,
    )
    assert status == 403
    assert missing_header["error"]["code"] == "missing_fragment_episode_continuation_header"
    status, child = request(
        intent_server,
        "POST",
        f"/fragment/v1/episodes/{confirmed['episode_id']}/continuations",
        continuation,
        {EPISODE_CONTINUATION_HEADER: "1"},
    )
    assert status == 201
    assert child["data"]["parent_episode_id"] == confirmed["episode_id"]

    digest = str(execution["result_digest"])
    escalation = {
        "alignment_id": confirmed["alignment_id"],
        "revision": confirmed["revision"],
        "input_digest": confirmed["input_digest"],
        "source_result_digest": digest,
        "escalation_id": escalation_id(str(confirmed["alignment_id"]), digest),
        "requester": "nigo",
    }
    status, missing_header = request(
        intent_server,
        "POST",
        f"/fragment/v1/alignments/{confirmed['alignment_id']}/escalations",
        escalation,
    )
    assert status == 403
    assert missing_header["error"]["code"] == "missing_fragment_alignment_escalation_header"
    status, unavailable = request(
        intent_server,
        "POST",
        f"/fragment/v1/alignments/{confirmed['alignment_id']}/escalations",
        escalation,
        {ALIGNMENT_ESCALATION_HEADER: "1"},
    )
    assert status == 409
    assert unavailable["error"]["code"] == "escalation_not_available"


def test_alignment_http_rejects_untrusted_origin(intent_server: ThreadingHTTPServer) -> None:
    status, envelope = request(
        intent_server,
        "POST",
        "/fragment/v1/alignments",
        proposal_body(),
        {ALIGNMENT_HEADER: "1", "Origin": "https://evil.example"},
    )
    assert status == 403
    assert envelope["error"]["code"] == "forbidden_origin"
