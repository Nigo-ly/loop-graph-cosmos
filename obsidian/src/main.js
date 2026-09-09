"use strict";

const { Platform, Notice } = require("obsidian");
const { CosmosView, COSMOS_VIEW_TYPE } = require("./standalone-view.js");
const BaselinePlugin = require("./runtime/main.js");
const { setupLoopConsole } = require("./console/register.js");
const { collectThoughtMap } = require("./console/thought-map.js");
const { LargeQuickCaptureModal } = require("./console/quick-capture-modal.js");

module.exports = class MyLifeHomepage extends BaselinePlugin {
  async onload() {
    await super.onload();
    setupLoopConsole(this);
    this.registerView(COSMOS_VIEW_TYPE, leaf => new CosmosView(leaf, this));
    await this.setupDesktopPinchZoom();
  }

  async setupDesktopPinchZoom() {
    if (!Platform.isDesktopApp) return;
    try {
      // ponytail: native pinch scales the main window, including Cosmos dialogs.
      // Keep Obsidian's separate Cmd +/-/0 layout zoom and mobile gestures intact.
      const { webFrame } = window.require("electron");
      const limits = (max) => Promise.resolve().then(() => webFrame.setVisualZoomLevelLimits(1, max));
      let disposed = false;
      this.register(() => {
        disposed = true;
        limits(1).catch(() => {});
      });
      await limits(3);
      if (disposed) return;
      this.addCommand({
        id: "reset-pinch-zoom",
        name: "恢复双指缩放（100%）",
        callback: async () => {
          if (disposed) return;
          try {
            await limits(1);
            if (!disposed) await limits(3);
          } catch {
            new Notice("双指缩放暂时无法恢复，请重载 My Life 插件后重试。");
          }
        },
      });
    } catch {
      new Notice("当前 Obsidian 未能开启双指缩放；仍可使用 ⌘ + 和 ⌘ − 调整界面大小。");
    }
  }

  async openHome() {
    const leaf = this.app.workspace.getLeavesOfType(COSMOS_VIEW_TYPE)[0] || this.app.workspace.getLeaf("tab");
    await leaf.setViewState({ type: COSMOS_VIEW_TYPE, active: true });
    this.app.workspace.revealLeaf(leaf);
  }

  openQuickCapture() {
    new LargeQuickCaptureModal(this.app, this).open();
  }

  async getThoughtMap() {
    return collectThoughtMap(this.app);
  }
};
