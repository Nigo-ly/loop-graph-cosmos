"use strict";

const { createHttpClient } = require("./http-client.js");

const LOOP_API_BASE_URL = "http://127.0.0.1:5679/loop/v1";
const CONTRACT_VERSION = "2";
const DB_MODE = "read_only";
const REQUEST_TIMEOUT_MS = 5000;
const MAX_SILENT_RETRIES = 1;

class LoopApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "LoopApiError";
    this.kind = kind;
    this.details = details || {};
  }
}

function validateEnvelope(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new LoopApiError("invalid_response", "Loop 数据服务返回了无效响应");
  }
  if (body.contract_version !== CONTRACT_VERSION) {
    throw new LoopApiError("contract_mismatch", "Loop 数据源契约版本不匹配，已停止渲染", {
      expected: CONTRACT_VERSION,
      actual: typeof body.contract_version === "undefined" ? null : body.contract_version,
    });
  }
  if (body.db_mode !== DB_MODE) {
    throw new LoopApiError("contract_mismatch", "Loop 数据源不是只读模式，已停止渲染", {
      expected: DB_MODE,
      actual: typeof body.db_mode === "undefined" ? null : body.db_mode,
    });
  }
  return body;
}

function createLoopApiClient(options) {
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  if (typeof options.transport !== "function") {
    throw new LoopApiError("invalid_response", "API client requires a transport function");
  }
  const http = createHttpClient({
    transport: options.transport,
    timeoutMs,
    errorClass: LoopApiError,
  });
  // The endpoint is frozen by contract: no baseUrl override exists. Tests
  // and cross-repo random ports must remap in the transport layer instead.
  const baseUrl = LOOP_API_BASE_URL;

  async function requestOnce(path) {
    const { json } = await http.sendOnce(
      {
        url: `${baseUrl}${path}`,
        method: "GET",
        headers: { Accept: "application/json" },
      },
      {
        timeoutMessage: "Loop 数据服务请求超时",
        serviceMessage: "Loop 数据服务返回",
        invalidMessage: "Loop 数据服务返回了无效响应",
        unreachableMessage: "无法连接 Loop 数据服务",
      }
    );
    return validateEnvelope(json);
  }

  async function get(path) {
    let lastError = null;
    for (let attempt = 0; attempt <= MAX_SILENT_RETRIES; attempt += 1) {
      try {
        return await requestOnce(path);
      } catch (error) {
        lastError = error;
        if (error instanceof LoopApiError && error.kind === "contract_mismatch") throw error;
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
    queue() {
      return get("/queue");
    },
    runs(filters) {
      const f = filters || {};
      return get(`/runs${buildQuery({ status: f.status, display_state: f.displayState, limit: f.limit })}`);
    },
    runDetail(runId) {
      return get(`/runs/${encodeURIComponent(runId)}`);
    },
  };
}

module.exports = {
  LOOP_API_BASE_URL,
  CONTRACT_VERSION,
  DB_MODE,
  REQUEST_TIMEOUT_MS,
  MAX_SILENT_RETRIES,
  LoopApiError,
  validateEnvelope,
  createLoopApiClient,
};
