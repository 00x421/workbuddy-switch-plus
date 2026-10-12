import type { RpGroup, RpKey, RpRequestLog } from "./reverse-proxy";

/**
 * 请求日志的「分组」归属。
 *
 * 口径与网页端 `reverse-proxy/admin/static/app.js:614-658` 逐字对齐。
 * 两端读的是同一份日志、同一批密钥，如果归属算得不一样，
 * 同一条请求在两端会显示成不同的组 —— 那比两端都没有这个功能更糟，
 * 因为用户不知道该信哪个。改这里之前请先改那边（或一起改）。
 *
 * 日志 entry 里【没有】分组字段（`admin/pool.py:523-541` 只有 `key_id`），
 * 分组挂在密钥上：`key_id` -> `RpKey.groups[]` -> `RpGroup.name`。
 * 这三份数据前端已经全有：`overview.keys`（含 groups）、`overview.groups`、`row.key_id`。
 * 所以不需要后端加字段。
 */

/**
 * 三种「没有组」的情形必须分开，否则用户看不出区别：
 *
 *   `key_id` 有值 + `groups` 空  -> 「不限」：密钥有效，能用全部账号（与密钥页 groupTags 一致）
 *   `key_id` 为 null            -> 「无密钥」：这次调用根本没走密钥（直连 / 被拒请求）
 *   `key_id` 有值但查不到密钥    -> 「密钥已删除」：日志还在，密钥没了
 *
 * 把后两种也显示成「不限」会让「没有分组」和「没有密钥」看起来一样，
 * 而它们的排查方向完全不同（一个去查密钥配置，一个去查调用方有没有带 Authorization）。
 */
export const LOG_GROUP_NONE = "不限";
export const LOG_GROUP_NOKEY = "无密钥";
export const LOG_GROUP_MISSING = "密钥已删除";

/**
 * 「一把密钥绑多个分组」在分组展示时单列一类。
 *
 * 为什么不重复计入每个组：那会让各组合计大于总数，看起来像统计 bug；
 * 为什么不按第一个组归：那会把这条请求从第二个组里藏起来。
 * 单列一类既不丢信息，也不制造对不上的合计。
 */
export const LOG_GROUP_MULTI = "多分组";

/** 四个特殊桶（不是真实分组 id）。分组筛选下拉据此判断该按 bucket 还是按 id 命中。 */
export const LOG_GROUP_BUCKETS: readonly string[] = [
  LOG_GROUP_NONE, LOG_GROUP_NOKEY, LOG_GROUP_MISSING, LOG_GROUP_MULTI,
];

export type LogGroupInfo = {
  /** 这行实际绑定的分组 id（用于按 id 筛选）。特殊情形为空数组。 */
  ids: string[];
  /** 列里显示的文字：真实组名用「、」连接，或某个特殊桶名。 */
  label: string;
  /** 分节时归到哪一类：真实组 id，或某个特殊桶名。 */
  bucket: string;
  /** 节标题显示的文字（多分组时用桶名，见下）。 */
  bucketLabel?: string;
};

/**
 * 一行日志的分组归属。
 *
 * @param row     日志行（只有 `key_id` 关键）
 * @param keys    `overview.keys`（含 `groups`）
 * @param groups  `overview.groups`（id -> name 映射）
 */
export function logGroupOf(
  row: Pick<RpRequestLog, "key_id"> | null | undefined,
  keys: RpKey[] | null | undefined,
  groups: RpGroup[] | null | undefined,
): LogGroupInfo {
  // ② key_id 为 null（也兼容 undefined / 空串）—— 这次调用根本没走密钥。
  if (!row || row.key_id === null || row.key_id === undefined || row.key_id === "") {
    return { ids: [], label: LOG_GROUP_NOKEY, bucket: LOG_GROUP_NOKEY };
  }
  // ③ key_id 有值但查不到密钥 —— 日志还在，密钥没了。
  const key = (keys || []).find((k) => k.id === row.key_id);
  if (!key) return { ids: [], label: LOG_GROUP_MISSING, bucket: LOG_GROUP_MISSING };
  const ids = (key.groups || []).filter(Boolean);
  // ① key_id 有值 + groups 空 —— 密钥有效，能用全部账号。
  if (!ids.length) return { ids: [], label: LOG_GROUP_NONE, bucket: LOG_GROUP_NONE };
  const names = new Map((groups || []).map((g) => [g.id, g.name]));
  const label = ids.map((id) => names.get(id) || id).join("、");
  /*
   * 列里显示真实组名（这行实际走了哪些组），分节标题显示桶名。
   * 两个问题不一样：列回答「这行属于谁」，节标题回答「这一节是按什么聚起来的」。
   * 多分组的行自成一节，如果标题直接显示「高速组、低价组」，
   * 那只是碰巧可读 —— 换个组合顺序就变成另一个标题，用户无从知道这是一类。
   * 所以这时用明确的桶名。
   */
  return {
    ids,
    label,
    bucket: ids.length > 1 ? LOG_GROUP_MULTI : ids[0],
    bucketLabel: ids.length > 1 ? LOG_GROUP_MULTI : label,
  };
}

