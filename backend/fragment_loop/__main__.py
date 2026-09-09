"""CLI for registering a real fragment without bypassing approval."""

from __future__ import annotations

import argparse
import json

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore
from fragment_loop.runtime import (
    DEFAULT_DB_PATH,
    register_cognitive_intake,
)


def print_checkpoint(
    checkpoint: LoopCheckpoint, reasons: list[str] | None = None
) -> None:
    print(
        json.dumps(
            {
                "run_id": checkpoint.run_id,
                "fragment_id": checkpoint.fragment_id,
                "status": checkpoint.status,
                "current_node": checkpoint.current_node,
                "attempt_episode": checkpoint.attempt_episode,
                "preflight_reasons": reasons or [],
                "resume_condition": checkpoint.resume_condition,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Register and inspect cognitive V1 fragments")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite state path")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser(
        "register", help="Register a fragment in the current cognitive V1 intake"
    )
    register.add_argument("source", help="Path to the raw fragment Markdown file")
    status = commands.add_parser("status", help="Read the latest persisted checkpoint")
    status.add_argument("run_id")
    args = parser.parse_args()

    if args.command == "register":
        print_checkpoint(register_cognitive_intake(args.source, db_path=args.db))
        return
    latest = SQLiteCheckpointStore(args.db).latest(args.run_id)
    if latest is None:
        parser.error(f"unknown run_id: {args.run_id}")
    print_checkpoint(latest)


if __name__ == "__main__":
    main()
