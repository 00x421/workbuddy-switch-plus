"""上游分组：把账号池切成若干个互不干扰的子池，再让密钥只落在自己那一组里。

要解决的问题
------------
一个账号池里同时挂着「自己的号」「同事的号」「买了积分的号」，是很常见的形态。
但只按密钥区分额度并不够——密钥 A 的请求照样可能被调度到「同事的号」上，
对方的积分被扣掉，而账单上看起来是 A 用的。分组把这件事变成结构约束：
密钥只能落到它所属分组的账号上。

数据模型（落在 state.json）
--------------------------
  groups.{gid} = {
      name, description, enabled, created,
      routing,             # 可选：该分组的调度方式覆盖全局设置；空 = 跟随全局
      accounts: [aid, ...] # 分组内含的账号
  }
  keys.{kid}.groups = [gid, ...]   # 空 = 不限制（可路由到全部账号）

enabled 的含义
--------------
`enabled=False` —— **这个分组整个不工作**，绑了它的密钥直接路由失败。
这是「下线」。

与 manager 的差异
-----------------
manager 的「分组」是它上游网关里的多账号池概念（一个池对应一个上游实例）。
我们这里没有多上游实例，池就是本机的账号库，所以分组被定义成**账号子集 +
可选调度覆盖**，语义更窄也更好解释：组内调度、组间隔离。
"""
from __future__ import annotations

import secrets
import time

MAX_GROUPS = 32

# 允许的调度方式。与 pool.DEFAULT_POOL 的 routing 保持同一套取值，
# 但不在这里 import pool —— 那会形成 groups → pool → groups 的循环导入。
ROUTINGS = ("round_robin", "manual", "least_used")


def norm_routing(value: object) -> str:
    """归一化分组级调度方式；非法值一律落回空串（= 跟随全局）。"""
    v = str(value or "").strip().lower()
    return v if v in ROUTINGS else ""


def norm_name(value: object) -> str:
    label = str(value or "").strip()
    if not label:
        raise ValueError("分组名称不能为空")
    if len(label) > 40:
        raise ValueError("分组名称请控制在 40 字以内")
    return label


def list_groups(store) -> list[dict]:
    """分组清单，附上成员账号的显示名（界面直接用，不必再查一次账号表）。"""
    with store.lock:
        raw = store.data.setdefault("groups", {})
        names = {}
        for aid, item in store.data["accounts"].items():
            names[aid] = item.get("name") or aid
        out = []
        for gid, item in raw.items():
            accounts = [aid for aid in (item.get("accounts") or [])]
            out.append({
                "id": gid,
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "enabled": bool(item.get("enabled", True)),
                # 老数据没有这个字段。默认**允许用户使用**，与历史行为一致：
                # 升级前用户本来就能选到所有分组，升级后不该突然选不到了。
                "routing": norm_routing(item.get("routing")),
                "created": item.get("created"),
                "accounts": accounts,
                "account_names": [names.get(aid, aid) for aid in accounts],
                "account_count": len(accounts),
                # 组里已经没有有效账号了。空分组会让绑它的密钥集体 503，
                # 界面上必须能提前看出来，而不是等报错才发现。
                "empty": not any(aid in names for aid in accounts),
            })
    out.sort(key=lambda x: x.get("created") or 0)
    return out


def create_group(store, name, *, description="", accounts=None, routing="",
                 enabled=True) -> dict:
    label = norm_name(name)
    gid = secrets.token_hex(6)
    with store.lock:
        raw = store.data.setdefault("groups", {})
        if len(raw) >= MAX_GROUPS:
            raise ValueError(f"最多保存 {MAX_GROUPS} 个分组")
        if any(item.get("name") == label for item in raw.values()):
            raise ValueError(f"已存在同名分组：{label}")
        raw[gid] = {
            "name": label,
            "description": str(description or "").strip()[:200],
            "enabled": bool(enabled),
            "routing": norm_routing(routing),
            "created": int(time.time()),
            "accounts": _clean_accounts(store, accounts),
        }
        store.save()
    return {"id": gid, "name": label}


