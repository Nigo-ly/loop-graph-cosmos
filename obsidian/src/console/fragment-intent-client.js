"use strict";

const ALIGNMENTS_URL = "http://127.0.0.1:5684/fragment/v1/alignments";
const RESEARCH_RUNS_URL = "http://127.0.0.1:5684/graph/v1/research-runs";
const CONTRACT_VERSION = "2";
const TRUSTED_ORIGIN = "app://obsidian.md";
const ALIGNMENT_HEADER = "X-Fragment-Alignment";
const DECISION_HEADER = "X-Fragment-Alignment-Decision";
const ESCALATION_HEADER = "X-Fragment-Alignment-Escalation";
const CONTINUATION_HEADER = "X-Fragment-Episode-Continuation";
const RESEARCH_RUN_CREATE_HEADER = "X-Graph-Research-Run-Create";
const RESEARCH_SPEC_ID = "fragment-research-escalation-v1";
// rev6：新提案切换到宏 v3；v1 仅用于旧提案/run 的只读重放。
const RESEARCH_MACRO_V3_SPEC_ID = "fragment-research-macro-v3";
const REQUEST_TIMEOUT_MS = 5000;
const STABLE_INTENTS = new Set([
  "save", "verify", "learn", "evaluate_relevance", "explore",
  "deploy_or_build", "plan_action", "track",
]);
const ROUTES = new Set(["save_only", "direct", "verify", "graph"]);
const MATURITY = new Set(["candidate", "qualified", "reusable"]);
const RESEARCH_RELATIONS = new Set([
  "supports", "partially_supports", "conflicts", "irrelevant",
]);
const EVIDENCE_MARKERS = new Set([
  "inherited", "newly_collected", "omitted", "stale",
]);

class FragmentIntentApiError extends Error {
  constructor(kind, message, details) {
    super(message);
    this.name = "FragmentIntentApiError";
    this.kind = kind;
    this.details = details || {};
  }
}

function cleanText(value, limit, empty = false) {
  return typeof value === "string" && value.length <= limit &&
    (empty || value.length > 0) && !/[\x00-\x1f\x7f]/.test(value);
}

function cleanList(value, limit, itemLimit) {
  return Array.isArray(value) && value.length <= limit &&
    value.every((item) => cleanText(item, itemLimit));
}

function toHex(buffer) {
  return [...new Uint8Array(buffer)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function fragmentInputDigest(rawText, organizedText) {
  if (typeof rawText !== "string" || typeof organizedText !== "string") {
    throw new FragmentIntentApiError("invalid_arguments", "碎片内容绑定无效");
  }
  const canonical = JSON.stringify({ raw_text: rawText, organized_text: organizedText });
  return toHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical)));
}

async function digestIdentity(value) {
  return toHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(value))));
}

function validateScope(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !cleanList(value.capabilities, 8, 64) ||
    !cleanList(value.external_scope, 8, 120) ||
    !Number.isInteger(value.model_call_cap) || value.model_call_cap < 0 ||
    value.model_call_cap > 8 || typeof value.cost_cap_cny !== "number" ||
    value.cost_cap_cny < 0 || value.cost_cap_cny > 100 ||
    !cleanText(value.side_effect, 80)) {
    throw new FragmentIntentApiError("invalid_response", "处理范围无效");
  }
  const hasPolicy = ["model_provider", "model_name", "write_scope"]
    .some((key) => Object.hasOwn(value, key));
  if (hasPolicy && (
    !cleanText(value.model_provider, 40, true) ||
    !cleanText(value.model_name, 80, true) ||
    !cleanList(value.write_scope, 8, 80) ||
    (value.model_call_cap === 0 && (value.model_provider || value.model_name)) ||
    (value.model_call_cap > 0 && (!value.model_provider || !value.model_name))
  )) {
    throw new FragmentIntentApiError("invalid_response", "处理范围无效");
  }
  return value;
}

