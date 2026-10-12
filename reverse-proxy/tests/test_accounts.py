"""终端用户账号体系（t2）：注册 / 登录 / 会话 / 验证码 / 服务设置。

这一层专门验**安全性质**，不是「接口通不通」：
  · 口令与验证码的明文会不会以任何形式漏出去；
  · 「邮箱不存在」与「密码错误」在**文案 + 状态码 + 耗时**三个维度上是否一致；
  · 禁用了账号，它现有的会话是不是**立刻**失效（而不是等 12 小时 TTL）；
  · SMTP 没配时是否**明确失败**（绝不自动降级成免验证）；
  · 用户会话 cookie 在明文 HTTP 下还能不能用（硬编码 Secure 会让登录直接失效）。

跑法：`python -m pytest tests/test_accounts.py`。**绝不用 8787** —— 那是线上服务的端口。
"""
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "wb-verify-admin-key-long-enough-1234"
PASSWORD = "user-password-123"


def build(tmp, *, admin=ADMIN):
    """建一只独立的 TestClient。

    每个用例都要有**自己的** store 目录 —— 共享会让「注册过的邮箱」泄漏到
    下一个用例，表现为单独跑绿、全量跑红。

    ⚠️ **隔离来自那个临时目录，不来自模块重载。**
    这里以前有一次 `importlib.reload(serve)`，已经删掉，原因有两条：

      1. 它重载的是 `serve`（入口脚本），不是 `admin.server`。
         真正让用例互不干扰的是 `tmp` 这个独立 store —— 见上面那句。
         所以它**从一开始就没在起作用**，删掉不改变任何行为。

      2. 留着它有害：它让人以为「隔离由重载保证」，于是将来有人会去
         「补一个重载 `admin.server`」——那才是真的危险。
         本仓库有 18 个测试文件在**模块级** `import admin.server as admin_server`，
         重载会让它们持有旧模块对象：模块级常量出现两份、
         `patch.object(admin_server, ...)` 打在旧模块上而**被测代码走新模块**，
         表现成「单跑绿、全量红」，且失败点会漂到别的文件去，极难查。

    不要加回来。 要防的是「新增模块级可变状态」，
    那条由 `test_no_module_level_mutable_state` 守着。
    """
    import serve
    os.environ["ADMIN_KEY"] = admin
    accounts_file = tmp / "acc.json"
    accounts_file.write_text("[]", encoding="utf-8")
    return TestClient(serve.build_app(
        ["--data", str(tmp), "--accounts", str(accounts_file)]))


