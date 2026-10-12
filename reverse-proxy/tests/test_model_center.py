"""模型中心：把产品配置里的模型条目完整暴露出来。

与 `get_available_models()`（只返回 id 列表）的分工：

| 接口 | 用途 | 字段 |
| --- | --- | --- |
| `/v1/models` | 给下游客户端选模型 | 只有 id |
| `/admin/api/models` | 给运营看「这个模型多大、多贵、谁在用」 | 完整 |

两者**必须用同一套过滤判据**——所以判据抽成了 `converter._is_chat_model_entry`。
早期判据散在循环里，一旦某一边改了、另一边没改，就会出现「客户端能选到、
模型中心里却没有」这种对不上的状态。
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
import pytest

import admin.server as admin_server
from core import converter

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token", "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


# --------------------------------------------------------------- 过滤判据

@pytest.mark.parametrize("entry,expected", [
    ({"id": "glm-5.2", "vendor": "f"}, True),
    ({"id": "x", "vendor": "f", "tags": ["text-to-image"]}, False),
    ({"id": "deepseek-v3", "vendor": "tencent"}, False),
    ({"id": "custom-local:Foo", "vendor": "Custom"}, False),
    ({"id": "vendor-slash", "vendor": "Custom"}, False),
    ({"id": "foo-completion", "vendor": "f"}, False),
    ({"id": "foo-Rewrite", "vendor": "f"}, False),
    ({"vendor": "f"}, False),
    ({}, False),
])
def test_chat_model_filter(entry, expected):
    assert converter._is_chat_model_entry(entry) is expected


@pytest.mark.parametrize("raw,expected", [
    ("x0.16 credits", 0.16),
    ("x0.11", 0.11),
    ("x2.00 credits", 2.0),
    ("", None),
    (None, None),
    ("免费", None),
])
def test_credit_rate_parsing(raw, expected):
    """解析不出来必须是 None（界面显示「—」），不能伪装成 0——0 看起来像免费。"""
    assert converter._credit_rate(raw) == expected


# --------------------------------------------------------------- 目录形状

def test_catalog_falls_back_to_default_models(monkeypatch):
    monkeypatch.setattr(converter, "_load_model_entries_from_workbuddy", lambda: [])
    catalog = converter.get_model_catalog()
    assert [m["id"] for m in catalog] == converter.DEFAULT_MODELS
    assert all(m["tags"] == ["fallback"] for m in catalog)


def test_catalog_normalises_entry_fields():
    entry = {
        "id": "glm-5.3", "name": "GLM-5.3", "vendor": "f",
        "descriptionZh": "中文说明", "credits": "x0.11",
        "contextWindow": {"defaultLength": 300000, "supportedLengths": [300000, 1000000]},
        "maxInputTokens": 1000000, "maxOutputTokens": 128000, "maxAllowedSize": 1000000,
        "supportsToolCall": True, "supportsImages": True,
        "supportsReasoning": True, "onlyReasoning": False,
        "reasoning": {"effort": "high"}, "temperature": 1, "tags": ["vision"],
    }
    out = converter._catalog_entry(entry)
    assert out["id"] == "glm-5.3"
    assert out["context_window"] == 300000
    assert out["context_window_options"] == [300000, 1000000]
    assert out["max_output_tokens"] == 128000
    assert out["credit_rate"] == 0.11
    assert out["reasoning_effort"] == "high"
    assert out["supports_images"] is True
    assert out["description"] == "中文说明"


def test_catalog_entry_tolerates_missing_fields():
    out = converter._catalog_entry({"id": "bare"})
    assert out["id"] == "bare"
    assert out["name"] == "bare"
    assert out["context_window"] is None
    assert out["context_window_options"] == []
    assert out["credit_rate"] is None
    assert out["reasoning_effort"] is None


# --------------------------------------------------------------- HTTP 面

class ModelCenterHttpTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_models_requires_login(self):
        self.assertEqual((await self.client.get("/admin/api/models")).status_code, 401)

    async def test_models_reports_alias_and_key_usage(self):
        await self.login()
        models = (await self.client.get("/admin/api/models")).json()["models"]
        self.assertTrue(models, "模型清单为空")
        target = models[0]["id"]

        await self.client.post("/admin/api/model-aliases", json={"aliases": {"gpt-4o": target}})
        await self.client.post("/admin/api/keys", json={"name": "带白名单", "models": [target]})

        body = (await self.client.get("/admin/api/models")).json()
        row = next(m for m in body["models"] if m["id"] == target)
        self.assertEqual(row["aliases"], ["gpt-4o"])
        self.assertEqual(row["key_count"], 1)
        self.assertEqual(body["aliases"], {"gpt-4o": target})
        self.assertIn("default_model", body)

    async def test_models_source_prefers_dynamic_config(self):
        """不变量：只要存在动态配置（环境变量指定的 / cache 下的），就不该退回装机快照。

        注意环境变量 `ACC_PRODUCT_CONFIG_PATH` 在桌面端进程树里是常态（指向 Temp 下的
        spill 副本），那也算动态配置 —— 所以这里不能写死断言路径必须含 "cache"。
        """
        await self.login()
        body = (await self.client.get("/admin/api/models")).json()
        source = body["source"]
        if not source:
            self.skipTest("本机没有 WorkBuddy 产品配置")
        normalized = source.replace("\\", "/").lower()
        if "app.asar.unpacked" in normalized:
            self.assertIsNone(
                converter._find_cached_product_config(),
                f"本机存在动态配置，却去读了装机快照：{source}")


@pytest.mark.skipif(
    not (Path.home() / ".workbuddy" / "cache").is_dir(),
    reason="本机没有 WorkBuddy 数据目录",
)
def test_real_machine_catalog_has_v41_flash(monkeypatch):
    """真机：WorkBuddy 里选得到的模型，模型中心就必须看得见、且带规格。"""
    monkeypatch.delenv("ACC_PRODUCT_CONFIG_PATH", raising=False)
    monkeypatch.delenv("WORKBUDDY_DATA_DIR", raising=False)

    catalog = {m["id"]: m for m in converter.get_model_catalog()}
    assert "deepseek-v4.1-flash" in catalog
    entry = catalog["deepseek-v4.1-flash"]
    assert entry["max_output_tokens"]
    assert entry["context_window"]
    assert entry["credit_rate"] is not None
    # 模型中心的清单必须与 /v1/models 完全一致（两边判据是同一个）
    assert set(catalog) == set(converter.get_available_models())


if __name__ == "__main__":
    unittest.main()
