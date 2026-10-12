"""上游（billing / growth 两个域）共用的主机、信封与判据。

这两个域的实现（`travel.py` 派猫猫旅行、`report.py` 对话活跃上报）必须用同一套
判据：各写一份总会漂移，而漂移的表现是「同一种失败，一个模块认得出来、另一个
认不出来」——比如 401 装在 HTTP 200 + 业务码里这种，漏判的一方会拿着死 token
一直重试。

信封形状沿用桌面端 `http_request` 的约定：

    HTTP 成功 → 直接返回响应体（业务码在 `code` 里，0 或 200 都算成功）
    HTTP 失败 → 合成 `{"code": <http 状态>, "message": ...}`

于是 `code in (0, 200)` 这一个判据在桌面端、Python 侧、上游 Go 侧三处都成立。
"""

from __future__ import annotations

import re

# 上游主机。不要改：billing（签到/积分/活跃上报）与 growth（成长中心/猫猫旅行）
# 都挂在 `www.codebuddy.cn`，与桌面端 config.rs 的 WORKBUDDY_API_ENDPOINT 同值。
# 写成别的域名，同一批凭据会被上游判成域不一致。
BASE = "https://www.codebuddy.cn"

# 单条错误文案的截断长度（上游偶尔会回整段 HTML 错误页）。
MESSAGE_LIMIT = 200

# 上游的报错体有两种形态：正常的 JSON（`{"code":..,"message":".."}`），
# 以及整段 HTML / 网关错误页。后者会被 `envelope()` 原样吞成 message，
# 而它偶尔会把我们发出去的鉴权头回显在正文里（`Authorization: Bearer sk-...`）。
# 这条文案会一路流进 account_status、travel_states 与任务流水并落盘，
# 所以脱敏必须发生在折信封这一步——晚于这里的每一处都在往磁盘上写凭据。
# 每条是 (模式, 替换成什么)。顺序有意义，必须键值整体匹配：
# 只匹配「值」的话，键名与值之间的引号 / 冒号会让相邻的普通文本抢到匹配——
# `bad token: "access_token":"at-..."` 里 `token: "access_token` 会先被吃成
# 一次命中，结果是「看着脱敏了，密钥还留在原地」。
# 反过来也不能把 `Authorization:` 这种键名一起抹掉：出问题时还得能看出
# 是什么错。所以每组只吃掉值。
_SECRET_PATTERNS = (
    # `"access_token":"..."` 这种 JSON 键值对：连键带值一起替换成 `"***"`。
    (re.compile(r'(?i)"[A-Za-z_]*(?:token|secret|password|authorization)"\s*:\s*"[^"]{6,}"'),
     '"***"'),
    # `Authorization: Bearer <token>` —— 保留 `Authorization:` 与 `Bearer` 这两个词。
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}"), "Bearer ***"),
    # `accessToken=xxx` / `refresh_token: xxx` 这种裸键值对（键名留在原地）。
    (re.compile(r"(?i)(?<![A-Za-z_\"])\b[A-Za-z_]*(?:token|secret|password)\b"
                r"\s*[:=]\s*[A-Za-z0-9._\-]{6,}"), "***"),
    # 裸的 sk- / wbt_ / wbk_ 前缀密钥
    (re.compile(r"\b(?:sk|wbt|wbk)[-_][A-Za-z0-9._\-]{6,}"), "***"),
)


def redact(text):
    """把文案里可能夹带的凭据抹掉。**任何要落盘或回给界面的文案都必须过这里。**

    上游的网关错误页会把请求头回显进正文；不脱敏的话，一次 502 就能把用户的
    access token 写进 state.json 和流水里，而这两处都会被网页后台渲染出来。
    """
    out = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def envelope(response):
    """把 httpx 响应折成统一信封（见模块 docstring）。

    两条出口都过 `redact()`：上游塞在 message 里的凭据不能落到我们的磁盘上。
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 300:
        if isinstance(body, dict) and body:
            return body
        return {"code": response.status_code,
                "message": redact(response.text)[:MESSAGE_LIMIT]}
    if isinstance(body, dict):
        return body
    return {"code": -1, "message": "响应格式异常"}


def ok(env):
    """业务码判据：0（常规）或 200（HTTP 成功但 body 里也带了 code）。"""
    return env.get("code") in (0, 200)


def message_of(env, fallback="操作失败"):
    raw = env.get("message") or env.get("msg") or ""
    raw = str(raw).strip() or fallback
    # 上游的 business message 同样会回显请求头，回给界面 / 落盘前再过一次。
    return redact(raw)[:MESSAGE_LIMIT]


def is_unauthorized(env):
    """未授权判据。

    除了 401/403，还要看文案：上游偶尔用 HTTP 200 + 业务码把「登录失效」装在
    message 里，只判状态码会漏掉，然后拿死 token 反复试。
    """
    if env.get("code") in (401, 403):
        return True
    text = str(env.get("message") or env.get("msg") or "").lower()
    return any(k in text for k in ("unauthorized", "401", "登录", "失效", "过期", "token"))


def request(client, url, method, headers, body=None):
    """发一次请求并折成信封。"""
    return envelope(client.request(method, url, headers=headers, json=body))
