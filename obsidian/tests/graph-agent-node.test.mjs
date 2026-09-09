import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { FakeEl } from './helpers/fake-dom.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const viewModel = require(path.join(ROOT, 'src', 'console', 'graph-view-model.js'));
const { GraphApiError, createGraphClient } = require(
  path.join(ROOT, 'src', 'console', 'graph-client.js'),
);

// Synthetic node data only — Phase 2A forbids any real backend run. The
// agent observation shape follows GRAPH-PHASE2-DESIGN.md §5/§7: status,
// provider/model, max calls, reserved/actual tokens, error category.

function nodeEntry(id, overrides = {}) {
  return {
    node_id: id,
    status: 'succeeded',
    attempts: 1,
    terminal: true,
    input_digest: 'd'.repeat(64),
    output_digest: 'e'.repeat(64),
    input_refs: ['fixture:fragment:abc'],
    output_refs: [],
    output: { note: 'ok' },
    error: null,
    completed_at: '2026-08-04T00:00:30+00:00',
    child_run_id: null,
    absorbed_failures: [],
    memory_refs: [],
    ...overrides,
  };
}

function agentMeta(overrides = {}) {
  return {
    status: 'completed',
    provider: 'synthetic-provider',
    model: 'synthetic-model-v1',
    max_calls: 1,
    reserved_tokens: 800,
    actual_tokens: 512,
    error_category: null,
    ...overrides,
  };
}

function detailWithAgentNode(agent) {
  return {
    nodes: [nodeEntry('agent_step', agent === undefined ? {} : { agent })],
    edges_taken: [],
    human_gates: {},
    ready: [],
  };
}

function renderAgentNode(agent) {
  const container = new FakeEl('div');
  const detail = detailWithAgentNode(agent);
  const model = viewModel.buildNodeDetailModel(detail, 'agent_step');
  viewModel.renderNodeDetail(container, model, { onBack: () => {} });
  return container;
}

function serialize(el) {
  const head = [
    el.tagName,
    [...el.classes].sort().join(' '),
    JSON.stringify(el.attributes),
    el.textContent,
  ].join('|');
  return `${head}{${el.children.map(serialize).join(',')}}`;
}

describe('Graph Phase 2A agent node detail (view-model)', () => {
  it('maps every frozen observation status to the six frozen labels', () => {
    const cases = [
      ['not_called', '未调用'],
      ['pre_call_pending', '等待调用前确认'],
      ['reserved', '已预留'],
      ['completed_pending_confirmation', '结果待确认'],
      ['completed', '已完成'],
      ['blocked', '阻断'],
      ['failed', '阻断'],
      ['unknown_send', '阻断'],
    ];
    for (const [status, label] of cases) {
      const container = renderAgentNode(agentMeta({ status }));
      assert.ok(
        container.text.includes(`状态：${label}`),
        `${status} must render as ${label}`,
      );
      assert.ok(
        container.text.includes('草稿 · 未验证 · 无外部写入'),
        `${status} must always carry the draft/unverified/no-external-write note`,
      );
    }
  });

  it('shows provider/model, call cap and reserved vs actual tokens', () => {
    const container = renderAgentNode(agentMeta());
    assert.ok(
      container.text.includes(
        'Provider／模型：synthetic-provider / synthetic-model-v1 · 调用次数上限 1',
      ),
    );
    assert.ok(container.text.includes('预留 token：800 · 实际 token：512'));
  });

  it('renders missing token figures as dashes', () => {
    const container = renderAgentNode(
      agentMeta({ status: 'reserved', reserved_tokens: 300, actual_tokens: null }),
    );
    assert.ok(container.text.includes('预留 token：300 · 实际 token：—'));
    assert.ok(container.text.includes('已预留'));
  });

  it('shows the error category only when present', () => {
    const withError = renderAgentNode(
      agentMeta({ status: 'failed', error_category: 'response_schema_invalid' }),
    );
    assert.ok(withError.text.includes('错误类别：response_schema_invalid'));
    const withoutError = renderAgentNode(agentMeta({ error_category: null }));
    assert.ok(!withoutError.text.includes('错误类别'));
  });

  it('unknown_send shows only the frozen human-wait copy, with no retry entry', () => {
    const container = renderAgentNode(
      agentMeta({ status: 'unknown_send', error_category: 'unknown_send' }),
    );
    // G1-16 冻结文案（与后端 projection UNKNOWN_SEND_LABEL 一致）
    assert.ok(container.text.includes('发送状态未知，等待人工'));
    assert.ok(!container.text.includes('重试'));
    assert.ok(!container.text.includes('retry'));
    assert.ok(!container.text.toLowerCase().includes('exactly-once'));
    const agentSection = container.querySelector('.graph-node-agent');
    assert.ok(agentSection, 'agent block rendered');
    assert.equal(
      agentSection.querySelectorAll('button').length,
      0,
      'the agent block must never contain any actionable (retry) button',
    );
    // The only button in the whole detail remains the back navigation.
    for (const button of container.querySelectorAll('button')) {
      assert.ok(button.className.includes('graph-view-back'));
    }
  });

  it('never renders authorization phrase, prompt or response bodies', () => {
    const secrets = {
      authorization_phrase: 'AUTH_PHRASE_SECRET_XYZ',
      prompt_text: 'PROMPT_BODY_SECRET_XYZ',
      response_body: 'RESPONSE_BODY_SECRET_XYZ',
      credentials: 'sk-SECRET-XYZ',
    };
    const container = renderAgentNode(agentMeta(secrets));
    for (const value of Object.values(secrets)) {
      assert.ok(
        !container.text.includes(value),
        `sensitive material must never enter the DOM: ${value}`,
      );
    }
  });

  it('escapes injected markup as plain text', () => {
    const payload = '<img src=x onerror=alert(1)>';
    const container = renderAgentNode(agentMeta({ provider: payload }));
    assert.ok(container.text.includes(payload), 'payload stays literal text');
    assert.equal(container.querySelectorAll('img').length, 0, 'no element is created from data');
  });

  it('treats a missing status as the fail-safe human-check state', () => {
    const meta = agentMeta();
    delete meta.status;
    const container = renderAgentNode(meta);
    assert.ok(container.text.includes('阻断'));
    assert.ok(container.text.includes('发送状态未知，等待人工'));
  });

  it('Phase 1 nodes without agent metadata keep the exact committed rendering', () => {
    // Regression against the committed baseline module: identical input must
    // produce a byte-identical serialized DOM tree when no agent metadata
    // exists, and no agent block may appear.
    const committedCode = readFileSync(path.join(ROOT, 'tests/fixtures/graph-view-model-baseline.cjs'), 'utf8');
    const tempDir = mkdtempSync(path.join(os.tmpdir(), 'graph-view-model-baseline-'));
    const baselinePath = path.join(tempDir, 'graph-view-model-baseline.cjs');
    writeFileSync(baselinePath, committedCode);
    const baseline = require(baselinePath);

    const detail = detailWithAgentNode(undefined);
    const currentContainer = new FakeEl('div');
    viewModel.renderNodeDetail(
      currentContainer,
      viewModel.buildNodeDetailModel(detail, 'agent_step'),
      { onBack: () => {} },
    );
    const baselineContainer = new FakeEl('div');
    baseline.renderNodeDetail(
      baselineContainer,
      baseline.buildNodeDetailModel(detail, 'agent_step'),
      { onBack: () => {} },
    );

    assert.equal(viewModel.buildNodeDetailModel(detail, 'agent_step').agent, null);
    assert.equal(currentContainer.querySelector('.graph-node-agent'), null);
    assert.equal(
      serialize(currentContainer),
      serialize(baselineContainer),
      'node detail without agent metadata must render byte-identical to the committed baseline',
    );
  });
});

