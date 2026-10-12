"""对话活跃上报：事件形状、逐条节流、uid 缺失的静默丢失，以及池级闸门。

这片测试分五层，因为它们坏起来的方式完全不同：
  · 纯函数层 —— 事件少一个字段、条数不夹取，上游会静默丢弃或直接拒；
  · 协议层   —— 逐条发的节拍、失败即停、streak 解析写错，表现都是「看着成功」；
  · 账号池层 —— 闸门算错会让每个 tick 都对同一个必然失败的原因打一次上游，
                而界面上只看到一串失败；
  · tick 相位 —— 闸门对了但**没人调**（顺序、时间戳、有没有真的等下一轮）
                只有这一层守得住；
  · 脱敏层   —— 上游把鉴权头回显进错误页时，文案会落盘并被后台渲染出来。

最要命的一条是这个接口的**静默失效**：`userId` 缺失时服务端照样回 200，但把事件
丢掉。所以这里既断言「没有 uid 时一个请求都不发」，也断言「发完之后必须回读 streak」，
还断言「回读说没生效时**不能**把当天标记成已完成」——检测出问题却关掉自愈，
比不检测更糟。
"""
import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import admin.server as server

from admin import report
from admin.pool import (ACTIVE_RETRY_SECONDS, TRAVEL_BATCH_PER_TICK, CN, AccountPool)
from admin.server import Store
from test_admin_server import credential


