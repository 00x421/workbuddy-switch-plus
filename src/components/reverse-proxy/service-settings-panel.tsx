import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { ChevronRight, Loader2, MailCheck, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  SMTP_MISSING_REGISTER_NOTICE, rp, smtpTestMessage, type RpServiceSettings, type RpSettingsPatch,
} from "@/lib/reverse-proxy";
import { Banner, Field, PanelHeader } from "./rp-shared";

/**
 * 服务设置（能力矩阵第 7-10 项）。
 *
 * 与网页端管理后台的「服务设置」共用同一套 `/admin/api/settings` 接口、同一批字段名
 * 管理员在哪一端改都是同一份配置，不会出现「网页端改完桌面端没了」。
 *
 * ## 注册开关与 SMTP 的联动（用户两次指示，以后者为准）
 *
 *   allow_register=false                            → 注册入口关闭
 *   开放注册 + SMTP 可用 + 要求验证码                → 必须邮箱验证码
 *   开放注册 + 无 SMTP + 要求验证码              → 注册失败，不降级！
 *   开放注册 + 无 SMTP + 关闭验证码开关              → 免验证直接注册
 *
 * 关键在第 3 行：不做自动降级。降级会让「验证码」这道防线看起来存在其实不存在。
 * 免验证注册只在管理员显式关闭验证码开关后才发生。
 *
 * 所以「开着验证码开关但没有 SMTP」是一个会让所有人都注册不了的错误配置，
 * 页面必须在最显眼的地方说出来，并且用用户指定的原文。
 */

