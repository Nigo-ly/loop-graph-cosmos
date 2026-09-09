// G4-00..04：Cosmos observer ownership filter 专项（Matrix G4-00..04）。
// 自 homepage-cosmos-mount.test.mjs 移出（G4-15 ratchet：旧文件不得净增长）。
// G4-00 口径：真实 Obsidian DomHelpers（HTMLElement.prototype.setText 的
// Obsidian 运行时实现）在本隔离 node 环境不可用（无 Obsidian 运行时，且
// 本任务禁止启动生产 Obsidian、禁止新增 jsdom 等依赖）——G4-00 修复前
// 真实语义基线记 ENV_BLOCKED，不计 PASS；下方 fake-dom 对照（本仓对
// Obsidian setText 语义的本地实现：清空 children + 赋值 textContent，
// 对应真实 DOM 的 childList mutation）仅作修复有效性旁证，不冒充基线。
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

let requestUrlHandler = async () => { throw new Error('requestUrl handler not installed'); };
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') {
    return {
      requestUrl: (options) => requestUrlHandler(options),
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
const { setupLoopConsole } = require(path.join(ROOT, 'src', 'console', 'register.js'));
const thoughtMap = require(path.join(ROOT, 'src', 'console', 'thought-map.js'));
Module._load = originalLoad;

// P0-1（R25）：隔离即保护——harness 默认注入临时消费目录，测试代码不再
// 触达真实看门狗目录。2026-08-27 起按 nigo 指令：测试套件连真实目录的
// 读取（快照/比对）也不做；证据 = 既有污染记录 + 注入后写入只落临时目录。
// 每个 makePlugin 实例默认隔离到独立的临时消费目录。
function isolatedConsumerDirs() {
  return {
    authNotifyDir: mkdtempSync(path.join(os.tmpdir(), 'p0-auth-notify-')),
    repairRequestDir: mkdtempSync(path.join(os.tmpdir(), 'p0-repair-req-')),
  };
}

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
}

function makePlugin({ getThoughtMap } = {}) {
  const cleanupCallbacks = [];
  const eventHandlers = {};
  const plugin = {
    registerView() {},
    addCommand() {},
    register(fn) {
      cleanupCallbacks.push(fn);
    },
    registerEvent() {},
    captureHomeScroll() {},
    openQuickCapture: async () => {},
    openContentManager: async () => {},
    openProjectAssessment: async () => {},
    toggleReadingTimer: async () => {},
    resetCurrentReadingTime: async () => {},
    setViewPreference: async () => {},
    setContentState: async () => {},
    completeTask: async () => {},
    requestFragmentExploration: async () => {},
    startExperiment: async () => {},
    openExperimentFeedback: async () => {},
    getViewPreferences: () => ({ filter: 'all', density: 'comfortable', ...isolatedConsumerDirs() }),
    app: {
      commands: { executeCommandById: async () => {} },
      workspace: {
        on: (event, fn) => {
          eventHandlers[event] = fn;
          return {};
        },
        onLayoutReady: () => {},
        getLeavesOfType: () => [],
        openLinkText: async () => {},
      },
      vault: {
        getAbstractFileByPath: (ref) => (ref ? { extension: 'md', path: ref } : null),
        read: async () => '原始内容',
      },
      metadataCache: {
        getFileCache: () => ({ frontmatter: { 'nigo-loop': true } }),
      },
    },
  };
  if (getThoughtMap) plugin.getThoughtMap = getThoughtMap;
  return { plugin, cleanupCallbacks, eventHandlers };
}

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
  const stat = stats.createDiv({ cls: 'life-stat' });
  stat.createSpan({ text: '待处理' });
  stat.createEl('strong', { text: '2' });
  dashboard.createEl('section', { cls: 'life-decision-bar' });
  return { root, mount, legacy, dashboard };
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


