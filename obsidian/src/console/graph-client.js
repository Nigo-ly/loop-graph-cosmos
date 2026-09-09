"use strict";

const { createHttpClient } = require("./http-client.js");

// Graph Phase 1 client for the optional loopback-only 5684 Graph resources.
// The base URL is frozen and can never be overridden by a caller; writes are
// limited to exactly two bound resources (human decisions, node reopen) and
// each carries its own frozen header. All dynamic text stays data — the view
// layer renders it with text nodes only.

const GRAPH_API_BASE_URL = "http://127.0.0.1:5684/graph/v1";
const CONTRACT_VERSION = "2";
const CANVAS_VERSION = "1";
const TRUSTED_ORIGIN = "app://obsidian.md";
const DECISION_HEADER = "X-Graph-Human-Decision";
const REOPEN_HEADER = "X-Graph-Node-Reopen";
const RUN_CREATE_HEADER = "X-Graph-Run-Create";
const RECEIPT_HEADER = "X-Graph-Authorization-Receipt";
const RESEARCH_RECEIPT_HEADER = "X-Graph-Research-Authorization-Receipt";
const RETRY_HEADER = "X-Graph-Pilot-Retry";
const PILOT_PLAN_PATH = "/pilot-plans/fragment-pilot-v1";
const REQUEST_TIMEOUT_MS = 5000;

class GraphApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "GraphApiError";
    this.kind = kind;
    this.details = details || {};
  }
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function sha256(value) {
  return toHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

// Mirrors graph_runtime.runtime.human_decision_id: SHA-256 over the canonical
// JSON form of the fixed binding material list.
async function graphDecisionId(body) {
  return sha256(
    JSON.stringify([
      "graph-human-decision-v1",
      body.requester,
      body.decision,
      body.run_id,
      body.node_id,
      body.spec_digest,
      body.input_digest,
      String(body.expected_sequence),
    ])
  );
}

// Mirrors graph_runtime.runtime.reopen_request_id.
async function graphReopenId(body) {
  return sha256(
    JSON.stringify([
      "graph-node-reopen-v1",
      body.requester,
      body.run_id,
      body.node_id,
      String(body.expected_sequence),
      body.reason,
    ])
  );
}

// Mirrors graph_runtime.pilot_execution.pilot_retry_id: SHA-256 over the
// canonical JSON form of the fixed retry binding material list.
async function graphPilotRetryId(body) {
  return sha256(
    JSON.stringify([
      "graph-pilot-retry-v1",
      body.requester,
      body.run_id,
      body.node_id,
      body.spec_digest,
      body.input_digest,
      body.authorization_digest,
      String(body.expected_sequence),
    ])
  );
}

function isSha256(value) {
  return (
    typeof value === "string" &&
    value.length === 64 &&
    /^[0-9a-f]+$/.test(value)
  );
}

function validateRunId(runId) {
  if (
    typeof runId !== "string" ||
    !runId.startsWith("exec:") ||
    runId.length > 256 ||
    /[\x00-\x20\x7f/\\]/.test(runId)
  ) {
    throw new GraphApiError("invalid_arguments", "Graph 运行 ID 无效");
  }
  return runId;
}

function validateNodeId(nodeId) {
  if (
    typeof nodeId !== "string" ||
    !nodeId ||
    nodeId.length > 128 ||
    /[\s\x1f/\\:]/.test(nodeId)
  ) {
    throw new GraphApiError("invalid_arguments", "Graph 节点 ID 无效");
  }
  return nodeId;
}

function validateEnvelope(response) {
  if (!response || typeof response !== "object" || response.contract_version !== CONTRACT_VERSION) {
    throw new GraphApiError("contract_mismatch", "Graph 服务契约不匹配");
  }
  return response.data;
}

// 时间字段契约：runs 列表与 run detail 的 run 摘要必须携带带时区的
// ISO 8601 started_at / updated_at（后端 common.checkpoint.utc_now 与
// graph_runtime 的 started_at 均为 timezone-aware ISO）。缺字段、非字符串、
// 无时区或不可解析都视为契约违反，整条响应作废——UI 绝不退化成猜测时间。
function isValidTimestamp(value) {
  return (
    typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.test(value) &&
    !Number.isNaN(Date.parse(value))
  );
}

function validateRunSummary(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    typeof value.run_id !== "string" ||
    typeof value.graph_id !== "string" ||
    typeof value.spec_digest !== "string" ||
    typeof value.status !== "string" ||
    !Number.isInteger(value.sequence) ||
    !Number.isInteger(value.step_count) ||
    !Array.isArray(value.pending_human)
  ) {
    throw new GraphApiError("invalid_response", "Graph 运行摘要无效");
  }
  if (!isValidTimestamp(value.started_at) || !isValidTimestamp(value.updated_at)) {
    throw new GraphApiError("invalid_response", "Graph 运行时间字段无效");
  }
  return value;
}

