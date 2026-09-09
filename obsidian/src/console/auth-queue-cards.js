/* 授权队列卡（自 homepage-cosmos.js 纯搬移；R26 gate_5c6cc9598d29 批准）。
   责任：待决/已批准授权卡的 DOM 构建、四段摘要、一键授权、队列项派生。
   控制审批绑定展示时版本；回执结果在详情内保留，不把受理等同生效。 */
"use strict";

const { isExpiredAt } = require("./view-model.js");


function makeAuthCard(doc, cfg) {
  const detail = renderAuthDetail(doc, cfg);
  return {
    kind: "auth",
    source: cfg.source,
    sourceLabel: cfg.sourceLabel,
    ref: cfg.ref,
    title: cfg.title,
    meta: cfg.meta,
    summary: cfg.summary,
    runAction: cfg.runAction,
    say: cfg.say,
    onResolved: cfg.onResolved,
    approved: Boolean(cfg.approved),
    element: detail,
  };
}

function renderAuthDetail(doc, cfg) {
  const detail = doc.createElement("div");
  detail.className = "life-cosmos-auth";
  /* R2 §4.5/§4.7：失效/过期/已批准态视觉降级（is-expired 对齐 is-disabled 语言）。 */
  if (cfg.expired) detail.toggleClass("is-expired", true);
  if (cfg.stale) detail.toggleClass("is-stale", true);
  if (cfg.approved) detail.toggleClass("is-approved", true);
  const head = detail.createDiv({ cls: "life-cosmos-auth-head" });
  head.createEl("span", { cls: "life-cosmos-auth-source", text: `[${cfg.sourceLabel}]` });
  head.createEl("h3", { text: cfg.title });
  const summaries = Array.isArray(cfg.summary) ? cfg.summary : [cfg.summary];
  for (const summary of summaries) {
    const box = detail.createDiv({ cls: "life-cosmos-auth-summary" });
    box.createEl("p", { cls: "life-cosmos-auth-line", text: `批什么：${summary.approve}` });
    box.createEl("p", { cls: "life-cosmos-auth-line", text: `验证什么：${summary.verify}` });
    box.createEl("p", { cls: "life-cosmos-auth-line", text: `授权后哪些程序会动：${summary.effects}` });
    box.createEl("p", { cls: "life-cosmos-auth-line", text: `影响面与回滚：${summary.impact}` });
    const row = box.createDiv({ cls: "life-cosmos-auth-actions" });
    if (cfg.approved) {
      // §4.7 已批准卡：撤回按钮仅服务端有撤回契约的来源渲染（当前队列源
      // 无 withdraw 契约 → 不渲染，不新造协议）；无契约即「已生效，不可撤回」。
      if (cfg.withdrawable && cfg.onWithdraw) {
        const withdraw = row.createEl("button", { cls: "life-cosmos-auth-withdraw", text: "撤回授权" });
        withdraw.setAttribute("type", "button");
        withdraw.addEventListener("click", () => cfg.onWithdraw(withdraw));
      } else {
        row.createEl("small", { cls: "life-cosmos-auth-expired", text: "已生效，不可撤回。" });
      }
      continue;
    }
    if (cfg.expired) {
      box.createEl("p", { cls: "life-cosmos-auth-expired", text: "授权单已过期，需重新签发。" });
    } else if (cfg.stale) {
      box.createEl("p", { cls: "life-cosmos-auth-expired", text: "数据已变化，请刷新。刷新后请关闭当前详情，再从队列重新打开；当前按钮保留旧版本。" });
    }
    const ref = cfg.group && summary.action ? { ...cfg.ref, action: summary.action } : cfg.ref;
    const approve = row.createEl("button", { cls: "life-cosmos-auth-approve", text: cfg.approved ? "已授权" : "一键授权" });
    approve.setAttribute("type", "button");
    approve.setAttribute("data-cosmos-action", "auth.approve");
    if (cfg.expired || cfg.stale) approve.setAttribute("disabled", "disabled");
    const resultLine = box.createEl("p", { cls: "life-cosmos-auth-result" });
    resultLine.setAttribute("role", "status");
    resultLine.setAttribute("aria-live", "polite");
    resultLine.setAttribute("aria-atomic", "true");
    approve.addEventListener("click", () => handleAuthApprove(approve, cfg, ref, resultLine));
    if (cfg.expired || cfg.stale) {
      const refresh = row.createEl("button", { cls: "life-cosmos-auth-refresh", text: "刷新" });
      refresh.setAttribute("type", "button");
      refresh.addEventListener("click", () => cfg.onRefresh && cfg.onRefresh());
    }
  }
  return detail;
}

/* R1 一键：submitting 禁用 + spinner（对齐 lc-dialog-confirm 模式）；
   成功 say + 触发卡移除（onResolved）；失败卡保留 + 中文原因回响。 */
