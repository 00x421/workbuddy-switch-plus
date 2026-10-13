"""HyactCrack 技术支持 —— 锁住「低权限拿不到明文密钥」这个不变量

对应已修复的 H-1：
  · /admin/api/overview 只要 viewer，却内联 keysvc.list_keys()，
    而它带回明文 secret —— 一枚 readonly wbt_ 令牌就能读走全站密钥。
  · /admin/api/keys/{kid}/export 同样只要 viewer，导出内容含明文。

两条都是**实测坐实**的（见 _hyact_work/poc_overview_key_leak.py），
所以要有测试守着，免得以后重构时又被"顺手"放回去。
"""
from __future__ import annotations

import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from admin import keysvc


ADMIN = "wb-keysecret-admin-key-long-enough-1"


def build(tmp):
    """建一个隔离实例。

    ⚠️ 必须重新对齐 ADMIN_KEY：多个测试文件都在模块级写这个环境变量，
    而 build_app 是从 env 读的 —— 后 import 的会覆盖先 import 的，
    于是拿本文件那个常量去登录会被 401 顶回来（单跑绿、全量红）。
    """
    os.environ["ADMIN_KEY"] = ADMIN
    import serve
    importlib.reload(serve)
    return TestClient(serve.build_app(
        ["--data", str(tmp), "--accounts", str(tmp / "acc.json")]))