// 当前套餐能力配置独立于原提案；槽位包含未知调用与复核，不等同实际模型调用数。
function validateEffectiveScope(value) {
  if (value === null || value === undefined) return value;
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    value.basis !== "currently_selected_subscription_configuration" ||
    !["exact_run_allowlist", "prospective_public_capture"].includes(value.selection_basis) ||
    !["kimi_subscription", "codex_subscription"].includes(value.model_provider) ||
    value.model_name !== null || value.billing_mode !== "existing_subscription" ||
    !Number.isSafeInteger(value.model_call_cap) || value.model_call_cap < 0 ||
    value.model_call_cap_basis !== "total_agent_invocation_slots_including_unknown_and_review" ||
    !Number.isSafeInteger(value.tool_call_cap) || value.tool_call_cap < 0 ||
    value.tool_call_cap_basis !== "total_new_tool_receipts_per_run" ||
    !["capabilities", "external_scope", "write_scope", "exclusions"].every(key => cleanList(value[key], 32, 500)) ||
    typeof value.repository_trial_allowed !== "boolean" || typeof value.automatic_knowledge_save_allowed !== "boolean" ||
    (value.observed_model_provider !== null && !cleanText(value.observed_model_provider, 80)) ||
    !["recorded", "unavailable", "not_recorded"].includes(value.knowledge_publication_status)) {
    throw new FragmentIntentApiError("invalid_response", "当前套餐执行范围无效");
  }
  return value;
}

function effectiveExecutionScopeOf(item) {
  const scope = item && Object.hasOwn(item, "effective_execution_scope")
    ? item.effective_execution_scope : item?.execution?.effective_execution_scope;
  return item?.execution?.subscription_selected === true &&
    scope?.basis === "currently_selected_subscription_configuration" ? scope : null;
}

function validateAlignment(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.alignment_id, 80) || !cleanText(value.fragment_id, 128) ||
    !cleanText(value.case_id, 80) || !cleanText(value.episode_id, 80) ||
    !cleanText(value.title, 120) || !["suggested", "passed"].includes(value.status) ||
    !Number.isInteger(value.sequence) || value.sequence < 1 ||
    !Number.isInteger(value.revision) || value.revision < 1 ||
    !/^[0-9a-f]{64}$/.test(value.input_digest) ||
    !cleanText(value.reasoning, 300) || !cleanText(value.plan, 500) ||
    !cleanText(value.expected_result, 300) ||
    !cleanList(value.exclusions, 8, 120) ||
    !cleanList(value.suggested_intents, 8, 40) ||
    value.suggested_intents.some((item) => !STABLE_INTENTS.has(item)) ||
    !ROUTES.has(value.recommended_route)) {
    throw new FragmentIntentApiError("invalid_response", "意图确认状态无效");
  }
  const dynamic = value.dynamic_intents;
  if (!Array.isArray(dynamic) || dynamic.length > 3 || dynamic.some((item) =>
    !item || typeof item !== "object" || Array.isArray(item) ||
    !cleanText(item.id, 40) || STABLE_INTENTS.has(item.id) ||
    !cleanText(item.label, 80) || !cleanText(item.basis, 160))) {
    throw new FragmentIntentApiError("invalid_response", "动态意图无效");
  }
  validateScope(value.execution_scope);
  validateEffectiveScope(value.effective_execution_scope);
  if (!Array.isArray(value.memory_basis) || value.memory_basis.length > 3 ||
    value.memory_basis.some((item) => !item || typeof item !== "object" ||
      Array.isArray(item) || !cleanText(item.label, 120) || !MATURITY.has(item.maturity))) {
    throw new FragmentIntentApiError("invalid_response", "记忆依据无效");
  }
  if (value.decision !== undefined) {
    if (!value.decision || typeof value.decision !== "object" ||
      !["confirm", "save_only"].includes(value.decision.action) ||
      !cleanList(value.decision.intents, 11, 40) ||
      !cleanText(value.decision.supplement, 500, true) || !ROUTES.has(value.route)) {
      throw new FragmentIntentApiError("invalid_response", "确认结果无效");
    }
  }
  if (value.execution !== undefined) validateExecution(value.execution);
  return value;
}

