"""G2 物化 current-state 投影（graph_node_state_v2 / graph_edge_state_v2）。

immutable checkpoint_events_v2 仍是权威历史；本模块两张表只是同一
SQLite 事务内可重建的投影：``graph_node_state_v2`` 每边界 UPSERT 当前
节点最新事实（success→failed 由最新 UPSERT 覆盖，last_succeeded_json
独立保留不被 failed 覆盖），``graph_edge_state_v2`` 物化 current 边
投影——每 (run_id, edge_id) 一行，重复 traversal 折叠为 traversals
计数与 last_sequence，行数 ≤ 图边数，绝不随历史增长；完整有序
traversal 审计（含 sequence/condition/feedback/exhausted route/retry）
由 immutable 事件 detail.edges（版本化 graph-edge-delta-v1）经显式
edge_audit 入口重建，不从 current latest 加载。current latest 读取 =
一个 anchor + 节点/边投影索引点查 + ≤N 事件——读取规模只与当前图与
固定窗口相关，与历史长度无关。投影写失败必须与 checkpoint/object/
event/head/reservation 同事务整体回滚。所有 row 带 schema_version
闭集，未知版本稳定 unsupported，零隐式迁移。

兼容红线：ensure_schema 只 CREATE IF NOT EXISTS——对任何旧实验表
（含 R13/R14 形状的 run_edges_v2）零 DROP/ALTER/UPDATE/DELETE/
backfill；旧表行原样保留且本模块永不读取（stable unavailable）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

NODE_PROJECTION_SCHEMA = "storage-v2"
EDGE_DELTA_SCHEMA = "graph-edge-delta-v1"
EDGE_DELTA_FIELDS = ("edge_id", "from", "to", "type", "decision_source", "reason")

NODE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS graph_node_state_v2 (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    output_digest TEXT,
    object_ref TEXT,
    input_digest TEXT,
    last_succeeded_json TEXT,
    error_json TEXT,
    updated_sequence INTEGER NOT NULL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS graph_edge_state_v2 (
    run_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    edge_json TEXT NOT NULL,
    traversals INTEGER NOT NULL,
    last_sequence INTEGER NOT NULL,
    PRIMARY KEY (run_id, edge_id)
);
"""


