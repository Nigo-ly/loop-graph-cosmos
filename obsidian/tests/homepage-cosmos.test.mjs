import { describe, it } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { FakeEl } from "./helpers/fake-dom.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const {
  buildCosmosModel,
  buildFragmentJourneys,
  collectHomepageActivity,
  FRAGMENT_JOURNEY_STAGES,
} = require(path.join(ROOT, "src", "console", "homepage-cosmos.js"));

function home() {
  const root = new FakeEl("div");
  const stats = root.createDiv({ cls: "life-stat-strip" });
  for (const [label, value] of [["待处理", "3"], ["今日产出", "7"], ["知识规模", "128"]]) {
    const stat = stats.createDiv({ cls: "life-stat" });
    stat.createSpan({ text: label });
    stat.createEl("strong", { text: value });
  }
  const focus = root.createDiv({ cls: "life-focus-bar" });
  focus.createEl("strong", { text: "整理受治理研究结论" });
  focus.createDiv({ cls: "life-focus-time", text: "24:51" });
  const todos = root.createDiv({ cls: "life-todos" });
  const list = todos.createEl("ul");
  for (const title of ["确认研究方向", "整理研究结论", "归档三条碎片"]) {
    const item = list.createEl("li");
    item.createEl("strong", { text: title });
  }
  const fragments = root.createDiv({ cls: "life-fragments" });
  for (const title of ["碎片甲", "碎片乙"]) {
    const card = fragments.createEl("article", { cls: "life-capture-card" });
    card.createEl("strong", { text: title });
    card.createEl("p", { text: "真实碎片投影" });
  }
  const intent = fragments.createDiv({ cls: "fragment-intent-card" });
  intent.createEl("strong", { text: "MiniMax H3 是否继续核验" });
  intent.createEl("p", { cls: "fragment-intent-reason", text: "证据仍不足" });
  intent.createEl("button", { cls: "fragment-intent-confirm", text: "确认并继续" });
  return root;
}

