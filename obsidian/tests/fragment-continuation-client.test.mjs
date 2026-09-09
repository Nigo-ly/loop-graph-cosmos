import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const {
  CONTINUATION_API_URL,
  createFragmentContinuationClient,
  FragmentContinuationApiError,
} = require(path.join(ROOT, 'src', 'console', 'fragment-continuation-client.js'));

const status = {
  fragment_id: '2026-08-08-19-41-45-1c8e88-desktop',
  status: 'approved',
  current_node: 'cognitive_contract',
  sequence: 376,
  stage: 'loop_registered',
  updated_at: '2026-08-08T19:45:00+08:00',
};

const outcome = {
  candidate_id: 'fragment-candidate-1',
  fragment_ref: 'fragments/2026-08-08-19-41-45-1c8e88-desktop.md',
  title: 'Minimax H3权重本地部署可行性调研',
  content_status: 'pending_confirmation',
  evidence_level: 'unverified',
  has_conflict: false,
  loop_sequence: 380,
  continuation_status: 'published',
};

function envelope(data, error = null) {
  return { contract_version: '2', data, error };
}

describe('fragment continuation client', () => {
  it('lists only validated safe status projections', async () => {
    const seen = [];
    const client = createFragmentContinuationClient({
      transport: async (request) => {
        seen.push(request);
        return { status: 200, json: envelope([status]) };
      },
    });
    assert.deepEqual(await client.list(), [status]);
    assert.equal(seen[0].url, CONTINUATION_API_URL);
    assert.equal(seen[0].method, 'GET');
    assert.equal(seen[0].headers.Origin, 'app://obsidian.md');
  });

  it('posts the exact seven-field binding with local digests and frozen header', async () => {
    const seen = [];
    const rawText = '---\nnigo-loop: true\n---\n原始碎片';
    const organizedText = '---\ntype: 已整理碎片\n---\n整理结果';
    const client = createFragmentContinuationClient({
      transport: async (request) => {
        seen.push(request);
        return { status: 201, json: envelope(outcome) };
      },
    });
    assert.deepEqual(await client.continueResearch({
      fragmentId: status.fragment_id,
      rawRef: `Notes/散记/碎片想法/${status.fragment_id}.md`,
      organizedRef: `Notes/AI创业/碎片整理/${status.fragment_id}.md`,
      rawText,
      organizedText,
    }), outcome);
    const request = seen[0];
    assert.equal(request.method, 'POST');
    assert.equal(request.headers['X-Fragment-Continuation'], '1');
    const body = JSON.parse(request.body);
    assert.deepEqual(Object.keys(body).sort(), [
      'fragment_id', 'organized_ref', 'organized_sha256', 'raw_ref',
      'raw_sha256', 'requester', 'route',
    ]);
    assert.equal(body.raw_sha256, createHash('sha256').update(rawText).digest('hex'));
    assert.equal(body.organized_sha256, createHash('sha256').update(organizedText).digest('hex'));
    assert.equal(body.route, 'research');
    assert.equal(body.requester, 'nigo');
    assert.ok(!request.body.includes('原始碎片'));
    assert.ok(!request.body.includes('整理结果'));
  });

  it('rejects malformed status and candidate responses', async () => {
    const badList = createFragmentContinuationClient({
      transport: async () => ({ status: 200, json: envelope([{ ...status, sequence: 0 }]) }),
    });
    await assert.rejects(() => badList.list(), /状态无效/);
    const badOutcome = createFragmentContinuationClient({
      transport: async () => ({ status: 201, json: envelope({ ...outcome, evidence_level: 'verified' }) }),
    });
    await assert.rejects(() => badOutcome.continueResearch({
      fragmentId: status.fragment_id,
      rawRef: 'raw.md', organizedRef: 'organized.md', rawText: 'raw', organizedText: 'organized',
    }), /结果无效/);
  });

  it('surfaces stable server error codes without retrying or leaking response text', async () => {
    let calls = 0;
    const client = createFragmentContinuationClient({
      transport: async () => {
        calls += 1;
        return {
          status: 409,
          json: envelope(null, { code: 'source_changed', message: 'private body must not surface' }),
        };
      },
    });
    try {
      await client.continueResearch({
        fragmentId: status.fragment_id,
        rawRef: 'raw.md', organizedRef: 'organized.md', rawText: 'raw', organizedText: 'organized',
      });
      assert.fail('must reject');
    } catch (error) {
      assert.ok(error instanceof FragmentContinuationApiError);
      assert.equal(error.details.code, 'source_changed');
      assert.ok(!error.message.includes('private body'));
    }
    assert.equal(calls, 1);
  });
});
