"use strict";

const model = require("./view-model.js");
const {
  isValidKeepThoughtCategory,
} = require("./cognitive-decision-client.js");

const VIEW_CLASS = "my-life-loop-console-view";

function field(grid, label, value) {
  const item = grid.createDiv({ cls: "lc-field" });
  item.createSpan({ cls: "lc-field-label", text: label });
  item.createSpan({ cls: "lc-field-value", text: value });
}

function renderHealthBadge(header, health) {
  const badge = header.createSpan({ cls: "lc-health" });
  if (!health) {
    badge.addClass("lc-health-unknown");
    badge.setText("状态未知");
    return;
  }
  if (health.stale) {
    badge.addClass("lc-health-stale");
    badge.setText("数据可能不是最新");
  } else if (!health.providerHealthy) {
    badge.addClass("lc-health-stale");
    badge.setText("数据服务读取异常");
  } else {
    badge.addClass("lc-health-ok");
    badge.setText("只读连接正常");
  }
}

function renderHeader(root, state, handlers) {
  const header = root.createDiv({ cls: "lc-header" });
  const titleWrap = header.createDiv({ cls: "lc-title-wrap" });
  titleWrap.createEl("h1", { cls: "lc-title", text: "Loop 控制台" });
  renderHealthBadge(titleWrap, state.status === "ready" ? state.health : null);
  const refresh = header.createEl("button", { cls: "lc-refresh", text: "刷新" });
  refresh.setAttribute("type", "button");
  refresh.setAttribute("aria-label", "刷新 Loop 控制台数据");
  refresh.addEventListener("click", () => handlers.onRefresh());
  return header;
}

function renderStateNotice(body, kind, title, detail) {
  const notice = body.createDiv({ cls: `lc-state lc-state-${kind}` });
  notice.createEl("p", { cls: "lc-state-title", text: title });
  if (detail) notice.createEl("p", { cls: "lc-state-detail", text: detail });
}

function renderAttentionSection(col, state, handlers) {
  const attention = Array.isArray(state.attention) ? state.attention : [];
  const section = col.createDiv({ cls: "lc-section lc-attention" });
  const head = section.createDiv({ cls: "lc-section-head" });
  head.createEl("h2", { text: "需要关注" });
  head.createSpan({ cls: "lc-count", text: `${attention.length} 条` });
  if (!attention.length) {
    section.createEl("p", { cls: "lc-empty", text: "没有等待人工、异常停止或预算风险的运行" });
    return;
  }
  const list = section.createDiv({ cls: "lc-rows" });
  for (const row of attention) {
    const btn = list.createEl("button", { cls: "lc-row" });
    btn.setAttribute("type", "button");
    if (state.selectedRunId === row.runId) btn.addClass("is-selected");
    btn.addEventListener("click", () => handlers.onSelectRun(row.runId));
    const top = btn.createDiv({ cls: "lc-row-top" });
    top.createSpan({ cls: "lc-row-id", text: row.title });
    top.createSpan({ cls: "lc-row-status", text: row.statusLabel });
    const meta = btn.createDiv({ cls: "lc-row-meta" });
    meta.createSpan({ text: `${row.attentionText} · ${row.decisionHint}` });
    meta.createSpan({ text: `当前阶段：${row.currentNodeLabel}` });
    if (row.stopReason) meta.createSpan({ text: `停止原因：${row.stopReason}` });
    const rawBits = [`LoopSpec：${row.loopId}`, `run：${row.runId}`, `碎片：${row.fragmentId}`];
    if (row.subjectSource) rawBits.push(`标题来源：${row.subjectSource}`);
    meta.createSpan({ cls: "lc-row-raw", text: rawBits.join(" · ") });
    meta.createSpan({ text: `更新：${row.updatedAt}` });
  }
}

function renderQueueSection(col, state, handlers) {
  const section = col.createDiv({ cls: "lc-section lc-queue" });
  // Collapsed by default: only the count is visible until nigo opens it.
  const disclosure = section.createEl("details", { cls: "lc-collapsed-section" });
  if (state.ui && state.ui.queueOpen) disclosure.setAttribute("open", "");
  disclosure.addEventListener("toggle", () => handlers.onToggleSection("queue", disclosure.open === true));
  disclosure.createEl("summary", {
    cls: "lc-collapsed-summary",
    text: `待路由队列（${state.queue.length} 条）`,
  });
  if (state.queueStale) disclosure.createEl("p", { cls: "lc-stale-note", text: "队列数据可能不是最新（只读确认失败）" });
  if (!state.queue.length) {
    disclosure.createEl("p", { cls: "lc-empty", text: "待路由队列为空（数据源正常，没有等待路由的 Loop）" });
    return;
  }
  const list = disclosure.createDiv({ cls: "lc-rows" });
  for (const row of state.queue) {
    const btn = list.createEl("button", { cls: "lc-row" });
    btn.setAttribute("type", "button");
    btn.addEventListener("click", () => handlers.onSelectRun(row.runId));
    const top = btn.createDiv({ cls: "lc-row-top" });
    top.createSpan({ cls: "lc-row-id", text: row.title });
    top.createSpan({ cls: "lc-row-status", text: row.statusLabel });
    const meta = btn.createDiv({ cls: "lc-row-meta" });
    meta.createSpan({ text: `状态分组：${row.displayStateLabel}` });
    meta.createSpan({ text: `下一步：${row.nextStep}` });
    meta.createSpan({ cls: "lc-row-raw", text: `LoopSpec：${row.loopId} · 原始状态：${row.status} · run：${row.runId} · 碎片：${row.fragmentId}` });
    meta.createSpan({ text: `更新：${row.updatedAt}` });
  }
}

function renderRunsSection(col, state, handlers) {
  const section = col.createDiv({ cls: "lc-section lc-runs" });
  const allRows = state.runs;
  // Exclude exactly what the primary surface shows (current attention
  // rows). Superseded or resolved attempts are NOT attention anymore and
  // stay available here as collapsed history.
  const normalRows = allRows.filter(
    (row) => !(model.isAttentionRun(row.displayState, row.status) && row.isCurrent)
  );
  const total = typeof state.runsTotal === "number" ? state.runsTotal : allRows.length;
  const hidden = state.filter && state.filter.status ? 0 : allRows.length - normalRows.length;
  // Collapsed by default with the true total; attention records are not
  // repeated here, and a secondary filter never changes 需要关注.
  const disclosure = section.createEl("details", { cls: "lc-collapsed-section" });
  if (state.ui && state.ui.runsOpen) disclosure.setAttribute("open", "");
  disclosure.addEventListener("toggle", () => handlers.onToggleSection("runs", disclosure.open === true));
  disclosure.createEl("summary", {
    cls: "lc-collapsed-summary",
    text: `全部运行（${total} 条）`,
  });
  if (state.runsStale) disclosure.createEl("p", { cls: "lc-stale-note", text: "列表数据可能不是最新（只读确认失败）" });
  if (hidden > 0) {
    disclosure.createEl("p", { cls: "lc-collapsed-note", text: `${hidden} 条需要关注的运行已在上方显示，这里不再重复` });
  }

  const filterWrap = disclosure.createDiv({ cls: "lc-filter" });
  filterWrap.createSpan({ cls: "lc-filter-label", text: "按原始状态筛选" });
  const select = filterWrap.createEl("select", { cls: "lc-filter-select" });
  select.setAttribute("aria-label", "按原始状态筛选运行列表");
  const all = select.createEl("option", { text: "全部状态" });
  all.setAttribute("value", "");
  for (const option of state.statuses) {
    const optText = option.label && option.label !== option.status ? `${option.label}（${option.status}）` : option.status;
    const opt = select.createEl("option", { text: optText });
    opt.setAttribute("value", option.status);
    if (state.filter.status === option.status) opt.setAttribute("selected", "selected");
  }
  if (!state.filter.status) all.setAttribute("selected", "selected");
  select.addEventListener("change", () => handlers.onFilterChange(select.value || null));

  if (!normalRows.length) {
    const filterLabel = state.filter.statusLabel && state.filter.statusLabel !== state.filter.status
      ? `${state.filter.status}（${state.filter.statusLabel}）`
      : state.filter.status;
    disclosure.createEl("p", { cls: "lc-empty", text: state.filter.status ? `没有原始状态为 ${filterLabel} 的运行` : "没有需要额外列出的运行（需要关注的已在上方显示）" });
    return;
  }
  const list = disclosure.createDiv({ cls: "lc-rows" });
  for (const row of normalRows) {
    const btn = list.createEl("button", { cls: "lc-row" });
    btn.setAttribute("type", "button");
    if (state.selectedRunId === row.runId) btn.addClass("is-selected");
    btn.addEventListener("click", () => handlers.onSelectRun(row.runId));
    const top = btn.createDiv({ cls: "lc-row-top" });
    top.createSpan({ cls: "lc-row-id", text: row.runId });
    top.createSpan({ cls: "lc-row-status", text: row.statusLabel });
    const meta = btn.createDiv({ cls: "lc-row-meta" });
    meta.createSpan({ text: `分组：${row.displayStateLabel} · 节点：${row.currentNodeLabel}` });
    meta.createSpan({ cls: "lc-row-raw", text: `原始状态：${row.status}` });
    const tokens = row.budgetUsed && typeof row.budgetUsed.active_tokens === "number" ? `${row.budgetUsed.active_tokens} 令牌（Token）` : "—";
    meta.createSpan({ text: `轮次：${row.iterations === null ? "—" : row.iterations} · 预算：${tokens}` });
    if (row.stopReason) meta.createSpan({ text: `停止原因：${row.stopReason}` });
    meta.createSpan({ text: `更新：${row.updatedAt}` });
  }
}

