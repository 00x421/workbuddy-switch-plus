"""账号出口线路：给单个账号指定一条命名代理（HTTP / HTTPS）。

为什么需要
----------
账号一多就会出现「同一条出口 IP 上挂着十几个号」的形态。上游对这种形态的容忍度
低于用户的预期，而出问题时的表现又很含糊（不是 403，而是莫名其妙的冷却与
「凭据暂不可用」）。把出口拆开之后，账号之间互不牵连，排查时也能一眼看出
「是这一个号的问题，还是这一整条线路的问题」。

数据模型（落在 state.json）
--------------------------
  egress.lines   {line_id: {name, url, enabled, created}}
  egress.assign  {account_id: line_id}

`assign` 里只留 account_id → line_id 的映射，**不复制 url**：改线路地址时
所有挂在这条线路上的账号一起生效，不需要逐账号改。指向已删除线路的悬空绑定
在解析时按「没有线路」处理（直连），不会让请求失败。

与 manager 的差异
-----------------
manager 把出口做在它自己那套 docker 化的上游网关上；我们这里直接作用于
两个层面：
  · **数据面**：`core.converter` 转发上游时带上 `proxy=`（经 ContextVar 传递，
    见 `REQUEST_EGRESS`）；
  · **账号面**：积分查询 / 签到 / token 刷新这些后台请求也走同一线路。
两处共用 `proxy_for()` 一个口径，避免「跑对话走了代理，但签到还是直连」这种
半截状态——那会让出口 IP 的判据失去意义。
"""
from __future__ import annotations

import re
import secrets
import time
from contextvars import ContextVar
from urllib.parse import urlsplit, urlunsplit

SUPPORTED_SCHEMES = ("http", "https")
MAX_LINES = 64

# 数据面的当前请求所用出口。由 PoolMiddleware 选完账号后 set()，
# converter 里的 httpx 客户端通过 `httpx_proxy_kwargs()` 读取。
REQUEST_EGRESS: ContextVar[str | None] = ContextVar("pool_egress", default=None)

_HOST_RE = re.compile(r"^[A-Za-z0-9._\-\[\]:]+$")


def default_egress() -> dict:
    return {"lines": {}, "assign": {}}


def norm_url(raw: object) -> str:
    """校验并归一化代理地址；不合法抛 ValueError。

    允许 `http://user:pass@host:port` 这种带凭据的形态——很多商用代理就是这么
    下发的。凭据原样保存（state.json 里本来就存着账号的 access token，
    这里没有额外的暴露面），但展示时会被 `mask()` 抹掉。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("代理地址不能为空")
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError("代理地址必须以 http:// 或 https:// 开头")
    host = parts.hostname or ""
    if not host or not _HOST_RE.match(host):
        raise ValueError("代理地址缺少有效主机名")
    if parts.port is not None and not (0 < parts.port < 65536):
        raise ValueError("代理端口超出范围")
    # 保留 path 的写法：少数自建代理把 token 放在路径里（http://gw/abc123）。
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def mask(url: str) -> str:
    """展示用：抹掉认证信息与路径里的 token，只留 scheme://host:port。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(无效地址)"
    user = parts.username
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    shown = f"{parts.scheme}://{netloc}"
    if user:
        shown = f"{parts.scheme}://{user}:***@{netloc}"
    if parts.path and parts.path != "/":
        shown += "/…"
    return shown


def _lines(store) -> dict:
    block = store.data.setdefault("egress", default_egress())
    if not isinstance(block.get("lines"), dict):
        block["lines"] = {}
    if not isinstance(block.get("assign"), dict):
        block["assign"] = {}
    return block


def list_lines(store) -> list[dict]:
    """线路清单（含每条线路上挂了哪些账号）。"""
    with store.lock:
        block = _lines(store)
        bound: dict[str, list[str]] = {}
        for aid, lid in block["assign"].items():
            bound.setdefault(lid, []).append(aid)
        names = {aid: (item.get("name") or aid)
                 for aid, item in store.data["accounts"].items()}
        out = []
        for lid, item in block["lines"].items():
            accounts = sorted(bound.get(lid, []), key=lambda a: names.get(a, a))
            out.append({
                "id": lid,
                "name": item.get("name", ""),
                "url": mask(item.get("url", "")),
                "enabled": bool(item.get("enabled", True)),
                "created": item.get("created"),
                "accounts": accounts,
                "account_names": [names.get(a, a) for a in accounts],
                "account_count": len(accounts),
            })
    out.sort(key=lambda x: x.get("created") or 0)
    return out


def create_line(store, name: object, url: object, enabled=True) -> dict:
    label = str(name or "").strip()
    if not label:
        raise ValueError("线路名称不能为空")
    if len(label) > 40:
        raise ValueError("线路名称请控制在 40 字以内")
    candidate = norm_url(url)
    lid = secrets.token_hex(6)
    with store.lock:
        block = _lines(store)
        if len(block["lines"]) >= MAX_LINES:
            raise ValueError(f"最多保存 {MAX_LINES} 条出口线路")
        if any(item.get("name") == label for item in block["lines"].values()):
            raise ValueError(f"已存在同名线路：{label}")
        block["lines"][lid] = {"name": label, "url": candidate,
                               "enabled": bool(enabled), "created": int(time.time())}
        store.save()
    return {"id": lid, "name": label}


