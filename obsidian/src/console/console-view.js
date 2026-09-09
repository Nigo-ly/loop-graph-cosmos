"use strict";

const { ItemView } = require("obsidian");
const { renderConsole } = require("./console-dom.js");
const model = require("./view-model.js");
const {
  isValidKeepThoughtCategory,
} = require("./cognitive-decision-client.js");

const LOOP_CONSOLE_VIEW_TYPE = "my-life-loop-console";

class LoopConsoleView extends ItemView {
  constructor(
    leaf,
    client,
    controlClient,
    proposalClient,
    reviewClient,
    minimumValueClient,
    cognitiveDecisionClient,
    options
  ) {
    super(leaf);
    this.client = client;
    this.controlClient = controlClient || null;
    this.proposalClient = proposalClient || null;
    this.reviewClient = reviewClient || null;
    this.minimumValueClient = minimumValueClient || null;
    this.cognitiveDecisionClient = cognitiveDecisionClient || null;
    // R2 §4.6：来源级互斥锁（队列卡与原位入口共享）——review 提交前检查。
    this.sharedAuthLocks = (options && options.sharedAuthLocks) || null;
    // Monotonic selection generation: every async detail/control write must
    // re-verify both the generation and selectedRunId after each await, so
    // a slow response can never bind detail or controls to a stale run.
    this.selectionGeneration = 0;
    this.dialogTrigger = null;
    this.proposalBackTrigger = null;
    // Scroll position survives re-renders (refresh never yanks the page)
    // and is restored when the view is reopened from the homepage.
    this.savedScrollTop = 0;
    this.state = {
      status: "loading",
      error: null,
      health: null,
      queue: [],
      queueStale: false,
      runs: [],
      runsStale: false,
      statuses: [],
      attention: [],
      runsTotal: 0,
      filter: { status: null, statusLabel: null },
      selectedRunId: null,
      detail: null,
      detailLoading: false,
      detailError: null,
      detailStale: false,
      ui: { queueOpen: false, runsOpen: false, proposalHistoryOpen: false },
      // P3B read-only shadow proposals: an independent sub-state so a shadow
      // projection outage never masks (or fakes) the main read-only data.
      proposals: {
        status: this.proposalClient ? "idle" : "unavailable",
        error: null,
        ledgerPresent: true,
        current: [],
        history: [],
        stale: false,
        selectedProposalId: null,
        detail: null,
        detailLoading: false,
        detailError: null,
        detailRaw: null,
        comparison: null,
      },
      // P3C review bookkeeping for the selected proposal: an independent
      // sub-state so a 5682 outage never hides confirmed receipts or the
      // read-only proposal data above.
      review: {
        status: "idle",
        actions: null,
        history: [],
        dialog: null,
        submitting: false,
        submitError: null,
        lastReceipt: null,
        duplicate: false,
      },
      control: {
        status: "idle",
        actions: null,
        error: null,
        dialog: null,
        submitting: false,
        submitError: null,
        lastReceipt: null,
        duplicate: false,
        intents: [],
      },
      minimumValue: {
        submitting: false,
        error: null,
        lastDecision: null,
      },
      cognitiveDecision: {
        submitting: false,
        error: null,
        lastDecision: null,
        thoughtCategory: "",
      },
    };
    this.handlers = {
      onRefresh: () => this.refresh(),
      onFilterChange: (status) => this.setFilter(status),
      onSelectRun: (runId) => this.selectRun(runId),
      onBack: () => this.clearSelection(),
      onOpenDialog: (action) => this.openDialog(action),
      onDialogCancel: () => this.closeDialog(),
      onDialogConfirm: (priority) => this.confirmDialog(priority),
      onRefreshControl: () => this.loadControl(this.state.selectedRunId),
      onToggleSection: (section, open) => this.toggleSection(section, open),
      onSelectProposal: (proposalId) => this.selectProposal(proposalId),
      onBackProposal: () => this.clearProposalSelection(),
      onRefreshProposals: () => this.loadProposals(),
      onOpenReviewDialog: () => this.openReviewDialog(),
      onReviewDialogCancel: () => this.closeReviewDialog(),
      onReviewChoose: (decision) => this.chooseReviewDecision(decision),
      onReviewConfirm: (fields) => this.submitReviewDialog(fields),
      onRefreshReview: () => this.loadReview(this.state.proposals.selectedProposalId),
      onMinimumValueConsent: () => this.submitMinimumValueDecision("consent"),
      onMinimumValueDecline: () => this.submitMinimumValueDecision("decline"),
      onCognitiveDraftDecision: (decision) =>
        this.submitCognitiveDraftDecision(decision),
      onCognitiveWithdraw: () => this.submitCognitiveWithdrawal(),
      onCognitiveCategoryChange: (value) =>
        this.setCognitiveThoughtCategory(value),
    };
  }