describe("Cosmos homepage real-data adapter", () => {
  it("maps the existing homepage and thought projection without demo constants", () => {
    const map = {
      total: 35,
      categories: [
        { name: "研究前线", items: [{ status: "provisional" }] },
        { name: "个人信息系统", items: [{ status: "concluded" }] },
      ],
    };
    const model = buildCosmosModel(home(), map);

    assert.equal(model.thoughts, 35);
    assert.equal(model.systems, 2);
    assert.equal(model.todayProgress, 7);
    assert.equal(model.focusTitle, "整理受治理研究结论");
    assert.equal(model.focusTime, "24:51");
    assert.deepEqual(model.tasks.map((item) => item.title), ["确认研究方向", "整理研究结论", "归档三条碎片"]);
    assert.deepEqual(model.fragments.map((item) => item.title), ["碎片甲", "碎片乙"]);
    assert.equal(model.decisions.length, 1);
    assert.equal(model.decisions[0].title, "MiniMax H3 是否继续核验");
    assert.equal(model.decisions[0].meta, "证据仍不足");
  });

  it("does not mistake completed intent cards for pending decisions", () => {
    const root = home();
    const completed = root.createDiv({ cls: "fragment-intent-card" });
    completed.createEl("strong", { text: "已经确认的方向" });
    completed.createEl("p", { text: "处理方向已确认" });
    completed.createEl("button", { cls: "fragment-intent-continue", text: "继续处理这个主题" });

    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.equal(model.decisions.length, 1);
    assert.ok(model.decisions.every((item) => item.title !== "已经确认的方向"));
  });

  it("keeps empty states honest instead of inventing sample tasks or fragments", () => {
    const root = new FakeEl("div");
    root.createDiv({ cls: "life-todos" }).createEl("li", { text: "今天没有未完成任务" });
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.equal(model.thoughts, 0);
    assert.equal(model.systems, 0);
    assert.equal(model.todayProgress, 0);
    assert.deepEqual(model.tasks, []);
    assert.deepEqual(model.fragments, []);
    assert.deepEqual(model.decisions, []);
    assert.equal(model.focusTitle, "尚未选择当前专注");
  });

  it("prefers the research proposal note over a terminal flow state as decision meta", () => {
    const root = new FakeEl("div");
    const confirmed = root.createDiv({ cls: "fragment-intent-card" });
    confirmed.createEl("strong", { text: "补充核验" });
    confirmed.createEl("p", { cls: "fragment-intent-reason", text: "处理已结束" });
    confirmed.createEl("small", {
      cls: "fragment-research-proposal-note",
      text: "已有升级提案（尚未执行）；确认后才会创建研究 Run。",
    });
    confirmed.createEl("button", { cls: "fragment-intent-research-create", text: "创建 Graph 研究 Run" });

    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.equal(model.decisions.length, 1);
    assert.equal(model.decisions[0].meta, "已有升级提案（尚未执行）；确认后才会创建研究 Run。");
  });

  it("keeps the real current focus honest, including the homepage itself", () => {
    const root = home();
    root.querySelector(".life-focus-bar strong").setText("My Life");
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    // 生产主页的当前焦点就是 My Life 本身：原样保留，不伪装成首个任务。
    assert.equal(model.focusTitle, "My Life");
  });

  it("builds a read-only monthly activity ledger from metadata without reading note bodies", () => {
    let bodyReads = 0;
    const files = [
      { path: "散记/碎片想法/a.md", basename: "碎片 A", stat: { ctime: new Date(2026, 7, 11, 9, 3).getTime() } },
      { path: "研究/报告.md", basename: "研究报告", stat: { ctime: new Date(2026, 7, 11, 10, 5).getTime() } },
      { path: "旧文档.md", basename: "旧文档", stat: { ctime: new Date(2026, 6, 1).getTime() } },
    ];
    const app = {
      vault: { getMarkdownFiles: () => files, read: () => { bodyReads += 1; } },
      metadataCache: { getFileCache: (file) => file.path.includes("报告") ? { frontmatter: { status: "待验证" } } : {} },
    };
    const activity = collectHomepageActivity(app, new Date(2026, 7, 11, 12));
    assert.equal(bodyReads, 0);
    assert.equal(activity.get("2026-08-11").length, 2);
    assert.deepEqual(activity.get("2026-08-11").map((item) => item.kind), ["fragment", "document"]);
    assert.equal(activity.get("2026-08-11")[1].pending, true);
    assert.equal(activity.has("2026-07-01"), false);
  });

  it("星历排除运维/归档目录（_vault-ops/ops/.trash）——批量导入副本不计入活动", () => {
    const files = [
      { path: "散记/碎片想法/a.md", basename: "碎片 A", stat: { ctime: new Date(2026, 7, 11, 9, 3).getTime() } },
      { path: "_vault-ops/候选归档区/副本1.md", basename: "副本1", stat: { ctime: new Date(2026, 7, 11, 8, 0).getTime() } },
      { path: "ops/看门狗/故障/2026-08-11_1.md", basename: "故障单", stat: { ctime: new Date(2026, 7, 11, 8, 0).getTime() } },
      { path: ".trash/旧碎片.md", basename: "旧碎片", stat: { ctime: new Date(2026, 7, 11, 8, 0).getTime() } },
    ];
    const app = {
      vault: { getMarkdownFiles: () => files, read: () => 0 },
      metadataCache: { getFileCache: () => ({}) },
    };
    const activity = collectHomepageActivity(app, new Date(2026, 7, 11, 12));
    const day = activity.get("2026-08-11");
    assert.equal(day.length, 1, "排除目录不计入：只留正常路径 1 条");
    assert.equal(day[0].kind, "fragment");
  });
});

