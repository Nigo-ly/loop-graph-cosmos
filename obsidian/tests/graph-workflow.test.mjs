import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const {
  GRAPH_API_BASE_URL,
  GraphApiError,
  createGraphClient,
  graphDecisionId,
  graphReopenId,
} = require(path.join(ROOT, 'src', 'console', 'graph-client.js'));
const viewModel = require(path.join(ROOT, 'src', 'console', 'graph-view-model.js'));

// Stub the obsidian module so the ItemView classes can load under plain Node.
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
const register = require(path.join(ROOT, 'src', 'console', 'register.js'));
const { GraphWorkflowView, GRAPH_WORKFLOW_VIEW_TYPE } = require(
  path.join(ROOT, 'src', 'console', 'graph-view.js'),
);
Module._load = originalLoad;

function runSummary(overrides = {}) {
  return {
    run_id: 'exec:test:1',
    graph_id: 'fragment-cognitive-v1',
    spec_version: '1.0.0',
    spec_digest: 'c'.repeat(64),
    fragment_ref: 'fixture:fragment:test',
    status: 'human_wait',
    current_node: 'human_confirmation',
    step_count: 12,
    sequence: 26,
    pending_human: ['human_confirmation'],
    blocked_reason: null,
    started_at: '2026-08-04T00:00:00+00:00',
    updated_at: '2026-08-04T00:01:00+00:00',
    ...overrides,
  };
}

function nodeEntry(id, overrides = {}) {
  return {
    node_id: id,
    status: 'succeeded',
    attempts: 1,
    terminal: true,
    input_digest: 'd'.repeat(64),
    output_digest: 'e'.repeat(64),
    input_refs: ['fixture:fragment:abc'],
    output_refs: [],
    output: { note: 'ok' },
    error: null,
    completed_at: '2026-08-04T00:00:30+00:00',
    child_run_id: null,
    absorbed_failures: [],
    memory_refs: [],
    ...overrides,
  };
}

function runDetail() {
  return {
    run: runSummary(),
    nodes: [
      nodeEntry('input_fragment'),
      nodeEntry('memory_context', {
        memory_refs: ['mem:asset:result-1'],
        output_refs: ['mem:asset:result-1'],
        output: { memory_findings: [{ ref: 'mem:asset:result-1' }], note: '只读记忆' },
      }),
      nodeEntry('human_confirmation', { status: 'waiting_human', terminal: false }),
      nodeEntry('synthetic_output', { status: 'pending', terminal: false }),
    ],
    edges_taken: [
      {
        edge_id: 'e_input_expansion',
        from: 'input_fragment',
        to: 'semantic_expansion',
        type: 'sequence',
        decision_source: 'declared',
        reason: 'sequence',
      },
    ],
    feedback_counts: { e_calibration_feedback: 1 },
    human_gates: {
      human_confirmation: {
        status: 'pending',
        decision: null,
        decision_id: null,
        input_digest: 'b'.repeat(64),
        spec_digest: 'c'.repeat(64),
        expected_sequence: 26,
        requester: 'nigo',
        decided_at: null,
        allowed_decisions: ['approve', 'reject'],
      },
    },
    ready: [],
  };
}

function pathData() {
  return {
    run_id: 'exec:test:1',
    sequence: 26,
    entry_node: 'input_fragment',
    status: 'human_wait',
    path: [
      {
        edge_id: 'e_route_direct',
        from: 'route_decision',
        to: 'direct_perspective',
        type: 'condition',
        decision_source: 'rule_evaluated',
        reason: "condition_matched:routeequals'direct'",
      },
    ],
    frontier: ['human_confirmation'],
    failed_nodes: [],
    blocked_reason: null,
    feedback_counts: { e_calibration_feedback: 1 },
  };
}

function envelope(data, status = 200) {
  return { status, json: { contract_version: '2', data, error: null } };
}

