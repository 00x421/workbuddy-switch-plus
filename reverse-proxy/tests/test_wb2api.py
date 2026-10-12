"""`admin/wb2api.py` 的契约测试。

重点覆盖三件**容易写错且后果严重**的事（每一条都对应上游一个真实的坑）：
1. Go `time.Time` 的零值 `0001-01-01T00:00:00Z` 是**假真值**，必须当「没有」；
   当真了会让一个没冷却的账号显示成冷却了 2000 年。
2. `omitempty` 缺键 ≠ 零值。`cool_remaining_sec` 缺键 = 不冷却。
3. 账号级 `until` 与模型级 `until` **不等价**：多模型限流时账号级只是其中一个。
"""
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from admin import wb2api


def _rfc3339(offset_seconds: float) -> str:
    """生成一个 RFC3339 时刻（带 +08:00，模拟上游在中文区的输出）。"""
    tz = timezone(timedelta(hours=8))
    return (datetime.now(tz) + timedelta(seconds=offset_seconds)).isoformat()


class ParseStatusTest(unittest.TestCase):
    def test_full_account_row(self):
        payload = {
            "accounts": [{
                "uid": "u1", "cooling": True, "cool_kind": "soft_rate",
                "cool_remaining_sec": 240, "until": _rfc3339(300),
                "reason": "429 rate limit", "disabled": False,
                "manual_disabled": False, "consecutive_fails": 3,
                "rate_limited_models": [{
                    "model": "glm-5.3", "until": _rfc3339(300),
                    "reset_at": _rfc3339(600), "reason": "6004 model rate limit",
                }],
            }],
            "total": 1, "cooling": 1,
        }
        parsed = wb2api.parse_status(payload)
        row = parsed["u1"]
        self.assertTrue(row["cooling"])
        self.assertEqual(row["cool_kind"], "soft_rate")
        self.assertEqual(row["cool_remaining_sec"], 240)
        self.assertEqual(row["consecutive_fails"], 3)
        self.assertIsNotNone(row["until"])
        self.assertEqual(len(row["rate_limited_models"]), 1)
        model = row["rate_limited_models"][0]
        self.assertEqual(model["model"], "glm-5.3")
        # until 与 reset_at 必须分别保留：前者可能被 soft_rate_max 截断过。
        self.assertNotEqual(model["until"], model["reset_at"])

    def test_go_zero_time_is_false_truth_and_must_be_dropped(self):
        """Go 的 time.Time 零值不能当真 —— 否则显示成「冷却到 0001 年」。"""
        payload = {"accounts": [{"uid": "u1", "cooling": False,
                                 "until": "0001-01-01T00:00:00Z"}]}
        row = wb2api.parse_status(payload)["u1"]
        self.assertIsNone(row["until"])

    def test_omitted_cool_remaining_means_not_cooling(self):
        """缺失键 = 不冷却，不是「冷却 0 秒」。"""
        row = wb2api.parse_status({"accounts": [{"uid": "u1"}]})["u1"]
        self.assertIsNone(row["cool_remaining_sec"])
        self.assertFalse(row["cooling"])

    def test_zero_and_negative_remaining_become_none(self):
        for value in (0, -5, "abc", None, True):
            with self.subTest(value=value):
                row = wb2api.parse_status({"accounts": [{"uid": "u1", "cool_remaining_sec": value}]})["u1"]
                self.assertIsNone(row["cool_remaining_sec"])

    def test_rows_without_uid_are_skipped(self):
        """没有 uid 就无法归属，留着只会把冷却安到别的账号头上。"""
        parsed = wb2api.parse_status({"accounts": [{"cooling": True}, {"uid": ""}, {"uid": "u2"}]})
        self.assertEqual(list(parsed), ["u2"])

    def test_garbage_payloads_degrade_to_empty(self):
        for payload in (None, [], "text", 42, {"accounts": "nope"}, {}):
            with self.subTest(payload=payload):
                self.assertEqual(wb2api.parse_status(payload), {})

    def test_bad_field_type_does_not_kill_the_row(self):
        """单个字段类型不对，其余字段照常解析（上游可能加新类型）。"""
        row = wb2api.parse_status({"accounts": [{
            "uid": "u1", "cooling": True, "consecutive_fails": "3",
            "rate_limited_models": [None, "x", {"model": ""}, {"model": "ok"}],
        }]})["u1"]
        self.assertTrue(row["cooling"])
        self.assertEqual(row["consecutive_fails"], 0)
        self.assertEqual([m["model"] for m in row["rate_limited_models"]], ["ok"])

    def test_naive_and_zulu_timestamps_both_parse(self):
        """上游可能给 Z 也可能给 +08:00；两者都必须当真解析，不能字符串比大小。"""
        zulu = (datetime.now(timezone.utc) + timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = wb2api.parse_status({"accounts": [{"uid": "u1", "until": zulu}]})["u1"]
        self.assertIsNotNone(row["until"])


class ViewTest(unittest.TestCase):
    def test_remaining_is_recomputed_from_absolute_until(self):
        """上游给的剩余秒数是**快照**；有绝对时刻时必须按当前时间重算。"""
        now = 1_700_000_000.0
        info = wb2api.parse_status({"accounts": [{
            "uid": "u1", "cooling": True, "cool_remaining_sec": 600,
            "until": "2023-11-14T22:16:40Z",     # = now + 200s（epoch 1700000000 = 22:13:20Z）
        }]})["u1"]
        shown = wb2api.view(info, now=now)
        self.assertEqual(shown["cool_remaining_sec"], 200)

    def test_falls_back_to_snapshot_when_no_absolute_time(self):
        """上游没给绝对时刻时，只能用它的快照值，别硬算。"""
        info = {"cooling": True, "cool_remaining_sec": 300, "until": None,
                "rate_limited_models": []}
        self.assertEqual(wb2api.view(info, now=1000.0)["cool_remaining_sec"], 300)

    def test_expired_absolute_time_yields_no_remaining(self):
        """绝对时刻已过 → 不说「还要等负数秒」，直接没有剩余。"""
        info = {"cooling": True, "cool_remaining_sec": 300,
                "until": "2020-01-01T00:00:00Z", "rate_limited_models": []}
        self.assertIsNone(wb2api.view(info, now=1_700_000_000.0)["cool_remaining_sec"])

    def test_model_level_remaining_is_per_model(self):
        """账号级 until 不等于模型级 until —— 多模型限流时必须逐模型算。"""
        now = 1_700_000_000.0
        info = {"cooling": True, "until": None, "cool_remaining_sec": None,
                "rate_limited_models": [
                    {"model": "a", "until": "2023-11-14T22:16:40Z", "reset_at": None, "reason": ""},
                    {"model": "b", "until": "2023-11-14T22:26:40Z", "reset_at": None, "reason": ""},
                ]}
        shown = wb2api.view(info, now=now)
        self.assertEqual([m["remaining_sec"] for m in shown["rate_limited_models"]], [200, 800])

    def test_model_uses_reset_at_as_fallback(self):
        info = {"cooling": True, "rate_limited_models": [
            {"model": "a", "until": None, "reset_at": "2023-11-14T22:16:40Z", "reason": "6004"},
        ]}
        shown = wb2api.view(info, now=1_700_000_000.0)
        self.assertEqual(shown["rate_limited_models"][0]["remaining_sec"], 200)

    def test_models_alone_imply_cooling(self):
        """上游有时只给模型台账：那也说明在冷却，不能因为 cooling=False 就吞掉。"""
        info = {"cooling": False, "rate_limited_models": [{"model": "a", "until": None,
                                                           "reset_at": None, "reason": ""}]}
        self.assertTrue(wb2api.view(info, now=1.0)["cooling"])

    def test_not_cooling_still_reports_disabled_state(self):
        """不冷却 ≠ 健康：disabled / manual_disabled 必须照常透出。"""
        info = {"cooling": False, "disabled": True, "manual_disabled": True,
                "consecutive_fails": 7, "rate_limited_models": []}
        shown = wb2api.view(info, now=1.0)
        self.assertFalse(shown["cooling"])
        self.assertTrue(shown["disabled"])
        self.assertTrue(shown["manual_disabled"])
        self.assertEqual(shown["consecutive_fails"], 7)
        self.assertEqual(shown["rate_limited_models"], [])

    def test_view_accepts_raw_rfc3339_strings_not_just_floats(self):
        """回归：view() 曾被喂原始 JSON 字符串直接 TypeError 崩掉。

        它有两个调用方——走 fetch_status 的给的是解析好的 float，
        调试/单测时直接给上游原样的字符串更直观。**两者都必须能work**，
        否则一个字符串就能把整个面板打崩。
        """
        raw = {"cooling": True, "until": "2023-11-14T22:16:40Z",
               "rate_limited_models": [{"model": "a", "until": "2023-11-14T22:16:40Z",
                                        "reset_at": None, "reason": ""}]}
        shown = wb2api.view(raw, now=1_700_000_000.0)
        self.assertEqual(shown["cool_remaining_sec"], 200)
        self.assertEqual(shown["rate_limited_models"][0]["remaining_sec"], 200)
        # 透出的绝对时刻统一成 unix 秒，前端只认一种形状。
        self.assertEqual(shown["until"], 1_700_000_200.0)

    def test_view_never_raises_on_hostile_input(self):
        """view() 在面板刷新路径上，任何脏数据都不该抛异常。"""
        hostile = [
            {"cooling": True, "until": "not-a-time"},
            {"cooling": True, "until": 0},
            {"cooling": True, "rate_limited_models": [{"model": "a", "until": "xx"}]},
            {"cooling": True, "rate_limited_models": "nope"},
            {"cooling": True, "degrade_until": "0001-01-01T00:00:00Z"},
        ]
        for value in hostile:
            with self.subTest(value=value):
                wb2api.view(value, now=1_700_000_000.0)

    def test_empty_input_gives_empty_view(self):
        for value in (None, {}):
            with self.subTest(value=value):
                self.assertEqual(wb2api.view(value), {})


class FetchStatusTest(unittest.TestCase):
    def test_sends_bearer_only_when_key_present(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"accounts": [{"uid": "u1", "cooling": True}]})

        transport = httpx.MockTransport(handler)
        with_key = wb2api.fetch_status("http://x:1", "secret", transport=transport)
        self.assertEqual(seen["auth"], "Bearer secret")
        self.assertEqual(seen["url"], "http://x:1/status")
        self.assertTrue(with_key["u1"]["cooling"])

        without = wb2api.fetch_status("http://x:1", "", transport=transport)
        # 上游 APIKey 为空时完全放行；发空 Bearer 反而可能被判成「给了但不对」。
        self.assertIsNone(seen["auth"])
        self.assertTrue(without["u1"]["cooling"])

    def test_trailing_slash_in_base_does_not_double_up(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"accounts": []})

        wb2api.fetch_status("http://x:1/", "", transport=httpx.MockTransport(handler))
        self.assertEqual(seen["url"], "http://x:1/status")

    def test_http_error_raises(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "nope"}))
        with self.assertRaises(wb2api.Wb2ApiError):
            wb2api.fetch_status("http://x:1", "bad", transport=transport)

    def test_non_json_body_raises(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>nope</html>"))
        with self.assertRaises(wb2api.Wb2ApiError):
            wb2api.fetch_status("http://x:1", "", transport=transport)

    def test_connection_failure_raises_wb2api_error(self):
        """上游没跑时必须抛我们的异常类型（本机实测就是拒绝连接）。"""
        def boom(request):
            raise httpx.ConnectError("connection refused", request=request)

        with self.assertRaises(wb2api.Wb2ApiError):
            wb2api.fetch_status("http://127.0.0.1:1", "", transport=httpx.MockTransport(boom))


if __name__ == "__main__":
    unittest.main()
