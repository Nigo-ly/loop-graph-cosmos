"use strict";

const { researchProposalReady, knowledgePublicationOf, researchResultDigestOf, effectiveExecutionScopeOf } = require("./fragment-intent-client.js");

const CARD_ATTR = "data-fragment-intent-card";
const INTENT_LABELS = {
  save: "保存这条信息",
  verify: "核实真实性",
  learn: "学习和深入了解",
  evaluate_relevance: "判断对我是否有价值",
  explore: "探索灵感和关联",
  deploy_or_build: "评估部署、下载或开发",
  plan_action: "形成行动计划",
  track: "持续关注",
};
// research_progress.stage → 用户可读的「当前在做什么」；未知枚举回退为
// 通用文案，绝不把内部枚举/节点名直接当产品文案。
const RESEARCH_STAGE_LABELS = {
  not_started: "尚未开始收集来源",
  collected: "已收集来源，正在整理判断",
  awaiting_authorization: "需要补充授权",
  synthesized: "已形成研究结论",
  no_evidence: "本轮未取得可核验证据",
  blocked: "研究受阻，未形成结论",
  failed_presend: "发送前检查未通过，未调用模型",
  output_invalid: "结果未通过校验，未采用",
  live_disabled: "研究能力未启用",
};
// 自主认知闭环（TASK F）：认知状态轴 → 用户可读标签。技术 ID/内部
// stop_reason/调度细节一律不出现；系统故障与耗尽不混入「等我决定」。
const COGNITIVE_STAGE_LABELS = {
  synthesis_disabled: "综合判断能力未启用，原问题尚未回答",
  watch_budget_exhausted: "自动补查次数已用尽，研究已停止",
  not_started: "系统核验尚未启动",
  capability_offline: "系统待恢复（核验能力未开启）",
  collecting: "系统正在收集来源",
  researching: "系统正在研究",
  evidence_ready: "已取得来源，待核验",
  search_exhausted: "已按策略搜索未果",
  awaiting_model_authorization: "需要你的决定（模型授权）",
  synthesized: "已形成研究结论",
  watching: "系统持续观察中",
  conflicted: "来源存在冲突",
};
const INDEPENDENT_REVIEW_STATES = {
  independent_review_failed: ["证据复核未完成（模型）", "审查调用或输出未通过要求，本轮结论未发布。"],
  independent_review_unknown: ["证据复核回执未知（模型）", "审查发送或回执状态不确定，系统不会重复发送。"],
  independent_review_limit: ["证据复核达到调用上限（模型）", "累计套餐调用已达上限或使用回执不完整，本轮复核未完成。"],
  independent_review_in_progress: ["证据复核进行中（模型）", "系统正在检查已有证据对答案的支持程度，尚无完整复核回执。"],
};
function independentReviewState(execution) {
  const progress = execution?.research_progress;
  const status = [progress?.stage, progress?.blocker, execution?.stop_reason]
    .find((value) => Object.hasOwn(INDEPENDENT_REVIEW_STATES, value));
  if (!status) return null;
  const [label, note] = INDEPENDENT_REVIEW_STATES[status];
  return { status, label, note: typeof progress?.note === "string" && progress.note ? progress.note : note,
    active: status === "independent_review_in_progress" };
}
function renderIndependentReview(parent, review) {
  const verdicts = { supported_with_limits: "现有证据支持，但有限制", revised: "已根据证据修订", insufficient_evidence: "证据不足，保留限制" };
  if (!review) return false;
  if (review.policy_version !== "independent-evidence-review-v1" || !Object.hasOwn(verdicts, review.verdict)
    || !/^[a-f0-9]{64}$/.test(review.draft_digest || "") || !/^[a-f0-9]{64}$/.test(review.reviewed_result_digest || "")
    || typeof review.reason !== "string" || typeof review.reviewed_at !== "string" || !Array.isArray(review.findings)) {
    parent.createEl("small", { text: "证据复核记录暂不可用，不能确认本记录是否经过独立模型复核。" });
    return false;
  }
  const box = parent.createEl("details", { cls: "fragment-independent-review" });
  box.createEl("summary", { text: `证据复核（模型） · ${verdicts[review.verdict]}` });
  box.createEl("p", { text: "仅检查所给证据对答案的支持程度；未经过人工审核，不代表已完成外部复现或事实证实。" });
  box.createEl("p", { text: `复核时间：${review.reviewed_at}` });
  box.createEl("p", { text: review.reason });
  const issues = { unsupported: "缺少支持", inference_as_fact: "推断被当成事实", stale: "材料已过时", overconfident: "判断过于确定", unverified_trial: "试跑未核实", citation_mismatch: "引用不匹配" };
  for (const finding of review.findings) {
    if (!finding || typeof finding !== "object") continue;
    const row = box.createDiv();
    if (typeof finding.statement === "string") row.createEl("p", { text: `原断言：${finding.statement}` });
    if (Object.hasOwn(issues, finding.issue)) row.createEl("small", { text: issues[finding.issue] });
    if (typeof finding.correction === "string") row.createEl("p", { text: `修订：${finding.correction}` });
    if (Array.isArray(finding.evidence_ids)) row.createEl("small", { text: `依据：${finding.evidence_ids.filter(id => typeof id === "string").join("、")}` });
  }
  return true;
}
function renderIndependentReviewPending(parent, execution, view) {
  parent.createEl("p", { cls: "fragment-independent-review-status", text: `${view.label}：${view.note}` });
  parent.createEl("small", { text: "这是系统处理状态，无需人工审批或重填目标；历史知识版本仍可在知识目录查阅。" });
  if (execution?.result) {
    const history = parent.createEl("details", { cls: "fragment-review-history" });
    history.createEl("summary", { text: "此前结果 / 待复核草稿（不代表本轮已完成）" });
    for (const field of ["summary", "answer_markdown"]) {
      if (typeof execution.result[field] === "string") history.createEl("p", { text: execution.result[field] });
    }
  }
}

