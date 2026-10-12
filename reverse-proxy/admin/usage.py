"""调用用量聚合：按天 / 按小时分桶，落 `state.json` 的 `usage_stats`。

为什么要有它：`pool.request_log` 是一个 **deque，上限 500 条**，只够看"最近发生了什么"。
想知道"这周哪几个模型在烧分"、"昨天失败率多少"、"峰值小时是几点"就无从下手了。
这里补上聚合层——**不引入数据库**（本项目的既定决策），全部落在 `store.data`。

分桶键就是 ISO 前缀，**字典序 == 时间序**，所以裁剪时直接按字符串排序取尾部即可：

    daily  : "2026-10-11"
    hourly : "2026-10-11T23"

每个桶的形状（`by_model` / `by_key` / `by_status` 是同一套计数字典的嵌套）：

    {"calls", "failed", "tokens", "credit",
     "duration_ms", "first_token_ms",      # 累加值，均值由读取方自己除
     "by_model": {model: 计数}, "by_key": {kid: 计数}, "by_status": {status: n}}

时间基准用 `pool.clock()`（可注入的时钟），保证测试里能造出确定的时间轴。
"""
from __future__ import annotations

import datetime

# 保留窗口：日桶 60 天、时桶 72 小时。够看趋势，也不至于把 state.json 撑大。
DAILY_KEEP = 60
HOURLY_KEEP = 72

# 每个桶内 by_model / by_key / by_status 的上限：防止有人用随机模型名刷爆磁盘。
DIMENSION_KEEP = 200

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))


def default_usage_stats() -> dict:
    """`store.data["usage_stats"]` 的初始形状。"""
    return {"daily": {}, "hourly": {}}


def _empty_bucket() -> dict:
    return {
        "calls": 0,
        "failed": 0,
        "tokens": 0,
        "credit": 0.0,
        "duration_ms": 0,
        "first_token_ms": 0,
        "by_model": {},
        "by_key": {},
        "by_status": {},
    }


def bucket_keys(now: float) -> tuple[str, str]:
    """把时间戳换算成 (日桶键, 时桶键)。用东八区——本项目的日志/签到都以它为准。"""
    dt = datetime.datetime.fromtimestamp(int(now), CN_TZ)
    day = dt.strftime("%Y-%m-%d")
    return day, f"{day}T{dt.strftime('%H')}"


def _bump(counter: dict, name, calls: int, failed: int, tokens: int, credit: float) -> None:
    if not name:
        return
    name = str(name)[:64]
    slot = counter.get(name)
    if slot is None:
        if len(counter) >= DIMENSION_KEEP:
            return  # 维度已经够散了，丢弃新维度比无限膨胀好
        slot = counter[name] = {"calls": 0, "failed": 0, "tokens": 0, "credit": 0.0}
    slot["calls"] += calls
    slot["failed"] += failed
    slot["tokens"] += tokens
    slot["credit"] = round(slot["credit"] + credit, 6)


def _add(bucket: dict, *, calls: int, failed: int, tokens: int, credit: float,
         duration_ms: int, first_token_ms: int, model=None, key_id=None, status=None) -> None:
    bucket["calls"] += calls
    bucket["failed"] += failed
    bucket["tokens"] += tokens
    bucket["credit"] = round(bucket["credit"] + credit, 6)
    bucket["duration_ms"] += int(duration_ms or 0)
    bucket["first_token_ms"] += int(first_token_ms or 0)
    _bump(bucket["by_model"], model, calls, failed, tokens, credit)
    _bump(bucket["by_key"], key_id, calls, failed, tokens, credit)

    name = str(status)
    slot = bucket["by_status"].get(name)
    if slot is None:
        if len(bucket["by_status"]) < DIMENSION_KEEP:
            bucket["by_status"][name] = 1
    else:
        bucket["by_status"][name] = slot + 1


def _prune(buckets: dict, keep: int) -> None:
    """按字典序（== 时间序）裁剪，只留最新 keep 个桶。

    必须先取好待删清单、再用 pop(key, None) 删。

    原先写的是：
        for key in sorted(buckets)[: len(buckets) - keep]:
            del buckets[key]
    这是个经典的 check-then-act：`sorted()` 算出的名单在真正删除之前就可能过期。
    两个线程同时进来会算出**同一份**要删的名单，先删的那个成功，
    后删的 `del` 直接抛 KeyError —— 那是未捕获异常，会把整个记账调用打断。

    当前调用点在 store.lock 里，所以并发写这条路上暂时撞不到；
    但 `snapshot()` 是**放开锁之后**才去遍历这些嵌套桶的，
    而且「谁该持锁」这种约定不该由被调用方来假设。
    pop 带默认值是一次原子的存在性判断 + 删除，天然免疫这个竞态。
    """
    if len(buckets) <= keep:
        return
    for key in sorted(buckets)[: len(buckets) - keep]:
        buckets.pop(key, None)


