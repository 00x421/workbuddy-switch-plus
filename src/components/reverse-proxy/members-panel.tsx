import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Coins, KeyRound, RefreshCw, Search, TriangleAlert, UserRound, UserRoundPlus } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  creditFromMilli, fmtCredit, fmtCreditTime, flowKindLabel, memberRoleLabel, memberStatusLabel, rp,
  type RpMember, type RpMemberDetail, type RpMemberList, type RpMemberStats, type RpSession,
} from "@/lib/reverse-proxy";
import { Banner, Empty, Field, PanelHeader, ToneBadge, fmtNum, type Tone } from "./rp-shared";
import { ConfirmDialog, useConfirm } from "./confirm-dialog";

/**
 * 用户管理（终端用户 / members）。
 *
 * ## 这是给谁用的
 *
 * 管理邮箱注册出来的终端用户：他们是用服务的人、是计费主体、有积分余额。
 *
 * ⚠️ 不是「后台账号」。那一套是控制台登录账号（username + admin/viewer），
 * 在旁边的「后台账号」页签里（`users-panel.tsx`），走 `/admin/api/users`。
 * 两套东西永不共用端点、界面上也分开叫。
 *
 * ## 与网页端同步
 *
 * 能力集合按 `_design/sync-spec.md` 的能力矩阵逐项对齐，一项不少：
 *   1 列表（分页 + 搜索）/ 2 详情 / 3 改角色 / 4 启用禁用 / 5 调整余额 / 6 删除。
 * 字段名、错误文案、积分口径全部沿用同一份定义（见 `lib/reverse-proxy.ts` 的常量），
 * 不在这里另起措辞。
 *
 * ## 积分口径
 *
 * 界面显示「积分」，底层是整数「厘」（1 积分 = 1000 厘）。
 * 显示一律用服务端算好的 `balance_credit` / 走 `fmtCredit`，不做浮点累加。
 */

/** 角色下拉的取值：与 `members.ROLES` 一致，只有这两个。 */
const ROLE_OPTIONS = [
  { value: "user", label: "普通用户" },
  { value: "admin", label: "管理员" },
];

function roleTone(role: string): Tone {
  return role === "admin" ? "warn" : "muted";
}

function statusTone(status: string): Tone {
  return status === "disabled" ? "bad" : "ok";
}

