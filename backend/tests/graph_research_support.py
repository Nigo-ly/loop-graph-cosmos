"""Graph Research Escalation v1 测试共享装配（合成 fixture，零网络、零模型）。

所有测试只使用临时 SQLite + 内存假 search/fetch/synthesis transport 与注入式
price_reader；任何真实网络访问、模型调用或凭据读取都会通过爆炸桩立刻失败。
"""
# mypy: disable-error-code="no-untyped-def,untyped-decorator"

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore
from fragment_loop.governed_research import (
    GovernedResearchRunner,
    ResearchSendOutcome,
    make_evidence_record,
)
from fragment_loop.intent_service import (
    ALIGNMENT_SPEC,
    EXECUTION_SPEC,
    escalation_id,
)
from graph_runtime.agent_ledger import AgentCallLedger
from graph_runtime.research_bridge import (
    ResearchBridge,
    ResearchExecution,
    active_bundle_records,
    evidence_bundle_digest_of,
)
from graph_runtime.runtime import human_decision_id
from graph_runtime.service import GraphService
from graph_runtime.specs.fragment_research_escalation_v1 import (
    RESEARCH_GRAPH_ID,
    RESEARCH_SPEC,
    SPEC_DIGEST,
)
from graph_runtime.specs.fragment_research_macro_v3 import (
    RESEARCH_MACRO_V3_GRAPH_ID,
    RESEARCH_MACRO_V3_SPEC,
)

FRAGMENT_ID = "frag-research-test"
GOAL = "核验示例产品是否已公开发布"
ALIGNMENT_ID = "align:" + "a" * 24
EPISODE_ID = "episode:" + "e" * 24
EXECUTION_RUN_ID = "exec:fragment-intent:" + "b" * 24
RESULT_DIGEST = hashlib.sha256(b"fixture-intent-result").hexdigest()
ALIGNMENT_DIGEST = hashlib.sha256(b"fixture-confirmed-alignment").hexdigest()
RESEARCH_SCOPE = {
    "capabilities": ["公开来源发现", "安全网页抓取", "受治理研究合成"],
    "external_scope": ["官方资料", "GitHub、模型社区与可信技术社区"],
    "model_call_cap": 1,
    "cost_cap_cny": 2.0,
    "side_effect": "只读，无外部写入",
    "model_provider": "deepseek",
    "model_name": "deepseek-v4-pro",
    "write_scope": ["Loop Checkpoint 研究结果"],
}

