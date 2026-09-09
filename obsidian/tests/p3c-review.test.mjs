import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);

const reviewApi = require(path.join(ROOT, 'src', 'console', 'review-client.js'));
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
// Synthetic review data (test fixtures only).
// ---------------------------------------------------------------------------

function reviewEnvelope(data, overrides = {}) {
  return {
    contract_version: '1',
    generated_at: '2026-07-20T10:00:00+08:00',
    service_version: 'p3c-synthetic',
    data,
    ...overrides,
  };
}

function syntheticActions(overrides = {}) {
  return reviewEnvelope({
    proposal_id: 'p-1',
    submittable: true,
    reason_code: null,
    proposal_fingerprint: 'fp-abc',
    source_sequence: 7,
    proposal_status: 'blocked',
    current_decision: null,
    ...overrides,
  });
}

function syntheticReceipt(overrides = {}) {
  return {
    decision_id: 'd-1',
    idempotency_key: 'key-1',
    proposal_id: 'p-1',
    run_id: 'run-1',
    fragment_id: 'frag-1',
    proposal_fingerprint: 'fp-abc',
    source_sequence: 7,
    decision: 'accepted',
    reason: null,
    resume_condition: null,
    note: null,
    decided_by: 'nigo',
    decided_at: '2026-07-20T02:00:00+00:00',
    supersedes_decision_id: null,
    ...overrides,
  };
}

function syntheticProposal(overrides = {}) {
  return {
    proposal_id: 'p-1',
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
    stop_conditions: [{ kind: 'budget_hard_gate', limits: {} }, { kind: 'no_gain', max_consecutive_no_gain: 2 }],
    execution_ready: false,
    blocked_reasons: ['worker_unavailable:health_unknown:health_not_checked'],
    dispatcher_version: 'p3a-2026-07-20.3',
    fingerprint: 'fp-abc',
    created_at: '2026-07-20T01:00:00+00:00',
    expires_at: '2026-07-20T02:00:00+00:00',
    proposal_status: 'blocked',
    supersedes_proposal_id: null,
    extra: { verifier_level: 'independent_model', goal: '共享目标' },
    last_event: { to_status: 'blocked', reason: 'scanned', created_at: '2026-07-20T01:00:00+00:00' },
    events: [{ from_status: null, to_status: 'blocked', reason: 'scanned', created_at: '2026-07-20T01:00:00+00:00' }],
    ...overrides,
  };
}

function recordingTransport(routes, calls) {
  return async (request) => {
    calls.push({ url: request.url, method: request.method, headers: request.headers, body: request.body });
    const url = new URL(request.url);
    const key = url.pathname + url.search;
    const handler = routes[key] || routes[url.pathname];
    if (!handler) return { status: 404, json: null };
    return handler(request);
  };
}

// ---------------------------------------------------------------------------
// 标签与视图模型
// ---------------------------------------------------------------------------

