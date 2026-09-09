import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const thoughtMap = require(path.join(ROOT, 'src', 'console', 'thought-map.js'));
const { injectHomepageThoughtMap } = require(path.join(ROOT, 'src', 'console', 'home-entry.js'));

const RAW_PATH = 'Notes/散记/碎片想法/synthetic-fragment.md';
const RESULT_PATH = 'Notes/散记/碎片认知/synthetic-result.md';

const COMPONENT_SLOTS = [
  'summary',
  'key_facts',
  'critical_corrections',
  'personal_connections',
  'blind_spots',
  'candidate_cards',
  'evidence_drawer',
];

function homeFrontmatter(extra = {}) {
  return {
    type: '碎片认知结果',
    title: '跨行业 AI 交付：平台化模式的证据盘点',
    thought_category: 'AI 公司与行业化',
    thought_status: 'provisional',
    thought_unknowns: ['制造与教育两个行业的交付成本结构差异'],
    content_lifecycle: 'draft',
    lifecycle_status: 'draft',
    evidence_level: 'unverified',
    user_confirmed: false,
    promoted_to_asset: false,
    home_schema_version: 'fragment-cognitive-home-v1',
    core_judgment: '该组织的平台化交付计划属实，但模式的普遍可行性只有部分证据。',
    user_value: '评估类似 AI 公司设想时，能区分「企业宣称」与「独立证据」。',
    credibility: 'medium',
    candidate_card_count: 2,
    unresolved_question_count: 1,
    primary_topic: 'AI 公司与行业化',
    topic_ids: ['ai-delivery'],
    available_actions: ['open_detail', 'confirm_draft', 'withdraw_draft'],
    component_slots: COMPONENT_SLOTS,
    source_fragment: RAW_PATH,
    ...extra,
  };
}

function newStyleNote(frontmatterExtra = {}) {
  return {
    path: RESULT_PATH,
    basename: 'synthetic-result',
    mtime: '2026-07-30T08:00:00Z',
    frontmatter: homeFrontmatter(frontmatterExtra),
    content: [
      '# 不应被当作标题的机器占位',
      '',
      '## 结论先行',
      '正文里的旧式推导结论不应覆盖结构化核心判断。',
    ].join('\n'),
  };
}

function rawFragmentNote() {
  return {
    path: RAW_PATH,
    basename: 'synthetic-fragment',
    mtime: '2026-07-20T09:30:57Z',
    frontmatter: { type: '碎片想法' },
    content: '---\ntype: "碎片想法"\n---\n\n我在思考一个尚未展开的问题。\nnigo-loop',
  };
}

