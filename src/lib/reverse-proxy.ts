/**
 * 反代服务管理端 API 客户端（codebuddy2api + switch 账号库）。
 *
 * 与内置静态后台（/admin/）的差别：SPA 跨源调用 /admin/api/*，走
 * `X-Admin-Token` 头鉴权而不是 cookie（跨站 SameSite=strict cookie 不可用），
 * 因此服务端已开放 CORS 并支持头令牌。base URL 与令牌存 localStorage。
 */

const BASE_KEY = "wb.rp.base";
const TOKEN_KEY = "wb.rp.token";
const USER_KEY = "wb.rp.user";
export const DEFAULT_BASE = "http://127.0.0.1:8787";

export class RpError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.name = "RpError";
    this.status = status;
  }
}

function read(key: string, fallback = ""): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* localStorage 不可用时静默降级（内存会话） */
  }
}

let memBase = "";
let memToken = "";

export function getBaseUrl(): string {
  return read(BASE_KEY) || memBase || DEFAULT_BASE;
}

export function setBaseUrl(value: string): void {
  memBase = value.trim().replace(/\/+$/, "");
  write(BASE_KEY, memBase);
}

export function getToken(): string {
  return read(TOKEN_KEY) || memToken;
}

export function setToken(value: string | null): void {
  memToken = value ?? "";
  write(TOKEN_KEY, value);
}

export function getSessionUser(): RpSession | null {
  const raw = read(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as RpSession;
  } catch {
    return null;
  }
}

export function setSessionUser(value: RpSession | null): void {
  write(USER_KEY, value ? JSON.stringify(value) : null);
}