// Graph Phase 2A: a node may optionally carry the frozen read-only Agent
// observation projection (GRAPH-PHASE2-DESIGN.md §5/§7). When present it must
// match the exact shape; the projection never carries prompt/response bodies,
// authorization phrases or credentials, so nothing here needs stripping.
function validateAgentObservation(value) {
  if (value === undefined || value === null) return value;
  if (
    typeof value !== "object" ||
    Array.isArray(value) ||
    typeof value.status !== "string" ||
    typeof value.provider !== "string" ||
    typeof value.model !== "string" ||
    !Number.isInteger(value.max_calls) ||
    !(value.reserved_tokens === null || Number.isInteger(value.reserved_tokens)) ||
    !(value.actual_tokens === null || Number.isInteger(value.actual_tokens)) ||
    !(value.error_category === null || typeof value.error_category === "string")
  ) {
    throw new GraphApiError("invalid_response", "Graph 节点 Agent 观察字段无效");
  }
  // rev3：安全重试的可见性条件依赖发送事实标记；存在时必须合法。
  if (
    value.request_sent !== undefined &&
    !["true", "false", "unknown", null].includes(value.request_sent)
  ) {
    throw new GraphApiError("invalid_response", "Graph 节点发送事实标记无效");
  }
  return value;
}

function validateRunDetail(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !value.run ||
    !Array.isArray(value.nodes) ||
    !Array.isArray(value.edges_taken) ||
    typeof value.human_gates !== "object" ||
    !Array.isArray(value.ready)
  ) {
    throw new GraphApiError("invalid_response", "Graph 运行详情无效");
  }
  validateRunSummary(value.run);
  for (const node of value.nodes) {
    if (
      !node ||
      typeof node !== "object" ||
      typeof node.node_id !== "string" ||
      typeof node.status !== "string" ||
      !Number.isInteger(node.attempts) ||
      !Array.isArray(node.input_refs) ||
      !Array.isArray(node.output_refs) ||
      !Array.isArray(node.memory_refs)
    ) {
      throw new GraphApiError("invalid_response", "Graph 节点字段无效");
    }
    validateAgentObservation(node.agent);
  }
  return value;
}

function validatePath(value) {
  if (
    !value ||
    typeof value !== "object" ||
    typeof value.entry_node !== "string" ||
    !Array.isArray(value.path) ||
    !Array.isArray(value.frontier) ||
    !Array.isArray(value.failed_nodes)
  ) {
    throw new GraphApiError("invalid_response", "Graph 关键路径无效");
  }
  return value;
}

function validateHistory(value) {
  if (!Array.isArray(value)) {
    throw new GraphApiError("invalid_response", "Graph 执行历史无效");
  }
  for (const item of value) {
    if (
      !item ||
      typeof item !== "object" ||
      !Number.isInteger(item.sequence) ||
      typeof item.event_type !== "string"
    ) {
      throw new GraphApiError("invalid_response", "Graph 执行历史条目无效");
    }
  }
  return value;
}

function validateAffected(value) {
  if (
    !value ||
    typeof value !== "object" ||
    typeof value.node_id !== "string" ||
    !Array.isArray(value.affected_nodes) ||
    !Array.isArray(value.affected_human_gates)
  ) {
    throw new GraphApiError("invalid_response", "Graph 影响范围无效");
  }
  return value;
}

// Canvas 契约（GRAPH-CANVAS-V1-DESIGN.md §1.1 rev2）校验：rev2 起不再只做
// JavaScript 类型检查——ID 格式、唯一性、引用完整性、枚举范围、文案限长全部
// 在此把关；任何一项不合格即抛错，调用方回退文本视图，畸形数据绝不进入布局器。
// rev3：ID 规则与后端 _is_plain_identifier 完全一致（NFC、允许合法
// Unicode、禁控制字符与路径语义、禁 exec:/mem: 命名空间），不得私自缩窄。
const CANVAS_LABEL_MAX = 120;
const CANVAS_JOIN_MODES = new Set(["all_success", "minimum_success"]);
const CANVAS_PARTIAL_FAILURE = new Set(["fail", "continue", "pause"]);
const CANVAS_EXHAUSTED = new Set(["fail", "pause", "route"]);
const CANVAS_EDGE_TYPES = new Set(["sequence", "condition", "feedback"]);
const CANVAS_DECISION_SOURCES = new Set(["declared", "rule_evaluated", "human_selected"]);
const CANVAS_GATE_STATUSES = new Set(["pending", "resolved", "expired"]);
// run/node status 与 kind 只限长 + 拒控制字符：未知值进入 model 后按纯文本
// 显示、CSS class 固定 is-unknown/kind-unknown（rev4 诚实降级）。

