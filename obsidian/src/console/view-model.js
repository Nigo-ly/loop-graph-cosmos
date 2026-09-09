"use strict";

const DISPLAY_STATE_LABELS = {
  queued: "待路由",
  running: "运行中",
  awaiting_human: "等待人工",
  stopped: "已停止",
  completed: "已完成",
};

// P1 localization: Chinese primary labels for raw database statuses. Unknown or
// historical values fall back to the verbatim raw value, never to a guessed
// translation. Filtering and requests always keep using the raw status.
const STATUS_LABELS = {
  approved: "已批准",
  running: "运行中",
  passed: "已通过",
  exhausted: "预算耗尽",
  blocked: "已阻塞",
  escalated: "已转人工",
  cancelled: "已取消",
  superseded: "已被取代",
  failed_safe: "安全停止",
  requested: "已请求",
  preflight_passed: "预检通过",
  pause_requested: "请求暂停中",
  paused: "已暂停",
};

// Event type vocabulary frozen in contracts/p1-projection-api.md.
const EVENT_TYPE_LABELS = {
  snapshot: "快照",
  run_registered: "运行登记",
  admission_transition: "准入变更",
  state_transition: "状态变更",
  node_completed: "节点完成",
  action_reserved: "动作预留",
  action_completed: "动作完成",
  action_reconciled: "动作对账",
  loop_stopped: "Loop 停止",
  loop_blocked: "Loop 阻塞",
  parent_reconciled: "父级对账",
};

// Common Loop node names observed in fixtures, real snapshots, and the real
// Loop database. Unknown names fall back verbatim.
const NODE_LABELS = {
  intake: "接收入口",
  source_lock: "来源锁定",
  claim_matrix: "主张矩阵",
  stop_policy_design: "停止策略设计",
  independent_eval: "独立核验",
  feedback: "实践反馈",
  worker: "执行节点",
  k27_worker: "K2.7 执行节点",
  content_acquisition: "内容获取",
  source_verification: "来源核验",
  value_routing: "价值路由",
  understanding: "理解分析",
  action_design: "行动设计",
  publication_eval: "发布评估",
  relevance_mapping: "相关性映射",
  skill_inventory: "技能清单",
  capability_model: "能力模型",
  capability_mapping: "能力映射",
};

const VERDICT_LABELS = {
  pass: "通过",
  fail: "未通过",
  revise_then_pass: "修订后通过",
};

// P2B control plane: Chinese-primary labels for the five frozen actions and
// the intent lifecycle. Raw values stay available in the audit disclosure.
const CONTROL_ACTION_LABELS = {
  pause: "暂停",
  resume: "继续",
  terminate: "终止",
  retry: "重试",
  priority: "调整优先级",
};

// 极简前端: primary row keeps only 暂停/继续/终止; retry and priority are
// secondary operations behind a collapsed disclosure.
const PRIMARY_ACTIONS = ["pause", "resume", "terminate"];
const SECONDARY_ACTIONS = ["retry", "priority"];

const CONTROL_ACTION_EFFECTS = {
  pause: "在安全节点边界暂停该运行",
  resume: "恢复该暂停的运行",
  terminate: "终止该运行并标记为已取消（历史和产物保留，不可撤销）",
  retry: "为该运行创建一个新的重试 attempt（旧终态保留）",
  priority: "调整该运行的调度优先级（-10 到 10，越大越先调度）",
};

// Runs that need nigo's attention first: awaiting a human decision or
// stopped abnormally. Normal runs only get a concise summary.
const ATTENTION_STATUSES = {
  awaiting_human: "等待人工",
  blocked: "异常停止：已阻塞",
  exhausted: "异常停止：预算耗尽",
  failed_safe: "异常停止：安全停止",
};

// Cards never guess concrete actions: the server /actions/{run_id} matrix
// is the only source of availability, shown in the detail view.
const ATTENTION_GENERIC_HINT = "需要处理：打开查看可用操作";

const INTENT_STATUS_LABELS = {
  pending: "待处理",
  accepted: "已接管",
  applied: "已执行",
  rejected: "已拒绝",
  failed: "执行失败",
};

// Stable reason codes from the P2A backend, Chinese explanation. Unknown
// codes fall back to the raw code, never to a guessed translation.
const REASON_TEXT = {
  invalid_state: "当前状态不允许该操作",
  stale_sequence: "数据已变化（序号过期），请刷新后重试",
  loopspec_unavailable: "LoopSpec 未注册，操作不可用",
  loopspec_version_mismatch: "LoopSpec 版本不一致，操作不可用",
  handler_unavailable: "节点处理器不可用，操作不可用",
  run_not_found: "运行不存在",
  conflict: "并发冲突，操作未执行",
  internal_error: "控制服务内部错误",
};

function displayStateLabel(state) {
  return DISPLAY_STATE_LABELS[state] || state || "未知";
}

function statusLabel(status) {
  if (status === null || typeof status === "undefined" || status === "") return "—";
  return STATUS_LABELS[status] || String(status);
}

function eventTypeLabel(eventType) {
  if (eventType === null || typeof eventType === "undefined" || eventType === "") return "—";
  return EVENT_TYPE_LABELS[eventType] || String(eventType);
}

function nodeLabel(node) {
  if (node === null || typeof node === "undefined" || node === "") return "—";
  return NODE_LABELS[node] || String(node);
}

function verdictLabel(verdict) {
  if (verdict === null || typeof verdict === "undefined" || verdict === "") return "—";
  return VERDICT_LABELS[verdict] || String(verdict);
}

function controlActionLabel(action) {
  return CONTROL_ACTION_LABELS[action] || String(action || "—");
}

function intentStatusLabel(status) {
  return INTENT_STATUS_LABELS[status] || String(status || "未知");
}

function reasonText(code) {
  if (!code) return null;
  return REASON_TEXT[code] || String(code);
}

// GET /actions/{run_id} -> view model. The UI never invents availability:
// it renders exactly the server matrix plus reasons.
function controlActionsView(body) {
  const data = body && body.data ? body.data : null;
  if (!data) return null;
  const rawActions = data.actions && typeof data.actions === "object" ? data.actions : {};
  const actions = Object.keys(rawActions).map((action) => {
    const entry = rawActions[action] || {};
    return {
      action,
      label: controlActionLabel(action),
      effect: CONTROL_ACTION_EFFECTS[action] || "",
      enabled: entry.enabled === true,
      reasonCode: entry.reason_code || null,
      reasonText: reasonText(entry.reason_code),
    };
  });
  return {
    runId: text(data.run_id),
    status: text(data.status),
    statusLabel: statusLabel(data.status),
    latestSequence: typeof data.latest_sequence === "number" ? data.latest_sequence : null,
    priority: typeof data.priority === "number" ? data.priority : 0,
    actions,
  };
}

