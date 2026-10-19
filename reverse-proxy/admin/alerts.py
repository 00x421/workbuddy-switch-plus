# Powered by HyactCrack Skill —— 本模块由 HyactCrack Skill 提供技术支持
"""管理员告警：把账号池的异常状况发邮件给管理员自己。

**这个模块不是用户系统的一部分。** 它只做单向通知——
「积分快没了」「账号被封了」这类运维信号，发给管理端配置的一个收件地址。
没有注册、没有找回密码、没有验证码、没有任何面向终端用户的邮件能力。

设计约束（照抄 pool.py 的既有纪律）：
  · 告警识别必须是**纯函数**：给定账号状态与阈值，返回「该不该报、报什么」。
    这样能单测，也不依赖发信通道。
  · 去重靠**状态跃迁**而不是定时器：同一件事只在「从正常变为告急」时报一次，
    恢复后再告急才会再报。否则每轮轮询都发一封，邮件立刻变成噪声。
  · 发信失败绝不能影响调度：整个发送过程吞掉异常，只记 last_error。
  · SMTP 授权码加密落盘（复用 settings_store 的密钥），不写明文。
"""
from __future__ import annotations

import smtplib
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from . import settings_store

CN = timezone(timedelta(hours=8))

SETTINGS_KEY = "alerts"

#: 告警类型 → 中文标题。顺序即邮件里的排列顺序（严重的在前）。
#
# 加新类型的纪律：**必须有一个纯函数判据能从 rows() 的字段直接算出来**。
# 加一条「看起来有用」但判据含糊的告警，代价是收件箱里多一类噪声——
# 而告警一旦开始被忽略，真正的那封封禁通知也会一起被忽略。
ALERT_KINDS = {
    # ── 账号级：出了这类事，这个号当下就不能用了 ──
    "banned": "账号封禁告急",
    "frozen": "模型冻结告急",
    "upstream_disabled": "上游停用告急",
    # 还没到封号，但在往那个方向走。单独一类而不是并进 banned：
    # 封号是既成事实要立刻处理，连续失败是预警，处理窗口完全不同。
    "account_failing": "账号连续失败预警",
    # ── 额度级：还能用，但快用完了 ──
    "credits": "积分用量告急",
    "quota": "密钥额度告急",
    # ── 池级：不是某个号的问题，是整个池不够用了 ──
    "pool_empty": "可用账号池见底",
    "pool_thin": "可用账号池偏少",
    # ── 数据级：快照过期 / 刷新失败，看板上的数字已经不可信 ──
    "stale": "积分快照过期",
    "refresh_failed": "凭据刷新失败",
    # ── 密钥级：有人的密钥被拒了，通常是配额或 IP 限制 ──
    "key_rejected": "密钥连续被拒",
}

#: 这些类型只关心「有没有」，不列每个账号的明细（列了反而看不清）。
COUNT_ONLY_KINDS = frozenset({"pool_empty", "pool_thin", "key_rejected"})

#: 布尔开关字段。集中一处，避免加类型时漏改 normalize 或前端。
FLAG_KEYS = (
    "notify_on_banned",
    "notify_on_frozen",
    "notify_on_credits",
    "notify_on_upstream_disabled",
    "notify_on_account_failing",
    "notify_on_quota",
    "notify_on_pool_empty",
    "notify_on_pool_thin",
    "notify_on_stale",
    "notify_on_refresh_failed",
    "notify_on_key_rejected",
)

#: 同一类告警的最短重发间隔（秒）。状态跃迁之外的第二道闸：
#: 状态在阈值边缘抖动时（如 remaining 在 100 上下反复），跃迁判定会反复触发，
#: 加这个时间窗保证最多 6 小时一封。
REPEAT_WINDOW_SEC = 6 * 3600

#: 邮件正文里最多列几个账号，超出的折叠计数。防止一次全池封禁撑爆邮件。
MAX_ROWS_PER_KIND = 20