// research_progress 只读投影（只来自 execution Run Checkpoint 的安全字段）：
// 存在时必须严格合法；缺省（null/undefined）表示老版本或 direct 路线，诚实降级。
function validateResearchProgress(value) {
  if (value === null || value === undefined) return value;
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.stage, 40) ||
    !Number.isInteger(value.collected_sources) || value.collected_sources < 0 ||
    !Number.isInteger(value.model_calls) || value.model_calls < 0 ||
    (value.network_requests !== undefined &&
      (!Number.isInteger(value.network_requests) || value.network_requests < 0)) ||
    (value.stop_reason !== undefined && value.stop_reason !== null &&
      !cleanText(value.stop_reason, 160)) ||
    (value.note !== undefined && !cleanText(value.note, 200))) {
    throw new FragmentIntentApiError("invalid_response", "研究进度投影无效");
  }
  return value;
}

// 与后端 subscription_research.RESULT_SCHEMA / validate_decision 的公开结果边界一致。
// 文本只进入 textContent 或安全 Markdown 渲染；保留换行，不截断真实结论。
const RESEARCH_RESULT_ITEMS = 24;
const RESEARCH_RESULT_CHARS = 24000;
function researchText(value, empty = false) {
  return typeof value === "string" && (empty || value.trim().length > 0) &&
    [...value].length <= RESEARCH_RESULT_CHARS && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value);
}
function researchList(value, check = researchText) {
  return Array.isArray(value) && value.length <= RESEARCH_RESULT_ITEMS &&
    value.every((item) => check(item));
}
function researchObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// 旧 direct / 采集结果可以没有这些字段；存在时必须满足真实结果契约。
function validateResearchResultExtras(result) {
  if (result.recommendation !== undefined && !researchText(result.recommendation)) {
    throw new FragmentIntentApiError("invalid_response", "研究建议行动无效");
  }
  if (result.confirmed !== undefined && !researchList(result.confirmed, (item) =>
    researchObject(item) && researchText(item.claim) && researchList(item.evidence_ids) && item.evidence_ids.length > 0)) {
    throw new FragmentIntentApiError("invalid_response", "研究已确认结论无效");
  }
  if (result.conflicts !== undefined && !researchList(result.conflicts, (item) =>
    researchObject(item) && researchText(item.topic) && researchList(item.dimensions) &&
    researchList(item.evidence_ids) && item.evidence_ids.length > 0)) {
    throw new FragmentIntentApiError("invalid_response", "研究来源冲突无效");
  }
  if (result.claims !== undefined && !researchList(result.claims, (item) =>
    researchObject(item) && researchText(item.claim) && researchText(item.evidence_id) && RESEARCH_RELATIONS.has(item.relation))) {
    throw new FragmentIntentApiError("invalid_response", "研究主张记录无效");
  }
  if (result.answer_markdown !== undefined && !researchText(result.answer_markdown)) {
    throw new FragmentIntentApiError("invalid_response", "研究完整正文无效");
  }
  if (result.coverage !== undefined && !researchList(result.coverage, (item) =>
    researchObject(item) && researchText(item.question) && researchText(item.answer, true) &&
    researchList(item.evidence_ids) && ["answered", "unknown"].includes(item.status) &&
    (item.status !== "answered" || item.evidence_ids.length > 0))) {
    throw new FragmentIntentApiError("invalid_response", "研究逐项回答无效");
  }
  if (result.agent_usage !== undefined && (!researchObject(result.agent_usage) ||
    !researchText(result.agent_usage.when_to_use) || !researchList(result.agent_usage.steps) ||
    !researchList(result.agent_usage.limitations))) {
    throw new FragmentIntentApiError("invalid_response", "研究 Agent 使用说明无效");
  }
  if (result.topic !== undefined && (!researchObject(result.topic) ||
    !["category", "subcategory", "title"].every((key) => researchText(result.topic[key])) ||
    !researchText(result.topic.existing_topic_id, true))) {
    throw new FragmentIntentApiError("invalid_response", "研究主题信息无效");
  }
}

// candidate evidence records（证据与来源段）：只有安全字段进投影；技术 ID
// 允许存在但只能进折叠审计区。
function validateResearchEvidence(value) {
  if (value === null || value === undefined) return value;
  if (!Array.isArray(value) || value.length > 16 ||
    value.some((record) => !record || typeof record !== "object" ||
      Array.isArray(record) || !cleanText(record.title, 200) ||
      !cleanText(record.url, 300) || !EVIDENCE_MARKERS.has(record.marker) ||
      (record.source_target !== undefined && !cleanText(record.source_target, 80)) ||
      (record.evidence_id !== undefined && !cleanText(record.evidence_id, 80)) ||
      (record.evidence_digest !== undefined &&
        !/^[0-9a-f]{64}$/.test(record.evidence_digest)))) {
    throw new FragmentIntentApiError("invalid_response", "研究证据记录无效");
  }
  return value;
}