// 旧 run 兼容投影（G12）：legacy stage/stop_reason → 认知态。
function cognitiveOfExecution(execution) {
  const review = independentReviewState(execution);
  if (review) return review.status;
  if (knowledgePublicationOf(execution)) return execution?.result?.conflicts?.length ? "conflicted" : "synthesized";
  const progress = execution && execution.research_progress;
  if (!progress) {
    // 无进度信息的旧执行：无法证明执行过任何网络请求——硬规则「0 次
    // 网络请求不得成为 search_exhausted/来源不足」，failure harvest
    // （旧 live_disabled 零请求失败的典型形态）保守投影为能力离线，
    // 绝不推断策略耗尽。
    if (
      execution && Array.isArray(execution.harvest) &&
      execution.harvest.some((entry) => entry && entry.role === "failure")
    ) {
      return "capability_offline";
    }
    return "collecting";
  }
  if (progress.blocker === "watch_budget_exhausted" || progress.watch?.status === "exhausted") {
    return "watch_budget_exhausted";
  }
  if (progress.blocker === "synthesis_disabled" || progress.stage === "synthesis_disabled") {
    return "synthesis_disabled";
  }
  if (progress.stage === "researching") return "researching";
  if (progress.cognitive) return progress.cognitive;
  const reason = progress.stop_reason;
  if (reason === "live_disabled" || reason === "capability_unavailable") return "capability_offline";
  const legacy = progress.stage;
  if (legacy === "synthesized") return "synthesized";
  if (legacy === "awaiting_authorization") return "awaiting_model_authorization";
  if (legacy === "no_evidence") {
    // 只有明确的 network_requests>0 且 plan_exhausted=true 才可显示
    // 策略耗尽；否则保守投影为系统处理中（绝不显示「来源不足」）。
    return progress.plan_exhausted === true && Number(progress.network_requests) > 0
      ? "search_exhausted"
      : "collecting";
  }
  if (legacy === "not_started") return "not_started";
  return "collecting";
}
const RESEARCH_STOP_LABELS = {
  completed: "正常完成",
  evidence_sufficient: "复用已有候选材料，结论另行核验",
  capability_unavailable: "能力不可用",
  not_found: "未找到可用来源",
  budget_exhausted: "达到预算上限",
  collection_deadline: "收集超过时限",
  run_deadline: "本轮超过时限",
  live_disabled: "研究能力未启用",
  authorization_blocked: "授权未通过",
  output_invalid: "结果未通过校验",
};
const RELATION_LABELS = {
  supports: "支持",
  partially_supports: "部分支持",
  conflicts: "冲突",
  irrelevant: "无关",
};
const EVIDENCE_MARKER_LABELS = {
  inherited: "继承自之前研究",
  newly_collected: "本轮新收集",
  omitted: "本轮未采用",
  stale: "已过时效，仅作线索",
};
const HARVEST_MATURITY_LABELS = {
  candidate: "候选",
  qualified: "合格",
  reusable: "可复用",
};

function basename(ref) {
  if (typeof ref !== "string") return "";
  const name = ref.split("/").pop() || "";
  return (name.endsWith(".md") ? name.slice(0, -3) : name).normalize("NFC");
}

function normalizedRef(ref) {
  return typeof ref === "string" ? ref.replace(/^!/, "").replace(/\.md$/, "").normalize("NFC") : "";
}

// Activity can supersede old continuation history only with an unambiguous zoned instant.
function alignmentActivityTime(value) {
  if (typeof value !== "string" || !/^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value)) return NaN;
  const day = new Date(`${value.slice(0, 10)}T00:00:00Z`);
  if (!Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== value.slice(0, 10)) return NaN;
  return Date.parse(value);
}

function alignmentFor(items, fragmentId) {
  // 真正的新用户目标仍按 Checkpoint sequence 选择；列表不承诺排序。
  const candidates = (items || []).filter((item) => item.fragment_id === fragmentId);
  const latest = candidates.reduce((current, item) => {
    if (!current || item.sequence > current.sequence) return item;
    if (item.sequence === current.sequence && item.alignment_id > current.alignment_id) return item;
    return current;
  }, null);
  // 用户重新承接旧根任务时，以新执行回执的时间为准；selected 配置本身不代表新活动。
  // 只沿同一碎片的实际父子关系，不推断未标记节点是自动恢复，也不修改历史。
  const lineage = latest ? [latest] : [];
  let cyclic = false;
  for (let current = latest; current?.parent_run_id;) {
    const parent = candidates.find(item => item.execution?.run_id === current.parent_run_id);
    if (!parent) break;
    if (lineage.includes(parent)) { cyclic = true; break; }
    lineage.push(parent);
    current = parent;
  }
  let renewed = null, newerThan = -Infinity;
  if (!cyclic) for (let index = 1; index < lineage.length; index += 1) {
    const descendant = lineage[index - 1], parent = lineage[index];
    const times = [alignmentActivityTime(descendant.updated_at), alignmentActivityTime(descendant.execution?.updated_at)];
    if (!times.every(Number.isFinite)) break;
    newerThan = Math.max(newerThan, ...times);
    const execution = parent.execution;
    const hasActivity = execution?.research_progress?.stage === "researching" ||
      independentReviewState(execution) || hasResearchConclusion(execution);
    if (execution?.subscription_selected === true && hasActivity && alignmentActivityTime(execution.updated_at) > newerThan) renewed = parent;
  }
  if (renewed) return renewed;
  // 旧离线恢复曾重复创建同目标子任务。只沿后端明确标记的恢复谱系回看，
  // 不用标题猜测，也不让无结论的恢复副本遮住父任务的结论或套餐接管。
  const visited = new Set();
  let recovery = latest;
  while (recovery?.recovery_reason === "offline_zero_evidence" && !visited.has(recovery)) {
    if (hasResearchConclusion(recovery.execution)) break;
    visited.add(recovery);
    const parent = typeof recovery.parent_run_id === "string" && recovery.parent_run_id
      ? candidates.find((item) => item.execution?.run_id === recovery.parent_run_id) : null;
    if (!parent || visited.has(parent)) break;
    if (hasResearchConclusion(parent.execution) || parent.execution?.subscription_selected === true) return parent;
    recovery = parent;
  }
  return latest;
}