class FakeSMTP:
    """一个只有骨架的 SMTP 服务器：只把「收到的信」记下来。

    用它而不是真的外网发信 —— 本机没有外网，测试也不该依赖第三方。
    实现的是最小 SMTP 会话（EHLO / AUTH / MAIL / RCPT / DATA / QUIT），
    够 `smtplib` 走完，也够我们断言「验证码确实进了邮件正文」。
    """

    def __init__(self, port=0, *, user="", password="", fail_data=False):
        self.user = user
        self.password = password
        self.fail_data = fail_data
        self.messages = []
        self.errors = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)

            def send(text):
                conn.sendall((text + "\r\n").encode())

            send("220 fake ESMTP ready")
            data_mode = False
            body = []
            envelope = {"from": "", "to": []}
            buf = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    text = line.decode("utf-8", "replace")
                    if data_mode:
                        if text == ".":
                            data_mode = False
                            self.messages.append(
                                {"from": envelope["from"],
                                 "to": list(envelope["to"]),
                                 "body": "\n".join(body)})
                            body = []
                            envelope = {"from": "", "to": []}
                            send("250 OK queued")
                        else:
                            body.append(text)
                        continue
                    upper = text.upper()
                    if upper.startswith("EHLO") or upper.startswith("HELO"):
                        conn.sendall(b"250-fake\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
                    elif upper.startswith("AUTH"):
                        # 不在这里判凭据：smtplib 的 AUTH 是两步握手
                        # （`AUTH PLAIN` 一行、base64 凭据再一行），在这里判会
                        # 因为看不到第二行而误判成 535。一律 235，凭据是否真的
                        # 被带上由 mailer 自己的单测覆盖。
                        send("235 ok")
                    elif upper.startswith("MAIL FROM"):
                        envelope["from"] = text
                        send("250 OK")
                    elif upper.startswith("RCPT TO"):
                        envelope["to"].append(text)
                        send("250 OK")
                    elif upper.startswith("DATA"):
                        if self.fail_data:
                            send("451 temp failure")
                            continue
                        data_mode = True
                        send("354 go ahead")
                    elif upper.startswith("QUIT"):
                        send("221 bye")
                        return
                    else:
                        send("250 OK")
        except Exception as exc:
            self.errors.append(repr(exc))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass

    @staticmethod
    def _decode(raw):
        """把 DATA 收到的正文还原成可读文本。

        `mailer` 生成的正文是 **base64** 传输编码的（中文必须如此，否则非 ASCII
        会在某些中继上被改写）。所以要按真收信客户端的方式解码 ——
        直接对 base64 跑正则会什么都找不到。
        """
        import email
        parsed = email.message_from_string(raw)
        chunks = []
        for part in parsed.walk():
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                chunks.append(payload.decode(charset, "replace"))
        return "\n".join(chunks)

    def decoded(self):
        """已经解出来的邮件正文列表（给断言用）。"""
        return [self._decode(m["body"]) for m in self.messages]

    def code(self):
        """从收到的邮件里抠出 6 位验证码。"""
        # 前后不能再有数字：`(?<!\d)` / `(?!\d)` 挡住「有效期 10 分钟」这类
        # 相邻数字串，也挡住更长数字里的片段。
        for text in self.decoded():
            hit = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
            if hit:
                return hit.group(1)
        raise AssertionError(
            "假 SMTP 没收到带 6 位码的邮件（共 %d 封）：%r"
            % (len(self.messages), self.decoded()))


class AccountBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.client = build(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    # ── 小工具 ──────────────────────────────────────────────────
    def admin_client(self, *, patch=None, user_system=True):
        """一只已登录管理员的 client；`patch` 非空时顺手改一次设置。

        ⚠️ **`user_system=True` 默认打开用户系统总闸**（t22 引入）。

        总闸的出厂默认值是 **False**（用户要求「新安装时默认全关」），
        但本文件测的恰恰是**用户系统本身**（注册 / 登录 / 门户会话 / 验证码），
        所以每一处都要先把闸打开 —— 否则测的会变成「总闸挡得住吗」，
        而不是各自原本要测的东西。

        放在这个 helper 里而不是逐个用例加：**它是所有用例的必经之路**，
        一处改完就覆盖全部；散在各处的话，将来新增用例的人一定会漏。

        要专门测「总闸关掉时被拒」，传 `user_system=False`。
        """
        c = self.client
        r = c.post("/admin/api/login", json={"key": ADMIN})
        assert r.status_code == 200, r.text
        c.headers.update({"X-CSRF-Token": r.json()["csrf"]})
        base = {"user_system_enabled": bool(user_system)}
        if patch is not None:
            base.update(patch)
        r = c.put("/admin/api/settings", json=base)
        assert r.status_code == 200, r.text
        return c

    def open_registration(self, **extra):
        patch = {"allow_register": True, "require_email_code": False}
        patch.update(extra)
        return self.admin_client(patch=patch)

    def enable_smtp(self, fake, **extra):
        patch = {
            "allow_register": True,
            "require_email_code": True,
            "smtp": {"enabled": True, "host": "127.0.0.1", "port": fake.port,
                     "use_ssl": False, "username": fake.user,
                     "password": fake.password or "smtp-secret",
                     "from_addr": "noreply@example.com"},
            "site_name": "测试站",
        }
        patch.update(extra)
        r = self.admin_client().put("/admin/api/settings", json=patch)
        assert r.status_code == 200, r.text
        return r.json()

    def register(self, email, password=PASSWORD, code=None, client=None, **kw):
        c = client or self.client
        body = {"email": email, "password": password}
        if code is not None:
            body["code"] = code
        return c.post("/login/api/register", json=body, **kw)

    def login(self, email, password=PASSWORD, client=None, **kw):
        c = client or self.client
        return c.post("/login/api/login",
                      json={"email": email, "password": password}, **kw)

    def send_code(self, email, client=None, **kw):
        c = client or self.client
        return c.post("/login/api/send-code", json={"email": email}, **kw)

    def store(self):
        return self.client.app.state.store

    def members(self):
        with self.store().lock:
            return dict(self.store().data.get("members") or {})

    def only_member(self):
        rows = self.members()
        assert len(rows) == 1, rows
        return next(iter(rows.values()))

    def state_raw(self):
        return (self.tmp / "state.json").read_text(encoding="utf-8")

class StorageKeyTest(AccountBase):
    """**第一要务**：绝不碰 `users` 键。

    `state.json` 的 `users` 是**控制台账号**（list）；终端用户写同一个键就是
    list/dict 类型碰撞，会直接打爆 `security.public_users` / `add_user` /
    `update_user` / `delete_user` 与充值流程。这组用例是那个坑的守门人。
    """

    def test_users_key_stays_a_list(self):
        self.register("a@example.com")   # 随便打一下，确保路径跑过
        self.assertIsInstance(self.store().data.get("users"), list,
                              "`users` 被改成了非 list —— 那是控制台账号的键")

    def test_registration_creates_members_not_users(self):
        self.open_registration()
        r = self.register("a@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.members()), 1)
        self.assertEqual(self.only_member()["email"], "a@example.com")
        usernames = [r.get("username") for r in self.store().data.get("users") or []]
        self.assertEqual(usernames, ["admin"],
                         "控制台账号列表被终端用户注册污染了")

    def test_no_plaintext_password_anywhere_on_disk(self):
        self.open_registration()
        self.register("disk@example.com", password="SuperSecret-42")
        self.assertNotIn("SuperSecret-42", self.state_raw(), "明文口令落盘了")
        self.assertTrue(str(self.only_member()["password_hash"])
                        .startswith("pbkdf2_sha256$"))

    def test_legacy_users_list_is_left_alone(self):
        with self.store().lock:
            self.store().data["users"] = ["legacy-admin"]
        self.open_registration()
        self.register("b@example.com")
        self.assertEqual(self.store().data["users"], ["legacy-admin"]);

    def test_index_is_maintained(self):
        self.open_registration()
        r = self.register("idx@example.com")
        uid = r.json()["user"]["id"]
        with self.store().lock:
            idx = self.store().data.get("member_index_email") or {}
        self.assertEqual(idx.get("idx@example.com"), uid)


class SmtpNotConfiguredTest(AccountBase):
    """**用户明确否定了自动降级**。开着验证码却没配 SMTP → 注册必须失败。

    白话理由：自动降级会让「验证码」这道防线**看起来存在、实际不存在**。
    """

    LOCKED = "未配置 SMTP，当前无法注册 请联系管理员关闭验证码验证 或者给您分发账号登录"

    def setUp(self):
        super().setUp()
        self.admin_client(patch={"allow_register": True, "require_email_code": True,
                                 "smtp": {"enabled": False, "host": ""}})

    def test_options_tells_the_login_page(self):
        opt = self.client.get("/login/api/options").json()
        self.assertTrue(opt["allow_register"])
        self.assertTrue(opt["require_email_code"])
        self.assertFalse(opt["smtp_ready"])
        # 逐字。用户在意的就是这一句。
        self.assertEqual(opt["register_locked_reason"], self.LOCKED)

    def test_registration_is_refused_and_never_falls_back(self):
        r = self.register("nope@example.com")
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.json()["detail"], self.LOCKED)
        self.assertEqual(self.members(), {},
                         "未配置 SMTP 却把账号建出来了 —— 这就是「降级」")

    def test_send_code_is_refused_too(self):
        r = self.send_code("nope@example.com")
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.json()["detail"], self.LOCKED)

    def test_no_code_row_is_written_on_refusal(self):
        self.send_code("nope@example.com")
        with self.store().lock:
            self.assertFalse(self.store().data.get("email_codes") or {},
                             "被拒了还留下验证码记录")

    def test_admin_explicitly_disabling_code_allows_open_registration(self):
        """管理员**人工**关掉 require_email_code 才允许免验证 —— 唯一合法路径。"""
        self.admin_client().put("/admin/api/settings", json={"require_email_code": False})
        r = self.register("open@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.only_member()["email_verified"],
                         "免验证注册不该被标成已验证")

    def test_register_rechecks_at_the_moment_of_registration(self):
        """不能只看 options 的旧值 —— 那个响应可能是几分钟前的。"""
        r = self.register("race@example.com", code="123456")
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("未配置 SMTP", r.json()["detail"])

    def test_half_configured_smtp_is_not_ready(self):
        """`smtp_ready = enabled && host 非空`。只开开关不填主机不算配好。"""
        self.admin_client().put("/admin/api/settings",
                                json={"smtp": {"enabled": True, "host": ""}})
        opt = self.client.get("/login/api/options").json()
        self.assertFalse(opt["smtp_ready"])
        self.assertEqual(opt["register_locked_reason"], self.LOCKED)
        self.assertEqual(self.register("x@example.com").status_code, 503)

    def test_smtp_without_credentials_counts_as_ready(self):
        """内网中继常不需要认证 —— 用户名/密码为空不算「没配好」。"""
        fake = FakeSMTP()
        self.addCleanup(fake.close)
        self.enable_smtp(fake)
        opt = self.client.get("/login/api/options").json()
        self.assertTrue(opt["smtp_ready"])
        self.assertEqual(opt["register_locked_reason"], "")


class RegisterClosedTest(AccountBase):
    def test_register_closed(self):
        self.admin_client(patch={"allow_register": False})
        r = self.register("closed@example.com")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"], "管理员已关闭注册")

    def test_options_reports_closed_registration(self):
        self.admin_client(patch={"allow_register": False})
        opt = self.client.get("/login/api/options").json()
        self.assertFalse(opt["allow_register"])
        self.assertEqual(opt["register_locked_reason"], "管理员已关闭注册")

    def test_send_code_also_closed(self):
        self.admin_client(patch={"allow_register": False})
        r = self.send_code("x@example.com")
        self.assertEqual(r.status_code, 403, r.text)

    def test_login_still_works_when_registration_is_closed(self):
        """关注册不该把老用户关在门外。"""
        self.open_registration()
        self.register("old@example.com")
        self.client.post("/login/api/logout")
        self.admin_client().put("/admin/api/settings", json={"allow_register": False})
        self.assertEqual(self.login("old@example.com").status_code, 200)

class RegisterLoginJourneyTest(AccountBase):
    """注册 → 登录 → 登出 的完整链路（免验证模式）。"""

    def setUp(self):
        super().setUp()
        self.open_registration()

    def test_full_journey(self):
        r = self.register("journey@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["user"]["email"], "journey@example.com")
        self.assertEqual(body["user"]["role"], "user")
        self.assertEqual(body["user"]["balance_credit"], 0)
        # 响应里绝不该出现会话 id 或者口令哈希
        self.assertNotIn("sid", body)
        self.assertNotIn("password_hash", json.dumps(body))
        self.assertIn("workbuddy_user=", r.headers.get("set-cookie", ""))

        me = self.client.get("/login/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["user"]["email"], "journey@example.com")

        out = self.client.post("/login/api/logout")
        self.assertEqual(out.status_code, 200)
        self.assertTrue(out.json()["ok"])
        self.assertEqual(self.client.get("/login/api/me").status_code, 401)

    def test_logout_clears_the_cookie(self):
        self.register("clear@example.com")
        header = self.client.post("/login/api/logout").headers.get("set-cookie", "")
        self.assertIn("workbuddy_user=", header)
        self.assertIn("Path=/", header)

    def test_logout_is_idempotent(self):
        """没登录也能退，退两次也是 200 —— 契约 §4 把这条写死了。"""
        self.assertEqual(self.client.post("/login/api/logout").status_code, 200)
        self.assertEqual(self.client.post("/login/api/logout").status_code, 200)
        self.register("idem@example.com")
        self.assertEqual(self.client.post("/login/api/logout").status_code, 200)
        self.assertEqual(self.client.post("/login/api/logout").status_code, 200)

    def test_logout_with_a_garbage_cookie(self):
        self.client.cookies.set("workbuddy_user", "not-a-real-session", path="/")
        self.assertEqual(self.client.post("/login/api/logout").status_code, 200)

    def test_me_when_logged_out(self):
        r = self.client.get("/login/api/me")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"], "请先登录")

    def test_me_with_a_forged_cookie(self):
        self.client.cookies.set("workbuddy_user", "forged-session-id", path="/")
        self.assertEqual(self.client.get("/login/api/me").status_code, 401)

    def test_admin_cookie_does_not_log_a_user_in(self):
        """控制台 cookie 不是用户 cookie。混用等于给后台账号开了个用户身份。"""
        self.admin_client()
        self.assertEqual(self.client.get("/login/api/me").status_code, 401)

    def test_login_after_logout(self):
        self.register("relogin@example.com")
        self.client.post("/login/api/logout")
        r = self.login("relogin@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        me = self.client.get("/login/api/me").json()
        self.assertEqual(me["user"]["email"], "relogin@example.com")

    def test_email_uniqueness_ignores_case(self):
        self.assertEqual(self.register("Case@Example.com").status_code, 200)
        r = self.register("case@example.com")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "该邮箱已注册，请直接登录")
        self.assertEqual(len(self.members()), 1)

    def test_case_insensitive_login(self):
        self.register("Mixed@Example.com")
        self.client.post("/login/api/logout")
        self.assertEqual(self.login("mixed@EXAMPLE.com").status_code, 200)

    def test_email_is_trimmed(self):
        self.assertEqual(self.register("  pad@example.com  ").status_code, 200)
        self.assertEqual(self.only_member()["email"], "pad@example.com")

    def test_weak_password_refused(self):
        r = self.register("weak@example.com", password="short")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "密码至少 8 位")
        self.assertNotIn("short", json.dumps(r.json()), "报错里回显了用户的口令")
        self.assertEqual(self.members(), {})

    def test_bad_email_refused(self):
        for bad in ("", "not-an-email", "a@b", "a b@c.com", "@x.com"):
            with self.subTest(email=bad):
                r = self.register(bad)
                self.assertEqual(r.status_code, 400, r.text)

    def test_overlong_password_refused(self):
        r = self.register("long@example.com", password="x" * 129)
        self.assertEqual(r.status_code, 400, r.text)

    def test_balance_fields_are_present_and_zero(self):
        self.register("bal@example.com")
        me = self.client.get("/login/api/me").json()["user"]
        self.assertEqual(me["balance_milli"], 0)
        self.assertEqual(me["balance_credit"], 0)
        self.assertEqual(me["api_key_ids"], [])
        self.assertEqual(me["status"], "active")

    def test_balance_comes_from_the_ledger(self):
        """余额**不存**在 members 行里，而是现算 `redemption` 的账本。

        存两份必然漂移，而 `redemption.grant/spend` 不会去更新 members。
        """
        r = self.register("ledger@example.com")
        uid = r.json()["user"]["id"]
        from admin import redemption as rd
        rd.grant(self.store(), uid, 5, note="测试充值")
        me = self.client.get("/login/api/me").json()["user"]
        self.assertEqual(me["balance_milli"], 5000)
        self.assertEqual(me["balance_credit"], 5)
        self.assertNotIn("balance_milli", self.only_member(),
                         "members 行里又存了一份余额 —— 那就是第二个真相源")

    def test_register_response_is_not_cacheable_by_accident(self):
        r = self.register("cache@example.com")
        self.assertNotIn("public", (r.headers.get("cache-control") or "").lower())

class EnumerationTest(AccountBase):
    """防枚举。**三个维度**：文案、状态码、耗时。

    前两个好办（统一文案），第三个最容易漏 —— 邮箱不存在时直接返回会省下一次
    PBKDF2（约 0.07 秒），这个差距足以把「哪些邮箱注册过」枚举出来，
    统一文案完全挡不住。所以这里**连耗时一起断言**。
    """

    def setUp(self):
        super().setUp()
        self.open_registration()
        self.register("known@example.com")
        self.client.post("/login/api/logout")

    def test_same_status_and_message(self):
        missing = self.login("nobody@example.com", password="whatever-pass-1")
        wrong = self.login("known@example.com", password="wrong-password-1")
        self.assertEqual(missing.status_code, wrong.status_code)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["detail"], wrong.json()["detail"])
        self.assertEqual(missing.json()["detail"], "邮箱或密码不正确")

    def test_response_body_does_not_mention_the_email(self):
        r = self.login("ghost@example.com", password="whatever-pass-1")
        self.assertNotIn("ghost@example.com", json.dumps(r.json()))

    def test_timing_is_comparable(self):
        """耗时量级必须一致。阈值给得很宽 —— 断言的是「没有量级差」，
        那是枚举能用的门槛；掐到毫秒级会让这条用例在慢机器上随机红。"""

        def timed(email, password):
            best = None
            for _ in range(3):
                t0 = time.perf_counter()
                self.login(email, password=password)
                dt = time.perf_counter() - t0
                best = dt if best is None else min(best, dt)
            return best

        missing = timed("ghost@example.com", "whatever-pass-1")
        wrong = timed("known@example.com", "wrong-password-1")
        # 不存在的那条不许明显更快（更快 = 省掉了 PBKDF2 = 可枚举）
        self.assertGreater(
            missing, wrong / 8,
            "邮箱不存在时快得多（%.4fs vs %.4fs），可被用来枚举已注册邮箱"
            % (missing, wrong))

    def test_disabled_account_looks_like_wrong_password(self):
        """禁用账号在**登录时**必须和密码错误长得一样 ——
        403「该账号已被禁用」等于告诉攻击者「这个邮箱注册过，而且被停用了」。"""
        uid = self.only_member()["id"]
        with self.store().lock:
            self.store().data["members"][uid]["status"] = "disabled"
        r = self.login("known@example.com")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["detail"], "邮箱或密码不正确")

    def test_bruteforce_lockout(self):
        for i in range(8):
            r = self.login("known@example.com", password="bad-password-%d" % i)
            self.assertEqual(r.status_code, 401, "第 %d 次" % (i + 1))
        r = self.login("known@example.com")
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()["detail"], "失败次数过多，请 10 分钟后再试")
        # 正确口令也进不去 —— 锁的就是这个 (ip, email) 组合
        self.assertEqual(self.login("known@example.com",
                                    password=PASSWORD).status_code, 429)

    def test_lockout_release_reopens_the_door(self):
        for i in range(8):
            self.login("known@example.com", password="bad-%d" % i)
        self.assertEqual(self.login("known@example.com").status_code, 429)
        with self.store().lock:
            for key in list(self.store().data["member_login_fails"]):
                self.store().data["member_login_fails"][key][1] = 0   # 假装 10 分钟已过
        self.assertEqual(self.login("known@example.com").status_code, 200)

    def test_successful_login_clears_the_failure_counter(self):
        for i in range(3):
            self.login("known@example.com", password="bad-%d" % i)
        self.assertEqual(self.login("known@example.com").status_code, 200)
        self.client.post("/login/api/logout")
        for i in range(7):
            r = self.login("known@example.com", password="bad2-%d" % i)
            self.assertEqual(r.status_code, 401, "第 %d 次不该被锁" % (i + 1))

    def test_other_account_is_not_locked_by_someone_elses_failures(self):
        for i in range(8):
            self.login("known@example.com", password="bad-%d" % i)
        self.register("other@example.com")
        self.client.post("/login/api/logout")
        self.assertEqual(self.login("other@example.com").status_code, 200)

    def test_failure_counter_does_not_leak_the_count(self):
        r = self.login("known@example.com", password="bad-password-1")
        body = json.dumps(r.json())   # noqa: F841
        self.assertEqual(set(r.json()), {"detail"})   # 只回 detail，不透露还剩几次;


