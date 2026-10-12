import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Activity, LayoutGrid, RefreshCw, Rows3 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  LOG_GROUP_MULTI, logGroupCell, logGroupOf, logGroupOptions, logGroupSections, logRowMatchesGroup,
} from "@/lib/log-group";
import {
  OUTCOME_LABELS, rp, type RpGroup, type RpKey, type RpRequestLog,
} from "@/lib/reverse-proxy";
import { Banner, Empty, PanelHeader, StatCard, ToneBadge, fmtNum, fmtTime } from "./rp-shared";

const OUTCOMES = [
  ["all", "全部结果"],
  ["success", "成功"],
  ["upstream_error", "上游报错"],
  ["no_account", "无可用账号"],
  ["rejected", "被规则拒绝"],
] as const;

/**
 * 请求日志：每次 /v1 调用用了哪个账号、什么模型、扣了多少。
 *
 * 这是排查「某个号是不是被过度使用」「积分花到哪去了」最直接的一页。
 * 数据是服务端最近 300 条，会落盘（`request-log.jsonl`），重启不清零——
 * 排查问题常常是「重启之后想看看上次崩之前发生了什么」。
 *
 * 自动刷新：进页面时拉一次，之后每 5 秒拉一次（切走 / 页面不可见时停），
 * 所以服务端一有新请求，这里几乎立刻能看到，不用手点刷新。
 */
const AUTO_REFRESH_MS = 5000;

/**
 * @param keys   `overview.keys`：日志行只有 `key_id`，分组要经它查 `groups[]`。
 * @param groups `overview.groups`：分组 id → 组名。
 * 两者缺失时（概览还没加载完）不报错，只是分组列会显示成「密钥已删除」，
 * 与「查不到这把密钥」同义 —— 见 log-group.ts 的 ③。
 */