# ── 假 HTTP 层 ─────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeClient:
    """按队列吐响应，并把每次请求原样记下来（含 headers 和 body）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": json})
        if not self.responses:
            raise AssertionError(f"没预备好的响应了：{method} {url}")
        return self.responses.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def paths(self):
        return [call["url"].split("codebuddy.cn", 1)[-1] for call in self.calls]


def ok(data=None):
    return FakeResponse(200, {"code": 0, "data": data if data is not None else {}})


def streak_body(days):
    return {"code": 0, "data": {"streak": {"days": days}}}


HEADERS = {"Authorization": "Bearer t0", "X-User-Id": "u1",
           "X-Domain": "www.codebuddy.cn"}


# ── 第一层：纯函数 ─────────────────────────────────────────────────────

class EventShapeTests(unittest.TestCase):
    def test_event_carries_the_full_field_shape(self):
        """事件必须是完整形状。

        精简成「只发几个必要字段」看着更干净，但官方客户端发的是这一整套，
        上游以后按字段加严时精简版会被挡掉——那是一次没有报错的静默失效。
        """
        event = report.chat_event("u1", "cid", "cid-r1", now_ms=1800000000000)
        expected = {
            "eventCode", "timestamp", "reportDelay", "mode", "conversationId",
            "requestId", "inputLength", "requestModelId", "requestModelName",
            "isPlan", "isAutoExecuteTerminal", "isAutoModify", "codebaseEnable",
            "maxToken", "maxSteps", "temperature", "maxRetries", "mentionContexts",
            "knowledgeId", "knowledgeName", "codebaseId", "mentionContextCount",
            "command", "expertId", "recommendId", "skillId", "skillCount",
            "totalCount", "fileUri", "presentAt", "traceId", "rootRequestId",
            "parentConversationId", "agentName", "agentType", "userId",
        }
        self.assertEqual(set(event), expected)
        self.assertEqual(len(event), 36)

    def test_event_uses_the_values_the_official_client_sends(self):
        event = report.chat_event("u1", "cid", "cid-r1")
        self.assertEqual(event["eventCode"], "chat_request_send")
        self.assertEqual(event["mode"], "craft")
        self.assertEqual(event["inputLength"], 12)
        self.assertEqual(event["requestModelId"], "deepseek-v4-flash")
        self.assertEqual(event["requestModelName"], "DeepSeek V4 Flash")
        self.assertEqual(event["agentName"], "default")
        self.assertEqual(event["agentType"], "conversation")
        # userId 必填。它缺失时上游回 200 但把事件丢掉——这是这个接口最阴的坑。
        self.assertEqual(event["userId"], "u1")

    def test_user_id_is_always_a_string_and_never_dropped(self):
        # 服务端不校验类型，但客户端发的是字符串；给 None 时也必须落到 "" 而不是 None
        # （None 会被 json 序列化成 null，那同样是「有字段但等于没有」）。
        self.assertEqual(report.chat_event(None, "cid", "r")["userId"], "")
        self.assertEqual(report.chat_event(12345, "cid", "r")["userId"], "12345")

    def test_events_share_one_conversation_but_not_request_ids(self):
        """N 条共用同一个 conversationId，requestId 各自独立。

        它模拟的是「同一会话里的 N 轮对话」。共用 requestId 会被当成重复上报，
        换 conversationId 则变成 N 个只有一轮的会话——都补不满对话量门槛。
        """
        events = report.report_events("u1", "cid", 5, now_ms=1800000000000)
        self.assertEqual(len(events), 5)
        self.assertEqual({e["conversationId"] for e in events}, {"cid"})
        self.assertEqual(len({e["requestId"] for e in events}), 5)
        self.assertEqual(events[0]["requestId"], "cid-r1")
        self.assertEqual(events[4]["requestId"], "cid-r5")

    def test_conversation_id_is_prefixed_and_time_stamped(self):
        self.assertEqual(report.conversation_id(1800000000000), "wb-switch-1800000000000")
        self.assertTrue(report.conversation_id().startswith(report.CONVERSATION_PREFIX + "-"))

    def test_clamp_count_holds_the_one_to_five_window(self):
        self.assertEqual(report.clamp_count(1), 1)
        self.assertEqual(report.clamp_count(5), 5)
        self.assertEqual(report.clamp_count(0), 1)
        self.assertEqual(report.clamp_count(-3), 1)
        self.assertEqual(report.clamp_count(99), 5)
        # 垃圾输入退回默认值，而不是抛异常——界面上传了个空字符串不该把调度打挂。
        self.assertEqual(report.clamp_count(None), report.DEFAULT_REPORTS)
        self.assertEqual(report.clamp_count("abc"), report.DEFAULT_REPORTS)

    def test_clamp_count_accepts_numeric_strings(self):
        self.assertEqual(report.clamp_count("3"), 3)

    def test_uid_of_reads_x_user_id_case_insensitively(self):
        # httpx 给回来的头是小写的，dict 形态则可能是任意大小写——两条路都要认。
        self.assertEqual(report.uid_of({"x-user-id": "u1"}), "u1")
        self.assertEqual(report.uid_of({"X-User-Id": "u2"}), "u2")
        self.assertEqual(report.uid_of({}), "")
        self.assertEqual(report.uid_of(None), "")


# ── 第二层：协议（假 client） ──────────────────────────────────────────

class ReportProtocolTests(unittest.TestCase):
    def test_missing_uid_sends_nothing_at_all(self):
        """没有 uid 时**一个请求都不发**。

        发了也是白发：服务端回 200 然后静默丢弃。少发一次请求就是少一次
        看起来成功的假象——那种假象会一路骗到界面上。
        """
        client = FakeClient([])
        result = report.report(client, {"Authorization": "Bearer t0"}, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["skip"], "no-uid")
        self.assertEqual(client.calls, [])

    def test_one_request_per_event_not_one_batched_request(self):
        """逐条发：N 条 = N 个请求，每个的 body 是只含一条事件的数组。

        上游的口径是「一条上报一个请求」。一次塞 N 条看着省事，但那是另一种
        请求形状，服务端不保证认。
        """
        client = FakeClient([ok(), ok(), ok()])
        result = report.report(client, HEADERS, 3, now_ms=1800000000000, sleep=lambda _s: None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], 3)
        self.assertEqual(client.paths, [report.REPORT_PATH] * 3)
        for call in client.calls:
            self.assertEqual(call["method"], "POST")
            self.assertIsInstance(call["body"], list)
            self.assertEqual(len(call["body"]), 1)
            self.assertEqual(call["body"][0]["eventCode"], "chat_request_send")
        # 所有请求共用同一个 session 标识，但 requestId 各不相同。
        cids = {c["body"][0]["conversationId"] for c in client.calls}
        rids = {c["body"][0]["requestId"] for c in client.calls}
        self.assertEqual(len(cids), 1)
        self.assertEqual(len(rids), 3)

    def test_each_event_carries_the_user_id_from_the_auth_headers(self):
        """body 里的 userId 与鉴权头同源。

        从别处再取一次 uid 是多余的一次机会：取错了就变成「200 但静默丢弃」，
        日志里一点异常都看不出来。
        """
        client = FakeClient([ok(), ok()])
        report.report(client, HEADERS, 2, sleep=lambda _s: None)
        for call in client.calls:
            self.assertEqual(call["body"][0]["userId"], "u1")
            self.assertEqual(call["headers"]["X-User-Id"], "u1")

    def test_consecutive_events_are_throttled(self):
        """同号内两条之间必须睡一觉（实测 1.5s）。秒发会触发风控。"""
        slept = []
        client = FakeClient([ok(), ok(), ok()])
        report.report(client, HEADERS, 3, sleep=slept.append)
        # 3 条 = 2 个间隔，不是 3 个：第一条前面不该睡。
        self.assertEqual(slept, [report.REPORT_GAP_SECONDS] * 2)

    def test_a_single_event_is_not_delayed(self):
        slept = []
        report.report(FakeClient([ok()]), HEADERS, 1, sleep=slept.append)
        self.assertEqual(slept, [])

    def test_stops_at_the_first_failure_instead_of_continuing(self):
        """某条失败就停在本号，不把剩下的续发出去。

        服务端按天幂等，续发既没必要，还会在一个已经出问题的号上多打几次。
        """
        client = FakeClient([ok(), FakeResponse(502, None, text="bad gateway"), ok()])
        result = report.report(client, HEADERS, 3, sleep=lambda _s: None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("bad gateway", result["message"])

    def test_report_sends_the_billing_headers_and_no_travel_web_headers(self):
        """走 billing 域，就是签到那套头。

        旅行那三个 web 头（x-client-platform / Origin / Referer）是 growth 域的
        口径，混进来会让请求在两个域之间不伦不类。
        """
        client = FakeClient([ok()])
        report.report(client, HEADERS, 1, sleep=lambda _s: None)
        sent = client.calls[0]["headers"]
        self.assertEqual(sent["X-User-Id"], "u1")
        self.assertNotIn("x-client-platform", {k.lower() for k in sent})
        self.assertNotIn("Origin", sent)
        self.assertNotIn("Referer", sent)
        self.assertTrue(client.calls[0]["url"].startswith("https://www.codebuddy.cn"))

    def test_count_is_clamped_before_it_reaches_the_wire(self):
        # 99 必须被夹到 5 —— 不是 99 个请求打到上游。
        client = FakeClient([ok()] * 5)
        result = report.report(client, HEADERS, 99, sleep=lambda _s: None)
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["sent"], 5)
        self.assertEqual(len(client.calls), 5)

    def test_streak_reads_the_days_with_a_get(self):
        client = FakeClient([FakeResponse(200, streak_body(7))])
        result = report.streak(client, HEADERS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["days"], 7)
        self.assertEqual(client.calls[0]["method"], "GET")
        self.assertEqual(client.paths, [report.STREAK_PATH])

    def test_streak_treats_a_missing_block_as_zero_days(self):
        # 形状对不上时给 0 而不是抛异常：调用方拿它做「是不是被静默丢弃了」的判断，
        # 读不出来就该按「没点亮」处理，而不是把整轮上报打成 500。
        client = FakeClient([FakeResponse(200, {"code": 0, "data": {}})])
        result = report.streak(client, HEADERS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["days"], 0)

    def test_streak_reports_failure_instead_of_pretending_zero(self):
        client = FakeClient([FakeResponse(500, None, text="boom")])
        result = report.streak(client, HEADERS)
        self.assertFalse(result["ok"])
        self.assertEqual(result["days"], 0)

# ── 第三层：账号池闸门 ─────────────────────────────────────────────────

class PoolActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "management", root / "auth", "api-test",
                           "admin-long-enough-test-key")
        self.now = 1800000000
        self.a = self.store.save_browser_account(credential("one"), "one")["account_id"]
        self.b = self.store.save_browser_account(credential("two"), "two")["account_id"]
        self.pool = AccountPool(self.store, clock=lambda: self.now)
        self.client = FakeClient([])
        self.pool.client_factory = lambda **kw: self.client
        self.headers = {"X-Domain": "www.codebuddy.cn", "X-User-Id": "u1"}
        for aid, item in self.store.data["accounts"].items():
            self.store.manager_for(aid, item).get_headers = Mock(return_value=dict(self.headers))
        self.today = time.strftime("%Y-%m-%d", time.localtime(self.now))

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **over):
        out = dict(self.store.data["pool"])
        out["auto_active"] = True
        out["active_time"] = "10:00"
        out.update(over)
        return out

    def last_body(self):
        """最后一条 **上报** 请求的 body。

        不能直接取 calls[-1]：一轮上报以一次 GET 回读连登收尾，那个请求没有 body，
        `["body"][0]` 会 TypeError。按路径挑才稳。
        """
        posts = [c for c in self.client.calls if c["body"] is not None]
        return posts[-1]["body"][0]

    # ── 动作落盘 ─────────────────────────────────────────────────────
    def test_operate_active_sends_one_event_then_reads_the_streak_back(self):
        """一轮上报 = 一条事件 + 一次连登回读。

        回读不是「顺手多查一次」：上报 200 不等于生效，days 才是唯一能证明它
        真的被服务端收下的东西。少了这一步，「静默丢弃」就永远发现不了。
        """
        self.client.responses = [ok(), FakeResponse(200, streak_body(3))]
        result = self.pool.operate(self.a, "active")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["streak"], 3)
        self.assertTrue(result["streak_ok"])
        self.assertEqual(self.client.paths, [report.REPORT_PATH, report.STREAK_PATH])
        self.assertEqual(self.last_body()["userId"], "u1")

    def test_success_writes_todays_date_and_the_streak(self):
        self.client.responses = [ok(), FakeResponse(200, streak_body(4))]
        self.pool.operate(self.a, "active")
        status = self.store.data["account_status"][self.a]
        self.assertEqual(status["active_date"], self.today)
        self.assertEqual(status["active_streak"], 4)

    def test_reported_but_dropped_says_so_out_loud(self):
        """上报 200、连登天数仍是 0 —— 事件被静默丢弃了，必须说出来。

        这是本模块唯一一处「HTTP 全绿但事实上没生效」的形态。如果这里也报
        「活跃上报完成」，用户看到的就是一个永远不会亮的连登和一个查不出原因的 0。
        """
        self.client.responses = [ok(), FakeResponse(200, streak_body(0))]
        result = self.pool.operate(self.a, "active")
        self.assertTrue(result["ok"])
        self.assertEqual(result["streak"], 0)
        # 断言要落在真的行为上。原先这里写的是
        # `assertIn("静默", result["message"] + "静默")`——自己给自己拼了个"静默"，
        # 恒真，等于没断言。
        self.assertIn("连登天数仍为 0", result["message"])

    def test_a_dropped_event_does_not_close_out_the_day(self):
        """**静默丢弃不能把当天标记成已完成。**

        这是本模块最要紧的一条状态语义：「上报 200」与「当天结束了」不是一件事。
        早先 `_note_active` 只看 `ok`，于是「检测出事件被丢弃」的下一步是
        把重试闸门关死——剩下半天里一条都不再试，用户面对的是一个永远点不亮的
        连登，而且没有任何自愈路径。检测出问题却顺手关掉自愈，比不检测更糟。
        """
        self.client.responses = [ok(), FakeResponse(200, streak_body(0))]
        self.pool.operate(self.a, "active")
        status = self.store.data["account_status"][self.a]
        self.assertIsNotNone(status.get("active_error"))
        # 关键：没写 active_date，所以这个账号还有机会被闸门选出来重试。
        self.assertNotIn("active_date", status)
        # 刚做完要等退避窗口（和普通失败同一个节拍），过了窗口就该重新入选。
        self.now += ACTIVE_RETRY_SECONDS + 1
        self.assertIn(self.a, self.pool.active_due(self.cfg(), "10:00", self.today))

    def test_a_verified_success_is_what_closes_out_the_day(self):
        # 另一半：回读到真的天数（>0）才是「生效了」，才关闸门。
        self.client.responses = [ok(), FakeResponse(200, streak_body(3))]
        self.pool.operate(self.a, "active")
        self.assertEqual(self.store.data["account_status"][self.a]["active_date"], self.today)
        self.assertNotIn(self.a, self.pool.active_due(self.cfg(), "10:00", self.today))

    def test_an_unreadable_streak_does_not_close_out_the_day(self):
        """回读**读不出来**同样不能把当天标记成已完成。

        「探针坏了」和「探到了 0」是两回事，但它们都不能证明上报生效，
        所以两者都不该关闸门——否则一次网关抖动就能让这个号当天彻底放弃。
        """
        self.client.responses = [ok(), FakeResponse(500, None, text="gateway boom")]
        result = self.pool.operate(self.a, "active")
        self.assertTrue(result["ok"])
        self.assertFalse(result["streak_ok"])
        self.assertNotIn("active_date", self.store.data["account_status"][self.a])
        self.now += ACTIVE_RETRY_SECONDS + 1
        self.assertIn(self.a, self.pool.active_due(self.cfg(), "10:00", self.today))

    def test_a_malformed_streak_value_does_not_poison_the_whole_round(self):
        """探针自己读到脏数据，**不能把已经成功的上报判成失败**。

        上游网关回一页 HTML、或者 days 字段换了类型，`int()` 就会抛。它原先
        落在 `_active` 的 except 里 → 报「活跃上报失败」、不写 active_date、
        下一个 tick 白打一轮。而事件其实好好报上去了——这就是「看起来失败、
        其实生效了」，用户会去查一个根本不存在的登录问题。
        """
        self.client.responses = [
            ok(), FakeResponse(200, {"code": 0, "data": {"streak": {"days": "abc"}}})]
        result = self.pool.operate(self.a, "active")
        # 上报本身是成功的，结果里必须如实反映这一点。
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], 1)
        self.assertFalse(result["streak_ok"])
        self.assertNotIn("streak", result)
        error = self.store.data["account_status"][self.a].get("active_error") or ""
        self.assertNotIn("invalid literal", error)
        self.assertNotIn("int()", error)

    def test_streak_lookup_failure_does_not_turn_the_report_red(self):
        # 上报本身成功了，只是回读炸了。这时候报「失败」会误导人重做一遍——
        # 而重做是幂等的，白白多打一次上游。
        self.client.responses = [ok(), FakeResponse(500, None, text="boom")]
        result = self.pool.operate(self.a, "active")
        self.assertTrue(result["ok"])
        self.assertFalse(result["streak_ok"])

    def test_a_failed_readback_does_not_wipe_the_known_streak(self):
        """回读失败**不能**把已知的连登天数抹成 0。

        这条是补票的：原先 `_active` 无条件写 `result["streak"] = check.get("days", 0)`，
        回读炸了 days 就是 0，于是把一个真实的天数（比如 9）覆盖成 0。用户看到连登
        突然归零，而事件其实好好报上去了——「读不到」被当成了「是 0」。
        """
        self.pool.update(self.a, active_streak=9)
        self.client.responses = [ok(), FakeResponse(500, None, text="boom")]
        result = self.pool.operate(self.a, "active")
        self.assertTrue(result["ok"])
        self.assertFalse(result["streak_ok"])
        # 回读失败时结果里不该有 streak 这个键（不是 0）。
        self.assertNotIn("streak", result)
        self.assertEqual(self.store.data["account_status"][self.a]["active_streak"], 9)

    def test_a_genuine_zero_is_reported_but_never_stored_as_a_streak(self):
        """回读成功而天数是 0（事件被丢弃）：结果里如实给 0，但**不写** active_streak。

        「读到了 0」与「读不到」在**结果**里必须分得开（一个是 0、一个没有这个键），
        但落到 `active_streak` 上两者都不该写：0 不是一个连登天数，它只是
        「这个事件此时此刻没有生效」的读数。把它当成天数存下来，界面上的
        「连登 0 天」和真实的「连登 9 天但今天的事件被丢了」就分不清了。
        """
        self.pool.update(self.a, active_streak=9)
        self.client.responses = [ok(), FakeResponse(200, streak_body(0))]
        result = self.pool.operate(self.a, "active")
        self.assertEqual(result["streak"], 0)
        status = self.store.data["account_status"][self.a]
        self.assertEqual(status["active_streak"], 9)   # 已知天数不被探针读数覆盖
        self.assertIsNotNone(status["active_error"])   # 但要留痕

    def test_success_clears_a_stale_error(self):
        """成功之后要把上一次的失败原因清掉。

        留着它，账号卡片上就永远挂着一条已经解决了的报错——陈旧状态比没有状态
        更误导人，而且它不会再被任何后续成功刷新掉。
        """
        self.pool.update(self.a, active_error="活跃上报失败：请检查登录凭据或网络")
        self.client.responses = [ok(), FakeResponse(200, streak_body(3))]
        self.pool.operate(self.a, "active")
        self.assertIsNone(self.store.data["account_status"][self.a]["active_error"])

    def test_a_failure_records_its_reason(self):
        # 失败要把原因落盘，否则界面上只看到「失败」两个字，无从判断要不要重试。
        self.client.responses = [FakeResponse(500, None, text="bad gateway")]
        self.pool.operate(self.a, "active")
        self.assertTrue(self.store.data["account_status"][self.a]["active_error"])

    def test_unexpected_exception_types_do_not_escape_as_500(self):
        """异常捕获口径要与 _travel / _operate 一致。

        捕窄了就会有漏网的类型：手动路径上变成 HTTP 500，自动路径上掀翻整个 tick，
        而两种表现都跟「上报」本身无关，排查时会往完全错误的方向找。
        """
        for boom in (ValueError("bad envelope"), KeyError("data"), RuntimeError("x")):
            with self.subTest(exc=type(boom).__name__):
                self.pool.client_factory = lambda **kw: (_ for _ in ()).throw(boom)
                result = self.pool.operate(self.a, "active")
                self.assertIsInstance(result, dict)
                self.assertFalse(result["ok"])
        # 契约：_active 捕的异常类型必须覆盖 _travel 那一套（后者是先写的，口径更全）。
        import inspect as _inspect
        import re as _re
        def caught(fn):
            found = _re.search(r"except \(([^)]*)\)", _inspect.getsource(fn))
            return {name.strip() for name in found.group(1).split(",")} if found else set()
        missing = caught(self.pool._travel) - caught(self.pool._active)
        self.assertEqual(missing, set(), f"_active 少捕了：{missing}")

    def test_active_reports_setting_controls_the_batch_size(self):
        self.pool.store.data["pool"]["active_reports"] = 3
        self.client.responses = [ok(), ok(), ok(), FakeResponse(200, streak_body(1))]
        result = self.pool.operate(self.a, "active")
        self.assertEqual(result["sent"], 3)
        self.assertEqual(len(self.client.calls), 4)

    def test_unauthorized_marks_a_failure_without_writing_the_date(self):
        self.client.responses = [FakeResponse(401, {"code": 401, "message": "unauthorized"})]
        result = self.pool.operate(self.a, "active")
        self.assertFalse(result["ok"])
        status = self.store.data["account_status"][self.a]
        self.assertIsNone(status.get("active_date"))
        # 失败也要记时间戳，否则下一个 tick 会立刻再打一次。
        self.assertEqual(status["auto_active_attempt"], int(self.now))

    def test_network_errors_are_folded_into_a_readable_message(self):
        self.client.responses = []          # 队列空了 → FakeClient 抛 AssertionError
        # 换一种更贴近真实的失败：直接让 client 抛 httpx 的异常类型。
        import httpx
        self.pool.client_factory = lambda **kw: (_ for _ in ()).throw(
            httpx.ConnectError("connection refused"))
        result = self.pool.operate(self.a, "active")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("失败", result["message"])
        # 不能把原始异常、响应体或凭据带出去。
        self.assertNotIn("connection refused", result["message"])
        self.assertNotIn("t0", json.dumps(result, ensure_ascii=False))

    def test_international_accounts_are_skipped_without_a_request(self):
        # 国际版走的是另一个计费域，这里发的还是国内主机，必然失败。
        # 与签到、旅行同一口径：拦在这里，别让调度对着必然失败的端点反复打。
        manager = self.store.manager_for(self.a, {})
        manager.get_headers = Mock(return_value={"X-Domain": "www.workbuddy.ai"})
        result = self.pool.operate(self.a, "active")
        self.assertFalse(result["ok"])
        self.assertIn("国内账号", result["message"])
        self.assertEqual(self.client.calls, [])

    def test_international_accounts_are_not_retried_all_day_long(self):
        """国际版账号是**当天有效的终局跳过**，不是普通失败。

        普通失败会按 30 分钟节拍重试到当天结束——对国际版账号来说，重试一万次
        也不会变好，只会往 500 条上限的任务流水里塞满同样的失败记录，把真正有用的
        签到 / 旅行记录挤出这一页。旅行那边用 RETRYABLE_SKIPS 分类挡的正是这件事；
        「活跃上报只有成功 / 失败两种落点」这个前提对国际版账号不成立。
        """
        self.store.manager_for(self.a, {}).get_headers = Mock(
            return_value={"X-Domain": "www.workbuddy.ai"})
        self.pool.operate(self.a, "active")
        status = self.store.data["account_status"][self.a]
        self.assertEqual(status["active_skip"], "intl")
        self.assertEqual(status["active_skip_date"], self.today)
        # 当天不再入选……
        self.assertNotIn(self.a, self.pool.active_due(self.cfg(), "10:00", self.today))
        # ……哪怕退避窗口早就过了（退避不是它的判据，终局标记才是）。
        self.now += ACTIVE_RETRY_SECONDS + 1
        self.assertNotIn(self.a, self.pool.active_due(self.cfg(), "10:00", self.today))
        # 但跨天要自动解禁：换台机器 / 用户改了域，第二天必须能重来。
        self.assertIn(self.a, self.pool.active_due(self.cfg(), "10:00", "2099-01-01"))

    def test_paused_accounts_are_not_dispatched(self):
        self.store.data["accounts"][self.a]["enabled"] = False
        result = self.pool.operate(self.a, "active")
        self.assertIn("暂停", result["message"])
        self.assertEqual(self.client.calls, [])

    def test_headers_lookup_failure_is_reported_not_raised(self):
        # 凭据读取失败不该变成 500：调度路径上的异常会掀翻整个 tick。
        self.pool.headers_for = Mock(side_effect=RuntimeError("credential store broken"))
        result = self.pool.operate(self.a, "active")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertIn("凭据", result["message"])

    # ── 任务流水 ─────────────────────────────────────────────────────
    def test_active_writes_a_kind_tagged_log_entry(self):
        self.client.responses = [ok(), FakeResponse(200, streak_body(2))]
        self.pool.operate(self.a, "active")
        logs = self.pool.task_logs(kind="active")
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0]["ok"])
        self.assertEqual(logs[0]["kind"], "active")
        # 别把签到 / 旅行 / 保活的流水搅在一起。
        self.assertEqual(self.pool.task_logs(kind="checkin"), [])
        self.assertEqual(self.pool.task_logs(kind="travel"), [])

    def test_clearing_one_kind_keeps_the_others(self):
        self.pool.log_task(self.a, "checkin", True, "签到成功")
        self.pool.log_task(self.a, "active", True, "活跃上报完成")
        self.pool.log_task(self.a, "keepalive", True, "已刷新")
        self.assertEqual(self.pool.clear_task_logs("active"), 1)
        kinds = sorted(row["kind"] for row in self.pool.task_logs())
        self.assertEqual(kinds, ["checkin", "keepalive"])

    def test_active_is_a_known_task_kind(self):
        # 界面下拉与「按类别清空」的白名单都从这一处取；漏了它就会出现
        # 「类别看得见、清不掉」的 400。
        from admin.pool import TASK_KINDS
        self.assertIn("active", TASK_KINDS)

    # ── 派发闸门 ─────────────────────────────────────────────────────
    def test_window_opens_at_the_configured_time(self):
        cfg = self.cfg()
        self.assertEqual(self.pool.active_due(cfg, "09:59", self.today), [])
        self.assertEqual(self.pool.active_due(cfg, "10:00", self.today), [self.a, self.b])

    def test_disabled_switch_dispatches_nobody(self):
        self.assertEqual(self.pool.active_due(self.cfg(auto_active=False), "23:00",
                                              self.today), [])

    def test_accounts_that_reported_today_are_not_picked_again(self):
        """今天已经成功报过就不再入选。

        这是整个闸门里最要紧的一条：漏了它，每个 tick（分钟级）都会对着同一个
        账号再报一轮，一天下来几百次同源写请求——风控口径上等于自曝。
        """
        self.pool.update(self.a, active_date=self.today)
        self.assertEqual(self.pool.active_due(self.cfg(), "10:00", self.today), [self.b])

    def test_yesterdays_success_does_not_block_today(self):
        self.pool.update(self.a, active_date="2000-01-01")
        self.assertEqual(self.pool.active_due(self.cfg(), "10:00", self.today),
                         [self.a, self.b])

    def test_failures_wait_out_the_retry_interval(self):
        self.pool.update(self.a, auto_active_attempt=int(self.now))
        self.assertEqual(self.pool.active_due(self.cfg(), "10:00", self.today), [self.b])
        self.now += ACTIVE_RETRY_SECONDS + 1
        self.assertEqual(self.pool.active_due(self.cfg(), "10:00", self.today),
                         [self.a, self.b])

    def test_paused_accounts_never_enter_the_window(self):
        self.store.data["accounts"][self.a]["enabled"] = False
        self.assertEqual(self.pool.active_due(self.cfg(), "10:00", self.today), [self.b])

    def test_gate_survives_a_corrupt_attempt_stamp(self):
        # auto_active_attempt 是从 state.json 读回来的，历史上可能是字符串或 null。
        # 让 int() 抛在这里，等于调度每个 tick 都炸一次。
        self.pool.update(self.a, auto_active_attempt="garbage")
        self.pool.update(self.b, auto_active_attempt=None)
        self.assertEqual(self.pool.active_due(self.cfg(), "10:00", self.today),
                         [self.a, self.b])

    def test_a_non_padded_time_does_not_starve_the_whole_feature(self):
        """`active_time` 不是补零的 `HH:MM` 时，整个功能会**静默饿死**。

        闸门是字符串比较（`now_hm` 由 strftime 给出，永远补零）。一旦配置那侧
        写的是 `"9:00"`，比较就不再是时间先后而是字典序——`"10:00" < "9:00"`
        为真，于是**一整天一个账号都不派**，且完全静默：界面显示开着、流水里
        一条记录都没有。server.py 的 PATCH 有正则挡新写入，但 state.json 是人手
        能改的、旧版本也可能写坏，所以判据必须能吞掉坏数据。
        """
        for bad in ("9:00", "9:0", "", None, "24:00", "10:60", "abc", 900):
            with self.subTest(active_time=bad):
                self.assertEqual(
                    self.pool.active_due(self.cfg(active_time=bad), "10:00", self.today),
                    [self.a, self.b], f"active_time={bad!r} 把整个功能饿死了")

    def test_the_same_normalization_protects_the_travel_window(self):
        # 同一根因、同一修法：派旅行那边也做字符串比较。
        self.assertEqual(
            self.pool.travel_depart_due({"travel_time": "9:00"}, "10:00", self.today),
            [self.a, self.b], "travel_time='9:00' 把派旅行饿死了")

    def test_a_terminal_skip_from_a_previous_day_does_not_leak_into_today(self):
        # active_skip 是「当天有效」的：昨天标了国际版，今天这台机器换成了国内
        # 账号（或者用户改了域），必须能正常入选，不需要任何人去清状态。
        self.pool.update(self.a, active_skip="intl", active_skip_date="2000-01-01")
        self.assertEqual(self.pool.active_due(self.cfg(), "10:00", self.today),
                         [self.a, self.b])

    # ── 透出字段 ─────────────────────────────────────────────────────
    def test_rows_expose_the_active_fields_the_ui_needs(self):
        self.pool.update(self.a, active_date=self.today, active_streak=6)
        rows = self.pool.rows(self.store.account_rows())
        row = next(r for r in rows if r["id"] == self.a)
        self.assertEqual(row["active_date"], self.today)
        self.assertTrue(row["active_today"])
        self.assertEqual(row["active_streak"], 6)
        other = next(r for r in rows if r["id"] == self.b)
        self.assertIsNone(other["active_date"])
        self.assertFalse(other["active_today"])

    def test_stale_active_date_is_not_reported_as_today(self):
        self.pool.update(self.a, active_date="2000-01-01")
        rows = self.pool.rows(self.store.account_rows())
        row = next(r for r in rows if r["id"] == self.a)
        self.assertFalse(row["active_today"])

    def test_rows_expose_the_error_so_the_dashboard_can_show_it(self):
        """写了 active_error 就得能被读到。

        它存在的唯一理由就是排查「为什么这个号一直没点亮」，而界面的数据源是
        rows()。不透出去，这个字段就只能靠人去翻 state.json——那跟没写没区别。
        """
        self.pool.update(self.a, active_error="已上报，但未能确认生效（连登天数仍为 0）")
        rows = self.pool.rows(self.store.account_rows())
        row = next(r for r in rows if r["id"] == self.a)
        self.assertIn("未能确认生效", row["active_error"])
        self.assertIsNone(next(r for r in rows if r["id"] == self.b)["active_error"])


# ── 第四层：tick 相位 ─────────────────────────────────────────────────
#
# 上面所有池级用例都直接调 `operate` / `active_due`。但**把这两件事接起来的
# 那段代码在 `tick` 里**，它自己也会错（顺序、有没有先记时间戳、有没有真的等到
# 下一轮）。这一段是「闸门算对了、但调度根本没调」这类失败的唯一守卫。

class TickPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "management", root / "auth", "api-test",
                           "admin-long-enough-test-key")
        self.now = 1800000000
        self.pool = AccountPool(self.store, clock=lambda: self.now)
        self.a = self.store.save_browser_account(credential("one"), "one")["account_id"]
        self.client = FakeClient([])
        # tick 的最后一相是 batch("status")，它走 billing 的 client.post；
        # FakeClient 只实现 request()，补一个 post 让它能静默通过。
        self.client.post = lambda *a, **k: FakeResponse(500, None, text="")
        self.pool.client_factory = lambda **kw: self.client
        for aid, item in self.store.data["accounts"].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={"X-Domain": "www.codebuddy.cn", "X-User-Id": "u1"})
        for key, value in (("auto_active", True), ("active_time", "00:00"),
                           ("auto_checkin", False), ("auto_travel", False),
                           ("auto_keepalive", False)):
            self.store.data["pool"][key] = value
        self.now += 3600

    def tearDown(self):
        self.tmp.cleanup()

    def _tick(self):
        """跑一轮 tick，把最后一相的 batch("status") 挡掉。

        挡的是「同步积分」，跟活跃上报无关，但它的上游调用会把 FakeClient 的
        响应队列吃掉，让断言变得要依赖无关相位的请求数。
        """
        with patch.object(self.pool, "batch", return_value={"results": []}):
            asyncio.run(self.pool.tick())

    def _report_paths(self):
        return [p for p in self.client.paths if p in (report.REPORT_PATH, report.STREAK_PATH)]

    def test_tick_reports_then_reads_the_streak_and_closes_out_the_day(self):
        self.client.responses = [ok(), FakeResponse(200, streak_body(3))]
        self._tick()
        self.assertEqual(self._report_paths(), [report.REPORT_PATH, report.STREAK_PATH])
        status = self.store.data["account_status"][self.a]
        self.assertEqual(status["active_date"],
                         datetime.fromtimestamp(self.now, CN).strftime("%Y-%m-%d"))
        self.assertEqual(status["active_streak"], 3)
        logs = self.pool.task_logs(kind="active")
        self.assertEqual([(row["ok"], row["source"]) for row in logs], [(True, "auto")])

    def test_a_second_tick_the_same_day_does_not_report_again(self):
        """每号每天一轮。这是这条链路的风控底线，必须由 tick 这一层守住。"""
        self.client.responses = [ok(), FakeResponse(200, streak_body(3))]
        self._tick()
        self.client.calls.clear()
        self._tick()
        self.assertEqual(self._report_paths(), [])

    def test_tick_records_the_attempt_before_it_acts(self):
        """动手**之前**先记尝试时间戳。

        上报里串了「逐条发 + 回读连登」多次往返，加上 1.5s 的条间节流，
        一轮要好几秒。中途挂住（进程被杀、上游不响应）时，如果时间戳还没写，
        下一个 tick 会立刻从头再来一遍——而 tick 是每分钟一轮。
        """
        seen = {}
        original = self.client.request

        def spy(method, url, headers=None, json=None):
            seen.setdefault("attempt_at_first_call",
                            self.store.data["account_status"][self.a].get("auto_active_attempt"))
            return original(method, url, headers=headers, json=json)

        self.client.responses = [ok(), FakeResponse(200, streak_body(2))]
        self.client.request = spy
        self._tick()
        self.assertEqual(seen.get("attempt_at_first_call"), int(self.now))

    def test_tick_does_not_starve_accounts_beyond_the_batch_cap(self):
        """批量上限是「这一轮处理几个」，不是「只有前几个账号有资格」。

        被处理过的账号在退避窗口内不再入选，所以剩下的账号下一轮自然轮上。
        漏了这一点，池子里排在后面（或先被处理）的账号会永远轮不到。
        """
        ids = [self.a] + [self.store.save_browser_account(credential(f"x{i}"), f"n{i}")["account_id"]
                          for i in range(6)]
        for aid in ids:
            self.store.manager_for(aid, self.store.data["accounts"][aid]).get_headers = Mock(
                return_value={"X-Domain": "www.codebuddy.cn", "X-User-Id": "u1"})
        seen = set()
        for _ in range(4):
            due = self.pool.active_due(dict(self.store.data["pool"]), "10:00",
                                       datetime.fromtimestamp(self.now, CN).strftime("%Y-%m-%d"))
            batch = due[:TRAVEL_BATCH_PER_TICK]
            seen.update(batch)
            for aid in batch:
                self.pool.update(aid, auto_active_attempt=int(self.now))
        self.assertEqual(seen, set(ids), "有账号永远轮不上")


# ── 第五层：凭据不外泄 ────────────────────────────────────────────────

class RedactionTests(unittest.TestCase):
    """上游的网关错误页会把我们发出去的鉴权头回显在正文里。

    这些文案会一路流进 `account_status` / `travel_states` / 任务流水**并落盘**，
    而这三处都会被网页后台渲染出来。所以脱敏必须发生在**折信封**那一步。
    """

    def test_envelope_strips_a_bearer_token_from_an_html_error_page(self):
        from admin import upstream
        env = upstream.envelope(FakeResponse(
            502, None, text="<html>Authorization: Bearer sk-live-abcdef123456</html>"))
        self.assertNotIn("sk-live-abcdef123456", env["message"])
        self.assertIn("***", env["message"])
        # 保留可读上下文：脱敏不是把整条文案删掉，出问题时还得能看出是什么错。
        self.assertIn("Authorization", env["message"])

    def test_message_of_strips_tokens_out_of_a_business_error(self):
        from admin import upstream
        msg = upstream.message_of({"code": 401, "message": 'bad token: "access_token":"at-987654321" rejected'})
        self.assertNotIn("at-987654321", msg)
        self.assertIn("token", msg)

    def test_a_wbt_key_is_redacted(self):
        from admin import upstream
        self.assertNotIn("wbt_abcdef123456", upstream.redact("key=wbt_abcdef123456 rejected"))

    def test_ordinary_text_survives_untouched(self):
        from admin import upstream
        for text in ("账号不存在", "daily limit reached", "连接超时 (10060)"):
            self.assertEqual(upstream.redact(text), text)

    def test_the_pool_never_persists_a_leaked_token(self):
        """端到端：一次把 bearer 回显出来的失败，不许在落盘状态里留下凭据。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = Store(root / "management", root / "auth", "api-test",
                      "admin-long-enough-test-key")
        pool = AccountPool(store, clock=lambda: 1800000000)
        aid = store.save_browser_account(credential("one"), "one")["account_id"]
        client = FakeClient([FakeResponse(500, None,
                                          text="<html>Authorization: Bearer sk-live-LEAKME123456</html>")])
        pool.client_factory = lambda **kw: client
        store.manager_for(aid, store.data["accounts"][aid]).get_headers = Mock(
            return_value={"X-Domain": "www.codebuddy.cn", "X-User-Id": "u1"})

        result = pool.operate(aid, "active")
        blob = json.dumps({"result": result,
                           "status": store.data["account_status"][aid],
                           "logs": pool.task_logs()}, ensure_ascii=False)
        self.assertNotIn("LEAKME123456", blob)


