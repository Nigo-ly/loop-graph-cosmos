"""Graph Phase 2B 唯一合成调用：live Pilot spec、受控 DeepSeek Transport 与一次性编排。

冻结依据：`GRAPH-PHASE2B-EXECUTION-FREEZE.md` 与 `TASK-GRAPH-PHASE2B.md`（2026-08-05）。
本模块只承载一次被精确授权的真实调用所需的最小后端能力：

- Phase 2B v2 Pilot GraphSpec `fragment-cognitive-agent-pilot-v2`（不触碰
  `fragment-cognitive-v1`、`fragment-cognitive-agent-pilot-v1` 与 GraphSpec v1 schema）；
- 精确输入/预算/授权绑定：输入固定为 398 UTF-8 bytes、SHA-256 冻结；任何 byte、摘要、
  Provider、模型、host、path、Keychain service、预算、授权或时效漂移都在读取凭据和
  发送前失败；
- 官方 DeepSeek 价格页 preflight：模型必须仍在页面列出，按「页面最坏单价的保守上界 ×
  输入+输出 token 上限 × 高峰两倍 × 保守汇率上界」核算，超过人民币 1 元或无法核验即
  零调用；调用当时采用的价格快照与计算被保存；
- 单次 HTTPS Transport：stdlib only，pin `api.deepseek.com` + `/v1/chat/completions`，
  一个请求、零重试、不 redirect、不代理、不流式、不 tool call，只接受 JSON Object；
- envelope + payload + 语义校验：精确键集合 `summary/unknowns/next_checks`，拒绝密钥
  形状、URL/写入指令与「已验证/已证实」类证据升级；
- Keychain reader 注入：只读精确 service `p4a-gate2-r9-provider-deepseek`，且只有在
  原子预留成功之后才允许读取；
- 一次性执行编排：pre_call 备份+验证 → 调用前人闸确认 → 价格 preflight → 原子预留 →
  读取凭据 → 最多一次发送 → 账本先写 → Checkpoint 后交 → 调用后人闸 → terminal 备份
  → 全新路径恢复演练。

本模块绝不持久化授权原文或 API key；日志/异常/投影不含 Prompt/响应正文/key。
生产接缝（真实 HTTPS、Keychain、价格页抓取）全部构造方注入；离线测试使用合成接缝。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NoReturn, Protocol

from common.checkpoint import SQLiteCheckpointStore
from graph_runtime.agent_adapter import TransportError, TransportRequest, TransportResponse
from graph_runtime.agent_backup import create_backup, restore_backup, verify_backup
from graph_runtime.agent_ledger import (
    ATTACH_OK,
    MAX_DB_BYTES,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RESERVED,
    AgentCallRecord,
    AgentExecutionLink,
    AgentLedgerStore,
    RequestSent,
)
from graph_runtime.agent_ledger import (
    ERROR_CATEGORIES as LEDGER_ERROR_CATEGORIES,
)
from graph_runtime.agent_policy import (
    AgentPolicy,
    AuthorizationReceipt,
    authorization_digest_of,
    validate_receipt_binding,
)
from graph_runtime.runtime import (
    GraphRuntime,
    NodeRequest,
    NodeResult,
    flatten_inputs,
    human_decision_id,
)
from graph_runtime.spec import GraphSpec, canonical_json, digest_of, validate_graph_spec

# ---------------------------------------------------------------------------
# 冻结身份（GRAPH-PHASE2B-EXECUTION-FREEZE §1/§2）
# ---------------------------------------------------------------------------

PILOT2B_GRAPH_ID = "fragment-cognitive-agent-pilot-v2"
PILOT2B_POLICY_PROFILE = "graph-phase2b-agent-live-pilot"
PILOT2B_AGENT_ADAPTER = "agent.live_drafter"

PRE_GATE = "pre_call_gate"
PREP_NODE = "call_preparation"
AGENT_NODE = "agent_live_drafter"
VALIDATOR_NODE = "output_validator"
POST_GATE = "post_call_gate"
DRAFT_OUTPUT = "draft_output"
ABORT_OUTPUT = "run_aborted"
REJECT_OUTPUT = "run_rejected"

PROVIDER = "deepseek_v4_pro"
MODEL = "deepseek-v4-pro"
OFFICIAL_HOST = "api.deepseek.com"
OFFICIAL_PATH = "/v1/chat/completions"
KEYCHAIN_SERVICE = "p4a-gate2-r9-provider-deepseek"
PHASE2B_ALLOWED_MODELS = frozenset(((PROVIDER, MODEL),))

# 唯一授权输入：398 UTF-8 bytes，SHA-256 冻结。任何漂移都是新身份。
EXACT_INPUT = (
    "请基于以下非私人合成材料生成一份未验证的认知草稿：Graph 的控制面负责确定性路由、"
    "并行汇合、有界反馈、暂停恢复和人工闸门；记忆面只提供只读上下文。只输出 JSON，"
    "字段为 summary（字符串）、unknowns（字符串数组）、next_checks（字符串数组）。"
    "不得声称已验证事实，不得提出或执行外部写入。"
)
EXACT_INPUT_BYTES = 398
EXACT_INPUT_SHA256 = "2b1310db2c62d588b08e4f21af7d5ef40766c4cde667ccc8dd74430690afeb96"

# 硬预算（§2）：max_calls=1、输入 2000 / 输出 1000 tokens、请求 16 KiB、
# 响应 64 KiB、墙钟 120 秒。
MAX_INPUT_TOKENS = 2000
MAX_OUTPUT_TOKENS = 1000
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
WALL_CLOCK_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 10

# 价格门（§2）：硬成本上限人民币 1 元；高峰按官方预告两倍计；汇率取保守上界
# 10 CNY/USD（仅用于把官方美元单价换算成人民币上界，快照中记录）。
MAX_COST_CNY = 1.0
PEAK_MULTIPLIER = 2.0
CNY_PER_USD_CEILING = 10.0
PRICING_HOST = "api-docs.deepseek.com"
PRICING_PATH = "/quick_start/pricing/"
PRICING_PAGE_URL = f"https://{PRICING_HOST}{PRICING_PATH}"
PRICING_PAGE_MAX_BYTES = 2 * 1024 * 1024
PRICING_FETCH_TIMEOUT_SECONDS = 15

# 结果只能固定为草稿、未验证、无外部写入（§3.6）。
DRAFT_EVIDENCE_NOTE = "draft + unverified + no_external_write"

# 冻结授权身份（§1）：授权原文永不进入源码/账本/投影；这里只钉住它的
# SHA-256 绑定摘要与失效上限（2026-08-06 12:00 Asia/Shanghai = 04:00 UTC）。
# 任何授权或时效漂移都在读取凭据和发送前失败。
EXPECTED_AUTHORIZATION_DIGEST = (
    "d293768b31238095aa914d96e732355d1bc56691c978a4919ff9abd0cfc38900"
)
AUTHORIZATION_NOT_AFTER = "2026-08-06T04:00:00+00:00"

# 双闸门 decision origin：固定为 nigo 当前任务预授权（不是实时点击），随
# run_inputs 持久化并在 record/execute/finalize 复核；投影只含该固定来源与
# 授权摘要，绝不含授权原文。
DECISION_ORIGIN = "nigo/current_task_preauthorization"

# 响应 payload 的精确键集合与有界上限（§4）。
PAYLOAD_SCHEMA = {"summary": "string", "unknowns": "array", "next_checks": "array"}
MAX_SUMMARY_CHARS = 2000
MAX_ARRAY_ITEMS = 16
MAX_ITEM_CHARS = 500
MAX_PAYLOAD_BYTES = 8 * 1024

# 语义门闭集（§4）：密钥形状、URL/外部写入指令、证据升级声称。
_SECRET_MARKERS = (
    "sk-",
    "bearer ",
    "-----begin",
    "akia",
    "api_key",
    "apikey",
    "api-key",
    "secret_key",
    "private_key",
    "password",
    "passwd",
    "密码",
    "私钥",
    "凭据",
)
_URL_MARKERS = ("http://", "https://", "www.")
_WRITE_MARKERS = (
    "写入",
    "保存到",
    "发布",
    "部署",
    "上传",
    "发送到",
    "创建文件",
    "修改文件",
    "删除文件",
    "git commit",
    "git push",
    "curl ",
    "wget ",
    "rm -",
)
_ESCALATION_MARKERS = (
    "已验证",
    "已被验证",
    "已证实",
    "经证实",
    "已确认",
    "事实",
    "fact",
    "verified",
    "proven",
    "confirmed",
)

_EDGE_SEQUENCE = {"type": "sequence", "decision_source": "declared"}
_GATE_BINDING = [
    "run_id",
    "node_id",
    "decision",
    "spec_digest",
    "input_digest",
    "expected_sequence",
    "requester",
    "decision_id",
]


class PilotExecutionError(RuntimeError):
    """编排/边界失败的稳定机器码；任何一项失败都禁止继续向发送方向前进。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise PilotExecutionError(code)


