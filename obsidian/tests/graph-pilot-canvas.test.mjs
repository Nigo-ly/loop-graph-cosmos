import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const { createGraphClient, GraphApiError } = require(path.join(ROOT, 'src', 'console', 'graph-client.js'));
const canvasModel = require(path.join(ROOT, 'src', 'console', 'graph-canvas-model.js'));

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
const { GraphWorkflowView } = require(path.join(ROOT, 'src', 'console', 'graph-view.js'));
Module._load = originalLoad;

const SPEC = 'c'.repeat(64);
const AUTH_DIGEST = 'd'.repeat(64);
const RESULT_DIGEST = 'f'.repeat(64);

function node(id, overrides = {}) {
  return {
    node_id: id,
    display_label: id,
    kind: 'capability',
    status: 'succeeded',
    is_entry: false,
    is_current: false,
    is_frontier: false,
    join: null,
    human_gate: null,
    output_digest: 'e'.repeat(64),
    error_code: null,
    completed_at: '2026-08-06T00:00:30+00:00',
    ...overrides,
  };
}

function authorization(overrides = {}) {
  return {
    authorization_digest: AUTH_DIGEST,
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    max_total_calls: 2,
    cost_cap_cny: 2,
    input_digest: 'b'.repeat(64),
    agent_input_digest: 'c'.repeat(64),
    candidate_content_sha256: 'a'.repeat(64),
    expected_sequence: 26,
    issued_at: '2026-08-06T00:00:00+00:00',
    expires_at: '2099-08-07T00:00:00+00:00',
    price_snapshot_source: 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing',
    input_fields: {
      title: 'taste-skill',
      core_judgment: '设计品味可以工程化',
      user_value: '统一审美基线',
      card_titles: ['设计规则卡'],
    },
    ...overrides,
  };
}

function authorizationPreview(overrides = {}) {
  return {
    provider: 'deepseek',
    model: 'deepseek-v4-pro',
    max_total_calls: 2,
    cost_cap_cny: 2,
    write_scope: 'Graph 检查点 + Agent 账本；不写笔记、不写资产',
    input_fields: {
      title: 'taste-skill',
      core_judgment: '设计品味可以工程化',
      user_value: '统一审美基线',
      card_titles: ['设计规则卡'],
    },
    ...overrides,
  };
}

function resultProjection(overrides = {}) {
  return {
    available: true,
    result_digest: RESULT_DIGEST,
    summary: '安全摘要',
    unknowns: ['待核实点'],
    next_checks: ['建议复核来源'],
    ...overrides,
  };
}

function pilotCanvas({ gate = null, result = null } = {}) {
  return {
    canvas_version: '1',
    run_id: 'exec:graph-pilot:test',
    graph_id: 'fragment-pilot-v1',
    spec_digest: SPEC,
    sequence: 26,
    status: 'human_wait',
    current_node: gate === null && result === null ? 'pre_call_gate' : result ? 'post_call_gate' : 'pre_call_gate',
    task_label: 'taste-skill',
    nodes: [
      node('pilot_input', { display_label: '候选输入', kind: 'input', is_entry: true }),
      node('pre_call_gate', {
        display_label: '调用前人工闸门',
        kind: 'human_decision',
        status: result ? 'succeeded' : 'waiting_human',
        is_current: !result,
        is_frontier: !result,
        human_gate: result
          ? { status: 'resolved', allowed_decisions: ['abort', 'approve_call'], authorization_preview: authorizationPreview(), ...(gate ? { authorization: gate } : {}) }
          : { status: 'pending', allowed_decisions: ['abort', 'approve_call'], authorization_preview: authorizationPreview(), ...(gate ? { authorization: gate } : {}) },
        output_digest: null,
      }),
      node('post_call_gate', {
        display_label: '调用后人工闸门',
        kind: 'human_decision',
        status: result ? 'waiting_human' : 'pending',
        is_current: Boolean(result),
        is_frontier: Boolean(result),
        human_gate: { status: result ? 'pending' : 'pending', allowed_decisions: ['accept_draft', 'reject'], ...(result ? { result } : {}) },
        output_digest: null,
      }),
      node('draft_output', { display_label: '未验证草稿', kind: 'output', status: 'pending', output_digest: null, completed_at: null }),
    ],
    edges: [
      { edge_id: 'e_input_pre_gate', from: 'pilot_input', to: 'pre_call_gate', type: 'sequence', taken: true, label: null, decision_source: 'declared', taken_count: 1, max_traversals: null, on_exhausted: null, exhausted_to: null },
      { edge_id: 'e_pre_gate_approve', from: 'pre_call_gate', to: 'post_call_gate', type: 'condition', taken: Boolean(result), label: '授权并执行', decision_source: 'human_selected', taken_count: result ? 1 : 0, max_traversals: null, on_exhausted: null, exhausted_to: null },
      { edge_id: 'e_post_accept', from: 'post_call_gate', to: 'draft_output', type: 'condition', taken: false, label: '接受为草稿', decision_source: 'declared', taken_count: 0, max_traversals: null, on_exhausted: null, exhausted_to: null },
    ],
  };
}

