"""R1-H isolated synthetic experience for the cognitive draft loop.

This module creates one fixed synthetic research draft in a caller-selected
database.  It does not read the production Loop database, private content,
credentials, environment configuration, or any network service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from common.checkpoint import SQLiteCheckpointStore
from fragment_loop.cognitive_contract import render_cognitive_result
from fragment_loop.cognitive_loop import SyntheticCognitiveLoop
from fragment_loop.cognitive_server import make_cognitive_decision_server
from fragment_loop.spec import FRAGMENT_COGNITIVE_SYNTHETIC_R1B
from projection_api.server import make_server as make_projection_server
from projection_api.store import latest_runs

DEMO_FRAGMENT_ID = "synthetic-r1h-research-demo"
DEMO_FRAGMENT_TEXT = "合成研究团队计划用通用工具研究咖啡烘焙行业。"
DEMO_RUN_ID = "cognitive-r1b-" + hashlib.sha256(DEMO_FRAGMENT_ID.encode()).hexdigest()


def _demo_result(fragment_text: str, route: str) -> dict[str, object]:
    if fragment_text != DEMO_FRAGMENT_TEXT or route != "research":
        raise ValueError("R1-H producer only accepts the frozen synthetic fixture")
    perspectives: list[dict[str, object]] = [
        {
            "perspective": perspective,
            "summary": f"{perspective} 合成视角暂无材料",
            "association": "只与合成原文相关",
            "conflicts": [],
            "value": "保留待验证问题",
            "uncertainty": "没有真实外部或私人材料",
            "materials": [],
        }
        for perspective in ("memory", "knowledge_base", "frontier")
    ]
    return {
        "route": "research",
        "route_reason": "包含行业研究能力主张，需要研究路线",
        "route_change_allowed": True,
        "summary": {
            "text": DEMO_FRAGMENT_TEXT,
            "source_quote": DEMO_FRAGMENT_TEXT,
            "transformation_basis": "摘要与合成原文一致",
        },
        "semantic_expansion": {
            "literal_facts": [
                {"text": "合成研究团队", "source_quote": "合成研究团队"},
                {"text": "研究咖啡烘焙行业", "source_quote": "研究咖啡烘焙行业"},
            ],
            "inferences": [],
            "uncertainties": ["通用工具的行业研究能力尚未被证据覆盖"],
        },
        "research": {
            "questions": ["通用工具是否能支持咖啡烘焙行业研究？"],
            "search_dimensions": ["合成咖啡烘焙行业材料"],
            "sources": [],
            "counter_evidence_search": {
                "status": "not_found",
                "scope": "只检查合成材料中的相反观点",
                "findings": [],
            },
            "v1_claims": [
                {
                    "claim_id": "C1",
                    "claim_kind": "general_fact",
                    "text": "通用工具已经具备咖啡烘焙行业研究能力",
                    "source_quote": "通用工具",
                    "verdict": "not_covered",
                    "confidence": "low",
                    "evidence_refs": [],
                    "evidence_support": [],
                    "claim_derivation": "从合成原文的工具计划拆出待验证能力主张",
                    "verdict_basis": "没有合成外部证据覆盖该能力",
                    "independent_verification": {
                        "status": "not_verified",
                        "mode": "none",
                        "basis": "没有合成证据，不能独立验证",
                        "evidence_refs": [],
                    },
                }
            ],
            "v2_revisions": [
                {
                    "claim_id": "C1",
                    "revision_status": "unchanged",
                    "deviation": "V1 已明确标记为未覆盖",
                    "revision_reason": "没有新合成材料可改变判定",
                    "revised_text": "通用工具已经具备咖啡烘焙行业研究能力",
                }
            ],
        },
        "perspectives": perspectives,
        "synthesis": {
            "value": "可作为后续研究问题",
            "weakest_link": "没有外部证据",
            "conflicts": [],
            "open_questions": ["通用工具的行业研究能力是否存在？"],
            "next_step": "等待人工决定是否保留",
            "credibility": "insufficient",
            "credibility_basis": "唯一主张未被证据覆盖",
            "claim_counts": {
                "supported": 0,
                "partially_supported": 0,
                "contradicted": 0,
                "not_covered": 1,
            },
        },
    }


def _validate_demo_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.name.startswith("fragment-cognitive-r1h-") or resolved.suffix != ".sqlite3":
        raise ValueError("R1-H database filename must be clearly synthetic")
    if path.is_symlink():
        raise ValueError("R1-H database path must not be a symbolic link")
    return resolved


def _existing_database_is_demo(path: Path) -> bool:
    try:
        rows = latest_runs(str(path))
    except sqlite3.Error:
        return False
    if not (
        len(rows) == 1
        and rows[0].run_id == DEMO_RUN_ID
        and rows[0].fragment_id == DEMO_FRAGMENT_ID
        and rows[0].loop_id == FRAGMENT_COGNITIVE_SYNTHETIC_R1B.loop_id
    ):
        return False
    payload = rows[0].payload
    eval_results = payload.get("eval_results")
    if not isinstance(eval_results, dict):
        return False
    expected_result = _demo_result(DEMO_FRAGMENT_TEXT, "research")
    expected_input = {
        "source_kind": "synthetic_fixture",
        "fragment_text": DEMO_FRAGMENT_TEXT,
        "expected_route": "research",
    }
    try:
        expected_markdown = render_cognitive_result(expected_result, DEMO_FRAGMENT_TEXT)
    except (TypeError, ValueError):
        return False
    state = (
        rows[0].status,
        payload.get("stop_reason"),
        eval_results.get("cognitive_decision"),
        eval_results.get("content_lifecycle"),
    )
    allowed_states = {
        ("paused", "awaiting_cognitive_decision", "pending", "draft"),
        ("passed", "cognitive_draft_kept", "keep_draft", "draft"),
        ("cancelled", "cognitive_result_rejected", "reject", "rejected"),
    }
    return (
        payload.get("loopspec_version") == FRAGMENT_COGNITIVE_SYNTHETIC_R1B.version
        and eval_results.get("cognitive_input") == expected_input
        and eval_results.get("cognitive_contract_status") == "validated"
        and eval_results.get("cognitive_route") == "research"
        and eval_results.get("cognitive_result") == expected_result
        and eval_results.get("cognitive_markdown") == expected_markdown
        and eval_results.get("evidence_level") == "unverified"
        and state in allowed_states
    )


def prepare_demo(db_path: str | Path) -> dict[str, object]:
    """Create or recover the one fixed R1-H synthetic draft database."""
    path = _validate_demo_path(Path(db_path))
    if path.exists() and not _existing_database_is_demo(path):
        raise ValueError("R1-H refuses an existing non-demo database")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise ValueError("R1-H database appeared concurrently") from error
        else:
            os.close(descriptor)
    try:
        service = SyntheticCognitiveLoop(SQLiteCheckpointStore(path), _demo_result)
        registered = service.register(
            fragment_id=DEMO_FRAGMENT_ID,
            fragment_text=DEMO_FRAGMENT_TEXT,
            route="research",
        )
        waiting = service.run(registered.run_id)
    except (sqlite3.Error, ValueError) as error:
        raise ValueError("R1-H synthetic demo database is invalid") from error
    return {
        "db_path": str(path),
        "run_id": waiting.run_id,
        "fragment_id": waiting.fragment_id,
        "status": waiting.status,
        "stop_reason": waiting.stop_reason,
        "cognitive_decision": waiting.eval_results.get("cognitive_decision"),
        "content_lifecycle": waiting.eval_results.get("content_lifecycle"),
        "evidence_level": waiting.eval_results.get("evidence_level"),
    }


def _no_generation(_fragment_text: str, _route: str) -> dict[str, object]:
    raise RuntimeError("R1-H demo server cannot generate cognitive content")


def make_demo_servers(
    db_path: str | Path,
    *,
    projection_port: int = 5679,
    decision_port: int = 5684,
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """Bind the existing projection and decision APIs to one validated demo DB."""
    path = _validate_demo_path(Path(db_path))
    if not path.is_file() or not _existing_database_is_demo(path):
        raise ValueError("R1-H refuses to serve a non-demo database")
    service = SyntheticCognitiveLoop(SQLiteCheckpointStore(path), _no_generation)
    projection = make_projection_server(str(path), port=projection_port)
    try:
        decisions = make_cognitive_decision_server(service, port=decision_port)
    except Exception:
        projection.server_close()
        raise
    return projection, decisions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragment_loop.cognitive_demo",
        description="Prepare or serve the isolated R1-H synthetic experience",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument(
        "--db",
        required=True,
        help="dedicated fragment-cognitive-r1h-*.sqlite3 path",
    )
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument(
        "--db",
        required=True,
        help="dedicated fragment-cognitive-r1h-*.sqlite3 path",
    )
    serve_parser.add_argument("--projection-port", type=int, default=5679)
    serve_parser.add_argument("--decision-port", type=int, default=5684)
    args = parser.parse_args(argv)
    prepared = prepare_demo(args.db)
    if args.command == "prepare":
        print(json.dumps(prepared, ensure_ascii=False, sort_keys=True))
        return 0

    projection, decisions = make_demo_servers(
        args.db,
        projection_port=args.projection_port,
        decision_port=args.decision_port,
    )
    projection_port = int(projection.server_address[1])
    decision_port = int(decisions.server_address[1])
    projection_thread = threading.Thread(target=projection.serve_forever, daemon=True)
    projection_thread.start()
    print(
        json.dumps(
            {
                **prepared,
                "projection_url": f"http://127.0.0.1:{projection_port}/loop/v1",
                "decision_url": (
                    f"http://127.0.0.1:{decision_port}/fragment-cognitive/v1"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        decisions.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        decisions.server_close()
        projection.shutdown()
        projection.server_close()
        projection_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