// ---------------------------------------------------------------------------
// P3B shadow proposal visibility (read-only; no write controls anywhere).
// ---------------------------------------------------------------------------

function proposalStatusClass(status) {
  if (status === "blocked") return "is-blocked";
  if (status === "needs_human_routing") return "is-human";
  if (status === "proposed") return "is-proposed";
  return "is-history";
}

function renderProposalsSection(col, state, handlers) {
  const proposals = state.proposals;
  if (!proposals) return; // legacy states without the P3B sub-state
  const section = col.createDiv({ cls: "lc-section lc-proposals" });
  const head = section.createDiv({ cls: "lc-section-head" });
  head.createEl("h2", { text: "路由建议" });
  if (proposals.status === "ready") {
    head.createSpan({ cls: "lc-count", text: `${proposals.current.length} 条` });
  }

  if (proposals.status === "idle" || proposals.status === "loading") {
    section.createEl("p", { cls: "lc-empty", text: "正在读取影子路由建议…" });
    return;
  }
  if (proposals.status === "unavailable" || proposals.status === "unreachable") {
    // Never a green/healthy display while the shadow service is down.
    const notice = section.createDiv({ cls: "lc-control-unavailable" });
    notice.createEl("p", {
      cls: "lc-control-unavailable-text",
      text: "影子建议服务不可用：上方 Loop 只读数据不受影响，界面不会编造建议。",
    });
    const retry = notice.createEl("button", { cls: "lc-control-retry", text: "重试连接" });
    retry.setAttribute("type", "button");
    retry.addEventListener("click", () => handlers.onRefreshProposals());
    return;
  }
  if (proposals.status === "error") {
    const notice = section.createDiv({ cls: "lc-control-unavailable" });
    notice.createEl("p", {
      cls: "lc-control-unavailable-text",
      text: "影子建议服务返回错误，已停止渲染建议（不会编造数据）。",
    });
    const retry = notice.createEl("button", { cls: "lc-control-retry", text: "重试连接" });
    retry.setAttribute("type", "button");
    retry.addEventListener("click", () => handlers.onRefreshProposals());
    return;
  }

  if (proposals.stale) {
    section.createEl("p", { cls: "lc-stale-note", text: "建议数据可能不是最新（只读确认失败）" });
  }
  if (!proposals.ledgerPresent) {
    section.createEl("p", {
      cls: "lc-empty",
      text: "尚未生成影子建议（影子账本不存在，投影服务如实报告空状态）",
    });
  } else if (!proposals.current.length) {
    section.createEl("p", { cls: "lc-empty", text: "当前没有有效的影子建议" });
  } else {
    const list = section.createDiv({ cls: "lc-rows" });
    for (const card of proposals.current) {
      const btn = list.createEl("button", { cls: "lc-row lc-proposal-card" });
      btn.setAttribute("type", "button");
      btn.setAttribute("aria-label", `查看建议 ${card.proposalId}`);
      if (proposals.selectedProposalId === card.proposalId) {
        btn.addClass("is-selected");
        btn.setAttribute("data-selected", "true");
      }
      btn.addEventListener("click", () => handlers.onSelectProposal(card.proposalId));
      const top = btn.createDiv({ cls: "lc-row-top" });
      top.createSpan({ cls: "lc-row-id", text: card.title });
      top.createSpan({
        cls: `lc-row-status lc-proposal-status ${proposalStatusClass(card.status)}`,
        text: card.statusLabel,
      });
      const meta = btn.createDiv({ cls: "lc-row-meta" });
      meta.createSpan({ text: `建议执行模型：${card.workerModel || "—"}` });
      meta.createSpan({ text: card.readinessLabel });
      if (card.keyReason) meta.createSpan({ text: card.keyReason });
      meta.createSpan({
        cls: "lc-row-raw",
        text: `原始状态：${card.status} · 建议：${card.proposalId} · run：${card.runId}`,
      });
      meta.createSpan({ text: `查看建议 · 更新：${card.updatedAt}` });
    }
  }

  // stale / superseded live in a collapsed history area, never in the
  // default view.
  if (proposals.history.length) {
    const disclosure = section.createEl("details", { cls: "lc-collapsed-section" });
    if (state.ui && state.ui.proposalHistoryOpen) disclosure.setAttribute("open", "");
    disclosure.addEventListener("toggle", () =>
      handlers.onToggleSection("proposalHistory", disclosure.open === true)
    );
    disclosure.createEl("summary", {
      cls: "lc-collapsed-summary",
      text: `历史建议（${proposals.history.length} 条）`,
    });
    const list = disclosure.createDiv({ cls: "lc-rows" });
    for (const row of proposals.history) {
      const item = list.createDiv({ cls: "lc-proposal-history-row" });
      const top = item.createDiv({ cls: "lc-row-top" });
      top.createSpan({ cls: "lc-row-id", text: row.title });
      top.createSpan({
        cls: `lc-row-status lc-proposal-status is-history`,
        text: row.statusLabel,
      });
      const meta = item.createDiv({ cls: "lc-row-meta" });
      if (row.transitionReasonText) {
        meta.createSpan({ text: `${row.transitionReasonText} · ${row.transitionAt}` });
      }
      meta.createSpan({
        cls: "lc-row-raw",
        text: `原始状态：${row.status} · 建议：${row.proposalId} · run：${row.runId}`,
      });
      if (row.supersededByProposalId) {
        const open = meta.createEl("button", {
          cls: "lc-proposal-open-new",
          text: "查看新建议",
        });
        open.setAttribute("type", "button");
        open.setAttribute("aria-label", `查看新建议 ${row.supersededByProposalId}`);
        open.addEventListener("click", () => handlers.onSelectProposal(row.supersededByProposalId));
      }
    }
  }
}

function renderProposalDetail(col, state, handlers) {
  const proposals = state.proposals;
  const section = col.createDiv({ cls: "lc-section lc-detail lc-proposal-detail" });
  const head = section.createDiv({ cls: "lc-section-head" });
  head.createEl("h2", { text: "建议详情" });
  if (proposals.detailLoading) {
    renderStateNotice(section, "loading", "正在读取建议详情…");
    return;
  }
  if (proposals.detailError) {
    renderStateNotice(section, "error", "建议详情读取失败", proposals.detailError);
    return;
  }
  const detail = proposals.detail;
  if (!detail) {
    renderStateNotice(section, "empty", "尚未选择建议", "从路由建议列表选择一条记录查看详情");
    return;
  }
  const back = head.createEl("button", { cls: "lc-back", text: "返回列表" });
  back.setAttribute("type", "button");
  back.addEventListener("click", () => handlers.onBackProposal());
  // Keyboard contract: Escape returns to the list; detail is read-only.
  section.setAttribute("tabindex", "-1");
  section.addEventListener("keydown", (event) => {
    if (event && event.key === "Escape") handlers.onBackProposal();
  });

  const mainGrid = section.createDiv({ cls: "lc-field-grid lc-main-fields" });
  field(mainGrid, "建议状态", `${detail.statusLabel}（原始：${detail.status}）`);
  field(mainGrid, "是否具备执行条件", detail.readinessLabel);
  if (detail.blockedReasons.length) {
    field(mainGrid, "阻塞原因", detail.blockedReasons.join("；"));
  }
  field(mainGrid, "风险等级", `${detail.riskLabel}（${detail.risk}）`);
  field(mainGrid, "外部副作用", `${detail.sideEffectsLabel}（${detail.sideEffects}）`);

  renderReviewSection(section, state, handlers);

  // Everything else collapses into the technical disclosure, mirroring the
  // run detail's 极简 structure.
  const techAll = section.createEl("details", { cls: "lc-tech-all" });
  techAll.createEl("summary", {
    cls: "lc-tech-all-summary",
    text: "技术详情（LoopSpec、Worker、Verifier、预算与原始值）",
  });

  const grid = techAll.createDiv({ cls: "lc-field-grid" });
  field(grid, "LoopSpec（循环执行规格）", detail.loopspec);
  field(grid, "目标", detail.goal);
  field(grid, "Worker（执行模型）", `${detail.workerAgent}（模型：${detail.workerModel}）`);
  field(grid, "Evaluator（独立评估器）", detail.independentEvaluator);
  field(grid, "Verifier（事实核验器）", `${detail.verifier}（核验等级：${detail.verifierLevel}）`);
  field(grid, "隐私等级", detail.privacyText);
  field(grid, "不可信输入", detail.untrustedWebText);
  field(grid, "路由理由", detail.routeReason);
  field(grid, "所需工具", detail.requiredTools.length ? detail.requiredTools.join("、") : "无");
  field(grid, "风险原因", detail.riskReasons.length ? detail.riskReasons.join("、") : "—");
  field(grid, "生成时间", detail.createdAt);
  field(grid, "建议有效期至", detail.expiresAt);

  if (state.proposals.comparison) {
    renderProposalComparison(techAll, state.proposals.comparison, handlers);
  }
  if (state.review && state.review.history && state.review.history.length) {
    renderReviewHistory(techAll, state.review.history);
  }

  const budgetBlock = techAll.createDiv({ cls: "lc-budget" });
  budgetBlock.createEl("h3", { text: "预算限制（已用 / 上限）" });
  for (const row of detail.budget) {
    const line = budgetBlock.createDiv({ cls: "lc-budget-row" });
    line.createSpan({ cls: "lc-budget-label", text: row.label });
    line.createSpan({ cls: "lc-budget-value", text: row.text });
  }

  const stopBlock = techAll.createDiv({ cls: "lc-issues" });
  stopBlock.createEl("h3", { text: "停止条件" });
  const stopList = stopBlock.createEl("ul", { cls: "lc-ref-list" });
  for (const condition of detail.stopConditions) stopList.createEl("li", { text: condition });

  if (detail.events.length) {
    const eventBlock = techAll.createDiv({ cls: "lc-timeline" });
    eventBlock.createEl("h3", { text: "建议状态时间线（append-only）" });
    const list = eventBlock.createDiv({ cls: "lc-timeline-list" });
    for (const event of detail.events) {
      const item = list.createDiv({ cls: "lc-timeline-item" });
      const main = item.createDiv({ cls: "lc-timeline-main" });
      const headLine = main.createDiv({ cls: "lc-timeline-head" });
      headLine.createSpan({ cls: "lc-timeline-type", text: event.toStatusLabel });
      headLine.createSpan({ cls: "lc-timeline-time", text: event.createdAt });
      if (event.reasonText) {
        main.createEl("p", { cls: "lc-timeline-summary", text: event.reasonText });
      }
      main.createEl("p", { cls: "lc-timeline-raw", text: `原始值：${event.toStatus}` });
    }
  }

  const tech = techAll.createEl("details", { cls: "lc-tech" });
  tech.createEl("summary", { cls: "lc-tech-summary", text: "原始值（用于审计）" });
  const lines = [
    `建议编号：${detail.proposalId}`,
    `run：${detail.runId} · 碎片：${detail.fragmentId} · episode：${detail.attemptEpisode}`,
    `原始状态：${detail.status} · 运行原始状态：${detail.loopStatus}`,
    `来源序号：${detail.sourceSequence}`,
    `dispatcher 版本：${detail.dispatcherVersion}`,
    `指纹：${detail.fingerprint}`,
    `被替代关系：supersedes ${detail.supersedesProposalId || "无"}`,
  ];
  if (detail.blockedReasonsRaw.length) {
    lines.push(`原始阻塞原因：${detail.blockedReasonsRaw.join(" ; ")}`);
  }
  for (const line of lines) tech.createEl("p", { cls: "lc-tech-line", text: line });
}