function subscriptionExecutionLabel(execution) {
  const authorization = execution?.subscription_authorization;
  const progress = execution?.research_progress;
  const providers = { kimi_subscription: "Kimi 月套餐", codex_subscription: "Codex 月套餐" };
  const authorized = authorization?.source === "user_authorized_subscription" && execution.run_id &&
    authorization.run_id === execution.run_id && providers[authorization.provider] &&
    Number.isInteger(authorization.max_agent_invocations) && authorization.max_agent_invocations >= 0 &&
    Number.isInteger(authorization.max_tools) && authorization.max_tools >= 0;
  const parts = [];
  if (authorized) parts.push(`执行授权回执：${providers[authorization.provider]}；回执所载 Agent 调用上限 ${authorization.max_agent_invocations} 次，工具调用上限 ${authorization.max_tools} 次`);
  // Prospective captures can have journal-backed progress without the older authorization receipt.
  // The current configured provider is not evidence that it actually executed this run.
  if (authorized || providers[progress?.provider]) {
    const observed = [];
    if (Number.isInteger(progress?.agent_invocations) && progress.agent_invocations >= 0) observed.push(`已观察 ${progress.agent_invocations} 次 Agent 调用`);
    if (Number.isInteger(progress?.model_calls) && progress.model_calls >= 0) observed.push(`${progress.model_calls} 次模型调用`);
    if (observed.length) parts.push(`${providers[progress?.provider] ? `实际记录：${providers[progress.provider]}；` : ""}${observed.join("，")}${progress.model_calls_unknown ? "（调用次数尚未完全确认）" : ""}`);
  }
  return parts.length ? `${parts.join("。 ")}。`
    : execution?.subscription_selected === true ? "本轮已纳入套餐研究范围；尚无实际执行回执。" : "";
}

function renderEffectiveExecutionScope(parent, item) {
  const scope = effectiveExecutionScopeOf(item);
  if (!scope) return false;
  const provider = { kimi_subscription: "Kimi 月套餐", codex_subscription: "Codex 月套餐" }[scope.model_provider];
  const box = parent.createEl("details", { cls: "fragment-effective-scope" });
  box.createEl("summary", { text: `当前配置允许：${provider}（能力范围）` });
  box.createEl("p", { text: `Agent 调用槽位上限 ${scope.model_call_cap} 次（含未知调用与模型复核）；新增工具回执上限 ${scope.tool_call_cap} 条。使用现有月套餐，具体模型未标注。` });
  for (const [label, values] of [["可用能力", scope.capabilities], ["材料范围", scope.external_scope], ["可写范围", scope.write_scope], ["当前排除", scope.exclusions]]) {
    if (values.length) box.createEl("p", { text: `${label}：${values.join("；")}` });
  }
  box.createEl("small", { text: `仓库试跑：${scope.repository_trial_allowed ? "允许使用已装配工具" : "未开放"}；自动知识保存：${scope.automatic_knowledge_save_allowed ? "已配置" : "未配置"}。是否已经试跑或保存，以实际结果和回执为准。` });
  return true;
}

function refsForCard(card) {
  let rawRef = "";
  let organizedRef = "";
  for (const anchor of card.querySelectorAll("a")) {
    const label = (anchor.textContent || "").trim();
    const ref = anchor.getAttribute("data-path") || anchor.getAttribute("data-href") || "";
    if (label === "原始记录") rawRef = ref;
    if (label === "查看整理结果") organizedRef = ref;
    if (label === "详情 →" && hasClass(card, "life-organized-card")) organizedRef = ref;
  }
  if (!rawRef && organizedRef && hasClass(card, "life-organized-card")) {
    const sourceFragment = card.getAttribute("data-source-fragment") || "";
    rawRef = `Notes/散记/碎片想法/${sourceFragment || basename(organizedRef)}.md`;
  }
  return { rawRef, organizedRef, fragmentId: basename(rawRef) };
}

function hasClass(element, name) {
  return Boolean(
    (element.classList && element.classList.contains(name)) ||
    (element.classes && element.classes.has(name))
  );
}

function button(parent, text, cls, handler) {
  const item = parent.createEl("button", { text, cls });
  item.setAttribute("type", "button");
  item.addEventListener("click", handler);
  return item;
}

