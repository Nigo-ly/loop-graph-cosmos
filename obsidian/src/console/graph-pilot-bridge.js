"use strict";

// Graph Pilot Entry Bridge v1 主页入口（GRAPH-PILOT-ENTRY-BRIDGE-V1-DESIGN.md
// rev7）：碎片卡资格判定、「转为 Graph 工作流」确认页、创建调用、已桥接入口。
//
// 边界：UI 不展示 Prompt、碎片正文、凭据、授权短语或技术 ID；所有外部文本
// 只用文本节点渲染；桥失败绝不影响碎片投递、Loop、主页或记忆面。

const PILOT_GRAPH_ID = "fragment-pilot-v1";
const BRIDGE_ATTR = "data-pilot-bridge";
const DIALOG_CLASS = "graph-pilot-dialog-overlay";

// §3.10 冻结匹配规则：双侧去目录、去 .md、NFC、区分大小写。
function normalizeFragmentBasename(ref) {
  if (typeof ref !== "string" || !ref.trim()) return "";
  const name = ref.split("/").pop() || "";
  const base = name.endsWith(".md") ? name.slice(0, -3) : name;
  return base.normalize("NFC");
}

// 零命中 → none；同 basename 多候选 → conflict（不猜测、不展示按钮）。
function matchCandidate(candidates, basename) {
  const matched = (candidates || []).filter(
    (item) => normalizeFragmentBasename(item.fragment_ref) === basename
  );
  if (matched.length === 0) return { status: "none" };
  if (matched.length > 1) return { status: "conflict" };
  return { status: "matched", candidate: matched[0] };
}

// 已桥接判定：pilot run 的 fragment_ref basename 与卡片一致。
function pilotRunForFragment(runs, basename) {
  for (const run of runs || []) {
    if (run.graph_id !== PILOT_GRAPH_ID) continue;
    if (normalizeFragmentBasename(run.fragment_ref) === basename) return run;
  }
  return null;
}

// 资格三字段（缺一不可），返回诚实原因。
function candidateEligibility(candidate) {
  if (candidate.has_conflict !== false) {
    return { ok: false, reason: "候选存在冲突，需要先处理" };
  }
  if (candidate.content_status !== "pending_confirmation") {
    return { ok: false, reason: "候选不在待确认状态" };
  }
  if (candidate.evidence_level !== "unverified") {
    return { ok: false, reason: "候选证据等级不满足要求" };
  }
  return { ok: true };
}

function normalizeNoteRef(ref) {
  if (typeof ref !== "string" || !ref.trim()) return "";
  const value = ref.trim().normalize("NFC");
  return value.endsWith(".md") ? value : `${value}.md`;
}

function cardFragmentRefs(card) {
  // My Life Dataview 模板使用 data-path；早期测试夹具与 Obsidian 内链常见
  // data-href。逐个读取两者，避免依赖逗号选择器，也不从 onclick 猜路径。
  const anchors = card.querySelectorAll("a");
  let rawRef = "";
  let organizedRef = "";
  for (const anchor of anchors) {
    const label = (anchor.textContent || "").trim();
    const ref = normalizeNoteRef(
      anchor.getAttribute("data-path") || anchor.getAttribute("data-href") || ""
    );
    if (label === "原始记录") rawRef = ref;
    if (label === "查看整理结果") organizedRef = ref;
  }
  return {
    rawRef,
    organizedRef,
    basename: normalizeFragmentBasename(rawRef),
  };
}

function continuationForFragment(items, basename) {
  return (items || []).find((item) => item.fragment_id === basename) || null;
}

function alignmentForFragment(items, basename) {
  return (items || []).filter((item) => item.fragment_id === basename).sort((left, right) => {
    const time = String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
    return time || String(right.alignment_id || "").localeCompare(String(left.alignment_id || ""));
  })[0] || null;
}

function setCaptureStatus(card, text) {
  const status = card.querySelector(".life-capture-status");
  if (status) status.setText(text);
}

function closePilotDialog(doc) {
  doc.querySelector(`.${DIALOG_CLASS}`)?.remove();
}

