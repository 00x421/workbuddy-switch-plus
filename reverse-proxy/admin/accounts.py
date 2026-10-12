"""终端用户账号：注册 / 登录 / 会话 / 邮箱验证码 / 余额。

这套东西和 `admin/security.py` 里的「控制台账号」是两回事。 后者是
`state.json` 的 `users` 键（一个 list，登录 8787 管理后台用的 username+role），
前者是 `members` 键（dict，邮箱注册、有积分余额、是 API 调用的计费主体）。
两套永不共用键名 —— 详见契约 §1 与 §8.6。

设计要点（改这个文件前先读完）：

1. **`users` 键绝对不要碰。** 它是 list，`setdefault("users", {})` 就是类型碰撞，
   会直接打爆 security.py 的四个函数与充值流程。

2. **owner 一律存 uid，不存 email。** email 可变、有大小写归一问题；
   uid 不可变。账本键（`redemption.balance_milli`）也走 uid，
   否则 `Alice@x.com` 与 `alice@x.com` 会变成两份独立的余额。

3. **登录失败在「文案 + 状态码 + 耗时」三个维度上都不能区分「邮箱不存在」与
   「密码错误」。** 前两个是常识，第三个最容易漏：邮箱不存在时直接返回会省下
   一次 PBKDF2（约 0.069 秒），这个差距足以枚举出哪些邮箱注册过。
   所以不存在时也要跑一次等价的 PBKDF2（对固定假哈希）。

4. **验证码用带盐的 PBKDF2，不用无盐 SHA-256。** 6 位数字只有 10^6 空间，
   无盐摘要拿到就能秒破。次数限制只防在线爆破，防不了离线。
   代价是每次校验一次 PBKDF2（约 0.07 秒）—— 可接受，且天然给验证码接口限了速。

5. **禁用 / 改密 / 删号必须立刻踢会话。** 否则「禁用」要等 12 小时 TTL 才生效，
   而用户看到的是「已经禁用了但那个人还在用」。

6. **明文口令绝不落盘、绝不进日志、绝不出现在任何响应里。**
   连「口令格式错误」这种报错都不能回显用户输入。
"""
from __future__ import annotations

import re
import secrets
import logging
import time

from fastapi import HTTPException, Request

from . import mailer, redemption, settings_store
from .iputil import client_ip
from .security import SESSION_TTL, digest, make_hash, verify_pwd

# ── 存储键（全部惰性 setdefault，老 state.json 不需要迁移）──────────
MEMBERS = "members"              # {uid: {...}}
INDEX_EMAIL = "member_index_email"   # {email_lower: uid}
SESSIONS = "member_sessions"     # {sid_digest: {...}}；进程重启后仍有效
CODES = "email_codes"            # {email_lower: {...}}

logger = logging.getLogger(__name__)

# cookie 名。与管理员 cookie 分开 —— 共用会让「退出登录」两边互相踢。
COOKIE = "workbuddy_user"
COOKIE_PATH = "/"

# 验证码参数
CODE_TTL = 600                   # 10 分钟
CODE_MAX_TRIES = 5
CODE_RESEND_SECONDS = 60
CODE_IP_HOURLY = 10
CODE_EMAIL_HOURLY = 5

# 口令长度上限。不加限制时，一次 1 MB 的口令（body 上限）会让 PBKDF2 卡很久，
# 成为一个廉价的 DoS 入口。
PASSWORD_MAX = 128

# 登录失败锁定（与 security.py 同口径：第 9 次起锁 10 分钟）
MAX_FAILS = 8
LOCK_SECONDS = 600
MAX_TRACKED = 5000

BALANCE_REASON_LIMIT = 120

# 邮箱格式。刻意保持宽松（不追求 RFC 5322 完备），但必须挡掉空格与多 @。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# 「邮箱不存在」时用来拉平耗时的固定假哈希。sink 只是防止解释器
# 把整个调用优化掉（在当前 CPython 里不会，但不要依赖这个巧合）。
_DUMMY_HASH = make_hash("wb-accounts-dummy-password", iterations=260000)
_sink = 0


