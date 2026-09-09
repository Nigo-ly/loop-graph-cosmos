"use strict";

// L2 Canvas semantic adapter (GRAPH-CANVAS-V1-DESIGN.md §2): turns the raw
// canvas contract into the semantic view model — Chinese labels, semantic
// states, sidebar participation entries. Layout (L3) and rendering (L4) never
// read the raw contract directly. Unknown display enums degrade safely here,
// never crash and never guess.

const {
  runStatusLabel,
  nodeStatusLabel,
  decisionSourceLabel,
} = require("./graph-view-model.js");

const KIND_LABELS = {
  input: "输入",
  router: "路由",
  capability: "能力",
  validator: "验证",
  human_decision: "人工决定",
  action: "动作",
  subgraph: "子图",
  join: "汇合",
  output: "输出",
};

const EDGE_TYPE_LABELS = {
  sequence: "顺序",
  condition: "条件",
  feedback: "返修",
};

const JOIN_MODE_LABELS = {
  all_success: "全部成功才汇合",
  minimum_success: "达到数量即汇合",
};

const PARTIAL_FAILURE_LABELS = {
  fail: "部分失败即失败",
  continue: "部分失败仍继续",
  pause: "部分失败即暂停",
};

const EXHAUSTED_LABELS = {
  fail: "返修耗尽即失败",
  pause: "返修耗尽即暂停",
  route: "返修耗尽后改道",
};

// rev3：CSS class 只能来自前端固定白名单。未知 kind/status 使用固定
// kind-unknown / is-unknown；原始未知值只允许作为纯文本显示。
const KIND_CLASS_WHITELIST = new Set(Object.keys(KIND_LABELS));
const NODE_STATUS_CLASS_WHITELIST = new Set([
  "pending", "ready", "succeeded", "failed", "waiting_human", "skipped", "cancelled",
]);
const RUN_STATUS_CLASS_WHITELIST = new Set([
  "running", "paused", "human_wait", "blocked", "completed", "failed",
]);

function kindClass(kind) {
  return KIND_CLASS_WHITELIST.has(kind) ? `kind-${kind}` : "kind-unknown";
}

function nodeStatusClass(status) {
  return NODE_STATUS_CLASS_WHITELIST.has(status) ? `is-${status}` : "is-unknown";
}

function runStatusClass(status) {
  return RUN_STATUS_CLASS_WHITELIST.has(status) ? `is-${status}` : "is-unknown";
}

function kindLabel(kind) {
  return KIND_LABELS[kind] || "未知类型";
}

function edgeTypeLabel(type) {
  return EDGE_TYPE_LABELS[type] || type;
}

function joinModeLabel(mode) {
  return JOIN_MODE_LABELS[mode] || mode || "";
}

function partialFailureLabel(policy) {
  return PARTIAL_FAILURE_LABELS[policy] || policy || "";
}

function exhaustedLabel(policy) {
  return EXHAUSTED_LABELS[policy] || policy || "";
}

function shortDigest(value) {
  return typeof value === "string" && value.length >= 12 ? `${value.slice(0, 12)}…` : value || "—";
}

// Pilot 首闸授权上下文与次闸结构化结果的视图模型（契约层已校验形状与
// 过滤规则，这里只做安全拷贝；展示与实发来自同一 canonical 对象）。
function buildAuthorizationModel(value) {
  if (!value || typeof value !== "object") return null;
  const fields = value.input_fields;
  return {
    authorizationDigest: value.authorization_digest,
    provider: value.provider,
    model: value.model,
    maxTotalCalls: value.max_total_calls,
    costCapCny: value.cost_cap_cny,
    inputDigest: value.input_digest,
    agentInputDigest: value.agent_input_digest,
    inputDigestShort: shortDigest(value.input_digest),
    issuedAt: value.issued_at,
    expiresAt: value.expires_at,
    priceSnapshotSource: value.price_snapshot_source || null,
    inputFields: fields
      ? {
          title: fields.title,
          coreJudgment: fields.core_judgment,
          userValue: fields.user_value,
          cardTitles: [...fields.card_titles],
        }
      : null,
  };
}

function buildAuthorizationPreviewModel(value) {
  if (!value || typeof value !== "object") return null;
  return {
    provider: value.provider,
    model: value.model,
    maxTotalCalls: value.max_total_calls,
    costCapCny: value.cost_cap_cny,
    writeScope: value.write_scope,
    inputFields: {
      title: value.input_fields.title,
      coreJudgment: value.input_fields.core_judgment,
      userValue: value.input_fields.user_value,
      cardTitles: [...value.input_fields.card_titles],
    },
  };
}

function buildResultModel(value) {
  if (!value || typeof value !== "object") return null;
  return {
    available: value.available === true,
    resultDigest: value.result_digest,
    summary: value.available === true ? value.summary : null,
    unknowns: value.available === true ? [...value.unknowns] : [],
    nextChecks: value.available === true ? [...value.next_checks] : [],
  };
}

function buildResearchPreviewModel(value) {
  if (!value || typeof value !== "object") return null;
  return {
    provider: value.provider,
    model: value.model,
    maxTotalCalls: value.max_total_calls,
    costCapCny: value.cost_cap_cny,
    researchGoal: value.research_goal,
    evidenceCount: value.evidence_count,
    sources: value.sources.map((source) => ({ ...source })),
    writeScope: value.write_scope,
  };
}

function buildResearchAuthorizationModel(value) {
  if (!value || typeof value !== "object") return null;
  return {
    authorizationDigest: value.authorization_digest,
    provider: value.provider,
    model: value.model,
    maxTotalCalls: value.max_total_calls,
    inputDigest: value.input_digest,
    inputDigestShort: shortDigest(value.input_digest),
    expectedSequence: value.expected_sequence,
    issuedAt: value.issued_at,
    expiresAt: value.expires_at,
    priceSnapshotSource: value.price_snapshot_source,
  };
}

