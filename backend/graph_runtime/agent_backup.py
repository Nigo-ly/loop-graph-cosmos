"""Graph Phase 2 Pilot 数据库的显式 backup/verify/restore 库（冻结切片）。

冻结路径语义（仅为常量声明，本库默认绝不创建它们；所有函数都接受显式
路径参数，测试全部使用 ``tmp_path``）：

- 真实库：``/Users/example/loop-engineering/data/graph_phase2_pilot.sqlite3``
- 备份目录：``/Users/example/loop-engineering/data/graph_phase2_backups/``

规则（GRAPH-PHASE2-DESIGN 第 6 节）：

- 备份使用 SQLite backup API，文件名含 UTC、阶段与源库 SHA-256；同目录写
  规范化 JSON manifest（源/备份摘要、大小、时间、schema 与原因）；
- SQLite backup API 的副本不保证与源字节一致（WAL/change counter），所以
  摘要语义固定为：manifest 记录备份文件自身的 SHA-256，复核即重算备份
  文件摘要并与 manifest 比对，另加只读 ``quick_check`` 与必备表集合校验；
  任何一步失败即整体失败（调用方据此禁止发送）；
- 已有同名备份绝不覆盖（同一时间戳重跑必须失败而不是替换证据）；
- 恢复只允许到一个不存在的新文件：先完整 verify，再经临时文件原子落位，
  复核摘要与 ``quick_check``；是否切换由人工决定，禁止原地覆盖；
- 本库不提供任何删除、重置或覆盖接口。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

PILOT_DB_PATH = (Path(__file__).resolve().parents[1] / "data" / "graph_phase2_pilot.sqlite3")
PILOT_BACKUP_DIR = (Path(__file__).resolve().parents[1] / "data" / "graph_phase2_backups")

MANIFEST_SCHEMA = "graph-phase2-backup-manifest-v1"
STAGES = frozenset(("pre_call", "terminal"))
TABLES_REQUIRED = frozenset(("checkpoints", "graph_agent_calls_v1"))
MAX_REASON_LENGTH = 256

# 备份/恢复产物完整性问题（调用方必须据此禁止发送）；其余为信任边界错误。
VERIFICATION_CODES = frozenset(
    (
        "quick_check_failed",
        "tables_missing",
        "backup_digest_mismatch",
        "restore_digest_mismatch",
        "manifest_digest_mismatch",
    )
)


class AgentBackupError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise AgentBackupError(code)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_regular_existing(path: Path, *, what: str) -> None:
    """lstat 信任检查：存在、普通文件、绝非符号链接。"""
    if path.is_symlink():
        _fail(f"{what}_symlink_rejected")
    if not path.exists():
        _fail(f"{what}_missing")
    if not stat.S_ISREG(path.stat().st_mode):
        _fail(f"{what}_not_regular_file")


def _readonly_quick_check(path: Path) -> str:
    """只读完整性检查：URI mode=ro，不创建 journal/WAL 副作用文件。

    损坏到无法打开/读取的镜像同样 fail-closed：返回非 "ok" 文本，
    由调用方映射为 ``quick_check_failed``。
    """
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute("PRAGMA quick_check").fetchone()
            return "ok" if row is not None and str(row[0]) == "ok" else str(row[0])
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        return f"error:{type(error).__name__}"


def _table_names(path: Path) -> list[str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name ASC"
        ).fetchall()
        return sorted(str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_"))
    finally:
        connection.close()


def _validate_reason(reason: str) -> None:
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > MAX_REASON_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in reason)
    ):
        _fail("invalid_reason")


def create_backup(
    db_path: str | Path, backup_dir: str | Path, *, stage: str, reason: str
) -> dict[str, Any]:
    """生成一次冷备份与同目录规范化 JSON manifest；返回 manifest 字典。

    备份后立刻对备份文件做只读 ``quick_check``、复核其 SHA-256 并校验必备
    表集合；任何一步失败都抛出 ``AgentBackupError``（调用方必须据此禁止
    发送），且半成品备份与 manifest 一并清除。
    """
    if stage not in STAGES:
        _fail("invalid_stage")
    _validate_reason(reason)
    source = Path(db_path).expanduser()
    _require_regular_existing(source, what="db")
    try:
        source_tables = _table_names(source)
    except sqlite3.DatabaseError:
        _fail("db_unreadable")
    if not TABLES_REQUIRED <= set(source_tables):
        # 缺必备表的库不能成为 Pilot 证据；在任何目录或产物创建之前失败。
        _fail("tables_missing")
    target_dir = Path(backup_dir).expanduser()
    if target_dir.exists():
        if target_dir.is_symlink() or not target_dir.is_dir():
            _fail("backup_dir_unsafe")
    else:
        target_dir.mkdir(parents=True)

    source_digest = sha256_file(source)
    source_size = os.lstat(source).st_size
    stamp = _utc_stamp()
    backup_name = f"{source.stem}-{stamp}-{stage}-{source_digest[:16]}.sqlite3"
    target = target_dir / backup_name
    manifest_path = target_dir / f"{backup_name}.manifest.json"
    if os.path.lexists(target) or os.path.lexists(manifest_path):
        # 绝不覆盖既有备份：同刻重跑必须失败，而不是替换证据。
        _fail("backup_exists")

    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            target_connection = sqlite3.connect(target)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
        finally:
            source_connection.close()

        check = _readonly_quick_check(target)
        if check != "ok":
            _fail("quick_check_failed")
        tables = _table_names(target)
        if not TABLES_REQUIRED <= set(tables):
            _fail("tables_missing")
        backup_digest = sha256_file(target)
        backup_size = os.lstat(target).st_size

        manifest: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "created_at_utc": _utc_iso(),
            "stage": stage,
            "reason": reason,
            "source": {
                "file_name": source.name,
                "sha256": source_digest,
                "size_bytes": source_size,
            },
            "backup": {
                "file_name": backup_name,
                "sha256": backup_digest,
                "size_bytes": backup_size,
            },
            "db_tables": tables,
            "quick_check": check,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    except BaseException:
        # 失败即整体失败：不留下无法证明完整性的产物。
        for artifact in (target, manifest_path):
            try:
                if os.path.lexists(artifact):
                    os.unlink(artifact)
            except OSError:
                pass
        raise


def _load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    candidate = Path(manifest_path).expanduser()
    _require_regular_existing(candidate, what="manifest")
    try:
        raw: Any = json.loads(candidate.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("manifest_unreadable")
    if not isinstance(raw, dict) or raw.get("schema") != MANIFEST_SCHEMA:
        _fail("invalid_manifest")
    for key in (
        "created_at_utc",
        "stage",
        "reason",
        "source",
        "backup",
        "db_tables",
        "quick_check",
    ):
        if key not in raw:
            _fail("invalid_manifest")
    if raw["stage"] not in STAGES:
        _fail("invalid_manifest")
    for section in ("source", "backup"):
        entry = raw[section]
        if not isinstance(entry, dict) or set(entry) != {"file_name", "sha256", "size_bytes"}:
            _fail("invalid_manifest")
        if (
            not isinstance(entry["sha256"], str)
            or len(entry["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in entry["sha256"])
        ):
            _fail("invalid_manifest")
        if not isinstance(entry["size_bytes"], int) or entry["size_bytes"] < 0:
            _fail("invalid_manifest")
    manifest: dict[str, Any] = raw
    return manifest


def verify_backup(
    backup_path: str | Path,
    manifest_path: str | Path | None = None,
    *,
    as_backup: bool = True,
) -> dict[str, Any]:
    """只读复核：SHA-256、大小、quick_check；给 manifest 时比对摘要。

    ``as_backup=True`` 比对 manifest 的备份摘要（复核备份文件）；
    ``as_backup=False`` 比对源摘要（复核源库仍与备份时一致）。
    """
    backup = Path(backup_path).expanduser()
    _require_regular_existing(backup, what="backup")
    digest = sha256_file(backup)
    result: dict[str, Any] = {
        "file_name": backup.name,
        "sha256": digest,
        "size_bytes": os.lstat(backup).st_size,
    }
    if manifest_path is not None:
        # 先比对摘要与大小：任何篡改都以其冻结类别失败，而不是被
        # quick_check 的次级症状掩盖。
        manifest = _load_manifest(manifest_path)
        expected = manifest["backup"]["sha256"] if as_backup else manifest["source"]["sha256"]
        result["manifest_sha256"] = expected
        result["manifest_match"] = digest == expected
        if digest != expected:
            _fail("manifest_digest_mismatch")
        if os.lstat(backup).st_size != manifest["backup" if as_backup else "source"]["size_bytes"]:
            _fail("backup_size_mismatch")
    check = _readonly_quick_check(backup)
    result["quick_check"] = check
    if check != "ok":
        _fail("quick_check_failed")
    return result


def restore_backup(
    backup_path: str | Path,
    manifest_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """恢复到一个不存在的新文件：完整 verify → 临时文件 → 复核 → 原子落位。

    是否切换由人工决定；本函数绝不原地覆盖，绝不删除任何既有文件。
    """
    backup = Path(backup_path).expanduser()
    _require_regular_existing(backup, what="backup")
    manifest = _load_manifest(manifest_path)
    if sha256_file(backup) != manifest["backup"]["sha256"]:
        _fail("manifest_digest_mismatch")
    if _readonly_quick_check(backup) != "ok":
        _fail("quick_check_failed")

    target = Path(target_path).expanduser()
    if target.is_symlink():
        _fail("restore_target_symlink_rejected")
    if os.path.lexists(target):
        # 禁止原地覆盖：恢复目标必须是一个全新路径。
        _fail("restore_target_exists")
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        _fail("restore_target_unsafe")

    temporary = target.with_name(f"{target.name}.restoring")
    try:
        shutil.copyfile(backup, temporary)
        if sha256_file(temporary) != manifest["backup"]["sha256"]:
            _fail("restore_digest_mismatch")
        if _readonly_quick_check(temporary) != "ok":
            _fail("quick_check_failed")
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return {
        "restored_to": str(target),
        "restored_sha256": manifest["backup"]["sha256"],
        "stage": manifest["stage"],
        "verified": True,
    }
