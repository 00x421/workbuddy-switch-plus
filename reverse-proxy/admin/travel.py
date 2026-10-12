"""派猫猫旅行（成长中心的 Buddy 旅行）：状态机、接口封装、派发与领奖。

协议对照桌面端 `crates/wb-switch-core/src/modules/travel.rs` —— 同一份实现的
另一侧：那边跑在桌面端的账号库上，这边跑在反代的账号池上。两处口径必须一致，
否则同一个账号在桌面端显示「已在旅行中」、在反代这边却报了别的错。

状态机以服务端的 `data.state` 为准：

    idle --(depart)--> traveling --(到点)--> arrived --(claim)--> idle

官网的 `idle` + `daily_limit_reached` 是「累了，明天再来吧」，按当日已结束处理，
不再按 traveling 倒计时，也不再重试。

主机名不要改：成长中心挂在 `www.codebuddy.cn`，不是 `www.workbuddy.cn`。
这个值和桌面端 config.rs 的 `WORKBUDDY_API_ENDPOINT` 一致，现在统一放在
`admin/upstream.py` —— 同域的活跃上报（`report.py`）也用同一份，两处不会再漂移。
"""

from __future__ import annotations

import time

from .upstream import BASE, MESSAGE_LIMIT  # noqa: F401  主机与截断长度：单一来源
from .upstream import envelope as _envelope  # noqa: F401  测试与同族模块沿用的旧名
from .upstream import is_unauthorized, message_of, ok, request

# 成长中心（含派猫猫旅行）所在主机 —— 由 admin/upstream.py 统一给出。
# 旅行接口前缀（桌面端 config.rs 的 TRAVEL_API_PREFIX）。
PREFIX = "/activity/growth/buddy/travel"
# 旅行卡片在官网的位置，web 域请求要带上它做 Referer。
GROWTH_PAGE = BASE + "/profile/growth-center"

# 服务端唯一认的三个状态。缺 state / 未知 state 一律当查询失败——
# 不能回落到 idle，否则「查不到状态」会被当成「还没派」，进而重复派发。
KNOWN_STATES = ("idle", "traveling", "arrived")

# 可重试的跳过原因：当日不算完成，下一轮继续试。
# 反过来 no-location / daily-limit 是「今天到此为止」，记下就不再重试——
# 否则每轮 tick 都会对着同一个必然失败的原因反复打上游请求。
RETRYABLE_SKIPS = frozenset({
    "no-buddy", "error", "config-error",
    "no-location", "location-unavailable", "status-error",
})

# TravelRunner 对 ctx（账号池）的成员约定。写在这里是为了能被断言——
# 假 ctx 和真账号池各写各的，名字对不上时（曾经就把 `refresh_account` 写成了
# `refresh`）单测全绿、一到真环境就 500：假 ctx 是照这份约定写的，真适配器
# 却没人对照过。tests/test_travel.py 会拿真 AccountPool 逐条核对。
CTX_PROTOCOL = ("client_for", "headers_for", "refresh_account")


def is_retryable_skip(skip):
    return (skip or "") in RETRYABLE_SKIPS


def _web_headers(headers):
    """在账号池给的鉴权头上补 web 域的三个字段。

    桌面端的 `build_travel_headers` 就是「build_auth_headers + 这三样」，
    少一个 `x-client-platform: web` 服务端会按桌面口径下发，成长中心的
    web 专属判据就点不亮。
    """
    out = dict(headers or {})
    out["x-client-platform"] = "web"
    out["Origin"] = BASE
    out["Referer"] = GROWTH_PAGE
    return out


def _send(client, method, path, headers, body=None):
    return request(client, BASE + path, method, headers, body)


