import { describe, it } from 'node:test';
import { effectiveScope } from './helpers/effective-scope.mjs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const { injectFragmentIntentCards, alignmentFor, subscriptionExecutionLabel, hasResearchConclusion } = require(path.join(ROOT, 'src', 'console', 'fragment-intent-card.js'));

function home() {
  const doc = new FakeEl('document');
  doc.body = doc.createDiv({ cls: 'body' });
  const root = doc.body.createDiv({ cls: 'my-life-homepage-view' });
  const card = root.createDiv({ cls: 'life-capture-card' });
  const footer = card.createEl('footer');
  const raw = footer.createEl('a', { text: '原始记录' });
  raw.setAttribute('data-path', '散记/碎片想法/fragment-1.md');
  const organized = footer.createEl('a', { text: '查看整理结果' });
  organized.setAttribute('data-path', 'AI创业/碎片整理/fragment-1.md');
  return { doc, card };
}

function organizedHome() {
  const doc = new FakeEl('document');
  doc.body = doc.createDiv({ cls: 'body' });
  const root = doc.body.createDiv({ cls: 'my-life-homepage-view' });
  const card = root.createEl('article', { cls: 'life-organized-card' });
  card.createEl('strong', { text: '历史碎片' });
  const actions = card.createDiv();
  const details = actions.createEl('a', { text: '详情 →' });
  details.setAttribute('data-href', 'AI创业/碎片整理/fragment-1');
  return { doc, card };
}

function alignment(overrides = {}) {
  return {
    alignment_id: 'align:1', fragment_id: 'fragment-1', title: '技术碎片',
    case_id: 'case:1', episode_id: 'episode:1',
    status: 'suggested', sequence: 1, revision: 1, input_digest: 'a'.repeat(64),
    reasoning: '先核实真实性与个人价值。',
    plan: '直接核实，复杂时再升级。', expected_result: '可信结论和下一步。',
    exclusions: [], suggested_intents: ['verify', 'evaluate_relevance'],
    dynamic_intents: [{ id: 'local_fit', label: '判断本机适用性', basis: '本地部署问题' }],
    recommended_route: 'verify', memory_basis: [{ label: '既有核验经验', maturity: 'reusable' }],
    execution_scope: { capabilities: ['核验'], external_scope: ['公开来源'], model_call_cap: 0, cost_cap_cny: 0, side_effect: 'none' },
    ...overrides,
  };
}

