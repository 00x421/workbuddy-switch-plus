import { describe, expect, it } from "vitest";

import {
  LOG_GROUP_MISSING, LOG_GROUP_MULTI, LOG_GROUP_NOKEY, LOG_GROUP_NONE,
  logGroupCell, logGroupOf, logGroupOptions, logGroupSections, logRowMatchesGroup,
} from "./log-group";
import type { RpGroup, RpKey, RpRequestLog } from "./reverse-proxy";

/** 造一行日志。只有 key_id 与本组测试相关，其余给稳定默认值。 */
function row(key_id: string | null, over: Partial<RpRequestLog> = {}): RpRequestLog {
  return {
    time: 1000, account_id: "a1", account_name: "账号1", model: "m", status: 200,
    ok: true, tokens: 10, credit: 1, duration_ms: 5, outcome: "success",
    source: "api", key_id, ...over,
  };
}

function key(id: string, groups: string[]): RpKey {
  return {
    id, name: id, hint: "", enabled: true, expires_at: null, max_ips: 0,
    ip_allowlist: [], models: [], realm: "", quota: 0, used_tokens: 0,
    quota_credit: 0, used_credit: 0, created: null, last_used_at: null, ips: [], groups,
  };
}

const KEYS: RpKey[] = [key("k_none", []), key("k_one", ["g1"]), key("k_two", ["g1", "g2"])];

function group(id: string, name: string): RpGroup {
  return {
    id, name, description: "", enabled: true, routing: "", created: null,
    accounts: [], account_names: [], account_count: 0, empty: false,
  };
}

const GROUPS: RpGroup[] = [group("g1", "高速组"), group("g2", "低价组")];

/**
 * 四个口径的归属计算。
 *
 * 与网页端 `admin/static/app.js:614-658` 对齐；之所以在桌面端也写死一套断言，
 * 是因为两端必须给出同一个结论 —— 同一个日志在两端显示不同的组，
 * 用户不知道该信哪个，比两端都没这个功能更糟。
 */
describe("logGroupOf 的四个口径", () => {
  it("① key_id 有值 + groups 空 -> 不限", () => {
    const g = logGroupOf(row("k_none"), KEYS, GROUPS);
    expect(g.label).toBe(LOG_GROUP_NONE);
    expect(g.bucket).toBe(LOG_GROUP_NONE);
    expect(g.ids).toEqual([]);
  });

  it("② key_id 为 null -> 无密钥", () => {
    const g = logGroupOf(row(null), KEYS, GROUPS);
    expect(g.label).toBe(LOG_GROUP_NOKEY);
    expect(g.bucket).toBe(LOG_GROUP_NOKEY);
    expect(g.ids).toEqual([]);
  });

  it("② 兼容 key_id 缺失 / 空串（老服务端不发该字段）", () => {
    expect(logGroupOf({}, KEYS, GROUPS).label).toBe(LOG_GROUP_NOKEY);
    expect(logGroupOf(row(""), KEYS, GROUPS).label).toBe(LOG_GROUP_NOKEY);
    expect(logGroupOf(null, KEYS, GROUPS).label).toBe(LOG_GROUP_NOKEY);
    expect(logGroupOf(undefined, KEYS, GROUPS).label).toBe(LOG_GROUP_NOKEY);
  });

  it("③ key_id 有值但查不到密钥 -> 密钥已删除", () => {
    const g = logGroupOf(row("k_gone"), KEYS, GROUPS);
    expect(g.label).toBe(LOG_GROUP_MISSING);
    expect(g.bucket).toBe(LOG_GROUP_MISSING);
    expect(g.ids).toEqual([]);
  });

  it("④ 一把密钥绑多组 -> 列里显示全部真实组名，分节自成一类「多分组」", () => {
    const g = logGroupOf(row("k_two"), KEYS, GROUPS);
    expect(g.ids).toEqual(["g1", "g2"]);
    expect(g.label).toBe("高速组、低价组");
    expect(g.bucket).toBe(LOG_GROUP_MULTI);
    expect(g.bucketLabel).toBe(LOG_GROUP_MULTI);
  });

  it("单组：列与节都用真实组名", () => {
    const g = logGroupOf(row("k_one"), KEYS, GROUPS);
    expect(g.ids).toEqual(["g1"]);
    expect(g.label).toBe("高速组");
    expect(g.bucket).toBe("g1");
    expect(g.bucketLabel).toBe("高速组");
  });

  it("组名查不到时退回显示 id（不显示成空白）", () => {
    const keys: RpKey[] = [key("k_one", ["g_unknown"])];
    expect(logGroupOf(row("k_one"), keys, GROUPS).label).toBe("g_unknown");
  });

  it("keys / groups 缺失时不抛错（概览还没加载完）", () => {
    expect(logGroupOf(row("k_one"), undefined, undefined).label).toBe(LOG_GROUP_MISSING);
    expect(logGroupOf(row("k_one"), null, null).label).toBe(LOG_GROUP_MISSING);
  });

  it("⭐ 四个口径的归属两两可区分（这是这个功能的存在理由）", () => {
    const labels = [
      logGroupOf(row("k_none"), KEYS, GROUPS).label,
      logGroupOf(row(null), KEYS, GROUPS).label,
      logGroupOf(row("k_gone"), KEYS, GROUPS).label,
      logGroupOf(row("k_two"), KEYS, GROUPS).bucket,
    ];
    expect(new Set(labels).size).toBe(4);
  });
});

