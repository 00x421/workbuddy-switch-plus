"""对话活跃上报：`POST {BASE}/v2/report`。

对照来源是上游 `workbuddy2api` 的 `internal/upstream/report.go`（142 行，含实测
结论）。要点如下，每一条都对应一个「错了就静默失效」的坑：

  · **body 是数组**，一条上报 = 数组里一个 `chat_request_send` 事件。上游是
    每条一个请求（不是一次塞 N 条），N 条之间隔 1.5 秒。
  · **事件必须是完整字段形状**，不能用「只发 3 个必要字段」的最小体：
    官方客户端就发这一整套，精简版会被上游后续加严挡掉。
  · **`userId` 必填**（= 账号 uid）。缺了它服务端照样回 200，但把事件**静默丢弃**
    ——这是这个接口最阴的地方：HTTP 200 不等于上报生效。所以发完之后要回读
    `/activity/growth/streak` 自检（`streak()`）。
  · **N 条共用同一个 `conversationId`**，`requestId` 各自独立——模拟同一会话内
    N 轮对话。领养猫的对话量门槛（chat_5 前置要 5 次对话）就是靠这个补满的。
  · 风控口径：**每号每天一轮即可**，不做多时点高频上报。

一条上报同时点亮 growth 连登 + 解锁 first_buddy 前置。注意它和旅行是**两个域**：
travel 走 growth（带 web 三头），report 走 billing（就是签到用的那套头），
所以这里复用账号池给的鉴权头，不额外加 Origin/Referer。
"""

from __future__ import annotations

import time

from .upstream import BASE, message_of, ok, request

REPORT_PATH = "/v2/report"
# 连登天数（只读 oracle）：上报之后回读它，才能发现「200 但静默丢弃」。
STREAK_PATH = "/activity/growth/streak"

# 每天每号发几条。默认 1 条（点亮连登 + 解锁领养前置）；
# 领养猫要求 5 次对话，上限取 5。
DEFAULT_REPORTS = 1
MAX_REPORTS = 5
MIN_REPORTS = 1

# 同一账号内两条之间的间隔。5 连发是在模拟同一会话的多轮对话，
# 连发会触发上游风控。
REPORT_GAP_SECONDS = 1.5

# 事件里的固定字段：照抄官方客户端 chat_request_send 的形状。
# 模型名等不要改——它模拟的是官方客户端事件，改成别的值上游可能不认。
EVENT_CODE = "chat_request_send"
MODE = "craft"
INPUT_LENGTH = 12
REQUEST_MODEL_ID = "deepseek-v4-flash"
REQUEST_MODEL_NAME = "DeepSeek V4 Flash"
AGENT_NAME = "default"
AGENT_TYPE = "conversation"

# conversationId 前缀。上游用 `wb2api-<ms>`；这边换个前缀便于日志里区分来源。
# 服务端不校验这个值的一致性，只是需要一个「同会话」的标识。
CONVERSATION_PREFIX = "wb-switch"


def conversation_id(now_ms=None):
    """本轮上报共用的会话标识。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    return f"{CONVERSATION_PREFIX}-{now_ms}"


def chat_event(uid, cid, rid, now_ms=None):
    """一条 `chat_request_send` 事件（字段与官方客户端对齐，勿精简）。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    return {
        "eventCode": EVENT_CODE,
        "timestamp": now_ms,
        "reportDelay": 0,
        "mode": MODE,
        "conversationId": cid,
        "requestId": rid,
        "inputLength": INPUT_LENGTH,
        "requestModelId": REQUEST_MODEL_ID,
        "requestModelName": REQUEST_MODEL_NAME,
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now_ms,
        "traceId": "",
        "rootRequestId": cid,
        "parentConversationId": cid,
        "agentName": AGENT_NAME,
        "agentType": AGENT_TYPE,
        # 必填。缺了它服务端 200 但静默丢弃，所以由调用方从鉴权头里取，
        # 保证 body 里的 userId 和请求头里的 X-User-Id 永远是同一个值。
        "userId": str(uid or ""),
    }