function isCleanLabel(value, max) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= max &&
    !/[\x00-\x1f\x7f]/.test(value)
  );
}

function isCanvasId(value) {
  if (typeof value !== "string" || !value || value.length > 128) return false;
  if (value.normalize("NFC") !== value) return false;
  if (value.startsWith("exec:") || value.startsWith("mem:")) return false;
  if (/[\x00-\x1f\x7f:/\\]/.test(value)) return false;
  if (value.startsWith(".")) return false;
  return true;
}

function isCleanEnum(value, allowed, what) {
  if (typeof value !== "string" || !allowed.has(value)) {
    throw new GraphApiError("invalid_response", `Graph 画布${what}无效`);
  }
  return value;
}

function isCleanCode(value, max) {
  return (
    typeof value === "string" &&
    value.length <= max &&
    !/[\x00-\x1f\x7f]/.test(value)
  );
}

function validateCanvasJoin(value) {
  if (value === null) return;
  if (
    typeof value !== "object" ||
    Array.isArray(value) ||
    !CANVAS_JOIN_MODES.has(value.mode) ||
    !CANVAS_PARTIAL_FAILURE.has(value.on_partial_failure) ||
    typeof value.cancel_remaining !== "boolean"
  ) {
    throw new GraphApiError("invalid_response", "Graph 画布 join 配置无效");
  }
  // threshold 按 mode 校验：all_success 必须为 null；minimum_success 必须为正整数。
  if (value.mode === "all_success" && value.threshold !== null) {
    throw new GraphApiError("invalid_response", "all_success 汇合不得携带 threshold");
  }
  if (value.mode === "minimum_success" && (!Number.isInteger(value.threshold) || value.threshold < 1)) {
    throw new GraphApiError("invalid_response", "minimum_success 汇合 threshold 必须为正整数");
  }
}

// Pilot 闸门扩展（DESIGN §3.4/§3.7）：授权单安全投影与次闸结构化结果。
// 前端复查一遍过滤规则：限长、控制字符、URL/凭据样式一律拒绝整条响应。
const PILOT_RESULT_TEXT = /https?:\/\/|www\.|api[_-]?key|token|secret|密码|凭据/i;

function validatePilotAuthorization(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !isSha256(value.authorization_digest) ||
    !isCleanCode(value.provider, 40) ||
    !isCleanCode(value.model, 64) ||
    !Number.isInteger(value.max_total_calls) ||
    value.max_total_calls < 1 ||
    value.max_total_calls > 8 ||
    typeof value.cost_cap_cny !== "number" ||
    !isSha256(value.input_digest) ||
    !isSha256(value.agent_input_digest) ||
    !isSha256(value.candidate_content_sha256) ||
    !Number.isInteger(value.expected_sequence) ||
    !isCleanCode(value.issued_at, 40) ||
    !isCleanCode(value.expires_at, 40) ||
    !(
      value.price_snapshot_source === null ||
      (typeof value.price_snapshot_source === "string" &&
        value.price_snapshot_source.startsWith("https://") &&
        value.price_snapshot_source.length <= 200)
    )
  ) {
    throw new GraphApiError("invalid_response", "Graph 授权单投影无效");
  }
  validatePilotInputFields(value.input_fields);
}

const PILOT_PREVIEW_WRITE_SCOPE = "Graph 检查点 + Agent 账本；不写笔记、不写资产";

function validatePilotAuthorizationPreview(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.provider !== "deepseek" ||
    value.model !== "deepseek-v4-pro" ||
    value.max_total_calls !== 2 ||
    value.cost_cap_cny !== 2 ||
    value.write_scope !== PILOT_PREVIEW_WRITE_SCOPE
  ) {
    throw new GraphApiError("invalid_response", "Graph 签发前授权材料无效");
  }
  validatePilotInputFields(value.input_fields);
}

function validatePilotResultText(value, max) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= max &&
    !/[\x00-\x1f\x7f]/.test(value) &&
    !PILOT_RESULT_TEXT.test(value)
  );
}

function validatePilotResult(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    typeof value.available !== "boolean" ||
    !isSha256(value.result_digest)
  ) {
    throw new GraphApiError("invalid_response", "Graph 结果投影无效");
  }
  if (value.available) {
    if (!validatePilotResultText(value.summary, 500)) {
      throw new GraphApiError("invalid_response", "Graph 结果摘要无效");
    }
    for (const key of ["unknowns", "next_checks"]) {
      const items = value[key];
      if (
        !Array.isArray(items) ||
        items.length > 8 ||
        !items.every((item) => validatePilotResultText(item, 200))
      ) {
        throw new GraphApiError("invalid_response", "Graph 结果列表无效");
      }
    }
  }
}

