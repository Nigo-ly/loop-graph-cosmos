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
const { computeCanvasLayout } = require(path.join(ROOT, 'src', 'console', 'graph-canvas-layout.js'));

// Stub the obsidian module so the ItemView class can load under plain Node.
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

function canvasNode(id, overrides = {}) {
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
    completed_at: '2026-08-04T00:00:30+00:00',
    ...overrides,
  };
}

function canvasEdge(id, from, to, overrides = {}) {
  return {
    edge_id: id,
    from,
    to,
    type: 'sequence',
    taken: true,
    label: null,
    decision_source: 'declared',
    taken_count: 1,
    max_traversals: null,
    on_exhausted: null,
    exhausted_to: null,
    ...overrides,
  };
}

// 锚定场景：输入 → 路由 →（研究 | 直接）→ 汇合 → 验证 →(返修回边)→ 人工闸门。
function sampleCanvas(overrides = {}) {
  return {
    canvas_version: '1',
    run_id: 'exec:test:1',
    graph_id: 'fragment-cognitive-v1',
    spec_digest: 'c'.repeat(64),
    sequence: 26,
    status: 'human_wait',
    current_node: 'human_confirmation',
    task_label: '碎片认知整理',
    nodes: [
      canvasNode('input_fragment', { display_label: '碎片输入', kind: 'input', is_entry: true }),
      canvasNode('route_decision', { display_label: '路线判断', kind: 'router' }),
      canvasNode('research_branch', { display_label: '研究子图', kind: 'subgraph' }),
      canvasNode('direct_perspective', { display_label: '直接视角', kind: 'capability', status: 'skipped' }),
      canvasNode('route_join', {
        display_label: '路线汇合',
        kind: 'join',
        join: { mode: 'all_success', threshold: null, on_partial_failure: 'fail', cancel_remaining: true },
      }),
      canvasNode('calibration_v2', { display_label: '校准验证', kind: 'validator' }),
      canvasNode('human_confirmation', {
        display_label: '人工确认',
        kind: 'human_decision',
        status: 'waiting_human',
        is_current: true,
        is_frontier: true,
        human_gate: { status: 'pending', allowed_decisions: ['approve', 'reject'] },
        output_digest: null,
      }),
    ],
    edges: [
      canvasEdge('e_input_router', 'input_fragment', 'route_decision'),
      canvasEdge('e_route_research', 'route_decision', 'research_branch', { type: 'condition', label: '需要研究' }),
      canvasEdge('e_route_direct', 'route_decision', 'direct_perspective', { type: 'condition', taken: false, taken_count: 0 }),
      canvasEdge('e_research_join', 'research_branch', 'route_join'),
      canvasEdge('e_direct_join', 'direct_perspective', 'route_join', { taken: false, taken_count: 0 }),
      canvasEdge('e_join_calibration', 'route_join', 'calibration_v2'),
      canvasEdge('e_calibration_feedback', 'calibration_v2', 'route_decision', {
        type: 'feedback',
        taken: false,
        taken_count: 1,
        max_traversals: 1,
        on_exhausted: 'route',
        exhausted_to: 'human_confirmation',
      }),
      canvasEdge('e_calibration_human', 'calibration_v2', 'human_confirmation'),
    ],
    ...overrides,
  };
}