async function validateViaClient(canvasData) {
  const client = createGraphClient({
    transport: async () => ({ status: 200, json: { contract_version: '2', data: canvasData, error: null }, text: '' }),
  });
  return client.canvas('exec:graph-pilot:test');
}

describe('pilot canvas contract extensions', () => {
  it('accepts gate authorization and result extensions', async () => {
    const canvas = await validateViaClient(pilotCanvas({ gate: authorization() }));
    const gate = canvas.nodes.find((item) => item.node_id === 'pre_call_gate');
    assert.equal(gate.human_gate.authorization.authorization_digest, AUTH_DIGEST);
  });

  it('rejects result projections carrying URLs or credentials', async () => {
    await assert.rejects(
      () => validateViaClient(pilotCanvas({ result: resultProjection({ summary: '看 https://evil.example' }) })),
      /结果摘要无效/
    );
    await assert.rejects(
      () => validateViaClient(pilotCanvas({ result: resultProjection({ unknowns: ['api_key=abc'] }) })),
      /结果列表无效/
    );
  });

  it('rejects over-limit input fields', async () => {
    const bad = authorization({ input_fields: { title: '长'.repeat(101), core_judgment: 'x', user_value: 'y', card_titles: [] } });
    await assert.rejects(() => validateViaClient(pilotCanvas({ gate: bad })), /授权输入字段无效/);
  });

  it('rejects forged authorization shapes', async () => {
    const bad = authorization({ max_total_calls: 99 });
    await assert.rejects(() => validateViaClient(pilotCanvas({ gate: bad })), /授权单投影无效/);
  });

  it('rejects forged pre-receipt policy or write scope', async () => {
    const bad = pilotCanvas();
    bad.nodes.find((item) => item.node_id === 'pre_call_gate').human_gate.authorization_preview =
      authorizationPreview({ max_total_calls: 3 });
    await assert.rejects(() => validateViaClient(bad), /签发前授权材料无效/);
  });

  it('model exposes authorization and result projections', () => {
    const model = canvasModel.buildCanvasModel(pilotCanvas({ gate: authorization(), result: resultProjection() }));
    const pre = model.nodeMap.get('pre_call_gate');
    assert.equal(pre.authorization.authorizationDigest, AUTH_DIGEST);
    assert.equal(pre.authorization.inputFields.title, 'taste-skill');
    assert.equal(pre.authorizationPreview.model, 'deepseek-v4-pro');
    const post = model.nodeMap.get('post_call_gate');
    assert.equal(post.result.resultDigest, RESULT_DIGEST);
    assert.deepEqual(post.result.unknowns, ['待核实点']);
  });

  it('missing extensions degrade to null models', () => {
    const model = canvasModel.buildCanvasModel(pilotCanvas());
    assert.equal(model.nodeMap.get('pre_call_gate').authorization, null);
    assert.equal(model.nodeMap.get('pre_call_gate').authorizationPreview.inputFields.title, 'taste-skill');
    assert.equal(model.nodeMap.get('post_call_gate').result, null);
  });
});

// -- 视图层：首闸授权区 / 次闸结果区 / digest 绑定 ---------------------------