class NodeProjectionError(ValueError):
    """投影行版本漂移：稳定 unsupported。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def ensure_schema(connection: sqlite3.Connection) -> None:
    # 只 CREATE IF NOT EXISTS：对任何旧实验表（含 R13/R14 形状的
    # run_edges_v2）零 DROP/ALTER/UPDATE/DELETE/backfill，旧行原样保留。
    connection.executescript(NODE_STATE_DDL)


def upsert_node(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    node_id: str,
    status: str,
    attempts: int,
    output_digest: str | None,
    object_ref: str | None,
    input_digest: str | None,
    last_succeeded: dict[str, Any] | None,
    error: Any,
    updated_sequence: int,
) -> None:
    """UPSERT 当前节点最新事实；last_succeeded 独立保留（failed 边界不
    覆盖它，除非新一次成功给出新值）。"""
    connection.execute(
        """
        INSERT INTO graph_node_state_v2 (
            run_id, node_id, schema_version, status, attempts,
            output_digest, object_ref, input_digest, last_succeeded_json,
            error_json, updated_sequence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, node_id) DO UPDATE SET
            schema_version = excluded.schema_version,
            status = excluded.status,
            attempts = excluded.attempts,
            output_digest = excluded.output_digest,
            object_ref = excluded.object_ref,
            input_digest = excluded.input_digest,
            last_succeeded_json = COALESCE(
                excluded.last_succeeded_json,
                graph_node_state_v2.last_succeeded_json
            ),
            error_json = excluded.error_json,
            updated_sequence = excluded.updated_sequence
        """,
        (
            run_id,
            node_id,
            NODE_PROJECTION_SCHEMA,
            status,
            attempts,
            output_digest,
            object_ref,
            input_digest,
            None if last_succeeded is None else _canonical(last_succeeded),
            None if error is None else _canonical(error),
            updated_sequence,
        ),
    )


def upsert_edge(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    edge: dict[str, Any],
    run_sequence: int,
) -> None:
    """物化 current 边投影：每 (run_id, edge_id) 一行；同一小图的
    retry/feedback/repeated traversal 折叠为 traversals+1 与
    last_sequence——行数 ≤ 图边数，与历史 traversal 数无关。"""
    connection.execute(
        """
        INSERT INTO graph_edge_state_v2 (
            run_id, edge_id, schema_version, edge_json, traversals,
            last_sequence
        ) VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(run_id, edge_id) DO UPDATE SET
            schema_version = excluded.schema_version,
            edge_json = excluded.edge_json,
            traversals = graph_edge_state_v2.traversals + 1,
            last_sequence = excluded.last_sequence
        """,
        (
            run_id,
            str(edge.get("edge_id")),
            NODE_PROJECTION_SCHEMA,
            _canonical(edge),
            run_sequence,
        ),
    )


def mark_node(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    node_id: str,
    status: str,
    updated_sequence: int,
) -> None:
    """调度状态标记（ready/skipped/pending 重置）：新行插默认值；已有行
    只更新 status/error/sequence，保留 attempts/output/last_succeeded
    等事实列（与 v1 重置语义一致：重置清 error 但不清执行事实）。"""
    connection.execute(
        """
        INSERT INTO graph_node_state_v2 (
            run_id, node_id, schema_version, status, attempts,
            output_digest, object_ref, input_digest, last_succeeded_json,
            error_json, updated_sequence
        ) VALUES (?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?)
        ON CONFLICT(run_id, node_id) DO UPDATE SET
            schema_version = excluded.schema_version,
            status = excluded.status,
            error_json = NULL,
            updated_sequence = excluded.updated_sequence
        """,
        (run_id, node_id, NODE_PROJECTION_SCHEMA, status, updated_sequence),
    )


def load_nodes(connection: sqlite3.Connection, run_id: str) -> dict[str, dict[str, Any]]:
    """当前节点投影索引点查：O(节点数)，与历史长度无关；版本闭集。"""
    rows = connection.execute(
        "SELECT * FROM graph_node_state_v2 WHERE run_id = ?", (run_id,)
    ).fetchall()
    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row["schema_version"]) != NODE_PROJECTION_SCHEMA:
            raise NodeProjectionError("unsupported_schema")
        node: dict[str, Any] = {
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "output_digest": row["output_digest"],
            "output_ref": row["object_ref"],
            "input_digest": row["input_digest"],
        }
        if row["last_succeeded_json"] is not None:
            node["last_succeeded"] = json.loads(str(row["last_succeeded_json"]))
        if row["error_json"] is not None:
            node["error"] = json.loads(str(row["error_json"]))
        nodes[str(row["node_id"])] = node
    return nodes


def load_edges(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """current 边投影点查：每 edge_id 一行（去重集合，附 traversals 折叠
    计数与 last_sequence），O(图边数)，与历史长度无关；版本闭集。
    完整有序 traversal 审计不在此读取——由 immutable 事件经显式
    history/detail 入口按需提供。"""
    rows = connection.execute(
        "SELECT edge_json, schema_version, traversals, last_sequence"
        " FROM graph_edge_state_v2 WHERE run_id = ?"
        " ORDER BY last_sequence, edge_id",
        (run_id,),
    ).fetchall()
    edges: list[dict[str, Any]] = []
    for row in rows:
        if str(row["schema_version"]) != NODE_PROJECTION_SCHEMA:
            raise NodeProjectionError("unsupported_schema")
        edge = dict(json.loads(str(row["edge_json"])))
        edge["traversals"] = int(row["traversals"])
        edge["last_sequence"] = int(row["last_sequence"])
        edges.append(edge)
    return edges


def edge_audit(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """显式 history 入口：从 immutable checkpoint_events_v2 的
    detail.edges（graph-edge-delta-v1）完整有序重建逐 traversal 审计——
    每条 delta 还原为 v1 edges_taken 条目形状（edge_id/from/to/type/
    decision_source/reason），与实际 traversals 一一对应；事件行与
    delta 双版本闭集，未知版本稳定 unsupported。此入口按显式调用读取
    全量事件，绝不用于 current latest。"""
    rows = connection.execute(
        "SELECT run_sequence, detail_json, schema_version"
        " FROM checkpoint_events_v2 WHERE run_id = ? ORDER BY run_sequence",
        (run_id,),
    ).fetchall()
    audit: list[dict[str, Any]] = []
    for row in rows:
        if row["schema_version"] is None or str(row["schema_version"]) != (
            NODE_PROJECTION_SCHEMA
        ):
            raise NodeProjectionError("unsupported_schema")
        detail = json.loads(str(row["detail_json"]))
        for delta in detail.get("edges") or []:
            if not isinstance(delta, dict) or delta.get("schema") != EDGE_DELTA_SCHEMA:
                raise NodeProjectionError("unsupported_schema")
            audit.append({field: delta.get(field) for field in EDGE_DELTA_FIELDS})
    return audit


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
