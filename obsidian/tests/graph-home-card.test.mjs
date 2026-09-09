import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const { computeGraphSummary, injectHomepageGraphCard } = require(path.join(ROOT, 'src', 'console', 'graph-home-card.js'));
const { injectHomepageEntry } = require(path.join(ROOT, 'src', 'console', 'home-entry.js'));
const { createGraphClient } = require(path.join(ROOT, 'src', 'console', 'graph-client.js'));

function run(overrides = {}) {
  return {
    run_id: 'exec:test-1',
    graph_id: 'fragment-cognitive-graph-v1',
    spec_digest: 'a'.repeat(64),
    status: 'running',
    sequence: 1,
    step_count: 3,
    pending_human: [],
    started_at: '2026-08-04T00:00:00+00:00',
    updated_at: '2026-08-04T00:01:00+00:00',
    ...overrides,
  };
}

function homeDocument() {
  const doc = new FakeEl('document');
  const home = doc.createDiv({ cls: 'my-life-homepage-view' });
  const content = home.createDiv({ cls: 'life-dashboard-content' });
  content.createDiv({ cls: 'life-decision-bar' });
  return doc;
}

function readyState(runs) {
  return { kind: 'ready', runs };
}

function noopHandlers(calls = {}) {
  return {
    onOpen: () => { calls.open = (calls.open || 0) + 1; },
    onRefresh: () => { calls.refresh = (calls.refresh || 0) + 1; },
  };
}

