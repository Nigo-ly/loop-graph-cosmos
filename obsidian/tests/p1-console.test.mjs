import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FIXTURES = path.join(ROOT, 'fixtures', 'p1', 'synthetic');
const require = createRequire(import.meta.url);

const api = require(path.join(ROOT, 'src', 'console', 'api-client.js'));
const model = require(path.join(ROOT, 'src', 'console', 'view-model.js'));
const { renderConsole } = require(path.join(ROOT, 'src', 'console', 'console-dom.js'));
const {
  injectHomepageEntry,
  injectHomepageExperience,
  ENTRY_BUTTON_CLASS,
  ENTRY_BUTTON_LABEL,
  EXPERIENCE_CONTROLS_CLASS,
  EXPERIENCE_STORAGE_KEY,
} = require(path.join(ROOT, 'src', 'console', 'home-entry.js'));

async function fixture(name) {
  return JSON.parse(await readFile(path.join(FIXTURES, name), 'utf8'));
}

function recordingTransport(routes, calls) {
  return async (request) => {
    calls.push({ url: request.url, method: request.method });
    const url = new URL(request.url);
    const key = url.pathname + url.search;
    const handler = routes[url.pathname] || routes[key];
    if (!handler) return { status: 404, json: null };
    return handler(request);
  };
}

function readyState(queueBody, runsBody, healthBody) {
  return {
    status: 'ready',
    error: null,
    health: model.healthSummary(healthBody),
    queue: model.queueRows(queueBody),
    queueStale: queueBody.stale === true,
    runs: model.runRows(runsBody, { status: null }),
    runsStale: runsBody.stale === true,
    statuses: model.statusFilterOptions(runsBody),
    attention: model.attentionRows(runsBody),
    filter: { status: null, statusLabel: null },
    selectedRunId: null,
    detail: null,
    detailLoading: false,
    detailError: null,
    detailStale: false,
  };
}

const noopHandlers = { onRefresh() {}, onFilterChange() {}, onSelectRun() {}, onBack() {} };

