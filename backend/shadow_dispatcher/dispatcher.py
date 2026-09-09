"""Scan orchestration: read-only queue scan -> deterministic proposals ->
atomic ledger commit. This is the whole P3A runtime; there is no service,
no scheduler, and no model call anywhere in the path.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.orchestrator import AgentRegistry, HealthSnapshot
from projection_api.project import compute_attempt_info
from projection_api.store import latest_runs

from . import DISPATCHER_VERSION
from .ledger import ShadowLedger
from .policy import RoutePolicy, registry_fingerprint
from .proposals import Proposal, build_proposal

ADMITTED_STATUS = "approved"

DEFAULT_LOOPSPEC_REGISTRY_PATH = "config/control_loop_registry.json"


def load_loopspec_registry(path: str | Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(key): dict(value) for key, value in payload.items()}


def scan(
    *,
    loop_db_path: str | Path,
    ledger: ShadowLedger,
    policy: RoutePolicy,
    agent_registry: AgentRegistry,
    health: HealthSnapshot,
    loopspec_registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """One deterministic shadow scan. Returns a JSON-able summary."""
    owner = f"scan-{uuid.uuid4().hex}"
    interrupted = ledger.fail_interrupted_runs()
    dispatcher_run_id = ledger.begin_run(DISPATCHER_VERSION, owner=owner)

    with ledger.heartbeat(dispatcher_run_id, owner):
        rows = latest_runs(str(loop_db_path))
        attempts = compute_attempt_info(rows)
        admitted = [
            row
            for row in rows
            if row.status == ADMITTED_STATUS and attempts[row.run_id].is_current
        ]
        # Runs already in execution keep their routed proposals open: no
        # new proposals for them, and their active proposals are never
        # staled while they are mid-flight. Once a run resolves, the next
        # scan stales its proposal through the normal lifecycle.
        preserved_run_ids = {row.run_id for row in rows if row.status == "running"}

        fingerprint = registry_fingerprint(policy, loopspec_registry, agent_registry, health)
        now = datetime.now(UTC)
        proposals: list[Proposal] = [
            build_proposal(
                row,
                attempt_episode=attempts[row.run_id].attempt_episode,
                loopspec_entry=loopspec_registry.get(row.loop_id),
                policy=policy,
                registry=agent_registry,
                health=health,
                fingerprint=fingerprint,
                now=now,
            )
            for row in sorted(admitted, key=lambda item: item.run_id)
        ]

        result = ledger.commit_scan(
            dispatcher_run_id,
            proposals,
            scanned_runs=len(rows),
            admitted_run_ids={row.run_id for row in admitted},
            owner=owner,
            preserved_run_ids=preserved_run_ids,
        )
    counts = result["counts"]
    supersedes: dict[str, str] = result["supersedes"]
    stored_proposals = [
        replace(proposal, supersedes_proposal_id=supersedes[proposal.proposal_id])
        if proposal.proposal_id in supersedes
        else proposal
        for proposal in proposals
    ]
    return {
        "dispatcher_run_id": dispatcher_run_id,
        "dispatcher_version": DISPATCHER_VERSION,
        "policy_version": policy.policy_version,
        "fingerprint": fingerprint,
        "interrupted_runs_failed": interrupted,
        "scanned_runs": len(rows),
        "admitted_runs": len(admitted),
        "counts": counts,
        "proposals": [proposal.to_dict() for proposal in stored_proposals],
    }