def travel_config(client, headers):
    """读旅行配置：`{ok, enabled, locations: [{id, name}], error}`。"""
    env = _send(client, "GET", PREFIX + "/config", _web_headers(headers))
    if not ok(env):
        return {"ok": False, "error": message_of(env, "读取旅行配置失败")}
    data = env.get("data") or {}
    locations = [{"id": loc.get("id"), "name": str(loc.get("name") or "")}
                 for loc in (data.get("locations") or []) if isinstance(loc, dict)]
    enabled = data.get("enabled", True)
    return {"ok": True, "enabled": bool(enabled) and bool(locations), "locations": locations}


def _status_fields(data):
    """把服务端原始 `data` 归一化成统一形状。

    depart / sync / travel_status 三处读的是同一批字段，各写一遍必然漂移——
    尤其是 `arrive_at` → `arriveAt` 这种改名：漏一处的表现是「到点了但领奖
    对账永远看不到」，而且完全静默。
    """
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    return {
        "state": str(data.get("state") or "").strip().lower(),
        "locationId": location.get("id"),
        "locationName": str(location.get("name") or ""),
        "departAt": _int(data.get("depart_at")),
        "arriveAt": _int(data.get("arrive_at")),
        "dailyLimitReached": bool(data.get("daily_limit_reached")),
        "buddyId": _int(data.get("buddy_id")),
        "recordId": _int(data.get("record_id")),
        "serverNow": _int(data.get("server_now")),
        "rewardCredit": data.get("reward_credit"),
    }


def travel_status(client, headers):
    """读当前旅行状态：`{ok, state, ...}`，失败时 `error` 给原因。"""
    env = _send(client, "GET", PREFIX + "/status", _web_headers(headers))
    if not ok(env):
        return {"ok": False, "error": message_of(env, "查询旅行状态失败")}
    fields = _status_fields(env.get("data") or {})
    if fields["state"] not in KNOWN_STATES:
        return {"ok": False,
                "error": f"unknown travel state: {fields['state']}"
                         if fields["state"] else "missing travel state"}
    return {"ok": True, **fields}


def travel_depart(client, headers, location_id):
    """派发一次（POST depart）。返回 `{ok, state}` 或 `{ok: False, code, message}`。"""
    env = _send(client, "POST", PREFIX + "/depart",
                _web_headers(headers), {"location_id": location_id})
    if ok(env):
        data = env.get("data") or {}
        return {"ok": True, "state": str(data.get("state") or "traveling")}
    return {"ok": False, "code": env.get("code"), "message": message_of(env, "派发失败")}


def travel_claim(client, headers, record_id):
    """领奖（POST claim）。`record_id` 拿不到时不带 body，服务端按当前行程判定。"""
    body = {"record_id": record_id} if _int(record_id) > 0 else {}
    env = _send(client, "POST", PREFIX + "/claim", _web_headers(headers), body)
    if ok(env):
        data = env.get("data") or {}
        return {"ok": True, "rewardCredit": data.get("reward_credit")}
    return {"ok": False, "code": env.get("code"), "message": message_of(env, "领奖失败")}


def decide_action(state, daily_limit_reached):
    """由服务端状态决定这一轮该做什么。

    `idle` + 已达当日上限 → "skip-daily"（当日结束），**不是** "depart"：
    把这条单独拆出来，是因为混淆这两者会让调度每轮都白打一次上游。
    """
    if state == "arrived":
        return "claim"
    if state == "traveling":
        return "wait"
    if state == "idle":
        return "skip-daily" if daily_limit_reached else "depart"
    return "error"


def classify_depart_error(code, message):
    """派发失败归类。只看文案——HTTP 429 是限流，不是「今日已派」。

    这条区分是有代价换来的：把限流当成当日完成，会白白丢掉一整天的旅行积分。
    """
    text = str(message or "").lower()
    if "already traveling" in text:
        return "already_traveling"
    if "daily limit" in text or "daily_limit" in text:
        return "daily_limit"
    if "no active buddy" in text:
        return "no_buddy"
    if "location not available" in text:
        return "location_unavailable"
    return "other"


def arrive_at_secs(arrive_at):
    """到达时间归一化到秒。上游有 ms / s 两种形态，写死一种会算错一小时以上。"""
    value = _int(arrive_at)
    return value // 1000 if value > 1_000_000_000_000 else value


