"""G3 Artifact transition 授权域（gate_93c955bbba75 自 result_unit.py 拆出）。

规范化 transition authorization envelope（schema + artifact unit_id/
当前 revision/当前 digest/from_status/to_status/run_id）经单一
`storage_v2.put_content_object` 写入 content-addressed object；
Decision.content_ref 永远指向真实 object digest。promote 加载对象、
复算 digest、逐字段比对 envelope——错 object/篡改 body/digest/旧
Decision 重用/并发 stale 均 stable reject；同一已提交 transition 的
精确 replay 幂等返回、不新增 revision；并发输家得稳定
transition_conflict（不泄漏 database is locked/IntegrityError，不
自行 commit/rollback 调用方事务）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from graph_runtime.result_unit import (
    ARTIFACT_TRANSITIONS,
    PROMOTION_STATUSES,
    ResultUnitError,
    _canonical,
    _require,
    create_unit,
    get_unit,
    revise_unit,
    unit_history,
)
from graph_runtime.storage_v2 import put_content_object


def _transition_envelope(
    *,
    artifact_unit_id: str,
    revision: int,
    artifact_digest: str,
    from_status: str,
    to_status: str,
    run_id: str,
) -> dict[str, Any]:
    """规范化 transition authorization 内容（写 content object 的载体）。"""
    return {
        "schema": "result-unit-transition-v1",
        "artifact_unit_id": artifact_unit_id,
        "revision": revision,
        "artifact_digest": artifact_digest,
        "from_status": from_status,
        "to_status": to_status,
        "run_id": run_id,
    }


TRANSITION_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "artifact_unit_id",
        "revision",
        "artifact_digest",
        "from_status",
        "to_status",
        "run_id",
    }
)


def _load_transition_envelope(
    connection: sqlite3.Connection, content_ref: Any
) -> dict[str, Any]:
    """加载并验证 authorization object：必须存在、digest 复算一致、
    正文是含精确字段闭集的 JSON object——解码/形状/类型错误统一
    unbound_decision，绝不泄漏 JSON/SQLite 原始异常。"""
    from graph_runtime.storage_v2 import object_digest  # 延迟 import 避免环

    _require(isinstance(content_ref, str) and bool(content_ref), "unbound_decision")
    row = connection.execute(
        "SELECT * FROM content_objects_v1 WHERE digest = ?", (content_ref,)
    ).fetchone()
    _require(row is not None, "unbound_decision")
    body = str(row["body"])
    _require(
        object_digest(
            schema_version=str(row["schema_version"]),
            media_type=str(row["media_type"]),
            body=body,
        )
        == content_ref,
        "unbound_decision",
    )
    try:
        envelope = json.loads(body)
    except ValueError:
        raise ResultUnitError("unbound_decision") from None
    _require(isinstance(envelope, dict), "unbound_decision")
    _require(set(envelope) == TRANSITION_ENVELOPE_FIELDS, "unbound_decision")
    _require(envelope["schema"] == "result-unit-transition-v1", "unbound_decision")
    _require(
        isinstance(envelope["revision"], int)
        and not isinstance(envelope["revision"], bool),
        "unbound_decision",
    )
    for field in TRANSITION_ENVELOPE_FIELDS - {"schema", "revision"}:
        _require(
            isinstance(envelope[field], str) and bool(envelope[field]),
            "unbound_decision",
        )
    return dict(envelope)


def grant_transition(
    connection: sqlite3.Connection,
    artifact_unit_id: str,
    *,
    to_status: str,
    creator: Any,
    created_at: Any,
    local_key: str | None = None,
) -> str:
    """唯一公开 grant 路径（gate_93c955bbba75）：规范化 authorization
    envelope 经单一 put_content_object 写入 object，Decision.content_ref
    指向其 digest；grant 是人工授权——creator.type 强制 human，与
    apply_human_decision 的 accept/reject 治理 Decision 语义互斥。"""
    _require(to_status in PROMOTION_STATUSES, "unknown_status")
    _require(
        isinstance(creator, dict) and creator.get("type") == "human",
        "grant_requires_human",
    )
    artifact = get_unit(connection, artifact_unit_id)
    _require(
        artifact is not None and artifact["kind"] == "Artifact", "not_an_artifact"
    )
    assert artifact is not None  # mypy：_require 已 fail closed
    from_status = str(artifact["status"])
    _require(
        to_status in ARTIFACT_TRANSITIONS.get(from_status, frozenset()),
        "illegal_transition",
    )
    envelope = _transition_envelope(
        artifact_unit_id=artifact_unit_id,
        revision=int(artifact["revision"]),
        artifact_digest=str(artifact["digest"]),
        from_status=from_status,
        to_status=to_status,
        run_id=str(artifact["source"]["run_id"]),
    )
    object_ref = put_content_object(connection, body=_canonical(envelope))
    return create_unit(
        connection,
        local_key=local_key
        or f"grant-{to_status}-{artifact['revision']}-{artifact_unit_id[-8:]}",
        kind="Decision",
        status="decided",
        summary=f"grant {from_status}→{to_status}",
        source=dict(artifact["source"]),
        creator=creator,
        created_at=created_at,
        depends_on=[artifact_unit_id],
        content_ref=object_ref,
    )


def _require_transition_authorization(
    connection: sqlite3.Connection,
    artifact: dict[str, Any],
    decision: dict[str, Any],
    *,
    unit_id: str,
    from_status: str,
    to_status: str,
) -> None:
    """Decision 精确绑定本 transition：content_ref 指向的 authorization
    object 与当前 artifact/revision/digest/from/to/run 完全一致。"""
    _require(
        decision["kind"] == "Decision"
        and decision["status"] == "decided"
        and decision["created_by"]["type"] == "human",
        "missing_human_decision",
    )
    run_id = str(artifact["source"]["run_id"])
    _require(
        decision["depends_on"] == [unit_id]
        and decision["source"]["run_id"] == run_id,
        "unbound_decision",
    )
    envelope = _load_transition_envelope(connection, decision["content_ref"])
    expected = _transition_envelope(
        artifact_unit_id=unit_id,
        revision=int(artifact["revision"]),
        artifact_digest=str(artifact["digest"]),
        from_status=from_status,
        to_status=to_status,
        run_id=run_id,
    )
    _require(envelope == expected, "unbound_decision")


def promote_artifact(
    connection: sqlite3.Connection,
    unit_id: str,
    *,
    to_status: str,
    decision_unit_id: str,
    creator: Any,
    created_at: Any,
) -> int:
    """candidate→qualified→reusable→withdrawn 闭集（qualified→withdrawn
    唯一额外）；每次 transition 需一次性人工 Decision（authorization
    object 绑定当前 revision/digest/from/to/run）；同一已提交
    transition 的精确 replay 幂等返回不新增 revision。"""
    _require(to_status in PROMOTION_STATUSES, "unknown_status")
    artifact = get_unit(connection, unit_id)
    _require(artifact is not None and artifact["kind"] == "Artifact", "not_an_artifact")
    assert artifact is not None  # mypy：_require 已 fail closed
    decision = get_unit(connection, decision_unit_id)
    _require(decision is not None, "missing_human_decision")
    assert decision is not None  # mypy 同上
    from_status = str(artifact["status"])
    if from_status == to_status:
        # 同一已提交 transition 的精确 replay：当前 revision 已由本
        # Decision 提交且授权 envelope 与上一 revision 一致——幂等返回。
        _require(artifact["depends_on"] == [decision_unit_id], "unbound_decision")
        history = unit_history(connection, unit_id)
        _require(len(history) >= 2, "illegal_transition")
        previous = history[-2]
        _require(
            to_status in ARTIFACT_TRANSITIONS.get(str(previous["status"]), frozenset()),
            "illegal_transition",
        )
        expected = _transition_envelope(
            artifact_unit_id=unit_id,
            revision=int(previous["revision"]),
            artifact_digest=str(previous["digest"]),
            from_status=str(previous["status"]),
            to_status=to_status,
            run_id=str(artifact["source"]["run_id"]),
        )
        envelope = _load_transition_envelope(connection, decision["content_ref"])
        _require(envelope == expected, "unbound_decision")
        return int(artifact["revision"])
    _require(
        to_status in ARTIFACT_TRANSITIONS.get(from_status, frozenset()),
        "illegal_transition",
    )
    _require_transition_authorization(
        connection,
        artifact,
        decision,
        unit_id=unit_id,
        from_status=from_status,
        to_status=to_status,
    )
    try:
        return revise_unit(
            connection,
            unit_id,
            status=to_status,
            depends_on=[decision_unit_id],
            creator=creator,
            created_at=created_at,
        )
    except sqlite3.IntegrityError:
        # 并发输家：另一连接已提交同 revision transition——稳定错误，
        # 不泄漏 database is locked / PK IntegrityError，不新增 revision；
        # 不自行 commit/rollback 调用方事务。
        raise ResultUnitError("transition_conflict") from None
    except sqlite3.OperationalError as error:
        # snapshot 升级冲突（对方持写锁）：同属并发输家，翻译为稳定错误。
        if "locked" in str(error):
            raise ResultUnitError("transition_conflict") from None
        raise
