"use strict";

// P3C shadow review client: GET reads on the frozen loopback Shadow Review
// API at http://127.0.0.1:5682/review/v1 (contract v1); POST exists only on
// /decisions and always carries the trusted Obsidian Origin and the intent
// header. A review decision is advice bookkeeping only — it never starts a
// Worker, advances a node, or changes real task state.

const REVIEW_API_BASE_URL = "http://127.0.0.1:5682/review/v1";
const REVIEW_CONTRACT_VERSION = "1";
const TRUSTED_ORIGIN = "app://obsidian.md";
const INTENT_HEADER = "X-Loop-Review-Intent";
const REQUEST_TIMEOUT_MS = 5000;
const MAX_SILENT_RETRIES = 1;

const REVIEW_DECISIONS = ["accepted", "deferred", "rejected"];

class ShadowReviewError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "ShadowReviewError";
    this.kind = kind;
    this.details = details || {};
  }
}

function canonicalJson(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

// Deterministic idempotency key for one decision submission: sha256 over the
// full canonical request, so a retry of the SAME decision replays, while any
// edited field becomes a new decision.
async function canonicalIdempotencyKey(requester, input) {
  const canonical = [
    requester,
    input.proposalId,
    input.decision,
    input.reason || "",
    input.resumeCondition || "",
    input.note || "",
    input.expectedFingerprint,
    String(input.expectedSourceSequence),
    input.expectedProposalStatus,
    input.expectedCurrentDecisionId || "",
  ].join("\x1f");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical));
  return toHex(digest);
}

// The ONE strict envelope validator for success AND error responses
// (frozen contract): object shape, contract_version, a full RFC 3339
// generated_at, service_version, and a payload bound to the HTTP status —
// 2xx carries ONLY a legal `data`, 4xx/5xx carries ONLY a legal `error`
// (object with string code/message). Anything else is invalid_response /
// contract_mismatch and never reaches business logic.
const RFC3339_RE =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

function isLeapYear(year) {
  // Full Gregorian rule: divisible by 4, except centuries not divisible by 400.
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

function daysInMonth(year, month) {
  if (month === 2) return isLeapYear(year) ? 29 : 28;
  if (month === 4 || month === 6 || month === 9 || month === 11) return 30;
  return 31;
}

function isValidGeneratedAt(value) {
  if (typeof value !== "string") return false;
  const match = RFC3339_RE.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (month < 1 || month > 12) return false;
  if (day < 1 || day > daysInMonth(year, month)) return false;
  if (hour > 23 || minute > 59 || second > 59) return false;
  const offset = match[8];
  if (offset !== "Z") {
    const offsetHour = Number(offset.slice(1, 3));
    const offsetMinute = Number(offset.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return false;
  }
  return true;
}

function validateEnvelope(body, httpStatus) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new ShadowReviewError("invalid_response", "影子建议审核服务返回了无效响应");
  }
  if (body.contract_version !== REVIEW_CONTRACT_VERSION) {
    throw new ShadowReviewError("contract_mismatch", "影子建议审核服务契约版本不匹配，已停止渲染", {
      expected: REVIEW_CONTRACT_VERSION,
      actual: typeof body.contract_version === "undefined" ? null : body.contract_version,
    });
  }
  if (!isValidGeneratedAt(body.generated_at)) {
    throw new ShadowReviewError("invalid_response", "影子建议审核服务信封缺少有效 generated_at");
  }
  if (typeof body.service_version !== "string" || !body.service_version) {
    throw new ShadowReviewError("invalid_response", "影子建议审核服务信封缺少有效 service_version");
  }
  const hasData = Object.prototype.hasOwnProperty.call(body, "data");
  const hasError = Object.prototype.hasOwnProperty.call(body, "error");
  const isSuccess = typeof httpStatus === "number" && httpStatus >= 200 && httpStatus < 300;
  if (isSuccess) {
    if (!hasData || hasError) {
      throw new ShadowReviewError("invalid_response", "成功响应必须只携带 data");
    }
  } else {
    if (!hasError || hasData) {
      throw new ShadowReviewError("invalid_response", "错误响应必须只携带 error");
    }
    const err = body.error;
    if (
      !err ||
      typeof err !== "object" ||
      Array.isArray(err) ||
      typeof err.code !== "string" ||
      typeof err.message !== "string"
    ) {
      throw new ShadowReviewError("invalid_response", "影子建议审核服务错误信封结构不完整");
    }
  }
  return body;
}