function runDetailFixture() {
  return {
    run: {
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
    },
    nodes: [],
    edges_taken: [],
    feedback_counts: {},
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

function pathFixture() {
  return {
    run_id: 'exec:test:1',
    sequence: 26,
    entry_node: 'input_fragment',
    status: 'human_wait',
    path: [],
    frontier: ['human_confirmation'],
    failed_nodes: [],
    blocked_reason: null,
    feedback_counts: {},
  };
}

function clientMock({ canvasData = sampleCanvas(), onSubmit = null } = {}) {
  const calls = { getRun: 0, path: 0, canvas: 0, submit: 0 };
  return {
    calls,
    listRuns: async () => [],
    getRun: async () => { calls.getRun += 1; return runDetailFixture(); },
    path: async () => { calls.path += 1; return pathFixture(); },
    canvas: canvasData === null
      ? async () => { calls.canvas += 1; throw new GraphApiError('http_error', 'Graph 服务返回 HTTP 404', { status: 404 }); }
      : async () => {
        calls.canvas += 1;
        // 走真实客户端契约校验：畸形数据在这里抛错，视图回退文本。
        const real = createGraphClient({
          transport: async () => ({ status: 200, json: { contract_version: '2', data: canvasData, error: null }, text: '' }),
        });
        return real.canvas('exec:test:1');
      },
    submitHumanDecision: async (body) => { calls.submit += 1; if (onSubmit) onSubmit(body); return {}; },
  };
}

function makeView(client) {
  const doc = new FakeEl('document');
  const view = new GraphWorkflowView(null, client);
  doc.appendChild(view.contentEl);
  return { doc, view };
}

describe('graph canvas layout (L3)', () => {
  it('produces identical coordinates for identical contracts', () => {
    const first = computeCanvasLayout(sampleCanvas());
    const second = computeCanvasLayout(sampleCanvas());
    assert.deepEqual([...first.positions.entries()], [...second.positions.entries()]);
    assert.equal(first.width, second.width);
    assert.equal(first.height, second.height);
  });

  it('excludes feedback edges from rank computation and never loops', () => {
    // feedback 边 calibration_v2 → route_decision 形成回环；rank 必须只按
    // sequence/condition DAG 计算：route_decision 在 calibration_v2 之前。
    const layout = computeCanvasLayout(sampleCanvas());
    assert.deepEqual(layout.feedbackEdgeIds, ['e_calibration_feedback']);
    const routeRank = layout.positions.get('route_decision').x;
    const calibrationRank = layout.positions.get('calibration_v2').x;
    assert.ok(routeRank < calibrationRank, 'feedback 边不得参与 rank，否则回环会推高路由节点');
    assert.ok(layout.positions.get('human_confirmation').x > calibrationRank);
  });

  it('sorts same-rank nodes by node_id deterministically', () => {
    const layout = computeCanvasLayout(sampleCanvas());
    const research = layout.positions.get('research_branch');
    const direct = layout.positions.get('direct_perspective');
    assert.equal(research.x, direct.x, '同 rank');
    assert.ok(direct.y < research.y, '同 rank 按 node_id 字典序：direct_perspective 在 research_branch 之前');
  });
});

describe('graph canvas model (L2)', () => {
  it('builds semantic labels and falls back honestly for unknown enums', () => {
    const model = canvasModel.buildCanvasModel(sampleCanvas());
    assert.equal(model.taskLabel, '碎片认知整理');
    assert.equal(model.statusLabel, '等待人工');
    const join = model.nodeMap.get('route_join');
    assert.equal(join.kindLabel, '汇合');
    assert.equal(join.join.modeLabel, '全部成功才汇合');
    const weird = canvasModel.buildCanvasModel(sampleCanvas({
      nodes: [canvasNode('odd', { kind: 'mystery_kind', status: 'mystery_status' })],
      edges: [],
    }));
    assert.equal(weird.nodes[0].kindLabel, '未知类型');
    assert.equal(weird.nodes[0].statusLabel, 'mystery_status', '未知状态原样显示');
  });

  it('uses the fixed edge label, degrades to 条件, and never leaks reasons or values', () => {
    const model = canvasModel.buildCanvasModel(sampleCanvas());
    const research = model.edges.find((edge) => edge.edgeId === 'e_route_research');
    const direct = model.edges.find((edge) => edge.edgeId === 'e_route_direct');
    assert.equal(research.label, '需要研究');
    assert.equal(direct.label, '条件', '无固定映射的条件边只显示「条件」');
    const feedback = model.edges.find((edge) => edge.edgeId === 'e_calibration_feedback');
    assert.ok(feedback.summary.includes('返修 1/1'));
    assert.ok(feedback.summary.includes('返修耗尽后改道'));
    assert.ok(!feedback.summary.includes('condition_matched'), '条件表达式原文不得出现');
  });

  it('marks the waiting human gate as the only attention node', () => {
    const model = canvasModel.buildCanvasModel(sampleCanvas());
    assert.equal(model.waitingNode.nodeId, 'human_confirmation');
    assert.deepEqual(model.waitingNode.allowedDecisions, ['approve', 'reject']);
    assert.ok(model.needsAttention);
  });

  it('shows the result digest fingerprint when no safe summary exists', () => {
    const model = canvasModel.buildCanvasModel(sampleCanvas());
    const gate = model.nodeMap.get('human_confirmation');
    assert.equal(gate.resultSummary, null);
    assert.equal(gate.outputDigest, null, 'output_digest 为 null 时不得伪造摘要');
    const input = model.nodeMap.get('input_fragment');
    assert.equal(input.resultSummary, null);
    assert.equal(input.outputDigestShort, 'eeeeeeeeeeee…', '无安全摘要时只显示指纹');
  });
});

describe('graph client canvas contract', () => {
  function transportReturning(payload) {
    return async () => ({ status: 200, json: { contract_version: '2', data: payload, error: null }, text: '' });
  }

  it('accepts a valid canvas payload', async () => {
    const client = createGraphClient({ transport: transportReturning(sampleCanvas()) });
    const canvas = await client.canvas('exec:test:1');
    assert.equal(canvas.task_label, '碎片认知整理');
  });

  it('rejects an unknown canvas_version', async () => {
    const client = createGraphClient({ transport: transportReturning(sampleCanvas({ canvas_version: '99' })) });
    await assert.rejects(() => client.canvas('exec:test:1'), /契约不匹配/);
  });

  it('rejects structurally invalid nodes/edges', async () => {
    const bad = sampleCanvas({ nodes: [{ node_id: 42 }], edges: [] });
    const client = createGraphClient({ transport: transportReturning(bad) });
    await assert.rejects(() => client.canvas('exec:test:1'), /无效/);
  });
});

describe('graph workflow view with canvas', () => {
  it('renders the canvas with Chinese labels, sidebar hint and collapsed audit', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const text = view.contentEl.text;
    assert.ok(text.includes('碎片认知整理'));
    assert.ok(text.includes('当前节点：人工确认 · Checkpoint #26'));
    assert.ok(text.includes('点击画布中的节点'));
    assert.ok(text.includes('审计区'), '文本详情保留为折叠审计区');
    assert.ok(!text.includes('c'.repeat(64)), 'spec_digest 不出现在主视觉');
    const nodes = view.contentEl.querySelectorAll('.graph-canvas-node');
    assert.equal(nodes.length, 7);
    for (const node of nodes) assert.equal(node.tagName, 'BUTTON', '节点必须是按钮（键盘可达）');
  });

  it('falls back to the text view when canvas is missing (404)', async () => {
    const client = clientMock({ canvasData: null });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    assert.equal(view.contentEl.querySelectorAll('.graph-canvas-node').length, 0);
    assert.ok(view.contentEl.text.includes('关键路径'), '回退到既有文本详情');
  });

  it('sidebar shows the selected node with safe summary only', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const nodes = view.contentEl.querySelectorAll('.graph-canvas-node');
    const inputNode = nodes.find((node) => node.text.includes('碎片输入'));
    inputNode.click();
    const sidebar = view.contentEl.querySelector('.graph-canvas-sidebar');
    assert.ok(sidebar.text.includes('类型：输入 · 状态：已完成'));
    assert.ok(sidebar.text.includes('结果摘要指纹：eeeeeeeeeeee…'));
    assert.ok(!sidebar.text.includes('结果摘要：'), '无安全摘要时不显示伪造结果');
  });

  it('blocks decisions when canvas and detail disagree — zero POST', async () => {
    const mismatched = sampleCanvas({ sequence: 27 }); // detail.sequence = 26
    const client = clientMock({ canvasData: mismatched });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const gate = view.contentEl.querySelectorAll('.graph-canvas-node')
      .find((node) => node.text.includes('人工确认'));
    gate.click();
    const sidebar = view.contentEl.querySelector('.graph-canvas-sidebar');
    assert.ok(sidebar.text.includes('数据已变化，请刷新'));
    assert.equal(sidebar.querySelectorAll('.graph-decision').length, 0, '不一致时不渲染决定按钮');
    assert.equal(client.calls.submit, 0, '零写入');
  });

  it('submits decisions bound ONLY to detail human_gates, then re-reads all three resources', async () => {
    const submitted = [];
    const client = clientMock({ onSubmit: (body) => submitted.push(body) });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    assert.deepEqual([client.calls.getRun, client.calls.path, client.calls.canvas], [1, 1, 1]);
    const gate = view.contentEl.querySelectorAll('.graph-canvas-node')
      .find((node) => node.text.includes('人工确认'));
    gate.click();
    const approve = view.contentEl.querySelector('.graph-decision-approve');
    approve.click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].specDigest, 'c'.repeat(64));
    assert.equal(submitted[0].inputDigest, 'b'.repeat(64), 'input_digest 来自 detail.human_gates');
    assert.equal(submitted[0].expectedSequence, 26);
    assert.deepEqual(
      [client.calls.getRun, client.calls.path, client.calls.canvas],
      [2, 2, 2],
      '决定成功后统一重读 detail/path/canvas'
    );
  });

  it('renders hostile labels as inert text and drops forbidden fields', async () => {
    const hostile = sampleCanvas();
    hostile.task_label = '<img src=x onerror=alert(1)>';
    hostile.nodes[0].display_label = '<b>bold</b>';
    hostile.nodes[0].prompt = 'SECRET PROMPT';
    hostile.nodes[0].output = 'RAW OUTPUT BODY';
    hostile.authorization = 'NIGO_AUTHORIZE_SECRET';
    const client = clientMock({ canvasData: hostile });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    assert.equal(view.contentEl.querySelectorAll('img').length, 0);
    assert.ok(view.contentEl.text.includes('<img src=x'));
    assert.ok(!view.contentEl.text.includes('SECRET PROMPT'));
    assert.ok(!view.contentEl.text.includes('RAW OUTPUT BODY'));
    assert.ok(!view.contentEl.text.includes('NIGO_AUTHORIZE_SECRET'));
  });

  it('releases document listeners on close', async () => {
    const client = clientMock();
    const { doc, view } = makeView(client);
    await view.openRun('exec:test:1');
    // 模拟节点拖动开始：注册 document 级监听。
    const node = view.contentEl.querySelectorAll('.graph-canvas-node')[0];
    (node.listeners.pointerdown || []).forEach((fn) => fn({ clientX: 10, clientY: 10 }));
    assert.ok((doc.listeners.pointermove || []).length > 0, '拖动期间有 document 监听');
    view.onClose();
    assert.equal((doc.listeners.pointermove || []).length, 0, 'onClose 后监听器全部解绑');
    assert.equal((doc.listeners.pointerup || []).length, 0);
  });

  it('moves nodes only as session-only visual offsets', async () => {
    const client = clientMock();
    const { doc, view } = makeView(client);
    await view.openRun('exec:test:1');
    const node = view.contentEl.querySelectorAll('.graph-canvas-node')[0];
    const before = node.getAttribute('style');
    (node.listeners.pointerdown || []).forEach((fn) => fn({ clientX: 10, clientY: 10 }));
    (doc.listeners.pointermove || []).forEach((fn) => fn({ clientX: 60, clientY: 40 }));
    (doc.listeners.pointerup || []).forEach((fn) => fn({}));
    const after = node.getAttribute('style');
    assert.notEqual(after, before, '拖动改变视觉位置');
    assert.equal(client.calls.submit, 0, '拖动零写入');
  });
});

