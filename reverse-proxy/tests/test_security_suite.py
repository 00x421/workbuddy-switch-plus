"""多密钥分发 / IP·模型白名单 / 额度 / 多用户鉴权（PBKDF2·防爆破·只读令牌）的验证。"""
import contextlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import httpx

import admin.server as admin_server
from admin import keysvc

ADMIN_KEY = "admin-test-credential-long-enough"
API = "client-existing-key"


@contextlib.contextmanager
def trust_proxy(on=True):
    """临时打开/关闭「采信转发头」（`admin/iputil.py:17 trust_proxy()`）。

    为什么必须有它：默认 `WB_TRUST_PROXY != "1"` 时 `client_ip*` 一律返回 TCP
    对端的真实 IP，测试里的 `X-Forwarded-For` **完全不生效**。想验「外部来源
    被挡住」就只能先把采信打开，否则塞进去的头被静默忽略，用例永远看到
    127.0.0.1 —— 绿得没有任何意义。

    用完必须还原成原值，不能 `pop` 了事：别的用例（或外部环境）可能本来就
    设了这个变量，一把删掉会把它们的语义改掉。
    """
    key = "WB_TRUST_PROXY"
    saved = os.environ.get(key)
    if on:
        os.environ[key] = "1"
    else:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


class SecuritySuite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.make_app()

    def make_app(self):
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN_KEY)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login_key(self):
        r = await self.client.post("/admin/api/login", json={"key": ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.headers["X-CSRF-Token"] = r.json()["csrf"]
        return r

    async def login_user(self, username="admin", password=ADMIN_KEY):
        r = await self.client.post("/admin/api/login", json={"username": username, "password": password})
        return r

    # ── 多用户鉴权 ───────────────────────────────────────────────
    async def test_multi_user_login_and_role(self):
        r = await self.login_user()
        self.assertEqual(r.status_code, 200, r.text)
        self.client.headers["X-CSRF-Token"] = r.json()["csrf"]
        me = await self.client.get("/admin/api/me")
        self.assertEqual(me.json()["role"], "admin")
        self.assertEqual(me.json()["username"], "admin")
        # 口令错误 → 401
        self.assertEqual((await self.client.post("/admin/api/login", json={"username": "admin", "password": "nope"})).status_code, 401)
        # 只读用户：能看到概览，但不能改密钥
        add = await self.client.post("/admin/api/users", json={"username": "ro", "password": "viewer-pass", "role": "viewer"})
        self.assertEqual(add.status_code, 200, add.text)
        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")
        r = await self.login_user("ro", "viewer-pass")
        self.client.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 200)
        self.assertEqual((await self.client.post("/admin/api/keys", json={"name": "x"})).status_code, 403)

    async def test_bruteforce_lock_by_username(self):
        for _ in range(8):
            self.assertEqual((await self.client.post("/admin/api/login", json={"username": "admin", "password": "bad"})).status_code, 401)
        self.assertEqual((await self.login_user("admin", ADMIN_KEY)).status_code, 429)

    async def test_user_crud_keeps_one_admin(self):
        await self.login_key()
        r = await self.client.post("/admin/api/users", json={"username": "second", "password": "second-pass", "role": "admin"})
        self.assertEqual(r.status_code, 200, r.text)
        users = (await self.client.get("/admin/api/users")).json()
        self.assertEqual({u["username"] for u in users}, {"admin", "second"})
        # 降级/删除最后一个管理员应被拒
        await self.client.request("PATCH", "/admin/api/users/second", json={"role": "viewer"})
        self.assertEqual((await self.client.request("PATCH", "/admin/api/users/admin", json={"role": "viewer"})).status_code, 400)
        self.assertEqual((await self.client.delete("/admin/api/users/admin")).status_code, 400)
        self.assertEqual((await self.client.delete("/admin/api/users/second")).status_code, 200)

    async def test_change_password_revokes_sessions(self):
        await self.login_key()
        await self.client.post("/admin/api/users", json={"username": "u1", "password": "u1-password", "role": "viewer"})
        other = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")
        r = await other.post("/admin/api/login", json={"username": "u1", "password": "u1-password"})
        other.headers["X-CSRF-Token"] = r.json()["csrf"]
        self.assertEqual((await other.get("/admin/api/overview")).status_code, 200)
        # 改密码 → 该用户既有会话立即失效
        await self.client.request("PATCH", "/admin/api/users/u1", json={"password": "u1-newpass"})
        self.assertEqual((await other.get("/admin/api/overview")).status_code, 401)
        await other.aclose()

    # ── 作用域化管理令牌 ─────────────────────────────────────────
    async def test_scoped_tokens(self):
        await self.login_key()
        created = await self.client.post("/admin/api/tokens", json={"name": "ci", "scope": "readonly"})
        self.assertEqual(created.status_code, 200, created.text)
        token = created.json()["token"]
        self.assertTrue(token.startswith("wbt_"))
        # 落盘只有哈希，绝不含明文
        self.assertNotIn(token, self.app.state.store.path.read_text(encoding="utf-8"))
        pub = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")
        pub.headers["Authorization"] = "Bearer " + token
        self.assertEqual((await pub.get("/admin/api/overview")).status_code, 200)
        # 只读令牌不能改东西
        self.assertEqual((await pub.post("/admin/api/keys", json={"name": "x"})).status_code, 403)
        # 令牌自身的管理接口只收会话管理员
        self.assertEqual((await pub.get("/admin/api/tokens")).status_code, 403)
        await pub.aclose()
        # admin scope 的令牌可以管理，但仍旧不能管令牌
        adm = await self.client.post("/admin/api/tokens", json={"name": "ops", "scope": "admin"})
        a = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://console.test")
        a.headers["Authorization"] = "Bearer " + adm.json()["token"]
        self.assertEqual((await a.post("/admin/api/keys", json={"name": "via-token"})).status_code, 200)
        self.assertEqual((await a.delete("/admin/api/tokens/" + adm.json()["id"])).status_code, 403)
        await a.aclose()

    # ── 密钥策略 ─────────────────────────────────────────────────
    async def test_key_ip_and_model_whitelist(self):
        await self.login_key()
        r = await self.client.post("/admin/api/keys", json={
            "name": "受限", "ip_allowlist": ["10.0.0.5/32"], "models": ["deepseek-v4-flash"], "quota": 100})
        self.assertEqual(r.status_code, 200, r.text)
        key = r.json()["key"]
        # 来源 IP 不在白名单 → 400
        bad_ip = await self.client.post("/v1/chat/completions",
                                        headers={"Authorization": "Bearer " + key},
                                        json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(bad_ip.status_code, 400, bad_ip.text)
        self.assertEqual(bad_ip.json()["detail"]["error"]["code"], "ip_not_allowed")
        # 换一把本机白名单的密钥，模型不在白名单 → 400
        r2 = await self.client.post("/admin/api/keys", json={
            "name": "本机", "ip_allowlist": ["127.0.0.0/8"], "models": ["only-this-model"]})
        key2 = r2.json()["key"]
        bad_model = await self.client.post("/v1/chat/completions",
                                           headers={"Authorization": "Bearer " + key2},
                                           json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(bad_model.status_code, 400, bad_model.text)
        self.assertEqual(bad_model.json()["detail"]["error"]["code"], "model_not_allowed")

    async def test_invalid_cidr_rejected_on_write(self):
        await self.login_key()
        # 带空格的条目在写入时被归一化（不会被拒，但必须存成可匹配的形态）
        r = await self.client.post("/admin/api/keys", json={"name": "spaced", "ip_allowlist": [" 10.0.0.0/8"]})
        self.assertEqual(r.status_code, 200, r.text)
        stored = self.app.state.store.data["keys"][r.json()["id"]]["ip_allowlist"]
        self.assertEqual(stored, ["10.0.0.0/8"])
        # 真正非法的 CIDR → 400
        r = await self.client.post("/admin/api/keys", json={"name": "bad", "ip_allowlist": ["not-a-cidr"]})
        self.assertEqual(r.status_code, 400)

    def test_keysvc_quota_and_realm_and_model_rules(self):
        from fastapi import HTTPException
        base = {"enabled": True, "expires_at": None, "quota": 0, "used_tokens": 0,
                "quota_credit": 0, "used_credit": 0, "ip_allowlist": [], "max_ips": 0,
                "models": [], "realm": "", "ips": []}
        # 额度用尽 → 429
        with self.assertRaises(HTTPException) as cm:
            keysvc.validate({**base, "quota": 10, "used_tokens": 10}, "127.0.0.1", "m")
        self.assertEqual(cm.exception.status_code, 429)
        # 停用 → 403
        with self.assertRaises(HTTPException) as cm:
            keysvc.validate({**base, "enabled": False}, "127.0.0.1", "m")
        self.assertEqual(cm.exception.status_code, 403)
        # 过期 → 403
        with self.assertRaises(HTTPException) as cm:
            keysvc.validate({**base, "expires_at": int(time.time()) - 5}, "127.0.0.1", "m")
        self.assertEqual(cm.exception.status_code, 403)
        # 版本归属：限国内版却发 global: 模型 → 400
        with self.assertRaises(HTTPException) as cm:
            keysvc.validate({**base, "realm": "cn"}, "127.0.0.1", "global:deepseek-v4-flash")
        self.assertEqual(cm.exception.status_code, 400)
        # 模型白名单命中（cn: 前缀等价）
        keysvc.validate({**base, "models": ["cn:deepseek-v4-flash"]}, "127.0.0.1", "deepseek-v4-flash")
        # max_ips 超出 → 400
        with self.assertRaises(HTTPException) as cm:
            keysvc.validate({**base, "max_ips": 1, "ips": ["1.1.1.1"]}, "2.2.2.2", "m")
        self.assertEqual(cm.exception.status_code, 400)

    async def test_reset_usage(self):
        await self.login_key()
        r = await self.client.post("/admin/api/keys", json={"name": "q", "quota": 5})
        kid = r.json()["id"]
        with self.app.state.store.lock:
            self.app.state.store.data["keys"][kid]["used_tokens"] = 5
            self.app.state.store.save()
        self.assertEqual((await self.client.post(f"/admin/api/keys/{kid}/reset-usage")).status_code, 200)
        self.assertEqual(self.app.state.store.data["keys"][kid]["used_tokens"], 0)

    # ── 入站 IP 管控 ─────────────────────────────────────────────
    async def test_ip_guard_blacklist(self):
        """黑名单命中一个**外部**来源时确实挡住。

        这里刻意不拿 127.0.0.1 当靶子：回环地址在中间件里是硬编码直通的
        （部署在本机的人本来就能改 state.json，挡他只会把面板锁死），
        用本机试会永远拿到 200，测出来的是「豁免生效」而不是「策略生效」。

        而且**必须先把 WB_TRUST_PROXY 打开**（`admin/iputil.py:17`）：默认不采信
        转发头，peer 永远是 127.0.0.1，塞 `X-Forwarded-For` 根本不起作用 ——
        不打开的话这两条用例验的是空气，绿得毫无意义。
        """
        with trust_proxy():
            await self.login_key()
            self.assertEqual((await self.client.post("/admin/api/security/config", json={"enabled": False, "mode": "blacklist"})).status_code, 200)
            r = await self.client.post("/admin/api/security/rules", json={"kind": "deny", "cidr": "203.0.113.0/24"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((await self.client.post("/admin/api/security/rules", json={"kind": "deny", "cidr": "bogus"})).status_code, 400)
            self.assertEqual((await self.client.post("/admin/api/security/config", json={"enabled": True, "mode": "blacklist"})).status_code, 200)
            # 外部来源命中 deny → 403（匿名也挡：身份不清不等于可以放行）
            outside = {"X-Forwarded-For": "203.0.113.7"}
            self.assertEqual((await self.client.get("/admin/api/overview", headers=outside)).status_code, 403)
            # 本机依旧直通，不受这条规则影响
            self.assertEqual((await self.client.get("/admin/api/overview")).status_code, 200)
            logs = self.app.state.store.data.get("ip_access_logs", [])
            self.assertTrue(any(l["blocked"] for l in logs))
        # 直接关掉开关以便后续用例不受影响
        with self.app.state.store.lock:
            self.app.state.store.data["security"]["enabled"] = False

    async def test_ip_guard_rescue_needs_identity(self):
        """被挡住时，「加放行规则」这条自救后路必须凭身份才给。

        否则任何人被墙挡住之后都能把自己写回白名单，IP 管控就只是摆设。
        身份不对时返 401 而不是 403 —— 他缺的是身份，不是权限。
        """
        with trust_proxy():
            await self.login_key()
            await self.client.post("/admin/api/security/config", json={"enabled": False, "mode": "blacklist"})
            await self.client.post("/admin/api/security/rules", json={"kind": "deny", "cidr": "203.0.113.0/24"})
            await self.client.post("/admin/api/security/config", json={"enabled": True, "mode": "blacklist"})
            outside = {"X-Forwarded-For": "203.0.113.7"}
            body = {"kind": "allow", "cidr": "203.0.113.7"}
            self.assertEqual((await self.client.post("/admin/api/security/rules", json=body, headers=outside)).status_code, 401)
            self.assertEqual(
                (await self.client.post("/admin/api/security/rules", json=body,
                                        headers={**outside, "X-Admin-Token": "not-a-real-key"})).status_code,
                401,
            )
        with self.app.state.store.lock:
            self.app.state.store.data["security"]["enabled"] = False

    async def test_audit_log_has_login(self):
        await self.login_key()
        logs = (await self.client.get("/admin/api/audit-logs")).json()
        self.assertTrue(any(l["action"] == "login" for l in logs))

    async def test_audit_entry_is_persisted_to_state_json(self):
        """审计要能扛住重启。

        多数调用点是在自己那次 `store.save()` **之后**才 `audit()` 的（改池设置就是），
        如果 `audit()` 只往内存里塞一条、不自己落盘，服务一重启这条记录就没了。
        """
        await self.login_key()
        r = await self.client.patch("/admin/api/pool/settings", json={"routing": "least_used"})
        self.assertEqual(r.status_code, 200, r.text)
        state = json.loads((self.root / "management" / "state.json").read_text(encoding="utf-8"))
        actions = [e["action"] for e in state.get("audit_logs", [])]
        self.assertIn("update_pool_settings", actions)

    async def test_check_models(self):
        await self.login_key()
        r = await self.client.post("/admin/api/keys/check-models", json={"models": ["definitely-not-a-real-model"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("definitely-not-a-real-model", r.json()["unknown"])


if __name__ == "__main__":
    unittest.main()
