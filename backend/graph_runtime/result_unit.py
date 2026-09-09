"""G3 ResultUnit v1（Design §9-§10，additive only）。

六 kind 与状态闭集；stable unit_id = sha256(schema, run_id, macro_node,
local_key)；状态变化不换 ID、每次形成不可变 revision 与新 digest
（canonical digest 排除自身，覆盖 schema_version）。引用完整性
（dangling/self/cross-namespace fail closed；跨 run 只允许显式
reuses edge）；过期投影 stale/expired 且不计证据覆盖；creator 只接受
trusted context 绑定，payload 自报字段一律拒绝。无证据必须 open Gap；
Artifact transition 闭集且每次需一次性人工 Decision（transition
digest 绑定）；published 与 verified 正交；旧 v1 run 绝不反向补造。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "result-unit-v1"
SUMMARY_MAX = 280
NEXT_STEP_MAX = 280
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

KINDS = ("Claim", "Evidence", "Decision", "Action", "Artifact", "Gap")
KIND_STATUS: dict[str, frozenset[str]] = {
    "Claim": frozenset({"provisional", "confirmed", "conflicted", "stale"}),
    "Evidence": frozenset({"available", "omitted", "stale", "invalid"}),
    "Decision": frozenset({"pending", "decided", "rejected", "expired"}),
    "Action": frozenset({"planned", "reserved", "completed", "failed", "unknown"}),
    "Artifact": frozenset({"candidate", "qualified", "reusable", "withdrawn"}),
    "Gap": frozenset({"open", "blocked", "resolved", "wont_fix"}),
}
EDGE_TYPES = frozenset({"supports", "depends_on", "reuses"})
CREATOR_TYPES = frozenset({"human", "rule", "adapter"})
PROMOTION_STATUSES = frozenset({"qualified", "reusable", "withdrawn"})
SOURCE_FIELDS = ("run_id", "node_id", "execution_id", "input_digest", "source_refs")
CREATOR_FIELDS = ("type", "id", "version")
PAYLOAD_FORBIDDEN = frozenset(
    {"unit_id", "digest", "created_by", "source", "schema_version", "revision"}
)
# revision 只允许这些可变字段；unit_id/kind/source/local_key/created_*/
# digest/schema_version/revision 一律 immutable。
MUTABLE_FIELDS = frozenset(
    {"status", "summary", "evidence_refs", "depends_on",
     "valid_until", "next_step", "content_ref"}
)
# Artifact legal transitions（冻结：qualified→withdrawn 是唯一额外允许）；
# 跳级/倒退/同态重放/withdrawn 后复活均不在映射内 → fail closed。
ARTIFACT_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"qualified"}),
    "qualified": frozenset({"reusable", "withdrawn"}),
    "reusable": frozenset({"withdrawn"}),
    "withdrawn": frozenset(),
}

RESULT_UNIT_DDL = """
CREATE TABLE IF NOT EXISTS result_units_v1 (
    unit_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    valid_until TEXT,
    created_by_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    next_step TEXT,
    content_ref TEXT,
    digest TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    PRIMARY KEY (unit_id, revision)
);
CREATE TABLE IF NOT EXISTS result_unit_edges_v1 (
    from_unit_id TEXT NOT NULL,
    to_unit_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (from_unit_id, to_unit_id, edge_type)
);
"""


class ResultUnitError(ValueError):
    """ResultUnit 稳定错误：契约/闭集/引用/绑定破坏，一律 fail closed。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_id_for(*, run_id: str, producer_macro_node_id: str, local_key: str) -> str:
    """stable occurrence identity：结构化 canonical JSON digest（无歧义
    编码——delimiter/control-character/Unicode 不产生碰撞）；跨 run
    相同内容不共用，复用走 reuses edge。"""
    return _sha256(
        _canonical(["result-unit-v1", run_id, producer_macro_node_id, local_key])
    )


def unit_digest(unit: dict[str, Any]) -> str:
    """canonical digest：覆盖除 digest 自身外全部字段。"""
    return _sha256(_canonical({k: v for k, v in unit.items() if k != "digest"}))


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(RESULT_UNIT_DDL)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ResultUnitError(code)


def _parse_rfc3339(value: str) -> datetime:
    """严格 RFC3339 → timezone-aware 瞬时值（仅限已通过 RFC3339_RE 的串）；
    不可能的日期/缺时区一律 ValueError（fail closed）。"""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def _require_rfc3339(value: Any, code: str) -> str:
    _require(isinstance(value, str) and bool(RFC3339_RE.match(value)), code)
    # 形状合法 ≠ 真实存在：2026-99-99 / 2026-02-30 之类必须同样 fail closed。
    try:
        _parse_rfc3339(str(value))
    except ValueError:
        raise ResultUnitError(code) from None
    return str(value)


