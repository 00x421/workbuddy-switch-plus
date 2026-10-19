"""国际版账号落盘前的两步一次性供给：地区注册 + trial 加油包。

为什么必须在**落盘之前**做完：

  · 未做地区注册的国际版账号，聊天会直接报 **14017「trial not activated」**。
    账号看起来是好的（uid 有、token 有、能列出来），一用就废，而且报错文案
    不在「登录失败」的语义里，用户根本联想不到是开号时漏了一步。
  · trial 是一次性的加油包，**每号只能领一次**（14051 = 已领过，幂等）。
    开号当时没领，后面没有任何入口能补。

这段口径来自上游 `workbuddy-manager`（server/services/tencent.py 的
`submit_region` / `registration_status` / `claim_trial`），三处都逐字对齐，
包括那个很容易写错的三字段取值。

设计取舍：**供给失败不阻断落盘**。理由是先让账号能进池、能用（哪怕额度
少），比因为地区注册网络抖动就把整次登录判死要好；失败原因通过 message
带回去给界面显示，用户看得到该重试什么。
"""
import json

from . import realm as realm_mod
from . import upstream


async def _post(client, url, headers, body):
    """发一次 POST 并折成 {code, message, data} 信封（**异步**）。

    为什么不用 upstream.request：那个是**同步**函数，服务的是账号池的
    httpx.Client（pool.py:297）。这条链路用的是 httpx.AsyncClient
    （rowser_login._client），同步函数会把客户端自己的 
equest()
    协程直接塞进 envelope()，炸
    AttributeError: 'coroutine' object has no attribute 'json'。
    所以这里自己 await，但**信封折叠仍然复用 upstream.envelope** ——
    信封语义（HTTP >= 300 原样返回上游 dict、不保证有 message）只有一份。
    """
    return upstream.envelope(await client.request("POST", url, headers=headers, json=body))


