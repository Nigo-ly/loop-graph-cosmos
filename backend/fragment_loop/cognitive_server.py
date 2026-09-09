"""Loopback research, decision and knowledge API.

Public entrypoint: explicit local vault; collection and model use default off.
Legacy cognitive decision routes remain for checkpoint compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from common.checkpoint import SQLiteCheckpointStore
from common.loopback_http import parse_json, read_bounded_body
from fragment_loop.cognitive_loop import (
    CognitiveDecision,
    CognitiveDecisionError,
    SyntheticCognitiveLoop,
)
from fragment_loop.cognitive_note import (
    CognitiveNotePublishError,
    CognitiveNoteWithdrawalError,
    CognitiveNoteWithdrawalOutcome,
)
from fragment_loop.continuation_bridge import (
    CONTINUATION_HEADER,
    ContinuationBridgeError,
    FragmentContinuationBridge,
)
from fragment_loop.governed_research import GovernedResearchError
from fragment_loop.intent_service import (
    ALIGNMENT_DECISION_HEADER,
    ALIGNMENT_ESCALATION_HEADER,
    ALIGNMENT_HEADER,
    ALIGNMENTS_PATH,
    CASES_PATH,
    EPISODE_CONTINUATION_HEADER,
    EPISODES_PATH,
    FragmentIntentError,
    FragmentIntentService,
)
from fragment_loop.product_review import ProductReviewError, ProductReviewService
from fragment_loop.research_fetch import SearchTransport
from graph_runtime.pilot_bridge import PilotBridge, PilotBridgeError, pilot_plan
from graph_runtime.pilot_execution import PilotExecution, PilotExecutionError
from graph_runtime.research_bridge import (
    ResearchBridge,
    ResearchBridgeError,
    ResearchExecution,
)
from graph_runtime.runtime import GraphDecisionError, GraphRuntimeError
from graph_runtime.service import GraphService

CONTRACT_VERSION = "2"
SERVICE_VERSION = "fragment-cognitive-decision-r1ob-product-review-continuation-v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5684
TRUSTED_ORIGIN = "app://obsidian.md"
DECISION_HEADER = "X-Fragment-Cognitive-Decision"
WITHDRAWAL_HEADER = "X-Fragment-Cognitive-Withdrawal"
DECISIONS_PATH = "/fragment-cognitive/v1/decisions"
WITHDRAWALS_PATH = "/fragment-cognitive/v1/withdrawals"
PRODUCT_REVIEWS_PATH = "/fragment/v1/product-reviews"
CONTINUATIONS_PATH = "/fragment/v1/continuations"
PRODUCT_DECISION_HEADER = "X-Loop-Product-Decision"
PRODUCT_WITHDRAWAL_HEADER = "X-Loop-Product-Withdrawal"
GRAPH_RUNS_PATH = "/graph/v1/runs"
GRAPH_DECISION_HEADER = "X-Graph-Human-Decision"
GRAPH_REOPEN_HEADER = "X-Graph-Node-Reopen"
GRAPH_RUN_CREATE_HEADER = "X-Graph-Run-Create"
GRAPH_RECEIPT_HEADER = "X-Graph-Authorization-Receipt"
GRAPH_RETRY_HEADER = "X-Graph-Pilot-Retry"
GRAPH_PILOT_PLAN_PATH = "/graph/v1/pilot-plans/fragment-pilot-v1"
GRAPH_RESEARCH_RUNS_PATH = "/graph/v1/research-runs"
GRAPH_RESEARCH_RUN_CREATE_HEADER = "X-Graph-Research-Run-Create"
GRAPH_RESEARCH_RECEIPT_HEADER = "X-Graph-Research-Authorization-Receipt"
MAX_BODY_BYTES = 8 * 1024
BODY_READ_TIMEOUT_SECONDS = 2.0
LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
BODY_KEYS = {
    "decision",
    "run_id",
    "fragment_id",
    "markdown_sha256",
    "expected_sequence",
    "idempotency_key",
    "requester",
    "thought_category",
}
WITHDRAWAL_BODY_KEYS = {
    "action",
    "run_id",
    "fragment_id",
    "expected_sequence",
    "requester",
    "withdrawal_id",
}
PRODUCT_BODY_KEYS = {
    "action",
    "candidate_id",
    "fragment_ref",
    "content_sha256",
    "expected_revision",
    "selected_card_ids",
    "requester",
    "decision_id",
}
GRAPH_DECISION_BODY_KEYS = {
    "run_id",
    "node_id",
    "decision",
    "spec_digest",
    "input_digest",
    "expected_sequence",
    "requester",
    "decision_id",
}
_PILOT_CREATE_STATUS = {
    "invalid_body": 400,
    "spec_not_allowed": 403,
    "plan_expired": 409,
    "fragment_not_trusted": 404,
    "candidate_not_ready": 409,
    "candidate_source_unavailable": 503,
    "already_bridged": 409,
    "candidate_input_over_limit": 409,
    "unsafe_prefix": 500,
}
_PILOT_RECEIPT_STATUS = {
    "not_a_pilot_run": 404,
    "gate_not_pending": 409,
    "reservation_exists": 409,
    "registration_invalid": 409,
    "candidate_version_drift": 409,
    "candidate_source_unavailable": 503,
    "authorization_still_valid": 409,
    "price_unavailable": 503,
    "cost_cap_exceeded": 409,
    "cas_conflict": 409,
}
_PILOT_DECIDE_STATUS = {
    "candidate_source_unavailable": 503,
}
_PILOT_RETRY_STATUS = {
    "not_a_pilot_run": 404,
    "retry_not_allowed": 409,
    "retry_never_resend": 409,
    "authorization_missing": 409,
    "authorization_digest_mismatch": 409,
    "authorization_expired": 409,
    "spec_digest_mismatch": 409,
    "input_digest_mismatch": 409,
    "sequence_mismatch": 409,
    "invalid_requester": 403,
    "retry_id_mismatch": 409,
    "candidate_version_drift": 409,
    "candidate_source_unavailable": 503,
}
_RESEARCH_CREATE_STATUS = {
    "invalid_body": 400,
    "spec_not_allowed": 403,
    "plan_expired": 409,
    "lineage_not_found": 404,
    "lineage_drift": 409,
    "escalation_not_available": 409,
    "escalation_binding_changed": 409,
    "evidence_bundle_drift": 409,
    "already_bridged": 409,
    "unsafe_prefix": 500,
}
GRAPH_REOPEN_BODY_KEYS = {
    "run_id",
    "node_id",
    "expected_sequence",
    "requester",
    "reason",
    "reopen_id",
}
FORBIDDEN_CATEGORY_CHARACTERS = ("\n", "\r", "\x1f")

# Internal sentinel distinguishing "a fixed error response was already sent"
# from any legitimately decoded JSON value — including JSON ``null``, which
# decodes to ``None`` and must still reach body validation.
_ERROR_RESPONSE_SENT = object()


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def decision_idempotency_key(body: dict[str, Any]) -> str:
    canonical = "\x1f".join(
        (
            str(body["requester"]),
            str(body["decision"]),
            str(body["run_id"]),
            str(body["fragment_id"]),
            str(body["markdown_sha256"]),
            str(body["expected_sequence"]),
            str(body["thought_category"]),
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def withdrawal_idempotency_key(body: dict[str, Any]) -> str:
    """The fixed canonical material of one withdrawal: nothing else is bound."""
    canonical = "\x1f".join(
        (
            str(body["requester"]),
            str(body["action"]),
            str(body["run_id"]),
            str(body["fragment_id"]),
            str(body["expected_sequence"]),
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validated_thought_category(raw: object, decision: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("invalid_thought_category")
    if decision == "reject":
        if raw != "":
            raise ValueError("invalid_thought_category")
        return ""
    if any(character in raw for character in FORBIDDEN_CATEGORY_CHARACTERS):
        raise ValueError("invalid_thought_category")
    category = raw.strip()
    if not category or len(category) > 128:
        raise ValueError("invalid_thought_category")
    return category


def _validated_run_and_fragment(raw: dict[str, Any]) -> None:
    for field in ("run_id", "fragment_id"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x1f" in value:
            raise ValueError(f"invalid_{field}")


def _validated_expected_sequence(raw: object) -> None:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise ValueError("invalid_expected_sequence")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_body(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != BODY_KEYS:
        raise ValueError("invalid_body")
    if raw.get("decision") not in {"keep_draft", "reject"}:
        raise ValueError("invalid_decision")
    _validated_run_and_fragment(raw)
    if not _is_sha256(raw.get("markdown_sha256")):
        raise ValueError("invalid_markdown_sha256")
    _validated_expected_sequence(raw.get("expected_sequence"))
    if raw.get("requester") != "nigo":
        raise ValueError("invalid_requester")
    raw["thought_category"] = _validated_thought_category(
        raw.get("thought_category"), raw.get("decision")
    )
    idempotency_key = raw.get("idempotency_key")
    if not _is_sha256(idempotency_key) or idempotency_key != decision_idempotency_key(raw):
        raise ValueError("invalid_idempotency_key")
    return dict(raw)


def _validated_withdrawal_body(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != WITHDRAWAL_BODY_KEYS:
        raise ValueError("invalid_body")
    if raw.get("action") != "withdraw":
        raise ValueError("invalid_action")
    _validated_run_and_fragment(raw)
    _validated_expected_sequence(raw.get("expected_sequence"))
    if raw.get("requester") != "nigo":
        raise ValueError("invalid_requester")
    withdrawal_id = raw.get("withdrawal_id")
    if not _is_sha256(withdrawal_id) or withdrawal_id != withdrawal_idempotency_key(raw):
        raise ValueError("invalid_withdrawal_id")
    return dict(raw)


def _validated_graph_run_id(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("invalid_run_id")
    value = unquote(raw)
    if (
        not value.startswith("exec:")
        or len(value) > 256
        or any(
            ord(character) <= 0x20 or ord(character) == 0x7F or character in ("/", "\\")
            for character in value
        )
    ):
        raise ValueError("invalid_run_id")
    return value


def _validated_graph_node_id(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("invalid_node_id")
    value = unquote(raw)
    if (
        not value
        or len(value) > 128
        or any(character in value for character in ("\x1f", " ", "\n", "\r", "\t", "/", "\\", ":"))
    ):
        raise ValueError("invalid_node_id")
    return value


def _validated_graph_decision_body(raw: object, path_run_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid_body")
    # Pilot 双闸门的扩展绑定字段（仅 fragment-pilot-v1 必需，服务端另行核验）。
    optional_keys = {"authorization_digest", "result_digest"}
    keys = set(raw)
    if not GRAPH_DECISION_BODY_KEYS <= keys or not keys <= (
        GRAPH_DECISION_BODY_KEYS | optional_keys
    ):
        raise ValueError("invalid_body")
    if _validated_graph_run_id(raw.get("run_id")) != path_run_id:
        raise ValueError("run_id_mismatch")
    _validated_graph_node_id(raw.get("node_id"))
    if not isinstance(raw.get("decision"), str) or not raw["decision"]:
        raise ValueError("invalid_decision")
    if not _is_sha256(raw.get("spec_digest")):
        raise ValueError("invalid_spec_digest")
    if not _is_sha256(raw.get("input_digest")):
        raise ValueError("invalid_input_digest")
    _validated_expected_sequence(raw.get("expected_sequence"))
    if raw.get("requester") != "nigo":
        raise ValueError("invalid_requester")
    if not _is_sha256(raw.get("decision_id")):
        raise ValueError("invalid_decision_id")
    for optional in optional_keys:
        if optional in raw and not _is_sha256(raw.get(optional)):
            raise ValueError(f"invalid_{optional}")
    body = dict(raw)
    body["run_id"] = path_run_id
    return body


def _validated_graph_reopen_body(
    raw: object, path_run_id: str, path_node_id: str
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != GRAPH_REOPEN_BODY_KEYS:
        raise ValueError("invalid_body")
    if _validated_graph_run_id(raw.get("run_id")) != path_run_id:
        raise ValueError("run_id_mismatch")
    if _validated_graph_node_id(raw.get("node_id")) != path_node_id:
        raise ValueError("node_id_mismatch")
    _validated_expected_sequence(raw.get("expected_sequence"))
    if raw.get("requester") != "nigo":
        raise ValueError("invalid_requester")
    reason = raw.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 128
        or any(character in reason for character in FORBIDDEN_CATEGORY_CHARACTERS)
    ):
        raise ValueError("invalid_reason")
    if not _is_sha256(raw.get("reopen_id")):
        raise ValueError("invalid_reopen_id")
    body = dict(raw)
    body["run_id"] = path_run_id
    body["node_id"] = path_node_id
    return body


PILOT_RETRY_BODY_KEYS = {
    "run_id",
    "node_id",
    "spec_digest",
    "input_digest",
    "authorization_digest",
    "expected_sequence",
    "requester",
    "retry_id",
}


def _validated_pilot_retry_body(raw: object, path_run_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != PILOT_RETRY_BODY_KEYS:
        raise ValueError("invalid_body")
    if _validated_graph_run_id(raw.get("run_id")) != path_run_id:
        raise ValueError("run_id_mismatch")
    _validated_graph_node_id(raw.get("node_id"))
    for field in ("spec_digest", "input_digest", "authorization_digest", "retry_id"):
        if not _is_sha256(raw.get(field)):
            raise ValueError(f"invalid_{field}")
    _validated_expected_sequence(raw.get("expected_sequence"))
    if raw.get("requester") != "nigo":
        raise ValueError("invalid_requester")
    body = dict(raw)
    body["run_id"] = path_run_id
    return body


def make_cognitive_decision_server(
    service: SyntheticCognitiveLoop | None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    require_trusted_origin: bool = True,
    note_publisher: Callable[[str], object] | None = None,
    note_withdrawer: Callable[[str, str, int, str], CognitiveNoteWithdrawalOutcome] | None = None,
    product_review_service: ProductReviewService | None = None,
    graph_service: GraphService | None = None,
    pilot_bridge: PilotBridge | None = None,
    pilot_execution: PilotExecution | None = None,
    research_bridge: ResearchBridge | None = None,
    research_execution: ResearchExecution | None = None,
    continuation_bridge: FragmentContinuationBridge | None = None,
    intent_service: FragmentIntentService | None = None,
    knowledge_library: Any = None,
) -> ThreadingHTTPServer:
    if host != DEFAULT_HOST:
        raise ValueError("cognitive decision API must bind 127.0.0.1")

    class CognitiveDecisionHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"cognitive-decision-api/{SERVICE_VERSION}"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, data: Any, error: Any = None, meta: Any = None) -> None:
            payload = json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "generated_at": _iso_now(),
                    "service_version": SERVICE_VERSION,
                    "data": data,
                    "error": error,
                    **({"meta": meta} if meta is not None else {}),
                },
                ensure_ascii=False,
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send_json(status, None, {"code": code, "message": message})

        def _host_allowed(self) -> bool:
            bound_port = cast(tuple[str, int], self.server.server_address)[1]
            return self.headers.get("Host") in {
                f"127.0.0.1:{bound_port}",
                f"localhost:{bound_port}",
            }

        def _read_json_body(self, required_header: str) -> object:
            """The fixed gate chain shared by both resources.

            Returns the decoded JSON value (any value, including ``None``
            from a literal ``null`` body), or the ``_ERROR_RESPONSE_SENT``
            sentinel after a fixed error response has already been sent.
            """
            if (
                self.headers.get("Content-Type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                self.close_connection = True
                self._error(415, "unsupported_media_type", "application/json required")
                return _ERROR_RESPONSE_SENT
            if self.headers.get(required_header) != "1":
                self.close_connection = True
                code = (
                    "missing_decision_header"
                    if required_header == DECISION_HEADER
                    else "missing_withdrawal_header"
                    if required_header == WITHDRAWAL_HEADER
                    else "missing_product_decision_header"
                    if required_header == PRODUCT_DECISION_HEADER
                    else "missing_graph_decision_header"
                    if required_header == GRAPH_DECISION_HEADER
                    else "missing_graph_reopen_header"
                    if required_header == GRAPH_REOPEN_HEADER
                    else "missing_graph_run_create_header"
                    if required_header == GRAPH_RUN_CREATE_HEADER
                    else "missing_graph_receipt_header"
                    if required_header == GRAPH_RECEIPT_HEADER
                    else "missing_graph_retry_header"
                    if required_header == GRAPH_RETRY_HEADER
                    else "missing_graph_research_run_create_header"
                    if required_header == GRAPH_RESEARCH_RUN_CREATE_HEADER
                    else "missing_graph_research_authorization_receipt_header"
                    if required_header == GRAPH_RESEARCH_RECEIPT_HEADER
                    else "missing_fragment_continuation_header"
                    if required_header == CONTINUATION_HEADER
                    else "missing_fragment_alignment_header"
                    if required_header == ALIGNMENT_HEADER
                    else "missing_fragment_alignment_decision_header"
                    if required_header == ALIGNMENT_DECISION_HEADER
                    else "missing_fragment_alignment_escalation_header"
                    if required_header == ALIGNMENT_ESCALATION_HEADER
                    else "missing_fragment_episode_continuation_header"
                    if required_header == EPISODE_CONTINUATION_HEADER
                    else "missing_product_withdrawal_header"
                )
                self._error(403, code, f"{required_header}: 1 required")
                return _ERROR_RESPONSE_SENT
            origin = self.headers.get("Origin")
            if require_trusted_origin and origin != TRUSTED_ORIGIN:
                self.close_connection = True
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return _ERROR_RESPONSE_SENT
            if not require_trusted_origin and origin not in {None, TRUSTED_ORIGIN}:
                self.close_connection = True
                self._error(403, "forbidden_origin", "Untrusted request origin")
                return _ERROR_RESPONSE_SENT
            body_status, raw_body = read_bounded_body(
                self.headers,
                self.rfile,
                max_bytes=MAX_BODY_BYTES,
                min_bytes=1,
                connection=self.connection,
                timeout_seconds=BODY_READ_TIMEOUT_SECONDS,
            )
            if body_status in {"missing", "invalid", "transfer_encoding"}:
                self.close_connection = True
                self._error(400, "invalid_content_length", "Valid Content-Length required")
                return _ERROR_RESPONSE_SENT
            if body_status in {"invalid_size", "too_large"}:
                self.close_connection = True
                self._error(413, "body_too_large", "Request body size is invalid")
                return _ERROR_RESPONSE_SENT
            if body_status == "timeout":
                self.close_connection = True
                self._error(408, "body_read_timeout", "Request body timed out")
                return _ERROR_RESPONSE_SENT
            if body_status == "truncated":
                self.close_connection = True
                self._error(400, "body_truncated", "Request body was truncated")
                return _ERROR_RESPONSE_SENT
            assert raw_body is not None
            ok, decoded = parse_json(raw_body)
            if not ok:
                self._error(400, "invalid_json", "Invalid cognitive decision request")
                return _ERROR_RESPONSE_SENT
            return decoded

        def _dispatch(self) -> None:
            if self.client_address[0] not in LOOPBACK_CLIENTS:
                self.close_connection = True
                return
            if not self._host_allowed():
                self.close_connection = True
                self._error(403, "forbidden", "Host header is not allowed")
                return
            path = urlsplit(self.path).path
            if path == "/fragment/v1/knowledge" or path.startswith("/fragment/v1/knowledge/"):
                if self.command != "GET":
                    self._error(405, "method_not_allowed", "Knowledge access is read-only")
                    return
                if self.headers.get("Origin") not in (None, TRUSTED_ORIGIN):
                    self._error(403, "forbidden", "Origin is not allowed")
                    return
                if knowledge_library is None:
                    self._error(503, "knowledge_unavailable", "Knowledge library is not configured")
                    return
                try:
                    from urllib.parse import parse_qs, unquote
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    suffix = path.removeprefix("/fragment/v1/knowledge").strip("/")
                    revision = None
                    if "revision" in query:
                        versions = query["revision"]
                        if (len(versions) != 1 or not re.fullmatch(r"[1-9][0-9]{0,8}", versions[0])
                                or not suffix or "/" in suffix
                                or suffix in ("notifications", "periods")):
                            raise ValueError("knowledge_revision_query_invalid")
                        revision = int(versions[0])
                    if suffix == "notifications":
                        data = knowledge_library.notifications(since=query.get("since", [None])[0])
                    elif suffix == "periods":
                        data = knowledge_library.period_summaries()
                    elif suffix:
                        parts = suffix.split("/")
                        data = (
                            knowledge_library.history(unquote(parts[0]))
                            if len(parts) == 2 and parts[1] == "history"
                            else knowledge_library.read(unquote(suffix), revision)
                        )
                    elif "q" in query:
                        data = knowledge_library.search(query["q"][0])
                    else:
                        data = knowledge_library.catalog()
                    self._send_json(200, data)
                except ValueError as error:
                    self._error(400, str(error), "Invalid knowledge reference or query")
                except Exception:
                    self._error(503, "knowledge_unavailable", "Knowledge read failed")
                return
            if path == GRAPH_PILOT_PLAN_PATH:
                self._dispatch_pilot_plan()
                return
            if path == GRAPH_RESEARCH_RUNS_PATH:
                self._dispatch_research_create()
                return
            if path == CONTINUATIONS_PATH:
                self._dispatch_continuation()
                return
            if path == ALIGNMENTS_PATH or path.startswith(f"{ALIGNMENTS_PATH}/"):
                self._dispatch_alignment(path)
                return
            if path.startswith(f"{EPISODES_PATH}/"):
                self._dispatch_episode(path)
                return
            if path.startswith(f"{CASES_PATH}/"):
                self._dispatch_case(path)
                return
            if path == PRODUCT_REVIEWS_PATH or path.startswith(f"{PRODUCT_REVIEWS_PATH}/"):
                self._dispatch_product(path)
                return
            if path == GRAPH_RUNS_PATH or path.startswith(f"{GRAPH_RUNS_PATH}/"):
                self._dispatch_graph(path)
                return
            if self.command != "POST":
                self.close_connection = True
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            if path not in (DECISIONS_PATH, WITHDRAWALS_PATH):
                self.close_connection = True
                self._error(404, "not_found", "Unknown resource")
                return
            if service is None:
                self._error(404, "not_found", "Cognitive decision is not configured")
                return
            header = DECISION_HEADER if path == DECISIONS_PATH else WITHDRAWAL_HEADER
            raw = self._read_json_body(header)
            if raw is _ERROR_RESPONSE_SENT:
                return
            if path == DECISIONS_PATH:
                self._apply_decision(raw)
            else:
                self._apply_withdrawal(raw)

        def _trusted_origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if require_trusted_origin:
                return origin == TRUSTED_ORIGIN
            return origin in {None, TRUSTED_ORIGIN}

        def _dispatch_pilot_plan(self) -> None:
            """GET-only 预案投影：零外网、零模型、零写入的确定性输出。"""
            if self.command != "GET":
                self.close_connection = True
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            if pilot_bridge is None:
                self._error(404, "not_found", "Graph pilot bridge is not configured")
                return
            if not self._trusted_origin_allowed():
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            self._send_json(200, pilot_plan())

        def _dispatch_continuation(self) -> None:
            if continuation_bridge is None:
                self._error(404, "not_found", "Fragment continuation is not configured")
                return
            if not self._trusted_origin_allowed():
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            if self.command == "GET":
                self._send_json(200, continuation_bridge.list_statuses())
                return
            if self.command != "POST":
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            raw = self._read_json_body(CONTINUATION_HEADER)
            if raw is _ERROR_RESPONSE_SENT:
                return
            try:
                status, payload = continuation_bridge.continue_fragment(raw)
            except ContinuationBridgeError as error:
                statuses = {
                    "source_not_found": 404,
                    "loop_not_registered": 404,
                    "source_changed": 409,
                    "source_mismatch": 409,
                    "route_conflict": 409,
                    "candidate_conflict": 409,
                    "loop_not_settled": 409,
                    "organized_not_ready": 409,
                    "fragment_not_approved": 409,
                }
                self._error(
                    statuses.get(error.code, 400),
                    error.code,
                    "Fragment continuation rejected",
                )
                return
            self._send_json(status, payload)

        def _dispatch_alignment(self, path: str) -> None:
            if intent_service is None:
                self._error(404, "not_found", "Fragment alignment is not configured")
                return
            if not self._trusted_origin_allowed():
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            if path == ALIGNMENTS_PATH:
                if self.command == "GET":
                    # meta.auto_propose：自动承接最近一轮逐条结果（内存投影），
                    # 展示层据此如实区分「系统接管中／等待上游／接管失败／历史
                    # 碎片」，不把后台跳过或失败伪装成用户待办。
                    self._send_json(
                        200,
                        intent_service.list_alignments(),
                        meta={
                            "auto_propose": list(
                                getattr(intent_service, "last_auto_propose_report", [])
                            )
                        },
                    )
                    return
                if self.command != "POST":
                    self._error(405, "method_not_allowed", "Method not allowed")
                    return
                raw = self._read_json_body(ALIGNMENT_HEADER)
                if raw is _ERROR_RESPONSE_SENT:
                    return
                try:
                    status, payload = intent_service.propose(raw)
                except FragmentIntentError as error:
                    self._error(
                        409 if error.code.endswith(("changed", "conflict")) else 400,
                        error.code,
                        "Fragment alignment proposal rejected",
                    )
                    return
                self._send_json(status, payload)
                return

            parts = [unquote(part) for part in path[len(ALIGNMENTS_PATH) :].strip("/").split("/")]
            if len(parts) == 1 and parts[0] and self.command == "GET":
                try:
                    self._send_json(200, intent_service.get_alignment(parts[0]))
                except FragmentIntentError as error:
                    self._error(404, error.code, "Unknown alignment")
                return
            if len(parts) != 2 or not parts[0] or parts[1] not in {"decisions", "escalations"}:
                self._error(404, "not_found", "Unknown resource")
                return
            if self.command != "POST":
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            header = (
                ALIGNMENT_DECISION_HEADER
                if parts[1] == "decisions"
                else ALIGNMENT_ESCALATION_HEADER
            )
            raw = self._read_json_body(header)
            if raw is _ERROR_RESPONSE_SENT:
                return
            try:
                if parts[1] == "decisions":
                    status, payload = intent_service.decide(parts[0], raw)
                else:
                    status, payload = intent_service.escalate(parts[0], raw)
            except FragmentIntentError as error:
                statuses = {
                    "alignment_not_found": 404,
                    "alignment_changed": 409,
                    "decision_conflict": 409,
                    "alignment_not_decidable": 409,
                    "escalation_binding_changed": 409,
                    "escalation_not_available": 409,
                }
                self._error(
                    statuses.get(error.code, 400),
                    error.code,
                    "Fragment alignment action rejected",
                )
                return
            self._send_json(status, payload)

        def _dispatch_episode(self, path: str) -> None:
            if intent_service is None:
                self._error(404, "not_found", "Fragment alignment is not configured")
                return
            if not self._trusted_origin_allowed():
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            parts = [unquote(part) for part in path[len(EPISODES_PATH) :].strip("/").split("/")]
            if len(parts) != 2 or not parts[0] or parts[1] not in {"continuations", "harvest"}:
                self._error(404, "not_found", "Unknown resource")
                return
            try:
                if parts[1] == "harvest" and self.command == "GET":
                    self._send_json(200, intent_service.harvest(parts[0]))
                    return
                if parts[1] != "continuations" or self.command != "POST":
                    self._error(405, "method_not_allowed", "Method not allowed")
                    return
                raw = self._read_json_body(EPISODE_CONTINUATION_HEADER)
                if raw is _ERROR_RESPONSE_SENT:
                    return
                status, payload = intent_service.continue_episode(parts[0], raw)
                self._send_json(status, payload)
            except FragmentIntentError as error:
                status = 404 if error.code in {"episode_not_found", "parent_run_not_found"} else 409
                self._error(status, error.code, "Fragment episode action rejected")

        def _dispatch_case(self, path: str) -> None:
            if intent_service is None:
                self._error(404, "not_found", "Fragment alignment is not configured")
                return
            if not self._trusted_origin_allowed():
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            case_id = unquote(path[len(CASES_PATH) :].strip("/"))
            if not case_id or "/" in case_id:
                self._error(404, "not_found", "Unknown resource")
                return
            if self.command != "GET":
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            try:
                self._send_json(200, intent_service.case(case_id))
            except FragmentIntentError as error:
                self._error(404, error.code, "Unknown fragment case")

        def _dispatch_pilot_create(self) -> None:
            if pilot_bridge is None:
                self._error(404, "not_found", "Graph pilot bridge is not configured")
                return
            raw = self._read_json_body(GRAPH_RUN_CREATE_HEADER)
            if raw is _ERROR_RESPONSE_SENT:
                return
            try:
                status, payload = pilot_bridge.create_run(raw)
            except PilotBridgeError as error:
                self._error(
                    _PILOT_CREATE_STATUS.get(error.code, 400),
                    error.code,
                    "Graph pilot run create rejected",
                )
                return
            self._send_json(status, payload)

        def _dispatch_research_create(self) -> None:
            """POST /graph/v1/research-runs：独立 Research Creation Bridge。"""
            if research_bridge is None:
                self._error(404, "not_found", "Graph research bridge is not configured")
                return
            if self.command != "POST":
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            raw = self._read_json_body(GRAPH_RESEARCH_RUN_CREATE_HEADER)
            if raw is _ERROR_RESPONSE_SENT:
                return
            try:
                status, payload = research_bridge.create_run(raw)
            except ResearchBridgeError as error:
                self._error(
                    _RESEARCH_CREATE_STATUS.get(error.code, 400),
                    error.code,
                    "Graph research run create rejected",
                )
                return
            self._send_json(status, payload)

        def _dispatch_pilot_receipt(self, run_id: str) -> None:
            if pilot_execution is None:
                self._error(404, "not_found", "Graph pilot execution is not configured")
                return
            raw = self._read_json_body(GRAPH_RECEIPT_HEADER)
            if raw is _ERROR_RESPONSE_SENT:
                return
            if (
                not isinstance(raw, dict)
                or set(raw) != {"run_id", "requester"}
                or raw.get("run_id") != run_id
                or raw.get("requester") != "nigo"
            ):
                self._error(400, "invalid_body", "Invalid authorization receipt request")
                return
            try:
                status, payload = pilot_execution.issue_receipt(run_id)
            except KeyError:
                self._error(404, "run_not_found", "Unknown run_id")
                return
            except PilotExecutionError as error:
                self._error(
                    _PILOT_RECEIPT_STATUS.get(error.code, 409),
                    error.code,
                    "Authorization receipt rejected",
                )
                return
            self._send_json(status, payload)

        def _dispatch_pilot_retry(self, run_id: str) -> None:
            if pilot_execution is None:
                self._error(404, "not_found", "Graph pilot execution is not configured")
                return
            raw = self._read_json_body(GRAPH_RETRY_HEADER)
            if raw is _ERROR_RESPONSE_SENT:
                return
            try:
                body = _validated_pilot_retry_body(raw, run_id)
            except ValueError as error:
                self._error(400, str(error), "Invalid pilot retry request")
                return
            try:
                outcome = pilot_execution.retry_failed_agent(run_id, body)
            except KeyError:
                self._error(404, "run_not_found", "Unknown run_id")
                return
            except PilotExecutionError as error:
                self._error(
                    _PILOT_RETRY_STATUS.get(error.code, 409),
                    error.code,
                    "Pilot retry rejected",
                )
                return
            self._send_json(202, outcome)

        def _dispatch_research_receipt(self, run_id: str) -> None:
            if research_execution is None:
                self._error(404, "not_found", "Graph research execution is not configured")
                return
            raw = self._read_json_body(GRAPH_RESEARCH_RECEIPT_HEADER)
            if raw is _ERROR_RESPONSE_SENT:
                return
            if (
                not isinstance(raw, dict)
                or set(raw) != {"run_id", "requester"}
                or raw.get("run_id") != run_id
                or raw.get("requester") != "nigo"
            ):
                self._error(400, "invalid_body", "Invalid research receipt request")
                return
            try:
                status, payload = research_execution.issue_receipt(run_id)
            except KeyError:
                self._error(404, "run_not_found", "Unknown run_id")
                return
            except (ResearchBridgeError, GovernedResearchError) as error:
                self._error(409, error.code, "Research authorization receipt rejected")
                return
            self._send_json(status, payload)

        def _dispatch_graph(self, path: str) -> None:
            """Optional Graph resources: read-only projections plus the two
            bound write resources (human decisions, node reopen)."""
            if graph_service is None:
                self._error(404, "not_found", "Graph resources are not configured")
                return
            if not self._trusted_origin_allowed():
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            suffix = path[len(GRAPH_RUNS_PATH) :].strip("/")
            parts = suffix.split("/") if suffix else []
            if self.command == "GET":
                if not parts:
                    self._send_json(200, graph_service.list_runs())
                    return
                try:
                    run_id = _validated_graph_run_id(parts[0])
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph run reference")
                    return
                try:
                    if len(parts) == 1:
                        self._send_json(200, graph_service.detail(run_id))
                        return
                    if len(parts) == 2 and parts[1] == "history":
                        self._send_json(200, graph_service.history(run_id))
                        return
                    if len(parts) == 2 and parts[1] == "path":
                        self._send_json(200, graph_service.path(run_id))
                        return
                    if len(parts) == 2 and parts[1] == "affected":
                        query = urlsplit(self.path).query
                        params = dict(
                            pair.split("=", 1) for pair in query.split("&") if "=" in pair
                        )
                        node_id = _validated_graph_node_id(params.get("node_id", ""))
                        self._send_json(200, graph_service.affected(run_id, node_id))
                        return
                    if len(parts) == 2 and parts[1] == "canvas":
                        self._send_json(200, graph_service.canvas(run_id))
                        return
                except KeyError:
                    self._error(404, "not_found", "Unknown run or node")
                    return
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph query")
                    return
                self._error(404, "not_found", "Unknown graph resource")
                return
            if self.command != "POST":
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            if not parts:
                self._dispatch_pilot_create()
                return
            if len(parts) == 2 and parts[1] == "authorization-receipt":
                try:
                    run_id = _validated_graph_run_id(parts[0])
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph run reference")
                    return
                self._dispatch_pilot_receipt(run_id)
                return
            if len(parts) == 2 and parts[1] == "research-authorization-receipt":
                try:
                    run_id = _validated_graph_run_id(parts[0])
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph run reference")
                    return
                self._dispatch_research_receipt(run_id)
                return
            if len(parts) == 2 and parts[1] == "pilot-retry":
                try:
                    run_id = _validated_graph_run_id(parts[0])
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph run reference")
                    return
                self._dispatch_pilot_retry(run_id)
                return
            if len(parts) == 2 and parts[1] == "human-decisions":
                try:
                    run_id = _validated_graph_run_id(parts[0])
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph run reference")
                    return
                raw = self._read_json_body(GRAPH_DECISION_HEADER)
                if raw is _ERROR_RESPONSE_SENT:
                    return
                try:
                    body = _validated_graph_decision_body(raw, run_id)
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph decision request")
                    return
                try:
                    if research_execution is not None:
                        # Research Run 走专用决定钩子；其他 Run 经其 fallback
                        # 原样委托 pilot_execution / graph_service，行为零变化。
                        outcome = research_execution.decide(run_id, str(body["node_id"]), body)
                    elif pilot_execution is not None:
                        outcome = pilot_execution.decide(run_id, str(body["node_id"]), body)
                    else:
                        outcome = graph_service.decide(run_id, str(body["node_id"]), body)
                except KeyError:
                    self._error(404, "run_not_found", "Unknown run_id")
                    return
                except PilotExecutionError as error:
                    self._error(
                        _PILOT_DECIDE_STATUS.get(error.code, 409),
                        error.code,
                        "Pilot decision binding changed",
                    )
                    return
                except GraphDecisionError as error:
                    self._error(409, str(error), "Decision state or binding changed")
                    return
                except GraphRuntimeError as error:
                    self._error(409, str(error), "Graph run state changed")
                    return
                self._send_json(202, outcome)
                return
            if len(parts) == 4 and parts[1] == "nodes" and parts[3] == "reopen":
                try:
                    run_id = _validated_graph_run_id(parts[0])
                    node_id = _validated_graph_node_id(parts[2])
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph reopen reference")
                    return
                raw = self._read_json_body(GRAPH_REOPEN_HEADER)
                if raw is _ERROR_RESPONSE_SENT:
                    return
                try:
                    body = _validated_graph_reopen_body(raw, run_id, node_id)
                except ValueError as error:
                    self._error(400, str(error), "Invalid graph reopen request")
                    return
                try:
                    outcome = graph_service.reopen(run_id, node_id, body)
                except KeyError:
                    self._error(404, "run_not_found", "Unknown run_id")
                    return
                except GraphDecisionError as error:
                    self._error(409, str(error), "Reopen state or binding changed")
                    return
                except GraphRuntimeError as error:
                    self._error(409, str(error), "Graph run state changed")
                    return
                self._send_json(202, outcome)
                return
            self._error(404, "not_found", "Unknown graph resource")

        def _dispatch_product(self, path: str) -> None:
            if product_review_service is None:
                self._error(404, "not_found", "Product review is not configured")
                return
            if not self._trusted_origin_allowed():
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            suffix = path[len(PRODUCT_REVIEWS_PATH) :].strip("/")
            parts = suffix.split("/") if suffix else []
            if self.command == "GET":
                if not parts:
                    self._send_json(200, product_review_service.list_reviews())
                    return
                if len(parts) == 1:
                    try:
                        detail = product_review_service.detail(parts[0])
                    except ProductReviewError as error:
                        self._error(404, str(error), "Unknown or invalid candidate")
                        return
                    self._send_json(200, detail)
                    return
                self._error(404, "not_found", "Unknown product review resource")
                return
            if self.command != "POST" or len(parts) != 2:
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            candidate_id, resource = parts
            if resource not in {"decisions", "withdrawals"}:
                self._error(404, "not_found", "Unknown product review resource")
                return
            header = (
                PRODUCT_DECISION_HEADER if resource == "decisions" else PRODUCT_WITHDRAWAL_HEADER
            )
            raw = self._read_json_body(header)
            if raw is _ERROR_RESPONSE_SENT:
                return
            if not isinstance(raw, dict) or set(raw) != PRODUCT_BODY_KEYS:
                self._error(400, "invalid_body", "Invalid product review request")
                return
            try:
                outcome = (
                    product_review_service.decide(candidate_id, raw)
                    if resource == "decisions"
                    else product_review_service.withdraw(candidate_id, raw)
                )
            except ProductReviewError as error:
                self._error(409, str(error), "Product review state or binding changed")
                return
            self._send_json(
                202,
                {
                    "candidate_id": outcome.candidate_id,
                    "content_status": outcome.content_status,
                    "revision": outcome.revision,
                    "decision_id": outcome.decision_id,
                    "idempotent": outcome.idempotent,
                    "review_path": outcome.review_path,
                    "asset_paths": list(outcome.asset_paths),
                },
            )

        def _apply_decision(self, raw: object) -> None:
            if service is None:
                self._error(404, "not_found", "Cognitive decision is not configured")
                return
            try:
                body = _validated_body(raw)
            except ValueError as error:
                self._error(400, str(error), "Invalid cognitive decision request")
                return
            try:
                service.decide_bound(
                    str(body["run_id"]),
                    cast(CognitiveDecision, body["decision"]),
                    fragment_id=str(body["fragment_id"]),
                    expected_sequence=int(body["expected_sequence"]),
                    markdown_sha256=str(body["markdown_sha256"]),
                    decision_id=str(body["idempotency_key"]),
                    thought_category=str(body["thought_category"]),
                )
                if body["decision"] == "keep_draft" and note_publisher is not None:
                    note_publisher(str(body["run_id"]))
            except KeyError:
                self._error(404, "run_not_found", "Unknown run_id")
                return
            except CognitiveDecisionError:
                self._error(409, "decision_conflict", "Decision state or binding changed")
                return
            except Exception:
                self._error(500, "internal_error", "Cognitive decision request failed")
                return
            response_data = {
                "decision": body["decision"],
                "decision_id": body["idempotency_key"],
                "status": "recorded",
            }
            if body["decision"] == "keep_draft":
                response_data["thought_category"] = body["thought_category"]
            if body["decision"] == "keep_draft" and note_publisher is not None:
                response_data["note_status"] = "published"
            self._send_json(
                202,
                response_data,
            )

        def _apply_withdrawal(self, raw: object) -> None:
            if service is None:
                self._error(404, "not_found", "Cognitive decision is not configured")
                return
            try:
                body = _validated_withdrawal_body(raw)
            except ValueError as error:
                self._error(400, str(error), "Invalid cognitive withdrawal request")
                return
            if note_withdrawer is None:
                self._error(403, "withdrawal_unavailable", "Note withdrawal is not configured")
                return
            try:
                outcome = note_withdrawer(
                    str(body["run_id"]),
                    str(body["fragment_id"]),
                    int(body["expected_sequence"]),
                    str(body["withdrawal_id"]),
                )
            except KeyError:
                self._error(404, "run_not_found", "Unknown run_id")
                return
            except (
                CognitiveDecisionError,
                CognitiveNotePublishError,
                CognitiveNoteWithdrawalError,
            ):
                self._error(409, "withdrawal_conflict", "Withdrawal state or binding changed")
                return
            except Exception:
                self._error(500, "internal_error", "Cognitive withdrawal request failed")
                return
            self._send_json(
                202,
                {
                    "action": "withdraw",
                    "withdrawal_id": body["withdrawal_id"],
                    "status": outcome.status,
                    "note_status": outcome.note_status,
                    "idempotent": outcome.already_withdrawn,
                },
            )

        do_POST = _dispatch  # noqa: N815
        do_GET = _dispatch  # noqa: N815
        do_PUT = _dispatch  # noqa: N815
        do_DELETE = _dispatch  # noqa: N815
        do_PATCH = _dispatch  # noqa: N815
        do_OPTIONS = _dispatch  # noqa: N815

    return ThreadingHTTPServer((host, port), CognitiveDecisionHandler)


def _decision_only_producer(_fragment_text: str, _route: str) -> dict[str, object]:
    raise RuntimeError("decision API cannot generate cognitive content")


def build_graph_service(graph_db: str) -> GraphService:
    """5684 的 Graph 只读观察装配：Phase 1 spec + Phase 2B Pilot spec + Agent 账本。

    只注册只读投影与现有人闸 API 所需的 spec/ledger；绝不构造任何模型发送
    Transport、凭据读取或网络接缝（模型执行只能走显式一次性脚本
    ``scripts/graph_phase2b_pilot.py``）。
    """
    from graph_runtime.agent_ledger import AgentCallLedger
    from graph_runtime.agent_live_pilot import build_phase2b_pilot_spec
    from graph_runtime.fragment_cognitive import build_fragment_cognitive_specs
    from graph_runtime.specs.fragment_pilot_v1 import PILOT_SPEC
    from graph_runtime.specs.fragment_research_escalation_v1 import RESEARCH_SPEC
    from graph_runtime.specs.fragment_research_macro_v3 import RESEARCH_MACRO_V3_SPEC

    specs = build_fragment_cognitive_specs()
    pilot_spec = build_phase2b_pilot_spec()
    specs[pilot_spec.graph_id] = pilot_spec
    specs[PILOT_SPEC.graph_id] = PILOT_SPEC
    specs[RESEARCH_SPEC.graph_id] = RESEARCH_SPEC
    # rev8 P0-1：生产 GraphService 同步注册宏 v3（保留 v1）——
    # ResearchExecution.decide 对 v3 的授权/候选决定才能按
    # graph_id/spec_digest 载入 spec；未知 digest 仍失败关闭。
    specs[RESEARCH_MACRO_V3_SPEC.graph_id] = RESEARCH_MACRO_V3_SPEC
    return GraphService(
        SQLiteCheckpointStore(graph_db),
        specs,
        ledger=AgentCallLedger(graph_db),
    )


def scan_public_vault_intents(*, vault_root, continuation_bridge, intent_service):
    """Register opted-in captures before the existing bounded intent scanner."""
    from scripts.register_nigo_loops import scan_fragments
    from fragment_loop.intent_service import propose_organized_vault_intents

    scan_fragments(vault_root / "Notes/散记/碎片想法", continuation_bridge.store.path,
                   min_age_seconds=2)
    return propose_organized_vault_intents(
        vault_root=vault_root, continuation_bridge=continuation_bridge,
        intent_service=intent_service,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragment_loop.cognitive_server",
        description="Serve local research, decision and knowledge APIs (developer preview)",
    )
    parser.add_argument("--db", help="existing synthetic checkpoint DB")
    parser.add_argument(
        "--notes-dir",
        help="existing local directory for user-kept cognitive result notes",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--product-candidates-dir")
    parser.add_argument("--product-assets-dir")
    parser.add_argument("--vault-root")
    parser.add_argument(
        "--graph-db",
        help="existing Graph checkpoint DB enabling the optional /graph/v1 resources",
    )
    parser.add_argument(
        "--pilot-live",
        action="store_true",
        help=(
            "enable the dormant pilot live send seam (real DeepSeek transport "
            "and Keychain credential read); default off — zero Keychain, zero network"
        ),
    )
    parser.add_argument(
        "--research-live",
        action="store_true",
        help=(
            "enable the dormant governed-research live seam (peer-verified DDG "
            "discovery, safe HTTPS fetch, price read and Keychain credential "
            "read); default off — collection fails closed, runs honestly "
            "blocked, model sends stay at zero"
        ),
    )
    parser.add_argument(
        "--research-collect-live",
        action="store_true",
        help=(
            "decoupled capability: enable ONLY public collection (peer-verified "
            "DDG discovery + safe HTTPS fetch); model synthesis, price read and "
            "Keychain stay off. --research-live implies both (compat locked)."
        ),
    )
    parser.add_argument(
        "--research-model-live",
        action="store_true",
        help=(
            "decoupled capability: enable ONLY model synthesis (price read + "
            "exact Receipt + Keychain credential read); public collection "
            "requires --research-live or --research-collect-live separately."
        ),
    )
    parser.add_argument(
        "--research-model-run-id",
        action="append",
        default=None,
        help=(
            "Limit enabled research synthesis to these exact Run IDs (repeatable). "
            "Does not enable models or grant a Receipt. Omit for legacy service-wide scope."
        ),
    )
    parser.add_argument(
        "--research-search-provider",
        choices=("ddg", "bing-cn"),
        default="ddg",
        help=(
            "closed-set public search provider for --research-collect-live "
            "(rev14): exactly one provider per query, never implicit "
            "multi-provider fan-out. Default ddg (compat)."
        ),
    )
    parser.add_argument(
        "--subscription-run-id",
        action="append",
        default=None,
        help="Exact research Run IDs authorized for existing Codex/K3 subscription execution",
    )
    parser.add_argument("--subscription-provider", choices=("kimi", "codex"), default="kimi")
    parser.add_argument(
        "--subscription-new-public-after", default=None,
        help="Fixed timezone-aware cutover for new opted-in public captures; default off",
    )
    args = parser.parse_args(argv)
    subscription_cutover = None
    if args.subscription_new_public_after is not None:
        try:
            subscription_cutover = datetime.fromisoformat(args.subscription_new_public_after)
            if subscription_cutover.tzinfo is None or subscription_cutover.utcoffset() is None:
                raise ValueError
        except ValueError:
            parser.error("subscription-new-public-after requires valid ISO time with timezone")
    subscription_enabled = bool(args.subscription_run_id or subscription_cutover is not None)
    if subscription_enabled and (
        args.research_live or args.research_model_live or args.pilot_live
    ):
        parser.error("subscription execution cannot be combined with paid API live flags")
    # 能力解耦映射（兼容锁定）：旧 --research-live = collection + model 全开；
    # 新开关各自独立，绝不在旧开关之外静默扩大生产权限。
    research_collect_live = bool(args.research_live or args.research_collect_live)
    research_model_live = bool(args.research_live or args.research_model_live)
    service = (
        SyntheticCognitiveLoop(SQLiteCheckpointStore(args.db), _decision_only_producer)
        if args.db
        else None
    )
    note_publisher: Callable[[str], object] | None = None
    note_withdrawer: Callable[[str, str, int, str], CognitiveNoteWithdrawalOutcome] | None = None
    if args.notes_dir:
        if service is None:
            parser.error("--notes-dir requires --db")
        decision_service = service
        from fragment_loop.cognitive_note import (
            publish_kept_cognitive_note,
            withdraw_kept_cognitive_note,
        )

        def publish_note(run_id: str) -> object:
            return publish_kept_cognitive_note(decision_service.store, run_id, args.notes_dir)

        def withdraw_note(
            run_id: str, fragment_id: str, expected_sequence: int, withdrawal_id: str
        ) -> CognitiveNoteWithdrawalOutcome:
            return withdraw_kept_cognitive_note(
                decision_service.store,
                run_id,
                args.notes_dir,
                fragment_id=fragment_id,
                expected_sequence=expected_sequence,
                withdrawal_id=withdrawal_id,
            )

        note_publisher = publish_note
        note_withdrawer = withdraw_note
    product_paths = (
        args.product_candidates_dir,
        args.product_assets_dir,
        args.vault_root,
    )
    if any(product_paths) and not all(product_paths):
        parser.error(
            "--product-candidates-dir, --product-assets-dir and --vault-root "
            "must be supplied together"
        )
    if service is None and not all(product_paths) and not args.graph_db:
        parser.error("supply --db, all three product review paths, and/or --graph-db")
    product_review_service = (
        ProductReviewService(
            args.product_candidates_dir,
            args.product_assets_dir,
            vault_root=args.vault_root,
        )
        if all(product_paths)
        else None
    )
    knowledge_library = None
    if all(product_paths):
        from fragment_loop.knowledge_library import KnowledgeLibrary
        knowledge_library = KnowledgeLibrary(args.product_assets_dir, vault_root=args.vault_root)
    if subscription_enabled and (
        not args.graph_db or not all(product_paths) or not research_collect_live
    ):
        parser.error(
            "subscription execution requires graph-db, product paths and public collection"
        )
    knowledge_consolidation = None
    graph_service = None
    if args.graph_db:
        graph_service = build_graph_service(args.graph_db)
    pilot_bridge = None
    pilot_execution = None
    continuation_bridge = None
    if graph_service is not None and product_review_service is not None:
        # Pilot 桥装配：Agent 账本复用同一 Graph DB。live 开关默认关闭
        # （rev2 §2/§5 休眠生产接缝）：未启用时零 Keychain、零网络——
        # Agent 节点在预留前以 live_disabled 失败；显式 --pilot-live 才装配
        # 真实 Transport 与精确凭据读取（当前 LaunchAgent 不安装该开关）。
        from graph_runtime.agent_ledger import AgentCallLedger
        from graph_runtime.pilot_live import (
            PilotLiveTransport,
            forbidden_credential_reader,
            live_pilot_price_reader,
            production_credential_reader,
        )

        pilot_ledger = AgentCallLedger(args.graph_db)
        pilot_bridge = PilotBridge(graph_service.store, product_review_service, pilot_ledger)
        pilot_execution = PilotExecution(
            graph_service.store,
            graph_service,
            product_review_service,
            pilot_ledger,
            PilotLiveTransport(),
            price_reader=live_pilot_price_reader,
            live_enabled=bool(args.pilot_live),
            credential_reader=(
                production_credential_reader if args.pilot_live else forbidden_credential_reader
            ),
        )
    if product_review_service is not None:
        continuation_bridge = FragmentContinuationBridge(
            product_review_service, **({"loop_db": args.db} if args.db else {}),
            prospective_public_after=subscription_cutover)
    intent_service = None
    if continuation_bridge is not None:
        from fragment_loop.intent_production import (
            PublicDiscoveryIntentAdapter,
            direct_intent_adapter,
            production_intent_resolver,
        )
        from fragment_loop.research_fetch import DDGSearchTransport

        def load_intent_source(fragment_id: str, input_digest: str) -> Mapping[str, object]:
            try:
                return continuation_bridge.load_intent_source(fragment_id, input_digest)
            except ContinuationBridgeError as error:
                raise FragmentIntentError(error.code) from error

        intent_service = FragmentIntentService(
            continuation_bridge.store,
            load_intent_source,
            production_intent_resolver,
            direct_adapter=direct_intent_adapter,
            # M5：发现出口的 live 门与研究链同开关（默认失败关闭、零网络）；
            # 能力解耦后跟随 collection 开关。
            verify_adapter=PublicDiscoveryIntentAdapter(
                DDGSearchTransport(live_enabled=research_collect_live)
            ),
            # rev10：旧离线零来源执行恢复的当前 frontmatter 重验证桥。
            continuation_bridge=continuation_bridge,
        )
    research_bridge = None
    research_execution = None
    if graph_service is not None and intent_service is not None:
        # Research 桥装配（DESIGN §5.3）：与 Pilot 桥完全分离的 allowlist 与账本
        # 行，复用同一 Graph DB。--research-live 默认关闭（休眠生产接缝）：
        # 未启用时采集失败关闭、Receipt 不签发、模型发送恒 0、零 Keychain、
        # 零网络；显式开启才装配 peer 核验 DDG 发现、安全 HTTPS 抓取、价格
        # 只读核验与精确凭据读取（当前 LaunchAgent 不安装该开关）。
        from fragment_loop.governed_research import (
            GovernedResearchRunner,
            GovernedResearchVerifyAdapter,
            ResearchLiveSynthesisTransport,
        )
        from graph_runtime.agent_ledger import AgentCallLedger
        from graph_runtime.pilot_live import (
            forbidden_credential_reader,
            live_pilot_price_reader,
            production_credential_reader,
        )

        # rev16：显式公共 callable 注解——两个分支各自赋 Bing/DDG 具体
        # transport 时，mypy 不再按首分支推断窄类型（运行语义不变）。
        search_transport: SearchTransport | None = None
        fetch_transport = None
        search_parser: Callable[[bytes], list[dict[str, str]]] | None = None
        search_fingerprint: str | None = None
        if research_collect_live:
            from fragment_loop.research_fetch import (
                BingCNSearchTransport,
                DDGSearchTransport,
                ResearchFetchTransport,
                parse_bing_cn_candidates,
                parse_ddg_candidates,
                search_provider_fingerprint,
            )

            # rev14：闭集单一 provider（每次查询只在选定 provider 内执行，
            # 绝不隐式多 provider 扩散）；transport/parser/fingerprint 成对。
            if args.research_search_provider == "bing-cn":
                search_transport = BingCNSearchTransport(live_enabled=True)
                search_parser = parse_bing_cn_candidates
            else:
                search_transport = DDGSearchTransport(live_enabled=True)
                search_parser = parse_ddg_candidates
            search_fingerprint = search_provider_fingerprint(args.research_search_provider)
            fetch_transport = ResearchFetchTransport(live_enabled=True)
        research_ledger = AgentCallLedger(args.graph_db)
        subscription_research = None
        if subscription_enabled:
            from fragment_loop.repository_trial import run_repository_trial
            from fragment_loop.subscription_agent import (
                CodexSubscriptionAgent,
                KimiSubscriptionAgent,
            )
            from fragment_loop.subscription_research import SubscriptionResearch

            subscription_agent = (
                KimiSubscriptionAgent()
                if args.subscription_provider == "kimi"
                else CodexSubscriptionAgent()
            )
            from fragment_loop.knowledge_consolidation import KnowledgeConsolidation
            assert knowledge_library is not None
            knowledge_consolidation = KnowledgeConsolidation(knowledge_library, subscription_agent)
            subscription_research = SubscriptionResearch(subscription_agent,
                run_ids=frozenset(args.subscription_run_id or []), library=knowledge_library,
                trial=run_repository_trial,
                prospective_eligibility=(
                    lambda run_id: intent_service.prospective_subscription_eligible(
                        run_id, subscription_cutover)
                ) if subscription_cutover is not None else None)

        def make_research_runner(store: SQLiteCheckpointStore) -> GovernedResearchRunner:
            return GovernedResearchRunner(
                store,
                live_enabled=bool(args.research_live),
                collection_enabled=research_collect_live,
                subscription_research=subscription_research,
                synthesis_enabled=research_model_live,
                synthesis_run_ids=(
                    frozenset(args.research_model_run_id)
                    if args.research_model_run_id is not None else None
                ),
                search_transport=search_transport,
                fetch_transport=fetch_transport,
                ledger=research_ledger,
                price_reader=live_pilot_price_reader if research_model_live else None,
                credential_reader=(
                    production_credential_reader
                    if research_model_live
                    else forbidden_credential_reader
                ),
                synthesis_transport=ResearchLiveSynthesisTransport(
                    live_enabled=research_model_live
                ),
                search_parser=search_parser,
                search_fingerprint=search_fingerprint,
            )

        # Lightweight verify runs are Loop executions, while escalated
        # Research Graph runs live in the Graph store.  They share the same
        # governed transports and Agent ledger, but each runner must append
        # Checkpoints to the store that owns its run_id.
        intent_research_runner = make_research_runner(intent_service.store)
        graph_research_runner = make_research_runner(graph_service.store)
        # The intent service is assembled before the Graph-backed research
        # runner because its store is also needed by the Research Bridge.
        # Replace the discovery-only compatibility adapter once the governed
        # runner exists; otherwise production ``verify`` would silently stay
        # on the historical URL-discovery path and never fetch evidence.
        intent_service.verify_adapter = GovernedResearchVerifyAdapter(intent_research_runner)
        research_bridge = ResearchBridge(
            graph_service.store,
            intent_service.store,
            research_ledger,
            graph_research_runner,
        )
        research_execution = ResearchExecution(
            graph_service.store,
            graph_service,
            research_ledger,
            graph_research_runner,
            fallback=(
                pilot_execution.decide if pilot_execution is not None else graph_service.decide
            ),
        )
    server = make_cognitive_decision_server(
        service,
        port=args.port,
        note_publisher=note_publisher,
        note_withdrawer=note_withdrawer,
        product_review_service=product_review_service,
        graph_service=graph_service,
        pilot_bridge=pilot_bridge,
        pilot_execution=pilot_execution,
        research_bridge=research_bridge,
        research_execution=research_execution,
        continuation_bridge=continuation_bridge,
        intent_service=intent_service,
        knowledge_library=knowledge_library,
    )
    watch_coordinator = None
    if intent_service is not None:
        # TASK C rev3/rev6 自主触发边界：无人打开 UI 也到期推进。最低频
        # 同进程扫描，同一线程承载 intent execution watch 与 v3 Graph
        # watch（research_execution 存在时）；collection 能力关闭时零
        # 副作用；服务关闭时干净 join。不新增服务/数据库/依赖/调度系统。
        from fragment_loop.intent_service import (
            ResearchWatchCoordinator,
            propose_organized_vault_intents,
        )

        watch_coordinator = ResearchWatchCoordinator(
            intent_service,
            knowledge_scanner=(
                knowledge_consolidation.scan_once if knowledge_consolidation else None
            ),
            graph_scanner=(
                research_execution.evaluate_due_watches if research_execution is not None else None
            ),
            # 自动承接：已勾选 nigo-loop 且整理完成的碎片，后台幂等创建
            # intent，不依赖 UI 打开；continuation_bridge 缺失时不扫描。
            auto_propose_scanner=(
                (
                    lambda: scan_public_vault_intents(
                        vault_root=continuation_bridge.review_service.vault_root,
                        continuation_bridge=continuation_bridge,
                        intent_service=intent_service,
                    )
                )
                if continuation_bridge is not None
                else None
            ),
        )
        watch_coordinator.start()
    port = int(server.server_address[1])
    print(
        f"cognitive-decision {SERVICE_VERSION} serving "
        f"http://{DEFAULT_HOST}:{port}/fragment-cognitive/v1",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if watch_coordinator is not None:
            # 在 runner 冻结上限内等待当前 cycle 安全结束；worker 仍存活
            # 时不谎报（daemon 线程随进程退出），但留下可见警告。
            if not watch_coordinator.close():
                print(
                    "cognitive-decision watch coordinator still winding down; "
                    "daemon thread exits with process",
                    flush=True,
                )
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
