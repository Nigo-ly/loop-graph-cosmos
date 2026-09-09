const { Plugin, Notice, Modal, Setting, MarkdownView, normalizePath, requestUrl } = require("obsidian");

const HOME_PATH = "Notes/My Life.md";
const VIEW_CLASS = "my-life-homepage-view";

class ExperimentFeedbackModal extends Modal {
  constructor(app, plugin, path) {
    super(app);
    this.plugin = plugin;
    this.path = path;
    this.values = { status: "passed", process: "", problems: "", outcome: "", evidence: "", reusable: "", capabilities: "", next: "" };
  }

  onOpen() {
    this.titleEl.setText("记录实践反馈");
    this.contentEl.createEl("p", { text: "只记录真实发生的过程和结果。验证结论只是你的实践判断，系统只更新原记录，不生成或更新任何资产卡。", cls: "setting-item-description" });
    new Setting(this.contentEl).setName("验证结论").addDropdown(dropdown => dropdown.addOptions({ passed: "验证通过", limited: "通过但有限制", failed: "验证未通过" }).setValue(this.values.status).onChange(value => this.values.status = value));
    const area = (name, key, placeholder) => new Setting(this.contentEl).setName(name).addTextArea(input => input.setPlaceholder(placeholder).onChange(value => this.values[key] = value));
    area("实际过程", "process", "按顺序记录真正执行过的步骤");
    area("遇到的问题", "problems", "没有则填写“无”");
    area("实际结果", "outcome", "写清楚成功、失败或限制");
    area("验证证据", "evidence", "测试现象、文件、截图或可复查结果");
    area("已具备能力", "capabilities", "用逗号分隔，例如：邮箱接入, 邮件转工单");
    area("可复用场景", "reusable", "以后哪些项目或场景可以使用");
    area("后续升级", "next", "下一轮需要复测或完善什么");
    new Setting(this.contentEl).addButton(button => button.setButtonText("保存反馈").setCta().onClick(async () => {
      if (!this.values.process.trim() || !this.values.outcome.trim()) return new Notice("请至少填写实际过程和实际结果");
      await this.plugin.saveExperimentFeedback(this.path, this.values);
      this.close();
    }));
  }

  onClose() { this.contentEl.empty(); }
}

class HomepageContentManagerModal extends Modal {
  constructor(app, plugin) { super(app); this.plugin = plugin; }
  onOpen() {
    this.titleEl.setText("主页隐藏内容");
    const hidden = Object.entries(this.plugin.data.contentStates || {}).filter(([, state]) => state.hidden || Number(state.hiddenUntil || 0) > Date.now());
    if (!hidden.length) return this.contentEl.createEl("p", { text: "当前没有隐藏内容。", cls: "setting-item-description" });
    this.contentEl.createEl("p", { text: "恢复后，内容会重新参与主页筛选。", cls: "setting-item-description" });
    for (const [path] of hidden) {
      const state = this.plugin.data.contentStates[path] || {};
      const mode = state.hidden ? "已移出主页" : `今日隐藏，${new Date(state.hiddenUntil).toLocaleDateString("zh-CN")} 自动恢复`;
      const setting = new Setting(this.contentEl).setName(path.split("/").pop().replace(/\.md$/, ""));
      setting.setDesc(`${mode} · ${path}`).addButton(button => button.setButtonText("立即恢复").onClick(async () => {
        await this.plugin.setContentState(path, "restore");
        setting.settingEl.remove();
      }));
    }
  }
  onClose() { this.contentEl.empty(); }
}

class QuickCaptureModal extends Modal {
  constructor(app, plugin) { super(app); this.plugin = plugin; this.content = ""; this.loopApproved = false; }
  onOpen() {
    this.titleEl.setText("快速记录碎片");
    new Setting(this.contentEl).setName("内容").setDesc("链接、想法或复制内容都可以").addTextArea(input => input.setPlaceholder("现在想到什么？").onChange(value => this.content = value));
    new Setting(this.contentEl).setName("进入 Loop").setDesc("与手机端 nigo-loop 勾选含义一致；未勾选只保存，不启动 Loop。").addToggle(toggle => toggle.setValue(false).onChange(value => this.loopApproved = value));
    new Setting(this.contentEl).addButton(button => button.setButtonText("保存到碎片系统").setCta().onClick(async () => {
      if (!this.content.trim()) return new Notice("请输入内容");
      await this.plugin.quickCapture(this.content, this.loopApproved);
      this.close();
    }));
  }
  onClose() { this.contentEl.empty(); }
}