describe('cognitive home projection (Package E)', () => {
  it('prefers structured home fields over Markdown headings for new-style notes', () => {
    const entry = thoughtMap.thoughtFromNote(newStyleNote());

    assert.ok(entry, 'new-style note must stay on the map');
    assert.ok(entry.home, 'valid home projection must be picked up');
    assert.equal(entry.home.schemaVersion, 'fragment-cognitive-home-v1');
    assert.equal(entry.title, '跨行业 AI 交付：平台化模式的证据盘点');
    assert.equal(entry.summary, '该组织的平台化交付计划属实，但模式的普遍可行性只有部分证据。');
    assert.equal(entry.home.credibility, 'medium');
    assert.equal(entry.home.candidateCardCount, 2);
    assert.equal(entry.home.unresolvedQuestionCount, 1);
    assert.equal(entry.home.primaryTopic, 'AI 公司与行业化');
    assert.deepEqual(entry.home.topicIds, ['ai-delivery']);
    assert.deepEqual(entry.home.availableActions, ['open_detail', 'confirm_draft', 'withdraw_draft']);
    assert.deepEqual(entry.home.componentSlots, COMPONENT_SLOTS);
    assert.equal(entry.home.lifecycleStatus, 'draft');
    assert.equal(entry.home.evidenceLevel, 'unverified');
    // 标题与核心判断来自 frontmatter 结构化投影，不依赖正文标题文本。
    assert.ok(!entry.title.includes('机器占位'));
    assert.ok(!entry.summary.includes('旧式推导'));
  });

  it('keeps legacy notes without the new frontmatter fully unchanged', () => {
    const legacy = thoughtMap.thoughtFromNote({
      path: RESULT_PATH,
      basename: 'synthetic-result',
      mtime: '2026-07-30T08:00:00Z',
      frontmatter: {
        type: '碎片认知结果',
        title: '未来 AI 公司业务架构设想',
        thought_category: 'AI 公司与行业化',
        source_fragment: RAW_PATH,
      },
      content: [
        '# 结果',
        '## 一、结论先行',
        '建立跨行业发现能力，并按行业形成专业交付单元。',
        '### 2.3 仍然未知',
        '- 首个行业如何选择；',
      ].join('\n'),
    });

    assert.ok(legacy, '反例 8：缺少新 frontmatter 的旧笔记不得从思考地图消失');
    assert.equal(legacy.home, null);
    assert.equal(legacy.title, '未来 AI 公司业务架构设想');
    assert.equal(legacy.summary, '建立跨行业发现能力，并按行业形成专业交付单元。');
    assert.equal(legacy.status, 'provisional');
    const map = thoughtMap.buildThoughtMap([legacy]);
    assert.equal(map.total, 1);
  });

  it('falls back to legacy derivation when the home projection is invalid or upgraded', () => {
    const invalidVariants = [
      { credibility: 'verified' },
      { content_lifecycle: 'published' },
      { lifecycle_status: 'published' },
      { evidence_level: 'source_confirmed' },
      { user_confirmed: true },
      { promoted_to_asset: true },
      { candidate_card_count: '2' },
      { unresolved_question_count: -1 },
      { available_actions: ['open_detail', 'auto_promote'] },
      { available_actions: ['confirm_draft'] },
      { component_slots: COMPONENT_SLOTS.slice(0, 6) },
      { component_slots: [...COMPONENT_SLOTS.slice(1), 'summary'] },
      { topic_ids: 'ai-delivery' },
      // 机器形态字段/值、哈希与带凭据 URL 参数必须拒绝。
      { core_judgment: '由 provider_id=deepseek 生成，已写入 ledger_path=data/x' },
      { user_value: '证据哈希 0fe761bacb239e65661b6c34d989a776edfd21d802d53c52a78f33e53f812ba0' },
      { user_value: '本次消耗 prompt_tokens=1024' },
      { primary_topic: 'AI 研究 ledger_path=data/ledger/x.jsonl' },
      { topic_ids: ['ai-delivery', 'prompt_tokens'] },
      { title: '' },
    ];
    for (const variant of invalidVariants) {
      const entry = thoughtMap.thoughtFromNote(newStyleNote(variant));
      assert.ok(entry, `非法主页投影不得让笔记消失: ${JSON.stringify(variant)}`);
      assert.equal(entry.home, null, `非法主页投影必须回退旧逻辑: ${JSON.stringify(variant)}`);
    }
    // 回退后完全按旧逻辑推导：摘要来自正文「结论」段而不是被篡改的 frontmatter。
    const entry = thoughtMap.thoughtFromNote(newStyleNote({ credibility: 'verified' }));
    assert.equal(entry.summary, '正文里的旧式推导结论不应覆盖结构化核心判断。');
    assert.equal(entry.title, '跨行业 AI 交付：平台化模式的证据盘点', '旧逻辑仍用 frontmatter title');
  });

  it('accepts normal semantic text about providers, tokens and ledgers', () => {
    // 正反例：讨论 provider/token/ledger/账本 概念的正常研究内容必须可输出。
    const entry = thoughtMap.thoughtFromNote(newStyleNote({
      core_judgment: '不同 provider 的公开评测差异显著，该服务按 token 计费。',
      user_value: '比较公开账本与 distributed ledger 研究时避免混淆概念。',
      primary_topic: '账本与审计研究',
    }));
    assert.ok(entry, '正常语义笔记必须留在思考地图上');
    assert.ok(entry.home, '正常语义句子不得被机器材料规则误伤');
    assert.equal(entry.home.primaryTopic, '账本与审计研究');
    assert.equal(entry.summary, '不同 provider 的公开评测差异显著，该服务按 token 计费。');
  });

  it('keeps source merge keys and withdrawal semantics identical for new-style notes', () => {
    // 反例 9：新主页字段不得改变 source 合并键或撤回语义。
    const entry = thoughtMap.thoughtFromNote(newStyleNote());
    assert.equal(entry.sourceKey, 'synthetic-fragment');
    const map = thoughtMap.buildThoughtMap([
      thoughtMap.thoughtFromNote(rawFragmentNote()),
      entry,
    ]);
    assert.equal(map.total, 1, '同一碎片的想法与结果仍按 sourceKey 合并');
    const item = map.categories.flatMap((group) => group.items)[0];
    assert.equal(item.path, RESULT_PATH, '认知结果仍优先于原始想法');
    assert.ok(item.home);

    const withdrawn = thoughtMap.thoughtFromNote(newStyleNote({ content_lifecycle: 'withdrawn' }));
    assert.equal(withdrawn, null, '撤回的新式笔记同样从地图消失');
    const recovered = thoughtMap.buildThoughtMap([
      thoughtMap.thoughtFromNote(rawFragmentNote()),
      withdrawn,
    ]);
    assert.equal(recovered.total, 1);
    assert.equal(recovered.categories.flatMap((group) => group.items)[0].path, RAW_PATH);
  });

  it('buildThoughtMap carries the home projection through to items', () => {
    const entry = thoughtMap.thoughtFromNote(newStyleNote());
    const map = thoughtMap.buildThoughtMap([entry]);
    const item = map.categories.flatMap((group) => group.items)[0];
    assert.ok(item.home);
    assert.equal(item.home.candidateCardCount, 2);
    assert.equal(item.status, 'provisional');
  });
});

