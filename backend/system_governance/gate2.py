"""P4A Gate 2 provider canary contracts.

The module contains no credential lookup and performs no I/O by itself. Callers
must inject a credential reader and a transport. Requests are restricted to the
frozen public synthetic handshake, JSON schema, and side-effect-free sum tool.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from system_governance.core import GovernanceError, ModelConfig, PrivacyLevel, select_role_pair

FROZEN_HANDSHAKE = "P4A_GATE2_PUBLIC_SYNTHETIC_HANDSHAKE_V1"
FROZEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["handshake", "status"],
    "properties": {
        "handshake": {"type": "string", "const": FROZEN_HANDSHAKE},
        "status": {"type": "string", "const": "ok"},
    },
}
FROZEN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "synthetic_sum",
        "description": "Return the sum of two public synthetic integers without side effects.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["left", "right"],
            "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
        },
    },
}
FROZEN_TOOL_ARGUMENTS = {"left": 17, "right": 25}
FROZEN_KEYCHAIN_SERVICES = frozenset({"p4a-provider-kimi", "p4a-provider-deepseek"})
PROVIDER_ERROR_FACT_MAX_LENGTH = 128
PROVIDER_ERROR_MESSAGE_MAX_LENGTH = 4096
UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"
P4A_GATE2_ID = "P4A-GATE2"
P4A_GATE2_RECONCILIATION_CONFIRMATION = "CODEX_RECONCILE_P4A_GATE2_18_SENT_REQUESTS"
P4A_GATE2_INCREMENTAL_CONFIRMATION = (
    "NIGO_AUTHORIZE_P4A_GATE2_INCREMENTAL_3_PER_PROVIDER_6_TOTAL"
)
P4A_GATE2_R7_STAGE_A_CONFIRMATION = (
    "NIGO_AUTHORIZE_P4A_GATE2_R7_STAGE_A_1_PER_PROVIDER_2_TOTAL"
)
HISTORICAL_MAX_REQUESTS_PER_PROVIDER = 8
HISTORICAL_MAX_REQUESTS_TOTAL = 16
INCREMENTAL_MAX_REQUESTS_PER_PROVIDER = 3
INCREMENTAL_MAX_REQUESTS_TOTAL = 6
INCREMENTAL_MAX_INPUT_TOKENS = 4_000
INCREMENTAL_MAX_OUTPUT_TOKENS = 1_536
INCREMENTAL_MAX_COST_USD = 1.0
STAGE_A_MAX_REQUESTS_PER_PROVIDER = 1
STAGE_A_MAX_REQUESTS_TOTAL = 2
STAGE_A_MAX_INPUT_TOKENS = 2_000
STAGE_A_MAX_OUTPUT_TOKENS = 512
STAGE_A_MAX_COST_USD = 0.25
STAGE_A_LEDGER_SOURCE = "r8_stage_a_validation"

_SENSITIVE_PROVIDER_ERROR_VALUE = re.compile(
    r"(?i)(?:bearer\s+|authorization\b|sk-[a-z0-9]|/(?:users|private|home)/|[a-z]:\\users\\)"
)
_PROVIDER_ERROR_CATEGORY_RULES: tuple[
    tuple[str, tuple[tuple[str, ...], ...]], ...
] = (
    (
        "unsupported_field_metadata",
        (
            ("metadata", "not supported"),
            ("metadata", "unsupported"),
            ("metadata", "unknown field"),
            ("metadata", "unrecognized field"),
            ("metadata", "unsupported parameter"),
        ),
    ),
    (
        "unsupported_response_format",
        (
            ("response_format", "not supported"),
            ("response_format", "unsupported"),
            ("response_format", "unknown field"),
            ("response_format", "unrecognized"),
            ("json_schema", "not supported"),
            ("json schema", "not supported"),
        ),
    ),
    (
        "unsupported_tool_call",
        (
            ("tool_choice", "not supported"),
            ("tool_choice", "unsupported"),
            ("tools", "not supported"),
            ("tools", "unsupported"),
            ("function calling", "not supported"),
            ("function calling", "unsupported"),
        ),
    ),
    (
        "authentication_rejected",
        (
            ("invalid api key",),
            ("incorrect api key",),
            ("authentication failed",),
            ("authentication error",),
            ("unauthorized",),
        ),
    ),
    (
        "rate_limit",
        (
            ("rate limit",),
            ("too many requests",),
            ("quota exceeded",),
        ),
    ),
    (
        "model_rejected",
        (
            ("model", "not found"),
            ("model", "does not exist"),
            ("model", "not available"),
            ("invalid model",),
        ),
    ),
    (
        "other_invalid_request",
        (
            ("invalid request",),
            ("request is invalid",),
        ),
    ),
)


class ProbeKind(StrEnum):
    STRUCTURED = "structured_output"
    TOOL = "tool_call"
    RECOVERY = "disconnect_recovery"


class AttemptStatus(StrEnum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    SERVER_ERROR = "server_error"
    PERMANENT_ERROR = "permanent_error"


@dataclass
class _ProviderFailureState:
    """Track provider-scoped streaks; a successful response resets both."""

    consecutive_transport_failures: int = 0
    consecutive_rate_limits: int = 0
    blocked_reason: str | None = None

    def observe(self, status: AttemptStatus, *, fault_source: str) -> None:
        if fault_source != "provider_transport":
            return
        if status is AttemptStatus.OK:
            self.consecutive_transport_failures = 0
            self.consecutive_rate_limits = 0
            return
        if status in {AttemptStatus.TIMEOUT, AttemptStatus.DISCONNECTED}:
            self.consecutive_transport_failures += 1
        else:
            self.consecutive_transport_failures = 0
        if status is AttemptStatus.RATE_LIMITED:
            self.consecutive_rate_limits += 1
        else:
            self.consecutive_rate_limits = 0
        if self.consecutive_transport_failures >= 3:
            self.blocked_reason = "gate2_consecutive_transport_failure_limit"
        elif self.consecutive_rate_limits >= 3:
            self.blocked_reason = "gate2_sustained_rate_limit"


@dataclass(frozen=True)
class Gate2Provider:
    provider_id: str
    endpoint: str
    model_id: str
    model_family: str
    quota_pool: str
    keychain_service: str
    role: Literal["worker", "evaluator"]

    def model_config(self) -> ModelConfig:
        return ModelConfig(
            provider_id=self.provider_id,
            endpoint=self.endpoint,
            adapter="openai_chat_compatible",
            model_id=self.model_id,
            model_family=self.model_family,
            context_limit=0,
            output_limit=0,
            capabilities=("structured_output", "tool_calling"),
            privacy=PrivacyLevel.PUBLIC,
            cost_unit="unknown",
            quota_pool=self.quota_pool,
            concurrency_limit=1,
            enabled=False,
            config_version=1,
            identity_status="identity_unverified",
            role=self.role,
        )


FROZEN_PROVIDERS = (
    Gate2Provider(
        provider_id="kimi",
        endpoint="https://api.kimi.com/coding/v1",
        model_id="k3",
        model_family="k3-family",
        quota_pool="kimi-gate2-quota",
        keychain_service="p4a-provider-kimi",
        role="worker",
    ),
    Gate2Provider(
        provider_id="deepseek",
        endpoint="https://api.deepseek.com/v1",
        model_id="deepseek-v4-pro",
        model_family="deepseek-v4-pro-family",
        quota_pool="deepseek-gate2-quota",
        keychain_service="p4a-provider-deepseek",
        role="evaluator",
    ),
)


class CredentialReader(Protocol):
    def __call__(self, service: str) -> str: ...


@dataclass(frozen=True)
class CanaryRequest:
    request_id: str
    provider_id: str
    endpoint: str
    model: str
    kind: ProbeKind
    privacy: PrivacyLevel
    body: dict[str, Any]
    timeout_seconds: float

    @property
    def input_digest(self) -> str:
        encoded = json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TransportAttempt:
    status: AttemptStatus
    request_sent: bool
    http_status: int | None = None
    response_json: dict[str, Any] | None = None
    content_type: str = ""
    final_endpoint: str = ""
    final_peer_ip: str = ""
    redirect_chain: tuple[str, ...] = ()
    tls_verified: bool = False
    first_byte_ms: int | None = None
    total_ms: int | None = None
    error_code: str | None = None
    provider_error_code: str | None = None
    provider_error_type: str | None = None
    provider_error_param: str | None = None
    provider_error_category: str | None = None


def classify_provider_error(message: object) -> str:
    """Classify only explicit provider language using the frozen rule table."""
    if (
        not isinstance(message, str)
        or not message
        or len(message) > PROVIDER_ERROR_MESSAGE_MAX_LENGTH
    ):
        return UNKNOWN_PROVIDER_ERROR
    normalized = message.casefold()
    for category, clauses in _PROVIDER_ERROR_CATEGORY_RULES:
        if any(all(token in normalized for token in clause) for clause in clauses):
            return category
    return UNKNOWN_PROVIDER_ERROR


def _bounded_provider_error_fact(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    fact = value.strip()
    if (
        not fact
        or len(fact) > PROVIDER_ERROR_FACT_MAX_LENGTH
        or not fact.isprintable()
        or _SENSITIVE_PROVIDER_ERROR_VALUE.search(fact)
    ):
        return None
    return fact


def extract_provider_error_facts(payload: bytes) -> dict[str, str | None]:
    """Extract a bounded allowlist of facts without retaining provider message text."""
    facts: dict[str, str | None] = {
        "provider_error_code": None,
        "provider_error_type": None,
        "provider_error_param": None,
        "provider_error_category": UNKNOWN_PROVIDER_ERROR,
    }
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return facts
    if not isinstance(decoded, dict):
        return facts
    error = decoded.get("error")
    if not isinstance(error, dict):
        return facts
    facts["provider_error_code"] = _bounded_provider_error_fact(error.get("code"))
    facts["provider_error_type"] = _bounded_provider_error_fact(error.get("type"))
    facts["provider_error_param"] = _bounded_provider_error_fact(error.get("param"))
    facts["provider_error_category"] = classify_provider_error(error.get("message"))
    return facts


class ProbeBudget:
    """Cross-process, crash-conservative budget for the complete Gate 2 phase."""

    def __init__(
        self,
        path: Path,
        *,
        gate_id: str = P4A_GATE2_ID,
        max_requests_per_provider: int = 8,
        max_requests_total: int = 16,
        max_input_tokens: int = 40_000,
        max_output_tokens: int = 12_000,
        max_cost_usd: float = 5.0,
        require_historical_reconciliation: bool = False,
        incremental_diagnostics: bool = False,
        stage_a_validation: bool = False,
    ) -> None:
        self.path = Path(path)
        self.gate_id = gate_id
        self.max_requests_per_provider = max_requests_per_provider
        self.max_requests_total = max_requests_total
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_cost_microusd = round(max_cost_usd * 1_000_000)
        self.require_historical_reconciliation = require_historical_reconciliation
        self.incremental_diagnostics = incremental_diagnostics
        self.stage_a_validation = stage_a_validation
        self._incremental_confirmation_present = False
        self._stage_a_confirmation_present = False
        if incremental_diagnostics and stage_a_validation:
            raise ValueError("Gate 2 budget modes are mutually exclusive")
        if incremental_diagnostics:
            self.max_requests_per_provider = INCREMENTAL_MAX_REQUESTS_PER_PROVIDER
            self.max_requests_total = INCREMENTAL_MAX_REQUESTS_TOTAL
            self.max_input_tokens = INCREMENTAL_MAX_INPUT_TOKENS
            self.max_output_tokens = INCREMENTAL_MAX_OUTPUT_TOKENS
            self.max_cost_microusd = round(INCREMENTAL_MAX_COST_USD * 1_000_000)
        elif stage_a_validation:
            self.max_requests_per_provider = STAGE_A_MAX_REQUESTS_PER_PROVIDER
            self.max_requests_total = STAGE_A_MAX_REQUESTS_TOTAL
            self.max_input_tokens = STAGE_A_MAX_INPUT_TOKENS
            self.max_output_tokens = STAGE_A_MAX_OUTPUT_TOKENS
            self.max_cost_microusd = round(STAGE_A_MAX_COST_USD * 1_000_000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        for attempt in range(20):
            try:
                with self._connect() as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(
                        """
                CREATE TABLE IF NOT EXISTS probe_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    gate_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('reserved', 'completed', 'failed')),
                    request_sent INTEGER CHECK(request_sent IN (0, 1) OR request_sent IS NULL),
                    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
                    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
                    known_cost_microusd INTEGER NOT NULL DEFAULT 0 CHECK(known_cost_microusd >= 0),
                    cost_unknown INTEGER NOT NULL DEFAULT 0 CHECK(cost_unknown IN (0, 1)),
                    input_tokens_unknown INTEGER NOT NULL DEFAULT 0
                        CHECK(input_tokens_unknown IN (0, 1)),
                    output_tokens_unknown INTEGER NOT NULL DEFAULT 0
                        CHECK(output_tokens_unknown IN (0, 1)),
                    quota_unknown INTEGER NOT NULL DEFAULT 0 CHECK(quota_unknown IN (0, 1)),
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_probe_reservations_gate_provider
                    ON probe_reservations(gate_id, provider_id);
                CREATE TABLE IF NOT EXISTS probe_budget_facts (
                    gate_id TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    fact_value TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (gate_id, fact_key)
                );
                """
                    )
                    columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(probe_reservations)")
                    }
                    for column in (
                        "input_tokens_unknown",
                        "output_tokens_unknown",
                        "quota_unknown",
                    ):
                        if column not in columns:
                            connection.execute(
                                f"ALTER TABLE probe_reservations ADD COLUMN {column} "
                                "INTEGER NOT NULL DEFAULT 0 CHECK("
                                f"{column} IN (0, 1))"
                            )
                    return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).casefold() or attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _totals(
        self, connection: sqlite3.Connection, *, source: str | None = None
    ) -> dict[str, Any]:
        source_clause = " AND source = ?" if source is not None else ""
        parameters: tuple[str, ...] = (
            (self.gate_id, source) if source is not None else (self.gate_id,)
        )
        rows = connection.execute(
            """SELECT provider_id, COUNT(*) AS requests,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(known_cost_microusd), 0) AS known_cost_microusd,
                      COALESCE(MAX(cost_unknown), 0) AS cost_unknown,
                      COALESCE(MAX(input_tokens_unknown), 0) AS input_tokens_unknown,
                      COALESCE(MAX(output_tokens_unknown), 0) AS output_tokens_unknown,
                      COALESCE(MAX(quota_unknown), 0) AS quota_unknown
                 FROM probe_reservations WHERE gate_id = ?"""
            + source_clause
            + " GROUP BY provider_id",
            parameters,
        ).fetchall()
        providers = {str(row["provider_id"]): int(row["requests"]) for row in rows}
        return {
            "requests_by_provider": providers,
            "total_requests": sum(providers.values()),
            "input_tokens": sum(int(row["input_tokens"]) for row in rows),
            "output_tokens": sum(int(row["output_tokens"]) for row in rows),
            "known_cost_microusd": sum(int(row["known_cost_microusd"]) for row in rows),
            "cost_unknown": any(bool(row["cost_unknown"]) for row in rows),
            "input_tokens_unknown": any(bool(row["input_tokens_unknown"]) for row in rows),
            "output_tokens_unknown": any(bool(row["output_tokens_unknown"]) for row in rows),
            "quota_unknown": any(bool(row["quota_unknown"]) for row in rows),
        }

    def _budget_totals(self, connection: sqlite3.Connection) -> dict[str, Any]:
        source = None
        if self.incremental_diagnostics:
            source = "live_probe"
        elif self.stage_a_validation:
            source = STAGE_A_LEDGER_SOURCE
        return self._totals(connection, source=source)

    def _limit_error(self, totals: dict[str, Any], provider_id: str) -> str | None:
        if totals["requests_by_provider"].get(provider_id, 0) >= self.max_requests_per_provider:
            return "gate2_provider_request_limit"
        if totals["total_requests"] >= self.max_requests_total:
            return "gate2_total_request_limit"
        if totals["input_tokens_unknown"] or totals["output_tokens_unknown"]:
            return "gate2_token_usage_unknown"
        if totals["cost_unknown"]:
            return "gate2_cost_unknown"
        if totals["quota_unknown"]:
            return "gate2_quota_unknown"
        if totals["input_tokens"] >= self.max_input_tokens:
            return "gate2_input_token_limit"
        if totals["output_tokens"] >= self.max_output_tokens:
            return "gate2_output_token_limit"
        if totals["known_cost_microusd"] >= self.max_cost_microusd:
            return "gate2_cost_limit"
        return None

    def _reconciliation_error(self, connection: sqlite3.Connection) -> str | None:
        if not self.require_historical_reconciliation:
            return None
        reconciled = connection.execute(
            """SELECT 1 FROM probe_budget_facts
                WHERE gate_id = ? AND fact_key = 'reconciliation_note'""",
            (self.gate_id,),
        ).fetchone()
        if reconciled is None:
            return "gate2_reconciliation_required"
        if self.stage_a_validation:
            authorized = connection.execute(
                """SELECT 1 FROM probe_budget_facts
                    WHERE gate_id = ? AND fact_key = 'r7_stage_a_authorization'
                      AND fact_value = ?""",
                (self.gate_id, P4A_GATE2_R7_STAGE_A_CONFIRMATION),
            ).fetchone()
            return (
                None
                if authorized is not None and self._stage_a_confirmation_present
                else "gate2_stage_a_authorization_required"
            )
        if not self.incremental_diagnostics:
            return "gate2_historical_cap_breached"
        authorized = connection.execute(
            """SELECT 1 FROM probe_budget_facts
                WHERE gate_id = ? AND fact_key = 'incremental_authorization'
                  AND fact_value = ?""",
            (self.gate_id, P4A_GATE2_INCREMENTAL_CONFIRMATION),
        ).fetchone()
        return (
            None
            if authorized is not None and self._incremental_confirmation_present
            else "gate2_incremental_authorization_required"
        )

    def authorize_incremental_diagnostics(self, confirmation: str) -> None:
        if not self.incremental_diagnostics or confirmation != P4A_GATE2_INCREMENTAL_CONFIRMATION:
            raise GovernanceError("gate2_incremental_authorization_not_confirmed")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts = {
                str(row["fact_key"]): str(row["fact_value"])
                for row in connection.execute(
                    """SELECT fact_key, fact_value FROM probe_budget_facts
                        WHERE gate_id = ?""",
                    (self.gate_id,),
                )
            }
            history = self._totals(connection, source="codex_historical_reconciliation")
            if (
                facts.get("cap_breached") != "true"
                or facts.get("historical_usage") != "unknown"
                or history["requests_by_provider"] != {"kimi": 9, "deepseek": 9}
                or history["total_requests"] != 18
            ):
                raise GovernanceError("gate2_historical_reconciliation_invalid")
            connection.execute(
                """INSERT OR IGNORE INTO probe_budget_facts(
                       gate_id, fact_key, fact_value, recorded_at
                   ) VALUES (?, 'incremental_authorization', ?, ?)""",
                (self.gate_id, P4A_GATE2_INCREMENTAL_CONFIRMATION, now),
            )
        self._incremental_confirmation_present = True

    def authorize_stage_a_validation(self, confirmation: str) -> None:
        if not self.stage_a_validation or confirmation != P4A_GATE2_R7_STAGE_A_CONFIRMATION:
            raise GovernanceError("gate2_stage_a_authorization_not_confirmed")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts = {
                str(row["fact_key"]): str(row["fact_value"])
                for row in connection.execute(
                    """SELECT fact_key, fact_value FROM probe_budget_facts
                        WHERE gate_id = ?""",
                    (self.gate_id,),
                )
            }
            history = self._totals(connection, source="codex_historical_reconciliation")
            revision6 = self._totals(connection, source="live_probe")
            if (
                facts.get("cap_breached") != "true"
                or facts.get("historical_usage") != "unknown"
                or history["requests_by_provider"] != {"kimi": 9, "deepseek": 9}
                or history["total_requests"] != 18
                or revision6["requests_by_provider"] != {"kimi": 1, "deepseek": 1}
                or revision6["total_requests"] != 2
                or not all(
                    revision6[field]
                    for field in (
                        "input_tokens_unknown",
                        "output_tokens_unknown",
                        "cost_unknown",
                        "quota_unknown",
                    )
                )
            ):
                raise GovernanceError("gate2_prior_real_ledger_invalid")
            connection.execute(
                """INSERT OR IGNORE INTO probe_budget_facts(
                       gate_id, fact_key, fact_value, recorded_at
                   ) VALUES (?, 'r7_stage_a_authorization', ?, ?)""",
                (self.gate_id, P4A_GATE2_R7_STAGE_A_CONFIRMATION, now),
            )
        self._stage_a_confirmation_present = True

    def check_request_allowed(self, provider_id: str) -> None:
        with self._connect() as connection:
            error = self._reconciliation_error(connection) or self._limit_error(
                self._budget_totals(connection), provider_id
            )
        if error is not None:
            raise GovernanceError(error)

    def reserve_request(self, provider_id: str, request_id: str) -> str:
        reservation_id = uuid.uuid4().hex
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            error = self._reconciliation_error(connection) or self._limit_error(
                self._budget_totals(connection), provider_id
            )
            if error is not None:
                raise GovernanceError(error)
            connection.execute(
                """INSERT INTO probe_reservations(
                       reservation_id, gate_id, provider_id, request_id, status,
                       request_sent, source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'reserved', NULL, ?, ?, ?)""",
                (
                    reservation_id,
                    self.gate_id,
                    provider_id,
                    request_id,
                    STAGE_A_LEDGER_SOURCE if self.stage_a_validation else "live_probe",
                    now,
                    now,
                ),
            )
        return reservation_id

    def finish_request(
        self,
        reservation_id: str,
        *,
        completed: bool,
        request_sent: bool | None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        usage_observed = usage is not None
        usage = usage or {}
        usage_invalid = False
        unknown_without_usage = not usage_observed and request_sent is not False
        if usage_observed:
            try:
                input_tokens, input_tokens_unknown = _usage_count(
                    usage.get("prompt_tokens", usage.get("input_tokens"))
                )
                output_tokens, output_tokens_unknown = _usage_count(
                    usage.get("completion_tokens", usage.get("output_tokens"))
                )
            except GovernanceError:
                usage_invalid = True
                input_tokens = output_tokens = 0
                input_tokens_unknown = output_tokens_unknown = 1
        else:
            input_tokens = output_tokens = 0
            input_tokens_unknown = output_tokens_unknown = int(unknown_without_usage)
        cost = usage.get("cost_usd")
        if (
            isinstance(cost, int | float)
            and not isinstance(cost, bool)
            and math.isfinite(float(cost))
            and float(cost) >= 0
        ):
            known_cost_microusd = round(float(cost) * 1_000_000)
            cost_fact: float | str = float(cost)
            cost_unknown = 0
        elif usage_observed and cost is not None and cost != "":
            usage_invalid = True
            known_cost_microusd = 0
            cost_fact = "unknown"
            cost_unknown = 1
        elif usage_observed or unknown_without_usage:
            known_cost_microusd = 0
            cost_fact = "unknown"
            cost_unknown = 1
        else:
            known_cost_microusd = 0
            cost_fact = "unknown"
            cost_unknown = 0
        quota = usage.get("quota_remaining")
        quota_unknown = int(
            unknown_without_usage or (usage_observed and (quota is None or quota == ""))
        )
        if usage_invalid:
            input_tokens = output_tokens = known_cost_microusd = 0
            input_tokens_unknown = output_tokens_unknown = cost_unknown = quota_unknown = 1
            cost_fact = "unknown"
            quota = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE probe_reservations
                      SET status = ?, request_sent = ?, input_tokens = ?, output_tokens = ?,
                           known_cost_microusd = ?, cost_unknown = ?,
                           input_tokens_unknown = ?, output_tokens_unknown = ?,
                           quota_unknown = ?, updated_at = ?
                    WHERE reservation_id = ? AND gate_id = ? AND status = 'reserved'""",
                (
                    "completed" if completed and not usage_invalid else "failed",
                    None if request_sent is None else int(request_sent),
                    input_tokens,
                    output_tokens,
                    known_cost_microusd,
                    cost_unknown,
                    input_tokens_unknown,
                    output_tokens_unknown,
                    quota_unknown,
                    self._now(),
                    reservation_id,
                    self.gate_id,
                ),
            ).rowcount
            if changed != 1:
                raise GovernanceError("gate2_reservation_not_open")
            totals = self._budget_totals(connection)
            if (
                totals["input_tokens"] > self.max_input_tokens
                or totals["output_tokens"] > self.max_output_tokens
                or totals["known_cost_microusd"] > self.max_cost_microusd
            ):
                connection.execute(
                    """INSERT OR REPLACE INTO probe_budget_facts(
                           gate_id, fact_key, fact_value, recorded_at
                       ) VALUES (?, 'cap_breached', 'true', ?)""",
                    (self.gate_id, self._now()),
                )
        if usage_invalid:
            raise GovernanceError("gate2_invalid_usage")
        if totals["input_tokens"] > self.max_input_tokens:
            raise GovernanceError("gate2_input_token_limit")
        if totals["output_tokens"] > self.max_output_tokens:
            raise GovernanceError("gate2_output_token_limit")
        if totals["known_cost_microusd"] > self.max_cost_microusd:
            raise GovernanceError("gate2_cost_limit")
        return {
            "input_tokens": "unknown" if input_tokens_unknown else input_tokens,
            "output_tokens": "unknown" if output_tokens_unknown else output_tokens,
            "cost_usd": cost_fact,
            "quota_remaining": "unknown" if quota_unknown else quota,
        }

    def public_dict(self) -> dict[str, Any]:
        with self._connect() as connection:
            historical_totals = self._totals(
                connection, source="codex_historical_reconciliation"
            )
            incremental_totals = self._totals(connection, source="live_probe")
            stage_a_totals = self._totals(connection, source=STAGE_A_LEDGER_SOURCE)
            totals = self._budget_totals(connection)
            status_rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM probe_reservations
                    WHERE gate_id = ? GROUP BY status""",
                (self.gate_id,),
            ).fetchall()
            cap_breached = connection.execute(
                """SELECT 1 FROM probe_budget_facts
                    WHERE gate_id = ? AND fact_key = 'cap_breached' AND fact_value = 'true'""",
                (self.gate_id,),
            ).fetchone() is not None
            budget_facts = {
                str(row["fact_key"]): str(row["fact_value"])
                for row in connection.execute(
                    """SELECT fact_key, fact_value FROM probe_budget_facts
                        WHERE gate_id = ? ORDER BY fact_key""",
                    (self.gate_id,),
                )
            }
        return {
            "gate_id": self.gate_id,
            "max_requests_per_provider": self.max_requests_per_provider,
            "max_requests_total": self.max_requests_total,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd": self.max_cost_microusd / 1_000_000,
            "historical_reconciliation_required": self.require_historical_reconciliation,
            "historical_frozen_limits": {
                "max_requests_per_provider": HISTORICAL_MAX_REQUESTS_PER_PROVIDER,
                "max_requests_total": HISTORICAL_MAX_REQUESTS_TOTAL,
            },
            "historical_requests_by_provider": historical_totals["requests_by_provider"],
            "historical_total_requests": historical_totals["total_requests"],
            "historical_usage": budget_facts.get("historical_usage", "unknown"),
            "incremental_authorized": budget_facts.get("incremental_authorization")
            == P4A_GATE2_INCREMENTAL_CONFIRMATION,
            "incremental_requests_by_provider": incremental_totals["requests_by_provider"],
            "incremental_total_requests": incremental_totals["total_requests"],
            "prior_real_requests_by_provider": {
                provider_id: historical_totals["requests_by_provider"].get(provider_id, 0)
                + incremental_totals["requests_by_provider"].get(provider_id, 0)
                for provider_id in {"kimi", "deepseek"}
            },
            "prior_real_total_requests": historical_totals["total_requests"]
            + incremental_totals["total_requests"],
            "prior_real_usage": "unknown",
            "revision6_usage": {
                "input_tokens_unknown": incremental_totals["input_tokens_unknown"],
                "output_tokens_unknown": incremental_totals["output_tokens_unknown"],
                "cost_unknown": incremental_totals["cost_unknown"],
                "quota_unknown": incremental_totals["quota_unknown"],
            },
            "stage_a_authorized": budget_facts.get("r7_stage_a_authorization")
            == P4A_GATE2_R7_STAGE_A_CONFIRMATION,
            "stage_a_requests_by_provider": stage_a_totals["requests_by_provider"],
            "stage_a_total_requests": stage_a_totals["total_requests"],
            "requests_by_provider": totals["requests_by_provider"],
            "total_requests": totals["total_requests"],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "known_cost_usd": totals["known_cost_microusd"] / 1_000_000,
            "cost_unknown": totals["cost_unknown"],
            "input_tokens_unknown": totals["input_tokens_unknown"],
            "output_tokens_unknown": totals["output_tokens_unknown"],
            "quota_unknown": totals["quota_unknown"],
            "reservations_by_status": {
                str(row["status"]): int(row["count"]) for row in status_rows
            },
            "budget_facts": budget_facts,
            "cap_breached": cap_breached
            or totals["total_requests"] > self.max_requests_total
            or any(
                count > self.max_requests_per_provider
                for count in totals["requests_by_provider"].values()
            ),
        }

    def reconcile_existing_overage(self, *, actor: str, confirmation: str) -> dict[str, Any]:
        if actor != "codex" or confirmation != P4A_GATE2_RECONCILIATION_CONFIRMATION:
            raise GovernanceError("gate2_reconciliation_not_authorized")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._totals(connection)
            if existing["total_requests"] != 0:
                raise GovernanceError("gate2_reconciliation_requires_empty_ledger")
            for round_number in range(1, 4):
                for provider_id in ("kimi", "deepseek"):
                    for request_number in range(1, 4):
                        stable_id = f"historical-r{round_number}-{provider_id}-{request_number}"
                        connection.execute(
                            """INSERT INTO probe_reservations(
                                   reservation_id, gate_id, provider_id, request_id, status,
                                   request_sent, cost_unknown, source, created_at, updated_at
                               ) VALUES (?, ?, ?, ?, 'failed', 1, 1,
                                         'codex_historical_reconciliation', ?, ?)""",
                            (stable_id, self.gate_id, provider_id, stable_id, now, now),
                        )
            connection.execute(
                """INSERT INTO probe_budget_facts(gate_id, fact_key, fact_value, recorded_at)
                   VALUES (?, 'cap_breached', 'true', ?)""",
                (self.gate_id, now),
            )
            connection.execute(
                """INSERT INTO probe_budget_facts(
                       gate_id, fact_key, fact_value, recorded_at
                   ) VALUES (?, 'reconciliation_note', ?, ?)""",
                (
                    self.gate_id,
                    "three prior live rounds; kimi=9; deepseek=9; "
                    "total=18; all HTTP 400",
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO probe_budget_facts(gate_id, fact_key, fact_value, recorded_at)
                   VALUES (?, 'historical_usage', 'unknown', ?)""",
                (self.gate_id, now),
            )
        return self.public_dict()