describe('Graph client', () => {
  it('pins the same decision digest as the backend', async () => {
    const digest = await graphDecisionId({
      requester: 'nigo',
      decision: 'approve',
      run_id: 'exec:test:1',
      node_id: 'human_confirmation',
      spec_digest: 'a'.repeat(64),
      input_digest: 'b'.repeat(64),
      expected_sequence: 7,
    });
    assert.equal(
      digest,
      'ad1a3233a71ad7986787d302a6478701d97da7fb805dc6b5bf40eb12571c7997',
    );
  });

  it('pins the same reopen digest as the backend', async () => {
    const digest = await graphReopenId({
      requester: 'nigo',
      run_id: 'exec:test:1',
      node_id: 'memory_context',
      expected_sequence: 7,
      reason: '人工要求重开',
    });
    assert.equal(
      digest,
      '567e37c1df486dfa615272a48e13e7b8958280f7e89fe63adf4f7bce505c00fd',
    );
  });

  it('never accepts a baseUrl override and pins the frozen base', () => {
    assert.equal(GRAPH_API_BASE_URL, 'http://127.0.0.1:5684/graph/v1');
    const client = createGraphClient({ transport: async () => envelope([]) });
    assert.equal(typeof client.listRuns, 'function');
  });

  it('rejects contract mismatch and invalid payloads', async () => {
    const badEnvelope = createGraphClient({
      transport: async () => ({ status: 200, json: { contract_version: '9', data: [] } }),
    });
    await assert.rejects(() => badEnvelope.listRuns(), (error) => {
      assert.equal(error.kind, 'contract_mismatch');
      return true;
    });
    const badPayload = createGraphClient({
      transport: async () => envelope([{ run_id: 1 }]),
    });
    await assert.rejects(() => badPayload.listRuns(), (error) => {
      assert.equal(error.kind, 'invalid_response');
      return true;
    });
  });

  it('surfaces http errors with the backend code', async () => {
    const client = createGraphClient({
      transport: async () => ({
        status: 409,
        json: { contract_version: '2', data: null, error: { code: 'sequence_mismatch' } },
      }),
    });
    await assert.rejects(() => client.listRuns(), (error) => {
      assert.equal(error.kind, 'http_error');
      assert.equal(error.details.code, 'sequence_mismatch');
      return true;
    });
  });

  it('queries runs, detail, history, path and affected via frozen GETs', async () => {
    const requests = [];
    const client = createGraphClient({
      transport: async (request) => {
        requests.push(request);
        if (request.url.endsWith('/history')) return envelope([{ sequence: 1, event_type: 'run_registered' }]);
        if (request.url.endsWith('/path')) return envelope(pathData());
        if (request.url.includes('/affected')) {
          return envelope({ node_id: 'memory_context', affected_nodes: ['final_join'], affected_human_gates: [] });
        }
        if (request.url.endsWith('/runs')) return envelope([runSummary()]);
        return envelope(runDetail());
      },
    });
    assert.equal((await client.listRuns())[0].run_id, 'exec:test:1');
    assert.equal((await client.getRun('exec:test:1')).run.status, 'human_wait');
    assert.equal((await client.history('exec:test:1'))[0].event_type, 'run_registered');
    assert.equal((await client.path('exec:test:1')).entry_node, 'input_fragment');
    const affected = await client.affected('exec:test:1', 'memory_context');
    assert.deepEqual(affected.affected_nodes, ['final_join']);
    for (const request of requests) {
      assert.equal(request.method, 'GET');
      assert.ok(request.url.startsWith(GRAPH_API_BASE_URL));
      assert.equal(request.headers.Origin, 'app://obsidian.md');
    }
    assert.ok(requests[4].url.includes('affected?node_id=memory_context'));
  });

  it('rejects non-execution run ids on the client side', async () => {
    const client = createGraphClient({ transport: async () => envelope([]) });
    await assert.rejects(() => client.getRun('mem:asset:fake'), GraphApiError);
    await assert.rejects(() => client.getRun('exec:bad id'), GraphApiError);
    await assert.rejects(() => client.getRun('exec:bad/path'), GraphApiError);
    await assert.rejects(() => client.getRun('exec:bad\\path'), GraphApiError);
    await assert.rejects(() => client.getRun('exec:bad\u007fpath'), GraphApiError);
  });

  it('rejects non-positive binding sequences before transport', async () => {
    let calls = 0;
    const client = createGraphClient({
      transport: async () => {
        calls += 1;
        return envelope({});
      },
    });
    await assert.rejects(
      () =>
        client.submitHumanDecision({
          runId: 'exec:test:1',
          nodeId: 'human_confirmation',
          decision: 'approve',
          specDigest: 'c'.repeat(64),
          inputDigest: 'b'.repeat(64),
          expectedSequence: -1,
        }),
      GraphApiError,
    );
    await assert.rejects(
      () =>
        client.reopenNode({
          runId: 'exec:test:1',
          nodeId: 'memory_context',
          expectedSequence: 0,
          reason: '合成反例',
        }),
      GraphApiError,
    );
    assert.equal(calls, 0);
  });

  it('posts human decisions with exact binding and frozen header', async () => {
    const requests = [];
    const client = createGraphClient({
      transport: async (request) => {
        requests.push(request);
        return envelope({ status: 'recorded', idempotent: false }, 202);
      },
    });
    await client.submitHumanDecision({
      runId: 'exec:test:1',
      nodeId: 'human_confirmation',
      decision: 'approve',
      specDigest: 'c'.repeat(64),
      inputDigest: 'b'.repeat(64),
      expectedSequence: 26,
    });
    assert.equal(requests.length, 1);
    const request = requests[0];
    assert.equal(request.method, 'POST');
    assert.ok(request.url.endsWith('/runs/exec%3Atest%3A1/human-decisions'));
    assert.equal(request.headers['X-Graph-Human-Decision'], '1');
    const body = JSON.parse(request.body);
    assert.equal(body.requester, 'nigo');
    assert.equal(body.decision, 'approve');
    assert.equal(body.expected_sequence, 26);
    const expected = await graphDecisionId(body);
    assert.equal(body.decision_id, expected);
  });

  it('posts node reopen with exact binding and frozen header', async () => {
    const requests = [];
    const client = createGraphClient({
      transport: async (request) => {
        requests.push(request);
        return envelope({ status: 'reopened' }, 202);
      },
    });
    await client.reopenNode({
      runId: 'exec:test:1',
      nodeId: 'memory_context',
      expectedSequence: 26,
      reason: '人工要求重开',
    });
    const request = requests[0];
    assert.equal(request.method, 'POST');
    assert.ok(request.url.endsWith('/nodes/memory_context/reopen'));
    assert.equal(request.headers['X-Graph-Node-Reopen'], '1');
    const body = JSON.parse(request.body);
    assert.equal(body.reopen_id, await graphReopenId(body));
    await assert.rejects(
      () =>
        client.reopenNode({
          runId: 'exec:test:1',
          nodeId: 'memory_context',
          expectedSequence: 26,
          reason: '含\n换行',
        }),
      GraphApiError,
    );
    assert.equal(requests.length, 1);
  });
});