/**
 * 「分组」列最终显示成什么。
 *
 * 把这一层从 JSX 里提出来，是为了让「四个口径在界面上两两可区分」这件事
 * 能被单测直接验证（本项目的 vitest 跑在 node 环境，没有 DOM）。
 * 抽出来的判据就是渲染时真正用到的那三个字段，所以它与 JSX 同源、不会漂移。
 */
export type LogGroupCell = {
  /** 单元格文字。 */
  text: string;
  /** 真实分组 = "group"（正常字色）；特殊桶 = "note"（弱化字色）。 */
  tone: "group" | "note";
  /** 多分组时附带的「(N 组)」标记；其余为 null。 */
  badge: string | null;
};

export function logGroupCell(g: LogGroupInfo): LogGroupCell {
  if (g.ids.length > 0) {
    return {
      text: g.label,
      tone: "group",
      badge: g.ids.length > 1 ? `(${g.ids.length} 组)` : null,
    };
  }
  // 特殊桶（不限 / 无密钥 / 密钥已删除）用弱化字色，与真实分组在视觉上分开。
  return { text: g.label, tone: "note", badge: null };
}

/** 分组筛选是否命中该行。`filter` 为 "all" 时恒真。 */
export function logRowMatchesGroup(
  row: Pick<RpRequestLog, "key_id"> | null | undefined,
  filter: string,
  keys: RpKey[] | null | undefined,
  groups: RpGroup[] | null | undefined,
): boolean {
  if (filter === "all") return true;
  const g = logGroupOf(row, keys, groups);
  // 具体分组按 id 命中（多分组的行，绑了就该被筛出来）；
  // 四个特殊桶按 bucket 命中。
  if (LOG_GROUP_BUCKETS.includes(filter)) return g.bucket === filter;
  return g.ids.includes(filter);
}

/** 分节后的一节。 */
export type LogGroupSection = {
  /** 节标题（真实组名，或「多分组」这类桶名）。 */
  label: string;
  bucket: string;
  rows: RpRequestLog[];
  tokens: number;
  credit: number;
};

/**
 * 按分组分节。
 *
 * 组间按「该组最新一条」降序，组内时间倒序 —— 顺序错了这页就没用：
 * 排查时最关心的是「刚刚发生了什么」，所以最新的组必须排在最上面。
 * 空分组不渲染（只对传入的 rows 分组，没有行的组自然不存在）。
 */
export function logGroupSections(
  rows: RpRequestLog[],
  keys: RpKey[] | null | undefined,
  groups: RpGroup[] | null | undefined,
): LogGroupSection[] {
  const byBucket = new Map<string, LogGroupSection>();
  for (const row of rows) {
    const g = logGroupOf(row, keys, groups);
    const item = byBucket.get(g.bucket) ?? {
      label: g.bucketLabel || g.label, bucket: g.bucket, rows: [], tokens: 0, credit: 0,
    };
    item.rows.push(row);
    item.tokens += row.tokens || 0;
    item.credit += row.credit || 0;
    byBucket.set(g.bucket, item);
  }
  return [...byBucket.values()]
    .map((s) => ({ ...s, rows: s.rows.slice().sort((a, b) => (b.time || 0) - (a.time || 0)) }))
    .sort((a, b) => (b.rows[0]?.time || 0) - (a.rows[0]?.time || 0));
}

/**
 * 分组筛选下拉的选项：只列当前日志里真正出现过的分组。
 *
 * 理由：列出没出现过的组，用户选了之后得到空表，会以为功能坏了。
 * 顺序：真实分组按「最新一条」降序在前（与分节顺序一致），四个特殊桶固定在后。
 */
export function logGroupOptions(
  rows: RpRequestLog[],
  keys: RpKey[] | null | undefined,
  groups: RpGroup[] | null | undefined,
): { value: string; label: string }[] {
  const seen = new Map<string, string>();
  for (const row of rows) {
    const g = logGroupOf(row, keys, groups);
    if (LOG_GROUP_BUCKETS.includes(g.bucket)) continue;
    if (!seen.has(g.bucket)) seen.set(g.bucket, g.label);
  }
  const real = [...seen.entries()].map(([value, label]) => ({ value, label }));
  // 特殊桶只在真出现过时才列。
  const presentBuckets = new Set(rows.map((r) => logGroupOf(r, keys, groups).bucket));
  const special = LOG_GROUP_BUCKETS
    .filter((b) => presentBuckets.has(b))
    .map((b) => ({ value: b, label: b }));
  return [...real, ...special];
}
