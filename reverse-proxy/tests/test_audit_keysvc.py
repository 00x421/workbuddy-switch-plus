"""AUDIT — admin/keysvc.py (+ one admin/security.py finding). Reproductions only."""
import threading
import time
import unittest

from fastapi import HTTPException

from admin import keysvc, security


class MemoryStore:
    """Minimal Store for keysvc / security unit-level checks."""

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"keys": {}, "api_key_ips": {}}
        self.sessions = {}
        self.saves = 0

    def save(self):
        self.saves += 1


def _disk_full():
    raise OSError("disk full")


def reject_code(exc: HTTPException):
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return (detail.get("error") or {}).get("code")
    return None


class ValidateCredentialDefensiveTest(unittest.TestCase):
    """BUG-K1 (keysvc.py:272/274/277): validate_credential() blows up on a
    non-numeric policy field instead of rejecting the key.

    Every normaliser in this module is deliberately forgiving — norm_credit()
    swallows dirty values, norm_realm() downgrades to "", norm_cidrs() strips.
    validate_credential() alone calls int()/float() straight on the stored row,
    so one corrupt field turns EVERY request carrying that key into an unhandled
    ValueError (HTTP 500) rather than a clean 4xx. The row lives in state.json,
    which is hand-editable and written by older versions, so it is reachable.

    Raw int()/float() calls: expires_at -> int(), quota/used_tokens -> int(),
    quota_credit/used_credit -> float().
    """

    def test_corrupt_quota_does_not_raise(self):
        key = {"id": "k1", "enabled": True, "quota": "not-a-number"}
        try:
            keysvc.validate_credential(key)
        except HTTPException:
            return
        except (TypeError, ValueError) as exc:
            self.fail(f"raised {type(exc).__name__} instead of rejecting: {exc}")

    def test_corrupt_used_credit_does_not_raise(self):
        key = {"id": "k1", "enabled": True, "quota_credit": 10, "used_credit": "NaN-ish"}
        try:
            keysvc.validate_credential(key)
        except HTTPException:
            return
        except (TypeError, ValueError) as exc:
            self.fail(f"raised {type(exc).__name__} instead of rejecting: {exc}")

    def test_corrupt_expires_at_does_not_raise(self):
        key = {"id": "k1", "enabled": True, "expires_at": "soon"}
        try:
            keysvc.validate_credential(key)
        except HTTPException:
            return
        except (TypeError, ValueError) as exc:
            self.fail(f"raised {type(exc).__name__} instead of rejecting: {exc}")


class QuotaBoundaryTest(unittest.TestCase):
    """Contract check — quota is a "reject once reached" gate. Pinned so the
    off-by-one cannot drift silently. These all PASS; listed to mark the
    boundary as audited rather than assumed."""

    def test_used_equal_to_quota_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            keysvc.validate_credential({"enabled": True, "quota": 100, "used_tokens": 100})
        self.assertEqual(reject_code(ctx.exception), "quota_exhausted")

    def test_used_one_below_quota_is_allowed(self):
        keysvc.validate_credential({"enabled": True, "quota": 100, "used_tokens": 99})

    def test_no_quota_means_unlimited(self):
        keysvc.validate_credential({"enabled": True, "quota": 0, "used_tokens": 10 ** 9})

    def test_credit_quota_boundary(self):
        with self.assertRaises(HTTPException) as ctx:
            keysvc.validate_credential({"enabled": True, "quota_credit": 1.0, "used_credit": 1.0})
        self.assertEqual(reject_code(ctx.exception), "credit_quota_exhausted")