class ProjectAssessmentModal extends Modal {
  constructor(app, plugin) { super(app); this.plugin = plugin; this.project = ""; this.goal = ""; }
  onOpen() {
    this.titleEl.setText("创建 Agent 项目能力评估");
    this.contentEl.createEl("p", { text: "生成一份包含项目目标、已验证资产和待验证路径的请求文件，供 Agent 扫描和继续处理。", cls: "setting-item-description" });
    new Setting(this.contentEl).setName("项目名称").addText(input => input.setPlaceholder("例如：客服自动化项目").onChange(value => this.project = value));
    new Setting(this.contentEl).setName("目标与需求").addTextArea(input => input.setPlaceholder("准备做什么，需要解决什么问题？").onChange(value => this.goal = value));
    new Setting(this.contentEl).addButton(button => button.setButtonText("生成评估请求").setCta().onClick(async () => {
      if (!this.project.trim() || !this.goal.trim()) return new Notice("请填写项目名称和目标");
      await this.plugin.createProjectAssessment(this.project, this.goal);
      this.close();
    }));
  }
  onClose() { this.contentEl.empty(); }
}

module.exports = class MyLifeHomepage extends Plugin {
  async onload() {
    window.MyLifeHomepagePlugin = this;
    this.data = Object.assign({ totals: {}, read: {}, contentStates: {}, viewPreferences: { filter: "all", density: "comfortable" } }, await this.loadData());
    this.data.totals ||= {};
    this.data.read ||= {};
    this.data.contentStates ||= {};
    this.data.viewPreferences ||= { filter: "all", density: "comfortable" };
    this.running = false;
    this.remaining = 25 * 60;
    this.currentPath = null;
    this.lastTick = 0;
    this.unsavedSeconds = 0;

    const refresh = () => {
      this.refreshViews();
      this.ensureHomePreview();
    };
    this.registerEvent(this.app.workspace.on("active-leaf-change", refresh));
    this.registerEvent(this.app.workspace.on("file-open", file => {
      this.changeReadingFile(file);
      refresh();
      if (file?.path === HOME_PATH) this.restoreHomeScroll();
    }));
    this.registerEvent(this.app.workspace.on("layout-change", refresh));

    this.addRibbonIcon("home", "打开 My Life", () => this.openHome());
    this.addRibbonIcon("timer", "开始或暂停番茄阅读钟", () => this.toggleReadingTimer());
    this.addCommand({ id: "open-my-life", name: "打开 My Life 主页", callback: () => this.openHome() });
    this.addCommand({ id: "toggle-reading-timer", name: "开始或暂停番茄阅读钟", callback: () => this.toggleReadingTimer() });
    this.addCommand({ id: "reset-reading-time", name: "清零当前文档阅读时间", callback: () => this.resetCurrentReadingTime() });
    this.addCommand({ id: "manage-homepage-content", name: "管理主页隐藏内容", callback: () => this.openContentManager() });
    this.addCommand({ id: "quick-capture", name: "快速记录碎片", callback: () => this.openQuickCapture() });
    this.addCommand({ id: "create-project-assessment", name: "创建 Agent 项目能力评估", callback: () => this.openProjectAssessment() });

    this.status = this.addStatusBarItem();
    this.status.addClass("my-life-reading-status");
    this.status.addEventListener("click", () => this.toggleReadingTimer());
    this.registerInterval(window.setInterval(() => this.tickReadingTimer(), 1000));
    this.updateTimerStatus();
    this.app.workspace.onLayoutReady(refresh);
  }

  onunload() {
    delete window.MyLifeHomepagePlugin;
    document.querySelectorAll(`.${VIEW_CLASS}`).forEach(el => el.removeClass(VIEW_CLASS));
  }

  refreshViews() {
    for (const leaf of this.app.workspace.getLeavesOfType("markdown")) {
      leaf.view.containerEl.toggleClass(VIEW_CLASS, leaf.view.file?.path === HOME_PATH);
    }
  }

  ensureHomePreview() {
    const file = this.app.workspace.getActiveFile();
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (file?.path !== HOME_PATH || view?.getMode?.() !== "source" || this.switchingHomeMode) return;
    this.switchingHomeMode = true;
    window.setTimeout(async () => {
      try {
        if (this.app.workspace.getActiveFile()?.path === HOME_PATH && view.getMode?.() === "source") {
          await this.app.commands.executeCommandById("markdown:toggle-preview");
        }
      } finally {
        this.switchingHomeMode = false;
      }
    }, 0);
  }

  getHomeScroller(leaf) {
    if (!leaf?.view?.containerEl) return null;
    const candidates = [leaf.view.containerEl, ...leaf.view.containerEl.querySelectorAll("*")]
      .filter(el => el.scrollHeight > el.clientHeight + 20)
      .filter(el => ["auto", "scroll", "overlay"].includes(getComputedStyle(el).overflowY));
    return candidates.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0]
      || leaf.view.containerEl.querySelector(".markdown-preview-view, .markdown-reading-view, .view-content");
  }

  captureHomeScroll() {
    const leaf = this.app.workspace.getLeavesOfType("markdown").find(item => item.view.file?.path === HOME_PATH);
    const scroller = this.getHomeScroller(leaf);
    if (scroller) this.homeScrollTop = scroller.scrollTop;
  }

  restoreHomeScroll() {
    if (!Number.isFinite(this.homeScrollTop)) return;
    let attempts = 0;
    const restore = () => {
      const leaf = this.app.workspace.getLeavesOfType("markdown").find(item => item.view.file?.path === HOME_PATH);
      const scroller = this.getHomeScroller(leaf);
      if (scroller && scroller.scrollHeight >= this.homeScrollTop + scroller.clientHeight) scroller.scrollTop = this.homeScrollTop;
      if (++attempts < 24) window.setTimeout(restore, 150);
    };
    restore();
  }

  async openHome() {
    const file = this.app.vault.getAbstractFileByPath(HOME_PATH);
    if (!file) return;
    const leaf = this.app.workspace.getLeaf(false);
    await leaf.openFile(file, { active: true, state: { mode: "preview" } });
    this.refreshViews();
  }

  openExperimentFeedback(path) {
    if (!path) return;
    new ExperimentFeedbackModal(this.app, this, path).open();
  }

  openContentManager() { new HomepageContentManagerModal(this.app, this).open(); }
  openQuickCapture() { new QuickCaptureModal(this.app, this).open(); }
  openProjectAssessment() { new ProjectAssessmentModal(this.app, this).open(); }

  getViewPreferences() { return { filter: "all", density: "comfortable", ...this.data.viewPreferences }; }

  async setViewPreference(key, value) {
    this.data.viewPreferences = { ...this.getViewPreferences(), [key]: value };
    await this.saveData(this.data);
    this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
  }

  showUndo(message, undo) {
    const notice = new Notice("", 6000);
    notice.noticeEl.createSpan({ text: message });
    const button = notice.noticeEl.createEl("button", { text: "撤销" });
    button.addEventListener("click", async () => { await undo(); notice.hide(); });
  }

  getWikiWatch(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    const frontmatter = file?.extension === "md" ? this.app.metadataCache.getFileCache(file)?.frontmatter : null;
    if (!frontmatter || !Object.prototype.hasOwnProperty.call(frontmatter, "homepage_watch")) return null;
    return Boolean(frontmatter.homepage_watch);
  }

  async setWikiWatch(path, watched) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file || file.extension !== "md") return;
    await this.app.fileManager.processFrontMatter(file, frontmatter => {
      if (watched) {
        frontmatter.homepage_watch = true;
        frontmatter.homepage_watch_since ||= new Date().toLocaleDateString("en-CA");
      } else {
        delete frontmatter.homepage_watch;
        delete frontmatter.homepage_watch_since;
      }
    });
  }

  getContentState(path) {
    const state = this.data.contentStates?.[path] || {};
    const hiddenUntil = Number(state.hiddenUntil || 0);
    const wikiWatch = this.getWikiWatch(path);
    return { read: Boolean(state.read || this.data.read?.[path]), watched: wikiWatch ?? Boolean(state.watched), hidden: Boolean(state.hidden || hiddenUntil > Date.now()), permanentlyHidden: Boolean(state.hidden), hiddenUntil, updatedAt: state.updatedAt || 0 };
  }

  async setContentState(path, action) {
    if (!path) return;
    const current = this.getContentState(path);
    const previous = { ...current };
    const next = { ...current, updatedAt: Date.now() };
    if (action === "read") next.read = !current.read;
    if (action === "watch") next.watched = !current.watched;
    if (action === "hide") next.hidden = true;
    if (action === "hide_today") { const tomorrow = new Date(); tomorrow.setHours(24, 0, 0, 0); next.hidden = false; next.hiddenUntil = tomorrow.getTime(); }
    if (action === "restore") { next.hidden = false; next.hiddenUntil = 0; }
    if (action === "watch") {
      try { await this.setWikiWatch(path, next.watched); }
      catch (error) { console.error("[My Life] 无法回写持续关注状态", path, error); return new Notice("持续关注状态未能写入 Wiki"); }
    }
    this.data.contentStates[path] = next;
    if (next.read) this.data.read[path] = Date.now(); else delete this.data.read[path];
    await this.saveData(this.data);
    this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
    if (["hide", "hide_today", "watch", "read"].includes(action)) this.showUndo(action === "hide" ? "已移出主页" : action === "hide_today" ? "今天不再显示，明日自动恢复" : action === "watch" ? (next.watched ? "已加入持续关注" : "已取消关注") : (next.read ? "已标为已读" : "已标为未读"), async () => {
      if (action === "watch") await this.setWikiWatch(path, previous.watched);
      this.data.contentStates[path] = previous;
      if (previous.read) this.data.read[path] = Date.now(); else delete this.data.read[path];
      await this.saveData(this.data);
      this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
    });
  }

  async startExperiment(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) return new Notice("找不到整理记录");
    await this.app.fileManager.processFrontMatter(file, frontmatter => {
      frontmatter.experiment_status = "testing";
      frontmatter.started_at = new Date().toISOString();
    });
    new Notice("已标记为验证中；请按下一步人工执行，系统不会自动验证");
    this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
  }

  async completeTask(path, lineNumber) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file || !Number.isFinite(Number(lineNumber))) return new Notice("找不到任务来源");
    let changed = false;
    await this.app.vault.process(file, content => {
      const lines = content.split("\n"), index = Number(lineNumber);
      if (lines[index] && /^\s*[-*+]\s+\[ \]/.test(lines[index])) { lines[index] = lines[index].replace("[ ]", "[x]"); changed = true; }
      return lines.join("\n");
    });
    new Notice(changed ? "任务已完成" : "任务状态没有变化");
    if (changed) this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
  }

  async quickCapture(content, loopApproved = false) {
    const now = new Date();
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(now).filter(p => p.type !== "literal");
    const values = Object.fromEntries(parts.map(p => [p.type, p.value]));
    const name = `${values.year}-${values.month}-${values.day}-${values.hour}-${values.minute}-${values.second}-${crypto.randomUUID().slice(0, 6)}-desktop.md`;
    const path = normalizePath(`Notes/散记/碎片想法/${name}`);
    const sourceUrl = content.match(/https?:\/\/[^\s)\]]+/)?.[0] || null;
    const body = `---\ntype: "碎片想法"\ncaptured_at: "${now.toISOString()}"\ncaptured_date: "${values.year}-${values.month}-${values.day}"\ncapture_type: "桌面快速记录"\nsource: "Obsidian"\npipeline_status: "queued"\nnigo-loop: ${loopApproved ? "true" : "false"}\nsource_url: ${JSON.stringify(sourceUrl)}\ntags: [碎片想法, Inbox]\n---\n\n${content.trim()}\n`;
    await this.app.vault.create(path, body);
    new Notice(loopApproved ? "碎片已保存；已标记进入 Loop" : "碎片已保存");
    this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
  }

  async requestFragmentExploration(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file || file.extension !== "md") return new Notice("找不到原始碎片");
    await this.app.fileManager.processFrontMatter(file, frontmatter => {
      frontmatter.exploration_requested = true;
      frontmatter.exploration_requested_at = new Date().toISOString();
      frontmatter.pipeline_status = "priority_queued";
    });
    try {
      await requestUrl({ url: "http://127.0.0.1:5678/webhook/fragment-organize-now", method: "POST", contentType: "application/json", body: JSON.stringify({ source_file: file.name }) });
      new Notice("已开始提取网页并整理，结果完成后会自动出现");
    } catch (error) {
      console.error("[My Life] 无法立即触发碎片整理", path, error);
      new Notice("已加入优先队列，自动整理会在5分钟内重试");
    }
    this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
  }

  async createProjectAssessment(project, goal) {
    const folder = "Notes/散记/Agent项目评估";
    if (!this.app.vault.getAbstractFileByPath(folder)) await this.app.vault.createFolder(folder);
    const assets = this.app.vault.getMarkdownFiles().filter(file => this.app.metadataCache.getFileCache(file)?.frontmatter?.type === "碎片已验证资产");
    const assetRows = assets.map(file => { const fm = this.app.metadataCache.getFileCache(file)?.frontmatter || {}; const capabilities = Array.isArray(fm.capabilities) ? fm.capabilities : this.list(fm.capabilities); return `- [[${file.path.replace(/\.md$/, "")}]] · ${fm.maturity || "已验证"} · ${capabilities.join("、") || "能力待补充"}`; }).join("\n") || "- 暂无已验证资产";
    const date = new Date().toLocaleDateString("en-CA");
    const path = normalizePath(`${folder}/${date}-${this.safeName(project)}.md`);
    const body = `---\ntype: "Agent项目能力评估请求"\nstatus: "pending"\ncreated_at: "${new Date().toISOString()}"\nproject: ${JSON.stringify(project)}\n---\n\n# ${project} · 能力评估请求\n\n## 项目目标与需求\n\n${goal.trim()}\n\n## 已验证碎片资产\n\n${assetRows}\n\n## Agent 必须输出\n\n1. 可直接复用的资产与依据。\n2. 需要升级或复测的路径。\n3. 尚不具备的能力缺口。\n4. 最小新增验证计划。\n5. 执行完成后需要回写的实践与资产。\n`;
    if (this.app.vault.getAbstractFileByPath(path)) await this.app.vault.modify(this.app.vault.getAbstractFileByPath(path), body); else await this.app.vault.create(path, body);
    await this.app.workspace.openLinkText(path, HOME_PATH, false);
    new Notice(`已生成评估请求，包含 ${assets.length} 项已验证资产`);
  }

  async markViewed(path) {
    if (!path || this.getContentState(path).read) return;
    const current = this.getContentState(path);
    this.data.contentStates[path] = { ...current, read: true, updatedAt: Date.now() };
    this.data.read[path] = Date.now();
    await this.saveData(this.data);
  }

  list(value) {
    return String(value || "").split(/[，,\n]/).map(item => item.trim()).filter(Boolean);
  }

  safeName(value) {
    return String(value || "未命名资产").replace(/[\\/:*?"<>|]/g, "-").slice(0, 70);
  }

  async saveExperimentFeedback(path, values) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) return new Notice("找不到对应的整理笔记");
    const now = new Date();
    const date = now.toLocaleDateString("en-CA");
    const passed = values.status !== "failed";
    await this.app.fileManager.processFrontMatter(file, frontmatter => {
      frontmatter.experiment_status = passed ? "completed" : "failed";
      frontmatter.completed_at = date;
      frontmatter.outcome = values.outcome.trim();
      frontmatter.reusable_for = values.reusable.trim();
      frontmatter.validation_evidence = values.evidence.trim();
      frontmatter.content_lifecycle = "user_confirmed";
      frontmatter.evidence_level = "unverified";
    });
    await this.app.vault.process(file, content => `${content.trim()}\n\n## 实践反馈更新 · ${date}\n\n### 实际过程\n\n${values.process.trim()}\n\n### 遇到的问题\n\n${values.problems.trim() || "无"}\n\n### 实际结果\n\n${values.outcome.trim()}\n\n### 验证证据\n\n${values.evidence.trim() || "待补充"}\n\n### 后续可复用场景\n\n${values.reusable.trim() || "待补充"}\n\n### 后续升级\n\n${values.next.trim() || "暂无"}\n`);
    new Notice(passed ? "实践反馈已保存，仅更新原记录，不生成资产" : "失败结果已保存，仅更新原记录，不生成资产");
    this.app.commands.executeCommandById("dataview:dataview-force-refresh-views");
  }

  startReadingTimer() {
    if (this.running) return;
    const file = this.app.workspace.getActiveFile();
    if (!file || file.extension !== "md") return new Notice("请先打开要阅读的文档");
    this.currentPath = file.path;
    this.remaining = 25 * 60;
    this.lastTick = Date.now();
    this.running = true;
    this.updateTimerStatus();
    new Notice(`开始记录：${file.basename}`);
  }

  async stopReadingTimer(completed = false) {
    if (!this.running) return;
    this.accrueReadingTime();
    this.running = false;
    await this.saveData(this.data);
    this.updateTimerStatus();
    new Notice(completed ? "番茄钟完成，阅读时间已保存" : "阅读计时已暂停");
  }

  toggleReadingTimer() {
    return this.running ? this.stopReadingTimer() : this.startReadingTimer();
  }

  changeReadingFile(file) {
    if (this.running) this.accrueReadingTime();
    this.currentPath = file?.extension === "md" ? file.path : null;
    this.updateTimerStatus();
  }

  accrueReadingTime() {
    if (!this.running || !this.lastTick) return;
    const now = Date.now();
    const seconds = Math.max(0, Math.min(5, (now - this.lastTick) / 1000));
    this.lastTick = now;
    if (this.currentPath) this.data.totals[this.currentPath] = (this.data.totals[this.currentPath] || 0) + seconds;
    this.remaining = Math.max(0, this.remaining - seconds);
    this.unsavedSeconds += seconds;
  }

  tickReadingTimer() {
    if (!this.running) return;
    this.accrueReadingTime();
    if (this.unsavedSeconds >= 30) {
      this.unsavedSeconds = 0;
      this.saveData(this.data);
    }
    if (this.remaining <= 0) return this.stopReadingTimer(true);
    this.updateTimerStatus();
  }

  getTimerState() {
    const file = this.app.workspace.getActiveFile();
    const path = file?.extension === "md" ? file.path : null;
    return {
      running: this.running,
      remaining: Math.ceil(this.remaining),
      total: path ? Math.round(this.data.totals[path] || 0) : 0,
      fileName: file?.extension === "md" ? file.basename : "",
      pomodoroDuration: 25,
    };
  }

  getCurrentFocusTask() {
    return this.app.workspace.getActiveFile()?.basename || "未选择文档";
  }

  getLastBriefingTime() {
    return "由 AI/每日简报记录";
  }

  formatDuration(seconds) {
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟`;
    return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
  }

  async resetCurrentReadingTime() {
    const file = this.app.workspace.getActiveFile();
    if (!file || file.extension !== "md") return;
    this.data.totals[file.path] = 0;
    await this.saveData(this.data);
    this.updateTimerStatus();
    new Notice("已清零当前文档阅读时间");
  }

  isRead(path) {
    return Boolean(this.data.read[path]);
  }

  async setRead(path, read) {
    if (!path) return;
    if (read) this.data.read[path] = Date.now();
    else delete this.data.read[path];
    await this.saveData(this.data);
  }

  updateTimerStatus() {
    if (!this.status) return;
    const state = this.getTimerState();
    const mins = Math.floor(state.remaining / 60);
    const secs = state.remaining % 60;
    this.status.setText(`🍅 ${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")} · ${state.fileName || "未打开文档"}`);
    this.status.setAttribute("aria-label", state.running ? "暂停番茄阅读钟" : "开始番茄阅读钟");
  }
};
