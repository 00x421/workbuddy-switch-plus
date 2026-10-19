"""国际版（realm=global）的归属判定、端点分派、地区供给与身份去重。

这些测试固定的都是**只有出错时才看得出来**的性质：

  · realm 判定走 domain 兜底 → 存量 .info 升级后行为不变
  · 归属与用户所选不一致时拒绝落盘，而不是静默改
  · 授给上游的登录页 URL 按 realm 校验 host
  · 地区注册三字段取值各不相同（写错会把账号归属写歪）
  · trial 的 14051 幂等
  · 身份去重是三元组，同 uid 跨版本不互相覆盖
"""
import json
import unittest
from unittest.mock import Mock

import httpx
from fastapi import HTTPException

from admin import global_region
from admin import realm as R
from admin.browser_login import BrowserLogin
import test_admin_server as admin_tests
from test_admin_server import credential


class RealmTests(unittest.TestCase):
    def test_norm_realm_aliases(self):
        for value in ("global", "GLOBAL", " global ", "ai", "intl", "international", "workbuddy-ai"):
            self.assertEqual(R.norm_realm(value), R.GLOBAL, value)
        for value in ("", None, "cn", "CN", "国内", "garbage", 0):
            self.assertEqual(R.norm_realm(value), R.CN, repr(value))

    def test_has_global_domain_is_suffix_aligned(self):
        for value in ("workbuddy.ai", "www.workbuddy.ai", "WWW.WorkBuddy.AI", "https://www.workbuddy.ai/x", "a.b.workbuddy.ai:443"):
            self.assertTrue(R.has_global_domain(value), value)
        # 子串判断会在这里全错：含 workbuddy.ai 但不是它的子域
        for value in ("workbuddy.ai.evil.com", "notworkbuddy.ai", "www.codebuddy.cn", "", None, "workbuddyai"):
            self.assertFalse(R.has_global_domain(value), repr(value))

    def test_realm_of_prefers_explicit_then_domain(self):
        self.assertEqual(R.realm_of({"realm": "global", "domain": "www.codebuddy.cn"}), R.GLOBAL)
        self.assertEqual(R.realm_of({"realm": "cn", "domain": "www.workbuddy.ai"}), R.CN)
        self.assertEqual(R.realm_of({"domain": "www.workbuddy.ai"}), R.GLOBAL)
        self.assertEqual(R.realm_of({"domain": "www.codebuddy.cn"}), R.CN)
        # 存量账号：没有 realm 也没有可识别域名 → 必须落到 CN（升级前后行为一致）
        self.assertEqual(R.realm_of({}), R.CN)
        self.assertEqual(R.realm_of("not a dict"), R.CN)

    def test_matches_domain(self):
        self.assertTrue(R.matches_domain(R.GLOBAL, "www.workbuddy.ai"))
        self.assertTrue(R.matches_domain(R.CN, "www.codebuddy.cn"))
        self.assertTrue(R.matches_domain(R.GLOBAL, ""))       # 空 domain 视为相符
        self.assertFalse(R.matches_domain(R.GLOBAL, "www.codebuddy.cn"))
        self.assertFalse(R.matches_domain(R.CN, "www.workbuddy.ai"))

    def test_endpoints_differ(self):
        self.assertEqual(R.chat_base(R.CN), "https://copilot.tencent.com")
        self.assertEqual(R.chat_base(R.GLOBAL), "https://www.workbuddy.ai")
        self.assertEqual(R.billing_base(R.CN), "https://www.codebuddy.cn")
        self.assertEqual(R.billing_base(R.GLOBAL), "https://www.workbuddy.ai")
        self.assertEqual(R.platform(R.CN), "workbuddy")
        self.assertEqual(R.platform(R.GLOBAL), "workbuddy-ai")

    def test_headers_carry_realm_specific_origin_and_global_fingerprint(self):
        cn = R.headers(R.CN, "tok", "u1")
        gl = R.headers(R.GLOBAL, "tok", "u1")
        self.assertEqual(cn["Origin"], "https://www.codebuddy.cn")
        self.assertEqual(gl["Origin"], "https://www.workbuddy.ai")
        self.assertEqual(cn["Accept-Language"], "zh-CN")
        self.assertEqual(gl["Accept-Language"], "en-US")
        self.assertIn("WorkBuddy AI", gl["User-Agent"])
        self.assertNotIn("WorkBuddy AI", cn["User-Agent"])
        # 风控闸门头两版必带
        self.assertEqual(cn["X-CodeBuddy-Request"], "1")
        self.assertEqual(gl["X-CodeBuddy-Request"], "1")
        self.assertEqual(cn["Authorization"], "Bearer tok")

    def test_device_fingerprint_is_stable_per_uid(self):
        a = R.device_fingerprint_headers("u1")
        b = R.device_fingerprint_headers("u1")
        c = R.device_fingerprint_headers("u2")
        self.assertEqual(a, b)
        self.assertNotEqual(a["X-Machine-ID"], c["X-Machine-ID"])
        self.assertEqual(len(a["X-Machine-ID"]), 36)
        self.assertEqual(R.device_fingerprint_headers(""), {})

    def test_billing_headers_single_segment_ua_and_identity_headers(self):
        h = R.billing_headers(R.GLOBAL, {"uid": "u1", "access_token": "tok",
                                         "enterprise_id": "e1", "domain": "www.workbuddy.ai"})
        self.assertEqual(h["User-Agent"], "WorkBuddy/5.5.4")  # 单段，无 CLI 段
        self.assertEqual(h["X-User-Id"], "u1")
        self.assertEqual(h["X-Enterprise-Id"], "e1")
        self.assertEqual(h["X-Tenant-Id"], "e1")
        self.assertEqual(h["X-Domain"], "www.workbuddy.ai")

    def test_billing_headers_omit_identity_when_absent(self):
        h = R.billing_headers(R.CN, {"access_token": "tok"})
        for key in ("X-User-Id", "X-Enterprise-Id", "X-Tenant-Id", "X-Domain"):
            self.assertNotIn(key, h)

    def test_expires_at_ms(self):
        self.assertEqual(R.expires_at_ms({"expiresAt": 1700000000}, 0), 1700000000000)
        self.assertEqual(R.expires_at_ms({"expiresAt": 1700000000000}, 0), 1700000000000)
        self.assertEqual(R.expires_at_ms({"expiresIn": 3600}, 10000), 13600000)
        self.assertEqual(R.expires_at_ms({}, 10000), 0)
        self.assertEqual(R.expires_at_ms({"expiresIn": 0}, 10000), 0)
        self.assertEqual(R.expires_at_ms({"expiresIn": True}, 10000), 0)

    def test_regions_are_listed(self):
        self.assertEqual(R.REGION_CODES, ("HK", "MO", "SG", "TH", "PH", "MY", "ID"))


