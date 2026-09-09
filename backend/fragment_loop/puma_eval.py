"""Deterministic hard gates for the PUMA stop-policy transfer experiment."""

import json
from pathlib import Path
from typing import Any


def evaluate_transfer(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    source = data.get("source", {})
    policy = data.get("transfer_policy", {})
    plan = data.get("eval_plan", {})
    checks = {
        "official_paper_locked": source.get("arxiv") == "2605.17672",
        "official_repo_locked": bool(source.get("commit")),
        "offline_boundary_stated": source.get("released_mode") == "offline",
        "progress_signal_present": bool(policy.get("progress_signal")),
        "answer_verification_present": bool(policy.get("answer_verification")),
        "loop_breaker_present": bool(policy.get("loop_breaker")),
        "eval_layers_present": all(key in plan for key in ("foundation", "business", "holdout")),
        "no_external_side_effects": data.get("external_side_effects") is False,
    }
    return {"passed": all(checks.values()), "checks": checks}