describe('graph canvas rev2 interaction counterexamples', () => {
  const KIND_IDS = ['input', 'router', 'capability', 'validator', 'human_decision', 'action', 'subgraph', 'join', 'output'];

  it('renders nine node kinds with distinct shape classes and markers', async () => {
    const allKinds = sampleCanvas({
      nodes: KIND_IDS.map((kind, index) => canvasNode(`n${index}`, { kind, display_label: `节点${index}` })),
      edges: [],
      current_node: '',
    });
    // 恰好一个入口
    allKinds.nodes[0].is_entry = true;
    const client = clientMock({ canvasData: allKinds });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const rendered = view.contentEl.querySelectorAll('.graph-canvas-node');
    for (const kind of KIND_IDS) {
      const node = rendered.find((el) => el.classes.has(`kind-${kind}`));
      assert.ok(node, `缺少 kind-${kind} 形状类`);
      assert.ok(node.querySelectorAll('.graph-canvas-node-marker').length > 0, `缺少 ${kind} 图形标记`);
    }
    const markers = new Set(
      view.contentEl.querySelectorAll('.graph-canvas-node-marker').map((el) => el.text)
    );
    assert.ok(markers.size >= 9, '九类节点的图形标记必须互不相同');
  });

  it('draws visible arrows and on-canvas edge labels', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const edges = view.contentEl.querySelectorAll('.graph-canvas-edge');
    assert.ok(edges.length > 0);
    for (const edge of edges) {
      assert.ok((edge.getAttribute('marker-end') || '').startsWith('url(#gc-arrow-'), '边必须有方向箭头');
    }
    const labels = view.contentEl.querySelectorAll('.graph-canvas-edge-label');
    const texts = labels.map((el) => el.text).join('|');
    assert.ok(texts.includes('需要研究'), '固定映射条件标签可见');
    assert.ok(texts.includes('条件'), '未映射条件边显示「条件」');
    assert.ok(texts.includes('返修 1/1'), 'feedback 显示「返修 n/N」');
    assert.ok(!texts.includes('condition_matched'), '不显示 reason 原文');
    assert.ok(!texts.includes('e_route_research'), '技术 edge_id 不进主视觉');
  });

  it('fit view computes real scale and centering from viewport size', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const viewport = view.contentEl.querySelector('.graph-canvas-viewport');
    viewport.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 420 });
    const layout = computeCanvasLayout(sampleCanvas());
    view.contentEl.querySelector('.graph-canvas-fit').click();
    const world = view.contentEl.querySelector('.graph-canvas-world');
    const expectedScale = Math.min(1, 800 / layout.width, 420 / layout.height);
    const expectedPanX = (800 - layout.width * expectedScale) / 2;
    const expectedPanY = (420 - layout.height * expectedScale) / 2;
    const style = world.getAttribute('style');
    assert.ok(style.includes(`scale(${expectedScale})`), `真实缩放 ${expectedScale}，实际: ${style}`);
    assert.ok(style.includes(`translate(${expectedPanX}px, ${expectedPanY}px)`), `真实居中，实际: ${style}`);
  });

  it('auto-fits the whole graph on first render using the canvas column width', async () => {
    const originalRect = FakeEl.prototype.getBoundingClientRect;
    FakeEl.prototype.getBoundingClientRect = function () {
      return this.classes.has('graph-canvas-viewport')
        ? { left: 0, top: 0, width: 620, height: 420 }
        : { left: 0, top: 0, width: 0, height: 0 };
    };
    try {
      const client = clientMock();
      const { view } = makeView(client);
      await view.openRun('exec:test:1');
      const layout = computeCanvasLayout(sampleCanvas());
      const expectedScale = Math.min(1, 620 / layout.width, 420 / layout.height);
      const world = view.contentEl.querySelector('.graph-canvas-world');
      const style = world.getAttribute('style');
      assert.ok(style.includes(`scale(${expectedScale})`), `首次渲染必须自动全图适配，实际: ${style}`);
      assert.ok(style.includes(`translate(${(620 - layout.width * expectedScale) / 2}px`), style);
    } finally {
      if (originalRect) FakeEl.prototype.getBoundingClientRect = originalRect;
      else delete FakeEl.prototype.getBoundingClientRect;
    }
  });

  it('zooms anchored at the pointer position', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const viewport = view.contentEl.querySelector('.graph-canvas-viewport');
    viewport.getBoundingClientRect = () => ({ left: 100, top: 50, width: 800, height: 420 });
    (viewport.listeners.wheel || []).forEach((fn) => fn({ clientX: 300, clientY: 200, deltaY: -100, preventDefault() {} }));
    const world = view.contentEl.querySelector('.graph-canvas-world');
    const style = world.getAttribute('style');
    assert.ok(style.includes('scale(1.08'), style);
    assert.ok(style.includes('translate(-16px, -12px)'), `指针锚定平移，实际: ${style}`);
  });

  it('drags nodes with scale-corrected coordinates', async () => {
    const client = clientMock();
    const { doc, view } = makeView(client);
    await view.openRun('exec:test:1');
    const viewport = view.contentEl.querySelector('.graph-canvas-viewport');
    viewport.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 420 });
    (viewport.listeners.wheel || []).forEach((fn) => fn({ clientX: 0, clientY: 0, deltaY: 100, preventDefault() {} })); // scale 0.92
    const node = view.contentEl.querySelectorAll('.graph-canvas-node')[0];
    const before = node.getAttribute('style');
    (node.listeners.pointerdown || []).forEach((fn) => fn({ clientX: 10, clientY: 10, stopPropagation() {} }));
    (doc.listeners.pointermove || []).forEach((fn) => fn({ clientX: 56, clientY: 56 }));
    (doc.listeners.pointerup || []).forEach((fn) => fn({}));
    const layout = computeCanvasLayout(sampleCanvas());
    const base = layout.positions.get('input_fragment');
    const expectedLeft = base.x + 46 / 0.92;
    const expectedTop = base.y + 46 / 0.92;
    const after = node.getAttribute('style');
    assert.notEqual(before, after);
    assert.ok(after.includes(`left:${expectedLeft}px`), `缩放换算位移，实际: ${after}`);
    assert.ok(after.includes(`top:${expectedTop}px`));
  });

  it('unbinds document drag listeners immediately on pointerup and pointercancel', async () => {
    const client = clientMock();
    const { doc, view } = makeView(client);
    await view.openRun('exec:test:1');
    const node = view.contentEl.querySelectorAll('.graph-canvas-node')[0];
    (node.listeners.pointerdown || []).forEach((fn) => fn({ clientX: 0, clientY: 0, stopPropagation() {} }));
    assert.ok((doc.listeners.pointermove || []).length > 0);
    (doc.listeners.pointerup || []).forEach((fn) => fn({}));
    assert.equal((doc.listeners.pointermove || []).length, 0, 'pointerup 后立即解绑');
    (node.listeners.pointerdown || []).forEach((fn) => fn({ clientX: 0, clientY: 0, stopPropagation() {} }));
    (doc.listeners.pointercancel || []).forEach((fn) => fn({}));
    assert.equal((doc.listeners.pointermove || []).length, 0, 'pointercancel 后立即解绑');
  });

  it('refresh button re-reads detail/path/canvas exactly once per click', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    assert.deepEqual([client.calls.getRun, client.calls.path, client.calls.canvas], [1, 1, 1]);
    view.contentEl.querySelector('.graph-canvas-refresh').click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual([client.calls.getRun, client.calls.path, client.calls.canvas], [2, 2, 2]);
  });

  it('double-clicking a decision produces exactly one POST', async () => {
    const submitted = [];
    const client = clientMock({ onSubmit: (body) => submitted.push(body) });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const gate = view.contentEl.querySelectorAll('.graph-canvas-node')
      .find((node) => node.text.includes('人工确认'));
    gate.click();
    const approve = view.contentEl.querySelector('.graph-decision-approve');
    approve.click();
    approve.click();
    approve.click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(submitted.length, 1, 'single-flight：连续点击最多一个 POST');
  });

  it('offers refresh right beside the mismatch notice', async () => {
    const client = clientMock({ canvasData: sampleCanvas({ sequence: 99 }) });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const gate = view.contentEl.querySelectorAll('.graph-canvas-node')
      .find((node) => node.text.includes('人工确认'));
    gate.click();
    const sidebar = view.contentEl.querySelector('.graph-canvas-sidebar');
    assert.ok(sidebar.text.includes('数据已变化，请刷新'));
    assert.ok(sidebar.querySelector('.graph-canvas-refresh'), '提示旁必须有直接刷新入口');
  });

  it('cleans up on showList, openNode and onClose', async () => {
    const client = clientMock();
    const { doc, view } = makeView(client);
    await view.openRun('exec:test:1');
    const node = view.contentEl.querySelectorAll('.graph-canvas-node')[0];
    (node.listeners.pointerdown || []).forEach((fn) => fn({ clientX: 0, clientY: 0, stopPropagation() {} }));
    assert.ok((doc.listeners.pointermove || []).length > 0);
    await view.showList();
    assert.equal((doc.listeners.pointermove || []).length, 0, '返回列表即清理');
    assert.equal(view.canvasDispose, null);
  });

  it('never lands late deferred responses after unload', async () => {
    let resolveCanvas;
    const deferredCanvas = new Promise((resolve) => { resolveCanvas = resolve; });
    const client = clientMock();
    client.canvas = async () => { client.calls.canvas += 1; return deferredCanvas; };
    const { doc, view } = makeView(client);
    const opening = view.openRun('exec:test:1');
    await view.onClose();
    resolveCanvas(sampleCanvas());
    await opening;
    assert.equal(view.detail, null, 'unload 后 detail 不得落地');
    assert.equal(view.contentEl.querySelectorAll('.graph-canvas-node').length, 0, 'unload 后 DOM 不得更新');
    assert.equal((doc.listeners.pointermove || []).length, 0);
  });

  it('keeps technical ids out of the main visual surface', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const mainVisual = [
      view.contentEl.querySelector('.graph-canvas-header')?.text || '',
      view.contentEl.querySelector('.graph-canvas-host')?.text || '',
      view.contentEl.querySelector('.graph-canvas-sidebar')?.text || '',
    ].join('\n');
    assert.ok(!mainVisual.includes('fragment-cognitive-v1'), 'graph_id 不进主视觉');
    assert.ok(!mainVisual.includes('input_fragment'), 'node_id 不进主视觉');
    assert.ok(!mainVisual.includes('e_route_research'), 'edge_id 不进主视觉');
    assert.ok(view.contentEl.text.includes('fragment-cognitive-v1'), '技术 ID 保留在审计区');
  });

  it('falls back to the text view on a malformed contract', async () => {
    const malformed = sampleCanvas();
    malformed.nodes.push(canvasNode('input_fragment', { kind: 'input' })); // 重复 node_id
    const client = clientMock({ canvasData: malformed });
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    assert.equal(view.contentEl.querySelectorAll('.graph-canvas-node').length, 0, '畸形契约不进入布局器');
    assert.ok(view.contentEl.text.includes('关键路径'), '回退文本视图');
  });

  it('shows no result section for pending nodes without digests', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const gate = view.contentEl.querySelectorAll('.graph-canvas-node')
      .find((node) => node.text.includes('人工确认'));
    gate.click();
    const sidebar = view.contentEl.querySelector('.graph-canvas-sidebar');
    assert.ok(!sidebar.text.includes('结果摘要'), '无结果时连指纹区都不显示');
  });
});

