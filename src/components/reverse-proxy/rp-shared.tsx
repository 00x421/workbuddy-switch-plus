import { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";

import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export type Tone = "ok" | "warn" | "bad" | "muted";

const TONE_CLASS: Record<Tone, string> = {
  ok: "border-transparent bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  warn: "border-transparent bg-amber-500/15 text-amber-600 dark:text-amber-400",
  bad: "border-transparent bg-destructive/15 text-destructive",
  muted: "border-transparent bg-muted text-muted-foreground",
};

export function ToneBadge({ tone, children, className }: { tone: Tone; children: React.ReactNode; className?: string }) {
  return (
    <Badge variant="outline" className={cn("h-5 shrink-0 rounded-full border px-2 text-[11px]", TONE_CLASS[tone], className)}>
      {children}
    </Badge>
  );
}

export function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

export function fmtTime(seconds?: number | null): string {
  if (!seconds) return "未提供";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function fmtRate(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(1) + "%";
}

export function StatCard({ label, value, hint, accent }: { label: string; value: React.ReactNode; hint?: React.ReactNode; accent?: boolean }) {
  return (
    <div className={cn("rounded-xl border border-border/70 bg-card p-4", accent && "border-primary/30 bg-primary/[0.06]")}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">{value}</div>
      {hint ? <div className="mt-1 text-[11px] leading-4 text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

export function PanelHeader({ title, desc, action }: { title: React.ReactNode; desc?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-base font-medium">{title}</h2>
        {desc ? <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function Empty({ title, desc, action }: { title: string; desc?: string; action?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border/70 px-6 py-12 text-center">
      <p className="text-sm font-medium">{title}</p>
      {desc ? <p className="max-w-md text-xs leading-5 text-muted-foreground">{desc}</p> : null}
      {action}
    </div>
  );
}

export function Field({ label, hint, children }: { label: React.ReactNode; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-foreground/90">{label}</span>
      {children}
      {hint ? <span className="text-[11px] leading-4 text-muted-foreground">{hint}</span> : null}
    </label>
  );
}

export function CopyButton({ value, label = "复制", className }: { value: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!done) return;
    const id = window.setTimeout(() => setDone(false), 1600);
    return () => window.clearTimeout(id);
  }, [done]);
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={className}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
        } catch {
          /* 剪贴板不可用：忽略 */
        }
      }}
    >
      {done ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      {done ? "已复制" : label}
    </Button>
  );
}

export function CodeBlock({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border/70 bg-muted/50 px-3 py-2">
      <code className="min-w-0 flex-1 truncate font-mono text-xs">{children}</code>
      {action}
    </div>
  );
}

export function Banner({ tone = "muted", children }: { tone?: Tone; children: React.ReactNode }) {
  const map: Record<Tone, string> = {
    ok: "border-emerald-500/30 bg-emerald-500/[0.08] text-emerald-700 dark:text-emerald-300",
    warn: "border-amber-500/30 bg-amber-500/[0.08] text-amber-700 dark:text-amber-300",
    bad: "border-destructive/30 bg-destructive/[0.08] text-destructive",
    muted: "border-border/70 bg-muted/40 text-muted-foreground",
  };
  return <div className={cn("rounded-lg border px-3 py-2 text-xs leading-5", map[tone])}>{children}</div>;
}
