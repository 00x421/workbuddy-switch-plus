"""终端用户（members）管理：数据层 + 管理员接口实现。

## 这个模块管什么、不管什么

管：邮箱注册出来的「终端用户」—— 他们是用服务的人、是计费主体、
有积分余额、名下有若干 API key。

不管：`/admin/api/users`（既有端点，`admin/security.py` 里的控制台账号）。
那是登录 8787 管理后台用的 username + role，两套东西永不共用键、永不共用端点。
契约 §8.6 已经论证过：复用会直接打爆 `UsersPanel` 与现有测试。

## 存储键

- `state.json` 的 `members`：`{ "<uid>": {...} }`
- `member_index_email`：`{ "<email_lower>": "<uid>" }`

⚠️ **绝不能用 `users` 键**。那个键在 `security.py:132` 被
`setdefault("users", [])` 初始化成 **list**，装的是控制台账号；
终端用户写同一个键就是 list/dict 类型碰撞，会直接打爆
`public_users` / `add_user` / `update_user` / `delete_user` / 充值流程。

## 余额：唯一真相源是 `redemption` 的账本

不在 `members[uid]` 里存 `balance_milli`。
但 `redemption._ledger`（`redemption.py:154-157`）**已经**是 `{键: {"milli": int}}`
的账本，而且它带着一整套踩过坑的实现：原子核销（核销与入账同一把锁）、
`add_flow` 记账、`FLOW_LIMIT` 裁剪。

再开一份余额字段就是**把已经解决的问题重新引入** —— 两份余额必然漂移，
而 `redemption.grant/spend/redeem` 不会去更新 `members`。
所以这里一律 `redemption.balance_milli(store, uid)` 现算。

账本键是 `uid`：`owner` 存 uid 不存 email。
存 email 的话 `Alice@x.com` 与 `alice@x.com` 会变成两份独立余额
（`keysvc._norm_owner` 只 strip 不 lower），而用户改邮箱后旧 key 的 owner
会变成悬空指针。

## 越权（IDOR）防线

本模块只做**数据层**，鉴权在 `server.py` 的路由段里（`viewer` / `admin` /
`require_session_admin` 三档）。数据层这里保证的是：**任何按 uid 取数的函数
都只接受 uid，不接受 email 或 username**，也就没有「传个别的标识就查到别人」
的入口。
"""
from __future__ import annotations

import re
import secrets
import time

from fastapi import HTTPException

from . import redemption as rd

# ── 角色 / 状态白名单 ────────────────────────────────────────────────
ROLES = ("user", "admin")
STATUSES = ("active", "disabled")

#: 单次余额调整的上限（积分）。防「多打一个 0 就是 10 倍的钱」。
#: 沿用 `server.py:1756` 充值接口的同一个口径。
MAX_ADJUST_CREDIT = 1_000_000

#: 列表分页：默认 20，封顶 200。
#: 封顶是必须的 —— 否则 `?size=100000` 就是一个「绕过搜索的全量导出」入口。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200
#: 一次最多能筛出多少个 uid 再排序分页。10000 是「比任何人手建的都多」的量级，
#: 真到这个数说明该上真正的索引了，而不是让单次请求把内存吃光。
SCAN_LIMIT = 10000

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ensure(store) -> None:
    """惰性初始化。老 `state.json` 没有这几个键 → 不需要迁移脚本。

    这里只建**全新键**：`members` / `member_index_email`。
    `users` 键是既有的、类型为 list，这里绝不碰。
    已经被别的地方初始化成别的类型时**不覆盖**（宁可让调用方读到一个
    非 dict 时报错，也好过把数据冲掉）。
    """
    with store.lock:
        for key in ("members", "member_index_email"):
            if not isinstance(store.data.get(key), dict):
                store.data[key] = {}


def members(store) -> dict:
    """成员表。**惰性**——调用方不必先 `ensure()`。"""
    row = store.data.get("members")
    return row if isinstance(row, dict) else {}


def index(store) -> dict:
    """邮箱 → uid 索引。键是 `email.strip().lower()`。"""
    row = store.data.get("member_index_email")
    return row if isinstance(row, dict) else {}


def norm_email(value) -> str:
    """邮箱归一化：唯一性与索引都按这个值判定。"""
    return str(value or "").strip().lower()