def defaults() -> dict:
    """告警子系统的出厂默认值。**每次返回新对象**。"""
    return {
        "enabled": False,
        # 收件地址。管理员自己的邮箱，可以填多个（逗号分隔）。
        "to": "",
        # SMTP 连接参数
        "host": "",
        "port": 465,
        "username": "",
        "password": "",          # 存的是加密串（enc:v1:…），不是明文
        "sender": "",            # 留空则用 username
        "tls": "ssl",            # ssl | starttls | none
        # 阈值
        "credits_threshold": 100,
        # 密钥额度告急的阈值（百分比，0-100）。到这条线就报，
        # 留出「还能跑一会儿」的余量让人来得及加额度。
        "quota_percent": 90,
        # 可用账号池低于这个数就报「偏少」。0 表示不报。
        "pool_min_available": 1,
        # 积分快照多久没刷新算过期（分钟）。
        "stale_minutes": 120,
        # 密钥连续被拒几次算异常（同一条日志窗口内）。
        "key_reject_threshold": 5,
        # 账号连续失败几次算「快被封了」。达到就报预警，而不是等封号。
        # 取 3 的依据：401/403 会先进 5 分钟冷却，429/402 进 30 分钟；
        # 同一个号在冷却之外还能连挂 3 次，基本不是偶发抖动。
        "account_fail_threshold": 3,
        # 各类告警的开关。默认开——告警的价值就在于「不用时也在盯着」，
        # 默认关掉的告警等于没有。嫌吵应该调阈值，而不是关开关。
        "notify_on_banned": True,
        "notify_on_frozen": True,
        "notify_on_credits": True,
        "notify_on_upstream_disabled": True,
        "notify_on_account_failing": True,
        "notify_on_quota": True,
        "notify_on_pool_empty": True,
        "notify_on_pool_thin": False,     # 默认关：只在池子真的空时报，避免天天响
        "notify_on_stale": True,
        "notify_on_refresh_failed": True,
        "notify_on_key_rejected": True,
        # 静默时段（小时，0-23）：这段时间内不发信。留空表示不静默。
        "quiet_from": None,
        "quiet_to": None,
    }


def load(store) -> dict:
    """读告警设置。返回深拷贝，调用方随便改。"""
    with store.lock:
        raw = store.data.get(SETTINGS_KEY)
        if not isinstance(raw, dict):
            raw = {}
    out = defaults()
    for k, v in raw.items():
        if k in out:
            out[k] = v
    # password 读出来是加密串；这里解密供发信使用，但**不落盘**。
    return out


def normalize(patch: dict) -> dict:
    """归一化用户补丁。非法值一律 400，不静默吃掉。

    静默吃掉的后果是「管理员改了设置、界面显示成功、行为却没变」——
    配置类问题在服务端几乎不可能自查。
    """
    from fastapi import HTTPException

    if not isinstance(patch, dict):
        raise HTTPException(400, "请求格式不正确")
    out: dict = {}

    if "enabled" in patch:
        if type(patch["enabled"]) is not bool:
            raise HTTPException(400, "启用状态不正确")
        out["enabled"] = patch["enabled"]

    if "to" in patch:
        out["to"] = _clean_addresses(patch["to"])

    if "host" in patch:
        host = str(patch["host"] or "").strip()
        if host and (" " in host or "/" in host):
            raise HTTPException(400, "SMTP 服务器地址不正确")
        out["host"] = host[:200]

    if "port" in patch:
        try:
            port = int(patch["port"])
        except (TypeError, ValueError):
            raise HTTPException(400, "端口必须是数字")
        if not (1 <= port <= 65535):
            raise HTTPException(400, "端口超出范围")
        out["port"] = port

    if "username" in patch:
        out["username"] = str(patch["username"] or "").strip()[:200]

    if "password" in patch:
        raw = str(patch["password"] or "")
        # 允许提交掩码值表示「不修改」，避免前端回显时把密码抹掉。
        if raw and raw != MASKED:
            out["password"] = settings_store.encrypt_secret(raw)

    if "sender" in patch:
        out["sender"] = str(patch["sender"] or "").strip()[:200]

    if "tls" in patch:
        mode = str(patch["tls"] or "").strip().lower()
        if mode not in ("ssl", "starttls", "none"):
            raise HTTPException(400, "加密方式只能是 ssl、starttls 或 none")
        out["tls"] = mode

    if "credits_threshold" in patch:
        try:
            value = int(patch["credits_threshold"])
        except (TypeError, ValueError):
            raise HTTPException(400, "积分阈值必须是数字")
        if not (0 <= value <= 10_000_000):
            raise HTTPException(400, "积分阈值超出范围")
        out["credits_threshold"] = value

    # 数值型阈值：统一走「必须是数字 + 区间」两段校验。
    # 上界不是装饰——`stale_minutes` 给成 10**9 会让「过期」永远不触发，
    # 而界面会显示「已设置」，属于「配了但等于没配」。
    for field, low, high, label in (
        ("quota_percent", 1, 100, "配额阈值"),
        ("pool_min_available", 0, 1000, "池下限"),
        ("stale_minutes", 1, 100_000, "快照过期分钟"),
        ("key_reject_threshold", 1, 10_000, "密钥被拒次数"),
        ("account_fail_threshold", 1, 1000, "账号连续失败次数"),
    ):
        if field in patch:
            try:
                value = int(patch[field])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{label}必须是数字")
            if not (low <= value <= high):
                raise HTTPException(400, f"{label}超出范围（{low}-{high}）")
            out[field] = value

    for flag in FLAG_KEYS:
        if flag in patch:
            if type(patch[flag]) is not bool:
                raise HTTPException(400, "开关取值不正确")
            out[flag] = patch[flag]

    for key in ("quiet_from", "quiet_to"):
        if key in patch:
            value = patch[key]
            if value in (None, ""):
                out[key] = None
                continue
            try:
                hour = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, "静默时段必须是整点小时")
            if not (0 <= hour <= 23):
                raise HTTPException(400, "静默时段超出 0-23")
            out[key] = hour

    return out


