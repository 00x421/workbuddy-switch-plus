"""可观测性三件套：`/v1/usage` 密钥自查、调用日志增强、用量聚合。

为什么值得单独锁死：

1. **`/v1/usage` 只读但必须留痕**。它不扣费，但「谁拿着错密钥反复敲门」是运营信号——
   早期这类请求既不进入站日志也不进调用日志，后台完全看不见。
2. **用量聚合要和明细分开**。`request_log` 是上限 300 条的内存环形缓冲，重启即清零、
   也答不了「这周哪几个模型在烧分」。聚合那份落 state.json，就必须保证裁剪是正确的
   （按字典序裁剪 == 按时间裁剪，前提是键格式恒定）。
3. **模型别名映射判两件事用的是两个名字**：版本归属（realm）判映射后的名字，
   模型白名单判客户端给的名字。混了就会出现「加了别名反而被白名单拦下」。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import pytest

import admin.server as admin_server
from admin import keysvc, usage
from core import converter

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


class FakeStore:
    """只管 `lock` / `data` 的最小 store，用于纯聚合逻辑的单测。"""

    def __init__(self):
        import threading
        self.lock = threading.RLock()
        self.data = {}


# --------------------------------------------------------------- 聚合逻辑

def test_record_creates_daily_and_hourly_buckets():
    store = FakeStore()
    # 2026-10-11 23:30 东八区
    now = 1791732600.0
    usage.record(store, now=now, status=200, tokens=100, credit=1.5,
                 duration_ms=800, first_token_ms=200, model="m1", key_id="k1")

    stats = store.data["usage_stats"]
    assert list(stats["daily"]) == ["2026-10-11"]
    assert list(stats["hourly"]) == ["2026-10-11T23"]
    bucket = stats["daily"]["2026-10-11"]
    assert bucket["calls"] == 1 and bucket["failed"] == 0
    assert bucket["tokens"] == 100
    assert bucket["credit"] == 1.5
    assert bucket["by_model"]["m1"]["calls"] == 1
    assert bucket["by_key"]["k1"]["calls"] == 1
    assert bucket["by_status"] == {"200": 1}


def test_failed_calls_are_counted_separately():
    store = FakeStore()
    now = 1791732600.0
    usage.record(store, now=now, status=200, tokens=10, model="m1")
    usage.record(store, now=now, status=429, tokens=0, model="m1")
    usage.record(store, now=now, status=503, tokens=0, model="m1")

    rows = usage.snapshot(store)["daily"]
    assert len(rows) == 1
    assert rows[0]["calls"] == 3 and rows[0]["failed"] == 2 and rows[0]["success"] == 1
    assert rows[0]["failure_rate"] == pytest.approx(2 / 3, abs=1e-4)
    # 失败也要计进 by_model，否则「某个模型一直在失败」会看不出来
    assert rows[0]["by_model"]["m1"]["failed"] == 2


def test_averages_ignore_failed_calls_for_first_token():
    """首字延迟只对成功的请求算：失败的根本没吐字，混进去会把均值拉低成假象。"""
    store = FakeStore()
    now = 1791732600.0
    usage.record(store, now=now, status=200, duration_ms=1000, first_token_ms=400)
    usage.record(store, now=now, status=200, duration_ms=3000, first_token_ms=600)
    usage.record(store, now=now, status=429, duration_ms=10, first_token_ms=0)

    row = usage.snapshot(store)["daily"][0]
    assert row["duration_ms_avg"] == pytest.approx((1000 + 3000 + 10) / 3, abs=0.2)
    assert row["first_token_ms_avg"] == pytest.approx(500.0, abs=0.2)


def test_prune_keeps_newest_buckets():
    store = FakeStore()
    base = 1791732600.0
    for i in range(usage.HOURLY_KEEP + 10):
        usage.record(store, now=base + i * 3600, status=200)

    hourly = store.data["usage_stats"]["hourly"]
    assert len(hourly) == usage.HOURLY_KEEP
    # 留下的必须是最新的那一批（键是 ISO 前缀，字典序即时间序）
    assert max(hourly) == max(sorted(hourly))


def test_snapshot_window_caps_rows():
    store = FakeStore()
    base = 1791732600.0
    for i in range(40):
        usage.record(store, now=base + i * 86400, status=200)

    assert len(usage.snapshot(store, days=7)["daily"]) == 7
    # 窗口参数有上限，防止有人传 days=100000 让响应爆掉
    assert usage.snapshot(store, days=10 ** 6)["window"]["days"] == usage.DAILY_KEEP


def test_record_without_store_key_initialises_it():
    """老 state.json 没有 usage_stats 键时不能 KeyError（上游就踩过缺键启动即崩）。"""
    store = FakeStore()
    usage.record(store, now=1791732600.0, status=200)
    assert store.data["usage_stats"]["daily"]


# --------------------------------------------------------------- 别名解析

class AliasUnitTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def test_resolves_chain(self):
        self.store.data["model_aliases"] = {"a": "b", "b": "deepseek-v4.1-flash"}
        self.assertEqual(keysvc.resolve_model_alias(self.store, "a"), "deepseek-v4.1-flash")

    def test_cycle_terminates(self):
        self.store.data["model_aliases"] = {"a": "b", "b": "a"}
        # 不能死循环；回到环上的名字就停
        self.assertIn(keysvc.resolve_model_alias(self.store, "a"), {"a", "b"})

    def test_non_alias_passes_through(self):
        self.store.data["model_aliases"] = {"old": "new"}
        self.assertEqual(keysvc.resolve_model_alias(self.store, "glm-5.2"), "glm-5.2")
        self.assertEqual(keysvc.resolve_model_alias(self.store, ""), "")

    def test_model_aliases_drops_self_mapping_and_blanks(self):
        self.store.data["model_aliases"] = {"a": "a", " ": "x", "b": " ", "c": "d"}
        self.assertEqual(keysvc.model_aliases(self.store), {"c": "d"})


# --------------------------------------------------------------- HTTP 面

class ObservabilityHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.store = self.app.state.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(result.status_code, 200)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    # ---- /v1/usage ----

    async def test_usage_requires_a_key(self):
        result = await self.client.get("/v1/usage")
        self.assertEqual(result.status_code, 401)
        body = result.json()
        self.assertEqual(body["error"]["code"], "invalid_api_key")

    async def test_usage_unknown_key_leaves_a_trail(self):
        """关键回归：鉴权失败也要进「入站日志 + 调用日志」，否则敲门声是隐形的。"""
        before_logs = len(self.store.data.get("ip_access_logs") or [])
        before_calls = len(self.app.state.pool.request_logs(300))

        result = await self.client.get("/v1/usage", headers={"Authorization": "Bearer nope"})
        self.assertEqual(result.status_code, 401)

        self.assertGreater(len(self.store.data.get("ip_access_logs") or []), before_logs)
        entry = (self.store.data["ip_access_logs"] or [])[-1]
        self.assertTrue(entry["blocked"])
        self.assertEqual(entry["reason"], "invalid")
        self.assertEqual(entry["path"], "/v1/usage")

        logs = self.app.state.pool.request_logs(300)
        self.assertGreater(len(logs), before_calls)
        self.assertEqual(logs[0]["outcome"], "rejected")
        self.assertEqual(logs[0]["source"], "usage")
        self.assertEqual(logs[0]["tokens"], 0)

    async def test_usage_reports_limits_without_charging(self):
        await self.login()
        created = await self.client.post("/admin/api/keys", json={
            "name": "下游A", "quota": 1000, "quota_credit": 5, "models": ["glm-5.2"]})
        token = created.json()["key"]
        kid = created.json()["id"]

        before = self.store.data["keys"][kid]["used_tokens"]
        result = await self.client.get("/v1/usage", headers={"Authorization": "Bearer " + token})
        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertEqual(body["tokens"]["limit"], 1000)
        self.assertEqual(body["tokens"]["used"], 0)
        self.assertEqual(body["tokens"]["remaining"], 1000)
        self.assertEqual(body["credit"]["remaining"], 5)
        self.assertEqual(body["key"]["models"], ["glm-5.2"])
        self.assertNotIn("hash", json.dumps(body))
        self.assertEqual(self.store.data["keys"][kid]["used_tokens"], before)

    async def test_usage_unlimited_reports_null_remaining(self):
        """额度 0 = 不限。不限时 remaining 必须是 null，不能给 0（下游会把 0 当用尽）。"""
        await self.login()
        token = (await self.client.post("/admin/api/keys", json={"name": "无限"})).json()["key"]
        body = (await self.client.get("/v1/usage",
                                      headers={"Authorization": "Bearer " + token})).json()
        self.assertEqual(body["tokens"]["limit"], 0)
        self.assertIsNone(body["tokens"]["remaining"])
        self.assertIsNone(body["credit"]["remaining"])

    async def test_usage_rejects_disabled_and_expired_keys(self):
        await self.login()
        created = (await self.client.post("/admin/api/keys", json={"name": "临时"})).json()
        kid, token = created["id"], created["key"]

        await self.client.patch("/admin/api/keys/" + kid, json={"enabled": False})
        self.assertEqual((await self.client.get(
            "/v1/usage", headers={"Authorization": "Bearer " + token})).status_code, 401)

        await self.client.patch("/admin/api/keys/" + kid,
                                json={"enabled": True, "expires_at": int(time.time()) - 60})
        self.assertEqual((await self.client.get(
            "/v1/usage", headers={"Authorization": "Bearer " + token})).status_code, 401)

    # ---- /admin/api/usage/stats ----

    async def test_usage_stats_requires_login(self):
        self.assertEqual((await self.client.get("/admin/api/usage/stats")).status_code, 401)

    async def test_usage_stats_reflects_traffic(self):
        self.app.state.pool.log_request(None, "glm-5.2", 200, True, 120, 0.5, 900,
                                        "success", "api", first_token_ms=180)
        self.app.state.pool.log_request(None, "glm-5.2", 503, False, 0, 0, 30,
                                        "no_account", "api")
        self.app.state.pool.flush_usage()

        await self.login()
        body = (await self.client.get("/admin/api/usage/stats?days=7&hours=6")).json()
        self.assertEqual(body["totals"]["daily"]["calls"], 2)
        self.assertEqual(body["totals"]["daily"]["failed"], 1)
        self.assertEqual(body["totals"]["daily"]["tokens"], 120)
        row = body["daily"][-1]
        self.assertEqual(row["by_model"]["glm-5.2"]["calls"], 2)

    # ---- 请求日志：是哪个密钥发起的 ----
    #
    # 需求是「日志里看得出具体是哪个 key 请求的」。三兄弟各管一段：
    #   key_id   稳定标识（用来 join 分组 / 配额）
    #   key_name 人起的名字（写日志时就固化）
    #   key_hint 密钥本身的脱敏片段

    async def test_log_entry_carries_key_identity(self):
        self.app.state.pool.log_request(None, "glm-5.2", 200, True, 10, 0.1, 100,
                                        "success", "api",
                                        key_id="k1", key_name="生产密钥",
                                        key_hint="sk-ab…1234")
        entry = self.app.state.pool.request_logs(1)[0]
        self.assertEqual(entry["key_id"], "k1")
        self.assertEqual(entry["key_name"], "生产密钥")
        self.assertEqual(entry["key_hint"], "sk-ab…1234")

    async def test_log_entry_without_key_has_no_name(self):
        """直连 / 测试请求没有密钥 —— 必须是 None，不能编一个出来。"""
        self.app.state.pool.log_request(None, "glm-5.2", 200, True, 10, 0.1, 100,
                                        "success", "test")
        entry = self.app.state.pool.request_logs(1)[0]
        self.assertIsNone(entry["key_id"])
        self.assertIsNone(entry["key_name"])

    async def test_blank_key_name_becomes_none(self):
        """空字符串要归一成 None，否则前端得同时判 null 和空串。"""
        for value in ("", "   ", None):
            self.app.state.pool.log_request(None, "m", 200, True, 0, 0, 1,
                                            "success", "api", key_id="k1",
                                            key_name=value)
            self.assertIsNone(self.app.state.pool.request_logs(1)[0]["key_name"])

    async def test_key_name_is_capped_and_whitespace_trimmed(self):
        self.app.state.pool.log_request(None, "m", 200, True, 0, 0, 1, "success",
                                        "api", key_id="k1", key_name="  名字  ")
        self.assertEqual(self.app.state.pool.request_logs(1)[0]["key_name"], "名字")

    async def test_key_name_survives_the_round_trip_to_disk(self):
        """落盘再读回，key_name 不能丢 —— 重启后历史还得说得出是谁在用。"""
        self.app.state.pool.log_request(None, "m", 200, True, 5, 0.2, 10,
                                        "success", "api", key_id="k9",
                                        key_name="重启前的密钥")
        self.app.state.pool._append_request_log(
            self.app.state.pool.request_logs(1)[0])
        # 重新从文件加载
        self.app.state.pool.request_log.clear()
        self.app.state.pool._load_request_log()
        names = [e.get("key_name") for e in self.app.state.pool.request_logs(50)]
        self.assertIn("重启前的密钥", names)

    # ---- 模型别名：管理端 ----

    async def test_alias_roundtrip_and_audit(self):
        await self.login()
        result = await self.client.post("/admin/api/model-aliases",
                                        json={"aliases": {"gpt-4o": "glm-5.2", "x": "x"}})
        self.assertEqual(result.status_code, 200)
        # 自映射（x -> x）会被丢掉：等价于没配
        self.assertEqual(result.json()["aliases"], {"gpt-4o": "glm-5.2"})

        fetched = (await self.client.get("/admin/api/model-aliases")).json()
        self.assertEqual(fetched["aliases"], {"gpt-4o": "glm-5.2"})
        self.assertIn("default_model", fetched)

        actions = [row.get("action") for row in self.store.data.get("audit_logs", [])]
        self.assertIn("set_model_aliases", actions)

    async def test_alias_rejects_bad_payload(self):
        await self.login()
        self.assertEqual((await self.client.post("/admin/api/model-aliases",
                                                 json={"aliases": []})).status_code, 400)
        self.assertEqual((await self.client.post("/admin/api/model-aliases",
                                                 json={"aliases": {"only-alias": ""}})).status_code, 400)

    async def test_alias_write_needs_admin(self):
        self.assertEqual((await self.client.post("/admin/api/model-aliases",
                                                 json={"aliases": {}})).status_code, 401)


class AliasDataPlaneTests(unittest.IsolatedAsyncioTestCase):
    """别名必须真的改掉发往上游的 model，然后按映射后的名字判版本归属。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN)
        self.store = self.app.state.store
        with self.store.lock:
            self.store.data["model_aliases"] = {"gpt-4o": "deepseek-v4.1-flash"}
            self.store.save()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_alias_is_applied_to_upstream_body(self):
        captured = []

        def upstream(request):
            captured.append(json.loads(request.content))
            payload = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, content=payload)

        original = httpx.AsyncClient

        converter.httpx.AsyncClient = lambda **kw: original(
            transport=httpx.MockTransport(upstream), **kw)
        try:
            async with original(transport=httpx.ASGITransport(app=self.app),
                                base_url="http://test") as client:
                response = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer " + API},
                    json={"messages": [{"role": "user", "content": "hi"}], "stream": True,
                          "model": "gpt-4o"})
                self.assertIn(response.status_code, (200, 503), response.text)
        finally:
            converter.httpx.AsyncClient = original

        self.assertTrue(captured, "请求没有到达上游，这条用例等于没验")
        self.assertEqual(captured[0]["model"], "deepseek-v4.1-flash",
                         "别名没有被应用到发往上游的请求体")

        # 日志里两个名字都要留着，方便排查「选了 A 却在跑 B」
        logs = self.app.state.pool.request_logs(50)
        self.assertTrue(logs, "调用日志为空")
        self.assertEqual(logs[0]["model"], "gpt-4o")
        self.assertEqual(logs[0]["upstream_model"], "deepseek-v4.1-flash")


if __name__ == "__main__":
    unittest.main()
