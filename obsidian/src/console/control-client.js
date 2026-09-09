"use strict";

// P2B control client: the ONLY module allowed to issue POST, and only to the
// frozen loopback Control API at http://127.0.0.1:5680/control/v1 (contract
// v1). The P1 api-client stays GET-only on 5679. No database access, no
// vault writes, no other URLs.

const CONTROL_API_BASE_URL = "http://127.0.0.1:5680/control/v1";
const CONTROL_CONTRACT_VERSION = "1";
const TRUSTED_ORIGIN = "app://obsidian.md";
const INTENT_HEADER = "X-Loop-Control-Intent";
const REQUEST_TIMEOUT_MS = 5000;

const CONTROL_ACTIONS = ["pause", "resume", "terminate", "retry", "priority"];
const PRIORITY_MIN = -10;
const PRIORITY_MAX = 10;

class ControlApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "ControlApiError";
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

// Mirrors control_api.service.canonical_idempotency_key exactly: sha256 over
// requester, run id, action, expected sequence, and normalized arguments
// joined by U+001F. The server recomputes and compares this value.
async function canonicalIdempotencyKey(requester, runId, action, expectedSequence, args) {
  const canonical = [
    requester,
    runId,
    action,
    String(expectedSequence),
    canonicalJson(args || {}),
  ].join("\x1f");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical));
  return toHex(digest);
}

function validateEnvelope(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new ControlApiError("invalid_response", "Loop 控制服务返回了无效响应");
  }
  if (body.contract_version !== CONTROL_CONTRACT_VERSION) {
    throw new ControlApiError("contract_mismatch", "Loop 控制服务契约版本不匹配，已停止操作", {
      expected: CONTROL_CONTRACT_VERSION,
      actual: typeof body.contract_version === "undefined" ? null : body.contract_version,
    });
  }
  return body;
}

function createControlClient(options) {
  const transport = options.transport;
  // The endpoint is frozen by contract: no baseUrl override exists. Tests
  // inject a transport; the URL is always the loopback Control API.
  const baseUrl = CONTROL_API_BASE_URL;
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  const requester = options.requester || "nigo";
  if (typeof transport !== "function") {
    throw new ControlApiError("invalid_response", "Control client requires a transport function");
  }

  function buildHeaders(intent) {
    const headers = { Accept: "application/json", Origin: TRUSTED_ORIGIN };
    if (intent) {
      headers["Content-Type"] = "application/json";
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
          timer = setTimeout(() => reject(new ControlApiError("unreachable", "Loop 控制服务请求超时")), timeoutMs);
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
        const err = json && json.error ? json.error : {};
        throw new ControlApiError("http_error", `Loop 控制服务返回 HTTP ${status}`, {
          status,
          code: typeof err.code === "string" ? err.code : null,
          message: typeof err.message === "string" ? err.message : null,
        });
      }
      return { httpStatus: status, body: validateEnvelope(json) };
    } catch (error) {
      if (error instanceof ControlApiError) throw error;
      throw new ControlApiError("unreachable", "无法连接 Loop 控制服务", {
        cause: String((error && error.message) || error),
      });
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  async function submitIntent(input) {
    const action = input.action;
    if (!CONTROL_ACTIONS.includes(action)) {
      throw new ControlApiError("invalid_response", `Unknown control action: ${action}`);
    }
    const args = input.arguments || {};
    if (action === "priority") {
      const priority = args.priority;
      if (!Number.isInteger(priority) || priority < PRIORITY_MIN || priority > PRIORITY_MAX) {
        throw new ControlApiError("invalid_arguments", "优先级必须是 -10 到 10 的整数");
      }
    }
    const idempotencyKey =
      input.idempotencyKey ||
      (await canonicalIdempotencyKey(requester, input.runId, action, input.expectedSequence, args));
    const body = {
      run_id: input.runId,
      action,
      expected_sequence: input.expectedSequence,
      idempotency_key: idempotencyKey,
      arguments: args,
      requester,
    };
    const result = await requestOnce("POST", "/intents", body);
    return { httpStatus: result.httpStatus, receipt: result.body.data };
  }

  return {
    requester,
    async health() {
      const result = await requestOnce("GET", "/health");
      return result.body;
    },
    async actionsFor(runId) {
      const result = await requestOnce("GET", `/actions/${encodeURIComponent(runId)}`);
      return result.body;
    },
    async listIntents(runId, limit) {
      const capped = Math.max(1, Math.min(100, limit || 20));
      const result = await requestOnce(
        "GET",
        `/intents?run_id=${encodeURIComponent(runId)}&limit=${capped}`
      );
      return result.body;
    },
    async getIntent(intentId) {
      const result = await requestOnce("GET", `/intents/${encodeURIComponent(intentId)}`);
      return result.body;
    },
    submitIntent,
  };
}

module.exports = {
  CONTROL_API_BASE_URL,
  CONTROL_CONTRACT_VERSION,
  TRUSTED_ORIGIN,
  INTENT_HEADER,
  REQUEST_TIMEOUT_MS,
  CONTROL_ACTIONS,
  PRIORITY_MIN,
  PRIORITY_MAX,
  ControlApiError,
  canonicalJson,
  canonicalIdempotencyKey,
  createControlClient,
};
