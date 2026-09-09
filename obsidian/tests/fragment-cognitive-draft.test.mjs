import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
// NOTE: the original suite imported ./helpers/fake-dom.mjs, which is not part
// of this offline workspace (and not inside the six modifiable files). An
// equivalent minimal FakeEl is inlined here so the deterministic tests can
// actually run; behaviour matches the parts of the DOM used by console-dom.
class FakeEl {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.classes = new Set();
    this.attributes = {};
    this.listeners = {};
    this.textContent = '';
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.open = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }
  appendChild(el) {
    el.parentElement = this;
    this.children.push(el);
    return el;
  }
  createEl(tag, opts = {}) {
    const el = new FakeEl(tag);
    if (opts.cls) for (const c of String(opts.cls).split(' ').filter(Boolean)) el.addClass(c);
    if (typeof opts.text !== 'undefined') el.setText(opts.text);
    this.appendChild(el);
    return el;
  }
  createDiv(opts = {}) {
    return this.createEl('div', opts);
  }
  createSpan(opts = {}) {
    return this.createEl('span', opts);
  }
  addClass(...cls) {
    for (const c of cls) this.classes.add(c);
  }
  setText(value) {
    this.textContent = String(value);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'disabled') this.disabled = true;
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  dispatchEvent(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
  empty() {
    this.children = [];
  }
  focus() {}
  get text() {
    return this.textContent + this.children.map((child) => child.text).join('');
  }
  _matchesCompound(compound) {
    const tokens = compound.match(/([a-zA-Z][\w-]*)|\.([\w-]+)/g) || [];
    for (const token of tokens) {
      if (token.startsWith('.')) {
        if (!this.classes.has(token.slice(1))) return false;
      } else if (this.tagName !== token.toUpperCase()) {
        return false;
      }
    }
    return true;
  }
  matches(selector) {
    const compounds = String(selector).trim().split(/\s+/);
    if (!compounds.length || !this._matchesCompound(compounds[compounds.length - 1])) {
      return false;
    }
    let ancestor = this.parentElement;
    for (let i = compounds.length - 2; i >= 0; i -= 1) {
      while (ancestor && !ancestor._matchesCompound(compounds[i])) {
        ancestor = ancestor.parentElement;
      }
      if (!ancestor) return false;
      ancestor = ancestor.parentElement;
    }
    return true;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (el) => {
      for (const child of el.children) {
        if (child.matches(selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const model = require(path.join(ROOT, 'src', 'console', 'view-model.js'));
const { renderConsole } = require(path.join(ROOT, 'src', 'console', 'console-dom.js'));
const cognitiveClient = require(
  path.join(ROOT, 'src', 'console', 'cognitive-decision-client.js')
);
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
const { LoopConsoleView } = require(
  path.join(ROOT, 'src', 'console', 'console-view.js')
);
Module._load = originalLoad;

const MARKDOWN = '# 碎片认知结果（离线合成）\n\n## 原始碎片\n\n> 完全合成的碎片';

function detailEnvelope(overrides = {}) {
  return {
    run: {
      run_id: 'cognitive-r1b-synthetic',
      parent_run_id: null,
      loop_id: 'fragment-cognitive-synthetic-r1b',
      loopspec_version: '1.0.0',
      fragment_id: 'synthetic-fragment-1',
      goal: '离线验证碎片认知链',
      status: 'paused',
      display_state: 'awaiting_human',
      current_node: 'human_decision',
      iterations: 1,
      worker_version: 'synthetic-cognitive-fixture-v1',
      evaluator_version: 'fragment-cognitive-contract-r1a',
      budget_limits: {},
      budget_used: {},
      stop_reason: 'awaiting_cognitive_decision',
      resume_condition: '等待用户保留草稿或拒绝该合成认知结果',
      updated_at: '2026-07-30T02:00:00+08:00',
      eval_results: {
        cognitive_contract_status: 'validated',
        cognitive_route: 'research',
        cognitive_markdown: MARKDOWN,
        cognitive_decision: 'pending',
        content_lifecycle: 'draft',
        evidence_level: 'unverified',
        cognitive_result: { must_not_be_projected: 'internal' },
      },
      unresolved_issues: [],
      timeline: [{ sequence: 3, event_type: 'human_decision_required' }],
      evidence_refs: [],
      artifact_refs: [],
      asset_refs: [],
      ...overrides,
    },
  };
}

function stateFor(detail) {
  return {
    status: 'ready',
    error: null,
    health: null,
    queue: [],
    queueStale: false,
    runs: [],
    runsStale: false,
    statuses: [],
    attention: [],
    runsTotal: 1,
    filter: { status: null, statusLabel: null },
    selectedRunId: detail.runId,
    detail,
    detailLoading: false,
    detailError: null,
    detailStale: false,
    ui: { queueOpen: false, runsOpen: false, proposalHistoryOpen: false },
    proposals: { status: 'idle', current: [], history: [], selectedProposalId: null },
    review: { status: 'idle' },
    control: { status: 'unavailable', intents: [] },
  };
}

const handlers = {
  onRefresh() {},
  onFilterChange() {},
  onSelectRun() {},
  onBack() {},
  onRefreshControl() {},
  onMinimumValueConsent() {},
  onMinimumValueDecline() {},
  onCognitiveDraftDecision() {},
  onCognitiveWithdraw() {},
};

describe('fragment cognitive synthetic draft surface', () => {
  it('uses the same fixed idempotency vector as the backend', async () => {
    const key = await cognitiveClient.decisionIdempotencyKey(
      {
        decision: 'keep_draft',
        runId: 'run-fixed',
        fragmentId: 'fragment-fixed',
        expectedSequence: 7,
      },
      'nigo',
      'a'.repeat(64),
      '行业研究'
    );

    assert.equal(
      key,
      '49f917a03ba5fdc678e5c46003a3045a767fee6faa5729d22180feed120881ab'
    );
  });

  it('changing the category produces a different idempotency key', async () => {
    const input = {
      decision: 'keep_draft',
      runId: 'run-fixed',
      fragmentId: 'fragment-fixed',
      expectedSequence: 7,
    };
    const original = await cognitiveClient.decisionIdempotencyKey(
      input,
      'nigo',
      'a'.repeat(64),
      '行业研究'
    );
    const changed = await cognitiveClient.decisionIdempotencyKey(
      input,
      'nigo',
      'a'.repeat(64),
      '生活整理'
    );

    assert.equal(
      changed,
      'ea6c701afad4003ae278a4864f536d8c612be215328f0040e05af2e7b90633bf'
    );
    assert.notEqual(changed, original);
  });

  it('projects and renders only the validated unverified draft fields', () => {
    const detail = model.detailModel(detailEnvelope());

    assert.deepEqual(detail.cognitiveDraft, {
      route: 'research',
      markdown: MARKDOWN,
      decision: 'pending',
      lifecycle: 'draft',
      evidenceLevel: 'unverified',
      awaitingDecision: true,
      kept: false,
      withdrawalPending: false,
      withdrawn: false,
      withdrawalId: null,
      runId: 'cognitive-r1b-synthetic',
      fragmentId: 'synthetic-fragment-1',
      expectedSequence: 3,
      // No cognitive_draft_view key: a legacy backend projection keeps the
      // markdown-only card with a null structured view.
      view: null,
    });
    assert.equal('cognitiveResult' in detail.cognitiveDraft, false);

    const root = new FakeEl('div');
    renderConsole(root, stateFor(detail), handlers);

    assert.ok(root.text.includes('碎片认知草稿'));
    const titleEl = root.querySelector('.lc-cognitive-draft h3');
    assert.ok(titleEl);
    assert.equal(titleEl.text, '碎片认知草稿');
    assert.ok(root.text.includes(MARKDOWN));
    assert.ok(root.text.includes('尚未经过真实来源验证'));
    assert.equal(root.querySelectorAll('.lc-cognitive-draft').length, 1);
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 1);
    assert.equal(root.querySelectorAll('.lc-cognitive-reject').length, 1);
  });

  it('fails closed for another loop, rejected contract, or non-unverified evidence', () => {
    const wrongLoop = detailEnvelope({ loop_id: 'another-loop' });
    assert.equal(model.detailModel(wrongLoop).cognitiveDraft, null);

    const rejected = detailEnvelope();
    rejected.run.eval_results.cognitive_contract_status = 'rejected';
    assert.equal(model.detailModel(rejected).cognitiveDraft, null);

    const elevated = detailEnvelope();
    elevated.run.eval_results.evidence_level = 'verified';
    assert.equal(model.detailModel(elevated).cognitiveDraft, null);
  });

  it('posts only the displayed binding to the frozen loopback endpoint', async () => {
    const calls = [];
    const client = cognitiveClient.createCognitiveDecisionClient({
      transport: async (request) => {
        calls.push(request);
        return {
          status: 202,
          json: {
            contract_version: '2',
            data: {
              decision: 'keep_draft',
              decision_id: 'a'.repeat(64),
              status: 'recorded',
              thought_category: '行业研究',
            },
          },
        };
      },
    });

    const result = await client.submitDecision({
      decision: 'keep_draft',
      runId: 'cognitive-r1b-synthetic',
      fragmentId: 'synthetic-fragment-1',
      markdown: MARKDOWN,
      expectedSequence: 3,
      thoughtCategory: '  行业研究  ',
    });

    assert.equal(result.httpStatus, 202);
    assert.equal(calls.length, 1);
    assert.equal(
      calls[0].url,
      'http://127.0.0.1:5684/fragment-cognitive/v1/decisions'
    );
    assert.equal(calls[0].method, 'POST');
    assert.equal(calls[0].headers['X-Fragment-Cognitive-Decision'], '1');
    const body = JSON.parse(calls[0].body);
    assert.deepEqual(Object.keys(body).sort(), [
      'decision',
      'expected_sequence',
      'fragment_id',
      'idempotency_key',
      'markdown_sha256',
      'requester',
      'run_id',
      'thought_category',
    ]);
    assert.equal(body.decision, 'keep_draft');
    assert.equal(body.thought_category, '行业研究');
    assert.match(body.markdown_sha256, /^[0-9a-f]{64}$/);
    assert.match(body.idempotency_key, /^[0-9a-f]{64}$/);
    assert.equal('markdown' in body, false);
  });

  it('blocks invalid keep categories before the transport is called', async () => {
    const invalidCategories = [
      '',
      '   ',
      '行业\n研究',
      '行业\r研究',
      '行业\x1f研究',
      '\n行业研究',
      '行业研究\n',
      '\r行业研究',
      '行业研究\r',
      'a'.repeat(129),
      undefined,
    ];
    for (const thoughtCategory of invalidCategories) {
      const calls = [];
      const client = cognitiveClient.createCognitiveDecisionClient({
        transport: async (request) => {
          calls.push(request);
          return { status: 202, json: { contract_version: '2', data: {} } };
        },
      });

      await assert.rejects(
        client.submitDecision({
          decision: 'keep_draft',
          runId: 'cognitive-r1b-synthetic',
          fragmentId: 'synthetic-fragment-1',
          markdown: MARKDOWN,
          expectedSequence: 3,
          thoughtCategory,
        }),
        (error) => error.kind === 'invalid_arguments'
      );
      assert.equal(calls.length, 0);
    }
  });

  it('limits the category by Unicode code points, not UTF-16 units', async () => {
    const calls = [];
    const client = cognitiveClient.createCognitiveDecisionClient({
      transport: async (request) => {
        calls.push(request);
        return {
          status: 202,
          json: {
            contract_version: '2',
            data: {
              decision: 'keep_draft',
              decision_id: 'a'.repeat(64),
              status: 'recorded',
              thought_category: '🧠'.repeat(128),
            },
          },
        };
      },
    });

    await client.submitDecision({
      decision: 'keep_draft',
      runId: 'cognitive-r1b-synthetic',
      fragmentId: 'synthetic-fragment-1',
      markdown: MARKDOWN,
      expectedSequence: 3,
      thoughtCategory: '🧠'.repeat(128),
    });
    assert.equal(calls.length, 1);
    assert.equal(JSON.parse(calls[0].body).thought_category, '🧠'.repeat(128));

    await assert.rejects(
      client.submitDecision({
        decision: 'keep_draft',
        runId: 'cognitive-r1b-synthetic',
        fragmentId: 'synthetic-fragment-1',
        markdown: MARKDOWN,
        expectedSequence: 3,
        thoughtCategory: '🧠'.repeat(129),
      }),
      (error) => error.kind === 'invalid_arguments'
    );
    assert.equal(calls.length, 1);
  });

  it('reject only carries an empty category', async () => {
    const calls = [];
    const client = cognitiveClient.createCognitiveDecisionClient({
      transport: async (request) => {
        calls.push(request);
        return {
          status: 202,
          json: {
            contract_version: '2',
            data: { decision: 'reject', decision_id: 'b'.repeat(64), status: 'recorded' },
          },
        };
      },
    });

    await assert.rejects(
      client.submitDecision({
        decision: 'reject',
        runId: 'cognitive-r1b-synthetic',
        fragmentId: 'synthetic-fragment-1',
        markdown: MARKDOWN,
        expectedSequence: 3,
        thoughtCategory: '行业研究',
      }),
      (error) => error.kind === 'invalid_arguments'
    );
    assert.equal(calls.length, 0);

    await client.submitDecision({
      decision: 'reject',
      runId: 'cognitive-r1b-synthetic',
      fragmentId: 'synthetic-fragment-1',
      markdown: MARKDOWN,
      expectedSequence: 3,
      thoughtCategory: '',
    });
    assert.equal(calls.length, 1);
    assert.equal(JSON.parse(calls[0].body).thought_category, '');
  });

  it('disables keep and shows the reason until a category is typed', () => {
    const detail = model.detailModel(detailEnvelope());
    const empty = stateFor(detail);
    empty.cognitiveDecision = {
      submitting: false,
      error: null,
      lastDecision: null,
      thoughtCategory: '',
    };
    const emptyRoot = new FakeEl('div');
    renderConsole(emptyRoot, empty, handlers);

    const emptyKeep = emptyRoot.querySelector('.lc-cognitive-keep');
    const emptyReject = emptyRoot.querySelector('.lc-cognitive-reject');
    const emptyReason = emptyRoot.querySelector('.lc-cognitive-category-reason');
    const emptyInput = emptyRoot.querySelector('.lc-cognitive-category-input');
    const emptyHint = emptyRoot.querySelector('.lc-cognitive-category-hint');
    assert.ok(emptyInput);
    assert.equal(emptyInput.getAttribute('maxlength'), null);
    assert.equal(emptyKeep.disabled, true);
    assert.equal(emptyReason.hidden, false);
    assert.equal(emptyReject.disabled || emptyReject.attributes.disabled, undefined);
    // G2：格式 hint 常驻（非法时一行说明的前提是用户知道合法格式长什么样）
    assert.ok(emptyHint, '格式 hint 存在');
    assert.ok(emptyHint.textContent.includes('示例'), 'hint 给出可照抄的格式示例');

    const filled = stateFor(detail);
    filled.cognitiveDecision = {
      submitting: false,
      error: null,
      lastDecision: null,
      thoughtCategory: '行业研究',
    };
    const filledRoot = new FakeEl('div');
    renderConsole(filledRoot, filled, handlers);

    assert.equal(filledRoot.querySelector('.lc-cognitive-keep').disabled, false);
    assert.equal(
      filledRoot.querySelector('.lc-cognitive-category-reason').hidden,
      true
    );
  });

  it('the view submits the selected draft once and replaces actions with a receipt', async () => {
    const calls = [];
    const client = {
      async submitDecision(input) {
        calls.push(input);
        return {
          decision: {
            decision: input.decision,
            decision_id: 'a'.repeat(64),
            status: 'recorded',
            thought_category: input.thoughtCategory,
          },
        };
      },
    };
    const view = new LoopConsoleView(
      {},
      null,
      null,
      null,
      null,
      null,
      client
    );
    view.render = () => {};
    view.state.detail = model.detailModel(detailEnvelope());
    view.state.selectedRunId = view.state.detail.runId;

    await view.submitCognitiveDraftDecision('keep_draft');
    assert.equal(calls.length, 0);

    view.handlers.onCognitiveCategoryChange('  行业研究  ');
    await view.submitCognitiveDraftDecision('keep_draft');

    assert.deepEqual(calls, [
      {
        decision: 'keep_draft',
        runId: 'cognitive-r1b-synthetic',
        fragmentId: 'synthetic-fragment-1',
        markdown: MARKDOWN,
        expectedSequence: 3,
        thoughtCategory: '行业研究',
      },
    ]);
    assert.equal(view.state.cognitiveDecision.submitting, false);
    assert.equal(view.state.cognitiveDecision.lastDecision.decision, 'keep_draft');
    assert.equal(
      view.state.cognitiveDecision.lastDecision.thoughtCategory,
      '行业研究'
    );
    assert.equal(view.state.detail.cognitiveDraft, null);
    view.state.status = 'ready';
    const root = new FakeEl('div');
    renderConsole(root, view.state, view.handlers);
    assert.ok(root.text.includes('已记录：保留为未验证草稿（分类：行业研究）'));
  });
});

// ---------------------------------------------------------------------------
// R1-OC: precise identity, withdrawal surface, withdrawal client contract.
// ---------------------------------------------------------------------------

function keptEnvelope() {
  const envelope = detailEnvelope({
    status: 'completed',
    display_state: 'completed',
    stop_reason: null,
  });
  envelope.run.eval_results.cognitive_decision = 'keep_draft';
  return envelope;
}

// Matches the backend's persisted pending receipt verbatim (plus private
// fields that must never be projected). Backend receipts carry no status
// field: withdrawal_pending / withdrawal_completed derives from the receipt
// key, never from receipt contents.
function validReceipt(overrides = {}) {
  return {
    action: 'withdraw',
    withdrawal_id: 'c'.repeat(64),
    fragment_id: 'synthetic-fragment-1',
    expected_sequence: 3,
    thought_updated_at: '2026-07-30T02:00:00+08:00',
    note_path: 'must/not/leak.md',
    note_body: 'private body must never be projected',
    note_category: 'private category',
    ...overrides,
  };
}

function pendingEnvelope(receipt) {
  const envelope = keptEnvelope();
  envelope.run.eval_results.cognitive_withdrawal_pending_receipt =
    receipt || validReceipt();
  return envelope;
}

// The real terminal checkpoint still carries the pending receipt next to the
// terminal one; the terminal adds note_status/withdrawn_at/note_sha256.
function completedEnvelope(receipt) {
  const envelope = keptEnvelope();
  envelope.run.eval_results.cognitive_withdrawal_pending_receipt = validReceipt();
  envelope.run.eval_results.cognitive_withdrawal_receipt =
    receipt ||
    validReceipt({
      note_status: 'withdrawn',
      withdrawn_at: '2026-07-30T03:00:00+08:00',
      note_sha256: 'd'.repeat(64),
    });
  envelope.run.eval_results.content_lifecycle = 'withdrawn';
  return envelope;
}

describe('fragment cognitive R1-OC precise identity', () => {
  it('projects the local chain with the exact id and exact version', () => {
    const local = detailEnvelope({ loop_id: 'fragment-cognitive-local-r1n' });
    const detail = model.detailModel(local);

    assert.ok(detail.cognitiveDraft);
    assert.equal(detail.cognitiveDraft.awaitingDecision, true);

    const root = new FakeEl('div');
    renderConsole(root, stateFor(detail), handlers);
    assert.ok(root.text.includes('碎片认知草稿'));
    const titleEl = root.querySelector('.lc-cognitive-draft h3');
    assert.ok(titleEl);
    assert.equal(titleEl.text, '碎片认知草稿');
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 1);
  });

  it('fails closed for lookalike ids and wrong or missing versions', () => {
    const lookalikes = [
      'fragment-cognitive-synthetic-r1b2',
      'xfragment-cognitive-synthetic-r1b',
      'fragment-cognitive-synthetic-r1',
      'fragment-cognitive-local-r1n-extra',
    ];
    for (const loopId of lookalikes) {
      assert.equal(
        model.detailModel(detailEnvelope({ loop_id: loopId })).cognitiveDraft,
        null,
        `lookalike ${loopId} must not project`
      );
    }

    assert.equal(
      model.detailModel(detailEnvelope({ loopspec_version: '1.0.1' })).cognitiveDraft,
      null
    );
    const missing = detailEnvelope();
    delete missing.run.loopspec_version;
    assert.equal(model.detailModel(missing).cognitiveDraft, null);
    const emptyVersion = detailEnvelope({ loopspec_version: '' });
    assert.equal(model.detailModel(emptyVersion).cognitiveDraft, null);
  });
});

describe('fragment cognitive R1-OC four exclusive states', () => {
  it('kept draft shows the withdraw action and no keep/reject actions', () => {
    const detail = model.detailModel(keptEnvelope());
    assert.equal(detail.cognitiveDraft.kept, true);
    assert.equal(detail.cognitiveDraft.awaitingDecision, false);
    assert.equal(detail.cognitiveDraft.withdrawalPending, false);
    assert.equal(detail.cognitiveDraft.withdrawn, false);
    assert.equal(detail.cognitiveDraft.expectedSequence, 3);

    const root = new FakeEl('div');
    renderConsole(root, stateFor(detail), handlers);
    assert.equal(root.querySelectorAll('.lc-cognitive-withdraw').length, 1);
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 0);
    assert.equal(root.querySelectorAll('.lc-cognitive-reject').length, 0);
    assert.equal(root.querySelectorAll('.lc-cognitive-withdraw-continue').length, 0);
    assert.ok(root.text.includes('未验证'));
  });

  it('a valid pending receipt shows continue-withdrawal with the receipt sequence', () => {
    const envelope = pendingEnvelope();
    envelope.run.timeline.push({ sequence: 9, event_type: 'state_transition' });
    const detail = model.detailModel(envelope);

    assert.equal(detail.cognitiveDraft.withdrawalPending, true);
    assert.equal(detail.cognitiveDraft.kept, false);
    assert.equal(detail.cognitiveDraft.awaitingDecision, false);
    // The continue submission must bind the receipt's original sequence,
    // never the newer timeline maximum (9).
    assert.equal(detail.cognitiveDraft.expectedSequence, 3);
    assert.equal(detail.cognitiveDraft.withdrawalId, 'c'.repeat(64));
    const projected = JSON.stringify(detail.cognitiveDraft);
    assert.ok(!projected.includes('must/not/leak.md'));
    assert.ok(!projected.includes('private body'));
    assert.ok(!projected.includes('private category'));

    const root = new FakeEl('div');
    renderConsole(root, stateFor(detail), handlers);
    assert.ok(root.text.includes('撤回待恢复'));
    assert.equal(root.querySelectorAll('.lc-cognitive-withdraw-continue').length, 1);
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 0);
    assert.equal(root.querySelectorAll('.lc-cognitive-reject').length, 0);
    assert.equal(root.querySelectorAll('.lc-cognitive-withdraw').length, 0);
    assert.ok(!root.text.includes('must/not/leak.md'));
    assert.ok(!root.text.includes('private body'));
  });

  it('invalid pending receipts fail closed and never show a continue action', () => {
    const invalidReceipts = [
      validReceipt({ withdrawal_id: undefined }),
      validReceipt({ withdrawal_id: 'C'.repeat(64) }),
      validReceipt({ withdrawal_id: 'g'.repeat(64) }),
      validReceipt({ withdrawal_id: 'c'.repeat(63) }),
      validReceipt({ fragment_id: 'another-fragment' }),
      validReceipt({ expected_sequence: 0 }),
      validReceipt({ expected_sequence: '3' }),
      validReceipt({ expected_sequence: 3.5 }),
      validReceipt({ action: 'keep' }),
    ];
    for (const receipt of invalidReceipts) {
      assert.equal(
        model.detailModel(pendingEnvelope(receipt)).cognitiveDraft,
        null,
        `invalid receipt must fail closed: ${JSON.stringify(receipt)}`
      );
    }
  });

  it('a receipt under an unrecognized key never creates a withdrawal state', () => {
    const envelope = keptEnvelope();
    envelope.run.eval_results.withdrawal_receipt = validReceipt();
    envelope.run.eval_results.cognitive_withdrawal_receipts = validReceipt();
    const detail = model.detailModel(envelope);

    assert.equal(detail.cognitiveDraft.kept, true);
    assert.equal(detail.cognitiveDraft.withdrawalPending, false);
    assert.equal(detail.cognitiveDraft.withdrawn, false);
  });

  it('a completed receipt plus withdrawn lifecycle shows the terminal state only', () => {
    const envelope = completedEnvelope();
    const detail = model.detailModel(envelope);

    assert.equal(detail.cognitiveDraft.withdrawn, true);
    assert.equal(detail.cognitiveDraft.lifecycle, 'withdrawn');
    assert.equal(detail.cognitiveDraft.evidenceLevel, 'unverified');

    const root = new FakeEl('div');
    renderConsole(root, stateFor(detail), handlers);
    assert.ok(root.text.includes('已撤回'));
    assert.ok(root.text.includes('未验证'));
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 0);
    assert.equal(root.querySelectorAll('.lc-cognitive-reject').length, 0);
    assert.equal(root.querySelectorAll('.lc-cognitive-withdraw').length, 0);
    assert.equal(root.querySelectorAll('.lc-cognitive-withdraw-continue').length, 0);
  });

  it('a completed receipt without the withdrawn lifecycle is causally inconsistent', () => {
    const envelope = completedEnvelope();
    envelope.run.eval_results.content_lifecycle = 'draft';
    assert.equal(model.detailModel(envelope).cognitiveDraft, null);
  });

  it('a pending receipt without the draft lifecycle is causally inconsistent', () => {
    const envelope = pendingEnvelope();
    envelope.run.eval_results.content_lifecycle = 'withdrawn';
    assert.equal(model.detailModel(envelope).cognitiveDraft, null);
  });

  it('an elevated evidence level never projects the withdrawn terminal state', () => {
    const envelope = completedEnvelope();
    envelope.run.eval_results.evidence_level = 'verified';
    assert.equal(model.detailModel(envelope).cognitiveDraft, null);
  });
});

describe('fragment cognitive R1-OC withdrawal client contract', () => {
  it('uses the frozen idempotency vector in the frozen field order', async () => {
    const key = await cognitiveClient.withdrawalIdempotencyKey(
      { runId: 'run-fixed', fragmentId: 'fragment-fixed', expectedSequence: 7 },
      'nigo'
    );
    assert.equal(
      key,
      '902f072890921190820b187ff18e2a3099ed78fd1b61b55b314ef43ff3c0b7ec'
    );

    const changed = await cognitiveClient.withdrawalIdempotencyKey(
      { runId: 'run-fixed', fragmentId: 'fragment-fixed', expectedSequence: 8 },
      'nigo'
    );
    assert.equal(
      changed,
      '8556ae3ba0d48c918357ddf254fa2f26ec32dcf936ea818ff3d1a337ef96e925'
    );
    assert.notEqual(changed, key);
  });

  it('posts exactly the six frozen fields with the withdrawal header only', async () => {
    const calls = [];
    const client = cognitiveClient.createCognitiveDecisionClient({
      transport: async (request) => {
        calls.push(request);
        return {
          status: 202,
          json: {
            contract_version: '2',
            data: {
              action: 'withdraw',
              withdrawal_id: 'c'.repeat(64),
              status: 'withdrawn',
              note_status: 'withdrawn',
              idempotent: false,
            },
          },
        };
      },
    });

    const result = await client.submitWithdrawal({
      runId: 'cognitive-r1b-synthetic',
      fragmentId: 'synthetic-fragment-1',
      expectedSequence: 3,
    });

    assert.equal(result.httpStatus, 202);
    assert.equal(calls.length, 1);
    assert.equal(
      calls[0].url,
      'http://127.0.0.1:5684/fragment-cognitive/v1/withdrawals'
    );
    assert.equal(calls[0].method, 'POST');
    assert.equal(calls[0].headers.Accept, 'application/json');
    assert.equal(calls[0].headers['Content-Type'], 'application/json');
    assert.equal(calls[0].headers.Origin, 'app://obsidian.md');
    assert.equal(calls[0].headers['X-Fragment-Cognitive-Withdrawal'], '1');
    assert.equal('X-Fragment-Cognitive-Decision' in calls[0].headers, false);

    const body = JSON.parse(calls[0].body);
    assert.deepEqual(Object.keys(body).sort(), [
      'action',
      'expected_sequence',
      'fragment_id',
      'requester',
      'run_id',
      'withdrawal_id',
    ]);
    assert.equal(body.action, 'withdraw');
    assert.equal(body.run_id, 'cognitive-r1b-synthetic');
    assert.equal(body.fragment_id, 'synthetic-fragment-1');
    assert.equal(body.expected_sequence, 3);
    assert.match(body.withdrawal_id, /^[0-9a-f]{64}$/);
    // idempotency_key belongs only to the decision body, never to a withdrawal.
    assert.equal('idempotency_key' in body, false);
    const recomputed = await cognitiveClient.withdrawalIdempotencyKey(
      { runId: body.run_id, fragmentId: body.fragment_id, expectedSequence: 3 },
      body.requester
    );
    assert.equal(body.withdrawal_id, recomputed);
  });

  it('binds the backend fixed vector into the exact withdrawal_id field', async () => {
    // Cross-end regression: the same fixed inputs the backend test pins must
    // produce the backend's frozen digest, carried by withdrawal_id verbatim
    // — no other field name may carry it and none may be inferred.
    const calls = [];
    const client = cognitiveClient.createCognitiveDecisionClient({
      transport: async (request) => {
        calls.push(request);
        return {
          status: 202,
          json: {
            contract_version: '2',
            data: {
              action: 'withdraw',
              withdrawal_id:
                '902f072890921190820b187ff18e2a3099ed78fd1b61b55b314ef43ff3c0b7ec',
              status: 'withdrawn',
              note_status: 'absent',
              idempotent: false,
            },
          },
        };
      },
    });

    await client.submitWithdrawal({
      runId: 'run-fixed',
      fragmentId: 'fragment-fixed',
      expectedSequence: 7,
    });

    assert.equal(calls.length, 1);
    const body = JSON.parse(calls[0].body);
    assert.deepEqual(Object.keys(body).sort(), [
      'action',
      'expected_sequence',
      'fragment_id',
      'requester',
      'run_id',
      'withdrawal_id',
    ]);
    assert.equal(body.requester, 'nigo');
    assert.equal(
      body.withdrawal_id,
      '902f072890921190820b187ff18e2a3099ed78fd1b61b55b314ef43ff3c0b7ec'
    );
    assert.equal('idempotency_key' in body, false);
  });

  it('rejects invalid withdrawal input before the transport is called', async () => {
    const invalidInputs = [
      { runId: '', fragmentId: 'f', expectedSequence: 3 },
      { runId: 'r', fragmentId: '', expectedSequence: 3 },
      { runId: 'r', fragmentId: 'f', expectedSequence: 0 },
      { runId: 'r', fragmentId: 'f', expectedSequence: -1 },
      { runId: 'r', fragmentId: 'f', expectedSequence: 3.5 },
      { runId: 'r', fragmentId: 'f', expectedSequence: '3' },
      { runId: 'r\x1fr', fragmentId: 'f', expectedSequence: 3 },
    ];
    for (const input of invalidInputs) {
      const calls = [];
      const client = cognitiveClient.createCognitiveDecisionClient({
        transport: async (request) => {
          calls.push(request);
          return { status: 202, json: { contract_version: '2', data: {} } };
        },
      });
      await assert.rejects(
        client.submitWithdrawal(input),
        (error) => error.kind === 'invalid_arguments'
      );
      assert.equal(calls.length, 0);
    }
  });

  it('surfaces HTTP errors once, without any automatic retry', async () => {
    const calls = [];
    const client = cognitiveClient.createCognitiveDecisionClient({
      transport: async (request) => {
        calls.push(request);
        return {
          status: 409,
          json: { error: { code: 'stale_sequence', message: 'sequence moved' } },
        };
      },
    });
    await assert.rejects(
      client.submitWithdrawal({
        runId: 'cognitive-r1b-synthetic',
        fragmentId: 'synthetic-fragment-1',
        expectedSequence: 3,
      }),
      (error) =>
        error.kind === 'http_error' &&
        error.details.status === 409 &&
        error.details.code === 'stale_sequence'
    );
    assert.equal(calls.length, 1);
  });

  it('times out as unreachable without retrying', async () => {
    const calls = [];
    const client = cognitiveClient.createCognitiveDecisionClient({
      timeoutMs: 20,
      transport: async (request) => {
        calls.push(request);
        return new Promise(() => {});
      },
    });
    await assert.rejects(
      client.submitWithdrawal({
        runId: 'cognitive-r1b-synthetic',
        fragmentId: 'synthetic-fragment-1',
        expectedSequence: 3,
      }),
      (error) => error.kind === 'unreachable'
    );
    assert.equal(calls.length, 1);
  });

  it('fails closed on a withdrawal contract version mismatch', async () => {
    const client = cognitiveClient.createCognitiveDecisionClient({
      transport: async () => ({
        status: 202,
        json: { contract_version: '3', data: {} },
      }),
    });
    await assert.rejects(
      client.submitWithdrawal({
        runId: 'cognitive-r1b-synthetic',
        fragmentId: 'synthetic-fragment-1',
        expectedSequence: 3,
      }),
      (error) => error.kind === 'contract_mismatch'
    );
  });
});

describe('fragment cognitive R1-OC view withdrawal flow', () => {
  function buildView({ detailBody, withdrawImpl }) {
    const runCalls = [];
    const withdrawCalls = [];
    const client = {
      async runDetail(runId) {
        runCalls.push(runId);
        return detailBody;
      },
    };
    const cognitive = {
      async submitWithdrawal(input) {
        withdrawCalls.push(input);
        if (withdrawImpl) return withdrawImpl(input);
        return {
          httpStatus: 202,
          receipt: {
            action: 'withdraw',
            withdrawal_id: 'c'.repeat(64),
            status: 'withdrawn',
            note_status: 'withdrawn',
            idempotent: false,
          },
        };
      },
    };
    const view = new LoopConsoleView({}, client, null, null, null, null, cognitive);
    view.render = () => {};
    return { view, runCalls, withdrawCalls };
  }

  it('a successful withdrawal refreshes the real projection instead of fabricating it', async () => {
    const pendingBody = pendingEnvelope();
    const { view, runCalls, withdrawCalls } = buildView({ detailBody: pendingBody });
    view.state.detail = model.detailModel(keptEnvelope());
    view.state.selectedRunId = view.state.detail.runId;

    await view.submitCognitiveWithdrawal();

    assert.deepEqual(withdrawCalls, [
      {
        runId: 'cognitive-r1b-synthetic',
        fragmentId: 'synthetic-fragment-1',
        expectedSequence: 3,
      },
    ]);
    // The completion state comes from re-reading the real projection, never
    // from a local optimistic flag.
    assert.deepEqual(runCalls, ['cognitive-r1b-synthetic']);
    assert.equal(view.state.detail.cognitiveDraft.withdrawalPending, true);
    assert.equal(view.state.detail.cognitiveDraft.expectedSequence, 3);
  });

  it('continues a pending withdrawal with the receipt sequence, not the timeline maximum', async () => {
    const envelope = pendingEnvelope();
    envelope.run.timeline.push({ sequence: 9, event_type: 'state_transition' });
    const { view, withdrawCalls } = buildView({ detailBody: envelope });
    view.state.detail = model.detailModel(envelope);
    view.state.selectedRunId = view.state.detail.runId;

    await view.submitCognitiveWithdrawal();

    assert.equal(withdrawCalls.length, 1);
    assert.equal(withdrawCalls[0].expectedSequence, 3);
  });

  it('an HTTP error keeps the current surface and allows a manual retry', async () => {
    const { view, runCalls } = buildView({
      detailBody: keptEnvelope(),
      withdrawImpl: () => {
        const error = new Error('conflict');
        error.kind = 'http_error';
        error.details = { status: 409, code: 'stale_sequence' };
        throw error;
      },
    });
    view.state.detail = model.detailModel(keptEnvelope());
    view.state.selectedRunId = view.state.detail.runId;

    await view.submitCognitiveWithdrawal();

    assert.equal(view.state.cognitiveDecision.submitting, false);
    assert.equal(view.state.cognitiveDecision.error, 'stale_sequence');
    // The kept draft stays on screen; no refresh was triggered.
    assert.equal(view.state.detail.cognitiveDraft.kept, true);
    assert.equal(runCalls.length, 0);
  });

  it('a timeout keeps the current surface without retrying', async () => {
    let calls = 0;
    const { view, runCalls } = buildView({
      detailBody: keptEnvelope(),
      withdrawImpl: () => {
        calls += 1;
        const error = new Error('timeout');
        error.kind = 'unreachable';
        error.details = {};
        throw error;
      },
    });
    view.state.detail = model.detailModel(keptEnvelope());
    view.state.selectedRunId = view.state.detail.runId;

    await view.submitCognitiveWithdrawal();

    assert.equal(calls, 1);
    assert.equal(view.state.cognitiveDecision.error, 'unreachable');
    assert.equal(view.state.detail.cognitiveDraft.kept, true);
    assert.equal(runCalls.length, 0);
  });

  it('a late withdrawal response cannot pollute a newly selected run', async () => {
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    const { view, runCalls } = buildView({
      detailBody: pendingEnvelope(),
      withdrawImpl: () => gate,
    });
    view.state.detail = model.detailModel(keptEnvelope());
    view.state.selectedRunId = view.state.detail.runId;

    const pending = view.submitCognitiveWithdrawal();
    // Switch the selection while the withdrawal is still in flight.
    view.clearSelection();
    release({
      httpStatus: 202,
      receipt: { action: 'withdraw', status: 'withdrawn', idempotent: false },
    });
    await pending;

    // The late success must not write error/receipt state nor trigger a
    // refresh bound to the abandoned selection.
    assert.equal(view.state.selectedRunId, null);
    assert.equal(view.state.cognitiveDecision.error, null);
    assert.equal(view.state.cognitiveDecision.submitting, false);
    assert.equal(runCalls.length, 0);
  });

  it('does not submit a withdrawal when no withdrawable draft is selected', async () => {
    const { view, withdrawCalls } = buildView({ detailBody: keptEnvelope() });
    view.state.detail = model.detailModel(detailEnvelope()); // pending decision, not kept
    view.state.selectedRunId = view.state.detail.runId;

    await view.submitCognitiveWithdrawal();
    assert.equal(withdrawCalls.length, 0);
  });
});

// ---------------------------------------------------------------------------
// R1-OC revision 3: scripts/check.mjs cognitive guard regression. The guard
// must accept exactly the frozen pair of POSTs (loopback 5684 /decisions +
// /withdrawals, each with its own header) and reject every deviation — it
// must never be loosened to a bare postCount check.
// ---------------------------------------------------------------------------

describe('R1-OC revision 3 check.mjs cognitive guard', () => {
  it('accepts the frozen two-POST client and rejects every tampered variant', async () => {
    const { readFile } = await import('node:fs/promises');
    const { cognitiveClientGuardHits } = await import(
      path.join(ROOT, 'scripts', 'check.mjs')
    );
    assert.equal(typeof cognitiveClientGuardHits, 'function');

    const code = await readFile(
      path.join(ROOT, 'src', 'console', 'cognitive-decision-client.js'),
      'utf8'
    );
    assert.deepEqual(cognitiveClientGuardHits(code), []);

    const has = (hits, fragment) =>
      hits.some((hit) => hit.includes(fragment));

    // A third POST path under the same base is rejected by both the path
    // whitelist and the exact POST-site count.
    const extraPost =
      code +
      '\nconst sneaky = { url: `${COGNITIVE_DECISION_API_BASE_URL}/notes`, method: "POST" };\n';
    const extraHits = cognitiveClientGuardHits(extraPost);
    assert.ok(has(extraHits, 'write paths'), JSON.stringify(extraHits));
    assert.ok(has(extraHits, 'POST sites: 3'), JSON.stringify(extraHits));

    // Dropping the withdrawal POST (or renaming its path) is rejected.
    const noWithdrawal = code.replace(
      'COGNITIVE_DECISION_API_BASE_URL}/withdrawals',
      'COGNITIVE_DECISION_API_BASE_URL}/decisions'
    );
    const noWithdrawalHits = cognitiveClientGuardHits(noWithdrawal);
    assert.ok(has(noWithdrawalHits, 'write paths'), JSON.stringify(noWithdrawalHits));
    assert.ok(
      has(noWithdrawalHits, 'withdrawal POST missing X-Fragment-Cognitive-Withdrawal header'),
      JSON.stringify(noWithdrawalHits)
    );

    // A foreign URL anywhere in the module is rejected.
    const foreign = code + '\nconst leak = "https://example.com/steal";\n';
    assert.ok(
      has(cognitiveClientGuardHits(foreign), 'foreign URL'),
      'foreign URL must be flagged'
    );

    // A baseUrl override is rejected.
    const override = code + '\nfunction f(options) { return options.baseUrl; }\n';
    assert.ok(
      has(cognitiveClientGuardHits(override), 'baseUrl override'),
      'baseUrl override must be flagged'
    );

    // The decision header must never masquerade a withdrawal.
    const masquerade = code.replace(
      '[WITHDRAWAL_HEADER]: "1"',
      '[DECISION_HEADER]: "1"'
    );
    const masqueradeHits = cognitiveClientGuardHits(masquerade);
    assert.ok(
      has(masqueradeHits, 'withdrawal POST missing X-Fragment-Cognitive-Withdrawal header'),
      JSON.stringify(masqueradeHits)
    );
    assert.ok(
      has(masqueradeHits, 'withdrawal POST reuses the decision header'),
      JSON.stringify(masqueradeHits)
    );

    // A POST written with different quoting still counts toward the exact
    // site total — the guard cannot be evaded by quote style.
    const quoted =
      code + "\nconst sneaky = { method: 'POST' };\n";
    assert.ok(
      has(cognitiveClientGuardHits(quoted), 'POST sites: 3'),
      'single-quoted POST must be counted'
    );
  });
});

// ---------------------------------------------------------------------------
// Package B: structured draft view projection, section rendering, and the
// authoritative re-read after keep/reject.
// ---------------------------------------------------------------------------

// Matches the backend cognitive_draft_view projection shape verbatim
// (fragment_loop/cognitive_draft_view.py), with retrieval discovery and page
// evidence as two deliberately different texts.
function draftViewFixture() {
  return {
    view_version: 'fragment-cognitive-draft-view-v1',
    route: 'research',
    route_reason: '包含行业研究能力主张，需要研究路线',
    fragment_text: '合成研究团队计划用通用工具研究咖啡烘焙行业。',
    evidence_level: 'unverified',
    summary: {
      text: '合成研究团队计划用通用工具研究咖啡烘焙行业。',
      source_quote: '合成研究团队计划用通用工具研究咖啡烘焙行业。',
      transformation_basis: '摘要与合成原文一致',
    },
    semantic_expansion: {
      literal_facts: [
        { text: '合成研究团队', source_quote: '合成研究团队' },
        { text: '研究咖啡烘焙行业', source_quote: '研究咖啡烘焙行业' },
      ],
      inferences: [],
      uncertainties: ['通用工具的行业研究能力尚未被证据覆盖'],
    },
    research: {
      questions: ['通用工具是否能支持咖啡烘焙行业研究？'],
      search_dimensions: ['公开咖啡烘焙行业材料'],
      sources: [
        {
          source_id: 'S1',
          name: '合成公开材料：咖啡烘焙行业研究实践',
          locator: 'https://example.com/coffee-roasting-research',
          authenticity: 'unverified',
          relevance: 'direct',
          independence: 'non_independent',
          source_tier: 'secondary',
          source_type: 'industry_practice',
          published_at: '2026-07',
          acquisition_method: 'public_search_adapter',
          verification_basis: '单次 200 获取，不代表发布者身份或主张真实',
          scope: '仅证明当次获取返回 200，不证明主张真实',
          evidence_excerpt: '某虚构团队确实使用通用工具研究咖啡烘焙行业。',
          discovery: {
            title: '合成公开材料：咖啡烘焙行业研究实践',
            excerpt: '搜索摘要：合成公开材料声称某虚构团队使用通用工具研究咖啡烘焙行业。',
            published_at_claim: '2026-07',
            source_type_hint: 'industry_practice',
            query_refs: ['Q1'],
          },
        },
        {
          source_id: 'S2',
          name: '合成公开材料：通用工具行业适配讨论',
          locator: 'https://example.org/general-tool-industry-fit',
          authenticity: 'unverified',
          relevance: 'indirect',
          independence: 'non_independent',
          source_tier: 'secondary',
          source_type: 'media_report',
          published_at: '2026-06',
          acquisition_method: 'public_search_adapter',
          verification_basis: '单次 200 获取，不代表发布者身份或主张真实',
          scope: '仅证明当次获取返回 200，不证明主张真实',
          evidence_excerpt: '通用工具在咖啡烘焙行业的适配情况。',
          discovery: null,
        },
      ],
      counter_evidence_search: {
        status: 'not_found',
        scope: '公开来源卡中未发现相反观点',
        findings: [],
      },
      v1_claims: [
        {
          claim_id: 'C1',
          claim_kind: 'general_fact',
          text: '通用工具已经具备咖啡烘焙行业研究能力',
          verdict: 'not_covered',
          confidence: 'low',
          claim_derivation: '从合成原文的工具计划拆出待验证能力主张',
          verdict_basis: '公开来源本阶段恒为 unverified，不能覆盖该能力主张',
          evidence_refs: [],
          independent_verification: {
            status: 'not_verified',
            mode: 'none',
            basis: '没有已核验公开证据，不能独立验证',
          },
        },
      ],
      v2_revisions: [
        {
          claim_id: 'C1',
          revision_status: 'unchanged',
          deviation: 'V1 已明确标记为未覆盖',
          revision_reason: '公开来源未核验，不能改变判定',
          revised_text: '通用工具已经具备咖啡烘焙行业研究能力',
        },
      ],
    },
    perspectives: [
      {
        perspective: 'memory',
        summary: 'memory 视角依据一条已确认合成用户事实与一条明确画像推断',
        association: '已确认事实要求研究路线，画像推断提示先核对证据',
        conflicts: [],
        value: '保留待验证问题',
        uncertainty: '公开来源真实性未核验',
        materials: [
          {
            material_type: 'confirmed_user_fact',
            text: '合成用户曾在合成对话中明确确认：咖啡烘焙行业研究类碎片一律走研究路线',
            confirmation_ref: 'synthetic/dialogue/package-c-confirm-001',
          },
          {
            material_type: 'profile_inference',
            text: '合成画像推断：合成用户可能偏好先核对行业证据再决定是否深入',
            basis: '合成交互记录中合成用户多次要求先展示证据再作决定',
            uncertainty: '该偏好可能只适用于行业研究类碎片，强度与适用范围未知',
          },
        ],
      },
      {
        perspective: 'knowledge_base',
        summary: 'knowledge_base 视角依据一条合成可追溯知识记录',
        association: '合成调研提纲笔记与咖啡烘焙研究主题衔接',
        conflicts: [],
        value: '保留待验证问题',
        uncertainty: '公开来源真实性未核验',
        materials: [
          {
            material_type: 'obsidian_record',
            text: '合成知识库中有一份虚构的咖啡烘焙行业调研提纲笔记可以衔接',
            record_ref: 'synthetic/notes/package-c-coffee-research.md',
          },
        ],
      },
      {
        perspective: 'frontier',
        summary: 'frontier 视角只依据公开来源卡与合成原文',
        association: '只与合成原文及公开来源卡相关',
        conflicts: [],
        value: '保留待验证问题',
        uncertainty: '公开来源真实性未核验',
        materials: [],
      },
    ],
    synthesis: {
      value: '可作为后续研究问题',
      weakest_link: '公开来源真实性未核验',
      conflicts: [],
      open_questions: ['通用工具的行业研究能力是否存在？'],
      next_step: '等待人工决定是否保留',
      credibility: 'insufficient',
      credibility_basis: '公开来源未核验真实性，唯一主张未被证据覆盖',
      claim_counts: {
        supported: 0,
        partially_supported: 0,
        contradicted: 0,
        not_covered: 1,
      },
    },
  };
}

function structuredEnvelope() {
  const envelope = detailEnvelope();
  envelope.run.cognitive_draft_view = draftViewFixture();
  return envelope;
}

describe('fragment cognitive Package B structured draft view', () => {
  it('projects the structured view and renders every required section', () => {
    const detail = model.detailModel(structuredEnvelope());
    const draft = detail.cognitiveDraft;
    assert.ok(draft);
    assert.ok(draft.view);
    assert.equal(draft.view.route, 'research');
    assert.equal(draft.view.evidenceLevel, 'unverified');
    assert.equal(draft.view.research.v1_claims[0].verdict, 'not_covered');
    assert.equal(draft.view.research.sources.length, 2);
    assert.equal(draft.view.research.sources[1].discovery, null);

    const root = new FakeEl('div');
    renderConsole(root, stateFor(detail), handlers);
    for (const label of [
      '原始碎片',
      '路线判断',
      '原文摘要',
      '语义展开',
      '字面信息',
      '真正未知项',
      '研究问题与检索维度',
      '来源清单（真实性均未验证）',
      '检索发现（搜索摘要，不是页面证据）',
      '页面证据（获取页面逐字摘录）',
      '相反观点检索',
      'V1 主张与判定',
      '未覆盖（not_covered）',
      'V2 逐项复核（回到原文校准）',
      '三个视角',
      '综合判断（仍为未验证草稿）',
      '仍然未知',
      '下一步：等待人工决定是否保留',
      '尚未经过真实来源验证',
      '完整草稿 Markdown（决定绑定的逐字原文）',
    ]) {
      assert.ok(root.text.includes(label), `missing section: ${label}`);
    }
    // Retrieval discovery and page evidence stay in two distinct blocks.
    const discovery = root.querySelector('.lc-cognitive-discovery');
    const evidence = root.querySelector('.lc-cognitive-evidence');
    assert.ok(discovery.text.includes('搜索摘要：合成公开材料声称'));
    assert.ok(!discovery.text.includes('某虚构团队确实使用通用工具研究咖啡烘焙行业。'));
    assert.ok(evidence.text.includes('某虚构团队确实使用通用工具研究咖啡烘焙行业。'));
    assert.ok(!evidence.text.includes('搜索摘要：合成公开材料声称'));
    // The second source honestly shows the missing discovery record.
    assert.ok(root.text.includes('无检索发现记录'));
    // Package C: the three material categories render in their own groups
    // with their honest Chinese boundaries, inside the matching perspectives.
    for (const label of [
      '已确认用户事实（仅表示用户确认，不代表外部真实性）',
      '可追溯知识记录（本包为合成记录，未访问真实知识库）',
      '用户画像推断（推断，不是用户事实）',
      '合成用户曾在合成对话中明确确认：咖啡烘焙行业研究类碎片一律走研究路线'
        + '（确认引用：synthetic/dialogue/package-c-confirm-001）',
      '合成知识库中有一份虚构的咖啡烘焙行业调研提纲笔记可以衔接'
        + '（记录引用：synthetic/notes/package-c-coffee-research.md）',
    ]) {
      assert.ok(root.text.includes(label), `missing material label: ${label}`);
    }
    const inferenceItem = root.querySelector('.lc-cognitive-inference-item');
    assert.ok(inferenceItem);
    assert.ok(inferenceItem.text.includes('合成画像推断：合成用户可能偏好先核对行业证据'));
    assert.ok(inferenceItem.text.includes('依据：合成交互记录中合成用户多次要求先展示证据再作决定'));
    assert.ok(inferenceItem.text.includes('不确定性：该偏好可能只适用于行业研究类碎片'));
    // The frontier perspective carries no personal materials, and nothing in
    // the UI ever claims a verified asset or a proven user fact.
    assert.ok(!root.text.includes('已验证资产'));
    assert.ok(!root.text.includes('用户事实已证明'));
    const materialGroups = root.querySelectorAll('.lc-cognitive-material-group');
    assert.equal(materialGroups.length, 3);
    const frontierCard = root.querySelectorAll('.lc-cognitive-perspective')[2];
    assert.ok(!frontierCard.querySelector('.lc-cognitive-material-group'));
    // The bound markdown stays available inside the collapsed disclosure.
    const markdownPre = root.querySelector('details pre.lc-cognitive-markdown');
    assert.ok(markdownPre);
    assert.ok(markdownPre.text.includes(MARKDOWN));
    // Decision surface stays intact next to the structured sections.
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 1);
    assert.equal(root.querySelectorAll('.lc-cognitive-reject').length, 1);
  });

  it('fails the whole draft closed when a present view is invalid', () => {
    for (const tamper of [
      (view) => { view.view_version = 'fragment-cognitive-draft-view-v0'; },
      (view) => { view.fragment_text = ''; },
      (view) => { view.evidence_level = 'verified'; },
      (view) => { view.route = 'direct'; },
      (view) => { delete view.research.sources[0].evidence_excerpt; },
      (view) => { view.research.sources[0].discovery = { title: 'x' }; },
      (view) => { delete view.research.v1_claims[0].independent_verification; },
      (view) => { view.synthesis.claim_counts = { not_covered: 1 }; },
      (view) => { delete view.summary.transformation_basis; },
      // Package C material shape violations fail the whole draft closed.
      (view) => { delete view.perspectives[0].materials; },
      (view) => { view.perspectives[0].materials = 'junk'; },
      (view) => { view.perspectives[0].materials[1].confirmation_ref = 'synthetic/x'; },
      (view) => { delete view.perspectives[0].materials[1].basis; },
      (view) => { view.perspectives[0].materials[0].inferred = false; },
      (view) => { view.perspectives[0].materials[0].unknown_field = 'x'; },
      (view) => { view.perspectives[2].materials.push({ material_type: 'unknown', text: 'x' }); },
      (view) => { view.perspectives[0].materials[1].text = ''; },
    ]) {
      const envelope = structuredEnvelope();
      tamper(envelope.run.cognitive_draft_view);
      assert.equal(model.detailModel(envelope).cognitiveDraft, null);
    }
    const notAnObject = detailEnvelope();
    notAnObject.run.cognitive_draft_view = 'junk';
    assert.equal(model.detailModel(notAnObject).cognitiveDraft, null);
  });

  function buildDecisionView({ detailBody, submitImpl }) {
    const runCalls = [];
    const submitCalls = [];
    const client = {
      async runDetail(runId) {
        runCalls.push(runId);
        return typeof detailBody === 'function' ? detailBody() : detailBody;
      },
    };
    const cognitive = {
      async submitDecision(input) {
        submitCalls.push(input);
        if (submitImpl) return submitImpl(input);
        return {
          decision: {
            decision: input.decision,
            decision_id: 'a'.repeat(64),
            status: 'recorded',
            thought_category: input.thoughtCategory,
          },
        };
      },
    };
    const view = new LoopConsoleView({}, client, null, null, null, null, cognitive);
    view.render = () => {};
    view.state.detail = model.detailModel(structuredEnvelope());
    view.state.selectedRunId = view.state.detail.runId;
    return { view, runCalls, submitCalls };
  }

  it('a successful keep re-reads the authoritative projection next to the receipt', async () => {
    const { view, runCalls, submitCalls } = buildDecisionView({
      detailBody: keptEnvelope(),
    });
    view.handlers.onCognitiveCategoryChange('行业研究');
    await view.submitCognitiveDraftDecision('keep_draft');

    assert.equal(submitCalls.length, 1);
    // The terminal state comes from the re-read projection, and the receipt
    // notice survives next to the authoritative kept card.
    assert.deepEqual(runCalls, ['cognitive-r1b-synthetic']);
    assert.equal(view.state.cognitiveDecision.lastDecision.decision, 'keep_draft');
    assert.equal(view.state.cognitiveDecision.lastDecision.thoughtCategory, '行业研究');
    assert.equal(view.state.detail.cognitiveDraft.kept, true);
    view.state.status = 'ready';
    const root = new FakeEl('div');
    renderConsole(root, view.state, view.handlers);
    assert.ok(root.text.includes('已记录：保留为未验证草稿（分类：行业研究）'));
    assert.ok(root.text.includes('该草稿已保留，仍为未验证草稿，不是正式资产。'));
    assert.equal(root.querySelectorAll('.lc-cognitive-withdraw').length, 1);
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 0);
  });

  it('a stale keep receipt never lands when the user switches away and back mid-re-read', async () => {
    const pendingReads = [];
    const client = {
      runDetail(runId) {
        return new Promise((resolve) => pendingReads.push({ runId, resolve }));
      },
    };
    const cognitive = {
      async submitDecision(input) {
        return {
          decision: {
            decision: input.decision,
            decision_id: 'a'.repeat(64),
            status: 'recorded',
            thought_category: input.thoughtCategory,
          },
        };
      },
    };
    const view = new LoopConsoleView({}, client, null, null, null, null, cognitive);
    view.render = () => {};
    view.state.detail = model.detailModel(structuredEnvelope());
    view.state.selectedRunId = view.state.detail.runId;
    view.handlers.onCognitiveCategoryChange('行业研究');

    const tick = () => new Promise((resolve) => setImmediate(resolve));
    const submit = view.submitCognitiveDraftDecision('keep_draft');
    await tick();
    // The decision succeeded and the authoritative re-read is in flight.
    assert.equal(pendingReads.length, 1);

    // The user switches away and back to the SAME run while the first
    // re-read is still pending; the newer selection owns the generation.
    view.clearSelection();
    const reselect = view.selectRun('cognitive-r1b-synthetic');
    assert.equal(pendingReads.length, 2);
    assert.deepEqual(
      pendingReads.map((read) => read.runId),
      ['cognitive-r1b-synthetic', 'cognitive-r1b-synthetic']
    );

    // The newer re-read lands first, then the stale one — the worst order.
    pendingReads[1].resolve(keptEnvelope());
    await tick();
    pendingReads[0].resolve(keptEnvelope());
    await submit;
    await reselect;

    assert.equal(view.state.selectedRunId, 'cognitive-r1b-synthetic');
    assert.equal(view.state.detail.cognitiveDraft.kept, true);
    // The old receipt must not leak into the new selection generation.
    assert.equal(view.state.cognitiveDecision.lastDecision, null);
  });

  it('a successful reject re-reads the authoritative projection and the card closes', async () => {
    const rejectedBody = detailEnvelope({
      status: 'cancelled',
      display_state: 'closed',
      stop_reason: 'cognitive_result_rejected',
    });
    rejectedBody.run.eval_results.cognitive_decision = 'reject';
    rejectedBody.run.eval_results.content_lifecycle = 'rejected';
    const { view, runCalls } = buildDecisionView({ detailBody: rejectedBody });
    await view.submitCognitiveDraftDecision('reject');

    assert.deepEqual(runCalls, ['cognitive-r1b-synthetic']);
    assert.equal(view.state.cognitiveDecision.lastDecision.decision, 'reject');
    assert.equal(view.state.detail.cognitiveDraft, null);
    view.state.status = 'ready';
    const root = new FakeEl('div');
    renderConsole(root, view.state, view.handlers);
    assert.ok(root.text.includes('已记录：拒绝该认知结果。'));
    assert.equal(root.querySelectorAll('.lc-cognitive-keep').length, 0);
  });

  it('an unreachable decision service keeps the draft and shows the honest error', async () => {
    const { view } = buildDecisionView({
      detailBody: keptEnvelope(),
      submitImpl: () => {
        const error = new Error('无法连接草稿决定服务');
        error.kind = 'unreachable';
        throw error;
      },
    });
    view.handlers.onCognitiveCategoryChange('行业研究');
    await view.submitCognitiveDraftDecision('keep_draft');

    assert.equal(view.state.cognitiveDecision.submitting, false);
    assert.equal(view.state.cognitiveDecision.error, 'unreachable');
    assert.equal(view.state.cognitiveDecision.lastDecision, null);
    // Nothing was re-read or fabricated: the draft stays awaiting a decision.
    assert.equal(view.state.detail.cognitiveDraft.awaitingDecision, true);
  });
});
