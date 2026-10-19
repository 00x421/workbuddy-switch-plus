"""账号级流量整形：权重、并发上限、最小请求间隔。

**为什么需要这一层**

账号被封的直接诱因几乎都是「短时间内某个号被打了太多请求」。原来只有两个
粗糙的旋钮：池级的 `spread`（驻留几分钟内只有一个号在跑）和收到 401/429 之后
30 分钟冷却。前者是全池一刀切，后者是事后惩罚——收到 429 时上游已经记账了，
反复触发只会加重风控画像。

这一层做的是**事前**控制，三个互相独立、可单账号配置的维度：

  · `weight`   权重：调度时的相对概率。给稳的号高权重、给新号低权重养着，
               避免新号一上来就被打满。权重 0 = 只读备用，不参与自动轮转。
  · `max_concurrency` 同一时刻最多几个在途请求。超出就排队/换号，
               而不是硬顶上去。这是最容易触发风控的维度。
  · `min_interval` 两次请求之间的最小间隔（秒）。给调用频繁的场景兜底，
               保证单个号不会被连珠炮打。

**语义约定**

- 三项都为 0（或缺失）= 不限制，行为与改造前完全一致。这是升级兼容的前提：
  存量 state.json 里没有任何这些字段，必须照旧跑。
- 限制是**软约束**而非硬拒绝。当所有账号都被限住时，宁可挑一个「超出最少的」
  放过去，也不要报「暂无可用账号」——后者会让用户以为服务挂了，而实际上只是
  自己把自己限死了。
- 时钟一律从外部传入，不在这里读 `time.time()`，否则没法测。
"""
from __future__ import annotations

import math

# 三项限制的取值上界。设上界不是为了防攻击，是为了拦住填错的单位：
# 有人把 min_interval 按毫秒填成 3000（本意 3 秒），结果一个号 50 分钟只能用一次，
# 现象是「所有请求都在等」，很难往配置上想。
MAX_CONCURRENCY = 256
MAX_MIN_INTERVAL = 3600.0
MAX_WEIGHT = 1000


