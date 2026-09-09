"use strict";

// Stable Graph workflow view-model and text-node-only renderers. Only
// semantic fields from the 5684 contract are consumed; coordinates, colors,
// layout, and visualization libraries never enter here. Every dynamic value
// is rendered via setText/text nodes — raw HTML injection is never used.

const RUN_STATUS_LABELS = {
  running: "运行中",
  human_wait: "等待人工",
  paused: "已暂停",
  blocked: "已阻断",
  completed: "流程已结束",
  failed: "已失败",
};

const NODE_STATUS_LABELS = {
  pending: "等待中",
  ready: "就绪",
  succeeded: "已完成",
  failed: "失败",
  waiting_human: "等待人工",
  skipped: "已跳过",
  cancelled: "已取消",
};

const DECISION_SOURCE_LABELS = {
  declared: "声明顺序",
  rule_evaluated: "规则判定",
  human_selected: "人工选择",
};

// Graph Phase 2A: read-only Agent observation block for node detail. The
// input contract follows GRAPH-PHASE2-DESIGN.md §5/§7: the backend
// observation projection exposes only call status, provider/model, max calls,
// reserved/actual tokens and the error category. Prompt bodies, response
// bodies, authorization phrases and credentials never enter the projection,
// and only the whitelisted fields below are ever read here.
const AGENT_STATUS_LABELS = {
  not_called: "未调用",
  pre_call_pending: "等待调用前确认",
  reserved: "已预留",
  completed_pending_confirmation: "结果待确认",
  completed: "已完成",
  blocked: "阻断",
  failed: "阻断",
  unknown_send: "阻断",
};

function runStatusLabel(status) {
  return RUN_STATUS_LABELS[status] || status;
}

function nodeStatusLabel(status) {
  return NODE_STATUS_LABELS[status] || status;
}

function decisionSourceLabel(source) {
  return DECISION_SOURCE_LABELS[source] || source;
}

function agentStatusLabel(status) {
  return AGENT_STATUS_LABELS[status] || status;
}

// A node either carries no agent metadata at all (Phase 1 data: the block is
// never rendered) or an observation object with the frozen fields. An
// unrecognised/missing status fails safe towards the human-check state.
function buildAgentModel(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const status = typeof raw.status === "string" ? raw.status : "unknown_send";
  return {
    status,
    statusLabel: agentStatusLabel(status),
    provider: typeof raw.provider === "string" ? raw.provider : "—",
    model: typeof raw.model === "string" ? raw.model : "—",
    maxCalls: Number.isInteger(raw.max_calls) ? raw.max_calls : null,
    reservedTokens: Number.isInteger(raw.reserved_tokens) ? raw.reserved_tokens : null,
    actualTokens: Number.isInteger(raw.actual_tokens) ? raw.actual_tokens : null,
    errorCategory: typeof raw.error_category === "string" ? raw.error_category : null,
    unknownSend: status === "unknown_send",
  };
}

function formatCount(value) {
  return value === null ? "—" : String(value);
}

function shortDigest(value) {
  if (typeof value !== "string" || value.length < 12) return value || "—";
  return `${value.slice(0, 12)}…`;
}

function buildRunListModel(runs, error) {
  return {
    empty: runs.length === 0,
    error: error || null,
    items: runs.map((run) => ({
      runId: run.run_id,
      graphId: run.graph_id,
      status: run.status,
      statusLabel: runStatusLabel(run.status),
      stepCount: run.step_count,
      sequence: run.sequence,
      pendingHuman: [...run.pending_human],
      blockedCode: run.blocked_reason ? run.blocked_reason.code : null,
    })),
  };
}

