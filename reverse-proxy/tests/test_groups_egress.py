"""上游分组与账号出口线路。

这两块的共同点是「配置在管理端，生效在数据面」。所以用例分两层：
  · 配置层：CRUD、校验、归一化；
  · **生效层**：分组真的挡住了组外账号吗？出口线路真的传到了 httpx 的 `proxy=` 吗？
第二层才是重点——配置存对了但没接上，界面看起来一切正常，线上却毫无变化。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

import admin.server as admin_server
from admin import egress, groups, keysvc
from admin.pool import AccountPool, PoolMiddleware
from admin.server import Store
from core import converter
from test_admin_server import credential

ADMIN = "admin-long-enough-test-key"
API = "api-test"


class Fixture(unittest.TestCase):
    """搭一套「store + 两个账号」的最小环境。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "management", root / "auth", API, ADMIN)
        self.a = self.store.save_browser_account(credential("one"), "one")["account_id"]
        self.b = self.store.save_browser_account(credential("two"), "two")["account_id"]
        self.now = 1800000000

    def tearDown(self):
        self.tmp.cleanup()

    def pool(self, **kwargs):
        pool = AccountPool(self.store, clock=lambda: self.now, **kwargs)
        for aid, item in self.store.data["accounts"].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={"X-Domain": "www.codebuddy.cn"})
        return pool


# --------------------------------------------------------------- 分组：配置层

class GroupConfigTests(Fixture):
    def test_create_list_update_delete(self):
        created = groups.create_group(self.store, "自用", description="我自己的号",
                                      accounts=[self.a], routing="least_used")
        gid = created["id"]
        listed = groups.list_groups(self.store)
        self.assertEqual([g["name"] for g in listed], ["自用"])
        self.assertEqual(listed[0]["accounts"], [self.a])
        self.assertEqual(listed[0]["routing"], "least_used")
        self.assertFalse(listed[0]["empty"])

        groups.update_group(self.store, gid, {"name": "自用号", "accounts": [self.a, self.b]})
        row = groups.list_groups(self.store)[0]
        self.assertEqual(row["name"], "自用号")
        self.assertEqual(sorted(row["accounts"]), sorted([self.a, self.b]))

        self.assertEqual(groups.delete_group(self.store, gid), {"removed": 1, "unbound_keys": 0})
        self.assertEqual(groups.list_groups(self.store), [])

    def test_duplicate_and_blank_names_are_rejected(self):
        groups.create_group(self.store, "重名")
        with self.assertRaises(ValueError):
            groups.create_group(self.store, "重名")
        with self.assertRaises(ValueError):
            groups.create_group(self.store, "   ")
        # 长名字截断而不是报错会让两个不同名字撞成同一个，所以直接拒绝
        with self.assertRaises(ValueError):
            groups.create_group(self.store, "x" * 41)

    def test_unknown_ids_are_dropped_on_create(self):
        created = groups.create_group(self.store, "含脏 id", accounts=[self.a, "不存在的号", self.a])
        row = next(g for g in groups.list_groups(self.store) if g["id"] == created["id"])
        self.assertEqual(row["accounts"], [self.a])

    def test_hidden_account_keeps_membership_across_save(self):
        """回归：本产品的「删除账号」是可逆的隐藏。

        `SwitchStore.hide_account` 把账号从 `data["accounts"]` 视图里摘掉（账号库
        保留，解除隐藏就回来）。这里直接模拟同一件事——把它从视图里删掉——因为
        这两个 store 的差别只在「怎么摘」，摘完之后 `data` 的形状是一样的。
        如果保存分组时按「不在视图里就丢掉」处理，用户暂停一个号再改一下分组名，
        那个号的成员资格就被永久抹掉了，而界面上完全看不出发生了什么。
        """
        gid = groups.create_group(self.store, "两人组", accounts=[self.a, self.b])["id"]
        with self.store.lock:
            del self.store.data["accounts"][self.b]

        groups.update_group(self.store, gid, {"name": "两人组（改名）"})
        row = groups.list_groups(self.store)[0]
        self.assertIn(self.b, row["accounts"], "不在视图里的账号不应因为一次保存就被踢出分组")
        # 但它确实已经不可调度了，界面要能看出这个组「实际只剩一个人」
        self.assertTrue(row["empty"] is False)
        self.assertEqual(row["account_count"], 2)

    def test_forget_account_only_on_hard_delete(self):
        gid = groups.create_group(self.store, "组", accounts=[self.a, self.b])["id"]
        self.assertEqual(groups.forget_account(self.store, self.b), 1)
        self.assertEqual(groups.list_groups(self.store)[0]["accounts"], [self.a])
        self.assertEqual(groups.forget_account(self.store, "从未存在"), 0)
        self.assertIsNotNone(gid)

    def test_routing_normalisation(self):
        self.assertEqual(groups.norm_routing("LEAST_USED"), "least_used")
        self.assertEqual(groups.norm_routing("bogus"), "")
        self.assertEqual(groups.norm_routing(None), "")