def update_line(store, lid: str, patch: dict) -> dict:
    with store.lock:
        block = _lines(store)
        item = block["lines"].get(lid)
        if item is None:
            raise KeyError(lid)
        if "name" in patch and patch["name"] is not None:
            label = str(patch["name"]).strip()
            if not label:
                raise ValueError("线路名称不能为空")
            if any(other.get("name") == label
                   for other_id, other in block["lines"].items() if other_id != lid):
                raise ValueError(f"已存在同名线路：{label}")
            item["name"] = label[:40]
        if "url" in patch and patch["url"] is not None:
            item["url"] = norm_url(patch["url"])
        if "enabled" in patch:
            item["enabled"] = bool(patch["enabled"])
        store.save()
        return {"id": lid, "name": item["name"]}


def delete_line(store, lid: str) -> int:
    """删除线路，并把挂在它上面的账号解绑。返回解绑的账号数。

    解绑而非报错：线路没了的账号应当**回落直连**继续工作，而不是因为一条
    被删掉的线路彻底不可用。解绑之后 `assign` 里不留悬空引用，所以这些账号
    不会出现在 `orphans()` 里——那条路只在有人手工改过 state.json 时才会走到。
    """
    with store.lock:
        block = _lines(store)
        if lid not in block["lines"]:
            raise KeyError(lid)
        del block["lines"][lid]
        freed = [aid for aid, bound in block["assign"].items() if bound == lid]
        for aid in freed:
            del block["assign"][aid]
        store.save()
        return len(freed)


def assign(store, aid: str, lid: str | None) -> None:
    """把账号绑定到某条线路；lid 为空则解绑（回直连）。"""
    with store.lock:
        block = _lines(store)
        if aid not in store.data["accounts"]:
            raise KeyError(aid)
        if lid:
            if lid not in block["lines"]:
                raise KeyError(lid)
            block["assign"][aid] = lid
        else:
            block["assign"].pop(aid, None)
        store.save()


def unbind_account(store, aid: str) -> None:
    """账号被**真删**时清掉绑定，避免 assign 里留下永远用不到的条目。

    注意只在真删路径上调（`delete_account` 的 file 分支）。本产品的「删除账号」
    默认是**可逆的隐藏**，那时**不能**解绑：账号库还在、解除隐藏就回来了，
    解绑等于把用户配好的线路悄悄丢掉，而界面上什么提示都没有。
    """
    with store.lock:
        block = _lines(store)
        if block["assign"].pop(aid, None) is not None:
            store.save()


def proxy_for(store, aid: str | None) -> str | None:
    """解析某账号当前应使用的代理地址；没有绑定或线路被停用则返回 None（直连）。"""
    if not aid:
        return None
    with store.lock:
        block = store.data.get("egress") or {}
        assign_map = block.get("assign") or {}
        lines = block.get("lines") or {}
        lid = assign_map.get(aid)
        if not lid:
            return None
        line = lines.get(lid)
        if not line or not line.get("enabled", True):
            return None
        return line.get("url") or None


def orphans(store) -> list[str]:
    """绑到了**已删除**线路上的账号 id（悬空引用）。

    只报「线路不存在」这一种：那是数据不一致，用户必须重新指定一条线路才能收尾。

    早期这里把「线路存在但已停用」也算进来，结果每次正常停用一条线路，挂在上面的
    账号就集体变成「异常」——喊狼来了，用户很快就不看了。停用是受支持的状态
    （账号回退直连），单独由 `stale()` 汇报。
    """
    with store.lock:
        block = store.data.get("egress") or {}
        assign_map = block.get("assign") or {}
        lines = block.get("lines") or {}
        return sorted(aid for aid, lid in assign_map.items() if lid not in lines)


def stale(store) -> list[str]:
    """绑到了**已停用**线路上的账号 id：线路还在，但当前按直连出网。

    与 `orphans()` 的区别是「要不要动手」：这里只要把线路启用回来就恢复，
    不需要给账号换绑定。所以界面上不分同一句话。
    """
    with store.lock:
        block = store.data.get("egress") or {}
        assign_map = block.get("assign") or {}
        lines = block.get("lines") or {}
        return sorted(aid for aid, lid in assign_map.items()
                      if lid in lines and not lines[lid].get("enabled", True))


def httpx_proxy_kwargs() -> dict:
    """给 httpx 客户端的 `proxy=` 关键字参数；没有出口时返回空 dict。

    只在 ContextVar 有值时才带这个参数：`httpx.AsyncClient(proxy=None)` 语义上
    等同于直连，但显式传 None 会让「环境变量代理」失效——用户用 HTTP_PROXY
    跑的场景不该被这次改动打断。
    """
    url = REQUEST_EGRESS.get()
    return {"proxy": url} if url else {}


def client_for(store, aid: str | None):
    """账号面（积分 / 签到 / 刷新）用的同步 httpx 客户端。"""
    import httpx
    return httpx.Client(timeout=20, follow_redirects=False,
                        **({"proxy": url} if (url := proxy_for(store, aid)) else {}))
