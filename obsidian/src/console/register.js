"use strict";

const { requestUrl, MarkdownRenderer, Component, loadMermaid, sanitizeHTMLToDom } = require("obsidian");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { createLoopApiClient, LOOP_API_BASE_URL } = require("./api-client.js");
const { createControlClient } = require("./control-client.js");
const { createMinimumValueClient } = require("./minimum-value-client.js");
const { createCognitiveDecisionClient } = require("./cognitive-decision-client.js");
const { createProductReviewClient } = require("./product-review-client.js");
const { createFragmentContinuationClient } = require("./fragment-continuation-client.js");
const {
  createFragmentIntentClient,
  fragmentInputDigest,
} = require("./fragment-intent-client.js");
const { createShadowProposalClient } = require("./proposal-client.js");
const { createShadowReviewClient } = require("./review-client.js");
const { createGraphClient } = require("./graph-client.js");
const { createAuthNotifier } = require("./auth-notify.js");
const model = require("./view-model.js");
const { LoopConsoleView, LOOP_CONSOLE_VIEW_TYPE } = require("./console-view.js");
const { GraphWorkflowView, GRAPH_WORKFLOW_VIEW_TYPE, GRAPH_DECISION_ERROR_LABELS } = require("./graph-view.js");
const {
  injectHomepageEntry,
  injectHomepageExperience,
  injectHomepageThoughtMap,
} = require("./home-entry.js");
const {
  prepareHomepageCosmos,
  failHomepageCosmos,
  finishHomepageCosmos,
  collectHomepageActivity,
  degradeHomepageCosmos,
  disposeHomepageCosmos,
  closestOf: cosmosClosestOf,
  hasClass: cosmosHasClass,
} = require("./homepage-cosmos.js");
const { injectHomepageGraphCard } = require("./graph-home-card.js");
const { injectFragmentIntentCards } = require("./fragment-intent-card.js");
const {
  injectHomepagePilotBridge,
  openPilotConfirmDialog,
  closePilotDialog,
} = require("./graph-pilot-bridge.js");
const {
  injectHomepageReviewDashboard,
  openReviewDialog,
} = require("./product-review-dashboard.js");

const SCAN_DEBOUNCE_MS = 300;
// 启动恢复窗口：收集失败或结果为空（metadata 未就绪）时有限重试，
// 延迟按 1s/2s/4s/8s 倍增；达到次数上限后停止并等待下一个稳定生命周期
// 事件（mutation / layout-change）重新打开窗口，绝不常驻轮询。
const SCAN_RETRY_BASE_MS = 1000;
const SCAN_RETRY_MAX_ATTEMPTS = 5;

async function obsidianTransport(request) {
  const options = {
    url: request.url,
    method: request.method,
    headers: request.headers,
    throw: false,
  };
  if (typeof request.body !== "undefined") options.body = request.body;
  const response = await requestUrl(options);
  let json = null;
  try {
    json = JSON.parse(response.text);
  } catch {
    json = null;
  }
  return { status: response.status, json, text: response.text };
}

