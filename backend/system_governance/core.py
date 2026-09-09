"""Isolated P4A Gate 1 governance contracts.

The module is deliberately self-contained and deterministic. It does not
perform model inference, external network calls, production writes, or real
Keychain access. Persistent state is limited to caller-supplied temporary
SQLite files; secret values stay in process memory only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import hashlib
import ipaddress
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class GovernanceError(ValueError):
    """Stable, stack-free error used by tests and HTTP envelopes."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = message


@contextmanager
def _exclusive_flock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class _NoCorsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: SystemControlHTTPServer  # pyright: ignore[reportIncompatibleVariableOverride]

    def do_OPTIONS(self) -> None:
        self.server.respond_contract_error(self, "method_not_allowed", status=405)

    def do_GET(self) -> None:
        if self.path != "/system/v1/health":
            self.server.respond_contract_error(self, "not_found", status=404)
            return
        host = self.headers.get("Host", "")
        if not self.server.contract.validate_host(host):
            self.server.respond_contract_error(self, "invalid_host")
            return
        self.server.respond_ok(self, {"status": "ok", "contract_version": "p4a-system-control-v1"})

    def do_POST(self) -> None:
        if self.path != "/system/v1/intents":
            self.server.respond_contract_error(self, "not_found", status=404)
            return
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.close_connection = True
            self.server.respond_contract_error(self, "chunked_not_allowed", status=400)
            return
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self.server.respond_contract_error(self, "content_length_required", status=411)
            return
        try:
            length = int(length_header)
        except ValueError:
            self.server.respond_contract_error(self, "invalid_content_length")
            return
        if length < 0:
            self.close_connection = True
            self.server.respond_contract_error(self, "invalid_content_length")
            return
        if length > self.server.contract.body_limit:
            self.close_connection = True
            self.server.respond_contract_error(self, "body_too_large", status=413)
            return
        self.connection.settimeout(self.server.contract.body_timeout_seconds)
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            self.close_connection = True
            self.server.respond_contract_error(self, "body_read_timeout", status=408)
            return
        if len(body) != length:
            self.close_connection = True
            self.server.respond_contract_error(self, "body_truncated", status=400)
            return
        result = self.server.contract.validate(
            host=self.headers.get("Host", ""),
            origin=self.headers.get("Origin"),
            method="POST",
            content_type=self.headers.get("Content-Type", ""),
            intent_header=self.headers.get("X-Loop-Control-Intent"),
            body=body,
        )
        if not result["ok"]:
            self.server.respond_contract_error(self, result["body"]["error"]["code"])
            return
        if self.server.contract.ledger is None:
            self.server.respond_contract_error(self, "ledger_not_configured", status=500)
            return
        try:
            intent_type = IntentType(result["body"]["intent_type"])
        except Exception:
            self.server.respond_contract_error(self, "invalid_intent_type")
            return
        receipt = self.server.contract.ledger.reserve(
            intent_type,
            requester=result["body"]["requester"],
            payload_summary=result["body"]["payload_summary"],
            expected_version=result["body"].get("expected_version"),
            expected_resource_sequence=result["body"].get("expected_resource_sequence"),
            replaces_intent_id=result["body"].get("replaces_intent_id"),
        )
        self.server.respond_ok(self, {"accepted": True, "receipt": receipt})

    def log_message(self, format: str, *args: object) -> None:
        del format, args
        return