describe('P3C review label tables', () => {
  it('maps decisions to Chinese labels, unknown to 未知状态', () => {
    assert.equal(model.REVIEW_DECISION_LABELS.accepted, '已接受');
    assert.equal(model.REVIEW_DECISION_LABELS.deferred, '已暂缓');
    assert.equal(model.REVIEW_DECISION_LABELS.rejected, '已驳回');
    assert.equal(model.reviewDecisionLabel('accepted'), '已接受');
    assert.equal(model.reviewDecisionLabel('rejected'), '已驳回');
    assert.equal(model.reviewDecisionLabel('approved'), '未知状态');
    assert.equal(model.reviewDecisionLabel(''), '—');
    assert.equal(model.reviewReasonText('proposal_changed'), '建议已经变化，请重新查看');
    assert.equal(model.reviewReasonText('conflict'), '已有其他审核决定，请刷新后重试');
    assert.equal(model.reviewReasonText('some_new_code'), 'some_new_code');
  });

  it('receipt and actions view models carry every frozen field', () => {
    const receipt = model.reviewReceiptView(syntheticReceipt({ decision: 'deferred', resume_condition: '等健康核验', supersedes_decision_id: 'd-0' }));
    assert.equal(receipt.decisionLabel, '已暂缓');
    assert.equal(receipt.resumeCondition, '等健康核验');
    assert.equal(receipt.supersedesDecisionId, 'd-0');
    assert.equal(receipt.fingerprint, 'fp-abc');
    const actions = model.reviewActionsView(syntheticActions({ current_decision: syntheticReceipt() }));
    assert.equal(actions.submittable, true);
    assert.equal(actions.expectedFingerprint, 'fp-abc');
    assert.equal(actions.expectedSourceSequence, 7);
    assert.equal(actions.expectedProposalStatus, 'blocked');
    assert.equal(actions.currentDecision.decisionLabel, '已接受');
  });

  it('comparison shows changed fields only, deterministically', () => {
    const previous = syntheticProposal({ proposal_id: 'p-0' });
    const current = syntheticProposal({
      worker_model: 'kimi-k3-code',
      risk_level: 'high',
      required_tools: ['web_fetch', 'archive_lookup'],
      budget: { limits: { tokens: 80000, tool_calls: 20, iterations: 8, seconds: 1800 }, used: { tokens: 0, tool_calls: 0 } },
    });
    const rows = model.proposalComparison(current, previous);
    const labels = rows.map((row) => row.label);
    assert.deepEqual(labels.sort(), ['执行模型版本', '所需工具', '预算', '风险等级'].sort());
    assert.ok(!labels.includes('核验器'));
    assert.ok(!labels.includes('阻塞原因'));
    const modelRow = rows.find((row) => row.label === '执行模型版本');
    assert.equal(modelRow.before, 'kimi-k2.7-code');
    assert.equal(modelRow.after, 'kimi-k3-code');
    // identical inputs → no rows
    assert.equal(model.proposalComparison(previous, syntheticProposal({ proposal_id: 'p-0' })).length, 0);
    assert.equal(model.proposalComparison(current, null).length, 0);
  });
});

// ---------------------------------------------------------------------------
// 客户端协议守卫
// ---------------------------------------------------------------------------