function receiptView(receipt) {
  if (!receipt || typeof receipt !== "object") return null;
  return {
    intentId: text(receipt.intent_id),
    idempotencyKey: text(receipt.idempotency_key),
    runId: text(receipt.run_id),
    action: text(receipt.action),
    actionLabel: controlActionLabel(receipt.action),
    arguments: receipt.arguments && typeof receipt.arguments === "object" ? receipt.arguments : {},
    requester: text(receipt.requester),
    expectedSequence:
      typeof receipt.expected_sequence === "number" ? receipt.expected_sequence : null,
    status: text(receipt.status),
    statusLabel: intentStatusLabel(receipt.status),
    reasonCode: receipt.reason_code || null,
    reasonText: reasonText(receipt.reason_code),
    resultRunId: receipt.result_run_id || null,
    appliedSequence:
      typeof receipt.applied_sequence === "number" ? receipt.applied_sequence : null,
    createdAt: formatDateTime(receipt.created_at),
    updatedAt: formatDateTime(receipt.updated_at),
  };
}

function intentHistoryView(body) {
  const items = body && body.data && Array.isArray(body.data.items) ? body.data.items : [];
  return items.map(receiptView).filter(Boolean);
}

// 需要关注: runs awaiting a human decision or stopped abnormally, shown
// first on the default page. Normal runs stay concise summaries below.
function isAttentionRun(displayState, status) {
  return (
    Object.prototype.hasOwnProperty.call(ATTENTION_STATUSES, displayState) ||
    Object.prototype.hasOwnProperty.call(ATTENTION_STATUSES, status)
  );
}

function attentionRows(envelope) {
  return runRows(envelope, { status: null })
    .filter((row) => isAttentionRun(row.displayState, row.status))
    // Authoritative currentness from the backend: resolved or superseded
    // attempts never enter the primary surface; they stay in collapsed
    // history (全部运行).
    .filter((row) => row.isCurrent)
    .map((row) => {
      const attentionKey = Object.prototype.hasOwnProperty.call(ATTENTION_STATUSES, row.displayState)
        ? row.displayState
        : row.status;
      return {
        runId: row.runId,
        // Card title: per-fragment subject, then the honest labelled
        // fallback — never the shared generic goal, never a bare UUID.
        title: row.subject || fallbackTitle(row.fragmentId, row.updatedAt),
        loopId: row.loopId,
        fragmentId: row.fragmentId,
        subjectSource: row.subjectSource,
        status: row.status,
        statusLabel: row.statusLabel,
        displayStateLabel: row.displayStateLabel,
        currentNodeLabel: row.currentNodeLabel,
        stopReason: row.stopReason,
        attentionText: ATTENTION_STATUSES[attentionKey],
        decisionHint: ATTENTION_GENERIC_HINT,
        updatedAt: row.updatedAt,
      };
    });
}

// The collapsed all-runs surface never repeats attention records.
function normalRunRows(envelope, filter) {
  return runRows(envelope, filter).filter(
    (row) => !isAttentionRun(row.displayState, row.status)
  );
}