def report_events(uid, cid, count, now_ms=None):
    """本轮要发的 N 条事件：同 cid，requestId 各自独立。"""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    return [chat_event(uid, cid, f"{cid}-r{i}", now_ms)
            for i in range(1, clamp_count(count) + 1)]


def clamp_count(count):
    """条数落在 [1, 5]。超出不报错、直接夹住——上游口径就是这个区间。"""
    try:
        value = int(count)
    except (TypeError, ValueError):
        value = DEFAULT_REPORTS
    return max(MIN_REPORTS, min(MAX_REPORTS, value))


def uid_of(headers):
    """从鉴权头取 uid（大小写都认，httpx 与 dict 两种来源都可能是小写）。"""
    for key, value in (headers or {}).items():
        if str(key).lower() == "x-user-id":
            return str(value or "")
    return ""


def send_one(client, headers, event):
    """发一条上报（body 是只含一条事件的数组）。"""
    return request(client, BASE + REPORT_PATH, "POST", headers, [event])


def report(client, headers, count=DEFAULT_REPORTS, now_ms=None,
                 sleep=time.sleep, gap=REPORT_GAP_SECONDS):
    """跑一轮上报，返回 `{ok, sent, count, conversationId, message}`。

    逐条发、逐条判：某一条失败就停在本号，不把剩下的续发出去——续发既没必要
    （服务端按天幂等），还会在已经出问题的号上多打几次。

    条与条之间的节流必须在请求之前：`sleep` 放在 `send_one` 之后等于没节流，
    而它防的是「同一账号秒发 N 条」这条风控信号，位置错了就等于没写。
    """
    uid = uid_of(headers)
    if not uid:
        # 没有 uid 一定会被静默丢弃，连请求都不用发。
        return {"ok": False, "sent": 0, "count": 0, "conversationId": "",
                "skip": "no-uid", "message": "凭据里没有账号标识（uid），已跳过"}

    cid = conversation_id(now_ms)
    events = report_events(uid, cid, count, now_ms)
    sent = 0
    for index, event in enumerate(events):
        if index:
            sleep(gap)
        env = send_one(client, headers, event)
        if not ok(env):
            return {"ok": False, "sent": sent, "count": len(events), "conversationId": cid,
                    "message": message_of(env, "活跃上报失败")}
        sent += 1
    return {"ok": True, "sent": sent, "count": len(events), "conversationId": cid,
            "message": f"活跃上报完成（{sent} 条）"}


def streak(client, headers):
    """回读连登天数：`{ok, days, message}`。

    这就是「上报 200 但静默丢弃」的探针——上报成功而 days 仍是 0，说明事件被
    丢了（最常见的原因就是 userId 缺失或不对）。

    解析必须容错。 这个函数是探针，它读不出来只说明「探不了」，绝不能
    反过来把已经成功的上报判成失败：早先这里直接 `int(block.get("days"))`，
    上游回了个非数字（网关错误页、字段类型变了）就抛 ValueError，一路被
    `_active` 的 except 接住 → 报「活跃上报失败」、不写 active_date、
    下一个 tick 白打一轮。事件其实好好地报上去了。
    读不出来时返回 `ok=False`（而不是 days=0）：调用方据此区分
    「探针坏了」与「读到了 0 = 事件真被丢了」。
    """
    env = request(client, BASE + STREAK_PATH, "GET", headers)
    if not ok(env):
        return {"ok": False, "days": 0, "message": message_of(env, "连登状态查询失败")}
    data = env.get("data") or {}
    block = data.get("streak") if isinstance(data.get("streak"), dict) else {}
    raw = block.get("days")
    try:
        days = int(raw or 0)
    except (TypeError, ValueError):
        return {"ok": False, "days": 0,
                "message": "连登天数格式异常，本次不做判断"}
    return {"ok": True, "days": days, "message": ""}