// ponytail: 仅复用宿主内置流程图引擎，不修改 Vault 信任或全局 Mermaid 配置。
// 不调用 bindFunctions；输出二次收紧为静态 SVG，颜色由插件固定样式负责。
let knowledgeDiagramSequence = 0;
async function renderStaticKnowledgeFlow(body, container, isDisposed) {
  if (!/^\s*(?:flowchart|graph)\s+(?:TD|TB|BT|LR|RL)\b/.test(body)
    || body.length > 12000 || body.split("\n").length > 250 || (body.match(/-->|---|==>|-\.->/g) || []).length > 128
    || /\b(?:style|class|classDef|linkStyle)\b|&(?:#\d+|#x[\da-f]+|\w+);/i.test(body)) {
    throw new Error("仅支持无自定义样式、链接或 HTML 的小型静态流程图");
  }
  const engine = await loadMermaid();
  if (isDisposed()) return;
  if (engine?.mermaidAPI?.getConfig?.().securityLevel !== "strict") {
    throw new Error("内置图形引擎当前不是 strict 模式，已保留原图代码");
  }
  const doc = container.ownerDocument || document;
  const measurement = doc.body.createDiv({ cls: "life-knowledge-flow-measure" });
  measurement.setAttribute("aria-hidden", "true");
  measurement.style.position = "absolute";
  measurement.style.visibility = "hidden";
  measurement.style.pointerEvents = "none";
  const id = `life-knowledge-flow-${++knowledgeDiagramSequence}`;
  try {
    // 每图配置只关闭 HTML label；安全级别仍由已检查的 strict 引擎承担。
    const source = `---\nconfig:\n  htmlLabels: false\n  flowchart:\n    htmlLabels: false\n---\n${body}`;
    const result = await engine.render(id, source, measurement);
    if (isDisposed()) return;
    const fragment = sanitizeHTMLToDom(result.svg);
    const svg = fragment.querySelector("svg");
    if (!svg) throw new Error("图形引擎没有返回有效 SVG");
    const tags = new Set(["svg", "g", "path", "rect", "circle", "ellipse", "line", "polygon", "polyline", "text", "tspan", "defs", "marker", "title", "desc"]);
    const attributes = new Set(["id", "viewbox", "preserveaspectratio", "xmlns", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "rx", "ry", "r", "d", "points", "transform", "dx", "dy", "text-anchor", "dominant-baseline", "marker-start", "marker-mid", "marker-end", "markerwidth", "markerheight", "markerunits", "refx", "refy", "orient", "role", "aria-label", "aria-labelledby", "aria-describedby"]);
    const visit = (element) => {
      if (!tags.has(element.tagName.toLowerCase())) { element.remove(); return; }
      for (const attr of element.getAttributeNames()) {
        const name = attr.toLowerCase();
        if (!attributes.has(name) || name.startsWith("marker-") && !/^url\(#[\w-]+\)$/.test(element.getAttribute(attr))) element.removeAttribute(attr);
      }
      for (const child of [...element.children]) visit(child);
    };
    visit(svg);
    svg.setAttribute("role", "img");
    svg.setAttribute("pointer-events", "none");
    svg.setAttribute("aria-label", "研究回答中的静态流程图；表达结论结构，不代表额外核验");
    const viewBox = (svg.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
    if (viewBox.length !== 4 || !viewBox.every(Number.isFinite)
      || viewBox[2] <= 0 || viewBox[3] <= 0 || viewBox[2] > 100000 || viewBox[3] > 100000) {
      throw new Error("流程图缺少有效自然尺寸，已保留原图代码");
    }
    // 保留布局单位对应的天然字号；长图由独立滚动区承接，不缩小正文。
    svg.setAttribute("width", String(viewBox[2]));
    svg.setAttribute("height", String(viewBox[3]));
    container.createEl("small", { text: "流程图按原始字号显示；可横向滚动查看完整内容。", cls: "life-knowledge-flow-hint" });
    const viewport = container.createDiv({ cls: "life-knowledge-flow" });
    viewport.setAttribute("tabindex", "0");
    viewport.setAttribute("role", "region");
    viewport.setAttribute("aria-label", "流程图完整内容，可横向滚动");
    viewport.appendChild(svg);
  } finally { measurement.remove(); }
}

// ponytail: 只渲染核心 Markdown 与无交互 Mermaid；HTML、远程图片、
// Vault 嵌入和其他插件代码块保留为文本/链接，不激活任意代码块处理器。
function renderKnowledgeMarkdown(app, markdown, target, sourcePath) {
  if (typeof MarkdownRenderer?.render !== "function" || typeof Component !== "function") return null;
  const component = new Component();
  component.load();
  let disposed = false;
  const staging = target.ownerDocument.createElement("div");
  staging.addClass("markdown-rendered");
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    component.unload();
    staging.remove();
  };
  const finished = (async () => {
    const lines = String(markdown).split("\n");
    const staticLanguages = new Set(["", "text", "plaintext", "python", "py", "javascript", "js", "typescript", "ts", "json", "jsonc", "yaml", "yml", "toml", "ini", "bash", "sh", "shell", "zsh", "html", "xml", "css", "sql", "diff", "markdown", "md", "mermaid"]);
    const literal = (host, text) => {
      host.createEl("small", { text: "此段包含活动插件或查询语法，仅显示原文，不执行。" });
      // Dataview can rescan <code> even inside <pre>; literal text has no code node.
      host.createEl("pre", { text });
    };
    let plain = [];
    const renderPlain = async () => {
      if (!plain.length || disposed) return;
      const source = plain.join("\n");
      plain = [];
      if (/`+\s*\$?=|^[\s>+*\-\d.)]*\$?=/m.test(source)) { literal(staging, source); return; }
      const text = source
        .replace(/</g, "&lt;")
        .replace(/!\[/g, "\\![")
        .replace(/\[\[/g, "\\[\\[")
        .replace(/`{3,}|~{3,}/g, (fence) => [...fence].map((char) => `\\${char}`).join(""));
      await MarkdownRenderer.render(app, text, staging.createDiv(), sourcePath, component);
    };
    for (let index = 0; index < lines.length && !disposed; index += 1) {
      const fence = lines[index].match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
      if (!fence) { plain.push(lines[index]); continue; }
      await renderPlain();
      if (disposed) return;
      const code = [];
      const closing = new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`);
      for (index += 1; index < lines.length && !closing.test(lines[index]); index += 1) code.push(lines[index]);
      const body = code.join("\n");
      if (/^\s*\$?=/.test(body)) { literal(staging, body); continue; }
      const mermaid = fence[2].trim() === "mermaid"
        && !/%%\s*\{|^\s*---|\b(click|href|callback|securityLevel)\b|<\s*[!/a-z]|!\[|(?:javascript|https?|data|file|obsidian|app|ftp|wss?):|\b(img|image|icon)\s*:/im.test(body);
      if (mermaid && typeof loadMermaid === "function" && typeof sanitizeHTMLToDom === "function") {
        const diagram = staging.createDiv();
        try { await renderStaticKnowledgeFlow(body, diagram, () => disposed); }
        catch (error) { diagram.createEl("small", { text: `流程图暂未渲染：${error?.message || "内置引擎不可用"}` }); diagram.createEl("pre").createEl("code", { text: body }); }
      } else if (mermaid) await MarkdownRenderer.render(app, `\`\`\`mermaid\n${body}\n\`\`\``, staging.createDiv(), sourcePath, component);
      else if (!staticLanguages.has(fence[2].trim().toLowerCase())) literal(staging, body);
      else staging.createEl("pre").createEl("code", { text: body });
    }
    await renderPlain();
    if (disposed || target.isConnected === false) { dispose(); return; }
    // Markdown 链接不允许触发命令协议；Vault 链接沿原文路径解析。
    for (const link of staging.querySelectorAll("a")) {
      const href = link.getAttribute("data-href") || link.getAttribute("href") || "";
      if (/^https?:\/\//i.test(href)) {
        link.setAttribute("rel", "noopener noreferrer");
        link.setAttribute("target", "_blank");
      } else if (!href || /:|^\/\//.test(href)) {
        link.removeAttribute("href");
        link.removeAttribute("data-href");
      } else {
        link.addEventListener("click", (event) => {
          event.preventDefault?.();
          event.stopPropagation?.();
          Promise.resolve().then(() => app.workspace.openLinkText(href, sourcePath, false)).catch(() => {
            target.createEl("small", { text: "关联文档暂时无法打开。" });
          });
        });
      }
    }
    target.empty();
    target.appendChild(staging);
  })();
  return { finished, dispose };
}

// 绑定/修订类冲突（HTTP 409 或冻结冲突码）与一般失败分态：冲突必须保留
// 用户输入并单独呈现，绝不冒充成功。
const CONFLICT_CODES = new Set([
  "source_changed",
  "candidate_conflict",
  "plan_expired",
  "already_bridged",
  "lineage_drift",
  "escalation_binding_changed",
  "evidence_bundle_drift",
  "revision_conflict",
]);
function adapterErrorKind(error) {
  const status = Number(error && (error.status ?? error.statusCode ?? error.details?.status));
  const code = error && error.details && error.details.code;
  if (status === 409 || (code && CONFLICT_CODES.has(code))) return "conflict";
  return "error";
}

async function activateLoopConsole(app) {
  const existing = app.workspace.getLeavesOfType(LOOP_CONSOLE_VIEW_TYPE)[0];
  if (existing) {
    app.workspace.revealLeaf(existing);
    return;
  }
  const leaf = app.workspace.getLeaf("tab");
  await leaf.setViewState({ type: LOOP_CONSOLE_VIEW_TYPE, active: true });
  app.workspace.revealLeaf(leaf);
}

async function activateGraphWorkflow(app) {
  const existing = app.workspace.getLeavesOfType(GRAPH_WORKFLOW_VIEW_TYPE)[0];
  if (existing) {
    app.workspace.revealLeaf(existing);
    return;
  }
  const leaf = app.workspace.getLeaf("tab");
  await leaf.setViewState({ type: GRAPH_WORKFLOW_VIEW_TYPE, active: true });
  app.workspace.revealLeaf(leaf);
}

// ---- Cosmos 会话注册表（单一真相）----
// generation 序号与已落地会话记录的状态机。setupLoopConsole 内部持有一个
// 实例；工厂导出仅供测试对全新实例做白盒观测，不暴露任何活跃插件状态。
// 不变式：generation 只由注册它的一轮自己释放（get(home) === generation
// 才删除）；mounted 会话的 generation 由 sessions 持有，直到断开清理或
// unload；旧轮绝不得删除新轮状态。
function createCosmosRegistry() {
  const generations = new Map(); // 主页容器 -> 当前轮挂载身份
  const sessions = new Map(); // 主页容器 -> { home, root, mount, generation, dispose }
  // registry 级单调计数器：生命周期内挂载身份永久不复用（防 ABA 交错），
  // clear() 只在 unload 路径使用且计数器继续单调。
  let nextGeneration = 0;
  // 只释放自己注册的身份（逐字相同才删除）；更新一轮的身份绝不得删除。
  const releaseGeneration = (home, generation) => {
    if (generations.get(home) === generation) generations.delete(home);
  };
  // 每轮挂载开始：取新的全局唯一身份并销毁上一轮会话（含 Dataview
  // 重渲染已断开的旧根），绝不累积。
  const beginMount = (home) => {
    const generation = ++nextGeneration;
    generations.set(home, generation);
    sessions.get(home)?.dispose?.();
    sessions.delete(home);
    return generation;
  };
  const isCurrent = (home, generation) => generations.get(home) === generation;
  const recordSession = (home, record) => {
    sessions.set(home, record);
  };
  // 断开会话清理：root 断开、mount 断开、home 断开、主页 class 被移除且
  // 旧根不再属于主页——四种情况都释放会话资源并删除 Map 项。此入口只清理
  // 资源，绝不重新注入、恢复或挂载 Cosmos，也不引入任何轮询或常驻计时器。
  const cleanupDisconnected = () => {
    for (const [home, session] of [...sessions]) {
      const homeIsHomepage = cosmosHasClass(home, "my-life-homepage-view");
      const rootBelongsToHome = cosmosClosestOf(session.root, ".my-life-homepage-view") === home;
      const disconnected =
        home.isConnected === false ||
        session.root.isConnected === false ||
        session.mount.isConnected === false ||
        (!homeIsHomepage && !rootBelongsToHome);
      if (!disconnected) continue;
      session.dispose();
      sessions.delete(home);
      releaseGeneration(home, session.generation);
    }
  };
  const clear = () => {
    for (const session of sessions.values()) session.dispose();
    sessions.clear();
    generations.clear();
  };
  return {
    generations,
    sessions,
    beginMount,
    isCurrent,
    recordSession,
    releaseGeneration,
    cleanupDisconnected,
    clear,
  };
}

function setupLoopConsole(plugin) {
  const client = createLoopApiClient({ transport: obsidianTransport });
  const controlClient = createControlClient({ transport: obsidianTransport });
  const minimumValueClient = createMinimumValueClient({ transport: obsidianTransport });
  const cognitiveDecisionClient = createCognitiveDecisionClient({
    transport: obsidianTransport,
  });
  const productReviewClient = createProductReviewClient({ transport: obsidianTransport });
  const continuationClient = createFragmentContinuationClient({ transport: obsidianTransport });
  const intentClient = createFragmentIntentClient({ transport: obsidianTransport });
  const proposalClient = createShadowProposalClient({ transport: obsidianTransport });
  const reviewClient = createShadowReviewClient({ transport: obsidianTransport });
  const graphClient = createGraphClient({ transport: obsidianTransport });
  plugin.registerView(
    LOOP_CONSOLE_VIEW_TYPE,
    (leaf) =>
      new LoopConsoleView(
        leaf,
        client,
        controlClient,
        proposalClient,
        reviewClient,
        minimumValueClient,
        cognitiveDecisionClient,
        // R2 §4.6：队列卡与原位入口共享来源级 single-flight。
        { sharedAuthLocks: { isPending: (key) => authFlightMap.has(key), authKey } }
      )
  );
  plugin.registerView(
    GRAPH_WORKFLOW_VIEW_TYPE,
    (leaf) => new GraphWorkflowView(leaf, graphClient, { sharedAuthLocks: { isPending: (key) => authFlightMap.has(key), authKey } })
  );
  plugin.addCommand({
    id: "open-loop-console",
    name: "打开 Loop 控制台",
    callback: () => activateLoopConsole(plugin.app),
  });
  plugin.addCommand({
    id: "open-graph-workflow",
    name: "打开 Graph 工作流",
    callback: () => activateGraphWorkflow(plugin.app),
  });

  const openConsole = () => activateLoopConsole(plugin.app);
  const openGraph = () => activateGraphWorkflow(plugin.app);
  // 打开 Graph 视图并定位到指定 Run（桥创建成功/已桥接入口使用）。
  const openGraphRun = async (runId) => {
    await activateGraphWorkflow(plugin.app);
    const leaf = plugin.app.workspace.getLeavesOfType(GRAPH_WORKFLOW_VIEW_TYPE)[0];
    const view = leaf && leaf.view;
    if (view && typeof view.openRun === "function") view.openRun(runId);
  };
  const openThought = async (path) => {
    plugin.captureHomeScroll?.();
    // 独立标签保留主页；用户关闭文档标签后自然回到原主页。
    await plugin.app.workspace.openLinkText(path, "Notes/My Life.md", true);
  };
  let productReviewCache = null;
  let productReviewFetchedAt = 0;
  let productReviewLastError = "";
  const PRODUCT_REVIEW_CACHE_MS = 5000;
  // Fragment Continuation Bridge：只在首次加载与用户明确点击接续时取数。
  // DOM 重扫复用缓存；与 Graph 摘要相同，不引入轮询。
  let continuationState = [];
  let continuationLastError = "";
  let continuationFetchPromise = null;
  let continuationInitialFetchDone = false;
  const fetchContinuationStatuses = () => {
    if (continuationFetchPromise) return continuationFetchPromise;
    const promise = continuationClient.list().then(
      (items) => {
        if (!disposed) {
          continuationState = items;
          continuationLastError = "";
        }
        return items;
      },
      (error) => {
        if (!disposed) {
          continuationState = [];
          continuationLastError = error && error.message
            ? error.message
            : "Loop 接续服务暂不可用";
        }
        return [];
      }
    ).finally(() => {
      if (continuationFetchPromise === promise) continuationFetchPromise = null;
    });
    continuationFetchPromise = promise;
    return promise;
  };
  // Shared intent confirmation: initial read, explicit actions, and visible-page
  // progress refreshes share one flight. DOM rescans only consume this cache.
  let intentState = [];
  let intentLastError = "";
  let intentFetchPromise = null;
  let intentInitialFetchDone = false;
  let intentAvailable = false;
  let intentAutoPropose = [];
  const fetchIntentAlignments = () => {
    if (intentFetchPromise) return intentFetchPromise;
    const promise = intentClient.list().then(
      (items) => {
        if (!disposed) {
          intentState = items;
          intentAutoPropose = intentClient.autoProposeReport();
          intentLastError = "";
          intentAvailable = true;
        }
        return items;
      },
      (error) => {
        if (!disposed) {
          intentState = [];
          intentAutoPropose = [];
          intentLastError = error && error.message
            ? error.message
            : "处理方向服务暂不可用";
          intentAvailable = false;
        }
        return [];
      }
    ).finally(() => {
      if (intentFetchPromise === promise) intentFetchPromise = null;
    });
    intentFetchPromise = promise;
    return promise;
  };
  // Graph 主页摘要卡状态：独立失败域；仅首载与手动刷新取数，重扫复用缓存。
  let graphCardState = null;
  // 最近一次成功读取的 Graph 摘要：刷新失败时保留旧数据并标注服务错误。
  let graphLastGood = null;
  // 启动扫描、layout-ready、MutationObserver、布局变化与手动刷新共享同一个
  // in-flight Promise：同一时刻最多一个 listRuns()。
  let graphFetchPromise = null;
  let graphFetchGeneration = 0; // 较早响应不得覆盖较新结果
  let graphInitialFetchDone = false;
  let disposed = false;
  let scanTimer = null;
  let retryAttempts = 0;
  const fetchGraphRuns = () => {
    if (graphFetchPromise) return graphFetchPromise;
    const generation = ++graphFetchGeneration;
    const promise = (async () => {
      try {
        const runs = await graphClient.listRuns();
        // unload 后或已有更新取数时，迟到响应不得覆盖状态。
        if (!disposed && generation === graphFetchGeneration) {
          graphCardState = { kind: "ready", runs };
          graphLastGood = graphCardState;
          // R1：run 列表变化 → 授权队列重算（N 由状态复算一致）。
          fetchAuthQueue().catch(() => {});
        }
      } catch (error) {
        if (!disposed && generation === graphFetchGeneration) {
          // G8：区分首次失败 vs 刷新失败——刷新失败保留旧数据并显式标注
          // stale（graphLastGood 承诺了但此前未实现）；首次失败才显示错误态。
          graphCardState = graphLastGood
            ? { kind: "ready", runs: graphLastGood.runs, stale: true }
            : {
                kind: "error",
                message: error && error.message ? error.message : "Graph 服务暂时不可用",
              };
        }
      } finally {
        if (graphFetchPromise === promise) graphFetchPromise = null;
      }
    })();
    graphFetchPromise = promise;
    return promise;
  };
  const renderGraphCard = () => {
    if (disposed) return; // unload 后迟到结果不得更新 DOM
    injectHomepageGraphCard(document, graphCardState || { kind: "loading" }, {
      onOpen: openGraph,
      onRefresh: refreshGraphCard,
      onOpenRun: openGraphRun,
    });
  };
  const refreshGraphCard = async () => {
    // 手动刷新与任何进行中的取数共享同一个 in-flight Promise，绝不叠加并发。
    await fetchGraphRuns();
    renderGraphCard();
  };

  // ===== 授权队列（R1）数据层 =====
  // 队列卡从既有 client 状态派生（设计 §4）：Graph pending_human（canvas 授权
  // 材料）、Loop 审核 review actionsFor submittable、Loop 控制 control actionsFor
  // enabled 非终止。不做新状态存储——每次 fetch 重算，刷新后 N 一致。
  // 源状态形状：{ kind: "ready"|"error", items: [...], partial?: true }
  // —— partial=子请求部分失败（P1-5）。
  let reviewQueueState = null;
  let controlQueueState = null;
  let graphQueueState = null;
  let authQueueLastSyncAt = null; // V2-C5：队列取数完成时间戳（陈旧可见）
  let authQueueFetchPromise = null;
  let authQueueFetchDirty = false;
  let authQueueGeneration = 0;

  // 授权队列只承载「继续推进」：终止与调优必须留在完整控制台显式操作。
  const CONTROL_QUEUE_ACTIONS = new Set(["resume", "retry"]);
  // 先按 Loop 状态缩小查询面；control actions 仍是动作能否执行的最终权威。
  const CONTROL_QUEUE_STATUSES = new Set(["paused", "blocked", "failed_safe", "escalated", "exhausted"]);

  const fetchAuthQueue = () => {
    // P1-6（R25）：在途期间的状态变更标 dirty——settle 前恰好重跑一次最新快照；
    // 等待者拿到含最终重算的同一 promise；dispose 即停、零落地。
    if (authQueueFetchPromise) {
      authQueueFetchDirty = true;
      return authQueueFetchPromise;
    }
    const generation = ++authQueueGeneration;
    const writeSource = (assign) => { if (!disposed && generation === authQueueGeneration) assign(); };
    // P1-5：源内逐项失败标 partial（保留已取得卡，数量未知绝不伪装 ready/0）。
    const collect = async (entries, fn) => {
      const pending = [];
      let partial = false;
      for (const entry of entries) {
        try {
          pending.push(...await fn(entry));
        } catch {
          partial = true;
        }
      }
      return { pending, partial: partial || undefined };
    };
    const compute = async () => {
      // 1) Graph：runs 有 pending_human → canvas 授权材料（authorization_preview）
      try {
        // 等 graph 列表就绪（与 fetchGraphRuns 并行启动；mount 时渲染前已结算）。
        let runs = graphCardState && graphCardState.kind === "ready" ? graphCardState.runs : null;
        if (!runs && graphFetchPromise) {
          await graphFetchPromise;
          runs = graphCardState && graphCardState.kind === "ready" ? graphCardState.runs : null;
        }
        const pendingRuns = (runs || []).filter(
          (run) => run && run.run_id && Array.isArray(run.pending_human) && run.pending_human.length
        );
        const collected = await collect(pendingRuns, async (run) => {
          const canvasData = await graphClient.canvas(run.run_id);
          const nodes = canvasData && Array.isArray(canvasData.nodes) ? canvasData.nodes : [];
          const graphId = canvasData && canvasData.graph_id ? canvasData.graph_id : run.graph_id || "graph";
          const cards = [];
          for (const node of nodes) {
            if (!run.pending_human.includes(node.node_id)) continue;
            const gate = node.human_gate || null;
            const preview = gate && gate.authorization_preview ? gate.authorization_preview : null;
            if (!preview) continue;
            cards.push({
              runId: run.run_id,
              nodeId: node.node_id,
              taskLabel: node.task_label || node.display_label || node.node_id,
              graphId,
              preview,
              authorization: gate.authorization || null,
              // R2 §4.5：授权单过期判定材料（共享 isExpiredAt 判定）。
              expiresAt: (gate.authorization && gate.authorization.expires_at) || null,
            });
          }
          return cards;
        });
        writeSource(() => { graphQueueState = { kind: "ready", items: collected.pending, partial: collected.partial }; });
      } catch {
        writeSource(() => { graphQueueState = { kind: "error", message: "Graph 授权状态不可用" }; });
      }
      // 2) Loop 审核：proposals → actionsFor submittable
      try {
        const listBody = await proposalClient.proposals({ limit: 12 });
        // proposals() 返回完整信封（data.items 是列表）；shadow 契约 data 在 body.data。
        const list = listBody && listBody.data && Array.isArray(listBody.data.items) ? listBody.data.items : [];
        const collected = await collect(
          list.filter((proposal) => proposal && proposal.proposal_id),
          async (proposal) => {
            const actions = model.reviewActionsView(await reviewClient.actionsFor(proposal.proposal_id));
            return actions && actions.submittable
              ? [{
                  proposalId: proposal.proposal_id,
                  subject: proposal.subject || proposal.proposal_id,
                  runId: proposal.run_id || null,
                  actions,
                }]
              : [];
          }
        );
        writeSource(() => { reviewQueueState = { kind: "ready", items: collected.pending, partial: collected.partial }; });
      } catch {
        writeSource(() => { reviewQueueState = { kind: "error", message: "Loop 审核服务不可用" }; });
      }
      // 3) Loop 控制：Loop Projection runs → Control actions。
      // Graph 与 Loop 使用不同运行注册表，绝不能把 Graph run_id 交给 Control。
      try {
        const listBody = await client.runs({ limit: 200 });
        const runs = listBody && Array.isArray(listBody.items) ? listBody.items : [];
        // Historical superseded runs and this exact migration-test LoopSpec are not user approvals.
        const collected = await collect(
          runs.filter((run) => run && typeof run.run_id === "string" && run.run_id &&
            run.is_current !== false && !run.superseded_by_run_id &&
            run.loop_id !== "p2c-gate2-canary-v1" && CONTROL_QUEUE_STATUSES.has(run.status)),
          async (run) => {
            const view = model.controlActionsView(await controlClient.actionsFor(run.run_id));
            if (!view || view.runId !== run.run_id || !Number.isSafeInteger(view.latestSequence) || view.latestSequence < 1) {
              throw new Error("Loop 控制动作缺少有效运行与版本绑定");
            }
            const enabled = view.actions.filter(
              (entry) => entry.enabled && CONTROL_QUEUE_ACTIONS.has(entry.action)
            );
            return enabled.length
              ? [{
                  runId: run.run_id,
                  runLabel: `${run.loop_id || "Loop"} · ${run.status || ""}`,
                  expectedSequence: view.latestSequence,
                  actions: enabled,
                }]
              : [];
          }
        );
        writeSource(() => { controlQueueState = { kind: "ready", items: collected.pending, partial: collected.partial }; });
      } catch {
        writeSource(() => { controlQueueState = { kind: "error", message: "Loop 控制服务不可用" }; });
      }
      // R3 §4.8：三源就绪后统计待决并评估触达（0→N 触发；只发计数+分类）。
      // P1-5：任一源 error/partial 时数量未知——失败源不计入、不清零、不触发。
      if (!disposed && generation === authQueueGeneration) {
        const states = [graphQueueState, reviewQueueState, controlQueueState];
        const complete = states.every((state) => state && state.kind === "ready" && !state.partial);
        if (complete) {
          const graphCount = graphQueueState.items.length;
          const reviewCount = reviewQueueState.items.length;
          const controlCount = controlQueueState.items.length;
          const total = graphCount + reviewCount + controlCount;
          if (total > 0) {
            const sources = {};
            if (graphCount > 0) sources.Graph = graphCount;
            if (reviewCount + controlCount > 0) sources.Loop = reviewCount + controlCount;
            maybeSendAuthNotification(total, sources);
          } else {
            maybeSendAuthNotification(0, {});
          }
        }
      }
      // V2-C5：队列取数完成后登记数据时间戳（陈旧可见——卡堆标题展示
      // 「最近同步：{时间}」，用户能判断 N 的新旧；格式对齐 formatGraphTime）。
      writeSource(() => {
        authQueueLastSyncAt = new Date().toISOString();
      });
    };
    const promise = (async () => {
      do {
        authQueueFetchDirty = false;
        await compute();
      } while (authQueueFetchDirty && !disposed);
    })();
    authQueueFetchPromise = promise;
    promise.finally(() => {
      if (authQueueFetchPromise === promise) authQueueFetchPromise = null;
    });
    return promise;
  };
  // Pilot Entry Bridge：预案投影缓存（失败可重试）；创建只在用户点击确认时
  // 发生；桥是独立失败域，绝不影响碎片投递、Loop、主页与 Graph 摘要卡。
  let pilotPlanPromise = null;
  const fetchPilotPlan = () => {
    if (!pilotPlanPromise) {
      pilotPlanPromise = graphClient.getPilotPlan().catch((error) => {
        pilotPlanPromise = null;
        throw error;
      });
    }
    return pilotPlanPromise;
  };
  const PILOT_CREATE_ERROR_LABELS = {
    already_bridged: "该碎片已进入 Graph 工作流。",
    plan_expired: "候选内容已变化，请刷新后重新确认。",
    candidate_not_ready: "候选尚未就绪，暂时不能转为 Graph 工作流。",
    fragment_not_trusted: "碎片与候选的绑定校验未通过。",
    candidate_input_over_limit: "候选字段超出冻结上限，未创建。",
    candidate_source_unavailable: "候选来源暂不可用，请稍后重试。",
    spec_not_allowed: "该工作流模板不在允许清单内。",
  };
  const confirmPilotCreate = async (candidate, setMessage) => {
    try {
      const plan = await fetchPilotPlan();
      const outcome = await graphClient.createPilotRun({
        specDigest: plan.spec_digest,
        fragmentRef: candidate.fragment_ref,
        candidateId: candidate.candidate_id,
        candidateContentSha256: candidate.content_sha256,
      });
      closePilotDialog(document);
      await fetchGraphRuns();
      renderGraphCard();
      renderPilotBridge();
      await openGraphRun(outcome.run_id);
      return { kind: "success", message: "" };
    } catch (error) {
      const code = error && error.details && error.details.code;
      const message = (code && PILOT_CREATE_ERROR_LABELS[code]) ||
        (error && error.message ? error.message : "创建失败，请稍后重试。");
      setMessage(message);
      return { kind: adapterErrorKind(error), message };
    }
  };
  const startPilotConvert = async (candidate) => {
    try {
      const plan = await fetchPilotPlan();
      openPilotConfirmDialog(document, plan, candidate, {
        onConfirm: confirmPilotCreate,
      });
      return { kind: "success", message: "" };
    } catch (error) {
      // 预案不可读：诚实提示，不打开确认页；调用方不得显示成功。
      const message = error && error.message ? error.message : "Graph 预案暂不可用";
      pilotBridgeError = message;
      renderPilotBridge();
      return { kind: "error", message };
    }
  };
  let pilotBridgeError = "";
  const noteFile = (ref) => {
    const file = plugin.app.vault.getAbstractFileByPath(ref);
    return file && file.extension === "md" ? file : null;
  };
  const isLoopApproved = (rawRef) => {
    const file = noteFile(rawRef);
    const frontmatter = file
      ? plugin.app.metadataCache.getFileCache(file)?.frontmatter
      : null;
    return frontmatter?.["nigo-loop"] === true;
  };
  const proposeFragmentIntent = async (refs) => {
    const rawFile = noteFile(refs.rawRef);
    const organizedFile = noteFile(refs.organizedRef);
    if (!rawFile || !organizedFile) throw new Error("原始碎片或整理结果已不存在");
    const [rawText, organizedText] = await Promise.all([
      plugin.app.vault.read(rawFile),
      plugin.app.vault.read(organizedFile),
    ]);
    await intentClient.propose({
      fragmentId: refs.fragmentId,
      inputDigest: await fragmentInputDigest(rawText, organizedText),
    });
    if (disposed) return;
    await fetchIntentAlignments();
    renderIntentCards();
    renderPilotBridge();
  };
  const decideFragmentIntent = async (item, decision, setMessage) => {
    try {
      await intentClient.decide(item, decision);
      if (disposed) return { kind: "error", message: "" };
      await fetchIntentAlignments();
      const message = decision.action === "save_only" ? "已按你的选择仅保存。" : "方向已确认，正在按最短路径推进。";
      setMessage(message);
      renderIntentCards();
      renderPilotBridge();
      return { kind: "success", message };
    } catch (error) {
      const message = error && error.message ? error.message : "确认失败，请稍后重试。";
      setMessage(message);
      return { kind: adapterErrorKind(error), message };
    }
  };
  const continueFragmentIntent = async (item, goal, setMessage) => {
    try {
      const nextItem = await intentClient.continueEpisode(item, goal);
      if (disposed) return { kind: "error", message: "" };
      await fetchIntentAlignments();
      const message = "新的处理目标已建立，等待你确认方向。";
      setMessage(message);
      renderIntentCards();
      /* 把服务端返回的新 episode 交还当前 Cosmos 抽屉，使用户立即看见
         真实状态；它不是第二套状态，仍是同一个权威 alignment envelope。 */
      return { kind: "success", message, item: nextItem };
    } catch (error) {
      const message = error && error.message ? error.message : "暂时无法续接此结果。";
      setMessage(message);
      return { kind: adapterErrorKind(error), message };
    }
  };
  const escalateFragmentIntent = async (item, setMessage) => {
    try {
      const outcome = await intentClient.escalate(item);
      if (disposed) return { kind: "error", message: "" };
      // 与 decide/continue/createResearchRun 对齐：成功后刷新同一权威状态。
      await fetchIntentAlignments();
      const message = outcome.graph_run_created
        ? "已进入 Graph 工作流。"
        : "升级理由已保留；当前没有匹配的工作流模板。";
      setMessage(message);
      renderIntentCards();
      renderPilotBridge();
      return { kind: "success", message };
    } catch (error) {
      const message = error && error.message ? error.message : "Graph 升级提案暂不可用。";
      setMessage(message);
      return { kind: adapterErrorKind(error), message };
    }
  };
  const RESEARCH_CREATE_ERROR_LABELS = {
    invalid_body: "升级提案材料不完整，未创建研究 Run。",
    spec_not_allowed: "该工作流模板不在允许清单内。",
    plan_expired: "研究模板已变化，请刷新后重新确认。",
    lineage_not_found: "升级来源已不存在，未创建研究 Run。",
    lineage_drift: "升级来源已变化，未创建研究 Run。",
    escalation_not_available: "升级提案已不可用。",
    escalation_binding_changed: "升级提案绑定已变化，未创建研究 Run。",
    evidence_bundle_drift: "证据已变化，未创建研究 Run。",
    already_bridged: "该升级已经创建过研究 Run。",
    unsafe_prefix: "研究流程安全检查未通过，未创建。",
  };
  const RESEARCH_CREATE_STATUS_LABELS = {
    human_wait: "研究 Run 已创建，等待你确认授权。",
    completed: "研究 Run 已创建并完成本轮执行。",
    blocked: "研究 Run 已创建但被阻止，未调用模型。",
  };
  // Research Creation Bridge：与既有 fragment intent 相同的 single-flight
  // 生命周期——双击/重放共享同一个 in-flight Promise（零重复提交）；迟到
  // 响应与页面 unload 经 disposed 守卫，绝不更新 DOM；创建成功后刷新同一
  // 权威状态（alignments + Graph runs）并定位到该 Run。
  let researchCreatePromise = null;
  const createFragmentResearchRun = (item, setMessage) => {
    if (researchCreatePromise) {
      // 跨卡片 single-flight：另一张卡的在途创建共享同一 Promise（零重复
      // 提交），但必须给当前卡用户可见反馈，绝不静默吞掉这次点击。
      setMessage("另一项操作正在进行，请稍候再试。");
      return researchCreatePromise;
    }
    const promise = (async () => {
      try {
        const outcome = await intentClient.createResearchRun(item);
        if (disposed) return { kind: "error", message: "" };
        await fetchIntentAlignments();
        await fetchGraphRuns();
        const message = RESEARCH_CREATE_STATUS_LABELS[outcome.status] || "研究 Run 创建请求已受理。";
        setMessage(message);
        renderIntentCards();
        renderGraphCard();
        if (outcome.run_id) await openGraphRun(outcome.run_id);
        return { kind: "success", message };
      } catch (error) {
        if (disposed) return { kind: "error", message: "" };
        const code = error && error.details && error.details.code;
        const message = (code && RESEARCH_CREATE_ERROR_LABELS[code]) ||
          (error && error.message ? error.message : "研究 Run 创建失败，请稍后重试。");
        setMessage(message);
        return { kind: adapterErrorKind(error), message };
      }
    })().finally(() => {
      if (researchCreatePromise === promise) researchCreatePromise = null;
    });
    researchCreatePromise = promise;
    return promise;
  };
  const renderIntentCards = () => {
    if (disposed) return;
    try {
      injectFragmentIntentCards(document, {
        items: intentState,
        error: intentLastError,
      }, {
        isLoopApproved,
        onPropose: proposeFragmentIntent,
        onDecide: decideFragmentIntent,
        onContinue: continueFragmentIntent,
        onEscalate: escalateFragmentIntent,
        onCreateResearchRun: createFragmentResearchRun,
        onOpenKnowledge: openThought,
      });
    } catch (error) {
      console.error("[My Life] 碎片处理方向注入失败", error);
    }
  };
  const CONTINUATION_ERROR_LABELS = {
    loop_not_registered: "Loop 尚未登记这条碎片。",
    fragment_not_approved: "这条碎片没有签名进入 Loop。",
    organized_not_ready: "整理结果尚未达到可接续状态。",
    source_mismatch: "原始碎片与整理结果的绑定不一致。",
    source_unsafe: "整理来源不在允许范围内。",
    source_changed: "碎片或整理结果已变化，请刷新主页后重试。",
    route_not_allowed: "这条碎片暂不属于研究接续类型。",
    candidate_conflict: "候选写入发生冲突，未覆盖已有内容。",
  };
  const continueFragmentResearch = async (refs, setMessage) => {
    try {
      const rawFile = noteFile(refs.rawRef);
      const organizedFile = noteFile(refs.organizedRef);
      if (!rawFile || !organizedFile) throw new Error("原始碎片或整理结果已不存在");
      const [rawText, organizedText] = await Promise.all([
        plugin.app.vault.read(rawFile),
        plugin.app.vault.read(organizedFile),
      ]);
      const outcome = await continuationClient.continueResearch({
        fragmentId: refs.basename,
        rawRef: refs.rawRef,
        organizedRef: refs.organizedRef,
        rawText,
        organizedText,
      });
      if (disposed) return;
      await fetchContinuationStatuses();
      productReviewCache = await productReviewClient.list();
      productReviewFetchedAt = Date.now();
      productReviewLastError = "";
      setMessage(
        outcome.continuation_status === "already_published"
          ? "接续已完成，候选保持唯一。"
          : "Loop 接续完成，候选已生成。"
      );
      renderPilotBridge();
    } catch (error) {
      const code = error && error.details && error.details.code;
      setMessage(
        (code && CONTINUATION_ERROR_LABELS[code]) ||
          (error && error.message ? error.message : "Loop 接续失败，请稍后重试。")
      );
    }
  };
  const renderPilotBridge = () => {
    if (disposed) return;
    try {
      injectHomepagePilotBridge(
        document,
        {
          candidates: productReviewCache || [],
          continuations: continuationState,
          runs: graphCardState && graphCardState.kind === "ready" ? graphCardState.runs : [],
          continuationError: continuationLastError,
          alignments: intentState,
          alignmentAvailable: intentAvailable,
          error:
            pilotBridgeError ||
            (graphCardState && graphCardState.kind === "error" ? graphCardState.message : ""),
        },
        {
          isLoopApproved,
          onContinueResearch: continueFragmentResearch,
          onStartConvert: startPilotConvert,
          onOpenRun: openGraphRun,
        }
      );
    } catch (error) {
      console.error("[My Life] Graph Pilot 入口注入失败", error);
    }
  };
  // 人工确认面板取数：扫描与 Cosmos 显式挂载共享同一份缓存与同一个
  // in-flight Promise；失败是独立错误域，绝不影响其他卡片。
  let productReviewFetchPromise = null;
  const fetchProductReviews = () => {
    if (productReviewCache && Date.now() - productReviewFetchedAt < PRODUCT_REVIEW_CACHE_MS) {
      return Promise.resolve();
    }
    if (productReviewFetchPromise) return productReviewFetchPromise;
    const promise = (async () => {
      try {
        productReviewCache = await productReviewClient.list();
        productReviewFetchedAt = Date.now();
        productReviewLastError = "";
      } catch (error) {
        productReviewCache = [];
        productReviewFetchedAt = Date.now();
        productReviewLastError = error && error.message ? error.message : "本地人工确认服务暂不可用";
      }
    })().finally(() => {
      if (productReviewFetchPromise === promise) productReviewFetchPromise = null;
    });
    productReviewFetchPromise = promise;
    return promise;
  };
  // 目录是后端知识库的内存投影。失败保留上次成功数据，并明确标注过期。
  let knowledgeState = { kind: "loading", topics: [], error: "", stale: false };
  let knowledgeNotificationsState = { kind: "loading", items: [], error: "", stale: false };
  let knowledgePeriodsState = { kind: "loading", items: [], error: "", stale: false };
  let knowledgeFetchedAt = 0;
  let knowledgeFetchPromise = null;
  const fetchKnowledge = (force = false) => {
    if (knowledgeFetchPromise) return knowledgeFetchPromise;
    if (!force && knowledgeFetchedAt && Date.now() - knowledgeFetchedAt < 5000) return Promise.resolve();
    const pending = Promise.allSettled([intentClient.knowledgeCatalog(), intentClient.knowledgeNotifications(), intentClient.knowledgePeriods()]).then(([catalog, notices, periods]) => {
      if (disposed) return;
      knowledgeState = catalog.status === "fulfilled"
        ? { kind: "ready", topics: catalog.value, error: "", stale: false }
        : { kind: "error", topics: knowledgeState.topics,
          stale: knowledgeState.kind === "ready" || knowledgeState.stale,
          error: catalog.reason?.message || "知识服务暂不可用" };
      knowledgeNotificationsState = notices.status === "fulfilled"
        ? { kind: "ready", items: notices.value, error: "", stale: false }
        : { kind: "error", items: knowledgeNotificationsState.items,
          stale: knowledgeNotificationsState.kind === "ready" || knowledgeNotificationsState.stale,
          error: notices.reason?.message || "知识更新暂不可用" };
      knowledgePeriodsState = periods.status === "fulfilled"
        ? { kind: "ready", items: periods.value, error: "", stale: false }
        : { kind: "error", items: knowledgePeriodsState.items,
          stale: knowledgePeriodsState.kind === "ready" || knowledgePeriodsState.stale,
          error: periods.reason?.message || "周/月总览暂不可用" };
    }).finally(() => {
      knowledgeFetchedAt = Date.now();
      if (knowledgeFetchPromise === pending) knowledgeFetchPromise = null;
    });
    knowledgeFetchPromise = pending;
    return pending;
  };
  // Loop 人工确认对话的唯一权威入口：legacy 看板与 Cosmos 语义抽屉共用，
  // 候选详情、知识卡多选、二次确认、保留/拒绝/撤回/重开全部由既有
  // openReviewDialog + productReviewClient（按 available_actions）承担。
  const refreshReviewsAfterChange = async () => {
    productReviewCache = null;
    productReviewFetchedAt = 0;
    await scanHomepage();
  };
  const openReview = (item) => openReviewDialog(document, item, productReviewClient, {
    openNote: openThought,
    onChanged: refreshReviewsAfterChange,
    confirmAction: (message) => window.confirm(message),
  });
  const renderReviewDashboard = () => {
    if (disposed || !productReviewCache) return;
    try {
      injectHomepageReviewDashboard(document, productReviewCache || [], {
        openReview,
        errorMessage: productReviewLastError,
      });
    } catch (error) {
      console.error("[My Life] 人工确认面板注入失败", error);
    }
  };
  // Graph / Continuation / Intent 三个初始取数只启动一次；启动扫描、
  // MutationObserver、布局变化与 Cosmos 显式挂载共享同一批 in-flight Promise。
  const ensureInitialFetches = () => {
    if (!graphInitialFetchDone) {
      graphInitialFetchDone = true;
      fetchGraphRuns().then(() => {
        renderGraphCard();
        renderPilotBridge();
      });
    }
    if (!continuationInitialFetchDone) {
      continuationInitialFetchDone = true;
      fetchContinuationStatuses().then(renderPilotBridge);
    }
    if (!intentInitialFetchDone) {
      intentInitialFetchDone = true;
      fetchIntentAlignments().then(() => {
        renderIntentCards();
        renderPilotBridge();
      });
    }
    // R1 授权队列：首次初始取数（依赖 graphCardState 提供 run 列表；失败不阻断）。
    if (authQueueFetchPromise === null) {
      fetchAuthQueue().catch(() => {});
    }
  };
  // Thought Map 投影共享 in-flight：扫描重试与 Cosmos 挂载同一时刻最多
  // 一次收集；重试发生在上一次结算之后，语义不变。
  let thoughtMapFetchPromise = null;
  const fetchThoughtMap = () => {
    if (thoughtMapFetchPromise) return thoughtMapFetchPromise;
    const promise = Promise.resolve()
      .then(() => plugin.getThoughtMap())
      .finally(() => {
        if (thoughtMapFetchPromise === promise) thoughtMapFetchPromise = null;
      });
    thoughtMapFetchPromise = promise;
    return promise;
  };
  // 现有产品卡全部写入 Dataview 拥有的 legacy 区域（document 级幂等注入），
  // Cosmos 挂载与 MutationObserver 扫描都复用这一个入口。
  const renderHomepageCards = () => {
    if (disposed) return;
    renderReviewDashboard();
    renderGraphCard();
    renderIntentCards();
    renderPilotBridge();
  };
  // ---- Cosmos 显式挂载握手（单一渲染所有权）----
  // My Life.md 在全部 root.innerHTML、主题 UI 与旧主页 DOM 创建完成后最后
  // 调用此方法；插件只把 Cosmos 写进 [data-life-cosmos-mount]，绝不移动、
  // 清空或重新包裹 Dataview 拥有的其他节点。返回值诚实区分三种结局：
  // "mounted"（落地）/ "failed"（结构或渲染失败，已诚实降级）/ "stale"
  // （新一轮接管、root/mount 断开或 unload，迟到结果零落地）。
  // generation 生命周期：beginMount 注册；mounted 后由会话记录持有直到断开
  // 清理或 unload；最新一轮断开返回 stale 前、当前轮失败完成诚实降级返回
  // failed 前，都必须释放自身 generation；旧轮（mismatch）与 unload 路径
  // 绝不触碰新轮或已清空的状态。
  // ---- Cosmos capability adapter（冻结语义动作层）----
  // 每个具名 actionId 只转发现一个既有 handler/client/dialog：adapter 不含第二套
  // 业务判断；返回 { kind: "success" | "conflict" | "error", message }，由 Cosmos 分态呈现。
  const callPluginMethod = async (method, ...args) => {
    if (typeof plugin[method] !== "function") {
      return { kind: "error", message: "该能力在当前环境不可用" };
    }
    await plugin[method](...args);
    return { kind: "success", message: "" };
  };
  const noopMessage = () => {};
  // R2 §4.6：来源级 single-flight（{source, ref} 分键）——队列卡与原位入口共享
  // 同一 in-flight Promise；pending 期间另一入口复用（对齐 graph-view runExclusive 先例）。
  const authFlightMap = new Map();
  // R2 §4.7：已批准记录（撤回标的）——批准成功后登记；撤回契约存在才渲染按钮。
  const approvedAuthMap = new Map();
  // R2 §4.5：漂移码 = 证据失效信号（卡标失效态而非普通错误）。
  const AUTH_STALE_CODES = new Set([
    "sequence_mismatch",
    "stale_sequence",
    "candidate_version_drift",
    "gate_not_pending",
    "plan_expired",
    "candidate_not_ready",
  ]);
  const authKey = (source, ref) => {
    if (source === "graph") return `graph:${ref.runId}:${ref.nodeId}`;
    if (source === "loop-review") return `loop-review:${ref.proposalId}`;
    if (source === "loop-control") return `loop-control:${ref.runId}:${ref.action}`;
    if (source === "intent") return `intent:${ref.elementId || "card"}`;
    return `${source}:${JSON.stringify(ref)}`;
  };
  const authFlightStatus = (key) => authFlightMap.has(key);
  // R3 §4.8：攒批微信触达——待决数 0→N 才通知；内容只发计数与来源分类，绝不外发
  // 授权材料。冷却 30 分钟（R3c sidecar 持久化 lastNotifyAt/lastCount，重载/多实例
  // 仍生效）；归零恢复不通知；读写失败静默降级内存态。判定逻辑在 auth-notify.js。
  // ⚠️ 默认路径即生产消费目录——隔离实例/测试必须注入 authNotifyDir 到 /tmp。
  const AUTH_NOTIFY_SIDECAR = ".last-notify.json";
  const authNotifyPrefs = () => {
    const prefs = typeof plugin.getViewPreferences === "function" ? plugin.getViewPreferences() : {};
    return {
      dir: prefs.authNotifyDir || path.join(os.homedir(), ".local", "share", "loop-graph-cosmos", "auth-notifications"),
      enabled: prefs.authNotify !== false, // 默认开攒批
    };
  };
  const authNotify = createAuthNotifier({
    enabled: () => authNotifyPrefs().enabled,
    persist: {
      load: () => {
        try {
          return JSON.parse(fs.readFileSync(path.join(authNotifyPrefs().dir, AUTH_NOTIFY_SIDECAR), "utf8"));
        } catch { return null; }
      },
      save: (state) => {
        try {
          const dir = authNotifyPrefs().dir;
          fs.mkdirSync(dir, { recursive: true });
          fs.writeFileSync(path.join(dir, AUTH_NOTIFY_SIDECAR), JSON.stringify(state), "utf8");
        } catch { /* 写入失败静默降级内存态 */ }
      },
    },
  });
  const writeAuthNotification = (payload) => {
    try {
      const dir = authNotifyPrefs().dir;
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, `auth-notify-${Date.now()}.json`), JSON.stringify(payload, null, 2), "utf8");
    } catch { /* 写入失败静默——不影响授权队列；通知是尽力而为 */ }
  };
  const maybeSendAuthNotification = (pendingCount, sourceCounts) => {
    const payload = authNotify.evaluate(pendingCount, sourceCounts);
    if (payload) writeAuthNotification(payload);
  };
  const capabilities = {
    "capture.quick": () => callPluginMethod("openQuickCapture"),
    "loop.openConsole": async () => { await openConsole(); return { kind: "success", message: "" }; },
    "graph.openWorkflow": async () => { await openGraph(); return { kind: "success", message: "" }; },
    "graph.refresh": async () => {
      // 手动刷新与任何进行中的取数共享同一个 in-flight Promise；失败时旧摘要保留在
      // graphLastGood。G8：主动刷新失败（含 stale）必须诚实报 error。
      await Promise.all([refreshGraphCard(), fetchKnowledge(true), fetchIntentAlignments()]);
      renderIntentCards();
      // R2 §4.5：刷新后授权队列重算完成再返回（过期卡恢复有效 → 按钮重新可用）。
      await fetchAuthQueue();
      if (graphCardState && (graphCardState.kind === "error" || graphCardState.stale)) {
        return { kind: "error", message: graphCardState.message || "Graph 服务刷新失败，显示上次数据" };
      }
      if (intentLastError) return { kind: "error", message: `碎片状态刷新失败：${intentLastError}` };
      if (knowledgeState.kind === "error") return { kind: "error", message: `知识目录刷新失败：${knowledgeState.error}` };
      return { kind: "success", message: "" };
    },
    "knowledge.refresh": async () => {
      await fetchKnowledge(true);
      return knowledgeState.kind === "error"
        ? { kind: "error", message: knowledgeState.error } : { kind: "success", message: "知识目录已更新" };
    },
    // R1 授权队列一键：按 source 映射到既有 client 方法（零新协议）。
    "repair.request": createRepairRequestCapability(plugin),
    "auth.approve": async (queueItem) => {
      if (!queueItem || typeof queueItem.source !== "string" || !queueItem.ref) {
        return { kind: "error", message: "授权参数无效" };
      }
      const { source, ref } = queueItem;
      if (source === "loop-control" && (typeof ref.runId !== "string" || !ref.runId ||
        !CONTROL_QUEUE_ACTIONS.has(ref.action) || !Number.isSafeInteger(ref.expectedSequence) || ref.expectedSequence < 1)) {
        return { kind: "error", message: "控制动作的运行或版本绑定无效，请刷新后重试。", stale: true };
      }
      const key = authKey(source, ref);
      // §4.6：同一授权项在途 → 复用在途 Promise（不叠加并发）。
      if (authFlightMap.has(key)) return authFlightMap.get(key);
      const pending = (async () => {
        try {
          let successMessage = "已授权并推进";
          if (source === "graph") {
            const [detailData, canvasData] = await Promise.all([
              graphClient.getRun(ref.runId),
              graphClient.canvas(ref.runId),
            ]);
            const gate = detailData && detailData.human_gates ? detailData.human_gates[ref.nodeId] : null;
            const canvasNode = canvasData && Array.isArray(canvasData.nodes)
              ? canvasData.nodes.find((node) => node.node_id === ref.nodeId)
              : null;
            const auth = canvasNode && canvasNode.human_gate ? canvasNode.human_gate.authorization : null;
            if (!gate || !auth || !auth.authorization_digest) {
              return { kind: "error", message: "授权绑定不可用，请刷新后重试" };
            }
            await graphClient.submitHumanDecision({
              runId: ref.runId,
              nodeId: ref.nodeId,
              decision: "approve_call",
              specDigest: detailData.run.spec_digest,
              inputDigest: gate.input_digest,
              expectedSequence: gate.expected_sequence,
              authorizationDigest: auth.authorization_digest,
            });
          } else if (source === "loop-review") {
            const actions = ref.actions;
            await reviewClient.submitDecision({
              proposalId: ref.proposalId,
              decision: "accepted",
              expectedFingerprint: actions.expectedFingerprint,
              expectedSourceSequence: actions.expectedSourceSequence,
              expectedProposalStatus: actions.expectedProposalStatus,
            });
          } else if (source === "loop-control") {
            const result = await controlClient.submitIntent({
              runId: ref.runId,
              action: ref.action,
              expectedSequence: ref.expectedSequence,
              arguments: {},
            });
            const receipt = result && result.receipt;
            if (!receipt || Array.isArray(receipt) || typeof receipt.intent_id !== "string" || !receipt.intent_id ||
              receipt.run_id !== ref.runId || receipt.action !== ref.action || receipt.expected_sequence !== ref.expectedSequence ||
              !["applied", "accepted", "rejected", "failed"].includes(receipt.status)) {
              return { kind: "error", message: "控制服务回执缺失或绑定不一致，尚不能确认指令已生效。" };
            }
            if (receipt.status === "accepted") {
              return { kind: "pending", message: "指令已受理，尚待执行；当前未登记为已授权，请刷新状态或查看运行记录。" };
            }
            if (receipt.status !== "applied") {
              const stale = AUTH_STALE_CODES.has(receipt.reason_code);
              const reason = stale ? `数据已变化，请刷新后重试（${receipt.reason_code}）`
                : model.reasonText(receipt.reason_code) || "服务端未提供原因";
              return { kind: "error", message: `控制指令${receipt.status === "rejected" ? "被拒绝" : "执行失败"}：${reason}`, stale: stale || undefined };
            }
            if (ref.action === "retry" && (typeof receipt.result_run_id !== "string" || !receipt.result_run_id || receipt.result_run_id === ref.runId)) {
              return { kind: "error", message: "重试回执缺少新运行绑定，尚不能确认重试任务已创建。" };
            }
            successMessage = ref.action === "retry"
              ? "重试任务已创建，实际执行进度请查看运行记录。"
              : "继续运行指令已生效，实际执行进度请查看运行记录。";
          } else if (source === "intent") {
            return await capabilities["intent.confirm"](ref.element, ref.intents, ref.supplement || "");
          } else {
            return { kind: "error", message: "未知授权来源" };
          }
          // 授权成功：登记已批准记录（§4.7 撤回标的）；先等 Graph 摘要更新
          // （成功回调会触发 fetchAuthQueue），再在新 graphCardState 上重算队列。
          approvedAuthMap.set(key, { source, ref, message: successMessage, approvedAt: new Date().toISOString() });
          await Promise.allSettled([fetchGraphRuns()]);
          await fetchAuthQueue();
          renderGraphCard();
          return { kind: "success", message: successMessage };
        } catch (error) {
          const code = error && error.details && error.details.code;
          let message;
          if (source === "graph") {
            message = (code && GRAPH_DECISION_ERROR_LABELS[code])
              || (error && error.message ? error.message : "授权失败，请刷新后重试。");
          } else if (source === "loop-review") {
            message = (code ? model.reviewReasonText(code) : null)
              || (error && error.message ? error.message : "审核提交失败，请刷新后重试。");
          } else if (source === "loop-control") {
            message = (code ? model.reasonText(code) : null)
              || (error && error.message ? error.message : "控制提交失败，请刷新后重试。");
          } else {
            message = error && error.message ? error.message : "授权失败";
          }
          // R2 §4.5：漂移码 → 卡标失效态（非普通错误）；UI 据此降级一键。
          const stale = Boolean(code && AUTH_STALE_CODES.has(code));
          return { kind: adapterErrorKind(error), message, stale: stale || undefined };
        } finally {
          if (authFlightMap.get(key) === pending) authFlightMap.delete(key);
        }
      })();
      authFlightMap.set(key, pending);
      return pending;
    },
    // R2 §4.7：批后撤回——仅映射有撤回契约的来源（当前队列源无 withdraw
    // 契约则不渲染按钮；有契约时走既有 withdraw 能力，不新造协议）。
    "auth.withdraw": async (queueItem) => {
      if (!queueItem || typeof queueItem.source !== "string" || !queueItem.ref) {
        return { kind: "error", message: "撤回参数无效" };
      }
      const { source, ref } = queueItem;
      const key = authKey(source, ref);
      if (source === "graph") {
        // Graph 服务端无授权单撤回契约（签发后不可撤销）——诚实拒绝。
        return { kind: "error", message: "该授权已生效，Graph 不支持撤回（服务端契约）。" };
      }
      if (source === "loop-review") {
        // 审核服务无撤回契约——诚实拒绝。
        return { kind: "error", message: "该审核决定已生效，不支持撤回（服务端契约）。" };
      }
      if (source === "loop-control") {
        // 控制动作无撤回契约——诚实拒绝。
        return { kind: "error", message: "该控制动作已生效，不支持撤回（服务端契约）。" };
      }
      return { kind: "error", message: "未知授权来源" };
    },
    "graph.openRun": async (runId) => { await openGraphRun(runId); return { kind: "success", message: "" }; },
    "home.openToday": async () => {
      const commands = plugin.app && plugin.app.commands;
      if (!commands || typeof commands.executeCommandById !== "function") {
        return { kind: "error", message: "今日笔记命令在当前环境不可用" };
      }
      await commands.executeCommandById("daily-notes:open-today");
      return { kind: "success", message: "" };
    },
    "home.manageHidden": () => callPluginMethod("openContentManager"),
    "home.assess": () => callPluginMethod("openProjectAssessment"),
    /* 具名 home.search：把查询写进旧 DOM 显式 [data-home-search] 输入框并
       派发既有 input 事件——权威过滤逻辑只由模板已注册的 handler 执行。 */
    "home.search": async (query) => {
      const input = document.querySelector(".my-life-homepage-view .life-cosmos-legacy [data-home-search]");
      if (!input) return { kind: "error", message: "搜索入口不可用" };
      input.value = typeof query === "string" ? query : "";
      if (typeof input.dispatchEvent === "function" && typeof Event === "function") {
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      return { kind: "success", message: "" };
    },
    "focus.toggle": () => callPluginMethod("toggleReadingTimer"),
    "focus.reset": () => callPluginMethod("resetCurrentReadingTime"),
    "view.setFilter": (filter) => callPluginMethod("setViewPreference", "filter", filter),
    "view.setDensity": (density) => callPluginMethod("setViewPreference", "density", density),
    "item.openNote": async (path) => {
      /* F-K13（防御性校验，保守方案）：路径不存在时不调用 openLinkText，
         返回 error 由调用点反馈，避免静默或意外创建空笔记。 */
      const vault = plugin.app && plugin.app.vault;
      const exists = typeof vault?.getAbstractFileByPath === "function"
        ? Boolean(vault.getAbstractFileByPath(path)) : true;
      if (!exists) return { kind: "error", message: `未找到文档：${path}` };
      await openThought(path);
      return { kind: "success", message: "" };
    },
    "knowledge.renderMarkdown": (markdown, target, sourcePath) => renderKnowledgeMarkdown(plugin.app, markdown, target, sourcePath),
    "item.toggleRead": (path) => callPluginMethod("setContentState", path, "read"),
    "item.toggleWatch": (path) => callPluginMethod("setContentState", path, "watch"),
    "item.hide": (path) => callPluginMethod("setContentState", path, "hide"),
    "item.hideToday": (path) => callPluginMethod("setContentState", path, "hide_today"),
    "item.complete": async (path, line, expectedTitle) => {
      if (typeof plugin.completeTask !== "function") {
        return { kind: "error", message: "任务完成协议在当前环境不可用" };
      }
      const vault = plugin.app && plugin.app.vault;
      const file = vault && typeof vault.getAbstractFileByPath === "function"
        ? vault.getAbstractFileByPath(path) : null;
      if (!file || typeof vault.read !== "function") {
        return { kind: "error", message: "任务来源不可读，未确认完成" };
      }
      const readLine = async () => String(await vault.read(file)).split("\n")[Number(line)];
      const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
      // 写前只读校验：行存在、仍是未完成任务、文本身份与绑定标题一致；
      // 任一不满足直接 conflict，零 completeTask 调用（防止行漂移误写
      // 另一条未完成任务）。
      const before = await readLine();
      if (typeof before !== "string") {
        return { kind: "conflict", message: "任务行已漂移，未执行写入" };
      }
      if (!/^\s*[-*+]\s+\[ \]/.test(before)) {
        return { kind: "conflict", message: "任务已完成或行已漂移，未执行写入" };
      }
      const body = normalize(before.replace(/^\s*[-*+]\s+\[ \]\s*/, ""));
      const title = normalize(expectedTitle);
      if (!title || (!body.includes(title) && !title.includes(body))) {
        return { kind: "conflict", message: "任务身份与绑定不一致（来源已漂移），未执行写入" };
      }
      // 通过预检后仍只由唯一 completeTask 写入；写后再读确认才 success。
      await plugin.completeTask(path, line);
      const after = await readLine();
      if (typeof after === "string" && /^\s*[-*+]\s+\[[xX]\]/.test(after)) {
        return { kind: "success", message: "已完成" };
      }
      return { kind: "conflict", message: "任务状态没有变化（来源可能已漂移），未打勾" };
    },
    "intent.save": (item) => decideFragmentIntent(item, { action: "save_only", intents: ["save"], supplement: "" }, noopMessage),
    "intent.confirm": (item, intents, supplement) =>
      decideFragmentIntent(item, { action: "confirm", intents, supplement }, noopMessage),
    "intent.continue": (item, goal) => continueFragmentIntent(item, goal, noopMessage),
    "intent.escalate": (item) => escalateFragmentIntent(item, noopMessage),
    "graphProposal.create": (item) => createFragmentResearchRun(item, noopMessage),
    "pilot.openPlan": (candidate) => startPilotConvert(candidate),
    "pilot.create": (candidate) => confirmPilotCreate(candidate, noopMessage),
    "pilot.openRun": async (runId) => { await openGraphRun(runId); return { kind: "success", message: "" }; },
    "review.open": async (item) => { await openReview(item); return { kind: "success", message: "" }; },
    "fragment.explore": (path) => callPluginMethod("requestFragmentExploration", path),
    "fragment.experiment.start": (path) => callPluginMethod("startExperiment", path),
    "fragment.feedback.save": (path) => callPluginMethod("openExperimentFeedback", path),
  };
  // Cosmos 语义抽屉与 legacy 共用缓存；打开研究详情时复用可见期刷新，
  // 失败域原样暴露，不把过期状态当作当前结果。
  const cosmosSources = {
    graph: () => ({ state: graphCardState, lastGood: graphLastGood }),
    // R1 授权队列：各 client pending 状态派生（不做新状态存储，每次 fetch 重算）。
    loopReview: () => ({ state: reviewQueueState, lastGood: null }),
    loopControl: () => ({ state: controlQueueState, lastGood: null }),
    graphQueue: () => ({ state: graphQueueState, lastGood: null }),
    authQueueSyncAt: () => authQueueLastSyncAt,
    // R2 §4.7：已批准记录（撤回标的）——批准成功后登记；当前队列源无撤回
    // 契约 → 卡标「已生效，不可撤回」，不渲染撤回按钮。
    approvedAuth: () => ({ items: [...approvedAuthMap.values()].map((entry) => ({ ...entry, withdrawable: false })) }),
    intents: () => ({ items: intentState, error: intentLastError, available: intentAvailable, autoPropose: intentAutoPropose }),
    continuations: () => ({ items: continuationState, error: continuationLastError }),
    reviews: () => ({ items: productReviewCache || [], error: productReviewLastError, available: productReviewCache !== null && !productReviewLastError }),
    knowledge: () => ({ ...knowledgeState, notifications: knowledgeNotificationsState, periods: knowledgePeriodsState }),
    searchKnowledge: (query) => intentClient.searchKnowledge(query),
    readKnowledge: (id, revision) => intentClient.readKnowledge(id, revision),
    refreshResearch: () => refreshVisibleProjections(true),
    preferences: () => (typeof plugin.getViewPreferences === "function" ? plugin.getViewPreferences() : {}),
  };
  const cosmosRegistry = createCosmosRegistry();
  // View-only state survives Dataview roots within one homepage leaf, never saveData.
  const cosmosViewStates = new WeakMap();
  const cleanupDisconnectedCosmosSessions = cosmosRegistry.cleanupDisconnected;
  // ponytail: 复用主页已有时钟检查可见性，不增加常驻轮询器或 Graph 请求。
  // 多主页共享 client single-flight；隐藏时不请求，卸载后迟到结果不触碰视图。
  let projectionFetchedAt = Date.now();
  let projectionFetchPromise = null;
  const visibleSession = (session) => {
    const doc = session.home.ownerDocument;
    if (disposed || !doc || doc.hidden || doc.visibilityState === "hidden") return false;
    if (session.home.isConnected === false || session.mount.isConnected === false) return false;
    for (let node = session.home; node; node = node.parentElement || node.parentNode || node.parent) {
      if (node.hidden || node.getAttribute?.("hidden") != null
        || node.getAttribute?.("aria-hidden") === "true") return false;
    }
    if (typeof session.home.checkVisibility === "function") return session.home.checkVisibility({ visibilityProperty: true });
    return typeof session.home.getClientRects !== "function" || session.home.getClientRects().length > 0;
  };
  const projectionStamp = () => ({
    research: JSON.stringify(cosmosSources.intents()),
    knowledge: JSON.stringify(cosmosSources.knowledge()),
  });
  const applyVisibleProjections = () => {
    const stamp = projectionStamp();
    for (const session of cosmosRegistry.sessions.values()) {
      if (!visibleSession(session)) continue;
      const changed = {
        research: session.projectionStamp.research !== stamp.research,
        knowledge: session.projectionStamp.knowledge !== stamp.knowledge,
      };
      if (changed.research || changed.knowledge) session.refreshProjections(changed);
      session.projectionStamp = stamp;
    }
  };
  const refreshVisibleProjections = (force = false) => {
    if (![...cosmosRegistry.sessions.values()].some(visibleSession)) return Promise.resolve();
    if (force) applyVisibleProjections(); // 回到可见状态时先接上共享缓存。
    if (projectionFetchPromise) return projectionFetchPromise;
    if (!force && Date.now() - projectionFetchedAt < 30000) return Promise.resolve();
    const pending = Promise.all([fetchIntentAlignments(), fetchKnowledge(true)])
      .then(applyVisibleProjections)
      .catch(error => console.error("[My Life] 研究投影刷新失败", error))
      .finally(() => {
        projectionFetchedAt = Date.now();
        if (projectionFetchPromise === pending) projectionFetchPromise = null;
      });
    projectionFetchPromise = pending;
    return pending;
  };
  const resumeVisibleProjections = () => { void refreshVisibleProjections(true); };
  document.addEventListener?.("visibilitychange", resumeVisibleProjections);
  window.addEventListener?.("focus", resumeVisibleProjections);

  plugin.mountHomepageCosmos = (root) => {
    const run = (async () => {
      let session = null;
      try {
        session = prepareHomepageCosmos(document, root);
      } catch (error) {
        // 结构校验失败：诚实降级——尽最大可能展开 legacy；mount 缺失时在
        // root 内、legacy 之前创建最小错误面（DOM API + 文本节点）。不得
        // 留下空白页，也不得把结构失败伪装成成功。此路径尚未注册
        // generation，无状态残留。
        console.error("[My Life] Cosmos 挂载结构校验失败", error);
        degradeHomepageCosmos(document, root, error);
        return "failed";
      }
      const generation = cosmosRegistry.beginMount(session.home);
      if (!cosmosViewStates.has(session.home)) cosmosViewStates.set(session.home, { knowledgeDisclosures: new Map(), knowledge: null });
      try {
        // 先让现有产品卡写入 legacy（缓存态），再从真实投影构建 Cosmos model。
        renderHomepageCards();
        ensureInitialFetches();
        let map = null;
        if (typeof plugin.getThoughtMap === "function") {
          try {
            map = await fetchThoughtMap();
          } catch (error) {
            // 思考地图收集失败不拖垮主页：Cosmos 允许空投影落地。
            console.error("[My Life] 无法生成思考地图", error);
          }
        }
        await Promise.all([fetchProductReviews(), fetchKnowledge()]);
        await Promise.allSettled(
          [graphFetchPromise, intentFetchPromise, continuationFetchPromise, authQueueFetchPromise].filter(Boolean)
        );
        // generation／connected 守卫：Dataview 新一轮渲染、root/mount 已断开、
        // 插件 unload 之后，上一轮迟到结果一律零落地。
        if (disposed) return "stale"; // unload 已由 clear() 统一清空，语义不变
        if (!cosmosRegistry.isCurrent(session.home, generation)) return "stale"; // 旧轮：不碰新轮状态
        if (root.isConnected === false || session.mount.isConnected === false) {
          // 最新一轮在投影落地前断开：释放自身 generation，不留 home 引用。
          cosmosRegistry.releaseGeneration(session.home, generation);
          return "stale";
        }
        renderHomepageCards();
        const activity = collectHomepageActivity(plugin.app);
        const cosmos = finishHomepageCosmos(document, session, map, {
          openNote: openThought,
          activity,
          capabilities,
          sources: cosmosSources,
          viewState: cosmosViewStates.get(session.home),
          onProjectionTick: () => { void refreshVisibleProjections(); },
        });
        cosmosRegistry.recordSession(session.home, {
          home: session.home,
          root: session.root,
          mount: session.mount,
          generation,
          projectionStamp: projectionStamp(),
          refreshProjections: (changed) => cosmos.__lifeCosmosRefreshProjections?.(changed),
          dispose: () => cosmos.__lifeCosmosDispose?.(),
        });
        return "mounted";
      } catch (error) {
        console.error("[My Life] Cosmos 挂载失败", error);
        const current = !disposed && cosmosRegistry.isCurrent(session.home, generation);
        try {
          if (current) {
            // 诚实错误态：显示原因并展开完整旧版操作面，绝不空白页。
            failHomepageCosmos(document, session, error);
          }
        } catch (nested) {
          console.error("[My Life] Cosmos 错误态渲染失败", nested);
        }
        // 当前轮完成诚实降级后释放自身 generation；旧轮不碰新轮状态。
        if (current) cosmosRegistry.releaseGeneration(session.home, generation);
        return current ? "failed" : "stale";
      }
    })();
    // 握手绝不向 Dataview 模板抛出未处理拒绝。
    run.catch((error) => console.error("[My Life] Cosmos 挂载异常", error));
    return run;
  };
  const scanHomepage = async () => {
    if (disposed) return;
    injectHomepageEntry(document, openConsole);
    injectHomepageExperience(document);
    await fetchProductReviews();
    // Graph 主页摘要卡：独立失败域（5684 不可达只影响这张卡）；摘要只
    // 来自 5684 只读 runs 列表，首载主动取数，重扫只复用缓存重新注入。
    ensureInitialFetches();
    renderHomepageCards();
    if (typeof plugin.getThoughtMap !== "function") return;
    const containers = [...document.querySelectorAll(".my-life-homepage-view .life-dashboard-content")];
    // Cosmos 不参与扫描：带 [data-life-cosmos-mount] 的新模板只走显式
    // 握手。这里只维护 legacy 内思考地图的完成度。
    const pending = containers.filter((el) => {
      const section = el.querySelector(".life-thought-map");
      return !section || Boolean(section.querySelector(".life-empty-state"));
    });
    if (!pending.length) {
      retryAttempts = 0;
      return;
    }
    let map = null;
    try {
      map = await fetchThoughtMap();
    } catch (error) {
      console.error("[My Life] 无法生成思考地图", error);
    }
    if (map && map.total > 0) {
      // 被替换的只可能是空状态占位 section；有内容的 section 不会进入 pending。
      for (const el of pending) el.querySelector(".life-thought-map")?.remove();
      injectHomepageThoughtMap(document, map, openThought);
      retryAttempts = 0;
      return;
    }
    if (map && pending.every((el) => el.querySelector(".life-thought-map"))) {
      // 空状态已落地且收集仍为空：主页处于稳定的空状态，不再重试。
      retryAttempts = 0;
      return;
    }
    // 收集失败或结果为空：在有限启动恢复窗口内重试；空结果先不注入。
    retryAttempts += 1;
    if (retryAttempts >= SCAN_RETRY_MAX_ATTEMPTS) {
      retryAttempts = 0;
      // 窗口耗尽：空结果落地为空状态后停止——绝不常驻轮询。
      if (map) {
        injectHomepageThoughtMap(document, map, openThought);
      }
      return;
    }
    scheduleRetry(SCAN_RETRY_BASE_MS * 2 ** (retryAttempts - 1));
  };
  const runScan = async () => {
    scanTimer = null;
    await scanHomepage();
  };
  // 外部事件（MutationObserver、layout-change）一律走固定防抖，事件参数
  // 绝不进入定时器延迟；重试延迟只通过独立的 scheduleRetry 入口进入。
  const scheduleScan = () => {
    if (scanTimer !== null) window.clearTimeout(scanTimer);
    scanTimer = window.setTimeout(runScan, SCAN_DEBOUNCE_MS);
  };
  const scheduleRetry = (delay) => {
    if (scanTimer !== null) window.clearTimeout(scanTimer);
    scanTimer = window.setTimeout(runScan, delay);
  };

  // ownership filter 的归属清单：Cosmos 挂载点与 home 根（覆盖 clock/
  // countdown/drawer/board 动画）、Graph Canvas、思考地图。
  const OWNED_CHURN_SELECTORS = [
    "[data-life-cosmos-mount]",
    ".life-cosmos-home",
    ".graph-canvas-host",
    ".life-thought-map",
  ];
  const ownedNode = (node) => {
    if (!node || typeof node.closest !== "function") return false;
    return OWNED_CHURN_SELECTORS.some((selector) => node.closest(selector));
  };
  // 自有 churn：target 在自有区域内，或全部 added/removed nodes 都在。
  const isOwnedChurn = (record) => {
    if (record.target && ownedNode(record.target)) return true;
    const changed = [...(record.addedNodes || []), ...(record.removedNodes || [])];
    return changed.length > 0 && changed.every((node) => ownedNode(node));
  };

  const observer = new MutationObserver((records) => {
    // 先清断开 session（与过滤独立，绝不注入 Cosmos）；自有区域 churn
    // （时钟 tick、倒计时、canvas/drawer/board、思考地图交互）绝不重扫，
    // 外部 legacy 变化仍走固定防抖。
    cleanupDisconnectedCosmosSessions();
    if (!records.length) return;
    if (records.every((record) => isOwnedChurn(record))) return;
    scheduleScan();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  plugin.register(() => {
    disposed = true;
    document.removeEventListener?.("visibilitychange", resumeVisibleProjections);
    window.removeEventListener?.("focus", resumeVisibleProjections);
    observer.disconnect();
    closePilotDialog(document);
    cosmosRegistry.clear();
    disposeHomepageCosmos(document);
    delete plugin.mountHomepageCosmos;
    if (scanTimer !== null) window.clearTimeout(scanTimer);
  });
  plugin.registerEvent(plugin.app.workspace.on("layout-change", () => { scheduleScan(); void refreshVisibleProjections(); }));
  plugin.registerEvent(plugin.app.workspace.on("active-leaf-change", resumeVisibleProjections));
  plugin.app.workspace.onLayoutReady(scanHomepage);
  // 插件重载时布局早已就绪、onLayoutReady 可能不再触发、DOM 也可能完全静态；
  // 启动即扫描一次，不依赖任何后续事件。
  scanHomepage();
}

// n8n-repair 部件 3：写修复请求文件，看门狗下轮消费执行（零新通道）。
// ⚠️ 默认路径即生产消费目录——隔离实例/测试必须注入 repairRequestDir。
// R26 P1-2：提升为注入 plugin 的工厂并导出——capability 边界可直接契约测试
// （非法 target/写失败/成功三态），UI 入口不变（仍固定 target n8n）。
const createRepairRequestCapability = (plugin) => async (payload) => {
  if (!payload || payload.target !== "n8n") {
    return { kind: "error", message: "仅支持 n8n 一键修复" };
  }
  try {
    const prefs = typeof plugin.getViewPreferences === "function" ? plugin.getViewPreferences() : {};
    const dir = prefs.repairRequestDir || path.join(os.homedir(), ".local", "share", "loop-graph-cosmos", "repair-requests");
    fs.mkdirSync(dir, { recursive: true });
    const body = {
      kind: "repair_request",
      target: payload.target,
      action: "restart",
      created_at: new Date().toISOString(),
    };
    fs.writeFileSync(path.join(dir, `repair-${Date.now()}.json`), JSON.stringify(body, null, 2), "utf8");
    return { kind: "success", message: "修复请求已提交，看门狗下轮执行（约 15 分钟内）；结果会体现在此列表" };
  } catch {
    return { kind: "error", message: "修复请求写入失败，请稍后重试" };
  }
};

module.exports = {
  renderKnowledgeMarkdown,
  LOOP_CONSOLE_VIEW_TYPE,
  GRAPH_WORKFLOW_VIEW_TYPE,
  obsidianTransport,
  activateLoopConsole,
  activateGraphWorkflow,
  createCosmosRegistry,
  createRepairRequestCapability,
  setupLoopConsole,
};