function createShadowReviewClient(options) {
  const transport = options.transport;
  // The endpoint is frozen by contract: no baseUrl override exists. Tests
  // inject a transport; the URL is always the loopback review API.
  const baseUrl = REVIEW_API_BASE_URL;
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  const requester = options.requester || "nigo";
  if (typeof transport !== "function") {
    throw new ShadowReviewError("invalid_response", "Shadow review client requires a transport function");
  }

  function buildHeaders(intent) {
    const headers = { Accept: "application/json" };
    if (intent) {
      headers["Content-Type"] = "application/json";
      headers["Origin"] = TRUSTED_ORIGIN;
      headers[INTENT_HEADER] = "1";
    }
    return headers;
  }

  async function requestOnce(method, path, body) {
    let timer = null;
    try {
      const response = await Promise.race([
        transport({
          url: `${baseUrl}${path}`,
          method,
          headers: buildHeaders(method === "POST"),
          body: body === undefined ? undefined : JSON.stringify(body),
        }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new ShadowReviewError("unreachable", "影子建议审核服务请求超时")), timeoutMs);
        }),
      ]);
      const status = response && typeof response.status === "number" ? response.status : 0;
      let json = response ? response.json : null;
      if (json === null || typeof json === "undefined") {
        try {
          json = JSON.parse((response && response.text) || "");
        } catch {
          json = null;
        }
      }
      if (status < 200 || status >= 300) {
        // The same strict validator gates error envelopes before any
        // business error is interpreted — malformed error responses never
        // reach business state (and never surface as a network outage).
        const envelope = validateEnvelope(json, status);
        const err = envelope.error;
        throw new ShadowReviewError("http_error", `影子建议审核服务返回 HTTP ${status}`, {
          status,
          code: typeof err.code === "string" ? err.code : null,
          message: typeof err.message === "string" ? err.message : null,
        });
      }
      return { httpStatus: status, body: validateEnvelope(json, status) };
    } catch (error) {
      if (error instanceof ShadowReviewError) throw error;
      throw new ShadowReviewError("unreachable", "无法连接影子建议审核服务", {
        cause: String((error && error.message) || error),
      });
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  async function get(path) {
    let lastError = null;
    for (let attempt = 0; attempt <= MAX_SILENT_RETRIES; attempt += 1) {
      try {
        return await requestOnce("GET", path);
      } catch (error) {
        lastError = error;
        if (error instanceof ShadowReviewError && error.kind === "contract_mismatch") throw error;
      }
    }
    throw lastError;
  }

  async function submitDecision(input) {
    if (!REVIEW_DECISIONS.includes(input.decision)) {
      throw new ShadowReviewError("invalid_response", `Unknown review decision: ${input.decision}`);
    }
    if (input.decision === "rejected" && !input.reason) {
      throw new ShadowReviewError("invalid_arguments", "驳回必须填写原因");
    }
    if (input.decision === "deferred" && !input.reason && !input.resumeCondition) {
      throw new ShadowReviewError("invalid_arguments", "暂缓必须填写原因或恢复条件");
    }
    const idempotencyKey = input.idempotencyKey || (await canonicalIdempotencyKey(requester, input));
    const body = {
      proposal_id: input.proposalId,
      decision: input.decision,
      reason: input.reason || null,
      resume_condition: input.resumeCondition || null,
      note: input.note || null,
      expected_fingerprint: input.expectedFingerprint,
      expected_source_sequence: input.expectedSourceSequence,
      expected_proposal_status: input.expectedProposalStatus,
      expected_current_decision_id: input.expectedCurrentDecisionId || null,
      idempotency_key: idempotencyKey,
    };
    const result = await requestOnce("POST", "/decisions", body);
    return { httpStatus: result.httpStatus, receipt: result.body.data };
  }

  return {
    requester,
    async health() {
      const result = await get("/health");
      return result.body;
    },
    async actionsFor(proposalId) {
      const result = await get(`/actions/${encodeURIComponent(proposalId)}`);
      return result.body;
    },
    async decisionsFor(proposalId, limit) {
      const capped = Math.max(1, Math.min(100, limit || 20));
      const result = await get(`/decisions?proposal_id=${encodeURIComponent(proposalId)}&limit=${capped}`);
      return result.body;
    },
    async currentDecisions() {
      const result = await get("/decisions");
      return result.body;
    },
    submitDecision,
  };
}

module.exports = {
  REVIEW_API_BASE_URL,
  REVIEW_CONTRACT_VERSION,
  TRUSTED_ORIGIN,
  INTENT_HEADER,
  REQUEST_TIMEOUT_MS,
  REVIEW_DECISIONS,
  ShadowReviewError,
  canonicalJson,
  canonicalIdempotencyKey,
  createShadowReviewClient,
};
