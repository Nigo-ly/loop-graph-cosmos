"""G3 ResultUnit 指标与 micro 投影（Design §10，自 result_unit.py 拆出）。

micro timeline：三 channel（checkpoint_event / reservation / agent_ledger）
各自按权威顺序排列并给出 channel 内序号——不伪造跨 channel 单一时序；
agent_ledger 只投影脱敏白名单字段（status/request_sent/error/digest/
tokens，绝无 prompt/response 正文）。metrics：logical bytes 只计本 run
引用对象；handler calls 给出首次/recovery/失败/feedback 分类与恒等式；
physical 报当前 main/WAL 字节（baseline/final 增量由调用方量取）。
0 分母一律 None（N/A）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from graph_runtime.result_unit import (
    _canonical,
    _parse_rfc3339,
    _sha256,
    get_unit,
    project_status,
    units_for_run,
)

# agent ledger micro 投影的脱敏白名单：无 prompt/response/result 正文。
_LEDGER_SAFE_FIELDS = (
    "session_id",
    "node_id",
    "adapter",
    "provider",
    "model",
    "status",
    "request_sent",
    "error_category",
    "authorization_digest",
    "spec_digest",
    "input_digest",
    "result_digest",
    "max_input_tokens",
    "max_output_tokens",
    "actual_input_tokens",
    "actual_output_tokens",
)


def micro_events(
    connection: sqlite3.Connection, run_id: str, macro_node_id: str | None = None
) -> list[dict[str, Any]]:
    """宏节点可展开 micro timeline：三 channel 各自权威顺序（checkpoint
    按 run_sequence、reservation 按 created_at、agent_ledger 按
    reserved_at），channel_sequence 只在 channel 内单调——返回按 channel
    分组拼接，不伪装成单一时间序列。"""
    items: list[dict[str, Any]] = []
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    rows = (
        connection.execute(
            "SELECT * FROM checkpoint_events_v2 WHERE run_id = ?"
            " AND (? IS NULL OR node_id = ?) ORDER BY run_sequence",
            (run_id, macro_node_id, macro_node_id),
        ).fetchall()
        if "checkpoint_events_v2" in tables
        else []
    )
    for index, row in enumerate(rows, start=1):
        items.append(
            {
                "macro_node_id": row["node_id"],
                "channel": "checkpoint_event",
                "channel_sequence": index,
                "event_type": row["event_type"],
                "detail": json.loads(str(row["detail_json"])),
                "digest": _sha256(_canonical(dict(row))),
            }
        )
    reservations = (
        connection.execute(
            "SELECT * FROM execution_reservations_v1 WHERE run_id = ?"
            " AND runtime_kind = 'graph'"
            " AND (? IS NULL OR node_id = ?) ORDER BY created_at, execution_id",
            (run_id, macro_node_id, macro_node_id),
        ).fetchall()
        if "execution_reservations_v1" in tables
        else []
    )
    for index, row in enumerate(reservations, start=1):
        items.append(
            {
                "macro_node_id": row["node_id"],
                "channel": "reservation",
                "channel_sequence": index,
                "event_type": f"reservation_{row['status']}",
                "detail": {
                    "execution_id": row["execution_id"],
                    "execution_class": row["execution_class"],
                    "handler_entry_count": int(row["handler_entry_count"]),
                },
                "digest": _sha256(_canonical(dict(row))),
            }
        )
    ledger = (
        connection.execute(
            "SELECT * FROM graph_agent_calls_v1 WHERE run_id = ?"
            " AND (? IS NULL OR node_id = ?) ORDER BY reserved_at, session_id",
            (run_id, macro_node_id, macro_node_id),
        ).fetchall()
        if "graph_agent_calls_v1" in tables
        else []
    )
    for index, row in enumerate(ledger, start=1):
        items.append(
            {
                "macro_node_id": row["node_id"],
                "channel": "agent_ledger",
                "channel_sequence": index,
                "event_type": f"agent_{row['status']}",
                "detail": {
                    field: row[field] for field in _LEDGER_SAFE_FIELDS if field in row.keys()
                },
                "digest": _sha256(_canonical(dict(row))),
            }
        )
    return items


def _seconds_between(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _earliest_created_at(units: list[dict[str, Any]]) -> str | None:
    """按 timezone-aware 瞬时值取最早 created_at（跨 offset 语义比较，
    返回原字符串——不改写存储格式）；空集返回 None（0 分母 N/A 口径）。"""
    best: str | None = None
    best_at: datetime | None = None
    for unit in units:
        at = _parse_rfc3339(str(unit["created_at"]))
        if best_at is None or at < best_at:
            best, best_at = str(unit["created_at"]), at
    return best


def _span(start: Any, end: Any) -> float | None:
    return None if start is None or end is None else _seconds_between(str(start), str(end))


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _physical_bytes(connection: sqlite3.Connection) -> tuple[int, int]:
    """当前 main/WAL 字节（baseline/final 增量由调用方量取）。"""
    main_path = ""
    for row in connection.execute("PRAGMA database_list"):
        if str(row[1]) == "main":
            main_path = str(row[2])
            break
    if not main_path:
        return 0, 0
    main = Path(main_path)
    main_bytes = main.stat().st_size if main.exists() else 0
    wal = main.with_name(main.name + "-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    return main_bytes, wal_bytes


def metrics_for_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    now: str,
    physical_baseline: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """G3-13..G3-19 指标（Design §10）：全部可从原始行独立复算；
    0 分母一律 None（N/A）。physical_baseline=(main, wal) 为调用方在
    run 前量取的整库字节——delta 是整库口径增量，不可归因到单 run。"""
    units = units_for_run(connection, run_id)
    valid = [u for u in units if u["status"] != "pending"]
    registration_row = connection.execute(
        "SELECT MIN(committed_at) AS first FROM checkpoints WHERE run_id = ?", (run_id,)
    ).fetchone()
    registration_at = None if registration_row is None else registration_row["first"]
    first_valid_at = _earliest_created_at(valid)
    gaps = [u for u in units if u["kind"] == "Gap"]
    first_gap_at = _earliest_created_at(gaps)
    transitions = int(
        connection.execute(
            "SELECT COUNT(*) AS c FROM checkpoints WHERE run_id = ?", (run_id,)
        ).fetchone()["c"]
    )
    event_rows = connection.execute(
        "SELECT detail_json FROM checkpoint_events_v2 WHERE run_id = ?", (run_id,)
    ).fetchall()
    snapshot_rows = connection.execute(
        "SELECT state_json FROM checkpoint_snapshots_v2 WHERE run_id = ?", (run_id,)
    ).fetchall()
    # logical object bytes 只计本 run 引用对象（digest 去重），不统计整库。
    object_bytes = int(
        connection.execute(
            "SELECT COALESCE(SUM(o.byte_size), 0) AS b FROM content_objects_v1 o"
            " WHERE o.digest IN (SELECT DISTINCT object_ref FROM"
            " checkpoint_events_v2 WHERE run_id = ? AND object_ref IS NOT NULL)",
            (run_id,),
        ).fetchone()["b"]
    )
    logical_bytes = sum(
        len(str(r["detail_json"]).encode("utf-8")) for r in event_rows
    ) + sum(len(str(r["state_json"]).encode("utf-8")) for r in snapshot_rows) + object_bytes
    valid_count = len(valid)
    claims = [u for u in units if u["kind"] == "Claim"]
    covered = sum(
        1
        for claim in claims
        if any(
            (ev := get_unit(connection, ref)) is not None
            and ev["kind"] == "Evidence"
            and project_status(ev, now=now) == "available"
            for ref in claim["evidence_refs"]
        )
    )
    decided = [
        u
        for u in units
        if u["kind"] == "Decision"
        and u["status"] == "decided"
        and u["created_by"]["type"] == "human"
    ]
    open_gaps = [u for u in gaps if project_status(u, now=now) == "open"]
    resolved_gaps = [u for u in gaps if u["status"] == "resolved"]
    artifacts = [u for u in units if u["kind"] == "Artifact"]
    reuse_rows = connection.execute(
        "SELECT from_unit_id, to_unit_id FROM result_unit_edges_v1"
        " WHERE edge_type = 'reuses'"
    ).fetchall()
    run_of = {u["unit_id"]: u["source"]["run_id"] for u in units}
    reused_artifacts = {
        str(row["to_unit_id"])
        for row in reuse_rows
        if run_of.get(str(row["to_unit_id"])) == run_id
        and run_of.get(str(row["from_unit_id"])) != run_id  # 跨 run 复用
    }
    handler = connection.execute(
        "SELECT COALESCE(SUM(handler_entry_count), 0) AS calls,"
        " COUNT(*) AS reservations,"
        " COALESCE(SUM(CASE WHEN status LIKE 'failed%' THEN 1 ELSE 0 END), 0) AS failed"
        " FROM execution_reservations_v1 WHERE run_id = ? AND runtime_kind = 'graph'",
        (run_id,),
    ).fetchone()
    handler_calls = int(handler["calls"])
    first_entries = int(handler["reservations"])
    recovery_entries = handler_calls - first_entries
    feedback_events = int(
        connection.execute(
            "SELECT COUNT(*) AS c FROM checkpoint_events_v2"
            " WHERE run_id = ? AND event_type = 'feedback_traversed'",
            (run_id,),
        ).fetchone()["c"]
    )
    main_bytes, wal_bytes = _physical_bytes(connection)
    main_delta = (
        None if physical_baseline is None else main_bytes - int(physical_baseline[0])
    )
    wal_delta = (
        None if physical_baseline is None else wal_bytes - int(physical_baseline[1])
    )
    return {
        "run_id": run_id,
        "first_valid_result_seconds": _span(registration_at, first_valid_at),
        "first_gap_seconds": _span(registration_at, first_gap_at),
        "valid_result_units": valid_count,
        "runtime_transitions": transitions,
        "event_count": len(event_rows),
        "checkpoints_per_valid_result": _ratio(transitions, valid_count),
        "bytes_per_valid_result": _ratio(logical_bytes, valid_count),
        "logical_bytes": logical_bytes,
        "physical_main_bytes": main_bytes,
        "physical_wal_bytes": wal_bytes,
        # 整库口径增量（page/WAL 阶跃）：不可归因到单 run（见模块 docstring）
        "physical_main_delta": main_delta,
        "physical_wal_delta": wal_delta,
        "human_decision_commits": len(decided),
        "human_decisions_per_valid_result": _ratio(len(decided), valid_count),
        "evidence_coverage": _ratio(covered, len(claims)),
        "claims": len(claims),
        "covered_claims": covered,
        "open_gaps": len(open_gaps),
        "resolved_gaps": len(resolved_gaps),
        "open_gap_ratio": _ratio(len(open_gaps), len(claims) + len(gaps)),
        "artifacts_qualified": sum(1 for u in artifacts if u["status"] == "qualified"),
        "artifacts_reusable": sum(1 for u in artifacts if u["status"] == "reusable"),
        "artifacts_candidate": sum(1 for u in artifacts if u["status"] == "candidate"),
        "reuses_edges": len(reuse_rows),
        "reused_artifacts_cross_run": len(reused_artifacts),
        "reservation_handler_calls": handler_calls,
        "handler_first_entries": first_entries,
        "handler_recovery_entries": recovery_entries,
        "handler_failed_reservations": int(handler["failed"]),
        "handler_feedback_events": feedback_events,
    }