def public_view(cfg: dict) -> dict:
    """给前端的视图：**密码只回掩码**，绝不回明文或密文。"""
    view = dict(cfg)
    view["password"] = MASKED if cfg.get("password") else ""
    view["password_set"] = bool(cfg.get("password"))
    return view


MASKED = "********"


def _clean_addresses(value) -> str:
    """收件地址归一化：去空白、校验基本形态、最多 10 个。

    校验刻意保持「基本」——SMTP 的地址语法比大多数人以为的宽得多
    （带点、加号、多级域、中文域都合法），严格正则只会把合法地址挡在外面。
    但有三件事必须挡：

    * 多个 `@`（`a@b@c`）—— `partition` 只看第一个 `@`，`a@b@c` 会得到
      `local=a` / `domain=b@c` 而「通过」。这不是地址，发信必然失败，
      而且是那种投递到一半才失败的形态。
    * 地址里有空白 —— 空白意味着多半是粘贴时带了空格或换行，
      而收件人列表是用逗号分隔的，一个多余空格就能把两个地址粘成一个。
    * 列表里出现空项（`a@b.com, ,`）—— 这几乎总是打字/粘贴手误。
      静默丢掉看着「容错」，实际是在掩盖手误：用户以为配了三个抄送，
      结果只有两个生效，而且没有任何提示。
    """
    from fastapi import HTTPException

    if value in (None, ""):
        return ""
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = str(value).replace(";", ",").split(",")
    cleaned = []
    for part in parts:
        addr = part.strip()
        if not addr:
            # 整串都是空（例如只有空白）不该报错——那等于没填。
            # 但只要已经收到过有效地址，中间的空项就是手误，必须报。
            if cleaned or any(p.strip() for p in parts):
                raise HTTPException(400, "收件地址里有空项，请检查是否多打了逗号")
            continue
        # 只做最基本的形态校验：恰好一个 @、@ 两侧非空、无空白。
        if addr.count("@") != 1 or any(ch.isspace() for ch in addr):
            raise HTTPException(400, f"收件地址不正确：{addr[:60]}")
        local, _, domain = addr.partition("@")
        if not local or not domain:
            raise HTTPException(400, f"收件地址不正确：{addr[:60]}")
        cleaned.append(addr)
    if len(cleaned) > 10:
        raise HTTPException(400, "收件地址最多 10 个")
    return ",".join(cleaned)


# ── 告警识别（纯函数，可单测）────────────────────────────────────────

