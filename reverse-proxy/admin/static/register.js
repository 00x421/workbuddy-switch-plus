/* 注册页（只做注册）。

   与 /login 的关键区别，也是本文件存在的主要理由：

   ⭐ **注册是否可用必须由本页自己判断。**
   拆分前是登录页把「注册」选项卡隐藏掉 —— 那只是**藏入口**。
   现在用户可以直接敲 /register，藏入口挡不住，
   所以本页必须自己拉 /login/api/options 并在被锁时**不渲染表单**。

   判据**只用 register_locked_reason 一个**：
   它的优先级由服务端保证（allow_register 先于 SMTP 判断），
   前端不复刻那套顺序 —— 两边各写一套判断，迟早会得出不同结论。

   三条纪律（与登录页一致）：
   1. **本页 HTML 是纯骨架**，不得内联任何身份数据。
      （拆分后 /login 的逐字节约束不受影响，但本页同样按这个标准写，
        免得以后有人把 options 的结果塞进 HTML。）
   2. **跳转全部由 JS 决定**，服务端不做 302。
   3. **所有用户可见文本一律 textContent**，不用 innerHTML 拼。

   接口（均来自 /login/api/*，字段名以真机实测为准）：
     GET  /login/api/options   -> {allow_register, require_email_code, site_name,
                                   smtp_ready, register_locked_reason, site_ready}
     POST /login/api/register  -> {ok:true, user:{...}} + Set-Cookie
     POST /login/api/send-code -> {ok, expires_in, dev_mode, code?}
*/

const $ = (id) => document.getElementById(id);

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

/** 页面状态。requireEmailCode 由 options 决定，绝不写死。 */
const STATE = {
  requireEmailCode: true,
  lockedReason: "",
  countdown: 0,
  timer: null,
};

function setNotice(text, kind) {
  const el = $("notice");
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  el.className = "banner" + (kind ? " " + kind : "");
  el.textContent = text;
  el.hidden = false;
}

function showError(id, message) {
  const el = $(id);
  if (!message) { el.hidden = true; el.textContent = ""; return; }
  el.textContent = message;
  el.hidden = false;
}

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

/** 注册不可用：用说明块替代表单。
 *  为什么移除表单而不是只禁用它：禁用按钮会让人反复点、以为是自己没填对；
 *  这里是「现在谁都注册不了」，要一眼看出不是自己的问题。 */
function lockForm(reason) {
  STATE.lockedReason = reason;
  const form = $("register-form");
  if (form) {
    form.hidden = true;
    form.remove();   // 彻底不渲染：URL 直连也拿不到表单，也不会有「禁用但可提交」的中间态
  }
  $("locked-reason").textContent = reason;
  $("locked-box").hidden = false;
  const hint = $("locked-hint");
  hint.textContent = "已经有账户？直接去登录，或联系管理员。";
  hint.hidden = false;
}

// ── 发码按钮倒计时 ──────────────────────────────────────────────

function tickCountdown() {
  const button = $("send-code");
  if (!button) return;
  if (STATE.countdown <= 0) {
    clearInterval(STATE.timer);
    STATE.timer = null;
    button.disabled = false;
    button.textContent = "发送验证码";
    return;
  }
  button.disabled = true;
  button.textContent = STATE.countdown + " 秒后可重发";
  STATE.countdown -= 1;
}

function startCountdown(seconds) {
  STATE.countdown = Math.max(0, Number(seconds) || 60);
  tickCountdown();
  if (STATE.timer) clearInterval(STATE.timer);
  STATE.timer = setInterval(tickCountdown, 1000);
}

// ── 启动 ────────────────────────────────────────────────────────

