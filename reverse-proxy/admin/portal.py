"""用户自助门户（服务平台侧）。

> ⚠️ **2026-10 起：路由与接口已全部删除，本模块目前没有调用方。**
>
> 用户裁定：「我让你给这个portal删了 你咋还不删」。于是：
>   · `server.py` 里的 `/portal`、`/portal/`、`/portal/assets/*` 三个路由已删；
>   · `/portal/api/overview|flows|redeem` 三个接口已删；
>   · 原先调用本模块的 `portal_key` / `portal_identity` / `portal_uid` /
>     `portal_keys_of` / `portal_session_payload` 五个辅助函数已删。
>
> **那为什么还留着这个文件？** 三个理由：
>   1. `owner_of()` 是「这枚 key 属于谁」的**通用判定**，语义不属于门户；
>   2. `members.py` 有 3 处注释把本文件的 `redeem()` 当作
>      「无主 key 不能核销」这条规则的**权威出处**引用（见 `portal.py:115-120`
>      附近）。删掉文件，那 3 处立刻变成**悬空引用**；
>   3. 全是纯函数、无路由、无副作用 —— 留着不会被执行到，死代码风险接近零。
>
> **所以这是有意保留的，不是忘了删。** 完整记录见 `_design/t24-portal-removal.md`。
> 若将来重建用户端，`redeem()` 里那两处限流保护（成功不清零 / 只删过期窗口）
> 必须一并回来 —— 原文存在 `C:/Work/_shots/t24-backup/`。

和 `/admin/api/*` 的根本区别：**这里的管理员接口一律不碰**。
调用者拿的是自己的 **API Key**，只能看到「自己这枚密钥」的数据。

设计要点：

1. **身份 = 密钥本身**。用户用 `Authorization: Bearer sk-...` 登录门户，
   不需要管理员账号。这样「拿到 key 的人」和「看自己用量的人」是同一个，
   不用再发一套密码，也就没有第二套凭据会泄露。
2. **一切以「这枚密钥」为过滤条件**，绝不做「传 username 就查谁」。
   任何让调用方指定「查谁」的参数都是越权入口。
3. **余额挂在 `owner` 上**，不是挂在密钥上。同一个人可以有多枚密钥共用一份余额；
   没设 owner 的密钥（管理员随手建的）余额为 0，这是**故意的** ——
   宁可让自助充值看起来「没生效」，也不能把某个人的钱记到没人认领的池子里。
4. **绝不回显 `hash`**，也绝不回显别人的 `secret`。
"""

from __future__ import annotations

import time

from fastapi import HTTPException

from . import redemption as rd


def owner_of(key: dict) -> str:
    """这枚密钥属于谁。没归属就返回空串 —— 见模块头第 3 条。"""
    return str(key.get("owner") or "").strip()


def view_key(key: dict) -> dict:
    """给自己看的密钥视图。

    不回显明文 `secret`：门户是拿 key 登进来的，如果这里再回显一次，
    那么任何一次「临时借用别人的 key」都会顺手把长期凭据交出去。
    只给前缀，够用户认人就行。
    """
    used_tokens = int(key.get("used_tokens", 0) or 0)
    used_credit = float(key.get("used_credit", 0) or 0)
    quota = int(key.get("quota", 0) or 0)
    quota_credit = float(key.get("quota_credit", 0) or 0)
    return {
        "id": key.get("id", ""),
        "name": key.get("name", ""),
        "hint": key.get("hint", ""),
        "enabled": bool(key.get("enabled", True)),
        "expires_at": key.get("expires_at"),
        "created": key.get("created"),
        "last_used_at": key.get("last_used_at"),
        "models": list(key.get("models") or []),
        "groups": list(key.get("groups") or []),
        "quota": quota,
        "used_tokens": used_tokens,
        "tokens_left": max(0, quota - used_tokens) if quota else None,
        "quota_credit": quota_credit,
        "used_credit": round(used_credit, 6),
        "credit_left": round(max(0.0, quota_credit - used_credit), 6) if quota_credit else None,
    }


def overview(store, key: dict, *, days: int = 30) -> dict:
    """门户首页：我是谁 / 我的密钥 / 我的余额 / 我的用量。"""
    owner = owner_of(key)
    kid = key.get("id", "")

    from . import usage
    with store.lock:
        snapshot = usage.snapshot(store, days=days, hours=24)

    # 只挑出「这枚密钥」的量。usage 的桶里 by_key 就是以 kid 为键的字典，
    # 所以这里不需要遍历请求日志，也就不会把日志上限（300 条）当成统计上限。
    def mine(rows: list, bucket: str) -> list:
        out = []
        for row in rows:
            hit = (row.get(bucket) or {}).get(kid)
            if not hit:
                continue
            out.append({**{k: v for k, v in row.items() if k != bucket}, **hit})
        return out

    daily = mine(snapshot.get("daily") or [], "by_key")
    hourly = mine(snapshot.get("hourly") or [], "by_key")

    balance_milli = rd.balance_milli(store, owner) if owner else 0
    return {
        "key": view_key(key),
        "owner": owner,
        "balance": rd.from_milli(balance_milli),
        "has_owner": bool(owner),
        "daily": daily,
        "hourly": hourly,
        "totals": {
            "calls": sum(int(r.get("calls", 0)) for r in daily),
            "tokens": sum(int(r.get("tokens", 0)) for r in daily),
            "credit": round(sum(float(r.get("credit", 0) or 0) for r in daily), 6),
        },
    }


def flows(store, key: dict, *, limit: int = 100) -> list:
    owner = owner_of(key)
    if not owner:
        return []
    return rd.list_flows(store, username=owner, limit=limit)


def redeem(store, key: dict, code: str) -> dict:
    """用自己的密钥兑换。

    没有 `owner` 的密钥**不允许**兑换：钱得有个主人。
    否则管理员随手建的测试密钥会把真金白银兑进一个没人认领的池子，
    而且用户那边看到的余额永远是 0（因为他查的是自己的 owner）。
    """
    owner = owner_of(key)
    if not owner:
        raise HTTPException(400, "这枚密钥还没有绑定归属，无法兑换。"
                                 "请在管理端为它设置 owner（归属用户）后再试。")
    return rd.redeem(store, code, owner)


def flows_by_uid(store, uid: str, *, limit: int = 100) -> list:
    """按 **uid** 取流水（会话模式）。

    与 `flows()` 的区别只是身份来源：那边从密钥里读 `owner`，这边直接就
    是 uid。**同一个账本、同一个键**，所以两种模式看到的是同一份数据 ——
    这正是「会话与 API Key 等价」这句话在存储层的含义。

    绝不接受 email / username 之类的标识：只有 uid 一条路，
    也就没有「传个别的标识就查到别人」的入口。
    """
    key = str(uid or "").strip()
    if not key:
        return []
    return rd.list_flows(store, username=key, limit=limit)