describe('Graph view-model', () => {
  it('builds the run list model with stable labels', () => {
    const model = viewModel.buildRunListModel([runSummary()]);
    assert.equal(model.empty, false);
    assert.equal(model.items[0].statusLabel, '等待人工');
    assert.equal(model.items[0].pendingHuman[0], 'human_confirmation');
    const empty = viewModel.buildRunListModel([], '本地 Graph 服务暂不可用');
    assert.equal(empty.empty, true);
    assert.equal(empty.error, '本地 Graph 服务暂不可用');
  });

  it('builds the run detail model with human tasks and critical path', () => {
    const model = viewModel.buildRunDetailModel(runDetail(), pathData());
    assert.equal(model.statusLabel, '等待人工');
    assert.equal(model.humanTasks.length, 1);
    assert.deepEqual(model.humanTasks[0].allowedDecisions, ['approve', 'reject']);
    assert.equal(model.humanTasks[0].gateSequence, 26);
    assert.equal(model.criticalPath[0].decisionSourceLabel, '规则判定');
    assert.equal(model.feedbackCounts.e_calibration_feedback, 1);
    const nodeIds = model.nodes.map((node) => node.nodeId);
    assert.ok(nodeIds.includes('memory_context'));
  });

  it('builds node detail with refs and read-only memory block data', () => {
    const model = viewModel.buildNodeDetailModel(runDetail(), 'memory_context');
    assert.equal(model.nodeId, 'memory_context');
    assert.deepEqual(model.memoryRefs, ['mem:asset:result-1']);
    assert.equal(model.output.note, '只读记忆');
    assert.equal(viewModel.buildNodeDetailModel(runDetail(), 'ghost'), null);
  });
});