async function boot() {
  // ⚠️ 刻意不做「已登录就跳走」：
  //    一个已登录的人想再注册一个账户是正常操作，不该被弹回用户中心。
  //    （/login 有这条逻辑，因为它是在「进不去」的时候才被访问的。）
  let data;
  try {
    data = await api("options");
  } catch (error) {
    setNotice(error.message, "warning");
    return;
  }

  if (data.site_name) document.title = "注册 · " + data.site_name;

  STATE.requireEmailCode = data.require_email_code !== false;

  // 判据只有 register_locked_reason。服务端已经把
  //   「管理员关了注册」和「要验证码但没配 SMTP」两种情况收敛成这一个字段，
  //   前端再判断一次 allow_register 就是复刻优先级 —— 不复刻。
  const reason = typeof data.register_locked_reason === "string"
    ? data.register_locked_reason : "";
  if (reason) {
    lockForm(reason);
    setNotice(reason, "warning");
    return;
  }

  // 可用：按配置决定验证码字段出不出现。
  $("code-field").hidden = !STATE.requireEmailCode;
  $("reg-code").required = STATE.requireEmailCode;

  const first = $("reg-email");
  if (first && !first.disabled) first.focus();
}

// ── 事件 ────────────────────────────────────────────────────────

// ⚠️ 注意这三个监听都在顶层、且在 boot() 之前执行：
//    它们依赖「此刻表单还在 DOM 里」。boot() 是 async 的，所以 lockForm()
//    必然晚于这里 —— 但那是时序巧合，不是保证。所以每个都先判存在，
//    免得以后有人把 boot() 提前成同步调用，就在锁定态抛 TypeError。
const form = $("register-form");
if (form) form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("register-submit");
  showError("register-error", "");
  const email = $("reg-email").value.trim();
  const password = $("reg-password").value;
  const code = $("reg-code").value.trim();
  if (!email || !password) { showError("register-error", "请填写邮箱和密码。"); return; }
  if (STATE.requireEmailCode && !code) { showError("register-error", "请填写邮箱验证码。"); return; }

  busy(button, true, "注册中…");
  try {
    const body = { email, password };
    if (STATE.requireEmailCode) body.code = code;
    await api("register", { method: "POST", body });
    // 2026-10 用户裁定：「注册成功返回登陆页面开始登陆」。
    //
    // 原先注册成功是直接进用户中心（/portal/）—— 那是已登录即入内的语义。
    // 现在改成回登录页让用户用刚设置的密码登录一次，理由：
    //   · 用户明确要求；
    //   · /portal/ 已被整体删除，不能留一个跳向 404 的重定向。
    //
    // 服务端注册成功已经下发了会话 cookie，所以用户在 /login 上其实已登录；
    // login.js 的 boot() 会检测到登录态 —— 为不让它把人直接弹走，
    // 这里带一个 ?registered=1，由登录页优先显示「注册成功」而不是立刻跳转。
    location.replace("/login?registered=1");
  } catch (error) {
    // 配置级失败（注册被关闭 / 无 SMTP）不是「这次没填对」，
    // 而是「现在谁都注册不了」—— 直接把表单换掉，措辞与位置都更醒目。
    if (error.status === 503 || error.status === 403) {
      lockForm(error.message);
      setNotice(error.message, "warning");
      return;
    }
    showError("register-error", error.message);
    busy(button, false);
  }
});

const sendButton = $("send-code");
if (sendButton) sendButton.addEventListener("click", async () => {
  const button = $("send-code");
  const hint = $("code-hint");
  const email = $("reg-email").value.trim();
  if (!email) { showError("register-error", "请先填写邮箱地址。"); $("reg-email").focus(); return; }

  button.disabled = true;
  try {
    const result = await api("send-code", { method: "POST", body: { email, purpose: "register" } });
    startCountdown(60);
    // 开发模式（未配置 SMTP）时服务端会把验证码回给调用方并标 dev_mode。
    // 显示它，但必须说明这只是本机，否则会被误当成线上行为。
    if (result.dev_mode && result.code) {
      hint.textContent = "开发模式（未配置 SMTP）：验证码 " + result.code;
      $("reg-code").value = result.code;
    } else {
      hint.textContent = "验证码已发送，10 分钟内有效。收不到请检查垃圾邮件。";
    }
  } catch (error) {
    showError("register-error", error.message);
    button.disabled = false;
  }
});

// 输入邮箱后恢复提示文案。
const emailInput = $("reg-email");
if (emailInput) emailInput.addEventListener("input", () => {
  const hint = $("code-hint");
  if (hint) hint.textContent = "验证码 10 分钟内有效，收不到请检查垃圾邮件。";
});

boot();