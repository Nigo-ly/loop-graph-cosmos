"""Deterministic evaluator for external-skill capability mappings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EXPECTED_SKILLS = {
    "google-agents-cli-workflow",
    "google-agents-cli-adk-code",
    "google-agents-cli-scaffold",
    "google-agents-cli-eval",
    "google-agents-cli-deploy",
    "google-agents-cli-publish",
    "google-agents-cli-observability",
}
CLASSIFICATIONS = {"direct", "adapt", "google_specific"}


def evaluate_mapping(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("skills", [])
    by_name = {row.get("name"): row for row in rows if isinstance(row, dict)}
    checks = {
        "exact_official_inventory": set(by_name) == EXPECTED_SKILLS,
        "version_locked": bool(rows)
        and all(row.get("version") == "1.1.0" for row in rows),
        "classification_valid": bool(rows)
        and all(row.get("classification") in CLASSIFICATIONS for row in rows),
        "evidence_bound": bool(rows)
        and all(row.get("source") and row.get("verified_points") for row in rows),
        "adoption_boundary_present": bool(rows)
        and all(row.get("reuse") and row.get("limits") for row in rows),
        "publish_not_portable": by_name.get("google-agents-cli-publish", {}).get(
            "classification"
        )
        == "google_specific",
        "no_external_side_effects": data.get("external_side_effects") is False,
        "no_install_or_deploy": not any(
            action in {"install", "login", "deploy", "publish", "create_resource"}
            for action in data.get("actions_executed", [])
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "skill_count": len(rows),
    }
