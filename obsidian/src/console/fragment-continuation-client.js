"use strict";

const CONTINUATION_API_URL = "http://127.0.0.1:5684/fragment/v1/continuations";
const CONTRACT_VERSION = "2";
const TRUSTED_ORIGIN = "app://obsidian.md";
const CONTINUATION_HEADER = "X-Fragment-Continuation";
const REQUEST_TIMEOUT_MS = 5000;

class FragmentContinuationApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "FragmentContinuationApiError";
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
  return toHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

function cleanText(value, max) {
  return typeof value === "string" && value.length > 0 && value.length <= max &&
    !/[\x00-\x1f\x7f]/.test(value);
}

function validateStatus(value) {
  const stages = new Set([
    "loop_registered", "loop_processing", "candidate_source_ready", "loop_stopped",
  ]);
  if (
    !value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.fragment_id, 128) || !cleanText(value.status, 32) ||
    !cleanText(value.current_node, 128) || !Number.isInteger(value.sequence) ||
    value.sequence < 1 || !stages.has(value.stage) || !cleanText(value.updated_at, 64)
  ) {
    throw new FragmentContinuationApiError("invalid_response", "Loop 接续状态无效");
  }
  return value;
}

function validateOutcome(value) {
  if (
    !value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.candidate_id, 128) || !cleanText(value.fragment_ref, 256) ||
    !cleanText(value.title, 120) || value.content_status !== "pending_confirmation" ||
    value.evidence_level !== "unverified" || value.has_conflict !== false ||
    !Number.isInteger(value.loop_sequence) || value.loop_sequence < 1 ||
    !["published", "already_published"].includes(value.continuation_status)
  ) {
    throw new FragmentContinuationApiError("invalid_response", "Loop 接续结果无效");
  }
  return value;
}

function createFragmentContinuationClient(options) {
  const transport = options.transport;
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  if (typeof transport !== "function") {
    throw new FragmentContinuationApiError("invalid_arguments", "Loop 接续客户端缺少本地 transport");
  }

  async function request(requestOptions = {}) {
    let timer = null;
    try {
      const response = await Promise.race([
        transport({
          url: CONTINUATION_API_URL,
          method: requestOptions.method || "GET",
          headers: {
            Accept: "application/json",
            Origin: TRUSTED_ORIGIN,
            ...(requestOptions.headers || {}),
          },
          ...(requestOptions.body ? { body: requestOptions.body } : {}),
        }),
        new Promise((unused, reject) => {
          timer = setTimeout(
            () => reject(new FragmentContinuationApiError("unreachable", "Loop 接续服务请求超时")),
            timeoutMs
          );
        }),
      ]);
      const status = response && typeof response.status === "number" ? response.status : 0;
      const envelope = response && response.json;
      if (!envelope || envelope.contract_version !== CONTRACT_VERSION) {
        throw new FragmentContinuationApiError("contract_mismatch", "Loop 接续服务契约不匹配");
      }
      if (status < 200 || status >= 300) {
        const error = envelope.error;
        throw new FragmentContinuationApiError("http_error", `Loop 接续服务返回 HTTP ${status}`, {
          status,
          code: error && typeof error.code === "string" ? error.code : null,
        });
      }
      return envelope.data;
    } catch (error) {
      if (error instanceof FragmentContinuationApiError) throw error;
      throw new FragmentContinuationApiError("unreachable", "无法连接本地 Loop 接续服务");
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  async function list() {
    const data = await request();
    if (!Array.isArray(data)) {
      throw new FragmentContinuationApiError("invalid_response", "Loop 接续状态列表无效");
    }
    return data.map(validateStatus);
  }

  async function continueResearch(input) {
    if (
      !cleanText(input.fragmentId, 128) || !cleanText(input.rawRef, 512) ||
      !cleanText(input.organizedRef, 512) || typeof input.rawText !== "string" ||
      typeof input.organizedText !== "string"
    ) {
      throw new FragmentContinuationApiError("invalid_arguments", "Loop 接续文件绑定无效");
    }
    const body = {
      fragment_id: input.fragmentId,
      raw_ref: input.rawRef,
      organized_ref: input.organizedRef,
      raw_sha256: await sha256(input.rawText),
      organized_sha256: await sha256(input.organizedText),
      route: "research",
      requester: "nigo",
    };
    return validateOutcome(await request({
      method: "POST",
      headers: { "Content-Type": "application/json", [CONTINUATION_HEADER]: "1" },
      body: JSON.stringify(body),
    }));
  }

  return { list, continueResearch };
}

module.exports = {
  CONTINUATION_API_URL,
  CONTINUATION_HEADER,
  FragmentContinuationApiError,
  validateStatus,
  validateOutcome,
  createFragmentContinuationClient,
};
