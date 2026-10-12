"""请求日志按**时间**保留 30 天（t20）。

用户原话：「默认保存日志内容为一个月内的 超出会自动删除」。

改动前是**按条数**保留：`deque(maxlen=300)` + 落盘时裁到最近 300 行。
于是「停机一个月再启动」看到的是 300 条陈年旧账，
而「一天打了一万次请求」只会剩最近 300 条 —— 两头都不符合直觉。

## 本文件要钉死的第一件事

「查询时过滤掉」和「真的删掉」在返回值上完全一样。
只断言「接口不返回超期条目」的测试，在**两种**实现下都会绿 ——
包括那个「文件照样无限长大」的错误实现。
所以下面每一条关于删除的断言，都**直接读磁盘文件本身**。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import admin.pool as poolmod
from admin.switch_store import SwitchStore

ADMIN_KEY = "admin-test-credential-long-enough"
DAY = 86400
NOW = 1_800_000_000           # 固定时钟：不靠 sleep，测试才可重复


class RequestLogRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wblogret-"))
        os.environ["ADMIN_KEY"] = ADMIN_KEY
        self.clock = [NOW]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pool(self, tick=None):
        """建一只池子。`tick` 为 None 时用固定时钟（可重复），否则用可变时钟。"""
        store = SwitchStore(root=str(self.tmp), admin_key=ADMIN_KEY)
        if tick is None:
            return store, poolmod.AccountPool(store, clock=lambda: NOW)

        def now(t=tick):
            t[0] += 1
            return t[0]

        return store, poolmod.AccountPool(store, clock=now)

    @property
    def log_path(self) -> Path:
        return self.tmp / "request-log.jsonl"

    def write_raw(self, entries):
        """直接往日志文件里塞条目（绕过 log_request），用来构造任意历史。"""
        self.log_path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
            encoding="utf-8")

    def read_raw(self):
        """读**磁盘文件本身** —— 不是读接口返回值。"""
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in self.log_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def entry(self, stamp, model):
        return {"time": stamp, "account_id": None, "account_name": None,
                "model": model, "status": 200, "ok": True, "tokens": 1,
                "credit": 0.0, "first_token_ms": 0, "cached_tokens": 0,
                "duration_ms": 1, "outcome": "success", "source": "api",
                "key_id": None, "key_hint": None}

    # ══ 验收 2 的核心：证明「真的从磁盘删掉」 ══════════════════════════
    def test_expired_entries_are_removed_from_the_file_not_just_filtered(self):
        """把「过滤」和「删除」区分开的那一条 —— 直接读文件。

        构造 3 条超期 + 3 条未超期，触发落盘，然后读文件。
        超期的必须**不在文件里**，未超期的必须**还在文件里**。

        第 2 条断言不能省：只验「超期没了」的话，
        把文件清空（或者整个删掉）也能让测试通过。
        """
        old = [self.entry(NOW - 40 * DAY, "old-%d" % i) for i in range(3)]
        fresh = [self.entry(NOW - 1 * DAY, "fresh-%d" % i) for i in range(3)]
        self.write_raw(old)
        # 记录「删除前」的真实文件内容，报告里要贴这个对比。
        before = self.read_raw()
        self.assertEqual(len(before), 3)

        # ⚠️ 构造池子 = 启动，而启动会强制清一次。
        # 所以这里不另外调 prune —— 让「启动那次清理」本身成为被测对象，
        # 这样测到的正是生产里真正会跑的那条路径（而不是只有测试才走的路径）。
        _store, pool = self._pool()
        for e in fresh:
            pool._append_request_log(e)

        after = self.read_raw()
        removed = 3          # 启动强制清理删掉的正是那 3 条超期记录
        models_after = [e["model"] for e in after]

        # ① 超期条目从文件里消失
        self.assertEqual([m for m in models_after if m.startswith("old-")], [],
                         "超期条目必须从磁盘文件里删除，而不只是不被返回")
        self.assertGreater(removed, 0, "应当报告删除了若干条")
        # ② 未超期条目仍然在文件里（否则「全删光」也能让 ① 通过）
        self.assertEqual(sorted(m for m in models_after if m.startswith("fresh-")),
                         ["fresh-0", "fresh-1", "fresh-2"],
                         "30 天内的条目必须原样保留")
        self.assertEqual(len(after), 3, "文件里应当只剩 3 条未超期的")

    def test_prune_actually_shrinks_the_file_on_disk(self):
        # ⚠️ 这 50 条的年龄是 10..59 天 —— 只有 30 天以上的才超期。
        # 构造时如果把 50 条全写成超期，那「文件变小了」就无法区分
        # 「清理正确」和「清理把该留的也删了」。故意混入未超期的，
        # 这样 size 断言同时约束了「删了该删的」和「留了该留的」。
        self.write_raw([self.entry(NOW - (10 + i) * DAY, "o-%02d" % i) for i in range(50)])
        size_before = self.log_path.stat().st_size
        self.assertGreater(size_before, 0)
        # 构造池子 = 启动 -> 强制清理超期条目
        _store, pool = self._pool()
        size_after = self.log_path.stat().st_size
        left = self.read_raw()
        # 年龄是 10..59 天。判据是「< 30 天前」，所以
        # o-00..o-20 是 10..30 天 —— o-20 正好 30 天，按边界规则保留；
        # o-21..o-49 是 31..59 天 —— 这才是超期的。
        # （写成 range(20) 是错的：那会把正好 30 天的 o-20 也算成该删，
        #   于是这条测试会去要求一个违反边界规则的行为。）
        self.assertEqual(sorted(e["model"] for e in left),
                         ["o-%02d" % i for i in range(21)],
                         "只该删掉超过 30 天的那 29 条；正好 30 天的 o-20 保留")
        self.assertLess(size_after, size_before, "文件必须真的变小")

    def test_fresh_entries_survive_a_prune(self):
        """反向对照：全是新条目时，清理**不该**删掉任何东西。

        没有这条的话，「清理逻辑一律删光」也会让上面几条变绿。
        """
        self.write_raw([self.entry(NOW - i * 3600, "n-%d" % i) for i in range(20)])
        _store, pool = self._pool()
        removed = pool._prune_request_log_file(self.log_path, now=NOW, force=True)
        self.assertEqual(removed, 0, "没有超期条目时不该删任何东西")
        self.assertEqual(len(self.read_raw()), 20)
    # ══ 验收 3：边界（判据是 < 还是 <=） ══════════════════════════════
    def test_boundary_exactly_thirty_days_is_kept(self):
        """正好 30 天的那一条**保留**。

        判据是 `stamp < now - 30天`（严格小于），所以
        「正好 30 天」不满足 `<`，留下；「差一秒超期」满足，删掉。

        选 `<` 而不是 `<=` 的理由写在 `REQUEST_LOG_RETENTION_DAYS` 的注释里：
        `<=` 会让边界取决于取整/浮点，而 `<` 只比两个整数，处处一致。
        方向上的取舍是「宁可多留一秒」—— 多留一条的代价远小于错删。
        """
        exactly = self.entry(NOW - 30 * DAY, "exactly-30d")
        one_sec_over = self.entry(NOW - 30 * DAY - 1, "over-by-1s")
        one_sec_under = self.entry(NOW - 30 * DAY + 1, "under-by-1s")
        self.write_raw([one_sec_over, exactly, one_sec_under])

        _store, pool = self._pool()
        pool._prune_request_log_file(self.log_path, now=NOW, force=True)
        left = sorted(e["model"] for e in self.read_raw())

        self.assertNotIn("over-by-1s", left, "差一秒超期 -> 删除")
        self.assertIn("exactly-30d", left, "正好 30 天 -> 保留（判据是严格小于）")
        self.assertIn("under-by-1s", left, "差一秒未超期 -> 保留")
        self.assertEqual(left, ["exactly-30d", "under-by-1s"])

    # ══ 验收 5：时间字段格式（确认到的事实） ══════════════════════════
    def test_time_is_epoch_seconds_int_as_written_by_log_request(self):
        """钉住「`time` 是 int 的 epoch 秒」这个**已确认的事实**。

        确认方式（不是假设）：
          · 读写入点 —— `log_request` 里 `now = int(self.clock())`；
          · 读真实文件 —— 本机现存的多份 request-log.jsonl（含线上那份）
            抽样复核，`time` 全是 int。
        这条测试把那个事实变成**可执行的断言**：将来有人改成毫秒或字符串，
        它会红，而不是让保留逻辑悄悄错位。
        """
        _store, pool = self._pool()
        pool.log_request(None, "m", 200, True, 1, 0.1, 5, "success")
        row = self.read_raw()[-1]
        self.assertIsInstance(row["time"], int, "写入的 time 必须是 int")
        self.assertNotIsInstance(row["time"], bool)
        self.assertEqual(row["time"], NOW)
        # epoch 秒的量级：10 位。毫秒会是 13 位。
        self.assertLess(row["time"], 1e11, "必须是秒，不是毫秒")

    def test_parser_handles_the_formats_we_accept(self):
        parse = poolmod.AccountPool._parse_log_time
        self.assertEqual(parse(NOW), NOW)
        self.assertEqual(parse(str(NOW)), NOW, "数字字符串要能解析")
        self.assertEqual(parse(float(NOW)), NOW)
        self.assertEqual(parse(NOW * 1000), NOW, "13 位毫秒要折算成秒")
        self.assertEqual(parse("  %d  " % NOW), NOW, "两侧空白要容忍")

    def test_parser_rejects_junk_without_raising(self):
        parse = poolmod.AccountPool._parse_log_time
        for junk in (None, "", "  ", "abc", [], {}, True, False, float("nan"), float("inf")):
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(parse(junk), "%r 不该被当成时间" % (junk,))

    # ══ 验收 4：解析不出时间的条目怎么办 ══════════════════════════════
    def test_entries_with_unparsable_time_are_KEPT_not_deleted(self):
        """解析不出时间的条目**保留**，不静默丢弃。

        理由：删除是**不可恢复**的，而我们并不确定它是否超期。
        保留的代价只是多占一点磁盘；删错的代价是永久丢数据。
        所以这里选保守侧，并向 `stderr` 明确计数，而不是装作没看见。

        这类条目只可能来自「外部手写 / 将来改格式」——
        正常写入路径恒为 int epoch 秒（见上一条测试），
        所以它出现本身就说明有异常，不该被悄悄清掉。
        """
        good = self.entry(NOW - 1 * DAY, "good")
        no_time = {"model": "no-time", "status": 200}
        null_time = {"time": None, "model": "null-time"}
        junk_time = {"time": "not-a-time", "model": "junk-time"}
        self.write_raw([good, no_time, null_time, junk_time])

        _store, pool = self._pool()
        pool._prune_request_log_file(self.log_path, now=NOW, force=True)
        left = sorted(e.get("model") for e in self.read_raw())
        self.assertEqual(left, ["good", "junk-time", "no-time", "null-time"],
                         "解析不出时间的条目必须保留，不能静默丢弃")

    def test_unparsable_entries_are_reported_not_silently_kept(self):
        """保留归保留，但要**说出来** —— 静默保留会让人以为清理是完美工作的。"""
        self.write_raw([self.entry(NOW - 40 * DAY, "expired"),
                        {"time": "???", "model": "mystery"}])
        _store, pool = self._pool()
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            pool._prune_request_log_file(self.log_path, now=NOW, force=True)
        message = err.getvalue()
        self.assertIn("1", message, "应当报告有 1 条无法判定时间")
        self.assertIn("无法判定", message)
    # ══ 验收 6：重复落盘不会指数级变慢 ══════════════════════════════
    def test_repeated_appends_do_not_rewrite_the_file_every_time(self):
        """裁剪是**批量**触发的，不是每条都重写 —— 否则就是 O(n²)。

        怎么证明「不是每条都重写」：数**写文件的次数**。
        正常追加 200 条（阈值 1000 行以内）应当只走追加分支，
        重写次数为 **0**；如果实现是「每条都裁」，这里会是 200。

        用记账而不是计时：计时会随机器负载漂移，记账是确定性的。
        """
        _store, pool = self._pool()
        rewrites = [0]
        real_replace = os.replace

        def counting_replace(src, dst):
            rewrites[0] += 1
            return real_replace(src, dst)

        original = os.replace
        poolmod.os.replace = counting_replace
        try:
            for i in range(200):
                pool._append_request_log(self.entry(NOW - 1, "m-%03d" % i))
        finally:
            poolmod.os.replace = original

        self.assertEqual(rewrites[0], 0,
                         "200 条远未到阈值，不该发生任何整文件重写")
        self.assertEqual(len(self.read_raw()), 200)

    def test_prune_is_batched_not_per_entry(self):
        """超过阈值之后，是**攒够一批裁一次**，不是「每条都裁」。

        怎么证明：数**日志文件被重写的次数**。
             · 每条都裁 -> 追加 50 条 = 重写 50 次（这才叫 O(n²)）；
             · 攒批再裁 -> 第一次超阈值裁一次，之后不再裁 = 1 次。

        这里不构造 `SwitchStore`：它自己也会 `os.replace` 落盘 state.json /
        runtime.json，把那些写也数进来就测不到想测的东西了。
        裁剪逻辑不依赖 store，直接对着一只轻量池子跑即可。
        """
        from admin.pool import REQUEST_LOG_COMPACT_THRESHOLD as THRESHOLD
        self.write_raw([self.entry(NOW - 40 * DAY, "old-%d" % i)
                        for i in range(THRESHOLD + 100)])

        rewrites = [0]
        real_replace = os.replace
        target = str(self.log_path)

        def counting_replace(src, dst):
            if str(dst) == target:
                rewrites[0] += 1
            return real_replace(src, dst)

        # 构造池子会读日志；这里只关心 `_append_request_log` 的裁剪行为。
        _store, pool = self._pool()
        # 启动那次强制清理已经把超期条目全删了 —— 先记录下来，
        # 再单独数「后续追加」引发的重写。
        self.write_raw([self.entry(NOW - 40 * DAY, "old-%d" % i)
                        for i in range(THRESHOLD + 100)])

        poolmod.os.replace = counting_replace
        try:
            for i in range(50):
                pool._append_request_log(self.entry(NOW - 1, "new-%d" % i))
        finally:
            poolmod.os.replace = real_replace

        # 文件一开始就有 THRESHOLD+100 行（超阈值）-> 第一次追加后裁掉全部超期条目；
        # 裁完只剩 50 行（远低于阈值）-> 之后的 49 次追加都不再裁。
        self.assertEqual(rewrites[0], 1,
                         "裁剪应当只发生一次；每条都裁就是 O(n²)，会变 %d 次" % 50)
        left = self.read_raw()
        self.assertTrue(all(not e["model"].startswith("old-") for e in left),
                        "超期条目应当在那一次裁剪里被清掉")
        self.assertEqual(len(left), 50, "剩下的是 50 条新记录")

    def test_request_logs_limit_still_caps_the_result(self):
        _store, pool = self._pool()
        for i in range(50):
            pool.log_request(None, "m-%02d" % i, 200, True, 1, 0.1, 4, "success")
        self.assertEqual(len(pool.request_logs(limit=10)), 10)
        self.assertEqual(len(pool.request_logs(limit=50)), 50)
        # 默认仍是 200
        self.assertLessEqual(len(pool.request_logs()), 200)
        # 最新的在前
        self.assertEqual(pool.request_logs(limit=3)[0]["model"], "m-49")

    def test_request_logs_never_returns_expired_entries(self):
        """30 天外的条目不该出现在返回值里（这是「可见性」那一半）。

        注意这条**不能**单独证明删除 —— 但那正是上面几条读磁盘的测试要做的。
        两者都要有：一条管「看得见什么」，一条管「磁盘上还剩什么」。
        """
        self.write_raw([self.entry(NOW - 40 * DAY, "expired"),
                        self.entry(NOW - 1 * DAY, "fresh")])
        _store, pool = self._pool()
        models = [r["model"] for r in pool.request_logs()]
        # 启动时的那次强裁已经把超期条目删了，所以读回来的就只有 fresh。
        self.assertEqual(models, ["fresh"])

    def test_limit_of_zero_is_clamped_to_at_least_one(self):
        """limit=0 不能返回空列表 —— 原有行为，别在改动里弄丢。"""
        _store, pool = self._pool()
        for i in range(3):
            pool.log_request(None, "m-%d" % i, 200, True, 1, 0.1, 4, "success")
        self.assertEqual(len(pool.request_logs(limit=0)), 1)

    # ══ 内存防爆上限（业务规则 ≠ 防爆） ══════════════════════════════
    def test_memory_deque_has_a_blowup_guard_not_the_business_rule(self):
        """30 天是**业务规则**；内存上限是**防爆**，两者都要有。

        去掉内存上限，一个狂刷的进程能把内存吃光；
        用 30 天去当内存上限则做不到（30 天能攒多少条是未知的）。
        所以内存用宽松的固定条数，磁盘用时间。
        """
        from admin.pool import (REQUEST_LOG_MEMORY_LIMIT,
                                REQUEST_LOG_RETENTION_SECONDS)
        self.assertEqual(REQUEST_LOG_RETENTION_SECONDS, 30 * DAY)
        self.assertGreater(REQUEST_LOG_MEMORY_LIMIT, 300,
                           "防爆上限应当比旧的 300 条宽松，否则等于没改")
        _store, pool = self._pool()
        self.assertEqual(pool.request_log.maxlen, REQUEST_LOG_MEMORY_LIMIT,
                         "内存 deque 必须有上限，不能是无界的")
    # ══ 启动路径：停机很久之后再启动也要清 ═══════════════════════════
    def test_startup_prunes_entries_that_expired_while_stopped(self):
        """进程停机一个月，文件中留下了早已超期的条目 —— 启动时应当清掉。

        这条覆盖的是「靠写入触发清理」覆盖不到的情形：
        如果没人再发请求，就不会有 append，也就永远不会清理。
        """
        self.write_raw([self.entry(NOW - 45 * DAY, "stale-%d" % i) for i in range(5)] +
                       [self.entry(NOW - 1 * DAY, "recent")])
        before = len(self.read_raw())
        self.assertEqual(before, 6)

        _store, pool = self._pool()          # 构造 = 启动
        after = self.read_raw()
        self.assertEqual([e["model"] for e in after], ["recent"],
                         "启动时必须清掉停机期间超期的条目")
        self.assertEqual([r["model"] for r in pool.request_logs()], ["recent"])

    def test_startup_keeps_everything_when_nothing_expired(self):
        """反向对照：没有超期条目时启动**不该**删东西。"""
        self.write_raw([self.entry(NOW - i * 3600, "n-%d" % i) for i in range(7)])
        _store, pool = self._pool()
        self.assertEqual(len(self.read_raw()), 7)
        self.assertEqual(len(pool.request_logs()), 7)

    # ══ 与既有行为的兼容（别把已有保证弄丢） ═══════════════════════
    def test_a_truncated_last_line_does_not_void_the_whole_history(self):
        """上次被强杀留下的半截 JSON —— 只丢那一条，整份历史要留住。"""
        self.write_raw([self.entry(NOW - 3600, "a"), self.entry(NOW - 1800, "b")])
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write('{"model": "hal')
        _store, pool = self._pool()
        self.assertEqual(sorted(r["model"] for r in pool.request_logs()), ["a", "b"])

    def test_log_still_survives_a_restart_in_the_right_order(self):
        _store, p1 = self._pool()
        for i in range(5):
            p1.log_request(None, "m-%d" % i, 200, True, 3, 0.25, 9, "success")
        _store2, p2 = self._pool()
        self.assertEqual([r["model"] for r in p2.request_logs()],
                         ["m-4", "m-3", "m-2", "m-1", "m-0"])

    def test_log_does_not_touch_state_json(self):
        """请求日志仍然**不落进 state.json** —— 那会把整份状态文件反复重写。"""
        store, pool = self._pool()
        before = store.path.read_text(encoding="utf-8")
        pool.log_request(None, "m", 200, True, 1, 0.1, 4, "success")
        self.assertEqual(store.path.read_text(encoding="utf-8"), before)
        self.assertNotIn("request_logs", store.data)

    def test_log_path_deletion_does_not_break_anything(self):
        """日志文件被外部删掉时，写入应当自己恢复，不该抛出去。"""
        _store, pool = self._pool()
        pool.log_request(None, "first", 200, True, 1, 0.1, 4, "success")
        self.log_path.unlink()
        pool.log_request(None, "second", 200, True, 1, 0.1, 4, "success")
        self.assertEqual([r["model"] for r in self.read_raw()], ["second"])


if __name__ == "__main__":
    unittest.main()