// graph_proposal（graph 路线下尚未获准执行的升级提案）：存在时严格校验；
// 创建材料字段（spec/digest/escalation）缺一即视为不合法提案，不显示创建入口。
function validateGraphEscalation(value) {
  if (value === null || value === undefined) return value;
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.status, 40) ||
    (value.reason !== undefined && !cleanText(value.reason, 300, true)) ||
    (value.source_result_digest !== undefined &&
      !/^[0-9a-f]{64}$/.test(value.source_result_digest)) ||
    (value.spec_id !== undefined && !cleanText(value.spec_id, 80)) ||
    (value.spec_digest !== undefined && !/^[0-9a-f]{64}$/.test(value.spec_digest)) ||
    (value.evidence_bundle_digest !== undefined &&
      !/^[0-9a-f]{64}$/.test(value.evidence_bundle_digest)) ||
    (value.escalation_id !== undefined && !cleanText(value.escalation_id, 80))) {
    throw new FragmentIntentApiError("invalid_response", "Graph 升级提案无效");
  }
  return value;
}

function validateResearchRunOutcome(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.run_id, 256) || !value.run_id.startsWith("exec:") ||
    !cleanText(value.status, 40) ||
    !Number.isInteger(value.model_calls) || value.model_calls < 0 ||
    (value.sequence !== undefined &&
      (!Number.isInteger(value.sequence) || value.sequence < 0)) ||
    (value.receipt_digest !== undefined &&
      !/^[0-9a-f]{64}$/.test(value.receipt_digest))) {
    throw new FragmentIntentApiError("invalid_response", "研究 Run 创建结果无效");
  }
  return value;
}

// 提案入口可见性（同步结构判定）：只有投影给出合法 graph_proposal（未获准
// 执行 + 完整创建材料 + 结果绑定）才允许显示创建入口；escalation_id 与结果
// 的确定性绑定在 createResearchRun 发送前再做一次失败关闭复核。
function researchProposalReady(item) {
  const execution = item && item.execution;
  const proposal = execution && execution.graph_escalation;
  return Boolean(item && execution && proposal &&
    proposal.status === "proposed" &&
    (proposal.spec_id === RESEARCH_SPEC_ID || proposal.spec_id === RESEARCH_MACRO_V3_SPEC_ID) &&
    /^[0-9a-f]{64}$/.test(proposal.spec_digest || "") &&
    /^[0-9a-f]{64}$/.test(proposal.evidence_bundle_digest || "") &&
    /^escalation:[0-9a-f]{24}$/.test(proposal.escalation_id || "") &&
    /^[0-9a-f]{64}$/.test(execution.result_digest || "") &&
    cleanText(item.episode_id, 80));
}

async function researchCreateMaterial(item) {
  if (!researchProposalReady(item)) return null;
  const execution = item.execution;
  const proposal = execution.graph_escalation;
  const expected = `escalation:${(await digestIdentity([
    "fragment-escalation-v1", item.alignment_id, execution.result_digest,
  ])).slice(0, 24)}`;
  if (proposal.escalation_id !== expected) return null;
  return {
    spec_id: proposal.spec_id,
    spec_digest: proposal.spec_digest,
    alignment_id: item.alignment_id,
    episode_id: item.episode_id,
    evidence_bundle_digest: proposal.evidence_bundle_digest,
    escalation_id: proposal.escalation_id,
    requester: "nigo",
  };
}

// 控制结果摘要继续绑定 continuation；研究发布绑定独立的研究正文摘要。
// 新字段显式 null 表示尚无研究结果，不得回退认领旧发布回执。
function researchResultDigestOf(execution) {
  return execution && Object.hasOwn(execution, "research_result_digest")
    ? execution.research_result_digest : execution?.result_digest;
}

