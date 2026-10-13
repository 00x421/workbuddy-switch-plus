import { describe, expect, it } from "vitest";

import { startOfTodaySeconds, todayCreditTotal } from "./request-log-today";

/** 造一个本地时间，避免测试结果随时区漂移（用的是本地 getter）。 */
function local(y: number, mo: number, d: number, h = 0, mi = 0, s = 0): Date {
  return new Date(y, mo - 1, d, h, mi, s);
}

const sec = (d: Date) => Math.floor(d.getTime() / 1000);

describe("startOfTodaySeconds 取本地零点", () => {
  it("返回当天 00:00:00 的秒级时间戳", () => {
    const now = local(2026, 10, 13, 15, 42, 7);
    expect(startOfTodaySeconds(now)).toBe(sec(local(2026, 10, 13)));
  });

  it("零点整时就是它自己", () => {
    const now = local(2026, 10, 13, 0, 0, 0);
    expect(startOfTodaySeconds(now)).toBe(sec(now));
  });

  it("不修改传入的 Date（避免调用方被悄悄改掉）", () => {
    const now = local(2026, 10, 13, 15, 42, 7);
    const before = now.getTime();
    startOfTodaySeconds(now);
    expect(now.getTime()).toBe(before);
  });
});

describe("todayCreditTotal 只累计今天", () => {
  const now = local(2026, 10, 13, 15, 0, 0);
  const midnight = sec(local(2026, 10, 13));

  it("只算零点及之后的，昨天的一律不算", () => {
    expect(
      todayCreditTotal(
        [
          { time: midnight - 1, credit: 999 }, // 昨天 23:59:59
          { time: midnight, credit: 3 }, // 今天 00:00:00 —— 边界含在内
          { time: midnight + 60, credit: 4 },
        ],
        now,
      ),
    ).toBe(7);
  });

  it("跨零点前后各一笔：只取零点的这一笔", () => {
    const justBefore = midnight - 1;
    const justAfter = midnight;
    expect(todayCreditTotal([
      { time: justBefore, credit: 100 },
      { time: justAfter, credit: 1 },
    ], now)).toBe(1);
  });

  it("没有今天的记录就是 0", () => {
    expect(todayCreditTotal([{ time: midnight - 3600, credit: 50 }], now)).toBe(0);
  });

  it("空数组是 0", () => {
    expect(todayCreditTotal([], now)).toBe(0);
  });

  it("credit 缺失 / 为 0 不炸、按 0 计", () => {
    expect(
      todayCreditTotal(
        [
          { time: midnight + 1, credit: 0 },
          { time: midnight + 2, credit: undefined as unknown as number },
        ],
        now,
      ),
    ).toBe(0);
  });

  it("很多笔能正确累加（不走浮点误差明显的路径）", () => {
    const rows = Array.from({ length: 1000 }, (_, i) => ({ time: midnight + i, credit: 0.1 }));
    expect(todayCreditTotal(rows, now)).toBeCloseTo(100, 6);
  });
});