describe('cognitive home projection rendering (Package E)', () => {
  function homeDocument() {
    const doc = new FakeEl('document');
    const home = doc.createDiv({ cls: 'my-life-homepage-view' });
    const content = home.createDiv({ cls: 'life-dashboard-content' });
    content.createDiv({ cls: 'life-fragments' });
    return doc;
  }

  function mapWithHome() {
    return thoughtMap.buildThoughtMap([
      thoughtMap.thoughtFromNote(newStyleNote()),
      {
        path: 'legacy.md',
        sourceKey: 'legacy',
        title: '旧式整理笔记',
        category: 'AI 公司与行业化',
        summary: '没有新主页字段的旧笔记。',
        unknowns: [],
        status: 'concluded',
        updatedAt: '2026-07-29T08:00:00Z',
        rank: 2,
      },
    ]);
  }

  it('exposes stable data hooks and readable home fields without any CSS', () => {
    // 反例 10：fake-dom 没有任何样式表，内容接口必须依然完整可用。
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, mapWithHome(), () => {});

    const rows = doc.querySelectorAll('.life-thought-row');
    assert.equal(rows.length, 2);
    const newRow = rows.find((row) => row.text.includes('跨行业 AI 交付'));
    assert.ok(newRow, 'new-style note must render as a row');
    assert.equal(newRow.getAttribute('data-home-schema'), 'fragment-cognitive-home-v1');
    assert.equal(newRow.getAttribute('data-credibility'), 'medium');
    assert.equal(newRow.getAttribute('data-candidate-cards'), '2');
    assert.equal(newRow.getAttribute('data-unresolved'), '1');
    const legacyRow = rows.find((row) => row.text.includes('旧式整理笔记'));
    assert.ok(legacyRow);
    assert.equal(legacyRow.getAttribute('data-home-schema'), null, '旧笔记没有新 hook');

    newRow.click();
    const panel = doc.querySelector('.life-thought-detail-panel');
    assert.equal(panel.getAttribute('data-component-slots'), COMPONENT_SLOTS.join(' '));
    assert.ok(panel.text.includes('可信度 中'));
    assert.ok(panel.text.includes('候选知识卡 2'));
    assert.ok(panel.text.includes('未解问题 1'));
    assert.ok(panel.text.includes('对你的价值：评估类似 AI 公司设想时'));
    assert.ok(panel.text.includes('仍然未知：制造与教育两个行业的交付成本结构差异'));

    // 收起详情后组件槽位 hook 复位，不残留上一条思考的投影。
    newRow.click();
    assert.equal(panel.getAttribute('data-component-slots'), '');
  });

  it('renders legacy items without home fields exactly as before', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, mapWithHome(), () => {});

    const legacyRow = doc.querySelectorAll('.life-thought-row')
      .find((row) => row.text.includes('旧式整理笔记'));
    legacyRow.click();
    const panel = doc.querySelector('.life-thought-detail-panel');
    assert.equal(panel.getAttribute('data-component-slots'), '');
    assert.ok(panel.text.includes('没有新主页字段的旧笔记。'));
    assert.ok(!panel.text.includes('候选知识卡'), '旧笔记不显示新计数字段');
    assert.ok(!panel.text.includes('对你的价值'), '旧笔记不显示新价值字段');
  });
});
