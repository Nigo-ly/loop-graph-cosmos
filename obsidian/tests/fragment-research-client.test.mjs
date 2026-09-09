import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

// Fragment Governed Research v1 包 D：research-runs 客户端与投影校验专项。
// 标注对应包 F 产品验收项编号（#9 手机端同一权威状态字段契约、#10 合法升级条件）。

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const {
  RESEARCH_RUNS_URL,
  RESEARCH_RUN_CREATE_HEADER,
  RESEARCH_SPEC_ID,
  RESEARCH_MACRO_V3_SPEC_ID,
  FragmentIntentApiError,
  createFragmentIntentClient,
  researchProposalReady,
  researchCreateMaterial,
} = require(path.join(ROOT, 'src', 'console', 'fragment-intent-client.js'));

const RESULT_DIGEST = 'b'.repeat(64);

function expectedEscalationId(alignmentId) {
  const canonical = JSON.stringify(['fragment-escalation-v1', alignmentId, RESULT_DIGEST]);
  return `escalation:${createHash('sha256').update(canonical).digest('hex').slice(0, 24)}`;
}

function proposalMaterial(alignmentId, overrides = {}) {
  return {
    status: 'proposed',
    reason: '证据缺口需要受治理研究升级',
    source_result_digest: RESULT_DIGEST,
    spec_id: RESEARCH_SPEC_ID,
    spec_digest: 'd'.repeat(64),
    evidence_bundle_digest: 'e'.repeat(64),
    escalation_id: expectedEscalationId(alignmentId),
    ...overrides,
  };
}

function researchExecution(overrides = {}) {
  return {
    run_id: 'exec:fragment-intent:0123456789abcdef01234567',
    fragment_id: 'fragment-1',
    status: 'passed',
    current_node: 'harvest',
    route: 'verify',
    stop_reason: 'completed',
    updated_at: '2026-08-09T00:00:00+00:00',
    result_digest: RESULT_DIGEST,
    harvest: [],
    research_progress: {
      stage: 'synthesized',
      collected_sources: 2,
      stop_reason: 'evidence_sufficient',
      model_calls: 1,
      network_requests: 3,
    },
    research_evidence: [{
      title: '官方发布页', url: 'https://example.com/release',
      marker: 'newly_collected', source_target: 'official_docs',
      evidence_id: 'ev-official', evidence_digest: 'c'.repeat(64),
    }],
    result: {
      summary: '示例产品已发布。',
      unknowns: ['性能未知'],
      next_checks: ['可查阅发布页。'],
      needs_escalation: false,
      escalation_reason: '',
      model_calls: 1,
      tool_calls: 3,
      confirmed: [{ claim: '已发布', evidence_ids: ['ev-official'] }],
      conflicts: [{
        topic: '发布日期',
        dimensions: ['官方写 8 月 1 日'],
        evidence_ids: ['ev-official'],
      }],
      recommendation: '可查阅发布页。',
      claims: [{ claim: '已发布', evidence_id: 'ev-official', relation: 'supports' }],
    },
    ...overrides,
  };
}

function alignment(overrides = {}) {
  const base = {
    alignment_id: 'align:0123456789abcdef01234567',
    fragment_id: 'fragment-1',
    case_id: 'case:0123456789abcdef01234567',
    episode_id: 'episode:0123456789abcdef01234567',
    title: '核验示例产品是否已公开发布',
    status: 'passed',
    sequence: 1,
    revision: 1,
    input_digest: 'a'.repeat(64),
    reasoning: '先核实真实性。',
    plan: '先核实再升级。',
    expected_result: '可信结论。',
    exclusions: [],
    suggested_intents: ['verify'],
    dynamic_intents: [],
    recommended_route: 'verify',
    memory_basis: [],
    route: 'verify',
    decision: { action: 'confirm', intents: ['verify'], supplement: '' },
    execution_scope: {
      capabilities: ['核验'], external_scope: ['公开来源'],
      model_call_cap: 1, cost_cap_cny: 1, side_effect: 'none',
    },
    ...overrides,
  };
  return base;
}

function envelope(data, status = 200, error = null) {
  return { status, json: { contract_version: '2', data, error } };
}

