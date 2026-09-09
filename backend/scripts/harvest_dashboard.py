#!/usr/bin/env python3
"""Generate a compact, reviewable Harvest dashboard from SQLite traces."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "loop_state.sqlite3"
PHONE_FRAGMENT_LINK_LOOP_ID = "phone-fragment-link-v1"
FRAGMENT_COGNITIVE_LOCAL_LOOP_ID = "fragment-cognitive-local-v1"


def latest_runs(database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            WITH latest AS (
              SELECT run_id, MAX(sequence) AS sequence FROM checkpoints GROUP BY run_id
            )
            SELECT c.sequence, c.payload_json,
                   (SELECT COUNT(*) FROM checkpoints h
                    WHERE h.run_id = c.run_id
                      AND COALESCE(h.revision_reason, '') NOT LIKE '%harvest_dashboard%') AS events
            FROM checkpoints c JOIN latest l ON c.sequence = l.sequence
            ORDER BY c.committed_at DESC
            """
        ).fetchall()
    values: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload["sequence"] = row["sequence"]
        payload["events"] = row["events"]
        values.append(payload)
    return values


def render(runs: list[dict[str, Any]]) -> str:
    child_runs = [item for item in runs if item.get("parent_loop_id")]
    passed = [item for item in child_runs if item.get("status") == "passed"]
    cognitive_fragments = {
        item.get("fragment_id")
        for item in runs
        if item.get("loop_id") == FRAGMENT_COGNITIVE_LOCAL_LOOP_ID
    }
    pending_parents = [
        item
        for item in runs
        if not item.get("parent_loop_id") and item.get("status") == "approved"
        and not (
            item.get("loop_id") == PHONE_FRAGMENT_LINK_LOOP_ID
            and item.get("fragment_id") in cognitive_fragments
        )
    ]
    lines = [
        "---",
        "type: loop-harvest-dashboard",
        "generated: true",
        "---",
        "",
        "# Loop Harvest 审核台",
        "",
        (
            f"> 可评审子 Loop：**{len(child_runs)}** · 已通过：**{len(passed)}** "
            f"· 待路由：**{len(pending_parents)}** · 目标：**20 条高质量 trace**"
        ),
        "",
        "## 待路由 nigo-loop",
        "",
        "| 碎片 | 状态 | 下一步 |",
        "|---|---|---|",
    ]
    if pending_parents:
        for item in pending_parents:
            next_step = (
                "等待语义展开与路线判断"
                if item.get("loop_id") == FRAGMENT_COGNITIVE_LOCAL_LOOP_ID
                else "选择业务 LoopSpec 与独立 Verifier"
            )
            lines.append(
                f"| `{item['fragment_id']}` | {item['status']} | "
                f"{next_step} |"
            )
    else:
        lines.append("| — | 无待路由项目 | — |")
    lines.extend(
        (
        "",
        "## 每日审核",
        "",
        "| 场景 | 状态 | 轮次 | 事件 | Eval | 预算 |",
        "|---|---|---:|---:|---|---|",
        )
    )
    for item in child_runs:
        eval_name = item.get("evaluator_version") or "未标注"
        used = item.get("budget_used", {})
        budget = f"{used.get('tokens', 0)} tok / {used.get('tool_calls', 0)} calls"
        lines.append(
            f"| `{item['loop_id']}` | {item['status']} | {item['iteration']} | "
            f"{item['events']} | `{eval_name}` | {budget} |"
        )
    lines.extend(
        (
            "",
            "## 晋级检查",
            "",
            "一条 trace 只有同时满足以下条件才进入 20 条 Harvest 样本：",
            "",
            "- 场景与已有样本具有实质差异；",
            "- 标注成功、失败、重试、无增益、预算耗尽或人工介入等结果类型；",
            "- 危险误判与修正被保留，不能只保留顺利路径；",
            "- 真实结果经过独立 Evaluator；",
            "- 离线回放没有发现错误早停。",
            "",
            "## 门控边界",
            "",
            (
                "Harvest 未满不阻塞继续运行真实 nigo-loop；"
                "只阻塞把尚未验证的停止策略接入生产 Supervisor。"
            ),
        )
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = render(latest_runs(args.db.expanduser()))
    if args.output:
        args.output.expanduser().write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
