"""Loopback decision API for the phone-fragment minimum value loop.

This adapter records only the user's send/decline decision. It never invokes
the synthetic adapter, reads credentials, or starts model generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlsplit

from common.loopback_http import parse_json, read_bounded_body
from fragment_loop.minimum_value import MinimumValueService

CONTRACT_VERSION = "1"
SERVICE_VERSION = "phone-fragment-minimum-value-decision-r1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5683
TRUSTED_ORIGIN = "app://obsidian.md"
DECISION_HEADER = "X-Fragment-Send-Decision"
MAX_BODY_BYTES = 16 * 1024
BODY_READ_TIMEOUT_SECONDS = 2.0
LOOPBACK_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

BODY_KEYS = {
    "decision",
    "run_id",
    "fragment_id",
    "outbound_payload_sha256",
    "target_provider",
    "target_model",
    "target_profile",
    "expected_sequence",
    "idempotency_key",
    "requester",
}


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def decision_idempotency_key(body: dict[str, Any]) -> str:
    canonical = "\x1f".join(
        (
            str(body["requester"]),
            str(body["decision"]),
            str(body["run_id"]),
            str(body["fragment_id"]),
            str(body["outbound_payload_sha256"]),
            str(body["target_provider"]),
            str(body["target_model"]),
            str(body["target_profile"]),
            str(body["expected_sequence"]),
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validated_body(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != BODY_KEYS:
        raise ValueError("invalid_body")
    if raw.get("decision") not in {"consent", "decline"}:
        raise ValueError("invalid_decision")
    for field in (
        "run_id",
        "fragment_id",
        "target_provider",
        "target_model",
        "target_profile",
    ):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x1f" in value:
            raise ValueError(f"invalid_{field}")
    sha = raw.get("outbound_payload_sha256")
    if (
        not isinstance(sha, str)
        or len(sha) != 64
        or any(character not in "0123456789abcdef" for character in sha)
    ):
        raise ValueError("invalid_outbound_payload_sha256")
    sequence = raw.get("expected_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("invalid_expected_sequence")
    requester = raw.get("requester")
    if requester != "nigo":
        raise ValueError("invalid_requester")
    idempotency_key = raw.get("idempotency_key")
    if (
        not isinstance(idempotency_key, str)
        or len(idempotency_key) != 64
        or any(character not in "0123456789abcdef" for character in idempotency_key)
        or idempotency_key != decision_idempotency_key(raw)
    ):
        raise ValueError("invalid_idempotency_key")
    return dict(raw)


def make_minimum_value_server(
    service: MinimumValueService,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    require_trusted_origin: bool = True,
) -> ThreadingHTTPServer:
    if host != DEFAULT_HOST:
        raise ValueError("minimum value decision API must bind 127.0.0.1")

    class MinimumValueHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"minimum-value-api/{SERVICE_VERSION}"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, data: Any, error: Any = None) -> None:
            payload = json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "generated_at": _iso_now(),
                    "service_version": SERVICE_VERSION,
                    "data": data,
                    "error": error,
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

        def _dispatch(self) -> None:
            if self.client_address[0] not in LOOPBACK_CLIENTS:
                self.close_connection = True
                return
            if not self._host_allowed():
                self.close_connection = True
                self._error(403, "forbidden", "Host header is not allowed")
                return
            if self.command != "POST":
                self.close_connection = True
                self._error(405, "method_not_allowed", "Method not allowed")
                return
            if urlsplit(self.path).path != "/fragment/v1/decisions":
                self.close_connection = True
                self._error(404, "not_found", "Unknown resource")
                return
            if (
                self.headers.get("Content-Type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                self.close_connection = True
                self._error(415, "unsupported_media_type", "application/json required")
                return
            if self.headers.get(DECISION_HEADER) != "1":
                self.close_connection = True
                self._error(403, "missing_decision_header", f"{DECISION_HEADER}: 1 required")
                return
            origin = self.headers.get("Origin")
            if require_trusted_origin and origin != TRUSTED_ORIGIN:
                self.close_connection = True
                self._error(403, "forbidden_origin", "Trusted Obsidian origin required")
                return
            if not require_trusted_origin and origin not in {None, TRUSTED_ORIGIN}:
                self.close_connection = True
                self._error(403, "forbidden_origin", "Untrusted request origin")
                return
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
                return
            if body_status in {"invalid_size", "too_large"}:
                self.close_connection = True
                self._error(413, "body_too_large", "Request body size is invalid")
                return
            if body_status == "timeout":
                self.close_connection = True
                self._error(408, "body_read_timeout", "Request body timed out")
                return
            if body_status == "truncated":
                self.close_connection = True
                self._error(400, "body_truncated", "Request body was truncated")
                return
            try:
                assert raw_body is not None
                ok, raw = parse_json(raw_body)
                if not ok:
                    raise ValueError("invalid_json")
                body = _validated_body(raw)
            except ValueError as error:
                code = str(error)
                self._error(400, code, "Invalid decision request")
                return

            binding = {
                "fragment_id": str(body["fragment_id"]),
                "outbound_payload_sha256": str(body["outbound_payload_sha256"]),
                "target_provider": str(body["target_provider"]),
                "target_model": str(body["target_model"]),
                "target_profile": str(body["target_profile"]),
            }
            try:
                if body["decision"] == "consent":
                    record = service.consent(
                        str(body["run_id"]),
                        str(body["idempotency_key"]),
                        expected_sequence=int(body["expected_sequence"]),
                        expected_binding=binding,
                    )
                    data = {
                        "decision": "consent",
                        "decision_id": record["consent_id"],
                        "status": "recorded",
                    }
                else:
                    checkpoint = service.decline_send(
                        str(body["run_id"]),
                        decision_id=str(body["idempotency_key"]),
                        expected_sequence=int(body["expected_sequence"]),
                        expected_binding=binding,
                    )
                    decision = checkpoint.eval_results.get("send_decision", {})
                    data = {
                        "decision": "decline",
                        "decision_id": decision.get("decision_id"),
                        "status": "recorded",
                    }
            except KeyError:
                self._error(404, "run_not_found", "Unknown run_id")
                return
            except PermissionError:
                self._error(409, "decision_conflict", "Decision state or binding changed")
                return
            except ValueError:
                self._error(400, "invalid_decision", "Invalid decision request")
                return
            except Exception:
                self._error(500, "internal_error", "Decision request failed")
                return
            self._send_json(202, data)

        do_POST = _dispatch  # noqa: N815
        do_GET = _dispatch  # noqa: N815
        do_PUT = _dispatch  # noqa: N815
        do_DELETE = _dispatch  # noqa: N815
        do_PATCH = _dispatch  # noqa: N815
        do_OPTIONS = _dispatch  # noqa: N815

    return ThreadingHTTPServer((host, port), MinimumValueHandler)


def _decision_only_adapter(payload: str) -> dict[str, Any]:
    del payload
    raise RuntimeError("decision API cannot generate a model draft")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragment_loop.minimum_value_server",
        description="Serve the phone-fragment send decision API in the foreground",
    )
    parser.add_argument("--db", required=True, help="existing or synthetic checkpoint DB")
    parser.add_argument("--workspace", required=True, help="minimum-value workspace")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    service = MinimumValueService(args.db, args.workspace, _decision_only_adapter)
    server = make_minimum_value_server(service, port=args.port)
    port = int(server.server_address[1])
    print(
        f"minimum-value-decision {SERVICE_VERSION} serving "
        f"http://{DEFAULT_HOST}:{port}/fragment/v1",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
