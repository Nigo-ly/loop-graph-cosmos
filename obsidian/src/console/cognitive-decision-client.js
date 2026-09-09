"use strict";

const { createHttpClient } = require("./http-client.js");

const COGNITIVE_DECISION_API_BASE_URL =
  "http://127.0.0.1:5684/fragment-cognitive/v1";
const COGNITIVE_DECISION_CONTRACT_VERSION = "2";
const TRUSTED_ORIGIN = "app://obsidian.md";
const DECISION_HEADER = "X-Fragment-Cognitive-Decision";
// R1-OC: the withdrawal header is frozen separately by the backend; the
// decision header must never be reused to masquerade a withdrawal.
const WITHDRAWAL_HEADER = "X-Fragment-Cognitive-Withdrawal";
const WITHDRAWAL_ACTION = "withdraw";
const REQUEST_TIMEOUT_MS = 5000;
const DECISIONS = new Set(["keep_draft", "reject"]);
const FORBIDDEN_CATEGORY_CHARACTERS = ["\n", "\r", "\x1f"];

class CognitiveDecisionApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "CognitiveDecisionApiError";
    this.kind = kind;
    this.details = details || {};
  }
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function sha256(value) {
  return toHex(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))
  );
}

async function decisionIdempotencyKey(input, requester, markdownSha256, thoughtCategory) {
  return sha256(
    [
      requester,
      input.decision,
      input.runId,
      input.fragmentId,
      markdownSha256,
      String(input.expectedSequence),
      thoughtCategory,
    ].join("\x1f")
  );
}

// R1-OC: frozen backend order requester/action/run/fragment/expected_sequence.
// The digest is submitted as the withdrawal_id body field; it never contains
// note paths, bodies, categories, or private content.
async function withdrawalIdempotencyKey(input, requester) {
  return sha256(
    [
      requester,
      WITHDRAWAL_ACTION,
      input.runId,
      input.fragmentId,
      String(input.expectedSequence),
    ].join("\x1f")
  );
}

function validateWithdrawalInput(input) {
  for (const field of ["runId", "fragmentId"]) {
    if (
      !input ||
      typeof input[field] !== "string" ||
      !input[field].trim() ||
      input[field].includes("\x1f")
    ) {
      throw new CognitiveDecisionApiError("invalid_arguments", `${field} 不可为空`);
    }
  }
  if (!Number.isInteger(input.expectedSequence) || input.expectedSequence < 1) {
    throw new CognitiveDecisionApiError("invalid_arguments", "Checkpoint 序号无效");
  }
}

function isValidKeepThoughtCategory(value) {
  if (typeof value !== "string") return false;
  if (
    FORBIDDEN_CATEGORY_CHARACTERS.some((character) => value.includes(character))
  ) {
    return false;
  }
  const category = value.trim();
  return category.length > 0 && Array.from(category).length <= 128;
}

function normalizeThoughtCategory(input) {
  const raw = input.thoughtCategory;
  if (input.decision === "reject") {
    if (raw !== "") {
      throw new CognitiveDecisionApiError(
        "invalid_arguments",
        "拒绝决定只能携带空分类"
      );
    }
    return "";
  }
  if (typeof raw !== "string" || !isValidKeepThoughtCategory(raw)) {
    throw new CognitiveDecisionApiError(
      "invalid_arguments",
      "思考分类必须为 1-128 字符且不含换行、回车或控制分隔符"
    );
  }
  return raw.trim();
}

function validateInput(input) {
  if (!input || !DECISIONS.has(input.decision)) {
    throw new CognitiveDecisionApiError(
      "invalid_arguments",
      "草稿决定必须是 keep_draft 或 reject"
    );
  }
  for (const field of ["runId", "fragmentId", "markdown"]) {
    if (
      typeof input[field] !== "string" ||
      !input[field].trim() ||
      input[field].includes("\x1f")
    ) {
      throw new CognitiveDecisionApiError("invalid_arguments", `${field} 不可为空`);
    }
  }
  if (!Number.isInteger(input.expectedSequence) || input.expectedSequence < 1) {
    throw new CognitiveDecisionApiError("invalid_arguments", "Checkpoint 序号无效");
  }
}

