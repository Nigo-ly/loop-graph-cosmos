// R25 授权队列返修专项（gate_bc0f4580468f 批准的版本化迁移，自
// homepage-cosmos-mount.test.mjs 迁出以恢复 G4-15 行数基线）：
// P1-5 部分失败可见性 / P1-6 single-flight dirty 重跑 / P1-7 修复反馈 /
// P1-8 撤回文案一致性。harness 与 mount 套件同形（复用 helpers/fake-dom.mjs；
// 零新框架、零新依赖、零重复测试——原文件已删除本 describe）。
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { promises as fsPromises, mkdtempSync } from 'node:fs';
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
Module._load = originalLoad;

// Layout double：开启 getBoundingClientRect 使 Cosmos 走真实布局路径；
// node --test 每文件独立进程，prototype patch 不外泄。
FakeEl.prototype.getBoundingClientRect = function () {
  return { width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600 };
};
const absorbingContext = new Proxy(function () {}, {
  get: (target, prop) => (prop === Symbol.toPrimitive ? () => 0 : absorbingContext),
  apply: () => absorbingContext,
  set: () => true,
});
FakeEl.prototype.getContext = function () { return absorbingContext; };

// P0-1：隔离即保护——默认注入临时消费目录，测试不触达真实看门狗目录。
function isolatedConsumerDirs() {
  return {
    authNotifyDir: mkdtempSync(path.join(os.tmpdir(), 'p0-auth-notify-')),
    repairRequestDir: mkdtempSync(path.join(os.tmpdir(), 'p0-repair-req-')),
  };
}

function envelope(data) {
  return { status: 200, text: JSON.stringify({ contract_version: '2', data }) };
}

function loopEnvelope(items) {
  return {
    status: 200,
    text: JSON.stringify({
      contract_version: '2',
      db_mode: 'read_only',
      generated_at: '2026-09-02T00:00:00+00:00',
      provider_version: 'test',
      source_sequence: 1,
      source_committed_at: '2026-09-02T00:00:00+00:00',
      stale: false,
      items,
    }),
  };
}

