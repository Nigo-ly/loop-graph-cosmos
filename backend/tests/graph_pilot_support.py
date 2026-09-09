"""Graph Pilot Entry Bridge v1 测试共享装配（合成 fixture，零网络、零模型）。

所有测试只使用内存假 Transport 与注入式 price_reader；任何真实网络访问
或模型调用都会通过爆炸桩立刻失败。
"""
# mypy: disable-error-code="no-untyped-def,untyped-decorator"

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from common.checkpoint import SQLiteCheckpointStore
from graph_runtime.agent_adapter import TransportResponse
from graph_runtime.agent_ledger import AgentCallLedger
from graph_runtime.pilot_bridge import PilotBridge
from graph_runtime.pilot_execution import PilotExecution
from graph_runtime.pilot_live import PilotSendOutcome, evaluate_pilot_price_gate
from graph_runtime.runtime import human_decision_id
from graph_runtime.service import GraphService
from graph_runtime.specs.fragment_pilot_v1 import PILOT_SPEC, SPEC_DIGEST

FRAGMENT_REF = "散记/碎片想法/taste-skill.md"
CANDIDATE_ID = "cand-pilot-1"
CONTENT_SHA = "a" * 64

CANDIDATE = {
    "title": "taste-skill：给 AI 注入设计品味",
    "core_judgment": "设计品味可以工程化沉淀为可复用规则",
    "user_value": "让主页 UI 决策有一致审美基线",
    "card_titles": ["设计规则卡", "审美基线卡"],
}

# 合成官方价格页（USD 表结构）：cache hit $0.5 / cache miss $2 / output $3。
SYNTHETIC_PRICING_PAGE = """
<html><body><table>
<tr><td>MODEL</td><td>deepseek-chat</td><td>deepseek-v4-pro</td></tr>
<tr><td>input tokens (cache hit)</td><td>$0.07</td><td>$0.5</td></tr>
<tr><td>input tokens (cache miss)</td><td>$0.27</td><td>$2.0</td></tr>
<tr><td>output tokens</td><td>$1.10</td><td>$3.0</td></tr>
</table></body></html>
"""

PRICE_SNAPSHOT = evaluate_pilot_price_gate(
    SYNTHETIC_PRICING_PAGE,
    fetched_at="2026-08-06T00:00:00+00:00",
    page_url="https://api-docs.deepseek.com/quick_start/pricing/",
)


def fresh_price_snapshot(now: Any = None) -> dict[str, Any]:
    """时效合格的快照：fetched_at 跟随注入时钟（默认真实 UTC 现在）。"""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    stamp = (now() if callable(now) else None) or _datetime.now(_UTC)
    return dict(
        evaluate_pilot_price_gate(
            SYNTHETIC_PRICING_PAGE,
            fetched_at=stamp.astimezone(_UTC).isoformat(timespec="seconds"),
            page_url="https://api-docs.deepseek.com/quick_start/pricing/",
        )
    )