function pilotDetail({ postGate = false } = {}) {
  return {
    run: {
      run_id: 'exec:graph-pilot:test',
      graph_id: 'fragment-pilot-v1',
      spec_version: '1.0.0',
      spec_digest: SPEC,
      fragment_ref: 'fixture:fragment:pilot',
      status: 'human_wait',
      current_node: postGate ? 'post_call_gate' : 'pre_call_gate',
      step_count: 4,
      sequence: 26,
      pending_human: [postGate ? 'post_call_gate' : 'pre_call_gate'],
      blocked_reason: null,
      started_at: '2026-08-06T00:00:00+00:00',
      updated_at: '2026-08-06T00:01:00+00:00',
    },
    nodes: [],
    edges_taken: [],
    feedback_counts: {},
    human_gates: postGate
      ? {
          post_call_gate: {
            status: 'pending', decision: null, decision_id: null,
            input_digest: 'b'.repeat(64), spec_digest: SPEC, expected_sequence: 26,
            requester: 'nigo', decided_at: null,
            allowed_decisions: ['accept_draft', 'reject'],
          },
        }
      : {
          pre_call_gate: {
            status: 'pending', decision: null, decision_id: null,
            input_digest: 'b'.repeat(64), spec_digest: SPEC, expected_sequence: 26,
            requester: 'nigo', decided_at: null,
            allowed_decisions: ['approve_call', 'abort'],
          },
        },
    ready: [],
  };
}

function pilotPath(postGate = false) {
  return {
    run_id: 'exec:graph-pilot:test',
    sequence: 26,
    entry_node: 'pilot_input',
    status: 'human_wait',
    path: [],
    frontier: [postGate ? 'post_call_gate' : 'pre_call_gate'],
    failed_nodes: [],
    blocked_reason: null,
    feedback_counts: {},
  };
}

function pilotClient({ canvasData, onSubmit = null, onIssue = null } = {}) {
  const calls = { submit: 0, issue: 0 };
  return {
    calls,
    listRuns: async () => [],
    getRun: async () => pilotDetail({ postGate: Boolean(canvasData && canvasData.nodes.find((n) => n.node_id === 'post_call_gate').human_gate.result) }),
    path: async () => pilotPath(Boolean(canvasData && canvasData.nodes.find((n) => n.node_id === 'post_call_gate').human_gate.result)),
    canvas: async () => {
      const real = createGraphClient({
        transport: async () => ({ status: 200, json: { contract_version: '2', data: canvasData, error: null }, text: '' }),
      });
      return real.canvas('exec:graph-pilot:test');
    },
    submitHumanDecision: async (body) => { calls.submit += 1; if (onSubmit) onSubmit(body); return {}; },
    issueAuthorizationReceipt: async (runId) => {
      calls.issue += 1;
      if (onIssue) onIssue(runId);
      return { run_id: runId, authorization_digest: AUTH_DIGEST, issued_at: 'x', expires_at: 'y', status: 'issued', model_calls: 0 };
    },
  };
}

async function openPilotRun(client, canvasData) {
  const doc = new FakeEl('document');
  const view = new GraphWorkflowView(null, client);
  doc.appendChild(view.contentEl);
  await view.openRun('exec:graph-pilot:test');
  view.selectCanvasNode(canvasData.nodes.find((n) => n.human_gate && n.human_gate.status === 'pending').node_id);
  return { doc, view };
}

