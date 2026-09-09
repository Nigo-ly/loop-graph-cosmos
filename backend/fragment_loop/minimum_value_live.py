"""One selected-line live trial wiring for the phone-fragment minimum loop."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from common.checkpoint import LoopCheckpoint
from common.supervisor import AdmissionState
from fragment_loop.intake import FragmentEnvelope
from fragment_loop.minimum_value import DraftAdapterResult, MinimumValueService
from fragment_loop.minimum_value_glm47 import (
    MODEL_ID,
    PROVIDER_ID,
    TARGET_PROFILE,
    Glm47DraftAdapter,
    selected_fragment_privacy_scan,
)

MAX_SOURCE_BYTES = 64 * 1024
Adapter = Callable[[str], Mapping[str, Any] | DraftAdapterResult]


def selected_line_envelope(
    source_path: str | Path,
    line_number: int,
    expected_sha256: str,
) -> FragmentEnvelope:
    source = Path(source_path)
    absolute_source = source.absolute()
    if any(part.is_symlink() for part in (absolute_source, *absolute_source.parents)):
        raise ValueError("selected source must not be a symbolic link")
    source = source.resolve(strict=True)
    if not source.is_file() or source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("selected source must be a small regular file")
    raw = source.read_text(encoding="utf-8")
    frontmatter = raw.split("---", 2)
    if len(frontmatter) < 3 or not re.search(
        r"(?m)^nigo-loop:\s*true\s*$", frontmatter[1]
    ):
        raise PermissionError("selected source does not contain nigo-loop: true")
    captured = re.search(r'(?m)^captured_at:\s*["\']?([^"\'\n]+)', frontmatter[1])
    if captured is None:
        raise ValueError("selected source has no captured_at")
    lines = raw.splitlines()
    if line_number < 1 or line_number > len(lines):
        raise ValueError("selected line is outside the source file")
    selected = lines[line_number - 1].strip()
    if not selected:
        raise ValueError("selected line is blank")
    actual_sha256 = hashlib.sha256(selected.encode("utf-8")).hexdigest()
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or actual_sha256 != expected_sha256
    ):
        raise ValueError("selected line SHA-256 does not match")
    return FragmentEnvelope(
        fragment_id=f"{source.stem}-line-{line_number}",
        captured_at=captured.group(1),
        raw_content=selected,
        input_type="text",
        source_hint="user_selected_local",
        user_note=None,
        attachments=(),
        privacy_level="personal",
        processing_status="captured",
        admission_state=AdmissionState.REQUESTED,
        source_path=f"{source}#L{line_number}",
    )


def build_glm47_service(
    db_path: str | Path,
    workspace: str | Path,
    *,
    adapter: Adapter | None = None,
) -> MinimumValueService:
    return MinimumValueService(
        db_path,
        workspace,
        adapter or Glm47DraftAdapter(),
        privacy_scan=selected_fragment_privacy_scan,
        allow_selected_local=True,
        target_provider=PROVIDER_ID,
        target_model=MODEL_ID,
        target_profile=TARGET_PROFILE,
    )


def prepare_selected_line(
    service: MinimumValueService,
    source_path: str | Path,
    line_number: int,
    expected_sha256: str,
) -> LoopCheckpoint:
    envelope = selected_line_envelope(source_path, line_number, expected_sha256)
    checkpoint = service.register_fragment(envelope)
    return service.run_local_organize(checkpoint.run_id)


def _public_status(service: MinimumValueService, run_id: str) -> dict[str, Any]:
    checkpoint = service.store.latest(run_id)
    if checkpoint is None:
        raise KeyError(run_id)
    preview = service.preview(run_id)
    attempts = checkpoint.eval_results.get("model_call_attempts", [])
    return {
        "run_id": run_id,
        "status": checkpoint.status,
        "stop_reason": checkpoint.stop_reason,
        "outbound_payload_sha256": preview.outbound_payload_sha256,
        "target_provider": preview.target_provider,
        "target_model": preview.target_model,
        "target_profile": preview.target_profile,
        "privacy_blocked": preview.privacy_blocked,
        "privacy_field_categories": list(preview.privacy_field_categories),
        "model_call_attempts": attempts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fragment_loop.minimum_value_live")
    parser.add_argument("--db", required=True)
    parser.add_argument("--workspace", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--line", required=True, type=int)
    prepare.add_argument("--expected-sha256", required=True)
    status = commands.add_parser("status")
    status.add_argument("run_id")
    run = commands.add_parser("run")
    run.add_argument("run_id")
    run.add_argument("--execute-once", action="store_true")
    commands.add_parser("serve-decisions")
    args = parser.parse_args(argv)

    service = build_glm47_service(args.db, args.workspace)
    if args.command == "prepare":
        checkpoint = prepare_selected_line(
            service,
            args.source,
            args.line,
            args.expected_sha256,
        )
        print(json.dumps(_public_status(service, checkpoint.run_id), ensure_ascii=False))
        return 0
    if args.command == "status":
        print(json.dumps(_public_status(service, args.run_id), ensure_ascii=False))
        return 0
    if args.command == "run":
        if not args.execute_once:
            parser.error("run requires --execute-once")
        checkpoint = service.generate_draft(args.run_id)
        print(json.dumps(_public_status(service, checkpoint.run_id), ensure_ascii=False))
        return 0
    if args.command == "serve-decisions":
        from fragment_loop.minimum_value_server import make_minimum_value_server

        server = make_minimum_value_server(service)
        print("minimum-value live decisions serving on 127.0.0.1:5683", flush=True)
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