class SessionTest(AccountBase):
    """会话：cookie 属性、持久化、多设备、吊销。"""

    def setUp(self):
        super().setUp()
        self.open_registration()

    def _reg(self, email="sess@example.com"):
        r = self.register(email)
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_cookie_attributes(self):
        r = self._reg()
        header = r.headers.get("set-cookie", "")
        self.assertIn("workbuddy_user=", header)
        self.assertIn("HttpOnly", header)
        self.assertIn("Path=/", header)
        self.assertIn("samesite=strict", header.lower())

    def test_no_secure_on_plain_http(self):
        """明文 HTTP 上不能带 Secure —— 带了就等于登录不了。
        本项目正是因为这个坑才把 `_cookie_secure` 抽出来的。"""
        r = self._reg()
        self.assertNotIn("Secure", r.headers.get("set-cookie", ""),
                         "明文 HTTP 上带了 Secure，浏览器会拒发这个 cookie")

    def test_secure_behind_https_proxy(self):
        self._reg("tls@example.com")
        c2 = TestClient(self.client.app)
        r = c2.post("/login/api/login",
                    json={"email": "tls@example.com", "password": PASSWORD},
                    headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Secure", r.headers.get("set-cookie", ""),
                      "HTTPS 部署下 cookie 丢了 Secure")

    def test_secure_when_env_forces_it(self):
        self._reg("env@example.com")
        os.environ["WB_SECURE_COOKIE"] = "1"
        try:
            c2 = TestClient(self.client.app)
            r = c2.post("/login/api/login",
                        json={"email": "env@example.com", "password": PASSWORD})
        finally:
            os.environ.pop("WB_SECURE_COOKIE", None)
        self.assertIn("Secure", r.headers.get("set-cookie", ""))

    def test_session_survives_a_store_reload(self):
        """用户会话**落盘**（`member_sessions`），服务重启后仍然有效。

        与管理员会话相反（那个在 `store.sessions`，进程内、重启即失效）——
        契约 §4 明确要求用户会话持久化。
        """
        self._reg()
        with self.store().lock:
            self.assertTrue(self.store().data.get("member_sessions"),
                            "会话没落盘 —— 重启后所有人都被踢下线")
        self.assertIn("member_sessions", self.state_raw())

    def test_session_id_is_never_stored_in_plaintext(self):
        cookie = self._reg().headers["set-cookie"]
        sid = cookie.split("workbuddy_user=")[1].split(";")[0]
        self.assertTrue(sid)
        self.assertNotIn(sid, self.state_raw(), "明文 session id 落盘了")

    def test_multi_device_sessions_are_independent(self):
        self._reg()
        other = TestClient(self.client.app)
        self.assertEqual(self.login("sess@example.com", client=other).status_code, 200)
        # 第一个设备登出，第二个设备不受影响（登录不踢旧会话）
        self.client.post("/login/api/logout")
        self.assertEqual(other.get("/login/api/me").status_code, 200)
        self.assertEqual(self.client.get("/login/api/me").status_code, 401)

    def test_logout_does_not_touch_the_admin_cookie(self):
        """用户侧登出不能把管理员的登录态一起清了 —— 那是两个身份。"""
        c = self.admin_client()
        self.register("both@example.com", client=c)
        self.assertEqual(c.get("/login/api/me").status_code, 200)
        c.post("/login/api/logout")
        self.assertNotEqual(c.get("/admin/api/session").status_code, 401,
                            "用户登出把管理员的登录态也清了")

    def test_deleting_a_member_kills_its_sessions(self):
        r = self._reg("gone@example.com")
        uid = r.json()["user"]["id"]
        self.assertEqual(self.client.get("/login/api/me").status_code, 200)
        self.admin_client().delete("/admin/api/members/" + uid)
        self.assertEqual(self.client.get("/login/api/me").status_code, 401,
                         "成员删了但会话还在")

