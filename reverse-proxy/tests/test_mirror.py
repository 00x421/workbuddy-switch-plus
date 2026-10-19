"""镜像加速：重试矩阵 + 退避 + 池配置接线。

锁定的是「什么时候**不该**重试」，因为那才是会出事的半边：
- 已经向下游吐过字节 → 绝不能重放（会在流中间插进第二份完整回答）
- 429 / 402 → 重试只会更快被限
- 401 / 403 → 凭据问题，重试不会变得有权限
- 重试次数用完 → 必须停，否则请求永远不返回
"""
import unittest
from unittest import mock

from admin import mirror, pool


class SettingsTest(unittest.TestCase):
    def test_defaults_are_off(self):
        conf = mirror.settings({})
        self.assertFalse(conf["enabled"])
        self.assertEqual(conf["attempts"], 0)
        self.assertEqual(conf["backoff"], 0.0)

    def test_garbage_input_does_not_raise(self):
        for bad in (None, "x", [], {"retry_attempts": "abc", "retry_backoff": "xyz"}):
            conf = mirror.settings(bad)
            self.assertEqual(conf["attempts"], 0)
            self.assertEqual(conf["backoff"], 0.0)

    def test_caps_are_enforced(self):
        conf = mirror.settings({"retry_enabled": True, "retry_attempts": 99,
                                "retry_backoff": 999.0})
        self.assertEqual(conf["attempts"], mirror.MAX_ATTEMPTS_CAP)
        self.assertEqual(conf["backoff"], mirror.MAX_BACKOFF_CAP)

    def test_negative_clamps_to_zero(self):
        conf = mirror.settings({"retry_attempts": -5, "retry_backoff": -1.0})
        self.assertEqual(conf["attempts"], 0)
        self.assertEqual(conf["backoff"], 0.0)


class ShouldRetryTest(unittest.TestCase):
    CFG = {"retry_enabled": True, "retry_attempts": 3, "retry_backoff": 0.1}

    def test_disabled_never_retries(self):
        self.assertFalse(mirror.should_retry(503, 0, {"retry_enabled": False,
                                                      "retry_attempts": 3}))

    def test_zero_attempts_never_retries(self):
        self.assertFalse(mirror.should_retry(503, 0, {"retry_enabled": True,
                                                      "retry_attempts": 0}))

    def test_server_errors_retry(self):
        for code in (500, 502, 503, 504, 408, 425):
            self.assertTrue(mirror.should_retry(code, 0, self.CFG), code)

    def test_connection_error_retries(self):
        self.assertTrue(mirror.should_retry(None, 0, self.CFG))

    def test_rate_limit_and_quota_do_not_retry(self):
        # 429 表达的是「你现在太快了」，加倍发只会更糟。
        for code in (429, 402, 401, 403, 400, 404, 200):
            self.assertFalse(mirror.should_retry(code, 0, self.CFG), code)

    def test_stops_when_attempts_exhausted(self):
        # attempts=3 → 第 0、1 次失败后可以重试，第 2 次失败后不行
        self.assertTrue(mirror.should_retry(503, 0, self.CFG))
        self.assertTrue(mirror.should_retry(503, 1, self.CFG))
        self.assertFalse(mirror.should_retry(503, 2, self.CFG))

    def test_non_numeric_status_does_not_retry(self):
        self.assertFalse(mirror.should_retry("oops", 0, self.CFG))


class DelayTest(unittest.TestCase):
    CFG = {"retry_enabled": True, "retry_attempts": 4, "retry_backoff": 0.2}

    def test_zero_backoff_means_no_wait(self):
        self.assertEqual(mirror.delay_ms(0, {"retry_backoff": 0}), 0)

    def test_exponential_growth(self):
        rng = lambda: 0.0  # noqa: E731  —— 关掉抖动，方便断言基数
        self.assertEqual(mirror.delay_ms(0, self.CFG, rng=rng), 200)
        self.assertEqual(mirror.delay_ms(1, self.CFG, rng=rng), 400)
        self.assertEqual(mirror.delay_ms(2, self.CFG, rng=rng), 800)

    def test_capped_at_max_backoff(self):
        rng = lambda: 0.0  # noqa: E731
        self.assertEqual(mirror.delay_ms(10, self.CFG, rng=rng),
                         int(mirror.MAX_BACKOFF_CAP * 1000))

    def test_jitter_stays_within_quarter(self):
        rng = lambda: 1.0  # noqa: E731
        # 0.2 * 1.25 = 0.25 秒
        self.assertEqual(mirror.delay_ms(0, self.CFG, rng=rng), 250)


class PoolConfigTest(unittest.TestCase):
    """三项必须进 DEFAULT_POOL，否则老 state.json 读不到、界面也存不下去。"""

    def test_defaults_present_and_off(self):
        conf = pool.default_pool()
        self.assertIn("retry_enabled", conf)
        self.assertIn("retry_attempts", conf)
        self.assertIn("retry_backoff", conf)
        self.assertFalse(conf["retry_enabled"])
        self.assertEqual(conf["retry_attempts"], 0)
        self.assertEqual(conf["retry_backoff"], 0.0)

    def test_backfill_keeps_old_state_working(self):
        """老 state.json 的 pool 段没有这三项，加载后必须被补齐且保持关闭。"""
        old = {k: v for k, v in pool.DEFAULT_POOL.items()
               if k not in ("retry_enabled", "retry_attempts", "retry_backoff")}
        merged = dict(pool.DEFAULT_POOL)
        merged.update(old)
        self.assertFalse(merged["retry_enabled"])
        self.assertEqual(mirror.settings(merged)["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