def valid_email(value) -> bool:
    text = norm_email(value)
    return bool(text) and len(text) <= 254 and bool(EMAIL_RE.match(text))


def by_email(store, email) -> dict | None:
    """按邮箱查成员。**索引与主表互相兜底**。

    只查索引是不够的：索引是派生的，任何一次「只写主表没写索引」都会让它
    静默漏人，而漏人的表现是「这个邮箱可以重复注册」—— 等到发现时已经有两个
    账号在抢同一个邮箱了。所以索引里查不到时**回扫主表**（成员是人工量级，
    几百条的线性扫描比一次静默的重复注册便宜得多），并顺手**自愈索引**。
    """
    want = norm_email(email)
    if not want:
        return None
    uid = index(store).get(want)
    if uid:
        row = members(store).get(uid)
        if row is not None:
            return row
        # 悬空索引：成员已删但索引没跟着删。清掉它，否则这个邮箱永远注册不了。
        index(store).pop(want, None)
    for uid_, row in members(store).items():
        if norm_email(row.get("email")) == want:
            index(store)[want] = uid_          # 自愈
            return row
    return None


def by_id(store, uid) -> dict | None:
    return members(store).get(str(uid or ""))


def resolve_owner(store, owner) -> dict | None:
    """把一个 `owner` 字段解析成成员。返回 `None` = 解析不出来（孤儿 owner）。

    `owner` 按契约 §7 **一律存 uid**。但真实的 `state.json` 里可能残留
    历史值（手敲的、老版本写的），所以这里做**受限的兼容**：

    1. 先当 uid 精确查；
    2. 查不到且长得像邮箱 → 当邮箱查（老数据是 email 的能对上）。

    绝不做「模糊匹配 / 前缀匹配 / 大小写猜测」—— 那正是「A 的密钥把钱记到
    B 头上」的入口。对不上就是 `None`，由调用方按「无主」处理（余额 0、禁止兑换）。
    """
    text = str(owner or "").strip()
    if not text:
        return None
    row = by_id(store, text)
    if row is not None:
        return row
    if "@" in text:
        return by_email(store, text)
    return None


def key_ids_of(store, uid) -> list[str]:
    """反查某成员名下的 key id。

    按需反查，不存 `api_key_ids` 缓存字段。
    契约 §1 的字段表里有这一项（还标了「可缓存」），但冗余真相源必然漂移：
    `keysvc.create_key` / `update_key` / `delete_key` 都不会去维护它，
    于是「用户详情里显示 3 枚密钥、实际名下有 4 枚」。

    key 有 100 枚的硬上限（`keysvc.py:185`），反查是几十次字典查询，不值得为它
    引入一个会漂移的字段。这里用 `str()` 比较：state.json 是人工可编辑的，
    `owner` 万一是数字也得能对上。
    """
    want = str(uid or "")
    with store.lock:
        rows = store.data.get("keys") or {}
        return [kid for kid, item in rows.items()
                if want and str(item.get("owner") or "").strip() == want]


def _key_summary(store) -> dict[str, dict]:
    """`{owner 原值: {"count": n, "names": [...]}}`。

    注意键是 **owner 原值**：真实 `state.json` 里 `owner` 可能是一个已经对不上
    任何成员的悬空值。那种 key 正是要**标红**的（它在跑、在消耗上游，但余额没人
    认领、也不能兑换），所以这里必须按 owner 原值分组，不能先解析成 uid 再分组 ——
    解析不出来的会被静默丢掉，而它们恰恰是最需要被看见的那批。
    """
    out: dict[str, dict] = {}
    with store.lock:
        rows = list((store.data.get("keys") or {}).items())
    for _kid, item in rows:
        owner = str(item.get("owner") or "").strip()
        bucket = out.setdefault(owner, {"count": 0, "names": []})
        bucket["count"] += 1
        if len(bucket["names"]) < 5:
            bucket["names"].append(str(item.get("name") or ""))
    return out


