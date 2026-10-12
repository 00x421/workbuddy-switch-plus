"""AUDIT - admin/pool.py. Reproductions for audit findings only."""
import threading
import unittest

from admin import usage
from admin.pool import REQUEST_LOG_MEMORY_LIMIT, AccountPool


class MemoryStore:
    """Minimal Store standing in for SwitchStore."""

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"pool": {}, "account_status": {}, "session_bindings": {},
                     "checkin_logs": [], "travel_states": {},
                     "usage_stats": usage.default_usage_stats(),
                     "groups": {}, "egress": {}, "accounts": {}}
        self.fail_save = False
        self.save_count = 0

    def save(self):
        if self.fail_save:
            raise OSError("disk full")
        self.save_count += 1

    def account_rows(self):
        return [{"id": k, "name": k, "enabled": True, "status": "ready",
                 "created": 0, "active": False} for k in self.data["accounts"]]

    def manager_for(self, aid, item):
        return object()


def build_accounts(store, n=3, **pool_cfg):
    store.data["pool"] = {"routing": "round_robin", "spread": False,
                          "spread_minutes": 30, "wb2api_base": "",
                          "wb2api_key": ""}
    store.data["pool"].update(pool_cfg)
    for i in range(n):
        store.data["accounts"]["a%d" % i] = {"name": "a%d" % i,
                                             "enabled": True, "created": 0}


class BoundedStructuresTest(unittest.TestCase):
    """BUG-P1 check: nothing in this module may only ever append.

    Audited: checkin_logs (CHECKIN_LOG_LIMIT=500), request_log (deque maxlen=300),
    session_bindings (hard cap 4096 in select()). All three hold - these PASS.
    """

    def test_task_log_stays_bounded(self):
        store = MemoryStore()
        build_accounts(store)
        pool = AccountPool(store)
        for i in range(1200):
            pool.log_task("a0", "checkin", True, "m%d" % i)
        self.assertEqual(len(store.data["checkin_logs"]), 500)

    def test_request_log_stays_bounded(self):
        store = MemoryStore()
        build_accounts(store)
        pool = AccountPool(store)
        # 内存上限是防爆保护（防狂刷把内存吃光），业务规则是"按时间留 30 天"。
        # 断言直接引用常量，避免常量再改时这里又变成一个过期的魔数。
        for i in range(REQUEST_LOG_MEMORY_LIMIT + 500):
            pool.log_request("a0", "m", 200, True, 1, 0.0, 1, "success")
        self.assertEqual(len(pool.request_log), REQUEST_LOG_MEMORY_LIMIT)

    def test_session_bindings_stay_bounded(self):
        store = MemoryStore()
        build_accounts(store)
        pool = AccountPool(store)
        for i in range(5000):
            pool.select("sess-%d" % i, key=None)
        self.assertLessEqual(len(store.data["session_bindings"]), 4096)


class LogRequestSaveFailureTest(unittest.TestCase):
    """BUG-P1 (pool.py:531-535): log_request() discards a store.save() failure.

    Every other persistence point in AccountPool propagates the failure -
    log_task(), update() and flush_usage() all let OSError escape. log_request()
    alone wraps its bookkeeping in a lock and sets usage_dirty, and the actual
    write is deferred to flush_usage(), whose exception is swallowed by tick():

        try:
            self.flush_usage()
        except Exception:  # noqa: BLE001
            pass                                  # pool.py:1086-1089

    Together these two swallows mean a permanently failing state.json write is
    invisible: usage_stats and the request log look fine in memory, the panel
    shows traffic, and nothing survives a restart.
    """

    def test_log_request_does_not_hide_a_failing_save(self):
        store = MemoryStore()
        build_accounts(store)
        pool = AccountPool(store)
        store.fail_save = True
        # log_request defers the write, so it must at least leave the dirty flag
        # set for the next flush rather than silently reporting success.
        pool.log_request("a0", "m", 200, True, 5, 0.5, 10, "success")
        self.assertTrue(pool.usage_dirty,
                        "log_request cleared/lost the dirty flag despite never persisting")
        with self.assertRaises(OSError):
            pool.flush_usage()

    def test_control_log_task_propagates(self):
        """Control: log_task() DOES surface the same failure."""
        store = MemoryStore()
        build_accounts(store)
        pool = AccountPool(store)
        store.fail_save = True
        with self.assertRaises(OSError):
            pool.log_task("a0", "checkin", True, "x")