// ---------------------------------------------------------------------------
// P3C shadow proposal review UI (bookkeeping only — never execution).
// ---------------------------------------------------------------------------

function renderReviewReceiptNotice(block, receipt, duplicate) {
  const notice = block.createDiv({ cls: "lc-receipt" });
  notice.createEl("p", {
    cls: "lc-receipt-title",
    text: duplicate
      ? "重复提交：审核服务返回了原始回执，没有产生新的决定"
      : "审核回执（不可变，状态以审核服务为准）",
  });
  notice.createEl("p", { text: `决定：${receipt.decisionLabel}` });
  notice.createEl("p", { text: `决定编号：${receipt.decisionId}（不可变）` });
  if (receipt.reason) notice.createEl("p", { text: `原因：${receipt.reason}` });
  if (receipt.resumeCondition) notice.createEl("p", { text: `恢复条件：${receipt.resumeCondition}` });
  if (receipt.note) notice.createEl("p", { text: `备注：${receipt.note}` });
}

function renderReviewSection(section, state, handlers) {
  const review = state.review;
  if (!review) return;
  const block = section.createDiv({ cls: "lc-review" });
  block.createEl("h3", { text: "审核决定" });

  // The immutable receipt and any submit error render independently of the
  // review service: an outage must never hide proof of a recorded decision.
  if (review.lastReceipt) {
    renderReviewReceiptNotice(block, review.lastReceipt, review.duplicate);
  }
  if (review.submitError) {
    const errorLine = block.createDiv({ cls: "lc-control-error" });
    errorLine.createEl("p", { text: `决定未提交：${review.submitError.text}` });
    if (review.submitError.code) {
      errorLine.createEl("p", { cls: "lc-control-raw", text: `原始错误码：${review.submitError.code}` });
    }
  }

  if (review.status === "idle" || review.status === "loading") {
    block.createEl("p", { cls: "lc-empty", text: "正在读取审核状态…" });
    return;
  }
  if (review.status !== "ready") {
    const notice = block.createDiv({ cls: "lc-control-unavailable" });
    notice.createEl("p", {
      cls: "lc-control-unavailable-text",
      text: "影子建议审核服务不可用：上方只读建议不受影响，已确认回执保留。",
    });
    const retry = notice.createEl("button", { cls: "lc-control-retry", text: "重试连接" });
    retry.setAttribute("type", "button");
    retry.addEventListener("click", () => handlers.onRefreshReview());
    return;
  }

  const actions = review.actions;
  if (!actions) return;
  const stateLine = block.createDiv({ cls: "lc-review-state" });
  const current = actions.currentDecision;
  if (!current) {
    stateLine.createSpan({ text: "当前尚未审核" });
  } else {
    const bits = [`当前决定：${current.decisionLabel}`];
    if (current.reason) bits.push(`原因：${current.reason}`);
    if (current.resumeCondition) bits.push(`恢复条件：${current.resumeCondition}`);
    if (current.note) bits.push(`备注：${current.note}`);
    bits.push(current.decidedAt);
    stateLine.createSpan({ text: bits.join(" · ") });
  }
  if (!actions.submittable) {
    const reason = block.createEl("p", {
      cls: "lc-control-reason",
      text: `不可审核：${actions.reasonText || "建议已经变化，请重新查看"}`,
    });
    reason.setAttribute("aria-live", "polite");
    return;
  }
  const open = block.createEl("button", { cls: "lc-control-btn lc-review-open", text: "审核建议" });
  open.setAttribute("type", "button");
  open.setAttribute("aria-label", `审核建议 ${actions.proposalId}`);
  open.addEventListener("click", () => handlers.onOpenReviewDialog());
}

function renderReviewDialog(root, state, handlers) {
  const review = state.review;
  const dialog = review.dialog;
  const overlay = root.createDiv({ cls: "lc-dialog-overlay lc-review-dialog" });
  overlay.setAttribute("tabindex", "-1");
  const box = overlay.createDiv({ cls: "lc-dialog" });
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-label", "审核建议");
  box.createEl("h3", { cls: "lc-dialog-title", text: "审核建议" });

  if (dialog.warning) {
    const warn = box.createEl("p", { cls: "lc-dialog-warning", text: dialog.warning });
    warn.setAttribute("role", "alert");
  }

  let reasonInput = null;
  let resumeInput = null;
  let noteInput = null;
  const focusables = [];

  if (dialog.step === "choose") {
    box.createEl("p", { cls: "lc-dialog-effect", text: "请选择审核决定（只会记录决定，不会启动任何执行）" });
    const buttons = box.createDiv({ cls: "lc-dialog-buttons" });
    const choices = [
      ["accepted", "接受方案"],
      ["deferred", "暂缓处理"],
      ["rejected", "驳回方案"],
    ];
    for (const [decision, label] of choices) {
      const btn = buttons.createEl("button", { cls: "lc-dialog-confirm", text: label });
      btn.setAttribute("type", "button");
      btn.addEventListener("click", () => handlers.onReviewChoose(decision));
      focusables.push(btn);
    }
    const cancel = buttons.createEl("button", { cls: "lc-dialog-cancel", text: "取消" });
    cancel.setAttribute("type", "button");
    cancel.addEventListener("click", () => handlers.onReviewDialogCancel());
    focusables.push(cancel);
  } else {
    const decisionLabel = model.reviewDecisionLabel(dialog.decision);
    box.createEl("p", { cls: "lc-dialog-effect", text: `决定：${decisionLabel}（确认后生成不可变回执）` });

    if (dialog.decision === "rejected") {
      const wrap = box.createDiv({ cls: "lc-dialog-field" });
      wrap.createEl("label", { text: "驳回原因（必填）" });
      reasonInput = wrap.createEl("textarea", { cls: "lc-dialog-input lc-review-input" });
      reasonInput.setAttribute("rows", "3");
      reasonInput.setAttribute("aria-label", "驳回原因");
      focusables.push(reasonInput);
    }
    if (dialog.decision === "deferred") {
      const reasonWrap = box.createDiv({ cls: "lc-dialog-field" });
      reasonWrap.createEl("label", { text: "暂缓原因（原因或恢复条件至少填写一项）" });
      reasonInput = reasonWrap.createEl("textarea", { cls: "lc-dialog-input lc-review-input" });
      reasonInput.setAttribute("rows", "2");
      reasonInput.setAttribute("aria-label", "暂缓原因");
      focusables.push(reasonInput);
      const resumeWrap = box.createDiv({ cls: "lc-dialog-field" });
      resumeWrap.createEl("label", { text: "恢复条件" });
      resumeInput = resumeWrap.createEl("textarea", { cls: "lc-dialog-input lc-review-input" });
      resumeInput.setAttribute("rows", "2");
      resumeInput.setAttribute("aria-label", "恢复条件");
      focusables.push(resumeInput);
    }
    if (dialog.decision === "accepted") {
      const wrap = box.createDiv({ cls: "lc-dialog-field" });
      wrap.createEl("label", { text: "备注（可选）" });
      noteInput = wrap.createEl("textarea", { cls: "lc-dialog-input lc-review-input" });
      noteInput.setAttribute("rows", "2");
      noteInput.setAttribute("aria-label", "备注");
      focusables.push(noteInput);
    }

    const errorLine = box.createDiv({ cls: "lc-review-dialog-error" });
    errorLine.setAttribute("aria-live", "polite");

    const buttons = box.createDiv({ cls: "lc-dialog-buttons" });
    const confirm = buttons.createEl("button", { cls: "lc-dialog-confirm", text: "确认提交" });
    confirm.setAttribute("type", "button");
    confirm.addEventListener("click", () => {
      const fields = {
        reason: reasonInput ? reasonInput.value.trim() : "",
        resumeCondition: resumeInput ? resumeInput.value.trim() : "",
        note: noteInput ? noteInput.value.trim() : "",
      };
      if (dialog.decision === "rejected" && !fields.reason) {
        errorLine.setText("驳回必须填写原因");
        return;
      }
      if (dialog.decision === "deferred" && !fields.reason && !fields.resumeCondition) {
        errorLine.setText("暂缓必须填写原因或恢复条件");
        return;
      }
      handlers.onReviewConfirm(fields);
    });
    focusables.push(confirm);
    const cancel = buttons.createEl("button", { cls: "lc-dialog-cancel", text: "取消" });
    cancel.setAttribute("type", "button");
    cancel.addEventListener("click", () => handlers.onReviewDialogCancel());
    focusables.push(cancel);
    if (review.submitting) {
      confirm.setAttribute("disabled", "disabled");
      cancel.setAttribute("disabled", "disabled");
      box.createEl("p", { cls: "lc-empty", text: "正在提交审核决定…" });
    }
  }

  // Keyboard contract: Escape cancels; Tab and Shift+Tab stay trapped
  // inside the dialog; the background is inert while the dialog is open.
  let virtualActive = null;
  overlay.addEventListener("keydown", (event) => {
    if (!event || !event.key) return;
    if (event.key === "Escape") {
      if (event.preventDefault) event.preventDefault();
      handlers.onReviewDialogCancel();
      return;
    }
    if (event.key === "Tab") {
      const usable = focusables.filter(Boolean);
      if (!usable.length) return;
      const active =
        typeof document !== "undefined" && document && document.activeElement
          ? document.activeElement
          : virtualActive;
      const index = usable.indexOf(active);
      if (event.preventDefault) event.preventDefault();
      const next = event.shiftKey
        ? usable[(index <= 0 ? usable.length : index) - 1]
        : usable[(index + 1) % usable.length];
      virtualActive = next;
      if (next && typeof next.focus === "function") next.focus();
    }
  });
}