async function request<T>(path: string, options: { method?: string; body?: unknown; auth?: boolean } = {}): Promise<T> {
  const method = options.method ?? "GET";
  const token = getToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (options.auth !== false && token) headers["X-Admin-Token"] = token;
  let response: Response;
  try {
    response = await fetch(getBaseUrl() + "/admin/api/" + path.replace(/^\/+/, ""), {
      method,
      headers,
      credentials: "omit",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch {
    throw new RpError("无法连接反代服务，请确认它已启动且地址正确", 0);
  }
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    if (!response.ok) throw new RpError(`服务返回异常（HTTP ${response.status}）`, response.status);
    return undefined as T;
  }
  if (!response.ok) {
    throw new RpError(extractDetail(data) || `请求失败（HTTP ${response.status}）`, response.status);
  }
  return data as T;
}

function extractDetail(data: unknown): string {
  if (!data || typeof data !== "object") return "";
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const err = (detail as { error?: { message?: string } }).error;
    if (err && typeof err.message === "string") return err.message;
    return JSON.stringify(detail);
  }
  return "";
}

// ── 类型 ──────────────────────────────────────────────────────────────
export type RpSession = { username: string; role: "admin" | "viewer" | string; via?: string };

export type RpAccount = {
  id: string; name: string; enabled: boolean; active: boolean; realm?: string;
  nickname?: string; uid?: string; status?: string;
  pool_state?: string; remaining?: number | null; credits_updated?: number;
  credits_stale?: boolean; today_checked_in?: boolean; cooldown_until?: number;
  last_error?: string | null; expires_at?: number; expired?: boolean;
  refresh_available?: boolean; request_count?: number;
  /**
   * 派猫猫旅行的展示档位（与服务端 travel_label 一一对应）：
   * traveling 旅行中 / finished 今日已派 / no-buddy 无 Buddy / untraveled 未旅行。
   */
  travel_label?: "traveling" | "finished" | "no-buddy" | "untraveled" | string;
  /** 这条旅行记录是不是今天的。为假表示是上一次行程的残留（还没领到手）。 */
  travel_same_day?: boolean;
  travel_location?: string | null;
  /** 到达时间，上游 ms / s 两种形态都有，展示前要归一化。 */
  travel_arrive_at?: number | null;
  travel_reward?: number | string | null;
  travel_skip?: string | null;
  /**
   * 最近一次「对话活跃上报」被证明生效的日期（YYYY-MM-DD）。没生效过为 null。
   *
   * 注意这里的口径：只有回读到连登天数 > 0 才写它。上报拿到 200 但事件被上游
   * 静默丢弃时不写——那种情况下当天还要按 30 分钟节拍重试，写下去等于把
   * 自愈的路关死。所以「今天没上报过」和「今天上报失败」不是一回事，见 active_error。
   */
  active_date?: string | null;
  /** 今天是否已经确认生效**。跨日会自然变回 false（与 travel_same_day 同一口径）。 */
  active_today?: boolean;
  /** 回读到的 growth 连登天数。上报成功但它是 0 就是被上游静默丢弃了。 */
  active_streak?: number | null;
  /**
   * 上一次失败、或「报了但没能确认生效」的原因。
   *
   * 活跃上报的失败写的是这个字段，而不是通用的 last_error——所以卡片上要
   * 单独把它显示出来，否则「一直没点亮」这个状态在界面上是看不见的。
   * 成功会被服务端清空。
   */
  active_error?: string | null;
  /**
   * 当天的终局跳过（目前只有国际版账号）。这类账号今天不会再被尝试，
   * 因为重试一万次也不会变好、只会把上限 500 条的流水塞满同样的失败记录。
   * 跨日自动失效，所以界面应显示「今日不再尝试」而不是一条看起来待重试的失败。
   */
  active_skip?: string | null;
  /**
   * ── 上游 workbuddy2api 的权威冷却（模型级）────────────────────────
   *
   * 与上面的 `cooldown_until` 是两个不同的来源，别混：
   * · `cooldown_until` 是我们自己按错误码猜的（401/403 → 5 分钟，402/429 → 30 分钟），
   *   整个账号一个时间戳，没有模型维度；
   * · 这几个 `up_*` 字段是上游状态机给的：哪个模型被限流、各自几点恢复、为什么。
   *
   * `null` 表示上游读不到或没配（不是「没有冷却」）。界面必须把这两种
   * 情况分开——把「读不到」画成「没有冷却」是在替上游下结论。
   */
  up_cooling?: boolean | null;
  /** 账号级冷却剩余秒数。上游给的是快照**，服务端已按绝对时刻重算过。 */
  up_cool_remaining?: number | null;
  up_reason?: string | null;
  up_until?: number | null;
  /** 模型级限额台账。这是这个功能的重点：账号级说不出「哪个模型、几点恢复」。 */
  up_models?: RpUpstreamModel[];
  /** 连续失败次数（连败降权用）。它与限流退避都会显示成「冷却中」，但处置不同。 */
  up_consecutive_fails?: number | null;
  up_degrade_until?: number | null;
  up_disabled?: boolean | null;
  up_manual_disabled?: boolean | null;
};

/** 上游单个被限流模型的台账行（对应 Go 的 pool.RateLimitedModel）。 */
export type RpUpstreamModel = {
  model?: string;
  /** 该模型的独立冷却截止（可能已被上游的 soft_rate_max 截断过）。 */
  until?: number | null;
  /** 上游「将在…重置」的原始墙钟，未截断时与 until 同值。 */
  reset_at?: number | null;
  /** 还要等多少秒。由服务端按绝对时刻重算，可能为 null（时刻已过）。 */
  remaining_sec?: number | null;
  /** 触发原因。6004 开头 = 限流（等一会儿就好）；11102 开头 = 没开通这个模型（等不好）。 */
  reason?: string;
};

/**
 * 调度方式：
 * - manual      只用「当前账号」，绝不轮到别的号
 * - round_robin 轮流用（默认），把请求摊到所有可用账号上
 * - least_used  每次挑累计请求数最少的号
 */
export type RpRouting = "manual" | "round_robin" | "least_used";

export type RpPool = {
  routing?: RpRouting | string;
  auto_checkin?: boolean;
  checkin_time?: string;
  /** 每日自动派猫猫旅行。开启后会对账号发起真实写请求，所以默认关闭。 */
  auto_travel?: boolean;
  travel_time?: string;
  /** token 剩余寿命少于 keepalive_hours 小时就提前刷新。 */
  auto_keepalive?: boolean;
  keepalive_hours?: number;
  /** 每日对话活跃上报。开启后会以每个启用账号的身份向上游发真实事件，默认关闭。 */
  auto_active?: boolean;
  active_time?: string;
  /** 每个账号每天上报几条 chat_request_send 事件（1–5）。 */
  active_reports?: number;
  /**
   * 上游 workbuddy2api 的地址（如 http://127.0.0.1:7863）。
   * 空串 = 不去读上游，界面就不显示模型级冷却。默认空。
   */
  wb2api_base?: string;
  /** 上游要求 Bearer 鉴权时填它的 API key；留空表示上游未设鉴权。 */
  wb2api_key?: string;
  /**
   * 最近一次读上游 `/status` 失败的原因；空串 = 没问题。
   *
   * 与 `up_cooling` 是两个层次：这个说「读没读到」，那个说「读到了的话有没有冷却」。
   * 读不到时 `up_cooling` 是 `null`（未知），靠这个字段才能把「未知」讲清楚 ——
   * 绝不因为读不到就退化成 `false`（显示成「没有冷却」），
   * 否则会出现那个缺陷。
   *
   * 服务端放在 `pool` 段而不是逐行账号里：这个错误是整批共享的
   *（`upstream_cooldowns()` 一次调用拿整张 uid→冷却 的表），
   * 逐行重复既冗余，又会让人误读成「每个号各有各的错」。
   *
   * ⚠️ 只在「上游读不到」时才有意义，所以必须是可选的（服务端实际总是会发，
   * 但老版本服务端 / 无上游配置时不发，可选才能让 `?? ""` 兜底成立）。
   */
  wb2api_error?: string;
  /** spread 开启后，轮到哪个号就把它钉住 spread_minutes 分钟再换下一个。 */
  spread?: boolean;
  spread_minutes?: number;
  /** spread 的驻留租约，服务端维护；只读。 */
  lease?: { account_id: string; until: number } | null;
};

export type RpCheckinLog = {
  time: number; id: string; name: string; ok: boolean;
  /** 任务类别：签到 / 派猫猫旅行 / 凭据保活 / 活跃上报。旧数据没有这个字段，按签到算。 */
  kind?: "checkin" | "travel" | "keepalive" | "active" | string;
  credits?: number;
  message: string; source: "auto" | "manual" | "batch" | string;
};

/** 一次 /v1 请求的去向：用了哪个账号、什么模型、扣了多少。 */
export type RpRequestLog = {
  time: number;
  account_id: string | null;
  account_name: string | null;
  model: string | null;
  status: number;
  ok: boolean;
  tokens: number;
  credit: number;
  duration_ms: number;
  outcome: "success" | "upstream_error" | "no_account" | "rejected" | string;
  source: "api" | "test" | string;
  /** 走的是哪把密钥（`RpKey.id`）。null / 空 = 这次调用没走密钥（直连 / 被拒请求）。
   * 日志行本身不带分组信息，分组挂在密钥上：key_id -> RpKey.groups[] -> RpGroup.name。 */
  key_id?: string | null;
  /** 密钥的展示用短标记（如 sk-wb-abc…）。可选。 */
  key_hint?: string | null;
};

export type RpMetricItem = {
  time: number; path: string; source: string; status: number | null;
  ok: boolean; duration_ms: number; outcome: string;
};
export type RpMetrics = {
  started_at: number; completed: number; in_flight: number; succeeded: number;
  failed: number; success_rate: number | null; http_success_rate: number | null;
  avg_duration_ms: number | null; api_count: number; test_count: number;
  recent: RpMetricItem[];
};

export type RpKey = {
  id: string; name: string; hint: string; prefix?: string; enabled: boolean;
  /** 明文密钥。仅本机自用形态才有；旧条目可能为空串。 */
  secret?: string;
  expires_at: number | null; max_ips: number; ip_allowlist: string[];
  models: string[]; realm: string; quota: number; used_tokens: number;
  quota_credit: number; used_credit: number; created: number | null;
  last_used_at: number | null; ips: string[];
  /** 绑定的上游分组 id。空数组 = 不限制（可路由到全部账号）。 */
  groups?: string[];
};

export type RpOverview = {
  accounts: RpAccount[]; pool: RpPool; metrics: RpMetrics; keys: RpKey[];
  models: string[]; uptime: number;
  /** 运行时长。session = 本次进程（重启归零）；total = 累计（跨重启累加）。 */
  runtime?: { session: number; total: number; since: number; total_since: number };
  /** 客户端没带 model 时反代替它选的那一个（服务端 converter.DEFAULT_MODEL）。 */
  default_model?: string;
  checkin_logs?: RpCheckinLog[];
  request_logs?: RpRequestLog[];
  /** 上游分组清单：密钥表单据此渲染勾选，密钥列表据此把 id 显示成组名。 */
  groups?: RpGroup[];
  events: { time: number; model: string; ok: boolean; seconds: number }[];
};

export type RpToken = {
  id: string; name: string; prefix: string; scope: string; enabled: boolean;
  expires_at: number | null; created_at: number | null; created_by: string;
  last_used_at: number | null; last_used_ip: string;
};

export type RpUser = { username: string; role: "admin" | "viewer" | string };

export type RpSecurityConfig = { enabled: boolean; mode: "whitelist" | "blacklist" };
export type RpIpRule = { id: string; kind: "allow" | "deny"; cidr: string; note: string; created_at: number };
export type RpIpLog = { ts: number; ip: string; path: string; blocked: boolean; reason: string; ua: string };
export type RpAuditLog = { time: number; actor: string; action: string; target: string; detail: string };

export type RpKeyInput = {
  name: string; expires_at?: number | null; max_ips?: number; ip_allowlist?: string[];
  models?: string[]; realm?: string; quota?: number; quota_credit?: number;
  /** 绑定到的上游分组；不传/空数组 = 不限制。 */
  groups?: string[];
};

/** 上游分组：账号池的一个子集，密钥可以只落在某一组里。 */
export type RpGroup = {
  id: string; name: string; description: string; enabled: boolean;
  /** 该分组自己的调度方式；空串 = 跟随全局设置。 */
  routing: "" | RpRouting;
  created: number | null;
  accounts: string[]; account_names: string[]; account_count: number;
  /** 组里已经没有可调度账号了（空组，或成员全是已被隐藏的账号）。 */
  empty: boolean;
};

export type RpGroupAccount = { id: string; name: string; pool_state: string; realm: string };
export type RpGroupsPayload = { groups: RpGroup[]; accounts: RpGroupAccount[]; routings: string[] };

/** 账号出口线路：给单个账号指定一条命名 HTTP/HTTPS 代理。 */
export type RpEgressLine = {
  id: string; name: string;
  /** 已脱敏的展示地址（`user:*@host`）；明文口令只存在服务端。 */
  url: string;
  enabled: boolean; created: number | null;
  accounts: string[]; account_names: string[]; account_count: number;
};
export type RpEgressAccount = { id: string; name: string; pool_state: string; line_id: string };
export type RpEgressPayload = {
  lines: RpEgressLine[]; accounts: RpEgressAccount[];
  /** 绑到了已删除**线路上的账号：必须重新指定一条线路才能收尾。 */
  orphans: string[];
  /** 绑到了已停用**线路上的账号：线路还在，启用回来即可恢复（当前按直连出网）。 */
  stale: string[];
};

// ── 终端用户（members）────────────────────────────────────────────────
//
// ⚠️ 与上面的 RpUser 是两套完全不同的东西，别混：
//   · `RpUser`（`/admin/api/users`）= 后台账号：username + role ∈ admin/viewer，
//     用来登录 8787 管理后台。桌面端已有 UsersPanel。
//   · `RpMember`（`/admin/api/members`）= 用户管理：邮箱注册的终端用户，
//     有积分余额、是 API 调用的计费主体。
// 界面上分别叫「后台账号」与「用户管理」。

/** 终端用户角色。与后台账号的 admin/viewer 不是一套取值。 */
export type RpMemberRole = "user" | "admin";
/** 终端用户状态。 */
export type RpMemberStatus = "active" | "disabled";

export type RpMember = {
  id: string;
  email: string;
  role: RpMemberRole | string;
  status: RpMemberStatus | string;
  email_verified: boolean;
  /**
   * 余额的权威单位是整数「厘」（1 积分 = 1000 厘）。
   * 界面显示一律用 balance_credit，不要自己拿 milli 去除 —— 除法的舍入
   * 口径在服务端（redemption.from_milli），前端再除一次就会与网页端不一致。
   */
  balance_milli: number;
  balance_credit: number;
  created_at: number | null;
  last_login_at: number | null;
  /** 名下 API 密钥数量。密钥明细在详情的 keys 里。 */
  key_count: number;
  key_names: string[];
};

/** 无主密钥：owner 非空但已经对不上任何终端用户。它们在跑、在消耗上游，钱却没人认领。 */
export type RpOrphanKeys = { owner: string; count: number; names: string[] };

export type RpMemberList = {
  items: RpMember[];
  total: number;
  page: number;
  size: number;
  pages: number;
  /** 命中数超过服务端扫描上限，结果被截断。 */
  truncated: boolean;
  scanned: number;
  orphan_keys: RpOrphanKeys[];
};

export type RpMemberKeys = {
  id: string; name: string; prefix: string; enabled: boolean;
  created: number | null; last_used_at: number | null;
};

/**
 * 一条积分流水。字段名与 `redemption.list_flows` 的实际输出一字不差
 * （已在真机 8961 上抓过响应核对），两端共用同一份。
 *
 * 注意这里同时有 `milli` 和 `credit`：`milli` 是权威整数（厘），
 * `credit` 是服务端舍入好的积分数。界面直接用 `credit` —— 前端再除一次 1000，
 * 舍入口径一旦和服务端不一致，同一条流水在两端就会显示成两个数。
 */
export type RpMemberFlow = {
  /** Unix 秒。 */
  ts: number;
  /** 归属用户（uid）—— 明细里恒等于当前用户，列出来便于排查串号。 */
  username?: string;
  kind: string;
  /** 变动量，单位厘**。正数入账、负数出账。 */
  milli: number;
  /** 变动量，单位积分**（服务端舍入结果）。 */
  credit?: number;
  /** 变动后余额，单位厘。 */
  balance?: number;
  note?: string;
  /** 谁操作的：uid 或后台账号名。 */
  actor?: string;
  ref?: string;
};

export type RpMemberDetail = RpMember & {
  keys: RpMemberKeys[];
  flows: RpMemberFlow[];
};

export type RpMemberStats = {
  total: number; active: number; disabled: number; admins: number;
  balance_milli: number; balance_credit: number;
  keys_total: number; orphan_keys: number;
};

/** 调余额的返回。delta_credit 回显本次增量，便于界面做二次确认文案。 */
export type RpBalanceResult = {
  id: string; balance_milli: number; balance_credit: number; delta_credit: number;
};

export type RpDeleteMemberResult = {
  ok: boolean; id: string; email: string;
  /** 解绑（不是删除**）的密钥数。删除后它们成为无主密钥，仍在消耗上游。 */
  keys_unbound: number;
  /** 强制删除时结清的余额（积分）。 */
  released_credit: number;
};

// ── 服务设置（/admin/api/settings）──────────────────────────────────────

export type RpSmtpSettings = {
  enabled: boolean;
  host: string;
  port: number;
  username: string;
  /**
   * 服务端只回显布尔「是否已配置」，绝不回显明文或密文。
   * 字段名 `has_password` 与服务端 `settings_store.view()` 一字不差。
   */
  has_password?: boolean;
  from_addr: string;
  use_ssl: boolean;
};

export type RpServiceSettings = {
  /**
   * 用户系统总闸。关掉后注册 / 登录 / 用户中心的会话身份
   * 全部不可用（403）。
   *
   * 边界：不影响 /v1 的 API Key 直连 —— 那是核心服务，不归这个闸管。
   * 已经发出去的密钥在总闸关闭后照常能用，否则这个开关就成了停机按钮。
   */
  user_system_enabled?: boolean;
  allow_register: boolean;
  require_email_code: boolean;
  smtp: RpSmtpSettings;
  site_name: string;
  allow_self_service_redeem: boolean;
  /** 邮件主题模板（两端共用）。 */
  email_subject?: string;
  /** 邮件正文模板。 */
  email_body?: string;
  /**
   * SMTP 是否真的可用（服务端判定的唯一真相源）。
   *
   * 必须用它来显示「已配置 / 未配置」，不要自己在界面上按 host 非空去猜 ——
   * 那就成了第二个真相源，会出现「页面说已配置、注册却说未配置 SMTP」的自相矛盾。
   */
  smtp_ready?: boolean;
  /**
   * 当前配置会导致「谁都注册不了」时，服务端给出的原因原文。
   * 非空 = 注册被锁死。这就是用户要的那句提示的来源。
   */
  register_locked_reason?: string;
};

/**
 * 测试发信的返回。
 *
 * 成功与失败用的是两个不同的键（来自 `mailer.send_test()`）：
 *   失败 → `{ok:false, error:"未配置 SMTP 主机"}`
 *   成功 → `{ok:true,  to:"<收件人>"}`
 * 两个键都声明成可选，取值一律走下面的 `smtpTestMessage()` ——
 * 只读一个键（比如只读 `message`）会让失败时弹一句没有信息量的「测试失败」，
 * 而管理员最需要的恰恰是「为什么失败」：没配主机？端口错？认证被拒？
 */
export type RpSmtpTestResult = {
  ok: boolean;
  /** 失败时的原因。这才是失败时该显示的文案。** */
  error?: string;
  /** 成功时的收件人。 */
  to?: string;
  /** 兼容旧版 / 其它实现可能用的字段名。 */
  message?: string;
};

/** 从测试发信结果里取出要显示的一句话（成功与失败都适用）。 */
export function smtpTestMessage(result: RpSmtpTestResult | null | undefined): string {
  if (!result) return "测试发信失败";
  if (result.ok) {
    return result.to ? "测试邮件已发往 " + result.to : "测试邮件已发出";
  }
  // 优先显示服务端给的原因；都没有才退回一句兜底文案。
  return result.error || result.message || "测试发信失败";
}

export type RpSettingsPatch = {
  /** 用户系统总闸。见 RpServiceSettings.user_system_enabled。 */
  user_system_enabled?: boolean;
  allow_register?: boolean;
  require_email_code?: boolean;
  site_name?: string;
  allow_self_service_redeem?: boolean;
  /**
   * 浅合并：只更新这里出现过的子键。
   * 不传 password = 不改密码；传 "" = 清空密码。
   */
  smtp?: Partial<RpSmtpSettings> & { password?: string };
};

// ── 接口 ──────────────────────────────────────────────────────────────
export const rp = {
  /** 多用户登录（用户名 + 口令，PBKDF2）。 */
  login: (username: string, password: string) =>
    request<{ csrf: string; token: string }>("login", { method: "POST", body: { username, password }, auth: false }),
  /** 旧形态：单密钥 ADMIN_KEY 登录（若服务端开启）。 */
  loginKey: (key: string) =>
    request<{ csrf: string; token: string }>("login", { method: "POST", body: { key }, auth: false }),
  /** 用「管理令牌」（wbt_…）换一个会话。换来的会话仍然进不了改用户/改令牌那类接口。 */
  loginToken: (token: string) =>
    request<{ csrf: string; token: string }>("login", { method: "POST", body: { token }, auth: false }),
  /** 服务端告诉登录页哪些入口在当前部署下真的可用（不需要登录）。 */
  loginOptions: () =>
    request<{ legacy_key: boolean; tokens: boolean }>("login-options", { auth: false }),
  session: () => request<RpSession & { csrf: string }>("session"),
  logout: () => request<{ ok: boolean }>("logout", { method: "POST" }),
  revokeSessions: () => request<{ ok: boolean; revoked: number }>("sessions/revoke", { method: "POST" }),

  overview: () => request<RpOverview>("overview"),
  poolSummary: () => request<{ accounts: RpAccount[]; pool: RpPool }>("pool/summary"),
  accountAction: (id: string, action: "refresh" | "status" | "checkin" | "travel" | "travel-claim" | "active") =>
    request<{ id: string; ok: boolean; message: string; remaining?: number
      claimed?: boolean; rewardCredit?: number | string; skip?: string | null; state?: string | null
      /** 活跃上报专属：实际发出去的事件条数 / 本轮配置条数 / 共用会话 id。 */
      sent?: number; count?: number; conversationId?: string
      /** 回读到的 growth 连登天数；streak_ok 为假表示 200 了但事件被静默丢弃。 */
      streak?: number; streak_ok?: boolean }>(
      `accounts/${id}/actions/${action}`, { method: "POST" }),
  poolAction: (action: "status" | "checkin" | "travel" | "travel-claim" | "active") =>
    request<{ results: { id: string; ok: boolean; message: string }[] }>(`pool/actions/${action}`, { method: "POST" }),
  poolSettings: (body: RpPool) => request<{ ok: boolean; pool: RpPool }>("pool/settings", { method: "PATCH", body }),
  /** 任务流水。`kind` 交给服务端过滤：流水是落盘的，本地过滤会漏掉被挤出这一页的记录。 */
  checkinLogs: (limit = 200, kind?: string) =>
    request<RpCheckinLog[]>(`pool/checkin-logs?limit=${limit}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`),
  clearCheckinLogs: (kind?: string) =>
    request<{ ok: boolean; removed: number }>(
      `pool/checkin-logs/clear${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`, { method: "POST" }),
  requestLogs: (limit = 200) => request<RpRequestLog[]>(`request-logs?limit=${limit}`),
  editAccount: (id: string, body: { name?: string; enabled?: boolean; active?: boolean }) =>
    request<{ ok: boolean }>(`accounts/${id}`, { method: "PATCH", body }),
  deleteAccount: (id: string) => request<{ ok: boolean; scope?: "service" | "file" }>(`accounts/${id}`, { method: "DELETE" }),
  addAccount: (body: { name?: string; credential: unknown }) =>
    request<{ id: string }>("accounts", { method: "POST", body }),

  createKey: (body: RpKeyInput) => request<{ id: string; key: string }>("keys", { method: "POST", body }),
  updateKey: (id: string, body: Partial<RpKeyInput> & { enabled?: boolean }) =>
    request<RpKey>(`keys/${id}`, { method: "PATCH", body }),
  resetKeyUsage: (id: string) => request<{ ok: boolean }>(`keys/${id}/reset-usage`, { method: "POST" }),
  deleteKey: (id: string) => request<{ ok: boolean }>(`keys/${id}`, { method: "DELETE" }),
  checkModels: (models: string[]) => request<{ checked: boolean; unknown: string[] }>("keys/check-models", { method: "POST", body: { models } }),

  listTokens: () => request<RpToken[]>("tokens"),
  createToken: (name: string, scope: string, expires_at?: number | null) =>
    request<RpToken & { token: string }>("tokens", { method: "POST", body: { name, scope, expires_at } }),
  updateToken: (id: string, body: { name?: string; scope?: string; enabled?: boolean; expires_at?: number | null }) =>
    request<RpToken>(`tokens/${id}`, { method: "PATCH", body }),
  deleteToken: (id: string) => request<{ ok: boolean }>(`tokens/${id}`, { method: "DELETE" }),

  listUsers: () => request<RpUser[]>("users"),
  addUser: (username: string, password: string, role: string) =>
    request<RpUser>("users", { method: "POST", body: { username, password, role } }),
  updateUser: (username: string, body: { password?: string; role?: string }) =>
    request<RpUser & { sessions_revoked?: boolean; relogin_required?: boolean }>(`users/${encodeURIComponent(username)}`, { method: "PATCH", body }),
  deleteUser: (username: string) => request<{ ok: boolean }>(`users/${encodeURIComponent(username)}`, { method: "DELETE" }),

  securityConfig: () => request<RpSecurityConfig>("security/config"),
  setSecurityConfig: (body: RpSecurityConfig) => request<RpSecurityConfig>("security/config", { method: "POST", body }),
  ipRules: () => request<RpIpRule[]>("security/rules"),
  addIpRule: (kind: string, cidr: string, note: string) =>
    request<RpIpRule>("security/rules", { method: "POST", body: { kind, cidr, note } }),
  deleteIpRule: (id: string) => request<{ ok: boolean }>(`security/rules/${id}`, { method: "DELETE" }),
  ipLogs: (limit = 200) => request<RpIpLog[]>(`security/logs?limit=${limit}`),
  clearIpLogs: () => request<{ ok: boolean }>("security/logs/clear", { method: "POST" }),
  auditLogs: (limit = 200) => request<RpAuditLog[]>(`audit-logs?limit=${limit}`),
  clearAuditLogs: () => request<{ ok: boolean; removed: number }>("audit-logs/clear", { method: "POST" }),

  oauthStart: (name?: string) => request<{ id: string; url: string; expires_at: number }>("oauth/start", { method: "POST", body: { name } }),
  oauthPoll: (id: string) => request<{ status: string; updated?: boolean }>(`oauth/${id}/poll`, { method: "POST" }),
  oauthCancel: (id: string) => request<{ ok: boolean }>(`oauth/${id}`, { method: "DELETE" }),

  testModel: (model: string, prompt: string) =>
    request<{ ok: boolean; status?: number; answer?: string; error?: string; seconds: number; usage?: { total_tokens?: number } }>(
      "test", { method: "POST", body: { model, prompt } }),

  // 上游分组：把账号池切成互不干扰的子池，密钥只落在自己那一组里。
  groups: () => request<RpGroupsPayload>("groups"),
  createGroup: (body: { name: string; description?: string; accounts?: string[]; routing?: string; enabled?: boolean }) =>
    request<{ ok: boolean; id: string; name: string } & { groups: RpGroup[] }>("groups", { method: "POST", body }),
  updateGroup: (id: string, body: Partial<Pick<RpGroup, "name" | "description" | "enabled" | "routing">> & { accounts?: string[] }) =>
    request<{ ok: boolean; groups: RpGroup[] }>(`groups/${id}`, { method: "PATCH", body }),
  deleteGroup: (id: string) =>
    request<{ ok: boolean; removed: number; unbound_keys: number; groups: RpGroup[] }>(`groups/${id}`, { method: "DELETE" }),

  // 出口线路：按账号绑命名代理。
  egress: () => request<RpEgressPayload>("egress"),
  createEgressLine: (body: { name: string; url: string; enabled?: boolean }) =>
    request<{ ok: boolean; id: string; name: string } & { lines: RpEgressLine[] }>("egress/lines", { method: "POST", body }),
  updateEgressLine: (id: string, body: Partial<Pick<RpEgressLine, "name" | "enabled">> & { url?: string }) =>
    request<{ ok: boolean; lines: RpEgressLine[] }>(`egress/lines/${id}`, { method: "PATCH", body }),
  deleteEgressLine: (id: string) =>
    request<{ ok: boolean; freed: number; lines: RpEgressLine[] }>(`egress/lines/${id}`, { method: "DELETE" }),
  /** lineId 传空串表示解绑，该账号回落到直连。 */
  assignEgress: (accountId: string, lineId: string) =>
    request<{ ok: boolean; lines: RpEgressLine[] }>("egress/assign", { method: "POST", body: { account_id: accountId, line_id: lineId } }),

  // ── 用户管理（终端用户 / members）────────────────────────────────────
  //
  // ⚠️ 这些不是上面那组 listUsers / addUser / updateUser / deleteUser。
  // 那组打的是 `/admin/api/users`，管的是后台账号（登录 8787 用的 username）。
  // 这组打的是 `/admin/api/members`，管的是邮箱注册的终端用户（有积分余额）。
  // 两套东西永不共用端点。
  //
  // 守卫分档（服务端实现）：
  //   · 列表 / 详情 → viewer：只读角色也能看
  //   · 改角色 / 启停 → admin
  //   · 调余额 / 删用户 → require_session_admin：拒绝 wbt_ 令牌身份
  // 所以桌面端用「账号密码登录」换来的会话令牌调用全部可用（见 lib 顶部注释：
  // `X-Admin-Token` 头走的是 `security.current_user`，与 cookie 等价）；
  // 但用「管理令牌」登录时，调余额 / 删用户会被 403 —— 这是有意的，
  // 界面要把那句 403 原样显示出来，不要吞掉。
  /**
   * 管理员分发账号（`POST /admin/api/members`）。
   *
   * 这条是「未配置 SMTP 时用户注册不了」的唯一出路 —— 那句提示的原文就是
   * 「或者给您分发账号登录」。所以它必须和「关闭验证码注册」一起出现在服务设置旁边，
   * 否则管理员看到提示却无处可去。
   *
   * 守卫是 `admin`（不是 session_admin）：建号能造出管理员，但可逆（能删能降级）。
   * 服务端默认 `email_verified=true` —— 管理员亲手分发等于已经确认过身份，
   * 不存在「验证过没有」的问题。
   */
  createMember: (body: { email: string; password: string; role?: string; email_verified?: boolean }) =>
    request<RpMember>("members", { method: "POST", body }),
  listMembers: (params: { q?: string; page?: number; size?: number } = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.page) search.set("page", String(params.page));
    if (params.size) search.set("size", String(params.size));
    const query = search.toString();
    return request<RpMemberList>("members" + (query ? "?" + query : ""));
  },
  memberStats: () => request<RpMemberStats>("members/stats"),
  /** 详情带名下密钥与最近流水；flows 是流水条数上限（服务端封顶 200）。 */
  memberDetail: (uid: string, flows = 50) =>
    request<RpMemberDetail>("members/" + encodeURIComponent(uid) + "?flows=" + flows),
  /** 改角色 / 启用禁用。禁用或改角色会立刻踢掉该用户的全部会话**。 */
  updateMember: (uid: string, body: { role?: string; status?: string }) =>
    request<RpMember & { sessions_revoked?: number; relogin_required?: boolean }>(
      "members/" + encodeURIComponent(uid), { method: "PATCH", body }),
  /**
   * 调余额。必须走增量 delta_credit，没有「设为 N」的语义。
   *
   * 服务端只做增量：设值语义在并发下会吞掉别人的充值
   * （A 读到 100 → 想设 50 → B 充了 20 → A 写入 50，那 20 就没了）。
   * 界面因此也用「+ / −」的增量输入，不要做一个「余额 = __」的输入框。
   */
  adjustMemberBalance: (uid: string, deltaCredit: number, reason: string) =>
    request<RpBalanceResult>("members/" + encodeURIComponent(uid) + "/balance",
      { method: "POST", body: { delta_credit: deltaCredit, reason } }),
  /**
   * 删用户。余额非 0 时服务端回 409，必须在界面上把这句话显示出来，
   * 并引导管理员改用「强制删除」。force=true 会记一笔负流水结清余额。
   */
  deleteMember: (uid: string, force = false) =>
    request<RpDeleteMemberResult>(
      "members/" + encodeURIComponent(uid) + (force ? "?force=true" : ""),
      { method: "DELETE" }),

  // ── 服务设置（两端共用同一套接口与字段名）──────────────────────────────
  /** 读全部设置。SMTP 密码只回显「是否已配置」，永不回显明文。 */
  settings: () => request<RpServiceSettings>("settings"),
  /**
   * 改设置。浅合并：只更新请求体里出现过的键。
   *
   * 所以「只想改站点名」就只传 site_name，不要把整个 settings 原样发回来 ——
   * 那会把 SMTP 配置（含加密后的密码）一起覆盖掉，而两端各自拼整份对象时
   * 拼法一旦不同，就是「网页端改完桌面端没了」。
   */
  saveSettings: (body: RpSettingsPatch) => request<RpServiceSettings>("settings", { method: "PUT", body }),
  /** 测试发信。不配真 SMTP 时返回失败原因，不会挂住界面（服务端是同步纯函数判定）。 */
  testSmtp: (to?: string) =>
    request<RpSmtpTestResult>("settings/smtp-test", { method: "POST", body: to ? { to } : {} }),
};

