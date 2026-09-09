"""Deterministic, side-effect-free test LoopSpec for P3D Gate 1.

No network, no model, no subprocess, no file writes. Costs are fixed
constants so every budget reservation and settlement is reproducible.
"""

from __future__ import annotations

from typing import Any

from common.execution import plugin_eval
from common.supervisor import LoopSpec, SupervisorContext
from common.types import LoopResult
from execution_dispatcher.budget import CostPlan

TEST_LOOPSPEC = LoopSpec(
    loop_id="p3d-isolated-test-v1",
    version="1.0.0",
    goal="P3D 隔离测试 Loop：确定性、无网络、无模型、无副作用",
    first_node="intake",
    nodes=("intake", "worker", "evaluator", "feedback"),
    max_iterations=8,
    max_seconds=1800,
    token_limit=50000,
    tool_call_limit=20,
    worker_version="deterministic-test-worker-v1",
    evaluator_version="deterministic-test-evaluator-v1",
)

TEST_NODE_COSTS = {
    "intake": CostPlan(tokens=10, tool_calls=1),
    "worker": CostPlan(tokens=100, tool_calls=2),
    "evaluator": CostPlan(tokens=50, tool_calls=1),
    "feedback": CostPlan(tokens=10, tool_calls=0),
}

EVALUATOR_NODES = frozenset({"evaluator"})

TEST_LOOPSPEC_REGISTRY_ENTRY = {
    "spec": {
        "loop_id": TEST_LOOPSPEC.loop_id,
        "version": TEST_LOOPSPEC.version,
        "goal": TEST_LOOPSPEC.goal,
        "first_node": TEST_LOOPSPEC.first_node,
        "nodes": list(TEST_LOOPSPEC.nodes),
        "max_iterations": TEST_LOOPSPEC.max_iterations,
        "max_seconds": TEST_LOOPSPEC.max_seconds,
        "token_limit": TEST_LOOPSPEC.token_limit,
        "tool_call_limit": TEST_LOOPSPEC.tool_call_limit,
        "worker_version": TEST_LOOPSPEC.worker_version,
        "evaluator_version": TEST_LOOPSPEC.evaluator_version,
    },
    "handlers": list(TEST_LOOPSPEC.nodes),
}


def cost_plan_of(node: str) -> CostPlan:
    return TEST_NODE_COSTS[node]


def make_test_handlers(*, eval_seen: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Deterministic node handlers; the evaluator records what it sees."""

    def intake(context: SupervisorContext) -> LoopResult:
        return LoopResult(
            status="continue",
            next_node="worker",
            evidence_refs=["evidence://p3d-isolated/intake"],
            tokens_used=10,
            tool_calls_used=1,
        )

    def worker(context: SupervisorContext) -> LoopResult:
        return LoopResult(
            status="continue",
            next_node="evaluator",
            evidence_refs=["evidence://p3d-isolated/worker"],
            eval_results=plugin_eval(
                "worker", {"worker_self_eval": "worker-claims-excellent"}
            ),
            tokens_used=100,
            tool_calls_used=2,
        )

    def evaluator(context: SupervisorContext) -> LoopResult:
        if eval_seen is not None:
            eval_seen.append(dict(context.checkpoint.eval_results))
        return LoopResult(
            status="continue",
            next_node="feedback",
            evidence_refs=["evidence://p3d-isolated/evaluator"],
            eval_results=plugin_eval("evaluator", {"independent_eval": "pass"}),
            tokens_used=50,
            tool_calls_used=1,
        )

    def feedback(context: SupervisorContext) -> LoopResult:
        return LoopResult(
            status="passed",
            stop_reason="isolated_test_complete",
            tokens_used=10,
            tool_calls_used=0,
        )

    return {
        "intake": intake,
        "worker": worker,
        "evaluator": evaluator,
        "feedback": feedback,
    }


# ---------------------------------------------------------------------------
# Deterministic cyclic LoopSpec for node-revisit regression tests.
# ---------------------------------------------------------------------------

CYCLE_LOOPSPEC = LoopSpec(
    loop_id="p3d-isolated-cycle-v1",
    version="1.0.0",
    goal="P3D 循环回访隔离测试 Loop：确定性、无网络、无模型、无副作用",
    first_node="intake",
    nodes=("intake", "worker"),
    max_iterations=8,
    max_seconds=1800,
    token_limit=50000,
    tool_call_limit=20,
    worker_version="deterministic-test-worker-v1",
    evaluator_version="deterministic-test-evaluator-v1",
)

CYCLE_NODE_COSTS = {
    "intake": CostPlan(tokens=10, tool_calls=1),
    "worker": CostPlan(tokens=20, tool_calls=1),
}

CYCLE_LOOPSPEC_REGISTRY_ENTRY = {
    "spec": {
        "loop_id": CYCLE_LOOPSPEC.loop_id,
        "version": CYCLE_LOOPSPEC.version,
        "goal": CYCLE_LOOPSPEC.goal,
        "first_node": CYCLE_LOOPSPEC.first_node,
        "nodes": list(CYCLE_LOOPSPEC.nodes),
        "max_iterations": CYCLE_LOOPSPEC.max_iterations,
        "max_seconds": CYCLE_LOOPSPEC.max_seconds,
        "token_limit": CYCLE_LOOPSPEC.token_limit,
        "tool_call_limit": CYCLE_LOOPSPEC.tool_call_limit,
        "worker_version": CYCLE_LOOPSPEC.worker_version,
        "evaluator_version": CYCLE_LOOPSPEC.evaluator_version,
    },
    "handlers": list(CYCLE_LOOPSPEC.nodes),
}


def cycle_cost_plan_of(node: str) -> CostPlan:
    return CYCLE_NODE_COSTS[node]


def make_cycle_handlers() -> dict[str, Any]:
    """intake → worker → intake → …（deterministic node revisit）。"""

    def intake(context: SupervisorContext) -> LoopResult:
        return LoopResult(
            status="continue",
            next_node="worker",
            evidence_refs=["evidence://p3d-isolated-cycle/intake"],
            tokens_used=10,
            tool_calls_used=1,
        )

    def worker(context: SupervisorContext) -> LoopResult:
        return LoopResult(
            status="continue",
            next_node="intake",
            evidence_refs=["evidence://p3d-isolated-cycle/worker"],
            tokens_used=20,
            tool_calls_used=1,
        )

    return {"intake": intake, "worker": worker}