export function RequestLogPanel({ keys, groups }: { keys?: RpKey[]; groups?: RpGroup[] }) {
  const [rows, setRows] = useState<RpRequestLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [account, setAccount] = useState("all");
  const [model, setModel] = useState("all");
  const [outcome, setOutcome] = useState<string>("all");
  const [group, setGroup] = useState("all");
  /** 平铺 / 分组。与网页端 app.js 的 `logView` 同名同义。 */
  const [view, setView] = useState<"flat" | "grouped">("flat");
  const [auto, setAuto] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number>(0);

  // silent=true 用于后台自动刷新：不显示 loading、不清空已有内容，
  // 否则每 5 秒闪一次骨架屏，反而看不清。
  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      setRows(await rp.requestLogs(300));
      setError("");
      setUpdatedAt(Date.now());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // 自动刷新。页面不可见时跳过，避免后台白白打请求。
  useEffect(() => {
    if (!auto) return;
    const id = window.setInterval(() => {
      if (!document.hidden) void load(true);
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [auto, load]);

  const accounts = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of rows) {
      if (row.account_id && row.account_name) seen.set(row.account_id, row.account_name);
    }
    return [...seen.entries()];
  }, [rows]);

  const models = useMemo(() => {
    const seen = new Set<string>();
    for (const row of rows) if (row.model) seen.add(row.model);
    return [...seen].sort();
  }, [rows]);

  const filtered = useMemo(() => rows.filter((row) => {
    if (account !== "all" && row.account_id !== account) return false;
    if (model !== "all" && row.model !== model) return false;
    if (outcome !== "all" && row.outcome !== outcome) return false;
    // 分组与其它三个条件是 AND 叠加（与网页端 logRowsFiltered 一致）。
    if (!logRowMatchesGroup(row, group, keys, groups)) return false;
    return true;
  }), [rows, account, model, outcome, group, keys, groups]);

  /** 分组筛选下拉：只列当前日志里真正出现过的分组（选了没出现过的会得到空表）。 */
  const groupOptions = useMemo(
    () => logGroupOptions(rows, keys, groups),
    [rows, keys, groups],
  );

  /** 分节结果。组间按「该组最新一条」降序，组内时间倒序。 */
  const sections = useMemo(
    () => logGroupSections(filtered, keys, groups),
    [filtered, keys, groups],
  );

  const totals = useMemo(() => {
    const ok = filtered.filter((r) => r.ok).length;
    return {
      count: filtered.length,
      ok,
      failed: filtered.length - ok,
      tokens: filtered.reduce((s, r) => s + (r.tokens || 0), 0),
      credit: filtered.reduce((s, r) => s + (r.credit || 0), 0),
    };
  }, [filtered]);

  /** 每个账号在筛选范围内的用量——「哪个号被用得多」一眼可见。 */
  const byAccount = useMemo(() => {
    const map = new Map<string, { name: string; count: number; tokens: number; credit: number }>();
    for (const row of filtered) {
      if (!row.account_id) continue;
      const item = map.get(row.account_id) ?? { name: row.account_name ?? row.account_id, count: 0, tokens: 0, credit: 0 };
      item.count += 1;
      item.tokens += row.tokens || 0;
      item.credit += row.credit || 0;
      map.set(row.account_id, item);
    }
    return [...map.values()].sort((a, b) => b.count - a.count);
  }, [filtered]);

  return (
    <div className="flex flex-col gap-5">
      <PanelHeader
        title="请求日志"
        desc="每次 /v1 调用的去向：用了哪个账号、什么模型、扣了多少积分。"
        action={(
          <div className="flex items-center gap-2">
            <Button
              variant={auto ? "default" : "outline"}
              size="sm"
              onClick={() => setAuto((v) => !v)}
              title={auto ? `每 ${AUTO_REFRESH_MS / 1000} 秒自动刷新，点击暂停` : "已暂停自动刷新，点击恢复"}
            >
              <Activity className="size-3.5" />{auto ? "实时" : "已暂停"}
            </Button>
            <Button variant="outline" size="sm" disabled={loading} onClick={() => void load().catch(() => toast.error("刷新失败"))}>
              <RefreshCw className="size-3.5" />刷新
            </Button>
          </div>
        )}
      />

      <Banner tone="muted">
        最近 <strong>300</strong> 条会<b>写入本地文件</b>（
        <code className="font-mono text-[11px]">request-log.jsonl</code>，与 state.json 同目录），
        服务重启后仍在；不塞进 state.json 是因为日志是高频追加写，
        混进去会导致每记一条就重写整份状态文件。
        {updatedAt ? ` 最后更新 ${new Date(updatedAt).toLocaleTimeString("zh-CN")}。` : ""}
      </Banner>

      {error ? <Banner tone="bad">{error}</Banner> : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="筛选后请求" value={fmtNum(totals.count, 0)} hint={`成功 ${totals.ok} · 失败 ${totals.failed}`} />
        <StatCard label="消耗 Token" value={fmtNum(totals.tokens, 0)} hint="按上游返回的 usage 累计" />
        <StatCard label="消耗积分" value={fmtNum(totals.credit)} hint="上游真实扣费口径" accent />
        <StatCard label="参与账号" value={fmtNum(byAccount.length, 0)} hint="这段时间里实际被调到的账号数" />
      </div>

      {byAccount.length > 0 ? (
        <section className="rounded-xl border border-border/70 bg-card p-4">
          <h3 className="text-xs font-medium text-foreground/90">各账号用量（当前筛选范围内）</h3>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            想知道有没有哪个号被「用得太狠」，看这一栏。
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {byAccount.map((item) => (
              <div key={item.name} className="min-w-0 rounded-lg border border-border/70 bg-muted/40 px-3 py-2">
                <div className="truncate text-xs font-medium">{item.name}</div>
                <div className="mt-0.5 text-[11px] tabular-nums text-muted-foreground">
                  {item.count} 次 · {fmtNum(item.tokens, 0)} token · {fmtNum(item.credit)} 积分
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Select value={account} onValueChange={setAccount}>
          <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部账号</SelectItem>
            {accounts.map(([id, name]) => <SelectItem key={id} value={id}>{name}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={model} onValueChange={setModel}>
          <SelectTrigger className="h-8 w-56 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部模型</SelectItem>
            {models.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={outcome} onValueChange={setOutcome}>
          <SelectTrigger className="h-8 w-36 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            {OUTCOMES.map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={group} onValueChange={setGroup}>
          <SelectTrigger className="h-8 w-40 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部分组</SelectItem>
            {groupOptions.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
          </SelectContent>
        </Select>
        {account !== "all" || model !== "all" || outcome !== "all" || group !== "all" ? (
          <Button
            variant="ghost" size="sm"
            // 清除筛选要一并清掉分组，否则用户点了「清除」界面还留着分组条件，会以为没生效。
            onClick={() => { setAccount("all"); setModel("all"); setOutcome("all"); setGroup("all"); }}
          >清除筛选</Button>
        ) : null}
        {/* 平铺 / 分组：与网页端的 seg 按钮组同义（aria-pressed 也保留）。 */}
        <div className="ml-auto flex items-center gap-2">
          <div className="flex overflow-hidden rounded-lg border border-border/70">
            <button
              type="button"
              aria-pressed={view === "flat"}
              onClick={() => setView("flat")}
              className={`flex items-center gap-1 px-2.5 py-1 text-xs transition-colors ${
                view === "flat" ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-muted/60"
              }`}
            >
              <Rows3 className="size-3.5" />平铺
            </button>
            <button
              type="button"
              aria-pressed={view === "grouped"}
              onClick={() => setView("grouped")}
              className={`flex items-center gap-1 border-l border-border/70 px-2.5 py-1 text-xs transition-colors ${
                view === "grouped" ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-muted/60"
              }`}
            >
              <LayoutGrid className="size-3.5" />分组
            </button>
          </div>
          <span className="text-xs text-muted-foreground">共 {filtered.length} 条</span>
        </div>
      </div>

      {filtered.length === 0 ? (
        <Empty
          title={rows.length === 0 ? "还没有请求记录" : "没有符合筛选条件的记录"}
          desc={rows.length === 0 ? "客户端发起第一次 /v1 调用后，这里会显示它走了哪个账号。" : "换个筛选条件试试。"}
        />
      ) : view === "grouped" ? (
        /* 分组视图：一节一个分组，节间按「该节最新一条」降序（见 logGroupSections）。
           每节自带表头，因为各节的列语义相同但内容不同 —— 共用一个表头会让
           滚动到下半屏时看不到列名。节内的 max-h 由外层统一控制。 */
        <div className="flex max-h-[32rem] flex-col gap-4 overflow-auto">
          {sections.map((s) => (
            <section key={s.bucket} className="rounded-xl border border-border/70">
              <header className="flex flex-wrap items-baseline gap-2 border-b border-border/50 px-3 py-2">
                <h3 className="text-xs font-medium">{s.label}</h3>
                <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground">
                  {s.rows.length}
                </span>
                <span className="text-[11px] tabular-nums text-muted-foreground">
                  {fmtNum(s.tokens, 0)} tokens · {fmtNum(s.credit)} 积分
                </span>
                {s.bucket === LOG_GROUP_MULTI ? (
                  <span className="text-[11px] text-muted-foreground">
                    · 绑了多个分组的密钥，自成一类（不重复计入各分组）
                  </span>
                ) : null}
              </header>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[860px] text-xs">
                  <LogTableHead />
                  <tbody>{s.rows.map((row, i) => <LogRow key={i} row={row} keys={keys} groups={groups} />)}</tbody>
                </table>
              </div>
            </section>
          ))}
        </div>
      ) : (
        <div className="max-h-[32rem] overflow-auto rounded-xl border border-border/70">
          <table className="w-full min-w-[860px] text-xs">
            <thead className="sticky top-0 bg-muted/60 text-muted-foreground backdrop-blur">
              <LogTableHeadRow />
            </thead>
            <tbody>
              {filtered.map((row, i) => <LogRow key={i} row={row} keys={keys} groups={groups} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** 表头（分组视图用：自带 thead）。 */
function LogTableHead() {
  return (
    <thead className="bg-muted/40 text-muted-foreground">
      <LogTableHeadRow />
    </thead>
  );
}

/**
 * 表头行。分组列紧跟在「账号」之后 —— 与网页端 `app.js:692` 的列序一致。
 * 分组是「这次调用走了谁」的补充：账号回答「用了哪个上游账号」，
 * 分组回答「调用方那把密钥被允许走哪些账号」，两个问题挨着看才顺。
 */
function LogTableHeadRow() {
  return (
    <tr>
      <th className="px-3 py-2 text-left font-medium">时间</th>
      <th className="px-3 py-2 text-left font-medium">账号</th>
      <th className="px-3 py-2 text-left font-medium">分组</th>
      <th className="px-3 py-2 text-left font-medium">模型</th>
      <th className="px-3 py-2 text-left font-medium">结果</th>
      <th className="px-3 py-2 text-right font-medium">Token</th>
      <th className="px-3 py-2 text-right font-medium">积分</th>
      <th className="px-3 py-2 text-right font-medium">耗时</th>
    </tr>
  );
}

/**
 * 一行日志。平铺与分组视图共用，保证两种视图下同一行长得完全一样。
 */
function LogRow({ row, keys, groups }: {
  row: RpRequestLog;
  keys?: RpKey[];
  groups?: RpGroup[];
}) {
  const state = OUTCOME_LABELS[row.outcome] ?? { label: row.outcome, tone: "muted" as const };
  const g = logGroupOf(row, keys, groups);
  // 单元格内容由 logGroupCell 决定（可单测），JSX 只负责套样式。
  const cell = logGroupCell(g);
  return (
    <tr className="border-t border-border/50">
      <td className="px-3 py-1.5 font-mono text-[10px] text-muted-foreground">{fmtTime(row.time)}</td>
      <td className="px-3 py-1.5">
        {row.account_name ? (
          <span className="flex items-center gap-1.5">
            <span className="truncate font-medium">{row.account_name}</span>
            {row.source === "test" ? <ToneBadge tone="muted">测试</ToneBadge> : null}
          </span>
        ) : <span className="text-muted-foreground">—（未选中账号）</span>}
      </td>
      <td className="px-3 py-1.5">
        {/* 与网页端 logGroupCell 同口径：
            有真实分组 -> 显示全部组名，多分组额外标出组数；
            特殊桶 -> 用弱化的 muted 文字，与真实分组在视觉上分开。 */}
        {cell.tone === "group" ? (
          <span className="truncate" title={g.ids.join("、")}>
            {cell.text}
            {cell.badge ? (
              <span className="ml-1 text-[10px] text-muted-foreground">{cell.badge}</span>
            ) : null}
          </span>
        ) : (
          <span className="text-[11px] text-muted-foreground">{cell.text}</span>
        )}
      </td>
      <td className="px-3 py-1.5 font-mono text-[11px]">{row.model || "—"}</td>
      <td className="px-3 py-1.5">
        <ToneBadge tone={state.tone}>{state.label}</ToneBadge>
        <span className="ml-1.5 font-mono text-[10px] text-muted-foreground">HTTP {row.status}</span>
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums">{row.tokens ? fmtNum(row.tokens, 0) : "—"}</td>
      <td className="px-3 py-1.5 text-right tabular-nums">{row.credit ? fmtNum(row.credit) : "—"}</td>
      <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground">{row.duration_ms} ms</td>
    </tr>
  );
}
