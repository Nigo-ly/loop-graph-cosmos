"use strict";

// Independent Obsidian ItemView for Graph Phase 1: list a run, open it, read
// the key path and branches, enter a node detail with its read-only memory
// references, act on a pending human gate, and navigate back. All rendering
// goes through graph-view-model.js text-node-only renderers.

const { ItemView } = require("obsidian");
const { isExpiredAt } = require("./view-model.js");
const model = require("./graph-view-model.js");
const { buildCanvasModel } = require("./graph-canvas-model.js");
const { computeCanvasLayout } = require("./graph-canvas-layout.js");
const { renderGraphCanvas } = require("./graph-canvas-view.js");

const GRAPH_WORKFLOW_VIEW_TYPE = "my-life-graph-workflow";

// Pilot 决定按钮中文文案；未知决定原样显示（契约已限长、拒控制字符）。
const PILOT_DECISION_LABELS = {
  approve: "确认",
  reject: "拒绝",
  approve_call: "授权并执行",
  abort: "中止本次运行",
  accept_draft: "接受为草稿",
  approve_synthesis: "授权并生成研究结论",
  accept_result: "接受研究结果",
};

function pilotDecisionLabel(decision) {
  return PILOT_DECISION_LABELS[decision] || decision;
}

// 授权单签发失败的诚实中文提示（按服务端稳定错误码）。
const PILOT_ISSUE_ERROR_LABELS = {
  price_unavailable: "价格核验不可用，未签发授权单；未产生任何模型调用。",
  cost_cap_exceeded: "最新价格超出冻结成本上限，未签发授权单。",
  candidate_version_drift: "候选内容已变化，请回到主页重新确认后再签发。",
  reservation_exists: "该运行已有调用预留，禁止重复签发授权单。",
  authorization_still_valid: "授权单仍在有效期内，无需重新签发。",
  gate_not_pending: "当前闸门状态已变化，请刷新后重试。",
};

// 安全重试拒绝的诚实中文提示（rev3 §3）。
const PILOT_RETRY_ERROR_LABELS = {
  retry_not_allowed: "当前运行状态不满足安全重试条件。",
  retry_never_resend: "该调用可能已经发出，绝不重发；请人工检查。",
  authorization_missing: "授权单不存在，无法安全重试。",
  authorization_digest_mismatch: "授权绑定已变化，请刷新。",
  authorization_expired: "授权单已过期，无法安全重试。",
  sequence_mismatch: "数据已变化，请刷新。",
  candidate_version_drift: "候选内容已变化，无法安全重试。",
};

// G1：人工决定提交失败的诚实中文提示（服务端拒绝码；无码回落 err.message）。
// 文案与既有授权/重试错误表语义一致（两入口回响统一，设计 §4.6）。
const GRAPH_DECISION_ERROR_LABELS = {
  sequence_mismatch: "数据已变化，请刷新后重试。",
  candidate_version_drift: "候选内容已变化，请回到主页重新确认后再试。",
  gate_not_pending: "当前闸门状态已变化，请刷新后重试。",
  authorization_missing: "授权单不存在，请重新签发。",
  authorization_digest_mismatch: "授权绑定已变化，请刷新。",
  authorization_expired: "授权单已过期，请重新签发。",
  reservation_exists: "该运行已有调用预留，禁止重复提交。",
  price_unavailable: "价格核验不可用，未提交决定。",
  cost_cap_exceeded: "最新价格超出冻结成本上限，未提交决定。",
};

class GraphWorkflowView extends ItemView {
  constructor(leaf, client, options) {
    super(leaf);
    this.client = client;
    // R2 §4.6：来源级互斥锁（队列卡与原位入口共享）——decide 前检查。
    this.sharedAuthLocks = (options && options.sharedAuthLocks) || null;
    // Monotonic navigation generation: a slow response can never render into
    // a screen the user has already left.
    this.generation = 0;
    this.screen = { name: "list", runId: null, nodeId: null };
    this.detail = null;
    this.pathData = null;
    this.canvasData = null;
    this.canvasDispose = null;
    this.selectedCanvasNode = null;
    this.disposed = false;
    this.pendingOperation = null; // 视图级 single-flight：刷新与人工决定共享
    this.selectedCanvasEdge = null;
    this.waitingOnly = false; // G4：列表「只看等待人工」过滤状态（会话级）
  }

  // 统一清理：返回列表、进入文本节点详情、重渲染、关闭视图都必须走这里。
  disposeCanvas() {
    if (this.canvasDispose) {
      this.canvasDispose();
      this.canvasDispose = null;
    }
    this.selectedCanvasNode = null;
    this.selectedCanvasEdge = null;
  }