function validateResearchText(value, max, allowUrl = false) {
  return typeof value === "string" && value.length > 0 && value.length <= max &&
    !/[\x00-\x1f\x7f]/.test(value) && (allowUrl || !/https?:\/\/|www\./i.test(value));
}

function validateResearchPreview(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    value.provider !== "deepseek" || value.model !== "deepseek-v4-pro" ||
    value.max_total_calls !== 1 || value.cost_cap_cny !== 2 ||
    !validateResearchText(value.research_goal, 200) ||
    !Number.isInteger(value.evidence_count) || value.evidence_count < 0 ||
    !Array.isArray(value.sources) || value.sources.length > 8 ||
    !validateResearchText(value.write_scope, 128)) {
    throw new GraphApiError("invalid_response", "Graph 研究授权材料无效");
  }
  for (const source of value.sources) {
    if (!source || typeof source !== "object" || Array.isArray(source) ||
      !validateResearchText(source.title, 300, true) ||
      !validateResearchText(source.url, 2048, true) || !source.url.startsWith("https://")) {
      throw new GraphApiError("invalid_response", "Graph 研究来源材料无效");
    }
  }
}

function validateResearchAuthorization(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !isSha256(value.authorization_digest) || value.provider !== "deepseek" ||
    value.model !== "deepseek-v4-pro" || value.max_total_calls !== 1 ||
    !isSha256(value.input_digest) || !Number.isInteger(value.expected_sequence) ||
    !isCleanCode(value.issued_at, 40) || !isCleanCode(value.expires_at, 40) ||
    typeof value.price_snapshot_source !== "string" ||
    !value.price_snapshot_source.startsWith("https://")) {
    throw new GraphApiError("invalid_response", "Graph 研究授权单投影无效");
  }
}

function validateResearchResult(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    value.available !== true || !isSha256(value.result_digest) ||
    !validateResearchText(value.summary, 600) ||
    !validateResearchText(value.recommendation, 400)) {
    throw new GraphApiError("invalid_response", "Graph 研究结果投影无效");
  }
  for (const key of ["confirmed", "unknowns", "conflicts", "claims"]) {
    if (!Array.isArray(value[key])) {
      throw new GraphApiError("invalid_response", "Graph 研究结果列表无效");
    }
  }
  if (value.confirmed.length > 8 || value.unknowns.length > 8 ||
    value.conflicts.length > 4 || value.claims.length > 16 ||
    !value.unknowns.every((item) => validateResearchText(item, 200))) {
    throw new GraphApiError("invalid_response", "Graph 研究结果列表无效");
  }
  for (const item of value.confirmed) {
    if (!item || typeof item !== "object" || Array.isArray(item) ||
      Object.keys(item).sort().join(",") !== "claim,evidence_ids" ||
      !validateResearchText(item.claim, 200) || !Array.isArray(item.evidence_ids) ||
      item.evidence_ids.length < 1 || item.evidence_ids.length > 4 ||
      !item.evidence_ids.every((id) => isCleanCode(id, 64))) {
      throw new GraphApiError("invalid_response", "Graph 研究确认项无效");
    }
  }
  for (const item of value.conflicts) {
    if (!item || typeof item !== "object" || Array.isArray(item) ||
      Object.keys(item).sort().join(",") !== "dimensions,evidence_ids,topic" ||
      !validateResearchText(item.topic, 100) || !Array.isArray(item.dimensions) ||
      item.dimensions.length < 1 || item.dimensions.length > 4 ||
      !item.dimensions.every((text) => validateResearchText(text, 200)) ||
      !Array.isArray(item.evidence_ids) || item.evidence_ids.length < 1 ||
      item.evidence_ids.length > 4 || !item.evidence_ids.every((id) => isCleanCode(id, 64))) {
      throw new GraphApiError("invalid_response", "Graph 研究冲突项无效");
    }
  }
  const relations = new Set(["supports", "partially_supports", "conflicts", "irrelevant"]);
  for (const item of value.claims) {
    if (!item || typeof item !== "object" || Array.isArray(item) ||
      Object.keys(item).sort().join(",") !== "claim,evidence_id,relation" ||
      !validateResearchText(item.claim, 200) || !isCleanCode(item.evidence_id, 64) ||
      !relations.has(item.relation)) {
      throw new GraphApiError("invalid_response", "Graph 研究证据关系无效");
    }
  }
}

