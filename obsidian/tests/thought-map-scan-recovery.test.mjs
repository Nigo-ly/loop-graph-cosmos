// Recovery tests for the homepage thought-map scan loop in
// src/console/register.js. The scan runs inside Obsidian; here it is driven
// with a fake DOM, a manual timer queue, and a stubbed MutationObserver, so
// reload / late-DOM / re-render / first-collection-failure / empty-metadata
// scenarios can be replayed under plain Node.
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

// Stub the obsidian module so register.js can load under plain Node.
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') {
    return {
      requestUrl: async () => { throw new Error('requestUrl must not be called in scan tests'); },
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

const SCAN_DEBOUNCE_MS = 300;
const RETRY_DELAYS = [1000, 2000, 4000, 8000];
const MAX_ATTEMPTS = 5;

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

function emptyMap() {
  return thoughtMap.buildThoughtMap([]);
}

function makeTimers() {
  const timers = [];
  return {
    timers,
    window: {
      setTimeout: (fn, ms) => {
        const timer = { fn, ms, cleared: false, fired: false };
        timers.push(timer);
        return timer;
      },
      clearTimeout: (timer) => {
        if (timer) timer.cleared = true;
      },
    },
    pending() {
      return timers.filter((t) => !t.cleared && !t.fired);
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
  // Real MutationObserver callbacks receive (MutationRecord[], observer);
  // passing them here pins the regression where event arguments leaked into
  // the timer delay.
  trigger() {
    this.callback([{ type: 'childList', addedNodes: [], removedNodes: [] }], this);
  }
}

function makePlugin({ getThoughtMap }) {
  const layoutReadyCallbacks = [];
  const cleanupCallbacks = [];
  const eventHandlers = {};
  const plugin = {
    registerView() {},
    addCommand() {},
    register(fn) {
      cleanupCallbacks.push(fn);
    },
    registerEvent() {},
    // P0-1：默认隔离通知/修复写目录（register.js 默认路径即生产消费目录）。
    getViewPreferences: () => ({
      authNotifyDir: mkdtempSync(path.join(os.tmpdir(), 'p0-auth-notify-')),
      repairRequestDir: mkdtempSync(path.join(os.tmpdir(), 'p0-repair-req-')),
    }),
    app: {
      workspace: {
        on: (event, fn) => {
          eventHandlers[event] = fn;
          return {};
        },
        onLayoutReady: (fn) => layoutReadyCallbacks.push(fn),
        getLeavesOfType: () => [],
      },
    },
  };
  if (getThoughtMap) plugin.getThoughtMap = getThoughtMap;
  return { plugin, layoutReadyCallbacks, cleanupCallbacks, eventHandlers };
}

function homeDocument() {
  const doc = new FakeEl('document');
  doc.body = doc.createDiv({ cls: 'body' });
  const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
  const content = home.createDiv({ cls: 'life-dashboard-content' });
  content.createDiv({ cls: 'life-fragments' });
  return { doc, content };
}

function thoughtMapSections(doc) {
  return doc.querySelectorAll('.life-thought-map');
}

function removeThoughtMap(content) {
  content.children = content.children.filter((el) => !el.classes.has('life-thought-map'));
}

// Flush pending microtasks so the fire-and-forget startup scan settles.
async function settle() {
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

describe('homepage thought-map scan recovery', () => {
  let timers;
  let savedGlobals;

  beforeEach(() => {
    FakeMutationObserver.instances = [];
    timers = makeTimers();
    savedGlobals = {
      document: globalThis.document,
      window: globalThis.window,
      MutationObserver: globalThis.MutationObserver,
    };
    globalThis.window = timers.window;
    globalThis.MutationObserver = FakeMutationObserver;
  });

  afterEach(() => {
    globalThis.document = savedGlobals.document;
    globalThis.window = savedGlobals.window;
    globalThis.MutationObserver = savedGlobals.MutationObserver;
  });

  it('always debounces external events at 300ms and never lets event arguments become the timer delay', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();

    // MutationObserver callbacks receive (MutationRecord[], observer): neither
    // may leak into setTimeout as a delay (an array coerces to NaN → 0ms hot loop).
    FakeMutationObserver.instances[0].trigger();
    let pending = timers.pending();
    assert.equal(pending.length, 1);
    assert.equal(pending[0].ms, SCAN_DEBOUNCE_MS, 'mutation events use the fixed debounce');

    // layout-change handlers receive an event argument too.
    harness.eventHandlers['layout-change']({ type: 'layout-change' });
    pending = timers.pending();
    assert.equal(pending.length, 1, 'layout-change re-arms the single debounce timer');
    assert.equal(pending[0].ms, SCAN_DEBOUNCE_MS, 'layout-change uses the fixed debounce');
  });

  it('scans once at startup after a plugin reload, with no layout-ready callback and no DOM event', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    let calls = 0;
    const harness = makePlugin({
      getThoughtMap: async () => {
        calls += 1;
        return sampleMap();
      },
    });
    setupLoopConsole(harness.plugin);

    // The actual reload failure: layout was already ready, so the layout-ready
    // callback never fires again, and the static DOM produces no mutation.
    // The startup scan must not depend on either.
    await settle();
    assert.ok(calls >= 1, 'startup scan collects without any lifecycle event');
    assert.equal(thoughtMapSections(doc).length, 1, 'thought map is injected by the startup scan');
    assert.equal(timers.pending().length, 0, 'successful startup leaves no timer behind');
  });

  it('never injects Cosmos from the scan — the explicit mount protocol owns it', async () => {
    const { doc, content } = homeDocument();
    content.createDiv({ cls: 'life-thought-map' }).createDiv({ cls: 'life-thought-ring' });
    globalThis.document = doc;
    let calls = 0;
    const harness = makePlugin({
      getThoughtMap: async () => {
        calls += 1;
        return sampleMap();
      },
    });

    setupLoopConsole(harness.plugin);
    await settle();

    // 单一所有权边界：扫描只维护 legacy 内的思考地图，绝不注入 Cosmos；
    // 已有完整思考地图的主页没有待办，连投影都不必重读。
    assert.equal(calls, 0, 'a settled homepage needs no new collection');
    assert.equal(doc.querySelectorAll('.life-cosmos-home').length, 0, 'scan never injects Cosmos');
    assert.equal(thoughtMapSections(doc).length, 1, 'the existing thought map is not duplicated');

    // 外部 mutation 触发的重扫同样不走任何 Cosmos 注入/恢复路径。
    FakeMutationObserver.instances[0].trigger();
    await timers.pump();
    assert.equal(doc.querySelectorAll('.life-cosmos-home').length, 0, 'mutation scans never restore Cosmos');
  });

  it('retries a failed collection inside a bounded window and recovers without any DOM event', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    let calls = 0;
    const harness = makePlugin({
      getThoughtMap: async () => {
        calls += 1;
        if (calls === 1) throw new Error('provider not up yet');
        return sampleMap();
      },
    });
    setupLoopConsole(harness.plugin);
    await settle();

    assert.equal(calls, 1);
    assert.equal(thoughtMapSections(doc).length, 0);
    const delay = await timers.pump();
    assert.equal(delay, RETRY_DELAYS[0], 'first retry fires after the base delay');
    assert.equal(calls, 2);
    assert.equal(thoughtMapSections(doc).length, 1, 'thought map appears after the retry');
    assert.equal(timers.pending().length, 0, 'no further retry once injected');
  });

  it('stops after the bounded recovery window and only re-opens it on a real lifecycle event', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    let calls = 0;
    const harness = makePlugin({
      getThoughtMap: async () => {
        calls += 1;
        throw new Error('provider down');
      },
    });
    setupLoopConsole(harness.plugin);
    await settle();

    const delays = [];
    for (let i = 0; i < RETRY_DELAYS.length; i += 1) delays.push(await timers.pump());
    assert.deepEqual(delays, RETRY_DELAYS, 'retry delays back off geometrically');
    assert.equal(calls, MAX_ATTEMPTS, 'the window allows exactly five collections');
    assert.equal(timers.pending().length, 0, 'window exhausted: polling stops, no permanent 30s loop');
    assert.equal(thoughtMapSections(doc).length, 0);

    // A stable lifecycle event re-opens a fresh window starting from the base delay.
    FakeMutationObserver.instances[0].trigger();
    const debounce = await timers.pump();
    assert.equal(debounce, SCAN_DEBOUNCE_MS);
    assert.equal(calls, MAX_ATTEMPTS + 1, 'lifecycle event re-arms the scan');
    const retry = timers.pending()[0];
    assert.ok(retry, 'the fresh window schedules its own retry');
    assert.equal(retry.ms, RETRY_DELAYS[0], 'backoff restarts from the base delay');
  });

  it('does not inject an empty result while metadata may be unready, and recovers when it becomes ready', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    let calls = 0;
    const harness = makePlugin({
      getThoughtMap: async () => {
        calls += 1;
        return calls === 1 ? emptyMap() : sampleMap();
      },
    });
    setupLoopConsole(harness.plugin);
    await settle();

    // First collection "succeeds" with total=0 (metadata cache not ready):
    // injecting it would freeze the homepage behind an existing section.
    assert.equal(thoughtMapSections(doc).length, 0, 'empty result is not injected during the window');
    const delay = await timers.pump();
    assert.equal(delay, RETRY_DELAYS[0], 'empty result schedules a retry like a failure');
    assert.equal(thoughtMapSections(doc).length, 1, 'ready metadata injects the real map');
    assert.equal(doc.querySelectorAll('.life-empty-state').length, 0, 'no empty state survives');
    assert.equal(doc.querySelectorAll('.life-thought-ring').length, 1);
  });

  it('settles into a replaceable empty state when the window exhausts on empty results, then upgrades instead of freezing', async () => {
    const { doc, content } = homeDocument();
    globalThis.document = doc;
    let calls = 0;
    const harness = makePlugin({
      getThoughtMap: async () => {
        calls += 1;
        return calls <= MAX_ATTEMPTS ? emptyMap() : sampleMap();
      },
    });
    setupLoopConsole(harness.plugin);
    await settle();

    for (let i = 0; i < RETRY_DELAYS.length; i += 1) await timers.pump();
    assert.equal(calls, MAX_ATTEMPTS);
    assert.equal(thoughtMapSections(doc).length, 1, 'exhausted window settles into the empty state');
    assert.equal(doc.querySelectorAll('.life-empty-state').length, 1);
    assert.ok(doc.text.includes('记录一条碎片后，它会出现在这里'));
    assert.equal(timers.pending().length, 0, 'no polling after settling');

    // Metadata becomes ready later; a lifecycle event must upgrade the empty
    // state in place rather than leaving the homepage frozen behind it.
    FakeMutationObserver.instances[0].trigger();
    await timers.pump();
    assert.equal(thoughtMapSections(doc).length, 1, 'still exactly one section after the upgrade');
    assert.equal(doc.querySelectorAll('.life-empty-state').length, 0, 'empty placeholder is replaced');
    assert.equal(doc.querySelectorAll('.life-thought-ring').length, 1, 'real content is injected');
    assert.ok(content.text.includes('未来 AI 公司业务架构设想'));
  });

  it('keeps a legitimately empty homepage stable without burning retry windows', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    let calls = 0;
    const harness = makePlugin({
      getThoughtMap: async () => {
        calls += 1;
        return emptyMap();
      },
    });
    setupLoopConsole(harness.plugin);
    await settle();
    for (let i = 0; i < RETRY_DELAYS.length; i += 1) await timers.pump();
    assert.equal(doc.querySelectorAll('.life-empty-state').length, 1, 'empty state settled');

    // Any later scan that still collects empty must recognize the settled
    // empty state and stop immediately instead of opening a new retry window.
    FakeMutationObserver.instances[0].trigger();
    await timers.pump();
    assert.equal(timers.pending().length, 0, 'settled empty state does not re-trigger retries');
    assert.equal(doc.querySelectorAll('.life-empty-state').length, 1);
  });

  it('re-injects the map after the homepage re-renders and wipes the section', async () => {
    const { doc, content } = homeDocument();
    globalThis.document = doc;
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    assert.equal(thoughtMapSections(doc).length, 1);

    removeThoughtMap(content);
    FakeMutationObserver.instances[0].trigger();
    const debounce = await timers.pump();
    assert.equal(debounce, SCAN_DEBOUNCE_MS);
    assert.equal(thoughtMapSections(doc).length, 1, 're-render triggers a re-injection');
  });

  it('keeps Cosmos out of the scan path even when the thought map collection fails after a re-render wipe', async () => {
    const { doc, content } = homeDocument();
    globalThis.document = doc;
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    assert.equal(thoughtMapSections(doc).length, 1, 'thought map injected at startup');
    assert.equal(doc.querySelectorAll('.life-cosmos-home').length, 0, 'startup scan injects no Cosmos');

    // DataviewJS 整体重渲染把思考地图抹掉；收集持续失败时扫描只保留思考
    // 地图自己的有界重试窗口，Cosmos 既不陪葬也不由扫描重建——它只随下一轮
    // Dataview 渲染末尾的显式 mount 握手回归。
    content.children = content.children.filter(() => false);
    content.createDiv({ cls: 'life-fragments' });

    harness.plugin.getThoughtMap = async () => { throw new Error('provider down'); };
    FakeMutationObserver.instances[0].trigger();
    await timers.pump();
    assert.equal(doc.querySelectorAll('.life-cosmos-home').length, 0, 'scan never restores Cosmos');
    assert.equal(thoughtMapSections(doc).length, 0, 'thought map still waits for a real projection');
    assert.equal(timers.pending().length, 1, 'thought map keeps its own bounded retry window');
  });

  it('cancels the pending retry on unload, and a reloaded plugin scans independently', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const first = makePlugin({
      getThoughtMap: async () => {
        throw new Error('provider not up yet');
      },
    });
    setupLoopConsole(first.plugin);
    await settle();
    assert.equal(timers.pending().length, 1, 'failed startup collection leaves one pending retry');

    for (const cleanup of first.cleanupCallbacks) cleanup();
    assert.equal(timers.pending().length, 0, 'unload cancels the pending retry');
    assert.ok(FakeMutationObserver.instances[0].disconnected, 'unload disconnects the observer');

    const second = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(second.plugin);
    await settle();
    assert.equal(FakeMutationObserver.instances.length, 2, 'reload installs a fresh observer');
    assert.equal(thoughtMapSections(doc).length, 1, 'reloaded plugin injects the map');
  });

  it('ignores thought-map internal DOM churn but still scans on external homepage changes', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const harness = makePlugin({ getThoughtMap: async () => sampleMap() });
    setupLoopConsole(harness.plugin);
    await settle();
    assert.equal(timers.pending().length, 0, 'startup settles with no pending scan');

    const observer = FakeMutationObserver.instances[0];
    // 思考地图内部 churn（环旋转、详情重绘）：record.target 落在 .life-thought-map
    // 内，必须被过滤——否则环的每帧交互都会触发全页重扫（曾表现为主页跳屏）。
    observer.callback([{ type: 'childList', target: { closest: (sel) => (sel === '.life-thought-map' ? {} : null) } }]);
    assert.equal(timers.pending().length, 0, 'thought-map internal churn must not schedule a scan');

    // 外部主页变化：target 不在思考地图内，仍按固定防抖排期扫描。
    observer.callback([{ type: 'childList', target: { closest: () => null } }]);
    const pending = timers.pending();
    assert.equal(pending.length, 1, 'external homepage changes still schedule a scan');
    assert.equal(pending[0].ms, SCAN_DEBOUNCE_MS);
  });
});
