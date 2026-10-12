"""兑换码 / 充值模块的测试。

这个文件里最要紧的不是「能不能兑换」，而是**同一张码兑两次会不会都成功**。
并发下读改写不原子 = 余额凭空翻倍，而且没有任何报错。
"""

import threading
import time
import unittest

from fastapi import HTTPException

from admin import redemption as rd


class FakeStore:
    """够用的 store 替身：真锁 + 真 save。

    `lock` 必须是真的可重入锁 —— 并发测试要验的就是锁的作用，
    用假锁测等于什么都没测。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}
        self.saves = 0

    def save(self):
        self.saves += 1


class MoneyTest(unittest.TestCase):
    def test_milli_round_trip(self):
        """能精确到厘的金额必须原样往返。"""
        for credit in (0, 1, 0.001, 12.345, 100, 0.5):
            with self.subTest(credit=credit):
                self.assertEqual(rd.from_milli(rd.to_milli(credit)), credit)

    def test_sub_milli_precision_rounds_half_up(self):
        """比「厘」还细的金额会被量化 —— 这是**有意**的，不是精度损失。

        钱的存储单位就是厘，12.3456 积分不存在。量化必须可预期（四舍五入），
        而不是「有时候向上有时候向下」—— 后者会让对账永远差几分钱。
        """
        self.assertEqual(rd.to_milli(12.3456), 12346)
        self.assertEqual(rd.from_milli(12346), 12.346)
        self.assertEqual(rd.to_milli(12.3454), 12345)
        self.assertEqual(rd.to_milli(0.0004), 0)

    def test_float_drift_is_avoided(self):
        """0.1 + 0.2 用浮点累加会漂；用厘累加不会。"""
        store = FakeStore()
        for _ in range(3):
            rd.grant(store, "u", 0.1)
        self.assertEqual(rd.balance_milli(store, "u"), 300)
        self.assertEqual(rd.from_milli(rd.balance_milli(store, "u")), 0.3)

    def test_garbage_amounts_do_not_crash(self):
        for bad in (None, "abc", "", [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(rd.to_milli(bad), 0)


class CodeNormalizeTest(unittest.TestCase):
    def test_accepts_what_a_human_would_type(self):
        """人要手抄这串码：大小写、空格、漏前缀都该认。"""
        code = "WB-ABCDE-FGHJK-MNPQR-STUVW"
        base = rd.normalize_code(code)
        for variant in (code.lower(), code.replace("-", ""),
                        " " + code + " ", "wb-abcde-fghjk-mnpqr-stuvw",
                        "ABCDE-FGHJK-MNPQR-STUVW"):
            with self.subTest(variant=variant):
                self.assertEqual(rd.normalize_code(variant), base)

    def test_new_code_shape_is_readable(self):
        code = rd.new_code()
        self.assertTrue(code.startswith("WB-"))
        self.assertEqual(len(code.split("-")), 5)
        # 易混字符不能出现，否则用户抄错还会以为自己没抄错。
        for ch in "01OIL":
            self.assertNotIn(ch, code)

    def test_codes_do_not_repeat(self):
        seen = {rd.new_code() for _ in range(2000)}
        self.assertEqual(len(seen), 2000)


class RedeemTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        batch = rd.create_batch(self.store, count=2, credit=10, actor="admin")
        self.codes = batch["codes"]

    def test_redeem_credits_the_balance(self):
        result = rd.redeem(self.store, self.codes[0], "alice")
        self.assertEqual(result["credit"], 10)
        self.assertEqual(result["balance"], 10)
        self.assertEqual(rd.balance_milli(self.store, "alice"), 10 * rd.MONEY)

    def test_the_same_code_cannot_be_redeemed_twice(self):
        rd.redeem(self.store, self.codes[0], "alice")
        with self.assertRaises(HTTPException):
            rd.redeem(self.store, self.codes[0], "bob")
        self.assertEqual(rd.balance_milli(self.store, "bob"), 0)

    def test_uppercase_and_spacing_variants_hit_the_same_code(self):
        rd.redeem(self.store, self.codes[0], "alice")
        with self.assertRaises(HTTPException):
            rd.redeem(self.store, " " + self.codes[0].lower() + " ", "alice")

    def test_a_nonexistent_code_is_rejected(self):
        with self.assertRaises(HTTPException):
            rd.redeem(self.store, "WB-ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ", "alice")
        with self.assertRaises(HTTPException):
            rd.redeem(self.store, "", "alice")

    def test_expired_code_is_rejected_and_does_not_pay(self):
        batch = rd.create_batch(self.store, count=1, credit=5,
                                expires_at=int(time.time()) + 1)
        code = batch["codes"][0]
        # 直接把过期时间拨到过去，比 sleep 稳。
        for row in self.store.data["redemption_codes"].values():
            if row.get("batch") == batch["batch"]:
                row["expires_at"] = int(time.time()) - 1
        with self.assertRaises(HTTPException):
            rd.redeem(self.store, code, "alice")
        self.assertEqual(rd.balance_milli(self.store, "alice"), 0)

    def test_disabled_code_is_rejected(self):
        for row in self.store.data["redemption_codes"].values():
            if row.get("batch") and row.get("milli") == 10 * rd.MONEY:
                row["enabled"] = False
                break
        with self.assertRaises(HTTPException):
            rd.redeem(self.store, self.codes[0], "alice")


class ConcurrencyTest(unittest.TestCase):
    def test_one_code_redeemed_from_many_threads_pays_exactly_once(self):
        """**这个模块最要紧的一条测试。**

        同一张码被 16 个线程同时兑。如果核销不是原子的，多个线程都会读到
        `redeemed_at is None` 然后都放行 —— 余额翻 16 倍，且全程没有任何报错。
        唯一能发现的途径就是这条测试。
        """
        store = FakeStore()
        batch = rd.create_batch(store, count=1, credit=100)
        code = batch["codes"][0]

        ok, failed = [], []
        barrier = threading.Barrier(16)

        def worker(i):
            try:
                barrier.wait(timeout=10)   # 尽量让 16 个线程同时冲进去
            except threading.BrokenBarrierError:
                pass
            try:
                rd.redeem(store, code, f"user{i}")
                ok.append(i)
            except HTTPException:
                failed.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(ok), 1, f"应该有且只有 1 次兑换成功，实际 {len(ok)} 次")
        self.assertEqual(len(failed), 15)
        total = sum(int(v.get("milli", 0))
                    for v in store.data["balances"].values())
        self.assertEqual(total, 100 * rd.MONEY, "总余额不等于面额 —— 有人重复兑了")

    def test_concurrent_grants_do_not_lose_updates(self):
        """并发加钱不能丢更新：`+=` 不加锁会少加。"""
        store = FakeStore()
        barrier = threading.Barrier(16)

        def worker():
            try:
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
            rd.grant(store, "u", 1)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(rd.balance_milli(store, "u"), 16 * rd.MONEY)


class SpendTest(unittest.TestCase):
    def test_spend_refuses_to_go_negative(self):
        """扣成负数 = 用户不知情地欠债。必须报错。"""
        store = FakeStore()
        rd.grant(store, "u", 5)
        rd.spend(store, "u", 3)
        self.assertEqual(rd.balance_milli(store, "u"), 2 * rd.MONEY)
        with self.assertRaises(HTTPException):
            rd.spend(store, "u", 3)
        self.assertEqual(rd.balance_milli(store, "u"), 2 * rd.MONEY, "失败时不该扣钱")


class BatchTest(unittest.TestCase):
    def test_batch_size_limits(self):
        store = FakeStore()
        for bad in (0, -1, rd.MAX_BATCH + 1, "x", None):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException):
                    rd.create_batch(store, count=bad, credit=1)

    def test_zero_and_negative_face_value_rejected(self):
        store = FakeStore()
        for bad in (0, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException):
                    rd.create_batch(store, count=1, credit=bad)

    def test_past_expiry_rejected(self):
        store = FakeStore()
        with self.assertRaises(HTTPException):
            rd.create_batch(store, count=1, credit=1, expires_at=int(time.time()) - 10)

    def test_only_hashes_are_stored(self):
        """明文绝不能落盘 —— 导出码库不该等于把余额送人。"""
        store = FakeStore()
        batch = rd.create_batch(store, count=3, credit=1)
        stored = store.data["redemption_codes"]
        for code in batch["codes"]:
            norm = rd.normalize_code(code)
            self.assertNotIn(norm, stored, "明文被当成键存进去了")
            self.assertNotIn(code, str(stored), "明文出现在库内容里")
        # 但用归一化后的码能查到
        self.assertEqual(len(stored), 3)


class FlowTest(unittest.TestCase):
    def test_flows_record_the_running_balance(self):
        store = FakeStore()
        rd.grant(store, "u", 10, kind="recharge")
        rd.spend(store, "u", 4)
        rows = rd.list_flows(store, username="u")
        # list_flows 倒序：最新在前
        self.assertEqual(rows[0]["kind"], "spend")
        self.assertEqual(rows[0]["milli"], -4000)
        self.assertEqual(rows[0]["balance"], 6 * rd.MONEY)
        self.assertEqual(rows[1]["balance"], 10 * rd.MONEY)

    def test_flow_log_is_bounded(self):
        store = FakeStore()
        for i in range(rd.FLOW_LIMIT + 50):
            rd.grant(store, "u", 0.001, note=f"n{i}")
        self.assertLessEqual(len(store.data["balance_flows"]), rd.FLOW_LIMIT)


class StatsTest(unittest.TestCase):
    def test_stats_separate_issued_from_outstanding(self):
        store = FakeStore()
        batch = rd.create_batch(store, count=4, credit=10)
        rd.redeem(store, batch["codes"][0], "alice")
        s = rd.stats(store)
        self.assertEqual(s["codes_total"], 4)
        self.assertEqual(s["codes_used"], 1)
        self.assertEqual(s["credit_issued"], 10)
        # 还没兑出去的那 3 张是「未核销」而不是「已发放」——
        # 混在一起会让账面看起来多发了 3 倍的钱。
        self.assertEqual(s["credit_outstanding"], 30)
        self.assertEqual(s["balance_total"], 10)


if __name__ == "__main__":
    unittest.main()