function renderReviewHistory(techAll, history) {
  const block = techAll.createEl("details", { cls: "lc-intents" });
  block.createEl("summary", {
    cls: "lc-intents-summary",
    text: `审核历史（最近 ${Math.min(5, history.length)} 条）`,
  });
  const list = block.createDiv({ cls: "lc-intent-list" });
  for (const receipt of history.slice(0, 5)) {
    const row = list.createDiv({ cls: "lc-intent-row" });
    row.createSpan({ cls: "lc-intent-action", text: receipt.decisionLabel });
    row.createSpan({ cls: "lc-intent-time", text: receipt.decidedAt });
    if (receipt.reason) row.createSpan({ cls: "lc-intent-reason", text: `原因：${receipt.reason}` });
    if (receipt.resumeCondition) row.createSpan({ cls: "lc-intent-reason", text: `恢复条件：${receipt.resumeCondition}` });
    const tech = row.createEl("details", { cls: "lc-tech" });
    tech.createEl("summary", { cls: "lc-tech-summary", text: "原始值" });
    tech.createEl("p", { cls: "lc-tech-line", text: `decision_id：${receipt.decisionId}` });
    tech.createEl("p", { cls: "lc-tech-line", text: `idempotency_key：${receipt.idempotencyKey}` });
    tech.createEl("p", {
      cls: "lc-tech-line",
      text: `decision：${receipt.decision} · source_sequence：${receipt.sourceSequence} · supersedes：${receipt.supersedesDecisionId || "无"}`,
    });
  }
}

function renderProposalComparison(techAll, comparison, handlers) {
  const block = techAll.createDiv({ cls: "lc-compare" });
  block.createEl("h3", { text: "与上一版比较" });
  // G3：加载失败显式标注（与「无字段差异」区分）——给原因 + 重试。
  if (comparison.failed) {
    block.createEl("p", { cls: "lc-state-detail", text: "对比加载失败：上一版建议暂不可读。" });
    if (handlers && handlers.onRefreshProposals) {
      const retry = block.createEl("button", { cls: "lc-compare-retry", text: "重试加载对比" });
      retry.setAttribute("type", "button");
      retry.addEventListener("click", () => handlers.onRefreshProposals());
    }
    return;
  }
  if (!comparison.rows.length) {
    block.createEl("p", { cls: "lc-empty", text: "与上一版没有字段差异" });
    return;
  }
  const grid = block.createDiv({ cls: "lc-field-grid" });
  for (const row of comparison.rows) {
    field(grid, row.label, `${row.before} → ${row.after}`);
  }
  block.createEl("p", { cls: "lc-tech-line", text: `上一版建议：${comparison.previousId}` });
}

function renderRefList(parent, label, refs) {
  const block = parent.createDiv({ cls: "lc-refs" });
  block.createEl("h3", { text: label });
  if (!refs.length) {
    block.createEl("p", { cls: "lc-empty", text: "无" });
    return;
  }
  const list = block.createEl("ul", { cls: "lc-ref-list" });
  for (const ref of refs) list.createEl("li", { text: ref });
}