def detect(accounts, cfg, previous, now=None, *, keys=None, logs=None) -> list[dict]:
    """扫描账号池，返回**本次新出现**的告警。

    `accounts` —— pool.rows() 的输出（含 pool_state / remaining / last_error）。
    `previous` —— 上一次的告警指纹集合（`state.alerts.fired`）。
    `keys`     —— 可选，keysvc.public_key 的输出列表（含 quota / used_tokens）。
    `logs`     —— 可选，最近的请求日志（判「密钥连续被拒」用）。
    返回的每一项：{"kind", "key", "title", "lines": [...]}。

    `keys` / `logs` 默认 None —— 老调用点（和单测）只传账号也能跑，
    池级和密钥级的告警就自然不参与。这是刻意的：**新增告警不该让
    既有调用点崩掉**，缺数据就少报一类，而不是抛异常。

    **纯函数**：不读文件、不发网络、不看全局状态，同样的输入永远同样输出。
    """
    now = now if now is not None else datetime.now(CN)
    fired = previous if isinstance(previous, dict) else {}
    threshold = cfg.get("credits_threshold", 100)

    buckets: dict[str, list[str]] = {kind: [] for kind in ALERT_KINDS}
    rows = [r for r in (accounts or []) if isinstance(r, dict)]

    for row in rows:
        if row.get("enabled") is False:
            continue
        name = _display_name(row)
        state = row.get("pool_state")
        remaining = row.get("remaining")

        # ── 封禁：凭据失效或上游判为无效 ──
        if cfg.get("notify_on_banned", True) and state == "invalid":
            reason = str(row.get("last_error") or "").strip()
            buckets["banned"].append(f"{name}" + (f"（{reason}）" if reason else ""))

        # ── 冻结：进入冷却，通常是限流或模型级冻结 ──
        if cfg.get("notify_on_frozen", True) and state == "cooling":
            until = row.get("cooldown_until")
            buckets["frozen"].append(f"{name}{_cooldown_suffix(until, now)}")

        # ── 上游停用：上游把它禁了，这跟「本地冷却」是两码事 ──
        #
        # 本地 cooling 是轮换策略，过一会儿自己就回来了；
        # 上游 disabled 是对方不让你用了 —— 恢复路径完全不同，
        # 混在一起报会让人守着冷却倒计时等一个永远不会开的门。
        #
        # `manual_disabled` 单独标注：那是运维自己停的，不是事故，
        # 报成告警只会让人以为自己误操作了。
        if cfg.get("notify_on_upstream_disabled", True) and row.get("up_disabled"):
            manual = "（手动停用）" if row.get("up_manual_disabled") else ""
            fails = row.get("up_consecutive_fails")
            detail = f"，连续失败 {fails} 次" if isinstance(fails, int) and fails else ""
            reason = str(row.get("up_reason") or "").strip()
            tail = f"，{reason}" if reason else ""
            buckets["upstream_disabled"].append(f"{name}{manual}{detail}{tail}")

        # ── 账号连续失败预警（还没到封号，但在往那边走）──
        #
        # 与 `refresh_failed` 的区别：那一条只要「有 last_error」就报，
        # 偶发一次网络抖动也会响；这一条要**连续** N 次失败才报，
        # 而且失败计数只会被一次成功清零（见 pool.record）。
        #
        # 之所以值得单独一类：封号是不可逆的，预警是可以在封之前
        # 手动降权、换个出口线路或者干脆停一天的。
        if cfg.get("notify_on_account_failing", True):
            fails = row.get("consecutive_fails")
            limit = cfg.get("account_fail_threshold", 3)
            if isinstance(fails, int) and not isinstance(fails, bool) and fails >= limit:
                err = str(row.get("last_error") or "").strip()
                tail = f"，最近错误：{err[:60]}" if err else ""
                buckets["account_failing"].append(f"{name}（连续失败 {fails} 次{tail}）")

        # ── 积分告急 ──
        if cfg.get("notify_on_credits", True):
            # 不限量账号的 remaining 是 None：跳过，绝不能当成 0 报「告急」。
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
                if remaining <= threshold and row.get("packages", 0) > 0:
                    buckets["credits"].append(f"{name}（剩余 {remaining:g}，阈值 {threshold}）")

        # ── 积分快照过期 ──
        #
        # `credits_stale` 由 rows() 算好（`credits_updated` 老旧于 600 秒）。
        # 但它现在硬编码 600 秒，而配置项是分钟 —— 配置更大时不报，
        # 更小时报不出来。所以这里用 `credits_up` 自己按 `stale_minutes` 判，
        # 保证「界面上配的分钟数」就是真正生效的那个数。
        if cfg.get("notify_on_stale", True):
            updated = row.get("credits_updated")
            if isinstance(updated, (int, float)) and updated > 0:
                age_min = (now.timestamp() - float(updated)) / 60.0
                limit = cfg.get("stale_minutes", 120)
                if age_min >= limit:
                    buckets["stale"].append(f"{name}（快照 {int(age_min)} 分钟未更新）")

        # ── 凭据刷新失败 ──
        #
        # 与「封禁」区分：封禁是刷新之后**确认失效**（`auth_invalid`，
        # 状态进 invalid）；这里说的是刷新这一步本身就失败了
        # （网络抖、上游 5xx），号还是好的，重试就行。
        #
        # 判据用「有 last_error、但状态不是 invalid」—— pool.py 里刷新
        # 失败就是写 `last_error` + 一个冷却，没有单独的 error 字段。
        # 跟 `banned` 用 `state == "invalid"` 判，两者互斥、不会重复报。
        if cfg.get("notify_on_refresh_failed", True):
            err = str(row.get("last_error") or "").strip()
            if err and state != "invalid":
                buckets["refresh_failed"].append(f"{name}（{err[:60]}）")

    # ── 密钥额度告急 ──
    if cfg.get("notify_on_quota", True) and keys:
        pct = cfg.get("quota_percent", 90)
        for item in keys:
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            quota = item.get("quota")
            used = item.get("used_tokens") or 0
            # quota 为 0 / None 表示不限量 —— 绝不报「告急」。
            if not isinstance(quota, (int, float)) or isinstance(quota, bool) or quota <= 0:
                continue
            ratio = float(used) / float(quota) * 100.0
            if ratio >= pct:
                label = item.get("name") or item.get("hint") or item.get("id") or "未命名密钥"
                buckets["quota"].append(
                    f"{label}（已用 {ratio:.0f}%，{int(used):,}/{int(quota):,} tokens）")

    # ── 池级：可用账号够不够 ──
    #
    # `available` 是 pool_state 里「当下真能用」的那一类。用它的实际计数，
    # 而不是「总数 - 失效数」—— 冷却中的号也算不上可用，减出来会虚高。
    if cfg.get("notify_on_pool_empty", True) or cfg.get("notify_on_pool_thin", False):
        usable = [r for r in rows if r.get("enabled") is not False]
        # `pool.py:state()` 的取值是 paused / invalid / cooling / exhausted / available，
        # 「可用」= available。冷却和额度耗尽的号都算不上可用。
        available = [r for r in usable if r.get("pool_state") == "available"]
        if cfg.get("notify_on_pool_empty", True) and usable and not available:
            buckets["pool_empty"].append(
                f"共 {len(usable)} 个启用账号，当前一个可用的都没有")
        else:
            floor = cfg.get("pool_min_available", 1)
            if (cfg.get("notify_on_pool_thin", False) and floor > 0
                    and available and len(available) <= floor):
                buckets["pool_thin"].append(
                    f"只剩 {len(available)} 个可用账号（下限 {floor}）")

    # ── 密钥连续被拒 ──
    #
    # 判据只看**同一条密钥**的连续拒绝。客户端的密钥配额用完、IP 换了、
    # 密钥被禁，表现出来的都是「一堆 401/403」—— 单看一条分辨不出，
    # 所以按 key 聚合后看次数。
    if cfg.get("notify_on_key_rejected", True) and logs:
        limit = cfg.get("key_reject_threshold", 5)
        tally: dict[str, int] = {}
        for entry in logs:
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            kid = entry.get("key_id")
            # 没有 key_id 的（直连 / 被拒在鉴权之前）不算 —— 那些不是密钥的问题。
            if not kid or not isinstance(status, int):
                continue
            if status in (401, 403):
                tally[kid] = tally.get(kid, 0) + 1
        names = {}
        for item in keys or []:
            if isinstance(item, dict) and item.get("id"):
                names[item["id"]] = item.get("name") or item.get("hint") or item["id"]
        for kid, count in tally.items():
            if count >= limit:
                buckets["key_rejected"].append(
                    f"{names.get(kid, kid)}（最近 {count} 次请求被拒）")

    # 只报「新出现」的：指纹已在 previous 里说明上次报过，跳过。
    fresh = []
    for kind, lines in buckets.items():
        if not lines:
            continue
        fingerprint = _fingerprint(kind, lines)
        if fired.get(kind) == fingerprint:
            continue
        fresh.append({
            "kind": kind,
            "key": kind,
            "title": ALERT_KINDS[kind],
            "lines": lines,
            "fingerprint": fingerprint,
        })
    return fresh