# ── 第六层：HTTP 接口白名单与设置校验 ─────────────────────────────────
#
# 这一层守的是「接线」：动作名有没有进白名单、设置项有没有被校验。
# 它坏起来的表现是 404 / 400，不会静默——但漏了 `active` 的话，
# 功能就是「后端全对、界面上点不动」。

class AdminSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "management", root / "auth", "api-test",
                           "admin-long-enough-test-key")
        self.aid = self.store.save_browser_account(credential("one"), "one")["account_id"]
        self.app = server.create_app(store=self.store,
                                     admin_key="admin-long-enough-test-key")
        import httpx
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")
        result = await self.client.post("/admin/api/login",
                                        json={"key": "admin-long-enough-test-key"})
        self.assertEqual(result.status_code, 200)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_the_account_action_whitelist_accepts_active(self):
        # 不在白名单里就是 404——界面上的「活跃」按钮会直接点不动。
        for action in ("active", "travel", "travel-claim", "checkin", "status", "refresh"):
            with self.subTest(action=action):
                resp = await self.client.post(
                    f"/admin/api/accounts/{self.aid}/actions/{action}")
                self.assertNotEqual(resp.status_code, 404, f"{action} 不在白名单里")

    async def test_the_pool_action_whitelist_accepts_active(self):
        resp = await self.client.post("/admin/api/pool/actions/active")
        self.assertNotEqual(resp.status_code, 404)

    async def test_an_unknown_action_is_still_404(self):
        self.assertEqual((await self.client.post(
            f"/admin/api/accounts/{self.aid}/actions/not-a-task")).status_code, 404)
        self.assertEqual((await self.client.post(
            "/admin/api/pool/actions/not-a-task")).status_code, 404)

    async def test_active_settings_round_trip(self):
        resp = await self.client.patch("/admin/api/pool/settings",
                                       json={"auto_active": True, "active_time": "10:30",
                                             "active_reports": 3})
        self.assertEqual(resp.status_code, 200)
        pool = resp.json()["pool"]
        self.assertIs(pool["auto_active"], True)
        self.assertEqual(pool["active_time"], "10:30")
        self.assertEqual(pool["active_reports"], 3)

    async def test_active_settings_reject_bad_values(self):
        """非法值必须被**拒绝**，不能默默存进去。

        存进去的后果是静默的：`active_reports: 99` 会被 `clamp_count` 夹到 5、
        `active_time: "9:00"` 会让闸门一整天不开——两件事界面上都看不出来。
        """
        for bad, field in (({"active_reports": 0}, "条数下界"),
                           ({"active_reports": 6}, "条数上界"),
                           ({"active_reports": "x"}, "条数非数字"),
                           ({"active_time": "25:00"}, "小时越界"),
                           ({"active_time": "10:60"}, "分钟越界"),
                           ({"active_time": "9:00"}, "未补零"),
                           ({"active_time": 900}, "非字符串"),
                           ({"auto_active": "yes"}, "非布尔")):
            with self.subTest(case=field, body=bad):
                resp = await self.client.patch("/admin/api/pool/settings", json=bad)
                self.assertEqual(resp.status_code, 400, f"{field} 没被拒：{bad}")

    async def test_wb2api_settings_round_trip_and_empty_is_a_valid_value(self):
        """空串必须能存进去 —— 它是「不读上游」，不是「没填」。

        如果实现用真值判断跳过空串，用户就永远清不掉已填的地址，
        只能去手改 state.json。这是那种「测试不写就一定会犯」的错。
        """
        resp = await self.client.patch("/admin/api/pool/settings",
                                       json={"wb2api_base": "http://127.0.0.1:7863/",
                                             "wb2api_key": "secret"})
        self.assertEqual(resp.status_code, 200)
        pool = resp.json()["pool"]
        # 末尾斜杠要归一化掉，否则会拼出 //status
        self.assertEqual(pool["wb2api_base"], "http://127.0.0.1:7863")
        self.assertEqual(pool["wb2api_key"], "secret")

        resp = await self.client.patch("/admin/api/pool/settings", json={"wb2api_base": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pool"]["wb2api_base"], "")

    async def test_wb2api_settings_reject_bad_addresses(self):
        for bad, why in (({"wb2api_base": "127.0.0.1:7863"}, "缺协议"),
                         ({"wb2api_base": "ftp://x/y"}, "非 http"),
                         ({"wb2api_base": 123}, "非字符串"),
                         ({"wb2api_base": "http://a b"}, "含空格"),
                         ({"wb2api_key": 123}, "密钥非字符串")):
            with self.subTest(case=why):
                resp = await self.client.patch("/admin/api/pool/settings", json=bad)
                self.assertEqual(resp.status_code, 400, f"{why} 没被拒：{bad}")

    async def test_account_rows_expose_upstream_cooldown_fields(self):
        """账号行必须带出 up_* 字段。

        没配上游时它们全是 None —— 界面据此**完全不显示**那一块。
        把「读不到」画成「没有冷却」是在替上游下结论，比不显示更糟。
        """
        resp = await self.client.get("/admin/api/overview")
        self.assertEqual(resp.status_code, 200)
        accounts = resp.json()["accounts"]
        self.assertTrue(accounts, "需要至少一个账号才能断言字段")
        for row in accounts:
            for field in ("up_cooling", "up_cool_remaining", "up_models"):
                self.assertIn(field, row, f"账号行缺 {field}")
            self.assertIsNone(row["up_cooling"])
            self.assertEqual(row["up_models"], [])

    async def test_the_clear_log_endpoint_takes_active_from_the_shared_whitelist(self):
        from admin.pool import TASK_KINDS
        for kind in TASK_KINDS:
            with self.subTest(kind=kind):
                resp = await self.client.post(
                    f"/admin/api/pool/checkin-logs/clear?kind={kind}")
                self.assertEqual(resp.status_code, 200)
        self.assertEqual((await self.client.post(
            "/admin/api/pool/checkin-logs/clear?kind=bogus")).status_code, 400)


if __name__ == "__main__":
    unittest.main()