describe('G4 Cosmos observer ownership filter', () => {
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
  });

  afterEach(() => {
    globalThis.document = savedGlobals.document;
    globalThis.window = savedGlobals.window;
    globalThis.MutationObserver = savedGlobals.MutationObserver;
  });

  function triggerObserver(records) {
    const observer = FakeMutationObserver.instances[0];
    observer.callback(records, observer);
  }

  function pendingScanTimers() {
    // 只计 scanHomepage 的 debounce/retry 调度（300ms 起）；Cosmos 内部
    // 动画/状态 timeout（如 480ms）不属于扫描调度口径。
    return win.state.timeouts.filter(
      (timer) => !timer.cleared && !timer.fired && timer.ms >= 300 && timer.ms !== 480
    ).length;
  }

  it('G4-00 ENV_BLOCKED：真实 DomHelpers/HTMLElement 语义在本隔离环境不可用', () => {
    // 真实 HTMLElement.setText／Obsidian DomHelpers 语义需要 Obsidian 运行时
    // 或真实浏览器 DOM；本 node 隔离环境两者皆无，且本任务禁止启动生产
    // Obsidian、禁止新增 jsdom 等依赖。G4-00 修复前真实语义基线因此记
    // ENV_BLOCKED——不计 PASS；下方 fake-dom 对照仅作修复有效性的旁证，
    // 绝不升级为 G4-00 基线证据。
    assert.ok(true, 'G4-00 记 ENV_BLOCKED（不计 PASS）');
  });

  it('G4-00 旁证（非基线）：fake-dom 语义下 60 次 setText tick 产生 childList churn，旧回调逐次重扫', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const current = makeHome(doc);
    const clock = current.mount.createDiv({ cls: 'life-cosmos-clock' });
    const clockTime = clock.createEl('strong', { text: '--:--:--' });
    const mutationTypes = new Set();
    let legacyRescans = 0;
    let legacyCleanups = 0;
    // 修复前回调逐字对照（R22 前 register.js:1010-1023：仅过滤
    // .life-thought-map，其余一律 scheduleScan）：
    const legacyCallback = (records) => {
      legacyCleanups += 1;  // cleanupDisconnectedCosmosSessions() 无条件执行
      if (records.length && records.every((record) => record.target?.closest?.('.life-thought-map'))) return;
      legacyRescans += 1;  // 旧逻辑在此处 scheduleScan()
    };
    for (let index = 0; index < 60; index += 1) {
      clockTime.setText(`10:24:${String(index).padStart(2, '0')}`);  // setText 语义 tick
      // setText 清空 children 并写 textContent ⇒ childList mutation
      const record = { type: 'childList', target: clockTime, addedNodes: [], removedNodes: [] };
      mutationTypes.add(record.type);
      legacyCallback([record]);
    }
    assert.deepEqual([...mutationTypes], ['childList'], 'setText tick 产生 childList mutation');
    assert.equal(legacyCleanups, 60, '断开清理在旧回调中照常执行');
    assert.equal(legacyRescans, 60, 'fake 语义对照：旧回调逐次重扫（旁证，非 G4-00 基线）');
  });

  it('G4-01：真实挂载后 60 次 clock tick——零重扫、零 fetch、断开清理仍执行', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const current = makeHome(doc);
    let fetches = 0;
    requestUrlHandler = async () => { fetches += 1; return envelope([]); };
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    const status = await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(status, 'mounted');
    const intervalsBefore = win.liveIntervals();
    assert.ok(intervalsBefore >= 1, 'clock tick interval 已注册');
    const timersBefore = pendingScanTimers();
    const fetchesBefore = fetches;
    const clockTime = current.mount.querySelector('.life-cosmos-clock strong');
    assert.ok(clockTime, 'Cosmos clock 节点存在');
    for (let index = 0; index < 60; index += 1) {
      clockTime.setText(`10:25:${String(index).padStart(2, '0')}`);
      triggerObserver([{ type: 'childList', target: clockTime, addedNodes: [], removedNodes: [] }]);
    }
    assert.equal(pendingScanTimers(), timersBefore, 'scanHomepage 调度增加 0');
    assert.equal(fetches, fetchesBefore, 'fetch 增加 0');
    // 断开清理仍执行：root 断开后 observer 触发——session 资源被回收
    current.root.remove();
    triggerObserver([{ type: 'childList', target: clockTime, addedNodes: [], removedNodes: [] }]);
    assert.equal(win.liveIntervals(), 0, '断开 copy 的 interval 全部被清理');
  });

  it('G4-02：countdown/canvas/drawer/board 自有 churn 不重扫；外部 legacy 变化重扫一次', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const current = makeHome(doc);
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    const status = await harness.plugin.mountHomepageCosmos(current.root);
    await settle();
    assert.equal(status, 'mounted');
    const timersBefore = pendingScanTimers();
    const ownedTargets = [
      current.mount.querySelector('.life-cosmos-home'),
      current.mount.querySelector('.life-cosmos-clock'),
      current.mount.querySelector('.life-cb-viewport') || current.mount.querySelector('.life-cosmos-home'),
      current.mount.querySelector('.life-cosmos-drawer') || current.mount.querySelector('.life-cosmos-home'),
    ];
    for (const target of ownedTargets) {
      triggerObserver([{ type: 'childList', target, addedNodes: [], removedNodes: [] }]);
    }
    assert.equal(pendingScanTimers(), timersBefore, '自有区域 churn 一律不重扫');
    // 外部 legacy 变化：仍重扫一次（固定 debounce 调度一个新 timer）
    const legacyTarget = current.legacy.querySelector('.life-stat-strip');
    triggerObserver([{ type: 'childList', target: legacyTarget, addedNodes: [], removedNodes: [] }]);
    assert.equal(pendingScanTimers(), timersBefore + 1, '外部 legacy 变化仍重扫');
    // addedNodes 归属判定：Cosmos 挂载点新增自有子树不触发重扫
    const added = doc.createElement('div');
    added.addClass('life-cosmos-home');
    current.mount.appendChild(added);
    triggerObserver([{ type: 'childList', target: current.mount, addedNodes: [added], removedNodes: [] }]);
    assert.equal(pendingScanTimers(), timersBefore + 1, '自有 addedNodes churn 不追加重扫');
  });

  it('G4-04：两个 home copy，一个断开——只清理断开 session，存活 copy 不受影响', async () => {
    const doc = makeDocument();
    globalThis.document = doc;
    const homeOne = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const homeTwo = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    const first = buildHomepageRoot(homeOne);
    const second = buildHomepageRoot(homeTwo);
    assert.equal(await harness.plugin.mountHomepageCosmos(first.root), 'mounted');
    await settle();
    assert.equal(await harness.plugin.mountHomepageCosmos(second.root), 'mounted');
    await settle();
    const intervalsLive = win.liveIntervals();
    assert.ok(intervalsLive >= 2, '两个 copy 各有自己的 tick interval');
    const timersBefore = pendingScanTimers();
    // 断开第一个 copy：触发 observer（records 来自存活 copy 的自有 churn）
    first.root.remove();
    const secondClock = second.mount.querySelector('.life-cosmos-clock strong');
    triggerObserver([{ type: 'childList', target: secondClock, addedNodes: [], removedNodes: [] }]);
    assert.ok(win.liveIntervals() < intervalsLive, '断开 copy 的 interval 被回收');
    assert.ok(win.liveIntervals() >= 1, '存活 copy 的 interval 不受影响');
    assert.ok(
      second.mount.querySelectorAll('.life-cosmos-home').length === 1,
      '存活 copy 的 Cosmos 不被误清'
    );
    assert.equal(pendingScanTimers(), timersBefore, '断开清理不触发重扫');
  });
});