async function settle() {
  for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

function visibleDetailText(el) {
  const children = el.tagName === 'DETAILS' && !el.open && el.getAttribute('open') === null
    ? el.children.filter(child => child.tagName === 'SUMMARY') : el.children;
  return `${el.textContent} ${children.map(visibleDetailText).join(' ')}`;
}

describe('fragment intent homepage card', () => {
  it('offers one compact confirmation only after organized Loop input is ready', async () => {
    const { doc, card } = home();
    const calls = [];
    injectFragmentIntentCards(doc, { items: [], error: '' }, {
      isLoopApproved: () => true,
      onPropose: async (refs) => calls.push(refs.fragmentId),
      onDecide: () => {},
    });
    assert.ok(card.text.includes('整理已完成，可以先确认'));
    card.querySelector('.fragment-intent-start').click();
    await settle();
    assert.deepEqual(calls, ['fragment-1']);
  });

  it('renders multi-select, dynamic intent and supplement as text-only data', async () => {
    const { doc, card } = home();
    const item = alignment({ reasoning: '<img src=x onerror=alert(1)>' });
    const calls = [];
    injectFragmentIntentCards(doc, { items: [item], error: '' }, {
      isLoopApproved: () => true,
      onPropose: () => {},
      onDecide: async (_item, decision) => calls.push(decision),
    });
    assert.ok(card.text.includes('<img src=x onerror=alert(1)>'));
    assert.equal(card.querySelector('img'), null);
    assert.ok(card.text.includes('判断本机适用性'));
    const inputs = card.querySelectorAll('input');
    const local = inputs.find((input) => input.getAttribute('data-intent-id') === 'local_fit');
    local.checked = true;
    card.querySelector('.fragment-intent-supplement').value = '补充社区案例';
    card.querySelector('.fragment-intent-confirm').click();
    await settle();
    assert.deepEqual(calls[0].intents, ['verify', 'evaluate_relevance', 'local_fit']);
    assert.equal(calls[0].supplement, '补充社区案例');
  });

  it('shows the bound model and write scope before confirmation', () => {
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [alignment({
      execution_scope: {
        capabilities: ['公开来源发现', '安全网页抓取', '受治理研究合成'],
        external_scope: ['官方资料', 'GitHub、模型社区与可信技术社区'],
        model_call_cap: 1, cost_cap_cny: 2, side_effect: '只读，无外部写入',
        model_provider: 'deepseek', model_name: 'deepseek-v4-pro',
        write_scope: ['Loop Checkpoint 研究结果'],
      },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('模型 deepseek/deepseek-v4-pro'));
    assert.ok(card.text.includes('成本上限 ¥2'));
    assert.ok(card.text.includes('写入 Loop Checkpoint 研究结果'));
  });

  it('supports save-only and prevents a programmatic double click', async () => {
    const { doc, card } = home();
    const item = alignment();
    let calls = 0;
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    injectFragmentIntentCards(doc, { items: [item], error: '' }, {
      isLoopApproved: () => true,
      onPropose: () => {},
      onDecide: async (_item, decision) => {
        calls += 1;
        assert.equal(decision.action, 'save_only');
        await gate;
      },
    });
    const save = card.querySelector('.fragment-intent-save');
    save.click();
    save.click();
    await settle();
    assert.equal(calls, 1);
    release();
    await settle();
    assert.equal(save.getAttribute('disabled'), null);
  });

  it('shows confirmed execution state without technical ids', () => {
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [alignment({
      status: 'passed', route: 'direct',
      decision: { action: 'confirm', intents: ['learn'], supplement: '' },
      execution: { run_id: 'exec:secret-id', status: 'passed', updated_at: '2026-08-09T00:00:00Z' },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('直接处理'));
    assert.ok(card.text.includes('处理已结束'));
    assert.equal(card.text.includes('exec:secret-id'), false);
  });

  it('shows the newest continuation episode instead of an older suggested form', () => {
    const { doc, card } = home();
    const oldSuggested = alignment({ sequence: 10, alignment_id: 'align:old' });
    const childPassed = alignment({
      sequence: 20, alignment_id: 'align:child', status: 'passed', route: 'verify',
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      execution: {
        run_id: 'exec:child', status: 'passed', updated_at: '2026-08-10T00:00:00Z',
        result_digest: 'b'.repeat(64),
        result: {
          summary: '子 episode 已形成可信边界。', unknowns: ['仍需复杂验证'],
          next_checks: ['查看 Graph 提案'], needs_escalation: true,
          escalation_reason: '需要有界工作流', model_calls: 1, tool_calls: 2,
        },
      },
    });
    injectFragmentIntentCards(doc, { items: [oldSuggested, childPassed], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('子 episode 已形成可信边界'));
    assert.equal(card.text.includes('确认并继续'), false);
  });

  it('renders the safe execution result and next checks as text only', () => {
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [alignment({
      status: 'passed', route: 'verify',
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      execution: {
        run_id: 'exec:secret-id', status: 'passed', updated_at: '2026-08-09T00:00:00Z',
        result_digest: 'b'.repeat(64),
        result: {
          summary: '<img src=x onerror=alert(1)> 官方与社区来源候选已分开整理。',
          unknowns: ['尚未逐页核实'], next_checks: ['核对官方模型卡', '核对社区复现'],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 2,
        },
      },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('<img src=x onerror=alert(1)>'));
    assert.ok(card.text.includes('核对官方模型卡'));
    assert.ok(card.text.includes('尚未逐页核实'));
    assert.equal(card.querySelector('img'), null);
  });

  it('shows an evidence miss as unresolved instead of completed', () => {
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [alignment({
      status: 'passed', route: 'verify',
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      execution: {
        run_id: 'exec:secret-id', fragment_id: 'fragment-1', status: 'passed',
        current_node: 'harvest', route: 'verify', stop_reason: 'completed',
        updated_at: '2026-08-09T00:00:00Z', result_digest: 'b'.repeat(64),
        // legacy no_evidence：带明确 network_requests>0 + plan_exhausted=true
        // 才可投影为策略耗尽（rev5 硬规则）。
        research_progress: {
          stage: 'no_evidence', collected_sources: 0,
          plan_exhausted: true, network_requests: 3,
        },
        harvest: [{ role: 'failure', maturity: 'candidate', summary: '没有取得证据' }],
        result: {
          summary: '本次公开来源发现没有取得可复核结果。',
          unknowns: ['官方状态仍未核实'], next_checks: ['补充官方链接'],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 2,
        },
      },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('本轮未取得可核验证据'));
    assert.ok(card.text.includes('补充来源后继续核验'));
    assert.equal(card.text.includes('已完成'), false);
    assert.ok(card.querySelector('.fragment-intent-card').classes.has('is-unresolved'));
    assert.equal(card.querySelector('.fragment-intent-continuation').tagName, 'DETAILS');
  });

  it('legacy execution without progress never projects search_exhausted (rev5)', () => {
    // 无 research_progress 的 legacy failure harvest：无法证明任何网络请求，
    // 保守投影为能力离线——不显示「来源不足」、不渲染继续按钮。
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [alignment({
      status: 'passed', route: 'verify',
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      execution: {
        run_id: 'exec:legacy', fragment_id: 'fragment-1', status: 'passed',
        current_node: 'harvest', route: 'verify', stop_reason: 'completed',
        updated_at: '2026-08-09T00:00:00Z', result_digest: 'b'.repeat(64),
        harvest: [{ role: 'failure', maturity: 'candidate', summary: '没有取得证据' }],
        result: {
          summary: '本次公开来源发现没有取得可复核结果。',
          unknowns: ['官方状态仍未核实'], next_checks: [],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 0,
        },
      },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('系统待恢复'));
    assert.ok(card.text.includes('无需你补来源或重填目标'));
    assert.equal(card.querySelector('.fragment-intent-continue'), null, '不得渲染继续按钮');
    assert.equal(card.text.includes('来源不足'), false);
    assert.equal(card.text.includes('已按策略搜索未果'), false, '零请求不得显示策略耗尽');
  });

  it('capability_offline shows system recovery note instead of a continue button (TASK F G1)', () => {
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [alignment({
      status: 'passed', route: 'verify',
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      execution: {
        run_id: 'exec:offline', fragment_id: 'fragment-1', status: 'passed',
        current_node: 'harvest', route: 'verify', stop_reason: 'completed',
        updated_at: '2026-08-09T00:00:00Z', result_digest: 'b'.repeat(64),
        research_progress: {
          cognitive: 'capability_offline', stage: 'collected',
          collected_sources: 0, stop_reason: 'live_disabled', network_requests: 0,
        },
        harvest: [{ role: 'failure', maturity: 'candidate', summary: '没有取得证据' }],
        result: {
          summary: '本轮未取得可核验证据。',
          unknowns: ['本轮未取得可核验证据，不能据此判断原始说法为真。'],
          next_checks: ['稍后重试公开来源发现。'],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 0,
        },
      },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    // 能力关闭 = 系统待恢复：诚实说明 + 无继续按钮、无「来源不足」、无补来源要求。
    assert.ok(card.text.includes('系统待恢复'));
    assert.ok(card.text.includes('无需你补来源或重填目标'));
    assert.equal(card.querySelector('.fragment-intent-continue'), null, '能力关闭不得渲染继续按钮');
    assert.equal(card.text.includes('来源不足'), false);
  });

  it('continues from a completed result and exposes Graph only when proposed', async () => {
    const { doc, card } = home();
    const calls = [];
    injectFragmentIntentCards(doc, { items: [alignment({
      status: 'passed', route: 'verify',
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      execution: {
        run_id: 'exec:secret', status: 'passed', updated_at: '2026-08-09T00:00:00Z',
        result_digest: 'b'.repeat(64), graph_escalation: { status: 'proposed' },
      },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
      onContinue: async (_item, goal) => calls.push(['continue', goal]),
      onEscalate: async () => calls.push(['escalate']),
    });
    const goal = card.querySelector('.fragment-intent-continuation textarea');
    goal.value = '继续处理新的技术问题';
    card.querySelector('.fragment-intent-continue').click();
    card.querySelector('.fragment-intent-escalate').click();
    await settle();
    assert.deepEqual(calls, [
      ['continue', '继续处理新的技术问题'], ['escalate'],
    ]);
  });

  it('shows an honest Graph proposal stop when no workflow template matches', () => {
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [alignment({
      status: 'passed', route: 'graph',
      decision: { action: 'confirm', intents: ['plan_action'], supplement: '' },
    })], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('没有匹配的工作流模板'));
    assert.ok(card.text.includes('没有创建 Run'));
  });

  it('does not appear for unorganized or non-Loop fragments', () => {
    const first = home();
    first.card.querySelectorAll('a').find((a) => a.text === '查看整理结果').remove();
    assert.equal(injectFragmentIntentCards(first.doc, { items: [] }, {
      isLoopApproved: () => true,
    }), 0);
    const second = home();
    assert.equal(injectFragmentIntentCards(second.doc, { items: [] }, {
      isLoopApproved: () => false,
    }), 0);
  });

  it('keeps an older organized fragment actionable when it is no longer in today captures', () => {
    const { doc, card } = organizedHome();
    injectFragmentIntentCards(doc, { items: [alignment()], error: '' }, {
      isLoopApproved: (rawRef) => rawRef.endsWith('/fragment-1.md'),
      onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('我理解你想这样推进'));
  });

  it('uses the template source fragment identity for an older organized card', () => {
    const { doc, card } = organizedHome();
    card.setAttribute('data-source-fragment', 'trusted-source-fragment');
    const current = alignment({
      alignment_id: 'align:current',
      fragment_id: 'trusted-source-fragment',
      status: 'passed',
      sequence: 7,
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      route: 'verify',
      execution: {
        status: 'passed', updated_at: '2026-08-10T00:00:00Z',
        result_digest: 'b'.repeat(64),
        result: { summary: '已完成可信核验。', unknowns: [], next_checks: [] },
      },
    });
    injectFragmentIntentCards(doc, { items: [alignment(), current], error: '' }, {
      isLoopApproved: (rawRef) => rawRef.endsWith('/trusted-source-fragment.md'),
      onPropose: () => {}, onDecide: () => {},
    });
    assert.ok(card.text.includes('处理方向已确认'));
    assert.equal(card.text.includes('确认并继续'), false);
  });

  it('does not duplicate one alignment when the raw and organized cards are both visible', () => {
    const first = home();
    const organized = first.doc.querySelector('.my-life-homepage-view').createEl('article', { cls: 'life-organized-card' });
    const details = organized.createEl('a', { text: '详情 →' });
    details.setAttribute('data-href', 'AI创业/碎片整理/fragment-1');
    const injected = injectFragmentIntentCards(first.doc, { items: [alignment()], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });
    assert.equal(injected, 1);
    assert.equal(organized.querySelector('.fragment-intent-card'), null);
  });

  it('uses the capture raw identity when an organized output has a different filename', () => {
    const first = home();
    const captureOrganized = first.card.querySelectorAll('a').find((a) => a.text === '查看整理结果');
    captureOrganized.setAttribute('data-path', 'AI创业/碎片整理/minimax-h3-research.md');
    const organized = first.doc.querySelector('.my-life-homepage-view').createEl('article', { cls: 'life-organized-card' });
    const details = organized.createEl('a', { text: '详情 →' });
    details.setAttribute('data-href', 'AI创业/碎片整理/minimax-h3-research');
    const stale = organized.createEl('section', { cls: 'fragment-intent-card' });
    stale.setAttribute('data-fragment-intent-card', 'old-plugin-instance');

    const injected = injectFragmentIntentCards(first.doc, { items: [alignment()], error: '' }, {
      isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
    });

    assert.equal(injected, 1);
    assert.equal(organized.querySelector('.fragment-intent-card'), null);
    assert.ok(first.card.text.includes('我理解你想这样推进'));
  });
});


it("disabled synthesis is a system blocker without a replacement-goal form", () => {
  const { doc, card } = home();
  const item = alignment({ status: 'passed', route: 'verify', execution: {
    status: 'passed', route: 'verify', result_digest: 'b'.repeat(64), harvest: [],
    research_progress: { cognitive: 'evidence_ready', stage: 'synthesis_disabled', collected_sources: 1 },
  }});
  injectFragmentIntentCards(doc, { items: [item], error: '' }, {
    isLoopApproved: () => true, onPropose: () => {}, onDecide: () => {},
  });
  assert.ok(card.text.includes('综合判断能力未启用'));
  assert.equal(card.text.includes('证据已足够'), false);
  assert.equal(card.querySelector('.fragment-intent-continuation'), null);
});


describe('automatic knowledge publication', () => {
  const publication = () => ({ knowledge_id: `knowledge-${'c'.repeat(24)}`, revision: 2,
    path: 'Loop知识库/Archify/00000002/note.md', result_digest: 'b'.repeat(64), publication_source: 'system_policy' });
  const completed = () => alignment({ status: 'passed', route: 'verify', execution: {
    run_id: 'exec:publication', status: 'passed', route: 'verify', result_digest: 'b'.repeat(64),
    updated_at: '2026-09-08T12:00:00Z',
    research_progress: { stage: 'synthesized', cognitive: 'synthesized', collected_sources: 2, model_calls: 1 },
    harvest: [{ role: 'pattern', summary: '旧候选', maturity: 'candidate' }],
    graph_escalation: { status: 'proposed' },
    result: { summary: '可限定使用', answer_markdown: '# 完整判断\n边界仍保留', unknowns: ['动态映射未验证'],
      limitations: ['只验证当前版本'], next_checks: [], needs_escalation: true, model_calls: 1, tool_calls: 3 },
  } });
  it('keeps the conclusion, limitations and conflicts visible while progressively disclosing completed execution detail', () => {
    const { doc, card } = home();
    const item = completed();
    item.effective_execution_scope = effectiveScope({ observed_model_provider: 'kimi_subscription' });
    item.execution.knowledge_publication = publication();
    item.execution.subscription_selected = true;
    item.execution.research_progress = { ...item.execution.research_progress, model_provider: 'kimi_subscription', agent_invocations: 3 };
    item.execution.result.conflicts = [{ topic: '成本口径不同', dimensions: ['供应商自评', '外部实测'] }];
    injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true, onOpenKnowledge: async () => {} });
    const visible = visibleDetailText(card);
    for (const text of ['可限定使用', '动态映射未验证', '只验证当前版本', '成本口径不同', '打开已沉淀知识', '证据与来源']) assert.ok(visible.includes(text), text);
    for (const text of ['exec:publication', '成本上限', '已观察执行', '# 完整判断', '技术碎片']) assert.equal(visible.includes(text), false, text);
    const receipt = card.querySelector('.fragment-execution-details');
    assert.ok(receipt.text.includes('Kimi 月套餐'));
    assert.ok(card.querySelector('.fragment-research-technical').text.includes('exec:publication'));
    assert.equal(card.querySelector('textarea'), null);
  });
  it('does not collapse a current system blocker just because a saved result exists', () => {
    const { doc, card } = home(); const item = completed();
    item.execution.knowledge_publication = publication();
    item.execution.research_progress = { stage: 'synthesis_disabled', blocker: 'synthesis_disabled', collected_sources: 2, note: '当前处理仍受系统能力限制' };
    injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true });
    assert.ok(visibleDetailText(card).includes('当前处理仍受系统能力限制'));
    assert.equal(card.querySelector('.fragment-execution-details'), null);
    assert.equal(card.querySelector('.fragment-research-technical'), null);
  });
  it('keeps unavailable publication reasons and review failures outside closed details', () => {
    for (const failedReview of [false, true]) {
      const { doc, card } = home(); const item = completed();
      item.execution.knowledge_publication = { ...publication(), status: 'unavailable', reason: '保存摘要冲突需保留原文件' };
      if (failedReview) item.execution.research_progress = { stage: 'independent_review_failed', note: '引用核对没有通过' };
      injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true });
      assert.ok(visibleDetailText(card).includes(failedReview ? '引用核对没有通过' : '保存摘要冲突需保留原文件'));
      assert.equal(card.querySelector('.fragment-execution-details'), null);
    }
  });
  it('published results keep limitations, open the exact Vault revision and suppress candidate/Graph/goal prompts', async () => {
    const { doc, card } = home();
    const item = completed(); item.execution.knowledge_publication = publication();
    const opened = [];
    injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true,
      onOpenKnowledge: async (path) => opened.push(path) });
    assert.ok(card.text.includes('结论已自动沉淀'));
    assert.ok(card.text.includes('未经过人工审核'));
    assert.ok(card.text.includes('完整判断'));
    assert.ok(card.text.includes('动态映射未验证'));
    assert.ok(card.text.includes('只验证当前版本'));
    assert.equal(card.querySelector('.fragment-harvest-candidates'), null);
    assert.equal(card.querySelector('textarea'), null);
    assert.equal(card.querySelector('.fragment-intent-escalate'), null);
    card.querySelector('.fragment-intent-knowledge').click(); await settle();
    assert.deepEqual(opened, [publication().path]);
  });
  it('an unavailable saved note keeps the answer and error reason without claiming publication', () => {
    const { doc, card } = home();
    const item = completed();
    item.execution.knowledge_publication = { ...publication(), status: 'unavailable', reason: '笔记与保存时的摘要不一致' };
    injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true });
    assert.ok(card.text.includes('已存笔记发生变动或无法读取'));
    assert.ok(card.text.includes('笔记与保存时的摘要不一致'));
    assert.ok(card.text.includes('完整判断'));
    assert.equal(card.text.includes('结论已自动沉淀'), false);
    assert.equal(card.querySelector('textarea'), null);
    assert.equal(card.querySelector('.fragment-intent-knowledge'), null);
  });
  it('a later publication receipt rerenders the existing result even if execution timestamp is unchanged', () => {
    const { doc, card } = home();
    const item = completed();
    const handlers = { isLoopApproved: () => true, onOpenKnowledge: async () => {} };
    injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers);
    assert.ok(card.text.includes('尚无本次保存回执'));
    assert.equal(card.querySelector('textarea'), null);
    item.execution.knowledge_publication = publication();
    assert.equal(injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers), 1);
    assert.ok(card.text.includes('结论已自动沉淀'));
    assert.ok(card.querySelector('.fragment-intent-knowledge'));
  });
  it('a newly projected research digest updates a pre-existing receipt without changing timestamp or control digest', () => {
    const { doc, card } = home(); const item = completed();
    const handlers = { isLoopApproved: () => true };
    item.execution.knowledge_publication = { ...publication(), result_digest: 'c'.repeat(64) };
    injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers);
    assert.ok(card.text.includes('尚无本次保存回执'));
    item.execution.research_result_digest = 'c'.repeat(64);
    assert.equal(injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers), 1);
    assert.ok(card.text.includes('结论已自动沉淀'));
    assert.equal(item.execution.result_digest, 'b'.repeat(64));
    item.execution.knowledge_publication.status = 'unavailable';
    item.execution.knowledge_publication.reason = '用户笔记已变动';
    injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers);
    assert.ok(card.text.includes('用户笔记已变动'));
    assert.equal(card.text.includes('结论已自动沉淀'), false);
  });
  it('an old publication cannot label a changed result as saved or hide its full answer', () => {
    const { doc, card } = home();
    const item = completed();
    item.execution.knowledge_publication = { ...publication(), result_digest: 'd'.repeat(64) };
    injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true });
    assert.equal(card.text.includes('结论已自动沉淀'), false);
    assert.ok(card.text.includes('完整判断'));
    assert.ok(card.text.includes('尚无本次保存回执'));
    assert.equal(card.querySelector('textarea'), null);
  });
});


describe('recovery lineage and actual subscription scope', () => {
  const parent = (execution = {}) => alignment({ alignment_id: 'align:parent', sequence: 10,
    status: 'passed', route: 'verify', execution: { run_id: 'exec:parent', status: 'running',
      result_digest: 'b'.repeat(64), research_progress: { stage: 'researching', cognitive: 'collecting', collected_sources: 1, model_calls: 0 }, ...execution } });
  const child = (overrides = {}) => alignment({ alignment_id: 'align:recovery', sequence: 20,
    parent_run_id: 'exec:parent', recovery_reason: 'offline_zero_evidence', status: 'passed', route: 'verify',
    execution: { run_id: 'exec:recovery', status: 'passed', research_progress: { stage: 'synthesis_disabled', cognitive: 'evidence_ready', collected_sources: 1, model_calls: 0 } }, ...overrides });
  const conclusion = { status: 'passed', result: { summary: '父任务的完整结论', unknowns: [], next_checks: [] },
    research_progress: { stage: 'synthesized', cognitive: 'synthesized', collected_sources: 2, model_calls: 1 } };
  const authorization = { source: 'user_authorized_subscription', run_id: 'exec:parent',
    provider: 'kimi_subscription', max_agent_invocations: 4, max_tools: 16 };

  it('selects the concluded parent over its unfinished recovery child, independent of API order', () => {
    const original = parent(conclusion), recovery = child();
    for (const items of [[original, recovery], [recovery, original]]) assert.equal(alignmentFor(items, 'fragment-1'), original);
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [original, recovery], error: '' }, { isLoopApproved: () => true });
    assert.ok(card.text.includes('父任务的完整结论'));
    assert.equal(card.text.includes('综合判断能力未启用'), false);
  });

  it('selects the exact parent currently authorized for subscription without pretending it completed', () => {
    const original = parent({ subscription_selected: true });
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [child(), original], error: '' }, { isLoopApproved: () => true });
    assert.equal(alignmentFor([child(), original], 'fragment-1'), original);
    assert.ok(card.text.includes('本轮已纳入套餐研究范围'));
    assert.equal(card.text.includes('已形成结论'), false);
  });

  it('does not let old success or subscription hide a real new user goal', () => {
    const original = parent({ ...conclusion, subscription_selected: true });
    for (const overrides of [{ recovery_reason: undefined }, { recovery_reason: 'unknown_recovery' }]) {
      const newGoal = child(overrides);
      assert.equal(alignmentFor([newGoal, original], 'fragment-1'), newGoal);
    }
    const newGoal = child({ alignment_id: 'align:user', sequence: 30, recovery_reason: undefined, parent_run_id: 'exec:recovery' });
    assert.equal(alignmentFor([original, newGoal, child()], 'fragment-1'), newGoal);
  });

  it('keeps a recovery child with its own complete conclusion and refuses unrelated or unavailable parents', () => {
    const original = parent({ ...conclusion, subscription_selected: true });
    const finished = child({ execution: { ...conclusion, run_id: 'exec:recovery', result_digest: 'c'.repeat(64) } });
    assert.equal(alignmentFor([original, finished], 'fragment-1'), finished);
    for (const items of [[], [parent()], [{ ...original, fragment_id: 'different' }]]) {
      const recovery = child();
      assert.equal(alignmentFor([...items, recovery], 'fragment-1'), recovery);
    }
  });

  it('handles repeated marked recovery and rejects cycles without mutating history', () => {
    const original = parent(conclusion), first = child();
    const second = child({ alignment_id: 'align:second', sequence: 30, parent_run_id: 'exec:recovery', execution: { ...first.execution, run_id: 'exec:second' } });
    const items = [second, original, first];
    assert.equal(alignmentFor(items, 'fragment-1'), original);
    assert.deepEqual(items.map(item => item.alignment_id), ['align:second', 'align:parent', 'align:recovery']);
    const cyclic = { ...first, parent_run_id: 'exec:second' };
    assert.equal(alignmentFor([second, cyclic], 'fragment-1'), second);
  });

  it('distinguishes current allowed capabilities from historical provider, observed calls and unpublished results', () => {
    const item = parent({ ...conclusion, route: 'verify', subscription_selected: true,
      subscription_authorization: { ...authorization, provider: 'codex_subscription' },
      result: { ...conclusion.result, model_calls: 2, tool_calls: 3 },
      research_progress: { ...conclusion.research_progress, model_calls: 2, agent_invocations: 2 } });
    item.execution_scope = { ...item.execution_scope, model_provider: 'deepseek', model_name: 'old-default', cost_cap_cny: 2 };
    item.effective_execution_scope = effectiveScope();
    const { doc, card } = home();
    const handlers = { isLoopApproved: () => true };
    injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers);
    assert.ok(card.text.includes('当前配置允许：Kimi 月套餐（能力范围）'));
    assert.ok(card.text.includes('执行授权回执：Codex 月套餐'));
    assert.ok(card.text.includes('Agent 调用槽位上限 10 次（含未知调用与模型复核）'));
    assert.ok(card.text.includes('已记录 2 次模型调用'));
    assert.ok(card.text.includes('是否已经试跑或保存，以实际结果和回执为准'));
    assert.ok(card.text.includes('当前排除：不启用新增付费 API；不自动部署到用户系统'));
    assert.equal(card.text.includes('结论已自动沉淀'), false);
    assert.equal(card.text.includes('deepseek'), false); assert.equal(card.text.includes('¥2'), false);
    item.effective_execution_scope = effectiveScope({ model_call_cap: 12 });
    injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers);
    assert.ok(card.text.includes('槽位上限 12 次'), 'configuration changes repaint even without timestamp changes');
    item.execution.subscription_selected = false;
    injectFragmentIntentCards(doc, { items: [item], error: '' }, handlers);
    assert.equal(card.querySelector('.fragment-effective-scope'), null, 'historical tasks are not promoted to current Kimi selection');
    assert.ok(card.text.includes('执行授权回执：Codex 月套餐'));
  });

  it('keeps actual Kimi progress when the prospective run has no separate authorization receipt', () => {
    // MarkupSafe production projection: passed/r2, three observed calls, authorization=null.
    const execution = { run_id: 'exec:fragment-intent:273adaabf05f9aa01859e70c', status: 'passed',
      subscription_selected: true, subscription_authorization: null,
      research_progress: { stage: 'synthesized', provider: 'kimi_subscription',
        model_calls: 3, agent_invocations: 3, model_calls_unknown: false } };
    const label = subscriptionExecutionLabel(execution);
    assert.match(label, /Kimi 月套餐/); assert.match(label, /已观察 3 次 Agent 调用/);
    assert.match(label, /3 次模型调用/); assert.doesNotMatch(label, /尚无实际执行回执|调用上限/);
    const historical = subscriptionExecutionLabel({ ...execution, subscription_selected: false,
      effective_execution_scope: effectiveScope(), research_progress: { ...execution.research_progress,
        provider: 'codex_subscription', model_calls: 5, agent_invocations: 6, model_calls_unknown: true } });
    assert.match(historical, /Codex 月套餐/); assert.doesNotMatch(historical, /Kimi 月套餐/);
    assert.match(historical, /已观察 6 次 Agent 调用/); assert.match(historical, /5 次模型调用/);
    assert.match(historical, /尚未完全确认/);
    const configured = subscriptionExecutionLabel({ ...execution, research_progress: null });
    assert.match(configured, /尚无实际执行回执/); assert.doesNotMatch(configured, /已观察 3/);
  });

  it('shows only the bound actual subscription authorization and distinguishes caps from observed use', () => {
    const original = parent({ subscription_authorization: authorization,
      research_progress: { stage: 'researching', collected_sources: 1, model_calls: 1, model_calls_unknown: true, agent_invocations: 1 } });
    original.execution_scope = { ...original.execution_scope, model_provider: 'deepseek', model_name: 'old-default', cost_cap_cny: 2 };
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [original], error: '' }, { isLoopApproved: () => true });
    assert.ok(card.text.includes('执行授权回执：Kimi 月套餐'));
    assert.ok(card.text.includes('Agent 调用上限 4 次，工具调用上限 16 次'));
    assert.ok(card.text.includes('已观察 1 次 Agent 调用，1 次模型调用（调用次数尚未完全确认）'));
    assert.equal(card.text.includes('deepseek'), false);
    assert.equal(card.text.includes('¥2'), false);
    assert.equal(subscriptionExecutionLabel({ run_id: 'exec:other', subscription_authorization: authorization }), '');
    assert.equal(subscriptionExecutionLabel({ run_id: 'exec:parent', subscription_authorization: { ...authorization, provider: 'deepseek' } }), '');
    original.execution.subscription_authorization = { ...authorization, provider: 'codex_subscription' };
    injectFragmentIntentCards(doc, { items: [original], error: '' }, { isLoopApproved: () => true });
    assert.ok(card.text.includes('执行授权回执：Codex 月套餐'), 'receipt changes repaint even without execution timestamp changes');
  });
});