function buildRunDetailModel(detail, pathData) {
  const run = detail.run;
  const humanTasks = Object.entries(detail.human_gates)
    .filter(([, gate]) => gate.status === "pending")
    .map(([nodeId, gate]) => ({
      nodeId,
      expectedSequence: run.sequence,
      gateSequence: gate.expected_sequence,
      inputDigest: gate.input_digest,
      specDigest: gate.spec_digest,
      allowedDecisions: [...(gate.allowed_decisions || [])],
      requester: gate.requester,
    }));
  const criticalPath = (pathData ? pathData.path : detail.edges_taken).map((edge) => ({
    edgeId: edge.edge_id,
    from: edge.from,
    to: edge.to,
    type: edge.type,
    reason: edge.reason,
    decisionSource: edge.decision_source,
    decisionSourceLabel: decisionSourceLabel(edge.decision_source),
  }));
  return {
    runId: run.run_id,
    graphId: run.graph_id,
    status: run.status,
    statusLabel: runStatusLabel(run.status),
    currentNode: run.current_node,
    stepCount: run.step_count,
    sequence: run.sequence,
    specDigest: run.spec_digest,
    fragmentRef: run.fragment_ref,
    blockedReason: run.blocked_reason,
    pendingHuman: [...run.pending_human],
    ready: [...detail.ready],
    frontier: pathData ? [...pathData.frontier] : [],
    feedbackCounts: { ...detail.feedback_counts },
    humanTasks,
    criticalPath,
    nodes: detail.nodes.map((node) => ({
      nodeId: node.node_id,
      status: node.status,
      statusLabel: nodeStatusLabel(node.status),
      attempts: node.attempts,
      error: node.error,
      childRunId: node.child_run_id || null,
      memoryRefCount: node.memory_refs.length,
    })),
  };
}

function buildNodeDetailModel(detail, nodeId) {
  const node = detail.nodes.find((item) => item.node_id === nodeId);
  if (!node) return null;
  const gate = detail.human_gates[nodeId] || null;
  const edges = detail.edges_taken.filter(
    (edge) => edge.from === nodeId || edge.to === nodeId
  );
  return {
    nodeId: node.node_id,
    status: node.status,
    statusLabel: nodeStatusLabel(node.status),
    attempts: node.attempts,
    inputDigest: node.input_digest,
    outputDigest: node.output_digest,
    inputRefs: [...node.input_refs],
    outputRefs: [...node.output_refs],
    memoryRefs: [...node.memory_refs],
    output: node.output,
    error: node.error,
    completedAt: node.completed_at,
    childRunId: node.child_run_id || null,
    absorbedFailures: [...(node.absorbed_failures || [])],
    edges: edges.map((edge) => ({
      edgeId: edge.edge_id,
      from: edge.from,
      to: edge.to,
      reason: edge.reason,
      decisionSourceLabel: decisionSourceLabel(edge.decision_source),
    })),
    gate: gate
      ? {
          status: gate.status,
          decision: gate.decision,
          decidedAt: gate.decided_at,
          expectedSequence: gate.expected_sequence,
        }
      : null,
    agent: buildAgentModel(node.agent),
  };
}

function el(parent, tag, className, text) {
  const node = parent.createEl(tag, { cls: className });
  if (typeof text !== "undefined") node.setText(text);
  return node;
}

function renderRunList(container, model, handlers) {
  container.empty();
  el(container, "h2", "graph-view-title", "Graph 工作流");
  if (model.error) {
    el(container, "p", "graph-view-error", model.error);
  }
  if (model.empty && !model.error) {
    el(container, "p", "graph-view-empty", "当前没有 Graph 运行实例。");
    return;
  }
  const list = el(container, "ul", "graph-run-list");
  for (const item of model.items) {
    const row = list.createEl("li", { cls: "graph-run-row" });
    const button = row.createEl("button", { cls: "graph-run-open" });
    button.setAttribute("type", "button");
    el(
      button,
      "span",
      "graph-run-row-title",
      `${item.graphId} · ${item.statusLabel}`
    );
    el(
      button,
      "span",
      "graph-run-row-meta",
      `节点边界 ${item.stepCount} · Checkpoint #${item.sequence}` +
        (item.pendingHuman.length ? ` · 人工待办 ${item.pendingHuman.length}` : "") +
        (item.blockedCode ? ` · 阻断 ${item.blockedCode}` : "")
    );
    button.addEventListener("click", () => handlers.onOpenRun(item.runId));
  }
  if (handlers.onRefresh) {
    const refresh = container.createEl("button", { cls: "graph-view-refresh" });
    refresh.setAttribute("type", "button");
    refresh.setText("刷新");
    refresh.addEventListener("click", () => handlers.onRefresh());
  }
}

