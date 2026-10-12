"""读取 workbuddy2api 的 `/status`，把**上游权威的模型级冷却**搬进我们的面板。

## 为什么要这个模块

我们自己的 `cooldown_until`（见 `pool.AccountPool.record`）是**根据错误码猜的**：
401/403 冷 5 分钟、402/429 冷 30 分钟，整个账号一个时间戳，**没有模型维度**。

而上游 workbuddy2api 维护的是一台真正的状态机：它知道**哪个模型**被限流、
各自几点恢复、以及原因（6004 是限流、等一会儿就好；11102 是这个号根本没这个模型、
等多久都不会好）。运维看到「冷却中」时，这两者的处置方式完全不同——
只看我们自己的估算，用户无从判断该等还是该换模型。

## 契约来源（照抄 Go 源码，别自己猜）

- `upstream/internal/server/handler.go` —— `GET /status`，`withAuth` 走
  `Authorization: Bearer <APIKey>`（APIKey 为空则**完全不鉴权**）。
  响应是 `{"accounts": [...], "total":…, "healthy":…, "cooling":…, …}`。
- `upstream/internal/pool/entry.go` —— 每个账号行的字段：
  `uid` / `cooling` / `cool_kind` / `cool_remaining_sec` / `until` / `reason` /
  `rate_limited_models[]` / `disabled` / `manual_disabled` / `consecutive_fails` /
  `degrade_until` / `success_count` / `in_flight` …
  `rate_limited_models` 每行是 `{model, until, reset_at, reason}`。

## 三个必须小心的点

1. **`omitempty` 会让键整个消失**，不是给零值。Go 的 `time.Time` 是值类型，
   不能 omitempty（会序列化成 `0001-01-01T00:00:00Z` 这种假真值，见上游
   `last_success` 的注释），所以 `until`/`reset_at` 缺键 = 没有，而
   `cool_remaining_sec` 缺键 = 0 秒（上游只在 >0 时写）。
2. **`until` 有两个含义**：账号级的 `until` 与模型级的 `until` 不等价——
   多模型同时被限流时，账号级 `until` 只是其中一个，必须逐模型看。
3. **对不上号就什么都别说**。上游的 `uid` 与我们的账号 id 未必同源
   （我们是 uid，但历史数据里可能有别名）。**匹配不上就不展示**，
   绝不要把别的账号的冷却安到这个账号头上——那比不显示更糟。

## 这个模块的边界

只读、无副作用、失败即静默降级（返回空映射），**绝不因为读不到上游而影响主流程**。
本机实测：workbuddy2api 未运行时 `7863` 直接拒绝连接，这种情况下
`fetch_status()` 抛 `Wb2ApiError`，由调用方决定怎么兜。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx

# 上游 /status 的默认地址。取自上游 manager 的 WB2API_BASE 默认值
# （.ref/.../server/config.py:38），保持一致，用户不用猜。
DEFAULT_BASE = "http://127.0.0.1:7863"
# 读上游状态是面板刷新路径上的事，不是请求路径。给短超时，
# 免得一个卡住的上游把我们的 overview 拖死。
TIMEOUT = 3.0


class Wb2ApiError(RuntimeError):
    """读上游状态失败（连不上 / 非 200 / 不是 JSON）。调用方应降级，不要抛给用户。"""


def _now() -> float:
    return time.time()


def _parse_time(value) -> float | None:
    """把上游的 RFC3339 时刻解析成 unix 秒。解析不了返回 None。

    上游用 Go 的 `encoding/json` 写 `time.Time`，格式是 RFC3339（带时区）。
    必须当真解析，不能用字符串比较或直接展示——上游可能给 `+08:00`
    也可能是 `Z`，字符串比大小是错的（这个坑本项目在 `_clock_hm` 上刚踩过一次）。
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    # Go 的 time.Time 零值（未设置却序列化出来）——这是假真值，必须当没有。
    if text.startswith("0001-01-01"):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _positive_int(value) -> int | None:
    """只接受 >0 的整数秒；其余（None / 0 / 负数 / 字符串）一律 None。

    上游 `cool_remaining_sec` 带 omitempty，**缺键 = 不冷却**，
    所以「没有值」和「值为 0」在这里是同一件事，都归 None。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number > 0 else None


def parse_status(payload) -> dict:
    """把上游 /status 的 JSON 解析成 `{uid: 冷却信息}`。

    返回的每个值形如::

        {
          "cooling": True,
          "cool_kind": "soft_rate",          # 上游的软/硬冷却分类，可能没有
          "cool_remaining_sec": 240,          # 仅当上游给了 >0 的值
          "until": 1791747520.5,              # 账号级截止（unix 秒），可能没有
          "reason": "429 rate limit",
          "disabled": False,
          "manual_disabled": False,
          "consecutive_fails": 0,
          "degrade_until": None,
          "rate_limited_models": [            # 只有真被限流的模型才在这里
            {"model": "glm-5.3", "until": 1791747000.0, "reset_at": 1791747000.0,
             "reason": "6004 model rate limit"},
          ],
        }

    容错口径（都是「宁可少说，不可说错」）：
    - 顶层不是 dict、`accounts` 不是 list → 返回 `{}`。
    - 某一行没有 `uid` → 跳过（没有 uid 就没法归属，留着只会误配）。
    - 字段类型不符 → 该字段归默认值，**不影响整行其它字段**。
    """
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("accounts")
    if not isinstance(rows, list):
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = row.get("uid")
        if not isinstance(uid, str) or not uid:
            continue

        models = []
        raw_models = row.get("rate_limited_models")
        if isinstance(raw_models, list):
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                name = item.get("model")
                if not isinstance(name, str) or not name:
                    continue
                models.append({
                    "model": name,
                    "until": _parse_time(item.get("until")),
                    "reset_at": _parse_time(item.get("reset_at")),
                    "reason": str(item.get("reason") or ""),
                })

        fails = row.get("consecutive_fails")
        out[uid] = {
            "cooling": bool(row.get("cooling")),
            "cool_kind": str(row.get("cool_kind") or ""),
            "cool_remaining_sec": _positive_int(row.get("cool_remaining_sec")),
            "until": _parse_time(row.get("until")),
            "reason": str(row.get("reason") or ""),
            "disabled": bool(row.get("disabled")),
            "manual_disabled": bool(row.get("manual_disabled")),
            "consecutive_fails": fails if isinstance(fails, int) and not isinstance(fails, bool) else 0,
            "degrade_until": _parse_time(row.get("degrade_until")),
            "rate_limited_models": models,
        }
    return out


def fetch_status(base: str, api_key: str = "", *, timeout: float = TIMEOUT,
                 transport: httpx.BaseTransport | None = None) -> dict:
    """同步读一次上游 `/status`，返回 `parse_status()` 的结果。

    `api_key` 为空时**不带 Authorization 头**——上游 `withAuth` 在 APIKey 为空时
    直接放行，发一个空 Bearer 反而可能被它当成「提供了但不对」。
    `transport` 只为测试注入（httpx.MockTransport），生产不传。
    """
    url = (base or "").rstrip("/") + "/status"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        kwargs = {"timeout": timeout, "headers": headers}
        if transport is not None:
            kwargs["transport"] = transport
        with httpx.Client(**kwargs) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise Wb2ApiError(f"读取上游状态失败：{type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise Wb2ApiError(f"上游 /status 返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise Wb2ApiError("上游 /status 返回的不是合法 JSON") from exc
    return parse_status(payload)


def view(info: dict | None, *, now: float | None = None) -> dict:
    """把一行上游冷却信息整理成**面板要显示的东西**。

    这是「读到的原始状态」与「界面文案」之间的唯一转换点，
    放在后端是因为**剩余时间是会变的**：上游给的 `cool_remaining_sec` 是它
    响应那一刻的快照，一旦缓存就过期。所以这里以 `until`/`reset_at` 这类
    绝对时刻为准重算剩余秒数，只有上游没给绝对时刻时才退回用快照值。
    """
    if not info:
        return {}
    clock = _now() if now is None else now

    def absolute_of(value) -> float | None:
        """容忍两种输入：`parse_status` 解析好的 unix 秒，或上游原样的 RFC3339 字符串。

        做成这样是因为 `view()` 会被两种调用方用：走 `fetch_status` 的拿的是已经
        解析过的 float，而单测/调试时直接喂原始 JSON 更直观。**接受字符串但必须
        当真解析**——绝不能拿字符串去减数字，那会直接抛 TypeError 把面板打崩。

        0 与负数都当成「没给」。上游表示「没有截止时刻」
        有两种写法 —— 键缺失，或者键在、值是 0（Go 的零值/int64 零值）。
        原先把 0 当成了合法的绝对时刻（1970-01-01），于是 left_seconds() 算出
        一个大负数 → 返回 None → 上游明明给了 cool_remaining_sec 快照也被丢掉，
        整个账号被判定成「没在冷却」。宁可当未设置，用 fallback 兜回来。
        """
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            return number if number > 0 else None
        return _parse_time(value)

    def left_seconds(absolute, fallback: int | None) -> int | None:
        stamp = absolute_of(absolute)
        if stamp is not None:
            seconds = int(stamp - clock)
            return seconds if seconds > 0 else None
        return fallback

    models = []
    # 类型防御：`parse_status` 出来的必然是 list，但 view() 也接受原始 JSON
    # （见它的 docstring），那时上游要是给了个非 list 的脏值，直接迭代字符串
    # 会让每个字符都去调 .get() 而崩掉整个面板。宁可当空，不可崩。
    raw_models = info.get("rate_limited_models")
    for item in (raw_models if isinstance(raw_models, list) else []):
        if not isinstance(item, dict):
            continue
        # 模型级的恢复时刻：优先 reset_at（上游的权威重置墙钟），退回 until
        # （可能被 soft_rate_max 截断过，是更早的那个）。两者都透给前端。
        # 注意这里不能用 `or` 串联：Go 的 time.Time 零值是个非空字符串
        # （"0001-01-01T00:00:00Z"），在 `or` 眼里是真值，会把本来好好的
        # reset_at 盖掉；解析后它又被归一成 None，于是「上游明确告诉我们
        # 模型什么时候恢复」变成了「恢复时间未知」。
        # 取第一个能解析出实际时刻的，谁都解析不出来才是真的没有。
        absolute = absolute_of(item.get("until")) or absolute_of(item.get("reset_at"))
        models.append({
            "model": item.get("model"),
            # 两个绝对时刻都规范化成 unix 秒再透出：前端只认一种形状，
            # 免得界面上出现「Go 给的是字符串、我们给的是数字」两种混着来。
            "until": absolute_of(item.get("until")),
            "reset_at": absolute_of(item.get("reset_at")),
            "remaining_sec": left_seconds(absolute, None),
            "reason": item.get("reason") or "",
        })

    account_left = left_seconds(info.get("until"), info.get("cool_remaining_sec"))
    cooling = bool(info.get("cooling")) or bool(models) or bool(account_left)
    if not cooling:
        # 账号不冷却、也没有模型台账 → 什么都不显示。
        # 注意：没有冷却不代表账号健康（可能 disabled），那些字段单独透出。
        return {
            "cooling": False,
            "cool_remaining_sec": None,
            "reason": str(info.get("reason") or ""),
            "disabled": bool(info.get("disabled")),
            "manual_disabled": bool(info.get("manual_disabled")),
            "consecutive_fails": int(info.get("consecutive_fails") or 0),
            "degrade_until": absolute_of(info.get("degrade_until")),
            "rate_limited_models": [],
        }

    return {
        "cooling": True,
        "cool_kind": str(info.get("cool_kind") or ""),
        "cool_remaining_sec": account_left,
        "until": absolute_of(info.get("until")),
        "reason": str(info.get("reason") or ""),
        "disabled": bool(info.get("disabled")),
        "manual_disabled": bool(info.get("manual_disabled")),
        "consecutive_fails": int(info.get("consecutive_fails") or 0),
        "degrade_until": absolute_of(info.get("degrade_until")),
        "rate_limited_models": models,
    }