describe('independent evidence review presentation', () => {
  const review = () => ({ policy_version: 'independent-evidence-review-v1', draft_digest: 'd'.repeat(64),
    reviewed_result_digest: 'e'.repeat(64), reviewed_at: '2026-09-08T12:00:00Z', verdict: 'revised',
    reason: '现有材料不支持替代人工系统的断言', findings: [{ statement: '<img src=x onerror=alert(1)>',
      issue: 'overconfident', correction: '限定到已读材料支持的范围', evidence_ids: ['ev-1'] }] });
  const historical = (stage) => alignment({ status: 'passed', route: 'verify', execution: {
    run_id: 'exec:review', status: stage === 'independent_review_in_progress' ? 'running' : 'blocked', route: 'verify',
    result_digest: 'b'.repeat(64), result: { summary: '历史第一版结论', answer_markdown: '# 历史正文', unknowns: [], next_checks: [] },
    research_progress: { stage, cognitive: 'synthesized', collected_sources: 1, model_calls: 2 },
    graph_escalation: { status: 'proposed' }, harvest: [{ role: 'pattern', summary: '历史候选', maturity: 'candidate' }],
    knowledge_publication: { knowledge_id: `knowledge-${'c'.repeat(24)}`, revision: 1, path: 'Loop知识库/v1.md',
      result_digest: 'b'.repeat(64), publication_source: 'system_policy' },
  }});
  for (const [stage, label] of [['independent_review_failed', '证据复核未完成'], ['independent_review_unknown', '证据复核回执未知'],
    ['independent_review_limit', '证据复核达到调用上限'], ['independent_review_in_progress', '证据复核进行中']]) {
    it(`${stage} preserves history but cannot complete or request human approval`, () => {
      const { doc, card } = home();const item = historical(stage);
      injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true });
      assert.ok(card.text.includes(label));
      assert.ok(card.querySelector('.fragment-review-history').text.includes('历史正文'));
      assert.equal(card.querySelector('textarea'), null);
      assert.equal(card.querySelector('.fragment-harvest-candidates'), null);
      assert.equal(card.text.includes('结论已自动沉淀'), false);
      assert.equal(card.text.includes('创建 Graph'), false);
      assert.equal(hasResearchConclusion(item.execution), false);
      assert.equal(hasResearchConclusion({ ...item.execution, status: 'passed' }), false, 'stale passed/result flags cannot override review status');
    });
  }
  it('shows structured model-review findings safely and uses the dynamic six-invocation cap', () => {
    const { doc, card } = home();const item = historical('synthesized');
    item.execution.status = 'passed'; item.execution.independent_review = review();
    item.execution.subscription_authorization = { source: 'user_authorized_subscription', run_id: item.execution.run_id,
      provider: 'codex_subscription', max_agent_invocations: 6, max_tools: 16 };
    injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true });
    assert.ok(card.text.includes('Agent 调用上限 6 次'));
    assert.ok(card.text.includes('证据复核（模型）'));
    assert.ok(card.text.includes('未经过人工审核'));
    assert.ok(card.text.includes('不代表已完成外部复现或事实证实'));
    assert.ok(card.text.includes('限定到已读材料支持的范围'));
    assert.ok(card.text.includes('<img src=x onerror=alert(1)>'));
    assert.equal(card.querySelector('img'), null);
    item.execution.independent_review.reason = '更新的复核理由';
    injectFragmentIntentCards(doc, { items: [item], error: '' }, { isLoopApproved: () => true });
    assert.ok(card.text.includes('更新的复核理由'), 'new review receipt must repaint even with unchanged execution timestamp');
  });
});
