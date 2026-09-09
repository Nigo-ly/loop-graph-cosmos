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

describe('homepage thought-map projection', () => {
  it('keeps raw thoughts, prefers the latest result for the same fragment, and never drops unknowns', () => {
    const map = thoughtMap.buildThoughtMap([
      {
        path: RAW_PATH,
        sourceKey: 'synthetic-fragment',
        title: '未来 AI 公司设想',
        category: 'AI 公司与行业化',
        summary: '原始想法等待展开。',
        unknowns: [],
        status: 'open',
        updatedAt: '2026-07-20T09:30:57Z',
        rank: 1,
      },
      {
        path: RESULT_PATH,
        sourceKey: 'synthetic-fragment',
        title: '未来 AI 公司业务架构设想',
        category: 'AI 公司与行业化',
        summary: '形成可复用的行业进入与交付底座。',
        unknowns: ['首个行业如何选择', '单位经济性如何成立'],
        status: 'provisional',
        updatedAt: '2026-07-30T08:00:00Z',
        rank: 3,
      },
      {
        path: 'other.md',
        sourceKey: 'other',
        title: '已经收敛的思考',
        category: '',
        summary: '已形成结论。',
        unknowns: [],
        status: 'concluded',
        updatedAt: '2026-07-29T08:00:00Z',
        rank: 3,
      },
    ]);

    assert.equal(map.total, 2);
    assert.deepEqual(map.counts, { open: 0, provisional: 1, concluded: 1 });
    assert.deepEqual(map.categories.map((group) => group.name), ['AI 公司与行业化', '未归类']);
    assert.equal(map.categories[0].items[0].path, RESULT_PATH);
    assert.deepEqual(map.categories[0].items[0].unknowns, ['首个行业如何选择', '单位经济性如何成立']);
  });

  it('derives a cognitive result from headings and preserves the detail path', () => {
    const entry = thoughtMap.thoughtFromNote({
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
        '- 深入研究由谁完成。',
        '## 三、来源',
        '略',
      ].join('\n'),
    });

    assert.equal(entry.sourceKey, 'synthetic-fragment');
    assert.equal(entry.status, 'provisional');
    assert.equal(entry.summary, '建立跨行业发现能力，并按行业形成专业交付单元。');
    assert.deepEqual(entry.unknowns, ['首个行业如何选择', '深入研究由谁完成']);
    assert.equal(entry.path, RESULT_PATH);
  });

  it('keeps an unprocessed raw fragment as an open thought and ignores unrelated notes', () => {
    const raw = thoughtMap.thoughtFromNote({
      path: RAW_PATH,
      basename: 'synthetic-fragment',
      mtime: '2026-07-20T09:30:57Z',
      frontmatter: { type: '碎片想法' },
      content: '---\ntype: "碎片想法"\n---\n\nhttps://example.invalid\n我在思考一个尚未展开的问题。\nnigo-loop',
    });
    const unrelated = thoughtMap.thoughtFromNote({
      path: 'daily.md',
      basename: 'daily',
      mtime: '2026-07-20T09:30:57Z',
      frontmatter: { type: '每日笔记' },
      content: '# 普通日记',
    });

    assert.equal(raw.status, 'open');
    assert.equal(raw.statusLabel, '待展开');
    assert.equal(raw.summary, '我在思考一个尚未展开的问题。');
    assert.equal(unrelated, null);
  });

  it('never labels a thought concluded while unresolved questions remain', () => {
    const map = thoughtMap.buildThoughtMap([{
      path: 'inconsistent.md',
      sourceKey: 'inconsistent',
      title: '状态矛盾样本',
      category: '合成测试',
      summary: '调用方错误地声称已经收敛。',
      unknowns: ['关键边界仍未确认'],
      status: 'concluded',
      updatedAt: '2026-07-30T08:00:00Z',
      rank: 3,
    }]);

    assert.equal(map.categories[0].items[0].status, 'provisional');
    assert.equal(map.categories[0].items[0].statusLabel, '已有阶段判断');
    assert.deepEqual(map.counts, { open: 0, provisional: 1, concluded: 0 });
  });
});