# --------------------------------------------------------------- 分组：生效层

class GroupRoutingTests(Fixture):
    def _key(self, **kwargs):
        kid, _ = keysvc.create_key(self.store, "下游", **kwargs)
        return {"id": kid, **self.store.data["keys"][kid]}

    def test_key_only_lands_inside_its_group(self):
        gid = groups.create_group(self.store, "只要甲", accounts=[self.a])["id"]
        key = self._key(groups=[gid])
        pool = self.pool()
        # 轮转 4 次都必须落在甲上：组外账号（乙）一次都不能出现
        self.assertEqual([pool.select(key=key)[0] for _ in range(4)], [self.a] * 4)

    def test_unbound_key_uses_the_whole_pool(self):
        groups.create_group(self.store, "只要甲", accounts=[self.a])
        key = self._key()
        pool = self.pool()
        self.assertEqual([pool.select(key=key)[0] for _ in range(4)], [self.a, self.b, self.a, self.b])

    def test_disabled_group_blocks_instead_of_widening(self):
        """分组停用必须是「挡住」，不能退回「不限制」。

        退回不限制是更糟的错误：密钥本来只该走某一个号，分组一停就偷偷放到
        所有号上——用户以为关掉了，实际流量扩大了。
        """
        gid = groups.create_group(self.store, "专用", accounts=[self.a])["id"]
        groups.update_group(self.store, gid, {"enabled": False})
        key = self._key(groups=[gid])
        pool = self.pool()
        with self.assertRaises(Exception) as ctx:
            pool.select(key=key)
        self.assertIn("分组", str(ctx.exception.detail))

    def test_empty_group_reports_a_specific_reason(self):
        gid = groups.create_group(self.store, "空组", accounts=[])["id"]
        key = self._key(groups=[gid])
        pool = self.pool()
        with self.assertRaises(Exception) as ctx:
            pool.select(key=key)
        self.assertIn("分组", str(ctx.exception.detail))

    def test_group_routing_overrides_global(self):
        """一个分组配了自己的调度方式时，用它而不是全局的；多个分组时跟随全局。"""
        solo = groups.create_group(self.store, "单人", accounts=[self.a], routing="manual")["id"]
        self.store.data["active"] = self.a
        key = self._key(groups=[solo])
        pool = self.pool()
        self.assertEqual(pool.select(key=key)[0], self.a)

        # 两个分组混在一起时，各自想要的调度方式没有合理答案 → 跟随全局轮转，
        # 于是两组的账号都会参与（不校验先后：cursor 是实例级状态，断言顺序会把
        # 用例和被测实现的内部计数器耦合起来）
        both = groups.create_group(self.store, "另一个", accounts=[self.b])["id"]
        key2 = self._key(groups=[solo, both])
        self.store.data["pool"]["routing"] = "round_robin"
        picked = {pool.select(key=key2)[0] for _ in range(4)}
        self.assertEqual(picked, {self.a, self.b})

    def test_resolve_flags(self):
        self.assertEqual(groups.resolve(self.store, None), (None, ""))
        self.assertEqual(groups.resolve(self.store, {"groups": []}), (None, ""))
        allowed, _ = groups.resolve(self.store, {"groups": ["查无此组"]})
        self.assertEqual(allowed, set())


