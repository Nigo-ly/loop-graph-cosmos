"""Loopback-only stdlib HTTP server for the P2A Control API (contract v1).

Security posture (contract §"Transport boundary"):
- binds 127.0.0.1 only; non-loopback clients are dropped without a response;
- Host must be exactly 127.0.0.1:<port> or localhost:<port>, else 403;
- POST exists only on /control/v1/intents and requires
  ``Content-Type: application/json``, a body of at most 16 KiB,
  ``X-Loop-Control-Intent: 1``, and a trusted Obsidian context (no browser
  Origin; only an absent Origin or ``app://obsidian.md`` is accepted);
- no CORS allow headers are ever emitted;
- errors are stable codes only — never stack traces, SQL, or file content.
"""

from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from common.checkpoint import SQLiteCheckpointStore
from common.loopback_http import (
    QuietLogMixin,
    close_without_response,
    host_header_allowed,
    is_loopback_peer,
    parse_json,
    read_bounded_body,
)

from . import CONTRACT_VERSION, SERVICE_VERSION
from .intents import SQLiteIntentLedger
from .service import ControlError, ControlService, RegisteredLoop

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5680

TRUSTED_ORIGIN = "app://obsidian.md"
MAX_BODY_BYTES = 16 * 1024
BODY_READ_TIMEOUT_SECONDS = 2.0

DEFAULT_INTENTS_LIMIT = 20
MAX_INTENTS_LIMIT = 100

ENVELOPE_KEYS = ("contract_version", "generated_at", "service_version", "data", "error")


