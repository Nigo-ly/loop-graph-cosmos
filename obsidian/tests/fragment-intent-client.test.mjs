import { describe, it } from 'node:test';
import { effectiveScope } from './helpers/effective-scope.mjs';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const {
  ALIGNMENTS_URL,
  FragmentIntentApiError,
  createFragmentIntentClient,
  fragmentInputDigest,
  knowledgePublicationOf,
  effectiveExecutionScopeOf,
} = require(path.join(ROOT, 'src', 'console', 'fragment-intent-client.js'));

function alignment(overrides = {}) {
  return {
    alignment_id: 'align:0123456789abcdef01234567',
    fragment_id: 'fragment-1',
    case_id: 'case:0123456789abcdef01234567',
    episode_id: 'episode:0123456789abcdef01234567',
    title: '一个待理解的技术碎片',
    status: 'suggested',
    sequence: 1,
    revision: 1,
    input_digest: 'a'.repeat(64),
    reasoning: '需要先核实，再判断个人价值。',
    plan: '先走最短可靠路径；只有复杂时才升级。',
    expected_result: '可信结论与下一步。',
    exclusions: ['不安装'],
    suggested_intents: ['verify', 'evaluate_relevance'],
    dynamic_intents: [{ id: 'local_fit', label: '判断本机适用性', basis: '问题包含本地部署' }],
    recommended_route: 'verify',
    memory_basis: [{ label: '既有技术核验案例', maturity: 'reusable' }],
    execution_scope: {
      capabilities: ['本地分析'], external_scope: ['公开来源'],
      model_call_cap: 0, cost_cap_cny: 0, side_effect: 'none',
    },
    ...overrides,
  };
}

function envelope(data, status = 200) {
  return { status, json: { contract_version: '2', data, error: null } };
}

