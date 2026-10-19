"""账号版本（realm）判定与分域请求头。

这是「跨版本统一账密」的地基：同一个账号池同时服务国内版与国际版，
每个账号属于哪一版只由它的凭据决定，不由服务的启动参数决定。

三条口径必须与桌面端一致（不一致会让同一个账号在两端落地成两套身份）：

  1. 显式 `realm` 字段优先；没有就看 domain 后缀；都没有判 CN。
  2. 域后缀必须**后缀对齐**，不能子串匹配（`workbuddy.ai.evil.com` 不能命中）。
  3. 空 domain 视为「无法判定」，交给调用方的兼容策略。

出站请求头按版本分三套，不能混：
  · chat 域    多段 UA + Origin/Referer
  · billing 域 单段 UA + 身份头（X-User-Id / X-Enterprise-Id / X-Domain）
  · 登录/扫码  只有 authorization + 极简头，UA 品牌段随版本
"""

from __future__ import annotations

import hashlib
import math

CN = "cn"
GLOBAL = "global"
REALMS = (CN, GLOBAL)

# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

CN_CHAT_BASE = "https://copilot.tencent.com"
CN_BILLING_BASE = "https://www.codebuddy.cn"
GLOBAL_BASE = "https://www.workbuddy.ai"

#: 登录/扫码链路的前缀（两版相同）
API_PREFIX = "/v2/plugin"

#: 国际版可选注册地区（IOS2 → 中文名）。顺序即界面展示顺序。
REGIONS = (
    ("HK", "中国香港"),
    ("MO", "中国澳门"),
    ("SG", "新加坡"),
    ("TH", "泰国"),
    ("PH", "菲律宾"),
    ("MY", "马来西亚"),
    ("ID", "印度尼西亚"),
)
REGION_CODES = tuple(code for code, _ in REGIONS)


def norm_realm(value) -> str:
    """把任意写法归一到 `"cn"` / `"global"`。

    上游在不同位置用了 global / ai / intl 几种别名，都收。**永远不返回空串**：
    「未知版本」这个状态一旦存在，下游就会到处写 `if realm:`，最后一处漏掉
    就会把国际版账号送进国内版端点。
    """
    text = str(value or "").strip().lower()
    if text in ("global", "ai", "intl", "international", "workbuddy-ai"):
        return GLOBAL
    return CN


def has_global_domain(domain) -> bool:
    """domain 是否属于国际版。

    只做后缀对齐，不性子串判断——`notworkbuddy.ai` 与 `workbuddy.ai.evil.com`
    都不是我们家的域。同时容忍传来的是完整 URL（去掉 scheme / 路径 / 端口）。
    """
    text = str(domain or "").strip().lower()
    if not text:
        return False
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
    return text == "workbuddy.ai" or text.endswith(".workbuddy.ai")


def realm_of(auth) -> str:
    """账号属于哪一版：显式 realm > variant > domain。

    domain 兜底是**升级兼容的关键**：升级前创建的 .info 里没有 realm 字段，
    不兜底就会把国际版账号误判成国内版。

    variant 排在 domain 之前：桌面端的 WbVariant 序列化成 cn / ai，而账号库里
    有一批是切版本之前登录的 —— variant 已经写成 ai，domain 却还停在
    www.codebuddy.cn。只看 domain 会把它们判成国内版，界面上的国际版标签就丢了。
    """
    if not isinstance(auth, dict):
        return CN
    explicit = str(auth.get("realm") or "").strip()
    if explicit:
        return norm_realm(explicit)
    variant = str(auth.get("variant") or "").strip()
    if variant:
        return norm_realm(variant)
    return GLOBAL if has_global_domain(auth.get("domain")) else CN


def matches_domain(realm, domain) -> bool:
    """domain 与 realm 是否自洽（空 domain 视为相符）。"""
    text = str(domain or "").strip()
    if not text:
        return True
    if norm_realm(realm) == GLOBAL:
        return has_global_domain(text)
    return not has_global_domain(text)


def chat_base(realm) -> str:
    return GLOBAL_BASE if norm_realm(realm) == GLOBAL else CN_CHAT_BASE


def billing_base(realm) -> str:
    return GLOBAL_BASE if norm_realm(realm) == GLOBAL else CN_BILLING_BASE


def origin(realm) -> str:
    return GLOBAL_BASE if norm_realm(realm) == GLOBAL else CN_BILLING_BASE


