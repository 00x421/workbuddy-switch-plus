import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { rp, type RpAccount } from "@/lib/reverse-proxy";
import { Banner, Field } from "./rp-shared";

/**
 * 单账号的流量整形。
 *
 * 三个字段的意义在**事前防封**：封号的直接诱因几乎都是单账号短时间被打太多。
 * 池级的 spread 是全域一刀切，表达不了「新号先养着、稳的号多跑点」。
 *
 * 两条容易做错的约定：
 *
 * 1. 缺省值不是 0。权重缺省是 1（等权），而 0 是「不参与自动轮转」这个
 *    独立语义。把 undefined 当 0 会让刚加的号从轮转里消失，而且没有任何报错。
 * 2. 三项都显式提交。服务端把「键缺失」读作「不动这一项」，但界面上三个
 *    输入框都摆着——用户看到什么就该存成什么，否则会出现「我明明清成 0 了，
 *    保存后还是 5」。
 */
export function AccountLimitsDialog({ account, defaults, onClose, onSaved }: {
  account: RpAccount | null;
  /** 池级默认值，用于占位提示（新号会拿到这些值）。 */
  defaults: { weight: number; max_concurrency: number; min_interval: number };
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const [weight, setWeight] = useState("1");
  const [concurrency, setConcurrency] = useState("0");
  const [interval, setInterval] = useState("0");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // 每次换账号都要重新灌值 —— 对话框是复用的同一个实例，
  // 不重灌就会把上一个号的数值带过来，一保存就把两个号设成同一个值。
  useEffect(() => {
    if (!account) return;
    setWeight(String(account.weight ?? defaults.weight));
    setConcurrency(String(account.max_concurrency ?? defaults.max_concurrency));
    setInterval(String(account.min_interval ?? defaults.min_interval));
    setError("");
  }, [account, defaults.weight, defaults.max_concurrency, defaults.min_interval]);

  function submit() {
    if (!account) return;
    // 前端先挡一遍空值和非法值：服务端也会挡，但来回一趟才报错的体验很差，
    // 而且「权重填了空」这种情况 400 的文案不够直白。
    const w = Number(weight);
    const c = Number(concurrency);
    const gap = Number(interval);
    if (!Number.isFinite(w) || w < 0) { setError("权重需为不小于 0 的数字"); return; }
    if (!Number.isFinite(c) || c < 0) { setError("并发上限需为不小于 0 的整数"); return; }
    if (!Number.isFinite(gap) || gap < 0) { setError("最小间隔需为不小于 0 的数字"); return; }
    setBusy(true);
    setError("");
    void (async () => {
      try {
        await rp.setAccountLimits(account.id, {
          weight: w, max_concurrency: Math.trunc(c), min_interval: gap,
        });
        toast.success("限速已保存");
        await onSaved();
        onClose();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(false);
      }
    })();
  }

  const inflight = Number(account?.inflight ?? 0);

  return (
    <Dialog open={Boolean(account)} onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>账号限速 · {account?.name}</DialogTitle>
          <DialogDescription>
            这三项是事前预防：封号的直接诱因几乎都是单账号短时间被打太多。
            留空或填 0 表示不限制。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Field label="权重">
            <Input
              type="number" min={0} max={100} step={0.1} value={weight}
              className="h-8 text-xs"
              onChange={(e) => setWeight(e.target.value)}
            />
            <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">
              大于 1 更常被选中，小于 1 更少。填 0 表示<strong className="font-medium">不参与自动轮转</strong>
              （手动指定仍可用），不是「禁止使用」。
            </span>
          </Field>
          <Field label="并发上限">
            <Input
              type="number" min={0} max={64} step={1} value={concurrency}
              className="h-8 text-xs"
              onChange={(e) => setConcurrency(e.target.value)}
            />
            <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">
              同一时刻最多几个请求打到这个号，0 = 不限制。
            </span>
          </Field>
          <Field label="最小间隔（秒）">
            <Input
              type="number" min={0} max={3600} step={0.5} value={interval}
              className="h-8 text-xs"
              onChange={(e) => setInterval(e.target.value)}
            />
            <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">
              两次请求之间至少隔多久，0 = 不限速。
            </span>
          </Field>
          <p className="text-[11px] leading-4 text-muted-foreground">
            当前进行中 {inflight} 个请求。
            新加账号的默认值是 权重 {defaults.weight}、并发 {defaults.max_concurrency || "不限"}
            、间隔 {defaults.min_interval || "不限"} 秒。
          </p>
          {error ? <Banner tone="bad">{error}</Banner> : null}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" disabled={busy} onClick={onClose}>取消</Button>
          <Button size="sm" disabled={busy} onClick={submit}>{busy ? "保存中…" : "保存"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