describe('GET-only runtime guard', () => {
  it('G4-12: a hostile baseUrl option cannot redirect the frozen loopback endpoint', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/loop/v1/health': async () => ({ status: 200, json: await fixture('health-fresh.json') }),
    }, calls);
    // caller 试图覆盖 base URL：冻结 loopback 不得被重定向。
    const client = api.createLoopApiClient({ transport, baseUrl: 'http://evil.invalid/loop/v1' });
    await client.health();
    assert.equal(calls.length, 1);
    assert.ok(
      calls[0].url.startsWith('http://127.0.0.1:5679/loop/v1'),
      `frozen URL must win, got ${calls[0].url}`
    );
  });

  it('every request issued by the API client uses GET, across all endpoints and retries', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/loop/v1/health': async () => ({ status: 200, json: await fixture('health-fresh.json') }),
      '/loop/v1/queue': async () => ({ status: 200, json: await fixture('queue.json') }),
      '/loop/v1/runs': async () => ({ status: 200, json: await fixture('runs.json') }),
      '/loop/v1/runs/synthetic-run-passed-0001': async () => ({ status: 200, json: await fixture('run-detail.json') }),
    }, calls);
    const client = api.createLoopApiClient({ transport });
    await client.health();
    await client.queue();
    await client.runs({ status: 'passed', limit: 50 });
    await client.runDetail('synthetic-run-passed-0001');
    assert.equal(calls.length, 4);
    for (const call of calls) assert.equal(call.method, 'GET', `non-GET request: ${JSON.stringify(call)}`);
  });

  it('retries once silently and still uses GET on the retry', async () => {
    const calls = [];
    let failures = 0;
    const transport = async (request) => {
      calls.push({ url: request.url, method: request.method });
      failures += 1;
      if (failures === 1) throw new Error('connection refused');
      return { status: 200, json: await fixture('health-fresh.json') };
    };
    const client = api.createLoopApiClient({ transport });
    const body = await client.health();
    assert.equal(body.contract_version, '2');
    assert.equal(calls.length, 2);
    for (const call of calls) assert.equal(call.method, 'GET');
  });

  it('client source contains no non-GET HTTP method, no fetch, no SQLite, no vault mutation, no n8n webhook', async () => {
    const src = await readFile(path.join(ROOT, 'src', 'console', 'api-client.js'), 'utf8');
    assert.ok(!/method:\s*["'`](POST|PUT|DELETE|PATCH|HEAD|OPTIONS)["'`]/i.test(src));
    assert.ok(!/\bfetch\s*\(/.test(src));
    assert.ok(!/sqlite/i.test(src));
    assert.ok(!/processFrontMatter|vault\.(process|modify|create|append|delete|createFolder)/.test(src));
    assert.ok(!/127\.0\.0\.1:5678|fragment-organize-now/.test(src));
    assert.ok(src.includes('method: "GET"'));
  });

  it('times out, retries once, then reports unreachable', async () => {
    const calls = [];
    const transport = async (request) => {
      calls.push(request);
      return new Promise(() => {});
    };
    const client = api.createLoopApiClient({ transport, timeoutMs: 20 });
    await assert.rejects(client.health(), (err) => {
      assert.equal(err.kind, 'unreachable');
      return true;
    });
    assert.equal(calls.length, 2);
    for (const call of calls) assert.equal(call.method, 'GET');
  });

  it('reports http_error with envelope error details after one retry', async () => {
    const calls = [];
    const transport = async (request) => {
      calls.push(request);
      return { status: 500, json: { contract_version: '2', db_mode: 'read_only', error: { code: 'read_failed', message: 'synthetic read failure' } } };
    };
    const client = api.createLoopApiClient({ transport });
    await assert.rejects(client.queue(), (err) => {
      assert.equal(err.kind, 'http_error');
      assert.equal(err.details.status, 500);
      assert.equal(err.details.code, 'read_failed');
      return true;
    });
    assert.equal(calls.length, 2);
  });
});

describe('contract envelope validation', () => {
  it('refuses a contract_version other than "2" without retrying', async () => {
    const calls = [];
    const transport = async (request) => {
      calls.push(request);
      const body = await fixture('health-fresh.json');
      return { status: 200, json: { ...body, contract_version: '1' } };
    };
    const client = api.createLoopApiClient({ transport });
    await assert.rejects(client.health(), (err) => {
      assert.equal(err.kind, 'contract_mismatch');
      assert.equal(err.details.expected, '2');
      assert.equal(err.details.actual, '1');
      return true;
    });
    assert.equal(calls.length, 1, 'contract mismatch must not be retried');
  });

  it('refuses a db_mode other than read_only', async () => {
    const transport = async () => {
      const body = await fixture('health-fresh.json');
      return { status: 200, json: { ...body, db_mode: 'read_write' } };
    };
    const client = api.createLoopApiClient({ transport });
    await assert.rejects(client.health(), (err) => err.kind === 'contract_mismatch');
  });
});

describe('fixture view models', () => {
  it('queue list rows keep raw status and display_state', async () => {
    const rows = model.queueRows(await fixture('queue.json'));
    assert.equal(rows.length, 3);
    assert.equal(rows[0].runId, 'synthetic-queue-0001');
    assert.equal(rows[0].status, 'approved');
    assert.equal(rows[0].displayState, 'queued');
    assert.equal(rows[0].displayStateLabel, '待路由');
    assert.ok(rows[0].nextStep.includes('LoopSpec'));
  });

  it('run list filters by raw status only', async () => {
    const body = await fixture('runs.json');
    assert.equal(model.runRows(body, { status: 'passed' }).length, 1);
    assert.equal(model.runRows(body, { status: 'escalated' })[0].runId, 'synthetic-run-escalated-0003');
    assert.equal(model.runRows(body, { status: null }).length, 5);
    assert.deepEqual(model.rawStatusList(body), ['passed', 'exhausted', 'escalated', 'blocked', 'running']);
  });

  it('run detail exposes timeline in committed order with snapshot rows kept', async () => {
    const detail = model.detailModel(await fixture('run-detail.json'));
    assert.deepEqual(detail.timeline.map((e) => e.sequence), [1, 2, 3, 4, 5, 6, 7, 8]);
    assert.equal(detail.timeline[0].eventType, 'snapshot');
    assert.equal(detail.timeline[7].eventType, 'loop_stopped');
  });

  it('run detail exposes budget against limits, eval gates, stop reason, and refs', async () => {
    const detail = model.detailModel(await fixture('run-detail.json'));
    assert.equal(detail.budget.length, 3);
    assert.equal(detail.budget[0].text, '3100 / 20000 令牌（Token）');
    assert.ok(Math.abs(detail.budget[0].ratio - 3100 / 20000) < 1e-9);
    assert.equal(detail.eval.gates, '8/8');
    assert.equal(detail.eval.verdict, 'pass');
    assert.equal(detail.stopReason, null);
    assert.equal(detail.evidenceRefs.length, 2);
    assert.equal(detail.artifactRefs.length, 1);
    assert.equal(detail.assetRefs.length, 1);
    assert.ok(detail.evidenceRefs[0].startsWith('artifacts/synthetic-run-passed-0001/'));
  });

  it('stop reason is surfaced verbatim when present', async () => {
    const body = await fixture('runs.json');
    const exhausted = model.runRows(body, { status: 'exhausted' })[0];
    assert.equal(exhausted.stopReason, 'token_budget_exhausted');
  });

  it('a quiet system with large data age is healthy, not stale', async () => {
    const health = model.healthSummary(await fixture('health-fresh.json'));
    assert.equal(health.stale, false);
    assert.equal(health.healthy, true);
    assert.equal(health.dataAgeSeconds, 43210);
    assert.equal(health.dataAgeText, '数据已静默 12 小时');
  });
});

describe('console DOM states', () => {
  it('renders loading state', () => {
    const root = new FakeEl('div');
    renderConsole(root, { status: 'loading', health: null }, noopHandlers);
    assert.ok(root.querySelector('.lc-state-loading'));
    assert.ok(root.text.includes('正在读取'));
  });

  it('renders unreachable state without fabricating rows', () => {
    const root = new FakeEl('div');
    renderConsole(root, { status: 'unreachable', health: { providerHealthy: true, stale: false } }, noopHandlers);
    assert.ok(root.querySelector('.lc-state-unreachable'));
    assert.ok(root.text.includes('无法连接 Loop 数据服务'));
    assert.ok(root.text.includes('状态未知'));
    assert.ok(!root.querySelector('.lc-health-ok'));
    assert.ok(!root.text.includes('synthetic-run'));
  });

  it('renders API error state with status and code', () => {
    const root = new FakeEl('div');
    renderConsole(root, { status: 'error', health: null, error: { details: { status: 500, code: 'read_failed', message: 'synthetic read failure' } } }, noopHandlers);
    assert.ok(root.querySelector('.lc-state-error'));
    assert.ok(root.text.includes('500'));
    assert.ok(root.text.includes('read_failed'));
  });

  it('contract mismatch renders an explicit refusal notice and nothing else', () => {
    const root = new FakeEl('div');
    renderConsole(root, { status: 'contract_mismatch', health: null, error: { details: { expected: '2', actual: '1' } } }, noopHandlers);
    assert.ok(root.querySelector('.lc-state-error'));
    assert.ok(root.text.includes('契约版本不匹配'));
    assert.ok(!root.querySelector('.lc-rows'));
  });

  it('ready state renders Chinese status badges while raw statuses stay auditable', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    renderConsole(root, state, noopHandlers);
    assert.equal(root.querySelectorAll('.lc-queue .lc-row').length, 3);
    // The collapsed all-runs surface does not repeat attention records:
    // 2 normal runs here, 3 attention runs in the attention section.
    assert.equal(root.querySelectorAll('.lc-runs .lc-row').length, 2);
    assert.equal(root.querySelectorAll('.lc-attention .lc-row').length, 3);
    // Primary badges are Chinese.
    const badges = root.querySelectorAll('.lc-row-status').map((el) => el.text);
    assert.ok(badges.includes('已批准'));
    assert.ok(badges.includes('预算耗尽'));
    assert.ok(badges.includes('已转人工'));
    // Raw statuses remain visible in the low-emphasis audit lines and filter options.
    assert.ok(root.text.includes('approved'));
    assert.ok(root.text.includes('exhausted'));
    assert.ok(root.text.includes('只读连接正常'));
    assert.ok(!root.querySelector('.lc-state-stale'));
  });

  it('quiet system shows a data-age note but no stale badge', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    renderConsole(root, state, noopHandlers);
    assert.ok(root.querySelector('.lc-quiet-note'));
    assert.ok(root.text.includes('数据已静默 12 小时'));
    assert.ok(!root.querySelector('.lc-stale-note'));
    assert.ok(!root.querySelector('.lc-health-stale'));
  });

  it('stale health and stale envelopes show visible stale markers', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-stale.json'));
    state.queueStale = true;
    renderConsole(root, state, noopHandlers);
    assert.ok(root.querySelector('.lc-state-stale'));
    assert.ok(root.querySelector('.lc-health-stale'));
    assert.ok(root.querySelector('.lc-stale-note'));
  });

  it('empty queue renders an explicit empty state, not an error', async () => {
    const root = new FakeEl('div');
    const queue = await fixture('queue.json');
    const state = readyState({ ...queue, items: [] }, await fixture('runs.json'), await fixture('health-fresh.json'));
    renderConsole(root, state, noopHandlers);
    assert.ok(root.querySelector('.lc-queue .lc-empty'));
    assert.ok(root.text.includes('待路由队列为空'));
  });

  it('empty detail renders a selection placeholder', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    renderConsole(root, state, noopHandlers);
    assert.ok(root.querySelector('.lc-detail .lc-state-empty'));
    assert.ok(root.text.includes('尚未选择运行'));
  });

  it('detail render shows timeline, budget, eval gates, stop reason and refs', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    state.selectedRunId = 'synthetic-run-passed-0001';
    state.detail = model.detailModel(await fixture('run-detail.json'));
    renderConsole(root, state, noopHandlers);
    assert.equal(root.querySelectorAll('.lc-timeline-item').length, 8);
    assert.equal(root.querySelectorAll('.lc-budget-row').length, 3);
    assert.ok(root.text.includes('8/8'));
    assert.ok(root.text.includes('artifacts/synthetic-run-passed-0001/source_lock.md'));
    assert.ok(root.text.includes('停止原因'));
    assert.ok(root.querySelector('.lc-back'));
  });

  it('status filter change notifies the handler with the raw status', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    let received = null;
    const handlers = { ...noopHandlers, onFilterChange: (s) => { received = s; } };
    renderConsole(root, state, handlers);
    const select = root.querySelector('.lc-filter-select');
    select.value = 'exhausted';
    for (const fn of select.listeners.change || []) fn({ target: select });
    assert.equal(received, 'exhausted');
  });

  it('keyboard focus order is refresh, queue rows, filter, run rows', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    renderConsole(root, state, noopHandlers);
    const focusables = root.focusables();
    assert.ok(focusables[0].classes.has('lc-refresh'), 'first focusable must be the refresh button');
    const queueRows = focusables.filter((el) => el.classes.has('lc-row') && !el.classes.has('is-selected'));
    assert.ok(focusables[1].classes.has('lc-row'));
    const selectIndex = focusables.findIndex((el) => el.classes.has('lc-filter-select'));
    assert.ok(selectIndex > 0, 'filter select must be focusable');
    assert.ok(focusables[selectIndex - 1].classes.has('lc-row'));
    assert.ok(focusables[selectIndex + 1].classes.has('lc-row'));
    assert.ok(queueRows.length > 0);
  });
});

