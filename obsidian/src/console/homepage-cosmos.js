"use strict";

// 复用既有安全模型与资格判定（纯函数，无第二套业务逻辑）：
// - computeGraphSummary：Graph 六态与最近运行的唯一权威计算；
// - researchProposalReady：升级提案可否创建研究 Run 的冻结规则；
// - graph-pilot-bridge 的候选资格/已桥接判定与显式路径读取。
const { computeGraphSummary } = require("./graph-home-card.js");
const { researchProposalReady, knowledgePublicationOf, researchResultDigestOf } = require("./fragment-intent-client.js");
const { alignmentFor, cognitiveOfExecution, hasResearchConclusion, subscriptionExecutionLabel, renderEffectiveExecutionScope, independentReviewState, renderIndependentReview, renderIndependentReviewPending } = require("./fragment-intent-card.js");
// R26（gate_5c6cc9598d29）：授权队列卡纯搬移至 auth-queue-cards.js，行为不变。
const { buildAuthQueueItems } = require("./auth-queue-cards.js");
const {
  candidateEligibility,
  pilotRunForFragment,
  normalizeFragmentBasename,
  cardFragmentRefs,
} = require("./graph-pilot-bridge.js");

// Cosmos homepage v11 — faithful product port of the approved K3 concept
// (thought-map-r3/v11-cosmos.html). All numbers and cards come from the
// existing Dataview/Loop/Graph projections via buildCosmosModel; this layer
// owns presentation and session-only visual state, never business state.
//
// Single-ownership contract: the My Life.md Dataview template owns the
// homepage structure and creates exactly two top-level regions —
//   <div class="life-cosmos-mount" data-life-cosmos-mount="1"></div>
//   <div class="life-cosmos-legacy">…旧搜索栏、配色入口与整个旧 dashboard…</div>
// The plugin only ever writes inside the mount; it never moves, clears or
// re-wraps any Dataview-owned node. The legacy surface stays collapsed until
// the user explicitly expands it ("深度工作台"), and every reveal() target
// still resolves inside it. Cosmos reaches the page exclusively through the
// explicit plugin.mountHomepageCosmos(root) handshake at the end of the
// template — the MutationObserver scan never injects Cosmos.

const COSMOS_CLASS = "life-cosmos-home";
const LEGACY_CLASS = "life-cosmos-legacy";
const MOUNT_ATTR = "data-life-cosmos-mount";
const MOUNT_SELECTOR = `[${MOUNT_ATTR}]`;
const LOADING_CLASS = "life-cosmos-loading";
const ERROR_CLASS = "life-cosmos-error";

const STATUS_COLORS = {
  open: "#f5f2fa",
  provisional: "#e8c393",
  concluded: "#a6ccb1",
};
const SYSTEM_COLORS = [
  [169, 152, 216],
  [166, 204, 177],
  [155, 200, 214],
  [214, 166, 220],
  [232, 195, 147],
  [150, 150, 160],
];
// v11 概念稿六峰坐标；超出六个主题时在下方弧线补位。
const SYSTEM_SPOTS = [
  [0.30, 0.34], [0.68, 0.26], [0.52, 0.58],
  [0.82, 0.62], [0.18, 0.70], [0.44, 0.80],
];
const WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

function clean(value) {
  return String(value == null ? "" : value).replace(/\s+/g, " ").trim();
}

/* 递归文本：真实 DOM 的 textContent 含后代，测试替身用 .text 递归求值。 */
function textOf(element) {
  if (!element) return "";
  if (typeof element.text === "string") return element.text;
  return clean(element.textContent);
}

