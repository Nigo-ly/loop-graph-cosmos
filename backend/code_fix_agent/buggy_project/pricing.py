"""破损的定价模块 — 包含 3 个 bug，供 Agent 修复。"""


def calculate_discount(price: float, discount_rate: float) -> float:
    """计算折扣价。Bug: 没处理负折扣。"""
    return price * (1 - discount_rate)


def apply_bulk_discount(unit_price: float, quantity: int) -> float:
    """批量折扣。Bug: 边界条件错误，quantity==10 时除零。"""
    if quantity >= 50:
        return unit_price * 0.8
    elif quantity >= 10:
        return unit_price * 0.9
    elif quantity == 0:
        raise ValueError("Quantity cannot be zero")
    return unit_price


def format_price(price: float) -> str:
    """格式化价格显示。Bug: 精度丢失。"""
    return f"¥{price:.2f}"