def _check_payload(payload: dict[str, Any] | None) -> None:
    if payload is not None:
        _require(isinstance(payload, dict), "invalid_payload")
        _require(not (PAYLOAD_FORBIDDEN & set(payload)), "payload_override")


def _check_creator(creator: Any) -> dict[str, Any]:
    # creator 只由 trusted context 以独立参数传入；形状与闭集在此绑定。
    _require(isinstance(creator, dict), "invalid_creator")
    _require(set(creator) == set(CREATOR_FIELDS), "invalid_creator")
    _require(creator["type"] in CREATOR_TYPES, "invalid_creator")
    for field in ("id", "version"):
        _require(
            isinstance(creator[field], str) and bool(creator[field]),
            "invalid_creator",
        )
    return dict(creator)


def _check_source(source: Any) -> dict[str, Any]:
    _require(isinstance(source, dict), "invalid_source")
    _require(set(source) == set(SOURCE_FIELDS), "invalid_source")
    for field in ("run_id", "node_id", "execution_id", "input_digest"):
        _require(isinstance(source[field], str) and bool(source[field]), "invalid_source")
    _require(isinstance(source["source_refs"], list), "invalid_source")
    _require(all(isinstance(ref, str) and ref for ref in source["source_refs"]), "invalid_source")
    return dict(source)


def _check_refs(
    connection: sqlite3.Connection, refs: Any, *, run_id: str, own_id: str | None
) -> list[str]:
    _require(isinstance(refs, (list, tuple)), "invalid_refs")
    checked: list[str] = []
    for ref in refs:
        _require(isinstance(ref, str) and bool(ref), "invalid_refs")
        _require(ref != own_id, "self_reference")
        row = connection.execute(
            "SELECT source_json FROM result_units_v1 WHERE unit_id = ? LIMIT 1", (ref,)
        ).fetchone()
        _require(row is not None, "dangling_reference")
        source = json.loads(str(row["source_json"]))
        _require(str(source.get("run_id")) == run_id, "cross_namespace_reference")
        checked.append(ref)
    return checked


def _build_unit(
    connection: sqlite3.Connection,
    *,
    unit_id: str,
    revision: int,
    kind: str,
    status: str,
    summary: Any,
    source: Any,
    evidence_refs: Any,
    depends_on: Any,
    valid_until: Any,
    creator: Any,
    created_at: Any,
    next_step: Any,
    content_ref: Any,
) -> dict[str, Any]:
    _require(kind in KIND_STATUS, "unknown_kind")
    _require(status in KIND_STATUS[kind], "unknown_status")
    _require(isinstance(summary, str) and 0 < len(summary) <= SUMMARY_MAX, "invalid_summary")
    checked_source = _check_source(source)
    run_id = str(checked_source["run_id"])
    checked_evidence = _check_refs(connection, evidence_refs, run_id=run_id, own_id=unit_id)
    checked_depends = _check_refs(connection, depends_on, run_id=run_id, own_id=unit_id)
    checked_valid_until = (
        None if valid_until is None else _require_rfc3339(valid_until, "invalid_valid_until")
    )
    checked_creator = _check_creator(creator)
    checked_created_at = _require_rfc3339(created_at, "invalid_created_at")
    _require(
        next_step is None or (isinstance(next_step, str) and len(next_step) <= NEXT_STEP_MAX),
        "invalid_next_step",
    )
    _require(
        content_ref is None or (isinstance(content_ref, str) and bool(content_ref)),
        "invalid_content_ref",
    )
    unit = {
        "unit_id": unit_id,
        "revision": revision,
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "status": status,
        "summary": summary,
        "source": checked_source,
        "evidence_refs": checked_evidence,
        "depends_on": checked_depends,
        "valid_until": checked_valid_until,
        "created_by": checked_creator,
        "created_at": checked_created_at,
        "next_step": next_step,
        "content_ref": content_ref,
    }
    unit["digest"] = unit_digest(unit)
    return unit


def _insert(connection: sqlite3.Connection, unit: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO result_units_v1 (unit_id, revision, kind, status, summary,"
        " source_json, evidence_refs_json, depends_on_json, valid_until,"
        " created_by_json, created_at, next_step, content_ref, digest,"
        " schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            unit["unit_id"], unit["revision"], unit["kind"], unit["status"],
            unit["summary"], _canonical(unit["source"]),
            _canonical(unit["evidence_refs"]), _canonical(unit["depends_on"]),
            unit["valid_until"], _canonical(unit["created_by"]),
            unit["created_at"], unit["next_step"], unit["content_ref"],
            unit["digest"], SCHEMA_VERSION,
        ),
    )