describe('fragment research client (包 F 产品验收 #9/#10)', () => {
  it('[F#9] accepts the exact authoritative projection field names shared with the phone surface', async () => {
    const item = alignment({
      execution: researchExecution({ graph_escalation: proposalMaterial('align:0123456789abcdef01234567') }),
    });
    const client = createFragmentIntentClient({ transport: async () => envelope([item]) });
    const [projected] = await client.list();
    // 同一权威状态字段名：投影字段逐名存活，不改名、不裁剪。
    assert.equal(projected.execution.research_progress.stage, 'synthesized');
    assert.equal(projected.execution.research_progress.collected_sources, 2);
    assert.equal(projected.execution.research_progress.network_requests, 3);
    assert.equal(projected.execution.research_progress.stop_reason, 'evidence_sufficient');
    assert.equal(projected.execution.result.confirmed[0].claim, '已发布');
    assert.equal(projected.execution.result.conflicts[0].topic, '发布日期');
    assert.equal(projected.execution.result.recommendation, '可查阅发布页。');
    assert.equal(projected.execution.result.claims[0].relation, 'supports');
    assert.equal(projected.execution.research_evidence[0].marker, 'newly_collected');
    assert.equal(projected.execution.graph_escalation.status, 'proposed');
    assert.ok(researchProposalReady(projected));
  });

  it('[F#10] posts exactly the seven-field contract with the frozen header to loopback only', async () => {
    const item = alignment({
      execution: researchExecution({ graph_escalation: proposalMaterial('align:0123456789abcdef01234567') }),
    });
    const calls = [];
    const client = createFragmentIntentClient({
      transport: async (request) => {
        calls.push(request);
        return envelope({
          run_id: 'exec:graph-research:0123456789abcdef01234567',
          status: 'human_wait',
          sequence: 7,
          model_calls: 0,
          receipt_digest: 'f'.repeat(64),
        }, 201);
      },
    });
    const outcome = await client.createResearchRun(item);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, RESEARCH_RUNS_URL);
    assert.equal(calls[0].url, 'http://127.0.0.1:5684/graph/v1/research-runs');
    assert.equal(calls[0].headers.Origin, 'app://obsidian.md');
    assert.equal(calls[0].headers[RESEARCH_RUN_CREATE_HEADER], '1');
    const body = JSON.parse(calls[0].body);
    assert.deepEqual(Object.keys(body).sort(), [
      'alignment_id', 'episode_id', 'escalation_id',
      'evidence_bundle_digest', 'requester', 'spec_digest', 'spec_id',
    ]);
    assert.equal(body.spec_id, RESEARCH_SPEC_ID);
    assert.equal(body.spec_digest, 'd'.repeat(64));
    assert.equal(body.alignment_id, item.alignment_id);
    assert.equal(body.episode_id, item.episode_id);
    assert.equal(body.evidence_bundle_digest, 'e'.repeat(64));
    assert.equal(body.escalation_id, expectedEscalationId(item.alignment_id));
    assert.equal(body.requester, 'nigo');
    assert.equal(outcome.status, 'human_wait');
    assert.equal(outcome.model_calls, 0);
  });

  it('[rev6] accepts macro v3 proposal and posts v3 spec identity end to end', async () => {
    // rev6 P0-1 前端段：新提案（宏 v3）→ 创建材料逐字段携带 v3 identity；
    // v1 提案仍被接受用于旧路径重放。
    const v3Item = alignment({
      execution: researchExecution({
        graph_escalation: proposalMaterial('align:0123456789abcdef01234567', {
          spec_id: RESEARCH_MACRO_V3_SPEC_ID,
        }),
      }),
    });
    assert.ok(researchProposalReady(v3Item), 'v3 提案必须可创建');
    const material = await researchCreateMaterial(v3Item);
    assert.equal(material.spec_id, RESEARCH_MACRO_V3_SPEC_ID);
    assert.equal(material.spec_digest, 'd'.repeat(64));
    const calls = [];
    const client = createFragmentIntentClient({
      transport: async (request) => {
        calls.push(request);
        return envelope({
          run_id: 'exec:graph-research:0123456789abcdef01234567',
          status: 'human_wait',
          sequence: 3,
          model_calls: 0,
          receipt_digest: 'f'.repeat(64),
        }, 201);
      },
    });
    await client.createResearchRun(v3Item);
    const body = JSON.parse(calls[0].body);
    assert.equal(body.spec_id, RESEARCH_MACRO_V3_SPEC_ID, '创建 body 端到端携带 v3 identity');
    // v1 旧提案仍接受（只读重放兼容）。
    const v1Item = alignment({
      execution: researchExecution({
        graph_escalation: proposalMaterial('align:0123456789abcdef01234567'),
      }),
    });
    assert.ok(researchProposalReady(v1Item), 'v1 旧提案必须仍可重放');
  });

  it('[F#10] fails closed before sending when the proposal binding drifts', async () => {
    const calls = [];
    const client = createFragmentIntentClient({
      transport: async (request) => {
        calls.push(request);
        return envelope({});
      },
    });
    const drifts = [
      // escalation_id 与 alignment/result 的确定性绑定不匹配。
      proposalMaterial('align:0123456789abcdef01234567', { escalation_id: `escalation:${'9'.repeat(24)}` }),
      // spec digest 漂移。
      proposalMaterial('align:0123456789abcdef01234567', { spec_digest: '0'.repeat(64) }),
    ];
    // 第一例：绑定不匹配 → 拒绝发送；第二例仅 digest 值不同（格式合法），
    // 客户端无法本地判定真伪，由服务端 plan_expired 兜底——只允许前者零请求。
    const mismatched = alignment({
      execution: researchExecution({ graph_escalation: drifts[0] }),
    });
    assert.equal(researchProposalReady(mismatched), true); // 结构合法，入口可见
    await assert.rejects(
      () => client.createResearchRun(mismatched),
      (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_arguments',
    );
    assert.equal(calls.length, 0);
    const material = await researchCreateMaterial(mismatched);
    assert.equal(material, null);
    // 材料缺失（无 spec_digest）：入口与发送双重关闭。
    const incomplete = alignment({
      execution: researchExecution({
        graph_escalation: proposalMaterial('align:0123456789abcdef01234567', { spec_digest: undefined }),
      }),
    });
    assert.equal(researchProposalReady(incomplete), false);
    await assert.rejects(
      () => client.createResearchRun(incomplete),
      (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_arguments',
    );
    assert.equal(calls.length, 0);
  });

  it('[F#10] surfaces server rejection codes for the register error mapping', async () => {
    const item = alignment({
      execution: researchExecution({ graph_escalation: proposalMaterial('align:0123456789abcdef01234567') }),
    });
    const client = createFragmentIntentClient({
      transport: async () => envelope(null, 409, { code: 'already_bridged', message: 'conflict' }),
    });
    await assert.rejects(
      () => client.createResearchRun(item),
      (error) => error instanceof FragmentIntentApiError &&
        error.kind === 'http_error' && error.details.code === 'already_bridged',
    );
  });

  it('rejects malformed research progress, evidence and result extras before rendering', async () => {
    const malformed = [
      researchExecution({ research_progress: { stage: 'synthesized', collected_sources: -1, model_calls: 0 } }),
      researchExecution({ research_progress: { stage: 'collected', collected_sources: 0, model_calls: 0, note: 'x\n' } }),
      researchExecution({ research_evidence: [{ title: 't', url: 'https://a', marker: 'invented' }] }),
      researchExecution({ result: { ...researchExecution().result, claims: [{ claim: 'c', evidence_id: 'e', relation: 'invented' }] } }),
      researchExecution({ result: { ...researchExecution().result, conflicts: [{ topic: 't', dimensions: [], evidence_ids: [] }] } }),
      researchExecution({ graph_escalation: { status: 'proposed', spec_digest: 'not-hex' } }),
    ];
    for (const execution of malformed) {
      const client = createFragmentIntentClient({
        transport: async () => envelope([alignment({ execution })]),
      });
      await assert.rejects(
        () => client.list(),
        (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_response',
      );
    }
  });

  it('rejects malformed research-run create outcomes', async () => {
    const item = alignment({
      execution: researchExecution({ graph_escalation: proposalMaterial('align:0123456789abcdef01234567') }),
    });
    for (const data of [
      { run_id: 'not-exec', status: 'human_wait', model_calls: 0 },
      { run_id: 'exec:graph-research:x', status: 'human_wait', model_calls: -1 },
      { run_id: 'exec:graph-research:x', status: 'human_wait', model_calls: 0, receipt_digest: 'bad' },
    ]) {
      const client = createFragmentIntentClient({ transport: async () => envelope(data, 201) });
      await assert.rejects(
        () => client.createResearchRun(item),
        (error) => error instanceof FragmentIntentApiError && error.kind === 'invalid_response',
      );
    }
  });
});
