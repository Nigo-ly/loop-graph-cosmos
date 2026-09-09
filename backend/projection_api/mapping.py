"""Frozen status -> display_state mapping (contract v2).

The raw database ``status`` is always passed through verbatim; display_state
is derived ONLY from this table. Unknown statuses map to "awaiting_human".
"""

from __future__ import annotations

DISPLAY_STATE_BY_STATUS: dict[str, str] = {
    # queued (待路由)
    "requested": "queued",
    "preflight_passed": "queued",
    "approved": "queued",
    # running (运行中)
    "running": "running",
    # awaiting_human (等待人工)
    "escalated": "awaiting_human",
    # stopped (已停止)
    "exhausted": "stopped",
    "blocked": "stopped",
    "cancelled": "stopped",
    "superseded": "stopped",
    "failed_safe": "stopped",
    # completed (已完成)
    "passed": "completed",
}

NEXT_STEP_BY_STATUS: dict[str, str] = {
    "approved": "选择业务 LoopSpec 与独立 Verifier",
    "preflight_passed": "等待 nigo-loop 执行批准",
    "requested": "等待安全预检",
}


def display_state_for(status: str | None) -> str:
    """Return the contract display_state for a raw status."""
    return DISPLAY_STATE_BY_STATUS.get(str(status), "awaiting_human")


def next_step_for(status: str | None) -> str | None:
    """Return the human next-step hint for queued statuses, else None."""
    return NEXT_STEP_BY_STATUS.get(str(status))
