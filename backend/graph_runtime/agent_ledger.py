"""持久 Agent 调用预留账本（Graph Phase 2A，冻结切片）。

账本与 Graph Checkpoint 共用一个 SQLite 文件，但使用独立
``graph_agent_calls_v1`` 表：Graph Checkpoint 仍是工作流状态权威，本表只是
不可重复发送与预算证据。唯一 session identity 先占位（PRIMARY KEY +
BEGIN IMMEDIATE，并发竞争只有一个预留成功），随后才允许调用 Transport。

状态机只有三个状态，且只前进：

- ``reserved``：已占位。进程中断后再次见到它即视为 ``unknown_send``，
  禁止重发，并原子改写为 ``failed``（额度不回收）；
- ``completed``：保存经 schema 校验的结果、模型声明与实际 token，
  崩溃恢复时确定性重放，绝不重复发送；
- ``failed``：保存固定错误类别与 ``request_sent=true|false|unknown``。

本模块不提供删除、重置、改写或「标记为未发送后重试」接口。账本路径只接受
已存在的普通文件或将要新建的常规路径；符号链接与非普通文件一律拒绝。
数据库达到 64 MiB 时由适配器停止新的发送（备份与只读诊断仍允许）。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

from common.execution import Reservation
from graph_runtime.agent_policy import utc_now_iso
from graph_runtime.spec import canonical_json

LEDGER_TABLE = "graph_agent_calls_v1"
LEDGER_SCHEMA = "graph-agent-calls-v1"

# 数据库达到 64 MiB 时停止新的真实发送（冻结口径，GRAPH-PHASE2-DESIGN 第 6 节）。
MAX_DB_BYTES = 64 * 1024 * 1024

STATUS_RESERVED = "reserved"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

RequestSent = Literal["true", "false", "unknown"]

# 固定错误类别闭集；账本只接受这些值，拒绝自由文本。
ERROR_CATEGORIES = frozenset(
    (
        "unknown_send",
        "transport_error",
        "transport_exception",
        "timeout",
        "provider_mismatch",
        "model_mismatch",
        "budget_exceeded",
        "invalid_response",
        "response_too_large",
    )
)


class AgentLedgerError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise AgentLedgerError(code)


def _reject_unsafe_path(path: Path) -> None:
    """符号链接、目录与其他非普通文件一律拒绝（fail-closed）。"""
    if path.is_symlink():
        _fail("symlink_rejected")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        _fail("not_regular_file")


@dataclass(frozen=True)
class AgentCallRecord:
    """一次预留的完整身份与预算绑定。"""

    session_id: str
    run_id: str
    node_id: str
    spec_digest: str
    input_digest: str
    adapter: str
    provider: str
    model: str
    authorization_digest: str
    max_input_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class AgentCallRow:
    """节点维度的脱敏调用行；Prompt 正文、响应正文与授权短语永不进入。

    ``error_code`` 是固定错误类别（账本列 ``error_category`` 的投影名）；
    行存在即代表一次预留已发生，所以 ``reserved_calls`` 恒为 1。
    """

    node_id: str
    session_id: str
    status: str
    provider: str
    model: str
    max_calls: int
    reserved_calls: int
    max_input_tokens: int
    max_output_tokens: int
    actual_input_tokens: int | None
    actual_output_tokens: int | None
    error_code: str | None
    request_sent: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentCallRow:
        return cls(
            node_id=str(row["node_id"]),
            session_id=str(row["session_id"]),
            status=str(row["status"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            max_calls=1,
            reserved_calls=1,
            max_input_tokens=int(row["max_input_tokens"]),
            max_output_tokens=int(row["max_output_tokens"]),
            actual_input_tokens=(
                None if row["actual_input_tokens"] is None else int(row["actual_input_tokens"])
            ),
            actual_output_tokens=(
                None
                if row["actual_output_tokens"] is None
                else int(row["actual_output_tokens"])
            ),
            error_code=(
                None if row["error_category"] is None else str(row["error_category"])
            ),
            request_sent=(
                None if row["request_sent"] is None else str(row["request_sent"])
            ),
        )


class AgentLedgerStore:
    """同一 Graph SQLite 文件中的独立 Agent 调用表。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        _reject_unsafe_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Commit/rollback exactly like ``with conn:`` and always close.

        ``with sqlite3.connect()`` commits on clean exit but never closes;
        the leaked handle survived until the cyclic GC collected it, and
        that late close could checkpoint the WAL into the main file at an
        arbitrary later moment (flaky backup digest assertions). Closing
        here makes connection lifetime deterministic without changing any
        ledger or transactional semantics.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                    session_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    spec_digest TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    authorization_digest TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('reserved', 'completed', 'failed')),
                    reserved_at TEXT NOT NULL,
                    completed_at TEXT,
                    request_sent TEXT
                        CHECK(request_sent IN ('true', 'false', 'unknown')),
                    error_category TEXT,
                    max_input_tokens INTEGER NOT NULL,
                    max_output_tokens INTEGER NOT NULL,
                    actual_input_tokens INTEGER,
                    actual_output_tokens INTEGER,
                    declared_provider TEXT,
                    declared_model TEXT,
                    result_json TEXT,
                    result_digest TEXT
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS graph_agent_calls_v1_authorization
                    ON {LEDGER_TABLE}(authorization_digest)
                """
            )

    # -- 路径与容量 ------------------------------------------------------

    def db_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    # -- 预留（唯一线性化点） --------------------------------------------

    def reserve(self, record: AgentCallRecord) -> str:
        """原子预留一个 session identity。

        返回 ``reserved`` / ``authorization_already_used`` / ``reservation_conflict``。
        全部检查与插入在同一个 BEGIN IMMEDIATE 事务内完成，并发竞争只有一个
        预留成功；失败方零写入、零发送。同一 session identity 的重复预留
        （并发输家或重复执行）是 ``reservation_conflict``；同一授权摘要被
        另一个 session 复用才是 ``authorization_already_used``。
        """
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            same_session = connection.execute(
                f"SELECT COUNT(*) AS count FROM {LEDGER_TABLE} WHERE session_id = ?",
                (record.session_id,),
            ).fetchone()
            if same_session is not None and int(same_session["count"]) >= 1:
                connection.rollback()
                return "reservation_conflict"
            used = connection.execute(
                f"SELECT COUNT(*) AS count FROM {LEDGER_TABLE} WHERE authorization_digest = ?",
                (record.authorization_digest,),
            ).fetchone()
            if used is not None and int(used["count"]) >= 1:
                connection.rollback()
                return "authorization_already_used"
            try:
                connection.execute(
                    f"""
                    INSERT INTO {LEDGER_TABLE} (
                        session_id, run_id, node_id, spec_digest, input_digest,
                        adapter, provider, model, authorization_digest,
                        status, reserved_at, max_input_tokens, max_output_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        record.session_id,
                        record.run_id,
                        record.node_id,
                        record.spec_digest,
                        record.input_digest,
                        record.adapter,
                        record.provider,
                        record.model,
                        record.authorization_digest,
                        utc_now_iso(),
                        record.max_input_tokens,
                        record.max_output_tokens,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return "reservation_conflict"
        return "reserved"

    # -- 状态前进（只进不退） --------------------------------------------

    def complete(
        self,
        session_id: str,
        *,
        declared_provider: str,
        declared_model: str,
        actual_input_tokens: int,
        actual_output_tokens: int,
        result: dict[str, Any],
        result_digest: str,
    ) -> bool:
        """reserved → completed，原子写入经校验的结果。返回是否完成转换。"""
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE {LEDGER_TABLE}
                SET status = 'completed', completed_at = ?, request_sent = 'true',
                    error_category = NULL, declared_provider = ?, declared_model = ?,
                    actual_input_tokens = ?, actual_output_tokens = ?,
                    result_json = ?, result_digest = ?
                WHERE session_id = ? AND status = 'reserved'
                """,
                (
                    utc_now_iso(),
                    declared_provider,
                    declared_model,
                    actual_input_tokens,
                    actual_output_tokens,
                    canonical_json(result),
                    result_digest,
                    session_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
        return True

    def fail(
        self,
        session_id: str,
        *,
        error_category: str,
        request_sent: RequestSent,
    ) -> bool:
        """reserved → failed；固定错误类别，额度不回收。返回是否完成转换。"""
        if error_category not in ERROR_CATEGORIES:
            _fail("invalid_error_category")
        if request_sent not in ("true", "false", "unknown"):
            _fail("invalid_request_sent")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE {LEDGER_TABLE}
                SET status = 'failed', completed_at = ?,
                    error_category = ?, request_sent = ?
                WHERE session_id = ? AND status = 'reserved'
                """,
                (utc_now_iso(), error_category, request_sent, session_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
        return True

    def resume_presend_failure(self, session_id: str) -> bool:
        """failed(request_sent=false) → reserved：发送前失败的安全重试原语。

        只有确实从未发送过的会话允许恢复；request_sent=true/unknown 的
        failed 行永远拒绝（返回 False），身份不变、不产生新会话。
        """
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE {LEDGER_TABLE}
                SET status = 'reserved', completed_at = NULL,
                    error_category = NULL, request_sent = NULL
                WHERE session_id = ? AND status = 'failed'
                  AND request_sent = 'false'
                """,
                (session_id,),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
        return True

    # -- 只读访问 --------------------------------------------------------

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                f"SELECT * FROM {LEDGER_TABLE} WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return None if row is None else dict(row)

    # -- 只读观察投影 ----------------------------------------------------

    @staticmethod
    def _actions_for(row: dict[str, Any]) -> list[str]:
        """可采取动作是状态的纯函数；unknown_send 只有停止并人工检查。"""
        status = str(row["status"])
        if status == STATUS_COMPLETED:
            return ["none"]
        if status == STATUS_RESERVED:
            return ["stop_and_inspect"]
        if str(row.get("error_category")) == "unknown_send":
            return ["stop_and_inspect"]
        if str(row.get("request_sent")) == "unknown":
            return ["stop_and_inspect"]
        return ["human_review"]

    @classmethod
    def _observe_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        """只暴露调用状态、provider/model、预留/实际用量、session 摘要、
        错误类别与可采取动作；Prompt 正文、响应正文、授权短语与凭据永不
        进入投影。"""
        return {
            "session_id": row["session_id"],
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "spec_digest": row["spec_digest"],
            "input_digest": row["input_digest"],
            "adapter": row["adapter"],
            "provider": row["provider"],
            "model": row["model"],
            "status": row["status"],
            "reserved_at": row["reserved_at"],
            "completed_at": row["completed_at"],
            "request_sent": row["request_sent"],
            "error_category": row["error_category"],
            "reserved_budget": {
                "max_calls": 1,
                "max_input_tokens": row["max_input_tokens"],
                "max_output_tokens": row["max_output_tokens"],
            },
            "actual_usage": {
                "input_tokens": row["actual_input_tokens"],
                "output_tokens": row["actual_output_tokens"],
            },
            "declared": {
                "provider": row["declared_provider"],
                "model": row["declared_model"],
            },
            "result_digest": row["result_digest"],
            "available_actions": cls._actions_for(row),
        }

    def observe(self, session_id: str) -> dict[str, Any] | None:
        row = self.get(session_id)
        return None if row is None else self._observe_row(row)

    def list_for_run(self, run_id: str) -> list[AgentCallRow]:
        """一个 run 的全部脱敏调用行（UI 观察投影的唯一读取路径）。"""
        with self._session() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM {LEDGER_TABLE} WHERE run_id = ?
                ORDER BY reserved_at ASC, session_id ASC
                """,
                (run_id,),
            ).fetchall()
        return [AgentCallRow.from_row(dict(row)) for row in rows]

    def list_observations(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._session() as connection:
            if run_id is None:
                rows = connection.execute(
                    f"SELECT * FROM {LEDGER_TABLE} ORDER BY reserved_at ASC, session_id ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT * FROM {LEDGER_TABLE} WHERE run_id = ?
                    ORDER BY reserved_at ASC, session_id ASC
                    """,
                    (run_id,),
                ).fetchall()
        return [self._observe_row(dict(row)) for row in rows]


# 观察投影/服务层使用的别名：同一实现，两个语义入口（诊断视图与 UI 行视图）。
AgentCallLedger = AgentLedgerStore


# Gate 1（§3.5）：reservation ↔ Agent session 唯一旁路关联
# 关联只写 ``execution_reservations_v1`` 新行的 nullable
# ``agent_session_id`` / ``agent_link_digest`` 两列；``graph_agent_calls_v1``
# 保持零 ALTER/UPDATE/DELETE/backfill。attach 只允许 NULL → exact session
# 或 exact replay；四类错误在发送前 fail closed。
# ---------------------------------------------------------------------------

ATTACH_OK = ("attached", "replayed")

# 版本化闭集 resume intent（R8）：Pilot retry 以已验证 latest ledger
# session 与授权生成；Runtime 以当前 checkpoint/spec/input/inventory 补全
# 其余期望字段。
RESUME_INTENT_SCHEMA = "agent-resume-intent-v1"
RESUME_INTENT_KEYS = frozenset(
    {"schema", "execution_id", "session_id", "authorization_digest"}
)


def validate_resume_intent(intent: Any) -> dict[str, str] | None:
    """闭集 resume intent 校验；非法即 None（resume_intent_invalid）。"""
    if not isinstance(intent, dict) or set(intent) != RESUME_INTENT_KEYS:
        return None
    if intent.get("schema") != RESUME_INTENT_SCHEMA:
        return None
    for key in ("execution_id", "session_id", "authorization_digest"):
        value = intent.get(key)
        if not isinstance(value, str) or not value:
            return None
    return {
        "execution_id": str(intent["execution_id"]),
        "session_id": str(intent["session_id"]),
        "authorization_digest": str(intent["authorization_digest"]),
    }


def agent_link_digest(
    *,
    execution_id: str,
    session_id: str,
    run_id: str,
    node_id: str,
    spec_digest: str,
    input_digest: str,
    adapter_metadata_digest: str,
    authorization_digest: str,
) -> str:
    """绑定字段全量 digest：任何字段漂移都得到不同 digest，关联不靠可变
    payload 或 checkpoint 推测。"""
    canonical = "\x1f".join(
        (
            "agent-execution-link-v1",
            execution_id,
            session_id,
            run_id,
            node_id,
            spec_digest,
            input_digest,
            adapter_metadata_digest,
            authorization_digest,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AgentExecutionLink:
    """同一 SQLite 文件上 reservation 行的 Agent session 附着（CAS）；
    只写旁路 ``execution_reservations_v1`` 关联列，绝不触碰
    ``graph_agent_calls_v1`` 历史行。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        _reject_unsafe_path(self.path)

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

    def attach(
        self,
        *,
        execution_id: str,
        session_id: str,
        adapter_metadata_digest: str,
        authorization_digest: str,
    ) -> str:
        """把 ``session_id`` 以 CAS 附着到 ``execution_id``：link digest 由
        reservation 行权威 run/node/spec/input 字段计算；返回
        attached/replayed 或四类稳定错误（missing/mismatch/
        already_linked/digest_mismatch），BEGIN IMMEDIATE 串行，部分唯一
        索引兜底。"""
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run_id, node_id, spec_digest, input_digest,
                       agent_session_id, agent_link_digest
                FROM execution_reservations_v1 WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                return "execution_reservation_missing"
            link_digest = agent_link_digest(
                execution_id=execution_id,
                session_id=session_id,
                run_id=str(row["run_id"]),
                node_id=str(row["node_id"]),
                spec_digest=str(row["spec_digest"]),
                input_digest=str(row["input_digest"]),
                adapter_metadata_digest=adapter_metadata_digest,
                authorization_digest=authorization_digest,
            )
            bound_session = row["agent_session_id"]
            if bound_session is None:
                holder = connection.execute(
                    """
                    SELECT execution_id FROM execution_reservations_v1
                    WHERE agent_session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if holder is not None:
                    # 冻结身份（§3.5）：同 session 绑定新 execution 一律
                    # already_linked——旧 sidecar 绑定是稳定审计身份，不得
                    # 清空或转移；request_sent='false' 只证明可沿**同一**
                    # execution 恢复，不授权重写 execution identity。
                    return "agent_session_already_linked"
                try:
                    cursor = connection.execute(
                        """
                        UPDATE execution_reservations_v1
                        SET agent_session_id = ?, agent_link_digest = ?,
                            updated_at = ?
                        WHERE execution_id = ? AND agent_session_id IS NULL
                        """,
                        (session_id, link_digest, time.time(), execution_id),
                    )
                except sqlite3.IntegrityError:
                    # 部分唯一索引兜底：同 session 已被并发 execution 绑定
                    return "agent_session_already_linked"
                if cursor.rowcount != 1:  # 防御：BEGIN IMMEDIATE 下不应发生
                    return "agent_session_mismatch"
                return "attached"
            if str(bound_session) != session_id:
                return "agent_session_mismatch"
            if str(row["agent_link_digest"] or "") != link_digest:
                return "agent_link_digest_mismatch"
            return "replayed"

    def latest_execution_for(
        self,
        *,
        runtime_kind: str,
        run_id: str,
        node_id: str,
        spec_digest: str,
        input_digest: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """精确 binding/session 查询最新 attempt 行；spec/input 漂移或
        session 不符即 None（不得只按 run/node 猜）。"""
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT execution_id, logical_attempt, status, agent_session_id
                FROM execution_reservations_v1
                WHERE runtime_kind = ? AND run_id = ? AND node_id = ?
                  AND spec_digest = ? AND input_digest = ?
                  AND agent_session_id = ?
                ORDER BY logical_attempt DESC LIMIT 1
                """,
                (
                    runtime_kind,
                    run_id,
                    node_id,
                    spec_digest,
                    input_digest,
                    session_id,
                ),
            ).fetchone()
        return None if row is None else dict(row)

    def resume_presend(
        self,
        *,
        execution_id: str,
        owner_token: str,
        runtime_kind: str,
        run_id: str,
        node_id: str,
        spec_digest: str,
        input_digest: str,
        execution_class: str | None,
        classification_digest: str | None,
        session_id: str,
        adapter_metadata_digest: str,
        authorization_digest: str,
        lease_seconds: float = 30.0,
    ) -> Reservation:
        """Verified pre-send resume（§3.5，同 execution 全绑定）：failed
        首次 resume；reserved live lease in_progress；reserved 过期且
        ledger 仍 failed/false 可 takeover（计数再 +1）；running/
        committed/invalid 安全停止。逐字段核对并以 adapter metadata 与
        authorization 重算 link digest；漂移零计数零发送。"""
        now = time.time()
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM execution_reservations_v1 WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                return Reservation(False, "execution_reservation_missing")
            status = str(row["status"])
            if status in ("committed", "failed_invalid_usage"):
                return Reservation(False, "resume_not_failed")
            if status == "running":
                if float(row["lease_expires_at"]) > now:
                    return Reservation(False, "in_progress")
                # expired running：交 external_hold 恢复链（verified result
                # 只提交；绝不 acquire/增计数/进 handler）
                return Reservation(
                    False,
                    "external_hold",
                    str(row["execution_id"]),
                    int(row["logical_attempt"]),
                )
            if status == "reserved":
                if float(row["lease_expires_at"]) > now:
                    return Reservation(False, "in_progress")
                takeover = True
            elif status == "failed":
                takeover = False
            else:
                return Reservation(False, "resume_not_failed")
            if row["agent_session_id"] is None or row["agent_link_digest"] is None:
                # 未 attach 的行没有可验证的关联证据
                return Reservation(False, "resume_evidence_missing")
            for field, expected in (
                ("runtime_kind", runtime_kind),
                ("run_id", run_id),
                ("node_id", node_id),
                ("spec_digest", spec_digest),
                ("input_digest", input_digest),
                ("execution_class", execution_class),
                ("classification_digest", classification_digest),
            ):
                actual = row[field]
                if (actual is None and expected is not None) or (
                    actual is not None and str(actual) != str(expected)
                ):
                    return Reservation(False, f"resume_drift:{field}")
            if str(row["agent_session_id"] or "") != session_id:
                return Reservation(False, "agent_session_mismatch")
            expected_link = agent_link_digest(
                execution_id=execution_id,
                session_id=session_id,
                run_id=run_id,
                node_id=node_id,
                spec_digest=spec_digest,
                input_digest=input_digest,
                adapter_metadata_digest=adapter_metadata_digest,
                authorization_digest=authorization_digest,
            )
            if str(row["agent_link_digest"] or "") != expected_link:
                return Reservation(False, "agent_link_digest_mismatch")
            ledger_row = None
            ledger_table = connection.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name = ?",
                (LEDGER_TABLE,),
            ).fetchone()
            if ledger_table is not None:
                ledger_row = connection.execute(
                    f"""
                    SELECT status, request_sent FROM {LEDGER_TABLE}
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
            if not (
                ledger_row is not None
                and str(ledger_row["status"]) == "failed"
                and str(ledger_row["request_sent"] or "") == "false"
            ):
                return Reservation(False, "resume_evidence_missing")
            cursor = connection.execute(
                """
                UPDATE execution_reservations_v1
                SET status = 'reserved', owner_token = ?,
                    handler_entry_count = handler_entry_count + 1,
                    lease_expires_at = ?, updated_at = ?
                WHERE execution_id = ? AND status = ?
                """,
                (
                    owner_token,
                    now + lease_seconds,
                    now,
                    execution_id,
                    "reserved" if takeover else "failed",
                ),
            )
            if cursor.rowcount != 1:  # 并发状态已变：发送前停止
                return Reservation(False, "resume_not_failed")
            return Reservation(
                True,
                "taken_over" if takeover else "resumed",
                str(row["execution_id"]),
                int(row["logical_attempt"]),
                int(row["handler_entry_count"]) + 1,
            )
