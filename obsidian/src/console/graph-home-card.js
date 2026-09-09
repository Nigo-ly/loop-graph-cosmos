"use strict";

// Graph homepage summary card ("Obsidian 主页产品入口与摘要层").
// Consumes ONLY the safe metadata of the 5684 read-only runs list (status,
// sequence, step_count, pending_human, started_at, updated_at). Prompt bodies,
// response bodies, node outputs, authorization phrases, credentials and note
// bodies never enter this layer; every dynamic value is rendered with text
// nodes only — innerHTML is never used for external text. The raw graph_id
// is a technical identifier and stays in the full Graph workflow view; the
// homepage never displays it.

const { runStatusLabel } = require("./graph-view-model.js");

const TERMINAL_NORMAL = new Set(["completed"]);
const TERMINAL_ABNORMAL = new Set(["blocked", "failed", "aborted"]);
const ACTIVE_STATUSES = new Set(["running", "paused"]);

const GRAPH_HOME_CARD_CLASS = "life-graph-home";

// 主页文案：completed 只表示"流程已结束"，绝不表达成业务结果已被接受。
// 完整 Graph 视图保留自己的技术状态文案，这里只收紧主页产品层。
function homeStatusLabel(status) {
  if (status === "completed") return "流程已结束";
  return runStatusLabel(status);
}

// 互斥主状态分类，优先级：异常停止 > 等待人工 > 进行中 > 流程已结束。
// 未知状态不进任何统计桶（不得错误归类），只计入 unknown 并原样显示。
function classifyRun(run) {
  const status = typeof run.status === "string" ? run.status : "";
  if (TERMINAL_ABNORMAL.has(status)) return "abnormal";
  if ((Array.isArray(run.pending_human) && run.pending_human.length > 0) || status === "human_wait") {
    return "waiting";
  }
  if (ACTIVE_STATUSES.has(status)) return "active";
  if (TERMINAL_NORMAL.has(status)) return "completed";
  return "unknown";
}

// 「最近运行」只按 updated_at（Checkpoint 墙钟时间）判断新旧；Checkpoint
// sequence 只保证单个 run 内单调，绝不跨 run 比较时间。没有可靠 updated_at
// 的 run 不参与候选；全部都没有时 latest 为 null，调用方降级为不显示
// 最近项，绝不退化成用 sequence 猜测。时间相同以 run_id 字典序稳定兜底。
function selectLatestRun(list) {
  let latest = null;
  let latestTime = Number.NEGATIVE_INFINITY;
  for (const run of list) {
    const time = Date.parse(typeof run.updated_at === "string" ? run.updated_at : "");
    if (Number.isNaN(time)) continue;
    if (
      time > latestTime ||
      (time === latestTime &&
        latest !== null &&
        String(run.run_id || "").localeCompare(String(latest.run_id || "")) > 0)
    ) {
      latest = run;
      latestTime = time;
    }
  }
  return latest;
}

// Deterministic summary over the raw contract fields. `attention` marks
// whether any run needs human attention (active / waiting / abnormal); only
// then does the homepage render the three stat cards — a fully calm summary
// collapses to a single compact line instead of three zero cards.
function computeGraphSummary(runs) {
  const list = (Array.isArray(runs) ? runs : []).filter((run) => run && typeof run === "object");
  const summary = {
    total: list.length,
    active: 0,
    waiting: 0,
    abnormal: 0,
    completed: 0,
    unknown: 0,
    attention: false,
    latest: null,
    headline: "暂无运行",
  };
  for (const run of list) summary[classifyRun(run)] += 1;
  if (!list.length) return summary;
  summary.attention = summary.active + summary.waiting + summary.abnormal > 0;
  const latest = selectLatestRun(list);
  if (latest) {
    summary.latest = {
      // runId 只用于确定性断言与调试，主页 DOM 绝不显示技术 ID。
      runId: String(latest.run_id || ""),
      statusLabel: homeStatusLabel(latest.status),
      sequence: latest.sequence,
      // 合法、带时区的 updated_at 原样保留（Cosmos 摘要的最近运行时间展示）；
      // 非法时间不会进入候选，此字段一定来自 selectLatestRun 的可靠时间。
      updatedAt: latest.updated_at,
    };
  }
  if (summary.abnormal > 0) summary.headline = "异常停止";
  else if (summary.waiting > 0) summary.headline = "等待人工";
  else if (summary.active > 0) summary.headline = "进行中";
  else if (summary.completed > 0) summary.headline = "流程已结束";
  else summary.headline = "未知状态";
  return summary;
}

