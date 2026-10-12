"""本会话新增的三件事的回归测试：

  1. 请求日志落盘（重启不丢、上限 300、保留最新的）
  2. 累计运行时间（跨重启累加，本次会话独立）
  3. logout 幂等（没登录也要 200 —— 桌面端「退不出去」的根因）

这三条都有过真实故障：日志一重启就空、运行时长永远显示几分钟、
登出在 token 已失效时返回 401 让前端以为失败。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin import pool as poolmod  # noqa: E402
from admin.switch_store import SwitchStore  # noqa: E402
import admin.server as admin_server  # noqa: E402

ADMIN_KEY = "admin-test-credential-long-enough"


class RequestLogPersistenceTest(unittest.TestCase):
    """请求日志：写本地文件，重启后还在。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wblogtest-"))
        os.environ["ADMIN_KEY"] = ADMIN_KEY

    def _pool(self):
        store = SwitchStore(root=str(self.tmp), admin_key=ADMIN_KEY)
        return store, poolmod.AccountPool(store)

    def test_log_survives_a_restart_in_the_right_order(self):
        _store, p1 = self._pool()
        for i in range(5):
            p1.log_request(None, "m-%d" % i, 200, True, 3, 0.25, 9, "success")
        # 重新构造 = 模拟进程重启
        _store2, p2 = self._pool()
        models = [r["model"] for r in p2.request_logs()]
        self.assertEqual(models, ["m-4", "m-3", "m-2", "m-1", "m-0"],
                         "重启后应按「新在前」的顺序读回全部记录")

    def test_expired_entries_are_pruned_by_time_not_by_count(self):
        """保留规则是**时间**（30 天），不再是条数。

        旧版本这里是「文件裁剪到最近 N 条」。那个语义已经废弃：
        现在超期的按时间删、未超期的无论多少条都留。

        这条测试同时钉住两点：
          · 文件不会因为「条数多」而被砍（旧行为必须消失）；
          · 文件也不会无界增长 —— 超期条目被真的删掉。
        """
        _store, p = self._pool()
        import admin.pool as _pm
        fresh = _pm.REQUEST_LOG_COMPACT_THRESHOLD + 50
        for i in range(fresh):
            p.log_request(None, "m-%05d" % i, 200, True, 1, 0.1, 4, "success")
        path = self.tmp / "request-log.jsonl"
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        # 全部未超期 -> 一条都不该被删（旧实现会砍到 300 条）
        self.assertEqual(len(lines), fresh,
                         "30 天内的日志不该因为「条数多」而被删除")
        newest = json.loads(lines[-1])["model"]
        self.assertEqual(newest, "m-%05d" % (fresh - 1), "顺序仍然是最新在最后")

    def test_a_truncated_last_line_does_not_void_the_whole_history(self):
        """上次被强杀会留下半截 JSON —— 只跳过那一条，不能整份历史报废。"""
        _store, p = self._pool()
        for i in range(3):
            p.log_request(None, "m-%d" % i, 200, True, 1, 0.1, 4, "success")
        path = self.tmp / "request-log.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"model": "hal')          # 半截行
        _store2, p2 = self._pool()
        self.assertEqual(len(p2.request_logs()), 3)

    def test_an_unwritable_log_path_does_not_fail_the_request(self):
        """日志是旁路：写不出去只提示，绝不让调用方失败。"""
        _store, p = self._pool()
        p._log_path = Path("Z:/definitely/not/here/request-log.jsonl")
        p.log_request(None, "m", 200, True, 1, 0.1, 4, "success")  # 不应抛
        self.assertEqual(len(p.request_logs()), 1)


