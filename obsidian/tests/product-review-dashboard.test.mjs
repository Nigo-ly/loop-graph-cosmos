import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const {
  createProductReviewClient,
  productDecisionId,
} = require(path.join(ROOT, 'src', 'console', 'product-review-client.js'));
const {
  injectHomepageReviewDashboard,
  openReviewDialog,
  summarizeReviews,
} = require(path.join(ROOT, 'src', 'console', 'product-review-dashboard.js'));

function candidate(overrides = {}) {
  return {
    schema_version: 'fragment-cognitive-product-review-v1',
    candidate_id: 'candidate-one',
    fragment_ref: 'fragments/synthetic.md',
    title: '合成认知候选',
    note_path: '碎片认知结果/candidate-one/fragment-product.md',
    content_status: 'pending_confirmation',
    evidence_level: 'unverified',
    candidate_card_count: 2,
    updated_at: '',
    available_actions: ['open_detail', 'confirm_asset', 'keep_draft'],
    has_conflict: false,
    revision: 0,
    content_sha256: 'a'.repeat(64),
    core_judgment: '合成核心判断',
    user_value: '合成用户价值',
    ...overrides,
  };
}

function homeDocument(copies = 1) {
  const doc = new FakeEl('document');
  doc.body = doc;
  for (let index = 0; index < copies; index += 1) {
    const home = doc.createDiv({ cls: 'my-life-homepage-view' });
    const content = home.createDiv({ cls: 'life-dashboard-content' });
    content.createDiv({ cls: 'life-fragments' });
  }
  return doc;
}

describe('Loop product review client', () => {
  it('pins the same decision digest as the backend', async () => {
    const body = {
      requester: 'nigo',
      action: 'keep_draft',
      candidate_id: 'candidate-one',
      fragment_ref: 'fragments/synthetic.md',
      content_sha256: 'a'.repeat(64),
      expected_revision: 0,
      selected_card_ids: [],
    };
    assert.equal(
      await productDecisionId(body),
      '201cd845b3d0455e3d727271147f1e2a1bb5334b6bc7d4ddab49f41aaa7b208f',
    );
  });

  it('uses only frozen loopback resources and exact decision binding', async () => {
    const requests = [];
    const client = createProductReviewClient({
      transport: async (request) => {
        requests.push(request);
        const data = request.method === 'GET' ? [candidate()] : {
          candidate_id: 'candidate-one', content_status: 'kept_draft', revision: 1,
          decision_id: 'receipt', idempotent: false, review_path: null, asset_paths: [],
        };
        return { status: request.method === 'GET' ? 200 : 202, json: { contract_version: '2', data } };
      },
    });
    const items = await client.list();
    await client.submit(items[0], 'keep_draft');

    assert.equal(requests.length, 2);
    assert.equal(requests[0].url, 'http://127.0.0.1:5684/fragment/v1/product-reviews');
    assert.equal(requests[0].method, 'GET');
    assert.equal(requests[1].url, 'http://127.0.0.1:5684/fragment/v1/product-reviews/candidate-one/decisions');
    assert.equal(requests[1].headers.Origin, 'app://obsidian.md');
    assert.equal(requests[1].headers['X-Loop-Product-Decision'], '1');
    assert.deepEqual(Object.keys(JSON.parse(requests[1].body)).sort(), [
      'action', 'candidate_id', 'content_sha256', 'decision_id', 'expected_revision',
      'fragment_ref', 'requester', 'selected_card_ids',
    ]);
  });
});

describe('homepage human confirmation dashboard', () => {
  it('shows explicit status counts and puts pending/conflict items first', () => {
    const result = summarizeReviews([
      candidate({ candidate_id: 'published', content_status: 'published_asset' }),
      candidate({ candidate_id: 'conflict', content_status: 'conflict' }),
      candidate({ candidate_id: 'pending' }),
    ]);
    assert.equal(result.counts.pending_confirmation, 1);
    assert.equal(result.counts.published_asset, 1);
    assert.equal(result.counts.conflict, 1);
    assert.deepEqual(result.items.map((item) => item.candidate_id), ['pending', 'conflict', 'published']);
  });

  it('injects every homepage copy idempotently with stable hooks and readable labels', () => {
    const doc = homeDocument(2);
    const items = [candidate(), candidate({
      candidate_id: 'published',
      title: '已确认候选',
      content_status: 'published_asset',
      revision: 1,
    })];
    let opened = null;
    const handlers = { openReview: (item) => { opened = item.candidate_id; } };

    assert.equal(injectHomepageReviewDashboard(doc, items, handlers), 2);
    assert.equal(injectHomepageReviewDashboard(doc, items, handlers), 0);
    assert.equal(doc.querySelectorAll('.life-loop-review-dashboard').length, 2);
    assert.ok(doc.text.includes('认知确认看板'));
    assert.ok(doc.text.includes('待你确认'));
    assert.ok(doc.text.includes('已形成资产'));
    assert.equal(doc.querySelectorAll('.life-loop-review-tile').length, 10);
    assert.equal(doc.querySelectorAll('.life-loop-review-row').length, 4);
    const first = doc.querySelectorAll('.life-loop-review-row')[0];
    assert.equal(first.getAttribute('data-review-candidate'), 'candidate-one');
    first.click();
    assert.equal(opened, 'candidate-one');
  });

  it('replaces a stale dashboard when a candidate revision changes', () => {
    const doc = homeDocument();
    const handlers = { openReview: () => {} };
    injectHomepageReviewDashboard(doc, [candidate()], handlers);
    assert.equal(injectHomepageReviewDashboard(doc, [candidate({ revision: 1 })], handlers), 1);
    assert.equal(doc.querySelectorAll('.life-loop-review-dashboard').length, 1);
    assert.ok(doc.text.includes('1 条候选'));
  });

  it('requires a selected card and a second confirmation before publishing', async () => {
    const doc = homeDocument();
    const submitted = [];
    let confirmations = 0;
    let changed = 0;
    const detail = {
      ...candidate(),
      candidate_cards: [
        { card_id: 'card-C1', title: '知识卡一', knowledge: '合成知识点' },
      ],
    };
    const client = {
      detail: async () => detail,
      submit: async (item, action, cards) => {
        submitted.push({ item, action, cards });
        return { content_status: 'published_asset', asset_paths: ['asset.md'] };
      },
    };
    await openReviewDialog(doc, candidate(), client, {
      openNote: async () => {},
      onChanged: async () => { changed += 1; },
      confirmAction: (message) => {
        confirmations += 1;
        assert.ok(message.includes('证据等级仍为 unverified'));
        return true;
      },
    });
    const confirm = doc.querySelectorAll('button').find((button) => button.text.includes('确认形成资产'));
    assert.ok(confirm);
    await confirm.listeners.click[0]();
    assert.equal(submitted.length, 0, 'zero cards must fail before confirmation and transport');
    const checkbox = doc.querySelector('input');
    checkbox.checked = true;
    checkbox.listeners.change[0]();
    await confirm.listeners.click[0]();
    assert.equal(confirmations, 1);
    assert.equal(submitted.length, 1);
    assert.deepEqual(submitted[0].cards, ['card-C1']);
    assert.equal(changed, 1);
    assert.equal(doc.querySelectorAll('.life-loop-review-dialog-overlay').length, 0);
  });
});