class TickFlushSwallowTest(unittest.TestCase):
    """tick() 里的用量落盘失败：**必须留痕，但不该打断 tick 循环**。

    audit 原先报的是「tick() 吞掉了 flush_usage() 的异常」。
    那个方向没错（失败确实曾经完全静默），但「往上抛」不是正确的修法：
    tick 还要同步账号状态、刷新额度，这些都是不能停的；
    为了一个落盘失败把整个 tick 循环打断，代价比问题本身大。

    所以契约是两条：
      ① 抛出异常**不能**冒泡出去（tick 的其它工作要继续）；
      ② 但必须在 stderr 留下可查的痕迹 —— 这正是原先缺的那一条。
    """

    def _pool_with_failing_flush(self):
        import asyncio  # noqa: F401
        store = MemoryStore()
        # 没有账号：tick 不做任何按账号的工作，唯一可能失败的就是用量落盘。
        store.data["pool"] = {"routing": "round_robin", "auto_checkin": False,
                              "auto_travel": False, "auto_active": False,
                              "auto_keepalive": False, "checkin_time": "00:00",
                              "wb2api_base": "", "wb2api_key": ""}
        pool = AccountPool(store)
        pool.usage_dirty = True

        def boom():
            raise OSError("disk full")

        pool.flush_usage = boom  # type: ignore[assignment]
        return pool

    def test_tick_survives_a_failing_flush(self):
        """① 落盘失败不能把 tick 掀翻。"""
        import asyncio
        pool = self._pool_with_failing_flush()
        try:
            asyncio.run(pool.tick())
        except OSError as exc:
            self.fail("tick() 不该把 flush 的失败抛出去，那会打断整个 tick 循环：%s" % exc)

    def test_tick_reports_a_failing_flush_on_stderr(self):
        """② 但绝不能静默 —— 这是 usage_stats 唯一的周期性落盘点。"""
        import asyncio
        import contextlib
        import io
        pool = self._pool_with_failing_flush()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            asyncio.run(pool.tick())
        err = captured.getvalue()
        self.assertIn("用量聚合落盘失败", err,
                      "tick() 静默丢掉了 flush_usage() 的失败（stderr 里什么都没有）")
        self.assertIn("disk full", err, "报错信息里应带上原始异常，便于排查")


class SpreadLeaseTest(unittest.TestCase):
    """BUG-P3 check: `spread` must rotate accounts over time. PASSES."""

    def test_lease_rotates_as_the_clock_advances(self):
        store = MemoryStore()
        build_accounts(store, n=3, spread=True)
        now = [1000.0]
        pool = AccountPool(store, clock=lambda: now[0])
        seen = []
        for _ in range(6):
            seen.append(pool.select(None, key=None)[0])
            now[0] += 31 * 60
        self.assertEqual(seen[:3], ["a0", "a1", "a2"])
        self.assertEqual(seen[3], "a0")

    def test_lease_pins_within_the_window(self):
        store = MemoryStore()
        build_accounts(store, n=3, spread=True)
        now = [1000.0]
        pool = AccountPool(store, clock=lambda: now[0])
        first = pool.select(None, key=None)[0]
        for _ in range(5):
            self.assertEqual(pool.select(None, key=None)[0], first)


class RoundRobinDistributionTest(unittest.TestCase):
    """BUG-P4 check: round_robin spreads evenly and cursor updates are
    serialised by store.lock. PASSES."""

    def test_even_distribution_across_threads(self):
        from collections import Counter
        store = MemoryStore()
        build_accounts(store, n=4)
        pool = AccountPool(store)
        seen = []

        def go():
            for _ in range(300):
                seen.append(pool.select(None, key=None)[0])

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        counts = Counter(seen)
        self.assertEqual(len(seen), 2400)
        self.assertEqual(set(counts.values()), {600})


class SelectEvictionTest(unittest.TestCase):
    """BUG-P5 check: the 4096 binding cap evicts instead of raising. PASSES."""

    def test_full_binding_table_still_accepts_a_new_session(self):
        store = MemoryStore()
        build_accounts(store, n=2)
        pool = AccountPool(store)
        for i in range(4096):
            store.data["session_bindings"]["s%d" % i] = {
                "account_id": "a0", "expires": 1000.0 + 86400}
        aid, _mgr = pool.select("fresh-key", key=None)
        self.assertIn("fresh-key", store.data["session_bindings"])
        self.assertLessEqual(len(store.data["session_bindings"]), 4096)
        self.assertIn(aid, {"a0", "a1"})


class CreditsPaginationTest(unittest.TestCase):
    """BUG-P6 check: credits() pagination terminates and sums every package.
    The loop cap is range(1, 101) with PageSize=100, i.e. at most 10 000 packages,
    and it raises BillingError when the pages run out early. PASSES."""

    def test_more_than_one_page_is_aggregated(self):
        import json
        import httpx
        pages = []

        def handler(request):
            page = json.loads(request.content)["PageNumber"]
            pages.append(page)
            accounts = [{"CapacityRemainPrecise": "1", "CapacitySize": "2"}] * (100 if page == 1 else 7)
            return httpx.Response(200, json={"code": 0, "data": {"Response": {"Data": {
                "TotalCount": 107, "Accounts": accounts}}}})

        store = MemoryStore()
        build_accounts(store)
        pool = AccountPool(store)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = pool.credits(client, {})
        self.assertEqual(pages, [1, 2])
        self.assertEqual(result["packages"], 107)
        self.assertEqual(result["remaining"], 107.0)

    def test_truncated_pages_raise_instead_of_under_reporting(self):
        """If upstream stops early the balance must NOT be silently understated."""
        import json
        import httpx
        from admin.pool import BillingError

        def handler(request):
            page = json.loads(request.content)["PageNumber"]
            # Claims 300 total but always returns an empty page after the first.
            accounts = [{"CapacityRemainPrecise": "1", "CapacitySize": "2"}] if page == 1 else []
            return httpx.Response(200, json={"code": 0, "data": {"Response": {"Data": {
                "TotalCount": 300, "Accounts": accounts}}}})

        store = MemoryStore()
        build_accounts(store)
        pool = AccountPool(store)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(BillingError):
                pool.credits(client, {})


if __name__ == "__main__":
    unittest.main()
