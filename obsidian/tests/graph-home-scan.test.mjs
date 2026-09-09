// Concurrency tests for the Graph homepage card wiring in
// src/console/register.js. The wiring runs inside Obsidian; here it is driven
// with a fake DOM, a manual timer queue, a stubbed MutationObserver and a
// deferred obsidian requestUrl transport, so startup-scan / layout-ready /
// MutationObserver / layout-change / manual-refresh interleavings can be
// replayed under plain Node. Only the Graph /graph/v1/runs calls are
// deferred; the product-review fetch (same scan loop, out of scope here) gets
// an immediate empty response.
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

// Per-test requestUrl handler; the obsidian stub delegates to whatever the
// active test installed before setupLoopConsole runs.
let requestUrlHandler = async () => { throw new Error('requestUrl handler not installed'); };

// Stub the obsidian module so register.js can load under plain Node.
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
Module._load = originalLoad;

const SCAN_DEBOUNCE_MS = 300;

function graphRun(overrides = {}) {
  return {
    run_id: 'exec:test-1',
    graph_id: 'fragment-cognitive-graph-v1',
    spec_version: '1.0.0',
    spec_digest: 'a'.repeat(64),
    fragment_ref: 'fixture:fragment:test',
    status: 'running',
    current_node: 'input_fragment',
    step_count: 3,
    sequence: 1,
    pending_human: [],
    blocked_reason: null,
    started_at: '2026-08-04T00:00:00+00:00',
    updated_at: '2026-08-04T00:01:00+00:00',
    ...overrides,
  };
}