describe('P3C review client protocol guards', () => {
  it('GET for reads, POST only on /decisions with trusted Origin and intent header', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/review/v1/health': async () => ({ status: 200, json: reviewEnvelope({ provider_healthy: true }) }),
      '/review/v1/actions/p-1': async () => ({ status: 200, json: syntheticActions() }),
      '/review/v1/decisions?proposal_id=p-1&limit=20': async () => ({ status: 200, json: reviewEnvelope({ items: [syntheticReceipt()] }) }),
      '/review/v1/decisions': async () => ({ status: 202, json: reviewEnvelope(syntheticReceipt()) }),
    }, calls);
    const client = reviewApi.createShadowReviewClient({ transport });
    await client.health();
    await client.actionsFor('p-1');
    await client.decisionsFor('p-1');
    await client.submitDecision({
      proposalId: 'p-1',
      decision: 'accepted',
      expectedFingerprint: 'fp-abc',
      expectedSourceSequence: 7,
      expectedProposalStatus: 'blocked',
      expectedCurrentDecisionId: null,
    });
    assert.equal(calls.length, 4);
    for (const call of calls.slice(0, 3)) assert.equal(call.method, 'GET');
    const post = calls[3];
    assert.equal(post.method, 'POST');
    assert.equal(post.url, 'http://127.0.0.1:5682/review/v1/decisions');
    assert.equal(post.headers.Origin, 'app://obsidian.md');
    assert.equal(post.headers['X-Loop-Review-Intent'], '1');
    const sent = JSON.parse(post.body);
    assert.equal(sent.proposal_id, 'p-1');
    assert.equal(sent.expected_fingerprint, 'fp-abc');
    assert.equal(sent.expected_source_sequence, 7);
    assert.ok(sent.idempotency_key);
  });

  it('ignores a caller-supplied baseUrl (frozen loopback trust boundary)', async () => {
    const calls = [];
    const transport = recordingTransport({
      '/review/v1/health': async () => ({ status: 200, json: reviewEnvelope({ provider_healthy: true }) }),
    }, calls);
    const client = reviewApi.createShadowReviewClient({ transport, baseUrl: 'https://attacker.invalid/review/v1' });
    await client.health();
    assert.equal(calls[0].url, 'http://127.0.0.1:5682/review/v1/health');
  });

  it('validates decision rules before any transport call', async () => {
    const calls = [];
    const client = reviewApi.createShadowReviewClient({ transport: recordingTransport({}, calls) });
    await assert.rejects(
      client.submitDecision({ proposalId: 'p-1', decision: 'rejected', expectedFingerprint: 'f', expectedSourceSequence: 1, expectedProposalStatus: 'blocked' }),
      (error) => error.kind === 'invalid_arguments',
    );
    await assert.rejects(
      client.submitDecision({ proposalId: 'p-1', decision: 'deferred', expectedFingerprint: 'f', expectedSourceSequence: 1, expectedProposalStatus: 'blocked' }),
      (error) => error.kind === 'invalid_arguments',
    );
    assert.equal(calls.length, 0);
  });

  it('canonical idempotency key is deterministic and input-sensitive', async () => {
    const input = { proposalId: 'p-1', decision: 'accepted', expectedFingerprint: 'f', expectedSourceSequence: 1, expectedProposalStatus: 'blocked', expectedCurrentDecisionId: null };
    const first = await reviewApi.canonicalIdempotencyKey('nigo', input);
    const second = await reviewApi.canonicalIdempotencyKey('nigo', input);
    assert.equal(first, second);
    const changed = await reviewApi.canonicalIdempotencyKey('nigo', { ...input, note: 'x' });
    assert.notEqual(first, changed);
  });

  it('error responses with a wrong/missing/malformed envelope are contract_mismatch, never business errors', async () => {
    const validError = { code: 'proposal_changed', message: 'x' };
    const ts = '2026-07-21T00:00:00+08:00';
    const cases = [
      ['wrong version on 409', { status: 409, json: { contract_version: '999', generated_at: ts, service_version: 's', error: validError } }],
      ['missing version on 409', { status: 409, json: { generated_at: ts, service_version: 's', error: validError } }],
      ['malformed envelope on 409', { status: 409, json: null, text: 'not json at all' }],
      ['wrong version on 500', { status: 500, json: { contract_version: '2', generated_at: ts, service_version: 's', error: validError } }],
      ['correct version but missing generated_at', { status: 409, json: { contract_version: '1', service_version: 's', error: validError } }],
      ['correct version but invalid generated_at', { status: 409, json: { contract_version: '1', generated_at: 'x', service_version: 's', error: validError } }],
      ['correct version but missing service_version', { status: 409, json: { contract_version: '1', generated_at: ts, error: validError } }],
      ['correct version but missing error object', { status: 409, json: { contract_version: '1', generated_at: ts, service_version: 's', unexpected: true } }],
      ['error.code not a string', { status: 409, json: { contract_version: '1', generated_at: ts, service_version: 's', error: { code: 7, message: 'x' } } }],
      ['error.message not a string', { status: 409, json: { contract_version: '1', generated_at: ts, service_version: 's', error: { code: 'x', message: 7 } } }],
      ['error not an object', { status: 409, json: { contract_version: '1', generated_at: ts, service_version: 's', error: 'boom' } }],
    ];
    for (const [name, route] of cases) {
      const transport = recordingTransport({ '/review/v1/decisions': async () => route }, []);
      const client = reviewApi.createShadowReviewClient({ transport });
      await assert.rejects(
        client.submitDecision({
          proposalId: 'p-1',
          decision: 'accepted',
          expectedFingerprint: 'fp',
          expectedSourceSequence: 7,
          expectedProposalStatus: 'blocked',
          expectedCurrentDecisionId: null,
        }),
        (error) => {
          assert.ok(
            error.kind === 'contract_mismatch' || error.kind === 'invalid_response',
            `${name}: expected contract_mismatch or invalid_response, got ${error.kind}`,
          );
          assert.notEqual(error.kind, 'http_error', `${name}: business fields must never be used`);
          return true;
        },
      );
    }
    // Malformed envelopes on 2xx are rejected by the same validator too.
    const badSuccess = [
      ['2xx missing generated_at', { status: 200, json: { contract_version: '1', service_version: 's', data: {} } }],
      ['2xx missing service_version', { status: 200, json: { contract_version: '1', generated_at: ts, data: {} } }],
      ['2xx missing data and error', { status: 200, json: { contract_version: '1', generated_at: ts, service_version: 's' } }],
      ['2xx wrong version', { status: 200, json: { contract_version: '9', generated_at: ts, service_version: 's', data: {} } }],
      ['2xx error-only payload', { status: 200, json: { contract_version: '1', generated_at: ts, service_version: 's', error: validError } }],
      ['2xx data and error together', { status: 200, json: { contract_version: '1', generated_at: ts, service_version: 's', data: {}, error: validError } }],
      ['invalid calendar time', { status: 200, json: { contract_version: '1', generated_at: '2026-99-99T99:99:99junk', service_version: 's', data: {} } }],
      ['trailing junk on time', { status: 200, json: { contract_version: '1', generated_at: ts + 'junk', service_version: 's', data: {} } }],
      ['time without timezone', { status: 200, json: { contract_version: '1', generated_at: '2026-07-21T00:00:00', service_version: 's', data: {} } }],
      ['hour 24 rejected', { status: 200, json: { contract_version: '1', generated_at: '2026-07-21T24:00:00Z', service_version: 's', data: {} } }],
      ['Feb 31 rejected', { status: 200, json: { contract_version: '1', generated_at: '2026-02-31T00:00:00Z', service_version: 's', data: {} } }],
      ['Feb 30 rejected', { status: 200, json: { contract_version: '1', generated_at: '2026-02-30T00:00:00Z', service_version: 's', data: {} } }],
      ['non-leap Feb 29 rejected (2025)', { status: 200, json: { contract_version: '1', generated_at: '2025-02-29T00:00:00Z', service_version: 's', data: {} } }],
      ['non-leap Feb 29 rejected (1900)', { status: 200, json: { contract_version: '1', generated_at: '1900-02-29T00:00:00Z', service_version: 's', data: {} } }],
      ['Apr 31 rejected', { status: 200, json: { contract_version: '1', generated_at: '2026-04-31T00:00:00+08:00', service_version: 's', data: {} } }],
      ['Jun 31 rejected', { status: 200, json: { contract_version: '1', generated_at: '2026-06-31T00:00:00Z', service_version: 's', data: {} } }],
      ['Sep 31 rejected', { status: 200, json: { contract_version: '1', generated_at: '2026-09-31T00:00:00Z', service_version: 's', data: {} } }],
      ['Nov 31 rejected', { status: 200, json: { contract_version: '1', generated_at: '2026-11-31T00:00:00Z', service_version: 's', data: {} } }],
    ];
    for (const [name, route] of badSuccess) {
      const transport = recordingTransport({ '/review/v1/health': async () => route }, []);
      const client = reviewApi.createShadowReviewClient({ transport });
      await assert.rejects(
        client.health(),
        (error) => {
          assert.ok(
            error.kind === 'contract_mismatch' || error.kind === 'invalid_response',
            `${name}: expected contract_mismatch or invalid_response, got ${error.kind}`,
          );
          assert.notEqual(error.kind, 'unreachable', `${name}: must never surface as a network outage`);
          return true;
        },
      );
    }
    // Error responses carrying data (or data-only) are invalid_response, never unreachable.
    const badErrorPayloads = [
      ['409 data-only payload', { status: 409, json: { contract_version: '1', generated_at: ts, service_version: 's', data: {} } }],
      ['409 data and error together', { status: 409, json: { contract_version: '1', generated_at: ts, service_version: 's', data: {}, error: validError } }],
      ['409 invalid time', { status: 409, json: { contract_version: '1', generated_at: '2026-99-99T99:99:99junk', service_version: 's', error: validError } }],
    ];
    for (const [name, route] of badErrorPayloads) {
      const transport = recordingTransport({ '/review/v1/decisions': async () => route }, []);
      const client = reviewApi.createShadowReviewClient({ transport });
      await assert.rejects(
        client.submitDecision({
          proposalId: 'p-1',
          decision: 'accepted',
          expectedFingerprint: 'fp',
          expectedSourceSequence: 7,
          expectedProposalStatus: 'blocked',
          expectedCurrentDecisionId: null,
        }),
        (error) => {
          assert.equal(error.kind, 'invalid_response', `${name}: got ${error.kind}`);
          return true;
        },
      );
    }
    // A correct-version error envelope still surfaces the business code.
    const transport = recordingTransport({
      '/review/v1/decisions': async () => ({
        status: 409,
        json: { contract_version: '1', generated_at: '2026-07-21T00:00:00+08:00', service_version: 's', error: { code: 'proposal_changed', message: '建议已经变化，请重新查看' } },
      }),
    }, []);
    const client = reviewApi.createShadowReviewClient({ transport });
    await assert.rejects(
      client.submitDecision({
        proposalId: 'p-1',
        decision: 'accepted',
        expectedFingerprint: 'fp',
        expectedSourceSequence: 7,
        expectedProposalStatus: 'blocked',
        expectedCurrentDecisionId: null,
      }),
      (error) => error.kind === 'http_error' && error.details.code === 'proposal_changed',
    );

    // Real calendar edges pass: leap-day in leap years and month-end days.
    const reviewApiModule = require(path.join(ROOT, 'src', 'console', 'review-client.js'));
    const validDates = [
      '2024-02-29T23:59:59Z',
      '2000-02-29T00:00:00Z',
      '2026-01-31T00:00:00Z',
      '2026-03-31T00:00:00Z',
      '2026-04-30T00:00:00Z',
      '2026-05-31T00:00:00Z',
      '2026-06-30T00:00:00Z',
      '2026-07-31T00:00:00Z',
      '2026-08-31T00:00:00Z',
      '2026-09-30T00:00:00Z',
      '2026-10-31T00:00:00Z',
      '2026-11-30T00:00:00Z',
      '2026-12-31T00:00:00Z',
      '2025-02-28T00:00:00Z',
    ];
    for (const date of validDates) {
      const okTransport = recordingTransport({
        '/review/v1/health': async () => ({
          status: 200,
          json: { contract_version: '1', generated_at: date, service_version: 's', data: { provider_healthy: true } },
        }),
      }, []);
      const okClient = reviewApiModule.createShadowReviewClient({ transport: okTransport });
      await okClient.health();
    }
  });

  it('client source: POST only /decisions, no fetch, no SQLite, no baseUrl override', async () => {
    const src = await readFile(path.join(ROOT, 'src', 'console', 'review-client.js'), 'utf8');
    const postCalls = [...src.matchAll(/requestOnce\(\s*"POST"\s*,\s*"([^"]+)"/g)].map((m) => m[1]);
    assert.deepEqual(postCalls, ['/decisions']);
    assert.ok(!/\bfetch\s*\(/.test(src));
    assert.ok(!/sqlite/i.test(src));
    assert.ok(!/options\.baseUrl/.test(src));
    assert.ok(src.includes('app://obsidian.md'));
    assert.ok(src.includes('X-Loop-Review-Intent'));
  });
});

