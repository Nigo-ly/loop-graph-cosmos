import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const P1_FIXTURES = path.join(ROOT, 'fixtures', 'p1', 'synthetic');
const P2_FIXTURES = path.join(ROOT, 'fixtures', 'p2', 'synthetic');
const require = createRequire(import.meta.url);

const control = require(path.join(ROOT, 'src', 'console', 'control-client.js'));
const model = require(path.join(ROOT, 'src', 'console', 'view-model.js'));
const { renderConsole } = require(path.join(ROOT, 'src', 'console', 'console-dom.js'));

// Stub the obsidian module so the view class can load under plain Node.
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'obsidian') {
    return {
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
const { LoopConsoleView } = require(path.join(ROOT, 'src', 'console', 'console-view.js'));
Module._load = originalLoad;

async function p1Fixture(name) {
  return JSON.parse(await readFile(path.join(P1_FIXTURES, name), 'utf8'));
}

async function p2Fixture(name) {
  return JSON.parse(await readFile(path.join(P2_FIXTURES, name), 'utf8'));
}

const noopHandlers = {
  onRefresh() {},
  onFilterChange() {},
  onSelectRun() {},
  onBack() {},
  onOpenDialog() {},
  onDialogCancel() {},
  onDialogConfirm() {},
  onRefreshControl() {},
};

function envelope(data) {
  return {
    contract_version: '1',
    generated_at: '2026-01-01T00:30:00+08:00',
    service_version: 'synthetic-control',
    data,
    error: null,
  };
}

function receiptFromBody(body, overrides) {
  return {
    intent_id: 'synthetic-intent-new-0001',
    idempotency_key: body.idempotency_key,
    run_id: body.run_id,
    action: body.action,
    arguments: body.arguments,
    requester: body.requester,
    expected_sequence: body.expected_sequence,
    status: 'applied',
    reason_code: null,
    result_run_id: null,
    applied_sequence: body.expected_sequence + 1,
    created_at: '2026-01-01T00:30:01+08:00',
    updated_at: '2026-01-01T00:30:02+08:00',
    ...(overrides || {}),
  };
}

// ---------------------------------------------------------------------------
// Control client transport boundary
// ---------------------------------------------------------------------------

describe('control client transport', () => {
  it('canonical idempotency key matches the frozen P2A vectors', async () => {
    assert.equal(
      await control.canonicalIdempotencyKey('nigo', 'synthetic-run-0001', 'pause', 3, {}),
      '3a37685a34bd3a14f9c1d468986d9c653ae40ca55f9d5477ee9db524d82dd0bc',
    );
    assert.equal(
      await control.canonicalIdempotencyKey('nigo', 'synthetic-run-0002', 'priority', 7, { priority: -5 }),
      '0df88f2b29d3d5fc98b9acd650b2322a2f94cfb3dc2c63ae6e7bd93124d8efe7',
    );
  });

  it('issues GET for reads and POST only to /control/v1/intents, with trusted headers', async () => {
    const calls = [];
    const transport = async (request) => {
      calls.push(request);
      const url = new URL(request.url);
      if (url.pathname === '/control/v1/health') return { status: 200, json: envelope({ status: 'ok' }) };
      if (url.pathname.startsWith('/control/v1/actions/')) {
        return { status: 200, json: await p2Fixture('actions-running.json') };
      }
      if (url.pathname === '/control/v1/intents' && request.method === 'GET') {
        return { status: 200, json: await p2Fixture('intents-empty.json') };
      }
      if (url.pathname === '/control/v1/intents' && request.method === 'POST') {
        const body = JSON.parse(request.body);
        return { status: 202, json: envelope(receiptFromBody(body)) };
      }
      return { status: 404, json: envelope(null) };
    };
    const client = control.createControlClient({ transport });
    await client.health();
    await client.actionsFor('synthetic-run-running-0001');
    await client.listIntents('synthetic-run-running-0001');
    const result = await client.submitIntent({
      runId: 'synthetic-run-running-0001',
      action: 'pause',
      expectedSequence: 7,
      arguments: {},
    });
    assert.equal(result.httpStatus, 202);
    assert.equal(result.receipt.status, 'applied');

    const methods = calls.map((c) => `${c.method} ${new URL(c.url).pathname}`);
    assert.deepEqual(methods, [
      'GET /control/v1/health',
      'GET /control/v1/actions/synthetic-run-running-0001',
      'GET /control/v1/intents',
      'POST /control/v1/intents',
    ]);
    for (const call of calls) {
      assert.ok(call.url.startsWith('http://127.0.0.1:5680/control/v1'), call.url);
      assert.equal(call.headers.Origin, 'app://obsidian.md');
    }
    const post = calls[3];
    assert.equal(post.headers['X-Loop-Control-Intent'], '1');
    assert.equal(post.headers['Content-Type'], 'application/json');
    const body = JSON.parse(post.body);
    assert.equal(body.run_id, 'synthetic-run-running-0001');
    assert.equal(body.action, 'pause');
    assert.equal(body.expected_sequence, 7);
    assert.match(body.idempotency_key, /^[0-9a-f]{64}$/);
    assert.equal(body.requester, 'nigo');
  });

  it('maps http errors, unreachable, and contract mismatch honestly', async () => {
    const errorTransport = async () => ({
      status: 409,
      json: { ...envelope(null), error: { code: 'stale_sequence', message: 'stale' } },
    });
    const client = control.createControlClient({ transport: errorTransport });
    await assert.rejects(
      client.submitIntent({ runId: 'r', action: 'pause', expectedSequence: 1, arguments: {} }),
      (err) => err.kind === 'http_error' && err.details.code === 'stale_sequence',
    );

    const down = control.createControlClient({
      transport: async () => {
        throw new Error('connection refused');
      },
    });
    await assert.rejects(down.health(), (err) => err.kind === 'unreachable');

    const wrongVersion = control.createControlClient({
      transport: async () => ({ status: 200, json: { ...envelope({}), contract_version: '0' } }),
    });
    await assert.rejects(wrongVersion.health(), (err) => err.kind === 'contract_mismatch');
  });

  it('validates priority range locally without any transport call', async () => {
    let called = 0;
    const client = control.createControlClient({
      transport: async () => {
        called += 1;
        return { status: 200, json: envelope({}) };
      },
    });
    for (const bad of [-11, 11, 1.5, 'x']) {
      await assert.rejects(
        client.submitIntent({ runId: 'r', action: 'priority', expectedSequence: 1, arguments: { priority: bad } }),
        (err) => err.kind === 'invalid_arguments',
      );
    }
    assert.equal(called, 0);
  });
});

// ---------------------------------------------------------------------------
// View models
// ---------------------------------------------------------------------------

describe('control view models', () => {
  it('maps action availability with Chinese labels and reasons', async () => {
    const view = model.controlActionsView(await p2Fixture('actions-paused.json'));
    assert.equal(view.runId, 'synthetic-run-paused-0002');
    assert.equal(view.statusLabel, '已暂停');
    assert.equal(view.latestSequence, 12);
    assert.equal(view.priority, 3);
    const byAction = new Map(view.actions.map((a) => [a.action, a]));
    assert.equal(byAction.get('resume').label, '继续');
    assert.equal(byAction.get('resume').enabled, true);
    assert.equal(byAction.get('retry').enabled, false);
    assert.equal(byAction.get('retry').reasonText, 'LoopSpec 未注册，操作不可用');
    assert.equal(byAction.get('pause').reasonText, '当前状态不允许该操作');
  });

  it('maps receipts to Chinese lifecycle labels and keeps raw values', async () => {
    const items = model.intentHistoryView(await p2Fixture('intents-history.json'));
    assert.equal(items.length, 4);
    assert.equal(items[0].statusLabel, '已接管');
    assert.equal(items[1].statusLabel, '执行失败');
    assert.equal(items[1].reasonText, '控制服务内部错误');
    assert.equal(items[2].statusLabel, '已拒绝');
    assert.equal(items[2].reasonText, '数据已变化（序号过期），请刷新后重试');
    assert.equal(items[3].statusLabel, '已执行');
    assert.equal(items[3].intentId, 'synthetic-intent-0001');
    assert.equal(items[3].idempotencyKey, 'dddd000000000000000000000000000000000000000000000000000000000001');
    assert.equal(items[3].appliedSequence, 4);
  });

  it('falls back to raw reason codes and statuses for unknown values', () => {
    const view = model.receiptView({
      intent_id: 'x',
      action: 'pause',
      status: 'mystery_state',
      reason_code: 'weird_code',
      arguments: {},
    });
    assert.equal(view.statusLabel, 'mystery_state');
    assert.equal(view.reasonText, 'weird_code');
  });

  it('attention rows surface awaiting-human and abnormal stops first', async () => {
    const rows = model.attentionRows(await p1Fixture('runs.json'));
    const byRun = new Map(rows.map((r) => [r.runId, r]));
    assert.equal(byRun.get('synthetic-run-escalated-0003').attentionText, '等待人工');
    assert.equal(byRun.get('synthetic-run-escalated-0003').title, '合成主题：能力映射实验审批');
    assert.equal(byRun.get('synthetic-run-exhausted-0002').attentionText, '异常停止：预算耗尽');
    assert.equal(byRun.get('synthetic-run-exhausted-0002').title, '合成主题：超额预算停止行为');
    assert.equal(byRun.get('synthetic-run-blocked-0004').attentionText, '异常停止：已阻塞');
    assert.ok(!byRun.has('synthetic-run-passed-0001'), 'normal passed run is not attention');
    assert.ok(!byRun.has('synthetic-run-running-0005'), 'normal running run is not attention');
  });

  it('attention titles use the labelled fallback when subject is absent', async () => {
    const body = await p1Fixture('runs.json');
    const mutated = {
      ...body,
      items: body.items.map((item) => {
        const copy = { ...item };
        delete copy.subject;
        delete copy.goal;
        return copy;
      }),
    };
    const rows = model.attentionRows(mutated);
    const byRun = new Map(rows.map((r) => [r.runId, r]));
    const title = byRun.get('synthetic-run-escalated-0003').title;
    assert.ok(title.startsWith('待获取标题 · '), `labelled fallback: ${title}`);
    // The shared generic goal is never used as the identifying title.
    assert.ok(!title.includes('synthetic-loop-gamma-v1'));
  });

  it('cards stay distinguishable for one LoopSpec with different subjects', async () => {
    const body = await p1Fixture('runs.json');
    const template = body.items.find((i) => i.run_id === 'synthetic-run-escalated-0003');
    const twin = {
      ...template,
      run_id: 'synthetic-run-escalated-0006',
      subject: '合成主题：同模板下的另一个实验主题',
    };
    const mutated = { ...body, items: [...body.items, twin] };
    const rows = model.attentionRows(mutated);
    const titles = rows
      .filter((r) => r.loopId === 'synthetic-loop-gamma-v1')
      .map((r) => r.title);
    assert.equal(titles.length, 2, 'two attention cards share the LoopSpec');
    assert.equal(titles[0] !== titles[1], true, 'different subjects keep cards distinguishable');
    assert.ok(titles.includes('合成主题：能力映射实验审批'));
    assert.ok(titles.includes('合成主题：同模板下的另一个实验主题'));
    // LoopSpec stays in the raw line, never as the title.
    const rawIds = rows.map((r) => r.loopId);
    assert.ok(rawIds.every((id) => typeof id === 'string'));
  });

  it('subject-null items with one shared goal stay distinguishable via fallback', async () => {
    const body = await p1Fixture('runs.json');
    const template = body.items.find((i) => i.run_id === 'synthetic-run-escalated-0003');
    const alpha = {
      ...template,
      subject: null,
      fragment_id: '2026-07-19-19-52-09-b032997b',
    };
    const beta = {
      ...template,
      run_id: 'synthetic-run-escalated-0007',
      subject: null,
      fragment_id: '2026-07-19-18-48-44-67822bdd',
    };
    const rows = model.attentionRows({ items: [alpha, beta] });
    const titles = rows.map((r) => r.title);
    assert.equal(titles[0] !== titles[1], true, 'fallback titles must differ');
    assert.ok(titles.includes('待获取标题 · 07-19 19:52:09'));
    assert.ok(titles.includes('待获取标题 · 07-19 18:48:44'));
    assert.ok(titles.every((t) => t.startsWith('待获取标题 · ')), 'labelled as fallback');
  });

  it('same-minute subject-less records stay distinct at second precision', async () => {
    const body = await p1Fixture('runs.json');
    const template = body.items.find((i) => i.run_id === 'synthetic-run-escalated-0003');
    const alpha = { ...template, subject: null, fragment_id: '2026-07-19-19-52-09-b032997b' };
    const beta = {
      ...template,
      run_id: 'synthetic-run-escalated-0008',
      subject: null,
      fragment_id: '2026-07-19-19-52-44-aaaa1111',
    };
    const titles = model.attentionRows({ items: [alpha, beta] }).map((r) => r.title);
    assert.equal(titles[0] !== titles[1], true, 'same-minute captures must not collide');
    assert.ok(titles.includes('待获取标题 · 07-19 19:52:09'));
    assert.ok(titles.includes('待获取标题 · 07-19 19:52:44'));
    // Non-timestamp ids keep a stable short suffix instead of colliding.
    const suffixAlpha = model.fallbackTitle('frag-alpha-1', '2026-01-01T00:00:00+08:00');
    const suffixBeta = model.fallbackTitle('frag-alpha-2', '2026-01-01T00:00:00+08:00');
    assert.notEqual(suffixAlpha, suffixBeta);
    assert.ok(suffixAlpha.endsWith(' · ha-1'), `stable short suffix: ${suffixAlpha}`);
    assert.ok(suffixBeta.endsWith(' · ha-2'), `stable short suffix: ${suffixBeta}`);
  });

  it('no list hint names a concrete action in any attention status', async () => {
    const rows = model.attentionRows(await p1Fixture('runs.json'));
    assert.ok(rows.length >= 3);
    for (const row of rows) {
      assert.equal(row.decisionHint, '需要处理：打开查看可用操作');
      assert.ok(!/暂停|继续|重试|终止|优先/.test(row.decisionHint), `hint must not name actions: ${row.decisionHint}`);
    }
  });

  it('flags budget risk at 90 percent or above', async () => {
    const detailBody = await p1Fixture('run-detail.json');
    const risky = model.detailModel({
      ...detailBody,
      run: { ...detailBody.run, budget_used: { active_tokens: 19000, calls: 3, elapsed_seconds: 640 } },
    });
    assert.deepEqual(risky.budgetRisk, ['活跃令牌（Token）已用 95%，接近上限']);
    const calm = model.detailModel(detailBody);
    assert.deepEqual(calm.budgetRisk, []);
  });
});

// ---------------------------------------------------------------------------
// Minimal-frontend DOM structure
// ---------------------------------------------------------------------------

async function baseState(controlState) {
  const [queue, runs, health, detail] = await Promise.all([
    p1Fixture('queue.json'),
    p1Fixture('runs.json'),
    p1Fixture('health-fresh.json'),
    p2Fixture('p1-detail-running.json'),
  ]);
  return {
    status: 'ready',
    error: null,
    health: model.healthSummary(health),
    queue: model.queueRows(queue),
    queueStale: false,
    runs: model.runRows(runs, { status: null }),
    runsStale: false,
    statuses: model.statusFilterOptions(runs),
    attention: model.attentionRows(runs),
    filter: { status: null, statusLabel: null },
    selectedRunId: 'synthetic-run-running-0001',
    detail: model.detailModel(detail),
    detailLoading: false,
    detailError: null,
    detailStale: false,
    control: controlState,
  };
}

async function readyControl(actionsName, intentsName) {
  return {
    status: 'ready',
    actions: model.controlActionsView(await p2Fixture(actionsName)),
    error: null,
    dialog: null,
    submitting: false,
    submitError: null,
    lastReceipt: null,
    duplicate: false,
    intents: model.intentHistoryView(await p2Fixture(intentsName)),
  };
}

describe('minimal frontend structure', () => {
  it('main view shows at most 状态/当前阶段/最近结果/停止原因, rest collapsed', async () => {
    const root = new FakeEl('div');
    renderConsole(root, await baseState(await readyControl('actions-running.json', 'intents-empty.json')), noopHandlers);
    const labels = root.querySelectorAll('.lc-main-fields .lc-field-label').map((el) => el.text);
    assert.deepEqual(labels, ['状态', '当前阶段', '最近结果', '停止原因']);
    const techAll = root.querySelector('.lc-tech-all');
    assert.ok(techAll, 'collapsed technical disclosure present');
    for (const collapsed of ['执行模型（Worker）', '独立评估器（Evaluator）', '节点时间线（按提交顺序）', '原始状态：running']) {
      assert.ok(techAll.text.includes(collapsed), `${collapsed} only inside 技术详情`);
    }
    // The main view (outside 技术详情) does not expose worker/evaluator/timeline.
    const mainText = root.text.replace(techAll.text, '');
    assert.ok(!mainText.includes('执行模型（Worker）'));
    assert.ok(!mainText.includes('synthetic-worker-v1'));
  });

  it('shows 预算风险 in the main view only when a budget is near limit', async () => {
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    state.detail = {
      ...state.detail,
      budgetRisk: ['活跃令牌（Token）已用 95%，接近上限'],
    };
    const root = new FakeEl('div');
    renderConsole(root, state, noopHandlers);
    const labels = root.querySelectorAll('.lc-main-fields .lc-field-label').map((el) => el.text);
    assert.deepEqual(labels, ['状态', '当前阶段', '最近结果', '停止原因', '预算风险']);
    assert.ok(root.text.includes('接近上限'));
  });

  it('attention section renders semantic cards before queue with decision hints', async () => {
    const root = new FakeEl('div');
    renderConsole(root, await baseState(await readyControl('actions-running.json', 'intents-empty.json')), noopHandlers);
    const attention = root.querySelector('.lc-attention');
    assert.ok(attention, 'attention section present');
    assert.ok(attention.text.includes('需要关注'));
    assert.ok(attention.text.includes('等待人工 · 需要处理：打开查看可用操作'));
    assert.ok(attention.text.includes('异常停止：预算耗尽 · 需要处理：打开查看可用操作'));
    // Card titles are the per-task subjects, not UUIDs or the shared
    // LoopSpec name; LoopSpec and run id stay in the low-emphasis raw line.
    const firstTitle = attention.querySelector('.lc-row .lc-row-id');
    assert.ok(firstTitle.text.startsWith('合成主题：'), `semantic title: ${firstTitle.text}`);
    assert.ok(!attention.text.includes('synthetic-loop-gamma-v1 已转人工'), 'LoopSpec is not the card title');
    assert.ok(attention.text.includes('LoopSpec：synthetic-loop-gamma-v1'));
    assert.ok(attention.text.includes('run：synthetic-run-escalated-0003'));
    assert.equal(attention.querySelectorAll('.lc-row').length, 3);
    // It precedes the queue section in document order.
    const sections = root.querySelectorAll('.lc-section');
    assert.ok(sections[0].classes.has('lc-attention'));
    assert.ok(sections[1].classes.has('lc-queue'));
  });

  it('primary row keeps 暂停/继续/终止; retry and priority are secondary', async () => {
    const root = new FakeEl('div');
    renderConsole(root, await baseState(await readyControl('actions-paused.json', 'intents-empty.json')), noopHandlers);
    const controls = root.querySelector('.lc-controls');
    const primaryRow = controls.querySelector('.lc-control-buttons');
    const primaryTexts = primaryRow.children
      .filter((el) => el.classes.has('lc-control-btn'))
      .map((el) => el.text);
    assert.deepEqual(primaryTexts, ['暂停', '继续', '终止']);
    const secondary = controls.querySelector('.lc-secondary-ops');
    assert.ok(secondary, 'secondary operations collapsed disclosure present');
    assert.ok(secondary.text.includes('重试'));
    assert.ok(secondary.text.includes('调整优先级'));
    assert.ok(secondary.text.includes('重试不可用：LoopSpec 未注册，操作不可用'));
    // Disabled buttons carry the disabled attribute and Chinese reason.
    const buttons = controls.querySelectorAll('.lc-control-btn');
    const retryBtn = buttons.find((el) => el.text === '重试');
    assert.equal(retryBtn.attributes.disabled, 'disabled');
  });

  it('intent history collapses with raw idempotency values inside 技术详情', async () => {
    const root = new FakeEl('div');
    renderConsole(root, await baseState(await readyControl('actions-running.json', 'intents-history.json')), noopHandlers);
    const intents = root.querySelector('.lc-intents');
    assert.equal(intents.tagName, 'DETAILS');
    assert.ok(intents.text.includes('已拒绝'));
    assert.ok(intents.text.includes('数据已变化（序号过期），请刷新后重试'));
    assert.ok(intents.text.includes('synthetic-intent-0001'));
    assert.ok(intents.text.includes('idempotency_key：dddd'));
  });

  it('control outage keeps P1 read-only intact and offers an explicit retry', async () => {
    const state = await baseState({
      status: 'unreachable',
      actions: null,
      error: 'unreachable',
      dialog: null,
      submitting: false,
      submitError: null,
      lastReceipt: null,
      duplicate: false,
      intents: [],
    });
    const root = new FakeEl('div');
    let retried = 0;
    renderConsole(root, state, { ...noopHandlers, onRefreshControl: () => { retried += 1; } });
    assert.ok(root.text.includes('控制服务不可用：写操作已禁用，上方只读数据不受影响。'));
    assert.ok(root.querySelector('.lc-queue .lc-row'), 'queue still rendered');
    assert.ok(root.querySelector('.lc-main-fields'), 'detail still rendered');
    assert.ok(!root.querySelector('.lc-control-btn'), 'no write controls while unavailable');
    root.querySelector('.lc-control-retry').click();
    assert.equal(retried, 1);
  });

  it('keeps the operational console free of decorative animation', async () => {
    const css = await readFile(path.join(ROOT, 'src', 'console', 'console.css'), 'utf8');
    const consoleRules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .filter((match) => match[1].includes('.my-life-loop-console-view'))
      .map((match) => match[2])
      .join('\n');
    assert.ok(!/animation(?:-\w+)?\s*:/.test(consoleRules), 'console rules have no keyframe animation');
    assert.ok(!/transition\s*:/.test(consoleRules), 'console rules have no transitions');
    assert.ok(css.includes('@keyframes life-panel-enter'), 'homepage motion is explicitly scoped and named');
  });
});

// ---------------------------------------------------------------------------
// View-level flows (obsidian stubbed)
// ---------------------------------------------------------------------------

async function p1Client(detailBody) {
  const [health, queue, runs] = await Promise.all([
    p1Fixture('health-fresh.json'),
    p1Fixture('queue.json'),
    p1Fixture('runs.json'),
  ]);
  return {
    async health() {
      return health;
    },
    async queue() {
      return queue;
    },
    async runs() {
      return runs;
    },
    async runDetail() {
      return detailBody;
    },
  };
}

function controlTransport(routes) {
  const calls = [];
  const seenKeys = new Set();
  const transport = async (request) => {
    calls.push(request);
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;
    if (key === 'POST /control/v1/intents') {
      const body = JSON.parse(request.body);
      if (seenKeys.has(body.idempotency_key)) {
        return { status: 200, json: envelope(receiptFromBody(body, { intent_id: 'synthetic-intent-dup-1' })) };
      }
      seenKeys.add(body.idempotency_key);
      if (routes.postError) {
        return { status: routes.postError.status, json: { ...envelope(null), error: routes.postError.error } };
      }
      return { status: 202, json: envelope(receiptFromBody(body)) };
    }
    if (key.startsWith('GET /control/v1/actions/')) {
      return { status: 200, json: routes.actions };
    }
    if (key === 'GET /control/v1/intents') {
      return { status: 200, json: routes.intents };
    }
    if (key === 'GET /control/v1/health') {
      return { status: 200, json: envelope({ status: 'ok' }) };
    }
    return { status: 404, json: envelope(null) };
  };
  return { transport, calls };
}

async function makeView(routes) {
  const detailBody = await p2Fixture('p1-detail-running.json');
  const { transport, calls } = controlTransport(routes);
  const controlClient = control.createControlClient({ transport });
  const view = new LoopConsoleView({}, await p1Client(detailBody), controlClient);
  await view.refresh();
  return { view, calls };
}

describe('control flows through the view', () => {
  it('selecting a run loads actions and history; pause dialog submits with canonical key', async () => {
    const { view, calls } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    await view.selectRun('synthetic-run-running-0001');
    assert.equal(view.state.control.status, 'ready');
    const buttons = view.contentEl.querySelectorAll('.lc-control-btn').filter((el) => !el.attributes.disabled);
    assert.ok(buttons.length >= 2, 'enabled pause/terminate buttons rendered');

    view.openDialog('pause');
    const dialog = view.contentEl.querySelector('.lc-dialog');
    assert.ok(dialog, 'review dialog rendered');
    assert.ok(dialog.text.includes('synthetic-run-running-0001'));
    assert.ok(dialog.text.includes('当前状态'));
    assert.ok(dialog.text.includes('期望序号'));
    assert.ok(dialog.text.includes('操作效果'));

    const postsBefore = calls.filter((c) => c.method === 'POST').length;
    await view.confirmDialog();
    const posts = calls.filter((c) => c.method === 'POST');
    assert.equal(posts.length, postsBefore + 1);
    const body = JSON.parse(posts[posts.length - 1].body);
    assert.equal(body.action, 'pause');
    assert.equal(body.expected_sequence, 7);
    assert.match(body.idempotency_key, /^[0-9a-f]{64}$/);

    // Receipt is shown with immutable intent id; run status is NOT altered.
    assert.ok(view.contentEl.text.includes('意图 ID：synthetic-intent-new-0001（不可变）'));
    assert.ok(view.contentEl.text.includes('意图状态：已执行'));
    assert.equal(view.state.detail.status, 'running');
  });

  it('duplicate submission shows the original-receipt notice', async () => {
    const { view } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    await view.selectRun('synthetic-run-running-0001');
    view.openDialog('pause');
    await view.confirmDialog();
    view.openDialog('pause');
    await view.confirmDialog();
    assert.ok(view.contentEl.text.includes('重复提交：控制服务返回了原始回执，没有产生新的操作'));
  });

  it('terminate requires the second destructive confirmation showing the run id', async () => {
    const { view, calls } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    await view.selectRun('synthetic-run-running-0001');
    view.openDialog('terminate');
    await view.confirmDialog();
    assert.equal(calls.filter((c) => c.method === 'POST').length, 0, 'no submit on first step');
    const step2 = view.contentEl.querySelector('.lc-dialog');
    assert.ok(step2.text.includes('二次确认：即将终止运行 synthetic-run-running-0001'));
    const confirm = step2.querySelector('.lc-dialog-confirm');
    assert.ok(confirm.classes.has('is-destructive'), 'destructive styling on final confirm');
    await view.confirmDialog();
    const posts = calls.filter((c) => c.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(JSON.parse(posts[0].body).action, 'terminate');
  });

  it('priority dialog submits the chosen value and rejects out-of-range locally', async () => {
    const { view, calls } = await makeView({
      actions: await p2Fixture('actions-paused.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    await view.selectRun('synthetic-run-paused-0002');
    view.openDialog('priority');
    const input = view.contentEl.querySelector('.lc-dialog-input');
    assert.ok(input, 'priority input rendered');
    input.value = '-11';
    await view.confirmDialog(Number.parseInt(input.value, 10));
    assert.equal(calls.filter((c) => c.method === 'POST').length, 0, 'out-of-range rejected locally');
    assert.ok(view.contentEl.text.includes('操作未提交'));

    view.openDialog('priority');
    const input2 = view.contentEl.querySelector('.lc-dialog-input');
    input2.value = '5';
    await view.confirmDialog(Number.parseInt(input2.value, 10));
    const posts = calls.filter((c) => c.method === 'POST');
    assert.equal(posts.length, 1);
    assert.deepEqual(JSON.parse(posts[0].body).arguments, { priority: 5 });
  });

  it('a stale rejection shows an honest error without a receipt', async () => {
    const { view } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
      postError: { status: 409, error: { code: 'stale_sequence', message: 'stale' } },
    });
    await view.selectRun('synthetic-run-running-0001');
    view.openDialog('pause');
    await view.confirmDialog();
    assert.ok(view.contentEl.text.includes('操作未提交：数据已变化（序号过期），请刷新后重试'));
    assert.ok(view.contentEl.text.includes('原始错误码：stale_sequence'));
    assert.equal(view.state.control.lastReceipt, null);
    assert.equal(view.state.detail.status, 'running');
  });

  it('control outage keeps read-only data usable', async () => {
    const detailBody = await p2Fixture('p1-detail-running.json');
    const downClient = control.createControlClient({
      transport: async () => {
        throw new Error('connection refused');
      },
    });
    const view = new LoopConsoleView({}, await p1Client(detailBody), downClient);
    await view.refresh();
    await view.selectRun('synthetic-run-running-0001');
    assert.equal(view.state.control.status, 'unreachable');
    assert.ok(view.contentEl.text.includes('控制服务不可用：写操作已禁用，上方只读数据不受影响。'));
    assert.ok(view.contentEl.querySelector('.lc-main-fields'), 'detail intact');
    assert.ok(view.contentEl.text.includes('synthetic-run-running-0001'));
  });

  it('keyboard order reaches attention rows before queue, and dialog controls are focusable', async () => {
    const { view } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    await view.refresh();
    const focusables = view.contentEl.focusables();
    assert.ok(focusables[0].classes.has('lc-refresh'));
    const firstAttention = focusables.findIndex(
      (el) => el.classes.has('lc-row') && el.parent && el.parent.parent && el.parent.parent.classes.has('lc-attention'),
    );
    const filterIndex = focusables.findIndex((el) => el.classes.has('lc-filter-select'));
    assert.ok(firstAttention > 0, 'attention row focusable');
    assert.ok(firstAttention < filterIndex, 'attention rows come before the filter');

    await view.selectRun('synthetic-run-running-0001');
    view.openDialog('pause');
    const dialogFocusables = view.contentEl.focusables().filter(
      (el) => el.classes.has('lc-dialog-confirm') || el.classes.has('lc-dialog-cancel'),
    );
    assert.equal(dialogFocusables.length, 2);
    view.closeDialog();
    assert.ok(!view.contentEl.querySelector('.lc-dialog'), 'cancel closes the dialog');
  });
});

// ---------------------------------------------------------------------------
// Revision 2: race safety, receipt persistence, modal keyboard, compact default
// ---------------------------------------------------------------------------

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function detailFor(runId) {
  const base = await p2Fixture('p1-detail-running.json');
  return { ...base, run: { ...base.run, run_id: runId } };
}

async function actionsForRun(runId) {
  const base = await p2Fixture('actions-running.json');
  return { ...base, data: { ...base.data, run_id: runId } };
}

describe('selection generation races (P0)', () => {
  async function p1RacingClient(slowRunId) {
    const [health, queue, runs] = await Promise.all([
      p1Fixture('health-fresh.json'),
      p1Fixture('queue.json'),
      p1Fixture('runs.json'),
    ]);
    const gate = deferred();
    return {
      gate,
      client: {
        async health() {
          return health;
        },
        async queue() {
          return queue;
        },
        async runs() {
          return runs;
        },
        async runDetail(runId) {
          if (runId === slowRunId) await gate.promise;
          return detailFor(runId);
        },
      },
    };
  }

  function instantControlTransport(slowActionsFor) {
    const gate = deferred();
    const transport = async (request) => {
      const url = new URL(request.url);
      if (slowActionsFor && url.pathname === `/control/v1/actions/${slowActionsFor}`) {
        await gate.promise;
      }
      if (url.pathname.startsWith('/control/v1/actions/')) {
        const runId = decodeURIComponent(url.pathname.slice('/control/v1/actions/'.length));
        return { status: 200, json: await actionsForRun(runId) };
      }
      if (url.pathname === '/control/v1/intents') {
        return { status: 200, json: await p2Fixture('intents-empty.json') };
      }
      return { status: 404, json: envelope(null) };
    };
    return { transport, gate };
  }

  it('A-slow/B-fast detail responses never bind detail to the stale run', async () => {
    const { gate, client } = await p1RacingClient('run-A');
    const { transport } = instantControlTransport(null);
    const view = new LoopConsoleView({}, client, control.createControlClient({ transport }));
    await view.refresh();

    const slowSelect = view.selectRun('run-A');
    await view.selectRun('run-B');
    gate.resolve();
    await slowSelect;

    assert.equal(view.state.selectedRunId, 'run-B');
    assert.equal(view.state.detail.runId, 'run-B');
  });

  it('A-slow/B-fast control responses never bind controls to the stale run', async () => {
    const { client } = await p1RacingClient(null);
    const { transport, gate } = instantControlTransport('run-A');
    const view = new LoopConsoleView({}, client, control.createControlClient({ transport }));
    await view.refresh();

    const slowSelect = view.selectRun('run-A');
    await view.selectRun('run-B');
    gate.resolve();
    await slowSelect;

    assert.equal(view.state.selectedRunId, 'run-B');
    assert.equal(view.state.detail.runId, 'run-B');
    assert.equal(view.state.control.status, 'ready');
    assert.equal(view.state.control.actions.runId, 'run-B', 'stale A control payload must be discarded');
  });

  it('back/clear invalidates an in-flight selection', async () => {
    const { gate, client } = await p1RacingClient('run-A');
    const view = new LoopConsoleView({}, client, null);
    await view.refresh();

    const slowSelect = view.selectRun('run-A');
    view.clearSelection();
    gate.resolve();
    await slowSelect;

    assert.equal(view.state.selectedRunId, null);
    assert.equal(view.state.detail, null);
    assert.equal(view.state.control.status, 'unavailable');
  });

  it('refresh invalidates an in-flight control load', async () => {
    const { client } = await p1RacingClient(null);
    const gate = deferred();
    let actionsCalls = 0;
    const transport = async (request) => {
      const url = new URL(request.url);
      if (url.pathname.startsWith('/control/v1/actions/')) {
        const runId = decodeURIComponent(url.pathname.slice('/control/v1/actions/'.length));
        if (runId === 'run-A') {
          actionsCalls += 1;
          await gate.promise;
          const body = await actionsForRun(runId);
          if (actionsCalls === 1) {
            // The stale generation's payload is marked so any leaked write
            // is visible in the final state.
            body.data = { ...body.data, run_id: 'stale-gen1-response' };
          }
          return { status: 200, json: body };
        }
        return { status: 200, json: await actionsForRun(runId) };
      }
      if (url.pathname === '/control/v1/intents') {
        return { status: 200, json: await p2Fixture('intents-empty.json') };
      }
      return { status: 404, json: envelope(null) };
    };
    const view = new LoopConsoleView({}, client, control.createControlClient({ transport }));
    await view.refresh();

    const slowSelect = view.selectRun('run-A');
    await new Promise((resolve) => setTimeout(resolve, 10));
    const refreshPromise = view.refresh(); // bumps the generation, re-selects run-A
    gate.resolve();
    await Promise.all([slowSelect, refreshPromise]);

    assert.equal(view.state.control.status, 'ready');
    assert.equal(view.state.control.actions.runId, 'run-A', 'stale gen1 payload must be discarded');
  });
});

describe('receipt persistence across outages (P1)', () => {
  it('keeps previously loaded receipt history visible when a later refresh is unreachable', async () => {
    const detailBody = await p2Fixture('p1-detail-running.json');
    let failGets = false;
    const history = await p2Fixture('intents-history.json');
    const transport = async (request) => {
      const url = new URL(request.url);
      if (failGets) throw new Error('connection refused');
      if (url.pathname.startsWith('/control/v1/actions/')) {
        return { status: 200, json: await p2Fixture('actions-running.json') };
      }
      if (url.pathname === '/control/v1/intents') return { status: 200, json: history };
      return { status: 404, json: envelope(null) };
    };
    const view = new LoopConsoleView(
      {},
      await p1Client(detailBody),
      control.createControlClient({ transport }),
    );
    await view.refresh();
    await view.selectRun('synthetic-run-running-0001');
    assert.equal(view.state.control.intents.length, 4);

    failGets = true;
    await view.selectRun('synthetic-run-running-0001');

    assert.equal(view.state.control.status, 'unreachable');
    assert.equal(view.state.control.intents.length, 4, 'loaded receipts remain in memory');
    assert.ok(view.contentEl.text.includes('控制服务不可用：写操作已禁用，上方只读数据不受影响。'));
    assert.ok(view.contentEl.text.includes('意图历史与回执（最近 4 条）'));
    assert.ok(view.contentEl.text.includes('synthetic-intent-0001'));
  });

  it('renders the confirmed receipt and the outage after POST success -> GET failure', async () => {
    const detailBody = await p2Fixture('p1-detail-running.json');
    let failGets = false;
    const transport = async (request) => {
      const url = new URL(request.url);
      const key = `${request.method} ${url.pathname}`;
      if (failGets && key.startsWith('GET')) throw new Error('connection refused');
      if (key === 'POST /control/v1/intents') {
        return { status: 202, json: envelope(receiptFromBody(JSON.parse(request.body))) };
      }
      if (key.startsWith('GET /control/v1/actions/')) {
        return { status: 200, json: await p2Fixture('actions-running.json') };
      }
      if (key === 'GET /control/v1/intents') {
        return { status: 200, json: await p2Fixture('intents-empty.json') };
      }
      return { status: 404, json: envelope(null) };
    };
    const view = new LoopConsoleView(
      {},
      await p1Client(detailBody),
      control.createControlClient({ transport }),
    );
    await view.refresh();
    await view.selectRun('synthetic-run-running-0001');
    view.openDialog('pause');
    failGets = true; // the post-submit refresh fails
    await view.confirmDialog();

    assert.equal(view.state.control.status, 'unreachable');
    const text = view.contentEl.text;
    assert.ok(text.includes('意图 ID：synthetic-intent-new-0001（不可变）'), 'receipt survives outage');
    assert.ok(text.includes('意图状态：已执行'));
    assert.ok(text.includes('控制服务不可用：写操作已禁用，上方只读数据不受影响。'), 'outage also visible');
  });

  it('receipt renders even while the control service is unreachable', async () => {
    const state = await baseState({
      status: 'unreachable',
      actions: null,
      error: 'unreachable',
      dialog: null,
      submitting: false,
      submitError: null,
      lastReceipt: model.receiptView(receiptFromBody({
        run_id: 'synthetic-run-running-0001',
        action: 'pause',
        expected_sequence: 7,
        idempotency_key: 'a'.repeat(64),
        arguments: {},
        requester: 'nigo',
      })),
      duplicate: false,
      intents: [],
    });
    const root = new FakeEl('div');
    renderConsole(root, state, noopHandlers);
    assert.ok(root.text.includes('意图状态：已执行'));
    assert.ok(root.text.includes('控制服务不可用：写操作已禁用，上方只读数据不受影响。'));
  });
});

describe('endpoint invariant and compact default (P1)', () => {
  it('baseUrl option can no longer override the frozen endpoint', async () => {
    const calls = [];
    const client = control.createControlClient({
      transport: async (request) => {
        calls.push(request.url);
        return { status: 200, json: envelope({ status: 'ok' }) };
      },
      baseUrl: 'http://evil.example.com:9999/control/v1',
    });
    await client.health();
    assert.equal(calls[0], 'http://127.0.0.1:5680/control/v1/health');
  });

  it('queue and all-runs collapse to count summaries by default; attention is not repeated', async () => {
    const root = new FakeEl('div');
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    renderConsole(root, state, noopHandlers);

    const queueDetails = root.querySelector('.lc-queue .lc-collapsed-section');
    const runsDetails = root.querySelector('.lc-runs .lc-collapsed-section');
    assert.equal(queueDetails.tagName, 'DETAILS');
    assert.equal(runsDetails.tagName, 'DETAILS');
    assert.equal(queueDetails.getAttribute('open'), null, 'queue collapsed by default');
    assert.equal(runsDetails.getAttribute('open'), null, 'runs collapsed by default');
    assert.ok(queueDetails.text.includes('待路由队列（3 条）'));
    assert.ok(runsDetails.text.includes('全部运行（5 条）'));
    assert.ok(runsDetails.text.includes('3 条需要关注的运行已在上方显示，这里不再重复'));
    // Attention records appear exactly once in the whole view.
    const escalatedRows = root
      .querySelectorAll('.lc-row')
      .filter((el) => el.text.includes('synthetic-run-escalated-0003'));
    assert.equal(escalatedRows.length, 1);
  });

  it('toggle events persist the open state in the view model', async () => {
    const root = new FakeEl('div');
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    let queueOpen = null;
    let runsOpen = null;
    renderConsole(root, state, {
      ...noopHandlers,
      onToggleSection: (section, open) => {
        if (section === 'queue') queueOpen = open;
        if (section === 'runs') runsOpen = open;
      },
    });
    const queueDetails = root.querySelector('.lc-queue .lc-collapsed-section');
    queueDetails.open = true;
    for (const fn of queueDetails.listeners.toggle || []) fn({ target: queueDetails });
    assert.equal(queueOpen, true);
    const runsDetails = root.querySelector('.lc-runs .lc-collapsed-section');
    runsDetails.open = false;
    for (const fn of runsDetails.listeners.toggle || []) fn({ target: runsDetails });
    assert.equal(runsOpen, false);
  });
});

describe('modal keyboard contract (P1)', () => {
  async function dialogState(action) {
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    state.control.dialog = {
      action: action || 'pause',
      runId: 'synthetic-run-running-0001',
      status: 'running',
      statusLabel: '运行中',
      expectedSequence: 7,
      priority: 0,
      step: 1,
    };
    return state;
  }

  it('Escape cancels the dialog', async () => {
    const root = new FakeEl('div');
    let cancelled = 0;
    renderConsole(root, await dialogState(), { ...noopHandlers, onDialogCancel: () => { cancelled += 1; } });
    const overlay = root.querySelector('.lc-dialog-overlay');
    assert.ok(overlay, 'overlay rendered');
    for (const fn of overlay.listeners.keydown || []) fn({ key: 'Escape', preventDefault() {} });
    assert.equal(cancelled, 1);
  });

  it('Tab and Shift+Tab stay trapped inside the dialog', async () => {
    const root = new FakeEl('div');
    renderConsole(root, await dialogState(), noopHandlers);
    const overlay = root.querySelector('.lc-dialog-overlay');
    const confirm = overlay.querySelector('.lc-dialog-confirm');
    const cancel = overlay.querySelector('.lc-dialog-cancel');
    const focused = [];
    confirm.focus = () => focused.push('confirm');
    cancel.focus = () => focused.push('cancel');

    const tab = { key: 'Tab', shiftKey: false, preventDefault() {} };
    for (const fn of overlay.listeners.keydown || []) fn(tab);
    assert.deepEqual(focused, ['confirm'], 'Tab from nowhere lands on first control');
    for (const fn of overlay.listeners.keydown || []) fn(tab);
    assert.deepEqual(focused, ['confirm', 'cancel'], 'Tab moves forward');
    for (const fn of overlay.listeners.keydown || []) fn(tab);
    assert.deepEqual(focused, ['confirm', 'cancel', 'confirm'], 'Tab wraps to first');
    for (const fn of overlay.listeners.keydown || []) fn({ key: 'Tab', shiftKey: true, preventDefault() {} });
    assert.equal(focused.at(-1), 'cancel', 'Shift+Tab wraps to last');
  });

  it('the whole background is inert while the dialog is open, overlay excluded', async () => {
    const root = new FakeEl('div');
    renderConsole(root, await dialogState(), noopHandlers);
    const header = root.querySelector('.lc-header');
    const body = root.querySelector('.lc-body');
    assert.equal(header.getAttribute('inert'), '');
    assert.equal(header.getAttribute('aria-hidden'), 'true');
    assert.equal(body.getAttribute('inert'), '');
    assert.equal(body.getAttribute('aria-hidden'), 'true');
    // The refresh control lives inside the inert header, i.e. excluded.
    assert.ok(header.querySelector('.lc-refresh'), 'refresh button is inside the inert header');
    const overlay = root.querySelector('.lc-dialog-overlay');
    assert.equal(overlay.getAttribute('inert'), null, 'dialog overlay must stay interactive');
    const plain = new FakeEl('div');
    renderConsole(plain, await baseState(await readyControl('actions-running.json', 'intents-empty.json')), noopHandlers);
    assert.equal(plain.querySelector('.lc-header').getAttribute('inert'), null);
    assert.equal(plain.querySelector('.lc-body').getAttribute('inert'), null);
  });

  it('focus restores to the trigger resolved by stable run/action selector', async () => {
    const { view } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    await view.selectRun('synthetic-run-running-0001');
    view.openDialog('pause');
    assert.deepEqual(view.dialogTrigger, { label: '暂停', runId: 'synthetic-run-running-0001' });
    // Simulate the closed dialog: render() rebuilds the DOM, then the
    // restore must find the trigger by its stable aria-label in the NEW tree.
    view.state.control.dialog = null;
    view.render();
    const wanted = '暂停运行 synthetic-run-running-0001';
    const target = view.contentEl
      .querySelectorAll('.lc-control-btn')
      .find((el) => el.getAttribute('aria-label') === wanted);
    assert.ok(target, 'trigger button exists with stable aria-label in rebuilt DOM');
    let focused = false;
    target.focus = () => {
      focused = true;
    };
    view.restoreDialogFocus();
    assert.equal(focused, true, 'focus returned to the trigger in the rebuilt DOM');
    assert.equal(view.dialogTrigger, null);
  });
});

// ---------------------------------------------------------------------------
// Revision 3: NodeList-compatible focus restore + filter isolation
// ---------------------------------------------------------------------------

describe('revision 3 regressions', () => {
  it('focus restore works with a real NodeList (no Array.find on it)', async () => {
    const { view } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    await view.selectRun('synthetic-run-running-0001');
    view.openDialog('pause');
    view.state.control.dialog = null;
    view.render();

    // Replace querySelectorAll with a NodeList-compatible object that has no
    // Array methods — the exact shape real DOM returns.
    const buttons = view.contentEl.querySelectorAll('.lc-control-btn');
    const nodeList = {
      length: buttons.length,
      item(index) {
        return this[index] || null;
      },
      forEach(fn) {
        for (let i = 0; i < this.length; i += 1) fn(this[i], i, this);
      },
    };
    buttons.forEach((btn, i) => {
      nodeList[i] = btn;
    });
    const originalQSA = view.contentEl.querySelectorAll;
    view.contentEl.querySelectorAll = (selector) =>
      selector === '.lc-control-btn' ? nodeList : originalQSA.call(view.contentEl, selector);
    assert.equal(typeof nodeList.find, 'undefined', 'NodeList has no .find');

    let focused = false;
    const wanted = nodeList.item(0);
    wanted.focus = () => {
      focused = true;
    };
    view.dialogTrigger = { label: '暂停', runId: 'synthetic-run-running-0001' };
    view.restoreDialogFocus();
    view.contentEl.querySelectorAll = originalQSA;
    assert.equal(focused, true, 'focus restored via Array.from over the NodeList');
  });

  it('the all-runs filter never changes 需要关注 or the true total', async () => {
    const { view } = await makeView({
      actions: await p2Fixture('actions-running.json'),
      intents: await p2Fixture('intents-empty.json'),
    });
    // view.refresh() already ran inside makeView with the full fixture set:
    // 5 runs total, 3 attention rows.
    assert.equal(view.state.attention.length, 3);
    assert.equal(view.state.runsTotal, 5);

    const transport = async (request) => {
      const url = new URL(request.url);
      if (url.pathname === '/loop/v1/runs') {
        const status = url.searchParams.get('status');
        const body = await p1Fixture('runs.json');
        const items = status ? body.items.filter((i) => i.status === status) : body.items;
        return { status: 200, json: { ...body, items } };
      }
      if (url.pathname === '/loop/v1/health') return { status: 200, json: await p1Fixture('health-fresh.json') };
      if (url.pathname === '/loop/v1/queue') return { status: 200, json: await p1Fixture('queue.json') };
      return { status: 200, json: await p2Fixture('p1-detail-running.json') };
    };
    view.client = {
      health: async () => (await transport({ url: 'http://127.0.0.1:5679/loop/v1/health', method: 'GET' })).json,
      queue: async () => (await transport({ url: 'http://127.0.0.1:5679/loop/v1/queue', method: 'GET' })).json,
      runs: async (filters) => {
        const status = filters && filters.status ? `?status=${filters.status}` : '';
        return (await transport({ url: `http://127.0.0.1:5679/loop/v1/runs${status}`, method: 'GET' })).json;
      },
      runDetail: async () => p2Fixture('p1-detail-running.json'),
    };

    await view.setFilter('passed');
    assert.equal(view.state.filter.status, 'passed');
    assert.equal(view.state.attention.length, 3, 'filter must not erase 需要关注');
    assert.equal(view.state.runsTotal, 5, 'summary keeps the true total');
    assert.equal(view.state.runs.length, 1, 'secondary list is filtered');
    const summaryText = view.contentEl.text;
    assert.ok(summaryText.includes('全部运行（5 条）'), 'true total shown');
    assert.ok(summaryText.includes('需要关注'));
    assert.ok(view.contentEl.querySelector('.lc-attention').text.includes('synthetic-loop-gamma-v1'));
  });

  it('attention cards lead with the fragment subject and keep ids in the raw line', async () => {
    const root = new FakeEl('div');
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    renderConsole(root, state, noopHandlers);
    const firstRow = root.querySelector('.lc-attention .lc-row');
    const title = firstRow.querySelector('.lc-row-id').text;
    assert.ok(!/^[0-9a-f-]{36}$/.test(title), `title must not be a bare UUID: ${title}`);
    assert.ok(title.startsWith('合成主题：'), `title is the fragment subject: ${title}`);
    assert.ok(firstRow.text.includes('需要处理：打开查看可用操作'), 'generic hint present');
    assert.ok(!/需要决定：.*(继续|重试|终止|暂停)/.test(firstRow.text), 'no concrete action guessed');
    const rawLine = firstRow.querySelector('.lc-row-raw').text;
    assert.ok(rawLine.includes('LoopSpec：'), 'LoopSpec in raw line');
    assert.ok(rawLine.includes('run：'), 'UUID in raw line');
  });
});

// ---------------------------------------------------------------------------
// Revision 5: authoritative attempt currentness + per-fragment subjects
// ---------------------------------------------------------------------------

function familyEnvelope() {
  const base = {
    parent_run_id: null,
    loop_id: 'phone-fragment-link-v1',
    goal: '把经 nigo 批准的手机链接碎片转化为可核验、可行动、可追溯的结果',
    subject: null,
    attempt_group_id: 'fam-real-1',
    is_current: true,
    superseded_by_run_id: null,
    current_node: 'independent_eval',
    iterations: 3,
    events: 10,
    evaluator_version: 'eval-v1',
    budget_used: { active_tokens: 100, calls: 2, elapsed_seconds: 60 },
    stop_reason: null,
    updated_at: '2026-01-01T00:10:00+08:00',
  };
  const attempts = [
    { run_id: 'real-a1-escalated', status: 'escalated', display_state: 'awaiting_human', stop_reason: 'evaluator_requested_revision' },
    { run_id: 'real-a2-blocked', status: 'blocked', display_state: 'stopped', stop_reason: 'internal_error' },
    { run_id: 'real-a3-exhausted', status: 'exhausted', display_state: 'stopped', stop_reason: 'token_budget_exhausted' },
    { run_id: 'real-a4-passed', status: 'passed', display_state: 'completed', stop_reason: null },
  ];
  return {
    items: attempts.map((attempt) => ({
      ...base,
      ...attempt,
      fragment_id: 'real-fragment-1',
      is_current: false,
      superseded_by_run_id: attempt.run_id === 'real-a4-passed' ? null : 'real-a4-passed',
    })),
  };
}

describe('revision 5 regressions', () => {
  it('a family with a later passed attempt yields zero attention cards but keeps history', async () => {
    const envelope = familyEnvelope();
    const rows = model.attentionRows(envelope);
    assert.equal(rows.length, 0, 'resolved family must not appear in 需要关注');

    // The superseded attempts remain in the collapsed all-runs surface.
    const root = new FakeEl('div');
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    state.runs = model.runRows(envelope, { status: null });
    state.attention = rows;
    state.runsTotal = envelope.items.length;
    renderConsole(root, state, noopHandlers);
    assert.ok(root.text.includes('没有等待人工、异常停止或预算风险的运行'));
    const runsSection = root.querySelector('.lc-runs');
    assert.ok(runsSection.text.includes('全部运行（4 条）'));
    const historical = runsSection.querySelectorAll('.lc-row').map((el) => el.text);
    assert.ok(
      historical.some((t) => t.includes('real-a1-escalated')),
      'failed attempts stay available in collapsed history',
    );
  });

  it('card title priority is subject, then the labelled fallback (never goal)', async () => {
    const envelope = familyEnvelope();
    envelope.items[0] = {
      ...envelope.items[0],
      run_id: 'real-b1-escalated',
      is_current: true,
      superseded_by_run_id: null,
      subject: '主题：来自碎片的真实标题',
    };
    const withSubject = model.attentionRows(envelope);
    assert.equal(withSubject[0].title, '主题：来自碎片的真实标题');

    envelope.items[0] = { ...envelope.items[0], subject: null };
    const withGoal = model.attentionRows(envelope);
    assert.ok(
      withGoal[0].title.startsWith('待获取标题 · '),
      `goal is never the identifying title: ${withGoal[0].title}`,
    );

    envelope.items[0] = { ...envelope.items[0], goal: null, subject: null };
    const withLoop = model.attentionRows(envelope);
    assert.ok(withLoop[0].title.startsWith('待获取标题 · '));
    assert.ok(!withLoop[0].title.includes('phone-fragment-link-v1'));
  });

  it('same LoopSpec and generic goal stay distinguishable via fragment subjects', async () => {
    const envelope = familyEnvelope();
    const alpha = {
      ...envelope.items[0],
      run_id: 'real-c1-escalated',
      fragment_id: 'real-fragment-alpha',
      attempt_group_id: 'fam-alpha',
      is_current: true,
      superseded_by_run_id: null,
      subject: '主题甲：多智能体路由文章',
    };
    const beta = {
      ...envelope.items[0],
      run_id: 'real-c2-blocked',
      status: 'blocked',
      display_state: 'stopped',
      fragment_id: 'real-fragment-beta',
      attempt_group_id: 'fam-beta',
      is_current: true,
      superseded_by_run_id: null,
      subject: '主题乙：Token 经济模型文章',
    };
    const rows = model.attentionRows({ items: [alpha, beta] });
    assert.equal(rows.length, 2);
    const titles = new Set(rows.map((r) => r.title));
    assert.deepEqual(titles, new Set(['主题甲：多智能体路由文章', '主题乙：Token 经济模型文章']));
  });

  it('older providers without is_current are treated as current', async () => {
    const body = await p1Fixture('runs.json');
    const rows = model.attentionRows(body);
    assert.equal(rows.length, 3, 'missing field must not silently empty the surface');
  });
});

// ---------------------------------------------------------------------------
// Revision 6: semantic queue titles and episode-aware attention
// ---------------------------------------------------------------------------

describe('revision 6 regressions', () => {
  it('queue rows lead with subject, never a bare UUID or shared goal, with ids in the raw line', async () => {
    const body = await p1Fixture('queue.json');
    const withSubjects = {
      ...body,
      items: body.items.map((item, index) => ({
        ...item,
        loop_id: 'phone-fragment-link-v1',
        goal: '把经 nigo 批准的手机链接碎片转化为可核验、可行动、可追溯的结果',
        subject: index === 0 ? '主题：Queue 语义标题样例' : null,
        subject_source: index === 0 ? 'user_text' : null,
      })),
    };
    const rows = model.queueRows(withSubjects);
    assert.equal(rows[0].title, '主题：Queue 语义标题样例');
    assert.ok(rows[1].title.startsWith('待获取标题 · '), `shared goal is not the title: ${rows[1].title}`);

    const root = new FakeEl('div');
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    state.queue = rows;
    renderConsole(root, state, noopHandlers);
    const queueSection = root.querySelector('.lc-queue');
    const firstTitle = queueSection.querySelector('.lc-row .lc-row-id').text;
    assert.ok(!/^[0-9a-f-]{36}$/.test(firstTitle), `queue title must not be a UUID: ${firstTitle}`);
    assert.ok(queueSection.text.includes('LoopSpec：phone-fragment-link-v1'));
    assert.ok(queueSection.text.includes('run：synthetic-queue-0001'), 'run id stays in raw line');
  });

  it('queue titles use the labelled fallback when no subject exists', () => {
    const rows = model.queueRows({
      items: [{
        run_id: 'synthetic-queue-x',
        fragment_id: '2026-07-19-19-52-09-b032997b',
        loop_id: 'loop-x',
        status: 'approved',
        display_state: 'queued',
        next_step: '选择',
        updated_at: '2026-01-01T00:00:00+08:00',
      }],
    });
    assert.equal(rows[0].title, '待获取标题 · 07-19 19:52:09');
    assert.ok(!rows[0].title.includes('loop-x'), 'LoopSpec is not the fallback title');
  });

  it('a reopened episode attempt renders current while the old episode stays in history', async () => {
    const envelope = familyEnvelope();
    const reopened = {
      ...envelope.items[0],
      run_id: 'real-reopened-escalated',
      attempt_episode: 1,
      is_current: true,
      superseded_by_run_id: null,
    };
    const rows = model.attentionRows({ items: [...envelope.items, reopened] });
    assert.equal(rows.length, 1, 'only the reopened episode is current');
    assert.equal(rows[0].runId, 'real-reopened-escalated');

    const root = new FakeEl('div');
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    state.runs = model.runRows({ items: [...envelope.items, reopened] }, { status: null });
    state.attention = rows;
    state.runsTotal = 5;
    renderConsole(root, state, noopHandlers);
    const attention = root.querySelector('.lc-attention');
    assert.equal(attention.querySelectorAll('.lc-row').length, 1);
    const runsSection = root.querySelector('.lc-runs');
    const historical = runsSection.querySelectorAll('.lc-row').map((el) => el.text);
    assert.ok(historical.some((t) => t.includes('real-a1-escalated')), 'old episode stays in history');
  });

  it('subject_source is shown in the raw line when present', async () => {
    const body = await p1Fixture('runs.json');
    const mutated = {
      ...body,
      items: body.items.map((item) => ({
        ...item,
        subject: item.goal,
        subject_source: 'user_text',
      })),
    };
    const rows = model.attentionRows(mutated);
    const root = new FakeEl('div');
    const state = await baseState(await readyControl('actions-running.json', 'intents-empty.json'));
    state.attention = rows;
    renderConsole(root, state, noopHandlers);
    assert.ok(root.text.includes('标题来源：user_text'));
  });
});
