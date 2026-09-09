"""Graph query and human-gate control service for the 5684 adapter.

The service is optional and read-first: list/detail/history/path/affected/canvas
are pure projections; the two write resources (human decisions, node
reopen) go through the same bound, zero-write-on-mismatch runtime checks.
It adds no new port, no new daemon, and no new storage product — it reuses
the same checkpoint store and the frozen in-process spec registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from common.checkpoint import SQLiteCheckpointStore
from graph_runtime import projection
from graph_runtime.agent_ledger import AgentCallLedger
from graph_runtime.canvas import run_canvas
from graph_runtime.runtime import GraphDecisionError, GraphRuntime
from graph_runtime.spec import GraphSpec


class GraphServiceError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class GraphService:
    def __init__(
        self,
        store: SQLiteCheckpointStore,
        specs: Mapping[str, GraphSpec],
        ledger: AgentCallLedger | None = None,
    ):
        self.store = store
        self.specs = dict(specs)
        self.ledger = ledger
        self._runtime = GraphRuntime(store, self.specs, {})

    def _load(self, run_id: str) -> tuple[Any, dict[str, Any], int, GraphSpec]:
        found = self.store.latest_with_sequence(run_id)
        if found is None:
            raise KeyError(run_id)
        checkpoint, sequence = found
        state = checkpoint.eval_results.get("graph_state")
        if not isinstance(state, dict):
            raise GraphServiceError("not_a_graph_run")
        spec = self.specs.get(str(state.get("graph_id")))
        if spec is None or spec.digest != state.get("spec_digest"):
            raise GraphServiceError("spec_unavailable")
        return checkpoint, state, sequence, spec

    def list_runs(self) -> list[dict[str, Any]]:
        return projection.list_runs(self.store)

    def canvas(self, run_id: str) -> dict[str, Any]:
        checkpoint, _state, sequence, spec = self._load(run_id)
        return run_canvas(checkpoint, sequence, spec=spec)

    def detail(self, run_id: str) -> dict[str, Any]:
        checkpoint, _state, sequence, spec = self._load(run_id)
        agent_calls = None
        if self.ledger is not None:
            agent_calls = {
                row.node_id: row for row in self.ledger.list_for_run(run_id)
            }
        return projection.run_detail(
            checkpoint, sequence, spec=spec, agent_calls=agent_calls
        )

    def history(self, run_id: str) -> list[dict[str, Any]]:
        self._load(run_id)
        return projection.run_history(self.store, run_id)

    def path(self, run_id: str) -> dict[str, Any]:
        checkpoint, _state, sequence, spec = self._load(run_id)
        return projection.run_path(spec, checkpoint, sequence)

    def affected(self, run_id: str, node_id: str) -> dict[str, Any]:
        checkpoint, _state, _sequence, spec = self._load(run_id)
        return projection.run_affected(spec, checkpoint, node_id)

    def decide(self, run_id: str, node_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        checkpoint, applied = self._runtime.apply_human_decision(
            run_id,
            node_id,
            decision=str(body["decision"]),
            spec_digest=str(body["spec_digest"]),
            input_digest=str(body["input_digest"]),
            expected_sequence=int(body["expected_sequence"]),
            requester=str(body["requester"]),
            decision_id=str(body["decision_id"]),
        )
        state = checkpoint.eval_results["graph_state"]
        gate = state["human_gates"][node_id]
        return {
            "run_id": run_id,
            "node_id": node_id,
            "decision": gate["decision"],
            "decision_id": gate["decision_id"],
            "status": "recorded",
            "idempotent": not applied,
            "run_status": state["run_status"],
        }

    def reopen(self, run_id: str, node_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        # rev4 §4：fragment-pilot-v1 的 agent_drafter 严禁走通用 reopen
        # 绕行——Pilot 的发送前失败恢复只能走专用 pilot-retry（带 Receipt
        # 与账本发送事实绑定）。其他 Graph/节点的语义零变化。
        _checkpoint, state, _sequence, _spec = self._load(run_id)
        if state.get("graph_id") == "fragment-pilot-v1" and node_id == "agent_drafter":
            raise GraphDecisionError("pilot_retry_required")
        checkpoint = self._runtime.reopen_node(
            run_id,
            node_id,
            expected_sequence=int(body["expected_sequence"]),
            requester=str(body["requester"]),
            reason=str(body["reason"]),
            reopen_id=str(body["reopen_id"]),
        )
        state = checkpoint.eval_results["graph_state"]
        return {
            "run_id": run_id,
            "node_id": node_id,
            "status": "reopened",
            "run_status": state["run_status"],
        }