  getViewType() {
    return LOOP_CONSOLE_VIEW_TYPE;
  }

  getDisplayText() {
    return "Loop 控制台";
  }

  getIcon() {
    return "activity";
  }

  async onOpen() {
    this.render();
    await this.refresh();
  }

  async onClose() {
    this.contentEl.empty();
  }

  findConsoleScroller() {
    // The real scroll container differs by layout: usually the view root
    // (`.my-life-loop-console-view` has overflow-y: auto), but when the
    // parent constrains the height, the parent scrolls instead. Pick the
    // first candidate that actually overflows; fall back to the view root.
    const candidates = [
      this.contentEl,
      this.contentEl && this.contentEl.parentElement ? this.contentEl.parentElement : null,
    ];
    for (const el of candidates) {
      if (
        el &&
        typeof el.scrollHeight === "number" &&
        typeof el.clientHeight === "number" &&
        el.scrollHeight > el.clientHeight + 1
      ) {
        return el;
      }
    }
    return this.contentEl;
  }

  render() {
    // G7：刷新聚合——batchRender 期间跳过中间渲染（detail/proposals 各自的
    // loading 态不再逐段刷屏），refresh 末尾统一一次 render 落到最终态。
    if (this.batchRender) return;
    // Preserve the scroll position across the rebuild: renderConsole empties
    // the tree, and a refresh must never yank nigo back to the top.
    const scroller = this.findConsoleScroller();
    const previousTop =
      scroller && typeof scroller.scrollTop === "number" ? scroller.scrollTop : this.savedScrollTop;
    renderConsole(this.contentEl, this.state, this.handlers);
    const after = this.findConsoleScroller();
    if (after && typeof after.scrollTop === "number") {
      after.scrollTop = previousTop;
    }
    this.savedScrollTop = previousTop;
  }

  async refresh() {
    this.selectionGeneration += 1;
    this.state.status = "loading";
    this.state.error = null;
    this.render();
    try {
      const healthBody = await this.client.health();
      const [queueBody, runsBody] = await Promise.all([this.client.queue(), this.client.runs({ limit: 200 })]);
      this.state.health = model.healthSummary(healthBody);
      this.state.queue = model.queueRows(queueBody);
      this.state.queueStale = queueBody.stale === true;
      this.state.statuses = model.statusFilterOptions(runsBody);
      this.state.runs = model.runRows(runsBody, this.state.filter);
      this.state.runsStale = runsBody.stale === true;
      this.state.runsTotal = Array.isArray(runsBody.items) ? runsBody.items.length : 0;
      this.state.attention = model.attentionRows(runsBody);
      this.state.status = "ready";
      this.state.error = null;
    } catch (error) {
      this.state.error = error && error.kind ? error : { kind: "unreachable", details: {} };
      this.state.status = model.consoleStatus({ loading: false, error: this.state.error });
    }
    // G7：刷新聚合——detail（选中 run）与 proposals 并行静默加载，
    // 期间跳过中间渲染，全部完成后统一一次 render（不逐段闪断）。
    this.batchRender = true;
    try {
      if (this.state.selectedRunId) await this.selectRun(this.state.selectedRunId);
      await this.loadProposals();
    } finally {
      this.batchRender = false;
      this.render();
    }
  }