class SecretVisibilityBase(unittest.TestCase):
    """admin 会话 + 一枚真实密钥（需要它的明文来断言抹除是否彻底）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-secvis-"))
        (self.tmp / "acc.json").write_text("[]", encoding="utf-8")
        self.client = build(self.tmp)
        r = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(r.status_code, 200, r.text)
        self.admin_h = {"X-Admin-Token": r.json()["token"]}

        r = self.client.post("/admin/api/keys", json={"name": "secret-probe"},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        made = r.json()
        self.kid = made["id"]
        self.secret = made["key"]
        self.store = self.client.app.state.store

    def token(self, name):
        """造一枚 readonly 令牌，返回 (明文令牌, 响应体)。"""
        r = self.client.post("/admin/api/tokens",
                             json={"name": name, "scope": "readonly"},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        return body.get("token") or body.get("value") or "", body

    def as_readonly(self, token, method, path):
        """以纯令牌身份发请求。

        ⚠️ 必须先清 cookie。current_user() 是会话优先的：
        TestClient 会自动带上刚才 admin 登录发的 cookie，于是请求被认成
        admin 会话，令牌根本没被看 —— 测出来的是"admin 能看到明文"，
        与要验的命题完全相反。
        """
        self.client.cookies.clear()
        return self.client.request(method, path,
                                   headers={"Authorization": "Bearer " + token})


class ListKeysUnitTest(SecretVisibilityBase):
    """单元层：`keysvc.list_keys` 的默认行为。"""

    def test_default_hides_secret(self):
        """⭐ 默认不回显明文 —— 默认安全，要显式说"这里可以给"。"""
        rows = keysvc.list_keys(self.store)
        probe = [r for r in rows if r["id"] == self.kid]
        self.assertEqual(len(probe), 1)
        self.assertEqual(probe[0]["secret"], "",
                         "list_keys 默认必须抹掉明文 secret")

    def test_reveals_only_when_asked(self):
        rows = keysvc.list_keys(self.store, reveal_secret=True)
        probe = [r for r in rows if r["id"] == self.kid]
        self.assertEqual(probe[0]["secret"], self.secret)

    def test_secret_key_still_present(self):
        """`secret` 这个**键**要留着（只抹值）。

        前端读 `k.secret` 渲染「复制」按钮；键没了会变 undefined
        而不是空串，可能渲染出 "undefined" 字样。
        """
        rows = keysvc.list_keys(self.store)
        probe = [r for r in rows if r["id"] == self.kid][0]
        self.assertIn("secret", probe)

    def test_hint_survives_masking(self):
        """只抹明文，`hint`/`prefix` 留着 —— 列表页靠它们区分是哪一枚。"""
        rows = keysvc.list_keys(self.store)
        probe = [r for r in rows if r["id"] == self.kid][0]
        self.assertTrue(probe.get("hint") or probe.get("prefix"),
                        "hint/prefix 被一起抹掉了，列表页将无法区分密钥")


class OverviewRoleGateTest(SecretVisibilityBase):
    """`/admin/api/overview` 按角色决定是否回显明文。

    ⚠️ 测试顺序有讲究：`as_readonly()` 会清掉 cookie（见它的注释），
    清掉之后 `self.admin_h` 就不再是有效身份了。
    所以**每个用例只测一个身份**，别在同一个用例里先 readonly 后 admin ——
    那会得到 401 而不是预期的结果。下面 admin / readonly 分开写。
    """

    def test_admin_sees_secret(self):
        """管理员要能拿到明文 —— 否则面板「复制密钥」就废了。"""
        r = self.client.get("/admin/api/overview", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        probe = [k for k in r.json()["keys"] if k["id"] == self.kid]
        self.assertEqual(len(probe), 1, "管理员看不到自己建的密钥")
        self.assertEqual(probe[0]["secret"], self.secret,
                         "管理员看不到明文，面板复制功能会坏")

    def test_readonly_token_sees_no_secret(self):
        """⭐ 核心回归：readonly 令牌看不到任何明文。"""
        tok, _ = self.token("probe-viewer")
        self.assertTrue(tok, "没拿到只读令牌明文")
        r = self.as_readonly(tok, "GET", "/admin/api/overview")
        self.assertEqual(r.status_code, 200, f"readonly 应当能读概览：{r.status_code}")
        body = r.json()
        for k in body.get("keys") or []:
            self.assertEqual(k.get("secret"), "",
                             f"readonly 读到了明文密钥：{k.get('id')}")
        self.assertNotIn(self.secret, r.text, "响应体里出现了明文密钥")

    def test_readonly_cannot_export(self):
        """⭐ export 会把密钥交出去，必须只给 admin。"""
        tok, _ = self.token("probe-viewer2")
        r = self.as_readonly(
            tok, "GET", f"/admin/api/keys/{self.kid}/export?client=ccswitch")
        self.assertEqual(r.status_code, 403, "readonly 竟然可以导出密钥配置")
        self.assertNotIn(self.secret, r.text)

    def test_admin_can_export(self):
        """管理员导出照常可用 —— 别把功能一起关掉。"""
        r = self.client.get(f"/admin/api/keys/{self.kid}/export?client=ccswitch",
                            headers=self.admin_h)
        self.assertEqual(r.status_code, 200, f"管理员导出失败：{r.status_code} {r.text}")
        self.assertIn(self.secret, r.text, "导出内容里没有密钥明文")


class PoolSecretVisibilityTest(SecretVisibilityBase):
    """pool 段里的上游 Bearer 凭据（`wb2api_key`）同样只给管理员。

    这是修 H-1 时漏掉的**同一个响应体的另一半**：
    `keys` 段收口了，`pool` 段还在全量摊平 `dict(store.data["pool"])`，
    而那里存着发给上游的 `wb2api_key`。已独立复现坐实。

    端点有两个（overview 与 pool/summary），两个都要覆盖 ——
    只测一个的话，另一个漏了不会被发现。
    """

    UPSTREAM = "sk-wb2api-UPSTREAM-SECRET-abcdef123456"

    def setUp(self):
        super().setUp()
        r = self.client.patch("/admin/api/pool/settings",
                              json={"wb2api_base": "http://127.0.0.1:1",
                                    "wb2api_key": self.UPSTREAM},
                              headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)

    def test_admin_sees_upstream_key(self):
        """管理员要能看到 —— 面板的「上游设置」表单靠它回显。"""
        r = self.client.get("/admin/api/overview", headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pool"].get("wb2api_key"), self.UPSTREAM,
                         "管理员读不到上游密钥，设置表单会显示成空")

    def test_readonly_overview_hides_upstream_key(self):
        """⭐ overview：readonly 拿不到上游密钥。"""
        tok, _ = self.token("pool-probe-1")
        r = self.as_readonly(tok, "GET", "/admin/api/overview")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(self.UPSTREAM, r.text,
                         "readonly 从 overview 读到了上游密钥明文")
        self.assertNotIn("wb2api_key", r.json().get("pool") or {},
                         "pool 段里仍带着 wb2api_key 这个键")

    def test_readonly_pool_summary_hides_upstream_key(self):
        """⭐ pool/summary：**第二个入口**，同样不能漏。"""
        tok, _ = self.token("pool-probe-2")
        r = self.as_readonly(tok, "GET", "/admin/api/pool/summary")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(self.UPSTREAM, r.text,
                         "readonly 从 pool/summary 读到了上游密钥明文")
        self.assertNotIn("wb2api_key", r.json().get("pool") or {})

    def test_readonly_still_gets_harmless_pool_fields(self):
        """脱敏别把只读面板需要的字段一起抹掉。

        `routing` / `spread` / 各种 auto_* 是面板要显示的运行参数，
        不含凭据。若白名单写窄了，只读面板会一片空白。
        """
        tok, _ = self.token("pool-probe-3")
        r = self.as_readonly(tok, "GET", "/admin/api/overview")
        pool = r.json().get("pool") or {}
        for field in ("routing", "spread", "auto_checkin", "wb2api_base"):
            self.assertIn(field, pool, f"只读视图少了非敏感字段 {field}")

    def test_base_url_is_not_secret(self):
        """上游**地址**要留着 —— 界面得显示「连的是哪个上游」。"""
        tok, _ = self.token("pool-probe-4")
        r = self.as_readonly(tok, "GET", "/admin/api/overview")
        self.assertEqual((r.json().get("pool") or {}).get("wb2api_base"),
                         "http://127.0.0.1:1")


class ReadonlyScopeTest(SecretVisibilityBase):
    """readonly 身份能看到多少「后台内部状态」。

    原本这类端点一律只要 `viewer`，但其中两个会给到只读身份本不该拿的东西：
      · `/admin/api/users`      -> 控制台账号清单 + 角色（登录爆破的输入）
      · `/admin/api/audit-logs` -> 全部运维动作 + **终端用户邮箱**（PII）
    同一段的写接口本来就是 `session_admin`，只有读接口漏了，口径不一致。
    """

    def test_readonly_cannot_list_console_users(self):
        tok, _ = self.token("scope-users")
        r = self.as_readonly(tok, "GET", "/admin/api/users")
        self.assertIn(r.status_code, (401, 403),
                      f"readonly 读到了控制台账号清单：{r.text[:120]}")

    def test_readonly_cannot_read_audit_logs(self):
        tok, _ = self.token("scope-audit")
        r = self.as_readonly(tok, "GET", "/admin/api/audit-logs")
        self.assertIn(r.status_code, (401, 403),
                      f"readonly 读到了审计日志：{r.text[:120]}")

    def test_readonly_can_still_read_overview(self):
        """别把只读面板一起关死 —— overview 是它的主数据源。"""
        tok, _ = self.token("scope-ok")
        r = self.as_readonly(tok, "GET", "/admin/api/overview")
        self.assertEqual(r.status_code, 200, r.text)

    def test_admin_session_still_reads_both(self):
        """后台自己的界面靠这两个接口，管理员必须照常能读。"""
        for path in ("/admin/api/users", "/admin/api/audit-logs"):
            r = self.client.get(path, headers=self.admin_h)
            self.assertEqual(r.status_code, 200, f"{path} 管理员读不到：{r.text[:120]}")


class SessionRoleDefaultTest(SecretVisibilityBase):
    """会话记录缺 `role` 时必须**降权**，不是提权。

    同一模式的实例：`revoke_user_sessions` 的 `username` 上修过一次
    （缺字段被当成有效值）。role 更危险 —— 缺字段若能当 admin，
    等于「数据不完整就自动放行」。

    走真实 HTTP 而不是手搓 Request：`_session_record` 要查 cookie、
    查 sessions 表、算空闲超时，手搓对象很容易测到一个不存在的路径。
    """

    def test_missing_role_falls_back_to_viewer(self):
        """造一条「有会话、但记录里没有 role」的会话，看它拿到什么角色。

        ⚠️ 会话表是 `store.sessions` 这个**顶层属性**，不是 `store.data[...]`
        （见 security.py:406）。键是 `digest(sid)`，且必须有 `expires`
        与 `csrf`，否则 `_session_record` 直接判它无效。
        """
        from admin import security as sec
        store = self.client.app.state.store
        sid = "no-role-sid"
        now = time.time()
        with store.lock:
            store.sessions[sec.digest(sid)] = {
                "csrf": "x", "expires": now + 3600,
                "username": "ghost", "via": "session", "iat": int(now),
                "token_id": "",
            }
        try:
            c = TestClient(self.client.app)
            c.cookies.set(sec.COOKIE, sid)
            r = c.get("/admin/api/me")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json().get("role"), "viewer",
                             "缺 role 的会话被当成了 admin —— 缺省值方向反了")
        finally:
            with store.lock:
                store.sessions.pop(sec.digest(sid), None)


class LoginSessionCredentialTest(SecretVisibilityBase):
    """登录下发的会话凭据：cookie 与响应体 token 是同一份，属性必须正确。

    这不是「漏洞测试」，而是**锁住已有的正确性质**，并记录一个已知取舍：

      · cookie 侧 `HttpOnly + SameSite=strict + Path=/admin` —— 正确；
      · 响应体里的 `token` 是 `X-Admin-Token` 的值，供跨源调用方使用
        （桌面端 SPA 走 credentials:"omit"，SameSite=strict 的 cookie 带不上）；
      · ⚠️ 已知取舍：走 `X-Admin-Token` 头**跳过 CSRF**（有意设计，
        见 security.bearer_session）。因为浏览器不会自动附加自定义头，
        它不构成自动可利用的 CSRF（实测见 `_hyact_work/poc_cors_csrf.py`），
        但代价是 XSS 的后果从「替用户发请求」升级为「完整接管会话」。

    我一度改成「只在跨源时才回传 token」，全量回归显示有 **25 处**调用方
    （跨 5 个测试文件）依赖这个字段做后续鉴权 —— 说明它是被广泛使用的
    正式契约，不是能随手收紧的实现细节，故撤回该改动。
    """

    def test_login_returns_both_csrf_and_token(self):
        r = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("csrf", body, "没有 csrf，写接口无法使用")
        self.assertIn("token", body, "跨源调用方与既有调用方都依赖这个字段")
        self.assertEqual(len(body["token"]), 43)

    def test_cookie_is_httponly_and_strict(self):
        r = self.client.post("/admin/api/login", json={"key": ADMIN})
        cookie = r.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie.replace("samesite", "SameSite"))
        self.assertIn("Path=/admin", cookie.replace("path=", "Path="))

    def test_logout_kills_the_header_credential_immediately(self):
        """⭐ 头令牌跳过 CSRF，所以它的失效必须可靠 —— 这是那个取舍的兜底。"""
        tok = self.client.post("/admin/api/login",
                               json={"key": ADMIN}).json()["token"]
        h = {"X-Admin-Token": tok}
        self.assertEqual(self.client.get("/admin/api/me", headers=h).status_code, 200)
        self.assertEqual(self.client.post("/admin/api/logout", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/admin/api/me", headers=h).status_code, 401,
                         "注销后头令牌仍然有效 —— 被偷走就一直到 12 小时到期")


if __name__ == "__main__":
    unittest.main()
