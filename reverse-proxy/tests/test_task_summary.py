"""任务流水汇总（`task_summary`）。

明细接口（`task_logs`）只能给"最近 200 条"，要回答"今天签到成功几个号"
得自己数。这里锁的是：
- 窗口按自然时间切，不是"最近 N 条"
- 四类任务即使一条记录都没有也要出现在结果里（否则界面会缺行）
- 未来新增的类别不能被吞掉
- 成功率的分母是总数，不是成功数
"""
import unittest

from admin import pool


class Fixture(unittest.TestCase):
    def setUp(self):
        self.bot = pool.AccountPool.__new__(pool.AccountPool)
        self.now = 1791732600.0
        self.bot.clock = lambda: self.now
        self.bot.store = type("S", (), {})()
        self.bot.store.lock = __import__("threading").RLock()
        self.bot.store.data = {"checkin_logs": []}

    def add(self, kind, ok, age_hours=0, aid="a1"):
        self.bot.store.data["checkin_logs"].append(
            {"time": int(self.now - age_hours * 3600), "id": aid, "name": aid,
             "kind": kind, "ok": ok, "message": "", "source": "manual"})


class SummaryTest(Fixture):
    def test_empty_still_lists_all_kinds(self):
        out = self.bot.task_summary(24)
        self.assertEqual([r["kind"] for r in out["kinds"]], list(pool.TASK_KINDS))
        for row in out["kinds"]:
            self.assertEqual(row["total"], 0)
            self.assertEqual(row["success_rate"], 0.0)
            self.assertIsNone(row["last"])

    def test_counts_and_rate(self):
        self.add("checkin", True)
        self.add("checkin", True, age_hours=1)
        self.add("checkin", False, age_hours=2)
        rows = {r["kind"]: r for r in self.bot.task_summary(24)["kinds"]}
        self.assertEqual(rows["checkin"]["total"], 3)
        self.assertEqual(rows["checkin"]["ok"], 2)
        self.assertEqual(rows["checkin"]["failed"], 1)
        # 分母是总数 3，不是成功数 2
        self.assertAlmostEqual(rows["checkin"]["success_rate"], 0.6667, places=4)

    def test_window_excludes_old_entries(self):
        self.add("travel", True)
        self.add("travel", False, age_hours=48)
        rows = {r["kind"]: r for r in self.bot.task_summary(24)["kinds"]}
        self.assertEqual(rows["travel"]["total"], 1)
        self.assertEqual(rows["travel"]["ok"], 1)

    def test_last_is_the_newest_time(self):
        self.add("keepalive", True, age_hours=5)
        self.add("keepalive", False, age_hours=1)
        rows = {r["kind"]: r for r in self.bot.task_summary(24)["kinds"]}
        self.assertEqual(rows["keepalive"]["last"], int(self.now - 3600))

    def test_unknown_kind_is_not_swallowed(self):
        self.add("brand_new_kind", True)
        rows = {r["kind"]: r for r in self.bot.task_summary(24)["kinds"]}
        self.assertEqual(rows["brand_new_kind"]["total"], 1)
        # 原有四类仍然在
        for kind in pool.TASK_KINDS:
            self.assertIn(kind, rows)

    def test_hours_is_echoed_and_clamped(self):
        self.assertEqual(self.bot.task_summary(0)["hours"], 1)
        self.assertEqual(self.bot.task_summary(1000)["hours"], 1000)

    def test_missing_kind_field_defaults_to_checkin(self):
        self.bot.store.data["checkin_logs"].append(
            {"time": int(self.now), "id": "a1", "name": "a1", "ok": True})
        rows = {r["kind"]: r for r in self.bot.task_summary(24)["kinds"]}
        self.assertEqual(rows["checkin"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