describe('homepage entry injection', () => {
  function homepageDom() {
    const doc = new FakeEl('body');
    const view = doc.createDiv({ cls: 'my-life-homepage-view' });
    const bar = view.createDiv({ cls: 'life-decision-bar' });
    const textCol = bar.createDiv({});
    textCol.createEl('strong', { text: 'DECISION SUPPORT' });
    const match = bar.createDiv({ cls: 'life-decision-match' });
    const primary = match.createEl('button', { text: '生成评估请求' });
    return { doc, match, primary };
  }

  it('appends one secondary Loop 控制台 action without touching the existing action', () => {
    const { doc, match, primary } = homepageDom();
    const injected = injectHomepageEntry(doc, () => {});
    assert.equal(injected, 1);
    assert.equal(match.children[0], primary, 'existing project-assessment action stays first and untouched');
    const entries = match.querySelectorAll(`.${ENTRY_BUTTON_CLASS}`);
    assert.equal(entries.length, 1);
    assert.equal(entries[0].text, ENTRY_BUTTON_LABEL);
  });

  it('is idempotent across repeated scans', () => {
    const { doc, match } = homepageDom();
    injectHomepageEntry(doc, () => {});
    injectHomepageEntry(doc, () => {});
    injectHomepageEntry(doc, () => {});
    assert.equal(match.querySelectorAll(`.${ENTRY_BUTTON_CLASS}`).length, 1);
  });

  it('clicking the entry opens the console', () => {
    const { doc, match } = homepageDom();
    let opened = 0;
    injectHomepageEntry(doc, () => { opened += 1; });
    match.querySelector(`.${ENTRY_BUTTON_CLASS}`).click();
    assert.equal(opened, 1);
  });

  it('does nothing on pages without the decision strip', () => {
    const doc = new FakeEl('body');
    doc.createDiv({ cls: 'some-other-view' });
    assert.equal(injectHomepageEntry(doc, () => {}), 0);
  });
});