export function ServiceSettingsPanel() {
  const [settings, setSettings] = useState<RpServiceSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  /** SMTP 密码只在管理员主动输入时才提交；留空 = 不改（浅合并语义）。 */
  const [smtpPassword, setSmtpPassword] = useState("");
  /** 测试发信的收件人；留空则用发件人自己。 */
  const [testTo, setTestTo] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSettings(await rp.settings());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setSettings(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  /** 改本地草稿。真正落盘要等「保存」。 */
  function patch(next: RpSettingsPatch) {
    setSettings((current) => (current ? ({ ...current, ...next } as RpServiceSettings) : current));
  }

  function patchSmtp(next: Partial<RpServiceSettings["smtp"]>) {
    setSettings((current) => current
      ? { ...current, smtp: { ...current.smtp, ...next } }
      : current);
  }

  /**
   * 保存。
   *
   * 只提交改动过的字段不够、整份回传又太多 —— 这里提交的是「界面上这份表单」
   * 的完整可编辑字段，但 SMTP 密码例外：只有管理员这次真的输了才带上，
   * 否则服务端的浅合并会保留原密码（传空串的含义是「清空」，不能拿来当「没改」）。
   */
  async function save() {
    if (!settings) return;
    setSaving(true);
    setError("");
    try {
      const smtp: RpSettingsPatch["smtp"] = {
        enabled: settings.smtp.enabled,
        host: settings.smtp.host,
        port: Number(settings.smtp.port) || 0,
        username: settings.smtp.username,
        from_addr: settings.smtp.from_addr,
        use_ssl: settings.smtp.use_ssl,
      };
      // 只有真的输入了才发 password，不传 = 不改密码。
      if (smtpPassword) smtp.password = smtpPassword;
      const saved = await rp.saveSettings({
        // 总闸必须在提交清单里：漏了它，界面拨了、保存也「成功」，
        // 但服务端收到的补丁里根本没有这个键 —— 表现是「拨完一保存又弹回去」。
        user_system_enabled: settings.user_system_enabled,
        allow_register: settings.allow_register,
        require_email_code: settings.require_email_code,
        site_name: settings.site_name,
        allow_self_service_redeem: settings.allow_self_service_redeem,
        smtp,
      });
      setSettings(saved);
      setSmtpPassword("");
      toast.success("服务设置已保存");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  async function testSmtp() {
    setTesting(true);
    setError("");
    try {
      const result = await rp.testSmtp(testTo.trim() || undefined);
      // 成功与失败用不同的键（成功是 to，失败是 error），统一走 smtpTestMessage 取值。
      // 只读 message 会让失败时弹一句没信息量的「测试失败」，
      // 而管理员最需要知道的恰恰是失败原因（没配主机 / 端口错 / 认证被拒）。
      if (result.ok) toast.success(smtpTestMessage(result));
      else toast.error(smtpTestMessage(result));
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setTesting(false);
    }
  }

  if (loading) {
    return (
      <div className="flex flex-col gap-5">
        <PanelHeader title="服务设置" desc="注册开关、邮箱验证码与 SMTP。与网页端共用同一份配置。" />
        <div className="rounded-xl border border-border/70 px-4 py-10 text-center text-xs text-muted-foreground">正在读取…</div>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="flex flex-col gap-5">
        <PanelHeader title="服务设置" desc="注册开关、邮箱验证码与 SMTP。与网页端共用同一份配置。" />
        {error ? <Banner tone="bad">{error}</Banner> : <Banner tone="muted">读取设置失败。</Banner>}
        <div><Button size="sm" variant="outline" onClick={() => void load()}>重试</Button></div>
      </div>
    );
  }

  /**
   * 「SMTP 可用」只认服务端返回的 `smtp_ready`。
   *
   * 绝不在这里按 host 非空自己判一次 —— 那会造出第二个真相源，
   * 于是出现「页面说已配置、注册却说未配置 SMTP」这种自相矛盾。
   * 服务端没给这个字段时退回 smtp.enabled，同样不做本地猜测。
   */
  const smtpReady = settings.smtp_ready ?? settings.smtp.enabled;
  /**
   * 会让所有人都注册不了的错误配置。
   *
   * 服务端的 `register_locked_reason` 非空就是锁死状态（它内部只算一个真相源，
   * 见 `settings_store.locked_out_reason`）。没给这个字段时按同一口径本地推一遍，
   * 保证界面不会因为服务端少发一个字段就漏报。
   */
  const lockedReason = String(settings.register_locked_reason || "");
  const registerBlocked = Boolean(lockedReason)
    || (settings.allow_register && settings.require_email_code && !smtpReady);
  /**
   * 总闸状态。缺字段时判成「关」，与服务端 `user_system_enabled()` 的兜底方向一致。
   *
   * 判成「关」而不是「开」是有意的：老服务端不给这个字段时，界面显示「已关闭」
   * 顶多是保守；显示「已开启」却实际是关的，会让管理员以为用户能用。
   */
  const userSystemOn = Boolean(settings.user_system_enabled);

  return (
    <div className="flex flex-col gap-5">
      <PanelHeader
        title="服务设置"
        desc="注册开关、邮箱验证码与 SMTP。这里与网页端管理后台是同一份配置，改任意一端都立即对另一端生效。"
        action={
          <Button size="sm" disabled={saving} onClick={() => void save()}>
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
            {saving ? "保存中…" : "保存"}
          </Button>
        }
      />

      {error ? <Banner tone="bad">{error}</Banner> : null}

      {/* 最要紧的一条：当前配置会让谁都注册不了。放在所有开关之前。
          ⚠️ 两条既有约束**不能因为「排版精简」而删**（t14 验证过）：
            ① `smtpReady` 只认服务端 `smtp_ready`，绝不本地按 host 猜；
            ② `register_locked_reason` 非空 = 锁死，原样透出。
          下面只压缩措辞，判断逻辑一个字没动。 */}
      {registerBlocked ? (
        <Banner tone="bad">
          <div className="font-medium">当前配置会导致所有人都无法注册</div>
          {/* 服务端已经给了原因就用它的原话（单一真相源）；没有才退回用户指定原文。 */}
          <div className="mt-1">{lockedReason || SMTP_MISSING_REGISTER_NOTICE}</div>
          <Disclosure
            summary="怎么办？"
            detail="关闭「邮箱验证码注册」即可让用户免验证注册；或者保持开启但把 SMTP 配好。"
          />
        </Banner>
      ) : null}

      {/* 未配置 SMTP 时也要给提示，但语气按「会不会挡住注册」分档。 */}
      {!smtpReady ? (
        <Banner tone={registerBlocked ? "bad" : "warn"}>
          {registerBlocked
            ? SMTP_MISSING_REGISTER_NOTICE
            : "未配置 SMTP：当前免验证注册。"}
        </Banner>
      ) : null}

      {/* ⭐ 总闸放在**最上方**，且视觉上明显区别于下面的普通开关：
          · 用户的原话是「这个不开启都用不了」，所以它必须第一个被看到；
          · 用 emerald 高亮 + 更重的边框，让「现在是开还是关」一眼可辨。 */}
      <section className={
        "rounded-xl border-2 px-4 py-3.5 transition-colors " +
        (userSystemOn
          ? "border-emerald-500/60 bg-emerald-500/[0.06]"
          : "border-border bg-card")
      }>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold">用户系统</span>
              <span className={
                "rounded-full px-2 py-0.5 text-[10px] font-medium " +
                (userSystemOn
                  ? "bg-emerald-600 text-white"
                  : "bg-muted text-muted-foreground")
              }>
                {userSystemOn ? "已开启" : "已关闭"}
              </span>
            </div>
            {/* 主文案只留一句短的；长解释收进折叠区。 */}
            <div className="mt-0.5 text-[11px] text-muted-foreground">
              {userSystemOn
                ? "注册、登录、用户中心可用。"
                : "全部用户功能不可用。已发出的 API Key 不受影响。"}
            </div>
          </div>
          <Switch
            checked={userSystemOn}
            onCheckedChange={(v) => patch({ user_system_enabled: v })}
            aria-label="用户系统"
          />
        </div>
        <Disclosure
          summary="这个开关管什么？"
          detail={
            "关掉后：注册、登录、用户中心、门户的登录态全部拒绝（403）。" +
            "不影响 /v1 的 API Key 直连 —— 那是核心服务，不归这个开关管；" +
            "已经发出去的密钥照常能用。"
          }
        />
      </section>

      <section className="rounded-xl border border-border/70 bg-card">
        <div className="border-b border-border/60 px-4 py-2.5 text-xs font-medium">注册与用户功能</div>
        <div className="flex flex-col gap-0 px-4 py-1.5">
          <ToggleRow
            label="开放注册"
            hint="关闭后登录页不显示注册入口，已有用户仍可登录。"
            checked={settings.allow_register}
            onChange={(v) => patch({ allow_register: v })}
          />
          <ToggleRow
            label="邮箱验证码注册"
            hint="未配置 SMTP 时注册会失败，不会降级成免验证。"
            checked={settings.require_email_code}
            onChange={(v) => patch({ require_email_code: v })}
          />
          <ToggleRow
            label="用户自助兑换"
            hint="关闭后只能由管理员兑换。"
            checked={settings.allow_self_service_redeem}
            onChange={(v) => patch({ allow_self_service_redeem: v })}
          />
          <div className="py-3">
            <Field label="站点名" hint="显示在登录页与用户中心标题上。">
              <Input
                className="w-full sm:w-80"
                value={settings.site_name}
                maxLength={60}
                placeholder="例如 WorkBuddy"
                onChange={(e) => patch({ site_name: e.target.value })}
              />
            </Field>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-border/70 bg-card">
        <div className="flex items-center justify-between gap-3 border-b border-border/60 px-4 py-2.5">
          <span className="text-xs font-medium">SMTP（发验证码用）</span>
          <span className="flex items-center gap-2 text-[11px]">
            <span className="text-muted-foreground">状态：</span>
            {smtpReady
              ? <span className="text-emerald-600 dark:text-emerald-400">已配置</span>
              : <span className="text-amber-600 dark:text-amber-400">未配置</span>}
          </span>
        </div>
        <div className="flex flex-col gap-4 px-4 py-4">
          <ToggleRow
            label="启用 SMTP"
            hint="关闭后不发送任何邮件；配合「用邮箱验证码注册」关闭即可免验证注册。"
            checked={settings.smtp.enabled}
            onChange={(v) => patchSmtp({ enabled: v })}
          />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="服务器地址">
              <Input
                value={settings.smtp.host}
                placeholder="smtp.example.com"
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => patchSmtp({ host: e.target.value })}
              />
            </Field>
            <Field label="端口">
              <Input
                type="number"
                value={String(settings.smtp.port || "")}
                placeholder="465"
                onChange={(e) => patchSmtp({ port: Number(e.target.value) || 0 })}
              />
            </Field>
            <Field label="账号">
              <Input
                value={settings.smtp.username}
                placeholder="登录 SMTP 的用户名"
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => patchSmtp({ username: e.target.value })}
              />
            </Field>
            <Field
              label="密码"
              hint={settings.smtp.has_password
                ? "已配置。留空表示不修改。"
                : "未配置。多数邮箱服务需要「授权码」而不是登录密码。"}
            >
              <div className="flex gap-2">
                <Input
                  type="password"
                  value={smtpPassword}
                  // 密码框永远不回显已存的密码（服务端也只回显布尔），
                  // 所以这里是空的，placeholder 负责说明当前状态。
                  placeholder={settings.smtp.has_password ? "••••••••（不改请留空）" : "请输入 SMTP 密码或授权码"}
                  autoComplete="new-password"
                  onChange={(e) => setSmtpPassword(e.target.value)}
                />
                {settings.smtp.has_password && smtpPassword ? (
                  <Button type="button" variant="outline" size="sm" className="shrink-0" onClick={() => setSmtpPassword("")}>
                    还原
                  </Button>
                ) : null}
              </div>
            </Field>
            <Field label="发件人" hint="收件人看到的发件地址，通常与账号相同。">
              <Input
                value={settings.smtp.from_addr}
                placeholder="noreply@example.com"
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => patchSmtp({ from_addr: e.target.value })}
              />
            </Field>
            <div className="flex items-end">
              <ToggleRow
                label="使用 SSL"
                hint="465 端口一般开；587 通常走 STARTTLS，按你的邮箱服务商说明选。"
                checked={settings.smtp.use_ssl}
                onChange={(v) => patchSmtp({ use_ssl: v })}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-end gap-2 border-t border-border/50 pt-3">
            <Field label="测试收件人" hint="留空则发给「发件人」自己。">
              <Input
                className="w-full sm:w-72"
                value={testTo}
                placeholder="留空 = 发给发件人"
                spellCheck={false}
                autoComplete="off"
                onChange={(e) => setTestTo(e.target.value)}
              />
            </Field>
            <Button variant="outline" size="sm" disabled={testing || saving} onClick={() => void testSmtp()}>
              {testing ? <Loader2 className="size-3.5 animate-spin" /> : <MailCheck className="size-3.5" />}
              {testing ? "发送中…" : "测试发信"}
            </Button>
            <Disclosure
              summary="测试发信注意事项"
              detail="测试用的是已保存的配置。改了地址或密码请先保存再测，否则测的还是旧配置。"
            />
          </div>
        </div>
      </section>

      <div className="flex justify-end">
        <Button size="sm" disabled={saving} onClick={() => void save()}>
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
          {saving ? "保存中…" : "保存"}
        </Button>
      </div>
    </div>
  );
}

