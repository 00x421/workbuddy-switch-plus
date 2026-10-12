"""安全加固回归测试（t13 · security）。

这些用例全部是**先看它红、再修成绿**写出来的 —— 每一条都对应一个
在真实 HTTP 上复现过的缺陷，不是「防御性编程」的装饰。

跑法：python -m pytest tests/test_security_hardening.py
绝不用 8787 —— 那是线上服务的端口。本文件只跑进程内 ASGI transport。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "wb-secharden-admin-key-long-enough-12"
PASSWORD = "password-123"


def build(tmp):
    """建一个隔离的 app。

    ⚠️ **必须同时 reload `admin.server`**，不能只 reload `serve`。

    `serve.build_app` 里的路由处理器来自 `admin.server.create_app`，
    而 `importlib.reload(serve)` **不会**重新执行 `admin.server` ——
    它已经在 `sys.modules` 里，reload(serve) 只是重新绑定了 serve 自己的
    模块级名字。后果是：同一进程里改代码后再 build，拿到的仍是**旧**的
    `admin.server`。

    这个坑很隐蔽：整个测试文件在一个进程里跑时，**第一个** build 之前
    `admin.server` 还没被导入，所以是新的 —— 于是真实缺陷能被抓到，
    但**变异测试（改代码看测试会不会红）会假绿**，因为变异根本没生效。
    换句话说，不修这一行，这个文件里的测试**证明不了任何东西**。
    """
    import importlib
    import admin.server as _server_module
    import serve

    os.environ["ADMIN_KEY"] = ADMIN
    importlib.reload(_server_module)
    importlib.reload(serve)
    acc = Path(tmp) / "acc.json"
    acc.write_text("[]", encoding="utf-8")
    return TestClient(serve.build_app(["--data", str(tmp), "--accounts", str(acc)]))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-sech-")
        self.client = build(self.tmp)
        self.admin = self.client
        r = self.admin.post("/admin/api/login", json={"key": ADMIN})
        self.h = {"X-Admin-Token": r.json()["token"]}
        # ⚠️ 两个新默认值都必须在 setUp 里显式打开，否则后面的用例根本走不到：
        #   · `user_system_enabled` 默认 False（新装实例不带用户体系）
        #     → 注册一律 403「管理员尚未开启用户系统」；
        #   · `allow_register`      默认 False（功能开关默认全关）
        #     → 注册一律 403「管理员已关闭注册」。
        # 本文件测的是「开关都打开时，各条安全性质是否成立」，
        # 所以先把它们打开。这不是放水：开关关死时的行为由
        # test_accounts.py 覆盖，这里测的是另一件事。
        # 关掉验证码，走「状态 4 免验证注册」—— 测试不该依赖 SMTP。
        self.admin.put("/admin/api/settings",
                       json={"user_system_enabled": True,
                             "allow_register": True,
                             "require_email_code": False},
                       headers=self.h)

    def register(self, email, password=PASSWORD):
        c = self.client
        r = c.post("/login/api/register", json={"email": email, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return c, r.json()["user"]

    def make_key(self, uid, name="k"):
        r = self.admin.post("/admin/api/keys", json={"name": name, "owner": uid},
                            headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class DevMailCodeDisclosureTest(Base):
    """**HIGH**：dev 出口把验证码回给**任意**匿名请求方 → 可冒名注册他人邮箱。

    原实现无条件 return result：只要进程带 WB_DEV_MAIL=1，
    任何匿名请求都能 POST 一个别人的邮箱、拿到验证码、再用它注册，
    拿到 email_verified=true。

    修复后只对**本机来源**回码，且判据是 TCP 对端而不是 client_ip(req)。
    """

    def test_remote_caller_is_not_treated_as_local(self):
        from admin import server as server_module

        class Req:
            def __init__(self, host):
                self.client = type("C", (), {"host": host})()
                self.headers = {}

        self.assertFalse(server_module._dev_caller(Req("203.0.113.9")),
                         "公网来源被误判为本机 —— 验证码会泄露给任何人")
        self.assertFalse(server_module._dev_caller(Req("10.0.0.5")),
                         "私网来源被误判为本机 —— 同网段的人可以拿到别人的验证码")
        self.assertTrue(server_module._dev_caller(Req("127.0.0.1")))
        self.assertTrue(server_module._dev_caller(Req("::1")))

    def test_forwarded_header_cannot_fake_being_local(self):
        """**核心**：判据必须是 TCP 对端，不能是 client_ip(req)。

        client_ip 在对端落在可信代理网段时会采信 X-Forwarded-For，
        而那是攻击者能伪造的字段。用它判「是不是本机」等于
        「带一个 XFF: 127.0.0.1 就能拿到别人的验证码」。
        """
        from admin import server as server_module

        class Req:
            def __init__(self, host, xff):
                self.client = type("C", (), {"host": host})()
                self.headers = {"x-forwarded-for": xff, "x-real-ip": xff}

        self.assertFalse(
            server_module._dev_caller(Req("203.0.113.9", "127.0.0.1")),
            "伪造 X-Forwarded-For 就能被当成本机 —— 验证码可被远程窃取")
        self.assertFalse(
            server_module._dev_caller(Req("203.0.113.9", "::1")),
            "伪造 X-Forwarded-For: ::1 就能被当成本机")

    def test_loopback_caller_is_still_served(self):
        """本机自测必须还能拿到码，否则 dev 出口就白设了。"""
        from admin import server as server_module

        class Req:
            def __init__(self):
                self.client = type("C", (), {"host": "127.0.0.1"})()
                self.headers = {}

        self.assertTrue(server_module._dev_caller(Req()))


class SmtpReadyReportingTest(Base):
    """**MEDIUM**：对外汇报的 smtp_ready 不能把 dev 出口算作「已配置」。

    原实现对外用 effective_smtp_ready()（含 WB_DEV_MAIL 例外），
    于是进程带 WB_DEV_MAIL=1 且未配 SMTP 时，**匿名**
    /login/api/options 会报 smtp_ready=true、register_locked_reason=""
    —— 等于主动告诉未登录者「这台机器开了开发出口」，而验证码根本没发出去。

    修复后对外报 smtp_configured()（只看配置）；
    放行判断仍走 locked_out_reason()（算 dev 出口，见下一条用例）。
    """

    def tearDown(self):
        os.environ.pop("WB_DEV_MAIL", None)

    def test_anonymous_options_reports_config_not_dev_exit(self):
        os.environ["WB_DEV_MAIL"] = "1"
        from admin import settings_store
        self.assertTrue(settings_store.mailer_dev_mail_enabled())
        opts = self.client.get("/login/api/options").json()
        self.assertFalse(
            opts["smtp_ready"],
            "匿名 options 把 dev 出口报成了 smtp_ready=true —— 泄露了本机配置")

    def test_admin_view_reports_config_and_exposes_dev_flag(self):
        os.environ["WB_DEV_MAIL"] = "1"
        got = self.admin.get("/admin/api/settings", headers=self.h).json()
        self.assertFalse(got["smtp_ready"],
                         "管理端把 dev 出口报成了 smtp_ready=true")
        self.assertTrue(got["dev_mail"],
                        "没把「本机开了开发出口」如实告诉管理员")

    def test_configured_smtp_still_reports_true(self):
        """反向断言：真配了 SMTP 时必须报 true（别把字段改成恒 False）。"""
        self.admin.put("/admin/api/settings",
                       json={"smtp": {"enabled": True, "host": "smtp.example.com",
                                      "port": 465, "use_ssl": True}},
                       headers=self.h)
        got = self.admin.get("/admin/api/settings", headers=self.h).json()
        self.assertTrue(got["smtp_ready"], "配好的 SMTP 被报成未配置")

    def test_locked_out_reason_still_counts_the_dev_exit(self):
        """**反向断言**：locked_out_reason 的判据**不许**跟着改。

        它决定「现在能不能注册」，而 dev 出口绕过的是**投递**不是**校验**。
        若这里也改成只看配置，本机开着 dev 出口时就注册不了了。
        """
        os.environ["WB_DEV_MAIL"] = "1"
        opts = self.client.get("/login/api/options").json()
        self.assertEqual(opts["register_locked_reason"], "",
                         "dev 出口下注册被误挡 —— 本机自测跑不通了")


class ForwardedAllowIpsTest(unittest.TestCase):
    """forwarded_allow_ips 默认不能是「*」（纵深防御）。

    「*」表示信任任何人写的 X-Forwarded-*。uvicorn 的 proxy_headers
    会用 X-Forwarded-For 覆盖 scope["client"]。

    影响面比看起来小（iputil.client_ip() 不读这个开关），
    但默认值仍应是安全的。
    """

    def setUp(self):
        self._saved = os.environ.get("WB_FORWARDED_ALLOW_IPS")
        os.environ.pop("WB_FORWARDED_ALLOW_IPS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("WB_FORWARDED_ALLOW_IPS", None)
        else:
            os.environ["WB_FORWARDED_ALLOW_IPS"] = self._saved

    def test_default_is_loopback_only(self):
        from admin import server as server_module
        self.assertEqual(server_module._forwarded_allow_ips(), "127.0.0.1")

    def test_escape_hatch_is_honoured(self):
        from admin import server as server_module
        os.environ["WB_FORWARDED_ALLOW_IPS"] = "10.0.0.1, 172.16.0.0/12"
        self.assertEqual(server_module._forwarded_allow_ips(),
                         "10.0.0.1,172.16.0.0/12")

    def test_wildcard_is_accepted_but_warned_about(self):
        """通配符仍允许（向后兼容），但必须留下警告。

        理由：旧部署可能已设成「* 」，直接拒绝会让服务起不来。
        「允许但吵」比「静默接受」好。
        """
        import contextlib
        import io
        from admin import server as server_module
        os.environ["WB_FORWARDED_ALLOW_IPS"] = "*"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            value = server_module._forwarded_allow_ips()
        self.assertEqual(value, "*")
        self.assertIn("*", buf.getvalue(), "用了通配符却没打警告")


class TraversalStillBlockedTest(Base):
    """回归：资产路由的目录穿越白名单（我实测 17 个 payload 全 404，钉住它）。"""

    PAYLOADS = [
        "/admin/assets/../admin/server.py",
        "/admin/assets/..%2fserver.py",
        "/admin/assets/%2e%2e%2fserver.py",
        "/admin/assets/..%252fserver.py",
        "/admin/assets/....//server.py",
        "/admin/assets/app.js%00.css",
        "/login/assets/..%2f..%2fadmin%2fserver.py",
        "/login/assets/%2e%2e%2f%2e%2e%2fstate.json",
        "/portal/assets/..%2f..%2fstate.json",
        "/portal/assets/%2e%2e%2f%2e%2e%2f%2e%2e%2fadmin%2fserver.py",
    ]

    def test_no_payload_escapes_the_asset_dir(self):
        anon = self.client
        for p in self.PAYLOADS:
            r = anon.get(p)
            self.assertEqual(r.status_code, 404, f"{p} 返回 {r.status_code}")
            self.assertNotIn("pbkdf2", r.text)
            self.assertNotIn("password_hash", r.text)


class SensitiveFieldExposureTest(Base):
    """扫所有对外响应，确认敏感字段一个都不出现。

    这条比「CORS 开了所以危险」之类的静态推理强：它直接验证**结论**。
    """

    def test_no_sensitive_field_in_any_response(self):
        _, user = self.register("scan@example.com")
        uid = user["id"]
        made = self.make_key(uid, "scan")
        self.admin.post(f"/admin/api/members/{uid}/balance",
                        json={"delta_credit": 12.5}, headers=self.h)

        payloads = []
        for r in (
            self.admin.get("/login/api/me"),
            self.admin.get(f"/admin/api/members/{uid}", headers=self.h),
            self.admin.get("/admin/api/members", headers=self.h),
            self.admin.get("/admin/api/settings", headers=self.h),
            self.admin.get("/login/api/options"),
        ):
            payloads.append(r.text)
        blob = "\n".join(payloads)
        for needle in ("password_hash", "pbkdf2", "password_enc", "enc:v1:",
                       made["key"]):
            self.assertNotIn(needle, blob, f"响应里出现了敏感串：{needle}")


class RedeemLedgerShapeTest(Base):
    """流水字段名钉子：ts / balance（**不是** time / balance_milli）。

    队长实测更正过：契约里写的 time / balance_milli 与真机不符，
    照契约写的 UI 会让「时间」列全显 —、余额列取不到值。
    """

    def test_flow_uses_ts_and_balance(self):
        _, user = self.register("flowshape@example.com")
        uid = user["id"]
        self.admin.post(f"/admin/api/members/{uid}/balance",
                        json={"delta_credit": 12.5, "reason": "测试"}, headers=self.h)
        flows = self.admin.get(f"/admin/api/members/{uid}", headers=self.h).json()["flows"]
        self.assertTrue(flows)
        row = flows[0]
        self.assertIn("ts", row, "流水缺 ts —— 界面「时间」列会全显 —")
        self.assertIn("balance", row, "流水缺 balance —— 余额列取不到值")
        self.assertNotIn("balance_milli", row)
        self.assertEqual(row["milli"], 12500)
        self.assertEqual(row["balance"], 12500)
        self.assertIsInstance(row["ts"], int)


if __name__ == "__main__":
    unittest.main()