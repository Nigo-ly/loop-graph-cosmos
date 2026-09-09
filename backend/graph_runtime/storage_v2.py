"""G2 checkpoint v2 sidecar storage（Design §7-§8，additive only）。

旁路表 checkpoint_events_v2/checkpoint_snapshots_v2/content_objects_v1/
run_heads_v2 + schema manifest；历史表零 ALTER/UPDATE/DELETE/backfill。
v2 仅显式选择的合成/新测试 run；stub checkpoint 行保持既有 CAS 机制，
状态由 v2 表 + graph_node_state_v2/graph_edge_state_v2 物化投影重建。
回滚 = freeze writer + dual-reader，不 drop、不改历史。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from graph_runtime import node_state_v2, result_unit

STORAGE_SCHEMA = "storage-v2"
STUB_SCHEMA_VERSION = 2
SNAPSHOT_INTERVAL = 16
OBJECT_MEDIA_TYPE = "application/json"

V2_DDL = """
CREATE TABLE IF NOT EXISTS checkpoint_events_v2 (
    run_id TEXT NOT NULL,
    run_sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    node_id TEXT,
    status TEXT,
    output_digest TEXT,
    object_ref TEXT,
    input_digest TEXT,
    attempt INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    tool_calls_used INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    schema_version TEXT,
    UNIQUE(run_id, run_sequence)
);
CREATE INDEX IF NOT EXISTS checkpoint_events_v2_run
    ON checkpoint_events_v2(run_id, run_sequence);
CREATE TABLE IF NOT EXISTS checkpoint_snapshots_v2 (
    run_id TEXT NOT NULL,
    run_sequence INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    schema_version TEXT,
    UNIQUE(run_id, run_sequence)
);
CREATE INDEX IF NOT EXISTS checkpoint_snapshots_v2_run
    ON checkpoint_snapshots_v2(run_id, run_sequence DESC);