def _usage_count(value: object) -> tuple[int, int]:
    if value is None:
        return 0, 1
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GovernanceError("gate2_invalid_usage")
    return value, 0


def build_canary_request(
    provider: Gate2Provider, kind: ProbeKind, request_id: str
) -> CanaryRequest:
    if provider.keychain_service not in FROZEN_KEYCHAIN_SERVICES:
        raise GovernanceError("gate2_keychain_service_not_frozen")
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "Process only the public synthetic P4A Gate 2 canary. Do not infer, fetch, "
                "or request any other data."
            ),
        }
    ]
    body: dict[str, Any] = {
        "model": provider.model_id,
        "temperature": 0,
        "max_tokens": 256,
    }
    if kind in {ProbeKind.STRUCTURED, ProbeKind.RECOVERY}:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Return only this JSON object with no markdown or additional text: "
                    f'{{"handshake":"{FROZEN_HANDSHAKE}","status":"ok"}}'
                ),
            }
        )
        if provider.provider_id == "deepseek":
            body["response_format"] = {"type": "json_object"}
    else:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Call synthetic_sum with left 17 and right 25. Do not perform other work."
                ),
            }
        )
        body["tools"] = [FROZEN_TOOL]
        body["tool_choice"] = {"type": "function", "function": {"name": "synthetic_sum"}}
    body["messages"] = messages
    return CanaryRequest(
        request_id=request_id,
        provider_id=provider.provider_id,
        endpoint=provider.endpoint.rstrip("/") + "/chat/completions",
        model=provider.model_id,
        kind=kind,
        privacy=PrivacyLevel.PUBLIC,
        body=body,
        timeout_seconds=20.0,
    )


