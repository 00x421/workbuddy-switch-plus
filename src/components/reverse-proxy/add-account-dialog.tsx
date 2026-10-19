import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { FileUp, Globe } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { rp, RP_REGIONS, realmOfVariant } from "@/lib/reverse-proxy";
import { cn } from "@/lib/utils";
import { Banner, Field } from "./rp-shared";

type Mode = "browser" | "file";
type Realm = "cn" | "global";

export function AddAccountDialog({ open, onOpenChange, onAdded }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: () => void;
}) {
  const [mode, setMode] = useState<Mode>("browser");
  const [realm, setRealm] = useState<Realm>("cn");
  const [region, setRegion] = useState("HK");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [flow, setFlow] = useState<{ id: string; url: string; expires_at: number } | null>(null);
  const [status, setStatus] = useState("");
  const [fileLabel, setFileLabel] = useState("");
  const [credentialJson, setCredentialJson] = useState("");
  const timerRef = useRef<number | null>(null);
  const genRef = useRef(0);

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const resetFlow = useCallback((cancel: boolean) => {
    genRef.current += 1;
    stopPolling();
    setFlow((current) => {
      if (cancel && current) void rp.oauthCancel(current.id).catch(() => undefined);
      return null;
    });
    setStatus("");
  }, [stopPolling]);

  useEffect(() => {
    if (!open) {
      resetFlow(true);
      setMode("browser");
      setRealm("cn");
      setRegion("HK");
      setName("");
      setError("");
      setFileLabel("");
      setCredentialJson("");
      setBusy(false);
    }
  }, [open, resetFlow]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  function poll(generation: number, flowId: string) {
    if (generation !== genRef.current) return;
    stopPolling();
    timerRef.current = window.setInterval(async () => {
      if (generation !== genRef.current) return;
      try {
        const result = await rp.oauthPoll(flowId);
        if (generation !== genRef.current) return;
        if (result.status === "success") {
          stopPolling();
          toast.success(result.updated ? "授权成功，账号凭据已更新" : "授权成功，账号已添加");
          onOpenChange(false);
          onAdded();
          return;
        }
        if (result.status === "expired") {
          stopPolling();
          setStatus("登录链接已过期，请重新生成。");
          return;
        }
        setStatus("等待你在官方页面完成登录…");
      } catch (cause) {
        stopPolling();
        setStatus(cause instanceof Error ? cause.message : String(cause));
      }
    }, 3000);
  }

  async function startOAuth() {
    setError("");
    setStatus("");
    resetFlow(true);
    const generation = genRef.current;
    setBusy(true);
    try {
      const next = await rp.oauthStart(
        name.trim() || undefined,
        realmOfVariant(realm),
        realm === "global" ? region : undefined,
      );
      if (generation !== genRef.current) {
        void rp.oauthCancel(next.id).catch(() => undefined);
        return;
      }
      setFlow(next);
      setStatus("登录链接已就绪，请打开官方页面完成登录。");
      poll(generation, next.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function submitFile() {
    setError("");
    let credential: unknown;
    try {
      credential = JSON.parse(credentialJson);
    } catch {
      setError("请选择文件或粘贴有效的 JSON 内容");
      return;
    }
    setBusy(true);
    try {
      await rp.addAccount({ name: name.trim() || undefined, credential });
      toast.success("账号已导入");
      onOpenChange(false);
      onAdded();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>添加账号</DialogTitle>
          <DialogDescription>在官方页面完成登录，授权凭据保存在本地服务；账号会并入 workbuddy-switch 账号库。</DialogDescription>
        </DialogHeader>

        <div className="flex gap-2">
          {([["browser", "浏览器登录", Globe], ["file", "导入文件", FileUp]] as const).map(([value, label, Icon]) => (
            <Button
              key={value}
              type="button"
              variant={mode === value ? "default" : "outline"}
              size="sm"
              onClick={() => { setMode(value); setError(""); resetFlow(true); }}
            >
              <Icon className="size-3.5" />{label}
            </Button>
          ))}
        </div>

        <Field label={<>账号备注 <span className="text-muted-foreground">（选填）</span></>}>
          <Input value={name} maxLength={60} placeholder="例如：日常开发、备用账号" onChange={(e) => setName(e.target.value)} disabled={Boolean(flow) || mode === "browser" && busy} />
        </Field>

        {mode === "browser" ? (
          <div className="flex flex-col gap-3">
            {/* 版本选择必须放在「生成链接」之前：上游的登录域与平台参数完全按版本
                分派，选错了拿到的是另一套二维码，而且不会报错（manager 里踩过）。 */}
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium">账号版本</span>
              <div className="flex gap-2">
                {([["cn", "国内版 CodeBuddy"], ["global", "国际版 WorkBuddy AI"]] as const).map(([value, label]) => (
                  <Button
                    key={value}
                    type="button"
                    variant={realm === value ? "default" : "outline"}
                    size="sm"
                    disabled={Boolean(flow) || busy}
                    onClick={() => { setRealm(value); setError(""); resetFlow(true); }}
                  >
                    {label}
                  </Button>
                ))}
              </div>
              {realm === "global" ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs font-medium">地区 <span className="text-muted-foreground">（首次登录必须选，否则聊天报 14017）</span></span>
                  <div className="flex flex-wrap gap-1.5">
                    {RP_REGIONS.map((item) => (
                      <Button
                        key={item.code}
                        type="button"
                        variant={region === item.code ? "default" : "outline"}
                        size="sm"
                        className="h-7 px-2 text-xs"
                        disabled={Boolean(flow) || busy}
                        onClick={() => { setRegion(item.code); setError(""); }}
                      >
                        {item.label}
                      </Button>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>

            <ol className="flex flex-col gap-2 text-xs text-muted-foreground">
              <li><b className="text-foreground">1.</b> 生成专属链接，在官方页面扫码或登录。</li>
              <li><b className="text-foreground">2.</b> 返回这里保持窗口打开，系统会自动获取授权结果。</li>
            </ol>
            {flow ? (
              <div className="flex flex-wrap items-center gap-2">
                <Button asChild size="sm">
                  <a href={flow.url} target="_blank" rel="noopener noreferrer">打开官方登录页面</a>
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => { void navigator.clipboard.writeText(flow.url).then(() => toast.success("已复制")).catch(() => toast.error("无法自动复制")); }}>复制链接</Button>
                <Button type="button" variant="ghost" size="sm" onClick={startOAuth}>重新生成</Button>
              </div>
            ) : (
              <Button type="button" size="sm" className="self-start" disabled={busy} onClick={startOAuth}>
                {busy ? "正在生成…" : "生成登录链接"}
              </Button>
            )}
            {status ? <Banner tone="muted">{status}　登录链接 5 分钟内有效。</Banner> : null}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <label className={cn("flex cursor-pointer flex-col items-center gap-1 rounded-xl border border-dashed border-border/70 px-4 py-6 text-center text-xs", "hover:bg-accent/40")}>
              <FileUp className="size-5 text-muted-foreground" />
              <span className="font-medium">{fileLabel || "选择 .info / .json 文件"}</span>
              <span className="text-muted-foreground">文件最大 1 MB，内容仅传入本地服务</span>
              <input
                type="file"
                accept=".info,.json,application/json"
                className="hidden"
                onChange={async (event) => {
                  const file = event.target.files?.[0];
                  if (!file) return;
                  if (file.size > 1024 * 1024) { setError("文件超过 1 MB，请选择登录凭据文件"); return; }
                  const text = (await file.text()).replace(/^\uFEFF/, "");
                  setCredentialJson(text);
                  setFileLabel(file.name);
                  setError("");
                }}
              />
            </label>
            <Field label="或粘贴 JSON 内容">
              <textarea
                className="min-h-24 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs outline-none focus-visible:border-ring"
                value={credentialJson}
                placeholder='包含 "auth" 和 "account" 的完整 JSON'
                spellCheck={false}
                onChange={(e) => setCredentialJson(e.target.value)}
              />
            </Field>
            <Button type="button" size="sm" className="self-start" disabled={busy || !credentialJson.trim()} onClick={submitFile}>
              {busy ? "导入中…" : "导入账号"}
            </Button>
          </div>
        )}

        {error ? <Banner tone="bad">{error}</Banner> : null}
      </DialogContent>
    </Dialog>
  );
}
