"""CLI: ``python -m shadow_projection_api serve --ledger ... --port ...``.

The ledger path is required on purpose: there is no default location, so
an accidental invocation can never serve (let alone create) a production
shadow ledger. P3B runs this service temporarily only; nothing here
installs anything.
"""

from __future__ import annotations

import argparse
import sys

from .server import DEFAULT_PORT, make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shadow_projection_api")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve the read-only shadow projection")
    serve.add_argument("--ledger", required=True, help="shadow ledger SQLite (read-only)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    if args.command == "serve":
        server = make_server(args.ledger, port=args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
