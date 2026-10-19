"""输出速率（tok/s）聚合。

为什么单独锁死这一块：

1. **分母只能是"生成本身"那段时间**。拿总耗时当分母，会把排队、鉴权、首字等待
   全部算进去，算出来的速率比真实值低一大截，而且低多少取决于网络——这种数字
   比没有数字更坏，它看起来是精确的。
2. **失败请求必须排除**。一次 429 重试三次才成功，耗时会翻好几倍，混进均值
   直接把速率压成噪声。
3. **样本量必须跟着速率一起给**。2 次调用和 200 次调用算出来的 tok/s 长得一样，
   不标样本数界面就没法如实显示。
4. **老 state.json 没有 gen_ms / gen_samples 键**。升级后第一次跑不能 KeyError，
   也不能把存量当 0 速率报出去。
"""
import threading
import unittest

import pytest

from admin import usage


class MemoryStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}


def _rows(store, days=30):
    return {r["model"]: r for r in usage.throughput_rows(store, days=days)}


# --------------------------------------------------------------- 分母

class GenerationWindowTest(unittest.TestCase):
    """`generation_ms()` 的四条分支。"""

    def test_streaming_subtracts_first_token(self):
        assert usage.generation_ms(1000, 200) == 800

    def test_non_streaming_uses_whole_duration(self):
        # 首字时间为 0 = 非流式。整段耗时里绝大部分是生成，只能拿它顶替。
        # 这样算出来偏低，但不会凭空捏造一个数字。
        assert usage.generation_ms(1000, 0) == 1000

    def test_first_token_after_total_is_rejected(self):
        # 两个数不是同一次计时（重试 / 时钟错位 / 只记了一半），相减是负数。
        assert usage.generation_ms(500, 900) == 0
        assert usage.generation_ms(500, 500) == 0

    def test_zero_or_garbage_duration_is_zero(self):
        assert usage.generation_ms(0, 0) == 0
        assert usage.generation_ms(None, None) == 0
        assert usage.generation_ms("abc", "def") == 0
        assert usage.generation_ms(-100, 10) == 0


# --------------------------------------------------------------- 样本入选

class SampleSelectionTest(unittest.TestCase):

    def test_successful_call_with_tokens_counts(self):
        store = MemoryStore()
        usage.record(store, now=1791732600.0, status=200, tokens=800,
                     duration_ms=1000, first_token_ms=200, model="m1")
        row = _rows(store)["m1"]
        assert row["throughput_samples"] == 1
        # 800 token / 0.8 秒
        assert row["tokens_per_sec"] == pytest.approx(1000.0, abs=0.01)

    def test_failed_call_does_not_contribute_generation_time(self):
        store = MemoryStore()
        usage.record(store, now=1791732600.0, status=200, tokens=100,
                     duration_ms=1000, first_token_ms=0, model="m1")
        # 429 带 token 带耗时，但它是重试后的残值，不能进生成时间
        usage.record(store, now=1791732600.0, status=429, tokens=100,
                     duration_ms=30000, first_token_ms=0, model="m1")
        row = _rows(store)["m1"]
        assert row["throughput_samples"] == 1
        # 分子是 slot 的全部输出 token（失败那次的 token 仍是上游真吐出来的），
        # 分母只有成功那次的 1000ms —— 所以在账面上会被抬高。
        # 这是刻意的取舍：宁可偏高也不能让 30 秒的重试耗时污染分母。
        assert row["tokens_per_sec"] == pytest.approx(200.0, abs=0.01)
        # 关键防线：如果失败请求混进了分母，这里会是 200/31 ≈ 6.45
        assert row["tokens_per_sec"] > 100.0

    def test_zero_token_call_is_excluded(self):
        store = MemoryStore()
        usage.record(store, now=1791732600.0, status=200, tokens=0,
                     duration_ms=5000, first_token_ms=100, model="m1")
        row = _rows(store)["m1"]
        assert row["throughput_samples"] == 0
        assert row["tokens_per_sec"] is None
        # 但它仍然是"这个模型被调用过"的证据，调用数照记
        assert row["calls"] == 1

    def test_model_with_only_bad_samples_reports_none_not_zero(self):
        store = MemoryStore()
        usage.record(store, now=1791732600.0, status=500, tokens=50,
                     duration_ms=1000, first_token_ms=0, model="broken")
        row = _rows(store)["broken"]
        assert row["tokens_per_sec"] is None
        assert row["throughput_samples"] == 0
        assert row["failed"] == 1