/**
 * 「分组」列显示出来的内容。
 *
 * 验收第 4 条要求「四个口径的渲染结果两两可区分」。本项目的 vitest 跑在 node
 * 环境（`vitest.config.ts` 的 `environment: "node"`，没有 jsdom / testing-library），
 * 所以我把渲染判据从 JSX 里提成了纯函数 `logGroupCell`，并让组件调用同一个函数 ——
 * 这样「可区分」就是可单测的，而不是只能靠肉眼看界面。
 */
describe("logGroupCell 列渲染（四个口径两两可区分）", () => {
  const cases = {
    不限: logGroupCell(logGroupOf(row("k_none"), KEYS, GROUPS)),
    无密钥: logGroupCell(logGroupOf(row(null), KEYS, GROUPS)),
    密钥已删除: logGroupCell(logGroupOf(row("k_gone"), KEYS, GROUPS)),
    多分组: logGroupCell(logGroupOf(row("k_two"), KEYS, GROUPS)),
    单组: logGroupCell(logGroupOf(row("k_one"), KEYS, GROUPS)),
  };

  it("四个口径的文字两两不同", () => {
    const texts = [cases.不限.text, cases.无密钥.text, cases.密钥已删除.text, cases.多分组.text];
    expect(new Set(texts).size).toBe(4);
    // 前三个是特殊桶名；多分组在【列里】显示的是真实组名，不是「多分组」——
    // 这正是网页端记的那条分工：列回答「这行属于谁」，节标题回答「这一节按什么聚起来」。
    expect(texts.slice(0, 3)).toEqual([LOG_GROUP_NONE, LOG_GROUP_NOKEY, LOG_GROUP_MISSING]);
    expect(texts[3]).toBe("高速组、低价组");
  });

  it("多分组的【节标题】才是「多分组」，列里仍显示真实组名", () => {
    // 同一个口径，两处显示不同 —— 这是刻意的，不是不一致。
    const g = logGroupOf(row("k_two"), KEYS, GROUPS);
    expect(logGroupCell(g).text).toBe("高速组、低价组");   // 列
    expect(g.bucketLabel).toBe(LOG_GROUP_MULTI);            // 节标题
  });

  it("四个口径的「文字+色调」组合也两两不同（只看文字不够）", () => {
    // 三个特殊桶同为 note 色调但文字不同；多分组是 group 色调、文字也不同。
    const combos = [cases.不限, cases.无密钥, cases.密钥已删除, cases.多分组]
      .map((c) => `${c.tone}|${c.text}`);
    expect(new Set(combos).size).toBe(4);
  });

  it("真实分组用 group 色调，特殊桶用 note 色调", () => {
    expect(cases.单组.tone).toBe("group");
    expect(cases.多分组.tone).toBe("group");
    expect(cases.不限.tone).toBe("note");
    expect(cases.无密钥.tone).toBe("note");
    expect(cases.密钥已删除.tone).toBe("note");
  });

  it("只有多分组带「(N 组)」标记", () => {
    expect(cases.多分组.badge).toBe("(2 组)");
    expect(cases.单组.badge).toBeNull();
    expect(cases.不限.badge).toBeNull();
    expect(cases.无密钥.badge).toBeNull();
    expect(cases.密钥已删除.badge).toBeNull();
  });

  it("多分组列里显示【全部真实组名】，不是桶名", () => {
    expect(cases.多分组.text).toBe("高速组、低价组");
  });
});