function renderProposed(slot, item, handlers) {
  slot.createEl("p", { cls: "fragment-intent-eyebrow", text: "处理方向确认" });
  slot.createEl("strong", { text: "我理解你想这样推进" });
  slot.createEl("p", { cls: "fragment-intent-reason", text: item.reasoning });

  const options = slot.createDiv({ cls: "fragment-intent-options" });
  const selected = new Set(item.suggested_intents);
  const all = [
    ...Object.entries(INTENT_LABELS).map(([id, label]) => ({ id, label })),
    ...item.dynamic_intents.map((entry) => ({ id: entry.id, label: entry.label })),
  ];
  for (const entry of all) {
    const label = options.createEl("label", { cls: "fragment-intent-option" });
    const input = label.createEl("input");
    input.setAttribute("type", "checkbox");
    input.setAttribute("data-intent-id", entry.id);
    if (selected.has(entry.id)) input.setAttribute("checked", "checked");
    label.createEl("span", { text: entry.label });
  }

  const details = slot.createDiv({ cls: "fragment-intent-plan" });
  details.createEl("p", { text: `准备这样推进：${item.plan}` });
  details.createEl("p", { text: `预计得到：${item.expected_result}` });
  if (!renderEffectiveExecutionScope(details, item)) {
    const policy = item.execution_scope.model_call_cap > 0 &&
      item.execution_scope.model_provider && item.execution_scope.model_name ?
      ` · 模型 ${item.execution_scope.model_provider}/${item.execution_scope.model_name}` : "";
    const writes = Array.isArray(item.execution_scope.write_scope) &&
      item.execution_scope.write_scope.length ?
      ` · 写入 ${item.execution_scope.write_scope.join("、")}` : "";
    details.createEl("p", {
      text: `原始提案范围（不代表本轮实际执行）：模型最多 ${item.execution_scope.model_call_cap} 次${policy} · 成本上限 ¥${item.execution_scope.cost_cap_cny} · ${item.execution_scope.side_effect}${writes}`,
    });
  }
  if (item.memory_basis.length) {
    details.createEl("small", {
      text: `判断依据：${item.memory_basis.map((basis) => basis.label).join("、")}`,
    });
  }

  const supplement = slot.createEl("textarea", {
    cls: "fragment-intent-supplement",
  });
  supplement.setAttribute("placeholder", "补充你希望关注的方向（可选）");
  supplement.setAttribute("maxlength", "500");
  const message = slot.createEl("small", { cls: "fragment-intent-message", text: "" });
  const actions = slot.createDiv({ cls: "fragment-intent-actions" });
  const confirm = button(actions, "确认并继续", "fragment-intent-confirm", async () => {
    if (confirm.getAttribute("disabled") !== null) return;
    const intents = [];
    for (const input of options.querySelectorAll("input")) {
      if (input.getAttribute("checked") !== null || input.checked === true) {
        intents.push(input.getAttribute("data-intent-id"));
      }
    }
    if (!intents.length) {
      message.setText("至少选择一个处理方向，或选择仅保存。");
      return;
    }
    confirm.setAttribute("disabled", "disabled");
    saveOnly.setAttribute("disabled", "disabled");
    try {
      await handlers.onDecide(item, {
        action: "confirm",
        intents,
        supplement: supplement.value || "",
      }, (text) => message.setText(text));
    } finally {
      confirm.removeAttribute("disabled");
      saveOnly.removeAttribute("disabled");
    }
  });
  const saveOnly = button(actions, "仅保存", "fragment-intent-save", async () => {
    if (saveOnly.getAttribute("disabled") !== null) return;
    confirm.setAttribute("disabled", "disabled");
    saveOnly.setAttribute("disabled", "disabled");
    try {
      await handlers.onDecide(item, {
        action: "save_only",
        intents: ["save"],
        supplement: "",
      }, (text) => message.setText(text));
    } finally {
      confirm.removeAttribute("disabled");
      saveOnly.removeAttribute("disabled");
    }
  });
}

// 旧 run 兼容投影（G12）：legacy stage/stop_reason → 认知态。
// 运行中紧凑 research_progress：认知态（当前在做什么）/ 已取得多少有效来源 /
// 是否真正需要你的决定；禁止日志墙、技术 ID 与内部推理。
function renderResearchProgress(slot, item, progress) {
  const line = slot.createDiv({ cls: "fragment-research-progress" });
  const cognitive = cognitiveOfExecution({ research_progress: progress });
  const stage = COGNITIVE_STAGE_LABELS[cognitive] || RESEARCH_STAGE_LABELS[progress.stage] || "研究进行中";
  const parts = [`当前：${stage}`, `已取得 ${progress.collected_sources} 个候选来源`];
  if (cognitive === "awaiting_model_authorization") parts.push("需要你的决定");
  if (cognitive === "watching" && progress.watch) parts.push("系统将按条件自动复查");
  line.createEl("p", { text: parts.join(" · ") });
  if (progress.note && cognitive !== "capability_offline") line.createEl("small", { text: progress.note });
  if (cognitive === "awaiting_model_authorization" && !effectiveExecutionScopeOf(item)) {
    const scope = item.execution_scope;
    line.createEl("small", {
      text: `原始提案待授权范围：模型最多 ${scope.model_call_cap} 次 · 成本上限 ¥${scope.cost_cap_cny} · ${scope.side_effect}`,
    });
  }
}