# --------------------------------------------------------------- 出口线路

class EgressTests(Fixture):
    def test_create_and_normalise(self):
        created = egress.create_line(self.store, "香港", "HTTP://User:Pass@Hk.Example.COM:8080/")
        self.assertEqual(created["name"], "香港")
        # scheme 归一小写、尾部斜杠去掉。
        #
        # 但主机名大小写原样保留：URL 的 netloc 里同时装着主机、用户名与口令，
        # 而口令是大小写敏感的（`urlsplit().netloc` 无法只把小写主机名挑出来改）。
        # 宁可不做这个归一化，也不能把用户的口令改了——那会变成「配置看起来对、
        # 但连不上」这种最难查的故障。HTTP 主机名本身大小写不敏感，因此不影响可用性。
        with self.store.lock:
            self.assertEqual(self.store.data["egress"]["lines"][created["id"]]["url"],
                             "http://User:Pass@Hk.Example.COM:8080")

    def test_mask_hides_credentials_and_path(self):
        shown = egress.mask("http://alice:secret@10.0.0.1:8080/relay/tok123")
        self.assertEqual(shown, "http://alice:***@10.0.0.1:8080/…")
        self.assertNotIn("secret", shown)
        self.assertNotIn("tok123", shown)

    def test_list_never_leaks_the_password(self):
        egress.create_line(self.store, "带凭据", "http://alice:secret@10.0.0.1:8080")
        self.assertNotIn("secret", json.dumps(egress.list_lines(self.store)))

    def test_bad_urls_are_rejected(self):
        for bad in ("", "   ", "10.0.0.1:8080", "socks5://10.0.0.1:1080", "http://"):
            with self.assertRaises(ValueError):
                egress.create_line(self.store, "坏线路", bad)
        with self.assertRaises(ValueError):
            egress.create_line(self.store, "端口越界", "http://10.0.0.1:70000")

    def test_duplicate_names_and_limits(self):
        egress.create_line(self.store, "香港", "http://10.0.0.1:8080")
        with self.assertRaises(ValueError):
            egress.create_line(self.store, "香港", "http://10.0.0.2:8080")

    def test_assign_resolve_and_disable(self):
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        egress.assign(self.store, self.a, lid)
        self.assertEqual(egress.proxy_for(self.store, self.a), "http://10.0.0.1:8080")
        # 没绑的账号是直连
        self.assertIsNone(egress.proxy_for(self.store, self.b))
        self.assertIsNone(egress.proxy_for(self.store, None))

        egress.update_line(self.store, lid, {"enabled": False})
        self.assertIsNone(egress.proxy_for(self.store, self.a), "线路停用即回直连")
        # 停用进 `stale` 而不是 `orphans`：这条绑定只是暂时不生效，启用回来就恢复，
        # 不需要用户给账号换线路。
        self.assertIn(self.a, egress.stale(self.store))
        self.assertEqual(egress.orphans(self.store), [])

    def test_disabling_a_line_does_not_raise_the_alarm(self):
        """停用一条线路是受支持的操作，不该让挂在上面的账号集体变成「异常」。

        老实现把「线路不存在」和「线路已停用」混在 `orphans()` 里，于是每次正常
        停用都会报一串假警报——喊狼来了，真出问题时就没人看了。"""
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        egress.assign(self.store, self.a, lid)
        egress.update_line(self.store, lid, {"enabled": False})
        self.assertEqual(egress.orphans(self.store), [], "停用不是悬空引用")
        self.assertEqual(egress.stale(self.store), [self.a])

        # 真正的悬空引用：线路记录被手工抹掉，assign 里还留着指向它的 id
        with self.store.lock:
            del self.store.data["egress"]["lines"][lid]
        self.assertEqual(egress.orphans(self.store), [self.a])
        self.assertEqual(egress.stale(self.store), [])

    def test_delete_line_unbinds_and_falls_back_to_direct(self):
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        egress.assign(self.store, self.a, lid)
        self.assertEqual(egress.delete_line(self.store, lid), 1)
        self.assertIsNone(egress.proxy_for(self.store, self.a), "线路没了应当回落直连，而不是把账号一起弃用")
        self.assertEqual(egress.orphans(self.store), [])

    def test_assign_unknown_targets_raise(self):
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        with self.assertRaises(KeyError):
            egress.assign(self.store, self.a, "查无此线路")
        with self.assertRaises(KeyError):
            egress.assign(self.store, "查无此账号", lid)

    def test_unbind_clears_assignment(self):
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        egress.assign(self.store, self.a, lid)
        egress.unbind_account(self.store, self.a)
        self.assertIsNone(egress.proxy_for(self.store, self.a))