# ---------------------------------------------------------------------------
# Phase 2B v2 Pilot GraphSpec
# ---------------------------------------------------------------------------


def pilot2b_raw() -> dict[str, Any]:
    return {
        "schema_version": "executable-agent-graph-spec-v1",
        "graph_id": PILOT2B_GRAPH_ID,
        "version": "2.0.0",
        "goal": (
            "Graph Phase 2B 唯一合成调用 Pilot：双人工闸门、单 live Agent 节点、"
            "确定性 schema/语义校验、有界反馈、草稿且未验证输出"
        ),
        "entry_node": "pilot_input",
        "nodes": [
            {
                "id": "pilot_input",
                "kind": "input",
                "adapter": "fixture.pilot2b_input",
                "input_schema": {},
                "output_schema": {"sanitized_text": "string", "text_digest": "string"},
            },
            {
                "id": PRE_GATE,
                "kind": "human_decision",
                "human_gate": {
                    "allowed_decisions": ["approve_call", "abort"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
                "input_schema": {"sanitized_text": "string"},
                "output_schema": {"decision": "string"},
            },
            {
                "id": PREP_NODE,
                "kind": "capability",
                "adapter": "fixture.pilot2b_preparation",
                "input_schema": {"decision": "string"},
                "output_schema": {
                    "sanitized_text": "string",
                    "text_digest": "string",
                    "decision": "string",
                },
            },
            {
                "id": AGENT_NODE,
                "kind": "capability",
                "adapter": PILOT2B_AGENT_ADAPTER,
                "input_schema": {"decision": "string"},
                "output_schema": dict(PAYLOAD_SCHEMA),
            },
            {
                "id": VALIDATOR_NODE,
                "kind": "validator",
                "adapter": "fixture.pilot2b_validator",
                "input_schema": {"summary": "string"},
                "output_schema": {
                    "valid": "boolean",
                    "summary": "string",
                    "unknowns": "array",
                    "next_checks": "array",
                    "evidence_note": "string",
                },
            },
            {
                "id": POST_GATE,
                "kind": "human_decision",
                "human_gate": {
                    "allowed_decisions": ["accept_draft", "reject"],
                    "timeout_policy": "pause",
                    "binding": list(_GATE_BINDING),
                },
                "input_schema": {"valid": "boolean"},
                "output_schema": {"decision": "string"},
            },
            {
                "id": DRAFT_OUTPUT,
                "kind": "output",
                "input_schema": {"decision": "string"},
                "output_schema": {},
            },
            {
                "id": ABORT_OUTPUT,
                "kind": "output",
                "input_schema": {"decision": "string"},
                "output_schema": {},
            },
            {
                "id": REJECT_OUTPUT,
                "kind": "output",
                "input_schema": {"decision": "string"},
                "output_schema": {},
            },
        ],
        "edges": [
            {
                "id": "e_input_pre_gate",
                "from": "pilot_input",
                "to": PRE_GATE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_pre_gate_approve",
                "from": PRE_GATE,
                "to": PREP_NODE,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "approve_call"},
                "priority": 0,
                "decision_source": "human_selected",
            },
            {
                "id": "e_pre_gate_abort",
                "from": PRE_GATE,
                "to": ABORT_OUTPUT,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "abort"},
                "priority": 1,
                "decision_source": "human_selected",
            },
            {
                "id": "e_preparation_agent",
                "from": PREP_NODE,
                "to": AGENT_NODE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_agent_validator",
                "from": AGENT_NODE,
                "to": VALIDATOR_NODE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_validator_feedback",
                "from": VALIDATOR_NODE,
                "to": AGENT_NODE,
                "type": "feedback",
                "condition": {"field": "error", "op": "equals", "value": "agent_output_invalid"},
                "max_traversals": 1,
                "on_exhausted": "pause",
                "decision_source": "rule_evaluated",
            },
            {
                "id": "e_validator_post_gate",
                "from": VALIDATOR_NODE,
                "to": POST_GATE,
                **_EDGE_SEQUENCE,
            },
            {
                "id": "e_post_gate_accept",
                "from": POST_GATE,
                "to": DRAFT_OUTPUT,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "accept_draft"},
                "priority": 0,
                "decision_source": "human_selected",
            },
            {
                "id": "e_post_gate_reject",
                "from": POST_GATE,
                "to": REJECT_OUTPUT,
                "type": "condition",
                "condition": {"field": "decision", "op": "equals", "value": "reject"},
                "priority": 1,
                "decision_source": "human_selected",
            },
        ],
        "budgets": {"max_node_executions": 32},
        "policy_profile": PILOT2B_POLICY_PROFILE,
        "subgraphs": [],
    }


def build_phase2b_pilot_spec() -> GraphSpec:
    return validate_graph_spec(pilot2b_raw())


def exact_input_sha256() -> str:
    return hashlib.sha256(EXACT_INPUT.encode("utf-8")).hexdigest()


def expected_agent_input_digest(decision: str = "approve_call") -> str:
    """Agent 节点规范化输入摘要（flatten 后的精确形状）。"""
    result: str = digest_of(
        {
            "sanitized_text": EXACT_INPUT,
            "text_digest": exact_input_sha256(),
            "decision": decision,
        }
    )
    return result