describe('graph homepage summary computation', () => {
  it('counts a normal mix of runs into mutually exclusive buckets', () => {
    const summary = computeGraphSummary([
      run({ run_id: 'exec:a', status: 'running' }),
      run({ run_id: 'exec:b', status: 'paused' }),
      run({ run_id: 'exec:c', status: 'human_wait', pending_human: ['gate'] }),
      run({ run_id: 'exec:d', status: 'blocked' }),
      run({ run_id: 'exec:e', status: 'completed' }),
    ]);
    assert.deepEqual(
      { active: summary.active, waiting: summary.waiting, abnormal: summary.abnormal, completed: summary.completed },
      { active: 2, waiting: 1, abnormal: 1, completed: 1 }
    );
    assert.equal(summary.total, 5);
    assert.equal(summary.attention, true);
    assert.equal(summary.headline, '异常停止');
  });

  it('prioritises waiting-for-human over plain running and never double-counts', () => {
    const summary = computeGraphSummary([
      run({ run_id: 'exec:a', status: 'running' }),
      run({ run_id: 'exec:b', status: 'running', pending_human: ['gate-1'] }),
    ]);
    assert.equal(summary.active, 1, 'run with a pending gate leaves the active bucket');
    assert.equal(summary.waiting, 1);
    assert.equal(summary.headline, '等待人工');
    assert.equal(summary.active + summary.waiting, 2, 'no run is counted twice');
  });

  it('prioritises abnormal stop over a pending human gate', () => {
    const summary = computeGraphSummary([
      run({ run_id: 'exec:a', status: 'running' }),
      run({ run_id: 'exec:b', status: 'blocked', pending_human: ['gate-1'] }),
      run({ run_id: 'exec:c', status: 'failed' }),
      run({ run_id: 'exec:d', status: 'aborted' }),
    ]);
    assert.equal(summary.abnormal, 3, 'blocked/failed/aborted all land in the abnormal bucket');
    assert.equal(summary.waiting, 0, 'an abnormal run with a pending gate is not double-counted');
    assert.equal(summary.headline, '异常停止');
  });

  it('keeps unknown statuses out of every bucket and reports them honestly', () => {
    const summary = computeGraphSummary([
      run({ run_id: 'exec:a', status: 'running' }),
      run({ run_id: 'exec:b', status: 'mystery_state' }),
    ]);
    assert.equal(summary.active, 1);
    assert.equal(summary.unknown, 1);
    assert.equal(summary.waiting + summary.abnormal + summary.completed, 0, 'unknown is never misclassified');
    assert.equal(summary.headline, '进行中');
  });

  it('reports 未知状态 when every run is unrecognised', () => {
    const summary = computeGraphSummary([run({ status: 'mystery' })]);
    assert.equal(summary.headline, '未知状态');
    assert.equal(summary.attention, false);
  });

  it('picks the latest run by newest updated_at, never by checkpoint sequence', () => {
    // 反例：旧 run 的 sequence 高、新 run 的 sequence 低——sequence 只保证
    // 单个 run 内单调，绝不能跨 run 判断时间新旧；必须选 updated_at 更新的 run。
    const older = run({
      run_id: 'exec:old',
      status: 'completed',
      sequence: 99,
      started_at: '2026-08-04T00:00:00+00:00',
      updated_at: '2026-08-04T00:05:00+00:00',
    });
    const newer = run({
      run_id: 'exec:new',
      status: 'completed',
      sequence: 2,
      started_at: '2026-08-05T00:00:00+00:00',
      updated_at: '2026-08-05T00:01:00+00:00',
    });
    const first = computeGraphSummary([older, newer]);
    const second = computeGraphSummary([newer, older]);
    assert.equal(first.latest.runId, 'exec:new', 'higher sequence of the older run must not win');
    assert.equal(second.latest.runId, 'exec:new', 'input order must not matter');
    assert.equal(first.latest.sequence, 2, 'the low-sequence newer run is the latest');
  });

  it('breaks updated_at ties deterministically by run_id and pins the selected run', () => {
    const stamp = '2026-08-04T08:00:00+00:00';
    const a = run({ run_id: 'exec:aaa', sequence: 7, updated_at: stamp });
    const b = run({ run_id: 'exec:bbb', sequence: 3, updated_at: stamp });
    assert.equal(computeGraphSummary([a, b]).latest.runId, 'exec:bbb');
    assert.equal(computeGraphSummary([b, a]).latest.runId, 'exec:bbb', 'tie-break is stable under input order');
  });

  it('does not claim a latest run when no run carries a reliable updated_at', () => {
    const missing = run({ run_id: 'exec:no-time', status: 'completed', sequence: 5 });
    delete missing.updated_at;
    const invalid = run({ run_id: 'exec:bad-time', status: 'completed', sequence: 6, updated_at: 'not-a-time' });
    const summary = computeGraphSummary([missing, invalid]);
    assert.equal(summary.total, 2, 'runs still count toward the summary');
    assert.equal(summary.latest, null, 'without a reliable time there is no 最近运行');
  });

  it('ignores unreliable timestamps when a reliable candidate exists', () => {
    const timeless = run({ run_id: 'exec:no-time', status: 'running', sequence: 99 });
    delete timeless.updated_at;
    const reliable = run({ run_id: 'exec:timed', status: 'running', sequence: 1 });
    const summary = computeGraphSummary([timeless, reliable]);
    assert.equal(summary.latest.runId, 'exec:timed', 'the high-sequence timeless run is not a candidate');
  });

  it('handles an empty list as 暂无运行', () => {
    const summary = computeGraphSummary([]);
    assert.equal(summary.total, 0);
    assert.equal(summary.headline, '暂无运行');
    assert.equal(summary.latest, null);
  });
});

describe('graph runs-list time-field contract validation', () => {
  const envelope = (data) => ({ status: 200, json: { contract_version: '2', data } });
  const clientWith = (data) => createGraphClient({ transport: async () => envelope(data) });

  it('accepts runs with timezone-aware ISO started_at and updated_at', async () => {
    const runs = await clientWith([run()]).listRuns();
    assert.equal(runs[0].run_id, 'exec:test-1');
  });

  it('rejects a run whose updated_at is missing, unparseable or timezone-less', async () => {
    for (const updatedAt of [undefined, 'not-a-time', '2026-08-04T00:01:00']) {
      const broken = run({ run_id: 'exec:broken' });
      if (updatedAt === undefined) delete broken.updated_at;
      else broken.updated_at = updatedAt;
      await assert.rejects(() => clientWith([broken]).listRuns(), (error) => {
        assert.equal(error.kind, 'invalid_response');
        assert.equal(error.message, 'Graph 运行时间字段无效');
        return true;
      });
    }
  });

  it('rejects a run whose started_at is missing, unparseable or timezone-less', async () => {
    for (const startedAt of [undefined, 'not-a-time', '2026-08-04 00:00:00']) {
      const broken = run({ run_id: 'exec:broken' });
      if (startedAt === undefined) delete broken.started_at;
      else broken.started_at = startedAt;
      await assert.rejects(() => clientWith([broken]).listRuns(), (error) => {
        assert.equal(error.kind, 'invalid_response');
        assert.equal(error.message, 'Graph 运行时间字段无效');
        return true;
      });
    }
  });
});

