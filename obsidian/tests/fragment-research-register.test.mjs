import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

// Fragment Governed Research v1 包 D：register single-flight 生命周期专项。
// 双击/重放/迟到响应/页面 unload 不得更新 DOM 或产生重复提交；创建后刷新
// 同一权威状态。标注对应包 F 产品验收项编号（#10）。

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
let requestUrlHandler = async () => { throw new Error('handler missing'); };
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') {
    return {
      requestUrl: (options) => requestUrlHandler(options),
      ItemView: class { constructor() { this.contentEl = new FakeEl('div'); this.app = {}; } },
    };
  }
  return originalLoad.call(this, request, ...rest);
};
const { setupLoopConsole } = require(path.join(ROOT, 'src', 'console', 'register.js'));
Module._load = originalLoad;

const RESULT_DIGEST = 'b'.repeat(64);
const ALIGNMENT_ID = 'align:0123456789abcdef01234567';
const ESCALATION_ID = `escalation:${createHash('sha256')
  .update(JSON.stringify(['fragment-escalation-v1', ALIGNMENT_ID, RESULT_DIGEST]))
  .digest('hex').slice(0, 24)}`;

function envelope(data, status = 200, error = null) {
  return { status, text: JSON.stringify({ contract_version: '2', data, error }) };
}

function researchAlignment() {
  return {
    alignment_id: ALIGNMENT_ID,
    fragment_id: 'fragment-1',
    case_id: 'case:0123456789abcdef01234567',
    episode_id: 'episode:0123456789abcdef01234567',
    title: '核验示例产品是否已公开发布',
    status: 'passed',
    sequence: 1,
    revision: 1,
    input_digest: 'a'.repeat(64),
    reasoning: '先核实真实性。',
    plan: '先核实再升级。',
    expected_result: '可信结论。',
    exclusions: [],
    suggested_intents: ['verify'],
    dynamic_intents: [],
    recommended_route: 'verify',
    memory_basis: [],
    route: 'verify',
    decision: { action: 'confirm', intents: ['verify'], supplement: '' },
    execution_scope: {
      capabilities: ['核验'], external_scope: ['公开来源'],
      model_call_cap: 1, cost_cap_cny: 1, side_effect: 'none',
    },
    execution: {
      run_id: 'exec:fragment-intent:0123456789abcdef01234567',
      fragment_id: 'fragment-1',
      status: 'passed',
      current_node: 'harvest',
      route: 'verify',
      stop_reason: 'completed',
      updated_at: '2026-08-09T00:00:00+00:00',
      result_digest: RESULT_DIGEST,
      harvest: [],
      research_progress: {
        stage: 'collected', collected_sources: 2,
        stop_reason: 'evidence_sufficient', model_calls: 0, network_requests: 3,
      },
      result: {
        summary: '已收集 2 个来源，已收集来源但尚未形成可靠判断。',
        unknowns: ['具体主张仍需逐项核对来源原文。'],
        next_checks: ['https://example.com/release'],
        needs_escalation: true,
        escalation_reason: '证据缺口需要受治理研究升级',
        model_calls: 0,
        tool_calls: 3,
      },
      graph_escalation: {
        status: 'proposed',
        reason: '证据缺口需要受治理研究升级',
        source_result_digest: RESULT_DIGEST,
        spec_id: 'fragment-research-escalation-v1',
        spec_digest: 'd'.repeat(64),
        evidence_bundle_digest: 'e'.repeat(64),
        escalation_id: ESCALATION_ID,
      },
    },
  };
}

function home() {
  const doc = new FakeEl('document');
  doc.body = doc.createDiv({ cls: 'body' });
  const root = doc.body.createDiv({ cls: 'my-life-homepage-view' });
  const content = root.createDiv({ cls: 'life-dashboard-content' });
  content.createDiv({ cls: 'life-decision-bar' });
  const card = content.createDiv({ cls: 'life-capture-card' });
  const footer = card.createEl('footer');
  const raw = footer.createEl('a', { text: '原始记录' });
  raw.setAttribute('data-path', '散记/碎片想法/fragment-1.md');
  const organized = footer.createEl('a', { text: '查看整理结果' });
  organized.setAttribute('data-path', 'AI创业/碎片整理/fragment-1.md');
  return { doc, card };
}

class Observer { observe() {} disconnect() {} }

