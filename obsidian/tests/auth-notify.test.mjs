"use strict";

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { createAuthNotifier } = require(path.join(ROOT, "src/console/auth-notify.js"));

it("auth-notify: 0→N 触发一次，内容只含计数+分类（不含任何标题/摘要正文）", () => {
  let t = 1_000_000;
  const notifier = createAuthNotifier({ now: () => t });
  const payload = notifier.evaluate(2, { Graph: 1, Loop: 1 });
  assert.ok(payload, "0→N 触发");
  assert.equal(payload.kind, "auth_notification");
  assert.equal(payload.count, 2);
  assert.deepEqual(payload.sources, { Graph: 1, Loop: 1 });
  const raw = JSON.stringify(payload);
  for (const forbidden of ["标题", "摘要", "碎片", "授权单", "批准", "证据"]) {
    assert.ok(!raw.includes(forbidden), `内容不含 ${forbidden}`);
  }
  // 持续 >0 不重复触发
  t += 1000;
  assert.equal(notifier.evaluate(2, { Graph: 1, Loop: 1 }), null, "持续 >0 不重复");
  assert.equal(notifier.evaluate(3, { Graph: 2, Loop: 1 }), null, "新入队只合并不重发");
});

it("auth-notify: 冷却期 30 分钟内第二次 0→N 不重复发（合并进下一条）", () => {
  let t = 1_000_000;
  const notifier = createAuthNotifier({ now: () => t });
  assert.ok(notifier.evaluate(1, { Graph: 1 }), "第一次 0→N 发");
  // 归零 → 再次 0→N（30 分钟内）
  assert.equal(notifier.evaluate(0, {}), null, "归零不通知");
  t += 5 * 60 * 1000; // +5 分钟（仍在冷却）
  assert.equal(notifier.evaluate(1, { Loop: 1 }), null, "冷却内 0→N 不重发");
  // 冷却结束后新 0→N 恢复发送
  t += 30 * 60 * 1000;
  const payload = notifier.evaluate(1, { Loop: 1 });
  assert.ok(payload, "冷却结束后恢复发送");
  assert.deepEqual(payload.sources, { Loop: 1 });
});

it("auth-notify: 归零恢复不通知", () => {
  const notifier = createAuthNotifier({ now: () => 1_000_000 });
  assert.ok(notifier.evaluate(1, { Graph: 1 }), "0→N 发");
  assert.equal(notifier.evaluate(0, {}), null, "N→0 恢复不通知");
  assert.equal(notifier.evaluate(0, {}), null, "持续归零不通知");
  // 恢复后再次 0→N 可以发（冷却外）
  const again = createAuthNotifier({ now: () => 2_000_000 });
  assert.ok(again.evaluate(1, { Graph: 1 }), "恢复后再次 0→N 可发");
});

it("auth-notify: 开关关闭时不发", () => {
  const notifier = createAuthNotifier({ now: () => 1_000_000, enabled: () => false });
  assert.equal(notifier.evaluate(2, { Graph: 1, Loop: 1 }), null, "开关关闭 0→N 不发");
  assert.equal(notifier.state().lastCount, 0, "关闭时状态不推进");
});

it("R3c: persist 重载后冷却仍生效（30 分钟内第二实例 0→N 不发；过期后可发）", () => {
  const store = { state: null };
  const persist = {
    load: () => store.state,
    save: (state) => { store.state = state; },
  };
  let t = 1_000_000;
  // 实例 1：0→N 触发 + save 持久化（冷却记忆写入 sidecar 存根）
  const n1 = createAuthNotifier({ now: () => t, persist });
  assert.ok(n1.evaluate(2, { Graph: 2 }), "实例 1 0→N 触发");
  assert.deepEqual(store.state, { lastCount: 2, lastNotifyAt: 1_000_000 }, "状态已持久化");
  // 实例 2（模拟插件重载/隔离实例）：load 恢复冷却记忆 → 冷却内 0→N 不发
  t += 5 * 60 * 1000; // +5 分钟（仍在 30 分钟冷却内）
  const n2 = createAuthNotifier({ now: () => t, persist });
  assert.deepEqual(n2.state(), { lastCount: 2, lastNotifyAt: 1_000_000 }, "实例 2 load 恢复冷却记忆");
  assert.equal(n2.evaluate(1, { Graph: 1 }), null, "重载后冷却内 0→N 不发");
  // 冷却过期后可发（先归零——「持续 >0 不重复」是 R3 语义；归零后再 0→N 触发）
  t += 30 * 60 * 1000;
  assert.equal(n2.evaluate(0, {}), null, "归零恢复不通知");
  const payload = n2.evaluate(1, { Loop: 1 });
  assert.ok(payload, "冷却过期 + 归零后 0→N 可发");
  assert.deepEqual(payload.sources, { Loop: 1 });
});

it("R3c: persist 损坏时静默降级内存态（不影响主功能）", () => {
  const persist = {
    load: () => { throw new Error("sidecar 损坏"); },
    save: () => { throw new Error("写入失败"); },
  };
  const notifier = createAuthNotifier({ now: () => 1_000_000, persist });
  const payload = notifier.evaluate(1, { Graph: 1 });
  assert.ok(payload, "persist 损坏仍可触发（内存态）");
  assert.equal(notifier.state().lastCount, 1, "内存态推进");
});
