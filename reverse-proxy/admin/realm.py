"""版本归属（realm）：国内版 CN 与国际版 GLOBAL 的端点与请求头分派。

为什么单独一层：上游从 2026-09-14 起支持国际版，**同一套管理面同时服务两套
上游**（共用账号池），按账号的 realm 字段或 domain 后缀路由。

口径严格镜像上游（`ithtelab/workbuddy-manager` 的 `server/services/realm.py`，
该文件又逐条对照官方 Go 客户端的 client.go / headers.go 读出来的）：

  | 维度        | CN                        | GLOBAL                    |
  |-------------|---------------------------|---------------------------|
  | chat base   | copilot.tencent.com       | www.workbuddy.ai          |
  | billing     | www.codebuddy.cn          | www.workbuddy.ai          |
  | Origin/Ref  | www.codebuddy.cn          | www.workbuddy.ai          |
  | UA 品牌段   | WorkBuddy                 | WorkBuddy AI              |

判定顺序与上游一致：显式 realm == "global" 优先；否则看 domain 是否
workbuddy.ai / *.workbuddy.ai；都没有就判 CN。

**为什么判定要看 domain 兜底**：存量账号（本服务升级前创建的 .info）只有当
accessToken/refreshToken/domain 三件套，没有 realm 字段。让 domain 兜底可以
保证「升级后既有部署的行为一字不变」——这是能不能直接替换 exe 的前提。
"""
import hashlib
import math

CN = "cn"
GLOBAL = "global"
REALMS = (CN, GLOBAL)

#: 国际版 chat 与 billing 共用的基址（上游 global.chat_base 留空时也是它）
GLOBAL_BASE = "https://www.workbuddy.ai"
CN_CHAT_BASE = "https://copilot.tencent.com"
CN_BILLING_BASE = "https://www.codebuddy.cn"

GLOBAL_ORIGIN = "https://www.workbuddy.ai"
CN_ORIGIN = "https://www.codebuddy.cn"

#: 国际版判定用的域名后缀（含前导点；另接受裸 workbuddy.ai）
GLOBAL_DOMAIN_SUFFIX = ".workbuddy.ai"

#: 设备码 / 授权三段式路径前缀（两档位相同）
API_PREFIX = "/v2/plugin"

#: 国际版可选注册地区（IOS2 短码 → 中文名）。
#: 取自国际版官网给出的短名单；`global_region.region_fields` 会再向上游
#: `/billing/area/get-country-code` 查回真实的三字段取值。
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
    """归一 realm 取值。

    只认 "global"（以及上游同义的 ai / intl / international 别名），其余一律
    落 CN。**刻意不返回空串**：本服务的每个账号都必须明确属于某一版，
    留一个「未知」状态会让下游到处写 `if realm:`，最后总有一处漏掉。
    """
    v = str(value or "").strip().lower()
    if v in ("global", "ai", "intl", "international", "workbuddy-ai"):
        return GLOBAL
    return CN


def has_global_domain(domain) -> bool:
    """域名是否属于国际版（裸 workbuddy.ai 或任意 *.workbuddy.ai）。

    不能用 `"workbuddy.ai" in domain` 这种子串判断：`workbuddy.ai.evil.com`
    与 `notworkbuddy.ai` 都会命中。必须是后缀对齐。
    """
    d = str(domain or "").strip().lower()
    if not d:
        return False
    # 去掉可能的 scheme 与路径尾巴，容忍上游把完整 URL 塞进 domain
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0].split(":", 1)[0]
    return d == GLOBAL_DOMAIN_SUFFIX.lstrip(".") or d.endswith(GLOBAL_DOMAIN_SUFFIX)


def realm_of(auth) -> str:
    """账号归属：显式 realm 优先，其次 domain 后缀兜底。"""
    if not isinstance(auth, dict):
        return CN
    explicit = str(auth.get("realm") or "").strip()
    if explicit:
        return norm_realm(explicit)
    return GLOBAL if has_global_domain(auth.get("domain")) else CN


def matches_domain(realm, domain) -> bool:
    """domain 与 realm 是否自洽。

    空 domain 视为相符（无法判定，交给调用方的兼容策略）——与桌面端
    `WbVariant::matches_domain` 同口径。这条必须和桌面端一致，否则同一个
    账号在两端会得到不同的入库结论，账号库就分叉了。
    """
    d = str(domain or "").strip()
    if not d:
        return True
    return has_global_domain(d) if norm_realm(realm) == GLOBAL else not has_global_domain(d)


def chat_base(realm) -> str:
    return GLOBAL_BASE if norm_realm(realm) == GLOBAL else CN_CHAT_BASE


def billing_base(realm) -> str:
    return GLOBAL_BASE if norm_realm(realm) == GLOBAL else CN_BILLING_BASE


def origin(realm) -> str:
    return GLOBAL_ORIGIN if norm_realm(realm) == GLOBAL else CN_ORIGIN


def platform(realm) -> str:
    """`/v2/plugin/auth/state?platform=` 的取值。

    国际版用 `workbuddy-ai`（与桌面端 `WbVariant::oauth_platform` 同值）。
    **不要照抄管理端那种一律 `CLI` 的写法**：`CLI` 是桌面端
    `WORKBUDDY_PLATFORM` 的对应物，两版必须取各自的值，否则国际版的码
    会落到国内版产品线上。
    """
    return "workbuddy-ai" if norm_realm(realm) == GLOBAL else "workbuddy"