function renderRunDetail(container, model, handlers) {
  container.empty();
  const header = el(container, "div", "graph-detail-header");
  const back = header.createEl("button", { cls: "graph-view-back" });
  back.setAttribute("type", "button");
  back.setText("← 返回列表");
  back.addEventListener("click", () => handlers.onBack());
  el(header, "h2", "graph-view-title", `${model.graphId} · ${model.statusLabel}`);

  const summary = el(container, "div", "graph-run-summary");
  el(summary, "p", "graph-summary-line", `当前节点：${model.currentNode}`);
  el(
    summary,
    "p",
    "graph-summary-line",
    `节点边界 ${model.stepCount} · Checkpoint #${model.sequence} · 规范摘要 ${shortDigest(model.specDigest)}`
  );
  if (model.blockedReason) {
    el(
      summary,
      "p",
      "graph-view-error",
      `阻断原因：${model.blockedReason.code} — ${model.blockedReason.message}`
    );
  }
  const feedbackEntries = Object.entries(model.feedbackCounts);
  if (feedbackEntries.length) {
    el(
      summary,
      "p",
      "graph-summary-line",
      `反馈计数：${feedbackEntries.map(([edge, count]) => `${edge} ×${count}`).join("，")}`
    );
  }

  if (model.humanTasks.length) {
    const tasks = el(container, "div", "graph-human-tasks");
    el(tasks, "h3", "graph-section-title", "人工待办");
    for (const task of model.humanTasks) {
      const card = tasks.createDiv({ cls: "graph-human-task" });
      el(card, "p", "graph-human-task-title", `节点 ${task.nodeId} 等待你的决定`);
      el(
        card,
        "p",
        "graph-human-task-meta",
        `绑定 Checkpoint #${task.gateSequence} · 输入摘要 ${shortDigest(task.inputDigest)}`
      );
      const actions = card.createDiv({ cls: "graph-human-task-actions" });
      for (const decision of task.allowedDecisions) {
        const button = actions.createEl("button", {
          cls: `graph-decision graph-decision-${decision}`,
        });
        button.setAttribute("type", "button");
        button.setText(decision === "approve" ? "确认" : decision === "reject" ? "拒绝" : decision);
        button.addEventListener("click", () =>
          handlers.onDecision(task, decision)
        );
      }
    }
  }

  const pathSection = el(container, "div", "graph-critical-path");
  el(pathSection, "h3", "graph-section-title", "关键路径与边的选择原因");
  if (!model.criticalPath.length) {
    el(pathSection, "p", "graph-view-empty", "尚未走过任何边。");
  }
  const pathList = pathSection.createEl("ol", { cls: "graph-path-list" });
  for (const edge of model.criticalPath) {
    const item = pathList.createEl("li", { cls: "graph-path-item" });
    el(item, "span", "graph-path-edge", `${edge.from} → ${edge.to}`);
    el(
      item,
      "span",
      "graph-path-reason",
      `${edge.decisionSourceLabel} · ${edge.reason}`
    );
  }

  const nodes = el(container, "div", "graph-node-list");
  el(nodes, "h3", "graph-section-title", "节点与分支状态");
  const nodeList = nodes.createEl("ul", { cls: "graph-nodes" });
  for (const node of model.nodes) {
    const row = nodeList.createEl("li", { cls: `graph-node-row graph-node-${node.status}` });
    const button = row.createEl("button", { cls: "graph-node-open" });
    button.setAttribute("type", "button");
    el(button, "span", "graph-node-name", node.nodeId);
    el(
      button,
      "span",
      "graph-node-meta",
      `${node.statusLabel} · 尝试 ${node.attempts}` +
        (node.memoryRefCount ? ` · 关联知识 ${node.memoryRefCount}` : "") +
        (node.childRunId ? " · 子图运行" : "") +
        (node.error ? ` · ${node.error.code}` : "")
    );
    button.addEventListener("click", () => handlers.onOpenNode(node.nodeId));
  }
}