describe('graph canvas rev3 counterexamples', () => {
  it('edge click opens the safe edge explanation in the sidebar', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const edge = view.contentEl.querySelectorAll('.graph-canvas-edge')
      .find((el) => (el.getAttribute('aria-label') || '').includes('需要研究'));
    assert.ok(edge, '条件边可定位');
    (edge.listeners.click || []).forEach((fn) => fn({}));
    const sidebar = view.contentEl.querySelector('.graph-canvas-sidebar');
    assert.ok(sidebar.text.includes('边的选择说明'));
    assert.ok(sidebar.text.includes('类型：条件 · 已走过'));
    assert.ok(sidebar.text.includes('说明：需要研究'));
    assert.ok(sidebar.text.includes('选择方式：声明顺序'));
    assert.ok(!sidebar.text.includes('condition_matched'), '不显示条件原文');
    assert.ok(!sidebar.text.includes('e_route_research'), '不显示技术 edge_id');
  });

  it('edge is keyboard focusable and opens with Enter', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const edge = view.contentEl.querySelectorAll('.graph-canvas-edge')[0];
    assert.equal(edge.getAttribute('tabindex'), '0', '边可 Tab 聚焦');
    assert.equal(edge.getAttribute('role'), 'button');
    (edge.listeners.keydown || []).forEach((fn) => fn({ key: 'Enter' }));
    const sidebar = view.contentEl.querySelector('.graph-canvas-sidebar');
    assert.ok(sidebar.text.includes('边的选择说明'), 'Enter 打开边说明');
  });

  it('feedback edge sidebar shows n/N and the safe exhausted target label', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const feedback = view.contentEl.querySelectorAll('.graph-canvas-edge')
      .find((el) => (el.getAttribute('aria-label') || '').includes('返修'));
    (feedback.listeners.click || []).forEach((fn) => fn({}));
    const sidebar = view.contentEl.querySelector('.graph-canvas-sidebar');
    assert.ok(sidebar.text.includes('返修 1/1'));
    assert.ok(sidebar.text.includes('返修耗尽后改道（转向 人工确认）'), 'exhausted_to 转成中文名');
    assert.ok(!sidebar.text.includes('human_confirmation'), '目标技术 ID 不显示');
  });

  it('never puts unknown kind/status into CSS classes', async () => {
    const hostile = canvasModel.buildCanvasModel(sampleCanvas({
      nodes: [canvasNode('n0', {
        kind: 'input is-waiting"><script',
        status: 'succeeded is-current',
        is_entry: true,
      })],
      edges: [],
      current_node: '',
    }));
    const node = hostile.nodes[0];
    assert.equal(node.kindClass, 'kind-unknown', '伪造 kind 只得到固定 unknown 类');
    assert.equal(node.statusClass, 'is-unknown', '伪造 status 只得到固定 unknown 类');
    assert.ok(!node.kindClass.includes(' '), 'class 不含空格注入');
    assert.ok(!node.statusClass.includes('<'), 'class 不含 HTML 字符');
    assert.equal(node.statusLabel, 'succeeded is-current', '原始未知状态只作为纯文本');
    assert.equal(node.kindLabel, '未知类型');
  });

  it('accepts legal Unicode (Chinese) node ids through the contract', async () => {
    const chinese = sampleCanvas({
      nodes: [canvasNode('碎片输入', { display_label: '碎片输入', kind: 'input', is_entry: true })],
      edges: [],
      current_node: '',
    });
    const client = createGraphClient({
      transport: async () => ({ status: 200, json: { contract_version: '2', data: chinese, error: null }, text: '' }),
    });
    const canvas = await client.canvas('exec:test:1');
    assert.equal(canvas.nodes[0].node_id, '碎片输入', '合法中文 ID 不得被缩窄拒绝');
  });

  it('rejects malformed contracts across the rev3 rules', async () => {
    const make = (mutate) => {
      const data = sampleCanvas();
      mutate(data);
      return createGraphClient({
        transport: async () => ({ status: 200, json: { contract_version: '2', data, error: null }, text: '' }),
      });
    };
    const cases = {
      'all_success 带 threshold': (d) => { d.nodes[4].join.threshold = 2; },
      'minimum_success 缺 threshold': (d) => { d.nodes[4].join.mode = 'minimum_success'; d.nodes[4].join.threshold = null; },
      '顺序边携带返修字段': (d) => { d.edges[0].max_traversals = 2; },
      '返修边缺上限': (d) => { d.edges[6].max_traversals = null; },
      '返修边缺耗尽策略': (d) => { d.edges[6].on_exhausted = null; },
      '结果摘要含 URL': (d) => { d.nodes[0].result_summary = '见 https://example.com'; },
      'decision_source 未知枚举': (d) => { d.edges[0].decision_source = 'guessed'; },
      '闸门状态未知枚举': (d) => { d.nodes[6].human_gate.status = 'maybe'; },
      '中文 ID 但带路径语义': (d) => { d.nodes[0].node_id = '碎片/输入'; },
    };
    for (const [name, mutate] of Object.entries(cases)) {
      await assert.rejects(() => make(mutate).canvas('exec:test:1'), /无效|不匹配|不得|必须/, name);
    }
  });

  it('decision clicked during an in-flight refresh shows busy and posts nothing', async () => {
    const submitted = [];
    const client = clientMock({ onSubmit: (body) => submitted.push(body) });
    const { view } = makeView(client);
    await view.openRun('exec:test:1'); // 首载用正常响应完成
    // 之后再换成 deferred，让刷新保持在途
    let resolveCanvas;
    const deferredCanvas = new Promise((resolve) => { resolveCanvas = resolve; });
    const realCanvas = client.canvas;
    client.canvas = async () => { client.calls.canvas += 1; return deferredCanvas; };
    view.refreshRun();
    await new Promise((resolve) => setImmediate(resolve));
    await view.decide(
      { nodeId: 'human_confirmation', specDigest: 'c'.repeat(64), inputDigest: 'b'.repeat(64), gateSequence: 26 },
      'approve'
    );
    assert.equal(submitted.length, 0, '刷新进行中点击决定不得产生 POST');
    assert.ok(view.sidebarEl.text.includes('正在处理上一个操作'), '显示忙碌提示而非静默');
    client.canvas = realCanvas;
    resolveCanvas(sampleCanvas());
    await new Promise((resolve) => setImmediate(resolve));
  });

  it('onOpen after onClose resets disposed and the view works again', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    await view.onClose();
    assert.equal(view.disposed, true);
    await view.onOpen();
    assert.equal(view.disposed, false, '重开必须复位 disposed');
    assert.ok(view.contentEl.text.length > 0, '重开后列表可用');
  });

  it('isolates SVG marker ids per canvas instance', async () => {
    const clientA = clientMock();
    const clientB = clientMock();
    const { view: viewA } = makeView(clientA);
    const { view: viewB } = makeView(clientB);
    await viewA.openRun('exec:test:1');
    await viewB.openRun('exec:test:1');
    const markerA = viewA.contentEl.querySelector('.graph-canvas-edge').getAttribute('marker-end');
    const markerB = viewB.contentEl.querySelector('.graph-canvas-edge').getAttribute('marker-end');
    assert.ok(markerA && markerB);
    assert.notEqual(markerA, markerB, '两个画布实例的 marker ID 必须隔离');
  });
});