  getViewType() {
    return GRAPH_WORKFLOW_VIEW_TYPE;
  }

  getDisplayText() {
    return "Graph 工作流";
  }

  getIcon() {
    return "git-branch";
  }

  async onOpen() {
    this.disposed = false; // rev3：同一 ItemView 重新打开必须可用
    // 画布样式的作用域类：Obsidian 只给 leaf 加 data-type，不会自动加这个
    // class——不加它，.my-life-graph-workflow 下的画布样式全部不生效。
    this.contentEl.addClass("my-life-graph-workflow");
    await this.showList();
  }

  async showList() {
    this.disposeCanvas();
    const generation = ++this.generation;
    this.screen = { name: "list", runId: null, nodeId: null };
    this.detail = null;
    this.pathData = null;
    this.canvasData = null;
    let runs = [];
    let error = "";
    try {
      runs = await this.client.listRuns();
    } catch (err) {
      error = err && err.message ? err.message : "本地 Graph 服务暂不可用";
    }
    if (this.disposed || generation !== this.generation) return;
    model.renderRunList(this.contentEl, model.buildRunListModel(runs, error, this.waitingOnly), {
      onOpenRun: (runId) => this.openRun(runId),
      onRefresh: () => this.showList(),
      onToggleWaiting: () => this.toggleWaitingFilter(),
    });
  }

  // G4：切换「只看等待人工」过滤，随后重拉列表（权威数据，非本地过滤）。
  toggleWaitingFilter() {
    this.waitingOnly = !this.waitingOnly;
    void this.showList();
  }

  // canvas 契约 404 / 契约不符 / 服务不可达一律回退现有文本视图。
  async fetchCanvasSafe(runId) {
    if (typeof this.client.canvas !== "function") return null;
    try {
      return await this.client.canvas(runId);
    } catch {
      return null;
    }
  }

  // Canvas 只负责可视化；与 detail 的 run_id / spec_digest / sequence 任一
  // 不一致时禁止提交人工决定（GRAPH-CANVAS-V1-DESIGN.md §3.2）。
  canvasConsistent() {
    if (!this.canvasData || !this.detail || !this.detail.run) return false;
    return (
      this.canvasData.run_id === this.detail.run.run_id &&
      this.canvasData.spec_digest === this.detail.run.spec_digest &&
      this.canvasData.sequence === this.detail.run.sequence
    );
  }

  async openRun(runId) {
    const generation = ++this.generation;
    this.screen = { name: "run", runId, nodeId: null };
    try {
      const [detail, pathData, canvasData] = await Promise.all([
        this.client.getRun(runId),
        this.client.path(runId),
        this.fetchCanvasSafe(runId),
      ]);
      if (this.disposed || generation !== this.generation || this.screen.runId !== runId) return;
      this.detail = detail;
      this.pathData = pathData;
      this.canvasData = canvasData;
    } catch (err) {
      if (this.disposed || generation !== this.generation) return;
      this.contentEl.empty();
      const errorText = err && err.message ? err.message : "无法读取 Graph 运行实例";
      const message = this.contentEl.createEl("p", { cls: "graph-view-error" });
      message.setText(errorText);
      const back = this.contentEl.createEl("button", { cls: "graph-view-back" });
      back.setAttribute("type", "button");
      back.setText("← 返回列表");
      back.addEventListener("click", () => this.showList());
      return;
    }
    this.renderRun();
  }

