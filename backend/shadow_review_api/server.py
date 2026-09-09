"""Loopback-only stdlib HTTP server for the Shadow Review API (contract v1).

Security posture:
- binds 127.0.0.1 only; non-loopback clients are dropped without a response;
- Host must be exactly 127.0.0.1:<port> or localhost:<port>, else 403;
- GET for reads; POST exists only on /review/v1/decisions and requires
  ``Content-Type: application/json``, a body of at most 16 KiB,
  ``X-Loop-Review-Intent: 1``, and exactly ``Origin: app://obsidian.md``;
- no CORS allow headers are ever emitted;
- keep-alive safety: every rejection before the body is read closes the
  connection, and any request with a body is answered then closed;
- errors are stable codes only — never stack traces, SQL, or file content.
"""

from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from . import CONTRACT_VERSION, SERVICE_VERSION
from .ledger import SQLiteReviewLedger
from .service import ReviewError, actions_for, submit_decision

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5682

LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
TRUSTED_ORIGIN = "app://obsidian.md"
MAX_BODY_BYTES = 16 * 1024

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _iso_now() -> str:
    """Local ISO-8601 with numeric offset, second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_server(
    proposal_ledger_path: str,
    review_ledger: SQLiteReviewLedger,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    decided_by: str = "nigo",
    require_trusted_origin: bool = True,
) -> ThreadingHTTPServer:
    """Build the review server.

    ``require_trusted_origin`` defaults to True: every mutating request
    must carry exactly ``Origin: app://obsidian.md``. Setting it to False
    is an explicit test/development mode and must never be used in
    production.
    """

    class ShadowReviewHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"shadow-review-api/{SERVICE_VERSION}"

        # -- helpers ------------------------------------------------------

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _envelope(self) -> dict[str, Any]:
            return {
                "contract_version": CONTRACT_VERSION,
                "generated_at": _iso_now(),
                "service_version": SERVICE_VERSION,
            }

        def _send_json(
            self,
            status: int,
            body: dict[str, Any],
            headers: dict[str, str] | None = None,
        ) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _send_data(self, status: int, data: Any) -> None:
            body = self._envelope()
            body["data"] = data
            self._send_json(status, body)

        def _send_error(
            self,
            status: int,
            code: str,
            message: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            body = self._envelope()
            body["error"] = {"code": code, "message": message}
            self._send_json(status, body, headers=headers)

        def _host_allowed(self) -> bool:
            port = cast(tuple[str, int], self.server.server_address)[1]
            return self.headers.get("Host") in (
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            )

        # -- dispatch -----------------------------------------------------

        def _dispatch(self) -> None:
            if self.client_address[0] not in LOOPBACK_CLIENTS:
                # Non-loopback client: close without any response.
                self.close_connection = True
                return
            content_length_header = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding") or content_length_header not in (
                None,
                "",
                "0",
            ):
                # Keep-alive safety: whenever a body is present, close
                # after responding so a stray or unread body can never be
                # parsed as the next request on this connection.
                self.close_connection = True
            if not self._host_allowed():
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
                    allow = "GET, POST" if path == "/review/v1/decisions" else "GET"
                    self.close_connection = True
                    self._send_error(
                        405,
                        "method_not_allowed",
                        "Method not allowed",
                        headers={"Allow": allow},
                    )
            except BrokenPipeError:
                self.close_connection = True
            except ReviewError as error:
                self._send_error(error.http_status, error.code, error.message)
            except Exception:  # never leak internals
                self._send_error(500, "internal_error", "Review request failed")

        # BaseHTTPRequestHandler dispatches on these exact method names.
        do_GET = _dispatch  # noqa: N815
        do_POST = _dispatch  # noqa: N815
        do_PUT = _dispatch  # noqa: N815
        do_DELETE = _dispatch  # noqa: N815
        do_PATCH = _dispatch  # noqa: N815
        do_HEAD = _dispatch  # noqa: N815
        do_OPTIONS = _dispatch  # noqa: N815

        # -- GET routes ---------------------------------------------------

        def _dispatch_get(self, path: str, query: dict[str, list[str]]) -> None:
            if path == "/review/v1/health":
                self._send_data(200, self._health())
            elif path == "/review/v1/decisions":
                self._handle_decisions_list(query)
            elif path.startswith("/review/v1/decisions/"):
                decision_id = unquote(path[len("/review/v1/decisions/") :])
                if not decision_id or "/" in decision_id:
                    self._send_error(404, "not_found", "Unknown resource")
                else:
                    self._handle_decision_detail(decision_id)
            elif path.startswith("/review/v1/actions/"):
                proposal_id = unquote(path[len("/review/v1/actions/") :])
                if not proposal_id or "/" in proposal_id:
                    self._send_error(404, "not_found", "Unknown resource")
                else:
                    self._send_data(
                        200, actions_for(proposal_ledger_path, review_ledger, proposal_id)
                    )
            else:
                self._send_error(404, "not_found", "Unknown resource")

        def _health(self) -> dict[str, Any]:
            import os

            proposal_present = os.path.isfile(proposal_ledger_path)
            review_present = os.path.isfile(review_ledger.path)
            return {
                "provider_healthy": True,
                "proposal_ledger_present": proposal_present,
                "review_ledger_present": review_present,
            }

        def _handle_decisions_list(self, query: dict[str, list[str]]) -> None:
            proposal_values = query.get("proposal_id")
            limit = DEFAULT_LIMIT
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
                limit = min(limit, MAX_LIMIT)
            if proposal_values:
                items = review_ledger.history_for(proposal_values[0], limit)
                self._send_data(200, {"items": items})
            else:
                self._send_data(200, {"current": review_ledger.current_map()})

        def _handle_decision_detail(self, decision_id: str) -> None:
            receipt = review_ledger.get(decision_id)
            if receipt is None:
                self._send_error(404, "not_found", f"Unknown decision_id: {decision_id}")
                return
            self._send_data(200, receipt)

        # -- POST route ---------------------------------------------------

        def _dispatch_post(self, path: str) -> None:
            if path != "/review/v1/decisions":
                self.close_connection = True
                self._send_error(
                    405,
                    "method_not_allowed",
                    "POST is only allowed on /review/v1/decisions",
                    headers={"Allow": "GET"},
                )
                return
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type != "application/json":
                self.close_connection = True
                self._send_error(415, "unsupported_media_type", "application/json required")
                return
            if self.headers.get("X-Loop-Review-Intent") != "1":
                self.close_connection = True
                self._send_error(403, "missing_intent_header", "X-Loop-Review-Intent: 1 required")
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
                self._send_error(403, "forbidden_origin", "Untrusted origin")
                return
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self.close_connection = True
                self._send_error(400, "missing_content_length", "Content-Length required")
                return
            try:
                length = int(raw_length)
            except ValueError:
                self.close_connection = True
                self._send_error(400, "invalid_content_length", "Content-Length must be an integer")
                return
            if length < 0:
                # A negative length would make rfile.read(-1) wait for EOF
                # and block this handler thread. Reject immediately and
                # close so the connection can never be desynchronized.
                self.close_connection = True
                self._send_error(400, "invalid_content_length", "Content-Length must be >= 0")
                return
            if length > MAX_BODY_BYTES:
                # The oversized body is never read; close the connection
                # so a keep-alive client cannot desynchronize the stream.
                self.close_connection = True
                self._send_error(413, "body_too_large", "Body exceeds 16 KiB")
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_error(400, "invalid_json", "Request body must be valid JSON")
                return
            if not isinstance(payload, dict):
                self._send_error(400, "invalid_json", "Request body must be a JSON object")
                return

            receipt, is_new = submit_decision(
                proposal_ledger_path, review_ledger, payload, decided_by=decided_by
            )
            self._send_data(202 if is_new else 200, receipt)

    class ShadowReviewHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self) -> None:
            super().__init__((host, port), ShadowReviewHandler)

    return ShadowReviewHTTPServer()
