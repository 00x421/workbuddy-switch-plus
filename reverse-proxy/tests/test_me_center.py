"""用户中心（/me）的限流与越权防线 —— t25 重建。

历史：这些测试原先叫 test_portal_ratelimit.py，t24 删除 portal 时一起没了。
t25 重建 /me 接口，**必须把这两条保护连同测试一起带回来** ——
它们锁的是「用血换来的」两个真缺陷，不是覆盖率。

⚠️ 断言的是**外部可观测行为**（429 有没有出现），不是内部计数。
理由：内部计数对不对是「实现」，攻击者能不能绕过是「结果」。
测结果才防得住将来有人改实现。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "wb-verify-admin-key-long-enough-1234"


def build(tmp):
    """建一只独立的 TestClient。

    ⚠️ **隔离来自那个临时目录，不来自模块重载。**
    这里**绝不** reload(serve) —— 本仓库有 18 个测试文件在模块级
    import admin.server，重载会让它们持有旧模块对象，表现成
    「单跑绿、全量红」且失败点漂到别的文件（见 test_accounts.build 的长注释）。
    """
    import serve
    os.environ["ADMIN_KEY"] = ADMIN      # 必须在 build_app 之前
    (tmp / "acc.json").write_text("[]", encoding="utf-8")
    return TestClient(serve.build_app(
        ["--data", str(tmp), "--accounts", str(tmp / "acc.json")]))


class MeBase(unittest.TestCase):
    """建好：管理员会话 + 一个已登录的普通用户 + 它的一枚 key。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-me-"))
        self.client = build(self.tmp)
        r = self.client.post("/admin/api/login", json={"key": ADMIN})
        self.admin_h = {"X-CSRF-Token": r.json()["csrf"]}
        # 打开用户体系总闸：默认是 False，不打开则用户侧全部 403。
        self.client.put("/admin/api/settings",
                        json={"user_system_enabled": True, "allow_register": True,
                              "require_email_code": False}, headers=self.admin_h)
        self.open_registration()
        self.user = self.register("me-user@example.com")
        self.uid = self.user["id"]

    def open_registration(self):
        return self.client.put("/admin/api/settings",
                               json={"allow_register": True, "require_email_code": False,
                                     "user_system_enabled": True}, headers=self.admin_h)

    def register(self, email, client=None):
        (client or self.client).post("/login/api/register",
                                     json={"email": email, "password": "Passw0rd-测试"})
        return self.only_member(email)

    def only_member(self, email):
        rows = self.client.get("/admin/api/members", params={"q": email},
                               headers=self.admin_h).json()["items"]
        return [r for r in rows if r["email"] == email][0]

    def make_code(self, credit=1.0):
        r = self.client.post("/admin/api/billing/codes",
                             json={"count": 1, "credit": credit}, headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["codes"][0]

    def make_key(self, uid, name="k"):
        r = self.client.post("/admin/api/keys", json={"name": name, "owner": uid},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class RedeemRateLimitTest(MeBase):
    """⚠️ 这一条锁的是「成功不清零」那个真缺陷。

    原缺陷：兑换成功后 pop(bucket) 把计数清零，攻击者用
    「19 次乱猜 -> 1 次真码」交错提交，**永远停在阈值以下**。
    实测 80 次交错 0 次 429 —— 限流形同不存在。
    """

    def test_success_does_not_reset_the_counter(self):
        key = self.make_key(self.uid, "interleave")["key"]
        headers = {"Authorization": "Bearer " + key}
        # 4 张不同的真码，确保「成功」不是同一张码的重复提交。
        reals = [self.make_code() for _ in range(4)]
        bogus = "WB-%05d-ZZZZZ-ZZZZZ-ZZZZZ"

        statuses = []
        for real in reals:
            for i in range(19):
                statuses.append(self.client.post(
                    "/me/api/redeem", headers=headers,
                    json={"code": bogus % i}).status_code)
            statuses.append(self.client.post(
                "/me/api/redeem", headers=headers,
                json={"code": real}).status_code)

        # 断言的是外部可见的结果：限流必须真的把请求挡下来。
        # 如果「成功就清零」，这里会是 0 次 429（80 次请求全部被放行）。
        self.assertIn(
            429, statuses,
            "限流被『成功即清零』绕过了：80 次「19 假 + 1 真」交错请求里，"
            "一次 429 都没出现。状态序列尾部=" + str(statuses[-25:]))
        # 而且成功次数必须很少 —— 真码只在计数未满时才可能成功。
        self.assertLessEqual(
            statuses.count(200), 1,
            "窗口内有多张真码兑换成功（" + str(statuses.count(200)) + " 次，应为 ≤1 次）："
            "说明成功之后计数被清零了。")

    def test_failed_attempts_still_rate_limit(self):
        """反向断言：限流器本身必须还在工作（别为了修上面那条把限流删了）。"""
        key = self.make_key(self.uid, "still")["key"]
        headers = {"Authorization": "Bearer " + key}
        bogus = "WB-%05d-ZZZZZ-ZZZZZ-ZZZZZ"
        statuses = [self.client.post("/me/api/redeem", headers=headers,
                                     json={"code": bogus % i}).status_code
                    for i in range(22)]
        self.assertIn(429, statuses, "连续 22 次假码没有触发限流：" + str(statuses))

    def test_a_valid_code_is_blocked_while_the_window_is_hot(self):
        """限流窗口内**正确**的码同样被挡 —— 这是限流的定义，不是缺陷。

        写下来是为了钉住语义：如果有人日后加回「成功了就放行/清零」，
        这条会红。正常用户兑错几次后兑对，5 分钟窗口一过自然恢复。
        """
        key = self.make_key(self.uid, "hot")["key"]
        headers = {"Authorization": "Bearer " + key}
        good = self.make_code()
        bogus = "WB-%05d-ZZZZZ-ZZZZZ-ZZZZZ"
        for i in range(20):
            self.client.post("/me/api/redeem", headers=headers,
                             json={"code": bogus % i})
        r = self.client.post("/me/api/redeem", headers=headers, json={"code": good})
        self.assertEqual(r.status_code, 429, r.text)


class EvictionTest(unittest.TestCase):
    """⚠️ 这一条锁的是「只删过期窗口」那个真缺陷。

    原缺陷：超过 500 条就按 window 排序削掉 200 条，而所有活跃条目的
    window 都等于同一个 now，排序退化成插入序 —— **正在被限流的那一条
    也会被削掉**。攻击者刷 500 个密钥就能把自己的限流计数清零。

    直接测模块级函数：真实服务里 API Key 上限 100 枚、清理阈值 500，
    这条路径端到端根本走不到，走不到的东西就没法用 HTTP 层的测试锁住。
    """

    def test_eviction_never_drops_an_active_counter(self):
        from admin.server import _evict_stale_attempts
        now = 1_000_000
        state = {"active-" + str(i): {"count": 19, "window": now} for i in range(600)}
        removed = _evict_stale_attempts(state, now)
        self.assertEqual(removed, 0, "活跃条目被削掉了 " + str(removed) + " 条")
        self.assertEqual(len(state), 600, "活跃条目数变了：" + str(len(state)))

    def test_eviction_does_remove_expired_counters(self):
        from admin.server import _evict_stale_attempts
        now = 1_000_000
        state = {"old": {"count": 5, "window": now - 400},
                 "fresh": {"count": 5, "window": now}}
        removed = _evict_stale_attempts(state, now)
        self.assertEqual(removed, 1)
        self.assertIn("fresh", state, "没过期的条目被误删")
        self.assertNotIn("old", state, "过期的条目没被删")


class CredentialConflictTest(MeBase):
    """⚠️ t1 BLOCKER-3 越权防线：**别人的 key + 自己的会话 = 必须 403**。

    若「以 key 为准」或「以 cookie 为准」，攻击者就能拿自己那条低权限凭据，
    套上别人那条高权限凭据，读到别人的余额和流水。
    """

    def test_conflicting_key_and_session_is_refused(self):
        other = TestClient(self.client.app)
        self.open_registration()
        other_user = self.register("me-other@example.com", client=other)
        made = self.make_key(other_user["id"], "别人的")
        r = self.client.get("/me/api/overview",
                            headers={"Authorization": "Bearer " + made["key"]})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("凭据冲突", r.json()["detail"])

    def test_conflict_also_blocks_flows_redeem_and_keys(self):
        other = TestClient(self.client.app)
        self.open_registration()
        other_user = self.register("me-other2@example.com", client=other)
        made = self.make_key(other_user["id"], "别人的2")
        h = {"Authorization": "Bearer " + made["key"]}
        self.assertEqual(self.client.get("/me/api/flows", headers=h).status_code, 403)
        self.assertEqual(self.client.get("/me/api/keys", headers=h).status_code, 403)
        self.assertEqual(self.client.post("/me/api/redeem", json={"code": "x"},
                                          headers=h).status_code, 403)

    def test_key_and_session_for_the_same_user_coexist(self):
        """同一个人的 key + cookie：放行（两种凭据指向同一个 uid，没有歧义）。"""
        made = self.make_key(self.uid, "我的密钥")
        r = self.client.get("/me/api/overview",
                            headers={"Authorization": "Bearer " + made["key"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["owner"], self.uid)


class KeyOnlyClientTest(MeBase):
    """老客户端（key 直连、无 cookie）不能被这次改动打断 —— 契约 §7 的意图。"""

    def test_key_only_still_works_without_any_cookie(self):
        made = self.make_key(self.uid, "直连")
        fresh = TestClient(self.client.app)   # 全新客户端，没有任何 cookie
        r = fresh.get("/me/api/overview",
                      headers={"Authorization": "Bearer " + made["key"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["key"]["name"], "直连")

    def test_overview_never_leaks_the_secret(self):
        made = self.make_key(self.uid, "不泄露")
        fresh = TestClient(self.client.app)
        r = fresh.get("/me/api/overview",
                      headers={"Authorization": "Bearer " + made["key"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(made["key"], r.text, "门户回显了密钥明文！")


class SessionAndGateTest(MeBase):
    """会话身份 + 用户体系总闸的边界。"""

    def test_overview_with_session_only(self):
        r = self.client.get("/me/api/overview")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body.get("mode"), "session")
        self.assertIsNone(body.get("key"))
        self.assertEqual(body["user"]["email"], "me-user@example.com")
        self.assertTrue(body["has_owner"])
        self.assertEqual(body["owner"], self.uid)
        for field in ("keys", "daily", "hourly", "totals", "balance_milli"):
            self.assertIn(field, body, "会话模式缺字段：" + field)

    def test_overview_without_any_identity_is_401(self):
        self.client.post("/login/api/logout")
        r = self.client.get("/me/api/overview")
        self.assertEqual(r.status_code, 401, r.text)

    def test_admin_cookie_is_not_a_me_identity(self):
        """控制台 cookie 不能拿来开用户中心 —— 那是两个身份。"""
        fresh = TestClient(self.client.app)
        r = fresh.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(fresh.get("/admin/api/session").status_code, 200)
        self.assertEqual(fresh.get("/me/api/overview").status_code, 401)

    def test_gate_off_blocks_session_but_not_key_direct(self):
        """⭐ 总闸关闭：**会话**身份 403，而 **Bearer 密钥**照常 200。

        这条锁的是「总闸不能变成停机按钮」：密钥直连是核心服务，
        管理员关掉「用户体系」不该让所有人的服务中断。
        """
        made = self.make_key(self.uid, "总闸")
        headers = {"Authorization": "Bearer " + made["key"]}
        # 关总闸
        self.client.put("/admin/api/settings", json={"user_system_enabled": False},
                        headers=self.admin_h)
        # 会话身份 -> 403（读得出原因）
        r = self.client.get("/me/api/overview")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("用户系统", r.json().get("detail", ""), r.text)
        # 密钥直连 -> 仍然 200
        r2 = self.client.get("/me/api/overview", headers=headers)
        self.assertEqual(r2.status_code, 200, "总闸把密钥直连也挡了：" + r2.text)


class AggregationTest(MeBase):
    """余额/流水按 uid 聚合要正确（不是按单枚 key）。"""

    def test_session_sees_its_own_keys_only(self):
        self.make_key(self.uid, "A")
        self.make_key(self.uid, "B")
        body = self.client.get("/me/api/overview").json()
        names = sorted(str(k.get("name")) for k in (body.get("keys") or []))
        self.assertEqual(names, ["A", "B"])

    def test_session_does_not_see_another_users_keys(self):
        other = TestClient(self.client.app)
        self.open_registration()
        other_user = self.register("me-other3@example.com", client=other)
        self.make_key(other_user["id"], "别人的")
        self.make_key(self.uid, "我的")
        body = self.client.get("/me/api/overview").json()
        names = [str(k.get("name")) for k in (body.get("keys") or [])]
        self.assertEqual(names, ["我的"], "看到了别人的密钥：" + str(names))

    def test_balance_comes_from_the_ledger(self):
        from admin import redemption as rd
        rd.grant(self.client.app.state.store if hasattr(self.client.app.state, "store") else None,
                 self.uid, 12345, note="测试充值") if False else None
        # 走正规入账路径：管理员给余额加钱
        r = self.client.post("/admin/api/members/" + self.uid + "/balance",
                             json={"delta_credit": 12.345, "reason": "测试"},
                             headers=self.admin_h)
        self.assertEqual(r.status_code, 200, r.text)
        body = self.client.get("/me/api/overview").json()
        self.assertEqual(body["balance_milli"], 12345)
        self.assertAlmostEqual(body["balance_credit"], 12.345, places=3)

    def test_flows_are_scoped_to_the_user(self):
        self.client.post("/admin/api/members/" + self.uid + "/balance",
                         json={"delta_credit": 5.0, "reason": "测试"},
                         headers=self.admin_h)
        body = self.client.get("/me/api/flows").json()
        self.assertIn("flows", body)
        self.assertTrue(body["flows"], "流水为空 —— 按 uid 聚合没生效")
        row = body["flows"][0]
        # 字段名钉子：用 ts / balance（不是 time / balance_milli）
        self.assertIn("ts", row)
        self.assertIn("balance", row)

    def test_flows_without_identity_is_401(self):
        self.client.post("/login/api/logout")
        self.assertEqual(self.client.get("/me/api/flows").status_code, 401)


class RedeemOwnershipTest(MeBase):
    """无主 key 不能核销 —— 钱要记到有人认领的桶里。"""

    def test_key_without_owner_cannot_redeem(self):
        made = self.client.post("/admin/api/keys", json={"name": "无归属"},
                                headers=self.admin_h).json()
        code = self.make_code()
        fresh = TestClient(self.client.app)
        r = fresh.post("/me/api/redeem",
                       headers={"Authorization": "Bearer " + made["key"]},
                       json={"code": code})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("归属", r.json().get("detail", ""))

    def test_redeem_credits_the_owner_balance(self):
        made = self.make_key(self.uid, "兑换用")
        code = self.make_code(credit=50)
        fresh = TestClient(self.client.app)
        headers = {"Authorization": "Bearer " + made["key"]}
        before = fresh.get("/me/api/overview", headers=headers).json()["balance"]
        r = fresh.post("/me/api/redeem", headers=headers, json={"code": code})
        self.assertEqual(r.status_code, 200, r.text)
        after = fresh.get("/me/api/overview", headers=headers).json()["balance"]
        self.assertAlmostEqual(after - before, 50, places=6)
        # 同一张码再兑一次必须失败
        again = fresh.post("/me/api/redeem", headers=headers, json={"code": code})
        self.assertIn(again.status_code, (400, 409), again.text)


class MePageTest(unittest.TestCase):
    """页面层：路由、骨架三态一致、白名单。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wb-mepage-"))
        self.client = build(self.tmp)

    def test_me_page_is_served(self):
        r = self.client.get("/me")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("content-type", "").split(";")[0], "text/html")

    def test_me_slash_alias(self):
        self.assertIn(self.client.get("/me/").status_code, (200, 404))

    def test_me_assets_are_served(self):
        for f in ("me.js", "me.css"):
            with self.subTest(filename=f):
                self.assertEqual(self.client.get("/me/assets/" + f).status_code, 200)

    def test_me_assets_whitelist(self):
        """白名单必须挡住穿越，也必须挡住**未被允许的真实文件**。"""
        for bad in ("../login.html", "..%2Flogin.html",
                    "login.js", "app.js", "index.html"):
            with self.subTest(filename=bad):
                self.assertEqual(self.client.get("/me/assets/" + bad).status_code, 404)

    def test_me_page_does_not_redirect_when_logged_in(self):
        """⚠️ 服务端**绝不能**在 /me 做 302：老客户端带 key 直连时没有 cookie，
        一律 302 会把它们全部挡在门外（t4 的教训）。"""
        r = self.client.get("/me", follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)

    def test_portal_paths_stay_dead(self):
        """⚠️ t24 的验收基线：/portal 相关路径必须**保持 404**。

        t25 重建用户中心时**没有复活旧路径** —— 这条测试就是那个决定的守卫。
        """
        for path in ("/portal", "/portal/", "/portal/api/overview",
                     "/portal/assets/portal.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404, path)


if __name__ == "__main__":
    unittest.main()