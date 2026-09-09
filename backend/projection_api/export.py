"""Read-only snapshot export tool (P1C fixture producer).

Hashes the database file before connecting and again after all reads
finish; refuses to write any output if the hash changed. Never writes into
the frontend repository and refuses output directories inside data/.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from . import project, store


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_all_rows(db_path: str) -> list[dict[str, Any]]:
    with closing(store._connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT * FROM checkpoints ORDER BY sequence ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def run_export(db_path: str, out_dir: str, argv: list[str]) -> list[Path]:
    db_file = Path(db_path)
    if not db_file.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")

    out = Path(out_dir).resolve()
    data_dir = db_file.resolve().parent
    if out == data_dir or data_dir in out.parents:
        raise ValueError(f"output directory must not be inside data/: {out}")

    hash_before = sha256_file(db_file)

    source_sequence, source_committed_at = store.source_marker(str(db_file))
    rows = _read_all_rows(str(db_file))
    queue_items = project.compute_queue_items(str(db_file))
    run_summaries = project.compute_run_summaries(str(db_file))

    hash_after = sha256_file(db_file)
    if hash_before != hash_after:
        raise RuntimeError(
            "database file changed during export; refusing to write snapshots"
        )

    export_record = {
        "command": shlex.join(list(argv)),
        "exported_at": _iso_now(),
        "db_sha256": hash_before,
        "source_sequence": source_sequence,
    }

    out.mkdir(parents=True, exist_ok=True)
    payloads = {
        "checkpoints.snapshot.json": {"export": export_record, "rows": rows},
        "queue.snapshot.json": {
            "export": export_record,
            "source_sequence": source_sequence,
            "source_committed_at": source_committed_at,
            "items": queue_items,
        },
        "runs.snapshot.json": {
            "export": export_record,
            "source_sequence": source_sequence,
            "source_committed_at": source_committed_at,
            "items": run_summaries,
        },
    }
    written: list[Path] = []
    for name, payload in payloads.items():
        target = out / name
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(target)
    return written