describe('pilot canvas sidebar participation', () => {
  it('pre gate without a receipt offers 生成授权单 and hides 授权并执行', async () => {
    const canvasData = pilotCanvas();
    const client = pilotClient({ canvasData });
    const { view } = await openPilotRun(client, canvasData);
    const sidebar = view.sidebarEl;
    assert.ok(sidebar.text.includes('尚未生成授权单'));
    assert.ok(sidebar.text.includes('模型：deepseek-v4-pro'));
    assert.ok(sidebar.text.includes('调用上限：最多 2 次'));
    assert.ok(sidebar.text.includes('总成本上限：¥2'));
    assert.ok(sidebar.text.includes('产出：Graph 内未验证研究草稿'));
    assert.ok(sidebar.text.includes('写入范围：Graph 检查点 + Agent 账本；不写笔记、不写资产'));
    assert.ok(sidebar.text.includes('标题：taste-skill'));
    assert.ok(sidebar.text.includes('核心判断：设计品味可以工程化'));
    assert.ok(sidebar.text.includes('用户价值：统一审美基线'));
    assert.ok(sidebar.text.includes('原始碎片正文永不发送'));
    const issue = sidebar.querySelector('.graph-pilot-issue');
    assert.ok(issue);
    assert.equal(issue.text, '生成授权单');
    const decisions = sidebar.querySelectorAll('.graph-decision').map((el) => el.text);
    assert.ok(!decisions.includes('授权并执行'), '未签发时不得渲染授权并执行');
    assert.ok(decisions.includes('中止本次运行'), 'abort 必须与调用后拒绝结果区分');
    assert.ok(!decisions.includes('拒绝'), '首闸不得使用含混的「拒绝」');
  });

  it('生成授权单 click issues a receipt through the client and refreshes', async () => {
    const canvasData = pilotCanvas();
    const issued = [];
    const client = pilotClient({ canvasData, onIssue: (runId) => issued.push(runId) });
    const { view } = await openPilotRun(client, canvasData);
    view.sidebarEl.querySelector('.graph-pilot-issue').click();
    await view.pendingOperation;
    assert.deepEqual(issued, ['exec:graph-pilot:test']);
    assert.equal(client.calls.issue, 1);
  });

  it('pre gate with a valid receipt shows 授权并执行 and binds its digest', async () => {
    const canvasData = pilotCanvas({ gate: authorization() });
    const submissions = [];
    const client = pilotClient({ canvasData, onSubmit: (body) => submissions.push(body) });
    const { view } = await openPilotRun(client, canvasData);
    const sidebar = view.sidebarEl;
    assert.ok(sidebar.text.includes('调用上限：最多 2 次'));
    assert.ok(sidebar.text.includes('总成本上限：¥2'));
    assert.ok(sidebar.text.includes('标题：taste-skill'));
    assert.ok(sidebar.text.includes('原始碎片正文永不发送'));
    const buttons = sidebar.querySelectorAll('.graph-decision');
    const approve = buttons.find((el) => el.text === '授权并执行');
    assert.ok(approve);
    approve.click();
    await view.pendingOperation;
    assert.equal(submissions.length, 1);
    assert.equal(submissions[0].authorizationDigest, AUTH_DIGEST);
    assert.equal(submissions[0].resultDigest, undefined);
  });

  it('expired receipt hides 授权并执行 and offers 重新签发授权', async () => {
    const canvasData = pilotCanvas({ gate: authorization({ expires_at: '2020-01-01T00:00:00+00:00' }) });
    const client = pilotClient({ canvasData });
    const { view } = await openPilotRun(client, canvasData);
    const sidebar = view.sidebarEl;
    assert.ok(sidebar.text.includes('授权单已过期'));
    const issue = sidebar.querySelector('.graph-pilot-issue');
    assert.equal(issue.text, '重新签发授权');
    const decisions = sidebar.querySelectorAll('.graph-decision').map((el) => el.text);
    assert.ok(!decisions.includes('授权并执行'));
  });

  it('post gate shows the safe structured result and binds result_digest', async () => {
    const canvasData = pilotCanvas({ result: resultProjection() });
    const submissions = [];
    const client = pilotClient({ canvasData, onSubmit: (body) => submissions.push(body) });
    const { view } = await openPilotRun(client, canvasData);
    const sidebar = view.sidebarEl;
    assert.ok(sidebar.text.includes('摘要：安全摘要'));
    assert.ok(sidebar.text.includes('待核实点'));
    assert.ok(sidebar.text.includes('接受仅收进 Graph 草稿区，不会写入知识资产'));
    const accept = sidebar.querySelectorAll('.graph-decision').find((el) => el.text === '接受为草稿');
    assert.ok(accept);
    accept.click();
    await view.pendingOperation;
    assert.equal(submissions.length, 1);
    assert.equal(submissions[0].resultDigest, RESULT_DIGEST);
    assert.equal(submissions[0].authorizationDigest, undefined);
  });

  it('post gate with unavailable result keeps honest fallback text', async () => {
    const canvasData = pilotCanvas({ result: resultProjection({ available: false, summary: undefined, unknowns: undefined, next_checks: undefined }) });
    const client = pilotClient({ canvasData });
    const { view } = await openPilotRun(client, canvasData);
    assert.ok(view.sidebarEl.text.includes('结果摘要不可用'));
  });
});

