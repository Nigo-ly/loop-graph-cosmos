"""Loopback-only stdlib HTTP server for the Projection API (contract v2).

Security posture (contract §"Provider implementation constraints"):
- binds 127.0.0.1 only; non-loopback clients are dropped without a response;
- Host must be exactly 127.0.0.1:<port> or localhost:<port>, else 403;
- GET only; anything else is 405 with ``Allow: GET``;
- no CORS headers are ever emitted.

Freshness (contract §"Freshness semantics"): every request performs a fresh
read-only source_marker probe. ``stale`` is true only when that probe fails
or the observed source_sequence regresses; a quiet database is never stale.
When stale, list/detail endpoints serve last-known payloads from a small
in-memory cache, or 503 if nothing was ever served.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from common.loopback_http import (
    QuietLogMixin,
    close_without_response,
    host_header_allowed,
    is_loopback_peer,
)

from . import CONTRACT_VERSION, PROVIDER_VERSION, project, store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5679

ENVELOPE_KEYS = (
    "contract_version",
    "generated_at",
    "provider_version",
    "db_mode",
    "source_sequence",
    "source_committed_at",
    "stale",
)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _iso_now() -> str:
    """Local ISO-8601 with numeric offset, second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _data_age_seconds(source_committed_at: str | None, now: datetime) -> int | None:
    committed = _parse_ts(source_committed_at)
    if committed is None:
        return None
    return max(0, int((now - committed).total_seconds()))


