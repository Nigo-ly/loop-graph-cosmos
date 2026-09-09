import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

const proposalsApi = require(path.join(ROOT, 'src', 'console', 'proposal-client.js'));
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

// ---------------------------------------------------------------------------
// Synthetic shadow data (test fixtures only; never presented as real data).
// ---------------------------------------------------------------------------

function shadowEnvelope(payloadKey, payload, overrides = {}) {
  return {
    contract_version: '1',
    generated_at: '2026-07-20T10:00:00+08:00',
    provider_version: 'p3b-synthetic',
    db_mode: 'read_only',
    ledger_present: true,
    source_event_id: 6,
    stale: false,
    [payloadKey]: payload,
    ...overrides,
  };
}

function proposalItem(overrides = {}) {
  return {
    proposal_id: 'proposal-1',
    run_id: 'run-1',
    fragment_id: '2026-07-20-01-02-03-aabbccdd',
    attempt_episode: 0,
    source_sequence: 7,
    subject: '真实碎片主题',
    loop_status: 'approved',
    proposed_loopspec: 'phone-fragment-link-v1',
    loopspec_version: '1.0.0',
    route_reason: '测试路由理由',
    worker_agent: 'hermes-k27-research',
    worker_model: 'kimi-k2.7-code',
    independent_evaluator: 'deepseek-pro-evaluator',
    verifier: 'codex-independent-source-verifier-v1',
    budget: { limits: { tokens: 50000, tool_calls: 20, iterations: 8, seconds: 1800 }, used: { tokens: 0, tool_calls: 0 } },
    risk_level: 'medium',
    risk_reasons: ['untrusted_web_input', 'privacy_content'],
    required_tools: ['web_fetch'],
    external_side_effects: 'none',
    stop_conditions: [
      { kind: 'budget_hard_gate', limits: {} },
      { kind: 'verifier_disagreement' },
      { kind: 'safety' },
      { kind: 'side_effect_unknown' },
      { kind: 'no_gain', max_consecutive_no_gain: 2 },
    ],
    execution_ready: true,
    blocked_reasons: [],
    dispatcher_version: 'p3a-2026-07-20.2',
    fingerprint: 'fingerprint-abc',
    created_at: '2026-07-20T01:00:00+00:00',
    expires_at: '2026-07-20T02:00:00+00:00',
    proposal_status: 'proposed',
    supersedes_proposal_id: null,
    extra: { verifier_level: 'independent_model' },
    last_event: { to_status: 'proposed', reason: 'scanned', created_at: '2026-07-20T01:00:00+00:00' },
    ...overrides,
  };
}

function syntheticItems() {
  return [
    proposalItem({ proposal_id: 'p-proposed', run_id: 'run-a', subject: '可执行的碎片' }),
    proposalItem({
      proposal_id: 'p-blocked',
      run_id: 'run-b',
      subject: '被阻塞的碎片',
      proposal_status: 'blocked',
      execution_ready: false,
      blocked_reasons: ['worker_unavailable:health_unknown:health_not_checked', 'evaluator_unavailable:health_unknown:not_health_checked'],
      last_event: { to_status: 'blocked', reason: 'scanned', created_at: '2026-07-20T01:05:00+00:00' },
    }),
    proposalItem({
      proposal_id: 'p-human',
      run_id: 'run-c',
      subject: null,
      fragment_id: '2026-07-19-08-09-10-ccddee00',
      proposal_status: 'needs_human_routing',
      execution_ready: false,
      blocked_reasons: ['loopspec_not_registered'],
      last_event: { to_status: 'needs_human_routing', reason: 'scanned', created_at: '2026-07-20T01:06:00+00:00' },
    }),
    proposalItem({
      proposal_id: 'p-stale',
      run_id: 'run-d',
      subject: '已过期的碎片',
      proposal_status: 'stale',
      execution_ready: false,
      last_event: { to_status: 'stale', reason: 'sequence_advanced', created_at: '2026-07-20T01:10:00+00:00' },
    }),
    proposalItem({
      proposal_id: 'p-superseded',
      run_id: 'run-e',
      subject: '被取代的旧建议',
      proposal_status: 'superseded',
      execution_ready: false,
      last_event: { to_status: 'superseded', reason: 'registry_or_policy_changed', created_at: '2026-07-20T01:12:00+00:00' },
    }),
    proposalItem({
      proposal_id: 'p-replacement',
      run_id: 'run-e',
      subject: '被取代的碎片（新建议）',
      proposal_status: 'blocked',
      execution_ready: false,
      blocked_reasons: ['worker_unavailable:health_unknown:health_not_checked'],
      supersedes_proposal_id: 'p-superseded',
      last_event: { to_status: 'blocked', reason: 'scanned', created_at: '2026-07-20T01:13:00+00:00' },
    }),
  ];
}