// 确认页（线框 §5.2）：全部字段来自预案投影与候选安全字段。
function openPilotConfirmDialog(doc, plan, candidate, handlers) {
  closePilotDialog(doc);
  const overlay = doc.createElement("div");
  overlay.className = DIALOG_CLASS;
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "转为 Graph 工作流");
  const panel = overlay.createDiv({ cls: "graph-pilot-dialog" });
  const close = panel.createEl("button", { cls: "graph-pilot-close", text: "关闭" });
  close.setAttribute("type", "button");
  close.addEventListener("click", () => closePilotDialog(doc));
  panel.createEl("p", { cls: "life-source-label", text: "GRAPH · PILOT" });
  panel.createEl("h2", { text: "转为 Graph 工作流" });

  const facts = panel.createDiv({ cls: "graph-pilot-facts" });
  const lines = [
    `工作流：${plan.task_label}`,
    `预计节点：${plan.node_count} 个（${plan.node_flow}）`,
    `预期产出：${plan.expected_output}`,
    `人工闸门：${plan.human_gates} 道（调用前、调用后）`,
    `最大返修：${plan.max_feedback} 次`,
    `模型调用：总上限 ${plan.max_total_calls} 次（首次 + 最多一次返修）`,
    `最坏成本：¥${plan.cost_cap_cny}（价格快照见审计区）`,
    `写入范围：${plan.write_scope}`,
  ];
  for (const line of lines) {
    facts.createEl("p", { cls: "graph-pilot-fact", text: line });
  }
  panel.createEl("p", { cls: "graph-pilot-note", text: plan.create_behavior });

  const message = panel.createEl("p", { cls: "graph-pilot-message", text: "" });
  const actions = panel.createDiv({ cls: "graph-pilot-actions" });
  const cancel = actions.createEl("button", { cls: "graph-pilot-cancel", text: "取消" });
  cancel.setAttribute("type", "button");
  cancel.addEventListener("click", () => closePilotDialog(doc));
  const confirm = actions.createEl("button", {
    cls: "graph-pilot-confirm",
    text: "创建工作流",
  });
  confirm.setAttribute("type", "button");
  confirm.addEventListener("click", async () => {
    // 防双击：真实 DOM 的 disabled 不派发点击，但程序性 click 仍会进入——
    // 守卫必须在监听器内部（反例：双击只产生一个创建请求）。
    if (confirm.getAttribute("disabled") !== null) return;
    confirm.setAttribute("disabled", "disabled");
    cancel.setAttribute("disabled", "disabled");
    try {
      await handlers.onConfirm(candidate, (text) => message.setText(text));
    } finally {
      confirm.removeAttribute("disabled");
      cancel.removeAttribute("disabled");
    }
  });
  doc.body.appendChild(overlay);
}