describe('homepage thought-map withdrawal semantics (R1-OD)', () => {
  function rawFragmentNote() {
    return {
      path: RAW_PATH,
      basename: 'synthetic-fragment',
      mtime: '2026-07-20T09:30:57Z',
      frontmatter: { type: '碎片想法' },
      content: '---\ntype: "碎片想法"\n---\n\n我在思考一个尚未展开的问题。\nnigo-loop',
    };
  }

  function derivedResultNote(frontmatterExtra = {}) {
    return {
      path: RESULT_PATH,
      basename: 'synthetic-result',
      mtime: '2026-07-30T08:00:00Z',
      frontmatter: {
        type: '碎片认知结果',
        title: '未来 AI 公司业务架构设想',
        thought_category: 'AI 公司与行业化',
        source_fragment: RAW_PATH,
        ...frontmatterExtra,
      },
      content: [
        '# 结果',
        '## 一、结论先行',
        '建立跨行业发现能力，并按行业形成专业交付单元。',
        '### 2.3 仍然未知',
        '- 首个行业如何选择；',
      ].join('\n'),
    };
  }

  it('filters a withdrawn derived result so a lone withdrawn input yields an empty map', () => {
    const entry = thoughtMap.thoughtFromNote(derivedResultNote({
      content_lifecycle: 'withdrawn',
      withdrawn_at: '2026-07-31T08:00:00Z',
    }));

    assert.equal(entry, null);
    const map = thoughtMap.buildThoughtMap([entry]);
    assert.equal(map.total, 0);
    assert.deepEqual(map.counts, { open: 0, provisional: 0, concluded: 0 });
    assert.deepEqual(map.categories, []);
  });

  it('keeps the raw fragment visible as open when its derived result is withdrawn', () => {
    const map = thoughtMap.buildThoughtMap([
      thoughtMap.thoughtFromNote(rawFragmentNote()),
      thoughtMap.thoughtFromNote(derivedResultNote({ content_lifecycle: 'withdrawn' })),
    ]);

    assert.equal(map.total, 1);
    assert.deepEqual(map.counts, { open: 1, provisional: 0, concluded: 0 });
    const item = map.categories.flatMap((group) => group.items)[0];
    assert.equal(item.path, RAW_PATH);
    assert.equal(item.status, 'open');
    assert.equal(item.statusLabel, '待展开');
  });

  it('lets a withdrawn derived result contribute no category, unknowns, summary, count, or clickable path', () => {
    const map = thoughtMap.buildThoughtMap([
      thoughtMap.thoughtFromNote(rawFragmentNote()),
      thoughtMap.thoughtFromNote(derivedResultNote({ content_lifecycle: 'withdrawn' })),
    ]);

    assert.deepEqual(map.categories.map((group) => group.name), ['未归类']);
    const allItems = map.categories.flatMap((group) => group.items);
    assert.ok(allItems.every((item) => item.path !== RESULT_PATH), 'withdrawn note must not be clickable');
    assert.ok(allItems.every((item) => !item.unknowns.includes('首个行业如何选择')));
    assert.ok(allItems.every((item) => !item.summary.includes('建立跨行业发现能力')));
    assert.ok(!map.categories.some((group) => group.name === 'AI 公司与行业化'));
  });

  it('withdraws organized notes as well, but never the raw fragment itself', () => {
    const organized = thoughtMap.thoughtFromNote({
      path: 'organized.md',
      basename: 'organized',
      mtime: '2026-07-30T08:00:00Z',
      frontmatter: {
        type: '已整理碎片',
        title: '整理稿',
        source_fragment: RAW_PATH,
        content_lifecycle: 'withdrawn',
      },
      content: '# 整理\n一些整理内容。',
    });
    assert.equal(organized, null);

    const rawWithdrawn = thoughtMap.thoughtFromNote({
      ...rawFragmentNote(),
      frontmatter: { type: '碎片想法', content_lifecycle: 'withdrawn' },
    });
    assert.ok(rawWithdrawn, '撤回只针对派生草稿/笔记，原始碎片不得消失');
    assert.equal(rawWithdrawn.status, 'open');
  });

  it('ignores withdrawn_at alone and keeps draft, missing, or legacy notes unchanged', () => {
    const onlyTimestamp = thoughtMap.thoughtFromNote(derivedResultNote({
      withdrawn_at: '2026-07-31T08:00:00Z',
    }));
    assert.ok(onlyTimestamp, 'withdrawn_at 单独存在不能推断已撤回');
    assert.equal(onlyTimestamp.status, 'provisional');

    const draft = thoughtMap.thoughtFromNote(derivedResultNote({ content_lifecycle: 'draft' }));
    assert.ok(draft, 'lifecycle=draft 保持既有行为');
    assert.equal(draft.status, 'provisional');

    const legacy = thoughtMap.thoughtFromNote(derivedResultNote());
    assert.ok(legacy, '缺失 lifecycle 的旧笔记保持既有行为');
    assert.equal(legacy.status, 'provisional');
  });

  it('matches the lifecycle value exactly after text normalization, never by guessing', () => {
    const padded = thoughtMap.thoughtFromNote(derivedResultNote({ content_lifecycle: '  withdrawn  ' }));
    assert.equal(padded, null, 'text() 规范化后精确为 withdrawn 才撤回');

    for (const lifecycle of ['Withdrawn', 'WITHDRAWN', 'withdrawn_draft', 'archived']) {
      const entry = thoughtMap.thoughtFromNote(derivedResultNote({ content_lifecycle: lifecycle }));
      assert.ok(entry, `lifecycle=${lifecycle} 不得被猜成 withdrawn`);
      assert.equal(entry.status, 'provisional');
    }
  });
});