function researchCanvas({ receipt = null, result = null, expired = false } = {}) {
  const preview = {
    provider: 'deepseek', model: 'deepseek-v4-pro', max_total_calls: 1,
    cost_cap_cny: 2, research_goal: '核验示例产品是否可以本地部署',
    evidence_count: 1, sources: [{ title: '官方模型说明', url: 'https://example.com/model' }],
    write_scope: 'Graph 检查点 + Agent 账本；不写知识资产',
  };
  const authorization = receipt ? {
    authorization_digest: AUTH_DIGEST, provider: 'deepseek', model: 'deepseek-v4-pro',
    max_total_calls: 1, input_digest: 'b'.repeat(64), expected_sequence: 26,
    issued_at: '2026-08-10T00:00:00+00:00',
    expires_at: expired ? '2020-08-11T00:00:00+00:00' : '2099-08-11T00:00:00+00:00',
    price_snapshot_source: 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing',
  } : null;
  const safeResult = result ? {
    available: true, result_digest: RESULT_DIGEST, summary: '存在可行路径。',
    confirmed: [{ claim: '权重已公开', evidence_ids: ['ev-001'] }],
    unknowns: ['本机性能仍需实测'], conflicts: [], recommendation: '先进行小规模验证。',
    claims: [{ claim: '权重已公开', evidence_id: 'ev-001', relation: 'supports' }],
  } : null;
  return {
    ...pilotCanvas(), run_id: 'exec:graph-research:test',
    graph_id: 'fragment-research-escalation-v1', task_label: preview.research_goal,
    current_node: result ? 'result_human_review' : 'synthesis_authorization_gate',
    nodes: [
      node('research_input', { kind: 'input', is_entry: true }),
      node('synthesis_authorization_gate', {
        kind: 'human_decision', status: result ? 'succeeded' : 'waiting_human',
        is_current: !result, is_frontier: !result, output_digest: null,
        human_gate: {
          status: result ? 'resolved' : 'pending',
          allowed_decisions: ['approve_synthesis', 'abort'],
          research_authorization_preview: preview,
          ...(authorization ? { research_authorization: authorization } : {}),
        },
      }),
      node('result_human_review', {
        kind: 'human_decision', status: result ? 'waiting_human' : 'pending',
        is_current: Boolean(result), is_frontier: Boolean(result), output_digest: null,
        human_gate: {
          status: 'pending', allowed_decisions: ['accept_result', 'reject'],
          ...(safeResult ? { research_result: safeResult } : {}),
        },
      }),
    ],
    edges: [],
  };
}

function researchDetail(postGate = false) {
  const detail = pilotDetail({ postGate: false });
  const nodeId = postGate ? 'result_human_review' : 'synthesis_authorization_gate';
  return {
    ...detail,
    run: { ...detail.run, run_id: 'exec:graph-research:test', graph_id: 'fragment-research-escalation-v1', current_node: nodeId, pending_human: [nodeId] },
    human_gates: {
      [nodeId]: {
        status: 'pending', decision: null, decision_id: null,
        input_digest: 'b'.repeat(64), spec_digest: SPEC, expected_sequence: 26,
        requester: 'nigo', decided_at: null,
        allowed_decisions: postGate ? ['accept_result', 'reject'] : ['approve_synthesis', 'abort'],
      },
    },
  };
}

function researchClient(canvasData, submissions = [], issues = []) {
  const postGate = canvasData.current_node === 'result_human_review';
  return {
    listRuns: async () => [], getRun: async () => researchDetail(postGate),
    path: async () => ({ ...pilotPath(postGate), run_id: 'exec:graph-research:test', frontier: [canvasData.current_node] }),
    canvas: async () => {
      const client = createGraphClient({ transport: async () => ({ status: 200, json: { contract_version: '2', data: canvasData, error: null }, text: '' }) });
      return client.canvas('exec:graph-research:test');
    },
    submitHumanDecision: async (body) => { submissions.push(body); return {}; },
    issueResearchAuthorizationReceipt: async (runId) => { issues.push(runId); return {}; },
  };
}

async function openResearchRun(canvasData, submissions = [], issues = []) {
  const view = new GraphWorkflowView(null, researchClient(canvasData, submissions, issues));
  new FakeEl('document').appendChild(view.contentEl);
  await view.openRun('exec:graph-research:test');
  view.selectCanvasNode(canvasData.current_node);
  return view;
}

