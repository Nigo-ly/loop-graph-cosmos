"""Loopback-only stdlib HTTP server for the Shadow Projection API (v1).

Same security posture as the P1A projection server: binds 127.0.0.1 only
and drops non-loopback clients; Host must be exactly 127.0.0.1:<port> or
localhost:<port>; GET only (405 + ``Allow: GET`` otherwise); no CORS
headers are ever emitted.

Freshness: every request probes the ledger's max event id. ``stale`` is
true only when that probe fails after the ledger was previously readable,
or the marker regresses. A missing ledger is not stale and not an error:
``ledger_present`` is false and list endpoints serve an empty list so the
console can render "尚未生成影子建议" honestly.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from . import CONTRACT_VERSION, PROVIDER_VERSION, store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5681

LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

SCOPES = frozenset({"current", "history", "all"})

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _iso_now() -> str:
    """Local ISO-8601 with numeric offset, second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ProviderState:
    """Shared provider state: last-known marker, readability, and cache."""

    def __init__(self, ledger_path: str):
        self.ledger_path = ledger_path
        self._lock = threading.Lock()
        self._last_marker: int = 0
        self._ever_read = False
        self._last_read_at: str | None = None
        self._cache: dict[str, Any] = {}

    def fresh_marker(self) -> tuple[int, bool, bool, bool]:
        """Fresh ledger probe.

        Returns (source_event_id, ledger_present, stale, read_ok). A
        missing ledger is present=False/read_ok=False but **not** stale —
        absence is a first-class honest state. A probe failure after a
        previously successful read is stale.
        """
        present = store.ledger_present(self.ledger_path)
        if not present:
            return 0, False, False, False
        try:
            marker = store.source_marker(self.ledger_path)
        except Exception:
            with self._lock:
                return (
                    self._last_marker,
                    True,
                    self._ever_read,
                    False,
                )
        with self._lock:
            stale = self._ever_read and marker < self._last_marker
            if not stale:
                self._last_marker = marker
            self._ever_read = True
            self._last_read_at = _iso_now()
            return self._last_marker, True, stale, True

    @property
    def last_read_at(self) -> str | None:
        with self._lock:
            return self._last_read_at

    def get_cached(self, key: str) -> Any:
        with self._lock:
            return self._cache.get(key)

    def set_cached(self, key: str, payload: object) -> None:
        with self._lock:
            self._cache[key] = payload