def _iso_now() -> str:
    """Local ISO-8601 with numeric offset, second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_server(
    loop_db_path: str,
    ledger_path: str,
    *,
    registry: dict[str, RegisteredLoop] | None = None,
    trusted_requester: str = "nigo",
    allowed_loop_ids: frozenset[str] | None = None,
    require_trusted_origin: bool = True,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Build the Control API server.

    ``require_trusted_origin`` defaults to True: every mutating request must
    carry exactly ``Origin: app://obsidian.md``. Setting it to False is an
    explicit test/development mode and must never be used in production.
    """
    service = ControlService(
        SQLiteCheckpointStore(loop_db_path),
        SQLiteIntentLedger(ledger_path),
        registry,
        trusted_requester=trusted_requester,
        allowed_loop_ids=allowed_loop_ids,
    )
    # Safely re-drive any intents interrupted before a terminal state.
    service.recover()

    class ControlHandler(QuietLogMixin, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"control-api/{SERVICE_VERSION}"

        # -- helpers ------------------------------------------------------

        def _envelope(self) -> dict[str, Any]:
            return {
                "contract_version": CONTRACT_VERSION,
                "generated_at": _iso_now(),
                "service_version": SERVICE_VERSION,
                "data": None,
                "error": None,
            }

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_data(self, status: int, data: Any) -> None:
            body = self._envelope()
            body["data"] = data
            self._send_json(status, body)

        def _send_error(self, status: int, code: str, message: str) -> None:
            body = self._envelope()
            body["error"] = {"code": code, "message": message}
            self._send_json(status, body)

        def _host_allowed(self) -> bool:
            return host_header_allowed(self.headers, self.server.server_address)

        # -- dispatch -----------------------------------------------------

        def _dispatch(self) -> None:
            if not is_loopback_peer(self.client_address[0]):
                # Non-loopback client: close without any response.
                close_without_response(self)
                return
            if not self._host_allowed():
                # Every rejection that happens before the body is read must
                # close the connection, or the unread body (e.g. chunked
                # transfer) is parsed as the next request and poisons the
                # keep-alive stream.
                self.close_connection = True
                self._send_error(403, "forbidden", "Host header is not allowed")
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if self.command == "GET":
                    self._dispatch_get(path, query)
                elif self.command == "POST":
                    self._dispatch_post(path)
                else:
                    self.close_connection = True
                    self._send_error(405, "method_not_allowed", "Method not allowed")
            except BrokenPipeError:
                self.close_connection = True
            except ControlError as error:
                self._send_error(error.http_status, error.code, error.message)
            except Exception:  # never leak internals
                self._send_error(500, "internal_error", "Control request failed")

        do_GET = _dispatch  # noqa: N815
        do_POST = _dispatch  # noqa: N815
        do_PUT = _dispatch  # noqa: N815
        do_DELETE = _dispatch  # noqa: N815
        do_PATCH = _dispatch  # noqa: N815
        do_HEAD = _dispatch  # noqa: N815
        do_OPTIONS = _dispatch  # noqa: N815

        # -- GET routes ---------------------------------------------------

        def _dispatch_get(self, path: str, query: dict[str, list[str]]) -> None:
            if path == "/control/v1/health":
                self._send_data(
                    200,
                    {
                        "status": "ok",
                        "executor_mode": "synchronous",
                        "trusted_requester": service.trusted_requester,
                        "require_trusted_origin": require_trusted_origin,
                        "allowed_loop_ids": (
                            sorted(service.allowed_loop_ids)
                            if service.allowed_loop_ids is not None
                            else None
                        ),
                    },
                )
                return
            if path.startswith("/control/v1/actions/"):
                run_id = unquote(path[len("/control/v1/actions/") :])
                if not run_id or "/" in run_id:
                    self._send_error(404, "not_found", "Unknown resource")
                    return
                actions = service.actions_for(run_id)
                if actions is None:
                    self._send_error(404, "run_not_found", "Unknown run_id")
                    return
                self._send_data(200, actions)
                return
            if path == "/control/v1/intents":
                run_ids = query.get("run_id")
                if not run_ids or not run_ids[0]:
                    self._send_error(400, "missing_run_id", "run_id query parameter required")
                    return
                service.assert_run_allowed(run_ids[0])
                limit = DEFAULT_INTENTS_LIMIT
                raw_limits = query.get("limit")
                if raw_limits:
                    try:
                        limit = int(raw_limits[0], 10)
                    except (TypeError, ValueError):
                        self._send_error(400, "invalid_limit", "limit must be an integer")
                        return
                    if limit < 1:
                        self._send_error(400, "invalid_limit", "limit must be >= 1")
                        return
                    limit = min(limit, MAX_INTENTS_LIMIT)
                self._send_data(200, {"items": service.ledger.list_for_run(run_ids[0], limit)})
                return
            if path.startswith("/control/v1/intents/"):
                intent_id = unquote(path[len("/control/v1/intents/") :])
                if not intent_id or "/" in intent_id:
                    self._send_error(404, "not_found", "Unknown resource")
                    return
                receipt = service.ledger.get(intent_id)
                if receipt is None:
                    self._send_error(404, "intent_not_found", "Unknown intent_id")
                    return
                service.assert_run_allowed(str(receipt["run_id"]))
                self._send_data(200, receipt)
                return
            self._send_error(404, "not_found", "Unknown resource")

        # -- POST routes --------------------------------------------------

        def _dispatch_post(self, path: str) -> None:
            # Every rejection in this method happens before the body is
            # consumed; all of them must close the connection so an unread
            # body never desynchronizes the keep-alive stream.
            if path != "/control/v1/intents":
                self.close_connection = True
                self._send_error(404, "not_found", "Unknown resource")
                return
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self.close_connection = True
                self._send_error(415, "unsupported_media_type", "application/json required")
                return
            if self.headers.get("X-Loop-Control-Intent") != "1":
                self.close_connection = True
                self._send_error(403, "missing_intent_header", "X-Loop-Control-Intent: 1 required")
                return
            origin = self.headers.get("Origin")
            if require_trusted_origin:
                if origin != TRUSTED_ORIGIN:
                    self.close_connection = True
                    code = "missing_origin" if origin is None else "forbidden_origin"
                    self._send_error(403, code, "Trusted Obsidian origin required")
                    return
            elif origin is not None and origin != TRUSTED_ORIGIN:
                self.close_connection = True
                self._send_error(403, "forbidden_origin", "Untrusted request origin")
                return
            body_status, raw = read_bounded_body(
                self.headers,
                self.rfile,
                max_bytes=MAX_BODY_BYTES,
                connection=self.connection,
                timeout_seconds=BODY_READ_TIMEOUT_SECONDS,
            )
            if body_status == "missing":
                self.close_connection = True
                self._send_error(400, "missing_content_length", "Content-Length required")
                return
            if body_status in {"invalid", "transfer_encoding"}:
                self.close_connection = True
                self._send_error(400, "missing_content_length", "Content-Length required")
                return
            if body_status == "too_large":
                # The oversized body is never read; close the connection so a
                # keep-alive client cannot desynchronize the request stream.
                self.close_connection = True
                self._send_error(413, "body_too_large", "Body exceeds 16 KiB")
                return
            if body_status == "timeout":
                self.close_connection = True
                self._send_error(408, "body_read_timeout", "Request body timed out")
                return
            if body_status == "truncated":
                self.close_connection = True
                self._send_error(400, "body_truncated", "Request body was truncated")
                return
            assert raw is not None
            ok, body = parse_json(raw)
            if not ok:
                self._send_error(400, "bad_json", "Body is not valid JSON")
                return
            status, receipt = service.submit(body)
            self._send_data(status, receipt)

    class ControlHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self) -> None:
            self.service = service
            super().__init__((host, port), ControlHandler)

    return ControlHTTPServer()
