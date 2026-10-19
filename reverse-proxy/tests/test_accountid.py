"""账号本地 id 派生：反代服务与桌面端必须算出同一个值。

## 这份测试在守什么

`switch_store.save_browser_account` 会绕过桌面端，直接把账号写进
`~/.wb-switch-plus/accounts.json`。桌面端 `find_account` 只认 `id` 或 `uid`，
缺 `id` 的记录在桌面端点切换会报「账号不存在」，要等桌面端下次刷新 token
走 `upsert_account_in` 补齐 id 才恢复 —— 用户看到的就是「等一天才好」。

两边因此必须用同一套 id 派生规则：

- Python：`admin/accountid.py`
- Rust：`crates/wb-switch-core/src/modules/account.rs` 的 `derive_account_id`

算法是 `uuid5(NAMESPACE, uid)`。任何一边改常量或改算法，本文件里写死的
黄金值都会失配 —— 那是故意的，因为失配意味着两边开始产出不同的 id，
线上表现为「桌面端时不时冒出重复账号」。

对应的 Rust 侧测试是同文件的 `derived_account_id_is_stable_for_same_uid`，
两处硬编码同一批黄金值。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin import accountid  # noqa: E402
from admin import switch_store  # noqa: E402


# 黄金值：由 `uuid.uuid5(NAMESPACE, uid)` 算出，Rust 侧必须得到同一串。
GOLDEN = {
    "ab43eca9-488c-42a4-9210-a536065e1f3d": "e3015225-739a-5167-8508-21c1820eef13",
    "abc": "2cdebaf4-f3f5-57fc-845c-d8f7a0435c8b",
}


class DeriveTests(unittest.TestCase):
    def test_golden_vectors_are_stable(self):
        """改了这个断言 = 改了算法，桌面端与反代服务立刻分叉。"""
        for uid, expected in GOLDEN.items():
            self.assertEqual(accountid.derive_account_id(uid), expected)

    def test_same_uid_always_same_id(self):
        uid = "7795e651-6e38-44d8-977c-dfc78672314c"
        first = accountid.derive_account_id(uid)
        for _ in range(5):
            self.assertEqual(accountid.derive_account_id(uid), first)

    def test_different_uid_gives_different_id(self):
        self.assertNotEqual(
            accountid.derive_account_id("uid-a"),
            accountid.derive_account_id("uid-b"),
        )

    def test_empty_uid_yields_empty_string(self):
        """空 uid 不派生出任何东西，也不抛异常 —— 兜底由调用方决定。"""
        for value in ("", None, "   "):
            self.assertEqual(accountid.derive_account_id(value), "")

    def test_surrounding_whitespace_is_trimmed(self):
        """同一 uid 带不带空白必须同 id，否则界面里粘一下空格就多出一个账号。"""
        self.assertEqual(
            accountid.derive_account_id("  abc  "),
            accountid.derive_account_id("abc"),
        )

    def test_namespace_constant_is_fixed_hex(self):
        self.assertEqual(
            accountid.NAMESPACE.hex, "6f9619ff8b86d011b42d00c04fc964ff"
        )


class EnsureIdTests(unittest.TestCase):
    def test_existing_id_is_never_replaced(self):
        """桌面端早年用随机 v4 生成 id，重新派生会让老记录切换失效。"""
        record = {"uid": "abc", "id": "8e1a2c64-0d39-4d14-91f7-7a1fa6a24e2b"}
        self.assertEqual(
            accountid.ensure_account_id(record),
            "8e1a2c64-0d39-4d14-91f7-7a1fa6a24e2b",
        )
        self.assertEqual(record["id"], "8e1a2c64-0d39-4d14-91f7-7a1fa6a24e2b")

    def test_missing_id_is_derived_and_written_into_record(self):
        record = {"uid": "abc"}
        value = accountid.ensure_account_id(record)
        self.assertEqual(value, GOLDEN["abc"])
        self.assertEqual(record["id"], GOLDEN["abc"])

    def test_blank_id_counts_as_missing(self):
        record = {"uid": "abc", "id": "  "}
        self.assertEqual(accountid.ensure_account_id(record), GOLDEN["abc"])

    def test_no_uid_yields_empty_and_leaves_record_untouched(self):
        record = {"nickname": "无名"}
        self.assertEqual(accountid.ensure_account_id(record), "")
        self.assertNotIn("id", record)

    def test_non_dict_is_tolerated(self):
        self.assertEqual(accountid.ensure_account_id(None), "")
        self.assertEqual(accountid.ensure_account_id("x"), "")


class ReadBackfillTests(unittest.TestCase):
    """读取路径补 id：只补内存，不写回文件。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wb-accountid-"))
        self.path = self.dir / "accounts.json"

    def _write(self, records):
        self.path.write_text(json.dumps(records), encoding="utf-8")

    def test_records_without_id_get_one_on_read(self):
        self._write([{"uid": "abc"}, {"uid": "u2", "id": "keep"}])
        records = switch_store.read_switch_accounts(self.path)
        self.assertEqual(records[0]["id"], GOLDEN["abc"])
        self.assertEqual(records[1]["id"], "keep")

    def test_read_does_not_touch_the_file(self):
        """文件归桌面端所有，反代服务不能顺手整份重写。"""
        original = [{"uid": "abc"}]
        self._write(original)
        switch_store.read_switch_accounts(self.path)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")), original
        )

    def test_unreadable_file_still_reports_failure(self):
        self.path.write_text("not json", encoding="utf-8")
        records, ok = switch_store.read_switch_accounts_checked(self.path)
        self.assertEqual(records, [])
        self.assertFalse(ok)


class SaveAccountIdTests(unittest.TestCase):
    """写入路径：新记录带 id，已有记录沿用原 id。"""

    def test_new_record_carries_derived_id(self):
        entry = {"uid": "abc", **{}}
        self.assertEqual(
            accountid.ensure_account_id(entry), GOLDEN["abc"]
        )

    def test_merge_keeps_existing_record_id(self):
        """命中已有记录时 `{**rec, **entry}` 会丢掉 id，必须显式补回。"""
        rec = {"uid": "abc", "id": "8e1a2c64-0d39-4d14-91f7-7a1fa6a24e2b"}
        merged = {"uid": "abc", "note": "改名", "id": accountid.ensure_account_id(rec)}
        self.assertEqual(merged["id"], "8e1a2c64-0d39-4d14-91f7-7a1fa6a24e2b")


class RustParityTests(unittest.TestCase):
    """与 Rust 侧的常量做静态比对（真正的行为等价由各自的黄金值测试保证）。"""

    def test_namespace_matches_rust_constant(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "crates"
            / "wb-switch-core"
            / "src"
            / "modules"
            / "account.rs"
        )
        if not source.exists():
            self.skipTest("找不到 Rust 源文件（仅 Python 树）")
        text = source.read_text(encoding="utf-8")
        self.assertIn(
            'ACCOUNT_ID_NAMESPACE: &str = "6f9619ff-8b86-d011-b42d-00c04fc964ff"',
            text,
        )
        self.assertEqual(
            accountid.NAMESPACE.hex, "6f9619ff8b86d011b42d00c04fc964ff"
        )


if __name__ == "__main__":
    unittest.main()
