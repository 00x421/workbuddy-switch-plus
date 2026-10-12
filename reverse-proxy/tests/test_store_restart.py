"""服务「第二次启动」必须能起来——回归 `KeyError: 'accounts'`。

背景：`accounts` 是**运行时视图**，每次启动都由账号库（`~/.wb-switch-plus/accounts.json`）
重建，`save()` 因此刻意不把它落盘。于是磁盘上的 `state.json` 里没有这个键，
第二次启动把文件读回来时，`sync_from_switch()` 一读 `data["accounts"]` 就 KeyError，
表现为「服务第一次能起，之后每次启动直接崩」。

这个用例之前没人写：既有测试都用全新的临时目录，走的都是「文件不存在」那条分支，
恰好绕开了「文件存在但没有 accounts」这条真实路径。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from admin.switch_store import SwitchStore

ADMIN_KEY = "admin-test-credential-long-enough"


def record(uid, name):
    return {"uid": uid, "nickname": name, "enterpriseId": "ent-" + uid,
            "access_token": "token-" + uid, "refresh_token": "refresh-" + uid,
            "expiresAt": int(time.time() * 1000) + 3600000, "variant": "cn",
            "domain": "www.codebuddy.cn"}


class RestartSuite(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.accounts = self.root / "accounts.json"
        self._tick = time.time_ns()
        self.write_accounts([record("u1", "一号"), record("u2", "二号")])

    def tearDown(self):
        self.temp.cleanup()

    def write_accounts(self, records):
        self.accounts.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        self._tick += 1_000_000_000
        os.utime(self.accounts, ns=(self._tick, self._tick))

    def make_store(self):
        return SwitchStore(
            root=self.root / "data",
            accounts_path=self.accounts,
            admin_key=ADMIN_KEY,
        )

    def test_state_json_on_disk_has_no_accounts(self):
        """先钉住前提：落盘的 state.json 里确实没有 accounts（否则这个回归就没意义了）。"""
        self.make_store()
        on_disk = json.loads((self.root / "data" / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("accounts", on_disk)
        self.assertIn("keys", on_disk)

    def test_second_start_rebuilds_accounts(self):
        """第二次启动：读回没有 accounts 的 state.json，既不崩、也要把账号重建出来。"""
        first = self.make_store()
        self.assertEqual(len(first.data["accounts"]), 2)

        second = self.make_store()          # ← 修复前这里直接 KeyError
        self.assertEqual(len(second.data["accounts"]), 2)
        self.assertEqual({a["name"] for a in second.data["accounts"].values()}, {"一号", "二号"})

    def test_third_start_after_account_added(self):
        """再次启动仍然稳定，并且账号库的新增会被带进来。"""
        self.make_store()
        self.write_accounts([record("u1", "一号"), record("u2", "二号"), record("u3", "三号")])
        third = self.make_store()
        self.assertEqual(len(third.data["accounts"]), 3)


if __name__ == "__main__":
    unittest.main()