describe('graph homepage card rendering', () => {
  it('renders the three stats only while runs need attention, with the latest line and no raw graph_id', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([
      run({ run_id: 'exec:a', status: 'running' }),
      run({ run_id: 'exec:b', status: 'blocked', sequence: 8, updated_at: '2026-08-05T00:01:00+00:00' }),
    ]), noopHandlers());
    const text = doc.text;
    assert.ok(text.includes('Graph 工作流'));
    assert.ok(text.includes('跨节点推进、人工闸门与异常状态'));
    assert.ok(text.includes('进行中'));
    assert.ok(text.includes('等待人工'));
    assert.ok(text.includes('异常停止'));
    assert.ok(text.includes('最近运行：已阻断 · Checkpoint #8'));
    assert.ok(!text.includes('fragment-cognitive-graph-v1'), 'homepage never shows the raw graph_id');
    assert.ok(text.includes('打开 Graph 工作流'));
    const pill = doc.querySelector('.life-graph-home-pill');
    assert.ok(pill && pill.classes.has('is-abnormal'), 'abnormal state raises emphasis');
  });

  it('uses a compact single line when all three counters are zero instead of three 0 cards', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([
      run({ run_id: 'exec:done', status: 'completed', sequence: 4 }),
    ]), noopHandlers());
    const text = doc.text;
    assert.equal(doc.querySelectorAll('.life-graph-home-stat').length, 0, 'no zero stat cards in the calm state');
    assert.ok(text.includes('Graph 工作流 · 流程已结束 · Checkpoint #4'));
    assert.ok(!text.includes('已完成'), 'completed is never phrased as an accepted business outcome');
    assert.equal(doc.querySelectorAll('.life-graph-home-pill').length, 0, 'calm state raises no emphasis');
    assert.ok(doc.querySelector('.life-graph-home-open'), 'main entry stays available');
  });

  it('labels an active-only summary without emphasis so it never outshines today tasks and focus', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([run({ run_id: 'exec:a', status: 'running' })]), noopHandlers());
    assert.equal(doc.querySelectorAll('.life-graph-home-stat').length, 3, 'active runs still show the stats');
    assert.equal(doc.querySelectorAll('.life-graph-home-pill').length, 0, 'normal state gets no pill');
  });

  it('shows a waiting summary with emphasis', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([
      run({ run_id: 'exec:a', status: 'human_wait', pending_human: ['gate-1'] }),
    ]), noopHandlers());
    const pill = doc.querySelector('.life-graph-home-pill');
    assert.ok(pill && pill.classes.has('is-waiting'), 'waiting state raises emphasis');
  });

  it('G4 renders a direct waiting-handler button that opens the latest run (runId never in DOM)', () => {
    const calls = {};
    const handlers = {
      onOpen: () => { calls.open = (calls.open || 0) + 1; },
      onRefresh: () => { calls.refresh = (calls.refresh || 0) + 1; },
      onOpenRun: (runId) => { calls.openRun = runId; },
    };
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([
      run({ run_id: 'exec:wait', status: 'human_wait', pending_human: ['gate-1'], sequence: 9, updated_at: '2026-08-05T00:01:00+00:00' }),
      run({ run_id: 'exec:run', status: 'running' }),
    ]), handlers);
    const btn = doc.querySelector('.life-graph-home-waiting');
    assert.ok(btn, '等待人工时出现「去处理等待项」直达按钮');
    assert.equal(btn.textContent, '去处理等待项');
    btn.click();
    assert.equal(calls.openRun, 'exec:wait', '点击直达最近等待 run 详情');
    assert.ok(!doc.text.includes('exec:wait'), '技术 ID 只进 handler，不进 DOM');
    assert.ok(!doc.text.includes('exec:run'), '任一 runId 都不进 DOM');
  });

  it('G4 does not render the waiting button when nothing waits (active or calm)', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([run({ run_id: 'exec:run', status: 'running' })]), noopHandlers());
    assert.equal(doc.querySelectorAll('.life-graph-home-waiting').length, 0, '仅进行中不出现直达按钮');
    const doc2 = homeDocument();
    injectHomepageGraphCard(doc2, readyState([run({ run_id: 'exec:done', status: 'completed' })]), noopHandlers());
    assert.equal(doc2.querySelectorAll('.life-graph-home-waiting').length, 0, '平静态不出现直达按钮');
  });

  it('G8 shows a stale banner over kept data after a failed refresh, distinct from first failure', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, {
      kind: 'ready',
      stale: true,
      runs: [run({ run_id: 'exec:a', status: 'human_wait', pending_human: ['g1'], sequence: 5, updated_at: '2026-08-05T00:01:00+00:00' })],
    }, noopHandlers());
    const text = doc.text;
    assert.ok(text.includes('上次刷新失败，显示上次数据'), 'G8：刷新失败显式标注（区别于首次失败）');
    assert.ok(text.includes('等待人工'), '旧数据保留展示');
    assert.ok(text.includes('Checkpoint #5'), '旧数据内容完整');
    // 首次失败仍是独立文案
    const doc2 = homeDocument();
    injectHomepageGraphCard(doc2, { kind: 'error', message: 'Graph 服务暂时不可用' }, noopHandlers());
    assert.ok(doc2.text.includes('Graph 服务暂时不可用'));
    assert.ok(!doc2.text.includes('上次刷新失败'), '首次失败不用 stale 文案');
  });

  it('degrades to a calm line without a 最近运行 claim when no run has a reliable time', () => {
    const doc = homeDocument();
    const timeless = run({ run_id: 'exec:no-time', status: 'completed' });
    delete timeless.updated_at;
    injectHomepageGraphCard(doc, readyState([timeless]), noopHandlers());
    const text = doc.text;
    assert.ok(text.includes('Graph 工作流 · 流程已结束'));
    assert.ok(!text.includes('最近运行'), 'no latest claim without a reliable time');
    assert.ok(!text.includes('Checkpoint #'), 'no checkpoint segment without a reliable time');
  });

  it('shows 暂无 Graph 运行 in the same compact form and keeps the entry usable', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([]), noopHandlers());
    assert.ok(doc.text.includes('暂无 Graph 运行'));
    assert.ok(doc.querySelector('.life-graph-home-compact'), 'empty state uses the compact form');
    assert.equal(doc.querySelectorAll('.life-graph-home-stat').length, 0);
    assert.ok(doc.querySelector('.life-graph-home-open'), 'main entry stays available');
  });

  it('shows an honest compact error when the service is unreachable and keeps the entry usable', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, { kind: 'error', message: 'Graph 服务暂时不可用' }, noopHandlers());
    assert.ok(doc.text.includes('Graph 服务暂时不可用'));
    const compact = doc.querySelector('.life-graph-home-compact');
    assert.ok(compact && compact.classes.has('is-error'), 'error state uses the compact form');
    assert.equal(doc.querySelectorAll('.life-graph-home-stat').length, 0, 'no fabricated zero counters');
    assert.ok(!doc.text.includes('进行中 0'), 'error state must not fake an empty summary');
    const open = doc.querySelector('.life-graph-home-open');
    assert.ok(open, 'main entry stays present in the error state');
    const calls = {};
    injectHomepageGraphCard(doc, { kind: 'error', message: 'Graph 服务暂时不可用' }, noopHandlers(calls));
    doc.querySelector('.life-graph-home-open').click();
    assert.equal(calls.open, 1, 'entry still opens the workflow in the error state');
  });

  it('renders unknown statuses verbatim in the compact line without misclassification', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, readyState([run({ status: 'mystery_state', sequence: 3 })]), noopHandlers());
    assert.ok(doc.text.includes('mystery_state'), 'unknown status text passes through verbatim');
    assert.ok(!doc.querySelector('.life-graph-home-pill'), 'unknown state raises no emphasis');
  });

  it('renders hostile strings as plain text only and never shows the raw graph_id', () => {
    const doc = homeDocument();
    const hostile = '<img src=x onerror=alert(1)>';
    injectHomepageGraphCard(doc, readyState([
      run({ graph_id: hostile, status: hostile, sequence: 2 }),
    ]), noopHandlers());
    assert.equal(doc.querySelectorAll('img').length, 0, 'no element may be created from external text');
    assert.ok(doc.text.includes(hostile), 'the hostile status is displayed as inert text');
    const calmDoc = homeDocument();
    injectHomepageGraphCard(calmDoc, readyState([run({ graph_id: hostile, status: 'completed', sequence: 2 })]), noopHandlers());
    assert.ok(!calmDoc.text.includes(hostile), 'the raw graph_id never reaches the homepage DOM');
  });

  it('never surfaces prompt, response, output or credential fields in the card', () => {
    const doc = homeDocument();
    const leaky = run({
      graph_id: 'safe-graph',
      prompt: 'SECRET PROMPT BODY',
      response_text: 'SECRET RESPONSE BODY',
      api_key: 'sk-live-secret',
      authorization: 'NIGO_AUTHORIZE_SECRET',
      node_output: 'RAW NODE OUTPUT',
    });
    injectHomepageGraphCard(doc, readyState([leaky]), noopHandlers());
    for (const leak of ['SECRET PROMPT BODY', 'SECRET RESPONSE BODY', 'sk-live-secret', 'NIGO_AUTHORIZE_SECRET', 'RAW NODE OUTPUT']) {
      assert.ok(!doc.text.includes(leak), `card must not contain: ${leak}`);
    }
    const summary = computeGraphSummary([leaky]);
    assert.ok(!('prompt' in summary) && !('api_key' in summary), 'summary carries only safe metadata');
    assert.ok(!('graphId' in (summary.latest || {})), 'summary.latest never carries the raw graph_id');
  });

  it('opens exactly the existing workflow view from the main button', () => {
    const doc = homeDocument();
    const calls = {};
    injectHomepageGraphCard(doc, readyState([run()]), noopHandlers(calls));
    doc.querySelector('.life-graph-home-open').click();
    assert.equal(calls.open, 1);
    assert.equal(calls.refresh || 0, 0, 'open must not trigger a refresh');
  });

  it('keeps a single card per homepage copy across repeated scans and refreshes', () => {
    const doc = homeDocument();
    const calls = {};
    const handlers = noopHandlers(calls);
    assert.equal(injectHomepageGraphCard(doc, { kind: 'loading' }, handlers), 1);
    assert.equal(injectHomepageGraphCard(doc, readyState([run()]), handlers), 0, 'second scan updates in place');
    assert.equal(doc.querySelectorAll('.life-graph-home').length, 1);
    const refresh = doc.querySelector('.life-graph-home-refresh');
    refresh.click();
    assert.equal(calls.refresh, 1);
    injectHomepageGraphCard(doc, readyState([run(), run({ run_id: 'exec:b', status: 'blocked', sequence: 5 })]), handlers);
    assert.equal(doc.querySelectorAll('.life-graph-home').length, 1, 'refresh re-render never duplicates the card');
    assert.ok(doc.text.includes('异常停止'), 'refreshed content replaces the old summary');
  });

  it('disables the refresh button while loading', () => {
    const doc = homeDocument();
    injectHomepageGraphCard(doc, { kind: 'loading' }, noopHandlers());
    const refresh = doc.querySelector('.life-graph-home-refresh');
    assert.equal(refresh.getAttribute('disabled'), 'disabled');
    injectHomepageGraphCard(doc, readyState([run()]), noopHandlers());
    assert.equal(doc.querySelector('.life-graph-home-refresh').getAttribute('disabled'), null);
  });

  it('graph failure leaves the Loop homepage entry untouched', () => {
    const doc = homeDocument();
    const bar = doc.querySelector('.life-decision-bar');
    const match = bar.createDiv({ cls: 'life-decision-match' });
    let opened = 0;
    injectHomepageEntry(doc, () => { opened += 1; });
    injectHomepageGraphCard(doc, { kind: 'error', message: 'Graph 服务暂时不可用' }, noopHandlers());
    const entry = doc.querySelector('.life-loop-console-entry');
    assert.ok(entry, 'Loop entry renders regardless of Graph state');
    entry.click();
    assert.equal(opened, 1);
    assert.ok(doc.text.includes('Graph 服务暂时不可用'));
  });

  it('injects one independent card into every homepage copy', () => {
    const doc = new FakeEl('document');
    for (let copy = 0; copy < 2; copy += 1) {
      const home = doc.createDiv({ cls: 'my-life-homepage-view' });
      const content = home.createDiv({ cls: 'life-dashboard-content' });
      content.createDiv({ cls: 'life-decision-bar' });
    }
    assert.equal(injectHomepageGraphCard(doc, readyState([run()]), noopHandlers()), 2);
    assert.equal(doc.querySelectorAll('.life-graph-home').length, 2);
  });

  it('creates no timers and leaves no listeners behind after unload-style teardown', () => {
    const originalSetTimeout = globalThis.setTimeout;
    const originalSetInterval = globalThis.setInterval;
    let timerCalls = 0;
    globalThis.setTimeout = (...args) => { timerCalls += 1; return originalSetTimeout(...args); };
    globalThis.setInterval = (...args) => { timerCalls += 1; return originalSetInterval(...args); };
    try {
      const doc = homeDocument();
      const handlers = noopHandlers({});
      injectHomepageGraphCard(doc, readyState([run()]), handlers);
      injectHomepageGraphCard(doc, { kind: 'loading' }, handlers);
      doc.querySelector('.life-graph-home-open').click();
      doc.querySelector('.life-graph-home-refresh').click();
    } finally {
      globalThis.setTimeout = originalSetTimeout;
      globalThis.setInterval = originalSetInterval;
    }
    assert.equal(timerCalls, 0, 'the card schedules zero timers (no polling, no hidden async work)');
  });

  it('anchors the card right after the decision bar', () => {
    const doc = homeDocument();
    const content = doc.querySelector('.life-dashboard-content');
    injectHomepageGraphCard(doc, readyState([]), noopHandlers());
    const barIndex = content.children.findIndex((child) => child.classes.has('life-decision-bar'));
    const cardIndex = content.children.findIndex((child) => child.classes.has('life-graph-home'));
    assert.equal(cardIndex, barIndex + 1, 'card sits immediately after DECISION SUPPORT');
  });
});

// V1：Cosmos 摘要需要最近运行的合法 updated_at（六态面板与抽屉显示时间）；
// 该字段必须来自 selectLatestRun 的可靠时间候选，非法时间不产生 latest。
describe('graph summary latest.updatedAt (V1 cosmos projection)', () => {
  it('exposes the reliable updated_at on the latest run summary', () => {
    const summary = computeGraphSummary([
      run({ run_id: 'exec:timed', status: 'running', sequence: 2, updated_at: '2026-08-14T10:00:00+00:00' }),
    ]);
    assert.equal(summary.latest.updatedAt, '2026-08-14T10:00:00+00:00', '合法时间原样保留');
  });

  it('never fabricates updatedAt for unreliable candidates', () => {
    const invalid = run({ run_id: 'exec:bad', status: 'completed', sequence: 1, updated_at: 'not-a-time' });
    const summary = computeGraphSummary([invalid]);
    assert.equal(summary.latest, null, '非法时间不声明最近运行');
  });
});