function recordingTransport(routes, calls) {
  return async (request) => {
    calls.push({ url: request.url, method: request.method });
    const url = new URL(request.url);
    const key = url.pathname + url.search;
    const handler = routes[key] || routes[url.pathname];
    if (!handler) return { status: 404, json: null };
    return handler(request);
  };
}

// ---------------------------------------------------------------------------
// 中文表驱动标签
// ---------------------------------------------------------------------------

describe('P3B Chinese label tables', () => {
  it('maps every frozen proposal status to its Chinese label', () => {
    assert.equal(model.PROPOSAL_STATUS_LABELS.proposed, '已生成建议');
    assert.equal(model.PROPOSAL_STATUS_LABELS.needs_human_routing, '需要人工决定路由');
    assert.equal(model.PROPOSAL_STATUS_LABELS.blocked, '当前被阻塞');
    assert.equal(model.PROPOSAL_STATUS_LABELS.stale, '建议已经过期');
    assert.equal(model.PROPOSAL_STATUS_LABELS.superseded, '已被新建议取代');
    assert.equal(model.proposalStatusLabel('proposed'), '已生成建议');
    assert.equal(model.proposalStatusLabel('needs_human_routing'), '需要人工决定路由');
    assert.equal(model.proposalStatusLabel('blocked'), '当前被阻塞');
    assert.equal(model.proposalStatusLabel('stale'), '建议已经过期');
    assert.equal(model.proposalStatusLabel('superseded'), '已被新建议取代');
  });

  it('falls back to 未知状态 for unknown values and never guesses', () => {
    assert.equal(model.proposalStatusLabel('something_new'), '未知状态');
    assert.equal(model.proposalStatusLabel('approved'), '未知状态'); // deliberate: shadow proposals never use it
    assert.equal(model.proposalStatusLabel(''), '—');
    assert.equal(model.proposalStatusLabel(null), '—');
    assert.equal(model.riskLabel('severe'), '未知状态');
    assert.equal(model.sideEffectLabel('teleport'), '未知状态');
  });

  it('maps readiness, risk, side effects, event reasons, stop conditions', () => {
    assert.equal(model.readinessLabel(true), '具备执行条件');
    assert.equal(model.readinessLabel(false), '暂不具备执行条件');
    assert.equal(model.riskLabel('low'), '低风险');
    assert.equal(model.riskLabel('medium'), '中风险');
    assert.equal(model.riskLabel('high'), '高风险');
    assert.equal(model.sideEffectLabel('none'), '无外部副作用');
    assert.equal(model.sideEffectLabel('read_only'), '只读外部访问');
    assert.equal(model.sideEffectLabel('write'), '有外部写入副作用');
    assert.equal(model.sideEffectLabel('unknown'), '外部副作用未知');
    assert.equal(model.proposalEventReasonText('scanned'), '首次扫描生成');
    assert.equal(model.proposalEventReasonText('sequence_advanced'), '来源序号已前进');
    assert.equal(model.proposalEventReasonText('run_left_queue'), '运行已离开当前队列');
    assert.equal(model.proposalEventReasonText('registry_or_policy_changed'), '注册表或策略已变化');
    assert.equal(model.proposalEventReasonText('mystery'), 'mystery');
    assert.equal(model.stopConditionLabel('budget_hard_gate'), '预算硬门');
    assert.equal(model.stopConditionLabel('no_gain'), '连续无增益停止');
    assert.equal(model.stopConditionLabel('unknown_kind'), 'unknown_kind');
  });

  it('translates known blocked-reason codes, keeps raw detail, passes unknown raw', () => {
    assert.equal(model.blockedReasonText('loopspec_not_registered'), '未注册 LoopSpec');
    assert.equal(
      model.blockedReasonText('worker_unavailable:health_unknown:health_not_checked'),
      '执行模型不可用（health_unknown:health_not_checked）',
    );
    assert.equal(model.blockedReasonText('budget_exceeded:tokens'), '预算已超限（tokens）');
    assert.equal(model.blockedReasonText('totally_new_code:x'), 'totally_new_code:x');
    assert.equal(model.blockedReasonText(null), null);
  });
});