  async loadProposals() {
    const proposals = this.state.proposals;
    if (!this.proposalClient) {
      proposals.status = "unavailable";
      this.render();
      return;
    }
    const generation = this.selectionGeneration;
    const stillCurrent = () => generation === this.selectionGeneration;
    proposals.status = "loading";
    proposals.error = null;
    this.render();
    try {
      const body = await this.proposalClient.proposals({ scope: "all", limit: 200 });
      if (!stillCurrent()) return;
      proposals.ledgerPresent = body.ledger_present === true;
      proposals.stale = body.stale === true;
      proposals.current = model.proposalCurrentRows(body.items);
      proposals.history = model.proposalHistoryRows(body.items);
      proposals.status = "ready";
      proposals.error = null;
    } catch (error) {
      if (!stillCurrent()) return;
      proposals.status = error && error.kind === "unreachable" ? "unreachable" : "error";
      proposals.error = error && error.kind ? error : { kind: "unreachable", details: {} };
    }
    if (!stillCurrent()) return;
    this.render();
    // Keep a selected proposal's detail honest across refreshes.
    if (proposals.selectedProposalId) await this.selectProposal(proposals.selectedProposalId);
  }

  async selectProposal(proposalId) {
    if (!proposalId || !this.proposalClient) return;
    const generation = ++this.selectionGeneration;
    const proposals = this.state.proposals;
    // A confirmed review receipt survives a re-selection of the SAME
    // proposal (e.g. refresh): immutable proof is never wiped by a reload.
    const retainedReceipt =
      proposals.selectedProposalId === proposalId && this.state.review
        ? this.state.review.lastReceipt
        : null;
    const retainedDuplicate =
      proposals.selectedProposalId === proposalId && this.state.review
        ? this.state.review.duplicate
        : false;
    proposals.selectedProposalId = proposalId;
    proposals.detailLoading = true;
    proposals.detailError = null;
    proposals.detailRaw = null;
    proposals.comparison = null;
    this.resetReview();
    this.state.review.lastReceipt = retainedReceipt;
    this.state.review.duplicate = retainedDuplicate;
    // A proposal selection replaces the run selection in the right column.
    this.state.selectedRunId = null;
    this.state.detail = null;
    this.resetControl();
    this.resetMinimumValue();
    this.resetCognitiveDecision();
    this.render();
    const stillCurrent = () =>
      generation === this.selectionGeneration && proposals.selectedProposalId === proposalId;
    try {
      const body = await this.proposalClient.proposalDetail(proposalId);
      if (!stillCurrent()) return;
      proposals.detail = model.proposalDetailModel(body.proposal);
      proposals.detailRaw = body.proposal || null;
      if (!proposals.detail) proposals.detailError = "影子建议服务返回了空的建议详情";
    } catch (error) {
      if (!stillCurrent()) return;
      proposals.detail = null;
      proposals.detailError = error && error.message ? error.message : "建议详情读取失败";
    }
    if (!stillCurrent()) return;
    proposals.detailLoading = false;
    this.render();
    await this.loadProposalComparison(proposalId, generation);
    if (stillCurrent()) await this.loadReview(proposalId, generation);
  }

  async loadProposalComparison(proposalId, generation) {
    const proposals = this.state.proposals;
    proposals.comparison = null;
    const raw = proposals.detailRaw;
    const previousId = raw && raw.supersedes_proposal_id ? String(raw.supersedes_proposal_id) : null;
    if (!previousId || !this.proposalClient) return;
    const stillCurrent = () =>
      generation === this.selectionGeneration && proposals.selectedProposalId === proposalId;
    try {
      const body = await this.proposalClient.proposalDetail(previousId);
      if (!stillCurrent()) return;
      proposals.comparison = {
        previousId,
        rows: model.proposalComparison(raw, body.proposal),
      };
      this.render();
    } catch {
      // G3：对比失败与「无对比」必须可区分——failed 状态位 + 可重试；
      // 缺失前置建议只丢对比，绝不丢详情。
      if (!stillCurrent()) return;
      proposals.comparison = { previousId, rows: [], failed: true };
      this.render();
    }
  }