function validateCanvasGate(value) {
  if (value === null) return;
  if (
    typeof value !== "object" ||
    Array.isArray(value) ||
    !CANVAS_GATE_STATUSES.has(value.status) ||
    !Array.isArray(value.allowed_decisions) ||
    !value.allowed_decisions.every((item) => isCleanCode(item, 32))
  ) {
    throw new GraphApiError("invalid_response", "Graph 画布人工闸门字段无效");
  }
  if (value.authorization !== undefined) validatePilotAuthorization(value.authorization);
  if (value.authorization_preview !== undefined) {
    validatePilotAuthorizationPreview(value.authorization_preview);
  }
  if (value.result !== undefined) validatePilotResult(value.result);
  if (value.research_authorization_preview !== undefined) {
    validateResearchPreview(value.research_authorization_preview);
  }
  if (value.research_authorization !== undefined) {
    validateResearchAuthorization(value.research_authorization);
  }
  if (value.research_result !== undefined) validateResearchResult(value.research_result);
}

// 授权投影可选携带「将发送的安全输入字段」（与实发同一 canonical 对象）；
// 前端按同一套上限复查，超限/控制字符一律作废整条响应。输入是合法候选
// 内容（可能含链接域名），URL 过滤只针对结果投影，不针对输入字段。
function validatePilotInputFields(value) {
  if (value === undefined) return;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new GraphApiError("invalid_response", "Graph 授权输入字段无效");
  }
  const caps = { title: 100, core_judgment: 500, user_value: 300 };
  for (const [key, cap] of Object.entries(caps)) {
    const field = value[key];
    if (
      typeof field !== "string" ||
      !field ||
      field.length > cap ||
      /[\x00-\x1f\x7f]/.test(field)
    ) {
      throw new GraphApiError("invalid_response", "Graph 授权输入字段无效");
    }
  }
  const cards = value.card_titles;
  if (
    !Array.isArray(cards) ||
    cards.length > 8 ||
    !cards.every(
      (item) =>
        typeof item === "string" &&
        item &&
        item.length <= 100 &&
        !/[\x00-\x1f\x7f]/.test(item)
    )
  ) {
    throw new GraphApiError("invalid_response", "Graph 授权输入卡片字段无效");
  }
}

function validateCanvasNode(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !isCanvasId(value.node_id) ||
    !isCleanLabel(value.display_label, 40) ||
    !isCleanCode(value.kind, 32) ||
    !isCleanCode(value.status, 32) ||
    typeof value.is_entry !== "boolean" ||
    typeof value.is_current !== "boolean" ||
    typeof value.is_frontier !== "boolean" ||
    !(value.output_digest === null || isSha256(value.output_digest)) ||
    !(value.error_code === null || isCleanCode(value.error_code, 64)) ||
    !(value.completed_at === null || isCleanCode(value.completed_at, 40)) ||
    !(value.result_summary === undefined || isCleanLabel(value.result_summary, CANVAS_LABEL_MAX)) ||
    (typeof value.result_summary === "string" && /https?:\/\/|www\./i.test(value.result_summary))
  ) {
    throw new GraphApiError("invalid_response", "Graph 画布节点字段无效");
  }
  validateCanvasJoin(value.join);
  validateCanvasGate(value.human_gate);
  return value;
}

function validateCanvasEdge(value, nodeIds) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !isCanvasId(value.edge_id) ||
    !isCanvasId(value.from) ||
    !isCanvasId(value.to) ||
    !CANVAS_EDGE_TYPES.has(value.type) ||
    typeof value.taken !== "boolean" ||
    !(value.label === null || isCleanLabel(value.label, CANVAS_LABEL_MAX)) ||
    !(value.decision_source === null || CANVAS_DECISION_SOURCES.has(value.decision_source)) ||
    !Number.isInteger(value.taken_count) ||
    value.taken_count < 0 ||
    !(value.max_traversals === null || (Number.isInteger(value.max_traversals) && value.max_traversals >= 0)) ||
    (value.max_traversals !== null && value.taken_count > value.max_traversals) ||
    !(value.on_exhausted === null || CANVAS_EXHAUSTED.has(value.on_exhausted)) ||
    !(value.exhausted_to === null || isCanvasId(value.exhausted_to))
  ) {
    throw new GraphApiError("invalid_response", "Graph 画布边字段无效");
  }
  // feedback 专属字段规则：仅 feedback 边可携带 max_traversals/on_exhausted/
  // exhausted_to；feedback 必须具备合法正整数上限与耗尽策略。
  if (value.type !== "feedback") {
    if (value.max_traversals !== null || value.on_exhausted !== null || value.exhausted_to !== null) {
      throw new GraphApiError("invalid_response", "非返修边不得携带返修字段");
    }
  } else {
    if (!Number.isInteger(value.max_traversals) || value.max_traversals < 1) {
      throw new GraphApiError("invalid_response", "返修边上限必须为正整数");
    }
    if (value.on_exhausted === null) {
      throw new GraphApiError("invalid_response", "返修边必须声明耗尽策略");
    }
  }
  if (!nodeIds.has(value.from) || !nodeIds.has(value.to)) {
    throw new GraphApiError("invalid_response", "Graph 画布边引用了不存在的节点");
  }
  if (value.exhausted_to !== null && !nodeIds.has(value.exhausted_to)) {
    throw new GraphApiError("invalid_response", "Graph 画布返修去向引用了不存在的节点");
  }
  return value;
}

