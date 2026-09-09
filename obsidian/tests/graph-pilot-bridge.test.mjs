import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const bridge = require(path.join(ROOT, 'src', 'console', 'graph-pilot-bridge.js'));
const { createGraphClient, GraphApiError } = require(path.join(ROOT, 'src', 'console', 'graph-client.js'));

const SPEC_DIGEST = 'a'.repeat(64);
const CONTENT_SHA = 'b'.repeat(64);

function candidate(overrides = {}) {
  return {
    candidate_id: 'cand-1',
    fragment_ref: '散记/碎片想法/taste-skill.md',
    title: 'taste-skill：给 AI 注入设计品味',
    content_status: 'pending_confirmation',
    evidence_level: 'unverified',
    has_conflict: false,
    content_sha256: CONTENT_SHA,
    ...overrides,
  };
}

function pilotRun(overrides = {}) {
  return {
    run_id: 'exec:graph-pilot:abc',
    graph_id: 'fragment-pilot-v1',
    fragment_ref: '散记/碎片想法/taste-skill.md',
    status: 'human_wait',
    ...overrides,
  };
}

function homeWithCard(
  href = 'Notes/散记/碎片想法/taste-skill.md',
  organizedHref = '',
  attribute = 'data-href'
) {
  const doc = new FakeEl('document');
  doc.body = doc.createDiv({ cls: 'body' });
  const home = doc.createDiv({ cls: 'my-life-homepage-view' });
  const content = home.createDiv({ cls: 'life-dashboard-content' });
  const card = content.createDiv({ cls: 'life-capture-card' });
  card.createEl('p', { cls: 'life-capture-status', text: '已提取并整理' });
  const footer = card.createEl('footer');
  const anchor = footer.createEl('a', { text: '原始记录' });
  anchor.setAttribute(attribute, href);
  if (organizedHref) {
    const organized = footer.createEl('a', { text: '查看整理结果' });
    organized.setAttribute(attribute, organizedHref);
  }
  return { doc, card };
}

function continuation(overrides = {}) {
  return {
    fragment_id: 'taste-skill',
    status: 'approved',
    current_node: 'cognitive_contract',
    sequence: 376,
    stage: 'loop_registered',
    updated_at: '2026-08-08T19:45:00+08:00',
    ...overrides,
  };
}

function plan() {
  return {
    plan_version: '1',
    spec_id: 'fragment-pilot-v1',
    spec_digest: SPEC_DIGEST,
    task_label: '真实碎片 Pilot',
    node_count: 9,
    node_flow: '输入→调用前闸门→准备→草稿生成→验证→调用后闸门→产出/拒绝/中止',
    expected_output: 'Graph 内未验证草稿（不写入知识资产）',
    human_gates: 2,
    max_feedback: 1,
    max_total_calls: 2,
    cost_cap_cny: 2,
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    write_scope: 'Graph 检查点 + Agent 账本；不写笔记、不写资产',
    create_behavior: '创建后推进到第一道人工闸门并停下；不会调用模型。',
  };
}

describe('pilot bridge fragment matching', () => {
  it('normalizes basename with NFC and strips directory and .md', () => {
    assert.equal(bridge.normalizeFragmentBasename('散记/碎片想法/x.md'), 'x');
    assert.equal(bridge.normalizeFragmentBasename('x.md'), 'x');
    assert.equal(bridge.normalizeFragmentBasename('a/b/c'), 'c');
    assert.equal(bridge.normalizeFragmentBasename(''), '');
    assert.equal(bridge.normalizeFragmentBasename(null), '');
  });

  it('matches exactly one candidate by basename', () => {
    const result = bridge.matchCandidate([candidate()], 'taste-skill');
    assert.equal(result.status, 'matched');
    assert.equal(result.candidate.candidate_id, 'cand-1');
  });

  it('reports conflict for multiple candidates with the same basename', () => {
    const result = bridge.matchCandidate(
      [candidate(), candidate({ candidate_id: 'cand-2' })],
      'taste-skill'
    );
    assert.equal(result.status, 'conflict');
  });

  it('reports none when no candidate matches', () => {
    assert.equal(bridge.matchCandidate([candidate()], 'other').status, 'none');
  });

  it('finds only pilot runs for the fragment', () => {
    const runs = [
      { graph_id: 'fragment-cognitive-v1', fragment_ref: '散记/碎片想法/taste-skill.md', run_id: 'exec:x' },
      pilotRun(),
    ];
    assert.equal(bridge.pilotRunForFragment(runs, 'taste-skill').run_id, 'exec:graph-pilot:abc');
    assert.equal(bridge.pilotRunForFragment(runs, 'other'), null);
  });

  it('enforces the three eligibility fields with honest reasons', () => {
    assert.equal(bridge.candidateEligibility(candidate()).ok, true);
    assert.equal(bridge.candidateEligibility(candidate({ has_conflict: true })).ok, false);
    assert.equal(bridge.candidateEligibility(candidate({ content_status: 'kept_draft' })).ok, false);
    assert.equal(bridge.candidateEligibility(candidate({ evidence_level: 'verified' })).ok, false);
  });
});

