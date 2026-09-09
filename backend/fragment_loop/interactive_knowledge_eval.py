"""Deterministic hard gates for an interactive-knowledge pattern transfer."""

import json
from pathlib import Path
from typing import Any


def evaluate_interactive_knowledge(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    source = data.get("source", {})
    capabilities = set(data.get("capabilities", []))
    transfer = data.get("transfer", {})
    checks = {
        "official_site_locked": source.get("official_site") == "https://aicosmos.ai",
        "media_source_separated": bool(source.get("media_article")),
        "core_capabilities_present": {
            "interactive_video",
            "interactive_notes",
            "collaborative_coding_lab",
        }.issubset(capabilities),
        "transfer_target_defined": bool(transfer.get("target")),
        "experiment_defined": bool(transfer.get("smallest_experiment")),
        "untested_boundary_stated": data.get("product_runtime_tested") is False,
        "limited_maturity_stated": data.get("asset_maturity") == "validated_with_limits",
        "no_external_side_effects": data.get("external_side_effects") is False,
    }
    return {"passed": all(checks.values()), "checks": checks}