/* 真实 DOM 的 class 存在于 attribute，FakeEl 存在于 classes 集合——两处都查。 */
function hasClass(el, name) {
  if (!el) return false;
  if (el.classes?.has?.(name)) return true;
  const attr = el.getAttribute?.("class");
  if (attr && attr.split(/\s+/).includes(name)) return true;
  const cn = typeof el.className === "string" ? el.className : "";
  return cn.split(/\s+/).includes(name);
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function firstText(root, selector, fallback = "") {
  return clean(queryAll(root, selector)[0]?.textContent) || fallback;
}

function linkPath(element) {
  if (!element) return "";
  /* 根节点自身的显式 data-path/data-href 优先（生产 life-asset-card /
     life-watch-card 等把路径放在根节点），其后才读后代链接。 */
  const own = clean(element.getAttribute?.("data-path") || element.getAttribute?.("data-href"));
  if (own) return own;
  const link = element.matches?.(".internal-link") ? element
    : element.querySelector?.(".internal-link") || element.querySelector?.("[data-href]") || element.querySelector?.("[data-path]");
  return clean(link?.getAttribute?.("data-href") || link?.getAttribute?.("data-path") || link?.getAttribute?.("href"));
}

/* 进度全景只接受显式碎片身份：优先 data-source-fragment / data-fragment-id，
   其次复用既有 cardFragmentRefs 的真实链接绑定；绝不从标题猜 fragment_id。 */
function explicitFragmentId(element) {
  const direct = clean(
    element?.getAttribute?.("data-source-fragment") ||
    element?.getAttribute?.("data-fragment-id")
  );
  if (direct) return normalizeFragmentBasename(direct);
  return cardFragmentRefs(element).basename || "";
}

function numberFrom(root, label, fallback = 0) {
  for (const stat of root.querySelectorAll(".life-stat")) {
    if (clean(stat.querySelector("span")?.textContent) !== label) continue;
    const value = Number.parseInt(clean(stat.querySelector("strong")?.textContent), 10);
    return Number.isFinite(value) ? value : fallback;
  }
  return fallback;
}

function sourceItems(root, selector, titleSelector, limit) {
  return [...root.querySelectorAll(selector)].map((element) => ({
    element,
    path: linkPath(element),
    title: firstText(element, titleSelector),
    meta: firstText(element, "small, p", "打开查看详情"),
    time: firstText(element, "time, .life-capture-time", ""),
  })).filter((item) => item.title).slice(0, limit);
}

function decisionItems(root) {
  const candidates = [
    ...root.querySelectorAll(".fragment-intent-card"),
    ...root.querySelectorAll(".life-loop-review-row.is-attention"),
  ];
  const seen = new Set();
  return candidates.filter((element) => {
    if (seen.has(element)) return false;
    seen.add(element);
    return Boolean(
      element.querySelector(".fragment-intent-confirm")
      || element.querySelector(".fragment-intent-research-create")
      || element.classList?.contains("is-attention")
      || element.classes?.has?.("is-attention")
    );
  }).slice(0, 6).map((element, index) => ({
    element,
    title: firstText(element, "strong", `待确认项目 ${index + 1}`),
    /* 已确认卡片里真正的待决动作是"创建研究 Run"；此时 reason 是流程状态
       （如"处理已结束"），不是待办说明，优先取提案备注。 */
    meta: firstText(element, ".fragment-research-proposal-note")
      || firstText(element, ".fragment-intent-reason")
      || firstText(element, "p")
      || firstText(element, "small", "需要你的决定"),
  }));
}

function parseClockSeconds(value) {
  const match = /^(\d{1,3}):(\d{2})(?::(\d{2}))?$/.exec(clean(value));
  if (!match) return null;
  const minutes = Number(match[1]) * 60 + Number(match[2]);
  return minutes + Number(match[3] || 0);
}

/* ---------- v13 四板块：真实 DOM 投影取数 ---------- */

function captureItems(root, limit = 6) {
  return [...root.querySelectorAll(".life-capture-card")].map((element) => {
    const kind = clean(element.querySelector("div > span")?.textContent);
    return {
      element,
      fragmentId: explicitFragmentId(element),
      title: firstText(element, "strong"),
      meta: firstText(element, "p"),
      time: firstText(element, "time"),
      kind,
      status: firstText(element, ".life-capture-status"),
      tone: hasClass(element, "is-archived") || hasClass(element, "is-ready")
        ? "r" : hasClass(element, "needs-retry") ? "e" : "q",
    };
  }).filter((item) => item.title).slice(0, limit);
}

function pipelineCounts(root) {
  return [...root.querySelectorAll(".life-pipeline-status > span")].map((span) => {
    const count = Number.parseInt(clean(span.querySelector("b")?.textContent), 10);
    return {
      count: Number.isFinite(count) ? count : 0,
      label: clean(span.textContent).replace(/\s*\d+\s*$/, ""),
    };
  });
}

function organizedItems(root, limit = 4) {
  return [...root.querySelectorAll(".life-organized-card")].map((element) => {
    const lines = [...element.querySelectorAll("p")].map((p) => clean(p.textContent));
    return {
      element,
      fragmentId: explicitFragmentId(element),
      title: firstText(element, "strong"),
      goal: (lines[0] || "").replace(/^目标\s*/, ""),
      next: (lines[1] || "").replace(/^下一步\s*/, ""),
    };
  }).filter((item) => item.title).slice(0, limit);
}

function feedbackItems(root, limit = 4) {
  return [...root.querySelectorAll(".life-feedback-card")].map((element) => ({
    element,
    fragmentId: explicitFragmentId(element),
    title: firstText(element, "strong"),
    label: clean(element.querySelector("span")?.textContent),
    body: firstText(element, "p"),
    tone: hasClass(element, "is-complete") ? "pass" : hasClass(element, "is-failed") ? "fail" : "run",
  })).filter((item) => item.title).slice(0, limit);
}

function assetItems(root, limit = 8) {
  return [...root.querySelectorAll(".life-asset-card")].map((element) => ({
    element,
    fragmentId: explicitFragmentId(element),
    path: linkPath(element),
    title: firstText(element, "strong"),
    maturity: clean(element.querySelector(".life-asset-meta span")?.textContent),
    date: clean(element.querySelector(".life-asset-meta time")?.textContent),
    tags: [...element.querySelectorAll(".life-asset-tags i")]
      .map((tag) => clean(tag.textContent)).filter(Boolean).slice(0, 3),
  })).filter((item) => item.title).slice(0, limit);
}

function paraItems(root) {
  return [...root.querySelectorAll(".life-database .life-intelligence-card")].map((element) => ({
    element,
    path: linkPath(element),
    label: clean(element.querySelector(".life-note-link")?.textContent),
    desc: firstText(element, "p"),
  })).filter((item) => item.label).slice(0, 4);
}

/* P2 只读投影：日报 / 持续关注 / AI 洞察 / 逾期 / 盲区。全部来自旧 DOM 的
   显式 data-path/data-href 与安全文本；无路径的项保留诚实空路径，由 UI
   说「暂无可打开文档」，绝不从标题拼路径。 */
function linkItems(root, selector, limit) {
  return [...root.querySelectorAll(selector)].map((element) => ({
    element,
    path: linkPath(element),
    title: firstText(element, "a, strong, .life-note-link"),
    meta: firstText(element, "small, p"),
    read: hasClass(element, "is-read"),
  })).filter((item) => item.title).slice(0, limit);
}

function overdueItems(root, limit = 5) {
  return [...root.querySelectorAll(".life-attention li")].map((element) => ({
    element,
    path: linkPath(element),
    title: clean(textOf(element)).replace(/\s*截止.*$/, ""),
    meta: clean(element.querySelector("small")?.textContent),
  })).filter((item) => item.title).slice(0, limit);
}

function blindspotItems(root, limit = 4) {
  return [...root.querySelectorAll(".life-blindspots li")].map((element) => {
    const action = queryAll(element, "[data-view-filter], [data-scroll-target]")[0];
    return {
      element,
      title: clean(element.querySelector("span")?.textContent || element.textContent),
      actionLabel: clean(action?.textContent),
      filter: clean(action?.getAttribute?.("data-view-filter")),
      scrollTarget: clean(action?.getAttribute?.("data-scroll-target")),
    };
  }).filter((item) => item.title).slice(0, limit);
}

/* 任务完成绑定：只有旧 DOM 显式携带 data-complete-task + data-task-line
   时，GO/EVA 才允许宣称真实完成；缺任一字段降级为「本次会话选择」。 */
function completeBinding(element) {
  if (!element || typeof element.querySelector !== "function") return null;
  const trigger = element.querySelector("[data-complete-task]");
  const holder = trigger || element;
  const path = clean(holder.getAttribute?.("data-complete-task"));
  const lineRaw = holder.getAttribute?.("data-task-line");
  const line = Number(lineRaw);
  if (!path || lineRaw === null || lineRaw === "" || !Number.isFinite(line)) return null;
  return { path, line };
}

function graphStatItems(root) {
  return [...root.querySelectorAll(".life-graph-home-stat")].map((element) => ({
    value: Number.parseInt(clean(element.querySelector("strong")?.textContent), 10) || 0,
    label: clean(element.querySelector("span")?.textContent),
  })).filter((item) => item.label);
}

const LOOP_SLOT_BY_TONE = {
  attention: "ATTENTION",
  success: "SUCCESS",
  quiet: "QUIET",
  muted: "QUIET",
  warning: "WARNING",
  danger: "DANGER",
};

function loopTileItems(root) {
  const slots = { ATTENTION: 0, SUCCESS: 0, QUIET: 0, WARNING: 0, DANGER: 0 };
  const elements = {};
  for (const tile of root.querySelectorAll(".life-loop-review-tile")) {
    const count = Number.parseInt(clean(tile.querySelector(".life-loop-review-tile-count")?.textContent), 10) || 0;
    const tone = Object.keys(LOOP_SLOT_BY_TONE).find((name) => hasClass(tile, `is-${name}`));
    const slot = LOOP_SLOT_BY_TONE[tone] || "QUIET";
    slots[slot] += count;
    if (!elements[slot]) elements[slot] = tile;
  }
  return { slots, elements };
}

function buildCosmosModel(root, map) {
  const tasks = sourceItems(root, ".life-todos li", "strong", Number.MAX_SAFE_INTEGER).map((task) => ({
    ...task,
    binding: completeBinding(task.element),
  }));
  /* W6：看门狗故障投影——legacy 模板 .life-faults li 的只读投影 */
  const faults = sourceItems(root, ".life-faults li", "strong", 8);
  const fragments = sourceItems(root, ".life-capture-card", "strong", 8);
  const decisions = decisionItems(root);
  const loop = loopTileItems(root);
  return {
    thoughts: Number(map?.total || 0),
    systems: Number(map?.categories?.length || 0),
    todayProgress: numberFrom(root, "今日产出", tasks.length),
    pendingCount: numberFrom(root, "待处理", tasks.length),
    knowledgeCount: numberFrom(root, "知识规模", 0),
    decisions,
    tasks,
    faults,
    fragments,
    daily: linkItems(root, ".life-daily-card", 6),
    watched: linkItems(root, ".life-watch-card", 6),
    signals: linkItems(root, ".life-signal-card", 4),
    overdue: overdueItems(root),
    blindspots: blindspotItems(root),
    decisionMatch: firstText(root, ".life-decision-match span", ""),
    /* 专注栏标题原样保留（生产主页的当前焦点就是 My Life 本身）：不伪装成
       首个任务；计时动作由既有 timer 命令作用于真实当前文档。 */
    focusTitle: firstText(root, ".life-focus-bar strong") || "尚未选择当前专注",
    focusElement: root.querySelector(".life-focus-bar"),
    focusTime: firstText(root, ".life-focus-time", "25:00"),
    focusRunning: clean(root.querySelector(".life-focus-bar small")?.textContent).includes("正在记录"),
    /* v13 板块投影 */
    captures: captureItems(root),
    pipeline: pipelineCounts(root),
    organized: organizedItems(root),
    feedback: feedbackItems(root),
    assets: assetItems(root),
    /* 全景必须覆盖全部显式绑定碎片；现有四列仍使用原有限量数组，避免
       视觉回归。这里不建立第二套状态，只保留同一 DOM 投影的无截断视图。 */
    journeySources: {
      captures: captureItems(root, Number.MAX_SAFE_INTEGER),
      organized: organizedItems(root, Number.MAX_SAFE_INTEGER),
      feedback: feedbackItems(root, Number.MAX_SAFE_INTEGER),
      assets: assetItems(root, Number.MAX_SAFE_INTEGER),
    },
    noteDays: new Set([...root.querySelectorAll(".life-day.has-note")]
      .map((day) => clean(day.getAttribute("data-path"))).filter(Boolean)),
    para: paraItems(root),
    graphStats: graphStatItems(root),
    loopSlots: loop.slots,
    loopElements: loop.elements,
    map: map || { total: 0, counts: {}, categories: [] },
  };
}

const FRAGMENT_JOURNEY_STAGES = [
  ["capture", "收集"], ["organize", "整理"], ["direction", "方向"],
  ["verify", "核验"], ["practice", "实践"], ["conclusion", "结论"],
  ["confirm", "确认"], ["asset", "资产"],
];

function safeSourceItems(source) {
  try {
    const value = typeof source === "function" ? source() : null;
    return Array.isArray(value?.items) ? value.items : [];
  } catch (_error) {
    return [];
  }
}

function emptyJourneyStages() {
  return Object.fromEntries(FRAGMENT_JOURNEY_STAGES.map(([id]) => [id, { state: "idle", label: "未开始" }]));
}

/* 自动承接视图：只按真实记录呈现——服务故障优先；有记录按 outcome；
   无记录 = 尚无接管证据（未扫描/被截断/服务异常），不得承诺已接管。 */
function autoProposeJourneyView(reportEntry, intentsError) {
  if (intentsError) {
    return {
      stage: { state: "blocked", label: "服务故障" }, state: "blocked",
      statusLabel: "服务故障",
      nextAction: `处理方向服务暂不可用：${intentsError}`,
    };
  }
  if (reportEntry) {
    switch (reportEntry.outcome) {
      case "proposed":
        return {
          stage: { state: "current", label: "系统接管中" }, state: "active",
          statusLabel: "系统接管中",
          nextAction: "已自动承接，正在生成处理方向建议",
        };
      case "waiting":
        return {
          stage: { state: "current", label: "等待上游整理" }, state: "blocked",
          statusLabel: "等待上游整理",
          nextAction: `材料未就绪（${reportEntry.reason}），就绪后系统自动承接`,
        };
      case "failed":
        return {
          stage: { state: "blocked", label: "接管失败" }, state: "blocked",
          statusLabel: "接管失败",
          nextAction: `系统接管失败（${reportEntry.reason}），后台自动重试，无需你操作`,
        };
      case "deferred":
        return {
          stage: { state: "skip", label: "不自动承接" }, state: "untracked",
          statusLabel: "历史碎片",
          nextAction: "早于自动承接范围，不自动处理；如需补接请明确指示",
        };
      case "indeterminate":
        return {
          stage: { state: "blocked", label: "资格未知" }, state: "blocked",
          statusLabel: "无法判断承接资格",
          nextAction: `整理日期缺失或非法（${reportEntry.reason}），无法判断是否自动承接`,
        };
      case "skipped":
        if (reportEntry.reason === "privacy_excluded") {
          return {
            stage: { state: "skip", label: "隐私排除" }, state: "untracked",
            statusLabel: "隐私保护排除",
            nextAction: "隐私级碎片不自动承接；原始记录仍保留",
          };
        }
        return {
          stage: { state: "skip", label: "未进入 Loop" }, state: "untracked",
          statusLabel: "未进入 Loop",
          nextAction: "这条碎片未选择系统接管；如需接管，请在碎片中勾选进入 Loop",
        };
      default:
        break;
    }
  }
  return {
    stage: { state: "current", label: "接管状态待确认" }, state: "untracked",
    statusLabel: "尚未确认接管状态",
    nextAction: "尚无接管证据；后台每分钟扫描，若长期停留请查看服务状态",
  };
}

/* 通用碎片航线是既有权威状态的只读并集，不拥有业务状态。任何缺少显式
   fragment_id 的旧卡只留在原四列，不用标题模糊匹配进全景。 */
function buildFragmentJourneys(model, sources = {}) {
  const records = new Map();
  const ensure = (fragmentId, patch = {}) => {
    const id = normalizeFragmentBasename(fragmentId);
    if (!id) return null;
    const current = records.get(id) || {
      fragmentId: id,
      title: id,
      element: null,
      alignment: null,
      review: null,
      stages: emptyJourneyStages(),
      state: "untracked",
      statusLabel: "尚未确认接管状态",
      nextAction: "尚无接管证据，等待实际处理记录",
      updatedAt: "",
    };
    if (patch.title) current.title = patch.title;
    if (patch.element) current.element = patch.element;
    Object.assign(current, patch);
    records.set(id, current);
    return current;
  };

  const journeySources = model.journeySources || {};
  /* 自动承接逐条结果（后端内存投影）：服务故障优先；有记录按真实
     outcome 呈现；无记录 = 尚无接管证据，不得显示成排队中或用户待办。 */
  const autoProposeByFragment = new Map();
  let intentsError = "";
  try {
    const intentsSource = typeof sources.intents === "function" ? sources.intents() : null;
    intentsError = (intentsSource && intentsSource.error) || "";
    for (const entry of (intentsSource && intentsSource.autoPropose) || []) {
      const id = entry && normalizeFragmentBasename(entry.fragment_id);
      if (id) autoProposeByFragment.set(id, entry);
    }
  } catch (_error) { intentsError = "读取失败"; }
  for (const item of journeySources.captures || []) {
    const row = ensure(item.fragmentId, { title: item.title, element: item.element });
    if (!row) continue;
    row.stages.capture = { state: "done", label: "已进入" };
    const view = autoProposeJourneyView(autoProposeByFragment.get(row.fragmentId) || null, intentsError);
    row.stages.direction = view.stage;
    if (view.state) row.state = view.state;
    row.statusLabel = view.statusLabel;
    row.nextAction = view.nextAction;
  }
  for (const item of journeySources.organized || []) {
    const row = ensure(item.fragmentId, { title: item.title, element: item.element });
    if (!row) continue;
    row.stages.capture = { state: "done", label: "已进入" };
    row.stages.organize = { state: "done", label: "已整理" };
    const view = autoProposeJourneyView(autoProposeByFragment.get(row.fragmentId) || null, intentsError);
    row.stages.direction = view.stage;
    if (view.state) row.state = view.state;
    row.statusLabel = view.statusLabel;
    row.nextAction = view.nextAction;
  }
  for (const item of journeySources.feedback || []) {
    const row = ensure(item.fragmentId, { title: item.title, element: item.element });
    if (!row) continue;
    row.stages.practice = item.tone === "fail"
      ? { state: "blocked", label: "实践失败" }
      : item.tone === "pass" ? { state: "done", label: "已有反馈" }
        : { state: "current", label: "实践中" };
  }
  for (const item of journeySources.assets || []) {
    const row = ensure(item.fragmentId, { title: item.title, element: item.element });
    if (!row) continue;
    row.stages.confirm = { state: "done", label: "人工通过" };
    row.stages.asset = { state: "done", label: "已入库" };
    row.state = "done";
    row.statusLabel = "已成为资产";
    row.nextAction = "后续变化将形成新版本";
  }

  const alignments = safeSourceItems(sources.intents);
  const fragmentIds = [...new Set(alignments.map((item) => item.fragment_id).filter(Boolean))];
  for (const fragmentId of fragmentIds) {
    const item = alignmentFor(alignments, fragmentId);
    if (!item) continue;
    const row = ensure(fragmentId, { title: item.title, alignment: item, updatedAt: item.updated_at || "" });
    row.stages.capture = { state: "done", label: "已进入" };
    if (item.source_origin !== "raw_capture") row.stages.organize = { state: "done", label: "已整理" };
    else if (row.stages.organize.state !== "done") row.stages.organize = { state: "skip", label: "直接研究" };
    if (["closed", "archived", "cancelled", "rejected"].includes(item.status)) {
      row.state = "untracked";
      row.statusLabel = "历史处理已结束";
      row.nextAction = "保留历史记录；当前没有待确认方向";
      continue;
    }
    if (item.status === "suggested") {
      row.stages.direction = { state: "wait", label: "等你确认" };
      row.state = "waiting";
      row.statusLabel = "等我决定";
      row.nextAction = "确认当前 episode 的处理方向";
      continue;
    }
    row.stages.direction = { state: "done", label: "已确认" };
    const execution = item.execution || null;
    row.state = execution ? "active" : "untracked";
    if (["closed", "archived", "cancelled", "rejected"].includes(execution?.status)) {
      row.state = "untracked";
      row.statusLabel = "历史执行已结束";
      row.nextAction = "保留执行记录；当前没有待确认操作";
      continue;
    }
    if (["save_only", "direct"].includes(item.route)) {
      row.stages.verify = { state: "skip", label: "本路线跳过" };
    } else if (item.route === "verify") {
      if (!execution) {
        row.stages.verify = { state: "idle", label: "尚无执行记录" };
        row.statusLabel = "尚未确认执行状态";
        row.nextAction = "方向已记录；尚无系统开始研究的回执";
      } else if (["approved", "queued", "running"].includes(execution.status)) {
        const researching = execution?.research_progress?.stage === "researching";
        row.stages.verify = { state: "current", label: researching ? "研究中" : "核验中" };
        row.statusLabel = researching ? "系统正在研究" : "核验进行中";
        row.nextAction = researching ? "系统正在核对材料并形成判断" : "等待本轮核验结果";
      } else if (execution.status === "blocked") {
        row.stages.verify = { state: "blocked", label: "核验受阻" };
        row.state = "blocked";
        row.statusLabel = "核验受阻";
        row.nextAction = execution.stop_reason || "查看阻断原因";
      } else {
        // 自主认知闭环（TASK F）：按认知态投影——能力关闭/处理中/观察不是
        // 用户任务；只有真需要人工的边界才进入「等我决定」。
        const cognitive = cognitiveOfExecution(execution);
        const view = COGNITIVE_JOURNEY[cognitive] || COGNITIVE_JOURNEY.collecting;
        row.stages.verify = view.stage;
        if (view.state) row.state = view.state;
        if (view.statusLabel) row.statusLabel = view.statusLabel;
        if (view.nextAction) row.nextAction = view.nextAction;
      }
    } else if (item.route === "graph") {
      row.stages.verify = { state: "current", label: "Graph 路线" };
      row.statusLabel = "准备进入 Graph";
      row.nextAction = "查看 Graph 升级提案";
    }
    // failure harvest（零证据诚实态）不算候选结果——认知未完成不得冒充已形成。
    const hasCandidate = item.route === "verify"
      ? ["synthesized", "conflicted"].includes(cognitiveOfExecution(execution))
      : Boolean(execution?.result) ||
        (Array.isArray(execution?.harvest) && execution.harvest.some((entry) => entry && entry.role !== "failure"));
    if (hasCandidate) {
      row.stages.conclusion = { state: "done", label: "已有候选" };
      if (row.state === "active") {
        row.state = "candidate";
        row.statusLabel = "候选已形成";
        row.nextAction = "等待人工确认";
      }
    }
  }

  for (const review of safeSourceItems(sources.reviews)) {
    const fragmentId = normalizeFragmentBasename(review.fragment_ref);
    const closedReview = ["closed", "archived", "cancelled", "rejected"].includes(review.status);
    // An ended historical review cannot replace a current direction or live review.
    const current = records.get(fragmentId);
    if (closedReview && (current?.alignment || (current?.review &&
      !["closed", "archived", "cancelled", "rejected"].includes(current.review.status)))) continue;
    const row = ensure(fragmentId, { title: review.title, review });
    if (!row) continue;
    row.stages.conclusion = { state: "done", label: "候选就绪" };
    if (closedReview && review.content_status === "pending_confirmation") {
      row.state = "untracked";
      row.statusLabel = "历史审阅已结束";
      row.nextAction = "保留历史；当前不需要再次确认";
    } else if (review.content_status === "pending_confirmation") {
      row.stages.confirm = { state: "wait", label: "等你确认" };
      row.state = "waiting";
      row.statusLabel = "候选待确认";
      row.nextAction = "审阅候选知识卡";
    } else if (review.content_status === "published_asset") {
      row.stages.confirm = { state: "done", label: "人工通过" };
      row.stages.asset = { state: "done", label: "已入库" };
      row.state = "done";
      row.statusLabel = "已成为资产";
      row.nextAction = "后续变化将形成新版本";
    } else if (review.content_status === "conflict") {
      row.stages.confirm = { state: "blocked", label: "确认冲突" };
      row.state = "blocked";
      row.statusLabel = "确认冲突";
      row.nextAction = "处理候选版本冲突";
    } else {
      row.stages.confirm = { state: "done", label: review.content_status === "kept_draft" ? "保留草稿" : "已处理" };
      row.stages.asset = { state: "skip", label: review.content_status === "rejected" ? "已拒绝" : "未入库" };
      row.state = "done";
      row.statusLabel = review.content_status === "kept_draft" ? "已保留草稿" : "已处理";
      row.nextAction = "当前无需操作";
    }
  }

  for (const row of records.values()) {
    const execution = row.alignment?.execution;
    const review = independentReviewState(execution);
    if (review) {
      row.state = review.active ? "active" : "blocked";
      row.statusLabel = review.label;
      row.nextAction = review.note;
      row.stages.verify = { state: review.active ? "current" : "blocked", label: "证据复核（模型）" };
      row.stages.conclusion = { state: "current", label: "本轮尚未完成" };
      row.stages.confirm = { state: "skip", label: "无需人工审批" };
      row.stages.asset = { state: "skip", label: "历史版本保留" };
      continue;
    }
    const publication = knowledgePublicationOf(execution);
    if (!publication && !hasResearchConclusion(execution)) continue;
    row.stages.conclusion = { state: "done", label: "已形成结论" };
    row.stages.confirm = { state: "skip", label: "无需人工审批" };
    row.stages.asset = publication ? { state: "done", label: "已自动沉淀" } : { state: "current", label: "保存状态待确认" };
    row.state = publication ? "done" : "blocked";
    row.statusLabel = publication ? "结论已自动沉淀" : "结论已形成，保存状态待确认";
    row.nextAction = publication
      ? `查看完整结论、证据与适用限制；${publication.revision > 1 ? "已有修订历史" : "后续修订会保留历史"}`
      : "尚无本次知识保存回执；无需你审批候选或重填目标";
    if (execution?.knowledge_publication?.status === "unavailable"
      && execution.knowledge_publication.result_digest === researchResultDigestOf(execution)) {
      row.stages.asset = { state: "blocked", label: "笔记无法读取" };
      row.statusLabel = "已存笔记发生变动或无法读取";
      row.nextAction = "原结论保留；不会覆盖你的文件，无需审批候选或重填目标";
    }
  }

  const priority = { waiting: 0, blocked: 1, active: 2, candidate: 3, done: 4, untracked: 5 };
  return [...records.values()].sort((a, b) =>
    (priority[a.state] ?? 9) - (priority[b.state] ?? 9) ||
    String(b.updatedAt).localeCompare(String(a.updatedAt)) ||
    a.title.localeCompare(b.title, "zh-CN")
  );
}

// 自主认知闭环（TASK F）：认知态 → 旅程投影。系统故障/能力关闭/搜索耗尽/
// 持续观察一律不进「等我决定」；技术 ID 与内部 stop_reason 不上屏。
const COGNITIVE_JOURNEY = {
  synthesis_disabled: {
    stage: { state: "blocked", label: "判断受阻" }, state: "blocked",
    statusLabel: "综合判断能力未启用",
    nextAction: "已取得候选材料，原问题尚未回答；需要配置综合判断能力",
  },
  watch_budget_exhausted: {
    stage: { state: "blocked", label: "补查已停止" }, state: "blocked",
    statusLabel: "自动补查次数已用尽",
    nextAction: "原问题仍未完成，系统不会继续重试",
  },
  not_started: {
    stage: { state: "current", label: "尚未启动" },
    statusLabel: "系统核验尚未启动",
    nextAction: "等待系统启动核验",
  },
  capability_offline: {
    stage: { state: "blocked", label: "待恢复" }, state: "blocked",
    statusLabel: "系统待恢复",
    nextAction: "核验能力未开启，等待系统恢复，无需你操作",
  },
  researching: {
    stage: { state: "current", label: "研究中" },
    statusLabel: "系统正在研究",
    nextAction: "系统正在核对材料并形成判断",
  },
  collecting: {
    stage: { state: "current", label: "核验中" },
    statusLabel: "系统核验中",
    nextAction: "系统正在收集来源",
  },
  evidence_ready: {
    stage: { state: "current", label: "待核验" },
    statusLabel: "已取得来源",
    nextAction: "等待系统核验",
  },
  search_exhausted: {
    stage: { state: "blocked", label: "本轮已结束" }, state: "blocked",
    statusLabel: "已按策略搜索未果",
    nextAction: "本轮搜索已结束，尚未取得足够材料；没有已登记的自动复查",
  },
  awaiting_model_authorization: {
    stage: { state: "wait", label: "等你决定" },
    state: "waiting",
    statusLabel: "等我决定",
    nextAction: "确认是否授权模型核验",
  },
  synthesized: {
    stage: { state: "done", label: "已核验" },
  },
  watching: {
    stage: { state: "current", label: "持续观察" },
    statusLabel: "系统持续观察中",
    nextAction: "系统将按条件自动复查",
  },
  conflicted: {
    stage: { state: "blocked", label: "来源冲突" },
    state: "blocked",
    statusLabel: "来源存在冲突",
    nextAction: "核对来源冲突",
  },
};
// 旧 run 兼容投影（G12）：legacy stage/stop_reason → 认知态。

function localDay(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/* 星历活动路径排除前缀：运维/归档目录的批量导入副本按 birthtime/ctime 计入会
   淹没真实活动计数（8-20 实测单日 39 条中 32 条来自 _vault-ops/候选归档区）。
   排除后每天 30 条截断自然不再触顶。需要增减时改这份清单即可。 */
const ACTIVITY_PATH_EXCLUDES = ["_vault-ops/", "ops/", ".trash/"];

/* 只读元数据投影：星历按真实 Markdown 文件创建日展示活动，不读取正文，
   不建立第二套数据库。frontmatter 的 captured_at 优先于文件创建时间。 */
function collectHomepageActivity(app, now = new Date()) {
  const days = new Map();
  const files = app?.vault?.getMarkdownFiles?.() || [];
  const currentYear = now.getFullYear();
  const currentMonth = now.getMonth();
  for (const file of files) {
    // 排除运维/归档目录（_vault-ops/、ops/、.trash/）——批量导入副本不计入活动
    if (ACTIVITY_PATH_EXCLUDES.some((prefix) => String(file.path || "").startsWith(prefix))) continue;
    const frontmatter = app.metadataCache?.getFileCache?.(file)?.frontmatter || {};
    const stamp = frontmatter.captured_at || frontmatter.created_at || file.stat?.ctime;
    const date = new Date(stamp);
    if (!Number.isFinite(date.getTime()) || date.getFullYear() !== currentYear || date.getMonth() !== currentMonth) continue;
    const iso = localDay(date);
    const type = clean(frontmatter.type || frontmatter.kind).toLowerCase();
    const status = clean(frontmatter.status || frontmatter.state).toLowerCase();
    const path = clean(file.path);
    const kind = type.includes("碎片") || path.includes("碎片想法") ? "fragment"
      : type.includes("资产") || path.includes("已验证资产") ? "asset"
        : "document";
    const pending = ["pending", "ready", "waiting", "待处理", "待验证", "进行中"].some((token) => status.includes(token));
    const entry = {
      path,
      title: clean(frontmatter.title || file.basename || file.name),
      kind,
      pending,
      time: `${pad2(date.getHours())}:${pad2(date.getMinutes())}`,
    };
    if (!days.has(iso)) days.set(iso, []);
    if (days.get(iso).length < 30) days.get(iso).push(entry);
  }
  for (const entries of days.values()) entries.sort((a, b) => a.time.localeCompare(b.time) || a.title.localeCompare(b.title));
  return days;
}

function button(parent, text, cls, onClick) {
  const item = parent.createEl("button", { text, cls });
  item.setAttribute("type", "button");
  if (onClick) item.addEventListener("click", () => onClick(item));
  return item;
}

/* fix8：把文本里的文件路径（tests/xxx.py、xxx.md 等）渲染为可点链接（走 item.openNote，
   F-K13 守卫由 runAction 侧提供）；无法确认是路径的文本保持纯文本，不猜测。
   fix9：点击结果接既有 say 反馈（W6 故障区同款：非 success 才说），零反馈缺陷修复。 */
const PATH_LINK_RE = /[\w./-]+\.(?:md|py|js|ts|json|yaml|yml|txt|sh|csv|xml|html)\b/gi;
function appendTextWithPathLinks(el, text, onClickPath, say) {
  const matches = [];
  let m;
  while ((m = PATH_LINK_RE.exec(text)) !== null) matches.push(m);
  if (!matches.length) {
    el.setText(text);
    return;
  }
  let last = 0;
  for (const match of matches) {
    if (match.index > last) el.createSpan({ text: text.slice(last, match.index) });
    const link = el.createSpan({ cls: "life-cb-path-link", text: match[0] });
    link.setAttribute("data-cosmos-action", "item.openNote");
    /* R26 P2-1：内联文本流中的路径链接无法用原生 button（打断排版且样式文件
       不在本轮范围）——补 role/tabindex/Enter/Space 等价键盘路径。 */
    link.setAttribute("role", "link");
    link.setAttribute("tabindex", "0");
    const openLink = (ev) => {
      if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
      if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
      void Promise.resolve(onClickPath(match[0]))
        .then((result) => {
          if (say && result && result.kind !== "success") say(result);
        });
    };
    link.addEventListener("click", openLink);
    link.addEventListener("keydown", (ev) => {
      if (!ev || (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar")) return;
      openLink(ev);
    });
    last = match.index + match[0].length;
  }
  if (last < text.length) el.createSpan({ text: text.slice(last) });
}

function addPanelHead(panel, title, tag) {
  const head = panel.createDiv({ cls: "life-cosmos-panel-head" });
  head.createEl("h2", { text: title });
  head.createSpan({ text: tag });
}

/* ---------- 星空氛围：星云漂移 + JS 播种星野 ---------- */
function renderAtmosphere(cosmos) {
  const nebula = cosmos.createDiv({ cls: "life-cosmos-nebula" });
  nebula.createEl("i"); nebula.createEl("i"); nebula.createEl("i");
  const seed = (layer, count) => {
    const shadows = [];
    for (let i = 0; i < count; i += 1) {
      const x = (Math.random() * 100).toFixed(1);
      const y = (Math.random() * 100).toFixed(1);
      const big = Math.random() < 0.14;
      shadows.push(`${x}vw ${y}vh 0 ${big ? ".6px" : "0"} rgba(255,255,255,${(Math.random() * 0.5 + 0.15).toFixed(2)})`);
    }
    // box-shadow 只接受长度单位：用 vw/vh 铺满视野，由 cosmos 容器裁切。
    if (layer.style) layer.style.boxShadow = shadows.join(",");
  };
  const sf1 = cosmos.createDiv({ cls: "life-cosmos-stars" });
  const sf2 = cosmos.createDiv({ cls: "life-cosmos-stars is-twinkle" });
  seed(sf1, 110);
  seed(sf2, 70);
}

/* ---------- 刊头：渐变标题 + 口号轮番槽 ---------- */
function renderMasthead(hero, model) {
  const masthead = hero.createDiv({ cls: "life-cosmos-masthead" });
  const eyebrow = masthead.createEl("small", { text: "星空号 · 星空版" });
  eyebrow.createSpan({ cls: "life-cosmos-rule" });
  masthead.createEl("h1", { text: "碎片正在汇入可靠的轨道。" });

  const wrap = masthead.createDiv({ cls: "life-cosmos-slot" });
  wrap.createSpan({ cls: "life-cosmos-slot-label", text: "今晚" });
  const window_ = wrap.createDiv({ cls: "life-cosmos-slot-window" });
  const list = window_.createEl("ul");
  const lines = [
    ["01", "先拍板：", model.decisions.length ? `${model.decisions.length} 个决定一张一张清` : "当前没有待拍板项目"],
    ["02", "再专注：", `${model.focusTitle} · ${model.focusTime}`],
    ["03", "收尾：", model.fragments.length ? `${model.fragments.length} 条新碎片等待归位` : "轨道干净，没有未归档碎片"],
  ];
  const pendingLabels = [];
  // 末尾重复第一条，让 9s 滚动循环无缝。
  [...lines, lines[0]].forEach(([no, lead, tail]) => {
    const item = list.createEl("li");
    item.createSpan({ cls: "life-cosmos-slot-no", text: no });
    item.createSpan({ text: lead });
    const label = item.createSpan({ cls: "life-cosmos-slot-hl", text: tail });
    if (no === "01") pendingLabels.push(label);
  });
  return (count, incomplete) => pendingLabels.forEach(label => label.setText(incomplete
    ? `至少 ${count} 项待决定；部分队列尚未同步`
    : count ? `${count} 个决定一张一张清` : "当前没有待拍板项目"));
}

/* ---------- 深空天体档案（指标）：馆藏标签式读数 ---------- */
function renderArchive(hero, model) {
  const archive = hero.createDiv({ cls: "life-cosmos-archive" });
  archive.createSpan({ cls: "life-cosmos-archive-tag", text: "深空档案 · 已标注" });
  /* fix6：回归统一场景形态（fix5 的 2×2 分区撤掉），保留 F1 放大尺寸；
     天体与 caption 锚点按放大后尺寸重算（CSS 内），四象限互不压字。 */
  const entries = [
    ["galaxy", model.thoughts, `条思考`, `编号 NGC-${model.thoughts}`],
    ["planet", model.systems, `个主题`, `编号 TH-${pad2(model.systems)}`],
    ["cluster", model.todayProgress, `今日推进`, `编号 M-${pad2(model.todayProgress)}`],
    ["comet", model.decisions.length, `待决定`, `编号 C/${pad2(model.decisions.length)}`],
  ];
  let pendingValue;
  let pendingCode;
  for (const [kind, value, label, code] of entries) {
    const obj = archive.createDiv({ cls: `life-cosmos-obj is-${kind}` });
    obj.createEl("i", { cls: "body" });
    const cap = archive.createDiv({ cls: `life-cosmos-obj-cap is-${kind}` });
    const number = cap.createEl("b", { text: String(value) });
    cap.createSpan({ text: label });
    const identifier = cap.createEl("small", { text: code });
    if (kind === "comet") { pendingValue = number; pendingCode = identifier; }
  }
  /* 知识库规模：只读真实统计，不占 KPI、不提供伪动作。 */
  if (model.knowledgeCount > 0) {
    archive.createEl("small", {
      cls: "life-cosmos-archive-scale",
      text: `知识库规模 ${model.knowledgeCount} · 只读统计`,
    });
  }
  return (count, incomplete) => {
    pendingValue.setText(`${incomplete ? "≥" : ""}${count}`);
    pendingCode.setText(incomplete ? "部分队列尚未同步" : `编号 C/${pad2(count)}`);
  };
}

/* ---------- 任务舱：录入、笔记、刷新；技术入口集中在系统管理 ---------- */
function renderCapsule(cosmos, model, drawer, runAction, graphRefreshers = []) {
  const capsule = cosmos.createDiv({ cls: "life-cosmos-capsule rise d1" });
  const status = capsule.createEl("p", { cls: "life-cosmos-capsule-status", text: "" });
  status.setAttribute("aria-live", "polite");
  const say = (result) => {
    if (!result) return;
    status.setText(result.message || "");
    status.setAttribute("data-kind", result.kind);
  };

  const entries = [
    ["capture.quick", "＋ 记录碎片"],
    ["graph.refresh", "刷新"],
  ];
  for (const [actionId, label] of entries) {
    const item = button(capsule, label, "life-cosmos-capsule-action", async () => {
      if (item.getAttribute("disabled") !== null) return;
      item.setAttribute("disabled", "disabled");
      try {
        say(await runAction(actionId));
        // Graph 刷新后各主板就地重读 sources 并重绘（成功更新、失败保旧）。
        if (actionId === "graph.refresh") for (const refresh of graphRefreshers) refresh();
      } finally {
        item.removeAttribute("disabled");
      }
    });
    item.setAttribute("data-cosmos-action", actionId);
  }

  /* F-D1/F-D3/F-K5：say 暴露为页面级反馈出口，提示条与主板动作共用。 */
  capsule.say = say;
  return capsule;
}

/* ---------- 等你拍板：星尘卡堆 + 流星雨 + 极光帘 ---------- */
function renderMeteorShower(panel) {
  // 同一辐射点（右上）散开、同速、成簇：三条一组先后掠过。
  for (let index = 1; index <= 5; index += 1) {
    const wrap = panel.createSpan({ cls: `life-cosmos-mwrap is-m${index}${index === 3 || index === 5 ? " is-soft" : ""}` });
    wrap.createEl("i", { cls: "life-cosmos-meteor" });
  }
  panel.createSpan({ cls: "life-cosmos-aurora-band" });
}

function renderDeck(panel, items, reveal, extra = {}) {

  const count = items.length;
  cbPanelHead(panel, "需要你确认", `${count} 项待定`);
  const deck = panel.createDiv({ cls: "life-cosmos-deck" });
  const dots = panel.createDiv({ cls: "life-cosmos-deck-dots" });
  let visible = 0;
  const cards = [];
  const empty = deck.createDiv({ cls: "life-cosmos-deck-empty" });
  empty.createSpan({ cls: "life-cosmos-deck-star", text: "✦" });
  /* P1-2：空态文案区分「真清完」与「全部稍后」——strong 文本由 sync 动态更新。 */
  const emptyTitle = empty.createEl("strong", { text: items.length ? "今晚的决定清完了" : "当前没有待拍板项目" });
  const emptyNote = empty.createEl("small", { text: "当前队列为空" });

  /* F-D6：撤销入口先声明（sync 内引用其可见性/文案），再定义 sync。 */
  const undoSnooze = button(empty, "收回上一张", "life-cosmos-deck-undo", () => {
    if (visible > 0 && cards.length) { visible -= 1; sync(); }
  });
  undoSnooze.setAttribute("data-cosmos-action", "deck.undoSnooze");
  undoSnooze.setAttribute("type", "button");

  /* R1：授权卡一键成功后从卡堆移除并复算 N——liveItems 是可变副本，
      rebuild 重建卡区（保留稍后语义；授权移除是新的持久化结果）。 */
  let liveItems = [...items];
  const headerMeta = panel.querySelector(".life-cb-tag");
  const updateCount = () => {
    if (headerMeta && typeof headerMeta.setText === "function") {
      // R2 §4.7：已批准卡不计入待决 N；V2-C5：断线标注项也不计入。
      const pendingCount = liveItems.filter((item) => !(item && item.approved) && !(item && item.kind === "offline")).length;
      // V2-C5：数据时间戳（陈旧可见——用户能判断 N 的新旧，对齐 Graph 摘要时间模式）。
      const syncAt = extra.syncAt && typeof extra.syncAt === "function" ? extra.syncAt() : null;
      const syncText = syncAt ? ` · 最近同步 ${formatGraphTime(syncAt)}` : "";
      // R25 P1-5：任一来源 error/partial → 数量为未知下界（≥N），不渲染确定值。
      const incomplete = typeof extra.queueIncomplete === "function" && extra.queueIncomplete();
      const countText = incomplete ? `≥${pendingCount} 项待定（有来源未完整同步）` : `${pendingCount} 项待定`;
      headerMeta.setText(`${countText}${syncText}`);
      extra.onCountChanged?.(pendingCount, incomplete);
      if (incomplete && pendingCount === 0) {
        emptyTitle.setText("待决定队列尚未完整同步");
        emptyNote.setText("不能据此认定没有待决定事项");
      }
    }
  };
  const rebuild = () => {
    deck.empty();
    deck.appendChild(empty);
    cards.length = 0;
    visible = Math.min(visible, liveItems.length);
    for (const item of liveItems) buildCard(item);
    sync();
  };

  const sync = () => {
    cards.forEach((card, index) => {
      const offset = index - visible;
      card.toggleClass("is-past", offset < 0);
      card.setAttribute("data-depth", String(Math.max(0, Math.min(2, offset))));
    });
    dots.empty();
    for (let index = visible; index < cards.length; index += 1) {
      dots.createSpan({ cls: index === visible ? "is-active" : "" });
    }
    empty.toggleClass("is-show", visible >= cards.length);
    if (liveItems.length) {
      undoSnooze.toggleClass("is-show", visible > 0);
      undoSnooze.setText(visible >= cards.length && cards.length
        ? `收回上一张（全部 ${cards.length} 张已稍后）`
        : `收回上一张（已稍后 ${visible} 张）`);
      emptyTitle.setText(visible >= cards.length && cards.length
        ? "全部已稍后 · 并未处理完"
        : "今晚的决定清完了");
      emptyNote.setText(visible >= cards.length && cards.length ? "尚未完成处理" : "当前队列为空");
    } else {
      undoSnooze.toggleClass("is-show", false);
    }
  };

  const buildCard = (item) => {
    const card = deck.createEl("article", { cls: "life-cosmos-deck-card" });
    card.createSpan({ cls: "life-cosmos-moon" });
    if (item.kind === "offline") {
      /* V2-C5 断线标注行（不参与卡堆计数/稍后语义）；R25 P1-5：partial 标注
         复用同一位置（hint 区分整体断线与部分失败）。 */
      card.addClass("is-offline");
      card.createEl("small", { cls: "life-cosmos-card-kind is-offline", text: `[${item.sourceLabel}] · ${item.hint || "服务暂时不可用"}` });
      card.createEl("h3", { text: item.title });
      card.createEl("p", { text: item.meta });
      return;
    }
    if (item.kind === "auth") {
      /* R1 授权卡：来源标签（[Graph]/[Loop]/[Intent] 复用胶囊语言）即卡堆语义扩展说明。
         R2：已批准卡标签区分（等待授权 → 已授权）。 */
      card.createEl("small", { cls: `life-cosmos-card-kind is-auth life-cosmos-auth-${item.sourceLabel.toLowerCase()}${item.approved ? " is-approved" : ""}`, text: `[${item.sourceLabel}] · ${item.approved ? "已授权" : "等待授权"}` });
      card.createEl("h3", { text: item.title });
      card.createEl("p", { text: item.meta });
      const actions = card.createDiv({ cls: "life-cosmos-card-actions" });
      button(actions, "稍后", "is-secondary", () => { visible += 1; sync(); });
      button(actions, "去拍板 →", "is-primary", (trigger) => reveal(item.element, trigger));
      cards.push(card);
      return;
    }
    card.createEl("small", { cls: "life-cosmos-card-kind", text: "处理方向 · 等你决定" });
    card.createEl("h3", { text: item.title });
    card.createEl("p", { text: item.meta });
    const actions = card.createDiv({ cls: "life-cosmos-card-actions" });
    button(actions, "稍后", "is-secondary", () => { visible += 1; sync(); });
    button(actions, "去拍板 →", "is-primary", (trigger) => reveal(item.element, trigger));
    cards.push(card);
  };

  /* R1：授权卡构建入口——renderDeck 内 buildCard 用于初始渲染；
     rebuild 用于一键成功后重渲染（保留未授权卡）。 */
  for (const item of liveItems) buildCard(item);
  sync();
  // V2-C5：初始即写入时间戳（若已有 syncAt），不等到授权操作后才可见。
  updateCount();
  panel.createEl("small", {
    cls: "life-cosmos-deck-hint",
    text: "「稍后」只调整本次会话的卡堆顺序（可收回），重载后恢复，不写入任何状态。",
  });

  /* R2：onAuthChanged 支持两种变更——移卡（item 指定）与整体刷新（重派生
     liveItems；过期卡恢复有效 / 漂移消除后按钮重新可用）。 */
  if (extra && typeof extra.onAuthChanged === "function") {
    extra.onAuthChanged((item, opts) => {
      if (opts && opts.refresh) {
        if (extra.refreshItems) {
          liveItems = extra.refreshItems();
          rebuild();
          updateCount();
        }
        return;
      }
      const index = liveItems.indexOf(item);
      // R2 §4.7：批准成功后先重派生（已批准卡出现 + 待决变化 + N 复算）。
      if (extra.refreshItems) {
        liveItems = extra.refreshItems();
      } else if (index >= 0) {
        liveItems.splice(index, 1);
      }
      rebuild();
      updateCount();
    });
  }
}

/* ---------- 发射任务控制：倒计时 + GO/NO-GO + 火箭 ---------- */
function buildRocket(pad) {
  pad.createEl("i", { cls: "rail" });
  pad.createEl("i", { cls: "tower" });
  pad.createEl("i", { cls: "arm" });
  const rocket = pad.createDiv({ cls: "life-cosmos-rocket" });
  for (const part of ["rk-nose", "rk-body", "rk-win", "rk-fin l", "rk-fin r", "rk-nozzle", "flame"]) {
    rocket.createEl("i", { cls: part });
  }
  pad.createEl("i", { cls: "deck" });
  pad.createEl("i", { cls: "trench" });
  pad.createEl("i", { cls: "ground" });
  return rocket;
}

function renderFocus(panel, model, reveal, hasLayout, registerTimer, runAction) {
  addPanelHead(panel, "发射任务控制", `任务控制 · ${model.pendingCount} 项待办 · 就绪检查`);
  const stage = panel.createDiv({ cls: "life-cosmos-mission" });
  const pad = stage.createDiv({ cls: "life-cosmos-pad" });
  const rocket = buildRocket(pad);

  const body = stage.createDiv({ cls: "life-cosmos-mission-body" });
  const top = body.createDiv({ cls: "life-cosmos-mission-time" });
  const clock = top.createEl("b");
  const mm = clock.createSpan({ cls: "mm" });
  const colon = clock.createSpan({ cls: "colon", text: ":" });
  const ss = clock.createSpan({ cls: "ss" });
  top.createSpan({ text: "专注倒计时" });
  body.createEl("h3", { text: `${model.focusTitle} · 今晚的发射窗口` });

  /* 倒计时：冒号随秒跳动——每秒数字变更时亮起，约半秒后转暗 */
  let remain = parseClockSeconds(model.focusTime);
  const renderTime = () => {
    mm.setText(pad2(Math.floor(remain / 60)));
    ss.setText(pad2(remain % 60));
    colon.removeClass("dim");
    if (hasLayout && typeof window !== "undefined") {
      registerTimer(window.setTimeout(() => colon.addClass("dim"), 480), "timeout");
    }
  };
  if (remain === null) {
    mm.setText(model.focusTime);
    ss.setText("");
    colon.addClass("dim");
  } else {
    renderTime();
    if (model.focusRunning && hasLayout && typeof window !== "undefined") {
      registerTimer(window.setInterval(() => {
        if (remain > 0) { remain -= 1; renderTime(); }
      }, 1000), "interval");
    }
  }

  /* GO / NO-GO：只有完整 data-complete-task + data-task-line 绑定时才调用
     既有 completeTask 并显示真实结果（失败/冲突不打勾）；缺绑定的行明确
     降级为「本次会话选择」——会话内视觉状态，重载即恢复，绝不伪装成持久
     完成，也不加删除线式持久暗示。 */
  const list = body.createDiv({ cls: "life-cosmos-go-list" });
  /* D11：横幅随状态切换——全部完成才「允许升空」，否则中性就绪文案 */
  const banner = body.createDiv({ cls: "life-cosmos-banner", text: "待全部完成 · 就绪检查" });
  const tasks = model.tasks.slice(0, 3);
  const syncPad = () => {
    const rows = [...list.querySelectorAll(".life-cosmos-go-item")];
    const done = rows.filter((row) => row.getAttribute("data-done") === "1").length;
    const all = rows.length > 0 && done === rows.length;
    rocket.toggleClass("launched", all);
    banner.toggleClass("go", all);
    banner.setText(all ? "全部就绪 · 允许升空" : `待全部完成 · ${done}/${rows.length} 就绪`);
    if (!all && rocket.style) rocket.style.bottom = `${26 + done * 50}px`;
    if (all && rocket.style) rocket.style.bottom = "";
  };
  if (!tasks.length) {
    const row = list.createDiv({ cls: "life-cosmos-go-item is-static" });
    row.createSpan({ cls: "life-cosmos-led" });
    row.createSpan({ cls: "nm", text: "今天没有待处理任务" });
    row.createSpan({ cls: "st", text: "已清空" });
  }
  tasks.forEach((task) => {
    if (task.binding) {
      const row = list.createDiv({ cls: "life-cosmos-go-item is-bound" });
      row.setAttribute("data-done", "0");
      row.createSpan({ cls: "life-cosmos-led" });
      row.createSpan({ cls: "nm", text: task.title });
      const state = row.createSpan({ cls: "st", text: "未就绪" });
      const note = row.createSpan({ cls: "life-cosmos-go-note", text: "" });
      const complete = button(row, "完成", "life-cosmos-go-complete", async () => {
        if (complete.getAttribute("disabled") !== null) return;
        complete.setAttribute("disabled", "disabled");
        try {
          const result = await runAction("item.complete", task.binding.path, task.binding.line, task.title);
          if (result.kind === "success") {
            row.setAttribute("data-done", "1");
            row.addClass("is-done");
            state.setText("已完成");
            note.setText("");
            rocket.addClass("firing");
            if (hasLayout && typeof window !== "undefined") {
              registerTimer(window.setTimeout(() => rocket.removeClass("firing"), 1000), "timeout");
            }
            syncPad();
          } else {
            note.setText(result.message || "完成失败，请稍后重试。");
            note.setAttribute("data-kind", result.kind);
          }
        } finally {
          complete.removeAttribute("disabled");
        }
      });
      complete.setAttribute("data-cosmos-action", "item.complete");
      row.addEventListener("dblclick", () => reveal(task.element));
      return;
    }
    const row = button(list, "", "life-cosmos-go-item is-session", () => {
      const done = row.getAttribute("data-done") === "1";
      row.setAttribute("data-done", done ? "0" : "1");
      row.toggleClass("is-done", !done);
      row.querySelector(".st")?.setText(done ? "未就绪" : "会话选择");
      rocket.addClass("firing");
      if (hasLayout && typeof window !== "undefined") {
        registerTimer(window.setTimeout(() => rocket.removeClass("firing"), 1000), "timeout");
      }
      syncPad();
    });
    row.setAttribute("data-done", "0");
    row.setAttribute("data-session-choice", "1");
    row.setAttribute("aria-label", `${task.title}（本次会话选择，重载后恢复）`);
    row.addEventListener("dblclick", () => reveal(task.element));
    row.createSpan({ cls: "life-cosmos-led" });
    row.createSpan({ cls: "nm", text: task.title });
    row.createSpan({ cls: "st", text: "未就绪" });
  });
  if (tasks.some((task) => !task.binding)) {
    body.createEl("small", {
      cls: "life-cosmos-go-hint",
      text: "无来源绑定的行只是「本次会话选择」，重载后恢复，不会写入任何状态。",
    });
  }
  syncPad();
}

/* ---------- 今日待办面板（Round3）：完整列表第一屏可见 ---------- */
/* 与发射任务控制/EVA 同一套完成协议：完整 binding 才调 item.compose 的
   item.complete 真实写；缺绑定降级为会话选择并明示。不新造第二套语义。 */
function renderTodayTodos(overview, model, reveal, hasLayout, registerTimer, runAction, say) {
  const panel = overview.createEl("section", {
    cls: "life-cosmos-panel life-cosmos-todos rise d3",
  });
  addPanelHead(panel, "今日待办", `今日 · ${model.tasks.length} 项 · 完整列表`);
  /* W6-2 第三分区：看门狗故障（故障比逾期更急——置于提醒分区之上）；
     空态也常显（nigo：要有个地方可以看到；有单时保持列表+一键修复逻辑）。 */
  {
    const faults = panel.createDiv({ cls: "life-cosmos-todo-faults" });
    if (model.faults && model.faults.length) {
      faults.createSpan({ cls: "life-cosmos-todo-faults-head", text: `看门狗 · ${model.faults.length} 张待处理` });
      for (const fault of model.faults) {
        const row = faults.createDiv({ cls: "life-cosmos-todo-fault-row" });
        row.createSpan({ cls: "life-cosmos-todo-fault-title", text: fault.title });
        row.createEl("small", { text: fault.meta || "" });
        if (fault.path) {
          const open = button(row, "打开", "life-cosmos-todo-fault-open", async () => {
            const result = await runAction("item.openNote", fault.path);
            if (say && result.kind !== "success") say(result);
          });
          open.setAttribute("data-cosmos-action", "item.openNote");
          // n8n-repair：fault_type 白名单（文件名含 n8n_scheduler_stalled / n8n_down）
          // 渲染「一键修复」——点 → repair.request 写请求文件，看门狗下轮执行。
          const repairType = /(n8n_scheduler_stalled|n8n_down)/.test(fault.path || "");
          if (repairType) {
            const repair = button(row, "一键修复", "life-cosmos-todo-fault-repair", async (target) => {
              if (target.getAttribute("disabled") !== null) return;
              target.setAttribute("disabled", "disabled");
              target.setText("提交中…");
              const result = await runAction("repair.request", { target: "n8n" });
              // R25 P1-7：只有 success 才「已提交」；error/conflict 恢复可重试。
              if (result && result.kind === "success") {
                target.setText("已提交");
              } else {
                target.removeAttribute("disabled");
                target.setText("一键修复");
              }
              if (say) say(result);
            });
            repair.setAttribute("data-cosmos-action", "repair.request");
          }
        } else {
          row.createEl("small", { text: "暂无可打开文档" });
        }
      }
    } else {
      faults.createSpan({ cls: "life-cosmos-todo-faults-head", text: "看门狗 · 无待处理故障" });
    }
  }
  /* P2b 分区一：逾期/盲区弱化提示行（原独立提醒条并入；语义零改动） */
  if (model.overdue.length || model.blindspots.length) {
    const notice = panel.createDiv({ cls: "life-cosmos-todo-notice" });
    for (const item of model.overdue) {
      notice.createSpan({ cls: "life-cosmos-todo-notice-item", text: `逾期：${item.title}` });
    }
    for (const item of model.blindspots) {
      notice.createSpan({ cls: "life-cosmos-todo-notice-item", text: `${item.title}` });
    }
  }
  /* P2b 分区二：盲区动作行（setFilter/展开工作台按钮原样迁移，行为不变） */
  if (model.blindspots.some((item) => item.filter || item.scrollTarget)) {
    const actRow = panel.createDiv({ cls: "life-cosmos-todo-notice-actions" });
    for (const item of model.blindspots) {
      if (item.filter) {
        const act = button(actRow, item.actionLabel || "只看", "life-cosmos-notice-act", async () => {
          const result = await runAction("view.setFilter", item.filter);
          const affected = applyCosmosFilter(overview, item.filter);
          if (say) {
            const filterName = item.filter === "unread" ? "未读" : item.filter === "watched" ? "关注" : "全部";
            say({
              kind: result.kind === "success" ? "success" : result.kind,
              message: result.message || (result.kind === "success"
                ? (affected > 0
                  ? `已切换筛选：${filterName}（持久保存；本页 ${affected} 条信号已同步过滤）`
                  : `已切换筛选：${filterName}（持久保存；本页暂无可过滤的信号条目）`)
                : "筛选未能更新"),
            });
          }
        });
        act.setAttribute("data-cosmos-action", "view.setFilter");
      } else if (item.scrollTarget && item.element) {
        button(actRow, item.actionLabel || "查看", "life-cosmos-notice-act", () => item.element?.scrollIntoView?.({ behavior: "smooth", block: "center" }));
      }
    }
  }
  const list = panel.createDiv({ cls: "life-cosmos-go-list" });
  if (!model.tasks.length) {
    const row = list.createDiv({ cls: "life-cosmos-go-item is-static" });
    row.createSpan({ cls: "life-cosmos-led" });
    row.createSpan({ cls: "nm", text: "今天没有待处理任务" });
    row.createSpan({ cls: "st", text: "已清空" });
    return;
  }
  model.tasks.forEach((task) => {
    let row;
    if (task.binding) {
      row = list.createDiv({ cls: "life-cosmos-go-item is-bound" });
      row.setAttribute("data-done", "0");
      row.createSpan({ cls: "life-cosmos-led" });
      row.createSpan({ cls: "nm", text: task.title });
      const state = row.createSpan({ cls: "st", text: "未就绪" });
      const note = row.createSpan({ cls: "life-cosmos-go-note", text: "" });
      const complete = button(row, "完成", "life-cosmos-go-complete", async () => {
        if (complete.getAttribute("disabled") !== null) return;
        complete.setAttribute("disabled", "disabled");
        try {
          const result = await runAction("item.complete", task.binding.path, task.binding.line, task.title);
          if (result.kind === "success") {
            row.setAttribute("data-done", "1");
            row.addClass("is-done");
            state.setText("已完成");
            note.setText("");
          } else {
            note.setText(result.message || "完成失败，请稍后重试。");
            note.setAttribute("data-kind", result.kind);
          }
        } finally {
          complete.removeAttribute("disabled");
        }
      });
      complete.setAttribute("data-cosmos-action", "item.complete");
    } else {
      row = button(list, "", "life-cosmos-go-item is-session", () => {
        const done = row.getAttribute("data-done") === "1";
        row.setAttribute("data-done", done ? "0" : "1");
        row.toggleClass("is-done", !done);
        row.querySelector(".st")?.setText(done ? "未就绪" : "会话选择");
      });
      row.setAttribute("data-done", "0");
      row.setAttribute("data-session-choice", "1");
      row.setAttribute("aria-label", `${task.title}（本次会话选择，重载后恢复）`);
      row.createSpan({ cls: "life-cosmos-led" });
      row.createSpan({ cls: "nm", text: task.title });
      row.createSpan({ cls: "st", text: "未就绪" });
    }
    /* 来源链接：有 path 时提供「打开来源」，复用既有 openNote 协议与 say 反馈 */
    if (task.path) {
      const open = button(row, "来源", "life-cosmos-todo-open", async () => {
        const result = await runAction("item.openNote", task.path);
        if (say && result.kind !== "success") say(result);
      });
      open.setAttribute("data-cosmos-action", "item.openNote");
      open.setAttribute("aria-label", `打开来源：${task.title}`);
    }
    row.addEventListener("dblclick", () => reveal(task.element));
  });
  if (model.tasks.some((task) => !task.binding)) {
    panel.createEl("small", {
      cls: "life-cosmos-go-hint",
      text: "无来源绑定的行只是「本次会话选择」，重载后恢复，不会写入任何状态。",
    });
  }
}

/* ---------- 碎星带：无缝漂移 + 点击锁定 + 取景框浮层 ---------- */
function renderFragments(panel, model, reveal) {
  addPanelHead(panel, "碎星带", `碎星带 · ${model.fragments.length} 块`);
  const wrap = panel.createDiv({ cls: "life-cosmos-belt-wrap" });
  const belt = wrap.createDiv({ cls: "life-cosmos-belt" });
  const entries = model.fragments.length ? model.fragments : [];
  if (!entries.length) {
    const row = wrap.createDiv({ cls: "life-cosmos-belt-empty" });
    row.createEl("strong", { text: "今天还没有新碎片" });
    row.createEl("small", { text: "等待新的输入进入轨道" });
  }
  // 首尾相接漂移：内容渲染两遍，轨道平移 -50% 即无缝。
  for (let repeat = 0; repeat < 2 && entries.length; repeat += 1) {
    entries.forEach((item, index) => {
      const tone = index % 5;
      const rock = button(belt, "", `life-cosmos-rock is-tone-${tone} is-sz-${(index % 5) + 1} is-bob-${(index % 5) + 1}${index % 2 === 0 ? " is-ringed" : ""}`);
      rock.setAttribute("aria-label", item.title);
      rock.setAttribute("data-frag", pad2(index + 1));
      rock.createEl("i", { cls: "life-cosmos-rock-body" });
      rock.createSpan({ cls: "life-cosmos-rock-name", text: item.title });
      rock.createSpan({ cls: "life-cosmos-rock-ts", text: item.time || "--:--" });
      rock.addEventListener("dblclick", () => reveal(item.element));
      /* R26 P2-1 / R27 P1-4：详情入口的键盘等价路径——Enter 与 Space 同一两
         阶段（未锁定先锁定、已锁定打开详情，等价 dblclick）；preventDefault
         抑制原生 button 在 keydown/keyup 合成的 click，防止 belt 委托把
         第二次按键反向解释为关闭。 */
      rock.addEventListener("keydown", (ev) => {
        if (!ev || (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar")) return;
        ev.preventDefault?.();
        if (locked === rock) reveal(item.element);
        else openPop(rock);
      });
    });
  }

  const pop = panel.createDiv({ cls: "life-cosmos-rock-pop" });
  pop.createEl("i", { cls: "rp-stem" });
  const popTitle = pop.createEl("b");
  const popMeta = pop.createSpan({ cls: "rp-meta" });
  const popHint = pop.createSpan({ cls: "rp-hint" });
  pop.createSpan({ cls: "rp-session", text: "视觉锁定 · 仅本次会话，重载后恢复" });
  let locked = null;

  const closePop = () => {
    pop.removeClass("show");
    belt.removeClass("held");
    if (locked) { locked.removeClass("locked"); locked = null; }
  };
  const openPop = (rock) => {
    if (locked) locked.removeClass("locked");
    locked = rock;
    rock.addClass("locked");
    belt.addClass("held");
    popTitle.setText(rock.querySelector(".life-cosmos-rock-name")?.textContent || "");
    popMeta.setText(`碎片-${rock.getAttribute("data-frag")} · ${rock.querySelector(".life-cosmos-rock-ts")?.textContent || "--:--"} · 主带`);
    const source = entries[Number(rock.getAttribute("data-frag")) - 1];
    popHint.setText(source?.meta || "双击（或锁定后 Enter/Space）打开来源");
    /* 定位：弹到星球上方、避开面板标题；瞄准线横向对准星球中心 */
    if (typeof panel.getBoundingClientRect === "function" && typeof rock.getBoundingClientRect === "function") {
      const pr = panel.getBoundingClientRect();
      const rr = rock.getBoundingClientRect();
      const popH = pop.offsetHeight || 120;
      const popW = 236;
      let x = rr.left + rr.width / 2 - pr.left - popW / 2;
      x = Math.max(10, Math.min(x, pr.width - popW - 10));
      let y = rr.top - pr.top - popH - 30;
      let below = false;
      if (y < 46) { y = rr.bottom - pr.top + 14; below = true; }
      pop.toggleClass("below", below);
      if (pop.style) {
        pop.style.left = `${x}px`;
        pop.style.top = `${y}px`;
        const gap = below ? 14 : Math.max(10, rr.top - pr.top - y - popH - 4);
        const stem = pop.querySelector(".rp-stem");
        const stemX = rr.left + rr.width / 2 - pr.left - x;
        if (stem?.style) {
          stem.style.left = `${Math.max(14, Math.min(stemX, popW - 14))}px`;
          stem.style.height = `${gap}px`;
          if (below) { stem.style.bottom = "auto"; stem.style.top = `${-gap}px`; }
          else { stem.style.top = "auto"; stem.style.bottom = `${-gap}px`; }
        }
      }
    }
    pop.addClass("show");
  };
  belt.addEventListener("click", (event) => {
    const rock = event.target?.closest?.(".life-cosmos-rock");
    if (!rock) return;
    if (locked === rock) closePop();
    else openPop(rock);
  });
  panel.addEventListener("click", (event) => {
    if (!event.target?.closest?.(".life-cosmos-rock")) closePop();
  });

  const foot = panel.createDiv({ cls: "life-cosmos-film-foot" });
  foot.createSpan({ text: "主带 · 真实碎片" });
  foot.createSpan({ text: "点击碎片锁定" });
}

/* ================================================================
   v13 板块群：碎片 / 专注 / 资产 / 研究
   视觉忠实移植自 thought-map-r3/v13-cosmos-boards.html，数据全部来自
   真实主页 DOM 投影（buildCosmosModel），会话内视觉状态不落业务状态。
   ================================================================ */

function cbBoardHead(board, eyebrow, headline, statValue, statLabel) {
  const head = board.createDiv({ cls: "life-cb-head" });
  const left = head.createDiv();
  left.createDiv({ cls: "life-cb-eyebrow", text: eyebrow });
  left.createEl("h1", { text: headline });
  const stat = head.createDiv({ cls: "life-cb-stat" });
  stat.createEl("b", { text: String(statValue) });
  stat.createSpan({ text: statLabel });
}

function cbPanelHead(panel, title, tag) {
  const head = panel.createDiv({ cls: "life-cb-p-head" });
  head.createEl("h2", { text: title });
  head.createSpan({ cls: "life-cb-tag", text: tag });
}

function cbEmpty(list, text) {
  const row = list.createDiv({ cls: "life-cb-empty" });
  row.createSpan({ text });
}

/* ---------- 板块：碎片 · 星际精炼航线 REFINERY RUN ---------- */
const REFINERY_STAGES = [
  ["采集带", "采集带 · 信息进入", "采集 · 信息进入"],
  ["精炼站", "精炼站 · 整理指引", "整理 · 指引"],
  ["试航场", "试航场 · 实践反馈", "实践 · 反馈"],
  ["恒星铸造", "恒星铸造 · 已验证资产", "资产 · 已验证 →"],
];
const ORE_STATUS_TEXT = { q: "排队", r: "已提取", e: "重试" };

function renderRefineryKanban(board, model, reveal, runAction, say) {
  const counts = REFINERY_STAGES.map((_, index) => model.pipeline[index]?.count ?? 0);
  const inTransit = counts.slice(0, 3).reduce((sum, value) => sum + value, 0);
  cbBoardHead(board, "精炼运行 · 碎片闭环", "每条碎片，都在航线上。", inTransit, "流转中");

  /* fix8：光点运动范围跟有货航段绑定——从第一个有货段巡航到最后一个有货段；
     只单段有货时 from==to（驻留该段）；全空由 is-active 门控停驻 */
  const liveIdx = counts.map((count, index) => (count > 0 ? index : -1)).filter((index) => index >= 0);
  const route = board.createDiv({ cls: `life-cb-route${inTransit > 0 ? " is-active" : ""}` });
  if (liveIdx.length > 0) {
    const fromPct = ((liveIdx[0] + 0.5) / REFINERY_STAGES.length) * 100;
    const toPct = ((liveIdx[liveIdx.length - 1] + 0.5) / REFINERY_STAGES.length) * 100;
    route.style.setProperty("--convoy-from", `${fromPct}%`);
    route.style.setProperty("--convoy-to", `${toPct}%`);
  }
  route.createSpan({ cls: "life-cb-convoy" });
  REFINERY_STAGES.forEach(([name], index) => {
    const count = counts[index];
    const wp = route.createDiv({ cls: `life-cb-waypoint${count > 0 ? " live" : ""}` });
    wp.createSpan({ cls: "life-cb-wp-dot", text: pad2(index + 1) });
    wp.createSpan({ cls: "life-cb-wp-name", text: name });
    const label = model.pipeline[index]?.label || ["信息进入", "整理与指引", "实践反馈", "已验证资产"][index];
    const wpCount = wp.createSpan({ cls: "life-cb-wp-count", text: `${label} ` });
    wpCount.createEl("b", { text: String(count) });
  });

  const grid = board.createDiv({ cls: "life-cb-grid" });

  /* 舱 1：原始输入（矿石） */
  const orePanel = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-stage" });
  cbPanelHead(orePanel, "原始输入", REFINERY_STAGES[0][1]);
  const oreList = orePanel.createDiv({ cls: "life-cb-stage-list" });
  if (!model.captures.length) cbEmpty(oreList, "今天还没有新碎片进入轨道");
  model.captures.forEach((item, index) => {
    const ore = button(oreList, "", "life-cb-ore", () => reveal(item.element));
    const size = 22 - (index % 4) * 2;
    const body = ore.createEl("i", { cls: "life-cb-ore-body" });
    if (body.style) { body.style.width = `${size}px`; body.style.height = `${size}px`; }
    const txt = ore.createDiv({ cls: "life-cb-ore-txt" });
    txt.createEl("b", { text: item.title });
    txt.createEl("small", { text: [item.time, item.kind].filter(Boolean).join(" · ") || "原始记录" });
    /* 微修：排队态带入队时间（item.time 已有；无 time 诚实仅「排队」） */
    const queueTime = item.tone === "q" && item.time ? ` · ${item.time} 入队` : "";
    const retryNote = item.tone === "e" && item.time ? ` · ${item.time}` : "";
    ore.createSpan({ cls: `life-cb-ore-st ${item.tone}`, text: (ORE_STATUS_TEXT[item.tone] || "排队") + queueTime + retryNote });
  });
  orePanel.createDiv({ cls: "life-cb-stage-foot", text: REFINERY_STAGES[0][2] });

  /* 舱 2：整理指引 */
  const guidePanel = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-stage" });
  cbPanelHead(guidePanel, "整理指引", REFINERY_STAGES[1][1]);
  const guideList = guidePanel.createDiv({ cls: "life-cb-stage-list" });
  if (!model.organized.length) cbEmpty(guideList, "没有待验证的整理结果");
  model.organized.forEach((item) => {
    const guide = button(guideList, "", "life-cb-guide", () => reveal(item.element));
    guide.createEl("b", { text: item.title });
    const goalRow = guide.createDiv({ cls: "life-cb-g-goal" });
    appendTextWithPathLinks(goalRow, `目标：${item.goal || "明确这条信息是否值得继续验证"}`, (path) => runAction("item.openNote", path), say);
    const nextRow = guide.createDiv({ cls: "life-cb-g-next" });
    appendTextWithPathLinks(nextRow, `下一步：${item.next || "补充一个最小可验证步骤"}`, (path) => runAction("item.openNote", path), say);
  });
  guidePanel.createDiv({ cls: "life-cb-stage-foot", text: REFINERY_STAGES[1][2] });

  /* 舱 3：实践反馈 */
  const logPanel = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-stage" });
  cbPanelHead(logPanel, "实践反馈", REFINERY_STAGES[2][1]);
  const logList = logPanel.createDiv({ cls: "life-cb-stage-list" });
  if (!model.feedback.length) cbEmpty(logList, "尚无进行中或已完成的实践");
  model.feedback.forEach((item) => {
    const log = button(logList, "", `life-cb-log ${item.tone}`, () => reveal(item.element));
    log.createEl("i", { cls: "life-cb-log-sig" });
    const txt = log.createDiv({ cls: "life-cb-log-txt" });
    txt.createEl("b", { text: item.title });
    txt.createEl("small", { text: [item.label, item.body].filter(Boolean).join(" · ") });
  });
  logPanel.createDiv({ cls: "life-cb-stage-foot", text: REFINERY_STAGES[2][2] });

  /* 舱 4：恒星铸造（最新资产） */
  const forgePanel = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-stage" });
  cbPanelHead(forgePanel, "恒星铸造", REFINERY_STAGES[3][1]);
  const forge = forgePanel.createDiv({ cls: "life-cb-forge" });
  const latest = model.assets[0];
  forge.createEl("i", { cls: `life-cb-forge-star${latest ? "" : " is-dim"}` });
  forge.createEl("b", { text: latest?.title || "尚未铸造恒星" });
  forge.createEl("small", { text: latest ? `已铸造 · ${latest.date || "日期待补"}` : "真实验证通过后在此点亮" });
  const forgeFoot = button(forgePanel, REFINERY_STAGES[3][2], "life-cb-stage-foot is-link", () => reveal(latest?.element));
  forgeFoot.setAttribute("type", "button");
  /* F-D7：无资产时禁用脚注，消除假 affordance。 */
  if (!latest) forgeFoot.setAttribute("disabled", "disabled");
}

function renderFragmentPanorama(host, model, sources, reveal, runAction, say, showIntent, viewState = {}) {
  const journeys = buildFragmentJourneys(model, sources);
  const stateCounts = Object.fromEntries(
    ["active", "waiting", "blocked", "candidate", "done", "untracked"].map((state) => [
      state, journeys.filter((item) => item.state === state).length,
    ])
  );
  cbBoardHead(host, "研究记录", "每条问题，进展与结果。", journeys.length, "条记录");

  const metrics = host.createDiv({ cls: "life-frag-panorama-metrics" });
  const metricDefs = [
    ["all", "全部碎片", journeys.length], ["active", "进行中", stateCounts.active],
    ["waiting", "等我决定", stateCounts.waiting], ["blocked", "已阻塞", stateCounts.blocked],
    ["candidate", "候选结论", stateCounts.candidate], ["done", "已完成", stateCounts.done],
    ["untracked", "未接管/待核实", stateCounts.untracked],
  ];
  const metricButtons = metricDefs.map(([filter, label, count]) => {
    const item = button(metrics, "", `life-frag-panorama-metric is-${filter}`, () => applyFilter(filter));
    item.setAttribute("data-fragment-filter", filter);
    item.createSpan({ text: label });
    item.createEl("b", { text: String(count) });
    return item;
  });

  const toolbar = host.createDiv({ cls: "life-frag-panorama-toolbar" });
  const search = viewState.search || toolbar.createEl("input", { cls: "life-frag-panorama-search" });
  if (viewState.search) toolbar.appendChild(search);
  search.setAttribute("type", "search");
  search.setAttribute("placeholder", "搜索碎片标题…");
  search.setAttribute("aria-label", "搜索碎片标题");
  search.value = viewState.query || "";
  toolbar.createSpan({ cls: "life-frag-panorama-hint", text: "先看需要处理的记录；点开查看结论、证据与限制。" });

  const table = host.createDiv({ cls: "life-frag-panorama life-research-list" });
  const rowsHost = table.createDiv({ cls: "life-frag-panorama-rows" });
  const rowEntries = journeys.map((journey) => {
    const open = async () => {
      if (journey.alignment && typeof showIntent === "function") showIntent(journey.alignment, journey.element, row);
      else if (journey.element) reveal(journey.element);
      else if (journey.review) say(await runAction("review.open", journey.review));
      else say({ kind: "error", message: "这条碎片暂时没有绑定可打开的详情卡。" });
    };
    const row = button(rowsHost, "", `life-frag-panorama-row life-research-row is-${journey.state}`, open);
    row.setAttribute("data-fragment-state", journey.state);
    row.setAttribute("data-fragment-title", journey.title.toLocaleLowerCase("zh-CN"));
    row.setAttribute("aria-label", `${journey.title}，${journey.statusLabel}，${journey.nextAction}`);
    const identity = row.createDiv({ cls: "life-frag-panorama-identity" });
    const type = journey.alignment?.route === "verify" ? "研究核验型"
      : journey.alignment?.route === "graph" ? "Graph 路线"
        : journey.review ? "候选知识型" : "碎片整理型";
    identity.createEl("b", { cls: "life-research-row-title", text: journey.title });
    identity.createEl("small", { text: journey.updatedAt ? formatGraphTime(journey.updatedAt) : type });
    const next = row.createDiv({ cls: "life-frag-panorama-next" });
    next.createEl("strong", { cls: "life-research-row-status", text: journey.statusLabel });
    next.createEl("p", { cls: "life-research-row-next", text: journey.nextAction });
    next.createEl("small", { text: journey.state === "done" ? "查看结果 →" : "查看详情 →" });
    return { row, journey };
  });
  const empty = table.createDiv({ cls: "life-frag-panorama-empty", text: journeys.length ? "没有符合当前条件的碎片。" : "当前没有可投影的碎片记录。" });

  let activeFilter = viewState.filter || "all";
  function applyFilter(filter = activeFilter) {
    activeFilter = filter;
    viewState.filter = filter;
    viewState.query = search.value || "";
    viewState.onChange?.(filter, viewState.query);
    const query = clean(search.value).toLocaleLowerCase("zh-CN");
    let visible = 0;
    for (const { row, journey } of rowEntries) {
      const matchesState = filter === "all" || journey.state === filter;
      const matchesText = !query || journey.title.toLocaleLowerCase("zh-CN").includes(query);
      if (matchesState && matchesText) {
        row.removeAttribute("hidden");
        visible += 1;
      } else row.setAttribute("hidden", "hidden");
    }
    for (const item of metricButtons) {
      item.setAttribute("aria-pressed", String(item.getAttribute("data-fragment-filter") === filter));
      if (item.getAttribute("data-fragment-filter") === filter) item.addClass("is-active");
      else item.removeClass("is-active");
    }
    if (visible) empty.setAttribute("hidden", "hidden");
    else empty.removeAttribute("hidden");
  }
  viewState.applyFilter = applyFilter;
  if (!viewState.search) search.addEventListener("input", () => viewState.applyFilter());
  viewState.search = search;
  applyFilter();
}

function renderWorkbench(host, model, sources, drawer, reveal, runAction, say, switchBoard) {
  host.empty();
  const journeys = buildFragmentJourneys(model, sources);
  const readState = (source) => {
    try { return typeof source === "function" ? source() : null; }
    catch (error) { return { kind: "error", error: error?.message || "读取失败" }; }
  };
  const intents = readState(sources.intents);
  const reviews = readState(sources.reviews);
  const researchKnown = Boolean(intents && Array.isArray(intents.items) && intents.available !== false && !intents.error);
  const pendingKnown = researchKnown && Boolean(reviews && Array.isArray(reviews.items) && reviews.available !== false && !reviews.error);
  const knowledge = readState(sources.knowledge) || { kind: "loading" };
  const notes = new Map();
  for (const topic of knowledge.topics || []) for (const note of topic.notes || []) {
    if (knowledgeFreshnessView(note).usable) notes.set(note.knowledge_id, note);
  }
  const stats = host.createDiv({ cls: "life-workbench-stats" });
  for (const [label, value, board, filter] of [
    ["研究进行中", researchKnown ? journeys.filter(row => row.state === "active").length : "—", "fragments", "active"],
    ["系统阻塞", researchKnown ? journeys.filter(row => row.state === "blocked").length : "—", "fragments", "blocked"],
    ["可用研究结论", knowledge.kind === "ready" ? notes.size : "—", "assets"],
    ["长期主题", knowledge.kind === "ready" ? (knowledge.topics || []).length : "—", "assets"],
  ]) {
    const stat = button(stats, "", "life-workbench-stat", () => switchBoard(board, filter));
    stat.createEl("strong", { text: String(value) });
    stat.createSpan({ text: label });
  }

  if (!researchKnown) host.createEl("p", { cls: "life-cosmos-graph-error", text: intents?.error
    ? "研究服务读取失败：" + intents.error + "；以下记录不能代表当前全量状态。"
    : "研究状态尚未完整读取；当前数量无法确认。" });
  const pending = journeys.filter(row => ["waiting", "blocked", "active", "candidate"].includes(row.state));
  const progress = host.createEl("section", { cls: "life-workbench-section" });
  progress.createEl("h2", { text: "当前进展" });
  if (!pending.length) progress.createEl("p", { text: pendingKnown
    ? "当前没有待处理的研究记录。可以记录新的链接与问题。"
    : "当前处理状态尚未完整读取，不能确认是否有待处理事项。" });
  for (const journey of pending.slice(0, 5)) {
    const row = button(progress, "", `life-research-row is-${journey.state}`, async () => {
      if (journey.alignment) drawer.showIntent(journey.alignment, journey.element, row);
      else if (journey.element) reveal(journey.element, row);
      else if (journey.review) say(await runAction("review.open", journey.review));
    });
    row.createEl("b", { cls: "life-research-row-title", text: journey.title });
    const status = row.createDiv();
    status.createEl("strong", { cls: "life-research-row-status", text: journey.statusLabel });
    status.createEl("p", { cls: "life-research-row-next", text: journey.nextAction });
  }
  if (pending.length > 5) button(progress, `查看全部 ${pending.length} 条进展 →`, "", () => switchBoard("fragments"));
  const latest = host.createEl("section", { cls: "life-workbench-section life-workbench-latest" });
  latest.createEl("h2", { text: "最近研究结论" });
  if (knowledge.kind !== "ready") latest.createEl("p", { text: knowledge.kind === "error" ? "知识目录暂时无法读取；保留的条目可能不是最新，请到知识库查看服务说明。" : "知识目录正在加载。" });
  const current = [...notes.values()].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  if (knowledge.kind === "ready" && !current.length) latest.createEl("p", { text: "尚无可作为当前判断的研究结论。研究完成后会自动保存到知识库。" });
  for (const note of current.slice(0, 5)) {
    const row = button(latest, "", "life-research-row", (trigger) => drawer.showKnowledge(note, trigger));
    row.createEl("b", { cls: "life-research-row-title", text: note.title });
    row.createEl("small", { text: `${knowledge.kind === "ready" ? "查看结论与证据" : "上次读取的结果，当前性待重查"} · 修订 ${note.revision} · ${formatGraphTime(note.updated_at)}` });
  }
  button(latest, "打开知识库 →", "", () => switchBoard("assets"));
}

function renderRefineryBoard(board, model, reveal, runAction, say, sources = {}, showIntent, refreshers = [], projectionRefreshers = [], selection = {}) {
  const panorama = board.createDiv({ cls: "life-frag-view-pane" });
  // Only plain view values survive native remounts; input DOM and callbacks stay local.
  const viewState = { filter: selection.filter || "all", query: selection.query || "",
    onChange: (filter, query) => {
      if (board.isConnected !== false) Object.assign(selection, { filter, query });
    },
  };
  const refresh = () => {
    const active = board.ownerDocument?.activeElement;
    const search = panorama.querySelector(".life-frag-panorama-search");
    const selected = search && active === search ? [search.selectionStart, search.selectionEnd] : null;
    const tableScroll = panorama.querySelector(".life-frag-panorama")?.scrollLeft;
    panorama.empty();
    renderFragmentPanorama(panorama, model, sources, reveal, runAction, say, showIntent, viewState);
    if (typeof tableScroll === "number") panorama.querySelector(".life-frag-panorama").scrollLeft = tableScroll;
    if (selected) {
      search.focus?.({ preventScroll: true });
      search.setSelectionRange?.(...selected);
    }
  };
  refresh();
  refreshers.push(refresh);
  projectionRefreshers.push(refresh);
  // ponytail: 保留旧卡片的数据与操作兼容入口；默认展示统一运行投影。
  const legacy = board.createEl("details", { cls: "life-frag-legacy-view" });
  legacy.createEl("summary", { text: "整理记录 · 旧阶段看板" });
  legacy.createEl("p", {
    text: "以下是整理文档中的记录与建议。当前运行状态及需要你处理的事项，以研究记录为准。",
  });
  const kanban = legacy.createDiv({ cls: "life-frag-view-pane" });
  renderRefineryKanban(kanban, model, reveal, runAction, say);
  return (filter) => viewState.applyFilter?.(filter);
}

/* ---------- 板块：专注 · 轨道窗口 ORBIT WINDOW ---------- */
function renderOrbitBoard(board, model, reveal, hasLayout, registerTimer, activity, showDay, runAction, say) {
  cbBoardHead(board, "轨道窗口 · 当前专注", "锁定目标星，保持轨道。", model.focusTime, "倒计时");
  const grid = board.createDiv({ cls: "life-cb-grid" });

  /* 舷窗 */
  const orbit = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-orbit" });
  cbPanelHead(orbit, "轨道窗口", "轨道 · 追踪中");
  const stage = orbit.createDiv({ cls: "life-cb-orbit-stage" });
  const porthole = stage.createDiv({ cls: "life-cb-porthole" });
  porthole.createEl("i", { cls: "life-cb-ring-bg" });
  porthole.createEl("i", { cls: "life-cb-ring-ticks" });
  const ring = porthole.createEl("i", { cls: "life-cb-ring-prog" });
  const target = porthole.createDiv({ cls: "life-cb-orbit-target" });
  target.createEl("i", { cls: "life-cb-t-star" });
  target.createDiv({ cls: "life-cb-t-name", text: model.focusTitle });
  const targetState = target.createDiv({ cls: "life-cb-t-state", text: model.focusRunning ? "正在记录" : "未在计时" });
  const time = target.createDiv({ cls: "life-cb-t-time" });
  const mm = time.createSpan();
  const colon = time.createSpan({ cls: "life-cb-colon", text: ":" });
  const ss = time.createSpan();

  /* 倒计时：环随剩余时间消耗，冒号随秒跳动 */
  const FULL = parseClockSeconds(model.focusTime) ?? 25 * 60;
  let remain = FULL;
  const renderTime = () => {
    mm.setText(pad2(Math.floor(remain / 60)));
    ss.setText(pad2(remain % 60));
    if (ring.style) ring.style.setProperty("--life-cb-prog", `${((remain / FULL) * 100).toFixed(2)}%`);
    colon.removeClass("dim");
    if (hasLayout && typeof window !== "undefined") {
      registerTimer(window.setTimeout(() => colon.addClass("dim"), 480), "timeout");
    }
  };
  renderTime();
  if (model.focusRunning && hasLayout && typeof window !== "undefined") {
    registerTimer(window.setInterval(() => {
      if (remain > 0) { remain -= 1; renderTime(); }
    }, 1000), "interval");
  }

  /* 真实指令：具名 focus.toggle / focus.reset adapter → 既有 timer 命令链。
     F-D2：结果接入 say 反馈；toggle 后按钮文案就地翻转（乐观更新，重挂载
     时被真实投影纠正）；reset 为破坏性清零，走 confirm 二次确认。 */
  const actions = orbit.createDiv({ cls: "life-cb-orbit-actions" });
  const hasFocus = model.focusTitle !== "尚未选择当前专注";
  let running = Boolean(model.focusRunning);
  const go = button(actions,
    hasFocus ? (running ? "暂停记录" : "启动记录") : "选择当前专注",
    "go",
    hasFocus ? async () => {
      const result = await runAction("focus.toggle");
      if (say) say(result);
      if (result.kind === "success") {
        running = !running;
        go.setText(running ? "暂停记录" : "启动记录");
        targetState?.setText(running ? "正在记录" : "未在计时");
      }
    } : () => reveal(model.focusElement));
  go.setAttribute("data-cosmos-action", hasFocus ? "focus.toggle" : "focus.choose");
  const reset = button(actions, "重置轨道", "reset", async () => {
    /* F-D2：破坏性清零需二次确认；取消零副作用。confirm 先例见 register
       openReview handlers（window.confirm）。 */
    if (typeof window !== "undefined" && typeof window.confirm === "function"
      && !window.confirm("确认清零当前文档的累计阅读时间？此操作不可撤销。")) return;
    const result = await runAction("focus.reset");
    if (say) say(result);
  });
  reset.setAttribute("data-cosmos-action", "focus.reset");
  if (!hasFocus) {
    reset.setAttribute("disabled", "disabled");
    go.setAttribute("title", "先选择一篇文档作为当前专注，再开始计时");
  }

  /* 舱外活动清单 EVA：与 GO/NO-GO 同一诚实规则——完整绑定才调用既有
     completeTask；缺绑定明确为「本次会话选择」，点击只切换会话内视觉。
     P5：任务行统一为 go-item full 密度类族（与今日待办面板同长相），
     done 状态沿用 is-done 视觉（EVA 原独立 eva-item 样式废弃）。 */
  const eva = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-eva" });
  cbPanelHead(eva, "舱外活动清单", "今日待办");
  const evaList = eva.createDiv({ cls: "life-cosmos-go-list" });
  if (!model.tasks.length) {
    const emptyRow = evaList.createDiv({ cls: "life-cosmos-go-item is-static" });
    emptyRow.createSpan({ cls: "life-cosmos-led" });
    emptyRow.createSpan({ cls: "nm", text: "今天没有未完成任务" });
    emptyRow.createSpan({ cls: "st", text: "已清空" });
  }
  model.tasks.forEach((task) => {
    let row;
    if (task.binding) {
      row = evaList.createDiv({ cls: "life-cosmos-go-item is-bound" });
      row.setAttribute("data-done", "0");
      row.createSpan({ cls: "life-cosmos-led" });
      row.createSpan({ cls: "nm", text: task.title });
      const state = row.createSpan({ cls: "st", text: "未就绪" });
      const note = row.createSpan({ cls: "life-cosmos-go-note", text: "" });
      const complete = button(row, "完成", "life-cosmos-go-complete", async () => {
        if (complete.getAttribute("disabled") !== null) return;
        complete.setAttribute("disabled", "disabled");
        try {
          const result = await runAction("item.complete", task.binding.path, task.binding.line, task.title);
          if (result.kind === "success") {
            row.setAttribute("data-done", "1");
            row.addClass("is-done");
            state.setText("已完成");
            note.setText("");
          } else {
            note.setText(result.message || "完成失败，请稍后重试。");
            note.setAttribute("data-kind", result.kind);
          }
        } finally {
          complete.removeAttribute("disabled");
        }
      });
      complete.setAttribute("data-cosmos-action", "item.complete");
    } else {
      row = button(evaList, "", "life-cosmos-go-item is-session", () => {
        const done = row.getAttribute("data-done") === "1";
        row.setAttribute("data-done", done ? "0" : "1");
        row.toggleClass("is-done", !done);
        row.querySelector(".st")?.setText(done ? "未就绪" : "会话选择");
      });
      row.setAttribute("data-done", "0");
      row.setAttribute("data-session-choice", "1");
      row.setAttribute("aria-label", `${task.title}（本次会话选择，重载后恢复）`);
      row.createSpan({ cls: "life-cosmos-led" });
      row.createSpan({ cls: "nm", text: task.title });
      row.createSpan({ cls: "st", text: "未就绪" });
    }
    row.addEventListener("dblclick", () => reveal(task.element));
  });
  eva.createDiv({ cls: "life-cb-stage-foot", text: model.tasks.some((task) => task.binding)
    ? "完成按钮调用真实任务协议；无绑定的行只是本次会话选择"
    : "点击封闭舱盖 · 会话选择，重载后恢复" });

  /* 星历：当月小格，有记录的日子点亮 */
  const almanac = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-almanac" });
  const now = new Date();
  cbPanelHead(almanac, "星历", `星历 · ${MONTHS[now.getMonth()]}`);
  const cells = almanac.createDiv({ cls: "life-cb-almanac-grid" });
  const year = now.getFullYear();
  const month = now.getMonth();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const leading = new Date(year, month, 1).getDay();
  for (let i = 0; i < leading; i += 1) cells.createEl("i", { cls: "is-pad" });
  for (let day = 1; day <= daysInMonth; day += 1) {
    const iso = `${year}-${pad2(month + 1)}-${pad2(day)}`;
    const entries = activity.get(iso) || [];
    /* F-D4：移除"当日有笔记记录"path:""占位条目——activity 为空时该日不
       点亮、不可点（诚实无活动）；activity 条目自带真实 path，可打开。 */
    const cell = button(cells, String(day), "life-cb-day", entries.length ? (trigger) => showDay?.(iso, entries, trigger) : null);
    cell.setAttribute("aria-label", `${iso}，${entries.length} 条活动`);
    if (day === now.getDate()) cell.addClass("today");
    if (entries.length) {
      cell.addClass("has");
      cell.setAttribute("data-count", String(entries.length));
      const pending = entries.filter((entry) => entry.pending).length;
      if (pending) cell.setAttribute("data-pending", String(pending));
    }
  }
}

/* ---------- 板块：资产 · 星表 STAR CATALOGUE ---------- */
const SECTOR_COLORS = [
  "rgba(169,152,216,.35)", "rgba(155,200,214,.35)",
  "rgba(166,204,177,.35)", "rgba(232,195,147,.35)",
];

function knowledgeFreshnessView(record) {
  const freshness = record?.freshness;
  const status = ["current", "stale", "unreviewed"].includes(freshness?.status) ? freshness.status : "unknown";
  const derived = record?.conclusion_authority === "canonical_research" || record?.content_status === "theme_summary" || record?.kind === "period";
  const usable = !derived && status === "current" && record?.usable_as_current === true && record?.conclusion_authority === "research_result";
  const label = status === "stale" ? "已失效 · 保留历史，待系统更新"
    : status === "unreviewed" ? "尚未复核 · 不作为当前结论"
      : derived && status === "current" ? "结构依赖当前 · 结论以源研究为准"
        : usable ? "当前研究结论 · 模型已复核" : "有效性未确认 · 不作为当前结论";
  return { status, derived, usable, label, reasons: Array.isArray(freshness?.reasons) ? freshness.reasons.filter(reason => typeof reason === "string") : [] };
}

function renderKnowledgeFreshness(parent, record) {
  const view = knowledgeFreshnessView(record);
  const box = parent.createDiv({ cls: "life-knowledge-freshness" });
  box.setAttribute("data-freshness", view.status);
  box.createEl("p", { text: view.label });
  if (view.derived) box.createEl("small", { text: "本条用于结构整理与导航；研究判断、使用方法和限制以当前源研究为准。" });
  for (const reason of view.reasons) box.createEl("p", { text: reason });
  if (Number.isInteger(record?.freshness?.latest_revision) && record.freshness.latest_revision !== record.revision) {
    box.createEl("small", { text: `本条修订 ${record.revision}；当前最新修订 ${record.freshness.latest_revision}。` });
  }
  return view;
}

function renderKnowledgeCatalogue(board, drawer, sources, runAction, refreshers, projectionRefreshers, expanded) {
  const panel = board.createEl("section", { cls: "life-cosmos-panel life-knowledge-catalogue" });
  cbPanelHead(panel, "知识目录", "大类 · 小类 · 长期主题");
  const controls = panel.createDiv({ cls: "life-knowledge-controls" });
  const input = controls.createEl("input", { cls: "life-knowledge-search" });
  input.setAttribute("type", "search");
  input.setAttribute("placeholder", "搜索已沉淀的结论与使用方法…");
  input.setAttribute("aria-label", "搜索知识库正文");
  input.setAttribute("maxlength", "300");
  const message = panel.createEl("p", { cls: "life-knowledge-message", text: "" });
  message.setAttribute("aria-live", "polite");
  const content = panel.createDiv({ cls: "life-knowledge-tree" });
  const history = panel.createEl("details", { cls: "life-knowledge-history" });
  history.setAttribute("data-knowledge-disclosure", "history");
  if (expanded.get("history")) history.setAttribute("open", "open");
  history.createEl("summary", { text: "周月整理与修订记录" });
  const periods = history.createDiv({ cls: "life-knowledge-periods" });
  const updates = history.createDiv({ cls: "life-knowledge-updates" });
  let searchGeneration = 0;
  const noteRow = (parent, note) => {
    const row = button(parent, "", "life-knowledge-note", (trigger) => drawer.showKnowledge(note, trigger));
    row.setAttribute("data-knowledge-id", note.knowledge_id);
    row.createEl("b", { text: note.title });
    const view = knowledgeFreshnessView(note);
    row.createEl("small", { text: `${view.derived ? "结构整理" : "研究结论"} · 修订 ${note.revision} · ${formatGraphTime(note.updated_at)}` });
    row.createEl("small", { text: view.label });
    row.setAttribute("data-freshness", view.status);
  };
  const captureDisclosures = () => {
    for (const el of panel.querySelectorAll("[data-knowledge-disclosure]")) {
      expanded.set(el.getAttribute("data-knowledge-disclosure"), el.open ?? el.getAttribute("open") !== null);
    }
  };
  const render = () => {
    captureDisclosures();
    const disclosure = (el, key, defaultOpen = false) => {
      el.setAttribute("data-knowledge-disclosure", key);
      if (expanded.get(key) ?? defaultOpen) el.setAttribute("open", "open");
    };
    searchGeneration += 1;
    content.empty();
    const state = typeof sources.knowledge === "function" ? sources.knowledge() : { kind: "loading" };
    periods.empty();
    periods.createEl("h3", { text: "周/月知识总览" });
    const periodState = state.periods || { kind: "loading", items: [] };
    if (periodState.kind === "error") periods.createEl("p", { text: `周/月总览读取失败：${periodState.error || "服务不可用"}${periodState.stale ? "；保留上次读取结果。" : "；目前不能判断是否已形成。"}` });
    else if (periodState.kind !== "ready") periods.createEl("p", { text: "周/月知识总览尚未加载。" });
    else if (!periodState.items.length) periods.createEl("p", { text: "尚未形成周/月知识总览。" });
    for (const record of periodState.items || []) noteRow(periods, record);
    updates.empty();
    updates.createEl("h3", { text: "最近知识更新" });
    const notices = state.notifications || { kind: "loading", items: [] };
    history.querySelector("summary").setText(`周月整理与修订记录 · ${periodState.kind === "ready" ? (periodState.items || []).length : "待确认"} 份整理 · ${notices.kind === "ready" ? (notices.items || []).length : "待确认"} 条更新`);

    if (notices.kind === "error") updates.createEl("p", { text: `更新通知读取失败：${notices.error || "服务不可用"}${notices.stale ? "；显示上次读取的通知。" : "；不能判断是否有新修订。"}` });
    else if (notices.kind !== "ready") updates.createEl("p", { text: "更新通知尚未加载。" });
    else if (!notices.items.length) updates.createEl("p", { text: "目前没有知识修订通知。" });
    for (const notice of (notices.items || []).slice(0, 10)) {
      const row = updates.createEl("details", { cls: "life-knowledge-update" });
      disclosure(row, `notice:${notice.notification_id}`);
      row.createEl("summary", { text: `${notice.title} · 修订 ${notice.revision} · ${formatGraphTime(notice.updated_at)}` });
      row.createEl("p", { text: notice.message });
      for (const revision of notice.revisions || []) {
        if (revision.previous_statement) row.createEl("p", { text: `此前：${revision.previous_statement}` });
        if (revision.updated_statement) row.createEl("p", { text: `本次：${revision.updated_statement}` });
        if (revision.reason) row.createEl("p", { text: `修订原因：${revision.reason}` });
      }
      const feedback = row.createEl("small", { text: "" });
      feedback.setAttribute("aria-live", "polite");
      button(row, "打开此修订记录", "", async () => {
        const outcome = await runAction("item.openNote", notice.path);
        if (outcome.kind !== "success") feedback.setText(outcome.message || "修订记录暂时无法打开。");
      });
    }
    if ((notices.items || []).length > 10) updates.createEl("p", { text: "这里只显示最近 10 条更新；更多知识与当前修订见下方目录。" });
    if (state.kind === "loading") {
      message.setText("知识目录尚未加载；可刷新重试。");
      return;
    }
    message.setText(state.kind === "error"
      ? `知识服务不可用：${state.error || "读取失败"}${state.stale ? "；显示最近一次成功读取的目录。" : "；尚未取得目录，不能判断是否为空。"}`
      : "目录保留研究历史与结构整理；有效性按条目标注，研究判断以当前源研究为准。");
    const topics = state.topics || [];
    if (state.kind === "ready" && !topics.length) content.createEl("p", { text: "还没有已沉淀的知识。" });
    const categories = new Map();
    for (const topic of topics) {
      if (!categories.has(topic.category)) categories.set(topic.category, new Map());
      const subs = categories.get(topic.category);
      if (!subs.has(topic.subcategory)) subs.set(topic.subcategory, []);
      subs.get(topic.subcategory).push(topic);
    }
    for (const [category, subcategories] of categories) {
      const group = content.createEl("details", { cls: "life-knowledge-category" });
      disclosure(group, `category:${category}`, true);
      group.createEl("summary", { text: category || "未归大类" });
      for (const [subcategory, themes] of subcategories) {
        const section = group.createDiv({ cls: "life-knowledge-subcategory" });
        section.createEl("h3", { text: subcategory || "未归小类" });
        for (const topic of themes) {
          const theme = section.createEl("details", { cls: "life-knowledge-topic" });
          disclosure(theme, `topic:${topic.topic_id}`);
          const derivedCount = topic.notes.filter(note => knowledgeFreshnessView(note).derived).length;
          const researchCount = topic.notes.filter(note => !knowledgeFreshnessView(note).derived && note.conclusion_authority === "research_result").length;
          const unknownCount = topic.notes.length - derivedCount - researchCount;
          const counts = [`${researchCount} 份研究`, `${derivedCount} 份结构整理`];
          if (unknownCount) counts.push(`${unknownCount} 份类型待确认`);
          theme.createEl("summary", { text: `${topic.title} · ${counts.join(" · ")}` });
          topic.notes.forEach((note) => noteRow(theme, note));
        }
      }
    }
  };
  const search = async () => {
    const query = clean(input.value);
    if (!query) { render(); return; }
    const current = ++searchGeneration;
    message.setText("正在搜索知识正文…");
    try {
      if (typeof sources.searchKnowledge !== "function") throw new Error("知识搜索尚未接入");
      const notes = await sources.searchKnowledge(query);
      if (current !== searchGeneration || panel.isConnected === false || clean(input.value) !== query) return;
      content.empty();
      const currentNotes = notes.filter(note => knowledgeFreshnessView(note).usable);
      message.setText(`当前可用研究的正文关键词匹配：${currentNotes.length} 条；不是语义相关度排序。`);
      currentNotes.forEach((note) => noteRow(content, note));
      if (!currentNotes.length) content.createEl("p", { text: "没有匹配的当前可用研究；未复核、失效记录和结构整理仍可在完整目录查看。" });
    } catch (error) {
      if (current === searchGeneration && panel.isConnected !== false) message.setText(`搜索失败：${error?.message || "服务不可用"}；原目录仍保留。`);
    }
  };
  button(controls, "搜索", "", search);
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault?.(); void search(); } });
  input.addEventListener("input", () => {
    searchGeneration += 1;
    if (!clean(input.value)) render();
    else message.setText("输入关键词后搜索；当前保留上次显示的内容。");
  });
  button(controls, "刷新目录", "", async (trigger) => {
    if (trigger.getAttribute("disabled") !== null) return;
    trigger.setAttribute("disabled", "disabled");
    try {
      const result = await runAction("knowledge.refresh");
      if (panel.isConnected === false) return;
      input.value = "";
      render();
      if (result.kind !== "success") message.setText(`目录刷新失败：${result.message || "服务不可用"}`);
    } finally { trigger.removeAttribute("disabled"); }
  }).setAttribute("data-cosmos-action", "knowledge.refresh");
  render();
  refreshers.push(() => { input.value = ""; render(); });
  projectionRefreshers.push(() => { render(); if (clean(input.value)) void search(); });
  return captureDisclosures;
}

function renderCatalogueBoard(board, model, reveal, runAction, drawer, sources, refreshers, projectionRefreshers, expanded) {
  cbBoardHead(board, "知识库", "按主题查找结论、知识结构与待补目标。", "", "");
  const captureDisclosures = renderKnowledgeCatalogue(board, drawer, sources, runAction, refreshers, projectionRefreshers, expanded);
  const older = board.createEl("details", { cls: "life-knowledge-legacy" });
  older.createEl("summary", { text: `原有资产与目录 · ${model.assets.length} 条` });
  const grid = older.createDiv({ cls: "life-cb-grid" });

  const catalog = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-catalog" });
  cbPanelHead(catalog, "原有资产目录", "历史验证记录");
  const headRow = catalog.createDiv({ cls: "life-cb-cat-head-row" });
  ["#", "名称", "成熟度", "标签", "验证日期"].forEach((text) => headRow.createSpan({ text }));
  const list = catalog.createDiv({ cls: "life-cb-cat-list" });

  const rail = grid.createDiv({ cls: "life-cb-scope-rail" });
  const scope = rail.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-scope-card" });
  scope.createDiv({ cls: "life-cb-sc-eye", text: "观测 · 观测卡" });
  const scName = scope.createEl("h3", { text: model.assets[0]?.title || "暂无已验证资产" });
  const scNote = scope.createEl("p", { text: model.assets[0] ? `${model.assets[0].maturity || "已验证"} · 末次验证 ${model.assets[0].date || "—"}` : "真实验证通过的实践会进入星表。" });
  const scCaps = scope.createDiv({ cls: "life-cb-sc-caps" });
  const syncCaps = (tags) => {
    scCaps.empty();
    (tags.length ? tags : ["已验证能力"]).forEach((tag) => scCaps.createEl("i", { text: tag }));
  };
  syncCaps(model.assets[0]?.tags || []);
  let current = model.assets[0] || null;
  /* 资产只认显式 data-path：有路径打开真实 Obsidian 文档，无路径诚实说明。
     R25 P1-9：守卫打开的结果必须可见（成功清空 note，失败显示原因）。 */
  const scopeNote = scope.createEl("small", { cls: "life-cb-scope-note", text: "" });
  const showOpenOutcome = (note, result) =>
    note.setText(result && result.kind === "success" ? "" : (result && result.message) || "打开失败，请稍后重试。");
  const openAsset = async (item) => {
    if (item?.path) {
      showOpenOutcome(scopeNote, await runAction("item.openNote", item.path));
      return;
    }
    scopeNote.setText("暂无可打开文档：这颗恒星没有显式路径。");
  };
  button(scope, "打开原始记录 →", "life-cb-sc-link", () => openAsset(current));

  if (!model.assets.length) cbEmpty(list, "只有真实验证通过的实践才会生成恒星");
  model.assets.forEach((item, index) => {
    const row = list.createDiv({ cls: `life-cb-cat-row${index === 0 ? " hot" : ""}` });
    /* R26 P2-1：表格行无法用原生 button（打断五列布局且样式文件不在本轮
       范围）——补 role/tabindex/focus/Enter/Space 等价键盘路径。 */
    row.setAttribute("role", "button");
    row.setAttribute("tabindex", "0");
    row.createSpan({ cls: "life-cb-idx", text: pad2(index + 1) });
    const name = row.createDiv({ cls: "life-cb-nm" });
    name.createEl("b", { text: item.title });
    row.createSpan({ cls: "life-cb-maturity", text: item.maturity || "未标注" });
    const spectra = row.createSpan({ cls: "life-cb-spectra" });
    (item.tags.length ? item.tags : ["已验证能力"]).forEach((tag, tagIndex) => {
      spectra.createEl("i", { cls: tagIndex > 0 ? "v" : "", text: tag });
    });
    row.createSpan({ cls: "life-cb-obs", text: item.date || "—" });
    const activate = () => {
      current = item;
      for (const other of list.querySelectorAll(".life-cb-cat-row")) other.removeClass("hot");
      row.addClass("hot");
      scName.setText(item.title);
      scNote.setText(`${item.maturity || "已验证"} · 末次验证 ${item.date || "—"}`);
      syncCaps(item.tags);
    };
    row.addEventListener("pointerenter", activate);
    row.addEventListener("focus", activate); // R26 P2-1：键盘聚焦等价悬停预览
    row.addEventListener("click", () => openAsset(item));
    row.addEventListener("keydown", (ev) => {
      if (!ev || (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar")) return;
      ev.preventDefault?.();
      openAsset(item);
    });
  });

  const para = rail.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-para" });
  cbPanelHead(para, "原有文件目录", "PARA");
  const paraGrid = para.createDiv({ cls: "life-cb-para-grid" });
  const paraNote = para.createEl("small", { cls: "life-cb-scope-note", text: "" });
  model.para.forEach((item, index) => {
    const sector = button(paraGrid, "", "life-cb-sector", async () => {
      if (item.path) {
        showOpenOutcome(paraNote, await runAction("item.openNote", item.path));
        return;
      }
      paraNote.setText(`「${item.label}」暂无可打开文档：没有显式路径。`);
    });
    if (sector.style) sector.style.setProperty("--sc", SECTOR_COLORS[index % SECTOR_COLORS.length]);
    sector.createEl("b", { text: item.label });
    sector.createEl("small", { text: item.desc || "PARA 入口" });
  });
  return captureDisclosures;
}

/* ---------- 板块：研究 · 深空探测阵列 DEEP ARRAY ---------- */
const GRAPH_LABEL_MAP = { 进行中: "进行中", 等待人工: "等待人工", 异常停止: "异常停止" };

function renderArrayBoard(board, model, reveal, drawer, runAction, sources, graphRefreshers = [], say) {
  cbBoardHead(board, "研究 · 运行与来源", "查看研究运行，追溯信息来源。", model.daily.length + model.watched.length + model.signals.length, "来源条目");
  const grid = board.createDiv({ cls: "life-cb-grid" });

  /* 复核频谱：Loop 五态 */
  const spectrum = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-spectrum" });
  cbPanelHead(spectrum, "Loop 复核", "运行状态 · 非研究结论");
  const bars = spectrum.createDiv({ cls: "life-cb-spectrum-bars" });
  const SLOTS = [
    ["ATTENTION", "需关注", "rgba(232,195,147,.5)"], ["SUCCESS", "已完成", "rgba(166,204,177,.5)"],
    ["QUIET", "平静", "rgba(155,200,214,.5)"], ["WARNING", "注意", "rgba(214,166,220,.5)"],
    ["DANGER", "危险", "rgba(232,155,155,.5)"],
  ];
  const max = Math.max(1, ...SLOTS.map(([slot]) => model.loopSlots[slot] || 0));
  SLOTS.forEach(([slot, label, color]) => {
    const count = model.loopSlots[slot] || 0;
    const spec = button(bars, "", "life-cb-spec", () => reveal(model.loopElements[slot]));
    if (spec.style) spec.style.setProperty("--bc", color);
    const bar = spec.createDiv({ cls: "life-cb-bar" });
    const fill = bar.createEl("i");
    const height = count === 0 ? 6 : Math.max(10, Math.round((count / max) * 100));
    if (fill.style) fill.style.height = `${height}%`;
    spec.createEl("b", { text: String(count) });
    spec.createSpan({ text: label });
  });

  /* Graph 六态摘要：主视觉只有人类状态、时间与 Checkpoint；详情与技术 ID
     进原生抽屉。刷新后就地重读 sources 重绘——成功更新、失败保留旧摘要
     并标注服务错误。 */
  const graphPanel = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-graph" });
  cbPanelHead(graphPanel, "Graph 工作流", "Graph · 六状态");
  const graphBody = graphPanel.createDiv({ cls: "life-cb-graph-body" });
  const renderGraphBody = () => {
    graphBody.empty();
    const graphSource = typeof sources?.graph === "function" ? sources.graph() : { state: null, lastGood: null };
    const graphReady = graphSource.state && graphSource.state.kind === "ready" ? graphSource.state : null;
    // G8：刷新失败（ready + stale）同样按服务错误标注——旧数据保留但显式说明。
    const graphError = graphSource.state && (graphSource.state.kind === "error" || graphSource.state.stale) ? graphSource.state : null;
    const graphUsable = graphReady || (graphSource.lastGood && graphSource.lastGood.kind === "ready" ? graphSource.lastGood : null);
    if (graphError) {
      graphBody.createEl("p", { cls: "life-cosmos-graph-error", text: "服务不可用" });
      if (graphUsable) graphBody.createEl("p", { cls: "life-cosmos-graph-stale", text: "显示最近一次成功读取" });
    }
    if (graphUsable) {
      const summary = computeGraphSummary(graphUsable.runs);
      if (!summary.total) {
        graphBody.createEl("p", { text: "暂无 Graph 运行" });
      } else {
        const line = graphBody.createDiv({ cls: "life-cb-graph-six" });
        for (const [label, value] of [
          ["进行中", summary.active], ["等待人工", summary.waiting], ["异常", summary.abnormal],
          ["已结束", summary.completed], ["未知", summary.unknown],
        ]) {
          const cell = line.createDiv({ cls: "life-cb-graph-cell" });
          cell.createEl("b", { text: String(value) });
          cell.createSpan({ text: label });
        }
        graphBody.createEl("p", {
          cls: "life-cosmos-graph-latest",
          text: summary.latest
            ? `最近运行：${summary.latest.statusLabel} · ${formatGraphTime(summary.latest.updatedAt)} · Checkpoint #${summary.latest.sequence}`
            : "运行缺少可靠时间，无法判断最近运行。",
        });
      }
    } else if (!graphError) {
      graphBody.createEl("p", { text: "正在读取 Graph 运行摘要…" });
    }
  };
  renderGraphBody();
  graphRefreshers.push(renderGraphBody);
  const graphActions = graphPanel.createDiv({ cls: "life-cb-graph-actions" });
  const graphDetail = button(graphActions, "详情", "", (trigger) => drawer.showGraph(trigger));
  graphDetail.setAttribute("data-cosmos-action", "drawer.graph");


  /* 研究信号：今日自动化情报 / 持续关注 / AI 洞察的只读摘要；点击只认显式
     data-path，无路径诚实说明。 */
  const signals = grid.createEl("section", { cls: "life-cosmos-panel life-cb-panel life-cb-signals" });
  cbPanelHead(signals, "研究信号", "信号 · 只读");
  const signalGroups = [
    ["今日自动化情报", model.daily],
    ["持续关注", model.watched],
    ["AI 洞察", model.signals],
  ];
  let anySignal = false;
  for (const [label, items] of signalGroups) {
    if (!items.length) continue;
    anySignal = true;
    signals.createEl("small", { cls: "life-cb-signal-label", text: label });
    for (const item of items.slice(0, 3)) {
      const row = button(signals, "", "life-cb-signal-row", async () => {
        if (!item.path) return;
        /* V2-C1 修复：openNote 结果接入 say（与待办来源 :1201 同款，F-K13
           语义）——失效路径的错误可见，不再静默。 */
        const result = await runAction("item.openNote", item.path);
        if (say && result.kind !== "success") say(result);
      });
      /* F-D1：filter 消费面需要 read/watched 语义——投影字段落到行属性。 */
      if (item.read) row.setAttribute("data-read", "1");
      if (label === "持续关注") row.setAttribute("data-watched", "1");
      row.createEl("b", { text: item.title });
      row.createEl("small", { text: item.meta || (item.path ? "打开查看详情" : "暂无可打开文档") });
      if (!item.path) {
        row.setAttribute("disabled", "disabled");
        row.setAttribute("aria-label", `${item.title}（暂无可打开文档）`);
      }
    }
  }
  if (!anySignal) cbEmpty(signals, "今天还没有自动化情报或关注更新");
}

/* ---------- 思考星图 canvas（v11 绘制逻辑忠实移植） ---------- */
function drawAtlas(canvas, map, caption, onOpen) {
  const ctx = typeof canvas.getContext === "function" ? canvas.getContext("2d") : null;
  if (!ctx || typeof window === "undefined") {
    const noop = () => {};
    noop.setActive = () => {};
    return noop;
  }
  const categories = (map.categories || []).slice(0, 8);
  const systems = categories.map((category, index) => {
    const spot = SYSTEM_SPOTS[index] || [
      0.5 + Math.cos((index / Math.max(1, categories.length)) * Math.PI * 2) * 0.34,
      0.56 + Math.sin((index / Math.max(1, categories.length)) * Math.PI * 2) * 0.30,
    ];
    return { category, x: spot[0], y: spot[1], hue: SYSTEM_COLORS[index % SYSTEM_COLORS.length] };
  });
  const stars = [];
  systems.forEach((system, si) => {
    const items = system.category.items || [];
    items.forEach((item, i) => {
      const angle = (i / Math.max(1, items.length)) * Math.PI * 2 + si * 1.7;
      const orbit = 26 + ((si * 31 + i * 47) % 40);
      stars.push({
        sys: si,
        item,
        ox: Math.cos(angle) * orbit,
        oy: Math.sin(angle) * orbit * 0.72,
        r: 1.7 + ((si + i) % 3) * 0.8,
        st: item.status === "provisional" ? "wip" : item.status === "concluded" ? "done" : "open",
        ph: (si * 13 + i * 7) % 6.28,
      });
    });
  });
  const baseLinks = [[0, 2], [1, 2], [2, 3], [2, 5], [0, 4], [3, 1]];
  const links = baseLinks.filter(([a, b]) => a < systems.length && b < systems.length);
  for (let i = 6; i < systems.length; i += 1) links.push([i, Math.min(2, systems.length - 1)]);

  let frame = 0;
  let disposed = false;
  let active = true;
  let mouse = { x: -999, y: -999 };
  let width = 0;
  let height = 0;
  let dpr = 1;
  let hoverSys = -1;
  let hitStars = [];
  const resize = () => {
    const rect = canvas.getBoundingClientRect();
    /* 面板未上屏时 rect 为 0：绝不能把 1x1 写进属性，否则 canvas 固有
       宽高比变成 1:1，上屏后被拉成正方形，把整个面板撑高一倍。 */
    if (!rect.width || !rect.height) return;
    dpr = window.devicePixelRatio || 1;
    width = canvas.width = Math.max(1, Math.round(rect.width * dpr));
    height = canvas.height = Math.max(1, Math.round(rect.height * dpr));
  };
  const onMove = (event) => {
    const rect = canvas.getBoundingClientRect();
    mouse = { x: (event.clientX - rect.left) * dpr, y: (event.clientY - rect.top) * dpr };
  };
  const onLeave = () => { mouse = { x: -999, y: -999 }; };
  const onOpenAtPointer = () => {
    const star = hitStars.find((hit) => Math.hypot(mouse.x - hit.x, mouse.y - hit.y) <= Math.max(12 * dpr, hit.r * 3));
    if (star?.item) onOpen?.(star.item, systems[star.sys]?.category);
    else if (hoverSys >= 0) onOpen?.(null, systems[hoverSys]?.category);
  };
  const onKey = (event) => {
    if ((event.key === "Enter" || event.key === " ") && systems.length) {
      event.preventDefault?.();
      /* F-D8：无悬停目标时打开总览（全部系统列表），不再错误落到 systems[0]。 */
      const category = hoverSys >= 0 ? systems[hoverSys]?.category : null;
      onOpen?.(null, category);
    }
  };
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerleave", onLeave);
  canvas.addEventListener("click", onOpenAtPointer);
  canvas.addEventListener("keydown", onKey);
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
  observer?.observe(canvas);
  resize();
  const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const render = (now = 0) => {
    if (disposed || !active || canvas.isConnected === false) return;
    const t = now / 1000;
    ctx.clearRect(0, 0, width, height);
    const px = (s) => s.x * width;
    const py = (s) => s.y * height;
    hoverSys = -1;
    systems.forEach((system, i) => {
      if (Math.hypot(mouse.x - px(system), mouse.y - py(system)) < 70 * dpr) hoverSys = i;
    });
    if (canvas.style) canvas.style.cursor = hoverSys >= 0 ? "pointer" : "crosshair";
    if (hoverSys >= 0) {
      const system = systems[hoverSys];
      caption.querySelector("strong")?.setText(system.category.name);
      caption.querySelector("span")?.setText(`${(system.category.items || []).length} 个星系 · 主题 ${hoverSys + 1}`);
      if (caption.style) caption.style.opacity = 1;
    } else {
      caption.querySelector("strong")?.setText(systems.length ? "悬停一个星系" : "暂无思考主题");
      caption.querySelector("span")?.setText("主题详情");
      if (caption.style) caption.style.opacity = 0.55;
    }
    /* 关系航线：渐变虚线 + 行进偏移 */
    links.forEach(([a, b]) => {
      const A = systems[a];
      const B = systems[b];
      const hot = hoverSys === a || hoverSys === b;
      const gradient = ctx.createLinearGradient(px(A), py(A), px(B), py(B));
      gradient.addColorStop(0, `rgba(${A.hue},${hot ? 0.55 : 0.22})`);
      gradient.addColorStop(1, `rgba(${B.hue},${hot ? 0.55 : 0.22})`);
      ctx.strokeStyle = gradient;
      ctx.lineWidth = (hot ? 1.4 : 0.8) * dpr;
      ctx.setLineDash([3 * dpr, 5 * dpr]);
      ctx.lineDashOffset = reduced ? 0 : -t * 12 * dpr;
      ctx.beginPath();
      ctx.moveTo(px(A), py(A));
      ctx.lineTo(px(B), py(B));
      ctx.stroke();
      ctx.setLineDash([]);
    });
    /* 星系光晕：呼吸半径 */
    systems.forEach((system, si) => {
      const x = px(system);
      const y = py(system);
      const [r, g, b] = system.hue;
      const hot = hoverSys === si;
      const dim = hoverSys >= 0 && !hot;
      const alpha = dim ? 0.3 : 1;
      const glowR = (54 + (reduced ? 0 : Math.sin(t * 0.8 + si) * 6) + (hot ? 18 : 0)) * dpr;
      const glow = ctx.createRadialGradient(x, y, 0, x, y, glowR);
      glow.addColorStop(0, `rgba(${r},${g},${b},${(hot ? 0.36 : 0.22) * alpha})`);
      glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = glow;
      ctx.fillRect(x - glowR, y - glowR, glowR * 2, glowR * 2);
    });
    /* 星星：闪烁 + 呼吸偏移 */
    hitStars = [];
    stars.forEach((star) => {
      const system = systems[star.sys];
      const wobble = reduced ? 0 : Math.sin(t * 0.6 + star.ph) * 3 * dpr;
      const x = system.x * width + star.ox * dpr + wobble;
      const y = system.y * height + star.oy * dpr + wobble * 0.6;
      hitStars.push({ x, y, r: star.r * dpr, item: star.item, sys: star.sys });
      const hot = hoverSys === star.sys;
      const dim = hoverSys >= 0 && !hot;
      const alpha = (dim ? 0.25 : 1) * (reduced ? 1 : 0.72 + Math.sin(t * 1.6 + star.ph) * 0.28);
      const col = star.st === "wip" ? "232,195,147" : star.st === "done" ? "166,204,177" : "255,255,255";
      ctx.beginPath();
      ctx.arc(x, y, star.r * dpr * (hot ? 1.5 : 1), 0, 6.283);
      ctx.fillStyle = `rgba(${col},${star.st === "open" ? 0.62 * alpha : alpha})`;
      ctx.shadowColor = `rgba(${col},.9)`;
      ctx.shadowBlur = (hot ? 12 : 7) * dpr;
      ctx.fill();
      ctx.shadowBlur = 0;
    });
    /* 主题标签 */
    systems.forEach((system, si) => {
      const hot = hoverSys === si;
      const dim = hoverSys >= 0 && !hot;
      ctx.font = `600 ${10.5 * dpr}px ui-monospace, Menlo, monospace`;
      ctx.fillStyle = `rgba(245,242,250,${dim ? 0.26 : hot ? 0.95 : 0.72})`;
      ctx.textAlign = "center";
      /* 靠近底边的星系标签收进画布，避免被下边缘裁掉 */
      const labelY = Math.min(py(system) + 58 * dpr, height - 10 * dpr);
      ctx.fillText(system.category.name, px(system), labelY);
    });
    if (active) frame = window.requestAnimationFrame(render);
  };
  const setActive = (next) => {
    const value = Boolean(next);
    if (disposed || value === active) return;
    active = value;
    window.cancelAnimationFrame?.(frame);
    if (active) {
      resize();
      frame = window.requestAnimationFrame(render);
    }
  };
  const visibilityObserver = typeof IntersectionObserver === "function"
    ? new IntersectionObserver((entries) => setActive(entries.some((entry) => entry.isIntersecting)))
    : null;
  visibilityObserver?.observe(canvas);
  frame = window.requestAnimationFrame(render);
  const dispose = () => {
    disposed = true;
    observer?.disconnect();
    visibilityObserver?.disconnect();
    window.cancelAnimationFrame?.(frame);
    canvas.removeEventListener?.("pointermove", onMove);
    canvas.removeEventListener?.("pointerleave", onLeave);
    canvas.removeEventListener?.("click", onOpenAtPointer);
    canvas.removeEventListener?.("keydown", onKey);
  };
  dispose.setActive = setActive;
  return dispose;
}

function renderAtlas(panel, model, onOpen) {
  addPanelHead(panel, "思考星图", `${model.thoughts} STARS · ${model.systems} SYSTEMS`);
  const canvas = panel.createEl("canvas");
  canvas.setAttribute("aria-label", "真实思考主题星图");
  canvas.setAttribute("role", "button");
  canvas.setAttribute("tabindex", "0");
  const caption = panel.createDiv({ cls: "life-cosmos-atlas-caption" });
  caption.createEl("strong", { text: model.systems ? "悬停一个星系" : "暂无思考主题" });
  caption.createSpan({ text: "主题详情" });
  const legend = panel.createDiv({ cls: "life-cosmos-atlas-legend" });
  [["provisional", "阶段判断"], ["concluded", "已有结论"], ["open", "待展开"]].forEach(([status, label]) => {
    const item = legend.createSpan({ text: label });
    item.setAttribute("data-status", status);
  });
  return drawAtlas(canvas, model.map, caption, onOpen);
}

function addPointerGlow(home, cosmos) {
  /* F3：全局跟手光层已删（nigo 判定逻辑不成立）——只保留面板内的
     --life-panel-x/y 光晕（光只照亮你指着的面板，语义明确）。 */
  const onMove = (event) => {
    const panel = event.target?.closest?.(".life-cosmos-panel");
    if (panel) {
      const panelRect = panel.getBoundingClientRect();
      panel.style.setProperty("--life-panel-x", `${event.clientX - panelRect.left}px`);
      panel.style.setProperty("--life-panel-y", `${event.clientY - panelRect.top}px`);
    }
  };
  home.addEventListener("pointermove", onMove);
  return () => home.removeEventListener?.("pointermove", onMove);
}

function uniqueTexts(element, selectors, limit = 8) {
  const values = [];
  for (const selector of selectors) {
    for (const node of element?.querySelectorAll?.(selector) || []) {
      const value = clean(node.textContent);
      if (value && !values.includes(value)) values.push(value);
      if (values.length >= limit) return values;
    }
  }
  return values;
}

/* ================================================================
   Cosmos 原生抽屉（语义动作层）
   - 每个动作以稳定 actionId 映射 register.js 的具名 capability adapter；
     旧 DOM 只提供只读安全字段与显式 data-path，绝不按按钮顺序复制动作。
   - 打开时记录触发器、主界面 inert、初始焦点进入第一个可操作控件；
     Escape 关闭、Tab/Shift+Tab 困在抽屉内；关闭后焦点归还仍连接的触发器。
     document 级监听与 inert 恢复器全部纳入 dispose，与 session 同生命周期。
   - 异步提交携带 drawer generation：抽屉关闭或重开后的迟到响应一律丢弃；
     提交按钮飞行中 disabled，连续点击只产生一个持久请求。
   ================================================================ */

const DRAWER_FOCUSABLE = "button, input, textarea, select, a, [tabindex]";

/* 逗号选择器在真实 DOM 与测试替身行为不一致：逐简单选择器按文档序合并，
   两端结果一致（Tab 圈依赖稳定的首/尾元素）。 */
function queryAll(root, selector) {
  const parts = selector.split(",").map((part) => part.trim()).filter(Boolean);
  if (parts.length <= 1) return [...root.querySelectorAll(selector)];
  const out = [];
  const walk = (el) => {
    for (const child of el.children || []) {
      if (parts.some((part) => child.matches?.(part))) out.push(child);
      walk(child);
    }
  };
  walk(root);
  return out;
}

function drawerFocusables(drawer) {
  return queryAll(drawer, DRAWER_FOCUSABLE)
    .filter((el) => el.getAttribute?.("disabled") === null && el.getAttribute?.("hidden") === null);
}

function formatGraphTime(value) {
  const time = Date.parse(typeof value === "string" ? value : "");
  if (Number.isNaN(time)) return "时间未知";
  const date = new Date(time);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function createCosmosDrawer(doc, cosmos, context) {
  const { openNote, capabilities = {}, sources = {}, legacy = null, viewState } = context || {};
  // Native top layer escapes Dataview's transformed/zoomed preview ancestors.
  const shell = cosmos.createEl("dialog", { cls: "life-cosmos-drawer-shell" });
  shell.setAttribute("aria-hidden", "true");
  const shade = shell.createDiv({ cls: "life-cosmos-drawer-shade" });
  const drawer = shell.createEl("aside", { cls: "life-cosmos-drawer" });
  shell.setAttribute("aria-modal", "true");

  let generation = 0;
  let opened = false;
  let trigger = null;
  let inertTargets = [];
  let keydownHandler = null;
  let markdownDisposers = [];
  let knowledgeSelection = null;
  let fragmentSelection = null;
  let knowledgeReadingPosition = null;
  let restoringKnowledgePosition = false;
  let disposed = false;

  // Dataview 移除节点后原生 scrollTop 会归零；仍连接时记录会话阅读位置。
  const rememberKnowledgePosition = () => {
    if (!opened || !knowledgeSelection || restoringKnowledgePosition || drawer.isConnected === false) return;
    const diagrams = [...drawer.querySelectorAll(".life-knowledge-flow")];
    knowledgeReadingPosition = {
      scrollTop: drawer.scrollTop || 0,
      diagramScrollLeft: diagrams.map((element) => element.scrollLeft || 0),
      focusedDiagram: diagrams.indexOf(doc.activeElement),
      answerExpanded: drawer.querySelector(".life-knowledge-answer")?.open === true,
      historyExpanded: drawer.querySelector(".life-knowledge-history")?.open === true,
    };
  };
  drawer.addEventListener("scroll", rememberKnowledgePosition, true);
  drawer.addEventListener("focusin", rememberKnowledgePosition);

  const isOpen = () => opened;
  const projectionChanged = () => {
    if (!opened || drawer.querySelector(".life-cosmos-projection-update")) return;
    const body = drawer.querySelector(".life-cosmos-drawer-body");
    body?.createEl("p", { cls: "life-cosmos-projection-update",
      text: "后台研究状态或知识目录已有更新。本详情保留打开时的内容与输入；重新打开可读取最新结果。" })
      ?.setAttribute("role", "status");
  };


  /* 统一 adapter 调用：缺失能力诚实报错；同步/异步异常一律归一为
     { kind: "error" }，绝不向监听器泄漏未处理 rejection。 */
  const runAction = async (actionId, ...args) => {
    const adapter = capabilities[actionId];
    if (typeof adapter !== "function") {
      return { kind: "error", message: "该能力在当前环境不可用" };
    }
    try {
      const outcome = await adapter(...args);
      if (outcome && typeof outcome === "object" && typeof outcome.kind === "string") return outcome;
      return { kind: "success", message: "" };
    } catch (error) {
      return { kind: "error", message: error && error.message ? error.message : "操作失败，请稍后重试。" };
    }
  };

  const inDrawer = (el) => Boolean(el) && (el === drawer || el.closest?.(".life-cosmos-drawer") === drawer);

  /* R25 P1-9：抽屉内「打开文档」入口统一走 guarded item.openNote capability——
     只有 success 才关闭抽屉；失败保持抽屉并经 say 给出行内错误。 */
  const openNoteGuarded = async (path, say) => {
    const result = await runAction("item.openNote", path);
    if (result && result.kind === "success") {
      close();
      return;
    }
    if (typeof say === "function") say(result);
  };

  const restoreInert = () => {
    for (const el of inertTargets) el.removeAttribute?.("inert");
    inertTargets = [];
  };

  const close = () => {
    if (!opened) return;
    opened = false;
    knowledgeSelection = null;
    fragmentSelection = null;
    if (viewState) { viewState.knowledge = null; viewState.fragment = null; }
    for (const dispose of markdownDisposers.splice(0)) dispose();
    generation += 1; // 关闭后迟到的异步响应一律作废
    shell.removeClass("is-open");
    shell.removeClass("is-knowledge-reading");
    shell.setAttribute("aria-hidden", "true");
    if (shell.open && typeof shell.close === "function") shell.close();
    if (keydownHandler) {
      doc.removeEventListener?.("keydown", keydownHandler);
      keydownHandler = null;
    }
    restoreInert();
    const backTo = trigger;
    trigger = null;
    if (backTo && backTo.isConnected !== false && typeof backTo.focus === "function") backTo.focus();
  };
  shade.addEventListener("click", close);
  const onCancel = (event) => { event.preventDefault(); close(); };
  shell.addEventListener("cancel", onCancel);

  const onKeydown = (event) => {
    if (!opened) return;
    if (event.key === "Escape") {
      event.preventDefault?.();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const items = drawerFocusables(drawer);
    if (!items.length) {
      event.preventDefault?.();
      return;
    }
    const active = doc.activeElement;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey) {
      if (active === first || !inDrawer(active)) {
        event.preventDefault?.();
        last.focus?.();
      }
    } else if (active === last || !inDrawer(active)) {
      event.preventDefault?.();
      first.focus?.();
    }
  };

  const open = (triggerElement) => {
    close(); // 先收尾上一轮（恢复 inert/焦点/监听），再开启新一轮
    opened = true;
    generation += 1;
    trigger = triggerElement || null;
    shell.addClass("is-open");
    shell.setAttribute("aria-hidden", "false");
    if (shell.isConnected && typeof shell.showModal === "function") shell.showModal();
    inertTargets = [...(cosmos.children || [])].filter((child) => child !== shell);
    for (const el of inertTargets) el.setAttribute?.("inert", "");
    keydownHandler = onKeydown;
    doc.addEventListener?.("keydown", keydownHandler);
    drawerFocusables(drawer)[0]?.focus?.();
    return generation;
  };

  const show = (title, eyebrow, rows, actions = [], triggerElement) => {
    drawer.empty();
    const head = drawer.createDiv({ cls: "life-cosmos-drawer-head" });
    const labels = head.createDiv();
    labels.createEl("small", { text: eyebrow || "详情" });
    const heading = labels.createEl("h2", { text: title || "查看详情" });
    heading.setAttribute("id", "life-cosmos-drawer-title");
    shell.setAttribute("aria-labelledby", "life-cosmos-drawer-title");
    button(head, "关闭", "life-cosmos-drawer-close", close).setAttribute("aria-label", "关闭详情");
    const body = drawer.createDiv({ cls: "life-cosmos-drawer-body" });
    for (const row of (rows || []).filter(Boolean)) body.createEl("p", { text: row });
    const foot = drawer.createDiv({ cls: "life-cosmos-drawer-actions" });
    for (const action of actions) button(foot, action.label, action.primary ? "is-primary" : "", action.run);
    const current = open(triggerElement);
    return { body, foot, generation: current };
  };

  /* aria-live 结果行：成功/冲突/失败分态；失败绝不用成功颜色或完成文案。
     writeMessage 绑定到具体行元素——行级反馈是并发归属的基础。 */
  const writeMessage = (line) => (result) => {
    if (!result) return;
    const fallback = result.kind === "success" ? "已完成。"
      : result.kind === "conflict" ? "绑定已变化，未写入。" : "操作失败，请稍后重试。";
    line.setText(result.message || fallback);
    line.setAttribute("data-kind", result.kind);
  };
  const addMessageLine = (body) => {
    const line = body.createEl("p", { cls: "life-cosmos-drawer-message", text: "" });
    line.setAttribute("aria-live", "polite");
    return writeMessage(line);
  };

  /* 飞行中防重：真实 DOM 的 disabled 不派发点击，程序性 click 仍会进入，
     守卫必须在监听器内部。 */
  const actionButton = (parent, label, cls, run) => {
    const item = button(parent, label, cls, async () => {
      if (item.getAttribute("disabled") !== null) return;
      item.setAttribute("disabled", "disabled");
      try {
        await run();
      } finally {
        item.removeAttribute("disabled");
      }
    });
    return item;
  };

  const expandLegacy = (target) => {
    close();
    legacy?.addClass?.("is-open");
    target?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  };

  const renderConclusion = (parent, result, title = "核心结论", recommendationTitle = "建议") => {
    const values = [[title, result.summary], [recommendationTitle, result.recommendation]]
      .filter(([, value]) => typeof value === "string" && value.trim());
    if (!values.length) return;
    const card = parent.createEl("section", { cls: "life-research-conclusion" });
    for (const [label, value] of values) {
      card.createEl("h3", { text: label });
      if (value === result.summary && value.length > 400) card.createEl("small", { text: "以下保留完整摘要" });
      card.createEl("p", { cls: "life-knowledge-prose", text: value });
    }
    const gaps = Array.isArray(result.unknowns) ? result.unknowns.filter(value => typeof value === "string" && value.trim()) : [];
    if (gaps.length) card.createEl("small", { text: `仍有 ${gaps.length} 项记录缺口，见下方“仍未回答的事项”。` });
  };

  const renderBoundaries = (parent, result) => {
    for (const [key, title] of [["unknowns", "仍未回答的事项"], ["limitations", "结论的适用范围"]]) {
      const values = Array.isArray(result[key]) ? result[key].filter(value => typeof value === "string" && value.trim()) : [];
      if (!values.length) continue;
      const details = parent.createEl("details", { cls: `life-research-${key}` });
      details.createEl("summary", { text: `${title}（${values.length} 项）` });
      if (key === "unknowns") details.createEl("p", { text: "以下是本记录保留的缺口，不会自动成为你的待办；此处也不表示系统已安排补查。" });
      const list = details.createEl("ul");
      for (const value of values) list.createEl("li", { text: value });
    }
  };

  const renderAnswer = (parent, markdown, sourcePath = "", expanded = false) => {
    const details = parent.createEl("details", { cls: "life-knowledge-answer" });
    details.createEl("summary", { text: "详细解读与依据" });
    const target = details.createDiv({ cls: "life-knowledge-prose life-knowledge-markdown" });
    target.setText(markdown); // 原文始终保留；展开且布局可见后才渲染图文。
    let started = false;
    let finished;
    const current = generation;
    const enclosingDetails = parent.closest?.("details");
    const render = () => {
      if (!details.open || (enclosingDetails && !enclosingDetails.open) || started || !opened || current !== generation) return finished;
      started = true;
      const adapter = capabilities["knowledge.renderMarkdown"];
      if (typeof adapter !== "function") return;
      let handle;
      try { handle = adapter(markdown, target, sourcePath); }
      catch { return; }
      if (!handle) return;
      markdownDisposers.push(handle.dispose);
      finished = Promise.resolve(handle.finished).catch(() => {
        handle.dispose();
        if (target.isConnected !== false) target.createEl("small", { text: "图文暂时无法渲染，已保留完整原文。" });
      });
      return finished;
    };
    details.addEventListener("toggle", () => { render(); rememberKnowledgePosition(); });
    enclosingDetails?.addEventListener("toggle", render);
    details.open = expanded;
    return render();
  };

  const showKnowledge = async (note, triggerElement, restore) => {
    const shown = show(note.title, "知识 · 结论与依据", ["正在读取完整知识…"], [], triggerElement);
    shell.addClass("is-knowledge-reading");
    const current = shown.generation;
    knowledgeReadingPosition = restore || { scrollTop: 0 };
    restoringKnowledgePosition = Boolean(restore);
    knowledgeSelection = restore ? { knowledge_id: note.knowledge_id, title: note.title, revision: restore.revision } : null;
    try {
      if (typeof sources.readKnowledge !== "function") throw new Error("知识详情尚未接入");
      const record = await sources.readKnowledge(note.knowledge_id, restore?.revision);
      if (!opened || generation !== current || cosmos.isConnected === false) return;
      const body = shown.body;
      body.empty();
      knowledgeSelection = { knowledge_id: record.knowledge_id, title: record.title, revision: record.revision };
      const markdownRenders = [];
      if (restore) body.createEl("p", { cls: "life-cosmos-projection-update", text: `已恢复上次阅读的修订 ${record.revision}；此处保留该版本，当前结论请从目录重新打开。` });
      const result = record.result;
      const freshness = renderKnowledgeFreshness(body, record);
      if (freshness.derived && record.input_summary_only === true) {
        body.createEl("p", { cls: "life-knowledge-prose", text: `${record.kind === "period" ? "本期" : "本条"}按全部研究的摘要整理；全部研究身份保留，完整事实依据与适用限制请查看原研究。` });
      }
      let content = body;
      if (freshness.derived || !freshness.usable) {
        content = body.createEl("details", { cls: "life-knowledge-history" });
        content.createEl("summary", { text: freshness.derived
          ? "结构整理原文（仅作导航，不作为当前结论）" : "历史研究原文（不作为当前结论）" });
        content.open = restore?.historyExpanded === true;
        content.addEventListener("toggle", rememberKnowledgePosition);
      }
      renderConclusion(content, result, freshness.derived ? "结构整理摘要" : freshness.usable ? "核心结论" : "研究摘要（当前不可用）",
        freshness.derived ? "原整理建议（以源研究为准）" : "建议");
      if (record.independent_review) renderIndependentReview(content, record.independent_review);
      else if (record.content_status !== "theme_summary") content.createEl("small", { text: "此版本没有独立模型复核回执；请结合原始证据和限制使用。" });
      const status = { qualified_conclusion: "已形成结论", conflicted: "存在冲突", theme_summary: "主题梳理" }[record.content_status] || "状态未标注";
      content.createEl("p", { cls: "life-knowledge-status", text: `${status} · 修订 ${record.revision} · ${formatGraphTime(record.updated_at)}` });
      const section = (title, value, parent = content) => {
        if (typeof value !== "string" || !value.trim()) return;
        parent.createEl("h3", { text: title });
        parent.createEl("p", { cls: "life-knowledge-prose", text: value });
      };
      const list = (title, entries, parent = content) => {
        if (!Array.isArray(entries) || !entries.length) return;
        parent.createEl("h3", { text: title });
        const ul = parent.createEl("ul");
        for (const item of entries) {
          const value = typeof item === "string" ? item : item?.claim || item?.question || "";
          if (value) ul.createEl("li", { text: value });
        }
      };
      if (freshness.derived) {
        const canonical = body.createDiv({ cls: "life-knowledge-canonical" });
        body.insertBefore(canonical, content);
        canonical.createEl("h3", { text: "源研究 · 当前结论与限制" });
        const records = Array.isArray(record.canonical_research) ? record.canonical_research : [];
        if (!records.length) canonical.createEl("p", { text: "当前源研究未能读取；结构整理原文不能替代研究判断。" });
        for (const source of records) {
          const card = canonical.createEl("article", { cls: "life-knowledge-canonical-record" });
          card.createEl("h4", { text: `${source.title} · 修订 ${source.revision}` });
          const sourceState = renderKnowledgeFreshness(card, source);
          const sourceContent = sourceState.usable ? card : card.createEl("details", { cls: "life-knowledge-source-history" });
          if (!sourceState.usable) sourceContent.createEl("summary", { text: "源研究历史内容（当前不可用）" });
          if (source.independent_review) renderIndependentReview(sourceContent, source.independent_review);
          const sourceResult = source.result || {};
          renderConclusion(sourceContent, sourceResult, sourceState.usable ? "源研究结论" : "源研究历史摘要（当前不可用）", "源研究建议");
          if (Array.isArray(sourceResult.confirmed) && sourceResult.confirmed.length) {
            const checks = sourceContent.createEl("details", { cls: "life-knowledge-checks" });
            checks.createEl("summary", { text: "核对事项" });
            list("源研究已确认事项", sourceResult.confirmed, checks);
          }
          renderBoundaries(sourceContent, sourceResult);
          if (sourceResult.agent_usage) {
            const reuse = sourceContent.createEl("details", { cls: "life-knowledge-reuse" });
            reuse.createEl("summary", { text: "复用说明" });
            section("Agent 使用场景", sourceResult.agent_usage.when_to_use, reuse);
            list("可复用步骤", sourceResult.agent_usage.steps, reuse);
            list("使用边界", sourceResult.agent_usage.limitations, reuse);
          }
          button(card, "查看源研究完整依据", "", trigger => showKnowledge(source, trigger));
        }
      }
      if (record.kind === "period") {
        section("整理范围", record.summary_scope?.title);
        section("覆盖周期", [record.period, record.start, record.end].filter(value => typeof value === "string").join(" · "));
      }
      if (record.kind === "period") {
        const analysis = record.analysis || {};
        const refs = (parent, value) => {
          for (const ref of Array.isArray(value) ? value : []) {
            if (!ref || !/^(knowledge|period)-[a-f0-9]{24}$/.test(ref.knowledge_id) || !Number.isInteger(ref.revision)) continue;
            const title = (record.canonical_research || []).find(source => source.knowledge_id === ref.knowledge_id)?.title || "源研究";
            button(parent, `打开当前${title}（整理引用修订 ${ref.revision}）`, "", trigger => showKnowledge({ knowledge_id: ref.knowledge_id, title }, trigger));
          }
        };
        for (const [key, title] of [["known_structure", "已形成的知识结构"], ["connections", "跨主题联系"]]) {
          if (!Array.isArray(analysis[key]) || !analysis[key].length) continue;
          content.createEl("h3", { text: title });
          for (const item of analysis[key]) {
            if (typeof item?.statement !== "string") continue;
            const entry = content.createDiv(); entry.createEl("p", { text: item.statement }); refs(entry, item.knowledge_refs);
          }
        }
        if (Array.isArray(analysis.gaps) && analysis.gaps.length) {
          content.createEl("h3", { text: "知识缺口与目标依据" });
          for (const gap of analysis.gaps) {
            if (typeof gap?.goal !== "string") continue;
            const entry = content.createDiv(); entry.createEl("h4", { text: gap.goal });
            if (typeof gap.basis === "string") entry.createEl("p", { text: gap.basis });
            refs(entry, gap.knowledge_refs);
          }
        }
        if (Array.isArray(record.goals) && record.goals.length) {
          content.createEl("h3", { text: "目标清单" });
          for (const goal of record.goals) {
            if (typeof goal?.goal !== "string") continue;
            const entry = content.createDiv(); entry.createEl("p", { text: goal.goal });
            entry.createEl("small", { text: [goal.priority == null ? "未设优先级" : `优先级 ${String(goal.priority)}`,
              goal.collection_mode === "natural_collection" ? "自然收集" : "收集模式见完整记录",
              goal.proactive_research_authorized === true ? "已授权主动研究" : "未授权主动研究"].join(" · ") });
          }
        }
        if (Array.isArray(analysis.revisions) && analysis.revisions.length) {
          content.createEl("h3", { text: "本次结构修订" });
          for (const change of analysis.revisions) {
            const entry = content.createDiv();
            for (const [key, label] of [["previous_statement", "此前"], ["updated_statement", "本次"], ["reason", "原因"]]) {
              if (typeof change?.[key] === "string") entry.createEl("p", { text: `${label}：${change[key]}` });
            }
            refs(entry, change?.knowledge_refs);
          }
        }
      }
      if (typeof result.answer_markdown === "string" && result.answer_markdown.trim()) {
        markdownRenders.push(renderAnswer(content, result.answer_markdown, record.path, restore?.answerExpanded === true));
      }
      if ((Array.isArray(result.coverage) && result.coverage.length) || (Array.isArray(result.confirmed) && result.confirmed.length)) {
        const checks = content.createEl("details", { cls: "life-knowledge-checks" });
        checks.createEl("summary", { text: "逐项回答与核对事项" });
        if (Array.isArray(result.coverage) && result.coverage.length) {
          checks.createEl("h3", { text: "逐项回答" });
          for (const item of result.coverage) {
            checks.createEl("h4", { text: item.question || "问题未标注" });
            checks.createEl("p", { cls: "life-knowledge-prose", text: item.answer || "尚未回答" });
            if (Array.isArray(item.evidence_ids) && item.evidence_ids.length) checks.createEl("small", { text: `依据：${item.evidence_ids.join("、")}` });
          }
        }
        list(freshness.derived ? "整理列出的事项（需以源研究核对）" : "已确认事项", result.confirmed, checks);
      }
      renderBoundaries(content, result);
      list("冲突", Array.isArray(result.conflicts) ? result.conflicts.map(item => {
        if (typeof item === "string") return item;
        if (typeof item?.topic !== "string") return "";
        const dimensions = Array.isArray(item.dimensions) ? item.dimensions.filter(value => typeof value === "string") : [];
        const evidence = Array.isArray(item.evidence_ids) ? item.evidence_ids.filter(value => typeof value === "string") : [];
        return `${item.topic}：${dimensions.join("；")}${evidence.length ? `（依据：${evidence.join("、")}）` : ""}`;
      }) : []);
      if (result.agent_usage && typeof result.agent_usage === "object") {
        const reuse = content.createEl("details", { cls: "life-knowledge-reuse" });
        reuse.createEl("summary", { text: "复用说明" });
        section("Agent 使用场景", result.agent_usage.when_to_use, reuse);
        list("可复用步骤", result.agent_usage.steps, reuse);
        list("使用边界", result.agent_usage.limitations, reuse);
      }
      const sourceDetails = content.createEl("details", { cls: "life-knowledge-sources" });
      sourceDetails.createEl("summary", { text: `来源与修订（${record.evidence.length} 条来源）` });
      for (const evidence of record.evidence) {
        const source = sourceDetails.createEl("details", { cls: "life-knowledge-evidence" });
        source.createEl("summary", { text: evidence.title || evidence.evidence_id || "未命名来源" });
        const reference = evidence.url || evidence.source_ref;
        let external = null;
        try { const url = new URL(reference); if (["https:", "http:"].includes(url.protocol)) external = url.href; } catch { /* 本地引用保留文本 */ }
        if (external) {
          const link = source.createEl("a", { text: "打开来源" });
          link.setAttribute("href", external);
          link.setAttribute("target", "_blank");
          link.setAttribute("rel", "noopener noreferrer");
        } else if (typeof reference === "string") source.createEl("small", { text: reference });
        const excerpts = Array.isArray(evidence.excerpts) ? evidence.excerpts : [];
        for (const excerpt of excerpts) if (typeof excerpt === "string") source.createEl("p", { cls: "life-knowledge-prose", text: excerpt });
      }
      if (!record.evidence.length) content.createEl("p", { text: "该条记录没有附带独立来源，请查看完整记录中的引用范围。" });
      section("本次修订原因", record.revision_reason, sourceDetails);
      const say = addMessageLine(body);
      button(shown.foot, "在 Obsidian 打开完整记录", "is-primary", () => openNoteGuarded(record.path, say));
      if (restore) {
        await Promise.all(markdownRenders);
        if (opened && generation === current && cosmos.isConnected !== false) {
          const diagrams = [...drawer.querySelectorAll(".life-knowledge-flow")];
          diagrams.forEach((element, index) => { element.scrollLeft = restore.diagramScrollLeft?.[index] || 0; });
          diagrams[restore.focusedDiagram]?.focus?.({ preventScroll: true });
          drawer.scrollTop = restore.scrollTop || 0;
          restoringKnowledgePosition = false;
          rememberKnowledgePosition();
        }
      }
    } catch (error) {
      if (!opened || generation !== current || cosmos.isConnected === false) return;
      shown.body.empty();
      shown.body.createEl("p", { cls: "life-cosmos-graph-error", text: `知识读取失败：${error?.message || "服务不可用"}` });
      button(shown.body, "重试读取", "", () => showKnowledge(note, triggerElement, restore));
    }
  };

  /* ---------- Graph：六态 + 最近运行 + Checkpoint 安全摘要 ---------- */
  const showGraph = (triggerElement) => {
    const { body, foot } = show("Graph 工作流", "Graph · 状态与安全摘要", [], [], triggerElement);
    const say = addMessageLine(body);
    const stateBox = body.createDiv({ cls: "life-cosmos-graph-statebox" });
    const renderState = () => {
      stateBox.empty();
      const { state, lastGood } = sources.graph ? sources.graph() : { state: null, lastGood: null };
      const ready = state && state.kind === "ready" ? state : null;
      // G8：抽屉同样识别刷新失败（ready + stale）为服务错误标注。
      const error = state && (state.kind === "error" || state.stale) ? state : null;
      const usable = ready || (lastGood && lastGood.kind === "ready" ? lastGood : null);
      if (error) {
        stateBox.createEl("p", { cls: "life-cosmos-graph-error", text: `服务不可用：${error.message || "Graph 服务暂时不可用"}` });
        if (usable) stateBox.createEl("p", { cls: "life-cosmos-graph-stale", text: "以下为最近一次成功读取；刷新失败不会清空旧摘要。" });
      }
      if (!usable) {
        if (!error) stateBox.createEl("p", { text: "正在读取 Graph 运行摘要…" });
        return;
      }
      const summary = computeGraphSummary(usable.runs);
      if (!summary.total) {
        stateBox.createEl("p", { text: "暂无 Graph 运行" });
        return;
      }
      const list = stateBox.createEl("ul", { cls: "life-cosmos-graph-six" });
      for (const [label, value] of [
        ["进行中", summary.active], ["等待人工", summary.waiting], ["异常停止", summary.abnormal],
        ["流程已结束", summary.completed], ["未知状态", summary.unknown],
      ]) {
        const row = list.createEl("li");
        row.createEl("b", { text: String(value) });
        row.createSpan({ text: label });
      }
      if (summary.latest) {
        stateBox.createEl("p", {
          cls: "life-cosmos-graph-latest",
          text: `最近运行：${summary.latest.statusLabel} · ${formatGraphTime(summary.latest.updatedAt)} · Checkpoint #${summary.latest.sequence}`,
        });
        /* 技术 ID 不进主视觉：run id 只在折叠审计区，并作为打开动作的显式身份。 */
        const audit = stateBox.createEl("details", { cls: "life-cosmos-drawer-audit" });
        audit.createEl("summary", { text: "审计信息（技术标识）" });
        audit.createEl("p", { text: `run_id：${summary.latest.runId || "—"}` });
        const openRunBtn = actionButton(audit, "打开最近运行", "", async () => {
          say(await runAction("graph.openRun", summary.latest.runId));
        });
        /* F-D10：runId 缺失时禁用，与 "—" 文案一致。 */
        if (!summary.latest.runId) openRunBtn.setAttribute("disabled", "disabled");
      } else {
        stateBox.createEl("p", { text: "运行缺少可靠时间，无法判断最近运行。" });
      }
    };
    renderState();
    actionButton(foot, "打开 Graph 工作流", "is-primary", async () => {
      say(await runAction("graph.openWorkflow"));
    });
    actionButton(foot, "刷新", "", async () => {
      const result = await runAction("graph.refresh");
      say(result);
      renderState();
    });
  };

  /* ---------- 处理方向 / Continuation / 升级提案 / Harvest ---------- */
  const intentFingerprint = (item) =>
    [item.fragment_id, item.sequence, item.alignment_id].map((part) => String(part ?? "")).join("|");

  /* P1-1 修复（fragment-autonomy-audit-v1）：抽屉复用 cognitiveOfExecution
     认知轴（:596）。「继续核验/补充来源/保持结果」三选项只对真
     search_exhausted（硬规则：网络请求 >0 且计划搜索耗尽）开放；
     capability_offline/collecting/watching/not_started 是系统状态而非
     用户任务——渲染与卡面一致的诚实说明，无继续核验主按钮、不要求
     用户补来源。 */
  const COGNITIVE_SYSTEM_STATES = new Set(["not_started", "capability_offline", "collecting", "researching", "watching", "evidence_ready", "synthesis_disabled", "watch_budget_exhausted"]);

  const continueIntent = async (item, goal, say) => {
    const value = String(goal || "").trim();
    if (!value) {
      say({ kind: "error", message: "请先写下这次继续处理的目标。" });
      return;
    }
    const gen = generation;
    const fp = intentFingerprint(item);
    const result = await runAction("intent.continue", item, value);
    // 迟到守卫：抽屉关闭/重开或目标身份改变后，响应一律丢弃。
    if (!isOpen() || gen !== generation || fp !== intentFingerprint(item)) return;
    say(result);
    return result;
  };

  const renderContinuation = (box, item, say, evidenceMissing = false, onAdvance = null) => {
    const execution = item.execution || null;
    if (!execution || execution.status !== "passed" || !execution.result_digest) {
      box.createEl("small", { text: "当前没有可用的接续入口。" });
      return;
    }
    const goal0 = item.decision && item.decision.supplement
      ? `${item.title}（补充：${item.decision.supplement}）`
      : item.title;
    if (evidenceMissing) {
      const choices = box.createDiv({ cls: "life-cosmos-next-actions" });
      choices.createEl("strong", { text: "核验结果" });
      choices.createEl("p", { cls: "life-cosmos-verification-result", text: "本轮未找到足以支持当前判断的可靠来源。" });
      const advice = choices.createDiv({ cls: "life-cosmos-verification-advice" });
      advice.createEl("b", { text: "系统建议" });
      advice.createEl("p", { text: "保持为未验证想法；只有这条信息仍值得投入时，再继续核验。" });
      const row = choices.createDiv({ cls: "life-cosmos-drawer-actions" });
      const automatic = actionButton(row, "继续核验", "is-primary", async () => {
        const result = await continueIntent(
          item,
          `继续核验“${item.title || item.fragment_id}”的官方来源；优先检查与当前主张直接相关的官方发布页、官方仓库或一手资料，若仍无证据则明确停止原因。`,
          say,
        );
        if (result?.kind === "success" && result.item && typeof onAdvance === "function") onAdvance(result.item);
      });
      automatic.setAttribute("data-cosmos-action", "intent.continue");
      const supplement = box.createEl("details", { cls: "life-cosmos-source-supplement" });
      supplement.createEl("summary", { text: "补充来源或核验要求" });
      const input = supplement.createEl("textarea", { cls: "life-cosmos-cont-goal" });
      input.setAttribute("placeholder", "粘贴官方页面或仓库链接，也可以补充核验要求");
      input.setAttribute("maxlength", "200");
      input.setAttribute("aria-label", "新的处理目标");
      const submit = actionButton(supplement, "补充后继续", "life-cosmos-cont-submit", async () => {
        const result = await continueIntent(item, input.value, say);
        if (result?.kind === "success" && result.item && typeof onAdvance === "function") onAdvance(result.item);
      });
      submit.setAttribute("data-cosmos-action", "intent.continue");
      button(row, "补充来源", "", () => {
        supplement.open = true;
        if (typeof input.focus === "function") input.focus();
      });
      button(row, "保持当前结果", "", close);
      const details = box.createEl("details", { cls: "life-cosmos-verification-details" });
      details.createEl("summary", { text: "查看核验依据" });
      details.createEl("small", { cls: "life-cosmos-cont-bind", text: `原目标：${goal0 || item.fragment_id} → 新问题 → 将创建子 episode` });
      if (execution.result?.summary) details.createEl("p", { text: execution.result.summary });
      if (Array.isArray(execution.result?.unknowns) && execution.result.unknowns.length) {
        const list = details.createEl("ul");
        for (const unknown of execution.result.unknowns) list.createEl("li", { text: unknown });
      }
      return;
    }
    box.createEl("small", { cls: "life-cosmos-cont-bind", text: `原目标：${goal0 || item.fragment_id} → 新问题 → 将创建子 episode` });
    const input = box.createEl("textarea", { cls: "life-cosmos-cont-goal" });
    input.setAttribute("placeholder", "写下新的处理目标");
    input.setAttribute("maxlength", "200");
    input.setAttribute("aria-label", "新的处理目标");
    const submit = actionButton(box, "创建后续处理", "life-cosmos-cont-submit", async () => {
      const result = await continueIntent(item, input.value, say);
      if (result?.kind === "success" && result.item && typeof onAdvance === "function") onAdvance(result.item);
    });
    submit.setAttribute("data-cosmos-action", "intent.continue");
  };

  const renderIntentRow = (body, item, onAdvance = null, showTitle = true) => {
    const box = body.createDiv({ cls: "life-cosmos-intent-row" });
    box.setAttribute("data-fragment-id", item.fragment_id || "");
    if (showTitle) box.createEl("strong", { text: item.title || item.fragment_id || "未命名碎片" });
    const pendingReview = independentReviewState(item.execution);
    const publication = pendingReview ? null : knowledgePublicationOf(item.execution);
    const concluded = hasResearchConclusion(item.execution);
    const published = Boolean(publication && concluded
      && !item.execution?.research_progress?.blocker
      && [undefined, "synthesized"].includes(item.execution?.research_progress?.stage)
      && [undefined, "synthesized", "conflicted"].includes(item.execution?.research_progress?.cognitive));
    const publicationUnavailable = item.execution?.knowledge_publication?.status === "unavailable"
      && item.execution.knowledge_publication.result_digest === researchResultDigestOf(item.execution);
    const route = { save_only: "仅保存", direct: "直接处理", verify: "补充核验", graph: "准备升级 Graph" }[item.route];
    box.createEl("small", { text: `状态：${pendingReview ? pendingReview.label : publicationUnavailable ? "已存笔记发生变动或无法读取" : publication ? "结论已自动沉淀" : concluded ? "结论已形成，保存状态待确认" : item.status === "suggested" ? "等待你确认方向" : route || "已确认"}` });
    /* 行级反馈：每个 fragment 绑定自己的消息行。同抽屉并发接续（A 慢、
       B 快）时，A 的迟到结果只落 A 的行，绝不覆盖 B。 */
    const say = addMessageLine(box);

    if (item.status === "suggested") {
      const options = box.createDiv({ cls: "life-cosmos-intent-options" });
      const all = [
        ...item.suggested_intents.map((id) => ({ id, label: id })),
        ...item.dynamic_intents.map((entry) => ({ id: entry.id, label: entry.label || entry.id })),
      ];
      for (const entry of all) {
        const label = options.createEl("label");
        const input = label.createEl("input");
        input.setAttribute("type", "checkbox");
        input.setAttribute("data-intent-id", entry.id);
        /* 真实 checked property：用户取消勾选只改 property，attribute 不动；
           提交只读 property，绝不凭 attribute 把已取消的选择提交上去。 */
        input.checked = true;
        label.createEl("span", { text: entry.label });
      }
      const supplement = box.createEl("textarea", { cls: "life-cosmos-cont-goal" });
      supplement.setAttribute("placeholder", "补充你希望关注的方向（可选）");
      supplement.setAttribute("maxlength", "500");
      const row = box.createDiv({ cls: "life-cosmos-drawer-actions" });
      actionButton(row, "确认并继续", "is-primary", async () => {
        const intents = [];
        for (const input of options.querySelectorAll("input")) {
          if (input.checked === true) intents.push(input.getAttribute("data-intent-id"));
        }
        if (!intents.length) {
          say({ kind: "error", message: "至少选择一个处理方向，或选择仅保存。" });
          return;
        }
        const gen = generation;
        const result = await runAction("intent.confirm", item, intents, supplement.value || "");
        if (!isOpen() || gen !== generation) return;
        say(result);
      });
      actionButton(row, "仅保存", "", async () => {
        const gen = generation;
        const result = await runAction("intent.save", item);
        if (!isOpen() || gen !== generation) return;
        say(result);
      });
      return;
    }

    const execution = item.execution || null;
    const subscriptionLabel = subscriptionExecutionLabel(execution);
    if (!published) {
      if (subscriptionLabel) box.createEl("small", { cls: "fragment-subscription-scope", text: subscriptionLabel });
      renderEffectiveExecutionScope(box, item);
    }
    if (pendingReview) { renderIndependentReviewPending(box, execution, pendingReview); return; }
    if (publication || concluded) {
      const result = execution.result || {};
      if (!published && execution.research_progress) {
        const progress = execution.research_progress;
        const state = cognitiveOfExecution({ research_progress: progress, stop_reason: execution.stop_reason });
        const view = COGNITIVE_JOURNEY[state];
        box.createEl("p", { cls: "life-cosmos-verification-result", text: `当前处理状态：${view?.statusLabel || "待确认"}；${progress.note || view?.nextAction || "请查看当前执行回执。"}` });
      }
      renderConclusion(box, result);
      renderIndependentReview(box, execution?.independent_review);
      if (publication) {
        const actions = box.createDiv({ cls: "life-cosmos-drawer-actions" });
        actionButton(actions, "查看完整知识与依据", "is-primary", () => showKnowledge({ ...publication, title: item.title }, trigger));
        actionButton(actions, "在 Obsidian 打开已沉淀知识", "", () => openNoteGuarded(publication.path, say));
      }
      if (typeof result.answer_markdown === "string" && result.answer_markdown.trim()) {
        renderAnswer(box, result.answer_markdown, publication?.path || "");
      }
      renderBoundaries(box, result);
      if (result.conflicts?.length) {
        box.createEl("h3", { text: "来源冲突" });
        for (const conflict of result.conflicts) box.createEl("p", { text: `${conflict.topic || ""}：${(conflict.dimensions || []).join("；")}` });
      }
      const records = Array.isArray(execution.research_evidence) ? execution.research_evidence : [];
      if (records.length) {
        const sources = box.createEl("details", { cls: "fragment-research-sources" });
        sources.createEl("summary", { text: `证据与来源（${records.length} 条）` });
        const list = sources.createEl("ul");
        for (const record of records) list.createEl("li", { text: `${record.title || "来源"} — ${record.url || ""}` });
      }
      box.createEl("small", { text: publication
        ? `已自动保存到知识库 · 修订 ${publication.revision}；未经过人工审核。`
        : publicationUnavailable ? "已存笔记发生变动或无法读取，原结论保留；不会覆盖你的文件。"
          : "尚未取得本次知识保存回执；结论保留在此，无需你审批候选或重填目标。" });
      if (publicationUnavailable && execution.knowledge_publication.reason) box.createEl("p", { text: execution.knowledge_publication.reason });
      if (published) {
        const receipt = box.createEl("details", { cls: "fragment-execution-details" });
        receipt.createEl("summary", { text: "执行回执与原始提案" });
        if (subscriptionLabel) receipt.createEl("small", { cls: "fragment-subscription-scope", text: subscriptionLabel });
        renderEffectiveExecutionScope(receipt, item);
        if (execution.run_id) receipt.createEl("p", { text: `运行标识：${execution.run_id}` });
        const proposal = receipt.createEl("details");
        proposal.createEl("summary", { text: "原始处理提案（不代表实际执行）" });
        for (const value of [item.reasoning, item.plan, item.expected_result]) {
          if (typeof value === "string" && value) proposal.createEl("p", { text: value });
        }
      }
      return;
    }
    renderIndependentReview(box, execution?.independent_review);
    const cognitive = execution && execution.route === "verify"
      ? cognitiveOfExecution(execution)
      : null;
    const evidenceMissing = cognitive === "search_exhausted";
    if (evidenceMissing) renderContinuation(box, item, say, true, onAdvance);
    if (cognitive && (COGNITIVE_SYSTEM_STATES.has(cognitive) || cognitive === "awaiting_model_authorization")) {
      const view = COGNITIVE_JOURNEY[cognitive] || COGNITIVE_JOURNEY.collecting;
      box.createEl("p", {
        cls: "life-cosmos-verification-result",
        text: `${view.statusLabel}：${view.nextAction}`,
      });
    }
    if (!evidenceMissing && execution && execution.result && execution.result.summary) {
      box.createEl("p", { text: execution.result.summary });
    }
    if (execution && Array.isArray(execution.harvest) && execution.harvest.length) {
      const harvest = box.createEl("details", { cls: "life-cosmos-harvest" });
      harvest.createEl("summary", { text: `经验候选（${execution.harvest.length} 条，尚未成为知识资产）` });
      const list = harvest.createEl("ul");
      for (const entry of execution.harvest) {
        const prefix = entry.role === "failure" ? "失败模式：" : "";
        list.createEl("li", { text: `${prefix}${entry.summary}（${entry.maturity || "候选"}）` });
      }
      harvest.createEl("small", { text: "只有人工确认协议能把候选形成资产，此处不提供捷径。" });
    }
    // 能力离线时与卡面一致不提供任何接续入口（fragment-intent-card.js
    // renderConfirmed 对 capability_offline 同样抑制接续）。
    if (!evidenceMissing && !["capability_offline", "synthesis_disabled", "watch_budget_exhausted"].includes(cognitive)) {
      const followup = execution?.status === "passed"
        ? box.createEl("details", { cls: "fragment-intent-continuation" }) : box;
      if (followup !== box) followup.createEl("summary", { text: "继续处理这个主题（可选）" });
      renderContinuation(followup, item, say, false, onAdvance);
    }
    const proposal = execution && execution.graph_escalation;
    if (proposal && proposal.status === "proposed") {
      const row = box.createDiv({ cls: "life-cosmos-drawer-actions" });
      if (researchProposalReady(item)) {
        row.createEl("small", { text: "已有升级提案（尚未执行）；确认后才会创建研究 Run。" });
        const create = actionButton(row, "创建 Graph 研究 Run", "is-primary", async () => {
          const gen = generation;
          const result = await runAction("graphProposal.create", item);
          if (!isOpen() || gen !== generation) return;
          say(result);
        });
        create.setAttribute("data-cosmos-action", "graphProposal.create");
      } else {
        actionButton(row, "提出 Graph 升级", "", async () => {
          const gen = generation;
          const result = await runAction("intent.escalate", item);
          if (!isOpen() || gen !== generation) return;
          say(result);
        });
      }
    }
  };

  const showIntents = async (triggerElement) => {
    const { body, generation: current } = show("处理方向与接续", "接续 · 意图与收获", [], [], triggerElement);
    if (typeof sources.refreshResearch === "function") {
      body.createEl("p", { text: "正在读取当前研究状态…" });
      await sources.refreshResearch();
      if (!isOpen() || generation !== current) return;
      body.empty();
    }
    const data = sources.intents ? sources.intents() : { items: [], error: "" };
    if (data.error) body.createEl("p", { cls: "life-cosmos-graph-error", text: `处理方向服务暂不可用：${data.error}` });
    if (!data.items.length && !data.error) {
      body.createEl("p", { text: "当前尚无处理方向记录；系统承接状态见碎片全景。" });
    }
    /* 同一 fragment/case 的多个 alignment 按权威 Checkpoint sequence 最大值
       确定性选 newest（alignment_id 字典序兜底），绝不从列表顺序猜测。 */
    const fragmentIds = [...new Set(data.items.map((item) => item.fragment_id).filter(Boolean))];
    for (const fragmentId of fragmentIds.slice(0, 6)) {
      const item = alignmentFor(data.items, fragmentId);
      /* 聚合抽屉允许多碎片并发接续，保留各自行级回执；单卡抽屉才在成功后
         切换到新 episode，避免重绘抹掉另一行的并发结果。 */
      if (item) renderIntentRow(body, item);
    }
  };

  const showIntent = async (item, sourceElement, triggerElement, fresh = false) => {
    if (!fresh && typeof sources.refreshResearch === "function") {
      const loading = show(item.title || "碎片处理", "正在读取当前研究状态", [], [], triggerElement);
      if (item.status === "passed") fragmentSelection = { fragment_id: item.fragment_id };
      await sources.refreshResearch();
      if (!isOpen() || generation !== loading.generation) return;
      const data = sources.intents();
      if (data.error) { loading.body.createEl("p", { text: `当前状态读取失败：${data.error}` }); return; }
      const latest = alignmentFor(data.items, item.fragment_id);
      if (!latest) { loading.body.createEl("p", { text: "当前已无这条处理记录；不能沿用此前状态。" }); return; }
      return showIntent(latest, sourceElement, triggerElement, true);
    }
    const { body, foot } = show(
      item.title || item.fragment_id || "碎片处理",
      "研究 · 结论与下一步",
      [],
      [],
      triggerElement,
    );
    renderIntentRow(body, item, (nextItem) => showIntent(nextItem, sourceElement, triggerElement, true), false);
    if (item.source_origin === "raw_capture") {
      const origin = knowledgePublicationOf(item.execution) && hasResearchConclusion(item.execution)
        ? body.createEl("details", { cls: "fragment-input-origin" }) : body;
      if (origin !== body) origin.createEl("summary", { text: "原始输入与研究路径" });
      origin.createEl("p", { text: "系统从原始公开碎片直接研究；没有以整理文档作为研究前提。" });
    }
    // 只恢复只读研究详情；带待填表单的操作舱不套用此读取恢复路径。
    if (item.status === "passed" && !body.querySelector("textarea")) fragmentSelection = { fragment_id: item.fragment_id };
    const rawCard = sourceElement && hasClass(sourceElement, "life-capture-card");
    const refs = rawCard ? cardFragmentRefs(sourceElement) : null;
    const documents = rawCard
      ? [["打开原始记录", refs.rawRef], ["查看整理结果", refs.organizedRef]]
      : [["打开整理文档", sourceElement ? linkPath(sourceElement) : ""]];
    if (openNote) {
      const say = addMessageLine(body);
      for (const [label, path] of documents) if (path) actionButton(foot, label, "", () => openNoteGuarded(path, say));
    }
  };

  /* ---------- Graph Pilot：资格判定 + 既有确认页，绝不按 DOM 顺序 ---------- */
  const showPilot = (triggerElement) => {
    const { body } = show("Graph Pilot", "PILOT · 候选转换", [], [], triggerElement);
    const say = addMessageLine(body);
    const reviews = sources.reviews ? sources.reviews() : { items: [], error: "" };
    const graph = sources.graph ? sources.graph() : { state: null, lastGood: null };
    const runs = graph.state && graph.state.kind === "ready" ? graph.state.runs
      : graph.lastGood && graph.lastGood.kind === "ready" ? graph.lastGood.runs : [];
    if (reviews.error) body.createEl("p", { cls: "life-cosmos-graph-error", text: `候选服务暂不可用：${reviews.error}` });
    if (!reviews.items.length && !reviews.error) body.createEl("p", { text: "当前没有可转换的候选。" });
    for (const candidate of reviews.items.slice(0, 6)) {
      const box = body.createDiv({ cls: "life-cosmos-pilot-row" });
      box.createEl("strong", { text: candidate.title || candidate.fragment_ref || "未命名候选" });
      const bridged = pilotRunForFragment(runs, normalizeFragmentBasename(candidate.fragment_ref));
      if (bridged) {
        box.createEl("small", { text: "已进入 Graph · 等待你决定" });
        const openRun = actionButton(box, "已进入 Graph 工作流 →", "is-primary", async () => {
          say(await runAction("pilot.openRun", bridged.run_id));
        });
        openRun.setAttribute("data-cosmos-action", "pilot.openRun");
        continue;
      }
      const eligibility = candidateEligibility(candidate);
      if (!eligibility.ok) {
        box.createEl("small", { text: `候选尚未就绪：${eligibility.reason}` });
        continue;
      }
      const convert = actionButton(box, "转为 Graph 工作流", "is-primary", async () => {
        say(await runAction("pilot.openPlan", candidate));
      });
      convert.setAttribute("data-cosmos-action", "pilot.openPlan");
    }
  };

  /* ---------- Loop 人工确认：只列候选，决定全部走既有 review 协议 ---------- */
  const showReview = (triggerElement) => {
    const { body } = show("Loop 人工确认", "Loop 复核 · 待确认清单", [
      "候选详情、知识卡选择、确认、保留、拒绝、撤回与重开均由同一个人工确认协议完成；这里不形成资产捷径。",
    ], [], triggerElement);
    const say = addMessageLine(body);
    const data = sources.reviews ? sources.reviews() : { items: [], error: "" };
    if (data.error) body.createEl("p", { cls: "life-cosmos-graph-error", text: `人工确认服务暂不可用：${data.error}` });
    if (!data.items.length && !data.error) body.createEl("p", { text: "当前没有认知候选。" });
    for (const item of data.items.slice(0, 8)) {
      const row = button(body, "", "life-cosmos-review-row", async () => {
        say(await runAction("review.open", item));
      });
      row.setAttribute("data-cosmos-action", "review.open");
      row.setAttribute("data-review-candidate", item.candidate_id || "");
      row.createEl("b", { text: item.title || "未命名候选" });
      row.createEl("small", { text: item.core_judgment || "查看候选判断与知识卡" });
    }
  };

  /* ---------- 显示与筛选 / 内容管理 / Agent 评估（持久状态走既有 handler） ---------- */
  const showSettings = (triggerElement, model) => {
    const { body } = show("系统管理", "主页 · 偏好与运行控制", [
      "筛选、密度与内容状态都是持久操作，由既有主页 handler 写入；此处不复制第二套状态。",
    ], [], triggerElement);
    const say = addMessageLine(body);

    /* 语义索引：抽屉各区的具名入口，与旧 DOM 顺序无关。 */
    const nav = body.createDiv({ cls: "life-cosmos-drawer-actions life-cosmos-drawer-nav" });
    for (const [actionId, label] of [["loop.openConsole", "Loop 控制台"], ["graph.openWorkflow", "Graph 工作流"]]) {
      const item = actionButton(nav, label, "", async () => say(await runAction(actionId)));
      item.setAttribute("data-cosmos-action", actionId);
    }
    for (const [label, openSection] of [
      ["Graph 状态详情", () => showGraph(triggerElement)],
      ["处理方向与接续", () => showIntents(triggerElement)],
      ["Graph Pilot 候选", () => showPilot(triggerElement)],
      ["Loop 人工确认", () => showReview(triggerElement)],
    ]) {
      button(nav, label, "", openSection);
    }

    const prefs = sources.preferences ? sources.preferences() : {};

    const filterBox = body.createDiv({ cls: "life-cosmos-settings-row" });
    filterBox.createEl("small", { text: "主页筛选（持久）" });
    const filterRow = filterBox.createDiv({ cls: "life-cosmos-drawer-actions" });
    for (const [id, label] of [["all", "全部"], ["unread", "未读"], ["watched", "关注"]]) {
      const item = actionButton(filterRow, label, prefs.filter === id ? "is-primary" : "", async () => {
        say(await runAction("view.setFilter", id));
      });
      item.setAttribute("data-cosmos-action", "view.setFilter");
      item.setAttribute("aria-pressed", String(prefs.filter === id));
    }

    const densityBox = body.createDiv({ cls: "life-cosmos-settings-row" });
    densityBox.createEl("small", { text: "显示密度（持久）" });
    const densityRow = densityBox.createDiv({ cls: "life-cosmos-drawer-actions" });
    for (const [id, label] of [["comfortable", "舒展"], ["compact", "紧凑"]]) {
      const item = actionButton(densityRow, label, prefs.density === id ? "is-primary" : "", async () => {
        say(await runAction("view.setDensity", id));
      });
      item.setAttribute("data-cosmos-action", "view.setDensity");
      item.setAttribute("aria-pressed", String(prefs.density === id));
    }

    const manageRow = body.createDiv({ cls: "life-cosmos-drawer-actions" });
    actionButton(manageRow, "管理隐藏内容", "", async () => {
      say(await runAction("home.manageHidden"));
    });
    actionButton(manageRow, "Agent 项目能力评估", "", async () => {
      say(await runAction("home.assess"));
    });

    if (model && model.decisionMatch) {
      body.createEl("p", { cls: "life-cosmos-graph-stale", text: `资产匹配：${model.decisionMatch}` });
    }

    const managed = [...(model?.daily || []), ...(model?.watched || [])].filter((item) => item.path).slice(0, 8);
    if (managed.length) {
      body.createEl("small", { text: "内容管理（已读/关注/隐藏均为持久操作）" });
      for (const item of managed) {
        const row = body.createDiv({ cls: "life-cosmos-item-row" });
        row.createEl("b", { text: item.title });
        const actions = row.createDiv({ cls: "life-cosmos-drawer-actions" });
        actionButton(actions, item.read ? "标为未读" : "标为已读", "", async () => {
          say(await runAction("item.toggleRead", item.path));
        });
        actionButton(actions, "关注/取消关注", "", async () => {
          say(await runAction("item.toggleWatch", item.path));
        });
        actionButton(actions, "今日隐藏", "", async () => {
          say(await runAction("item.hideToday", item.path));
        });
        actionButton(actions, "移出主页", "", async () => {
          say(await runAction("item.hide", item.path));
        });
      }
    }
  };

  /* ---------- 语义详情：按来源类型路由到具名动作，绝不复制旧按钮 ---------- */
  const showElement = async (element, triggerElement, fresh = false) => {
    if (!element) return;
    if (!fresh && (hasClass(element, "life-organized-card") || hasClass(element, "life-capture-card")) && typeof sources.refreshResearch === "function") {
      const loading = show(firstText(element, "strong", "碎片详情"), "正在读取当前研究状态", [], [], triggerElement);
      const selected = alignmentFor(safeSourceItems(sources.intents), explicitFragmentId(element));
      if (selected?.status === "passed") fragmentSelection = { fragment_id: selected.fragment_id };
      await sources.refreshResearch();
      if (!isOpen() || generation !== loading.generation) return;
      return showElement(element, triggerElement, true);
    }
    /* R1：授权详情——直接挂载四段摘要 + 一键按钮（通用 fallback 提取文本会丢按钮）。 */
    if (hasClass(element, "life-cosmos-auth")) {
      const source = firstText(element, ".life-cosmos-auth-source", "");
      const title = firstText(element, "h3", "授权详情");
      const { body } = show(`${source ? `${source} ` : ""}${title}`, "授权摘要 · 看清后一键批准", [], [], triggerElement);
      body.appendChild(element);
      return;
    }
    if (hasClass(element, "fragment-intent-card")) {
      showIntents(triggerElement);
      return;
    }
    if (hasClass(element, "life-organized-card") || hasClass(element, "life-capture-card")) {
      const rawCard = hasClass(element, "life-capture-card");
      const fragmentId = explicitFragmentId(element);
      const data = sources.intents ? sources.intents() : { items: [], error: "" };
      const item = fragmentId ? alignmentFor(data.items, fragmentId) : null;
      if (item) {
        showIntent(item, element, triggerElement, fresh);
        return;
      }
      if (fragmentId) {
        /* 无 alignment 时按自动承接视图如实呈现：服务故障优先；有记录按
           outcome；无记录 = 尚无接管证据，不承诺「后台每分钟自动承接」。 */
        const reportEntry = (Array.isArray(data.autoPropose) ? data.autoPropose : [])
          .find((entry) => entry && normalizeFragmentBasename(entry.fragment_id) === fragmentId);
        const view = autoProposeJourneyView(reportEntry || null, data.error || "");
        const title = firstText(element, "strong", "碎片详情");
        const lines = [view.nextAction];
        if (!reportEntry && !data.error) {
          lines.push("处理记录形成后会在此显示实际状态；只有需要你的决定时才会提供确认操作。");
        }
        const { body, foot } = show(title, `碎片 · ${view.statusLabel}`, lines, [], triggerElement);
        const refs = rawCard ? cardFragmentRefs(element) : null;
        const documents = rawCard
          ? [["打开原始记录", refs.rawRef], ["查看整理结果", refs.organizedRef]]
          : [["打开整理文档", linkPath(element)]];
        if (openNote) {
          const say = addMessageLine(body);
          for (const [label, path] of documents) if (path) actionButton(foot, label, "", () => openNoteGuarded(path, say));
          if (rawCard) for (const [label, id, path] of [
            ["探索来源", "fragment.explore", refs.rawRef],
            ["开始实验", "fragment.experiment.start", refs.organizedRef],
            ["记录实践反馈", "fragment.feedback.save", refs.organizedRef],
          ]) if (path) actionButton(foot, label, "", async () => say(await runAction(id, path))).setAttribute("data-cosmos-action", id);
        }
        return;
      }
    }
    if (hasClass(element, "life-loop-review-row") || element.getAttribute?.("data-review-candidate")) {
      const candidateId = element.getAttribute?.("data-review-candidate") || "";
      const data = sources.reviews ? sources.reviews() : { items: [] };
      const item = data.items.find((entry) => entry.candidate_id === candidateId);
      if (item) {
        runAction("review.open", item);
        return;
      }
      showReview(triggerElement);
      return;
    }
    if (hasClass(element, "life-capture-card")) {
      const refs = cardFragmentRefs(element);
      const title = firstText(element, "strong", "碎片详情");
      const { body, foot } = show(title, "碎片 · 显式路径与真实动作", [
        firstText(element, "p", "这里暂时没有更多说明。"),
      ], [], triggerElement);
      const say = addMessageLine(body);
      if (!refs.rawRef && !refs.organizedRef) {
        body.createEl("p", { text: "暂无可打开文档：这条记录没有显式路径。" });
      }
      if (refs.rawRef) {
        actionButton(foot, "打开原始记录", "is-primary", () => openNoteGuarded(refs.rawRef, say));
        actionButton(foot, "探索来源", "", async () => {
          say(await runAction("fragment.explore", refs.rawRef));
        }).setAttribute("data-cosmos-action", "fragment.explore");
      }
      if (refs.organizedRef) {
        actionButton(foot, "查看整理结果", "", () => openNoteGuarded(refs.organizedRef, say));
        actionButton(foot, "开始实验", "", async () => {
          say(await runAction("fragment.experiment.start", refs.organizedRef));
        }).setAttribute("data-cosmos-action", "fragment.experiment.start");
        actionButton(foot, "记录实践反馈", "", async () => {
          say(await runAction("fragment.feedback.save", refs.organizedRef));
        }).setAttribute("data-cosmos-action", "fragment.feedback.save");
      }
      return;
    }
    /* 资产与通用项：只认显式 data-path；无路径诚实说明，不猜路径。 */
    const path = linkPath(element);
    const title = firstText(element, "strong, h1, h2, h3, .life-note-link", "项目详情");
    /* F-D9：排除已知占位文案，不把 UI 提示当详情内容展示。 */
    const PLACEHOLDER_TEXTS = ["暂无可打开文档", "打开查看详情", "只读统计", "暂无更多说明"];
    const rows = uniqueTexts(element, ["p", "li", "small"], 10)
      .filter((value) => value !== title)
      .filter((value) => !PLACEHOLDER_TEXTS.some((ph) => value.includes(ph)));
    const { body, foot } = show(title, "主页原生详情", rows.length ? rows : ["这里暂时没有更多说明。"], [], triggerElement);
    if (path && openNote) {
      const say = addMessageLine(body);
      actionButton(foot, "打开真实文档", "is-primary", () => openNoteGuarded(path, say));
    } else {
      button(foot, "展开深度工作台", "", () => expandLegacy(element));
      if (!path) foot.createEl("small", { text: "暂无可打开文档：此项没有显式路径。" });
    }
  };

  const showThought = (item, category, triggerElement) => {
    /* R25 P1-9：直接打开也走 guarded capability；失败打开抽屉给出错误面。 */
    if (item?.path && openNote) {
      runAction("item.openNote", item.path).then((result) => {
        if (result && result.kind !== "success") {
          const failed = show(item.title || item.path, "思考主题 · 打开失败", [], [], triggerElement);
          addMessageLine(failed.body)(result);
        }
      });
      return;
    }
    const items = category?.items || [];
    let sayLine = null;
    const { body } = show(category?.name || item?.title || "思考主题", `${items.length} 条关联思考`,
      items.slice(0, 12).map((entry) => entry.title || entry.summary || entry.path || "未命名思考"),
      items.filter((entry) => entry.path).slice(0, 6).map((entry) => ({
        label: `打开：${entry.title || entry.path}`,
        primary: true,
        run: () => openNoteGuarded(entry.path, (result) => sayLine && sayLine(result)),
      })), triggerElement);
    sayLine = addMessageLine(body);
  };

  const showDay = (iso, entries, triggerElement) => {
    const names = { fragment: "碎片", document: "生成文档", asset: "知识资产" };
    if (!entries.length) {
      show(iso, "0 条当日活动", ["当天没有可展示的碎片或文档记录。"], [], triggerElement);
      return;
    }
    const counts = entries.reduce((all, entry) => {
      all[entry.kind] = (all[entry.kind] || 0) + 1;
      if (entry.pending) all.pending = (all.pending || 0) + 1;
      return all;
    }, {});
    const summary = [
      counts.fragment ? `碎片 ${counts.fragment}` : "",
      counts.document ? `文档 ${counts.document}` : "",
      counts.asset ? `资产 ${counts.asset}` : "",
      counts.pending ? `待推进 ${counts.pending}` : "",
    ].filter(Boolean).join(" · ");
    const { body } = show(iso, `${entries.length} 条当日活动`, [summary], [], triggerElement);
    const sayLine = addMessageLine(body);
    const list = body.createDiv({ cls: "life-cosmos-day-list" });
    entries.slice(0, 30).forEach((entry) => {
      const row = button(list, "", "life-cosmos-day-item", async () => {
        if (!entry.path) return;
        await openNoteGuarded(entry.path, sayLine); // R25 P1-9：成功才关抽屉
      });
      row.createEl("b", { text: entry.title });
      row.createEl("small", { text: `${entry.time} · ${names[entry.kind] || "记录"}${entry.pending ? " · 待推进" : ""}` });
      if (!entry.path) row.setAttribute("disabled", "disabled");
    });
  };

  const restoreKnowledge = () => {
    const saved = viewState?.knowledge;
    if (saved) return showKnowledge(saved, null, saved);
  };
  const restoreFragment = () => {
    const saved = viewState?.fragment;
    if (!saved || viewState?.knowledge) return;
    const source = queryAll(legacy, ".life-capture-card, .life-organized-card")
      .find(element => explicitFragmentId(element) === saved.fragment_id);
    if (source) return showElement(source, null);
    const item = alignmentFor(safeSourceItems(sources.intents), saved.fragment_id);
    if (item) return showIntent(item, null, null);
    show("研究详情", "当前记录暂不可用", ["暂时无法重新取得这条碎片的研究记录，请刷新后重试。"]);
  };
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    rememberKnowledgePosition();
    const saved = opened && knowledgeSelection ? { ...knowledgeSelection, ...knowledgeReadingPosition } : null;
    const savedFragment = opened && fragmentSelection ? { ...fragmentSelection } : null;
    close();
    drawer.removeEventListener("scroll", rememberKnowledgePosition, true);
    drawer.removeEventListener("focusin", rememberKnowledgePosition);
    shell.removeEventListener("cancel", onCancel);
    if (viewState) { viewState.knowledge = saved; viewState.fragment = savedFragment; }
  };
  return { close, dispose, isOpen, restoreKnowledge, restoreFragment, projectionChanged, showElement, showThought, showDay, showGraph, showIntents, showPilot, showReview, showSettings, showKnowledge, showIntent };
}

/* ---------- 总览提示条：逾期事项 + 盲区提醒（只读投影 + 诚实动作） ---------- */
function renderAttentionStrip(overview, cosmos, model, runAction, expandLegacy, say) {
  if (!model.overdue.length && !model.blindspots.length) return;
  const strip = overview.createDiv({ cls: "life-cosmos-attention rise d3" });
  if (model.overdue.length) {
    const box = strip.createDiv({ cls: "life-cosmos-att-box" });
    box.createEl("small", { text: `逾期事项 ${model.overdue.length} · 只读提示` });
    for (const item of model.overdue.slice(0, 3)) {
      const row = box.createDiv({ cls: "life-cosmos-att-row" });
      row.createSpan({ text: item.title });
      if (item.path) {
        /* P1-3/F-K13：openNote 结果接入 say——失效路径的错误可见。 */
        const open = button(row, "打开来源", "", async () => {
          const result = await runAction("item.openNote", item.path);
          if (say && result.kind !== "success") say(result);
        });
        open.setAttribute("data-cosmos-action", "item.openNote");
      } else {
        row.createEl("small", { text: "暂无可打开文档" });
      }
    }
  }
  if (model.blindspots.length) {
    const box = strip.createDiv({ cls: "life-cosmos-att-box" });
    box.createEl("small", { text: `盲区提醒 ${model.blindspots.length}` });
    for (const item of model.blindspots) {
      const row = box.createDiv({ cls: "life-cosmos-att-row" });
      /* K6/F-K6：纯标题行不带可点样式，与动作按钮视觉分离。 */
      const titleSpan = row.createSpan({ text: item.title, cls: "life-cosmos-att-title" });
      titleSpan.setAttribute("data-noninteractive", "1");
      if (item.filter) {
        /* F-D1（方案 A，T1 实测后确定）：Cosmos 自身消费 filter 做会话内
           过滤，同时持久写仍走既有 setViewPreference 单一权威；反馈文案
           与实际过滤结果一致——0 张受影响时不声称已过滤（P1-1）。 */
        const act = button(row, item.actionLabel || "只看", "", async () => {
          const result = await runAction("view.setFilter", item.filter);
          const affected = applyCosmosFilter(cosmos, item.filter);
          if (say) {
            const filterName = item.filter === "unread" ? "未读" : item.filter === "watched" ? "关注" : "全部";
            say({
              kind: result.kind === "success" ? "success" : result.kind,
              message: result.message || (result.kind === "success"
                ? (affected > 0
                  ? `已切换筛选：${filterName}（持久保存；本页 ${affected} 条信号已同步过滤）`
                  : `已切换筛选：${filterName}（持久保存；本页暂无可过滤的信号条目）`)
                : "筛选未能更新"),
            });
          }
        });
        act.setAttribute("data-cosmos-action", "view.setFilter");
      } else if (item.scrollTarget) {
        button(row, item.actionLabel || "查看", "", () => expandLegacy(item.element));
      }
    }
  }
}

/* F-D1 方案 A 的会话内过滤（P1-1 修订）：目标对准真实渲染元素
   .life-cb-signal-row（研究信号行，daily/watched/signals 的唯一渲染面），
   read/watched 语义由行属性 data-read/data-watched 承载。返回受影响卡数。 */
function applyCosmosFilter(scope, filter) {
  const cards = queryAll(scope, ".life-cb-signal-row");
  let affected = 0;
  for (const card of cards) {
    const isRead = card.getAttribute("data-read") === "1";
    const isWatched = card.getAttribute("data-watched") === "1";
    let hide = false;
    if (filter === "unread") hide = isRead;
    else if (filter === "watched") hide = !isWatched;
    if (hide) { card.setAttribute("hidden", "hidden"); affected += 1; }
    else card.removeAttribute("hidden");
  }
  return affected;
}

/* ---------- 服务状态指示灯（P2a）：从独立摘要条降级为 capsule 内小灯。
   F-D3 的 refresher 机制原样保留——update/updateLoop 闭包注册进 graphRefreshers，
   指示灯随刷新动态更新；常态静默，仅异常/待办时点亮并可点开抽屉详情。 ---------- */
function renderServiceDots(capsule, drawer, sources, graphRefreshers = []) {
  const wrap = capsule.createDiv({ cls: "life-cosmos-svc-dots" });
  const mkDot = (name, title, open) => {
    const dot = button(wrap, name === "graph" ? "Graph 运行状态" : "Loop 待确认", `life-cosmos-svc-dot is-${name}`, (trigger) => open(trigger));
    dot.setAttribute("data-cosmos-action", name === "graph" ? "drawer.graph" : "drawer.review");
    dot.setAttribute("aria-label", title);
    dot.setAttribute("title", title);
    return dot;
  };
  const graphDot = mkDot("graph", "Graph 服务状态", (trigger) => drawer.showGraph(trigger));
  const loopDot = mkDot("loop", "Loop 待确认", (trigger) => drawer.showReview(trigger));
  /* P2-2 返修：基础 label 与状态后缀分离，每次刷新整体重写而非追加 */
  const STATE_LABEL = { ok: "正常", attention: "需要关注", error: "异常" };
  const setDot = (dot, baseLabel, kind) => {
    dot.setAttribute("data-state", kind);
    dot.setAttribute("aria-label", `${baseLabel}（${STATE_LABEL[kind]}）`);
    dot.setAttribute("title", `${baseLabel}（${STATE_LABEL[kind]}）`);
  };
  const update = () => {
    const graphSource = typeof sources?.graph === "function" ? sources.graph() : { state: null, lastGood: null };
    const ready = graphSource.state && graphSource.state.kind === "ready" ? graphSource.state : null;
    const error = graphSource.state && graphSource.state.kind === "error" ? graphSource.state : null;
    const usable = ready || (graphSource.lastGood && graphSource.lastGood.kind === "ready" ? graphSource.lastGood : null);
    if (error && !usable) setDot(graphDot, "Graph 服务状态", "error");
    else if (usable) {
      const summary = computeGraphSummary(usable.runs);
      /* 等待人工/异常 = 需要用户关注（attention），与 Loop 待确认灯同语义 */
      const needAttention = summary.abnormal > 0 || summary.waiting > 0;
      setDot(graphDot, "Graph 服务状态", error ? "attention" : needAttention ? "attention" : "ok");
      graphDot.setAttribute("data-detail", error ? "服务暂不可用，显示最近成功读取" : `${summary.headline}${summary.latest ? ` · Checkpoint #${summary.latest.sequence}` : ""}`);
    } else setDot(graphDot, "Graph 服务状态", "error");
  };
  /* F-D3：LOOP 灯与 Graph 灯同构——闭包重读 sources，注册进 refreshers。 */
  const updateLoop = () => {
    const reviews = typeof sources?.reviews === "function" ? sources.reviews() : { items: [], error: "" };
    const pending = reviews.items.filter((item) => item.content_status === "pending_confirmation").length;
    const loopDetail = reviews.error ? "Loop 复核服务不可用"
      : pending ? `${pending} 条候选等待你确认` : "当前没有待确认候选";
    setDot(loopDot, "Loop 待确认", reviews.error ? "error" : pending ? "attention" : "ok");
    loopDot.setAttribute("data-detail", loopDetail);
  };
  update();
  updateLoop();
  graphRefreshers.push(update);
  graphRefreshers.push(updateLoop);
}

/* fix7：当前激活板记入模块级会话状态——Dataview 频繁 force-refresh 重建模板
   导致重挂载时板块被重置回总览；恢复上次激活板，会话级（reload 回总览是预期）。 */
let lastActiveBoard = "overview";

function renderCosmos(doc, home, mount, legacy, model, options = {}) {
  const cosmos = doc.createElement("section");
  cosmos.className = COSMOS_CLASS;
  cosmos.setAttribute("data-life-cosmos-version", "14");
  /* fix7：恢复上次激活板（会话级） */
  cosmos.setAttribute("data-board", lastActiveBoard);
  const hasLayout = typeof cosmos.getBoundingClientRect === "function";
  const timers = [];
  const registerTimer = (id, kind) => { timers.push({ id, kind }); return id; };

  const viewState = options.viewState || { knowledgeDisclosures: new Map(), knowledge: null };
  // Daily work uses a static reading surface; no decorative animation loop.
  const drawer = createCosmosDrawer(doc, cosmos, {
    openNote: options.openNote,
    capabilities: options.capabilities || {},
    sources: options.sources || {},
    legacy,
    viewState,
  });
  /* 面板级动作入口：与抽屉共用同一组具名 adapter，缺失能力诚实报错；
     同步/异步异常一律归一为 error，绝不泄漏未处理 rejection。 */
  const runAction = async (actionId, ...args) => {
    const adapter = (options.capabilities || {})[actionId];
    if (typeof adapter !== "function") return { kind: "error", message: "该能力在当前环境不可用" };
    try {
      const outcome = await adapter(...args);
      if (outcome && typeof outcome === "object" && typeof outcome.kind === "string") return outcome;
      return { kind: "success", message: "" };
    } catch (error) {
      return { kind: "error", message: error && error.message ? error.message : "操作失败，请稍后重试。" };
    }
  };
  /* Graph 主板就地刷新器：手动刷新后各面板重读 sources 并重绘自身。 */
  const graphRefreshers = [];
  const researchRefreshers = [];
  const knowledgeRefreshers = [];
  let disposeAtlas = () => {};
  disposeAtlas.setActive = () => {};

  /* 顶栏：品牌脉冲 + 板块导航 + 时钟 */
  const top = cosmos.createEl("header", { cls: "life-cosmos-topbar rise d1" });
  const brand = top.createDiv({ cls: "life-cosmos-brand" });
  brand.createSpan({ cls: "life-cosmos-brand-pulse" });
  brand.createEl("strong", { text: "MY LIFE" });

  const openLegacy = () => {
    legacy.addClass("is-open");
    cosmos.addClass("is-legacy-open");
    disposeAtlas.setActive?.(false);
    toggle.setText("收起旧版工作台");
  };
  const reveal = (element, trigger) => {
    if (!element) return;
    drawer.showElement(element, trigger);
  };

  /* v13：导航 = 真板块切换（不再是展开旧版的快捷方式） */
  const NAV_BOARDS = [
    ["overview", "工作台"], ["fragments", "研究记录"], ["assets", "知识库"],
  ];
  const nav = top.createEl("nav", { cls: "life-cosmos-nav" });
  const navButtons = [];
  let setResearchFilter = () => {};
  const switchBoard = (key, filter) => {
    drawer.close();
    disposeAtlas.setActive?.(key === "overview" && !hasClass(cosmos, "is-legacy-open"));
    cosmos.setAttribute("data-board", key);
    lastActiveBoard = key; /* fix7：会话级记住激活板 */
    if (key === "fragments" && filter) setResearchFilter(filter);
    for (const section of cosmos.querySelectorAll(".life-cb-board")) {
      const on = section.getAttribute("data-board-id") === key;
      section.toggleClass("is-on", on);
      section.setAttribute("aria-hidden", String(!on));
    }
    NAV_BOARDS.forEach(([boardKey], index) => {
      const on = boardKey === key;
      navButtons[index]?.toggleClass("is-active", on);
      navButtons[index]?.setAttribute("aria-pressed", String(on));
    });
  };
  NAV_BOARDS.forEach(([key, label], index) => {
    const item = button(nav, label, index === 0 ? "is-active" : "", () => switchBoard(key));
    item.setAttribute("aria-pressed", String(index === 0));
    navButtons.push(item);
  });
  const clock = top.createDiv({ cls: "life-cosmos-clock" });
  const clockTime = clock.createEl("strong", { text: "" });
  const clockDate = clock.createSpan({ text: "今日" });
  const tick = () => {
    if (cosmos.isConnected === false) return;
    const now = new Date();
    clockTime.setText(`${now.getMonth() + 1}月${now.getDate()}日`);
    clockDate.setText(["周日", "周一", "周二", "周三", "周四", "周五", "周六"][now.getDay()]);
    options.onProjectionTick?.();
  };
  tick();
  if (hasLayout && typeof window !== "undefined") {
    registerTimer(window.setInterval(tick, 1000), "interval");
  }

  /* 任务舱：六个高频入口，不依赖展开深度工作台 */
  const capsule = renderCapsule(cosmos, model, drawer, runAction, graphRefreshers);
  const say = capsule.say;
  /* P2a：服务状态降级为 capsule 内指示灯（F-D3 refresher 保留） */


  const boards = cosmos.createDiv({ cls: "life-cosmos-boards" });

  /* 板块：总览（v11 星空全量保留） */
  const overview = boards.createDiv({ cls: "life-cb-board is-on" });
  overview.setAttribute("data-board-id", "overview");

  const hero = overview.createDiv({ cls: "life-workbench-head" });
  hero.createEl("h1", { text: "工作台" });
  hero.createEl("p", { text: "记下链接与问题，查看研究结论，积累可复用的知识。" });
  const workbench = overview.createDiv({ cls: "life-workbench" });
  const refreshWorkbench = () => renderWorkbench(workbench, model, options.sources || {}, drawer, reveal, runAction, say, switchBoard);
  refreshWorkbench();
  graphRefreshers.push(refreshWorkbench);
  researchRefreshers.push(refreshWorkbench);
  knowledgeRefreshers.push(refreshWorkbench);

  const grid = overview.createDiv({ cls: "life-workbench-approvals" });
  overview.insertBefore(grid, workbench);
  const decisions = grid.createEl("section", { cls: "life-cosmos-panel life-cosmos-decisions rise d4" });
  /* R1 授权队列：client 状态派生卡并入卡堆（来源标签即语义说明），
     与既有 DOM 投影决定卡共存；N = 卡数（含授权卡）。
     R2：refreshItems 重派生（刷新恢复有效 / 漂移消除）；onAuthChanged 统一
     移卡与整体刷新；刷新按钮走 graph.refresh 后重派生。 */
  let resolveAuthItem = null;
  const buildItems = () => [
    ...buildAuthQueueItems(doc, options.sources || {}, runAction, say, (item) => {
      if (resolveAuthItem) resolveAuthItem(item);
    }, () => {
      // 刷新按钮：重取数据后整体重派生（过期卡恢复有效 → 按钮重新可用）。
      runAction("graph.refresh").then((result) => {
        say(result);
        if (resolveAuthItem) resolveAuthItem(null, { refresh: true });
      });
    }, null),
    ...model.decisions,
  ];
  renderDeck(decisions, buildItems(), reveal, {
    onCountChanged: (count, incomplete) => {
      if (!count && !incomplete) grid.setAttribute("hidden", "hidden");
      else grid.removeAttribute("hidden");
    },
    refreshItems: buildItems,
    onAuthChanged: (fn) => { resolveAuthItem = fn; },
    // V2-C5：队列取数时间戳（陈旧可见）。
    syncAt: () => (options.sources && typeof options.sources.authQueueSyncAt === "function" ? options.sources.authQueueSyncAt() : null),
    // R25 P1-5：任一队列来源 error/partial → 标题数量降级为下界（≥N）。
    queueIncomplete: () => ["graphQueue", "loopReview", "loopControl"].some((key) => {
      const src = options.sources || {};
      const view = typeof src[key] === "function" ? src[key]() : null;
      const state = view && view.state;
      return Boolean(state && (state.kind === "error" || state.partial));
    }),
  });
  graphRefreshers.push(() => resolveAuthItem?.(null, { refresh: true }));
  // Low-frequency tools stay available without competing with the daily path.
  const tools = cosmos.createEl("details", { cls: "life-cosmos-tools" });
  tools.createEl("summary", { text: "更多工具与系统管理" });
  const toolLinks = tools.createDiv({ cls: "life-cosmos-tool-links" });
  button(toolLinks, "运行监控与信息源", "", () => switchBoard("research"));
  button(toolLinks, "日记与专注工具", "", () => switchBoard("focus"));
  button(toolLinks, "系统管理", "", (trigger) => drawer.showSettings(trigger, model)).setAttribute("data-cosmos-action", "drawer.settings");
  renderServiceDots(toolLinks, drawer, options.sources || {}, graphRefreshers);
  const toggle = button(toolLinks, "打开旧版工作台", "", () => {
    if (hasClass(legacy, "is-open")) {
      legacy.removeClass("is-open");
      cosmos.removeClass("is-legacy-open");
      toggle.setText("打开旧版工作台");
    } else openLegacy();
  });
  tools.createEl("p", { text: "旧版看板保留原始记录和低频操作；研究进度与知识结果以上方三个入口为准。" });

  /* v13 四板块：数据全部来自 buildCosmosModel 的真实投影 */
  const researchBoard = boards.createDiv({ cls: "life-cb-board" });
  researchBoard.setAttribute("data-board-id", "research");
  button(researchBoard, "← 返回工作台", "life-cosmos-back", () => switchBoard("overview"));
  renderAttentionStrip(researchBoard, cosmos, model, runAction, (element) => {
    openLegacy();
    element?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, say);
  renderArrayBoard(researchBoard, model, reveal, drawer, runAction, options.sources || {}, graphRefreshers, say);
  const fragmentsBoard = boards.createDiv({ cls: "life-cb-board" });
  fragmentsBoard.setAttribute("data-board-id", "fragments");
  viewState.research ||= { filter: "all", query: "" };
  setResearchFilter = renderRefineryBoard(fragmentsBoard, model, reveal, runAction, say, options.sources || {}, drawer.showIntent, graphRefreshers, researchRefreshers, viewState.research);
  const focusBoard = boards.createDiv({ cls: "life-cb-board" });
  focusBoard.setAttribute("data-board-id", "focus");
  button(focusBoard, "← 返回工作台", "life-cosmos-back", () => switchBoard("overview"));
  button(focusBoard, "打开今日笔记", "", async () => say(await runAction("home.openToday"))).setAttribute("data-cosmos-action", "home.openToday");
  renderTodayTodos(focusBoard, model, reveal, hasLayout, registerTimer, runAction, say);
  const focusPanel = focusBoard.createEl("section", { cls: "life-cosmos-panel life-cosmos-focus" });
  renderFocus(focusPanel, model, reveal, hasLayout, registerTimer, runAction);
  renderOrbitBoard(focusBoard, model, reveal, hasLayout, registerTimer, options.activity || new Map(), drawer.showDay, runAction, say);
  const assetsBoard = boards.createDiv({ cls: "life-cb-board" });
  assetsBoard.setAttribute("data-board-id", "assets");
  const captureKnowledgeDisclosures = renderCatalogueBoard(assetsBoard, model, reveal, runAction, drawer, options.sources || {}, graphRefreshers, knowledgeRefreshers, viewState.knowledgeDisclosures);

  /* fix7：初始化时恢复上次激活板——data-board 属性 + is-on class + 导航态同步
     （必须在全部 board section 创建之后调用，否则 is-on 同步不到任何板块） */
  if (lastActiveBoard !== "overview") switchBoard(lastActiveBoard);

  cosmos.__lifeCosmosRefreshProjections = (changed) => {
    const positions = [];
    for (let node = cosmos; node; node = node.parentElement || node.parentNode || node.parent) {
      positions.push([node, node.scrollTop, node.scrollLeft]);
    }
    if (changed.research) researchRefreshers.forEach(refresh => refresh());
    if (changed.knowledge) knowledgeRefreshers.forEach(refresh => refresh());
    drawer.projectionChanged();
    for (const [node, top, left] of positions) {
      if (typeof top === "number") node.scrollTop = top;
      if (typeof left === "number") node.scrollLeft = left;
    }
  };
  const disposeGlow = () => {};
  let cosmosDisposed = false;
  cosmos.__lifeCosmosDispose = () => {
    if (cosmosDisposed) return;
    cosmosDisposed = true;
    captureKnowledgeDisclosures();
    disposeAtlas();
    disposeGlow();
    drawer.dispose();
    if (typeof window !== "undefined") {
      for (const timer of timers) {
        if (timer.kind === "interval") window.clearInterval?.(timer.id);
        else window.clearTimeout?.(timer.id);
      }
    }
  };
  mount.__lifeCosmosDispose = cosmos.__lifeCosmosDispose;
  mount.appendChild(cosmos);
  void drawer.restoreKnowledge();
  void drawer.restoreFragment();
  return cosmos;
}

/* ================================================================
   显式挂载协议（Dataview ↔ 插件 单一所有权）
   Dataview 模板拥有 .life-home 结构（mount + legacy + dashboard）；
   插件只在 mount 内写入。旧的事后 sweep/recovery 注入路径已删除，
   MutationObserver 不再承担 Cosmos 正确性责任。
   ================================================================ */

function parentOf(el) {
  return (el && (el.parent || el.parentNode)) || null;
}

/* matches 在真实 DOM 与 FakeEl 上都存在；向上走父链模拟 closest。 */
function closestOf(el, selector) {
  let node = el;
  while (node) {
    if (typeof node.matches === "function" && node.matches(selector)) return node;
    node = parentOf(node);
  }
  return null;
}

function clearElement(el) {
  if (typeof el.empty === "function") {
    el.empty();
    return;
  }
  for (const child of [...(el.children || [])]) child.remove?.();
}

/* 校验握手结构：root 必须属于某个 .my-life-homepage-view 主页，且直接拥有
   一个 mount 与一个 legacy；legacy 内必须保留 .life-dashboard-content。 */
function resolveCosmosStructure(root) {
  if (!root || typeof root.querySelector !== "function") {
    throw new Error("主页根节点不可用");
  }
  const home = closestOf(root, ".my-life-homepage-view");
  if (!home) throw new Error("主页根节点不属于 My Life 主页视图");
  const mount = root.querySelector(MOUNT_SELECTOR);
  if (!mount || parentOf(mount) !== root) {
    throw new Error("主页缺少独立的 Cosmos 挂载点");
  }
  const legacy = root.querySelector(`.${LEGACY_CLASS}`);
  if (!legacy || parentOf(legacy) !== root) {
    throw new Error("主页缺少折叠的旧版操作面容器");
  }
  const dashboard = legacy.querySelector(".life-dashboard-content");
  if (!dashboard) throw new Error("旧版操作面缺少 dashboard 内容区");
  return { home, root, mount, legacy, dashboard };
}

/* 挂载开始：校验结构、销毁上一轮 Cosmos（timer/rAF/listener）、清空 mount，
   放入诚实的加载占位——数据读取期间页面不是空白，也不是旧主页闪切。 */
function prepareHomepageCosmos(doc, root) {
  const session = resolveCosmosStructure(root);
  session.mount.__lifeCosmosDispose?.();
  delete session.mount.__lifeCosmosDispose;
  clearElement(session.mount);
  const loading = doc.createElement("section");
  loading.className = LOADING_CLASS;
  loading.setAttribute("role", "status");
  /* P3 骨架屏：hero/grid 同形灰色占位，消除高频重挂载的布局抖动 */
  const skeleton = doc.createElement("div");
  skeleton.className = "life-cosmos-skeleton";
  for (const block of ["sk-hero", "sk-band", "sk-grid"]) {
    const region = doc.createElement("div");
    region.className = `sk ${block}`;
    if (block === "sk-grid") {
      for (let i = 0; i < 4; i += 1) region.appendChild(doc.createElement("i"));
    }
    skeleton.appendChild(region);
  }
  loading.appendChild(skeleton);
  const strong = doc.createElement("strong");
  strong.textContent = "正在加载工作台";
  const hint = doc.createElement("small");
  hint.textContent = "读取研究记录与知识目录…";
  loading.appendChild(strong);
  loading.appendChild(hint);
  session.mount.appendChild(loading);
  return session;
}

/* 挂载失败：诚实显示错误并展开旧版操作面；绝不空白页。 */
function failHomepageCosmos(doc, session, error) {
  const message = error && error.message ? error.message : String(error || "未知错误");
  const mount = session && session.mount;
  const legacy = session && session.legacy;
  if (legacy) legacy.addClass("is-open");
  if (!mount) return;
  mount.__lifeCosmosDispose?.();
  delete mount.__lifeCosmosDispose;
  clearElement(mount);
  const box = doc.createElement("section");
  box.className = ERROR_CLASS;
  box.setAttribute("role", "alert");
  const strong = doc.createElement("strong");
  strong.textContent = "工作台暂时不可用";
  const detail = doc.createElement("p");
  detail.textContent = message;
  const hint = doc.createElement("small");
  hint.textContent = "已展开完整操作面，所有既有功能不受影响。";
  box.appendChild(strong);
  box.appendChild(detail);
  box.appendChild(hint);
  mount.appendChild(box);
}

/* 落地：从 legacy 内真实投影构建 model，只把 Cosmos 渲染进当前 mount。
   重复落地前先销毁上一轮 Canvas/rAF/interval/timeout/pointer listener，
   同一 mount 任何时刻恰好一个 Cosmos。 */
function finishHomepageCosmos(doc, session, map, options = {}) {
  session.mount.__lifeCosmosDispose?.();
  delete session.mount.__lifeCosmosDispose;
  clearElement(session.mount);
  const model = buildCosmosModel(session.dashboard, map);
  return renderCosmos(doc, session.home, session.mount, session.legacy, model, options);
}

/* 结构校验失败的诚实降级：尽最大可能从 root 内定位 legacy 并展开；
   mount 存在但其他结构损坏时错误面写入既有 mount；mount 缺失时在 root
   内、legacy 之前创建一个最小错误面。全程 DOM API + 文本节点（textContent
   赋值），不使用不可信 innerHTML；错误文本只有固定文案与异常摘要。
   任何情况下不得留下空白页，也不得向调用方抛出异常。 */
function degradeHomepageCosmos(doc, root, error) {
  try {
    if (!root || typeof root.querySelector !== "function") return false;
    const legacy = root.querySelector(`.${LEGACY_CLASS}`);
    if (legacy) legacy.addClass("is-open");
    let mount = root.querySelector(MOUNT_SELECTOR);
    if (!mount) {
      mount = doc.createElement("div");
      mount.className = "life-cosmos-mount";
      mount.setAttribute(MOUNT_ATTR, "1");
      if (legacy && typeof root.insertBefore === "function") {
        root.insertBefore(mount, legacy);
      } else {
        root.appendChild(mount);
      }
    }
    failHomepageCosmos(doc, { mount, legacy }, error);
    return true;
  } catch (nested) {
    console.error("[My Life] Cosmos 降级错误面渲染失败", nested);
    return false;
  }
}

function disposeHomepageCosmos(doc) {
  for (const mount of doc.querySelectorAll(MOUNT_SELECTOR)) {
    mount.__lifeCosmosDispose?.();
    delete mount.__lifeCosmosDispose;
  }
  for (const cosmos of doc.querySelectorAll(`.${COSMOS_CLASS}`)) {
    cosmos.__lifeCosmosDispose?.();
    cosmos.remove?.();
  }
}

module.exports = {
  COSMOS_CLASS,
  LEGACY_CLASS,
  MOUNT_ATTR,
  MOUNT_SELECTOR,
  LOADING_CLASS,
  ERROR_CLASS,
  buildCosmosModel,
  buildFragmentJourneys,
  FRAGMENT_JOURNEY_STAGES,
  collectHomepageActivity,
  prepareHomepageCosmos,
  failHomepageCosmos,
  finishHomepageCosmos,
  degradeHomepageCosmos,
  disposeHomepageCosmos,
  closestOf,
  hasClass,
};
