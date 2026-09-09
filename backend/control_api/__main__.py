"""CLI: ``python -m control_api serve``.

Foreground only. P2A does not install or start any background service;
production use requires separate approval.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from common.supervisor import LoopSpec

from . import LEDGER_PATH, LOOP_DB_PATH, SERVICE_VERSION
from .server import DEFAULT_HOST, DEFAULT_PORT
from .service import RegisteredLoop


def load_registry(path: str) -> dict[str, RegisteredLoop]:
    """Load {loop_id: {spec: {...}, "handlers": [...]}} from a JSON file."""
    with open(path, encoding="utf-8") as handle:
        raw: dict[str, Any] = json.load(handle)
    registry: dict[str, RegisteredLoop] = {}
    for loop_id, entry in raw.items():
        spec_data = entry["spec"]
        spec = LoopSpec(
            loop_id=str(spec_data["loop_id"]),
            version=str(spec_data["version"]),
            goal=str(spec_data["goal"]),
            first_node=str(spec_data["first_node"]),
            nodes=tuple(str(node) for node in spec_data["nodes"]),
            max_iterations=int(spec_data["max_iterations"]),
            max_seconds=int(spec_data["max_seconds"]),
            token_limit=int(spec_data["token_limit"]),
            tool_call_limit=int(spec_data["tool_call_limit"]),
            worker_version=str(spec_data.get("worker_version", "unassigned")),
            evaluator_version=str(spec_data.get("evaluator_version", "unassigned")),
        )
        registry[loop_id] = RegisteredLoop(
            spec=spec,
            handler_nodes=frozenset(str(node) for node in entry.get("handlers", [])),
        )
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="control_api")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help="serve the API in the foreground")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--db", default=LOOP_DB_PATH)
    serve_parser.add_argument("--ledger", default=LEDGER_PATH)
    serve_parser.add_argument("--registry", default=None, help="JSON LoopSpec registry file")
    serve_parser.add_argument(
        "--allow-loop-id",
        action="append",
        default=None,
        help="Restrict all control reads, writes, and recovery to this LoopSpec id; repeatable.",
    )
    serve_parser.add_argument(
        "--allow-missing-origin",
        action="store_true",
        help="TEST/DEV ONLY: accept mutating requests without the trusted "
        "Obsidian Origin. Never use in production.",
    )
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .server import make_server

        registry = load_registry(args.registry) if args.registry else {}
        server = make_server(
            args.db,
            args.ledger,
            registry=registry,
            allowed_loop_ids=(
                frozenset(str(loop_id) for loop_id in args.allow_loop_id)
                if args.allow_loop_id is not None
                else None
            ),
            require_trusted_origin=not args.allow_missing_origin,
            host=DEFAULT_HOST,
            port=int(args.port),
        )
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        print(
            f"control-api {SERVICE_VERSION} serving http://{host}:{port}/control/v1 "
            f"(db={args.db}, ledger={args.ledger})",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