describe('fragment intent client', () => {
  it('preserves current subscription configuration separately from old proposals and actual receipts', async () => {
    const scope = effectiveScope();
    const execution = { run_id: 'exec:scope', fragment_id: 'fragment-1', status: 'running',
      current_node: 'research', route: 'verify', stop_reason: '', updated_at: '2026-09-09T00:00:00Z',
      result_digest: null, harvest: [], result: null, subscription_selected: true, effective_execution_scope: scope };
    const item = alignment({ effective_execution_scope: scope, execution,
      execution_scope_basis: 'original_alignment_proposal', exclusions_basis: 'original_alignment_proposal' });
    const calls = [];
    const client = createFragmentIntentClient({ transport: async request => { calls.push(request); return envelope([item, alignment()]); } });
    const [read, old] = await client.list();
    assert.deepEqual(read.effective_execution_scope, scope);
    assert.deepEqual(read.execution.effective_execution_scope, scope);
    assert.equal(read.execution_scope.model_call_cap, 0);
    assert.equal(effectiveExecutionScopeOf(read).model_call_cap, 10);
    assert.equal(effectiveExecutionScopeOf(read).observed_model_provider, 'codex_subscription');
    assert.equal(effectiveExecutionScopeOf(old), null);
    assert.equal(effectiveExecutionScopeOf({ ...read, execution: { ...execution, subscription_selected: false } }), null);
    assert.equal(effectiveExecutionScopeOf({ ...read, effective_execution_scope: null }), null);
    assert.deepEqual(effectiveExecutionScopeOf({ execution }), scope);
    assert.equal(calls.length, 1); assert.equal(calls[0].method, 'GET');
  });

  it('rejects malformed effective scopes in either projection without weakening legacy scope validation', async () => {
    for (const change of [{ basis: 'original_alignment_proposal' }, { model_provider: 'deepseek' },
      { model_name: 'unverified-model' }, { model_call_cap: -1 }, { tool_call_cap: 1.5 },
      { repository_trial_allowed: 'yes' }, { capabilities: ['bad\ntext'] }, { knowledge_publication_status: 'saved' }]) {
      const client = createFragmentIntentClient({ transport: async () => envelope([alignment({ effective_execution_scope: effectiveScope(change) })]) });
      await assert.rejects(client.list(), /当前套餐执行范围无效/);
    }
    const nested = alignment({ execution: { run_id: 'exec:scope', fragment_id: 'fragment-1', status: 'running',
      current_node: '', route: 'verify', stop_reason: '', updated_at: '2026-09-09T00:00:00Z', harvest: [],
      effective_execution_scope: effectiveScope({ exclusions: null }) } });
    await assert.rejects(createFragmentIntentClient({ transport: async () => envelope([nested]) }).list(), /当前套餐执行范围无效/);
    const legacy = alignment(); legacy.execution_scope.model_call_cap = 10;
    await assert.rejects(createFragmentIntentClient({ transport: async () => envelope([legacy]) }).list(), /处理范围无效/);
  });

  it('binds proposal to a deterministic digest without sending note text', async () => {
    const calls = [];
    const client = createFragmentIntentClient({
      transport: async (request) => {
        calls.push(request);
        return envelope(alignment(), 201);
      },
    });
    const digest = await fragmentInputDigest('raw secret', 'organized secret');
    const canonical = JSON.stringify({ raw_text: 'raw secret', organized_text: 'organized secret' });
    assert.equal(digest, createHash('sha256').update(canonical).digest('hex'));
    await client.propose({ fragmentId: 'fragment-1', inputDigest: digest });
    assert.equal(calls[0].url, ALIGNMENTS_URL);
    assert.equal(calls[0].headers.Origin, 'app://obsidian.md');
    assert.equal(calls[0].headers['X-Fragment-Alignment'], '1');
    const body = JSON.parse(calls[0].body);
    assert.deepEqual(Object.keys(body).sort(), ['fragment_id', 'input_digest', 'requester']);
    assert.equal(calls[0].body.includes('raw secret'), false);
    assert.equal(calls[0].body.includes('organized secret'), false);
  });

  it('submits one bound multi-select decision with supplement', async () => {
    const item = alignment();
    const calls = [];
    const client = createFragmentIntentClient({
      transport: async (request) => {
        calls.push(request);
        return envelope(alignment({
          status: 'passed', route: 'verify',
          decision: { action: 'confirm', intents: ['verify', 'local_fit'], supplement: '优先核实硬件' },
        }), 201);
      },
    });
    await client.decide(item, {
      action: 'confirm', intents: ['verify', 'local_fit'], supplement: '优先核实硬件',
    });
    assert.equal(calls[0].url, `${ALIGNMENTS_URL}/${encodeURIComponent(item.alignment_id)}/decisions`);
    assert.equal(calls[0].headers['X-Fragment-Alignment-Decision'], '1');
    const body = JSON.parse(calls[0].body);
    assert.equal(body.revision, 1);
    assert.equal(body.input_digest, 'a'.repeat(64));
    assert.deepEqual(body.intents, ['verify', 'local_fit']);
    assert.equal(body.supplement, '优先核实硬件');
  });

  it('rejects unbound intents and malformed memory projections before rendering', async () => {
    const item = alignment();
    const client = createFragmentIntentClient({ transport: async () => envelope([]) });
    await assert.rejects(
      () => client.decide(item, { action: 'confirm', intents: ['invented'], supplement: '' }),
      (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_arguments',
    );
    const malformed = createFragmentIntentClient({
      transport: async () => envelope([alignment({
        memory_basis: [{ label: '<img src=x>', maturity: 'secret' }],
      })]),
    });
    await assert.rejects(
      () => malformed.list(),
      (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_response',
    );
  });

  it('retains explicit raw origin and the full original question through the read-only list', async () => {
    const original = '公开链接问题与方法限制。'.repeat(400);
    const item = alignment({ source_origin: 'raw_capture', fragment_title_source: 'raw_capture', goal: original });
    const client = createFragmentIntentClient({ transport: async () => envelope([item]) });
    const [read] = await client.list();
    assert.equal(read.source_origin, 'raw_capture'); assert.equal(read.goal, original);
    assert.equal(Object.hasOwn(read, 'organized_at'), false);
  });

  it('lists only contract-v2 alignments and fails closed on contract drift', async () => {
    const client = createFragmentIntentClient({ transport: async () => envelope([alignment()]) });
    assert.equal((await client.list()).length, 1);
    const drift = createFragmentIntentClient({
      transport: async () => ({ status: 200, json: { contract_version: '3', data: [] } }),
    });
    await assert.rejects(() => drift.list(), /契约不匹配/);
  });

  it('accepts a complete governed research scope and rejects partial model policy', async () => {
    const governedScope = {
      capabilities: ['公开来源发现', '安全网页抓取', '受治理研究合成'],
      external_scope: ['官方资料', 'GitHub、模型社区与可信技术社区'],
      model_call_cap: 1,
      cost_cap_cny: 2,
      side_effect: '只读，无外部写入',
      model_provider: 'deepseek',
      model_name: 'deepseek-v4-pro',
      write_scope: ['Loop Checkpoint 研究结果'],
    };
    const client = createFragmentIntentClient({
      transport: async () => envelope([alignment({ execution_scope: governedScope })]),
    });
    assert.deepEqual((await client.list())[0].execution_scope, governedScope);

    const partial = createFragmentIntentClient({
      transport: async () => envelope([alignment({
        execution_scope: { ...governedScope, model_name: '' },
      })]),
    });
    await assert.rejects(
      () => partial.list(),
      (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_response',
    );
  });

  it('rejects malformed execution result projections before rendering', async () => {
    const malformed = createFragmentIntentClient({
      transport: async () => envelope([alignment({
        status: 'passed', route: 'verify',
        decision: { action: 'confirm', intents: ['verify'], supplement: '' },
        execution: {
          run_id: 'exec:fragment-intent:0123456789abcdef01234567',
          fragment_id: 'fragment-1', status: 'passed', current_node: 'harvest',
          route: 'verify', stop_reason: 'completed', updated_at: '2026-08-09T00:00:00+00:00',
          result_digest: 'b'.repeat(64), harvest: [],
          result: {
            summary: '安全摘要', unknowns: [], next_checks: [],
            needs_escalation: false, escalation_reason: '', model_calls: -1, tool_calls: 0,
          },
        },
      })]),
    });
    await assert.rejects(
      () => malformed.list(),
      (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_response',
    );
  });

  it('creates a bound child episode and a separate Graph escalation proposal', async () => {
    const parent = alignment({
      status: 'passed', route: 'verify',
      decision: { action: 'confirm', intents: ['verify'], supplement: '' },
      execution: {
        run_id: 'exec:fragment-intent:0123456789abcdef01234567', fragment_id: 'fragment-1',
        status: 'passed', current_node: 'harvest', route: 'verify', stop_reason: 'completed',
        updated_at: '2026-08-09T00:00:00+00:00', result_digest: 'b'.repeat(64), harvest: [],
      },
    });
    const calls = [];
    const client = createFragmentIntentClient({
      transport: async (request) => {
        calls.push(request);
        if (request.url.includes('/continuations')) return envelope(alignment({
          alignment_id: 'align:child', episode_id: 'episode:child',
        }), 201);
        return envelope({
          escalation_id: 'escalation:x', status: 'proposed',
          reason: '需要复杂工作流', capability_status: 'template_required',
          graph_run_created: false,
        });
      },
    });
    await client.continueEpisode(parent, '继续处理部署故障');
    await client.escalate(parent);
    const continuation = JSON.parse(calls[0].body);
    assert.equal(continuation.parent_episode_id, parent.episode_id);
    assert.equal(continuation.source_result_digest, 'b'.repeat(64));
    assert.match(continuation.continuation_id, /^continuation:[0-9a-f]{24}$/);
    assert.equal(calls[0].headers['X-Fragment-Episode-Continuation'], '1');
    const escalation = JSON.parse(calls[1].body);
    assert.match(escalation.escalation_id, /^escalation:[0-9a-f]{24}$/);
    assert.equal(calls[1].headers['X-Fragment-Alignment-Escalation'], '1');
  });
});


function researchAlignment() {
  return alignment({
    status: 'passed', route: 'verify',
    decision: { action: 'confirm', intents: ['verify'], supplement: '' },
    execution: {
      run_id: 'exec:fragment-intent:0123456789abcdef01234567',
      fragment_id: 'fragment-1', status: 'passed', current_node: 'harvest', route: 'verify',
      stop_reason: 'completed', updated_at: '2026-09-08T13:08:22+00:00',
      result_digest: 'b'.repeat(64), harvest: [],
      result: {
        summary: '已形成有边界的结论', recommendation: '限定使用',
        unknowns: [], next_checks: [], needs_escalation: false, escalation_reason: '', model_calls: 2, tool_calls: 3,
        confirmed: [{ claim: '公开文档有正文提取说明', evidence_ids: ['ev-source'] }],
        conflicts: [], claims: [{ claim: '正文提取', evidence_id: 'ev-source', relation: 'supports' }],
        answer_markdown: '# 完整判断\n保留局限',
        coverage: [{ question: '能否提取正文？', answer: '依据当前材料可以', evidence_ids: ['ev-source'], status: 'answered' }],
        agent_usage: { when_to_use: '公开网页正文提取', steps: ['先核实页面范围'], limitations: ['不证明任意网站适用'] },
        topic: { category: '技术', subcategory: '内容处理', title: '网页提取', existing_topic_id: '' },
      },
    },
  });
}

function resultClient(item) {
  return createFragmentIntentClient({ transport: async () => envelope([item]) });
}

describe('subscription research result projection contract', () => {
  it('returns all 20 mixed alignments with the live Mozilla nine-confirmation shape without truncating', async () => {
    const mozilla = researchAlignment();
    mozilla.fragment_id = mozilla.execution.fragment_id = '2026-09-08-public-github-mozilla-readability';
    mozilla.alignment_id = 'align:caaa691c5df8ed3d0fcde82b';
    mozilla.execution.run_id = 'exec:fragment-intent:e30209acf023b265ac9f0fb7';
    // Live GET 2026-09-08 21:08: nine valid claims. Text redacted; lengths and citation cardinalities preserved.
    mozilla.execution.result.confirmed = [
      [84, ['ev-24632d7a90a4', 'ev-34b0d10c45e0']], [96, ['ev-24632d7a90a4']],
      [60, ['ev-24632d7a90a4']], [70, ['ev-34b0d10c45e0']],
      [79, ['ev-26f4d241f297', 'ev-24632d7a90a4']], [67, ['ev-24632d7a90a4']],
      [45, ['ev-24632d7a90a4']], [57, ['ev-26f4d241f297']], [48, ['ev-26f4d241f297']],
    ].map(([length, evidence_ids], index) => ({ claim: `${index}：${'据'.repeat(length - 2)}`, evidence_ids }));
    const items = Array.from({ length: 19 }, (_, index) => ({
      ...(index % 2 ? researchAlignment() : alignment()), alignment_id: `align:mixed-${index}`,
    }));
    items.splice(7, 0, mozilla);
    const client = createFragmentIntentClient({ transport: async () => envelope(items) });
    const listed = await client.list();
    assert.equal(listed.length, 20);
    assert.deepEqual(listed, items);
    assert.equal(listed[7].execution.result.confirmed.length, 9);
    assert.equal(listed[7].execution.result.confirmed[8].claim.length, 48);
    // An actually invalid entry is still rejected, not silently omitted to make the list look complete.
    mozilla.execution.result.confirmed[8].claim = { unsafe: 'not text' };
    await assert.rejects(client.list(), /研究已确认结论无效/);
  });

  it('preserves 24-item nested results and 24000-code-point text allowed by the backend validator', async () => {
    const item = researchAlignment(); const result = item.execution.result;
    const list = (value) => Array.from({ length: 24 }, () => structuredClone(value));
    const longText = '结'.repeat(24000);
    result.summary = '🌍'.repeat(24000); // Python len counts Unicode code points, not JavaScript code units.
    result.recommendation = longText;
    result.unknowns = list(longText);
    result.confirmed = list({ claim: longText, evidence_ids: list('ev-source') });
    result.conflicts = list({ topic: longText, dimensions: list(longText), evidence_ids: list('ev-source') });
    result.claims = list({ claim: longText, evidence_id: 'ev-source', relation: 'partially_supports' });
    result.answer_markdown = '# 表格\n| 问题 | 回答 |\n| --- | --- |\n| a | b |';
    result.coverage = list({ question: longText, answer: longText, evidence_ids: list('ev-source'), status: 'answered' });
    result.agent_usage = { when_to_use: longText, steps: list(longText), limitations: list(longText) };
    result.topic = { category: longText, subcategory: longText, title: longText, existing_topic_id: '' };
    assert.deepEqual((await resultClient(item).list())[0], item);
    result.coverage[0] = { question: '尚未验证', answer: '', evidence_ids: [], status: 'unknown' };
    assert.deepEqual((await resultClient(item).list())[0], item);
  });

  it('rejects the 25th item at every public result array boundary', async () => {
    const mutations = [
      r => r.unknowns = Array(25).fill('未知'),
      r => r.confirmed = Array(25).fill(r.confirmed[0]),
      r => r.confirmed[0].evidence_ids = Array(25).fill('ev-source'),
      r => r.conflicts = Array(25).fill({ topic: '差异', dimensions: [], evidence_ids: ['ev-source'] }),
      r => r.conflicts = [{ topic: '差异', dimensions: Array(25).fill('维度'), evidence_ids: ['ev-source'] }],
      r => r.conflicts = [{ topic: '差异', dimensions: [], evidence_ids: Array(25).fill('ev-source') }],
      r => r.claims = Array(25).fill(r.claims[0]),
      r => r.coverage = Array(25).fill(r.coverage[0]),
      r => r.coverage[0].evidence_ids = Array(25).fill('ev-source'),
      r => r.agent_usage.steps = Array(25).fill('步骤'),
      r => r.agent_usage.limitations = Array(25).fill('限制'),
    ];
    for (const mutate of mutations) {
      const item = researchAlignment(); mutate(item.execution.result);
      await assert.rejects(resultClient(item).list(), error => error.kind === 'invalid_response');
    }
  });

  it('keeps string bounds, object shapes, citation requirements and relation enums fail-closed', async () => {
    const mutations = [
      r => r.summary = '🌍'.repeat(24001), r => r.recommendation = '字'.repeat(24001),
      r => r.summary = 'bad\0text', r => r.confirmed[0].claim = 'bad\x1btext',
      r => r.confirmed[0].claim = '', r => r.confirmed[0].evidence_ids = [],
      r => r.confirmed[0].evidence_ids = [false], r => r.unknowns = ['字'.repeat(24001)],
      r => r.claims[0].relation = 'verified', r => r.claims[0].evidence_id = 3,
      r => r.conflicts = [{ topic: '冲突', dimensions: {}, evidence_ids: ['ev-source'] }],
      r => r.answer_markdown = '字'.repeat(24001), r => r.coverage[0].evidence_ids = [],
      r => r.coverage[0].status = 'verified', r => r.coverage[0].answer = {},
      r => r.agent_usage.steps = [42], r => r.agent_usage = [],
      r => r.topic.title = {}, r => r.topic.existing_topic_id = false,
      r => r.next_checks = Array(9).fill('下步'), // Legacy execution fields retain their original contract.
    ];
    for (const mutate of mutations) {
      const item = researchAlignment(); mutate(item.execution.result);
      await assert.rejects(resultClient(item).list(), error => error.kind === 'invalid_response');
    }
  });
});


describe('read-only knowledge access', () => {
  const id = `knowledge-${'a'.repeat(24)}`;
  const note = { knowledge_id: id, revision: 2, title: 'Archify 适配结论', path: 'Loop知识库/Archify.md', result: { summary: '限定使用' }, evidence: [] };
  const topic = { topic_id: `topic-${'b'.repeat(24)}`, category: '技术', subcategory: 'Agent 工具', title: '系统可视化', notes: [note] };

  it('catalog/search/read use only local GET with encoded query and preserve complete results', async () => {
    const calls = [];
    const client = createFragmentIntentClient({ transport: async (request) => {
      calls.push(request);
      return envelope(request.url.includes('?') ? [note] : request.url.endsWith('/knowledge') ? [topic] : note);
    } });
    assert.deepEqual(await client.knowledgeCatalog(), [topic]);
    assert.deepEqual(await client.searchKnowledge('Archify & 可视化'), [note]);
    assert.deepEqual(await client.readKnowledge(id), note);
    assert.equal(calls.length, 3);
    for (const call of calls) {
      assert.equal(call.method, 'GET');
      assert.equal(call.body, undefined);
      assert.equal(call.headers.Origin, 'app://obsidian.md');
      assert.ok(call.url.startsWith('http://127.0.0.1:5684/fragment/v1/knowledge'));
    }
    assert.ok(calls[1].url.endsWith(`?q=${encodeURIComponent('Archify & 可视化')}`));
  });

  it('invalid input produces no request; mismatched identity and unsafe paths fail closed', async () => {
    let calls = 0;
    let value = note;
    const client = createFragmentIntentClient({ transport: async () => { calls += 1; return envelope(value); } });
    await assert.rejects(client.readKnowledge('../private'), /知识标识无效/);
    await assert.rejects(client.searchKnowledge(' '), /搜索内容/);
    assert.equal(calls, 0);
    value = { ...note, knowledge_id: `knowledge-${'c'.repeat(24)}` };
    await assert.rejects(client.readKnowledge(id), /请求不匹配/);
    value = { ...note, path: '../private.md' };
    await assert.rejects(client.readKnowledge(id), /知识记录字段无效/);
    value = { ...note, path: '/etc/hosts' };
    await assert.rejects(client.readKnowledge(id), /知识记录字段无效/);
  });

  it('exact revision GET rejects malformed input before transport and refuses a newer response', async () => {
    const calls = []; let value = note;
    const client = createFragmentIntentClient({ transport: async request => { calls.push(request); return envelope(value); } });
    for (const revision of [null, 0, -1, 1.2, '2', 1000000000]) {
      await assert.rejects(client.readKnowledge(id, revision), /修订号无效/);
    }
    assert.equal(calls.length, 0);
    assert.deepEqual(await client.readKnowledge(id, 2), note);
    assert.equal(calls[0].method, 'GET'); assert.equal(calls[0].body, undefined);
    assert.ok(calls[0].url.endsWith('/' + id + '?revision=2'));
    value = { ...note, revision: 3 };
    await assert.rejects(client.readKnowledge(id, 2), /请求不匹配/);
    assert.equal(calls.length, 2, 'mismatch causes no fallback to current');
  });

  it('malformed catalogs and service failures are errors rather than empty knowledge', async () => {
    let response = envelope({ topics: [] });
    const client = createFragmentIntentClient({ transport: async () => response });
    await assert.rejects(client.knowledgeCatalog(), /知识目录字段无效/);
    response = envelope({ error: 'down' }, 503);
    await assert.rejects(client.knowledgeCatalog(), /HTTP 503/);
  });
});


it('knowledge publication authority requires current digest, system policy and a safe Vault path', () => {
  const receipt = { knowledge_id: `knowledge-${'a'.repeat(24)}`, revision: 1,
    path: 'Loop知识库/结果.md', result_digest: 'b'.repeat(64), publication_source: 'system_policy' };
  const execution = { status: 'passed', result_digest: receipt.result_digest, knowledge_publication: receipt };
  assert.equal(knowledgePublicationOf(execution), receipt);
  for (const patch of [{ status: 'unavailable' }, { result_digest: 'c'.repeat(64) }, { publication_source: 'model_claim' },
    { revision: 0 }, { path: '../private.md' }, { path: '/tmp/record.md' }, { path: 'obsidian://command.md' }]) {
    assert.equal(knowledgePublicationOf({ ...execution, knowledge_publication: { ...receipt, ...patch } }), null);
  }
  assert.equal(knowledgePublicationOf({ ...execution, status: 'running' }), null);
});

it('knowledge revision notifications use a read-only endpoint and reject unbound identities', async () => {
  let notices = [{ knowledge_id: `knowledge-${'a'.repeat(24)}`, notification_id: `knowledge-${'a'.repeat(24)}:2`,
    revision: 2, title: '主题更新', path: 'Loop知识库/00000002/note.md', message: '新证据修订原结论', updated_at: '2026-09-08T12:00:00Z' }];
  const calls = [];
  const client = createFragmentIntentClient({ transport: async (request) => { calls.push(request); return envelope(notices); } });
  assert.deepEqual(await client.knowledgeNotifications(), notices);
  assert.equal(calls[0].method, 'GET');
  assert.equal(calls[0].url, 'http://127.0.0.1:5684/fragment/v1/knowledge/notifications');
  notices = [{ ...notices[0], notification_id: 'wrong:2' }];
  await assert.rejects(client.knowledgeNotifications(), /知识更新通知字段无效/);
});


it('research publication requires its separate digest and explicit null cannot claim a legacy receipt', () => {
  const receipt = { knowledge_id: `knowledge-${'a'.repeat(24)}`, revision: 2, path: 'Loop知识库/v2.md',
    result_digest: 'c'.repeat(64), publication_source: 'system_policy' };
  const execution = { status: 'passed', result_digest: 'b'.repeat(64), research_result_digest: 'c'.repeat(64), knowledge_publication: receipt };
  assert.equal(knowledgePublicationOf(execution), receipt);
  assert.equal(knowledgePublicationOf({ ...execution, research_result_digest: null }), null);
  assert.equal(knowledgePublicationOf({ ...execution, research_result_digest: 'd'.repeat(64) }), null);
  assert.equal(knowledgePublicationOf({ ...execution, knowledge_publication: { ...receipt, revision: 1, result_digest: 'b'.repeat(64) } }), null);
  const legacy = { ...execution, result_digest: receipt.result_digest }; delete legacy.research_result_digest;
  assert.equal(knowledgePublicationOf(legacy), receipt);
  assert.equal(knowledgePublicationOf({ ...legacy, research_result_digest: null }), null);
});

it('knowledge freshness metadata and canonical research obey nested identity and path validation', async () => {
  const source = { knowledge_id: `knowledge-${'c'.repeat(24)}`, revision: 2, title: '当前源研究', path: '知识/v2.md',
    result: { summary: '原研究结论' }, freshness: { status: 'current', reasons: [], latest_revision: 2 },
    usable_as_current: true, conclusion_authority: 'research_result' };
  const record = { knowledge_id: `knowledge-${'a'.repeat(24)}`, revision: 1, title: '主题整理', path: '知识/主题.md',
    result: { summary: '旧结构摘要' }, evidence: [], canonical_research: [source],
    freshness: { status: 'stale', reasons: ['源研究已更新'], latest_revision: 1 }, usable_as_current: false, conclusion_authority: 'canonical_research' };
  let value = record;
  const client = createFragmentIntentClient({ transport: async () => envelope(value) });
  assert.deepEqual(await client.readKnowledge(record.knowledge_id), record);
  for (const mutate of [
    r => r.freshness.status = 'verified', r => r.freshness.reasons = [4], r => r.freshness.latest_revision = 0,
    r => r.usable_as_current = 'true', r => r.conclusion_authority = 'model_says_so',
    r => r.canonical_research[0].path = '../private.md', r => r.canonical_research[0].result = [],
  ]) {
    value = structuredClone(record); mutate(value);
    await assert.rejects(client.readKnowledge(record.knowledge_id), error => error.kind === 'invalid_response');
  }
});