def _clean_accounts(store, accounts, keep=()) -> list[str]:
    """只保留有效的账号 id，去重且顺序稳定。

    `keep` 是「本次保存之前就已经在组里」的 id。为什么要单独留这个口子：
    本产品的「删除账号」其实是**可逆的隐藏**（`SwitchStore.hide_account` 把它从
    `data["accounts"]` 视图里摘掉，但账号库保留，解除隐藏就回来了）。如果在保存
    分组时按「不在视图里就丢掉」处理，用户只是暂停了一个号、顺手改了下分组名，
    那个号的成员资格就被永久抹掉了——而界面上完全看不出发生了什么。

    所以判据是「在视图里 **或** 原本就在组里」。真正不存在的 id（拼错的、
    早已删掉的）代价极小：它永远不会匹配到任何一行可调度账号，选号时被忽略。

    不报错而是丢弃（对 create 而言）：账号可能刚在别处被删掉，这时保存分组
    应当成功，而不是因为一个陈旧 id 整条失败。
    """
    if not accounts:
        return []
    previously = {str(x) for x in (keep or ())}
    out: list[str] = []
    for aid in accounts:
        key = str(aid)
        if (key in store.data["accounts"] or key in previously) and key not in out:
            out.append(key)
    return out


def update_group(store, gid: str, patch: dict) -> dict:
    with store.lock:
        raw = store.data.setdefault("groups", {})
        item = raw.get(gid)
        if item is None:
            raise KeyError(gid)
        if "name" in patch and patch["name"] is not None:
            label = norm_name(patch["name"])
            if any(other.get("name") == label
                   for other_id, other in raw.items() if other_id != gid):
                raise ValueError(f"已存在同名分组：{label}")
            item["name"] = label
        if "description" in patch and patch["description"] is not None:
            item["description"] = str(patch["description"]).strip()[:200]
        if "enabled" in patch:
            item["enabled"] = bool(patch["enabled"])
        if "routing" in patch:
            item["routing"] = norm_routing(patch["routing"])
        if "accounts" in patch and patch["accounts"] is not None:
            # 传 keep：被隐藏的账号不能因为一次保存就被踢出分组（见 _clean_accounts）
            item["accounts"] = _clean_accounts(store, patch["accounts"],
                                               keep=item.get("accounts"))
        store.save()
        return {"id": gid, "name": item["name"]}


def delete_group(store, gid: str) -> dict:
    """删除分组，并把绑在它上面的密钥解绑。返回被解绑的密钥数。

    密钥侧只移除这一个 gid，**不动密钥的其它分组**；移除后若密钥再无分组，
    语义回到「不限制」——这是有意的：宁可放开也不要把密钥变成不可用
    （用户删分组是为了整理，不是为了停用下游）。
    """
    with store.lock:
        raw = store.data.setdefault("groups", {})
        if gid not in raw:
            raise KeyError(gid)
        del raw[gid]
        touched = 0
        for item in store.data["keys"].values():
            groups = item.get("groups") or []
            if gid in groups:
                item["groups"] = [g for g in groups if g != gid]
                touched += 1
        store.save()
        return {"removed": 1, "unbound_keys": touched}


def norm_groups(store, values) -> list[str]:
    """归一化密钥的分组列表：去掉不存在的 gid，去重保序。"""
    if not values:
        return []
    raw = store.data.get("groups") or {}
    out: list[str] = []
    for gid in values:
        key = str(gid)
        if key in raw and key not in out:
            out.append(key)
    return out


def forget_account(store, aid: str) -> int:
    """账号被**真删**时把它从所有分组里摘掉。返回受影响的分组数。

    只在真删路径上调。可逆的「隐藏」不解绑——理由见 `_clean_accounts` 的说明。
    返回受影响的分组数，是为了让调用方能提示用户「这些分组少了一个成员」。
    """
    with store.lock:
        touched = 0
        for item in (store.data.get("groups") or {}).values():
            accounts = item.get("accounts") or []
            if aid in accounts:
                item["accounts"] = [a for a in accounts if a != aid]
                touched += 1
        if touched:
            store.save()
        return touched


def resolve(store, key) -> tuple[set[str] | None, str]:
    """算出密钥能用的账号集合与生效的调度方式。

    返回 `(allowed_ids, routing_override)`：
      · allowed_ids 为 None = 不限制（密钥没绑分组，或绑的分组都被停用）；
      · routing_override 只在「恰好一个分组生效且它配了 routing」时非空 ——
        多个分组混在一起时各自想要不同调度，没有合理答案，此时跟随全局。
    """
    gids = [g for g in ((key or {}).get("groups") or [])]
    if not gids:
        return None, ""
    with store.lock:
        raw = store.data.get("groups") or {}
        active = [raw[g] for g in gids
                  if g in raw and raw[g].get("enabled", True)]
    if not active:
        # 绑了分组但分组全被停用：这是「有意的停用」，不是「没限制」。
        # 返回空集让请求被挡住，用户才会去处理分组而不是发现流量偷偷放行了。
        return set(), ""
    allowed: set[str] = set()
    for item in active:
        allowed.update(item.get("accounts") or [])
    override = norm_routing(active[0].get("routing")) if len(active) == 1 else ""
    return allowed, override
