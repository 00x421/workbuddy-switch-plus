"""密钥明文展示（小眼睛）的服务端契约。

产品取向：本服务是单机自用，`state.json` 本来就在用户自己磁盘上、账号凭据也在同一份
文件里，所以管理端允许回显明文，便于随时复制到客户端。这组测试把这条取向钉住，
同时确保 `hash` 永远不外泄。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

import admin.server as admin_server
from admin import keysvc

ADMIN_KEY = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


class KeyRevealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", self.auth, API, ADMIN_KEY)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")
        r = await self.client.post("/admin/api/login", json={"key": ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.headers["X-CSRF-Token"] = r.json()["csrf"]

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def create(self, name="小眼睛"):
        r = await self.client.post("/admin/api/keys", json={"name": name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def test_create_response_returns_the_plaintext(self):
        data = await self.create()
        self.assertTrue(data["key"].startswith("sk-wb-"))
        self.assertGreater(len(data["key"]), 30)

    async def test_overview_exposes_the_same_plaintext(self):
        created = await self.create()
        r = await self.client.get("/admin/api/overview")
        self.assertEqual(r.status_code, 200)
        row = next(k for k in r.json()["keys"] if k["id"] == created["id"])
        # 与创建时返回的完全一致——这才是「随时可以再复制一次」的前提。
        self.assertEqual(row["secret"], created["key"])

    async def test_hash_is_never_exposed(self):
        await self.create()
        r = await self.client.get("/admin/api/overview")
        for row in r.json()["keys"]:
            self.assertNotIn("hash", row)
        # 顺带确认哈希确实存了（否则上面的断言可能因为根本没存而通过）。
        with self.app.state.store.lock:
            stored = list(self.app.state.store.data["keys"].values())
        self.assertTrue(all(item.get("hash") for item in stored))

    async def test_secret_is_persisted_to_state_json(self):
        created = await self.create()
        state = json.loads((self.root / "management" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["keys"][created["id"]]["secret"], created["key"])

    async def test_legacy_key_without_secret_still_lists(self):
        """老条目只有 hash/hint：不能因此报错，secret 回显为空串即可。"""
        with self.app.state.store.lock:
            self.app.state.store.data["keys"]["legacy01"] = {
                "name": "旧密钥", "hash": keysvc._hash("sk-wb-legacy"),
                "hint": "sk-wb…gacy", "prefix": "sk-wb-legacy",
                "enabled": True, "expires_at": None, "max_ips": 0, "ip_allowlist": [],
                "models": [], "realm": "", "quota": 0, "used_tokens": 0,
                "quota_credit": 0, "used_credit": 0, "created": 1,
                "last_used_at": None, "ips": [],
            }
            self.app.state.store.save()
        r = await self.client.get("/admin/api/overview")
        self.assertEqual(r.status_code, 200)
        row = next(k for k in r.json()["keys"] if k["id"] == "legacy01")
        self.assertEqual(row["secret"], "")
        self.assertEqual(row["hint"], "sk-wb…gacy")

    def test_public_key_carries_secret_and_strips_hash(self):
        # 直接压 public_key：secret 进视图、hash 必须被剔掉。
        view = keysvc.public_key("kid", {"name": "n", "hash": "deadbeef", "secret": "sk-wb-abc"})
        self.assertEqual(view["secret"], "sk-wb-abc")
        self.assertNotIn("hash", view)

    def test_key_value_from_create_is_usable(self):
        """明文字段不能只是「显示用」——它必须就是能通过校验的那一串。"""
        import hashlib
        token = "sk-wb-" + "x" * 40
        with self.app.state.store.lock:
            kid, returned = keysvc.create_key(self.app.state.store, "校对", value=token)
        item = self.app.state.store.data["keys"][kid]
        self.assertEqual(returned, token)
        self.assertEqual(item["hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(item["secret"], token)


if __name__ == "__main__":
    unittest.main()