// ---------------------------------------------------------------------------
// 视图模型
// ---------------------------------------------------------------------------

describe('P3B proposal view models', () => {
  it('splits current vs history and resolves the replacement pointer', () => {
    const items = syntheticItems();
    const current = model.proposalCurrentRows(items);
    const history = model.proposalHistoryRows(items);
    assert.deepEqual(current.map((c) => c.proposalId).sort(), ['p-blocked', 'p-human', 'p-proposed', 'p-replacement'].sort());
    assert.deepEqual(history.map((h) => h.proposalId).sort(), ['p-stale', 'p-superseded'].sort());
    const superseded = history.find((h) => h.proposalId === 'p-superseded');
    assert.equal(superseded.supersededByProposalId, 'p-replacement');
    assert.equal(superseded.transitionReasonText, '注册表或策略已变化');
    const stale = history.find((h) => h.proposalId === 'p-stale');
    assert.equal(stale.transitionReasonText, '来源序号已前进');
    assert.equal(stale.supersededByProposalId, null);
  });

  it('card row carries subject title, Chinese status, model, readiness, key reason', () => {
    const items = syntheticItems();
    const current = model.proposalCurrentRows(items);
    const proposed = current.find((c) => c.proposalId === 'p-proposed');
    assert.equal(proposed.title, '可执行的碎片');
    assert.equal(proposed.statusLabel, '已生成建议');
    assert.equal(proposed.workerModel, 'kimi-k2.7-code');
    assert.equal(proposed.readinessLabel, '具备执行条件');
    assert.equal(proposed.keyReason, '测试路由理由');
    const blocked = current.find((c) => c.proposalId === 'p-blocked');
    assert.equal(blocked.statusLabel, '当前被阻塞');
    assert.equal(blocked.readinessLabel, '暂不具备执行条件');
    assert.equal(blocked.keyReason, '执行模型不可用（health_unknown:health_not_checked）');
    const human = current.find((c) => c.proposalId === 'p-human');
    assert.equal(human.statusLabel, '需要人工决定路由');
    assert.match(human.title, /^待获取碎片主题 · 07-19 08:09:10$/); // distinguishable fallback from fragment_id
    assert.equal(human.keyReason, '未注册 LoopSpec');
  });

  it('subjectless cards sharing one goal stay distinguishable and never show the goal as title', () => {
    const sharedGoal = '把经 nigo 批准的手机链接碎片转化为可核验、可行动、可追溯的结果';
    const items = [
      proposalItem({ proposal_id: 'p-no-a', run_id: 'run-na', subject: '', fragment_id: '2026-07-19-08-09-10-aaaaaaaa', extra: { goal: sharedGoal } }),
      proposalItem({ proposal_id: 'p-no-b', run_id: 'run-nb', subject: '', fragment_id: '2026-07-19-08-11-42-bbbbbbbb', extra: { goal: sharedGoal } }),
    ];
    const current = model.proposalCurrentRows(items);
    assert.equal(current.length, 2);
    assert.notEqual(current[0].title, current[1].title, 'cards must be distinguishable');
    for (const card of current) {
      assert.match(card.title, /^待获取碎片主题 · /);
      assert.ok(!card.title.includes(sharedGoal), 'the shared goal is never a card title');
    }
    const detail = model.proposalDetailModel(items[0]);
    assert.equal(detail.goal, sharedGoal, 'the generic goal stays available in the detail view');
  });

  it('detail model carries every frozen field with raw values preserved', () => {
    const detail = model.proposalDetailModel(proposalItem({ events: [
      { from_status: null, to_status: 'proposed', reason: 'scanned', created_at: '2026-07-20T01:00:00+00:00' },
    ] }));
    assert.equal(detail.loopspec, 'phone-fragment-link-v1 · v1.0.0');
    assert.equal(detail.workerAgent, 'hermes-k27-research');
    assert.equal(detail.workerModel, 'kimi-k2.7-code');
    assert.equal(detail.independentEvaluator, 'deepseek-pro-evaluator');
    assert.equal(detail.verifier, 'codex-independent-source-verifier-v1');
    assert.equal(detail.verifierLevel, 'independent_model');
    assert.equal(detail.riskLabel, '中风险');
    assert.equal(detail.sideEffectsLabel, '无外部副作用');
    assert.equal(detail.privacyText, '涉及隐私内容');
    assert.equal(detail.untrustedWebText, '包含不可信网页输入');
    assert.equal(detail.requiredTools.join(''), 'web_fetch');
    assert.ok(detail.stopConditions.some((line) => line.includes('连续无增益停止（最多连续 2 轮）')));
    assert.equal(detail.budget.length, 4);
    assert.equal(detail.fingerprint, 'fingerprint-abc');
    assert.equal(detail.sourceSequence, 7);
    assert.equal(detail.events[0].toStatusLabel, '已生成建议');
    assert.equal(detail.events[0].reasonText, '首次扫描生成');
  });
});