// 手工定时器窗口：扫描防抖定时器绝不自动触发（调用计数断言的决定性前提）。
function makeWindow() {
  return {
    setTimeout: () => ({}),
    clearTimeout: () => {},
    setInterval: () => ({}),
    clearInterval: () => {},
    requestAnimationFrame: () => ({}),
    cancelAnimationFrame: () => {},
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
  disconnect() { this.disconnected = true; }
  trigger() {
    this.callback([{ type: 'childList', addedNodes: [], removedNodes: [] }], this);
  }
}

function makePlugin({ getThoughtMap, missingFiles, viewPreferences } = {}) {
  const cleanupCallbacks = [];
  const plugin = {
    registerView() {},
    addCommand() {},
    register(fn) { cleanupCallbacks.push(fn); },
    registerEvent() {},
    captureHomeScroll() {},
    getViewPreferences: () => ({ filter: 'all', density: 'comfortable', ...isolatedConsumerDirs(), ...(viewPreferences || {}) }),
    app: {
      commands: { executeCommandById: async () => {} },
      workspace: {
        on: () => ({}),
        onLayoutReady: () => {},
        getLeavesOfType: () => [],
        openLinkText: async () => {},
      },
      vault: {
        getAbstractFileByPath: (ref) => (ref && missingFiles && missingFiles.includes(ref) ? null : (ref ? { extension: 'md', path: ref } : null)),
        read: async () => '原始内容',
      },
      metadataCache: { getFileCache: () => ({ frontmatter: { 'nigo-loop': true } }) },
    },
  };
  if (getThoughtMap) plugin.getThoughtMap = getThoughtMap;
  return { plugin, cleanupCallbacks };
}

// 与 mount 套件同形的主页结构（Dataview 模板所有）：mount + 折叠 legacy。
function buildHomepageRoot(home) {
  const root = home.createDiv({ cls: 'life-home' });
  const mount = root.createDiv({ cls: 'life-cosmos-mount' });
  mount.setAttribute('data-life-cosmos-mount', '1');
  const legacy = root.createDiv({ cls: 'life-cosmos-legacy' });
  const bar = legacy.createEl('section', { cls: 'life-command-bar' });
  bar.createDiv({ cls: 'life-command-search' });
  bar.createDiv({ cls: 'life-command-actions' });
  const dashboard = legacy.createEl('section', { cls: 'life-dashboard-content' });
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

async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

// FakeEl 的 matches 只支持属性存在性，不支持 [attr="value"]：按值过滤。
function byAction(root, actionId) {
  return [...root.querySelectorAll('[data-cosmos-action]')]
    .find((el) => el.getAttribute('data-cosmos-action') === actionId) || null;
}

// 试点桥在 mount 时取预案：给一个最小合法形状（本套件不断言其内容）。
const PILOT_PLAN = {
  plan_version: '1', spec_id: 'fragment-pilot-v1', spec_digest: 'c'.repeat(64),
  task_label: '碎片试点', node_count: 3, human_gates: 2, max_feedback: 1,
  max_total_calls: 2, cost_cap_cny: 0.5, provider: 'kimi', model: 'k3',
  node_flow: '收集 → 判断 → 输出', expected_output: '研究结论',
  write_scope: '仅研究报告', create_behavior: '创建后等待人工授权',
};

// 统一请求路由：按 URL 前缀分派到各夹具。
function routeRequests(overrides = {}) {
  requestUrlHandler = async (options) => {
    const url = String(options.url);
    if (url.includes('/fragment/v1/alignments')) return envelope([]);
    if (url.includes('/fragment/v1/continuations')) return envelope([]);
    if (url.includes('/fragment/v1/product-reviews')) return envelope([]);
    if (url.includes('/graph/v1/pilot-plans')) return envelope(PILOT_PLAN);
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
    if (url.endsWith('/control/v1/intents') && options.method === 'POST') {
      return overrides.controlIntents ? overrides.controlIntents(options) : envelope(null);
    }
    if (url.includes('/control/v1/actions/')) {
      return overrides.controlActions ? overrides.controlActions(options) : envelope({ run_id: 'none', status: 'running', actions: {} });
    }
    if (url.includes('/proposals')) {
      return overrides.proposals ? overrides.proposals(options) : envelope({ items: [] });
    }
    return envelope([]);
  };
}

async function setupMounted({ requests, beforeMount, makeOptions } = {}) {
  const doc = makeDocument();
  globalThis.document = doc;
  const home = doc.body.createDiv({ cls: 'my-life-homepage-view' });
  routeRequests(requests);
  const harness = makePlugin({ getThoughtMap: async () => ({ total: 0, counts: {}, categories: [] }), ...(makeOptions || {}) });
  setupLoopConsole(harness.plugin);
  await settle();
  const current = buildHomepageRoot(home);
  if (beforeMount) beforeMount(current);
  await harness.plugin.mountHomepageCosmos(current.root);
  await settle();
  return { doc, home, harness, current };
}

let savedGlobals;

beforeEach(() => {
  FakeMutationObserver.instances = [];
  savedGlobals = {
    document: globalThis.document,
    window: globalThis.window,
    MutationObserver: globalThis.MutationObserver,
  };
  globalThis.window = makeWindow();
  globalThis.MutationObserver = FakeMutationObserver;
  requestUrlHandler = async () => envelope([]);
});

afterEach(() => {
  globalThis.document = savedGlobals.document;
  globalThis.window = savedGlobals.window;
  globalThis.MutationObserver = savedGlobals.MutationObserver;
});

  describe('R25 返修反例（P1-5 部分失败 / P1-6 dirty 重跑 / P1-7 修复反馈 / P1-8 撤回文案）', () => {
    // FakeEl textContent 不聚合子元素——递归取全文（本 describe 局部）。
    const allText = (el) => (el.textContent || '') + (el.children || []).map((c) => allText(c)).join(' ');
    const shadow = (data) => ({ status: 200, text: JSON.stringify({ contract_version: '1', db_mode: 'read_only', generated_at: '2026-08-25T00:00:00+00:00', service_version: 's', data }) });
    const runItem = (id) => ({
      run_id: id, graph_id: 'fragment-pilot-v1', spec_digest: 'c'.repeat(64),
      status: 'human_wait', sequence: 7, step_count: 3,
      pending_human: ['pilot_gate'], blocked_reason: null,
      started_at: '2026-08-25T00:00:00+00:00', updated_at: '2026-08-25T00:01:00+00:00',
    });
    function canvasWithAuth(runId = 'exec:auth:1') {
      return {
        canvas_version: '1',
        run_id: runId,
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
              // R25 P1-10：有效授权夹具用稳定远未来（不随墙钟过期）；
              // 过期反例见既有 canvasExpired（2020）测试。
              issued_at: '2026-08-25T00:00:00+00:00', expires_at: '2099-01-01T00:00:00+00:00', price_snapshot_source: null,
              input_fields: { title: '核验目标', core_judgment: '核心判断', user_value: '用户价值', card_titles: ['卡片一'] },
            },
          }, output_digest: null, error_code: null, completed_at: null },
        ],
        edges: [],
      };
    }
    const quietSources = {
      proposals: () => shadow({ items: [] }),
      reviewActions: () => shadow({ proposal_id: 'none', submittable: false }),
      controlActions: () => shadow({ run_id: 'none', status: 'running', actions: {} }),
    };
    const addFault = (fixture, name, path) => {
      const ul = fixture.dashboard.querySelector('.life-faults ul') || fixture.dashboard.createEl('section', { cls: 'life-faults' }).createEl('ul');
      const li = ul.createEl('li');
      li.setAttribute('data-path', path);
      li.createEl('strong', { text: name });
      li.createEl('small', { text: '2026-08-25 09:00' });
      return li;
    };

    it('Loop 控制只使用 Loop runs，且只查询可恢复状态与一键推进动作', async () => {
      const actionRunIds = [];
      const { current } = await setupMounted({
        requests: {
          ...quietSources,
          runs: () => envelope([{
            ...runItem('exec:graph:1'),
            pending_human: [],
            status: 'running',
          }]),
          loopRuns: () => loopEnvelope([
            { run_id: 'exec:loop:paused', loop_id: 'fragment-cognitive-v1', status: 'paused', display_state: 'awaiting_human' },
            { run_id: 'exec:loop:running', loop_id: 'fragment-cognitive-v1', status: 'running', display_state: 'running' },
          ]),
          controlActions: (options) => {
            const runId = decodeURIComponent(String(options.url)).split('/').pop();
            actionRunIds.push(runId);
            if (runId !== 'exec:loop:paused') throw new Error(`wrong run domain: ${runId}`);
            return shadow({
              run_id: runId,
              status: 'paused',
              latest_sequence: 3,
              priority: 0,
              actions: {
                resume: { enabled: true },
                terminate: { enabled: true },
                priority: { enabled: true },
              },
            });
          },
        },
      });
      assert.ok(actionRunIds.length > 0, '可恢复的 Loop run 会查询 Control 权威动作');
      assert.deepEqual([...new Set(actionRunIds)], ['exec:loop:paused'], '不得把 Graph ID 或普通 running 历史交给 Loop Control');
      const cards = [...current.mount.querySelectorAll('.life-cosmos-deck-card')];
      assert.ok(cards.some((el) => allText(el).includes('[Loop]') && allText(el).includes('等待授权')), 'Loop 恢复卡出现');
      assert.ok(!cards.some((el) => allText(el).includes('部分数据不可用')), '正确数据域不得误报 partial');
    });

    it('P1-5 Graph 单 run canvas 失败 → 已取得卡保留 + partial 标注 + ≥N + 通知零写入', async () => {
      const tmpDir = await fsPromises.mkdtemp(path.join(os.tmpdir(), 'p15-notify-'));
      try {
        const { current } = await setupMounted({
          requests: {
            ...quietSources,
            runs: () => envelope([runItem('exec:auth:1'), runItem('exec:auth:2')]),
            canvas: (options) => {
              if (decodeURIComponent(String(options.url)).includes('exec:auth:2')) throw new Error('canvas down');
              return envelope(canvasWithAuth('exec:auth:1'));
            },
          },
          makeOptions: { viewPreferences: { authNotifyDir: tmpDir } },
        });
        await settle(); await settle(); await settle();
        const cards = [...current.mount.querySelectorAll('.life-cosmos-deck-card')];
        assert.ok(cards.some((el) => allText(el).includes('等待授权')), '已取得源的授权卡保留');
        const marker = cards.find((el) => allText(el).includes('部分数据不可用'));
        assert.ok(marker, 'partial 标注可见（[Graph] · 部分数据不可用）');
        const tag = current.mount.querySelector('.life-cb-tag');
        assert.ok(tag.textContent.includes('≥'), `未知数量渲染为下界（实际 ${tag.textContent}）`);
        const files = await fsPromises.readdir(tmpDir);
        assert.deepEqual(files, [], '部分失败不得触发通知写入');
      } finally {
        await fsPromises.rm(tmpDir, { recursive: true, force: true });
      }
    });

    it('P1-5 review 单 proposal actionsFor 失败 → partial 标注 + 通知零写入', async () => {
      const tmpDir = await fsPromises.mkdtemp(path.join(os.tmpdir(), 'p15-notify-'));
      try {
        const { current } = await setupMounted({
          requests: {
            ...quietSources,
            proposals: () => shadow({ items: [
              { proposal_id: 'p-1', subject: '主题一', run_id: 'run-1' },
              { proposal_id: 'p-2', subject: '主题二', run_id: 'run-2' },
            ] }),
            reviewActions: (options) => {
              if (String(options.url).includes('p-2')) throw new Error('review down');
              return shadow({ proposal_id: 'p-1', submittable: true, reason_code: null, proposal_fingerprint: 'fp-abc', source_sequence: 7, proposal_status: 'blocked', current_decision: null });
            },
          },
          makeOptions: { viewPreferences: { authNotifyDir: tmpDir } },
        });
        await settle(); await settle(); await settle();
        const cards = [...current.mount.querySelectorAll('.life-cosmos-deck-card')];
        assert.ok(cards.some((el) => allText(el).includes('主题一')), '已取得 proposal 的卡保留');
        assert.ok(!cards.some((el) => allText(el).includes('主题二')), '失败子请求的卡不出现（无材料）');
        assert.ok(cards.some((el) => allText(el).includes('部分数据不可用')), 'partial 标注可见');
        const tag = current.mount.querySelector('.life-cb-tag');
        assert.ok(tag.textContent.includes('≥'), `未知数量渲染为下界（实际 ${tag.textContent}）`);
        assert.deepEqual(await fsPromises.readdir(tmpDir), [], '部分失败不得触发通知写入');
      } finally {
        await fsPromises.rm(tmpDir, { recursive: true, force: true });
      }
    });

    it('P1-5 control 单 run actionsFor 失败 + 可见 0 卡 → partial 标注且失败源不得清零通知状态', async () => {
      const tmpDir = await fsPromises.mkdtemp(path.join(os.tmpdir(), 'p15-notify-'));
      // 预置冷却 sidecar：lastCount=2 —— 若失败源被当作 0 归零，sidecar 会被重写。
      const sidecar = path.join(tmpDir, '.last-notify.json');
      const sidecarBefore = JSON.stringify({ lastCount: 2, lastNotifyAt: Date.now() });
      await fsPromises.writeFile(sidecar, sidecarBefore);
      try {
        const { current } = await setupMounted({
          requests: {
            ...quietSources,
            loopRuns: () => loopEnvelope([
              { run_id: 'exec:ctrl:1', loop_id: 'fragment-cognitive-v1', status: 'paused', display_state: 'awaiting_human' },
              { run_id: 'exec:ctrl:2', loop_id: 'fragment-cognitive-v1', status: 'blocked', display_state: 'blocked' },
            ]),
            controlActions: (options) => {
              if (decodeURIComponent(String(options.url)).includes('exec:ctrl:2')) throw new Error('control down');
              return shadow({ run_id: 'exec:ctrl:1', status: 'paused', latest_sequence: 3, priority: 0, actions: {} });
            },
          },
          makeOptions: { viewPreferences: { authNotifyDir: tmpDir } },
        });
        await settle(); await settle(); await settle();
        const cards = [...current.mount.querySelectorAll('.life-cosmos-deck-card')];
        assert.ok(cards.some((el) => allText(el).includes('部分数据不可用')), 'partial 标注可见');
        const tag = current.mount.querySelector('.life-cb-tag');
        assert.ok(tag.textContent.includes('≥'), `未知数量渲染为下界而非确定 0（实际 ${tag.textContent}）`);
        assert.equal(await fsPromises.readFile(sidecar, 'utf8'), sidecarBefore, '失败源不得用于清零通知状态（sidecar 零写入）');
      } finally {
        await fsPromises.rm(tmpDir, { recursive: true, force: true });
      }
    });

    it('P1-5 混合：review 源整体 error + Graph 正常 → 卡保留 + 断线标注 + ≥N + 通知零写入', async () => {
      const tmpDir = await fsPromises.mkdtemp(path.join(os.tmpdir(), 'p15-notify-'));
      try {
        const { current } = await setupMounted({
          requests: {
            ...quietSources,
            proposals: () => { throw new Error('service down'); },
            runs: () => envelope([runItem('exec:auth:1')]),
            canvas: () => envelope(canvasWithAuth('exec:auth:1')),
          },
          makeOptions: { viewPreferences: { authNotifyDir: tmpDir } },
        });
        await settle(); await settle(); await settle();
        const cards = [...current.mount.querySelectorAll('.life-cosmos-deck-card')];
        assert.ok(cards.some((el) => allText(el).includes('等待授权')), '正常来源的卡保留');
        assert.ok(cards.some((el) => allText(el).includes('授权状态暂时不可用')), 'error 源断线标注可见');
        const tag = current.mount.querySelector('.life-cb-tag');
        assert.ok(tag.textContent.includes('≥'), `未知数量渲染为下界（实际 ${tag.textContent}）`);
        assert.deepEqual(await fsPromises.readdir(tmpDir), [], '失败源不得计入通知（零写入）');
      } finally {
        await fsPromises.rm(tmpDir, { recursive: true, force: true });
      }
    });

    it('P1-6 列表变化期间 auth 取数在途 → settle 前恰好重跑一次最新快照（无重叠、无漏刷新）', async () => {
      let proposalsCalls = 0;
      let baseline = 0; // mount 完全 settle 后的调用数（扫描/挂载路径各自有取数）
      let gateResolve = null;
      const proposalsRoute = () => {
        proposalsCalls += 1;
        // 只对 mount 之后的第一次取数挂起（确定性 deferred），其余立即放行。
        if (baseline > 0 && proposalsCalls > baseline && !gateResolve) {
          return new Promise((resolve) => { gateResolve = () => resolve(shadow({ items: [] })); });
        }
        return shadow({ items: [] });
      };
      let currentRuns = [runItem('exec:auth:1')]; // v1：1 条 pending
      const { current } = await setupMounted({
        requests: {
          ...quietSources,
          proposals: proposalsRoute,
          runs: () => envelope(currentRuns),
          canvas: () => {
            const payload = canvasWithAuth('exec:auth:1');
            // 过期卡：带「刷新」入口（驱动 deck 级刷新按钮）。
            payload.nodes[1].human_gate.authorization.expires_at = '2020-01-01T00:00:00+00:00';
            return envelope(payload);
          },
        },
      });
      await settle(); await settle();
      baseline = proposalsCalls;
      assert.ok(baseline > 0, 'mount 已完成基线取数');
      const findCard = () => [...current.mount.querySelectorAll('.life-cosmos-deck-card')]
        .find((el) => allText(el).includes('等待授权'));
      assert.ok(findCard(), '初始（过期）授权卡存在');
      const go = [...findCard().querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      go.click();
      await settle();
      const drawer = current.mount.querySelector('.life-cosmos-drawer');
      const deckRefresh = drawer.querySelector('.life-cosmos-auth-refresh');
      assert.ok(deckRefresh, '过期卡刷新按钮存在');
      deckRefresh.click(); // auth 取数在 review 源挂起（在途）
      await settle();
      assert.ok(gateResolve, 'auth 取数已在途（review 源挂起）');
      currentRuns = []; // 在途期间 run 列表变化：pending 清空
      const capsuleRefresh = byAction(current.mount, 'graph.refresh');
      capsuleRefresh.click(); // fetchGraphRuns 落地新列表 → dirty 标记
      await settle();
      gateResolve(); // 放行在途取数
      await settle(); await settle(); await settle(); await settle(); await settle();
      assert.equal(proposalsCalls, baseline + 2, '恰好重跑一次最新快照（在途 + 重跑；无重叠）');
      assert.equal(findCard(), undefined, '重跑读到最新快照：授权卡消失（漏刷新则残留）');
    });

    it('P1-6 dispose 后停跑：在途 settle 不重跑、迟到结果零落地', async () => {
      let proposalsCalls = 0;
      let baseline = 0;
      let gateResolve = null;
      const proposalsRoute = () => {
        proposalsCalls += 1;
        if (baseline > 0 && proposalsCalls > baseline && !gateResolve) {
          return new Promise((resolve) => { gateResolve = () => resolve(shadow({ items: [] })); });
        }
        return shadow({ items: [] });
      };
      const { harness, current } = await setupMounted({
        requests: {
          ...quietSources,
          proposals: proposalsRoute,
          runs: () => envelope([runItem('exec:auth:1')]),
          canvas: () => {
            const payload = canvasWithAuth('exec:auth:1');
            payload.nodes[1].human_gate.authorization.expires_at = '2020-01-01T00:00:00+00:00';
            return envelope(payload);
          },
        },
      });
      await settle(); await settle();
      baseline = proposalsCalls;
      const findCard = () => [...current.mount.querySelectorAll('.life-cosmos-deck-card')]
        .find((el) => allText(el).includes('等待授权'));
      const go = [...findCard().querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
      go.click();
      await settle();
      const deckRefresh = current.mount.querySelector('.life-cosmos-drawer .life-cosmos-auth-refresh');
      deckRefresh.click();
      await settle();
      assert.ok(gateResolve, 'auth 取数已在途');
      byAction(current.mount, 'graph.refresh').click(); // 标 dirty
      await settle();
      harness.cleanupCallbacks.forEach((cb) => cb()); // dispose（DOM 由 dispose 流程拆除）
      gateResolve();
      await settle(); await settle(); await settle();
      // 零落地判定：dirty 已标但 dispose 后重跑循环立即退出——不再发起任何
      // 新取数（proposals 计数停在最后一次在途调用），迟到结果无处落地。
      assert.equal(proposalsCalls, baseline + 1, 'dispose 后不重跑（零落地）');
    });

    it('P1-7 n8n 修复写入失败 → 真实失败回响 + 按钮恢复可重试（不假「已提交」）', async () => {
      // repairRequestDir 指向已存在文件之下 → mkdirSync 必失败（隔离 tmp，不碰真实目录）。
      const blockerRoot = await fsPromises.mkdtemp(path.join(os.tmpdir(), 'p17-block-'));
      const blocker = path.join(blockerRoot, 'file');
      await fsPromises.writeFile(blocker, 'x');
      try {
        const { current } = await setupMounted({
          beforeMount: (fixture) => {
            addFault(fixture, '看门狗：n8n 日报总调度器停滞', 'ops/看门狗/故障/active/2026-08-25_1-n8n_scheduler_stalled.md');
          },
          makeOptions: { viewPreferences: { repairRequestDir: path.join(blocker, 'sub') } },
        });
        const repairBtn = current.mount.querySelector('.life-cosmos-todo-fault-repair');
        assert.ok(repairBtn, '修复按钮存在');
        repairBtn.click();
        await settle(); await settle(); await settle();
        const message = current.mount.querySelector('.life-cosmos-capsule-status');
        assert.ok(message && message.getAttribute('data-kind') === 'error', '失败回响 error');
        assert.ok(allText(message).includes('修复请求写入失败'), '真实失败原因可见');
        assert.ok((repairBtn.textContent || '').includes('一键修复'), '按钮文案恢复（不假「已提交」）');
        assert.equal(repairBtn.getAttribute('disabled'), null, '按钮恢复可重试');
      } finally {
        await fsPromises.rm(blockerRoot, { recursive: true, force: true });
      }
    });

    it('P1-8 待决卡与已批准卡文案与撤回契约一致（不承诺撤回）', async () => {
      let approved = false;
      const { current } = await setupMounted({
        requests: {
          ...quietSources,
          proposals: () => shadow({ items: approved ? [] : [{ proposal_id: 'p-1', subject: '真实碎片主题', run_id: 'run-1' }] }),
          reviewActions: () => shadow({
            proposal_id: 'p-1', submittable: true, reason_code: null, proposal_fingerprint: 'fp-abc',
            source_sequence: 7, proposal_status: 'blocked', current_decision: null,
          }),
          '/decisions': () => { approved = true; return shadow({ decision_id: 'd-1', proposal_id: 'p-1', decision: 'accepted' }); },
        },
      });
      const findPending = () => [...current.mount.querySelectorAll('.life-cosmos-deck-card')]
        .find((el) => allText(el).includes('等待授权'));
      const openCard = async (card) => {
        const go = [...card.querySelectorAll('button')].find((b) => allText(b).includes('去拍板'));
        go.click();
        await settle();
        return current.mount.querySelector('.life-cosmos-drawer');
      };
      // 待决卡：影响面如实标注不可撤回
      let drawer = await openCard(findPending());
      assert.ok(allText(drawer).includes('不支持撤回'), '待决卡影响面标注不可撤回');
      assert.ok(!allText(drawer).includes('可后续撤回'), '不再承诺「可后续撤回」');
      // 批准 → 已批准卡：同样不承诺撤回
      drawer.querySelector('.life-cosmos-auth-approve').click();
      // 授权后的文件写入是实际异步 I/O，固定次数 setImmediate 不保证完成。
      let approvedCard;
      const deadline = Date.now() + 2000;
      while (!approvedCard && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 5));
        approvedCard = [...current.mount.querySelectorAll('.life-cosmos-deck-card')]
          .find((el) => allText(el).includes('已授权 ·'));
      }
      assert.ok(approvedCard, '已批准卡出现');
      drawer = await openCard(approvedCard);
      assert.ok(allText(drawer).includes('不支持撤回'), '已批准卡影响面标注不可撤回');
      assert.ok(!allText(drawer).includes('窗口内可撤回'), '不再承诺「窗口内可撤回」');
    });
  });