describe('homepage thought-map mini constellation positions', () => {
  function sampleItems() {
    return [
      { sourceKey: 'alpha', title: '甲' },
      { sourceKey: 'beta', title: '乙' },
      { sourceKey: 'gamma', title: '丙' },
      { sourceKey: 'delta', title: '丁' },
      { sourceKey: 'epsilon', title: '戊' },
    ];
  }

  it('is deterministic and keeps every star inside the safe margins', () => {
    const first = thoughtMap.computeConstellationPositions(sampleItems());
    const second = thoughtMap.computeConstellationPositions(sampleItems());

    assert.deepEqual(first, second, 'same input must yield the same positions');
    for (const star of first) {
      assert.ok(star.x >= 8 && star.x <= 92, `star x out of bounds: ${star.x}`);
      assert.ok(star.y >= 8 && star.y <= 92, `star y out of bounds: ${star.y}`);
    }
  });

  it('conserves every thought as exactly one star, in order', () => {
    const items = sampleItems();
    const positions = thoughtMap.computeConstellationPositions(items);

    assert.equal(positions.length, items.length);
    assert.deepEqual(positions.map((star) => star.item), items);
  });

  it('spreads stars around the centre instead of collapsing onto one point', () => {
    const positions = thoughtMap.computeConstellationPositions(sampleItems());
    const distinct = new Set(positions.map((star) => `${Math.round(star.x)}:${Math.round(star.y)}`));
    assert.ok(distinct.size >= 4, 'expected stars to scatter across the mini map');
  });
});