  renderRun() {
    if (!this.detail || this.disposed) return;
    this.disposeCanvas();
    if (!this.canvasData) {
      model.renderRunDetail(
        this.contentEl,
        model.buildRunDetailModel(this.detail, this.pathData),
        {
          onBack: () => this.showList(),
          onOpenNode: (nodeId) => this.openNode(nodeId),
          onDecision: (task, decision) => this.decide(task, decision),
          onRefreshRun: () => this.refreshRun(),
        }
      );
      return;
    }
    // 画布模式：画布 + 中文侧栏 + 默认折叠的审计区（既有文本详情整体迁入）。
    const canvasModel = buildCanvasModel(this.canvasData);
    const layout = computeCanvasLayout(this.canvasData);
    this.contentEl.empty();
    const header = this.contentEl.createDiv({ cls: "graph-detail-header" });
    const back = header.createEl("button", { cls: "graph-view-back" });
    back.setAttribute("type", "button");
    back.setText("← 返回列表");
    back.addEventListener("click", () => this.showList());

    const body = this.contentEl.createDiv({ cls: "graph-canvas-body" });
    const canvasHost = body.createDiv({ cls: "graph-canvas-host" });
    this.sidebarEl = body.createDiv({ cls: "graph-canvas-sidebar" });
    this.canvasModel = canvasModel;
    const rendered = renderGraphCanvas(canvasHost, canvasModel, layout, {
      onSelectNode: (nodeId) => this.selectCanvasNode(nodeId),
      onSelectEdge: (edgeId) => this.selectCanvasEdge(edgeId),
      onRefresh: () => this.refreshRun(),
    });
    this.canvasDispose = rendered.dispose;
    this.renderCanvasSidebar(canvasModel);

    const audit = this.contentEl.createEl("details", { cls: "graph-audit" });
    audit.createEl("summary", { text: "审计区（技术详情与文本视图）" });
    const auditBody = audit.createDiv({ cls: "graph-audit-body" });
    model.renderRunDetail(
      auditBody,
      model.buildRunDetailModel(this.detail, this.pathData),
      {
        onBack: () => this.showList(),
        onOpenNode: (nodeId) => this.openNode(nodeId),
        onDecision: (task, decision) => this.decide(task, decision),
        onRefreshRun: () => this.refreshRun(),
      }
    );
  }

  selectCanvasNode(nodeId) {
    this.selectedCanvasNode = nodeId;
    this.selectedCanvasEdge = null;
    if (this.canvasData) this.renderCanvasSidebar(buildCanvasModel(this.canvasData));
  }

  selectCanvasEdge(edgeId) {
    this.selectedCanvasEdge = edgeId;
    this.selectedCanvasNode = null;
    if (this.canvasData) this.renderCanvasSidebar(buildCanvasModel(this.canvasData));
  }

