import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Pencil, Plus, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ROUTING_LABELS, rp, type RpGroup, type RpGroupAccount } from "@/lib/reverse-proxy";
import { Banner, Empty, Field, PanelHeader, ToneBadge } from "./rp-shared";
import { ConfirmDialog, useConfirm } from "./confirm-dialog";

/** 「跟随全局」在下拉里不能是空串（Radix 里空串会被当成「未选择」渲染成占位符）。 */
const INHERIT = "__inherit__";

/**
 * 上游分组：把账号池切成互不干扰的子池。
 *
 * 界面要传达的核心只有一句：密钥绑了哪个组，它就只用那个组里的账号。
 * 所以每行都把成员账号名列出来——用户不需要点进去猜「这组里到底有谁」。
 */
export function GroupsPanel() {
  const [groups, setGroups] = useState<RpGroup[]>([]);
  const [accounts, setAccounts] = useState<RpGroupAccount[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [routing, setRouting] = useState(INHERIT);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<RpGroup | null>(null);
  const [draftMembers, setDraftMembers] = useState<string[]>([]);
  const confirm = useConfirm();

  const load = useCallback(async () => {
    try {
      const payload = await rp.groups();
      setGroups(payload.groups);
      setAccounts(payload.accounts);
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

  const orphanCount = groups.filter((g) => g.empty).length;

  return (
    <div className="flex flex-col gap-5">
      <PanelHeader
        title="上游分组"
        desc="把账号池切成若干子池；在「API 密钥」里把密钥绑到某个组，它的请求就只会用那个组里的账号。"
        action={<Button variant="outline" size="sm" onClick={() => void load()}><RefreshCw className="size-3.5" />刷新</Button>}
      />

      {orphanCount > 0 ? (
        <Banner tone="warn">
          有 {orphanCount} 个分组里没有可调度的账号。绑到这些组的密钥会直接收到 503——请补成员，或把密钥的分组绑定去掉。
        </Banner>
      ) : null}

      <section className="flex flex-wrap items-end gap-3 rounded-xl border border-border/70 bg-card p-4">
        <Field label="分组名称">
          <Input value={name} maxLength={40} placeholder="例如 自用账号" className="h-8 w-44 text-xs" onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="说明（可选）">
          <Input value={description} maxLength={200} placeholder="这组是给谁用的" className="h-8 w-52 text-xs" onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label="组内调度">
          <Select value={routing} onValueChange={setRouting}>
            <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={INHERIT}>跟随全局设置</SelectItem>
              {Object.entries(ROUTING_LABELS).map(([value, meta]) => (
                <SelectItem key={value} value={value}>{meta.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Button
          size="sm"
          disabled={busy || !name.trim()}
          onClick={() => void run(
            () => rp.createGroup({ name: name.trim(), description: description.trim(), routing: routing === INHERIT ? "" : routing }),
            "分组已创建",
          ).then(() => { setName(""); setDescription(""); setRouting(INHERIT); })}
        ><Plus className="size-3.5" />新建分组</Button>
        <p className="max-w-sm text-[11px] leading-4 text-muted-foreground">
          新建后点该行的「成员」勾选账号。空分组不会拦住用户建，但绑到它上面的密钥会被挡在 503 上。
        </p>
      </section>

      {groups.length === 0 ? (
        <Empty title="还没有分组" desc="不建分组也能用：密钥默认可以走全部账号。分组只在你需要「某把密钥只用某几个号」时才必要。" />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border/70">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">分组</th>
                <th className="px-3 py-2 text-left font-medium">成员</th>
                <th className="px-3 py-2 text-left font-medium">组内调度</th>
                <th className="px-3 py-2 text-left font-medium">启用</th>
                                <th className="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <tr key={group.id} className="border-t border-border/60 align-top">
                  <td className="px-3 py-2">
                    <div className="font-medium">{group.name}</div>
                    {group.description ? (
                      <div className="text-[11px] leading-4 text-muted-foreground">{group.description}</div>
                    ) : null}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="text-xs tabular-nums text-muted-foreground">{group.account_count} 个</span>
                      {group.empty ? <ToneBadge tone="warn">无可调度账号</ToneBadge> : null}
                    </div>
                    {/* 成员名直接列出来：用户不用点进去猜「这组里到底有谁」 */}
                    <div className="mt-1 max-w-xs text-[11px] leading-4 text-muted-foreground">
                      {group.account_names.length ? group.account_names.join("、") : "（空）"}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    {group.routing
                      ? <ToneBadge tone="muted">{ROUTING_LABELS[group.routing]?.label ?? group.routing}</ToneBadge>
                      : <span className="text-xs text-muted-foreground">跟随全局</span>}
                  </td>
                  <td className="px-3 py-2">
                    <Switch
                      checked={group.enabled}
                      disabled={busy}
                      aria-label={`启用分组 ${group.name}`}
                      onCheckedChange={(checked) => void run(() => rp.updateGroup(group.id, { enabled: checked }),
                        checked ? "分组已启用" : "分组已停用")}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1.5">
                      <Button
                        variant="outline" size="sm" className="h-7 px-2 text-xs"
                        onClick={() => { setEditing(group); setDraftMembers([...group.accounts]); }}
                      ><Pencil className="size-3" />成员</Button>
                      <Button
                        variant="outline" size="sm" className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                        onClick={() => confirm.ask({
                          title: `删除分组「${group.name}」？`,
                          description: "删掉之后，绑在这个组上的密钥会变成「不限制分组」——也就是可以走到全部账号上。这是权限放大，删之前请确认那些密钥本来就该放开。",
                          confirmLabel: "删除", danger: true,
                          run: async () => {
                            const result = await rp.deleteGroup(group.id);
                            if (result.unbound_keys > 0) {
                              toast.warning(`已解绑 ${result.unbound_keys} 枚密钥的分组限制，它们现在可以走全部账号`);
                            }
                            await load();
                          },
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

      <Dialog open={editing !== null} onOpenChange={(open) => { if (!open) setEditing(null); }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>「{editing?.name}」的成员账号</DialogTitle>
            <DialogDescription>
              只有勾选的账号会承接绑到这个分组的密钥请求。已经在别处被隐藏的账号会保留勾选但不可调度。
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-80 overflow-y-auto rounded-lg border border-border/70">
            {accounts.length === 0 ? (
              <p className="px-3 py-6 text-center text-xs text-muted-foreground">账号池是空的</p>
            ) : accounts.map((account) => {
              const checked = draftMembers.includes(account.id);
              return (
                <label key={account.id} className="flex cursor-pointer items-center gap-3 border-b border-border/50 px-3 py-2 last:border-b-0 hover:bg-muted/40">
                  <Checkbox
                    checked={checked}
                    onCheckedChange={(next) => setDraftMembers((prev) =>
                      next === true ? [...prev, account.id] : prev.filter((id) => id !== account.id))}
                  />
                  <span className="min-w-0 flex-1 truncate text-sm">{account.name}</span>
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{account.realm === "global" ? "国际版" : "国内版"}</span>
                </label>
              );
            })}
          </div>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setEditing(null)}>取消</Button>
            <Button
              size="sm"
              disabled={busy || editing === null}
              onClick={() => void run(async () => {
                if (!editing) return;
                await rp.updateGroup(editing.id, { accounts: draftMembers });
                setEditing(null);
              }, "成员已保存")}
            >保存成员</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog request={confirm.request} onClose={confirm.close} />
    </div>
  );
}