OFFICIAL_URL = "https://example.com/release"
COMMUNITY_URL = "https://community.example.org/thread/1"
DDG_PAGE = b'<a class="result__a" href="https://example.com/release">Example Release Notes</a>'
PAGE_BODY = (
    b"<html><body><h1>Example Release Notes</h1>"
    b"<p>example product released 2026-08-01</p></body></html>"
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def make_record(
    evidence_id: str,
    url: str,
    source_target: str,
    claim_types: list[str],
    *,
    marker: str = "newly_collected",
    title: str = "Example Release Notes",
) -> dict[str, Any]:
    """合成 canonical candidate evidence record（digest 自洽，可独立复算）。"""
    record = make_evidence_record(
        evidence_id=evidence_id,
        title=title,
        url=url,
        source_target=source_target,
        claim_types=claim_types,
        canonical_text=(
            "Example Release Notes\nexample product released 2026-08-01, deploy install guide"
        ),
        transport_facts={"fetched_at": _now_iso(), "body_sha256": "f" * 64},
        research_goal=GOAL,
    )
    record["marker"] = marker
    return record


def seed_lineage(
    intent_store: SQLiteCheckpointStore,
    *,
    records: list[dict[str, Any]] | None = None,
    title: str = GOAL,
    supplement: str = "",
    with_escalation: bool = True,
) -> dict[str, Any]:
    """在 intent 库播种 alignment + execution 谱系（含升级提案与证据包）。"""
    evidence = list(records or [])
    escalation = (
        {
            "status": "proposed",
            "reason": "证据缺口需要受治理研究升级",
            "source_result_digest": RESULT_DIGEST,
        }
        if with_escalation
        else None
    )
    alignment = LoopCheckpoint(
        loop_id=ALIGNMENT_SPEC.loop_id,
        run_id=ALIGNMENT_ID,
        fragment_id=FRAGMENT_ID,
        current_node="alignment",
        status="confirmed",
        eval_results={
            "alignment": {
                "episode_id": EPISODE_ID,
                "execution_run_id": EXECUTION_RUN_ID,
                "case_id": "case:" + "c" * 24,
            }
        },
    )
    assert intent_store.compare_and_append(alignment, expected_sequence=0) is not None
    execution = LoopCheckpoint(
        loop_id=EXECUTION_SPEC.loop_id,
        run_id=EXECUTION_RUN_ID,
        fragment_id=FRAGMENT_ID,
        current_node="harvest",
        status="passed",
        eval_results={
            "execution_binding": {
                "alignment_id": ALIGNMENT_ID,
                "alignment_digest": ALIGNMENT_DIGEST,
                "episode_id": EPISODE_ID,
                "title": title,
                "supplement": supplement,
                "execution_scope": dict(RESEARCH_SCOPE),
            },
            "result_digest": RESULT_DIGEST,
            "graph_escalation": escalation,
            "research_evidence": evidence,
        },
    )
    assert intent_store.compare_and_append(execution, expected_sequence=0) is not None
    active = active_bundle_records(evidence)
    return {
        "alignment_id": ALIGNMENT_ID,
        "episode_id": EPISODE_ID,
        "execution_run_id": EXECUTION_RUN_ID,
        "result_digest": RESULT_DIGEST,
        "escalation_id": escalation_id(ALIGNMENT_ID, RESULT_DIGEST),
        "evidence_bundle_digest": evidence_bundle_digest_of(active),
        "goal": title if not supplement else f"{title} {supplement}",
        "fragment_id": FRAGMENT_ID,
    }


def update_execution_evidence(
    intent_store: SQLiteCheckpointStore, records: list[dict[str, Any]]
) -> None:
    """TOCTOU/已建 Run 反例用：更新 execution Run 的持久化证据包。"""
    found = intent_store.latest_with_sequence(EXECUTION_RUN_ID)
    assert found is not None
    checkpoint, sequence = found
    from dataclasses import replace

    committed = intent_store.compare_and_append(
        replace(
            checkpoint,
            eval_results={**checkpoint.eval_results, "research_evidence": records},
        ),
        expected_sequence=sequence,
        event_type="research_evidence_updated",
    )
    assert committed is not None


class FakeSearchTransport:
    def __init__(self, pages: bytes | Exception = DDG_PAGE):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, query: str) -> bytes:
        self.calls.append(query)
        if isinstance(self.pages, Exception):
            raise self.pages
        return self.pages


class FakeFetchTransport:
    def __init__(self, body: bytes = PAGE_BODY):
        self.body = body
        self.calls: list[str] = []

    def __call__(self, request: Any) -> dict[str, Any]:
        locator = str(request["locator"])
        self.calls.append(locator)
        return {
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "body_bytes": self.body,
            "final_locator": locator,
            "peer_ip": "93.184.216.34",
            "resolved_ips": ["93.184.216.34"],
        }


class FakeSynthesisTransport:
    """fake 合成 transport：记录请求字节，按脚本返回 ResearchSendOutcome。"""

    def __init__(self, outcome: ResearchSendOutcome):
        self.outcome = outcome
        self.calls: list[Any] = []
        self.credentials: list[str] = []

    def send_once(self, request: Any, credential: str) -> ResearchSendOutcome:
        self.calls.append(request)
        self.credentials.append(credential)
        return self.outcome


class ExplodingSearchTransport:
    def __call__(self, query: str) -> bytes:
        raise AssertionError("search must never happen on this path")


class ExplodingFetchTransport:
    def __call__(self, request: Any) -> Any:
        raise AssertionError("fetch must never happen on this path")


class ExplodingCredentialReader:
    def __call__(self, service: str) -> str:
        raise AssertionError("credential read must never happen on this path")


def fixture_credential_reader(service: str) -> str:
    return f"fixture-credential-for-{service}"


def valid_synthesis_outcome(evidence_id: str) -> ResearchSendOutcome:
    payload = {
        "summary": "示例产品已发布。",
        "confirmed": [{"claim": "已发布", "evidence_ids": [evidence_id]}],
        "unknowns": ["性能未知"],
        "conflicts": [],
        "recommendation": "可查阅发布页。",
        "claims": [{"claim": "已发布", "evidence_id": evidence_id, "relation": "supports"}],
    }
    return ResearchSendOutcome(
        request_sent="true",
        raw_content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        error_category=None,
        http_status=200,
        declared_model="deepseek-v4-pro",
        input_tokens=128,
        output_tokens=64,
    )


