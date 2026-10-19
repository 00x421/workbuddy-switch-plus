"""账号本地 id 的派生规则（反代服务与桌面端共用同一算法）。

## 为什么需要这一层

桌面端的 `accounts.json` 里每条记录有两个标识：

- `uid`：上游（CodeBuddy / WorkBuddy AI）给的账号 uid；
- `id`：本地 uuid，`switch::switch_account` → `account::find_account` 按它查号。

`account::find_account_in`（`crates/wb-switch-core/src/modules/account.rs:52`）
**只认 id 或 uid 两者**：

    account.get("id").as_str() == Some(account_id)
        || account.get("uid").as_str() == Some(account_id)

反代服务早期版本的 `save_browser_account` 只写 uid 不写 id，于是这条记录在桌面端
是「有 uid 但没有 id」的形态。表现就是用户报的那个 bug：在反代服务里登录的账号，
桌面端从没登录过，点切换报 account id 错误；要等到桌面端自己刷新 token、走
`upsert_account_in` 的 id 补齐分支，才「等一天就好了」。

## 算法

`uuid5(NAMESPACE, uid)` —— 纯函数，任何一端、任何机器、任何次序都算出同一个值。

### 已存在的 id 一律不动

这是硬约束，不是优化：桌面端早年用 `Uuid::new_v4()`（随机）生成 id，用户电脑上
已有十几条这样的记录。如果改成「按 uid 重新派生」，这些记录的 id 会变，切换、
会话绑定、导出文件名里记着的旧 id 全部失效 —— 等于用修 bug 的名义制造更大的 bug。

所以规则是：**已有 id 就原样保留；只有缺 id 时才派生。**

### 与桌面端的等价性

Rust 侧是同目录 `account.rs` 的 `derive_account_id(uid)`，命名空间常量两边写死成
同十六进制串，由 `test_derive_id_matches_rust_constant` 与 Rust 的
`derived_account_id_is_stable_for_same_uid` 互相锁定。改其中一边而不改另一边，
测试会直接红。
"""

from __future__ import annotations

import uuid

# 固定命名空间。十六进制串直接写死，不用 uuid.NAMESPACE_DNS —— 那个常量属于
# 标准库实现细节，Rust 侧没有对等物，两边只能靠这个显式常量对齐。
NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def derive_account_id(uid: str) -> str:
    """按 uid 派生稳定的本地账号 id。

    空 uid 返回空串而不是抛异常：调用方在这里拿到的空串会被上层当成「无法派生」
    走各自的兜底（反代侧退回随机 uid 作 key，桌面端走 `Uuid::new_v4()`），
    本函数不该替它们决定。
    """
    value = str(uid or "").strip()
    if not value:
        return ""
    return str(uuid.uuid5(NAMESPACE, value))


def ensure_account_id(record: dict) -> str:
    """返回记录应持有的 id：已有的不动，缺失的按 uid 派生，派不出则空串。

    就地写回 `record["id"]`，因为两个调用点（落盘前、读取后）都需要记录里
    真的有这个字段，而不只是拿到返回值。
    """
    if not isinstance(record, dict):
        return ""
    current = str(record.get("id") or "").strip()
    if current:
        return current
    derived = derive_account_id(record.get("uid"))
    if derived:
        record["id"] = derived
    return derived
