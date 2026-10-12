"""`t14`：「上游读不到」这条状态必须能被界面区分出来。

用户报的现象：上游 workbuddy2api 挂掉之后，账号页面上看不到 deepseek 的限流显示 ——
而「上游挂了」和「上游说没限流」在界面上渲染成**完全一样的空串**，
用户无法判断该等还是该换模型。

这里钉住三件事，缺一条这个缺陷就会回来：
1. 上游读不到时 `/admin/api/overview` **仍是 200**、账号列表照常返回（不得回归）；
2. 响应里读得到失败原因（`pool.wb2api_error` 非空）；
3. `up_cooling` 仍为 **None**（未知）而**不是 False**（无冷却）——
   把它退化成 False 正是这个缺陷的成因，所以单独钉一条。

4. 前端两端（`admin/static/app.js` 与 `src/components/.../accounts-panel.tsx`）
   对这三种输入给出**两两不同**的结果。前端不容易在这层跑，
   所以用「读源码断言关键分支存在」的方式守住一致性 —— 比不测好。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import admin.server as admin_server
from admin import wb2api

ADMIN = "admin-test-credential-long-enough"
API = "client-existing-key"

#: TEST-NET-1（RFC 5737 保留段），保证连不上，且不会误连到任何真实服务。
UNREACHABLE = "http://192.0.2.99:7863"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token",
                     "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


def _status_client(payload):
    """一个只会回固定 /status 的客户端，用来制造「读得到上游」那一档。

    走真实的 `parse_status`，所以视图的形状与生产一致，
    而不是手搓一个 dict —— 后者会和真实解析漂移。
    """
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    raw = httpx.Client(transport=transport).get("http://x/status").json()
    parsed = wb2api.parse_status(raw)
    return lambda *a, **k: parsed


class UpstreamUnreachableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        auth = self.root / "auth"
        auth.mkdir()
        (auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", auth, API, ADMIN)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="https://console.test")
        await self.login()
        await self.set_upstream(UNREACHABLE)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(result.status_code, 200)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    async def set_upstream(self, base: str):
        result = await self.client.patch("/admin/api/pool/settings",
                                        json={"wb2api_base": base, "wb2api_key": ""})
        self.assertEqual(result.status_code, 200, result.text)
        # 换了地址就是换了一个数据源，残留的缓存与错误都必须作废。
        self.expire_upstream_cache()

    def expire_upstream_cache(self):
        """让上游冷却缓存过期，好让下一次 `rows()` 真的去打一次上游。

        `upstream_cooldowns()` 有 5 秒 TTL —— 缓存**本身是对的**
        （面板几秒刷一次，不能每次都跨网络），所以这里不去关它，
        只是把时钟推过 TTL。**不要为了测试把缓存关掉。**
        """
        pool = self.app.state.pool
        cached, error, at = getattr(pool, "_upstream_cache", ({}, None, 0.0))
        pool._upstream_cache = (cached, error, at - 3600)
    async def overview(self):
        result = await self.client.get("/admin/api/overview")
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    # ── 验收 1：不得回归 ────────────────────────────────────────────
    async def test_overview_still_200_with_full_account_list(self):
        """上游读不到**不该**让账号列表刷不出来 —— 这是原设计，不许被这次改动碰坏。"""
        body = await self.overview()
        self.assertEqual(len(body["accounts"]), 1)
        self.assertIn("uid", body["accounts"][0])
        self.assertIn("pool_state", body["accounts"][0])

    # ── 验收 2：错误读得到 ──────────────────────────────────────────
    async def test_error_is_reachable_in_the_response(self):
        body = await self.overview()
        err = body["pool"]["wb2api_error"]
        self.assertIsInstance(err, str)
        self.assertTrue(err.strip(), "上游读不到，wb2api_error 却是空的 —— 界面无从知道")

    async def test_error_is_empty_when_upstream_is_not_configured(self):
        """没配上游不是故障 —— 那种情况下不该在界面上报警。

        这条同时钉住一个容易漏的点：把地址清空之后，
        残留的错误必须一起清掉，否则会照着已经不存在的配置报警。
        """
        await self.overview()                      # 先制造一次失败
        await self.set_upstream("")                # 再把上游关掉
        body = await self.overview()
        self.assertEqual(body["pool"]["wb2api_error"], "")

    async def test_error_clears_after_a_successful_read(self):
        """读到了就报「读到了」—— 否则界面会永远挂着一句过期的告警。"""
        await self.overview()
        self.expire_upstream_cache()
        with patch.object(wb2api, "fetch_status",
                          side_effect=_status_client(
                              {"accounts": [{"uid": "one", "cooling": True,
                                             "cool_remaining_sec": 240}]})):
            body = await self.overview()
        self.assertEqual(body["pool"]["wb2api_error"], "")
        self.assertTrue(body["accounts"][0]["up_cooling"])

    # ── 验收 3：未知 ≠ 无冷却 ───────────────────────────────────────
    async def test_up_cooling_stays_none_not_false(self):
        """⚠️ 这条是**缺陷复发的哨兵**。

        `None` = 未知（读不到），`False` = 上游明确说没冷却。
        把前者写成后者，界面就再也分不出「挂了」和「没限流」——
        那正是用户报的那个现象。
        """
        body = await self.overview()
        row = body["accounts"][0]
        self.assertIsNone(row["up_cooling"],
                          "读不到上游时 up_cooling 必须是 None（未知），不能是 False（无冷却）")
        self.assertIsNot(row["up_cooling"], False)
        self.assertIsNone(row["up_cool_remaining"])
        self.assertIsNone(row["up_reason"])
        self.assertEqual(row["up_models"], [])

    async def test_none_reads_differently_from_a_real_false(self):
        """把「读到了、没冷却」也跑一遍：它与「读不到」必须长得不一样。

        只验「读不到时是 None」不够 —— 如果两种情况都给 None，
        那这条测试会绿着而缺陷依旧。必须两种都断言。
        """
        unreachable = await self.overview()
        self.assertTrue(unreachable["pool"]["wb2api_error"])
        self.assertIsNone(unreachable["accounts"][0]["up_cooling"])

        self.expire_upstream_cache()
        with patch.object(wb2api, "fetch_status",
                          side_effect=_status_client(
                              {"accounts": [{"uid": "one", "cooling": False}]})):
            reachable = await self.overview()
        self.assertEqual(reachable["pool"]["wb2api_error"], "")
        self.assertIs(reachable["accounts"][0]["up_cooling"], False)

    # ── 验收 4/5：两端渲染两两可分 ──────────────────────────────────
    def test_both_frontends_distinguish_the_three_states(self):
        """两端都必须把三种输入渲染成**两两不同**的结果。

        前端拿不到浏览器环境来跑，所以这里做的是一条**结构性**断言：
        读源码，确认两端都在 up_cooling 为 null 的分支里读了 wb2api_error，
        并且都产出了一条「未知」文案。

        它不能证明渲染结果好看，但能证明**那个分支存在** ——
        而在此之前，这个分支在两端都是「直接 return 空」。
        """
        root = Path(__file__).resolve().parents[2]
        js = (root / "reverse-proxy" / "admin" / "static" / "app.js").read_text(encoding="utf-8")
        tsx = (root / "src" / "components" / "reverse-proxy" / "accounts-panel.tsx")
        tsx_text = tsx.read_text(encoding="utf-8")

        for label, text in (("app.js", js), ("accounts-panel.tsx", tsx_text)):
            with self.subTest(frontend=label):
                self.assertIn("wb2api_error", text,
                              "%s 没有读 wb2api_error，界面无从知道上游读不到" % label)
                self.assertIn("冷却状态未知", text,
                              "%s 没有渲染「未知」这一档" % label)

        # 两端必须是同一句话：用户在两处看到的信息不能有差异。
        self.assertIn("上游读不到，冷却状态未知", js)
        self.assertIn("上游读不到，冷却状态未知", tsx_text)

    def test_unknown_and_cooling_use_different_colour(self):
        """「未知」和「有冷却」不能同色 —— 前者只是「不知道」，后者是上游说的。"""
        root = Path(__file__).resolve().parents[2]
        tsx = (root / "src" / "components" / "reverse-proxy" / "accounts-panel.tsx")
        text = tsx.read_text(encoding="utf-8")
        self.assertIn("upstreamTone", text)
        self.assertIn("text-muted-foreground", text)


if __name__ == "__main__":
    unittest.main()