def make_live_policy() -> AgentPolicy:
    """Phase 2B 冻结预算与身份契约：live_enabled=True，max_calls=1。"""
    return AgentPolicy(
        adapter=PILOT2B_AGENT_ADAPTER,
        provider=PROVIDER,
        model=MODEL,
        capability="draft_synthesis",
        max_calls=1,
        max_input_tokens=MAX_INPUT_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_input_bytes=MAX_REQUEST_BYTES,
        timeout_seconds=WALL_CLOCK_SECONDS,
        live_enabled=True,
    )


# ---------------------------------------------------------------------------
# 响应 payload：精确 schema + 语义门
# ---------------------------------------------------------------------------


def _has_control_chars(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def semantic_violation(text: str) -> str | None:
    """语义门：密钥形状、URL/外部写入指令、证据升级声称；返回稳定子类别。"""
    lowered = text.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "semantic_secret_material"
    if any(marker in lowered for marker in _URL_MARKERS):
        return "semantic_url_or_endpoint"
    if any(marker in lowered for marker in _WRITE_MARKERS):
        return "semantic_write_instruction"
    if any(marker in lowered for marker in _ESCALATION_MARKERS):
        return "semantic_evidence_escalation"
    return None


def validate_draft_payload(payload: Any) -> str | None:
    """精确键集合、类型、有界长度与语义门；返回 None 或稳定错误码。"""
    if not isinstance(payload, dict):
        return "invalid_response:not_object"
    if set(payload) != set(PAYLOAD_SCHEMA):
        return "invalid_response:keys"
    if len(canonical_json(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return "response_too_large"
    summary = payload["summary"]
    if not isinstance(summary, str) or not summary.strip():
        return "invalid_response:summary"
    if len(summary) > MAX_SUMMARY_CHARS or _has_control_chars(summary):
        return "invalid_response:summary"
    for field_name in ("unknowns", "next_checks"):
        items = payload[field_name]
        if not isinstance(items, list) or len(items) > MAX_ARRAY_ITEMS:
            return f"invalid_response:{field_name}"
        for item in items:
            if not isinstance(item, str) or not item.strip():
                return f"invalid_response:{field_name}"
            if len(item) > MAX_ITEM_CHARS or _has_control_chars(item):
                return f"invalid_response:{field_name}"
    texts = [summary, *payload["unknowns"], *payload["next_checks"]]
    for text in texts:
        violation = semantic_violation(text)
        if violation is not None:
            return violation
    return None


def make_pilot2b_fixture_adapters() -> dict[str, Any]:
    """v2 图的确定性节点：输入、调用准备与输出 schema/语义校验器。

    校验器对 Agent 结果做第二层确定性复核；失败固定 ``agent_output_invalid``
    （反馈边最多一次，耗尽暂停，绝不自动再发）。
    """

    def pilot_input(_request: NodeRequest) -> NodeResult:
        return NodeResult(
            output={"sanitized_text": EXACT_INPUT, "text_digest": exact_input_sha256()}
        )

    def call_preparation(request: NodeRequest) -> NodeResult:
        decision = str(request.inputs.get(PRE_GATE, {}).get("decision", ""))
        return NodeResult(
            output={
                "sanitized_text": EXACT_INPUT,
                "text_digest": exact_input_sha256(),
                "decision": decision,
            }
        )

    def output_validator(request: NodeRequest) -> NodeResult:
        agent_output = request.inputs.get(AGENT_NODE, {})
        payload = {
            "summary": agent_output.get("summary"),
            "unknowns": agent_output.get("unknowns"),
            "next_checks": agent_output.get("next_checks"),
        }
        error = validate_draft_payload(payload)
        if error is not None:
            return NodeResult(
                output={"weakest_link": f"草稿未通过确定性校验：{error}"},
                failure="agent_output_invalid",
            )
        return NodeResult(
            output={
                "valid": True,
                "summary": payload["summary"],
                "unknowns": payload["unknowns"],
                "next_checks": payload["next_checks"],
                "evidence_note": DRAFT_EVIDENCE_NOTE,
            }
        )

    return {
        "fixture.pilot2b_input": pilot_input,
        "fixture.pilot2b_preparation": call_preparation,
        "fixture.pilot2b_validator": output_validator,
    }


# ---------------------------------------------------------------------------
# 官方 DeepSeek 价格页 preflight
# ---------------------------------------------------------------------------


def fetch_pricing_page(
    *,
    resolver: Callable[[str], frozenset[str]] | None = None,
    connection_factory: Callable[
        [str, int, float, ssl.SSLContext], _ConnectionLike
    ] | None = None,
) -> str:
    """生产价格页抓取器：direct ``HTTPSConnection``，固定官方 host/path。

    零代理（``http.client`` 不读任何代理环境变量）、3xx 一律拒绝（不跟随）、
    2 MiB 大小上限、15 秒超时、默认 TLS context 校验官方 hostname、DNS 解析
    与 TLS 对端地址交叉核验；任何漂移或异常 fail-closed（零调用）。只用于
    Codex 执行的唯一真实调用；离线测试注入假连接，绝不联网。
    """
    resolve = resolver or _default_resolver
    factory = connection_factory or _default_connection_factory
    try:
        addresses = resolve(PRICING_HOST)
    except Exception as error:
        raise PilotExecutionError("price_page_unavailable") from error
    if not addresses:
        _fail("price_page_unavailable")
    context = ssl.create_default_context()
    connection = factory(PRICING_HOST, 443, float(PRICING_FETCH_TIMEOUT_SECONDS), context)
    try:
        try:
            connection.connect()
        except (ssl.SSLError, OSError, http.client.HTTPException) as error:
            raise PilotExecutionError("price_page_unavailable") from error
        sock = connection.sock
        peer = sock.getpeername()[0] if sock is not None else None
        if peer not in addresses:
            _fail("price_page_unavailable")
        if sock is not None:
            sock.settimeout(float(PRICING_FETCH_TIMEOUT_SECONDS))
        try:
            connection.request(
                "GET",
                PRICING_PATH,
                body=b"",
                headers={
                    "Host": PRICING_HOST,
                    "User-Agent": "graph-phase2b-preflight",
                    "Accept": "text/html",
                },
            )
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            raise PilotExecutionError("price_page_unavailable") from error
        status = int(response.status)
        if status in (301, 302, 303, 307, 308):
            # 禁止 redirect：官方页必须直接 200。
            _fail("price_page_redirect_rejected")
        if status != 200:
            _fail("price_page_unavailable")
        try:
            raw = response.read(PRICING_PAGE_MAX_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise PilotExecutionError("price_page_unavailable") from error
        if len(raw) > PRICING_PAGE_MAX_BYTES:
            _fail("price_page_too_large")
        text: str = raw.decode("utf-8", errors="replace")
        return text
    finally:
        connection.close()


class _PricingTableParser(HTMLParser):
    """把页面解析为「表格 → 行 → (单元格文本, 是否 th)」的纯结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, bool]]]] = []
        self._table_depth = 0
        self._current_table: list[list[tuple[str, bool]]] | None = None
        self._current_row: list[tuple[str, bool]] | None = None
        self._current_cell: list[str] | None = None
        self._cell_is_header = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._table_depth == 0:
                self._current_table = []
            self._table_depth += 1
        elif self._current_table is not None and tag == "tr":
            self._current_row = []
        elif self._current_row is not None and tag in ("th", "td"):
            self._current_cell = []
            self._cell_is_header = tag == "th"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._current_cell is not None:
            assert self._current_row is not None
            text = " ".join("".join(self._current_cell).split())
            self._current_row.append((text, self._cell_is_header))
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            assert self._current_table is not None
            self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._table_depth > 0:
            self._table_depth -= 1
            if self._table_depth == 0 and self._current_table is not None:
                self.tables.append(self._current_table)
                self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)


def _strict_usd_amount(cell_text: str) -> float | None:
    """单元格必须含恰好一个 ``$金额``；返回其值或 None。"""
    amounts = re.findall(r"\$(\d+(?:\.\d+)?)", cell_text)
    if len(amounts) != 1:
        return None
    return float(amounts[0])


def _price_row_category(cell_text: str) -> str | None:
    """价格行标签匹配（冻结官方表结构）：返回类别或 None。"""
    label = cell_text.lower()
    if "cache hit" in label:
        return "cache_hit"
    if "cache miss" in label:
        return "cache_miss"
    if "output tokens" in label and "cache" not in label:
        return "output"
    return None


def extract_model_prices_usd(page_text: str, *, model: str = MODEL) -> dict[str, float]:
    """用 stdlib HTMLParser 定位官方价格表，读取该模型列的三个价格行。

    只锚定「首单元格文本精确为 ``MODEL``」的唯一表头行（官方页全部单元格
    都是 td，绝不依赖 th；``MODEL VERSION`` 等噪声行不会被误当表头）；
    模型名在表头行中必须恰好出现一次，其位置（去掉首列角标后）映射到
    每个价格行「类别标签单元格之后」的值列下标——价格行可能因 rowspan
    携带额外的行组单元格（如官方页的 ``PRICING(2)``），所以绝不用固定
    列下标。只读取 ``cache hit`` / ``cache miss`` / ``output tokens``
    三个价格行中严格的 ``$金额``。模型不存在、模型在表头行重复、候选
    表头多于一个、三项不齐、行重复、金额缺失/非正/异常均 fail-closed；
    context/concurrency/日期/时间等噪声单元格永不进入核算。
    """
    if not isinstance(page_text, str) or not page_text:
        _fail("price_page_unavailable")
    parser = _PricingTableParser()
    parser.feed(page_text)
    parser.close()

    header_index: int | None = None
    target_rows: list[list[tuple[str, bool]]] | None = None
    for table in parser.tables:
        for row in table:
            if not row or row[0][0] != "MODEL":
                continue
            matches = [
                index for index, (text, _is_header) in enumerate(row) if text == model
            ]
            if len(matches) > 1:
                # 模型在表头行重复：歧义，拒绝。
                _fail("price_model_ambiguous")
            if not matches:
                continue
            if target_rows is not None:
                # 多个候选表/表头行含该模型：歧义，拒绝。
                _fail("price_model_ambiguous")
            header_index = matches[0]
            target_rows = table
    if target_rows is None or header_index is None:
        _fail("price_model_not_listed")
    if header_index < 1:
        # header 首列必须是角标（如 MODEL），模型列从第二列起。
        _fail("price_model_not_listed")
    value_position = header_index - 1

    prices: dict[str, float] = {}
    for row in target_rows:
        label_index: int | None = None
        category: str | None = None
        for index, (text, _is_header) in enumerate(row):
            category = _price_row_category(text)
            if category is not None:
                label_index = index
                break
        if label_index is None or category is None:
            continue
        if category in prices:
            _fail("price_row_duplicate")
        value_cells = row[label_index + 1 :]
        if len(value_cells) <= value_position:
            _fail("price_not_found")
        amount = _strict_usd_amount(value_cells[value_position][0])
        if amount is None:
            _fail("price_not_found")
        if amount <= 0 or amount >= 1000:
            _fail("price_implausible")
        prices[category] = amount
    if set(prices) != {"cache_hit", "cache_miss", "output"}:
        _fail("price_not_found")
    return prices


def extract_worst_price_usd(page_text: str, *, model: str = MODEL) -> float:
    """三个价格行的最坏（最大）每百万 token 美元单价（保守上界）。"""
    return max(extract_model_prices_usd(page_text, model=model).values())


def evaluate_price_gate(
    page_text: str,
    *,
    fetched_at: str,
    page_url: str = PRICING_PAGE_URL,
) -> dict[str, Any]:
    """价格门核算与快照：最坏单价 × (输入+输出上限) × 高峰两倍 × 汇率上界。

    返回的快照必须随调用证据保存；``within_budget=False`` 时调用方零发送。
    """
    worst_price = extract_worst_price_usd(page_text)
    worst_case_usd = (
        worst_price * (MAX_INPUT_TOKENS + MAX_OUTPUT_TOKENS) / 1_000_000 * PEAK_MULTIPLIER
    )
    worst_case_cny = worst_case_usd * CNY_PER_USD_CEILING
    return {
        "schema": "graph-phase2b-price-snapshot-v1",
        "fetched_at_utc": fetched_at,
        "page_url": page_url,
        "page_sha256": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
        "model": MODEL,
        "worst_price_usd_per_million": worst_price,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "peak_multiplier": PEAK_MULTIPLIER,
        "cny_per_usd_ceiling": CNY_PER_USD_CEILING,
        "worst_case_usd": worst_case_usd,
        "worst_case_cny": worst_case_cny,
        "max_cost_cny": MAX_COST_CNY,
        "within_budget": worst_case_cny < MAX_COST_CNY,
    }


# ---------------------------------------------------------------------------
# Keychain reader（精确 service，预留之后才允许调用）
# ---------------------------------------------------------------------------


def production_credential_reader(service: str) -> str:
    """生产 Keychain 边界：只读精确冻结 service，凭据只在内存。

    其它任何 service、环境变量或配置文件凭据一律拒绝。复用已验收的
    ``SecurityCommandCredentialReader``（/usr/bin/security 只读形状）。
    """
    if service != KEYCHAIN_SERVICE:
        _fail("credential_service_rejected")
    from system_governance.gate2_r9 import SecurityCommandCredentialReader

    try:
        credential = SecurityCommandCredentialReader()(service)
    except Exception as error:
        raise PilotExecutionError("credential_unavailable") from error
    if not isinstance(credential, str) or not credential:
        _fail("credential_unavailable")
    return credential


def forbidden_credential_reader(service: str) -> str:
    """离线接缝：任何凭据读取企图都 fail-closed。"""
    raise PilotExecutionError("credential_read_forbidden")


def forbidden_price_fetcher() -> str:
    """离线接缝：任何联网抓取企图都 fail-closed。"""
    raise PilotExecutionError("price_fetch_forbidden")


# ---------------------------------------------------------------------------
# 单次 HTTPS Transport（stdlib only；不重试、不 redirect、不流式）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveTransportOutcome:
    """一次发送尝试的完整可归因结果；``response`` 为 None 时必有错误类别。"""

    request_sent: RequestSent
    response: TransportResponse | None
    error_category: str | None
    http_status: int | None = None


class _SocketLike(Protocol):
    def getpeername(self, /) -> tuple[str, int]: ...

    def settimeout(self, value: float, /) -> None: ...


class _ResponseLike(Protocol):
    @property
    def status(self) -> int: ...

    def read(self, amt: int, /) -> bytes: ...


class _ConnectionLike(Protocol):
    """最小 HTTPS 连接面；HTTPSConnection 与离线假连接都结构化满足。"""

    @property
    def sock(self) -> _SocketLike | None: ...

    def connect(self, /) -> None: ...

    def request(
        self,
        method: str,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> None: ...

    def getresponse(self, /) -> _ResponseLike: ...

    def close(self, /) -> None: ...


def _default_resolver(host: str) -> frozenset[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise PilotExecutionError("dns_resolution_failed") from error
    return frozenset(str(info[4][0]) for info in infos)


def _default_connection_factory(
    host: str, port: int, timeout: float, context: ssl.SSLContext
) -> _ConnectionLike:
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)


def build_request_body(prompt: str, max_output_tokens: int) -> bytes:
    """冻结请求体：精确模型、单条 user 消息、JSON Object 响应、不流式、无工具。"""
    body: bytes = canonical_json(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
            "thinking": {"type": "disabled"},
        }
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise TransportError("request_too_large")
    return body


def _parse_envelope(decoded: Any) -> TransportResponse:
    """DeepSeek chat.completions envelope：模型声明、usage 与 content JSON。

    结构性失败全部归入 ``invalid_response``；身份漂移（model 值不符）留给
    Adapter 以 ``model_mismatch`` 固定类别拒绝。
    """
    if not isinstance(decoded, dict):
        raise TransportError("envelope_not_object")
    model = decoded.get("model")
    usage = decoded.get("usage")
    choices = decoded.get("choices")
    if not isinstance(model, str) or not isinstance(usage, dict):
        raise TransportError("envelope_invalid")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise TransportError("envelope_invalid")
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if (
        not isinstance(prompt_tokens, int)
        or isinstance(prompt_tokens, bool)
        or prompt_tokens < 0
    ):
        raise TransportError("envelope_invalid")
    if (
        not isinstance(completion_tokens, int)
        or isinstance(completion_tokens, bool)
        or completion_tokens < 0
    ):
        raise TransportError("envelope_invalid")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise TransportError("envelope_invalid")
    try:
        payload = json.loads(message["content"])
    except json.JSONDecodeError as error:
        raise TransportError("envelope_content_not_json") from error
    if not isinstance(payload, dict):
        raise TransportError("envelope_content_not_json")
    return TransportResponse(
        declared_provider=PROVIDER,
        declared_model=model,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        payload=payload,
    )


class DeepSeekHttpsTransport:
    """生产 HTTPS Transport：一个请求、零重试、不 redirect、不代理、不流式。

    host/path 是模块常量而非参数；DNS 解析与 TLS 对端地址交叉核验；响应
    超过 64 KiB、非 200、重定向、无法解析或内容不是 JSON Object 都以固定
    类别拒绝。发送前本地违规（空凭据、请求体超限）抛 ``TransportError``
    （账本记 request_sent=false）。
    """

    def __init__(
        self,
        *,
        resolver: Callable[[str], frozenset[str]] = _default_resolver,
        connection_factory: Callable[
            [str, int, float, ssl.SSLContext], _ConnectionLike
        ] = _default_connection_factory,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._monotonic = monotonic

    def send_once(self, request: TransportRequest, credential: str) -> LiveTransportOutcome:
        if not isinstance(credential, str) or not credential:
            raise TransportError("credential_unavailable")
        body = build_request_body(request.prompt, request.max_output_tokens)
        total_budget = float(request.timeout_seconds)
        try:
            addresses = self._resolver(OFFICIAL_HOST)
        except Exception:
            return LiveTransportOutcome("false", None, "transport_error")
        if not addresses:
            return LiveTransportOutcome("false", None, "transport_error")
        context = ssl.create_default_context()
        started = self._monotonic()

        def remaining() -> float:
            return total_budget - (self._monotonic() - started)

        connection = self._connection_factory(
            OFFICIAL_HOST, 443, float(CONNECT_TIMEOUT_SECONDS), context
        )
        try:
            try:
                connection.connect()
            except ssl.SSLError:
                return LiveTransportOutcome("false", None, "transport_error")
            except (OSError, http.client.HTTPException):
                # 连接期异常无法证明发送未发生：unknown。
                return LiveTransportOutcome("unknown", None, "transport_exception")
            sock = connection.sock
            peer = sock.getpeername()[0] if sock is not None else None
            if peer not in addresses:
                return LiveTransportOutcome("false", None, "transport_error")
            if remaining() <= 0:
                return LiveTransportOutcome("false", None, "timeout")
            if sock is not None:
                sock.settimeout(remaining())
            try:
                connection.request(
                    "POST",
                    OFFICIAL_PATH,
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {credential}",
                    },
                )
            except (OSError, http.client.HTTPException):
                return LiveTransportOutcome("unknown", None, "transport_exception")
            if remaining() <= 0:
                return LiveTransportOutcome("true", None, "timeout")
            if sock is not None:
                sock.settimeout(remaining())
            try:
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                # 请求已发出但响应未知：发送事实成立。
                return LiveTransportOutcome("true", None, "transport_exception")
            status = int(response.status)
            if status in (301, 302, 303, 307, 308):
                return LiveTransportOutcome("true", None, "invalid_response", status)
            if status != 200:
                return LiveTransportOutcome("true", None, "invalid_response", status)
            if remaining() <= 0:
                return LiveTransportOutcome("true", None, "timeout", status)
            if sock is not None:
                sock.settimeout(remaining())
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                return LiveTransportOutcome("true", None, "transport_exception", status)
            if len(raw) > MAX_RESPONSE_BYTES:
                return LiveTransportOutcome("true", None, "response_too_large", status)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return LiveTransportOutcome("true", None, "invalid_response", status)
            try:
                parsed = _parse_envelope(decoded)
            except TransportError:
                return LiveTransportOutcome("true", None, "invalid_response", status)
            if self._monotonic() - started > total_budget:
                return LiveTransportOutcome("true", None, "timeout", status)
            return LiveTransportOutcome("true", parsed, None, status)
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# Live Pilot Adapter（v2 发送控制面）
# ---------------------------------------------------------------------------

# ReceiptSource：由构造方注入的授权收据来源，键为 (run_id, node_id)。
LiveReceiptSource = Mapping[tuple[str, str], AuthorizationReceipt]


class LivePilotAdapter:
    """Phase 2B 单 capability Agent 节点的完整发送控制面。

    与 Phase 2A 相同的账本语义（先预留、后发送、完成先写账本、崩溃重放、
    reserved 恢复即 unknown_send、额度不回收），但绑定 Phase 2B 冻结身份：
    精确输入 bytes/摘要、DeepSeek 官方身份、v2 payload schema 与语义门。
    凭据只在原子预留成功之后读取；最多一次发送，绝不重试。
    """

    def __init__(
        self,
        *,
        ledger: AgentLedgerStore,
        receipts: LiveReceiptSource,
        transport: Any,
        credential_reader: Callable[[str], str],
        policy: AgentPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        execution_link: AgentExecutionLink | None = None,
        adapter_metadata_digest: str | None = None,
    ):
        if (execution_link is None) != (adapter_metadata_digest is None):
            # 关联与其 digest 绑定必须同时提供；半配置等于身份可漂移。
            raise ValueError(
                "execution_link and adapter_metadata_digest must be set together"
            )
        self.policy = policy or make_live_policy()
        self.ledger = ledger
        self.receipts = receipts
        self.transport = transport
        self.credential_reader = credential_reader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        # Gate 1（§3.5）：唯一旁路关联；GraphRuntime 经显式协议接线。
        self.execution_link = execution_link
        self.adapter_metadata_digest = adapter_metadata_digest

    EXECUTION_LINK_PROTOCOL = "agent-execution-link-v1"

    def bind_execution_link(
        self, link: AgentExecutionLink, metadata_digest: str
    ) -> None:
        """显式关联协议（GraphRuntime 接线入口）：未接线时绑定；同 digest
        重复绑定幂等；冲突即拒绝，绝不静默改写既有关联。"""
        if self.execution_link is not None:
            if self.adapter_metadata_digest == metadata_digest:
                return
            raise ValueError("execution_link_conflict")
        if self.adapter_metadata_digest not in (None, metadata_digest):
            raise ValueError("execution_link_conflict")
        self.execution_link = link
        self.adapter_metadata_digest = metadata_digest

    def observe(self, session_id: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = self.ledger.observe(session_id)
        return result

    def handler(self, request: NodeRequest) -> NodeResult:
        policy = self.policy
        flattened = flatten_inputs(request.inputs)
        if isinstance(flattened, str):
            return NodeResult(output={}, failure=f"input_conflict:{flattened}")
        # 精确输入绑定：sanitized_text 必须逐字节等于冻结输入。
        if (
            flattened.get("sanitized_text") != EXACT_INPUT
            or flattened.get("text_digest") != exact_input_sha256()
            or flattened.get("decision") != "approve_call"
        ):
            return NodeResult(output={}, failure="input_mismatch")
        input_bytes = len(canonical_json(flattened).encode("utf-8"))
        if input_bytes > policy.max_input_bytes:
            return NodeResult(output={}, failure="input_too_large")
        input_digest = digest_of(flattened)

        receipt = self.receipts.get((request.run_id, request.node_id))
        if receipt is None:
            return NodeResult(output={}, failure="authorization_missing")
        binding_error = validate_receipt_binding(
            receipt,
            policy,
            run_id=request.run_id,
            node_id=request.node_id,
            spec_digest=request.spec_digest,
            input_digest=input_digest,
            allowed_models=PHASE2B_ALLOWED_MODELS,
            now=self._clock(),
        )
        if binding_error is not None:
            return NodeResult(output={}, failure=binding_error)

        session_id = receipt.session_id()
        if self.execution_link is not None:
            # §3.5：在既有 ledger.reserve 前把 session 以 CAS 附着到前置
            # reservation（execution_id 只来自可信 NodeRequest）。已配置
            # execution-link 时缺 execution identity 必须在 ledger.reserve
            # 与 transport 前 fail closed；只有未配置链接的 Phase 2A 兼容
            # 路径保留旧直调语义。link digest 由 attach 内部以 reservation
            # 行的权威 run/node/spec/input 字段计算。
            execution_id = request.execution_id
            if execution_id is None:
                return NodeResult(output={}, failure="execution_reservation_missing")
            assert self.adapter_metadata_digest is not None  # 成对校验
            attached = self.execution_link.attach(
                execution_id=execution_id,
                session_id=session_id,
                adapter_metadata_digest=self.adapter_metadata_digest,
                authorization_digest=receipt.authorization_digest,
            )
            if attached not in ATTACH_OK:
                return NodeResult(output={}, failure=attached)

        existing = self.ledger.get(session_id)
        if existing is not None:
            status = str(existing["status"])
            if status == STATUS_COMPLETED:
                result_json = existing.get("result_json")
                if not isinstance(result_json, str):
                    return NodeResult(output={}, failure="ledger_result_missing")
                replayed = json.loads(result_json)
                return NodeResult(output=dict(replayed))
            if status == STATUS_FAILED:
                return NodeResult(
                    output={},
                    failure=f"session_consumed:{existing.get('error_category')}",
                )
            if status == STATUS_RESERVED:
                with self._inflight_lock:
                    inflight = session_id in self._inflight
                if inflight:
                    return NodeResult(output={}, failure="reservation_in_progress")
                self.ledger.fail(
                    session_id, error_category="unknown_send", request_sent="unknown"
                )
                return NodeResult(output={}, failure="unknown_send")

        if self.ledger.db_bytes() >= MAX_DB_BYTES:
            return NodeResult(output={}, failure="db_size_limit")

        if not policy.live_enabled:
            return NodeResult(output={}, failure="live_disabled")

        reservation = self.ledger.reserve(
            AgentCallRecord(
                session_id=session_id,
                run_id=request.run_id,
                node_id=request.node_id,
                spec_digest=request.spec_digest,
                input_digest=input_digest,
                adapter=policy.adapter,
                provider=policy.provider,
                model=policy.model,
                authorization_digest=receipt.authorization_digest,
                max_input_tokens=policy.max_input_tokens,
                max_output_tokens=policy.max_output_tokens,
            )
        )
        if reservation != "reserved":
            return NodeResult(output={}, failure=reservation)

        with self._inflight_lock:
            self._inflight.add(session_id)
        try:
            # 原子预留成功之后才允许读取精确 Keychain service。
            try:
                credential = self.credential_reader(KEYCHAIN_SERVICE)
            except Exception:
                # 凭据不可用：可证明未发送，记 transport_error/false，额度不回收。
                self.ledger.fail(
                    session_id, error_category="transport_error", request_sent="false"
                )
                return NodeResult(output={}, failure="credential_unavailable")
            started = self._monotonic()
            try:
                outcome: LiveTransportOutcome = self.transport.send_once(
                    TransportRequest(
                        session_id=session_id,
                        prompt=EXACT_INPUT,
                        max_output_tokens=policy.max_output_tokens,
                        timeout_seconds=policy.timeout_seconds,
                    ),
                    credential,
                )
            except TransportError:
                self.ledger.fail(
                    session_id, error_category="transport_error", request_sent="false"
                )
                return NodeResult(output={}, failure="transport_error")
            except Exception:
                self.ledger.fail(
                    session_id, error_category="transport_exception", request_sent="unknown"
                )
                return NodeResult(output={}, failure="transport_exception")
            elapsed = self._monotonic() - started
            if outcome.response is None:
                category = outcome.error_category or "transport_exception"
                self.ledger.fail(
                    session_id,
                    error_category=category,
                    request_sent=outcome.request_sent,
                )
                return NodeResult(output={}, failure=category)
            if elapsed > policy.timeout_seconds:
                self.ledger.fail(session_id, error_category="timeout", request_sent="true")
                return NodeResult(output={}, failure="timeout")
            failure = self._validate_response(outcome.response)
            if failure is not None:
                category = (
                    failure if failure in LEDGER_ERROR_CATEGORIES else "invalid_response"
                )
                self.ledger.fail(
                    session_id,
                    error_category=category,
                    request_sent="true",
                )
                return NodeResult(output={}, failure=failure)
            completed = self.ledger.complete(
                session_id,
                declared_provider=outcome.response.declared_provider,
                declared_model=outcome.response.declared_model,
                actual_input_tokens=outcome.response.input_tokens,
                actual_output_tokens=outcome.response.output_tokens,
                result=outcome.response.payload,
                result_digest=digest_of(outcome.response.payload),
            )
            if not completed:
                return NodeResult(output={}, failure="ledger_write_conflict")
            return NodeResult(output=dict(outcome.response.payload))
        finally:
            with self._inflight_lock:
                self._inflight.discard(session_id)

    def _validate_response(self, response: TransportResponse) -> str | None:
        """响应身份、预算、payload schema 与语义门；返回稳定错误码或 None。"""
        if response.declared_provider != self.policy.provider:
            return "provider_mismatch"
        if response.declared_model != self.policy.model:
            return "model_mismatch"
        if (
            not isinstance(response.input_tokens, int)
            or isinstance(response.input_tokens, bool)
            or response.input_tokens < 0
            or response.input_tokens > self.policy.max_input_tokens
        ):
            return "budget_exceeded"
        if (
            not isinstance(response.output_tokens, int)
            or isinstance(response.output_tokens, bool)
            or response.output_tokens < 0
            or response.output_tokens > self.policy.max_output_tokens
        ):
            return "budget_exceeded"
        payload_error = validate_draft_payload(response.payload)
        if payload_error is not None:
            if payload_error == "response_too_large":
                return "response_too_large"
            return payload_error
        return None


# ---------------------------------------------------------------------------
# 一次性执行编排
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LivePilotConfig:
    """一次执行的全部显式参数；无任何隐藏默认值。

    ``expected_authorization_digest`` / ``authorization_not_after`` 是授权身份
    绑定：生产 CLI 固定注入模块级冻结常量；测试注入自己的合成常量。
    """

    db_path: Path
    backup_dir: Path
    snapshot_path: Path
    run_id: str
    fragment_ref: str
    authorization_phrase: str
    issued_at: str
    expires_at: str
    expected_authorization_digest: str
    authorization_not_after: str


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LivePilotOrchestrator:
    """Phase 2B 唯一调用的一次性编排（冻结 §3 顺序）。

    顺序固定：pre_call 备份+验证 → 调用前人闸确认 → 官方价格 preflight →
    图推进（原子预留 → Keychain → 最多一次发送 → 账本 → Checkpoint）→
    调用后人闸等待。任何前置失败都在读取凭据和发送之前停止。
    """

    def __init__(
        self,
        config: LivePilotConfig,
        *,
        transport: Any,
        credential_reader: Callable[[str], str],
        price_fetcher: Callable[[], str],
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.transport = transport
        self.credential_reader = credential_reader
        self.price_fetcher = price_fetcher
        self._clock = clock or (lambda: datetime.now(UTC))
        self.spec = build_phase2b_pilot_spec()

    # -- 基础构造 ----------------------------------------------------------

    def _store_and_ledger(self) -> tuple[SQLiteCheckpointStore, AgentLedgerStore]:
        db = Path(self.config.db_path)
        store = SQLiteCheckpointStore(db)
        ledger = AgentLedgerStore(db)
        return store, ledger

    def _runtime(self, ledger: AgentLedgerStore) -> GraphRuntime:
        receipt = self._receipt()
        adapter = LivePilotAdapter(
            ledger=ledger,
            receipts={(self.config.run_id, AGENT_NODE): receipt},
            transport=self.transport,
            credential_reader=self.credential_reader,
            clock=self._clock,
        )
        store, _ = self._store_and_ledger()
        adapters: dict[str, Any] = {
            **make_pilot2b_fixture_adapters(),
            PILOT2B_AGENT_ADAPTER: adapter.handler,
        }
        return GraphRuntime(store, {self.spec.graph_id: self.spec}, adapters)

    def _receipt(self) -> AuthorizationReceipt:
        digest = authorization_digest_of(
            self.config.authorization_phrase, authorized_by="nigo"
        )
        if digest != self.config.expected_authorization_digest:
            _fail("authorization_identity_drift")
        not_after = datetime.fromisoformat(self.config.authorization_not_after)
        expires = datetime.fromisoformat(self.config.expires_at)
        if expires.astimezone(UTC) > not_after.astimezone(UTC):
            _fail("authorization_window_drift")
        return AuthorizationReceipt(
            run_id=self.config.run_id,
            node_id=AGENT_NODE,
            spec_digest=self.spec.digest,
            input_digest=expected_agent_input_digest(),
            adapter=PILOT2B_AGENT_ADAPTER,
            provider=PROVIDER,
            model=MODEL,
            max_calls=1,
            max_input_tokens=MAX_INPUT_TOKENS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_input_bytes=MAX_REQUEST_BYTES,
            authorization_digest=digest,
            authorized_by="nigo",
            issued_at=self.config.issued_at,
            expires_at=self.config.expires_at,
        )

    def _state(self) -> tuple[Any, dict[str, Any], int]:
        store = SQLiteCheckpointStore(Path(self.config.db_path))
        found = store.latest_with_sequence(self.config.run_id)
        if found is None:
            _fail("run_missing")
        checkpoint, sequence = found
        state = checkpoint.eval_results.get("graph_state")
        if not isinstance(state, dict):
            _fail("not_a_graph_run")
        if state.get("graph_id") != PILOT2B_GRAPH_ID:
            _fail("run_graph_mismatch")
        return checkpoint, state, sequence

    # -- 生命周期步骤 ------------------------------------------------------

    def initialize_database(self) -> dict[str, Any]:
        """初始化独立 Pilot 库（只含 Graph Checkpoint 与 Agent 账本表）。

        已存在的路径一律拒绝：真实库必须是本步骤新建的，绝不复用或覆盖。
        """
        db = Path(self.config.db_path)
        if db.exists() or db.is_symlink():
            _fail("db_already_exists")
        store, ledger = self._store_and_ledger()
        del store, ledger
        if not db.exists():
            _fail("db_init_failed")
        return {"db_path": str(db), "initialized": True}

    def _decision_origin(self) -> dict[str, Any]:
        """双闸门 decision origin：固定来源 + 授权摘要（绝不含原文）。"""
        return {
            "origin": DECISION_ORIGIN,
            "authorization_digest": self.config.expected_authorization_digest,
        }

    def _registration_run_inputs(self) -> Any:
        checkpoint, _state, _sequence = self._state()
        registration = checkpoint.eval_results.get("registration")
        if not isinstance(registration, dict):
            _fail("decision_origin_drift")
        return registration.get("run_inputs")

    def _verify_decision_origin(self) -> None:
        """record/execute/finalize 前的固定复核：origin 漂移即拒绝。"""
        run_inputs = self._registration_run_inputs()
        if not isinstance(run_inputs, dict):
            _fail("decision_origin_drift")
        if run_inputs.get("decision_origin") != self._decision_origin():
            _fail("decision_origin_drift")

    def start_run(self) -> dict[str, Any]:
        """注册 run 并推进到调用前人闸；返回闸门绑定供 Codex 裁决。

        run_inputs 持久化双闸门 decision origin（固定预授权来源 + 授权摘要）。
        """
        store, ledger = self._store_and_ledger()
        del ledger
        runtime = GraphRuntime(
            store, {self.spec.graph_id: self.spec}, make_pilot2b_fixture_adapters()
        )
        runtime.register_run(
            self.spec,
            self.config.run_id,
            fragment_ref=self.config.fragment_ref,
            run_inputs={"decision_origin": self._decision_origin()},
        )
        outcome = runtime.run_until_settled(self.config.run_id)
        if outcome.event != "human_gate_requested" or outcome.node_id != PRE_GATE:
            _fail(f"unexpected_run_state:{outcome.event}")
        return self.gate_binding(PRE_GATE)

    def gate_binding(self, node_id: str) -> dict[str, Any]:
        """只读闸门绑定投影（不含任何正文/凭据）。"""
        _checkpoint, state, sequence = self._state()
        gate = state["human_gates"].get(node_id)
        if not isinstance(gate, dict):
            _fail("gate_not_pending")
        return {
            "run_id": self.config.run_id,
            "node_id": node_id,
            "spec_digest": gate["spec_digest"],
            "input_digest": gate["input_digest"],
            "expected_sequence": sequence,
            "requester": "nigo",
            "allowed_decisions": list(gate["allowed_decisions"]),
            "gate_status": gate["status"],
            "decision": gate["decision"],
            "decision_origin": self._decision_origin(),
        }

    def record_gate(self, node_id: str, decision: str) -> dict[str, Any]:
        """记录一道人闸决定；requester 固定 nigo（预授权来源）。"""
        self._verify_decision_origin()
        store, _ledger = self._store_and_ledger()
        runtime = GraphRuntime(store, {self.spec.graph_id: self.spec}, {})
        _checkpoint, state, sequence = self._state()
        gate = state["human_gates"].get(node_id)
        if not isinstance(gate, dict) or gate["status"] != "pending":
            _fail("gate_not_pending")
        decision_id = human_decision_id(
            requester="nigo",
            decision=decision,
            run_id=self.config.run_id,
            node_id=node_id,
            spec_digest=str(state["spec_digest"]),
            input_digest=str(gate["input_digest"]),
            expected_sequence=sequence,
        )
        _committed, applied = runtime.apply_human_decision(
            self.config.run_id,
            node_id,
            decision=decision,
            spec_digest=str(state["spec_digest"]),
            input_digest=str(gate["input_digest"]),
            expected_sequence=sequence,
            requester="nigo",
            decision_id=decision_id,
        )
        return {
            "run_id": self.config.run_id,
            "node_id": node_id,
            "decision": decision,
            "decision_id": decision_id,
            "applied": applied,
        }

    def execute_call(self) -> dict[str, Any]:
        """唯一发送的编排：授权身份 → 备份 → 人闸确认 → 价格门 → 预留/凭据/发送。"""
        # (0) 授权与时效身份、decision origin：任何漂移都在一切副作用之前失败。
        self._receipt()
        self._verify_decision_origin()
        # (1) pre_call 备份 + 验证；失败即零凭据读取、零发送（§3.2）。
        manifest = create_backup(
            self.config.db_path,
            self.config.backup_dir,
            stage="pre_call",
            reason="graph-phase2b unique synthetic call: pre_call",
        )
        backup_file = Path(self.config.backup_dir) / str(manifest["backup"]["file_name"])
        manifest_file = Path(str(backup_file) + ".manifest.json")
        verify_backup(backup_file, manifest_file)

        # (2) 调用前人闸确认：必须已被 nigo 预授权来源批准（§3.3）。
        _checkpoint, state, _sequence = self._state()
        gate = state["human_gates"].get(PRE_GATE)
        if not isinstance(gate, dict) or gate.get("status") != "resolved":
            _fail("pre_gate_not_resolved")
        if gate.get("decision") != "approve_call" or gate.get("requester") != "nigo":
            _fail("pre_gate_not_approved")

        # (3) 官方价格 preflight（§2）；快照先落盘，超预算即零调用。
        if Path(self.config.snapshot_path).exists():
            _fail("snapshot_exists")
        page_text = self.price_fetcher()
        snapshot = evaluate_price_gate(page_text, fetched_at=_utc_now_iso())
        snapshot_path = Path(self.config.snapshot_path)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if not snapshot["within_budget"]:
            _fail("price_gate_failed")

        # (4) 图推进：原子预留 → Keychain → 最多一次发送 → 账本 → Checkpoint，
        #     停在调用后人闸（§3.4-3.6）。
        store, ledger = self._store_and_ledger()
        runtime = self._runtime(ledger)
        outcome = runtime.run_until_settled(self.config.run_id)
        session_id = self._receipt().session_id()
        observation = ledger.observe(session_id)
        evidence: dict[str, Any] = {
            "run_id": self.config.run_id,
            "session_id": session_id,
            "pre_call_backup": manifest["backup"]["file_name"],
            "price_snapshot": snapshot,
            "agent_call": observation,
            "run_event": outcome.event,
        }
        if outcome.event != "human_gate_requested" or outcome.node_id != POST_GATE:
            evidence["post_gate"] = None
            return evidence
        evidence["post_gate"] = self.gate_binding(POST_GATE)
        return evidence

    def finalize(self, restore_to: str | Path) -> dict[str, Any]:
        """人闸之后的收口：推进终态 → terminal 备份 → 全新路径恢复演练。

        两条合法终态路径：调用后人闸已裁决（accept_draft/reject），或调用前
        人闸 abort（从未发送，post gate 不存在）。其余状态一律拒绝。
        """
        self._verify_decision_origin()
        _checkpoint, state, _sequence = self._state()
        gate = state["human_gates"].get(POST_GATE)
        post_decision: str | None = None
        if isinstance(gate, dict) and gate.get("status") == "resolved":
            if gate.get("decision") not in ("accept_draft", "reject"):
                _fail("post_gate_not_resolved")
            post_decision = str(gate["decision"])
        else:
            pre_gate = state["human_gates"].get(PRE_GATE)
            if (
                not isinstance(pre_gate, dict)
                or pre_gate.get("status") != "resolved"
                or pre_gate.get("decision") != "abort"
            ):
                _fail("post_gate_not_resolved")

        store, _ledger = self._store_and_ledger()
        runtime = GraphRuntime(
            store, {self.spec.graph_id: self.spec}, make_pilot2b_fixture_adapters()
        )
        outcome = runtime.run_until_settled(self.config.run_id)
        if outcome.event != "completed":
            _fail(f"unexpected_terminal_state:{outcome.event}")

        manifest = create_backup(
            self.config.db_path,
            self.config.backup_dir,
            stage="terminal",
            reason="graph-phase2b unique synthetic call: terminal",
        )
        backup_file = Path(self.config.backup_dir) / str(manifest["backup"]["file_name"])
        manifest_file = Path(str(backup_file) + ".manifest.json")
        verify_backup(backup_file, manifest_file)
        restored = restore_backup(backup_file, manifest_file, restore_to)
        verify_backup(restore_to, manifest_file, as_backup=True)
        _checkpoint2, final_state, _seq2 = self._state()
        return {
            "run_id": self.config.run_id,
            "run_status": final_state["run_status"],
            "post_gate_decision": post_decision,
            "terminal_backup": manifest["backup"]["file_name"],
            "restore_rehearsal": restored,
        }
