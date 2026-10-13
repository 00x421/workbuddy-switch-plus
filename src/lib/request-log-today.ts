/**
 * 请求日志里「今日消耗积分」的统计口径。
 *
 * 单独放这里是为了能测：跨零点、时区、时间戳单位这些地方最容易悄悄算错，
 * 而在组件里没法直接测。
 */

/** 日志行的最小形状 —— 只取统计需要的两个字段。 */
export type CreditLogRow = { time: number; credit: number };

/**
 * 今天的本地零点（Unix 秒）。
 *
 * 用本地零点而不是 UTC 零点：用户看到的日期就是本地日期，
 * 用 UTC 会让「今天」在非 UTC 时区下偏移若干小时。
 */
export function startOfTodaySeconds(now: Date = new Date()): number {
  const start = new Date(now.getTime());
  start.setHours(0, 0, 0, 0);
  return Math.floor(start.getTime() / 1000);
}

/**
 * 累计 `rows` 中今天（本地零点起）的消耗积分。
 *
 * `time` 是 **Unix 秒**（与 `RpRequestLog.time` 一致，
 * 见 `fmtTime` 里的 `seconds * 1000`）。
 *
 * 刻意不接筛选结果：这张卡要回答「今天额度用了多少」，
 * 不该跟着账号/模型筛选变。
 */
export function todayCreditTotal(rows: readonly CreditLogRow[], now: Date = new Date()): number {
  const from = startOfTodaySeconds(now);
  let sum = 0;
  for (const row of rows) {
    if (row.time >= from) sum += row.credit || 0;
  }
  return sum;
}
