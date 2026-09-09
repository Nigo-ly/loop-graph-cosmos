"""Trusted n8n-organized fragment → Loop V1 product-candidate continuation.

The bridge is deliberately narrow: it accepts one explicitly bound research
continuation, revalidates both Vault files, advances the already-registered
``fragment-cognitive-local-v1`` run through its existing checkpoint/runtime
surface, and publishes the existing ProductReview bundle format atomically.
It performs no network, model, credential, Graph or human-decision action.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.checkpoint import LoopCheckpoint, SQLiteCheckpointStore
from fragment_loop.cognitive_loop import LocalCognitiveV1Loop
from fragment_loop.cognitive_product import PRODUCT_VERSION, build_cognitive_product
from fragment_loop.intake import load_fragment
from fragment_loop.product_review import ProductReviewService
from fragment_loop.runtime import DEFAULT_DB_PATH
from fragment_loop.spec import FRAGMENT_COGNITIVE_LOCAL_V1

CONTINUATION_VERSION = "fragment-continuation-bridge-v1"
CONTINUATION_HEADER = "X-Fragment-Continuation"
CONTINUATION_BODY_KEYS = frozenset(
    {
        "fragment_id",
        "raw_ref",
        "organized_ref",
        "raw_sha256",
        "organized_sha256",
        "route",
        "requester",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FRAGMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RAW_PARENT = ("Notes", "散记", "碎片想法")
_MAX_RAW_BYTES = 16 * 1024
_MAX_ORGANIZED_BYTES = 64 * 1024


class ContinuationBridgeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _frontmatter(payload: bytes) -> tuple[dict[str, object], str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContinuationBridgeError("source_invalid") from error
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ContinuationBridgeError("source_invalid")
    try:
        end = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as error:
        raise ContinuationBridgeError("source_invalid") from error
    fields: dict[str, object] = {}
    for line in lines[1:end]:
        if not line or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        value = raw.strip()
        if value in {"true", "false"}:
            parsed: object = value == "true"
        elif value in {"null", ""}:
            parsed = None
        elif value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
        else:
            parsed = value.strip("\"'")
        fields[key.strip()] = parsed
    return fields, "\n".join(lines[end + 1 :]).strip()


def _validate_body(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != CONTINUATION_BODY_KEYS:
        raise ContinuationBridgeError("invalid_body")
    if raw.get("requester") != "nigo" or raw.get("route") != "research":
        raise ContinuationBridgeError("route_not_allowed")
    fragment_id = raw.get("fragment_id")
    if not isinstance(fragment_id, str) or _FRAGMENT_ID.fullmatch(fragment_id) is None:
        raise ContinuationBridgeError("invalid_body")
    for field in ("raw_ref", "organized_ref"):
        value = raw.get(field)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or value.startswith(("/", "~"))
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(character) < 0x20 for character in value)
        ):
            raise ContinuationBridgeError("invalid_body")
    for field in ("raw_sha256", "organized_sha256"):
        value = raw.get(field)
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ContinuationBridgeError("invalid_body")
    return {key: str(value) for key, value in raw.items()}


def _regular_file(vault_root: Path, ref: str) -> tuple[Path, bytes]:
    requested = vault_root.joinpath(*ref.split("/"))
    try:
        resolved = requested.resolve(strict=True)
        info = requested.lstat()
    except OSError as error:
        raise ContinuationBridgeError("source_not_found") from error
    try:
        resolved.relative_to(vault_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ContinuationBridgeError("source_unsafe") from error
    if requested != resolved or requested.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ContinuationBridgeError("source_unsafe")
    try:
        return resolved, resolved.read_bytes()
    except OSError as error:
        raise ContinuationBridgeError("source_unavailable") from error


def _safe_text(value: object, label: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(character) < 0x20 and character not in "\n\t" for character in value)
    ):
        raise ContinuationBridgeError(f"{label}_invalid")
    return " ".join(value.split())


def _semantic_expansion(fragment_text: str, title: str, goal: str) -> dict[str, object]:
    return {
        "literal_facts": [{"text": fragment_text, "source_quote": fragment_text}],
        "inferences": [
            {
                "text": f"用户希望研究：{title}",
                "source_quote": fragment_text,
                "transformation_basis": (
                    "整理结果把原始研究意图归并为一个待核验任务，未增加事实结论"
                ),
            }
        ],
        "uncertainties": [goal, "尚无一手来源证明相关权重已公开或可在本地运行"],
    }


def _cognitive_result(
    fragment_text: str,
    title: str,
    goal: str,
    next_action: str,
    expansion: Mapping[str, object],
) -> dict[str, object]:
    claim_text = "相关权重已经公开并可用于本地部署"
    route_reason = "整理结果明确要求检索官方资料；当前没有一手来源，只能进入研究路线"
    empty_perspective = {
        "association": "本次接续不读取额外记忆或知识资产",
        "conflicts": [],
        "value": "只保留待研究任务，不作外部事实判断",
        "uncertainty": "尚未执行 Graph Pilot 研究",
        "materials": [],
    }
    return {
        "route": "research",
        "route_reason": route_reason,
        "route_change_allowed": True,
        "summary": {
            "text": title,
            "source_quote": fragment_text,
            "transformation_basis": "标题来自已绑定的 n8n 整理产物，仅作为未验证任务名称",
        },
        "semantic_expansion": dict(expansion),
        "research": {
            "questions": [goal],
            "search_dimensions": [next_action],
            "sources": [],
            "counter_evidence_search": {
                "status": "not_found",
                "scope": "接续阶段不访问外网；相反证据留待受控 Graph Pilot 研究",
                "findings": [],
            },
            "v1_claims": [
                {
                    "claim_id": "C1",
                    "claim_kind": "general_fact",
                    "text": claim_text,
                    "source_quote": fragment_text,
                    "verdict": "not_covered",
                    "confidence": "low",
                    "evidence_refs": [],
                    "evidence_support": [],
                    "claim_derivation": "原始碎片提出开源与本地运行设想，整理结果未提供一手来源",
                    "verdict_basis": "没有外部证据，不能把用户输入升级为已验证事实",
                    "independent_verification": {
                        "status": "not_verified",
                        "mode": "none",
                        "basis": "接续阶段零外网、零模型，尚未独立验证",
                        "evidence_refs": [],
                        "evidence_support": [],
                    },
                }
            ],
            "v2_revisions": [
                {
                    "claim_id": "C1",
                    "revision_status": "unchanged",
                    "deviation": "逐项复核后仍无外部证据覆盖",
                    "revision_reason": "保持未覆盖，等待 Graph Pilot 检索官方资料",
                    "revised_text": claim_text,
                }
            ],
        },
        "perspectives": [
            {"perspective": "memory", "summary": "未读取额外记忆", **empty_perspective},
            {
                "perspective": "knowledge_base",
                "summary": "未读取额外知识库材料",
                **empty_perspective,
            },
            {"perspective": "frontier", "summary": "尚未检索前沿资料", **empty_perspective},
        ],
        "synthesis": {
            "value": "把已经整理的研究意图接续为可审计的未验证候选",
            "weakest_link": "缺少官方发布、模型规格、许可证和硬件要求",
            "conflicts": [],
            "open_questions": [goal],
            "next_step": next_action,
            "credibility": "insufficient",
            "credibility_basis": "全部事实主张仍为未覆盖，尚无来源",
            "claim_counts": {
                "supported": 0,
                "partially_supported": 0,
                "contradicted": 0,
                "not_covered": 1,
            },
        },
    }


def _product(
    fragment_id: str,
    title: str,
    category: str,
    goal: str,
    next_action: str,
) -> dict[str, object]:
    return {
        "title": title,
        "core_judgment": {
            "statement": "当前只有待研究目标，相关权重是否公开及能否本地运行均尚未验证。",
            "user_value": "通过受控 Graph Pilot 核验官方发布、硬件需求与可执行部署路径。",
            "credibility": "insufficient",
            "suggested_action": "创建研究工作流并在模型调用前由用户检查输入与预算。",
        },
        "key_facts": [
            "用户明确提出本地部署可行性研究目标。",
            "现有整理结果没有提供可核验的一手资料。",
            "下一步需要检索官方发布、模型规格、许可证和硬件要求。",
        ],
        "critical_corrections": [],
        "personal_connections": [],
        "blind_spots": [goal],
        "conclusion": {
            "summary": "适合作为未验证研究任务进入 Graph，不适合作为事实或知识资产发布。",
            "next_step": next_action,
        },
        "candidate_cards": [
            {
                "card_id": "card-official-release-and-local-fit",
                "title": "官方发布与本地运行条件待核验",
                "knowledge": (
                    "只有官方权重、许可证、依赖和硬件要求均明确后，"
                    "才能判断本地部署可行性。"
                ),
                "why_important": "避免把用户输入中的开源说法直接当成可执行结论。",
                "scope": "适用于开源模型权重与本地部署可行性调研。",
                "evidence_refs": ["C1"],
            }
        ],
        "topics": [{"topic_id": "local-model-deployment", "label": category}],
        "fragment_ref": f"fragments/{fragment_id}.md",
    }


def _candidate_payloads(
    fragment_id: str,
    bundle: Mapping[str, object],
    binding: Mapping[str, object],
) -> tuple[str, dict[str, bytes]]:
    markdown = str(bundle["human_markdown"]).encode("utf-8")
    binding_digest = _sha256(_canonical(binding))
    base = f"fragment-product-{fragment_id}-{binding_digest[:16]}"
    machine = {
        "bridge_version": CONTINUATION_VERSION,
        "product_version": PRODUCT_VERSION,
        "plan_version": CONTINUATION_VERSION,
        "session_identity": binding_digest,
        "fragment_id": fragment_id,
        "lifecycle_status": "draft",
        "evidence_level": "unverified",
        "user_confirmed": False,
        "promoted_to_asset": False,
        "paper_independently_verified": False,
        "source_basis": "n8n_organized_unverified",
        "draft_sha256": _sha256(markdown),
        "candidate_cards": bundle["candidate_cards"],
        "graph_projection": bundle["graph_projection"],
        "home_projection": bundle["home_projection"],
        "sidecar": bundle["sidecar"],
        "continuation_binding": dict(binding),
    }
    return base, {
        f"{base}.md": markdown,
        f"{base}.json": _canonical(machine) + b"\n",
    }


def _publish_candidate(directory: Path, payloads: Mapping[str, bytes]) -> str:
    if directory.exists():
        json_files = sorted(directory.glob("fragment-product-*.json"))
        if len(json_files) != 1:
            raise ContinuationBridgeError("candidate_conflict")
        try:
            machine = json.loads(json_files[0].read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContinuationBridgeError("candidate_conflict") from error
        expected = next(name for name in payloads if name.endswith(".json"))
        if json_files[0].name != expected or _canonical(machine) + b"\n" != payloads[expected]:
            raise ContinuationBridgeError("candidate_conflict")
        markdown_name = next(name for name in payloads if name.endswith(".md"))
        try:
            if (directory / markdown_name).read_bytes() != payloads[markdown_name]:
                raise ContinuationBridgeError("candidate_conflict")
        except OSError as error:
            raise ContinuationBridgeError("candidate_conflict") from error
        return "already_published"
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}.staging-", dir=directory.parent))
    try:
        for name, payload in payloads.items():
            path = staging / name
            path.write_bytes(payload)
            path.chmod(0o600)
        os.replace(staging, directory)
    except Exception:
        for path in staging.iterdir():
            try:
                path.unlink()
            except OSError:
                pass
        try:
            staging.rmdir()
        except OSError:
            pass
        raise
    return "published"


def prospective_raw_source(
    store: SQLiteCheckpointStore, vault_root: Path, fragment_id: str, cutover: datetime | None,
) -> dict[str, object] | None:
    """Read the original opted-in public question only after first-intake proof.

    No organizer artifact is required or invented. This is the same durable
    capture gate used before an execution can use subscription research.
    """
    from fragment_loop.intake import _split_frontmatter
    from fragment_loop.research_fetch import public_https_seed_url
    from fragment_loop.runtime import capture_identity_sha256

    try:
        if (not isinstance(cutover, datetime) or cutover.tzinfo is None
                or cutover.utcoffset() is None or _FRAGMENT_ID.fullmatch(fragment_id) is None):
            return None
        intake_id = "cognitive-local-v1-" + hashlib.sha256(
            fragment_id.encode("utf-8")).hexdigest()
        history = store.history(intake_id)
        if not history:
            return None
        first = history[0]
        captured = first.eval_results.get("cognitive_input", {})
        receipt = captured.get("capture_receipt") if isinstance(captured, Mapping) else None
        if (first.loop_id != FRAGMENT_COGNITIVE_LOCAL_V1.loop_id
                or first.fragment_id != fragment_id
                or not isinstance(receipt, Mapping) or set(receipt) != {
                    "version", "captured_at", "received_at", "raw_sha256", "identity_sha256",
                    "source_ref"}
                or receipt.get("version") != "fragment-capture-v1"):
            return None
        timestamps = []
        for key in ("captured_at", "received_at"):
            stamp = receipt[key]
            if not isinstance(stamp, str) or len(stamp) > 80:
                return None
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            timestamps.append(parsed)
        original_at, received_at = timestamps
        if not cutover <= original_at <= received_at <= datetime.now(UTC):
            return None
        raw_ref = f"Notes/散记/碎片想法/{fragment_id}.md"
        raw_path, raw_bytes = _regular_file(vault_root, raw_ref)
        if (len(raw_bytes) > _MAX_RAW_BYTES or str(raw_path.resolve()) != receipt["source_ref"]
                or first.input_refs != [receipt["source_ref"]]
                or not isinstance(receipt["raw_sha256"], str)
                or _SHA256.fullmatch(receipt["raw_sha256"]) is None):
            return None
        raw_text = raw_bytes.decode("utf-8")
        if (capture_identity_sha256(fragment_id, raw_text)
                != receipt["identity_sha256"]):
            return None
        fields, body = _split_frontmatter(raw_text)
        if (fields.get("nigo-loop") != "true"
                or fields.get("privacy_level", "personal") not in {"public", "personal"}
                or fields.get("fragment_id", fragment_id) != fragment_id
                or fields.get("captured_at") != receipt["captured_at"]
                or body != captured.get("fragment_text")):
            return None
        seed = public_https_seed_url(raw_text)
        if seed is None:
            return None
        _safe_text(body, "goal", 20000)  # Validate only; user code/line breaks are not web text.
        goal = body
        title = _safe_text(first.fragment_title or " ".join(goal.split())[:100], "title", 120)
        return {
            "source_origin": "raw_capture", "title": title,
            "literal_summary": " ".join(goal.split())[:800], "goal": goal, "memory_basis": [],
            "input_digest": _sha256(_canonical({"origin": "raw_capture_v1",
                "fragment_id": fragment_id, "identity_sha256": receipt["identity_sha256"]})),
            "nigo_loop": True, "source_seed_url": seed,
        }
    except (OSError, ValueError, TypeError, KeyError):
        return None


class FragmentContinuationBridge:
    def __init__(
        self,
        review_service: ProductReviewService,
        *,
        loop_db: str | Path = DEFAULT_DB_PATH,
        prospective_public_after: datetime | None = None,
    ) -> None:
        self.review_service = review_service
        self.store = SQLiteCheckpointStore(loop_db)
        self.prospective_public_after = prospective_public_after
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, fragment_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(fragment_id, threading.Lock())

    def discover_intent_source(self, fragment_id: str) -> dict[str, object]:
        """Return authorized raw input or the existing organized projection.

        A qualified new raw capture always wins so later organizer output cannot
        replace the original question or its task identity. The lookup is
        deliberately limited to the frozen raw inbox and one-level
        category ``碎片整理`` / ``已整理碎片`` directories.  It never scans
        arbitrary Vault notes.
        """
        if _FRAGMENT_ID.fullmatch(fragment_id) is None:
            raise ContinuationBridgeError("invalid_body")
        root = self.review_service.vault_root
        raw_source = prospective_raw_source(
            self.store, root, fragment_id, self.prospective_public_after)
        if raw_source is not None:
            return raw_source
        raw_ref = f"Notes/散记/碎片想法/{fragment_id}.md"
        raw_path, raw_bytes = _regular_file(root, raw_ref)
        # 整理链按类别分流：多数类别进 ``碎片整理``，散记类进 ``已整理碎片``；
        # 两个目录都属 frozen 整理产物位置，仍要求全局恰好一个匹配。
        organized_matches = sorted(
            path
            for pattern in (
                f"*/碎片整理/{fragment_id}.md",
                f"*/已整理碎片/{fragment_id}.md",
            )
            for path in (root / "Notes").glob(pattern)
            if path.is_file() and not path.is_symlink()
        )
        if len(organized_matches) != 1:
            raise ContinuationBridgeError(
                "organized_not_ready" if not organized_matches else "source_mismatch"
            )
        organized_path = organized_matches[0]
        try:
            organized_path.resolve(strict=True).relative_to(root.resolve(strict=True))
            organized_bytes = organized_path.read_bytes()
        except (OSError, ValueError) as error:
            raise ContinuationBridgeError("source_unsafe") from error
        if len(raw_bytes) > _MAX_RAW_BYTES or len(organized_bytes) > _MAX_ORGANIZED_BYTES:
            raise ContinuationBridgeError("source_too_large")
        fragment = load_fragment(raw_path)
        if (
            fragment.fragment_id != fragment_id
            or fragment.admission_state.value != "requested"
        ):
            raise ContinuationBridgeError("source_mismatch")
        # 资格事实（TASK D rev2）：上方已真实校验 raw 碎片 frontmatter 的
        # nigo-loop: true（admission_state == requested），此处把该已验证
        # 事实显式传给 alignment 契约；缺失/非 True 一律默认不自动。
        fields, body = _frontmatter(organized_bytes)
        if (
            fields.get("type") != "已整理碎片"
            or fields.get("source_file") != f"{fragment_id}.md"
            or fields.get("pipeline_status") != "ready_to_review"
            or fields.get("experiment_status") != "ready"
        ):
            raise ContinuationBridgeError("organized_not_ready")
        title = _safe_text(fields.get("title"), "title", 100)
        goal = _safe_text(fields.get("goal"), "goal", 500)
        next_action = _safe_text(fields.get("next_action"), "next_action", 500)
        conclusion = ""
        match = re.search(
            r"(?:^|\n)## 结论先行\s*\n+(.+?)(?=\n## |\Z)", body, flags=re.S
        )
        if match:
            conclusion = " ".join(match.group(1).split())
        literal_summary = conclusion or f"目标：{goal}；下一步：{next_action}"
        if len(literal_summary) > 800:
            literal_summary = f"目标：{goal}；下一步：{next_action}"
        raw_text = raw_bytes.decode("utf-8")
        organized_text = organized_bytes.decode("utf-8")
        input_digest = _sha256(
            json.dumps(
                {"raw_text": raw_text, "organized_text": organized_text},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        result: dict[str, object] = {
            "title": title,
            "literal_summary": literal_summary,
            "goal": goal,
            "memory_basis": [],
            "input_digest": input_digest,
            "nigo_loop": True,
            # 自动承接边界判断用：整理完成时刻（ISO 字符串，来自整理链自身
            # 写入的 frontmatter；缺失时为空串——早于 cutover、按历史碎片
            # 处理，未通过校验时不参与任何信任决策）。
            "organized_at": (
                " ".join(str(fields.get("organized_at")).split())[:40]
                if isinstance(fields.get("organized_at"), str)
                and str(fields.get("organized_at")).strip()
                else ""
            ),
        }
        # rev14：入口元数据中已明确提供的公开来源链接——经既有 public
        # HTTPS/SSRF 校验并去查询参数后作为 source-bound seed candidate
        # 传给证据链；仅待核验候选，不信任来源身份、不外传私人正文。
        from fragment_loop.research_fetch import public_https_seed_url

        seed_url = public_https_seed_url(raw_text)
        if seed_url is not None:
            result["source_seed_url"] = seed_url
        return result

    def load_intent_source(
        self, fragment_id: str, input_digest: str
    ) -> dict[str, object]:
        source = self.discover_intent_source(fragment_id)
        if source["input_digest"] != input_digest:
            raise ContinuationBridgeError("source_changed")
        return source

    def list_statuses(self) -> list[dict[str, object]]:
        latest_by_fragment: dict[str, tuple[int, LoopCheckpoint]] = {}
        for run_id in self.store.run_ids_with_prefix(FRAGMENT_COGNITIVE_LOCAL_V1.loop_id):
            found = self.store.latest_with_sequence(run_id)
            if found is None:
                continue
            checkpoint, sequence = found
            if checkpoint.loop_id != FRAGMENT_COGNITIVE_LOCAL_V1.loop_id:
                continue
            current = latest_by_fragment.get(checkpoint.fragment_id)
            if current is None or sequence > current[0]:
                latest_by_fragment[checkpoint.fragment_id] = (sequence, checkpoint)
        result: list[dict[str, object]] = []
        for fragment_id, (sequence, checkpoint) in sorted(latest_by_fragment.items()):
            status = str(checkpoint.status)
            node = str(checkpoint.current_node)
            route = checkpoint.eval_results.get("cognitive_input", {})
            route_bound = isinstance(route, Mapping) and route.get("expected_route") is not None
            stage = (
                "candidate_source_ready"
                if status == "paused" and node == "human_decision"
                else "loop_processing"
                if route_bound or status == "running"
                else "loop_registered"
                if status == "approved" and node == "cognitive_contract"
                else "loop_stopped"
            )
            result.append(
                {
                    "fragment_id": fragment_id,
                    "status": status,
                    "current_node": node,
                    "sequence": sequence,
                    "stage": stage,
                    "updated_at": str(checkpoint.updated_at),
                }
            )
        return result

    def continue_fragment(self, raw: object) -> tuple[int, dict[str, Any]]:
        body = _validate_body(raw)
        fragment_id = body["fragment_id"]
        with self._lock(fragment_id):
            return self._continue_locked(body)

    def _continue_locked(self, body: dict[str, str]) -> tuple[int, dict[str, Any]]:
        fragment_id = body["fragment_id"]
        vault_root = self.review_service.vault_root
        raw_path, raw_bytes = _regular_file(vault_root, body["raw_ref"])
        organized_path, organized_bytes = _regular_file(vault_root, body["organized_ref"])
        if len(raw_bytes) > _MAX_RAW_BYTES or len(organized_bytes) > _MAX_ORGANIZED_BYTES:
            raise ContinuationBridgeError("source_too_large")
        if _sha256(raw_bytes) != body["raw_sha256"] or _sha256(organized_bytes) != body[
            "organized_sha256"
        ]:
            raise ContinuationBridgeError("source_changed")
        if tuple(body["raw_ref"].split("/")[-4:-1]) != _RAW_PARENT:
            raise ContinuationBridgeError("source_unsafe")
        if raw_path.stem != fragment_id or organized_path.stem != fragment_id:
            raise ContinuationBridgeError("source_mismatch")
        organized_parts = body["organized_ref"].split("/")
        if len(organized_parts) < 4 or organized_parts[-2] != "碎片整理":
            raise ContinuationBridgeError("source_unsafe")

        fragment = load_fragment(raw_path)
        if fragment.fragment_id != fragment_id or fragment.admission_state.value != "requested":
            raise ContinuationBridgeError("fragment_not_approved")
        fields, _organized_body = _frontmatter(organized_bytes)
        if (
            fields.get("type") != "已整理碎片"
            or fields.get("source_file") != raw_path.name
            or fields.get("pipeline_status") != "ready_to_review"
            or fields.get("experiment_status") != "ready"
        ):
            raise ContinuationBridgeError("organized_not_ready")
        title = _safe_text(fields.get("title"), "title", 100)
        category = _safe_text(fields.get("category"), "category", 100)
        goal = _safe_text(fields.get("goal"), "goal", 500)
        next_action = _safe_text(fields.get("next_action"), "next_action", 500)

        latest = self.store.latest_for_fragment(
            FRAGMENT_COGNITIVE_LOCAL_V1.loop_id, fragment_id
        )
        if latest is None:
            raise ContinuationBridgeError("loop_not_registered")
        expansion = _semantic_expansion(fragment.raw_content, title, goal)
        result = _cognitive_result(
            fragment.raw_content, title, goal, next_action, expansion
        )
        service = LocalCognitiveV1Loop(self.store, lambda _text, _route: result)
        input_value = latest.eval_results.get("cognitive_input")
        current_route = (
            input_value.get("expected_route")
            if isinstance(input_value, Mapping)
            else None
        )
        if current_route is None:
            latest = service.bind_route(
                latest.run_id,
                "research",
                reason=str(result["route_reason"]),
                semantic_expansion=expansion,
            )
        elif current_route != "research":
            raise ContinuationBridgeError("route_conflict")
        latest = service.run(latest.run_id)
        if latest.status != "paused" or latest.current_node != "human_decision":
            raise ContinuationBridgeError("loop_not_settled")
        if latest.eval_results.get("cognitive_contract_status") != "validated":
            raise ContinuationBridgeError("loop_contract_invalid")
        sequence = self.store.latest_sequence(latest.run_id)
        if sequence is None:
            raise ContinuationBridgeError("loop_state_lost")

        bundle = build_cognitive_product(
            latest.eval_results,
            _product(fragment_id, title, category, goal, next_action),
        )
        binding = {
            "version": CONTINUATION_VERSION,
            "fragment_id": fragment_id,
            "raw_ref": body["raw_ref"],
            "organized_ref": body["organized_ref"],
            "raw_sha256": body["raw_sha256"],
            "organized_sha256": body["organized_sha256"],
            "route": "research",
            "loop_sequence": sequence,
        }
        base, payloads = _candidate_payloads(fragment_id, bundle, binding)
        candidate_id = f"fragment-{fragment_id}-product-candidate-v1"
        publish_status = _publish_candidate(
            self.review_service.candidates_root / candidate_id, payloads
        )
        projection = next(
            (
                item
                for item in self.review_service.list_reviews()
                if item.get("candidate_id") == candidate_id
            ),
            None,
        )
        if projection is None or projection.get("has_conflict") is not False:
            raise ContinuationBridgeError("candidate_projection_failed")
        return (200 if publish_status == "already_published" else 201), {
            **projection,
            "continuation_status": publish_status,
            "loop_sequence": sequence,
            "bundle_name": base,
        }


__all__ = [
    "CONTINUATION_BODY_KEYS",
    "CONTINUATION_HEADER",
    "CONTINUATION_VERSION",
    "ContinuationBridgeError",
    "FragmentContinuationBridge",
]