class ProviderState:
    """Shared provider state: last-known source marker and payload cache."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._last_source_sequence: int | None = None
        self._last_source_committed_at: str | None = None
        self._last_read_at: str | None = None
        self._cache: dict[str, Any] = {}

    def fresh_marker(self) -> tuple[int | None, str | None, bool, bool]:
        """Fresh source probe.

        Returns (source_sequence, source_committed_at, stale, read_ok) where
        read_ok is whether the read-only query itself succeeded (drives
        provider_healthy) and stale additionally covers sequence regression.
        """
        try:
            sequence, committed_at = store.source_marker(self.db_path)
        except Exception:
            with self._lock:
                return (
                    self._last_source_sequence,
                    self._last_source_committed_at,
                    True,
                    False,
                )
        with self._lock:
            stale = (
                self._last_source_sequence is not None
                and sequence < self._last_source_sequence
            )
            if not stale:
                self._last_source_sequence = sequence
                self._last_source_committed_at = committed_at
            self._last_read_at = _iso_now()
            return (
                self._last_source_sequence,
                self._last_source_committed_at,
                stale,
                True,
            )

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
    db_path: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    state = ProviderState(db_path)

    class ProjectionHandler(QuietLogMixin, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"projection-api/{PROVIDER_VERSION}"

        # -- helpers ------------------------------------------------------

        def _envelope(
            self,
            source_sequence: int | None,
            source_committed_at: str | None,
            stale: bool,
        ) -> dict[str, Any]:
            return {
                "contract_version": CONTRACT_VERSION,
                "generated_at": _iso_now(),
                "provider_version": PROVIDER_VERSION,
                "db_mode": "read_only",
                "source_sequence": source_sequence,
                "source_committed_at": source_committed_at,
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
            source_sequence: int | None,
            source_committed_at: str | None,
            stale: bool,
            headers: dict[str, str] | None = None,
        ) -> None:
            body = self._envelope(source_sequence, source_committed_at, stale)
            body["error"] = {"code": code, "message": message}
            self._send_json(status, body, headers=headers)

        def _host_allowed(self) -> bool:
            return host_header_allowed(self.headers, self.server.server_address)

        # -- dispatch -----------------------------------------------------

        def _dispatch(self) -> None:
            if not is_loopback_peer(self.client_address[0]):
                # Non-loopback client: close without any response.
                close_without_response(self)
                return
            sequence, committed_at, stale, read_ok = state.fresh_marker()
            if not self._host_allowed():
                self._send_error(
                    403, "forbidden", "Host header is not allowed",
                    sequence, committed_at, stale,
                )
                return
            if self.command != "GET":
                self._send_error(
                    405, "method_not_allowed", "Only GET is supported",
                    sequence, committed_at, stale, headers={"Allow": "GET"},
                )
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            try:
                if path == "/loop/v1/health":
                    self._handle_health(sequence, committed_at, stale, read_ok)
                elif path == "/loop/v1/queue":
                    self._handle_queue(sequence, committed_at, stale)
                elif path == "/loop/v1/runs":
                    self._handle_runs(query, sequence, committed_at, stale)
                elif path.startswith("/loop/v1/runs/"):
                    run_id = unquote(path[len("/loop/v1/runs/"):])
                    if not run_id or "/" in run_id:
                        self._send_error(
                            404, "not_found", "Unknown resource",
                            sequence, committed_at, stale,
                        )
                    else:
                        self._handle_run_detail(run_id, sequence, committed_at, stale)
                else:
                    self._send_error(
                        404, "not_found", "Unknown resource",
                        sequence, committed_at, stale,
                    )
            except BrokenPipeError:
                self.close_connection = True
            except Exception:  # projection failure: report safely, never leak internals
                self._send_error(
                    500, "internal_error", "Projection failed",
                    sequence, committed_at, stale,
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
            self,
            sequence: int | None,
            committed_at: str | None,
            stale: bool,
            read_ok: bool,
        ) -> None:
            now = datetime.now().astimezone()
            body = self._envelope(sequence, committed_at, stale)
            body["provider_healthy"] = read_ok
            body["last_read_at"] = state.last_read_at
            body["data_age_seconds"] = _data_age_seconds(committed_at, now)
            self._send_json(200, body)

        def _handle_queue(
            self,
            sequence: int | None,
            committed_at: str | None,
            stale: bool,
        ) -> None:
            if stale:
                items = state.get_cached("queue")
                if items is None:
                    self._send_error(
                        503, "service_unavailable",
                        "Database read failed and no last-known data exists",
                        sequence, committed_at, stale,
                    )
                    return
            else:
                items = project.compute_queue_items(state.db_path)
                state.set_cached("queue", items)
            body = self._envelope(sequence, committed_at, stale)
            body["items"] = items
            self._send_json(200, body)

        def _handle_runs(
            self,
            query: dict[str, list[str]],
            sequence: int | None,
            committed_at: str | None,
            stale: bool,
        ) -> None:
            limit = DEFAULT_LIMIT
            raw_limits = query.get("limit")
            if raw_limits:
                try:
                    limit = int(raw_limits[0], 10)
                except (TypeError, ValueError):
                    self._send_error(
                        400, "invalid_limit", "limit must be an integer",
                        sequence, committed_at, stale,
                    )
                    return
                if limit < 1:
                    self._send_error(
                        400, "invalid_limit", "limit must be >= 1",
                        sequence, committed_at, stale,
                    )
                    return
                limit = min(limit, MAX_LIMIT)
            status_values = query.get("status")
            display_values = query.get("display_state")
            status_filter = status_values[0] if status_values else None
            display_filter = display_values[0] if display_values else None

            if stale:
                summaries = state.get_cached("runs")
                if summaries is None:
                    self._send_error(
                        503, "service_unavailable",
                        "Database read failed and no last-known data exists",
                        sequence, committed_at, stale,
                    )
                    return
            else:
                summaries = project.compute_run_summaries(state.db_path)
                state.set_cached("runs", summaries)

            items = list(summaries)
            if status_filter is not None:
                items = [item for item in items if item["status"] == status_filter]
            if display_filter is not None:
                items = [
                    item for item in items if item["display_state"] == display_filter
                ]
            items = items[:limit]
            body = self._envelope(sequence, committed_at, stale)
            body["items"] = items
            self._send_json(200, body)

        def _handle_run_detail(
            self,
            run_id: str,
            sequence: int | None,
            committed_at: str | None,
            stale: bool,
        ) -> None:
            cache_key = f"run:{run_id}"
            if stale:
                detail = state.get_cached(cache_key)
                if detail is None:
                    self._send_error(
                        503, "service_unavailable",
                        "Database read failed and no last-known data exists",
                        sequence, committed_at, stale,
                    )
                    return
            else:
                detail = project.compute_run_detail(state.db_path, run_id)
                if detail is None:
                    self._send_error(
                        404, "not_found", f"Unknown run_id: {run_id}",
                        sequence, committed_at, stale,
                    )
                    return
                state.set_cached(cache_key, detail)
            body = self._envelope(sequence, committed_at, stale)
            body["run"] = detail
            self._send_json(200, body)

    class ProjectionHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self) -> None:
            self.state = state
            super().__init__((host, port), ProjectionHandler)

    return ProjectionHTTPServer()
