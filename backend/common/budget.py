"""Token 预算控制 — 预估式「开头检查」机制。

用法:
    budget = TokenBudget(limit=8000)
    for round in loop:
        budget.check()          # 开头检查：预估不够就抛 BudgetExceededError
        ...                     # 执行本轮
        budget.consume(cost)    # 记录消耗
"""

from __future__ import annotations


class BudgetExceededError(Exception):
    """预算超限异常 — 被捕获后优雅终止。"""


class TokenBudget:
    """预估式预算：已消耗 + 预估本轮消耗 < 总预算，才允许继续。

    核心逻辑: check() 在每轮开头调用，基于历史消耗动态预估本轮开销。
    如果剩余预算不够跑一轮，立刻终止，拒绝执行。
    """

    def __init__(self, limit: int, default_estimate: int = 300):
        self.limit = limit
        self.consumed = 0
        self._costs: list[int] = []
        self._fallback_estimate = default_estimate

    def consume(self, tokens: int) -> None:
        self.consumed += tokens
        self._costs.append(tokens)

    def _estimate_next_round(self) -> int:
        """基于最近 3 轮滑动平均预估下一轮消耗，首轮用默认值。"""
        if not self._costs:
            return self._fallback_estimate
        window = self._costs[-3:]
        return max(1, int(sum(window) / len(window)))

    def check(self) -> None:
        """预估式检查：剩余预算不够跑一轮就抛异常终止。"""
        estimate = self._estimate_next_round()
        if self.consumed + estimate >= self.limit:
            raise BudgetExceededError(
                f"预估终止: 已消耗 {self.consumed} + 预估 {estimate} "
                f"= {self.consumed + estimate} >= 预算 {self.limit}"
            )

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed)

    @property
    def ratio(self) -> float:
        return self.consumed / self.limit if self.limit > 0 else 1.0


class GraduatedBudget(TokenBudget):
    """分级预算 — 越接近底线越省着用。

    分级策略:
      50%  → SLIM    (切换到滑动窗口)
      70%  → THRIFTY (注入精简提示)
      100% → 终止
    """

    class Tier:
        NORMAL = "normal"
        SLIM = "slim"
        THRIFTY = "thrifty"
        EXHAUSTED = "exhausted"

    TIER_LABEL = {
        Tier.NORMAL: "NORMAL",
        Tier.SLIM: "SLIM",
        Tier.THRIFTY: "THRIFTY",
        Tier.EXHAUSTED: "EXHAUSTED",
    }

    TIER_HINT = {
        Tier.THRIFTY: (
            "[系统提示] Token 预算已消耗 70% 以上，剩余预算紧张。"
            "请用最精简的方式回复，优先给出结论而非详细分析。"
        ),
    }

    def __init__(self, limit: int, default_estimate: int = 300):
        super().__init__(limit, default_estimate)
        self.tier = self.Tier.NORMAL
        self.history: list[tuple[int, str]] = []  # (consumed, tier_label)

    def consume(self, tokens: int) -> None:
        super().consume(tokens)
        self._eval_tier()

    def _eval_tier(self) -> None:
        r = self.ratio
        if r >= 1.0:
            new = self.Tier.EXHAUSTED
        elif r >= 0.7:
            new = self.Tier.THRIFTY
        elif r >= 0.5:
            new = self.Tier.SLIM
        else:
            new = self.Tier.NORMAL
        if new != self.tier:
            self.tier = new
            self.history.append((self.consumed, self.TIER_LABEL[new]))

    def hint(self) -> str | None:
        return self.TIER_HINT.get(self.tier)
