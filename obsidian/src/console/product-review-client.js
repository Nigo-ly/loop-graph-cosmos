"use strict";

const PRODUCT_REVIEW_API_BASE_URL = "http://127.0.0.1:5684/fragment/v1/product-reviews";
const CONTRACT_VERSION = "2";
const TRUSTED_ORIGIN = "app://obsidian.md";
const DECISION_HEADER = "X-Loop-Product-Decision";
const WITHDRAWAL_HEADER = "X-Loop-Product-Withdrawal";
const REQUEST_TIMEOUT_MS = 5000;

class ProductReviewApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "ProductReviewApiError";
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

async function productDecisionId(body) {
  return sha256([
    body.requester,
    body.action,
    body.candidate_id,
    body.fragment_ref,
    body.content_sha256,
    String(body.expected_revision),
    body.selected_card_ids.join(","),
  ].join("\x1f"));
}

function validateEnvelope(response) {
  if (!response || typeof response !== "object" || response.contract_version !== CONTRACT_VERSION) {
    throw new ProductReviewApiError("contract_mismatch", "人工确认服务契约不匹配");
  }
  return response.data;
}

function validateCandidate(value, detail = false) {
  const statuses = new Set([
    "pending_confirmation", "kept_draft", "published_asset",
    "rejected", "withdrawn_asset", "conflict",
  ]);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ProductReviewApiError("invalid_response", "人工确认服务返回了无效候选");
  }
  if (
    typeof value.candidate_id !== "string" ||
    typeof value.title !== "string" ||
    typeof value.fragment_ref !== "string" ||
    typeof value.note_path !== "string" ||
    !statuses.has(value.content_status) ||
    value.evidence_level !== "unverified" ||
    !Number.isInteger(value.revision) ||
    typeof value.content_sha256 !== "string" ||
    !Array.isArray(value.available_actions)
  ) {
    throw new ProductReviewApiError("invalid_response", "人工确认候选字段无效");
  }
  if (detail && !Array.isArray(value.candidate_cards)) {
    throw new ProductReviewApiError("invalid_response", "候选知识卡字段无效");
  }
  return value;
}

function createProductReviewClient(options) {
  const transport = options.transport;
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  if (typeof transport !== "function") {
    throw new ProductReviewApiError("invalid_arguments", "人工确认客户端缺少本地 transport");
  }

  async function request(path, requestOptions = {}) {
    let timer = null;
    try {
      const response = await Promise.race([
        transport({
          url: `${PRODUCT_REVIEW_API_BASE_URL}${path}`,
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
            () => reject(new ProductReviewApiError("unreachable", "人工确认服务请求超时")),
            timeoutMs
          );
        }),
      ]);
      const status = response && typeof response.status === "number" ? response.status : 0;
      if (status < 200 || status >= 300) {
        const error = response && response.json && response.json.error;
        throw new ProductReviewApiError("http_error", `人工确认服务返回 HTTP ${status}`, {
          status,
          code: error && typeof error.code === "string" ? error.code : null,
        });
      }
      return validateEnvelope(response.json);
    } catch (error) {
      if (error instanceof ProductReviewApiError) throw error;
      throw new ProductReviewApiError("unreachable", "无法连接本地人工确认服务");
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  async function list() {
    const data = await request("");
    if (!Array.isArray(data)) {
      throw new ProductReviewApiError("invalid_response", "人工确认列表无效");
    }
    return data.map((item) => validateCandidate(item));
  }

  async function detail(candidateId) {
    return validateCandidate(await request(`/${encodeURIComponent(candidateId)}`), true);
  }

  async function submit(item, action, selectedCardIds = []) {
    const body = {
      action,
      candidate_id: item.candidate_id,
      fragment_ref: item.fragment_ref,
      content_sha256: item.content_sha256,
      expected_revision: item.revision,
      selected_card_ids: [...selectedCardIds],
      requester: "nigo",
    };
    body.decision_id = await productDecisionId(body);
    const withdrawal = action === "withdraw_asset";
    return request(`/${encodeURIComponent(item.candidate_id)}/${withdrawal ? "withdrawals" : "decisions"}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [withdrawal ? WITHDRAWAL_HEADER : DECISION_HEADER]: "1",
      },
      body: JSON.stringify(body),
    });
  }

  return { list, detail, submit };
}

module.exports = {
  PRODUCT_REVIEW_API_BASE_URL,
  ProductReviewApiError,
  productDecisionId,
  validateCandidate,
  createProductReviewClient,
};