class DisabledAccountTest(AccountBase):
    """「禁用」必须**立刻**生效，而不是等 12 小时 TTL。"""

    def setUp(self):
        super().setUp()
        self.admin = self.open_registration()
        r = self.register("ban@example.com")
        self.uid = r.json()["user"]["id"]

    def _patch(self, payload):
        return self.admin.patch("/admin/api/members/" + self.uid, json=payload)

    def test_disabled_session_is_rejected_and_dropped(self):
        self.assertEqual(self.client.get("/login/api/me").status_code, 200)
        r = self._patch({"status": "disabled"})
        self.assertIn(r.status_code, (200, 204), r.text)
        r = self.client.get("/login/api/me")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"], "该账号已被禁用")
        # 会话被顺手删掉了（下次请求连 403 都不是，直接 401）
        self.assertEqual(self.client.get("/login/api/me").status_code, 401)

    def test_disabled_account_cannot_login(self):
        self._patch({"status": "disabled"})
        r = self.login("ban@example.com")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.json()["detail"], "邮箱或密码不正确")

    def test_re_enabled_account_can_login_again(self):
        self._patch({"status": "disabled"})
        self._patch({"status": "active"})
        self.assertEqual(self.login("ban@example.com").status_code, 200)

    def test_disabled_account_gets_no_new_session(self):
        self._patch({"status": "disabled"})
        before = len(self.store().data.get("member_sessions") or {})
        self.login("ban@example.com")
        self.assertEqual(len(self.store().data.get("member_sessions") or {}), before)