function renderDetail(col, state, handlers) {
  const section = col.createDiv({ cls: "lc-section lc-detail" });
  const head = section.createDiv({ cls: "lc-section-head" });
  head.createEl("h2", { text: "运行详情" });
  if (state.detailLoading) {
    renderStateNotice(section, "loading", "正在读取运行详情…");
    return;
  }
  if (state.detailError) {
    renderStateNotice(section, "error", "运行详情读取失败", state.detailError);
    return;
  }
  const detail = state.detail;
  if (!detail) {
    renderStateNotice(section, "empty", "尚未选择运行", "从待路由队列或运行列表中选择一条记录查看详情");
    return;
  }
  const back = head.createEl("button", { cls: "lc-back", text: "返回列表" });
  back.setAttribute("type", "button");
  back.addEventListener("click", () => handlers.onBack());

  if (state.detailStale) section.createEl("p", { cls: "lc-stale-note", text: "详情数据可能不是最新（只读确认失败）" });

  // 极简主视图: only 状态 / 当前阶段 / 最近结果 / 停止原因与风险.
  const mainGrid = section.createDiv({ cls: "lc-field-grid lc-main-fields" });
  field(mainGrid, "状态", `${detail.statusLabel}（分组：${detail.displayStateLabel}）`);
  field(mainGrid, "当前阶段", detail.currentNodeLabel);
  const evalText = detail.eval.verdict === "—"
    ? "—"
    : `${detail.eval.verdictLabel}（硬门槛 ${detail.eval.gates}）`;
  field(mainGrid, "最近结果", evalText);
  field(mainGrid, "停止原因", detail.stopReasonText);
  if (detail.budgetRisk.length) {
    field(mainGrid, "预算风险", detail.budgetRisk.join("；"));
  }

  renderCognitiveDraft(
    section,
    detail.cognitiveDraft,
    state.cognitiveDecision,
    handlers
  );
  renderMinimumValueDecision(section, detail, state.minimumValue, handlers);
  renderControlsSection(section, state, handlers);

  // Everything else — Worker、Evaluator、sequence、完整时间线、原始值 —
  // collapses into the technical disclosure.
  const techAll = section.createEl("details", { cls: "lc-tech-all" });
  techAll.createEl("summary", { cls: "lc-tech-all-summary", text: "技术详情（完整字段、时间线与原始值）" });

  const grid = techAll.createDiv({ cls: "lc-field-grid" });
  field(grid, "运行 ID", detail.runId);
  field(grid, "状态", detail.statusLabel);
  field(grid, "状态分组", detail.displayStateLabel);
  field(grid, "当前节点", detail.currentNodeLabel);
  field(grid, "Loop", `${detail.loopId} · LoopSpec v${detail.loopspecVersion}`);
  field(grid, "碎片", detail.fragmentId);
  field(grid, "父运行", detail.parentRunId);
  field(grid, "执行模型（Worker）", detail.workerVersion);
  field(grid, "独立评估器（Evaluator）", detail.evaluatorVersion);
  field(grid, "轮次", detail.iterations === null ? "—" : String(detail.iterations));
  field(grid, "停止原因", detail.stopReasonText);
  field(grid, "恢复条件", detail.resumeCondition);
  field(grid, "更新时间", detail.updatedAt);

  if (detail.technical) {
    const tech = techAll.createEl("details", { cls: "lc-tech" });
    tech.createEl("summary", { cls: "lc-tech-summary", text: "原始值（用于审计与筛选）" });
    const lines = [
      `原始状态：${detail.technical.status}`,
      `原始分组：${detail.technical.displayState}`,
      `原始当前节点：${detail.technical.currentNode}`,
      `原始 Eval 结论：${detail.technical.verdict}`,
    ];
    if (detail.technical.stopReason) lines.push(`原始停止原因：${detail.technical.stopReason}`);
    for (const line of lines) tech.createEl("p", { cls: "lc-tech-line", text: line });
  }

  const goalBlock = techAll.createDiv({ cls: "lc-goal" });
  goalBlock.createEl("h3", { text: "目标" });
  goalBlock.createEl("p", { text: detail.goal });

  const budgetBlock = techAll.createDiv({ cls: "lc-budget" });
  budgetBlock.createEl("h3", { text: "预算（已用 / 上限）" });
  for (const row of detail.budget) {
    const line = budgetBlock.createDiv({ cls: "lc-budget-row" });
    line.createSpan({ cls: "lc-budget-label", text: row.label });
    line.createSpan({ cls: "lc-budget-value", text: row.text });
    const bar = line.createDiv({ cls: "lc-budget-bar" });
    const fill = bar.createDiv({ cls: "lc-budget-fill" });
    const pct = row.ratio === null ? 0 : Math.min(100, Math.round(row.ratio * 100));
    fill.setAttribute("style", `width: ${pct}%`);
    if (row.ratio !== null && row.ratio >= 0.9) fill.addClass("is-near-limit");
  }

  const evalBlock = techAll.createDiv({ cls: "lc-eval" });
  evalBlock.createEl("h3", { text: "独立评估结果（Eval）" });
  const evalLine = evalBlock.createDiv({ cls: "lc-eval-line" });
  evalLine.createSpan({ text: `结论：${detail.eval.verdictLabel}` });
  evalLine.createSpan({ text: `硬门槛：${detail.eval.gates}` });

  const issuesBlock = techAll.createDiv({ cls: "lc-issues" });
  issuesBlock.createEl("h3", { text: "未解决问题" });
  if (!detail.unresolvedIssues.length) {
    issuesBlock.createEl("p", { cls: "lc-empty", text: "无" });
  } else {
    const ul = issuesBlock.createEl("ul", { cls: "lc-ref-list" });
    for (const issue of detail.unresolvedIssues) ul.createEl("li", { text: issue });
  }

  const timelineBlock = techAll.createDiv({ cls: "lc-timeline" });
  timelineBlock.createEl("h3", { text: "节点时间线（按提交顺序）" });
  if (!detail.timeline.length) {
    timelineBlock.createEl("p", { cls: "lc-empty", text: "暂无时间线事件" });
  } else {
    const list = timelineBlock.createDiv({ cls: "lc-timeline-list" });
    for (const event of detail.timeline) {
      const item = list.createDiv({ cls: "lc-timeline-item" });
      item.createSpan({ cls: "lc-timeline-seq", text: event.sequence === null ? "—" : `#${event.sequence}` });
      const main = item.createDiv({ cls: "lc-timeline-main" });
      const headLine = main.createDiv({ cls: "lc-timeline-head" });
      headLine.createSpan({ cls: "lc-timeline-type", text: event.eventTypeLabel });
      headLine.createSpan({ cls: "lc-timeline-node", text: event.nodeLabel });
      headLine.createSpan({ cls: "lc-timeline-time", text: event.createdAt });
      const rawBits = [];
      if (event.eventType && event.eventType !== event.eventTypeLabel) rawBits.push(event.eventType);
      if (event.node && event.node !== "—" && event.node !== event.nodeLabel) rawBits.push(event.node);
      if (rawBits.length) main.createEl("p", { cls: "lc-timeline-raw", text: `原始值：${rawBits.join(" · ")}` });
      main.createEl("p", { cls: "lc-timeline-summary", text: event.summary });
    }
  }

  renderRefList(techAll, "证据引用", detail.evidenceRefs);
  renderRefList(techAll, "产物引用", detail.artifactRefs);
  renderRefList(techAll, "资产引用", detail.assetRefs);
}

const COGNITIVE_VERDICT_LABELS = {
  supported: "支持",
  partially_supported: "部分支持",
  contradicted: "反对",
  not_covered: "未覆盖",
};
const COGNITIVE_CREDIBILITY_LABELS = {
  high: "高",
  medium: "中",
  low: "低",
  insufficient: "不足",
};
const COGNITIVE_PERSPECTIVE_LABELS = {
  memory: "现有记忆视角",
  knowledge_base: "Obsidian 知识库与项目进展视角",
  frontier: "前沿行业／技术／科学视角",
};
const COGNITIVE_RELEVANCE_LABELS = { direct: "直接相关", indirect: "间接相关" };
const COGNITIVE_INDEPENDENCE_LABELS = {
  independent: "独立来源",
  non_independent: "非独立来源",
};
// Package C: the three material categories stay explicitly separated, each
// with its honest boundary; an inference is always marked as an inference.
const COGNITIVE_MATERIAL_GROUP_LABELS = {
  confirmed_user_fact: "已确认用户事实（仅表示用户确认，不代表外部真实性）",
  obsidian_record: "可追溯知识记录（本包为合成记录，未访问真实知识库）",
  profile_inference: "用户画像推断（推断，不是用户事实）",
};

function renderCognitivePerspectiveMaterials(card, materials) {
  if (!materials.length) return;
  for (const type of [
    "confirmed_user_fact",
    "obsidian_record",
    "profile_inference",
  ]) {
    const items = materials.filter((material) => material.materialType === type);
    if (!items.length) continue;
    const group = card.createDiv({ cls: "lc-cognitive-material-group" });
    group.createEl("h6", { text: COGNITIVE_MATERIAL_GROUP_LABELS[type] });
    const ul = group.createEl("ul", { cls: "lc-cognitive-list" });
    for (const item of items) {
      let text = item.text;
      if (type === "confirmed_user_fact") {
        text += `（确认引用：${item.confirmationRef}）`;
      } else if (type === "obsidian_record") {
        text += `（记录引用：${item.recordRef}）`;
      } else {
        text += `（依据：${item.basis}；不确定性：${item.uncertainty}）`;
      }
      const li = ul.createEl("li", { text });
      if (type === "profile_inference") li.addClass("lc-cognitive-inference-item");
    }
  }
}

function labelledValue(labels, raw) {
  const label = labels[raw];
  return label ? `${label}（${raw}）` : String(raw);
}

function renderCognitiveList(parent, items, emptyText) {
  if (!items.length) {
    parent.createEl("p", { cls: "lc-empty", text: emptyText });
    return;
  }
  const ul = parent.createEl("ul", { cls: "lc-cognitive-list" });
  for (const item of items) ul.createEl("li", { text: item });
}

// Package B structured sections. Every value comes from the backend's
// sanitized cognitive_draft_view projection; retrieval discovery (搜索摘要)
// and page evidence (页面逐字摘录) are rendered as two explicitly separated
// blocks, and nothing here upgrades evidence beyond 未验证.
function renderCognitiveDraftView(block, view) {
  const fragment = block.createDiv({ cls: "lc-cognitive-section" });
  fragment.createEl("h4", { text: "原始碎片" });
  fragment.createEl("blockquote", {
    cls: "lc-cognitive-fragment",
    text: view.fragmentText,
  });

  const route = block.createDiv({ cls: "lc-cognitive-section" });
  route.createEl("h4", { text: "路线判断" });
  route.createEl("p", {
    text: `路线：${view.route === "research" ? "研究路线" : "直接路线"}（${view.route}）`,
  });
  route.createEl("p", { cls: "lc-cognitive-note", text: `路线理由：${view.routeReason}` });

  const summary = block.createDiv({ cls: "lc-cognitive-section" });
  summary.createEl("h4", { text: "原文摘要" });
  summary.createEl("p", { text: view.summary.text });
  summary.createEl("p", {
    cls: "lc-cognitive-note",
    text: `逐字引用：${view.summary.source_quote}；转换依据：${view.summary.transformation_basis}`,
  });

  const expansion = block.createDiv({ cls: "lc-cognitive-section" });
  expansion.createEl("h4", { text: "语义展开" });
  expansion.createEl("h5", { text: "字面信息" });
  renderCognitiveList(
    expansion,
    view.semanticExpansion.literal_facts.map((fact) => fact.text),
    "无"
  );
  expansion.createEl("h5", { text: "高可信结构推断" });
  renderCognitiveList(
    expansion,
    view.semanticExpansion.inferences.map(
      (inference) =>
        `${inference.text}（原文：${inference.source_quote}；变化依据：${inference.transformation_basis}）`
    ),
    "（无）"
  );
  expansion.createEl("h5", { text: "真正未知项" });
  renderCognitiveList(expansion, view.semanticExpansion.uncertainties, "未发现实质不确定项");

  if (view.research) renderCognitiveResearch(block, view.research);

  const perspectives = block.createDiv({ cls: "lc-cognitive-section" });
  perspectives.createEl("h4", { text: "三个视角" });
  for (const perspective of view.perspectives) {
    const card = perspectives.createDiv({ cls: "lc-cognitive-perspective" });
    card.createEl("h5", {
      text: labelledValue(COGNITIVE_PERSPECTIVE_LABELS, perspective.perspective),
    });
    card.createEl("p", { text: perspective.summary });
    card.createEl("p", {
      cls: "lc-cognitive-note",
      text: `关联：${perspective.association}；价值：${perspective.value}；不确定：${perspective.uncertainty}`,
    });
    renderCognitivePerspectiveMaterials(card, perspective.materials);
    if (perspective.conflicts.length) {
      renderCognitiveList(card, perspective.conflicts, "");
    }
  }

  const synthesis = block.createDiv({ cls: "lc-cognitive-section" });
  synthesis.createEl("h4", { text: "综合判断（仍为未验证草稿）" });
  synthesis.createEl("p", { text: `价值：${view.synthesis.value}` });
  synthesis.createEl("p", {
    cls: "lc-cognitive-note",
    text: `最薄弱环节：${view.synthesis.weakest_link}`,
  });
  if (view.synthesis.credibility) {
    synthesis.createEl("p", {
      cls: "lc-cognitive-note",
      text: `可信度：${labelledValue(COGNITIVE_CREDIBILITY_LABELS, view.synthesis.credibility)}；依据：${view.synthesis.credibility_basis}`,
    });
  }
  synthesis.createEl("h5", { text: "冲突" });
  renderCognitiveList(synthesis, view.synthesis.conflicts, "未发现冲突");
  synthesis.createEl("h5", { text: "仍然未知" });
  renderCognitiveList(synthesis, view.synthesis.open_questions, "无");
  synthesis.createEl("p", {
    cls: "lc-cognitive-next-step",
    text: `下一步：${view.synthesis.next_step}`,
  });
}