// ---------------------------------------------------------------------------
// 客户端协议守卫
// ---------------------------------------------------------------------------

describe('P3B proposal client protocol guards', () => {
  it('issues GET only across every endpoint and retry', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/shadow/v1/health': async () => ({ status: 200, json: shadowEnvelope('provider_healthy', true, { last_read_at: '2026-07-20T10:00:00+08:00' }) }),
      '/shadow/v1/proposals?scope=all&limit=200': async () => ({ status: 200, json: shadowEnvelope('items', syntheticItems()) }),
      '/shadow/v1/proposals/p-proposed': async () => ({ status: 200, json: shadowEnvelope('proposal', proposalItem()) }),
    }, calls);
    const client = proposalsApi.createShadowProposalClient({ transport });
    await client.health();
    await client.proposals({ scope: 'all', limit: 200 });
    await client.proposalDetail('p-proposed');
    assert.equal(calls.length, 3);
    for (const call of calls) assert.equal(call.method, 'GET');
    assert.ok(calls.every((call) => call.url.startsWith('http://127.0.0.1:5681/shadow/v1')));
  });

  it('retries once silently then reports unreachable', async () => {
    const calls = [];
    const transport = async (request) => {
      calls.push(request);
      throw new Error('connection refused');
    };
    const client = proposalsApi.createShadowProposalClient({ transport });
    await assert.rejects(client.health(), (error) => error.kind === 'unreachable');
    assert.equal(calls.length, 2);
  });

  it('refuses a mismatched contract without retrying', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/shadow/v1/health': async () => ({ status: 200, json: { ...shadowEnvelope('provider_healthy', true), contract_version: '9' } }),
    }, calls);
    const client = proposalsApi.createShadowProposalClient({ transport });
    await assert.rejects(client.health(), (error) => error.kind === 'contract_mismatch');
    assert.equal(calls.length, 1);
  });

  it('ignores a caller-supplied baseUrl (frozen loopback trust boundary)', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/shadow/v1/health': async () => ({ status: 200, json: shadowEnvelope('provider_healthy', true, { last_read_at: '2026-07-20T10:00:00+08:00' }) }),
    }, calls);
    const client = proposalsApi.createShadowProposalClient({ transport, baseUrl: 'https://attacker.invalid/shadow/v1' });
    await client.health();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, 'http://127.0.0.1:5681/shadow/v1/health');
    assert.ok(!calls[0].url.includes('attacker.invalid'));
  });

  it('client source contains no write method, no fetch, no SQLite, no control API', async () => {
    const src = await readFile(path.join(ROOT, 'src', 'console', 'proposal-client.js'), 'utf8');
    assert.ok(!/method:\s*["'`](POST|PUT|DELETE|PATCH|HEAD|OPTIONS)["'`]/i.test(src));
    assert.ok(!/\bfetch\s*\(/.test(src));
    assert.ok(!/sqlite/i.test(src));
    assert.ok(!/127\.0\.0\.1:5680/.test(src));
    assert.ok(!/options\.baseUrl/.test(src), 'no baseUrl override may exist');
    assert.ok(src.includes('method: "GET"'));
  });
});

// ---------------------------------------------------------------------------
// 渲染（fake DOM）
// ---------------------------------------------------------------------------

function baseState(proposalsSubState) {
  return {
    status: 'ready',
    error: null,
    health: null,
    queue: [],
    queueStale: false,
    runs: [],
    runsStale: false,
    statuses: [],
    attention: [],
    runsTotal: 0,
    filter: { status: null, statusLabel: null },
    selectedRunId: null,
    detail: null,
    detailLoading: false,
    detailError: null,
    detailStale: false,
    ui: { queueOpen: false, runsOpen: false, proposalHistoryOpen: false },
    proposals: proposalsSubState,
    control: { status: 'unavailable', actions: null, error: null, dialog: null, submitting: false, submitError: null, lastReceipt: null, duplicate: false, intents: [] },
  };
}

function readyProposals(items) {
  return {
    status: 'ready',
    error: null,
    ledgerPresent: true,
    current: model.proposalCurrentRows(items),
    history: model.proposalHistoryRows(items),
    stale: false,
    selectedProposalId: null,
    detail: null,
    detailLoading: false,
    detailError: null,
  };
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
  onToggleSection() {},
  onSelectProposal() {},
  onBackProposal() {},
  onRefreshProposals() {},
};

function texts(root) {
  const out = [];
  const walk = (el) => {
    if (el.textContent) out.push(el.textContent);
    for (const child of el.children || []) walk(child);
  };
  walk(root);
  return out.join('\n');
}

function findByClass(root, cls) {
  const found = [];
  const walk = (el) => {
    if (el.classes && el.classes.has(cls)) found.push(el);
    for (const child of el.children || []) walk(child);
  };
  walk(root);
  return found;
}

describe('P3B console rendering', () => {
  it('renders current proposals with Chinese labels and keeps history collapsed', () => {
    const root = new FakeEl('div');
    renderConsole(root, baseState(readyProposals(syntheticItems())), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('路由建议'));
    assert.ok(text.includes('可执行的碎片'));
    assert.ok(text.includes('已生成建议'));
    assert.ok(text.includes('当前被阻塞'));
    assert.ok(text.includes('需要人工决定路由'));
    assert.ok(text.includes('具备执行条件'));
    assert.ok(text.includes('暂不具备执行条件'));
    assert.ok(text.includes('执行模型不可用（health_unknown:health_not_checked）'));
    assert.ok(text.includes('历史建议（2 条）'));
    // 历史区默认折叠：stale/superseded 卡片不在默认视图。
    const historyRows = findByClass(root, 'lc-proposal-history-row');
    assert.equal(historyRows.length, 2);
    const disclosure = findByClass(root, 'lc-collapsed-section')
      .map((el) => el.children.map((c) => c.textContent).join(' '))
      .find((t) => t.includes('历史建议'));
    assert.ok(disclosure);
    // 没有任何写操作按钮。
    assert.ok(!text.includes('批准'));
    assert.ok(!text.includes('驳回'));
    assert.ok(!text.includes('执行建议'));
  });

  it('shows the honest empty state when the ledger is missing', () => {
    const root = new FakeEl('div');
    const sub = readyProposals([]);
    sub.ledgerPresent = false;
    renderConsole(root, baseState(sub), noopHandlers);
    assert.ok(texts(root).includes('尚未生成影子建议'));
  });

  it('never shows a healthy state while the shadow service is down', () => {
    const root = new FakeEl('div');
    const sub = { status: 'unreachable', error: { kind: 'unreachable' }, ledgerPresent: true, current: [], history: [], stale: false, selectedProposalId: null, detail: null, detailLoading: false, detailError: null };
    renderConsole(root, baseState(sub), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('影子建议服务不可用'));
    assert.ok(!text.includes('已生成建议'));
    assert.equal(findByClass(root, 'lc-proposal-status').length, 0);
  });

  it('renders the superseded history row with a pointer to the replacement', () => {
    const root = new FakeEl('div');
    renderConsole(root, baseState(readyProposals(syntheticItems())), noopHandlers);
    const historyRows = findByClass(root, 'lc-proposal-history-row');
    const supersededRow = historyRows.find((row) => texts(row).includes('被取代的旧建议'));
    assert.ok(supersededRow);
    assert.ok(texts(supersededRow).includes('已被新建议取代'));
    assert.ok(texts(supersededRow).includes('查看新建议'));
  });

  it('renders the proposal detail with Chinese labels and raw audit values', () => {
    const root = new FakeEl('div');
    const sub = readyProposals(syntheticItems());
    sub.selectedProposalId = 'p-blocked';
    sub.detail = model.proposalDetailModel(proposalItem({
      proposal_id: 'p-blocked',
      proposal_status: 'blocked',
      execution_ready: false,
      blocked_reasons: ['worker_unavailable:health_unknown:health_not_checked'],
      events: [{ from_status: null, to_status: 'blocked', reason: 'scanned', created_at: '2026-07-20T01:05:00+00:00' }],
    }));
    renderConsole(root, baseState(sub), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('建议详情'));
    assert.ok(text.includes('当前被阻塞（原始：blocked）'));
    assert.ok(text.includes('暂不具备执行条件'));
    assert.ok(text.includes('执行模型不可用（health_unknown:health_not_checked）'));
    assert.ok(text.includes('hermes-k27-research（模型：kimi-k2.7-code）'));
    assert.ok(text.includes('codex-independent-source-verifier-v1（核验等级：independent_model）'));
    assert.ok(text.includes('涉及隐私内容'));
    assert.ok(text.includes('无外部副作用（none）'));
    assert.ok(text.includes('指纹：fingerprint-abc'));
    assert.ok(text.includes('来源序号：7'));
  });
});

// ---------------------------------------------------------------------------
// 视图级流程（obsidian stubbed）
// ---------------------------------------------------------------------------

function p1StubClient() {
  const health = { contract_version: '2', generated_at: '2026-07-20T10:00:00+08:00', provider_version: 'p1a', db_mode: 'read_only', source_sequence: 1, source_committed_at: '2026-07-20T10:00:00+08:00', stale: false, provider_healthy: true, last_read_at: '2026-07-20T10:00:00+08:00', data_age_seconds: 0 };
  const empty = { ...health, items: [] };
  return {
    async health() { return health; },
    async queue() { return empty; },
    async runs() { return empty; },
    async runDetail() { return { ...health, run: null }; },
  };
}

describe('P3B view flows', () => {
  it('loads proposals on refresh, selecting a card shows detail, back restores focus', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/shadow/v1/proposals?scope=all&limit=200': async () => ({ status: 200, json: shadowEnvelope('items', syntheticItems()) }),
      '/shadow/v1/proposals/p-blocked': async () => ({
        status: 200,
        json: shadowEnvelope('proposal', proposalItem({
          proposal_id: 'p-blocked',
          proposal_status: 'blocked',
          execution_ready: false,
          blocked_reasons: ['worker_unavailable:health_unknown:health_not_checked'],
          events: [{ from_status: null, to_status: 'blocked', reason: 'scanned', created_at: '2026-07-20T01:05:00+00:00' }],
        })),
      }),
    }, calls);
    const proposalClient = proposalsApi.createShadowProposalClient({ transport });
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalClient);
    await view.refresh();
    assert.equal(view.state.proposals.status, 'ready');
    assert.equal(view.state.proposals.current.length, 4);
    assert.equal(view.state.proposals.history.length, 2);
    assert.ok(calls.every((call) => call.method === 'GET'));

    await view.selectProposal('p-blocked');
    assert.equal(view.state.proposals.detail.proposalId, 'p-blocked');
    assert.equal(view.state.proposals.detail.statusLabel, '当前被阻塞');
    assert.ok(texts(view.contentEl).includes('建议详情'));

    view.clearProposalSelection();
    assert.equal(view.state.proposals.selectedProposalId, null);
    // 焦点回到原卡片区域（FakeEl 记录 focus 调用即视为契约成立）。
    assert.ok(view.contentEl);
  });

  it('outage renders unavailable, recovery reloads real data', async () => {
    let down = true;
    const transport = async (request) => {
      if (down) throw new Error('connection refused');
      const url = new URL(request.url);
      if (url.pathname === '/shadow/v1/proposals') {
        return { status: 200, json: shadowEnvelope('items', syntheticItems()) };
      }
      return { status: 404, json: null };
    };
    const proposalClient = proposalsApi.createShadowProposalClient({ transport });
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalClient);
    await view.refresh();
    assert.equal(view.state.proposals.status, 'unreachable');
    assert.ok(texts(view.contentEl).includes('影子建议服务不可用'));
    assert.ok(!texts(view.contentEl).includes('已生成建议'));

    down = false;
    await view.loadProposals();
    assert.equal(view.state.proposals.status, 'ready');
    assert.ok(texts(view.contentEl).includes('被阻塞的碎片'));
  });

  it('selecting a run clears the proposal selection and vice versa', async () => {
    const transport = recordingTransport({
      '/shadow/v1/proposals?scope=all&limit=200': async () => ({ status: 200, json: shadowEnvelope('items', syntheticItems()) }),
      '/shadow/v1/proposals/p-proposed': async () => ({ status: 200, json: shadowEnvelope('proposal', proposalItem({ proposal_id: 'p-proposed' })) }),
    }, []);
    const proposalClient = proposalsApi.createShadowProposalClient({ transport });
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalClient);
    await view.refresh();
    await view.selectProposal('p-proposed');
    assert.equal(view.state.proposals.selectedProposalId, 'p-proposed');
    view.state.selectedRunId = 'run-1';
    await view.selectRun('run-1');
    assert.equal(view.state.proposals.selectedProposalId, null);
  });

  it('render() preserves the scroll position of the overflowing scroller', async () => {
    const transport = recordingTransport({
      '/shadow/v1/proposals?scope=all&limit=200': async () => ({ status: 200, json: shadowEnvelope('items', syntheticItems()) }),
    }, []);
    const proposalClient = proposalsApi.createShadowProposalClient({ transport });
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalClient);
    await view.refresh();
    // Fake a real scroller: the view root overflows and is scrolled down.
    view.contentEl.scrollHeight = 1200;
    view.contentEl.clientHeight = 500;
    view.contentEl.scrollTop = 137;
    view.render();
    assert.equal(view.contentEl.scrollTop, 137, 'refresh must never yank the page back to top');
    assert.equal(view.savedScrollTop, 137);
  });

  it('render() falls back to the parent when the parent is the scroller', async () => {
    const transport = recordingTransport({
      '/shadow/v1/proposals?scope=all&limit=200': async () => ({ status: 200, json: shadowEnvelope('items', syntheticItems()) }),
    }, []);
    const proposalClient = proposalsApi.createShadowProposalClient({ transport });
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalClient);
    await view.refresh();
    const parent = new FakeEl('div');
    parent.appendChild(view.contentEl);
    view.contentEl.scrollHeight = 400;
    view.contentEl.clientHeight = 500; // root does not overflow
    parent.scrollHeight = 1400;
    parent.clientHeight = 500;
    parent.scrollTop = 88;
    view.render();
    assert.equal(parent.scrollTop, 88);
  });
});