describe('pilot bridge homepage injection', () => {
  it('renders the convert button for an eligible candidate and calls back', () => {
    const { doc, card } = homeWithCard();
    const calls = [];
    const injected = bridge.injectHomepagePilotBridge(doc, { candidates: [candidate()], runs: [] }, {
      onStartConvert: (item) => calls.push(item.candidate_id),
      onOpenRun: () => {},
    });
    assert.equal(injected, 1);
    const button = card.querySelector('.graph-pilot-convert');
    assert.ok(button);
    assert.equal(button.text, '转为 Graph 工作流');
    button.click();
    assert.deepEqual(calls, ['cand-1']);
  });

  it('shows muted reason for ineligible candidates and no button', () => {
    const { doc, card } = homeWithCard();
    bridge.injectHomepagePilotBridge(doc, { candidates: [candidate({ has_conflict: true })], runs: [] }, {
      onStartConvert: () => assert.fail('must not offer convert'),
      onOpenRun: () => {},
    });
    assert.equal(card.querySelector('.graph-pilot-convert'), null);
    assert.ok(card.querySelector('.graph-pilot-note-muted').text.includes('候选尚未就绪'));
  });

  it('shows binding conflict note without a button', () => {
    const { doc, card } = homeWithCard();
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [candidate(), candidate({ candidate_id: 'cand-2' })],
      runs: [],
    }, { onStartConvert: () => assert.fail('must not guess'), onOpenRun: () => {} });
    assert.equal(card.querySelector('.graph-pilot-convert'), null);
    assert.ok(card.querySelector('.graph-pilot-note-muted').text.includes('来源绑定冲突'));
  });

  it('adds nothing when the candidate is not ready yet', () => {
    const { doc, card } = homeWithCard();
    const injected = bridge.injectHomepagePilotBridge(doc, { candidates: [], runs: [] }, {
      onStartConvert: () => {}, onOpenRun: () => {},
    });
    assert.equal(injected, 0);
    assert.equal(card.querySelector('.graph-pilot-entry'), null);
  });

  it('shows the real Loop checkpoint and continuation action after n8n organized an approved fragment', () => {
    const { doc, card } = homeWithCard(
      'Notes/散记/碎片想法/taste-skill.md',
      'Notes/AI创业/碎片整理/taste-skill.md'
    );
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [], runs: [], continuations: [continuation()],
    }, {
      isLoopApproved: () => true,
      onContinueResearch: () => {},
      onStartConvert: () => {},
      onOpenRun: () => {},
    });
    assert.equal(
      card.querySelector('.life-capture-status').text,
      'Loop 已登记 · Checkpoint #376 · 等待接续'
    );
    assert.equal(card.querySelector('.graph-continuation-start').text, '接续到 Loop（研究）');
  });

  it('binds the production My Life data-path links without guessing from href', () => {
    const { doc, card } = homeWithCard(
      'Notes/散记/碎片想法/taste-skill',
      'Notes/AI创业/碎片整理/taste-skill',
      'data-path'
    );
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [candidate()], runs: [pilotRun()], continuations: [continuation()],
    }, {
      isLoopApproved: () => true,
      onContinueResearch: () => {}, onStartConvert: () => {}, onOpenRun: () => {},
    });
    assert.equal(card.querySelector('.life-capture-status').text, '已进入 Graph · 等待你决定');
    assert.ok(card.querySelector('.graph-pilot-open'));
  });

  it('does not offer continuation when the fragment was organized without Loop approval', () => {
    const { doc, card } = homeWithCard(
      'Notes/散记/碎片想法/taste-skill.md',
      'Notes/AI创业/碎片整理/taste-skill.md'
    );
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [], runs: [], continuations: [continuation()],
    }, {
      isLoopApproved: () => false,
      onContinueResearch: () => assert.fail('unsigned fragment must not continue'),
      onStartConvert: () => {},
      onOpenRun: () => {},
    });
    assert.equal(card.querySelector('.graph-continuation-start'), null);
    assert.ok(card.querySelector('.life-capture-status').text.includes('未签名'));
  });

  it('double-clicking continuation produces one request and disables during flight', async () => {
    const { doc, card } = homeWithCard(
      'Notes/散记/碎片想法/taste-skill.md',
      'Notes/AI创业/碎片整理/taste-skill.md'
    );
    let calls = 0;
    let release;
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [], runs: [], continuations: [continuation()],
    }, {
      isLoopApproved: () => true,
      onContinueResearch: () => new Promise((resolve) => {
        calls += 1;
        release = resolve;
      }),
      onStartConvert: () => {},
      onOpenRun: () => {},
    });
    const button = card.querySelector('.graph-continuation-start');
    button.click();
    button.click();
    assert.equal(calls, 1);
    assert.equal(button.getAttribute('disabled'), 'disabled');
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(button.getAttribute('disabled'), null);
  });

  it('candidate and Graph states replace the intermediate continuation action', () => {
    const { doc, card } = homeWithCard(
      'Notes/散记/碎片想法/taste-skill.md',
      'Notes/AI创业/碎片整理/taste-skill.md'
    );
    const handlers = {
      isLoopApproved: () => true,
      onContinueResearch: () => {},
      onStartConvert: () => {},
      onOpenRun: () => {},
    };
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [candidate()], runs: [], continuations: [continuation({ stage: 'candidate_source_ready' })],
    }, handlers);
    assert.equal(card.querySelector('.life-capture-status').text, 'Loop 候选已就绪');
    assert.ok(card.querySelector('.graph-pilot-convert'));
    assert.equal(card.querySelector('.graph-continuation-start'), null);
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [candidate()], runs: [pilotRun()], continuations: [continuation({ stage: 'candidate_source_ready' })],
    }, handlers);
    assert.equal(card.querySelector('.life-capture-status').text, '已进入 Graph · 等待你决定');
    assert.ok(card.querySelector('.graph-pilot-open'));
  });

  it('keeps the legacy candidate converter hidden after the shared alignment layer is active', () => {
    const { doc, card } = homeWithCard();
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [candidate()], runs: [], alignmentAvailable: true,
      alignments: [
        { fragment_id: 'taste-skill', alignment_id: 'align:old', route: 'verify', updated_at: '2026-08-09T01:00:00Z' },
        { fragment_id: 'taste-skill', alignment_id: 'align:new', route: 'graph', updated_at: '2026-08-09T02:00:00Z' },
      ],
    }, {
      isLoopApproved: () => true,
      onStartConvert: () => {}, onOpenRun: () => {},
    });
    assert.equal(card.querySelector('.graph-pilot-convert'), null);
  });

  it('shows the bridged entry that opens the Graph run', () => {
    const { doc, card } = homeWithCard();
    const opened = [];
    bridge.injectHomepagePilotBridge(doc, { candidates: [candidate()], runs: [pilotRun()] }, {
      onStartConvert: () => assert.fail('already bridged'),
      onOpenRun: (runId) => opened.push(runId),
    });
    const open = card.querySelector('.graph-pilot-open');
    assert.ok(open.text.includes('已进入 Graph 工作流'));
    open.click();
    assert.deepEqual(opened, ['exec:graph-pilot:abc']);
  });

  it('shows an honest error note when the service is unreachable', () => {
    const { doc, card } = homeWithCard();
    bridge.injectHomepagePilotBridge(doc, { candidates: [candidate()], runs: [], error: 'Graph 服务暂时不可用' }, {
      onStartConvert: () => {}, onOpenRun: () => {},
    });
    assert.ok(card.querySelector('.graph-pilot-note-muted').text.includes('Graph 工作流暂不可用'));
    assert.equal(card.querySelector('.graph-pilot-convert'), null);
  });

  it('re-injection with the same fingerprint is idempotent', () => {
    const { doc, card } = homeWithCard();
    const state = { candidates: [candidate()], runs: [] };
    const handlers = { onStartConvert: () => {}, onOpenRun: () => {} };
    bridge.injectHomepagePilotBridge(doc, state, handlers);
    const first = card.querySelector('.graph-pilot-entry');
    bridge.injectHomepagePilotBridge(doc, state, handlers);
    const entries = card.querySelectorAll('.graph-pilot-entry');
    assert.equal(entries.length, 1);
    assert.equal(entries[0], first);
  });

  it('re-injects when the fingerprint changes (candidate updated)', () => {
    const { doc, card } = homeWithCard();
    const handlers = { onStartConvert: () => {}, onOpenRun: () => {} };
    bridge.injectHomepagePilotBridge(doc, { candidates: [candidate()], runs: [] }, handlers);
    bridge.injectHomepagePilotBridge(doc, {
      candidates: [candidate({ content_sha256: 'c'.repeat(64) })],
      runs: [],
    }, handlers);
    const entries = card.querySelectorAll('.graph-pilot-entry');
    assert.equal(entries.length, 1);
  });

  it('skips cards without a source link', () => {
    const doc = new FakeEl('document');
    const home = doc.createDiv({ cls: 'my-life-homepage-view' });
    const content = home.createDiv({ cls: 'life-dashboard-content' });
    content.createDiv({ cls: 'life-capture-card' });
    const injected = bridge.injectHomepagePilotBridge(doc, { candidates: [candidate()], runs: [] }, {
      onStartConvert: () => assert.fail('no basename'),
      onOpenRun: () => {},
    });
    assert.equal(injected, 0);
  });
});

