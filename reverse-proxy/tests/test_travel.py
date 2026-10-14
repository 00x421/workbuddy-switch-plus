"""派猫猫旅行：状态机、协议形状、以及接进账号池之后的调度闸门。

这片测试分三层，因为它们坏起来的方式完全不同：
  · 纯函数层 —— 判据写错（例如把「限流」当成「今日已派」）会静默丢积分；
  · 旅行模块层 —— 请求顺序/信封解析错，界面显示的行程就是假的；
  · 账号池层 —— 闸门算错会让每个 tick 都对同一个必然失败的原因打上游。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from admin import travel
from admin.pool import (KEEPALIVE_CHECK_SECONDS, TRAVEL_CLAIM_SECONDS,
                        TRAVEL_RETRY_SECONDS, AccountPool)
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


class FakeCtx:
    """账号池给旅行模块的三件适配（客户端 / 鉴权头 / 刷新）。"""

    def __init__(self, client, headers=None, refresh_ok=True):
        self.client = client
        self.headers = dict(headers or {"Authorization": "Bearer t0",
                                        "X-User-Id": "u1",
                                        "X-Domain": "www.codebuddy.cn"})
        self.refresh_ok = refresh_ok
        self.refresh_calls = 0
        self.seen_headers = []
        # 成员名以 travel.CTX_PROTOCOL 为准。协议里改了名字，这里立刻报错——
        # 否则假 ctx 会抱着旧名字继续让测试全绿，真账号池却在线上 500
        # （`refresh` / `refresh_account` 分叉就是这么漏过去的）。
        missing = [name for name in travel.CTX_PROTOCOL
                   if not callable(getattr(self, name, None))]
        assert not missing, f"FakeCtx 与 CTX_PROTOCOL 不一致，缺：{missing}"

    def client_for(self, aid):
        return self.client

    def headers_for(self, aid):
        self.seen_headers.append(dict(self.headers))
        return dict(self.headers)

    def refresh_account(self, aid):
        self.refresh_calls += 1
        if not self.refresh_ok:
            return False
        self.headers["Authorization"] = "Bearer t1"
        return True


def ok(data=None):
    return FakeResponse(200, {"code": 0, "data": data if data is not None else {}})


def status_body(state, **extra):
    data = {"state": state}
    data.update(extra)
    return {"code": 0, "data": data}


CONFIG = {"code": 0, "data": {"enabled": True,
                              "locations": [{"id": 1, "name": "杭州"},
                                            {"id": 2, "name": "成都"}]}}


# ── 第一层：纯函数 ─────────────────────────────────────────────────────

class DecideActionTests(unittest.TestCase):
    def test_state_machine_matches_desktop_side(self):
        self.assertEqual(travel.decide_action("arrived", False), "claim")
        self.assertEqual(travel.decide_action("traveling", False), "wait")
        self.assertEqual(travel.decide_action("idle", False), "depart")
        # idle + 已达当日上限 = 「累了，明天再来吧」，是当日结束，不是可派。
        self.assertEqual(travel.decide_action("idle", True), "skip-daily")
        self.assertEqual(travel.decide_action("garbage", False), "error")

    def test_rate_limit_is_not_treated_as_daily_done(self):
        # HTTP 429 是限流。把它归成 daily_limit 会白白丢掉一整天的旅行积分。
        self.assertEqual(travel.classify_depart_error(429, "too many requests"), "other")
        self.assertEqual(travel.classify_depart_error(0, "Daily limit reached"), "daily_limit")
        self.assertEqual(travel.classify_depart_error(0, "daily_limit_exceeded"), "daily_limit")
        self.assertEqual(travel.classify_depart_error(0, "Already traveling"), "already_traveling")
        self.assertEqual(travel.classify_depart_error(0, "no active buddy"), "no_buddy")
        self.assertEqual(travel.classify_depart_error(0, "Location not available"),
                         "location_unavailable")

    def test_arrive_at_is_normalized_from_both_units(self):
        seconds = 1760000000
        self.assertEqual(travel.arrive_at_secs(seconds), seconds)
        self.assertEqual(travel.arrive_at_secs(seconds * 1000), seconds)
        self.assertEqual(travel.arrive_at_secs(None), 0)

    def test_retryable_skips_exclude_terminal_states(self):
        # 当日名额用完 / 已完成不是「可重试」，否则调度会每轮白打一次上游。
        self.assertFalse(travel.is_retryable_skip("daily-limit"))
        self.assertTrue(travel.is_retryable_skip("no-buddy"))
        self.assertTrue(travel.is_retryable_skip("location-unavailable"))
        self.assertFalse(travel.is_retryable_skip(None))

    def test_in_flight_due_uses_cached_arrival(self):
        now = 1760000000
        flying = {"ok": True, "claimed": False, "arriveAt": (now + 60) * 1000}
        self.assertFalse(travel.in_flight_due(flying, now))
        flying["arriveAt"] = (now - 1) * 1000
        self.assertTrue(travel.in_flight_due(flying, now))
        # 缓存里没有到达时间 → 只能回查。
        self.assertTrue(travel.in_flight_due({"ok": True, "claimed": False}, now))
        # 已领取的不再回查。
        self.assertFalse(travel.in_flight_due({"ok": True, "claimed": True}, now))

    def test_web_headers_do_not_mutate_the_caller(self):
        base = {"Authorization": "Bearer t"}
        out = travel._web_headers(base)
        self.assertEqual(base, {"Authorization": "Bearer t"})
        self.assertEqual(out["x-client-platform"], "web")
        self.assertEqual(out["Origin"], travel.BASE)
        self.assertEqual(out["Referer"], travel.GROWTH_PAGE)


# ── 第二层：旅行模块 ───────────────────────────────────────────────────

class TravelRunnerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1760000000

    def runner(self, responses, refresh_ok=True, headers=None):
        client = FakeClient(responses)
        ctx = FakeCtx(client, headers=headers, refresh_ok=refresh_ok)
        return travel.TravelRunner(ctx, clock=lambda: self.now), client, ctx

    def test_depart_reads_config_then_departs_then_backfills_status(self):
        runner, client, _ = self.runner([
            FakeResponse(200, CONFIG),
            FakeResponse(200, {"code": 0, "data": {"state": "traveling"}}),
            FakeResponse(200, status_body("traveling", record_id=77,
                                          arrive_at=(self.now + 3600) * 1000,
                                          location={"id": 1, "name": "杭州"})),
        ])
        result = runner.depart("a1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "traveling")
        self.assertEqual(result["recordId"], 77)
        self.assertEqual(result["locationName"], "杭州")
        self.assertEqual(result["arriveAt"], (self.now + 3600) * 1000)
        self.assertEqual(client.paths, ["/activity/growth/buddy/travel/config",
                                       "/activity/growth/buddy/travel/depart",
                                       "/activity/growth/buddy/travel/status"])
        self.assertEqual(client.calls[1]["body"], {"location_id": 1})

    def test_depart_marks_daily_limit_as_done_for_today(self):
        runner, _, _ = self.runner([
            FakeResponse(200, CONFIG),
            FakeResponse(200, {"code": 45001, "message": "daily limit reached"}),
        ])
        result = runner.depart("a1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["skip"], "daily-limit")
        self.assertTrue(result["claimed"])          # 当日结束 → 不再重试
        self.assertTrue(result["already"])
        self.assertFalse(travel.is_retryable_skip(result["skip"]))

    def test_depart_no_buddy_is_a_retryable_skip(self):
        runner, _, _ = self.runner([
            FakeResponse(200, CONFIG),
            FakeResponse(200, {"code": 45002, "message": "no active buddy"}),
        ])
        result = runner.depart("a1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["skip"], "no-buddy")
        self.assertTrue(travel.is_retryable_skip(result["skip"]))

    def test_depart_already_traveling_is_not_an_error(self):
        runner, _, _ = self.runner([
            FakeResponse(200, CONFIG),
            FakeResponse(200, {"code": 45003, "message": "already traveling"}),
        ])
        result = runner.depart("a1")
        self.assertTrue(result["ok"])
        self.assertTrue(result["already"])
        self.assertEqual(result["state"], "traveling")

    def test_depart_tries_every_location_before_giving_up(self):
        runner, client, _ = self.runner([
            FakeResponse(200, CONFIG),
            FakeResponse(200, {"code": 1, "message": "location not available"}),
            FakeResponse(200, {"code": 1, "message": "location not available"}),
        ])
        result = runner.depart("a1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["skip"], "location-unavailable")
        self.assertEqual([c["body"] for c in client.calls[1:]],
                         [{"location_id": 1}, {"location_id": 2}])

    def test_depart_reports_config_failure_without_departing(self):
        runner, client, _ = self.runner([
            FakeResponse(200, {"code": 500, "message": "boom"}),
        ])
        result = runner.depart("a1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["skip"], "config-error")
        self.assertEqual(len(client.calls), 1)

    def test_depart_without_locations_is_terminal(self):
        runner, _, _ = self.runner([
            FakeResponse(200, {"code": 0, "data": {"enabled": True, "locations": []}}),
        ])
        result = runner.depart("a1")
        self.assertEqual(result["skip"], "no-location")
        self.assertFalse(result["ok"])

    def test_sync_waits_while_traveling(self):
        runner, client, _ = self.runner([
            FakeResponse(200, status_body("traveling", record_id=8,
                                          arrive_at=(self.now + 600) * 1000,
                                          location={"id": 1, "name": "成都"})),
        ])
        result = runner.sync("a1", {"ok": True, "claimed": False})
        self.assertTrue(result["ok"])
        self.assertFalse(result["claimed"])
        self.assertEqual(result["state"], "traveling")
        self.assertEqual(result["locationName"], "成都")
        self.assertEqual(len(client.calls), 1)      # 只查状态，不发领奖

    def test_sync_claims_when_arrived(self):
        runner, client, _ = self.runner([
            FakeResponse(200, status_body("arrived", record_id=9,
                                          reward_credit=100,
                                          location={"id": 1, "name": "杭州"})),
            FakeResponse(200, {"code": 0, "data": {"reward_credit": 100}}),
        ])
        result = runner.sync("a1", {"ok": True, "claimed": False})
        self.assertTrue(result["claimed"])
        self.assertEqual(result["rewardCredit"], 100)
        self.assertEqual(result["state"], "idle")
        self.assertEqual(client.paths[-1], "/activity/growth/buddy/travel/claim")
        self.assertEqual(client.calls[-1]["body"], {"record_id": 9})

    def test_sync_claim_without_record_id_sends_empty_body(self):
        runner, client, _ = self.runner([
            FakeResponse(200, status_body("arrived", reward_credit=50)),
            FakeResponse(200, {"code": 0, "data": {}}),
        ])
        runner.sync("a1", {"ok": True, "claimed": False})
        self.assertEqual(client.calls[-1]["body"], {})

    def test_sync_daily_limit_finishes_the_day(self):
        runner, _, _ = self.runner([
            FakeResponse(200, status_body("idle", daily_limit_reached=True,
                                          location={"id": 1, "name": "杭州"})),
        ])
        result = runner.sync("a1", {"ok": True, "claimed": False,
                                    "locationName": "杭州"})
        self.assertEqual(result["skip"], "daily-limit")
        self.assertTrue(result["claimed"])
        self.assertTrue(travel.result_claimed(result))

    def test_sync_unknown_state_is_an_error_not_idle(self):
        # 把「查不到状态」当成 idle，下一轮就会重复派发。
        runner, _, _ = self.runner([FakeResponse(200, {"code": 0, "data": {}})])
        result = runner.sync("a1", {"ok": True, "claimed": False})
        self.assertEqual(result["skip"], "status-error")
        self.assertFalse(result["claimed"])

    def test_unauthorized_is_retried_once_after_refresh(self):
        runner, client, ctx = self.runner([
            FakeResponse(401, {"code": 401, "message": "登录已失效"}),
            FakeResponse(200, CONFIG),
        ])
        result = runner._get("a1", travel.PREFIX + "/config")
        self.assertEqual(ctx.refresh_calls, 1)
        self.assertEqual(result["code"], 0)
        self.assertEqual(client.calls[0]["headers"]["Authorization"], "Bearer t0")
        self.assertEqual(client.calls[1]["headers"]["Authorization"], "Bearer t1")

    def test_unrefreshable_credentials_are_not_retried(self):
        runner, client, ctx = self.runner([
            FakeResponse(401, {"code": 401, "message": "登录已失效"}),
        ], refresh_ok=False)
        result = runner._get("a1", travel.PREFIX + "/config")
        self.assertEqual(ctx.refresh_calls, 1)
        self.assertEqual(len(client.calls), 1)     # 刷不出新凭据就别再打一遍
        self.assertTrue(travel.is_unauthorized(result))

    def test_http_error_body_is_folded_into_an_envelope(self):
        runner, _, _ = self.runner([FakeResponse(502, None, text="bad gateway")])
        result = runner._get("a1", travel.PREFIX + "/config")
        self.assertEqual(result["code"], 502)
        self.assertIn("bad gateway", result["message"])


# ── 第三层：账号池闸门 ─────────────────────────────────────────────────

def travel_credential(uid, expires_at):
    doc = credential(uid)
    doc["auth"]["expiresAt"] = expires_at
    return doc


class PoolTravelTests(unittest.TestCase):
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
        out["travel_time"] = "08:00"
        out.update(over)
        return out

    # ── 适配契约 ─────────────────────────────────────────────────────
    def test_network_failure_keeps_a_complete_state_shape(self):
        """网络异常分支存下的形状必须与成功路径一致。

        这一支以前只写 4 个键（skip/ok/claimed/message），没有 `state` ——
        界面读 `state` 判档位，读到 undefined 就显示成「状态未知」，
        而且上一轮的 recordId / arriveAt 被整个丢掉，途中那次行程从此找不回来。
        """
        import httpx

        # 先放进一轮「已经在路上」的结果，模拟真实时序。
        self.pool.set_travel_state(self.a, {
            "ok": True, "claimed": False, "state": "traveling",
            "recordId": "R-77", "arriveAt": (self.now + 600) * 1000,
            "locationName": "奈良", "date": self.today})

        def boom(**kw):
            raise httpx.ConnectError("connection reset")

        self.pool.client_factory = boom
        result = self.pool.operate(self.a, "travel-claim")

        self.assertFalse(result["ok"])
        stored = self.pool.travel_state(self.a)
        # 形状完整：界面要的 state 必须在，不能是 undefined。
        self.assertIn("state", stored)
        self.assertIsNotNone(stored["state"])
        self.assertEqual(stored["skip"], "error")
        # 行程线索保留：那次行程可能还在路上，不该因为一次网络抖动就忘掉。
        self.assertEqual(stored["recordId"], "R-77")
        self.assertEqual(stored["arriveAt"], (self.now + 600) * 1000)
        # 凭据 / 原始响应不得进入落盘文案。
        self.assertNotIn("Bearer", str(stored["message"]))

    def test_account_pool_satisfies_the_travel_runner_contract(self):
        """真账号池必须逐条满足 travel.CTX_PROTOCOL。

        这条是补票的：旅行模块曾经按 docstring 约定的 `ctx.refresh()` 去调，
        而账号池上的方法叫 `refresh_account()`。单测全绿（假 ctx 是照约定写的），
        真环境一到批量动作就 `AttributeError` → 500。假 ctx 只能证明「模块按约定
        用」，证明不了「适配器按约定给」——那条只能拿真对象核。
        """
        missing = [name for name in travel.CTX_PROTOCOL
                   if not callable(getattr(self.pool, name, None))]
        self.assertEqual(missing, [],
                         f"AccountPool 缺少 TravelRunner 需要的成员：{missing}")

    def test_unauthorized_travel_goes_through_refresh_without_blowing_up(self):
        """上游 401 时走「刷凭据 → 重试一次」，不能把异常漏成 500。

        这是上面那条契约测试的行为版：真对象、真调用链，上游全回 401。
        """
        # 只把最内层的凭据刷新换成假的（真刷新要出网），
        # `refresh_account` 这个方法名本身不动——名字写错就该在这里炸。
        manager = self.store.manager_for(self.a, self.store.data["accounts"][self.a])
        manager._refresh = Mock()
        manager.get_headers = Mock(return_value=dict(self.headers))
        unauthorized = FakeResponse(401, {"code": 401, "message": "unauthorized"})
        self.client.responses = [unauthorized, unauthorized]
        result = self.pool.operate(self.a, "travel")
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        # 两次：401 → 刷 → 重试。少一次说明「拒绝后不再试」，多一次说明死循环。
        self.assertEqual(len(self.client.paths), 2)

    # ── 动作落盘 ─────────────────────────────────────────────────────
    def test_operate_travel_stores_state_and_writes_a_kind_tagged_log(self):
        self.client.responses = [
            FakeResponse(200, CONFIG),
            FakeResponse(200, {"code": 0, "data": {"state": "traveling"}}),
            FakeResponse(200, status_body("traveling", record_id=5,
                                          arrive_at=(self.now + 3600) * 1000,
                                          location={"id": 1, "name": "杭州"})),
        ]
        result = self.pool.operate(self.a, "travel")
        self.assertTrue(result["ok"])
        state = self.pool.travel_state(self.a)
        self.assertEqual(state["state"], "traveling")
        self.assertEqual(state["locationName"], "杭州")
        # 回读状态必须真的落到盘上：领奖对账只认 arriveAt，读不到就永远不会回查。
        self.assertEqual(state["arriveAt"], (self.now + 3600) * 1000)
        self.assertEqual(state["recordId"], 5)
        self.assertEqual(state["date"], self.today)
        logs = self.pool.task_logs(kind="travel")
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0]["ok"])
        self.assertEqual(logs[0]["kind"], "travel")
        # 签到流水不该被旅行的记录污染。
        self.assertEqual(self.pool.task_logs(kind="checkin"), [])

    def test_travel_claim_logs_the_reward(self):
        self.pool.set_travel_state(self.a, {"ok": True, "claimed": False,
                                            "state": "traveling"})
        self.client.responses = [            FakeResponse(200, status_body("arrived", record_id=5, reward_credit=100)),
            FakeResponse(200, {"code": 0, "data": {"reward_credit": 100}}),
        ]
        result = self.pool.operate(self.a, "travel-claim")
        self.assertTrue(result["claimed"])
        logs = self.pool.task_logs(kind="travel")
        self.assertIn("+100", logs[0]["message"])
        self.assertEqual(logs[0]["credits"], 100)

    def test_international_accounts_are_skipped_without_a_request(self):
        # 国际版没有成长中心。拦不到这里，调度会对着必然失败的端点一直打。
        manager = self.store.manager_for(self.a, {})
        manager.get_headers = Mock(return_value={"X-Domain": "www.workbuddy.ai"})
        result = self.pool.operate(self.a, "travel")
        self.assertFalse(result["ok"])
        self.assertIn("国际版", result["message"])
        self.assertEqual(self.client.calls, [])

    def test_paused_accounts_are_not_dispatched(self):
        self.store.data["accounts"][self.a]["enabled"] = False
        result = self.pool.operate(self.a, "travel")
        self.assertIn("暂停", result["message"])
        self.assertEqual(self.client.calls, [])

    def test_clearing_one_kind_keeps_the_others(self):
        self.pool.log_task(self.a, "checkin", True, "签到成功")
        self.pool.log_task(self.a, "travel", True, "已派")
        self.pool.log_task(self.a, "keepalive", True, "已刷新")
        self.assertEqual(self.pool.clear_task_logs("travel"), 1)
        kinds = sorted(row["kind"] for row in self.pool.task_logs())
        self.assertEqual(kinds, ["checkin", "keepalive"])

    # ── 派发闸门 ─────────────────────────────────────────────────────
    def test_depart_window_opens_at_the_configured_time(self):
        cfg = self.cfg()
        self.assertEqual(self.pool.travel_depart_due(cfg, "07:59", self.today), [])
        self.assertEqual(self.pool.travel_depart_due(cfg, "08:00", self.today),
                         [self.a, self.b])

    def test_depart_skips_accounts_already_finished_today(self):
        self.pool.set_travel_state(self.a, {"ok": True, "claimed": True,
                                            "skip": "daily-limit", "state": "idle"})
        self.assertEqual(self.pool.travel_depart_due(self.cfg(), "09:00", self.today), [self.b])

    def test_depart_skips_accounts_with_a_trip_in_flight(self):
        self.pool.set_travel_state(self.a, {"ok": True, "claimed": False,
                                            "state": "traveling"})
        self.assertEqual(self.pool.travel_depart_due(self.cfg(), "09:00", self.today), [self.b])

    def test_retryable_failures_wait_a_full_retry_interval(self):
        self.pool.set_travel_state(self.a, {"ok": False, "claimed": False,
                                            "skip": "no-buddy", "updated": self.now})
        self.assertEqual(self.pool.travel_depart_due(self.cfg(), "09:00", self.today), [self.b])
        self.now += TRAVEL_RETRY_SECONDS + 1
        self.assertEqual(self.pool.travel_depart_due(self.cfg(), "09:00", self.today,),
                         [self.a, self.b])

    def test_yesterdays_failure_does_not_block_today(self):
        self.pool.set_travel_state(self.a, {"ok": False, "claimed": False,
                                            "skip": "no-buddy"})
        self.store.data["travel_states"][self.a]["date"] = "2000-01-01"
        self.assertEqual(self.pool.travel_depart_due(self.cfg(), "09:00", self.today),
                         [self.a, self.b])

    # ── 领奖闸门 ─────────────────────────────────────────────────────
    def test_claim_sweep_waits_for_the_arrival_time(self):
        self.pool.set_travel_state(self.a, {"ok": True, "claimed": False, "state": "traveling",
                                            "arriveAt": (self.now + 600) * 1000})
        self.assertEqual(self.pool.travel_claim_due(), [])
        # 到点之后（且过了 15 分钟的对账间隔）才回查。
        self.pool.set_travel_state(self.a, {"arriveAt": (self.now - 1) * 1000})
        self.now += TRAVEL_CLAIM_SECONDS + 1
        self.assertEqual(self.pool.travel_claim_due(), [self.a])

    def test_claim_sweep_ignores_finished_trips(self):
        self.pool.set_travel_state(self.a, {"ok": True, "claimed": True, "state": "idle",
                                            "updated": 0})
        self.assertEqual(self.pool.travel_claim_due(), [])

    def test_claim_sweep_throttles_repeat_queries(self):
        self.pool.set_travel_state(self.a, {"ok": True, "claimed": False,
                                            "state": "traveling", "updated": self.now})
        self.assertEqual(self.pool.travel_claim_due(), [])

    # ── 保活闸门 ─────────────────────────────────────────────────────
    def test_keepalive_picks_tokens_inside_the_horizon(self):
        soon = self.store.save_browser_account(
            travel_credential("soon", (self.now + 3600) * 1000), "soon")["account_id"]
        far = self.store.save_browser_account(
            travel_credential("far", (self.now + 48 * 3600) * 1000), "far")["account_id"]
        due = self.pool.keepalive_due(6)
        self.assertIn(soon, due)
        self.assertNotIn(far, due)

    def test_keepalive_skips_accounts_that_cannot_refresh(self):
        aid = self.store.save_browser_account(
            travel_credential("norefresh", (self.now + 60) * 1000), "norefresh")["account_id"]
        # 凭据入库时会校验 refreshToken 非空，所以这里只能绕过入库直接改文件：
        # 要复现的是「历史凭据里本来就没有 refresh token」这种真实形态。
        path = self.store.file_for(self.store.data["accounts"][aid])
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["auth"]["refreshToken"] = ""
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertNotIn(aid, self.pool.keepalive_due(6))

    def test_keepalive_check_is_throttled_per_account(self):
        soon = self.store.save_browser_account(
            travel_credential("soon", (self.now + 3600) * 1000), "soon")["account_id"]
        self.assertIn(soon, self.pool.keepalive_due(6))
        self.pool.update(soon, keepalive_checked=self.now)
        self.assertNotIn(soon, self.pool.keepalive_due(6))
        self.now += KEEPALIVE_CHECK_SECONDS + 1
        self.assertIn(soon, self.pool.keepalive_due(6))

    # ── 展示档位 ─────────────────────────────────────────────────────
    def test_travel_label_matches_the_desktop_side(self):
        label = self.pool.travel_label
        self.assertEqual(label({}, self.today), "untraveled")
        self.assertEqual(label({"ok": True, "claimed": False}, self.today), "traveling")
        self.assertEqual(label({"date": self.today, "skip": "no-buddy"}, self.today), "no-buddy")
        self.assertEqual(label({"date": self.today, "ok": True, "claimed": True}, self.today),
                         "finished")
        # 跨日还没领到手的行程仍然显示「旅行中」——它确实还在路上。
        self.assertEqual(label({"date": "2000-01-01", "ok": True, "claimed": False}, self.today),
                         "traveling")
        self.assertEqual(label({"date": "2000-01-01", "ok": True, "claimed": True}, self.today),
                         "untraveled")


class StaleConclusionTests(unittest.TestCase):
    """回归：`sync()` 不得把上一轮的结论挂在这一轮的结果上。

    线上真实故障：流水里出现 `ok=true` 却带着上一轮的英文失败原因
    （`daily limit reached`），界面上看起来像「成功了但又报了错」，
    而且同一句英文在 20 条流水里反复出现，人根本判断不出它属于哪一轮。

    根因是 `sync()` 以 `dict(prior or {})` 起手，成功后只改 `ok` 不清 `message`，
    于是旧原因一直跟着新结果走，并被反复写进流水。

    这一片测试盯的就是「上一轮的失败结论必须随轮次失效」。
    """

    def setUp(self):
        self.now = 1_800_000_000
        # 上一轮失败后的落盘结果 —— 这是每一轮 sync 的输入。
        self.stale = {"skip": "error", "ok": False, "claimed": False,
                      "message": "daily limit reached", "date": "2026-10-13"}

    def _sync(self, body, prior=None):
        client = FakeClient([FakeResponse(200, body)])
        runner = travel.TravelRunner(FakeCtx(client), clock=lambda: self.now)
        return runner.sync("a1", self.stale if prior is None else prior)

    def test_traveling_clears_the_previous_failure_message(self):
        """本轮查询成功 → 旧失败原因必须消失，换成描述当前行程的文案。"""
        result = self._sync(status_body("traveling",
                                       arrive_at=(self.now + 1800) * 1000,
                                       location={"id": 1, "name": "京都"}))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["skip"])
        self.assertNotIn("daily limit", str(result["message"]))
        self.assertIn("京都", result["message"])
        self.assertIn("30 分钟", result["message"])

    def test_traveling_without_arrival_time_still_gets_a_message(self):
        """没有到达时间时也不能留空 —— 界面要显示一句人话，而不是空白。"""
        result = self._sync(status_body("traveling"))
        self.assertTrue(result["ok"])
        self.assertTrue(str(result["message"]).strip())
        self.assertNotIn("daily limit", str(result["message"]))

    def test_daily_limit_replaces_the_stale_message(self):
        result = self._sync(status_body("idle", daily_limit_reached=True))
        self.assertTrue(result["ok"])
        self.assertEqual(result["skip"], "daily-limit")
        self.assertEqual(result["message"], "今日已派")
        self.assertNotIn("daily limit reached", result["message"])

    def test_idle_without_a_trip_says_so_instead_of_leaving_blank(self):
        result = self._sync(status_body("idle"))
        self.assertFalse(result["ok"])
        self.assertTrue(str(result["message"]).strip())
        self.assertNotIn("daily limit", str(result["message"]))

    def test_unknown_state_reports_the_actual_state(self):
        result = self._sync(status_body("nonsense"))
        self.assertEqual(result["skip"], "status-error")
        self.assertIn("nonsense", result["message"])

    def test_failed_status_query_reports_this_rounds_error(self):
        client = FakeClient([FakeResponse(200, {"code": 500, "message": "上游炸了"})])
        runner = travel.TravelRunner(FakeCtx(client), clock=lambda: self.now)
        result = runner.sync("a1", self.stale)
        self.assertEqual(result["skip"], "status-error")
        self.assertIn("上游炸了", result["message"])
        self.assertNotIn("daily limit reached", result["message"])

    def test_previous_message_never_survives_a_second_round(self):
        """连跑两轮：第二轮的文案必须来自第二轮，不是第一轮。"""
        first = travel.TravelRunner(
            FakeCtx(FakeClient([FakeResponse(200, status_body("idle", daily_limit_reached=True))])),
            clock=lambda: self.now).sync("a1", self.stale)
        self.assertEqual(first["message"], "今日已派")

        second = travel.TravelRunner(
            FakeCtx(FakeClient([FakeResponse(200, status_body("traveling",
                                                              location={"id": 1, "name": "大阪"}))])),
            clock=lambda: self.now).sync("a1", first)
        self.assertNotIn("今日已派", second["message"])

    def test_trip_fields_are_preserved_across_rounds(self):
        """recordId / arriveAt 是行程的客观属性，跨轮次必须保留 —— 清了就领不到奖。"""
        prior = dict(self.stale, recordId="R-9", arriveAt=(self.now + 600) * 1000)
        result = self._sync(status_body("traveling", record_id="R-9",
                                        arrive_at=(self.now + 600) * 1000,
                                        location={"id": 1, "name": "奈良"}), prior=prior)
        self.assertEqual(result.get("recordId"), "R-9")


class DepartMessageTests(unittest.TestCase):
    """派发失败时的文案：必须能看出是失败，且尽量是中文。"""

    def test_unknown_upstream_text_is_kept_for_diagnosis(self):
        """认不出的英文文案要保留原文 —— 那是排查时唯一的线索。"""
        self.assertIn("some brand new upstream error",
                      travel._depart_text("some brand new upstream error"))

    def test_known_phrases_are_translated_and_keep_the_original(self):
        text = travel._depart_text("buddy not found")
        self.assertIn("还没有领养猫猫", text)
        self.assertIn("buddy not found", text)

    def test_empty_text_gets_a_readable_fallback(self):
        self.assertEqual(travel._depart_text(""), "上游未说明原因")
        self.assertEqual(travel._depart_text(None), "上游未说明原因")

    def test_left_minutes_rounds_up_and_clamps_at_zero(self):
        now = 1_800_000_000
        self.assertEqual(travel._left_minutes((now + 30) * 1000, now), 1)   # 剩 30 秒 → 1 分钟
        self.assertEqual(travel._left_minutes((now + 60) * 1000, now), 1)
        self.assertEqual(travel._left_minutes((now + 61) * 1000, now), 2)
        self.assertEqual(travel._left_minutes((now - 10) * 1000, now), 0)   # 已到 → 0
        self.assertEqual(travel._left_minutes(0, now), 0)
        self.assertEqual(travel._left_minutes("garbage", now), 0)

    def test_unclassifiable_depart_marks_failure_not_success(self):
        """兜底分支必须是 ok=False —— 报成成功会让「今天已派」变成谎话。"""
        cfg = ok({"enabled": True, "locations": [{"id": "L1", "name": "京都"}]})
        dep = FakeResponse(200, {"code": 45001, "message": "totally new error"})
        runner = travel.TravelRunner(FakeCtx(FakeClient([cfg, dep])),
                                     clock=lambda: 1_800_000_000)
        result = runner.depart("a1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["skip"], "error")
        self.assertIn("旅行派发失败", result["message"])
        self.assertIn("totally new error", result["message"])


class ErrorTextTests(unittest.TestCase):
    """信封的文案可能在好几个地方 —— 只读顶层 `message` 会漏。

    `upstream.envelope()` 在 HTTP >= 300 且 body 是 dict 时**原样返回**，
    并不保证里面有 `message`。于是下面第 2~4 种形态会让分类器拿到空串，
    把「今日已派完」判成失败：界面上刷出「旅行派发失败：daily limit reached」，
    并且调度每轮都白打一次上游。
    """

    def test_top_level_message(self):
        self.assertEqual(travel.error_text({"message": "x"}), "x")

    def test_msg_alias(self):
        self.assertEqual(travel.error_text({"msg": "x"}), "x")

    def test_nested_under_data(self):
        self.assertEqual(travel.error_text({"code": 1, "data": {"message": "x"}}), "x")

    def test_error_key(self):
        self.assertEqual(travel.error_text({"error": "x"}), "x")

    def test_detail_key(self):
        self.assertEqual(travel.error_text({"detail": "x"}), "x")

    def test_doubly_nested(self):
        self.assertEqual(travel.error_text({"detail": {"message": "x"}}), "x")

    def test_missing_everywhere_is_empty_not_a_crash(self):
        for env in ({}, None, [], "字符串", {"code": 500}, {"data": {"code": 1}}):
            self.assertEqual(travel.error_text(env), "")

    def test_blank_strings_do_not_win_over_real_text(self):
        """空串/空白不能算「找到了」，否则会把后面的真文案挡住。"""
        self.assertEqual(travel.error_text({"message": "   ", "error": "真原因"}), "真原因")

    def test_message_of_also_digs_into_nested_shapes(self):
        """`message_of` 走同一套挖掘逻辑。

        它是全仓共用的「把上游原因取给人看」的入口，先前只读顶层
        `message`/`msg`：文案在别处时调用方拿到的是自己那个兜底文案
        （「领奖失败」之类），真正的原因被吞掉，排查时什么都看不到。
        """
        from admin import upstream
        self.assertEqual(upstream.message_of({"error": "真原因"}), "真原因")
        self.assertEqual(upstream.message_of({"data": {"message": "真原因"}}), "真原因")
        self.assertEqual(upstream.message_of({"detail": "真原因"}), "真原因")
        # 都找不到时仍然用调用方给的兜底文案
        self.assertEqual(upstream.message_of({}, "领奖失败"), "领奖失败")
        self.assertEqual(upstream.message_of(None, "领奖失败"), "领奖失败")


class DailyLimitClassificationTests(unittest.TestCase):
    """「今日已派完」必须被认出来 —— 漏判的代价是整天反复重试 + 假告警。

    线上真实故障：6 个账号同时刷出「旅行派发失败：daily limit reached」。
    根因是上游把文案放在 `data.message` 等位置时分类器拿到空串，
    于是落到兜底分支，被记成失败。
    """

    def test_english_variants(self):
        for text in ("daily limit reached", "Daily Limit Reached",
                     "daily travel limit reached", "daily_limit_exceeded",
                     "day limit reached", "limit reached"):
            with self.subTest(text=text):
                self.assertEqual(travel.classify_depart_error(0, text), "daily_limit")

    def test_chinese_variants(self):
        """上游是中文产品，本地化过的文案同样要认。"""
        for text in ("已达今日旅行上限", "今日已派完", "今天次数已达上限，明天再来吧",
                     "已达今日上限"):
            with self.subTest(text=text):
                self.assertEqual(travel.classify_depart_error(0, text), "daily_limit")

    def test_rate_limit_is_still_not_daily_done(self):
        """反向护栏：限流绝不能被当成「今日已派」。

        这条比正向匹配更重要 —— 误判成当日结束会白白丢掉一整天的旅行积分，
        而且当天再也不会重试。
        """
        for text in ("too many requests", "rate limit exceeded",
                     "service unavailable", "internal error", ""):
            with self.subTest(text=text):
                self.assertNotEqual(travel.classify_depart_error(0, text), "daily_limit")

    def test_http_429_alone_is_not_daily_done(self):
        self.assertEqual(travel.classify_depart_error(429, "too many requests"), "other")

    def test_already_traveling_wins_over_daily_limit(self):
        """"正在旅行"更具体、更可操作，两者都出现时优先它。"""
        self.assertEqual(
            travel.classify_depart_error(0, "already traveling, daily limit reached"),
            "already_traveling")

    def _depart(self, body):
        cfg = ok({"enabled": True, "locations": [{"id": "L1", "name": "京都"}]})
        runner = travel.TravelRunner(FakeCtx(FakeClient([cfg, FakeResponse(200, body)])),
                                     clock=lambda: 1_800_000_000)
        return runner.depart("a1")

    def test_nested_message_is_recognised_as_daily_done(self):
        """回归：文案在 data.message 里时，先前会被判成失败。"""
        result = self._depart({"code": 45001, "data": {"message": "daily limit reached"}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["skip"], "daily-limit")
        self.assertNotIn("派发失败", result["message"])

    def test_error_key_is_recognised_as_daily_done(self):
        result = self._depart({"error": "daily limit reached"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["skip"], "daily-limit")

    def test_detail_key_is_recognised_as_daily_done(self):
        result = self._depart({"detail": "daily limit reached"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["skip"], "daily-limit")

    def test_daily_done_is_marked_claimed_so_it_stops_retrying(self):
        """当日结束要落 `claimed`，否则调度每轮还会再打一次上游。"""
        result = self._depart({"code": 45001, "data": {"message": "daily limit reached"}})
        self.assertTrue(result["claimed"])
        self.assertFalse(travel.is_retryable_skip(result["skip"]))


if __name__ == "__main__":
    unittest.main()