def accept_language(realm) -> str:
    return "en-US" if norm_realm(realm) == GLOBAL else "zh-CN"


def _ua(realm) -> str:
    """chat 域出站 UA：`WorkBuddy/<ver> <platform>/<ver> CLI/<cli>`。

    对齐上游 defaultWorkBuddyUAFor：国际版的平台段是 `WorkBuddy AI`。
    """
    platform_name = "WorkBuddy AI" if norm_realm(realm) == GLOBAL else "WorkBuddy"
    return f"WorkBuddy/5.5.4 {platform_name}/5.5.4 CLI/2.137.1"


def billing_ua() -> str:
    """billing 域 UA：**单段** `WorkBuddy/<ver>`，不带 CLI 段。

    官方客户端在 banner / 签到 / trial 这类白名单接口显式覆写 UA 为单段形态，
    RestOperations 层的 CLI 扩展段被业务层覆盖掉。发多段会被当成网关流量。
    """
    return "WorkBuddy/5.5.4"


def device_fingerprint_headers(uid) -> dict:
    """按账号稳定派生的设备指纹头（X-Machine-ID / X-Session-ID）。

    对齐上游 `deriveAccountStableID`：

        sha256("wb2a:" + purpose + ":" + uid) 前 18 字节的 hex = 36 字符

    语义是「每个账号一台固定虚拟设备」：跨重启恒定、账号间互异、同 uid 恒同值。
    **派生方式必须与上游逐字一致**，否则同一个账号在「经上游」与「本服务直连」
    两条路上会被认成两台设备，反而制造出可被关联的异常。
    """
    u = str(uid or "").strip()
    if not u:
        return {}
    return {
        "X-Machine-ID": hashlib.sha256(f"wb2a:machine:{u}".encode()).hexdigest()[:36],
        "X-Session-ID": hashlib.sha256(f"wb2a:session:{u}".encode()).hexdigest()[:36],
    }


def headers(realm, token=None, uid=None) -> dict:
    """该 realm 的通用请求头（Origin / Referer / UA / Accept-Language 随 realm 变）。

    token 非空附 Authorization；uid 非空附账号级设备指纹头。
    其余接口特有的身份头（X-User-Id 等）走 `billing_headers`。
    """
    r = norm_realm(realm)
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": accept_language(r),
        "X-Requested-With": "XMLHttpRequest",
        # 官方客户端的风控闸门头，所有 API 请求必带。缺了它就是
        # 「唯一没这个头」的流量，最容易被挑出来。
        "X-CodeBuddy-Request": "1",
        "User-Agent": _ua(r),
        "Origin": origin(r),
        "Referer": origin(r) + "/",
    }
    if token:
        h["Authorization"] = "Bearer " + str(token)
    h.update(device_fingerprint_headers(uid))
    return h


def billing_headers(realm, auth=None) -> dict:
    """billing 域请求头（签到 / 积分 / trial / 地区注册）。

    与 `headers()` 的差别是**身份头**：

      X-User-Id                    账号 uid（非空才发）
      X-Enterprise-Id / X-Tenant-Id 企业账号的两个同值头
      X-Domain                     账号域（非空才发）
      X-Device-Token               设备风控 token（上游注入了才转发）

    上游 Go 侧测试明确断言 trial 必须携带 X-User-Id；这一批端点正是我们
    直连、不经上游的那批，漏发身份头等于把「不像官方客户端」的形态留在
    最敏感的路上。
    """
    auth = auth if isinstance(auth, dict) else {}
    r = norm_realm(realm)
    uid = str(auth.get("uid") or "")
    h = headers(r, str(auth.get("access_token") or "") or None, uid or None)
    h["User-Agent"] = billing_ua()
    if uid:
        h["X-User-Id"] = uid
    ent = str(auth.get("enterprise_id") or "")
    if ent:
        h["X-Enterprise-Id"] = ent
        h["X-Tenant-Id"] = ent
    domain = str(auth.get("domain") or "")
    if domain:
        h["X-Domain"] = domain
    return h


def envelope(resp):
    """腾讯统一信封 `{code, msg, data}`；HTTP 4xx 也可能是业务信封。

    返回 (code, data)。解析不出 JSON 时用 HTTP 状态码当 code、None 当 data。
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

    上游给法不统一：优先 `expiresAt`（可能是秒也可能是毫秒，用 1e11 分界），
    其次 `expiresIn`（秒）。**两者都没有时必须报错**，不能默认给一个值 ——
    猜错会让账号在界面上显示「还有很久」而实际早已失效，用户不知道要重登。
    """
    raw = tokens.get("expiresAt") if isinstance(tokens, dict) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and raw > 0:
        return int(raw if raw >= 100000000000 else raw * 1000)
    seconds = tokens.get("expiresIn") if isinstance(tokens, dict) else None
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) \
            and math.isfinite(seconds) and seconds > 0:
        return int((now_seconds + seconds) * 1000)
    return 0