describe('Graph renderers (fake DOM, text nodes only)', () => {
  it('renders the run list and opens a run via button', () => {
    const container = new FakeEl('div');
    const opened = [];
    viewModel.renderRunList(container, viewModel.buildRunListModel([runSummary()]), {
      onOpenRun: (runId) => opened.push(runId),
      onRefresh: () => {},
    });
    const buttons = container.querySelectorAll('.graph-run-open');
    assert.equal(buttons.length, 1);
    assert.ok(buttons[0].text.includes('等待人工'));
    assert.ok(buttons[0].text.includes('Checkpoint #26'));
    buttons[0].click();
    assert.deepEqual(opened, ['exec:test:1']);
  });

  it('labels a completed run as an ended flow rather than an accepted result', () => {
    const container = new FakeEl('div');
    viewModel.renderRunList(
      container,
      viewModel.buildRunListModel([{ ...runSummary(), status: 'completed' }]),
      { onOpenRun: () => {}, onRefresh: () => {} },
    );
    const button = container.querySelector('.graph-run-open');
    assert.ok(button.text.includes('流程已结束'));
    assert.equal(button.text.includes('已完成'), false);
  });

  it('renders empty and error states', () => {
    const container = new FakeEl('div');
    viewModel.renderRunList(
      container,
      viewModel.buildRunListModel([], '本地 Graph 服务暂不可用'),
      { onOpenRun: () => {}, onRefresh: () => {} },
    );
    assert.ok(container.querySelector('.graph-view-error'));
  });

  it('renders run detail with human task, path reasons and nodes', () => {
    const container = new FakeEl('div');
    const decisions = [];
    const nodes = [];
    viewModel.renderRunDetail(
      container,
      viewModel.buildRunDetailModel(runDetail(), pathData()),
      {
        onBack: () => {},
        onOpenNode: (nodeId) => nodes.push(nodeId),
        onDecision: (task, decision) => decisions.push([task.nodeId, decision]),
      },
    );
    assert.ok(container.text.includes('人工待办'));
    assert.ok(container.text.includes('关键路径与边的选择原因'));
    assert.ok(container.text.includes('规则判定'));
    assert.ok(container.text.includes('反馈计数'));
    const approve = container.querySelector('.graph-decision-approve');
    approve.click();
    assert.deepEqual(decisions, [['human_confirmation', 'approve']]);
    const nodeButtons = container.querySelectorAll('.graph-node-open');
    assert.equal(nodeButtons.length, 4);
    nodeButtons[1].click();
    assert.deepEqual(nodes, ['memory_context']);
  });

  it('renders node detail with refs, memory block and back navigation', () => {
    const container = new FakeEl('div');
    let backed = 0;
    const detail = runDetail();
    viewModel.renderNodeDetail(
      container,
      viewModel.buildNodeDetailModel(detail, 'memory_context'),
      { onBack: () => { backed += 1; } },
    );
    assert.ok(container.text.includes('只读关联知识'));
    assert.ok(container.text.includes('unverified'));
    assert.ok(container.text.includes('mem:asset:result-1'));
    assert.ok(container.text.includes('输入摘要'));
    container.querySelector('.graph-view-back').click();
    assert.equal(backed, 1);
  });

  it('never uses innerHTML in the graph modules', async () => {
    for (const name of ['graph-client.js', 'graph-view-model.js', 'graph-view.js']) {
      const code = await readFile(path.join(ROOT, 'src', 'console', name), 'utf8');
      assert.ok(!code.includes('innerHTML'), `${name} must not use innerHTML`);
    }
  });
});