// 只有异常停止与等待人工可以提高视觉强调；进行中、平静与未知状态一律
// 不渲染状态 pill，避免抢过今日任务与当前专注。
const HEADLINE_PILL_CLASSES = {
  异常停止: "is-abnormal",
  等待人工: "is-waiting",
};

function renderGraphHomeCard(card, state, handlers) {
  card.empty();
  const head = card.createDiv({ cls: "life-graph-home-head" });
  const headText = head.createDiv();
  headText.createEl("span", { cls: "life-source-label", text: "GRAPH WORKFLOW" });
  headText.createEl("h2", { text: "Graph 工作流" });
  headText.createEl("p", { cls: "life-graph-home-sub", text: "跨节点推进、人工闸门与异常状态" });

  if (state.kind === "ready") {
    const pillClass = HEADLINE_PILL_CLASSES[state.summary.headline];
    if (state.summary.total > 0 && pillClass) {
      head.createSpan({
        cls: `life-graph-home-pill ${pillClass}`,
        text: state.summary.headline,
      });
    }
  }

  const body = card.createDiv({ cls: "life-graph-home-body" });
  if (state.kind === "loading") {
    body.createEl("p", { cls: "life-graph-home-note", text: "正在读取 Graph 运行摘要…" });
  } else if (state.kind === "error") {
    // 诚实错误：服务不可达时绝不伪造 0 条运行；与暂无运行同为紧凑单行，
    // 主入口保持可用。首次失败才进这里（刷新失败走 ready+stale）。
    body.createEl("p", { cls: "life-graph-home-compact is-error", text: "Graph 服务暂时不可用" });
  } else if (state.stale) {
    // G8：刷新失败——保留旧数据 + 显式标注，与「服务不可用」首次失败区分；
    // 刷新按钮仍可用（可重试），打开入口不丢。
    body.createEl("p", { cls: "life-graph-home-compact is-stale", text: "上次刷新失败，显示上次数据" });
    if (state.summary.total === 0) {
      body.createEl("p", { cls: "life-graph-home-compact", text: "暂无 Graph 运行" });
    } else if (state.summary.attention) {
      const summary = state.summary;
      const stats = body.createDiv({ cls: "life-graph-home-stats" });
      for (const [label, value, cls] of [
        ["进行中", summary.active, "is-active"],
        ["等待人工", summary.waiting, "is-waiting"],
        ["异常停止", summary.abnormal, "is-abnormal"],
      ]) {
        const stat = stats.createDiv({ cls: `life-graph-home-stat ${cls}` });
        stat.createEl("strong", { text: String(value) });
        stat.createEl("span", { text: label });
      }
      if (summary.latest) {
        body.createEl("p", {
          cls: "life-graph-home-latest",
          text: `最近运行：${summary.latest.statusLabel} · Checkpoint #${summary.latest.sequence}`,
        });
      }
    } else {
      const summary = state.summary;
      body.createEl("p", {
        cls: "life-graph-home-compact",
        text: summary.latest
          ? `Graph 工作流 · ${summary.latest.statusLabel} · Checkpoint #${summary.latest.sequence}`
          : `Graph 工作流 · ${summary.headline}`,
      });
    }
  } else if (state.summary.total === 0) {
    body.createEl("p", { cls: "life-graph-home-compact", text: "暂无 Graph 运行" });
  } else if (state.summary.attention) {
    const summary = state.summary;
    const stats = body.createDiv({ cls: "life-graph-home-stats" });
    for (const [label, value, cls] of [
      ["进行中", summary.active, "is-active"],
      ["等待人工", summary.waiting, "is-waiting"],
      ["异常停止", summary.abnormal, "is-abnormal"],
    ]) {
      const stat = stats.createDiv({ cls: `life-graph-home-stat ${cls}` });
      stat.createEl("strong", { text: String(value) });
      stat.createEl("span", { text: label });
    }
    if (summary.latest) {
      body.createEl("p", {
        cls: "life-graph-home-latest",
        text: `最近运行：${summary.latest.statusLabel} · Checkpoint #${summary.latest.sequence}`,
      });
    }
  } else {
    // 平静状态：三项全为 0，紧凑单行，不渲染三个 0 卡片；没有可靠时间的
    // 列表不声称「最近运行」，降级为不带 Checkpoint 段的状态行。
    const summary = state.summary;
    body.createEl("p", {
      cls: "life-graph-home-compact",
      text: summary.latest
        ? `Graph 工作流 · ${summary.latest.statusLabel} · Checkpoint #${summary.latest.sequence}`
        : `Graph 工作流 · ${summary.headline}`,
    });
  }

  const actions = card.createDiv({ cls: "life-graph-home-actions" });
  // G4：attention 且有等待人工时给「去处理等待项」直达出口——一键直达
  // 最近等待 run 的详情（runId 只进 handler，不进 DOM，技术 ID 不展示）。
  if (
    state.kind === "ready" &&
    state.summary.attention &&
    state.summary.waiting > 0 &&
    state.summary.latest &&
    handlers.onOpenRun
  ) {
    const waiting = actions.createEl("button", {
      cls: "life-graph-home-waiting",
      text: "去处理等待项",
    });
    waiting.setAttribute("type", "button");
    waiting.addEventListener("click", () => handlers.onOpenRun(state.summary.latest.runId));
  }
  const open = actions.createEl("button", { cls: "life-graph-home-open", text: "打开 Graph 工作流" });
  open.setAttribute("type", "button");
  open.addEventListener("click", () => handlers.onOpen());
  const refresh = actions.createEl("button", { cls: "life-graph-home-refresh", text: "刷新" });
  refresh.setAttribute("type", "button");
  if (state.kind === "loading") refresh.setAttribute("disabled", "disabled");
  refresh.addEventListener("click", () => handlers.onRefresh());
}