function validateCanvas(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.canvas_version !== CANVAS_VERSION ||
    typeof value.run_id !== "string" ||
    !isCanvasId(value.graph_id) ||
    !isSha256(value.spec_digest) ||
    !Number.isInteger(value.sequence) ||
    value.sequence < 1 ||
    !isCleanCode(value.status, 32) ||
    !(typeof value.current_node === "string") ||
    !isCleanLabel(value.task_label, CANVAS_LABEL_MAX) ||
    !Array.isArray(value.nodes) ||
    !Array.isArray(value.edges)
  ) {
    throw new GraphApiError("contract_mismatch", "Graph 画布契约不匹配");
  }
  try {
    validateRunId(value.run_id);
  } catch {
    throw new GraphApiError("invalid_response", "Graph 画布运行 ID 无效");
  }
  const nodeIds = new Set();
  let entryCount = 0;
  for (const node of value.nodes) {
    validateCanvasNode(node);
    if (nodeIds.has(node.node_id)) {
      throw new GraphApiError("invalid_response", "Graph 画布节点 ID 重复");
    }
    nodeIds.add(node.node_id);
    if (node.is_entry) entryCount += 1;
  }
  if (entryCount !== 1) {
    throw new GraphApiError("invalid_response", "Graph 画布必须恰好一个入口节点");
  }
  if (value.current_node !== "" && !nodeIds.has(value.current_node)) {
    throw new GraphApiError("invalid_response", "Graph 画布当前节点不存在");
  }
  const edgeIds = new Set();
  for (const edge of value.edges) {
    validateCanvasEdge(edge, nodeIds);
    if (edgeIds.has(edge.edge_id)) {
      throw new GraphApiError("invalid_response", "Graph 画布边 ID 重复");
    }
    edgeIds.add(edge.edge_id);
  }
  return value;
}

// Pilot 预案投影契约（GRAPH-PILOT-ENTRY-BRIDGE-V1-DESIGN.md §3.1 配套 GET）。
function validatePilotPlan(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.plan_version !== "1" ||
    typeof value.spec_id !== "string" ||
    !isSha256(value.spec_digest) ||
    !isCleanLabel(value.task_label, CANVAS_LABEL_MAX) ||
    !Number.isInteger(value.node_count) ||
    !Number.isInteger(value.human_gates) ||
    !Number.isInteger(value.max_feedback) ||
    !Number.isInteger(value.max_total_calls) ||
    typeof value.cost_cap_cny !== "number" ||
    !isCleanCode(value.provider, 40) ||
    !isCleanCode(value.model, 64) ||
    !isCleanLabel(value.node_flow, 200) ||
    !isCleanLabel(value.expected_output, 200) ||
    !isCleanLabel(value.write_scope, 200) ||
    !isCleanLabel(value.create_behavior, 200)
  ) {
    throw new GraphApiError("contract_mismatch", "Graph 预案契约不匹配");
  }
  return value;
}

function validatePilotRunOutcome(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !isCleanCode(value.status, 32) ||
    !Number.isInteger(value.sequence) ||
    !Number.isInteger(value.model_calls) ||
    !(value.current_node === undefined || typeof value.current_node === "string") ||
    !(value.blocked_code === undefined || isCleanCode(value.blocked_code, 64)) ||
    !(value.idempotent === undefined || typeof value.idempotent === "boolean")
  ) {
    throw new GraphApiError("invalid_response", "Graph 创建结果无效");
  }
  try {
    validateRunId(value.run_id);
  } catch {
    throw new GraphApiError("invalid_response", "Graph 创建结果运行 ID 无效");
  }
  return value;
}

function validateReceiptOutcome(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !isSha256(value.authorization_digest) ||
    !isCleanCode(value.issued_at, 40) ||
    !isCleanCode(value.expires_at, 40) ||
    (value.status !== "issued" && value.status !== "renewed") ||
    value.model_calls !== 0
  ) {
    throw new GraphApiError("invalid_response", "Graph 授权单结果无效");
  }
  try {
    validateRunId(value.run_id);
  } catch {
    throw new GraphApiError("invalid_response", "Graph 授权单运行 ID 无效");
  }
  return value;
}

