"use strict";

// G4 shared client helper（Design §11.1）：只提取无业务知识的四件事——
// timeout、transport invocation、HTTP status 门、JSON/envelope 基础形态
// 与稳定错误包装。各 client 继续拥有：冻结 URL/Origin/专用 header、
// idempotency digest、contract version/db_mode、业务字段闭集与中文错误。
// 本 helper 不引入重试、不拼接 URL 路径、不理解任何业务字段。

function createHttpClient(options) {
  const transport = options.transport;
  const timeoutMs = options.timeoutMs || 5000;
  // errorClass 由各 client 传入（构造签名 (kind, message, details)），
  // 迁移前后抛出的错误类型逐字不变（golden 锁定）。
  const errorClass = options.errorClass;
  if (typeof transport !== "function" || typeof errorClass !== "function") {
    throw new TypeError("createHttpClient requires transport and errorClass");
  }

  // 单次传输 + 超时 + status 门 + 基础 envelope 形态（object、非数组）。
  // labels 的中文文案由各 client 提供，helper 不内置任何业务文案。
  async function sendOnce(request, labels) {
    let timer = null;
    try {
      const response = await Promise.race([
        transport(request),
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(new errorClass("unreachable", labels.timeoutMessage)),
            timeoutMs
          );
        }),
      ]);
      const status =
        response && typeof response.status === "number" ? response.status : 0;
      const json = response ? response.json : undefined;
      if (status < 200 || status >= 300) {
        const error = json && json.error ? json.error : {};
        const details =
          typeof labels.errorDetails === "function"
            ? labels.errorDetails(status, error)
            : {
                status,
                code: typeof error.code === "string" ? error.code : null,
                message: typeof error.message === "string" ? error.message : null,
              };
        throw new errorClass(
          "http_error",
          `${labels.serviceMessage} HTTP ${status}`,
          details
        );
      }
      if (!json || typeof json !== "object" || Array.isArray(json)) {
        throw new errorClass(
          labels.invalidKind || "invalid_response",
          labels.invalidMessage
        );
      }
      return { status, json };
    } catch (error) {
      if (error instanceof errorClass) throw error;
      throw new errorClass("unreachable", labels.unreachableMessage, {
        cause: String((error && error.message) || error),
      });
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  return { sendOnce };
}

module.exports = { createHttpClient };
