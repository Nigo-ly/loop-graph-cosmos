"""CLI: ``python -m projection_api serve|export``."""

from __future__ import annotations

import argparse
import sys

from . import DB_PATH, PROVIDER_VERSION
from .server import DEFAULT_HOST, DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="projection_api")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="serve the API in the foreground")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--db", default=DB_PATH)

    export_parser = subparsers.add_parser("export", help="write read-only snapshots")
    export_parser.add_argument("--out", required=True, help="output directory")
    export_parser.add_argument("--db", default=DB_PATH)

    args = parser.parse_args(argv)

    if args.command == "serve":
        from .server import make_server

        server = make_server(args.db, host=DEFAULT_HOST, port=args.port)
        host = DEFAULT_HOST
        port = int(server.server_address[1])
        print(
            f"projection-api {PROVIDER_VERSION} serving "
            f"http://{host}:{port}/loop/v1 (db={args.db}, read-only)",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.command == "export":
        from .export import run_export

        command_argv = sys.argv if argv is None else ["projection_api", *argv]
        written = run_export(args.db, args.out, command_argv)
        for path in written:
            print(f"wrote {path}", flush=True)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
