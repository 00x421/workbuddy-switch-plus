"""金额换算的回归测试。

这里锁的是一个**真的踩过**的坑：早先 `to_milli` 用
`int(round(float(credit) * MONEY))`，因为 `round()` 是银行家舍入、
而 `float(x) * 1000` 对 `x.xxxx5` 的二进制表示不精确，
约 3.3% 的输入会少算 1 厘（`1.0005` → 1000 而不是 1001）。

这类 bug 的危险之处是**不报错**：余额、流水、统计各差一分，
查账时才发现，而且很难复现（取决于用户输入的巧合）。
所以下面既测边界值，也用大批随机值压一遍。
"""
import random
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin.redemption import MONEY, from_milli, to_milli  # noqa: E402


def decimal_expect(credit) -> int:
    """独立的参照实现：按十进制语义四舍五入到厘。

    刻意用和被测代码不同的写法（先 quantize 到 3 位再乘），
    这样两边同时写错的概率低一些。
    """
    d = Decimal(repr(float(credit))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return int(d * 1000)


class MilliExactnessTest(unittest.TestCase):
    def test_half_milli_rounds_up_not_to_even(self):
        """`x.xxxx5` 必须一律进位。银行家舍入会把它推到偶数上。"""
        cases = {
            0.0005: 1,
            0.0015: 2,
            0.0025: 3,      # 银行家舍入会给 2
            1.0005: 1001,   # 银行家舍入会给 1000
            1.0015: 1002,
            2.0005: 2001,   # 银行家舍入会给 2000
            87.6865: 87687,
            7512.2125: 7512213,
        }
        for credit, expected in cases.items():
            with self.subTest(credit=credit):
                self.assertEqual(to_milli(credit), expected)

    def test_plain_two_decimal_amounts_are_exact(self):
        """用户手输的两位小数（最常见的一条路）不能有任何误差。"""
        for cents in range(1, 5000):
            credit = cents / 100
            with self.subTest(cents=cents):
                self.assertEqual(to_milli(credit), cents * 10)

    def test_random_amounts_match_decimal_reference(self):
        """压一遍随机金额。旧实现这里会挂 —— 3.3% 的失败率。"""
        rng = random.Random(20251012)
        mismatches = []
        for _ in range(20000):
            value = round(rng.uniform(0.0005, 9999.9995), 4)
            got = to_milli(value)
            want = decimal_expect(value)
            if got != want:
                mismatches.append((value, got, want))
        self.assertEqual(mismatches[:5], [], f"{len(mismatches)} 个金额换算错误")

    def test_round_trip_is_stable(self):
        """厘 → 积分 → 厘 必须回到原值（不能因为格式化丢钱）。"""
        for milli in (1, 10, 999, 1000, 12345, 999999, 123456789):
            with self.subTest(milli=milli):
                self.assertEqual(to_milli(from_milli(milli)), milli)

    def test_zero_and_junk_are_zero(self):
        for junk in (None, "", "abc", [], {}, 0, 0.0):
            with self.subTest(junk=junk):
                self.assertEqual(to_milli(junk), 0)

    def test_below_half_milli_rounds_to_zero(self):
        for value in (0.0001, 0.0004, 0.00049):
            with self.subTest(value=value):
                self.assertEqual(to_milli(value), 0)

    def test_negative_amounts_keep_sign(self):
        self.assertEqual(to_milli(-1.5), -1500)
        self.assertEqual(to_milli(-0.0005), -1)

    def test_money_constant_is_a_thousand(self):
        """MONEY 是「1 积分 = 多少厘」。改了它，全项目的金额语义都变。"""
        self.assertEqual(MONEY, 1000)


if __name__ == "__main__":
    unittest.main()