def view(store, member: dict) -> dict:
    """成员的管理端视图。

    绝对不出现：`password_hash`、任何形式的盐、会话 sid、key 的 secret/hash。
    这里用**白名单构造**而不是「拷贝后 del 敏感键」—— 后者会在有人往存储里
    加一个新敏感字段时静默泄露。白名单的失败模式是「少显示一个字段」，
    那只是难看；黑名单的失败模式是「泄露凭据」。
    """
    uid = str(member.get("id") or "")
    keys = _key_summary(store)
    own = keys.get(uid) or {"count": 0, "names": []}
    milli = rd.balance_milli(store, uid) if uid else 0
    return {
        "id": uid,
        "email": member.get("email", ""),
        "role": member.get("role", "user"),
        "status": member.get("status", "active"),
        "email_verified": bool(member.get("email_verified", False)),
        "balance_milli": milli,
        "balance_credit": rd.from_milli(milli),
        "created_at": member.get("created_at"),
        "last_login_at": member.get("last_login_at"),
        "key_count": int(own["count"]),
        "key_names": list(own["names"]),
    }


def _orphans(store) -> list[dict]:
    """孤儿 key：owner 非空但解析不出成员。

    它们能正常调 API（`keysvc` 不校验 owner），却把钱记进一个没人认领的桶，
    也**不能兑换**（`portal.py:115-120`）。管理员看不到它们，
    于是「这个用户的余额怎么一直不动」永远查不出原因。
    """
    out = []
    for owner, bucket in _key_summary(store).items():
        if not owner:
            continue
        if resolve_owner(store, owner) is None:
            out.append({"owner": owner, "count": bucket["count"],
                        "names": list(bucket["names"])})
    return out


def list_members(store, *, q: str = "", page: int = 1,
                 size: int = DEFAULT_PAGE_SIZE) -> dict:
    """列表 + 分页 + 搜索。

    搜索用的**排序键**是：

    ```python
    sort_key = (0 if q else 1, -created_at, uid)
    ```

    这里有个**必须解释清楚的点**：契约 §5.2 明确要求「搜索」，所以命中搜索词的
    成员**一定要出现在结果里**。但真实用户表里信息是不全的 ——
    `created_at` 可能是 `None`（手写进 state.json 的条目、或老版本建的）。

    若按「`None` 排最后 + 全局先截断再过滤」，一个匹配了搜索词但没有 created_at
    的成员会被**静默丢掉**，管理员看到的结果是「搜了等于没搜」。
    所以命中搜索的成员排在**前面**（第 0 位是 0），不命中的排在后面，
    两类内部再按创建时间倒序。这样：

    - 搜索准确（命中的一定在，且在最前面）；
    - 不搜索时就是纯粹的「最新在前」，没有截断偏差。
    """
    text = str(q or "").strip().lower()
    rows = list(members(store).values())
    if text:
        rows = [r for r in rows
                if text in str(r.get("email", "")).lower()
                or text in str(r.get("id", "")).lower()]
    rows.sort(key=lambda r: (0 if text else 1,
                             -int(r.get("created_at") or 0),
                             str(r.get("id") or "")))
    scanned = len(rows)
    truncated = scanned > SCAN_LIMIT
    rows = rows[:SCAN_LIMIT]
    total = len(rows)

    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = max(1, min(MAX_PAGE_SIZE, size))

    start = (page - 1) * size
    window = rows[start:start + size]
    # 分页之后再算余额：余额要读账本，全表算一遍没必要。
    return {
        "items": [view(store, r) for r in window],
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
        "truncated": truncated,
        "scanned": scanned,
        "orphan_keys": _orphans(store),
    }