/**
 * 一行开关：左文案 + 右控件。
 *
 * 排版规则来自用户反馈「文字太多」：主文案短（≤ 6 字为佳），
 * 解释压成一行 11px 的小字；更长的说明走 `Disclosure` 折叠区。
 *
 * 行之间用分隔线而不是大间距 —— 原来 `gap-4` 把每一项撑成一坨，
 * 一个列表看上去像五段独立内容。
 */
function ToggleRow({ label, hint, checked, onChange }: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-3 border-b border-border/40 py-3 last:border-b-0">
      <div className="min-w-0">
        <div className="text-xs font-medium text-foreground/90">{label}</div>
        {hint ? <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{hint}</div> : null}
      </div>
      <Switch checked={checked} onCheckedChange={onChange} aria-label={label} />
    </div>
  );
}

/**
 * 可折叠的解释区。默认收起 —— 用户的抱怨就是「文字太多」。
 *
 * 用原生 details/summary 而不是自己管状态：少一个 useState、少一处可能失同步的
 * 状态，而且浏览器自带键盘可达性与「页内查找能自动展开」的行为。
 */
function Disclosure({ summary, detail }: { summary: string; detail: string }) {
  return (
    <details className="group mt-2">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
        <ChevronRight className="size-3 transition-transform group-open:rotate-90" />
        {summary}
      </summary>
      <p className="mt-1.5 pl-4 text-[11px] leading-4 text-muted-foreground">{detail}</p>
    </details>
  );
}