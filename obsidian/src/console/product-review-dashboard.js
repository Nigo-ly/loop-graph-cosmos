"use strict";

const DASHBOARD_CLASS = "life-loop-review-dashboard";
const DIALOG_CLASS = "life-loop-review-dialog-overlay";
const STATUS_ORDER = [
  "pending_confirmation", "conflict", "kept_draft",
  "published_asset", "withdrawn_asset", "rejected",
];
const STATUS_META = {
  pending_confirmation: { label: "待你确认", tone: "attention" },
  kept_draft: { label: "继续保留草稿", tone: "quiet" },
  published_asset: { label: "已形成资产", tone: "success" },
  rejected: { label: "已拒绝", tone: "muted" },
  withdrawn_asset: { label: "已撤回", tone: "warning" },
  conflict: { label: "需要处理冲突", tone: "danger" },
};

function summarizeReviews(items) {
  const counts = Object.fromEntries(Object.keys(STATUS_META).map((key) => [key, 0]));
  for (const item of items) counts[item.content_status] += 1;
  const sorted = [...items].sort((left, right) => {
    const status = STATUS_ORDER.indexOf(left.content_status) - STATUS_ORDER.indexOf(right.content_status);
    if (status !== 0) return status;
    return String(right.updated_at || "").localeCompare(String(left.updated_at || ""));
  });
  return { counts, items: sorted, total: items.length };
}

function closeDialog(doc) {
  doc.querySelector(`.${DIALOG_CLASS}`)?.remove();
}

function actionButton(container, label, action, detail, client, selectedCards, handlers) {
  const button = container.createEl("button", { cls: `life-loop-review-action is-${action}`, text: label });
  button.setAttribute("type", "button");
  button.setAttribute("data-review-action", action);
  button.addEventListener("click", async () => {
    const cards = action === "confirm_asset" ? [...selectedCards] : detail.selected_card_ids || [];
    if (action === "confirm_asset") {
      if (!cards.length) {
        handlers.setMessage("至少选择一张候选知识卡后才能确认形成资产。", true);
        return;
      }
      const cardNames = detail.candidate_cards
        .filter((card) => cards.includes(card.card_id))
        .map((card) => card.title || card.card_id)
        .join("、");
      const prompt = [
        `确认形成知识资产：${detail.title}`,
        `内容摘要：${detail.content_sha256.slice(0, 16)}…`,
        `知识卡：${cardNames}`,
        "证据等级仍为 unverified，不代表事实已经验证。",
      ].join("\n");
      const confirmAction = handlers.confirmAction || ((text) => window.confirm(text));
      if (!confirmAction(prompt)) return;
    }
    button.disabled = true;
    try {
      const outcome = await client.submit(detail, action, cards);
      handlers.setMessage("决定已保存，主页状态正在刷新。", false);
      if (action === "create_edit_copy" && outcome.review_path) {
        await handlers.openNote(outcome.review_path);
      }
      await handlers.onChanged();
      closeDialog(handlers.doc);
    } catch (error) {
      handlers.setMessage(error && error.message ? error.message : "操作失败，请刷新后重试。", true);
      button.disabled = false;
    }
  });
  return button;
}

async function openReviewDialog(doc, item, client, handlers) {
  closeDialog(doc);
  const overlay = doc.createElement("div");
  overlay.className = DIALOG_CLASS;
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "Loop 人工确认");
  const panel = overlay.createDiv({ cls: "life-loop-review-dialog" });
  const close = panel.createEl("button", { cls: "life-loop-review-close", text: "关闭" });
  close.setAttribute("type", "button");
  close.addEventListener("click", () => closeDialog(doc));
  panel.createEl("p", { cls: "life-source-label", text: "LOOP · HUMAN REVIEW" });
  panel.createEl("h2", { text: item.title });
  const loading = panel.createEl("p", { cls: "life-loop-review-message", text: "正在读取候选详情…" });
  doc.body.appendChild(overlay);

  let detail;
  try {
    detail = await client.detail(item.candidate_id);
  } catch (error) {
    loading.setText(error && error.message ? error.message : "无法读取候选详情");
    loading.addClass("is-error");
    return;
  }
  loading.remove();
  const status = STATUS_META[detail.content_status];
  const badges = panel.createDiv({ cls: "life-loop-review-badges" });
  badges.createSpan({ cls: `is-${status.tone}`, text: status.label });
  badges.createSpan({ text: "证据：未验证" });
  badges.createSpan({ text: `修订 ${detail.revision}` });
  panel.createEl("p", { cls: "life-loop-review-core", text: detail.core_judgment || "打开完整候选查看核心判断。" });
  if (detail.user_value) panel.createEl("p", { cls: "life-loop-review-value", text: `对你的价值：${detail.user_value}` });

  const noteButton = panel.createEl("button", { cls: "life-loop-review-open-note", text: "打开完整候选" });
  noteButton.setAttribute("type", "button");
  noteButton.addEventListener("click", () => handlers.openNote(detail.note_path));

  const selectedCards = new Set();
  if (detail.candidate_cards.length) {
    panel.createEl("h3", { text: "选择要形成资产的知识卡" });
    const cards = panel.createDiv({ cls: "life-loop-review-cards" });
    for (const card of detail.candidate_cards) {
      const row = cards.createEl("label", { cls: "life-loop-review-card" });
      const input = row.createEl("input");
      input.setAttribute("type", "checkbox");
      input.setAttribute("value", card.card_id);
      input.addEventListener("change", () => {
        if (input.checked) selectedCards.add(card.card_id);
        else selectedCards.delete(card.card_id);
      });
      const text = row.createDiv();
      text.createEl("strong", { text: card.title || card.card_id });
      text.createEl("p", { text: card.knowledge || "" });
    }
  }

  const message = panel.createEl("p", { cls: "life-loop-review-message", text: "所有决定只改变派生状态，不覆盖原始碎片和旧草稿。" });
  const setMessage = (text, error) => {
    message.setText(text);
    message.toggleClass("is-error", Boolean(error));
  };
  const actions = panel.createDiv({ cls: "life-loop-review-actions" });
  const labels = {
    confirm_asset: "确认形成资产",
    create_edit_copy: "创建编辑副本",
    keep_draft: "继续保留草稿",
    reject: "拒绝这份候选",
    withdraw_asset: "撤回已发布资产",
    reopen: "重新进入待确认",
  };
  for (const action of detail.available_actions) {
    if (!labels[action]) continue;
    actionButton(actions, labels[action], action, detail, client, selectedCards, {
      ...handlers, doc, setMessage,
    });
  }
}