CREATE TABLE IF NOT EXISTS content_objects_v1 (
    digest TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_heads_v2 (
    run_id TEXT PRIMARY KEY,
    runtime_kind TEXT NOT NULL,
    loop_id TEXT NOT NULL,
    fragment_id TEXT NOT NULL,
    run_family_id TEXT NOT NULL,
    episode INTEGER NOT NULL DEFAULT 0,
    latest_sequence INTEGER NOT NULL,
    status TEXT NOT NULL,
    current_node TEXT,
    pending_human INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    schema_version TEXT
);
CREATE INDEX IF NOT EXISTS run_heads_v2_family
    ON run_heads_v2(loop_id, fragment_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS run_heads_v2_episode
    ON run_heads_v2(run_family_id, episode DESC);
CREATE TABLE IF NOT EXISTS storage_v2_manifest (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_STATE_REF_KEYS = ("output", "last_succeeded")
ANCHOR_SCHEMA = "graph-anchor-v1"


def anchor_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": ANCHOR_SCHEMA,
        "graph_id": state.get("graph_id"),
        "spec_version": state.get("spec_version"),
        "spec_digest": state.get("spec_digest"),
        "run_status": state.get("run_status"),
        "run_inputs": state.get("run_inputs", {}),
        "step_count": int(state.get("step_count", 0) or 0),
        "started_at": state.get("started_at"),
        "feedback_counts": state.get("feedback_counts", {}),
        "human_gates": state.get("human_gates", {}),
        "blocked_reason": state.get("blocked_reason"),
    }


def compact_state(state: Mapping[str, Any]) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for node_id, node in (state.get("nodes") or {}).items():
        if node.get("status") == "pending" and not node.get("attempts"):
            continue
        compact = {
            key: value for key, value in dict(node).items() if key not in _STATE_REF_KEYS
        }
        if node.get("output") is not None:
            compact["has_output"] = True
        last = node.get("last_succeeded")
        if isinstance(last, Mapping):
            compact["last_succeeded"] = {
                "input_digest": last.get("input_digest"),
                "output_digest": last.get("output_digest"),
                "output_refs": list(last.get("output_refs") or []),
            }
        nodes[str(node_id)] = compact
    compacted = dict(state)
    compacted["nodes"] = nodes
    return compacted


class StorageV2Error(ValueError):
    """G2 storage 稳定错误：版本漂移、完整性破坏或 writer 已冻结。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_row_version(row: sqlite3.Row) -> None:
    if row["schema_version"] is None or str(row["schema_version"]) != STORAGE_SCHEMA:
        raise StorageV2Error("unsupported_schema")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def object_digest(*, schema_version: str, media_type: str, body: str) -> str:
    return _sha256("\x1f".join(("content-object-v1", schema_version, media_type, body)))


def put_content_object(
    connection: sqlite3.Connection, *, body: str, media_type: str = OBJECT_MEDIA_TYPE
) -> str:
    """单一 content-addressed object 写入点（gate_93c955bbba75）。"""
    digest = object_digest(
        schema_version=STORAGE_SCHEMA, media_type=media_type, body=body
    )
    connection.execute(
        "INSERT OR IGNORE INTO content_objects_v1"
        " (digest, schema_version, media_type, byte_size, body, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (digest, STORAGE_SCHEMA, media_type, len(body.encode("utf-8")), body, _utc_now()),
    )
    return digest


class StorageV2:
    """一个 SQLite 文件上的 G2 v2 读写面（writer + dual-reader + freeze）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript(V2_DDL)
            # sidecar 幂等升级：补版本列（无 DEFAULT，旧行 NULL 稳定拒绝）。
            for table in ("checkpoint_events_v2", "checkpoint_snapshots_v2", "run_heads_v2"):
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if "schema_version" not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN schema_version TEXT"
                    )
            node_state_v2.ensure_schema(connection)
            result_unit.ensure_schema(connection)
            connection.execute(
                "INSERT OR IGNORE INTO storage_v2_manifest(key, value)"
                " VALUES ('schema', ?), ('writer_frozen', '0')",
                (STORAGE_SCHEMA,),
            )

    def _manifest(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute(
            "SELECT value FROM storage_v2_manifest WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def writer_frozen(self) -> bool:
        with self._session() as connection:
            return self._manifest(connection, "writer_frozen") == "1"

    def freeze_writer(self) -> None:
        self.ensure_schema()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO storage_v2_manifest(key, value) VALUES ('writer_frozen', '1')"
                " ON CONFLICT(key) DO UPDATE SET value = '1'"
            )

    def put_object(
        self, connection: sqlite3.Connection, *, body: str, media_type: str = OBJECT_MEDIA_TYPE
    ) -> str:
        return put_content_object(connection, body=body, media_type=media_type)

    def get_object(self, connection: sqlite3.Connection, digest: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM content_objects_v1 WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise StorageV2Error("object_missing")
        if str(row["schema_version"]) != STORAGE_SCHEMA:
            raise StorageV2Error("unsupported_schema")
        body = str(row["body"])
        recomputed = object_digest(
            schema_version=str(row["schema_version"]), media_type=str(row["media_type"]), body=body
        )
        if recomputed != digest or int(row["byte_size"]) != len(body.encode("utf-8")):
            raise StorageV2Error("object_integrity")
        return dict(row)


    def commit_boundary(
        self,
        *,
        stub: dict[str, Any],
        head: dict[str, Any],
        event: dict[str, Any],
        output_body: str | None = None,
        compacted_state: Mapping[str, Any] | None = None,
        snapshot_forced: bool = False,
        execution_id: str | None = None,
        owner_token: str | None = None,
        result_units: list[dict[str, Any]] | None = None,
    ) -> int:
        """节点边界单事务：CAS stub、object、delta event、N=16 周期/强制
        anchor、head、reservation、ResultUnit 同事务；失败整体回滚。"""
        self.ensure_schema()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._manifest(connection, "writer_frozen") == "1":
                raise StorageV2Error("writer_frozen")
            row = connection.execute(
                "SELECT MAX(sequence) AS sequence FROM checkpoints WHERE run_id = ?",
                (stub["run_id"],),
            ).fetchone()
            current = int(row["sequence"]) if row is not None and row["sequence"] is not None else 0
            if current != int(stub["expected_sequence"]):
                connection.rollback()
                raise StorageV2Error("sequence_conflict")
            object_ref = (
                self.put_object(connection, body=output_body)
                if output_body is not None
                else None
            )
            cursor = connection.execute(
                """
                INSERT INTO checkpoints (
                    schema_version, run_id, loop_id, fragment_id, iteration,
                    current_node, status, event_type, revision_reason,
                    supersedes_sequence, payload_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    STUB_SCHEMA_VERSION,
                    stub["run_id"],
                    stub["loop_id"],
                    stub["fragment_id"],
                    stub["iteration"],
                    stub["current_node"],
                    stub["status"],
                    stub["event_type"],
                    stub.get("revision_reason"),
                    current if current else None,
                    "{}",
                    _utc_now(),
                ),
            )
            # 全局 AUTOINCREMENT 主键才是该边界的事实 sequence：event/
            # head/snapshot 一律关联实际 lastrowid（多 run 交错安全）。
            rowid = cursor.lastrowid
            if rowid is None:  # 防御：sqlite INSERT 后恒有 rowid
                raise StorageV2Error("sequence_conflict")
            sequence = int(rowid)
            # 版本化 edge delta 并入 immutable detail_json（审计权威）。
            detail = dict(event.get("detail") or {})
            edges = event.get("edges") or []
            if edges:
                detail["edges"] = edges
            connection.execute(
                """
                INSERT INTO checkpoint_events_v2 (
                    run_id, run_sequence, event_type, node_id, status,
                    output_digest, object_ref, input_digest, attempt,
                    tokens_used, tool_calls_used, detail_json, created_at,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stub["run_id"],
                    sequence,
                    stub["event_type"],
                    event.get("node_id"),
                    event.get("status"),
                    event.get("output_digest"),
                    object_ref,
                    event.get("input_digest"),
                    event.get("attempt"),
                    int(event.get("tokens_used", 0)),
                    int(event.get("tool_calls_used", 0)),
                    _canonical(detail),
                    _utc_now(),
                    STORAGE_SCHEMA,
                ),
            )
            if event.get("node_id") is not None and event.get("status") is not None:
                node_state_v2.upsert_node(
                    connection,
                    run_id=stub["run_id"],
                    node_id=str(event["node_id"]),
                    status=str(event["status"]),
                    attempts=int(event.get("attempt") or 0),
                    output_digest=event.get("output_digest"),
                    object_ref=object_ref,
                    input_digest=event.get("input_digest"),
                    last_succeeded=event.get("last_succeeded"),
                    error=event.get("error"),
                    updated_sequence=sequence,
                )
            marks: dict[str, str] = {
                str(k): str(v)
                for k, v in (event.get("detail") or {}).get("status_resets", {}).items()
            }
            for key, mark in (("ready_nodes", "ready"), ("skipped_nodes", "skipped")):
                for derived_id in (event.get("detail") or {}).get(key, []):
                    marks.setdefault(str(derived_id), mark)
            marks.pop(str(event.get("node_id")), None)
            for marked_id, mark_status in marks.items():
                node_state_v2.mark_node(
                    connection,
                    run_id=stub["run_id"],
                    node_id=marked_id,
                    status=mark_status,
                    updated_sequence=sequence,
                )
            for delta in edges:
                # current 边投影：剥版本键存条目，重复 traversal 折叠计数。
                node_state_v2.upsert_edge(
                    connection,
                    run_id=stub["run_id"],
                    edge={k: v for k, v in dict(delta).items() if k != "schema"},
                    run_sequence=sequence,
                )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) AS c FROM checkpoint_events_v2 WHERE run_id = ?",
                    (stub["run_id"],),
                ).fetchone()["c"]
            )
            if snapshot_forced or count % SNAPSHOT_INTERVAL == 0:
                # 自足 anchor 快照：latest 只读一个基线 anchor + ≤N 事件。
                connection.execute(
                    "INSERT INTO checkpoint_snapshots_v2"
                    " (run_id, run_sequence, state_json, created_at,"
                    " schema_version) VALUES (?, ?, ?, ?, ?)",
                    (
                        stub["run_id"],
                        sequence,
                        _canonical(anchor_state(compacted_state or {})),
                        _utc_now(),
                        STORAGE_SCHEMA,
                    ),
                )
            connection.execute(
                """
                INSERT INTO run_heads_v2 (
                    run_id, runtime_kind, loop_id, fragment_id, run_family_id,
                    episode, latest_sequence, status, current_node,
                    pending_human, updated_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    latest_sequence = excluded.latest_sequence,
                    status = excluded.status,
                    current_node = excluded.current_node,
                    pending_human = excluded.pending_human,
                    updated_at = excluded.updated_at,
                    schema_version = excluded.schema_version
                """,
                (
                    stub["run_id"],
                    head["runtime_kind"],
                    head["loop_id"],
                    head["fragment_id"],
                    head["run_family_id"],
                    int(head.get("episode", 0)),
                    sequence,
                    stub["status"],
                    stub["current_node"],
                    int(head.get("pending_human", 0)),
                    _utc_now(),
                    STORAGE_SCHEMA,
                ),
            )
            if execution_id is not None:
                cursor = connection.execute(
                    "UPDATE execution_reservations_v1"
                    " SET status = 'committed', updated_at = ?"
                    " WHERE execution_id = ? AND owner_token = ?"
                    " AND status IN ('reserved', 'running')",
                    (
                        stub["updated_at"] if "updated_at" in stub else _utc_now(),
                        execution_id,
                        owner_token,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise StorageV2Error("reservation_commit_failed")
            if result_units:
                # G3 ResultUnit 同事务写入（gate_41b46efbf4c2）。
                result_unit.commit_declared_units(
                    connection,
                    prepared_units=result_units,
                    boundary_object_ref=object_ref,
                )
            return sequence

    def head_of(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM run_heads_v2 WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        _require_row_version(row)
        return dict(row)

    def _require_schema(self, connection: sqlite3.Connection) -> None:
        schema = self._manifest(connection, "schema")
        if schema != STORAGE_SCHEMA:
            raise StorageV2Error("unsupported_schema")

    def latest(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
        """latest = 一个基线 anchor + 物化投影点查 + ≤N 事件 replay，行版本闭集。"""
        self._require_schema(connection)
        head = self.head_of(connection, run_id)
        if head is None:
            return None
        anchor = connection.execute(
            "SELECT * FROM checkpoint_snapshots_v2 WHERE run_id = ?"
            " ORDER BY run_sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        state: dict[str, Any] = {"nodes": {}}
        base_sequence = 0
        if anchor is not None:
            _require_row_version(anchor)
            state = json.loads(str(anchor["state_json"]))
            if state.get("schema") != ANCHOR_SCHEMA:
                raise StorageV2Error("unsupported_schema")
            base_sequence = int(anchor["run_sequence"])
        # 物化投影点查：O(节点数)，与历史长度无关。
        projected = node_state_v2.load_nodes(connection, run_id)
        for node_id, node in projected.items():
            last = node.get("last_succeeded")
            if isinstance(last, dict) and last.get("output") is not None:
                node.setdefault("output", last["output"])
        state["nodes"] = projected
        events = connection.execute(
            "SELECT * FROM checkpoint_events_v2 WHERE run_id = ?"
            " AND run_sequence > ? ORDER BY run_sequence",
            (run_id, base_sequence),
        ).fetchall()
        for event in events:
            self._apply_event(state, event, connection)
        state["edges_taken"] = node_state_v2.load_edges(connection, run_id)
        state["run_status"] = head["status"]
        state["current_node"] = head["current_node"]
        state["head_sequence"] = int(head["latest_sequence"])
        return state

    def _apply_event(
        self, state: dict[str, Any], event: sqlite3.Row, connection: sqlite3.Connection
    ) -> None:
        _require_row_version(event)
        node_id = event["node_id"]
        if node_id is None:
            return
        node = state.setdefault("nodes", {}).setdefault(str(node_id), {})
        if event["status"] is not None:
            node["status"] = event["status"]
        if event["output_digest"] is not None:
            node["output_digest"] = event["output_digest"]
        if event["object_ref"] is not None:
            node["output_ref"] = event["object_ref"]
            node["output"] = json.loads(
                self.get_object(connection, str(event["object_ref"]))["body"]
            )
            node["last_succeeded"] = {
                "input_digest": event["input_digest"],
                "output": node["output"],
                "output_digest": event["output_digest"],
                "output_ref": event["object_ref"],  # 与 output 显式同 ref
                "output_refs": [],
            }
        if event["attempt"] is not None:
            node["attempts"] = int(event["attempt"])
        detail = json.loads(str(event["detail_json"]))
        for ready_id in detail.get("ready_nodes", []):
            state.setdefault("nodes", {}).setdefault(str(ready_id), {})["status"] = "ready"
        replay_marks = {str(s): "skipped" for s in detail.get("skipped_nodes", [])}
        replay_marks.update(
            {str(k): str(v) for k, v in (detail.get("status_resets") or {}).items()}
        )
        for marked_id, mark_status in replay_marks.items():
            state.setdefault("nodes", {}).setdefault(marked_id, {})["status"] = mark_status
        if detail.get("error") is not None:
            node["error"] = detail["error"]
        if detail.get("feedback_edge") is not None:
            counts = state.setdefault("feedback_counts", {})
            edge_id = str(detail["feedback_edge"])
            counts[edge_id] = int(detail.get("traversal", counts.get(edge_id, 0) + 1))
        if detail.get("run_status") is not None:
            state["run_status"] = detail["run_status"]

    def history_summary(self, connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT run_sequence, event_type, node_id, status, output_digest,"
            " object_ref, input_digest, attempt, tokens_used, tool_calls_used,"
            " detail_json, schema_version FROM checkpoint_events_v2"
            " WHERE run_id = ? ORDER BY run_sequence",
            (run_id,),
        ).fetchall()
        for row in rows:
            _require_row_version(row)
        return [dict(row) for row in rows]

    def detail_object(self, digest: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._session() as connection:
            return self.get_object(connection, digest)

    def family_heads(
        self, connection: sqlite3.Connection, *, loop_id: str, fragment_id: str
    ) -> list[dict[str, Any]]:
        """run family 索引（确定排序）；版本闭集：任一 NULL/漂移稳定拒绝。"""
        rows = connection.execute(
            "SELECT * FROM run_heads_v2 WHERE loop_id = ? AND fragment_id = ?"
            " ORDER BY episode DESC, updated_at DESC, run_id ASC",
            (loop_id, fragment_id),
        ).fetchall()
        for row in rows:
            _require_row_version(row)
        return [dict(row) for row in rows]

    def explain_plan(self, sql: str, params: tuple[Any, ...] = ()) -> list[str]:
        self.ensure_schema()
        with self._session() as connection:
            rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        return [str(row[3]) for row in rows]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