function renderCognitiveResearch(block, research) {
  const questions = block.createDiv({ cls: "lc-cognitive-section" });
  questions.createEl("h4", { text: "研究问题与检索维度" });
  questions.createEl("h5", { text: "研究问题" });
  renderCognitiveList(questions, research.questions, "无");
  questions.createEl("h5", { text: "检索维度" });
  renderCognitiveList(questions, research.search_dimensions, "无");

  const sources = block.createDiv({ cls: "lc-cognitive-section" });
  sources.createEl("h4", { text: "来源清单（真实性均未验证）" });
  if (!research.sources.length) {
    sources.createEl("p", { cls: "lc-empty", text: "没有检索到来源" });
  }
  for (const source of research.sources) {
    const card = sources.createDiv({ cls: "lc-cognitive-source" });
    card.createEl("h5", { text: `${source.source_id}：${source.name}` });
    card.createEl("p", {
      cls: "lc-cognitive-note",
      text:
        `真实性：${source.authenticity}（未验证）；` +
        `相关性：${labelledValue(COGNITIVE_RELEVANCE_LABELS, source.relevance)}；` +
        `独立性：${labelledValue(COGNITIVE_INDEPENDENCE_LABELS, source.independence)}`,
    });
    card.createEl("p", {
      cls: "lc-cognitive-note",
      text:
        `层级：${source.source_tier}；类型：${source.source_type}；` +
        `发布：${source.published_at}；获取：${source.acquisition_method}`,
    });
    card.createEl("p", { cls: "lc-cognitive-locator", text: `位置：${source.locator}` });
    const discovery = card.createDiv({ cls: "lc-cognitive-discovery" });
    discovery.createEl("h6", { text: "检索发现（搜索摘要，不是页面证据）" });
    if (source.discovery) {
      discovery.createEl("p", { text: source.discovery.excerpt });
      discovery.createEl("p", {
        cls: "lc-cognitive-note",
        text:
          `标题：${source.discovery.title}；` +
          `发布时间声称：${source.discovery.published_at_claim}（未核验）`,
      });
    } else {
      discovery.createEl("p", { cls: "lc-empty", text: "无检索发现记录" });
    }
    const evidence = card.createDiv({ cls: "lc-cognitive-evidence" });
    evidence.createEl("h6", { text: "页面证据（获取页面逐字摘录）" });
    evidence.createEl("blockquote", {
      cls: "lc-cognitive-excerpt",
      text: source.evidence_excerpt,
    });
    evidence.createEl("p", {
      cls: "lc-cognitive-note",
      text: `核验依据：${source.verification_basis}`,
    });
    evidence.createEl("p", {
      cls: "lc-cognitive-note",
      text: `证据边界：${source.scope}`,
    });
  }

  const counter = block.createDiv({ cls: "lc-cognitive-section" });
  counter.createEl("h4", { text: "相反观点检索" });
  counter.createEl("p", {
    text: `状态：${research.counter_evidence_search.status === "found" ? "发现相反观点" : "未发现"}（${research.counter_evidence_search.status}）`,
  });
  counter.createEl("p", {
    cls: "lc-cognitive-note",
    text: `范围：${research.counter_evidence_search.scope}`,
  });
  if (research.counter_evidence_search.findings.length) {
    renderCognitiveList(
      counter,
      research.counter_evidence_search.findings.map(
        (finding) => `${finding.text}（引用：${finding.evidence_refs.join("、")}）`
      ),
      ""
    );
  }

  const claims = block.createDiv({ cls: "lc-cognitive-section" });
  claims.createEl("h4", { text: "V1 主张与判定" });
  for (const claim of research.v1_claims) {
    const card = claims.createDiv({ cls: "lc-cognitive-claim" });
    card.createEl("h5", { text: `${claim.claim_id}：${claim.text}` });
    card.createEl("p", {
      cls: "lc-cognitive-verdict",
      text: `判定：${labelledValue(COGNITIVE_VERDICT_LABELS, claim.verdict)}；置信：${claim.confidence}`,
    });
    card.createEl("p", {
      cls: "lc-cognitive-note",
      text: `推导：${claim.claim_derivation}；判定依据：${claim.verdict_basis}`,
    });
    card.createEl("p", {
      cls: "lc-cognitive-note",
      text:
        `独立验证：${claim.independent_verification.status}（${claim.independent_verification.mode}）；` +
        claim.independent_verification.basis,
    });
  }

  const revisions = block.createDiv({ cls: "lc-cognitive-section" });
  revisions.createEl("h4", { text: "V2 逐项复核（回到原文校准）" });
  for (const revision of research.v2_revisions) {
    const card = revisions.createDiv({ cls: "lc-cognitive-claim" });
    card.createEl("h5", {
      text: `${revision.claim_id}：${revision.revision_status === "revised" ? "已修改" : "未修改"}（${revision.revision_status}）`,
    });
    card.createEl("p", { text: revision.revised_text });
    card.createEl("p", {
      cls: "lc-cognitive-note",
      text: `偏离：${revision.deviation}；复核理由：${revision.revision_reason}`,
    });
  }
}

function renderCognitiveDraft(section, draft, decisionState, handlers) {
  const state = decisionState || {};
  if (!draft && !state.lastDecision && !state.error) return;
  const block = section.createDiv({ cls: "lc-cognitive-draft" });
  // R1-OC: the title must fit both the synthetic and the local chain; the
  // local chain must never be labelled 离线合成.
  block.createEl("h3", { text: "碎片认知草稿" });
  if (state.lastDecision) {
    const keptCategory = state.lastDecision.thoughtCategory;
    // Package B: the receipt is a notice next to the authoritative state
    // re-read from the backend, never a replacement that hides it.
    block.createEl("p", {
      cls: "lc-receipt",
      text:
        state.lastDecision.decision === "keep_draft"
          ? keptCategory
            ? `已记录：保留为未验证草稿（分类：${keptCategory}）。`
            : "已记录：保留为未验证草稿。"
          : "已记录：拒绝该认知结果。",
    });
    if (!draft) return;
  }
  if (!draft) return;
  block.createEl("p", {
    cls: "lc-cognitive-warning",
    text: "证据等级：未验证。该内容尚未经过真实来源验证，不是正式资产。",
  });
  if (draft.withdrawn) {
    block.createEl("p", {
      cls: "lc-cognitive-decision-note",
      text: "已撤回：该草稿已撤回，不会成为正式资产。",
    });
    return;
  }
  block.createEl("p", {
    cls: "lc-cognitive-route",
    text: `处理路线：${draft.route === "research" ? "研究路线" : "直接路线"}`,
  });
  if (draft.view) {
    renderCognitiveDraftView(block, draft.view);
    const markdownDisclosure = block.createEl("details", {
      cls: "lc-collapsed-section",
    });
    markdownDisclosure.createEl("summary", {
      cls: "lc-collapsed-summary",
      text: "完整草稿 Markdown（决定绑定的逐字原文）",
    });
    markdownDisclosure.createEl("pre", {
      cls: "lc-cognitive-markdown",
      text: draft.markdown,
    });
  } else {
    block.createEl("pre", { cls: "lc-cognitive-markdown", text: draft.markdown });
  }
  if (state.error) {
    block.createEl("p", {
      cls: "lc-control-error",
      text: `草稿决定未记录：${state.error}。请刷新后重新查看草稿。`,
    });
  }
  if (draft.withdrawalPending) {
    block.createEl("p", {
      cls: "lc-cognitive-decision-note",
      text: "撤回待恢复：撤回请求已提交但尚未完成，只能继续同一撤回。",
    });
    const resume = block.createEl("button", {
      cls: "lc-cognitive-withdraw-continue",
      text: state.submitting ? "正在撤回…" : "继续撤回",
    });
    resume.setAttribute("type", "button");
    resume.disabled = state.submitting === true;
    resume.addEventListener("click", () => handlers.onCognitiveWithdraw());
    return;
  }
  if (!draft.awaitingDecision) {
    block.createEl("p", {
      cls: "lc-cognitive-decision-note",
      text: "该草稿已保留，仍为未验证草稿，不是正式资产。",
    });
    if (draft.kept) {
      const withdraw = block.createEl("button", {
        cls: "lc-cognitive-withdraw",
        text: state.submitting ? "正在撤回…" : "撤回草稿",
      });
      withdraw.setAttribute("type", "button");
      withdraw.disabled = state.submitting === true;
      withdraw.addEventListener("click", () => handlers.onCognitiveWithdraw());
    }
    return;
  }
  const categoryWrap = block.createDiv({ cls: "lc-cognitive-category" });
  categoryWrap.createEl("label", {
    text: "思考分类（保留草稿必填，可填写已有主题或新主题）",
  });
  const categoryInput = categoryWrap.createEl("input", {
    cls: "lc-cognitive-category-input",
  });
  categoryInput.setAttribute("type", "text");
  categoryInput.setAttribute("aria-label", "思考分类");
  categoryInput.value = typeof state.thoughtCategory === "string" ? state.thoughtCategory : "";
  const categoryValid = () => isValidKeepThoughtCategory(categoryInput.value);
  // G2：格式 hint——用户必须知道「合法分类长什么样」，否则非法时一行说明等于没有
  // 参照（reason 行只在非法时出现，hint 常驻给出可照抄的格式）。
  categoryWrap.createEl("p", {
    cls: "lc-cognitive-category-hint",
    text: "示例：『MiniMax H3 是否继续核验』『研究方向确认』——1–128 字符，不含换行/回车/控制分隔符。",
  });
  const reason = block.createEl("p", {
    cls: "lc-cognitive-category-reason",
    text: "请先填写有效的思考分类（1–128 字符，不含换行、回车或控制分隔符）后才能保留草稿。",
  });
  reason.hidden = categoryValid();
  const actions = block.createDiv({ cls: "lc-cognitive-actions" });
  const keep = actions.createEl("button", {
    cls: "lc-cognitive-keep",
    text: state.submitting ? "正在记录…" : "保留草稿",
  });
  keep.setAttribute("type", "button");
  keep.disabled = state.submitting || !categoryValid();
  keep.addEventListener("click", () =>
    handlers.onCognitiveDraftDecision("keep_draft")
  );
  const reject = actions.createEl("button", {
    cls: "lc-cognitive-reject",
    text: "拒绝此结果",
  });
  reject.setAttribute("type", "button");
  if (state.submitting) reject.setAttribute("disabled", "");
  reject.addEventListener("click", () =>
    handlers.onCognitiveDraftDecision("reject")
  );
  categoryInput.addEventListener("input", () => {
    handlers.onCognitiveCategoryChange(categoryInput.value);
    reason.hidden = categoryValid();
    keep.disabled = state.submitting || !categoryValid();
  });
}