function plugin(disposeCallbacks) {
  const files = new Map([
    ['散记/碎片想法/fragment-1.md', { extension: 'md', path: 'raw' }],
    ['AI创业/碎片整理/fragment-1.md', { extension: 'md', path: 'organized' }],
  ]);
  return {
    registerView() {}, addCommand() {},
    register(cb) { disposeCallbacks.push(cb); },
    registerEvent() {},
    // P0-1：默认隔离通知/修复写目录（register.js 默认路径即生产消费目录）。
    getViewPreferences: () => ({
      authNotifyDir: mkdtempSync(path.join(os.tmpdir(), 'p0-auth-notify-')),
      repairRequestDir: mkdtempSync(path.join(os.tmpdir(), 'p0-repair-req-')),
    }),
    app: {
      vault: {
        getAbstractFileByPath: (ref) => files.get(ref) || null,
        read: async (file) => file.path === 'raw' ? 'raw note' : 'organized note',
      },
      metadataCache: { getFileCache: () => ({ frontmatter: { 'nigo-loop': true } }) },
      workspace: {
        on: () => ({}), onLayoutReady() {}, getLeavesOfType: () => [],
        getLeaf: () => ({ setViewState: async () => {} }), revealLeaf() {},
      },
    },
  };
}

async function settle() {
  for (let i = 0; i < 12; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

describe('fragment research register lifecycle (包 F 产品验收 #10)', () => {
  let saved;
  let disposeCallbacks;
  beforeEach(() => {
    saved = { document: globalThis.document, window: globalThis.window, observer: globalThis.MutationObserver };
    globalThis.window = { setTimeout: () => 1, clearTimeout() {} };
    globalThis.MutationObserver = Observer;
    disposeCallbacks = [];
  });
  afterEach(() => {
    globalThis.document = saved.document;
    globalThis.window = saved.window;
    globalThis.MutationObserver = saved.observer;
  });

  it('[F#10] creates once on double click, then refreshes the same authoritative state', async () => {
    const { doc, card } = home();
    globalThis.document = doc;
    const calls = [];
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    requestUrlHandler = async (options) => {
      calls.push(options);
      if (options.url.endsWith('/graph/v1/research-runs')) {
        await gate;
        return envelope({
          run_id: 'exec:graph-research:0123456789abcdef01234567',
          status: 'human_wait', sequence: 7, model_calls: 0,
        }, 201);
      }
      if (options.url.endsWith('/fragment/v1/alignments')) return envelope([researchAlignment()]);
      return envelope([]);
    };
    setupLoopConsole(plugin(disposeCallbacks));
    await settle();
    const create = card.querySelector('.fragment-intent-research-create');
    assert.ok(create, 'legal graph_proposal must show the create entry');
    const alignmentsBefore = calls.filter((c) => c.url.endsWith('/fragment/v1/alignments') && !c.method).length
      + calls.filter((c) => c.url.endsWith('/fragment/v1/alignments') && c.method === 'GET').length;
    const runsBefore = calls.filter((c) => c.url.endsWith('/graph/v1/runs')).length;
    create.click();
    create.click();
    await settle();
    const posts = calls.filter((c) => c.url.endsWith('/graph/v1/research-runs'));
    assert.equal(posts.length, 1, 'double click must not produce a duplicate submission');
    assert.equal(posts[0].headers['X-Graph-Research-Run-Create'], '1');
    release();
    await settle();
    // 创建后刷新同一权威状态：alignments 与 Graph runs 各重取一次。
    const alignmentsAfter = calls.filter((c) => c.url.endsWith('/fragment/v1/alignments')).length;
    const runsAfter = calls.filter((c) => c.url.endsWith('/graph/v1/runs')).length;
    assert.equal(alignmentsAfter, alignmentsBefore + 1);
    assert.equal(runsAfter, runsBefore + 1);
    assert.ok(card.text.includes('研究 Run 已创建，等待你确认授权。'));
  });

  it('[F#10] treats an idempotent replayed create as the same run and refreshes again', async () => {
    const { doc, card } = home();
    globalThis.document = doc;
    const posts = [];
    requestUrlHandler = async (options) => {
      if (options.url.endsWith('/graph/v1/research-runs')) {
        posts.push(options);
        // 服务端幂等：相同请求 200 返回同一 Run。
        return envelope({
          run_id: 'exec:graph-research:0123456789abcdef01234567',
          status: 'human_wait', sequence: 7, model_calls: 0,
        }, posts.length === 1 ? 201 : 200);
      }
      if (options.url.endsWith('/fragment/v1/alignments')) return envelope([researchAlignment()]);
      return envelope([]);
    };
    setupLoopConsole(plugin(disposeCallbacks));
    await settle();
    const create = card.querySelector('.fragment-intent-research-create');
    create.click();
    await settle();
    create.click();
    await settle();
    assert.equal(posts.length, 2, 'explicit repeated user action is a new request');
    assert.equal(posts[0].body, posts[1].body, 'replayed body is byte-identical and server-idempotent');
    assert.ok(card.text.includes('研究 Run 已创建，等待你确认授权。'));
  });

  it('[F#10] maps server rejection codes to honest messages without creating anything else', async () => {
    const { doc, card } = home();
    globalThis.document = doc;
    requestUrlHandler = async (options) => {
      if (options.url.endsWith('/graph/v1/research-runs')) {
        return envelope(null, 409, { code: 'already_bridged', message: 'conflict' });
      }
      if (options.url.endsWith('/fragment/v1/alignments')) return envelope([researchAlignment()]);
      return envelope([]);
    };
    setupLoopConsole(plugin(disposeCallbacks));
    await settle();
    card.querySelector('.fragment-intent-research-create').click();
    await settle();
    assert.ok(card.text.includes('该升级已经创建过研究 Run。'));
  });

  it('[F#10] applies zero DOM updates when a late response arrives after unload', async () => {
    const { doc, card } = home();
    globalThis.document = doc;
    let alignmentReads = 0;
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    requestUrlHandler = async (options) => {
      if (options.url.endsWith('/graph/v1/research-runs')) {
        await gate;
        return envelope({
          run_id: 'exec:graph-research:0123456789abcdef01234567',
          status: 'human_wait', sequence: 7, model_calls: 0,
        }, 201);
      }
      if (options.url.endsWith('/fragment/v1/alignments')) {
        alignmentReads += 1;
        return envelope([researchAlignment()]);
      }
      return envelope([]);
    };
    setupLoopConsole(plugin(disposeCallbacks));
    await settle();
    const create = card.querySelector('.fragment-intent-research-create');
    const slot = card.querySelector('[data-fragment-intent-card]');
    const message = card.querySelector('.fragment-intent-continuation .fragment-intent-message');
    create.click();
    await settle();
    const readsBeforeUnload = alignmentReads;
    // 页面 unload：所有 register 清理回调生效。
    for (const cb of disposeCallbacks) cb();
    release();
    await settle();
    // 迟到响应：零 DOM 更新、零状态刷新。
    assert.equal(alignmentReads, readsBeforeUnload, 'no authoritative refetch after unload');
    assert.equal(message.text, '', 'no late message write');
    assert.equal(card.querySelector('[data-fragment-intent-card]'), slot, 'card not re-injected');
    assert.equal(card.text.includes('研究 Run 已创建'), false);
  });
});

describe('fragment research register review fixes (审核 A1/A2)', () => {
  let saved;
  let disposeCallbacks;
  beforeEach(() => {
    saved = { document: globalThis.document, window: globalThis.window, observer: globalThis.MutationObserver };
    globalThis.window = { setTimeout: () => 1, clearTimeout() {} };
    globalThis.MutationObserver = Observer;
    disposeCallbacks = [];
  });
  afterEach(() => {
    globalThis.document = saved.document;
    globalThis.window = saved.window;
    globalThis.MutationObserver = saved.observer;
  });

  it('[A1] refreshes the authoritative state after a successful escalation proposal', async () => {
    const { doc, card } = home();
    globalThis.document = doc;
    const calls = [];
    const item = researchAlignment();
    // 无创建材料的提案：走既有「提出 Graph 升级」入口。
    item.execution.graph_escalation = {
      status: 'proposed',
      reason: '证据缺口需要受治理研究升级',
      source_result_digest: RESULT_DIGEST,
    };
    requestUrlHandler = async (options) => {
      calls.push(options);
      if (options.url.includes('/escalations')) {
        return envelope({
          escalation_id: ESCALATION_ID, status: 'proposed',
          reason: '证据缺口需要受治理研究升级',
          capability_status: 'template_required', graph_run_created: false,
        });
      }
      if (options.url.endsWith('/fragment/v1/alignments')) return envelope([item]);
      return envelope([]);
    };
    setupLoopConsole(plugin(disposeCallbacks));
    await settle();
    const readsBefore = calls.filter((c) => c.url.endsWith('/fragment/v1/alignments')).length;
    const escalate = card.querySelector('.fragment-intent-escalate');
    assert.ok(escalate);
    escalate.click();
    await settle();
    assert.equal(
      calls.filter((c) => c.url.endsWith('/fragment/v1/alignments')).length,
      readsBefore + 1,
      'escalate 成功后必须重取同一权威状态',
    );
    assert.ok(card.text.includes('升级理由已保留'));
  });

  it('[A2] gives visible feedback instead of silently swallowing a cross-card click', async () => {
    // 双卡主页：两张卡各有合法提案。
    const doc = new FakeEl('document');
    doc.body = doc.createDiv({ cls: 'body' });
    const root = doc.body.createDiv({ cls: 'my-life-homepage-view' });
    const content = root.createDiv({ cls: 'life-dashboard-content' });
    content.createDiv({ cls: 'life-decision-bar' });
    const makeCard = (id) => {
      const card = content.createDiv({ cls: 'life-capture-card' });
      const footer = card.createEl('footer');
      const raw = footer.createEl('a', { text: '原始记录' });
      raw.setAttribute('data-path', `散记/碎片想法/${id}.md`);
      const organized = footer.createEl('a', { text: '查看整理结果' });
      organized.setAttribute('data-path', `AI创业/碎片整理/${id}.md`);
      return card;
    };
    const card1 = makeCard('fragment-1');
    const card2 = makeCard('fragment-2');
    globalThis.document = doc;
    const secondAlignmentId = 'align:aaaaaaaaaaaaaaaaaaaaaaaa';
    const second = researchAlignment();
    second.alignment_id = secondAlignmentId;
    second.fragment_id = 'fragment-2';
    second.episode_id = 'episode:0123456789abcdef01234568';
    second.execution = { ...second.execution, fragment_id: 'fragment-2' };
    second.execution.graph_escalation = {
      ...second.execution.graph_escalation,
      escalation_id: `escalation:${createHash('sha256')
        .update(JSON.stringify(['fragment-escalation-v1', secondAlignmentId, RESULT_DIGEST]))
        .digest('hex').slice(0, 24)}`,
    };
    const first = researchAlignment();
    const posts = [];
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    requestUrlHandler = async (options) => {
      if (options.url.endsWith('/graph/v1/research-runs')) {
        posts.push(options);
        await gate;
        return envelope({
          run_id: 'exec:graph-research:0123456789abcdef01234567',
          status: 'human_wait', sequence: 7, model_calls: 0,
        }, 201);
      }
      if (options.url.endsWith('/fragment/v1/alignments')) return envelope([first, second]);
      return envelope([]);
    };
    const dispose = [];
    const files = new Map([
      ['散记/碎片想法/fragment-1.md', { extension: 'md', path: 'raw' }],
      ['AI创业/碎片整理/fragment-1.md', { extension: 'md', path: 'organized' }],
      ['散记/碎片想法/fragment-2.md', { extension: 'md', path: 'raw' }],
      ['AI创业/碎片整理/fragment-2.md', { extension: 'md', path: 'organized' }],
    ]);
    setupLoopConsole({
      registerView() {}, addCommand() {}, register(cb) { dispose.push(cb); }, registerEvent() {},
      app: {
        vault: {
          getAbstractFileByPath: (ref) => files.get(ref) || null,
          read: async (file) => file.path === 'raw' ? 'raw note' : 'organized note',
        },
        metadataCache: { getFileCache: () => ({ frontmatter: { 'nigo-loop': true } }) },
        workspace: {
          on: () => ({}), onLayoutReady() {}, getLeavesOfType: () => [],
          getLeaf: () => ({ setViewState: async () => {} }), revealLeaf() {},
        },
      },
    });
    await settle();
    card1.querySelector('.fragment-intent-research-create').click();
    await settle();
    card2.querySelector('.fragment-intent-research-create').click();
    await settle();
    assert.equal(posts.length, 1, '跨卡 single-flight：绝不产生重复提交');
    const message2 = card2.querySelector('.fragment-intent-continuation .fragment-intent-message');
    assert.ok(message2.text.includes('另一项操作正在进行'), '第二张卡必须得到可见反馈');
    release();
    await settle();
    assert.ok(card1.text.includes('研究 Run 已创建'));
  });
});