function hasResearchConclusion(execution) {
  return Boolean(!independentReviewState(execution) && execution?.status === "passed" && /^[a-f0-9]{64}$/.test(execution.result_digest || "")
    && execution.result && (knowledgePublicationOf(execution)
    || ["synthesized", "conflicted"].includes(cognitiveOfExecution(execution))));
}

function researchSection(parent, label) {
  const section = parent.createDiv({ cls: "fragment-research-section" });
  section.createEl("small", { cls: "fragment-research-label", text: label });
  return section;
}

// 冻结九段（DESIGN §9）：研究目标、结论摘要、已确认、仍未知、来源冲突、
// 建议行动、证据与来源、预算与停止原因、后续入口。证据与来源默认折叠；
// 技术 ID/digest/节点名只进折叠审计区；缺字段一律诚实降级，不模板冒充。
// 返回「后续入口」容器，由调用方填入 continuation 与升级入口。
function renderResearchResult(slot, item, execution, evidenceMissing, published = false) {
  const result = execution.result;
  const progress = execution.research_progress || null;
  const synthesized = result.model_calls > 0 ||
    (progress && progress.stage === "synthesized");
  const noEvidence = Boolean(evidenceMissing) ||
    (progress && progress.collected_sources === 0) ||
    (progress && progress.stage === "no_evidence");
  const box = slot.createDiv({ cls: "fragment-research-result" });
  const scope = item.execution_scope;

  const goalHost = published ? box.createEl("details", { cls: "fragment-research-goal" }) : box;
  if (published) goalHost.createEl("summary", { text: "原研究问题" });
  const goal = researchSection(goalHost, "研究目标");
  const supplement = item.decision && item.decision.supplement ?
    `（补充：${item.decision.supplement}）` : "";
  goal.createEl("p", { text: `${item.title}${supplement}` });

  researchSection(box, "结论摘要").createEl("p", { text: result.summary });
  if (typeof result.answer_markdown === "string" && result.answer_markdown.trim()) {
    const answer = published ? box.createEl("details", { cls: "fragment-research-answer" }) : box;
    if (published) answer.createEl("summary", { text: "完整回答" });
    researchSection(answer, "完整回答").createEl("p", { cls: "life-knowledge-prose", text: result.answer_markdown });
  }
  if (Array.isArray(result.limitations) && result.limitations.length) {
    const list = researchSection(box, "适用限制").createEl("ul");
    for (const entry of result.limitations) list.createEl("li", { text: entry });
  }

  const confirmed = researchSection(box, "已确认");
  if (Array.isArray(result.confirmed) && result.confirmed.length) {
    const list = confirmed.createEl("ul");
    for (const entry of result.confirmed) list.createEl("li", { text: entry.claim });
  } else {
    confirmed.createEl("p", {
      text: noEvidence ? "本轮未取得可核验证据。" :
        synthesized ? "本轮没有新增已确认的结论。" : "已收集来源但尚未形成可靠判断。",
    });
  }

  const unknowns = researchSection(box, "仍未知");
  if (result.unknowns.length) {
    const list = unknowns.createEl("ul");
    for (const unknown of result.unknowns) list.createEl("li", { text: unknown });
  } else {
    unknowns.createEl("p", { text: "本轮没有遗留的未知项。" });
  }

  const conflicts = researchSection(box, "来源冲突");
  if (Array.isArray(result.conflicts) && result.conflicts.length) {
    const list = conflicts.createEl("ul");
    for (const entry of result.conflicts) {
      list.createEl("li", { text: `${entry.topic}：${entry.dimensions.join("；")}` });
    }
  } else {
    conflicts.createEl("p", {
      text: synthesized ? "各来源之间未发现冲突。" : "本轮未进行来源比对。",
    });
  }

  const actions = researchSection(box, "建议行动");
  if (typeof result.recommendation === "string" && result.recommendation) {
    actions.createEl("p", { text: result.recommendation });
  } else if (result.next_checks.length) {
    const list = actions.createEl("ul");
    for (const check of result.next_checks) list.createEl("li", { text: check });
  } else {
    actions.createEl("p", { text: "暂无建议行动。" });
  }

  const records = Array.isArray(execution.research_evidence) ? execution.research_evidence : [];
  const claims = Array.isArray(result.claims) ? result.claims : [];
  const sources = box.createEl("details", { cls: "fragment-research-sources" });
  sources.createEl("summary", {
    text: `证据与来源（${records.length + claims.length} 条）`,
  });
  if (!records.length && !claims.length) {
    sources.createEl("p", { text: "本轮没有可展示的证据记录。" });
  }
  if (claims.length) {
    const list = sources.createEl("ul");
    for (const claim of claims) {
      list.createEl("li", {
        text: `${claim.claim}（${RELATION_LABELS[claim.relation] || "关联"}）`,
      });
    }
  }
  if (records.length) {
    const list = sources.createEl("ul");
    for (const record of records) {
      list.createEl("li", {
        text: `${record.title} — ${record.url}（${EVIDENCE_MARKER_LABELS[record.marker] || "来源"}）`,
      });
    }
  }

  const technical = published ? box.createEl("details", { cls: "fragment-research-technical" }) : box;
  if (published) technical.createEl("summary", { text: "预算与运行记录" });
  const budget = researchSection(technical, "预算与停止原因");
  const networkRequests = progress && Number.isInteger(progress.network_requests) ?
    progress.network_requests : result.tool_calls;
  if (effectiveExecutionScopeOf(item) || subscriptionExecutionLabel(execution)) {
    budget.createEl("p", { text: `${progress?.model_calls_unknown ? "已观察至少" : "已记录"} ${result.model_calls} 次模型调用${progress?.model_calls_unknown ? "（总数尚未完全确认）" : ""} · 网络请求 ${networkRequests} 次。当前配置与执行授权分别见上方说明。` });
  } else {
    budget.createEl("p", {
      text: `模型调用 ${result.model_calls} 次 · 网络请求 ${networkRequests} 次；原始提案上限：模型 ${scope.model_call_cap} 次、成本 ¥${scope.cost_cap_cny}（不代表本轮实际授权或已发生费用）。`,
    });
  }
  const stopReason = (progress && progress.stop_reason) || execution.stop_reason || "";
  if (stopReason) {
    budget.createEl("p", {
      text: `停止原因：${RESEARCH_STOP_LABELS[stopReason] || "研究已停止，请查看审计信息"}`,
    });
  }

  const audit = technical.createEl("details", { cls: "fragment-intent-audit" });
  audit.createEl("summary", { text: "审计信息（技术标识）" });
  const auditList = audit.createEl("ul");
  auditList.createEl("li", { text: `run_id：${execution.run_id}` });
  if (execution.result_digest) {
    auditList.createEl("li", { text: `result_digest：${execution.result_digest}` });
  }
  if (execution.current_node) {
    auditList.createEl("li", { text: `current_node：${execution.current_node}` });
  }
  const proposal = execution.graph_escalation;
  if (proposal && proposal.escalation_id) {
    auditList.createEl("li", { text: `escalation_id：${proposal.escalation_id}` });
  }
  for (const claim of claims) {
    if (claim.evidence_id) auditList.createEl("li", { text: `evidence_id：${claim.evidence_id}` });
  }
  for (const record of records) {
    if (record.evidence_id) auditList.createEl("li", { text: `evidence_id：${record.evidence_id}` });
    if (record.evidence_digest) {
      auditList.createEl("li", { text: `evidence_digest：${record.evidence_digest}` });
    }
  }

  return researchSection(box, "后续入口");
}