function renderMinimumValueDecision(section, detail, decisionState, handlers) {
  const decision = detail.minimumValue;
  const state = decisionState || {};
  if (!decision && !state.lastDecision && !state.error) return;
  const block = section.createDiv({ cls: "lc-minimum-value" });
  block.createEl("h3", { text: "发送模型前确认" });
  if (state.lastDecision) {
    block.createEl("p", {
      cls: "lc-receipt",
      text:
        state.lastDecision.decision === "consent"
          ? "已记录：同意发送。模型生成仍由后端按状态继续，不会由界面重复提交。"
          : "已记录：不发送。该运行已安全结束。",
    });
    return;
  }
  if (state.error) {
    block.createEl("p", {
      cls: "lc-control-error",
      text: `发送决定未记录：${state.error}。请刷新后重新查看正文。`,
    });
  }
  if (!decision) return;
  if (decision.privacyBlocked) {
    block.createEl("p", {
      cls: "lc-minimum-value-blocked",
      text: `隐私初筛已阻断：${decision.privacyFieldCategories.join("、") || "敏感字段"}`,
    });
    block.createEl("p", {
      cls: "lc-control-reason",
      text: "原正文不会发送。请先在本地替换内容，重新展示后再决定。",
    });
    return;
  }
  block.createEl("p", {
    cls: "lc-minimum-value-target",
    text: `目标：${decision.targetProvider} / ${decision.targetModel}`,
  });
  block.createEl("p", {
    cls: "lc-control-raw",
    text: `profile：${decision.targetProfile} · 正文 SHA-256：${decision.outboundPayloadSha256}`,
  });
  block.createEl("pre", {
    cls: "lc-minimum-value-payload",
    text: decision.outboundPayload || "正文不可用，已禁止发送",
  });
  const buttons = block.createDiv({ cls: "lc-control-buttons" });
  const consent = buttons.createEl("button", {
    cls: decision.ready && !state.submitting
      ? "lc-control-btn lc-minimum-value-consent"
      : "lc-control-btn lc-minimum-value-consent is-disabled",
    text: state.submitting ? "正在记录…" : "同意发送这段正文",
  });
  consent.setAttribute("type", "button");
  if (decision.ready && !state.submitting) {
    consent.addEventListener("click", () => handlers.onMinimumValueConsent(decision));
  } else {
    consent.setAttribute("disabled", "disabled");
  }
  const decline = buttons.createEl("button", {
    cls: state.submitting
      ? "lc-control-btn lc-minimum-value-decline is-disabled"
      : "lc-control-btn lc-minimum-value-decline",
    text: state.submitting ? "正在记录…" : "不发送",
  });
  decline.setAttribute("type", "button");
  if (state.submitting) {
    decline.setAttribute("disabled", "disabled");
  } else {
    decline.addEventListener("click", () => handlers.onMinimumValueDecline(decision));
  }
}

function renderControlsSection(section, state, handlers) {
  const control = state.control || { status: "unavailable" };
  const block = section.createDiv({ cls: "lc-controls" });
  block.createEl("h3", { text: "受控操作" });

  // The immutable receipt and any submit error render independently of
  // control-service availability: a failed refresh must never hide proof
  // that a write already succeeded.
  if (control.lastReceipt) {
    renderReceiptNotice(block, control.lastReceipt, control.duplicate);
  }
  if (control.submitError) {
    const errorLine = block.createDiv({ cls: "lc-control-error" });
    errorLine.createEl("p", { text: `操作未提交：${control.submitError.text}` });
    if (control.submitError.code) {
      errorLine.createEl("p", { cls: "lc-control-raw", text: `原始错误码：${control.submitError.code}` });
    }
  }

  if (control.status === "idle") {
    block.createEl("p", { cls: "lc-empty", text: "正在准备控制服务…" });
    return;
  }
  if (control.status === "loading") {
    block.createEl("p", { cls: "lc-empty", text: "正在读取控制服务状态…" });
    return;
  }
  if (control.status !== "ready") {
    const notice = block.createDiv({ cls: "lc-control-unavailable" });
    notice.createEl("p", {
      cls: "lc-control-unavailable-text",
      text: "控制服务不可用：写操作已禁用，上方只读数据不受影响。",
    });
    if (control.error) {
      notice.createEl("p", { cls: "lc-control-raw", text: `原始错误码：${control.error}` });
    }
    const retry = notice.createEl("button", { cls: "lc-control-retry", text: "重试连接" });
    retry.setAttribute("type", "button");
    retry.addEventListener("click", () => handlers.onRefreshControl());
    if (control.intents && control.intents.length) renderIntentHistory(block, control.intents);
    return;
  }

  const actions = control.actions;
  if (!actions) return;
  const summary = block.createDiv({ cls: "lc-control-summary" });
  summary.createSpan({ text: `当前状态：${actions.statusLabel}` });
  summary.createSpan({ text: `当前优先级：${actions.priority}` });
  summary.createSpan({ cls: "lc-control-raw", text: `原始状态：${actions.status} · 序号：${actions.latestSequence}` });

  const renderActionButton = (parent, entry) => {
    const btn = parent.createEl("button", {
      cls: entry.enabled ? "lc-control-btn" : "lc-control-btn is-disabled",
      text: entry.label,
    });
    btn.setAttribute("type", "button");
    btn.setAttribute("aria-label", `${entry.label}运行 ${actions.runId}`);
    if (!entry.enabled) {
      btn.setAttribute("disabled", "disabled");
      if (entry.reasonText) {
        const reason = parent.createSpan({ cls: "lc-control-reason", text: `${entry.label}不可用：${entry.reasonText}` });
        reason.setAttribute("aria-live", "polite");
      }
    } else {
      btn.addEventListener("click", () => handlers.onOpenDialog(entry.action));
    }
  };

  // 主操作: 暂停 / 继续 / 终止 only. 重试与优先级收进二级操作.
  const buttonRow = block.createDiv({ cls: "lc-control-buttons" });
  for (const entry of actions.actions) {
    if (model.PRIMARY_ACTIONS.includes(entry.action)) renderActionButton(buttonRow, entry);
  }
  const secondary = block.createEl("details", { cls: "lc-secondary-ops" });
  secondary.createEl("summary", { cls: "lc-secondary-ops-summary", text: "二级操作（重试、优先级）" });
  const secondaryRow = secondary.createDiv({ cls: "lc-control-buttons" });
  for (const entry of actions.actions) {
    if (model.SECONDARY_ACTIONS.includes(entry.action)) renderActionButton(secondaryRow, entry);
  }

  renderIntentHistory(block, control.intents);
}