function buildDashboardSection(doc, container, dashboard, handlers) {
  const section = doc.createElement("section");
  section.className = `life-panel ${DASHBOARD_CLASS}`;
  section.setAttribute("data-loop-review-schema", "fragment-cognitive-product-review-v1");
  section.setAttribute("data-loop-review-fingerprint", dashboard.fingerprint);
  const heading = section.createDiv({ cls: "life-panel-heading" });
  const headingText = heading.createDiv();
  headingText.createEl("span", { cls: "life-source-label", text: "LOOP · REVIEW · ASSETS" });
  headingText.createEl("h2", { text: "认知确认看板" });
  heading.createEl("span", { cls: "life-panel-index", text: `${dashboard.total} 条候选` });
  section.createEl("p", {
    cls: "life-loop-review-intro",
    text: "这里集中显示需要你决定的认知草稿。颜色和文字共同表达状态，点击即可查看、确认、保留或撤回。",
  });
  if (handlers.errorMessage) {
    section.createEl("p", {
      cls: "life-loop-review-service-error",
      text: `人工确认服务暂不可用：${handlers.errorMessage}`,
    });
  }

  const tiles = section.createDiv({ cls: "life-loop-review-tiles" });
  const visibleStatuses = [
    "pending_confirmation", "published_asset", "kept_draft", "withdrawn_asset", "conflict",
  ];
  for (const key of visibleStatuses) {
    const meta = STATUS_META[key];
    const tile = tiles.createEl("button", { cls: `life-loop-review-tile is-${meta.tone}` });
    tile.setAttribute("type", "button");
    tile.setAttribute("data-review-status", key);
    tile.createSpan({ cls: "life-loop-review-tile-count", text: String(dashboard.counts[key]) });
    tile.createSpan({ text: meta.label });
    tile.addEventListener("click", () => {
      const row = section.querySelector(`[data-review-row-status="${key}"]`);
      if (row && typeof row.focus === "function") row.focus();
    });
  }

  const list = section.createDiv({ cls: "life-loop-review-list" });
  if (!dashboard.items.length) {
    list.createEl("p", { cls: "life-loop-review-empty", text: "当前没有认知候选。" });
  }
  for (const item of dashboard.items.slice(0, 8)) {
    const meta = STATUS_META[item.content_status];
    const row = list.createEl("button", { cls: `life-loop-review-row is-${meta.tone}` });
    row.setAttribute("type", "button");
    row.setAttribute("data-review-candidate", item.candidate_id);
    row.setAttribute("data-review-row-status", item.content_status);
    const text = row.createDiv({ cls: "life-loop-review-row-main" });
    text.createEl("strong", { text: item.title });
    text.createEl("span", { text: item.core_judgment || "查看候选判断与知识卡" });
    row.createEl("span", { cls: `life-loop-review-status is-${meta.tone}`, text: meta.label });
    row.addEventListener("click", () => handlers.openReview(item));
  }

  const fragments = container.querySelector(".life-fragments");
  if (fragments && typeof fragments.insertAdjacentElement === "function") {
    fragments.insertAdjacentElement("afterend", section);
  } else {
    container.appendChild(section);
  }
  return 1;
}

function injectHomepageReviewDashboard(doc, items, handlers) {
  const dashboard = summarizeReviews(items);
  dashboard.fingerprint = dashboard.items
    .map((item) => `${item.candidate_id}:${item.content_status}:${item.revision}`)
    .concat(handlers.errorMessage || "")
    .join("|");
  const containers = doc.querySelectorAll(".my-life-homepage-view .life-dashboard-content");
  let injected = 0;
  containers.forEach((container) => {
    const existing = container.querySelector(`.${DASHBOARD_CLASS}`);
    if (existing && existing.getAttribute("data-loop-review-fingerprint") === dashboard.fingerprint) {
      return;
    }
    existing?.remove();
    injected += buildDashboardSection(doc, container, dashboard, handlers);
  });
  return injected;
}

module.exports = {
  DASHBOARD_CLASS,
  STATUS_META,
  summarizeReviews,
  injectHomepageReviewDashboard,
  openReviewDialog,
};