def _int(value, default=0):
    """把配置值拢成非负整数；非法值退回 default 而不是抛异常。

    配置是用户手填的，读配置不该让整个服务起不来。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, MAX_CONCURRENCY if default == 0 else 10 ** 9))


def limits_of(status):
    """从一个账号的 status 字典里读出三项限制。

    缺失 / 非法 / 负数一律当 0（不限制），这是最保守的退化方向：
    宁可少限也不要把号限死到不可用。
    """
    status = status or {}

    def num(key, cap, cast=int):
        raw = status.get(key)
        if raw in (None, ""):
            return 0
        try:
            val = cast(raw)
        except (TypeError, ValueError):
            return 0
        if val != val or val in (float("inf"), float("-inf")):  # NaN / inf
            return 0
        return max(0, min(val, cap))

    return {
        "weight": num("weight", MAX_WEIGHT),
        "max_concurrency": num("max_concurrency", MAX_CONCURRENCY),
        "min_interval": num("min_interval", MAX_MIN_INTERVAL, float),
    }


def effective_weight(status):
    """调度用的相对权重。

    没配（0 / 缺失）时返回 **1**，表示「与其他人同权」，而不是「权重 0 不许用」。
    这是最容易搞反的地方：`weight` 缺省必须是 1，显式填 0 才是「不参与轮转」。
    """
    raw = (status or {}).get("weight")
    if raw in (None, ""):
        return 1.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if val != val or val in (float("inf"), float("-inf")):
        return 1.0
    if val <= 0:
        return 0.0
    return min(val, MAX_WEIGHT)


def inflight(status):
    """当前在途请求数。由 `acquire` / `release` 维护，非负。"""
    try:
        return max(0, int((status or {}).get("inflight") or 0))
    except (TypeError, ValueError):
        return 0


def is_saturated(status):
    """并发是否已经顶满。未配并发上限时永远 False。"""
    lim = limits_of(status)["max_concurrency"]
    if lim <= 0:
        return False
    return inflight(status) >= lim


def interval_blocked(status, now):
    """最小间隔是否还没过。

    返回剩余秒数（float，>0 表示还要等），0 表示可以立刻发。
    `last_request` 缺失或比 now 还大（时钟回拨）时都当「可以发」——
    时钟回拨时如果按「还要等很久」处理，会让一个号永久卡死。
    """
    lim = limits_of(status)["min_interval"]
    if lim <= 0:
        return 0.0
    try:
        last = float((status or {}).get("last_request") or 0)
    except (TypeError, ValueError):
        return 0.0
    if last <= 0 or last > now:
        return 0.0
    remain = lim - (now - last)
    return remain if remain > 0 else 0.0


def penalty(status, now):
    """账号的「欠账」分数，越小越该被选中。

    组成：
      · 饱和 / 间隔未到 → 一个很大的常数，把它们排到最后
      · 在途请求数 → 越忙越靠后（并发上限没配时也做自然分摊）
      · 轮转序号 → 同权同忙时保持确定性顺序，避免随机抖动带来「同一会话
        时好时坏」的观感

    返回 (是否硬阻塞, 分数)。硬阻塞只用于「所有号都阻塞了」时的兜底判断。
    """
    lim = limits_of(status)
    busy = inflight(status)
    blocked = False
    score = 0.0
    if lim["max_concurrency"] > 0 and busy >= lim["max_concurrency"]:
        blocked = True
        score += 1e6
    wait = interval_blocked(status, now)
    if wait > 0:
        blocked = True
        # 等得越久排得越后，但同样都进 1e6 档，不与正常号混排。
        score += 5e5 + min(wait, 3600.0)
    score += busy * 100.0
    return blocked, score


def smooth_weights(items):
    """把 [(key, weight)] 压成 [(key, probability)]。

    权重全为 0（所有号都被显式设成不参与轮转）时退化成**均分**，
    而不是除零或返回空——这时候更可能的解释是用户想临时停用全部号的
    自动轮转，均分至少让服务还能跑，同时 `weight=0` 的语义在界面里
    已经写明「不参与自动轮转」，不是「禁止使用」。
    """
    pairs = [(k, max(0.0, float(w))) for k, w in items]
    total = sum(w for _, w in pairs)
    if total <= 0:
        n = len(pairs)
        return [(k, 1.0 / n) for k, _ in pairs] if n else []
    return [(k, w / total) for k, w in pairs]


def weighted_order(candidates, statuses, offset=0):
    """按权重展开成一个确定性的选择序列。

    做法是最大余数法的确定性版本：给每个账号分配 `round(weight/total * N)`
    个槽位，再把余下的槽位按权重小数部分从大到小补，最后按「交错」顺序排列。

    为什么不用 `random.choices`：调度必须可复现，否则测试没法断言，
    线上出问题也没法解释「为什么这次选了 3 号」。这里所有输入确定，
    输出就确定。
    """
    if not candidates:
        return []
    # 权重 0 的账号**完全不参与自动轮转**（这是 `weight=0` 的明确语义：
    # 只读备用，等人工切过去才用）。它们既不占槽位，也不该被塞进序列里。
    pairs = [(row["id"], effective_weight(statuses.get(row["id"]))) for row in candidates]
    pairs = [(aid, w) for aid, w in pairs if w > 0]
    if not pairs:
        # 一个都没配权重（或全被显式设成 0）时退化成均分，而不是让调度空转。
        # 全 0 更可能是「用户想临时停掉自动轮转」，此时仍要能跑，
        # 否则所有请求会一股脑落到「所有号都被卡住」的兜底分支上。
        pairs = [(row["id"], 1.0) for row in candidates]
    n = len(pairs)
    total = sum(w for _, w in pairs)
    if total <= 0:
        # 全部 weight=0：均分槽位。
        quotas = [1] * n
    else:
        exact = [w / total * n for _, w in pairs]
        quotas = [int(math.floor(v)) for v in exact]
        short = n - sum(quotas)
        if short > 0:
            order = sorted(range(n), key=lambda i: (-(exact[i] - quotas[i]), pairs[i][0]))
            for i in order[:short]:
                quotas[i] += 1
        # 每个账号至少留一个槽位。
        #
        # 纯按权重分配会出现「权重 3 / 1 / 1，三个号只有一个槽位给 a」这种情况，
        # 权重 1 的号在一个完整轮转里完全排不上，长时间下去它的 token 会过期、
        # 保活会断，等于被悄悄淘汰。账号池的价值在于「都在可用状态」，
        # 权重只该决定**频率**，不该决定**生死**。
        # 注意这里只给 weight>0 的号补位——weight=0 是「不参与」而非「排不上」。
        for i in range(n):
            if quotas[i] <= 0:
                quotas[i] = 1
    seq = []
    # 轮转长度 = 槽位总和，而不是账号个数：权重 3 的号要在一轮里出现 3 次。
    live = [i for i in range(n) if quotas[i] > 0]
    while live:
        for i in live[:]:
            if quotas[i] > 0:
                quotas[i] -= 1
                seq.append(pairs[i][0])
                if quotas[i] == 0:
                    live.remove(i)
    if not seq:
        return []
    shift = int(offset) % len(seq)
    return seq[shift:] + seq[:shift]


# ── 池级总量闸门 ──────────────────────────────────────────────────────────
#
# 上面三个维度都是**单账号**的。它们治不了另一类事故：
#
#   池子里 20 个号，每个号 max_concurrency=3（看起来已经很保守），
#   同时涌入 60 个请求 —— 每个号都被打满 3 个。从单个号看完全合规，
#   但从上游风控看，这 60 个请求分属 20 个「同一台机器同一时刻忽然活跃」
#   的账号，很容易被一起判定成同一来源的批量行为。
#
# 所以需要一个**全池共享**的门槛：不管有多少号、每个号配几，同一时刻
# 打到上游的请求总数不超过 N。默认 0 = 不限制，与改造前完全一致。
#
# 注意这不是账号健康阈值（那种「所有号都被限住」的兜底仍由 select 处理），
# 而是吞吐上限：它会让请求**排队**，不会让它们失败。


def pool_gate(pool_cfg):
    """从池设置里读全局并发上限。返回 0 表示不限制。

    取值上限和账号级共用 MAX_CONCURRENCY——理由相同：拦住单位填错
    （把 300 当「300 毫秒」这类），上界不存在时一个笔误能让服务彻底停摆。
    """
    if not isinstance(pool_cfg, dict):
        return 0
    raw = pool_cfg.get("max_total_concurrency")
    if raw in (None, ""):
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, MAX_CONCURRENCY))


def total_inflight(statuses):
    """全池在途请求总数。

    `statuses` 是 `store.data["account_status"]` 的值：`{aid: {...}}`。
    只看活跃的 key，被隐藏/删除的账号不该继续占着名额。
    """
    if not isinstance(statuses, dict):
        return 0
    total = 0
    for status in statuses.values():
        total += inflight(status)
    return total

