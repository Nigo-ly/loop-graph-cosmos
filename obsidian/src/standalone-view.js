"use strict";
const { ItemView } = require("obsidian");
const { disposeHomepageCosmos } = require("./console/homepage-cosmos.js");
const COSMOS_VIEW_TYPE = "loop-graph-cosmos";

class CosmosView extends ItemView {
  constructor(leaf, plugin) { super(leaf); this.plugin = plugin; }
  getViewType() { return COSMOS_VIEW_TYPE; }
  getDisplayText() { return "Loop Graph Cosmos"; }
  getIcon() { return "orbit"; }
  async onOpen() {
    this.contentEl.empty();
    this.contentEl.addClass("my-life-homepage-view");
    const root = this.contentEl.createDiv();
    const mount = root.createDiv({ cls: "life-cosmos-mount" });
    mount.setAttribute("data-life-cosmos-mount", "1");
    const legacy = root.createDiv({ cls: "life-cosmos-legacy" });
    const dashboard = legacy.createDiv({ cls: "life-dashboard-content" });
    dashboard.createDiv({ cls: "life-dashboard-grid" });
    await this.plugin.mountHomepageCosmos(root);
  }
  async onClose() {
    disposeHomepageCosmos(this.contentEl);
    this.contentEl.empty();
  }
}
module.exports = { CosmosView, COSMOS_VIEW_TYPE };
