"use strict";

// P3B read-only client for the Shadow Proposal Projection API (contract v1).
// Display only: this module issues GET requests and nothing else. There is
// no approve/dismiss/execute call here — proposals are advice, never actions.
const SHADOW_API_BASE_URL = "http://127.0.0.1:5681/shadow/v1";
const CONTRACT_VERSION = "1";
const DB_MODE = "read_only";
const REQUEST_TIMEOUT_MS = 5000;
const MAX_SILENT_RETRIES = 1;

class ShadowProposalError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "ShadowProposalError";
    this.kind = kind;
    this.details = details || {};
  }
}

function validateEnvelope(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new ShadowProposalError("invalid_response", "影子建议服务返回了无效响应");
  }
  if (body.contract_version !== CONTRACT_VERSION) {
    throw new ShadowProposalError("contract_mismatch", "影子建议数据源契约版本不匹配，已停止渲染", {
      expected: CONTRACT_VERSION,
      actual: typeof body.contract_version === "undefined" ? null : body.contract_version,
    });
  }
  if (body.db_mode !== DB_MODE) {
    throw new ShadowProposalError("contract_mismatch", "影子建议数据源不是只读模式，已停止渲染", {
      expected: DB_MODE,
      actual: typeof body.db_mode === "undefined" ? null : body.db_mode,
    });
  }
  return body;
}

function createShadowProposalClient(options) {
  const transport = options.transport;
  // The P3B trust boundary freezes the service at 127.0.0.1:5681. Unlike a
  // generic client there is deliberately NO baseUrl override — a caller
  // (or injected code) must never redirect proposal traffic elsewhere,
  // matching the accepted P2 control-client hardening.
  const baseUrl = SHADOW_API_BASE_URL;
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  if (typeof transport !== "function") {
    throw new ShadowProposalError("invalid_response", "Shadow proposal client requires a transport function");
  }

  async function requestOnce(path) {
    let timer = null;
    try {
      const response = await Promise.race([
        transport({ url: `${baseUrl}${path}`, method: "GET", headers: { Accept: "application/json" } }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new ShadowProposalError("unreachable", "影子建议服务请求超时")), timeoutMs);
        }),
      ]);
      const status = response && typeof response.status === "number" ? response.status : 0;
      if (status < 200 || status >= 300) {
        const err = response && response.json && response.json.error ? response.json.error : {};
        throw new ShadowProposalError("http_error", `影子建议服务返回 HTTP ${status}`, {
          status,
          code: typeof err.code === "string" ? err.code : null,
          message: typeof err.message === "string" ? err.message : null,
        });
      }
      return validateEnvelope(response.json);
    } catch (error) {
      if (error instanceof ShadowProposalError) throw error;
      throw new ShadowProposalError("unreachable", "无法连接影子建议服务", { cause: String((error && error.message) || error) });
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  async function get(path) {
    let lastError = null;
    for (let attempt = 0; attempt <= MAX_SILENT_RETRIES; attempt += 1) {
      try {
        return await requestOnce(path);
      } catch (error) {
        lastError = error;
        if (error instanceof ShadowProposalError && error.kind === "contract_mismatch") throw error;
      }
    }
    throw lastError;
  }

  function buildQuery(params) {
    const parts = [];
    for (const [key, value] of Object.entries(params)) {
      if (value === null || typeof value === "undefined" || value === "") continue;
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
    return parts.length ? `?${parts.join("&")}` : "";
  }

  return {
    health() {
      return get("/health");
    },
    proposals(filters) {
      const f = filters || {};
      return get(`/proposals${buildQuery({ scope: f.scope, limit: f.limit })}`);
    },
    proposalDetail(proposalId) {
      return get(`/proposals/${encodeURIComponent(proposalId)}`);
    },
  };
}

module.exports = {
  SHADOW_API_BASE_URL,
  CONTRACT_VERSION,
  DB_MODE,
  REQUEST_TIMEOUT_MS,
  MAX_SILENT_RETRIES,
  ShadowProposalError,
  validateEnvelope,
  createShadowProposalClient,
};