# ── 小工具 ──────────────────────────────────────────────────────────
def now() -> int:
    return int(time.time())


def norm_email(value) -> str:
    """邮箱归一化：判重、建索引、查表一律用它。"""
    return str(value or "").strip().lower()


def valid_email(value) -> bool:
    text = str(value or "").strip()
    return bool(text) and len(text) <= 254 and bool(_EMAIL_RE.match(text))


def check_password(password) -> str:
    """口令强度校验。**报错里绝不回显用户输入。**"""
    if not isinstance(password, str) or len(password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if len(password) > PASSWORD_MAX:
        raise HTTPException(400, f"密码最长 {PASSWORD_MAX} 位")
    return password


def ensure(store) -> None:
    """惰性初始化四个新键。**注意不要碰 `users`。**"""
    with store.lock:
        store.data.setdefault(MEMBERS, {})
        store.data.setdefault(INDEX_EMAIL, {})
        store.data.setdefault(SESSIONS, {})
        store.data.setdefault(CODES, {})


def _members(store) -> dict:
    return store.data.setdefault(MEMBERS, {})


def _index(store) -> dict:
    return store.data.setdefault(INDEX_EMAIL, {})


def _sessions(store) -> dict:
    return store.data.setdefault(SESSIONS, {})


def _codes(store) -> dict:
    return store.data.setdefault(CODES, {})


def balance_milli(store, uid: str) -> int:
    """用户的余额（厘）。**唯一真相源是 redemption 账本**（键 = uid）。

    不在 `members[uid]` 里另存一份：`redemption` 那套（原子核销、并发锁、
    `add_flow` 记账、流水裁剪）是踩过坑的成熟实现，再开一份余额字段就是
    把已解决的问题重新引入 —— 而且两份必然漂移。
    """
    if not uid:
        return 0
    return redemption.balance_milli(store, uid)


def balance_credit(store, uid: str) -> float:
    return redemption.from_milli(balance_milli(store, uid))


def adjust_balance(store, uid: str, delta_credit, *, reason: str = "",
                   actor: str = "", kind: str = "adjust") -> dict:
    """管理员调余额：**必须写流水**，这是契约 §5.2 的硬要求。

    走 `redemption.grant` / `redemption.spend` —— 它们是唯一的加钱/扣钱入口。
    扣成负数由 `spend` 用 402 拒绝（不允许用户不知不觉欠债）。
    """
    if not find(store, uid):
        raise HTTPException(404, "用户不存在")
    try:
        delta = float(delta_credit)
    except (TypeError, ValueError):
        raise HTTPException(400, "请填写正确的积分金额")
    # 上限防手滑：多打一个 0 就是 10 倍的钱（与 server.py 的充值口径一致）。
    if abs(delta) > 1_000_000:
        raise HTTPException(400, "调整金额需在 -1,000,000 到 1,000,000 积分之间")
    if delta == 0:
        raise HTTPException(400, "调整金额不能为 0")
    note = str(reason or "").strip()[:BALANCE_REASON_LIMIT] or "管理员调整"
    if delta > 0:
        result = redemption.grant(store, uid, delta, kind=kind, note=note, actor=actor)
    else:
        result = redemption.spend(store, uid, -delta, note=note)
    return {**result, "balance_milli": balance_milli(store, uid),
            "balance_credit": balance_credit(store, uid)}


# ── 序列化 ──────────────────────────────────────────────────────────
def public_user(store, row: dict) -> dict:
    """对外的用户视图。**绝不包含 password_hash / 盐 / 会话 sid。**"""
    if not row:
        return {}
    uid = str(row.get("id") or "")
    return {
        "id": uid,
        "email": str(row.get("email") or ""),
        "role": str(row.get("role") or "user"),
        "status": str(row.get("status") or "active"),
        "email_verified": bool(row.get("email_verified")),
        "balance_credit": balance_credit(store, uid),
        "balance_milli": balance_milli(store, uid),
        "created_at": row.get("created_at"),
        "last_login_at": row.get("last_login_at"),
        "api_key_ids": list(row.get("api_key_ids") or []),
    }


# ── 查询 ────────────────────────────────────────────────────────────
def find(store, uid: str) -> dict | None:
    if not uid:
        return None
    with store.lock:
        row = _members(store).get(str(uid))
        return dict(row) if isinstance(row, dict) else None


def find_by_email(store, email: str) -> dict | None:
    """按邮箱找用户。索引是唯一真相源 —— 找不到就是没有。"""
    key = norm_email(email)
    if not key:
        return None
    with store.lock:
        uid = _index(store).get(key)
        row = _members(store).get(uid) if uid else None
        return dict(row) if isinstance(row, dict) else None


def list_members(store, *, q: str = "", page: int = 1, size: int = 20) -> dict:
    """分页 + 邮箱搜索。`size` 封顶 200，避免有人拿它做全量导出。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = max(1, min(200, int(size)))
    except (TypeError, ValueError):
        size = 20
    needle = norm_email(q)
    with store.lock:
        rows = [dict(v) for v in _members(store).values() if isinstance(v, dict)]
    if needle:
        rows = [r for r in rows if needle in norm_email(r.get("email"))]
    rows.sort(key=lambda r: int(r.get("created_at") or 0), reverse=True)
    total = len(rows)
    start = (page - 1) * size
    page_rows = rows[start:start + size]
    return {
        "items": [public_user(store, r) for r in page_rows],
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
    }


def count_admins(store) -> int:
    with store.lock:
        return sum(1 for v in _members(store).values()
                   if isinstance(v, dict) and v.get("role") == "admin")


def owner_is_valid(store, uid) -> bool:
    """`owner` 校验：空串合法（= 无归属），非空必须命中一个真实用户。

    只有让 owner 的取值域收敛到 uid，账本才不会有「两个键对应一个人」的问题。
    """
    text = str(uid or "").strip()
    if not text:
        return True
    return find(store, text) is not None


def email_taken(store, email: str) -> bool:
    return find_by_email(store, email) is not None


def _hash_password(password: str) -> str:
    return make_hash(password)


def create_member(store, email: str, password: str, *, role: str = "user",
                  verified: bool = False, register_mode: str = "") -> dict:
    """建用户。**查重与写入必须在同一把锁里** —— 否则并发两次同邮箱注册
    会产生两个 uid 抢同一个邮箱索引，后写的那个用户永远登不进去。
    """
    check_password(password)
    if not valid_email(email):
        raise HTTPException(400, "请输入正确的邮箱地址")
    clean = str(email).strip()
    key = norm_email(clean)
    uid = secrets.token_hex(8)
    stamp = now()
    entry = {
        "id": uid,
        "email": clean,
        "password_hash": _hash_password(password),
        "role": role if role in ("user", "admin") else "user",
        "status": "active",
        "email_verified": bool(verified),
        "created_at": stamp,
        "last_login_at": None,
        "register_mode": str(register_mode or ""),
    }
    with store.lock:
        index = _index(store)
        if key in index:
            raise HTTPException(400, "该邮箱已注册，请直接登录")
        _members(store)[uid] = entry
        index[key] = uid
        store.save()
    return dict(entry)


def set_password(store, uid: str, password: str) -> None:
    check_password(password)
    with store.lock:
        row = _members(store).get(str(uid))
        if not isinstance(row, dict):
            raise HTTPException(404, "用户不存在")
        row["password_hash"] = _hash_password(password)
        store.save()
    revoke_sessions(store, uid)


def update_member(store, uid: str, patch: dict, *, actor_id: str = "") -> dict:
    """改角色 / 改状态（管理端 PATCH）。

    两条硬规则（与 `security.update_user` 同口径）：
      · 不能把最后一个管理员降级；
      · 不能对自己执行禁用 / 降级。
    改成功之后**立刻吊销该用户全部会话** —— 否则「禁用」要等 12 小时才生效。
    """
    changed: list[str] = []
    with store.lock:
        row = _members(store).get(str(uid))
        if not isinstance(row, dict):
            raise HTTPException(404, "用户不存在")
        if patch.get("role") is not None:
            role = str(patch["role"])
            if role not in ("user", "admin"):
                raise HTTPException(400, "角色无效（仅支持 管理员 / 普通用户）")
            if role != row.get("role"):
                if role == "user" and row.get("role") == "admin" and count_admins(store) <= 1:
                    raise HTTPException(400, "至少保留一个管理员")
                if str(uid) == str(actor_id):
                    raise HTTPException(400, "不能对自己执行该操作")
                row["role"] = role
                changed.append("role")
        if patch.get("status") is not None:
            status = str(patch["status"])
            if status not in ("active", "disabled"):
                raise HTTPException(400, "状态无效")
            if status != row.get("status"):
                if status == "disabled" and row.get("role") == "admin" and count_admins(store) <= 1:
                    raise HTTPException(400, "至少保留一个管理员")
                if str(uid) == str(actor_id):
                    raise HTTPException(400, "不能对自己执行该操作")
                row["status"] = status
                changed.append("status")
        if changed:
            store.save()
        result = dict(row)
    if changed:
        revoke_sessions(store, uid)
    return {"user": public_user(store, result), "changed": changed}


def delete_member(store, uid: str, *, actor_id: str = "") -> dict:
    """删用户。**语义写死为**（见契约 BLOCKER-4）：

      · 余额非 0 → 409，要求先清零（钱不能凭空消失）；
      · 不能删最后一个管理员、不能删自己；
      · **不删它名下的 API Key**（key 归 keysvc 管，members 层不越权），
        只把 owner 置空 → 那枚 key 变成「无归属」，仍能调用上游，
        但**不能兑换**（门户会给 400「还没有绑定归属」）。
        这样绝不会出现「新用户继承了前任的余额」；
      · 立刻吊销其全部会话、清掉邮箱索引与验证码。
    """
    with store.lock:
        row = _members(store).get(str(uid))
        if not isinstance(row, dict):
            raise HTTPException(404, "用户不存在")
        if row.get("role") == "admin" and count_admins(store) <= 1:
            raise HTTPException(400, "至少保留一个管理员")
        if str(uid) == str(actor_id):
            raise HTTPException(400, "不能删除当前登录用户")
        milli = balance_milli(store, uid)
        if milli != 0:
            raise HTTPException(409, f"该用户还有 {redemption.from_milli(milli):g} 积分余额，请先清零")
        _members(store).pop(str(uid), None)
        key = norm_email(row.get("email"))
        if _index(store).get(key) == str(uid):
            _index(store).pop(key, None)
        _codes(store).pop(key, None)
        _sessions(store)  # 确保键存在
        store.save()
    revoked = revoke_sessions(store, uid)
    detached = detach_keys(store, uid)
    return {"ok": True, "sessions_revoked": revoked, "keys_detached": detached}


def detach_keys(store, uid: str) -> list:
    """把用户名下所有 API Key 的 owner 置空，并同步 `api_key_ids` 缓存。

    不删 key。「最后一枚密钥不能删」这条规则归 keysvc 独有，
    在这里再实现一遍会让两套规则打架（删用户时被 keysvc 顶回 400）。
    """
    detached: list = []
    with store.lock:
        for kid, item in (store.data.get("keys") or {}).items():
            if isinstance(item, dict) and str(item.get("owner") or "").strip() == str(uid):
                item["owner"] = ""
                detached.append(kid)
        store.save()
    return detached


def refresh_key_ids(store, uid: str) -> list:
    """重算 `api_key_ids` 缓存（由 owner 反查，唯一真相源仍是 keys 表）。"""
    with store.lock:
        ids = sorted(kid for kid, item in (store.data.get("keys") or {}).items()
                     if isinstance(item, dict)
                     and str(item.get("owner") or "").strip() == str(uid))
        row = _members(store).get(str(uid))
        if isinstance(row, dict):
            row["api_key_ids"] = ids
        return ids


# ── 会话 ────────────────────────────────────────────────────────────
def _prune_sessions(store) -> None:
    """惰性清理过期会话。调用方须已持锁。"""
    stamp = time.time()
    sessions = _sessions(store)
    for key in [k for k, v in sessions.items()
                if not isinstance(v, dict) or float(v.get("expires") or 0) <= stamp]:
        sessions.pop(key, None)


def start_session(store, uid: str) -> str:
    """建会话，返回**明文 sid**（只在这一刻存在于内存里）。"""
    sid = secrets.token_urlsafe(32)
    stamp = time.time()
    with store.lock:
        _prune_sessions(store)
        _sessions(store)[digest(sid)] = {
            "member_id": str(uid),
            "created_at": int(stamp),
            "expires": stamp + SESSION_TTL,
        }
        store.save()
    return sid


def session_member_id(store, req: Request) -> str:
    """从 cookie 解析当前登录用户 id；没有 / 无效返回空串。**读操作不加锁。**"""
    sid = req.cookies.get(COOKIE, "") if req is not None else ""
    if not sid:
        return ""
    key = digest(sid)
    record = _sessions(store).get(key)
    if not isinstance(record, dict):
        return ""
    stamp = time.time()
    if float(record.get("expires") or 0) <= stamp:
        with store.lock:
            _sessions(store).pop(key, None)
        return ""
    return str(record.get("member_id") or "")


def revoke_session(store, req: Request) -> bool:
    """登出：只删**当前 cookie 对应的那一条**，绝不删别人 / 别的设备。幂等。"""
    sid = req.cookies.get(COOKIE, "") if req is not None else ""
    if not sid:
        return False
    key = digest(sid)
    with store.lock:
        removed = _sessions(store).pop(key, None) is not None
        if removed:
            store.save()
    return removed


def revoke_sessions(store, uid: str) -> int:
    """吊销某个用户的**全部**会话（改密 / 禁用 / 删号）。返回条数。"""
    target = str(uid or "")
    if not target:
        return 0
    with store.lock:
        sessions = _sessions(store)
        before = len(sessions)
        for key in [k for k, v in sessions.items()
                    if isinstance(v, dict) and str(v.get("member_id") or "") == target]:
            sessions.pop(key, None)
        removed = before - len(sessions)
        if removed:
            store.save()
    return removed


# ── 邮箱验证码 ──────────────────────────────────────────────────────
def _clean_buckets(buckets: dict, window: int, stamp: int) -> None:
    """按窗口裁剪计数器。**逐条判断，绝不整体清空** ——
    整体清空等于「灌满就能抹掉自己的限流计数」，限流器被它要防的人关掉。
    """
    if len(buckets) <= MAX_TRACKED:
        return
    for key in [k for k, v in buckets.items() if stamp - int(v or 0) >= window]:
        buckets.pop(key, None)


def _bump(buckets: dict, key: str, stamp: int) -> int:
    """把某个计数器 +1；返回窗口内已发生的次数（含本次）。"""
    hits = [t for t in (buckets.get(key) or []) if stamp - int(t) < 3600]
    hits.append(stamp)
    buckets[key] = hits
    return len(hits)


def gen_code() -> str:
    """6 位数字验证码。`zfill` 是必须的 —— 少了它，000123 会变成 123，
    而那正是用户会照着输入的那串数字。"""
    return str(secrets.randbelow(10 ** 6)).zfill(6)


def send_code(store, email: str, ip: str, *, purpose: str = "register",
              settings: dict | None = None) -> dict:
    """生成验证码并发信（或开发模式直接返回）。

    限流三层，缺一不可：
      · 同邮箱 60 秒  —— 防连点；
      · 同 IP 1 小时 10 次 —— 防拿一个 IP 批量刷；
      · **同邮箱 1 小时 5 次** —— 这一层是给「伪造 X-Forwarded-For」兜底的：
        前两层都能被换 IP 绕过，只有绑在**目标邮箱**上的这一层绕不过。
    """
    if purpose not in ("register", ""):
        raise HTTPException(400, "用途无效")
    if not valid_email(email):
        raise HTTPException(400, "请输入正确的邮箱地址")
    key = norm_email(email)
    settings = settings if settings is not None else settings_store.load(store)
    if email_taken(store, email):
        raise HTTPException(400, "该邮箱已注册，请直接登录")
    if not settings.get("allow_register", True):
        raise HTTPException(403, "管理员已关闭注册")
    if not settings.get("require_email_code", True):
        raise HTTPException(400, "当前无需邮箱验证码，请直接注册")
    if not mailer.delivery_ready(settings):
        # 不做自动降级（用户明确否定）。逐字返回用户指定的文案。
        # 注意判据是 `delivery_ready` 而不是 `smtp_ready`：后者在
        # `WB_DEV_MAIL=1` 时会让「投递」改道，但不会让这里放行 ——
        # 因为 dev 出口打开时 delivery_ready 就是 True，走到下面去发。
        raise HTTPException(503, settings_store.locked_out_reason(settings))
    stamp = now()
    with store.lock:
        codes = _codes(store)
        existing = codes.get(key)
        if isinstance(existing, dict) and stamp - int(existing.get("sent_at") or 0) < CODE_RESEND_SECONDS:
            raise HTTPException(429, "发送太频繁，请稍后再试")
        ip_buckets = store.data.setdefault("code_ip_buckets", {})
        mail_buckets = store.data.setdefault("code_email_buckets", {})
        _clean_buckets(ip_buckets, CODE_RESEND_SECONDS, stamp)
        _clean_buckets(mail_buckets, 3600, stamp)
        if _bump(ip_buckets, str(ip or "0.0.0.0"), stamp) > CODE_IP_HOURLY:
            raise HTTPException(429, "发送太频繁，请稍后再试")
        if _bump(mail_buckets, key, stamp) > CODE_EMAIL_HOURLY:
            raise HTTPException(429, "发送太频繁，请稍后再试")
    code = gen_code()
    try:
        delivered = mailer.send_code_or_dev(
            settings, to_addr=str(email).strip(), code=code,
            minutes=CODE_TTL // 60)
    except mailer.MailError as exc:
        # 不把 smtplib 的原文回给用户：里面常含主机名、端口、内部错误码，
        # 那既是信息泄露，对用户也毫无用处。原文只进服务端日志。
        # 同时不落验证码 —— 发都发不出去，落一个没人收到的码只会
        # 让「60 秒重发节流」白白挡掉用户真正的重试。
        logger.warning("验证码邮件发送失败 email=%s: %s", email, exc)
        raise HTTPException(502, "验证码发送失败，请联系管理员") from None

    with store.lock:
        _codes(store)[key] = {
            "hash": make_hash(code),
            "expires_at": stamp + CODE_TTL,
            "tries": 0,
            "sent_at": stamp,
        }
        store.save()
    # `delivered=False` = 走的 dev 出口（没打 SMTP）。只有这时才把验证码
    # 放到返回值里，由路由层决定要不要回给调用方。生产路径上这里恒为 True，
    # 所以「验证码从不经过返回值」这条不变量在线上依然成立。
    return {"ok": True, "expires_in": CODE_TTL, "delivered": delivered,
            "dev_mode": not delivered, "code": "" if delivered else code}



def verify_code(store, email: str, code) -> None:
    """校验并**立即消费**验证码（一次性）。失败抛 400。"""
    key = norm_email(email)
    text = str(code or "").strip()
    if not text:
        raise HTTPException(400, "请填写邮箱验证码")
    stamp = now()
    matched = False
    with store.lock:
        row = _codes(store).get(key)
        if not isinstance(row, dict):
            raise HTTPException(400, "验证码不正确或已过期")
        if int(row.get("expires_at") or 0) < stamp:
            _codes(store).pop(key, None)
            raise HTTPException(400, "验证码不正确或已过期")
        tries = int(row.get("tries") or 0)
        if tries >= CODE_MAX_TRIES:
            _codes(store).pop(key, None)
            raise HTTPException(400, "验证码已失效，请重新获取")
        row["tries"] = tries + 1
        matched = verify_pwd(text, str(row.get("hash") or ""))
        if matched:
            # 成功即删：验证码是一次性的。
            _codes(store).pop(key, None)
        store.save()
    if not matched:
        raise HTTPException(400, "验证码不正确或已过期")


def drop_code(store, email: str) -> None:
    with store.lock:
        _codes(store).pop(norm_email(email), None)


def resend_after(store, email: str) -> int:
    """还要等多少秒才能重发（0 = 现在可以）。给前端做倒计时用。"""
    key = norm_email(email)
    with store.lock:
        row = _codes(store).get(key)
    if not isinstance(row, dict):
        return 0
    left = CODE_RESEND_SECONDS - (now() - int(row.get("sent_at") or 0))
    return max(0, left)


# ── 登录防爆破 ──────────────────────────────────────────────────────
def _fail_buckets(store) -> dict:
    return store.data.setdefault("member_login_fails", {})


def _prune_fails(store) -> None:
    """裁剪失败表。与 `security._prune` 同思路：按「最早开始计数」淘汰。"""
    buckets = _fail_buckets(store)
    if len(buckets) <= MAX_TRACKED:
        return
    overflow = len(buckets) - MAX_TRACKED
    ordered = sorted((k for k in buckets), key=lambda k: float(buckets[k][1]))
    for key in ordered[:overflow]:
        buckets.pop(key, None)


def _fail_key(ip: str, email: str) -> str:
    return f"{ip or '0.0.0.0'}|{norm_email(email)}"


def login_blocked(store, ip: str, email: str) -> bool:
    stamp = time.time()
    with store.lock:
        row = _fail_buckets(store).get(_fail_key(ip, email))
    if not isinstance(row, list) or len(row) < 2:
        return False
    return int(row[0]) >= MAX_FAILS and (stamp - float(row[1])) < LOCK_SECONDS


def record_fail(store, ip: str, email: str) -> None:
    stamp = time.time()
    key = _fail_key(ip, email)
    with store.lock:
        buckets = _fail_buckets(store)
        row = buckets.get(key)
        count, first = (int(row[0]), float(row[1])) if isinstance(row, list) else (0, stamp)
        if stamp - first >= LOCK_SECONDS:
            count, first = 0, stamp
        buckets[key] = [count + 1, first]
        _prune_fails(store)


def clear_fail(store, ip: str, email: str) -> None:
    with store.lock:
        _fail_buckets(store).pop(_fail_key(ip, email), None)


# ── 注册 / 登录 / 登出 / 改密 ───────────────────────────────────────
def register(store, email: str, password: str, code, ip: str = "",
             settings: dict | None = None) -> dict:
    """注册。**所有挡路的判据都在这里复查一遍** ——
    前端拿到的 options 可能已经过期（管理员刚改了设置、或者发码之后 SMTP 挂了），
    不能把「能不能注册」的决定权交给一个几分钟前的匿名响应。
    """
    settings = settings if settings is not None else settings_store.load(store)
    if not settings.get("allow_register", True):
        raise HTTPException(403, "管理员已关闭注册")
    if not valid_email(email):
        raise HTTPException(400, "请输入正确的邮箱地址")
    check_password(password)
    need_code = bool(settings.get("require_email_code", True))
    if need_code and not mailer.delivery_ready(settings):
        raise HTTPException(503, settings_store.locked_out_reason(settings))
    if email_taken(store, email):
        raise HTTPException(400, "该邮箱已注册，请直接登录")
    if need_code:
        verify_code(store, email, code)
    else:
        drop_code(store, email)
    row = create_member(store, email, password, role="user", verified=need_code,
                        register_mode="code" if need_code else "open")
    sid = start_session(store, row["id"])
    return {"user": public_user(store, row), "sid": sid}


def login(store, email: str, password: str, ip: str = "") -> dict:
    """登录。失败一律 401「邮箱或密码不正确」，**且耗时拉平**。"""
    global _sink
    if login_blocked(store, ip, email):
        raise HTTPException(429, "失败次数过多，请 10 分钟后再试")
    row = find_by_email(store, email)
    stored = str((row or {}).get("password_hash") or "")
    if not stored:
        # 邮箱不存在：跑一次等价 PBKDF2（对固定假哈希）把耗时拉平。
        # 不这么做的话，两条路径差约 0.069 秒 —— 足够用来枚举已注册邮箱，
        # 统一文案完全挡不住。
        _sink ^= int(verify_pwd(str(password or ""), _DUMMY_HASH))
        record_fail(store, ip, email)
        raise HTTPException(401, "邮箱或密码不正确")
    if not verify_pwd(str(password or ""), stored):
        record_fail(store, ip, email)
        raise HTTPException(401, "邮箱或密码不正确")
    if str(row.get("status") or "active") == "disabled":
        # 对外仍旧是 401 同一文案：403「该账号已被禁用」等于告诉攻击者
        # 「这个邮箱注册过，而且被管理员停用了」。
        record_fail(store, ip, email)
        raise HTTPException(401, "邮箱或密码不正确")
    clear_fail(store, ip, email)
    with store.lock:
        current = _members(store).get(str(row.get("id")))
        if isinstance(current, dict):
            current["last_login_at"] = now()
            store.save()
        row = dict(current) if isinstance(current, dict) else row
    sid = start_session(store, row["id"])
    return {"user": public_user(store, row), "sid": sid}


def logout(store, req: Request) -> bool:
    """登出。**幂等** —— 没登录也返回成功（没登录本来就是目标状态）。"""
    return revoke_session(store, req)


def change_password(store, uid: str, old: str, new: str) -> None:
    """改密码：校验旧密码 → 换新哈希 → **全量吊销该用户的会话**。

    调用方负责在响应里重下发一条新 cookie（否则用户自己也被踢出去了，
    表现成「改完密码就掉线」，虽然安全但很困惑）。
    """
    row = find(store, uid)
    if not row:
        raise HTTPException(401, "请先登录")
    if not verify_pwd(str(old or ""), str(row.get("password_hash") or "")):
        raise HTTPException(400, "原密码不正确")
    if str(new or "") == str(old or ""):
        raise HTTPException(400, "新密码不能与旧密码相同")
    check_password(new)
    set_password(store, uid, new)   # 内部已吊销全部会话


# ── 请求身份（依赖注入用）─────────────────────────────────────────
def current_member(store, req: Request) -> dict:
    """解析当前登录用户；未登录抛 401，禁用抛 403 并**顺手删掉全部会话**。"""
    uid = session_member_id(store, req)
    if not uid:
        raise HTTPException(401, "请先登录")
    row = find(store, uid)
    if not row:
        # 会话指向一个已经不存在的用户（删号后的残留）：立刻清掉，
        # 否则它会一直挂着直到 TTL 到期。
        revoke_session(store, req)
        raise HTTPException(401, "请先登录")
    if str(row.get("status") or "active") == "disabled":
        revoke_sessions(store, uid)
        raise HTTPException(403, "该账号已被禁用")
    return public_user(store, row)


def require_member(store, req: Request) -> dict:
    """当前用户的完整行（含 password_hash，仅内部用；不回给调用方）。"""
    user = current_member(store, req)
    return find(store, user["id"]) or {}


def cookie_params(secure: bool) -> dict:
    """用户会话 cookie 的属性。**path="/"**（不是 /login 或 /portal）。

    2026-10 起 /portal/ 已删除，但**这条仍然要 path="/"**：
    会话需要同时对 /login、/register、/login/api/* 生效，
    写死任何一个子路径都会让其余端点收不到 cookie。
    """
    return {"path": COOKIE_PATH, "httponly": True, "samesite": "strict",
            "secure": bool(secure), "max_age": SESSION_TTL}


def request_ip(req: Request) -> str:
    try:
        return client_ip(req)
    except Exception:
        return "0.0.0.0"