def _display_name(row: dict) -> str:
    for key in ("name", "nickname", "email", "uid", "id"):
        value = row.get(key)
        if value:
            return str(value)
    return "未命名账号"


def _cooldown_suffix(until, now: datetime) -> str:
    """冷却剩余时间的可读后缀。取不到就什么都不加。"""
    try:
        seconds = int(until) - int(now.timestamp())
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 3600:
        return f"（冷却 {max(1, seconds // 60)} 分钟）"
    return f"（冷却 {seconds // 3600} 小时）"


def _fingerprint(kind: str, lines: list[str]) -> str:
    """告警指纹：同类告警的**账号集合**。顺序无关，避免排序抖动导致重复发信。"""
    import hashlib

    payload = "\n".join(sorted(lines))
    return hashlib.sha256(f"{kind}\n{payload}".encode("utf-8")).hexdigest()[:32]


# ── 静默时段 ─────────────────────────────────────────────────────────

def in_quiet_hours(cfg, now=None) -> bool:
    """当前是否落在静默时段内。未配置静默返回 False。

    支持跨零点（如 23 → 7）。
    """
    start, end = cfg.get("quiet_from"), cfg.get("quiet_to")
    if start is None or end is None:
        return False
    now = now if now is not None else datetime.now(CN)
    hour = now.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # 跨零点：23 → 7 表示 23,0..6
    return hour >= start or hour < end