def create_unit(
    connection: sqlite3.Connection,
    *,
    local_key: str,
    kind: str,
    status: str,
    summary: Any,
    source: Any,
    creator: Any,
    created_at: Any,
    evidence_refs: Any = (),
    depends_on: Any = (),
    valid_until: Any = None,
    next_step: Any = None,
    content_ref: Any = None,
    payload: dict[str, Any] | None = None,
) -> str:
    _check_payload(payload)
    _require(isinstance(local_key, str) and bool(local_key), "invalid_local_key")
    checked_source = _check_source(source)
    unit_id = unit_id_for(
        run_id=str(checked_source["run_id"]),
        producer_macro_node_id=str(checked_source["node_id"]),
        local_key=local_key,
    )
    unit = _build_unit(
        connection,
        unit_id=unit_id,
        revision=1,
        kind=kind,
        status=status,
        summary=summary,
        source=checked_source,
        evidence_refs=evidence_refs,
        depends_on=depends_on,
        valid_until=valid_until,
        creator=creator,
        created_at=created_at,
        next_step=next_step,
        content_ref=content_ref,
    )
    _insert(connection, unit)
    return unit_id


def get_unit(connection: sqlite3.Connection, unit_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM result_units_v1 WHERE unit_id = ? ORDER BY revision DESC LIMIT 1",
        (unit_id,),
    ).fetchone()
    return None if row is None else _row_to_unit(row)


def unit_history(connection: sqlite3.Connection, unit_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM result_units_v1 WHERE unit_id = ? ORDER BY revision", (unit_id,)
    ).fetchall()
    return [_row_to_unit(row) for row in rows]


def _row_to_unit(row: sqlite3.Row) -> dict[str, Any]:
    _require(str(row["schema_version"]) == SCHEMA_VERSION, "unsupported_schema")
    unit = {
        "unit_id": str(row["unit_id"]),
        "revision": int(row["revision"]),
        "schema_version": str(row["schema_version"]),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "summary": str(row["summary"]),
        "source": json.loads(str(row["source_json"])),
        "evidence_refs": json.loads(str(row["evidence_refs_json"])),
        "depends_on": json.loads(str(row["depends_on_json"])),
        "valid_until": row["valid_until"],
        "created_by": json.loads(str(row["created_by_json"])),
        "created_at": str(row["created_at"]),
        "next_step": row["next_step"],
        "content_ref": row["content_ref"],
        "digest": str(row["digest"]),
    }
    _require(unit_digest(unit) == unit["digest"], "digest_mismatch")
    return unit


def revise_unit(
    connection: sqlite3.Connection,
    unit_id: str,
    *,
    creator: Any,
    created_at: Any,
    payload: dict[str, Any] | None = None,
    **changes: Any,
) -> int:
    """状态变化不换 ID：形成不可变 revision+1 与新 digest。identity
    不可修订——只允许 MUTABLE_FIELDS，非法变化 stable reject。"""
    _check_payload(payload)
    _require(set(changes) <= MUTABLE_FIELDS, "immutable_field_change")
    current = get_unit(connection, unit_id)
    _require(current is not None, "unknown_unit")
    assert current is not None  # mypy：_require 已 fail closed
    merged = {**current, **changes}
    unit = _build_unit(
        connection,
        unit_id=unit_id,
        revision=int(current["revision"]) + 1,
        kind=merged["kind"],
        status=merged["status"],
        summary=merged["summary"],
        source=merged["source"],
        evidence_refs=merged["evidence_refs"],
        depends_on=merged["depends_on"],
        valid_until=merged["valid_until"],
        creator=creator,
        created_at=created_at,
        next_step=merged["next_step"],
        content_ref=merged["content_ref"],
    )
    _insert(connection, unit)
    return int(unit["revision"])