/** 请求日志里 outcome 字段的中文与色调。 */
export const OUTCOME_LABELS: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "muted" }> = {
  success: { label: "成功", tone: "ok" },
  upstream_error: { label: "上游报错", tone: "bad" },
  no_account: { label: "无可用账号", tone: "warn" },
  rejected: { label: "被规则拒绝", tone: "warn" },
};

export const POOL_STATE_LABELS: Record<string, { label: string; tone: "ok" | "warn" | "bad" | "muted" }> = {
  available: { label: "可用", tone: "ok" },
  cooling: { label: "冷却中", tone: "warn" },
  paused: { label: "已暂停", tone: "muted" },
  exhausted: { label: "积分耗尽", tone: "warn" },
  invalid: { label: "凭据异常", tone: "bad" },
  missing: { label: "缺失", tone: "bad" },
};

/**
 * 终端用户角色 / 状态的中文口径。两端共用这一份（sync-spec「显示口径」）。
 *
 * 刻意与后台账号的「管理员 / 只读」区分开：那套是 admin/viewer（控制台登录用），
 * 这套是 admin/user（终端用户）。界面混用会让管理员以为在改同一个东西。
 */
export const MEMBER_ROLE_LABELS: Record<string, string> = {
  admin: "管理员",
  user: "普通用户",
};

