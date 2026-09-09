import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

// Fragment Governed Research v1 包 D：主页产品面卡片渲染专项。
// 标注对应包 F 产品验收项编号（12 项清单中属于前端的项）。

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const { injectFragmentIntentCards } = require(path.join(ROOT, 'src', 'console', 'fragment-intent-card.js'));

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

function researchExecution(overrides = {}) {
  return {
    run_id: 'exec:fragment-intent:0123456789abcdef01234567',
    fragment_id: 'fragment-1',
    status: 'passed',
    current_node: 'harvest',
    route: 'verify',
    stop_reason: 'completed',
    updated_at: '2026-08-09T00:00:00Z',
    result_digest: 'b'.repeat(64),
    harvest: [],
    research_progress: {
      stage: 'synthesized',
      collected_sources: 2,
      stop_reason: 'evidence_sufficient',
      model_calls: 1,
      network_requests: 3,
    },
    research_evidence: [
      {
        title: '官方发布页', url: 'https://example.com/release',
        marker: 'newly_collected', source_target: 'official_docs',
        evidence_id: 'ev-official', evidence_digest: 'c'.repeat(64),
      },
      {
        title: '社区讨论串', url: 'https://community.example.org/thread/1',
        marker: 'inherited', evidence_id: 'ev-community',
      },
    ],
    result: {
      summary: '示例产品已发布。',
      unknowns: ['性能未知'],
      next_checks: [],
      needs_escalation: false,
      escalation_reason: '',
      model_calls: 1,
      tool_calls: 3,
      confirmed: [{ claim: '已发布', evidence_ids: ['ev-official'] }],
      conflicts: [{
        topic: '发布日期',
        dimensions: ['官方写 8 月 1 日', '社区写 7 月 30 日'],
        evidence_ids: ['ev-official', 'ev-community'],
      }],
      recommendation: '可查阅发布页。',
      claims: [{ claim: '已发布', evidence_id: 'ev-official', relation: 'supports' }],
    },
    ...overrides,
  };
}

function proposalMaterial(overrides = {}) {
  return {
    status: 'proposed',
    reason: '证据缺口需要受治理研究升级',
    source_result_digest: 'b'.repeat(64),
    spec_id: 'fragment-research-escalation-v1',
    spec_digest: 'd'.repeat(64),
    evidence_bundle_digest: 'e'.repeat(64),
    escalation_id: `escalation:${'f'.repeat(24)}`,
    ...overrides,
  };
}

function alignment(overrides = {}) {
  return {
    alignment_id: 'align:1', fragment_id: 'fragment-1', title: '核验示例产品是否已公开发布',
    case_id: 'case:1', episode_id: 'episode:1',
    status: 'passed', sequence: 1, revision: 1, input_digest: 'a'.repeat(64),
    reasoning: '先核实真实性。', plan: '先核实再升级。', expected_result: '可信结论。',
    exclusions: [], suggested_intents: ['verify'], dynamic_intents: [],
    recommended_route: 'verify', memory_basis: [],
    route: 'verify',
    decision: { action: 'confirm', intents: ['verify'], supplement: '关注硬件要求' },
    execution_scope: {
      capabilities: ['核验'], external_scope: ['公开来源'],
      model_call_cap: 1, cost_cap_cny: 1, side_effect: 'none',
    },
    ...overrides,
  };
}

function inject(doc, items, handlers = {}) {
  return injectFragmentIntentCards(doc, { items, error: '' }, {
    isLoopApproved: () => true,
    onPropose: () => {},
    onDecide: () => {},
    onContinue: () => {},
    onEscalate: () => {},
    ...handlers,
  });
}