# --------------------------------------------------------------- 跨桶累加

class AggregationTest(unittest.TestCase):

    def test_accumulates_across_days(self):
        store = MemoryStore()
        base = 1791732600.0
        for i in range(3):
            usage.record(store, now=base + i * 86400, status=200, tokens=300,
                         duration_ms=1000, first_token_ms=0, model="m1")
        row = _rows(store)["m1"]
        assert row["throughput_samples"] == 3
        # 900 token / 3 秒
        assert row["tokens_per_sec"] == pytest.approx(300.0, abs=0.01)
        assert row["calls"] == 3
        assert row["tokens"] == 900

    def test_window_caps_the_days_used(self):
        store = MemoryStore()
        base = 1791732600.0
        for i in range(10):
            usage.record(store, now=base + i * 86400, status=200, tokens=100,
                         duration_ms=1000, first_token_ms=0, model="m1")
        # 只取最后 2 天 → 2 次样本
        assert _rows(store, days=2)["m1"]["throughput_samples"] == 2

    def test_sorted_by_rate_desc_with_unmeasured_last(self):
        store = MemoryStore()
        now = 1791732600.0
        usage.record(store, now=now, status=200, tokens=1000, duration_ms=1000,
                     first_token_ms=0, model="fast")
        usage.record(store, now=now, status=200, tokens=100, duration_ms=1000,
                     first_token_ms=0, model="slow")
        usage.record(store, now=now, status=500, tokens=10, duration_ms=1000,
                     first_token_ms=0, model="unknown")
        names = [r["model"] for r in usage.throughput_rows(store)]
        assert names == ["fast", "slow", "unknown"]

    def test_snapshot_carries_throughput(self):
        store = MemoryStore()
        usage.record(store, now=1791732600.0, status=200, tokens=500,
                     duration_ms=1000, first_token_ms=0, model="m1")
        snap = usage.snapshot(store)
        assert "throughput" in snap
        assert snap["throughput"][0]["model"] == "m1"
        assert snap["throughput"][0]["tokens_per_sec"] == pytest.approx(500.0, abs=0.01)


# --------------------------------------------------------------- 升级兼容

class UpgradeCompatTest(unittest.TestCase):
    """老 state.json 里的 by_model slot 没有 gen_ms / gen_samples。"""

    def test_legacy_slot_does_not_break_record(self):
        store = MemoryStore()
        store.data["usage_stats"] = {
            "daily": {"2026-10-11": {
                "calls": 2, "failed": 0, "tokens": 50, "credit": 0.0,
                "duration_ms": 2000, "first_token_ms": 0,
                "by_model": {"m1": {"calls": 2, "failed": 0, "tokens": 50, "credit": 0.0}},
                "by_key": {}, "by_status": {"200": 2},
            }},
            "hourly": {},
        }
        usage.record(store, now=1791732600.0, status=200, tokens=100,
                     duration_ms=1000, first_token_ms=0, model="m1")
        slot = store.data["usage_stats"]["daily"]["2026-10-11"]["by_model"]["m1"]
        assert slot["calls"] == 3
        assert slot["tokens"] == 150
        # 新记录的 1000ms 是唯一有效样本
        assert slot["gen_ms"] == 1000
        assert slot["gen_samples"] == 1

    def test_legacy_slot_reads_as_no_rate(self):
        store = MemoryStore()
        store.data["usage_stats"] = {
            "daily": {"2026-10-11": {
                "calls": 1, "failed": 0, "tokens": 50, "credit": 0.0,
                "duration_ms": 1000, "first_token_ms": 0,
                "by_model": {"m1": {"calls": 1, "failed": 0, "tokens": 50, "credit": 0.0}},
                "by_key": {}, "by_status": {"200": 1},
            }},
            "hourly": {},
        }
        row = _rows(store)["m1"]
        # 存量 token 照常统计，但速率算不出来 —— 报 None 而不是拿总耗时瞎凑
        assert row["tokens"] == 50
        assert row["tokens_per_sec"] is None
        assert row["throughput_samples"] == 0

    def test_snapshot_of_empty_store_has_empty_throughput(self):
        assert usage.snapshot(MemoryStore())["throughput"] == []
