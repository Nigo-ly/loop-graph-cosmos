// Single-ownership mount handshake tests for the Cosmos homepage.
//
// Contract under test: the My Life.md Dataview template owns the homepage
// structure — one [data-life-cosmos-mount] plus one collapsed
// .life-cosmos-legacy containing the search bar, theme entry and the whole
// old dashboard. The plugin only writes inside the mount, exclusively via
// the explicit plugin.mountHomepageCosmos(root) handshake at the end of the
// template. The MutationObserver scan never injects, restores or moves
// Cosmos. Late async results from a superseded render round, a detached
// root, or an unloaded plugin must never land.
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, promises as fsPromises, mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { effectiveScope } from './helpers/effective-scope.mjs';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

// Stub the obsidian module so register.js can load under plain Node.
let requestUrlHandler = async () => { throw new Error('requestUrl handler not installed'); };
let markdownRenderHandler = null;
let markdownComponents = [];
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') {
    return {
      requestUrl: (options) => requestUrlHandler(options),
      MarkdownRenderer: { render: (...args) => markdownRenderHandler
        ? markdownRenderHandler(...args) : Promise.reject(new Error('native renderer not present in Node')) },
      Component: class {
        constructor() { this.unloads = 0; markdownComponents.push(this); }
        load() { this.loaded = true; }
        unload() { this.unloads += 1; }
      },
      ItemView: class {
        constructor() {
          this.contentEl = new FakeEl('div');
          this.app = {};
        }
      },
    };
  }
  return originalLoad.call(this, request, ...rest);
};
const { setupLoopConsole, createCosmosRegistry } = require(path.join(ROOT, 'src', 'console', 'register.js'));
const cosmos = require(path.join(ROOT, 'src', 'console', 'homepage-cosmos.js'));
const thoughtMap = require(path.join(ROOT, 'src', 'console', 'thought-map.js'));
Module._load = originalLoad;

// P0-1（R25）：隔离即保护——所有 harness 默认注入临时消费目录，测试代码
// 不再触达真实看门狗目录。2026-08-27 起按 nigo 指令：测试套件连真实目录的
// 读取（快照/比对）也不做；证据 = 既有污染记录 + 注入后写入只落临时目录。
// 每个 makePlugin 实例默认隔离到独立的临时消费目录；显式 viewPreferences 仍可覆盖。
function isolatedConsumerDirs() {
  return {
    authNotifyDir: mkdtempSync(path.join(os.tmpdir(), 'p0-auth-notify-')),
    repairRequestDir: mkdtempSync(path.join(os.tmpdir(), 'p0-repair-req-')),
  };
}

// Layout/canvas doubles: enabling getBoundingClientRect flips the Cosmos
// renderers into their "real layout" path so intervals, timeouts, rAF and
// pointer listeners are actually registered and can be audited for cleanup.
// node --test isolates each file in its own process, so the prototype patch
// cannot leak into other suites.
FakeEl.prototype.getBoundingClientRect = function () {
  return { width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600 };
};
const absorbingContext = new Proxy(function () {}, {
  get: (target, prop) => (prop === Symbol.toPrimitive ? () => 0 : absorbingContext),
  apply: () => absorbingContext,
  set: () => true,
});
FakeEl.prototype.getContext = function () { return absorbingContext; };

function sampleMap() {
  return thoughtMap.buildThoughtMap([
    {
      path: 'note.md',
      sourceKey: 'note',
      title: '未来 AI 公司业务架构设想',
      category: 'AI 公司与行业化',
      summary: '形成可复用的行业进入与交付底座。',
      unknowns: [],
      status: 'provisional',
      updatedAt: '2026-07-30T08:00:00Z',
      rank: 3,
    },
  ]);
}

function envelope(data) {
  return { status: 200, text: JSON.stringify({ contract_version: '2', data }) };
}

function loopEnvelope(items) {
  return {
    status: 200,
    text: JSON.stringify({
      contract_version: '2', db_mode: 'read_only', generated_at: '2026-09-02T00:00:00+00:00',
      provider_version: 'test', source_sequence: 1, source_committed_at: '2026-09-02T00:00:00+00:00',
      stale: false, items,
    }),
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// Timer/rAF bookkeeping window: every scheduled handle is recorded, every
// clear/cancel is counted, so each test can prove exactly one live session.
function makeWindow() {
  const state = { timeouts: [], intervals: [], rafs: [] };
  const win = {
    setTimeout: (fn, ms) => {
      const timer = { fn, ms, cleared: false, fired: false };
      state.timeouts.push(timer);
      return timer;
    },
    clearTimeout: (timer) => { if (timer && !timer.cleared) timer.cleared = true; },
    setInterval: (fn, ms) => {
      const timer = { fn, ms, cleared: false };
      state.intervals.push(timer);
      return timer;
    },
    clearInterval: (timer) => { if (timer && !timer.cleared) timer.cleared = true; },
    requestAnimationFrame: (fn) => {
      const frame = { fn, cancelled: false };
      state.rafs.push(frame);
      return frame;
    },
    cancelAnimationFrame: (frame) => { if (frame && !frame.cancelled) frame.cancelled = true; },
  };
  return {
    state,
    window: win,
    liveIntervals: () => state.intervals.filter((t) => !t.cleared).length,
    liveTimeouts: () => state.timeouts.filter((t) => !t.cleared && !t.fired).length,
    liveRafs: () => state.rafs.filter((f) => !f.cancelled).length,
    pending() {
      return state.timeouts.filter((t) => !t.cleared && !t.fired);
    },
    async pump() {
      const timer = this.pending()[0];
      assert.ok(timer, 'expected a scheduled timer');
      timer.fired = true;
      await timer.fn();
      return timer.ms;
    },
  };
}

class FakeMutationObserver {
  static instances = [];
  constructor(callback) {
    this.callback = callback;
    this.disconnected = false;
    FakeMutationObserver.instances.push(this);
  }
  observe() {}
  disconnect() {
    this.disconnected = true;
  }
  trigger() {
    this.callback([{ type: 'childList', addedNodes: [], removedNodes: [] }], this);
  }
}

function makePlugin({ getThoughtMap, missingFiles, viewPreferences } = {}) {
  const cleanupCallbacks = [];
  const eventHandlers = {};
  const openedLinks = [];
  const openLinkCalls = [];
  const pluginCalls = [];
  const executedCommands = [];
  const plugin = {
    registerView() {},
    addCommand() {},
    register(fn) {
      cleanupCallbacks.push(fn);
    },
    registerEvent() {},
    captureHomeScroll() {},
    // V1 capability adapter 的既有 handler 探针：每个方法只记录调用，不做事。
    openQuickCapture: async () => { pluginCalls.push(['openQuickCapture']); },
    openContentManager: async () => { pluginCalls.push(['openContentManager']); },
    openProjectAssessment: async () => { pluginCalls.push(['openProjectAssessment']); },
    toggleReadingTimer: async () => { pluginCalls.push(['toggleReadingTimer']); },
    resetCurrentReadingTime: async () => { pluginCalls.push(['resetCurrentReadingTime']); },
    setViewPreference: async (key, value) => { pluginCalls.push(['setViewPreference', key, value]); },
    setContentState: async (path, action) => { pluginCalls.push(['setContentState', path, action]); },
    completeTask: async (path, line) => { pluginCalls.push(['completeTask', path, line]); },
    requestFragmentExploration: async (path) => { pluginCalls.push(['requestFragmentExploration', path]); },
    startExperiment: async (path) => { pluginCalls.push(['startExperiment', path]); },
    openExperimentFeedback: async (path) => { pluginCalls.push(['openExperimentFeedback', path]); },
    getViewPreferences: () => ({ filter: 'all', density: 'comfortable', ...isolatedConsumerDirs(), ...(viewPreferences || {}) }),
    app: {
      commands: {
        executeCommandById: async (id) => { executedCommands.push(id); },
      },
      workspace: {
        on: (event, fn) => {
          eventHandlers[event] = fn;
          return {};
        },
        onLayoutReady: () => {},
        getLeavesOfType: () => [],
        openLinkText: async (...args) => { openedLinks.push(args[0]); openLinkCalls.push(args); },
      },
      vault: {
        getAbstractFileByPath: (ref) => (ref && missingFiles && missingFiles.includes(ref) ? null : (ref ? { extension: 'md', path: ref } : null)),
        read: async () => '原始内容',
      },
      metadataCache: {
        getFileCache: () => ({ frontmatter: { 'nigo-loop': true } }),
      },
    },
  };
  if (getThoughtMap) plugin.getThoughtMap = getThoughtMap;
  return { plugin, cleanupCallbacks, eventHandlers, openedLinks, openLinkCalls, pluginCalls, executedCommands };
}

// The new-template homepage structure, owned by Dataview: mount first,
// then the collapsed legacy containing the search bar and the whole old
// dashboard. Mirrors what the patched My Life.md emits.
function buildHomepageRoot(home) {
  const root = home.createDiv({ cls: 'life-home' });
  const mount = root.createDiv({ cls: 'life-cosmos-mount' });
  mount.setAttribute('data-life-cosmos-mount', '1');
  const legacy = root.createDiv({ cls: 'life-cosmos-legacy' });
  const bar = legacy.createEl('section', { cls: 'life-command-bar' });
  bar.createDiv({ cls: 'life-command-search' });
  bar.createDiv({ cls: 'life-command-actions' });
  const dashboard = legacy.createDiv({ cls: 'life-dashboard-content' });
  dashboard.createEl('section', { cls: 'life-hero' });
  const stats = dashboard.createEl('section', { cls: 'life-stat-strip' });
  for (const [label, value] of [['待处理', '2'], ['今日产出', '5'], ['知识规模', '128']]) {
    const stat = stats.createDiv({ cls: 'life-stat' });
    stat.createSpan({ text: label });
    stat.createEl('strong', { text: value });
  }
  const focus = dashboard.createEl('section', { cls: 'life-focus-bar' });
  focus.createEl('strong', { text: '整理研究结论' });
  focus.createDiv({ cls: 'life-focus-time', text: '24:51' });
  const decisionBar = dashboard.createEl('section', { cls: 'life-decision-bar' });
  decisionBar.createDiv({ cls: 'life-decision-match' });
  const todos = dashboard.createEl('section', { cls: 'life-panel life-todos' });
  todos.createEl('ul').createEl('li').createEl('strong', { text: '确认研究方向' });
  /* W6 fixture：默认无故障区；测试按需追加 .life-faults */
  const fragments = dashboard.createEl('section', { cls: 'life-panel life-fragments' });
  /* 精炼管线计数（车队动画数据源）：采集带 1 / 精炼站 0 / 试航场 0 / 恒星铸造 0 */
  const pipeline = fragments.createDiv({ cls: 'life-pipeline-status' });
  for (const label of ['采集带', '精炼站', '试航场', '恒星铸造']) {
    const span = pipeline.createEl('span');
    span.createEl('b', { text: label === '采集带' ? '1' : '0' });
  }
  const card = fragments.createEl('article', { cls: 'life-capture-card' });
  card.createEl('strong', { text: '碎片甲' });
  card.createEl('p', { text: '真实碎片投影' });
  const footer = card.createEl('footer');
  const rawLink = footer.createEl('a', { text: '原始记录' });
  rawLink.setAttribute('data-path', 'Notes/散记/碎片想法/frag-1.md');
  const orgLink = footer.createEl('a', { text: '查看整理结果' });
  orgLink.setAttribute('data-path', 'Notes/散记/已整理碎片/org-1.md');
  const intent = fragments.createDiv({ cls: 'fragment-intent-card' });
  intent.createEl('strong', { text: 'MiniMax H3 是否继续核验' });
  intent.createEl('p', { cls: 'fragment-intent-reason', text: '证据仍不足' });
  intent.createEl('button', { cls: 'fragment-intent-confirm', text: '确认并继续' });
  return { root, mount, legacy, dashboard, intentCard: intent };
}

function makeDocument() {
  const doc = new FakeEl('document');
  doc.body = doc.createDiv({ cls: 'body' });
  return doc;
}

function makeHome(doc) {
  const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
  return { home, ...buildHomepageRoot(home) };
}

async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

function cosmosCount(doc) {
  return doc.querySelectorAll('.life-cosmos-home').length;
}

describe('Cosmos single-ownership mount handshake', () => {
  let win;
  let savedGlobals;

  beforeEach(() => {
    FakeMutationObserver.instances = [];
    win = makeWindow();
    savedGlobals = {
      document: globalThis.document,
      window: globalThis.window,
      MutationObserver: globalThis.MutationObserver,
    };
    globalThis.window = win.window;
    globalThis.MutationObserver = FakeMutationObserver;
    requestUrlHandler = async () => envelope([]);
    markdownRenderHandler = null;
    markdownComponents = [];
  });

  afterEach(() => {
    globalThis.document = savedGlobals.document;
    globalThis.window = savedGlobals.window;
    globalThis.MutationObserver = savedGlobals.MutationObserver;
  });

  it('反例1: the old structure really raced — Cosmos injected into the dashboard is deleted by a late Dataview innerHTML', () => {
    // 旧所有权模式最小重演（已删除的 injectHomepageCosmos 语义）：插件把
    // Cosmos 直接插进 .life-dashboard-content，并把既有节点扫进 legacy。
    // 这证明竞态真实存在于旧结构，而不是测试幻觉。
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const dashboard = home.createDiv({ cls: 'life-dashboard-content' });
    dashboard.createDiv({ cls: 'life-fragments' });

    const oldCosmos = dashboard.createEl('section', { cls: 'life-cosmos-home' });
    const sweptLegacy = dashboard.createDiv({ cls: 'life-cosmos-legacy' });
    for (const child of [...dashboard.children]) {
      if (child === sweptLegacy || child === oldCosmos) continue;
      child.remove();
      sweptLegacy.appendChild(child);
    }
    assert.equal(dashboard.querySelectorAll('.life-cosmos-home').length, 1, 'old path injected Cosmos into the dashboard');

    // Dataview 迟到重渲染：root.innerHTML 重建 = 旧 children 整体丢弃。
    dashboard.children = [];
    dashboard.createDiv({ cls: 'life-fragments' });
    assert.equal(dashboard.querySelectorAll('.life-cosmos-home').length, 0, 'late Dataview re-render deletes the injected Cosmos');
    assert.equal(dashboard.querySelectorAll('.life-cosmos-legacy').length, 0, 'the swept legacy container dies with it');
  });

  it('反例2: five consecutive Dataview re-renders each settle into exactly one mount, one Cosmos and one legacy', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    let current = null;
    for (let round = 1; round <= 5; round += 1) {
      // Dataview 新一轮渲染：旧根断开，新根接管。
      if (current) current.root.remove();
      current = buildHomepageRoot(home);
      const status = await harness.plugin.mountHomepageCosmos(current.root);
      await settle();
      assert.equal(status, 'mounted', `round ${round}: handshake reports an honest mount`);
      assert.equal(doc.querySelectorAll('[data-life-cosmos-mount]').length, 1, `round ${round}: exactly one mount`);
      assert.equal(cosmosCount(doc), 1, `round ${round}: exactly one Cosmos`);
      assert.equal(doc.querySelectorAll('.life-cosmos-legacy').length, 1, `round ${round}: exactly one legacy`);
      assert.equal(current.mount.querySelectorAll('.life-cosmos-home').length, 1, `round ${round}: Cosmos lives in the current mount`);
    }
    // 未在计时时只保留真实墙钟 interval；两个专注面都不得偷偷倒计时。
    assert.equal(win.liveIntervals(), 1, 'only the wall clock runs while focus is stopped');
    assert.equal(win.liveRafs(), 0, 'only the final atlas keeps a live animation frame');
  });

  it('反例3: when round one resolves after round two took over, round one lands nothing', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const gate = deferred();
    const harness = makePlugin({ getThoughtMap: () => gate.promise });
    setupLoopConsole(harness.plugin);
    await settle();

    // 第一轮：挂载后停在共享 in-flight 投影上。
    const first = buildHomepageRoot(home);
    const runOne = harness.plugin.mountHomepageCosmos(first.root);
    await settle();
    assert.equal(first.mount.querySelectorAll('.life-cosmos-loading').length, 1, 'round one shows an honest loading state');

    // P3 骨架结构：loading 内有同形占位（hero/band/grid 三区，grid 含 4 格）
    const sk = first.mount.querySelector('.life-cosmos-loading .life-cosmos-skeleton');
    assert.ok(sk, '骨架屏容器存在');
    assert.ok(sk.querySelector('.sk-hero') && sk.querySelector('.sk-band'), '骨架含 hero/band 区');
    assert.equal(sk.querySelectorAll('.sk-grid i').length, 4, '骨架 grid 为 4 格');

    // Dataview 第二轮渲染：旧根断开，新根接管（同一共享投影仍在飞行）。
    first.root.remove();
    const second = buildHomepageRoot(home);
    const runTwo = harness.plugin.mountHomepageCosmos(second.root);
    await settle();

    // 迟到的第一轮结果随共享投影一起返回：第一轮零落地，第二轮落地。
    gate.resolve(sampleMap());
    const [statusOne, statusTwo] = await Promise.all([runOne, runTwo]);
    await settle();
    assert.equal(statusOne, 'stale', 'round one honestly reports a stale landing');
    assert.equal(statusTwo, 'mounted', 'round two honestly reports its mount');
    assert.equal(first.mount.querySelectorAll('.life-cosmos-home').length, 0, 'round one lands nothing on the detached root');
    assert.equal(second.mount.querySelectorAll('.life-cosmos-home').length, 1, 'round two owns the page');
    assert.equal(cosmosCount(doc), 1, 'exactly one Cosmos after the late response');
  });

  it('反例4: a late result after the mount disconnected lands nothing', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const gate = deferred();
    const harness = makePlugin({ getThoughtMap: () => gate.promise });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    const run = harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    // Dataview 整体重渲染把 root 摘掉，且这一轮不再回来（用户切走）。
    current.root.remove();
    assert.equal(current.root.isConnected, false, 'root is detached before the response arrives');

    gate.resolve(sampleMap());
    const status = await run;
    await settle();
    assert.equal(status, 'stale', 'late result on a detached mount is honestly stale');
    assert.equal(current.mount.querySelectorAll('.life-cosmos-home').length, 0, 'no Cosmos lands on a detached mount');
    assert.equal(cosmosCount(doc), 0, 'nothing leaks into the document');
  });

  it('反例5: a late result after plugin unload lands nothing, and the handshake is removed', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const gate = deferred();
    const harness = makePlugin({ getThoughtMap: () => gate.promise });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    const run = harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(typeof harness.plugin.mountHomepageCosmos, 'function', 'handshake installed during load');

    for (const cleanup of harness.cleanupCallbacks) cleanup();
    assert.equal(harness.plugin.mountHomepageCosmos, undefined, 'unload removes the public handshake');
    gate.resolve(sampleMap());
    const status = await run;
    await settle();
    assert.equal(status, 'stale', 'post-unload result is honestly stale');
    assert.equal(cosmosCount(doc), 0, 'post-unload response lands nothing');
  });

  it('反例6: repeated mount calls in the same round keep exactly one Cosmos and clock without decorative resources', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    const runOne = harness.plugin.mountHomepageCosmos(current.root);
    const runTwo = harness.plugin.mountHomepageCosmos(current.root);
    await Promise.all([runOne, runTwo]);
    await settle();

    assert.equal(current.mount.querySelectorAll('.life-cosmos-home').length, 1, 'one Cosmos');
    assert.equal(current.mount.querySelectorAll('canvas').length, 0, 'no duplicate decorative canvas');
    assert.equal(current.mount.querySelectorAll('.life-cosmos-clock').length, 1, 'one clock');
    assert.equal(win.liveIntervals(), 1, 'one wall-clock interval, not hidden focus timers');
    assert.equal((home.listeners.pointermove || []).length, 0, 'one pointer-glow listener');
  });

  it('反例7: with the explicit mount present, MutationObserver scans never run the old injection path', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const mounted = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(mounted.root);
    await settle();
    assert.equal(cosmosCount(doc), 1);

    // 第二个主页副本带有 mount 但从未显式握手：扫描绝不得替它注入 Cosmos，
    // 也绝不得搬移它 legacy 里的任何节点。
    const homeTwo = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const unmounted = buildHomepageRoot(homeTwo);
    for (let i = 0; i < 3; i += 1) {
      FakeMutationObserver.instances[0].trigger();
      await win.pump();
      await settle();
    }
    assert.equal(cosmosCount(doc), 1, 'scans create no Cosmos');
    assert.equal(unmounted.mount.children.length, 0, 'unmounted mount stays empty');
    assert.ok(unmounted.legacy.children.includes(unmounted.dashboard), 'legacy children are never moved by the scan');
    assert.ok(mounted.legacy.children.includes(mounted.dashboard), 'mounted legacy keeps its Dataview-owned children');
  });

  it('反例7b: the old sweep/recovery path is gone from the plugin source (strict mutual exclusion)', () => {
    const registerSource = readFileSync(path.join(ROOT, 'src', 'console', 'register.js'), 'utf8');
    const cosmosSource = readFileSync(path.join(ROOT, 'src', 'console', 'homepage-cosmos.js'), 'utf8');
    for (const banned of ['injectHomepageCosmos', 'sweepLegacy', 'restoreLegacy']) {
      assert.ok(!registerSource.includes(banned), `register.js must not reference ${banned}`);
      assert.ok(!cosmosSource.includes(banned), `homepage-cosmos.js must not reference ${banned}`);
    }
  });

  it('反例8: a failing Thought Map collection still mounts Cosmos and never falls back to the old homepage', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({
      getThoughtMap: async () => { throw new Error('provider down'); },
    });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();

    assert.equal(cosmosCount(doc), 1, 'Cosmos mounts on an empty projection');
    assert.equal(doc.querySelectorAll('.life-cosmos-error').length, 0, 'no error surface for a recoverable projection failure');
    assert.equal(current.legacy.classes.has('is-open'), false, 'legacy stays collapsed — no fallback flash to the old homepage');
    assert.equal(current.mount.querySelectorAll('.life-workbench').length, 1, 'workbench remains useful without a thought-map projection');
  });

  it('反例9: the search bar, hero and stat strip live inside the collapsed legacy by default', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();

    assert.equal(current.root.children[0], current.mount, 'mount is the first region');
    assert.equal(current.root.children[1], current.legacy, 'legacy is the second region');
    assert.equal(current.legacy.classes.has('is-open'), false, 'legacy is collapsed by default (display:none in CSS)');
    for (const selector of ['.life-command-bar', '.life-hero', '.life-stat-strip']) {
      const el = current.root.querySelector(selector);
      assert.ok(el, `${selector} exists`);
      assert.equal(el.closest('.life-cosmos-legacy'), current.legacy, `${selector} is owned by the legacy surface`);
    }
    assert.equal(current.mount.closest('.life-cosmos-legacy'), null, 'mount is not inside legacy');
    assert.equal(current.mount.querySelector('.life-command-bar'), null, 'no search bar floats above Cosmos');
  });

  it('反例10: expanding the deep workbench keeps Loop, Graph, intent and research cards operable', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    let graphCalls = 0;
    requestUrlHandler = async (options) => {
      if (String(options.url).includes('/graph/v1/runs')) graphCalls += 1;
      return envelope([]);
    };
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();

    // Graph 摘要卡：真实按钮与监听器都在 legacy 内。
    const graphCard = current.legacy.querySelector('.life-graph-home');
    assert.ok(graphCard, 'Graph card is injected into legacy');
    const refresh = graphCard.querySelector('.life-graph-home-refresh');
    assert.ok(refresh.listeners.click.length >= 1, 'Graph refresh button is wired');
    const before = graphCalls;
    refresh.click();
    await settle();
    assert.equal(graphCalls, before + 1, 'Graph refresh still fetches');

    // 意图卡：注入在 legacy 的碎片卡上，确认按钮可点。
    const intentSlot = current.legacy.querySelector('[data-fragment-intent-card]');
    assert.ok(intentSlot, 'intent card is injected into legacy');
    const start = intentSlot.querySelector('.fragment-intent-start');
    assert.ok(start.listeners.click.length >= 1, 'intent start button is wired');
    start.click();
    await settle();

    // 「去拍板 →」打开 Cosmos 原生详情，不再把用户扔回旧版底部。
    const go = current.mount.querySelector('.is-primary');
    assert.ok(go, 'decision deck offers a 去拍板 action');
    go.click();
    assert.equal(current.legacy.classes.has('is-open'), false, '深度工作台保持折叠');
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-open'), true, '原生详情抽屉打开');
  });

  it('日常工作台只有三个主入口，不再重复呈现思考星图', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    const current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.deepEqual(current.mount.querySelectorAll('.life-cosmos-nav button').map(item => item.textContent), ['工作台', '研究记录', '知识库']);
    assert.equal(current.mount.querySelector('canvas'), null);
    assert.ok(current.mount.querySelector('.life-workbench'));
    assert.ok(current.legacy.querySelector('.life-dashboard-content'), '原始操作面仍保留');
  });

  it('产品反例: 专注停止时不倒计时，权威状态启动后才登记两个专注 interval', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    const current = buildHomepageRoot(home);
    current.dashboard.querySelector('.life-focus-bar').createEl('small', { text: '正在记录' });
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(win.liveIntervals(), 3, 'wall clock plus overview and orbit countdowns');
  });

  it('产品反例: 星历按真实文件活动点亮，点击当天打开原生清单而非旧主页', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    const today = new Date();
    harness.plugin.app.vault.getMarkdownFiles = () => [{
      path: '散记/碎片想法/today.md', basename: '今天的碎片', stat: { ctime: today.getTime() },
    }];
    setupLoopConsole(harness.plugin);
    await settle();
    const current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    const day = [...current.mount.querySelectorAll('.life-cb-day')].find((cell) => cell.textContent === String(today.getDate()));
    assert.ok(day);
    assert.equal(day.getAttribute('data-count'), '1');
    day.click();
    assert.equal(current.legacy.classes.has('is-open'), false);
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-open'), true);
    const activityRow = current.mount.querySelector('.life-cosmos-day-item');
    assert.ok(activityRow);
    assert.equal(activityRow.querySelector('b').textContent, '今天的碎片');
    activityRow.click();
    await settle();
    assert.equal(harness.openLinkCalls.at(-1)[2], true, '日历记录在独立标签打开');
  });

  it('产品反例: 未选当前专注时提供可操作的选择入口而非禁用启动', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    const current = buildHomepageRoot(home);
    current.dashboard.querySelector('.life-todos').empty();
    // 生产同形：专注栏标题为空 = 没有当前专注；My Life 本身是真实焦点（见 V1 反例）。
    current.dashboard.querySelector('.life-focus-bar strong').setText('');
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    const focusButton = [...current.mount.querySelector('.life-cb-orbit-actions').querySelectorAll('button')]
      .find((item) => item.textContent === '选择当前专注');
    assert.ok(focusButton);
    assert.equal(focusButton.getAttribute('disabled'), null);
    focusButton.click();
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-open'), true);
  });

  it('主入口切换与旧工具展开不启动装饰动画，也不丢失业务视图', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    const current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(win.liveRafs(), 0);
    const navButtons = current.mount.querySelectorAll('.life-cosmos-nav button');
    navButtons.find(item => item.textContent === '研究记录').click();
    assert.equal(current.mount.querySelector('.life-cosmos-home').getAttribute('data-board'), 'fragments');
    navButtons.find(item => item.textContent === '工作台').click();
    assert.equal(win.liveRafs(), 0);
    current.mount.querySelectorAll('.life-cosmos-tools button').find(item => item.textContent === '打开旧版工作台').click();
    assert.equal(current.legacy.classes.has('is-open'), true);
    assert.equal(win.liveRafs(), 0);
  });

  it('反例11: the My Life.md template ends with the mount handshake and never writes root.innerHTML after it', () => {
    const note = readFileSync(path.join(ROOT, 'tests/fixtures/homepage-template.md'), 'utf8');
    assert.ok(note.includes('<div class="life-cosmos-mount" data-life-cosmos-mount="1"></div>'), 'template emits the mount');
    assert.ok(note.includes('<div class="life-cosmos-legacy">'), 'template emits the legacy wrapper');
    const legacyIndex = note.indexOf('<div class="life-cosmos-legacy">');
    const searchIndex = note.indexOf('life-command-bar');
    assert.ok(legacyIndex >= 0 && searchIndex > legacyIndex, 'search bar is inside legacy');
    const mountIndex = note.indexOf('data-life-cosmos-mount="1"');
    assert.ok(mountIndex >= 0 && mountIndex < legacyIndex, 'mount precedes legacy');

    const handshakeIndex = note.indexOf('plugin.mountHomepageCosmos(root)');
    assert.ok(handshakeIndex > 0, 'template calls the handshake');
    // 握手调用之后、外层 catch 之前，不得再出现任何 root.innerHTML。
    const outerCatch = note.lastIndexOf('} catch (error) {');
    const tail = note.slice(handshakeIndex, outerCatch);
    assert.ok(!tail.includes('root.innerHTML'), 'no root.innerHTML after the mount handshake inside the try block');
  });

  it('反例12: 30 rounds of external DOM mutation leave Cosmos single and visible', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(cosmosCount(doc), 1);

    // 30 秒内的多轮外部 mutation（每秒一轮）：每轮 300ms 防抖后扫描，
    // Cosmos 必须始终唯一、始终留在当前 mount 内。
    for (let round = 0; round < 30; round += 1) {
      FakeMutationObserver.instances[0].trigger();
      await win.pump();
      await settle();
      assert.equal(cosmosCount(doc), 1, `mutation round ${round + 1}: Cosmos stays single`);
      assert.equal(current.mount.querySelectorAll('.life-cosmos-home').length, 1, `mutation round ${round + 1}: Cosmos stays in its mount`);
    }
    assert.equal(current.root.isConnected, true, 'homepage root still connected');
    assert.equal(win.liveIntervals(), 1, 'mutation storms never duplicate wall clocks');
  });

  it('反例13: mount 缺失时诚实降级——legacy 展开、错误面在 legacy 之前、零 Cosmos、返回 failed', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    // 结构损坏：root 只有 legacy，没有 mount。
    const brokenRoot = home.createDiv({ cls: 'life-home' });
    const legacy = brokenRoot.createDiv({ cls: 'life-cosmos-legacy' });
    legacy.createDiv({ cls: 'life-dashboard-content' });

    const status = await harness.plugin.mountHomepageCosmos(brokenRoot);
    await settle();
    assert.equal(status, 'failed', 'structural failure is honestly reported, never disguised as success');
    assert.equal(legacy.classes.has('is-open'), true, 'legacy is opened so the page never goes blank');
    const mount = brokenRoot.querySelector('[data-life-cosmos-mount]');
    assert.ok(mount, 'degradation creates a minimal error mount inside root');
    assert.ok(
      brokenRoot.children.indexOf(mount) < brokenRoot.children.indexOf(legacy),
      'the error surface sits before legacy'
    );
    assert.equal(mount.querySelectorAll('.life-cosmos-error').length, 1, 'honest error is visible');
    assert.ok(mount.text.includes('工作台暂时不可用'), 'fixed safe copy is rendered');
    assert.ok(mount.text.includes('主页缺少独立的 Cosmos 挂载点'), 'exception summary arrives via text nodes');
    assert.equal(cosmosCount(doc), 0, 'zero Cosmos on a broken structure');
  });

  it('反例13b: 缺 legacy 时错误面写入既有 mount，诚实 failed 且零异常泄漏', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const brokenRoot = home.createDiv({ cls: 'life-home' });
    const mount = brokenRoot.createDiv({ cls: 'life-cosmos-mount' });
    mount.setAttribute('data-life-cosmos-mount', '1');

    const status = await harness.plugin.mountHomepageCosmos(brokenRoot);
    await settle();
    assert.equal(status, 'failed');
    assert.equal(mount.querySelectorAll('.life-cosmos-error').length, 1, 'error face lands in the existing mount');
    assert.ok(mount.text.includes('主页缺少折叠的旧版操作面容器'));
    assert.equal(cosmosCount(doc), 0, 'zero Cosmos');
  });

  it('反例13c: 缺 dashboard 时展开 legacy 并把错误面写入 mount', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const brokenRoot = home.createDiv({ cls: 'life-home' });
    const mount = brokenRoot.createDiv({ cls: 'life-cosmos-mount' });
    mount.setAttribute('data-life-cosmos-mount', '1');
    brokenRoot.createDiv({ cls: 'life-cosmos-legacy' }); // 没有 .life-dashboard-content

    const status = await harness.plugin.mountHomepageCosmos(brokenRoot);
    await settle();
    assert.equal(status, 'failed');
    assert.equal(brokenRoot.querySelector('.life-cosmos-legacy').classes.has('is-open'), true, 'legacy opened');
    assert.equal(mount.querySelectorAll('.life-cosmos-error').length, 1);
    assert.ok(mount.text.includes('旧版操作面缺少 dashboard 内容区'));
    assert.equal(cosmosCount(doc), 0);
  });

  it('反例13d: root 不属于主页视图时诚实失败、展开 legacy、零 Cosmos', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    // root 结构完整，但祖先链上没有 .my-life-homepage-view。
    const stranger = doc.body.createDiv({ cls: 'some-other-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const root = stranger.createDiv({ cls: 'life-home' });
    const mount = root.createDiv({ cls: 'life-cosmos-mount' });
    mount.setAttribute('data-life-cosmos-mount', '1');
    const legacy = root.createDiv({ cls: 'life-cosmos-legacy' });
    legacy.createDiv({ cls: 'life-dashboard-content' });

    const status = await harness.plugin.mountHomepageCosmos(root);
    await settle();
    assert.equal(status, 'failed', 'foreign root is honestly rejected');
    assert.equal(legacy.classes.has('is-open'), true, 'legacy opened as the honest fallback');
    assert.equal(mount.querySelectorAll('.life-cosmos-error').length, 1, 'error face visible');
    assert.ok(mount.text.includes('主页根节点不属于 My Life 主页视图'));
    assert.equal(cosmosCount(doc), 0, 'zero Cosmos outside the homepage');
  });

  it('反例13e: 渲染期失败的错误面直接驱动协议——错误可见、legacy 展开、loading 被替换', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const current = buildHomepageRoot(home);
    const session = cosmos.prepareHomepageCosmos(doc, current.root);
    cosmos.failHomepageCosmos(doc, session, new Error('投影服务不可用'));
    assert.equal(current.mount.querySelectorAll('.life-cosmos-error').length, 1, 'honest error is shown inside the mount');
    assert.ok(current.mount.text.includes('投影服务不可用'), 'error carries the real cause');
    assert.equal(current.legacy.classes.has('is-open'), true, 'legacy opens so every capability stays reachable');
    assert.equal(current.mount.querySelectorAll('.life-cosmos-loading').length, 0, 'loading placeholder is replaced, not stacked');
  });

  it('反例14: re-render and unload release every timer, rAF and listener of the previous session', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const first = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(first.root);
    await settle();
    assert.equal(first.mount.querySelector('canvas'), null, 'static workbench needs no animation canvas');
    assert.equal(win.liveIntervals(), 1, 'session one: only wall clock while focus stopped');
    assert.equal(win.liveTimeouts(), 2, 'session one: colon-dim timeouts');
    assert.equal(win.liveRafs(), 0, 'session one: no animation frame');
    assert.equal((home.listeners.pointermove || []).length, 0, 'session one: no pointer glow');

    // Dataview 重渲染：旧根断开，新根接管时先销毁上一轮全部资源。
    first.root.remove();
    const second = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(second.root);
    await settle();
    assert.equal(win.liveIntervals(), 1, 'previous interval released, exactly one wall clock lives');
    assert.equal(win.liveTimeouts(), 2, 'previous timeouts released');
    assert.equal(win.liveRafs(), 0, 'no animation frame created on remount');
    assert.equal((home.listeners.pointermove || []).length, 0, 'no glow listener created on remount');

    // unload：最后一轮也完整清理。
    for (const cleanup of harness.cleanupCallbacks) cleanup();
    assert.equal(win.liveIntervals(), 0, 'unload clears all intervals');
    assert.equal(win.liveTimeouts(), 0, 'unload clears all timeouts');
    assert.equal(win.liveRafs(), 0, 'unload cancels the atlas frame');
    assert.equal((home.listeners.pointermove || []).length, 0, 'unload removes the glow listener');
    assert.equal(cosmosCount(doc), 0, 'unload removes the Cosmos element');
  });

  it('清理反例1: Cosmos 完整渲染后 root.remove()（不建新根），生命周期事件后资源全释放', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(current.root), 'mounted');
    await settle();
    assert.equal(current.mount.querySelector('canvas'), null);
    assert.equal(win.liveIntervals(), 1);
    assert.equal(win.liveRafs(), 0);
    assert.equal((home.listeners.pointermove || []).length, 0);

    // 用户切走/关闭：root 断开，没有后续 mount。
    current.root.remove();
    FakeMutationObserver.instances[0].trigger();
    await win.pump(); // 防抖后的扫描照常运行，但不得重建任何 Cosmos 资源
    await settle();
    assert.equal(win.liveIntervals(), 0, 'intervals released');
    assert.equal(win.liveRafs(), 0, 'rAF cancelled');
    assert.equal(win.liveTimeouts(), 0, 'timeouts cleared');
    assert.equal((home.listeners.pointermove || []).length, 0, 'pointer glow listener removed');
    assert.equal(cosmosCount(doc), 0, 'no Cosmos left in the document');
  });

  it('清理反例2: 整个 home.remove() 或主页 class 被移除，资源与 Map 状态都清理', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    // 场景 A：整个主页容器被移除。
    const homeA = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const first = buildHomepageRoot(homeA);
    assert.equal(await harness.plugin.mountHomepageCosmos(first.root), 'mounted');
    await settle();
    homeA.remove();
    FakeMutationObserver.instances[0].trigger();
    await win.pump();
    await settle();
    assert.equal(win.liveIntervals(), 0, 'home removal releases intervals');
    assert.equal(win.liveRafs(), 0, 'home removal cancels rAF');
    assert.equal(cosmosCount(doc), 0);

    // 场景 B：home 仍连接，但 my-life-homepage-view class 被摘掉（leaf 复用
    // 打开其他笔记），旧根已不再属于主页。
    const homeB = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const second = buildHomepageRoot(homeB);
    assert.equal(await harness.plugin.mountHomepageCosmos(second.root), 'mounted');
    await settle();
    assert.equal(win.liveIntervals(), 1);
    homeB.removeClass('my-life-homepage-view');
    FakeMutationObserver.instances[0].trigger();
    await win.pump();
    await settle();
    assert.equal(win.liveIntervals(), 0, 'class removal releases intervals');
    assert.equal(win.liveRafs(), 0, 'class removal cancels rAF');
    assert.equal((homeB.listeners.pointermove || []).length, 0, 'class removal removes listeners');

    // Map 状态的行为证据：class 恢复后 Obsidian/Dataview 会重建内容（旧根
    // 随重渲染移除），重新挂载必须从零开始并成功落地（旧 generation 项已
    // 删除，不会干扰新一轮守卫）。
    second.root.remove();
    homeB.addClass('my-life-homepage-view');
    const third = buildHomepageRoot(homeB);
    assert.equal(await harness.plugin.mountHomepageCosmos(third.root), 'mounted', 'remount after cleanup works cleanly');
    assert.equal(cosmosCount(doc), 1);
  });

  it('清理反例3: 两个主页副本并存，关闭其中一个只清理该副本', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const homeOne = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const first = buildHomepageRoot(homeOne);
    const homeTwo = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const second = buildHomepageRoot(homeTwo);
    assert.equal(await harness.plugin.mountHomepageCosmos(first.root), 'mounted');
    assert.equal(await harness.plugin.mountHomepageCosmos(second.root), 'mounted');
    await settle();
    assert.equal(cosmosCount(doc), 2, 'two live Cosmos copies');
    assert.equal(win.liveIntervals(), 2, 'two independent wall clocks');

    first.root.remove();
    FakeMutationObserver.instances[0].trigger();
    await win.pump();
    await settle();
    assert.equal(cosmosCount(doc), 1, 'copy two survives');
    assert.equal(win.liveIntervals(), 1, 'only copy one wall clock remains');
    assert.equal(win.liveRafs(), 0, 'copy two still needs no animation');
    assert.equal((homeTwo.listeners.pointermove || []).length, 0, 'copy two needs no pointer listener');

    // 副本二的交互仍正常：「去拍板 →」打开它自己的原生详情。
    const go = second.mount.querySelector('.is-primary');
    assert.ok(go, 'copy two decision deck intact');
    go.click();
    assert.equal(second.legacy.classes.has('is-open'), false, 'copy two workbench remains collapsed');
    assert.equal(second.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-open'), true, 'copy two drawer opens');
  });

  it('清理反例4: 断开清理与下一轮 mount 交错，不误杀新会话', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    // 顺序 A：新一轮 mount 在途时清理运行——在途会话不得被误杀。
    const first = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(first.root), 'mounted');
    await settle();
    first.root.remove();
    const gate = deferred();
    harness.plugin.getThoughtMap = () => gate.promise;
    const second = buildHomepageRoot(home);
    const runTwo = harness.plugin.mountHomepageCosmos(second.root);
    await settle();
    FakeMutationObserver.instances[0].trigger(); // 清理在新会话落地前运行
    await settle();
    gate.resolve(sampleMap());
    assert.equal(await runTwo, 'mounted', 'in-flight mount survives the cleanup pass');
    await win.pump(); // 放行防抖扫描（其共享投影已结算）
    await settle();
    assert.equal(cosmosCount(doc), 1);
    assert.equal(win.liveIntervals(), 1, 'exactly the new wall clock lives');

    // 顺序 B：清理先运行（删除无用 generation 项），新一轮 mount 照常落地。
    second.root.remove();
    FakeMutationObserver.instances[0].trigger();
    await win.pump();
    await settle();
    assert.equal(win.liveIntervals(), 0, 'cleanup disposed the previous session');
    harness.plugin.getThoughtMap = async () => sampleMap();
    const third = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(third.root), 'mounted', 'mount after cleanup starts fresh');
    assert.equal(cosmosCount(doc), 1);
    assert.equal(win.liveIntervals(), 1);
  });

  it('清理反例5: 连续打开/关闭主页 10 次，最终零 session 资源、零 timer、零 listener', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    for (let cycle = 1; cycle <= 10; cycle += 1) {
      const current = buildHomepageRoot(home);
      assert.equal(await harness.plugin.mountHomepageCosmos(current.root), 'mounted', `cycle ${cycle} mounts`);
      current.root.remove();
      FakeMutationObserver.instances[0].trigger();
      await win.pump();
      await settle();
    }
    assert.equal(cosmosCount(doc), 0, 'no Cosmos survives');
    assert.equal(win.liveIntervals(), 0, 'zero intervals after 10 cycles');
    assert.equal(win.liveTimeouts(), 0, 'zero timeouts after 10 cycles');
    assert.equal(win.liveRafs(), 0, 'zero rAFs after 10 cycles');
    assert.equal((home.listeners.pointermove || []).length, 0, 'zero listeners after 10 cycles');

    // 资源回收没有损害后续使用：第 11 次打开照常工作。
    const again = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(again.root), 'mounted', 'the homepage still opens after the cycles');
    assert.equal(cosmosCount(doc), 1);
  });

  it('清理反例6: thought-map 内部 mutation 被扫描过滤，但断开清理照常执行', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(current.root), 'mounted');
    await settle();
    assert.equal(win.liveIntervals(), 1);

    current.root.remove();
    // record.target 落在 .life-thought-map 内：扫描被过滤（不排防抖计时器），
    // 但断开会话清理必须在过滤之前完成。
    FakeMutationObserver.instances[0].callback([
      { type: 'childList', target: { closest: (sel) => (sel === '.life-thought-map' ? {} : null) } },
    ]);
    await settle();
    assert.equal(win.pending().length, 0, 'thought-map churn still schedules no scan');
    assert.equal(win.liveIntervals(), 0, 'disconnected session cleaned despite the scan filter');
    assert.equal(win.liveRafs(), 0, 'rAF cancelled despite the scan filter');
    assert.equal((home.listeners.pointermove || []).length, 0, 'listeners removed despite the scan filter');
  });

  it('清理反例7: MutationObserver 清理路径绝不调用 mount/finish/旧注入逻辑', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(current.root), 'mounted');
    await settle();
    current.root.remove();
    FakeMutationObserver.instances[0].trigger();
    await win.pump();
    await settle();
    const childrenAfterCleanup = current.mount.children.length;
    for (let i = 0; i < 2; i += 1) {
      FakeMutationObserver.instances[0].trigger();
      await win.pump();
      await settle();
    }
    // 行为证据：清理后任何 mutation 都不会制造 Cosmos / loading / 错误面，
    // 也不会向 mount 写入任何新节点。
    assert.equal(cosmosCount(doc), 0, 'cleanup never recreates Cosmos');
    assert.equal(doc.querySelectorAll('.life-cosmos-loading').length, 0, 'cleanup never renders a loading placeholder');
    assert.equal(doc.querySelectorAll('.life-cosmos-error').length, 0, 'cleanup never renders an error face');
    assert.equal(current.mount.children.length, childrenAfterCleanup, 'cleanup never writes into the mount');

    // 源码级互斥：注册表的清理函数体内不得出现任何挂载/落地/旧注入调用。
    const source = readFileSync(path.join(ROOT, 'src', 'console', 'register.js'), 'utf8');
    const start = source.indexOf('function createCosmosRegistry');
    const body = source.slice(start, source.indexOf('\n}\n', start));
    for (const banned of ['prepareHomepageCosmos(', 'finishHomepageCosmos(', 'injectHomepageCosmos', 'failHomepageCosmos(', 'degradeHomepageCosmos(']) {
      assert.ok(!body.includes(banned), `cleanup must not call ${banned}`);
    }
  });

  it('释放反例1(白盒): releaseGeneration 只释放自己注册的序号，绝不动更新一轮', () => {
    const registry = createCosmosRegistry();
    const home = new FakeEl('div');
    const g1 = registry.beginMount(home);
    const g2 = registry.beginMount(home);
    assert.equal(g1, 1);
    assert.equal(g2, 2);
    registry.releaseGeneration(home, g1);
    assert.equal(registry.generations.get(home), 2, '旧请求不得删除新请求的 generation');
    registry.releaseGeneration(home, g2);
    assert.equal(registry.generations.size, 0, '最新一轮释放后无残留');
  });

  it('释放反例2: 在途投影期间移除 root、无成功 session，迟到 stale 释放本轮 generation', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const gate = deferred();
    const harness = makePlugin({ getThoughtMap: () => gate.promise });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    const run = harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    current.root.remove(); // 投影仍在飞行，没有任何成功 session
    gate.resolve(sampleMap());
    assert.equal(await run, 'stale', 'late result is honestly stale');
    await settle();
    assert.equal(cosmosCount(doc), 0);
    assert.equal(win.liveIntervals(), 0, 'no session resources ever materialised');

    // 白盒回放同一路径（beginMount → 断开 → releaseGeneration），
    // 直接观测 Map：本轮 generation 已释放。
    const registry = createCosmosRegistry();
    const g = registry.beginMount(home);
    registry.releaseGeneration(home, g);
    assert.equal(registry.generations.size, 0, 'stale round released its own generation');
  });

  it('释放反例3: 在途断开连续 10 次，generation 与 session 零积累，主页仍可用', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    for (let cycle = 1; cycle <= 10; cycle += 1) {
      const gate = deferred();
      harness.plugin.getThoughtMap = () => gate.promise;
      const current = buildHomepageRoot(home);
      const run = harness.plugin.mountHomepageCosmos(current.root);
      await settle();
      current.root.remove();
      gate.resolve(sampleMap());
      assert.equal(await run, 'stale', `cycle ${cycle}: disconnected round is stale`);
      await settle();
    }
    assert.equal(cosmosCount(doc), 0, 'no Cosmos ever landed');
    assert.equal(win.liveIntervals(), 0, 'zero intervals accumulated');
    assert.equal(win.liveRafs(), 0, 'zero rAFs accumulated');
    assert.equal((home.listeners.pointermove || []).length, 0, 'zero listeners accumulated');

    // 白盒回放 10 次「注册 → 断开释放」：Map 不积累。
    const registry = createCosmosRegistry();
    for (let cycle = 0; cycle < 10; cycle += 1) {
      const g = registry.beginMount(home);
      registry.releaseGeneration(home, g);
    }
    assert.equal(registry.generations.size, 0, 'generations never accumulate');
    assert.equal(registry.sessions.size, 0, 'sessions never accumulate');

    // 泄漏修复不损害后续使用：第 11 次打开照常 mounted。
    harness.plugin.getThoughtMap = async () => sampleMap();
    const again = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(again.root), 'mounted', 'homepage still mounts after the stale cycles');
    assert.equal(cosmosCount(doc), 1);
  });

  it('释放反例4(白盒): 新旧请求交错时旧请求的释放不得删除新 generation，新会话正常存活', () => {
    const doc = makeDocument();
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const live = buildHomepageRoot(home);
    const registry = createCosmosRegistry();
    const g1 = registry.beginMount(home);
    const g2 = registry.beginMount(home); // 新一轮接管
    registry.recordSession(home, {
      home,
      root: live.root,
      mount: live.mount,
      generation: g2,
      dispose: () => {},
    });
    registry.releaseGeneration(home, g1); // 旧请求迟到释放
    assert.equal(registry.generations.get(home), g2, '新 generation 保留');
    registry.cleanupDisconnected(); // 新会话仍连接：不得误清
    assert.equal(registry.sessions.size, 1, 'live session survives cleanup');
    assert.equal(registry.generations.get(home), g2, 'live generation survives cleanup');
    // 新会话断开后：清理同时释放会话与 generation。
    live.root.remove();
    registry.cleanupDisconnected();
    assert.equal(registry.sessions.size, 0);
    assert.equal(registry.generations.size, 0);
  });

  it('释放反例5: 渲染期抛错返回 failed、错误面可见、legacy 展开，generation 同步释放', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    // 投影在 finish 阶段读取 total 时抛错（启动扫描在 homepage root 建立前
    // 已早退，不会触及该投影）。
    const harness = makePlugin({
      getThoughtMap: async () => ({
        counts: { open: 0, provisional: 0, concluded: 0 },
        categories: [],
        get total() { throw new Error('投影损坏'); },
      }),
    });
    setupLoopConsole(harness.plugin);
    await settle();

    const current = buildHomepageRoot(home);
    const status = await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(status, 'failed', 'render failure is honestly failed');
    assert.equal(current.legacy.classes.has('is-open'), true, 'legacy opened');
    assert.equal(current.mount.querySelectorAll('.life-cosmos-error').length, 1, 'error face visible');
    assert.ok(current.mount.text.includes('投影损坏'), 'error carries the real cause');
    assert.equal(cosmosCount(doc), 0, 'zero Cosmos');

    // 白盒回放「注册 → 失败降级 → 释放」：无残留。
    const registry = createCosmosRegistry();
    const g = registry.beginMount(home);
    registry.releaseGeneration(home, g);
    assert.equal(registry.generations.size, 0, 'failed round released its own generation');
  });

  it('释放反例6: 释放后清理路径仍然零注入、零重复 Cosmos', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    // 一轮在途断开（stale + 释放）后，再正常挂载一轮。
    const gate = deferred();
    harness.plugin.getThoughtMap = () => gate.promise;
    const staleRound = buildHomepageRoot(home);
    const staleRun = harness.plugin.mountHomepageCosmos(staleRound.root);
    await settle();
    staleRound.root.remove();
    gate.resolve(sampleMap());
    assert.equal(await staleRun, 'stale');
    harness.plugin.getThoughtMap = async () => sampleMap();
    const current = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(current.root), 'mounted');
    await settle();

    current.root.remove();
    for (let i = 0; i < 3; i += 1) {
      FakeMutationObserver.instances[0].trigger();
      await win.pump();
      await settle();
    }
    assert.equal(cosmosCount(doc), 0, 'cleanup after release never recreates Cosmos');
    assert.equal(doc.querySelectorAll('.life-cosmos-loading').length, 0, 'no loading placeholder injected');
    assert.equal(doc.querySelectorAll('.life-cosmos-error').length, 0, 'no error face injected');
    assert.equal(win.liveIntervals(), 0, 'zero live timers');

    // 接线断言：mount 握手体内恰好两处确定性释放（断开 stale / 失败 failed）。
    const source = readFileSync(path.join(ROOT, 'src', 'console', 'register.js'), 'utf8');
    const start = source.indexOf('plugin.mountHomepageCosmos = (root)');
    const body = source.slice(start, source.indexOf('const scanHomepage', start));
    const releases = body.split('releaseGeneration(').length - 1;
    assert.equal(releases, 2, 'stale-disconnect and failed paths both release their own generation');
  });

  it('ABA反例1(白盒): 挂载身份永久不复用——释放→新挂载后旧轮迟到释放不误删新轮', () => {
    const doc = makeDocument();
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const live = buildHomepageRoot(home);
    const registry = createCosmosRegistry();

    const tokenA = registry.beginMount(home);
    const tokenB = registry.beginMount(home);
    assert.notEqual(tokenB, tokenA, 'B 接管取新身份');
    registry.releaseGeneration(home, tokenB); // B 释放：Map 项删除
    assert.equal(registry.generations.size, 0);

    const tokenC = registry.beginMount(home);
    assert.notEqual(tokenC, tokenA, 'C 身份不得复用 A 的旧编号（ABA 核心断言）');
    assert.notEqual(tokenC, tokenB, 'C 身份不得复用 B 的旧编号');

    registry.releaseGeneration(home, tokenA); // A 迟到释放
    assert.equal(registry.generations.get(home), tokenC, 'C 仍是 current，Map 未被误删');

    // C 的完整生命周期不受影响：登记会话 → 连接中清理保留 → 断开清理释放。
    registry.recordSession(home, {
      home,
      root: live.root,
      mount: live.mount,
      generation: tokenC,
      dispose: () => {},
    });
    registry.cleanupDisconnected();
    assert.equal(registry.sessions.size, 1, 'connected session C survives cleanup');
    assert.equal(registry.generations.get(home), tokenC);
    live.root.remove();
    registry.cleanupDisconnected();
    assert.equal(registry.sessions.size, 0, 'disconnected session C is cleaned');
    assert.equal(registry.generations.size, 0, 'C generation released by cleanup');
  });

  it('ABA反例2(握手级): 旧异步结果在「释放→新挂载」之后仍返回 stale，新 Cosmos 唯一落地', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const gate = deferred();
    const harness = makePlugin({ getThoughtMap: () => gate.promise });
    setupLoopConsole(harness.plugin);
    await settle();

    // A 在途（挂在共享投影上）。
    const roundA = buildHomepageRoot(home);
    const runA = harness.plugin.mountHomepageCosmos(roundA.root);
    await settle();

    // 投影结算的同一同步窗口内：B 接管（新身份）、A 的旧根断开。
    gate.resolve(sampleMap());
    harness.plugin.getThoughtMap = async () => sampleMap();
    roundA.root.remove();
    const roundB = buildHomepageRoot(home);
    const runB = harness.plugin.mountHomepageCosmos(roundB.root);
    await settle(); // A 在此恢复：身份 mismatch → stale，且不得误删 B 的身份
    assert.equal(await runB, 'mounted', 'B 正常落地');
    await settle();
    assert.equal(cosmosCount(doc), 1, 'B owns the page');

    // B 的会话经断开清理释放（generations Map 项删除）。
    roundB.root.remove();
    FakeMutationObserver.instances[0].trigger();
    await win.pump();
    await settle();
    assert.equal(cosmosCount(doc), 0);
    assert.equal(win.liveIntervals(), 0, 'B session fully released');

    // C 新挂载：旧实现会把编号重置回 A 的身份（ABA）；修复后身份唯一、
    // C 正常落地且唯一。
    const roundC = buildHomepageRoot(home);
    assert.equal(await harness.plugin.mountHomepageCosmos(roundC.root), 'mounted', 'C lands after the release');
    await settle();
    assert.equal(cosmosCount(doc), 1, 'exactly one Cosmos after the interleave');
    assert.equal(roundC.mount.querySelectorAll('.life-cosmos-home').length, 1, 'C owns its mount');
    assert.equal(win.liveIntervals(), 1, 'only C session keeps its wall clock');

    // 最后才读 A 的结局：穿越「B 落地 → B 释放 → C 挂载」之后，旧结果仍
    // 必须是 stale，绝不错误落地。
    assert.equal(await runA, 'stale', 'late A stays stale across release and remount');
  });
});