async function settle() {
  for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

describe('fragment research result card (包 F 产品验收 #8/#10/#11)', () => {
  it('[F#8] renders all nine frozen sections with the DESIGN §9 mapping', () => {
    const { doc, card } = home();
    inject(doc, [alignment({ execution: researchExecution() })]);
    const labels = [...card.querySelectorAll('.fragment-research-label')].map((el) => el.text);
    assert.deepEqual(labels, [
      '研究目标', '结论摘要', '已确认', '仍未知', '来源冲突',
      '建议行动', '预算与停止原因', '后续入口',
    ]);
    const box = card.querySelector('.fragment-research-result');
    assert.ok(box);
    // 映射正确：每个值落在它该在的段里。
    const sectionText = (label) => {
      const section = [...card.querySelectorAll('.fragment-research-section')]
        .find((el) => el.querySelector('.fragment-research-label').text === label);
      return section ? section.text : '';
    };
    assert.ok(sectionText('研究目标').includes('核验示例产品是否已公开发布'));
    assert.ok(sectionText('研究目标').includes('关注硬件要求'));
    assert.ok(sectionText('结论摘要').includes('示例产品已发布。'));
    assert.ok(sectionText('已确认').includes('已发布'));
    assert.ok(sectionText('仍未知').includes('性能未知'));
    assert.ok(sectionText('来源冲突').includes('发布日期'));
    assert.ok(sectionText('来源冲突').includes('官方写 8 月 1 日'));
    assert.ok(sectionText('建议行动').includes('可查阅发布页。'));
    assert.ok(sectionText('预算与停止原因').includes('模型调用 1 次'));
    assert.ok(sectionText('预算与停止原因').includes('原始提案上限：模型 1 次'));
    assert.ok(sectionText('预算与停止原因').includes('不代表本轮实际授权或已发生费用'));
    assert.ok(sectionText('预算与停止原因').includes('网络请求 3 次'));
    assert.ok(sectionText('预算与停止原因').includes('复用已有候选材料，结论另行核验'));
    // 运行中紧凑进度：当前在做什么 + 已取得多少有效来源。
    const progress = card.querySelector('.fragment-research-progress');
    assert.ok(progress.text.includes('已形成研究结论'));
    assert.ok(progress.text.includes('已取得 2 个候选来源'));
    // [F#11] 后续入口：可以从结果继续新问题。
    assert.ok(sectionText('后续入口').includes('尚未取得本次知识保存回执'));
    assert.equal(card.querySelector('textarea'), null, '已形成结论不把保存状态交回用户重填目标');
  });

  it('[F#8] keeps sources collapsed by default and isolates technical ids in the audit area', () => {
    const { doc, card } = home();
    inject(doc, [alignment({ execution: researchExecution() })]);
    const sources = card.querySelector('.fragment-research-sources');
    assert.equal(sources.tagName, 'DETAILS');
    assert.equal(sources.getAttribute('open'), null);
    assert.ok(sources.text.includes('证据与来源'));
    assert.ok(sources.text.includes('官方发布页'));
    assert.ok(sources.text.includes('https://example.com/release'));
    assert.ok(sources.text.includes('本轮新收集'));
    assert.ok(sources.text.includes('继承自之前研究'));
    assert.ok(sources.text.includes('已发布（支持）'));
    const audit = card.querySelector('.fragment-intent-audit');
    assert.equal(audit.tagName, 'DETAILS');
    assert.equal(audit.getAttribute('open'), null);
    assert.ok(audit.text.includes('exec:fragment-intent:0123456789abcdef01234567'));
    assert.ok(audit.text.includes('c'.repeat(64)));
    assert.ok(audit.text.includes('ev-official'));
    // 审计区之外绝不出现技术 ID / digest / 节点名。
    const box = card.querySelector('.fragment-research-result');
    const outside = box.children
      .filter((child) => child !== audit)
      .map((child) => child.text)
      .join('');
    assert.equal(outside.includes('c'.repeat(64)), false);
    assert.equal(outside.includes('ev-official'), false);
    assert.equal(outside.includes('harvest'), false);
    assert.equal(outside.includes('exec:fragment-intent'), false);
  });

  it('[F#8] degrades honestly when the service is unavailable (降级态一：服务不可用)', () => {
    const { doc, card } = home();
    injectFragmentIntentCards(doc, { items: [], error: '处理方向服务暂不可用' }, {
      isLoopApproved: () => true,
    });
    assert.ok(card.text.includes('处理方向暂不可用'));
    assert.equal(card.querySelector('.fragment-research-result'), null);
  });

  it('[F#8] degrades honestly when no verifiable evidence was collected (降级态二：来源不足)', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      execution: researchExecution({
        research_progress: {
          stage: 'no_evidence', collected_sources: 0,
          stop_reason: 'not_found', model_calls: 0, network_requests: 1,
          note: '本轮未取得可核验证据。',
        },
        research_evidence: [],
        harvest: [{ role: 'failure', maturity: 'candidate', summary: '没有取得证据' }],
        result: {
          summary: '本轮未取得可核验证据。',
          unknowns: ['本轮未取得可核验证据，不能据此判断原始说法为真。'],
          next_checks: ['稍后重试公开来源发现，或补充一个明确的官方／仓库链接。'],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 1,
        },
      }),
    })]);
    assert.ok(card.text.includes('本轮未取得可核验证据'));
    const sectionText = (label) => [...card.querySelectorAll('.fragment-research-section')]
      .find((el) => el.querySelector('.fragment-research-label').text === label).text;
    assert.ok(sectionText('已确认').includes('本轮未取得可核验证据'));
    assert.ok(sectionText('来源冲突').includes('本轮未进行来源比对'));
    assert.ok(sectionText('预算与停止原因').includes('未找到可用来源'));
    // 诚实态：不得出现编造的结论或来源。
    assert.equal(card.text.includes('各来源之间未发现冲突'), false);
    assert.ok(card.querySelector('.fragment-research-sources').text.includes('本轮没有可展示的证据记录'));
  });

  it('[F#8] degrades honestly when synthesis needs authorization (降级态三：模型未授权)', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      execution: researchExecution({
        research_progress: {
          stage: 'awaiting_authorization', collected_sources: 2,
          stop_reason: null, model_calls: 0, network_requests: 3,
          note: '已收集来源但尚未形成可靠判断。',
        },
        result: {
          summary: '已收集 2 个来源，已收集来源但尚未形成可靠判断（需要补充授权）。',
          unknowns: ['具体主张仍需逐项核对来源原文。'],
          next_checks: ['https://example.com/release'],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 3,
        },
      }),
    })]);
    const progress = card.querySelector('.fragment-research-progress');
    // 新契约（TASK F）：awaiting_model_authorization →「需要你的决定（模型授权）」。
    assert.ok(progress.text.includes('需要你的决定（模型授权）'));
    // 「需要补充授权」状态附带完整安全材料。
    assert.ok(progress.text.includes('待授权范围：模型最多 1 次 · 成本上限 ¥1 · none'));
    const confirmed = [...card.querySelectorAll('.fragment-research-section')]
      .find((el) => el.querySelector('.fragment-research-label').text === '已确认');
    assert.ok(confirmed.text.includes('已收集来源但尚未形成可靠判断'));
    // 未授权时模型调用必须如实为 0。
    assert.ok(card.text.includes('模型调用 0 次'));
  });

  it('[F#10] legacy proposal without a final-conclusion state keeps the governed Graph create entry', async () => {
    const { doc, card } = home();
    const calls = [];
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    inject(doc, [alignment({
      execution: researchExecution({ research_progress: undefined, graph_escalation: proposalMaterial() }),
    })], {
      onCreateResearchRun: async (_item, setMessage) => {
        calls.push('create');
        await gate;
        setMessage('研究 Run 已创建，等待你确认授权。');
      },
    });
    const create = card.querySelector('.fragment-intent-research-create');
    assert.ok(create);
    assert.equal(card.querySelector('.fragment-intent-escalate'), null);
    // 文案不得暗示已创建 Graph Run。
    assert.ok(card.text.includes('尚未执行'));
    assert.equal(card.text.includes('已进入 Graph 工作流'), false);
    // 双击防重：监听器内部守卫，程序性双击只产生一次创建调用。
    create.click();
    create.click();
    await settle();
    assert.equal(calls.length, 1);
    release();
    await settle();
    assert.equal(create.getAttribute('disabled'), null);
    assert.ok(card.text.includes('研究 Run 已创建，等待你确认授权。'));
  });

  it('[F#10] never shows the create entry when proposal material is missing or drifted', () => {
    const cases = [
      ['missing spec_digest', proposalMaterial({ spec_digest: undefined })],
      ['wrong spec_id', proposalMaterial({ spec_id: 'fragment-pilot-v1' })],
      ['bad bundle digest', proposalMaterial({ evidence_bundle_digest: 'not-a-digest' })],
      ['bad escalation id', proposalMaterial({ escalation_id: 'escalation:XYZ' })],
      ['already executed', proposalMaterial({ status: 'bridged' })],
    ];
    for (const [name, proposal] of cases) {
      const { doc, card } = home();
      inject(doc, [alignment({
        execution: researchExecution({ research_progress: undefined, graph_escalation: proposal }),
      })], { onCreateResearchRun: () => assert.fail(`create handler fired for ${name}`) });
      assert.equal(card.querySelector('.fragment-intent-research-create'), null, name);
      // 未获准执行的提案若无合法材料，维持既有「提出 Graph 升级」入口；
      // 已非 proposed 状态的提案两个入口都不出现。
      if (proposal.status === 'proposed') {
        assert.ok(card.querySelector('.fragment-intent-escalate'), name);
      } else {
        assert.equal(card.querySelector('.fragment-intent-escalate'), null, name);
      }
    }
    // 完全没有提案时不出现任何 Graph 创建入口。
    const { doc, card } = home();
    inject(doc, [alignment({ execution: researchExecution() })]);
    assert.equal(card.querySelector('.fragment-intent-research-create'), null);
    assert.equal(card.querySelector('.fragment-intent-escalate'), null);
  });

  it('an actual final conclusion with unresolved limits no longer redirects the user into an old Graph proposal', () => {
    const { doc, card } = home();
    inject(doc, [alignment({ execution: researchExecution({ graph_escalation: proposalMaterial() }) })], {
      onCreateResearchRun: () => assert.fail('结论不能因为缺保存回执就升级 Graph'),
    });
    assert.ok(card.text.includes('性能未知'));
    assert.ok(card.text.includes('示例产品已发布'));
    assert.equal(card.querySelector('textarea'), null);
    assert.equal(card.querySelector('.fragment-intent-research-create'), null);
  });

  it('[F#2] a simple direct route never offers Graph creation', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      route: 'direct',
      execution: {
        run_id: 'exec:fragment-intent:0123456789abcdef01234567',
        fragment_id: 'fragment-1', status: 'passed', current_node: 'harvest',
        route: 'direct', stop_reason: 'completed', updated_at: '2026-08-09T00:00:00Z',
        result_digest: 'b'.repeat(64), harvest: [],
        result: {
          summary: '已直接处理。', unknowns: [], next_checks: [],
          needs_escalation: false, escalation_reason: '', model_calls: 0, tool_calls: 0,
        },
      },
    })]);
    assert.ok(card.text.includes('直接处理'));
    assert.equal(card.querySelector('.fragment-intent-research-create'), null);
    assert.equal(card.querySelector('.fragment-intent-escalate'), null);
    assert.equal(card.querySelector('.fragment-research-result'), null);
  });

  it('[F#8] renders external text as text nodes only, never markup', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      execution: researchExecution({
        result: {
          summary: '<img src=x onerror=alert(1)> 结论',
          unknowns: ['<b>加粗</b>'],
          next_checks: [],
          needs_escalation: false, escalation_reason: '', model_calls: 1, tool_calls: 1,
          confirmed: [{ claim: '<script>alert(1)</script>', evidence_ids: ['ev-official'] }],
          recommendation: '<u>建议</u>',
        },
        research_evidence: [{
          title: '<img src=y>', url: 'https://example.com/<b>',
          marker: 'newly_collected', evidence_id: 'ev-x',
        }],
      }),
    })]);
    assert.equal(card.querySelector('img'), null);
    assert.equal(card.querySelector('script'), null);
    assert.ok(card.text.includes('<img src=x onerror=alert(1)>'));
  });
});