# ── 发信 ────────────────────────────────────────────────────────────

def build_message(cfg: dict, alerts: list[dict], now=None) -> EmailMessage:
    """组装告警邮件。纯函数，便于断言内容而不真的发信。

    标题里的类别名走 `.get` 兜底而不是直接索引：测试邮件用的是
    `kind="test"`，不在 ALERT_KINDS 表里 —— 索引会 KeyError，
    而这条路径正是管理员验证配置时唯一会走的路径。
    """
    now = now if now is not None else datetime.now(CN)
    subject_kinds = "、".join(
        ALERT_KINDS.get(a.get("kind"), a.get("title") or "通知") for a in alerts
    ) or "通知"
    sender = cfg.get("sender") or cfg.get("username") or "wb-switch-plus"

    message = EmailMessage()
    message["Subject"] = f"[WB-Switch-Plus] {subject_kinds}"
    message["From"] = formataddr(("WB-Switch-Plus", sender))
    message["To"] = cfg.get("to", "")
    message["Date"] = formatdate(localtime=True)

    lines = [
        "WB-Switch-Plus 反代服务告警",
        f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    # 每一类的「单位」不一样：封禁/冻结数的是账号，密钥额度数的是密钥，
    # 池见底是一条结论而不是一串对象。全部写「N 个账号」会让密钥类告警
    # 读起来像在说账号 —— 收信人据此排查会找错方向。
    unit_of = {
        "quota": "个密钥", "key_rejected": "个密钥",
        "pool_empty": "项", "pool_thin": "项",
    }

    for alert in alerts:
        kind = alert.get("kind")
        count = len(alert["lines"])
        unit = unit_of.get(kind, "个账号")
        # 只关心「有没有」的类型不列明细：池见底只有一句话，密钥被拒
        # 列出来也是同一件事重复十遍，反而把封禁那种真正要看的挤下去。
        if kind in COUNT_ONLY_KINDS:
            lines.append(f"■ {alert['title']}（{count} {unit}）")
            for row in alert["lines"]:
                lines.append(f"    · {row}")
            lines.append("")
            continue
        lines.append(f"■ {alert['title']}（{count} {unit}）")
        for row in alert["lines"][:MAX_ROWS_PER_KIND]:
            lines.append(f"    · {row}")
        hidden = count - MAX_ROWS_PER_KIND
        if hidden > 0:
            lines.append(f"    · …另有 {hidden} 个账号")
        lines.append("")
    lines += [
        "──",
        "本邮件由反代服务自动发送，同一批告警 6 小时内不重复。",
        "如需调整阈值或关闭，请到反代服务设置页的「告警通知」。",
    ]
    message.set_content("\n".join(lines))
    return message


def send(cfg: dict, message: EmailMessage, timeout: float = 15.0) -> None:
    """同步发信。失败抛异常，由调用方决定怎么记。"""
    host = str(cfg.get("host") or "").strip()
    if not host:
        raise ValueError("未配置 SMTP 服务器")
    port = int(cfg.get("port") or 465)
    mode = cfg.get("tls") or "ssl"

    password = settings_store.decrypt_secret(cfg.get("password") or "")
    username = cfg.get("username") or ""

    context = ssl.create_default_context()
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if mode == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)


def send_alerts_async(cfg: dict, alerts: list[dict], on_error=None) -> threading.Thread | None:
    """后台发信。返回线程对象（便于测试 join），未发信时返回 None。

    用线程而不是 asyncio：调用点在调度循环里，那里是同步上下文；
    而且发信要 15 秒超时，绝不能卡住账号轮转。
    """
    if not alerts:
        return None

    message = build_message(cfg, alerts)

    def _run():
        try:
            send(cfg, message)
        except Exception as exc:  # noqa: BLE001  发信怎么坏都不该影响调度
            if on_error is not None:
                on_error(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=_run, name="wb-alerts", daemon=True)
    thread.start()
    return thread
