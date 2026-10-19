"""浏览器扫码登录：国内版 CodeBuddy 与国际版 WorkBuddy AI 两条链路。

为什么要分 realm：上游是两套完全独立的产品线（copilot.tencent.com 与
www.workbuddy.ai），域名、platform 取值、UA、Origin 全不一样。把国际版
二维码拿给国内版扫（或反过来）不会报错，只会一直 pending —— 上游
`workbuddy-manager` 的注释里明确记过这个坑。

链路（两版一致，只是基址与 platform 不同）：

  1. POST {chat_base}/v2/plugin/auth/state?platform=…   → {state, authUrl}
  2. 用户在浏览器里扫码/登录
  3. GET  {chat_base}/v2/plugin/auth/token?state=…      → {accessToken, refreshToken, domain, expiresAt}
  4. GET  {chat_base}/v2/plugin/login/account?state=…   → {uid, enterpriseId, nickname}
  5. 国际版：地区注册 → 复核 → 领 trial（见 global_region.py）
  6. 落盘

令牌只在这一次请求内使用，永不回传给管理端页面（`_public` 里没有任何 token）。
"""
import asyncio
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import HTTPException

from . import global_region
from . import realm as realm_mod

TTL = 300

#: 同时存在的授权会话上限。
#:
#: 之前是 16：那是「单账号登录」时代的口径，用户一次只开一张码，16 个
#: 余额绰绰有余。批量扫码要同时摆出 20~50 个二维码，16 会在第 17 张
#: 直接 429，而这恰恰是批量场景下的**第一步**，用户会以为功能坏了。
#:
#: 每个 flow 持有一个 httpx.AsyncClient（连接池），50 个的总开销仍在
#: 单进程可接受范围内；真正的上限由 MAX_FLOWS_PER_OWNER 兜住。
MAX_FLOWS = 60

#: 单个操作者（= 一个管理会话）名下的并发会话上限。
MAX_FLOWS_PER_OWNER = 50

#: 批量扫码时国际版账号自动注册的默认地区（中国香港）。
#:
#: 为什么必须有个默认值而不是「留空待选」：未注册地区 → 聊天 14017，而
#: 14017 的报错文案（trial not activated）与开号漏步骤之间没有任何可见关联。
#: 批量场景又没法逐格选地区，所以只能自动挑一个能用的。事后可改。
DEFAULT_BATCH_REGION = "HK"


def _headers(realm):
    """扫码链路自己的请求头。

    与 realm.headers() 有两点不同，都是照上游实现定的：

      · Referer 要带 `/login` 后缀（这是登录页自己的地址，不带的话
        风控看到的来源和实际页面不一致）
      · chat 域 UA 用的是 `CLI/<ver> CodeBuddy/<ver>` 形态，与管理面
        `WorkBuddy/… CLI/…` 那段不同源

    **不要图省事直接复用 realm.headers()**：这一段是登录链路，比普通
    管理接口更容易被审视。
    """
    r = realm_mod.norm_realm(realm)
    platform = "CodeBuddy" if r == realm_mod.CN else "WorkBuddy AI"
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": realm_mod.accept_language(r),
        "X-Requested-With": "XMLHttpRequest",
        "X-CodeBuddy-Request": "1",
        "Origin": realm_mod.origin(r),
        "Referer": realm_mod.origin(r) + "/login",
        "User-Agent": f"CLI/2.63.2 {platform}/2.63.2",
    }


