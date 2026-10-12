"use strict";
const $ = id => document.getElementById(id);
let csrf = "", overview = null, pendingConfirm = null, page = "dashboard", busy = false;
let accountMode = "browser", oauthFlow = null, oauthTimer = null, oauthGeneration = 0;
let requestLogs = null, egressData = null;
/* 账号池状态 → [中文, 色调]。原先这份表是 renderAccounts 里的局部变量，名字还叫 `labels`
   ——和全局的页面标题表同名，容易看串；提出来两处共用。 */
const POOL_STATES = {available: ["可用", "green"], cooling: ["冷却中", "amber"], paused: ["已暂停", ""], exhausted: ["积分耗尽", "amber"], invalid: ["凭据异常", "red"]};
const poolPill = state => { const [text, tone] = POOL_STATES[state] || ["待查询", ""]; return `<span class="pill ${tone}">${esc(text)}</span>`; };
const GROUP_ROUTINGS = {round_robin: "轮转", manual: "手动指定", least_used: "最少使用优先"};
const labels = {
  dashboard: ["让每个账号，各尽其用", "WORKBUDDY WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  groups: ["上游分组", "ACCOUNT GROUPS", "把账号池切成若干子集，让不同的密钥只落在各自的账号上。"],
  egress: ["出口线路", "EGRESS LINES", "给账号指定固定的出口代理，并查看每个账号当前走哪条线路。"],
  logs: ["请求日志", "REQUEST ACTIVITY", "每个请求用了哪个账号、什么模型，消耗了多少 Token 与积分。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  billing: ["充值与兑换码", "BILLING & VOUCHERS", "为用户充值、生成兑换码，并核对每一笔资金流水。"],
  members: ["用户管理", "END USERS", "邮箱注册出来的终端用户：余额、角色、启停与名下密钥。与「后台账号」是两套东西。"],
  settings: ["服务设置", "SERVICE SETTINGS", "开放注册、邮箱验证码与 SMTP 发信。改这里会直接影响所有人都能不能注册。"],
  test: ["连接测试", "CONNECTION LAB", "从当前账号发起请求，确认模型能否正常响应。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"]
};
/* 线性图标。原先用 👁 / 🙈 emoji，与整页的线性描边风格不一致，也受系统 emoji 字体影响。 */
const ICON_EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.2 12S5.6 5.6 12 5.6 21.8 12 21.8 12 18.4 18.4 12 18.4 2.2 12 2.2 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4.5 7.4C2.9 9 2.2 12 2.2 12s3.4 6.4 9.8 6.4c1.5 0 2.8-.33 3.95-.85"/><path d="M9.6 5.9A9.7 9.7 0 0 1 12 5.6c6.4 0 9.8 6.4 9.8 6.4a17 17 0 0 1-2.6 3.5"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M3.2 3.2 20.8 20.8"/></svg>';
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = ts => ts ? new Date(ts).toLocaleString("zh-CN", {hour12:false}) : "未提供";
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").hidden = true, 4500); }
function showLogin() {
  csrf = ""; overview = null; $("workspace").hidden = true; $("login").hidden = false; $("boot").hidden = true;
  document.querySelectorAll("dialog[open]").forEach(d => d.close());
  setLoginMode("user");
}
/* 登录方式切换。三种凭据共用一个 /admin/api/login，服务端靠请求体里出现的是
   key / token / username 来分流。切面板时把非当前面板的输入框 disable 掉：
   隐藏面板里留着 required 的输入框，浏览器提交时会报 "not focusable"，
   而且这个错用户根本看不见，只会觉得「点了没反应」。 */
function setLoginMode(mode) {
  document.querySelectorAll("[data-login-mode]").forEach(b => {
    const on = b.dataset.loginMode === mode;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll("[data-login-pane]").forEach(pane => {
    const on = pane.dataset.loginPane === mode;
    pane.hidden = !on;
    // 顺带清空：切走就不该把上一个凭据留在隐藏字段里
    pane.querySelectorAll("input").forEach(i => { i.disabled = !on; i.value = ""; });
  });
  const error = $("login-error"); if (error) error.textContent = "";
}
function loginPayload() {
  const active = document.querySelector("[data-login-mode].is-active");
  const mode = active ? active.dataset.loginMode : "user";
  if (mode === "key") return {key: $("admin-key").value.trim()};
  if (mode === "token") return {token: $("login-token").value.trim()};
  return {username: $("login-username").value.trim(), password: $("login-password").value};
}
async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {"Content-Type":"application/json", ...(method !== "GET" ? {"X-CSRF-Token":csrf} : {})};
  const response = await fetch("/admin/api/" + path, {method, headers, credentials:"same-origin", body:options.body === undefined ? undefined : JSON.stringify(options.body)});
  let data; try { data = await response.json(); } catch { throw new Error("服务器暂时不可用，请稍后重试"); }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") showLogin();
    throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请检查输入后重试");
  }
  return data;
}
function goPage(next) {
  if (!(next in labels)) next = "dashboard";
  page = next;
  document.querySelectorAll(".page-panel").forEach(el => el.hidden = el.id !== "page-" + next);
  document.querySelectorAll("[data-page]").forEach(el => { el.classList.toggle("selected", el.dataset.page === next); el.setAttribute("aria-current", el.dataset.page === next ? "page" : "false"); });
  const [title, kicker, desc] = labels[next];
  $("page-title").replaceChildren(document.createTextNode(title));
  const dot = document.createElement("span"); dot.className = "title-dot"; dot.textContent = "."; $("page-title").append(dot);
  $("breadcrumb").textContent = next === "dashboard" ? "概览" : title; $("page-kicker").textContent = kicker; $("page-desc").textContent = desc;
  history.replaceState(null, "", "#" + next);
  // 日志 / 出口线路 / 自动任务流水的数据都来自独立端点，切过来时立刻拉一次，
  // 不等 30 秒的轮询——否则新切进来的页面会先空着一张表杵在那里。
  // 分组不在这里拉：overview.groups 已经带着完整清单，render() 直接渲染即可。
  if (next === "logs" && csrf) loadLogs();
  syncLogsTimer();
  if (next === "egress" && csrf) loadEgress();
  if (next === "accounts" && csrf) loadTaskLogs();
  // 计费页同理。注意别只加在 refresh() 里：refresh 是 30 秒轮询触发的，
  // 用户点进页面后会先盯着一张全 "—" 的空表等半分钟 —— 表现得像坏了。
  if (next === "billing" && csrf) loadBilling();
  // 用户管理与服务设置也在这里拉：这两页不进 30 秒轮询 ——
  // 设置是低频操作，轮询只会白白重画表单，把管理员正在输入的内容冲掉。
  if (next === "members" && csrf) loadMembers();
  if (next === "settings" && csrf) loadSettings();
}
/* 运行时长。两个口径，都要显示：
 *   session = 本次进程起来多久（重启归零）
 *   total   = 累计运行（跨重启累加，从 state.json 读回）
 * 只显示 session 的话，用户重启一次就再也看不到自己实际用了多久。
 */
function fmtDuration(seconds, { compact = false } = {}) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (compact) {
    // 标题里给完整写法，鼠标悬停能看到。
    return d > 0 ? `${d} 天 ${h} 小时 ${m} 分` : h > 0 ? `${h} 小时 ${m} 分` : `${m} 分钟`;
  }
  if (d > 0) return `${d}天 ${h}小时`;
  if (h > 0) return `${h}小时 ${m}分`;
  if (m > 0) return `${m}分 ${s % 60}秒`;
  return `${s} 秒`;
}
let uptimeBase = null;      // { session, total, at }：用于本地每秒推进，不必等轮询
function renderUptime(sessionSec, runtime) {
  const el = $("uptime");
  if (!el) return;
  const session = runtime && typeof runtime.session === "number" ? runtime.session : sessionSec;
  const total = runtime && typeof runtime.total === "number" ? runtime.total : null;
  uptimeBase = { session: Number(session) || 0, total, at: Date.now() };
  paintUptime();
  el.title = total === null
    ? `本次运行 ${fmtDuration(session, { compact: true })}`
    : `本次运行 ${fmtDuration(session, { compact: true })} · 累计运行 ${fmtDuration(total, { compact: true })}`;
}
function paintUptime() {
  const el = $("uptime");
  if (!el || !uptimeBase) return;
  // 用本地时钟往前推，避免界面上的秒数要等 30 秒轮询才跳一下。
  const drift = Math.max(0, Math.floor((Date.now() - uptimeBase.at) / 1000));
  const session = uptimeBase.session + drift;
  const total = uptimeBase.total === null ? null : uptimeBase.total + drift;
  el.textContent = total === null
    ? `本次 ${fmtDuration(session)}`
    : `本次 ${fmtDuration(session)} · 累计 ${fmtDuration(total)}`;
}
// 每秒刷新一次时长显示（页面不可见时跳过，省点 CPU）。
setInterval(() => { if (!document.hidden && uptimeBase) paintUptime(); }, 1000);

// 页面切走/切回时，重新决定日志的实时刷新要不要跑。
document.addEventListener("visibilitychange", () => {
  if (typeof syncLogsTimer === "function") syncLogsTimer();
});

function render() {
  const {accounts, keys, models, uptime, events} = overview;
  const active = accounts.find(a => a.active && a.enabled);
  renderDashboard();
  $("account-count").textContent = accounts.length; $("account-badge").textContent = accounts.length;
  const rotating = overview.pool?.routing === "round_robin";
  $("active-name").textContent = rotating ? "账号池轮转" : active?.name || "未选择账号";
  $("active-state").textContent = rotating ? "新请求轮流分配，跳过暂停与冷却账号" : "新请求使用手动指定账号";
  $("test-account").textContent = active?.name || "尚未选择";
  renderUptime(uptime, overview.runtime);
  $("accounts-empty").hidden = accounts.length > 0;
  renderAccounts();
  renderGroups();
  const count = status => accounts.filter(a => a.pool_state === status).length;
  $("pool-counts").textContent = `全部 ${accounts.length}  ·  可用 ${count("available")}  ·  冷却 ${count("cooling")}  ·  暂停 ${count("paused")}  ·  耗尽 ${count("exhausted")}`;
  const known = accounts.filter(a => a.remaining !== null && a.remaining !== undefined);
  $("total-credits").textContent = known.length ? known.reduce((s,a) => s + a.remaining, 0).toLocaleString("zh-CN", {maximumFractionDigits:2}) + (known.length < accounts.length ? "（部分）" : "") : "待查询";
  if (!$("pool-settings").contains(document.activeElement)) {
    $("pool-routing").value = overview.pool?.routing || "manual"; $("auto-checkin").checked = overview.pool?.auto_checkin || false; $("checkin-time").value = overview.pool?.checkin_time || "09:00";
    $("auto-travel").checked = overview.pool?.auto_travel || false; $("travel-time").value = overview.pool?.travel_time || "08:30";
    $("auto-keepalive").checked = overview.pool?.auto_keepalive !== false; $("keepalive-hours").value = overview.pool?.keepalive_hours ?? 6;
    $("auto-active").checked = overview.pool?.auto_active || false; $("active-time").value = overview.pool?.active_time || "10:00"; $("active-reports").value = overview.pool?.active_reports ?? 1;
    // 上游接入点：用 ?? "" 而不是 || —— 空串本身是有效状态（= 不读上游），
    // 用 || 会让它退到默认值，用户就永远清不掉已填的地址。
    $("wb2api-base").value = overview.pool?.wb2api_base ?? ""; $("wb2api-key").value = overview.pool?.wb2api_key ?? "";
  }
  $("keys-body").innerHTML = keys.map(k => `<tr><td><strong>${esc(k.name)}</strong></td><td><div class="secret-cell"><code data-secret="${esc(k.secret || k.hint)}" data-hidden="1">${esc(k.secret ? "•".repeat(12) : k.hint)}</code><button class="text-button reveal" data-reveal="${k.id}" title="显示 / 隐藏密钥" aria-label="显示或隐藏密钥">${ICON_EYE}</button></div></td><td class="muted mono">${esc(stamp(k.created * 1000))}</td><td>${groupTags(k.groups)}</td><td class="align-right"><div class="actions"><button class="danger" data-revoke="${k.id}">撤销</button></div></td></tr>`).join("");
  const selected = $("model").value || overview.default_model || "deepseek-v4.1-flash";
  $("model").replaceChildren(...models.map(model => { const o = document.createElement("option"); o.value = o.textContent = model; return o; }));
  if (models.includes(selected)) $("model").value = selected;
  $("test-submit").disabled = !active || busy;
  $("test-history").innerHTML = events.length ? events.map(e => `<div class="history-row"><span class="mono muted">${esc(stamp(e.time * 1000))}</span><strong>${esc(e.model)}</strong><span class="pill ${e.ok ? "green" : "red"}">${e.ok ? "成功" : "失败"}</span><span class="mono">${e.seconds}s</span></div>`).join("") : '<p class="history-empty">暂无测试记录，发送第一条测试消息。</p>';
}
function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Number(value).toLocaleString("zh-CN",{maximumFractionDigits:2});
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  const known = accounts.filter(a=>typeof a.remaining === "number");
  $("dash-credits").textContent = known.length ? fmt(known.reduce((s,a)=>s+a.remaining,0)) : "—";
  $("dash-credit-note").textContent = known.length < accounts.length ? `已查询 ${known.length}/${accounts.length} 个账号，余额可能不完整` : accounts.some(a=>a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
  $("dash-requests").textContent = fmt(m.completed || 0);
  $("dash-request-note").textContent = `API ${m.api_count || 0} · 后台测试 ${m.test_count || 0}`;
  $("dash-success").textContent = rate(m.success_rate); $("dash-http").textContent = rate(m.http_success_rate);
  $("dash-failed").textContent = m.failed || 0; $("dash-inflight").textContent = m.in_flight || 0;
  $("dash-latency").textContent = m.avg_duration_ms === null || m.avg_duration_ms === undefined ? "—" : fmt(m.avg_duration_ms) + " ms";
  $("dash-since").textContent = "统计开始于 " + stamp((m.started_at || 0)*1000);
  $("dash-routing").textContent = pool?.routing === "round_robin" ? "轮流分配请求，自动跳过不可用账号" : "手动指定账号模式";
  const states={available:"可用",paused:"已暂停",cooling:"冷却中",exhausted:"积分耗尽",invalid:"凭据异常"};
  $("dash-account-list").innerHTML = accounts.slice(0,8).map(a=>`<div class="dashboard-account"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}</strong><small class="cell-note">${esc(a.uid || a.nickname)}</small></div><div class="dashboard-account-credit"><strong>${a.remaining === null || a.remaining === undefined ? "待查询" : fmt(a.remaining)}</strong><small class="cell-note">积分</small></div><span class="pill ${a.pool_state === 'available' ? 'green' : 'amber'}">${states[a.pool_state] || '待查询'}</span></div>`).join("") || '<p class="history-empty">尚未添加账号，点击“添加账号”开始。</p>';
  if(accounts.length > 8) $("dash-account-list").insertAdjacentHTML("beforeend",'<p class="muted">更多账号请前往账号池查看。</p>');
  const outcomes={success:"完成",stream_error:"流式错误",interrupted:"未完整结束",http_error:"请求失败"};
  $("dash-recent-body").innerHTML=(m.recent || []).map(r=>`<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${r.source === 'test' ? '后台测试' : 'API'}<small class="cell-note mono">${esc(r.path)}</small></td><td><span class="pill ${r.ok ? 'green' : 'red'}">${r.status ?? '—'}</span><small class="cell-note">${outcomes[r.outcome] || '请求失败'}</small></td><td class="align-right mono">${fmt(r.duration_ms)} ms</td></tr>`).join("") || '<tr><td colspan="4" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}
document.querySelectorAll("[data-dashboard-page]").forEach(b=>b.addEventListener("click",()=>goPage(b.dataset.dashboardPage)));
$("dash-add").addEventListener("click",()=>openAccount());
$("dash-base").textContent=location.origin+"/v1";
$("dash-copy").addEventListener("click",()=>copy(location.origin+"/v1"));
/* 旅行档位与桌面端 travel.rs 的 display_label 同口径。`travel_same_day` 为假
   表示这是上一次行程的残留（还没领到手），此时不该显示「今日已派」。 */
const TRAVEL_LABELS = {traveling:"旅行中", finished:"今日已派", "no-buddy":"无 Buddy", untraveled:"未旅行"};
function travelNote(a) {
  const label = TRAVEL_LABELS[a.travel_label];
  if (!label) return "";
  const parts = [label];
  if (a.travel_label === "traveling" && a.travel_arrive_at) {
    parts.push(`预计 ${stamp(Number(a.travel_arrive_at) > 1e12 ? Number(a.travel_arrive_at) : Number(a.travel_arrive_at) * 1000)}`);
  }
  if (a.travel_location) parts.push(a.travel_location);
  if (a.travel_reward) parts.push(`+${a.travel_reward}`);
  // no-buddy / finished 跨日之后就不再提了，避免账号卡片上留一条过期状态。
  if (!a.travel_same_day && a.travel_label !== "traveling") return "";
  return `<small class="cell-note">${esc(parts.join(" · "))}</small>`;
}

/* 活跃上报的状态小字。三档，因为「报上去了」和「真的生效了」是两件事：
     · active_today            —— 有正面证据（回读到的连登天数 > 0），今天确实生效了；
     · active_skip             —— 当天有效的终局跳过（如国际版账号），今天不会再试；
     · active_error 且没生效   —— 报了但没证明生效，仍在按 30 分钟节拍重试。
   前两档跨日就不展示（照 travelNote 的口径，否则卡片上会一直挂着昨天那条）；
   第三档只在今天真的尝试过时才显示，靠 detail 里带出的原因说明「还在重试」。 */
function activeNote(a) {
  const parts = [];
  if (a.active_today) {
    parts.push("今日已活跃上报");
    if (a.active_streak) parts.push(`连登 ${esc(String(a.active_streak))} 天`);
  } else if (a.active_skip) {
    parts.push("今日跳过活跃上报");
  } else if (a.active_error) {
    // 服务端只在今天真跑过之后才写 active_error（成功会清空），所以到这里
    // 必然意味着「今天试过了、但还没被证明生效」。
    parts.push("活跃上报待确认");
  }
  if (!parts.length) return "";
  const note = `<small class="cell-note">${parts.join(" · ")}</small>`;
  // 原因单独一行、按错误色显示——它才是排查时真正要看的东西。
  const why = a.active_error && !a.active_today
    ? `<small class="cell-note field-error">${esc(a.active_error)}</small>` : "";
  return note + why;
}

/* 把秒数说成人话：4 分 30 秒 / 2 小时 5 分 / 3 天。
   上游给的是秒，直接摆「5400」没人算得出来是多少。
   超过一天就说「N 天」——那时候精确到分已经没有意义了。 */
function fmtLeft(sec) {
  const n = Number(sec);
  if (!isFinite(n) || n <= 0) return "";
  if (n >= 86400) return `${Math.floor(n / 86400)} 天`;
  if (n >= 3600) {
    const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60);
    return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
  }
  if (n >= 60) {
    const m = Math.floor(n / 60), s = Math.round(n % 60);
    return s ? `${m} 分 ${s} 秒` : `${m} 分`;
  }
  return `${Math.round(n)} 秒`;
}

/* 上游 workbuddy2api 的权威冷却（模型级）。
   为什么要单独一块、而不是并进上面那条「至 HH:MM」：两者来源完全不同——
   上面那条是我们**自己按错误码猜的**（整个账号一个时间戳），
   这一条是上游状态机给的（哪个模型、各自几点恢复、为什么）。
   混在一起用户就分不清「我该等还是该去查」。

   上游读不到时 up_cooling === null。这时**不能**画成「没有冷却」（那是在替上游下结论），
   但也不能什么都不画 —— 两者在界面上长得一模一样，用户无法区分
   「上游挂了，冷却状态未知」和「上游说真的没限流」，
   于是不知道该等还是该换个模型。

   所以这里读 pool 段的 wb2api_error（上游最近一次读取失败的原因）：
     有错 + up_cooling 为 null  -> 说明「读不到，状态未知」
     无错 + up_cooling 为 null  -> 上游压根没配，这一块确实不该出现
   三种状态就此两两可分：未知 / 无冷却 / 有冷却。 */
/* 上游读取失败的原因（空串 = 没问题）。来自 /admin/api/overview 的 pool 段。
   它和 up_cooling 是两个层次的信息：
     wb2api_error 说的是「我们**读没读到**上游」；
     up_cooling   说的是「读到了的话，上游**说**有没有冷却」。
   读不到时后者是 null（未知），把前者一起摆出来，用户才知道那是「不知道」。 */
function upstreamError() {
  const p = overview && overview.pool;
  return (p && typeof p.wb2api_error === "string") ? p.wb2api_error : "";
}


function upstreamNote(a) {
  if (a.up_cooling === null || a.up_cooling === undefined) {
    // 只有「确实配了上游、但读不到」才说这句话。没配时不显示 ——
    // 那是用户自己关掉的功能，不是故障。
    const err = upstreamError();
    if (!err) return "";
    return `<small class="cell-note up-note up-note-unknown" title="${esc(err)}">上游读不到，冷却状态未知</small>`;
  }
  const models = Array.isArray(a.up_models) ? a.up_models : [];
  // 台账里的原因分两类，处置方式完全不同：
  //   6004       → 这个模型被限流，等一会儿就好
  //   11102 等   → 这个号根本没有这个模型，等多久都不会好，该换模型
  const limited = [], missing = [];
  for (const m of models) {
    ((m.reason || "").startsWith("11102") ? missing : limited).push(
      m.remaining_sec ? `${m.model}（${fmtLeft(m.remaining_sec)}）` : m.model);
  }
  const parts = [];
  if (a.up_cooling) {
    const left = fmtLeft(a.up_cool_remaining);
    parts.push(left ? `上游冷却中 · 还要 ${left}` : "上游冷却中");
  }
  if (limited.length) parts.push(`限流模型：${limited.join("、")}`);
  if (missing.length) parts.push(`未开通模型：${missing.join("、")}`);
  // 连败降权与限流退避都会表现为「冷却中」，但一个该等、一个该查，必须分开说。
  if (a.up_degrade_until && a.up_degrade_until * 1000 > Date.now()) {
    parts.push(`连败降权 ${a.up_consecutive_fails || 0} 次`);
  }
  if (!parts.length) return "";
  return `<small class="cell-note up-note">${parts.map(esc).join(" · ")}</small>`;
}

function renderAccounts() {
  const query = $("account-search").value.toLowerCase(), filter = $("account-filter").value;
  const rows = overview.accounts.filter(a => (!query || [a.name,a.nickname,a.uid].join(" ").toLowerCase().includes(query)) && (filter === "all" || a.pool_state === filter));
  $("accounts-body").innerHTML = rows.map(a => {
    const status = POOL_STATES[a.pool_state] || ["待查询", ""];
    const credits = a.remaining === null || a.remaining === undefined ? "—" : Number(a.remaining).toLocaleString("zh-CN",{maximumFractionDigits:2});
    return `<tr><td><div class="account-cell"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}${a.active ? '<span class="mini-active">手动 / 测试账号</span>' : ""}</strong><small>${esc(a.uid || a.nickname)}</small></div></div></td><td><span class="pill ${status[1]}">${status[0]}</span><small class="cell-note">${a.today_checked_in ? "今日已签到" : "今日未确认签到"}</small>${travelNote(a)}${activeNote(a)}${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}${upstreamNote(a)}</td><td><strong class="credit-number">${credits}</strong><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td><td class="mono">${esc(stamp(a.expires_at))}<small class="cell-note">${a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新"}</small></td><td><div class="actions pool-actions">${a.enabled ? `<button data-action="status" data-id="${a.id}" title="查询积分与签到状态">查询积分</button><button data-action="checkin" data-id="${a.id}">签到</button><button data-action="travel" data-id="${a.id}" title="按状态机派一次猫猫旅行；已在旅途中会提示">派旅行</button><button data-action="travel-claim" data-id="${a.id}" title="回查官方状态，到点则领奖">领奖</button><button data-action="active" data-id="${a.id}" title="向上游发送一条 chat_request_send 事件，点亮 growth 连登">活跃</button><button data-action="refresh" data-id="${a.id}">刷新凭据</button>` : ""}${a.enabled && !a.active ? `<button data-action="activate" data-id="${a.id}">设为手动 / 测试</button>` : ""}<button data-action="rename" data-id="${a.id}">备注</button><button data-action="toggle" data-id="${a.id}">${a.enabled ? "暂停" : "恢复"}</button><button class="danger" data-action="delete" data-id="${a.id}">删除</button></div></td></tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="5" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
}
/* ---------- 上游分组 ---------- */
/* 密钥里存的是分组 id，表格上要显示成人看得懂的名字。分组被删掉后 id 可能悬空
   （服务端删组时会顺带解绑，但手工改过 state.json 的旧数据未必），退回显示 id 本身，
   总好过显示「未知」让人无从查起。 */
function groupTags(ids) {
  const list = ids || [];
  if (!list.length) return '<span class="muted">不限</span>';
  const names = new Map((overview?.groups || []).map(g => [g.id, g.name]));
  return `<div class="tag-list">${list.map(id => `<code title="${esc(id)}">${esc(names.get(id) || id)}</code>`).join("")}</div>`;
}

function renderGroups() {
  const groups = overview?.groups || [];
  $("group-badge").textContent = groups.length;
  $("groups-empty").hidden = groups.length > 0;
  $("groups-body").innerHTML = groups.map(g => {
    const members = g.account_names.length
      ? `<div class="tag-list">${g.account_names.slice(0, 6).map(n => `<code>${esc(n)}</code>`).join("")}${g.account_names.length > 6 ? `<span class="muted">+${g.account_names.length - 6}</span>` : ""}</div>`
      : '<span class="muted">空分组</span>';
    const routing = g.routing ? `<span class="mono">${esc(GROUP_ROUTINGS[g.routing] || g.routing)}</span>` : '<span class="muted">跟随全局</span>';
    // 停用 / 空组都会让绑它的密钥拿不到账号，必须在列表上就能看出来
    const state = !g.enabled
      ? '<span class="pill">已停用</span><small class="cell-note">绑它的密钥会拿不到账号</small>'
      : g.empty
        ? '<span class="pill amber">无可用账号</span><small class="cell-note">成员账号已全部隐藏或移除</small>'
        : '<span class="pill green">生效中</span>';
    return `<tr><td><strong>${esc(g.name)}</strong>${g.description ? `<small class="cell-note">${esc(g.description)}</small>` : ""}</td><td>${members}</td><td>${routing}</td><td>${state}</td><td class="align-right"><div class="actions"><button data-group-edit="${esc(g.id)}">编辑</button><button class="danger" data-group-delete="${esc(g.id)}">删除</button></div></td></tr>`;
  }).join("");
}

function openGroupDialog(group) {
  $("group-dialog-title").textContent = group ? "编辑分组" : "新建分组";
  $("group-id").value = group?.id || "";
  $("group-name").value = group?.name || "";
  $("group-desc").value = group?.description || "";
  $("group-routing").value = group?.routing || "";
  $("group-enabled").checked = group ? group.enabled : true;
  $("group-error").textContent = "";
  const selected = new Set(group?.accounts || []);
  const accounts = overview?.accounts || [];
  $("group-members").innerHTML = accounts.length
    ? accounts.map(a => `<label><input type="checkbox" value="${esc(a.id)}"${selected.has(a.id) ? " checked" : ""}><span>${esc(a.name)}</span><small class="cell-note">${esc((POOL_STATES[a.pool_state] || ["待查询"])[0])}</small></label>`).join("")
    : '<p class="check-list-empty">账号池还是空的，先到「账号池」添加账号。</p>';
  $("group-dialog").showModal();
}

$("add-group").addEventListener("click", () => openGroupDialog(null));
$("empty-add-group").addEventListener("click", () => openGroupDialog(null));
$("group-dialog").addEventListener("close", () => {
  $("group-form").reset(); $("group-id").value = ""; $("group-members").innerHTML = ""; $("group-error").textContent = "";
});
$("group-form").addEventListener("submit", async e => {
  e.preventDefault(); e.submitter.disabled = true;
  const gid = $("group-id").value;
  const body = {
    name: $("group-name").value.trim(),
    description: $("group-desc").value.trim(),
    routing: $("group-routing").value,
    enabled: $("group-enabled").checked,
    accounts: [...$("group-members").querySelectorAll("input[type=checkbox]:checked")].map(x => x.value),
  };
  try {
    if (gid) await api("groups/" + gid, {method: "PATCH", body});
    else await api("groups", {method: "POST", body});
    $("group-dialog").close(); await refresh(); toast(gid ? "分组已更新" : "分组已创建");
  } catch (err) { $("group-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
$("groups-body").addEventListener("click", e => {
  const edit = e.target.closest("[data-group-edit]");
  if (edit) { const g = (overview?.groups || []).find(x => x.id === edit.dataset.groupEdit); if (g) openGroupDialog(g); return; }
  const del = e.target.closest("[data-group-delete]"); if (!del) return;
  const g = (overview?.groups || []).find(x => x.id === del.dataset.groupDelete); if (!g) return;
  // 删除分组等于给绑它的密钥「放宽权限」——从「只能用组内账号」变回「能用全部账号」，
  // 所以要把解绑了几枚密钥写进提示里，不能只报一句「已删除」。
  confirmAction("删除这个分组？", `「${g.name}」将从分组列表移除，绑它的密钥会被解绑，并恢复成「可用全部账号」。`,
    () => api("groups/" + g.id, {method: "DELETE"}).then(r => r.unbound_keys ? `分组已删除，同时解绑了 ${r.unbound_keys} 枚密钥` : "分组已删除"));
});

/* ---------- 出口线路 ---------- */
async function loadEgress() {
  try {
    egressData = await api("egress");
    $("egress-error").hidden = true;
    renderEgress();
  } catch (e) {
    $("egress-error").textContent = "出口线路读取失败：" + e.message;
    $("egress-error").hidden = false;
  }
}

function renderEgress() {
  const lines = egressData?.lines || [];
  const accounts = egressData?.accounts || [];
  const orphans = egressData?.orphans || [];
  const stale = egressData?.stale || [];
  const nameOf = id => (accounts.find(a => a.id === id)?.name) || id;
  $("egress-badge").textContent = lines.length;
  $("egress-account-badge").textContent = accounts.length;
  $("egress-empty").hidden = lines.length > 0;
  // 「线路已删除」和「线路已停用」分开说：前者必须重新指定线路，后者启用回来就好
  $("egress-orphans").hidden = orphans.length === 0;
  if (orphans.length) $("egress-orphans").textContent = `有 ${orphans.length} 个账号绑在已删除的线路上（${orphans.map(nameOf).join("、")}），当前按直连出网。这条绑定已经无解，请在下方给它们重新指定线路。`;
  $("egress-stale").hidden = stale.length === 0;
  if (stale.length) $("egress-stale").textContent = `有 ${stale.length} 个账号绑在已停用的线路上（${stale.map(nameOf).join("、")}），当前按直连出网。想让它们恢复走代理，把那条线路启用回来即可，不必改账号的绑定。`;

  $("egress-body").innerHTML = lines.map(l => {
    const names = l.account_names || [];
    const bound = names.length
      ? `<div class="tag-list">${names.slice(0, 6).map(n => `<code>${esc(n)}</code>`).join("")}${names.length > 6 ? `<span class="muted">+${names.length - 6}</span>` : ""}</div>`
      : '<span class="muted">未绑定账号</span>';
    const state = l.enabled
      ? '<span class="pill green">启用中</span><small class="cell-note">该线路上的账号走此代理</small>'
      : '<span class="pill amber">已停用</span><small class="cell-note">退回直连，不再使用此代理</small>';
    return `<tr><td><strong>${esc(l.name)}</strong><small class="cell-note mono">${esc(l.id)}</small></td><td><code>${esc(l.url)}</code></td><td>${bound}</td><td>${state}</td><td class="align-right"><div class="actions"><button data-egress-edit="${esc(l.id)}">编辑</button><button class="danger" data-egress-delete="${esc(l.id)}">删除</button></div></td></tr>`;
  }).join("");

  const dead = new Set(orphans);
  $("egress-accounts-body").innerHTML = accounts.map(a => {
    const broken = dead.has(a.id);
    const options = '<option value="">直连（默认网络）</option>'
      + lines.map(l => `<option value="${esc(l.id)}"${l.id === a.line_id ? " selected" : ""}>${esc(l.name)}${l.enabled ? "" : "（已停用）"}</option>`).join("")
      + (broken ? `<option value="${esc(a.line_id)}" selected>已失效（${esc(a.line_id)}）</option>` : "");
    return `<tr><td><div class="account-cell"><span class="account-icon">${esc(a.name.slice(0, 1))}</span><div><strong>${esc(a.name)}</strong><small class="mono">${esc(a.id)}</small></div></div></td><td>${poolPill(a.pool_state)}</td><td><select class="line-picker" data-egress-assign="${esc(a.id)}" aria-label="为 ${esc(a.name)} 选择出口线路">${options}</select>${broken ? '<small class="cell-note field-error">线路已不存在，请重新选择</small>' : ""}</td></tr>`;
  }).join("") || '<tr><td colspan="3" class="history-empty">还没有账号，先到「账号池」添加。</td></tr>';
}

function openEgressDialog(line) {
  $("egress-dialog-title").textContent = line ? "编辑线路" : "新建线路";
  $("egress-id").value = line?.id || "";
  $("egress-name").value = line?.name || "";
  // 口令不回显：编辑时地址留空表示不改动，真改就整条重填。
  $("egress-url").value = "";
  $("egress-url").required = !line;
  $("egress-url").placeholder = line ? "留空表示不改动地址" : "http://user:pass@host:8080";
  $("egress-url-hint").hidden = !line;
  $("egress-url-hint").textContent = line ? `当前地址：${line.url}（口令不回显，要改就整条重填）` : "";
  $("egress-enabled").checked = line ? line.enabled : true;
  $("egress-error").textContent = "";
  $("egress-dialog").showModal();
}

$("add-egress").addEventListener("click", () => openEgressDialog(null));
$("empty-add-egress").addEventListener("click", () => openEgressDialog(null));
$("egress-dialog").addEventListener("close", () => {
  $("egress-form").reset(); $("egress-id").value = ""; $("egress-error").textContent = ""; $("egress-url-hint").hidden = true;
});
$("egress-form").addEventListener("submit", async e => {
  e.preventDefault(); e.submitter.disabled = true;
  const lid = $("egress-id").value, url = $("egress-url").value.trim();
  try {
    if (lid) {
      const body = {name: $("egress-name").value.trim(), enabled: $("egress-enabled").checked};
      if (url) body.url = url;
      await api("egress/lines/" + lid, {method: "PATCH", body});
    } else {
      if (!url) { $("egress-error").textContent = "请填写代理地址，例如 http://user:pass@host:8080"; return; }
      await api("egress/lines", {method: "POST", body: {name: $("egress-name").value.trim(), url, enabled: $("egress-enabled").checked}});
    }
    $("egress-dialog").close(); await loadEgress(); toast(lid ? "线路已更新" : "线路已创建");
  } catch (err) { $("egress-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
$("egress-body").addEventListener("click", e => {
  const edit = e.target.closest("[data-egress-edit]");
  if (edit) { const l = (egressData?.lines || []).find(x => x.id === edit.dataset.egressEdit); if (l) openEgressDialog(l); return; }
  const del = e.target.closest("[data-egress-delete]"); if (!del) return;
  const l = (egressData?.lines || []).find(x => x.id === del.dataset.egressDelete); if (!l) return;
  confirmAction("删除这条线路？", `「${l.name}」将被移除，挂在上面的账号会全部改回直连。`,
    () => api("egress/lines/" + l.id, {method: "DELETE"}).then(r => r.freed ? `线路已删除，${r.freed} 个账号改回直连` : "线路已删除"));
});
$("egress-accounts-body").addEventListener("change", async e => {
  const picker = e.target.closest("[data-egress-assign]"); if (!picker) return;
  const accountId = picker.dataset.egressAssign, lineId = picker.value;
  picker.disabled = true;
  try {
    await api("egress/assign", {method: "POST", body: {account_id: accountId, line_id: lineId}});
    toast(lineId ? "已改为走该出口线路" : "已改为直连");
  } catch (err) { toast(err.message); }
  finally { await loadEgress(); }
});

/* ---------- 请求日志 ---------- */
const LOG_OUTCOMES = {success:"成功", upstream_error:"上游报错", no_account:"无可用账号", rejected:"被规则拒绝"};
const LOG_TONES = {success:"green", upstream_error:"red", no_account:"amber", rejected:"amber"};
const logNumber = v => Number(v || 0).toLocaleString("zh-CN", {maximumFractionDigits: 0});
/* 积分单价很小（一次请求常常只有几位小数），不能按整数格式化，否则一律显示为 0 */
const logCredit = v => Number(v || 0).toLocaleString("zh-CN", {maximumFractionDigits: 6});
const logAccountName = r => r.account_name || (r.account_id ? r.account_id : "未分配账号");

/*
 * 请求日志的实时刷新。
 *
 * 主轮询是 30 秒一次，对「盯着日志看服务在不在跑」这个场景太慢了。
 * 日志页单独用一个更快的节拍（5 秒），只在这一页可见时才跑：
 *   - 页面不可见 → 不打请求；
 *   - 切到别的页 → 定时器停掉，不留后台噪音。
 */
const LOGS_REFRESH_MS = 5000;
let logsTimer = null;

function syncLogsTimer() {
  const want = page === "logs" && csrf && !document.hidden;
  if (want && !logsTimer) {
    logsTimer = setInterval(() => { if (!document.hidden) loadLogs(true); }, LOGS_REFRESH_MS);
  } else if (!want && logsTimer) {
    clearInterval(logsTimer);
    logsTimer = null;
  }
}

async function loadLogs(silent = false) {
  try {
    requestLogs = await api("request-logs?limit=300");
    $("log-error").hidden = true;
    renderLogs();
    const stampEl = $("logs-updated");
    if (stampEl) stampEl.textContent = "最后更新 " + new Date().toLocaleTimeString("zh-CN");
  } catch (e) {
    // 只影响日志页：让用户看到原因，但不要把整页的 load-error 也点亮。
    // 静默刷新失败时不覆盖上一次的错误提示，免得每 5 秒闪一次。
    if (!silent) {
      $("log-error").textContent = "请求日志读取失败：" + e.message;
      $("log-error").hidden = false;
    }
  }
}

/* 筛选下拉只在选项集合真的变了才重建，否则每 30 秒的轮询会把用户选好的条件弹回默认值 */
function fillLogFilter(select, pairs) {
  const seen = new Map();
  for (const [value, label] of pairs) if (!seen.has(value)) seen.set(value, label);
  const html = `<option value="all">${esc(select.dataset.all)}</option>` + [...seen.entries()]
    .sort((a, b) => String(a[1]).localeCompare(String(b[1]), "zh-CN"))
    .map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  const current = select.value;
  if (select.dataset.signature !== html) { select.dataset.signature = html; select.innerHTML = html; }
  select.value = [...select.options].some(o => o.value === current) ? current : "all";
}

function renderLogs() {
  const rows = requestLogs || [];
  $("log-count").textContent = logNumber(rows.length);
  $("log-tokens").textContent = logNumber(rows.reduce((sum, r) => sum + (r.tokens || 0), 0));
  $("log-credit").textContent = logCredit(rows.reduce((sum, r) => sum + (r.credit || 0), 0));

  // 按账号汇总：轮转是否均匀、有没有某个号在猛跑，一眼能看出来
  const byAccount = new Map();
  for (const r of rows) {
    const key = r.account_id || "";
    const item = byAccount.get(key) || {name: logAccountName(r), count: 0, tokens: 0, credit: 0, failed: 0};
    item.count++; item.tokens += r.tokens || 0; item.credit += r.credit || 0;
    if (!r.ok) item.failed++;
    byAccount.set(key, item);
  }
  const summary = [...byAccount.values()].sort((a, b) => b.count - a.count);
  $("log-account-list").innerHTML = summary.length ? summary.map(a => `<div class="log-account"><span class="account-icon">${esc(a.name.slice(0, 1))}</span><div><strong>${esc(a.name)}</strong><small>${logNumber(a.count)} 次请求${a.failed ? ` · 失败 ${logNumber(a.failed)}` : ""}</small></div><div class="align-right"><b>${logCredit(a.credit)}</b><small>积分 · ${logNumber(a.tokens)} tokens</small></div></div>`).join("") : '<p class="history-empty">缓冲区内还没有请求记录，发起一次调用后再回来看。</p>';

  fillLogFilter($("log-account"), rows.map(r => [r.account_id || "", logAccountName(r)]));
  fillLogFilter($("log-model"), rows.map(r => [r.model || "", r.model || "未知模型"]));
  // 分组筛选项从当前日志里真正出现过的分组生成（而不是全量分组清单）：
  // 列一个永远筛不出结果的分组，比不列它更让人困惑。
  // 三个特殊桶只在真的出现过时才给：否则「无密钥」这种选项绝大多数时候是空的。
  {
    const pairs = [];
    const seenBuckets = new Set();
    for (const r of rows) {
      const g = logGroupOf(r);
      if (g.ids.length > 1) {
        if (!seenBuckets.has(LOG_GROUP_MULTI)) { seenBuckets.add(LOG_GROUP_MULTI); pairs.push([LOG_GROUP_MULTI, LOG_GROUP_MULTI]); }
        continue;
      }
      if (g.ids.length === 1) {
        const names = new Map((overview?.groups || []).map(x => [x.id, x.name]));
        pairs.push([g.ids[0], names.get(g.ids[0]) || g.ids[0]]);
        continue;
      }
      if (!seenBuckets.has(g.bucket)) { seenBuckets.add(g.bucket); pairs.push([g.bucket, g.bucket]); }
    }
    fillLogFilter($("log-group"), pairs);
  }
  applyLogFilter();
}

/* ---------- 请求日志：分组归属 ---------- */
/* 日志 entry 里【没有】分组字段（pool.py:523-541 只有 key_id），分组挂在密钥上：
     key_id -> keysvc 的 key.groups[] -> groups 的 id/name
   这三份数据前端已经全有：overview.keys（含 groups）、overview.groups、entry.key_id。
   所以不需要后端加字段。 */

/* 三种「没有组」的情形必须分开，否则用户看不出区别：
     key_id 有值 + groups 空  -> 「不限」：密钥有效，能用全部账号（与密钥页 groupTags 一致）
     key_id 为 null          -> 「无密钥」：这次调用根本没走密钥（直连 / 被拒请求）
     key_id 有值但查不到密钥 -> 「密钥已删除」：日志还在，密钥没了
   把后两种也显示成「不限」会让「没有分组」和「没有密钥」看起来一样，
   而它们的排查方向完全不同。 */
const LOG_GROUP_NONE = "不限";
const LOG_GROUP_NOKEY = "无密钥";
const LOG_GROUP_MISSING = "密钥已删除";
/* 「一个密钥绑多个分组」在分组展示时单列一类。
   为什么不重复计入每个组：那会让各组合计大于总数，看起来像统计 bug；
   为什么不按第一个组归：那会把这条请求从第二个组里藏起来。
   单列一类既不丢信息，也不制造对不上的合计。 */
const LOG_GROUP_MULTI = "多分组";

/** 日志行的分组归属。返回 {ids, label}：ids 用于筛选，label 用于展示。 */
function logGroupOf(r) {
  if (!r || r.key_id === null || r.key_id === undefined || r.key_id === "") {
    return {ids: [], label: LOG_GROUP_NOKEY, bucket: LOG_GROUP_NOKEY};
  }
  const key = (overview?.keys || []).find(k => k.id === r.key_id);
  if (!key) return {ids: [], label: LOG_GROUP_MISSING, bucket: LOG_GROUP_MISSING};
  const ids = (key.groups || []).filter(Boolean);
  if (!ids.length) return {ids: [], label: LOG_GROUP_NONE, bucket: LOG_GROUP_NONE};
  const names = new Map((overview?.groups || []).map(g => [g.id, g.name]));
  const label = ids.map(id => names.get(id) || id).join("、");
  /* 列里显示真实组名（这行实际走了哪些组），分节标题显示桶名。
     两个问题不一样：列回答「这行属于谁」，节标题回答「这一节是按什么聚起来的」。
     多分组的行自成一节，如果标题直接显示「高速组、低价组」，
     那只是碰巧可读 —— 换个组合顺序就变成另一个标题，用户无从知道这是一类。
     所以这时用明确的桶名，并在说明里讲清它为什么单独一类。 */
  return {
    ids,
    label,
    bucket: ids.length > 1 ? LOG_GROUP_MULTI : ids[0],
    bucketLabel: ids.length > 1 ? LOG_GROUP_MULTI : label,
    groupKey: ids.length > 1 ? null : ids[0],
  };
}

function logGroupCell(r) {
  const g = logGroupOf(r);
  if (!g.ids.length) {
    const tone = g.label === LOG_GROUP_NOKEY || g.label === LOG_GROUP_MISSING ? "cell-note" : "muted";
    return `<span class="${tone}">${esc(g.label)}</span>`;
  }
  // 与密钥页 groupTags 同一套 .tag-list 视觉，但日志有 300 行，
  // 所以只给「多分组」加 title，不在每行里塞 <code> 的完整 id。
  return `<span class="log-group-cell" title="${esc(g.ids.join("、"))}">${esc(g.label)}${g.ids.length > 1 ? `<small class="cell-note">(${g.ids.length} 组)</small>` : ""}</span>`;
}

function logRowsFiltered() {
  const account = $("log-account").value, model = $("log-model").value,
        outcome = $("log-outcome").value, group = $("log-group").value;
  return (requestLogs || []).filter(r => {
    if (account !== "all" && (r.account_id || "") !== account) return false;
    if (model !== "all" && (r.model || "") !== model) return false;
    if (outcome !== "all" && r.outcome !== outcome) return false;
    if (group !== "all") {
      const g = logGroupOf(r);
      // 具体分组按 id 命中（多分组的行，绑了就该被筛出来）；
      // 三个特殊桶（不限 / 无密钥 / 密钥已删除 / 多分组）按 bucket 命中。
      if (group === LOG_GROUP_MULTI || group === LOG_GROUP_NONE ||
          group === LOG_GROUP_NOKEY || group === LOG_GROUP_MISSING) {
        if (g.bucket !== group) return false;
      } else if (!g.ids.includes(group)) return false;
    }
    return true;
  });
}

function logRowHtml(r) {
  return `<tr><td class="mono muted">${esc(stamp(r.time * 1000))}</td><td>${esc(logAccountName(r))}${r.source === "test" ? '<span class="mini-active">测试</span>' : ""}<small class="cell-note mono">${r.account_id ? esc(r.account_id) : "—"}</small></td><td>${logGroupCell(r)}</td><td class="mono">${esc(r.model || "—")}</td><td><span class="pill ${LOG_TONES[r.outcome] || ""}">${esc(LOG_OUTCOMES[r.outcome] || r.outcome || "—")}</span><small class="cell-note">HTTP ${r.status ?? "—"}</small></td><td class="align-right mono">${logNumber(r.tokens)}</td><td class="align-right mono">${logCredit(r.credit)}</td><td class="align-right mono">${logNumber(r.duration_ms)} ms</td></tr>`;
}

/** 按分组分节。组按「最新一条」排序，组内保持时间倒序 —— 顺序错了这页就没用。 */
function renderLogGroups(rows) {
  const box = $("log-grouped-view");
  if (!rows.length) {
    box.innerHTML = '<p class="history-empty">没有符合筛选条件的记录。</p>';
    return;
  }
  const byBucket = new Map();
  for (const r of rows) {
    const g = logGroupOf(r);
    const item = byBucket.get(g.bucket) || {label: g.bucketLabel || g.label, rows: [], tokens: 0, credit: 0};
    item.rows.push(r);
    item.tokens += r.tokens || 0;
    item.credit += r.credit || 0;
    byBucket.set(g.bucket, item);
  }
  const sections = [...byBucket.values()]
    .map(s => ({...s, rows: s.rows.slice().sort((a, b) => (b.time || 0) - (a.time || 0))}))
    .sort((a, b) => (b.rows[0]?.time || 0) - (a.rows[0]?.time || 0));
  box.innerHTML = sections.map(s => `<section class="log-group-section"><header><h3>${esc(s.label)} <span class="count-badge">${logNumber(s.rows.length)}</span></h3><small class="muted">${logNumber(s.tokens)} tokens · ${logCredit(s.credit)} 积分${s.label === LOG_GROUP_MULTI ? ' · 绑了多个分组的密钥，自成一类（不重复计入各分组）' : ""}</small></header><div class="table-wrap"><table><thead><tr><th>时间</th><th>账号</th><th>分组</th><th>模型</th><th>结果</th><th class="align-right">Token</th><th class="align-right">积分</th><th class="align-right">耗时</th></tr></thead><tbody>${s.rows.map(logRowHtml).join("")}</tbody></table></div></section>`).join("");
}

let logView = "flat";
$("log-view-flat").addEventListener("click", () => setLogView("flat"));
$("log-view-grouped").addEventListener("click", () => setLogView("grouped"));
function setLogView(mode) {
  logView = mode;
  $("log-view-flat").setAttribute("aria-pressed", String(mode === "flat"));
  $("log-view-grouped").setAttribute("aria-pressed", String(mode === "grouped"));
  applyLogFilter();
}

function applyLogFilter() {
  const rows = logRowsFiltered();
  $("log-badge").textContent = rows.length;
  $("log-flat-view").hidden = logView !== "flat";
  $("log-grouped-view").hidden = logView !== "grouped";
  if (logView === "flat") {
    $("logs-body").innerHTML = rows.map(logRowHtml).join("")
      || '<tr><td colspan="8" class="history-empty">没有符合筛选条件的记录。</td></tr>';
  } else {
    renderLogGroups(rows);
  }
}

["log-account", "log-model", "log-outcome", "log-group"].forEach(id => $(id).addEventListener("change", applyLogFilter));
$("log-clear-filter").addEventListener("click", () => {
  $("log-account").value = $("log-model").value = $("log-outcome").value = $("log-group").value = "all";
  applyLogFilter();
});

/* ---------- 自动任务流水（签到 / 派猫猫旅行 / 保活） ---------- */
const TASK_KINDS = {checkin:"签到", travel:"派猫猫旅行", keepalive:"凭据保活", active:"活跃上报"};
let taskLogs = [];
async function loadTaskLogs() {
  const kind = $("task-log-kind").value;
  try {
    // 类别筛选交给服务端做：流水是落盘的（上限 500 条），前端只拿最近一页，
    // 在本地过滤会把「被别的类别挤出这一页」的记录彻底漏掉。
    taskLogs = await api("pool/checkin-logs?limit=100" + (kind ? "&kind=" + encodeURIComponent(kind) : ""));
    renderTaskLogs();
  } catch (e) {
    $("task-logs-body").innerHTML = `<tr><td colspan="5" class="history-empty">任务流水读取失败：${esc(e.message)}</td></tr>`;
  }
}
function renderTaskLogs() {
  $("task-log-badge").textContent = taskLogs.length;
  $("task-logs-body").innerHTML = taskLogs.map(r => {
    const kind = TASK_KINDS[r.kind] || r.kind || "签到";
    const credits = r.credits ? `<small class="cell-note">+${esc(String(r.credits))} 积分</small>` : "";
    return `<tr><td class="mono muted">${esc(stamp(r.time * 1000))}</td><td>${esc(r.name || r.id || "—")}</td><td>${esc(kind)}</td><td><span class="pill ${r.ok ? "green" : "red"}">${r.ok ? "成功" : "失败"}</span><small class="cell-note">${r.source === "auto" ? "自动" : "手动"}</small></td><td>${esc(r.message || "")}${credits}</td></tr>`;
  }).join("") || '<tr><td colspan="5" class="history-empty">还没有任务记录。开启自动任务，或到账号列表点「签到 / 派旅行」。</td></tr>';
}
$("task-log-kind").addEventListener("change", () => loadTaskLogs());
$("task-log-refresh").addEventListener("click", () => loadTaskLogs());
$("task-log-clear").addEventListener("click", () => confirmAction("清空任务流水？", "只清空这份记录，不影响账号、积分与旅行状态。", async () => {
  const kind = $("task-log-kind").value;
  const result = await api("pool/checkin-logs/clear" + (kind ? "?kind=" + encodeURIComponent(kind) : ""), {method: "POST"});
  await loadTaskLogs();
  return `已清空 ${result.removed} 条`;
}));

$("account-search").addEventListener("input", () => { if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { if(overview) renderAccounts(); });
async function refresh() {
  $("refresh").disabled = true;
  try {
    overview = await api("overview"); render(); $("load-error").hidden = true;
    // 日志页跟着一起刷新；loadLogs 自己吞掉错误并在页内提示，不阻断主刷新
    if (page === "logs") await loadLogs();
    // 出口线路同理。但正在操作那排下拉时不重绘——重绘会把用户展开的选项弹掉。
    if (page === "egress" && !$("egress-accounts-body").contains(document.activeElement)) await loadEgress();
    // 自动任务流水同理：只在账号页拉，别让每次 30 秒轮询都多打一次接口。
    if (page === "accounts") await loadTaskLogs();
    // 计费页有成本：三个接口 + 两张表。同样只在当前页时才拉。
    if (page === "billing") await loadBilling();
  }
  catch (e) { $("load-error").textContent = e.message; $("load-error").hidden = false; throw e; }
  finally { $("refresh").disabled = false; }
}
async function enter() { $("boot").hidden = true; $("login").hidden = true; $("workspace").hidden = false; goPage(location.hash.slice(1)); await refresh(); }
document.querySelectorAll("[data-login-mode]").forEach(b => b.addEventListener("click", () => setLoginMode(b.dataset.loginMode)));
$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); const button = e.submitter || $("login-form").querySelector('button[type="submit"]'); button.disabled = true; $("login-error").textContent = "";
  try { const data = await api("login", {method:"POST",body:loginPayload()}); csrf = data.csrf; setLoginMode("user"); await enter(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => { try { await api("logout", {method:"POST"}); showLogin(); } catch (e) { toast(e.message); } });
async function batchAction(action) {
  // 所有批量按钮一起禁掉：批量动作本来就是「一个在跑、别的等着」，
  // 只禁当前这一个的话，用户可以在签到还没跑完时再点一次批量派旅行，
  // 两个批次会同时对着同一批账号发写请求。
  const buttons = [...document.querySelectorAll("[data-batch]")];
  buttons.forEach(b => b.disabled = true);
  $("refresh").disabled = true;
  try {
    const result = await api("pool/actions/"+action,{method:"POST"});
    $("pool-result").hidden = false;
    $("pool-result").textContent = result.results.map(r => `${overview.accounts.find(a=>a.id===r.id)?.name || "账号"}：${r.message}`).join("；") || "没有启用的账号";
    await refresh();
  } catch(e) { toast(e.message); } finally { buttons.forEach(b => b.disabled = false); $("refresh").disabled = false; }
}
$("refresh").addEventListener("click", () => page === "accounts" ? batchAction("status") : refresh().catch(e=>toast(e.message)));
$("batch-checkin").addEventListener("click", () => batchAction("checkin"));
// 派旅行会对每个账号发起真实写请求，批量入口先确认一次。
$("batch-travel").addEventListener("click", () => confirmAction("批量派猫猫旅行？", "会以每个启用账号的身份向成长中心发起真实派发请求；已在旅途中或今日已派的账号自动跳过。", () => batchAction("travel")));
// 活跃上报同样是对上游的真实写请求，批量入口先确认一次。
$("batch-active").addEventListener("click", () => confirmAction("批量活跃上报？", "会以每个启用账号的身份向上游发送一条 chat_request_send 事件，每号每天一轮；国际版账号与今日已上报的账号自动跳过。", () => batchAction("active")));
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value,auto_travel:$("auto-travel").checked,travel_time:$("travel-time").value,auto_keepalive:$("auto-keepalive").checked,keepalive_hours:Number($("keepalive-hours").value),auto_active:$("auto-active").checked,active_time:$("active-time").value,active_reports:Number($("active-reports").value),wb2api_base:$("wb2api-base").value.trim(),wb2api_key:$("wb2api-key").value.trim()}});await refresh();toast("账号池设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
setInterval(() => { if(csrf && overview && !document.hidden && !document.querySelector("dialog[open]") && !$("refresh").disabled) refresh().catch(()=>{}); },30000);
document.querySelectorAll("[data-page]").forEach(b => b.addEventListener("click", () => goPage(b.dataset.page)));
$("go-guide").addEventListener("click", () => goPage("guide"));
document.querySelectorAll(".close-dialog").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
function clearOAuth() {
  oauthGeneration++; clearTimeout(oauthTimer);
  const previous = oauthFlow; oauthFlow = null;
  if (previous && csrf) api("oauth/" + previous.id, {method:"DELETE"}).catch(() => {});
  $("oauth-link-box").hidden = true; $("oauth-open").removeAttribute("href");
  $("oauth-start").disabled = false; $("oauth-start").textContent = "生成登录链接 ↗";
  $("oauth-retry").hidden = true; $("account-name").disabled = false;
  $("oauth-status").textContent = "登录链接 5 分钟内有效。同一账号重新登录会更新凭据。";
  $("oauth-status").className = "banner oauth-status";
}
function setAccountMode(mode) {
  clearOAuth(); accountMode = mode;
  $("browser-login-panel").hidden = mode !== "browser";
  $("file-import-panel").hidden = $("account-import").hidden = mode !== "file";
  $("mode-browser").setAttribute("aria-pressed", String(mode === "browser"));
  $("mode-file").setAttribute("aria-pressed", String(mode === "file"));
  $("account-error").textContent = "";
}
$("mode-browser").addEventListener("click", () => setAccountMode("browser"));
$("mode-file").addEventListener("click", () => setAccountMode("file"));
$("account-dialog").addEventListener("close", () => { clearOAuth(); $("account-form").reset(); $("file-label").textContent = "选择或拖入 .info / .json 文件"; $("account-error").textContent = ""; });
function openAccount() { setAccountMode("browser"); $("account-dialog").showModal(); }
async function pollOAuth(generation) {
  if (generation !== oauthGeneration || !oauthFlow) return;
  const flow = oauthFlow;
  if (Date.now() >= flow.expires_at) { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
  try {
    const result = await api("oauth/" + flow.id + "/poll", {method:"POST"});
    if (generation !== oauthGeneration) return;
    if (result.status === "success") {
      $("account-dialog").close(); await refresh();
      toast(result.updated ? "授权成功，账号凭据已更新" : "授权成功，账号已添加；可在列表中切换使用"); return;
    }
    if (result.status === "expired") { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
    $("oauth-status").className = "banner oauth-status";
    $("oauth-status").textContent = `等待你在官方页面完成登录… 链接剩余 ${Math.max(1, Math.ceil((flow.expires_at - Date.now()) / 1000))} 秒。`;
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) {
    if (generation !== oauthGeneration) return;
    $("oauth-status").className = "banner warning oauth-status"; $("oauth-status").textContent = e.message;
    $("oauth-retry").hidden = false;
  }
}
$("oauth-start").addEventListener("click", async () => {
  clearOAuth(); const generation = oauthGeneration;
  $("oauth-start").disabled = true; $("oauth-start").textContent = "正在生成…";
  $("account-error").textContent = "";
  try {
    const flow = await api("oauth/start", {method:"POST",body:{name:$("account-name").value.trim() || undefined}});
    if (generation !== oauthGeneration) { api("oauth/" + flow.id, {method:"DELETE"}).catch(() => {}); return; }
    oauthFlow = flow; $("oauth-open").href = flow.url; $("oauth-link-box").hidden = false;
    $("oauth-start").textContent = "重新生成链接"; $("account-name").disabled = true;
    $("oauth-status").textContent = "登录链接已就绪，请打开官方页面完成登录。";
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) { if (generation === oauthGeneration) $("account-error").textContent = e.message; }
  finally { if (generation === oauthGeneration) $("oauth-start").disabled = false; }
});
$("oauth-copy").addEventListener("click", () => { if (oauthFlow) copy(oauthFlow.url); });
$("oauth-retry").addEventListener("click", () => { $("oauth-retry").hidden = true; pollOAuth(oauthGeneration); });
$("add-account").addEventListener("click", openAccount); $("empty-add").addEventListener("click", openAccount);
async function readFile(file) {
  if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("文件超过 1 MB，请选择登录凭据文件");
  $("credential-json").value = (await file.text()).replace(/^\uFEFF/, "");
  $("file-label").textContent = file.name;
}
$("credential-file").addEventListener("change", e => { readFile(e.target.files[0]).catch(err => $("account-error").textContent = err.message); });
$("file-drop").addEventListener("dragover", e => { e.preventDefault(); $("file-drop").classList.add("drag-over"); });
$("file-drop").addEventListener("dragleave", () => $("file-drop").classList.remove("drag-over"));
$("file-drop").addEventListener("drop", e => { e.preventDefault(); $("file-drop").classList.remove("drag-over"); readFile(e.dataTransfer.files[0]).catch(err => $("account-error").textContent = err.message); });
$("account-form").addEventListener("submit", async e => {
  if (accountMode !== "file") { e.preventDefault(); return; }
  e.preventDefault(); e.submitter.disabled = true; $("account-error").textContent = "";
  try {
    let credential; try { credential = JSON.parse($("credential-json").value); } catch { throw new Error("请选择文件或粘贴有效的 JSON 内容"); }
    await api("accounts", {method:"POST", body:{name:$("account-name").value.trim() || undefined, credential}});
    $("account-dialog").close(); await refresh(); toast("账号已导入");
  } catch (err) { $("account-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
function confirmAction(title, desc, callback, rename = null) {
  $("confirm-title").textContent = title; $("confirm-desc").textContent = desc; $("confirm-error").textContent = "";
  $("rename-label").hidden = $("rename-value").hidden = rename === null; $("rename-value").value = rename || "";
  pendingConfirm = callback; $("confirm-dialog").showModal();
}
$("confirm-form").addEventListener("submit", async e => {
  e.preventDefault(); e.submitter.disabled = true;
  try {
    // 回调可以返回一句话当作提示（例如「同时解绑了 3 枚密钥」）；返回别的，
    // 通常是 api() 的响应对象，就退回默认文案。
    const message = await pendingConfirm();
    $("confirm-dialog").close(); await refresh();
    toast(typeof message === "string" && message ? message : "操作已保存");
  } catch (err) { $("confirm-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
$("accounts-body").addEventListener("click", async e => {
  const b = e.target.closest("[data-action]"); if (!b) return;
  const a = overview.accounts.find(x => x.id === b.dataset.id); if (!a) return;
  if (["status","checkin","refresh","travel","travel-claim","active"].includes(b.dataset.action)) {
    b.disabled = true;
    try { const result=await api(`accounts/${a.id}/actions/${b.dataset.action}`,{method:"POST"});toast(result.message);await refresh(); }
    catch(err){toast(err.message);} finally {b.disabled=false;} return;
  }
  const update = body => api("accounts/" + a.id, {method:"PATCH", body});
  if (b.dataset.action === "activate") confirmAction("设置手动 / 测试账号？", `设为「${a.name}」。仅在手动调度模式和连接测试中使用；账号池轮转模式仍自动分配。`, () => update({active:true}));
  if (b.dataset.action === "rename") confirmAction("编辑账号备注", "备注仅用于在管理后台识别账号。", () => update({name:$("rename-value").value}), a.name);
  if (b.dataset.action === "toggle") confirmAction(a.enabled ? "暂停这个账号？" : "恢复这个账号？", "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。", () => update({enabled:!a.enabled}));
  if (b.dataset.action === "delete") confirmAction("删除这个账号？", `「${a.name}」将从账号列表移除。服务器保留恢复副本。${a.active ? "当前调用账号将被清空。" : ""}`, () => api("accounts/" + a.id, {method:"DELETE"}));
});
$("keys-body").addEventListener("click", e => {
  // 小眼睛：默认只显示掩码，点开才把明文写回单元格（不写进 HTML 属性以免被全选复制带走）。
  const eye = e.target.closest("[data-reveal]");
  if (eye) {
    const code = eye.parentElement.querySelector("code");
    const k = overview.keys.find(x => x.id === eye.dataset.reveal);
    if (!k) return;
    // 早于「明文落盘」这次改动的密钥没有 secret，只能提示，不能假装能看
    if (!k.secret) { toast("这个密钥创建于旧版本，明文未保存，无法显示；请重新创建一个。"); return; }
    const hidden = code.dataset.hidden === "1";
    code.textContent = hidden ? k.secret : "•".repeat(12);
    code.dataset.hidden = hidden ? "0" : "1";
    eye.innerHTML = hidden ? ICON_EYE_OFF : ICON_EYE;
    return;
  }
  const b = e.target.closest("[data-revoke]"); if (!b) return; const k = overview.keys.find(x => x.id === b.dataset.revoke); confirmAction("撤销客户端密钥？", `使用「${k.name}」的客户端将无法发起新请求。此操作不可撤销。`, () => api("keys/" + k.id, {method:"DELETE"}));
});
/* 创建密钥弹窗里的分组勾选。每次打开重填一次，避免勾选状态跨次残留。 */
function fillKeyGroups() {
  const groups = overview?.groups || [];
  $("key-groups").innerHTML = groups.length
    ? groups.map(g => `<label><input type="checkbox" value="${esc(g.id)}"><span>${esc(g.name)}</span><small class="cell-note">${g.account_count} 个账号</small></label>`).join("")
    : '<p class="check-list-empty">还没有分组。先到「上游分组」建一个，就能把密钥限制在组内账号上。</p>';
}
$("add-key").addEventListener("click", () => { $("key-form").hidden = false; $("created-key").hidden = true; fillKeyGroups(); $("key-dialog").showModal(); });
$("key-dialog").addEventListener("close", () => { $("key-form").reset(); $("new-key-value").textContent = ""; $("key-error").textContent = ""; $("key-groups").innerHTML = ""; });
$("key-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { const d = await api("keys", {method:"POST",body:{name:$("key-name").value, groups:[...$("key-groups").querySelectorAll("input[type=checkbox]:checked")].map(x => x.value)}}); $("key-form").hidden = true; $("created-key").hidden = false; $("new-key-value").textContent = d.key; await refresh(); } catch (err) { $("key-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { toast("无法自动复制，请手动选择文本复制"); } }
$("copy-key").addEventListener("click", () => copy($("new-key-value").textContent));
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => copy(b.dataset.copy)));
$("base-url").textContent = location.origin + "/v1"; $("anthropic-url").textContent = location.origin;
$("copy-base").addEventListener("click", () => copy(location.origin + "/v1")); $("copy-anthropic").addEventListener("click", () => copy(location.origin));
$("host-label").textContent = location.host;
$("test-form").addEventListener("submit", async e => {
  e.preventDefault(); busy = true; $("test-submit").disabled = true; $("test-submit").textContent = "正在调用…"; $("test-status").className = "pill amber"; $("test-status").textContent = "请求中"; $("test-output").textContent = "正在等待上游响应，最长约 90 秒…"; $("test-meta").textContent = "";
  try { const r = await api("test", {method:"POST",body:{model:$("model").value,prompt:$("test-prompt").value}}); $("test-status").className = "pill " + (r.ok ? "green" : "red"); $("test-status").textContent = r.ok ? "连接成功" : "调用失败"; $("test-output").textContent = r.ok ? r.answer : r.error; $("test-meta").textContent = `${r.seconds}s${r.status ? " · HTTP " + r.status : ""}${r.usage?.total_tokens !== undefined ? " · " + r.usage.total_tokens + " tokens" : ""}`; }
  catch (err) { $("test-status").className = "pill red"; $("test-status").textContent = "请求失败"; $("test-output").textContent = err.message; }
  finally { busy = false; $("test-submit").textContent = "发送测试 ↗"; await refresh().catch(() => {}); }
});
/* 登录入口不是每种部署都成立，由服务端说了算：
   - 没显式配 ADMIN_KEY 的部署（桌面端托管就是这种）密钥每次启动都重新随机，
     用户拿不到，摆出来只会让人反复被告知「管理密钥不正确」；
   - 一枚管理令牌都没建过时，「管理令牌」入口同样没有意义。
   只剩一种方式时连分段控件一起收起来——一格孤零零的切换按钮比没有更迷惑。 */
async function applyLoginOptions() {
  let options;
  try { options = await api("login-options"); } catch { return; }
  const usable = {user: true, key: options.legacy_key !== false, token: options.tokens !== false};
  let visible = 0;
  document.querySelectorAll("[data-login-mode]").forEach(b => {
    const on = usable[b.dataset.loginMode] !== false;
    b.hidden = !on;
    if (on) visible += 1;
  });
  const bar = document.querySelector(".login-modes");
  if (bar) {
    bar.hidden = visible < 2;
    bar.style.gridTemplateColumns = `repeat(${Math.max(1, visible)}, minmax(0, 1fr))`;
  }
}

/* ── 充值与兑换码 ─────────────────────────────────────────────────
   三条纪律：
   1. **生成后的明文只显示一次**。库里只有哈希，所以「刚才那批码」
      是这个页面唯一一次能拿到明文的机会，必须显眼、可一键复制、可下载。
   2. **金额按积分显示但按厘计算**（后端），前端只做展示格式化 ——
      不要在这里再算一遍钱，两个地方各算一次迟早会对不上。
   3. **已发放 vs 未核销分开显示**。混在一起账面会多出好几倍。
*/
function fmtCredit(n) {
  return Number(n || 0).toLocaleString("zh-CN", {maximumFractionDigits: 3});
}

async function loadBilling() {
  // 用户列表来自独立的 users 接口，不在 overview 里。
  // 之前这里读 overview.users 拿到的是 undefined，下拉永远是空的 ——
  // 而因为下拉空着不影响其它渲染，页面看着「差不多对」，只有充值时才发现选不到人。
  const [stats, codes, flows, users] = await Promise.all([
    api("billing/stats"),
    api("billing/codes?limit=200&status=" + encodeURIComponent($("codes-status").value || "all")),
    api("billing/flows?limit=200"),
    api("users"),
  ]);
  const s = stats.stats || {};
  $("bill-codes").textContent = s.codes_total ?? "—";
  $("bill-used").textContent = s.codes_used ?? "—";
  $("bill-issued").textContent = fmtCredit(s.credit_issued);
  $("bill-outstanding").textContent = fmtCredit(s.credit_outstanding);
  $("bill-balance").textContent = fmtCredit(s.balance_total);
  renderCodes(codes.codes || []);
  renderFlows(flows.flows || []);
  // 用户下拉：只在选项变了才重建，否则每次轮询都会把用户选中的项弹回去。
  const select = $("recharge-user");
  const names = (Array.isArray(users) ? users : []).map(u => u.username);
  const current = select.value;
  const existing = [...select.options].map(o => o.value).filter(Boolean);
  if (names.join("\u0000") !== existing.join("\u0000")) {
    select.replaceChildren();
    const blank = document.createElement("option");
    blank.value = ""; blank.textContent = "选择用户…";
    select.appendChild(blank);
    for (const name of names) {
      const option = document.createElement("option");
      option.value = name; option.textContent = name;
      select.appendChild(option);
    }
    if (names.includes(current)) select.value = current;
  }
}

function renderCodes(rows) {
  const body = $("codes-body");
  body.replaceChildren();
  $("codes-badge").textContent = rows.length;
  $("codes-empty").hidden = rows.length > 0;
  for (const row of rows) {
    const tr = document.createElement("tr");
    const tdId = document.createElement("td");
    tdId.className = "mono muted";
    tdId.textContent = row.id;
    tdId.title = "只存了哈希，明文不可再取回";
    tr.appendChild(tdId);
    const tdBatch = document.createElement("td");
    tdBatch.className = "mono muted";
    tdBatch.textContent = row.batch;
    tr.appendChild(tdBatch);
    const tdAmount = document.createElement("td");
    tdAmount.className = "align-right";
    tdAmount.textContent = fmtCredit(row.credit);
    tr.appendChild(tdAmount);
    const tdState = document.createElement("td");
    const used = Boolean(row.redeemed_at);
    // 用页面统一的 .pill 口径，别自造一套 badge()。
    const pill = document.createElement("span");
    if (used) {
      pill.className = "pill";
      pill.textContent = "已核销 " + stamp(row.redeemed_at * 1000).slice(0, 10);
    } else if (row.enabled === false) {
      pill.className = "pill red";
      pill.textContent = "已停用";
    } else {
      pill.className = "pill green";
      pill.textContent = "未核销";
    }
    tdState.appendChild(pill);
    tr.appendChild(tdState);
    const tdBy = document.createElement("td");
    tdBy.textContent = row.redeemed_by || "—";
    tr.appendChild(tdBy);
    const tdNote = document.createElement("td");
    tdNote.className = "muted";
    tdNote.textContent = row.note || "—";
    tr.appendChild(tdNote);
    const tdAct = document.createElement("td");
    tdAct.className = "align-right";
    if (!used && row.enabled !== false) {
      const button = document.createElement("button");
      button.className = "text-button";
      button.textContent = "停用";
      button.addEventListener("click", async () => {
        try {
          await api("billing/codes/" + encodeURIComponent(row.id) + "/disable", {method: "POST"});
          toast("已停用该兑换码");
          await loadBilling();
        } catch (error) { toast(error.message); }
      });
      tdAct.appendChild(button);
    } else {
      tdAct.textContent = "—";
    }
    tr.appendChild(tdAct);
    body.appendChild(tr);
  }
}

function renderFlows(rows) {
  const body = $("flows-body");
  body.replaceChildren();
  $("flows-badge").textContent = rows.length;
  $("flows-none").hidden = rows.length > 0;
  const LABEL = {recharge: "充值", redeem: "兑换", spend: "消费"};
  for (const row of rows) {
    const tr = document.createElement("tr");
    const tdTime = document.createElement("td");
    tdTime.className = "mono muted";
    tdTime.textContent = stamp(row.ts * 1000);
    tr.appendChild(tdTime);
    const tdUser = document.createElement("td");
    tdUser.textContent = row.username || "—";
    tr.appendChild(tdUser);
    const tdKind = document.createElement("td");
    tdKind.textContent = LABEL[row.kind] || row.kind || "—";
    tr.appendChild(tdKind);
    const amount = Number(row.credit || 0);
    const tdAmount = document.createElement("td");
    tdAmount.className = "align-right " + (amount >= 0 ? "amount-pos" : "amount-neg");
    tdAmount.textContent = (amount > 0 ? "+" : "") + fmtCredit(amount);
    tr.appendChild(tdAmount);
    const tdBalance = document.createElement("td");
    tdBalance.className = "align-right muted";
    tdBalance.textContent = fmtCredit(row.balance);
    tr.appendChild(tdBalance);
    const tdNote = document.createElement("td");
    tdNote.className = "muted";
    tdNote.textContent = row.note || "—";
    tr.appendChild(tdNote);
    body.appendChild(tr);
  }
}

$("recharge-form").addEventListener("submit", async event => {
  event.preventDefault();
  const username = $("recharge-user").value;
  const credit = Number($("recharge-credit").value);
  if (!username) { toast("请先选择用户"); return; }
  if (!Number.isFinite(credit) || credit <= 0) { toast("充值金额必须大于 0"); return; }
  const button = event.submitter || $("recharge-form").querySelector("button");
  button.disabled = true;
  try {
    const result = await api("billing/recharge", {method: "POST", body: {
      username, credit, note: $("recharge-note").value.trim(),
    }});
    $("recharge-result").hidden = false;
    $("recharge-result").textContent = `已为 ${username} 充值 ${fmtCredit(credit)} 积分，当前余额 ${fmtCredit(result.credit)}。`;
    $("recharge-credit").value = ""; $("recharge-note").value = "";
    await loadBilling();
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});

$("codes-form").addEventListener("submit", async event => {
  event.preventDefault();
  const count = Number($("codes-count").value);
  const credit = Number($("codes-credit").value);
  if (!Number.isFinite(credit) || credit <= 0) { toast("每张面额必须大于 0"); return; }
  const button = event.submitter || $("codes-form").querySelector("button");
  button.disabled = true;
  try {
    const result = await api("billing/codes", {method: "POST", body: {
      count, credit, note: $("codes-note").value.trim(),
    }});
    showGeneratedCodes(result);
    await loadBilling();
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});

/** 明文只此一次：给复制按钮，也给下载 —— 手工抄 100 张码是不现实的。 */
function showGeneratedCodes(result) {
  const box = $("codes-output");
  box.hidden = false;
  box.replaceChildren();
  const head = document.createElement("div");
  head.className = "codes-head";
  const title = document.createElement("strong");
  title.textContent = `已生成 ${result.count} 张 × ${fmtCredit(result.credit)} 积分（批次 ${result.batch}）`;
  const hint = document.createElement("span");
  hint.className = "muted";
  hint.textContent = " 明文只显示这一次，离开本页就再也取不回来。";
  head.append(title, hint);
  box.appendChild(head);

  const list = document.createElement("textarea");
  list.className = "codes-list";
  list.readOnly = true;
  list.rows = Math.min(12, result.codes.length + 1);
  list.value = result.codes.join("\n");
  box.appendChild(list);

  const actions = document.createElement("div");
  actions.className = "codes-actions";
  const copy = document.createElement("button");
  copy.className = "text-button";
  copy.textContent = "复制全部";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(result.codes.join("\n"));
      toast("已复制 " + result.codes.length + " 张兑换码");
    } catch { list.select(); toast("浏览器拒绝了剪贴板，已为你全选，请按 Ctrl+C"); }
  });
  const download = document.createElement("button");
  download.className = "text-button";
  download.textContent = "下载 .txt";
  download.addEventListener("click", () => {
    // 前缀 \ufeff：Excel / 记事本打开中文备注时不会乱码。
    const blob = new Blob(["\ufeff" + result.codes.join("\r\n")], {type: "text/plain;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `兑换码-${result.batch}-${result.count}张-${fmtCredit(result.credit)}积分.txt`;
    a.click();
    URL.revokeObjectURL(url);
  });
  actions.append(copy, download);
  box.appendChild(actions);
}

$("codes-status").addEventListener("change", () => { loadBilling().catch(e => toast(e.message)); });



(async () => { await applyLoginOptions(); try { const s = await api("session"); csrf = s.csrf; await enter(); } catch (e) { if (!csrf) showLogin(); } })();

/* ══════════════════════════════════════════════════════════════════
   用户管理（终端用户 / members）
   ──────────────────────────────────────────────────────────────────
   ⚠️ 与「后台账号」（/admin/api/users，控制台登录账号）是**两套东西**：
      · 后台账号  -> state.json 的 `users`（list）
      · 终端用户  -> state.json 的 `members`（dict），members.* 模块
   两者永不共用端点、永不共用存储键。界面上也必须分开叫，
   否则管理员分不清「我到底给谁加了钱」。

   守卫分三档（契约 §5.2）。UI 要**预先**表达能力差异，不能只靠 403 报错：
      · 列表 / 统计 / 详情        -> viewer
      · 改角色 / 启停 / 分发账号   -> admin
      · 调整余额 / 删除           -> session_admin（管理令牌登录时不可用）
   ══════════════════════════════════════════════════════════════════ */

let membersState = {items: [], total: 0, page: 1, size: 20, pages: 1, q: ""};
let sessionInfo = null;   // 来自 GET /admin/api/session，用来判断能力边界

/** 当前会话能不能改钱（余额 / 删除）。
 *  判据是 `via` —— 只在 GET /admin/api/session 返回（登录响应里没有）：
 *    账号密码登录 -> "session"  可用
 *    管理令牌换会话 -> "token"    服务端 403
 *  拿不到会话信息时保守放行：宁可由服务端拒绝并给出准确文案，
 *  也不要因为前端猜错而把本来能用的功能藏起来。 */
function canManageFunds() {
  if (!sessionInfo) return true;
  return sessionInfo.via !== "token";
}

/** 能力差异的统一文案。两端（网页端 / 桌面端）必须一致：
 *  重点是「换个登录方式就能用」，而不是「这个功能没有」。 */
const TOKEN_LIMIT_NOTE = "用管理令牌登录时，调整余额与删除用户不可用 —— 请改用账号密码登录。";

const ROLE_LABEL = {user: "普通用户", admin: "管理员"};
const MEMBER_STATUS = {active: ["正常", "green"], disabled: ["已禁用", "red"]};

async function loadSession() {
  if (sessionInfo !== null) return sessionInfo;
  try { sessionInfo = await api("session"); }
  catch { sessionInfo = {}; }   // 拿不到就不做前端阉割，交给服务端判定
  return sessionInfo;
}

async function loadMembers(page) {
  if (typeof page === "number") membersState.page = page;
  const warn = $("mem-warn");
  warn.hidden = true;
  try {
    await loadSession();
    const query = "members?page=" + membersState.page + "&size=" + membersState.size +
                  (membersState.q ? "&q=" + encodeURIComponent(membersState.q) : "");
    const [list, stats] = await Promise.all([api(query), api("members/stats")]);
    membersState.items = list.items || [];
    membersState.total = list.total || 0;
    membersState.pages = list.pages || 1;
    renderMembers(list);
    renderMemberStats(stats);
    if (list.truncated) {
      // 扫描被截断时说清楚：管理员不该以为「列表里没有 = 系统里没有」。
      warn.textContent = "用户数超过 " + (list.scanned || 0) + " 条，本次只扫描了前 " +
                         (list.scanned || 0) + " 条，结果可能不完整。";
      warn.hidden = false;
    }
  } catch (error) {
    warn.textContent = "读取用户列表失败：" + error.message;
    warn.hidden = false;
  }
}

function renderMemberStats(s) {
  $("mem-total").textContent = s.total ?? 0;
  $("mem-active").textContent = s.active ?? 0;
  $("mem-disabled").textContent = s.disabled ?? 0;
  $("mem-admins").textContent = s.admins ?? 0;
  $("mem-balance").textContent = fmtCredit(s.balance_credit);
  const box = $("mem-orphans");
  const orphans = s.orphan_keys || 0;
  if (orphans > 0) {
    // 孤儿密钥 = 「钱记在没人认领的桶里」。必须显式提示，否则账目对不上也没人知道。
    box.textContent = "有 " + orphans + " 枚密钥的归属指向了不存在的用户（孤儿密钥）。" +
                      "它们仍能调用接口，但余额记在一个没人认领的桶里，也无法兑换 —— " +
                      "请到「API 密钥」页把这些密钥的归属改到一个真实用户上。";
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}

function renderMembers(list) {
  const body = $("mem-body");
  body.replaceChildren();
  $("mem-badge").textContent = membersState.total;
  $("mem-empty").hidden = membersState.items.length > 0;
  $("mem-page").textContent = "第 " + membersState.page + " / " + membersState.pages + " 页";
  $("mem-prev").disabled = membersState.page <= 1;
  $("mem-next").disabled = membersState.page >= membersState.pages;

  const funds = canManageFunds();

  for (const m of membersState.items) {
    const tr = document.createElement("tr");

    const tdEmail = document.createElement("td");
    const strong = document.createElement("strong");
    strong.textContent = m.email || "—";
    tdEmail.appendChild(strong);
    if (!m.email_verified) {
      const note = document.createElement("small");
      note.className = "cell-note";
      note.textContent = "邮箱未验证";
      tdEmail.appendChild(note);
    }
    const idNote = document.createElement("small");
    idNote.className = "cell-note mono";
    idNote.textContent = m.id || "";
    tdEmail.appendChild(idNote);
    tr.appendChild(tdEmail);

    const tdRole = document.createElement("td");
    tdRole.textContent = ROLE_LABEL[m.role] || m.role || "—";
    tr.appendChild(tdRole);

    const tdStatus = document.createElement("td");
    const [statusText, tone] = MEMBER_STATUS[m.status] || [m.status || "—", ""];
    const pill = document.createElement("span");
    pill.className = "pill " + tone;
    pill.textContent = statusText;
    tdStatus.appendChild(pill);
    tr.appendChild(tdStatus);

    const tdBal = document.createElement("td");
    tdBal.className = "align-right mono credit-number";
    tdBal.textContent = fmtCredit(m.balance_credit);
    tr.appendChild(tdBal);

    const tdKeys = document.createElement("td");
    tdKeys.textContent = m.key_count ? m.key_count + " 枚" : "—";
    if (m.key_names && m.key_names.length) tdKeys.title = m.key_names.join("、");
    tr.appendChild(tdKeys);

    const tdLogin = document.createElement("td");
    tdLogin.className = "muted mono";
    tdLogin.textContent = m.last_login_at ? stamp(m.last_login_at * 1000) : "还没登录过";
    tr.appendChild(tdLogin);

    const tdActions = document.createElement("td");
    tdActions.className = "align-right";
    const actions = document.createElement("div");
    actions.className = "actions";

    const detail = document.createElement("button");
    detail.textContent = "详情";
    detail.addEventListener("click", () => openMemberDetail(m.id));
    actions.appendChild(detail);

    const role = document.createElement("button");
    role.textContent = m.role === "admin" ? "设为普通" : "设为管理员";
    role.addEventListener("click", () => patchMember(m,
      {role: m.role === "admin" ? "user" : "admin"},
      "把「" + m.email + "」的角色改为" + (m.role === "admin" ? "普通用户" : "管理员") + "？"));
    actions.appendChild(role);

    const toggle = document.createElement("button");
    toggle.textContent = m.status === "disabled" ? "启用" : "禁用";
    toggle.addEventListener("click", () => patchMember(m,
      {status: m.status === "disabled" ? "active" : "disabled"},
      (m.status === "disabled" ? "启用" : "禁用") + "「" + m.email + "」？" +
      (m.status === "disabled" ? "" : "禁用后该账号无法登录，但名下密钥仍可调用接口。")));
    actions.appendChild(toggle);

    const balance = document.createElement("button");
    balance.textContent = "余额";
    balance.disabled = !funds;
    if (!funds) balance.title = TOKEN_LIMIT_NOTE;
    balance.addEventListener("click", () => openBalance(m));
    actions.appendChild(balance);

    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "删除";
    del.disabled = !funds;
    if (!funds) del.title = TOKEN_LIMIT_NOTE;
    del.addEventListener("click", () => deleteMember(m));
    actions.appendChild(del);

    tdActions.appendChild(actions);
    tr.appendChild(tdActions);
    body.appendChild(tr);
  }
}

/** 改角色 / 启停。不在前端复刻业务规则（至少留一个管理员、不能改自己…）——
 *  那些规则属于服务端，前端抄一份就是多一份会漂移的副本。
 *  但要把服务端的原话透出来，管理员才知道为什么不行。 */
function patchMember(member, patch, prompt) {
  confirmAction("确认操作", prompt, async () => {
    const result = await api("members/" + encodeURIComponent(member.id), {method: "PATCH", body: patch});
    let message = "已更新「" + member.email + "」。";
    if (result.relogin_required) message += "该用户的登录已失效，需要重新登录。";
    if (result.sessions_revoked) message += "（已吊销 " + result.sessions_revoked + " 个会话）";
    await loadMembers();
    return message;
  });
}

async function openMemberDetail(uid) {
  const dialog = $("mem-detail-dialog");
  const body = $("mem-detail-body");
  body.replaceChildren();
  $("mem-detail-title").textContent = "用户详情";
  $("mem-detail-sub").textContent = "读取中…";
  dialog.showModal();
  try {
    const d = await api("members/" + encodeURIComponent(uid) + "?flows=50");
    $("mem-detail-title").textContent = d.email || "(无邮箱)";
    $("mem-detail-sub").textContent = "用户 ID " + (d.id || "—") +
      " · 余额 " + fmtCredit(d.balance_credit) + " 积分";

    body.appendChild(detailSection("基本信息", [
      ["邮箱", d.email || "—"],
      ["邮箱验证", d.email_verified ? "已验证" : "未验证"],
      ["角色", ROLE_LABEL[d.role] || d.role || "—"],
      ["状态", (MEMBER_STATUS[d.status] || [d.status])[0] || "—"],
      ["注册时间", d.created_at ? stamp(d.created_at * 1000) : "—"],
      ["最近登录", d.last_login_at ? stamp(d.last_login_at * 1000) : "还没登录过"],
      ["余额", fmtCredit(d.balance_credit) + " 积分（" + (d.balance_milli ?? 0) + " 厘）"],
    ]));

    const keys = d.keys || [];
    body.appendChild(detailSection("名下密钥（" + keys.length + "）",
      keys.length ? keys.map(k => [k.name || "(未命名)",
        (k.prefix || "—") + " · " + (k.enabled ? "启用中" : "已停用") +
        " · 创建于 " + (k.created ? stamp(k.created * 1000) : "—") +
        (k.last_used_at ? " · 最近使用 " + stamp(k.last_used_at * 1000) : " · 未使用过")])
        : [["—", "该用户名下还没有 API 密钥"]]));

    const flows = d.flows || [];
    body.appendChild(detailSection("最近流水（" + flows.length + "）",
      flows.length ? flows.slice(0, 50).map(f => {
        const amount = Number(f.credit || 0);
        // 金额直接用服务端的 credit，不自己做 milli/1000 —— 浮点舍入口径
        // 一旦与服务端不同，同一条流水在两端就会显示成两个数。
        return [stamp((f.ts || 0) * 1000),
          (amount > 0 ? "+" : "") + fmtCredit(amount) + " 积分 · 余额 " + fmtCredit(f.balance) +
          " · " + (f.note || "无说明")];
      }) : [["—", "还没有流水"]]));
  } catch (error) {
    $("mem-detail-sub").textContent = "";
    const p = document.createElement("p");
    p.className = "banner warning";
    p.textContent = "读取详情失败：" + error.message;
    body.appendChild(p);
  }
}

function detailSection(title, rows) {
  const wrap = document.createElement("section");
  wrap.className = "detail-section";
  const h = document.createElement("h3");
  h.textContent = title;
  wrap.appendChild(h);
  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }
  wrap.appendChild(dl);
  return wrap;
}

function openBalance(member) {
  if (!canManageFunds()) { toast(TOKEN_LIMIT_NOTE); return; }
  const dialog = $("mem-balance-dialog");
  $("mem-balance-who").textContent = "用户：" + member.email +
    "（当前余额 " + fmtCredit(member.balance_credit) + " 积分）";
  $("mem-balance-delta").value = "";
  $("mem-balance-reason").value = "";
  $("mem-balance-error").hidden = true;
  dialog.dataset.uid = member.id;
  dialog.showModal();
  $("mem-balance-delta").focus();
}

async function submitBalance() {
  const dialog = $("mem-balance-dialog");
  const uid = dialog.dataset.uid;
  const button = $("mem-balance-submit");
  const errorBox = $("mem-balance-error");
  errorBox.hidden = true;
  const delta = $("mem-balance-delta").value.trim();
  if (!delta) { errorBox.textContent = "请填写要调整的积分数量。"; errorBox.hidden = false; return; }
  button.disabled = true;
  button.textContent = "提交中…";
  try {
    const result = await api("members/" + encodeURIComponent(uid) + "/balance",
      {method: "POST", body: {delta_credit: Number(delta), reason: $("mem-balance-reason").value.trim()}});
    toast("已调整，当前余额 " + fmtCredit(result.balance_credit) + " 积分。");
    dialog.close();
    await loadMembers();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "确认调整";
  }
}

async function deleteMember(member) {
  if (!canManageFunds()) { toast(TOKEN_LIMIT_NOTE); return; }
  const hasBalance = Number(member.balance_credit || 0) !== 0;
  const keyNote = member.key_count
    ? "该用户名下有 " + member.key_count + " 枚密钥，删除后它们会变成无主密钥（仍能调用，但余额归零且无法兑换）。"
    : "该用户名下没有密钥。";
  const balanceNote = hasBalance
    ? "⚠️ 该用户还有 " + fmtCredit(member.balance_credit) + " 积分余额，删除会吞掉这笔钱；" +
      "系统会记一笔结清流水，账面仍然平。"
    : "";
  confirmAction("删除用户",
    "确定删除「" + member.email + "」？" + keyNote + balanceNote + "这是不可撤销的操作。",
    async () => {
      const result = await api("members/" + encodeURIComponent(member.id) + (hasBalance ? "?force=1" : ""),
                              {method: "DELETE"});
      let message = "已删除「" + (result.email || member.email) + "」。";
      if (result.keys_unbound) message += " 解绑了 " + result.keys_unbound + " 枚密钥。";
      if (Number(result.released_credit || 0)) message += " 结清了 " + fmtCredit(result.released_credit) + " 积分。";
      await loadMembers();
      return message;
    });
}

/* ── 分发账号 ─────────────────────────────────────────────────────
   为什么这个入口是必须的：未配置 SMTP 且开着验证码时**任何人都注册不了**，
   而登录页那句提示的原文就写着「或者给您分发账号登录」——
   管理员照着一个自己解决不了的建议去做、却无处可去，这个入口就是补这个洞。 */
$("mem-add").addEventListener("click", () => {
  $("mem-new-email").value = "";
  $("mem-new-password").value = "";
  $("mem-new-role").value = "user";
  $("mem-add-error").hidden = true;
  $("mem-add-dialog").showModal();
  $("mem-new-email").focus();
});
$("mem-empty-add").addEventListener("click", () => $("mem-add").click());

$("mem-add-submit").addEventListener("click", async () => {
  const button = $("mem-add-submit");
  const errorBox = $("mem-add-error");
  errorBox.hidden = true;
  const email = $("mem-new-email").value.trim();
  const password = $("mem-new-password").value;
  if (!email || !password) { errorBox.textContent = "请填写邮箱和初始密码。"; errorBox.hidden = false; return; }
  button.disabled = true;
  button.textContent = "创建中…";
  try {
    const created = await api("members", {method: "POST",
      body: {email, password, role: $("mem-new-role").value, email_verified: true}});
    toast("已创建用户「" + created.email + "」，可以把邮箱和初始密码发给对方了。");
    $("mem-add-dialog").close();
    await loadMembers();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "创建";
  }
});

$("mem-refresh").addEventListener("click", () => loadMembers());
$("mem-prev").addEventListener("click", () => loadMembers(membersState.page - 1));
$("mem-next").addEventListener("click", () => loadMembers(membersState.page + 1));
$("mem-search").addEventListener("input", event => {
  membersState.q = event.target.value.trim();
  membersState.page = 1;
  clearTimeout($("mem-search").timer);
  $("mem-search").timer = setTimeout(() => loadMembers(), 300);
});
$("mem-balance-submit").addEventListener("click", submitBalance);

/* ══════════════════════════════════════════════════════════════════
   服务设置
   ──────────────────────────────────────────────────────────────────
   读写同一份 settings_store。**PUT 是浅合并**（实测确认）：
   只发「改动的键」，绝不整份回传 —— 整份回传会让网页端与桌面端
   各自拼的对象互相覆盖，表现成「网页端刚改完，桌面端那边没了」。

   smtp.password 的语义：**不传 = 不改，传 "" = 清空**。
   所以留空时**不把这个键放进请求体**。
   ══════════════════════════════════════════════════════════════════ */

let settingsCache = null;

async function loadSettings() {
  $("set-error").hidden = true;
  $("set-ok").hidden = true;
  try {
    const s = await api("settings");
    settingsCache = s;
    renderSettings(s);
  } catch (error) {
    $("set-error").textContent = "读取设置失败：" + error.message;
    $("set-error").hidden = false;
  }
}

function renderSettings(s) {
  const smtp = s.smtp || {};
  $("set-allow-register").checked = Boolean(s.allow_register);
  $("set-require-code").checked = Boolean(s.require_email_code);
  $("set-self-redeem").checked = Boolean(s.allow_self_service_redeem);
  $("set-site-name").value = s.site_name || "";
  $("set-email-subject").value = s.email_subject || "";
  $("set-email-body").value = s.email_body || "";

  $("set-smtp-enabled").checked = Boolean(smtp.enabled);
  $("set-smtp-host").value = smtp.host || "";
  $("set-smtp-port").value = smtp.port || 465;
  $("set-smtp-username").value = smtp.username || "";
  $("set-smtp-from").value = smtp.from_addr || "";
  $("set-smtp-ssl").checked = smtp.use_ssl !== false;
  // 口令永不回显：只告诉管理员「配过没有」。
  $("set-smtp-password").value = "";
  $("set-smtp-password").placeholder = smtp.has_password ? "已配置 · 留空表示不修改" : "尚未配置";
  $("set-smtp-pw-state").textContent = "口令状态：" + (smtp.has_password ? "已配置" : "未配置");

  // 危险组合必须在设置页当场说清，而不是等用户去注册时才发现。
  // 文案直接绑定服务端的 register_locked_reason，不自己拼 ——
  // 将来管理员改了规则，登录页与这里会自动同步。
  const box = $("set-smtp-warning");
  const reason = s.register_locked_reason || "";
  if (reason) {
    box.textContent = "⚠️ 当前配置会让所有人都无法注册：" + reason;
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}

function settingsFeedback(message, ok) {
  const box = ok ? $("set-ok") : $("set-error");
  const other = ok ? $("set-error") : $("set-ok");
  other.hidden = true;
  box.textContent = message;
  box.hidden = false;
}

/** 提交一组「改动的键」，配合服务端的浅合并。 */
async function patchSettings(patch, okMessage, button) {
  if (button) { button.disabled = true; button.dataset.label = button.textContent; button.textContent = "保存中…"; }
  try {
    const saved = await api("settings", {method: "PUT", body: patch});
    settingsCache = saved;
    renderSettings(saved);
    settingsFeedback(okMessage, true);
  } catch (error) {
    settingsFeedback(error.message, false);
  } finally {
    if (button) { button.disabled = false; button.textContent = button.dataset.label || button.textContent; }
  }
}

$("set-register-form").addEventListener("submit", event => {
  event.preventDefault();
  patchSettings({
    allow_register: $("set-allow-register").checked,
    require_email_code: $("set-require-code").checked,
    allow_self_service_redeem: $("set-self-redeem").checked,
    site_name: $("set-site-name").value.trim(),
  }, "注册与站点设置已保存。", $("set-register-submit"));
});

$("set-smtp-form").addEventListener("submit", event => {
  event.preventDefault();
  const smtp = {
    enabled: $("set-smtp-enabled").checked,
    host: $("set-smtp-host").value.trim(),
    port: Number($("set-smtp-port").value) || 465,
    username: $("set-smtp-username").value.trim(),
    from_addr: $("set-smtp-from").value.trim(),
    use_ssl: $("set-smtp-ssl").checked,
  };
  // 口令留空 = 不修改（不放这个键）；填了才传。
  // 「不传」与「传空串」是两种完全不同的意图，不能混。
  const password = $("set-smtp-password").value;
  if (password) smtp.password = password;
  patchSettings({smtp}, password ? "发信设置已保存（口令已更新）。" : "发信设置已保存。",
                $("set-smtp-submit"));
});

$("set-template-form").addEventListener("submit", event => {
  event.preventDefault();
  patchSettings({
    email_subject: $("set-email-subject").value,
    email_body: $("set-email-body").value,
  }, "邮件模板已保存。", $("set-template-submit"));
});

$("set-smtp-test").addEventListener("click", async () => {
  const button = $("set-smtp-test");
  const note = $("set-smtp-test-note");
  button.disabled = true;
  button.textContent = "发送中…";
  note.hidden = false;
  note.className = "fine";
  note.textContent = "正在连接 SMTP 服务器…";
  try {
    const body = {};
    const to = $("set-smtp-test-to").value.trim() || $("set-smtp-username").value.trim();
    if (to) body.to = to;
    const password = $("set-smtp-password").value;
    if (password) body.password = password;
    const result = await api("settings/smtp-test", {method: "POST", body});
    if (result.ok) {
      note.className = "fine ok";
      note.textContent = "测试邮件已发送到 " + (result.to || to || "（未指定收件人）") + "。";
    } else {
      // 失败原因的键是 error，不是 message。
      // 两个键都读，免得换个版本键名一变就退化成一句没信息量的「失败」。
      note.className = "fine error";
      note.textContent = "发送失败：" + (result.error || result.message || "未知原因");
    }
  } catch (error) {
    note.className = "fine error";
    note.textContent = "发送失败：" + error.message;
  } finally {
    button.disabled = false;
    button.textContent = "发送测试邮件";
  }
});