describe('pilot confirm dialog', () => {
  it('renders the frozen plan facts and cancel closes without writes', () => {
    const { doc } = homeWithCard();
    bridge.openPilotConfirmDialog(doc, plan(), candidate(), {
      onConfirm: () => assert.fail('cancel must not create'),
    });
    const dialog = doc.querySelector('.graph-pilot-dialog');
    assert.ok(dialog.text.includes('真实碎片 Pilot'));
    assert.ok(dialog.text.includes('总上限 2 次'));
    assert.ok(dialog.text.includes('不会调用模型'));
    dialog.querySelector('.graph-pilot-cancel').click();
    assert.equal(doc.querySelector('.graph-pilot-dialog'), null);
  });

  it('double-clicking confirm produces a single create call', async () => {
    const { doc } = homeWithCard();
    let calls = 0;
    let release;
    bridge.openPilotConfirmDialog(doc, plan(), candidate(), {
      onConfirm: () => new Promise((resolve) => {
        calls += 1;
        release = resolve;
      }),
    });
    const confirm = doc.querySelector('.graph-pilot-confirm');
    confirm.click();
    confirm.click();
    assert.equal(calls, 1);
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
});

function fakeTransport(routes, seen = []) {
  return async (request) => {
    seen.push(request);
    const route = routes[`${request.method} ${request.url}`];
    if (!route) return { status: 404, json: { contract_version: '2', data: null, error: { code: 'not_found' } } };
    return { status: route.status, json: { contract_version: '2', data: route.data, error: null } };
  };
}

describe('pilot client contracts', () => {
  const BASE = 'http://127.0.0.1:5684/graph/v1';

  it('getPilotPlan validates the plan contract', async () => {
    const client = createGraphClient({
      transport: fakeTransport({ [`GET ${BASE}/pilot-plans/fragment-pilot-v1`]: { status: 200, data: plan() } }),
    });
    const result = await client.getPilotPlan();
    assert.equal(result.spec_digest, SPEC_DIGEST);
  });

  it('getPilotPlan rejects contract drift', async () => {
    const broken = { ...plan(), max_total_calls: 'two' };
    const client = createGraphClient({
      transport: fakeTransport({ [`GET ${BASE}/pilot-plans/fragment-pilot-v1`]: { status: 200, data: broken } }),
    });
    await assert.rejects(() => client.getPilotPlan(), /契约不匹配/);
  });

  it('createPilotRun sends the six-field body with the frozen header', async () => {
    const seen = [];
    const client = createGraphClient({
      transport: fakeTransport({
        [`POST ${BASE}/runs`]: {
          status: 201,
          data: { run_id: 'exec:graph-pilot:abc', status: 'human_wait', current_node: 'pre_call_gate', sequence: 3, model_calls: 0 },
        },
      }, seen),
    });
    const outcome = await client.createPilotRun({
      specDigest: SPEC_DIGEST,
      fragmentRef: '散记/碎片想法/taste-skill.md',
      candidateId: 'cand-1',
      candidateContentSha256: CONTENT_SHA,
    });
    assert.equal(outcome.run_id, 'exec:graph-pilot:abc');
    const sent = seen[0];
    assert.equal(sent.headers['X-Graph-Run-Create'], '1');
    const body = JSON.parse(sent.body);
    assert.deepEqual(Object.keys(body).sort(), [
      'candidate_content_sha256', 'candidate_id', 'fragment_ref', 'requester', 'spec_digest', 'spec_id',
    ]);
  });

  it('createPilotRun surfaces stable error codes', async () => {
    const client = createGraphClient({
      transport: fakeTransport({
        [`POST ${BASE}/runs`]: { status: 409, data: null, },
      }),
    });
    // 409 无 route 数据时走 http_error；错误码来自 error.code。
    const failing = createGraphClient({
      transport: async () => ({
        status: 409,
        json: { contract_version: '2', data: null, error: { code: 'already_bridged' } },
      }),
    });
    await assert.rejects(() => client.createPilotRun({}), GraphApiError);
    try {
      await failing.createPilotRun({
        specDigest: SPEC_DIGEST,
        fragmentRef: 'x.md',
        candidateId: 'cand-1',
        candidateContentSha256: CONTENT_SHA,
      });
      assert.fail('must throw');
    } catch (error) {
      assert.equal(error.details.code, 'already_bridged');
    }
  });

  it('issueAuthorizationReceipt posts the bound body and validates the outcome', async () => {
    const seen = [];
    const client = createGraphClient({
      transport: fakeTransport({
        [`POST ${BASE}/runs/exec%3Agraph-pilot%3Aabc/authorization-receipt`]: {
          status: 201,
          data: {
            run_id: 'exec:graph-pilot:abc',
            authorization_digest: 'd'.repeat(64),
            issued_at: '2026-08-06T00:00:00+00:00',
            expires_at: '2026-08-07T00:00:00+00:00',
            status: 'issued',
            model_calls: 0,
          },
        },
      }, seen),
    });
    const outcome = await client.issueAuthorizationReceipt('exec:graph-pilot:abc');
    assert.equal(outcome.status, 'issued');
    assert.equal(seen[0].headers['X-Graph-Authorization-Receipt'], '1');
    assert.deepEqual(JSON.parse(seen[0].body), { run_id: 'exec:graph-pilot:abc', requester: 'nigo' });
  });

  it('issueAuthorizationReceipt rejects a forged model_calls payload', async () => {
    const client = createGraphClient({
      transport: async () => ({
        status: 201,
        json: {
          contract_version: '2',
          data: {
            run_id: 'exec:graph-pilot:abc',
            authorization_digest: 'd'.repeat(64),
            issued_at: '2026-08-06T00:00:00+00:00',
            expires_at: '2026-08-07T00:00:00+00:00',
            status: 'issued',
            model_calls: 3,
          },
          error: null,
        },
      }),
    });
    await assert.rejects(() => client.issueAuthorizationReceipt('exec:graph-pilot:abc'), /授权单结果无效/);
  });

  it('submitHumanDecision carries pilot binding digests when provided', async () => {
    const seen = [];
    const client = createGraphClient({
      transport: fakeTransport({
        [`POST ${BASE}/runs/exec%3Agraph-pilot%3Aabc/human-decisions`]: {
          status: 202,
          data: { run_id: 'exec:graph-pilot:abc', node_id: 'pre_call_gate', decision: 'approve_call', decision_id: 'x', status: 'recorded', idempotent: false, run_status: 'running' },
        },
      }, seen),
    });
    await client.submitHumanDecision({
      runId: 'exec:graph-pilot:abc',
      nodeId: 'pre_call_gate',
      decision: 'approve_call',
      specDigest: SPEC_DIGEST,
      inputDigest: CONTENT_SHA,
      expectedSequence: 3,
      authorizationDigest: 'd'.repeat(64),
    });
    const body = JSON.parse(seen[0].body);
    assert.equal(body.authorization_digest, 'd'.repeat(64));
    assert.equal(body.result_digest, undefined);
  });

  it('submitHumanDecision rejects malformed digests before sending', async () => {
    const seen = [];
    const client = createGraphClient({ transport: fakeTransport({}, seen) });
    await assert.rejects(
      () => client.submitHumanDecision({
        runId: 'exec:graph-pilot:abc',
        nodeId: 'pre_call_gate',
        decision: 'approve_call',
        specDigest: SPEC_DIGEST,
        inputDigest: CONTENT_SHA,
        expectedSequence: 3,
        authorizationDigest: 'forged',
      }),
      /授权单摘要无效/
    );
    assert.equal(seen.length, 0);
  });
});