describe('fragment research harvest candidates (包 F 产品验收 #8/#12, 审核 N2)', () => {
  it('[F#8] unfinished collection keeps collapsed candidates without calling them knowledge assets', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      execution: researchExecution({
        research_progress: { stage: 'collected', collected_sources: 2, model_calls: 0, network_requests: 3 },
        harvest: [
          { role: 'evidence', maturity: 'qualified', summary: '官方发布页可核验发布状态。' },
          { role: 'failure', maturity: 'candidate', summary: '社区来源未能确认发布日期。' },
        ],
      }),
    })]);
    const followup = [...card.querySelectorAll('.fragment-research-section')]
      .find((el) => el.querySelector('.fragment-research-label').text === '后续入口');
    const candidates = followup.querySelector('.fragment-harvest-candidates');
    assert.ok(candidates, 'harvest 候选必须进入后续入口段');
    assert.equal(candidates.tagName, 'DETAILS');
    assert.equal(candidates.getAttribute('open'), null, '默认折叠');
    // 候选明确标注非知识资产，绝不冒充（F#12）。
    assert.ok(candidates.text.includes('尚未成为知识资产'));
    assert.ok(candidates.text.includes('官方发布页可核验发布状态。（合格）'));
    assert.ok(candidates.text.includes('失败模式：社区来源未能确认发布日期。（候选）'));
    // continuation 入口不受影响，仍在后续入口段。
    assert.ok(followup.querySelector('.fragment-intent-continuation'));
  });

  it('[F#8] shows candidates instead of the empty state when no continuation is available', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      execution: researchExecution({
        status: 'running',
        result_digest: null,
        harvest: [{ role: 'evidence', maturity: 'candidate', summary: '已收集来源的身份链。' }],
      }),
    })]);
    const followup = [...card.querySelectorAll('.fragment-research-section')]
      .find((el) => el.querySelector('.fragment-research-label').text === '后续入口');
    assert.ok(followup.querySelector('.fragment-harvest-candidates'));
    assert.ok(followup.text.includes('已收集来源的身份链。（候选）'));
    assert.equal(followup.text.includes('当前没有可用的后续入口'), false);
  });

  it('[F#8] keeps the honest empty state when there are no candidates and no continuation', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      execution: researchExecution({
        status: 'running',
        result_digest: null,
        harvest: [],
      }),
    })]);
    const followup = [...card.querySelectorAll('.fragment-research-section')]
      .find((el) => el.querySelector('.fragment-research-label').text === '后续入口');
    assert.equal(followup.querySelector('.fragment-harvest-candidates'), null);
    assert.ok(followup.text.includes('当前没有可用的后续入口'));
  });

  it('[F#8] renders harvest candidate text as text nodes only', () => {
    const { doc, card } = home();
    inject(doc, [alignment({
      execution: researchExecution({
        status: 'running',
        result_digest: null,
        harvest: [{ role: 'evidence', maturity: 'candidate', summary: '<img src=z onerror=alert(1)>' }],
      }),
    })]);
    assert.equal(card.querySelector('img'), null);
    assert.ok(card.text.includes('<img src=z onerror=alert(1)>'));
  });
});