class FakeReviewService:
    """ProductReviewService 的最小只读替身；记录读取次数便于断言。"""

    def __init__(self, projection: dict[str, Any] | None, detail: dict[str, Any] | None):
        self.projection = projection
        self.detail_payload = detail
        self.list_calls = 0

    @classmethod
    def ready(cls, **overrides: Any) -> FakeReviewService:
        projection = {
            "candidate_id": CANDIDATE_ID,
            "fragment_ref": FRAGMENT_REF,
            "title": CANDIDATE["title"],
            "core_judgment": CANDIDATE["core_judgment"],
            "user_value": CANDIDATE["user_value"],
            "content_status": "pending_confirmation",
            "evidence_level": "unverified",
            "has_conflict": False,
            "content_sha256": CONTENT_SHA,
        }
        projection.update(overrides)
        detail = {
            **projection,
            "candidate_cards": [
                {"card_id": f"card-{index}", "title": title}
                for index, title in enumerate(CANDIDATE["card_titles"], start=1)
            ],
        }
        return cls(projection, detail)

    def list_reviews(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        return [dict(self.projection)] if self.projection is not None else []

    def detail(self, candidate_id: str) -> dict[str, Any]:
        if (
            self.detail_payload is None
            or candidate_id != self.detail_payload.get("candidate_id")
        ):
            raise KeyError(candidate_id)
        return dict(self.detail_payload)


class UnavailableReviewService:
    def list_reviews(self) -> list[dict[str, Any]]:
        raise OSError("candidates directory unreadable")

    def detail(self, candidate_id: str) -> dict[str, Any]:
        raise OSError("candidates directory unreadable")


class ExplodingTransport:
    """任何 send_once 都是缺陷：创建/预案/推进路径零外网的证明桩。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def send_once(self, request: Any, credential: str = "") -> Any:
        self.calls.append(request)
        raise AssertionError("model send must never happen on this path")


class ExplodingPriceReader:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("price read must never happen on this path")


class FakePilotTransport:
    """send_once 协议的脚本化假 Transport：记录完整请求（含 Prompt 字节），
    按脚本返回 PilotSendOutcome；凭据逐字透传记录。"""

    def __init__(self, outcomes: list[PilotSendOutcome]):
        self._remaining = list(outcomes)
        self.calls: list[Any] = []
        self.credentials: list[str] = []

    def send_once(self, request: Any, credential: str) -> PilotSendOutcome:
        self.calls.append(request)
        self.credentials.append(credential)
        if not self._remaining:
            raise AssertionError("unexpected extra model call")
        return self._remaining.pop(0)


def fixture_credential_reader(service: str) -> str:
    return f"fixture-credential-for-{service}"


class ExplodingCredentialReader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, service: str) -> str:
        self.calls.append(service)
        raise AssertionError("credential read must never happen on this path")


def good_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "spec_id": "fragment-pilot-v1",
        "spec_digest": SPEC_DIGEST,
        "fragment_ref": FRAGMENT_REF,
        "candidate_id": CANDIDATE_ID,
        "candidate_content_sha256": CONTENT_SHA,
        "requester": "nigo",
    }
    payload.update(overrides)
    return payload


def make_valid_outcome(
    summary: str = "安全摘要",
    unknowns: list[str] | None = None,
    next_checks: list[str] | None = None,
) -> PilotSendOutcome:
    payload = {
        "summary": summary,
        "unknowns": unknowns if unknowns is not None else ["待核实点"],
        "next_checks": next_checks if next_checks is not None else ["建议复核来源"],
    }
    return PilotSendOutcome(
        "true",
        TransportResponse(
            declared_provider="deepseek",
            declared_model="deepseek-v4-pro",
            input_tokens=120,
            output_tokens=60,
            payload=payload,
        ),
        None,
        200,
    )


def make_scripted_transport(outcomes: list[PilotSendOutcome]) -> FakePilotTransport:
    return FakePilotTransport(outcomes)


class PilotEnv:
    """一套完整 Pilot 装配：store/ledger/service/bridge/execution。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        review: Any = None,
        transport: Any = None,
        price_reader: Any = None,
        live_enabled: bool = False,
        credential_reader: Any = None,
        monotonic: Any = None,
        clock: Any = None,
    ):
        self.store = SQLiteCheckpointStore(tmp_path / "graph.sqlite3")
        self.ledger = AgentCallLedger(tmp_path / "graph.sqlite3")
        self.service = GraphService(
            self.store, {PILOT_SPEC.graph_id: PILOT_SPEC}, ledger=self.ledger
        )
        self.review = review if review is not None else FakeReviewService.ready()
        self.transport = transport if transport is not None else ExplodingTransport()
        self.price_reader = price_reader
        self.bridge = PilotBridge(self.store, self.review, self.ledger)
        self.execution = PilotExecution(
            self.store,
            self.service,
            self.review,
            self.ledger,
            self.transport,
            price_reader=price_reader,
            live_enabled=live_enabled,
            credential_reader=credential_reader,
            monotonic=monotonic,
            clock=clock,
        )

    def create(self, **overrides: Any) -> tuple[int, dict[str, Any]]:
        return self.bridge.create_run(good_payload(**overrides))

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

    def issue(self, run_id: str) -> tuple[int, dict[str, Any]]:
        return self.execution.issue_receipt(run_id)

    def canvas_node(self, run_id: str, node_id: str) -> dict[str, Any]:
        canvas = self.service.canvas(run_id)
        return next(n for n in canvas["nodes"] if n["node_id"] == node_id)

    def state(self, run_id: str) -> dict[str, Any]:
        latest = self.store.latest(run_id)
        assert latest is not None
        return cast(dict[str, Any], latest.eval_results["graph_state"])
