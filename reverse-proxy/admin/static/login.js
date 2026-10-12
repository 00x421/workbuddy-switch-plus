/* 登录页（只做登录）。

   注册已拆到独立页 /register —— 本文件不再有任何注册/选项卡逻辑。
   拆分的理由：用户要求「登录和注册分开」。副作用是 /register 必须**自己**
   判断注册可不可用 —— 以前是登录页把注册 tab 藏起来，但藏入口挡不住
   直接敲 URL 的人。

   三条纪律（与拆分前一致）：
   1. **本页 HTML 是纯骨架**，任何身份数据都不得内联 ——
      已登录与未登录取回的 HTML 必须逐字节一致。数据一律在这里运行时取。
   2. **跳转全部由 JS 决定**，服务端不做 302。
      理由：key 直连的客户端在浏览器里没有 cookie，服务端 302 会把它们全挡住。
   3. **所有用户可见文本一律 textContent**，不用 innerHTML 拼。

   接口（均来自 /login/api/*，字段名以真机实测为准）：
     GET  /login/api/options -> {allow_register, require_email_code, site_name,
                                 smtp_ready, register_locked_reason, site_ready}
     POST /login/api/login   -> {ok:true, user:{...}} + Set-Cookie
     GET  /login/api/me      -> {ok:true, user:{...}} / 401
*/

const $ = (id) => document.getElementById(id);

// 登录成功后的落点：用户中心 /me。
//


//
// 注意不要写成「已登录就 replace(LANDING)」而不判断当前页，
// 那在 /login 上会变成无限自跳。本文件的做法是：
//   · boot()  先判 location.pathname !== LANDING 再跳；
//   · 提交成功那一处是「动作终点」，直接跳，不判。
const LANDING = "/me";

/** 已登录时把页面切成「已登录」状态：不留一个点不动、也不知道成没成的登录页。
 *
 *  只切 UI、绝不跳转。boot() 里已登录但当前就在本页时走这条路
 *  （正常情况 boot 会先跳去 /me，这里是兜底，见 LANDING 的注释）。 */
function showSignedIn(member) {
  const form = $("login-form");
  if (form) {
    form.hidden = true;
    form.remove();
  }
  const email = (member && member.email) || "";
  setNotice(email ? "已登录：" + email : "已登录。", "ok");
  const foot = document.getElementById("foot-register");
  if (foot) foot.hidden = true;   // 已登录不该再引导去注册
  // 管理员入口同理：已登录态下卡片里已经有「管理后台」链接，
  // 页脚再留一个是重复的。
  const adminFoot = document.querySelector(".admin-entry");
  if (adminFoot) adminFoot.hidden = true;
  const box = document.getElementById("signed-in");
  if (box) {
    box.hidden = false;
    const who = document.getElementById("signed-in-email");
    if (who) who.textContent = email;
    const out = document.getElementById("sign-out");
    if (out) out.hidden = false;
  }
}

// 退出登录：POST /login/api/logout 后回到未登录的登录页。
// 用 location.reload() 而不是 replace("/login")：当前已经在 /login，
// reload 会把页面恢复成干净的登录表单（boot 会重新走一遍判断）。
// 不直接 replace("/me") 之类 —— 登出后就该留在登录页。
const signOutButton = document.getElementById("sign-out");
if (signOutButton) signOutButton.addEventListener("click", async () => {
  signOutButton.disabled = true;
  try { await api("logout", { method: "POST" }); } catch { /* 已过期也算登出成功 */ }
  location.reload();
});

/** 会话类请求必须带 cookie。
 *  写成 credentials:"omit" 会导致「刚登录成功、下一个请求却 401」。 */
async function api(path, options = {}) {
  const init = {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
  };
  if (options.body !== undefined) init.body = JSON.stringify(options.body);
  let response;
  try {
    response = await fetch("/login/api/" + path, init);
  } catch {
    throw new Error("连不上本机服务，请确认反代服务正在运行。");
  }
  let data = {};
  try { data = await response.json(); } catch { /* 空响应体合法 */ }
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "";
    const error = new Error(detail || "操作失败，请稍后再试");
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function setNotice(text, kind) {
  const el = $("notice");
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  el.className = "banner" + (kind ? " " + kind : "");
  el.textContent = text;   // textContent：绝不用 innerHTML
  el.hidden = false;
}

function showError(id, message) {
  const el = $(id);
  if (!message) { el.hidden = true; el.textContent = ""; return; }
  el.textContent = message;
  el.hidden = false;
}

/** 提交中：禁用按钮并换文案，避免重复提交。 */
function busy(button, on, busyText) {
  if (on) {
    button.dataset.label = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

// ── 启动：先判断登录态，再决定去向 ──────────────────────────────

async function boot() {
  // 0) 从注册页过来？先显示「注册成功」。
  //
  //    注册成功时服务端已经下发了会话 cookie，所以下面第 1 步会判定「已登录」。
  //    如果不特别处理，用户会被立刻弹走，根本看不到「请用刚设置的密码登录」——
  //    而用户的原话正是「注册成功返回登陆页面开始登陆」，即他要看到登录页。
  //    所以带 ?registered=1 时：显示提示，并跳过自动跳转，把页面留给用户。
  const justRegistered = new URLSearchParams(location.search).get("registered") === "1";
  if (justRegistered) {
    setNotice("注册成功，请用刚设置的密码登录。", "ok");
    document.querySelector("form").reset();
    $("login-email").focus();
    return;
  }

  // 1) 已登录？直接进用户中心。
  //
  // ⚠️ 注意不要写成「已登录就 replace(LANDING)」而不判断当前页 ——
  //    那在 /login 上会变成无限自跳。这里用 pathname 判断后再跳。
  try {
    const me = await api("me");
    if (location.pathname !== LANDING) { location.replace(LANDING); return; }
    showSignedIn(me && me.user);
    return;
  } catch { /* 未登录是正常情况，继续 */ }

  // 2) 取配置。本页只用它拿站点名做标题 ——
  //    「注册能不能用」是 /register 的判断，本页不再掺和。
  try {
    const data = await api("options");
    if (data.site_name) document.title = "登录 · " + data.site_name;
  } catch (error) {
    setNotice(error.message, "warning");
  }

  // 3) 聚焦邮箱输入框。
  const first = $("login-email");
  if (first && !first.disabled) first.focus();
}

// ── 事件 ────────────────────────────────────────────────────────

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("login-submit");
  showError("login-error", "");
  const email = $("login-email").value.trim();
  const password = $("login-password").value;
  if (!email || !password) { showError("login-error", "请填写邮箱和密码。"); return; }

  busy(button, true, "登录中…");
  try {
    const result = await api("login", { method: "POST", body: { email, password } });
    // 登录成功 -> 用户中心。
    // 这里是「登录成功」这个动作的终点，不存在自跳问题。
    location.replace(LANDING);
  } catch (error) {
    showError("login-error", error.message);
    busy(button, false);
  }
});

boot();