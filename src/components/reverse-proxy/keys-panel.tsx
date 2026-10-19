import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Copy, Eye, EyeOff, KeyRound, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { rp, type RpGroup, type RpKey, type RpKeyInput, type RpOverview } from "@/lib/reverse-proxy";
import { Banner, CodeBlock, CopyButton, Empty, Field, PanelHeader, ToneBadge, fmtNum, fmtTime } from "./rp-shared";
import { ConfirmDialog, useConfirm } from "./confirm-dialog";

type FormState = {
  name: string;
  realm: string;
  models: string;
  ipAllowlist: string;
  maxIps: string;
  quota: string;
  quotaCredit: string;
  expiresDays: string;
  enabled: boolean;
  /**
   * 绑定的上游分组 id。
   *
   * 用数组而不是单个 id：一枚密钥可以同时属于多个分组（服务端 `groups.norm_groups`
   * 也按数组存）。空数组 = 不限制，可路由到全部账号。
   */
  groups: string[];
};

const EMPTY_FORM: FormState = {
  name: "", realm: "", models: "", ipAllowlist: "", maxIps: "0",
  quota: "0", quotaCredit: "0", expiresDays: "0", enabled: true, groups: [],
};

function splitList(value: string): string[] {
  return value.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
}

/**
 * 模型白名单选择器。
 *
 * 以前是个纯文本框，得自己记住模型名再手敲——拼错了要到失焦时才提示。
 * 现在把服务端 `converter.get_available_models()（overview.models）` 的清单直接列出来
 * 点着选；仍然允许手动输入清单里没有的名字（上游可能加了新模型）。
 */
function ModelPicker({ value, options, free, onChange }: {
  value: string[];
  options: string[];
  /** 限时免费的模型。拉不到就是空集——不标色，绝不瞎标。 */
  free?: Set<string>;
  onChange: (next: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const [freeOnly, setFreeOnly] = useState(false);
  const selected = new Set(value);
  const needle = query.trim().toLowerCase();
  // 「只看免费」先过滤再截断：先切片会把后面的免费模型切掉，
  // 表现为「勾了只看免费却只出来两个」，其实是被 60 条上限吃掉了。
  const byFree = freeOnly && free && free.size > 0 ? options.filter((m) => free.has(m)) : options;
  const filtered = needle
    ? byFree.filter((m) => m.toLowerCase().includes(needle)).slice(0, 60)
    : byFree;

  function toggle(model: string) {
    onChange(selected.has(model) ? value.filter((m) => m !== model) : [...value, model]);
  }

  function addCustom() {
    const name = query.trim();
    if (!name) return;
    if (!selected.has(name)) onChange([...value, name]);
    setQuery("");
  }

  return (
    <div className="flex flex-col gap-2">
      {value.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {value.map((m) => (
            <span key={m} className="flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-foreground">
              {m}
              <button
                type="button"
                aria-label={`移除 ${m}`}
                className="text-muted-foreground hover:text-destructive"
                onClick={() => onChange(value.filter((x) => x !== m))}
              >×</button>
            </span>
          ))}
        </div>
      ) : (
        <span className="text-[11px] text-muted-foreground">未限制：该密钥可以使用任何模型。</span>
      )}

      <div className="flex gap-2">
        <Input
          className="h-8 text-xs"
          value={query}
          placeholder="搜索可用模型，或直接输入清单里没有的名字"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { e.preventDefault(); addCustom(); }
          }}
        />
        {query.trim() && !options.includes(query.trim()) ? (
          <Button type="button" variant="outline" size="sm" className="h-8 shrink-0" onClick={addCustom}>
            <Plus className="size-3.5" />添加
          </Button>
        ) : null}
      </div>

      {options.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">服务端没有返回可用模型清单，请手动输入。</p>
      ) : (
        <div className="max-h-40 overflow-y-auto rounded-md border border-border/70 bg-muted/20 p-1.5">
          <div className="flex flex-wrap gap-1">
            {filtered.map((m) => {
              const on = selected.has(m);
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => toggle(m)}
                  className={
                    "rounded px-1.5 py-0.5 font-mono text-[10px] transition-colors " +
                    (on
                      ? "bg-primary text-primary-foreground"
                      : "bg-background text-muted-foreground hover:bg-muted hover:text-foreground")
                  }
                >{m}</button>
              );
            })}
            {filtered.length === 0 ? (
              <span className="px-1 py-0.5 text-[11px] text-muted-foreground">没有匹配的模型。</span>
            ) : null}
          </div>
        </div>
      )}

      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span className="flex items-center gap-3">
          <span>点击即选中，再点一次取消。已选 {value.length} 个。</span>
          {free && free.size > 0 ? (
            <label className="flex cursor-pointer items-center gap-1">
              <input
                type="checkbox"
                className="size-3"
                checked={freeOnly}
                onChange={(e) => setFreeOnly(e.target.checked)}
              />
              只看限时免费（{free.size}）
            </label>
          ) : null}
        </span>
        {value.length > 0 ? (
          <button type="button" className="text-primary hover:underline" onClick={() => onChange([])}>清空</button>
        ) : null}
      </div>

      {freeOnly && free && free.size > 0 ? (
        <button
          type="button"
          className="self-start text-[11px] text-primary hover:underline"
          onClick={() => onChange([...new Set([...value, ...filtered])])}
        >
          把当前筛出的 {filtered.length} 个全选
        </button>
      ) : null}
    </div>
  );
}