# --------------------------------------------------------------- 出口线路：生效层

class EgressDataPlaneTests(Fixture):
    def test_httpx_kwargs_only_carries_proxy_when_set(self):
        """没有出口时**必须**返回空 dict：显式传 `proxy=None` 会让 HTTP_PROXY 失效。"""
        token = egress.REQUEST_EGRESS.set(None)
        try:
            self.assertEqual(egress.httpx_proxy_kwargs(), {})
        finally:
            egress.REQUEST_EGRESS.reset(token)

    def test_httpx_kwargs_carries_the_proxy(self):
        token = egress.REQUEST_EGRESS.set("http://10.0.0.1:8080")
        try:
            self.assertEqual(egress.httpx_proxy_kwargs(), {"proxy": "http://10.0.0.1:8080"})
            self.assertEqual(converter._proxy_kwargs(), {"proxy": "http://10.0.0.1:8080"})
        finally:
            egress.REQUEST_EGRESS.reset(token)
        self.assertEqual(converter._proxy_kwargs(), {})

    def test_client_for_passes_the_proxy_to_the_account_plane(self):
        seen = []
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        egress.assign(self.store, self.a, lid)

        def factory(**kwargs):
            seen.append(kwargs)
            # 真实建客户端时不带 proxy：这里只验「有没有传下去」，带上了会去连
            # 一个不存在的代理，用例就变成网络测试了。
            return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

        pool = self.pool(client_factory=factory)
        with pool.client_for(self.a):
            pass
        with pool.client_for(self.b):
            pass
        self.assertEqual(seen, [{"proxy": "http://10.0.0.1:8080"}, {}])


class EgressMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """数据面真的是从「选中的账号」推导出口的（而不是从请求头或全局配置）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "management", root / "auth", API, ADMIN)
        self.a = self.store.save_browser_account(credential("one"), "one")["account_id"]
        self.b = self.store.save_browser_account(credential("two"), "two")["account_id"]
        self.pool = AccountPool(self.store, clock=lambda: 1800000000)
        for aid, item in self.store.data["accounts"].items():
            self.store.manager_for(aid, item).get_headers = Mock(
                return_value={"X-Domain": "www.codebuddy.cn"})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _call(self, app):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)),
                base_url="http://test") as client:
            return await client.post("/v1/chat/completions",
                                     json={"model": "m", "messages": []},
                                     headers={"Authorization": "Bearer " + API})

    async def test_middleware_binds_the_selected_accounts_line(self):
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        # 只给乙绑线路：轮转第一跳是甲（直连），第二跳才轮到乙
        egress.assign(self.store, self.b, lid)
        seen = []

        async def app(scope, receive, send):
            seen.append(egress.REQUEST_EGRESS.get())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        await self._call(app)
        await self._call(app)
        self.assertEqual(seen, [None, "http://10.0.0.1:8080"])

    async def test_contextvar_is_reset_after_the_request(self):
        """请求结束后不能把出口漏给下一个请求（ContextVar 是每个请求自己的）。"""
        lid = egress.create_line(self.store, "香港", "http://10.0.0.1:8080")["id"]
        egress.assign(self.store, self.a, lid)

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        await self._call(app)
        self.assertIsNone(egress.REQUEST_EGRESS.get())


# --------------------------------------------------------------- HTTP 面

class GroupsEgressHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        auth = root / "auth"
        auth.mkdir()
        (auth / "session.info").write_text(json.dumps(credential()), encoding="utf-8")
        self.app = admin_server.create_app(root / "management", auth, API, ADMIN)
        self.store = self.app.state.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="https://console.test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(result.status_code, 200, result.text)
        self.client.headers["X-CSRF-Token"] = result.json()["csrf"]

    async def test_group_endpoints_require_login(self):
        for method, path in (("get", "/admin/api/groups"),
                             ("get", "/admin/api/egress"),
                             ("post", "/admin/api/groups")):
            call = getattr(self.client, method)
            response = await (call(path, json={}) if method == "post" else call(path))
            self.assertEqual(response.status_code, 401, f"{method} {path}")

    async def test_group_crud_over_http(self):
        await self.login()
        accounts = (await self.client.get("/admin/api/pool/summary")).json()["accounts"]
        aid = accounts[0]["id"]

        created = await self.client.post("/admin/api/groups",
                                         json={"name": "自用", "accounts": [aid],
                                               "routing": "least_used"})
        self.assertEqual(created.status_code, 200, created.text)
        gid = created.json()["id"]

        body = (await self.client.get("/admin/api/groups")).json()
        self.assertEqual(body["accounts"][0]["id"], aid)
        self.assertEqual(body["groups"][0]["accounts"], [aid])

        bad = await self.client.post("/admin/api/groups", json={"name": "自用"})
        self.assertEqual(bad.status_code, 400, "同名分组必须是 400 而不是 500")

        patched = await self.client.patch(f"/admin/api/groups/{gid}", json={"enabled": False})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertFalse(patched.json()["groups"][0]["enabled"])

        missing = await self.client.patch("/admin/api/groups/nope", json={"enabled": True})
        self.assertEqual(missing.status_code, 404)

    async def test_deleting_a_group_reports_how_many_keys_it_freed(self):
        """删分组会把绑它的密钥从「限定在一组」变成「不限账号」——这是权限放大，
        必须把数字回给界面，不能让用户以为只是整理了一下。"""
        await self.login()
        created = (await self.client.post("/admin/api/groups", json={"name": "组"})).json()
        kids = []
        for name in ("甲", "乙"):
            key = (await self.client.post("/admin/api/keys", json={"name": name})).json()
            kids.append(key["id"])
            await self.client.patch(f"/admin/api/keys/{key['id']}",
                                    json={"groups": [created["id"]]})

        result = (await self.client.delete(f"/admin/api/groups/{created['id']}")).json()
        self.assertEqual(result["unbound_keys"], 2)
        # 密钥清单挂在 overview 里（没有独立的 GET /admin/api/keys）
        keys = (await self.client.get("/admin/api/overview")).json()["keys"]
        for kid in kids:
            view = next(k for k in keys if k["id"] == kid)
            self.assertEqual(view["groups"], [])

    async def test_key_can_be_created_into_a_group_in_one_request(self):
        """`POST /admin/api/keys` 必须接受 `groups`。

        这条用例盯的是一个真实踩过的坑：`add_key` 是把请求体**逐字段**拆开传给
        `keysvc.create_key` 的，而 `edit_key` 是整包透传 `body`。于是「编辑时改分组」
        一直正常，只有「创建时就勾好分组」这条路会静默丢掉绑定——得重新打开列表
        才发现，而且看起来像是界面没生效。"""
        await self.login()
        group = (await self.client.post("/admin/api/groups", json={"name": "绑定组"})).json()
        created = await self.client.post("/admin/api/keys",
                                         json={"name": "一步到位", "groups": [group["id"]]})
        self.assertEqual(created.status_code, 200, created.text)
        kid = created.json()["id"]

        # 创建响应只回 id/key，绑定结果得从 overview 里读回来
        keys = (await self.client.get("/admin/api/overview")).json()["keys"]
        self.assertEqual(next(k for k in keys if k["id"] == kid)["groups"], [group["id"]])

        # 不存在的分组 id 要被丢掉，而不是原样落盘：悬空 id 会让数据面解析时直接 503
        bogus = await self.client.post("/admin/api/keys",
                                       json={"name": "悬空绑定", "groups": ["no-such-group"]})
        keys = (await self.client.get("/admin/api/overview")).json()["keys"]
        self.assertEqual(next(k for k in keys if k["id"] == bogus.json()["id"])["groups"], [])

    async def test_overview_carries_the_group_list(self):
        """8787 网页后台的分组页直接读 `overview.groups`，不再单发一次请求；密钥列表
        也要靠它把分组 id 显示成组名。缺了这个字段，页面会整片空白。"""
        await self.login()
        await self.client.post("/admin/api/groups", json={"name": "看板组"})
        body = (await self.client.get("/admin/api/overview")).json()
        self.assertIn("groups", body)
        self.assertEqual([g["name"] for g in body["groups"]], ["看板组"])
        # 前端渲染直接用这些字段，少一个就会静默变成 undefined（页面显示空白而不是报错）
        self.assertLessEqual({"id", "name", "enabled", "routing", "accounts",
                              "account_names", "account_count", "empty"},
                             set(body["groups"][0]))

    async def test_egress_crud_and_assignment_over_http(self):
        await self.login()
        accounts = (await self.client.get("/admin/api/pool/summary")).json()["accounts"]
        aid = accounts[0]["id"]

        created = await self.client.post("/admin/api/egress/lines",
                                         json={"name": "香港", "url": "http://u:p@10.0.0.1:8080"})
        self.assertEqual(created.status_code, 200, created.text)
        lid = created.json()["id"]
        # 明文口令绝不能出现在响应里
        self.assertNotIn("u:p@", created.text)
        self.assertIn("u:***@10.0.0.1:8080", created.text)

        bad = await self.client.post("/admin/api/egress/lines",
                                     json={"name": "坏", "url": "socks5://10.0.0.1:1080"})
        self.assertEqual(bad.status_code, 400, "不支持的协议必须是 400")

        assigned = await self.client.post("/admin/api/egress/assign",
                                          json={"account_id": aid, "line_id": lid})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        body = (await self.client.get("/admin/api/egress")).json()
        self.assertEqual(next(a for a in body["accounts"] if a["id"] == aid)["line_id"], lid)
        self.assertEqual(body["accounts"][0]["pool_state"] in ("available", "paused", "cooling", "exhausted"), True)

        freed = (await self.client.delete(f"/admin/api/egress/lines/{lid}")).json()
        self.assertEqual(freed["freed"], 1)

        missing = await self.client.post("/admin/api/egress/assign",
                                         json={"account_id": aid, "line_id": "查无此线路"})
        self.assertEqual(missing.status_code, 404)

    async def test_egress_actions_are_audited(self):
        await self.login()
        await self.client.post("/admin/api/egress/lines",
                               json={"name": "香港", "url": "http://10.0.0.1:8080"})
        actions = [row.get("action") for row in self.store.data.get("audit_logs", [])]
        self.assertIn("create_egress_line", actions)


if __name__ == "__main__":
    unittest.main()
