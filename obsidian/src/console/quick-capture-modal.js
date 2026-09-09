"use strict";

const { Modal, Notice, Setting } = require("obsidian");

// 放大的快速记录碎片入口：替代 baseline 的窄小输入框，便于粘贴长链接与多行内容。
// 仅放大交互面，不改变保存行为（仍走 plugin.quickCapture）。
class LargeQuickCaptureModal extends Modal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this.content = "";
    this.loopApproved = false;
  }

  onOpen() {
    this.titleEl.setText("快速记录碎片");
    this.modalEl.addClass("my-life-quick-capture-modal");
    new Setting(this.contentEl)
      .setName("内容")
      .setDesc("链接、想法或复制内容都可以")
      .addTextArea((input) => {
        input.setPlaceholder("现在想到什么？").onChange((value) => (this.content = value));
        input.inputEl.rows = 8;
      });
    new Setting(this.contentEl)
      .setName("进入 Loop")
      .setDesc("本次部署后新记录的公开链接，勾选后会使用现有 K3 套餐研究并自动保存结论；问题与公开资料会发送给 K3。未勾选仅保存。")
      .addToggle((toggle) => toggle.setValue(false).onChange((value) => (this.loopApproved = value)));
    new Setting(this.contentEl).addButton((button) =>
      button
        .setButtonText("保存到碎片系统")
        .setCta()
        .onClick(async () => {
          if (!this.content.trim()) return new Notice("请输入内容");
          await this.plugin.quickCapture(this.content, this.loopApproved);
          this.close();
        })
    );
  }

  onClose() {
    this.contentEl.empty();
  }
}

module.exports = { LargeQuickCaptureModal };