/**
 * 上游分组选择器。
 *
 * 绑定分组后，这枚密钥的请求只会落在组内的账号上——相当于「按线路隔离」。
 * 一个分组都不勾 = 不限制，走全部账号（与旧行为一致）。
 *
 * 空分组（`group.empty`）也会列出来但标灰：允许绑定，只是绑了以后一时半会儿
 * 没有账号可用；把它藏起来反而会让人以为分组没建成。
 */
function GroupPicker({ value, groups, onChange }: {
  value: string[];
  groups: RpGroup[];
  onChange: (next: string[]) => void;
}) {
  const selected = new Set(value);

  function toggle(id: string) {
    onChange(selected.has(id) ? value.filter((x) => x !== id) : [...value, id]);
  }

  if (groups.length === 0) {
    return (
      <p className="text-[11px] text-muted-foreground">
        还没有建立分组。到「账号 → 分组」里新建一个，就能把密钥限制在组内账号上。
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="max-h-40 overflow-y-auto rounded-md border border-border/70 bg-muted/20 p-1.5">
        <div className="flex flex-col gap-0.5">
          {groups.map((group) => {
            const on = selected.has(group.id);
            return (
              <label
                key={group.id}
                className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-xs hover:bg-muted"
              >
                <input
                  type="checkbox"
                  className="size-3.5 accent-[var(--primary)]"
                  checked={on}
                  onChange={() => toggle(group.id)}
                />
                <span className={on ? "font-medium" : ""}>{group.name}</span>
                {!group.enabled ? <ToneBadge tone="muted">已停用</ToneBadge> : null}
                {group.enabled && group.empty ? <ToneBadge tone="warn">空组</ToneBadge> : null}
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                  {group.account_count} 个账号
                </span>
              </label>
            );
          })}
        </div>
      </div>
      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span>
          {value.length === 0
            ? "未限制：可用全部账号。"
            : `已绑定 ${value.length} 个分组，仅在这些组内调度。`}
        </span>
        {value.length > 0 ? (
          <button type="button" className="text-primary hover:underline" onClick={() => onChange([])}>清空</button>
        ) : null}
      </div>
    </div>
  );
}

function keyTone(key: RpKey): { tone: "ok" | "warn" | "bad" | "muted"; label: string } {
  if (!key.enabled) return { tone: "muted", label: "已停用" };
  if (key.expires_at && key.expires_at * 1000 < Date.now()) return { tone: "bad", label: "已过期" };
  if (key.quota && key.used_tokens >= key.quota) return { tone: "warn", label: "配额用尽" };
  if (key.quota_credit && key.used_credit >= key.quota_credit) return { tone: "warn", label: "积分用尽" };
  return { tone: "ok", label: "可用" };
}

export function KeysPanel({ overview, reload }: { overview: RpOverview; reload: () => Promise<void> }) {
  const [editing, setEditing] = useState<RpKey | null>(null);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<{ name: string; key: string } | null>(null);
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  /**
   * 限时免费的模型集合。
   *
   * `overview.models` 只有名字，没有免费标记——那个字段在 `/admin/api/models` 的
   * `tags` 里。所以这里单独拉一次目录，失败就退化成空集（不标色），
   * 不能让「拿不到免费列表」把整个密钥面板卡住。
   */
  const [freeModels, setFreeModels] = useState<Set<string>>(() => new Set());
  const confirm = useConfirm();

  useEffect(() => {
    let alive = true;
    rp.modelCatalog()
      .then((catalog) => {
        if (!alive) return;
        setFreeModels(new Set(catalog.models.filter((m) => m.free).map((m) => m.id)));
      })
      .catch(() => { /* 免费标记是锦上添花，拉不到就不标 */ });
    return () => { alive = false; };
  }, []);

  const keys = useMemo(() => [...overview.keys].sort((a, b) => (b.created ?? 0) - (a.created ?? 0)), [overview.keys]);
  // 有 secret 才能显示明文；旧的、创建于这个版本之前的条目只有 hint。
  const revealable = keys.some((k) => k.secret);
  // 密钥里存的是分组 id，表格上要显示成人能看懂的名字，所以先建一张 id → 名称表。
  // 分组被删掉后 id 可能悬空（服务端删组时会顺带解绑，但旧数据未必），
  // 那时退回显示 id 本身，好过显示「未知」。
  const groups = overview.groups ?? [];
  const groupName = useMemo(() => new Map(groups.map((g) => [g.id, g.name])), [groups]);

  function askRevoke(key: RpKey) {
    confirm.ask({
      title: "撤销客户端密钥？",
      description: `使用「${key.name}」的客户端将无法发起新请求。此操作不可撤销。`,
      confirmLabel: "撤销",
      danger: true,
      run: () => rp.deleteKey(key.id),
    });
  }

  async function resetUsage(key: RpKey) {
    try {
      await rp.resetKeyUsage(key.id);
      toast.success("已重置该密钥的用量");
      await reload();
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <PanelHeader
        title="API 密钥"
        desc="为不同客户端分别签发密钥，可限定来源 IP、可用模型与用量额度；多密钥分发时各自独立计费。"
        action={<Button size="sm" onClick={() => setCreating(true)}><Plus className="size-3.5" />创建密钥</Button>}
      />

      <Banner tone="muted">
        密钥明文保存在本机 <code className="font-mono text-[11px]">state.json</code> 里，随时可以点
        <Eye className="mx-1 inline size-3.5 align-[-2px]" />
        查看；默认就显示，点一下可以藏起来。展示区已加 <code className="font-mono text-[11px]">notranslate</code>，
        避免被浏览器或翻译插件「顺手翻译」导致复制出错。
      </Banner>
      {keys.length === 1 ? (
        <p className="text-[11px] text-muted-foreground">当前只有一枚密钥，服务端会拒绝撤销——以免把数据面锁死。需要下线时先创建新密钥。</p>
      ) : null}
      {keys.length > 0 && !revealable ? (
        <Banner tone="warn">
          这些密钥是在支持「查看明文」之前创建的，服务端只有哈希，无法还原。
          如果确实需要随时复制，可以新建一枚替换掉它。
        </Banner>
      ) : null}

      {keys.length === 0 ? (
        <Empty title="还没有客户端密钥" desc="创建一枚密钥，填入你的客户端（OpenAI / Anthropic 兼容）。" action={<Button size="sm" onClick={() => setCreating(true)}>创建密钥</Button>} />
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border/70">
          <table className="w-full min-w-[1000px] text-sm">
            <thead className="bg-muted/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">名称</th>
                <th className="px-3 py-2 text-left font-medium">密钥</th>
                <th className="px-3 py-2 text-left font-medium">状态</th>
                <th className="px-3 py-2 text-left font-medium">分组</th>
                <th className="px-3 py-2 text-left font-medium">IP 白名单</th>
                <th className="px-3 py-2 text-left font-medium">模型白名单</th>
                <th className="px-3 py-2 text-left font-medium">额度</th>
                <th className="px-3 py-2 text-left font-medium">最近使用</th>
                <th className="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((key) => {
                const state = keyTone(key);
                return (
                  <tr key={key.id} className="border-t border-border/60 align-top">
                    <td className="px-3 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <KeyRound className="size-3.5 text-muted-foreground" />
                        <span className="font-medium">{key.name}</span>
                        {key.realm ? <ToneBadge tone="muted">{key.realm === "cn" ? "国内版" : "国际版"}</ToneBadge> : null}
                      </div>
                    </td>
                    <td className="px-3 py-2.5">
                      {!key.secret ? (
                        <div className="font-mono text-[11px] text-muted-foreground">{key.hint || key.prefix || "—"}</div>
                      ) : revealed[key.id] ? (
                        <div className="flex max-w-72 items-center gap-1.5">
                          <code
                            className="min-w-0 break-all font-mono text-[11px] leading-4"
                            translate="no"
                          >{key.secret}</code>
                          <Button
                            variant="ghost" size="icon" className="size-6 shrink-0"
                            title="隐藏密钥" aria-label="隐藏密钥"
                            onClick={() => setRevealed((s) => ({ ...s, [key.id]: false }))}
                          ><EyeOff className="size-3.5" /></Button>
                          <Button
                            variant="ghost" size="icon" className="size-6 shrink-0"
                            title="复制密钥" aria-label="复制密钥"
                            onClick={() => { void navigator.clipboard.writeText(key.secret ?? "").then(() => toast.success("已复制")).catch(() => toast.error("无法自动复制")); }}
                          ><Copy className="size-3.5" /></Button>
                        </div>
                      ) : (
                        <div className="flex items-center gap-1.5">
                          <code className="font-mono text-[11px] text-muted-foreground">{"•".repeat(14)}</code>
                          <Button
                            variant="ghost" size="icon" className="size-6 shrink-0"
                            title="显示密钥" aria-label="显示密钥"
                            onClick={() => setRevealed((s) => ({ ...s, [key.id]: true }))}
                          ><Eye className="size-3.5" /></Button>
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      <ToneBadge tone={state.tone}>{state.label}</ToneBadge>
                      {key.expires_at ? <div className="mt-1 text-[11px] text-muted-foreground">至 {fmtTime(key.expires_at)}</div> : <div className="mt-1 text-[11px] text-muted-foreground">永不过期</div>}
                    </td>
                    <td className="px-3 py-2.5">
                      {(key.groups ?? []).length === 0 ? <span className="text-xs text-muted-foreground">不限</span> : (
                        <div className="flex max-w-48 flex-wrap gap-1">
                          {(key.groups ?? []).map((gid) => (
                            <code key={gid} className="rounded bg-muted px-1.5 py-0.5 text-[10px]" title={gid}>
                              {groupName.get(gid) ?? gid}
                            </code>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      {key.ip_allowlist.length === 0 ? <span className="text-xs text-muted-foreground">不限</span> : (
                        <div className="flex max-w-48 flex-wrap gap-1">
                          {key.ip_allowlist.map((cidr) => <code key={cidr} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{cidr}</code>)}
                        </div>
                      )}
                      {key.max_ips ? <div className="mt-1 text-[11px] text-muted-foreground">最多 {key.max_ips} 个 IP（已用 {key.ips.length}）</div> : null}
                    </td>
                    <td className="px-3 py-2.5">
                      {key.models.length === 0 ? <span className="text-xs text-muted-foreground">不限</span> : (
                        <div className="flex max-w-56 flex-wrap gap-1">
                          {key.models.slice(0, 4).map((m) => <code key={m} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{m}</code>)}
                          {key.models.length > 4 ? <span className="text-[10px] text-muted-foreground">+{key.models.length - 4}</span> : null}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-xs">
                      <div>Token {fmtNum(key.used_tokens, 0)}{key.quota ? ` / ${fmtNum(key.quota, 0)}` : " / 不限"}</div>
                      <div className="text-muted-foreground">积分 {fmtNum(key.used_credit)}{key.quota_credit ? ` / ${fmtNum(key.quota_credit)}` : " / 不限"}</div>
                    </td>
                    <td className="px-3 py-2.5 text-[11px] text-muted-foreground">{key.last_used_at ? fmtTime(key.last_used_at) : "从未使用"}</td>
                    <td className="px-3 py-2.5">
                      <div className="flex flex-wrap justify-end gap-1.5">
                        <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => setEditing(key)}>编辑</Button>
                        <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => void resetUsage(key)}>重置用量</Button>
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                          disabled={keys.length <= 1}
                          title={keys.length <= 1 ? "服务端要求至少保留一枚密钥：请先创建新密钥，再撤销这一枚" : undefined}
                          onClick={() => askRevoke(key)}
                        >
                          撤销
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {(creating || editing) ? (
        <KeyFormDialog
          open
          existing={editing}
          models={overview.models}
          freeModels={freeModels}
          groups={groups}
          onOpenChange={(open) => { if (!open) { setCreating(false); setEditing(null); } }}
          onSaved={async (issuedKey, name) => {
            setCreating(false);
            setEditing(null);
            await reload();
            if (issuedKey) setIssued({ name, key: issuedKey });
          }}
        />
      ) : null}

      <Dialog open={Boolean(issued)} onOpenChange={(open) => { if (!open) setIssued(null); }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>密钥已创建</DialogTitle>
            <DialogDescription>
              现在复制到客户端即可。就算关掉这个窗口，之后也能在列表里点
              <Eye className="mx-1 inline size-3.5 align-[-2px]" />
              重新查看。
            </DialogDescription>
          </DialogHeader>
          <CodeBlock action={<CopyButton value={issued?.key ?? ""} />}>
            <span className="whitespace-pre-wrap break-all" translate="no">{issued?.key}</span>
          </CodeBlock>
          <DialogFooter><Button size="sm" onClick={() => setIssued(null)}>完成</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog request={confirm.request} onClose={confirm.close} onDone={() => void reload().catch(() => undefined)} />
    </div>
  );
}

function KeyFormDialog({ open, existing, models, freeModels, groups, onOpenChange, onSaved }: {
  open: boolean;
  existing: RpKey | null;
  /** 服务端当前可用的模型清单，用于白名单点选。 */
  models: string[];
  /** 限时免费的模型（`models` 的子集）。目录拉不到就是空集。 */
  freeModels: Set<string>;
  /** 可绑定的上游分组；为空时表单给一句引导而不是空白框。 */
  groups: RpGroup[];
  onOpenChange: (open: boolean) => void;
  onSaved: (issuedKey: string | null, name: string) => Promise<void> | void;
}) {
  const [form, setForm] = useState<FormState>(() => existing ? {
    name: existing.name,
    realm: existing.realm,
    models: existing.models.join("\n"),
    ipAllowlist: existing.ip_allowlist.join("\n"),
    maxIps: String(existing.max_ips ?? 0),
    quota: String(existing.quota ?? 0),
    quotaCredit: String(existing.quota_credit ?? 0),
    expiresDays: "0",
    enabled: existing.enabled,
    // 复制一份：勾选会改这个数组，直接引用 `existing.groups` 会顺带改到列表页那份缓存。
    groups: [...(existing.groups ?? [])],
  } : EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [unknown, setUnknown] = useState<string[]>([]);

  function expiresAt(): number | null {
    const days = Number(form.expiresDays);
    if (!days || days <= 0) return existing?.expires_at && form.expiresDays === "0" ? null : null;
    return Math.floor(Date.now() / 1000) + Math.round(days * 86400);
  }

  const payload: RpKeyInput = {
    name: form.name.trim(),
    realm: form.realm,
    models: splitList(form.models),
    ip_allowlist: splitList(form.ipAllowlist),
    max_ips: Number(form.maxIps) || 0,
    quota: Number(form.quota) || 0,
    quota_credit: Number(form.quotaCredit) || 0,
    // 空数组是「不限制」的有效取值，必须显式带上——不传的话服务端会按
    // 「请求里没有这个字段」处理，编辑时就会出现「改了别的字段，分组被悄悄清空」。
    groups: form.groups,
  };

  async function checkModels(names: string[] = splitList(form.models)) {
    if (names.length === 0) { setUnknown([]); return; }
    try {
      const result = await rp.checkModels(names);
      setUnknown(result.unknown ?? []);
    } catch {
      setUnknown([]);
    }
  }

  async function submit() {
    setError("");
    if (!payload.name) { setError("密钥名称不能只有空格"); return; }
    setBusy(true);
    try {
      if (existing) {
        await rp.updateKey(existing.id, { ...payload, enabled: form.enabled, expires_at: expiresAt() });
        toast.success("密钥已更新");
        await onSaved(null, payload.name);
      } else {
        const result = await rp.createKey({ ...payload, expires_at: expiresAt() });
        await onSaved(result.key, payload.name);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
      <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{existing ? "编辑密钥" : "创建客户端密钥"}</DialogTitle>
          <DialogDescription>留空表示不限制。IP 白名单支持单个 IP 或 CIDR（如 10.0.0.0/8）。</DialogDescription>
        </DialogHeader>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="名称">
            <Input value={form.name} maxLength={60} placeholder="例如：Cherry Studio" onChange={(e) => setForm((s) => ({ ...s, name: e.target.value }))} />
          </Field>
          <Field label="版本归属">
            <Select value={form.realm || "any"} onValueChange={(v) => setForm((s) => ({ ...s, realm: v === "any" ? "" : v }))}>
              <SelectTrigger className="h-9 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="any">不限制</SelectItem>
                <SelectItem value="cn">仅国内版</SelectItem>
                <SelectItem value="global">仅国际版</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field label="IP 白名单" hint="每行一个 IP 或 CIDR；留空不限制。">
            <textarea className="min-h-20 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs outline-none focus-visible:border-ring" value={form.ipAllowlist} placeholder={"127.0.0.1\n10.0.0.0/8"} spellCheck={false} onChange={(e) => setForm((s) => ({ ...s, ipAllowlist: e.target.value }))} />
          </Field>
          <div className="sm:col-span-2">
            <Field label="模型白名单" hint={unknown.length ? `无法识别：${unknown.join("、")}` : "留空表示不限制。下面列出的是服务端当前可用的模型，点选即可。"}>
              <ModelPicker
                value={splitList(form.models)}
                options={models}
                free={freeModels}
                onChange={(next) => {
                  setForm((s) => ({ ...s, models: next.join("\n") }));
                  void checkModels(next);
                }}
              />
            </Field>
          </div>
          <Field label="最大 IP 数" hint="0 = 不限（限制同一密钥可使用的来源 IP 数量）。">
            <Input type="number" min={0} className="h-9" value={form.maxIps} onChange={(e) => setForm((s) => ({ ...s, maxIps: e.target.value }))} />
          </Field>
          <Field label="有效期（天）" hint="0 = 永不过期。">
            <Input type="number" min={0} className="h-9" value={form.expiresDays} onChange={(e) => setForm((s) => ({ ...s, expiresDays: e.target.value }))} />
          </Field>
          <Field label="Token 额度" hint="按累计 token 计数，0 = 不限。">
            <Input type="number" min={0} className="h-9" value={form.quota} onChange={(e) => setForm((s) => ({ ...s, quota: e.target.value }))} />
          </Field>
          <Field label="积分额度" hint="按上游真实扣费累计，0 = 不限。">
            <Input type="number" min={0} step="0.01" className="h-9" value={form.quotaCredit} onChange={(e) => setForm((s) => ({ ...s, quotaCredit: e.target.value }))} />
          </Field>
        </div>

        <Field
          label="上游分组"
          hint="勾选后，这枚密钥的请求只会调度到组内账号；一个都不勾表示可用全部账号。"
        >
          <GroupPicker
            value={form.groups}
            groups={groups}
            onChange={(next) => setForm((s) => ({ ...s, groups: next }))}
          />
        </Field>

        {existing ? (
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" className="size-4 accent-[var(--primary)]" checked={form.enabled} onChange={(e) => setForm((s) => ({ ...s, enabled: e.target.checked }))} />
            启用该密钥
          </label>
        ) : null}

        {unknown.length ? <Banner tone="warn">以下模型名匹配不到当前可用模型，请确认拼写：{unknown.join("、")}</Banner> : null}
        {error ? <Banner tone="bad">{error}</Banner> : null}

        <DialogFooter>
          <Button variant="outline" size="sm" disabled={busy} onClick={() => onOpenChange(false)}>取消</Button>
          <Button size="sm" disabled={busy} onClick={() => void submit()}>{busy ? "保存中…" : existing ? "保存" : "创建密钥"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