function text(value, fallback) {
  if (value === null || typeof value === "undefined" || value === "") return fallback || "—";
  return String(value);
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatDataAge(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 120) return "数据刚刚更新";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `数据已静默 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `数据已静默 ${hours} 小时`;
  return `数据已静默 ${Math.floor(hours / 24)} 天`;
}

function healthSummary(envelope, nowMs) {
  if (!envelope) return null;
  const dataAge = Number.isFinite(envelope.data_age_seconds)
    ? envelope.data_age_seconds
    : (() => {
        const committed = Date.parse(envelope.source_committed_at || "");
        const generated = Date.parse(envelope.generated_at || "");
        if (Number.isNaN(committed) || Number.isNaN(generated)) return null;
        const base = Number.isFinite(nowMs) ? nowMs : generated;
        return Math.max(0, Math.round((base - committed) / 1000));
      })();
  return {
    healthy: envelope.provider_healthy !== false && envelope.stale !== true,
    stale: envelope.stale === true,
    providerHealthy: envelope.provider_healthy !== false,
    dataAgeSeconds: dataAge,
    dataAgeText: formatDataAge(dataAge),
    sourceSequence: typeof envelope.source_sequence === "number" ? envelope.source_sequence : null,
    sourceCommittedAt: envelope.source_committed_at || null,
    generatedAt: envelope.generated_at || null,
    providerVersion: envelope.provider_version || null,
  };
}

// Honest distinguishable fallback when no semantic subject exists yet
// (link-only fragments awaiting verified acquisition). It is explicitly
// labelled 待获取标题 — never presented as a semantic subject, and never
// the shared generic goal. Second precision plus a stable short suffix
// keep same-minute captures distinct.
function fallbackTitle(fragmentId, updatedAt) {
  const stamp = /^(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})/.exec(fragmentId || "");
  if (stamp) {
    return `待获取标题 · ${stamp[2]}-${stamp[3]} ${stamp[4]}:${stamp[5]}:${stamp[6]}`;
  }
  const formatted = formatDateTime(updatedAt);
  const suffix = fragmentId ? ` · ${String(fragmentId).slice(-4)}` : "";
  if (formatted && formatted !== "—") return `待获取标题 · ${formatted}${suffix}`;
  return `待获取标题${suffix}`;
}

function queueRows(envelope) {
  const items = envelope && Array.isArray(envelope.items) ? envelope.items : [];
  return items.map((item) => ({
    runId: text(item.run_id),
    fragmentId: text(item.fragment_id),
    loopId: text(item.loop_id),
    goal: item.goal ? String(item.goal) : null,
    subject: item.subject ? String(item.subject) : null,
    subjectSource: item.subject_source ? String(item.subject_source) : null,
    // Semantic-first title: per-fragment subject, then an honest labelled
    // fallback — never the shared generic goal, never a bare UUID.
    title: item.subject ? String(item.subject) : fallbackTitle(item.fragment_id, item.updated_at),
    status: text(item.status),
    statusLabel: statusLabel(item.status),
    displayState: item.display_state || null,
    displayStateLabel: displayStateLabel(item.display_state),
    sourceFile: text(item.source_file),
    nextStep: text(item.next_step),
    updatedAt: formatDateTime(item.updated_at),
  }));
}

function runRows(envelope, filter) {
  const items = envelope && Array.isArray(envelope.items) ? envelope.items : [];
  const statusFilter = filter && filter.status ? String(filter.status) : null;
  const stateFilter = filter && filter.displayState ? String(filter.displayState) : null;
  return items
    .filter((item) => (statusFilter ? item.status === statusFilter : true))
    .filter((item) => (stateFilter ? item.display_state === stateFilter : true))
    .map((item) => ({
      runId: text(item.run_id),
      parentRunId: text(item.parent_run_id),
      loopId: text(item.loop_id),
      fragmentId: text(item.fragment_id),
      goal: item.goal ? String(item.goal) : null,
      subject: item.subject ? String(item.subject) : null,
      attemptGroupId: item.attempt_group_id ? String(item.attempt_group_id) : null,
      // Older providers lack the field; treat as current (backend is the
      // authority once the additive field exists).
      isCurrent: item.is_current !== false,
      supersededByRunId: item.superseded_by_run_id ? String(item.superseded_by_run_id) : null,
      subjectSource: item.subject_source ? String(item.subject_source) : null,
      status: text(item.status),
      statusLabel: statusLabel(item.status),
      displayState: item.display_state || null,
      displayStateLabel: displayStateLabel(item.display_state),
      currentNode: text(item.current_node),
      currentNodeLabel: nodeLabel(item.current_node),
      iterations: typeof item.iterations === "number" ? item.iterations : null,
      events: typeof item.events === "number" ? item.events : null,
      evaluatorVersion: text(item.evaluator_version),
      budgetUsed: item.budget_used || null,
      stopReason: item.stop_reason || null,
      updatedAt: formatDateTime(item.updated_at),
    }));
}

function rawStatusList(envelope) {
  const items = envelope && Array.isArray(envelope.items) ? envelope.items : [];
  const seen = [];
  for (const item of items) {
    const status = text(item.status, "");
    if (status && !seen.includes(status)) seen.push(status);
  }
  return seen;
}

// Filter dropdown options: Chinese primary label, raw status preserved as the
// submitted value and shown in parentheses for audit.
function statusFilterOptions(envelope) {
  return rawStatusList(envelope).map((status) => ({ status, label: statusLabel(status) }));
}

function formatBudgetValue(used, limit, unit) {
  const usedText = typeof used === "number" ? String(used) : "—";
  const limitText = typeof limit === "number" ? String(limit) : "—";
  return `${usedText} / ${limitText}${unit ? ` ${unit}` : ""}`;
}

function budgetView(run) {
  const used = run.budget_used || {};
  const limits = run.budget_limits || {};
  const rows = [
    { key: "active_tokens", label: "活跃令牌（Token）", used: used.active_tokens, limit: limits.active_tokens, unit: "令牌（Token）" },
    { key: "calls", label: "调用次数", used: used.calls, limit: limits.calls, unit: "次" },
    { key: "elapsed_seconds", label: "已用时间", used: used.elapsed_seconds, limit: limits.seconds, unit: "秒" },
  ];
  return rows.map((row) => ({
    label: row.label,
    text: formatBudgetValue(row.used, row.limit, row.unit),
    ratio: typeof row.used === "number" && typeof row.limit === "number" && row.limit > 0 ? row.used / row.limit : null,
  }));
}

function evalView(run) {
  const evalResults = run.eval_results || null;
  if (!evalResults) return { verdict: "—", verdictLabel: "—", gates: "—", detail: null };
  const gates = evalResults.hard_gates || {};
  const passed = typeof gates.passed === "number" ? gates.passed : null;
  const total = typeof gates.total === "number" ? gates.total : null;
  return {
    verdict: text(evalResults.verdict),
    verdictLabel: verdictLabel(evalResults.verdict),
    gates: passed !== null && total !== null ? `${passed}/${total}` : "—",
    detail: evalResults,
  };
}

function timelineView(run) {
  const events = Array.isArray(run.timeline) ? run.timeline.slice() : [];
  events.sort((a, b) => {
    const sa = typeof a.sequence === "number" ? a.sequence : 0;
    const sb = typeof b.sequence === "number" ? b.sequence : 0;
    return sa - sb;
  });
  return events.map((event) => ({
    sequence: typeof event.sequence === "number" ? event.sequence : null,
    eventType: text(event.event_type),
    eventTypeLabel: eventTypeLabel(event.event_type),
    node: text(event.node),
    nodeLabel: event.node ? nodeLabel(event.node) : "—",
    createdAt: formatDateTime(event.created_at),
    summary: text(event.summary),
  }));
}

function minimumValueDecisionView(run) {
  if (
    !run ||
    run.loop_id !== "phone-fragment-minimum-value-v1" ||
    run.status !== "paused" ||
    run.stop_reason !== "awaiting_send_consent"
  ) {
    return null;
  }
  const evalResults =
    run.eval_results && typeof run.eval_results === "object" ? run.eval_results : {};
  const projection =
    evalResults.minimum_value && typeof evalResults.minimum_value === "object"
      ? evalResults.minimum_value
      : {};
  const outbound =
    evalResults.outbound && typeof evalResults.outbound === "object"
      ? evalResults.outbound
      : {};
  const timeline = Array.isArray(run.timeline) ? run.timeline : [];
  const expectedSequence = timeline.reduce(
    (latest, event) =>
      typeof event.sequence === "number" && event.sequence > latest
        ? event.sequence
        : latest,
    0
  );
  const blocked = projection.privacy_blocked === true;
  const sha =
    typeof projection.payload_sha256 === "string" &&
    /^[0-9a-f]{64}$/.test(projection.payload_sha256)
      ? projection.payload_sha256
      : "";
  const targetProvider =
    typeof projection.target_provider === "string" ? projection.target_provider : "";
  const targetModel =
    typeof projection.target_model === "string" ? projection.target_model : "";
  const targetProfile =
    typeof projection.target_profile === "string" ? projection.target_profile : "";
  const payload =
    !blocked && typeof outbound.payload_text === "string" ? outbound.payload_text : null;
  return {
    runId: text(run.run_id),
    fragmentId: text(run.fragment_id),
    outboundPayload: payload,
    outboundPayloadSha256: sha,
    targetProvider,
    targetModel,
    targetProfile,
    expectedSequence,
    privacyBlocked: blocked,
    privacyFieldCategories: Array.isArray(projection.privacy_field_categories)
      ? projection.privacy_field_categories.map((value) => text(value))
      : [],
    ready:
      !blocked &&
      payload !== null &&
      sha !== "" &&
      targetProvider !== "" &&
      targetModel !== "" &&
      targetProfile !== "" &&
      expectedSequence > 0,
  };
}

// R1-OC: exact identity only. Two frozen loops, exact loopspec_version
// equality; lookalike ids, prefixes, or missing/wrong versions fail closed.
const COGNITIVE_LOOP_IDS = new Set([
  "fragment-cognitive-synthetic-r1b",
  "fragment-cognitive-local-r1n",
]);
const COGNITIVE_LOOPSPEC_VERSION = "1.0.0";
// Receipt keys frozen verbatim by the backend (cognitive_note.py): a pending
// withdrawal lives under cognitive_withdrawal_pending_receipt, the terminal
// under cognitive_withdrawal_receipt (which coexists with the pending one).
// Neither receipt carries a status field — withdrawal_pending /
// withdrawal_completed is derived from the key, never inferred from data.
const WITHDRAWAL_PENDING_RECEIPT_KEY = "cognitive_withdrawal_pending_receipt";
const WITHDRAWAL_TERMINAL_RECEIPT_KEY = "cognitive_withdrawal_receipt";
const WITHDRAWAL_ID_PATTERN = /^[0-9a-f]{64}$/;

// Package B: the backend recomputes one sanitized, allowlisted draft view
// (fragment_loop/cognitive_draft_view.py) and attaches it to the run detail
// as cognitive_draft_view. The console projects it only when the shape is
// exact; a missing key means a legacy backend (markdown-only card), while a
// present-but-invalid view fails the whole draft closed.
const DRAFT_VIEW_VERSION = "fragment-cognitive-draft-view-v1";

function nonEmptyString(value) {
  return typeof value === "string" && value.trim() !== "";
}

function stringArray(value) {
  return Array.isArray(value) && value.every((entry) => typeof entry === "string");
}

function plainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function pickStrings(source, keys) {
  const out = {};
  for (const key of keys) {
    if (!nonEmptyString(source[key])) return null;
    out[key] = source[key];
  }
  return out;
}

function draftSourceView(source) {
  if (!plainObject(source)) return null;
  const card = pickStrings(source, [
    "source_id",
    "name",
    "locator",
    "authenticity",
    "relevance",
    "independence",
    "source_tier",
    "source_type",
    "published_at",
    "acquisition_method",
    "verification_basis",
    "scope",
    "evidence_excerpt",
  ]);
  if (!card) return null;
  const discovery = source.discovery;
  if (discovery === null) {
    card.discovery = null;
    return card;
  }
  if (!plainObject(discovery)) return null;
  const snippet = pickStrings(discovery, [
    "title",
    "excerpt",
    "published_at_claim",
    "source_type_hint",
  ]);
  if (!snippet || !stringArray(discovery.query_refs)) return null;
  snippet.query_refs = discovery.query_refs.slice();
  card.discovery = snippet;
  return card;
}

function draftClaimView(claim) {
  if (!plainObject(claim)) return null;
  const view = pickStrings(claim, [
    "claim_id",
    "claim_kind",
    "text",
    "verdict",
    "confidence",
    "claim_derivation",
    "verdict_basis",
  ]);
  if (!view || !stringArray(claim.evidence_refs)) return null;
  view.evidence_refs = claim.evidence_refs.slice();
  const verification = pickStrings(claim.independent_verification || {}, [
    "status",
    "mode",
    "basis",
  ]);
  if (!verification) return null;
  view.independent_verification = verification;
  return view;
}

function draftResearchView(research) {
  if (!plainObject(research)) return null;
  if (!stringArray(research.questions) || !research.questions.length) return null;
  if (!stringArray(research.search_dimensions)) return null;
  if (!Array.isArray(research.sources)) return null;
  const sources = [];
  for (const source of research.sources) {
    const card = draftSourceView(source);
    if (!card) return null;
    sources.push(card);
  }
  const counter = research.counter_evidence_search;
  if (!plainObject(counter) || !nonEmptyString(counter.status) || !nonEmptyString(counter.scope)) {
    return null;
  }
  if (!Array.isArray(counter.findings)) return null;
  const findings = [];
  for (const finding of counter.findings) {
    if (!plainObject(finding) || !nonEmptyString(finding.text) || !stringArray(finding.evidence_refs)) {
      return null;
    }
    findings.push({ text: finding.text, evidence_refs: finding.evidence_refs.slice() });
  }
  if (!Array.isArray(research.v1_claims) || !research.v1_claims.length) return null;
  const claims = [];
  for (const claim of research.v1_claims) {
    const view = draftClaimView(claim);
    if (!view) return null;
    claims.push(view);
  }
  if (!Array.isArray(research.v2_revisions)) return null;
  const revisions = [];
  for (const revision of research.v2_revisions) {
    const view = pickStrings(revision, [
      "claim_id",
      "revision_status",
      "deviation",
      "revision_reason",
      "revised_text",
    ]);
    if (!view) return null;
    revisions.push(view);
  }
  return {
    questions: research.questions.slice(),
    search_dimensions: research.search_dimensions.slice(),
    sources,
    counter_evidence_search: {
      status: counter.status,
      scope: counter.scope,
      findings,
    },
    v1_claims: claims,
    v2_revisions: revisions,
  };
}

// Package C: strict per-type material allowlists. The console never trusts
// the backend object: any unknown key, missing required key, illegal
// combination, or unknown material type fails the whole draft closed.
const DRAFT_MATERIAL_KEYS = {
  confirmed_user_fact: ["material_type", "text", "confirmation_ref"],
  obsidian_record: ["material_type", "text", "record_ref"],
  profile_inference: ["material_type", "text", "basis", "uncertainty"],
};

function draftMaterialView(material) {
  if (!plainObject(material)) return null;
  const allowed = DRAFT_MATERIAL_KEYS[material.material_type];
  if (!allowed) return null;
  if (!Object.keys(material).every((key) => allowed.includes(key))) return null;
  const view = pickStrings(material, allowed);
  if (!view) return null;
  const out = { materialType: view.material_type, text: view.text };
  if (view.confirmation_ref !== undefined) out.confirmationRef = view.confirmation_ref;
  if (view.record_ref !== undefined) out.recordRef = view.record_ref;
  if (view.basis !== undefined) out.basis = view.basis;
  if (view.uncertainty !== undefined) out.uncertainty = view.uncertainty;
  return out;
}

function cognitiveDraftStructuredView(rawView, route) {
  if (!plainObject(rawView)) return null;
  if (rawView.view_version !== DRAFT_VIEW_VERSION) return null;
  if (rawView.route !== route) return null;
  if (!nonEmptyString(rawView.fragment_text)) return null;
  if (rawView.evidence_level !== "unverified") return null;
  if (!nonEmptyString(rawView.route_reason)) return null;
  const summary = pickStrings(rawView.summary || {}, [
    "text",
    "source_quote",
    "transformation_basis",
  ]);
  if (!summary) return null;
  const expansion = rawView.semantic_expansion;
  if (!plainObject(expansion)) return null;
  if (!Array.isArray(expansion.literal_facts) || !expansion.literal_facts.length) return null;
  const literalFacts = [];
  for (const fact of expansion.literal_facts) {
    const view = pickStrings(fact, ["text", "source_quote"]);
    if (!view) return null;
    literalFacts.push(view);
  }
  if (!Array.isArray(expansion.inferences)) return null;
  const inferences = [];
  for (const inference of expansion.inferences) {
    const view = pickStrings(inference, ["text", "source_quote", "transformation_basis"]);
    if (!view) return null;
    inferences.push(view);
  }
  if (!stringArray(expansion.uncertainties)) return null;
  let research = null;
  if (route === "research") {
    research = draftResearchView(rawView.research);
    if (!research) return null;
  } else if (rawView.research !== null) {
    return null;
  }
  if (!Array.isArray(rawView.perspectives) || !rawView.perspectives.length) return null;
  const perspectives = [];
  for (const perspective of rawView.perspectives) {
    if (!plainObject(perspective)) return null;
    const view = pickStrings(perspective, [
      "perspective",
      "summary",
      "association",
      "value",
      "uncertainty",
    ]);
    if (!view || !stringArray(perspective.conflicts)) return null;
    if (!Array.isArray(perspective.materials)) return null;
    view.conflicts = perspective.conflicts.slice();
    view.materials = [];
    for (const material of perspective.materials) {
      const materialView = draftMaterialView(material);
      if (!materialView) return null;
      view.materials.push(materialView);
    }
    perspectives.push(view);
  }
  const synthesisRaw = rawView.synthesis;
  if (!plainObject(synthesisRaw)) return null;
  const synthesis = pickStrings(synthesisRaw, ["value", "weakest_link", "next_step"]);
  if (!synthesis) return null;
  if (!stringArray(synthesisRaw.conflicts) || !stringArray(synthesisRaw.open_questions)) {
    return null;
  }
  synthesis.conflicts = synthesisRaw.conflicts.slice();
  synthesis.open_questions = synthesisRaw.open_questions.slice();
  if (route === "research") {
    if (!nonEmptyString(synthesisRaw.credibility) || !nonEmptyString(synthesisRaw.credibility_basis)) {
      return null;
    }
    synthesis.credibility = synthesisRaw.credibility;
    synthesis.credibility_basis = synthesisRaw.credibility_basis;
    const counts = synthesisRaw.claim_counts;
    if (
      !plainObject(counts) ||
      !["supported", "partially_supported", "contradicted", "not_covered"].every(
        (key) => Number.isInteger(counts[key])
      )
    ) {
      return null;
    }
    synthesis.claim_counts = {
      supported: counts.supported,
      partially_supported: counts.partially_supported,
      contradicted: counts.contradicted,
      not_covered: counts.not_covered,
    };
  }
  return {
    route,
    routeReason: rawView.route_reason,
    fragmentText: rawView.fragment_text,
    evidenceLevel: "unverified",
    summary,
    semanticExpansion: {
      literal_facts: literalFacts,
      inferences,
      uncertainties: expansion.uncertainties.slice(),
    },
    research,
    perspectives,
    synthesis,
  };
}

// Strict receipt projection: any missing field, wrong type, or causal
// mismatch fails closed (returns null) and the continue action is never
// shown. note_path/note_body/category are never projected.
function cognitiveWithdrawalReceiptView(receipt, fragmentId, status) {
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) return null;
  if (receipt.action !== "withdraw") return null;
  if (
    typeof receipt.withdrawal_id !== "string" ||
    !WITHDRAWAL_ID_PATTERN.test(receipt.withdrawal_id)
  ) {
    return null;
  }
  if (receipt.fragment_id !== fragmentId) return null;
  if (!Number.isInteger(receipt.expected_sequence) || receipt.expected_sequence < 1) {
    return null;
  }
  return {
    status,
    withdrawalId: receipt.withdrawal_id,
    expectedSequence: receipt.expected_sequence,
  };
}

function cognitiveDraftView(run) {
  if (!run || !COGNITIVE_LOOP_IDS.has(run.loop_id)) return null;
  if (run.loopspec_version !== COGNITIVE_LOOPSPEC_VERSION) return null;
  const values =
    run.eval_results && typeof run.eval_results === "object" ? run.eval_results : {};
  const route = values.cognitive_route;
  const markdown = values.cognitive_markdown;
  const decision = values.cognitive_decision;
  if (
    values.cognitive_contract_status !== "validated" ||
    (route !== "research" && route !== "direct") ||
    typeof markdown !== "string" ||
    markdown.trim() === "" ||
    (decision !== "pending" && decision !== "keep_draft") ||
    values.evidence_level !== "unverified"
  ) {
    return null;
  }
  const lifecycle = values.content_lifecycle;
  const hasTerminalReceipt =
    values[WITHDRAWAL_TERMINAL_RECEIPT_KEY] !== null &&
    typeof values[WITHDRAWAL_TERMINAL_RECEIPT_KEY] !== "undefined";
  const hasPendingReceipt =
    values[WITHDRAWAL_PENDING_RECEIPT_KEY] !== null &&
    typeof values[WITHDRAWAL_PENDING_RECEIPT_KEY] !== "undefined";
  // The terminal key wins: in the real backend chain the terminal checkpoint
  // still carries the pending receipt alongside the completed one.
  const receipt = hasTerminalReceipt
    ? cognitiveWithdrawalReceiptView(
        values[WITHDRAWAL_TERMINAL_RECEIPT_KEY],
        run.fragment_id,
        "withdrawal_completed"
      )
    : hasPendingReceipt
      ? cognitiveWithdrawalReceiptView(
          values[WITHDRAWAL_PENDING_RECEIPT_KEY],
          run.fragment_id,
          "withdrawal_pending"
        )
      : null;
  // A receipt field that fails strict validation is never projected at all:
  // the console must not guess the withdrawal state from malformed data.
  if ((hasTerminalReceipt || hasPendingReceipt) && !receipt) return null;
  const hasStructuredView = Object.prototype.hasOwnProperty.call(
    run,
    "cognitive_draft_view"
  );
  const structuredView = hasStructuredView
    ? cognitiveDraftStructuredView(run.cognitive_draft_view, route)
    : null;
  // A present-but-invalid view fails the whole draft closed; an absent key
  // only means a legacy backend projection and keeps the markdown-only card.
  if (hasStructuredView && !structuredView) return null;
  const base = {
    route,
    markdown,
    decision,
    evidenceLevel: "unverified",
    runId: text(run.run_id),
    fragmentId: text(run.fragment_id),
    view: structuredView,
  };
  // Terminal state: only a valid completed receipt together with the
  // withdrawn lifecycle (still unverified) may show 已撤回.
  if (receipt && receipt.status === "withdrawal_completed") {
    if (lifecycle !== "withdrawn") return null;
    return {
      ...base,
      lifecycle: "withdrawn",
      awaitingDecision: false,
      kept: false,
      withdrawalPending: false,
      withdrawn: true,
      withdrawalId: receipt.withdrawalId,
      expectedSequence: receipt.expectedSequence,
    };
  }
  if (lifecycle !== "draft") return null;
  // Pending withdrawal: continue binds the receipt's original
  // expected_sequence, never the latest timeline sequence.
  if (receipt) {
    return {
      ...base,
      lifecycle: "draft",
      awaitingDecision: false,
      kept: false,
      withdrawalPending: true,
      withdrawn: false,
      withdrawalId: receipt.withdrawalId,
      expectedSequence: receipt.expectedSequence,
    };
  }
  const timeline = Array.isArray(run.timeline) ? run.timeline : [];
  const expectedSequence = timeline.reduce(
    (latest, event) =>
      typeof event.sequence === "number" && event.sequence > latest
        ? event.sequence
        : latest,
    0
  );
  if (expectedSequence < 1) return null;
  return {
    ...base,
    lifecycle: "draft",
    awaitingDecision:
      decision === "pending" &&
      run.status === "paused" &&
      run.stop_reason === "awaiting_cognitive_decision",
    kept: decision === "keep_draft",
    withdrawalPending: false,
    withdrawn: false,
    withdrawalId: null,
    expectedSequence,
  };
}

function detailModel(envelope) {
  const run = envelope && envelope.run ? envelope.run : null;
  if (!run) return null;
  const stopReason = run.stop_reason || null;
  const evalInfo = evalView(run);
  const budget = budgetView(run);
  return {
    runId: text(run.run_id),
    parentRunId: text(run.parent_run_id),
    loopId: text(run.loop_id),
    loopspecVersion: text(run.loopspec_version),
    fragmentId: text(run.fragment_id),
    goal: text(run.goal),
    status: text(run.status),
    statusLabel: statusLabel(run.status),
    displayState: run.display_state || null,
    displayStateLabel: displayStateLabel(run.display_state),
    currentNode: text(run.current_node),
    currentNodeLabel: nodeLabel(run.current_node),
    iterations: typeof run.iterations === "number" ? run.iterations : null,
    workerVersion: text(run.worker_version),
    evaluatorVersion: text(run.evaluator_version),
    stopReason,
    stopReasonText: stopReason || (run.display_state === "running" || run.status === "running" ? "运行中，尚未停止" : "—"),
    resumeCondition: text(run.resume_condition, "无"),
    budget,
    budgetRisk: budget
      .filter((row) => row.ratio !== null && row.ratio >= 0.9)
      .map((row) => `${row.label}已用 ${Math.round(row.ratio * 100)}%，接近上限`),
    eval: evalInfo,
    minimumValue: minimumValueDecisionView(run),
    cognitiveDraft: cognitiveDraftView(run),
    // Raw values kept verbatim for the compact 技术详情 audit disclosure.
    technical: {
      status: text(run.status),
      displayState: run.display_state ? String(run.display_state) : "—",
      currentNode: text(run.current_node),
      verdict: evalInfo.verdict,
      stopReason: stopReason ? String(stopReason) : null,
    },
    unresolvedIssues: Array.isArray(run.unresolved_issues) ? run.unresolved_issues.map((issue) => text(issue)) : [],
    timeline: timelineView(run),
    evidenceRefs: Array.isArray(run.evidence_refs) ? run.evidence_refs.slice() : [],
    artifactRefs: Array.isArray(run.artifact_refs) ? run.artifact_refs.slice() : [],
    assetRefs: Array.isArray(run.asset_refs) ? run.asset_refs.slice() : [],
    updatedAt: formatDateTime(run.updated_at),
  };
}

function consoleStatus(state) {
  if (state.loading) return "loading";
  if (state.error && state.error.kind === "contract_mismatch") return "contract_mismatch";
  if (state.error && state.error.kind === "unreachable") return "unreachable";
  if (state.error) return "error";
  return "ready";
}

// ---------------------------------------------------------------------------
// P3B shadow proposal visibility (read-only). Chinese primary labels are
// table-driven; unknown values render 未知状态 with the raw value preserved
// for audit — never a guessed translation.
// ---------------------------------------------------------------------------

// Frozen in contracts/p3b-shadow-projection.md and TASK-P3B.md.
const PROPOSAL_STATUS_LABELS = {
  proposed: "已生成建议",
  needs_human_routing: "需要人工决定路由",
  blocked: "当前被阻塞",
  stale: "建议已经过期",
  superseded: "已被新建议取代",
};

const READINESS_LABELS = {
  ready: "具备执行条件",
  not_ready: "暂不具备执行条件",
};

const RISK_LABELS = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
};

const SIDE_EFFECT_LABELS = {
  none: "无外部副作用",
  read_only: "只读外部访问",
  write: "有外部写入副作用",
  unknown: "外部副作用未知",
};

// Last-transition reasons from the shadow ledger's append-only events.
const PROPOSAL_EVENT_REASON_LABELS = {
  scanned: "首次扫描生成",
  sequence_advanced: "来源序号已前进",
  run_left_queue: "运行已离开当前队列",
  registry_or_policy_changed: "注册表或策略已变化",
};

// Known blocked-reason codes (first segment before ":"). Unknown codes fall
// back to the raw value, never to a guessed translation.
const BLOCKED_REASON_LABELS = {
  loopspec_not_registered: "未注册 LoopSpec",
  route_policy_missing: "缺少路由策略",
  worker_unavailable: "执行模型不可用",
  evaluator_unavailable: "独立评估器不可用",
  evaluator_not_independent: "评估器必须与执行模型不同",
  budget_exceeded: "预算已超限",
  side_effect_unknown: "外部副作用未知",
  loopspec_version_mismatch: "LoopSpec 版本不一致",
  verifier_mismatch: "核验器与 LoopSpec 不一致",
  handlers_missing: "节点处理器缺失",
  phase_not_allowed: "该 Agent 未获 P3 阶段授权",
  capabilities_missing: "能力不满足",
  health_unknown: "健康状态未核验",
  health_unavailable: "健康状态不可用",
  agent_not_registered: "Agent 未注册",
  agent_disabled: "Agent 已禁用",
};

// Stop-condition kinds emitted by the P3A dispatcher.
const STOP_CONDITION_LABELS = {
  budget_hard_gate: "预算硬门",
  verifier_disagreement: "核验分歧停止",
  safety: "安全停止",
  side_effect_unknown: "副作用未知停止",
  no_gain: "连续无增益停止",
};

const HISTORY_PROPOSAL_STATUSES = ["stale", "superseded"];

function proposalStatusLabel(status) {
  if (status === null || typeof status === "undefined" || status === "") return "—";
  return PROPOSAL_STATUS_LABELS[status] || "未知状态";
}

function readinessLabel(executionReady) {
  return executionReady === true ? READINESS_LABELS.ready : READINESS_LABELS.not_ready;
}

function riskLabel(risk) {
  if (risk === null || typeof risk === "undefined" || risk === "") return "—";
  return RISK_LABELS[risk] || "未知状态";
}

function sideEffectLabel(value) {
  if (value === null || typeof value === "undefined" || value === "") return "—";
  return SIDE_EFFECT_LABELS[value] || "未知状态";
}

function proposalEventReasonText(reason) {
  if (!reason) return null;
  return PROPOSAL_EVENT_REASON_LABELS[reason] || String(reason);
}

function blockedReasonText(reason) {
  if (!reason) return null;
  const raw = String(reason);
  const first = raw.split(":")[0];
  const label = BLOCKED_REASON_LABELS[first];
  if (!label) return raw; // unknown code: raw value, never a guessed translation
  const detail = raw.length > first.length ? raw.slice(first.length + 1) : "";
  return detail ? `${label}（${detail}）` : label;
}

function stopConditionLabel(kind) {
  if (!kind) return "—";
  return STOP_CONDITION_LABELS[kind] || String(kind);
}

// Explicit distinguishable fallback for proposal cards when no verified
// fragment subject exists (P3B revision 2): derived from the fragment id's
// capture timestamp plus a stable suffix — never the shared generic goal,
// never a bare UUID, never a fabricated topic.
function proposalFallbackTitle(fragmentId, createdAt) {
  const stamp = /^(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})/.exec(fragmentId || "");
  if (stamp) {
    return `待获取碎片主题 · ${stamp[2]}-${stamp[3]} ${stamp[4]}:${stamp[5]}:${stamp[6]}`;
  }
  const formatted = formatDateTime(createdAt);
  const suffix = fragmentId ? ` · ${String(fragmentId).slice(-4)}` : "";
  if (formatted && formatted !== "—") return `待获取碎片主题 · ${formatted}${suffix}`;
  return `待获取碎片主题${suffix}`;
}

// The one most important line on a card: the first blocked reason when
// blocked, otherwise the route rationale. Raw value stays in the detail view.
function proposalKeyReason(item) {
  const blocked = Array.isArray(item.blocked_reasons) ? item.blocked_reasons : [];
  if (blocked.length) return blockedReasonText(blocked[0]);
  return item.route_reason ? String(item.route_reason) : null;
}

function proposalHealthSummary(envelope) {
  if (!envelope) return null;
  return {
    healthy: envelope.provider_healthy !== false && envelope.stale !== true,
    stale: envelope.stale === true,
    providerHealthy: envelope.provider_healthy === true,
    ledgerPresent: envelope.ledger_present === true,
    sourceEventId: typeof envelope.source_event_id === "number" ? envelope.source_event_id : 0,
  };
}

function proposalCardRow(item) {
  return {
    proposalId: text(item.proposal_id),
    runId: text(item.run_id),
    fragmentId: text(item.fragment_id),
    title: item.subject ? String(item.subject) : proposalFallbackTitle(item.fragment_id, item.created_at),
    status: text(item.proposal_status),
    statusLabel: proposalStatusLabel(item.proposal_status),
    readinessLabel: readinessLabel(item.execution_ready),
    executionReady: item.execution_ready === true,
    workerModel: item.worker_model ? String(item.worker_model) : "",
    workerAgent: item.worker_agent ? String(item.worker_agent) : "",
    keyReason: proposalKeyReason(item),
    updatedAt: formatDateTime(item.created_at),
  };
}

// Current proposals (proposed / needs_human_routing / blocked) as cards.
function proposalCurrentRows(items) {
  const list = Array.isArray(items) ? items : [];
  return list
    .filter((item) => !HISTORY_PROPOSAL_STATUSES.includes(text(item.proposal_status, "")))
    .map(proposalCardRow);
}

// History rows (stale / superseded) with the replacement pointer resolved
// from the current list (a new proposal names the one it supersedes).
function proposalHistoryRows(items) {
  const list = Array.isArray(items) ? items : [];
  const supersededBy = {};
  for (const item of list) {
    if (item.supersedes_proposal_id) supersededBy[String(item.supersedes_proposal_id)] = String(item.proposal_id);
  }
  return list
    .filter((item) => HISTORY_PROPOSAL_STATUSES.includes(text(item.proposal_status, "")))
    .map((item) => {
      const card = proposalCardRow(item);
      const lastEvent = item.last_event && typeof item.last_event === "object" ? item.last_event : null;
      return {
        ...card,
        transitionReason: lastEvent ? text(lastEvent.to_status) : null,
        transitionReasonText: lastEvent ? proposalEventReasonText(lastEvent.reason) : null,
        transitionAt: lastEvent ? formatDateTime(lastEvent.created_at) : "—",
        supersededByProposalId: supersededBy[card.proposalId] || null,
      };
    });
}

function proposalBudgetRows(budget) {
  const b = budget && typeof budget === "object" ? budget : {};
  const limits = b.limits && typeof b.limits === "object" ? b.limits : {};
  const used = b.used && typeof b.used === "object" ? b.used : {};
  const rows = [
    { key: "tokens", label: "令牌（Token）" },
    { key: "tool_calls", label: "工具调用次数" },
    { key: "iterations", label: "最大轮数" },
    { key: "seconds", label: "最长秒数" },
  ];
  return rows.map((row) => ({
    label: row.label,
    text: formatBudgetValue(
      typeof used[row.key] === "number" ? used[row.key] : null,
      typeof limits[row.key] === "number" ? limits[row.key] : null,
      ""
    ),
  }));
}

// Detail model for the right column: every frozen field, raw values kept.
function proposalDetailModel(item) {
  if (!item || typeof item !== "object") return null;
  const stopConditions = Array.isArray(item.stop_conditions) ? item.stop_conditions : [];
  const blockedReasons = Array.isArray(item.blocked_reasons) ? item.blocked_reasons : [];
  const riskReasons = Array.isArray(item.risk_reasons) ? item.risk_reasons : [];
  const requiredTools = Array.isArray(item.required_tools) ? item.required_tools : [];
  const events = Array.isArray(item.events) ? item.events : [];
  return {
    proposalId: text(item.proposal_id),
    runId: text(item.run_id),
    fragmentId: text(item.fragment_id),
    title: item.subject ? String(item.subject) : proposalFallbackTitle(item.fragment_id, item.created_at),
    status: text(item.proposal_status),
    statusLabel: proposalStatusLabel(item.proposal_status),
    readinessLabel: readinessLabel(item.execution_ready),
    executionReady: item.execution_ready === true,
    loopspec: `${text(item.proposed_loopspec)} · v${text(item.loopspec_version)}`,
    workerAgent: text(item.worker_agent),
    workerModel: text(item.worker_model),
    independentEvaluator: text(item.independent_evaluator, "无"),
    verifier: text(item.verifier),
    verifierLevel: item.extra && item.extra.verifier_level ? String(item.extra.verifier_level) : "—",
    budget: proposalBudgetRows(item.budget),
    risk: text(item.risk_level),
    riskLabel: riskLabel(item.risk_level),
    riskReasons: riskReasons.map((reason) => text(reason)),
    stopConditions: stopConditions.map((condition) => {
      const c = condition && typeof condition === "object" ? condition : {};
      const extra = c.kind === "no_gain" && typeof c.max_consecutive_no_gain === "number"
        ? `（最多连续 ${c.max_consecutive_no_gain} 轮）`
        : "";
      return `${stopConditionLabel(c.kind)}${extra}`;
    }),
    requiredTools: requiredTools.map((tool) => text(tool)),
    goal: item.extra && item.extra.goal ? String(item.extra.goal) : "—",
    privacyText: riskReasons.includes("privacy_content") ? "涉及隐私内容" : "不涉及隐私内容",
    untrustedWebText: riskReasons.includes("untrusted_web_input") ? "包含不可信网页输入" : "不含不可信网页输入",
    sideEffects: text(item.external_side_effects),
    sideEffectsLabel: sideEffectLabel(item.external_side_effects),
    routeReason: item.route_reason ? String(item.route_reason) : "—",
    blockedReasons: blockedReasons.map((reason) => blockedReasonText(reason)),
    blockedReasonsRaw: blockedReasons.map((reason) => text(reason)),
    loopStatus: text(item.loop_status),
    attemptEpisode: typeof item.attempt_episode === "number" ? item.attempt_episode : null,
    sourceSequence: typeof item.source_sequence === "number" ? item.source_sequence : null,
    fingerprint: text(item.fingerprint),
    dispatcherVersion: text(item.dispatcher_version),
    supersedesProposalId: item.supersedes_proposal_id ? String(item.supersedes_proposal_id) : null,
    createdAt: formatDateTime(item.created_at),
    expiresAt: formatDateTime(item.expires_at),
    events: events.map((event) => ({
      toStatus: text(event.to_status),
      toStatusLabel: proposalStatusLabel(event.to_status),
      reasonText: proposalEventReasonText(event.reason),
      createdAt: formatDateTime(event.created_at),
    })),
  };
}

// ---------------------------------------------------------------------------
// P3C shadow proposal review (accept/defer/reject bookkeeping — never
// execution). Chinese primary labels are table-driven; unknown values fall
// back to 未知状态 with the raw value preserved.
// ---------------------------------------------------------------------------

const REVIEW_DECISION_LABELS = {
  accepted: "已接受",
  deferred: "已暂缓",
  rejected: "已驳回",
};

// R2 §4.5：授权有效期共享判定（graph-view 侧栏/文本视图/授权队列三处复用，
// 不写第二套）。值无效或已过期为过期。
function isExpiredAt(value) {
  const time = Date.parse(value);
  return Number.isNaN(time) || time <= Date.now();
}

const REVIEW_REASON_TEXT = {
  proposal_changed: "建议已经变化，请重新查看",
  conflict: "已有其他审核决定，请刷新后重试",
  missing_reason: "缺少必填原因或恢复条件",
  missing_origin: "请求缺少可信来源",
  forbidden_origin: "请求来源不可信",
  unreachable: "影子建议审核服务不可用",
};

function reviewDecisionLabel(decision) {
  if (decision === null || typeof decision === "undefined" || decision === "") return "—";
  return REVIEW_DECISION_LABELS[decision] || "未知状态";
}

function reviewReasonText(code) {
  if (!code) return null;
  return REVIEW_REASON_TEXT[code] || String(code);
}

function reviewReceiptView(receipt) {
  if (!receipt || typeof receipt !== "object") return null;
  return {
    decisionId: text(receipt.decision_id),
    idempotencyKey: text(receipt.idempotency_key),
    proposalId: text(receipt.proposal_id),
    runId: text(receipt.run_id),
    fragmentId: text(receipt.fragment_id),
    fingerprint: text(receipt.proposal_fingerprint),
    sourceSequence: typeof receipt.source_sequence === "number" ? receipt.source_sequence : null,
    decision: text(receipt.decision),
    decisionLabel: reviewDecisionLabel(receipt.decision),
    reason: receipt.reason ? String(receipt.reason) : null,
    resumeCondition: receipt.resume_condition ? String(receipt.resume_condition) : null,
    note: receipt.note ? String(receipt.note) : null,
    decidedBy: text(receipt.decided_by),
    decidedAt: formatDateTime(receipt.decided_at),
    supersedesDecisionId: receipt.supersedes_decision_id ? String(receipt.supersedes_decision_id) : null,
  };
}

function reviewHistoryView(body) {
  const items = body && body.data && Array.isArray(body.data.items) ? body.data.items : [];
  return items.map(reviewReceiptView).filter(Boolean);
}

// Authoritative action matrix from the review server: the console dialog
// never invents submittability.
function reviewActionsView(body) {
  const data = body && body.data ? body.data : null;
  if (!data) return null;
  return {
    proposalId: text(data.proposal_id),
    submittable: data.submittable === true,
    reasonCode: data.reason_code || null,
    reasonText: reviewReasonText(data.reason_code),
    expectedFingerprint: data.proposal_fingerprint ? String(data.proposal_fingerprint) : null,
    expectedSourceSequence: typeof data.source_sequence === "number" ? data.source_sequence : null,
    expectedProposalStatus: data.proposal_status ? String(data.proposal_status) : null,
    currentDecision: data.current_decision ? reviewReceiptView(data.current_decision) : null,
  };
}

// Deterministic changed-fields-only comparison between a proposal and the
// one it supersedes. No model, no summaries, no guessed meaning: canonical
// JSON inequality per frozen field, raw values on both sides.
const PROPOSAL_COMPARE_FIELDS = [
  { key: "proposed_loopspec", label: "LoopSpec" },
  { key: "loopspec_version", label: "LoopSpec 版本" },
  { key: "worker_agent", label: "Worker（执行模型）" },
  { key: "worker_model", label: "执行模型版本" },
  { key: "independent_evaluator", label: "独立评估器" },
  { key: "verifier", label: "核验器" },
  { key: "risk_level", label: "风险等级" },
  { key: "external_side_effects", label: "外部副作用" },
  { key: "required_tools", label: "所需工具" },
  { key: "blocked_reasons", label: "阻塞原因" },
  { key: "stop_conditions", label: "停止条件" },
  { key: "budget", label: "预算" },
  { key: "execution_ready", label: "可执行状态" },
  { key: "proposal_status", label: "建议状态" },
];

function _canon(value) {
  if (value === null || typeof value === "undefined") return "";
  if (typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(_canon).join(",")}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${_canon(value[key])}`).join(",")}}`;
}

function _display(value) {
  if (value === null || typeof value === "undefined" || value === "") return "—";
  if (Array.isArray(value)) return value.map((item) => _display(item)).join("、");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function proposalComparison(currentItem, previousItem) {
  if (!currentItem || !previousItem) return [];
  const rows = [];
  for (const field of PROPOSAL_COMPARE_FIELDS) {
    const before = previousItem[field.key];
    const after = currentItem[field.key];
    if (_canon(before) === _canon(after)) continue;
    rows.push({ label: field.label, before: _display(before), after: _display(after) });
  }
  return rows;
}

module.exports = {
  isExpiredAt,
  DISPLAY_STATE_LABELS,
  STATUS_LABELS,
  EVENT_TYPE_LABELS,
  NODE_LABELS,
  VERDICT_LABELS,
  CONTROL_ACTION_LABELS,
  CONTROL_ACTION_EFFECTS,
  INTENT_STATUS_LABELS,
  REASON_TEXT,
  PRIMARY_ACTIONS,
  SECONDARY_ACTIONS,
  ATTENTION_STATUSES,
  ATTENTION_GENERIC_HINT,
  displayStateLabel,
  statusLabel,
  eventTypeLabel,
  nodeLabel,
  verdictLabel,
  controlActionLabel,
  intentStatusLabel,
  reasonText,
  fallbackTitle,
  controlActionsView,
  receiptView,
  intentHistoryView,
  attentionRows,
  normalRunRows,
  isAttentionRun,
  formatDateTime,
  formatDataAge,
  healthSummary,
  queueRows,
  runRows,
  rawStatusList,
  statusFilterOptions,
  budgetView,
  evalView,
  timelineView,
  detailModel,
  minimumValueDecisionView,
  cognitiveDraftView,
  cognitiveDraftStructuredView,
  DRAFT_VIEW_VERSION,
  cognitiveWithdrawalReceiptView,
  COGNITIVE_LOOP_IDS,
  COGNITIVE_LOOPSPEC_VERSION,
  consoleStatus,
  PROPOSAL_STATUS_LABELS,
  READINESS_LABELS,
  RISK_LABELS,
  SIDE_EFFECT_LABELS,
  PROPOSAL_EVENT_REASON_LABELS,
  BLOCKED_REASON_LABELS,
  STOP_CONDITION_LABELS,
  proposalStatusLabel,
  readinessLabel,
  riskLabel,
  sideEffectLabel,
  proposalEventReasonText,
  blockedReasonText,
  stopConditionLabel,
  proposalKeyReason,
  proposalFallbackTitle,
  proposalHealthSummary,
  proposalCurrentRows,
  proposalHistoryRows,
  proposalDetailModel,
  REVIEW_DECISION_LABELS,
  reviewDecisionLabel,
  reviewReasonText,
  reviewReceiptView,
  reviewHistoryView,
  reviewActionsView,
  proposalComparison,
};