function validateRetryOutcome(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    (value.status !== "retried" && value.status !== "retry_failed") ||
    !isCleanCode(value.run_status, 32) ||
    typeof value.current_node !== "string" ||
    (value.error_code !== undefined && !isCleanCode(value.error_code, 64))
  ) {
    throw new GraphApiError("invalid_response", "Graph 重试结果无效");
  }
  try {
    validateRunId(value.run_id);
    validateNodeId(value.node_id);
  } catch {
    throw new GraphApiError("invalid_response", "Graph 重试结果标识无效");
  }
  return value;
}

function createGraphClient(options) {
  const transport = options && options.transport;
  const timeoutMs = (options && options.timeoutMs) || REQUEST_TIMEOUT_MS;
  if (typeof transport !== "function") {
    throw new GraphApiError("invalid_arguments", "Graph 客户端缺少本地 transport");
  }
  const http = createHttpClient({
    transport,
    timeoutMs,
    errorClass: GraphApiError,
  });

  async function request(path, requestOptions = {}) {
    const { json } = await http.sendOnce(
      {
        url: `${GRAPH_API_BASE_URL}${path}`,
        method: requestOptions.method || "GET",
        headers: {
          Accept: "application/json",
          Origin: TRUSTED_ORIGIN,
          ...(requestOptions.headers || {}),
        },
        ...(requestOptions.body ? { body: requestOptions.body } : {}),
      },
      {
        timeoutMessage: "Graph 服务请求超时",
        serviceMessage: "Graph 服务返回",
        invalidKind: "contract_mismatch",
        invalidMessage: "Graph 服务契约不匹配",
        unreachableMessage: "无法连接本地 Graph 服务",
        errorDetails: (status, error) => ({
          status,
          code: error && typeof error.code === "string" ? error.code : null,
        }),
      }
    );
    return validateEnvelope(json);
  }

  async function listRuns() {
    const data = await request("/runs");
    if (!Array.isArray(data)) {
      throw new GraphApiError("invalid_response", "Graph 运行列表无效");
    }
    return data.map((item) => validateRunSummary(item));
  }

  async function getRun(runId) {
    return validateRunDetail(await request(`/runs/${encodeURIComponent(validateRunId(runId))}`));
  }

  async function history(runId) {
    return validateHistory(
      await request(`/runs/${encodeURIComponent(validateRunId(runId))}/history`)
    );
  }

  async function path(runId) {
    return validatePath(
      await request(`/runs/${encodeURIComponent(validateRunId(runId))}/path`)
    );
  }

  async function affected(runId, nodeId) {
    const query = encodeURIComponent(validateNodeId(nodeId));
    return validateAffected(
      await request(`/runs/${encodeURIComponent(validateRunId(runId))}/affected?node_id=${query}`)
    );
  }

  async function submitHumanDecision(input) {
    const body = {
      run_id: validateRunId(input.runId),
      node_id: validateNodeId(input.nodeId),
      decision: input.decision,
      spec_digest: input.specDigest,
      input_digest: input.inputDigest,
      expected_sequence: input.expectedSequence,
      requester: "nigo",
    };
    if (typeof body.decision !== "string" || !body.decision) {
      throw new GraphApiError("invalid_arguments", "Graph 人工决定无效");
    }
    if (!isSha256(body.spec_digest) || !isSha256(body.input_digest)) {
      throw new GraphApiError("invalid_arguments", "Graph 决定绑定摘要无效");
    }
    if (!Number.isInteger(body.expected_sequence) || body.expected_sequence < 1) {
      throw new GraphApiError("invalid_arguments", "Graph 决定绑定序号无效");
    }
    // Pilot 双闸门扩展绑定（仅 fragment-pilot-v1 必需）：授权单摘要与结果摘要。
    if (input.authorizationDigest !== undefined) {
      if (!isSha256(input.authorizationDigest)) {
        throw new GraphApiError("invalid_arguments", "Graph 授权单摘要无效");
      }
      body.authorization_digest = input.authorizationDigest;
    }
    if (input.resultDigest !== undefined) {
      if (!isSha256(input.resultDigest)) {
        throw new GraphApiError("invalid_arguments", "Graph 结果摘要无效");
      }
      body.result_digest = input.resultDigest;
    }
    body.decision_id = await graphDecisionId(body);
    return request(
      `/runs/${encodeURIComponent(body.run_id)}/human-decisions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", [DECISION_HEADER]: "1" },
        body: JSON.stringify(body),
      }
    );
  }

  async function reopenNode(input) {
    const body = {
      run_id: validateRunId(input.runId),
      node_id: validateNodeId(input.nodeId),
      expected_sequence: input.expectedSequence,
      requester: "nigo",
      reason: input.reason,
    };
    if (
      typeof body.reason !== "string" ||
      !body.reason.trim() ||
      body.reason.length > 128 ||
      /[\n\r\x1f]/.test(body.reason)
    ) {
      throw new GraphApiError("invalid_arguments", "Graph 重开原因无效");
    }
    if (!Number.isInteger(body.expected_sequence) || body.expected_sequence < 1) {
      throw new GraphApiError("invalid_arguments", "Graph 重开绑定序号无效");
    }
    body.reopen_id = await graphReopenId(body);
    return request(
      `/runs/${encodeURIComponent(body.run_id)}/nodes/${encodeURIComponent(body.node_id)}/reopen`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", [REOPEN_HEADER]: "1" },
        body: JSON.stringify(body),
      }
    );
  }

  async function canvas(runId) {
    return validateCanvas(
      await request(`/runs/${encodeURIComponent(validateRunId(runId))}/canvas`)
    );
  }

  // Pilot Entry Bridge（唯一新增写资源 + 配套 GET + Receipt 签发）。
  async function getPilotPlan() {
    return validatePilotPlan(await request(PILOT_PLAN_PATH));
  }

  async function createPilotRun(input) {
    const body = {
      spec_id: "fragment-pilot-v1",
      spec_digest: input.specDigest,
      fragment_ref: input.fragmentRef,
      candidate_id: input.candidateId,
      candidate_content_sha256: input.candidateContentSha256,
      requester: "nigo",
    };
    if (!isSha256(body.spec_digest) || !isSha256(body.candidate_content_sha256)) {
      throw new GraphApiError("invalid_arguments", "Graph 创建绑定摘要无效");
    }
    if (
      typeof body.fragment_ref !== "string" ||
      !body.fragment_ref.trim() ||
      body.fragment_ref.length > 256 ||
      /[\x00-\x1f\x7f]/.test(body.fragment_ref)
    ) {
      throw new GraphApiError("invalid_arguments", "Graph 创建碎片引用无效");
    }
    if (
      typeof body.candidate_id !== "string" ||
      !body.candidate_id.trim() ||
      body.candidate_id.length > 128 ||
      /[\x00-\x1f\x7f]/.test(body.candidate_id)
    ) {
      throw new GraphApiError("invalid_arguments", "Graph 创建候选 ID 无效");
    }
    return validatePilotRunOutcome(
      await request("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json", [RUN_CREATE_HEADER]: "1" },
        body: JSON.stringify(body),
      })
    );
  }

  async function issueAuthorizationReceipt(runId) {
    const validRunId = validateRunId(runId);
    return validateReceiptOutcome(
      await request(`/runs/${encodeURIComponent(validRunId)}/authorization-receipt`, {
        method: "POST",
        headers: { "Content-Type": "application/json", [RECEIPT_HEADER]: "1" },
        body: JSON.stringify({ run_id: validRunId, requester: "nigo" }),
      })
    );
  }

  async function issueResearchAuthorizationReceipt(runId) {
    const validRunId = validateRunId(runId);
    return validateReceiptOutcome(
      await request(`/runs/${encodeURIComponent(validRunId)}/research-authorization-receipt`, {
        method: "POST",
        headers: { "Content-Type": "application/json", [RESEARCH_RECEIPT_HEADER]: "1" },
        body: JSON.stringify({ run_id: validRunId, requester: "nigo" }),
      })
    );
  }

  // rev3 §3：发送前失败的安全重试（完整绑定 + 幂等身份）。
  async function retryPilotRun(input) {
    const body = {
      run_id: validateRunId(input.runId),
      node_id: validateNodeId(input.nodeId),
      spec_digest: input.specDigest,
      input_digest: input.inputDigest,
      authorization_digest: input.authorizationDigest,
      expected_sequence: input.expectedSequence,
      requester: "nigo",
    };
    if (
      !isSha256(body.spec_digest) ||
      !isSha256(body.input_digest) ||
      !isSha256(body.authorization_digest)
    ) {
      throw new GraphApiError("invalid_arguments", "Graph 重试绑定摘要无效");
    }
    if (!Number.isInteger(body.expected_sequence) || body.expected_sequence < 1) {
      throw new GraphApiError("invalid_arguments", "Graph 重试绑定序号无效");
    }
    body.retry_id = await graphPilotRetryId(body);
    return validateRetryOutcome(
      await request(`/runs/${encodeURIComponent(body.run_id)}/pilot-retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json", [RETRY_HEADER]: "1" },
        body: JSON.stringify(body),
      })
    );
  }

  return {
    listRuns,
    getRun,
    history,
    path,
    affected,
    canvas,
    submitHumanDecision,
    reopenNode,
    getPilotPlan,
    createPilotRun,
    issueAuthorizationReceipt,
    issueResearchAuthorizationReceipt,
    retryPilotRun,
  };
}

module.exports = {
  GRAPH_API_BASE_URL,
  GraphApiError,
  graphDecisionId,
  graphReopenId,
  graphPilotRetryId,
  createGraphClient,
};