def record(store, *, now: float, status: int, tokens: int = 0, credit: float = 0.0,
           duration_ms: int = 0, first_token_ms: int = 0, model=None, key_id=None) -> None:
    """记一次调用。**调用方负责持锁 / 落盘**（这里只改内存，不做 I/O）。

    与 `pool.log_request` 是同一次调用的两条记录：那边留"最近 500 条的明细"，
    这边留"长期的分桶计数"。
    """
    # status 由上游 HTTP 响应决定，正常情况下是 int；但记账是旁路，
    # 不该因为一个脏值就把整条记账链路打断（那会让用量统计整段缺失）。
    # 读不出数字就归成 0：既不进 2xx，也不假装成功。
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = 0
    ok = 200 <= code < 300
    calls, failed = 1, 0 if ok else 1
    tokens = max(0, int(tokens or 0))
    credit = round(max(0.0, float(credit or 0.0)), 6)

    stats = store.data.setdefault("usage_stats", default_usage_stats())
    stats.setdefault("daily", {})
    stats.setdefault("hourly", {})

    day, hour = bucket_keys(now)
    for buckets, key, keep in ((stats["daily"], day, DAILY_KEEP), (stats["hourly"], hour, HOURLY_KEEP)):
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = _empty_bucket()
        _add(bucket, calls=calls, failed=failed, tokens=tokens, credit=credit,
             duration_ms=duration_ms, first_token_ms=first_token_ms,
             model=model, key_id=key_id, status=code)
        _prune(buckets, keep)


def _with_averages(bucket: dict) -> dict:
    calls = int(bucket.get("calls", 0))
    tokens = int(bucket.get("tokens", 0))
    duration = int(bucket.get("duration_ms", 0))
    first = int(bucket.get("first_token_ms", 0))
    failed = int(bucket.get("failed", 0))
    return {
        **bucket,
        "tokens": tokens,
        "credit": round(float(bucket.get("credit", 0.0)), 6),
        "failed": failed,
        "success": calls - failed,
        "failure_rate": round(failed / calls, 4) if calls else 0.0,
        "tokens_avg": round(tokens / calls, 1) if calls else 0.0,
        "duration_ms_avg": round(duration / calls, 1) if calls else 0.0,
        # 只对成功的请求算首字延迟均值：失败请求根本没有首字
        "first_token_ms_avg": round(first / max(calls - failed, 1), 1),
        "duration_ms": duration,
        "first_token_ms": first,
    }


def snapshot(store, *, days: int = 30, hours: int = 24) -> dict:
    """给管理端读的聚合视图：按天一段、按小时一段，各自带总量与均值。

    只读，不查上游、不产生任何调用。
    """
    with store.lock:
        stats = store.data.get("usage_stats") or default_usage_stats()
        daily = dict(stats.get("daily") or {})
        hourly = dict(stats.get("hourly") or {})

    days = max(1, min(DAILY_KEEP, int(days or 30)))
    hours = max(1, min(HOURLY_KEEP, int(hours or 24)))

    daily_rows = [{"date": k, **_with_averages(daily[k])} for k in sorted(daily)[-days:]]
    hourly_rows = [{"hour": k, **_with_averages(hourly[k])} for k in sorted(hourly)[-hours:]]

    def total(rows: list[dict]) -> dict:
        calls = sum(r["calls"] for r in rows)
        failed = sum(r["failed"] for r in rows)
        tokens = sum(r["tokens"] for r in rows)
        credit = round(sum(r["credit"] for r in rows), 6)
        duration = sum(r["duration_ms"] for r in rows)
        return {
            "calls": calls, "failed": failed, "success": calls - failed,
            "failure_rate": round(failed / calls, 4) if calls else 0.0,
            "tokens": tokens, "credit": credit,
            "tokens_avg": round(tokens / calls, 1) if calls else 0.0,
            "duration_ms_avg": round(duration / calls, 1) if calls else 0.0,
        }

    return {
        "daily": daily_rows,
        "hourly": hourly_rows,
        "totals": {"daily": total(daily_rows), "hourly": total(hourly_rows)},
        "window": {"days": days, "hours": hours,
                   "daily_keep": DAILY_KEEP, "hourly_keep": HOURLY_KEEP},
    }
