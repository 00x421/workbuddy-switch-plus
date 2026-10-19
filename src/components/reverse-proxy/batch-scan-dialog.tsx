import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ExternalLink, QrCode } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { rp, RP_REGIONS, realmOfVariant, type RpBatchScanItem, type RpRealm } from "@/lib/reverse-proxy";
import { Banner, Field, ToneBadge } from "./rp-shared";

/**
 * 批量扫码登录。
 *
 * 存在的理由：一个人要加十几个号，逐个「打开弹窗 → 生成链接 → 切浏览器 →
 * 扫码 → 关窗」时，等待时间全花在切窗口上。这里是 N 张码并排，扫完一张
 * 自动轮到下一张，人只需要一直扫。
 *
 * 四条实现纪律，每条都对应一个会静默出错的坑：
 *
 *   · **不能循环调 /oauth/start**。那个接口不带 slot 时会 `cancel_owner()`，
 *     第二次调用就把第一张码干掉，循环 20 次只剩最后一张活着。必须走
 *     `/oauth/batch/start`，它给每张码分配独立 slot。
 *
 *   · **部分失败不回滚**。第 40 张拿不到链接不该让前 39 张作废 —— 用户可能
 *     已经扫了其中几张。失败格单独标红，其余照常轮询。
 *
 *   · **用 ref 存轮询定时器与代数**。对话框卸载后定时器还在跑，回调里 setState
 *     会报警告、更糟的是会对着已经关掉的流程继续轮询，把新会话的 flow 覆盖掉。
 *     `genRef` 每轮 +1，回调里先比对代数，过期直接退出。
 *
 *   · **关窗时统一取消未完成的 flow**。不取消的话服务端会一直留着这些会话
 *     直到 TTL 到期，占着同 owner 的 50 个名额，下次批量直接 429。
 */
type SlotState = "waiting" | "ready" | "done" | "failed" | "expired";

type Row = {
  slot: string;
  name: string;
  url?: string;
  flowId?: string;
  state: SlotState;
  error?: string;
};

const POLL_MS = 3000;
const MAX_COUNT = 50;

