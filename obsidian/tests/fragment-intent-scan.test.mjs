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

function envelope(data, status = 200) {
  return { status, text: JSON.stringify({ contract_version: '2', data, error: null }) };
}

function proposed(overrides = {}) {
  return {
    alignment_id: 'align:0123456789abcdef01234567', fragment_id: 'fragment-1',
    case_id: 'case:0123456789abcdef01234567', episode_id: 'episode:0123456789abcdef01234567',
    title: '技术碎片', status: 'suggested', sequence: 1, revision: 1,
    input_digest: 'a'.repeat(64), reasoning: '先核实真实性，再判断个人价值。',
    plan: '走最短可靠路径，复杂时再升级。', expected_result: '可信结论与下一步。',
    exclusions: ['不安装'], suggested_intents: ['verify'], dynamic_intents: [],
    recommended_route: 'verify', memory_basis: [],
    execution_scope: { capabilities: ['核验'], external_scope: [], model_call_cap: 0, cost_cap_cny: 0, side_effect: 'none' },
    ...overrides,
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

function plugin() {
  const files = new Map([
    ['散记/碎片想法/fragment-1.md', { extension: 'md', path: 'raw' }],
    ['AI创业/碎片整理/fragment-1.md', { extension: 'md', path: 'organized' }],
  ]);
  return {
    registerView() {}, addCommand() {}, register() {}, registerEvent() {},
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
      },
    },
  };
}

async function settle() {
  for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

describe('fragment intent register lifecycle', () => {
  let saved;
  beforeEach(() => {
    saved = { document: globalThis.document, window: globalThis.window, observer: globalThis.MutationObserver };
    globalThis.window = { setTimeout: () => 1, clearTimeout() {} };
    globalThis.MutationObserver = Observer;
  });
  afterEach(() => {
    globalThis.document = saved.document;
    globalThis.window = saved.window;
    globalThis.MutationObserver = saved.observer;
  });

  it('reads alignment once, binds note digests locally and refreshes only after an explicit action', async () => {
    const { doc, card } = home();
    globalThis.document = doc;
    let list = [];
    const calls = [];
    requestUrlHandler = async (options) => {
      calls.push(options);
      if (options.url.endsWith('/fragment/v1/alignments')) {
        if (options.method === 'POST') {
          const body = JSON.parse(options.body);
          list = [proposed({ input_digest: body.input_digest })];
          return envelope(list[0], 201);
        }
        return envelope(list);
      }
      return envelope([]);
    };
    setupLoopConsole(plugin());
    await settle();
    assert.ok(card.querySelector('.fragment-intent-start'));
    assert.equal(calls.filter((call) => call.url.endsWith('/fragment/v1/alignments') && call.method === 'GET').length, 1);
    const start = card.querySelector('.fragment-intent-start');
    await start.listeners.click[0]({ target: start });
    await settle();
    const write = calls.find((call) => call.url.endsWith('/fragment/v1/alignments') && call.method === 'POST');
    assert.ok(write);
    assert.equal(write.body.includes('raw note'), false);
    assert.equal(write.body.includes('organized note'), false);
    assert.ok(card.text.includes('先核实真实性'));
    assert.equal(calls.filter((call) => call.url.endsWith('/fragment/v1/alignments') && call.method === 'GET').length, 2);
  });
});