describe("Cosmos V1 capability projection", () => {
  it("extracts task completion bindings only from explicit data-complete-task + data-task-line", () => {
    const root = new FakeEl("div");
    const todos = root.createDiv({ cls: "life-todos" });
    const list = todos.createEl("ul");
    const bound = list.createEl("li");
    bound.createEl("strong", { text: "有绑定任务" });
    bound.setAttribute("data-complete-task", "tasks/today.md");
    bound.setAttribute("data-task-line", "3");
    const partial = list.createEl("li");
    partial.createEl("strong", { text: "缺行号任务" });
    partial.setAttribute("data-complete-task", "tasks/other.md");
    const none = list.createEl("li");
    none.createEl("strong", { text: "无绑定任务" });

    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.deepEqual(model.tasks[0].binding, { path: "tasks/today.md", line: 3 }, "完整绑定可用");
    assert.equal(model.tasks[1].binding, null, "缺 data-task-line 降级");
    assert.equal(model.tasks[2].binding, null, "无绑定降级");
  });

  it("projects asset and PARA paths only from explicit data-path, never from titles", () => {
    const root = new FakeEl("div");
    const good = root.createEl("article", { cls: "life-asset-card" });
    good.createEl("strong", { text: "资产甲" });
    const link = good.createEl("a", { text: "打开" });
    link.setAttribute("data-path", "assets/good.md");
    const bad = root.createEl("article", { cls: "life-asset-card" });
    bad.createEl("strong", { text: "无路径资产" });
    const db = root.createDiv({ cls: "life-database" });
    const para = db.createEl("article", { cls: "life-intelligence-card" });
    para.createEl("span", { cls: "life-note-link", text: "项目" });
    const paraLink = para.createEl("a", { text: "入口" });
    paraLink.setAttribute("data-href", "para/projects.md");

    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.equal(model.assets[0].path, "assets/good.md");
    assert.equal(model.assets[1].path, "", "无路径不从标题拼路径");
    assert.equal(model.para[0].path, "para/projects.md");
  });

  it("projects daily / watched / signal / overdue / blindspot read-only items", () => {
    const root = new FakeEl("div");
    const daily = root.createEl("article", { cls: "life-daily-card is-read" });
    const dailyLink = daily.createEl("a", { text: "今日综合日报" });
    dailyLink.setAttribute("data-path", "daily/2026-08-15.md");
    daily.createEl("small", { text: "08:00 自动生成" });
    const watch = root.createEl("article", { cls: "life-watch-card" });
    const watchLink = watch.createEl("a", { text: "持续关注甲" });
    watchLink.setAttribute("data-path", "watch/a.md");
    const signal = root.createEl("article", { cls: "life-signal-card" });
    const signalLink = signal.createEl("a", { text: "查看今日综合情报" });
    signalLink.setAttribute("data-path", "daily/brief.md");
    const attention = root.createEl("section", { cls: "life-attention" });
    const overdueItem = attention.createEl("ul").createEl("li");
    overdueItem.createEl("span", { text: "逾期任务甲" });
    const overdueSmall = overdueItem.createEl("small", { text: "截止 08-12" });
    const overdueLink = overdueSmall.createEl("a", { text: "来源" });
    overdueLink.setAttribute("data-path", "tasks/overdue.md");
    const blind = root.createEl("section", { cls: "life-blindspots" });
    const blindItem = blind.createEl("ul").createEl("li");
    blindItem.createEl("span", { text: "2 份今日自动化资料尚未阅读" });
    const blindButton = blindItem.createEl("button", { text: "只看未读" });
    blindButton.setAttribute("data-view-filter", "unread");

    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.equal(model.daily[0].path, "daily/2026-08-15.md");
    assert.equal(model.daily[0].read, true, "已读状态投影");
    assert.equal(model.watched[0].path, "watch/a.md");
    assert.equal(model.signals[0].path, "daily/brief.md");
    assert.equal(model.overdue[0].path, "tasks/overdue.md");
    assert.equal(model.blindspots[0].filter, "unread", "盲区动作走既有筛选");
  });

  it("merges 待处理/今日产出/知识规模 into read-only overview stats", () => {
    const root = new FakeEl("div");
    const stats = root.createDiv({ cls: "life-stat-strip" });
    for (const [label, value] of [["待处理", "4"], ["今日产出", "6"], ["知识规模", "256"]]) {
      const stat = stats.createDiv({ cls: "life-stat" });
      stat.createSpan({ text: label });
      stat.createEl("strong", { text: value });
    }
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.equal(model.pendingCount, 4);
    assert.equal(model.todayProgress, 6);
    assert.equal(model.knowledgeCount, 256);
  });
});

