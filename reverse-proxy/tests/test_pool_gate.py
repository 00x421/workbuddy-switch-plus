# -*- coding: utf-8 -*-
"""池级总量闸门（max_total_concurrency）的单元测试。

用 unittest 而不是 pytest 风格：本仓库所有 Python 测试都是
`unittest.TestCase` / `unittest.IsolatedAsyncioTestCase`，裸 `class Xxx:` 不会被收集。

**为什么需要这一项**：账号级 max_concurrency 只管单个号。20 个号 × 每号 3
并发 = 60 个请求同时打到上游，从单号看全都合规，从风控看却是 20 个账号
在同一瞬间一起活跃 —— 这正是「一并发多就报错」的直接成因。
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin import throttle  # noqa: E402
from admin.pool import AccountPool  # noqa: E402
from admin.server import Store  # noqa: E402

API = "sk-test-api-key-000000"
ADMIN = "hyact-admin-token-0000000000"


def credential(name):
    # 形状必须与桌面端 .info 一致：顶层要有 `auth` 和 `account` 两个对象
    # （`Store.validate_credential` 会当面校验，缺一个直接 400）。
    return {"account": {"uid": f"uid-{name}", "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": f"tok-{name}", "refreshToken": f"ref-{name}",
                     "expiresAt": 4102444800000}}


class Fixture(unittest.TestCase):
    """store + 三个账号的最小环境。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "management", root / "auth", API, ADMIN)
        self.ids = [self.store.save_browser_account(credential(n), n)["account_id"]
                    for n in ("one", "two", "three")]
        self.now = 1800000000
        for aid, item in self.store.data["accounts"].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={"X-Domain": "www.codebuddy.cn"})

    def tearDown(self):
        self.tmp.cleanup()

    def pool(self, **kwargs):
        return AccountPool(self.store, clock=lambda: self.now, **kwargs)

    def set_gate(self, value):
        with self.store.lock:
            self.store.data.setdefault("pool", {})["max_total_concurrency"] = value

    def set_status(self, aid, **fields):
        """写账号状态。`account_status` 是懒建的，测试里要自己 setdefault。"""
        with self.store.lock:
            self.store.data.setdefault("account_status", {}).setdefault(aid, {}).update(fields)


class GateConfigTests(Fixture):
    def test_default_is_unlimited(self):
        """缺省必须是 0（不限制），否则升级后行为立刻变了。"""
        self.assertEqual(throttle.pool_gate(self.store.data.get("pool")), 0)

    def test_missing_key_is_unlimited(self):
        self.assertEqual(throttle.pool_gate({}), 0)

    def test_non_dict_is_unlimited(self):
        for bad in (None, "", [], "abc", 5):
            self.assertEqual(throttle.pool_gate(bad), 0)

    def test_garbage_value_falls_back_to_unlimited(self):
        """配置是手填的，读配置不该让服务起不来。"""
        for bad in ("abc", None, "", [], {}):
            self.assertEqual(throttle.pool_gate({"max_total_concurrency": bad}), 0)

    def test_negative_clamped_to_zero(self):
        self.assertEqual(throttle.pool_gate({"max_total_concurrency": -5}), 0)

    def test_over_cap_clamped(self):
        got = throttle.pool_gate({"max_total_concurrency": 99999})
        self.assertEqual(got, throttle.MAX_CONCURRENCY)

    def test_valid_value_read_as_is(self):
        self.assertEqual(throttle.pool_gate({"max_total_concurrency": 8}), 8)


class TotalInflightTests(Fixture):
    def test_empty_is_zero(self):
        self.assertEqual(throttle.total_inflight({}), 0)
        self.assertEqual(throttle.total_inflight(None), 0)

    def test_sums_across_accounts(self):
        statuses = {self.ids[0]: {"inflight": 2}, self.ids[1]: {"inflight": 3},
                    self.ids[2]: {}}
        self.assertEqual(throttle.total_inflight(statuses), 5)

    def test_ignores_garbage(self):
        statuses = {self.ids[0]: {"inflight": "x"}, self.ids[1]: {"inflight": -4},
                    self.ids[2]: {"inflight": 1}}
        self.assertEqual(throttle.total_inflight(statuses), 1)


class GateAcquireTests(Fixture):
    def test_no_gate_allows_many(self):
        """不配闸门时行为与改造前一模一样：能占多少占多少。"""
        pool = self.pool()
        got = sum(1 for _ in range(50) if pool.acquire(self.ids[0]))
        self.assertEqual(got, 50)

    def test_gate_blocks_across_different_accounts(self):
        """关键语义：闸门是全池共享的，换号也不能绕过去。"""
        self.set_gate(3)
        pool = self.pool()
        granted = [pool.acquire(aid) for aid in self.ids * 2]
        self.assertEqual(sum(1 for g in granted if g), 3,
                         f"应只放行 3 个，实际 {granted}")

    def test_single_account_limit_still_applies(self):
        """闸门不能把账号级上限顶掉。"""
        self.set_gate(100)
        self.set_status(self.ids[0], max_concurrency=2)
        pool = self.pool()
        got = sum(1 for _ in range(10) if pool.acquire(self.ids[0]))
        self.assertEqual(got, 2)

    def test_tighter_of_the_two_wins(self):
        """两项同时配时，先撞到哪个就按哪个算。"""
        self.set_gate(2)
        for aid in self.ids:
            self.set_status(aid, max_concurrency=5)
        pool = self.pool()
        got = sum(1 for aid in self.ids for _ in range(5) if pool.acquire(aid))
        self.assertEqual(got, 2)

    def test_release_frees_gate_slot(self):
        """名额必须能还回去，否则跑满一次服务就永久卡住。"""
        self.set_gate(2)
        pool = self.pool()
        self.assertTrue(pool.acquire(self.ids[0]))
        self.assertTrue(pool.acquire(self.ids[1]))
        self.assertFalse(pool.acquire(self.ids[2]))
        pool.release(self.ids[0])
        self.assertTrue(pool.acquire(self.ids[2]))

    def test_ignore_gate_bypasses_total_but_not_account(self):
        """后台任务（签到/旅行）不该和对话流量抢同一个闸门。"""
        self.set_gate(1)
        pool = self.pool()
        self.assertTrue(pool.acquire(self.ids[0]))
        self.assertFalse(pool.acquire(self.ids[1]))
        self.assertTrue(pool.acquire(self.ids[1], ignore_gate=True))
        # 但账号级上限照样生效。
        self.set_status(self.ids[2], max_concurrency=1)
        self.assertTrue(pool.acquire(self.ids[2], ignore_gate=True))
        self.assertFalse(pool.acquire(self.ids[2], ignore_gate=True))

    def test_gate_shared_across_threads(self):
        """`acquire` 是同步方法，会被 `asyncio.to_thread` 从多个线程并发调。

        读改写没有锁的话，两个线程可能同时看到 total=gate-1 都放过去，
        实际超出上限。这里用 8 个线程抢 5 个名额验证不会超发。
        """
        self.set_gate(5)
        pool = self.pool()
        granted = []
        lock = threading.Lock()

        def worker():
            ok = pool.acquire(self.ids[0])
            with lock:
                granted.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sum(1 for g in granted if g), 5,
                         f"闸门在并发下失效：放行了 {sum(1 for g in granted if g)} 个")

    def test_unknown_account_still_counts(self):
        """账号被删但 status 还留着 inflight 时，不该白送一个名额。"""
        self.set_gate(1)
        self.set_status("gone-account", inflight=1)
        pool = self.pool()
        self.assertFalse(pool.acquire(self.ids[0]))


if __name__ == "__main__":
    unittest.main()
