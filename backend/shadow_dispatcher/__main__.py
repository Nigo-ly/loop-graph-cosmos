"""CLI: ``python -m shadow_dispatcher scan --loop-db ... --ledger ...``.

The ledger path is required on purpose: there is no default location, so
an accidental invocation can never create a production shadow ledger.
"""

from __future__ import annotations

import argparse
import json
import sys

from common.orchestrator import AgentRegistry, HealthSnapshot

from .dispatcher import DEFAULT_LOOPSPEC_REGISTRY_PATH, load_loopspec_registry, scan
from .ledger import ShadowLedger
from .policy import RoutePolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shadow_dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_parser = sub.add_parser("scan", help="run one deterministic shadow scan")
    scan_parser.add_argument("--loop-db", required=True, help="Loop SQLite, opened read-only")
    scan_parser.add_argument("--ledger", required=True, help="shadow ledger SQLite (created)")
    scan_parser.add_argument("--policy", default="config/shadow_route_policy.json")
    scan_parser.add_argument("--registry", default="config/agent_registry.json")
    scan_parser.add_argument("--health", default="data/agent_health.json")
    scan_parser.add_argument("--loopspec-registry", default=DEFAULT_LOOPSPEC_REGISTRY_PATH)
    args = parser.parse_args(argv)

    if args.command == "scan":
        summary = scan(
            loop_db_path=args.loop_db,
            ledger=ShadowLedger(args.ledger),
            policy=RoutePolicy.load(args.policy),
            agent_registry=AgentRegistry.load(args.registry),
            health=HealthSnapshot.load(args.health),
            loopspec_registry=load_loopspec_registry(args.loopspec_registry),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
