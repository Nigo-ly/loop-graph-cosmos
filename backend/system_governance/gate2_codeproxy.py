"""Offline CodeProxy R1 contract and fail-closed synthetic runner."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from system_governance.core import GovernanceError

CONTRACT_VERSION = "codeproxy-r1-revision-3"
LEDGER_IDENTITY = "p4a-gate2-codeproxy-r1-revision-3-independent"
LEDGER_SCHEMA_VERSION = 2
LEDGER_APPLICATION_ID = 0x43505231
REQUESTED_ROUTE_IDENTITY = "codeproxy-p4/gpt-5.6-sol"
ENDPOINT = "https://code2proxy.xyz/v1/chat/completions"
MODEL = "gpt-5.6-sol"
STAGE_A_AUTHORIZATION = "NIGO_AUTHORIZE_P4A_GATE2_CODEPROXY_R1_STAGE_A_1_TOTAL"
STAGE_A_FIXTURE_ID = "p4a-gate2-codeproxy-r1-stage-a-public-v1"
STAGE_B_FIXTURE_ID = "p4a-gate2-codeproxy-r1-stage-b-public-tool-v1"
STAGE_A_SCHEMA_ID = "p4a_gate2_codeproxy_r1_stage_a_v1"
STAGE_B_TOOL_NAME = "codeproxy_public_sum"
MAX_REQUESTS = 1
MAX_OUTPUT_TOKENS = 512
STAGE_A_REQUEST_SHA256 = "05a8b54e06ba972ae582ee9bd8ae56eb4c914e1f78fad94064b36391504e1cd0"
STAGE_B_REQUEST_SHA256 = "5b6a4886a27aa1aaab345ffca39ce71b42584b59148588bb5dc4872b42789f64"

_SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "Process only the public synthetic CodeProxy R1 fixture. Do not infer, fetch, "
        "or request any private or external data."
    ),
}
_STAGE_A_EXPECTED_OUTPUT = {
    "fixture": STAGE_A_FIXTURE_ID,
    "result": "CODEPROXY_R1_STAGE_A_OK",
}
_STAGE_A_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fixture", "result"],
    "properties": {
        "fixture": {"type": "string", "const": STAGE_A_FIXTURE_ID},
        "result": {"type": "string", "const": "CODEPROXY_R1_STAGE_A_OK"},
    },
}
_STAGE_B_ARGUMENTS = {"left": 19, "right": 23}
_STAGE_B_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": STAGE_B_TOOL_NAME,
        "description": "Return the sum of two fixed public synthetic integers.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["left", "right"],
            "properties": {
                "left": {"type": "integer", "const": 19},
                "right": {"type": "integer", "const": 23},
            },
        },
    },
}


class Stage(StrEnum):
    A = "stage_a"
    B = "stage_b"


FROZEN_REQUEST_SHA256 = {
    Stage.A: STAGE_A_REQUEST_SHA256,
    Stage.B: STAGE_B_REQUEST_SHA256,
}


class EvidenceOrigin(StrEnum):
    SYNTHETIC_ONLY = "synthetic_only"
    PRODUCTION_TRANSPORT = "production_transport"


class BillingSource(StrEnum):
    SYNTHETIC_FIXTURE = "synthetic_fixture"


class QuotaFact(StrEnum):
    WITHIN_LIMIT = "within_limit"
    QUOTA_EXCEEDED = "quota_exceeded"
    BALANCE_INSUFFICIENT = "balance_insufficient"


class QuotaSource(StrEnum):
    SYNTHETIC_FIXTURE = "synthetic_fixture"


class IdentitySource(StrEnum):
    SYNTHETIC_FIXTURE = "synthetic_fixture"


@dataclass(frozen=True)
class CodeProxyRequest:
    stage: Stage
    fixture_id: str
    body: dict[str, Any]

    @property
    def canonical_body(self) -> str:
        return canonical_json(self.body)

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(self.canonical_body.encode("utf-8")).hexdigest()

    @property
    def canonical_body_bytes(self) -> int:
        return len(self.canonical_body.encode("utf-8"))

    @property
    def canonical_body_characters(self) -> int:
        return len(self.canonical_body)


@dataclass(frozen=True)
class TransportResult:
    """Transient facts. Production transport intentionally supplies no governance facts."""

    request_sent: bool | None
    http_status: int | None
    response_json: Mapping[str, Any] | None = None
    evidence_origin: EvidenceOrigin = EvidenceOrigin.PRODUCTION_TRANSPORT
    provider_billed_cost_usd: float | None = None
    provider_billing_source: BillingSource | str | None = None
    quota_fact: QuotaFact | str | None = None
    quota_source: QuotaSource | str | None = None
    upstream_identity_attested: bool = False
    upstream_attestation_source: IdentitySource | str | None = None
    local_error_category: str | None = None


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def build_stage_a_request() -> CodeProxyRequest:
    body: dict[str, Any] = {
        "model": MODEL,
        "store": False,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            _SYSTEM_MESSAGE,
            {
                "role": "user",
                "content": (
                    "Return the fixed public fixture result using the required JSON schema: "
                    f"fixture={STAGE_A_FIXTURE_ID}; result=CODEPROXY_R1_STAGE_A_OK."
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": STAGE_A_SCHEMA_ID,
                "strict": True,
                "schema": _STAGE_A_SCHEMA,
            },
        },
    }
    return CodeProxyRequest(Stage.A, STAGE_A_FIXTURE_ID, body)


def build_stage_b_request() -> CodeProxyRequest:
    body: dict[str, Any] = {
        "model": MODEL,
        "store": False,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            _SYSTEM_MESSAGE,
            {
                "role": "user",
                "content": (
                    "Call codeproxy_public_sum exactly once with left=19 and right=23. "
                    "Do not perform any other work."
                ),
            },
        ],
        "tools": [_STAGE_B_TOOL],
        "tool_choice": {"type": "function", "function": {"name": STAGE_B_TOOL_NAME}},
    }
    return CodeProxyRequest(Stage.B, STAGE_B_FIXTURE_ID, body)


def normalized_request_summary(request: CodeProxyRequest) -> dict[str, Any]:
    return {
        "method": "POST",
        "path": "/v1/chat/completions",
        "fixture_id": request.fixture_id,
        "requested_route_identity": REQUESTED_ROUTE_IDENTITY,
        "model": MODEL,
        "store": False,
        "canonical_body_bytes": request.canonical_body_bytes,
        "canonical_body_characters": request.canonical_body_characters,
        "input_token_bound_status": "tokenizer_not_frozen",
        "output_token_limit": MAX_OUTPUT_TOKENS,
        "schema_id": STAGE_A_SCHEMA_ID if request.stage is Stage.A else None,
        "tool_declaration_count": 0 if request.stage is Stage.A else 1,
    }


def validate_frozen_request(request: CodeProxyRequest) -> None:
    expected = build_stage_a_request() if request.stage is Stage.A else build_stage_b_request()
    if request != expected:
        raise GovernanceError("codeproxy_r1_request_drift")
    if request.request_sha256 != FROZEN_REQUEST_SHA256[request.stage]:
        raise GovernanceError("codeproxy_r1_request_drift")
    if {"temperature", "top_p", "n", "metadata"}.intersection(request.body):
        raise GovernanceError("codeproxy_r1_forbidden_request_field")


def require_live_stage_ready(stage: Stage, authorization: str) -> None:
    """Validate stage, authorization, then the currently unfrozen live-evidence gate."""
    if stage is Stage.B:
        raise GovernanceError("codeproxy_r1_stage_b_live_not_implemented")
    if authorization != STAGE_A_AUTHORIZATION:
        raise GovernanceError("codeproxy_r1_stage_a_authorization_required")
    raise GovernanceError("codeproxy_r1_live_evidence_sources_not_frozen")


_SCHEMA = """
CREATE TABLE ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL UNIQUE CHECK(stage = 'stage_a'),
    fixture_id TEXT NOT NULL CHECK(fixture_id = 'p4a-gate2-codeproxy-r1-stage-a-public-v1'),
    request_sha256 TEXT NOT NULL,
    normalized_summary_json TEXT NOT NULL,
    request_body_bytes INTEGER NOT NULL CHECK(request_body_bytes > 0),
    reserved_output_tokens INTEGER NOT NULL CHECK(reserved_output_tokens = 512),
    status TEXT NOT NULL CHECK(status = 'consumed'),
    reserved_at TEXT NOT NULL
);
CREATE TABLE outcomes (
    reservation_id TEXT PRIMARY KEY REFERENCES reservations(reservation_id),
    request_sent INTEGER CHECK(request_sent IN (0, 1) OR request_sent IS NULL),
    evidence_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""
_EXPECTED_COLUMNS = {
    "ledger_meta": (
        ("key", "TEXT", 0, 1),
        ("value", "TEXT", 1, 0),
    ),
    "reservations": (
        ("reservation_id", "TEXT", 0, 1),
        ("stage", "TEXT", 1, 0),
        ("fixture_id", "TEXT", 1, 0),
        ("request_sha256", "TEXT", 1, 0),
        ("normalized_summary_json", "TEXT", 1, 0),
        ("request_body_bytes", "INTEGER", 1, 0),
        ("reserved_output_tokens", "INTEGER", 1, 0),
        ("status", "TEXT", 1, 0),
        ("reserved_at", "TEXT", 1, 0),
    ),
    "outcomes": (
        ("reservation_id", "TEXT", 0, 1),
        ("request_sent", "INTEGER", 0, 0),
        ("evidence_json", "TEXT", 1, 0),
        ("recorded_at", "TEXT", 1, 0),
    ),
}


def _normalized_sql(value: str) -> str:
    return " ".join(value.split())


_EXPECTED_TABLE_SQL = {
    statement.split()[2]: _normalized_sql(statement)
    for statement in _SCHEMA.split(";")
    if statement.strip()
}

_RESERVATION_ID = re.compile(r"cp-r1-[0-9a-f]{32}\Z")
_SENSITIVE_ID = re.compile(
    r"(?:sk|key|token|bearer|authorization|codeproxy_p4_api_key)", re.IGNORECASE
)


class CodeProxyLedger:
    """Independent R1 ledger with read-only identity checks before any write."""

    def __init__(
        self,
        path: Path,
        *,
        id_factory: Callable[[], str] = lambda: f"cp-r1-{uuid.uuid4().hex}",
    ) -> None:
        self.path = Path(path)
        self._id_factory = id_factory
        self._reject_symlink_components()
        if self.path.exists():
            self._validate_existing()

    def _reject_symlink_components(self) -> None:
        absolute = self.path.absolute()
        for component in reversed((absolute, *absolute.parents)):
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise GovernanceError("codeproxy_r1_ledger_symlink_rejected")
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISREG(mode):
            raise GovernanceError("codeproxy_r1_ledger_not_regular_file")

    def _readonly_connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path), safe='/')}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _validate_existing(self) -> None:
        self._reject_symlink_components()
        try:
            with self._readonly_connect() as connection:
                application_id = connection.execute("PRAGMA application_id").fetchone()[0]
                if application_id != LEDGER_APPLICATION_ID:
                    raise GovernanceError("codeproxy_r1_ledger_identity_mismatch")
                if connection.execute("PRAGMA user_version").fetchone()[0] != LEDGER_SCHEMA_VERSION:
                    raise GovernanceError("codeproxy_r1_ledger_schema_mismatch")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    )
                }
                if tables != set(_EXPECTED_COLUMNS):
                    raise GovernanceError("codeproxy_r1_ledger_schema_mismatch")
                schema_objects = {
                    row[0]: _normalized_sql(row[1])
                    for row in connection.execute(
                        "SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL"
                    )
                }
                if schema_objects != _EXPECTED_TABLE_SQL:
                    raise GovernanceError("codeproxy_r1_ledger_schema_mismatch")
                for table, expected in _EXPECTED_COLUMNS.items():
                    columns = tuple(
                        (row[1], row[2], row[3], row[5])
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    )
                    if columns != expected:
                        raise GovernanceError("codeproxy_r1_ledger_schema_mismatch")
                meta = dict(connection.execute("SELECT key, value FROM ledger_meta"))
                if meta != {
                    "contract_version": CONTRACT_VERSION,
                    "ledger_identity": LEDGER_IDENTITY,
                    "schema_version": str(LEDGER_SCHEMA_VERSION),
                }:
                    raise GovernanceError("codeproxy_r1_ledger_identity_mismatch")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise GovernanceError("codeproxy_r1_ledger_integrity_failed")
        except GovernanceError:
            raise
        except sqlite3.Error as error:
            raise GovernanceError("codeproxy_r1_ledger_identity_mismatch") from error

    def _initialize_candidate(self, candidate: Path) -> None:
        with sqlite3.connect(candidate) as connection:
            connection.execute(f"PRAGMA application_id={LEDGER_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={LEDGER_SCHEMA_VERSION}")
            connection.executescript(_SCHEMA)
            connection.executemany(
                "INSERT INTO ledger_meta(key, value) VALUES (?, ?)",
                (
                    ("contract_version", CONTRACT_VERSION),
                    ("ledger_identity", LEDGER_IDENTITY),
                    ("schema_version", str(LEDGER_SCHEMA_VERSION)),
                ),
            )
        os.chmod(candidate, 0o600)
        descriptor = os.open(candidate, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _open_or_initialize(self) -> None:
        self._reject_symlink_components()
        if self.path.exists():
            self._validate_existing()
            return
        if not self.path.parent.is_dir():
            raise GovernanceError("codeproxy_r1_ledger_parent_missing")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.init-", dir=self.path.parent
        )
        os.close(descriptor)
        candidate = Path(temporary)
        try:
            self._initialize_candidate(candidate)
            try:
                os.link(candidate, self.path, follow_symlinks=False)
            except FileExistsError:
                self._validate_existing()
                return
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            candidate.unlink(missing_ok=True)
        self._validate_existing()

    def _connect(self) -> sqlite3.Connection:
        self._open_or_initialize()
        self._validate_existing()
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def check_available(self, stage: Stage) -> None:
        if stage is Stage.B:
            raise GovernanceError("codeproxy_r1_stage_b_live_not_implemented")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM reservations WHERE stage = ?", (stage.value,)
            ).fetchone()
        if row is not None:
            raise GovernanceError("codeproxy_r1_stage_a_request_limit")

    def _new_reservation_id(self) -> str:
        reservation_id = self._id_factory()
        _validate_reservation_id(reservation_id)
        return reservation_id

    def reserve(self, request: CodeProxyRequest) -> str:
        validate_frozen_request(request)
        if request.stage is not Stage.A:
            raise GovernanceError("codeproxy_r1_stage_b_live_not_implemented")
        reservation_id = self._new_reservation_id()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO reservations(
                           reservation_id, stage, fixture_id, request_sha256,
                           normalized_summary_json, request_body_bytes,
                           reserved_output_tokens, status, reserved_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'consumed', ?)""",
                    (
                        reservation_id,
                        request.stage.value,
                        request.fixture_id,
                        request.request_sha256,
                        canonical_json(normalized_request_summary(request)),
                        request.canonical_body_bytes,
                        MAX_OUTPUT_TOKENS,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise GovernanceError("codeproxy_r1_stage_a_request_limit") from error
        return reservation_id

    def record_outcome(
        self,
        reservation_id: str,
        result: TransportResult,
        *,
        observed_at: str,
    ) -> dict[str, Any]:
        _validate_reservation_id(reservation_id)
        evidence = evaluate_stage_a_result(
            build_stage_a_request(), reservation_id, result, observed_at=observed_at
        )
        encoded = canonical_json(evidence)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM reservations WHERE reservation_id = ?", (reservation_id,)
            ).fetchone() is None:
                raise GovernanceError("codeproxy_r1_reservation_missing")
            try:
                connection.execute(
                    """INSERT INTO outcomes(
                           reservation_id, request_sent, evidence_json, recorded_at
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        reservation_id,
                        None if result.request_sent is None else int(result.request_sent),
                        encoded,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise GovernanceError("codeproxy_r1_outcome_already_recorded") from error
        return evidence

    def public_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT r.reservation_id, r.stage, r.fixture_id,
                          r.request_sha256, r.normalized_summary_json, r.status,
                          r.reserved_at, o.request_sent, o.evidence_json, o.recorded_at
                     FROM reservations r LEFT JOIN outcomes o USING (reservation_id)
                    ORDER BY r.reserved_at, r.reservation_id"""
            ).fetchall()
        return {
            "ledger_identity": LEDGER_IDENTITY,
            "contract_version": CONTRACT_VERSION,
            "limits": {
                "max_requests": MAX_REQUESTS,
                "input_token_bound_status": "tokenizer_not_frozen",
                "output_token_limit": MAX_OUTPUT_TOKENS,
                "cost_limit_status": "pricing_not_frozen",
            },
            "reservations": [
                {
                    "reservation_id": row["reservation_id"],
                    "stage": row["stage"],
                    "fixture_id": row["fixture_id"],
                    "normalized_request_sha256": row["request_sha256"],
                    "normalized_request_summary": json.loads(row["normalized_summary_json"]),
                    "reservation_status": row["status"],
                    "reserved_at": row["reserved_at"],
                    "request_sent": (
                        None if row["request_sent"] is None else bool(row["request_sent"])
                    ),
                    "outcome": (
                        None if row["evidence_json"] is None else json.loads(row["evidence_json"])
                    ),
                    "outcome_recorded_at": row["recorded_at"],
                }
                for row in rows
            ],
        }


_LOCAL_ERRORS = {
    "none",
    "transport_exception",
    "transport_failure",
    "response_too_large",
    "redirect_rejected",
    "http_failure",
    "response_not_object",
    "response_decode_failure",
}


def _validate_reservation_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or _RESERVATION_ID.fullmatch(value) is None
        or _SENSITIVE_ID.search(value) is not None
    ):
        raise GovernanceError("codeproxy_r1_reservation_id_invalid")


def _valid_count(value: object, maximum: int) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        return None
    return value


def _valid_cost(value: object) -> float | None:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def _validate_observed_at(value: object) -> None:
    if not isinstance(value, str):
        raise GovernanceError("codeproxy_r1_evidence_schema_invalid")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as error:
        raise GovernanceError("codeproxy_r1_evidence_schema_invalid") from error
    if observed.utcoffset() is None:
        raise GovernanceError("codeproxy_r1_evidence_schema_invalid")


def _validate_transport_result(result: TransportResult) -> None:
    if not isinstance(result, TransportResult):
        raise GovernanceError("codeproxy_r1_transport_facts_invalid")
    if result.evidence_origin not in {
        EvidenceOrigin.SYNTHETIC_ONLY,
        EvidenceOrigin.PRODUCTION_TRANSPORT,
    }:
        raise GovernanceError("codeproxy_r1_transport_facts_invalid")
    if result.request_sent is not None and not isinstance(result.request_sent, bool):
        raise GovernanceError("codeproxy_r1_transport_facts_invalid")
    if result.http_status is not None and (
        not isinstance(result.http_status, int)
        or isinstance(result.http_status, bool)
        or not 100 <= result.http_status <= 599
    ):
        raise GovernanceError("codeproxy_r1_transport_facts_invalid")
    if result.response_json is not None and not isinstance(result.response_json, Mapping):
        raise GovernanceError("codeproxy_r1_transport_facts_invalid")

    local_error = result.local_error_category or "none"
    if local_error not in _LOCAL_ERRORS:
        raise GovernanceError("codeproxy_r1_transport_facts_invalid")

    governance_facts = (
        result.provider_billed_cost_usd,
        result.provider_billing_source,
        result.quota_fact,
        result.quota_source,
        result.upstream_attestation_source,
    )
    if result.evidence_origin is EvidenceOrigin.PRODUCTION_TRANSPORT:
        if (
            any(value is not None for value in governance_facts)
            or result.upstream_identity_attested
        ):
            raise GovernanceError("codeproxy_r1_evidence_origin_inconsistent")
    elif not all(
        (
            _valid_cost(result.provider_billed_cost_usd) is not None,
            result.provider_billing_source is BillingSource.SYNTHETIC_FIXTURE,
            result.quota_fact is QuotaFact.WITHIN_LIMIT,
            result.quota_source is QuotaSource.SYNTHETIC_FIXTURE,
            result.upstream_identity_attested is True,
            result.upstream_attestation_source is IdentitySource.SYNTHETIC_FIXTURE,
        )
    ):
        raise GovernanceError("codeproxy_r1_evidence_origin_inconsistent")

    sent = result.request_sent
    status = result.http_status
    response = result.response_json
    route_consistent = False
    if local_error == "none":
        route_consistent = sent is True and status == 200 and response is not None
    elif local_error in {"transport_exception", "transport_failure"}:
        route_consistent = status is None and response is None
    elif local_error == "response_too_large":
        route_consistent = sent is True and status is not None and response is None
    elif local_error == "redirect_rejected":
        route_consistent = sent is True and status in {301, 302, 303, 307, 308} and response is None
    elif local_error == "http_failure":
        route_consistent = (
            sent is True
            and status is not None
            and status != 200
            and status not in {301, 302, 303, 307, 308}
            and response is None
        )
    elif local_error in {"response_not_object", "response_decode_failure"}:
        route_consistent = sent is True and status == 200 and response is None
    if not route_consistent:
        raise GovernanceError("codeproxy_r1_route_facts_inconsistent")

    if response is not None:
        model = response.get("model")
        if model is not None and not isinstance(model, str):
            raise GovernanceError("codeproxy_r1_route_facts_inconsistent")
        usage = response.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise GovernanceError("codeproxy_r1_usage_facts_inconsistent")
            if (
                _valid_count(usage.get("prompt_tokens"), 2**31 - 1) is None
                or _valid_count(usage.get("completion_tokens"), MAX_OUTPUT_TOKENS) is None
            ):
                raise GovernanceError("codeproxy_r1_usage_facts_inconsistent")


def evaluate_stage_a_result(
    request: CodeProxyRequest,
    reservation_id: str,
    result: TransportResult,
    *,
    observed_at: str,
) -> dict[str, Any]:
    validate_frozen_request(request)
    _validate_reservation_id(reservation_id)
    _validate_observed_at(observed_at)
    _validate_transport_result(result)
    response = result.response_json if isinstance(result.response_json, Mapping) else {}
    choices = response.get("choices")
    structured: object = None
    try:
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                structured = json.loads(message["content"])
    except json.JSONDecodeError:
        structured = None
    schema_status = "passed" if structured == _STAGE_A_EXPECTED_OUTPUT else "failed"
    usage_value = response.get("usage")
    usage = usage_value if isinstance(usage_value, Mapping) else {}
    input_tokens = _valid_count(usage.get("prompt_tokens"), 2**31 - 1)
    output_tokens = _valid_count(usage.get("completion_tokens"), MAX_OUTPUT_TOKENS)
    usage_known = input_tokens is not None and output_tokens is not None
    synthetic = result.evidence_origin is EvidenceOrigin.SYNTHETIC_ONLY
    billed_cost = _valid_cost(result.provider_billed_cost_usd)
    billing_known = (
        synthetic
        and billed_cost is not None
        and result.provider_billing_source is BillingSource.SYNTHETIC_FIXTURE
    )
    quota_known = (
        synthetic
        and result.quota_fact is QuotaFact.WITHIN_LIMIT
        and result.quota_source is QuotaSource.SYNTHETIC_FIXTURE
    )
    identity_attested = (
        synthetic
        and result.upstream_identity_attested is True
        and result.upstream_attestation_source is IdentitySource.SYNTHETIC_FIXTURE
    )
    response_model = response.get("model")
    response_model_claim = (
        "present_unverified"
        if isinstance(response_model, str) and response_model.strip()
        else "absent"
    )
    local_error = result.local_error_category or "none"
    route_observed = all(
        (
            result.request_sent is True,
            result.http_status == 200,
            schema_status == "passed",
            response_model_claim == "present_unverified",
            usage_known,
            local_error == "none",
        )
    )
    synthetic_complete = all(
        (synthetic, route_observed, billing_known, quota_known, identity_attested)
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "evidence_class": result.evidence_origin.value,
        "fixture_id": request.fixture_id,
        "normalized_request_summary": normalized_request_summary(request),
        "normalized_request_sha256": request.request_sha256,
        "reservation_id": reservation_id,
        "reservation_status": "consumed",
        "request_sent": result.request_sent,
        "usage": {
            "status": "known" if usage_known else "unknown",
            "input_tokens": input_tokens if input_tokens is not None else "unknown",
            "output_tokens": output_tokens if output_tokens is not None else "unknown",
            "source": "response_usage" if usage_known else "not_available",
        },
        "route_capability": {
            "status": "observed" if route_observed else "failed",
            "http_status": result.http_status if result.http_status is not None else "unknown",
            "structured_output": schema_status,
            "response_model_claim": response_model_claim,
            "conclusion": "synthetic_only" if synthetic and route_observed else "failed",
        },
        "billing": {
            "status": "known" if billing_known else "unknown",
            "cost_usd": billed_cost if billing_known else "unknown",
            "source": "synthetic_fixture" if billing_known else "not_available",
            "conclusion": "synthetic_only" if billing_known else "failed",
        },
        "quota": {
            "status": "known" if quota_known else "unknown",
            "fact": "within_limit" if quota_known else "unknown",
            "source": "synthetic_fixture" if quota_known else "not_available",
            "conclusion": "synthetic_only" if quota_known else "failed",
        },
        "identity": {
            "requested_route_identity": REQUESTED_ROUTE_IDENTITY,
            "response_model_claim": response_model_claim,
            "upstream_identity_status": "attested" if identity_attested else "unknown",
            "upstream_identity": "synthetic_attested_identity" if identity_attested else "unknown",
            "source": "synthetic_fixture" if identity_attested else "not_available",
            "conclusion": "synthetic_only" if identity_attested else "failed",
        },
        "observed_at": observed_at,
        "stage_conclusion": "synthetic_only" if synthetic_complete else "failed",
        "live_reachability": "not_evaluated",
        "independent_review_conclusion": "not_performed",
        "local_error_category": local_error,
    }


class CodeProxyStageRunner:
    """Offline evaluator that consumes caller-supplied transport facts only."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        id_factory: Callable[[], str] = lambda: f"cp-r1-{uuid.uuid4().hex}",
        clock: Callable[[], str] = lambda: datetime.now(UTC).isoformat(),
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self.id_factory = id_factory
        self.clock = clock

    def run_stage_a(self, authorization: str) -> dict[str, Any]:
        require_live_stage_ready(Stage.A, authorization)
        raise AssertionError("unreachable")

    def run_synthetic_stage_a(
        self, authorization: str, result: TransportResult
    ) -> dict[str, Any]:
        if authorization != STAGE_A_AUTHORIZATION:
            raise GovernanceError("codeproxy_r1_stage_a_authorization_required")
        if result.evidence_origin is not EvidenceOrigin.SYNTHETIC_ONLY:
            raise GovernanceError("codeproxy_r1_synthetic_result_required")
        observed_at = self.clock()
        _validate_observed_at(observed_at)
        _validate_transport_result(result)
        request = build_stage_a_request()
        validate_frozen_request(request)
        ledger = CodeProxyLedger(self.ledger_path, id_factory=self.id_factory)
        reservation_id = ledger.reserve(request)
        return ledger.record_outcome(
            reservation_id,
            result,
            observed_at=observed_at,
        )

    def run_stage_b(self, authorization: str) -> dict[str, Any]:
        require_live_stage_ready(Stage.B, authorization)
        raise AssertionError("unreachable")