export const MEMBER_STATUS_LABELS: Record<string, string> = {
  active: "正常",
  disabled: "已禁用",
};

export function memberRoleLabel(role: string | undefined | null): string {
  const key = String(role || "");
  return MEMBER_ROLE_LABELS[key] ?? (key || "—");
}

export function memberStatusLabel(status: string | undefined | null): string {
  const key = String(status || "");
  return MEMBER_STATUS_LABELS[key] ?? (key || "—");
}

/**
 * 厘 → 积分。只在显示层用，且固定 3 位小数（1 积分 = 1000 厘）。
 *
 * 服务端已经算好 balance_credit，正常情况下直接用那个字段；
 * 这个函数是给「流水里的 milli」这类只有厘的地方兜底用的。
 * 用整数除法 + 定点格式化，不做浮点累加 —— 本项目被 banker's rounding 坑过。
 */
export function creditFromMilli(milli: number | null | undefined): number {
  const value = Number(milli ?? 0);
  if (!Number.isFinite(value)) return 0;
  return Math.round(value) / 1000;
}

/** 积分显示：统一 3 位小数、去掉无意义的尾零（100 → "100"、1234 → "1.234"）。 */
export function fmtCredit(credit: number | null | undefined): string {
  const value = Number(credit ?? 0);
  if (!Number.isFinite(value)) return "0";
  const fixed = (Math.round(value * 1000) / 1000).toFixed(3);
  return fixed.replace(/\.?0+$/, "") || "0";
}

