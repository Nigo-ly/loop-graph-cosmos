"""Offline P4A Gate 2 revision 9 dual-provider protocol candidate runner, r2.

Status: ``offline_implementation_candidate_r9`` (revision 2);
``live_authorized=false``; all four Stage A/B send counters are zero;
Gate 2 has not passed.

Provider-agnostic governance core plus two versioned frozen
``ProviderProfile`` records (GLM-4.7 = Worker, DeepSeek V4 Pro = Evaluator),
one minimal OpenAI Chat Completions ``ProviderAdapter`` and a frozen
``GatePlan``. Synthetic and live ledgers are strictly isolated by an
immutable ``ledger_kind``. This module never performs network, Keychain,
CC Switch, or real-path access in this candidate; live entries are
fail-closed before any side effect. No production transport exists in this
revision; it returns with a future live-enablement revision. The historical
Kimi 11 + DeepSeek 11 = 22 ledger with ``cap_breached=true`` and
``historical_usage=unknown`` is never read, modified, or reconciled here.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import ssl
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from system_governance.core import GovernanceError

CONTRACT_VERSION = "p4a-gate2-r9-revision-11-stage-b-ledger-compatibility-offline-candidate"
# Persistent protocol/ledger identity: the exact value already stored in the
# real Stage A ledger_meta and in every stored R9 evidence row. Never use the
# implementation version to reject or rewrite existing R9 ledgers.
PERSISTENT_CONTRACT_VERSION = "p4a-gate2-r9-revision-9-type-correction-offline-candidate"
EVIDENCE_FORMAT_LAYERED_V2 = "p4a-gate2-r9-stage-layered-v2"
LEDGER_IDENTITY = "p4a-gate2-r9-revision-9-independent"
LEDGER_SCHEMA_VERSION = 2
LEDGER_APPLICATION_ID = 0x52395F31

PROTOCOL_CONFIRMATION = (
    "NIGO_CONFIRM_P4A_GATE2_R9_PROTOCOL_GLM47_WORKER_DEEPSEEK_V4_PRO_EVALUATOR"
)
STAGE_A_AUTHORIZATION = "NIGO_AUTHORIZE_P4A_GATE2_R9_STAGE_A_PAIR_1_PER_PROVIDER_2_TOTAL"
STAGE_B_AUTHORIZATION = "NIGO_AUTHORIZE_P4A_GATE2_R9_STAGE_B_PAIR_1_PER_PROVIDER_2_TOTAL"
SYNTHETIC_STAGE_A_SIMULATION = "P4A_GATE2_R9_OFFLINE_SIMULATION_STAGE_A_PAIR"
SYNTHETIC_STAGE_B_SIMULATION = "P4A_GATE2_R9_OFFLINE_SIMULATION_STAGE_B_PAIR"

DEFAULT_LEDGER_PATH = Path("data/p4a_gate2_r9_probe_ledger.sqlite3")
DEFAULT_EVIDENCE_ROOT = Path("delivery/p4a-gate2-r9")
DEFAULT_LOCK_PATH = Path("delivery/.p4a-gate2-r9.lock")

MAX_SENDS_PER_PROVIDER = 2
MAX_SENDS_TOTAL = 4
MAX_INPUT_TOKENS = 8000
MAX_OUTPUT_TOKENS = 2048
MAX_KNOWN_COST_USD_MICROS = 1_000_000
MAX_SINGLE_COST_USD = 0.25
RESERVE_INPUT_TOKENS = 2000
RESERVE_OUTPUT_TOKENS = 512
RESERVE_COST_USD_MICROS = 250_000

CONNECT_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 60.0
MAX_TOTAL_LATENCY_MS = 60_000.0

STAGE_A_FIXTURE_ID = "p4a-gate2-r9-stage-a-fixture-1"
STAGE_A_MARKER = "P4A_GATE2_R9_STAGE_A_OK"
TOOL_NAME = "synthetic_add"
TOOL_ARGUMENTS: dict[str, Any] = {"a": 40, "b": 2}
STAGE_A_FINISH_REASONS = frozenset({"stop"})
STAGE_B_FINISH_REASONS = frozenset({"tool_calls"})

ADAPTER_TYPE = "openai_chat_completions"


class Stage(StrEnum):
    A = "stage_a"
    B = "stage_b"


class Role(StrEnum):
    WORKER = "worker"
    EVALUATOR = "evaluator"


class EvidenceOrigin(StrEnum):
    SYNTHETIC_ONLY = "synthetic_only"
    PRODUCTION_TRANSPORT = "production_transport"


class LedgerKind(StrEnum):
    SYNTHETIC = "synthetic"
    LIVE = "live"


def expected_origin(ledger_kind: LedgerKind) -> EvidenceOrigin:
    if ledger_kind is LedgerKind.SYNTHETIC:
        return EvidenceOrigin.SYNTHETIC_ONLY
    return EvidenceOrigin.PRODUCTION_TRANSPORT


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_STAGE_A_MESSAGES: list[dict[str, Any]] = [
    {
        "role": "system",
        "content": (
            "You are a deterministic offline acceptance probe for a public synthetic "
            "fixture. You only return JSON."
        ),
    },
    {
        "role": "user",
        "content": (
            "This public synthetic fixture has no business content. Return one JSON object "
            "with exactly these fields: fixture_id, status, marker. Use fixture_id "
            '"p4a-gate2-r9-stage-a-fixture-1", status "ok", marker "P4A_GATE2_R9_STAGE_A_OK".'
        ),
    },
]
_STAGE_B_MESSAGES: list[dict[str, Any]] = [
    {
        "role": "system",
        "content": (
            "You are a deterministic offline acceptance probe for a public synthetic "
            "fixture. You only call the declared tool."
        ),
    },
    {
        "role": "user",
        "content": (
            "This public synthetic fixture has no business content. Call the synthetic_add "
            "tool exactly once with a=40 and b=2. Do not answer in plain text."
        ),
    },
]
_TOOL_DECLARATION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Public synthetic side-effect-free addition fixture tool. "
                "It is never executed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "First synthetic integer."},
                    "b": {"type": "integer", "description": "Second synthetic integer."},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        },
    }
]

_GLM_STAGE_A_BODY: dict[str, Any] = {
    "model": "glm-4.7",
    "messages": _STAGE_A_MESSAGES,
    "thinking": {"type": "disabled"},
    "stream": False,
    "max_tokens": RESERVE_OUTPUT_TOKENS,
    "response_format": {"type": "json_object"},
}
_DEEPSEEK_STAGE_A_BODY: dict[str, Any] = {
    "model": "deepseek-v4-pro",
    "messages": _STAGE_A_MESSAGES,
    "thinking": {"type": "disabled"},
    "stream": False,
    "max_tokens": RESERVE_OUTPUT_TOKENS,
    "response_format": {"type": "json_object"},
}
_GLM_STAGE_B_BODY: dict[str, Any] = {
    "model": "glm-4.7",
    "messages": _STAGE_B_MESSAGES,
    "thinking": {"type": "disabled"},
    "stream": False,
    "max_tokens": RESERVE_OUTPUT_TOKENS,
    "tools": _TOOL_DECLARATION,
    "tool_choice": "auto",
}
_DEEPSEEK_STAGE_B_BODY: dict[str, Any] = {
    "model": "deepseek-v4-pro",
    "messages": _STAGE_B_MESSAGES,
    "thinking": {"type": "disabled"},
    "stream": False,
    "max_tokens": RESERVE_OUTPUT_TOKENS,
    "tools": _TOOL_DECLARATION,
    "tool_choice": "auto",
}

_STAGE_A_EXPECTED_OUTPUT: dict[str, Any] = {
    "fixture_id": STAGE_A_FIXTURE_ID,
    "status": "ok",
    "marker": STAGE_A_MARKER,
}


@dataclass(frozen=True)
class FrozenRequest:
    """One hand-written literal request body frozen by the protocol."""

    stage: Stage
    provider_id: str
    fixture_id: str
    body: Mapping[str, Any]
    sha256: str
    utf8_bytes: int

    @property
    def canonical_body(self) -> str:
        return canonical_json(self.body)


# Frozen (utf8_bytes, sha256) pairs copied verbatim from the protocol.
_FROZEN_DIGESTS: dict[tuple[str, Stage], tuple[int, str]] = {
    ("glm47", Stage.A): (
        532,
        "07c4906c75c00df28cef1b400dc48c4ab18e0c6b25058eb07514bc95c8009f89",
    ),
    ("deepseek_v4_pro", Stage.A): (
        540,
        "baff47bacea9124fac18976cddb634434e1f9a7767f5c6e0cff0015db91213b5",
    ),
    ("glm47", Stage.B): (
        818,
        "07bea198c8f09ed4bf6412c26dd6dff87d501cdf967b3d711c7af075f22c5ffe",
    ),
    ("deepseek_v4_pro", Stage.B): (
        826,
        "018229da696430a1626b60909ddcf70a5fe67533d52b1812bd6e64e25d108f02",
    ),
}

_FROZEN_BODIES: dict[tuple[str, Stage], Mapping[str, Any]] = {
    ("glm47", Stage.A): _GLM_STAGE_A_BODY,
    ("deepseek_v4_pro", Stage.A): _DEEPSEEK_STAGE_A_BODY,
    ("glm47", Stage.B): _GLM_STAGE_B_BODY,
    ("deepseek_v4_pro", Stage.B): _DEEPSEEK_STAGE_B_BODY,
}


def frozen_request(provider_id: str, stage: Stage) -> FrozenRequest:
    key = (provider_id, stage)
    if key not in _FROZEN_BODIES:
        raise GovernanceError("gate2_r9_profile_not_registered")
    utf8_bytes, sha256 = _FROZEN_DIGESTS[key]
    fixture_id = STAGE_A_FIXTURE_ID if stage is Stage.A else "p4a-gate2-r9-stage-b-fixture-1"
    return FrozenRequest(
        stage=stage,
        provider_id=provider_id,
        fixture_id=fixture_id,
        body=_FROZEN_BODIES[key],
        sha256=sha256,
        utf8_bytes=utf8_bytes,
    )


def _walk_json(value: object) -> list[object]:
    items = [value]
    if isinstance(value, Mapping):
        for key, item in value.items():
            items.extend(_walk_json(key))
            items.extend(_walk_json(item))
    elif isinstance(value, list):
        for item in value:
            items.extend(_walk_json(item))
    return items


def validate_frozen_request(request: FrozenRequest) -> None:
    """Fail closed on any drift before reservation, credential read, or transport."""
    key = (request.provider_id, request.stage)
    if key not in _FROZEN_DIGESTS:
        raise GovernanceError("gate2_r9_request_drift")
    utf8_bytes, sha256 = _FROZEN_DIGESTS[key]
    canonical = request.canonical_body
    encoded = canonical.encode("utf-8")
    if len(encoded) != utf8_bytes or hashlib.sha256(encoded).hexdigest() != sha256:
        raise GovernanceError("gate2_r9_request_drift")
    if len(encoded) != request.utf8_bytes or request.sha256 != sha256:
        raise GovernanceError("gate2_r9_request_drift")
    for item in _walk_json(request.body):
        if item in ("metadata", "json_schema"):
            raise GovernanceError("gate2_r9_forbidden_request_field")
    if request.body.get("thinking") != {"type": "disabled"}:
        raise GovernanceError("gate2_r9_request_drift")
    if request.body.get("stream") is not False:
        raise GovernanceError("gate2_r9_request_drift")
    if request.body.get("max_tokens") != RESERVE_OUTPUT_TOKENS:
        raise GovernanceError("gate2_r9_request_drift")
    profile = _registered_by_id().get(request.provider_id)
    if profile is None or request.body.get("model") != profile.model_id:
        raise GovernanceError("gate2_r9_request_drift")


@dataclass(frozen=True)
class ProviderProfile:
    """Versioned frozen provider profile; the only place vendor facts live."""

    provider_id: str
    profile_version: str
    model_family: str
    role: Role
    official_host: str
    endpoint: str
    model_id: str
    keychain_service: str
    adapter_type: str
    thinking_disabled: bool
    structured_output_format: Mapping[str, Any]
    tool_choice: str
    usage_prompt_field: str
    usage_completion_field: str
    usage_total_field: str

    def request(self, stage: Stage) -> FrozenRequest:
        return frozen_request(self.provider_id, stage)


def glm47_profile() -> ProviderProfile:
    return ProviderProfile(
        provider_id="glm47",
        profile_version="p4a-gate2-r9-glm47-v1",
        model_family="glm-4.7-family",
        role=Role.WORKER,
        official_host="open.bigmodel.cn",
        endpoint="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        model_id="glm-4.7",
        keychain_service="p4a-gate2-r9-provider-glm47",
        adapter_type=ADAPTER_TYPE,
        thinking_disabled=True,
        structured_output_format={"type": "json_object"},
        tool_choice="auto",
        usage_prompt_field="prompt_tokens",
        usage_completion_field="completion_tokens",
        usage_total_field="total_tokens",
    )


def deepseek_v4_pro_profile() -> ProviderProfile:
    return ProviderProfile(
        provider_id="deepseek_v4_pro",
        profile_version="p4a-gate2-r9-deepseek-v4-pro-v1",
        model_family="deepseek-v4-pro-family",
        role=Role.EVALUATOR,
        official_host="api.deepseek.com",
        endpoint="https://api.deepseek.com/v1/chat/completions",
        model_id="deepseek-v4-pro",
        keychain_service="p4a-gate2-r9-provider-deepseek",
        adapter_type=ADAPTER_TYPE,
        thinking_disabled=True,
        structured_output_format={"type": "json_object"},
        tool_choice="auto",
        usage_prompt_field="prompt_tokens",
        usage_completion_field="completion_tokens",
        usage_total_field="total_tokens",
    )


def registered_profiles() -> tuple[ProviderProfile, ProviderProfile]:
    return (glm47_profile(), deepseek_v4_pro_profile())


def _registered_by_id() -> dict[str, ProviderProfile]:
    return {profile.provider_id: profile for profile in registered_profiles()}


@dataclass(frozen=True)
class TransportResult:
    """Transient transport facts; never persisted raw."""

    request_sent: bool | None
    http_status: int | None
    response_json: Mapping[str, Any] | None = None
    evidence_origin: EvidenceOrigin = EvidenceOrigin.PRODUCTION_TRANSPORT
    time_to_headers_ms: float | None = None
    total_latency_ms: float | None = None
    connect_timeout_s: float | None = None
    total_timeout_s: float | None = None
    final_host: str | None = None
    tls_hostname_verified: bool | None = None
    provider_cost_usd: float | None = None
    provider_quota_fact: str | None = None
    local_error_category: str | None = None


class ProviderAdapter(Protocol):
    """Vendor-protocol differences only; no governance logic here."""

    def build_request(self, profile: ProviderProfile, stage: Stage) -> FrozenRequest: ...

    def parse_response_model(self, response: Mapping[str, Any]) -> object: ...

    def parse_structured_content(self, response: Mapping[str, Any]) -> object: ...

    def parse_tool_calls(self, response: Mapping[str, Any]) -> object: ...

    def parse_usage(self, response: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def parse_finish_reason(self, response: Mapping[str, Any]) -> object: ...

    def classify_error(self, http_status: int | None) -> str: ...


class OpenAIChatCompletionsAdapter:
    """Minimal adapter shared by both frozen providers."""

    def build_request(self, profile: ProviderProfile, stage: Stage) -> FrozenRequest:
        if profile.adapter_type != ADAPTER_TYPE:
            raise GovernanceError("gate2_r9_adapter_unknown")
        request = profile.request(stage)
        validate_frozen_request(request)
        return request

    def parse_response_model(self, response: Mapping[str, Any]) -> object:
        return response.get("model")

    def parse_structured_content(self, response: Mapping[str, Any]) -> object:
        message = _first_message(response)
        if message is None:
            return None
        content = message.get("content")
        if not isinstance(content, str):
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None

    def parse_tool_calls(self, response: Mapping[str, Any]) -> object:
        """Return the raw tool_calls value; all validation lives in the evaluator."""
        message = _first_message(response)
        if message is None:
            return None
        return message.get("tool_calls")

    def parse_usage(self, response: Mapping[str, Any]) -> Mapping[str, Any]:
        usage = response.get("usage")
        return usage if isinstance(usage, Mapping) else {}

    def parse_finish_reason(self, response: Mapping[str, Any]) -> object:
        choices = response.get("choices")
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping):
            return choices[0].get("finish_reason")
        return None

    def classify_error(self, http_status: int | None) -> str:
        if http_status is None:
            return "transport_failure"
        if http_status in {301, 302, 303, 307, 308}:
            return "redirect_rejected"
        if http_status == 200:
            return "none"
        return "http_failure"


def _first_message(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = response.get("choices")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        if isinstance(message, Mapping):
            return message
    return None


def adapter_for(profile: ProviderProfile) -> ProviderAdapter:
    if profile.adapter_type != ADAPTER_TYPE:
        raise GovernanceError("gate2_r9_adapter_unknown")
    return OpenAIChatCompletionsAdapter()


@dataclass(frozen=True)
class GatePlan:
    """Frozen plan: profiles, roles, order, phrases, budgets, blocking rules."""

    worker: ProviderProfile
    evaluator: ProviderProfile
    stage_a_authorization: str
    stage_b_authorization: str
    max_sends_per_provider: int
    max_sends_total: int
    max_input_tokens: int
    max_output_tokens: int
    max_known_cost_usd_micros: int

    def stage_order(self, stage: Stage) -> tuple[ProviderProfile, ProviderProfile]:
        return (self.worker, self.evaluator)

    def authorization_for(self, stage: Stage) -> str:
        return self.stage_a_authorization if stage is Stage.A else self.stage_b_authorization


def default_gate_plan() -> GatePlan:
    return GatePlan(
        worker=glm47_profile(),
        evaluator=deepseek_v4_pro_profile(),
        stage_a_authorization=STAGE_A_AUTHORIZATION,
        stage_b_authorization=STAGE_B_AUTHORIZATION,
        max_sends_per_provider=MAX_SENDS_PER_PROVIDER,
        max_sends_total=MAX_SENDS_TOTAL,
        max_input_tokens=MAX_INPUT_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_known_cost_usd_micros=MAX_KNOWN_COST_USD_MICROS,
    )


def validate_gate_plan(plan: GatePlan) -> None:
    registered = _registered_by_id()
    for profile in (plan.worker, plan.evaluator):
        if registered.get(profile.provider_id) != profile:
            raise GovernanceError("gate2_r9_profile_not_registered")
    if plan.worker.role is not Role.WORKER or plan.evaluator.role is not Role.EVALUATOR:
        raise GovernanceError("gate2_r9_role_mapping_invalid")
    distinct = {
        plan.worker.provider_id,
        plan.evaluator.provider_id,
    }
    if len(distinct) != 2:
        raise GovernanceError("gate2_r9_role_mapping_invalid")
    for field in ("model_family", "official_host", "keychain_service", "endpoint"):
        if getattr(plan.worker, field) == getattr(plan.evaluator, field):
            raise GovernanceError("gate2_r9_providers_not_independent")
    if plan.stage_a_authorization != STAGE_A_AUTHORIZATION:
        raise GovernanceError("gate2_r9_authorization_phrase_drift")
    if plan.stage_b_authorization != STAGE_B_AUTHORIZATION:
        raise GovernanceError("gate2_r9_authorization_phrase_drift")
    expected_budgets = (
        MAX_SENDS_PER_PROVIDER,
        MAX_SENDS_TOTAL,
        MAX_INPUT_TOKENS,
        MAX_OUTPUT_TOKENS,
        MAX_KNOWN_COST_USD_MICROS,
    )
    actual_budgets = (
        plan.max_sends_per_provider,
        plan.max_sends_total,
        plan.max_input_tokens,
        plan.max_output_tokens,
        plan.max_known_cost_usd_micros,
    )
    if actual_budgets != expected_budgets:
        raise GovernanceError("gate2_r9_budget_drift")


_RESERVATION_ID = re.compile(r"r9-[0-9a-f]{32}\Z")
_SENSITIVE_ID = re.compile(r"(?:sk-|bearer|authorization|api[_-]?key)", re.IGNORECASE)


def _validate_reservation_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or _RESERVATION_ID.fullmatch(value) is None
        or _SENSITIVE_ID.search(value) is not None
    ):
        raise GovernanceError("gate2_r9_reservation_id_invalid")


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


def _valid_latency(value: object) -> float | None:
    if value is None:
        return None
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
        raise GovernanceError("gate2_r9_evidence_schema_invalid")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as error:
        raise GovernanceError("gate2_r9_evidence_schema_invalid") from error
    if observed.utcoffset() is None:
        raise GovernanceError("gate2_r9_evidence_schema_invalid")


_LOCAL_ERRORS = frozenset(
    {
        "none",
        "transport_exception",
        "transport_failure",
        "redirect_rejected",
        "http_failure",
        "response_not_object",
        "response_decode_failure",
        "response_too_large",
        "response_read_failure",
    }
)


def _route_facts_consistent(
    request_sent: object, http_status: object, local_error: object
) -> bool:
    """Single route-facts relation shared by transport input validation and
    stored-evidence derivation. ``http_status=None`` means unknown/absent."""
    if local_error == "none":
        return request_sent is True and http_status == 200
    if local_error in {"transport_exception", "transport_failure"}:
        return http_status is None
    if local_error == "redirect_rejected":
        return request_sent is True and http_status in {301, 302, 303, 307, 308}
    if local_error == "http_failure":
        return (
            request_sent is True
            and isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and http_status != 200
            and http_status not in {301, 302, 303, 307, 308}
        )
    if local_error in {"response_not_object", "response_decode_failure"}:
        return request_sent is True and http_status == 200
    if local_error in {"response_too_large", "response_read_failure"}:
        return (
            request_sent is True
            and isinstance(http_status, int)
            and not isinstance(http_status, bool)
        )
    return False


def _validate_transport_result(result: object) -> None:
    if not isinstance(result, TransportResult):
        raise GovernanceError("gate2_r9_transport_facts_invalid")
    if result.evidence_origin not in {
        EvidenceOrigin.SYNTHETIC_ONLY,
        EvidenceOrigin.PRODUCTION_TRANSPORT,
    }:
        raise GovernanceError("gate2_r9_transport_facts_invalid")
    if result.request_sent is not None and not isinstance(result.request_sent, bool):
        raise GovernanceError("gate2_r9_transport_facts_invalid")
    if result.http_status is not None and (
        not isinstance(result.http_status, int)
        or isinstance(result.http_status, bool)
        or not 100 <= result.http_status <= 599
    ):
        raise GovernanceError("gate2_r9_transport_facts_invalid")
    if result.response_json is not None and not isinstance(result.response_json, Mapping):
        raise GovernanceError("gate2_r9_transport_facts_invalid")
    local_error = result.local_error_category or "none"
    if local_error not in _LOCAL_ERRORS:
        raise GovernanceError("gate2_r9_transport_facts_invalid")
    if not _route_facts_consistent(
        result.request_sent, result.http_status, local_error
    ):
        raise GovernanceError("gate2_r9_route_facts_inconsistent")
    response_present = result.response_json is not None
    if local_error == "none":
        if not response_present:
            raise GovernanceError("gate2_r9_route_facts_inconsistent")
    elif response_present:
        raise GovernanceError("gate2_r9_route_facts_inconsistent")


_SCHEMA = """
CREATE TABLE ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL CHECK(stage IN ('stage_a', 'stage_b')),
    provider_id TEXT NOT NULL CHECK(provider_id IN ('glm47', 'deepseek_v4_pro')),
    request_sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    request_body_bytes INTEGER NOT NULL CHECK(request_body_bytes > 0),
    reserved_input_tokens INTEGER NOT NULL CHECK(reserved_input_tokens = 2000),
    reserved_output_tokens INTEGER NOT NULL CHECK(reserved_output_tokens = 512),
    reserved_cost_usd_micros INTEGER NOT NULL CHECK(reserved_cost_usd_micros = 250000),
    status TEXT NOT NULL CHECK(status = 'consumed'),
    reserved_at TEXT NOT NULL,
    UNIQUE(stage, provider_id)
);
CREATE TABLE outcomes (
    reservation_id TEXT PRIMARY KEY REFERENCES reservations(reservation_id),
    request_sent INTEGER CHECK(request_sent IN (0, 1) OR request_sent IS NULL),
    known_cost_usd_micros INTEGER CHECK(
        known_cost_usd_micros >= 0 OR known_cost_usd_micros IS NULL
    ),
    evidence_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE stage_results (
    stage TEXT NOT NULL CHECK(stage IN ('stage_a', 'stage_b')),
    provider_id TEXT NOT NULL CHECK(provider_id IN ('glm47', 'deepseek_v4_pro')),
    verdict TEXT NOT NULL CHECK(verdict IN ('pass', 'fail')),
    evidence_origin TEXT NOT NULL CHECK(
        evidence_origin IN ('synthetic_only', 'production_transport')
    ),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (stage, provider_id)
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
        ("provider_id", "TEXT", 1, 0),
        ("request_sha256", "TEXT", 1, 0),
        ("summary_json", "TEXT", 1, 0),
        ("request_body_bytes", "INTEGER", 1, 0),
        ("reserved_input_tokens", "INTEGER", 1, 0),
        ("reserved_output_tokens", "INTEGER", 1, 0),
        ("reserved_cost_usd_micros", "INTEGER", 1, 0),
        ("status", "TEXT", 1, 0),
        ("reserved_at", "TEXT", 1, 0),
    ),
    "outcomes": (
        ("reservation_id", "TEXT", 0, 1),
        ("request_sent", "INTEGER", 0, 0),
        ("known_cost_usd_micros", "INTEGER", 0, 0),
        ("evidence_json", "TEXT", 1, 0),
        ("recorded_at", "TEXT", 1, 0),
    ),
    "stage_results": (
        ("stage", "TEXT", 1, 1),
        ("provider_id", "TEXT", 1, 2),
        ("verdict", "TEXT", 1, 0),
        ("evidence_origin", "TEXT", 1, 0),
        ("reservation_id", "TEXT", 1, 0),
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


def normalized_request_summary(profile: ProviderProfile, request: FrozenRequest) -> dict[str, Any]:
    return {
        "method": "POST",
        "official_host": profile.official_host,
        "endpoint_path": urlsplit(profile.endpoint).path,
        "fixture_id": request.fixture_id,
        "model": profile.model_id,
        "role": profile.role.value,
        "stage": request.stage.value,
        "request_sha256": request.sha256,
        "request_utf8_bytes": request.utf8_bytes,
        "thinking_disabled": True,
        "output_token_limit": RESERVE_OUTPUT_TOKENS,
        "tool_declaration_count": 0 if request.stage is Stage.A else 1,
    }


class Gate2R9Ledger:
    """Independent persistent R9 ledger with kind, identity, and budget checks."""

    def __init__(
        self,
        path: Path,
        *,
        ledger_kind: LedgerKind,
        id_factory: Callable[[], str] = lambda: f"r9-{uuid.uuid4().hex}",
    ) -> None:
        if ledger_kind not in {LedgerKind.SYNTHETIC, LedgerKind.LIVE}:
            raise GovernanceError("gate2_r9_ledger_kind_invalid")
        self.path = Path(path)
        self.ledger_kind = ledger_kind
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
                raise GovernanceError("gate2_r9_ledger_symlink_rejected")
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISREG(mode):
            raise GovernanceError("gate2_r9_ledger_not_regular_file")

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
                    raise GovernanceError("gate2_r9_ledger_identity_mismatch")
                if connection.execute("PRAGMA user_version").fetchone()[0] != (
                    LEDGER_SCHEMA_VERSION
                ):
                    raise GovernanceError("gate2_r9_ledger_schema_mismatch")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    )
                }
                if tables != set(_EXPECTED_COLUMNS):
                    raise GovernanceError("gate2_r9_ledger_schema_mismatch")
                schema_objects = {
                    row[0]: _normalized_sql(row[1])
                    for row in connection.execute(
                        "SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL"
                    )
                }
                if schema_objects != _EXPECTED_TABLE_SQL:
                    raise GovernanceError("gate2_r9_ledger_schema_mismatch")
                for table, expected in _EXPECTED_COLUMNS.items():
                    columns = tuple(
                        (row[1], row[2], row[3], row[5])
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    )
                    if columns != expected:
                        raise GovernanceError("gate2_r9_ledger_schema_mismatch")
                meta = dict(connection.execute("SELECT key, value FROM ledger_meta"))
                if meta != {
                    "contract_version": PERSISTENT_CONTRACT_VERSION,
                    "ledger_identity": LEDGER_IDENTITY,
                    "schema_version": str(LEDGER_SCHEMA_VERSION),
                    "ledger_kind": self.ledger_kind.value,
                }:
                    if meta.get("ledger_kind") in {
                        LedgerKind.SYNTHETIC.value,
                        LedgerKind.LIVE.value,
                    } and meta.get("ledger_kind") != self.ledger_kind.value:
                        raise GovernanceError("gate2_r9_ledger_kind_mismatch")
                    raise GovernanceError("gate2_r9_ledger_identity_mismatch")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise GovernanceError("gate2_r9_ledger_integrity_failed")
        except GovernanceError:
            raise
        except sqlite3.Error as error:
            raise GovernanceError("gate2_r9_ledger_identity_mismatch") from error

    def _initialize_candidate(self, candidate: Path) -> None:
        with sqlite3.connect(candidate) as connection:
            connection.execute(f"PRAGMA application_id={LEDGER_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={LEDGER_SCHEMA_VERSION}")
            connection.executescript(_SCHEMA)
            connection.executemany(
                "INSERT INTO ledger_meta(key, value) VALUES (?, ?)",
                (
                    ("contract_version", PERSISTENT_CONTRACT_VERSION),
                    ("ledger_identity", LEDGER_IDENTITY),
                    ("schema_version", str(LEDGER_SCHEMA_VERSION)),
                    ("ledger_kind", self.ledger_kind.value),
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
            raise GovernanceError("gate2_r9_ledger_parent_missing")
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

    def _new_reservation_id(self) -> str:
        reservation_id = self._id_factory()
        _validate_reservation_id(reservation_id)
        return reservation_id

    @staticmethod
    def _require_order(
        plan: GatePlan,
        profile: ProviderProfile,
        stage: Stage,
        passed: frozenset[tuple[str, str]],
    ) -> None:
        """Order checks consume only fully cross-validated evidence pass facts."""
        if stage is Stage.A and profile.provider_id == plan.evaluator.provider_id:
            if (Stage.A.value, plan.worker.provider_id) not in passed:
                raise GovernanceError("gate2_r9_stage_a_worker_not_passed")
        if stage is Stage.B:
            pair_passed = (Stage.A.value, plan.worker.provider_id) in passed and (
                Stage.A.value,
                plan.evaluator.provider_id,
            ) in passed
            if not pair_passed:
                raise GovernanceError("gate2_r9_stage_a_pair_not_passed")
            if profile.provider_id == plan.evaluator.provider_id and (
                Stage.B.value,
                plan.worker.provider_id,
            ) not in passed:
                raise GovernanceError("gate2_r9_stage_b_worker_not_passed")

    def reserve(
        self,
        plan: GatePlan,
        profile: ProviderProfile,
        stage: Stage,
        request: FrozenRequest,
    ) -> str:
        validate_gate_plan(plan)
        validate_frozen_request(request)
        if profile.provider_id != request.provider_id or profile.request(stage) != request:
            raise GovernanceError("gate2_r9_request_drift")
        reservation_id = self._new_reservation_id()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                passed = self._validate_semantic_state(connection)
                self._require_order(plan, profile, stage, passed)
                totals = connection.execute(
                    """SELECT COUNT(*),
                              COALESCE(SUM(reserved_input_tokens), 0),
                              COALESCE(SUM(reserved_output_tokens), 0),
                              COALESCE(SUM(reserved_cost_usd_micros), 0)
                         FROM reservations"""
                ).fetchone()
                provider_count = connection.execute(
                    "SELECT COUNT(*) FROM reservations WHERE provider_id = ?",
                    (profile.provider_id,),
                ).fetchone()[0]
                if (
                    provider_count >= plan.max_sends_per_provider
                    or totals[0] >= plan.max_sends_total
                    or totals[1] + RESERVE_INPUT_TOKENS > plan.max_input_tokens
                    or totals[2] + RESERVE_OUTPUT_TOKENS > plan.max_output_tokens
                    or totals[3] + RESERVE_COST_USD_MICROS > plan.max_known_cost_usd_micros
                ):
                    raise GovernanceError("gate2_r9_send_cap_reached")
                connection.execute(
                    """INSERT INTO reservations(
                           reservation_id, stage, provider_id, request_sha256,
                           summary_json, request_body_bytes, reserved_input_tokens,
                           reserved_output_tokens, reserved_cost_usd_micros,
                           status, reserved_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'consumed', ?)""",
                    (
                        reservation_id,
                        stage.value,
                        profile.provider_id,
                        request.sha256,
                        canonical_json(normalized_request_summary(profile, request)),
                        request.utf8_bytes,
                        RESERVE_INPUT_TOKENS,
                        RESERVE_OUTPUT_TOKENS,
                        RESERVE_COST_USD_MICROS,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise GovernanceError("gate2_r9_send_cap_reached") from error
        return reservation_id

    def record_outcome(
        self,
        plan: GatePlan,
        profile: ProviderProfile,
        stage: Stage,
        reservation_id: str,
        request: FrozenRequest,
        result: TransportResult,
        *,
        observed_at: str,
    ) -> dict[str, Any]:
        _validate_reservation_id(reservation_id)
        evidence = evaluate_result(
            plan, profile, stage, request, reservation_id, result, observed_at=observed_at
        )
        encoded = canonical_json(evidence)
        known_cost = _valid_cost(result.provider_cost_usd)
        known_cost_micros = None if known_cost is None else round(known_cost * 1_000_000)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_semantic_state(connection)
            row = connection.execute(
                """SELECT stage, provider_id, request_sha256, summary_json,
                          request_body_bytes, reserved_input_tokens,
                          reserved_output_tokens, reserved_cost_usd_micros
                     FROM reservations WHERE reservation_id = ?""",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise GovernanceError("gate2_r9_reservation_missing")
            bound = (
                row["stage"],
                row["provider_id"],
                row["request_sha256"],
                row["summary_json"],
                row["request_body_bytes"],
                row["reserved_input_tokens"],
                row["reserved_output_tokens"],
                row["reserved_cost_usd_micros"],
            )
            expected = (
                stage.value,
                profile.provider_id,
                request.sha256,
                canonical_json(normalized_request_summary(profile, request)),
                request.utf8_bytes,
                RESERVE_INPUT_TOKENS,
                RESERVE_OUTPUT_TOKENS,
                RESERVE_COST_USD_MICROS,
            )
            if bound != expected:
                raise GovernanceError("gate2_r9_outcome_reservation_mismatch")
            if result.evidence_origin is not expected_origin(self.ledger_kind):
                raise GovernanceError("gate2_r9_outcome_reservation_mismatch")
            if known_cost_micros is not None:
                cumulative = connection.execute(
                    "SELECT COALESCE(SUM(known_cost_usd_micros), 0) FROM outcomes"
                ).fetchone()[0]
                if cumulative + known_cost_micros > plan.max_known_cost_usd_micros:
                    raise GovernanceError("gate2_r9_known_cost_cap_reached")
            try:
                connection.execute(
                    """INSERT INTO outcomes(
                           reservation_id, request_sent, known_cost_usd_micros,
                           evidence_json, recorded_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        reservation_id,
                        None if result.request_sent is None else int(result.request_sent),
                        known_cost_micros,
                        encoded,
                        self._now(),
                    ),
                )
                connection.execute(
                    """INSERT INTO stage_results(
                           stage, provider_id, verdict, evidence_origin,
                           reservation_id, recorded_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        stage.value,
                        profile.provider_id,
                        evidence["stage_verdict"],
                        result.evidence_origin.value,
                        reservation_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise GovernanceError("gate2_r9_outcome_already_recorded") from error
        return evidence

    def public_state(self) -> dict[str, Any]:
        self._reject_symlink_components()
        if not self.path.exists():
            raise GovernanceError("gate2_r9_ledger_missing")
        self._validate_existing()
        with self._readonly_connect() as connection:
            reservations, outcomes, results, _ = self._query_validated_state(connection)
        ordered = sorted(
            reservations.values(),
            key=lambda record: (record["reserved_at"], record["reservation_id"]),
        )
        return {
            "ledger_identity": LEDGER_IDENTITY,
            "contract_version": PERSISTENT_CONTRACT_VERSION,
            "ledger_kind": self.ledger_kind.value,
            "limits": {
                "max_sends_per_provider": MAX_SENDS_PER_PROVIDER,
                "max_sends_total": MAX_SENDS_TOTAL,
                "max_input_tokens": MAX_INPUT_TOKENS,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "max_known_cost_usd_micros": MAX_KNOWN_COST_USD_MICROS,
            },
            "stage_results": results,
            "reservations": [
                {**record, "outcome": outcomes.get(record["reservation_id"])}
                for record in ordered
            ],
        }

    def _query_validated_state(
        self, connection: sqlite3.Connection
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        frozenset[tuple[str, str]],
    ]:
        return _validate_full_state(
            _rows_to_dicts(connection.execute("SELECT * FROM reservations").fetchall()),
            _rows_to_dicts(connection.execute("SELECT * FROM outcomes").fetchall()),
            _rows_to_dicts(connection.execute("SELECT * FROM stage_results").fetchall()),
            self.ledger_kind,
        )

    def _validate_semantic_state(
        self, connection: sqlite3.Connection
    ) -> frozenset[tuple[str, str]]:
        """Full in-transaction cross-validation; fail-closed on any tamper.

        Runs before any order or budget judgment and before any write. Only
        fully cross-validated evidence pass facts are returned and may be used
        for order decisions.
        """
        _, _, _, passed = self._query_validated_state(connection)
        return passed

    def validated_state(
        self,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        frozenset[tuple[str, str]],
    ]:
        """Read-only fully validated state for restart and evidence rebuild."""
        self._reject_symlink_components()
        if not self.path.exists():
            raise GovernanceError("gate2_r9_ledger_missing")
        self._validate_existing()
        with self._readonly_connect() as connection:
            return self._query_validated_state(connection)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def _strict_reservation_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Rebind a stored reservation to the frozen profile and frozen request."""
    reservation_id = record.get("reservation_id")
    try:
        _validate_reservation_id(reservation_id)
    except GovernanceError as error:
        raise GovernanceError("gate2_r9_ledger_tampered") from error
    provider_id = record.get("provider_id")
    stage_value = record.get("stage")
    profile = (
        _registered_by_id().get(provider_id) if isinstance(provider_id, str) else None
    )
    if profile is None or stage_value not in {Stage.A.value, Stage.B.value}:
        raise GovernanceError("gate2_r9_ledger_tampered")
    stage = Stage(stage_value)
    frozen = profile.request(stage)
    if (
        record.get("request_sha256") != frozen.sha256
        or record.get("request_body_bytes") != frozen.utf8_bytes
        or record.get("reserved_input_tokens") != RESERVE_INPUT_TOKENS
        or record.get("reserved_output_tokens") != RESERVE_OUTPUT_TOKENS
        or record.get("reserved_cost_usd_micros") != RESERVE_COST_USD_MICROS
        or record.get("status") != "consumed"
        or not isinstance(record.get("reserved_at"), str)
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    summary_json = record.get("summary_json")
    summary: object = None
    if isinstance(summary_json, str):
        try:
            summary = json.loads(summary_json)
        except json.JSONDecodeError:
            summary = None
    if summary != normalized_request_summary(profile, frozen):
        raise GovernanceError("gate2_r9_ledger_tampered")
    try:
        _validate_observed_at(record["reserved_at"])
    except GovernanceError as error:
        raise GovernanceError("gate2_r9_ledger_tampered") from error
    return {
        "reservation_id": reservation_id,
        "stage": stage_value,
        "provider_id": provider_id,
        "request_sha256": frozen.sha256,
        "request_utf8_bytes": frozen.utf8_bytes,
        "summary": summary,
        "reservation_status": "consumed",
        "reserved_at": record["reserved_at"],
    }


_EVIDENCE_TOP_LEVEL = frozenset(
    {
        "contract_version",
        "evidence_class",
        "stage",
        "provider_id",
        "profile_version",
        "role",
        "model_family",
        "official_host",
        "fixture_id",
        "request_sha256",
        "request_utf8_bytes",
        "reservation_id",
        "reservation_status",
        "request_sent",
        "observed_at",
        "host_verification",
        "http_status",
        "timing",
        "response_model_claim",
        "response_model_match",
        "finish_reason",
        "usage",
        "structure_verification",
        "tool_call_verification",
        "tool_name",
        "tool_arguments_match",
        "tool_executed",
        "cost",
        "quota",
        "budget",
        "stage_verdict",
        "local_error_category",
        "identity_note",
    }
)
_EVIDENCE_NESTED: dict[str, frozenset[str]] = {
    "host_verification": frozenset({"status", "final_host", "tls_hostname_verified"}),
    "timing": frozenset(
        {"time_to_headers_ms", "total_latency_ms", "connect_timeout_s", "total_timeout_s"}
    ),
    "usage": frozenset({"status", "prompt_tokens", "completion_tokens", "total_tokens"}),
    "cost": frozenset({"status", "cost_usd"}),
    "quota": frozenset({"status", "fact"}),
    "budget": frozenset(
        {
            "reservation_status",
            "reserved_input_tokens",
            "reserved_output_tokens",
            "reserved_cost_usd_micros",
        }
    ),
}


def _strict_evidence(
    evidence: object,
    *,
    stage: Stage,
    profile: ProviderProfile,
    reservation: Mapping[str, Any],
    ledger_kind: LedgerKind,
) -> None:
    """Complete stored-evidence schema: required fields, types, enums, relations."""
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_TOP_LEVEL:
        raise GovernanceError("gate2_r9_ledger_tampered")
    try:
        validate_evidence(evidence)
    except GovernanceError as error:
        raise GovernanceError("gate2_r9_ledger_tampered") from error
    frozen = profile.request(stage)
    if (
        evidence["contract_version"] != PERSISTENT_CONTRACT_VERSION
        or evidence["evidence_class"] != expected_origin(ledger_kind).value
        or evidence["stage"] != stage.value
        or evidence["provider_id"] != profile.provider_id
        or evidence["profile_version"] != profile.profile_version
        or evidence["role"] != profile.role.value
        or evidence["model_family"] != profile.model_family
        or evidence["official_host"] != profile.official_host
        or evidence["fixture_id"] != frozen.fixture_id
        or evidence["request_sha256"] != frozen.sha256
        or evidence["request_utf8_bytes"] != frozen.utf8_bytes
        or evidence["reservation_id"] != reservation["reservation_id"]
        or evidence["reservation_status"] != "consumed"
        or not (evidence["request_sent"] is None or isinstance(evidence["request_sent"], bool))
        or evidence["response_model_match"] not in {"match", "mismatch", "absent"}
        or not isinstance(evidence["response_model_claim"], str)
        or not isinstance(evidence["finish_reason"], str)
        or evidence["tool_executed"] is not False
        or evidence["stage_verdict"] not in {"pass", "fail"}
        or evidence["local_error_category"] not in _LOCAL_ERRORS
        or evidence["identity_note"] != (
            "route_and_response_claim_only_not_cryptographic_identity"
        )
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    try:
        _validate_observed_at(evidence["observed_at"])
    except GovernanceError as error:
        raise GovernanceError("gate2_r9_ledger_tampered") from error
    http_status = evidence["http_status"]
    if http_status != "unknown" and (
        not isinstance(http_status, int)
        or isinstance(http_status, bool)
        or not 100 <= http_status <= 599
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    for key, required in _EVIDENCE_NESTED.items():
        nested = evidence[key]
        if not isinstance(nested, dict) or set(nested) != required:
            raise GovernanceError("gate2_r9_ledger_tampered")
    host = evidence["host_verification"]
    if (
        host["status"] not in {"verified", "failed"}
        or not isinstance(host["final_host"], str)
        or not isinstance(host["tls_hostname_verified"], bool)
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    timing = evidence["timing"]
    for key in ("time_to_headers_ms", "total_latency_ms"):
        value = timing[key]
        if value != "unknown" and _valid_latency(value) is None:
            raise GovernanceError("gate2_r9_ledger_tampered")
    for key in ("connect_timeout_s", "total_timeout_s"):
        value = timing[key]
        if value is not None and _valid_latency(value) is None:
            raise GovernanceError("gate2_r9_ledger_tampered")
    usage = evidence["usage"]
    counts = [usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]]
    if usage["status"] not in {"known", "missing", "invalid"}:
        raise GovernanceError("gate2_r9_ledger_tampered")
    for value in counts:
        if value != "unknown" and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise GovernanceError("gate2_r9_ledger_tampered")
    if usage["status"] == "known" and (
        "unknown" in counts or counts[2] != counts[0] + counts[1]
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    structure = evidence["structure_verification"]
    tool_verification = evidence["tool_call_verification"]
    if not isinstance(evidence["tool_name"], str):
        raise GovernanceError("gate2_r9_ledger_tampered")
    if stage is Stage.A:
        if (
            structure not in {"pass", "fail", "not_applicable"}
            or tool_verification != "not_applicable"
            or evidence["tool_name"] != "absent"
            or evidence["tool_arguments_match"] != "absent"
        ):
            raise GovernanceError("gate2_r9_ledger_tampered")
    elif structure != "not_applicable" or tool_verification not in {
        "pass",
        "fail",
        "not_applicable",
    }:
        raise GovernanceError("gate2_r9_ledger_tampered")
    cost = evidence["cost"]
    if cost["status"] not in {"known", "unknown", "invalid", "exceeded"}:
        raise GovernanceError("gate2_r9_ledger_tampered")
    if cost["status"] in {"known", "exceeded"}:
        if _valid_cost(cost["cost_usd"]) is None:
            raise GovernanceError("gate2_r9_ledger_tampered")
    elif cost["cost_usd"] != "unknown":
        raise GovernanceError("gate2_r9_ledger_tampered")
    quota = evidence["quota"]
    if (
        quota["status"] not in {"known", "unknown"}
        or quota["fact"] not in {"within_limit", "unknown"}
        or (quota["status"] == "known") != (quota["fact"] == "within_limit")
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    if evidence["budget"] != {
        "reservation_status": "consumed",
        "reserved_input_tokens": RESERVE_INPUT_TOKENS,
        "reserved_output_tokens": RESERVE_OUTPUT_TOKENS,
        "reserved_cost_usd_micros": RESERVE_COST_USD_MICROS,
    }:
        raise GovernanceError("gate2_r9_ledger_tampered")
    derived = derive_evidence_semantics(evidence, profile, stage)
    if (
        evidence["response_model_match"] != derived.model_match
        or evidence["host_verification"]["status"] != derived.host_status
        or not derived.usage_status_consistent
        or not derived.route_facts_consistent
        or not derived.cost_consistent
        or evidence["stage_verdict"] != derived.stage_verdict
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")


def _strict_outcome_row(
    record: Mapping[str, Any],
    reservation: Mapping[str, Any],
    ledger_kind: LedgerKind,
) -> dict[str, Any]:
    request_sent = record.get("request_sent")
    if request_sent not in (0, 1, None):
        raise GovernanceError("gate2_r9_ledger_tampered")
    known_cost = record.get("known_cost_usd_micros")
    if known_cost is not None and (
        not isinstance(known_cost, int) or isinstance(known_cost, bool) or known_cost < 0
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    evidence_json = record.get("evidence_json")
    evidence: object = None
    if isinstance(evidence_json, str):
        try:
            evidence = json.loads(evidence_json)
        except json.JSONDecodeError:
            evidence = None
    profile = _registered_by_id()[reservation["provider_id"]]
    stage = Stage(reservation["stage"])
    _strict_evidence(
        evidence,
        stage=stage,
        profile=profile,
        reservation=reservation,
        ledger_kind=ledger_kind,
    )
    if not isinstance(evidence, dict):
        raise GovernanceError("gate2_r9_ledger_tampered")
    row_request_sent = None if request_sent is None else bool(request_sent)
    if row_request_sent != evidence["request_sent"]:
        raise GovernanceError("gate2_r9_ledger_tampered")
    cost = evidence["cost"]
    if cost["status"] in {"known", "exceeded"}:
        expected_micros = round(float(cost["cost_usd"]) * 1_000_000)
        if known_cost != expected_micros:
            raise GovernanceError("gate2_r9_ledger_tampered")
    elif known_cost is not None:
        raise GovernanceError("gate2_r9_ledger_tampered")
    try:
        _validate_observed_at(record["recorded_at"])
    except GovernanceError as error:
        raise GovernanceError("gate2_r9_ledger_tampered") from error
    return {
        "request_sent": None if request_sent is None else bool(request_sent),
        "known_cost_usd_micros": known_cost,
        "evidence": evidence,
        "outcome_recorded_at": record["recorded_at"],
    }


def _strict_result_row(
    record: Mapping[str, Any],
    reservation: Mapping[str, Any],
    outcome: Mapping[str, Any],
    ledger_kind: LedgerKind,
) -> dict[str, Any]:
    if (
        record.get("verdict") not in {"pass", "fail"}
        or record.get("evidence_origin") != expected_origin(ledger_kind).value
        or record.get("stage") != reservation["stage"]
        or record.get("provider_id") != reservation["provider_id"]
        or record.get("verdict") != outcome["evidence"]["stage_verdict"]
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    try:
        _validate_observed_at(record["recorded_at"])
    except GovernanceError as error:
        raise GovernanceError("gate2_r9_ledger_tampered") from error
    return {
        "stage": record["stage"],
        "provider_id": record["provider_id"],
        "verdict": record["verdict"],
        "evidence_origin": record["evidence_origin"],
        "recorded_at": record["recorded_at"],
    }


def _validate_full_state(
    reservation_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
    ledger_kind: LedgerKind,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    frozenset[tuple[str, str]],
]:
    """Complete semantic validation of the whole ledger state.

    Any inconsistency fails closed as ``gate2_r9_ledger_tampered``. Returns
    normalized reservations, outcomes, stage results, and the set of
    ``(stage, provider_id)`` pairs whose evidence verdict is ``pass``.
    """
    reservations: dict[str, dict[str, Any]] = {}
    for record in reservation_rows:
        normalized = _strict_reservation_row(record)
        if normalized["reservation_id"] in reservations:
            raise GovernanceError("gate2_r9_ledger_tampered")
        reservations[normalized["reservation_id"]] = normalized
    outcomes: dict[str, dict[str, Any]] = {}
    for record in outcome_rows:
        reservation_id = record.get("reservation_id")
        if not isinstance(reservation_id, str):
            raise GovernanceError("gate2_r9_ledger_tampered")
        reservation = reservations.get(reservation_id)
        if reservation is None:
            raise GovernanceError("gate2_r9_ledger_tampered")
        outcomes[reservation_id] = _strict_outcome_row(record, reservation, ledger_kind)
    results: list[dict[str, Any]] = []
    result_reservation_ids: list[str] = []
    for record in result_rows:
        reservation_id = record.get("reservation_id")
        if not isinstance(reservation_id, str):
            raise GovernanceError("gate2_r9_ledger_tampered")
        reservation = reservations.get(reservation_id)
        outcome = outcomes.get(reservation_id)
        if reservation is None or outcome is None:
            raise GovernanceError("gate2_r9_ledger_tampered")
        results.append(_strict_result_row(record, reservation, outcome, ledger_kind))
        result_reservation_ids.append(reservation_id)
    for reservation_id in outcomes:
        if result_reservation_ids.count(reservation_id) != 1:
            raise GovernanceError("gate2_r9_ledger_tampered")
    cumulative = sum(
        outcome["known_cost_usd_micros"]
        for outcome in outcomes.values()
        if outcome["known_cost_usd_micros"] is not None
    )
    if cumulative > MAX_KNOWN_COST_USD_MICROS:
        raise GovernanceError("gate2_r9_ledger_tampered")
    passed = frozenset(
        (result["stage"], result["provider_id"])
        for result in results
        if result["verdict"] == "pass"
    )
    profiles_by_role = {profile.role: profile for profile in registered_profiles()}
    worker_id = profiles_by_role[Role.WORKER].provider_id
    evaluator_id = profiles_by_role[Role.EVALUATOR].provider_id
    present = {
        (reservation["stage"], reservation["provider_id"])
        for reservation in reservations.values()
    }
    if (Stage.A.value, evaluator_id) in present and (
        Stage.A.value,
        worker_id,
    ) not in passed:
        raise GovernanceError("gate2_r9_ledger_tampered")
    if any(stage == Stage.B.value for stage, _ in present) and (
        (Stage.A.value, worker_id) not in passed
        or (Stage.A.value, evaluator_id) not in passed
    ):
        raise GovernanceError("gate2_r9_ledger_tampered")
    if (Stage.B.value, evaluator_id) in present and (
        Stage.B.value,
        worker_id,
    ) not in passed:
        raise GovernanceError("gate2_r9_ledger_tampered")
    return reservations, outcomes, results, passed


_EVIDENCE_ALLOWED_KEYS = frozenset(
    {
        "budget",
        "contract_version",
        "cost",
        "cost_usd",
        "evidence_class",
        "fact",
        "final_host",
        "finish_reason",
        "fixture_id",
        "host_verification",
        "http_status",
        "identity_note",
        "local_error_category",
        "model_family",
        "official_host",
        "observed_at",
        "provider_id",
        "profile_version",
        "quota",
        "request_sent",
        "request_sha256",
        "request_utf8_bytes",
        "reservation_id",
        "reservation_status",
        "reserved_cost_usd_micros",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "response_model_claim",
        "response_model_match",
        "role",
        "stage",
        "stage_verdict",
        "status",
        "structure_verification",
        "timing",
        "time_to_headers_ms",
        "tls_hostname_verified",
        "tool_arguments_match",
        "tool_call_count",
        "tool_call_verification",
        "tool_executed",
        "tool_name",
        "total_latency_ms",
        "connect_timeout_s",
        "total_timeout_s",
        "usage",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
)
_EVIDENCE_ENUMS: dict[str, frozenset[str]] = {
    "stage": frozenset({"stage_a", "stage_b"}),
    "evidence_class": frozenset({"synthetic_only", "production_transport"}),
    "stage_verdict": frozenset({"pass", "fail"}),
    "reservation_status": frozenset({"consumed"}),
    "response_model_match": frozenset({"match", "mismatch", "absent"}),
    "role": frozenset({"worker", "evaluator"}),
    "structure_verification": frozenset({"pass", "fail", "not_applicable"}),
    "tool_call_verification": frozenset({"pass", "fail", "not_applicable"}),
    "tool_arguments_match": frozenset({"match", "mismatch", "absent", "invalid"}),
}
_SENSITIVE_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]|bearer\s|authorization|api[_-]?key|ccswitch|cc_switch|"
    r"/Users/|/home/|/private/|\\\\)",
    re.IGNORECASE,
)


def validate_evidence(value: object, *, _path: str = "evidence") -> None:
    """Recursive strict allowlist; illegal evidence must never be written."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key not in _EVIDENCE_ALLOWED_KEYS:
                raise GovernanceError("gate2_r9_evidence_field_rejected")
            enum = _EVIDENCE_ENUMS.get(key)
            if enum is not None and item not in enum:
                raise GovernanceError("gate2_r9_evidence_field_rejected")
            validate_evidence(item, _path=f"{_path}.{key}")
        return
    if isinstance(value, list):
        for item in value:
            validate_evidence(item, _path=_path)
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GovernanceError("gate2_r9_evidence_field_rejected")
        return
    if isinstance(value, str):
        if len(value) > 512 or _SENSITIVE_VALUE.search(value) is not None:
            raise GovernanceError("gate2_r9_evidence_field_rejected")
        return
    raise GovernanceError("gate2_r9_evidence_field_rejected")


@dataclass(frozen=True)
class DerivedSemantics:
    """Facts deterministically re-derived from an evidence document."""

    model_match: str
    host_status: str
    usage_known: bool
    usage_status_consistent: bool
    route_facts_consistent: bool
    finish_ok: bool
    timeouts_ok: bool
    latency_ok: bool
    cost_consistent: bool
    stage_verdict: str


def derive_evidence_semantics(
    evidence: Mapping[str, Any], profile: ProviderProfile, stage: Stage
) -> DerivedSemantics:
    """Single side-effect-free verdict derivation shared by production and
    stored-evidence validation. There is exactly one pass condition."""
    claim = evidence["response_model_claim"]
    if isinstance(claim, str) and claim.strip() and claim != "absent":
        model_match = "match" if claim == profile.model_id else "mismatch"
    else:
        model_match = "absent"
    host = evidence["host_verification"]
    host_verified = (
        host["final_host"] == profile.official_host
        and host["tls_hostname_verified"] is True
    )
    host_status = "verified" if host_verified else "failed"
    timing = evidence["timing"]
    timeouts_ok = (
        timing["connect_timeout_s"] == CONNECT_TIMEOUT_SECONDS
        and timing["total_timeout_s"] == TOTAL_TIMEOUT_SECONDS
    )
    tth = timing["time_to_headers_ms"]
    total_latency = timing["total_latency_ms"]
    latency_ok = (
        tth != "unknown"
        and total_latency != "unknown"
        and _valid_latency(tth) is not None
        and _valid_latency(total_latency) is not None
        and float(tth) <= float(total_latency) <= MAX_TOTAL_LATENCY_MS
    )
    usage = evidence["usage"]
    counts = [usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]]
    counts_valid = (
        all(isinstance(value, int) and not isinstance(value, bool) for value in counts)
        and 0 <= counts[0] <= RESERVE_INPUT_TOKENS
        and 0 <= counts[1] <= RESERVE_OUTPUT_TOKENS
        and 0 <= counts[2] <= MAX_INPUT_TOKENS
        and counts[2] == counts[0] + counts[1]
    )
    all_unknown = all(value == "unknown" for value in counts)
    if usage["status"] == "known":
        usage_status_consistent = counts_valid
    elif usage["status"] == "missing":
        usage_status_consistent = all_unknown
    elif usage["status"] == "invalid":
        usage_status_consistent = not counts_valid
    else:
        usage_status_consistent = False
    usage_known = usage["status"] == "known" and counts_valid
    http_raw = evidence["http_status"]
    normalized_status = None if http_raw == "unknown" else http_raw
    route_facts_consistent = _route_facts_consistent(
        evidence["request_sent"], normalized_status, evidence["local_error_category"]
    )
    finish_set = STAGE_A_FINISH_REASONS if stage is Stage.A else STAGE_B_FINISH_REASONS
    finish_ok = evidence["finish_reason"] in finish_set
    if stage is Stage.A:
        capability_ok = evidence["structure_verification"] == "pass"
    else:
        capability_ok = (
            evidence["tool_call_verification"] == "pass"
            and evidence["tool_name"] == TOOL_NAME
            and evidence["tool_arguments_match"] == "match"
        )
    cost = evidence["cost"]
    cost_value = cost["cost_usd"]
    if cost_value == "unknown":
        cost_consistent = cost["status"] in {"unknown", "invalid"}
        cost_pass = cost["status"] == "unknown"
    else:
        valid_cost = _valid_cost(cost_value)
        if valid_cost is None:
            cost_consistent = False
            cost_pass = False
        else:
            expected_status = "known" if valid_cost <= MAX_SINGLE_COST_USD else "exceeded"
            cost_consistent = cost["status"] == expected_status
            cost_pass = expected_status == "known"
    verdict = (
        "pass"
        if all(
            (
                evidence["request_sent"] is True,
                evidence["http_status"] == 200,
                evidence["local_error_category"] == "none",
                model_match == "match",
                host_verified,
                timeouts_ok,
                latency_ok,
                usage_known,
                finish_ok,
                capability_ok,
                cost_pass,
            )
        )
        else "fail"
    )
    return DerivedSemantics(
        model_match=model_match,
        host_status=host_status,
        usage_known=usage_known,
        usage_status_consistent=usage_status_consistent,
        route_facts_consistent=route_facts_consistent,
        finish_ok=finish_ok,
        timeouts_ok=timeouts_ok,
        latency_ok=latency_ok,
        cost_consistent=cost_consistent,
        stage_verdict=verdict,
    )


def evaluate_result(
    plan: GatePlan,
    profile: ProviderProfile,
    stage: Stage,
    request: FrozenRequest,
    reservation_id: str,
    result: TransportResult,
    *,
    observed_at: str,
) -> dict[str, Any]:
    validate_gate_plan(plan)
    validate_frozen_request(request)
    _validate_reservation_id(reservation_id)
    _validate_observed_at(observed_at)
    _validate_transport_result(result)

    adapter = adapter_for(profile)
    response = result.response_json if isinstance(result.response_json, Mapping) else {}
    local_error = result.local_error_category or "none"
    http_ok = result.request_sent is True and result.http_status == 200 and local_error == "none"

    model_claim = adapter.parse_response_model(response)
    if isinstance(model_claim, str) and model_claim.strip():
        model_match = "match" if model_claim == profile.model_id else "mismatch"
    else:
        model_match = "absent"

    structure_verification = "not_applicable"
    tool_verification = "not_applicable"
    tool_name: object = "absent"
    tool_arguments_match: object = "absent"
    if http_ok and stage is Stage.A:
        structured = adapter.parse_structured_content(response)
        structure_verification = "pass" if structured == _STAGE_A_EXPECTED_OUTPUT else "fail"
    if http_ok and stage is Stage.B:
        raw_calls = adapter.parse_tool_calls(response)
        tool_verification = "fail"
        if (
            isinstance(raw_calls, list)
            and len(raw_calls) == 1
            and isinstance(raw_calls[0], Mapping)
        ):
            call = raw_calls[0]
            function = call.get("function")
            if call.get("type") == "function" and isinstance(function, Mapping):
                name = function.get("name")
                tool_name = name if isinstance(name, str) and name else "absent"
                arguments = function.get("arguments")
                parsed: object = None
                parseable = False
                if isinstance(arguments, str):
                    try:
                        parsed = json.loads(arguments)
                        parseable = True
                    except json.JSONDecodeError:
                        parseable = False
                if parseable and parsed == TOOL_ARGUMENTS:
                    tool_arguments_match = "match"
                elif parseable:
                    tool_arguments_match = "mismatch"
                else:
                    tool_arguments_match = "invalid"
                if tool_name == TOOL_NAME and tool_arguments_match == "match":
                    tool_verification = "pass"

    usage = adapter.parse_usage(response)
    prompt = _valid_count(usage.get(profile.usage_prompt_field), RESERVE_INPUT_TOKENS)
    completion = _valid_count(usage.get(profile.usage_completion_field), RESERVE_OUTPUT_TOKENS)
    total = _valid_count(usage.get(profile.usage_total_field), MAX_INPUT_TOKENS)
    if not usage:
        usage_status = "missing"
    elif prompt is None or completion is None or total is None or total != prompt + completion:
        usage_status = "invalid"
    else:
        usage_status = "known"

    finish_reason = adapter.parse_finish_reason(response)

    host_ok = result.final_host == profile.official_host and result.tls_hostname_verified is True
    tth = _valid_latency(result.time_to_headers_ms)
    total_latency = _valid_latency(result.total_latency_ms)

    if result.provider_cost_usd is None:
        cost_status = "unknown"
        cost_value: object = "unknown"
    elif _valid_cost(result.provider_cost_usd) is None:
        cost_status = "invalid"
        cost_value = "unknown"
    else:
        cost = float(result.provider_cost_usd)
        if cost > MAX_SINGLE_COST_USD:
            cost_status = "exceeded"
        else:
            cost_status = "known"
        cost_value = cost
    quota_known = result.provider_quota_fact == "within_limit"

    evidence: dict[str, Any] = {
        "contract_version": PERSISTENT_CONTRACT_VERSION,
        "evidence_class": result.evidence_origin.value,
        "stage": stage.value,
        "provider_id": profile.provider_id,
        "profile_version": profile.profile_version,
        "role": profile.role.value,
        "model_family": profile.model_family,
        "official_host": profile.official_host,
        "fixture_id": request.fixture_id,
        "request_sha256": request.sha256,
        "request_utf8_bytes": request.utf8_bytes,
        "reservation_id": reservation_id,
        "reservation_status": "consumed",
        "request_sent": result.request_sent,
        "observed_at": observed_at,
        "host_verification": {
            "status": "verified" if host_ok else "failed",
            "final_host": result.final_host if isinstance(result.final_host, str) else "unknown",
            "tls_hostname_verified": result.tls_hostname_verified is True,
        },
        "http_status": result.http_status if result.http_status is not None else "unknown",
        "timing": {
            "time_to_headers_ms": tth if tth is not None else "unknown",
            "total_latency_ms": (total_latency if total_latency is not None else "unknown"),
            "connect_timeout_s": result.connect_timeout_s,
            "total_timeout_s": result.total_timeout_s,
        },
        "response_model_claim": (
            model_claim if isinstance(model_claim, str) and model_claim.strip() else "absent"
        ),
        "response_model_match": model_match,
        "finish_reason": (
            finish_reason if isinstance(finish_reason, str) and finish_reason else "unknown"
        ),
        "usage": {
            "status": usage_status,
            "prompt_tokens": prompt if prompt is not None else "unknown",
            "completion_tokens": completion if completion is not None else "unknown",
            "total_tokens": total if total is not None else "unknown",
        },
        "structure_verification": structure_verification,
        "tool_call_verification": tool_verification,
        "tool_name": tool_name,
        "tool_arguments_match": tool_arguments_match,
        "tool_executed": False,
        "cost": {
            "status": cost_status,
            "cost_usd": cost_value,
        },
        "quota": {
            "status": "known" if quota_known else "unknown",
            "fact": "within_limit" if quota_known else "unknown",
        },
        "budget": {
            "reservation_status": "consumed",
            "reserved_input_tokens": RESERVE_INPUT_TOKENS,
            "reserved_output_tokens": RESERVE_OUTPUT_TOKENS,
            "reserved_cost_usd_micros": RESERVE_COST_USD_MICROS,
        },
        "stage_verdict": "fail",
        "local_error_category": local_error,
        "identity_note": "route_and_response_claim_only_not_cryptographic_identity",
    }
    derived = derive_evidence_semantics(evidence, profile, stage)
    evidence["stage_verdict"] = derived.stage_verdict
    validate_evidence(evidence)
    return evidence


class Gate2R9Runner:
    """Offline runner; consumes injected synthetic transport facts only."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        ledger_kind: LedgerKind = LedgerKind.SYNTHETIC,
        id_factory: Callable[[], str] = lambda: f"r9-{uuid.uuid4().hex}",
        clock: Callable[[], str] = lambda: datetime.now(UTC).isoformat(),
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_kind = ledger_kind
        self.id_factory = id_factory
        self.clock = clock

    def run_synthetic_stage_a_pair(
        self, authorization: str, results: Mapping[str, TransportResult]
    ) -> dict[str, Any]:
        return self._run_synthetic_pair(Stage.A, authorization, results)

    def run_synthetic_stage_b_pair(
        self, authorization: str, results: Mapping[str, TransportResult]
    ) -> dict[str, Any]:
        return self._run_synthetic_pair(Stage.B, authorization, results)

    def _run_synthetic_pair(
        self, stage: Stage, authorization: str, results: Mapping[str, TransportResult]
    ) -> dict[str, Any]:
        plan = default_gate_plan()
        validate_gate_plan(plan)
        if authorization in (STAGE_A_AUTHORIZATION, STAGE_B_AUTHORIZATION):
            raise GovernanceError("gate2_r9_live_phrase_rejected_for_synthetic")
        expected = (
            SYNTHETIC_STAGE_A_SIMULATION if stage is Stage.A else SYNTHETIC_STAGE_B_SIMULATION
        )
        if authorization != expected:
            raise GovernanceError("gate2_r9_authorization_required")
        if self.ledger_kind is not LedgerKind.SYNTHETIC:
            raise GovernanceError("gate2_r9_synthetic_requires_synthetic_ledger")
        if not isinstance(results, Mapping):
            raise GovernanceError("gate2_r9_transport_facts_invalid")
        ledger = Gate2R9Ledger(
            self.ledger_path, ledger_kind=self.ledger_kind, id_factory=self.id_factory
        )
        outcomes: dict[str, Any] = {}
        for profile in plan.stage_order(stage):
            result = results.get(profile.provider_id)
            if result is None:
                raise GovernanceError("gate2_r9_synthetic_result_required")
            _validate_transport_result(result)
            if result.evidence_origin is not EvidenceOrigin.SYNTHETIC_ONLY:
                raise GovernanceError("gate2_r9_synthetic_result_required")
            request = adapter_for(profile).build_request(profile, stage)
            reservation_id = ledger.reserve(plan, profile, stage, request)
            evidence = ledger.record_outcome(
                plan,
                profile,
                stage,
                reservation_id,
                request,
                result,
                observed_at=self.clock(),
            )
            outcomes[profile.provider_id] = evidence
            if evidence["stage_verdict"] != "pass":
                break
        pair_verdict = (
            "pass"
            if len(outcomes) == 2
            and all(item["stage_verdict"] == "pass" for item in outcomes.values())
            else "fail"
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "stage": stage.value,
            "pair_verdict": pair_verdict,
            "results": outcomes,
        }

    def run_local_fault_injection(self, fault: str) -> dict[str, Any]:
        """Deterministic pre-reservation fault probe; zero side effects."""
        if fault not in {"disconnect", "timeout"}:
            raise GovernanceError("gate2_r9_fault_invalid")
        return {
            "contract_version": CONTRACT_VERSION,
            "fault": fault,
            "injected_before": "reservation_and_transport",
            "credential_reads": 0,
            "reservations": 0,
            "ledger_writes": 0,
            "transport_calls": 0,
            "request_sent": False,
            "retry_count": 0,
            "retry_storm": False,
            "verdict": "no_duplicate_send_no_retry_storm",
        }

    def run_live_stage(
        self,
        stage: Stage,
        *,
        live: bool,
        authorization: str,
        ledger_path: Path,
        evidence_root: Path,
        lock_path: Path,
        repo_root: Path,
        credential_reader: Callable[[str], str],
        transport: Callable[[ProviderProfile, FrozenRequest, str], TransportResult],
    ) -> dict[str, Any]:
        """Stage A live entry. Any failure before transport is zero-side-effect."""
        plan = default_gate_plan()
        validate_gate_plan(plan)
        if not live:
            raise GovernanceError("gate2_r9_live_mode_required")
        if stage is Stage.B and authorization != STAGE_B_AUTHORIZATION:
            raise GovernanceError("gate2_r9_authorization_required")
        if stage is Stage.A and authorization != STAGE_A_AUTHORIZATION:
            raise GovernanceError("gate2_r9_authorization_required")
        root = Path(repo_root).resolve()
        expected_paths = tuple(
            (root / path).resolve()
            for path in (DEFAULT_LEDGER_PATH, DEFAULT_EVIDENCE_ROOT, DEFAULT_LOCK_PATH)
        )
        candidates = (Path(ledger_path), Path(evidence_root), Path(lock_path))
        trusted_ledger, trusted_evidence, trusted_lock = tuple(
            path.resolve() if path.is_absolute() else (root / path).resolve()
            for path in candidates
        )
        if (trusted_ledger, trusted_evidence, trusted_lock) != expected_paths:
            raise GovernanceError("gate2_r9_frozen_path_mismatch")
        for raw in candidates:
            _reject_symlink_path(raw if raw.is_absolute() else (root / raw))
        self_path = Path(self.ledger_path)
        self_resolved = (
            self_path.resolve()
            if self_path.is_absolute()
            else (root / self_path).resolve()
        )
        if self_resolved != trusted_ledger:
            raise GovernanceError("gate2_r9_frozen_path_mismatch")
        if self.ledger_kind is not LedgerKind.LIVE:
            raise GovernanceError("gate2_r9_live_requires_live_ledger")
        with _nonblocking_flock(trusted_lock):
            ledger = Gate2R9Ledger(
                trusted_ledger,
                ledger_kind=self.ledger_kind,
                id_factory=self.id_factory,
            )
            if ledger.path.exists():
                reservations, outcomes, _, passed = ledger.validated_state()
            else:
                reservations, outcomes, passed = {}, {}, frozenset()
            executed: list[dict[str, Any]] = []
            publication: dict[str, Any] | None = None
            try:
                live_plan = _decide_live_stage(
                    plan, reservations, outcomes, passed, stage
                )
                for profile in live_plan.todo:
                    request = adapter_for(profile).build_request(profile, stage)
                    credential = credential_reader(profile.keychain_service)
                    if not isinstance(credential, str) or not credential:
                        raise GovernanceError("gate2_r9_credential_unavailable")
                    try:
                        reservation_id = ledger.reserve(plan, profile, stage, request)
                        try:
                            result = transport(profile, request, credential)
                        except Exception:
                            result = TransportResult(
                                request_sent=None,
                                http_status=None,
                                local_error_category="transport_exception",
                            )
                    finally:
                        credential = ""
                    _validate_transport_result(result)
                    evidence = ledger.record_outcome(
                        plan,
                        profile,
                        stage,
                        reservation_id,
                        request,
                        result,
                        observed_at=self.clock(),
                    )
                    executed.append(evidence)
                    if evidence["stage_verdict"] != "pass":
                        break
            except GovernanceError:
                if reservations or executed:
                    publication = publish_stage_a_evidence(
                        ledger, trusted_evidence, plan
                    )
                raise
            publication = publish_stage_a_evidence(ledger, trusted_evidence, plan)
        return {
            "contract_version": CONTRACT_VERSION,
            "stage": stage.value,
            "executed": executed,
            "publication": publication,
        }


def forbidden_credential_reader(service: str) -> str:
    """Offline candidate never reads Keychain; this reader always fails closed."""
    raise GovernanceError("gate2_r9_credential_read_forbidden")


class SecurityCommandCredentialReader:
    """Production Keychain boundary; only the two frozen services, memory only."""

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._run = run

    def __call__(self, service: str) -> str:
        allowed = {profile.keychain_service for profile in registered_profiles()}
        if service not in allowed:
            raise GovernanceError("gate2_r9_credential_service_rejected")
        try:
            completed = self._run(
                ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GovernanceError("gate2_r9_credential_unavailable") from error
        if completed.returncode != 0:
            raise GovernanceError("gate2_r9_credential_unavailable")
        credential = completed.stdout.removesuffix("\n")
        if not credential:
            raise GovernanceError("gate2_r9_credential_unavailable")
        return credential


_MAX_RESPONSE_BYTES = 1_048_576


class _SocketLike(Protocol):
    """Minimal socket surface the transport actually uses."""

    def getpeername(self, /) -> tuple[str, int]: ...

    def settimeout(self, value: float, /) -> None: ...


class _ResponseLike(Protocol):
    """Minimal HTTP response surface the transport actually uses."""

    @property
    def status(self) -> int: ...

    def read(self, amt: int, /) -> bytes: ...


class _ConnectionLike(Protocol):
    """Minimal HTTPS connection surface; HTTPSConnection satisfies it
    structurally, and offline fake connections satisfy it as well."""

    @property
    def sock(self) -> _SocketLike | None: ...

    def connect(self, /) -> None: ...

    def request(
        self,
        method: str,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> None: ...

    def getresponse(self, /) -> _ResponseLike: ...

    def close(self, /) -> None: ...


def _default_resolver(host: str) -> frozenset[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise GovernanceError("gate2_r9_dns_resolution_failed") from error
    return frozenset(str(info[4][0]) for info in infos)


def _default_connection_factory(
    host: str, port: int, timeout: float, context: ssl.SSLContext
) -> _ConnectionLike:
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)


class HttpsStageATransport:
    """Production HTTPS transport: stdlib only, one request, zero retries."""

    def __init__(
        self,
        *,
        resolver: Callable[[str], frozenset[str]] = _default_resolver,
        connection_factory: Callable[
            [str, int, float, ssl.SSLContext], _ConnectionLike
        ] = _default_connection_factory,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._monotonic = monotonic

    def __call__(
        self, profile: ProviderProfile, request: FrozenRequest, credential: str
    ) -> TransportResult:
        parts = urlsplit(profile.endpoint)
        host = parts.hostname
        if (
            host is None
            or parts.scheme != "https"
            or host != profile.official_host
            or parts.port not in (None, 443)
        ):
            raise GovernanceError("gate2_r9_endpoint_drift")
        validate_frozen_request(request)
        if not isinstance(credential, str) or not credential:
            raise GovernanceError("gate2_r9_credential_unavailable")
        try:
            addresses = self._resolver(host)
        except Exception:
            return TransportResult(
                request_sent=False,
                http_status=None,
                local_error_category="transport_failure",
            )
        if not addresses:
            return TransportResult(
                request_sent=False,
                http_status=None,
                local_error_category="transport_failure",
            )
        context = ssl.create_default_context()
        started = self._monotonic()

        def remaining() -> float:
            return TOTAL_TIMEOUT_SECONDS - (self._monotonic() - started)

        connection = self._connection_factory(host, 443, CONNECT_TIMEOUT_SECONDS, context)
        try:
            try:
                connection.connect()
            except ssl.SSLError:
                return TransportResult(
                    request_sent=False,
                    http_status=None,
                    local_error_category="transport_failure",
                )
            except (OSError, http.client.HTTPException):
                return TransportResult(
                    request_sent=None,
                    http_status=None,
                    local_error_category="transport_exception",
                )
            sock = connection.sock
            peer = sock.getpeername()[0] if sock is not None else None
            if peer not in addresses:
                return TransportResult(
                    request_sent=False,
                    http_status=None,
                    tls_hostname_verified=True,
                    local_error_category="transport_failure",
                )
            before_request = remaining()
            if before_request <= 0:
                return TransportResult(
                    request_sent=False,
                    http_status=None,
                    tls_hostname_verified=True,
                    local_error_category="transport_failure",
                )
            if sock is not None:
                sock.settimeout(before_request)
            try:
                send_started = self._monotonic()
                connection.request(
                    "POST",
                    parts.path,
                    body=request.canonical_body.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {credential}",
                    },
                )
            except (OSError, http.client.HTTPException):
                return TransportResult(
                    request_sent=None,
                    http_status=None,
                    local_error_category="transport_exception",
                )
            before_headers = remaining()
            if before_headers <= 0:
                return TransportResult(
                    request_sent=True,
                    http_status=None,
                    local_error_category="transport_failure",
                )
            if sock is not None:
                sock.settimeout(before_headers)
            try:
                response = connection.getresponse()
                tth_ms = (self._monotonic() - send_started) * 1000.0
            except (OSError, http.client.HTTPException):
                return TransportResult(
                    request_sent=True,
                    http_status=None,
                    local_error_category="transport_exception",
                )
            status = response.status

            def sent_result(
                local_error: str,
                response_json: Mapping[str, Any] | None = None,
            ) -> TransportResult:
                return TransportResult(
                    request_sent=True,
                    http_status=status,
                    response_json=response_json,
                    time_to_headers_ms=tth_ms,
                    total_latency_ms=(self._monotonic() - started) * 1000.0,
                    connect_timeout_s=CONNECT_TIMEOUT_SECONDS,
                    total_timeout_s=TOTAL_TIMEOUT_SECONDS,
                    final_host=host,
                    tls_hostname_verified=True,
                    local_error_category=local_error,
                )

            if status in {301, 302, 303, 307, 308}:
                return sent_result("redirect_rejected")
            if status != 200:
                return sent_result("http_failure")
            before_read = remaining()
            if before_read <= 0:
                return sent_result("transport_failure")
            if sock is not None:
                sock.settimeout(before_read)
            try:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                return sent_result("response_read_failure")
            if len(payload) > _MAX_RESPONSE_BYTES:
                return sent_result("response_too_large")
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return sent_result("response_decode_failure")
            if not isinstance(decoded, dict):
                return sent_result("response_not_object")
            return sent_result("none", decoded)
        finally:
            connection.close()


@contextmanager
def _nonblocking_flock(path: Path) -> Iterator[None]:
    """Non-blocking exclusive flock; contenders fail before any side effect."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise GovernanceError("gate2_r9_live_lock_unavailable") from error
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _LivePlan:
    todo: tuple[ProviderProfile, ...]
    completed: bool


def _decide_live_stage(
    plan: GatePlan,
    reservations: Mapping[str, Mapping[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]],
    passed: frozenset[tuple[str, str]],
    stage: Stage,
) -> _LivePlan:
    """Restart-safe decision from the fully validated ledger state.

    Stage B eligibility comes only from validated Stage A pair passes in the
    same LIVE ledger; nothing is inferred from docs, evidence, or CLI input.
    """

    def has_reservation(profile: ProviderProfile) -> bool:
        return any(
            record["provider_id"] == profile.provider_id
            and record["stage"] == stage.value
            for record in reservations.values()
        )

    def has_outcome(profile: ProviderProfile) -> bool:
        return any(
            reservations[reservation_id]["provider_id"] == profile.provider_id
            and reservations[reservation_id]["stage"] == stage.value
            for reservation_id in outcomes
        )

    if stage is Stage.B:
        pair_a_passed = (Stage.A.value, plan.worker.provider_id) in passed and (
            Stage.A.value,
            plan.evaluator.provider_id,
        ) in passed
        if not pair_a_passed:
            raise GovernanceError("gate2_r9_stage_a_pair_not_passed")
    worker, evaluator = plan.worker, plan.evaluator
    worker_pass = (stage.value, worker.provider_id) in passed
    worker_failed_code = (
        "gate2_r9_stage_a_worker_not_passed"
        if stage is Stage.A
        else "gate2_r9_stage_b_worker_not_passed"
    )
    if has_reservation(worker) and not has_outcome(worker):
        raise GovernanceError("gate2_r9_live_unresolved_reservation")
    if has_outcome(worker) and not worker_pass:
        raise GovernanceError(worker_failed_code)
    if has_reservation(evaluator):
        if not has_outcome(evaluator):
            raise GovernanceError("gate2_r9_live_unresolved_reservation")
        return _LivePlan(todo=(), completed=True)
    if worker_pass:
        return _LivePlan(todo=(evaluator,), completed=False)
    return _LivePlan(todo=(worker, evaluator), completed=False)


_SUMMARY_ALLOWED_KEYS = frozenset(
    {
        "contract_version",
        "stage",
        "ledger_kind",
        "pair_verdict",
        "stop_reason",
        "generated_at",
        "providers",
        "requests",
        "stages",
        "evidence_format_version",
        "stage_a",
        "stage_b",
        "stage_b_sha256",
        "stage_b_utf8_bytes",
        "verdicts",
        "reservations",
        "reservation_counters",
        "send_counters",
        "not_sent_counters",
        "unknown_send_counters",
        "provider_id",
        "role",
        "profile_version",
        "model_family",
        "official_host",
        "model_id",
        "stage_a_sha256",
        "stage_a_utf8_bytes",
        "status",
        "request_sent",
        "cost_status",
        "quota_status",
    }
)
_SUMMARY_ENUMS: dict[str, frozenset[str]] = {
    "stage": frozenset({"stage_a"}),
    "ledger_kind": frozenset({"live"}),
    "pair_verdict": frozenset({"pass", "fail", "incomplete", "not_started"}),
    "stop_reason": frozenset(
        {
            "pair_complete",
            "worker_failed",
            "evaluator_failed",
            "pair_incomplete",
            "not_started",
        }
    ),
    "evidence_format_version": frozenset({"p4a-gate2-r9-stage-layered-v2"}),
    "role": frozenset({"worker", "evaluator"}),
    "status": frozenset({"consumed", "absent"}),
    "cost_status": frozenset({"known", "unknown", "invalid", "exceeded", "not_recorded"}),
    "quota_status": frozenset({"known", "unknown", "not_recorded"}),
}


def _validate_summary(value: object) -> None:
    if isinstance(value, Mapping):
        allowed_keys = _SUMMARY_ALLOWED_KEYS | {
            profile.provider_id for profile in registered_profiles()
        }
        for key, item in value.items():
            if not isinstance(key, str) or key not in allowed_keys:
                raise GovernanceError("gate2_r9_evidence_field_rejected")
            enum = _SUMMARY_ENUMS.get(key)
            if enum is not None and isinstance(item, str) and item not in enum:
                raise GovernanceError("gate2_r9_evidence_field_rejected")
            _validate_summary(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_summary(item)
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GovernanceError("gate2_r9_evidence_field_rejected")
        return
    if isinstance(value, str):
        if len(value) > 512 or _SENSITIVE_VALUE.search(value) is not None:
            raise GovernanceError("gate2_r9_evidence_field_rejected")
        return
    raise GovernanceError("gate2_r9_evidence_field_rejected")


_SNAPSHOT_DIR_NAME = re.compile(r"sha256-[0-9a-f]{64}\Z")


def _reject_symlink_path(path: Path) -> None:
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents)):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise GovernanceError("gate2_r9_evidence_symlink_rejected")


def _stage_layer(
    stage: Stage,
    ordered: list[dict[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]],
    passed: frozenset[tuple[str, str]],
    plan: GatePlan,
) -> dict[str, Any]:
    """Per-stage, per-provider facts; Stage A/B never share counters."""
    records = [record for record in ordered if record["stage"] == stage.value]
    verdicts: dict[str, str] = {}
    reservation_summary: dict[str, dict[str, Any]] = {}
    counters: dict[str, dict[str, int]] = {}
    cost_status: dict[str, str] = {}
    quota_status: dict[str, str] = {}
    for profile in (plan.worker, plan.evaluator):
        provider_records = [
            record
            for record in records
            if record["provider_id"] == profile.provider_id
        ]
        sent = not_sent = unknown = 0
        for record in provider_records:
            outcome = outcomes.get(record["reservation_id"])
            if outcome is None:
                unknown += 1
            elif outcome["request_sent"] is True:
                sent += 1
            elif outcome["request_sent"] is False:
                not_sent += 1
            else:
                unknown += 1
        if sent + not_sent + unknown != len(provider_records):
            raise GovernanceError("gate2_r9_ledger_tampered")
        counters[profile.provider_id] = {
            "reservation": len(provider_records),
            "sent": sent,
            "not_sent": not_sent,
            "unknown": unknown,
        }
        first_record = provider_records[0] if provider_records else None
        if first_record is None:
            verdicts[profile.provider_id] = "not_recorded"
            reservation_summary[profile.provider_id] = {
                "status": "absent",
                "request_sent": "absent",
            }
            cost_status[profile.provider_id] = "not_recorded"
            quota_status[profile.provider_id] = "not_recorded"
            continue
        outcome = outcomes.get(first_record["reservation_id"])
        verdicts[profile.provider_id] = (
            "pass"
            if (stage.value, profile.provider_id) in passed
            else ("fail" if outcome is not None else "not_recorded")
        )
        reservation_summary[profile.provider_id] = {
            "status": "consumed",
            "request_sent": (
                outcome["request_sent"] if outcome is not None else None
            ),
        }
        cost_status[profile.provider_id] = (
            outcome["evidence"]["cost"]["status"] if outcome is not None else "not_recorded"
        )
        quota_status[profile.provider_id] = (
            outcome["evidence"]["quota"]["status"]
            if outcome is not None
            else "not_recorded"
        )
    if not records:
        pair_verdict = "not_started"
        stop_reason = "not_started"
    elif all(
        (stage.value, profile.provider_id) in passed
        for profile in (plan.worker, plan.evaluator)
    ):
        pair_verdict = "pass"
        stop_reason = "pair_complete"
    elif any(
        outcomes[record["reservation_id"]]["evidence"]["stage_verdict"] == "fail"
        for record in records
        if record["reservation_id"] in outcomes
    ):
        pair_verdict = "fail"
        failed_provider = next(
            record["provider_id"]
            for record in records
            if record["reservation_id"] in outcomes
            and outcomes[record["reservation_id"]]["evidence"]["stage_verdict"] == "fail"
        )
        stop_reason = (
            "worker_failed" if failed_provider == plan.worker.provider_id else "evaluator_failed"
        )
    else:
        pair_verdict = "incomplete"
        stop_reason = "pair_incomplete"
    return {
        "pair_verdict": pair_verdict,
        "stop_reason": stop_reason,
        "verdicts": verdicts,
        "reservations": reservation_summary,
        "reservation_counters": {pid: counters[pid]["reservation"] for pid in counters},
        "send_counters": {pid: counters[pid]["sent"] for pid in counters},
        "not_sent_counters": {pid: counters[pid]["not_sent"] for pid in counters},
        "unknown_send_counters": {pid: counters[pid]["unknown"] for pid in counters},
        "cost_status": cost_status,
        "quota_status": quota_status,
    }


def publish_stage_a_evidence(
    ledger: Gate2R9Ledger,
    evidence_root: Path,
    plan: GatePlan,
) -> dict[str, Any]:
    """Publish an immutable snapshot of the validated ledger state.

    The snapshot ID is the SHA-256 of the canonical validated ledger
    projection, so identical state republishes idempotently regardless of
    the caller's clock. Snapshots are never overwritten or deleted.
    """
    reservations, outcomes, results, passed = ledger.validated_state()
    if not reservations:
        raise GovernanceError("gate2_r9_evidence_requires_reservation")
    ordered = sorted(
        reservations.values(),
        key=lambda record: (record["reserved_at"], record["reservation_id"]),
    )
    projection = {
        "contract_version": PERSISTENT_CONTRACT_VERSION,
        "ledger_kind": ledger.ledger_kind.value,
        "reservations": [reservations[rid] for rid in sorted(reservations)],
        "outcomes": {rid: outcomes[rid] for rid in sorted(outcomes)},
        "stage_results": sorted(
            results, key=lambda item: (item["stage"], item["provider_id"])
        ),
    }
    has_stage_b = any(record["stage"] == Stage.B.value for record in ordered)
    if has_stage_b:
        projection["evidence_format_version"] = EVIDENCE_FORMAT_LAYERED_V2
    snapshot_id = (
        "sha256-"
        + hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()
    )
    time_facts = [record["reserved_at"] for record in ordered]
    time_facts += [
        outcome["outcome_recorded_at"] for outcome in outcomes.values()
    ]
    time_facts += [item["recorded_at"] for item in results]
    generated_at = max(time_facts)
    providers_summary = [
        {
            "provider_id": profile.provider_id,
            "role": profile.role.value,
            "profile_version": profile.profile_version,
            "model_family": profile.model_family,
            "official_host": profile.official_host,
            "model_id": profile.model_id,
        }
        for profile in (plan.worker, plan.evaluator)
    ]
    if has_stage_b:
        summary: dict[str, Any] = {
            "contract_version": PERSISTENT_CONTRACT_VERSION,
            "evidence_format_version": EVIDENCE_FORMAT_LAYERED_V2,
            "ledger_kind": ledger.ledger_kind.value,
            "generated_at": generated_at,
            "providers": providers_summary,
            "requests": {
                profile.provider_id: {
                    "stage_a_sha256": profile.request(Stage.A).sha256,
                    "stage_a_utf8_bytes": profile.request(Stage.A).utf8_bytes,
                    "stage_b_sha256": profile.request(Stage.B).sha256,
                    "stage_b_utf8_bytes": profile.request(Stage.B).utf8_bytes,
                }
                for profile in (plan.worker, plan.evaluator)
            },
            "stages": {
                "stage_a": _stage_layer(Stage.A, ordered, outcomes, passed, plan),
                "stage_b": _stage_layer(Stage.B, ordered, outcomes, passed, plan),
            },
        }
    else:
        stage_a_layer = _stage_layer(Stage.A, ordered, outcomes, passed, plan)
        summary = {
            "contract_version": PERSISTENT_CONTRACT_VERSION,
            "stage": Stage.A.value,
            "ledger_kind": ledger.ledger_kind.value,
            "pair_verdict": stage_a_layer["pair_verdict"],
            "stop_reason": stage_a_layer["stop_reason"],
            "generated_at": generated_at,
            "providers": providers_summary,
            "requests": {
                profile.provider_id: {
                    "stage_a_sha256": profile.request(Stage.A).sha256,
                    "stage_a_utf8_bytes": profile.request(Stage.A).utf8_bytes,
                }
                for profile in (plan.worker, plan.evaluator)
            },
            "verdicts": stage_a_layer["verdicts"],
            "reservations": stage_a_layer["reservations"],
            "reservation_counters": stage_a_layer["reservation_counters"],
            "send_counters": stage_a_layer["send_counters"],
            "not_sent_counters": stage_a_layer["not_sent_counters"],
            "unknown_send_counters": stage_a_layer["unknown_send_counters"],
            "cost_status": stage_a_layer["cost_status"],
            "quota_status": stage_a_layer["quota_status"],
        }
    _validate_summary(summary)
    _validate_observed_at(generated_at)
    files = {
        "summary.json": canonical_json(summary) + "\n",
        "requests.json": canonical_json(summary["requests"]) + "\n",
        "evidence.jsonl": "".join(
            canonical_json(outcomes[record["reservation_id"]]["evidence"]) + "\n"
            for record in ordered
            if record["reservation_id"] in outcomes
        ),
    }
    hashes = {
        name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for name, content in files.items()
    }
    hashes_content = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
    all_files = {**files, "hashes.txt": hashes_content}
    _reject_symlink_path(evidence_root)
    parent = evidence_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    if evidence_root.exists():
        if evidence_root.is_symlink() or not evidence_root.is_dir():
            raise GovernanceError("gate2_r9_evidence_symlink_rejected")
        for entry in evidence_root.iterdir():
            if (
                entry.is_symlink()
                or not entry.is_dir()
                or _SNAPSHOT_DIR_NAME.fullmatch(entry.name) is None
            ):
                raise GovernanceError("gate2_r9_evidence_entry_rejected")
    else:
        evidence_root.mkdir()
    snapshot_dir = evidence_root / snapshot_id
    if snapshot_dir.exists():
        if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
            raise GovernanceError("gate2_r9_evidence_entry_rejected")
        snapshot_entries = list(snapshot_dir.iterdir())
        for item in snapshot_entries:
            if item.is_symlink() or not item.is_file() or item.name not in all_files:
                raise GovernanceError("gate2_r9_evidence_entry_rejected")
        existing = {
            item.name: item.read_text(encoding="utf-8") for item in snapshot_entries
        }
        if set(existing) != set(all_files):
            raise GovernanceError("gate2_r9_evidence_root_conflict")
        if existing == all_files:
            return {
                "status": "already_published",
                "snapshot_id": snapshot_id,
                "files": sorted(all_files),
                "hashes": hashes,
            }
        raise GovernanceError("gate2_r9_evidence_root_conflict")
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=evidence_root))
    try:
        for name, content in all_files.items():
            (staging / name).write_text(content, encoding="utf-8")
        for name in all_files:
            descriptor = os.open(staging / name, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.rename(staging, snapshot_dir)
        directory = os.open(evidence_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "status": "published",
        "snapshot_id": snapshot_id,
        "files": sorted(all_files),
        "hashes": hashes,
    }


def synthetic_pass_result(profile: ProviderProfile, stage: Stage) -> TransportResult:
    """Deterministic synthetic passing fixture; never a real provider response."""
    if stage is Stage.A:
        response: dict[str, Any] = {
            "model": profile.model_id,
            "choices": [
                {
                    "message": {"content": canonical_json(_STAGE_A_EXPECTED_OUTPUT)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 96, "completion_tokens": 24, "total_tokens": 120},
        }
    else:
        response = {
            "model": profile.model_id,
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": TOOL_NAME,
                                    "arguments": canonical_json(TOOL_ARGUMENTS),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 110, "completion_tokens": 18, "total_tokens": 128},
        }
    return TransportResult(
        request_sent=True,
        http_status=200,
        response_json=response,
        evidence_origin=EvidenceOrigin.SYNTHETIC_ONLY,
        time_to_headers_ms=120.0,
        total_latency_ms=450.0,
        connect_timeout_s=CONNECT_TIMEOUT_SECONDS,
        total_timeout_s=TOTAL_TIMEOUT_SECONDS,
        final_host=profile.official_host,
        tls_hostname_verified=True,
        provider_cost_usd=0.01,
        provider_quota_fact="within_limit",
    )


def synthetic_pass_results(stage: Stage) -> dict[str, TransportResult]:
    return {
        profile.provider_id: synthetic_pass_result(profile, stage)
        for profile in registered_profiles()
    }


def selfcheck() -> dict[str, Any]:
    plan = default_gate_plan()
    validate_gate_plan(plan)
    profiles = []
    for profile in (plan.worker, plan.evaluator):
        for stage in (Stage.A, Stage.B):
            validate_frozen_request(profile.request(stage))
        profiles.append(
            {
                "provider_id": profile.provider_id,
                "profile_version": profile.profile_version,
                "role": profile.role.value,
                "model_family": profile.model_family,
                "official_host": profile.official_host,
                "model_id": profile.model_id,
                "stage_a_sha256": profile.request(Stage.A).sha256,
                "stage_b_sha256": profile.request(Stage.B).sha256,
            }
        )
    return {
        "status": "ok",
        "contract_version": CONTRACT_VERSION,
        "state": "offline_implementation_candidate_r9",
        "live_authorized": False,
        "send_counters": {
            "glm47_stage_a": 0,
            "glm47_stage_b": 0,
            "deepseek_v4_pro_stage_a": 0,
            "deepseek_v4_pro_stage_b": 0,
        },
        "gate2_passed": False,
        "frozen_request_check": "pass",
        "profiles": profiles,
    }
