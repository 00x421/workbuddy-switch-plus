"""回归：内置后台在明文 HTTP 上的登录必须能用。

原来的 `_login_response` 把 cookie 的 Secure 硬编码成 True。后果不是「更安全」，
而是**登录直接失效**：客户端拒绝把 Secure cookie 发回明文 HTTP，
于是登录返回 200、cookie 也下发了，但后续每个请求都 401 ——
界面表现为「登录成功后立刻弹回登录页」，且没有任何报错可查。

而本服务默认监听 127.0.0.1，明文就是它的主路径，所以这条必须守着。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "wb-verify-admin-key-long-enough-1234"


def build(tmp, name="serve"):
    os.environ["ADMIN_KEY"] = ADMIN
    import importlib
    mod = importlib.import_module(name)
    importlib.reload(mod)
    (tmp / "acc.json").write_text("[]", encoding="utf-8")
    os.environ["ADMIN_KEY"] = ADMIN
    return TestClient(mod.build_app(["--data", str(tmp), "--accounts", str(tmp / "acc.json")]))


class CookieSecureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-cookie-"))
        self.client = build(self.tmp)

    def test_cookie_is_not_secure_on_plain_http(self):
        """明文 HTTP 上不能带 Secure —— 带了就等于登录不了。"""
        r = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(r.status_code, 200, r.text)
        header = r.headers.get("set-cookie", "")
        self.assertIn("workbuddy_admin=", header)
        self.assertNotIn("Secure", header,
                         "明文 HTTP 上带了 Secure，浏览器会拒发这个 cookie，登录会掉线")

    def test_the_session_actually_survives_the_round_trip(self):
        """**真正要守的**是这一条：登录之后下一个请求必须认得我。

        只断言「没带 Secure」是不够的 —— 那样把 cookie 整个删掉也能过。
        """
        r = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(r.status_code, 200)
        s = self.client.get("/admin/api/session")
        self.assertEqual(s.status_code, 200,
                         "登录成功了但会话没带上 —— 界面会表现为「弹回登录页」")

    def test_forwarded_https_still_gets_a_secure_cookie(self):
        """经过 HTTPS 反代时仍要带 Secure，不能为了本机方便把线上降级。"""
        r = self.client.post("/admin/api/login", json={"key": ADMIN},
                             headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Secure", r.headers.get("set-cookie", ""),
                      "HTTPS 部署下 cookie 丢了 Secure")

    def test_env_override_wins(self):
        os.environ["WB_SECURE_COOKIE"] = "1"
        try:
            client = build(self.tmp, name="serve")
            r = client.post("/admin/api/login", json={"key": ADMIN})
            self.assertIn("Secure", r.headers.get("set-cookie", ""),
                          "WB_SECURE_COOKIE=1 没生效")
        finally:
            os.environ.pop("WB_SECURE_COOKIE", None)

    def test_forwarded_http_does_not_get_secure(self):
        """反代终止了 TLS，但回源是 http → 不该带 Secure。"""
        r = self.client.post("/admin/api/login", json={"key": ADMIN},
                             headers={"X-Forwarded-Proto": "http"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Secure", r.headers.get("set-cookie", ""))


if __name__ == "__main__":
    unittest.main()