describe('homepage thought-map ring geometry', () => {
  it('spaces cards evenly and grows the radius with the card count', () => {
    const two = thoughtMap.computeRingLayout(2);
    assert.equal(two.count, 2);
    assert.equal(two.step, 180);
    const eight = thoughtMap.computeRingLayout(8);
    assert.equal(eight.step, 45);
    assert.ok(eight.radius > two.radius, 'more cards need a larger circle');
    assert.deepEqual(thoughtMap.computeRingLayout(0), { count: 0, step: 0, radius: 0 });
    assert.equal(thoughtMap.computeRingLayout(1).radius, 0, 'a single card needs no circle');
  });

  it('puts card zero at the front for zero rotation and derives depth from the angle', () => {
    const states = thoughtMap.computeRingCardStates(4, 0);

    assert.equal(states.length, 4);
    assert.equal(states[0].angle, 0);
    assert.equal(states[0].depth, 1);
    assert.ok(states[0].front);
    assert.equal(states[2].depth, 0, 'the opposite card sits at the back');
    assert.ok(!states[2].front);
    assert.ok(states[0].opacity > states[2].opacity);
    assert.ok(states[0].scale > states[2].scale);
    assert.ok(states[0].zIndex > states[2].zIndex);
  });

  it('brings another card to the front when the ring rotates', () => {
    const states = thoughtMap.computeRingCardStates(4, 90);

    assert.ok(states[1].front);
    assert.equal(states[1].depth, 1);
    assert.ok(!states[0].front);
  });

  it('is deterministic for the same input', () => {
    assert.deepEqual(thoughtMap.computeRingCardStates(5, 123), thoughtMap.computeRingCardStates(5, 123));
  });

  it('focuses a card along the shortest arc', () => {
    assert.equal(thoughtMap.ringRotationForIndex(1, 4, 0), 90);
    assert.equal(thoughtMap.ringRotationForIndex(3, 4, 0), -90, 'never spins the long way round');
    assert.equal(thoughtMap.ringRotationForIndex(0, 4, 350), 360, 'wraps across the 0/360 seam');
    assert.equal(thoughtMap.ringRotationForIndex(0, 0, 12), 0, 'an empty ring stays put');
  });
});