def _region_match(env):
    """从地区清单响应里取 {IOS2: (IOS2, EnName, Code)}。

    响应形状是内嵌的 **JSON 字符串**（不是对象）——上游脚本明确处理了这一层，
    这里照做。拿不到就返回空字典，调用方退回只发 IOS2。
    """
    data = env.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    if not isinstance(data, dict):
        return {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else None
    items = (inner or data).get("list")
    if not isinstance(items, list):
        return {}
    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        ios2 = str(item.get("IOS2") or "").strip().upper()
        if ios2:
            out[ios2] = (
                str(item.get("IOS2") or ios2),
                str(item.get("EnName") or ios2),
                str(item.get("Code") or ios2),
            )
    return out


async def region_fields(client, ios2):
    """按 IOS2 短码查该地区的三字段取值 (IOS2, EnName, 数字 Code)。

    查不到时退回三者同值。上游会拒绝非法值——**被拒绝也比替用户瞎猜好**：
    猜错会把地区归属写到别的国家，账号表面正常但权益按错误地区算。
    """
    code = str(ios2 or "").strip().upper()
    fallback = (code, code, code)
    env = await _post(
        client,
        realm_mod.billing_base(realm_mod.GLOBAL) + "/billing/area/get-country-code",
        realm_mod.headers(realm_mod.GLOBAL),
        {"filterForbidden": 1},
    )
    table = _region_match(env)
    return table.get(code, fallback)


async def registration_status(client, auth):
    """查国际版账号是否已完成地区注册。返回 (已完成, 说明)。

    三种「已完成」的判据：code 200 / code 0 / 文案里没有 region required。
    上游把「未注册」表达成 HTTP 500 + `region required`，按 code 或文案
    任一条命中都算未注册。
    """
    uid = str(auth.get("uid") or "")
    if not uid:
        return False, "缺少 uid，无法查询地区注册状态"
    env = upstream.envelope(await client.request(
        "GET",
        realm_mod.billing_base(realm_mod.GLOBAL) + "/auth/realms/copilot/overseas/user/register",
        headers=realm_mod.billing_headers(realm_mod.GLOBAL, auth),
        params={"userId": uid},
    ))
    # userId 已放上面的 params，不另拼 query
    code = env.get("code")
    text = str(upstream.message_of(env, "") or "").lower()
    if "region required" in text:
        return False, "尚未完成地区注册"
    if code in (0, 200):
        return True, "地区注册已完成"
    return False, f"注册状态未知 code={code}"


async def submit_region(client, auth, region_code):
    """提交国际版账号的地区。返回 (成功, 说明)。

    请求体形状照上游参照实现（其注释标注「实测」）：

        {"attributes": {"countryCode": [数字码],       ← 如 810000
                        "countryFullName": [英文名],   ← 如 China Hong Kong
                        "countryName": [IOS2]}}        ← 如 HK

    三个字段**取值各不相同**。写成「三处都填 IOS2」是最容易犯的错：上游按这
    三个字段落库，填错会把地区归属写歪，而表面上看不出任何异常。
    """
    r = realm_mod.norm_realm(auth.get("realm")) if isinstance(auth, dict) else realm_mod.GLOBAL
    if r != realm_mod.GLOBAL:
        return False, "国内版无需地区注册"
    code_upper = str(region_code or "").strip().upper()
    if code_upper not in realm_mod.REGION_CODES:
        return False, f"不支持的地区代码 {region_code!r}（可选：{'、'.join(sorted(realm_mod.REGION_CODES))}）"
    ios2, en_name, numeric = await region_fields(client, code_upper)
    env = await _post(
        client,
        realm_mod.billing_base(realm_mod.GLOBAL) + "/console/login/account",
        realm_mod.billing_headers(realm_mod.GLOBAL, auth),
        {"attributes": {"countryCode": [numeric], "countryFullName": [en_name], "countryName": [ios2]}},
    )
    if env.get("code") in (0, 200):
        return True, f"地区已提交（{code_upper}）"
    return False, f"提交地区失败：{_why(env)}"


async def claim_trial(client, auth):
    """领取国际版的一次性 trial 加油包。返回 (成功, 说明)。

    14051 = 已领过，按幂等算成功——用户重新登录同一个号时不该报错。
    """
    env = await _post(
        client,
        realm_mod.billing_base(realm_mod.GLOBAL) + "/billing/ide/trial",
        realm_mod.billing_headers(realm_mod.GLOBAL, auth),
        {},
    )
    if env.get("code") == 0:
        return True, "trial 已领取"
    if env.get("code") == 14051 or "14051" in str(env.get("message") or ""):
        return True, "trial 此前已领取"
    return False, f"领取 trial 失败：{_why(env)}"


def _why(env):
    """失败原因：有上游文案就用文案，没有就把业务码带出来。

    直接用 `message_of(env, "code=…")` 是不行的——它只在**整条链都挖不到**
    时才回兜底，而 `message_of` 一旦拿到任意文案就把 code 丢掉。可下游要的是
    「code 在不在」，比如 14017 的文案是 `trial not activated`，只留文案会让
    排查时连业务码都看不到。

    顺序：文案（含 code 字样的直接用）→ 文案 + code → 只有 code。
    """
    text = upstream._dig_message(env)
    code = env.get("code")
    if text:
        # 文案里已经带了这个 code 就不重复拼一遍。
        if code not in (None, 0, 200) and str(code) not in text:
            return f"{upstream.redact(text)}（code={code}）"
        return upstream.redact(text)
    if code not in (None, 0, 200):
        return f"code={code}"
    return "上游未说明原因"


async def provision(client, auth, region_code=None):
    """国际版落盘前的完整供给。返回给界面看的一句话（空串 = 一切正常）。

    顺序不能变：地区注册 → 复核 → trial。trial 的前置条件就是「地区已注册」，
    反了会拿到 14017 或干脆领不到。

    国内版直接返回空串——这一步对国内版是纯粹的 no-op，不能让它去碰国际版
    端点（那会带着国内 token 打到 www.workbuddy.ai，白留一条异常流量）。
    """
    if realm_mod.realm_of(auth) != realm_mod.GLOBAL:
        return ""
    notes = []
    if region_code:
        ok, msg = await submit_region(client, auth, region_code)
        notes.append(msg)
        if not ok:
            # 地区没提交成功就别去领 trial：trial 的前置是地区已注册，
            # 此时去领只会拿到一个失败码，反而把真实原因盖掉。
            return "；".join(notes)
    else:
        done, why = await registration_status(client, auth)
        if not done:
            return "尚未完成地区注册，请选择地区后重新登录（否则聊天会报 14017）"
        notes.append(why)
    ok, msg = await claim_trial(client, auth)
    notes.append(msg)
    return "；".join(x for x in notes if x)