describe('research canvas governed participation', () => {
  it('uses the dedicated research receipt resource and frozen header', async () => {
    const calls = [];
    const client = createGraphClient({
      transport: async (options) => {
        calls.push(options);
        return { status: 201, json: { contract_version: '2', data: {
          run_id: 'exec:graph-research:test', authorization_digest: AUTH_DIGEST,
          issued_at: '2026-08-10T00:00:00+00:00', expires_at: '2026-08-11T00:00:00+00:00',
          status: 'issued', model_calls: 0,
        }, error: null }, text: '' };
      },
    });
    await client.issueResearchAuthorizationReceipt('exec:graph-research:test');
    assert.ok(calls[0].url.endsWith('/runs/exec%3Agraph-research%3Atest/research-authorization-receipt'));
    assert.equal(calls[0].headers['X-Graph-Research-Authorization-Receipt'], '1');
  });

  it('shows safe material, issues receipt first, and hides approval before receipt', async () => {
    const issues = [];
    const view = await openResearchRun(researchCanvas(), [], issues);
    assert.ok(view.sidebarEl.text.includes('研究目标：核验示例产品是否可以本地部署'));
    assert.ok(view.sidebarEl.text.includes('调用上限：1 次'));
    assert.ok(!view.sidebarEl.querySelectorAll('.graph-decision').some((el) => el.text === '授权并生成研究结论'));
    view.sidebarEl.querySelector('.graph-pilot-issue').click();
    await view.pendingOperation;
    assert.deepEqual(issues, ['exec:graph-research:test']);
  });

  it('binds authorization digest and result digest on the two research gates', async () => {
    const submissions = [];
    let view = await openResearchRun(researchCanvas({ receipt: true }), submissions);
    view.sidebarEl.querySelectorAll('.graph-decision').find((el) => el.text === '授权并生成研究结论').click();
    await view.pendingOperation;
    assert.equal(submissions[0].authorizationDigest, AUTH_DIGEST);
    view = await openResearchRun(researchCanvas({ result: true }), submissions);
    assert.ok(view.sidebarEl.text.includes('结论摘要：存在可行路径。'));
    view.sidebarEl.querySelectorAll('.graph-decision').find((el) => el.text === '接受研究结果').click();
    await view.pendingOperation;
    assert.equal(submissions[1].resultDigest, RESULT_DIGEST);
  });

  it('hides approval and offers renewal when the research receipt expired', async () => {
    const issues = [];
    const view = await openResearchRun(researchCanvas({ receipt: true, expired: true }), [], issues);
    assert.ok(view.sidebarEl.text.includes('研究授权单已过期，需要重新签发。'));
    assert.ok(!view.sidebarEl.querySelectorAll('.graph-decision').some((el) => el.text === '授权并生成研究结论'));
    const renew = view.sidebarEl.querySelectorAll('.graph-pilot-issue')
      .find((el) => el.text === '重新签发研究授权');
    assert.ok(renew);
    renew.click();
    await view.pendingOperation;
    assert.deepEqual(issues, ['exec:graph-research:test']);
  });
});

// -- rev3 §3：安全重试入口 ----------------------------------------------------

function failedPilotCanvas({ withAuth = true } = {}) {
  const base = pilotCanvas({ gate: withAuth ? authorization() : null });
  return {
    ...base,
    status: 'failed',
    current_node: 'agent_drafter',
    nodes: [
      ...base.nodes.filter((n) => !['pre_call_gate', 'post_call_gate'].includes(n.node_id)),
      node('pre_call_gate', {
        display_label: '调用前人工闸门',
        kind: 'human_decision',
        status: 'succeeded',
        human_gate: { status: 'resolved', allowed_decisions: ['abort', 'approve_call'], ...(withAuth ? { authorization: authorization() } : {}) },
        output_digest: null,
      }),
      node('agent_drafter', {
        display_label: 'Agent 草稿生成',
        kind: 'capability',
        status: 'failed',
        is_current: true,
        error_code: 'transport_error',
        output_digest: null,
        completed_at: null,
      }),
      node('post_call_gate', {
        display_label: '调用后人工闸门',
        kind: 'human_decision',
        status: 'pending',
        human_gate: { status: 'pending', allowed_decisions: ['accept_draft', 'reject'] },
        output_digest: null,
      }),
    ],
  };
}

function failedPilotDetail(requestSent) {
  const detail = pilotDetail({ postGate: false });
  return {
    ...detail,
    run: { ...detail.run, status: 'failed', current_node: 'agent_drafter', pending_human: [] },
    nodes: [
      {
        node_id: 'agent_drafter',
        status: 'failed',
        attempts: 1,
        input_refs: [],
        output_refs: [],
        memory_refs: [],
        agent: {
          status: 'failed',
          provider: 'deepseek',
          model: 'deepseek-v4-pro',
          max_calls: 1,
          reserved_tokens: 3000,
          actual_tokens: null,
          error_category: 'transport_error',
          request_sent: requestSent,
        },
      },
    ],
    human_gates: {},
  };
}

