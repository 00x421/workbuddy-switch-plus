"""管理控制台登录入口的回归测试。

覆盖三件事：
1. 管理员账号（用户名 + 口令）能进网页端——这正是原先缺失的入口；
2. 管理令牌（wbt_）能换一个浏览器会话来进网页端，但**不因此**获得高危权限；
3. 登录页的入口清单按部署情况给：没显式配 ADMIN_KEY 时不能摆「管理密钥」。

第 3 条防的是一类很隐蔽的坑：serve.py 在没配 ADMIN_KEY 时会现场随机一个，
桌面端托管（从不传 ADMIN_KEY）就是这样，用户永远拿不到那串值——
入口摆出来的唯一效果就是让人反复看到「管理密钥不正确」，还以为是密码记错了。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

import admin.server as admin_server

ADMIN_KEY = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="https://console.test")


class ConsoleLogin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN_KEY)
        self.session = client_for(self.app)
        await self.login_as_admin()

    async def asyncTearDown(self):
        await self.session.aclose()
        self.temp.cleanup()

    async def login_as_admin(self):
        """走「管理员账号」，也就是网页端现在默认的那个入口。"""
        r = await self.session.post("/admin/api/login", json={"username": "admin", "password": ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.session.headers["X-CSRF-Token"] = r.json()["csrf"]

    async def make_token(self, scope="admin", name="ops") -> dict:
        r = await self.session.post("/admin/api/tokens", json={"name": name, "scope": scope})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ── 入口清单 ─────────────────────────────────────────────────
    async def test_login_options_reports_configured_entries(self):
        r = await self.session.get("/admin/api/login-options")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # 显式配了 ADMIN_KEY（测试里就是传了 ADMIN_KEY），所以单密钥入口该摆出来
        self.assertTrue(body["legacy_key"])
        self.assertFalse(body["tokens"], "还没建过令牌，不该摆「管理令牌」入口")
        await self.make_token()
        self.assertTrue((await self.session.get("/admin/api/login-options")).json()["tokens"])

    async def test_login_options_hides_fabricated_legacy_key(self):
        """桌面端托管形态：密钥是现场随机的，入口必须藏掉——而且接口也要一起关掉。"""
        app = admin_server.create_app(self.root / "fabricated", self.auth, API, ADMIN_KEY,
                                      legacy_key_configured=False)
        async with client_for(app) as c:
            body = (await c.get("/admin/api/login-options")).json()
            self.assertFalse(body["legacy_key"])
            # 藏了就必须真的不认：首启那次随机密钥恰好等于打印出来的首启口令，
            # 如果接口还认它，就会出现「昨天能登今天不能登」这种没法排查的现象。
            r = await c.post("/admin/api/login", json={"key": ADMIN_KEY})
            self.assertEqual(r.status_code, 401, r.text)
            # 但管理员账号这条路照样通
            r = await c.post("/admin/api/login", json={"username": "admin", "password": ADMIN_KEY})
            self.assertEqual(r.status_code, 200, r.text)

    async def test_login_options_needs_no_auth(self):
        """登录页自己要先拿到入口清单，所以这个接口不能要求登录。"""
        async with client_for(self.app) as fresh:
            self.assertEqual((await fresh.get("/admin/api/login-options")).status_code, 200)

    # ── 管理令牌换会话 ───────────────────────────────────────────
    async def test_token_login_grants_console_session(self):
        token = (await self.make_token(scope="admin"))["token"]
        async with client_for(self.app) as c:
            r = await c.post("/admin/api/login", json={"token": token})
            self.assertEqual(r.status_code, 200, r.text)
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            me = await c.get("/admin/api/me")
            self.assertEqual(me.status_code, 200, me.text)
            self.assertEqual(me.json()["via"], "token", "令牌换来的会话要标出来源是令牌")
            self.assertEqual(me.json()["role"], "admin")
            # admin scope 的令牌换来的会话，日常读接口照样能用
            self.assertEqual((await c.get("/admin/api/overview")).status_code, 200)

    async def test_token_login_still_blocked_from_high_risk_endpoints(self):
        """令牌进得了控制台 ≠ 令牌能改用户和令牌。高危接口只认会话登录。"""
        token = (await self.make_token(scope="admin"))["token"]
        async with client_for(self.app) as c:
            r = await c.post("/admin/api/login", json={"token": token})
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            # 管理令牌自身的增删改查：整组都是 session_admin
            self.assertEqual((await c.get("/admin/api/tokens")).status_code, 403)
            self.assertEqual((await c.post("/admin/api/tokens", json={"name": "x", "scope": "readonly"})).status_code, 403)
            self.assertEqual((await c.patch("/admin/api/tokens/some-id", json={"enabled": False})).status_code, 403)
            self.assertEqual((await c.delete("/admin/api/tokens/some-id")).status_code, 403)
            # 用户写操作同样只认会话（读接口是 viewer，令牌会话可以看）
            self.assertEqual((await c.get("/admin/api/users")).status_code, 200)
            self.assertEqual((await c.post("/admin/api/users", json={"username": "u1", "password": "password1"})).status_code, 403)
            self.assertEqual((await c.delete("/admin/api/users/admin")).status_code, 403)

    async def test_readonly_token_login_is_viewer(self):
        token = (await self.make_token(scope="readonly", name="ci"))["token"]
        async with client_for(self.app) as c:
            r = await c.post("/admin/api/login", json={"token": token})
            self.assertEqual(r.status_code, 200, r.text)
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            self.assertEqual((await c.get("/admin/api/me")).json()["role"], "viewer")
            self.assertEqual((await c.get("/admin/api/overview")).status_code, 200)
            self.assertEqual((await c.post("/admin/api/keys", json={"name": "x"})).status_code, 403)

    async def test_revoked_token_kills_its_console_session(self):
        """令牌换来的会话不是 12 小时的后门票：令牌一撤，会话立刻失效。"""
        created = await self.make_token(scope="admin")
        token, tid = created["token"], created["id"]
        async with client_for(self.app) as c:
            r = await c.post("/admin/api/login", json={"token": token})
            c.headers["X-CSRF-Token"] = r.json()["csrf"]
            self.assertEqual((await c.get("/admin/api/overview")).status_code, 200)

            revoked = await self.session.delete("/admin/api/tokens/" + tid)
            self.assertEqual(revoked.status_code, 200, revoked.text)

            after = await c.get("/admin/api/overview")
            self.assertEqual(after.status_code, 401, "令牌已被撤销，会话不该还能用")

    async def test_disabled_token_login_rejected(self):
        created = await self.make_token(scope="admin")
        token, tid = created["token"], created["id"]
        patched = await self.session.patch("/admin/api/tokens/" + tid, json={"enabled": False})
        self.assertEqual(patched.status_code, 200, patched.text)
        async with client_for(self.app) as c:
            r = await c.post("/admin/api/login", json={"token": token})
            self.assertEqual(r.status_code, 401, r.text)

    async def test_bogus_token_login_rejected(self):
        async with client_for(self.app) as c:
            for value in ("wbt_does-not-exist", "not-a-token", ""):
                r = await c.post("/admin/api/login", json={"token": value})
                self.assertEqual(r.status_code, 401, f"{value!r} → {r.status_code} {r.text}")

    async def test_expired_token_login_rejected(self):
        created = await self.make_token(scope="admin")
        token, tid = created["token"], created["id"]
        # 直接把过期时间改到过去（走 store，避免依赖 PATCH 对 expires_at 的校验宽松度）
        with self.app.state.store.lock:
            row = self.app.state.store.data["api_tokens"][tid]
            row["expires_at"] = int(time.time()) - 60
            self.app.state.store.save()
        async with client_for(self.app) as c:
            r = await c.post("/admin/api/login", json={"token": token})
            self.assertEqual(r.status_code, 401, r.text)


class TaskLogKinds(unittest.IsolatedAsyncioTestCase):
    """任务流水的类别白名单。

    「活跃上报」是后加的一类日志（kind=active）。流水接口的筛选与清空各带一份类别
    白名单——只加一处、漏另一处是很容易发生的：清空接口会把 active 当非法值挡成
    400，而用户看到的现象只是「按类别筛选后点了清空没反应」，排查成本很高。
    这里把 active 在两个接口上都钉死。
    """

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN_KEY)
        self.session = client_for(self.app)
        await self.login_as_admin()

    async def asyncTearDown(self):
        await self.session.aclose()
        self.temp.cleanup()

    async def login_as_admin(self):
        r = await self.session.post("/admin/api/login", json={"username": "admin", "password": ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.session.headers["X-CSRF-Token"] = r.json()["csrf"]

    async def test_active_kind_readable(self):
        r = await self.session.get("/admin/api/pool/checkin-logs", params={"kind": "active"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsInstance(r.json(), list, r.text)

    async def test_active_kind_accepted_by_clear(self):
        # 清空接口的类别白名单漏了 active 就会变成 400——界面上表现为「清不动」
        r = await self.session.post("/admin/api/pool/checkin-logs/clear", params={"kind": "active"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])

    async def test_unknown_kind_still_rejected(self):
        """反向参照：白名单没被放宽成「什么都收」。"""
        r = await self.session.post("/admin/api/pool/checkin-logs/clear", params={"kind": "not-a-kind"})
        self.assertEqual(r.status_code, 400, r.text)

    async def test_clear_accepts_every_declared_kind(self):
        """clear 的白名单必须与 admin.pool.TASK_KINDS 同源，逐条过一遍。

        这条是拿真实事故换来的：server.py 里曾写成 `pool.TASK_KINDS`（那个作用域里
        pool 是实例，常量却是模块级的）→ AttributeError → 对**任何**非空 kind 回 500，
        四个类别一起清不动，而界面上只是「点了没反应」。
        逐条遍历常量而不是硬编码四个字符串：以后再加类别只改 pool.py 一处，
        这里会自动覆盖到，不会重演「看得见、清不掉」。
        """
        from admin.pool import TASK_KINDS
        self.assertIn("active", TASK_KINDS, "活跃上报的类别必须登记在唯一白名单里")
        for kind in TASK_KINDS:
            r = await self.session.post("/admin/api/pool/checkin-logs/clear", params={"kind": kind})
            self.assertEqual(r.status_code, 200, f"kind={kind!r} → {r.status_code} {r.text}")
            self.assertTrue(r.json()["ok"])
            g = await self.session.get("/admin/api/pool/checkin-logs", params={"kind": kind})
            self.assertEqual(g.status_code, 200, f"kind={kind!r} 读取 → {g.status_code} {g.text}")
            self.assertIsInstance(g.json(), list)