describe('homepage thought-map 3D ring rendering', () => {
  function homeDocument() {
    const doc = new FakeEl('document');
    const home = doc.createDiv({ cls: 'my-life-homepage-view' });
    const content = home.createDiv({ cls: 'life-dashboard-content' });
    content.createDiv({ cls: 'life-fragments' });
    return doc;
  }

  function sampleMap() {
    return thoughtMap.buildThoughtMap([
      {
        path: RESULT_PATH,
        sourceKey: 'synthetic-fragment',
        title: '未来 AI 公司业务架构设想',
        category: 'AI 公司与行业化',
        summary: '建立行业研究与交付底座。',
        unknowns: ['首个行业如何选择'],
        status: 'provisional',
        updatedAt: '2026-07-30T08:00:00Z',
        rank: 3,
      },
      {
        path: 'second.md',
        sourceKey: 'second',
        title: '行业研究方法论',
        category: 'AI 公司与行业化',
        summary: '持续打磨中。',
        unknowns: [],
        status: 'open',
        updatedAt: '2026-07-28T08:00:00Z',
        rank: 1,
      },
      {
        path: 'other.md',
        sourceKey: 'other',
        title: '已经收敛的思考',
        category: '',
        summary: '已形成结论。',
        unknowns: [],
        status: 'concluded',
        updatedAt: '2026-07-29T08:00:00Z',
        rank: 3,
      },
    ]);
  }

  function manyCategoryMap() {
    return thoughtMap.buildThoughtMap(
      ['主题甲', '主题乙', '主题丙', '主题丁', '主题戊'].map((category, index) => ({
        path: `note-${index}.md`,
        sourceKey: `note-${index}`,
        title: `${category}的思考`,
        category,
        summary: '摘要。',
        unknowns: [],
        status: 'open',
        updatedAt: `2026-07-2${index}T08:00:00Z`,
        rank: 1,
      }))
    );
  }

  function ringCards(doc) {
    return doc.querySelectorAll('.life-thought-ring-card');
  }

  function activeCards(doc) {
    return ringCards(doc).filter((card) => card.classes.has('is-active'));
  }

  function hueOf(el) {
    return [...el.classes].find((cls) => cls.startsWith('is-hue')) || null;
  }

  it('renders the ring with one card per theme, the active card listing its thoughts', () => {
    const doc = homeDocument();

    assert.equal(injectHomepageThoughtMap(doc, sampleMap(), () => {}), 1);
    assert.equal(injectHomepageThoughtMap(doc, sampleMap(), () => {}), 0, 'must be idempotent');
    assert.equal(doc.querySelectorAll('.life-thought-map').length, 1);
    assert.ok(doc.text.includes('思考地图'));
    assert.ok(doc.text.includes('3 条思考 · 2 个主题'));
    assert.ok(doc.text.includes('阶段判断 1'));
    assert.ok(doc.text.includes('已有结论 1'));

    assert.equal(doc.querySelectorAll('.life-thought-ring-stage').length, 1);
    assert.equal(doc.querySelectorAll('.life-thought-ring').length, 1);
    const cards = ringCards(doc);
    assert.equal(cards.length, 2, 'every theme gets one card on the ring');
    assert.equal(activeCards(doc).length, 1, 'exactly one card is active');
    assert.ok(activeCards(doc)[0].text.includes('AI 公司与行业化'));
    assert.equal(hueOf(activeCards(doc)[0]), 'is-hue-0', 'first theme gets the first palette hue');
    const idle = cards.find((card) => !card.classes.has('is-active'));
    assert.ok(idle.text.includes('未归类'));
    assert.equal(hueOf(idle), 'is-hue-none', '未归类 always stays neutral grey');
    assert.equal(idle.getAttribute('aria-label'), '切换到主题：未归类');
    assert.equal(idle.querySelectorAll('.life-thought-row').length, 1, 'every card carries its full thought list (no expand-on-front rebuild)');

    const rows = doc.querySelectorAll('.life-thought-row');
    assert.equal(rows.length, 3, 'all cards together list every thought on the map');
    assert.ok(rows.some((row) => row.text.includes('未来 AI 公司业务架构设想')), 'full title must be present');
    assert.ok(rows.some((row) => row.text.includes('行业研究方法论')));
    const unknownRow = rows.find((row) => row.text.includes('未来 AI 公司业务架构设想'));
    assert.equal(unknownRow.querySelectorAll('.life-thought-row-unknown').length, 1, 'unresolved questions get an orange marker');
    assert.ok(unknownRow.text.includes('2026-07-30'), 'row shows the update date');

    assert.equal(doc.querySelectorAll('.life-thought-mini-map').length, 2, 'every card head carries its constellation');
    assert.equal(doc.querySelectorAll('.life-thought-mini-star').length, 3);
    assert.equal(doc.querySelectorAll('.life-thought-mini-link').length, 1, 'multi-star constellation gets a polyline');
    assert.ok(doc.text.includes('点击卡片中的一条思考'), 'detail panel starts with a hint');
    assert.equal(doc.querySelector('.life-thought-detail'), null, 'no detail action before a thought is selected');

    const cruise = doc.querySelector('.life-thought-ring-cruise');
    assert.ok(cruise, 'multi-theme rings get a cruise control');
    assert.equal(cruise.text, '暂停巡航', 'cruise starts on');
    assert.equal(doc.querySelectorAll('.life-thought-ring-step').length, 2, 'previous and next controls make every topic explicit');
    assert.ok(doc.querySelector('.life-thought-stage-caption').text.includes('AI 公司与行业化'));
    assert.ok(doc.text.includes('拖动旋转 · 点击卡片聚焦正面'));
    for (const card of cards) {
      assert.equal(card.getAttribute('role'), 'button');
      assert.equal(card.getAttribute('tabindex'), '0');
    }
  });

  it('injects an independent section into every rendered homepage copy', () => {
    const doc = new FakeEl('document');
    const contents = [];
    for (let copy = 0; copy < 2; copy += 1) {
      const home = doc.createDiv({ cls: 'my-life-homepage-view' });
      const content = home.createDiv({ cls: 'life-dashboard-content' });
      content.createDiv({ cls: 'life-fragments' });
      contents.push(content);
    }

    assert.equal(injectHomepageThoughtMap(doc, sampleMap(), () => {}), 2, 'every copy gets its own section');
    assert.equal(injectHomepageThoughtMap(doc, sampleMap(), () => {}), 0, 'second pass is a no-op on every copy');
    assert.equal(doc.querySelectorAll('.life-thought-map').length, 2);
    for (const content of contents) {
      assert.equal(content.querySelectorAll('.life-thought-map').length, 1);
      assert.equal(content.querySelectorAll('.life-thought-ring').length, 1);
      assert.equal(content.querySelectorAll('.life-thought-ring-card').length, 2);
      assert.equal(content.querySelectorAll('.life-thought-row').length, 3, 'each copy renders every thought on every card');
      assert.equal(content.querySelectorAll('.life-thought-mini-star').length, 3);
    }

    const firstRow = contents[0].querySelectorAll('.life-thought-row')[0];
    firstRow.click();
    assert.ok(firstRow.classes.has('is-selected'));
    const secondRow = contents[1].querySelectorAll('.life-thought-row')[0];
    assert.ok(!secondRow.classes.has('is-selected'), 'selection state is independent per homepage copy');
    assert.ok(!contents[1].text.includes('仍然未知：'), 'detail panel of the other copy stays on the hint');
  });

  it('rotates a clicked card to the front and makes it the active card', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, sampleMap(), () => {});

    assert.ok(activeCards(doc)[0].text.includes('AI 公司与行业化'));
    const idle = ringCards(doc).find((card) => !card.classes.has('is-active'));
    idle.click();

    const active = activeCards(doc);
    assert.equal(active.length, 1);
    assert.ok(active[0].text.includes('未归类'), 'clicked theme becomes the active card');
    assert.ok(active[0].text.includes('已经收敛的思考'));
    assert.equal(active[0].querySelectorAll('.life-thought-row').length, 1, 'active card lists its own thought');
    assert.equal(doc.querySelectorAll('.life-thought-row').length, 3, 'switching cards never adds or removes rows');
    assert.equal(hueOf(active[0]), 'is-hue-none', 'theme keeps its hue when promoted to the front');
    const demoted = ringCards(doc).find((card) => !card.classes.has('is-active'));
    assert.ok(demoted.text.includes('AI 公司与行业化'), 'previous card becomes an idle face');
    assert.equal(hueOf(demoted), 'is-hue-0', 'theme keeps its hue when demoted');
    assert.equal(demoted.querySelectorAll('.life-thought-row').length, 2, 'demoted card keeps its full thought list');
    assert.ok(doc.text.includes('点击卡片中的一条思考'), 'selection resets to the hint after switching');
    assert.equal(doc.querySelector('.life-thought-ring-cruise').text, '自动巡航', 'focusing a card pauses the cruise');
  });

  it('never rebuilds the ring or touches the detail panel when switching cards', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, sampleMap(), () => {});

    const before = ringCards(doc);
    const row = doc.querySelectorAll('.life-thought-row')
      .find((candidate) => candidate.text.includes('行业研究方法论'));
    row.click();
    assert.ok(doc.text.includes('持续打磨中。'), 'detail panel shows the selected thought');

    before[1].click();

    const after = ringCards(doc);
    assert.deepEqual(after, before, 'card elements are built once and never rebuilt');
    assert.ok(after[1].classes.has('is-active'), 'the highlight is just a class swap');
    assert.ok(doc.text.includes('持续打磨中。'), 'detail panel keeps the selected thought across rotation');
    assert.ok(row.classes.has('is-selected'), 'selection state survives rotation');
  });

  it('gives neighbouring themes different hues from the palette', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, manyCategoryMap(), () => {});

    const hues = ringCards(doc).map(hueOf);
    assert.deepEqual(hues, ['is-hue-0', 'is-hue-1', 'is-hue-2', 'is-hue-3', 'is-hue-4'], 'adjacent themes take consecutive palette hues');
    assert.equal(new Set(hues).size, hues.length, 'no two themes share a hue');
  });

  it('keeps every theme reachable on the ring no matter how many themes exist', () => {
    const doc = homeDocument();
    const map = manyCategoryMap();
    injectHomepageThoughtMap(doc, map, () => {});

    const cards = ringCards(doc);
    assert.equal(cards.length, 5, 'no theme is buried outside the ring');
    for (const category of map.categories) {
      assert.ok(
        cards.some((card) => card.text.includes(category.name)),
        `${category.name} has its own card`
      );
    }
    const target = cards.find((card) => card.text.includes('主题丁'));
    target.click();
    assert.ok(activeCards(doc)[0].text.includes('主题丁'), 'any card can be rotated to the front');
    const dimmed = doc.querySelectorAll('.life-thought-legend-item').filter((item) => item.classes.has('is-empty'));
    assert.equal(dimmed.length, 2, 'legend entries with zero thoughts are dimmed, not hidden');
  });

  it('pauses and resumes the idle cruise from the controls', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, sampleMap(), () => {});

    const cruise = doc.querySelector('.life-thought-ring-cruise');
    assert.equal(cruise.text, '暂停巡航');
    cruise.click();
    assert.equal(cruise.text, '自动巡航');
    assert.equal(cruise.getAttribute('aria-pressed'), 'false');
    cruise.click();
    assert.equal(cruise.text, '暂停巡航');
    assert.equal(cruise.getAttribute('aria-pressed'), 'true');
  });

  it('navigates topics from explicit controls and keeps the live caption in sync', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, sampleMap(), () => {});

    const steps = doc.querySelectorAll('.life-thought-ring-step');
    steps[1].click();
    assert.ok(activeCards(doc)[0].text.includes('未归类'));
    assert.ok(doc.querySelector('.life-thought-stage-caption').text.includes('未归类'));
    assert.equal(doc.querySelector('.life-thought-ring-stage').getAttribute('data-active-topic'), '2');
    steps[0].click();
    assert.ok(activeCards(doc)[0].text.includes('AI 公司与行业化'));
  });

  it('supports keyboard topic focus and honours reduced-motion preference', () => {
    const savedWindow = globalThis.window;
    globalThis.window = { matchMedia: () => ({ matches: true }) };
    try {
      const doc = homeDocument();
      injectHomepageThoughtMap(doc, sampleMap(), () => {});
      const cruise = doc.querySelector('.life-thought-ring-cruise');
      assert.equal(cruise.text, '自动巡航', 'reduced motion never starts idle cruise');
      assert.equal(cruise.getAttribute('aria-pressed'), 'false');

      const idle = ringCards(doc).find((card) => !card.classes.has('is-active'));
      let prevented = false;
      for (const fn of idle.listeners.keydown || []) {
        fn({ key: 'Enter', preventDefault() { prevented = true; } });
      }
      assert.equal(prevented, true);
      assert.ok(idle.classes.has('is-active'), 'keyboard focus uses the same deterministic ring path');
    } finally {
      globalThis.window = savedWindow;
    }
  });

  it('opens the detail sheet when a row is clicked and keeps the detail action working', () => {
    const doc = homeDocument();
    let opened = null;
    injectHomepageThoughtMap(doc, sampleMap(), (pathValue) => { opened = pathValue; });

    const rows = doc.querySelectorAll('.life-thought-row');
    const row = rows.find((candidate) => candidate.text.includes('未来 AI 公司业务架构设想'));
    assert.ok(row, 'expected a row for the synthetic fragment');

    row.click();
    assert.ok(row.classes.has('is-selected'));
    assert.ok(doc.text.includes('已有阶段判断'));
    assert.ok(doc.text.includes('建立行业研究与交付底座。'));
    assert.ok(doc.text.includes('仍然未知：首个行业如何选择'));
    assert.ok(doc.text.includes('更新于 2026-07-30'));
    assert.equal(doc.querySelector('.life-thought-ring-cruise').text, '自动巡航', 'selecting a thought pauses the cruise');
    doc.querySelector('.life-thought-detail').click();
    assert.equal(opened, RESULT_PATH);

    row.click();
    assert.ok(!row.classes.has('is-selected'), 'clicking again collapses the detail sheet');
    assert.ok(doc.text.includes('点击卡片中的一条思考'));
  });

  it('selects the same thought when a mini constellation star is clicked', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, sampleMap(), () => {});

    const stars = doc.querySelectorAll('.life-thought-mini-star');
    const star = stars.find((candidate) => (candidate.getAttribute('aria-label') || '').includes('行业研究方法论'));
    assert.ok(star, 'expected a mini star for the second thought');

    star.click();
    assert.ok(star.classes.has('is-selected'), 'mini star is marked selected');
    const rows = doc.querySelectorAll('.life-thought-row');
    const row = rows.find((candidate) => candidate.text.includes('行业研究方法论'));
    assert.ok(row.classes.has('is-selected'), 'the matching row stays in sync');
    assert.ok(doc.text.includes('持续打磨中。'), 'detail panel shows the selected thought');

    star.click();
    assert.ok(!row.classes.has('is-selected'), 'clicking again collapses the detail sheet');
    assert.ok(doc.text.includes('点击卡片中的一条思考'));
  });

  it('renders a useful empty state', () => {
    const doc = homeDocument();
    injectHomepageThoughtMap(doc, thoughtMap.buildThoughtMap([]), () => {});
    assert.ok(doc.text.includes('记录一条碎片后，它会出现在这里'));
  });

  it('drag rotates the ring, snaps to the nearest card, and suppresses the release click', () => {
    const doc = homeDocument();
    // suppressClick 的延迟重置依赖 window.setTimeout；本用例补一个最小 window
    // 桩，否则 Node 环境下 else 分支会把抑制标记立即清掉。
    const savedWindow = globalThis.window;
    globalThis.window = { setTimeout: () => 0 };
    try {
      injectHomepageThoughtMap(doc, sampleMap(), () => {});
      const stage = doc.querySelector('.life-thought-ring-stage');
      const ring = doc.querySelector('.life-thought-ring');
      const fire = (el, name, event) => (el.listeners[name] || []).forEach((fn) => fn(event));
      const rotationOf = () => ring.getAttribute('style');

      assert.ok(rotationOf().includes('rotateY(0deg)'), 'starts at zero rotation');
      fire(stage, 'pointerdown', { clientX: 400, pointerId: 1 });
      fire(doc, 'pointermove', { clientX: 320 });
      assert.ok(!rotationOf().includes('rotateY(0deg)'), 'drag rotates the ring');
      fire(doc, 'pointerup', { clientX: 320 });
      assert.ok(rotationOf().includes('rotateY(0deg)'), 'release snaps back to the nearest card');

      const activeBefore = ringCards(doc).findIndex((card) => card.classes.has('is-active'));
      const idle = ringCards(doc).find((card) => !card.classes.has('is-active'));
      idle.click();
      const activeAfter = ringCards(doc).findIndex((card) => card.classes.has('is-active'));
      assert.equal(activeAfter, activeBefore, 'the click released after a drag is suppressed');
    } finally {
      globalThis.window = savedWindow;
    }
  });
});