class TouchKeyAccountingTest(unittest.TestCase):
    """NOT-A-BUG (keysvc.py:374) — 复审后判定为**有意设计**，不是缺陷。

    audit 脚本原先把这处 `except Exception: pass` 报成了 bug：save 失败时
    used_tokens / used_credit 已经在内存里改过、落盘被跳过、调用方不知情。

    但 touch_key 的 docstring 写得很清楚：「旁路，异常不影响请求」。
    记账是**旁路**：它失败的正确后果是「这次用量没记上」，
    而不是「用户的请求失败」。反过来做才是真的错 ——
    磁盘满 / state.json 被写坏的时候，让所有下游请求一起 500，
    等于用一个统计问题换一次全站故障。

    所以下面两个用例**保留为「记录当前契约」的测试**：它们断言的是
    「确实被吞掉」。哪天有人把它改成抛出，这里会红，提醒他先想清楚
    「记账失败该不该影响用户的请求」这个问题。
    """

    def test_save_failure_is_deliberately_swallowed(self):
        store = MemoryStore()
        kid, _token = keysvc.create_key(store, "audit")
        store.data["polluted"] = {1, 2, 3}   # a set is not JSON-serialisable

        def real_save():
            import json
            json.dumps(store.data)           # exactly what write_json() does

        store.save = real_save  # type: ignore[assignment]
        # 契约：记账失败不向上抛，请求照常完成。
        keysvc.touch_key(store, kid, "1.2.3.4", tokens=10, credit=0.5)
        item = store.data["keys"][kid]
        self.assertEqual(item["used_tokens"], 10, "内存里的累计应该照常生效")

    def test_injected_oserror_is_deliberately_swallowed(self):
        store = MemoryStore()
        kid, _token = keysvc.create_key(store, "audit")
        store.save = _disk_full  # type: ignore[assignment]
        # 同上：OSError（磁盘满）也不该让请求失败。
        keysvc.touch_key(store, kid, "1.2.3.4", tokens=10, credit=0.5)
        self.assertEqual(store.data["keys"][kid]["used_tokens"], 10)


class UpdateAndResetTest(unittest.TestCase):
    """Contract checks — these PASS."""

    def test_update_name_is_trimmed_and_echoed(self):
        store = MemoryStore()
        kid, _ = keysvc.create_key(store, "old")
        self.assertEqual(keysvc.update_key(store, kid, {"name": "  new name  "})["name"],
                         "new name")

    def test_reset_usage_zeroes_both_counters(self):
        store = MemoryStore()
        kid, _ = keysvc.create_key(store, "k", quota_credit=5)
        keysvc.touch_key(store, kid, "1.1.1.1", tokens=7, credit=1.25)
        keysvc.reset_usage(store, kid)
        item = store.data["keys"][kid]
        self.assertEqual(item["used_tokens"], 0)
        self.assertEqual(item["used_credit"], 0.0)

    def test_ip_list_is_capped_at_64(self):
        store = MemoryStore()
        kid, _ = keysvc.create_key(store, "k")
        for i in range(200):
            keysvc.touch_key(store, kid, f"10.0.{i // 256}.{i % 256}")
        self.assertEqual(len(store.data["api_key_ips"][kid]), 64)


class SessionUsernameFallbackTest(unittest.TestCase):
    """BUG-K3 (security.py:333-334 vs 409-412): the username default is applied
    on READ but not on REVOKE, so a session stored with an empty username cannot
    be revoked while it still authenticates as admin.

    * current_user() returns record.get("username", "admin") — read-side default.
    * revoke_user_sessions(name) compares against the STORED value, so "" never
      matches any caller-supplied name.

    server.py passes a non-empty actor on all three login paths today, so this is
    a latent inconsistency (one start_session("", ...) caller away from being a
    real "changed the password but the attacker is still logged in" bug).
    """

    def setUp(self):
        self.store = MemoryStore()
        self.sec = security.Security(self.store, admin_key="adminkey-adminkey",
                                     legacy_key_configured=True)

    def test_empty_username_session_is_not_revocable(self):
        sid, _csrf = self.sec.start_session("", "admin", via="session")
        self.sec.revoke_user_sessions("admin")
        self.assertNotIn(security.digest(sid), self.store.sessions,
                         "a session that authenticates as admin survived revocation")


class RealLoginRevocationTest(unittest.TestCase):
    """Control — the ordinary login path revokes correctly. PASSES."""

    def test_revoke_drops_the_session_for_a_named_user(self):
        store = MemoryStore()
        sec = security.Security(store, admin_key="", legacy_key_configured=False)
        sid, _csrf = sec.start_session("admin", "admin", via="session")
        self.assertEqual(sec.revoke_user_sessions("admin"), 1)
        self.assertNotIn(security.digest(sid), store.sessions)


if __name__ == "__main__":
    unittest.main()