class PasswordChangeTest(AccountBase):
    def setUp(self):
        super().setUp()
        self.open_registration()
        self.register("pw@example.com")

    def _change(self, old, new, client=None):
        c = client or self.client
        return c.post("/login/api/password", json={"old": old, "new": new})

    def test_password_change_revokes_other_sessions(self):
        other = TestClient(self.client.app)
        self.assertEqual(self.login("pw@example.com", client=other).status_code, 200)
        r = self._change(PASSWORD, "brand-new-pass-9")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["relogin"])
        self.assertEqual(other.get("/login/api/me").status_code, 401,
                         "改密码后别的设备还挂着")
        # 发起改密的那台会被重下发一条新 cookie，所以它自己还能用
        self.assertEqual(self.client.get("/login/api/me").status_code, 200)
        self.assertEqual(self.login("pw@example.com", password="brand-new-pass-9",
                                     client=other).status_code, 200)

    def test_password_change_checks_old_password(self):
        r = self._change("not-the-password", "whatever-12345")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "原密码不正确")
        # 旧口令仍然有效（失败的改密不该把账号弄坏）
        self.assertEqual(self.login("pw@example.com").status_code, 200)

    def test_password_change_rejects_same_password(self):
        r = self._change(PASSWORD, PASSWORD)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "新密码不能与旧密码相同")

    def test_password_change_rejects_weak_new_password(self):
        r = self._change(PASSWORD, "abc")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "密码至少 8 位")

    def test_password_change_requires_login(self):
        self.client.post("/login/api/logout")
        r = self._change(PASSWORD, "whatever-12345")
        self.assertEqual(r.status_code, 401)

    def test_password_hash_is_rehashed_not_reused(self):
        before = self.only_member()["password_hash"]
        self._change(PASSWORD, "brand-new-pass-9")
        after = self.only_member()["password_hash"]
        self.assertNotEqual(before, after)
        self.assertNotIn("brand-new-pass-9", after)
        self.assertTrue(after.startswith("pbkdf2_sha256$"))


