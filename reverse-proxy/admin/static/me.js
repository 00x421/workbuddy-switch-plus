/* 用户中心（/me）。
 *
 * 数据全部来自 /me/api/*，本文件不内联任何身份数据。
 * ⚠️ CSRF 口径：/me/api/* 与 /login/api/* 一样不需要 X-CSRF-Token
 *   （只有 /admin/api/* 才需要）。别混。
 */
const $ = (id) => document.getElementById(id);

const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** fetch 包装：401 一律当作「未登录」。
 *  ⚠️ 不要在这里做 location.replace —— 跳转只由 boot() 决定一处，
 *  否则将来加一个接口就会多一个跳转入口，早晚打架。 */
async function api(path, options = {}) {
  const response = await fetch("/me/api/" + path, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let data = null;
  try { data = await response.json(); } catch { /* 空响应体是正常的（204 等） */ }
  if (!response.ok) {
    const error = new Error((data && (data.detail || data.error)) || ("请求失败（" + response.status + "）"));
    error.status = response.status;
    throw error;
  }
  return data;
}

function setNotice(text, kind) {
  const el = $("me-notice");
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

/** 千分位。数字来自服务端，但服务端可能回 null/字符串**，所以先转数。 */
function num(value) {
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return n.toLocaleString("zh-CN");
}

/** 金额：一律按整数「厘」理解，转成积分显示时再除 1000。
 *  ⚠️ 绝不用浮点累加 —— 只做一次除法用于显示。 */
function credit(value) {
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return (n / 1000).toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 3 });
}

function stamp(ts) {
  return ts ? new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function renderOverview(data) {
  const balanceMilli = data.balance_milli != null
    ? Number(data.balance_milli)
    : Number(data.balance || 0) * 1000;
  $("me-balance").textContent = credit(balanceMilli);
  $("me-balance-hint").textContent = data.has_owner === false
    ? "积分（当前身份未绑定归属，余额按 0 算）"
    : "积分";
  const totals = data.totals || {};
  $("me-calls").textContent = num(totals.calls);
  $("me-tokens").textContent = num(totals.tokens);
  const keys = data.keys || [];
  $("me-key-count").textContent = num(keys.length);
  renderKeys(keys);
}

function renderKeys(keys) {
  const body = $("me-keys-body");
  const empty = $("me-keys-empty");
  body.innerHTML = "";
  if (!keys.length) { empty.hidden = false; return; }
  empty.hidden = true;
  for (const k of keys) {
    const tr = document.createElement("tr");
    const on = k.enabled !== false;
    tr.innerHTML =
      "<td>" + esc(k.name || "未命名") + "</td>" +
      "<td class='mono'>" + esc(k.hint || "—") + "</td>" +
      "<td><span class='tag " + (on ? "ok" : "off") + "'>" + (on ? "启用" : "停用") + "</span></td>" +
      "<td class='align-right'>" + num(k.used_tokens) + "</td>";
    body.appendChild(tr);
  }
}

const FLOW_LABEL = {
  recharge: "管理员充值", redeem: "兑换码", spend: "消费",
  grant: "赠送", adjust: "调整", refund: "退款", revoke: "撤销",
};

function renderFlows(flows) {
  const body = $("me-flows-body");
  const empty = $("me-flows-empty");
  body.innerHTML = "";
  if (!flows.length) { empty.hidden = false; return; }
  empty.hidden = true;
  for (const f of flows) {
    const tr = document.createElement("tr");
    // ⚠️ 字段名钉子：流水用 ts / balance（不是 time / balance_milli），
    //    契约里曾写错，照契约写会让「时间」列全显 —、余额列取不到值。
    const milli = Number(f.milli ?? 0);
    const sign = milli > 0 ? "+" : "";
    tr.innerHTML =
      "<td>" + esc(stamp(f.ts)) + "</td>" +
      "<td>" + esc(FLOW_LABEL[f.kind] || f.kind || "—") + "</td>" +
      "<td class='mono'>" + sign + credit(milli) + "</td>" +
      "<td class='align-right mono'>" + credit(f.balance) + "</td>";
    body.appendChild(tr);
  }
}

async function loadAll() {
  const [overview, flows] = await Promise.all([
    api("overview?days=30"),
    api("flows?limit=100"),
  ]);
  renderOverview(overview || {});
  renderFlows((flows && flows.flows) || []);
  if (overview && overview.user && overview.user.email) {
    $("me-who").textContent = overview.user.email;
  }
}

// ── 启动 ────────────────────────────────────────────────────────
// 唯一决定「跳不跳」的地方。顺序很重要：
//   1) 先问 /login/api/me（那是登录态的唯一真相源，且不受总闸影响之外的因素干扰）；
//   2) 未登录 -> replace("/login")；
//   3) 已登录 -> 留在本页渲染。
async function boot() {
  try {
    await fetch("/login/api/me", { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error("not signed in");
        return r.json();
      });
  } catch {
    // 未登录（或总闸关闭）。回登录页，不是在这里显示错误 ——
    // 用户此刻的合理动作就是去登录。
    location.replace("/login");
    return;
  }

  $("boot").hidden = true;
  $("me-page").hidden = false;
  try {
    await loadAll();
  } catch (error) {
    if (error.status === 401) { location.replace("/login"); return; }
    if (error.status === 403) {
      // 总闸被关：留在本页给出可读的原因，不要跳走 ——
      // 跳走会让用户以为「地址错了」，而这是管理员关的功能。
      setNotice(error.message, "warning");
      return;
    }
    setNotice(error.message, "warning");
  }
}

$("sign-out").addEventListener("click", async () => {
  const button = $("sign-out");
  button.disabled = true;
  try {
    await fetch("/login/api/logout", { method: "POST", credentials: "same-origin" });
  } catch { /* 已过期也算登出成功 */ }
  location.replace("/login");
});

$("redeem-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("redeem-submit");
  const input = $("redeem-code");
  showError("redeem-error", "");
  const code = input.value.trim();
  if (!code) { showError("redeem-error", "请输入兑换码。"); return; }
  button.disabled = true;
  button.textContent = "兑换中…";
  try {
    const result = await api("redeem", { method: "POST", body: { code } });
    setNotice("兑换成功，已到账 " + credit(result && result.milli) + " 积分。", "ok");
    input.value = "";
    await loadAll();
  } catch (error) {
    showError("redeem-error", error.message);
  } finally {
    button.disabled = false;
    button.textContent = "兑换";
  }
});

boot();