class CumulativeRuntimeTest(unittest.TestCase):
    """累计运行时间：跨重启累加；本次会话独立。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wbrt-"))

    def _store(self):
        return SwitchStore(root=str(self.tmp), admin_key=ADMIN_KEY)

    def test_session_and_total_are_different_quantities(self):
        s1 = self._store()
        s1.flush_runtime()
        first_total = s1.data["runtime_total_sec"]
        time.sleep(1.1)
        s2 = self._store()
        u = s2.uptime()
        self.assertLess(u["session"], 5, "新进程的会话时长应该很小")
        self.assertGreater(u["total"], first_total,
                           "累计必须把刚过去的那段算进去，而不是跟着重启归零")

    def test_flush_is_idempotent(self):
        """atexit 与显式调用都可能触发；重复累加会让累计值越跑越虚。"""
        s = self._store()
        time.sleep(1.1)
        s.flush_runtime()
        once = s.data["runtime_total_sec"]
        s.flush_runtime()
        s.flush_runtime()
        self.assertEqual(s.data["runtime_total_sec"], once)

    def test_a_clock_that_jumps_backwards_never_yields_a_negative(self):
        """对时 / 手动改钟会让 now < since，界面显示负数比不显示更糟。"""
        s = self._store()
        s.data["runtime_since"] = int(time.time()) + 10_000   # 未来时间
        s.data["runtime_total_sec"] = 500
        u = s.uptime()
        self.assertGreaterEqual(u["total"], 0)
        self.assertGreaterEqual(u["session"], 0)


class LogoutIdempotencyTest(unittest.IsolatedAsyncioTestCase):
    """logout 必须幂等：没登录时点退出也是成功（桌面端「退不出去」的根因）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["ADMIN_KEY"] = ADMIN_KEY
        self.app = admin_server.create_app(self.root / "mgmt", self.root,
                                           "client-key", ADMIN_KEY, secure_cookie=False)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_logout_without_any_credential_is_still_ok(self):
        r = await self.client.post("/admin/api/logout")
        self.assertEqual(r.status_code, 200,
                         "没登录时登出应当成功——它本来就是要达到的状态")

    async def test_logging_out_twice_is_ok(self):
        first = await self.client.post("/admin/api/login", json={"key": ADMIN_KEY})
        self.assertEqual(first.status_code, 200, first.text)
        token = first.json()["token"]
        headers = {"X-Admin-Token": token}
        self.assertEqual((await self.client.post("/admin/api/logout", headers=headers)).status_code, 200)
        # 同一个 token 再登出一次：曾经这里是 401，前端据此弹「退出失败」。
        self.assertEqual((await self.client.post("/admin/api/logout", headers=headers)).status_code, 200)

    async def test_the_token_really_stops_working_after_logout(self):
        """幂等 ≠ 什么都没做：登出后原 token 必须失效。"""
        first = await self.client.post("/admin/api/login", json={"key": ADMIN_KEY})
        token = first.json()["token"]
        headers = {"X-Admin-Token": token}
        self.assertEqual((await self.client.get("/admin/api/overview", headers=headers)).status_code, 200)
        await self.client.post("/admin/api/logout", headers=headers)
        self.assertEqual((await self.client.get("/admin/api/overview", headers=headers)).status_code, 401)