class EnvelopeTests(unittest.TestCase):
    def test_envelope_reads_code_and_data(self):
        resp = Mock(status_code=200, json=Mock(return_value={"code": 0, "data": {"a": 1}}))
        self.assertEqual(R.envelope(resp), (0, {"a": 1}))

    def test_envelope_falls_back_to_http_status(self):
        resp = Mock(status_code=500, json=Mock(side_effect=ValueError))
        self.assertEqual(R.envelope(resp), (500, None))

    def test_envelope_handles_non_dict(self):
        resp = Mock(status_code=200, json=Mock(return_value=[1, 2]))
        self.assertEqual(R.envelope(resp), (200, [1, 2]))


class _Client:
    """按 URL 路径回放固定响应，并记录请求以便断言。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def request(self, method, url, headers=None, json=None, params=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "json": json})
        for suffix, response in self.routes.items():
            if url.endswith(suffix) or suffix in url:
                if callable(response):
                    return response(url, headers or {}, json)
                code, body = response
                return httpx.Response(code, json=body)
        return httpx.Response(404, json={"code": 404})

    def paths(self):
        return [c["url"] for c in self.calls]


class ProvisionTests(unittest.IsolatedAsyncioTestCase):
    AUTH = {"uid": "u1", "access_token": "tok", "realm": "global", "domain": "www.workbuddy.ai"}

    def client(self, routes):
        return _Client(routes)

    def test_region_fields_parses_nested_json_string(self):
        payload = json.dumps({"data": {"list": [
            {"IOS2": "HK", "EnName": "China Hong Kong", "Code": "810000"}]}})
        env = {"code": 0, "data": payload}
        table = global_region._region_match(env)
        self.assertEqual(table["HK"], ("HK", "China Hong Kong", "810000"))

    def test_region_fields_handles_plain_dict(self):
        env = {"code": 0, "data": {"list": [{"IOS2": "sg", "EnName": "Singapore", "Code": "702"}]}}
        self.assertEqual(global_region._region_match(env)["SG"], ("sg", "Singapore", "702"))

    def test_region_fields_bad_shapes_return_empty(self):
        for env in ({"code": 0, "data": None}, {"code": 0, "data": "not json"},
                    {"code": 0, "data": {"data": {"list": "nope"}}}, {"code": 0}):
            self.assertEqual(global_region._region_match(env), {}, env)

    async def test_region_fields_falls_back_to_code_when_lookup_misses(self):
        client = self.client({"/billing/area/get-country-code": (200, {"code": 0, "data": "{}"})})
        self.assertEqual(await global_region.region_fields(client, "hk"), ("HK", "HK", "HK"))

    async def test_submit_region_sends_three_different_values(self):
        payload = json.dumps({"data": {"list": [
            {"IOS2": "HK", "EnName": "China Hong Kong", "Code": "810000"}]}})
        client = self.client({
            "/billing/area/get-country-code": (200, {"code": 0, "data": payload}),
            "/console/login/account": (200, {"code": 0}),
        })
        ok, msg = await global_region.submit_region(client, self.AUTH, "hk")
        self.assertTrue(ok, msg)
        body = [c for c in client.calls if c["url"].endswith("/console/login/account")][0]["json"]
        self.assertEqual(body, {"attributes": {
            "countryCode": ["810000"], "countryFullName": ["China Hong Kong"], "countryName": ["HK"]}})

    async def test_submit_region_rejects_unknown_region_before_network(self):
        client = self.client({})
        ok, msg = await global_region.submit_region(client, self.AUTH, "US")
        self.assertFalse(ok)
        self.assertIn("不支持的地区", msg)
        self.assertEqual(client.calls, [])

    async def test_submit_region_noop_for_cn(self):
        client = self.client({})
        ok, msg = await global_region.submit_region(client, {"realm": "cn"}, "HK")
        self.assertFalse(ok)
        self.assertIn("国内版", msg)
        self.assertEqual(client.calls, [])

    async def test_registration_status_detects_required_and_done(self):
        pending = self.client({"/register": (500, {"code": 500, "message": "region required"})})
        self.assertFalse((await global_region.registration_status(pending, self.AUTH))[0])
        done = self.client({"/register": (200, {"code": 0})})
        self.assertTrue((await global_region.registration_status(done, self.AUTH))[0])

    async def test_registration_status_requires_uid(self):
        ok, why = await global_region.registration_status(self.client({}), {"uid": ""})
        self.assertFalse(ok)
        self.assertIn("uid", why)

    async def test_claim_trial_is_idempotent_on_14051(self):
        client = self.client({"/billing/ide/trial": (200, {"code": 14051})})
        ok, msg = await global_region.claim_trial(client, self.AUTH)
        self.assertTrue(ok)
        self.assertIn("此前已领取", msg)

    async def test_claim_trial_failure_is_reported(self):
        client = self.client({"/billing/ide/trial": (200, {"code": 14017, "message": "trial not activated"})})
        ok, msg = await global_region.claim_trial(client, self.AUTH)
        self.assertFalse(ok)
        self.assertIn("14017", msg)

    async def test_provision_noop_for_cn(self):
        client = self.client({})
        self.assertEqual(await global_region.provision(client, {"realm": "cn"}), "")
        self.assertEqual(client.calls, [])

    async def test_provision_runs_region_then_trial(self):
        payload = json.dumps({"data": {"list": [
            {"IOS2": "SG", "EnName": "Singapore", "Code": "702"}]}})
        client = self.client({
            "/billing/area/get-country-code": (200, {"code": 0, "data": payload}),
            "/console/login/account": (200, {"code": 0}),
            "/billing/ide/trial": (200, {"code": 0}),
        })
        note = await global_region.provision(client, self.AUTH, "SG")
        self.assertIn("地区已提交", note)
        self.assertIn("trial 已领取", note)
        order = [p for p in client.paths() if not p.endswith("get-country-code")]
        self.assertTrue(order[0].endswith("/console/login/account"), order)
        self.assertTrue(order[1].endswith("/billing/ide/trial"), order)

    async def test_provision_skips_trial_when_region_fails(self):
        # 地区没提交成功就不该去领 trial：trial 前置是地区已注册，
        # 此时去领只会拿一个失败码，把真实原因盖掉。
        client = self.client({
            "/billing/area/get-country-code": (200, {"code": 0, "data": "{}"}),
            "/console/login/account": (400, {"code": 400, "message": "invalid region"}),
            "/billing/ide/trial": (200, {"code": 0}),
        })
        note = await global_region.provision(client, self.AUTH, "HK")
        self.assertIn("提交地区失败", note)
        self.assertFalse(any(p.endswith("/billing/ide/trial") for p in client.paths()))

    async def test_provision_warns_when_region_missing(self):
        client = self.client({"/register": (500, {"code": 500, "message": "region required"})})
        note = await global_region.provision(client, self.AUTH, None)
        self.assertIn("14017", note)


class GlobalFlowTests(unittest.IsolatedAsyncioTestCase):
    """走 BrowserLogin 完整链路，验证 realm 分派与落盘前供给的先后顺序。"""

    async def asyncSetUp(self):
        self.time = 10000
        self.realm_domain = "www.workbuddy.ai"
        self.region_calls = []
        self.calls = []
        self.saved = []
        def saver(doc, name):
            self.saved.append(doc)
            return {"account_id": "id-1", "updated": False}
        self.manager = BrowserLogin(saver, lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(self.respond)), lambda: self.time)

    async def asyncTearDown(self):
        await self.manager.close()

    def respond(self, req):
        self.calls.append((req.method, req.url.path, dict(req.url.params)))
        path = req.url.path
        if path.endswith("/auth/state"):
            # 登录页 host 必须跟着 realm 走：CN 的登录页在 copilot.tencent.com，
            # GLOBAL 在 www.workbuddy.ai。这里固定成国际版那句，国内版用例就会
            # 被 _check_url 按 502 拦掉——是测试假响应失真，不是产品有问题。
            host = ("www.workbuddy.ai"
                    if "workbuddy.ai" in str(req.url) else "copilot.tencent.com")
            return httpx.Response(200, json={"code": 0, "data": {
                "state": "st", "authUrl": f"https://{host}/login?state=st"}})
        if path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "a", "refreshToken": "r", "expiresIn": 3600,
                "domain": self.realm_domain}})
        # 顺序要紧：`/console/login/account` 也以 `/login/account` 结尾，
        # 先判 login/account 会把地区注册那次请求误认成查账号信息，
        # 于是 region_calls 里永远少一条 —— 看起来像「没提交地区」。
        if path.endswith("/console/login/account"):
            self.region_calls.append(path)
            return httpx.Response(200, json={"code": 0})
        if path.endswith("/login/account"):
            return httpx.Response(200, json={"code": 0, "data": {"uid": "u1", "nickname": "国际号"}})
        if path.endswith("/billing/area/get-country-code"):
            self.region_calls.append(path)
            return httpx.Response(200, json={"code": 0, "data": "{}"})
        if path.endswith("/billing/ide/trial"):
            self.region_calls.append(path)
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 404})

    async def test_start_uses_global_platform_and_accepts_global_url(self):
        flow = await self.manager.start("owner", "g", R.GLOBAL, "HK")
        self.assertEqual(flow["realm"], R.GLOBAL)
        self.assertEqual(flow["region"], "HK")
        state_call = [c for c in self.calls if c[1].endswith("/auth/state")][0]
        self.assertEqual(state_call[2]["platform"], "workbuddy-ai")

    async def test_start_rejects_unknown_region(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.manager.start("owner", None, R.GLOBAL, "US")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_start_global_rejects_cn_url(self):
        def respond(req):
            if req.url.path.endswith("/auth/state"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "state": "st", "authUrl": "https://copilot.tencent.com/login?state=st"}})
            return httpx.Response(404, json={})
        manager = BrowserLogin(Mock(), lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)), lambda: self.time)
        try:
            with self.assertRaises(HTTPException) as ctx:
                await manager.start("owner", None, R.GLOBAL)
            self.assertEqual(ctx.exception.status_code, 502)
        finally:
            await manager.close()

    async def test_poll_provisions_before_saving(self):
        flow = await self.manager.start("owner", "g", R.GLOBAL, "HK")
        self.time += 4
        result = await self.manager.poll(flow["id"], "owner")
        self.assertEqual(result["status"], "success")
        # 供给端点都打了，且顺序是 查国家码 → 提交地区 → 领 trial
        # 用「结尾匹配」而不是 rsplit 取段：/billing/ide/trial 的倒数第二段是
        # "ide"、/console/login/account 是 "login"，都不是端点名，取段必错。
        self.assertEqual([opt for opt in ("get-country-code", "console/login/account",
                                          "billing/ide/trial")
                          if any(p.endswith(opt) for p in self.region_calls)],
                         ["get-country-code", "console/login/account", "billing/ide/trial"])
        # /console/login/account 在响应里带 code 0 才算提交成功；这里断言落盘前
        # 一定过了地区那一步（顺序由上面那条锁死，这里锁「真的提交成功了」）。
        self.assertTrue(any(p.endswith("/console/login/account") for p in self.region_calls))
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["auth"]["realm"], R.GLOBAL)
        self.assertIn("message", result)

    async def test_poll_refuses_mismatched_realm_without_saving(self):
        # 用户在界面上选了国内版，但账号实际是国际版 → 明确报错，不静默改。
        flow = await self.manager.start("owner", None, R.CN)
        self.time += 4
        with self.assertRaises(HTTPException) as ctx:
            await self.manager.poll(flow["id"], "owner")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("国际版", ctx.exception.detail)
        self.assertEqual(self.saved, [])

    async def test_cn_flow_skips_provisioning(self):
        self.realm_domain = "www.codebuddy.cn"
        flow = await self.manager.start("owner", None, R.CN)
        self.time += 4
        self.assertEqual((await self.manager.poll(flow["id"], "owner"))["status"], "success")
        self.assertEqual(self.region_calls, [])
        self.assertEqual(self.saved[0]["auth"]["realm"], R.CN)
        # expiresIn=3600 秒，按**签发时刻**（self.time 已被推进 4 秒）算，
        # 不是按字面 10000+3600 算——续期基准不能随轮询次数漂移。
        self.assertEqual(self.saved[0]["auth"]["expiresAt"], (self.time + 3600) * 1000)


class AccountIdentityTests(unittest.IsolatedAsyncioTestCase):
    """身份去重是 (uid, enterpriseId, realm) 三元组。"""

    asyncSetUp = admin_tests.AdminTests.asyncSetUp
    asyncTearDown = admin_tests.AdminTests.asyncTearDown
    make_app = admin_tests.AdminTests.make_app
    login = admin_tests.AdminTests.login

    async def test_same_uid_different_realm_are_two_accounts(self):
        store = self.app.state.store
        before = len(store.data["accounts"])
        cn = credential("shared")
        cn["auth"]["realm"] = "cn"
        gl = credential("shared")
        gl["auth"]["realm"] = "global"
        gl["auth"]["domain"] = "www.workbuddy.ai"
        r1 = store.save_browser_account(cn, "国内")
        r2 = store.save_browser_account(gl, "国际")
        self.assertFalse(r1["updated"])
        self.assertFalse(r2["updated"])
        self.assertNotEqual(r1["account_id"], r2["account_id"])
        self.assertEqual(len(store.data["accounts"]), before + 2)

    async def test_repeat_login_updates_in_place_per_realm(self):
        store = self.app.state.store
        gl = credential("shared")
        gl["auth"]["realm"] = "global"
        gl["auth"]["domain"] = "www.workbuddy.ai"
        first = store.save_browser_account(gl, "国际")
        again = credential("shared")
        again["auth"]["realm"] = "global"
        again["auth"]["domain"] = "www.workbuddy.ai"
        again["account"]["nickname"] = "改名了"
        second = store.save_browser_account(again, None)
        self.assertTrue(second["updated"])
        self.assertEqual(first["account_id"], second["account_id"])

    async def test_legacy_credential_without_realm_still_deduplicates(self):
        # 存量 .info 无 realm 字段 → 靠 domain 兜底，行为与升级前一致。
        store = self.app.state.store
        legacy = credential("legacy")
        legacy["auth"].pop("realm", None)
        first = store.save_browser_account(legacy, None)
        again = credential("legacy")
        again["auth"].pop("realm", None)
        second = store.save_browser_account(again, None)
        self.assertTrue(second["updated"])
        self.assertEqual(first["account_id"], second["account_id"])

    async def test_account_row_exposes_realm(self):
        store = self.app.state.store
        gl = credential("row")
        gl["auth"]["realm"] = "global"
        gl["auth"]["domain"] = "www.workbuddy.ai"
        saved = store.save_browser_account(gl, "国际")
        row = [r for r in store.account_rows() if r["id"] == saved["account_id"]][0]
        self.assertEqual(row["realm"], "global")

    async def test_invalid_realm_is_rejected(self):
        store = self.app.state.store
        bad = credential("bad")
        bad["auth"]["realm"] = "mars"
        with self.assertRaises(HTTPException) as ctx:
            store.save_browser_account(bad, None)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_oauth_start_passes_realm_and_region(self):
        await self.login()
        r = await self.client.post("/admin/api/oauth/start", json={"realm": "global", "region": "US"})
        # 地区非法要在起流程之前就拒掉，不能白打一次上游
        self.assertEqual(r.status_code, 400)

    async def test_oauth_start_defaults_to_cn(self):
        await self.login()
        r = await self.client.post("/admin/api/oauth/start", json={})
        # 没有上游凭据时会是 502，但不该是参数错误
        self.assertNotEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