describe('Graph view registration', () => {
  it('registers the workflow view and command without touching existing ones', async () => {
    const views = {};
    const commands = [];
    const plugin = {
      // P0-1：默认隔离通知/修复写目录（register.js 默认路径即生产消费目录）。
      getViewPreferences: () => ({
        authNotifyDir: mkdtempSync(path.join(os.tmpdir(), 'p0-auth-notify-')),
        repairRequestDir: mkdtempSync(path.join(os.tmpdir(), 'p0-repair-req-')),
      }),
      app: {
        workspace: {
          on: () => ({}),
          onLayoutReady: () => {},
        },
      },
      registerView: (type, factory) => {
        views[type] = factory;
      },
      addCommand: (command) => commands.push(command),
      register: () => {},
      registerEvent: () => {},
    };
    // MutationObserver/window are browser globals; the homepage scan half is
    // not under test here, so stub the minimum.
    globalThis.MutationObserver = class {
      observe() {}
      disconnect() {}
    };
    globalThis.window = {
      clearTimeout: () => {},
      setTimeout: () => 0,
    };
    globalThis.document = new FakeEl('document');
    globalThis.document.body = globalThis.document;
    try {
      register.setupLoopConsole(plugin);
      // Let the fire-and-forget homepage scan promise chain settle before
      // tearing down the globals it touches.
      for (let index = 0; index < 5; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
      }
    } finally {
      delete globalThis.MutationObserver;
      delete globalThis.window;
      delete globalThis.document;
    }
    assert.ok(views['my-life-graph-workflow'], 'graph workflow view registered');
    assert.ok(views['my-life-loop-console'], 'loop console view still registered');
    const commandIds = commands.map((command) => command.id);
    assert.ok(commandIds.includes('open-graph-workflow'));
    assert.ok(commandIds.includes('open-loop-console'));
  });
});