function renderNodeDetail(container, model, handlers) {
  container.empty();
  const header = el(container, "div", "graph-detail-header");
  const back = header.createEl("button", { cls: "graph-view-back" });
  back.setAttribute("type", "button");
  back.setText("← 返回运行实例");
  back.addEventListener("click", () => handlers.onBack());
  el(header, "h2", "graph-view-title", `节点 ${model.nodeId}`);

  const summary = el(container, "div", "graph-node-summary");
  el(
    summary,
    "p",
    "graph-summary-line",
    `状态：${model.statusLabel} · 尝试 ${model.attempts}` +
      (model.completedAt ? ` · 完成于 ${model.completedAt}` : "")
  );
  if (model.error) {
    el(summary, "p", "graph-view-error", `失败：${model.error.code} — ${model.error.message}`);
  }
  el(summary, "p", "graph-summary-line", `输入摘要：${shortDigest(model.inputDigest)}`);
  el(summary, "p", "graph-summary-line", `输出摘要：${shortDigest(model.outputDigest)}`);
  if (model.childRunId) {
    el(summary, "p", "graph-summary-line", `子图运行：${model.childRunId}`);
  }
  if (model.gate) {
    el(
      summary,
      "p",
      "graph-summary-line",
      `人工闸门：${model.gate.status}` +
        (model.gate.decision ? ` · 决定 ${model.gate.decision}` : "")
    );
  }

  // Phase 2A: minimal read-only Agent call area. Rendered only when the node
  // carries agent metadata; Phase 1 nodes render exactly as before. The area
  // never offers a retry action — unknown_send only tells the user to stop
  // and inspect manually.
  if (model.agent) {
    const agent = el(container, "div", "graph-node-agent");
    el(agent, "h3", "graph-section-title", "Agent 调用（只读观察）");
    el(agent, "p", "graph-agent-status", `状态：${model.agent.statusLabel}`);
    el(
      agent,
      "p",
      "graph-summary-line",
      `Provider／模型：${model.agent.provider} / ${model.agent.model}` +
        ` · 调用次数上限 ${formatCount(model.agent.maxCalls)}`
    );
    el(
      agent,
      "p",
      "graph-summary-line",
      `预留 token：${formatCount(model.agent.reservedTokens)}` +
        ` · 实际 token：${formatCount(model.agent.actualTokens)}`
    );
    if (model.agent.errorCategory) {
      el(agent, "p", "graph-view-error", `错误类别：${model.agent.errorCategory}`);
    }
    el(agent, "p", "graph-agent-note", "结果性质：草稿 · 未验证 · 无外部写入");
    if (model.agent.unknownSend) {
      el(
        agent,
        "p",
        "graph-agent-unknown",
        "发送状态未知：停止并人工检查；额度按已消费处理，不提供重试。"
      );
    }
  }

  if (model.edges.length) {
    const edges = el(container, "div", "graph-node-edges");
    el(edges, "h3", "graph-section-title", "相关边与原因");
    const list = edges.createEl("ul", { cls: "graph-edge-list" });
    for (const edge of model.edges) {
      const item = list.createEl("li", { cls: "graph-edge-item" });
      el(
        item,
        "span",
        "graph-edge-text",
        `${edge.from} → ${edge.to} · ${edge.decisionSourceLabel} · ${edge.reason}`
      );
    }
  }

  const refs = el(container, "div", "graph-node-refs");
  el(refs, "h3", "graph-section-title", "输入输出引用");
  const refList = refs.createEl("ul", { cls: "graph-ref-list" });
  const allRefs = [
    ...model.inputRefs.map((ref) => ({ ref, kind: "输入" })),
    ...model.outputRefs.map((ref) => ({ ref, kind: "输出" })),
  ];
  if (!allRefs.length) {
    el(refs, "p", "graph-view-empty", "该节点没有记录输入输出引用。");
  }
  for (const { ref, kind } of allRefs) {
    const item = refList.createEl("li", { cls: "graph-ref-item" });
    el(item, "span", "graph-ref-text", `${kind} · ${ref}`);
  }

  if (model.memoryRefs.length) {
    const memory = el(container, "div", "graph-node-memory");
    el(memory, "h3", "graph-section-title", "只读关联知识");
    el(
      memory,
      "p",
      "graph-memory-note",
      "以下为已确认资产与候选的只读引用；用户确认不等于事实已验证（均为 unverified）。"
    );
    const list = memory.createEl("ul", { cls: "graph-memory-list" });
    for (const ref of model.memoryRefs) {
      const item = list.createEl("li", { cls: "graph-memory-item" });
      el(item, "span", "graph-memory-ref", ref);
    }
  }

  if (model.output && typeof model.output === "object") {
    const output = el(container, "div", "graph-node-output");
    el(output, "h3", "graph-section-title", "节点输出（摘要）");
    const list = output.createEl("ul", { cls: "graph-output-list" });
    for (const [key, value] of Object.entries(model.output)) {
      const item = list.createEl("li", { cls: "graph-output-item" });
      const rendered =
        typeof value === "string" ? value : JSON.stringify(value, null, 0);
      const trimmed = rendered.length > 160 ? `${rendered.slice(0, 160)}…` : rendered;
      el(item, "span", "graph-output-text", `${key}：${trimmed}`);
    }
  }
}

module.exports = {
  buildRunListModel,
  buildRunDetailModel,
  buildNodeDetailModel,
  renderRunList,
  renderRunDetail,
  renderNodeDetail,
  runStatusLabel,
  nodeStatusLabel,
  decisionSourceLabel,
  agentStatusLabel,
};