// harvest_candidate → 后续入口（DESIGN §9 映射）：只渲染安全字段
// （summary/maturity/role），默认折叠；候选明确标注「尚未成为知识资产」，
// 绝不自动冒充知识资产（包 F 产品验收 #12）。返回是否有候选被渲染。
function renderHarvestCandidates(followup, execution) {
  const harvest = Array.isArray(execution.harvest) ? execution.harvest : [];
  if (!harvest.length) return false;
  const box = followup.createEl("details", { cls: "fragment-harvest-candidates" });
  box.createEl("summary", {
    text: `经验候选（${harvest.length} 条，尚未成为知识资产）`,
  });
  const list = box.createEl("ul");
  for (const entry of harvest) {
    const maturity = HARVEST_MATURITY_LABELS[entry.maturity] || "候选";
    const prefix = entry.role === "failure" ? "失败模式：" : "";
    list.createEl("li", { text: `${prefix}${entry.summary}（${maturity}）` });
  }
  return true;
}

function renderConfirmed(slot, item, handlers) {
  const route = {
    save_only: "仅保存",
    direct: "直接处理",
    verify: "补充核验",
    graph: "准备升级 Graph",
  }[item.route] || "已确认";
  const execution = item.execution;
  const publication = knowledgePublicationOf(execution);
  const concluded = hasResearchConclusion(execution);
  const publicationUnavailable = execution?.knowledge_publication?.status === "unavailable"
    && execution.knowledge_publication.result_digest === researchResultDigestOf(execution);
  const cognitive = cognitiveOfExecution(execution);
  const published = Boolean(publication && concluded && !publicationUnavailable
    && !execution?.research_progress?.blocker
    && [undefined, "synthesized"].includes(execution?.research_progress?.stage)
    && [undefined, "synthesized", "conflicted"].includes(execution?.research_progress?.cognitive));
  // 「来源不足」只作为历史 legacy 反例存在；系统状态（能力关闭/处理中/观察）
  // 绝不解释为用户该补来源（TASK F）。
  const evidenceMissing = Boolean(
    execution && execution.route === "verify" && cognitive === "search_exhausted" &&
    execution.harvest.some((entry) => entry.role === "failure")
  );
  if (evidenceMissing) slot.addClass("is-unresolved");
  slot.createEl("p", { cls: "fragment-intent-eyebrow", text: published ? "研究结论" : "处理方向已确认" });
  if (!published) slot.createEl("strong", { text: route });
  if (execution) {
    const subscriptionLabel = subscriptionExecutionLabel(execution);
    if (!published) {
      if (subscriptionLabel) slot.createEl("small", { cls: "fragment-subscription-scope", text: subscriptionLabel });
      renderEffectiveExecutionScope(slot, item);
    }
    const review = independentReviewState(execution);
    if (review) { renderIndependentReviewPending(slot, execution, review); return; }
    renderIndependentReview(slot, execution.independent_review);
    const state = publicationUnavailable ? "已存笔记发生变动或无法读取，原结论保留" : publication ? "结论已自动沉淀" : concluded ? "已形成结论，尚无本次保存回执" : execution.status === "passed" ?
      (evidenceMissing ? "本轮未取得可核验证据" : "处理已结束") :
      execution.status === "blocked" ? "需要补充能力" : "处理中";
    slot.createEl("p", { cls: "fragment-intent-reason", text: state });
    if (execution.research_progress && !published) {
      renderResearchProgress(slot, item, execution.research_progress);
    }
    // The exact saved note is the primary action; completed research needs no new goal.
    if (published && typeof handlers.onOpenKnowledge === "function") {
      const message = slot.createEl("small", { cls: "fragment-intent-message", text: "" });
      button(slot, "打开已沉淀知识", "fragment-intent-knowledge", async () => {
        try { await handlers.onOpenKnowledge(publication.path); }
        catch (error) { message.setText(error?.message || "知识文档暂时无法打开。"); }
      });
    }
    let followup = null;
    if (execution.result) {
      if (execution.route === "verify") {
        followup = renderResearchResult(slot, item, execution, evidenceMissing, published);
      } else {
        const result = slot.createDiv({ cls: "fragment-intent-result" });
        result.createEl("strong", { text: "本次结果" });
        result.createEl("p", { text: execution.result.summary });
        if (execution.result.unknowns.length) {
          result.createEl("small", {
            text: `仍未知：${execution.result.unknowns.join("；")}`,
          });
        }
        if (execution.result.next_checks.length) {
          const checks = result.createDiv({ cls: "fragment-intent-checks" });
          checks.createEl("small", { text: "下一步可选" });
          const list = checks.createEl("ul");
          for (const check of execution.result.next_checks) list.createEl("li", { text: check });
        }
      }
    }
    if (publication || concluded) {
      const host = followup || slot;
      host.createEl("p", { text: publication
        ? `已自动保存到知识库 · 修订 ${publication.revision}；未经过人工审核。`
        : publicationUnavailable ? "已存笔记发生变动或无法读取，原结论保留；不会覆盖你的文件。"
          : "结论已形成，尚未取得本次知识保存回执；无需你审批候选或重填目标。" });
      if (publicationUnavailable && execution.knowledge_publication.reason) host.createEl("small", { text: execution.knowledge_publication.reason });
      if (execution.result?.unknowns?.length) host.createEl("small", { text: "未解决事项是本结论的边界，保留供后续补充；不代表需要你另建目标。" });
      if (publication && !published && typeof handlers.onOpenKnowledge === "function") {
        const message = host.createEl("small", { cls: "fragment-intent-message", text: "" });
        button(host, "打开已沉淀知识", "fragment-intent-knowledge", async () => {
          try { await handlers.onOpenKnowledge(publication.path); }
          catch (error) { message.setText(error?.message || "知识文档暂时无法打开。"); }
        });
      }
      if (published) {
        const receipt = slot.createEl("details", { cls: "fragment-execution-details" });
        receipt.createEl("summary", { text: "执行回执与处理范围" });
        if (subscriptionLabel) receipt.createEl("small", { cls: "fragment-subscription-scope", text: subscriptionLabel });
        renderEffectiveExecutionScope(receipt, item);
        if (execution.research_progress) renderResearchProgress(receipt, item, execution.research_progress);
      }
    } else if (execution.status === "passed" && execution.result_digest) {
      const continuationHost = followup || slot;
      if (followup) renderHarvestCandidates(followup, execution);
      if (["capability_offline", "synthesis_disabled", "watch_budget_exhausted"].includes(cognitive)) {
        // TASK F：能力关闭 ≠ 用户任务——不渲染继续按钮，诚实显示系统待恢复
        // （恢复后系统按 due 条件自动继续，无需用户补来源）。
        continuationHost.createEl("small", {
          cls: "fragment-intent-offline-note",
          text: cognitive === "capability_offline"
            ? "核验能力未开启，系统待恢复；无需你补来源或重填目标。"
            : COGNITIVE_STAGE_LABELS[cognitive],
        });
      } else {
      const continuation = continuationHost.createEl("details", { cls: "fragment-intent-continuation" });
      continuation.createEl("summary", {
        text: evidenceMissing ? "补充来源后继续核验" : "继续处理这个主题",
      });
      const goal = continuation.createEl("textarea", { cls: "fragment-intent-supplement" });
      goal.setAttribute(
        "placeholder",
        evidenceMissing ? "补充一个明确的官方或仓库链接" : "写下新的处理目标"
      );
      goal.setAttribute("maxlength", "200");
      const message = continuation.createEl("small", { cls: "fragment-intent-message", text: "" });
      const actions = continuation.createDiv({ cls: "fragment-intent-actions" });
      const next = button(
        actions,
        evidenceMissing ? "创建后续核验" : "创建后续处理",
        "fragment-intent-continue",
        async () => {
        if (next.getAttribute("disabled") !== null) return;
        const value = (goal.value || "").trim();
        if (!value) {
          message.setText("请先写下这次继续处理的目标。");
          return;
        }
        next.setAttribute("disabled", "disabled");
        try {
          await handlers.onContinue(item, value, (text) => message.setText(text));
        } finally {
          next.removeAttribute("disabled");
        }
      });
      const proposal = execution.graph_escalation;
      if (proposal && proposal.status === "proposed") {
        if (researchProposalReady(item) && typeof handlers.onCreateResearchRun === "function") {
          // 合法 graph_proposal（未获准执行）：文案不得暗示已创建 Graph Run。
          actions.createEl("small", {
            cls: "fragment-research-proposal-note",
            text: "已有升级提案（尚未执行）；确认后才会创建研究 Run。",
          });
          const create = button(actions, "创建 Graph 研究 Run", "fragment-intent-research-create", async () => {
            if (create.getAttribute("disabled") !== null) return;
            create.setAttribute("disabled", "disabled");
            try {
              await handlers.onCreateResearchRun(item, (text) => message.setText(text));
            } finally {
              create.removeAttribute("disabled");
            }
          });
        } else {
          const escalate = button(actions, "提出 Graph 升级", "fragment-intent-escalate", async () => {
            if (escalate.getAttribute("disabled") !== null) return;
            escalate.setAttribute("disabled", "disabled");
            try {
              await handlers.onEscalate(item, (text) => message.setText(text));
            } finally {
              escalate.removeAttribute("disabled");
            }
          });
        }
      }
      }
    } else if (followup) {
      // 无 continuation 可用时：有 harvest 候选渲染候选，否则保持诚实空态。
      if (!renderHarvestCandidates(followup, execution)) {
        followup.createEl("small", { text: "当前没有可用的后续入口。" });
      }
    }
  } else if (item.route === "graph") {
    slot.createEl("p", {
      cls: "fragment-intent-reason",
      text: "已形成 Graph 升级提案；当前没有匹配的工作流模板，因此没有创建 Run。",
    });
  } else {
    slot.createEl("p", { cls: "fragment-intent-reason", text: "已记录你的选择。" });
  }
}