function renderIntentHistory(block, intents) {
  const history = block.createEl("details", { cls: "lc-intents" });
  history.createEl("summary", { cls: "lc-intents-summary", text: `意图历史与回执（最近 ${Math.min(5, intents.length)} 条）` });
  if (!intents.length) {
    history.createEl("p", { cls: "lc-empty", text: "该运行还没有控制意图" });
  } else {
    const list = history.createDiv({ cls: "lc-intent-list" });
    for (const intent of intents.slice(0, 5)) {
      const row = list.createDiv({ cls: "lc-intent-row" });
      row.createSpan({ cls: "lc-intent-action", text: intent.actionLabel });
      row.createSpan({ cls: `lc-intent-status lc-intent-${intent.status}`, text: intent.statusLabel });
      if (intent.reasonText) row.createSpan({ cls: "lc-intent-reason", text: intent.reasonText });
      row.createSpan({ cls: "lc-intent-time", text: intent.createdAt });
      const tech = row.createEl("details", { cls: "lc-tech" });
      tech.createEl("summary", { cls: "lc-tech-summary", text: "原始值" });
      tech.createEl("p", { cls: "lc-tech-line", text: `intent_id：${intent.intentId}` });
      tech.createEl("p", { cls: "lc-tech-line", text: `idempotency_key：${intent.idempotencyKey}` });
      tech.createEl("p", {
        cls: "lc-tech-line",
        text: `status：${intent.status} · expected_sequence：${intent.expectedSequence} · applied_sequence：${intent.appliedSequence}`,
      });
      if (intent.resultRunId) {
        tech.createEl("p", { cls: "lc-tech-line", text: `result_run_id：${intent.resultRunId}` });
      }
    }
  }
}

function renderReceiptNotice(block, receipt, duplicate) {
  const notice = block.createDiv({ cls: "lc-receipt" });
  notice.createEl("p", {
    cls: "lc-receipt-title",
    text: duplicate
      ? "重复提交：控制服务返回了原始回执，没有产生新的操作"
      : "意图已提交，回执如下（状态以控制服务为准）",
  });
  notice.createEl("p", { text: `意图状态：${receipt.statusLabel}` });
  notice.createEl("p", { text: `意图 ID：${receipt.intentId}（不可变）` });
  if (receipt.reasonText) notice.createEl("p", { text: `原因：${receipt.reasonText}` });
  if (receipt.resultRunId) notice.createEl("p", { text: `新运行：${receipt.resultRunId}` });
}

function renderDialog(root, control, handlers) {
  const dialog = control.dialog;
  const overlay = root.createDiv({ cls: "lc-dialog-overlay" });
  overlay.setAttribute("tabindex", "-1");
  const box = overlay.createDiv({ cls: "lc-dialog" });
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  const actionLabel = dialog.action === "terminate" && dialog.step === 2
    ? "二次确认终止"
    : `确认${model.controlActionLabel(dialog.action)}`;
  box.setAttribute("aria-label", actionLabel);
  box.createEl("h3", { cls: "lc-dialog-title", text: actionLabel });

  const grid = box.createDiv({ cls: "lc-field-grid" });
  field(grid, "运行 ID", dialog.runId);
  field(grid, "当前状态", `${dialog.statusLabel}（${dialog.status}）`);
  field(grid, "期望序号", dialog.expectedSequence === null ? "—" : String(dialog.expectedSequence));
  const effect = model.CONTROL_ACTION_EFFECTS[dialog.action] || "";
  const effectLine = box.createEl("p", { cls: "lc-dialog-effect", text: `操作效果：${effect}` });
  effectLine.setAttribute("aria-live", "polite");

  let priorityInput = null;
  if (dialog.action === "priority") {
    const wrap = box.createDiv({ cls: "lc-dialog-field" });
    wrap.createEl("label", { text: "优先级（-10 到 10，越大越先调度）" });
    priorityInput = wrap.createEl("input", { cls: "lc-dialog-input" });
    priorityInput.setAttribute("type", "number");
    priorityInput.setAttribute("min", "-10");
    priorityInput.setAttribute("max", "10");
    priorityInput.setAttribute("step", "1");
    priorityInput.value = String(dialog.priority);
    priorityInput.setAttribute("aria-label", "优先级数值");
  }

  if (dialog.action === "terminate") {
    const warn = box.createEl("p", {
      cls: "lc-dialog-warning",
      text: dialog.step === 1
        ? "终止是不可撤销的破坏性操作。历史和产物会保留，但运行将被标记为已取消。点击“继续”进入二次确认。"
        : `二次确认：即将终止运行 ${dialog.runId}。此操作不可撤销。`,
    });
    warn.setAttribute("role", "alert");
  }

  const buttons = box.createDiv({ cls: "lc-dialog-buttons" });
  const confirmText = dialog.action === "terminate"
    ? (dialog.step === 1 ? "继续" : "确认终止")
    : "确认提交";
  const confirm = buttons.createEl("button", {
    cls: dialog.action === "terminate" && dialog.step === 2 ? "lc-dialog-confirm is-destructive" : "lc-dialog-confirm",
    text: confirmText,
  });
  confirm.setAttribute("type", "button");
  confirm.addEventListener("click", () => {
    const value = priorityInput ? Number.parseInt(priorityInput.value, 10) : undefined;
    handlers.onDialogConfirm(value);
  });
  const cancel = buttons.createEl("button", { cls: "lc-dialog-cancel", text: "取消" });
  cancel.setAttribute("type", "button");
  cancel.addEventListener("click", () => handlers.onDialogCancel());
  if (control.submitting) {
    confirm.setAttribute("disabled", "disabled");
    cancel.setAttribute("disabled", "disabled");
    box.createEl("p", { cls: "lc-empty", text: "正在提交意图…" });
  }

  // Keyboard contract: Escape cancels; Tab and Shift+Tab stay trapped
  // inside the dialog; the background is inert while the dialog is open.
  let virtualActive = null;
  overlay.addEventListener("keydown", (event) => {
    if (!event || !event.key) return;
    if (event.key === "Escape") {
      if (event.preventDefault) event.preventDefault();
      handlers.onDialogCancel();
      return;
    }
    if (event.key === "Tab") {
      const focusables = [priorityInput, confirm, cancel].filter(Boolean);
      if (!focusables.length) return;
      const active =
        typeof document !== "undefined" && document && document.activeElement
          ? document.activeElement
          : virtualActive;
      const index = focusables.indexOf(active);
      if (event.preventDefault) event.preventDefault();
      const next = event.shiftKey
        ? focusables[(index <= 0 ? focusables.length : index) - 1]
        : focusables[(index + 1) % focusables.length];
      virtualActive = next;
      if (next && typeof next.focus === "function") next.focus();
    }
  });
}

function renderConsole(root, state, handlers) {
  root.empty();
  root.addClass(VIEW_CLASS);
  renderHeader(root, state, handlers);

  const body = root.createDiv({ cls: "lc-body" });
  if (state.status === "loading") {
    renderStateNotice(body, "loading", "正在读取 Loop 状态…");
    return;
  }
  if (state.status === "contract_mismatch") {
    const d = state.error && state.error.details ? state.error.details : {};
    renderStateNotice(body, "error", "数据源契约版本不匹配，已停止渲染", `期望 contract_version=${d.expected || "?"}，实际为 ${d.actual === null ? "缺失" : d.actual}`);
    return;
  }
  if (state.status === "unreachable") {
    renderStateNotice(body, "unreachable", "无法连接 Loop 数据服务（http://127.0.0.1:5679）", "只读服务可能未运行。控制台不会编造任何数据。请启动服务后点击“刷新”。");
    return;
  }
  if (state.status === "error") {
    const err = state.error || {};
    const bits = [`HTTP 状态：${err.details && err.details.status ? err.details.status : "未知"}`];
    if (err.details && err.details.code) bits.push(`错误码：${err.details.code}`);
    if (err.details && err.details.message) bits.push(err.details.message);
    renderStateNotice(body, "error", "Loop 数据服务返回错误", bits.join(" · "));
    return;
  }

  if (state.health && state.health.stale) {
    renderStateNotice(body, "stale", "数据可能不是最新", "只读确认失败，以下为最后一次成功读取的数据。");
  } else if (state.health && state.health.dataAgeText && state.health.dataAgeSeconds >= 3600) {
    const note = body.createDiv({ cls: "lc-quiet-note" });
    note.setText(`${state.health.dataAgeText}（静默不代表过期，Loop 无新事件时属正常）`);
  }

  const columns = body.createDiv({ cls: "lc-columns" });
  const listCol = columns.createDiv({ cls: "lc-col lc-col-list" });
  renderProposalsSection(listCol, state, handlers);
  renderAttentionSection(listCol, state, handlers);
  renderQueueSection(listCol, state, handlers);
  renderRunsSection(listCol, state, handlers);
  const detailCol = columns.createDiv({ cls: "lc-col lc-col-detail" });
  if (state.proposals && state.proposals.selectedProposalId) {
    renderProposalDetail(detailCol, state, handlers);
  } else {
    renderDetail(detailCol, state, handlers);
  }

  if (state.control && state.control.dialog) {
    // Full-background isolation: EVERY non-dialog region (header with the
    // refresh button included) is inert and hidden while the modal is open.
    // The overlay is appended afterwards and stays interactive.
    for (const child of root.children) {
      child.setAttribute("inert", "");
      child.setAttribute("aria-hidden", "true");
    }
    renderDialog(root, state.control, handlers);
  } else if (state.review && state.review.dialog) {
    for (const child of root.children) {
      child.setAttribute("inert", "");
      child.setAttribute("aria-hidden", "true");
    }
    renderReviewDialog(root, state, handlers);
  }
}

module.exports = { VIEW_CLASS, renderConsole };
