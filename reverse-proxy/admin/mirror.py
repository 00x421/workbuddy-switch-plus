"""镜像加速：把"重试"和"退避"做成可配的，而不是每次靠人盯着重启。

为什么需要它：上游偶发的 5xx / 连接重置，在池里表现为"这个账号挂了"，
于是冷却、换号、告警一路触发。实际上绝大多数是几百毫秒内的瞬时故障，
重试一次就过去了。没有重试的代价是把噪声当故障处理。

三条硬纪律（都有测试锁定）：

1. **默认关闭**。打开重试会放大上游压力，配置项存在不等于应该打开。
2. **只重试幂等的、没有副作用的失败**。429/402 是配额，重试只会更快被限；
   401/403 是凭据问题，重试不会变得有权限。真正该重试的是 5xx 与网络层错误。
3. **退避必须有上限**。第一次失败立刻重试等于把瞬时故障原封不动复现一遍。
"""
from __future__ import annotations

import random

# 可配置区间。上限存在的理由：重试次数叠乘退避会变成"卡住不返回"，
# 用户看到的是超时，比直接报错更糟。
MAX_ATTEMPTS_CAP = 5
MAX_BACKOFF_CAP = 8.0

# 这些状态码重试有意义：上游自己出了问题，换一次可能就好。
# 4xx 里只有 408（请求超时）和 425（太早）算瞬时，429 显式排除——
# 它表达的是"你现在太快了"，加倍发只会更糟，那是限速该管的事。
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})


def settings(pool_cfg) -> dict:
    """从 pool 配置里读镜像加速的三项，全部带默认值与上限。"""
    cfg = pool_cfg if isinstance(pool_cfg, dict) else {}
    try:
        attempts = int(cfg.get("retry_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    try:
        backoff = float(cfg.get("retry_backoff") or 0.0)
    except (TypeError, ValueError):
        backoff = 0.0
    return {
        "enabled": bool(cfg.get("retry_enabled")),
        "attempts": max(0, min(attempts, MAX_ATTEMPTS_CAP)),
        "backoff": max(0.0, min(backoff, MAX_BACKOFF_CAP)),
    }


def should_retry(status, attempt, cfg) -> bool:
    """第 `attempt` 次（0 起）已经失败、要不要再来一次。

    `status is None` 表示连接层失败（DNS / 超时 / 重置）——那是重试最值钱的场景。
    """
    conf = settings(cfg)
    if not conf["enabled"] or conf["attempts"] <= 0:
        return False
    if attempt + 1 >= conf["attempts"]:
        return False
    if status is None:
        return True
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code in RETRYABLE_STATUS


def delay_ms(attempt, cfg, *, rng=None) -> int:
    """第 `attempt` 次失败后的等待毫秒数：指数退避 + 抖动。

    抖动的理由不是好看：多个账号同时被同一个上游故障打中时，
    固定退避会让它们在同一毫秒一起回到上游，把一次瞬时故障放大成一次雪崩。
    """
    conf = settings(cfg)
    if conf["backoff"] <= 0:
        return 0
    base = conf["backoff"] * (2 ** max(0, attempt))
    base = min(base, MAX_BACKOFF_CAP)
    jitter = (rng or random.random)() * base * 0.25
    return int(round((base + jitter) * 1000))