describe('historical Loop approvals and bound Control receipts', () => {
  const controlEnvelope = (data, status = 200, error = null) => ({ status, text: JSON.stringify({ contract_version: '1', data, error }) });
  const run = { run_id: 'real-control-canary-topic', loop_id: 'real-research-canary-topic', status: 'paused', is_current: true };
  const actions = (patch = {}) => ({ run_id: run.run_id, status: 'paused', latest_sequence: 41,
    actions: { resume: { enabled: true }, retry: { enabled: true } }, ...patch });
  const receipt = (body, patch = {}) => ({ intent_id: 'intent-test', idempotency_key: body.idempotency_key,
    run_id: body.run_id, action: body.action, expected_sequence: body.expected_sequence, status: 'applied',
    reason_code: null, result_run_id: body.action === 'retry' ? 'retry-child' : null, applied_sequence: 42, ...patch });
  const quiet = { proposals: () => ({ status: 200, text: JSON.stringify({ contract_version: '1', db_mode: 'read_only', data: { items: [] } }) }) };
  async function openControl(options = {}) {
    const requests = [], postBodies = []; let sequence = 41;
    const mounted = await setupMounted({ requests: { ...quiet,
      loopRuns: () => loopEnvelope([run]),
      controlActions: opts => { requests.push(opts); return controlEnvelope(actions({ latest_sequence: sequence })); },
      controlIntents: opts => { requests.push(opts); const body = JSON.parse(opts.body); postBodies.push(body);
        return options.response ? options.response(body) : controlEnvelope(receipt(body), 202); },
    }, beforeMount: current => current.intentCard.remove() });
    const card = [...mounted.current.mount.querySelectorAll('.life-cosmos-deck-card')].find(el => el.text.includes('等待授权'));
    assert.ok(card); [...card.querySelectorAll('button')].find(el => el.text.includes('去拍板')).click(); await settle();
    const drawer = mounted.current.mount.querySelector('.life-cosmos-drawer');
    return { ...mounted, drawer, requests, postBodies, changeSequence(value) { sequence = value; } };
  }
  async function approve(h, index = 0) {
    const button = h.drawer.querySelectorAll('.life-cosmos-auth-approve')[index]; button.click();
    const deadline = Date.now() + 2000;
    while (button.text === '授权中…' && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 5));
    return button;
  }
  const approvedCards = h => [...h.current.mount.querySelectorAll('.life-cosmos-deck-card')].filter(el => el.text.includes('已授权 ·'));

  it('filters non-current, superseded and the exact legacy test spec; real canary-named research stays', async () => {
    const queried = [];
    const { current } = await setupMounted({ beforeMount: current => current.intentCard.remove(), requests: { ...quiet,
      loopRuns: () => loopEnvelope([run, { ...run, run_id: 'old', is_current: false },
        { ...run, run_id: 'superseded', superseded_by_run_id: 'cancelled-child' },
        { ...run, run_id: 'p2c-old', loop_id: 'p2c-gate2-canary-v1' }]),
      controlActions: opts => { const id = decodeURIComponent(opts.url.split('/').pop()); queried.push(id); return controlEnvelope(actions({ run_id: id })); },
    } });
    assert.deepEqual([...new Set(queried)], [run.run_id]);
    assert.match(current.mount.querySelector('.life-cosmos-decisions').text, /real-research-canary-topic/);
    assert.match(current.mount.querySelector('.life-cb-tag').text, /1 项待定/);
  });

  for (const patch of [{ run_id: 'other-run' }, { latest_sequence: undefined }, { latest_sequence: 0 }, { latest_sequence: -1 }, { latest_sequence: 1.5 }, { latest_sequence: '41' }]) {
    it(`unbound action snapshot cannot produce an approval: ${JSON.stringify(patch)}`, async () => {
      const { current } = await setupMounted({ beforeMount: current => current.intentCard.remove(), requests: { ...quiet,
        loopRuns: () => loopEnvelope([run]), controlActions: () => controlEnvelope(actions(patch)),
      } });
      assert.equal(current.mount.querySelectorAll('.life-cosmos-auth-approve').length, 0);
      assert.match(current.mount.querySelector('.life-cosmos-decisions').text, /部分数据不可用/);
    });
  }

  for (const [index, action, message] of [[0, 'resume', '继续运行指令已生效'], [1, 'retry', '重试任务已创建']]) {
    it(`sends the displayed run/action/sequence unchanged and only applied ${action} is successful`, async () => {
      const h = await openControl(); const reads = h.requests.length; h.changeSequence(99);
      const button = await approve(h, index);
      assert.deepEqual({ run_id: h.postBodies[0].run_id, action: h.postBodies[0].action, expected_sequence: h.postBodies[0].expected_sequence },
        { run_id: run.run_id, action, expected_sequence: 41 });
      assert.equal(h.requests[reads].method, 'POST', 'no silent pre-submit snapshot refresh');
      assert.match(h.postBodies[0].idempotency_key, /^[a-f0-9]{64}$/);
      const result = button.closest('.life-cosmos-auth-summary').querySelector('.life-cosmos-auth-result');
      assert.equal(result.getAttribute('aria-live'), 'polite'); assert.match(result.text, new RegExp(message));
      assert.equal(result.getAttribute('data-kind'), 'success');
      assert.equal(h.drawer.text.includes('执行完成'), false); assert.equal(approvedCards(h).length, 1);
    });
  }

  for (const childId of [null, run.run_id]) {
    it(`applied retry without a distinct child binding is not approved: ${childId}`, async () => {
      const h = await openControl({ response: body => controlEnvelope(receipt(body, { result_run_id: childId }), 202) });
      const button = await approve(h, 1);
      assert.equal(approvedCards(h).length, 0);
      const result = button.closest('.life-cosmos-auth-summary').querySelector('.life-cosmos-auth-result');
      assert.match(result.text, /回执缺少新运行绑定/); assert.equal(result.getAttribute('data-kind'), 'error');
    });
  }

  for (const [name, response, message] of [
    ['HTTP 400', body => controlEnvelope(null, 400, { code: 'invalid_body', message: 'Missing body fields: expected_sequence' }), /invalid_body|Missing body/],
    ['202 failed', body => controlEnvelope(receipt(body, { status: 'failed', reason_code: 'scheduler_offline' }), 202), /scheduler_offline/],
    ['202 rejected', body => controlEnvelope(receipt(body, { status: 'rejected', reason_code: 'sequence_mismatch' }), 202), /数据|序号|变化/],
    ['missing receipt', () => controlEnvelope(null, 202), /回执/],
    ['wrong receipt run', body => controlEnvelope(receipt(body, { run_id: 'other' }), 202), /回执/],
    ['missing receipt identity', body => controlEnvelope(receipt(body, { intent_id: undefined }), 202), /回执/],
    ['wrong receipt action', body => controlEnvelope(receipt(body, { action: 'terminate' }), 202), /回执/],
    ['wrong receipt sequence', body => controlEnvelope(receipt(body, { expected_sequence: 99 }), 202), /回执/],
    ['unknown status', body => controlEnvelope(receipt(body, { status: 'done' }), 202), /回执/],
  ]) {
    it(`${name} stays pending and leaves the real error in the open detail`, async () => {
      const h = await openControl({ response }); const button = await approve(h);
      assert.equal(h.postBodies.length, 1); assert.equal(approvedCards(h).length, 0);
      const result = button.closest('.life-cosmos-auth-summary').querySelector('.life-cosmos-auth-result');
      assert.ok(result, 'inline persistent result, not only a background toast'); assert.equal(result.getAttribute('aria-live'), 'polite');
      assert.notEqual(result.getAttribute('data-kind'), 'success'); assert.match(result.text, message);
      assert.equal(button.text.includes('已授权'), false);
    });
  }

  for (const httpStatus of [409, 202]) {
    it(`real stale_sequence from HTTP ${httpStatus} expires the approval and requires explicit refresh`, async () => {
      const h = await openControl({ response: body => httpStatus === 409
        ? controlEnvelope(null, 409, { code: 'stale_sequence', message: 'Sequence changed' })
        : controlEnvelope(receipt(body, { status: 'rejected', reason_code: 'stale_sequence' }), 202) });
      const button = await approve(h);
      assert.equal(button.text, '已失效'); assert.notEqual(button.getAttribute('disabled'), null);
      assert.equal(approvedCards(h).length, 0);
      const summary = button.closest('.life-cosmos-auth-summary');
      assert.ok(summary.querySelector('.life-cosmos-auth-refresh'), 'version drift leaves a visible refresh action');
      assert.notEqual(summary.querySelector('.life-cosmos-auth-result').getAttribute('data-kind'), 'success');
      assert.match(summary.text, /刷新.*关闭.*重新打开/);
      button.click(); await settle(); assert.equal(h.postBodies.length, 1, 'stale approval cannot submit again');
      h.changeSequence(42); summary.querySelector('.life-cosmos-auth-refresh').click(); await settle(); await settle();
      assert.equal(h.postBodies.length, 1, 'explicit refresh only reads; it does not resubmit approval');
      assert.equal(button.text, '已失效'); assert.notEqual(button.getAttribute('disabled'), null);
      const card = [...h.current.mount.querySelectorAll('.life-cosmos-deck-card')].find(el => el.text.includes('等待授权'));
      [...card.querySelectorAll('button')].find(el => el.text.includes('去拍板')).click(); await settle();
      assert.match(h.drawer.text, /运行版本 42/);
      await approve(h); assert.equal(h.postBodies.length, 2); assert.equal(h.postBodies[1].expected_sequence, 42);

    });
  }

  it('accepted remains pending, does not register approval and does not submit again on a second click', async () => {
    const h = await openControl({ response: body => controlEnvelope(receipt(body, { status: 'accepted', applied_sequence: null }), 202) });
    const button = await approve(h); button.click(); await settle();
    const result = button.closest('.life-cosmos-auth-summary').querySelector('.life-cosmos-auth-result');
    assert.equal(result.getAttribute('data-kind'), 'pending'); assert.match(result.text, /尚待执行/);
    assert.ok(button.closest('.life-cosmos-auth-summary').querySelector('.life-cosmos-auth-refresh'));
    assert.match(button.closest('.life-cosmos-auth-summary').text, /刷新.*关闭.*重新打开/);
    assert.equal(approvedCards(h).length, 0); assert.equal(h.postBodies.length, 1);
    assert.equal(button.text.includes('已授权'), false);
    assert.match(h.current.mount.querySelector('.life-cb-tag').text, /1 项待定/);
  });
});


describe('approval detail does not convert a thrown adapter into silence', () => {
  it('preserves a thrown error under the button and permits a deliberate retry', async () => {
    const { renderAuthDetail, handleAuthApprove } = require(path.join(ROOT, 'src/console/auth-queue-cards.js'));
    const doc = makeDocument(), cfg = { source: 'loop-control', sourceLabel: 'Loop', title: '真实审批',
      ref: { runId: 'r', expectedSequence: 3, action: 'resume' },
      summary: [{ approve: '继续', verify: '3', effects: '指令', impact: '仅当前运行' }],
      runAction: async () => { throw new Error('服务回执丢失'); } };
    const detail = renderAuthDetail(doc, cfg); doc.body.appendChild(detail);
    const button = detail.querySelector('.life-cosmos-auth-approve'), status = detail.querySelector('.life-cosmos-auth-result');
    await handleAuthApprove(button, cfg, cfg.ref, status);
    assert.equal(status.text, '服务回执丢失'); assert.equal(status.getAttribute('data-kind'), 'error');
    assert.equal(button.getAttribute('disabled'), null);
  });
});