describe("logRowMatchesGroup 筛选", () => {
  it("all 恒命中", () => {
    expect(logRowMatchesGroup(row(null), "all", KEYS, GROUPS)).toBe(true);
  });

  it("多分组的行，绑了哪个组就该被哪个组筛出来（两个都命中）", () => {
    expect(logRowMatchesGroup(row("k_two"), "g1", KEYS, GROUPS)).toBe(true);
    expect(logRowMatchesGroup(row("k_two"), "g2", KEYS, GROUPS)).toBe(true);
  });

  it("具体组 id 只命中真正绑了它的行", () => {
    expect(logRowMatchesGroup(row("k_one"), "g1", KEYS, GROUPS)).toBe(true);
    expect(logRowMatchesGroup(row("k_one"), "g2", KEYS, GROUPS)).toBe(false);
    expect(logRowMatchesGroup(row("k_none"), "g1", KEYS, GROUPS)).toBe(false);
  });

  it("四个特殊桶按 bucket 命中，且互不串味", () => {
    expect(logRowMatchesGroup(row(null), LOG_GROUP_NOKEY, KEYS, GROUPS)).toBe(true);
    expect(logRowMatchesGroup(row(null), LOG_GROUP_MISSING, KEYS, GROUPS)).toBe(false);
    expect(logRowMatchesGroup(row("k_gone"), LOG_GROUP_MISSING, KEYS, GROUPS)).toBe(true);
    expect(logRowMatchesGroup(row("k_gone"), LOG_GROUP_NOKEY, KEYS, GROUPS)).toBe(false);
    expect(logRowMatchesGroup(row("k_none"), LOG_GROUP_NONE, KEYS, GROUPS)).toBe(true);
    expect(logRowMatchesGroup(row("k_two"), LOG_GROUP_MULTI, KEYS, GROUPS)).toBe(true);
    expect(logRowMatchesGroup(row("k_two"), LOG_GROUP_NONE, KEYS, GROUPS)).toBe(false);
    expect(logRowMatchesGroup(row("k_one"), LOG_GROUP_MULTI, KEYS, GROUPS)).toBe(false);
  });
});

describe("logGroupSections 分节", () => {
  it("组间按该组最新一条降序，组内时间倒序", () => {
    const rows = [
      row("k_one", { time: 100 }),
      row(null, { time: 500 }),
      row("k_one", { time: 300 }),
    ];
    const secs = logGroupSections(rows, KEYS, GROUPS);
    expect(secs.map((s) => s.bucket)).toEqual([LOG_GROUP_NOKEY, "g1"]);
    expect(secs[1].rows.map((r) => r.time)).toEqual([300, 100]);
  });

  it("每节带计数与 tokens/积分合计", () => {
    const rows = [
      row("k_one", { time: 10, tokens: 10, credit: 1 }),
      row("k_one", { time: 20, tokens: 20, credit: 2 }),
    ];
    const secs = logGroupSections(rows, KEYS, GROUPS);
    expect(secs).toHaveLength(1);
    expect(secs[0].rows).toHaveLength(2);
    expect(secs[0].tokens).toBe(30);
    expect(secs[0].credit).toBe(3);
  });

  it("空分组不渲染（只对传入的 rows 分组）", () => {
    const secs = logGroupSections([row("k_one")], KEYS, GROUPS);
    expect(secs).toHaveLength(1);
    expect(secs.map((s) => s.bucket)).not.toContain("g2");
    expect(logGroupSections([], KEYS, GROUPS)).toEqual([]);
  });

  it("多分组的行不重复计入各分组 —— 合计不会大于总数", () => {
    const rows = [row("k_two", { tokens: 5, credit: 1 })];
    const secs = logGroupSections(rows, KEYS, GROUPS);
    expect(secs).toHaveLength(1);
    expect(secs[0].bucket).toBe(LOG_GROUP_MULTI);
    const totalTokens = secs.reduce((s, x) => s + x.tokens, 0);
    expect(totalTokens).toBe(5);
  });
});

describe("logGroupOptions 下拉选项", () => {
  it("只列当前日志里真正出现过的分组", () => {
    const opts = logGroupOptions([row("k_one")], KEYS, GROUPS);
    expect(opts.map((o) => o.value)).toEqual(["g1"]);
    expect(opts.map((o) => o.label)).toEqual(["高速组"]);
  });

  it("没出现过的分组不列（选了会得到空表，会让用户以为坏了）", () => {
    const opts = logGroupOptions([row("k_one")], KEYS, GROUPS);
    expect(opts.map((o) => o.value)).not.toContain("g2");
  });

  it("真出现过的特殊桶才列，且排在真实分组之后", () => {
    const rows = [row("k_one"), row(null), row("k_gone"), row("k_two")];
    const vals = logGroupOptions(rows, KEYS, GROUPS).map((o) => o.value);
    expect(vals).toEqual(["g1", LOG_GROUP_NOKEY, LOG_GROUP_MISSING, LOG_GROUP_MULTI]);
    expect(vals).not.toContain(LOG_GROUP_NONE);
  });

  it("空日志 -> 空选项", () => {
    expect(logGroupOptions([], KEYS, GROUPS)).toEqual([]);
  });
});