async function handleAuthApprove(approve, cfg, ref, resultLine) {
  if (approve.getAttribute && approve.getAttribute("disabled") !== null) return;
  if (approve.setAttribute) approve.setAttribute("disabled", "disabled");
  if (approve.setText) approve.setText("授权中…");
  if (resultLine) { resultLine.setText("正在提交指令…"); resultLine.setAttribute("data-kind", "pending"); }
  let result;
  try {
    result = await (cfg.runAction ? cfg.runAction("auth.approve", { source: cfg.source, ref }) : Promise.resolve({ kind: "error", message: "授权能力不可用" }));
  } catch (error) {
    result = { kind: "error", message: error?.message || "授权提交失败，请重试。" };
  }
  if (approve.isConnected === false) return; // 抽屉已关闭
  if (resultLine) {
    resultLine.setText(result?.message || "服务未返回操作回执，尚不能确认已生效。");
    resultLine.setAttribute("data-kind", result?.kind || "error");
  }
  if (result && result.kind === "success") {
    if (approve.setText) approve.setText(cfg.source === "loop-control" ? "指令已生效 ✓" : "已授权 ✓");
    if (cfg.say) cfg.say(result);
    if (cfg.onResolved) cfg.onResolved();
  } else if (result && result.kind === "pending") {
    if (approve.setText) approve.setText("已受理，等待执行");
    if (cfg.say) cfg.say(result);
    const box = approve.closest ? approve.closest(".life-cosmos-auth-summary") : null;
    if (cfg.onRefresh && box) {
      const refresh = box.createEl("button", { cls: "life-cosmos-auth-refresh", text: "刷新状态" });
      refresh.setAttribute("type", "button");
      refresh.addEventListener("click", () => cfg.onRefresh());
      box.createEl("small", { text: "刷新后请关闭当前详情，再从队列重新打开，查看最新状态。" });
    }
  } else if (result && result.stale) {
    // R2 §4.5：漂移码 → 卡标失效态（一键禁用 + 原因行可见），保留刷新。
    if (approve.setText) approve.setText("已失效");
    if (approve.setAttribute) approve.setAttribute("disabled", "disabled");
    const box = approve.closest ? approve.closest(".life-cosmos-auth-summary") : null;
    if (box && typeof box.createEl === "function") {
      box.createEl("p", { cls: "life-cosmos-auth-expired", text: "数据已变化，请刷新。刷新后请关闭当前详情，再从队列重新打开；当前按钮保留旧版本。" });
      if (cfg.onRefresh) {
        const refresh = box.createEl("button", { cls: "life-cosmos-auth-refresh", text: "刷新" });
        refresh.setAttribute("type", "button");
        refresh.addEventListener("click", () => cfg.onRefresh());
      }
    }
    if (cfg.say) cfg.say(result);
  } else {
    if (approve.removeAttribute) approve.removeAttribute("disabled");
    if (approve.setText) approve.setText("一键授权");
    if (cfg.say) cfg.say(result);
  }
}

/* ---------- R1 授权队列：从 client 状态派生卡（设计 §4） ---------- */