  resetReview() {
    this.state.review = {
      status: this.reviewClient ? "idle" : "unavailable",
      actions: null,
      history: [],
      dialog: null,
      submitting: false,
      submitError: null,
      lastReceipt: null,
      duplicate: false,
    };
  }

  async loadReview(proposalId, generation) {
    const review = this.state.review;
    if (!proposalId || !this.reviewClient) {
      review.status = "unavailable";
      this.render();
      return;
    }
    const gen = typeof generation === "number" ? generation : this.selectionGeneration;
    const stillCurrent = () =>
      gen === this.selectionGeneration && this.state.proposals.selectedProposalId === proposalId;
    const retained = review.lastReceipt;
    review.status = "loading";
    this.render();
    try {
      const [actionsBody, historyBody] = await Promise.all([
        this.reviewClient.actionsFor(proposalId),
        this.reviewClient.decisionsFor(proposalId, 20),
      ]);
      if (!stillCurrent()) return;
      review.actions = model.reviewActionsView(actionsBody);
      review.history = model.reviewHistoryView(historyBody);
      review.status = "ready";
      review.lastReceipt = retained;
    } catch (error) {
      if (!stillCurrent()) return;
      review.status = error && error.kind === "unreachable" ? "unreachable" : "error";
      review.lastReceipt = retained;
    }
    if (!stillCurrent()) return;
    this.render();
  }

  openReviewDialog() {
    const review = this.state.review;
    if (review.status !== "ready" || !review.actions || !review.actions.submittable) return;
    review.dialog = {
      step: "choose",
      decision: null,
      // Changing an existing decision must warn that history is preserved.
      warning: review.actions.currentDecision ? "将保留此前记录" : null,
    };
    review.submitError = null;
    this.render();
    this.focusReviewDialogFirst();
  }

  chooseReviewDecision(decision) {
    const review = this.state.review;
    if (!review.dialog) return;
    review.dialog = { ...review.dialog, step: "form", decision };
    this.render();
    this.focusReviewDialogFirst();
  }

  closeReviewDialog() {
    this.state.review.dialog = null;
    this.render();
    this.restoreReviewFocus();
  }

  restoreReviewFocus() {
    if (!this.contentEl || typeof this.contentEl.querySelector !== "function") return;
    const el = this.contentEl.querySelector(".lc-review-open");
    if (el && typeof el.focus === "function") el.focus({ preventScroll: true });
  }

  focusReviewDialogFirst() {
    if (!this.contentEl || typeof this.contentEl.querySelector !== "function") return;
    const el = this.contentEl.querySelector(".lc-review-dialog .lc-dialog-confirm")
      || this.contentEl.querySelector(".lc-review-dialog button");
    if (el && typeof el.focus === "function") el.focus({ preventScroll: true });
  }

  async submitReviewDialog(fields) {
    const review = this.state.review;
    const dialog = review.dialog;
    const actions = review.actions;
    if (!dialog || !actions || review.submitting) return;
    // R2 §4.6：队列卡同一审核项在途 → 原位入口被拒（共享来源级互斥锁）。
    if (this.sharedAuthLocks && this.sharedAuthLocks.isPending(`loop-review:${dialog.proposalId}`)) {
      review.submitError = "该审核项正在其他入口处理中，请稍候。";
      this.render();
      return;
    }
    review.submitting = true;
    review.submitError = null;
    this.render();
    try {
      const result = await this.reviewClient.submitDecision({
        proposalId: actions.proposalId,
        decision: dialog.decision,
        reason: fields && fields.reason ? fields.reason : null,
        resumeCondition: fields && fields.resumeCondition ? fields.resumeCondition : null,
        note: fields && fields.note ? fields.note : null,
        expectedFingerprint: actions.expectedFingerprint,
        expectedSourceSequence: actions.expectedSourceSequence,
        expectedProposalStatus: actions.expectedProposalStatus,
        expectedCurrentDecisionId: actions.currentDecision ? actions.currentDecision.decisionId : null,
      });
      review.lastReceipt = model.reviewReceiptView(result.receipt);
      review.duplicate = result.httpStatus === 200;
      review.dialog = null;
      review.submitting = false;
      this.render();
      this.restoreReviewFocus();
      await this.loadReview(actions.proposalId);
    } catch (error) {
      review.submitting = false;
      review.dialog = null;
      review.duplicate = false;
      if (error && error.kind === "http_error") {
        const code = error.details && error.details.code ? error.details.code : null;
        review.submitError = {
          code,
          text: model.reviewReasonText(code) || "审核服务返回错误，决定未提交",
        };
      } else if (error && error.kind === "invalid_arguments") {
        review.submitError = { code: "missing_reason", text: error.message };
      } else {
        review.submitError = { code: "unreachable", text: "影子建议审核服务不可用，决定未提交" };
      }
      this.render();
      this.restoreReviewFocus();
    }
  }