def in_flight_due(entry, now=None):
    """旅行中且已过到达时间（或缓存里没有到达时间）→ 该回查一次官方状态。"""
    if not result_in_flight(entry):
        return False
    now = int(now if now is not None else time.time())
    arrive = arrive_at_secs((entry or {}).get("arriveAt"))
    return arrive <= 0 or arrive <= now


def result_in_flight(entry):
    entry = entry or {}
    return bool(entry.get("ok")) and not bool(entry.get("claimed"))


def result_claimed(entry):
    return bool((entry or {}).get("claimed"))


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class TravelRunner:
    """把「一个账号跑一轮旅行」跑完，只依赖三件事的适配层。

    需要 ctx 提供：

      · ``client_for(aid)`` —— 上下文管理器，产出该账号的 httpx 客户端
        （由账号池带上它的出口线路，见 admin/egress.py）
      · ``headers_for(aid)`` —— 该账号当前的鉴权头（过期会自动刷新）
      · ``refresh_account(aid)`` —— 强制刷新一次凭据；**返回 bool**，True 表示确实
        换了新凭据，False 表示没得刷（比如没有 refresh token）。返回数值型真假会让
        「刷了个空」被当成刷成功，然后拿同一个死 token 重试第二遍。

    这三条的实际名字以 `CTX_PROTOCOL` 为准，改动时同步改那份元组，
    否则测试里的假 ctx 会和真账号池悄悄分叉。

    只管发请求、判状态、回结果；「记哪条日志、存什么缓存」由账号池决定——
    这样单测里塞一个假 ctx 就能把状态机整条链路跑完，不必起 HTTP。
    """

    def __init__(self, ctx, clock=time.time):
        self.ctx = ctx
        self.clock = clock

    # ── 请求层 ──────────────────────────────────────────────────────────
    def _call(self, aid, method, path, body=None):
        with self.ctx.client_for(aid) as client:
            env = _send(client, method, path, _web_headers(self.ctx.headers_for(aid)), body)
            if is_unauthorized(env) and self.ctx.refresh_account(aid):
                env = _send(client, method, path,
                            _web_headers(self.ctx.headers_for(aid)), body)
        return env

    def _get(self, aid, path):
        return self._call(aid, "GET", path)

    def _post(self, aid, path, body):
        return self._call(aid, "POST", path, body)

    # ── 对外：派发 ──────────────────────────────────────────────────────
    def depart(self, aid):
        """跑一轮派发：读配置 → 依次试地点 → 回读一次状态补齐 record_id/到达时间。"""
        env = self._get(aid, PREFIX + "/config")
        if not ok(env):
            return _result(aid, skip="config-error",
                           message=message_of(env, "读取旅行配置失败"))
        data = env.get("data") or {}
        locations = [loc for loc in (data.get("locations") or []) if isinstance(loc, dict)]
        if not data.get("enabled", True) or not locations:
            return _result(aid, skip="no-location", message="无旅行地点")

        unavailable = False
        for location in locations:
            env = self._post(aid, PREFIX + "/depart", {"location_id": location.get("id")})
            if ok(env):
                state = str((env.get("data") or {}).get("state") or "traveling")
                out = _result(aid, ok=True, state=state)
                name = str(location.get("name") or "").strip()
                if name:
                    out["locationName"] = name
                status = self._get(aid, PREFIX + "/status")
                if ok(status):
                    _apply_status(out, _status_fields(status.get("data") or {}))
                return out

            cls = classify_depart_error(env.get("code"), env.get("message"))
            text = message_of(env, "派发失败")
            if cls == "already_traveling":
                return _result(aid, ok=True, already=True, state="traveling",
                               message="已在旅行中")
            if cls == "daily_limit":
                out = _result(aid, ok=True, already=True, skip="daily-limit",
                              state="idle", message="今日已派")
                out["claimed"] = True
                out["claimedAt"] = int(self.clock() * 1000)
                return out
            if cls == "no_buddy":
                return _result(aid, skip="no-buddy", message="无 Buddy")
            if cls == "location_unavailable":
                unavailable = True
                continue
            return _result(aid, skip="error", message=text[:80])

        if unavailable:
            return _result(aid, skip="location-unavailable", message="地点不可用")
        return _result(aid, skip="error", message="派发失败")

    # ── 对外：对账 / 领奖 ───────────────────────────────────────────────
    def sync(self, aid, prior=None):
        """按官方状态对一次账：traveling 等着，arrived 领奖，idle 收尾。

        Arrived 才领奖；`idle` 且已达上限记成当日结束。这两条都是「服务端说的
        算」——本地缓存的倒计时只用来决定**什么时候回查**，不用来决定领不领。
        """
        result = dict(prior or {})
        env = self._get(aid, PREFIX + "/status")
        if not ok(env):
            result["skip"] = "status-error"
            result["message"] = message_of(env, "查询旅行状态失败")
            return result

        fields = _status_fields(env.get("data") or {})
        raw = fields["state"]
        record_id = fields["recordId"]

        if raw == "traveling":
            result["ok"] = True
            result["claimed"] = False
            result["state"] = "traveling"
            _apply_status(result, fields)
        elif raw == "arrived":
            result["state"] = "arrived"
            result["ok"] = True
            _apply_status(result, fields)
            self._claim(aid, result, record_id)
        elif raw == "idle":
            if fields["dailyLimitReached"]:
                _apply_daily_limit(result, fields, int(self.clock() * 1000))
            elif result_claimed(result):
                result["state"] = "idle"
            elif result.get("ok"):
                self._claim(aid, result, record_id)
            else:
                result["claimed"] = False
                result["state"] = "idle"
        else:
            result["skip"] = "status-error"
            result["claimed"] = False
            result["message"] = f"unknown travel state: {raw}" if raw else "missing travel state"
        return result

    def _claim(self, aid, result, record_id):
        env = self._post(aid, PREFIX + "/claim",
                         {"record_id": record_id} if record_id > 0 else {})
        if not ok(env):
            result["ok"] = False
            result["claimed"] = False
            result["skip"] = "error"
            result["message"] = message_of(env, "领奖失败")
            return
        data = env.get("data") or {}
        result["ok"] = True
        result["claimed"] = True
        result["claimedAt"] = int(self.clock() * 1000)
        result["state"] = "idle"
        result["message"] = "已领取旅行奖励"
        if data.get("reward_credit") is not None:
            result["rewardCredit"] = data.get("reward_credit")


