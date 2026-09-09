"""测试 pricing.py — 运行这个来验证修复。"""

from pricing import apply_bulk_discount, calculate_discount, format_price


def test_discount_positive():
    """正常折扣：100元打8折应该=80"""
    result = calculate_discount(100, 0.2)
    assert result == 80.0, f"expected 80.0, got {result}"


def test_bulk_discount_large():
    """大数量折扣：单件100元，买50件应该享受8折"""
    result = apply_bulk_discount(100, 50)
    assert result == 80.0, f"expected 80.0, got {result}"


def test_format_price():
    """价格格式化：12.5元应该显示¥12.50"""
    result = format_price(12.5)
    assert result == "¥12.50", f"expected ¥12.50, got {result}"


if __name__ == "__main__":
    failures = 0
    for name, fn in [
        ("test_discount_positive", test_discount_positive),
        ("test_bulk_discount_large", test_bulk_discount_large),
        ("test_format_price", test_format_price),
    ]:
        try:
            fn()
            print(f"✅ {name} PASSED")
        except AssertionError as e:
            print(f"❌ {name} FAILED: {e}")
            failures += 1
    if failures:
        print(f"\n{failures}/3 tests failed — 需要 Agent 修复")
    else:
        print("\nAll tests passed!")