def good_payload(lineage: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = {
        "spec_id": RESEARCH_GRAPH_ID,
        "spec_digest": SPEC_DIGEST,
        "alignment_id": lineage["alignment_id"],
        "episode_id": lineage["episode_id"],
        "evidence_bundle_digest": lineage["evidence_bundle_digest"],
        "escalation_id": lineage["escalation_id"],
        "requester": "nigo",
    }
    payload.update(overrides)
    return payload


class ResearchEnv:
    """一套完整 Research 装配：graph/intent 双库、ledger、runner、bridge、execution。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        records: list[dict[str, Any]] | None = None,
        live: bool = True,
        search: Any = None,
        fetch: Any = None,
        synthesis_transport: Any = None,
        price_reader: Any = None,
        credential_reader: Any = None,
        with_escalation: bool = True,
        title: str = GOAL,
    ):
        self.store = SQLiteCheckpointStore(tmp_path / "graph.sqlite3")
        self.intent_store = SQLiteCheckpointStore(tmp_path / "intent.sqlite3")
        self.ledger = AgentCallLedger(tmp_path / "graph.sqlite3")
        self.service = GraphService(
            self.store,
            # v3 spec 入注册表：v3 run 的人工决定（decide）可经同一
            # GraphService 契约推进；v1 run 行为不变。
            {
                RESEARCH_GRAPH_ID: RESEARCH_SPEC,
                RESEARCH_MACRO_V3_GRAPH_ID: RESEARCH_MACRO_V3_SPEC,
            },
            ledger=self.ledger,
        )
        self.lineage = seed_lineage(
            self.intent_store,
            records=records,
            title=title,
            with_escalation=with_escalation,
        )
        self.runner = GovernedResearchRunner(
            self.store,
            live_enabled=live,
            search_transport=search,
            fetch_transport=fetch,
            ledger=self.ledger,
            price_reader=price_reader,
            credential_reader=credential_reader,
            synthesis_transport=synthesis_transport,
        )
        self.bridge = ResearchBridge(self.store, self.intent_store, self.ledger, self.runner)
        self.execution = ResearchExecution(
            self.store,
            self.service,
            self.ledger,
            self.runner,
            fallback=self.service.decide,
        )

    def create(self, **overrides: Any) -> tuple[int, dict[str, Any]]:
        return self.bridge.create_run(good_payload(self.lineage, **overrides))

    def gate_body(self, run_id: str, node_id: str, decision: str, **extra: Any):
        gate = self.service.detail(run_id)["human_gates"][node_id]
        return {
            "run_id": run_id,
            "node_id": node_id,
            "decision": decision,
            "spec_digest": gate["spec_digest"],
            "input_digest": gate["input_digest"],
            "expected_sequence": gate["expected_sequence"],
            "requester": "nigo",
            "decision_id": human_decision_id(
                requester="nigo",
                decision=decision,
                run_id=run_id,
                node_id=node_id,
                spec_digest=gate["spec_digest"],
                input_digest=gate["input_digest"],
                expected_sequence=gate["expected_sequence"],
            ),
            **extra,
        }

    def decide(
        self, run_id: str, node_id: str, decision: str, **extra: Any
    ) -> dict[str, Any]:
        return self.execution.decide(
            run_id, node_id, self.gate_body(run_id, node_id, decision, **extra)
        )

    def state(self, run_id: str) -> dict[str, Any]:
        latest = self.store.latest(run_id)
        assert latest is not None
        return cast(dict[str, Any], latest.eval_results["graph_state"])

    def node_statuses(self, run_id: str) -> dict[str, str]:
        state = self.state(run_id)
        return {node_id: str(node["status"]) for node_id, node in state["nodes"].items()}

    def edges_taken(self, run_id: str) -> list[str]:
        return [str(record["edge_id"]) for record in self.state(run_id)["edges_taken"]]


def succeeded_outputs(env: ResearchEnv, run_id: str) -> list[str]:
    """三个出口节点中真实 succeeded 的子集（唯一出口断言用）。"""
    statuses = env.node_statuses(run_id)
    return [
        node_id
        for node_id in ("research_output", "rejected_output", "aborted_output")
        if statuses[node_id] == "succeeded"
    ]