describe('GraphWorkflowView (obsidian stubbed)', () => {
  function fakeClient(calls) {
    return {
      async listRuns() {
        calls.push(['listRuns']);
        return [runSummary()];
      },
      async getRun(runId) {
        calls.push(['getRun', runId]);
        return runDetail();
      },
      async path(runId) {
        calls.push(['path', runId]);
        return pathData();
      },
      async submitHumanDecision(input) {
        calls.push(['submitHumanDecision', input]);
        return { status: 'recorded', idempotent: false };
      },
    };
  }

  it('walks list → run → node → back with keyboard-focusable buttons', async () => {
    const calls = [];
    const view = new GraphWorkflowView(null, fakeClient(calls));
    assert.equal(view.getViewType(), GRAPH_WORKFLOW_VIEW_TYPE);
    assert.equal(view.getDisplayText(), 'Graph 工作流');
    await view.showList();
    const runButton = view.contentEl.querySelector('.graph-run-open');
    assert.ok(runButton, 'run list rendered');
    assert.equal(runButton.tagName, 'BUTTON');
    runButton.click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(view.contentEl.text.includes('关键路径与边的选择原因'));
    const nodeButtons = view.contentEl.querySelectorAll('.graph-node-open');
    nodeButtons[1].click();
    assert.ok(view.contentEl.text.includes('只读关联知识'));
    view.contentEl.querySelector('.graph-view-back').click();
    assert.ok(view.contentEl.text.includes('节点与分支状态'));
    view.contentEl.querySelector('.graph-view-back').click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(view.contentEl.querySelector('.graph-run-open'));
  });

  it('submits a human decision with the gate binding and reloads', async () => {
    const calls = [];
    const view = new GraphWorkflowView(null, fakeClient(calls));
    await view.openRun('exec:test:1');
    const approve = view.contentEl.querySelector('.graph-decision-approve');
    assert.ok(approve, 'decision button rendered');
    approve.click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const submission = calls.find((call) => call[0] === 'submitHumanDecision');
    assert.ok(submission, 'decision submitted');
    assert.deepEqual(submission[1], {
      runId: 'exec:test:1',
      nodeId: 'human_confirmation',
      decision: 'approve',
      specDigest: 'c'.repeat(64),
      inputDigest: 'b'.repeat(64),
      expectedSequence: 26,
    });
  });

  it('R2 §4.6 原位入口被拒：队列卡同一授权项在途时 decide 不提交 + 可见提示', async () => {
    const calls = [];
    // sharedAuthLocks：graph:exec:test:1:human_confirmation 在途（队列卡 pending）。
    const locks = { isPending: (key) => key === 'graph:exec:test:1:human_confirmation' };
    const view = new GraphWorkflowView(null, fakeClient(calls), { sharedAuthLocks: locks });
    await view.openRun('exec:test:1');
    const approve = view.contentEl.querySelector('.graph-decision-approve');
    approve.click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    const submission = calls.find((call) => call[0] === 'submitHumanDecision');
    assert.equal(submission, undefined, '互斥在途时原位入口不提交');
    assert.ok(view.contentEl.text.includes('正在其他入口处理中'), '可见中文提示');
  });

  it('G5/G9 text-fallback view never renders a doomed approve_call; shows refresh guidance', async () => {
    // Pilot 首闸（approve_call）在文本回退视图（无 canvas 授权投影）下：
    // 不给必然失败的按钮，给可见说明 + 刷新指引。
    const detail = runDetail();
    detail.run.graph_id = 'fragment-pilot-v1';
    detail.human_gates.human_confirmation.allowed_decisions = ['approve_call', 'reject'];
    const calls = [];
    const view = new GraphWorkflowView(null, {
      async listRuns() { calls.push(['listRuns']); return [runSummary({ graph_id: 'fragment-pilot-v1' })]; },
      async getRun(runId) { calls.push(['getRun', runId]); return detail; },
      async path(runId) { calls.push(['path', runId]); return pathData(); },
      // 无 canvas → fetchCanvasSafe null → 文本回退视图
    });
    await view.openRun('exec:test:1');
    // 文本视图渲染 humanTasks
    const taskCard = view.contentEl.querySelector('.graph-human-task');
    assert.ok(taskCard, '文本视图渲染人工待办');
    assert.ok(view.contentEl.text.includes('需要授权上下文'), 'G9：提示授权上下文缺失');
    assert.equal(view.contentEl.querySelectorAll('.graph-decision-approve_call').length, 0, '不渲染必然失败的 approve_call 按钮');
    assert.ok(view.contentEl.querySelector('.graph-decision-reject'), '非授权决定仍可用（reject）');
    assert.ok(view.contentEl.querySelector('.graph-view-refresh'), '提供刷新指引');
  });

  it('G6 shows a visible explanation and back button when node detail is missing', async () => {
    const calls = [];
    const view = new GraphWorkflowView(null, fakeClient(calls));
    await view.openRun('exec:test:1');
    // 打开不存在的节点 → buildNodeDetailModel 返回 null
    view.openNode('missing_node');
    assert.ok(view.contentEl.text.includes('节点详情不可用'), 'G6：可见说明');
    assert.ok(view.contentEl.text.includes('missing_node'), '说明携带缺失节点');
    const back = view.contentEl.querySelector('.graph-view-back');
    assert.ok(back, '提供返回按钮');
    back.click();
    assert.ok(view.contentEl.querySelector('.graph-decision-approve'), '返回后恢复运行详情视图');
  });

  it('shows an honest error when the service is unreachable', async () => {
    const view = new GraphWorkflowView(null, {
      async listRuns() {
        throw new GraphApiError('unreachable', '无法连接本地 Graph 服务');
      },
    });
    await view.showList();
    assert.ok(view.contentEl.text.includes('无法连接本地 Graph 服务'));
  });

  it('G4 filters the run list to waiting-human runs and toggles back', async () => {
    let runs = [
      runSummary({ run_id: 'exec:wait', status: 'human_wait', pending_human: ['g1'] }),
      runSummary({ run_id: 'exec:run', status: 'running', pending_human: [] }),
    ];
    const view = new GraphWorkflowView(null, {
      async listRuns() { return runs; },
    });
    await view.showList();
    assert.equal(view.contentEl.querySelectorAll('.graph-run-open').length, 2, '初始显示全部 run');
    const toggle = view.contentEl.querySelector('.graph-view-waiting-filter');
    assert.ok(toggle, '「只看等待人工」过滤按钮存在');
    assert.equal(toggle.textContent, '只看等待人工');
    toggle.click();
    await new Promise((resolve) => setImmediate(resolve));
    const rows = view.contentEl.querySelectorAll('.graph-run-open');
    assert.equal(rows.length, 1, '过滤后只剩等待人工 run');
    assert.ok(view.contentEl.text.includes('等待人工'), '保留的是等待 run');
    assert.equal(view.contentEl.querySelector('.graph-view-waiting-filter').textContent, '显示全部', '过滤态按钮可切回');
    // 切回显示全部
    view.contentEl.querySelector('.graph-view-waiting-filter').click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(view.contentEl.querySelectorAll('.graph-run-open').length, 2, '切回后全部 run 恢复');
  });
});