class BrowserLogin:
    def __init__(self, save_account, client_factory=None, clock=time.time):
        self.save_account = save_account
        # client_factory 允许测试注入；真实路径按 realm 建 client（见 _client）
        self.client_factory = client_factory
        self.clock = clock
        self.flows = {}
        self.guard = asyncio.Lock()

    def _client(self, realm):
        if self.client_factory is not None:
            return self.client_factory()
        return httpx.AsyncClient(headers=_headers(realm), timeout=20, follow_redirects=False)

    async def _discard(self, fid):
        flow = self.flows.pop(fid, None)
        if flow:
            await flow["client"].aclose()

    async def cleanup(self):
        for fid, flow in list(self.flows.items()):
            if flow["expires"] <= self.clock() and not flow["lock"].locked():
                await self._discard(fid)

    async def close(self):
        for fid in list(self.flows):
            await self._discard(fid)

    async def cancel_owner(self, owner):
        for fid, flow in list(self.flows.items()):
            if flow["owner"] == owner:
                async with flow["lock"]:
                    await self._discard(fid)

    @staticmethod
    def envelope(response, realm=None):
        """把上游响应折成字典，异常转成对用户有意义的 502。

        `realm` 只用于错误文案：国际版账号拿到 401 时应提示检查国际服
        登录，而不是笼统的「授权服务不可用」。
        """
        if response.status_code != 200:
            raise HTTPException(502, "授权服务暂时不可用，请稍后重试")
        try:
            obj = response.json()
        except ValueError:
            raise HTTPException(502, "授权服务返回格式异常，请重新生成链接")
        if not isinstance(obj, dict):
            raise HTTPException(502, "授权服务返回格式异常")
        return obj

    async def start(self, owner, name, realm=realm_mod.CN, region=None, slot=None):
        """生成授权链接。

        `realm` 决定打哪一套上游；`region` 只对国际版有意义，会被存进 flow，
        在**落盘之前**用于地区注册——不在这里就注册，是因为那时还没有
        accessToken（region 提交必须带账号身份头）。

        `slot` 是批量扫码用的槽位键（界面上那一格的标识）。同一个 owner 下
        slot 相同视为**同一格重新发码**，旧的直接顶掉，不会累积成孤儿会话；
        不传 slot 则退化成「一个 owner 只有一张码」（单账号登录的原有行为，
        保持向后兼容）。
        """
        r = realm_mod.norm_realm(realm)
        region = str(region or "").strip().upper()
        if r == realm_mod.GLOBAL and region and region not in realm_mod.REGION_CODES:
            raise HTTPException(400, f"不支持的地区：{region}（可选 {' / '.join(sorted(realm_mod.REGION_CODES))}）")
        slot = str(slot or "").strip()[:32]
        async with self.guard:
            await self.cleanup()
            if len(self.flows) >= MAX_FLOWS:
                raise HTTPException(429, "等待授权的请求过多，请稍后重试")
            if slot:
                # 同一格重发码：顶掉旧的。不这么做的话批量页刷新一次就多
                # 50 个死会话，很快撞上 MAX_FLOWS，而用户看到的只是「太多了」。
                for fid, flow in list(self.flows.items()):
                    if flow["owner"] == owner and flow.get("slot") == slot:
                        async with flow["lock"]:
                            await self._discard(fid)
                owned = sum(1 for flow in self.flows.values() if flow["owner"] == owner)
                if owned >= MAX_FLOWS_PER_OWNER:
                    raise HTTPException(429, f"同时等待授权的数量已达上限（{MAX_FLOWS_PER_OWNER}），请先完成或关闭一部分")
            else:
                await self.cancel_owner(owner)
            client = self._client(r)
            try:
                obj = self.envelope(await client.post(
                    realm_mod.chat_base(r) + realm_mod.API_PREFIX + "/auth/state",
                    params={"platform": realm_mod.platform(r)}, json={}))
                data = obj.get("data") or {}
                if obj.get("code") != 0 or not isinstance(data, dict):
                    raise HTTPException(502, "无法生成授权链接，请稍后重试")
                state, url = data.get("state"), data.get("authUrl")
                if not isinstance(state, str) or not 1 <= len(state) <= 2048 or not isinstance(url, str) or len(url) > 8192:
                    raise HTTPException(502, "授权服务缺少有效的登录信息")
                self._check_url(url, state, r)
                fid = secrets.token_urlsafe(24)
                flow = {"owner": owner, "state": state, "client": client, "name": name, "realm": r,
                        "region": region, "slot": slot, "bare": bool(slot and not region),
                        "expires": self.clock() + TTL, "lock": asyncio.Lock(),
                        "next_poll": 0, "tokens": None, "saved": None, "provision": ""}
                self.flows[fid] = flow
                return {"id": fid, "url": url, "expires_at": int(flow["expires"] * 1000), "interval": 3,
                        "realm": r, "region": region, "slot": slot}
            except BaseException:
                await client.aclose()
                raise

    @staticmethod
    def _check_url(url, state, realm):
        """校验上游回的登录页地址，防跳走域与开放重定向。

        判定基准是 **chat_base 的 host**，不是 origin（网页源站）的 host ——
        这两者在两版里都不是同一个域：
            CN      chat=copilot.tencent.com   origin=www.codebuddy.cn
            GLOBAL  chat=www.workbuddy.ai      origin=www.workbuddy.ai
        拿 origin 去卡会把国内版 100% 误杀（登录页就在 copilot.tencent.com）。
        必须按 realm 取基准，否则国际版的登录页又会被国内版白名单拦掉。
        """
        parts = urlsplit(url)
        expect_host = urlsplit(realm_mod.chat_base(realm)).hostname
        if (parts.scheme != "https" or parts.hostname != expect_host
                or parts.port not in (None, 443) or parts.username or parts.password
                or parts.path != "/login"
                or parse_qs(parts.query).get("state") != [state]):
            raise HTTPException(502, "授权链接校验失败，请重新生成")

    def get(self, fid, owner):
        flow = self.flows.get(fid)
        if not flow or flow["owner"] != owner:
            raise HTTPException(404, "授权会话不存在，请重新生成登录链接")
        return flow

    async def cancel(self, fid, owner):
        flow = self.get(fid, owner)
        async with flow["lock"]:
            await self._discard(fid)
        return {"status": "cancelled"}

    async def poll(self, fid, owner):
        flow = self.get(fid, owner)
        if flow["lock"].locked():
            return {"status": "pending"}
        async with flow["lock"]:
            if fid not in self.flows:
                raise HTTPException(404, "授权会话已取消")
            if flow["expires"] <= self.clock():
                await self._discard(fid)
                return {"status": "expired"}
            if flow["saved"]:
                return {"status": "success", **flow["saved"]}
            if flow["next_poll"] > self.clock():
                return {"status": "pending"}
            flow["next_poll"] = self.clock() + 3
            client, r = flow["client"], flow["realm"]
            base = realm_mod.chat_base(r) + realm_mod.API_PREFIX
            if flow["tokens"] is None:
                obj = self.envelope(await client.get(base + "/auth/token", params={"state": flow["state"]}))
                if obj.get("code") == 11217:
                    return {"status": "pending"}
                if obj.get("code") != 0:
                    raise HTTPException(502, "上游授权未成功，请重新生成链接并登录")
                tokens = obj.get("data")
                if not isinstance(tokens, dict) or not tokens.get("accessToken") or not tokens.get("refreshToken"):
                    raise HTTPException(502, "授权服务没有返回完整凭据，请重新登录")
                flow["tokens"] = tokens
            tok = flow["tokens"]
            obj = self.envelope(await client.get(
                base + "/login/account", params={"state": flow["state"]},
                headers={"Authorization": "Bearer " + tok["accessToken"]}))
            acct = obj.get("data")
            if obj.get("code") != 0 or not isinstance(acct, dict) or not acct.get("uid"):
                raise HTTPException(502, "授权已完成，但账号信息暂未获取成功，请稍后重试")
            auth = {k: tok[k] for k in ["accessToken", "refreshToken", "domain"] if k in tok}
            # 归属以 domain 兜底：即使用户在界面上选错（或没选）realm，
            # 也以账号自己声明的 domain 为准，避免把国际服令牌当国内服存。
            actual = realm_mod.GLOBAL if realm_mod.has_global_domain(auth.get("domain")) else r
            if actual != r:
                # 域与用户所选不一致：不静默改，明确告知。静默改会让账号
                # 悄悄落进另一个 realm，后续所有端点请求都跟着错。
                raise HTTPException(400, (
                    "该账号属于国际版 WorkBuddy AI，请把版本切到「国际版」后重新登录"
                    if actual == realm_mod.GLOBAL else
                    "该账号属于国内版 CodeBuddy，请把版本切到「国内版」后重新登录"))
            auth["realm"] = actual
            expires = realm_mod.expires_at_ms(tok, self.clock())
            if not expires:
                raise HTTPException(502, "授权服务没有返回有效的令牌到期时间")
            auth["expiresAt"] = expires
            if flow["expires"] <= self.clock():
                await self._discard(fid)
                return {"status": "expired"}
            doc = {"account": {k: acct.get(k, "") for k in ["uid", "enterpriseId", "nickname"]}, "auth": auth}
            # 供给必须在落盘之前：未注册地区的国际版账号一旦进池，聊天就报
            # 14017，而那时已经没有 accessToken 之外的补救入口了。
            #
            # 批量扫码没有「选地区」这一步（一次摆 50 张码，让用户逐格选地区
            # 不现实），所以 region 为空时不能沿用单账号那句「请选择地区后重新
            # 登录」——那句话在批量语义下等于死路。改成自动注册到默认地区，
            # 用户事后可以在账号行上改。
            region = flow.get("region")
            if not region and flow.get("bare") and auth["realm"] == realm_mod.GLOBAL:
                region = DEFAULT_BATCH_REGION
            note = await global_region.provision(client, auth, region)
            flow["provision"] = note
            flow["saved"] = self.save_account(doc, flow["name"])
            flow["tokens"] = None
            flow["state"] = ""
            await client.aclose()
            result = {"status": "success", **flow["saved"]}
            if note:
                result["message"] = note
            if region:
                result["region"] = region
            return result
