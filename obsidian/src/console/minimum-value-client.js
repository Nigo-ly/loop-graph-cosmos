"use strict";

const MINIMUM_VALUE_API_BASE_URL = "http://127.0.0.1:5683/fragment/v1";
const MINIMUM_VALUE_CONTRACT_VERSION = "1";
const TRUSTED_ORIGIN = "app://obsidian.md";
const DECISION_HEADER = "X-Fragment-Send-Decision";
const REQUEST_TIMEOUT_MS = 5000;
const DECISIONS = new Set(["consent", "decline"]);

class MinimumValueApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "MinimumValueApiError";
    this.kind = kind;
    this.details = details || {};
  }
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function decisionIdempotencyKey(input, requester) {
  const canonical = [
    requester,
    input.decision,
    input.runId,
    input.fragmentId,
    input.outboundPayloadSha256,
    input.targetProvider,
    input.targetModel,
    input.targetProfile,
    String(input.expectedSequence),
  ].join("\x1f");
  return toHex(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical))
  );
}

function validateInput(input) {
  if (!input || !DECISIONS.has(input.decision)) {
    throw new MinimumValueApiError("invalid_arguments", "发送决定必须是 consent 或 decline");
  }
  for (const field of [
    "runId",
    "fragmentId",
    "targetProvider",
    "targetModel",
    "targetProfile",
  ]) {
    if (
      typeof input[field] !== "string" ||
      !input[field].trim() ||
      input[field].includes("\x1f")
    ) {
      throw new MinimumValueApiError("invalid_arguments", `${field} 不可为空`);
    }
  }
  if (
    typeof input.outboundPayloadSha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(input.outboundPayloadSha256)
  ) {
    throw new MinimumValueApiError("invalid_arguments", "正文 SHA-256 无效");
  }
  if (!Number.isInteger(input.expectedSequence) || input.expectedSequence < 1) {
    throw new MinimumValueApiError("invalid_arguments", "Checkpoint 序号无效");
  }
  if (
    input.decision === "consent" &&
    (typeof input.outboundPayload !== "string" || !input.outboundPayload)
  ) {
    throw new MinimumValueApiError("invalid_arguments", "实际发送正文不可用");
  }
}

function validateEnvelope(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new MinimumValueApiError("invalid_response", "发送决定服务返回了无效响应");
  }
  if (body.contract_version !== MINIMUM_VALUE_CONTRACT_VERSION) {
    throw new MinimumValueApiError(
      "contract_mismatch",
      "发送决定服务契约版本不匹配，已停止操作"
    );
  }
  return body;
}

function createMinimumValueClient(options) {
  const transport = options.transport;
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  const requester = options.requester || "nigo";
  if (typeof transport !== "function") {
    throw new MinimumValueApiError(
      "invalid_response",
      "Minimum value client requires a transport function"
    );
  }

  async function submitDecision(input) {
    validateInput(input);
    if (input.decision === "consent") {
      const displayedPayloadSha256 = toHex(
        await crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(input.outboundPayload)
        )
      );
      if (displayedPayloadSha256 !== input.outboundPayloadSha256) {
        throw new MinimumValueApiError(
          "binding_mismatch",
          "展示正文与固定摘要不一致，已停止提交"
        );
      }
    }
    const idempotencyKey =
      input.idempotencyKey || (await decisionIdempotencyKey(input, requester));
    const body = {
      decision: input.decision,
      run_id: input.runId,
      fragment_id: input.fragmentId,
      outbound_payload_sha256: input.outboundPayloadSha256,
      target_provider: input.targetProvider,
      target_model: input.targetModel,
      target_profile: input.targetProfile,
      expected_sequence: input.expectedSequence,
      idempotency_key: idempotencyKey,
      requester,
    };
    let timer = null;
    try {
      const response = await Promise.race([
        transport({
          url: `${MINIMUM_VALUE_API_BASE_URL}/decisions`,
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            Origin: TRUSTED_ORIGIN,
            [DECISION_HEADER]: "1",
          },
          body: JSON.stringify(body),
        }),
        new Promise((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new MinimumValueApiError(
                  "unreachable",
                  "发送决定服务请求超时"
                )
              ),
            timeoutMs
          );
        }),
      ]);
      const status =
        response && typeof response.status === "number" ? response.status : 0;
      const json = response ? response.json : null;
      if (status < 200 || status >= 300) {
        const error = json && json.error ? json.error : {};
        throw new MinimumValueApiError(
          "http_error",
          `发送决定服务返回 HTTP ${status}`,
          {
            status,
            code: typeof error.code === "string" ? error.code : null,
            message: typeof error.message === "string" ? error.message : null,
          }
        );
      }
      const envelope = validateEnvelope(json);
      return { httpStatus: status, decision: envelope.data };
    } catch (error) {
      if (error instanceof MinimumValueApiError) throw error;
      throw new MinimumValueApiError("unreachable", "无法连接发送决定服务");
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  return { requester, submitDecision };
}

module.exports = {
  MINIMUM_VALUE_API_BASE_URL,
  MINIMUM_VALUE_CONTRACT_VERSION,
  TRUSTED_ORIGIN,
  DECISION_HEADER,
  REQUEST_TIMEOUT_MS,
  MinimumValueApiError,
  decisionIdempotencyKey,
  createMinimumValueClient,
};