// 只有绑定本次结果的系统发布回执才证明已经沉淀；旧修订不能替代新结果。
function knowledgePublicationOf(execution) {
  const value = execution?.knowledge_publication;
  const resultDigest = researchResultDigestOf(execution);
  if (!value || typeof value !== "object" || Array.isArray(value)
    || !/^knowledge-[a-f0-9]{24}$/.test(value.knowledge_id || "")
    || !Number.isInteger(value.revision) || value.revision < 1
    || !cleanText(value.path, 1024) || value.path.startsWith("/")
    || value.path.includes("\\") || value.path.includes(":") || value.path.split("/").includes("..") || !value.path.endsWith(".md")
    || value.status === "unavailable" || value.publication_source !== "system_policy"
    || !/^[a-f0-9]{64}$/.test(value.result_digest || "")
    || value.result_digest !== resultDigest || execution.status !== "passed") return null;
  return value;
}

function validateExecution(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.run_id, 96) || !cleanText(value.fragment_id, 128) ||
    !cleanText(value.status, 32) || !cleanText(value.current_node, 64, true) ||
    !["direct", "verify"].includes(value.route) ||
    !cleanText(value.stop_reason || "", 160, true) ||
    !cleanText(value.updated_at, 40) ||
    (value.result_digest !== null && value.result_digest !== undefined &&
      !/^[0-9a-f]{64}$/.test(value.result_digest)) ||
    (value.research_result_digest !== null && value.research_result_digest !== undefined &&
      !/^[0-9a-f]{64}$/.test(value.research_result_digest))) {
    throw new FragmentIntentApiError("invalid_response", "执行状态无效");
  }
  if (!Array.isArray(value.harvest) || value.harvest.length > 8 ||
    value.harvest.some((item) => !item || typeof item !== "object" ||
      !cleanText(item.role, 40) || !cleanText(item.summary, 500) ||
      !MATURITY.has(item.maturity))) {
    throw new FragmentIntentApiError("invalid_response", "经验候选无效");
  }
  if (value.result !== null && value.result !== undefined) {
    const result = value.result;
    if (!result || typeof result !== "object" || Array.isArray(result) ||
      !researchText(result.summary) || !researchList(result.unknowns) ||
      !cleanList(result.next_checks, 8, 300) ||
      typeof result.needs_escalation !== "boolean" ||
      !cleanText(result.escalation_reason, 300, true) ||
      !Number.isInteger(result.model_calls) || result.model_calls < 0 ||
      !Number.isInteger(result.tool_calls) || result.tool_calls < 0) {
      throw new FragmentIntentApiError("invalid_response", "处理结果无效");
    }
    validateResearchResultExtras(result);
  }
  validateEffectiveScope(value.effective_execution_scope);
  validateResearchProgress(value.research_progress);
  validateResearchEvidence(value.research_evidence);
  validateGraphEscalation(value.graph_escalation);
  return value;
}

function validateEscalation(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
    !cleanText(value.escalation_id, 80) || value.status !== "proposed" ||
    !cleanText(value.reason, 300) || value.capability_status !== "template_required" ||
    value.graph_run_created !== false) {
    throw new FragmentIntentApiError("invalid_response", "Graph 升级提案无效");
  }
  return value;
}