  clearProposalSelection() {
    this.selectionGeneration += 1;
    const proposals = this.state.proposals;
    proposals.selectedProposalId = null;
    proposals.detail = null;
    proposals.detailError = null;
    proposals.detailLoading = false;
    proposals.detailRaw = null;
    proposals.comparison = null;
    this.resetReview();
    this.render();
    this.restoreProposalFocus();
  }

  restoreProposalFocus() {
    if (!this.contentEl || typeof this.contentEl.querySelectorAll !== "function") return;
    // Stable selector, not a captured node: render() rebuilds the tree.
    const cards = Array.from(this.contentEl.querySelectorAll(".lc-proposal-card"));
    const target = cards.find((el) => el.getAttribute("data-selected") === "true");
    const fallback = cards[0] || this.contentEl.querySelector(".lc-refresh");
    const node = target || fallback;
    if (node && typeof node.focus === "function") node.focus({ preventScroll: true });
  }

  async setFilter(status) {
    this.selectionGeneration += 1;
    this.state.filter = { status: status || null, statusLabel: model.statusLabel(status) };
    if (this.state.status !== "ready") return;
    this.state.status = "loading";
    this.render();
    try {
      const runsBody = await this.client.runs({ status: this.state.filter.status, limit: 200 });
      // The secondary all-runs filter never alters the primary attention
      // surface or the true total; those refresh only on unfiltered reads.
      if (!this.state.filter.status) {
        this.state.statuses = model.statusFilterOptions(runsBody);
        this.state.attention = model.attentionRows(runsBody);
        this.state.runsTotal = Array.isArray(runsBody.items) ? runsBody.items.length : 0;
      }
      this.state.runs = model.runRows(runsBody, this.state.filter);
      this.state.runsStale = runsBody.stale === true;
      this.state.status = "ready";
      this.state.error = null;
    } catch (error) {
      this.state.error = error && error.kind ? error : { kind: "unreachable", details: {} };
      this.state.status = model.consoleStatus({ loading: false, error: this.state.error });
    }
    this.render();
  }

  async selectRun(runId) {
    if (!runId) return;
    const retainedIntents =
      this.state.selectedRunId === runId && this.state.control
        ? this.state.control.intents
        : [];
    const generation = ++this.selectionGeneration;
    this.state.selectedRunId = runId;
    // A run selection replaces any proposal selection in the right column.
    this.state.proposals.selectedProposalId = null;
    this.state.proposals.detail = null;
    this.state.proposals.detailError = null;
    this.state.proposals.detailLoading = false;
    this.state.detailLoading = true;
    this.state.detailError = null;
    this.resetControl();
    this.resetMinimumValue();
    this.resetCognitiveDecision();
    this.state.control.intents = retainedIntents;
    this.render();
    const stillCurrent = () =>
      generation === this.selectionGeneration && this.state.selectedRunId === runId;
    try {
      const detailBody = await this.client.runDetail(runId);
      if (!stillCurrent()) return;
      this.state.detail = model.detailModel(detailBody);
      this.state.detailStale = detailBody.stale === true;
      if (!this.state.detail) this.state.detailError = "数据源返回了空的运行详情";
    } catch (error) {
      if (!stillCurrent()) return;
      this.state.detail = null;
      this.state.detailError = error && error.message ? error.message : "运行详情读取失败";
    }
    if (!stillCurrent()) return;
    this.state.detailLoading = false;
    this.render();
    if (this.state.detail) await this.loadControl(runId, generation);
  }