function injectFragmentIntentCards(doc, state, handlers) {
  let count = 0;
  const captureCards = [...doc.querySelectorAll(".my-life-homepage-view .life-capture-card")];
  const captureRefs = captureCards.map((card) => refsForCard(card));
  const captureIds = new Set(captureRefs.map((refs) => refs.fragmentId));
  const captureByOrganizedRef = new Map(
    captureRefs
      .filter((refs) => refs.fragmentId && normalizedRef(refs.organizedRef))
      .map((refs) => [normalizedRef(refs.organizedRef), refs]),
  );
  const organizedCards = [...doc.querySelectorAll(".my-life-homepage-view .life-organized-card")];
  for (const card of [...captureCards, ...organizedCards]) {
    const cardRefs = refsForCard(card);
    // 整理输出的文件名不一定等于原始 fragment_id。同屏原始卡已经携带
    // 受信的 raw→organized 绑定，必须复用它；按输出文件名再造身份会让
    // 同一碎片出现第二张确认表单。
    const refs = hasClass(card, "life-organized-card") ?
      captureByOrganizedRef.get(normalizedRef(cardRefs.organizedRef)) || cardRefs : cardRefs;
    if (hasClass(card, "life-organized-card") && captureIds.has(refs.fragmentId)) {
      // 插件热重载会保留 Markdown DOM；先移除旧版本曾注入的重复卡，再跳过。
      card.querySelector(`[${CARD_ATTR}]`)?.remove();
      continue;
    }
    if (!refs.fragmentId || !refs.organizedRef || !handlers.isLoopApproved(refs.rawRef)) continue;
    const item = alignmentFor(state.items, refs.fragmentId);
    const fingerprint = [
      refs.fragmentId,
      item ? item.status : "none",
      item ? item.sequence : "",
      item && item.execution ? item.execution.status : "",
      item && item.execution ? item.execution.updated_at : "",
      item && item.execution ? item.execution.result_digest || "" : "",
      JSON.stringify(item?.execution?.knowledge_publication || null),
      researchResultDigestOf(item?.execution),
      subscriptionExecutionLabel(item?.execution),
      JSON.stringify(effectiveExecutionScopeOf(item)),
      JSON.stringify(item?.execution?.independent_review || null),
      JSON.stringify(independentReviewState(item?.execution)),
      item && item.execution && item.execution.research_progress ?
        item.execution.research_progress.stage : "",
      item && item.execution && item.execution.graph_escalation ?
        item.execution.graph_escalation.status : "",
      state.error || "",
    ].join("|");
    const existing = card.querySelector(`[${CARD_ATTR}]`);
    if (existing && existing.getAttribute(CARD_ATTR) === fingerprint) continue;
    existing?.remove();
    const slot = doc.createElement("section");
    slot.addClass("fragment-intent-card");
    slot.setAttribute(CARD_ATTR, fingerprint);
    if (state.error) {
      slot.createEl("small", { text: `处理方向暂不可用：${state.error}` });
    } else if (!item) {
      slot.createEl("p", { cls: "fragment-intent-reason", text: "整理已完成，可以先确认你希望怎样处理。" });
      const message = slot.createEl("small", { cls: "fragment-intent-message", text: "" });
      const start = button(slot, "确认处理方向", "fragment-intent-start", async () => {
        if (start.getAttribute("disabled") !== null) return;
        start.setAttribute("disabled", "disabled");
        try {
          await handlers.onPropose(refs);
        } catch (error) {
          message.setText(error && error.message ? error.message : "暂时无法生成处理建议。");
        } finally {
          start.removeAttribute("disabled");
        }
      });
    } else if (item.status === "suggested") {
      renderProposed(slot, item, handlers);
    } else {
      renderConfirmed(slot, item, handlers);
    }
    const footer = card.querySelector("footer");
    if (footer && typeof footer.insertAdjacentElement === "function") {
      footer.insertAdjacentElement("afterend", slot);
    } else {
      card.appendChild(slot);
    }
    count += 1;
  }
  return count;
}

module.exports = {
  CARD_ATTR,
  INTENT_LABELS,
  RESEARCH_STAGE_LABELS,
  RESEARCH_STOP_LABELS,
  alignmentFor,
  subscriptionExecutionLabel,
  renderEffectiveExecutionScope,
  independentReviewState,
  renderIndependentReview,
  renderIndependentReviewPending,
  cognitiveOfExecution,
  hasResearchConclusion,
  basename,
  refsForCard,
  hasClass,
  injectFragmentIntentCards,
};
