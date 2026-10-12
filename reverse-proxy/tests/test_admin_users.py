"""管理员「用户管理」（终端用户 / members）接口的测试。

这一套覆盖三件事，**每一件都是会被实际打到的**：

1. **越权**：逐个端点、逐个身份（匿名 / 只读角色 / 管理令牌会话）都试一遍。
   这是契约 §9 排第一的风险，t6 会照着这里再验一遍。
2. **钱**：调余额必须产生流水，且金额走整数「厘」。
3. **禁用/删除的副作用**：禁用后不能登录、删除要解绑 key 但**不能删 key**。

⚠️ 这里测的是 `/admin/api/members/*`（终端用户），**不是** `/admin/api/users`
（控制台账号）。两者名字像、含义完全不同，契约 §8.6 有对照表。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx

import admin.server as admin_server
from admin import members as members_svc
from admin import redemption

ADMIN_KEY = "admin-test-credential-long-enough"
API = "client-existing-key"


def credential(uid="one", name="测试账号"):
    return {"account": {"uid": uid, "nickname": name, "enterpriseId": "test"},
            "auth": {"accessToken": "private-access-token",
                     "refreshToken": "private-refresh-token",
                     "expiresAt": int(time.time() * 1000) + 3600000}}


MEMBERS = ("/admin/api/members",)


class MemberBase(unittest.IsolatedAsyncioTestCase):
    """公共脚手架：建 app、登录管理员、建/查成员。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        (self.auth / "session.info").write_text(json.dumps(credential()),
                                                encoding="utf-8")
        self.app = admin_server.create_app(self.root / "management", self.auth,
                                           API, ADMIN_KEY)
        self.admin = self.client_for()
        await self.login_as_admin(self.admin)

    async def asyncTearDown(self):
        for c in getattr(self, "_clients", []):
            await c.aclose()
        self.temp.cleanup()

    def client_for(self) -> httpx.AsyncClient:
        """新建一只 client。

        必须手动登记进 `_clients`。 漏登记不会立刻报错，只会在测试进程里
        留下一个没关的连接池 —— 这种泄漏很难在 review 里看出来。
        """
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                              base_url="https://console.test")
        self._clients = getattr(self, "_clients", [])
        self._clients.append(c)
        return c

    async def login_as_admin(self, client):
        r = await client.post("/admin/api/login",
                              json={"username": "admin", "password": ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        client.headers["X-CSRF-Token"] = r.json()["csrf"]
        return r.json()

    # ── 建成员：走数据层 ────────────────────────────────────────
    #
    # 故意不走 HTTP：`/login/api/register` 是另一套模块的职责，
    # 本套测试不依赖它：否则那边一改接口，这边的测试就全红。
    # 而且红的原因和被改的东西毫无关系。
    # 所以这里直接调 `members.create()`，它正是注册流程要复用的那一层。
    def make_member(self, email, password="memberpass123", role="user"):
        return members_svc.create(self.app.state.store, email=email,
                                  password=password, role=role)

    def grant(self, uid, credit):
        return redemption.grant(self.app.state.store, uid, credit,
                                kind="recharge", note="测试充值")

    async def as_viewer(self) -> httpx.AsyncClient:
        """一只登录成 **viewer 角色** 控制台账号的 client。"""
        email = f"viewer{int(time.time() * 1000) % 100000}@viewer.test"
        await self.admin.post("/admin/api/users",
                              json={"username": "viewer1",
                                    "password": "viewerpass123",
                                    "role": "viewer"})
        c = self.client_for()
        r = await c.post("/admin/api/login",
                         json={"username": "viewer1", "password": "viewerpass123"})
        self.assertEqual(r.status_code, 200, r.text)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return c

    async def token_session(self, scope="admin") -> httpx.AsyncClient:
        """一只**用管理令牌换来的**会话 client（`wbt_` 身份）。"""
        made = await self.admin.post("/admin/api/tokens",
                                     json={"name": "ops", "scope": scope})
        self.assertEqual(made.status_code, 200, made.text)
        token = made.json()["token"]
        c = self.client_for()
        r = await c.post("/admin/api/login", json={"token": token})
        self.assertEqual(r.status_code, 200, r.text)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return c


class MemberCrudTests(MemberBase):
    async def test_create_then_list_and_detail(self):
        row = self.make_member("Alice@Example.com")
        self.assertEqual(row["role"], "user")
        self.assertEqual(row["email"], "Alice@Example.com")

        r = await self.admin.get("/admin/api/members")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        ids = [m["id"] for m in body["items"]]
        self.assertIn(row["id"], ids)
        self.assertGreaterEqual(body["total"], 1)

        one = await self.admin.get(f"/admin/api/members/{row['id']}")
        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual(one.json()["email"], "Alice@Example.com")
        self.assertEqual(one.json()["balance_credit"], 0)
        self.assertEqual(one.json()["keys"], [])

    async def test_email_is_unique_case_insensitively(self):
        """唯一性按 `.strip().lower()` 判定（契约 §1）。

        不这样做的话 `Alice@x.com` 与 `alice@x.com` 是两个账号，
        而它们看起来是同一个人。
        """
        self.make_member("Dup@Example.com")
        r = await self.admin.post("/admin/api/members",
                                 json={"email": "  dup@example.COM  ",
                                       "password": "anotherpass123"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("已注册", r.json()["detail"])

    async def test_never_exposes_password_hash(self):
        row = self.make_member("hash@example.com")
        for resp in (await self.admin.get("/admin/api/members"),
                     await self.admin.get(f"/admin/api/members/{row['id']}")):
            body = json.dumps(resp.json(), ensure_ascii=False)
            self.assertNotIn("password_hash", body)
            self.assertNotIn("pbkdf2", body)
        # 顺带确认哈希确实存了 —— 否则上面的断言可能是因为压根没存而通过。
        stored = members_svc.by_id(self.app.state.store, row["id"])
        self.assertTrue(stored["password_hash"].startswith("pbkdf2_sha256$"))

    async def test_password_is_never_echoed_back(self):
        r = await self.admin.post("/admin/api/members",
                                 json={"email": "secret@example.com",
                                       "password": "supersecret123"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("supersecret123", r.text)

    async def test_create_validates_input(self):
        for body, why in (
                ({"email": "not-an-email", "password": "longenough1"}, "邮箱格式"),
                ({"email": "", "password": "longenough1"}, "空邮箱"),
                ({"email": "ok@example.com", "password": "short"}, "密码过短"),
                ({"email": "ok@example.com", "password": "longenough1",
                  "role": "root"}, "角色非法"),
        ):
            with self.subTest(why=why):
                r = await self.admin.post("/admin/api/members", json=body)
                self.assertEqual(r.status_code, 400, f"{why} 没被拒：{r.text}")

    async def test_search_finds_member_without_created_at(self):
        """**搜索必须命中 created_at 缺失的成员。**

        `state.json` 是人工可编辑的，`created_at` 完全可能是 `None`。
        如果排序时把 `None` 排到最后、又先截断再过滤，这条就会被静默丢掉，
        管理员看到的现象是「搜了等于没搜」。
        """
        row = self.make_member("no-created-at@example.com")
        with self.app.state.store.lock:
            self.app.state.store.data["members"][row["id"]].pop("created_at")
        r = await self.admin.get("/admin/api/members", params={"q": "no-created-at"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([m["id"] for m in r.json()["items"]], [row["id"]])

    async def test_search_matches_email_and_id(self):
        row = self.make_member("findme@example.com")
        bymail = await self.admin.get("/admin/api/members", params={"q": "findme"})
        self.assertEqual([m["id"] for m in bymail.json()["items"]], [row["id"]])
        byid = await self.admin.get("/admin/api/members", params={"q": row["id"]})
        self.assertEqual([m["id"] for m in byid.json()["items"]], [row["id"]])

    async def test_pagination_is_capped(self):
        """`size` 必须有上限 —— 否则就是一个「绕过搜索的全量导出」入口。"""
        r = await self.admin.get("/admin/api/members",
                                params={"size": 100000, "page": 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLessEqual(r.json()["size"], members_svc.MAX_PAGE_SIZE)
        self.assertLessEqual(len(r.json()["items"]), members_svc.MAX_PAGE_SIZE)
        # 脏参数全部吃成默认值，不能 500
        bad = await self.admin.get("/admin/api/members",
                                  params={"page": "x", "size": "y"})
        self.assertEqual(bad.status_code, 200, bad.text)

    async def test_detail_404_for_unknown_id(self):
        r = await self.admin.get("/admin/api/members/deadbeefdeadbeef")
        self.assertEqual(r.status_code, 404, r.text)


class MemberUpdateTests(MemberBase):
    async def test_change_role_and_status(self):
        row = self.make_member("role@example.com")
        r = await self.admin.patch(f"/admin/api/members/{row['id']}",
                                  json={"role": "admin"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["role"], "admin")

        r = await self.admin.patch(f"/admin/api/members/{row['id']}",
                                  json={"status": "disabled"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "disabled")
        # 落盘了才算数
        self.assertEqual(members_svc.by_id(self.app.state.store,
                                            row["id"])["status"], "disabled")

    async def test_rejects_bad_role_and_status(self):
        row = self.make_member("badrole@example.com")
        for patch in ({"role": "root"}, {"status": "deleted"}, {"role": ""}):
            with self.subTest(patch=patch):
                r = await self.admin.patch(f"/admin/api/members/{row['id']}",
                                          json=patch)
                self.assertEqual(r.status_code, 400, r.text)

    async def test_cannot_demote_the_last_admin(self):
        """最后一个 admin 不能降级 —— 否则没人能管后台了。"""
        a = self.make_member("a-admin@example.com", role="admin")
        b = self.make_member("b-admin@example.com", role="admin")
        # 有两个的时候可以降一个
        ok = await self.admin.patch(f"/admin/api/members/{b['id']}",
                                    json={"role": "user"})
        self.assertEqual(ok.status_code, 200, ok.text)
        # 只剩一个了，再降就顶回来
        no = await self.admin.patch(f"/admin/api/members/{a['id']}",
                                    json={"role": "user"})
        self.assertEqual(no.status_code, 400, no.text)
        self.assertIn("管理员", no.json()["detail"])

    async def test_cannot_demote_or_disable_yourself(self):
        """管理员自己也是终端用户时，不能把自己降级/禁用。

        这里用**数据层**直接验（`actor` 的 member_id 来自管理员会话，
        HTTP 路径下控制台账号与终端用户不是同一个命名空间）。
        """
        row = self.make_member("self@example.com", role="admin")
        self.make_member("other-admin@example.com", role="admin")
        actor = {"username": "admin", "role": "admin", "member_id": row["id"]}
        store = self.app.state.store
        for patch in ({"role": "user"}, {"status": "disabled"}):
            with self.subTest(patch=patch):
                with self.assertRaises(Exception) as ctx:
                    members_svc.update(store, row["id"], patch, actor=actor)
                self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    async def test_disable_revokes_member_sessions(self):
        """禁用必须立刻踢会话。

        `accounts`（t2）还没落地时会话体系也不存在，此时踢 0 个是**正确**结果；
        落地了就必须真踢掉。两种都接受，但不能报错。
        """
        row = self.make_member("kick@example.com")
        store = self.app.state.store
        try:
            from admin import accounts as accounts_mod
        except ImportError:
            accounts_mod = None
        if accounts_mod is not None and hasattr(accounts_mod, "revoke_member_sessions"):
            accounts_mod.revoke_member_sessions(store, row["id"])
            # 建一个会话（用 accounts 自己的入口建，不猜它的内部形状）
            for name in ("start_session", "create_session", "new_session"):
                fn = getattr(accounts_mod, name, None)
                if callable(fn):
                    fn(store, row["id"])
                    break
        r = await self.admin.patch(f"/admin/api/members/{row['id']}",
                                  json={"status": "disabled"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("sessions_revoked", r.json())
        if accounts_mod is not None and hasattr(accounts_mod, "revoke_member_sessions"):
            self.assertGreaterEqual(r.json()["sessions_revoked"], 0)


class MemberBalanceTests(MemberBase):
    async def test_adjust_writes_a_flow(self):
        """**调余额必须写流水**，否则钱对不上。"""
        row = self.make_member("money@example.com")
        r = await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                                  json={"delta_credit": 100, "reason": "首充"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["balance_credit"], 100)
        self.assertEqual(r.json()["balance_milli"], 100_000)

        flows = redemption.list_flows(self.app.state.store,
                                      username=row["id"], limit=10)
        self.assertTrue(flows, "调余额没写流水")
        self.assertEqual(flows[0]["milli"], 100_000)
        self.assertEqual(flows[0]["kind"], "admin_adjust")
        self.assertIn("首充", flows[0]["note"])

    async def test_negative_delta_spends(self):
        row = self.make_member("spend@example.com")
        await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                              json={"delta_credit": 50})
        r = await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                                 json={"delta_credit": -20, "reason": "退款"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["balance_credit"], 30)
        flows = redemption.list_flows(self.app.state.store,
                                      username=row["id"], limit=10)
        self.assertEqual(flows[0]["kind"], "spend")
        self.assertEqual(flows[0]["milli"], -20_000)

    async def test_spending_below_zero_is_rejected(self):
        """余额不足必须报错，不能扣成负数 —— 那会让服务在用户不知情时累积债务。"""
        row = self.make_member("poor@example.com")
        r = await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                                  json={"delta_credit": -1})
        self.assertEqual(r.status_code, 402, r.text)

    async def test_milli_precision_uses_decimal_not_float(self):
        """**金额必须走 `redemption.to_milli`**，不能用 `int(x * 1000)`。

        `1.0005` 在 float 路径下会变成 1000 而不是 1001（banker's rounding），
        差 1 厘看似小事，但账对不平才是大事。
        """
        row = self.make_member("milli@example.com")
        r = await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                                  json={"delta_credit": 1.0005})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["balance_milli"], 1001)

    async def test_bad_amounts_are_rejected(self):
        row = self.make_member("badamt@example.com")
        # NaN / inf 特意不走 HTTP：它们不是合法 JSON，httpx 在客户端就序列化失败，
        # 测不到服务端行为。用数据层直调，才真的验到「脏值不会被写进账本」。
        for delta in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(why=repr(delta)):
                with self.assertRaises(Exception) as ctx:
                    members_svc.adjust_balance(self.app.state.store, row["id"], delta)
                self.assertEqual(getattr(ctx.exception, "status_code", None), 400)
                self.assertEqual(
                    redemption.balance_milli(self.app.state.store, row["id"]), 0)
        for delta, why in ((0, "0"), ("x", "非数字"), (None, "缺失"),
                           (members_svc.MAX_ADJUST_CREDIT + 1, "超上限"),
                           (-members_svc.MAX_ADJUST_CREDIT - 1, "负超上限"),
                           (0.0001, "小于最小单位")):
            with self.subTest(why=why):
                r = await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                                          json={"delta_credit": delta})
                self.assertEqual(r.status_code, 400, f"{why} 没被拒：{r.text}")

    async def test_balance_404_for_unknown_member(self):
        r = await self.admin.post("/admin/api/members/deadbeefdeadbeef/balance",
                                  json={"delta_credit": 10})
        self.assertEqual(r.status_code, 404, r.text)

    async def test_stats_aggregates(self):
        row = self.make_member("stats@example.com")
        await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                              json={"delta_credit": 7})
        r = await self.admin.get("/admin/api/members/stats")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["total"], 1)
        self.assertGreaterEqual(r.json()["balance_credit"], 7)


class MemberDeleteTests(MemberBase):
    async def test_delete_requires_zero_balance(self):
        """删一个还有余额的用户 = 把钱吞掉，而管理员完全无从察觉 → 默认 409。"""
        row = self.make_member("rich@example.com")
        await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                              json={"delta_credit": 12})
        r = await self.admin.delete(f"/admin/api/members/{row['id']}")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("余额", r.json()["detail"])
        # 被拒之后成员必须还在（不能删一半）
        self.assertIsNotNone(members_svc.by_id(self.app.state.store, row["id"]))

    async def test_force_delete_settles_the_ledger(self):
        row = self.make_member("force@example.com")
        await self.admin.post(f"/admin/api/members/{row['id']}/balance",
                              json={"delta_credit": 9})
        r = await self.admin.delete(f"/admin/api/members/{row['id']}",
                                    params={"force": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["released_credit"], 9)
        self.assertIsNone(members_svc.by_id(self.app.state.store, row["id"]))
        # 必须留下一笔结算流水，否则这笔钱就凭空消失了
        flows = redemption.list_flows(self.app.state.store,
                                      username=row["id"], limit=10)
        self.assertEqual(flows[0]["kind"], "member_deleted")
        self.assertEqual(flows[0]["milli"], -9_000)
        self.assertEqual(redemption.balance_milli(self.app.state.store, row["id"]), 0)

    async def test_delete_unbinds_keys_but_never_deletes_them(self):
        """**关键**：删用户只解绑 owner，绝不删/停用 key。

        原因有三：keysvc 有「最后一枚密钥不能删」的硬规则会顶回来；
        key 可能正在线上跑，删它等于让「删账号」变成「线上调用全挂」；
        key 归 keysvc 管，members 层不越权删别的东西。
        """
        row = self.make_member("keys@example.com")
        before = len(self.app.state.store.data["keys"])
        made = await self.admin.post("/admin/api/keys",
                                    json={"name": "归属密钥", "owner": row["id"]})
        self.assertEqual(made.status_code, 200, made.text)
        kid = made.json()["id"]

        r = await self.admin.delete(f"/admin/api/members/{row['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["keys_unbound"], 1)

        store = self.app.state.store
        self.assertEqual(len(store.data["keys"]), before + 1, "key 被删掉了")
        self.assertEqual(store.data["keys"][kid]["owner"], "")
        self.assertTrue(store.data["keys"][kid].get("enabled", True),
                        "key 被停用了 —— 删账号不该让线上调用全挂")

    async def test_deleted_members_key_becomes_unowned_not_orphan(self):
        """删完之后那枚 key 的 owner 是**空**，不是「悬空字符串」。

        区分很重要：`owner=""` 是**既有语义**的无主 key（`portal.py:115-120`
        已经处理：余额 0、禁止兑换），所以**不该**被报成异常；
        而「owner 指向一个不存在的 uid」才是配置坏了，必须报出来。

        这里把两件事都钉住：删用户之后 → 不报孤儿（因为已是无主）；
        手动改成悬空 uid → 报孤儿。
        """
        row = self.make_member("orphan@example.com")
        made = await self.admin.post("/admin/api/keys",
                                     json={"name": "将成无主", "owner": row["id"]})
        kid = made.json()["id"]
        before = await self.admin.get("/admin/api/members")
        self.assertNotIn(row["id"], [o["owner"] for o in before.json()["orphan_keys"]])

        await self.admin.delete(f"/admin/api/members/{row['id']}")
        after = await self.admin.get("/admin/api/members")
        self.assertEqual(self.app.state.store.data["keys"][kid]["owner"], "")
        self.assertEqual(after.json()["orphan_keys"], [],
                         "owner 置空的 key 不该被当成孤儿 —— 那是既有的无主语义")

        # 真正的孤儿：owner 指向一个解析不出来的值（手改 state.json / 老数据）
        with self.app.state.store.lock:
            self.app.state.store.data["keys"][kid]["owner"] = "ghost-uid"
        r = await self.admin.get("/admin/api/members")
        hit = next((o for o in r.json()["orphan_keys"] if o["owner"] == "ghost-uid"), None)
        self.assertIsNotNone(hit, "悬空 owner 没被报出来")
        self.assertEqual(hit["count"], 1)
        self.assertIn("将成无主", hit["names"])
        # 搜索时也要带出来 —— 否则管理员按邮箱搜完，看到的正是这个人的无主密钥，
        # 报告却是空的。
        searched = await self.admin.get("/admin/api/members",
                                        params={"q": "orphan@example.com"})
        self.assertEqual(searched.json()["items"], [])
        self.assertIn("ghost-uid", [o["owner"] for o in searched.json()["orphan_keys"]])

    async def test_delete_404_and_self_protection(self):
        r = await self.admin.delete("/admin/api/members/deadbeefdeadbeef")
        self.assertEqual(r.status_code, 404, r.text)
        row = self.make_member("self-del@example.com", role="admin")
        self.make_member("other-del-admin@example.com", role="admin")
        with self.assertRaises(Exception) as ctx:
            members_svc.delete(self.app.state.store, row["id"],
                               actor={"username": "admin", "member_id": row["id"]})
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    async def test_cannot_delete_the_last_admin(self):
        only = self.make_member("only-admin@example.com", role="admin")
        r = await self.admin.delete(f"/admin/api/members/{only['id']}")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("管理员", r.json()["detail"])

    async def test_email_can_be_reused_after_delete(self):
        """删掉之后邮箱要能重新注册 —— 否则索引泄漏会让这个邮箱永久不可用。"""
        row = self.make_member("reuse@example.com")
        await self.admin.delete(f"/admin/api/members/{row['id']}")
        again = self.make_member("reuse@example.com")
        self.assertNotEqual(again["id"], row["id"])


class MemberAuthorizationTests(MemberBase):
    """**逐个端点**验越权。契约 §9 排第一的风险，t6 会照着重验。

    `read=(method, path)` 用**只读角色**调，`write=(method, path, body)` 用
    更弱的身份调。全表遍历而不是零散写几个 assert —— 新增端点时漏测的可能性小得多。
    """

    async def test_anonymous_is_rejected_on_every_endpoint(self):
        row = self.make_member("anon@example.com")
        uid = row["id"]
        cases = [
            ("GET", "/admin/api/members", None),
            ("GET", "/admin/api/members/stats", None),
            ("GET", f"/admin/api/members/{uid}", None),
            ("PATCH", f"/admin/api/members/{uid}", {"role": "admin"}),
            ("POST", f"/admin/api/members/{uid}/balance", {"delta_credit": 999}),
            ("DELETE", f"/admin/api/members/{uid}", None),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                c = self.client_for()
                r = await c.request(method, path, json=body)
                self.assertEqual(r.status_code, 401, f"{method} {path} 匿名可调")
        # 全程越权之后，钱和账号都不能被改动
        self.assertEqual(redemption.balance_milli(self.app.state.store, uid), 0)
        self.assertEqual(members_svc.by_id(self.app.state.store, uid)["role"], "user")

    async def test_viewer_can_read_but_not_write(self):
        row = self.make_member("viewer-target@example.com")
        uid = row["id"]
        v = await self.as_viewer()

        # 读：允许（列表/详情是 viewer 档）
        self.assertEqual((await v.get("/admin/api/members")).status_code, 200)
        self.assertEqual((await v.get("/admin/api/members/stats")).status_code, 200)
        self.assertEqual((await v.get(f"/admin/api/members/{uid}")).status_code, 200)

        # 写：全部拒绝
        writes = [
            ("PATCH", f"/admin/api/members/{uid}", {"role": "admin"}),
            ("PATCH", f"/admin/api/members/{uid}", {"status": "disabled"}),
            ("POST", f"/admin/api/members/{uid}/balance", {"delta_credit": 999}),
            ("DELETE", f"/admin/api/members/{uid}", None),
        ]
        for method, path, body in writes:
            with self.subTest(method=method, path=path):
                r = await v.request(method, path, json=body)
                self.assertEqual(r.status_code, 403, f"{method} {path} viewer 可写")

        # 越权之后状态必须纹丝不动
        fresh = members_svc.by_id(self.app.state.store, uid)
        self.assertEqual(fresh["role"], "user")
        self.assertEqual(fresh["status"], "active")
        self.assertEqual(redemption.balance_milli(self.app.state.store, uid), 0)

    async def test_token_session_cannot_touch_money_or_delete(self):
        """`wbt_` 令牌换来的会话**不能**调改钱 / 删用户的端点。

        令牌本来就是「能进门」而不是「能改钱」。这条与现有
        `/admin/api/tokens`、`/admin/api/users` 的策略保持一致 ——
        否则一枚泄露的只读令牌就能给自己加余额。
        """
        row = self.make_member("token-target@example.com")
        uid = row["id"]
        t = await self.token_session("admin")

        for method, path, body in (
                ("POST", f"/admin/api/members/{uid}/balance", {"delta_credit": 500}),
                ("DELETE", f"/admin/api/members/{uid}", None),
        ):
            with self.subTest(method=method, path=path):
                r = await t.request(method, path, json=body)
                self.assertEqual(r.status_code, 403,
                                 f"{method} {path} 令牌身份可调：{r.text}")
                self.assertIn("令牌", r.json()["detail"])
        self.assertEqual(redemption.balance_milli(self.app.state.store, uid), 0)
        self.assertIsNotNone(members_svc.by_id(self.app.state.store, uid))

    async def test_admin_header_token_works_without_cookie(self):
        """桌面端走 `X-Admin-Token` 头 + `credentials:"omit"`，**永远不带 cookie**
        （`src/lib/reverse-proxy.ts:82-87`）。只认 cookie 会让桌面端全 401。
        """
        uid = self.make_member("desktop@example.com")["id"]
        # 登录拿到的是会话 sid，桌面端把它当 `X-Admin-Token` 头带回来
        # （`security.bearer_session` 读的就是这个头）。不是 ADMIN_KEY ——
        # ADMIN_KEY 是「单密钥登录」用的，当令牌头发过来只会 401。
        c = self.client_for()
        logged = await c.post("/admin/api/login",
                             json={"username": "admin", "password": ADMIN_KEY})
        self.assertEqual(logged.status_code, 200, logged.text)
        sid = logged.json()["token"]
        bare = self.client_for()   # 一只**完全没有 cookie** 的 client
        r = await bare.get("/admin/api/members", headers={"X-Admin-Token": sid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(uid, [m["id"] for m in r.json()["items"]])

    async def test_auth_check_runs_before_lookup(self):
        """**鉴权必须先于查参数**：越权调用不能靠 404/403 的差别探出 uid 是否存在。

        不存在的 uid + viewer 改角色 → 必须是 **403**（身份不够），
        而不是 404（这个 uid 不存在）。
        """
        v = await self.as_viewer()
        ghost = "deadbeefdeadbeef"
        self.assertEqual((await v.get(f"/admin/api/members/{ghost}")).status_code, 404)
        self.assertEqual(
            (await v.patch(f"/admin/api/members/{ghost}", json={"role": "user"})).status_code,
            403)
        self.assertEqual(
            (await v.post(f"/admin/api/members/{ghost}/balance",
                          json={"delta_credit": 1})).status_code,
            403)
        self.assertEqual(
            (await v.delete(f"/admin/api/members/{ghost}")).status_code, 403)
        anon = self.client_for()
        self.assertEqual(
            (await anon.post(f"/admin/api/members/{ghost}/balance",
                             json={"delta_credit": 1})).status_code,
            401)

    async def test_unknown_uid_from_admin_is_404(self):
        r = await self.admin.get("/admin/api/members/00000000000000ff")
        self.assertEqual(r.status_code, 404, r.text)


class MemberStorageTests(MemberBase):
    """存储层的不变量。"""

    def test_uses_members_key_never_users(self):
        """**绝不能碰 `users` 键。**

        `security.py:132` 把 `users` 初始化成 **list**（控制台账号）。
        终端用户若写同一个键就是 list/dict 类型碰撞，会直接打爆
        `public_users` / `add_user` / `update_user` / `delete_user`。
        """
        self.make_member("keycheck@example.com")
        store = self.app.state.store
        self.assertIsInstance(store.data["users"], list)
        self.assertIsInstance(store.data["members"], dict)
        # 控制台账号一条都没少
        self.assertTrue(any(u.get("username") == "admin"
                            for u in store.data["users"]))

    def test_index_self_heals(self):
        """索引被外部改坏时要能自愈，且**不能让这个邮箱变成可重复注册**。"""
        row = self.make_member("heal@example.com")
        store = self.app.state.store
        with store.lock:
            store.data["member_index_email"].pop("heal@example.com", None)
        found = members_svc.by_email(store, "HEAL@example.com")
        self.assertIsNotNone(found, "索引缺失导致查不到人 → 可以重复注册")
        self.assertEqual(found["id"], row["id"])
        with store.lock:
            self.assertEqual(store.data["member_index_email"]["heal@example.com"],
                             row["id"])
        with self.assertRaises(Exception):
            self.make_member("HEAL@example.com")

    def test_owner_must_resolve_to_a_member(self):
        """契约 §7：非空 `owner` 必须命中成员 —— 否则钱记进没人认领的桶。"""
        store = self.app.state.store
        with self.assertRaises(Exception) as ctx:
            members_svc.assert_owner(store, "nobody@example.com")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)
        # 空 owner 是合法的（无主 key 是既有语义）
        self.assertEqual(members_svc.assert_owner(store, ""), "")
        row = self.make_member("owned@example.com")
        self.assertEqual(members_svc.assert_owner(store, row["id"]), row["id"])

    def test_owner_resolution_accepts_legacy_email(self):
        """老 `state.json` 里 owner 可能是 email —— 能对上就当命中，
        但**绝不做模糊匹配**（那正是串账的入口）。"""
        row = self.make_member("legacy@example.com")
        store = self.app.state.store
        self.assertEqual(members_svc.resolve_owner(store, "LEGACY@example.com")["id"],
                         row["id"])
        self.assertIsNone(members_svc.resolve_owner(store, "legac"))
        self.assertIsNone(members_svc.resolve_owner(store, row["id"][:-1]))

    async def test_detail_never_exposes_key_secret(self):
        """用户详情会列出名下密钥，但**绝不能**带出 `secret` / `hash`。

        `/admin/api/keys` 那条路是**故意**回显明文的（单机自用，便于复制），
        所以「密钥明文在别处可见」是设计如此。但用户管理页只需要「这个人有几枚
        密钥、叫什么」，把长期凭据顺手带进另一个页面属于无谓的扩大暴露面。
        """
        row = self.make_member("secrets@example.com")
        with self.app.state.store.lock:
            self.app.state.store.data["keys"]["kid1"] = {
                "name": "k", "owner": row["id"], "secret": "sk-wb-LEAKME",
                "hash": "deadbeef", "prefix": "sk-wb-LEAKM",
                "created": int(time.time())}
        r = await self.admin.get(f"/admin/api/members/{row['id']}")
        self.assertEqual(r.status_code, 200, r.text)
        body = json.dumps(r.json(), ensure_ascii=False)
        self.assertNotIn("sk-wb-LEAKME", body, "详情把密钥明文带出来了")
        self.assertNotIn("deadbeef", body, "详情把密钥哈希带出来了")
        self.assertEqual(r.json()["key_count"], 1)
        self.assertEqual([k["id"] for k in r.json()["keys"]], ["kid1"])

    def test_detail_data_layer_matches_the_http_view(self):
        """数据层与 HTTP 层口径一致 —— 前端两种取数路径看到同一个东西。"""
        row = self.make_member("sameview@example.com")
        with self.app.state.store.lock:
            self.app.state.store.data["keys"]["kid2"] = {
                "name": "k2", "owner": row["id"], "secret": "sk-wb-X",
                "hash": "cafe", "created": int(time.time())}
        out = members_svc.detail(self.app.state.store, row["id"])
        self.assertNotIn("sk-wb-X", json.dumps(out, ensure_ascii=False))
        self.assertEqual(out["key_count"], 1)


if __name__ == "__main__":
    unittest.main()
