import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Globe, Plus, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { rp, type RpEgressAccount, type RpEgressLine } from "@/lib/reverse-proxy";
import { Banner, Empty, Field, PanelHeader, ToneBadge } from "./rp-shared";
import { ConfirmDialog, useConfirm } from "./confirm-dialog";

/** 「直连」在下拉里不能是空串（Radix 会把空串当成「未选择」渲染成占位符）。 */
const DIRECT = "__direct__";

/**
 * 账号出口线路：给单个账号指定一条命名 HTTP / HTTPS 代理。
 *
 * 为什么值得单独配：账号一多就会出现「同一条出口 IP 上挂着十几个号」的形态，
 * 上游对这种形态的容忍度比预期低，而出问题时的表现很含糊（不是 403，而是莫名
 * 其妙的冷却）。把出口拆开之后账号之间互不牵连，排查时也能分清「是这一个号的
 * 问题，还是这一整条线路的问题」。
 *
 * 列表里显示的地址是服务端脱敏过的（`user:***@host`）：带凭据的商用代理
 * 口令不能送进浏览器。因此「改地址」对话框里的输入框是空的——拿不到原文，
 * 只支持整条替换，留空即不改。
 */
export function EgressPanel() {
  const [lines, setLines] = useState<RpEgressLine[]>([]);
  const [accounts, setAccounts] = useState<RpEgressAccount[]>([]);
  const [orphans, setOrphans] = useState<string[]>([]);
  const [stale, setStale] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<RpEgressLine | null>(null);
  const [draftUrl, setDraftUrl] = useState("");
  const confirm = useConfirm();

  const load = useCallback(async () => {
    try {
      const payload = await rp.egress();
      setLines(payload.lines);
      setAccounts(payload.accounts);
      setOrphans(payload.orphans);
      setStale(payload.stale ?? []);
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function run(action: () => Promise<unknown>, done: string) {
    setBusy(true);
    try {
      await action();
      toast.success(done);
      await load();
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  const nameOf = (id: string) => accounts.find((a) => a.id === id)?.name ?? id;

  return (
    <div className="flex flex-col gap-5">
      <PanelHeader
        title="账号出口线路"
        desc="按账号指定出口代理。对话转发、积分查询、签到与 token 刷新都走同一条线路——只代理其中一半会让出口 IP 这个判据失去意义。"
        action={<Button variant="outline" size="sm" onClick={() => void load()}><RefreshCw className="size-3.5" />刷新</Button>}
      />

      {orphans.length > 0 ? (
        <Banner tone="warn">
          有 {orphans.length} 个账号绑在**已删除**的线路上（{orphans.map(nameOf).join("、")}），当前按直连出网。
          这条绑定已经无解，请给它们重新指定一条线路（或在下面选「直连」把残留清掉）。
        </Banner>
      ) : null}
      {stale.length > 0 ? (
        <Banner tone="muted">
          有 {stale.length} 个账号绑在**已停用**的线路上（{stale.map(nameOf).join("、")}），当前按直连出网。
          想让它们恢复走代理，把那条线路启用回来即可，不必改账号的绑定。
        </Banner>
      ) : null}

      <section className="flex flex-wrap items-end gap-3 rounded-xl border border-border/70 bg-card p-4">
        <Field label="线路名称">
          <Input value={name} maxLength={40} placeholder="例如 香港出口" className="h-8 w-40 text-xs" onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="代理地址" hint="http:// 或 https://，可带 user:pass@">
          <Input
            value={url} spellCheck={false} autoComplete="off"
            placeholder="http://user:pass@10.0.0.1:8080"
            className="h-8 w-72 font-mono text-xs"
            onChange={(e) => setUrl(e.target.value)}
          />
        </Field>
        <Button
          size="sm"
          disabled={busy || !name.trim() || !url.trim()}
          onClick={() => void run(() => rp.createEgressLine({ name: name.trim(), url: url.trim() }), "线路已创建")
            .then(() => { setName(""); setUrl(""); })}
        ><Plus className="size-3.5" />新建线路</Button>
      </section>

      {lines.length === 0 ? (
        <Empty title="还没有出口线路" desc="不配也能用：所有账号默认直连。只有当你需要把不同账号分开出网时才需要线路。" />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border/70">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">线路</th>
                <th className="px-3 py-2 text-left font-medium">代理地址</th>
                <th className="px-3 py-2 text-left font-medium">绑定账号</th>
                <th className="px-3 py-2 text-left font-medium">启用</th>
                <th className="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.id} className="border-t border-border/60 align-top">
                  <td className="px-3 py-2 font-medium">{line.name}</td>
                  <td className="px-3 py-2">
                    <span className="font-mono text-xs">{line.url}</span>
                  </td>
                  <td className="px-3 py-2">
                    <span className="text-xs tabular-nums text-muted-foreground">{line.account_count} 个</span>
                    {line.account_names.length ? (
                      <div className="mt-1 max-w-xs text-[11px] leading-4 text-muted-foreground">{line.account_names.join("、")}</div>
                    ) : null}
                  </td>
                  <td className="px-3 py-2">
                    <Switch
                      checked={line.enabled}
                      disabled={busy}
                      aria-label={`启用线路 ${line.name}`}
                      onCheckedChange={(checked) => void run(() => rp.updateEgressLine(line.id, { enabled: checked }),
                        checked ? "线路已启用" : "线路已停用（绑定的账号改为直连）")}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1.5">
                      <Button
                        variant="outline" size="sm" className="h-7 px-2 text-xs"
                        onClick={() => { setEditing(line); setDraftUrl(""); }}
                      >改地址</Button>
                      <Button
                        variant="outline" size="sm" className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                        onClick={() => confirm.ask({
                          title: `删除线路「${line.name}」？`,
                          description: line.account_count > 0
                            ? `绑在它上面的 ${line.account_count} 个账号会改为直连，仍然可用。`
                            : "这条线路没有绑定任何账号。",
                          confirmLabel: "删除", danger: true,
                          run: async () => { await rp.deleteEgressLine(line.id); await load(); },
                        })}
                      >删除</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <section className="flex flex-col gap-3">
        <div>
          <h3 className="text-sm font-medium">账号出口绑定</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">每个账号只能挂一条线路；选「直连」即解除绑定。</p>
        </div>
        {accounts.length === 0 ? (
          <Empty title="账号池是空的" desc="先在「账号池」里加账号，再来分配出口。" />
        ) : (
          <div className="overflow-hidden rounded-xl border border-border/70">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">账号</th>
                  <th className="px-3 py-2 text-left font-medium">状态</th>
                  <th className="px-3 py-2 text-left font-medium">出口线路</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((account) => {
                  const current = account.line_id && lines.some((l) => l.id === account.line_id)
                    ? account.line_id : DIRECT;
                  return (
                    <tr key={account.id} className="border-t border-border/60">
                      <td className="px-3 py-2">
                        <span className="flex items-center gap-2">
                          <Globe className="size-3.5 text-muted-foreground" />
                          <span className="truncate">{account.name}</span>
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        <ToneBadge tone={account.pool_state === "available" ? "ok" : "muted"}>
                          {account.pool_state === "available" ? "可用" : "不可用"}
                        </ToneBadge>
                      </td>
                      <td className="px-3 py-2">
                        <Select
                          value={current}
                          disabled={busy}
                          onValueChange={(value) => void run(
                            () => rp.assignEgress(account.id, value === DIRECT ? "" : value),
                            value === DIRECT ? "已改为直连" : "出口线路已更新",
                          )}
                        >
                          <SelectTrigger className="h-8 w-56 text-xs" aria-label={`${account.name} 的出口线路`}>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={DIRECT}>直连（默认）</SelectItem>
                            {lines.map((line) => (
                              <SelectItem key={line.id} value={line.id}>
                                {line.name}{line.enabled ? "" : "（已停用）"}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) setEditing(null); }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>修改「{editing?.name}」的代理地址</DialogTitle>
            <DialogDescription>
              当前地址是 {editing?.url}。口令不会回显，所以只能整条替换；留空即保持不变。
            </DialogDescription>
          </DialogHeader>
          <Field label="新的代理地址">
            <Input
              value={draftUrl} spellCheck={false} autoComplete="off"
              placeholder="http://user:pass@10.0.0.1:8080"
              className="h-8 font-mono text-xs"
              onChange={(e) => setDraftUrl(e.target.value)}
            />
          </Field>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setEditing(null)}>取消</Button>
            <Button
              size="sm"
              disabled={busy || editing === null || !draftUrl.trim()}
              onClick={() => void run(async () => {
                if (!editing) return;
                await rp.updateEgressLine(editing.id, { url: draftUrl.trim() });
                setEditing(null);
              }, "代理地址已更新")}
            >保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog request={confirm.request} onClose={confirm.close} />
    </div>
  );
}
