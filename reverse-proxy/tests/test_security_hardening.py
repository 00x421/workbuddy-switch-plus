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

    def make_key(self, name="k"):
        r = self.admin.post("/admin/api/keys", json={"name": name},
                            headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


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


if __name__ == "__main__":
    unittest.main()