describe('homepage reading experience controls', () => {
  function experienceDom(copies = 1) {
    const doc = new FakeEl('document');
    for (let index = 0; index < copies; index += 1) {
      const view = doc.createDiv({ cls: 'my-life-homepage-view' });
      const command = view.createDiv({ cls: 'life-command-bar' });
      command.createDiv({ cls: 'life-view-switch' });
      view.createDiv({ cls: 'life-dashboard-content' });
    }
    return doc;
  }

  function memoryStorage(initial = {}) {
    const values = new Map(Object.entries(initial));
    return {
      getItem(key) { return values.has(key) ? values.get(key) : null; },
      setItem(key, value) { values.set(key, String(value)); },
    };
  }

  it('adds one accessible three-level scale control per homepage copy', () => {
    const doc = experienceDom(2);
    assert.equal(injectHomepageExperience(doc, memoryStorage()), 2);
    assert.equal(injectHomepageExperience(doc, memoryStorage()), 0, 'repeat scan is idempotent');
    assert.equal(doc.querySelectorAll(`.${EXPERIENCE_CONTROLS_CLASS}`).length, 2);
    for (const group of doc.querySelectorAll(`.${EXPERIENCE_CONTROLS_CLASS}`)) {
      assert.equal(group.getAttribute('role'), 'group');
      assert.equal(group.getAttribute('aria-label'), '主页字号');
      assert.equal(group.querySelectorAll('button').length, 3);
    }
    for (const view of doc.querySelectorAll('.my-life-homepage-view')) {
      assert.equal(view.getAttribute('data-life-reading-scale'), 'comfortable');
      assert.ok(view.querySelector('.life-dashboard-content').classes.has('life-motion-ready'));
    }
  });

  it('persists a selected scale and applies it to every rendered homepage copy', () => {
    const storage = memoryStorage({ [EXPERIENCE_STORAGE_KEY]: 'standard' });
    const doc = experienceDom(2);
    injectHomepageExperience(doc, storage);
    const large = doc.querySelectorAll('[data-life-scale]')
      .find((button) => button.getAttribute('data-life-scale') === 'large');
    large.click();

    for (const view of doc.querySelectorAll('.my-life-homepage-view')) {
      assert.equal(view.getAttribute('data-life-reading-scale'), 'large');
    }
    assert.equal(storage.getItem(EXPERIENCE_STORAGE_KEY), 'large');
    assert.equal(doc.querySelectorAll('[data-life-scale]')
      .filter((button) => button.getAttribute('aria-pressed') === 'true').length, 2);
  });

  it('rejects an unknown stored scale and falls back to the comfortable default', () => {
    const doc = experienceDom();
    injectHomepageExperience(doc, memoryStorage({ [EXPERIENCE_STORAGE_KEY]: '<bad>' }));
    assert.equal(doc.querySelector('.my-life-homepage-view').getAttribute('data-life-reading-scale'), 'comfortable');
  });
});