export function MembersPanel({ session }: { session: RpSession }) {
  const [data, setData] = useState<RpMemberList | null>(null);
  const [stats, setStats] = useState<RpMemberStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  /** 搜索框里正在编辑**的文本；回车 / 点按钮才提交成 `query`。*/
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [size, setSize] = useState(20);
  const [detail, setDetail] = useState<RpMemberDetail | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [balanceTarget, setBalanceTarget] = useState<RpMember | null>(null);
  /** 「分发账号」对话框的开合。null = 关闭。 */
  const [createOpen, setCreateOpen] = useState(false);
  const confirm = useConfirm();

  /**
   * 只有管理员能改东西。只读身份照样看得到列表（服务端 `viewer(req)` 就是这么分的），
   * 但写操作必须提前藏起来 —— 让按钮点下去才报 403，比一开始就不给按钮更糟。
   */
  const isAdmin = session.role === "admin";
  /**
   * 用「管理令牌」进来的会话（`session.via === "token"`）调不了改钱 / 删号：
   * 服务端那两条走 `require_session_admin`，明确拒绝 `wbt_` 令牌身份。
   * 所以对这类会话，把「调整余额 / 删除」置灰并说明原因，而不是让用户撞 403。
   * 改角色 / 启停走的是 `admin(req)`，令牌身份可以用，不受影响。
   */
  const tokenSession = session.via === "token";
  const canTouchMoney = isAdmin && !tokenSession;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [list, summary] = await Promise.all([
        rp.listMembers({ q: query, page, size }),
        // 总览失败不该把整页拖垮：列表才是主内容。
        rp.memberStats().catch(() => null),
      ]);
      setData(list);
      setStats(summary);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [query, page, size]);

  useEffect(() => { void load(); }, [load]);

  /**
   * 服务端在先鉴权、后查参数的顺序下返回 404/403 都不会泄露 uid 是否存在；
   * 但它可能返回「这一页被删空了」的情况。删/改之后回到第 1 页重拉，
   * 否则用户会停在一个空白页上以为数据没了。
   */
  const reloadFromFirstPage = useCallback(async () => {
    if (page !== 1) { setPage(1); return; }   // page 变化会触发 load
    await load();
  }, [page, load]);

  async function openDetail(member: RpMember) {
    setDetailBusy(true);
    try {
      setDetail(await rp.memberDetail(member.id));
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setDetailBusy(false);
    }
  }

  async function setRole(member: RpMember, role: string) {
    try {
      await rp.updateMember(member.id, { role });
      toast.success("已将 " + member.email + " 设为" + memberRoleLabel(role));
      await reloadFromFirstPage();
    } catch (cause) {
      // 服务端的原话（「至少保留一个管理员」「不能修改自己的角色」）比任何本地
      // 措辞都准确，直接显示。
      toast.error(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function setStatus(member: RpMember, status: string) {
    try {
      const result = await rp.updateMember(member.id, { status });
      toast.success(status === "disabled" ? "已禁用该用户" : "已启用该用户", {
        description: result.sessions_revoked
          ? "已同时踢掉它名下的 " + result.sessions_revoked + " 个登录会话"
          : undefined,
      });
      await reloadFromFirstPage();
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause));
    }
  }

  /**
   * 管理员分发账号 —— 「未配置 SMTP 时用户注册不了」的出口。
   *
   * 那句提示的原文就是「或者给您分发账号登录」，所以这个入口必须在用户管理里
   * 显眼可见；否则管理员看到提示却没有任何地方能兑现它。
   */
  async function createMember(email: string, password: string, role: string) {
    const row = await rp.createMember({ email, password, role });
    toast.success("已创建 " + row.email, {
      description: "把这个邮箱和密码发给对方即可登录；管理员分发的账号默认已验证。",
    });
    setCreateOpen(false);
    await reloadFromFirstPage();
  }

  function askDelete(member: RpMember) {
    // 余额非 0 时服务端默认 409。这里先提示，让管理员知道会撞上什么，
    // 而不是等他点了确认再吃一个错误。
    const hasBalance = Number(member.balance_credit || 0) !== 0;
    const keyNote = member.key_count > 0
      ? "该用户名下有 " + member.key_count + " 枚密钥，删除后将成为无主密钥（仍会继续调用上游）。"
      : "该用户名下没有密钥。";
    confirm.ask({
      title: "删除这个用户？",
      description: [
        "「" + member.email + "」将被删除，其登录会话立即失效。此操作不可撤销。",
        keyNote,
        hasBalance
          ? "该用户还有 " + fmtCredit(member.balance_credit) + " 积分余额，直接删除会被拒绝（409）；确认后将使用强制删除，余额会被记一笔结清流水。余额不再返还。"
          : null,
      ].filter(Boolean).join(" "),
      confirmLabel: hasBalance ? "强制删除" : "删除",
      danger: true,
      run: async () => {
        const result = await rp.deleteMember(member.id, hasBalance);
        toast.success("已删除 " + member.email, {
          description: result.keys_unbound > 0
            ? "已解绑 " + result.keys_unbound + " 枚密钥（未删除、未停用）"
            : undefined,
        });
      },
    });
  }

  const items = data?.items ?? [];
  const pages = data?.pages ?? 1;

  return (
    <div className="flex flex-col gap-5">
      <PanelHeader
        title="用户管理"
        desc="邮箱注册的终端用户：他们是 API 调用的计费主体，余额以「积分」计。密钥归属与余额记在用户身上，不记在密钥上。"
        action={
          <div className="flex items-center gap-2">
            {/*
             * 「分发账号」只在管理员可见。它是「未配置 SMTP 时没人能注册」的唯一出路
             * —— 服务设置里那句提示的原文就是「或者给您分发账号登录」。
             */}
            {isAdmin ? (
              <Button size="sm" disabled={loading} onClick={() => setCreateOpen(true)}>
                <UserRoundPlus className="size-3.5" />分发账号
              </Button>
            ) : null}
            <Button size="sm" variant="outline" disabled={loading} onClick={() => void load()}>
              <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />刷新
            </Button>
          </div>
        }
      />

      {/* 总览：让管理员一眼看到「有没有人、有多少钱、有没有无主密钥」。 */}
      {stats ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatBox label="用户总数" value={String(stats.total)} hint={"正常 " + stats.active + " · 已禁用 " + stats.disabled} />
          <StatBox label="管理员" value={String(stats.admins)} hint="终端用户里的管理员" />
          <StatBox label="积分总额" value={fmtCredit(stats.balance_credit)} hint="所有用户余额之和" />
          <StatBox
            label="无主密钥"
            value={String(stats.orphan_keys)}
            hint={stats.orphan_keys > 0 ? "归属已失效，仍在消耗上游" : "没有无主密钥"}
            warn={stats.orphan_keys > 0}
          />
        </div>
      ) : null}

      {!isAdmin ? (
        <Banner tone="muted">
          当前身份是只读，可以查看用户列表与详情，但不能改角色、启停、调余额或删除。
        </Banner>
      ) : null}
      {isAdmin && tokenSession ? (
        <Banner tone="warn">
          你正在用「管理令牌」登录。「调整余额」与「删除用户」在服务端只接受**会话**管理员
          （令牌身份被明确拒绝），因此这两项已置灰；改角色与启停不受影响。
        </Banner>
      ) : null}

      {/* 搜索：对齐能力矩阵第 1 项（分页 + 搜索）。 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex min-w-56 flex-1 items-center gap-2">
          <Input
            className="h-8 text-xs"
            value={draft}
            placeholder="按邮箱或用户 ID 搜索"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { setPage(1); setQuery(draft.trim()); }
            }}
          />
          <Button
            size="sm"
            variant="outline"
            className="h-8 shrink-0"
            onClick={() => { setPage(1); setQuery(draft.trim()); }}
          >
            <Search className="size-3.5" />搜索
          </Button>
          {query ? (
            <Button
              size="sm"
              variant="ghost"
              className="h-8 shrink-0"
              onClick={() => { setDraft(""); setQuery(""); setPage(1); }}
            >清除</Button>
          ) : null}
        </div>
        <Select value={String(size)} onValueChange={(v) => { setSize(Number(v)); setPage(1); }}>
          <SelectTrigger className="h-8 w-28 text-xs" aria-label="每页条数"><SelectValue /></SelectTrigger>
          <SelectContent>
            {[10, 20, 50, 100].map((n) => (
              <SelectItem key={n} value={String(n)}>每页 {n} 条</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {error ? <Banner tone="bad">{error}</Banner> : null}

      {/* 无主密钥必须显式提示：它们在跑、在消耗上游，但余额没人认领也不能兑换，
          管理员不看到它们就永远查不出「这个账号在花钱但余额不动」。 */}
      {data && data.orphan_keys.length > 0 ? (
        <Banner tone="warn">
          <div className="flex items-start gap-2">
            <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
            <div className="min-w-0">
              <div>有 {data.orphan_keys.length} 个归属已失效的密钥（无主密钥）：</div>
              <ul className="mt-1 space-y-0.5">
                {data.orphan_keys.slice(0, 5).map((o) => (
                  <li key={o.owner} className="font-mono text-[11px]">
                    {o.owner} · {o.count} 枚{o.names.length ? "（" + o.names.join("、") + "）" : ""}
                  </li>
                ))}
              </ul>
              <div className="mt-1">
                它们在继续调用上游，但余额无人认领、也不能兑换。到「API 密钥」里把归属改回某个用户，或撤销它们。
              </div>
            </div>
          </div>
        </Banner>
      ) : null}

      {data && data.truncated ? (
        <Banner tone="warn">
          匹配结果超过服务端的单次扫描上限，列表可能不完整。请用搜索缩小范围。
        </Banner>
      ) : null}

      {!data && !error ? (
        <div className="rounded-xl border border-border/70 px-4 py-10 text-center text-xs text-muted-foreground">正在读取…</div>
      ) : items.length === 0 && !error ? (
        <Empty
          title={query ? "没有匹配的用户" : "还没有终端用户"}
          desc={query
            ? "换个关键词试试；搜索匹配邮箱与用户 ID。"
            : "用户在登录页自助注册，或由管理员在「服务设置」里开启注册后分发。"}
        />
      ) : items.length > 0 ? (
        <div className="overflow-x-auto rounded-xl border border-border/70">
          <table className="w-full min-w-[840px] text-sm">
            <thead className="bg-muted/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">邮箱</th>
                <th className="px-3 py-2 text-left font-medium">角色</th>
                <th className="px-3 py-2 text-left font-medium">状态</th>
                <th className="px-3 py-2 text-right font-medium">余额（积分）</th>
                <th className="px-3 py-2 text-left font-medium">密钥</th>
                <th className="px-3 py-2 text-left font-medium">最近登录</th>
                <th className="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((member) => (
                <tr key={member.id} className="border-t border-border/60 align-top">
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-1.5">
                      <UserRound className="size-3.5 shrink-0 text-muted-foreground" />
                      <span className="font-medium">{member.email || "—"}</span>
                      {!member.email_verified ? <ToneBadge tone="muted">未验证</ToneBadge> : null}
                    </div>
                    <div className="mt-1 font-mono text-[10px] text-muted-foreground" translate="no">{member.id}</div>
                  </td>
                  <td className="px-3 py-2.5">
                    {isAdmin ? (
                      <Select value={member.role} onValueChange={(v) => void setRole(member, v)}>
                        <SelectTrigger className="h-7 w-28 text-xs"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {ROLE_OPTIONS.map((o) => (
                            <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <ToneBadge tone={roleTone(member.role)}>{memberRoleLabel(member.role)}</ToneBadge>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    <ToneBadge tone={statusTone(member.status)}>{memberStatusLabel(member.status)}</ToneBadge>
                  </td>
                  <td className="px-3 py-2.5 text-right font-medium tabular-nums">
                    {fmtCredit(member.balance_credit)}
                  </td>
                  <td className="px-3 py-2.5 text-xs">
                    {member.key_count === 0
                      ? <span className="text-muted-foreground">无</span>
                      : <span title={member.key_names.join("、")}>{member.key_count} 枚</span>}
                  </td>
                  <td className="px-3 py-2.5 text-[11px] text-muted-foreground">
                    {member.last_login_at ? fmtCreditTime(member.last_login_at) : "从未登录"}
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex flex-wrap justify-end gap-1.5">
                      <Button
                        variant="outline" size="sm" className="h-7 px-2 text-xs"
                        disabled={detailBusy}
                        onClick={() => void openDetail(member)}
                      >详情</Button>
                      <Button
                        variant="outline" size="sm" className="h-7 px-2 text-xs"
                        disabled={!canTouchMoney}
                        title={!canTouchMoney
                          ? (tokenSession ? "调整余额只接受会话管理员，令牌登录不可用" : "需要管理员权限")
                          : undefined}
                        onClick={() => setBalanceTarget(member)}
                      >调整余额</Button>
                      <Button
                        variant="outline" size="sm" className="h-7 px-2 text-xs"
                        disabled={!isAdmin}
                        onClick={() => void setStatus(member, member.status === "disabled" ? "active" : "disabled")}
                      >{member.status === "disabled" ? "启用" : "禁用"}</Button>
                      <Button
                        variant="outline" size="sm"
                        className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                        disabled={!canTouchMoney}
                        title={!canTouchMoney
                          ? (tokenSession ? "删除用户只接受会话管理员，令牌登录不可用" : "需要管理员权限")
                          : undefined}
                        onClick={() => askDelete(member)}
                      >删除</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {/* 分页。只有一页时不摆控件 —— 一个永远禁用的「下一页」只会让人以为坏了。 */}
      {pages > 1 ? (
        <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
          <span>
            共 {fmtNum(data?.total ?? 0, 0)} 个用户 · 第 {page} / {pages} 页
          </span>
          <div className="flex gap-1.5">
            <Button variant="outline" size="sm" className="h-7 px-2 text-xs" disabled={page <= 1 || loading} onClick={() => setPage((p) => Math.max(1, p - 1))}>上一页</Button>
            <Button variant="outline" size="sm" className="h-7 px-2 text-xs" disabled={page >= pages || loading} onClick={() => setPage((p) => Math.min(pages, p + 1))}>下一页</Button>
          </div>
        </div>
      ) : null}

      <MemberDetailDialog detail={detail} onClose={() => setDetail(null)} />
      <BalanceDialog
        member={balanceTarget}
        onClose={() => setBalanceTarget(null)}
        onSaved={reloadFromFirstPage}
      />
      <ConfirmDialog request={confirm.request} onClose={confirm.close} onDone={() => void reloadFromFirstPage()} />
      <CreateMemberDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onSubmit={createMember}
      />
    </div>
  );
}

function StatBox({ label, value, hint, warn }: { label: string; value: string; hint?: string; warn?: boolean }) {
  return (
    <div className={warn ? "rounded-xl border border-amber-500/40 bg-amber-500/[0.06] p-3" : "rounded-xl border border-border/70 bg-card p-3"}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

/** 详情：视图 + 名下密钥 + 最近流水（能力矩阵第 2 项）。 */
function MemberDetailDialog({ detail, onClose }: { detail: RpMemberDetail | null; onClose: () => void }) {
  return (
    <Dialog open={Boolean(detail)} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>用户详情</DialogTitle>
          <DialogDescription className="break-all">{detail?.email}</DialogDescription>
        </DialogHeader>
        {detail ? (
          <div className="flex max-h-[60vh] flex-col gap-4 overflow-y-auto">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-3">
              <Fact label="用户 ID" value={detail.id} mono />
              <Fact label="角色" value={memberRoleLabel(detail.role)} />
              <Fact label="状态" value={memberStatusLabel(detail.status)} />
              <Fact label="邮箱验证" value={detail.email_verified ? "已验证" : "未验证"} />
              <Fact label="余额" value={fmtCredit(detail.balance_credit) + " 积分"} />
              <Fact label="创建时间" value={fmtCreditTime(detail.created_at)} />
              <Fact label="最近登录" value={detail.last_login_at ? fmtCreditTime(detail.last_login_at) : "从未登录"} />
            </dl>

            <section>
              <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-medium">
                <KeyRound className="size-3.5 text-muted-foreground" />名下密钥（{detail.keys.length}）
              </h3>
              {detail.keys.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">还没有密钥。到「API 密钥」里创建并把它归属到这个用户。</p>
              ) : (
                <ul className="divide-y divide-border/40 rounded-lg border border-border/70">
                  {detail.keys.map((k) => (
                    <li key={k.id} className="flex items-center justify-between gap-2 px-2.5 py-1.5 text-xs">
                      <span className="min-w-0 truncate font-medium">{k.name || k.id}</span>
                      <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{k.prefix || "—"}</span>
                      <ToneBadge tone={k.enabled ? "ok" : "muted"}>{k.enabled ? "启用" : "停用"}</ToneBadge>
                      <span className="shrink-0 text-[10px] text-muted-foreground">
                        {k.last_used_at ? fmtCreditTime(k.last_used_at) : "从未使用"}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-medium">
                <Coins className="size-3.5 text-muted-foreground" />最近流水（{detail.flows.length}）
              </h3>
              {detail.flows.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">还没有积分流水。</p>
              ) : (
                <div className="overflow-x-auto rounded-lg border border-border/70">
                  <table className="w-full min-w-[420px] text-xs">
                    <thead className="bg-muted/40 text-[11px] text-muted-foreground">
                      <tr>
                        <th className="px-2.5 py-1.5 text-left font-medium">时间</th>
                        <th className="px-2.5 py-1.5 text-left font-medium">类型</th>
                        <th className="px-2.5 py-1.5 text-right font-medium">变动（积分）</th>
                        <th className="px-2.5 py-1.5 text-left font-medium">说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.flows.map((f, i) => (
                        <tr key={String(f.ts) + "-" + i} className="border-t border-border/60">
                          <td className="px-2.5 py-1.5 text-muted-foreground">{fmtCreditTime(f.ts)}</td>
                          <td className="px-2.5 py-1.5">{flowKindLabel(f.kind)}</td>
                          <td className={f.milli >= 0 ? "px-2.5 py-1.5 text-right tabular-nums text-emerald-600 dark:text-emerald-400" : "px-2.5 py-1.5 text-right tabular-nums text-destructive"}>
                            {/*
                             * 用服务端算好的 `credit`，不自己做 `milli / 1000`。
                             * 浮点除法会引入 0.1+0.2 那类尾差，而且舍入口径一旦与服务端
                             * 不同，同一条流水在网页端和桌面端就会显示成两个数 ——
                             * 本项目已经因为浮点累加被坑过一次。
                             * 极老的流水可能没有 credit 字段，才退回整数换算。
                             */}
                            {(f.milli >= 0 ? "+" : "") + fmtCredit(f.credit ?? creditFromMilli(f.milli))}
                          </td>
                          <td className="px-2.5 py-1.5 text-muted-foreground">{f.note || f.actor || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-muted-foreground">{label}</dt>
      <dd className={mono ? "truncate font-mono text-[11px]" : "truncate"} translate={mono ? "no" : undefined}>{value}</dd>
    </div>
  );
}

/**
 * 调整余额（能力矩阵第 5 项）。
 *
 * 只做增量，没有「设为 N」：服务端就是 `delta_credit`，
 * 设值语义在并发下会吞掉别人的充值。所以这里的输入框是「+ / −」的增量，
 * 界面上也直接把「调整后余额」算给管理员看，避免他误以为在改一个绝对值。
 */
function BalanceDialog({ member, onClose, onSaved }: {
  member: RpMember | null;
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setAmount("");
    setReason("");
    setError("");
  }, [member?.id]);

  const parsed = useMemo(() => {
    const text = amount.trim();
    if (!text) return null;
    const value = Number(text);
    return Number.isFinite(value) ? value : null;
  }, [amount]);

  const preview = member && parsed !== null && parsed !== 0
    // 只是预览：真正落库的值由服务端按 Decimal 舍入（避免此处浮点误差影响结果）。
    ? fmtCredit(member.balance_credit + parsed)
    : null;

  async function submit() {
    if (!member) return;
    if (parsed === null || parsed === 0) { setError("请填写不为 0 的调整金额"); return; }
    setBusy(true);
    setError("");
    try {
      const result = await rp.adjustMemberBalance(member.id, parsed, reason.trim() || (parsed > 0 ? "管理员加余额" : "管理员扣余额"));
      toast.success("已调整 " + member.email + " 的余额", {
        description: "当前余额 " + fmtCredit(result.balance_credit) + " 积分",
      });
      onClose();
      await onSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={Boolean(member)} onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>调整余额</DialogTitle>
          <DialogDescription className="break-all">
            {member?.email} · 当前 {fmtCredit(member?.balance_credit)} 积分
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Field
            label="调整金额（积分）"
            hint="正数加、负数扣。这里是**增量**，不是把余额设成这个数。最小 0.001 积分。"
          >
            <Input
              type="number"
              step="0.001"
              value={amount}
              placeholder="例如 100 或 -50"
              onChange={(e) => setAmount(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
            />
          </Field>
          <div className="flex flex-wrap gap-1.5">
            {[10, 100, 1000, -10, -100].map((v) => (
              <Button
                key={v}
                type="button"
                variant="outline"
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => setAmount(String(v))}
              >{v > 0 ? "+" + v : String(v)}</Button>
            ))}
          </div>
          {preview ? (
            <p className="text-[11px] text-muted-foreground">调整后余额约 {preview} 积分</p>
          ) : null}
          <Field label="原因" hint="写进流水，方便日后对账。留空会按加/扣自动生成。">
            <Input value={reason} maxLength={120} onChange={(e) => setReason(e.target.value)} placeholder="例如 充值 100 积分" />
          </Field>
          {error ? <Banner tone="bad">{error}</Banner> : null}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" disabled={busy} onClick={onClose}>取消</Button>
          <Button size="sm" disabled={busy || parsed === null || parsed === 0} onClick={() => void submit()}>
            {busy ? "提交中…" : "确认调整"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * 分发账号对话框。
 *
 * 存在的理由很具体：未配置 SMTP 时所有人都注册不了，
 * 而服务端给出的提示原文就是「或者给您分发账号登录」。
 * 没有这个入口，那句提示就是一句空话 —— 管理员知道该怎么办，却没有地方能办。
 *
 * 与自助注册共用服务端的 `members.create()`（密码哈希、邮箱唯一性、索引维护
 * 只有一处实现），所以两条路建出来的账号不可能有差异。
 */
function CreateMemberDialog({ open, onClose, onSubmit }: {
  open: boolean;
  onClose: () => void;
  onSubmit: (email: string, password: string, role: string) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("user");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // 每次打开都清空，避免上一个人的邮箱留在框里被误发。
  useEffect(() => {
    if (open) {
      setEmail("");
      setPassword("");
      setRole("user");
      setError("");
    }
  }, [open]);

  /**
   * 本地先挡一道。
   *
   * 服务端有同样的校验，但本地挡是为了措辞一致（sync-spec 的错误文案表）：
   * 「密码至少 8 位」这句话两端一字不差。等一个网络往返再报错对填表体验也更差。
   */
  const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  const passwordOk = password.length >= 8;

  async function submit() {
    setError("");
    if (!emailOk) { setError("请输入正确的邮箱地址"); return; }
    if (!passwordOk) { setError("密码至少 8 位"); return; }
    setBusy(true);
    try {
      await onSubmit(email.trim(), password, role);
    } catch (cause) {
      // 服务端原话（「该邮箱已注册，请直接登录」等）比本地措辞准确，直接显示。
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>分发账号</DialogTitle>
          <DialogDescription>
            直接创建一个终端用户，把邮箱和密码发给对方即可登录。
            管理员分发的账号默认标记为已验证，不经过邮箱验证码 ——
            这正是「未配置 SMTP 时无法注册」的替代方案。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Field label="邮箱">
            <Input
              value={email}
              placeholder="someone@example.com"
              spellCheck={false}
              autoComplete="off"
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label="密码" hint="至少 8 位。请通过安全渠道转给对方，让他登录后自行修改。">
            <Input
              type="text"
              value={password}
              placeholder="至少 8 位"
              spellCheck={false}
              autoComplete="new-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <Field label="角色">
            <Select value={role} onValueChange={setRole}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {ROLE_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          {role === "admin" ? (
            <Banner tone="warn">
              终端用户里的「管理员」可以进入用户中心管理其他终端用户（含调整余额）。
              如果你要的只是「能登录控制台后台」，那是另一套东西 —— 见「后台账号」页签。
            </Banner>
          ) : null}
          {error ? <Banner tone="bad">{error}</Banner> : null}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" disabled={busy} onClick={onClose}>取消</Button>
          <Button size="sm" disabled={busy || !emailOk || !passwordOk} onClick={() => void submit()}>
            {busy ? "创建中…" : "创建账号"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