describe("Cosmos fragment journey panorama projection", () => {
  it("system states never leak into 等我决定 (TASK F G1/G11)", () => {
    const root = new FakeEl("div");
    const card = root.createEl("article", { cls: "life-organized-card" });
    card.createEl("strong", { text: "碎片" });
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    const items = [
      {
        fragment_id: "offline", title: "碎片 offline", episode_id: "e1", sequence: 1, status: "passed", route: "verify",
        execution: {
          route: "verify", status: "passed",
          research_progress: { cognitive: "capability_offline", stage: "collected", collected_sources: 0, stop_reason: "live_disabled" },
          harvest: [{ role: "failure", summary: "没有取得证据" }],
        },
      },
      {
        fragment_id: "watching", title: "碎片 watching", episode_id: "e2", sequence: 1, status: "passed", route: "verify",
        execution: {
          route: "verify", status: "passed",
          research_progress: { cognitive: "watching", stage: "collected", collected_sources: 0, plan_exhausted: true, watch: { reason: "plan_exhausted", attempts: 1 } },
          harvest: [{ role: "failure", summary: "没有取得证据" }],
        },
      },
      {
        fragment_id: "exhausted", title: "碎片 exhausted", episode_id: "e3", sequence: 1, status: "passed", route: "verify",
        execution: {
          route: "verify", status: "passed",
          research_progress: { cognitive: "search_exhausted", stage: "collected", collected_sources: 0, plan_exhausted: true, network_requests: 4 },
          harvest: [{ role: "failure", summary: "没有取得证据" }],
        },
      },
      {
        fragment_id: "awaiting", title: "碎片 awaiting", episode_id: "e4", sequence: 1, status: "passed", route: "verify",
        execution: {
          route: "verify", status: "passed",
          research_progress: { cognitive: "awaiting_model_authorization", stage: "awaiting_authorization", collected_sources: 2 },
          harvest: [{ role: "evidence", summary: "已收集 2 个来源" }],
        },
      },
    ];
    const journeys = buildFragmentJourneys(model, { intents: () => ({ items }) });
    const byId = (id) => journeys.find((item) => item.fragmentId === id);

    // G1：capability_offline → 系统待恢复，绝不是来源不足、归入系统阻塞、不算人工待办。
    assert.equal(byId("offline").statusLabel, "系统待恢复");
    assert.equal(byId("offline").stages.verify.label, "待恢复");
    assert.notEqual(byId("offline").state, "waiting");
    assert.equal(byId("offline").state, "blocked");
    // G11：watching 与 search_exhausted 是系统状态，绝不混入「等我决定」。
    assert.equal(byId("watching").statusLabel, "系统持续观察中");
    assert.notEqual(byId("watching").state, "waiting");
    assert.equal(byId("exhausted").statusLabel, "已按策略搜索未果");
    assert.equal(byId("exhausted").stages.verify.label, "本轮已结束");
    assert.notEqual(byId("exhausted").state, "waiting");
    assert.equal(byId("exhausted").nextAction.includes("没有已登记的自动复查"), true);
    // 只有真需要人工的模型授权才进入「等我决定」。
    assert.equal(byId("awaiting").state, "waiting");
    assert.equal(byId("awaiting").stages.verify.label, "等你决定");
    const waiting = journeys.filter((item) => item.state === "waiting");
    assert.deepEqual(waiting.map((item) => item.fragmentId), ["awaiting"],
      "「等我决定」只计算真正 human-required 的项目");
  });

  it("covers every explicitly bound fragment beyond the legacy four-card limit", () => {
    const root = new FakeEl("div");
    for (let index = 1; index <= 7; index += 1) {
      const card = root.createEl("article", { cls: "life-organized-card" });
      card.setAttribute("data-source-fragment", `fragment-${index}`);
      card.createEl("strong", { text: `碎片 ${index}` });
      card.createEl("p", { text: `目标 ${index}` });
      card.createEl("p", { text: `下一步 ${index}` });
    }
    const unbound = root.createEl("article", { cls: "life-organized-card" });
    unbound.createEl("strong", { text: "碎片 1" });
    unbound.createEl("p", { text: "同名但没有显式 fragment_id" });

    const model = buildCosmosModel(root, { total: 0, categories: [] });
    const journeys = buildFragmentJourneys(model);

    assert.equal(model.organized.length, 4, "旧阶段看板继续保留四卡视觉上限");
    assert.equal(journeys.length, 7, "全景不截断显式绑定的碎片");
    assert.equal(journeys.filter((item) => item.fragmentId === "fragment-1").length, 1,
      "同名无身份卡不通过标题猜测合并");
    assert.deepEqual(FRAGMENT_JOURNEY_STAGES.map(([, label]) => label),
      ["收集", "整理", "方向", "核验", "实践", "结论", "确认", "资产"]);
  });

  it("uses the latest episode and projects review outcomes without inventing progress", () => {
    const root = new FakeEl("div");
    for (const id of ["alpha", "beta", "gamma"]) {
      const card = root.createEl("article", { cls: "life-organized-card" });
      card.setAttribute("data-source-fragment", id);
      card.createEl("strong", { text: `碎片 ${id}` });
      card.createEl("p", { text: "目标：验证" });
      card.createEl("p", { text: "下一步：等待权威状态" });
    }
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    const items = [
      { fragment_id: "alpha", episode_id: "old", sequence: 2, status: "passed", route: "verify" },
      { fragment_id: "alpha", episode_id: "new", sequence: 3, status: "suggested", route: "verify" },
      {
        fragment_id: "beta", episode_id: "beta-1", sequence: 1, status: "passed", route: "verify",
        execution: {
          route: "verify", status: "passed",
          research_progress: { stage: "no_evidence", collected_sources: 0 },
          harvest: [{ role: "failure", summary: "无证据" }],
        },
      },
      { fragment_id: "gamma", episode_id: "gamma-1", sequence: 1, status: "passed", route: "direct" },
    ];
    const reviews = [
      { fragment_ref: "alpha", title: "碎片 alpha", content_status: "pending_confirmation" },
      { fragment_ref: "gamma", title: "碎片 gamma", content_status: "published_asset" },
    ];
    const journeys = buildFragmentJourneys(model, {
      intents: () => ({ items }),
      reviews: () => ({ items: reviews }),
    });
    const byId = (id) => journeys.find((item) => item.fragmentId === id);

    assert.equal(byId("alpha").alignment.episode_id, "new", "同一碎片只采用最新 episode");
    assert.equal(byId("alpha").state, "waiting");
    assert.equal(byId("alpha").stages.confirm.label, "等你确认");
    // rev5 硬规则：legacy no_evidence 缺乏可验证 network/plan_exhausted
    // 事实时保守投影为系统处理中——绝不显示「已按策略搜索未果」（0 次
    // 网络请求不得成为 search_exhausted）；failure harvest 不冒充候选。
    assert.equal(byId("beta").state, "active");
    assert.equal(byId("beta").stages.verify.label, "核验中");
    assert.equal(byId("beta").statusLabel, "系统核验中");
    assert.notEqual(byId("beta").statusLabel, "已按策略搜索未果");
    assert.notEqual(byId("beta").stages.conclusion.state, "done", "failure harvest 不得标记已有候选");
    assert.equal(byId("gamma").state, "done");
    assert.equal(byId("gamma").stages.verify.state, "skip", "直达路线不伪造核验完成");
    assert.equal(byId("gamma").stages.asset.label, "已入库");
  });

  it("shows unconfirmed takeover status, not a user todo, before any alignment exists", () => {
    const root = new FakeEl("div");
    const card = root.createEl("article", { cls: "life-organized-card" });
    card.setAttribute("data-source-fragment", "frag-auto");
    card.createEl("strong", { text: "Archify 能否可视化 Loop Graph？" });
    card.createEl("p", { text: "目标：判断是否引入" });
    card.createEl("p", { text: "下一步：下载仓库运行 doctor" });
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    const journeys = buildFragmentJourneys(model, { intents: () => ({ items: [] }) });
    const row = journeys.find((item) => item.fragmentId === "frag-auto");
    // 无记录 = 尚无接管证据——绝不声称「已选择进入 Loop，每分钟自动承接」，
    // 不把整理的执行清单显示成用户待办，也不计入「等我决定」。
    assert.equal(row.statusLabel, "尚未确认接管状态");
    assert.equal(row.stages.direction.label, "接管状态待确认");
    assert.notEqual(row.state, "waiting");
    assert.equal(row.nextAction.includes("尚无接管证据"), true);
    assert.equal(row.nextAction.includes("下载仓库"), false);
  });

  it("reflects per-fragment auto-propose outcomes instead of a blanket takeover label", () => {
    const root = new FakeEl("div");
    for (const id of ["frag-wait", "frag-fail", "frag-old", "frag-bad", "frag-skip", "frag-privacy", "frag-err", "frag-new"]) {
      const card = root.createEl("article", { cls: "life-organized-card" });
      card.setAttribute("data-source-fragment", id);
      card.createEl("strong", { text: `碎片 ${id}` });
      card.createEl("p", { text: "目标" });
    }
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    const autoPropose = [
      { fragment_id: "frag-wait", outcome: "waiting", reason: "organized_not_ready" },
      { fragment_id: "frag-fail", outcome: "failed", reason: "source_invalid" },
      { fragment_id: "frag-old", outcome: "deferred", reason: "before_cutover" },
      { fragment_id: "frag-bad", outcome: "indeterminate", reason: "organized_at_missing_or_invalid" },
      { fragment_id: "frag-skip", outcome: "skipped", reason: "not_approved" },
      { fragment_id: "frag-privacy", outcome: "skipped", reason: "privacy_excluded" },
    ];
    const journeys = buildFragmentJourneys(model, {
      intents: () => ({ items: [], autoPropose }),
    });
    const byId = (id) => journeys.find((item) => item.fragmentId === id);
    // 后台真实结果逐条呈现，绝不统一伪装成「系统接管中」。
    assert.equal(byId("frag-wait").statusLabel, "等待上游整理");
    assert.equal(byId("frag-wait").state, "blocked");
    assert.equal(byId("frag-fail").statusLabel, "接管失败");
    assert.equal(byId("frag-fail").state, "blocked", "接管失败是系统阻塞，不是用户待办");
    assert.equal(byId("frag-old").statusLabel, "历史碎片");
    assert.equal(byId("frag-old").stages.direction.label, "不自动承接");
    // 非法日期与「历史碎片」分开解释。
    assert.equal(byId("frag-bad").statusLabel, "无法判断承接资格");
    assert.notEqual(byId("frag-bad").statusLabel, "历史碎片");
    // 未勾选/隐私排除是跳过事实，不是「已选择进入 Loop」。
    assert.equal(byId("frag-skip").statusLabel, "未进入 Loop");
    assert.equal(byId("frag-privacy").statusLabel, "隐私保护排除");
    // 无记录的碎片：尚无接管证据。
    assert.equal(byId("frag-new").statusLabel, "尚未确认接管状态");
    // 各种异常都不计入「等我决定」。
    assert.equal(journeys.filter((item) => item.state === "waiting").length, 0);
    assert.equal(journeys.filter((item) => item.state === "active").length, 0);
    assert.equal(journeys.filter((item) => item.state === "untracked").length, 5);
    assert.equal(journeys.filter((item) => item.state === "blocked").length, 3);
  });

  it("closed history never becomes a new approval or hides a live alignment", () => {
    const model = buildCosmosModel(new FakeEl("div"), { total: 0, categories: [] });
    const base = { fragment_id: "live", alignment_id: "a-live", title: "当前新方向", status: "suggested", sequence: 5 };
    const reviews = [
      { fragment_ref: "live.md", status: "closed", content_status: "pending_confirmation", title: "过期审阅" },
      { fragment_ref: "only-old.md", status: "archived", content_status: "pending_confirmation", title: "已归档审阅" },
      { fragment_ref: "actual-review.md", status: "ready", content_status: "pending_confirmation", title: "确需确认" },
      { fragment_ref: "actual-review.md", status: "closed", content_status: "pending_confirmation", title: "更早结束的审阅" },
    ];
    const journeys = buildFragmentJourneys(model, { intents: () => ({ items: [base] }), reviews: () => ({ items: reviews }) });
    const live = journeys.find(row => row.fragmentId === "live");
    assert.equal(live.state, "waiting"); assert.equal(live.title, "当前新方向");
    assert.equal(live.statusLabel, "等我决定");
    assert.equal(journeys.find(row => row.fragmentId === "only-old").state, "untracked");
    assert.equal(journeys.find(row => row.fragmentId === "actual-review").state, "waiting");
    assert.equal(journeys.find(row => row.fragmentId === "actual-review").title, "确需确认");
    assert.equal(journeys.filter(row => row.state === "waiting").length, 2);
  });

  it("missing execution and ended alignments cannot claim system research is running", () => {
    const model = buildCosmosModel(new FakeEl("div"), { total: 0, categories: [] });
    const items = [
      { fragment_id: "missing", alignment_id: "a", title: "无执行回执", status: "passed", route: "verify", sequence: 1 },
      { fragment_id: "closed", alignment_id: "b", title: "已结束", status: "closed", route: "verify", sequence: 1 },
      { fragment_id: "archived", alignment_id: "c", title: "执行归档", status: "passed", route: "verify", sequence: 1, execution: { status: "archived" } },
    ];
    const rows = buildFragmentJourneys(model, { intents: () => ({ items }) });
    assert.ok(rows.every(row => row.state === "untracked"));
    assert.equal(rows.find(row => row.fragmentId === "missing").statusLabel, "尚未确认执行状态");
  });

  it("shows service failure ahead of any auto-propose record", () => {
    const root = new FakeEl("div");
    const card = root.createEl("article", { cls: "life-organized-card" });
    card.setAttribute("data-source-fragment", "frag-err");
    card.createEl("strong", { text: "碎片 frag-err" });
    const model = buildCosmosModel(root, { total: 0, categories: [] });
    const journeys = buildFragmentJourneys(model, {
      intents: () => ({
        items: [],
        error: "无法连接本地意图确认服务",
        autoPropose: [{ fragment_id: "frag-err", outcome: "proposed", reason: "" }],
      }),
    });
    const row = journeys.find((item) => item.fragmentId === "frag-err");
    // 服务故障优先于一切记录——故障时不声称任何接管进度。
    assert.equal(row.statusLabel, "服务故障");
    assert.equal(row.state, "blocked");
    assert.equal(row.nextAction.includes("无法连接本地意图确认服务"), true);
  });
});