describe('console responsive layout', () => {
  it('collapses before the two-column console becomes cramped', async () => {
    const css = await readFile(path.join(ROOT, 'src', 'console', 'console.css'), 'utf8');
    assert.ok(css.includes('@media (max-width: 1320px)'));
  });

  it('keeps multi-line list rows free of host button clipping and no-wrap', async () => {
    // Obsidian app.css forces `height: var(--input-height)` and
    // `white-space: nowrap` on every <button>; multi-line .lc-row buttons must
    // override both or their meta lines are clipped/overlapped at any width.
    const css = await readFile(path.join(ROOT, 'src', 'console', 'console.css'), 'utf8');
    const rowRule = css.match(/\.lc-row \{([^}]*)\}/);
    assert.ok(rowRule, '.lc-row rule present');
    assert.match(rowRule[1], /height:\s*auto/, '.lc-row height must be auto');
    assert.match(rowRule[1], /white-space:\s*normal/, '.lc-row must allow wrapping');
    const metaRule = css.match(/\.lc-row-meta \{([^}]*)\}/);
    assert.ok(metaRule, '.lc-row-meta rule present');
    assert.match(metaRule[1], /overflow-wrap:\s*anywhere/, '.lc-row-meta must wrap long tokens');
  });
});

describe('Chinese localization (P1 patch)', () => {
  it('maps every contract raw status to a Chinese label and falls back verbatim', () => {
    const expected = {
      approved: '已批准',
      running: '运行中',
      passed: '已通过',
      exhausted: '预算耗尽',
      blocked: '已阻塞',
      escalated: '已转人工',
      cancelled: '已取消',
      superseded: '已被取代',
      failed_safe: '安全停止',
      requested: '已请求',
      preflight_passed: '预检通过',
    };
    for (const [raw, label] of Object.entries(expected)) {
      assert.equal(model.statusLabel(raw), label, `status ${raw}`);
    }
    assert.equal(model.statusLabel('mystery_historical_status'), 'mystery_historical_status');
    assert.equal(model.statusLabel(''), '—');
    assert.equal(model.statusLabel(null), '—');
  });

  it('maps display states, event types, node names, and Eval verdicts to Chinese', () => {
    assert.equal(model.displayStateLabel('queued'), '待路由');
    assert.equal(model.displayStateLabel('awaiting_human'), '等待人工');
    const eventTypes = [
      'snapshot', 'run_registered', 'admission_transition', 'state_transition',
      'node_completed', 'action_reserved', 'action_completed', 'action_reconciled',
      'loop_stopped', 'loop_blocked', 'parent_reconciled',
    ];
    for (const type of eventTypes) {
      const label = model.eventTypeLabel(type);
      assert.notEqual(label, type, `event type ${type} must have a Chinese label`);
      assert.ok(/[一-鿿]/.test(label), `event type label for ${type} must contain Chinese`);
    }
    assert.equal(model.eventTypeLabel('unknown_historical_event'), 'unknown_historical_event');
    const nodeNames = {
      intake: '接收入口',
      source_lock: '来源锁定',
      claim_matrix: '主张矩阵',
      stop_policy_design: '停止策略设计',
      independent_eval: '独立核验',
      feedback: '实践反馈',
      worker: '执行节点',
      k27_worker: 'K2.7 执行节点',
      content_acquisition: '内容获取',
      source_verification: '来源核验',
      value_routing: '价值路由',
      understanding: '理解分析',
      action_design: '行动设计',
      publication_eval: '发布评估',
      relevance_mapping: '相关性映射',
      skill_inventory: '技能清单',
      capability_model: '能力模型',
      capability_mapping: '能力映射',
    };
    for (const [raw, label] of Object.entries(nodeNames)) {
      assert.equal(model.nodeLabel(raw), label, `node ${raw}`);
    }
    assert.equal(model.nodeLabel('unseen_node'), 'unseen_node');
    const verdicts = {
      pass: '通过',
      fail: '未通过',
      revise_then_pass: '修订后通过',
    };
    for (const [raw, label] of Object.entries(verdicts)) {
      assert.equal(model.verdictLabel(raw), label, `verdict ${raw}`);
    }
    assert.equal(model.verdictLabel('some_other_verdict'), 'some_other_verdict');
  });

  it('revise_then_pass verdict renders Chinese in the detail view with raw preserved', async () => {
    const detailBody = await fixture('run-detail.json');
    const detail = model.detailModel({
      ...detailBody,
      run: {
        ...detailBody.run,
        eval_results: { hard_gates: { passed: 7, total: 8 }, verdict: 'revise_then_pass' },
      },
    });
    assert.equal(detail.eval.verdict, 'revise_then_pass');
    assert.equal(detail.eval.verdictLabel, '修订后通过');
    assert.equal(detail.technical.verdict, 'revise_then_pass', 'raw verdict preserved for audit');
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    state.selectedRunId = 'synthetic-run-passed-0001';
    state.detail = detail;
    renderConsole(root, state, noopHandlers);
    assert.ok(root.text.includes('结论：修订后通过'), 'Chinese primary verdict rendered');
    const tech = root.querySelector('.lc-tech');
    assert.ok(tech.text.includes('原始 Eval 结论：revise_then_pass'), 'raw verdict in 技术详情');
  });

  it('Chinese-primary section labels render in the detail view', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    state.selectedRunId = 'synthetic-run-passed-0001';
    state.detail = model.detailModel(await fixture('run-detail.json'));
    renderConsole(root, state, noopHandlers);
    assert.ok(root.text.includes('执行模型（Worker）'));
    assert.ok(root.text.includes('独立评估器（Evaluator）'));
    assert.ok(root.text.includes('独立评估结果（Eval）'));
    assert.ok(root.text.includes('活跃令牌（Token）'));
    assert.ok(root.text.includes('3100 / 20000 令牌（Token）'));
    const runRowTokens = state.runs.find((r) => r.runId === 'synthetic-run-passed-0001');
    assert.ok(runRowTokens, 'fixture run present in list');
    assert.ok(root.text.includes('预算：3100 令牌（Token）'), 'list budget uses Chinese-primary unit');
  });

  it('view models carry both Chinese labels and raw values', async () => {
    const rows = model.runRows(await fixture('runs.json'), { status: null });
    const exhausted = rows.find((r) => r.status === 'exhausted');
    assert.equal(exhausted.statusLabel, '预算耗尽');
    assert.equal(exhausted.currentNode, 'worker');
    assert.equal(exhausted.currentNodeLabel, '执行节点');
    const detail = model.detailModel(await fixture('run-detail.json'));
    assert.equal(detail.status, 'passed');
    assert.equal(detail.statusLabel, '已通过');
    assert.equal(detail.currentNodeLabel, '实践反馈');
    assert.equal(detail.eval.verdict, 'pass');
    assert.equal(detail.eval.verdictLabel, '通过');
    assert.deepEqual(detail.technical, {
      status: 'passed',
      displayState: 'completed',
      currentNode: 'feedback',
      verdict: 'pass',
      stopReason: null,
    });
    assert.equal(detail.timeline[2].eventTypeLabel, '节点完成');
    assert.equal(detail.timeline[2].nodeLabel, '来源锁定');
    assert.equal(detail.timeline[2].eventType, 'node_completed');
  });

  it('filter options show Chinese labels but submit the raw status', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    let received = null;
    renderConsole(root, state, { ...noopHandlers, onFilterChange: (s) => { received = s; } });
    const select = root.querySelector('.lc-filter-select');
    const options = select.querySelectorAll('option');
    const byValue = new Map(options.map((o) => [o.attributes.value, o.text]));
    assert.equal(byValue.get(''), '全部状态');
    assert.equal(byValue.get('exhausted'), '预算耗尽（exhausted）');
    assert.equal(byValue.get('escalated'), '已转人工（escalated）');
    select.value = 'exhausted';
    for (const fn of select.listeners.change || []) fn({ target: select });
    assert.equal(received, 'exhausted', 'filter must still submit the raw status');
  });

  it('detail view shows Chinese primaries with a 技术详情 raw disclosure', async () => {
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), await fixture('runs.json'), await fixture('health-fresh.json'));
    state.selectedRunId = 'synthetic-run-passed-0001';
    state.detail = model.detailModel(await fixture('run-detail.json'));
    renderConsole(root, state, noopHandlers);
    assert.ok(root.text.includes('已通过'), 'Chinese status label rendered');
    assert.ok(root.text.includes('通过（硬门槛 8/8）'), 'Chinese verdict in minimal main view');
    assert.ok(root.text.includes('实践反馈'), 'Chinese node label rendered');
    const techAll = root.querySelector('.lc-tech-all');
    assert.ok(techAll, 'collapsed 技术详情 disclosure present');
    assert.ok(techAll.text.includes('技术详情'));
    const tech = root.querySelector('.lc-tech');
    assert.ok(tech, 'raw-value sub-disclosure present');
    assert.ok(tech.text.includes('原始状态：passed'));
    assert.ok(tech.text.includes('原始分组：completed'));
    assert.ok(tech.text.includes('原始当前节点：feedback'));
    assert.ok(tech.text.includes('原始 Eval 结论：pass'));
    const types = root.querySelectorAll('.lc-timeline-type').map((el) => el.text);
    assert.deepEqual(types, ['快照', '运行登记', '节点完成', '节点完成', '节点完成', '节点完成', '节点完成', 'Loop 停止']);
    const rawLines = root.querySelectorAll('.lc-timeline-raw').map((el) => el.text);
    assert.ok(rawLines.some((t) => t.includes('node_completed · source_lock')));
    assert.ok(rawLines.some((t) => t.includes('loop_stopped')));
  });

  it('unknown values render verbatim and are never mislabeled', async () => {
    const runsBody = await fixture('runs.json');
    const mutated = {
      ...runsBody,
      items: [{
        ...runsBody.items[0],
        run_id: 'synthetic-run-unknown-0099',
        status: 'totally_unknown_status',
        display_state: 'odd_display_state',
        current_node: 'mystery_node',
      }],
    };
    const root = new FakeEl('div');
    const state = readyState(await fixture('queue.json'), mutated, await fixture('health-fresh.json'));
    renderConsole(root, state, noopHandlers);
    const badge = root.querySelector('.lc-runs .lc-row-status');
    assert.equal(badge.text, 'totally_unknown_status', 'unknown status badge stays verbatim');
    assert.ok(root.text.includes('odd_display_state'), 'unknown display state stays verbatim');
    assert.ok(root.text.includes('mystery_node'), 'unknown node stays verbatim');
    const detailBody = await fixture('run-detail.json');
    const detail = model.detailModel({
      ...detailBody,
      run: {
        ...detailBody.run,
        status: 'totally_unknown_status',
        eval_results: { hard_gates: { passed: 1, total: 2 }, verdict: 'unseen_verdict' },
        timeline: [{ sequence: 1, event_type: 'unseen_event', node: 'mystery_node', created_at: '2026-01-01T00:00:00+08:00', summary: 'synthetic unknown event' }],
      },
    });
    assert.equal(detail.statusLabel, 'totally_unknown_status');
    assert.equal(detail.eval.verdictLabel, 'unseen_verdict');
    assert.equal(detail.timeline[0].eventTypeLabel, 'unseen_event');
    assert.equal(detail.timeline[0].nodeLabel, 'mystery_node');
    assert.equal(detail.technical.verdict, 'unseen_verdict');
  });

  it('budget units are localized and stop reasons stay verbatim', async () => {
    const detail = model.detailModel(await fixture('run-detail.json'));
    assert.equal(detail.budget[0].text, '3100 / 20000 令牌（Token）');
    assert.equal(detail.budget[0].label, '活跃令牌（Token）');
    assert.equal(detail.budget[1].text, '3 / 10 次');
    assert.equal(detail.budget[2].text, '640 / 1200 秒');
    const rows = model.runRows(await fixture('runs.json'), { status: 'escalated' });
    assert.equal(rows[0].stopReason, 'awaiting_experiment_approval', 'stop reason must not be translated');
  });
});
