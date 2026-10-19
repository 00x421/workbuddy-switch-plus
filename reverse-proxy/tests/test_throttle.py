"""账号级流量整形：权重 / 并发上限 / 最小间隔。

这一层的意义是**事前防封**。原来的两个旋钮（池级 spread、收到 429 后冷却）
一个是全池一刀切，一个是事后惩罚——收到 429 时上游已经记账了。

三条硬约束，每条都有对应测试：
  1. 三项都不配时必须与改造前完全一致（升级兼容，存量 state.json 没这些字段）
  2. 限制是软约束：全池都被卡住时宁可选一个放过去，也不能报「暂无可用账号」
  3. weight=0 是「不参与自动轮转」，不是「禁止使用」，更不能让别的号一起饿死
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from admin import throttle  # noqa: E402


class LimitsTests(unittest.TestCase):
    def test_missing_everything_means_unlimited(self):
        """存量 state.json 里没有任何这些字段，必须读成「不限制」。"""
        self.assertEqual(throttle.limits_of({}),
                         {"weight": 0, "max_concurrency": 0, "min_interval": 0.0})
        self.assertEqual(throttle.limits_of(None),
                         {"weight": 0, "max_concurrency": 0, "min_interval": 0.0})

    def test_illegal_values_fall_back_to_unlimited(self):
        """配置是用户手填的，读配置不该让服务起不来。"""
        for bad in ("abc", None, [], {}, object()):
            got = throttle.limits_of({"max_concurrency": bad, "min_interval": bad})
            self.assertEqual(got["max_concurrency"], 0)
            self.assertEqual(got["min_interval"], 0.0)

    def test_negative_and_nan_become_unlimited(self):
        self.assertEqual(throttle.limits_of({"max_concurrency": -5})["max_concurrency"], 0)
        self.assertEqual(throttle.limits_of({"min_interval": float("nan")})["min_interval"], 0.0)
        self.assertEqual(throttle.limits_of({"min_interval": float("inf")})["min_interval"], 0.0)

    def test_caps_clamp_overflow(self):
        """填错单位（把毫秒当秒）时不能真的把号锁死一小时以外。"""
        self.assertEqual(
            throttle.limits_of({"max_concurrency": 99999})["max_concurrency"],
            throttle.MAX_CONCURRENCY)
        self.assertEqual(
            throttle.limits_of({"min_interval": 10 ** 9})["min_interval"],
            throttle.MAX_MIN_INTERVAL)


class WeightTests(unittest.TestCase):
    def test_default_weight_is_one_not_zero(self):
        """最容易搞反的一处：缺省必须是 1（同权），显式 0 才是「不参与」。"""
        self.assertEqual(throttle.effective_weight({}), 1.0)
        self.assertEqual(throttle.effective_weight(None), 1.0)
        self.assertEqual(throttle.effective_weight({"weight": 0}), 0.0)
        self.assertEqual(throttle.effective_weight({"weight": "3"}), 3.0)

    def test_bad_weight_falls_back_to_one(self):
        for bad in ("abc", [], object()):
            self.assertEqual(throttle.effective_weight({"weight": bad}), 1.0)


class ConcurrencyTests(unittest.TestCase):
    def test_no_limit_never_saturates(self):
        self.assertFalse(throttle.is_saturated({}))
        self.assertFalse(throttle.is_saturated({"inflight": 9999}))

    def test_saturates_at_limit(self):
        self.assertFalse(throttle.is_saturated({"max_concurrency": 2, "inflight": 1}))
        self.assertTrue(throttle.is_saturated({"max_concurrency": 2, "inflight": 2}))
        self.assertTrue(throttle.is_saturated({"max_concurrency": 2, "inflight": 5}))

    def test_penalty_marks_saturated_as_blocked(self):
        hard, _score = throttle.penalty({"max_concurrency": 2, "inflight": 2}, 100.0)
        self.assertTrue(hard)

    def test_busier_account_scores_worse_without_any_limit(self):
        """并发上限没配时也要做自然分摊：越忙越靠后。"""
        _, idle = throttle.penalty({"inflight": 0}, 100.0)
        _, busy = throttle.penalty({"inflight": 3}, 100.0)
        self.assertLess(idle, busy)


class IntervalTests(unittest.TestCase):
    def test_returns_remaining_seconds(self):
        got = throttle.interval_blocked({"min_interval": 60, "last_request": 90}, 100.0)
        self.assertAlmostEqual(got, 50.0)

    def test_passes_after_window(self):
        self.assertEqual(
            throttle.interval_blocked({"min_interval": 60, "last_request": 90}, 200.0), 0.0)

    def test_clock_rollback_does_not_lock_account_forever(self):
        """`last_request` 比 now 还大 = 时钟回拨。按「还要等很久」处理会让号永久卡死。"""
        self.assertEqual(
            throttle.interval_blocked({"min_interval": 60, "last_request": 300}, 100.0), 0.0)

    def test_missing_last_request_never_blocks(self):
        self.assertEqual(throttle.interval_blocked({"min_interval": 60}, 100.0), 0.0)

    def test_interval_marks_blocked(self):
        hard, _ = throttle.penalty({"min_interval": 60, "last_request": 90}, 100.0)
        self.assertTrue(hard)


def _rows(*ids):
    return [{"id": i} for i in ids]


class WeightedOrderTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(throttle.weighted_order([], {}), [])

    def test_all_default_is_even(self):
        seq = throttle.weighted_order(_rows("a", "b", "c"), {})
        self.assertEqual(sorted(seq), ["a", "b", "c"])

    def test_higher_weight_appears_more_often(self):
        seq = throttle.weighted_order(
            _rows("a", "b", "c"),
            {"a": {"weight": 6}, "b": {"weight": 2}, "c": {"weight": 2}})
        self.assertGreater(seq.count("a"), seq.count("b"))
        self.assertEqual(seq[0], "a")

    def test_no_account_ever_starves(self):
        """权重 10:1:1 时，权重 1 的号也不能在一个轮转里完全排不上——
        排不上意味着它的 token 会过期、保活会断，等于被悄悄淘汰。"""
        for w in (1, 3, 10, 100):
            seq = throttle.weighted_order(_rows("a", "b", "c"), {"a": {"weight": w}})
            self.assertEqual(set(seq), {"a", "b", "c"}, f"weight={w} 时有人被饿死：{seq}")

    def test_zero_weight_is_excluded(self):
        """weight=0 的语义是「不参与自动轮转」，不是「排最后」。"""
        seq = throttle.weighted_order(_rows("a", "b", "c"), {"a": {"weight": 0}})
        self.assertNotIn("a", seq)
        self.assertEqual(set(seq), {"b", "c"})

    def test_all_zero_falls_back_to_even(self):
        """全 0 更像是「想临时停掉自动轮转」，此时仍要能跑，
        否则请求会一股脑落到「所有号都被卡住」的兜底分支。"""
        seq = throttle.weighted_order(
            _rows("a", "b", "c"),
            {"a": {"weight": 0}, "b": {"weight": 0}, "c": {"weight": 0}})
        self.assertEqual(set(seq), {"a", "b", "c"})

    def test_offset_rotates_without_losing_anyone(self):
        base = throttle.weighted_order(_rows("a", "b", "c"), {})
        for shift in range(1, 4):
            seq = throttle.weighted_order(_rows("a", "b", "c"), {}, shift)
            self.assertEqual(sorted(seq), sorted(base))

    def test_deterministic(self):
        """调度必须可复现，否则线上出问题没法解释「为什么这次选了 3 号」。"""
        args = (_rows("a", "b", "c"), {"a": {"weight": 2}})
        self.assertEqual(throttle.weighted_order(*args), throttle.weighted_order(*args))

    def test_smooth_weights_probabilities(self):
        got = throttle.smooth_weights([("a", 3), ("b", 1)])
        self.assertAlmostEqual(got[0][1], 0.75)
        self.assertAlmostEqual(got[1][1], 0.25)

    def test_smooth_weights_all_zero_is_even(self):
        got = throttle.smooth_weights([("a", 0), ("b", 0)])
        self.assertAlmostEqual(got[0][1], 0.5)


if __name__ == "__main__":
    unittest.main()