  clearSelection() {
    this.selectionGeneration += 1;
    this.state.selectedRunId = null;
    this.state.detail = null;
    this.state.detailError = null;
    this.state.detailLoading = false;
    this.resetControl();
    this.resetMinimumValue();
    this.resetCognitiveDecision();
    this.render();
  }

  resetControl() {
    this.state.control = {
      status: this.controlClient ? "idle" : "unavailable",
      actions: null,
      error: null,
      dialog: null,
      submitting: false,
      submitError: null,
      lastReceipt: null,
      duplicate: false,
      intents: [],
    };
  }

  resetMinimumValue() {
    this.state.minimumValue = {
      submitting: false,
      error: null,
      lastDecision: null,
    };
  }

  resetCognitiveDecision() {
    this.state.cognitiveDecision = {
      submitting: false,
      error: null,
      lastDecision: null,
      thoughtCategory: "",
    };
  }

  setCognitiveThoughtCategory(value) {
    // The DOM input listener syncs the keep button locally on each keystroke;
    // this only records the raw value so a re-render never steals focus.
    this.state.cognitiveDecision.thoughtCategory =
      typeof value === "string" ? value : "";
  }

  async submitCognitiveDraftDecision(decision) {
    const selected = this.state.detail && this.state.detail.cognitiveDraft;
    if (
      !this.cognitiveDecisionClient ||
      !selected ||
      !selected.awaitingDecision ||
      this.state.cognitiveDecision.submitting
    ) {
      return;
    }
    const thoughtCategory =
      decision === "keep_draft"
        ? this.state.cognitiveDecision.thoughtCategory.trim()
        : "";
    if (decision === "keep_draft" && !isValidKeepThoughtCategory(thoughtCategory)) {
      return;
    }
    const generation = this.selectionGeneration;
    const runId = selected.runId;
    const stillCurrent = () =>
      generation === this.selectionGeneration && this.state.selectedRunId === runId;
    this.state.cognitiveDecision.submitting = true;
    this.state.cognitiveDecision.error = null;
    this.render();
    try {
      const result = await this.cognitiveDecisionClient.submitDecision({
        decision,
        runId: selected.runId,
        fragmentId: selected.fragmentId,
        markdown: selected.markdown,
        expectedSequence: selected.expectedSequence,
        thoughtCategory,
      });
      if (!stillCurrent()) return;
      const receipt = {
        ...result.decision,
        thoughtCategory,
      };
      if (this.client && typeof this.client.runDetail === "function") {
        // A success never fabricates a terminal state locally: the run's
        // authoritative projection is re-read instead (selectRun resets the
        // decision state, so the receipt notice is restored afterwards and
        // renders next to the authoritative card). selectRun bumps the
        // generation exactly once, so the receipt is restored only when this
        // re-read is still the unique current generation — if the user
        // switched away and back to the same run mid-read, the newer
        // selection owns the state and this stale receipt is dropped.
        const rereadGeneration = this.selectionGeneration + 1;
        await this.selectRun(runId);
        if (this.selectionGeneration !== rereadGeneration) return;
        this.state.cognitiveDecision.lastDecision = receipt;
        this.render();
        return;
      }
      this.state.cognitiveDecision.submitting = false;
      this.state.cognitiveDecision.lastDecision = receipt;
      this.state.detail.cognitiveDraft = null;
    } catch (error) {
      if (!stillCurrent()) return;
      this.state.cognitiveDecision.submitting = false;
      this.state.cognitiveDecision.error =
        error && error.details && error.details.code
          ? error.details.code
          : error && error.kind
            ? error.kind
            : "unreachable";
    }
    if (!stillCurrent()) return;
    this.render();
  }