describe('graph canvas rev4 counterexamples', () => {
  it('accepts unknown run/node status through the real client and degrades honestly', async () => {
    const weird = sampleCanvas({ status: 'mystery_run_status' });
    weird.nodes[0].status = 'mystery_node_status';
    const client = createGraphClient({
      transport: async () => ({ status: 200, json: { contract_version: '2', data: weird, error: null }, text: '' }),
    });
    const canvas = await client.canvas('exec:test:1');
    assert.equal(canvas.status, 'mystery_run_status', '未知 run status 通过契约');
    assert.equal(canvas.nodes[0].status, 'mystery_node_status', '未知 node status 通过契约');
    const model = canvasModel.buildCanvasModel(canvas);
    assert.equal(model.statusLabel, 'mystery_run_status', 'run 未知状态纯文本显示');
    assert.equal(model.statusClass, 'is-unknown');
    assert.equal(model.nodes[0].statusLabel, 'mystery_node_status');
    assert.equal(model.nodes[0].statusClass, 'is-unknown');
  });

  it('disables buttons during a deferred refresh and removes the disabled attribute after', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    let resolveCanvas;
    const deferredCanvas = new Promise((resolve) => { resolveCanvas = resolve; });
    const realCanvas = client.canvas;
    client.canvas = async () => { client.calls.canvas += 1; return deferredCanvas; };
    const refreshing = view.refreshRun();
    await new Promise((resolve) => setImmediate(resolve));
    const refreshBtn = view.contentEl.querySelector('.graph-canvas-refresh');
    assert.equal(refreshBtn.getAttribute('disabled'), 'disabled', '刷新进行中按钮禁用');
    client.canvas = realCanvas;
    resolveCanvas(sampleCanvas());
    await refreshing;
    const after = view.contentEl.querySelector('.graph-canvas-refresh');
    assert.equal(after.getAttribute('disabled'), null, '完成后 disabled 属性必须消失');
  });

  it('restores buttons with removeAttribute after a failed refresh', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    client.canvas = async () => { client.calls.canvas += 1; throw new GraphApiError('unreachable', '无法连接本地 Graph 服务'); };
    // canvas 失败 → fetchCanvasSafe 回退 null → 文本视图重渲染，按钮恢复可用
    await view.refreshRun();
    assert.ok(view.contentEl.text.includes('关键路径'), '失败后回退文本视图');
    const leftovers = [
      ...view.contentEl.querySelectorAll('.graph-canvas-refresh'),
      ...view.contentEl.querySelectorAll('.graph-decision'),
    ].filter((button) => button.getAttribute('disabled') === 'disabled');
    assert.equal(leftovers.length, 0, '失败路径不得残留禁用按钮');
  });

  it('restores decision buttons after a failed POST', async () => {
    const client = clientMock();
    client.submitHumanDecision = async () => { client.calls.submit += 1; throw new GraphApiError('http_error', 'Graph 服务返回 HTTP 409', { status: 409 }); };
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const gate = view.contentEl.querySelectorAll('.graph-canvas-node')
      .find((node) => node.text.includes('人工确认'));
    gate.click();
    view.contentEl.querySelector('.graph-decision-approve').click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(client.calls.submit, 1, '失败请求也只产生一个 POST');
    const disabled = [
      ...view.contentEl.querySelectorAll('.graph-canvas-refresh'),
      ...view.contentEl.querySelectorAll('.graph-decision'),
    ].filter((button) => button.getAttribute('disabled') === 'disabled');
    assert.equal(disabled.length, 0, '失败后按钮必须恢复可用');
  });

  it('G1 shows the server rejection reason after a failed POST (no silent re-render)', async () => {
    const client = clientMock();
    client.submitHumanDecision = async () => {
      client.calls.submit += 1;
      throw new GraphApiError('http_error', 'Graph 服务返回 HTTP 409', { status: 409, code: 'sequence_mismatch' });
    };
    const { view } = makeView(client);
    await view.openRun('exec:test:1');
    const gate = view.contentEl.querySelectorAll('.graph-canvas-node')
      .find((node) => node.text.includes('人工确认'));
    gate.click();
    view.contentEl.querySelector('.graph-decision-approve').click();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    // G1：失败原因必须可见（sequence_mismatch → 中文映射），不允许静默重渲染
    const err = view.contentEl.querySelector('.graph-canvas-sidebar-error');
    assert.ok(err, '失败后侧栏出现错误行');
    assert.ok(err.text.includes('数据已变化'), '错误携带中文原因（sequence_mismatch 映射）');
    // 视图不假装成功：决定按钮仍在人工确认态且恢复可用
    const approve = view.contentEl.querySelector('.graph-decision-approve');
    assert.ok(approve, '决定按钮仍在（未假装成功）');
    assert.equal(approve.getAttribute('disabled'), null, '失败后按钮恢复可用');
  });
});

describe('graph canvas production-scope regression', () => {
  it('adds the my-life-graph-workflow scope class on open (without it all canvas CSS is dead)', async () => {
    const client = clientMock();
    const { view } = makeView(client);
    await view.onOpen();
    assert.ok(
      view.contentEl.classes.has('my-life-graph-workflow'),
      'contentEl 必须带作用域类，否则画布样式全部失效（生产实机暴露的回归）'
    );
  });
});