def add_edge(
    connection: sqlite3.Connection,
    *,
    from_unit_id: str,
    to_unit_id: str,
    edge_type: str,
    created_at: Any,
) -> None:
    """显式关系边；跨 run 只允许 reuses（且目标必须是可用 Artifact）。"""
    _require(edge_type in EDGE_TYPES, "unknown_edge_type")
    _require(from_unit_id != to_unit_id, "self_reference")
    source_unit = get_unit(connection, from_unit_id)
    target_unit = get_unit(connection, to_unit_id)
    _require(source_unit is not None and target_unit is not None, "dangling_reference")
    assert source_unit is not None and target_unit is not None  # mypy 同上
    same_run = source_unit["source"]["run_id"] == target_unit["source"]["run_id"]
    if not same_run:
        _require(edge_type == "reuses", "cross_namespace_reference")
    if edge_type == "reuses":
        _require(target_unit["kind"] == "Artifact", "reuse_target_not_artifact")
        _require(
            target_unit["status"] in ("qualified", "reusable"),
            "reuse_target_not_reusable",
        )
    _require_rfc3339(created_at, "invalid_created_at")
    connection.execute(
        "INSERT INTO result_unit_edges_v1 (from_unit_id, to_unit_id, edge_type,"
        " created_at) VALUES (?, ?, ?, ?)",
        (from_unit_id, to_unit_id, edge_type, created_at),
    )



def commit_declared_units(
    connection: sqlite3.Connection,
    *,
    prepared_units: list[dict[str, Any]],
    boundary_object_ref: str | None,
) -> list[str]:
    """G3 边界同事务写入（仅 storage_v2.commit_boundary 调用）：
    content_ref="boundary" 绑定本 boundary object digest；显式 ref 校验
    存在；重放与 revision 1 完整 envelope 比较（created_at 豁免）。"""
    created: list[str] = []
    for prepared in prepared_units:
        content_ref = prepared.get("content_ref")
        if content_ref == "boundary":
            _require(boundary_object_ref is not None, "content_ref_missing")
            content_ref = boundary_object_ref
        elif content_ref is not None:
            row = connection.execute(
                "SELECT digest FROM content_objects_v1 WHERE digest = ?",
                (content_ref,),
            ).fetchone()
            _require(row is not None, "content_ref_missing")
        unit_id = unit_id_for(
            run_id=str(prepared["source"]["run_id"]),
            producer_macro_node_id=str(prepared["source"]["node_id"]),
            local_key=str(prepared["local_key"]),
        )
        history = unit_history(connection, unit_id)
        if history:
            # 幂等重放（CAS 重试同一边界）：与 revision 1 的规范化完整
            # envelope 比较——除 created_at 重放口径（digest 随之豁免）
            # 外任何业务字段漂移均 result_unit_conflict。
            first = history[0]
            candidate = _build_unit(
                connection,
                unit_id=unit_id,
                revision=1,
                kind=prepared["kind"],
                status=prepared["status"],
                summary=prepared["summary"],
                source=prepared["source"],
                evidence_refs=prepared.get("evidence_refs", []),
                depends_on=prepared.get("depends_on", []),
                valid_until=prepared.get("valid_until"),
                creator=prepared["creator"],
                created_at=first["created_at"],
                next_step=prepared.get("next_step"),
                content_ref=content_ref,
            )
            drift = {
                field
                for field in candidate
                if field not in ("created_at", "digest")
                and candidate[field] != first.get(field)
            }
            _require(not drift, "result_unit_conflict")
            continue
        payload = {k: v for k, v in prepared.items() if k != "content_ref"}
        created.append(create_unit(connection, content_ref=content_ref, **payload))
    return created


def project_status(unit: dict[str, Any], *, now: str) -> str:
    """validity 投影（只读不回写）：过期 Claim/Evidence→stale、Decision→expired。

    比较按 timezone-aware 瞬时值（跨 offset 等价语义），绝不按字符串字典序；
    now 无条件先过冻结 RFC3339 形状门禁再解析——即使记录无有效期（valid_until
    为 None）也拒绝非法 now；valid_until 存在时同样先形状后解析。任一失败
    fail closed（ResultUnitError）。"""
    checked_now = _require_rfc3339(now, "invalid_now")
    valid_until = unit.get("valid_until")
    if valid_until is not None:
        checked_valid_until = _require_rfc3339(valid_until, "invalid_valid_until")
        if _parse_rfc3339(checked_now) > _parse_rfc3339(checked_valid_until):
            if unit["kind"] in ("Claim", "Evidence") and unit["status"] != "stale":
                return "stale"
            if unit["kind"] == "Decision" and unit["status"] not in ("expired",):
                return "expired"
    return str(unit["status"])


def units_for_run(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM result_units_v1 a"
        " WHERE a.revision = (SELECT MAX(revision) FROM result_units_v1 b"
        " WHERE b.unit_id = a.unit_id)"
        " AND json_extract(a.source_json, '$.run_id') = ? ORDER BY a.unit_id",
        (run_id,),
    ).fetchall()
    return [_row_to_unit(row) for row in rows]