  async submitCognitiveWithdrawal() {
    const selected = this.state.detail && this.state.detail.cognitiveDraft;
    if (
      !this.cognitiveDecisionClient ||
      !selected ||
      !(selected.kept || selected.withdrawalPending) ||
      this.state.cognitiveDecision.submitting
    ) {
      return;
    }
    const generation = this.selectionGeneration;
    const runId = selected.runId;
    const stillCurrent = () =>
      generation === this.selectionGeneration && this.state.selectedRunId === runId;
    this.state.cognitiveDecision.submitting = true;
    this.state.cognitiveDecision.error = null;
    this.render();
    try {
      // expectedSequence comes from the projection: for a pending withdrawal
      // it is the receipt's original sequence, never the timeline maximum.
      await this.cognitiveDecisionClient.submitWithdrawal({
        runId: selected.runId,
        fragmentId: selected.fragmentId,
        expectedSequence: selected.expectedSequence,
      });
      if (!stillCurrent()) return;
      // A success never fabricates a completed withdrawal locally: the run's
      // real projection is re-read instead. selectRun bumps the generation,
      // so it must run after the stillCurrent check above.
      if (this.client && typeof this.client.runDetail === "function") {
        await this.selectRun(runId);
      } else {
        this.state.cognitiveDecision.submitting = false;
        this.render();
      }
    } catch (error) {
      if (!stillCurrent()) return;
      // Timeout / HTTP / contract errors keep the current surface; the same
      // idempotent request can be triggered again manually.
      this.state.cognitiveDecision.submitting = false;
      this.state.cognitiveDecision.error =
        error && error.details && error.details.code
          ? error.details.code
          : error && error.kind
            ? error.kind
            : "unreachable";
      this.render();
    }
  }

  async submitMinimumValueDecision(decision) {
    const selected = this.state.detail && this.state.detail.minimumValue;
    if (
      !this.minimumValueClient ||
      !selected ||
      this.state.minimumValue.submitting ||
      (decision === "consent" && !selected.ready)
    ) {
      return;
    }
    const generation = this.selectionGeneration;
    const runId = selected.runId;
    const stillCurrent = () =>
      generation === this.selectionGeneration && this.state.selectedRunId === runId;
    this.state.minimumValue.submitting = true;
    this.state.minimumValue.error = null;
    this.render();
    try {
      const result = await this.minimumValueClient.submitDecision({
        decision,
        runId: selected.runId,
        fragmentId: selected.fragmentId,
        outboundPayload: selected.outboundPayload,
        outboundPayloadSha256: selected.outboundPayloadSha256,
        targetProvider: selected.targetProvider,
        targetModel: selected.targetModel,
        targetProfile: selected.targetProfile,
        expectedSequence: selected.expectedSequence,
      });
      if (!stillCurrent()) return;
      this.state.minimumValue.submitting = false;
      this.state.minimumValue.lastDecision = result.decision;
      this.state.detail.minimumValue = null;
    } catch (error) {
      if (!stillCurrent()) return;
      this.state.minimumValue.submitting = false;
      this.state.minimumValue.error =
        error && error.details && error.details.code
          ? error.details.code
          : error && error.kind
            ? error.kind
            : "unreachable";
    }
    if (!stillCurrent()) return;
    this.render();
  }

  async loadControl(runId, generation) {
    if (!runId || !this.controlClient) {
      this.state.control.status = "unavailable";
      this.render();
      return;
    }
    const gen = typeof generation === "number" ? generation : this.selectionGeneration;
    const stillCurrent = () =>
      gen === this.selectionGeneration && this.state.selectedRunId === runId;
    this.state.control.status = "loading";
    this.state.control.error = null;
    this.render();
    try {
      const [actionsBody, intentsBody] = await Promise.all([
        this.controlClient.actionsFor(runId),
        this.controlClient.listIntents(runId, 20),
      ]);
      if (!stillCurrent()) return;
      this.state.control.actions = model.controlActionsView(actionsBody);
      this.state.control.intents = model.intentHistoryView(intentsBody);
      this.state.control.status = "ready";
      this.state.control.error = null;
    } catch (error) {
      if (!stillCurrent()) return;
      this.state.control.actions = null;
      this.state.control.status = error && error.kind === "unreachable" ? "unreachable" : "error";
      this.state.control.error =
        error && error.details && error.details.code ? error.details.code : "unreachable";
    }
    if (!stillCurrent()) return;
    this.render();
  }