describe('Graph view model rev7 research v3 governance states', () => {
  function v3Detail(gates) {
    return {
      run: runSummary({ graph_id: 'fragment-research-macro-v3', status: 'human_wait' }),
      nodes: [
        nodeEntry('judgment'),
        nodeEntry('human_review', { status: 'waiting_human', terminal: false }),
      ],
      edges_taken: [],
      feedback_counts: {},
      human_gates: gates,
      ready: [],
    };
  }

  it('distinguishes model authorization vs candidate review on macro v3 runs', () => {
    const authzModel = viewModel.buildRunDetailModel(v3Detail({
      synthesis_authorization_gate: {
        status: 'pending', decision: null, decision_id: null,
        input_digest: 'b'.repeat(64), spec_digest: 'c'.repeat(64),
        expected_sequence: 9, requester: 'nigo', decided_at: null,
        allowed_decisions: ['approve_synthesis'],
      },
    }), null);
    assert.equal(authzModel.isResearchRun, true, 'v3 run 必须识别为 Research run');
    assert.equal(authzModel.humanTasks.length, 1);
    assert.equal(authzModel.humanTasks[0].taskKind, 'model_authorization');
    assert.equal(authzModel.humanTasks[0].isResearchPreGate, true);

    const reviewModel = viewModel.buildRunDetailModel(v3Detail({
      human_review: {
        status: 'pending', decision: null, decision_id: null,
        input_digest: 'b'.repeat(64), spec_digest: 'c'.repeat(64),
        expected_sequence: 17, requester: 'nigo', decided_at: null,
        allowed_decisions: ['accept_result', 'reject'],
      },
    }), null);
    assert.equal(reviewModel.isResearchRun, true);
    assert.equal(reviewModel.humanTasks[0].taskKind, 'candidate_review');
    assert.equal(reviewModel.humanTasks[0].isResearchPreGate, false);
  });

  it('projects resolved accept/reject decisions on the node detail gate', () => {
    const accepted = viewModel.buildNodeDetailModel(v3Detail({
      human_review: {
        status: 'resolved', decision: 'accept_result', decision_id: 'd'.repeat(64),
        input_digest: 'b'.repeat(64), spec_digest: 'c'.repeat(64),
        expected_sequence: 17, requester: 'nigo', decided_at: '2026-08-09T00:00:00+00:00',
        allowed_decisions: ['accept_result', 'reject'],
      },
    }), 'human_review');
    assert.equal(accepted.gate.decision, 'accept_result');
    const rejected = viewModel.buildNodeDetailModel(v3Detail({
      human_review: {
        status: 'resolved', decision: 'reject', decision_id: 'e'.repeat(64),
        input_digest: 'b'.repeat(64), spec_digest: 'c'.repeat(64),
        expected_sequence: 17, requester: 'nigo', decided_at: '2026-08-09T00:00:00+00:00',
        allowed_decisions: ['accept_result', 'reject'],
      },
    }), 'human_review');
    assert.equal(rejected.gate.decision, 'reject');
  });
});