// 碎片卡入口注入：幂等（fingerprint 不变跳过），独立失败域由调用方保证。
function injectHomepagePilotBridge(doc, state, handlers) {
  const cards = doc.querySelectorAll(
    ".my-life-homepage-view .life-capture-card"
  );
  let injected = 0;
  cards.forEach((card) => {
    const refs = cardFragmentRefs(card);
    const basename = refs.basename;
    if (!basename) return;
    const match = matchCandidate(state.candidates, basename);
    const bridged = pilotRunForFragment(state.runs, basename);
    const continuation = continuationForFragment(state.continuations, basename);
    const alignment = alignmentForFragment(state.alignments, basename);
    const loopApproved = refs.rawRef && typeof handlers.isLoopApproved === "function"
      ? handlers.isLoopApproved(refs.rawRef)
      : false;
    const fingerprint = [
      match.status,
      match.candidate ? match.candidate.candidate_id : "",
      match.candidate ? match.candidate.content_sha256 : "",
      match.candidate ? match.candidate.content_status : "",
      bridged ? bridged.run_id : "",
      bridged ? bridged.status : "",
      refs.organizedRef,
      loopApproved ? "approved" : "not-approved",
      continuation ? continuation.stage : "",
      continuation ? continuation.sequence : "",
      state.alignmentAvailable ? "alignment-on" : "alignment-off",
      alignment ? alignment.status : "",
      alignment ? alignment.route || "" : "",
      state.error || "",
      state.continuationError || "",
    ].join("|");
    const existing = card.querySelector(`[${BRIDGE_ATTR}]`);
    if (existing && existing.getAttribute(BRIDGE_ATTR) === fingerprint) return;
    existing?.remove();

    // 新的共享确认层启用后，Graph 入口只在明确选择/升级为 Graph 时出现。
    // 已存在的历史 Run 永远保留入口；服务尚未启用时维持旧产品行为。
    if (state.alignmentAvailable && !bridged) {
      return;
    }

    const slot = doc.createElement("div");
    slot.addClass("graph-pilot-entry");
    slot.setAttribute(BRIDGE_ATTR, fingerprint);
    if (bridged) {
      setCaptureStatus(card, "已进入 Graph · 等待你决定");
      const open = slot.createEl("button", {
        cls: "graph-pilot-open",
        text: "已进入 Graph 工作流 →",
      });
      open.setAttribute("type", "button");
      open.addEventListener("click", () => handlers.onOpenRun(bridged.run_id));
    } else if (match.status === "matched" && state.error) {
      slot.createEl("small", {
        cls: "graph-pilot-note-muted",
        text: `Graph 工作流暂不可用：${state.error}`,
      });
    } else if (match.status === "matched") {
      setCaptureStatus(card, "Loop 候选已就绪");
      const eligibility = candidateEligibility(match.candidate);
      if (!eligibility.ok) {
        slot.createEl("small", {
          cls: "graph-pilot-note-muted",
          text: `候选尚未就绪：${eligibility.reason}`,
        });
      } else {
        const button = slot.createEl("button", {
          cls: "graph-pilot-convert",
          text: "转为 Graph 工作流",
        });
        button.setAttribute("type", "button");
        button.addEventListener("click", () => handlers.onStartConvert(match.candidate));
      }
    } else if (match.status === "conflict") {
      setCaptureStatus(card, "Loop 候选存在来源冲突");
      slot.createEl("small", {
        cls: "graph-pilot-note-muted",
        text: "来源绑定冲突：同一碎片对应多个候选，请先在认知确认看板处理。",
      });
    } else if (!refs.organizedRef) {
      // n8n 尚未产出整理结果：保留主页原有的等待整理语义。
      slot.remove();
      return;
    } else if (!loopApproved) {
      setCaptureStatus(card, "主页整理已完成 · 未签名，不进入 Loop");
      slot.createEl("small", {
        cls: "graph-pilot-note-muted",
        text: "这条碎片没有勾选进入 Loop；整理结果会保留，但不会自动接续。",
      });
    } else if (state.continuationError) {
      setCaptureStatus(card, "主页整理已完成 · Loop 状态暂不可读");
      slot.createEl("small", {
        cls: "graph-pilot-note-muted",
        text: `Loop 接续状态暂不可用：${state.continuationError}`,
      });
    } else if (!continuation) {
      setCaptureStatus(card, "主页整理已完成 · 等待 Loop 登记");
      slot.createEl("small", {
        cls: "graph-pilot-note-muted",
        text: "已签名，但 Loop 尚未登记这条碎片。",
      });
    } else if (["loop_registered", "loop_processing", "loop_stopped"].includes(continuation.stage)) {
      const stopped = continuation.stage === "loop_stopped";
      setCaptureStatus(
        card,
        stopped
          ? `Loop 接续受阻 · Checkpoint #${continuation.sequence}`
          : `Loop 已登记 · Checkpoint #${continuation.sequence} · 等待接续`
      );
      const message = slot.createEl("small", {
        cls: stopped ? "graph-continuation-message is-error" : "graph-continuation-message",
        text: stopped ? "上次接续未完成，可安全重试。" : "整理结果已就绪，可以继续生成待确认候选。",
      });
      const button = slot.createEl("button", {
        cls: "graph-continuation-start",
        text: stopped ? "安全重试接续" : "接续到 Loop（研究）",
      });
      button.setAttribute("type", "button");
      button.addEventListener("click", async () => {
        if (button.getAttribute("disabled") !== null) return;
        button.setAttribute("disabled", "disabled");
        message.setText("正在接续，模型调用为 0…");
        try {
          await handlers.onContinueResearch(refs, (text) => message.setText(text));
        } finally {
          button.removeAttribute("disabled");
        }
      });
    } else if (continuation.stage === "candidate_source_ready") {
      setCaptureStatus(card, `Loop 已完成 · Checkpoint #${continuation.sequence}`);
      slot.createEl("small", {
        cls: "graph-pilot-note-muted",
        text: "候选已经生成，正在等待主页读取最新投影。",
      });
    }
    const footer = card.querySelector("footer");
    if (footer && typeof footer.insertAdjacentElement === "function") {
      footer.insertAdjacentElement("afterend", slot);
    } else {
      card.appendChild(slot);
    }
    injected += 1;
  });
  return injected;
}

module.exports = {
  PILOT_GRAPH_ID,
  BRIDGE_ATTR,
  DIALOG_CLASS,
  normalizeFragmentBasename,
  normalizeNoteRef,
  cardFragmentRefs,
  continuationForFragment,
  alignmentForFragment,
  matchCandidate,
  pilotRunForFragment,
  candidateEligibility,
  openPilotConfirmDialog,
  closePilotDialog,
  injectHomepagePilotBridge,
};