function buildAuthQueueItems(doc, sources, runAction, say, onResolved, onRefresh, onWithdraw) {
  const items = [];
  // V2-C5：任一源 error → 断线标注；R25 P1-5：子请求部分失败 → partial 标注
  // （已取得卡保留，未知数量绝不伪装确定 0）。
  const offlineSources = [];
  const partialSources = [];
  const graphQueueSource = sources.graphQueue ? sources.graphQueue() : null;
  const loopReviewSource = sources.loopReview ? sources.loopReview() : null;
  const loopControlSource = sources.loopControl ? sources.loopControl() : null;
  for (const [view, label] of [[graphQueueSource, "Graph"], [loopReviewSource, "Loop"], [loopControlSource, "Loop"]]) {
    const state = view && view.state;
    if (!state) continue;
    if (state.kind === "error") offlineSources.push(label);
    if (state.partial) partialSources.push(label);
  }
  const graphQueue = graphQueueSource;
  if (graphQueue && graphQueue.state && graphQueue.state.kind === "ready") {
    for (const entry of graphQueue.state.items) {
      let card;
      card = makeAuthCard(doc, {
        source: "graph",
        sourceLabel: "Graph",
        ref: { runId: entry.runId, nodeId: entry.nodeId },
        title: `${entry.graphId} · ${entry.taskLabel}`,
        meta: "授权并执行 · 等待你的批准",
        summary: [{
          approve: `批准节点执行（${entry.preview.model || "模型"} · 上限 ${entry.preview.maxTotalCalls ?? "—"} 次）`,
          verify: `价格核验由服务端完成 · 成本上限 ¥${entry.preview.costCapCny ?? "—"} · 授权单有效期内`,
          effects: "Graph 检查点推进 · Agent 账本写入",
          impact: entry.preview.writeScope || "Graph 检查点 + Agent 账本；不写笔记、不写资产",
        }],
        runAction,
        say,
        onResolved: () => onResolved && onResolved(card),
        // R2 §4.5：授权单过期 → 卡降级 + 一键禁用（共享 isExpiredAt 判定）。
        expired: Boolean(entry.expiresAt && isExpiredAt(entry.expiresAt)),
        onRefresh: onRefresh,
      });
      items.push(card);
    }
  }
  const loopReview = loopReviewSource;
  if (loopReview && loopReview.state && loopReview.state.kind === "ready") {
    for (const entry of loopReview.state.items) {
      let card;
      card = makeAuthCard(doc, {
        source: "loop-review",
        sourceLabel: "Loop",
        ref: { proposalId: entry.proposalId, actions: entry.actions },
        title: `Loop 审核 · ${entry.subject}`,
        meta: "审核建议待批准",
        summary: [{
          approve: "批准该审核建议（decision: accepted）",
          verify: `指纹 ${(entry.actions.expectedFingerprint || "—").slice(0, 12)}… · 源序号 ${entry.actions.expectedSourceSequence ?? "—"}`,
          effects: "建议进入执行调度 · 记录审核回执",
          // R25 P1-8：与契约一致——批准即生效，当前来源不支持撤回。
          impact: "仅批准该条建议；批准即生效，当前来源不支持撤回（服务端契约）",
        }],
        runAction,
        say,
        onResolved: () => onResolved && onResolved(card),
      });
      items.push(card);
    }
  }
  const loopControl = loopControlSource;
  if (loopControl && loopControl.state && loopControl.state.kind === "ready") {
    for (const entry of loopControl.state.items) {
      let card;
      card = makeAuthCard(doc, {
        source: "loop-control",
        sourceLabel: "Loop",
        ref: { runId: entry.runId, expectedSequence: entry.expectedSequence },
        title: `Loop 控制 · ${entry.runLabel}`,
        meta: `${entry.actions.length} 项可用操作待批准`,
        group: true,
        summary: entry.actions.map((action) => ({
          approve: `执行「${action.label}」`,
          verify: `服务端可用性矩阵已确认（enabled） · 运行版本 ${entry.expectedSequence}`,
          effects: action.effect || "影响该运行调度",
          impact: "仅影响该 run；可再次调整",
          action: action.action,
        })),
        runAction,
        say,
        onRefresh,
        onResolved: () => onResolved && onResolved(card),
      });
      items.push(card);
    }
  }
  // R2 §4.7：已批准卡（撤回标的）——从 approvedAuth 源派生，排在待决卡后；
  // 不计入待决 N（updateCount 过滤 approved）。撤回按钮仅服务端有撤回契约的
  // 来源渲染；当前队列源无 withdraw 契约 → 标「已生效，不可撤回」。
  const approvedAuth = sources.approvedAuth ? sources.approvedAuth() : null;
  if (approvedAuth && Array.isArray(approvedAuth.items)) {
    for (const entry of approvedAuth.items) {
      let card;
      card = makeAuthCard(doc, {
        source: entry.source,
        sourceLabel: entry.source === "graph" ? "Graph" : "Loop",
        ref: entry.ref,
        title: `已授权 · ${entry.title || entry.source}`,
        meta: `已批准（${(entry.approvedAt || "").slice(0, 10)}）`,
        summary: [{
          approve: entry.source === "loop-control" ? entry.message || "控制指令已生效；实际执行进度请查看运行记录。" : "该授权已生效并推进",
          verify: "服务端回执已确认",
          effects: entry.source === "loop-control" ? "已记录控制回执，不代表实际任务执行完成" : "对应程序链已推进",
          // R25 P1-8：与契约一致——已生效不可撤回；有契约来源才条件渲染撤回。
          impact: "影响面见原卡摘要；已生效，当前来源不支持撤回（服务端契约）",
        }],
        runAction,
        say,
        approved: true,
        withdrawable: Boolean(entry.withdrawable),
        onWithdraw: entry.withdrawable && onWithdraw ? (button) => onWithdraw(button, entry) : null,
        onResolved: () => onResolved && onResolved(card),
      });
      items.push(card);
    }
  }
  // V2-C5 + R25 P1-5：断线/部分失败标注项排在卡堆末尾，不参与待决 N。
  const OFFLINE_MARKER = { title: "授权状态暂时不可用", meta: "对应服务未响应；可稍后手动刷新。" };
  const PARTIAL_MARKER = { hint: "部分数据不可用", title: "该来源部分条目读取失败", meta: "以上仅为已取得的卡；待决数量不完整，可稍后手动刷新。" };
  for (const [sourceList, marker] of [[offlineSources, OFFLINE_MARKER], [partialSources, PARTIAL_MARKER]]) {
    for (const sourceName of [...new Set(sourceList)]) {
      items.push({ kind: "offline", sourceLabel: sourceName, ...marker });
    }
  }
  return items;
}

module.exports = {
  buildAuthQueueItems,
  makeAuthCard,
  renderAuthDetail,
  handleAuthApprove,
};