def make_server(
    ledger_path: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    state = ProviderState(ledger_path)

    class ShadowProjectionHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"shadow-projection-api/{PROVIDER_VERSION}"

        # -- helpers ------------------------------------------------------

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _envelope(
            self, marker: int, present: bool, stale: bool
        ) -> dict[str, Any]:
            return {
                "contract_version": CONTRACT_VERSION,
                "generated_at": _iso_now(),
                "provider_version": PROVIDER_VERSION,
                "db_mode": "read_only",
                "ledger_present": present,
                "source_event_id": marker,
                "stale": stale,
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

        def _send_error(
            self,
            status: int,
            code: str,
            message: str,
            marker: int,
            present: bool,
            stale: bool,
            headers: dict[str, str] | None = None,
        ) -> None:
            body = self._envelope(marker, present, stale)
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
            content_length = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding") or content_length not in (None, "", "0"):
                # Handlers never read request bodies. Whenever a body is
                # present, close after responding so a stray body (or a
                # chunked stream) can never be parsed as the next
                # keep-alive request and poison the connection.
                self.close_connection = True
            marker, present, stale, read_ok = state.fresh_marker()
            if not self._host_allowed():
                # Every rejection that happens before the body is read must
                # close the connection, or the unread body (e.g. chunked
                # transfer) is parsed as the next request and poisons the
                # keep-alive stream.
                self.close_connection = True
                self._send_error(
                    403, "forbidden", "Host header is not allowed", marker, present, stale
                )
                return
            if self.command != "GET":
                self.close_connection = True
                self._send_error(
                    405, "method_not_allowed", "Only GET is supported",
                    marker, present, stale, headers={"Allow": "GET"},
                )
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/shadow/v1/health":
                    self._handle_health(marker, present, stale, read_ok)
                elif path == "/shadow/v1/proposals":
                    self._handle_proposals(query, marker, present, stale)
                elif path.startswith("/shadow/v1/proposals/"):
                    proposal_id = unquote(path[len("/shadow/v1/proposals/"):])
                    if not proposal_id or "/" in proposal_id:
                        self._send_error(
                            404, "not_found", "Unknown resource", marker, present, stale
                        )
                    else:
                        self._handle_proposal_detail(
                            proposal_id, marker, present, stale
                        )
                else:
                    self._send_error(
                        404, "not_found", "Unknown resource", marker, present, stale
                    )
            except BrokenPipeError:
                self.close_connection = True
            except Exception:  # projection failure: report safely, never leak internals
                self._send_error(
                    500, "internal_error", "Projection failed", marker, present, stale
                )

        # BaseHTTPRequestHandler dispatches on these exact method names.
        do_GET = _dispatch  # noqa: N815
        do_POST = _dispatch  # noqa: N815
        do_PUT = _dispatch  # noqa: N815
        do_DELETE = _dispatch  # noqa: N815
        do_PATCH = _dispatch  # noqa: N815
        do_HEAD = _dispatch  # noqa: N815
        do_OPTIONS = _dispatch  # noqa: N815

        # -- routes -------------------------------------------------------

        def _handle_health(
            self, marker: int, present: bool, stale: bool, read_ok: bool
        ) -> None:
            body = self._envelope(marker, present, stale)
            body["provider_healthy"] = read_ok
            body["last_read_at"] = state.last_read_at
            self._send_json(200, body)

        def _handle_proposals(
            self,
            query: dict[str, list[str]],
            marker: int,
            present: bool,
            stale: bool,
        ) -> None:
            scope = (query.get("scope") or ["current"])[0]
            if scope not in SCOPES:
                self._send_error(
                    400, "invalid_scope", "scope must be current, history, or all",
                    marker, present, stale,
                )
                return
            limit = DEFAULT_LIMIT
            raw_limits = query.get("limit")
            if raw_limits:
                try:
                    limit = int(raw_limits[0], 10)
                except (TypeError, ValueError):
                    self._send_error(
                        400, "invalid_limit", "limit must be an integer",
                        marker, present, stale,
                    )
                    return
                if limit < 1:
                    self._send_error(
                        400, "invalid_limit", "limit must be >= 1",
                        marker, present, stale,
                    )
                    return
                limit = min(limit, MAX_LIMIT)
            if not present:
                body = self._envelope(marker, present, stale)
                body["items"] = []
                self._send_json(200, body)
                return
            if stale:
                items = state.get_cached(f"proposals:{scope}")
                if items is None:
                    self._send_error(
                        503, "service_unavailable",
                        "Ledger read failed and no last-known data exists",
                        marker, present, stale,
                    )
                    return
            else:
                items = store.list_proposals(state.ledger_path, scope, MAX_LIMIT)
                state.set_cached(f"proposals:{scope}", items)
            body = self._envelope(marker, present, stale)
            body["items"] = items[:limit]
            self._send_json(200, body)

        def _handle_proposal_detail(
            self, proposal_id: str, marker: int, present: bool, stale: bool
        ) -> None:
            if not present:
                self._send_error(
                    404, "not_found", f"Unknown proposal_id: {proposal_id}",
                    marker, present, stale,
                )
                return
            cache_key = f"proposal:{proposal_id}"
            if stale:
                detail = state.get_cached(cache_key)
                if detail is None:
                    self._send_error(
                        503, "service_unavailable",
                        "Ledger read failed and no last-known data exists",
                        marker, present, stale,
                    )
                    return
            else:
                detail = store.get_proposal(state.ledger_path, proposal_id)
                if detail is None:
                    self._send_error(
                        404, "not_found", f"Unknown proposal_id: {proposal_id}",
                        marker, present, stale,
                    )
                    return
                state.set_cached(cache_key, detail)
            body = self._envelope(marker, present, stale)
            body["proposal"] = detail
            self._send_json(200, body)

    class ShadowProjectionHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self) -> None:
            self.state = state
            super().__init__((host, port), ShadowProjectionHandler)

    return ShadowProjectionHTTPServer()