def create(store, *, email: str, password: str, role: str = "user",
           email_verified: bool = False) -> dict:
    """建一个成员。管理员分发账号走这里。

    邮箱查重必须在锁内完成。 先查后写（check-then-act）在并发下会放进
    两个同邮箱账号 —— 而「同一邮箱两个账号」直接毁掉「邮箱唯一」这个前提，
    后面所有按 email 反查用户的地方都会随机挑一个。
    """
    if not valid_email(email):
        raise HTTPException(400, "请输入正确的邮箱地址")
    if not isinstance(password, str) or len(password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if role not in ROLES:
        raise HTTPException(400, "角色无效")
    text = str(email).strip()
    key = norm_email(text)

    with store.lock:
        ensure(store)
        if by_email(store, key) is not None:
            raise HTTPException(400, "该邮箱已注册，请直接登录")
        uid = secrets.token_hex(8)
        # 理论上撞不上（64 bit），但撞上就是「两个用户互相覆盖」，
        # 不是「概率极小可以忽略」。
        while uid in members(store):
            uid = secrets.token_hex(8)
        row = {
            "id": uid,
            "email": text,
            "password_hash": hash_password(password),
            "role": role,
            "status": "active",
            "email_verified": bool(email_verified),
            "created_at": int(time.time()),
            "last_login_at": None,
        }
        members(store)[uid] = row
        index(store)[key] = uid
    store.save()
    return row


def hash_password(password: str) -> str:
    """口令哈希。

    优先用 `accounts`（t2）的实现 —— 用户侧注册与管理员分发账号必须**同一种**
    哈希，否则会出现「管理员建的账号登录不了」或者反过来。
    `accounts` 还没落地（t2 未完成）时回退到 `security.make_hash`，
    两者都是 pbkdf2_sha256，格式兼容。
    """
    try:
        from . import accounts as accounts_mod
        maker = (getattr(accounts_mod, "hash_password", None)
                 or getattr(accounts_mod, "make_hash", None))
        if callable(maker):
            return maker(password)
    except Exception:  # noqa: BLE001  # accounts 不存在 / 未就绪都走回退
        pass
    from .security import make_hash
    return make_hash(password)


def update(store, uid: str, patch: dict, *, actor: dict | None = None) -> dict:
    """改角色 / 启停。返回更新后的成员行。

    两个不变量：

    - `role="admin"` 的成员只剩一个时，**不能降级**（否则没人能管后台了）；
    - 成员**不能把自己降级/禁用**（否则一次误点就自锁，只能改 state.json 救）。

    改角色 / 禁用后**必须吊销它的全部会话** —— 由调用方做
    （`server.py` 里的路由段调 `accounts.revoke_member_sessions`），
    因为「降级之后攻击者还挂着」正是本项目在管理员侧栽过的坑。
    """
    patch = patch or {}
    with store.lock:
        row = members(store).get(str(uid))
        if row is None:
            raise HTTPException(404, "用户不存在")
        changed = False

        if "role" in patch and patch["role"] is not None:
            role = str(patch["role"]).strip()
            if role not in ROLES:
                raise HTTPException(400, "角色无效")
            if role != row.get("role"):
                if row.get("role") == "admin" and _admin_count(store) <= 1:
                    raise HTTPException(400, "至少保留一个管理员")
                if _same_actor(actor, row):
                    raise HTTPException(400, "不能修改自己的角色")
                row["role"] = role
                changed = True

        if "status" in patch and patch["status"] is not None:
            status = str(patch["status"]).strip()
            if status not in STATUSES:
                raise HTTPException(400, "状态无效")
            if status != row.get("status"):
                if _same_actor(actor, row):
                    raise HTTPException(400, "不能禁用自己的账号")
                row["status"] = status
                changed = True

        if changed:
            row["updated_at"] = int(time.time())
    if changed:
        store.save()
    return row


def _admin_count(store) -> int:
    return sum(1 for r in members(store).values() if r.get("role") == "admin")


def _same_actor(actor: dict | None, row: dict) -> bool:
    """当前操作者是不是这个成员本人。

    这里**故意只认 `actor["member_id"]`**。控制台账号（`actor["username"]`）与
    终端用户是两套完全不同的命名空间：控制台里恰好有个叫 `admin` 的账号，
    不代表终端用户里 uid 是 `admin` 的那个人「是自己」。
    拿 username 去比 uid 是**碰巧实现出来的越权**，不能这么写。

    `member_id` 由 `server.py` 从管理员会话里读（管理员自己也是终端用户时才有），
    没有就是 `None`，此时不做「自己」判定 —— 保守方向是「宁可不让管理员改自己」，
    而不是「误判成自己然后放行」。
    """
    uid = str((actor or {}).get("member_id") or "").strip()
    return bool(uid) and uid == str(row.get("id") or "")


def unbind_keys(store, uid: str) -> int:
    """把这个成员名下所有 key 的 `owner` 置空。返回改了几枚。

    不删 key、不停用 key。 三条理由：

    1. `keysvc.delete_key` 有「最后一枚密钥不能删」的硬规则
       （`keysvc.py:230-231`），连带删除会在这条规则上顶回来，
       管理员看到的是一个看不懂的 400；
    2. 这些 key 可能正在线上跑，删/停用等于让一次「删账号」变成
       「线上调用突然全挂」；
    3. key 归 `keysvc` 管，`members` 层不越权删别的东西。

    置空之后它是**无主 key**：还能调 API，但余额归零、不能兑换
    （`portal.py:115-120` 的既有行为）。这是**有意的**取舍，
    删除确认里也会告诉管理员「该用户名下有 N 枚密钥，删除后将成为无主密钥」。
    """
    with store.lock:
        rows = store.data.get("keys") or {}
        hit = 0
        for _kid, item in rows.items():
            if str(item.get("owner") or "").strip() == str(uid):
                item["owner"] = ""
                hit += 1
    if hit:
        store.save()
    return hit


def delete(store, uid: str, *, force: bool = False,
           actor: dict | None = None) -> dict:
    """删成员。

    顺序**必须是**：余额检查 → 解绑 key → 删 member → 落审计（最后一步在路由里）。

    余额非 0 时默认 409：删号即等于把钱吞掉，而管理员完全无从察觉。
    `force=true` 时记一笔 **负数的 `member_deleted` 流水**再清账本键 ——
    负流水是「账对不平」的护栏：删号后 `sum(flows)` 仍然等于 `sum(ledger)`（= 0），
    不会凭空多出一笔流向不明的余额。
    """
    with store.lock:
        row = members(store).get(str(uid))
        if row is None:
            raise HTTPException(404, "用户不存在")
        if _same_actor(actor, row):
            raise HTTPException(400, "不能删除自己的账号")
        if row.get("role") == "admin" and _admin_count(store) <= 1:
            raise HTTPException(400, "至少保留一个管理员")

    milli = rd.balance_milli(store, uid)
    if milli and not force:
        raise HTTPException(
            409,
            f"该用户还有 {rd.from_milli(milli):g} 积分余额，请先清零或使用强制删除")

    keys_unbound = unbind_keys(store, uid)
    email = norm_email(row.get("email"))

    if milli:
        # force 路径：先记结算流水（此时账本还是原值，add_flow 里的
        # balance 读出来才是「删号前的余额」），再把账本归零。
        _settle(store, uid, milli)

    with store.lock:
        members(store).pop(str(uid), None)
        ind = index(store)
        # 索引只有在这条映射确实指向本 uid 时才删 —— 否则会把别人的邮箱解绑掉。
        if email and ind.get(email) == str(uid):
            ind.pop(email, None)
    store.save()
    return {"ok": True, "id": str(uid), "email": row.get("email", ""),
            "keys_unbound": keys_unbound, "released_credit": rd.from_milli(milli)}


def _settle(store, uid: str, milli: int) -> None:
    """强制删除前把余额结清：记一笔负流水，并把账本键归零。"""
    with store.lock:
        rd.add_flow(store, uid, "member_deleted", -int(milli),
                    "管理员删除用户，余额结清")
        ledger = store.data.setdefault("balances", {})
        row = ledger.get(str(uid))
        if isinstance(row, dict):
            row["milli"] = 0
            row["updated_at"] = int(time.time())
    store.save()


def adjust_balance(store, uid: str, delta_credit, reason: str = "",
                   *, actor: str = "") -> dict:
    """调整余额。**必写流水。**

    几个刻意的选择：

    1. **只做增量 `delta`，不做「设为 N」。** 契约 §5.2 就是 `delta_credit`。
       设值语义在并发下会吞掉别人的充值：A 读到 100 → 想设 50 → B 充了 20 →
       A 写入 50，那 20 没了。增量走 `redemption.grant/spend`，它们全程持锁。
    2. **金额一律走 `redemption.to_milli`**（Decimal 舍入）。
       绝不用 `int(credit * 1000)` —— `redemption.py:37-59` 的长注释讲了
       为什么：约 3.3% 的输入会差 1 厘，然后账对不平。
    3. **上限防手滑**：多打一个 0 就是 10 倍的钱。
    4. **允许扣成 0，不允许扣成负数**：`redemption.spend` 余额不足报 402。
       这是既有行为，保留 —— 允许负数会让服务在用户不知情时累积债务。
    """
    row = by_id(store, uid)
    if row is None:
        raise HTTPException(404, "用户不存在")

    try:
        credit = float(delta_credit)
    except (TypeError, ValueError):
        raise HTTPException(400, "请填写正确的积分金额")
    if credit != credit or credit in (float("inf"), float("-inf")):
        raise HTTPException(400, "请填写正确的积分金额")
    if credit == 0:
        raise HTTPException(400, "调整金额不能为 0")
    if abs(credit) > MAX_ADJUST_CREDIT:
        raise HTTPException(400, f"调整金额需在 ±{MAX_ADJUST_CREDIT:,} 积分以内")

    # 先按「厘」算出来，确保不小于一个可表达的最小单位 ——
    # 0.0001 积分会被 to_milli 归成 0 厘，grant/spend 会当成「0 元」静默放行，
    # 而管理界面却会显示「已调整」。
    milli = rd.to_milli(credit)
    if milli == 0:
        raise HTTPException(400, "调整金额太小，最小单位为 0.001 积分")

    note = str(reason or "").strip()[:120] or ("管理员加余额" if milli > 0 else "管理员扣余额")
    if milli > 0:
        result = rd.grant(store, uid, credit, kind="admin_adjust",
                          note=note, actor=actor)
    else:
        result = rd.spend(store, uid, -credit, note=note)
    return {
        "id": str(uid),
        "balance_milli": int(result.get("milli", 0)),
        "balance_credit": rd.from_milli(result.get("milli", 0)),
        "delta_credit": credit,
    }


def _member_keys(store, uid: str) -> list[dict]:
    """名下 key 的**精简**列表。绝不回显 `secret` / `hash`。"""
    want = str(uid or "")
    with store.lock:
        rows = list((store.data.get("keys") or {}).items())
    out = []
    for kid, item in rows:
        if not want or str(item.get("owner") or "").strip() != want:
            continue
        out.append({
            "id": kid,
            "name": item.get("name", ""),
            "prefix": item.get("prefix", ""),
            "enabled": bool(item.get("enabled", True)),
            "created": item.get("created"),
            "last_used_at": item.get("last_used_at"),
        })
    out.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return out


def detail(store, uid: str, *, flow_limit: int = 50) -> dict:
    """详情 = 视图 + 名下 key + 最近流水。"""
    row = by_id(store, uid)
    if row is None:
        raise HTTPException(404, "用户不存在")
    out = view(store, row)
    out["keys"] = _member_keys(store, uid)
    try:
        limit = max(1, min(200, int(flow_limit)))
    except (TypeError, ValueError):
        limit = 50
    out["flows"] = rd.list_flows(store, username=str(uid), limit=limit)
    return out


def stats(store) -> dict:
    """给「用户管理」页头部的总览。"""
    with store.lock:
        rows = list(members(store).values())
    total = len(rows)
    disabled = sum(1 for r in rows if r.get("status") == "disabled")
    admins = sum(1 for r in rows if r.get("role") == "admin")
    # 余额是逐用户读账本；成员是人工量级（几百），一次线性扫描可以接受。
    # 真到上万就该改成增量统计了 —— list_members 里的 truncated 标志已经在
    # 提示这件事。
    balance_total = sum(rd.balance_milli(store, str(r.get("id") or "")) for r in rows)
    return {
        "total": total,
        "active": total - disabled,
        "disabled": disabled,
        "admins": admins,
        "balance_milli": balance_total,
        "balance_credit": rd.from_milli(balance_total),
        "keys_total": len(store.data.get("keys") or {}),
        "orphan_keys": len(_orphans(store)),
    }


def assert_owner(store, owner) -> str:
    """校验一个 `owner` 是否可用，返回归一化后的 uid。对不上就 400。

    契约 §7 要求「创建/修改 API key 时校验该 uid 确实存在」。
    `owner` 为空是**允许**的（无主 key 是既有语义，`portal.py` 已处理）。

    非空 owner **必须命中一个成员**，否则就是「钱记进没人认领的桶」
    —— 那正是 t1 的 BLOCKER-2 要堵的洞。
    """
    text = str(owner or "").strip()
    if not text:
        return ""
    row = resolve_owner(store, text)
    if row is None:
        raise HTTPException(400, "归属用户不存在，请先在「用户管理」里创建该用户")
    return str(row.get("id") or "")