describe('Graph Phase 2A agent observation (client validation)', () => {
  function runSummary() {
    return {
      run_id: 'exec:test:agent',
      graph_id: 'fragment-cognitive-agent-pilot-v1',
      spec_version: '1.0.0',
      spec_digest: 'c'.repeat(64),
      fragment_ref: 'fixture:fragment:test',
      status: 'completed',
      current_node: 'output_draft',
      step_count: 5,
      sequence: 5,
      pending_human: [],
      blocked_reason: null,
      started_at: '2026-08-05T00:00:00+00:00',
      updated_at: '2026-08-05T00:01:00+00:00',
    };
  }

  function runDetail(agent) {
    return {
      run: runSummary(),
      nodes: [nodeEntry('agent_step', agent === undefined ? {} : { agent })],
      edges_taken: [],
      feedback_counts: {},
      human_gates: {},
      ready: [],
    };
  }

  function clientWith(payload) {
    return createGraphClient({
      transport: async () => ({
        status: 200,
        json: { contract_version: '2', data: payload, error: null },
      }),
    });
  }

  it('passes a well-formed agent observation through untouched', async () => {
    const client = clientWith(runDetail(agentMeta()));
    const detail = await client.getRun('exec:test:agent');
    assert.equal(detail.nodes[0].agent.status, 'completed');
    assert.equal(detail.nodes[0].agent.reserved_tokens, 800);
  });

  it('accepts Phase 1 detail without agent metadata', async () => {
    const client = clientWith(runDetail(undefined));
    const detail = await client.getRun('exec:test:agent');
    assert.equal(detail.nodes[0].agent, undefined);
  });

  it('rejects malformed agent observations before they reach the view', async () => {
    for (const bad of [
      agentMeta({ max_calls: 'one' }),
      agentMeta({ reserved_tokens: 'many' }),
      agentMeta({ provider: 42 }),
      agentMeta({ error_category: 7 }),
      ['not-an-object'],
    ]) {
      const client = clientWith(runDetail(bad));
      await assert.rejects(() => client.getRun('exec:test:agent'), (error) => {
        assert.ok(error instanceof GraphApiError);
        assert.equal(error.kind, 'invalid_response');
        return true;
      });
    }
  });
});