function buildResearchResultModel(value) {
  if (!value || typeof value !== "object") return null;
  return {
    available: value.available === true,
    resultDigest: value.result_digest,
    summary: value.summary,
    confirmed: value.confirmed.map((item) => ({ ...item })),
    unknowns: [...value.unknowns],
    conflicts: value.conflicts.map((item) => ({ ...item })),
    recommendation: value.recommendation,
    claims: value.claims.map((item) => ({ ...item })),
  };
}

// 边说明：固定映射 label 优先；条件边无法映射时只显示「条件」；绝不显示
// reason 原文或条件值。exhausted_to 经 nodeMap 转为 display_label，技术 ID
// 不进入主视觉。
function buildEdgeModel(edge, nodeLabels) {
  const isFeedback = edge.type === "feedback";
  let note = edgeTypeLabel(edge.type);
  if (edge.type === "condition") note = edge.label || "条件";
  else if (edge.label) note = edge.label;
  const parts = [note];
  if (edge.decision_source) parts.push(decisionSourceLabel(edge.decision_source));
  if (isFeedback && edge.max_traversals !== null) {
    parts.push(`返修 ${edge.taken_count}/${edge.max_traversals}`);
    if (edge.on_exhausted) {
      const targetLabel = edge.exhausted_to ? nodeLabels.get(edge.exhausted_to) : null;
      parts.push(exhaustedLabel(edge.on_exhausted) + (targetLabel ? `（转向 ${targetLabel}）` : ""));
    }
  }
  return {
    edgeId: edge.edge_id,
    from: edge.from,
    to: edge.to,
    type: edge.type,
    typeLabel: edgeTypeLabel(edge.type),
    taken: edge.taken,
    takenLabel: edge.taken ? "已走过" : "未走过",
    decisionSourceLabel: edge.decision_source ? decisionSourceLabel(edge.decision_source) : null,
    label: note,
    feedbackBadge: isFeedback && edge.max_traversals !== null ? `返修 ${edge.taken_count}/${edge.max_traversals}` : null,
    exhaustedNote: isFeedback && edge.on_exhausted
      ? exhaustedLabel(edge.on_exhausted) + (nodeLabels.get(edge.exhausted_to) ? `（转向 ${nodeLabels.get(edge.exhausted_to)}）` : "")
      : null,
    summary: parts.join(" · "),
    isFeedback,
  };
}

function buildNodeModel(node) {
  const gate = node.human_gate;
  const needsHuman = Boolean(gate && gate.status === "pending");
  return {
    nodeId: node.node_id,
    label: node.display_label || "未知节点",
    kind: node.kind,
    kindLabel: kindLabel(node.kind),
    kindClass: kindClass(node.kind),
    status: node.status,
    statusLabel: nodeStatusLabel(node.status),
    statusClass: nodeStatusClass(node.status),
    isEntry: node.is_entry,
    isCurrent: node.is_current,
    isFrontier: node.is_frontier,
    join: node.join
      ? {
          modeLabel: joinModeLabel(node.join.mode),
          partialFailureLabel: partialFailureLabel(node.join.on_partial_failure),
          cancelRemaining: node.join.cancel_remaining === true,
        }
      : null,
    needsHuman,
    allowedDecisions: needsHuman ? [...(gate.allowed_decisions || [])] : [],
    authorization: gate ? buildAuthorizationModel(gate.authorization) : null,
    authorizationPreview: gate
      ? buildAuthorizationPreviewModel(gate.authorization_preview)
      : null,
    result: gate ? buildResultModel(gate.result) : null,
    researchAuthorizationPreview: gate
      ? buildResearchPreviewModel(gate.research_authorization_preview)
      : null,
    researchAuthorization: gate
      ? buildResearchAuthorizationModel(gate.research_authorization)
      : null,
    researchResult: gate ? buildResearchResultModel(gate.research_result) : null,
    resultSummary: typeof node.result_summary === "string" ? node.result_summary : null,
    outputDigest: typeof node.output_digest === "string" ? node.output_digest : null,
    outputDigestShort: node.output_digest ? shortDigest(node.output_digest) : null,
    errorCode: node.error_code,
    completedAt: node.completed_at,
  };
}

function buildCanvasModel(canvas) {
  const nodes = canvas.nodes.map(buildNodeModel);
  const nodeLabels = new Map(nodes.map((node) => [node.nodeId, node.label]));
  const edges = canvas.edges.map((edge) => buildEdgeModel(edge, nodeLabels));
  const waitingNode = nodes.find((node) => node.needsHuman) || null;
  return {
    runId: canvas.run_id,
    graphId: canvas.graph_id,
    specDigest: canvas.spec_digest,
    sequence: canvas.sequence,
    status: canvas.status,
    statusLabel: runStatusLabel(canvas.status),
    statusClass: runStatusClass(canvas.status),
    currentNode: canvas.current_node,
    taskLabel: canvas.task_label || "未命名工作流",
    nodes,
    edges,
    nodeMap: new Map(nodes.map((node) => [node.nodeId, node])),
    waitingNode,
    needsAttention: Boolean(waitingNode) || nodes.some((node) => node.status === "failed"),
  };
}

module.exports = {
  buildCanvasModel,
  kindClass,
  nodeStatusClass,
  runStatusClass,
  buildNodeModel,
  buildEdgeModel,
  buildAuthorizationModel,
  buildAuthorizationPreviewModel,
  buildResultModel,
  kindLabel,
  edgeTypeLabel,
  joinModeLabel,
  partialFailureLabel,
  exhaustedLabel,
  shortDigest,
};