// 每个主页副本注入一张卡（锚定在 DECISION SUPPORT 决策条之后）；结构幂等——
// 已有卡片只做内容重渲染，绝不重复创建。ready 状态传入原始 runs，摘要在此
// 由 computeGraphSummary 统一计算，所有副本共享同一份确定性结果。
function injectHomepageGraphCard(doc, state, handlers) {
  const containers = doc.querySelectorAll(".my-life-homepage-view .life-dashboard-content");
  const viewState =
    state && state.kind === "ready"
      ? { kind: "ready", stale: state.stale === true, summary: computeGraphSummary(state.runs) }
      : state;
  let created = 0;
  containers.forEach((container) => {
    let card = container.querySelector(`.${GRAPH_HOME_CARD_CLASS}`);
    if (!card) {
      card = doc.createElement("section");
      card.className = `life-panel ${GRAPH_HOME_CARD_CLASS}`;
      const anchor = container.querySelector(".life-decision-bar");
      if (anchor && typeof anchor.insertAdjacentElement === "function") {
        anchor.insertAdjacentElement("afterend", card);
      } else {
        container.appendChild(card);
      }
      created += 1;
    }
    renderGraphHomeCard(card, viewState, handlers);
  });
  return created;
}

module.exports = {
  GRAPH_HOME_CARD_CLASS,
  computeGraphSummary,
  injectHomepageGraphCard,
};
