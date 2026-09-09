"use strict";

// R3 §4.8：攒批授权通知判定器（纯逻辑，可注入时钟/写入/开关做单元测试）。
// 触发语义：
// - 只有「待决数 0 → N（N≥1）」评估一次（系统停下来等人授权）；
// - 冷却期内新入队合并进下一条（不重复发）；
// - 归零恢复不通知（pendingCount === 0 只复位 lastCount）；
// - 持续 >0 不重复触发；
// - 内容契约只含 kind/count/sources/created_at——绝不外发授权材料。

function createAuthNotifier(options = {}) {
  const cooldownMs = options.cooldownMs || 30 * 60 * 1000;
  const now = options.now || (() => Date.now());
  const enabled = options.enabled || (() => true);
  // R3c：可选持久化——多实例/插件重载后冷却记忆不丢（lastNotifyAt 与
  // lastCount 变更时 save；创建时 load；读写失败静默降级内存态）。
  const persist = options.persist || null;
  let lastCount = 0;
  let lastNotifyAt = 0;
  if (persist && typeof persist.load === "function") {
    try {
      const restored = persist.load();
      if (restored && typeof restored.lastCount === "number") lastCount = restored.lastCount;
      if (restored && typeof restored.lastNotifyAt === "number") lastNotifyAt = restored.lastNotifyAt;
    } catch { /* 持久化损坏/不可读 → 内存态（不影响队列主功能） */ }
  }
  const saveState = () => {
    if (persist && typeof persist.save === "function") {
      try {
        persist.save({ lastCount, lastNotifyAt });
      } catch { /* 写入失败静默降级内存态 */ }
    }
  };
  return {
    // 返回 null（不通知）或 {kind, count, sources, created_at}（通知载荷）。
    evaluate(pendingCount, sourceCounts) {
      if (!enabled()) return null;
      if (pendingCount === 0) {
        if (lastCount !== 0) {
          lastCount = 0;
          saveState();
        }
        return null;
      }
      if (lastCount > 0) {
        lastCount = pendingCount;
        saveState();
        return null;
      }
      const at = now();
      // lastNotifyAt === 0 = 从未通知过 → 不检查冷却。
      // 冷却期内新入队合并进下一条：不推进 lastCount（保持 0），冷却结束后
      // 再次 0→N 触发时计数即窗口期合并值。
      if (lastNotifyAt > 0 && at - lastNotifyAt < cooldownMs) {
        return null;
      }
      lastCount = pendingCount;
      lastNotifyAt = at;
      saveState();
      return {
        kind: "auth_notification",
        count: pendingCount,
        sources: sourceCounts || {},
        created_at: new Date(at).toISOString(),
      };
    },
    state() {
      return { lastCount, lastNotifyAt };
    },
  };
}

module.exports = { createAuthNotifier };