function retryClient({ canvasData, requestSent, onRetry = null }) {
  const calls = { retry: 0 };
  return {
    calls,
    listRuns: async () => [],
    getRun: async () => failedPilotDetail(requestSent),
    path: async () => pilotPath(false),
    canvas: async () => {
      const real = createGraphClient({
        transport: async () => ({ status: 200, json: { contract_version: '2', data: canvasData, error: null }, text: '' }),
      });
      return real.canvas('exec:graph-pilot:test');
    },
    submitHumanDecision: async () => ({}),
    retryPilotRun: async (body) => {
      calls.retry += 1;
      if (onRetry) onRetry(body);
      return { run_id: body.run_id, node_id: body.node_id, status: 'retried', run_status: 'human_wait', current_node: 'post_call_gate' };
    },
  };
}

describe('pilot safe retry entry (rev3)', () => {
  it('shows 安全重试 only when every condition is visible and binds correctly', async () => {
    const canvasData = failedPilotCanvas();
    const submissions = [];
    const client = retryClient({ canvasData, requestSent: 'false', onRetry: (b) => submissions.push(b) });
    const doc = new FakeEl('document');
    const view = new GraphWorkflowView(null, client);
    doc.appendChild(view.contentEl);
    await view.openRun('exec:graph-pilot:test');
    view.selectCanvasNode('agent_drafter');
    const button = view.sidebarEl.querySelector('.graph-pilot-retry');
    assert.ok(button, '条件可见时必须展示安全重试入口');
    assert.equal(button.text, '安全重试');
    button.click();
    await view.pendingOperation;
    assert.equal(submissions.length, 1);
    assert.equal(submissions[0].runId, 'exec:graph-pilot:test');
    assert.equal(submissions[0].nodeId, 'agent_drafter');
    assert.equal(submissions[0].specDigest, SPEC);
    assert.equal(submissions[0].inputDigest, 'c'.repeat(64));
    assert.equal(submissions[0].authorizationDigest, AUTH_DIGEST);
    assert.equal(submissions[0].expectedSequence, 26);
  });

  it('hides the entry when the send fact is true or unknown', async () => {
    for (const sent of ['true', 'unknown']) {
      const canvasData = failedPilotCanvas();
      const client = retryClient({ canvasData, requestSent: sent });
      const doc = new FakeEl('document');
      const view = new GraphWorkflowView(null, client);
      doc.appendChild(view.contentEl);
      await view.openRun('exec:graph-pilot:test');
      view.selectCanvasNode('agent_drafter');
      assert.equal(view.sidebarEl.querySelector('.graph-pilot-retry'), null, `${sent} 绝不展示重试入口`);
    }
  });

  it('hides the entry when the receipt is gone or run is not pilot', async () => {
    const canvasData = failedPilotCanvas({ withAuth: false });
    const client = retryClient({ canvasData, requestSent: 'false' });
    const doc = new FakeEl('document');
    const view = new GraphWorkflowView(null, client);
    doc.appendChild(view.contentEl);
    await view.openRun('exec:graph-pilot:test');
    view.selectCanvasNode('agent_drafter');
    assert.equal(view.sidebarEl.querySelector('.graph-pilot-retry'), null);
  });

  it('double-click produces a single retry call', async () => {
    const canvasData = failedPilotCanvas();
    let calls = 0;
    let release;
    const client = retryClient({ canvasData, requestSent: 'false' });
    client.retryPilotRun = async (body) => {
      calls += 1;
      await new Promise((resolve) => { release = resolve; });
      return { run_id: body.run_id, node_id: body.node_id, status: 'retried', run_status: 'human_wait', current_node: 'post_call_gate' };
    };
    const doc = new FakeEl('document');
    const view = new GraphWorkflowView(null, client);
    doc.appendChild(view.contentEl);
    await view.openRun('exec:graph-pilot:test');
    view.selectCanvasNode('agent_drafter');
    const button = view.sidebarEl.querySelector('.graph-pilot-retry');
    button.click();
    button.click();
    release();
    await view.pendingOperation;
    assert.equal(calls, 1);
  });

  it('retryPilotRun posts the bound body with retry_id via the real client', async () => {
    const seen = [];
    const BASE = 'http://127.0.0.1:5684/graph/v1';
    const client = createGraphClient({
      transport: async (request) => {
        seen.push(request);
        return {
          status: 202,
          json: {
            contract_version: '2',
            data: { run_id: 'exec:graph-pilot:abc', node_id: 'agent_drafter', status: 'retried', run_status: 'human_wait', current_node: 'post_call_gate' },
            error: null,
          },
        };
      },
    });
    await client.retryPilotRun({
      runId: 'exec:graph-pilot:abc',
      nodeId: 'agent_drafter',
      specDigest: SPEC,
      inputDigest: 'c'.repeat(64),
      authorizationDigest: AUTH_DIGEST,
      expectedSequence: 26,
    });
    assert.equal(seen.length, 1);
    assert.equal(seen[0].headers['X-Graph-Pilot-Retry'], '1');
    assert.ok(seen[0].url.startsWith(`${BASE}/runs/`));
    const body = JSON.parse(seen[0].body);
    assert.equal(body.requester, 'nigo');
    assert.equal(body.authorization_digest, AUTH_DIGEST);
    assert.match(body.retry_id, /^[0-9a-f]{64}$/);
    const { graphPilotRetryId } = require(path.join(ROOT, 'src', 'console', 'graph-client.js'));
    const expected = await graphPilotRetryId({
      requester: 'nigo',
      run_id: 'exec:graph-pilot:abc',
      node_id: 'agent_drafter',
      spec_digest: SPEC,
      input_digest: 'c'.repeat(64),
      authorization_digest: AUTH_DIGEST,
      expected_sequence: 26,
    });
    assert.equal(body.retry_id, expected);
  });

  it('retryPilotRun rejects malformed bindings before sending', async () => {
    const seen = [];
    const client = createGraphClient({ transport: async (r) => { seen.push(r); return { status: 202, json: {} }; } });
    await assert.rejects(
      () => client.retryPilotRun({
        runId: 'exec:graph-pilot:abc',
        nodeId: 'agent_drafter',
        specDigest: SPEC,
        inputDigest: 'forged',
        authorizationDigest: AUTH_DIGEST,
        expectedSequence: 26,
      }),
      /绑定摘要无效/
    );
    assert.equal(seen.length, 0);
  });
});