function createFragmentIntentClient(options) {
  const transport = options.transport;
  const timeoutMs = options.timeoutMs || REQUEST_TIMEOUT_MS;
  if (typeof transport !== "function") {
    throw new FragmentIntentApiError("invalid_arguments", "意图确认客户端缺少本地 transport");
  }

  async function requestFull(path = "", requestOptions = {}) {
    let timer = null;
    const label = requestOptions.serviceLabel || "意图确认";
    const requestTimeoutMs = requestOptions.timeoutMs || timeoutMs;
    try {
      const response = await Promise.race([
        transport({
          url: requestOptions.url || `${ALIGNMENTS_URL}${path}`,
          method: requestOptions.method || "GET",
          headers: { Accept: "application/json", Origin: TRUSTED_ORIGIN, ...(requestOptions.headers || {}) },
          ...(requestOptions.body ? { body: requestOptions.body } : {}),
        }),
        new Promise((unused, reject) => {
          timer = setTimeout(
            () => reject(new FragmentIntentApiError("unreachable", `${label}服务请求超时`)),
            requestTimeoutMs
          );
        }),
      ]);
      const status = response && typeof response.status === "number" ? response.status : 0;
      const envelope = response && response.json;
      if (!envelope || envelope.contract_version !== CONTRACT_VERSION) {
        throw new FragmentIntentApiError("contract_mismatch", `${label}服务契约不匹配`);
      }
      if (status < 200 || status >= 300) {
        throw new FragmentIntentApiError("http_error", `${label}服务返回 HTTP ${status}`, {
          status,
          code: envelope.error && typeof envelope.error.code === "string" ? envelope.error.code : null,
        });
      }
      return envelope;
    } catch (error) {
      if (error instanceof FragmentIntentApiError) throw error;
      throw new FragmentIntentApiError("unreachable", `无法连接本地${label}服务`);
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  async function request(path = "", requestOptions = {}) {
    return (await requestFull(path, requestOptions)).data;
  }

  let lastAutoProposeReport = [];

  async function list() {
    const envelope = await requestFull();
    const data = envelope.data;
    if (!Array.isArray(data)) {
      throw new FragmentIntentApiError("invalid_response", "意图确认列表无效");
    }
    /* 自动承接逐条结果（内存投影）：缺失/非数组时诚实置空，展示层据实际
       report 区分接管中/等待上游/失败/历史，不凭猜测显示状态。 */
    const report = envelope.meta && Array.isArray(envelope.meta.auto_propose)
      ? envelope.meta.auto_propose
      : [];
    lastAutoProposeReport = report.filter(
      (entry) => entry && typeof entry.fragment_id === "string" && typeof entry.outcome === "string"
    ).map((entry) => ({
      fragment_id: entry.fragment_id,
      outcome: entry.outcome,
      reason: typeof entry.reason === "string" ? entry.reason : "",
    }));
    return data.map(validateAlignment);
  }

  function autoProposeReport() {
    return lastAutoProposeReport;
  }

  async function propose(input) {
    if (!cleanText(input.fragmentId, 128) || !/^[0-9a-f]{64}$/.test(input.inputDigest)) {
      throw new FragmentIntentApiError("invalid_arguments", "碎片绑定无效");
    }
    return validateAlignment(await request("", {
      method: "POST",
      headers: { "Content-Type": "application/json", [ALIGNMENT_HEADER]: "1" },
      body: JSON.stringify({
        fragment_id: input.fragmentId,
        input_digest: input.inputDigest,
        requester: "nigo",
      }),
    }));
  }

  async function decide(item, input) {
    const allowed = new Set([
      ...STABLE_INTENTS,
      ...item.dynamic_intents.map((dynamic) => dynamic.id),
    ]);
    if (!Array.isArray(input.intents) || !input.intents.length ||
      input.intents.some((intent) => !allowed.has(intent)) ||
      !cleanText(input.supplement || "", 500, true) ||
      !["confirm", "save_only"].includes(input.action)) {
      throw new FragmentIntentApiError("invalid_arguments", "确认内容无效");
    }
    return validateAlignment(await request(
      `/${encodeURIComponent(item.alignment_id)}/decisions`,
      {
        method: "POST",
        timeoutMs: 50000,
        headers: { "Content-Type": "application/json", [DECISION_HEADER]: "1" },
        body: JSON.stringify({
          alignment_id: item.alignment_id,
          revision: item.revision,
          input_digest: item.input_digest,
          intents: input.action === "save_only" ? ["save"] : input.intents,
          supplement: input.action === "save_only" ? "" : input.supplement || "",
          action: input.action,
          requester: "nigo",
        }),
      }
    ));
  }

  async function continueEpisode(item, goal) {
    const execution = item && item.execution;
    if (!cleanText(goal, 200) || !item || !cleanText(item.episode_id, 80) ||
      !execution || !cleanText(execution.run_id, 96) ||
      !/^[0-9a-f]{64}$/.test(execution.result_digest || "")) {
      throw new FragmentIntentApiError("invalid_arguments", "续接目标或结果绑定无效");
    }
    const continuationId = `continuation:${(await digestIdentity([
      "fragment-continuation-v1", item.episode_id, execution.run_id,
      execution.result_digest, goal,
    ])).slice(0, 24)}`;
    return validateAlignment(await request("", {
      url: `http://127.0.0.1:5684/fragment/v1/episodes/${encodeURIComponent(item.episode_id)}/continuations`,
      method: "POST",
      headers: { "Content-Type": "application/json", [CONTINUATION_HEADER]: "1" },
      body: JSON.stringify({
        parent_episode_id: item.episode_id,
        parent_run_id: execution.run_id,
        source_result_digest: execution.result_digest,
        goal,
        continuation_id: continuationId,
        requester: "nigo",
      }),
    }));
  }

  async function escalate(item) {
    const execution = item && item.execution;
    if (!item || !execution || !/^[0-9a-f]{64}$/.test(execution.result_digest || "")) {
      throw new FragmentIntentApiError("invalid_arguments", "升级绑定无效");
    }
    const escalationId = `escalation:${(await digestIdentity([
      "fragment-escalation-v1", item.alignment_id, execution.result_digest,
    ])).slice(0, 24)}`;
    return validateEscalation(await request(`/${encodeURIComponent(item.alignment_id)}/escalations`, {
      method: "POST",
      headers: { "Content-Type": "application/json", [ESCALATION_HEADER]: "1" },
      body: JSON.stringify({
        alignment_id: item.alignment_id,
        revision: item.revision,
        input_digest: item.input_digest,
        source_result_digest: execution.result_digest,
        escalation_id: escalationId,
        requester: "nigo",
      }),
    }));
  }

  // Research Creation Bridge（DESIGN §5.3）：只在投影给出合法 graph_proposal
  // 时可用；请求体恰好七字段，全部取自投影并做确定性绑定复核；未知字段
  // 绝不发送。同 escalation 重复创建由服务端幂等/409 兜底。
  async function createResearchRun(item) {
    const material = await researchCreateMaterial(item);
    if (!material) {
      throw new FragmentIntentApiError("invalid_arguments", "Graph 升级提案材料无效");
    }
    return validateResearchRunOutcome(await request("", {
      url: RESEARCH_RUNS_URL,
      method: "POST",
      headers: { "Content-Type": "application/json", [RESEARCH_RUN_CREATE_HEADER]: "1" },
      body: JSON.stringify(material),
    }));
  }

  // 知识读取复用既有本地只读传输；调用不会启动研究或写入 Vault。
  const knowledgeUrl = "http://127.0.0.1:5684/fragment/v1/knowledge";
  const knowledgeId = (value) => typeof value === "string" && /^(knowledge|period)-[a-f0-9]{24}$/.test(value);
  const knowledgeNote = (value, detail = false) => {
    if (!value || !knowledgeId(value.knowledge_id) || typeof value.title !== "string"
      || !Number.isInteger(value.revision) || value.revision < 1
      || typeof value.path !== "string" || !value.path || value.path.startsWith("/")
      || value.path.includes("\\") || value.path.includes("\0") || value.path.split("/").includes("..")
      || (detail && (!value.result || typeof value.result !== "object" || Array.isArray(value.result)
        || !Array.isArray(value.evidence)))) {
      throw new FragmentIntentApiError("invalid_response", "知识记录字段无效");
    }
    if ((value.freshness !== undefined && (!researchObject(value.freshness) ||
      !["current", "stale", "unreviewed"].includes(value.freshness.status) ||
      !Array.isArray(value.freshness.reasons) || value.freshness.reasons.some(reason => typeof reason !== "string") ||
      !Number.isInteger(value.freshness.latest_revision) || value.freshness.latest_revision < 1)) ||
      (value.usable_as_current !== undefined && typeof value.usable_as_current !== "boolean") ||
      (value.conclusion_authority !== undefined && !["research_result", "canonical_research"].includes(value.conclusion_authority))) {
      throw new FragmentIntentApiError("invalid_response", "知识有效性标记无效");
    }
    if (value.canonical_research !== undefined) {
      if (!Array.isArray(value.canonical_research)) throw new FragmentIntentApiError("invalid_response", "知识源研究字段无效");
      for (const source of value.canonical_research) {
        knowledgeNote(source);
        if (!researchObject(source.result)) throw new FragmentIntentApiError("invalid_response", "知识源研究结果无效");
      }
    }
    return value;
  };
  async function knowledgeCatalog() {
    const data = await request("", { url: knowledgeUrl, serviceLabel: "知识" });
    if (!Array.isArray(data) || data.some((topic) => !topic
      || !["topic_id", "category", "subcategory", "title"].every((key) => typeof topic[key] === "string")
      || !Array.isArray(topic.notes))) {
      throw new FragmentIntentApiError("invalid_response", "知识目录字段无效");
    }
    for (const topic of data) topic.notes.forEach((note) => knowledgeNote(note));
    return data;
  }
  async function knowledgePeriods() {
    const data = await request("", { url: `${knowledgeUrl}/periods`, serviceLabel: "周/月知识总览" });
    if (!Array.isArray(data)) throw new FragmentIntentApiError("invalid_response", "周/月知识总览无效");
    for (const record of data) {
      knowledgeNote(record, true);
      if (record.kind !== "period" || record.preview === true || record.topic !== undefined || !researchObject(record.summary_scope)
        || record.summary_scope.scope_id !== "library" || typeof record.summary_scope.title !== "string") {
        throw new FragmentIntentApiError("invalid_response", "周/月知识总览范围无效");
      }
    }
    return data;
  }
  async function searchKnowledge(query) {
    if (typeof query !== "string" || !query.trim() || query.length > 300) {
      throw new FragmentIntentApiError("invalid_arguments", "搜索内容应为 1–300 个字符");
    }
    const data = await request("", { url: `${knowledgeUrl}?q=${encodeURIComponent(query.trim())}`, serviceLabel: "知识" });
    if (!Array.isArray(data)) throw new FragmentIntentApiError("invalid_response", "知识搜索结果无效");
    return data.map((note) => knowledgeNote(note, true));
  }
  async function knowledgeNotifications() {
    const data = await request("", { url: `${knowledgeUrl}/notifications`, serviceLabel: "知识更新" });
    if (!Array.isArray(data)) throw new FragmentIntentApiError("invalid_response", "知识更新通知无效");
    for (const notice of data) {
      knowledgeNote(notice);
      if (notice.notification_id !== `${notice.knowledge_id}:${notice.revision}`
        || typeof notice.message !== "string" || !cleanText(notice.updated_at, 40)) {
        throw new FragmentIntentApiError("invalid_response", "知识更新通知字段无效");
      }
    }
    return data;
  }
  async function readKnowledge(id, revision) {
    if (!knowledgeId(id)) throw new FragmentIntentApiError("invalid_arguments", "知识标识无效");
    if (revision !== undefined && (!Number.isInteger(revision) || revision < 1 || revision > 999999999)) {
      throw new FragmentIntentApiError("invalid_arguments", "知识修订号无效");
    }
    const suffix = revision === undefined ? "" : `?revision=${revision}`;
    const data = knowledgeNote(await request("", { url: `${knowledgeUrl}/${encodeURIComponent(id)}${suffix}`, serviceLabel: "知识" }), true);
    if (data.knowledge_id !== id || (revision !== undefined && data.revision !== revision)) {
      throw new FragmentIntentApiError("invalid_response", "知识详情与请求不匹配（标识或修订）");
    }
    return data;
  }

  return { list, autoProposeReport, propose, decide, continueEpisode, escalate, createResearchRun,
    knowledgeCatalog, knowledgePeriods, searchKnowledge, readKnowledge, knowledgeNotifications };
}

module.exports = {
  ALIGNMENTS_URL,
  ALIGNMENT_HEADER,
  DECISION_HEADER,
  ESCALATION_HEADER,
  CONTINUATION_HEADER,
  RESEARCH_RUNS_URL,
  RESEARCH_RUN_CREATE_HEADER,
  RESEARCH_SPEC_ID,
  RESEARCH_MACRO_V3_SPEC_ID,
  FragmentIntentApiError,
  STABLE_INTENTS,
  validateAlignment,
  effectiveExecutionScopeOf,
  fragmentInputDigest,
  researchProposalReady,
  knowledgePublicationOf,
  researchResultDigestOf,
  researchCreateMaterial,
  createFragmentIntentClient,
};