// ---------------------------------------------------------------------------
// 渲染（fake DOM）
// ---------------------------------------------------------------------------

function baseState(reviewSubState, proposalsOverride = {}) {
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
    proposals: {
      status: 'ready',
      error: null,
      ledgerPresent: true,
      current: [],
      history: [],
      stale: false,
      selectedProposalId: 'p-1',
      detail: model.proposalDetailModel(syntheticProposal()),
      detailLoading: false,
      detailError: null,
      detailRaw: syntheticProposal(),
      comparison: null,
      ...proposalsOverride,
    },
    review: reviewSubState,
    control: { status: 'unavailable', actions: null, error: null, dialog: null, submitting: false, submitError: null, lastReceipt: null, duplicate: false, intents: [] },
  };
}

function readyReview(actionsOverrides = {}) {
  return {
    status: 'ready',
    actions: model.reviewActionsView(syntheticActions(actionsOverrides)),
    history: [],
    dialog: null,
    submitting: false,
    submitError: null,
    lastReceipt: null,
    duplicate: false,
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
  onOpenReviewDialog() {},
  onReviewDialogCancel() {},
  onReviewChoose() {},
  onReviewConfirm() {},
  onRefreshReview() {},
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

describe('P3C review rendering', () => {
  it('shows 尚未审核 and the 审核建议 action when submittable', () => {
    const root = new FakeEl('div');
    renderConsole(root, baseState(readyReview()), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('审核决定'));
    assert.ok(text.includes('当前尚未审核'));
    assert.ok(text.includes('审核建议'));
  });

  it('shows the current decision with reason and resume condition', () => {
    const current = syntheticReceipt({ decision: 'deferred', reason: '等一等', resume_condition: '等 K2.7 健康核验', decided_at: '2026-07-20T02:00:00+00:00' });
    const root = new FakeEl('div');
    renderConsole(root, baseState(readyReview({ current_decision: current })), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('当前决定：已暂缓'));
    assert.ok(text.includes('原因：等一等'));
    assert.ok(text.includes('恢复条件：等 K2.7 健康核验'));
  });

  it('blocks the action with the authoritative reason when not submittable', () => {
    const root = new FakeEl('div');
    renderConsole(root, baseState(readyReview({ submittable: false, reason_code: 'proposal_changed' })), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('不可审核：建议已经变化，请重新查看'));
    assert.ok(!findByClass(root, 'lc-review-open').length);
  });

  it('keeps the confirmed receipt visible during an outage', () => {
    const review = { status: 'unreachable', actions: null, history: [], dialog: null, submitting: false, submitError: null, lastReceipt: model.reviewReceiptView(syntheticReceipt({ decision: 'accepted', note: '可以' })), duplicate: false };
    const root = new FakeEl('div');
    renderConsole(root, baseState(review), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('影子建议审核服务不可用'));
    assert.ok(text.includes('审核回执（不可变，状态以审核服务为准）'));
    assert.ok(text.includes('决定：已接受'));
  });

  it('dialog offers the three decisions plus cancel, with the preserve warning', () => {
    const review = readyReview({ current_decision: syntheticReceipt() });
    review.dialog = { step: 'choose', decision: null, warning: '将保留此前记录' };
    const root = new FakeEl('div');
    renderConsole(root, baseState(review), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('将保留此前记录'));
    assert.ok(text.includes('接受方案'));
    assert.ok(text.includes('暂缓处理'));
    assert.ok(text.includes('驳回方案'));
    assert.ok(text.includes('取消'));
    // 背景在对话框打开时必须 inert。
    const inert = [];
    const walk = (el) => {
      if (el.attributes && el.attributes.inert === '') inert.push(el);
      for (const child of el.children || []) walk(child);
    };
    walk(root);
    assert.ok(inert.length >= 2);
  });

  it('dialog form validates required fields before confirm', () => {
    const review = readyReview();
    review.dialog = { step: 'form', decision: 'rejected', warning: null };
    const root = new FakeEl('div');
    let confirmed = null;
    const handlers = { ...noopHandlers, onReviewConfirm: (fields) => { confirmed = fields; } };
    renderConsole(root, baseState(review), handlers);
    const confirm = findByClass(root, 'lc-dialog-confirm')[0];
    confirm.click();
    assert.equal(confirmed, null, 'empty reason must not submit');
    assert.ok(texts(root).includes('驳回必须填写原因'));
    const input = findByClass(root, 'lc-review-input')[0];
    input.value = '风险不可接受';
    confirm.click();
    assert.equal(confirmed.reason, '风险不可接受');
  });

  it('comparison block lists changed fields only', () => {
    const comparison = {
      previousId: 'p-0',
      rows: model.proposalComparison(
        syntheticProposal({ worker_model: 'kimi-k3-code' }),
        syntheticProposal({ proposal_id: 'p-0' }),
      ),
    };
    const root = new FakeEl('div');
    renderConsole(root, baseState(readyReview(), { comparison }), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('与上一版比较'));
    assert.ok(text.includes('kimi-k2.7-code → kimi-k3-code'));
    assert.ok(!text.includes('与上一版没有字段差异'));
  });

  it('G3 renders a visible failed-comparison row with retry, distinct from no-difference', () => {
    const comparison = { previousId: 'p-0', rows: [], failed: true };
    const root = new FakeEl('div');
    renderConsole(root, baseState(readyReview(), { comparison }), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('对比加载失败'), 'G3：失败显式标注（区别于「没有字段差异」）');
    const retry = root.querySelector('.lc-compare-retry');
    assert.ok(retry, '失败对比提供重试按钮');
    assert.equal(retry.textContent, '重试加载对比');
    // 失败态与无差异态可区分
    const root2 = new FakeEl('div');
    renderConsole(root2, baseState(readyReview(), { comparison: { previousId: 'p-0', rows: [] } }), noopHandlers);
    assert.ok(texts(root2).includes('与上一版没有字段差异'));
    assert.ok(!texts(root2).includes('对比加载失败'), '无差异 ≠ 失败');
  });

  it('review history renders inside collapsed technical details', () => {
    const review = readyReview();
    review.history = [
      model.reviewReceiptView(syntheticReceipt({ decision: 'rejected', reason: '驳回原因', decision_id: 'd-2', supersedes_decision_id: 'd-1' })),
      model.reviewReceiptView(syntheticReceipt({ decision_id: 'd-1' })),
    ];
    const root = new FakeEl('div');
    renderConsole(root, baseState(review), noopHandlers);
    const text = texts(root);
    assert.ok(text.includes('审核历史（最近 2 条）'));
    assert.ok(text.includes('decision_id：d-2'));
    assert.ok(text.includes('idempotency_key：key-1'));
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

function proposalStubClient() {
  const envelope = (key, payload) => ({
    contract_version: '1',
    generated_at: '2026-07-20T10:00:00+08:00',
    provider_version: 'p3b-synthetic',
    db_mode: 'read_only',
    ledger_present: true,
    source_event_id: 6,
    stale: false,
    [key]: payload,
  });
  return {
    async proposals() { return envelope('items', [syntheticProposal()]); },
    async proposalDetail(id) {
      if (id === 'p-0') return envelope('proposal', syntheticProposal({ proposal_id: 'p-0', worker_model: 'kimi-k2.5-code' }));
      return envelope('proposal', syntheticProposal());
    },
  };
}

describe('P3C view flows', () => {
  it('select proposal → review loads → dialog → submit accepted → receipt + history reload', async () => {
    const calls = [];
    let submitted = null;
    const transport = recordingTransport({
      '/review/v1/actions/p-1': async () => ({ status: 200, json: syntheticActions() }),
      '/review/v1/decisions?proposal_id=p-1&limit=20': async () => ({
        status: 200,
        json: reviewEnvelope({ items: submitted ? [syntheticReceipt({ note: submitted.note })] : [] }),
      }),
      '/review/v1/decisions': async (request) => {
        submitted = JSON.parse(request.body);
        return { status: 202, json: reviewEnvelope(syntheticReceipt({ note: submitted.note, idempotency_key: submitted.idempotency_key })) };
      },
    }, calls);
    const reviewClient = reviewApi.createShadowReviewClient({ transport });
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalStubClient(), reviewClient);
    await view.refresh();
    await view.selectProposal('p-1');
    assert.equal(view.state.review.status, 'ready');
    assert.equal(view.state.review.actions.submittable, true);

    view.openReviewDialog();
    assert.equal(view.state.review.dialog.step, 'choose');
    view.chooseReviewDecision('accepted');
    assert.equal(view.state.review.dialog.step, 'form');
    await view.submitReviewDialog({ note: '可以执行' });
    assert.equal(view.state.review.lastReceipt.decisionLabel, '已接受');
    assert.equal(submitted.expected_fingerprint, 'fp-abc');
    assert.equal(submitted.expected_source_sequence, 7);
    assert.equal(submitted.expected_proposal_status, 'blocked');
    assert.equal(submitted.expected_current_decision_id, null);
    assert.equal(submitted.note, '可以执行');
    const posts = calls.filter((call) => call.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].headers.Origin, 'app://obsidian.md');
    // reload happened: history now carries the recorded decision
    assert.equal(view.state.review.history.length, 1);
    assert.equal(view.state.review.history[0].note, '可以执行');
  });

  it('duplicate submission shows the original receipt notice', async () => {
    const transport = recordingTransport({
      '/review/v1/actions/p-1': async () => ({ status: 200, json: syntheticActions() }),
      '/review/v1/decisions?proposal_id=p-1&limit=20': async () => ({ status: 200, json: reviewEnvelope({ items: [] }) }),
      '/review/v1/decisions': async () => ({ status: 200, json: reviewEnvelope(syntheticReceipt()) }),
    }, []);
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalStubClient(), reviewApi.createShadowReviewClient({ transport }));
    await view.refresh();
    await view.selectProposal('p-1');
    view.openReviewDialog();
    view.chooseReviewDecision('accepted');
    await view.submitReviewDialog({});
    assert.equal(view.state.review.duplicate, true);
    assert.ok(texts(view.contentEl).includes('重复提交：审核服务返回了原始回执，没有产生新的决定'));
  });

  it('refresh with the same selected proposal retains the confirmed receipt during an outage', async () => {
    let reviewDown = false;
    const transport = recordingTransport({
      '/review/v1/actions/p-1': async () => ({ status: 200, json: syntheticActions() }),
      '/review/v1/decisions?proposal_id=p-1&limit=20': async () => ({ status: 200, json: reviewEnvelope({ items: [] }) }),
      '/review/v1/decisions': async () => ({ status: 202, json: reviewEnvelope(syntheticReceipt()) }),
    }, []);
    const downClient = reviewApi.createShadowReviewClient({
      transport: async (request) => {
        if (reviewDown) throw new Error('connection refused');
        return transport(request);
      },
    });
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalStubClient(), downClient);
    await view.refresh();
    await view.selectProposal('p-1');
    view.openReviewDialog();
    view.chooseReviewDecision('accepted');
    await view.submitReviewDialog({});
    assert.ok(view.state.review.lastReceipt);

    reviewDown = true;
    await view.refresh(); // triggers selectProposal for the same proposal
    assert.equal(view.state.review.status, 'unreachable');
    assert.ok(view.state.review.lastReceipt, 'receipt must survive the outage');
    assert.ok(texts(view.contentEl).includes('审核回执（不可变，状态以审核服务为准）'));
  });

  it('proposal_changed on submit surfaces the re-read hint and no receipt', async () => {
    const transport = recordingTransport({
      '/review/v1/actions/p-1': async () => ({ status: 200, json: syntheticActions() }),
      '/review/v1/decisions?proposal_id=p-1&limit=20': async () => ({ status: 200, json: reviewEnvelope({ items: [] }) }),
      '/review/v1/decisions': async () => ({
        status: 409,
        json: { contract_version: '1', generated_at: '2026-07-21T00:00:00+08:00', service_version: 's', error: { code: 'proposal_changed', message: '建议已经变化，请重新查看' } },
      }),
    }, []);
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalStubClient(), reviewApi.createShadowReviewClient({ transport }));
    await view.refresh();
    await view.selectProposal('p-1');
    view.openReviewDialog();
    view.chooseReviewDecision('accepted');
    await view.submitReviewDialog({});
    assert.equal(view.state.review.lastReceipt, null);
    assert.equal(view.state.review.submitError.text, '建议已经变化，请重新查看');
  });

  it('comparison loads when the proposal supersedes another', async () => {
    const proposalClient = proposalStubClient();
    const originalDetail = proposalClient.proposalDetail;
    proposalClient.proposalDetail = async (id) => {
      if (id === 'p-0') return originalDetail('p-0');
      const body = await originalDetail('p-1');
      body.proposal = { ...body.proposal, supersedes_proposal_id: 'p-0' };
      return body;
    };
    const transport = recordingTransport({
      '/review/v1/actions/p-1': async () => ({ status: 200, json: syntheticActions() }),
      '/review/v1/decisions?proposal_id=p-1&limit=20': async () => ({ status: 200, json: reviewEnvelope({ items: [] }) }),
    }, []);
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalClient, reviewApi.createShadowReviewClient({ transport }));
    await view.refresh();
    await view.selectProposal('p-1');
    assert.ok(view.state.proposals.comparison);
    const labels = view.state.proposals.comparison.rows.map((row) => row.label);
    assert.ok(labels.includes('执行模型版本'));
  });

  it('G7 refresh aggregates intermediate renders into one final render', async () => {
    const view = new LoopConsoleView({}, p1StubClient(), null, proposalStubClient(), null);
    let renders = 0;
    // 保留 batchRender 语义的计数 render：refresh 期间中间渲染必须被聚合
    view.render = () => { if (view.batchRender) return; renders += 1; };
    await view.refresh();
    assert.equal(renders, 2, 'refresh 只渲染 2 次（loading 一次 + 最终一次），detail/proposals 中间态被聚合');
  });
});