def _result(aid, ok=False, already=False, skip=None, state=None, message="", **extra):
    out = {"id": aid, "ok": bool(ok), "already": bool(already), "skip": skip,
           "state": state, "message": message, "claimed": False}
    out.update(extra)
    return out


def _apply_status(result, status):
    """把 status 里的展示字段并进结果。只补非空值，别把已有的覆盖成空。"""
    name = str(status.get("locationName") or "").strip()
    if name:
        result["locationName"] = name
    credit = status.get("rewardCredit")
    if credit not in (None, 0, 0.0, ""):
        result["rewardCredit"] = credit
    arrive = _int(status.get("arriveAt"))
    if arrive > 0:
        result["arriveAt"] = arrive
    # 记下 record_id：领奖时优先用它，省掉一次「先查状态再领奖」的往返，
    # 也避免行程刚好在此期间翻页导致领错一笔。
    record = _int(status.get("recordId"))
    if record > 0:
        result["recordId"] = record


def _apply_daily_limit(result, status, now_ms):
    """官网 idle + daily_limit_reached：保留地点/积分，只把当日标记成已结束。"""
    result["ok"] = True
    result["already"] = True
    result["skip"] = "daily-limit"
    result["claimed"] = True
    result["state"] = "idle"
    result["message"] = "今日已派"
    if not _int(result.get("claimedAt")):
        result["claimedAt"] = now_ms
    _apply_status(result, status)