  toggleSection(section, open) {
    if (section === "queue") this.state.ui.queueOpen = open === true;
    if (section === "runs") this.state.ui.runsOpen = open === true;
    if (section === "proposalHistory") this.state.ui.proposalHistoryOpen = open === true;
  }

  openDialog(action) {
    const control = this.state.control;
    if (!control.actions || control.status !== "ready") return;
    const entry = control.actions.actions.find((item) => item.action === action);
    if (!entry || !entry.enabled) return;
    // Remember the trigger by a stable run/action selector, not a DOM node:
    // render() rebuilds the tree, so a captured node would be detached.
    this.dialogTrigger = { label: entry.label, runId: control.actions.runId };
    control.dialog = {
      action,
      runId: control.actions.runId,
      status: control.actions.status,
      statusLabel: control.actions.statusLabel,
      expectedSequence: control.actions.latestSequence,
      priority: control.actions.priority,
      step: 1,
    };
    control.submitError = null;
    this.render();
    this.focusDialogConfirm();
  }

  closeDialog() {
    this.state.control.dialog = null;
    this.render();
    this.restoreDialogFocus();
  }

  restoreDialogFocus() {
    const trigger = this.dialogTrigger;
    this.dialogTrigger = null;
    if (!trigger || !this.contentEl || typeof this.contentEl.querySelectorAll !== "function") {
      return;
    }
    const wanted = `${trigger.label}运行 ${trigger.runId}`;
    // Real DOM querySelectorAll returns a NodeList, not an Array — never
    // call Array methods on it directly.
    const buttons = Array.from(this.contentEl.querySelectorAll(".lc-control-btn"));
    const target = buttons.find((el) => el.getAttribute("aria-label") === wanted);
    const fallback = this.contentEl.querySelector(".lc-refresh");
    const node = target || fallback;
    if (node && typeof node.focus === "function") node.focus({ preventScroll: true });
  }

  focusDialogConfirm() {
    if (!this.contentEl || typeof this.contentEl.querySelector !== "function") return;
    const el = this.contentEl.querySelector(".lc-dialog-confirm");
    if (el && typeof el.focus === "function") el.focus({ preventScroll: true });
  }

  async confirmDialog(priority) {
    const control = this.state.control;
    const dialog = control.dialog;
    if (!dialog || control.submitting) return;
    if (dialog.action === "terminate" && dialog.step === 1) {
      control.dialog = { ...dialog, step: 2 };
      this.render();
      this.focusDialogConfirm();
      return;
    }
    const args = dialog.action === "priority" ? { priority } : {};
    control.submitting = true;
    control.submitError = null;
    this.render();
    try {
      const result = await this.controlClient.submitIntent({
        runId: dialog.runId,
        action: dialog.action,
        expectedSequence: dialog.expectedSequence,
        arguments: args,
      });
      control.lastReceipt = model.receiptView(result.receipt);
      control.duplicate = result.httpStatus === 200;
      control.dialog = null;
      control.submitting = false;
      this.render();
      this.restoreDialogFocus();
      await this.loadControl(dialog.runId);
    } catch (error) {
      control.submitting = false;
      control.dialog = null;
      control.duplicate = false;
      if (error && error.kind === "http_error") {
        const code = error.details && error.details.code ? error.details.code : null;
        control.submitError = {
          code,
          text: model.reasonText(code) || "控制服务返回错误，操作未提交",
        };
      } else {
        control.submitError = { code: "unreachable", text: "控制服务不可用，操作未提交" };
      }
      this.render();
      this.restoreDialogFocus();
    }
  }
}

module.exports = { LOOP_CONSOLE_VIEW_TYPE, LoopConsoleView };