describe('pilot safe retry entry tightening (rev4)', () => {
  it('hides the entry when the failed node is not the current blocking node', async () => {
    const canvasData = { ...failedPilotCanvas(), current_node: 'post_call_gate' };
    const client = retryClient({ canvasData, requestSent: 'false' });
    const doc = new FakeEl('document');
    const view = new GraphWorkflowView(null, client);
    doc.appendChild(view.contentEl);
    await view.openRun('exec:graph-pilot:test');
    view.selectCanvasNode('agent_drafter');
    assert.equal(view.sidebarEl.querySelector('.graph-pilot-retry'), null);
  });

  it('hides the entry when the receipt has expired', async () => {
    const canvasData = failedPilotCanvas();
    const pre = canvasData.nodes.find((n) => n.node_id === 'pre_call_gate');
    pre.human_gate.authorization = authorization({ expires_at: '2020-01-01T00:00:00+00:00' });
    const client = retryClient({ canvasData, requestSent: 'false' });
    const doc = new FakeEl('document');
    const view = new GraphWorkflowView(null, client);
    doc.appendChild(view.contentEl);
    await view.openRun('exec:graph-pilot:test');
    view.selectCanvasNode('agent_drafter');
    assert.equal(view.sidebarEl.querySelector('.graph-pilot-retry'), null);
  });

  it('retry_failed outcome renders an honest failure instead of success', async () => {
    const canvasData = failedPilotCanvas();
    const client = retryClient({ canvasData, requestSent: 'false' });
    client.retryPilotRun = async (body) => ({
      run_id: body.run_id,
      node_id: body.node_id,
      status: 'retry_failed',
      run_status: 'failed',
      current_node: 'agent_drafter',
      error_code: 'transport_error',
    });
    const doc = new FakeEl('document');
    const view = new GraphWorkflowView(null, client);
    doc.appendChild(view.contentEl);
    await view.openRun('exec:graph-pilot:test');
    view.selectCanvasNode('agent_drafter');
    view.sidebarEl.querySelector('.graph-pilot-retry').click();
    await view.pendingOperation;
    await view.pendingOperation; // openRun 链
    assert.ok(view.contentEl.text.includes('安全重试仍未成功'), '必须诚实显示重试失败');
    assert.ok(!view.contentEl.text.includes('重试成功'));
  });
});
