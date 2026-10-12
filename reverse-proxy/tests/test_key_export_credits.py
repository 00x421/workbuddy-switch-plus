"""密钥导出（cc-switch / ZCode 配置片段）与积分只读快照。

导出这一块最容易出错的地方不是格式，而是**base url 的口径**：

  · claude 要根地址（SDK 自己拼 `/v1/messages`），填成 `…/v1` 会得到
    `…/v1/v1/messages`，报 **405** 而不是 404 —— 看起来像"方法不对"，
    很容易把人引到错误方向去查；
  · codex / ZCode 要带 `/v1`（它们在 base url 后面拼 `/responses`）。

另一处是本项目与 manager 的**架构差异**：manager 的网关用 `cn:` / `global:`
前缀表达版本归属，我们的 `converter.py` 完全没有这层，`/v1/models` 返回裸名。
所以导出必须剥掉前缀而不是加上——加了会被原样转发给上游，换来 400。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import pytest

import admin.server as admin_server
from admin import keyexport

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


# --------------------------------------------------------------- 模型 id 口径

@pytest.mark.parametrize("raw,expected", [
    ("glm-5.2", "glm-5.2"),
    ("cn:glm-5.2", "glm-5.2"),
    ("global:glm-5.2", "glm-5.2"),
    ("  cn:deepseek-v4.1-flash  ", "deepseek-v4.1-flash"),
])
def test_model_id_strips_manager_prefix(raw, expected):
    """manager 的 cn:/global: 前缀在我们这儿没有对应实现，必须剥掉。"""
    assert keyexport.gateway_model_id(raw) == expected


def test_model_id_rejects_blank():
    with pytest.raises(ValueError):
        keyexport.gateway_model_id("   ")


# --------------------------------------------------------------- base url 口径

@pytest.mark.parametrize("raw,expected", [
    ("http://127.0.0.1:8787", "http://127.0.0.1:8787/v1"),
    ("http://127.0.0.1:8787/", "http://127.0.0.1:8787/v1"),
    ("http://127.0.0.1:8787/v1", "http://127.0.0.1:8787/v1"),
])
def test_gateway_base_url_appends_v1_once(raw, expected):
    assert keyexport.gateway_base_url(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("http://127.0.0.1:8787", "http://127.0.0.1:8787/"),
    ("http://127.0.0.1:8787/v1", "http://127.0.0.1:8787/"),
    # 尾斜杠必须保留：SDK 直接做字符串拼接，没有它会得到 http://hostv1/messages
    ("http://h/v1/", "http://h/"),
])
def test_anthropic_base_url_is_root_with_trailing_slash(raw, expected):
    assert keyexport.anthropic_base_url(raw) == expected


def test_base_url_rejects_blank():
    with pytest.raises(ValueError):
        keyexport.gateway_base_url("")
    with pytest.raises(ValueError):
        keyexport.anthropic_base_url("  ")


# --------------------------------------------------------------- cc-switch

def test_ccswitch_claude_uses_root_base_url():
    out = keyexport.to_ccswitch(token="sk-wb-token", base_url="http://127.0.0.1:8787",
                                app="claude", name="WB", models=["glm-5.2"])
    env = out["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-wb-token"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787/"
    # 三个档位都指向同一模型：留空会让 cc-switch 回退到官方模型名
    for field in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        assert env[field] == "glm-5.2"


def test_ccswitch_codex_base_url_has_v1_and_toml_escapes():
    out = keyexport.to_ccswitch(token="sk-wb-token", base_url="http://h:8787",
                                app="codex", name='有"引号"的名',
                                models=['weird"model'])
    assert out["auth"]["OPENAI_API_KEY"] == "sk-wb-token"
    config = out["config"]
    assert 'base_url = "http://h:8787/v1"' in config
    assert 'wire_api = "responses"' in config
    # 模型名/供应商名里的引号必须转义，否则 TOML 直接解析失败
    assert 'model = "weird\\"model"' in config
    assert 'name = "有\\"引号\\"的名"' in config


@pytest.mark.parametrize("kwargs", [
    dict(token="", base_url="http://h", app="claude", name="x"),
    dict(token="t", base_url="http://h", app="claude", name=""),
    dict(token="t", base_url="http://h", app="vscode", name="x"),
])
def test_ccswitch_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        keyexport.to_ccswitch(**kwargs)


# --------------------------------------------------------------- ZCode

def test_zcode_fragment_shape():
    out = keyexport.to_zcode(token="sk-wb-t", base_url="http://h:8787", name="WB",
                             provider_id="workbuddy", models=["glm-5.2", "glm-5.2"],
                             context_windows={"glm-5.2": 300000})
    rule = out["providerRule"]
    assert rule["providerId"] == "workbuddy"
    api = rule["config"]["api"]
    assert api["type"] == "openai-responses"
    assert api["baseUrl"] == "http://h:8787/v1"
    # 去重：同一个模型不能出现两次
    assert rule["config"]["personalModelIds"] == ["glm-5.2"]
    assert rule["config"]["modelOrder"] == ["glm-5.2"]
    rules = out["providerModelRules"]
    assert len(rules) == 1
    assert rules[0]["modelId"] == "glm-5.2"
    assert rules[0]["config"]["properties"]["contextWindow"] == 300000


def test_zcode_omits_context_window_when_unknown():
    out = keyexport.to_zcode(token="t", base_url="http://h", name="WB",
                             provider_id="p", models=["m"])
    # 拿不到上下文长度就不写，让客户端用自己的默认值（写 0 会被当成"零上下文"）
    assert out["providerModelRules"][0]["config"]["properties"] == {}


def test_pick_models_falls_back_and_dedupes():
    assert keyexport._pick_models([], "default-m") == ["default-m"]
    assert keyexport._pick_models(["a", "a", "b"], None) == ["a", "b"]
    with pytest.raises(ValueError):
        keyexport._pick_models([], None)


def test_build_requires_plaintext_secret():
    """早期版本的密钥只存哈希，没有明文——这时必须明确报错，不能导出一个空密钥。"""
    with pytest.raises(ValueError, match="明文"):
        keyexport.build(client="ccswitch", key={"name": "x", "secret": "", "models": []},
                        name="x", base_url="http://h")


def test_build_skips_models_not_available_locally():
    result = keyexport.build(client="zcode",
                             key={"name": "k", "secret": "sk-wb-1", "models": ["gone", "kept"]},
                             name="k", base_url="http://h", provider_id="p",
                             known_models=["kept"])
    assert result["models"] == ["kept"]


# --------------------------------------------------------------- HTTP 面

class ExportAndSnapshotHttpTests(unittest.IsolatedAsyncioTestCase):
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

    async def new_key(self, **kwargs):
        body = {"name": "下游A", **kwargs}
        result = await self.client.post("/admin/api/keys", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    async def test_export_requires_login(self):
        # 必须先把密钥建出来（建密钥本身就要登录），再注销，才能确认
        # 401 是导出接口自己给的——否则 401 来自建密钥那一步，这条用例等于没验。
        await self.login()
        created = await self.new_key()
        logged_out = await self.client.post("/admin/api/logout")
        self.assertEqual(logged_out.status_code, 200, logged_out.text)
        self.client.headers.pop("X-CSRF-Token", None)

        result = await self.client.get(f"/admin/api/keys/{created['id']}/export")
        self.assertEqual(result.status_code, 401, result.text)

    async def test_export_ccswitch_claude(self):
        await self.login()
        created = await self.new_key(models=["glm-5.2"])
        result = await self.client.get(f"/admin/api/keys/{created['id']}/export",
                                       params={"client": "ccswitch", "app": "claude",
                                               "base_url": "http://127.0.0.1:8787"})
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["app"], "claude")
        self.assertEqual(body["settings_config"]["env"]["ANTHROPIC_BASE_URL"],
                         "http://127.0.0.1:8787/")
        self.assertEqual(body["settings_config"]["env"]["ANTHROPIC_AUTH_TOKEN"], created["key"])
        self.assertEqual(body["models"], ["glm-5.2"])

    async def test_export_zcode(self):
        await self.login()
        created = await self.new_key(models=["glm-5.2"])
        body = (await self.client.get(f"/admin/api/keys/{created['id']}/export",
                                      params={"client": "zcode"})).json()
        self.assertEqual(body["fragment"]["providerRule"]["providerId"], "workbuddy")
        self.assertEqual(body["fragment"]["providerRule"]["config"]["api"]["baseUrl"].endswith("/v1"), True)

    async def test_export_unknown_client_and_key(self):
        await self.login()
        created = await self.new_key()
        self.assertEqual((await self.client.get(
            f"/admin/api/keys/{created['id']}/export", params={"client": "vscode"})).status_code, 400)
        self.assertEqual((await self.client.get(
            "/admin/api/keys/nope/export")).status_code, 404)

    async def test_export_is_audited(self):
        await self.login()
        created = await self.new_key()
        await self.client.get(f"/admin/api/keys/{created['id']}/export")
        actions = [row.get("action") for row in self.store.data.get("audit_logs", [])]
        self.assertIn("export_key", actions)

    async def test_credits_snapshot_requires_login(self):
        self.assertEqual((await self.client.get(
            "/admin/api/accounts/credits-snapshot")).status_code, 401)

    async def test_credits_snapshot_marks_known_and_totals(self):
        await self.login()
        aid = (await self.client.get("/admin/api/overview")).json()["accounts"][0]["id"]
        # 模拟上一次登记到的积分
        with self.store.lock:
            self.store.data["account_status"].setdefault(aid, {})["remaining"] = 120.5
            self.store.data["account_status"][aid]["capacity"] = 200.0
            self.store.save()

        body = (await self.client.get("/admin/api/accounts/credits-snapshot")).json()
        row = next(r for r in body["rows"] if r["id"] == aid)
        self.assertTrue(row["known"])
        self.assertEqual(row["remaining"], 120.5)
        self.assertEqual(row["capacity"], 200.0)
        self.assertEqual(body["totals"]["known_remaining"], 120.5)
        self.assertEqual(body["known_count"], body["total_count"])


if __name__ == "__main__":
    unittest.main()