def validate_frozen_request(request: CanaryRequest) -> None:
    serialized = json.dumps(request.body, sort_keys=True, separators=(",", ":"))
    if request.privacy is not PrivacyLevel.PUBLIC:
        raise GovernanceError("gate2_non_public_request")
    if "metadata" in request.body or "json_schema" in serialized:
        raise GovernanceError("gate2_outbound_field_drift")
    if request.kind in {ProbeKind.STRUCTURED, ProbeKind.RECOVERY}:
        response_format = request.body.get("response_format")
        if request.provider_id == "deepseek":
            if response_format != {"type": "json_object"}:
                raise GovernanceError("gate2_response_format_drift")
        elif response_format is not None:
            raise GovernanceError("gate2_response_format_drift")
        if FROZEN_HANDSHAKE not in serialized:
            raise GovernanceError("gate2_handshake_drift")
    else:
        if request.body.get("tools") != [FROZEN_TOOL]:
            raise GovernanceError("gate2_tool_drift")
        if "17" not in serialized or "25" not in serialized:
            raise GovernanceError("gate2_tool_arguments_drift")


def _parse_message(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise GovernanceError("gate2_malformed_choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise GovernanceError("gate2_malformed_message")
    content = message.get("content")
    return (content if isinstance(content, str) else "", message)


def parse_probe_response(request: CanaryRequest, attempt: TransportAttempt) -> dict[str, Any]:
    if attempt.response_json is None:
        raise GovernanceError("gate2_missing_response_json")
    if "application/json" not in attempt.content_type.lower():
        raise GovernanceError("gate2_bad_content_type")
    content, message = _parse_message(attempt.response_json)
    if request.kind in {ProbeKind.STRUCTURED, ProbeKind.RECOVERY}:
        try:
            structured = json.loads(content)
        except json.JSONDecodeError as error:
            raise GovernanceError("gate2_malformed_structured_output") from error
        if structured != {"handshake": FROZEN_HANDSHAKE, "status": "ok"}:
            raise GovernanceError("gate2_structured_output_mismatch")
        capability = {"structured_output": "ok", "tool_calling": "not_tested"}
    else:
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            raise GovernanceError("gate2_tool_call_missing")
        function = tool_calls[0].get("function") if isinstance(tool_calls[0], dict) else None
        if not isinstance(function, dict) or function.get("name") != "synthetic_sum":
            raise GovernanceError("gate2_tool_name_mismatch")
        arguments = function.get("arguments")
        try:
            parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError as error:
            raise GovernanceError("gate2_tool_arguments_malformed") from error
        if parsed_arguments != FROZEN_TOOL_ARGUMENTS:
            raise GovernanceError("gate2_tool_arguments_mismatch")
        capability = {"structured_output": "not_tested", "tool_calling": "ok"}
    actual_model = attempt.response_json.get("model")
    return {
        "actual_model": (
            actual_model if isinstance(actual_model, str) and actual_model else "unknown"
        ),
        "identity": "identity_unverified",
        "capability": capability,
        "usage": attempt.response_json.get("usage", {}),
    }


def validate_transport_success(request: CanaryRequest, attempt: TransportAttempt) -> None:
    requested = urllib.parse.urlsplit(request.endpoint)
    final = urllib.parse.urlsplit(attempt.final_endpoint)
    if requested.scheme != "https" or final.scheme != "https":
        raise GovernanceError("gate2_https_required")
    if not requested.hostname or final.hostname != requested.hostname:
        raise GovernanceError("gate2_final_host_mismatch")
    if attempt.tls_verified is not True:
        raise GovernanceError("gate2_tls_unverified")
    if not attempt.final_peer_ip:
        raise GovernanceError("gate2_peer_ip_missing")
    if attempt.first_byte_ms is None or attempt.total_ms is None:
        raise GovernanceError("gate2_latency_missing")
    if attempt.first_byte_ms < 0 or attempt.total_ms < attempt.first_byte_ms:
        raise GovernanceError("gate2_latency_invalid")


class Gate2CanaryRunner:
    def __init__(
        self,
        *,
        credential_reader: CredentialReader,
        transport: Callable[[CanaryRequest, str], TransportAttempt],
        budget: ProbeBudget,
        sleeper: Callable[[float], None] = time.sleep,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.credential_reader = credential_reader
        self.transport = transport
        self.budget = budget
        self.sleeper = sleeper
        self.id_factory = id_factory

    def run_pair(
        self, providers: tuple[Gate2Provider, Gate2Provider] = FROZEN_PROVIDERS
    ) -> dict[str, Any]:
        worker, evaluator = providers
        roles = select_role_pair(worker.model_config(), evaluator.model_config(), risk="high")
        if roles["status"] != "assigned" or worker.quota_pool == evaluator.quota_pool:
            raise GovernanceError("gate2_role_independence_missing")
        requests_by_provider = {
            provider.provider_id: [
                build_canary_request(provider, kind, self.id_factory())
                for kind in (ProbeKind.STRUCTURED, ProbeKind.TOOL, ProbeKind.RECOVERY)
            ]
            for provider in providers
        }
        probes_by_provider: dict[str, list[dict[str, Any]]] = {
            provider.provider_id: [] for provider in providers
        }
        credentials: dict[str, str] = {}
        failure_states = {
            provider.provider_id: _ProviderFailureState() for provider in providers
        }

        stage_a_prepared: dict[str, tuple[CanaryRequest, str, str]] = {}
        # Reserve both first probes before either transport can change usage facts.
        for provider in providers:
            request = requests_by_provider[provider.provider_id][0]
            try:
                self.budget.check_request_allowed(provider.provider_id)
                credential = self.credential_reader(provider.keychain_service)
                if not credential:
                    raise GovernanceError("gate2_credential_missing")
                credentials[provider.provider_id] = credential
                reservation_id = self.budget.reserve_request(
                    provider.provider_id, request.request_id
                )
                stage_a_prepared[provider.provider_id] = (
                    request,
                    credential,
                    reservation_id,
                )
            except BaseException as error:
                code = (
                    error.code if isinstance(error, GovernanceError) else "credential_read_failed"
                )
                probe = self._blocked_probe(request, code)
                probes_by_provider[provider.provider_id].append(probe)

        # Stage A always executes every successfully prepared provider.
        for provider in providers:
            prepared = stage_a_prepared.get(provider.provider_id)
            if prepared is None:
                continue
            request, credential, reservation_id = prepared
            probes_by_provider[provider.provider_id].append(
                self._run_probe(
                    request,
                    credential,
                    inject_disconnect=False,
                    failure_state=failure_states[provider.provider_id],
                    initial_reservation_id=reservation_id,
                )
            )

        stage_a_passed = all(
            probes_by_provider[provider.provider_id][0]["status"] == "passed"
            for provider in providers
        )
        for provider in providers:
            for request in requests_by_provider[provider.provider_id][1:]:
                if self.budget.stage_a_validation:
                    probe = self._blocked_probe(
                        request,
                        "gate2_stage_a_only_window",
                        fault_source="stage_a_window_guard",
                    )
                elif not stage_a_passed:
                    probe = self._blocked_probe(
                        request,
                        "gate2_stage_a_pair_blocked",
                        fault_source="pair_stage_guard",
                    )
                elif failure_states[provider.provider_id].blocked_reason is not None:
                    probe = self._blocked_probe(
                        request,
                        failure_states[provider.provider_id].blocked_reason
                        or "gate2_provider_blocked",
                        fault_source="provider_failure_guard",
                    )
                else:
                    probe = self._run_probe(
                        request,
                        credentials[provider.provider_id],
                        inject_disconnect=request.kind is ProbeKind.RECOVERY,
                        failure_state=failure_states[provider.provider_id],
                    )
                probes_by_provider[provider.provider_id].append(probe)
        results = [
            self._provider_result(provider, probes_by_provider[provider.provider_id])
            for provider in providers
        ]
        return {
            "status": (
                "candidate" if all(item["status"] == "passed" for item in results) else "blocked"
            ),
            "role_pair": roles,
            "quota_pools_distinct": True,
            "providers": results,
            "stage_a_passed": stage_a_passed,
            "budget": self.budget.public_dict(),
            "silent_fallback": False,
        }

    def run_provider(self, provider: Gate2Provider) -> dict[str, Any]:
        requests = [
            build_canary_request(provider, kind, self.id_factory())
            for kind in (ProbeKind.STRUCTURED, ProbeKind.TOOL, ProbeKind.RECOVERY)
        ]
        try:
            self.budget.check_request_allowed(provider.provider_id)
        except GovernanceError as error:
            probes = [self._blocked_probe(request, error.code) for request in requests]
            return self._provider_result(provider, probes)
        try:
            credential = self.credential_reader(provider.keychain_service)
            if not credential:
                raise GovernanceError("gate2_credential_missing")
        except BaseException as error:
            code = error.code if isinstance(error, GovernanceError) else "credential_read_failed"
            probes = [self._blocked_probe(request, code) for request in requests]
            return self._provider_result(provider, probes)
        probes = []
        failure_state = _ProviderFailureState()
        for request in requests:
            if self.budget.stage_a_validation and request.kind is not ProbeKind.STRUCTURED:
                probes.append(
                    self._blocked_probe(
                        request,
                        "gate2_stage_a_only_window",
                        fault_source="stage_a_window_guard",
                    )
                )
                continue
            if failure_state.blocked_reason is not None:
                probes.append(
                    self._blocked_probe(
                        request,
                        failure_state.blocked_reason,
                        fault_source="provider_failure_guard",
                    )
                )
                continue
            probes.append(
                self._run_probe(
                    request,
                    credential,
                    inject_disconnect=request.kind is ProbeKind.RECOVERY,
                    failure_state=failure_state,
                )
            )
        return self._provider_result(provider, probes)

    def _provider_result(
        self, provider: Gate2Provider, probes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        passed = all(probe["status"] == "passed" for probe in probes)
        return {
            "provider_id": provider.provider_id,
            "declared_model": provider.model_id,
            "model_family": provider.model_family,
            "quota_pool": provider.quota_pool,
            "role": provider.role,
            "status": "passed" if passed else "blocked",
            "probes": probes,
            "fallback_provider": None,
        }

    @staticmethod
    def _request_summary(request: CanaryRequest) -> dict[str, Any]:
        return {
            "method": "POST",
            "endpoint": request.endpoint,
            "model": request.model,
            "kind": request.kind.value,
            "request_id": request.request_id,
            "input_digest": request.input_digest,
            "timeout_seconds": request.timeout_seconds,
        }

    def _blocked_probe(
        self,
        request: CanaryRequest,
        error_code: str,
        *,
        fault_source: str = "credential_or_governance",
    ) -> dict[str, Any]:
        return {
            "status": "blocked",
            "kind": request.kind.value,
            "request_id": request.request_id,
            "input_digest": request.input_digest,
            "privacy": "public",
            "request_summary": self._request_summary(request),
            "attempts": [
                {
                    "attempt": 1,
                    "status": AttemptStatus.PERMANENT_ERROR.value,
                    "request_sent": False,
                    "http_status": None,
                    "error_code": error_code,
                    "provider_error_code": None,
                    "provider_error_type": None,
                    "provider_error_param": None,
                    "provider_error_category": None,
                    "fault_source": fault_source,
                }
            ],
            "retry_count": 0,
            "failure_reason": error_code,
            "fallback_provider": None,
        }

    def _run_probe(
        self,
        request: CanaryRequest,
        credential: str,
        *,
        inject_disconnect: bool,
        failure_state: _ProviderFailureState | None = None,
        initial_reservation_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            validate_frozen_request(request)
        except GovernanceError as error:
            return self._blocked_probe(request, error.code)
        attempts: list[dict[str, Any]] = []
        failure_state = failure_state or _ProviderFailureState()
        max_attempts = 1 if self.budget.stage_a_validation else 3
        for attempt_number in range(1, max_attempts + 1):
            reservation_id = initial_reservation_id if attempt_number == 1 else None
            if inject_disconnect and attempt_number == 1:
                attempt = TransportAttempt(
                    status=AttemptStatus.DISCONNECTED,
                    request_sent=False,
                    error_code="synthetic_pre_send_disconnect",
                )
                fault_source = "local_deterministic_injection"
            else:
                fault_source = "provider_transport"
                try:
                    if reservation_id is None:
                        reservation_id = self.budget.reserve_request(
                            request.provider_id, request.request_id
                        )
                    attempt = self.transport(request, credential)
                except GovernanceError as error:
                    attempt = TransportAttempt(
                        AttemptStatus.PERMANENT_ERROR,
                        False,
                        error_code=error.code,
                    )
                    fault_source = "governance"
                except Exception:
                    attempt = TransportAttempt(
                        AttemptStatus.PERMANENT_ERROR,
                        False,
                        error_code="provider_transport_failed",
                    )
                    fault_source = "provider_transport"
                if reservation_id is not None and attempt.status is not AttemptStatus.OK:
                    self.budget.finish_request(
                        reservation_id,
                        completed=False,
                        request_sent=attempt.request_sent,
                    )
            attempt_fact = {
                "attempt": attempt_number,
                "status": attempt.status.value,
                "request_sent": attempt.request_sent,
                "http_status": attempt.http_status,
                "error_code": attempt.error_code,
                "provider_error_code": attempt.provider_error_code,
                "provider_error_type": attempt.provider_error_type,
                "provider_error_param": attempt.provider_error_param,
                "provider_error_category": attempt.provider_error_category,
                "fault_source": fault_source,
            }
            attempts.append(attempt_fact)
            failure_state.observe(attempt.status, fault_source=fault_source)
            if attempt.status is AttemptStatus.OK:
                try:
                    validate_transport_success(request, attempt)
                    parsed = parse_probe_response(request, attempt)
                    usage = parsed.pop("usage")
                    if reservation_id is None:
                        raise GovernanceError("gate2_reservation_missing")
                    usage_facts = self.budget.finish_request(
                        reservation_id,
                        completed=True,
                        request_sent=attempt.request_sent,
                        usage=usage if isinstance(usage, dict) else {},
                    )
                    return {
                        "status": "passed",
                        "kind": request.kind.value,
                        "request_id": request.request_id,
                        "input_digest": request.input_digest,
                        "privacy": "public",
                        "request_summary": self._request_summary(request),
                        "attempts": attempts,
                        "retry_count": attempt_number - 1,
                        "recovery_without_duplicate_send": sum(
                            1 for item in attempts if item["request_sent"] is True
                        )
                        == 1,
                        "transport": {
                            "final_endpoint": attempt.final_endpoint,
                            "final_peer_ip": attempt.final_peer_ip,
                            "redirect_chain": attempt.redirect_chain,
                            "tls_verified": attempt.tls_verified,
                            "first_byte_ms": attempt.first_byte_ms,
                            "total_ms": attempt.total_ms,
                        },
                        "usage": usage_facts,
                        "limits": {
                            "context_limit": {
                                "value": "unknown",
                                "source": "not_returned_or_measured",
                            },
                            "output_limit": {
                                "value": request.body["max_tokens"],
                                "source": "canary_request_measured",
                            },
                        },
                        **parsed,
                    }
                except GovernanceError as error:
                    error_code = error.code
                    if reservation_id is not None:
                        try:
                            self.budget.finish_request(
                                reservation_id,
                                completed=False,
                                request_sent=attempt.request_sent,
                                usage=(
                                    attempt.response_json.get("usage", {})
                                    if isinstance(attempt.response_json, dict)
                                    else {}
                                ),
                            )
                        except GovernanceError as finish_error:
                            if finish_error.code != "gate2_reservation_not_open":
                                error_code = finish_error.code
                    attempt_fact["error_code"] = error_code
                    return {
                        "status": "blocked",
                        "kind": request.kind.value,
                        "request_id": request.request_id,
                        "input_digest": request.input_digest,
                        "privacy": "public",
                        "request_summary": self._request_summary(request),
                        "attempts": attempts,
                        "retry_count": attempt_number - 1,
                        "failure_reason": error_code,
                        "fallback_provider": None,
                    }
            retryable = not attempt.request_sent and attempt.status in {
                AttemptStatus.RATE_LIMITED,
                AttemptStatus.SERVER_ERROR,
                AttemptStatus.TIMEOUT,
                AttemptStatus.DISCONNECTED,
            }
            if (
                not retryable
                or failure_state.blocked_reason is not None
                or attempt_number == max_attempts
            ):
                break
            self.sleeper(0.1 * (2 ** (attempt_number - 1)))
        return {
            "status": "blocked",
            "kind": request.kind.value,
            "request_id": request.request_id,
            "input_digest": request.input_digest,
            "privacy": "public",
            "request_summary": self._request_summary(request),
            "attempts": attempts,
            "failure_reason": attempts[-1].get("error_code") or attempts[-1]["status"],
            "fallback_provider": None,
        }