/* ================================================================
   Cosmos 能力迁移 V1：任务舱 / 语义抽屉 / 诚实状态反例
   覆盖冻结设计 §11：语义路由（绝不按 DOM 按钮顺序）、Quick Capture 单一
   写路径、GO/EVA 绑定降级、Graph 六态与刷新失败保留旧数据、Continuation
   空输入/双击/409/迟到、资产显式路径、Review/Pilot 具名动作、抽屉键盘与
   焦点、生命周期零积累。
   ================================================================ */
describe('Cosmos 能力迁移 V1（任务舱与语义抽屉）', () => {
  let win;
  let savedGlobals;

  const DIGEST = (ch) => ch.repeat(64);

  beforeEach(() => {
    FakeMutationObserver.instances = [];
    win = makeWindow();
    savedGlobals = {
      document: globalThis.document,
      window: globalThis.window,
      MutationObserver: globalThis.MutationObserver,
    };
    globalThis.window = win.window;
    globalThis.MutationObserver = FakeMutationObserver;
    requestUrlHandler = async () => envelope([]);
  });

  afterEach(() => {
    globalThis.document = savedGlobals.document;
    globalThis.window = savedGlobals.window;
    globalThis.MutationObserver = savedGlobals.MutationObserver;
  });

  function pilotPlanPayload() {
    return {
      plan_version: '1', spec_id: 'fragment-pilot-v1', spec_digest: DIGEST('c'),
      task_label: '碎片试点', node_count: 3, human_gates: 2, max_feedback: 1,
      max_total_calls: 2, cost_cap_cny: 0.5, provider: 'kimi', model: 'k3',
      node_flow: '收集 → 判断 → 输出', expected_output: '研究结论',
      write_scope: '仅研究报告', create_behavior: '创建后等待人工授权',
    };
  }

  function reviewCandidatePayload(overrides = {}) {
    return {
      candidate_id: 'cand-1', title: '候选甲', fragment_ref: '散记/碎片想法/frag-1.md',
      note_path: '散记/已整理碎片/org-1.md', content_status: 'pending_confirmation',
      evidence_level: 'unverified', revision: 1, content_sha256: DIGEST('d'),
      has_conflict: false, available_actions: ['confirm_asset', 'keep_draft', 'reject'],
      core_judgment: '核心判断',
      ...overrides,
    };
  }

  function alignmentPayload(overrides = {}) {
    return {
      alignment_id: 'align-1', fragment_id: 'frag-1', case_id: 'case-1', episode_id: 'ep-1',
      title: '核验 MiniMax H3', status: 'passed', sequence: 2, revision: 1,
      input_digest: DIGEST('a'), reasoning: '证据仍不足', plan: '补充核验',
      expected_result: '结论', exclusions: [], suggested_intents: ['verify'],
      dynamic_intents: [], recommended_route: 'verify',
      execution_scope: { capabilities: [], external_scope: [], model_call_cap: 0, cost_cap_cny: 0, side_effect: '无' },
      memory_basis: [],
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      route: 'verify',
      execution: {
        run_id: 'run-1', fragment_id: 'frag-1', status: 'passed', current_node: '',
        route: 'verify', stop_reason: '', updated_at: '2026-08-14T08:00:00Z',
        result_digest: DIGEST('b'),
        harvest: [{ role: 'pattern', summary: '官方来源优先', maturity: 'candidate' }],
        result: {
          summary: '初步结论', unknowns: [], next_checks: [], needs_escalation: false,
          escalation_reason: '', model_calls: 1, tool_calls: 1,
        },
      },
      updated_at: '2026-08-14T08:00:00Z',
      ...overrides,
    };
  }

  // 统一请求路由：按 URL 前缀分派到各夹具；记录全部请求供计数断言。
  function routeRequests(overrides = {}) {
    const calls = [];
    requestUrlHandler = async (options) => {
      calls.push(options);
      const url = String(options.url);
      if (url.includes('/fragment/v1/episodes/')) {
        return overrides.continueEpisode ? overrides.continueEpisode(options) : envelope(alignmentPayload({ episode_id: 'ep-2', sequence: 3 }));
      }
      if (url.includes('/fragment/v1/alignments') && String(options.method || 'GET') !== 'GET') {
        return overrides.decide ? overrides.decide(options) : envelope(alignmentPayload());
      }
      if (url.includes('/fragment/v1/alignments')) {
        return overrides.alignments ? overrides.alignments(options) : envelope([]);
      }
      if (url.includes('/fragment/v1/continuations')) {
        return overrides.continuations ? overrides.continuations(options) : envelope([]);
      }
      if (url.endsWith('/fragment/v1/knowledge/periods')) {
        return overrides.periods ? overrides.periods(options) : envelope([]);
      }
      if (url.endsWith('/fragment/v1/knowledge/notifications')) {
        return overrides.notifications ? overrides.notifications(options) : envelope([]);
      }
      if (url.includes('/fragment/v1/knowledge')) {
        return overrides.knowledge ? overrides.knowledge(options) : envelope([]);
      }
      if (url.includes('/fragment/v1/product-reviews/')) {
        return envelope({ ...reviewCandidatePayload(), candidate_cards: [{ card_id: 'kc-1', title: '知识卡一', knowledge: '知识内容' }] });
      }
      if (url.includes('/fragment/v1/product-reviews')) {
        return overrides.reviews ? overrides.reviews(options) : envelope([]);
      }
      if (url.includes('/graph/v1/pilot-plans')) {
        return overrides.pilotPlan ? overrides.pilotPlan(options) : envelope(pilotPlanPayload());
      }
      if (url.includes('/loop/v1/runs')) {
        return overrides.loopRuns ? overrides.loopRuns(options) : loopEnvelope([]);
      }
      if (url.includes('/graph/v1/runs')) {
        if (url.includes('/canvas')) return overrides.canvas ? overrides.canvas(options) : envelope(null);
        // /runs/{id} 详情（auth.approve 绑定材料）与 /runs 列表分开。
        if (/\/runs\/[^/]+$/.test(url)) return overrides.runDetail ? overrides.runDetail(options) : envelope(null);
        return overrides.runs ? overrides.runs(options) : envelope([]);
      }
      // R1 授权队列路由：review/control/proposals 由既有 client 方法调用。
      if (url.includes('/review/v1/decisions')) {
        const decisionOverride = overrides['/decisions'] || overrides.decisions;
        return decisionOverride ? decisionOverride(options) : envelope({ decision_id: 'none' });
      }
      if (url.includes('/review/v1/actions/')) {
        return overrides.reviewActions ? overrides.reviewActions(options) : envelope({ proposal_id: 'none', submittable: false });
      }
      if (url.includes('/control/v1/actions/')) {
        return overrides.controlActions ? overrides.controlActions(options) : envelope({ run_id: 'none', status: 'running', actions: {} });
      }
      if (url.includes('/proposals')) {
        return overrides.proposals ? overrides.proposals(options) : envelope({ items: [] });
      }
      return envelope([]);
    };
    return calls;
  }

  async function setupMounted({ requests, beforeMount, makeOptions } = {}) {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const calls = routeRequests(requests);
    const harness = makePlugin({ getThoughtMap: async () => sampleMap(), ...(makeOptions || {}) });
    setupLoopConsole(harness.plugin);
    await settle();
    const current = buildHomepageRoot(home);
    if (beforeMount) beforeMount(current);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    return { doc, home, harness, current, calls };
  }

  // FakeEl 的 matches 只支持属性存在性，不支持 [attr="value"]：按值过滤。
  function byAction(root, actionId) {
    return [...root.querySelectorAll('[data-cosmos-action]')]
      .find((el) => el.getAttribute('data-cosmos-action') === actionId) || null;
  }

  function capsuleAction(mount, actionId) {
    return byAction(mount, actionId);
  }

  it('任务舱保留日常入口，Loop/Graph 技术入口集中系统管理', async () => {
    const { current } = await setupMounted();
    const capsule = current.mount.querySelector('.life-cosmos-capsule');
    assert.ok(capsule, '任务舱存在');
    for (const actionId of ['capture.quick', 'graph.refresh', 'drawer.settings']) {
      assert.ok(capsuleAction(current.mount, actionId), `入口 ${actionId} 存在`);
    }
    for (const actionId of ['loop.openConsole', 'graph.openWorkflow']) {
      assert.equal(byAction(capsule, actionId), null, '技术控制不占用录入入口');
    }
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const settings = current.mount.querySelector('.life-cosmos-drawer');
    for (const actionId of ['loop.openConsole', 'graph.openWorkflow']) {
      assert.ok(byAction(settings, actionId), '系统管理保留真实控制入口');
      assert.equal([...current.mount.querySelectorAll('[data-cosmos-action]')]
        .filter((el) => el.getAttribute('data-cosmos-action') === actionId).length, 1);
    }
    assert.equal(current.mount.querySelector('.life-cosmos-capsule-search input'), null, '搜索归属研究记录和知识库，不误称全局搜索');
    assert.equal(current.legacy.classes.has('is-open'), false, 'legacy 折叠，入口不藏在旧界面');
  });

  it('initial pending production sources cannot render zero counts before the actual read completes', async () => {
    const alignments = deferred(); const reviews = deferred();
    const doc = makeDocument(); globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    routeRequests({ alignments: () => alignments.promise, reviews: () => reviews.promise });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin); await settle();
    const current = buildHomepageRoot(home);
    const mounting = harness.plugin.mountHomepageCosmos(current.root); await settle();
    assert.equal(current.mount.querySelectorAll('.life-workbench-stat').length, 0, 'unresolved sources leave the loading surface, not fabricated zero statistics');
    alignments.resolve(envelope([])); await settle();
    assert.equal(current.mount.querySelectorAll('.life-workbench-stat').length, 0, 'review source is also awaited before claims of an empty queue');
    reviews.resolve(envelope([])); assert.equal(await mounting, 'mounted'); await settle();
    const stats = current.mount.querySelectorAll('.life-workbench-stat strong');
    assert.equal(stats[0].text, '0'); assert.equal(stats[1].text, '0');
    assert.match(current.mount.querySelector('.life-workbench').text, /当前没有待处理的研究记录/);
  });

  it('workbench keeps unavailable counts unknown and does not infer an empty user queue', async () => {
    const unavailable = () => { throw new Error('service unavailable'); };
    const { current } = await setupMounted({ requests: {
      alignments: unavailable, reviews: unavailable, knowledge: unavailable, periods: unavailable, notifications: unavailable,
    } });
    const workbench = current.mount.querySelector('.life-workbench');
    const stats = [...workbench.querySelectorAll('.life-workbench-stat strong')].map(el => el.text);
    assert.deepEqual(stats, ['—', '—', '—', '—']);
    assert.match(workbench.text, /研究服务读取失败/);
    assert.doesNotMatch(workbench.text, /当前没有待处理的研究记录/);
    const history = current.mount.querySelector('.life-knowledge-history summary');
    assert.match(history.text, /待确认/);
  });

  it('workbench shortcuts select real active or blocked research and untracked rows remain separately counted', async () => {
    const active = alignmentPayload({ fragment_id: 'active', title: '正在实际研究', alignment_id: 'align-active' });
    active.execution = { ...active.execution, status: 'running', result: null, harvest: [], research_progress: { stage: 'researching', collected_sources: 0, model_calls: 2 } };
    const blocked = alignmentPayload({ fragment_id: 'blocked', title: '综合判断阻塞', alignment_id: 'align-blocked' });
    blocked.execution = { ...blocked.execution, result: null, harvest: [], research_progress: { stage: 'synthesis_disabled', blocker: 'synthesis_disabled', collected_sources: 1, model_calls: 0 } };
    const { current } = await setupMounted({ requests: { alignments: () => envelope([active, blocked]) } });
    const metrics = [...current.mount.querySelectorAll('[data-fragment-filter]')];
    const count = id => Number(metrics.find(el => el.getAttribute('data-fragment-filter') === id).querySelector('b').text);
    assert.equal(count('all'), 3);
    assert.equal(count('untracked'), 1);
    assert.equal(metrics.filter(el => el.getAttribute('data-fragment-filter') !== 'all').reduce((sum, el) => sum + Number(el.querySelector('b').text), 0), count('all'));
    const workbench = current.mount.querySelector('.life-workbench');
    for (const [label, state] of [['研究进行中', 'active'], ['系统阻塞', 'blocked']]) {
      const stat = [...workbench.querySelectorAll('.life-workbench-stat')].find(el => el.text.includes(label));
      assert.equal(stat.querySelector('strong').text, '1'); stat.click();
      assert.equal(current.mount.querySelector('.life-cosmos-home').getAttribute('data-board'), 'fragments');
      const visible = [...current.mount.querySelectorAll('.life-frag-panorama-row')].filter(el => el.getAttribute('hidden') === null);
      assert.equal(visible.length, 1); assert.equal(visible[0].getAttribute('data-fragment-state'), state);
    }
    assert.equal(workbench.querySelectorAll('.life-research-row').length, 2, 'untracked capture never pretends to run');
  });

  it('topic counts separate research, derived structure and unknown type while latest results expose stale cache honestly', async () => {
    const research = knowledgeNote({ freshness: { status: 'current', reasons: [], latest_revision: 2 }, usable_as_current: true, conclusion_authority: 'research_result' });
    const derived = knowledgeNote({ knowledge_id: 'knowledge-' + 'c'.repeat(24), title: '主题结构', content_status: 'theme_summary', conclusion_authority: 'canonical_research', usable_as_current: false, freshness: { status: 'current', reasons: [], latest_revision: 2 } });
    const unknown = knowledgeNote({ knowledge_id: 'knowledge-' + 'd'.repeat(24), title: '类型未知记录' });
    let failed = false;
    const { current } = await setupMounted({ requests: { knowledge: () => {
      if (failed) throw new Error('knowledge offline');
      return envelope([{ ...knowledgeTopics()[0], notes: [research, derived, unknown] }]);
    } } });
    assert.match(current.mount.querySelector('.life-knowledge-topic summary').text, /1 份研究 · 1 份结构整理 · 1 份类型待确认/);
    assert.equal(current.mount.querySelector('.life-workbench-latest').querySelectorAll('.life-research-row').length, 1);
    assert.equal(current.mount.querySelector('.life-knowledge-topic').querySelectorAll('.life-knowledge-note').length, 3);
    failed = true; byAction(current.mount, 'graph.refresh').click(); await settle();
    assert.match(current.mount.querySelector('.life-workbench-latest').text, /上次读取的结果，当前性待重查/);
    assert.equal([...current.mount.querySelectorAll('.life-workbench-stat')].find(el => el.text.includes('可用研究结论')).querySelector('strong').text, '—');
  });

  it('Quick Capture 连点只触发一个既有 modal，源码不存在第二写入路径', async () => {
    const { harness, current } = await setupMounted();
    const capture = capsuleAction(current.mount, 'capture.quick');
    capture.click();
    capture.click();
    await settle();
    const calls = harness.pluginCalls.filter((call) => call[0] === 'openQuickCapture');
    assert.equal(calls.length, 1, '连点只打开一个既有 QuickCaptureModal');
    // 源码级：Cosmos 与 adapter 层不得出现第二个 Vault 写入或碎片路径拼接。
    const cosmosSource = readFileSync(path.join(ROOT, 'src', 'console', 'homepage-cosmos.js'), 'utf8');
    const registerSource = readFileSync(path.join(ROOT, 'src', 'console', 'register.js'), 'utf8');
    for (const banned of ['vault.create', 'vault.process', 'vault.modify', 'createFolder', '碎片想法/']) {
      assert.ok(!cosmosSource.includes(banned), `homepage-cosmos.js 不得包含 ${banned}`);
      assert.ok(!registerSource.includes(banned), `register.js 不得包含 ${banned}`);
    }
  });

  it('Loop/Graph/今日笔记在 legacy 折叠时仍真实打开', async () => {
    const { harness, current } = await setupMounted();
    const revealed = [];
    harness.plugin.app.workspace.getLeaf = () => ({ setViewState: async () => {} });
    harness.plugin.app.workspace.revealLeaf = (leaf) => { revealed.push(leaf); };

    byAction(current.mount, 'drawer.settings').click();
    await settle();
    capsuleAction(current.mount, 'loop.openConsole').click();
    await settle();
    capsuleAction(current.mount, 'graph.openWorkflow').click();
    await settle();
    current.mount.querySelector('.life-cosmos-drawer-close').click();
    [...current.mount.querySelectorAll('.life-cosmos-tools button')].find(el => el.textContent === '日记与专注工具').click();
    [...current.mount.querySelectorAll('button')].find(el => el.textContent === '打开今日笔记').click();
    await settle();

    assert.equal(revealed.length, 2, 'Loop 与 Graph 视图真实打开');
    assert.ok(harness.executedCommands.includes('daily-notes:open-today'), '今日笔记走既有命令链');
    assert.equal(current.legacy.classes.has('is-open'), false, '全程不展开深度工作台');
  });

  it('Graph 六态、最近运行与 Checkpoint 安全摘要，技术 ID 不进主视觉', async () => {
    const runs = [
      { run_id: 'exec:aaa', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'running', sequence: 5, step_count: 3, updated_at: '2026-08-14T10:00:00Z', started_at: '2026-08-14T09:00:00Z', pending_human: [] },
      { run_id: 'exec:bbb', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'human_wait', sequence: 2, step_count: 2, updated_at: '2026-08-13T09:00:00Z', started_at: '2026-08-13T08:00:00Z', pending_human: [{ node: 'n' }] },
      { run_id: 'exec:ccc', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'completed', sequence: 9, step_count: 4, updated_at: '2026-08-12T08:00:00Z', started_at: '2026-08-12T07:00:00Z', pending_human: [] },
      { run_id: 'exec:ddd', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'mystery', sequence: 1, step_count: 1, updated_at: '2026-08-11T08:00:00Z', started_at: '2026-08-11T07:00:00Z', pending_human: [] },
    ];
    const { current } = await setupMounted({ requests: { runs: () => envelope(runs) } });
    const panel = current.mount.querySelector('.life-cb-graph-body');
    assert.ok(panel, 'Graph 六态面板存在');
    const text = panel.text;
    assert.ok(text.includes('最近运行'), '保留最近运行');
    assert.ok(text.includes('Checkpoint #5'), '最近运行按 updated_at 选择，显示 Checkpoint 安全摘要');
    assert.ok(!text.includes('exec:'), '技术 ID 不进主视觉');
    const cells = [...panel.querySelectorAll('.life-cb-graph-cell')].map((cell) => cell.text);
    assert.deepEqual(cells, ['1进行中', '1等待人工', '0异常', '1已结束', '1未知'], '六态互斥且未知不冒充结束');
    // 非法时间的 run 不能成为最近运行。
    assert.ok(!text.includes('时间未知') || text.includes('Checkpoint #5'), '非法时间不声明最近运行');
  });

  it('Graph 刷新失败保留旧摘要并标注服务错误', async () => {
    let fail = false;
    const { current } = await setupMounted({
      requests: {
        runs: () => {
          if (fail) return { status: 503, text: JSON.stringify({ contract_version: '2', error: { code: 'down' } }) };
          return envelope([{ run_id: 'exec:aaa', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'running', sequence: 5, step_count: 3, updated_at: '2026-08-14T10:00:00Z', started_at: '2026-08-14T09:00:00Z', pending_human: [] }]);
        },
      },
    });
    const before = current.mount.querySelector('.life-cb-graph-body').text;
    assert.ok(before.includes('Checkpoint #5'));
    fail = true;
    capsuleAction(current.mount, 'graph.refresh').click();
    await settle();
    const status = current.mount.querySelector('.life-cosmos-capsule-status');
    assert.equal(status.getAttribute('data-kind'), 'error', '刷新失败诚实报错');
    // 面板旧数据仍在（静态投影不被清空）。
    assert.ok(current.mount.querySelector('.life-cb-graph-body').text.includes('Checkpoint #5'), '旧摘要保留');
    // 抽屉详情：错误 + 旧摘要并列。
    byAction(current.mount, 'drawer.graph').click();
    await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('服务不可用'), '抽屉标注服务错误');
    assert.ok(drawer.text.includes('最近一次成功读取'), '旧数据明确标注为历史快照');
    assert.ok(drawer.text.includes('Checkpoint #5'), '抽屉保留旧 Checkpoint 摘要');
  });

  it('GO：绑定完整才调用既有 completeTask；无绑定降级为本次会话选择且重渲染不伪持久', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        const li = fixture.dashboard.querySelector('.life-todos li');
        li.setAttribute('data-complete-task', 'tasks/today.md');
        li.setAttribute('data-task-line', '2');
        const li2 = fixture.dashboard.querySelector('.life-todos ul').createEl('li');
        li2.createEl('strong', { text: '无绑定任务' });
      },
    });
    // 写前只读预检看到未完成行；唯一 completeTask 写入后再读确认 [x]。
    let completed = false;
    harness.plugin.completeTask = async (path, line) => {
      harness.pluginCalls.push(['completeTask', path, line]);
      completed = true;
    };
    harness.plugin.app.vault.read = async () => `第一行\n第二行\n- [${completed ? 'x' : ' '}] 确认研究方向`;
    // 限定在发射任务控制面板（.life-cosmos-focus）内查找——Round3 今日待办面板
    // 同样提供 item.complete，全 mount 查找会命中错面板（banner 不动）。
    const mission = current.mount.querySelector('.life-cosmos-focus');
    const rows = [...mission.querySelectorAll('.life-cosmos-go-item')];
    const bound = rows.find((row) => byAction(row, 'item.complete'));
    const session = rows.find((row) => row.getAttribute('data-session-choice') === '1');
    assert.ok(bound, '绑定行存在完成按钮');
    assert.ok(session, '无绑定行降级为会话选择');

    // D11：横幅状态化两态——初始未全部完成时是中性就绪文案
    const banner = mission.querySelector('.life-cosmos-banner');
    assert.ok(banner, '横幅存在');
    assert.ok(banner.text.includes('待全部完成') && banner.text.includes('就绪'), '初始为中性就绪文案');
    assert.ok(!banner.text.includes('允许升空'), '初始不宣称允许升空');

    byAction(bound, 'item.complete').click();
    await settle();
    assert.deepEqual(harness.pluginCalls.filter((call) => call[0] === 'completeTask'), [['completeTask', 'tasks/today.md', 2]], '调用既有 completeTask');
    assert.equal(bound.getAttribute('data-done'), '1', '只读权威确认通过后才显示完成');
    assert.ok(bound.text.includes('已完成'));
    // D11 第二态：仍有会话行未完成 → 部分就绪文案
    assert.ok(banner.text.includes('1/2 就绪') && !banner.text.includes('允许升空'), `部分完成仍不宣称允许升空，got "${banner.text}"`);

    session.click();
    assert.ok(!harness.pluginCalls.some((call) => call[0] === 'completeTask' && call[1] !== 'tasks/today.md'), '会话选择不产生写入');
    assert.ok(session.text.includes('会话选择'), '文案明确为会话选择');
    assert.ok(!session.text.includes('已完成'), '无持久完成暗示');
    // D11 第三态：两行都完成（会话选择也算当前会话完成）→ 允许升空
    assert.ok(banner.text.includes('允许升空'), '全部完成后允许升空');

    // 重渲染（Dataview 新一轮）后会话选择不保留。
    const homeView = current.root.parent;
    current.root.remove();
    const next = buildHomepageRoot(homeView);
    next.dashboard.querySelector('.life-todos ul').createEl('li').createEl('strong', { text: '无绑定任务' });
    await harness.plugin.mountHomepageCosmos(next.root);
    await settle();
    const again = [...next.mount.querySelectorAll('.life-cosmos-go-item')].find((row) => row.getAttribute('data-session-choice') === '1');
    assert.ok(again, '重渲染后会话行重新出现');
    assert.equal(again.getAttribute('data-done'), '0', '会话选择不跨渲染持久');
  });

  it('EVA：绑定行真实完成，无绑定行点击只是会话选择', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        const li = fixture.dashboard.querySelector('.life-todos li');
        li.setAttribute('data-complete-task', 'tasks/today.md');
        li.setAttribute('data-task-line', '4');
      },
    });
    let completed = false;
    harness.plugin.completeTask = async (path, line) => {
      harness.pluginCalls.push(['completeTask', path, line]);
      completed = true;
    };
    harness.plugin.app.vault.read = async () => `一\n二\n三\n四\n- [${completed ? 'x' : ' '}] 确认研究方向`;
    const nav = [...current.mount.querySelectorAll('.life-cosmos-tools button')].find((item) => item.textContent === '日记与专注工具');
    nav.click();
    await settle();
    // P5：EVA 行统一为 go-item 类族（与今日待办面板同长相），完成协议零改动
    const items = [...current.mount.querySelectorAll('.life-cb-eva .life-cosmos-go-item')];
    const bound = items.find((item) => byAction(item, 'item.complete'));
    assert.ok(bound, 'EVA 绑定行存在');
    byAction(bound, 'item.complete').click();
    await settle();
    assert.deepEqual(harness.pluginCalls.filter((call) => call[0] === 'completeTask'), [['completeTask', 'tasks/today.md', 4]]);
    assert.ok(bound.classes.has('is-done'), '真实完成后状态翻转');
    assert.ok(bound.text.includes('已完成'), '完成反馈可见');

    const unbound = items.find((item) => item.getAttribute('data-session-choice') === '1');
    if (unbound) {
      unbound.click();
      assert.ok(unbound.text.includes('会话选择'), '无绑定行明示会话语义');
      assert.equal(harness.pluginCalls.filter((call) => call[0] === 'completeTask').length, 1, '无绑定行不产生额外写入');
    }
  });

  const knowledgeNote = (overrides = {}) => ({
    knowledge_id: `knowledge-${'a'.repeat(24)}`, revision: 2, title: 'Archify 的适配判断',
    path: 'Loop知识库/Archify.md', updated_at: '2026-09-08T09:00:00Z', kind: 'research',
    content_status: 'qualified_conclusion', revision_reason: '补入真实试跑证据',
    result: {
      summary: '适合静态流程表达，实时状态需提供映射。', recommendation: '限定试用',
      answer_markdown: '完整回答：<img src=x onerror=alert(1)> 不执行正文 HTML。',
      confirmed: [{ claim: '示例通过验证', evidence_ids: ['ev-1'] }],
      unknowns: ['生产映射尚未验证'], limitations: ['图正确不代表输入判断正确'], conflicts: [],
      coverage: [{ question: '是否能试用？', answer: '可以，限定表达范围', evidence_ids: ['ev-1'] }],
      agent_usage: { when_to_use: '解释工作流时', steps: ['按已验证版本生成输入', '运行 validate'], limitations: ['不要推测当前运行状态'] },
    },
    evidence: [{ evidence_id: 'ev-1', title: '独立验证', url: 'https://example.org/evidence', excerpts: ['doctor 返回成功'] }],
    ...overrides,
  });
  async function expandAnswer(drawer) {
    const details = drawer.querySelector('.life-knowledge-answer');
    assert.ok(details, '完整回答有可展开入口');
    for (let node = details; node; node = node.parent) if (node.tagName === 'DETAILS') node.open = true;
    details.dispatchEvent({ type: 'toggle' });
    await settle();
    return details;
  }

  const knowledgeTopics = () => [{
    topic_id: `topic-${'b'.repeat(24)}`, category: '技术', subcategory: 'Agent 工具',
    title: '系统可视化', notes: [knowledgeNote()],
  }];

  it('真实知识目录按大类/小类/主题展示，详情读取完整结论并打开 Vault 记录', async () => {
    const { current, harness, calls } = await setupMounted({ requests: {
      knowledge: (options) => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : knowledgeNote()),
    } });
    const panel = current.mount.querySelector('.life-knowledge-catalogue');
    assert.ok(panel.text.includes('技术'));
    assert.ok(panel.text.includes('Agent 工具'));
    assert.ok(panel.text.includes('系统可视化'));
    assert.ok(current.mount.querySelector('.life-cb-catalog'), '原有资产目录未被覆盖');
    panel.querySelector('.life-knowledge-note').click();
    await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-knowledge-reading'), 'knowledge opens in the readable-width mode');
    assert.ok(drawer.text.includes('适合静态流程表达'));
    assert.ok(drawer.text.includes('完整回答：<img'));
    assert.ok(drawer.text.includes('运行 validate'));
    assert.ok(drawer.text.includes('生产映射尚未验证'));
    assert.ok(drawer.text.includes('补入真实试跑证据'));
    assert.equal(drawer.querySelector('img'), null, '模型正文只作文本');
    [...drawer.querySelectorAll('button')].find((el) => el.textContent === '在 Obsidian 打开完整记录').click();
    await settle();
    assert.ok(harness.openedLinks.includes('Loop知识库/Archify.md'));
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-open'), false);
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-knowledge-reading'), false, 'closing clears the reading mode for subsequent action drawers');
    assert.ok(calls.filter((call) => call.url.includes('/knowledge')).every((call) => call.method === 'GET'));
  });


  it('knowledge opens with the complete short conclusion and renders detailed Markdown only after disclosure', async (t) => {
    const previousRenderer = markdownRenderHandler;
    t.after(() => { markdownRenderHandler = previousRenderer; });
    markdownRenderHandler = async (_app, text, host) => {
      if (text.startsWith('```mermaid')) host.createEl('svg', { text: '可见图表哨兵' });
      else host.createEl('p', { text });
    };
    const record = knowledgeNote({ freshness: { status: 'current', reasons: [], latest_revision: 2 },
      usable_as_current: true, conclusion_authority: 'research_result' });
    record.result.answer_markdown = '完整正文哨兵\n\n```mermaid\nflowchart LR\n A --> B\n```';
    record.result.conflicts = [{ topic: '来源口径冲突', dimensions: ['口径一', '口径二'], evidence_ids: ['ev-1'] }];
    const { current, calls } = await setupMounted({ requests: {
      knowledge: options => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : record),
    } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    const visible = detailVisibleText(drawer);
    for (const text of ['核心结论', '适合静态流程表达，实时状态需提供映射。', '限定试用', '仍有 1 项记录缺口', '仍未回答的事项（1 项）', '来源口径冲突', '在 Obsidian 打开完整记录']) assert.ok(visible.includes(text), text);
    for (const text of ['完整正文哨兵', '可见图表哨兵', '生产映射尚未验证', '图正确不代表输入判断正确', '是否能试用', '示例通过验证', '运行 validate', '不要推测当前运行状态', '独立验证', '补入真实试跑证据']) assert.equal(visible.includes(text), false, text);
    assert.equal(drawer.querySelector('svg'), null, 'collapsed content must not invoke Mermaid against a hidden layout');
    const answer = await expandAnswer(drawer);
    assert.ok(detailVisibleText(drawer).includes('完整正文哨兵'));
    assert.ok(detailVisibleText(drawer).includes('可见图表哨兵'));
    answer.open = false; answer.dispatchEvent({ type: 'toggle' });
    answer.open = true; answer.dispatchEvent({ type: 'toggle' }); await settle();
    assert.equal(drawer.querySelectorAll('svg').length, 1, 'reopening reuses the rendered Markdown');
    for (const selector of ['.life-knowledge-checks', '.life-knowledge-reuse', '.life-knowledge-sources', '.life-research-unknowns', '.life-research-limitations']) {
      const section = drawer.querySelector(selector); assert.ok(section, selector); section.open = true;
    }
    const expanded = detailVisibleText(drawer);
    for (const text of ['生产映射尚未验证', '图正确不代表输入判断正确', '不会自动成为你的待办', '不表示系统已安排补查', '是否能试用', '示例通过验证', '运行 validate', '不要推测当前运行状态', '独立验证', '补入真实试跑证据']) assert.ok(expanded.includes(text), text);
    const source = drawer.querySelector('.life-knowledge-evidence'); source.open = true;
    assert.ok(detailVisibleText(drawer).includes('doctor 返回成功'));
    assert.equal(source.querySelector('a').getAttribute('href'), 'https://example.org/evidence');
    assert.ok(calls.every(call => !call.method || call.method === 'GET'));
    drawer.querySelector('.life-cosmos-drawer-close').click();
    assert.ok(markdownComponents.every(component => component.unloads === 1));
  });

  it('long summaries preserve every qualifier without inventing a historical label or a local short answer', async () => {
    const summary = '公开结果仅支持该实验条件下的比较。'.repeat(32) + '不能据此断言它在本地部署中更优。';
    const record = knowledgeNote({ revision: 1, usable_as_current: true, conclusion_authority: 'research_result',
      freshness: { status: 'current', reasons: [], latest_revision: 1 },
      result: { summary, recommendation: '先看实验边界', unknowns: [], limitations: [], answer_markdown: '' } });
    const { current } = await setupMounted({ requests: { knowledge: options => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : record) } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    const card = drawer.querySelector('.life-research-conclusion');
    assert.equal(card.querySelector('p').textContent, summary);
    assert.ok(detailVisibleText(drawer).includes('不能据此断言它在本地部署中更优。'));
    assert.ok(card.text.includes('以下保留完整摘要'));
    assert.equal(card.text.includes('历史'), false, 'length alone does not establish a historical version');
    assert.equal(drawer.querySelector('.life-research-unknowns'), null);
    assert.equal(drawer.querySelector('.life-research-limitations'), null);
    assert.equal(drawer.querySelector('.life-knowledge-answer'), null);
  });

  it('knowledge conflict objects show their topic, dimensions and evidence instead of an empty heading', async () => {
    const note = knowledgeNote();
    note.result.conflicts = [{ topic: '<img src=x onerror=alert(1)> 基准不一致',
      dimensions: ['一种设置包含检索', '另一种设置不包含检索'], evidence_ids: ['ev-1', 'ev-2'] }];
    const { current } = await setupMounted({ requests: {
      knowledge: options => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : note),
    } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('基准不一致：一种设置包含检索；另一种设置不包含检索（依据：ev-1、ev-2）'));
    assert.equal(drawer.querySelector('img'), null);
  });

  it('library week/month summaries have their own section, stay outside topic counts, and open through the existing read path', async () => {
    const period = knowledgeNote({ knowledge_id: `period-${'f'.repeat(24)}`, kind: 'period', title: '本周知识总览',
      summary_scope: { scope_id: 'library', title: '全部当前已复核研究' }, period: 'week', start: '2026-09-07', end: '2026-09-13',
      input_summary_only: true,
      content_status: 'theme_summary', conclusion_authority: 'canonical_research', usable_as_current: false,
      freshness: { status: 'current', reasons: [], latest_revision: 2 }, canonical_research: [],
      analysis: { known_structure: [{ statement: '已形成工具验证结构', knowledge_refs: [] }],
        connections: [{ statement: '来源核对与工具试跑可串联', knowledge_refs: [] }],
        gaps: [{ goal: '补齐长期运行证据', basis: '目前只完成短程试跑', knowledge_refs: [] }],
        revisions: [{ previous_statement: '仅有孤立条目', updated_statement: '形成两个相联主题', reason: '新增工具验证记录', knowledge_refs: [] }] },
      goals: [{ goal_id: `goal-${'e'.repeat(24)}`, goal: '补齐长期运行证据', priority: null, collection_mode: 'natural_collection', proactive_research_authorized: false }],
    });
    let periods = [];
    const { current, calls } = await setupMounted({ requests: {
      knowledge: options => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : period),
      periods: () => envelope(periods),
    } });
    const panel = current.mount.querySelector('.life-knowledge-periods');
    assert.match(panel.text, /尚未形成周\/月知识总览/);
    periods = [period]; byAction(current.mount, 'knowledge.refresh').click(); await settle();
    assert.match(panel.text, /本周知识总览/);
    assert.equal(current.mount.querySelectorAll('.life-knowledge-topic').length, 1, 'library scope is not another theme');
    panel.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.match(drawer.text, /全部当前已复核研究/); assert.match(drawer.text, /2026-09-07/);
    assert.match(drawer.text, /本期按全部研究的摘要整理/);
    assert.match(drawer.text, /完整事实依据与适用限制请查看原研究/);
    assert.match(drawer.text, /结构整理/);
    for (const text of ['已形成工具验证结构', '来源核对与工具试跑可串联', '目前只完成短程试跑', '未设优先级 · 自然收集 · 未授权主动研究', '新增工具验证记录']) assert.ok(drawer.text.includes(text), text);
    assert.ok(calls.some(call => call.url.endsWith('/knowledge/'+period.knowledge_id)));
    assert.ok(calls.every(call => call.method === 'GET'));
  });

  it('knowledge validity distinguishes current research, stale history, unreviewed records and structural navigation', async () => {
    const notes = [
      knowledgeNote({ title: '当前研究', freshness: { status: 'current', reasons: [], latest_revision: 2 }, usable_as_current: true, conclusion_authority: 'research_result' }),
      knowledgeNote({ knowledge_id: `knowledge-${'c'.repeat(24)}`, title: '失效研究', freshness: { status: 'stale', reasons: ['源证据已更新'], latest_revision: 3 }, usable_as_current: false, conclusion_authority: 'research_result' }),
      knowledgeNote({ knowledge_id: `knowledge-${'d'.repeat(24)}`, title: '未复核研究', freshness: { status: 'unreviewed', reasons: ['没有复核回执'], latest_revision: 2 }, usable_as_current: false, conclusion_authority: 'research_result' }),
      knowledgeNote({ knowledge_id: `period-${'e'.repeat(24)}`, title: '周期结构', kind: 'period', content_status: 'theme_summary', freshness: { status: 'current', reasons: [], latest_revision: 2 }, usable_as_current: false, conclusion_authority: 'canonical_research', canonical_research: [] }),
    ];
    const { current } = await setupMounted({ requests: { knowledge: options => envelope(options.url.endsWith('/knowledge')
      ? [{ ...knowledgeTopics()[0], notes }] : notes.find(note => options.url.endsWith(note.knowledge_id))) } });
    const rows = current.mount.querySelectorAll('.life-knowledge-note');
    assert.equal(rows.length, 4, '失效和未复核记录仍在目录');
    assert.ok(rows[0].text.includes('当前研究结论'));
    assert.ok(rows[1].text.includes('已失效'));
    assert.ok(rows[2].text.includes('尚未复核'));
    assert.ok(rows[3].text.includes('结构依赖当前'));
    assert.equal(rows[3].text.includes('当前研究结论'), false);
    rows[1].click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('源证据已更新'));
    assert.ok(drawer.text.includes('当前最新修订 3'));
    const history = drawer.querySelector('.life-knowledge-history');
    assert.equal(history.getAttribute('open'), null);
    assert.ok(history.text.includes('适合静态流程表达'));
    assert.equal(drawer.querySelector('textarea'), null);
    assert.equal(drawer.text.includes('等待人工'), false);
  });

  it('derived summaries point to the current source conclusion and limits while retaining their stale original in a fold', async () => {
    const source = knowledgeNote({ knowledge_id: `knowledge-${'c'.repeat(24)}`, title: '源研究新修订',
      freshness: { status: 'current', reasons: [], latest_revision: 2 }, usable_as_current: true, conclusion_authority: 'research_result',
      result: { summary: '新研究撤销原绝对判断', recommendation: '仅有限场景可用', unknowns: ['当前部署情况未知'],
        agent_usage: { when_to_use: '有版本证据时', steps: ['核对来源版本'], limitations: ['不得把缺少证据说成无需行动'] } } });
    const derived = knowledgeNote({ title: '旧主题结构', content_status: 'theme_summary',
      freshness: { status: 'stale', reasons: ['依赖研究已修订'], latest_revision: 2 }, usable_as_current: false, conclusion_authority: 'canonical_research',
      result: { summary: '旧整理的绝对判断', recommendation: '旧整理的行动建议', unknowns: [] }, canonical_research: [source] });
    const { current, calls } = await setupMounted({ requests: { knowledge: options => envelope(options.url.endsWith('/knowledge')
      ? [{ ...knowledgeTopics()[0], notes: [derived] }] : options.url.endsWith(source.knowledge_id) ? source : derived) } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    const canonical = drawer.querySelector('.life-knowledge-canonical');
    const historical = drawer.querySelector('.life-knowledge-history');
    assert.ok(canonical.text.includes('新研究撤销原绝对判断'));
    assert.ok(detailVisibleText(drawer).includes('新研究撤销原绝对判断'));
    assert.equal(detailVisibleText(drawer).includes('旧整理的绝对判断'), false);
    assert.equal(historical.querySelector('.life-research-conclusion').querySelector('h3').textContent, '结构整理摘要');
    assert.ok(canonical.text.includes('当前部署情况未知'));
    assert.ok(canonical.text.includes('不得把缺少证据说成无需行动'));
    assert.equal(canonical.text.includes('旧整理的绝对判断'), false);
    assert.ok(historical.text.includes('旧整理的绝对判断'));
    assert.equal(historical.getAttribute('open'), null);
    assert.ok(canonical.parent.children.indexOf(canonical) < canonical.parent.children.indexOf(historical));
    [...canonical.querySelectorAll('button')].find(el => el.textContent === '查看源研究完整依据').click(); await settle();
    assert.ok(calls.some(call => call.url.endsWith(source.knowledge_id)));
    assert.ok(drawer.text.includes('新研究撤销原绝对判断'));
    assert.equal(drawer.text.includes('旧整理的绝对判断'), false);
  });

  it('knowledge search only calls current usable research matches current while the full index retains history', async () => {
    const currentNote = knowledgeNote({ freshness: { status: 'current', reasons: [], latest_revision: 2 }, usable_as_current: true, conclusion_authority: 'research_result' });
    const oldNote = knowledgeNote({ knowledge_id: `knowledge-${'c'.repeat(24)}`, title: '未知有效性旧记录' });
    const { current } = await setupMounted({ requests: { knowledge: options => envelope(options.url.includes('?q=')
      ? [oldNote, currentNote] : [{ ...knowledgeTopics()[0], notes: [oldNote, currentNote] }]) } });
    const panel = current.mount.querySelector('.life-knowledge-catalogue');
    const input = panel.querySelector('.life-knowledge-search');
    input.value = '适配';
    [...panel.querySelectorAll('button')].find(el => el.textContent === '搜索').click(); await settle();
    assert.equal(panel.querySelectorAll('.life-knowledge-note').length, 1);
    assert.equal(panel.text.includes('未知有效性旧记录'), false);
    input.value = ''; input.dispatchEvent({ type: 'input' });
    assert.equal(panel.querySelectorAll('.life-knowledge-note').length, 2);
    assert.ok(panel.text.includes('有效性未确认'));
  });

  it('a missing canonical research read never lets a structural summary impersonate the answer', async () => {
    const note = knowledgeNote({ content_status: 'theme_summary', conclusion_authority: 'canonical_research',
      freshness: { status: 'stale', reasons: ['原研究无法读取'], latest_revision: 2 }, usable_as_current: false, canonical_research: [] });
    const { current } = await setupMounted({ requests: { knowledge: options => envelope(options.url.endsWith('/knowledge')
      ? [{ ...knowledgeTopics()[0], notes: [note] }] : note) } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('当前源研究未能读取'));
    assert.ok(drawer.querySelector('.life-knowledge-history').text.includes('结构整理摘要'));
    assert.equal(drawer.querySelector('textarea'), null);
  });

  it('native whole-container postprocessing does not rewrap an existing untrusted Mermaid source', async () => {
    const calls = [];
    // Obsidian 1.13.7 app.js: render appends new Markdown, then runs processors on the entire supplied container.
    // Its untrusted Mermaid guard keeps code.language-mermaid inside guard-source, so reprocessing it nests another guard.
    markdownRenderHandler = async (app, markdown, target, sourcePath, component) => {
      calls.push({ markdown, target, component });
      if (markdown.startsWith('```mermaid')) target.createEl('pre').createEl('code', { cls: 'language-mermaid', text: markdown });
      else target.createEl('p', { text: markdown });
      for (const code of [...target.querySelectorAll('.language-mermaid')]) {
        const pre = code.parent; const parent = pre.parent; const text = code.textContent;
        const wrapper = parent.createDiv({ cls: 'mermaid-wrapper is-guarded' });
        pre.remove();
        wrapper.createEl('p', { text: '在此仓库中显示 Mermaid 图表？' });
        wrapper.createDiv({ cls: 'mermaid-guard-source' }).createEl('pre').createEl('code', { cls: 'language-mermaid', text });
      }
    };
    const markdown = '# 研究判断\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n限制：未验证外部复现';
    const { current } = await setupMounted({ requests: {
      knowledge: options => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics()
        : knowledgeNote({ result: { summary: '有限结论', answer_markdown: markdown } })),
    } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    await expandAnswer(drawer);
    assert.equal(drawer.querySelectorAll('.mermaid-wrapper').length, 1, 'one source must produce one native trust guard');
    assert.equal(drawer.querySelectorAll('.language-mermaid').length, 1, 'untrusted source remains readable');
    assert.ok(drawer.text.includes('限制：未验证外部复现'));
    assert.equal(calls.filter(call => call.markdown.startsWith('```mermaid')).length, 1);
    assert.equal(new Set(calls.map(call => call.component)).size, 1, 'all segments share one lifecycle');
    drawer.querySelector('.life-cosmos-drawer-close').click();
    assert.equal(calls[0].component.unloads, 1);
  });

  it('图文仅交给原生核心 Markdown 与安全 Mermaid，其他插件代码块保留文本，关闭和卸载释放组件', async () => {
    const markdown = '# 判断\n\n![配图](https://example.org/image.png)\n![[私人文档]]\n<img onerror=alert(1)>\n\n```dataviewjs\ndv.io.load("secret")\n```\n\n`= dangerous()`\n\n```mermaid\ngraph TD\n A[输入] --> B[判断]\n```'.replaceAll('\\n', '\n');
    const renderCalls = [];
    markdownRenderHandler = async (_app, text, host, sourcePath) => {
      renderCalls.push({ text, sourcePath });
      if (text.startsWith('```mermaid')) host.createEl('svg', { text: '原生图表输出' });
      else host.createEl('p', { text });
    };
    const { current } = await setupMounted({ requests: {
      knowledge: (options) => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics()
        : knowledgeNote({ result: { summary: '结论', answer_markdown: markdown } })),
    } });
    const note = current.mount.querySelector('.life-knowledge-note');
    note.click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    await expandAnswer(drawer);
    assert.ok(drawer.querySelector('svg'), '把原生 renderer 的 SVG 结果放入正文，不在测试中声称真的跑过 Mermaid 引擎');
    assert.ok(drawer.querySelector('pre').text.includes('dv.io.load'));
    assert.ok(renderCalls.every(({ text }) => !text.includes('```dataviewjs')));
    assert.ok(renderCalls[0].text.includes('&lt;img'));
    assert.ok(renderCalls[0].text.includes('\\!['));
    assert.ok(renderCalls[0].text.includes('\\[\\['));
    assert.ok(renderCalls.every(({ text }) => !text.includes('dangerous()')));
    assert.ok(drawer.querySelectorAll('pre').some(node => node.text.includes('`= dangerous()`')));
    assert.ok(drawer.querySelectorAll('pre').every(node => node.querySelector('code') === null), 'activity is literal even if a parent later scans code nodes');
    assert.ok(renderCalls.every(({ sourcePath }) => sourcePath === knowledgeNote().path));
    const first = markdownComponents.at(-1);
    assert.equal(first.unloads, 0);
    drawer.querySelector('.life-cosmos-drawer-close').click();
    assert.equal(first.unloads, 1);
    note.click(); await settle();
    await expandAnswer(drawer);
    const second = markdownComponents.at(-1);
    current.mount.querySelector('.life-cosmos-home').__lifeCosmosDispose();
    assert.equal(second.unloads, 1);
  });

  it('不安全 Mermaid 作为代码展示，普通 Vault 链接走原文路径，命令协议不生成可点击入口', async () => {
    const renderCalls = [];
    markdownRenderHandler = async (_app, text, host) => {
      renderCalls.push(text);
      const local = host.createEl('a', { text: '关联知识' }); local.setAttribute('href', '相关.md');
      const command = host.createEl('a', { text: '不可执行' }); command.setAttribute('href', 'obsidian://command');
    };
    const markdown = '正文\n```mermaid\ngraph TD\nclick A "javascript:alert(1)"\n```\n```mermaid\nflowchart LR\nA@{ img: "file:///private.png" }\n```\n```mermaid\n%%{init: {securityLevel: "loose"}}%%\ngraph TD\nA --> B\n```'.replaceAll('\\n', '\n');
    const { current, harness } = await setupMounted({ requests: {
      knowledge: (options) => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics()
        : knowledgeNote({ result: { summary: '结论', answer_markdown: markdown } })),
    } });
    const routes = [];
    harness.plugin.app.workspace.openLinkText = (...args) => { routes.push(args); };
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    await expandAnswer(drawer);
    assert.ok(renderCalls.every((text) => !text.includes('```mermaid')));
    assert.ok(drawer.querySelector('pre').text.includes('javascript:alert'));
    assert.equal(drawer.querySelectorAll('pre').length, 3, '交互、本地图片和配置指令各自降级');
    const links = drawer.querySelectorAll('a');
    assert.equal(links.find((a) => a.text === '不可执行').getAttribute('href'), null);
    links.find((a) => a.text === '关联知识').click(); await settle();
    assert.deepEqual(routes, [['相关.md', knowledgeNote().path, false]]);
    drawer.querySelector('.life-cosmos-drawer-close').click();
  });

  it('抽屉关闭后的迟到图文不落地，渲染失败保留原始完整回答', async () => {
    const gate = deferred();
    markdownRenderHandler = async (_app, _text, host) => { await gate.promise; host.createEl('svg', { text: '迟到图表' }); };
    const { current } = await setupMounted({ requests: {
      knowledge: (options) => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : knowledgeNote()),
    } });
    const note = current.mount.querySelector('.life-knowledge-note');
    note.click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    await expandAnswer(drawer);
    drawer.querySelector('.life-cosmos-drawer-close').click();
    gate.resolve(); await settle();
    assert.equal(drawer.querySelector('svg'), null);
    assert.ok(markdownComponents.every((component) => component.unloads === 1));
    markdownRenderHandler = async () => { throw new Error('render failed'); };
    note.click(); await settle();
    await expandAnswer(drawer);
    assert.ok(drawer.text.includes('完整回答：<img'));
    assert.ok(drawer.text.includes('图文暂时无法渲染'));
    assert.ok(markdownComponents.every((component) => component.unloads === 1));
  });

  function detailVisibleText(el) {
    const children = el.tagName === 'DETAILS' && !el.open && el.getAttribute('open') === null
      ? el.children.filter(child => child.tagName === 'SUMMARY') : el.children;
    return `${el.textContent} ${children.map(detailVisibleText).join(' ')}`;
  }

  it('completed detail prioritizes the exact conclusion while gaps, scope and execution details remain optional', async () => {
    const item = concludedAlignment(); item.source_origin = 'raw_capture';
    item.reasoning = '原提案技术说明哨兵';
    item.effective_execution_scope = effectiveScope({ observed_model_provider: 'kimi_subscription', knowledge_publication_status: 'recorded' });
    item.execution.subscription_selected = true;
    item.execution.research_progress = { ...item.execution.research_progress, model_provider: 'kimi_subscription', agent_invocations: 3 };
    item.execution.result = { ...item.execution.result, summary: '可以复用的结论哨兵',
      limitations: ['仅验证固定公开版本'], unknowns: ['本地映射仍未验证'],
      conflicts: [{ topic: '成本测量口径冲突', dimensions: ['供应商自评', '外部核对'], evidence_ids: ['ev-public-source'] }] };
    const { current, calls } = await setupMounted({ requests: { alignments: () => envelope([item]), reviews: () => envelope([]) } });
    current.mount.querySelector('.life-frag-panorama-row').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    const visible = detailVisibleText(drawer);
    assert.equal(drawer.querySelector('.life-cosmos-intent-row').children.filter(child => child.tagName === 'STRONG').length, 0, 'the single detail keeps its title only in the drawer header');
    assert.ok(drawer.text.includes('研究 · 结论与下一步'));
    for (const text of ['核心结论', '可以复用的结论哨兵', '仍有 1 项记录缺口', '仍未回答的事项（1 项）', '结论的适用范围', '成本测量口径冲突', '查看完整知识与依据']) assert.ok(visible.includes(text), text);
    for (const text of ['仅验证固定公开版本', '本地映射仍未验证', '原提案技术说明哨兵', '运行标识', 'agent_invocation', '成本上限', '原始公开碎片直接研究']) assert.equal(visible.includes(text), false, text);
    drawer.querySelector('.life-research-unknowns').open = true;
    drawer.querySelector('.life-research-limitations').open = true;
    for (const text of ['仅验证固定公开版本', '本地映射仍未验证', '不会自动成为你的待办', '不表示系统已安排补查']) assert.ok(detailVisibleText(drawer).includes(text), text);
    assert.ok(drawer.querySelector('.fragment-execution-details').text.includes('Kimi 月套餐'));
    assert.ok(drawer.querySelector('.fragment-input-origin').text.includes('原始公开碎片直接研究'));
    assert.equal(drawer.querySelector('textarea'), null);
    assert.ok(calls.every(call => !call.method || call.method === 'GET'));
  });

  it('ordinary completed follow-up requires disclosure before showing a new goal form', async () => {
    const item = alignmentPayload();
    item.execution.route = 'direct'; item.route = 'direct';
    const { current } = await setupMounted({ requests: { alignments: () => envelope([item]), reviews: () => envelope([]) } });
    current.mount.querySelector('.life-frag-panorama-row').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    const disclosure = drawer.querySelector('.fragment-intent-continuation');
    assert.ok(disclosure);
    assert.ok(disclosure.querySelector('textarea'));
    assert.equal(detailVisibleText(drawer).includes('将创建子 episode'), false);
    disclosure.open = true;
    assert.ok(detailVisibleText(drawer).includes('将创建子 episode'));
  });

  it('authorization and failed review notices stay visible without opening execution detail', async () => {
    for (const [stage, text] of [['awaiting_authorization', '确认是否授权模型核验'], ['independent_review_failed', '证据复核未完成']]) {
      const item = alignmentPayload();
      item.execution.status = 'blocked';
      item.execution.research_progress = { stage, collected_sources: 2, model_calls: 1 };
      const { current } = await setupMounted({ requests: { alignments: () => envelope([item]), reviews: () => envelope([]) } });
      current.mount.querySelector('.life-frag-panorama-row').click(); await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      assert.ok(detailVisibleText(drawer).includes(text), text);
      assert.equal(drawer.querySelector('.fragment-execution-details'), null);
    }
  });

  it('automatic publication wins over legacy review state and opens the complete result without a goal or Graph prompt', async () => {
    const item = alignmentPayload();
    item.execution.research_progress = { stage: 'synthesized', cognitive: 'synthesized', collected_sources: 2, model_calls: 1 };
    item.execution.knowledge_publication = { knowledge_id: knowledgeNote().knowledge_id, revision: 2,
      path: knowledgeNote().path, result_digest: item.execution.result_digest, publication_source: 'system_policy' };
    item.execution.result = { ...item.execution.result, unknowns: ['动态映射未验证'], answer_markdown: '# 完整判断\n限制仍在' };
    item.execution.graph_escalation = { status: 'proposed' };
    const { current } = await setupMounted({ requests: {
      alignments: () => envelope([item]), reviews: () => envelope([reviewCandidatePayload()]),
      knowledge: (options) => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : knowledgeNote()),
    } });
    const row = current.mount.querySelector('.life-frag-panorama-row');
    assert.ok(row.text.includes('结论已自动沉淀'));
    assert.ok(!row.text.includes('等待人工确认'), '研究记录不要求审批已自动沉淀结论');
    row.click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('动态映射未验证'));
    assert.ok(drawer.text.includes('完整判断'));
    assert.equal(drawer.querySelector('textarea'), null);
    assert.equal(drawer.text.includes('创建 Graph 研究 Run'), false);
    assert.equal(drawer.text.includes('尚未成为知识资产'), false);
    [...drawer.querySelectorAll('button')].find((el) => el.textContent === '查看完整知识与依据').click(); await settle();
    assert.ok(drawer.text.includes('补入真实试跑证据'));
  });


  function addOrganizedFragment(current) {
    const card = current.dashboard.createEl('article', { cls: 'life-organized-card' });
    card.setAttribute('data-source-fragment', 'frag-1.md');
    card.createEl('strong', { text: '核验 MiniMax H3' });
    card.createEl('a', { text: '打开文档' }).setAttribute('data-path', '散记/已整理碎片/frag-1.md');
  }

  function concludedAlignment() {
    const item = alignmentPayload();
    item.execution.research_progress = { stage: 'synthesized', cognitive: 'synthesized', collected_sources: 2, model_calls: 2 };
    item.execution.knowledge_publication = { knowledge_id: knowledgeNote().knowledge_id, revision: 2,
      path: knowledgeNote().path, result_digest: item.execution.result_digest, publication_source: 'system_policy' };
    return item;
  }

  it('raw public capture follows real research without an organized note, including later publication', async (t) => {
    let now = Date.now(); t.mock.method(Date, 'now', () => now);
    const running = alignmentPayload({ source_origin: 'raw_capture', goal: '完整的原始问题。'.repeat(500) });
    running.execution.status = 'running'; running.execution.result = null;
    running.execution.source_origin = 'raw_capture';
    running.execution.research_progress = { stage: 'researching', collected_sources: 0, model_calls: 1 };
    let items = [];
    const { current, calls, harness } = await setupMounted({ requests: { alignments: () => envelope(items) }, beforeMount: fixture => {
      const raw = fixture.dashboard.querySelector('.life-capture-card');
      raw.querySelectorAll('a').find(link => link.textContent === '查看整理结果').remove();
    } });
    let row = current.mount.querySelector('.life-frag-panorama-row');
    assert.match(row.text, /尚未确认接管状态/);
    assert.equal(row.text.includes('整理进行中'), false);
    const clock = win.state.intervals.find(timer => timer.ms === 1000 && !timer.cleared);
    items = [running]; now += 30001; clock.fn(); await settle();
    row = current.mount.querySelector('.life-frag-panorama-row');
    assert.match(row.text, /系统正在研究/); assert.doesNotMatch(row.text, /已整理/);
    assert.equal(row.text.includes('已整理'), false, 'raw execution does not create an organized fact');
    row.click(); await settle();
    let drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.match(drawer.text, /原始公开碎片直接研究/); assert.match(drawer.text, /系统正在研究/);
    assert.ok(drawer.querySelectorAll('button').some(button => button.textContent === '打开原始记录'));
    assert.equal(drawer.text.includes('打开整理文档'), false);
    drawer.querySelector('.life-cosmos-drawer-close').click();
    const published = concludedAlignment(); published.source_origin = 'raw_capture'; items = [published];
    now += 30001; clock.fn(); await settle();
    row = current.mount.querySelector('.life-frag-panorama-row');
    assert.match(row.text, /结论已自动沉淀/); assert.equal(row.text.includes('已整理'), false);
    row.click(); await settle(); drawer = current.mount.querySelector('.life-cosmos-drawer');
    drawer.querySelectorAll('button').find(button => button.textContent === '打开原始记录').click(); await settle();
    assert.deepEqual(harness.openedLinks, ['Notes/散记/碎片想法/frag-1.md']);
    assert.ok(calls.every(call => call.method === 'GET'));
  });

  it('a raw research detail survives Dataview replacement while its read-only refresh is still pending', async () => {
    const item = concludedAlignment(); item.source_origin = 'raw_capture';
    let gate = null;
    const { current, home, harness, calls } = await setupMounted({ requests: { alignments: () => gate ? gate.promise : envelope([item]) }, beforeMount: fixture => {
      fixture.dashboard.querySelector('.life-capture-card').querySelectorAll('a').find(link => link.textContent === '查看整理结果').remove();
    } });
    gate = deferred();
    current.mount.querySelector('.life-frag-panorama-row').click(); await settle();
    assert.match(current.mount.querySelector('.life-cosmos-drawer').text, /正在读取当前研究状态/);
    current.root.remove();
    const replacement = buildHomepageRoot(home);
    replacement.dashboard.querySelector('.life-capture-card').querySelectorAll('a').find(link => link.textContent === '查看整理结果').remove();
    const remount = harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    gate.resolve(envelope([item])); await remount; await settle(); await settle();
    const drawer = replacement.mount.querySelector('.life-cosmos-drawer');
    assert.equal(replacement.mount.querySelector('.life-cosmos-drawer-shell').getAttribute('aria-hidden'), 'false');
    assert.match(drawer.text, /结论已自动沉淀/);
    assert.match(drawer.text, /原始公开碎片直接研究/);
    assert.equal(drawer.querySelector('textarea'), null);
    assert.ok(calls.every(call => call.method === 'GET'), 'restore neither proposes nor decides again');
    drawer.querySelector('.life-cosmos-drawer-close').click();
    replacement.root.remove(); const again = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(again.root); await settle();
    assert.equal(again.mount.querySelector('.life-cosmos-drawer-shell').getAttribute('aria-hidden'), 'true', 'intentional close remains closed');
  });

  it('a raw card without alignment shows its actual scanner outcome in both panorama and detail', async () => {
    const { current } = await setupMounted({ requests: { alignments: () => ({ status: 200, text: JSON.stringify({
      contract_version: '2', data: [], meta: { auto_propose: [{ fragment_id: 'frag-1', outcome: 'skipped', reason: 'not_approved' }] },
    }) }) }, beforeMount: fixture => {
      fixture.dashboard.querySelector('.life-capture-card').querySelectorAll('a').find(link => link.textContent === '查看整理结果').remove();
    } });
    const row = current.mount.querySelector('.life-frag-panorama-row'); assert.match(row.text, /未进入 Loop/);
    row.click(); await settle(); const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.match(drawer.text, /未进入 Loop/);
    assert.equal(drawer.text.includes('系统正在研究'), false); assert.equal(drawer.text.includes('打开整理文档'), false);
  });

  it('visible progress reaches running and published from no alignment without clicks or Graph polling', async (t) => {
    let now = Date.now(); t.mock.method(Date, 'now', () => now);
    let items = [];
    const { current, calls } = await setupMounted({ requests: { alignments: () => envelope(items) }, beforeMount: addOrganizedFragment });
    const graphCount = () => calls.filter(c => c.url.includes('/graph/')).length;
    const originalGraphCount = graphCount();
    const clock = win.state.intervals.find(timer => timer.ms === 1000 && !timer.cleared);
    const search = current.mount.querySelector('.life-frag-panorama-search');
    search.value = '核验'; search.dispatchEvent({ type: 'input' }); search.focus();
    current.root.scrollTop = 420;
    current.mount.querySelector('.life-frag-panorama').scrollLeft = 170;
    assert.ok(current.mount.querySelector('.life-frag-panorama-row').text.includes('尚未确认接管状态'));
    const running = alignmentPayload(); running.execution.status = 'running'; running.execution.result = null;
    running.execution.research_progress = { stage: 'researching', collected_sources: 0, model_calls: 1 };
    items = [running]; now += 30001; clock.fn(); await settle();
    assert.ok(current.mount.querySelector('.life-frag-panorama-row').text.includes('系统正在研究'));
    assert.equal(current.mount.querySelector('.life-frag-panorama-search'), search, 'same input node, with its current event listener');
    assert.equal(search.value, '核验'); assert.equal(current.root.scrollTop, 420);
    assert.equal(current.mount.querySelector('.life-frag-panorama').scrollLeft, 170, 'horizontal stage-table scroll survives');
    items = [concludedAlignment()]; now += 30001; clock.fn(); await settle();
    assert.ok(current.mount.querySelector('.life-frag-panorama-row').text.includes('结论已自动沉淀'));
    assert.equal(graphCount(), originalGraphCount, 'background progress never refreshes Graph/control queues');
    assert.equal(calls.filter(c => c.url.includes('/alignments')).length, 3);
    assert.ok(calls.every(c => c.method === 'GET'), 'automatic refresh is exclusively read-only');
    search.value = '不存在'; search.dispatchEvent({ type: 'input' });
    assert.equal(current.mount.querySelector('.life-frag-panorama-row').getAttribute('hidden'), 'hidden', 'reused input filters current rows, not a detached old list');
  });

  it('projection refresh is single-flight, hidden sessions make no requests, foreground resumes, and unload ignores late responses', async (t) => {
    let now = Date.now(); t.mock.method(Date, 'now', () => now);
    let response = () => envelope([]);
    const { doc, home, current, harness, calls } = await setupMounted({ requests: { alignments: () => response() }, beforeMount: addOrganizedFragment });
    const count = () => calls.filter(c => c.url.includes('/alignments')).length;
    const clock = win.state.intervals.find(timer => timer.ms === 1000 && !timer.cleared);
    const gate = deferred(); response = () => gate.promise;
    now += 30001; clock.fn(); clock.fn(); harness.eventHandlers['active-leaf-change'](); await settle();
    assert.equal(count(), 2, 'tick and foreground notification share the in-flight list');
    doc.hidden = true; doc.dispatchEvent({ type: 'visibilitychange' }); now += 60000; clock.fn(); await settle();
    assert.equal(count(), 2);
    const oldRow = current.mount.querySelector('.life-frag-panorama-row');
    gate.resolve(envelope([concludedAlignment()])); await settle();
    assert.equal(current.mount.querySelector('.life-frag-panorama-row'), oldRow, 'late hidden response does not repaint');
    response = () => envelope([concludedAlignment()]); doc.hidden = false;
    doc.dispatchEvent({ type: 'visibilitychange' }); await settle();
    assert.equal(count(), 3, 'foreground fetch does not wait another 30 seconds');
    assert.ok(current.mount.querySelector('.life-frag-panorama-row').text.includes('结论已自动沉淀'));
    home.setAttribute('hidden', 'hidden'); now += 30001; clock.fn(); harness.eventHandlers['active-leaf-change'](); await settle();
    assert.equal(count(), 3, 'an inactive/hidden homepage also makes no requests');
    home.removeAttribute('hidden');
    const late = deferred(); response = () => late.promise; now += 30001; clock.fn(); await settle();
    assert.equal(count(), 4);
    harness.cleanupCallbacks.forEach(fn => fn()); late.resolve(envelope([])); await settle();
    const afterUnload = current.mount.text;
    doc.dispatchEvent({ type: 'visibilitychange' }); clock.fn(); await settle();
    assert.equal(count(), 4); assert.equal(current.mount.text, afterUnload);
    assert.equal(win.liveIntervals(), 0);
  });

  it('unchanged projections do not redraw and changed data preserves an open drawer draft and scroll', async (t) => {
    let now = Date.now(); t.mock.method(Date, 'now', () => now);
    let item = alignmentPayload();
    const { current, doc } = await setupMounted({ requests: {
      alignments: () => envelope([item]), knowledge: () => envelope(knowledgeTopics()),
    } });
    const clock = win.state.intervals.find(timer => timer.ms === 1000 && !timer.cleared);
    let row = current.mount.querySelector('.life-frag-panorama-row');
    const theme = current.mount.querySelector('.life-knowledge-topic'); theme.setAttribute('open', 'open');
    const search = current.mount.querySelector('.life-knowledge-search'); search.value = '尚未提交的搜索';
    row.click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    const draft = drawer.querySelector('textarea'); assert.ok(draft);
    draft.value = '尚未提交的新目标'; draft.focus(); drawer.scrollTop = 210;
    const body = drawer.querySelector('.life-cosmos-drawer-body');
    now += 30001; clock.fn(); await settle();
    assert.equal(current.mount.querySelector('.life-frag-panorama-row'), row, 'unchanged list retains DOM identity');
    assert.equal(current.mount.querySelector('.life-knowledge-topic'), theme, 'unchanged catalogue retains expanded DOM identity');
    assert.equal(drawer.querySelector('.life-cosmos-projection-update'), null);
    item = concludedAlignment(); now += 30001; clock.fn(); await settle();
    assert.equal(drawer.querySelector('.life-cosmos-drawer-body'), body);
    assert.equal(drawer.querySelector('textarea'), draft); assert.equal(draft.value, '尚未提交的新目标');
    assert.equal(doc.activeElement, draft); assert.equal(drawer.scrollTop, 210);
    assert.equal(search.value, '尚未提交的搜索'); assert.equal(theme.getAttribute('open'), 'open');
    assert.match(drawer.querySelector('.life-cosmos-projection-update').text, /已有更新.*保留打开时的内容与输入/);
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').getAttribute('aria-hidden'), 'false');
  });

  it('research search survives workbench filtering, opening knowledge r2, automatic refresh and a native homepage remount', async (t) => {
    let now = Date.now(); t.mock.method(Date, 'now', () => now);
    const archify = concludedAlignment(); archify.title = 'Archify 的适配判断';
    const blocked = id => {
      const item = alignmentPayload({ alignment_id: 'align-' + id, fragment_id: id, title: '系统阻塞 ' + id });
      item.execution.fragment_id = id;
      item.execution.research_progress = { stage: 'synthesis_disabled', blocker: 'synthesis_disabled', collected_sources: 1, model_calls: 0 };
      return item;
    };
    let items = [archify, blocked('one'), blocked('two')];
    const reading = knowledgeNote({ result: { ...knowledgeNote().result, summary: '正在阅读的第二版结论' } });
    const { current, home, harness, calls } = await setupMounted({ requests: {
      alignments: () => envelope(items),
      knowledge: options => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : reading),
    } });
    const filter = (mount, id) => [...mount.querySelectorAll('[data-fragment-filter]')].find(el => el.getAttribute('data-fragment-filter') === id);
    const visible = mount => [...mount.querySelectorAll('.life-frag-panorama-row')].filter(el => el.getAttribute('hidden') === null);
    [...current.mount.querySelectorAll('.life-workbench-stat')].find(el => el.text.includes('系统阻塞')).click();
    assert.equal(visible(current.mount).length, 2);
    filter(current.mount, 'all').click();
    const originalSearch = current.mount.querySelector('.life-frag-panorama-search');
    originalSearch.value = 'Archify'; originalSearch.dispatchEvent({ type: 'input' });
    assert.equal(visible(current.mount).length, 1); visible(current.mount)[0].click(); await settle();
    [...current.mount.querySelector('.life-cosmos-drawer').querySelectorAll('button')].find(el => el.textContent === '查看完整知识与依据').click(); await settle();
    assert.match(current.mount.querySelector('.life-cosmos-drawer').text, /正在阅读的第二版结论/);
    items = items.map(item => ({ ...item, sequence: item.sequence + 1 }));
    const clock = win.state.intervals.find(timer => timer.ms === 1000 && !timer.cleared);
    now += 30001; clock.fn(); await settle();
    assert.equal(current.mount.querySelector('.life-frag-panorama-search'), originalSearch);
    assert.equal(originalSearch.value, 'Archify'); assert.equal(visible(current.mount).length, 1);
    assert.match(current.mount.querySelector('.life-cosmos-drawer').text, /正在阅读的第二版结论/);
    current.root.remove(); const replacement = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    const search = replacement.mount.querySelector('.life-frag-panorama-search');
    assert.notEqual(search, originalSearch, 'new mount owns its own input DOM');
    assert.equal(search.value, 'Archify', 'the user query survives a Dataview replacement');
    assert.equal(filter(replacement.mount, 'all').getAttribute('aria-pressed'), 'true');
    assert.equal(visible(replacement.mount).length, 1);
    assert.match(visible(replacement.mount)[0].text, /Archify/);
    assert.match(replacement.mount.querySelector('.life-cosmos-drawer').text, /正在阅读的第二版结论/);
    assert.ok(calls.some(call => call.url.endsWith('/knowledge/' + reading.knowledge_id + '?revision=2')));
    assert.ok(calls.every(call => !call.method || call.method === 'GET'), 'preserving view state never writes research or preferences');
  });

  it('a non-default research filter and cleared query survive remount without sharing DOM or selection with another homepage', async () => {
    const one = alignmentPayload({ title: '保持阻塞筛选' });
    one.execution.research_progress = { stage: 'synthesis_disabled', blocker: 'synthesis_disabled', collected_sources: 1, model_calls: 0 };
    const { current, home, harness } = await setupMounted({ requests: { alignments: () => envelope([one]) } });
    const filter = (mount, id) => [...mount.querySelectorAll('[data-fragment-filter]')].find(el => el.getAttribute('data-fragment-filter') === id);
    filter(current.mount, 'blocked').click();
    const originalSearch = current.mount.querySelector('.life-frag-panorama-search');
    originalSearch.value = '保持'; originalSearch.dispatchEvent({ type: 'input' });
    current.root.remove(); let replacement = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    let search = replacement.mount.querySelector('.life-frag-panorama-search');
    assert.equal(search.value, '保持'); assert.equal(filter(replacement.mount, 'blocked').getAttribute('aria-pressed'), 'true');
    search.value = ''; search.dispatchEvent({ type: 'input' });
    replacement.root.remove(); replacement = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    search = replacement.mount.querySelector('.life-frag-panorama-search');
    assert.equal(search.value, '', 'an explicitly cleared query must not resurrect');
    assert.equal(filter(replacement.mount, 'blocked').getAttribute('aria-pressed'), 'true');
    const other = buildHomepageRoot(home.ownerDocument.body.createDiv({ cls: 'my-life-homepage-view' }));
    await harness.plugin.mountHomepageCosmos(other.root); await settle();
    assert.equal(other.mount.querySelector('.life-frag-panorama-search').value, '');
    assert.equal(filter(other.mount, 'all').getAttribute('aria-pressed'), 'true', 'another homepage has independent research selection');
    assert.notEqual(other.mount.querySelector('.life-frag-panorama-search'), search);
    originalSearch.value = 'detached stale input'; originalSearch.dispatchEvent({ type: 'input' });
    assert.equal(search.value, '', 'detached input events cannot mutate the current view');
    replacement.root.remove(); const final = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(final.root); await settle();
    assert.equal(final.mount.querySelector('.life-frag-panorama-search').value, '', 'detached input cannot corrupt the next remount either');
    assert.equal(filter(final.mount, 'blocked').getAttribute('aria-pressed'), 'true');
  });

  it('catalogue revisions preserve theme expansion and the current search input', async (t) => {
    let now = Date.now(); t.mock.method(Date, 'now', () => now);
    let topics = knowledgeTopics();
    const { current } = await setupMounted({ requests: { knowledge: options => envelope(options.url.includes('?q=') ? [knowledgeNote()] : topics) } });
    const clock = win.state.intervals.find(timer => timer.ms === 1000 && !timer.cleared);
    current.mount.querySelector('.life-knowledge-topic').setAttribute('open', 'open');
    topics = [{ ...topics[0], notes: [{ ...knowledgeNote(), revision: 3 }] }];
    now += 30001; clock.fn(); await settle();
    assert.equal(current.mount.querySelector('.life-knowledge-topic').getAttribute('open'), 'open');
    assert.match(current.mount.querySelector('.life-knowledge-note').text, /修订 3/);
    const input = current.mount.querySelector('.life-knowledge-search'); input.value = '工作流'; input.focus();
    topics = [{ ...topics[0], notes: [{ ...knowledgeNote(), revision: 4 }] }];
    now += 30001; clock.fn(); await settle();
    assert.equal(current.mount.querySelector('.life-knowledge-search'), input);
    assert.equal(input.value, '工作流'); assert.equal(globalThis.document.activeElement, input);
  });

  it('knowledge expansion survives changed 30s projections and a Dataview replacement, keyed by theme ID rather than title', async (t) => {
    let now = Date.now(); t.mock.method(Date, 'now', () => now);
    let topics = knowledgeTopics();
    const { current, home, harness, calls } = await setupMounted({ requests: { knowledge: options => envelope(
      options.url.endsWith('/knowledge') ? topics : knowledgeNote({ revision: 3 })) } });
    const themeId = topics[0].topic_id;
    current.mount.querySelector('.life-knowledge-topic').setAttribute('open', '');
    current.mount.querySelector('.life-knowledge-history').setAttribute('open', '');
    topics = [{ ...topics[0], title: '修订后的主题名', notes: [knowledgeNote({ revision: 3 })] }];
    const clock = win.state.intervals.find(timer => timer.ms === 1000 && !timer.cleared);
    now += 30001; clock.fn(); await settle();
    const refreshed = current.mount.querySelector('.life-knowledge-topic');
    assert.notEqual(refreshed.getAttribute('open'), null, 'changed timer projection keeps the same topic expanded');
    assert.equal(refreshed.getAttribute('data-knowledge-disclosure'), 'topic:' + themeId);
    assert.notEqual(current.mount.querySelector('.life-knowledge-history').getAttribute('open'), null, 'history expansion survives periodic data refresh');
    current.root.remove();
    const replacement = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    const restored = replacement.mount.querySelector('.life-knowledge-topic');
    assert.notEqual(restored.getAttribute('open'), null, 'Dataview remount must not lose the existing topic expansion');
    assert.match(restored.text, /修订后的主题名/);
    assert.notEqual(replacement.mount.querySelector('.life-knowledge-history').getAttribute('open'), null, 'history expansion survives native remount');
    restored.querySelector('.life-knowledge-note').click(); await settle();
    assert.match(replacement.mount.querySelector('.life-cosmos-drawer').text, /适合静态流程表达/);
    assert.ok(calls.every(call => call.method === 'GET'));
    const other = buildHomepageRoot(home.ownerDocument.body.createDiv({ cls: 'my-life-homepage-view' }));
    await harness.plugin.mountHomepageCosmos(other.root); await settle();
    assert.equal(other.mount.querySelector('.life-knowledge-topic').getAttribute('open'), null, 'separate homepage leaves do not share expansion');
  });

  it('a Dataview replacement restores the reading revision and scroll, and a deliberate close is not reopened', async () => {
    const original = knowledgeNote({ revision: 2, result: { ...knowledgeNote().result, summary: '打开时的第二版结论' } });
    const latest = knowledgeNote({ revision: 3, result: { ...knowledgeNote().result, summary: '后台产生的第三版结论' } });
    let newest = original;
    const { current, home, harness, calls } = await setupMounted({ requests: { knowledge: options => envelope(
      options.url.endsWith('/knowledge') ? knowledgeTopics() : options.url.endsWith('?revision=2') ? original : newest) } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    current.mount.querySelector('.life-cosmos-drawer').scrollTop = 260;
    current.mount.querySelector('.life-cosmos-drawer').dispatchEvent({ type: 'scroll' });
    newest = latest;
    current.root.remove(); let replacement = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    const drawer = replacement.mount.querySelector('.life-cosmos-drawer');
    assert.equal(replacement.mount.querySelector('.life-cosmos-drawer-shell').getAttribute('aria-hidden'), 'false');
    assert.match(drawer.text, /打开时的第二版结论/);
    assert.equal(drawer.text.includes('后台产生的第三版结论'), false, 'restore must not silently substitute the newest revision');
    assert.equal(drawer.scrollTop, 260);
    assert.ok(calls.some(call => call.url.endsWith('/knowledge/' + original.knowledge_id + '?revision=2')));
    drawer.querySelector('.life-cosmos-drawer-close').click();
    replacement.root.remove(); replacement = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    assert.equal(replacement.mount.querySelector('.life-cosmos-drawer-shell').getAttribute('aria-hidden'), 'true');
  });

  it('an unavailable reading revision remains an explicit error after remount and never falls back to current', async () => {
    let unavailable = false;
    const { current, home, harness, calls } = await setupMounted({ requests: { knowledge: options => {
      if (options.url.endsWith('/knowledge')) return envelope(knowledgeTopics());
      if (unavailable && options.url.includes('?revision=')) throw new Error('saved revision unavailable');
      return envelope(knowledgeNote());
    } } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    unavailable = true;
    const before = calls.filter(call => call.url.includes('/knowledge/')).length;
    current.root.remove(); const replacement = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(replacement.root); await settle();
    assert.equal(replacement.mount.querySelector('.life-cosmos-drawer-shell').getAttribute('aria-hidden'), 'false');
    assert.match(replacement.mount.querySelector('.life-cosmos-drawer').text, /知识读取失败/);
    const reads = calls.filter(call => call.url.includes('/knowledge/')).slice(before).filter(call => !/\/(notifications|periods)$/.test(call.url));
    assert.equal(reads.length, 1); assert.ok(reads[0].url.endsWith('?revision=2'));
  });

  it('opening an unclaimed fragment reads its current result and closing discards the late open response', async () => {
    let response = () => envelope([]);
    const { current } = await setupMounted({ requests: { alignments: () => response() }, beforeMount: addOrganizedFragment });
    response = () => envelope([concludedAlignment()]);
    current.mount.querySelector('.life-frag-panorama-row').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('查看完整知识与依据'), drawer.text);
    drawer.querySelector('.life-cosmos-drawer-close').click();
    const gate = deferred(); response = () => gate.promise;
    current.mount.querySelector('.life-frag-panorama-row').click(); await settle();
    drawer.querySelector('.life-cosmos-drawer-close').click();
    gate.resolve(envelope([concludedAlignment()])); await settle();
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').getAttribute('aria-hidden'), 'true');
  });

  it('researching journal progress is shown as system research rather than always collecting or awaiting the user', async () => {
    const item = alignmentPayload();
    item.execution.status = 'running'; item.execution.result = null; item.execution.harvest = [];
    item.execution.research_progress = { stage: 'researching', cognitive: 'collecting', collected_sources: 0,
      model_calls: 2, model_calls_unknown: true, model_call_count_basis: 'observed_cli_turns_lower_bound' };
    const { current } = await setupMounted({ requests: { alignments: () => envelope([item]), reviews: () => envelope([]) } });
    const row = current.mount.querySelector('.life-frag-panorama-row');
    assert.ok(row.text.includes('系统正在研究'));
    assert.equal(row.text.includes('系统正在收集来源'), false);
    row.click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('系统正在核对材料并形成判断'));
    assert.equal(drawer.text.includes('模型尚未调用'), false);
    assert.equal(drawer.text.includes('需要你的决定'), false);
  });

  it('NIST revised publication binds the research result digest without changing the continuation digest', async () => {
    const item = alignmentPayload();
    const control = 'd1af2f14678781e8e9d63b987d2ce3276fafd3b5e9a66e8918e6e247408f30d5';
    const reviewed = '8dda6a88b7811d516a04a6f0a5b4efbbb3cc8341d317a4879fab032f94572a0f';
    item.execution.result_digest = control;
    item.execution.research_result_digest = reviewed;
    item.execution.research_progress = { stage: 'synthesized', cognitive: 'synthesized', collected_sources: 1, model_calls: 5 };
    item.execution.knowledge_publication = { knowledge_id: knowledgeNote().knowledge_id, revision: 2, path: knowledgeNote().path,
      result_digest: reviewed, publication_source: 'system_policy' };
    const { current } = await setupMounted({ requests: { alignments: () => envelope([item]) } });
    const row = current.mount.querySelector('.life-frag-panorama-row');
    assert.ok(row.text.includes('结论已自动沉淀'));
    assert.equal(row.text.includes('保存状态待确认'), false);
    row.click(); await settle();
    assert.ok(current.mount.querySelector('.life-cosmos-drawer').text.includes('查看完整知识与依据'));
    assert.equal(item.execution.result_digest, control);
  });

  it('the panorama and native drawer use the exact concluded parent over an unfinished recovery duplicate', async () => {
    const item = alignmentPayload();
    item.execution.research_progress = { stage: 'synthesized', cognitive: 'synthesized', collected_sources: 2, model_calls: 1, agent_invocations: 1 };
    item.execution.subscription_authorization = { source: 'user_authorized_subscription', run_id: item.execution.run_id,
      provider: 'codex_subscription', max_agent_invocations: 4, max_tools: 16 };
    item.execution.knowledge_publication = { knowledge_id: knowledgeNote().knowledge_id, revision: 2,
      path: knowledgeNote().path, result_digest: item.execution.result_digest, publication_source: 'system_policy' };
    item.execution.result = { ...item.execution.result, summary: '原任务已完成的结论', answer_markdown: '# 完整原结论' };
    const recovery = { ...item, alignment_id: 'align:recovery-child', sequence: item.sequence + 10,
      parent_run_id: item.execution.run_id, recovery_reason: 'offline_zero_evidence',
      execution: { ...item.execution, run_id: 'exec:recovery-child', subscription_authorization: null,
        result: null, knowledge_publication: null,
        research_progress: { stage: 'synthesis_disabled', cognitive: 'evidence_ready', collected_sources: 1, model_calls: 0 } } };
    const { current } = await setupMounted({ requests: { alignments: () => envelope([recovery, item]), reviews: () => envelope([]) } });
    const row = current.mount.querySelector('.life-frag-panorama-row');
    assert.ok(row.text.includes('结论已自动沉淀'));
    row.click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('原任务已完成的结论'));
    assert.ok(drawer.text.includes('执行授权回执：Codex 月套餐'));
    assert.equal(drawer.text.includes('综合判断能力未启用'), false);
  });

  for (const [stage, label] of [['independent_review_failed', '证据复核未完成'], ['independent_review_unknown', '证据复核回执未知'],
    ['independent_review_limit', '证据复核达到调用上限'], ['independent_review_in_progress', '证据复核进行中']]) {
    it(`panorama and drawer prioritize ${stage} over old review and publication state`, async () => {
      const item = alignmentPayload();
      item.execution.status = stage === 'independent_review_in_progress' ? 'running' : 'blocked';
      item.execution.research_progress = { stage, cognitive: 'synthesized', collected_sources: 2, model_calls: 2 };
      item.execution.graph_escalation = { status: 'proposed' };
      item.execution.result = { ...item.execution.result, summary: '旧版结论待复核' };
      item.execution.knowledge_publication = { knowledge_id: knowledgeNote().knowledge_id, revision: 1,
        path: knowledgeNote().path, result_digest: item.execution.result_digest, publication_source: 'system_policy' };
      const { current } = await setupMounted({ requests: { alignments: () => envelope([item]), reviews: () => envelope([reviewCandidatePayload()]) } });
      const row = current.mount.querySelector('.life-frag-panorama-row');
      assert.ok(row.text.includes(label));
      assert.equal(row.getAttribute('data-fragment-state'), stage === 'independent_review_in_progress' ? 'active' : 'blocked');
      assert.equal(row.text.includes('等你确认'), false);
      row.click(); await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      assert.ok(drawer.text.includes(label));
      assert.ok(drawer.querySelector('.fragment-review-history').text.includes('旧版结论待复核'));
      assert.equal(drawer.querySelector('textarea'), null);
      assert.equal(drawer.querySelector('.life-cosmos-harvest'), null);
      assert.equal(drawer.text.includes('创建 Graph'), false);
      assert.equal(drawer.text.includes('结论已自动沉淀'), false);
    });
  }

  it('knowledge record shows the independent model review and preserves its limits', async () => {
    const review = { policy_version: 'independent-evidence-review-v1', draft_digest: 'd'.repeat(64), reviewed_result_digest: 'e'.repeat(64),
      reviewed_at: '2026-09-08T12:00:00Z', verdict: 'insufficient_evidence', reason: '缺少独立试跑证据',
      findings: [{ statement: '<script>unsafe()</script>', issue: 'unverified_trial', correction: '试跑结果仍未知', evidence_ids: ['ev-1'] }] };
    const { current } = await setupMounted({ requests: {
      knowledge: options => envelope(options.url.endsWith('/knowledge') ? knowledgeTopics() : knowledgeNote({ independent_review: review })),
    } });
    current.mount.querySelector('.life-knowledge-note').click(); await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('证据复核（模型） · 证据不足，保留限制'));
    assert.ok(drawer.text.includes('试跑结果仍未知'));
    assert.ok(drawer.text.includes('未经过人工审核'));
    assert.equal(drawer.querySelector('script'), null);
  });

  it('主页刷新重新取得自动沉淀回执并重绘全景，保留搜索和筛选', async () => {
    const item = alignmentPayload();
    item.execution.research_progress = { stage: 'synthesized', collected_sources: 1, model_calls: 1 };
    const { current } = await setupMounted({ requests: { alignments: () => envelope([item]) } });
    const firstRow = current.mount.querySelector('.life-frag-panorama-row');
    assert.ok(firstRow.text.includes('保存状态待确认'));
    const search = current.mount.querySelector('.life-frag-panorama-search');
    search.value = 'MiniMax'; search.dispatchEvent({ type: 'input' });
    const doneFilter = current.mount.querySelectorAll('[data-fragment-filter]').find((el) => el.getAttribute('data-fragment-filter') === 'done');
    doneFilter.click();
    item.execution.knowledge_publication = { knowledge_id: knowledgeNote().knowledge_id, revision: 2,
      path: knowledgeNote().path, result_digest: item.execution.result_digest, publication_source: 'system_policy' };
    byAction(current.mount, 'graph.refresh').click(); await settle();
    const row = current.mount.querySelector('.life-frag-panorama-row');
    assert.ok(row.text.includes('结论已自动沉淀'));
    assert.equal(row.getAttribute('hidden'), null);
    assert.equal(current.mount.querySelector('.life-frag-panorama-search').value, 'MiniMax');
    assert.equal(current.mount.querySelectorAll('[data-fragment-filter]').find((el) => el.getAttribute('data-fragment-filter') === 'done').getAttribute('aria-pressed'), 'true');
  });

  it('recent updates show revision reasons, open the recorded revision, and do not silently vanish on refresh failure', async () => {
    let fail = false;
    const notice = { notification_id: `${knowledgeNote().knowledge_id}:2`, knowledge_id: knowledgeNote().knowledge_id,
      title: '系统可视化主题更新', revision: 2, path: 'Loop知识库/版本2.md', updated_at: '2026-09-08T12:00:00Z',
      message: '新证据触发复核', revisions: [{ previous_statement: '原判断', updated_statement: '新判断', reason: '实际试跑补入' }] };
    const { current, harness } = await setupMounted({ requests: {
      knowledge: () => envelope(knowledgeTopics()),
      notifications: () => fail ? { status: 503, text: JSON.stringify({ contract_version: '2', error: { code: 'down' } }) } : envelope([notice]),
    } });
    const updates = current.mount.querySelector('.life-knowledge-updates');
    assert.ok(updates.text.includes('实际试跑补入'));
    assert.ok(updates.text.includes('此前：原判断'));
    assert.ok(updates.text.includes('本次：新判断'));
    [...updates.querySelectorAll('button')].find((el) => el.textContent === '打开此修订记录').click(); await settle();
    assert.ok(harness.openedLinks.includes(notice.path));
    fail = true;
    byAction(current.mount, 'knowledge.refresh').click(); await settle();
    assert.ok(updates.text.includes('更新通知读取失败'));
    assert.ok(updates.text.includes('显示上次读取的通知'));
    assert.ok(updates.text.includes('实际试跑补入'));
  });

  it('知识服务首次故障不假空目录，刷新恢复后再次故障保留旧目录且标注', async () => {
    let fail = true;
    const { current } = await setupMounted({ requests: {
      knowledge: () => fail ? { status: 503, text: JSON.stringify({ contract_version: '2', error: { code: 'down' } }) } : envelope(knowledgeTopics()),
    } });
    const panel = current.mount.querySelector('.life-knowledge-catalogue');
    assert.ok(panel.text.includes('不能判断是否为空'));
    assert.ok(!panel.text.includes('还没有已沉淀的知识'));
    fail = false;
    byAction(panel, 'knowledge.refresh').click();
    await settle();
    assert.ok(panel.text.includes('Archify 的适配判断'));
    fail = true;
    byAction(current.mount, 'graph.refresh').click();
    await settle();
    assert.ok(panel.text.includes('显示最近一次成功读取的目录'));
    assert.ok(panel.text.includes('Archify 的适配判断'));
  });

  it('知识搜索使用后端全文检索，清空恢复目录，迟到结果不能替换新输入', async () => {
    const gate = deferred();
    const { current, calls } = await setupMounted({ requests: {
      knowledge: (options) => options.url.includes('?q=') ? gate.promise : envelope(knowledgeTopics()),
    } });
    const panel = current.mount.querySelector('.life-knowledge-catalogue');
    const input = panel.querySelector('.life-knowledge-search');
    input.value = '适配 & 试跑';
    [...panel.querySelectorAll('button')].find((el) => el.textContent === '搜索').click();
    await settle();
    assert.ok(calls.some((call) => call.url.endsWith(`?q=${encodeURIComponent('适配 & 试跑')}`)));
    input.value = '';
    input.dispatchEvent({ type: 'input' });
    gate.resolve(envelope([knowledgeNote({ title: '旧搜索迟到结果' })]));
    await settle();
    assert.ok(panel.text.includes('系统可视化'));
    assert.ok(!panel.text.includes('旧搜索迟到结果'));
  });

  it('知识详情读取失败显示原因并可重试，危险来源不生成外链', async () => {
    let fail = true;
    const { current } = await setupMounted({ requests: {
      knowledge: (options) => options.url.endsWith('/knowledge') ? envelope(knowledgeTopics())
        : fail ? { status: 503, text: JSON.stringify({ contract_version: '2', error: { code: 'down' } }) }
          : envelope(knowledgeNote({ evidence: [{ evidence_id: 'ev-unsafe', title: '无效链接', url: 'javascript:alert(1)' }] })),
    } });
    current.mount.querySelector('.life-knowledge-note').click();
    await settle();
    let drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('知识读取失败'));
    assert.ok(!drawer.text.includes('适合静态流程表达'));
    fail = false;
    [...drawer.querySelectorAll('button')].find((el) => el.textContent === '重试读取').click();
    await settle();
    drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('适合静态流程表达'));
    assert.equal(drawer.querySelectorAll('a').length, 0, '非 HTTP(S) 来源仅显示文本');
  });

  it('知识详情关闭后迟到响应不重新打开抽屉', async () => {
    const gate = deferred();
    const { current } = await setupMounted({ requests: {
      knowledge: (options) => options.url.endsWith('/knowledge') ? envelope(knowledgeTopics()) : gate.promise,
    } });
    current.mount.querySelector('.life-knowledge-note').click();
    await settle();
    current.mount.querySelector('.life-cosmos-drawer-close').click();
    gate.resolve(envelope(knowledgeNote({ result: { summary: '迟到知识详情' } })));
    await settle();
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-open'), false);
    assert.ok(!current.mount.querySelector('.life-cosmos-drawer').text.includes('迟到知识详情'));
  });

  it('今日待办超过五条时完整显示，末条保留真实来源打开能力', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        const list = fixture.dashboard.querySelector('.life-todos ul');
        list.empty();
        for (let index = 1; index <= 7; index += 1) {
          const row = list.createEl('li');
          row.createEl('strong', { text: `待办 ${index}` });
          row.setAttribute('data-path', `tasks/task-${index}.md`);
        }
      },
    });
    const panel = current.mount.querySelector('.life-cosmos-todos');
    const rows = panel.querySelectorAll('.life-cosmos-go-item');
    assert.equal(rows.length, 7);
    assert.ok(panel.text.includes('7 项'));
    assert.ok(rows[6].text.includes('待办 7'));
    rows[6].querySelector('.life-cosmos-todo-open').click();
    await settle();
    assert.ok(harness.openedLinks.includes('tasks/task-7.md'));
  });

  it('研究页不把待决定当研究运行，保留真实 Graph 摘要和信息来源', async () => {
    const { current } = await setupMounted({
      requests: { runs: () => envelope([]) },
      beforeMount: (fixture) => {
        const card = fixture.dashboard.createDiv({ cls: 'fragment-intent-card' });
        card.createEl('strong', { text: '只待用户决定的事项' });
        card.createEl('button', { cls: 'fragment-intent-confirm', text: '确认方向' });
        const signal = fixture.dashboard.createDiv({ cls: 'life-daily-card' });
        signal.createEl('a', { text: '真实研究来源' }).setAttribute('data-path', 'research/source.md');
      },
    });
    const board = [...current.mount.querySelectorAll('.life-cb-board')]
      .find((item) => item.getAttribute('data-board-id') === 'research');
    assert.ok(board);
    assert.ok(board.text.includes('暂无 Graph 运行'));
    assert.ok(board.text.includes('真实研究来源'));
    assert.ok(!board.text.includes('只待用户决定的事项'));
    assert.ok(!board.text.includes('活跃探针'));
    assert.ok(!board.text.includes('扫描中'));
    assert.equal(board.querySelectorAll('.life-cb-dish').length, 0);
    assert.equal(board.querySelectorAll('.life-cb-probe').length, 0);
    assert.ok(board.querySelector('.life-cb-spectrum'), '真实 Loop 复核仍然可用');
  });

  it('知识目录展示原始成熟度，未标注不伪造评级', async () => {
    const { current } = await setupMounted({
      beforeMount: (fixture) => {
        for (const [title, maturity] of [['已记录限制', '有限制'], ['待补充记录', '']]) {
          const card = fixture.dashboard.createDiv({ cls: 'life-asset-card' });
          card.createEl('strong', { text: title });
          const meta = card.createDiv({ cls: 'life-asset-meta' });
          if (maturity) meta.createSpan({ text: maturity });
        }
      },
    });
    const rows = current.mount.querySelectorAll('.life-cb-cat-row');
    assert.equal(rows.find((row) => row.text.includes('已记录限制')).querySelector('.life-cb-maturity').text, '有限制');
    assert.equal(rows.find((row) => row.text.includes('待补充记录')).querySelector('.life-cb-maturity').text, '未标注');
    assert.equal(current.mount.querySelectorAll('.life-cb-mag').length, 0);
    assert.deepEqual(current.mount.querySelector('.life-cb-cat-head-row').children.map((el) => el.text),
      ['#', '名称', '成熟度', '标签', '验证日期']);
  });

  it('Round3 今日待办面板：第一屏完整列表 + 完成协议与 GO/EVA 同语义', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        // fixture 默认 1 条「确认研究方向」；补第二条无绑定任务
        fixture.dashboard.querySelector('.life-todos li')
          .setAttribute('data-complete-task', 'tasks/today.md');
        fixture.dashboard.querySelector('.life-todos li')
          .setAttribute('data-task-line', '2');
        fixture.dashboard.querySelector('.life-todos li')
          .setAttribute('data-path', 'tasks/today.md');
        fixture.dashboard.querySelector('.life-todos ul')
          .createEl('li').createEl('strong', { text: '整理碎片收件箱' });
      },
    });
    let completed = false;
    harness.plugin.completeTask = async (path, line) => {
      harness.pluginCalls.push(['completeTask', path, line]);
      completed = true;
    };
    harness.plugin.app.vault.read = async () => `第一行\n第二行\n- [${completed ? 'x' : ' '}] 确认研究方向`;

    // 面板存在且位于 overview 板、四面板 grid 之前（第一屏可见位置）
    const panel = current.mount.querySelector('.life-cosmos-todos');
    assert.ok(panel, '今日待办面板存在');
    // P1 样式存在性：console.css 中 .life-cosmos-todos 有专属规则（防回归为无样式裸面板）
    const css = await (await import('node:fs/promises')).readFile(
      new URL('../src/console/console.css', import.meta.url), 'utf8');
    assert.ok(/\.life-cosmos-todos\s*\{[^}]*margin/.test(css), '.life-cosmos-todos 具备 margin 定位样式');
    // FakeEl matches 只支持 [attr] 存在性（R3 先例）：closest 按存在性找，再验属性值
    const board = panel.closest('[data-board-id]');
    assert.ok(board && board.getAttribute('data-board-id') === 'focus', '面板归属日记与专注工具');

    // 完整列表：fixture 两条任务 → 面板出现两行（不是 slice(0,3) 截断问题，是全量）
    const rows = [...panel.querySelectorAll('.life-cosmos-go-item')];
    assert.equal(rows.length, 2, '完整列表渲染全部任务（非前3条截断）');

    // 行结构：标题 + 完成按钮 + 来源链接
    const boundRow = rows.find((row) => row.text.includes('确认研究方向'));
    const sessionRow = rows.find((row) => row.text.includes('整理碎片收件箱'));
    assert.ok(boundRow && byAction(boundRow, 'item.complete'), '绑定行有完成按钮');
    assert.ok(boundRow.querySelector('.life-cosmos-todo-open'), '绑定行有来源链接');
    assert.ok(sessionRow.getAttribute('data-session-choice') === '1', '无绑定行降级为会话选择');

    // DOM 序（P2-1 返修）：行首为 led、nm 在 st 之前——flex 布局下 st 的 margin-left:auto
    // 若置首位会把整行推右，必须与发射任务控制同序（led→nm→st）。
    const orderOf = (rowEl, cls) => [...rowEl.children].findIndex((el) => el.classes.has(cls));
    for (const [label, rowEl] of [['绑定行', boundRow], ['会话行', sessionRow]]) {
      const first = rowEl.children[0];
      assert.ok(first && first.classes.has('life-cosmos-led'), `${label}行首子元素为 led`);
      assert.ok(orderOf(rowEl, 'nm') < orderOf(rowEl, 'st'), `${label}：nm 在 st 之前`);
    }

    // 点完成 → 状态翻转 + 反馈可见（GO · 已完成）
    byAction(boundRow, 'item.complete').click();
    await settle();
    assert.deepEqual(
      harness.pluginCalls.filter((c) => c[0] === 'completeTask'),
      [['completeTask', 'tasks/today.md', 2]],
      '复用既有 completeTask 协议',
    );
    assert.equal(boundRow.getAttribute('data-done'), '1', '状态翻转');
    assert.ok(boundRow.text.includes('已完成'), '完成反馈可见');
    assert.ok(!harness.pluginCalls.some((c) => c[0] === 'completeTask' && c[1] !== 'tasks/today.md'), '会话行零写入');

    // 会话选择语义明示
    sessionRow.click();
    assert.ok(sessionRow.text.includes('会话选择'), '无绑定行明示会话语义');
    assert.ok(panel.text.includes('本次会话选择'), '降级提示文案在场');
  });

  it('Round3 今日待办面板：空态诚实文案', async () => {
    const { current } = await setupMounted({
      beforeMount: (fixture) => {
        fixture.dashboard.querySelector('.life-todos').empty();
      },
    });
    const panel = current.mount.querySelector('.life-cosmos-todos');
    assert.ok(panel, '空态下面板仍在（不隐藏）');
    assert.ok(panel.text.includes('今天没有待处理任务'), '诚实空态文案');
    // 空态常显（nigo 要求：要有个地方可以看到）
    const faults = panel.querySelector('.life-cosmos-todo-faults');
    assert.ok(faults, '无故障时故障区也常显');
    assert.ok(faults.text.includes('看门狗 · 无待处理故障'), '空态文案可见');
    assert.equal(faults.querySelectorAll('.life-cosmos-todo-fault-row').length, 0, '空态无故障行');
  });

  it('W6 看门狗故障分区：有单显示分区与行、点击调 openNote；无单不渲染', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        const faults = fixture.dashboard.createEl('section', { cls: 'life-faults' });
        const ul = faults.createEl('ul');
        const li1 = ul.createEl('li');
        li1.setAttribute('data-path', 'ops/看门狗/故障/active/2026-08-23_1-file_missing.md');
        li1.createEl('strong', { text: '看门狗：文件缺失检测' });
        li1.createEl('small', { text: '2026-08-23 09:00' });
        const li2 = ul.createEl('li');
        li2.setAttribute('data-path', 'ops/看门狗/故障/active/2026-08-23_2-cron_fail.md');
        li2.createEl('strong', { text: '看门狗：定时任务连续失败' });
        li2.createEl('small', { text: '2026-08-23 10:00' });
      },
    });
    // 有单：分区渲染，含计数与两行
    const faults = current.mount.querySelector('.life-cosmos-todo-faults');
    assert.ok(faults, '故障分区存在');
    assert.ok(faults.text.includes('看门狗 · 2 张待处理'), '故障计数可见');
    assert.ok(faults.text.includes('文件缺失检测') && faults.text.includes('定时任务连续失败'), '两行标题可见');
    // 故障分区在提醒分区之上（DOM 序：故障先行）
    const panel = current.mount.querySelector('.life-cosmos-todos');
    const kids = [...panel.children];
    const faultIdx = kids.indexOf(faults);
    const noticeIdx = kids.findIndex((k) => k.classes.has('life-cosmos-todo-notice'));
    assert.ok(faultIdx !== -1 && (noticeIdx === -1 || faultIdx < noticeIdx), '故障分区在提醒分区之上');
    // 点击打开：经 F-K13 守卫调 openLinkText 且路径正确（既有守卫语义）
    const open = [...current.mount.querySelectorAll('.life-cosmos-todo-fault-open')][0];
    open.click();
    await settle();
    assert.ok(harness.openedLinks.includes('ops/看门狗/故障/active/2026-08-23_1-file_missing.md'), '点击故障行调 openNote 且路径正确');
  });

  it('研究板布局：graph/signals 面板有 grid 跨度样式（防塌陷回归）', async () => {
    const { current } = await setupMounted();
    // 热修：两面板自始无跨度规则，12 列 grid 自动落单格塌陷成 ~60px 窄条。
    // 样式存在性断言进套件（本轮第二次"功能对但无样式"，断言必须防回归）。
    const cssText = await (await import('node:fs/promises')).readFile(
      new URL('../src/console/console.css', import.meta.url), 'utf8');
    const graphRule = cssText.match(/\.life-cb-graph \{[^}]*\}/);
    const signalsRule = cssText.match(/\.life-cb-signals \{[^}]*\}/);
    assert.ok(graphRule && /grid-column:\s*span 7/.test(graphRule[0]), '.life-cb-graph 有 span 7 跨度');
    assert.ok(signalsRule && /grid-column:\s*span 5/.test(signalsRule[0]), '.life-cb-signals 有 span 5 跨度');
    // 窄视口断点下两面板加入 span 12 全宽列表（选择器分两行，与 spectrum 同组）
    const graphInBp = cssText.includes('.life-cb-graph,\n  .my-life-homepage-view .life-cb-signals,');
    const signalsInBp = /life-cb-signals,\n\s*\.my-life-homepage-view \.life-cb-spectrum \{ grid-column: span 12/.test(cssText);
    assert.ok(graphInBp && signalsInBp, '窄视口断点含两面板全宽规则');
    // DOM 存在性：两面板真实渲染在研究板
    assert.ok(current.mount.querySelector('.life-cb-graph'), 'Graph 面板已渲染');
    assert.ok(current.mount.querySelector('.life-cb-signals'), '研究信号面板已渲染');
  });

  it('日常主导航仅三项，系统能力保留在次级工具且没有装饰动画', async () => {
    const { current } = await setupMounted();
    assert.deepEqual([...current.mount.querySelectorAll('.life-cosmos-nav button')].map(el => el.textContent), ['工作台', '研究记录', '知识库']);
    const tools = current.mount.querySelector('.life-cosmos-tools');
    assert.equal(tools.getAttribute('open'), null);
    for (const label of ['运行监控与信息源', '日记与专注工具', '系统管理']) assert.ok(tools.text.includes(label));
    assert.equal(current.mount.querySelector('.life-cosmos-atlas'), null);
    assert.equal(current.mount.querySelector('.life-cosmos-archive'), null);
    assert.equal(win.liveRafs(), 0);
    const task = current.mount.querySelector('.life-cosmos-todos .life-cosmos-go-item');
    assert.ok(task, '次级工具保留真实任务记录，完成协议由绑定行反例验证');
  });

  it('工作台以文字进展和当前研究结论为中心，零跟手光层和球体入口', async () => {
    const { current } = await setupMounted();
    const workbench = current.mount.querySelector('.life-workbench');
    assert.ok(workbench);
    assert.ok(workbench.text.includes('当前进展') && workbench.text.includes('最近研究结论'));
    assert.equal(current.mount.querySelectorAll('.life-cosmos-obj').length, 0);
    assert.equal(current.mount.querySelectorAll('.life-cosmos-rock').length, 0);
    assert.equal(win.liveRafs(), 0);
  });

  it('次级功能可打开但不扩张主导航，旧工作台默认折叠', async () => {
    const { current } = await setupMounted();
    assert.equal(current.legacy.classes.has('is-open'), false);
    const tools = current.mount.querySelector('.life-cosmos-tools');
    [...tools.querySelectorAll('button')].find(el => el.textContent === '日记与专注工具').click();
    assert.equal(current.mount.querySelector('.life-cosmos-home').getAttribute('data-board'), 'focus');
    assert.equal(current.mount.querySelectorAll('.life-cosmos-nav button').length, 3);
    [...tools.querySelectorAll('button')].find(el => el.textContent === '打开旧版工作台').click();
    assert.equal(current.legacy.classes.has('is-open'), true);
  });

  it('fix7 板切换跨重挂载保持 + 车队动画由流转中数据驱动', async () => {
    // ── F1：挂载 → 切碎片板 → 重挂载 → 仍停碎片板 ──
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    let current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    const nav = [...current.mount.querySelectorAll('.life-cosmos-nav button')].find((item) => item.textContent === '研究记录');
    nav.click();
    await settle();
    assert.equal(current.mount.querySelector('.life-cosmos-home').getAttribute('data-board'), 'fragments', '切到碎片板');
    // 模拟 Dataview force-refresh：旧根断开，新根接管
    current.root.remove();
    current = buildHomepageRoot(home);
    await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(current.mount.querySelector('.life-cosmos-home').getAttribute('data-board'), 'fragments', '重挂载后仍停碎片板（会话级保持）');
    // FakeEl matches 不支持带值属性：遍历 board 验证
    const fragBoard = [...current.mount.querySelectorAll('.life-cb-board')].find((b) => b.getAttribute('data-board-id') === 'fragments');
    assert.ok(fragBoard && fragBoard.classes.has('is-on'), '碎片板 is-on');
    // reload 回总览是预期——这里不模拟整页 reload，仅验证会话级。

    // ── F2：车队动画由 inTransit 驱动 ──
    // 当前 fixture 有排队碎片 → inTransit > 0 → route.is-active 存在
    // （FakeEl matches 不支持复合类，遍历判断）
    const activeRoute = [...current.mount.querySelectorAll('.life-cb-route')].find((r) => r.classes.has('is-active'));
    assert.ok(activeRoute, 'inTransit > 0 时车队运行动画类存在');
    // 清空 captures → inTransit = 0 → 无 is-active
    const emptyHome = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    current.root.remove();
    const cur2 = buildHomepageRoot(emptyHome);
    cur2.dashboard.querySelector('.life-fragments').empty();
    await harness.plugin.mountHomepageCosmos(cur2.root);
    await settle();
    // fix7 板保持：清空场景重挂载后仍停在碎片板（lastActiveBoard 会话级）
    assert.equal(cur2.mount.querySelector('.life-cosmos-home').getAttribute('data-board'), 'fragments', '空数据重挂载仍保持碎片板');
    const nav2 = [...cur2.mount.querySelectorAll('.life-cosmos-nav button')].find((item) => item.textContent === '研究记录');
    nav2.click();
    await settle();
    const route2 = cur2.mount.querySelector('.life-cb-route');
    assert.ok(route2, 'route 容器仍在');
    assert.ok(!route2.classes.has('is-active'), 'inTransit = 0 时车队空跑停止（无 is-active）');
  });

  it('fix8 车队光点范围绑定有货航段 + 指引下一步路径可点', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const cur = buildHomepageRoot(home);
    // 只第 2 段（精炼站 index=1）有货：采集带 0 / 精炼站 1 / 试航场 0 / 恒星铸造 0
    const pipe = cur.dashboard.querySelector('.life-pipeline-status');
    pipe.children[0].children[0].setText('0');
    pipe.children[1].children[0].setText('1');
    pipe.children[2].children[0].setText('0');
    pipe.children[3].children[0].setText('0');
    // 指引卡：含路径 + 无路径两张
    const frag = cur.dashboard.querySelector('.life-fragments');
    const orgCard = frag.createEl('article', { cls: 'life-organized-card' });
    orgCard.createEl('strong', { text: '整理指引甲' });
    orgCard.createEl('p', { text: '目标：核验这条信息' });
    orgCard.createEl('p', { text: '下一步：检查仓库中 tests/test_actionable_env_toy24.py' });
    const plainCard = frag.createEl('article', { cls: 'life-organized-card' });
    plainCard.createEl('strong', { text: '整理指引乙' });
    plainCard.createEl('p', { text: '目标：确认方向' });
    plainCard.createEl('p', { text: '下一步：补充一个最小可验证步骤' });

    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await harness.plugin.mountHomepageCosmos(cur.root);
    await settle();

    // ── F1 车队：光点范围限于有货航段（第 2 段中心 = (1+0.5)/4*100 = 37.5%）──
    const route = cur.mount.querySelector('.life-cb-route');
    assert.ok(route.classes.has('is-active'), '有货时车队运行');
    assert.equal(route.style.getPropertyValue('--convoy-from'), '37.5%', '光点起点 = 第 2 段中心');
    assert.equal(route.style.getPropertyValue('--convoy-to'), '37.5%', '只有一段有货 → from==to（驻留该段）');
    // 航段 live 态正确
    const wps = [...cur.mount.querySelectorAll('.life-cb-waypoint')];
    assert.ok(wps[1].classes.has('live'), '第 2 段 live');
    assert.ok(!wps[0].classes.has('live'), '第 1 段无货非 live');
    // 全空 → 无 --convoy-*（is-active 停驻断言在 fix7 已覆盖）
    const emptyHome = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    cur.root.remove();
    const cur3 = buildHomepageRoot(emptyHome);
    cur3.dashboard.querySelector('.life-fragments').empty();
    await harness.plugin.mountHomepageCosmos(cur3.root);
    await settle();
    const route3 = cur3.mount.querySelector('.life-cb-route');
    assert.equal(route3.style.getPropertyValue('--convoy-from'), null, '全空不设光点范围');

    // ── F2 指引路径链接 ──
    cur3.root.remove();
    const home2 = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const cur4 = buildHomepageRoot(home2);
    const frag4 = cur4.dashboard.querySelector('.life-fragments');
    const org4 = frag4.createEl('article', { cls: 'life-organized-card' });
    org4.createEl('strong', { text: '整理指引甲' });
    org4.createEl('p', { text: '目标：核验这条信息' });
    org4.createEl('p', { text: '下一步：检查仓库中 tests/test_actionable_env_toy24.py' });
    const plain4 = frag4.createEl('article', { cls: 'life-organized-card' });
    plain4.createEl('strong', { text: '整理指引乙' });
    plain4.createEl('p', { text: '目标：确认方向' });
    plain4.createEl('p', { text: '下一步：补充一个最小可验证步骤' });
    // P2-1（K3 返修）：补「目标」行含路径的卡——目标行链接也必须可点
    const org5 = frag4.createEl('article', { cls: 'life-organized-card' });
    org5.createEl('strong', { text: '整理指引丙' });
    org5.createEl('p', { text: '目标：精读 docs/guide.md' });
    org5.createEl('p', { text: '下一步：补充一个最小可验证步骤' });
    await harness.plugin.mountHomepageCosmos(cur4.root);
    await settle();
    const links = [...cur4.mount.querySelectorAll('.life-cb-path-link')];
    assert.equal(links.length, 2, '下一步含路径卡 + 目标含路径卡 = 2 个路径链接');
    // 下一步行的链接（org4）
    const nextLink = links.find((l) => l.textContent === 'tests/test_actionable_env_toy24.py');
    assert.ok(nextLink, '下一步路径链接存在');
    assert.equal(nextLink.getAttribute('data-cosmos-action'), 'item.openNote', '链接走 openNote 协议');
    nextLink.click();
    await settle();
    assert.ok(harness.openedLinks.includes('tests/test_actionable_env_toy24.py'), '点击下一步路径链接打开正确文档（F-K13 走 openLinkText）');
    // 目标行的链接（org5，P2-1 新增断言）
    const goalLink = links.find((l) => l.textContent === 'docs/guide.md');
    assert.ok(goalLink, '目标路径链接存在');
    assert.equal(goalLink.getAttribute('data-cosmos-action'), 'item.openNote', '目标链接走 openNote 协议');
    goalLink.click();
    await settle();
    assert.ok(harness.openedLinks.includes('docs/guide.md'), '点击目标路径链接打开正确文档');
    // org5 目标行有链接、下一步行保持纯文本（无链接）
    const goalGuide = [...cur4.mount.querySelectorAll('.life-cb-guide')].find((g) => g.children[0]?.textContent.includes('整理指引丙'));
    assert.ok(goalGuide, '目标含路径指引卡存在');
    const goalRow = goalGuide.children.find((c) => c.classes.has('life-cb-g-goal'));
    assert.equal(goalRow.querySelectorAll('.life-cb-path-link').length, 1, '目标行渲染 1 个链接');
    const goalNextRow = goalGuide.children.find((c) => c.classes.has('life-cb-g-next'));
    assert.equal(goalNextRow.querySelectorAll('.life-cb-path-link').length, 0, '目标卡下一步行（纯文本）不渲染链接');
    // 无路径卡保持纯文本（无链接）
    // （FakeEl textContent 不聚合子元素——用 b(title) 定位指引卡）
    const plainGuide = [...cur4.mount.querySelectorAll('.life-cb-guide')].find((g) => g.children[0]?.textContent.includes('整理指引乙'));
    assert.ok(plainGuide, '无路径指引卡存在');
    assert.equal(plainGuide.querySelectorAll('.life-cb-path-link').length, 0, '无路径卡不渲染链接');
    const plainNext = plainGuide.children.find((c) => c.classes.has('life-cb-g-next'));
    assert.ok(plainNext.textContent.includes('补充一个最小可验证步骤'), '无路径文本原样保留');
  });

  it('fix9 路径链接点击零反馈修复：守卫拦截（仓库路径不在 vault）给可见错误反馈', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const cur = buildHomepageRoot(home);
    const frag = cur.dashboard.querySelector('.life-fragments');
    const org = frag.createEl('article', { cls: 'life-organized-card' });
    org.createEl('strong', { text: '整理指引仓库' });
    org.createEl('p', { text: '目标：核验这条信息' });
    org.createEl('p', { text: '下一步：检查仓库中 tests/test_actionable_env_toy24.py' });

    // 仓库路径不在 vault → F-K13 守卫拦截返回 error（missingFiles mock）
    const harness = makePlugin({ getThoughtMap: async () => sampleMap(), missingFiles: ['tests/test_actionable_env_toy24.py'] });
    setupLoopConsole(harness.plugin);
    await harness.plugin.mountHomepageCosmos(cur.root);
    await settle();

    // 碎片板激活（fix7 会话保持初始 overview——手动切到碎片板）
    const nav = [...cur.mount.querySelectorAll('.life-cosmos-nav button')].find((item) => item.textContent === '研究记录');
    assert.ok(nav, '碎片导航存在');
    nav.click();
    await settle();

    const link = [...cur.mount.querySelectorAll('.life-cb-path-link')].find((l) => l.textContent === 'tests/test_actionable_env_toy24.py');
    assert.ok(link, '仓库路径链接存在');
    const status = cur.mount.querySelector('.life-cosmos-capsule-status');
    assert.ok(status, 'capsule 状态行存在');
    // 点击前无 error
    assert.notEqual(status.getAttribute('data-kind'), 'error', '点击前无错误反馈');
    link.click();
    await settle();
    // fix9：守卫 error 必须可见（say 反馈到 capsule）
    assert.equal(status.getAttribute('data-kind'), 'error', '守卫拦截给出可见 error 反馈');
    assert.ok(status.textContent.includes('未找到文档'), '错误信息可见（未找到文档）');
    assert.ok(status.textContent.includes('tests/test_actionable_env_toy24.py'), '错误携带真实路径');
    // 守卫拦截时不得打开链接（F-K13 语义）
    assert.ok(!harness.openedLinks.includes('tests/test_actionable_env_toy24.py'), '路径不存在时不调用 openLinkText');
  });

  it('整理指引卡按 fragment_id 打开专属自动化选项，不退化为通用详情', async () => {
    const qwen = alignmentPayload({
      alignment_id: 'align-qwen', fragment_id: 'qwen-fragment', case_id: 'case-qwen',
      episode_id: 'ep-qwen', title: 'Qwen3.8-Flash-Next 预告 Qwen4 架构',
      execution: {
        run_id: 'run-qwen', fragment_id: 'qwen-fragment', status: 'passed',
        current_node: '', route: 'verify', stop_reason: 'not_found',
        updated_at: '2026-08-28T00:00:00Z', result_digest: DIGEST('e'),
        research_progress: {
          /* P1-1 修复后本用例锁定「真 search_exhausted」形态：硬规则要求
             plan_exhausted=true 且 network_requests>0 才可投影为策略耗尽
             （0 网络请求的旧形态属系统态，见下方 capability_offline 回归）。 */
          stage: 'no_evidence', collected_sources: 0, stop_reason: 'not_found',
          plan_exhausted: true, model_calls: 0, network_requests: 3,
          note: '本轮未取得可核验证据。',
        },
        research_evidence: [],
        harvest: [{ role: 'failure', maturity: 'candidate', summary: '没有取得证据' }],
        result: {
          summary: '本轮未取得可核验证据。',
          unknowns: ['QSA 与 51B N-gram 的官方实现仍未知。'],
          next_checks: ['检查官方模型卡或官方仓库。'],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 0,
        },
      },
    });
    const { current, calls } = await setupMounted({
      requests: {
        alignments: () => envelope([qwen]),
        continueEpisode: () => envelope(alignmentPayload({
          alignment_id: 'align-qwen-next', fragment_id: 'qwen-fragment', case_id: 'case-qwen',
          episode_id: 'ep-qwen-next', title: 'Qwen3.8-Flash-Next 预告 Qwen4 架构',
          status: 'suggested', sequence: 3, decision: undefined, execution: undefined,
        })),
      },
      beforeMount: (fixture) => {
        const card = fixture.dashboard.querySelector('.life-fragments')
          .createEl('article', { cls: 'life-organized-card' });
        card.setAttribute('data-source-fragment', 'qwen-fragment');
        card.createEl('strong', { text: 'Qwen3.8-Flash-Next 预告 Qwen4 架构' });
        card.createEl('p', { text: '目标：核验官方架构说明' });
        card.createEl('p', { text: '下一步：检查官方模型卡' });
        const detail = card.createEl('a', { text: '详情 →' });
        detail.setAttribute('data-href', 'AI创业/碎片整理/qwen-fragment.md');
      },
    });

    [...current.mount.querySelectorAll('.life-cosmos-nav button')]
      .find((item) => item.textContent === '研究记录').click();
    await settle();
    const guide = [...current.mount.querySelectorAll('.life-cb-guide')]
      .find((item) => item.children[0]?.textContent.includes('Qwen3.8-Flash-Next'));
    assert.ok(guide, 'Qwen 整理指引卡存在');
    guide.click();
    await settle();

    let drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('Qwen3.8-Flash-Next 预告 Qwen4 架构'), '抽屉绑定当前碎片');
    assert.ok(drawer.text.includes('核验结果'), '零证据状态先展示结果');
    assert.ok(drawer.text.includes('本轮未找到足以支持当前判断的可靠来源'), '结果直接说明结论');
    assert.ok(drawer.text.includes('系统建议'), '展示系统建议');
    assert.ok(drawer.text.includes('继续核验'), '提供自动继续选项');
    assert.ok(drawer.text.includes('补充来源'), '提供补充来源选项');
    assert.ok(drawer.text.includes('保持当前结果'), '提供不改变权威结果的选择');
    assert.ok(drawer.text.includes('查看核验依据'), '证据过程默认折叠');
    assert.equal(drawer.text.includes('选择一种方式继续'), false, '不再展示过程式说明');
    assert.equal(drawer.text.includes('主页原生详情'), false, '不再进入通用详情');

    const keep = [...drawer.querySelectorAll('button')]
      .find((item) => item.textContent === '保持当前结果');
    keep.click();
    await settle();
    assert.equal(drawer.classes.has('is-open'), false, '保持当前结果只关闭决策面');
    assert.equal(calls.filter((entry) => String(entry.url).includes('/fragment/v1/episodes/')).length, 0, '保持当前结果不伪造新 episode 或持久状态');
    guide.click();
    await settle();
    drawer = current.mount.querySelector('.life-cosmos-drawer');

    const supplement = [...drawer.querySelectorAll('button')]
      .find((item) => item.textContent === '补充来源');
    const supplementDetails = drawer.querySelector('.life-cosmos-source-supplement');
    assert.notEqual(supplementDetails.open, true, '补充输入默认折叠');
    supplement.click();
    assert.equal(supplementDetails.open, true, '用户选择补充来源后才展开输入');
    assert.equal(calls.filter((entry) => String(entry.url).includes('/fragment/v1/episodes/')).length, 0, '展开补充输入不会创建后续 episode');

    const auto = [...drawer.querySelectorAll('button')]
      .find((item) => item.textContent === '继续核验');
    auto.click();
    await settle();
    await settle();
    const continuationCalls = calls.filter((entry) => String(entry.url).includes('/fragment/v1/episodes/'));
    assert.equal(continuationCalls.length, 1, '自动继续只创建一个后续 episode');
    assert.ok(JSON.parse(continuationCalls[0].body).goal.includes('官方来源'), '后续目标绑定官方来源核验');
    assert.match(drawer.text, /等待你确认方向/, `成功后抽屉切换到服务端返回的新 episode：${drawer.text}`);
    assert.equal(drawer.text.includes('继续核验'), false, '旧 episode 的继续按钮不会残留造成重复点击错觉');

    const open = [...drawer.querySelectorAll('button')]
      .find((item) => item.textContent === '打开整理文档');
    assert.ok(open, '整理文档入口保留');
    assert.equal(open.classes.has('is-primary'), false, '文档入口降为次要操作');

    const css = readFileSync(path.join(ROOT, 'src', 'console', 'console.css'), 'utf8');
    assert.match(css, /\.life-cb-g-next \{ display: block;/, '下一步恢复内联文本流，不再把路径前后文字压成三列');
  });

  for (const [blocker, label] of [
    ['synthesis_disabled', '综合判断能力未启用'],
    ['watch_budget_exhausted', '自动补查次数已用尽'],
  ]) {
    it(`API → client → Cosmos 抽屉保留 ${blocker}，不要求用户重填目标`, async () => {
      const item = alignmentPayload({ title: 'Archify 适配判断' });
      item.execution.research_progress = {
        stage: 'synthesis_disabled', cognitive: 'evidence_ready', blocker,
        collected_sources: 1, model_calls: 0, network_requests: 1,
        coverage: { status: 'unassessed', covered: [], gaps: ['compatibility'] },
        candidate_coverage: { covered: ['compatibility'], gaps: [] },
      };
      item.execution.result.model_calls = 0;
      item.execution.result.summary = '已取得一个候选来源，尚未形成可靠判断。';
      const { current, calls } = await setupMounted({
        requests: { alignments: () => envelope([item]) },
        beforeMount: (fixture) => {
          const card = fixture.dashboard.querySelector('.life-fragments')
            .createEl('article', { cls: 'life-organized-card' });
          card.setAttribute('data-source-fragment', item.fragment_id);
          card.createEl('strong', { text: item.title });
          card.createEl('p', { text: '目标：判断能否适配' });
        },
      });
      [...current.mount.querySelectorAll('.life-cosmos-nav button')]
        .find((button) => button.textContent === '研究记录').click();
      await settle();
      const guide = [...current.mount.querySelectorAll('.life-cb-guide')]
        .find((card) => card.text.includes('Archify 适配判断'));
      assert.ok(guide, '从 API 返回的碎片进入 Cosmos');
      guide.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      assert.ok(drawer.text.includes(label), drawer.text);
      assert.equal(drawer.querySelectorAll('textarea').length, 0);
      assert.equal(drawer.querySelectorAll('[data-cosmos-action="intent.continue"]').length, 0);
      assert.equal(drawer.text.includes('创建后续处理'), false);
      assert.equal(calls.filter((entry) => String(entry.method || 'GET') !== 'GET').length, 0);
    });
  }

  it('P1-1 抽屉消费认知轴：零网络 capability_offline 只显诚实系统态、无继续核验入口', async () => {
    /* 后端硬规则：0 网络请求绝不判 search_exhausted（live_disabled →
       capability_offline）。旧形态（stage no_evidence + 0 请求）曾被抽屉
       误投为「未找到可靠来源，请继续核验/补充来源」——本回归锁定修复。 */
    const offline = alignmentPayload({
      alignment_id: 'align-offline', fragment_id: 'offline-fragment', case_id: 'case-offline',
      episode_id: 'ep-offline', title: '零网络离线碎片',
      execution: {
        run_id: 'run-offline', fragment_id: 'offline-fragment', status: 'passed',
        current_node: '', route: 'verify', stop_reason: 'live_disabled',
        updated_at: '2026-08-28T00:00:00Z', result_digest: DIGEST('f'),
        research_progress: {
          stage: 'no_evidence', collected_sources: 0, stop_reason: 'live_disabled',
          model_calls: 0, network_requests: 0, note: '核验能力未开启。',
        },
        research_evidence: [],
        harvest: [{ role: 'failure', maturity: 'candidate', summary: '能力未开启，未发起任何请求' }],
        result: {
          summary: '未取得证据（核验能力未开启）。',
          unknowns: [], next_checks: [], needs_escalation: false,
          escalation_reason: '', model_calls: 0, tool_calls: 0,
        },
      },
    });
    const { current, calls } = await setupMounted({
      requests: { alignments: () => envelope([offline]) },
      beforeMount: (fixture) => {
        const card = fixture.dashboard.querySelector('.life-fragments')
          .createEl('article', { cls: 'life-organized-card' });
        card.setAttribute('data-source-fragment', 'offline-fragment');
        card.createEl('strong', { text: '零网络离线碎片' });
        card.createEl('p', { text: '目标：核验这条信息' });
        card.createEl('p', { text: '下一步：等待系统恢复' });
      },
    });

    [...current.mount.querySelectorAll('.life-cosmos-nav button')]
      .find((item) => item.textContent === '研究记录').click();
    await settle();
    const guide = [...current.mount.querySelectorAll('.life-cb-guide')]
      .find((item) => item.children[0]?.textContent.includes('零网络离线碎片'));
    assert.ok(guide, '离线碎片整理指引卡存在');
    guide.click();
    await settle();

    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('零网络离线碎片'), '抽屉绑定当前碎片');
    // 诚实系统态，与卡面 COGNITIVE_JOURNEY.capability_offline 一致。
    assert.ok(drawer.text.includes('系统待恢复'), '显示系统待恢复而非来源不足');
    assert.ok(drawer.text.includes('无需你操作'), '明确无需用户操作');
    // 无继续核验主按钮、不要求补来源、无三选项分支、无自由接续输入。
    assert.equal(drawer.text.includes('继续核验'), false, '无继续核验入口');
    assert.equal(drawer.text.includes('补充来源'), false, '不要求用户补来源');
    assert.equal(drawer.text.includes('保持当前结果'), false, '无三选项分支');
    assert.equal(drawer.text.includes('创建后续处理'), false, '能力离线无自由接续输入（与卡面一致）');
    assert.equal(drawer.text.includes('本轮未找到足以支持当前判断的可靠来源'), false, '0 网络请求不得投影为来源不足');
    assert.equal(drawer.querySelectorAll('[data-cosmos-action="intent.continue"]').length, 0, '无任何 continuation 动作按钮');
    assert.equal(calls.filter((entry) => String(entry.url).includes('/fragment/v1/episodes/')).length, 0, '零网络形态零 episode 调用');
  });

  it('碎片统一筛选覆盖全部记录，旧阶段看板作为默认折叠的整理快照', async () => {
    const suggested = alignmentPayload({
      alignment_id: 'align-panorama-2', fragment_id: 'panorama-2', case_id: 'case-panorama-2',
      episode_id: 'ep-panorama-2', title: '全景碎片 2', status: 'suggested', sequence: 1,
      decision: undefined, execution: undefined,
    });
    const { current } = await setupMounted({
      requests: { alignments: () => envelope([suggested]) },
      beforeMount: (fixture) => {
        const fragments = fixture.dashboard.querySelector('.life-fragments');
        for (let index = 1; index <= 6; index += 1) {
          const card = fragments.createEl('article', { cls: 'life-organized-card' });
          card.setAttribute('data-source-fragment', `panorama-${index}`);
          card.createEl('strong', { text: `全景碎片 ${index}` });
          card.createEl('p', { text: '目标：检查完整旅程' });
          card.createEl('p', { text: '下一步：按权威状态继续' });
        }
      },
    });

    [...current.mount.querySelectorAll('.life-cosmos-nav button')]
      .find((item) => item.textContent === '研究记录').click();
    await settle();
    const panes = current.mount.querySelectorAll('.life-frag-view-pane');
    const rows = current.mount.querySelectorAll('.life-frag-panorama-row');
    assert.equal(rows.length, 7, '六条整理记录加原有显式原始路径碎片，不沿用旧四卡上限或丢失 raw 记录');
    assert.ok(rows.some(row => row.text.includes('碎片甲')), '原始路径的 basename 也必须进入全景');
    assert.equal(panes[0].getAttribute('hidden'), null, '默认打开全景进度');
    const legacy = current.mount.querySelector('.life-frag-legacy-view');
    assert.equal(legacy.tagName, 'DETAILS');
    assert.equal(legacy.getAttribute('open'), null, '旧整理快照默认折叠');
    assert.equal(panes[1].closest('details'), legacy, '保留旧看板的数据和操作入口');
    assert.ok(legacy.text.includes('当前运行状态及需要你处理的事项，以研究记录为准'));
    assert.equal(current.mount.querySelectorAll('[data-fragment-filter]').length, 7, '每种状态仅一个带数量的筛选控件');

    const waiting = [...current.mount.querySelectorAll('.life-frag-panorama-metric')]
      .find((item) => item.getAttribute('data-fragment-filter') === 'waiting');
    waiting.click();
    assert.equal(rows.filter((row) => row.getAttribute('hidden') === null).length, 1, '状态筛选只留下待决碎片');
    assert.ok(rows.find((row) => row.getAttribute('hidden') === null)
      .text.includes('全景碎片 2'), '待决筛选来自真实 suggested episode');

    assert.equal(waiting.getAttribute('aria-pressed'), 'true', '读屏可识别选中筛选');
    const search = current.mount.querySelector('.life-frag-panorama-search');
    search.value = '全景碎片 6';
    search.dispatchEvent({ type: 'input' });
    assert.equal(rows.filter((row) => row.getAttribute('hidden') === null).length, 0, '关键词与状态组合生效');
    const all = [...current.mount.querySelectorAll('.life-frag-panorama-metric')]
      .find((item) => item.getAttribute('data-fragment-filter') === 'all');
    all.click();
    assert.equal(rows.filter((row) => row.getAttribute('hidden') === null).length, 1);
    assert.equal(waiting.getAttribute('aria-pressed'), 'false');
    legacy.setAttribute('open', 'open');
    assert.equal(panes[0].getAttribute('hidden'), null, '查看旧快照不会替换权威全景');
  });

  it('Continuation：空目标零请求、确定性 newest 绑定、双击一次、409 冲突保留输入', async () => {
    const posts = [];
    let conflict = false;
    const { current } = await setupMounted({
      requests: {
        alignments: () => envelope([alignmentPayload()]),
        continueEpisode: (options) => {
          posts.push(options);
          if (conflict) {
            return { status: 409, text: JSON.stringify({ contract_version: '2', error: { code: 'source_changed', message: '来源已变化' } }) };
          }
          return envelope(alignmentPayload({ episode_id: 'ep-2', sequence: 3 }));
        },
      },
    });
    // 打开语义抽屉 → 处理方向与接续。
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === '处理方向与接续').click();
    await settle();
    assert.ok(drawer().text.includes('原目标'), '展示目标绑定');
    assert.ok(drawer().text.includes('将创建子 episode'));

    const input = drawer().querySelector('.life-cosmos-cont-goal');
    const submit = drawer().querySelector('.life-cosmos-cont-submit');
    // 空目标：零请求。
    input.value = '   ';
    submit.click();
    await settle();
    assert.equal(posts.length, 0, '空目标不发请求');
    assert.ok(drawer().text.includes('请先写下'), '空目标诚实提示');

    // 双击：只产生一个请求；绑定父 episode 是 newest（ep-1，sequence 2）。
    input.value = '继续核验官方公告';
    submit.click();
    submit.click();
    await settle();
    assert.equal(posts.length, 1, '双击只产生一个持久请求');
    const body = JSON.parse(posts[0].body);
    assert.equal(body.parent_episode_id, 'ep-1', 'newest episode 确定性绑定');
    assert.equal(body.goal, '继续核验官方公告');
    assert.ok(posts[0].url.includes('/fragment/v1/episodes/ep-1/continuations'));
    assert.ok(drawer().text.includes('新的处理目标已建立'), '成功反馈');

    // 完成异步刷新后按钮恢复，再提交另一目标验证 409。
    await settle();
    assert.equal(submit.getAttribute('disabled'), null);
    // 409 冲突：保留输入，明确冲突文案，不冒充成功。
    conflict = true;
    input.value = '换一个目标再试';
    submit.click();
    await settle();
    assert.equal(posts.length, 2);
    const message = drawer().querySelector('.life-cosmos-drawer-message');
    assert.equal(message.getAttribute('data-kind'), 'conflict', '409 呈现为冲突');
    assert.equal(input.value, '换一个目标再试', '冲突后输入保留');
  });

  it('Continuation：抽屉关闭后迟到的响应一律丢弃', async () => {
    const gate = deferred();
    const { current } = await setupMounted({
      requests: {
        alignments: () => envelope([alignmentPayload()]),
        continueEpisode: () => gate.promise,
      },
    });
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === '处理方向与接续').click();
    await settle();
    drawer().querySelector('.life-cosmos-cont-goal').value = '迟到响应测试';
    drawer().querySelector('.life-cosmos-cont-submit').click();
    await settle();
    // 关闭抽屉，再放行迟到的成功响应：不得落任何 UI、不得抛错。
    drawer().querySelector('.life-cosmos-drawer-close').click();
    gate.resolve(envelope(alignmentPayload({ episode_id: 'ep-2', sequence: 3 })));
    await settle();
    assert.equal(current.mount.querySelector('.life-cosmos-drawer-shell').classes.has('is-open'), false, '抽屉保持关闭');
    assert.ok(!current.mount.text.includes('新的处理目标已建立'), '迟到成功不落 UI');
  });

  it('资产：显式 data-path 打开真实文档，无路径诚实说明，恶意文本当纯文本', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        const strip = fixture.dashboard.createEl('section', { cls: 'life-assets-strip' });
        const list = strip.createDiv({ cls: 'life-asset-list' });
        const good = list.createEl('article', { cls: 'life-asset-card' });
        good.createEl('strong', { text: '资产甲' });
        const link = good.createEl('a', { text: '打开' });
        link.setAttribute('data-path', 'assets/good.md');
        const bad = list.createEl('article', { cls: 'life-asset-card' });
        bad.createEl('strong', { text: '<img src=x onerror=alert(1)> 无路径资产' });
      },
    });
    const nav = [...current.mount.querySelectorAll('.life-cosmos-nav button')].find((item) => item.textContent === '知识库');
    nav.click();
    await settle();
    const rows = [...current.mount.querySelectorAll('.life-cb-cat-row')];
    const good = rows.find((row) => row.text.includes('资产甲'));
    good.click();
    await settle();
    assert.ok(harness.openedLinks.includes('assets/good.md'), '显式路径打开真实 Obsidian 文档');

    const bad = rows.find((row) => row.text.includes('无路径资产'));
    bad.click();
    await settle();
    assert.equal(harness.openedLinks.filter((item) => item !== 'assets/good.md').length, 0, '无路径不猜路径、不打开');
    assert.ok(current.mount.text.includes('暂无可打开文档'), '无路径诚实说明');
    assert.equal(current.mount.querySelector('img'), null, '恶意文本不生成元素');
  });

  it('Review：候选列表具名打开既有确认协议，未选知识卡不能确认', async () => {
    const { current } = await setupMounted({
      requests: { reviews: () => envelope([reviewCandidatePayload()]) },
    });
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === 'Loop 人工确认').click();
    await settle();
    const row = byAction(drawer(), 'review.open');
    assert.ok(row, '候选行存在');
    assert.ok(drawer().text.includes('不形成资产捷径'), 'Harvest/Review 不绕过确认协议');
    row.click();
    await settle();
    const overlay = current.mount.ownerDocument.querySelector('.life-loop-review-dialog-overlay');
    assert.ok(overlay, '既有权威确认对话打开');
    const confirm = [...overlay.querySelectorAll('button')].find((item) => item.textContent === '确认形成资产');
    assert.ok(confirm, '确认动作按 available_actions 呈现');
    confirm.click();
    await settle();
    assert.ok(overlay.text.includes('至少选择一张候选知识卡'), '未选知识卡不能确认');
  });

  it('Pilot：合格候选打开既有确认页，不合格候选诚实原因，已桥接打开 Run', async () => {
    const openedRuns = [];
    const { current, harness } = await setupMounted({
      requests: {
        reviews: () => envelope([
          reviewCandidatePayload(),
          reviewCandidatePayload({ candidate_id: 'cand-2', title: '候选乙', fragment_ref: '散记/碎片想法/frag-2.md', content_status: 'kept_draft' }),
          reviewCandidatePayload({ candidate_id: 'cand-3', title: '候选丙', fragment_ref: '散记/碎片想法/frag-3.md' }),
        ]),
        runs: () => envelope([{ run_id: 'exec:pilot1', graph_id: 'fragment-pilot-v1', spec_digest: DIGEST('e'), fragment_ref: '散记/碎片想法/frag-3.md', status: 'running', sequence: 1, step_count: 1, updated_at: '2026-08-14T09:00:00Z', started_at: '2026-08-14T08:00:00Z', pending_human: [] }]),
      },
    });
    harness.plugin.app.workspace.getLeaf = () => ({ setViewState: async () => {} });
    harness.plugin.app.workspace.revealLeaf = () => { openedRuns.push(1); };
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === 'Graph Pilot 候选').click();
    await settle();
    assert.ok(drawer().text.includes('候选尚未就绪'), '不合格候选诚实原因');
    const convert = byAction(drawer(), 'pilot.openPlan');
    assert.ok(convert, '合格候选出现转换入口');
    convert.click();
    await settle();
    const overlay = current.mount.ownerDocument.querySelector('.graph-pilot-dialog-overlay');
    assert.ok(overlay, '既有 Pilot 确认页打开（计划绑定原样保留）');
    assert.ok(overlay.text.includes('碎片试点'), '确认页显示真实预案');
    // 已桥接候选：具名 pilot.openRun 打开既有 Graph Run。
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === 'Graph Pilot 候选').click();
    await settle();
    const bridgedRow = byAction(drawer(), 'pilot.openRun');
    assert.ok(bridgedRow, '已桥接候选出现打开入口');
    const before = openedRuns.length;
    bridgedRow.click();
    await settle();
    assert.ok(openedRuns.length > before, '已桥接候选打开真实 Graph Run');
  });

  it('打乱旧 DOM 按钮顺序，Proposal/Pilot/Intent/Review 仍按语义路由', async () => {
    const { current } = await setupMounted({
      requests: {
        alignments: () => envelope([alignmentPayload({
          alignment_id: 'align-2', status: 'suggested', decision: undefined, route: undefined, execution: undefined,
        })]),
      },
      beforeMount: (fixture) => {
        // 往旧 intent 卡里塞乱序按钮：语义路由绝不读它们。
        const card = fixture.intentCard;
        card.createEl('button', { text: '查看' });
        card.createEl('button', { text: '打开' });
        card.createEl('button', { text: '确认并继续' });
      },
    });
    const go = [...current.mount.querySelectorAll('.life-cosmos-deck .is-primary')][0];
    assert.ok(go, '拍板入口存在');
    go.click();
    await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('处理方向与接续'), '按卡片语义类路由到 Intent 区');
    const copied = [...drawer.querySelectorAll('button')].filter((item) => ['查看', '打开'].includes(item.textContent));
    assert.equal(copied.length, 0, '旧按钮不按顺序复制进抽屉');
  });

  it('抽屉键盘：初始焦点、Escape、inert、Tab 圈、焦点归还、监听器释放', async () => {
    const { doc, current } = await setupMounted();
    const trigger = byAction(current.mount, 'drawer.settings');
    const before = (doc.listeners.keydown || []).length;
    trigger.click();
    await settle();
    const shell = current.mount.querySelector('.life-cosmos-drawer-shell');
    assert.equal(shell.classes.has('is-open'), true, '抽屉打开');
    assert.equal((doc.listeners.keydown || []).length, before + 1, '打开时注册一个键盘监听');
    const topbar = current.mount.querySelector('.life-cosmos-topbar');
    assert.equal(topbar.getAttribute('inert'), '', '主界面 inert');
    assert.equal(doc.activeElement, current.mount.querySelector('.life-cosmos-drawer-close'), '初始焦点进入第一个可操作控件');
    assert.equal(shell.getAttribute('aria-labelledby'), 'life-cosmos-drawer-title', '标题关联');

    // Tab 圈：焦点在最后一个控件时回到第一个。
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    const focusables = [];
    const walk = (el) => {
      for (const child of el.children || []) {
        if (['button', 'input', 'textarea', 'select', 'a', '[tabindex]'].some((sel) => child.matches(sel))
          && child.getAttribute('disabled') === null) focusables.push(child);
        walk(child);
      }
    };
    walk(drawer);
    assert.ok(focusables.length >= 2, '抽屉内有多个可聚焦控件');
    const last = focusables[focusables.length - 1];
    last.focus();
    let prevented = false;
    doc.listeners.keydown.at(-1)({ key: 'Tab', preventDefault: () => { prevented = true; } });
    assert.equal(prevented, true, 'Tab 在末尾被拦截');
    assert.equal(doc.activeElement, focusables[0], '焦点绕回第一个控件');

    // Escape：关闭、inert 恢复、监听移除、焦点归还触发器。
    doc.listeners.keydown.at(-1)({ key: 'Escape', preventDefault: () => {} });
    assert.equal(shell.classes.has('is-open'), false, 'Escape 关闭');
    assert.equal(topbar.getAttribute('inert'), null, 'inert 恢复');
    assert.equal((doc.listeners.keydown || []).length, before, '键盘监听移除');
    assert.equal(trigger.focused, true, '焦点归还仍连接触发器');
  });

  it('抽屉 10 次开关 + 会话销毁：零监听器/inert 积累', async () => {
    const { doc, current } = await setupMounted();
    const base = (doc.listeners.keydown || []).length;
    for (let cycle = 0; cycle < 10; cycle += 1) {
      const trigger = [...current.mount.querySelectorAll('.life-cosmos-deck .is-primary')][0];
      trigger.click();
      await settle();
      assert.equal((doc.listeners.keydown || []).length, base + 1, `第 ${cycle + 1} 次打开恰好一个监听`);
      doc.listeners.keydown.at(-1)({ key: 'Escape', preventDefault: () => {} });
      assert.equal((doc.listeners.keydown || []).length, base, `第 ${cycle + 1} 次关闭监听清零`);
    }
    assert.equal(current.mount.querySelector('.life-cosmos-topbar').getAttribute('inert'), null, 'inert 零残留');
    // 会话销毁：打开状态随 Cosmos dispose 完整恢复。
    const trigger = [...current.mount.querySelectorAll('.life-cosmos-deck .is-primary')][0];
    trigger.click();
    await settle();
    current.root.remove();
    FakeMutationObserver.instances[0].trigger();
    await win.pump();
    await settle();
    assert.equal((doc.listeners.keydown || []).length, base, '销毁后监听清零');
  });

  it('研究记录搜索仅筛选具备真实碎片身份的行，清空恢复全部', async () => {
    const { current } = await setupMounted({ beforeMount: fixture => {
      const first = fixture.dashboard.querySelector('.life-capture-card');
      first.setAttribute('data-fragment-id', 'frag-1');
      const second = fixture.dashboard.querySelector('.life-fragments').createEl('article', { cls: 'life-capture-card' });
      second.setAttribute('data-fragment-id', 'frag-2');
      second.createEl('strong', { text: '碎片乙' });
      second.createEl('p', { text: '另一条' });
    } });
    const input = current.mount.querySelector('.life-frag-panorama-search');
    input.value = '碎片乙'; input.dispatchEvent({ type: 'input' });
    const visible = () => [...current.mount.querySelectorAll('.life-frag-panorama-row')].filter(el => el.getAttribute('hidden') === null);
    assert.equal(visible().length, 1); assert.match(visible()[0].text, /碎片乙/);
    input.value = ''; input.dispatchEvent({ type: 'input' });
    assert.equal(visible().length, 2);
    assert.equal(current.mount.querySelector('.life-cosmos-capsule-search'), null);
  });

  it('微修：碎片排队状态带时间（有 time 显示入队时间，无 time 诚实仅「排队」）', async () => {
    const { current } = await setupMounted({
      beforeMount: (fixture) => {
        // 现有 capture 卡补 time + 排队态；再加一张无 time 的排队卡与一张重试卡
        const frag = fixture.dashboard.querySelector('.life-fragments');
        const card1 = frag.querySelector('.life-capture-card');
        if (!card1.querySelector('time')) card1.createEl('time', { text: '09:14' });
        const card2 = frag.createEl('article', { cls: 'life-capture-card' });
        card2.createEl('strong', { text: '碎片乙' });
        card2.createEl('p', { text: '无时间戳投影' });
        const card3 = frag.createEl('article', { cls: 'life-capture-card needs-retry' });
        card3.createEl('strong', { text: '碎片丙' });
        card3.createEl('p', { text: '重试态投影' });
        card3.createEl('time', { text: '08:02' });
      },
    });
    // 切到碎片板
    const nav = [...current.mount.querySelectorAll('.life-cosmos-nav button')].find((item) => item.textContent === '研究记录');
    nav.click();
    await settle();
    const statuses = [...current.mount.querySelectorAll('.life-cb-ore-st')];
    assert.equal(statuses.length, 3, '三条矿石状态行');
    assert.ok(statuses[0].text.startsWith('排队 · 09:14 入队'), '有 time 的排队条目带时间');
    assert.ok(statuses[1].text === '排队', '无 time 诚实仅「排队」');
    assert.ok(statuses[2].text.includes('已提取') || statuses[2].text.includes('重试'), '重试/已提取态正常渲染');
    assert.ok(statuses[2].text.includes('08:02'), '重试态同样带时间');
    // 已提取（is-ready/is-archived）不带时间——该态语义是完成，无需入队时间
    const readyStatus = statuses.find((s) => s.text.includes('已提取'));
    if (readyStatus) assert.ok(!readyStatus.text.includes('·'), '已提取态保持纯状态词');
  });

  it('碎片详情：探索/开始实验/反馈按显式路径调用既有 handler', async () => {
    const { harness, current } = await setupMounted();
    // 碎片主板「原始输入」舱的矿石按钮点击即 reveal 碎片卡。
    const nav = [...current.mount.querySelectorAll('.life-cosmos-nav button')].find((item) => item.textContent === '研究记录');
    nav.click();
    await settle();
    const ore = [...current.mount.querySelectorAll('.life-cb-ore')][0];
    ore.click();
    await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    assert.ok(drawer.text.includes('碎片甲'), '碎片详情打开');
    byAction(drawer, 'fragment.explore').click();
    await settle();
    byAction(drawer, 'fragment.experiment.start').click();
    await settle();
    byAction(drawer, 'fragment.feedback.save').click();
    await settle();
    assert.deepEqual(harness.pluginCalls.filter((call) => ['requestFragmentExploration', 'startExperiment', 'openExperimentFeedback'].includes(call[0])), [
      ['requestFragmentExploration', 'Notes/散记/碎片想法/frag-1.md'],
      ['startExperiment', 'Notes/散记/已整理碎片/org-1.md'],
      ['openExperimentFeedback', 'Notes/散记/已整理碎片/org-1.md'],
    ], '三动作只转发既有 handler，路径来自显式 data-path');
  });

  it('显示与筛选：筛选/密度走持久偏好，管理隐藏与 Agent 评估走既有入口', async () => {
    const { harness, current } = await setupMounted();
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    [...[...drawer.querySelectorAll('[data-cosmos-action]')].filter((el) => el.getAttribute('data-cosmos-action') === 'view.setFilter')].find((item) => item.textContent === '未读').click();
    await settle();
    [...[...drawer.querySelectorAll('[data-cosmos-action]')].filter((el) => el.getAttribute('data-cosmos-action') === 'view.setDensity')].find((item) => item.textContent === '紧凑').click();
    await settle();
    [...drawer.querySelectorAll('button')].find((item) => item.textContent === '管理隐藏内容').click();
    await settle();
    [...drawer.querySelectorAll('button')].find((item) => item.textContent === 'Agent 项目能力评估').click();
    await settle();
    assert.deepEqual(harness.pluginCalls, [
      ['setViewPreference', 'filter', 'unread'],
      ['setViewPreference', 'density', 'compact'],
      ['openContentManager'],
      ['openProjectAssessment'],
    ], '全部转发既有 handler，无第二套状态');
  });

  it('capability 同步/异步异常归一为 error，不泄漏未处理 rejection', async () => {
    const { harness, current } = await setupMounted();
    harness.plugin.openContentManager = async () => { throw new Error('管理器损坏'); };
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = current.mount.querySelector('.life-cosmos-drawer');
    [...drawer.querySelectorAll('button')].find((item) => item.textContent === '管理隐藏内容').click();
    await settle();
    const message = drawer.querySelector('.life-cosmos-drawer-message');
    assert.equal(message.getAttribute('data-kind'), 'error', '异常归一为 error');
    assert.ok(message.text.includes('管理器损坏'), '错误携带真实原因');
    assert.ok(!message.text.includes('成功'), '异常不显示成功');
    // 缺失能力同样诚实报错。
    delete harness.plugin.openQuickCapture;
    const capsule = current.mount.querySelector('.life-cosmos-capsule-status');
    byAction(current.mount, 'capture.quick').click();
    await settle();
    assert.equal(capsule.getAttribute('data-kind'), 'error', '缺失能力诚实 error');
  });

  it('源码互斥：抽屉不按 DOM 顺序复制动作、不使用 innerHTML', () => {
    const source = readFileSync(path.join(ROOT, 'src', 'console', 'homepage-cosmos.js'), 'utf8');
    assert.ok(!source.includes('source.click'), '不得点击旧按钮转发动作');
    assert.ok(!/\.innerHTML\s*=/.test(source), '不得用 innerHTML 写入内容');
    const registerSource = readFileSync(path.join(ROOT, 'src', 'console', 'register.js'), 'utf8');
    assert.ok(!/\.innerHTML\s*=/.test(registerSource), 'adapter 层同样不用 innerHTML');
    assert.ok(!registerSource.includes('querySelectorAll("button")'), 'adapter 不得按按钮查找动作');
  });

  it('窄窗样式：820/620 媒体查询存在，抽屉全宽 100dvh，核心入口不隐藏', () => {
    const css = readFileSync(path.join(ROOT, 'src', 'console', 'console.css'), 'utf8');
    assert.ok(css.includes('@media (max-width: 820px)'), '820px 断点存在');
    assert.ok(css.includes('@media (max-width: 620px)'), '620px 断点存在');
    assert.ok(css.includes('100dvh'), '抽屉窄窗全高');
    assert.ok(css.includes('.my-life-homepage-view .life-cosmos-capsule-action'), '任务舱入口窄窗样式存在');
    assert.ok(css.includes('prefers-reduced-motion'), 'reduced-motion 降级保留');
    assert.ok(css.includes('focus-visible'), 'focus-visible 样式存在');
  });

  it('总览服务指示灯：Graph/Loop 状态点亮，详情进抽屉，无技术 ID', async () => {
    const { current } = await setupMounted({
      requests: {
        runs: () => envelope([{ run_id: 'exec:sec1', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'human_wait', sequence: 7, step_count: 3, updated_at: '2026-08-14T10:00:00Z', started_at: '2026-08-14T09:00:00Z', pending_human: [{ node: 'n' }] }]),
        reviews: () => envelope([reviewCandidatePayload()]),
      },
    });
    // P2a：安全摘要条降级为 capsule 服务指示灯——旧独立条不再渲染
    assert.ok(!current.mount.querySelector('.life-cosmos-safety'), '旧安全摘要条已移除');
    // P1-1 返修：指示灯三态样式存在（防"测试全绿但灯不可见"回归）
    const cssText = await (await import('node:fs/promises')).readFile(
      new URL('../src/console/console.css', import.meta.url), 'utf8');
    assert.ok(/\.life-cosmos-svc-dot \{[^}]*min-height:\s*42px/.test(cssText), '服务状态文字按钮具备可点击高度');
    assert.ok(/\.life-cosmos-svc-dot\[data-state="attention"\]/.test(cssText), 'attention 态样式存在');
    assert.ok(/\.life-cosmos-svc-dot\[data-state="error"\]/.test(cssText), 'error 态样式存在');
    assert.ok(/\.life-cosmos-todo-notice \{/.test(cssText), '提醒分区弱化样式存在');
    // FakeEl matches 不支持组合选择器：按存在性找 dot 再按 class 过滤
    const dots = [...current.mount.querySelectorAll('.life-cosmos-svc-dot')];
    const graphDot = dots.find((d) => d.classes.has('is-graph'));
    const loopDot = dots.find((d) => d.classes.has('is-loop'));
    assert.ok(graphDot && loopDot, '两颗服务指示灯存在');
    // P2-1 返修：Graph human_wait → attention；Loop 有待确认候选 → attention
    assert.equal(graphDot.getAttribute('data-state'), 'attention', 'Graph 需要关注');
    assert.equal(loopDot.getAttribute('data-state'), 'attention', 'Loop 需要关注');
    // P2-2：aria-label 为整体重写——多次刷新不追加后缀
    for (let i = 0; i < 3; i += 1) {
      byAction(current.mount, 'graph.refresh').click();
      await settle();
    }
    const labelAfter = graphDot.getAttribute('aria-label');
    assert.ok(labelAfter.includes('Graph 服务状态'), 'label 含基础标识');
    assert.equal((labelAfter.match(/（/g) || []).length, 1, 'label 只有一组状态后缀（无累积）');
    assert.ok(!current.mount.text.includes('exec:'), '无技术 ID 泄漏');
    byAction(current.mount, 'drawer.graph').click();
    await settle();
    assert.ok(current.mount.querySelector('.life-cosmos-drawer').text.includes('Graph · 状态与安全摘要'), 'Graph 详情进抽屉');
    byAction(current.mount, 'drawer.review').click();
    await settle();
    assert.ok(current.mount.querySelector('.life-cosmos-drawer').text.includes('Loop 复核 · 待确认清单'), 'Review 详情进抽屉');
  });

  it('唯一归属：最近笔记不重复上主板（深度工作台保留完整旧信息密度）', async () => {
    const { current } = await setupMounted();
    const nav = [...current.mount.querySelectorAll('.life-cosmos-tools button')].find((item) => item.textContent === '日记与专注工具');
    nav.click();
    await settle();
    assert.equal(current.mount.querySelector('.life-cb-ship-log'), null, '专注主板不再重复最近笔记');
    assert.ok(current.legacy.querySelector('.life-dashboard-content'), '深度工作台完整保留');
  });

  it('旧工作台搜索保留原 handler，研究搜索不冒充全局搜索或修改旧筛选', async () => {
    const forwarded = [];
    const { current } = await setupMounted({ beforeMount: fixture => {
      const input = fixture.legacy.querySelector('.life-command-search').createEl('input');
      input.setAttribute('data-home-search', '1');
      input.addEventListener('input', () => forwarded.push(input.value));
    } });
    const local = current.mount.querySelector('.life-frag-panorama-search');
    local.value = '碎片乙'; local.dispatchEvent({ type: 'input' });
    assert.deepEqual(forwarded, []);
    [...current.mount.querySelectorAll('.life-cosmos-tools button')].find(el => el.textContent === '打开旧版工作台').click();
    const legacyInput = current.legacy.querySelector('[data-home-search]');
    legacyInput.value = '碎片甲'; legacyInput.dispatchEvent({ type: 'input' });
    assert.deepEqual(forwarded, ['碎片甲']);
    assert.equal(current.legacy.classes.has('is-open'), true);
  });

  it('Continuation：多 alignment 按 sequence 最大值确定性选 newest，与列表顺序无关', async () => {
    const older = alignmentPayload({ alignment_id: 'align-old', episode_id: 'ep-old', sequence: 1 });
    const newer = alignmentPayload({ alignment_id: 'align-new', episode_id: 'ep-new', sequence: 3 });
    for (const order of [[older, newer], [newer, older]]) {
      const posts = [];
      const { current } = await setupMounted({
        requests: {
          alignments: () => envelope(order),
          continueEpisode: (options) => {
            posts.push(options);
            return envelope(alignmentPayload({ alignment_id: 'align-child', episode_id: 'ep-child', sequence: 4 }));
          },
        },
      });
      byAction(current.mount, 'drawer.settings').click();
      await settle();
      const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
      [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === '处理方向与接续').click();
      await settle();
      const rows = [...drawer().querySelectorAll('.life-cosmos-intent-row')];
      assert.equal(rows.length, 1, '同一 fragment 只呈现 newest episode');
      drawer().querySelector('.life-cosmos-cont-goal').value = '新的核验目标';
      drawer().querySelector('.life-cosmos-cont-submit').click();
      await settle();
      assert.equal(posts.length, 1);
      assert.equal(JSON.parse(posts[0].body).parent_episode_id, 'ep-new', `列表顺序 ${order[0].episode_id} 在前仍绑定 newest`);
      const message = drawer().querySelector('.life-cosmos-drawer-message');
      assert.equal(message.getAttribute('data-kind'), 'success', '成功安全态明确');
      current.root.remove();
      await settle();
    }
  });

  it('Continuation：目标切换后迟到的响应丢弃，不污染新目标', async () => {
    const gate = deferred();
    const { current } = await setupMounted({
      requests: {
        alignments: () => envelope([alignmentPayload()]),
        continueEpisode: () => gate.promise,
      },
    });
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === '处理方向与接续').click();
    await settle();
    drawer().querySelector('.life-cosmos-cont-goal').value = '目标 A';
    drawer().querySelector('.life-cosmos-cont-submit').click();
    await settle();
    // 目标切换：在途期间打开另一个语义区（generation 推进）。
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === 'Graph 状态详情').click();
    await settle();
    gate.resolve(envelope(alignmentPayload({ episode_id: 'ep-2', sequence: 3 })));
    await settle();
    assert.ok(!drawer().text.includes('新的处理目标已建立'), '旧目标迟到响应不落新目标 UI');
  });

  it('pilot.openPlan：预案失败返回 error，不打开确认页也不显示成功', async () => {
    const { current } = await setupMounted({
      requests: {
        reviews: () => envelope([reviewCandidatePayload()]),
        pilotPlan: () => ({ status: 503, text: JSON.stringify({ contract_version: '2', error: { code: 'down', message: '预案服务不可用' } }) }),
      },
    });
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === 'Graph Pilot 候选').click();
    await settle();
    byAction(drawer(), 'pilot.openPlan').click();
    await settle();
    const message = drawer().querySelector('.life-cosmos-drawer-message');
    assert.equal(message.getAttribute('data-kind'), 'error', '失败明确为 error');
    assert.ok(!message.text.includes('成功'), '失败不显示成功');
    assert.equal(current.mount.ownerDocument.querySelector('.graph-pilot-dialog-overlay'), null, '预案失败不打开确认页');
  });

  it('Intent 确认：取消勾选以真实 checked property 为准，全取消零请求', async () => {
    const posts = [];
    const { current } = await setupMounted({
      requests: {
        alignments: () => envelope([alignmentPayload({
          alignment_id: 'align-2', status: 'suggested', suggested_intents: ['verify', 'learn'],
          decision: undefined, route: undefined, execution: undefined,
        })]),
        decide: (options) => {
          posts.push(options);
          return envelope(alignmentPayload({ alignment_id: 'align-2' }));
        },
      },
    });
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === '处理方向与接续').click();
    await settle();
    const inputs = [...drawer().querySelectorAll('.life-cosmos-intent-options input')];
    assert.equal(inputs.length, 2);
    // 用户取消一个勾选（只改 property）。
    const verify = inputs.find((input) => input.getAttribute('data-intent-id') === 'verify');
    verify.checked = false;
    const confirm = [...drawer().querySelectorAll('button')].find((item) => item.textContent === '确认并继续');
    confirm.click();
    await settle();
    assert.equal(posts.length, 1, '剩余一个选择正常提交');
    assert.deepEqual(JSON.parse(posts[0].body).intents, ['learn'], '已取消的选择不被提交');
    // 全部取消：零请求 + 诚实提示。
    for (const input of inputs) input.checked = false;
    confirm.click();
    await settle();
    assert.equal(posts.length, 1, '全部取消后不产生请求');
    assert.ok(drawer().text.includes('至少选择一个处理方向'), '零选择诚实提示');
  });

  it('卡堆稍后明示仅本次会话，不再提供容易误认为持久的碎星锁定', async () => {
    const { current } = await setupMounted();
    assert.ok(current.mount.text.includes('「稍后」只调整本次会话的卡堆顺序'));
    assert.equal(current.mount.querySelector('.life-cosmos-rock-pop'), null);
    assert.equal(current.mount.querySelector('.life-cosmos-belt'), null);
  });

  it('生产同形：当前焦点是 My Life 本身时如实显示且可真实启动/暂停/重置', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        fixture.dashboard.querySelector('.life-focus-bar strong').setText('My Life');
      },
    });
    const nav = [...current.mount.querySelectorAll('.life-cosmos-tools button')].find((item) => item.textContent === '日记与专注工具');
    nav.click();
    await settle();
    const target = current.mount.querySelector('.life-cb-t-name');
    assert.equal(target.textContent, 'My Life', '真实当前焦点原样显示，不伪装成任务');
    const go = byAction(current.mount, 'focus.toggle');
    assert.ok(go, '启动/暂停走具名 focus.toggle');
    assert.equal(go.textContent, '启动记录');
    go.click();
    await settle();
    byAction(current.mount, 'focus.reset').click();
    await settle();
    assert.deepEqual(harness.pluginCalls, [['toggleReadingTimer'], ['resetCurrentReadingTime']], '既有 timer 命令真实生效');
  });

  it('GO：completeTask 后行状态未变化返回 conflict，不打勾', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        const li = fixture.dashboard.querySelector('.life-todos li');
        li.setAttribute('data-complete-task', 'tasks/today.md');
        li.setAttribute('data-task-line', '1');
      },
    });
    // 只读权威确认失败：目标行仍是 [ ]（未变化或已漂移）。
    harness.plugin.app.vault.read = async () => '第一行\n- [ ] 确认研究方向';
    const bound = [...current.mount.querySelectorAll('.life-cosmos-go-item')].find((row) => byAction(row, 'item.complete'));
    byAction(bound, 'item.complete').click();
    await settle();
    assert.equal(harness.pluginCalls.filter((call) => call[0] === 'completeTask').length, 1, '唯一写入已发生');
    assert.equal(bound.getAttribute('data-done'), '0', '未确认完成不打勾');
    const note = bound.querySelector('.life-cosmos-go-note');
    assert.equal(note.getAttribute('data-kind'), 'conflict', '未变化呈现为冲突');
    assert.ok(note.text.includes('没有变化'), '诚实说明未打勾原因');
    assert.ok(!bound.text.includes('已完成'), '不出现完成文案');
  });

  it('Graph 主板刷新：成功就地更新六态与最近运行，失败保留旧数据', async () => {
    let version = 1;
    const { current } = await setupMounted({
      requests: {
        runs: () => (version === 1
          ? envelope([{ run_id: 'exec:v1', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'running', sequence: 5, step_count: 3, updated_at: '2026-08-14T10:00:00Z', started_at: '2026-08-14T09:00:00Z', pending_human: [] }])
          : envelope([{ run_id: 'exec:v2', graph_id: 'g1', spec_digest: DIGEST('e'), status: 'completed', sequence: 2, step_count: 4, updated_at: '2026-08-15T10:00:00Z', started_at: '2026-08-15T09:00:00Z', pending_human: [] }])),
      },
    });
    const panel = () => current.mount.querySelector('.life-cb-graph-body');
    assert.ok(panel().text.includes('Checkpoint #5'), '主板就地更新为新数据前显示旧 Checkpoint');
    version = 2;
    byAction(current.mount, 'graph.refresh').click();
    await settle();
    assert.ok(panel().text.includes('Checkpoint #2'), '主板就地更新为新数据');
    assert.ok(panel().text.includes('1已结束'), '六态同步更新');
    assert.ok(!panel().text.includes('Checkpoint #5'), '旧数据被新数据替换');
    /* P2a：安全摘要降级为指示灯——F-D3 refresher 机制验证：刷新后 Graph 灯
       data-state 随新数据（completed，无 pending_human）翻转为 ok。 */
    const graphDot = [...current.mount.querySelectorAll('.life-cosmos-svc-dot')].find((d) => d.classes.has('is-graph'));
    assert.equal(graphDot.getAttribute('data-state'), 'ok', 'F-D3：refresher 触发指示灯动态更新');
  });

  it('GO/EVA 写前预检：行号指向另一任务或已完成行时零写入', async () => {
    const { harness, current } = await setupMounted({
      beforeMount: (fixture) => {
        const li = fixture.dashboard.querySelector('.life-todos li');
        li.setAttribute('data-complete-task', 'tasks/today.md');
        li.setAttribute('data-task-line', '1');
      },
    });
    // 行漂移：绑定行号现在指向另一条未完成任务——预检必须拦截，零 completeTask。
    harness.plugin.app.vault.read = async () => '第一行\n- [ ] 完全不同的另一任务';
    const bound = [...current.mount.querySelectorAll('.life-cosmos-go-item')].find((row) => byAction(row, 'item.complete'));
    byAction(bound, 'item.complete').click();
    await settle();
    assert.equal(harness.pluginCalls.filter((call) => call[0] === 'completeTask').length, 0, '行漂移零写入');
    assert.equal(bound.getAttribute('data-done'), '0', '不打勾');
    const note = bound.querySelector('.life-cosmos-go-note');
    assert.equal(note.getAttribute('data-kind'), 'conflict', '漂移呈现为冲突');
    assert.ok(note.text.includes('未执行写入'), '明确说明未写入');

    // 已完成行：同样零写入。
    harness.plugin.app.vault.read = async () => '第一行\n- [x] 确认研究方向';
    byAction(bound, 'item.complete').click();
    await settle();
    assert.equal(harness.pluginCalls.filter((call) => call[0] === 'completeTask').length, 0, '已完成行零写入');
    assert.equal(bound.getAttribute('data-done'), '0', '仍不打勾');
  });

  it('Continuation 并发：A 慢 B 快同抽屉，结果各落各行互不覆盖', async () => {
    const gateA = deferred();
    const { current } = await setupMounted({
      requests: {
        alignments: () => envelope([
          alignmentPayload({ alignment_id: 'align-a', fragment_id: 'frag-a', episode_id: 'ep-a', title: '主题甲' }),
          alignmentPayload({ alignment_id: 'align-b', fragment_id: 'frag-b', episode_id: 'ep-b', title: '主题乙' }),
        ]),
        continueEpisode: (options) => {
          if (String(options.url).includes('/ep-a/')) return gateA.promise;
          return envelope(alignmentPayload({ alignment_id: 'align-b2', fragment_id: 'frag-b', episode_id: 'ep-b2', sequence: 4, title: '主题乙' }));
        },
      },
    });
    byAction(current.mount, 'drawer.settings').click();
    await settle();
    const drawer = () => current.mount.querySelector('.life-cosmos-drawer');
    [...drawer().querySelectorAll('.life-cosmos-drawer-nav button')].find((item) => item.textContent === '处理方向与接续').click();
    await settle();
    const rowA = [...drawer().querySelectorAll('[data-fragment-id]')].find((el) => el.getAttribute('data-fragment-id') === 'frag-a');
    const rowB = [...drawer().querySelectorAll('[data-fragment-id]')].find((el) => el.getAttribute('data-fragment-id') === 'frag-b');
    assert.ok(rowA && rowB, '两个 fragment 各有自己的行');
    rowA.querySelector('.life-cosmos-cont-goal').value = '甲的新目标';
    rowA.querySelector('.life-cosmos-cont-submit').click();
    rowB.querySelector('.life-cosmos-cont-goal').value = '乙的新目标';
    rowB.querySelector('.life-cosmos-cont-submit').click();
    await settle();
    // B 快：结果只落 B；A 仍在途，行内无结果。
    const messageB = rowB.querySelector('.life-cosmos-drawer-message');
    const messageA = rowA.querySelector('.life-cosmos-drawer-message');
    // 等真实行级回执；固定 4 个 setImmediate 不能保证 WebCrypto/transport 已完成。
    const waitForMessage = async (line) => {
      const deadline = Date.now() + 1000;
      while (line.getAttribute('data-kind') === null && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
    };
    await waitForMessage(messageB);
    assert.equal(messageB.getAttribute('data-kind'), 'success', 'B 结果只落 B');
    assert.equal(messageA.getAttribute('data-kind'), null, 'A 在途无结果');
    // A 迟到：只落 A，B 的行不被覆盖。
    gateA.resolve(envelope(alignmentPayload({ alignment_id: 'align-a2', fragment_id: 'frag-a', episode_id: 'ep-a2', sequence: 4, title: '主题甲' })));
    await waitForMessage(messageA);
    assert.equal(messageA.getAttribute('data-kind'), 'success', 'A 迟到结果只落 A');
    assert.equal(messageB.getAttribute('data-kind'), 'success', 'B 的行保持自己的结果');
  });

  // R26 P1-3（gate_bc0f4580468f）：R1/R2/R3 三 describe 的同名 fixture 原本
  // 逐字重复三份；提升为共享一份（逐字未改），配合 R25 describe 迁移使本文件
  // 回落至冻结基线以内。
  function canvasWithAuth() {
      return {
        canvas_version: '1',
        run_id: 'exec:auth:1',
        graph_id: 'fragment-pilot-v1',
        spec_digest: 'c'.repeat(64),
        sequence: 7,
        status: 'human_wait',
        current_node: 'pilot_gate',
        task_label: '碎片认知整理',
        nodes: [
          { node_id: 'input_fragment', display_label: '碎片输入', kind: 'input', status: 'succeeded', is_entry: true, is_current: false, is_frontier: false, join: null, human_gate: null, output_digest: 'e'.repeat(64), error_code: null, completed_at: null },
          { node_id: 'pilot_gate', display_label: '人工确认', kind: 'human_decision', status: 'waiting_human', is_entry: false, is_current: true, is_frontier: true, join: null, human_gate: {
            status: 'pending',
            allowed_decisions: ['approve_call', 'reject'],
            authorization_preview: {
              provider: 'deepseek', model: 'deepseek-v4-pro', max_total_calls: 2, cost_cap_cny: 2,
              write_scope: 'Graph 检查点 + Agent 账本；不写笔记、不写资产',
              input_fields: { title: '核验目标', core_judgment: '核心判断', user_value: '用户价值', card_titles: ['卡片一'] },
            },
            authorization: {
              authorization_digest: 'a'.repeat(64), provider: 'deepseek', model: 'deepseek-v4-pro', max_total_calls: 2, cost_cap_cny: 2,
              input_digest: 'b'.repeat(64), agent_input_digest: 'c'.repeat(64), candidate_content_sha256: 'd'.repeat(64), expected_sequence: 7,
              issued_at: '2026-08-25T00:00:00+00:00', expires_at: '2099-01-01T00:00:00+00:00', price_snapshot_source: null,
              input_fields: { title: '核验目标', core_judgment: '核心判断', user_value: '用户价值', card_titles: ['卡片一'] },
            },
          }, output_digest: null, error_code: null, completed_at: null },
        ],
        edges: [],
      };
    }

  const runWithPending = () => [{
      run_id: 'exec:auth:1', graph_id: 'fragment-pilot-v1', spec_digest: 'c'.repeat(64),
      status: 'human_wait', sequence: 7, step_count: 3,
      pending_human: ['pilot_gate'], blocked_reason: null,
      started_at: '2026-08-25T00:00:00+00:00', updated_at: '2026-08-25T00:01:00+00:00',
    }];

  const runDetailFixture = () => envelope({
      run: { run_id: 'exec:auth:1', graph_id: 'fragment-pilot-v1', spec_digest: 'c'.repeat(64), status: 'human_wait', current_node: 'pilot_gate', step_count: 3, sequence: 7, pending_human: ['pilot_gate'], blocked_reason: null, started_at: '2026-08-25T00:00:00+00:00', updated_at: '2026-08-25T00:01:00+00:00' },
      nodes: [],
      edges_taken: [],
      feedback_counts: {},
      human_gates: {
        pilot_gate: { status: 'pending', decision: null, decision_id: null, input_digest: 'b'.repeat(64), spec_digest: 'c'.repeat(64), expected_sequence: 7, requester: 'nigo', decided_at: null, allowed_decisions: ['approve_call', 'reject'] },
      },
      ready: [],
    });

  describe('R1 授权队列', () => {
    // review/control/proposals client 的契约版本为 1（+proposal 需 read_only）；
    // graph 用通用 envelope（版本 2）。
    const shadowEnvelope = (data) => ({ status: 200, text: JSON.stringify({ contract_version: '1', db_mode: 'read_only', generated_at: '2026-08-25T00:00:00+00:00', service_version: 's', data }) });
    // FakeEl textContent 不聚合子元素——递归取全文。
    const allText = (el) => (el.textContent || '') + (el.children || []).map((c) => allText(c)).join(' ');

    it('R1 Graph 待决 → 卡出现（N=1）→ 展开四段 → 一键成功 → 卡消失 + say + N-1', async () => {
      let approved = false;
      const { harness, current, calls } = await setupMounted({
        requests: {
          // V2-C5 适配：review/control 源显式空路由（shadowEnvelope 版本 1）——
          // 避免默认 envelope 版本 2 被 proposal-client 解析失败产生断线标注
          // 干扰 N 复算断言。
          proposals: () => shadowEnvelope({ items: [] }),
          reviewActions: () => shadowEnvelope({ proposal_id: 'none', submittable: false }),
          controlActions: () => shadowEnvelope({ run_id: 'none', status: 'running', actions: {} }),
          runs: (options) => {
            if (String(options.method || 'GET') === 'POST') { approved = true; return envelope({ status: 'recorded', idempotent: false }); }
            return envelope(approved ? [] : runWithPending());
          },
          canvas: () => envelope(canvasWithAuth()),
          runDetail: () => runDetailFixture(),
        },
      });
      const tag = current.mount.querySelector('.life-cb-tag');
      const nBefore = Number((tag.textContent.match(/(\d+)/) || [])[1]);
      assert.ok(tag.textContent.includes('项待定'), `N 项待定存在（实际 ${tag.textContent}）`);
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      assert.ok(card, '授权卡出现');
      assert.ok(allText(card).includes('[Graph]'), '来源标签 [Graph]');
      const go = [...card.querySelectorAll('button')].find((b) => b.textContent.includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      assert.ok(allText(drawer).includes('批什么'), '四段摘要：批什么');
      assert.ok(allText(drawer).includes('验证什么'), '四段摘要：验证什么');
      assert.ok(allText(drawer).includes('授权后哪些程序会动'), '四段摘要：授权后');
      assert.ok(allText(drawer).includes('影响面与回滚'), '四段摘要：影响面');
      const approve = drawer.querySelector('.life-cosmos-auth-approve');
      assert.ok(approve, '一键授权按钮');
      approve.click();
      await settle();
      await settle();
      await settle();
      await settle();
      await settle();
      const submitCall = calls.find((c) => String(c.url).includes('/human-decisions'));
      assert.ok(submitCall, 'submitHumanDecision 被调用');
      const body = JSON.parse(submitCall.body);
      assert.equal(body.decision, 'approve_call');
      assert.equal(body.authorization_digest, 'a'.repeat(64), '授权绑定摘要已带');
      const message = current.mount.querySelector('.life-cosmos-capsule-status');
      assert.ok(message && message.textContent.includes('已授权'), 'say 成功回响');
      const after = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      assert.equal(after, undefined, '授权卡已移除');
      const nAfter = Number((current.mount.querySelector('.life-cb-tag').textContent.match(/(\d+)/) || [])[1]);
      assert.equal(nAfter, nBefore - 1, `N-1 复算（${nBefore} → ${nAfter}）`);
    });

    it('R1 Loop 审核待决 → 卡出现 → 一键成功 → 卡消失 + say', async () => {
      let approved = false;
      const { current } = await setupMounted({
        requests: {
          proposals: () => shadowEnvelope({ items: approved ? [] : [{ proposal_id: 'p-1', subject: '真实碎片主题', run_id: 'run-1' }] }),
          reviewActions: () => shadowEnvelope({
            proposal_id: 'p-1', submittable: true, reason_code: null, proposal_fingerprint: 'fp-abc', source_sequence: 7, proposal_status: 'blocked',
            current_decision: null,
          }),
          '/decisions': () => { approved = true; return shadowEnvelope({ decision_id: 'd-1', proposal_id: 'p-1', decision: 'accepted' }); },
        },
      });
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      assert.ok(card, 'Loop 审核授权卡出现');
      assert.ok(allText(card).includes('[Loop]'), '来源标签 [Loop]');
      const go = [...card.querySelectorAll('button')].find((b) => b.textContent.includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      assert.ok(allText(drawer).includes('批准该审核建议'), '批什么：批准建议');
      const approve = drawer.querySelector('.life-cosmos-auth-approve');
      approve.click();
      await settle();
      await settle();
      await settle();
      await settle();
      await settle();
      const message = current.mount.querySelector('.life-cosmos-capsule-status');
      assert.ok(message && message.textContent.includes('已授权'), 'say 成功回响');
      const after = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      assert.equal(after, undefined, '审核卡已移除');
    });


    it('pending headlines and archive use the same control queue as the deck, including canary and refresh/error states', async () => {
      let runs = [{ run_id: 'canary-control-1', loop_id: 'canary-control', status: 'paused', display_state: 'awaiting_human', updated_at: '2026-09-08T00:01:00+00:00' }];
      let failed = false;
      const { current, calls } = await setupMounted({ beforeMount: current => current.intentCard.remove(), requests: {
        proposals: () => shadowEnvelope({ items: [] }),
        loopRuns: () => { if (failed) throw new Error('control unavailable'); return loopEnvelope(runs); },
        controlActions: (options) => shadowEnvelope({ run_id: options.url.split('/').pop(), status: 'paused', latest_sequence: 3, priority: 0,
          actions: { resume: { enabled: true } } }),
      } });
      const deck = () => current.mount.querySelector('.life-cosmos-decisions');
      assert.match(deck().querySelector('.life-cb-tag').text, /1 项待定/);
      assert.match(deck().text, /canary-control/, 'real canary control item is neither hidden by name nor dropped');
      runs = [...runs, { ...runs[0], run_id: 'ordinary-control-2', loop_id: 'ordinary-control' }];
      capsuleAction(current.mount, 'graph.refresh').click(); await settle();
      assert.match(deck().querySelector('.life-cb-tag').text, /2 项待定/);
      failed = true; capsuleAction(current.mount, 'graph.refresh').click(); await settle();
      assert.match(deck().querySelector('.life-cosmos-deck-empty').text, /不能据此认定没有待决定事项/);
      assert.ok(calls.every(call => call.method === 'GET'), 'count correction never approves or deletes real records');
    });

    it('R1 Loop 控制组卡：同 run 多动作合并一张卡；terminate 不进一键', async () => {
      const { current } = await setupMounted({
        requests: {
          loopRuns: () => loopEnvelope([{
            run_id: 'exec:ctrl:1', loop_id: 'fragment-cognitive-v1', status: 'paused', display_state: 'awaiting_human',
            updated_at: '2026-08-25T00:01:00+00:00',
          }]),
          controlActions: () => shadowEnvelope({
            run_id: 'exec:ctrl:1', status: 'paused', latest_sequence: 3, priority: 0,
            actions: { resume: { enabled: true }, terminate: { enabled: true, reason_code: null }, priority: { enabled: true } },
          }),
        },
      });
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      assert.ok(card, 'Loop 控制组卡出现');
      assert.ok(allText(card).includes('[Loop]'), '来源标签 [Loop]');
      const go = [...card.querySelectorAll('button')].find((b) => b.textContent.includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      const approves = drawer.querySelectorAll('.life-cosmos-auth-approve');
      assert.equal(approves.length, 1, '终止类与需参数动作不进一键（仅 resume）');
      assert.ok(allText(drawer).includes('恢复该暂停的运行'), '批什么：resume 效果');
      assert.ok(!allText(drawer).includes('终止该运行'), 'terminate 不进一键');
    });

    it('R1 一键失败：错误行含中文原因 + 卡保留', async () => {
      const { current } = await setupMounted({
        requests: {
          runs: (options) => {
            if (String(options.method || 'GET') === 'POST') {
              return { status: 409, text: JSON.stringify({ contract_version: '2', error: { code: 'sequence_mismatch', message: '数据已变化' } }) };
            }
            return envelope(runWithPending());
          },
          canvas: () => envelope(canvasWithAuth()),
          runDetail: () => runDetailFixture(),
        },
      });
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      const go = [...card.querySelectorAll('button')].find((b) => b.textContent.includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      drawer.querySelector('.life-cosmos-auth-approve').click();
      await settle();
      await settle();
      await settle();
      await settle();
      await settle();
      const message = current.mount.querySelector('.life-cosmos-capsule-status');
      assert.ok(message && message.getAttribute('data-kind') !== 'success', '失败回响非 success（conflict/error 均可）');
      assert.ok(message.textContent.includes('数据已变化'), '错误行含中文原因（sequence_mismatch 映射）');
      const after = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      assert.ok(after, '失败卡保留');
    });
  });

  describe('R2 授权队列语义完备', () => {
    // FakeEl textContent 不聚合子元素——递归取全文（R2 describe 局部）。
    const allText = (el) => (el.textContent || '') + (el.children || []).map((c) => allText(c)).join(' ');
    // canvasWithAuth/runWithPending/runDetailFixture 已提升为顶层共享（R26）。
    // canvas fixture：authorization 过期（expires_at 已过去）——§4.5 过期判定。
    function canvasExpired() {
      const base = canvasWithAuth();
      base.nodes[1].human_gate.authorization.expires_at = '2020-01-01T00:00:00+00:00';
      return base;
    }
    // canvas fixture：授权有效（expires_at 未来）——刷新恢复场景。
    function canvasFresh() {
      const base = canvasWithAuth();
      base.nodes[1].human_gate.authorization.expires_at = '2099-01-01T00:00:00+00:00';
      return base;
    }

    it('R2 过期授权卡：降级 + 一键禁用 + 原因行可见', async () => {
      const { current } = await setupMounted({
        requests: {
          runs: () => envelope(runWithPending()),
          canvas: () => envelope(canvasExpired()),
          runDetail: () => runDetailFixture(),
        },
      });
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      assert.ok(card, '授权卡出现');
      const go = [...card.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      assert.ok(allText(drawer).includes('授权单已过期，需重新签发。'), '原因行可见');
      const approve = drawer.querySelector('.life-cosmos-auth-approve');
      assert.ok(approve, '一键按钮存在');
      assert.notEqual(approve.getAttribute('disabled'), null, '过期卡一键禁用');
      const refresh = drawer.querySelector('.life-cosmos-auth-refresh');
      assert.ok(refresh, '刷新按钮保留');
    });

    it('R2 漂移码失败 → 卡标失效态（一键禁用 + 原因行，非普通错误）', async () => {
      const { current } = await setupMounted({
        requests: {
          runs: (options) => {
            if (String(options.method || 'GET') === 'POST') {
              return { status: 409, text: JSON.stringify({ contract_version: '2', error: { code: 'sequence_mismatch', message: '数据已变化' } }) };
            }
            return envelope(runWithPending());
          },
          canvas: () => envelope(canvasWithAuth()),
          runDetail: () => runDetailFixture(),
        },
      });
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      const go = [...card.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      drawer.querySelector('.life-cosmos-auth-approve').click();
      await settle();
      await settle();
      await settle();
      const errLine = drawer.querySelector('.life-cosmos-auth-expired');
      assert.ok(errLine && allText(errLine).includes('数据已变化，请刷新。'), '漂移码 → 失效原因行');
      const approve = drawer.querySelector('.life-cosmos-auth-approve');
      assert.notEqual(approve.getAttribute('disabled'), null, '漂移后一键禁用');
      const refresh = drawer.querySelector('.life-cosmos-auth-refresh');
      assert.ok(refresh, '刷新入口保留');
    });

    it('R2 刷新后失效卡恢复有效 → 按钮重新可用', async () => {
      let canvasMode = 'expired';
      const { current } = await setupMounted({
        requests: {
          runs: () => envelope(runWithPending()),
          canvas: () => envelope(canvasMode === 'expired' ? canvasExpired() : canvasFresh()),
          runDetail: () => runDetailFixture(),
        },
      });
      const openCard = () => {
        const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
        const go = [...card.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
        go.click();
      };
      openCard();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      let approve = drawer.querySelector('.life-cosmos-auth-approve');
      assert.notEqual(approve.getAttribute('disabled'), null, '过期态按钮禁用');
      // 服务端数据恢复（授权单重新签发）→ 刷新 → 重派生恢复可用。
      canvasMode = 'fresh';
      drawer.querySelector('.life-cosmos-auth-refresh').click();
      await settle();
      await settle();
      await settle();
      await settle();
      await settle();
      await settle();
      // 刷新重派生后：关闭旧抽屉 → 重新打开新卡（新 element 已恢复可用）。
      const close = [...current.mount.querySelectorAll('.life-cosmos-drawer button')].find((b) => allText(b).includes('关闭'));
      if (close) close.click();
      await settle();
      const cardAfter = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      const goAfter = [...cardAfter.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      goAfter.click();
      await settle();
      const drawerAfter = current.mount.querySelector('.life-cosmos-drawer');
      const approveAfter = drawerAfter.querySelector('.life-cosmos-auth-approve');
      assert.equal(approveAfter.getAttribute('disabled'), null, '刷新后按钮重新可用');
      assert.ok(!allText(drawerAfter).includes('授权单已过期'), '过期原因行消失');
    });

    it('R2 已批准卡：无撤回契约来源 → 标「已生效，不可撤回」+ 无撤回按钮', async () => {
      let approved = false;
      const { current } = await setupMounted({
        requests: {
          runs: (options) => {
            if (String(options.method || 'GET') === 'POST') { approved = true; return envelope({ status: 'recorded', idempotent: false }); }
            return envelope(approved ? [] : runWithPending());
          },
          canvas: () => envelope(canvasWithAuth()),
          runDetail: () => runDetailFixture(),
        },
      });
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      const go = [...card.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      drawer.querySelector('.life-cosmos-auth-approve').click();
      await settle();
      await settle();
      await settle();
      await settle();
      await settle();
      // 批准后卡堆重派生 → 已批准卡（is-approved）出现在卡堆
      const approvedCard = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('已授权'));
      assert.ok(approvedCard, '已批准卡出现');
      const go2 = [...approvedCard.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      go2.click();
      await settle();
      const drawer2 = current.mount.querySelector('.life-cosmos-drawer');
      assert.ok(allText(drawer2).includes('已生效，不可撤回。'), '无契约来源标不可撤回');
      assert.equal(drawer2.querySelectorAll('.life-cosmos-auth-withdraw').length, 0, '无撤回按钮');
      // N 不计已批准（待决仅剩 model.decisions 的默认卡 1 张）
      const nAfter = Number((current.mount.querySelector('.life-cb-tag').textContent.match(/(\d+)/) || [])[1]);
      assert.equal(nAfter, 1, '已批准卡不计待决 N（仅剩默认决定卡）');
    });
  });

  describe('R3 攒批触达', () => {
    // FakeEl textContent 不聚合子元素——递归取全文（R3 describe 局部）。
    const allText = (el) => (el.textContent || '') + (el.children || []).map((c) => allText(c)).join(' ');
    // shadow 信封（review/control/proposals 契约版本 1）。R25 P1-5：失败源
    // 不再计入通知，干净环境必须显式提供三源成功路由。
    const shadow = (data) => ({ status: 200, text: JSON.stringify({ contract_version: '1', db_mode: 'read_only', generated_at: '2026-08-25T00:00:00+00:00', service_version: 's', data }) });
    const quietSources = {
      proposals: () => shadow({ items: [] }),
      reviewActions: () => shadow({ proposal_id: 'none', submittable: false }),
      controlActions: () => shadow({ run_id: 'none', status: 'running', actions: {} }),
    };
    // canvasWithAuth/runWithPending/runDetailFixture 已提升为顶层共享（R26）。
    // R2 挂账收尾：is-expired 视觉 class 断言（破坏后必须变红）。
    it('R2 挂账：过期授权卡带 is-expired class', async () => {
      const { current } = await setupMounted({
        requests: {
          runs: () => envelope(runWithPending()),
          canvas: () => envelope((() => {
            const base = canvasWithAuth();
            base.nodes[1].human_gate.authorization.expires_at = '2020-01-01T00:00:00+00:00';
            return base;
          })()),
          runDetail: () => runDetailFixture(),
        },
      });
      const card = [...current.mount.querySelectorAll('.life-cosmos-deck-card')].find((el) => allText(el).includes('等待授权'));
      const go = [...card.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      const auth = drawer.querySelector('.life-cosmos-auth');
      const cls = ((auth.getAttribute('class') || '') + ' ' + [...(auth.classes || [])].join(' ')).trim();
      assert.ok(cls.includes('is-expired'), `R2 挂账：is-expired class 存在（实际 class="${cls}"）`);
    });

    it('R3 0→N 触发：通知请求文件生成（只含计数+分类，无标题/摘要）', async () => {
      const tmpDir = await fsPromises.mkdtemp(path.join(os.tmpdir(), 'auth-notify-'));
      try {
        const { current } = await setupMounted({
          requests: {
            ...quietSources,
            runs: () => envelope(runWithPending()),
            canvas: () => envelope(canvasWithAuth()),
            runDetail: () => runDetailFixture(),
          },
          makeOptions: { viewPreferences: { authNotifyDir: tmpDir } },
        });
        await settle();
        await settle();
        const files = await fsPromises.readdir(tmpDir);
        const notifyFiles = files.filter((f) => f.startsWith('auth-notify-'));
        assert.equal(notifyFiles.length, 1, '0→N 触发生成一个通知文件');
        const raw = await fsPromises.readFile(path.join(tmpDir, notifyFiles[0]), 'utf8');
        const payload = JSON.parse(raw);
        assert.equal(payload.kind, 'auth_notification');
        assert.equal(payload.count, 1);
        assert.deepEqual(payload.sources, { Graph: 1 });
        for (const forbidden of ['标题', '摘要', '碎片', '授权单', '批准', '证据', '等待授权', '批准节点']) {
          assert.ok(!raw.includes(forbidden), `通知内容不含 ${forbidden}`);
        }
      } finally {
        await fsPromises.rm(tmpDir, { recursive: true, force: true });
      }
    });
  });

  describe('n8n 一键修复', () => {
    const allText = (el) => (el.textContent || '') + (el.children || []).map((c) => allText(c)).join(' ');
    const addFault = (fixture, name, path) => {
      const ul = fixture.dashboard.querySelector('.life-faults ul') || fixture.dashboard.createEl('section', { cls: 'life-faults' }).createEl('ul');
      const li = ul.createEl('li');
      li.setAttribute('data-path', path);
      li.createEl('strong', { text: name });
      li.createEl('small', { text: '2026-08-25 09:00' });
      return li;
    };

    it('n8n 故障单 → 一键修复按钮可见可点 → 请求文件写入（载荷正确）→ 回响', async () => {
      const tmpDir = await fsPromises.mkdtemp(path.join(os.tmpdir(), 'repair-req-'));
      try {
        const { current } = await setupMounted({
          beforeMount: (fixture) => {
            addFault(fixture, '看门狗：n8n 日报总调度器停滞', 'ops/看门狗/故障/active/2026-08-25_1-n8n_scheduler_stalled.md');
          },
          makeOptions: { viewPreferences: { repairRequestDir: tmpDir } },
        });
        const faults = current.mount.querySelector('.life-cosmos-todo-faults');
        assert.ok(faults, '故障分区存在');
        const repairBtn = faults.querySelector('.life-cosmos-todo-fault-repair');
        assert.ok(repairBtn, '一键修复按钮可见');
        repairBtn.click();
        await settle();
        await settle();
        await settle();
        // 请求文件写入（注入 /tmp 目录）
        const files = await fsPromises.readdir(tmpDir);
        const reqFiles = files.filter((f) => f.startsWith('repair-'));
        assert.equal(reqFiles.length, 1, '请求文件已写入');
        const payload = JSON.parse(await fsPromises.readFile(path.join(tmpDir, reqFiles[0]), 'utf8'));
        assert.equal(payload.kind, 'repair_request');
        assert.equal(payload.target, 'n8n');
        assert.equal(payload.action, 'restart');
        // say 回响
        const message = current.mount.querySelector('.life-cosmos-capsule-status');
        assert.ok(message && message.getAttribute('data-kind') === 'success', '提交回响 success');
        assert.ok(allText(message).includes('修复请求已提交'), '中文回响');
        // submitting 后禁用（已提交态）
        assert.notEqual(repairBtn.getAttribute('disabled'), null, '提交后按钮禁用');
      } finally {
        await fsPromises.rm(tmpDir, { recursive: true, force: true });
      }
    });

    it('非 n8n 故障单 → 不渲染一键修复按钮', async () => {
      const { current } = await setupMounted({
        beforeMount: (fixture) => {
          addFault(fixture, '看门狗：文件缺失检测', 'ops/看门狗/故障/active/2026-08-23_1-file_missing.md');
        },
      });
      const faults = current.mount.querySelector('.life-cosmos-todo-faults');
      assert.ok(faults, '故障分区存在');
      assert.equal(faults.querySelectorAll('.life-cosmos-todo-fault-repair').length, 0, '非 n8n 单子无修复按钮');
      const openBtn = faults.querySelector('.life-cosmos-todo-fault-open');
      assert.ok(openBtn, '打开按钮保留');
    });
  });

  describe('AUDIT-V2 修复验收', () => {
    const allText = (el) => (el.textContent || '') + (el.children || []).map((c) => allText(c)).join(' ');
    // shadow 信封（review/control/proposals 契约版本 1）
    const shadow = (data) => ({ status: 200, text: JSON.stringify({ contract_version: '1', db_mode: 'read_only', generated_at: '2026-08-25T00:00:00+00:00', service_version: 's', data }) });
    const addSignal = (fixture, title, path) => {
      const card = fixture.dashboard.createEl('section', { cls: 'life-panel life-signal-card' });
      if (path) card.setAttribute('data-path', path);
      card.createEl('strong', { text: title });
      return card;
    };

    it('V2-C1: 研究信号行 openNote 失败 → say 回响（不再静默）', async () => {
      const { current } = await setupMounted({
        beforeMount: (fixture) => {
          addSignal(fixture, '失效信号', '研究/不存在.md');
        },
        makeOptions: { missingFiles: ['研究/不存在.md'] },
        requests: {
          proposals: () => shadow({ items: [] }),
        },
      });
      // 切研究板块（信号行在 research board）
      [...current.mount.querySelectorAll('.life-cosmos-tools button')].find((b) => b.textContent === '运行监控与信息源').click();
      await settle();
      const row = current.mount.querySelector('.life-cb-signal-row');
      assert.ok(row, '信号行存在');
      row.click();
      await settle();
      const message = current.mount.querySelector('.life-cosmos-capsule-status');
      assert.ok(message && message.getAttribute('data-kind') === 'error', `openNote 失败回响 error（实际 kind=${message && message.getAttribute('data-kind')}）`);
    });

    it('V2-C5: 授权队列源断线 → 断线标注可见 + N 不含断线项', async () => {
      const { current } = await setupMounted({
        requests: {
          proposals: () => { throw new Error('service down'); },
          runs: () => envelope([]),
        },
      });
      // 首次 fetchAuthQueue 完成（review 源 error）后，手动刷新触发卡堆重建
      await settle();
      await settle();
      await settle();
      const refreshBtn = byAction(current.mount, 'graph.refresh');
      assert.ok(refreshBtn, '刷新按钮存在');
      refreshBtn.click();
      await settle();
      await settle();
      const offlineCard = [...current.mount.querySelectorAll('.life-cosmos-deck-card')]
        .find((el) => el.classes && el.classes.has('is-offline'));
      assert.ok(offlineCard, '断线标注卡存在');
      assert.ok(allText(offlineCard).includes('授权状态暂时不可用'), '断线标注文案可见');
      assert.ok(allText(offlineCard).includes('服务暂时不可用'), '来源断线标注可见');
      // N 不含断线项（只有 decisions 默认卡 1 张）
      const tag = current.mount.querySelector('.life-cb-tag');
      const n = Number((tag.textContent.match(/(\d+)/) || [])[1]);
      assert.equal(n, 1, `N 不含断线项（实际 ${tag.textContent}）`);
      assert.ok(!tag.textContent.includes('0 项待定'), '不静默显示 0 项待定误导');
    });

    it('V2-C5: 卡堆标题时间戳存在（最近同步）且随数据更新', async () => {
      const { current } = await setupMounted({
        requests: {
          proposals: () => shadow({ items: [] }),
        },
      });
      await settle();
      await settle();
      await settle();
      const tag = current.mount.querySelector('.life-cb-tag');
      assert.ok(tag.textContent.includes('最近同步'), `时间戳存在（实际 ${tag.textContent}）`);
    });
  });

});