describe("Cosmos V1 linkPath root priority", () => {
  it("prefers the element's own data-path/data-href over descendant links (production-shaped cards)", () => {
    const root = new FakeEl("div");
    // 生产同形：life-asset-card 的显式路径在根节点。
    const card = root.createEl("article", { cls: "life-asset-card" });
    card.setAttribute("data-path", "assets/root.md");
    card.createEl("strong", { text: "根路径资产" });
    const child = card.createEl("a", { text: "其他链接" });
    child.setAttribute("data-path", "assets/child.md");
    // 根节点无路径时才读后代链接。
    const nested = root.createEl("article", { cls: "life-asset-card" });
    nested.createEl("strong", { text: "后代路径资产" });
    const nestedLink = nested.createEl("a", { text: "打开" });
    nestedLink.setAttribute("data-path", "assets/nested.md");

    const model = buildCosmosModel(root, { total: 0, categories: [] });
    assert.equal(model.assets[0].path, "assets/root.md", "根节点 data-path 优先于后代链接");
    assert.equal(model.assets[1].path, "assets/nested.md", "根节点无路径才回落后代链接");
  });
});


it("source-only harvest is not a conclusion and system blockers are not user decisions", () => {
  const model = buildCosmosModel(new FakeEl("div"), { total: 0, categories: [] });
  const items = ["synthesis_disabled", "watch_budget_exhausted", "evidence_ready"].map((kind) => ({
    fragment_id: kind, title: kind, episode_id: kind, sequence: 1, status: "passed", route: "verify",
    execution: { status: "passed", route: "verify", result: { summary: "已收集一个来源，尚未形成判断" },
      harvest: [{ role: "evidence", summary: "已收集一个来源" }],
      research_progress: { cognitive: "evidence_ready", stage: "collected", collected_sources: 1,
        ...(kind === "evidence_ready" ? {} : { blocker: kind }) } },
  }));
  const rows = buildFragmentJourneys(model, { intents: () => ({ items }) });
  for (const item of items) {
    const row = rows.find((entry) => entry.fragmentId === item.fragment_id);
    assert.notEqual(row.state, "waiting");
    assert.notEqual(row.state, "candidate");
    assert.notEqual(row.stages.conclusion.state, "done");
    assert.notEqual(row.nextAction, "等待人工确认");
  }
  assert.equal(rows.find((row) => row.fragmentId === "synthesis_disabled").state, "blocked");
  assert.equal(rows.find((row) => row.fragmentId === "watch_budget_exhausted").state, "blocked");
});