function validateEnvelope(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new CognitiveDecisionApiError("invalid_response", "草稿决定服务返回了无效响应");
  }
  if (body.contract_version !== COGNITIVE_DECISION_CONTRACT_VERSION) {
    throw new CognitiveDecisionApiError(
      "contract_mismatch",
      "草稿决定服务契约版本不匹配，已停止操作"
    );
  }
  return body;
}

function createCognitiveDecisionClient(options) {
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  const requester = options.requester || "nigo";
  if (typeof options.transport !== "function") {
    throw new CognitiveDecisionApiError(
      "invalid_response",
      "Cognitive decision client requires a transport function"
    );
  }
  const http = createHttpClient({
    transport: options.transport,
    timeoutMs,
    errorClass: CognitiveDecisionApiError,
  });

  async function submitDecision(input) {
    validateInput(input);
    const thoughtCategory = normalizeThoughtCategory(input);
    const markdownSha256 = await sha256(input.markdown);
    const idempotencyKey =
      input.idempotencyKey ||
      (await decisionIdempotencyKey(input, requester, markdownSha256, thoughtCategory));
    const body = {
      decision: input.decision,
      run_id: input.runId,
      fragment_id: input.fragmentId,
      markdown_sha256: markdownSha256,
      expected_sequence: input.expectedSequence,
      idempotency_key: idempotencyKey,
      requester,
      thought_category: thoughtCategory,
    };
    const { status, json } = await http.sendOnce(
      {
        url: `${COGNITIVE_DECISION_API_BASE_URL}/decisions`,
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          Origin: TRUSTED_ORIGIN,
          [DECISION_HEADER]: "1",
        },
        body: JSON.stringify(body),
      },
      {
        timeoutMessage: "草稿决定服务请求超时",
        serviceMessage: "草稿决定服务返回",
        invalidMessage: "草稿决定服务返回了无效响应",
        unreachableMessage: "无法连接草稿决定服务",
      }
    );
    const envelope = validateEnvelope(json);
    return { httpStatus: status, decision: envelope.data };
  }

  async function submitWithdrawal(input) {
    validateWithdrawalInput(input);
    const withdrawalId =
      input.withdrawalId || (await withdrawalIdempotencyKey(input, requester));
    // Exactly the six frozen fields; nothing else may enter the body. The
    // backend pins the sixth field to withdrawal_id (the server-recomputed
    // idempotency digest); idempotency_key belongs only to the decision body.
    const body = {
      action: WITHDRAWAL_ACTION,
      run_id: input.runId,
      fragment_id: input.fragmentId,
      expected_sequence: input.expectedSequence,
      withdrawal_id: withdrawalId,
      requester,
    };
    const { status, json } = await http.sendOnce(
      {
        url: `${COGNITIVE_DECISION_API_BASE_URL}/withdrawals`,
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          Origin: TRUSTED_ORIGIN,
          [WITHDRAWAL_HEADER]: "1",
        },
        body: JSON.stringify(body),
      },
      {
        timeoutMessage: "草稿撤回服务请求超时",
        serviceMessage: "草稿撤回服务返回",
        invalidMessage: "草稿决定服务返回了无效响应",
        unreachableMessage: "无法连接草稿撤回服务",
      }
    );
    const envelope = validateEnvelope(json);
    return { httpStatus: status, receipt: envelope.data };
  }

  return { requester, submitDecision, submitWithdrawal };
}

module.exports = {
  COGNITIVE_DECISION_API_BASE_URL,
  COGNITIVE_DECISION_CONTRACT_VERSION,
  TRUSTED_ORIGIN,
  DECISION_HEADER,
  WITHDRAWAL_HEADER,
  WITHDRAWAL_ACTION,
  REQUEST_TIMEOUT_MS,
  CognitiveDecisionApiError,
  decisionIdempotencyKey,
  withdrawalIdempotencyKey,
  isValidKeepThoughtCategory,
  createCognitiveDecisionClient,
};
