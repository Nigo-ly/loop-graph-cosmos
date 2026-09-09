#!/usr/bin/env python3
"""Register approved phone fragments with the local Loop Supervisor."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.checkpoint import SQLiteCheckpointStore  # noqa: E402
from common.supervisor import AdmissionState  # noqa: E402
from fragment_loop.cognitive_loop import LocalCognitiveV1Loop  # noqa: E402
from fragment_loop.intake import fragment_title_for, load_fragment  # noqa: E402
from fragment_loop.runtime import DEFAULT_DB_PATH, capture_identity_sha256  # noqa: E402
from fragment_loop.spec import (  # noqa: E402
    FRAGMENT_COGNITIVE_LOCAL_V1,
    PHONE_FRAGMENT_LINK_V1,
)
from scripts.harvest_dashboard import latest_runs, render  # noqa: E402

DEFAULT_SOURCE = Path(
    "/example/vault/Notes/散记/碎片想法"
)
DEFAULT_STATUS = ROOT / "data" / "registrar_status.json"
DEFAULT_DASHBOARD = Path("/example/vault/04 项目/Loop Harvest 审核台.md")
COGNITIVE_V1_CUTOVER_AT = datetime(2026, 8, 3, 7, 0, tzinfo=UTC)


def _registrar_does_not_execute(
    _fragment_text: str, _route: str
) -> Mapping[str, object]:
    raise RuntimeError("registrar never executes cognitive producers")


def _is_cognitive_v1_cutover_fragment(captured_at: str | None) -> bool:
    if not captured_at:
        return False
    try:
        value = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return value.tzinfo is not None and value >= COGNITIVE_V1_CUTOVER_AT


def scan_fragments(
    source: Path, database: Path, *, min_age_seconds: float = 0
) -> dict[str, Any]:
    store = SQLiteCheckpointStore(database)
    service = LocalCognitiveV1Loop(store, _registrar_does_not_execute)
    result: dict[str, Any] = {
        "scanned_at": datetime.now(UTC).isoformat(),
        "source": str(source),
        "registered": [],
        "existing": [],
        "historical": [],
        "deferred": [],
        "preflight_failed": [],
        "binding_conflicts": [],
        "errors": [],
    }
    for path in sorted(source.glob("*.md")):
        try:
            age_seconds = time.time() - path.stat().st_mtime
            if age_seconds < min_age_seconds:
                result["deferred"].append({"path": str(path)})
                continue
            original_bytes = path.read_bytes()
            fragment = load_fragment(path)
            if path.read_bytes() != original_bytes:
                raise ValueError("fragment changed during intake")
            if fragment.admission_state is not AdmissionState.REQUESTED:
                continue
            if fragment.privacy_level in {"sensitive", "restricted"}:
                result["preflight_failed"].append(
                    {
                        "fragment_id": fragment.fragment_id,
                        "reasons": [
                            "sensitive or restricted fragments require manual handling"
                        ],
                    }
                )
                continue
            existing = store.latest_for_fragment(
                FRAGMENT_COGNITIVE_LOCAL_V1.loop_id, fragment.fragment_id
            )
            if existing is None:
                legacy = store.latest_for_fragment(
                    PHONE_FRAGMENT_LINK_V1.loop_id, fragment.fragment_id
                )
                if legacy is not None and not _is_cognitive_v1_cutover_fragment(
                    fragment.captured_at
                ):
                    result["historical"].append(
                        {
                            "fragment_id": fragment.fragment_id,
                            "run_id": legacy.run_id,
                            "loop_id": legacy.loop_id,
                            "status": legacy.status,
                        }
                    )
                    continue
            title, title_source = fragment_title_for(fragment)
            checkpoint = service.register_intake(
                fragment_id=fragment.fragment_id,
                fragment_text=fragment.raw_content,
                source_ref=fragment.source_path,
                privacy_level=fragment.privacy_level,
                fragment_title=title,
                fragment_title_source=title_source,
                captured_at=fragment.captured_at,
                raw_sha256=hashlib.sha256(original_bytes).hexdigest(),
                identity_sha256=capture_identity_sha256(
                    fragment.fragment_id, original_bytes.decode("utf-8")
                ),
            )
            record: dict[str, Any] = {
                "fragment_id": fragment.fragment_id,
                "run_id": checkpoint.run_id,
                "loop_id": checkpoint.loop_id,
                "status": checkpoint.status,
            }
            if existing is None:
                result["registered"].append(record)
            else:
                result["existing"].append(record)
        except ValueError as error:
            if str(error) == "fragment_id 已绑定不同的本地认知输入":
                result["binding_conflicts"].append(
                    {
                        "path": str(path),
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                )
                continue
            result["errors"].append(
                {
                    "path": str(path),
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )
        except Exception as error:  # continue scanning; status remains auditable
            result["errors"].append(
                {
                    "path": str(path),
                    "type": type(error).__name__,
                    "message": str(error),
                }
            )
    return result


def write_status(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def refresh_dashboard(path: Path, database: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(render(latest_runs(database)), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    parser.add_argument("--min-age-seconds", type=float, default=2.0)
    args = parser.parse_args()
    result = scan_fragments(
        args.source.expanduser(),
        args.db.expanduser(),
        min_age_seconds=max(0.0, args.min_age_seconds),
    )
    write_status(args.status.expanduser(), result)
    refresh_dashboard(args.dashboard.expanduser(), args.db.expanduser())
    print(json.dumps(result, ensure_ascii=False))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