def platform(realm) -> str:
    """`/v2/plugin/auth/state?platform=` 的取值。

    国际版是 `workbuddy-ai`（与桌面端 `WbVariant::oauth_platform` 同值）。
    两版必须各取各的——写错了码会落到另一条产品线上，而且**不会报错**，
    只表现为扫了没反应。
    """
    return "workbuddy-ai" if norm_realm(realm) == GLOBAL else "workbuddy"


def accept_language(realm) -> str:
    return "en-US" if norm_realm(realm) == GLOBAL else "zh-CN"


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------


def _ua(realm) -> str:
    """chat 域 UA：`WorkBuddy/<ver> <平台名>/<ver> CLI/<cli>`。

    国际版的平台段是 `WorkBuddy AI`（带空格），这是官方客户端自己定的形态。
    """
    name = "WorkBuddy AI" if norm_realm(realm) == GLOBAL else "WorkBuddy"
    return f"WorkBuddy/5.5.4 {name}/5.5.4 CLI/2.137.1"


def billing_ua() -> str:
    """billing 域 UA：**单段** `WorkBuddy/<ver>`。

    签到 / 积分 / trial 这批接口官方会把 CLI 扩展段覆写掉；发多段会被当成
    网关流量。两版都用单段。
    """
    return "WorkBuddy/5.5.4"


def device_fingerprint_headers(uid) -> dict:
    """按账号稳定派生的设备指纹头。

    语义是「每个账号一台固定虚拟设备」：跨重启恒定、账号间互异、同 uid 恒同。
    派生方式必须与上游逐字一致，否则同一账号在「经上游」与「本服务直连」
    两条路上会被认成两台设备，反而制造出可关联的异常。

        X-Machine-ID = sha256("wb2a:machine:" + uid)[:36]
        X-Session-ID = sha256("wb2a:session:" + uid)[:36]
    """
    text = str(uid or "").strip()
    if not text:
        return {}
    return {
        "X-Machine-ID": hashlib.sha256(f"wb2a:machine:{text}".encode()).hexdigest()[:36],
        "X-Session-ID": hashlib.sha256(f"wb2a:session:{text}".encode()).hexdigest()[:36],
    }


def headers(realm, token=None, uid=None) -> dict:
    """chat 域通用请求头（Origin / Referer / UA / Accept-Language 随版本变）。"""
    realmed = norm_realm(realm)
    out = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": accept_language(realmed),
        "X-Requested-With": "XMLHttpRequest",
        # 官方客户端的风控闸门头，所有 API 请求必带。缺了它就是
        # 「唯一没这个头」的流量，最容易被单独挑出来。
        "X-CodeBuddy-Request": "1",
        "User-Agent": _ua(realmed),
        "Origin": origin(realmed),
        "Referer": origin(realmed) + "/",
    }
    if token:
        out["Authorization"] = "Bearer " + str(token)
    out.update(device_fingerprint_headers(uid))
    return out


def billing_headers(realm, auth=None) -> dict:
    """billing 域请求头：在通用头上换单段 UA 并补身份头。

    身份头只在值非空时才发（空值发过去反而像伪造——官方客户端不会发空头）。
    """
    auth = auth if isinstance(auth, dict) else {}
    realmed = norm_realm(realm)
    uid = str(auth.get("uid") or "")
    out = headers(realmed, str(auth.get("access_token") or "") or None, uid or None)
    out["User-Agent"] = billing_ua()
    if uid:
        out["X-User-Id"] = uid
    enterprise = str(auth.get("enterprise_id") or "")
    if enterprise:
        out["X-Enterprise-Id"] = enterprise
        out["X-Tenant-Id"] = enterprise
    domain = str(auth.get("domain") or "")
    if domain:
        out["X-Domain"] = domain
    return out


# ---------------------------------------------------------------------------
# 信封与到期时间
# ---------------------------------------------------------------------------


def envelope(resp):
    """腾讯统一信封 `{code, msg, data}` → `(code, data)`。

    HTTP 4xx 也可能是业务信封，所以先试 JSON。解析不出时用状态码当 code、
    None 当 data（调用方据 code 决定是否换下一条候选路径）。
    """
    try:
        env = resp.json()
    except Exception:  # noqa: BLE001
        return getattr(resp, "status_code", -1), None
    if isinstance(env, dict) and "code" in env:
        try:
            return int(env.get("code", -1)), env.get("data")
        except (TypeError, ValueError):
            return -1, env.get("data")
    return getattr(resp, "status_code", -1), env


