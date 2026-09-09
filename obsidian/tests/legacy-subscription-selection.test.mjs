import { it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const require = createRequire(import.meta.url);
const { alignmentFor } = require('../src/console/fragment-intent-card.js');
const { buildCosmosModel, buildFragmentJourneys } = require('../src/console/homepage-cosmos.js');
const snapshot = JSON.parse(readFileSync(new URL('./fixtures/legacy-subscription-continuation.json', import.meta.url), 'utf8'));
const continuing = 'exec:fragment-intent:586453724a9a916c46cc18ba';
const qwen = 'exec:fragment-intent:6fbdcab00621400a45cfae68';
const rows = items => buildFragmentJourneys(buildCosmosModel(new FakeEl('div'), { total: 0, categories: [] }), {
  intents: () => ({ items }), reviews: () => ({ items: [] }),
});

for (const [runId, label] of [[continuing, 'continuing learning'], [qwen, 'Qwen legacy continuation']]) {
  for (const [stage, expected] of [['researching', '系统正在研究'], ['independent_review_in_progress', '证据复核进行中（模型）']]) {
    it(`${label}: current root ${stage} remains the daily row even with later history`, () => {
      const items = structuredClone(snapshot.items);
      const root = items.find(item => item.execution.run_id === runId);
      root.execution.updated_at = '2026-09-09T10:00:00.000000+00:00';
      root.execution.research_progress = { ...root.execution.research_progress, stage, cognitive: 'collecting' };
      // Keep the observed status=passed: the subscription journal is the current progress axis.
      for (const order of [items, [...items].reverse()]) {
        assert.equal(alignmentFor(order, root.fragment_id), root);
        const row = rows(order).find(item => item.fragmentId === root.fragment_id);
        assert.equal(row.alignment, root);
        assert.equal(row.state, 'active');
        assert.equal(row.statusLabel, expected);
      }
    });
  }
}

it('Qwen root publication remains reachable after completion without deleting older history', () => {
  const items = structuredClone(snapshot.items);
  const root = items.find(item => item.execution.run_id === qwen);
  const digest = 'f'.repeat(64);
  Object.assign(root.execution, { status: 'passed', updated_at: '2026-09-09T10:00:00+00:00', research_result_digest: digest,
    research_progress: { stage: 'synthesized', cognitive: 'synthesized', collected_sources: 3, model_calls: 3 },
    result: { summary: 'Fixture conclusion, not a system research result.', unknowns: [] },
    knowledge_publication: { knowledge_id: 'knowledge-' + 'f'.repeat(24), revision: 1,
      path: 'Loop知识资产/研究知识/fixture/note.md', result_digest: digest, publication_source: 'system_policy' } });
  const before = JSON.stringify(items);
  const row = rows(items).find(item => item.fragmentId === root.fragment_id);
  assert.equal(row.alignment, root);
  assert.equal(row.statusLabel, '结论已自动沉淀');
  assert.equal(row.state, 'done');
  assert.equal(JSON.stringify(items), before);
});


it('allowlisting alone does not replace the latest unmarked user continuation', () => {
  const items = structuredClone(snapshot.items);
  const original = items.find(item => item.execution.run_id === qwen);
  const latest = items.filter(item => item.fragment_id === original.fragment_id).sort((a,b) => b.sequence-a.sequence)[0];
  assert.equal(original.execution.subscription_selected, true);
  assert.equal(alignmentFor(items, original.fragment_id), latest);
});

it('newer real user goals, uncertain chronology, and unrelated runs cannot be hidden by a resumed root', () => {
  const base = structuredClone(snapshot.items);
  const original = base.find(item => item.execution.run_id === qwen);
  original.execution.updated_at = '2026-09-09T10:00:00+00:00';
  original.execution.research_progress = { stage: 'researching', cognitive: 'collecting' };
  const latestId = 'exec:fragment-intent:6b2421a35765641fb0b15290';
  const blocked = [
    (items, root, latest) => { latest.recovery_reason = undefined; latest.updated_at = '2026-09-09T11:00:00+00:00'; },
    (items, root, latest) => { latest.execution.updated_at = root.execution.updated_at; },
    (items, root, latest) => { latest.parent_run_id = 'exec:unavailable'; },
    (items, root, latest) => { root.fragment_id = 'another-fragment'; },
    (items, root, latest) => { root.parent_run_id = latest.execution.run_id; },
    (items, root) => { root.execution.subscription_selected = false; },
  ];
  for (const value of [undefined, '', '2026-09-09T10:00:00', '2026-02-30T10:00:00Z', 'not-a-date']) {
    blocked.push((items, root) => { root.execution.updated_at = value; });
    blocked.push((items, root, latest) => { latest.updated_at = value; });
    blocked.push((items, root, latest) => { latest.execution.updated_at = value; });
  }
  for (const change of blocked) {
    const items = structuredClone(base), root = items.find(item => item.execution.run_id === qwen);
    const latest = items.find(item => item.execution.run_id === latestId), fragmentId = latest.fragment_id;
    change(items, root, latest);
    assert.equal(alignmentFor(items, fragmentId), latest);
  }
});

it('activity compares timezone instants rather than timestamp strings', () => {
  const items = structuredClone(snapshot.items), root = items.find(item => item.execution.run_id === qwen);
  root.execution.research_progress = { stage: 'researching', cognitive: 'collecting' };
  root.execution.updated_at = '2026-09-09T09:00:00+08:00';
  const latest = items.find(item => item.execution.run_id === 'exec:fragment-intent:6b2421a35765641fb0b15290');
  latest.execution.updated_at = '2026-09-09T02:00:00Z';
  assert.equal(alignmentFor(items, root.fragment_id), latest);
  root.execution.updated_at = '2026-09-09T11:00:00+08:00';
  assert.equal(alignmentFor(items, root.fragment_id), root);
});