/**
 * 积分流水的时间格式：本地时区 YYYY-MM-DD HH:mm（sync-spec「显示口径」）。
 * 与 rp-shared 的 fmtTime 不同 —— 那个按秒、带秒且用 toLocaleString 的完整格式；
 * 流水表列多，这里要的是固定宽度。
 */
export function fmtCreditTime(seconds: number | null | undefined): string {
  const value = Number(seconds ?? 0);
  if (!value) return "—";
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
    + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

/**
 * 积分流水 kind 的中文。
 *
 * 词表来自后端实际会写出的 kind（`redemption.grant/spend` 的全部调用点）：
 * · `recharge`      —— `server.py` 的后台充值
 * · `admin_adjust`  —— `members.adjust_balance`（本面板的调余额）
 * · `adjust`        —— `accounts.adjust_balance` 的默认值
 * · `member_deleted` / `spend` —— 删号结清与扣费
 *
 * 未知 kind 原样显示而不是留空：服务端将来加新类别时，
 * 留空会让管理员以为这条流水坏了，而原样显示至少还能看出是什么。
 */
export const FLOW_KIND_LABELS: Record<string, string> = {
  recharge: "充值",
  admin_adjust: "管理员调整",
  adjust: "余额调整",
  spend: "消费扣费",
  member_deleted: "删号结清",
  refund: "退款",
};

export function flowKindLabel(kind: string | undefined | null): string {
  const key = String(kind || "");
  return FLOW_KIND_LABELS[key] ?? (key || "—");
}

/** SMTP 未配置且开着验证码开关时，用户指定的原文（两端一字不差）。 */
export const SMTP_MISSING_REGISTER_NOTICE =
  "未配置 SMTP，当前无法注册 请联系管理员关闭验证码验证 或者给您分发账号登录";

export const ROUTING_LABELS: Record<string, { label: string; desc: string }> = {
  round_robin: {
    label: "轮流使用",
    desc: "每个请求依次落到不同的可用账号上。多个号分摊调用量，单号被高频使用的风险最低。",
  },
  manual: {
    label: "只用当前账号",
    desc: "所有请求都走「当前账号」这一个号，不会自动切到其它号。账号不可用时直接报错，不会偷换。",
  },
  least_used: {
    label: "最少使用优先",
    desc: "每次挑累计请求数最少的可用账号，让各号的用量尽量拉平。",
  },
};

export function apiBaseFromService(): string {
  return getBaseUrl().replace(/\/+$/, "");
}

export function modelsEndpoint(): string {
  return apiBaseFromService() + "/v1";
}
