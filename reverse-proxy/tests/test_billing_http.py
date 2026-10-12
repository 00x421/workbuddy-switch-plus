"""充值 + 兑换码的 HTTP 层测试（走真实 ASGI 应用）。

2026-10 用户裁定删除 /portal/ 后，本文件原先的 PortalTest 与两个走
/portal/api/* 的用例已移除（见 _design/t24-portal-removal.md）。
留下的 BillingTest 测的是 **/admin/api/billing/***，那些接口仍然存在。
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402


ADMIN = "wb-verify-admin-key-long-enough-1234"


def build(tmp):
    # 每次建 app 之前都重新对齐 ADMIN_KEY：多个测试文件都在模块级写这个
    # 环境变量，而 build_app 是从 env 里读的 —— 后 import 的会覆盖先 import 的，
    # 于是用本文件那个常量去登录就会被 401 顶回来（单独跑绿、全量跑红）。
    os.environ["ADMIN_KEY"] = ADMIN
    import importlib
    import serve
    importlib.reload(serve)
    return TestClient(serve.build_app(["--data", str(tmp), "--accounts", str(tmp / "acc.json")]))


class BillingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wb-bill-"))
        (cls.tmp / "acc.json").write_text("[]", encoding="utf-8")
        cls.client = build(cls.tmp)
        # 用同一只 client 完成「登录 → 建用户」，否则 cookie 不共享。
        cls.login(cls.client)
        cls.client.post("/admin/api/users",
                        json={"username": "alice", "password": "alicepass123", "role": "viewer"})

    @staticmethod
    def login(client):
        r = client.post("/admin/api/login", json={"key": "wb-verify-admin-key-long-enough-1234"})
        client.headers.update({"X-CSRF-Token": r.json()["csrf"]})
        return r

    def test_recharge_then_balance_is_visible(self):
        # 用增量断言：同类里多个用例共用一个 app，余额是累积的。
        # 断言绝对值会让测试依赖于执行顺序 —— 那种 flaky 早晚会来咬人。
        before = self.client.get("/admin/api/billing/stats").json()["balances"].get("alice", 0)
        r = self.client.post("/admin/api/billing/recharge",
                             json={"username": "alice", "credit": 100})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["credit"], before + 100)
        s = self.client.get("/admin/api/billing/stats").json()
        self.assertEqual(s["balances"]["alice"], before + 100)

    def test_recharge_rejects_bad_input(self):
        for body, why in (({"username": "ghost", "credit": 10}, "用户不存在"),
                          ({"username": "alice", "credit": 0}, "0"),
                          ({"username": "alice", "credit": -5}, "负数"),
                          ({"username": "alice", "credit": "x"}, "非数字"),
                          ({"username": "alice", "credit": 10 ** 9}, "超上限"),
                          ({"credit": 10}, "缺用户")):
            with self.subTest(why=why):
                r = self.client.post("/admin/api/billing/recharge", json=body)
                self.assertGreaterEqual(r.status_code, 400, f"{why} 没被拒")

    def test_admin_billing_endpoints_require_admin(self):
        self.client.post("/admin/api/users", json={"username": "viewer1", "password": "viewerpass123", "role": "viewer"})
        v = TestClient(self.client.app)
        r = v.post("/admin/api/login", json={"username": "viewer1", "password": "viewerpass123"})
        self.assertEqual(r.status_code, 200, r.text)
        v.headers.update({"X-CSRF-Token": r.json()["csrf"]})
        # 只读的人能看统计与流水
        self.assertEqual(v.get("/admin/api/billing/stats").status_code, 200)
        self.assertEqual(v.get("/admin/api/billing/flows").status_code, 200)
        # 但不能充钱、不能发码、不能停码
        self.assertGreaterEqual(v.post("/admin/api/billing/recharge",
                                       json={"username": "alice", "credit": 10}).status_code, 400)
        self.assertGreaterEqual(v.post("/admin/api/billing/codes",
                                       json={"count": 1, "credit": 10}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
