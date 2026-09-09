"""CLI: ``python -m shadow_review_api serve --proposal-ledger ... --review-ledger ...``.

Both ledger paths are required: the proposal ledger is read-only, and the
review ledger is the ONLY writable store. There are no default locations,
so an accidental invocation can never touch a production file.
"""

from __future__ import annotations

import argparse
import sys

from .ledger import SQLiteReviewLedger
from .server import DEFAULT_PORT, make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shadow_review_api")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve the shadow review API")
    serve.add_argument(
        "--proposal-ledger", required=True, help="shadow proposal SQLite (read-only)"
    )
    serve.add_argument("--review-ledger", required=True, help="shadow review SQLite (created)")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    if args.command == "serve":
        server = make_server(
            args.proposal_ledger,
            SQLiteReviewLedger(args.review_ledger),
            port=args.port,
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
    sys.exit(main())