def expires_at_ms(tokens, now_seconds) -> int:
    """从 token 响应推导到期时间（毫秒）。

    上游给法不统一：`expiresAt` 有的是秒有的毫秒（用 1e11 分界），或者只给
    `expiresIn`（秒）。**两个都没有时返回 0 表示「无法判定」**，由调用方
    报错——猜一个值会让账号在界面上显示「还有很久」而其实早已失效。
    """
    raw = tokens.get("expiresAt") if isinstance(tokens, dict) else None
    if (
        isinstance(raw, (int, float))
        and not isinstance(raw, bool)
        and math.isfinite(raw)
        and raw > 0
    ):
        return int(raw if raw >= 100_000_000_000 else raw * 1000)
    seconds = tokens.get("expiresIn") if isinstance(tokens, dict) else None
    if (
        isinstance(seconds, (int, float))
        and not isinstance(seconds, bool)
        and math.isfinite(seconds)
        and seconds > 0
    ):
        return int((now_seconds + seconds) * 1000)
    return 0


# ---------------------------------------------------------------------------
# 能力判断：某版本到底有没有这项功能
# ---------------------------------------------------------------------------
#
# 为什么要显式判断而不是「发了再说」：国际版没有签到体系（上游 realm 层同样
# 按版本分派）。以前我们对所有账号都发 `daily-checkin`，国际版账号每次点签到
# 都拿回一个业务错误码，被记成一次失败 → consecutive_fails 累加 → 误报
# 「账号连续失败预警」。正确做法是先判断，再决定发不发，并把「没有这个能力」
# 当成**正常结果**返回而不是失败。

#: billing 域路径候选表：kind → (path, ...)。顺序即尝试顺序。
#:
#: 用候选表而不是单个路径：上游同一语义的东西在不同时期挂在不同路径
#: （`checkin-activity-status` 与 `checkin-status` 并存就是现成的例子）。
#: 把「换路径」留在数据里，比散在调用点的 if 里好维护。
BILLING_PATHS = {
    "user-resource": ("get-user-resource",),
    "enterprise-usage": ("get-enterprise-user-usage",),
    "checkin-status": ("checkin-activity-status", "checkin-status"),
    "checkin": ("daily-checkin",),
    "checkin-compensation-status": ("checkin-compensation-status",),
    "checkin-compensation-claim": ("checkin-compensation-claim",),
}

#: 国际版**没有**的 billing 能力。新增能力只需改这张表。
GLOBAL_UNSUPPORTED = frozenset({
    "checkin",
    "checkin-status",
    "checkin-compensation-status",
    "checkin-compensation-claim",
})


def supports_checkin(realm) -> bool:
    """该版本有没有「每日签到」这套运营活动。

    国际版没有 —— 不是「暂时不可用」，是上游根本没提供。调用方据此**跳过
    请求**，并按「不支持」而不是「失败」处理。
    """
    return norm_realm(realm) != GLOBAL


def supports_billing(kind, realm) -> bool:
    """该版本是否提供某项 billing 能力。"""
    if norm_realm(realm) == GLOBAL and kind in GLOBAL_UNSUPPORTED:
        return False
    return True


def billing_paths(kind, realm):
    """取某能力的 billing 路径候选（只回该版本真的有的）。

    未知 kind 返回空元组 —— 比抛异常好：调用方本来就要处理「上游没这个端点」。
    """
    if not supports_billing(kind, realm):
        return ()
    return BILLING_PATHS.get(kind, ())


def billing_url(path, realm) -> str:
    """把 billing 路径拼成完整 URL（base 随版本变）。"""
    return f"{billing_base(realm)}/v2/billing/meter/{path.lstrip('/')}"


def unsupported_message(kind, realm) -> str:
    """「该版本没有这个能力」的用户可读文案（界面直接展示）。"""
    label = {
        "checkin": "每日签到",
        "checkin-status": "签到状态",
        "checkin-compensation-status": "补签状态",
        "checkin-compensation-claim": "补签",
    }.get(kind, kind)
    version = "国际版" if norm_realm(realm) == GLOBAL else "国内版"
    return f"{version}没有{label}"


def realm_label(realm) -> str:
    """界面用的版本标签（短，可当徽章）。"""
    return "国际版" if norm_realm(realm) == GLOBAL else "国内版"