function envelope(data) {
  return { status: 200, text: JSON.stringify({ contract_version: '2', data }) };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// Counts only Graph runs-list calls; every other URL resolves immediately so
// the product-review branch of the scan never interferes.
function makeTransport() {
  const state = { graphCalls: 0, gate: null };
  requestUrlHandler = (options) => {
    if (String(options.url).includes('/graph/v1/runs')) {
      state.graphCalls += 1;
      if (state.gate) return state.gate.promise;
      return Promise.resolve(envelope([graphRun()]));
    }
    return Promise.resolve(envelope([]));
  };
  return state;
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
  trigger() {
    this.callback([{ type: 'childList', addedNodes: [], removedNodes: [] }], this);
  }
}

function makePlugin() {
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
    // P0-1：默认隔离通知/修复写目录（真实消费目录由套件级保护门兜底）。
    getViewPreferences: () => isolatedConsumerDirs(),
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
  // No getThoughtMap: the scan returns right after the Graph card section, so
  // these tests isolate the Graph wiring.
  return { plugin, layoutReadyCallbacks, cleanupCallbacks, eventHandlers };
}

function homeDocument() {
  const doc = new FakeEl('document');
  doc.body = doc.createDiv({ cls: 'body' });
  const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
  const content = home.createDiv({ cls: 'life-dashboard-content' });
  content.createDiv({ cls: 'life-decision-bar' });
  return { doc, content };
}

// Flush pending microtasks so the fire-and-forget startup scan settles.
async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

describe('graph homepage fetch concurrency', () => {
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
    requestUrlHandler = async () => { throw new Error('requestUrl handler not installed'); };
  });

  it('shares one in-flight listRuns across the startup scan, layout-ready, MutationObserver and layout-change', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const transport = makeTransport();
    transport.gate = deferred(); // 5684 response stays in flight
    const harness = makePlugin();
    setupLoopConsole(harness.plugin);
    await settle();
    assert.equal(transport.graphCalls, 1, 'startup scan fetches exactly once');
    assert.ok(doc.text.includes('正在读取 Graph 运行摘要'), 'card shows the loading state while in flight');

    // layout-ready fires while the first fetch is still in flight.
    for (const cb of harness.layoutReadyCallbacks) cb();
    await settle();
    assert.equal(transport.graphCalls, 1, 'layout-ready shares the in-flight fetch');

    // 反例：MutationObserver 与启动扫描并发——debounce 后的重扫只复用缓存，
    // 绝不发起第二次 listRuns()。
    FakeMutationObserver.instances[0].trigger();
    const debounce = await timers.pump();
    assert.equal(debounce, SCAN_DEBOUNCE_MS);
    await settle();
    assert.equal(transport.graphCalls, 1, 'MutationObserver rescan must not start a second listRuns');

    // layout-change behaves the same way.
    harness.eventHandlers['layout-change']({ type: 'layout-change' });
    await timers.pump();
    await settle();
    assert.equal(transport.graphCalls, 1, 'layout-change rescan must not start a second listRuns');

    transport.gate.resolve(envelope([graphRun({ status: 'running' })]));
    await settle();
    assert.equal(transport.graphCalls, 1);
    assert.ok(doc.text.includes('进行中'), 'the single response renders the ready state');
  });

  it('never refetches on DOM rescans after the first load — no TTL, no polling', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const transport = makeTransport(); // resolves immediately
    const harness = makePlugin();
    setupLoopConsole(harness.plugin);
    await settle();
    assert.equal(transport.graphCalls, 1, 'first load fetches once');
    assert.ok(doc.text.includes('进行中'));

    for (let round = 0; round < 3; round += 1) {
      FakeMutationObserver.instances[0].trigger();
      await timers.pump();
      await settle();
    }
    assert.equal(transport.graphCalls, 1, 'repeated rescans reuse the cache and never refetch');
    assert.equal(timers.pending().length, 0, 'no timers are left behind: no polling');
  });

  it('manual refresh shares the in-flight fetch, and only a later click starts a new one', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const transport = makeTransport();
    transport.gate = deferred();
    setupLoopConsole(makePlugin().plugin);
    await settle();
    assert.equal(transport.graphCalls, 1);

    // 用户在首个请求仍在途时点击「刷新」：共享同一个 in-flight Promise。
    doc.querySelector('.life-graph-home-refresh').click();
    await settle();
    assert.equal(transport.graphCalls, 1, 'refresh during an in-flight fetch must not double the request');

    transport.gate.resolve(envelope([graphRun({ status: 'running' })]));
    await settle();
    assert.ok(doc.text.includes('进行中'));

    // 取数结束后的再次点击是唯一另一个主动取数入口。
    transport.gate = null;
    doc.querySelector('.life-graph-home-refresh').click();
    await settle();
    assert.equal(transport.graphCalls, 2, 'a settled refresh click fetches again exactly once');
  });

  it('a late result after unload never touches state or DOM', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const transport = makeTransport();
    transport.gate = deferred();
    const harness = makePlugin();
    setupLoopConsole(harness.plugin);
    await settle();
    assert.equal(transport.graphCalls, 1);
    assert.ok(doc.text.includes('正在读取 Graph 运行摘要'));

    for (const cleanup of harness.cleanupCallbacks) cleanup();
    transport.gate.resolve(envelope([graphRun({ status: 'blocked' })]));
    await settle();
    assert.ok(doc.text.includes('正在读取 Graph 运行摘要'), 'late result must not re-render the card after unload');
    assert.ok(!doc.text.includes('异常停止'), 'late result must not update the card state after unload');
  });

  it('keeps an unreachable service as a compact error until the user explicitly refreshes', async () => {
    const { doc } = homeDocument();
    globalThis.document = doc;
    const transport = makeTransport();
    let failGraph = true;
    const baseHandler = requestUrlHandler;
    requestUrlHandler = (options) => {
      if (String(options.url).includes('/graph/v1/runs')) {
        if (failGraph) {
          transport.graphCalls += 1;
          return Promise.reject(new Error('connect ECONNREFUSED'));
        }
        return baseHandler(options);
      }
      return baseHandler(options);
    };
    setupLoopConsole(makePlugin().plugin);
    await settle();
    assert.equal(transport.graphCalls, 1);
    assert.ok(doc.text.includes('Graph 服务暂时不可用'), 'honest error state');

    // 自动路径绝不重试：DOM 事件只重新注入缓存的错误形态。
    FakeMutationObserver.instances[0].trigger();
    await timers.pump();
    await settle();
    assert.equal(transport.graphCalls, 1, 'no automatic retry after an error');

    // 用户点击「刷新」才会再取一次。
    failGraph = false;
    doc.querySelector('.life-graph-home-refresh').click();
    await settle();
    assert.equal(transport.graphCalls, 2, 'manual refresh recovers explicitly');
    assert.ok(doc.text.includes('进行中'), 'recovered state renders');
  });
});
