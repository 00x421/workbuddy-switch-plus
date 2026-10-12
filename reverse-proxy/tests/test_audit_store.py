"""AUDIT - admin/store.py equivalent (SwitchStore) + admin/security.py caps.

Reproductions for audit findings only.
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from admin import security
from admin.switch_store import SwitchStore, write_json

ADMIN_KEY = "admin-test-credential-long-enough"


def account(uid, **extra):
    rec = {"uid": uid, "nickname": uid, "access_token": "tok-" + uid,
           "refresh_token": "ref-" + uid, "expiresAt": int(time.time() * 1000) + 3600_000,
           "enterpriseId": "ent", "domain": "codebuddy.cn"}
    rec.update(extra)
    return rec


class TempSwitch:
    """SwitchStore backed by a throwaway accounts.json + state.json."""

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.accounts = root / "accounts.json"
        self.accounts.write_text(json.dumps([account("u1", nickname="One")]), encoding="utf-8")
        self.store = SwitchStore(root / "state", accounts_path=self.accounts,
                                 admin_key=ADMIN_KEY)
        return self

    def __exit__(self, *exc):
        self.dir.cleanup()


class SecurityFailTableCapTest(unittest.TestCase):
    """BUG-S1 (security.py:241-250): MAX_TRACKED is not enforced.

    `_prune()` only evicts entries whose age is already >= LOCK_SECONDS (600s).
    Any entry younger than the window is kept, and there is no other cap, so a
    client that varies the key on every attempt grows the table without bound.

    Measured: 30 000 distinct IPs => _fail holds 30 000 entries (6x MAX_TRACKED).
    The same applies to the per-username table. Both live for the whole process
    lifetime; only the timer-based `_prune` ever removes anything, and it removes
    nothing while the flood is ongoing.

    Impact: unbounded memory growth reachable from unauthenticated endpoints
    (the login route calls record_fail before any credential check), i.e. a
    remote memory-exhaustion vector.
    """

    def setUp(self):
        self.store = _FakeStore()
        self.sec = security.Security(self.store, admin_key="k" * 20,
                                     legacy_key_configured=True)

    def test_fail_table_is_capped_by_max_tracked(self):
        for i in range(security.MAX_TRACKED * 3):
            self.sec.record_fail("10.%d.%d.%d" % (i // 65536, (i // 256) % 256, i % 256))
        self.assertLessEqual(len(self.sec._fail), security.MAX_TRACKED)

    def test_user_fail_table_is_capped(self):
        for i in range(security.MAX_TRACKED * 3):
            self.sec.record_fail("1.1.1.1", username="user%d" % i)
        self.assertLessEqual(len(self.sec._user_fail), security.MAX_TRACKED)

    def test_prune_does_nothing_when_nothing_has_expired(self):
        """The root cause, stated directly."""
        table = {"k%d" % i: [1, time.time()] for i in range(security.MAX_TRACKED * 2)}
        self.sec._prune(table)
        self.assertLessEqual(len(table), security.MAX_TRACKED)


class _FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}
        self.sessions = {}
        self.attempts = {}

    def save(self):
        pass


class SecurityAuditCapTest(unittest.TestCase):
    """BUG-S2 check: audit_logs is trimmed to AUDIT_LIMIT. PASSES."""

    def test_audit_log_is_trimmed(self):
        store = _FakeStore()
        sec = security.Security(store, admin_key="k" * 20, legacy_key_configured=True)
        for i in range(security.AUDIT_LIMIT * 3):
            sec.audit({"username": "admin"}, "act", "t", "d%d" % i)
        self.assertEqual(len(store.data["audit_logs"]), security.AUDIT_LIMIT)
        # the newest entries survive (trim happens at the front)
        self.assertEqual(store.data["audit_logs"][-1]["detail"],
                         "d%d" % (security.AUDIT_LIMIT * 3 - 1))


class SecuritySessionCapTest(unittest.TestCase):
    """BUG-S3 check: store.sessions is capped at 100 in start_session. PASSES."""

    def test_sessions_are_capped(self):
        store = _FakeStore()
        sec = security.Security(store, admin_key="k" * 20, legacy_key_configured=True)
        for _ in range(250):
            sec.start_session("admin", "admin")
        self.assertLessEqual(len(store.sessions), 100)


class SwitchStoreLeaveOnDiskTest(unittest.TestCase):
    """FIXED-S4 (switch_store.py): save() must not drop write failures silently.

    Original defect: `except OSError: pass` around write_json(). state.json
    holds the API keys, their owner and pool settings; on a read-only volume or
    a full disk every mutation still returned 200 while nothing was persisted,
    surfacing only after a restart as "all my keys vanished" with no log trace.

    Fixed while this audit ran: save() now reports the failure on stderr but
    still deliberately does NOT re-raise (callers are request paths and must not
    turn a persistence problem into a 500 for every user). These tests pin that
    exact contract - observable failure, non-fatal to the caller.
    """

    def test_save_failure_is_reported_not_silently_swallowed(self):
        import contextlib
        import io
        with TempSwitch() as env:
            store = env.store
            store.data["keys"]["sentinel"] = {"name": "sentinel"}

            def boom(*a, **k):
                raise OSError("read-only file system")

            import admin.switch_store as ss
            original = ss.write_json
            ss.write_json = boom
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    store.save()          # must not raise: callers are request paths
            finally:
                ss.write_json = original
            self.assertIn("read-only file system", err.getvalue(),
                          "the persistence failure left no trace anywhere")


class SwitchStoreAccountsNotPersistedTest(unittest.TestCase):
    """BUG-S5 check (documented design): `accounts` is deliberately excluded from
    state.json. PASSES - recorded so the exclusion is marked intentional."""

    def test_accounts_are_not_written_to_state_json(self):
        with TempSwitch() as env:
            store = env.store
            store.save()
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertNotIn("accounts", payload)
            self.assertIn("keys", payload)


class SwitchStoreReloadTest(unittest.TestCase):
    """BUG-S6 check: an unreadable accounts file keeps the previous view
    (never treats a transient read error as "all accounts deleted"). PASSES."""

    def test_corrupt_accounts_file_is_not_treated_as_empty(self):
        with TempSwitch() as env:
            store = env.store
            self.assertIn("u1", store.data["accounts"])
            env.accounts.write_text("{ this is not json", encoding="utf-8")
            result = store.refresh_accounts(force=True)
            self.assertTrue(result["stale"])
            self.assertIn("u1", store.data["accounts"])

    def test_missing_accounts_file_is_empty_not_stale(self):
        with TempSwitch() as env:
            store = env.store
            env.accounts.unlink()
            result = store.refresh_accounts(force=True)
            self.assertFalse(result["stale"])
            self.assertEqual(store.data["accounts"], {})


class SwitchStoreOverridePersistenceTest(unittest.TestCase):
    """BUG-S7 check: local enable/disable survives an accounts-file reload.
    PASSES."""

    def test_disable_override_survives_refresh(self):
        with TempSwitch() as env:
            store = env.store
            store.set_enabled("u1", False)
            store.refresh_accounts(force=True)
            self.assertFalse(store.data["accounts"]["u1"]["enabled"])

    def test_hidden_account_stays_hidden_after_reload(self):
        with TempSwitch() as env:
            store = env.store
            store.hide_account("u1")
            store.refresh_accounts(force=True)
            self.assertNotIn("u1", store.data["accounts"])


if __name__ == "__main__":
    unittest.main()