class PrivacyLevel(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class IntentType(StrEnum):
    ADD_PROVIDER = "add_provider"
    MODIFY_PROVIDER = "modify_provider"
    TEST_PROVIDER = "test_provider"
    ENABLE_PROVIDER = "enable_provider"
    DISABLE_PROVIDER = "disable_provider"
    IMMEDIATE_SCAN = "immediate_scan"
    RESOURCE_RESUME = "resource_resume"
    ASSIGN_ROLE = "assign_role"


SECRET_FIELD_NAMES = frozenset({"api_key", "secret", "token", "password", "key_value"})
PRIVACY_ORDER = {
    PrivacyLevel.PUBLIC: 0,
    PrivacyLevel.PRIVATE: 1,
    PrivacyLevel.SENSITIVE: 2,
    PrivacyLevel.SECRET: 3,
}


@dataclass(frozen=True)
class ModelConfig:
    provider_id: str
    endpoint: str
    adapter: str
    model_id: str
    model_family: str
    context_limit: int
    output_limit: int
    capabilities: tuple[str, ...]
    privacy: PrivacyLevel
    cost_unit: str
    quota_pool: str
    concurrency_limit: int
    enabled: bool
    config_version: int
    identity_status: str = "identity_unverified"
    role: str = "unassigned"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ModelConfig:
        forbidden = SECRET_FIELD_NAMES.intersection(value)
        if forbidden:
            raise GovernanceError("secret_in_model_config", ",".join(sorted(forbidden)))
        return cls(
            provider_id=str(value["provider_id"]),
            endpoint=str(value["endpoint"]),
            adapter=str(value["adapter"]),
            model_id=str(value["model_id"]),
            model_family=str(value["model_family"]),
            context_limit=int(value["context_limit"]),
            output_limit=int(value["output_limit"]),
            capabilities=tuple(str(item) for item in value["capabilities"]),
            privacy=PrivacyLevel(str(value["privacy"])),
            cost_unit=str(value["cost_unit"]),
            quota_pool=str(value["quota_pool"]),
            concurrency_limit=int(value["concurrency_limit"]),
            enabled=bool(value["enabled"]),
            config_version=int(value["config_version"]),
            identity_status=str(value.get("identity_status", "identity_unverified")),
            role=str(value.get("role", "unassigned")),
        )

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["privacy"] = self.privacy.value
        return payload

    def content_digest(self) -> str:
        return _digest(self.public_dict())


def enforce_privacy_boundary(
    current: PrivacyLevel, requested: PrivacyLevel, actor: str
) -> PrivacyLevel:
    if actor == "model" and PRIVACY_ORDER[requested] < PRIVACY_ORDER[current]:
        raise GovernanceError("model_cannot_downgrade_privacy")
    return requested


class RuntimeModelRegistry:
    """Non-secret runtime model registry with atomic version updates."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_configs(
                    provider_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    config_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS update_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generations(
                    generation TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    published_version INTEGER,
                    content_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            if self._meta_get(connection, "registry_version") is None:
                self._meta_set(connection, "registry_version", "0")
                self._meta_set(connection, "content_digest", _digest([]))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _meta_get(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _meta_set(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, value),
        )

    def version(self) -> int:
        with self._connect() as connection:
            return int(self._meta_get(connection, "registry_version") or "0")

    def content_digest(self) -> str:
        with self._connect() as connection:
            return str(self._meta_get(connection, "content_digest"))

    def list_configs(self) -> list[ModelConfig]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM model_configs ORDER BY provider_id"
            ).fetchall()
        return [ModelConfig.from_dict(json.loads(str(row["payload_json"]))) for row in rows]

    def update_atomic(
        self,
        configs: Iterable[ModelConfig],
        *,
        expected_version: int,
        generation: str,
        crash_at: Literal["before_commit", "after_commit_before_receipt"] | None = None,
    ) -> dict[str, Any]:
        payloads = [
            config.public_dict() for config in sorted(configs, key=lambda item: item.provider_id)
        ]
        for payload in payloads:
            if SECRET_FIELD_NAMES.intersection(payload):
                raise GovernanceError("secret_in_model_config")
        digest = _digest(payloads)
        with _exclusive_flock(self.lock_path):
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    duplicate = connection.execute(
                        "SELECT status, published_version, content_digest FROM generations "
                        "WHERE generation = ?",
                        (generation,),
                    ).fetchone()
                    if duplicate is not None:
                        connection.rollback()
                        if (
                            duplicate["status"] == "published"
                            and str(duplicate["content_digest"]) == digest
                        ):
                            return {
                                "status": "already_applied",
                                "version": int(duplicate["published_version"]),
                                "digest": digest,
                            }
                        return {
                            "status": "generation_conflict",
                            "digest": str(duplicate["content_digest"]),
                        }
                    current = int(self._meta_get(connection, "registry_version") or "0")
                    current_digest = str(self._meta_get(connection, "content_digest"))
                    if current != expected_version:
                        if digest == current_digest:
                            connection.rollback()
                            return {
                                "status": "already_applied",
                                "version": current,
                                "digest": digest,
                            }
                        connection.rollback()
                        return {
                            "status": "version_conflict",
                            "version": current,
                            "digest": current_digest,
                        }
                    connection.execute(
                        "INSERT INTO update_events(generation, event_type, detail_json, created_at)"
                        " VALUES (?, 'staged', ?, ?)",
                        (generation, _canonical_json({"digest": digest}), utc_now()),
                    )
                    connection.execute(
                        "INSERT INTO generations(generation, status, expected_version, "
                        "published_version, content_digest, created_at) VALUES "
                        "(?, 'staged', ?, NULL, ?, ?)",
                        (generation, expected_version, digest, utc_now()),
                    )
                    connection.execute("DELETE FROM model_configs")
                    for payload in payloads:
                        config_digest = _digest(payload)
                        connection.execute(
                            "INSERT INTO model_configs(provider_id, payload_json, content_digest,"
                            " config_version) VALUES (?, ?, ?, ?)",
                            (
                                str(payload["provider_id"]),
                                _canonical_json(payload),
                                config_digest,
                                int(payload["config_version"]),
                            ),
                        )
                    if crash_at == "before_commit":
                        raise RuntimeError("synthetic_crash_before_commit")
                    new_version = current + 1
                    self._meta_set(connection, "registry_version", str(new_version))
                    self._meta_set(connection, "content_digest", digest)
                    connection.execute(
                        "INSERT INTO update_events(generation, event_type, detail_json, created_at)"
                        " VALUES (?, 'published', ?, ?)",
                        (
                            generation,
                            _canonical_json({"version": new_version, "digest": digest}),
                            utc_now(),
                        ),
                    )
                    connection.execute(
                        "UPDATE generations SET status='published', published_version=? "
                        "WHERE generation=?",
                        (new_version, generation),
                    )
                    connection.commit()
                    if crash_at == "after_commit_before_receipt":
                        raise RuntimeError("synthetic_crash_after_commit_before_receipt")
                    return {"status": "published", "version": new_version, "digest": digest}
                except Exception:
                    connection.rollback()
                    raise

    def recover(self) -> dict[str, Any]:
        with _exclusive_flock(self.lock_path):
            version = self.version()
            digest = self.content_digest()
        return {
            "status": "recovered",
            "strategy": "single_transaction_rollback",
            "version": version,
            "digest": digest,
            "pending_generations": [],
        }


class TempKeychain:
    """Dedicated temporary keychain substitute.

    Secret values are held only in memory. The SQLite file records only
    non-secret lifecycle events so tests can prove create/read/replace/delete
    and cleanup without placing recoverable key material on disk.
    """

    def __init__(self, path: str | Path, *, namespace: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self._values: dict[str, str] = {}
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS key_meta(
                    name TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    mask TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS key_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def mask(value: str) -> str:
        _ = value
        return "configured"

    def put(self, name: str, value: str) -> dict[str, str]:
        if not value.startswith("test-"):
            raise GovernanceError("only_valueless_test_secret_allowed")
        self._values[name] = value
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO key_meta(name, namespace, mask, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (name, self.namespace, self.mask(value), utc_now()),
            )
            connection.execute(
                "INSERT INTO key_events(event_type, name, created_at) VALUES ('put', ?, ?)",
                (name, utc_now()),
            )
        return {"name": name, "mask": self.mask(value)}

    def get_for_request(self, name: str) -> str:
        if name not in self._values:
            raise GovernanceError("secret_missing")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO key_events(event_type, name, created_at) VALUES ('read', ?, ?)",
                (name, utc_now()),
            )
        return self._values[name]

    def delete(self, name: str) -> None:
        self._values.pop(name, None)
        with self._connect() as connection:
            connection.execute("DELETE FROM key_meta WHERE name = ?", (name,))
            connection.execute(
                "INSERT INTO key_events(event_type, name, created_at) VALUES ('delete', ?, ?)",
                (name, utc_now()),
            )

    def cleanup(self) -> dict[str, Any]:
        self._values.clear()
        with self._connect() as connection:
            names = [str(row["name"]) for row in connection.execute("SELECT name FROM key_meta")]
            connection.execute("DELETE FROM key_meta")
            for name in names:
                connection.execute(
                    "INSERT INTO key_events(event_type, name, created_at) VALUES ('cleanup', ?, ?)",
                    (name, utc_now()),
                )
        return {"status": "cleaned", "remaining_values": 0}

    def recover(self) -> dict[str, Any]:
        self._values.clear()
        with self._connect() as connection:
            names = [str(row["name"]) for row in connection.execute("SELECT name FROM key_meta")]
            connection.execute("DELETE FROM key_meta")
            for name in names:
                connection.execute(
                    "INSERT INTO key_events(event_type, name, created_at) VALUES "
                    "('crash_recovered_cleanup', ?, ?)",
                    (name, utc_now()),
                )
        return {"status": "recovered", "cleaned_names": len(names), "remaining_values": 0}

    def evidence(self) -> dict[str, Any]:
        with self._connect() as connection:
            meta = [dict(row) for row in connection.execute("SELECT * FROM key_meta")]
            events = [dict(row) for row in connection.execute("SELECT * FROM key_events")]
        return {"meta": meta, "events": events, "has_in_memory_values": bool(self._values)}


class MacOSTemporaryKeychain:
    """Dedicated temporary macOS keychain using Security.framework.

    The synthetic secret is passed through in-process memory to the framework;
    it is never written to argv, environment variables, logs, databases, or
    evidence files. This class is intentionally small and is only for Gate 1
    isolated tests with worthless `test-` material.
    """

    def __init__(self, path: str | Path, *, password: bytes, service: str) -> None:
        security_path = ctypes.util.find_library("Security")
        core_foundation_path = ctypes.util.find_library("CoreFoundation")
        if security_path is None or core_foundation_path is None:
            raise GovernanceError("macos_security_framework_unavailable")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._password = password
        self.service = service.encode("utf-8")
        self._security = ctypes.CDLL(security_path)
        self._core_foundation = ctypes.CDLL(core_foundation_path)
        self._keychain = ctypes.c_void_p()
        self.events: list[dict[str, str]] = []
        self._configure_api()

    def _configure_api(self) -> None:
        self._security.SecKeychainCreate.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainCreate.restype = ctypes.c_int32
        self._security.SecKeychainUnlock.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_bool,
        ]
        self._security.SecKeychainUnlock.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._security.SecKeychainDelete.argtypes = [ctypes.c_void_p]
        self._security.SecKeychainDelete.restype = ctypes.c_int32
        self._security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self._security.SecKeychainOpen.restype = ctypes.c_int32
        self._core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self._core_foundation.CFRelease.restype = None

    def _check(self, status: int, code: str) -> None:
        if status != 0:
            raise GovernanceError(code, str(status))

    def create(self) -> None:
        path_bytes = os.fsencode(self.path)
        password_buffer = ctypes.create_string_buffer(self._password)
        status = self._security.SecKeychainCreate(
            path_bytes,
            len(self._password),
            ctypes.cast(password_buffer, ctypes.c_void_p),
            False,
            None,
            ctypes.byref(self._keychain),
        )
        self._check(int(status), "temporary_keychain_create_failed")
        unlock_status = self._security.SecKeychainUnlock(
            self._keychain,
            len(self._password),
            ctypes.cast(password_buffer, ctypes.c_void_p),
            True,
        )
        self._check(int(unlock_status), "temporary_keychain_unlock_failed")
        self.events.append({"event_type": "create", "path": str(self.path)})

    def put(self, account: str, value: str) -> None:
        if not value.startswith("test-"):
            raise GovernanceError("only_valueless_test_secret_allowed")
        account_bytes = account.encode("utf-8")
        value_bytes = value.encode("utf-8")
        item = self._find_item(account_bytes)
        value_buffer = ctypes.create_string_buffer(value_bytes)
        if item is None:
            item_ref = ctypes.c_void_p()
            status = self._security.SecKeychainAddGenericPassword(
                self._keychain,
                len(self.service),
                self.service,
                len(account_bytes),
                account_bytes,
                len(value_bytes),
                ctypes.cast(value_buffer, ctypes.c_void_p),
                ctypes.byref(item_ref),
            )
            self._check(int(status), "temporary_keychain_add_failed")
            if item_ref.value is not None:
                self._core_foundation.CFRelease(item_ref)
            self.events.append({"event_type": "put", "account": account})
            return
        status = self._security.SecKeychainItemModifyAttributesAndData(
            item,
            None,
            len(value_bytes),
            ctypes.cast(value_buffer, ctypes.c_void_p),
        )
        self._core_foundation.CFRelease(item)
        self._check(int(status), "temporary_keychain_replace_failed")
        self.events.append({"event_type": "replace", "account": account})

    def get_for_request(self, account: str) -> str:
        account_bytes = account.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            self._keychain,
            len(self.service),
            self.service,
            len(account_bytes),
            account_bytes,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        self._check(int(status), "temporary_keychain_secret_missing")
        try:
            raw = ctypes.string_at(data, length.value)
            self.events.append({"event_type": "read", "account": account})
            return raw.decode("utf-8")
        finally:
            self._security.SecKeychainItemFreeContent(None, data)
            if item.value is not None:
                self._core_foundation.CFRelease(item)

    def delete(self, account: str) -> None:
        item = self._find_item(account.encode("utf-8"))
        if item is not None:
            status = self._security.SecKeychainItemDelete(item)
            self._core_foundation.CFRelease(item)
            self._check(int(status), "temporary_keychain_delete_item_failed")
        self.events.append({"event_type": "delete", "account": account})

    def cleanup(self) -> dict[str, Any]:
        status = self._security.SecKeychainDelete(self._keychain)
        if self._keychain.value is not None:
            self._core_foundation.CFRelease(self._keychain)
            self._keychain = ctypes.c_void_p()
        if self.path.exists():
            self.path.unlink()
        self._check(int(status), "temporary_keychain_cleanup_failed")
        self.events.append({"event_type": "cleanup", "path_exists": str(self.path.exists())})
        return {"status": "cleaned", "path_exists": self.path.exists()}

    def recover(self) -> dict[str, Any]:
        path_bytes = os.fsencode(self.path)
        if not self.path.exists():
            self.events.append({"event_type": "recover_missing", "path": str(self.path)})
            return {"status": "recovered", "path_exists": False, "orphan_cleaned": False}
        if self._keychain.value is None:
            status = self._security.SecKeychainOpen(path_bytes, ctypes.byref(self._keychain))
            self._check(int(status), "temporary_keychain_open_failed")
            self.events.append({"event_type": "recover_open", "path": str(self.path)})
            unlock_status = self._security.SecKeychainUnlock(
                self._keychain,
                len(self._password),
                ctypes.cast(ctypes.create_string_buffer(self._password), ctypes.c_void_p),
                True,
            )
            self._check(int(unlock_status), "temporary_keychain_unlock_failed")
        delete_status = self._security.SecKeychainDelete(self._keychain)
        self._check(int(delete_status), "temporary_keychain_recover_delete_failed")
        if self._keychain.value is not None:
            self._core_foundation.CFRelease(self._keychain)
            self._keychain = ctypes.c_void_p()
        if self.path.exists():
            self.path.unlink()
        self.events.append({"event_type": "recover_cleanup", "path": str(self.path)})
        return {"status": "recovered", "path_exists": False, "orphan_cleaned": True}

    def _find_item(self, account: bytes) -> ctypes.c_void_p | None:
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            self._keychain,
            len(self.service),
            self.service,
            len(account),
            account,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        if status != 0:
            return None
        self._security.SecKeychainItemFreeContent(None, data)
        return item

    def evidence(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "events": list(self.events),
            "secret_material_recorded": False,
        }


class EndpointPolicy:
    def __init__(
        self,
        *,
        resolver: Callable[[str], list[str]],
        allow_loopback_test: bool = False,
        loopback_allowlist: frozenset[str] = frozenset(),
    ) -> None:
        self.resolver = resolver
        self.allow_loopback_test = allow_loopback_test
        self.loopback_allowlist = loopback_allowlist

    @staticmethod
    def _is_forbidden_ip(value: str) -> bool:
        ip = ipaddress.ip_address(value)
        return (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or value.startswith("169.254.169.254")
        )

    def normalize(self, url: str, *, redirects: tuple[str, ...] = ()) -> str:
        chain = (url, *redirects)
        normalized = ""
        for raw in chain:
            parsed = urlsplit(raw.strip())
            if parsed.scheme != "https":
                if not (
                    self.allow_loopback_test
                    and parsed.scheme == "http"
                    and parsed.hostname in self.loopback_allowlist
                ):
                    raise GovernanceError("endpoint_requires_https")
            if parsed.username or parsed.password:
                raise GovernanceError("endpoint_userinfo_rejected")
            if not parsed.hostname:
                raise GovernanceError("endpoint_missing_host")
            addresses = self.resolver(parsed.hostname)
            if not addresses:
                raise GovernanceError("endpoint_dns_missing")
            for address in addresses:
                if self._is_forbidden_ip(address):
                    allowed = (
                        self.allow_loopback_test and parsed.hostname in self.loopback_allowlist
                    )
                    if not allowed:
                        raise GovernanceError("endpoint_forbidden_address")
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"{parsed.hostname.lower()}{port}"
            normalized = urlunsplit(
                (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
            )
        return normalized


@dataclass(frozen=True)
class HealthSnapshot:
    probe_version: str
    model_config_version: int
    observed_at: str
    connection: str
    identity: str
    context: str
    structured_output: str
    tool_calling: str
    latency_ms: int
    rate_limit: str
    retries: int
    quota: str
    cost: str
    privacy: str
    stability: str
    failure_reason: str | None
    valid_until: str


@dataclass(frozen=True)
class DeterministicAdapterResult:
    ok: bool
    actual_identity: str
    latency_ms: int = 1
    retries: int = 0
    structured: bool = True
    tool_call: bool = True
    rate_limited: bool = False
    quota: str = "available"
    cost: str = "synthetic"
    malformed: bool = False
    failure: str | None = None


@dataclass(frozen=True)
class ResponsesAdapterRequest:
    endpoint: str
    model: str
    input: tuple[dict[str, Any], ...]
    response_format: dict[str, str]
    tools: tuple[dict[str, Any], ...]
    timeout_seconds: int


@dataclass(frozen=True)
class TransportFacts:
    status: str
    content_type: str
    contract_version: str
    actual_model: str
    output: dict[str, Any]
    usage: dict[str, Any]
    redirect_chain: tuple[str, ...] = ()
    final_peer_ip: str = ""
    tls_verified: bool = True
    body_complete: bool = True
    final_endpoint: str = ""


class ResponsesCompatibleAdapterContract:
    """Provider-neutral contract for Responses-compatible transports.

    The transport is injected for Gate 1 so tests stay offline. Runtime callers
    receive only stable structured results; provider-specific wire details stay
    outside Loop Core.
    """

    def __init__(
        self,
        *,
        endpoint_policy: EndpointPolicy,
        transport: Callable[[ResponsesAdapterRequest, str], dict[str, Any]],
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1 or max_attempts > 3:
            raise GovernanceError("retry_budget_out_of_bounds")
        self.endpoint_policy = endpoint_policy
        self.transport = transport
        self.max_attempts = max_attempts

    def build_request(
        self, config: ModelConfig, messages: Iterable[dict[str, Any]]
    ) -> ResponsesAdapterRequest:
        return ResponsesAdapterRequest(
            endpoint=self.endpoint_policy.normalize(config.endpoint),
            model=config.model_id,
            input=tuple(messages),
            response_format={"type": "json_schema"},
            tools=({"type": "function", "name": "synthetic_tool"},)
            if "tool_calling" in config.capabilities
            else (),
            timeout_seconds=10,
        )

    def call(
        self,
        config: ModelConfig,
        *,
        key: str,
        messages: Iterable[dict[str, Any]],
        privacy_permit: bool = True,
    ) -> dict[str, Any]:
        if config.privacy is not PrivacyLevel.PUBLIC and not privacy_permit:
            raise GovernanceError("privacy_permission_required")
        request = self.build_request(config, messages)
        attempts = 0
        failures: list[str] = []
        backoff_ms: list[int] = []
        while attempts < self.max_attempts:
            attempts += 1
            response = self.transport(request, key)
            status = str(response.get("status", "ok"))
            if status == "ok":
                self._validate_transport_facts(request, response)
                actual_model = str(response.get("actual_model", ""))
                if config.identity_status == "verified" and actual_model != config.model_id:
                    raise GovernanceError("adapter_identity_drift")
                parsed = self.parse_response(response)
                parsed["attempts"] = attempts
                return parsed
            if status not in {"rate_limited", "timeout", "disconnected"}:
                raise GovernanceError("adapter_permanent_failure", status)
            failures.append(status)
            if attempts < self.max_attempts:
                backoff_ms.append(100 * (2 ** (attempts - 1)))
        return {
            "status": "failed_safe",
            "attempts": attempts,
            "failures": failures,
            "backoff_ms": backoff_ms,
        }

    def _validate_transport_facts(
        self, request: ResponsesAdapterRequest, response: dict[str, Any]
    ) -> None:
        redirect_chain = tuple(str(item) for item in response.get("redirect_chain", ()))
        if not isinstance(response.get("usage"), dict):
            raise GovernanceError("adapter_missing_usage")
        if not response.get("final_peer_ip"):
            raise GovernanceError("adapter_missing_peer_ip")
        if response.get("tls_verified") is not True:
            raise GovernanceError("adapter_tls_not_verified")
        if response.get("body_complete") is not True:
            raise GovernanceError("adapter_body_truncated")
        normalized = self.endpoint_policy.normalize(request.endpoint, redirects=redirect_chain)
        final_endpoint = str(response.get("final_endpoint") or normalized)
        if normalized != final_endpoint:
            raise GovernanceError("adapter_redirect_mismatch")
        final_host = urlsplit(final_endpoint).hostname or ""
        if not final_host:
            raise GovernanceError("adapter_missing_final_host")
        final_addresses = self.endpoint_policy.resolver(final_host)
        if not final_addresses:
            raise GovernanceError("endpoint_dns_missing")
        for address in final_addresses:
            if self.endpoint_policy._is_forbidden_ip(address):
                allowed = (
                    self.endpoint_policy.allow_loopback_test
                    and final_host in self.endpoint_policy.loopback_allowlist
                )
                if not allowed:
                    raise GovernanceError("endpoint_forbidden_address")
        if str(response["final_peer_ip"]) not in final_addresses:
            raise GovernanceError("adapter_peer_ip_mismatch")
        usage = response["usage"]
        if usage.get("cost") in {None, ""}:
            raise GovernanceError("adapter_cost_missing")
        if usage.get("quota") in {None, ""}:
            raise GovernanceError("adapter_quota_missing")

    @staticmethod
    def parse_response(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("content_type") != "application/json":
            raise GovernanceError("adapter_bad_content_type")
        if response.get("contract_version") != "responses-compatible-v1":
            raise GovernanceError("adapter_version_mismatch")
        allowed = {
            "status",
            "content_type",
            "contract_version",
            "actual_model",
            "output",
            "usage",
            "redirect_chain",
            "final_peer_ip",
            "tls_verified",
            "body_complete",
            "final_endpoint",
        }
        unknown = set(response) - allowed
        if unknown:
            raise GovernanceError("adapter_unknown_fields", ",".join(sorted(unknown)))
        output = response.get("output")
        if not isinstance(output, dict):
            raise GovernanceError("adapter_malformed_output")
        return {
            "status": "ok",
            "actual_model": str(response.get("actual_model", "identity_unverified")),
            "output": output,
            "usage": response.get("usage", {}),
            "redirect_chain": tuple(str(item) for item in response.get("redirect_chain", ())),
            "final_peer_ip": str(response.get("final_peer_ip", "")),
        }


def probe_model_health(
    config: ModelConfig,
    adapter_result: DeterministicAdapterResult,
    *,
    observed_at: str,
    valid_until: str,
) -> HealthSnapshot:
    if adapter_result.malformed:
        return HealthSnapshot(
            "p4a-gate1-probe-v1",
            config.config_version,
            observed_at,
            "failed",
            "identity_unverified",
            "failed",
            "failed",
            "failed",
            adapter_result.latency_ms,
            "unknown",
            0,
            "unknown",
            "unknown",
            config.privacy.value,
            "failed_safe",
            "malformed_response",
            valid_until,
        )
    identity = (
        "verified"
        if adapter_result.actual_identity == config.model_id
        and config.identity_status == "verified"
        else "identity_unverified"
    )
    failure = adapter_result.failure
    connection = "ok" if adapter_result.ok and not adapter_result.rate_limited else "failed"
    if adapter_result.rate_limited:
        failure = "rate_limited"
    return HealthSnapshot(
        "p4a-gate1-probe-v1",
        config.config_version,
        observed_at,
        connection,
        identity,
        "ok" if adapter_result.ok else "failed",
        "ok" if adapter_result.structured else "failed",
        "ok" if adapter_result.tool_call else "failed",
        adapter_result.latency_ms,
        "limited" if adapter_result.rate_limited else "ok",
        adapter_result.retries if hasattr(adapter_result, "retries") else 0,
        adapter_result.quota,
        adapter_result.cost,
        config.privacy.value,
        "stable" if adapter_result.ok and not failure else "failed_safe",
        failure,
        valid_until,
    )


def select_role_pair(
    worker: ModelConfig,
    evaluator: ModelConfig,
    *,
    risk: Literal["low", "high"],
) -> dict[str, str]:
    if (
        worker.model_family != evaluator.model_family
        and worker.provider_id != evaluator.provider_id
    ):
        return {
            "status": "assigned",
            "worker": worker.provider_id,
            "evaluator": evaluator.provider_id,
        }
    if risk == "low":
        return {"status": "pending_independent_eval", "reason": "role_independence_missing"}
    return {"status": "paused", "reason": "role_independence_missing"}


@dataclass(frozen=True)
class ResourceSample:
    used_memory_gb: float
    pressure: str
    p4_process_tree: tuple[int, ...]
    sampled_at: str
    sequence: int
    recovery_check: bool = False


@dataclass
class ProcessController:
    registered: set[int]
    identities: dict[int, str] = field(default_factory=dict)
    identity_probe: Callable[[int], str | None] | None = None
    terminate_probe: Callable[[int], bool] | None = None
    terminated: list[int] = field(default_factory=list)

    def terminate_registered(self, pids: Iterable[int], *, max_attempts: int = 3) -> dict[str, Any]:
        unknown = [pid for pid in pids if pid not in self.registered]
        if unknown:
            return {"status": "failed_safe", "reason": "unknown_process_identity"}
        mismatched = []
        for pid in pids:
            expected = self.identities.get(pid)
            observed = self.identity_probe(pid) if self.identity_probe is not None else expected
            if expected is None or observed is None:
                mismatched.append(pid)
                continue
            if observed != expected:
                mismatched.append(pid)
        if mismatched:
            return {
                "status": "failed_safe",
                "reason": "process_identity_drift",
                "pids": mismatched,
            }
        failed = []
        for pid in pids:
            ok = False
            for _attempt in range(max_attempts):
                ok = self.terminate_probe(pid) if self.terminate_probe is not None else True
                if ok:
                    break
            if ok:
                self.terminated.append(pid)
            else:
                failed.append(pid)
        if failed:
            return {"status": "failed_safe", "reason": "termination_timeout", "pids": failed}
        return {"status": "terminated", "pids": list(pids)}


@dataclass(frozen=True)
class SafeNodePauseRequest:
    checkpoint: LoopCheckpoint
    expected_sequence: int
    stop_reason: str = "resource_pause_20gb"
    resume_condition: str = "manual_resume_below_16gb"


class HarnessBridge:
    """Minimal P4A composition layer over the accepted checkpoint harness.

    It does not introduce a P4 run state machine. Resource pauses are represented
    as ordinary checkpoint CAS writes at the latest safe node boundary.
    """

    def __init__(self, checkpoint_store: SQLiteCheckpointStore) -> None:
        self.checkpoint_store = checkpoint_store

    def pause_at_safe_node(self, request: SafeNodePauseRequest) -> dict[str, Any]:
        paused = LoopCheckpoint(
            **{
                **request.checkpoint.to_dict(),
                "status": "paused",
                "stop_reason": request.stop_reason,
                "resume_condition": request.resume_condition,
            }
        )
        committed = self.checkpoint_store.compare_and_append(
            paused,
            expected_sequence=request.expected_sequence,
            event_type="resource_pause",
            revision_reason=f"p4a:resource_guard:{request.stop_reason}",
        )
        if committed is None:
            return {"status": "failed_safe", "reason": "checkpoint_sequence_drift"}
        sequence = self.checkpoint_store.latest_sequence(committed.run_id)
        return {"status": "paused", "run_id": committed.run_id, "sequence": sequence}

    def resource_pause(self, run_id: str) -> tuple[LoopCheckpoint, int] | None:
        found = self.checkpoint_store.latest_with_sequence(run_id)
        if found is None:
            return None
        checkpoint, sequence = found
        if checkpoint.status != "paused" or checkpoint.stop_reason not in {
            "resource_pause_20gb",
            "resource_hard_stop_22gb",
        }:
            return None
        return checkpoint, sequence

    def resume_resource_pause(
        self,
        run_id: str,
        *,
        expected_sequence: int,
        application_id: str,
    ) -> dict[str, Any]:
        marker = f"p4a:resource_resume:{application_id}"
        existing = self.checkpoint_store.find_revision_marker(run_id, marker)
        if existing is not None:
            return {"status": "already_applied", "run_id": run_id, "sequence": existing}
        found = self.resource_pause(run_id)
        if found is None or found[1] != expected_sequence:
            return {"status": "conflict", "reason": "checkpoint_sequence_drift"}
        checkpoint, _sequence = found
        resumed = LoopCheckpoint(
            **{
                **checkpoint.to_dict(),
                "status": "approved",
                "stop_reason": None,
                "resume_condition": None,
            }
        )
        committed = self.checkpoint_store.compare_and_append(
            resumed,
            expected_sequence=expected_sequence,
            event_type="resource_resume",
            revision_reason=marker,
        )
        if committed is None:
            return {"status": "conflict", "reason": "checkpoint_sequence_drift"}
        sequence = self.checkpoint_store.latest_sequence(run_id)
        return {"status": "applied", "run_id": run_id, "sequence": sequence}


class ResourceGuard:
    def __init__(self, controller: ProcessController, harness: HarnessBridge | None = None) -> None:
        self.controller = controller
        self.harness = harness
        self._last_sequence = -1

    def evaluate(
        self,
        sample: ResourceSample | None,
        *,
        quota_healthy: bool = True,
        tools_healthy: bool = True,
        third_slot_authorized: bool = False,
        pause_request: SafeNodePauseRequest | None = None,
    ) -> dict[str, Any]:
        if sample is None:
            return {"state": "failed_safe", "reason": "sample_missing", "max_concurrency": 0}
        if sample.sequence <= self._last_sequence:
            return {"state": "failed_safe", "reason": "sample_order_drift", "max_concurrency": 0}
        self._last_sequence = sample.sequence
        if sample.pressure not in {"normal", "warning", "critical"}:
            return {"state": "failed_safe", "reason": "pressure_unknown", "max_concurrency": 0}
        persisted_pause = (
            self.harness.resource_pause(pause_request.checkpoint.run_id)
            if self.harness is not None and pause_request is not None
            else None
        )
        if persisted_pause is not None and sample.used_memory_gb < 20:
            return {"state": "manual_resume_only", "max_concurrency": 0, "auto_resume": False}
        if sample.recovery_check and sample.used_memory_gb < 16:
            return {"state": "manual_resume_only", "max_concurrency": 0, "auto_resume": False}
        if sample.used_memory_gb >= 22:
            termination = self.controller.terminate_registered(sample.p4_process_tree)
            if termination["status"] != "terminated":
                return {
                    "state": "failed_safe",
                    "reason": termination.get("reason", "termination_failed"),
                    "max_concurrency": 0,
                    "termination": termination,
                }
            result: dict[str, Any] = {
                "state": "hard_stop_22gb",
                "max_concurrency": 0,
                "termination": termination,
            }
            if self.harness is not None and pause_request is not None:
                result["pause"] = self.harness.pause_at_safe_node(
                    SafeNodePauseRequest(
                        checkpoint=pause_request.checkpoint,
                        expected_sequence=pause_request.expected_sequence,
                        stop_reason="resource_hard_stop_22gb",
                    )
                )
            return result
        if sample.used_memory_gb >= 20:
            if self.harness is not None and pause_request is not None:
                pause = self.harness.pause_at_safe_node(pause_request)
                return {
                    "state": "pause_20gb",
                    "max_concurrency": 0,
                    "dispatch": "stop_new",
                    "pause": pause,
                }
            return {"state": "pause_20gb", "max_concurrency": 0, "dispatch": "stop_new"}
        if sample.used_memory_gb >= 18:
            return {"state": "warning_18gb", "max_concurrency": 2}
        max_concurrency = 3 if (third_slot_authorized and quota_healthy and tools_healthy) else 2
        return {"state": "normal", "max_concurrency": max_concurrency}

    def apply_resume_intent(
        self,
        ledger: IntentLedger,
        intent_id: str,
        *,
        run_id: str,
        sample: ResourceSample | None,
        crash_at: Literal["after_external_effect_before_marker"] | None = None,
    ) -> dict[str, Any]:
        harness = self.harness
        if harness is None:
            raise GovernanceError("checkpoint_harness_required")
        if sample is None or not sample.recovery_check or sample.used_memory_gb >= 16:
            return {"intent_id": intent_id, "status": "rejected", "error_code": "not_resumable"}
        current = ledger.current_receipt(intent_id)
        if current["status"] in {"applied", "rejected"}:
            return current
        found = harness.resource_pause(run_id)
        if found is None:
            if current["status"] != "applying":
                return {"intent_id": intent_id, "status": "rejected", "error_code": "not_paused"}
            sequence = harness.checkpoint_store.latest_sequence(run_id)
            if sequence is None:
                return {"intent_id": intent_id, "status": "rejected", "error_code": "not_paused"}
        else:
            _checkpoint, sequence = found

        def applicator(application_id: str) -> dict[str, Any]:
            return harness.resume_resource_pause(
                run_id,
                expected_sequence=sequence,
                application_id=application_id,
            )

        return ledger.apply(
            intent_id,
            current_resource_sequence=sequence,
            applicator=applicator,
            crash_at=crash_at,
        )


class IntentLedger:
    """Append-only system governance intent ledger."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intent_payloads(
                    intent_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    intent_type TEXT NOT NULL,
                    expected_version INTEGER,
                    expected_resource_sequence INTEGER,
                    requester TEXT NOT NULL,
                    payload_summary TEXT NOT NULL,
                    replaces_intent_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_current(
                    intent_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    current_index INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intent_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def current_receipt(self, intent_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM intent_current WHERE intent_id=?", (intent_id,)
            ).fetchone()
        if row is None:
            raise GovernanceError("intent_missing")
        return cast(dict[str, Any], json.loads(str(row["receipt_json"])))

    def reserve(
        self,
        intent_type: IntentType,
        *,
        requester: str,
        payload_summary: dict[str, Any],
        expected_version: int | None = None,
        expected_resource_sequence: int | None = None,
        replaces_intent_id: str | None = None,
    ) -> dict[str, Any]:
        if intent_type is IntentType.RESOURCE_RESUME and requester != "nigo":
            raise GovernanceError("resource_resume_requester_forbidden")
        if SECRET_FIELD_NAMES.intersection(payload_summary):
            raise GovernanceError("secret_in_intent_summary")
        canonical_key = _digest(
            {
                "type": intent_type.value,
                "requester": requester,
                "summary": payload_summary,
                "expected_version": expected_version,
                "expected_resource_sequence": expected_resource_sequence,
                "replaces_intent_id": replaces_intent_id,
            }
        )
        now = utc_now()
        intent_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"p4a-system-intent:{canonical_key}"))
        receipt = {"intent_id": intent_id, "status": "reserved", "idempotency_key": canonical_key}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT c.receipt_json FROM intent_payloads p "
                "JOIN intent_current c ON c.intent_id = p.intent_id "
                "WHERE p.idempotency_key = ?",
                (canonical_key,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return cast(dict[str, Any], json.loads(str(existing["receipt_json"])))
            connection.execute(
                "INSERT INTO intent_payloads(intent_id, idempotency_key, intent_type,"
                " expected_version, expected_resource_sequence, requester, payload_summary,"
                " replaces_intent_id,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    canonical_key,
                    intent_type.value,
                    expected_version,
                    expected_resource_sequence,
                    requester,
                    _canonical_json(payload_summary),
                    replaces_intent_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO intent_current(intent_id, status, receipt_json, current_index,"
                " updated_at)"
                " VALUES (?, 'reserved', ?, 0, ?)",
                (intent_id, _canonical_json(receipt), now),
            )
            connection.execute(
                "INSERT INTO intent_events(intent_id, event_type, detail_json, created_at)"
                " VALUES (?, 'reserved', ?, ?)",
                (intent_id, _canonical_json({"payload_digest": _digest(payload_summary)}), now),
            )
            connection.execute(
                "INSERT INTO audit_events(intent_id, event_type, detail_json, created_at)"
                " VALUES (?, 'reserved', ?, ?)",
                (intent_id, _canonical_json({"payload_digest": _digest(payload_summary)}), now),
            )
            connection.commit()
        return receipt

    def apply(
        self,
        intent_id: str,
        *,
        current_version: int | None = None,
        current_resource_sequence: int | None = None,
        applicator: Callable[[str], dict[str, Any]] | None = None,
        crash_at: Literal[
            "after_external_effect_before_marker", "after_marker_before_receipt"
        ]
        | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        application_id = _digest({"intent_id": intent_id, "purpose": "system_intent_application"})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT p.*, c.status, c.receipt_json, c.current_index FROM intent_payloads p "
                "JOIN intent_current c ON c.intent_id = p.intent_id WHERE p.intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise GovernanceError("intent_missing")
            if row["status"] in {"applied", "rejected"}:
                connection.rollback()
                return cast(dict[str, Any], json.loads(str(row["receipt_json"])))
            if row["status"] == "reserved" and row["expected_version"] is not None and row[
                "expected_version"
            ] != current_version:
                connection.rollback()
                return {
                    "intent_id": intent_id,
                    "status": "rejected",
                    "error_code": "version_drift",
                }
            if row["status"] == "reserved" and (
                row["expected_resource_sequence"] is not None
                and row["expected_resource_sequence"] != current_resource_sequence
            ):
                connection.rollback()
                return {
                    "intent_id": intent_id,
                    "status": "rejected",
                    "error_code": "resource_sequence_drift",
                }
            if row["status"] == "reserved" and row["replaces_intent_id"] is not None:
                replaced = connection.execute(
                    "SELECT status FROM intent_current WHERE intent_id = ?",
                    (row["replaces_intent_id"],),
                ).fetchone()
                if replaced is not None and str(replaced["status"]) not in {"applied", "replaced"}:
                    connection.rollback()
                    return {
                        "intent_id": intent_id,
                        "status": "rejected",
                        "error_code": "replacement_not_resolved",
                    }
            if row["status"] == "reserved":
                applying = {
                    "intent_id": intent_id,
                    "status": "applying",
                    "application_id": application_id,
                }
                connection.execute(
                    "UPDATE intent_current SET status='applying', receipt_json=?,"
                    " current_index=current_index + 1, updated_at=? WHERE intent_id=?",
                    (_canonical_json(applying), now, intent_id),
                )
                detail = _canonical_json({"application_id": application_id})
                connection.execute(
                    "INSERT INTO intent_events(intent_id, event_type, detail_json, created_at)"
                    " VALUES (?, 'application_started', ?, ?)",
                    (intent_id, detail, now),
                )
                connection.execute(
                    "INSERT INTO audit_events(intent_id, event_type, detail_json, created_at)"
                    " VALUES (?, 'application_started', ?, ?)",
                    (intent_id, detail, now),
                )
                connection.commit()
            else:
                connection.rollback()

        application_result = (
            applicator(application_id) if applicator is not None else {"status": "applied"}
        )
        if application_result.get("status") not in {"applied", "already_applied"}:
            receipt = {
                "intent_id": intent_id,
                "status": "rejected",
                "error_code": str(application_result.get("reason", "application_conflict")),
            }
            rejected_at = utc_now()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE intent_current SET status='rejected', receipt_json=?,"
                    " current_index=current_index + 1, updated_at=? WHERE intent_id=?",
                    (_canonical_json(receipt), rejected_at, intent_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(intent_id, event_type, detail_json, created_at)"
                    " VALUES (?, 'application_rejected', ?, ?)",
                    (intent_id, _canonical_json(receipt), rejected_at),
                )
                connection.commit()
            return receipt
        if crash_at == "after_external_effect_before_marker":
            raise RuntimeError("synthetic_crash_after_external_effect_before_marker")

        marked_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, receipt_json FROM intent_current WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise GovernanceError("intent_missing")
            if row["status"] == "applied":
                connection.rollback()
                return cast(dict[str, Any], json.loads(str(row["receipt_json"])))
            detail = _canonical_json({"application_id": application_id})
            connection.execute(
                "INSERT INTO intent_events(intent_id, event_type, detail_json, created_at)"
                " VALUES (?, 'application_marked', ?, ?)",
                (intent_id, detail, marked_at),
            )
            connection.execute(
                "INSERT INTO audit_events(intent_id, event_type, detail_json, created_at)"
                " VALUES (?, 'application_marked', ?, ?)",
                (intent_id, detail, marked_at),
            )
            if crash_at == "after_marker_before_receipt":
                connection.commit()
                raise RuntimeError("synthetic_crash_after_marker_before_receipt")
            receipt = {
                "intent_id": intent_id,
                "status": "applied",
                "application_id": application_id,
            }
            connection.execute(
                "UPDATE intent_current SET status='applied', receipt_json=?,"
                " current_index=current_index + 1, updated_at=? "
                "WHERE intent_id=?",
                (_canonical_json(receipt), marked_at, intent_id),
            )
            connection.execute(
                "INSERT INTO intent_events(intent_id, event_type, detail_json, created_at)"
                " VALUES (?, 'receipt_written', ?, ?)",
                (intent_id, _canonical_json(receipt), marked_at),
            )
            connection.execute(
                "INSERT INTO audit_events(intent_id, event_type, detail_json, created_at)"
                " VALUES (?, 'receipt_written', ?, ?)",
                (intent_id, _canonical_json(receipt), marked_at),
            )
            connection.commit()
            return receipt

    def recover(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT p.intent_id, p.replaces_intent_id, c.status, c.receipt_json"
                " FROM intent_payloads p "
                "JOIN intent_current c ON c.intent_id = p.intent_id"
            ).fetchall()
            for row in rows:
                event = connection.execute(
                    "SELECT 1 FROM audit_events WHERE intent_id=?"
                    " AND event_type='application_marked'",
                    (row["intent_id"],),
                ).fetchone()
                if row["status"] == "reserved" and event is None:
                    replaced = None
                    if row["replaces_intent_id"] is not None:
                        replaced = connection.execute(
                            "SELECT status FROM intent_current WHERE intent_id = ?",
                            (row["replaces_intent_id"],),
                        ).fetchone()
                    if replaced is not None and str(replaced["status"]) == "applied":
                        recovered.append({"intent_id": str(row["intent_id"]), "state": "expired"})
                        continue
                    recovered.append(
                        {
                            "intent_id": str(row["intent_id"]),
                            "state": "not_applied",
                            "receipt": json.loads(str(row["receipt_json"])),
                        }
                    )
                elif row["status"] == "applying" and event is None:
                    current_receipt = json.loads(str(row["receipt_json"]))
                    recovered.append(
                        {
                            "intent_id": str(row["intent_id"]),
                            "state": "application_unconfirmed",
                            "application_id": current_receipt["application_id"],
                        }
                    )
                elif row["status"] in {"reserved", "applying"} and event is not None:
                    application_id = _digest(
                        {
                            "intent_id": str(row["intent_id"]),
                            "purpose": "system_intent_application",
                        }
                    )
                    receipt = {
                        "intent_id": str(row["intent_id"]),
                        "status": "applied",
                        "application_id": application_id,
                    }
                    connection.execute(
                        "UPDATE intent_current SET status='applied', receipt_json=?,"
                        " current_index=current_index + 1, updated_at=? WHERE intent_id=?",
                        (_canonical_json(receipt), utc_now(), row["intent_id"]),
                    )
                    connection.execute(
                        "INSERT INTO intent_events(intent_id, event_type, detail_json, created_at)"
                        " VALUES (?, 'receipt_recovered', ?, ?)",
                        (row["intent_id"], _canonical_json(receipt), utc_now()),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(intent_id, event_type, detail_json, created_at)"
                        " VALUES (?, 'receipt_recovered', ?, ?)",
                        (row["intent_id"], _canonical_json(receipt), utc_now()),
                    )
                    recovered.append(
                        {
                            "intent_id": str(row["intent_id"]),
                            "state": "recovered_receipt",
                            "receipt": receipt,
                        }
                    )
                elif row["status"] == "applied" and event is not None:
                    continue
                elif row["status"] == "applied":
                    recovered.append(
                        {
                            "intent_id": str(row["intent_id"]),
                            "state": "applied_missing_receipt",
                        }
                    )
                elif row["status"] == "rejected":
                    recovered.append({"intent_id": str(row["intent_id"]), "state": "expired"})
                else:
                    recovered.append(
                        {
                            "intent_id": str(row["intent_id"]),
                            "state": "applied_missing_receipt",
                        }
                    )
        return recovered

    def audit_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM audit_events")]


class SystemControlContract:
    def __init__(
        self,
        *,
        trusted_origin: str,
        body_limit: int = 16 * 1024,
        body_timeout_seconds: float = 1.0,
        ledger: IntentLedger | None = None,
    ) -> None:
        self.trusted_origin = trusted_origin
        self.body_limit = body_limit
        self.body_timeout_seconds = body_timeout_seconds
        self.ledger = ledger

    @staticmethod
    def validate_host(host: str) -> bool:
        return host in {"127.0.0.1", "127.0.0.1:0", "localhost"} or host.startswith("127.0.0.1:")

    def validate(
        self,
        *,
        host: str,
        origin: str | None,
        method: str,
        content_type: str,
        intent_header: str | None,
        body: bytes,
    ) -> dict[str, Any]:
        if not self.validate_host(host):
            return self.error("invalid_host")
        if method != "POST":
            return self.error("method_not_allowed")
        if origin != self.trusted_origin:
            return self.error("invalid_origin")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return self.error("invalid_content_type")
        if intent_header != "1":
            return self.error("invalid_intent_header")
        if len(body) > self.body_limit:
            return self.error("body_too_large")
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.error("invalid_json")
        if not isinstance(payload, dict):
            return self.error("invalid_json_shape")
        allowed = {
            "intent_type",
            "requester",
            "payload_summary",
            "expected_version",
            "expected_resource_sequence",
            "replaces_intent_id",
        }
        unknown = set(payload) - allowed
        if unknown:
            return self.error("unknown_fields")
        required = {"intent_type", "requester", "payload_summary"}
        if not required.issubset(payload):
            return self.error("missing_fields")
        if not isinstance(payload["payload_summary"], dict):
            return self.error("invalid_payload_summary")
        if not isinstance(payload.get("requester"), str) or not payload["requester"]:
            return self.error("invalid_requester")
        if not isinstance(payload.get("intent_type"), str):
            return self.error("invalid_intent_type")
        for range_field in ("expected_version", "expected_resource_sequence"):
            if (
                range_field in payload
                and payload[range_field] is not None
                and (
                    not isinstance(payload[range_field], int)
                    or isinstance(payload[range_field], bool)
                    or payload[range_field] < 0
                )
            ):
                return self.error(f"invalid_{range_field}")
        if (
            "replaces_intent_id" in payload
            and payload["replaces_intent_id"] is not None
            and (
                not isinstance(payload["replaces_intent_id"], str)
                or not payload["replaces_intent_id"]
            )
        ):
            return self.error("invalid_replaces_intent_id")
        return {
            "ok": True,
            "headers": {"Connection": "close"},
            "body": {
                "intent_type": str(payload["intent_type"]),
                "requester": str(payload["requester"]),
                "payload_summary": payload["payload_summary"],
                "expected_version": payload.get("expected_version"),
                "expected_resource_sequence": payload.get("expected_resource_sequence"),
                "replaces_intent_id": payload.get("replaces_intent_id"),
            },
        }

    @staticmethod
    def error(code: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": 400,
            "body": {"error": {"code": code, "message": code}},
            "headers": {"Connection": "close"},
        }


class SystemControlHTTPServer(HTTPServer):
    def __init__(self, address: tuple[str, int], contract: SystemControlContract) -> None:
        if address[0] not in {"127.0.0.1", "localhost"}:
            raise GovernanceError("system_control_must_bind_loopback")
        self.contract = contract
        super().__init__(address, _NoCorsHandler)

    @staticmethod
    def _write_json(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
        payload = _canonical_json(
            {"contract_version": "p4a-system-control-v1", "ok": status < 400, **body}
        ).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)
        handler.close_connection = True

    def respond_ok(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        self._write_json(handler, 200, body)

    def respond_contract_error(
        self,
        handler: BaseHTTPRequestHandler,
        code: str,
        *,
        status: int = 400,
    ) -> None:
        self._write_json(handler, status, {"error": {"code": code, "message": code}})


class ScheduleDedupe:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._completed_slots: set[str] = set()
        self._pending_manual: set[str] = set()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS scan_dedupe(
                        idempotency_key TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        trigger_kind TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        if self.path is None:
            raise GovernanceError("schedule_dedupe_requires_persistent_path")
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def reserve_scan(
        self, slot: str, *, trigger: Literal["scheduled", "wakeup", "manual"]
    ) -> dict[str, str]:
        key = f"scan:{slot}"
        if trigger == "wakeup":
            key = f"scan:wakeup:{slot}"
        if trigger == "manual":
            key = f"scan:manual:{slot}"
        if self.path is None:
            if key in self._completed_slots or key in self._pending_manual:
                return {"status": "duplicate", "idempotency_key": key}
            self._pending_manual.add(key)
            return {"status": "reserved", "idempotency_key": key}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM scan_dedupe WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is not None:
                connection.rollback()
                return {"status": "duplicate", "idempotency_key": key}
            connection.execute(
                "INSERT INTO scan_dedupe(idempotency_key, status, trigger_kind, created_at)"
                " VALUES (?, 'reserved', ?, ?)",
                (key, trigger, utc_now()),
            )
            connection.commit()
            return {"status": "reserved", "idempotency_key": key}

    def complete(self, idempotency_key: str) -> None:
        if self.path is None:
            self._pending_manual.discard(idempotency_key)
            self._completed_slots.add(idempotency_key)
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE scan_dedupe SET status='completed', completed_at=?"
                " WHERE idempotency_key = ?",
                (utc_now(), idempotency_key),
            )
            connection.commit()


def sqlite_readonly_logical_snapshot(path: str | Path) -> dict[str, Any]:
    db_path = Path(path)
    uri = f"file:{db_path}?mode=ro"
    tables: dict[str, Any] = {}
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger') ORDER BY type, name"
        ).fetchall()
        schema_digest = hashlib.sha256(repr(schema_rows).encode("utf-8")).hexdigest()
        table_names = [
            str(row[1])
            for row in schema_rows
            if row[0] == "table" and not str(row[1]).startswith("sqlite_")
        ]
        for table in table_names:
            escaped = table.replace('"', '""')
            count = connection.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
            columns = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
            rows = connection.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid').fetchall()
            tables[table] = {
                "row_count": int(count),
                "column_count": len(columns),
                "schema_only_digest": hashlib.sha256(repr(columns).encode("utf-8")).hexdigest(),
                "rows_digest": hashlib.sha256(
                    repr([tuple(row) for row in rows]).encode("utf-8")
                ).hexdigest(),
            }
    sidecars = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        sidecars[suffix or "main"] = {
            "exists": candidate.exists(),
            "size": candidate.stat().st_size if candidate.exists() else 0,
        }
    return {
        "path": str(db_path),
        "schema_digest": schema_digest,
        "tables": tables,
        "wal_aware": sidecars,
    }


class EvidencePublisher:
    def __init__(
        self, staging_dir: str | Path, final_dir: str | Path, *, evidence_run_id: str
    ) -> None:
        self.staging_dir = Path(staging_dir)
        self.final_dir = Path(final_dir)
        self.evidence_run_id = evidence_run_id
        self.lock_path = self.final_dir.parent / f".{self.final_dir.name}.lock"
        self._lock_fd: int | None = None
        self.recovered_orphans: list[str] = []
        self.final_dir.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise GovernanceError("evidence_publish_locked") from error
        self._lock_fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(
                fd,
                _canonical_json(
                    {
                        "pid": os.getpid(),
                        "evidence_run_id": evidence_run_id,
                        "staging_generation": self.staging_dir.name,
                        "audit_only": True,
                    }
                ).encode("utf-8"),
            )
            self._recover_orphan_staging()
            self.staging_dir.mkdir(parents=True, exist_ok=False)
            (self.staging_dir / ".generation.json").write_text(
                _canonical_json(
                    {
                        "evidence_run_id": evidence_run_id,
                        "target": self.final_dir.name,
                        "staging_generation": self.staging_dir.name,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        except BaseException:
            self.release()
            raise

    def _recover_orphan_staging(self) -> None:
        prefix = f".{self.final_dir.name}.staging-"
        for candidate in sorted(self.final_dir.parent.glob(f"{prefix}*")):
            if candidate == self.staging_dir or not candidate.is_dir():
                continue
            self.recovered_orphans.append(candidate.name)
            shutil.rmtree(candidate)

    def write_json(self, name: str, value: dict[str, Any]) -> None:
        payload = {"evidence_run_id": self.evidence_run_id, **value}
        target = self.staging_dir / name
        target.write_text(_canonical_json(payload) + "\n", encoding="utf-8")

    def publish(self) -> dict[str, Any]:
        try:
            if self.final_dir.exists():
                raise GovernanceError("evidence_final_already_exists")
            hashes: list[str] = []
            for path in sorted(item for item in self.staging_dir.rglob("*") if item.is_file()):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                hashes.append(f"{digest}  {path.relative_to(self.staging_dir)}")
            (self.staging_dir / "hashes.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
            self._fsync_dir(self.staging_dir)
            self.staging_dir.replace(self.final_dir)
            self._fsync_dir(self.final_dir.parent)
            return {
                "status": "published",
                "evidence_run_id": self.evidence_run_id,
                "final_dir": str(self.final_dir),
                "recovered_orphans": self.recovered_orphans,
            }
        except BaseException:
            shutil.rmtree(self.staging_dir, ignore_errors=True)
            raise
        finally:
            self.release()

    def abort(self) -> None:
        shutil.rmtree(self.staging_dir, ignore_errors=True)
        self.release()

    def release(self) -> None:
        if self._lock_fd is not None:
            try:
                os.ftruncate(self._lock_fd, 0)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