export function BatchScanDialog({ open, onOpenChange, onAdded }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: () => void;
}) {
  const [realm, setRealm] = useState<"cn" | "global">("cn");
  const [region, setRegion] = useState("HK");
  const [count, setCount] = useState(5);
  const [prefix, setPrefix] = useState("批量");
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [started, setStarted] = useState(false);

  const timerRef = useRef<number | null>(null);
  const genRef = useRef(0);
  const rowsRef = useRef<Row[]>([]);
  /** 已经在轮询中的 flow，避免同一个 flow 被排进多个 tick。 */
  const pollingRef = useRef<Set<string>>(new Set());
  /** 本次会话成功落盘的账号数，用来决定关窗时要不要通知外层刷新。 */
  const addedRef = useRef(0);

  useEffect(() => {
    rowsRef.current = rows;
  }, [rows]);

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    pollingRef.current.clear();
  }, []);

  const reset = useCallback((cancelFlows: boolean) => {
    genRef.current += 1;
    stopPolling();
    if (cancelFlows) {
      for (const row of rowsRef.current) {
        if (row.flowId && row.state !== "done") {
          void rp.oauthCancel(row.flowId).catch(() => undefined);
        }
      }
    }
    setRows([]);
    setStarted(false);
    setError("");
    setBusy(false);
  }, [stopPolling]);

  useEffect(() => {
    if (open) return;
    // 关窗：取消所有还没扫的 flow，并通知外层刷新账号列表。
    const added = addedRef.current;
    addedRef.current = 0;
    reset(true);
    setRealm("cn");
    setRegion("HK");
    setCount(5);
    setPrefix("批量");
    if (added > 0) onAdded();
  }, [open, reset, onAdded]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  function patch(slot: string, next: Partial<Row>) {
    setRows((current) => current.map((row) => (row.slot === slot ? { ...row, ...next } : row)));
  }

  function tick(generation: number) {
    if (generation !== genRef.current) return;
    const pending = rowsRef.current.filter(
      (row) => row.flowId && row.state === "ready" && !pollingRef.current.has(row.flowId));
    if (!pending.length) return;
    for (const row of pending) {
      const flowId = row.flowId as string;
      pollingRef.current.add(flowId);
      void rp.oauthPoll(flowId)
        .then((result) => {
          if (generation !== genRef.current) return;
          if (result.status === "success") {
            addedRef.current += 1;
            patch(row.slot, { state: "done", error: undefined });
            toast.success(`${row.name} 登录成功`);
            return;
          }
          if (result.status === "expired") {
            patch(row.slot, { state: "expired", error: "登录链接已过期" });
          }
        })
        .catch((cause) => {
          if (generation !== genRef.current) return;
          // 单格轮询失败不等于这一格废了：上游 5xx 时会连续失败几次。
          // 只在第一次把错误写出来，状态仍是 ready，下一轮继续试。
          const text = cause instanceof Error ? cause.message : String(cause);
          if (!rowsRef.current.find((item) => item.slot === row.slot)?.error) {
            patch(row.slot, { error: text });
          }
        })
        .finally(() => {
          pollingRef.current.delete(flowId);
        });
    }
  }

  async function start() {
    setError("");
    reset(true);
    const generation = genRef.current;
    setBusy(true);
    try {
      const payload = await rp.batchScan(
        count,
        realmOfVariant(realm) as RpRealm,
        realm === "global" ? region : undefined,
        prefix.trim() || undefined,
      );
      if (generation !== genRef.current) {
        for (const item of payload.items) {
          if (item.id) void rp.oauthCancel(item.id).catch(() => undefined);
        }
        return;
      }
      const next: Row[] = payload.items.map((item: RpBatchScanItem, index: number) => ({
        slot: item.slot,
        name: `${prefix.trim() || "批量"}${index + 1}`,
        url: item.url,
        flowId: item.id,
        state: item.status === "ready" ? "ready" : "failed",
        error: item.error,
      }));
      setRows(next);
      rowsRef.current = next;
      setStarted(true);
      if (payload.ready > 0) {
        toast.success(`已生成 ${payload.ready} 张登录码，${Math.round(payload.ttl / 60)} 分钟内有效`);
        timerRef.current = window.setInterval(() => tick(generation), POLL_MS);
      } else {
        setError("一张都没生成出来，稍后重试或减少数量");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  const doneCount = rows.filter((row) => row.state === "done").length;
  const readyCount = rows.filter((row) => row.state === "ready").length;

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <QrCode className="size-4" />批量扫码登录
          </DialogTitle>
          <DialogDescription>
            一次生成多张登录码，用同一台手机连续扫完即可。每张码独立，扫完一张另一张不受影响。
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3">
          <Field label="数量">
            <Input
              type="number"
              min={1}
              max={MAX_COUNT}
              value={count}
              disabled={busy || started}
              onChange={(e) => setCount(Math.min(MAX_COUNT, Math.max(1, Math.trunc(Number(e.target.value) || 1))))}
            />
          </Field>
          <Field label="备注前缀">
            <Input
              value={prefix}
              maxLength={40}
              disabled={busy || started}
              placeholder="批量"
              onChange={(e) => setPrefix(e.target.value)}
            />
          </Field>
        </div>

        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium">账号版本</span>
          <div className="flex gap-2">
            {([["cn", "国内版 CodeBuddy"], ["global", "国际版 WorkBuddy AI"]] as const).map(([value, label]) => (
              <Button
                key={value}
                type="button"
                variant={realm === value ? "default" : "outline"}
                size="sm"
                disabled={busy || started}
                onClick={() => { setRealm(value); setError(""); }}
              >
                {label}
              </Button>
            ))}
          </div>
          {realm === "global" ? (
            <div className="flex flex-col gap-1">
              <span className="text-xs font-medium">
                地区 <span className="text-muted-foreground">（首次登录必须选，否则聊天报 14017）</span>
              </span>
              <div className="flex flex-wrap gap-1.5">
                {RP_REGIONS.map((item) => (
                  <Button
                    key={item.code}
                    type="button"
                    variant={region === item.code ? "default" : "outline"}
                    size="sm"
                    className="h-7 px-2 text-xs"
                    disabled={busy || started}
                    onClick={() => setRegion(item.code)}
                  >
                    {item.label}
                  </Button>
                ))}
              </div>
            </div>
          ) : null}
        </div>

        {started ? (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <ToneBadge tone={doneCount ? "ok" : "muted"}>已完成 {doneCount}</ToneBadge>
              <ToneBadge tone={readyCount ? "warn" : "muted"}>等待扫码 {readyCount}</ToneBadge>
              <span>关掉这个窗口会取消所有还没扫的登录码。</span>
            </div>
            <div className="max-h-[340px] overflow-y-auto rounded-lg border border-border/70">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-muted/60 text-muted-foreground backdrop-blur">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">#</th>
                    <th className="px-3 py-2 text-left font-medium">备注</th>
                    <th className="px-3 py-2 text-left font-medium">状态</th>
                    <th className="px-3 py-2 text-left font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <tr key={row.slot} className="border-t border-border/50">
                      <td className="px-3 py-1.5 tabular-nums text-muted-foreground">{index + 1}</td>
                      <td className="px-3 py-1.5">{row.name}</td>
                      <td className="px-3 py-1.5">
                        {row.state === "done" ? <ToneBadge tone="ok">已登录</ToneBadge>
                          : row.state === "failed" ? <ToneBadge tone="bad">未生成</ToneBadge>
                            : row.state === "expired" ? <ToneBadge tone="bad">已过期</ToneBadge>
                              : <ToneBadge tone="warn">等待扫码</ToneBadge>}
                        {row.error ? <span className="ml-2 text-[11px] text-muted-foreground">{row.error}</span> : null}
                      </td>
                      <td className="px-3 py-1.5">
                        {row.url && row.state === "ready" ? (
                          <span className="flex items-center gap-1">
                            <Button asChild size="sm" variant="outline" className="h-6 px-2 text-[11px]">
                              <a href={row.url} target="_blank" rel="noopener noreferrer">
                                <ExternalLink className="size-3" />登录页
                              </a>
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              className="h-6 px-2 text-[11px]"
                              onClick={() => {
                                void navigator.clipboard.writeText(row.url as string)
                                  .then(() => toast.success("已复制"))
                                  .catch(() => toast.error("无法自动复制"));
                              }}
                            >
                              复制
                            </Button>
                          </span>
                        ) : <span className="text-muted-foreground">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <Banner tone="muted">
            生成后这一页会列出所有登录码。手机扫完一张直接扫下一张，不需要回来点任何按钮。
          </Banner>
        )}

        {error ? <Banner tone="bad">{error}</Banner> : null}

        <div className="flex justify-end gap-2">
          {started ? (
            <Button type="button" variant="outline" size="sm" onClick={() => reset(true)} disabled={busy}>
              重新生成
            </Button>
          ) : (
            <Button type="button" size="sm" onClick={start} disabled={busy}>
              {busy ? "正在生成…" : `生成 ${count} 张登录码`}
            </Button>
          )}
          <Button type="button" size="sm" variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            关闭
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