class SettingsTest(AccountBase):
    def test_settings_require_admin(self):
        self.assertEqual(self.client.get("/admin/api/settings").status_code, 401)
        self.assertEqual(self.client.put("/admin/api/settings", json={}).status_code, 401)

    def test_defaults_are_reachable(self):
        r = self.admin_client().get("/admin/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("allow_register", body)
        self.assertIn("smtp", body)
        self.assertFalse(body["smtp"]["has_password"])

    def test_password_is_never_echoed(self):
        c = self.admin_client()
        r = c.put("/admin/api/settings", json={"smtp": {"enabled": True,
                                               "host": "smtp.example.com",
                                               "password": "top-secret-smtp"}})
        self.assertEqual(r.status_code, 200, r.text)
        body = json.dumps(r.json())   # noqa: F841
        self.assertNotIn("top-secret-smtp", body, "SMTP 口令被回显了")
        self.assertTrue(r.json()["smtp"]["has_password"])
        self.assertNotIn("top-secret-smtp", self.state_raw(), "SMTP 口令明文落盘了")

    def test_password_survives_a_partial_update(self):
        c = self.admin_client()
        c.put("/admin/api/settings", json={"smtp": {"enabled": True,
                                            "host": "smtp.example.com",
                                            "password": "keep-me-please"}})
        # 只改端口，不传口令 → 口令必须保留（否则「改个端口就把密码清了」）
        r = c.put("/admin/api/settings", json={"smtp": {"port": 2525}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["smtp"]["has_password"])
        self.assertEqual(r.json()["smtp"]["port"], 2525)
        self.assertEqual(r.json()["smtp"]["host"], "smtp.example.com")

    def test_clearing_password_with_empty_string(self):
        c = self.admin_client()
        c.put("/admin/api/settings", json={"smtp": {"enabled": True,
                                            "host": "smtp.example.com",
                                            "password": "to-be-cleared"}})
        r = c.put("/admin/api/settings", json={"smtp": {"password": ""}})
        self.assertFalse(r.json()["smtp"]["has_password"])

    def test_stored_password_is_not_plaintext(self):
        c = self.admin_client()
        c.put("/admin/api/settings", json={"smtp": {"enabled": True,
                                            "host": "smtp.example.com",
                                            "password": "very-secret-credential"}})
        raw = json.loads(self.state_raw())
        blob = json.dumps(raw["service_settings"], ensure_ascii=False)
        self.assertNotIn("very-secret-credential", blob)
        self.assertIn("enc:v1:", blob, "口令没有加密落盘")

    def test_invalid_values_are_rejected_not_swallowed(self):
        c = self.admin_client()
        bads = [{"allow_register": "yes"}, {"require_email_code": 1},
                {"smtp": {"port": "not-a-port"}}, {"smtp": {"port": 99999}},
                {"smtp": "not-an-object"}, {"smtp": {"enabled": "true"}}]
        for bad in bads:
            with self.subTest(patch=bad):
                r = c.put("/admin/api/settings", json=bad)
                self.assertEqual(r.status_code, 400, r.text)

    def test_unknown_keys_are_ignored(self):
        c = self.admin_client()
        r = c.put("/admin/api/settings", json={"nonsense": 1, "allow_register": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("nonsense", r.json())
        self.assertFalse(r.json()["allow_register"])

    def test_settings_survive_a_reload(self):
        c = self.admin_client()
        c.put("/admin/api/settings", json={"site_name": "我的站点"})
        raw = json.loads(self.state_raw())
        self.assertEqual(raw["service_settings"]["site_name"], "我的站点")

    def test_audit_does_not_record_the_password(self):
        c = self.admin_client()
        c.put("/admin/api/settings", json={"smtp": {"password": "audit-me-not"}})
        blob = json.dumps(self.store().data.get("events") or [])
        self.assertNotIn("audit-me-not", blob)

    def test_smtp_test_requires_admin(self):
        r = self.client.post("/admin/api/settings/smtp-test", json={"to": "x@example.com"})
        self.assertEqual(r.status_code, 401)

    def test_options_is_anonymous_and_minimal(self):
        opt = self.client.get("/login/api/options")
        self.assertEqual(opt.status_code, 200, opt.text)
        keys = set(opt.json().keys())
        self.assertEqual(keys, {"allow_register", "require_email_code", "site_name",
                                "smtp_ready", "register_locked_reason", "site_ready",
                                "user_system_enabled"},
                         "匿名 options 多回了字段：" + repr(sorted(keys)))

    def test_options_exposes_the_master_switch(self):
        """总闸状态**必须匿名可见**（t22 新增）。

        这不是「多回字段」而是**必要**：登录/注册页要靠它决定画不画出表单。
        不知道总闸状态的话，用户会看到一个能填、一提交就被 403 的表单 ——
        纯粹的挫败感，而且他会以为站点坏了。

        它只泄露一个布尔值，不暴露任何配置细节（SMTP 主机、发件人都不在内），
        所以与「匿名只回最小字段集」的初衷不冲突 —— 上一条用例仍守着那个集合，
        这次的扩展是**有意的**，不是不小心漏出来的。
        """
        opt = self.client.get("/login/api/options")
        self.assertIn("user_system_enabled", opt.json())
        # 默认（新装）是关的 —— 与 defaults() 一致。
        self.assertFalse(opt.json()["user_system_enabled"])
        # 打开之后匿名也立刻看得到（登录页据此显示表单）。
        self.admin_client(user_system=True)
        self.assertTrue(self.client.get("/login/api/options").json()["user_system_enabled"])

    def test_options_needs_no_authentication_at_all(self):
        c = TestClient(self.client.app)
        self.assertEqual(c.get("/login/api/options").status_code, 200)

class EmailCodeTest(AccountBase):
    """验证码：一次性、会过期、有次数上限、有重发节流。

    走真实的 SMTP 会话（对本地假服务器），不是只看 `smtp_ready`。
    """

    def setUp(self):
        super().setUp()
        self.fake = FakeSMTP()
        self.addCleanup(self.fake.close)
        self.enable_smtp(self.fake)

    def test_code_is_mailed_not_returned(self):
        r = self.send_code("code@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json().get("dev_mode"))
        self.assertTrue(r.json()["delivered"], "没走真投递")
        self.assertFalse(r.json()["dev_mode"])
        self.assertEqual(r.json()["code"], "", "生产路径把验证码回给调用方了")

        self.assertGreaterEqual(len(self.fake.messages), 1, "没发出邮件")
        self.assertIn("noreply@example.com", self.fake.messages[0]["from"])

    def test_code_in_the_mail_body(self):
        self.send_code("code@example.com")
        code = self.fake.code()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_code_works_and_marks_verified(self):
        self.send_code("code@example.com")
        code = self.fake.code()
        r = self.register("code@example.com", code=code)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.only_member()["email_verified"],
                         "走验证码注册的应当被标为已验证")

    def test_missing_code_is_refused(self):
        self.send_code("code@example.com")
        r = self.register("code@example.com")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "请填写邮箱验证码")

    def test_wrong_code_is_refused(self):
        self.send_code("code@example.com")
        good = self.fake.code()
        bad = "000000" if good != "000000" else "111111"
        r = self.register("code@example.com", code=bad)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "验证码不正确或已过期")

    def test_code_is_single_use(self):
        self.send_code("code@example.com")
        code = self.fake.code()
        self.assertEqual(self.register("code@example.com", code=code).status_code, 200)
        self.client.post("/login/api/logout")
        # 同一枚码不能再用（换个邮箱试，排除「邮箱已注册」这个干扰因素）
        r = self.register("other@example.com", code=code)
        self.assertEqual(r.status_code, 400, r.text)

    def test_code_row_is_consumed_on_success(self):
        self.send_code("code@example.com")
        self.register("code@example.com", code=self.fake.code())
        with self.store().lock:
            self.assertEqual(self.store().data.get("email_codes") or {}, {},
                             "用过的验证码还留在库里")

    def test_expired_code_is_refused(self):
        self.send_code("code@example.com")
        code = self.fake.code()
        with self.store().lock:
            for row in self.store().data["email_codes"].values():
                row["expires_at"] = 1
        r = self.register("code@example.com", code=code)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "验证码不正确或已过期")

    def test_too_many_wrong_tries_burns_the_code(self):
        self.send_code("code@example.com")
        good = self.fake.code()
        for _ in range(5):
            self.register("code@example.com", code="111111" if good != "111111" else "222222")
        # 试满 5 次后正确的码也作废 —— 否则爆破只是变慢，不是被挡住
        r = self.register("code@example.com", code=good)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "验证码已失效，请重新获取")

    def test_code_is_not_stored_in_plaintext(self):
        self.send_code("code@example.com")
        code = self.fake.code()
        rows = self.store().data["email_codes"]
        self.assertEqual(len(rows), 1)
        row = next(iter(rows.values()))
        self.assertNotIn(code, json.dumps(row), "验证码明文落盘了")
        self.assertTrue(str(row["hash"]).startswith("pbkdf2_sha256$"),
                        "验证码用了不带盐的 digest() —— 6 位数的彩虹表是秒破的")

    def test_resend_is_throttled_per_email(self):
        self.assertEqual(self.send_code("code@example.com").status_code, 200)
        r = self.send_code("code@example.com")
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()["detail"], "发送太频繁，请稍后再试")

    def test_resend_throttle_survives_a_different_ip(self):
        """换 IP 绕不过去 —— 这一层是绑在**目标邮箱**上的。"""
        self.assertEqual(self.send_code("code@example.com").status_code, 200)
        r = self.send_code("code@example.com", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 429, r.text)

    def test_hourly_email_cap(self):
        """同邮箱一小时 5 次封顶。

        前两层（60 秒 / 同 IP 10 次）都能被「伪造 X-Forwarded-For」绕过，
        只有绑在目标邮箱上的这一层绕不过，所以这条必须验。
        """
        email = "cap@example.com"
        for i in range(5):
            r = self.send_code(email, headers={"X-Forwarded-For": "198.51.100.%d" % (i + 1)})
            self.assertEqual(r.status_code, 200, "第 %d 次: %s" % (i + 1, r.text))
            with self.store().lock:
                codes = self.store().data["email_codes"]
                if email in codes:
                    codes[email]["sent_at"] = 0   # 假装 60 秒已过；只留小时配额约束
        r = self.send_code(email, headers={"X-Forwarded-For": "198.51.100.99"})
        self.assertEqual(r.status_code, 429, r.text)

    def test_send_code_refuses_an_already_registered_email(self):
        """对已注册邮箱发码 = 白送一个「这个邮箱存在吗」的探针。"""
        with self.store().lock:
            self.store().data.setdefault("members", {})["u1"] = {
                "id": "u1", "email": "taken@example.com", "role": "user",
                "status": "active", "password_hash": "x", "created_at": 0}
            self.store().data.setdefault("member_index_email", {})["taken@example.com"] = "u1"
        r = self.send_code("taken@example.com")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "该邮箱已注册，请直接登录")

    def test_send_code_rejects_a_bad_email(self):
        self.assertEqual(self.send_code("not-an-email").status_code, 400)

    def test_mail_failure_does_not_leak_the_smtp_error(self):
        """给用户的是人话，不是 `smtplib` 的原文 ——
        原文会泄露主机名 / 端口 / 内部错误码。"""
        bad = FakeSMTP(fail_data=True)
        self.addCleanup(bad.close)
        self.enable_smtp(bad)
        r = self.send_code("b@example.com")
        self.assertGreaterEqual(r.status_code, 400, r.text)
        detail = str(r.json().get("detail") or "")
        self.assertNotIn("451", detail)
        self.assertNotIn("Traceback", detail)
        self.assertNotIn("smtplib", detail)

    def test_smtp_auth_is_used_when_configured(self):
        authed = FakeSMTP(user="mailer", password="mailer-pw")
        self.addCleanup(authed.close)
        self.enable_smtp(authed)
        r = self.send_code("auth@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(authed.messages), 1)
        self.assertEqual(authed.errors, [])


class SmtpTestEndpointTest(AccountBase):
    def test_reports_success(self):
        fake = FakeSMTP()
        self.addCleanup(fake.close)
        self.enable_smtp(fake)
        r = self.admin_client().post("/admin/api/settings/smtp-test",
                                     json={"to": "admin@example.com"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(len(fake.messages), 1)

    def test_reports_failure_without_crashing(self):
        fake = FakeSMTP(fail_data=True)
        self.addCleanup(fake.close)
        self.enable_smtp(fake)
        r = self.admin_client().post("/admin/api/settings/smtp-test",
                                     json={"to": "admin@example.com"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["ok"])
        self.assertTrue(r.json().get("error"),
                        "失败结果必须带 error 说明")

    def test_reports_not_configured(self):
        self.admin_client(patch={"smtp": {"enabled": False, "host": ""}})
        r = self.admin_client().post("/admin/api/settings/smtp-test",
                                     json={"to": "admin@example.com"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["ok"])

    def test_rejects_an_empty_recipient(self):
        fake = FakeSMTP()
        self.addCleanup(fake.close)
        self.enable_smtp(fake)
        r = self.admin_client().post("/admin/api/settings/smtp-test", json={"to": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["ok"])   # 空收件人要被拒


class LoginPageTest(AccountBase):
    """`/login` 与它的静态资源。

    `/login` **刻意不做**「已登录就 302」——后端不知道前端要跳到哪，
    插一脚只会制造两套互相打架的跳转规则。
    """

    def test_login_page_is_served_or_404_before_assets_land(self):
        r = self.client.get("/login")
        self.assertIn(r.status_code, (200, 404), r.text)
        if r.status_code == 200:
            self.assertEqual(r.headers.get("content-type", "").split(";")[0],
                             "text/html")

    def test_login_slash_alias(self):
        r = self.client.get("/login/")
        self.assertIn(r.status_code, (200, 404), r.text)

    def test_login_page_is_not_redirected_when_logged_in(self):
        self.open_registration()
        self.register("in@example.com")
        r = self.client.get("/login", follow_redirects=False)
        self.assertNotIn(r.status_code, (301, 302, 303, 307, 308),
                         "登录页把已登录用户重定向走了 —— 那是前端的判断")

    def test_asset_whitelist_blocks_traversal(self):
        """白名单必须挡住穿越，也必须挡住**未被允许的真实文件**。

        2026-10 /portal/ 删除后，原先入参里的 portal 文件名已不存在
        （那样测出来是「文件没了」而不是「白名单拦住了」，理由不对）。
        改用仍然存在的文件做样本：
          · login.html / register.html —— 真实存在但不在白名单里，必须 404；
          · app.js / style.css       —— admin 的资源，跨目录必须 404。
        """
        for bad in ("../login.html", "..%2Flogin.html",
                    "login.html", "register.html", "app.js", "style.css"):
            with self.subTest(filename=bad):
                r = self.client.get("/login/assets/" + bad)
                self.assertEqual(r.status_code, 404, r.text)

    def test_login_page_is_anonymous(self):
        self.assertIn(self.client.get("/login").status_code, (200, 404))


class AdminSessionEndpointsTest(AccountBase):
    """管理端的新端点对 `X-Admin-Token` 与 CSRF 的口径。"""

    def test_settings_readable_with_admin_token(self):
        """X-Admin-Token 是**会话 id**（登录返回的 token），不是 ADMIN_KEY。

        它对应 bearer_session() 那条「头令牌凭持有即证明」的路径 ——
        也就是 SPA 跨源调用走的鉴权方式。新端点必须也认它。
        """
        c = TestClient(self.client.app)
        r = c.post("/admin/api/login", json={"key": ADMIN})
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["token"]
        fresh = TestClient(self.client.app)
        r = fresh.get("/admin/api/settings",
                      headers={"X-Admin-Token": sid})
        self.assertEqual(r.status_code, 200, r.text)

    def test_options_ignores_admin_token(self):
        r = self.client.get("/login/api/options", headers={"X-Admin-Token": ADMIN})
        self.assertEqual(r.status_code, 200, r.text)

    def test_settings_write_rejects_token_only_identity(self):
        """写设置会改 SMTP 口令 / 注册开关 —— 与 `/admin/api/security/config` 同档。"""
        r = self.client.put("/admin/api/settings", json={"allow_register": False},
                            headers={"X-Admin-Token": ADMIN})
        self.assertIn(r.status_code, (401, 403), r.text)


class DevMailOutletTest(AccountBase):
    """`WB_DEV_MAIL=1` 的**开发用邮件出口**。

    队长裁定里的三条约束，逐条验：
      1. 只在环境变量打开时生效，默认关闭；
      2. 只改变「验证码往哪去」，**绝不改变「是否要求验证码」这个判定**；
      3. **不改变 `locked_out_reason()` 的结论**。

    一句话：**dev 出口绕过的是「投递」，不是「校验」。**
    """

    LOCKED = "未配置 SMTP，当前无法注册 请联系管理员关闭验证码验证 或者给您分发账号登录"

    def setUp(self):
        super().setUp()
        os.environ.pop("WB_DEV_MAIL", None)
        self.addCleanup(os.environ.pop, "WB_DEV_MAIL", None)
        # 开着验证码 + 没有 SMTP —— 正是「本机跑不通注册」的原始场景
        self.admin_client(patch={"allow_register": True, "require_email_code": True,
                                 "smtp": {"enabled": False, "host": ""}})

    def test_off_by_default_still_503(self):
        """约束 1 + 3：不开这个开关，行为和以前**一字不差**。"""
        r = self.send_code("dev@example.com")
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.json()["detail"], self.LOCKED)
        self.assertEqual(self.register("dev@example.com", code="123456").status_code, 503)
        opt = self.client.get("/login/api/options").json()
        self.assertFalse(opt["smtp_ready"])
        self.assertEqual(opt["register_locked_reason"], self.LOCKED)

    def test_on_allows_end_to_end_registration(self):
        """打开后本机能走通注册 —— 这正是加这个出口的唯一目的。"""
        os.environ["WB_DEV_MAIL"] = "1"
        r = self.send_code("dev@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        payload = r.json()
        self.assertFalse(payload["delivered"], "打着 dev 标记却说真投递了")
        self.assertTrue(payload["dev_mode"])
        code = payload["code"]
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        rr = self.register("dev@example.com", code=code)
        self.assertEqual(rr.status_code, 200, rr.text)
        self.assertTrue(self.only_member()["email_verified"])

    def test_code_returned_actually_matches_the_stored_hash(self):
        """回给调用方的码必须是**真能用的那一枚**，不是另生成一个。"""
        os.environ["WB_DEV_MAIL"] = "1"
        code = self.send_code("match@example.com").json()["code"]
        wrong = "000000" if code != "000000" else "111111"
        # 错的必须被拒
        self.assertEqual(self.register("match@example.com", code=wrong).status_code, 400)
        # 对的必须能用
        self.assertEqual(self.register("match@example.com", code=code).status_code, 200)

    def test_dev_outlet_does_not_relax_require_email_code(self):
        """约束 2：**是否要验证码**的判定完全不变。

        打开 dev 出口后，`require_email_code` 仍然有效 ——
        不带验证码注册必须还是 400，而不是「反正能拿到码就免了」。
        """
        os.environ["WB_DEV_MAIL"] = "1"
        self.assertEqual(self.send_code("strict@example.com").status_code, 200)
        r = self.register("strict@example.com")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "请填写邮箱验证码")

    def test_dev_outlet_does_not_relax_allow_register(self):
        os.environ["WB_DEV_MAIL"] = "1"
        self.admin_client().put("/admin/api/settings", json={"allow_register": False})
        r = self.register("closed@example.com", code="123456")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"], "管理员已关闭注册")

    def test_dev_outlet_is_reported_honestly(self):
        """约束 3：dev 出口下注册**不被挡**，但对外汇报**不谎报 SMTP 已配置**。

        ⚠️ 这两个字段职责不同，**不能合并**：

          · `smtp_ready`             -> 「管理员配了 SMTP 吗」
            -> **不含** dev 例外。理由有二：① 报 true 等于匿名可知「本机开了
               dev 出口」；② 它会让注册四态漂移出第五种，UI 分不清
               「邮件真会发出去」和「验证码只回本机」。
          · `register_locked_reason` -> 「现在能注册吗」
            -> **含** dev 例外。否则本机开着 dev 出口时根本注册不了。

        这不是放宽：dev 出口绕过的是**投递**，不是**校验**。
        而 `/login/api/options` 仍**不回**任何能看出「这是 dev 模式」的字段。
        """
        os.environ["WB_DEV_MAIL"] = "1"
        opt = self.client.get("/login/api/options").json()
        # 对外汇报：不把 dev 出口算作「SMTP 已配置」。
        self.assertFalse(opt["smtp_ready"])
        # 放行判断：dev 出口仍然算「投递可用」，注册不被挡。这条不能删 ——
        # 它才是这个用例原本要守的东西（本机自测必须注册得进去）。
        self.assertEqual(opt["register_locked_reason"], "")
        self.assertNotIn("dev_mode", opt)

    def test_env_value_must_be_affirmative(self):
        """`WB_DEV_MAIL=0` / 空串 / 乱七八糟的值都不算打开。

        「设了变量就生效」是个危险默认 —— 运维把 `WB_DEV_MAIL=0` 当关闭写，
        结果反而打开了一道绕过投递的口子。
        """
        for value in ("0", "", "no", "off", "false", "maybe"):
            with self.subTest(value=value):
                os.environ["WB_DEV_MAIL"] = value
                r = self.send_code("val@example.com")
                self.assertEqual(r.status_code, 503, "value=%r 被当成了打开" % value)
                # 清掉限流，免得下一次撞到 60 秒节流
                with self.store().lock:
                    self.store().data.get("email_codes", {}).clear()
                with self.store().lock:
                    self.store().data.get("code_email_buckets", {}).clear()
                with self.store().lock:
                    self.store().data.get("code_ip_buckets", {}).clear()

    def test_real_smtp_takes_precedence_over_dev_outlet(self):
        """配好了 SMTP 就走真投递，**不再把码回给响应**。"""
        fake = FakeSMTP()
        self.addCleanup(fake.close)
        os.environ["WB_DEV_MAIL"] = "1"
        self.enable_smtp(fake)
        r = self.send_code("both@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["delivered"])
        self.assertFalse(r.json()["dev_mode"])
        self.assertEqual(r.json()["code"], "")
        self.assertEqual(len(fake.messages), 1, "该走真投递却没发信")



if __name__ == "__main__":
    unittest.main()