  renderCanvasSidebar(canvasModel) {
    const sidebar = this.sidebarEl;
    if (!sidebar) return;
    sidebar.empty();
    const edgeId = this.selectedCanvasEdge;
    if (edgeId) {
      const edge = (canvasModel.edges || []).find((item) => item.edgeId === edgeId);
      if (!edge) return;
      sidebar.createEl("h3", { cls: "graph-canvas-sidebar-title", text: "边的选择说明" });
      sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: `类型：${edge.typeLabel} · ${edge.takenLabel}` });
      if (edge.label) sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: `说明：${edge.label}` });
      if (edge.decisionSourceLabel) {
        sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: `选择方式：${edge.decisionSourceLabel}` });
      }
      if (edge.feedbackBadge) sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: edge.feedbackBadge });
      if (edge.exhaustedNote) sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: edge.exhaustedNote });
      return;
    }
    const nodeId = this.selectedCanvasNode;
    const node = nodeId ? canvasModel.nodeMap.get(nodeId) : null;
    if (!node) {
      sidebar.createEl("p", { cls: "graph-canvas-sidebar-hint", text: "点击画布中的节点，查看状态与需要参与的事项。" });
      return;
    }
    sidebar.createEl("h3", { cls: "graph-canvas-sidebar-title", text: node.label });
    sidebar.createEl("p", {
      cls: "graph-canvas-sidebar-line",
      text: `类型：${node.kindLabel} · 状态：${node.statusLabel}`,
    });
    if (node.completedAt) {
      sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: `完成于 ${node.completedAt}` });
    }
    if (node.errorCode) {
      sidebar.createEl("p", { cls: "graph-canvas-sidebar-error", text: `失败：${node.errorCode}` });
    }
    // rev3 §3：发送前失败的安全重试——可见性条件只决定按钮是否展示，
    // 服务端仍做完整绑定核验（request_sent=true/unknown 绝不出现入口）。
    const retryCandidate = this.pilotRetryCandidate(node);
    if (retryCandidate) {
      const retryBlock = sidebar.createDiv({ cls: "graph-pilot-retry-block" });
      retryBlock.createEl("p", {
        cls: "graph-canvas-sidebar-line",
        text: "上一次调用在发送前失败，没有产生真实发送；可以在同一授权下安全重试。",
      });
      const retry = retryBlock.createEl("button", { cls: "graph-pilot-retry", text: "安全重试" });
      retry.setAttribute("type", "button");
      retry.addEventListener("click", () => this.retryPilot(node, retryCandidate));
    }
    if (node.join) {
      sidebar.createEl("p", {
        cls: "graph-canvas-sidebar-line",
        text: `汇合：${node.join.modeLabel} · ${node.join.partialFailureLabel}${node.join.cancelRemaining ? " · 汇合后取消剩余分支" : ""}`,
      });
    }
    // 安全结果：只有注册表审定的 result_summary 才显示文字；否则只给摘要指纹。
    if (node.resultSummary) {
      sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: `结果摘要：${node.resultSummary}` });
    } else if (node.outputDigestShort) {
      sidebar.createEl("p", { cls: "graph-canvas-sidebar-line", text: `结果摘要指纹：${node.outputDigestShort}` });
    }

    if (node.needsHuman) {
      const participation = sidebar.createDiv({ cls: "graph-canvas-participation" });
      participation.createEl("h4", { text: "需要你的决定" });
      if (!this.canvasConsistent()) {
        participation.createEl("p", { cls: "graph-canvas-sidebar-error", text: "数据已变化，请刷新" });
        const retry = participation.createEl("button", { cls: "graph-canvas-refresh", text: "刷新" });
        retry.setAttribute("type", "button");
        retry.addEventListener("click", () => this.refreshRun());
        return;
      }
      // 绑定材料只来自既有 detail 的 human_gates，绝不来自 canvas。
      const detailModel = model.buildRunDetailModel(this.detail, this.pathData);
      const task = detailModel.humanTasks.find((item) => item.nodeId === node.nodeId);
      if (!task) {
        participation.createEl("p", { cls: "graph-canvas-sidebar-line", text: "该闸门在当前 Checkpoint 上没有可绑定的决定材料，请刷新。" });
        return;
      }
      // Pilot 闸门角色只由 graph_id + allowed_decisions 决定（不依赖技术 ID）；
      // Phase2B 合成 Pilot 与历史 Graph 永远走既有按钮路径。
      const isPilotRun = this.canvasData && this.canvasData.graph_id === "fragment-pilot-v1";
      const isResearchRun = this.canvasData &&
        this.canvasData.graph_id === "fragment-research-escalation-v1";
      const isPreGate = Boolean(isPilotRun && task.allowedDecisions.includes("approve_call"));
      const isPostGate = Boolean(isPilotRun && task.allowedDecisions.includes("accept_draft"));
      const isResearchPreGate = Boolean(
        isResearchRun && task.allowedDecisions.includes("approve_synthesis")
      );
      const isResearchPostGate = Boolean(
        isResearchRun && task.allowedDecisions.includes("accept_result")
      );
      // Pilot 首闸：授权上下文（Authorization Receipt 安全投影，含未签发状态）。
      if (isPreGate) this.renderPilotAuthorization(participation, node);
      // Pilot 次闸：安全结构化结果 + 产出边界说明。
      if (isPostGate && node.result) this.renderPilotResult(participation, node);
      if (isResearchPreGate) this.renderResearchAuthorization(participation, node);
      if (isResearchPostGate && node.researchResult) {
        this.renderResearchResult(participation, node);
      }
      const authExpired = Boolean(
        node.authorization &&
          isExpiredAt(node.authorization.expiresAt)
      );
      const actions = participation.createDiv({ cls: "graph-human-task-actions" });
      for (const decision of task.allowedDecisions) {
        // 首闸「授权并执行」必须已有未过期的授权单；未签发/已过期时不渲染。
        if (decision === "approve_call" && isPreGate && (!node.authorization || authExpired)) {
          continue;
        }
        const researchAuthExpired = Boolean(
          node.researchAuthorization &&
          isExpiredAt(node.researchAuthorization.expiresAt)
        );
        if (decision === "approve_synthesis" && isResearchPreGate &&
          (!node.researchAuthorization || researchAuthExpired)) continue;
        const button = actions.createEl("button", { cls: `graph-decision graph-decision-${decision}` });
        button.setAttribute("type", "button");
        button.setText(pilotDecisionLabel(decision));
        button.addEventListener("click", () => this.decide(task, decision));
      }
    }
  }

  renderPilotAuthorization(container, node) {
    const auth = node.authorization;
    const preview = node.authorizationPreview;
    const block = container.createDiv({ cls: "graph-pilot-auth" });
    block.createEl("h4", { text: "本次授权" });
    const material = auth || preview;
    if (!material) {
      block.createEl("p", {
        cls: "graph-canvas-sidebar-error",
        text: "授权材料不可用，禁止生成授权单；请刷新后重试。",
      });
      return;
    }
    block.createEl("p", { cls: "graph-pilot-auth-fact", text: `模型：${material.model}` });
    block.createEl("p", {
      cls: "graph-pilot-auth-fact",
      text: `调用上限：最多 ${material.maxTotalCalls} 次`,
    });
    block.createEl("p", {
      cls: "graph-pilot-auth-fact",
      text: `总成本上限：¥${material.costCapCny}`,
    });
    block.createEl("p", {
      cls: "graph-pilot-auth-fact",
      text: "产出：Graph 内未验证研究草稿",
    });
    block.createEl("p", {
      cls: "graph-pilot-auth-fact",
      text: `写入范围：${material.writeScope || "Graph 检查点 + Agent 账本；不写笔记、不写资产"}`,
    });
    if (material.inputFields) {
      const fields = block.createDiv({ cls: "graph-pilot-auth-fields" });
      fields.createEl("p", { cls: "graph-pilot-auth-heading", text: "将发送的安全字段" });
      fields.createEl("p", {
        cls: "graph-canvas-sidebar-line",
        text: "原始碎片正文永不发送；以下内容与实际发送材料同源。",
      });
      const list = fields.createEl("ul", { cls: "graph-pilot-auth-list" });
      list.createEl("li", { text: `标题：${material.inputFields.title}` });
      list.createEl("li", { text: `核心判断：${material.inputFields.coreJudgment}` });
      list.createEl("li", { text: `用户价值：${material.inputFields.userValue}` });
      for (const title of material.inputFields.cardTitles) {
        list.createEl("li", { text: `候选卡片：${title}` });
      }
    }
    if (!auth) {
      block.createEl("p", {
        cls: "graph-canvas-sidebar-line",
        text: "尚未生成授权单。下一步只读取一次官方价格并核验预算，不会调用模型。",
      });
      const issue = block.createEl("button", { cls: "graph-pilot-issue", text: "生成授权单" });
      issue.setAttribute("type", "button");
      issue.addEventListener("click", () => this.issueAuthorization());
      return;
    }
    if (!auth.inputFields) {
      block.createEl("p", { cls: "graph-canvas-sidebar-line", text: `输入摘要指纹：${auth.inputDigestShort}` });
    }
    block.createEl("p", { cls: "graph-canvas-sidebar-line", text: `有效期：签发后 24 小时（${auth.expiresAt} 前）` });
    const expired = isExpiredAt(auth.expiresAt);
    if (expired) {
      block.createEl("p", { cls: "graph-canvas-sidebar-error", text: "授权单已过期，需要重新签发。" });
      const renew = block.createEl("button", { cls: "graph-pilot-issue", text: "重新签发授权" });
      renew.setAttribute("type", "button");
      renew.addEventListener("click", () => this.issueAuthorization());
    }
  }

  renderPilotResult(container, node) {
    const result = node.result;
    const block = container.createDiv({ cls: "graph-pilot-result" });
    block.createEl("h4", { text: "结果判断" });
    if (!result || !result.available) {
      block.createEl("p", { cls: "graph-canvas-sidebar-line", text: "结果摘要不可用。" });
    } else {
      block.createEl("p", { cls: "graph-pilot-result-summary", text: `摘要：${result.summary}` });
      if (result.unknowns.length) {
        block.createEl("p", { cls: "graph-canvas-sidebar-line", text: "未解问题：" });
        const list = block.createEl("ul", { cls: "graph-pilot-auth-list" });
        for (const item of result.unknowns) list.createEl("li", { text: item });
      }
      if (result.nextChecks.length) {
        block.createEl("p", { cls: "graph-canvas-sidebar-line", text: "建议检查：" });
        const list = block.createEl("ul", { cls: "graph-pilot-auth-list" });
        for (const item of result.nextChecks) list.createEl("li", { text: item });
      }
    }
    block.createEl("p", {
      cls: "graph-canvas-sidebar-line",
      text: "接受仅收进 Graph 草稿区，不会写入知识资产。",
    });
  }

  renderResearchAuthorization(container, node) {
    const preview = node.researchAuthorizationPreview;
    const auth = node.researchAuthorization;
    const block = container.createDiv({ cls: "graph-pilot-auth graph-research-auth" });
    block.createEl("h4", { text: "研究合成授权" });
    if (!preview) {
      block.createEl("p", { cls: "graph-canvas-sidebar-error", text: "研究授权材料不可用。" });
      return;
    }
    block.createEl("p", { text: `研究目标：${preview.researchGoal}` });
    block.createEl("p", { text: `模型：${preview.model}` });
    block.createEl("p", { text: `调用上限：${preview.maxTotalCalls} 次` });
    block.createEl("p", { text: `成本上限：¥${preview.costCapCny}` });
    block.createEl("p", { text: `写入范围：${preview.writeScope}` });
    block.createEl("p", { text: `纳入来源：${preview.evidenceCount} 条` });
    const list = block.createEl("ul", { cls: "graph-pilot-auth-list" });
    for (const source of preview.sources) list.createEl("li", { text: source.title });
    if (!auth) {
      block.createEl("p", { text: "签发只核验官方价格与绑定材料，不会调用模型。" });
      const issue = block.createEl("button", { cls: "graph-pilot-issue", text: "生成研究授权单" });
      issue.setAttribute("type", "button");
      issue.addEventListener("click", () => this.issueAuthorization(true));
    } else {
      block.createEl("p", { text: `输入摘要：${auth.inputDigestShort}` });
      block.createEl("p", { text: `有效期至：${auth.expiresAt}` });
      if (isExpiredAt(auth.expiresAt)) {
        block.createEl("p", {
          cls: "graph-canvas-sidebar-error",
          text: "研究授权单已过期，需要重新签发。",
        });
        const renew = block.createEl("button", {
          cls: "graph-pilot-issue",
          text: "重新签发研究授权",
        });
        renew.setAttribute("type", "button");
        renew.addEventListener("click", () => this.issueAuthorization(true));
      }
    }
  }

  renderResearchResult(container, node) {
    const result = node.researchResult;
    const block = container.createDiv({ cls: "graph-pilot-result graph-research-result" });
    block.createEl("h4", { text: "研究结果审核" });
    block.createEl("p", { text: `结论摘要：${result.summary}` });
    const confirmed = block.createEl("ul");
    for (const item of result.confirmed) confirmed.createEl("li", { text: item.claim });
    if (result.unknowns.length) {
      block.createEl("p", { text: "仍未知" });
      const unknowns = block.createEl("ul");
      for (const item of result.unknowns) unknowns.createEl("li", { text: item });
    }
    block.createEl("p", { text: `建议行动：${result.recommendation}` });
    block.createEl("p", { text: "接受只确认本次研究结果，不会自动写入知识资产。" });
  }

  // 安全重试可见性（rev3 §3 / rev4 §4）：pilot run 阻断、当前阻断节点确实
  // 是该失败节点、Agent 观察显示发送前失败（request_sent=false）、授权单
  // 存在且未过期。任一不满足 → 无入口；服务端仍是最终权威。
  pilotRetryCandidate(node) {
    if (!this.canvasData || this.canvasData.graph_id !== "fragment-pilot-v1") return null;
    if (this.canvasData.status !== "failed" && this.canvasData.status !== "blocked") return null;
    if (!node || node.status !== "failed") return null;
    if (this.canvasData.current_node !== node.nodeId) return null;
    const detailNodes = (this.detail && this.detail.nodes) || [];
    const detailNode = detailNodes.find((item) => item.node_id === node.nodeId);
    const agent = detailNode && detailNode.agent;
    if (!agent || agent.request_sent !== "false") return null;
    const holder = this.canvasModel
      ? this.canvasModel.nodes.find((item) => item.authorization)
      : null;
    if (!holder || !holder.authorization) return null;
    if (isExpiredAt(holder.authorization.expiresAt)) return null;
    return { agent, authorization: holder.authorization };
  }

  // 安全重试：与刷新/决定共享视图级 single-flight，双击最多一个 POST。
  retryPilot(node, candidate) {
    const runId = this.screen.runId;
    if (!runId || this.disposed) return;
    if (this.pendingOperation) return this.pendingOperation;
    this.setActionButtonsDisabled(true);
    const done = () => this.setActionButtonsDisabled(false);
    return this.runExclusive(async () => {
      const generation = ++this.generation;
      let outcome;
      try {
        outcome = await this.client.retryPilotRun({
          runId,
          nodeId: node.nodeId,
          specDigest: this.canvasData.spec_digest,
          inputDigest: candidate.authorization.agentInputDigest,
          authorizationDigest: candidate.authorization.authorizationDigest,
          expectedSequence: this.detail.run.sequence,
        });
      } catch (err) {
        if (this.disposed || generation !== this.generation) return;
        if (this.sidebarEl) {
          const code = err && err.details && err.details.code;
          this.sidebarEl.createEl("p", {
            cls: "graph-canvas-sidebar-error",
            text:
              (code && PILOT_RETRY_ERROR_LABELS[code]) ||
              (err && err.message ? err.message : "安全重试被拒绝，请刷新后查看运行状态。"),
          });
        }
        return;
      }
      if (this.disposed || generation !== this.generation) return;
      // rev4 §1：retry_failed 不虚报成功——先按权威状态重渲染，再在最新
      // 侧栏追加诚实失败说明。openRun 自身会推进 generation，这里只查
      // disposed，不把自家重渲染误判为过期。
      if (outcome && outcome.status === "retry_failed") {
        await this.openRun(runId);
        if (this.disposed) return;
        if (this.sidebarEl) {
          this.sidebarEl.createEl("p", {
            cls: "graph-canvas-sidebar-error",
            text: `安全重试仍未成功（${outcome.error_code || "未知原因"}）；运行保持阻断，请查看状态。`,
          });
        }
        return;
      }
      await this.openRun(runId);
    }).then(done, done);
  }

  // 「生成授权单 / 重新签发授权」：唯一允许的外网只读动作由服务端在签发时
  // 完成；失败诚实提示，绝不伪造授权。与刷新/决定共享视图级 single-flight。
  issueAuthorization(research = false) {
    const runId = this.screen.runId;
    if (!runId || this.disposed) return;
    if (this.pendingOperation) return this.pendingOperation;
    this.setActionButtonsDisabled(true);
    const done = () => this.setActionButtonsDisabled(false);
    return this.runExclusive(async () => {
      const generation = ++this.generation;
      try {
        if (research) await this.client.issueResearchAuthorizationReceipt(runId);
        else await this.client.issueAuthorizationReceipt(runId);
      } catch (err) {
        if (this.disposed || generation !== this.generation) return;
        if (this.sidebarEl) {
          const message = err && err.details && err.details.code
            ? PILOT_ISSUE_ERROR_LABELS[err.details.code] || null
            : null;
          this.sidebarEl.createEl("p", {
            cls: "graph-canvas-sidebar-error",
            text: message || (err && err.message ? err.message : "授权单签发失败，请稍后重试。"),
          });
        }
        return;
      }
      if (this.disposed || generation !== this.generation) return;
      await this.openRun(runId);
    }).then(done, done);
  }


  openNode(nodeId) {
    if (!this.detail) return;
    this.disposeCanvas();
    this.generation += 1;
    this.screen = { name: "node", runId: this.screen.runId, nodeId };
    const nodeModel = model.buildNodeDetailModel(this.detail, nodeId);
    if (!nodeModel) {
      // G6：节点详情不可用绝不静默退回——写可见说明 + 返回入口。
      this.contentEl.empty();
      const message = this.contentEl.createEl("p", {
        cls: "graph-view-error",
        text: `节点详情不可用：数据不完整（缺少节点 ${nodeId} 的投影）。`,
      });
      const back = this.contentEl.createEl("button", { cls: "graph-view-back" });
      back.setAttribute("type", "button");
      back.setText("← 返回运行详情");
      back.addEventListener("click", () => {
        this.screen = { name: "run", runId: this.screen.runId, nodeId: null };
        this.renderRun();
      });
      return;
    }
    model.renderNodeDetail(this.contentEl, nodeModel, {
      onBack: () => {
        this.screen = { name: "run", runId: this.screen.runId, nodeId: null };
        this.renderRun();
      },
    });
  }

  async onClose() {
    this.disposed = true;
    this.generation += 1; // 使所有在途响应失效
    this.disposeCanvas();
  }

  // 刷新与人工决定共享视图级 single-flight：进行中重复触发直接复用在途
  // Promise，连续点击最多产生一个 POST。
  runExclusive(operation) {
    if (this.pendingOperation) return this.pendingOperation;
    const pending = (async () => {
      try {
        await operation();
      } finally {
        if (this.pendingOperation === pending) this.pendingOperation = null;
      }
    })();
    this.pendingOperation = pending;
    return pending;
  }

  setActionButtonsDisabled(disabled) {
    if (!this.contentEl) return;
    // 注意：FakeEl 的 matches 不支持逗号选择器，两组分开查；侧栏与审计区
    // 的决定按钮都覆盖。恢复必须移除真正的 disabled 属性（rev4）。
    const buttons = [
      ...this.contentEl.querySelectorAll(".graph-canvas-refresh"),
      ...this.contentEl.querySelectorAll(".graph-decision"),
      ...this.contentEl.querySelectorAll(".graph-pilot-issue"),
      ...this.contentEl.querySelectorAll(".graph-pilot-retry"),
    ];
    for (const button of buttons) {
      if (disabled) button.setAttribute("disabled", "disabled");
      else if (typeof button.removeAttribute === "function") button.removeAttribute("disabled");
    }
  }

  refreshRun() {
    if (this.pendingOperation) return this.pendingOperation;
    this.setActionButtonsDisabled(true);
    const done = () => this.setActionButtonsDisabled(false);
    return this.runExclusive(async () => {
      if (this.screen.runId) await this.openRun(this.screen.runId);
    }).then(done, done);
  }

  async decide(task, decision) {
    const runId = this.screen.runId;
    if (!runId || this.disposed) return;
    // R2 §4.6：队列卡同一授权项在途 → 原位入口被拒（共享来源级互斥锁）。
    if (this.sharedAuthLocks && task && task.nodeId && this.sharedAuthLocks.isPending(`graph:${runId}:${task.nodeId}`)) {
      // R2 §4.6：队列卡同一授权项在途 → 原位入口被拒（可见提示；canvas 模式
      // 走侧栏，文本模式走内容区，两处文案一致）。
      if (this.sidebarEl) {
        this.sidebarEl.empty();
        this.sidebarEl.createEl("p", { cls: "graph-canvas-sidebar-error", text: "该授权项正在其他入口处理中，请稍候。" });
      } else if (this.contentEl) {
        this.contentEl.createEl("p", { cls: "graph-view-error", text: "该授权项正在其他入口处理中，请稍候。" });
      }
      return;
    }
    if (this.pendingOperation) {
      // 刷新/决定进行中：明确提示，不得把决定静默当成在途 Promise 的附属。
      if (this.sidebarEl) {
        this.sidebarEl.empty();
        this.sidebarEl.createEl("p", { cls: "graph-canvas-sidebar-line", text: "正在处理上一个操作，请稍候。" });
      }
      return;
    }
    // canvas 与 detail 不一致：禁止提交，零写入。
    if (this.canvasData && !this.canvasConsistent()) {
      if (this.sidebarEl) {
        this.sidebarEl.empty();
        const note = this.sidebarEl.createEl("p", { cls: "graph-canvas-sidebar-error", text: "数据已变化，请刷新" });
        const retry = this.sidebarEl.createEl("button", { cls: "graph-canvas-refresh", text: "刷新" });
        retry.setAttribute("type", "button");
        retry.addEventListener("click", () => this.refreshRun());
      }
      return;
    }
    // 提交期间统一禁用刷新与全部决定按钮（侧栏 + 审计区）；成功/失败都经
    // openRun 权威重渲染恢复，异常中断由 finally 明确恢复（rev4）。
    this.setActionButtonsDisabled(true);
    return this.runExclusive(async () => {
      const generation = ++this.generation;
      try {
        // Pilot 双闸门绑定：authorization_digest 来自首闸授权投影，
        // result_digest 来自次闸结果投影；非 Pilot 节点两者都不存在。
        const canvasNode = this.canvasModel
          ? this.canvasModel.nodeMap.get(task.nodeId)
          : null;
        await this.client.submitHumanDecision({
          runId,
          nodeId: task.nodeId,
          decision,
          specDigest: task.specDigest,
          inputDigest: task.inputDigest,
          expectedSequence: task.gateSequence,
          ...(canvasNode && canvasNode.authorization
            ? { authorizationDigest: canvasNode.authorization.authorizationDigest }
            : {}),
          ...(canvasNode && canvasNode.result && canvasNode.result.resultDigest
            ? { resultDigest: canvasNode.result.resultDigest }
            : {}),
          ...(canvasNode && canvasNode.researchAuthorization
            ? { authorizationDigest: canvasNode.researchAuthorization.authorizationDigest }
            : {}),
          ...(canvasNode && canvasNode.researchResult
            ? { resultDigest: canvasNode.researchResult.resultDigest }
            : {}),
        });
      } catch (err) {
        if (this.disposed || generation !== this.generation) return;
        // G1：失败绝不静默——权威重渲染恢复按钮后追加可见错误行
        // （对齐 retryPilot retry_failed 先例：openRun 之后在侧栏补错误）。
        await this.openRun(runId);
        if (this.disposed) return;
        const code = err && err.details && err.details.code;
        const message =
          (code && GRAPH_DECISION_ERROR_LABELS[code]) ||
          (err && err.message ? err.message : "决定提交失败，请刷新后重试。");
        if (this.sidebarEl) {
          this.sidebarEl.createEl("p", { cls: "graph-canvas-sidebar-error", text: message });
        } else {
          const note = this.contentEl.createEl("p", { cls: "graph-view-error", text: message });
          const back = this.contentEl.createEl("button", { cls: "graph-view-back" });
          back.setAttribute("type", "button");
          back.setText("← 返回列表");
          back.addEventListener("click", () => this.showList());
        }
        return;
      } finally {
        this.setActionButtonsDisabled(false);
      }
      if (this.disposed || generation !== this.generation) return;
      await this.openRun(runId);
    });
  }
}

module.exports = { GraphWorkflowView, GRAPH_WORKFLOW_VIEW_TYPE, GRAPH_DECISION_ERROR_LABELS };
