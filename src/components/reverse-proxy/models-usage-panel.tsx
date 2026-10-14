import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowRight, Loader2, Plus, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Banner, Empty, PanelHeader, StatCard, ToneBadge, fmtNum } from "@/components/reverse-proxy/rp-shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { rp, type RpModelAliases, type RpUsageStats } from "@/lib/reverse-proxy";

/**
 * 模型与用量。
 *
 * 这两块后端早就在跑、但**两个界面都没有入口**：
 *   · 用量统计：每个请求都 `usage.record(...)` 落桶，可从没地方看
 *   · 模型别名：`pool.py:1705` 在真实请求链路上解析别名，却没地方配
 * 所以这里不是「补个按钮」，是第一次让人看得见、改得动它们。
 *
 * 分成两块而不是两个页签：别名是「配置」、用量是「看数据」，
 * 但都跟"模型"这一个话题有关，放一起比塞进两个不同的组更好找。
 */
export function ModelsUsagePanel() {
  const [aliases, setAliases] = useState<RpModelAliases | null>(null);
  const [stats, setStats] = useState<RpUsageStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  /** 别名编辑：本地先改，保存时整表提交（后端是整表替换语义）。 */
  const [draft, setDraft] = useState<{ name: string; target: string }[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // 两个请求互不依赖，并发拿。任意一个失败都要能看到原因，
      // 所以用 allSettled 而不是 all —— all 会在第一个失败时丢掉另一个的结果。
      const [aliasRes, statsRes] = await Promise.allSettled([rp.modelAliases(), rp.usageStats(30, 24)]);
      if (aliasRes.status === "fulfilled") {
        setAliases(aliasRes.value);
        setDraft(Object.entries(aliasRes.value.aliases).map(([name, target]) => ({ name, target })));
      }
      if (statsRes.status === "fulfilled") setStats(statsRes.value);
      const failed = [aliasRes, statsRes].filter((r) => r.status === "rejected");
      if (failed.length === 2) {
        const first = failed[0] as PromiseRejectedResult;
        setError(first.reason instanceof Error ? first.reason.message : String(first.reason));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const known = useMemo(() => new Set(aliases?.known_models ?? []), [aliases]);

  /** 本地校验：哪些行的目标模型不在已知清单里（多半是拼错）。 */
  const unknownTargets = useMemo(() => {
    if (!aliases || aliases.known_models.length === 0) return new Set<string>();
    return new Set(
      draft
        .filter((row) => row.name.trim() && row.target.trim() && !known.has(row.target.trim()))
        .map((row) => row.name.trim()),
    );
  }, [draft, known, aliases]);

  async function save() {
    setSaving(true);
    setError("");
    try {
      const table: Record<string, string> = {};
      for (const row of draft) {
        const name = row.name.trim();
        const target = row.target.trim();
        // 与后端一致的丢弃规则：两边都空就跳过；只填一半是错误。
        if (!name && !target) continue;
        if (!name || !target) throw new Error("别名与目标模型都必须填写（不能只填一半）");
        // `a → a` 是无意义的自我指向，后端也会丢掉它，前端直接跳过免得来回。
        if (name === target) continue;
        table[name] = target;
      }
      if (Object.keys(table).length > 200) throw new Error("别名最多 200 条");
      await rp.saveModelAliases(table);
      toast.success("模型别名已保存");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex flex-col gap-5">
        <PanelHeader title="模型与用量" desc="别名映射与长期用量分桶。" />
        <div className="rounded-xl border border-border/70 px-4 py-10 text-center text-xs text-muted-foreground">
          正在读取…
        </div>
      </div>
    );
  }

  const totals = stats?.totals.daily;

  return (
    <div className="flex flex-col gap-5">
      <PanelHeader
        title="模型与用量"
        desc="别名映射决定客户端传的模型名实际跑到哪个模型；用量按天/按小时分桶，存在服务端，重启不丢。"
      />

      {error ? <Banner tone="bad">{error}</Banner> : null}

      {/* ── 用量 ── */}
      {totals ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label={`调用（最近 ${stats?.window.days ?? 30} 天）`} value={fmtNum(totals.calls, 0)} />
            <StatCard
              label="失败率"
              value={totals.calls ? `${(totals.failure_rate * 100).toFixed(1)}%` : "—"}
              hint={totals.calls ? `失败 ${fmtNum(totals.failed, 0)} 次` : "窗口内没有调用"}
            />
            <StatCard label="Token 总量" value={fmtNum(totals.tokens, 0)} hint={`均 ${fmtNum(totals.tokens_avg, 1)} / 次`} />
            <StatCard label="积分消耗" value={fmtNum(totals.credit, 2)} />
          </div>

          {/* 按天明细。取最近 14 天，再多屏幕放不下、也没人逐行看。 */}
          {(stats?.daily ?? []).length > 0 ? (
            <section className="rounded-xl border border-border/70 bg-card">
              <div className="border-b border-border/60 px-4 py-2.5">
                <span className="text-xs font-medium">按天（最近 14 天）</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[720px] text-xs">
                  <thead>
                    <tr className="text-left text-muted-foreground">
                      <th className="px-4 py-2 font-medium">日期</th>
                      <th className="px-4 py-2 font-medium">调用</th>
                      <th className="px-4 py-2 font-medium">失败</th>
                      <th className="px-4 py-2 font-medium">Token</th>
                      <th className="px-4 py-2 font-medium">积分</th>
                      <th className="px-4 py-2 font-medium">平均耗时</th>
                      <th className="px-4 py-2 font-medium">首字延迟</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(stats?.daily ?? []).slice(-14).reverse().map((row) => (
                      <tr key={row.date} className="border-t border-border/50">
                        <td className="px-4 py-2 font-mono">{row.date}</td>
                        <td className="px-4 py-2 tabular-nums">{fmtNum(row.calls, 0)}</td>
                        <td className="px-4 py-2 tabular-nums">
                          {row.failed > 0 ? <span className="text-destructive">{fmtNum(row.failed, 0)}</span> : "0"}
                        </td>
                        <td className="px-4 py-2 tabular-nums">{fmtNum(row.tokens, 0)}</td>
                        <td className="px-4 py-2 tabular-nums">{fmtNum(row.credit, 2)}</td>
                        <td className="px-4 py-2 tabular-nums">{fmtNum(row.duration_ms_avg, 0)} ms</td>
                        <td className="px-4 py-2 tabular-nums">{fmtNum(row.first_token_ms_avg, 0)} ms</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="border-t border-border/60 px-4 py-2 text-[11px] leading-4 text-muted-foreground">
                首字延迟只统计<strong className="font-medium">成功</strong>的请求 —— 失败请求根本没有首字，算进去会把均值拉低，
                看着像「变快了」。
              </p>
            </section>
          ) : (
            <Empty title="还没有用量数据" desc="服务处理请求后才会开始分桶计数。" />
          )}
        </>
      ) : null}

      {/* ── 模型别名 ── */}
      <section className="rounded-xl border border-border/70 bg-card">
        <div className="flex items-center justify-between border-b border-border/60 px-4 py-2.5">
          <span className="text-xs font-medium">模型别名</span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setDraft((rows) => [...rows, { name: "", target: "" }])}
          >
            <Plus className="size-3.5" />新增一行
          </Button>
        </div>

        <div className="flex flex-col gap-3 px-4 py-4">
          <p className="text-[11px] leading-4 text-muted-foreground">
            客户端传别名时，服务端会在<strong className="font-medium">真实请求链路</strong>上把它换成目标模型。
            支持链式（a → b → c）但会防环；`a → a` 这种自我指向会被丢弃。
            {aliases?.default_model ? (
              <> 当前默认模型：<code className="font-mono">{aliases.default_model}</code>。</>
            ) : null}
          </p>

          {draft.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border/70 px-4 py-6 text-center text-xs text-muted-foreground">
              还没有别名。点「新增一行」开始。
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {draft.map((row, index) => (
                <div key={index} className="flex items-center gap-2">
                  <Input
                    className="flex-1"
                    value={row.name}
                    placeholder="客户端传的名字，如 gpt-4o"
                    maxLength={64}
                    onChange={(e) => setDraft((rows) =>
                      rows.map((r, i) => (i === index ? { ...r, name: e.target.value } : r)))}
                  />
                  <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" />
                  <Input
                    className="flex-1"
                    value={row.target}
                    placeholder="实际模型，如 glm-5.2"
                    maxLength={64}
                    onChange={(e) => setDraft((rows) =>
                      rows.map((r, i) => (i === index ? { ...r, target: e.target.value } : r)))}
                  />
                  {/* 目标不在已知清单里时**当下就标出来** ——
                      拼错的别名会让请求打到不存在的模型上，而那种失败
                      在日志里只表现为一个 4xx，很难追回配置这一步。 */}
                  {unknownTargets.has(row.name.trim()) ? (
                    <ToneBadge tone="warn">未知模型</ToneBadge>
                  ) : null}
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setDraft((rows) => rows.filter((_, i) => i !== index))}
                    title="删除这一行"
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              ))}
            </div>
          )}

          {aliases && aliases.known_models.length > 0 ? (
            <details className="text-[11px]">
              <summary className="cursor-pointer text-muted-foreground">
                已知模型（{aliases.known_models.length} 个，点开对照）
              </summary>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {aliases.known_models.map((model) => (
                  <code key={model} className="rounded border border-border/70 px-1.5 py-0.5 font-mono">
                    {model}
                  </code>
                ))}
              </div>
            </details>
          ) : (
            <p className="text-[11px] leading-4 text-muted-foreground">
              本机暂时拿不到已知模型清单，因此不做「未知模型」提示 ——
              假警报比不提示更糟。
            </p>
          )}
        </div>
      </section>

      <div className="flex justify-end">
        <Button size="sm" disabled={saving || !aliases} onClick={() => void save()}>
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
          {saving ? "保存中…" : "保存别名"}
        </Button>
      </div>
    </div>
  );
}