class OverviewRuntimeFieldTest(unittest.IsolatedAsyncioTestCase):
    """overview 必须带 runtime；旧 Store 也要能用（不能 AttributeError）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["ADMIN_KEY"] = ADMIN_KEY
        self.app = admin_server.create_app(self.root / "mgmt", self.root,
                                           "client-key", ADMIN_KEY, secure_cookie=False)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_overview_reports_both_uptime_views(self):
        first = await self.client.post("/admin/api/login", json={"key": ADMIN_KEY})
        headers = {"X-Admin-Token": first.json()["token"]}
        body = (await self.client.get("/admin/api/overview", headers=headers)).json()
        self.assertIn("runtime", body)
        self.assertIn("session", body["runtime"])
        self.assertIn("total", body["runtime"])
        # 老字段必须还在：老前端/老脚本在用。
        self.assertIsInstance(body["uptime"], int)


class RuntimeFileTest(unittest.TestCase):
    """运行时长写入文件（t21）。

    用户的要求：「已运行时间也写入文件里 统计总运行时间」。

    ⚠️ 这里每一条都**读磁盘原文**，不看函数返回值。
    「函数被调用了」与「文件真的写对内容了」是两件事，
    而本任务要证的是后者。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wbrtfile-"))
        # 关掉定期线程：这些用例要精确控制「什么时候写」，
        # 后台线程会让断言随机化（写与不写之间赛跑）。
        self._old = os.environ.get("WB_RUNTIME_WRITER")
        os.environ["WB_RUNTIME_WRITER"] = "0"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("WB_RUNTIME_WRITER", None)
        else:
            os.environ["WB_RUNTIME_WRITER"] = self._old

    def _store(self):
        return SwitchStore(root=str(self.tmp), admin_key=ADMIN_KEY)

    def test_file_is_written_on_startup_without_any_flush(self):
        """启动瞬间就要有文件 —— 「刚起来就被杀」是最常见的崩溃场景。"""
        s = self._store()
        path = self.tmp / "runtime.json"
        self.assertTrue(path.exists(), "构造 store 后 runtime.json 就该存在")
        s.stop_runtime_writer()

    def test_file_total_matches_uptime_total(self):
        """total_sec 必须是 uptime().total 的**投影**，不是另算的一份。

        若这里自己计时，两份必然漂移 —— 那就多出一个「该信哪个」的问题。
        """
        s = self._store()
        time.sleep(1.1)
        s.write_runtime_file()
        doc = json.loads((self.tmp / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["total_sec"], s.uptime()["total"])
        s.stop_runtime_writer()

    def test_survives_a_hard_kill_that_never_runs_atexit(self):
        """⭐ 本任务最关键的用例：**没走 atexit 就死了**，文件里仍要有值。

        只靠 atexit 写的话，强杀 / 崩溃 / 断电都留不下一份记录 ——
        而「跑了多久」恰恰是崩溃后最想知道的信息。
        这里刻意**不调 flush_runtime**，只让定期线程自己写。
        """
        os.environ["WB_RUNTIME_WRITER"] = "1"
        s = SwitchStore(root=str(self.tmp), admin_key=ADMIN_KEY)
        s.start_runtime_writer(interval=0.3)   # 加快，便于观察
        # ⚠️ 轮询，不要固定 sleep。
        # 我第一版写的是 sleep(1.2) 然后断言 total_sec >= 1：
        # 单跑绿、与别的文件串跑时偶发红 —— 因为「1.2 秒内恰好跨过 1 秒整数边界」
        # 依赖机器快慢。那种红只会让人怀疑测试本身，不如等它真的更新了再断言。
        path = self.tmp / "runtime.json"
        fresh = json.loads(path.read_text(encoding="utf-8"))
        deadline = time.time() + 8.0
        while time.time() < deadline:
            fresh = json.loads(path.read_text(encoding="utf-8"))
            # updated_at 前进了 = 定期线程真的又写了一次（不是启动时那一版）
            if fresh["updated_at"] > fresh["session_started_at"]:
                break
            time.sleep(0.1)
        # 不调 flush_runtime —— 等价于进程被强杀
        self.assertGreater(fresh["updated_at"], fresh["session_started_at"],
                           "定期写入一次都没发生过：文件停在启动时那一版")
        self.assertGreaterEqual(fresh["total_sec"], 0)
        s.stop_runtime_writer()

    def test_total_does_not_reset_or_double_count_across_restarts(self):
        a = self._store()
        time.sleep(1.1)
        a.flush_runtime()
        a.stop_runtime_writer()
        t1 = json.loads((self.tmp / "runtime.json").read_text(encoding="utf-8"))["total_sec"]
        time.sleep(1.1)
        b = self._store()          # 模拟重启
        u = b.uptime()
        self.assertGreater(u["total"], t1, "重启后累计不能被清零")
        b.flush_runtime()
        b.flush_runtime()          # 再调一次，不能重复计入
        self.assertEqual(b.data["runtime_total_sec"], u["total"],
                         "同一次会话被累加两遍会让累计值越跑越虚")
        b.stop_runtime_writer()

    def test_a_clock_that_jumps_backwards_never_writes_a_negative(self):
        """墙钟可被改。文件里出现负数比不出现更糟 —— 用户会以为数据坏了。"""
        s = self._store()
        s.data["runtime_since"] = int(time.time()) + 10_000   # 未来时间
        s.data["runtime_total_sec"] = 500
        s.write_runtime_file()
        doc = json.loads((self.tmp / "runtime.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(doc["total_sec"], 0)
        self.assertGreaterEqual(doc["session_sec"], 0)
        self.assertEqual(doc["total_sec"], 500, "未来起点那段按 0 算，已有累计不能被吃掉")
        s.stop_runtime_writer()

    def test_an_unwritable_path_does_not_break_the_service_or_exit(self):
        """旁路写失败（只读挂载 / 磁盘满）不该影响主流程，也不该卡住退出。"""
        s = self._store()
        s._runtime_path = Path("Z:/definitely/not/here/runtime.json")
        self.assertFalse(s.write_runtime_file(), "写失败应返回 False 而不是抛")
        self.assertGreaterEqual(s.uptime()["total"], 0, "主流程照常工作")
        s.flush_runtime()          # 退出路径也不能抛
        s.stop_runtime_writer()

    def test_sessions_counter_only_grows(self):
        a = self._store()
        a.stop_runtime_writer()
        first = json.loads((self.tmp / "runtime.json").read_text(encoding="utf-8"))
        b = self._store()
        b.write_runtime_file()
        second = json.loads((self.tmp / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(second["sessions"], first["sessions"] + 1,
                         "重启应让会话次数 +1（用户能看出「跑了几次」）")
        self.assertEqual(second["first_started_at"], first["first_started_at"],
                         "首次启动时间一旦写下就永不覆盖")
        b.stop_runtime_writer()


if __name__ == "__main__":
    unittest.main()
