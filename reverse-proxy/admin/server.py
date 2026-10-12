"""Small, persistent management layer for workbuddy2api. Single-process deployment."""
import asyncio
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from core import converter
from . import accounts
from . import egress
from . import groups
from . import keysvc
from . import mailer
from . import settings_store
from . import keyexport
from . import members as members_svc
from . import portal
from . import redemption
from . import usage
from .browser_login import BrowserLogin
from .pool import TASK_KINDS, AccountPool, PoolMiddleware, default_pool
from .metrics import RequestMetrics, MetricsMiddleware
from .security import COOKIE, Security, bearer_session, digest, scope_to_role
from .iputil import client_ip, client_ip_from_scope, evaluate, valid_cidr

MAX_BODY = 1024 * 1024
def _assets_dir() -> Path:
    """内置静态后台的目录；打包成 exe 后在 PyInstaller 的解包目录里。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "admin" / "static")
    candidates.append(Path(__file__).parent / "static")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[-1]


ASSETS = _assets_dir()
IP_LOG_LIMIT = 500


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def clean_name(value, fallback):
    if value is None:
        return fallback
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60:
        raise HTTPException(400, "名称需为 1–60 个字符")
    return value.strip()


def live_accounts(store) -> None:
    """让账号视图对齐账号库（SwitchStore 支持热加载，旧 Store 无此概念）。

    代价只有一次 stat()：指纹没变就直接返回，所以在每个涉及账号的
    请求前调用是安全的。
    """
    refresh = getattr(store, "refresh_accounts", None)
    if refresh is not None:
        refresh()


class Store:
    def __init__(self, root, auth_dir, initial_key, admin_key):
        if len(admin_key) < 20:
            raise RuntimeError("ADMIN_KEY must contain at least 20 characters")
        self.root, self.auth_dir = Path(root), Path(auth_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.admin_digest = digest(admin_key)
        self.admin_key = admin_key
        self.sessions, self.attempts = {}, {}
        self.test_lock = asyncio.Lock()
        self.test_keys = set()
        self.managers = {}
        self.started = time.time()
        self.events = deque(maxlen=40)
        self.path = self.root / "state.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"accounts": {}, "active": None, "keys": {}, "pool": default_pool()}
            for p in sorted(self.auth_dir.glob("*.info")):
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    self.validate_credential(doc)
                except (ValueError, OSError, HTTPException):
                    continue
                aid = secrets.token_hex(8)
                name = str(doc.get("account", {}).get("nickname") or "已迁移账号")[:60]
                self.data["accounts"][aid] = {"name": name, "file": p.name, "enabled": not bool(doc.get("disabled")), "created": int(time.time())}
                if self.data["active"] is None and not doc.get("disabled"):
                    self.data["active"] = aid
            if initial_key:
                self.add_key("原有 API Key", initial_key)
            self.save()
        self.sync_active()

    @staticmethod
    def validate_credential(doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("auth"), dict) or not isinstance(doc.get("account"), dict):
            raise HTTPException(400, "需要桌面端 .info／JSON 登录文件，包含 auth 和 account 对象")
        for key in ("accessToken", "refreshToken"):
            if not isinstance(doc["auth"].get(key), str) or not doc["auth"][key].strip():
                raise HTTPException(400, "凭据缺少 accessToken 或 refreshToken，请重新导出桌面端登录文件")
            if len(doc["auth"][key]) > 65536:
                raise HTTPException(400, "凭据字段过长")
        if not isinstance(doc["account"].get("uid"), str) or not doc["account"]["uid"]:
            raise HTTPException(400, "凭据缺少 account.uid")
        if not isinstance(doc["auth"].get("expiresAt"), (int, float)) or isinstance(doc["auth"].get("expiresAt"), bool):
            raise HTTPException(400, "凭据需要有效的 expiresAt 时间戳")
        for key in ("domain",):
            value = doc["auth"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "凭据包含无效字段")
        for key in ("uid", "enterpriseId"):
            value = doc["account"].get(key, "")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise HTTPException(400, "账号字段格式不正确")

    def save(self):
        write_json(self.path, self.data)

    def save_browser_account(self, doc, name):
        self.validate_credential(doc)
        with self.lock:
            identity = (doc["account"]["uid"], doc["account"].get("enterpriseId", ""))
            for aid, item in self.data["accounts"].items():
                try:
                    old = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if (old.get("account", {}).get("uid"), old.get("account", {}).get("enterpriseId", "")) == identity:
                    current = self.manager_for(aid, item)
                    if current:
                        with current._lock:
                            write_json(self.file_for(item), doc)
                            current._cached = None
                    else:
                        write_json(self.file_for(item), doc)
                    if name:
                        item["name"] = name
                    self.data.get("account_status", {}).pop(aid, None)
                    self.save()
                    self.sync_active()
                    return {"account_id": aid, "updated": True}
            if len(self.data["accounts"]) >= 100:
                raise HTTPException(400, "最多保存 100 个账号")
            aid = secrets.token_hex(8)
            write_json(self.auth_dir / (aid + ".info"), doc)
            self.data["accounts"][aid] = {"file": aid + ".info", "name": name or str(doc["account"].get("nickname") or "浏览器授权账号")[:60], "created": int(time.time()), "enabled": True}
            if self.data["active"] is None:
                self.data["active"] = aid
            self.save()
            self.sync_active()
            return {"account_id": aid, "updated": False}

    def file_for(self, item):
        path = self.auth_dir / item["file"]
        if path.parent.resolve() != self.auth_dir.resolve() or path.is_symlink():
            raise HTTPException(400, "凭据路径不合法")
        return path

    def sync_active(self):
        aid = self.data.get("active")
        item = self.data["accounts"].get(aid)
        converter.CONFIG["cred"] = self.manager_for(aid, item) if item and item["enabled"] and self.file_for(item).exists() else None

    def manager_for(self, aid, item):
        with self.lock:
            if aid not in self.managers:
                self.managers[aid] = converter.CredentialManager(self.file_for(item))
            return self.managers[aid]

    def account_rows(self):
        result = []
        for aid, item in self.data["accounts"].items():
            row = {"id": aid, "name": item["name"], "enabled": item["enabled"], "active": aid == self.data["active"], "created": item["created"]}
            try:
                doc = json.loads(self.file_for(item).read_text(encoding="utf-8"))
                expiry = doc["auth"].get("expiresAt", 0)
                row.update({"nickname": str(doc["account"].get("nickname") or ""), "uid": str(doc["account"].get("uid") or ""), "expires_at": expiry, "expired": expiry < time.time() * 1000, "refresh_available": bool(doc["auth"].get("refreshToken")), "status": "ready"})
            except (ValueError, OSError, KeyError):
                row.update({"status": "invalid", "expired": True, "expires_at": 0, "refresh_available": False})
            result.append(row)
        return result

    def add_key(self, name, value=None):
        value = value or "sk-wb-" + secrets.token_urlsafe(32)
        kid = secrets.token_hex(8)
        self.data["keys"][kid] = {"name": name, "hash": digest(value), "hint": value[:5] + "…" + value[-4:], "created": int(time.time())}
        return kid, value

    def check_api(self, authorization, x_api_key):
        """校验下游 API Key：返回命中的密钥条目（含 id / 策略），否则抛 401。

        供 `converter._check_auth` 与 PoolMiddleware 复用。返回密钥是为了让
        池中间件在拿到 model / 来源 IP 后补做 IP·模型白名单与用量记账。
        """
        token = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else x_api_key or ""
        with self.lock:
            if digest(token) in self.test_keys:
                return None  # 临时测试密钥：无策略
        key = keysvc.resolve_key(self, token)
        if not key:
            raise HTTPException(401, "invalid api key")
        keysvc.validate_credential(key)  # 只校验凭据；IP / 模型在池中间件补
        return key

    def add_key(self, name, value=None):
        kid, token = keysvc.create_key(self, name, value=value)
        return kid, token


class AdminMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                response = JSONResponse({"detail": "文件或请求超过 1 MB"}, 413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ]
            await send(message)
        await self.app(scope, replay, secure_send)


class IPGuardMiddleware:
    """入站 IP 管控（全局白/黑名单）：启用时对 /v1 与 /admin 请求按规则放行/拦截。

    关闭时（默认）零开销直接穿透。规则命中与否写进 `ip_access_logs` 供安全页查看。
    """

    def __init__(self, app, store):
        self.app, self.store = app, store

    def _log(self, ip: str, path: str, blocked: bool, reason: str, ua: str) -> None:
        try:
            with self.store.lock:
                logs = self.store.data.setdefault("ip_access_logs", [])
                logs.append({"ts": int(time.time()), "ip": ip, "path": path,
                             "blocked": bool(blocked), "reason": reason, "ua": ua[:120]})
                if len(logs) > IP_LOG_LIMIT:
                    del logs[:len(logs) - IP_LOG_LIMIT]
        except Exception:  # noqa: BLE001
            pass

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not (path.startswith("/admin") or path.startswith("/v1")):
            return await self.app(scope, receive, send)
        cfg = self.store.data.get("security") or {}
        if not cfg.get("enabled"):
            return await self.app(scope, receive, send)
        ip = client_ip_from_scope(scope)
        rules = self.store.data.get("ip_rules", [])
        allowed = evaluate(ip, rules, cfg.get("mode", "blacklist"))
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
        self._log(ip, path, not allowed, "" if allowed else "global", headers.get("user-agent", ""))
        if not allowed:
            response = JSONResponse({"detail": "来源 IP 已被安全策略拒绝"}, 403)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


def _dev_caller(req) -> bool:
    """调用方是不是「本机」。**只用于决定要不要把 dev 验证码回给响应**。

    判据是 **TCP 对端**（peer），不是 `client_ip(req)`：
    `client_ip` 会在对端落在可信代理网段时采信 `X-Forwarded-For`，
    而那正是攻击者能伪造的字段 —— 用它来判「是不是本机」等于
    「带一个 XFF: 127.0.0.1 就能拿到别人的验证码」。
    TCP 对端没法伪造，才是可用的判据。

    IPv6 回环 (`::1`) 与本机 IPv4 回环都算；`testclient` 的对端是
    `testclient`（不是 IP），那也算本机 —— 它是进程内 ASGI transport，
    请求根本没有经过网络（FastAPI 的 TestClient 就是这么发的）。

    放在模块级是为了**能被直接测**：这个判定是「dev 出口会不会把验证码
    泄露给远程调用方」的唯一闸门，藏在 `create_app()` 的闭包里就只能
    端到端测，而端到端测模拟不了「来自公网的对端」。
    """
    peer = (getattr(req, "client", None).host if getattr(req, "client", None) else "") or ""
    if peer == "testclient":
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _evict_stale_attempts(state: dict, now: int) -> int:
    """清掉**窗口已过期**的限流条目，返回清掉的条数。

    只删过期的，绝不删还在计数的 —— 这是这个函数存在的全部理由。

    早先的写法是「超过 500 条就按 window 排序削掉最老的 200 条」，
    而所有活跃条目的 window 都等于同一个 now，排序于是退化成插入序，
    正在被限流的那一条也会被削掉：换个密钥刷一轮就能把自己的限流
    清零。限流器被它要防的人关掉，等于没有。

    拆成独立函数是为了能直接测 —— 真实服务里 API Key 上限 100 枚、
    清理阈值 500，这条路径端到端根本走不到，走不到的东西就没法用
    HTTP 层的测试锁住。
    """
    expired = [k for k, v in state.items() if now - int(v.get("window", 0)) > 300]
    for key in expired:
        state.pop(key, None)
    return len(expired)

def _runtime_of(store) -> dict:
    """运行时长。优先用 store 自己的实现（SwitchStore.uptime 能给出累计值）。

    旧 `Store` 没有 uptime()——它只是个「账号存在 .info 里」的老实现，
    没有跨重启的累计概念。这里退化成「累计 == 本次会话」，
    保证接口在任何 store 下都返回同样形状的字段，前端不必分叉。
    """
    fn = getattr(store, "uptime", None)
    if callable(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            pass
    session = max(0, int(time.time() - getattr(store, "started", time.time())))
    return {"session": session, "total": session,
            "since": int(getattr(store, "started", time.time())),
            "total_since": int(getattr(store, "started", time.time()))}


def create_app(root=None, auth_dir=None, initial_key=None, admin_key=None, secure_cookie=True, store=None,
               legacy_key_configured=True):
    # store 可由调用方注入（例如 SwitchStore：账号来自 workbuddy-switch 的账号库）。
    # 不注入时保持原行为：账号来自本目录下的 .info 文件。
    if store is None:
        store = Store(root or os.environ.get("MANAGEMENT_DATA_DIR", "/data/management"), auth_dir or os.environ.get("CODEBUDDY_AUTH_DIR", "/data/auth"), initial_key if initial_key is not None else os.environ.get("CODEBUDDY2OPENAI_KEY", ""), admin_key or os.environ.get("ADMIN_KEY", ""))
    converter._check_auth = store.check_api
    converter.CONFIG.update({"desensitize": True, "no_compact": False, "log_path": None})
    browser_login = BrowserLogin(store.save_browser_account)
    pool = AccountPool(store)
    metrics = RequestMetrics()
    security = Security(store, admin_key if admin_key is not None
                        else (getattr(store, "admin_key", "") or os.environ.get("ADMIN_KEY", "")),
                        legacy_key_configured=legacy_key_configured)
    @asynccontextmanager
    async def lifespan(app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                await browser_login.cleanup()
        cleanup_task = asyncio.create_task(reap())
        async def pool_worker():
            while True:
                try:
                    await pool.tick()
                except Exception:
                    pass  # Per-account errors are persisted without credential data.
                await asyncio.sleep(60)
        pool_task = asyncio.create_task(pool_worker())

        # 账号库热加载：workbuddy-switch 里增删账号后，服务无需重启。
        # 轮询代价只有一次 stat()（指纹不变就直接返回），默认 2 秒。
        async def accounts_worker():
            interval = max(0.5, float(os.environ.get("WB_ACCOUNTS_POLL", "2") or 2))
            while True:
                try:
                    await asyncio.to_thread(store.refresh_accounts)
                except Exception:
                    pass  # 账号库暂时不可读时保持现状，下一轮再试。
                await asyncio.sleep(interval)
        accounts_task = asyncio.create_task(accounts_worker())

        try:
            yield
        finally:
            cleanup_task.cancel()
            pool_task.cancel()
            accounts_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            await browser_login.close()
            for task in (pool_task, accounts_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.security = security
    app.state.browser_login = browser_login
    app.state.pool = pool
    app.state.metrics = metrics
    app.add_middleware(AdminMiddleware)
    app.add_middleware(PoolMiddleware, pool=pool)
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    app.add_middleware(IPGuardMiddleware, store=store)
    # SPA（workbuddy-switch 前端）跨源调用 /admin/api/*：走 X-Admin-Token 头鉴权、不使用 cookie，
    # 因此可放开 CORS。最后添加 → 处于最外层，在 AdminMiddleware 之前处理预检 OPTIONS。
    # 可用 WB_ADMIN_CORS_ORIGINS 收紧来源（逗号分隔），默认放开。
    origins = [o.strip() for o in os.environ.get("WB_ADMIN_CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        # ⚠️ 必须包含 PUT —— 桌面端「服务设置 → 保存」走的就是 PUT
        # （src/lib/reverse-proxy.ts 的 saveSettings）。少了它，浏览器预检
        # 直接被拒 400，那个 PUT 根本不会发出去，界面表现是「点保存什么都不发生」，
        # 而后端日志里连一条请求都看不到 —— 因为请求确实没到达。
        #
        # 影响面只有桌面端：网页端同源、不触发预检，所以一直好用。
        # 后端测试也测不出来：TestClient 直连 ASGI，不发预检。
        #
        # 只补 PUT。不要改成 ["*"] —— 这里开的是跨源管理接口的面，
        # 凭空多放开 HEAD/TRACE 之类没有对接方的动词，只是白白扩大攻击面。
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Admin-Token", "X-CSRF-Token"],
        max_age=600,
    )

    async def payload(req):
        try:
            body = await req.json()
        except ValueError:
            raise HTTPException(400, "请求不是有效 JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "请求格式不正确")
        return body

    def log_inbound(ip: str, path: str, reason: str, ua: str, *, blocked: bool) -> None:
        """把一次入站访问写进 `ip_access_logs`（安全页看的那份）。

        与 IPGuardMiddleware 共用同一张表与上限，但**不受「IP 管控开关」影响**：
        安全策略没启用时也要留下「谁拿着错密钥在敲门」，否则这个缺口平时是隐形的。
        反过来，开关打开时由中间件统一记录，这里让路不重复写。
        """
        try:
            if (store.data.get("security") or {}).get("enabled"):
                return
            with store.lock:
                logs = store.data.setdefault("ip_access_logs", [])
                logs.append({"ts": int(time.time()), "ip": ip, "path": path,
                             "blocked": bool(blocked), "reason": reason, "ua": ua[:120]})
                if len(logs) > IP_LOG_LIMIT:
                    del logs[:len(logs) - IP_LOG_LIMIT]
        except Exception:  # noqa: BLE001
            pass

    # ── 角色依赖：viewer（任意身份）/ admin（管理员）/ session_admin（仅会话管理员）──
    def viewer(req: Request):
        return security.current_user(req)

    def admin(req: Request):
        return security.require_admin(req)

    def session_admin(req: Request):
        return security.require_session_admin(req)

    @app.get("/")
    async def home():
        return RedirectResponse("/admin/", 302)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/admin")
    async def redirect_admin():
        return RedirectResponse("/admin/", 302)

    @app.get("/admin/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/admin/assets/{filename}")
    async def asset(filename: str):
        if filename not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / filename)

    # 图标：没有它每个页面的控制台都会多一条 404，做前端检查时很容易
    # 被误当成「页面有资源加载失败」。用 SVG，省一个二进制文件。
    @app.get("/favicon.ico")
    async def favicon():
        return FileResponse(ASSETS / "favicon.svg", media_type="image/svg+xml")

    # 用户中心页面。
    #
    # 与 /login、/register 同形：服务端不做跳转判断。
    # 「未登录该不该看到用户中心」由页面 JS 读 /login/api/me 后自己决定；
    # 服务端插手会制造两套互相打架的跳转规则。
    #
    # 本页 HTML 是纯骨架（匿名 / 用户1 / 用户2 三态逐字节相同），
    # 不内联任何身份数据，数据一律由 me.js 运行时取。
    @app.get("/me")
    async def me_page():
        return _login_asset("me.html")

    @app.get("/me/")
    async def me_page_slash():
        return _login_asset("me.html")

    @app.get("/me/assets/{filename}")
    async def me_asset(filename: str):
        # 白名单显式枚举，与 /login/assets、/register/assets 同口径：
        # 直接用用户传来的名字拼路径就是目录穿越（../../etc/passwd）。
        if filename not in {"me.js", "me.css"}:
            raise HTTPException(404)
        return _login_asset(filename)

    # 用户登录页。
    #
    # `/login` 是终端用户的登录页，和 `/admin/`（控制台）不是一回事。
    #
    # ⚠️ 刻意不做「已登录就 302 跳走」：那是前端的判断（它知道要跳哪、
    # 带了什么 query），服务端插一脚只会制造两套互相打架的跳转规则。
    @app.get("/login")
    async def login_page():
        return _login_asset("login.html")

    @app.get("/login/")
    async def login_page_slash():
        return _login_asset("login.html")

    @app.get("/login/assets/{filename}")
    async def login_asset(filename: str):
        # 白名单：直接用用户传来的名字拼路径就是目录穿越。
        if filename not in {"login.js", "login.css"}:
            raise HTTPException(404)
        return _login_asset(filename)

    def _login_asset(filename: str):
        path = ASSETS / filename
        if not path.is_file():
            raise HTTPException(404, "登录页资源尚未生成")
        return FileResponse(path)

    # 注册页。
    #
    # 与 /login 同形，服务端不做任何跳转判断。
    # 「已登录该不该看到注册页」「本站允不允许注册」全部由前端 JS 决定。
    #
    # `/register` 在任何情况下都不做 302，包括已登录、站点关闭注册、
    # 邮箱域名不被允许 —— 这些都由前端读接口后自己决定渲染什么。
    @app.get("/register")
    async def register_page():
        return _login_asset("register.html")

    @app.get("/register/")
    async def register_page_slash():
        return _login_asset("register.html")

    @app.get("/register/assets/{filename}")
    async def register_asset(filename: str):
        # 白名单显式枚举，与 /login/assets、/portal/assets 同口径：
        # 直接用用户传来的名字拼路径就是目录穿越（`../../etc/passwd`）。
        #
        # 为什么只有 register.js、没有 register.css：register.html 刻意复用
        # `/login/assets/login.css`（见该文件里的注释）—— 两个页面是同一套视觉，
        # 复制成两份转手就会漂移。所以不要在这里放开 login.css，
        # 那会同时给出第二个 css 真相源。
        if filename not in {"register.js"}:
            raise HTTPException(404)
        return _login_asset(filename)

    # ── Cookie 的 Secure 标志：跟着请求实际是不是 HTTPS 走 ──────
    #
    # 原先是硬编码 True。后果不是「更安全」，而是登录直接失效：
    # 客户端（浏览器、httpx）拒绝把 Secure cookie 发回明文 HTTP，于是
    # 登录返回 200、cookie 也下发了，但后续每个请求都 401 ——
    # 界面表现是「登录成功后立刻弹回登录页」，且没有任何报错可查。
    #
    # 本服务默认就监听 127.0.0.1，明文是它的正常形态（见 serve.py 的
    # `--host` 默认值），所以这不是边缘场景，是主路径。
    #
    # 仍然保留 Secure：只要经过 HTTPS 反代（VPS 上挂 Caddy / Nginx），
    # 或者显式配 `WB_SECURE_COOKIE=1`，cookie 就带 Secure，
    # 不会为了本机调试方便而把线上部署的安全性降下来。
    def _cookie_secure(req: Request) -> bool:
        override = os.environ.get("WB_SECURE_COOKIE", "").strip().lower()
        if override in ("1", "true", "yes", "on"):
            return True
        if override in ("0", "false", "no", "off"):
            return False
        # X-Forwarded-Proto 由反代设置；uvicorn 的 proxy_headers 也会读它。
        proto = (req.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
        return req.url.scheme == "https"

    def _login_response(csrf: str, sid: str, req: Request) -> JSONResponse:
        response = JSONResponse({"csrf": csrf, "token": sid})
        response.set_cookie(COOKIE, sid, secure=_cookie_secure(req), httponly=True,
                            samesite="strict", path="/admin", max_age=12 * 3600)
        return response

    @app.post("/admin/api/login")
    async def login(req: Request):
        body = await payload(req)
        ip = client_ip(req)
        username = str(body.get("username") or "").strip()[:64]

        # 管理令牌（wbt_…）换会话：让浏览器控制台也能用令牌进门。
        # 放在最前面判，因为令牌登录的请求体里既没有 key 也没有 username，
        # 落到下面那个兼容分支只会得到「管理密钥不正确」这种驴唇不对马嘴的报错。
        #
        # 安全性上没放宽：令牌本来就能用 `Authorization: Bearer` 调 API，所以
        # 「能进控制台」没有多给任何权限，角色照样按令牌 scope 定（只读令牌进得去、
        # 改不动东西）。而「改用户 / 改令牌」这类高危接口仍然拒绝——见
        # `require_session_admin`，它靠会话记录里的 token_id 认出这条路。
        if "token" in body:
            value = str(body.get("token") or "").strip()
            if security.login_blocked(ip):
                raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")
            row = security.resolve_token(value)
            if not row or not security.token_usable(row):
                security.record_fail(ip)
                security.audit({"username": "(管理令牌)"}, "login_failed", "",
                               f"管理令牌无效、已停用或已过期；来源 {ip}")
                raise HTTPException(401, "管理令牌无效、已停用或已过期")
            security.clear_fail(ip)
            actor = f"token:{row['name']}"
            sid, csrf = security.start_session(actor, scope_to_role(row["scope"]),
                                               via="token", token_id=row["id"])
            security.audit({"username": actor}, "login", row["name"],
                           f"管理令牌登录；来源 {ip}")
            # 令牌换来的会话照常种 cookie：内置静态后台就是靠它维持登录态的
            # （见 tests/test_console_login.py::test_token_login_grants_console_session）。
            # 曾经想「令牌是凭据、不必再种 cookie」，那会把控制台的令牌登录打断 ——
            # 登录返回 200，之后每个请求 401。
            return _login_response(csrf, sid, req)

        if "key" in body or not username:
            # 兼容旧形态：单密钥 ADMIN_KEY 登录（内置静态后台）。保留原有「按 IP 8 次锁定」行为。
            value = body.get("key")
            now = time.time()
            with store.lock:
                store.attempts = {k: v for k, v in store.attempts.items() if v[-1] > now - 600}
                failures = [t for t in store.attempts.get(ip, []) if t > now - 600]
                if len(failures) >= 8:
                    raise HTTPException(429, "尝试次数过多，请 10 分钟后重试")
                if not security.verify_legacy_key(value):
                    store.attempts[ip] = failures + [now]
                    raise HTTPException(401, "管理密钥不正确")
                store.attempts.pop(ip, None)
            sid, csrf = security.start_session("admin", "admin", via="session")
            security.audit({"username": "admin"}, "login", "admin", f"管理密钥登录；来源 {ip}")
            return _login_response(csrf, sid, req)

        # 多用户：用户名 + 口令（PBKDF2 校验）。
        password = str(body.get("password") or "")
        if security.login_blocked(ip, username):
            raise HTTPException(429, "失败次数过多，请 10 分钟后再试")
        user = security.verify_login(username, password)
        if not user:
            security.record_fail(ip, username)
            security.audit({"username": username or "(空)"}, "login_failed", username, f"来源 {ip}")
            raise HTTPException(401, "用户名或密码错误")
        security.clear_fail(ip, username)
        sid, csrf = security.start_session(user["username"], user["role"], via="session")
        security.audit(user, "login", user["username"], f"来源 {ip}")
        return _login_response(csrf, sid, req)

    @app.get("/admin/api/session")
    async def session(req: Request):
        user = security.current_user(req)
        return {"csrf": security.session_csrf(req), "username": user["username"],
                "role": user["role"], "via": user.get("via")}

    @app.get("/admin/api/login-options")
    async def login_options():
        """登录页能摆哪些入口——**不需要登录**，登录页本身要据此渲染。

        只暴露「这个入口在当前部署下有没有用」，不含任何凭据、也不透露用户名。
        """
        return {"legacy_key": security.legacy_key_enabled(),
                "tokens": security.has_tokens()}

    @app.get("/admin/api/me")
    async def me(req: Request):
        user = viewer(req)
        return {"username": user["username"], "role": user["role"], "via": user.get("via")}

    @app.post("/admin/api/logout")
    async def logout(req: Request):
        """登出：**必须幂等**。

        原先第一行是 `security.current_user(req)`，而它在本机已经没登录时会直接
        抛 401。于是「登出」这个动作本身变成了「只有已登录才能登出」——
        听起来像句废话，实际会造成两个真问题：

          1. 桌面端点「退出」时，token 已经被上次登出清掉了，这一发就是 401；
             前端把它当失败弹 toast，用户看到的是「退不出去 / 报错」。
          2. 会话已经过期 / 被吊销之后再点退出，同样 401。

        登出的语义是「把当前的登录态清干净」，**没登录本来就是目标状态**，
        没有什么可失败的。所以这里不再要求先通过鉴权：拿到什么就清什么，
        永远回 200。cookie 照删（清掉可能残留的那一份）。
        """
        sid, _ = bearer_session(req)
        if sid:
            # cancel_owner / end_session 都是按 sid 清，sid 不存在时是空操作。
            await browser_login.cancel_owner(digest(sid))
            security.end_session(req)
        response = JSONResponse({"ok": True})
        # 无论上面走没走到，都把 cookie 删掉——浏览器里可能还留着上一次的。
        security.delete_cookie(response, secure_cookie)
        return response

    @app.post("/admin/api/sessions/revoke")
    async def revoke_sessions(req: Request):
        """吊销**当前用户**的全部会话（含本次），并清除本机 cookie。"""
        actor = session_admin(req)
        n = security.revoke_user_sessions(actor["username"])
        security.audit(actor, "revoke_sessions", actor["username"], f"吊销 {n} 个会话；来源 {client_ip(req)}")
        response = JSONResponse({"ok": True, "revoked": n, "relogin_required": True})
        security.delete_cookie(response, secure_cookie)
        return response

    # ── 管理用户（仅会话管理员）────────────────────────────────
    @app.get("/admin/api/users")
    async def list_users(req: Request):
        viewer(req)
        return security.public_users()

    @app.post("/admin/api/users")
    async def add_user(req: Request):
        actor = session_admin(req)
        body = await payload(req)
        result = security.add_user(body.get("username", ""), body.get("password", ""), body.get("role", "viewer"))
        security.audit(actor, "add_user", result["username"],
                       f"角色={result['role']}；来源 {client_ip(req)}")
        return result

    @app.patch("/admin/api/users/{username}")
    async def edit_user(username: str, req: Request):
        actor = session_admin(req)
        body = await payload(req)
        result = security.update_user(username, body.get("password"), body.get("role"))
        security.audit(actor, "update_user", username, f"来源 {client_ip(req)}")
        result["relogin_required"] = result["sessions_revoked"] and username == actor["username"]
        if result["relogin_required"]:
            security.end_session(req)
        return result

    @app.delete("/admin/api/users/{username}")
    async def remove_user(username: str, req: Request):
        actor = session_admin(req)
        result = security.delete_user(username, actor["username"])
        security.audit(actor, "delete_user", username, f"来源 {client_ip(req)}")
        return result

    # ── 作用域化管理令牌（wbt_；仅会话管理员可增删改）─────────
    @app.get("/admin/api/tokens")
    async def list_tokens(req: Request):
        # 令牌自身的接口一律只收会话管理员：泄露的令牌不能用来枚举其它令牌。
        session_admin(req)
        return security.list_tokens()

    @app.post("/admin/api/tokens")
    async def add_token(req: Request):
        actor = session_admin(req)
        body = await payload(req)
        created = security.create_token(body.get("name", ""), body.get("scope", "readonly"),
                                        body.get("expires_at"), created_by=actor["username"])
        security.audit(actor, "create_token", created["name"],
                       f"scope={created['scope']}；来源 {client_ip(req)}")
        return created

    @app.patch("/admin/api/tokens/{tid}")
    async def edit_token(tid: str, req: Request):
        actor = session_admin(req)
        body = await payload(req)
        updated = security.update_token(tid, body)
        security.audit(actor, "update_token", updated["name"], f"来源 {client_ip(req)}")
        return updated

    @app.delete("/admin/api/tokens/{tid}")
    async def remove_token(tid: str, req: Request):
        actor = session_admin(req)
        result = security.delete_token(tid)
        security.audit(actor, "delete_token", tid, f"来源 {client_ip(req)}")
        return result

    # ── 入站 IP 管控 ────────────────────────────────────────────
    @app.get("/admin/api/security/config")
    async def security_config(req: Request):
        viewer(req)
        with store.lock:
            return dict(store.data.get("security") or {"enabled": False, "mode": "blacklist"})

    @app.post("/admin/api/security/config")
    async def write_security_config(req: Request):
        actor = session_admin(req)
        body = await payload(req)
        mode = body.get("mode", "blacklist")
        if mode not in ("whitelist", "blacklist"):
            raise HTTPException(400, "模式无效")
        cfg = {"enabled": bool(body.get("enabled")), "mode": mode}
        with store.lock:
            store.data["security"] = cfg
            store.save()
        security.audit(actor, "update_security", "",
                       f"enabled={cfg['enabled']} mode={cfg['mode']}；来源 {client_ip(req)}")
        return cfg

    @app.get("/admin/api/security/rules")
    async def list_ip_rules(req: Request):
        viewer(req)
        with store.lock:
            return list(store.data.get("ip_rules", []))

    @app.post("/admin/api/security/rules")
    async def add_ip_rule(req: Request):
        actor = session_admin(req)
        body = await payload(req)
        kind = body.get("kind")
        cidr = str(body.get("cidr") or "").strip()
        if kind not in ("allow", "deny"):
            raise HTTPException(400, "规则类型无效")
        if not valid_cidr(cidr):
            raise HTTPException(400, f"非法的 IP / CIDR：{cidr}")
        with store.lock:
            rid = secrets.token_hex(6)
            rule = {"id": rid, "kind": kind, "cidr": cidr,
                    "note": str(body.get("note") or "")[:120], "created_at": int(time.time())}
            store.data.setdefault("ip_rules", []).insert(0, rule)
            store.save()
        security.audit(actor, "add_ip_rule", cidr, f"{kind}；来源 {client_ip(req)}")
        return rule

    @app.delete("/admin/api/security/rules/{rid}")
    async def remove_ip_rule(rid: str, req: Request):
        actor = session_admin(req)
        with store.lock:
            rules = store.data.get("ip_rules", [])
            if not any(r["id"] == rid for r in rules):
                raise HTTPException(404, "规则不存在")
            store.data["ip_rules"] = [r for r in rules if r["id"] != rid]
            store.save()
        security.audit(actor, "delete_ip_rule", rid, f"来源 {client_ip(req)}")
        return {"ok": True}

    @app.get("/admin/api/security/logs")
    async def ip_access_logs(req: Request, limit: int = 200):
        viewer(req)
        with store.lock:
            logs = list(store.data.get("ip_access_logs", []))
        logs.reverse()
        return logs[: max(1, min(IP_LOG_LIMIT, limit))]

    @app.post("/admin/api/security/logs/clear")
    async def clear_ip_logs(req: Request):
        actor = session_admin(req)
        with store.lock:
            store.data["ip_access_logs"] = []
            store.save()
        security.audit(actor, "clear_ip_logs", "", f"来源 {client_ip(req)}")
        return {"ok": True}

    @app.get("/admin/api/audit-logs")
    async def audit_logs(req: Request, limit: int = 200):
        viewer(req)
        return security.audit_logs(limit)

    @app.post("/admin/api/audit-logs/clear")
    async def clear_audit_logs(req: Request):
        actor = session_admin(req)
        removed = security.clear_audit_logs()
        # 清空动作本身要留痕：先清空再记录，新日志里就只剩这一条。
        security.audit(actor, "clear_audit_logs", "", f"清空 {removed} 条；来源 {client_ip(req)}")
        return {"ok": True, "removed": removed}

    @app.post("/admin/api/oauth/start")
    async def oauth_start(req: Request):
        admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), None)
        owner, _ = bearer_session(req)
        try:
            return await browser_login.start(digest(owner), name)
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权服务连接失败，请稍后重试")

    @app.post("/admin/api/oauth/{fid}/poll")
    async def oauth_poll(fid: str, req: Request):
        admin(req)
        owner, _ = bearer_session(req)
        try:
            return await browser_login.poll(fid, digest(owner))
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "授权状态暂时无法获取，请稍后重试")

    @app.delete("/admin/api/oauth/{fid}")
    async def oauth_cancel(fid: str, req: Request):
        admin(req)
        owner, _ = bearer_session(req)
        return await browser_login.cancel(fid, digest(owner))

    def _pool_view(store) -> dict:
        """`pool` 段 + 一条**上游读取状态**。

        `wb2api_error` 非空 = 这次（或最近一次）读上游 `/status` 失败了，
        界面据此把「冷却状态未知」讲出来，而不是画成「没有冷却」。

        放在 pool 段而不是每行账号里：这个错误是**整批共享**的 ——
        `upstream_cooldowns()` 一次调用拿整张 uid→冷却 的表
        （见 pool.py 里那句「只读一次，给整批账号用」）。
        逐行重复同一个错误字符串既冗余，又会让「是不是每个号各有各的错」
        这种误读变得可能。

        注意：**这里不碰 `up_cooling`**。那仍然是 None（未知），
        绝不因为读不到就退化成 False（无冷却）—— 那正是这个缺陷的成因。
        """
        view = dict(store.data["pool"])
        view["wb2api_error"] = pool.upstream_error()
        return view


    @app.get("/admin/api/overview")
    async def overview(req: Request):
        viewer(req)
        with store.lock:
            return {"accounts": pool.rows(store.account_rows()),
                    "pool": _pool_view(store),
                    "metrics": metrics.snapshot(), "keys": keysvc.list_keys(store),
                    "models": converter.get_available_models(),
                    # 客户端没带 model 时反代替它选的那一个（已按本机可用清单解析过）。
                    # 界面拿它当测试表单的默认选中项，也是「默认模型」这件事的唯一出处。
                    "default_model": converter.resolve_default_model(),
                    "checkin_logs": pool.checkin_logs(20),
                    "request_logs": pool.request_logs(50),
                    # 分组也放进来：密钥表单要能勾选分组，密钥列表要能把 id 显示成组名。
                    # 单独再发一次 /admin/api/groups 会让「打开密钥页」多一次往返，
                    # 而这份清单很小（≤32 条）。
                    "groups": groups.list_groups(store),
                    # `uptime` 保留为整数秒（老前端/老脚本在用），
                    # 新增 `runtime` 给出 session / total 两个口径，见 SwitchStore.uptime()。
                    "uptime": int(time.time() - store.started),
                    # 两个 store 实现都要能用：SwitchStore 有累计运行时间，
                    # 旧的 Store（账号来自 .info 文件）没有——不能让它 AttributeError，
                    # 否则所有走旧 store 的接口一起 500。缺的时候退化成「只有本次会话」。
                    "runtime": _runtime_of(store),
                    "events": list(store.events)}

    @app.post("/admin/api/accounts/{aid}/actions/{action}")
    async def account_action(aid: str, action: str, req: Request):
        admin(req)
        if action not in ("refresh", "status", "checkin", "travel", "travel-claim", "active"):
            raise HTTPException(404)
        live_accounts(store)
        return await asyncio.to_thread(pool.operate, aid, action)

    @app.post("/admin/api/pool/actions/{action}")
    async def pool_action(action: str, req: Request):
        admin(req)
        if action not in ("status", "checkin", "travel", "travel-claim", "active"):
            raise HTTPException(404)
        return await pool.batch(action)

    @app.patch("/admin/api/pool/settings")
    async def pool_settings(req: Request):
        actor = admin(req)
        body = await payload(req)
        import re
        with store.lock:
            settings = dict(store.data["pool"])
            if "routing" in body:
                if body["routing"] not in ("manual", "round_robin", "least_used"):
                    raise HTTPException(400, "调度方式无效")
                settings["routing"] = body["routing"]
            if "spread" in body:
                if type(body["spread"]) is not bool:
                    raise HTTPException(400, "轮换驻留开关无效")
                settings["spread"] = body["spread"]
            if "spread_minutes" in body:
                try:
                    minutes = int(body["spread_minutes"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "驻留时长需为整数分钟")
                if not 1 <= minutes <= 1440:
                    raise HTTPException(400, "驻留时长需在 1–1440 分钟之间")
                settings["spread_minutes"] = minutes
            if "auto_checkin" in body:
                if type(body["auto_checkin"]) is not bool:
                    raise HTTPException(400, "签到开关无效")
                settings["auto_checkin"] = body["auto_checkin"]
            if "checkin_time" in body:
                if not isinstance(body["checkin_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["checkin_time"]):
                    raise HTTPException(400, "请选择有效的签到时间")
                settings["checkin_time"] = body["checkin_time"]
            if "auto_travel" in body:
                if type(body["auto_travel"]) is not bool:
                    raise HTTPException(400, "派猫猫旅行开关无效")
                settings["auto_travel"] = body["auto_travel"]
            if "travel_time" in body:
                if not isinstance(body["travel_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["travel_time"]):
                    raise HTTPException(400, "请选择有效的派旅行时间")
                settings["travel_time"] = body["travel_time"]
            if "auto_keepalive" in body:
                if type(body["auto_keepalive"]) is not bool:
                    raise HTTPException(400, "保活开关无效")
                settings["auto_keepalive"] = body["auto_keepalive"]
            if "keepalive_hours" in body:
                try:
                    hours = int(body["keepalive_hours"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "保活提前量需为整数小时")
                # 上限 168 小时（一周）：比 token 自身的寿命还长的提前量没有意义，
                # 只会让每个 tick 都去刷一遍。
                if not 1 <= hours <= 168:
                    raise HTTPException(400, "保活提前量需在 1–168 小时之间")
                settings["keepalive_hours"] = hours
            if "auto_active" in body:
                if type(body["auto_active"]) is not bool:
                    raise HTTPException(400, "活跃上报开关无效")
                settings["auto_active"] = body["auto_active"]
            if "active_time" in body:
                if not isinstance(body["active_time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["active_time"]):
                    raise HTTPException(400, "请选择有效的活跃上报时间")
                settings["active_time"] = body["active_time"]
            if "active_reports" in body:
                try:
                    reports = int(body["active_reports"])
                except (TypeError, ValueError):
                    raise HTTPException(400, "上报条数需为整数")
                # 上限 5：领养猫的对话量门槛就是 5 次，报更多没有额外收益，
                # 只是把同一个账号在同一分钟里对着上游多打几次。
                if not 1 <= reports <= 5:
                    raise HTTPException(400, "上报条数需在 1–5 之间")
                settings["active_reports"] = reports
            # 上游 workbuddy2api 接入点：填了才去读它的模型级冷却。
            # 空串是合法值（= 不读），所以不能用「真值判断」跳过——
            # 那样用户就永远清不掉这个配置了。
            if "wb2api_base" in body:
                base = body["wb2api_base"]
                if not isinstance(base, str):
                    raise HTTPException(400, "上游地址需为字符串")
                base = base.strip().rstrip("/")
                if base and not re.fullmatch(r"https?://[^\s/]+(?::\d+)?(?:/[^\s]*)?", base):
                    raise HTTPException(400, "上游地址需形如 http://127.0.0.1:7863")
                settings["wb2api_base"] = base
            if "wb2api_key" in body:
                key = body["wb2api_key"]
                if not isinstance(key, str):
                    raise HTTPException(400, "上游密钥需为字符串")
                settings["wb2api_key"] = key.strip()
            store.data["pool"] = settings
            store.save()
        # 改了地址/密钥必须立刻生效，不能还拿旧上游的缓存糊弄界面。
        pool._upstream_cache = ({}, None, 0.0)
        editable = ('routing', 'spread', 'spread_minutes', 'auto_checkin', 'checkin_time',
                    'auto_travel', 'travel_time', 'auto_keepalive', 'keepalive_hours',
                    'auto_active', 'active_time', 'active_reports',
                    'wb2api_base', 'wb2api_key')
        security.audit(actor, "update_pool_settings", "",
                       f"字段={'、'.join(sorted(k for k in body if k in editable))}；来源 {client_ip(req)}")
        return {"ok": True, "pool": settings}

    @app.get("/admin/api/pool/checkin-logs")
    async def checkin_logs(req: Request, limit: int = 200, kind: str = ""):
        """任务流水：签到 / 旅行 / 保活，新的在前。`kind` 为空时给全部。

        路径名保留 checkin-logs：它是最早的名字，绑定它的地方不少；改名的收益
        只是一个更好听的路径，代价是所有调用方都要跟着动。
        """
        viewer(req)
        return pool.task_logs(limit, kind or None)

    @app.get("/admin/api/request-logs")
    async def request_logs(req: Request, limit: int = 200):
        """每次 /v1 请求用了哪个账号、什么模型、扣了多少。

        与指标一样是内存环形缓冲：服务重启即清零，不写 state.json。
        """
        viewer(req)
        return pool.request_logs(limit)

    @app.get("/admin/api/usage/stats")
    async def usage_stats(req: Request, days: int = 30, hours: int = 24):
        """用量聚合（按天 / 按小时）：调用数、失败率、token、积分、耗时与首字延迟。

        与 request-logs 的分工：那边是「最近 300 条的明细」，这边是「长期的分桶计数」，
        所以这一份会落进 state.json（由池的 tick 每分钟兜底刷盘），重启不丢。
        """
        viewer(req)
        return usage.snapshot(store, days=days, hours=hours)

    @app.get("/v1/usage")
    async def gateway_key_usage(req: Request):
        """网关密钥自查：已用 / 剩余额度、有效期、模型范围、IP 绑定情况。

        只读 —— 不查上游、不产生计费调用，token 与积分一分不记。
        但**要留下痕迹**：鉴权失败也记一条入站日志与 0 token 的请求日志，
        否则「谁在拿错密钥反复敲门」在后台完全看不见。
        """
        auth = req.headers.get("authorization") or ""
        x_api_key = req.headers.get("x-api-key") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = token or x_api_key.strip()
        ip = client_ip(req)
        ua = (req.headers.get("user-agent") or "")[:120]

        def deny(message: str, reason: str):
            log_inbound(ip, "/v1/usage", reason, ua, blocked=True)
            pool.log_request(None, None, 401, False, 0, 0, 0, "rejected", "usage")
            return JSONResponse(
                {"error": {"message": message, "type": "auth_error", "code": "invalid_api_key"}}, 401)

        if not token:
            return deny("缺少 API Key", "missing")
        try:
            key = store.check_api(auth, x_api_key)
        except HTTPException:
            return deny("invalid api key", "invalid")
        log_inbound(ip, "/v1/usage", "", ua, blocked=False)
        # 0 token 的调用日志：只为留下「最近使用」与来源 IP，不计任何用量。
        pool.log_request(None, None, 200, True, 0, 0, 0, "success", "usage")

        if key is None:  # 临时测试密钥：无策略，直接告知
            return {"object": "usage", "key": None, "unrestricted": True,
                    "note": "临时测试密钥没有策略限制"}

        quota = int(key.get("quota") or 0)
        used_tokens = int(key.get("used_tokens") or 0)
        quota_credit = float(key.get("quota_credit") or 0)
        used_credit = round(float(key.get("used_credit") or 0), 6)
        return {
            "object": "usage",
            "key": {
                "id": key.get("id"),
                "name": key.get("name", ""),
                "prefix": key.get("prefix", ""),
                "hint": key.get("hint", ""),
                "enabled": bool(key.get("enabled", True)),
                "expires_at": key.get("expires_at"),
                "realm": keysvc.norm_realm(key.get("realm")),
                "models": list(key.get("models") or []),
                "created": key.get("created"),
                "last_used_at": key.get("last_used_at"),
            },
            "tokens": {
                "limit": quota,
                "used": used_tokens,
                # 0 表示不限；不限时不给 remaining，免得下游把 null 当成 0 处理
                "remaining": max(0, quota - used_tokens) if quota else None,
            },
            "credit": {
                "limit": quota_credit,
                "used": used_credit,
                "remaining": round(max(0.0, quota_credit - used_credit), 6) if quota_credit else None,
            },
            "ips": {
                "max": int(key.get("max_ips") or 0),
                "allowlist": list(key.get("ip_allowlist") or []),
                "bound": list((store.data.get("api_key_ips") or {}).get(key.get("id")) or []),
            },
        }

    @app.get("/admin/api/model-aliases")
    async def get_model_aliases(req: Request):
        """模型别名表 + 可用于校验的已知模型清单。"""
        viewer(req)
        known = converter.get_available_models()
        aliases = keysvc.model_aliases(store)
        return {
            "aliases": aliases,
            "known_models": known,
            "default_model": converter.resolve_default_model(),
            # 别名指向的模型不在清单里（多半是拼错，或本机还没下发这个模型）
            "unknown_targets": sorted({
                target for target in aliases.values()
                if known and target not in set(known)
            }),
        }

    @app.post("/admin/api/model-aliases")
    async def set_model_aliases(req: Request):
        """整表替换别名映射。传空表即清空。

        设计成整表替换而不是逐条增删：别名表很小（十几条），整表提交能避免
        「先删后加」中间态被读到的竞态，前端也好做「一个输入框一行」的编辑。
        """
        actor = admin(req)
        body = await payload(req)
        raw = body.get("aliases")
        if not isinstance(raw, dict):
            raise HTTPException(400, "aliases 必须是对象")
        cleaned: dict[str, str] = {}
        for key, value in raw.items():
            name, target = str(key).strip(), str(value).strip()
            if not name and not target:
                continue
            if not name or not target:
                raise HTTPException(400, "别名与目标模型都必须填写")
            if name == target:
                continue
            cleaned[name[:64]] = target[:64]
        if len(cleaned) > 200:
            raise HTTPException(400, "别名最多 200 条")
        with store.lock:
            store.data["model_aliases"] = cleaned
            store.save()
        security.audit(actor, "set_model_aliases", "", f"{len(cleaned)} 条；来源 {client_ip(req)}")
        return {"ok": True, "aliases": cleaned}

    @app.get("/admin/api/models")
    async def model_catalog(req: Request):
        """模型中心：本机可用模型的完整信息（上下文长度 / 最大输出 / 思考档位 / 积分单价）。

        数据源与 `/v1/models` 是同一份产品配置，只是不裁字段。额外补两样运营要看的东西：
          · `aliases`：哪些别名指向它——一眼看出「gpt-4o 实跑 glm-5.2」
          · `key_count`：有多少枚密钥的白名单里含它——决定某个模型能不能安全停用
        """
        viewer(req)
        catalog = converter.get_model_catalog()
        aliases = keysvc.model_aliases(store)
        reverse: dict[str, list[str]] = {}
        for alias, target in aliases.items():
            reverse.setdefault(target, []).append(alias)

        with store.lock:
            keys = list(store.data.get("keys", {}).values())

        for entry in catalog:
            mid = entry["id"]
            entry["aliases"] = sorted(reverse.get(mid, []))
            entry["key_count"] = sum(1 for item in keys if mid in (item.get("models") or []))

        source = converter._find_workbuddy_product_json()
        return {
            "models": catalog,
            "total": len(catalog),
            "default_model": converter.resolve_default_model(),
            "aliases": aliases,
            # 清单到底读的哪一份配置：产品配置有「装机快照」与「运行时缓存」两份，
            # 排「为什么看不到某个模型」时第一眼就要看这个。
            "source": str(source) if source else "",
        }

    @app.get("/admin/api/accounts/credits-snapshot")
    async def credits_snapshot(req: Request):
        """已登记积分的**只读快照**——给看板 / 对账脚本用。

        特点（与 `/admin/api/overview` 的区别）：**不查上游**。overview 会带上池的
        实时状态与签到信息；这里只是把 `account_status` 里上一次登记到的积分数
        原样摊平，所以它是可以高频轮询的。

        每行带 `known`：这个 uid 现在还**真实存在**于账号库吗？对账脚本靠它区分
        「还在的账号」与「只剩历史记录的 uid」——早期版本两者混在一起，
        对账时总分对不上却查不出原因。
        """
        viewer(req)
        rows = pool.rows(store.account_rows())
        out = []
        remaining_total = 0.0
        known_remaining = 0.0
        for row in rows:
            remaining = row.get("remaining")
            known = row.get("status") != "missing"
            credit = float(remaining) if isinstance(remaining, (int, float)) else None
            if credit is not None:
                remaining_total += credit
                if known:
                    known_remaining += credit
            out.append({
                "id": row.get("id"),
                "name": row.get("name"),
                "known": known,
                "enabled": bool(row.get("enabled", True)),
                "pool_state": row.get("pool_state"),
                "realm": row.get("realm"),
                "remaining": credit,
                "capacity": row.get("capacity"),
                "used": row.get("used"),
                "packages": row.get("packages"),
                "credits_updated": row.get("credits_updated"),
                "credits_stale": bool(row.get("credits_stale")),
                "today_checked_in": bool(row.get("today_checked_in")),
                "expires_at": row.get("expires_at"),
                "expired": bool(row.get("expired")),
                "request_count": row.get("request_count", 0),
            })
        return {
            "generated_at": int(time.time()),
            "rows": out,
            "known_count": sum(1 for r in out if r["known"]),
            "total_count": len(out),
            "totals": {
                "remaining": round(remaining_total, 6),
                # 被服务侧移除 / 账号库里已消失的 uid 不计入这一项
                "known_remaining": round(known_remaining, 6),
            },
        }

    @app.get("/admin/api/keys/{kid}/export")
    async def export_key(kid: str, req: Request):
        """把一枚密钥导出成下游客户端的配置片段（cc-switch / ZCode）。

        纯计算，**不写任何文件、不碰客户端配置**——返回值只交给前端展示/复制。

        反向的「导入」**有意没有实现**：上游 workbuddy-manager 的做法是结束
        用户正在运行的 cc-switch / ZCode 进程，再改写它们的 SQLite 库与
        `provider_config.json`。那是动别人软件的配置，风险与「读一份配置生成片段」
        完全不在一个量级，需要用户明确同意后才谈得上做。

        查询参数：`client`（ccswitch|zcode）、`app`（claude|codex，仅 ccswitch）、
        `base_url`（面板对外地址，默认取本机 127.0.0.1:端口）、`provider_id`、
        `name`。
        """
        actor = viewer(req)
        client = (req.query_params.get("client") or "ccswitch").strip().lower()
        if client not in keyexport.CLIENTS:
            raise HTTPException(400, f"client 必须是 {keyexport.CLIENTS} 之一")
        with store.lock:
            item = store.data.get("keys", {}).get(kid)
            if not item:
                raise HTTPException(404, "密钥不存在")
            view = keysvc.public_key(kid, item)
            host = str(req.base_url).rstrip("/")
        try:
            result = keyexport.build(
                client=client,
                key=view,
                name=(req.query_params.get("name") or view.get("name") or "").strip(),
                base_url=(req.query_params.get("base_url") or host).strip(),
                app=(req.query_params.get("app") or "").strip().lower() or None,
                provider_id=(req.query_params.get("provider_id") or "").strip() or None,
                default_model=converter.resolve_default_model(),
                known_models=converter.get_available_models(),
                # 上下文长度来自模型中心，让 ZCode 那边能填上正确的窗口大小
                context_windows={m["id"]: m["context_window"]
                                 for m in converter.get_model_catalog()
                                 if m.get("context_window")},
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        result["key_id"] = kid
        # 导出含有明文密钥，属于敏感动作，记一笔（不记内容）
        security.audit(actor, "export_key", view.get("name") or kid,
                       f"client={client} app={result.get('app','')}；来源 {client_ip(req)}")
        return result

    # ── 上游分组 ──────────────────────────────────────────────
    # 一整套 CRUD 都用 `admin(req)`（读写都要权限）：分组的成员构成直接决定
    # 「哪个密钥能用哪些账号」，只读角色看得到就够了，改必须是有权限的人。

    @app.get("/admin/api/groups")
    async def list_groups(req: Request):
        """分组清单 + 可选成员账号清单（界面上的勾选框直接用）。"""
        viewer(req)
        rows = pool.rows(store.account_rows())
        return {
            "groups": groups.list_groups(store),
            "accounts": [{"id": row["id"], "name": row.get("name") or row["id"],
                          "pool_state": row.get("pool_state"),
                          "realm": row.get("realm")} for row in rows],
            "routings": list(groups.ROUTINGS),
        }

    @app.post("/admin/api/groups")
    async def create_group(req: Request):
        actor = admin(req)
        body = await payload(req)
        try:
            created = groups.create_group(
                store, body.get("name"),
                description=body.get("description") or "",
                accounts=body.get("accounts"),
                routing=body.get("routing") or "",
                enabled=body.get("enabled", True),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        security.audit(actor, "create_group", created["name"], f"来源 {client_ip(req)}")
        return {"ok": True, **created, "groups": groups.list_groups(store)}

    @app.patch("/admin/api/groups/{gid}")
    async def update_group(gid: str, req: Request):
        actor = admin(req)
        body = await payload(req)
        try:
            groups.update_group(store, gid, body)
        except KeyError:
            raise HTTPException(404, "分组不存在")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        security.audit(actor, "update_group", gid,
                       f"字段={'、'.join(sorted(body))}；来源 {client_ip(req)}")
        return {"ok": True, "groups": groups.list_groups(store)}

    @app.delete("/admin/api/groups/{gid}")
    async def delete_group(gid: str, req: Request):
        """删除分组并解除密钥绑定。

        返回 `unbound_keys`：删完之后必须告诉用户「有 N 枚密钥从'限定在某组'
        变成了'不限账号'」——这是个**权限放大**的副作用，不能静默发生。
        """
        actor = admin(req)
        try:
            result = groups.delete_group(store, gid)
        except KeyError:
            raise HTTPException(404, "分组不存在")
        security.audit(actor, "delete_group", gid,
                       f"解绑 {result['unbound_keys']} 枚密钥；来源 {client_ip(req)}")
        return {"ok": True, **result, "groups": groups.list_groups(store)}

    # ── 账号出口线路 ──────────────────────────────────────────

    @app.get("/admin/api/egress")
    async def list_egress(req: Request):
        """线路清单 + 每个账号当前绑的线路。

        `url` 已经过 `egress.mask()`：带 `user:pass@` 的商用代理不能把口令
        原样送进浏览器（同一条线路往往多个下游共用）。
        """
        viewer(req)
        rows = pool.rows(store.account_rows())
        with store.lock:
            assign_map = dict((store.data.get("egress") or {}).get("assign") or {})
        return {
            "lines": egress.list_lines(store),
            "accounts": [{"id": row["id"], "name": row.get("name") or row["id"],
                          "pool_state": row.get("pool_state"),
                          "line_id": assign_map.get(row["id"]) or ""} for row in rows],
            # 两种「绑定不生效」分开给，因为处理方式不同：悬空引用必须重新指定线路，
            # 线路停用只要启用回来。混成一个列表，界面就只能写一句含糊的提示。
            "orphans": egress.orphans(store),
            "stale": egress.stale(store),
        }

    @app.post("/admin/api/egress/lines")
    async def create_egress_line(req: Request):
        actor = admin(req)
        body = await payload(req)
        try:
            created = egress.create_line(store, body.get("name"), body.get("url"),
                                         enabled=body.get("enabled", True))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # 只记线路名与掩码后的地址：明文口令不进审计日志
        masked = egress.mask(egress.norm_url(body.get("url")))
        security.audit(actor, "create_egress_line", created["name"],
                       f"{masked}；来源 {client_ip(req)}")
        return {"ok": True, **created, "lines": egress.list_lines(store)}

    @app.patch("/admin/api/egress/lines/{lid}")
    async def update_egress_line(lid: str, req: Request):
        actor = admin(req)
        body = await payload(req)
        try:
            egress.update_line(store, lid, body)
        except KeyError:
            raise HTTPException(404, "线路不存在")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        security.audit(actor, "update_egress_line", lid,
                       f"字段={'、'.join(sorted(body))}；来源 {client_ip(req)}")
        return {"ok": True, "lines": egress.list_lines(store)}

    @app.delete("/admin/api/egress/lines/{lid}")
    async def delete_egress_line(lid: str, req: Request):
        actor = admin(req)
        try:
            freed = egress.delete_line(store, lid)
        except KeyError:
            raise HTTPException(404, "线路不存在")
        security.audit(actor, "delete_egress_line", lid, f"解绑 {freed} 个账号；来源 {client_ip(req)}")
        return {"ok": True, "freed": freed, "lines": egress.list_lines(store)}

    @app.post("/admin/api/egress/assign")
    async def assign_egress(req: Request):
        """给账号绑定 / 解绑出口线路：`{account_id, line_id}`，line_id 为空表示回直连。"""
        actor = admin(req)
        body = await payload(req)
        aid = str(body.get("account_id") or "").strip()
        lid = str(body.get("line_id") or "").strip()
        if not aid:
            raise HTTPException(400, "缺少 account_id")
        try:
            egress.assign(store, aid, lid or None)
        except KeyError as exc:
            raise HTTPException(404, "账号或线路不存在")
        security.audit(actor, "assign_egress", aid,
                       f"线路={lid or '直连'}；来源 {client_ip(req)}")
        return {"ok": True, "lines": egress.list_lines(store)}

    @app.post("/admin/api/pool/checkin-logs/clear")
    async def clear_checkin_logs(req: Request, kind: str = ""):
        actor = session_admin(req)
        # TASK_KINDS 是 admin.pool 的模块级常量，不是 AccountPool 实例的属性。
        # 这里必须用 import 进来的那个名字：`pool` 在本作用域里是实例（见 create_app
        # 开头的 `pool = AccountPool(store)`），写成 pool.TASK_KINDS 会 AttributeError → 500。
        if kind and kind not in TASK_KINDS:
            raise HTTPException(400, "任务类别无效")
        removed = pool.clear_task_logs(kind or None)
        security.audit(actor, "clear_checkin_logs", "",
                       f"清空 {removed} 条（类别={kind or '全部'}）；来源 {client_ip(req)}")
        return {"ok": True, "removed": removed}

    @app.post("/admin/api/accounts")
    async def add_account(req: Request):
        actor = admin(req)
        body = await payload(req)
        doc = body.get("credential")
        store.validate_credential(doc)
        name = clean_name(body.get("name"), None)
        # 统一走 save_browser_account：按 uid 去重合并（重登会刷新凭据），
        # 并让 SwitchStore 把新账号并入 workbuddy-switch 账号库。
        result = store.save_browser_account(doc, name)
        aid = result.get("account_id") or result.get("id")
        security.audit(actor, "add_account", str(name or aid), f"来源 {client_ip(req)}")
        return {"id": aid}

    @app.patch("/admin/api/accounts/{aid}")
    async def edit_account(aid: str, req: Request):
        actor = admin(req)
        body = await payload(req)
        live_accounts(store)
        hot = hasattr(store, "apply_override")
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            new = dict(item)
            if "name" in body:
                new["name"] = clean_name(body["name"], item["name"])
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise HTTPException(400, "启用状态不正确")
                new["enabled"] = body["enabled"]
            if body.get("active") is True:
                if not new["enabled"]:
                    raise HTTPException(400, "请先启用账号")
                if hot:
                    # 账号库才是凭据的权威：这里只确认库里确实有这个账号的可用记录。
                    row = next((r for r in store.account_rows() if r["id"] == aid), None)
                    if row is None or row.get("status") != "ready":
                        raise HTTPException(400, "该账号在 switch 账号库里找不到凭据，请在 workbuddy-switch 中重新登录")
                else:
                    try:
                        store.validate_credential(json.loads(store.file_for(new).read_text(encoding="utf-8")))
                    except (ValueError, OSError):
                        raise HTTPException(400, "凭据文件无法读取，请重新导入")
                store.data["active"] = aid
                # 换「当前账号」时作废 spread 驻留租约，否则切了号还会被旧租约钉住。
                store.data["pool"]["lease"] = None
            store.data["accounts"][aid] = new
            if not new["enabled"] and store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
        if hot:
            # 暂停 / 改名属于本服务的视图，落到覆盖层，否则下次账号库刷新就被抹掉。
            patch = {}
            if "enabled" in body:
                patch["enabled"] = body["enabled"]
            if "name" in body:
                patch["name"] = new["name"]
            if patch:
                store.apply_override(aid, **patch)
        security.audit(actor, "update_account", new["name"],
                       f"字段={'、'.join(sorted(k for k in body if k in ('name', 'enabled', 'active')))}；来源 {client_ip(req)}")
        return {"ok": True}

    @app.delete("/admin/api/accounts/{aid}")
    async def delete_account(aid: str, req: Request):
        actor = session_admin(req)
        live_accounts(store)
        if hasattr(store, "hide_account"):
            # 账号库是权威来源，服务侧「删除」= 不再调度这个账号（本地覆盖），
            # 真删账号请到 workbuddy-switch 的账号管理页。
            with store.lock:
                if aid not in store.data["accounts"]:
                    raise HTTPException(404, "账号不存在")
            store.hide_account(aid)
            security.audit(actor, "hide_account", aid, f"仅从本服务移除，账号库保留；来源 {client_ip(req)}")
            return {"ok": True, "scope": "service"}
        with store.lock:
            item = store.data["accounts"].get(aid)
            if not item:
                raise HTTPException(404, "账号不存在")
            trash = store.root / "trash" / (aid + "-" + secrets.token_hex(4))
            trash.mkdir(parents=True, mode=0o700)
            source = store.file_for(item)
            if source.exists():
                write_json(trash / "credential.json", json.loads(source.read_text(encoding="utf-8")))
            write_json(trash / "metadata.json", item)
            del store.data["accounts"][aid]
            if store.data["active"] == aid:
                store.data["active"] = None
            store.save()
            store.sync_active()
            # 这一支是真删（账号库也随之作废），所以要顺手清掉分组与出口绑定，
            # 否则 state.json 里会留下永远匹配不到人的悬空 id。
            #
            # 上面 hide_account 那一支故意不清：隐藏是可逆的，清掉等于用户
            # 暂停一个号再改下分组，那个号的成员资格与线路就永久丢了。
            # 隐藏账号本来就进不了候选集，留着这两条配置没有任何副作用。
            groups.forget_account(store, aid)
            egress.unbind_account(store, aid)
            # Keep the file so an in-flight token refresh may still complete safely.
            # Account storage is authoritative; deleted records cannot be selected.
        security.audit(actor, "delete_account", aid, f"来源 {client_ip(req)}")
        return {"ok": True, "scope": "file"}

    @app.get("/admin/api/pool/summary")
    async def pool_summary(req: Request):
        """账号池概览：可直接喂给 SPA 的账号页。"""
        viewer(req)
        live_accounts(store)
        return {"accounts": pool.rows(store.account_rows()), "pool": _pool_view(store)}

    # ── API 密钥（多密钥分发 + IP/模型白名单 + 额度）──────────────
    @app.post("/admin/api/keys")
    async def add_key(req: Request):
        actor = admin(req)
        body = await payload(req)
        name = clean_name(body.get("name"), "新客户端")
        if not str(body.get("name") or "").strip():
            raise HTTPException(400, "密钥名称不能只有空格")
        ip_allowlist = body.get("ip_allowlist") or []
        for cidr in keysvc.norm_cidrs(ip_allowlist):
            if not valid_cidr(cidr):
                raise HTTPException(400, f"IP 白名单里有无法识别的条目：{cidr}。请写成单个 IP 或 CIDR（10.0.0.0/8）。")
        kid, key = keysvc.create_key(
            store, name,
            expires_at=body.get("expires_at"),
            max_ips=int(body.get("max_ips") or 0),
            ip_allowlist=ip_allowlist,
            models=body.get("models") or [],
            realm=body.get("realm") or "",
            quota=int(body.get("quota") or 0),
            quota_credit=body.get("quota_credit") or 0,
            # 分组绑定。`update_key` 是整包透传 `body` 所以本来就带得上，
            # 但这里是逐字段拆开传的——漏掉这一行会让「创建时勾了分组、保存后没生效」，
            # 而且只在重新打开列表时才发现。
            groups=body.get("groups") or [],
            # 归属用户：门户按它查余额与流水。同样是逐字段拆开传的，
            # 漏掉会让「创建时选了归属、保存后是空」—— 表现是门户里余额永远 0。
            # `owner` 一律存用户 uid（不是 email）：`keysvc._norm_owner` 只 strip 不 lower，
            # 存 email 会让 Alice@x.com / alice@x.com 变成两份余额。
            # 且必须校验该 uid 存在，否则钱会记进没人认领的桶 —— 见 members.assert_owner。
            owner=members_svc.assert_owner(store, body.get("owner")),
            created_by=actor["username"],
        )
        security.audit(actor, "create_key", name, f"id={kid}；来源 {client_ip(req)}")
        return {"id": kid, "key": key}

    @app.patch("/admin/api/keys/{kid}")
    async def edit_key(kid: str, req: Request):
        actor = admin(req)
        body = await payload(req)
        if "ip_allowlist" in body:
            for cidr in keysvc.norm_cidrs(body.get("ip_allowlist")):
                if not valid_cidr(cidr):
                    raise HTTPException(400, f"IP 白名单里有无法识别的条目：{cidr}。请写成单个 IP 或 CIDR（10.0.0.0/8）。")
        if "owner" in body:
            # 与创建路径同样的校验：owner 必须是真实存在的用户 uid。
            # 空字符串是允许的（= 解绑，无主 key 是既有语义）。
            body = dict(body, owner=members_svc.assert_owner(store, body.get("owner")))
        updated = keysvc.update_key(store, kid, body)
        security.audit(actor, "update_key", updated["name"], f"字段={'、'.join(sorted(body))}；来源 {client_ip(req)}")
        return updated

    @app.post("/admin/api/keys/{kid}/reset-usage")
    async def reset_key_usage(kid: str, req: Request):
        actor = admin(req)
        result = keysvc.reset_usage(store, kid)
        security.audit(actor, "reset_key_usage", kid, f"来源 {client_ip(req)}")
        return result

    @app.delete("/admin/api/keys/{kid}")
    async def revoke_key(kid: str, req: Request):
        actor = session_admin(req)
        result = keysvc.delete_key(store, kid)
        security.audit(actor, "delete_key", kid, f"来源 {client_ip(req)}")
        return result

    @app.post("/admin/api/keys/check-models")
    async def check_key_models(req: Request):
        """校验模型白名单里哪些名字匹配不到当前可用模型（拼写提醒）。"""
        viewer(req)
        body = await payload(req)
        names = [str(x).strip() for x in (body.get("models") or []) if str(x).strip()]
        if not names:
            return {"checked": True, "unknown": []}
        known = set(converter.get_available_models())
        unknown = keysvc.unknown_whitelist_entries(names, known)
        return {"checked": True, "unknown": unknown}

    @app.post("/admin/api/test")
    async def test_model(req: Request):
        admin(req)
        body = await payload(req)
        # 与数据面同一份默认模型（按本机可用清单解析），别在这里再写一个字面量。
        model = body.get("model", converter.resolve_default_model())
        prompt = body.get("prompt", "请只回复：连接成功")
        if not isinstance(model, str) or model not in converter.get_available_models():
            raise HTTPException(400, "请选择列表中的模型")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            raise HTTPException(400, "测试消息需为 1–2000 个字符")
        if store.test_lock.locked():
            raise HTTPException(409, "已有测试正在进行")
        if converter.CONFIG.get("cred") is None:
            raise HTTPException(400, "请先导入并启用一个账号")
        async with store.test_lock:
            started = time.monotonic()
            # Use a short-lived API key with the same authentication path as clients.
            with store.lock:
                temp_key = secrets.token_urlsafe(48)
                store.test_keys.add(digest(temp_key))
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=MetricsMiddleware(converter.app, metrics, source="test")), base_url="http://internal") as client:
                    result = await asyncio.wait_for(client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + temp_key}, json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024, "stream": False}), timeout=90)
                data = result.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "") if result.status_code == 200 else ""
                output = {"ok": result.status_code == 200 and bool(answer), "status": result.status_code, "answer": answer, "seconds": round(time.monotonic() - started, 2), "usage": data.get("usage") if result.status_code == 200 else None}
                if not output["ok"]:
                    output["error"] = "上游未返回有效回复，请检查登录凭据、账号额度或模型权限。"
                    if result.status_code == 200 and data.get("choices", [{}])[0].get("finish_reason") == "length":
                        output["error"] = "已连接上游，但生成预算耗尽，未获得正文。请在客户端提高 max_tokens 后重试。"
            except (Exception, asyncio.TimeoutError):
                output = {"ok": False, "error": "调用失败或超时，请检查凭据是否有效，稍后重试。", "seconds": round(time.monotonic() - started, 2)}
            finally:
                with store.lock:
                    store.test_keys.discard(digest(temp_key))
            store.events.appendleft({"time": int(time.time()), "model": model, "ok": output["ok"], "seconds": output["seconds"]})
            return output

    # 用户中心接口（页面路径 /me，接口前缀 /me/api/*）。
    #
    # 数据层在 portal.py，这里只做 HTTP 层。
    #
    # 四条必须保留的保护：
    #   ① require_user_system()  总闸：只在会话分支挡，Bearer 分支不挡；
    #   ② 凭据冲突 403           自己的会话 + 别人的 key = 越权；
    #   ③ 兑换成功不清零          否则 19 假 + 1 真 交错可绕过限流；
    #   ④ 清理只删过期窗口         否则刷 500 个密钥即可重置自己的限流计数。

    def me_key(req: Request) -> dict:
        """解析 Bearer / X-API-Key。无效、停用、过期一律 401。

        配额用尽不拦：额度用完的用户恰恰最需要进来查用量、去充值。
        """
        header = req.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            token = (req.headers.get("x-api-key") or "").strip()
        if not token:
            raise HTTPException(401, "缺少 API Key。请用 Authorization: Bearer <你的密钥> 访问。")
        found = keysvc.resolve_key(store, token)
        if not found:
            raise HTTPException(401, "API Key 无效")
        if not found.get("enabled", True):
            raise HTTPException(401, "这枚 API Key 已被停用")
        exp = found.get("expires_at")
        if exp and int(exp) < time.time():
            raise HTTPException(401, "这枚 API Key 已过期")
        return found

    # ── 用户中心身份：两种等价身份 ─────────────────────────────────
    #
    #   · 老客户端：Authorization: Bearer sk-wb-...（没有 cookie）；
    #   · 新用户：浏览器里的 workbuddy_user 会话 cookie（没有 key）。
    #
    # ⚠️ 同时送 key 和 cookie 时的优先级是安全关键，不是风格问题：
    #     同一个人 -> 放行（两种凭据指向同一个 uid，没有歧义）；
    #     不同的人 -> 403 凭据冲突。
    # 为什么不能「以 key 为准」或「以 cookie 为准」：那等于给攻击者一个
    # 「用自己那条低权限凭据，套上别人那条高权限凭据」的越权入口 ——
    # 拿自己的会话配上偷来的 key，就能以 key 主人的身份读余额和流水。
    def me_identity(req: Request) -> dict:
        """解析用户中心身份。返回 {"kind", "key", "uid", "member"}。

        kind: "key"（只有 key）/ "session"（只有会话）/ "both"（两者且同主）。
        """
        header = req.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            token = (req.headers.get("x-api-key") or "").strip()

        uid = accounts.session_member_id(store, req)
        member = None
        if uid:
            row = accounts.find(store, uid)
            if row and str(row.get("status") or "active") != "disabled":
                member = accounts.public_user(store, row)
            else:
                # 会话指向已删/已禁用的成员：当作没登录，并且顺手清掉，
                # 否则它会一直挂到 TTL 到期。
                accounts.revoke_session(store, req)
                uid = ""

        if token:
            key = me_key(req)
            key_uid = portal.owner_of(key)
            if uid and key_uid and key_uid != uid:
                # 跨用户越权。
                security.audit({"username": "member:" + uid, "role": "member"},
                               "me_identity_conflict", uid,
                               "会话 uid=" + uid + " 密钥归属=" + key_uid
                               + "；来源 " + client_ip(req))
                raise HTTPException(403, "凭据冲突：当前登录账号与这枚 API Key"
                                         "不属于同一个用户，请退出后重新登录。")
            # 密钥无归属时不因 cookie 而自动认领 ——
            # 那会让「谁的 session 先撞上这枚 key」决定钱记到谁头上。
            return {"kind": "both" if uid else "key", "key": key,
                    "uid": key_uid or uid, "member": member, "key_uid": key_uid}

        if uid:
            # 总闸：关掉用户体系后，会话身份这条路不再放行。
            #
            # ⚠️ 只挡这里 —— 上面的 if token: 分支（Bearer 密钥）不挡。
            # 密钥直连是核心服务，不能因为管理员关了「用户体系」就中断；
            # 否则这个开关一拨，所有人的服务同时中断，它就从「功能开关」
            # 变成了「停机按钮」。
            require_user_system()
            return {"kind": "session", "key": None, "uid": uid, "member": member,
                    "key_uid": ""}
        raise HTTPException(401, "缺少 API Key。请用 Authorization: Bearer <你的密钥> 访问，或先登录账号。")

    def me_uid(ident: dict) -> str:
        """身份对应的**计费主体** uid。空串 = 无归属（余额按 0 算）。"""
        return str(ident.get("uid") or "")

    def me_keys_of(ident: dict) -> list:
        """这个身份名下的全部密钥（会话模式下的「我的密钥」列表）。"""
        uid = me_uid(ident)
        if not uid:
            return []
        with store.lock:
            rows = list((store.data.get("keys") or {}).values())
        mine = [r for r in rows if portal.owner_of(r) == uid]
        mine.sort(key=lambda r: float(r.get("created") or 0), reverse=True)
        return [portal.view_key(r) for r in mine]

    def me_rd_from_milli(milli: int) -> float:
        from . import redemption as _rd
        return _rd.from_milli(int(milli or 0))

    def me_overview_payload(ident: dict, *, days: int = 30) -> dict:
        """用户中心首页（会话模式）。形状与 key 模式**刻意保持同构**。

        balance / keys / daily / hourly / totals 都在，只是 key 为 null、
        mode 为 "session"。前端因此可以只写一套渲染，不必按模式分叉 ——
        分叉出来的两套渲染早晚会漂移成两个不同的页面。
        """
        uid = me_uid(ident)
        keys = me_keys_of(ident)
        with store.lock:
            snapshot = usage.snapshot(store, days=days, hours=24)

        # 会话模式下「我的用量」= 名下所有密钥的合计，而不是某一枚。
        kids = {k["id"] for k in keys}

        def merge(rows: list) -> list:
            out = []
            for row in rows:
                by_key = row.get("by_key") or {}
                hit = [by_key[k] for k in kids if k in by_key]
                if not hit:
                    continue
                agg = {k: v for k, v in row.items() if k != "by_key"}
                for field in ("calls", "tokens"):
                    agg[field] = sum(int(h.get(field, 0) or 0) for h in hit)
                agg["credit"] = round(
                    sum(float(h.get("credit", 0) or 0) for h in hit), 6)
                out.append(agg)
            return out

        daily = merge(snapshot.get("daily") or [])
        hourly = merge(snapshot.get("hourly") or [])
        balance_milli = accounts.balance_milli(store, uid) if uid else 0
        return {
            "mode": "session",
            "user": ident.get("member"),
            "key": None,
            "owner": uid,
            "keys": keys,
            "balance": me_rd_from_milli(balance_milli),
            "balance_milli": balance_milli,
            "balance_credit": me_rd_from_milli(balance_milli),
            "has_owner": bool(uid),
            "daily": daily,
            "hourly": hourly,
            "totals": {
                "calls": sum(int(r.get("calls", 0)) for r in daily),
                "tokens": sum(int(r.get("tokens", 0)) for r in daily),
                "credit": round(sum(float(r.get("credit", 0) or 0)
                                    for r in daily), 6),
            },
        }

    @app.get("/me/api/overview")
    async def me_overview(req: Request):
        ident = me_identity(req)
        try:
            days = max(1, min(365, int(req.query_params.get("days") or 30)))
        except (TypeError, ValueError):
            days = 30
        if ident["kind"] == "key":
            return portal.overview(store, ident["key"], days=days)
        return me_overview_payload(ident, days=days)

    @app.get("/me/api/flows")
    async def me_flows(req: Request):
        ident = me_identity(req)
        try:
            limit = max(1, min(200, int(req.query_params.get("limit") or 100)))
        except (TypeError, ValueError):
            limit = 100
        uid = me_uid(ident)
        if not uid:
            return {"flows": []}
        return {"flows": portal.flows_by_uid(store, uid, limit=limit)}

    @app.post("/me/api/redeem")
    async def me_redeem(req: Request):
        ident = me_identity(req)
        # 兑换要记名，所以必须落到某一个身份上：
        #   · 有 key -> 沿用密钥的 id 做限流桶（老行为不变）；
        #   · 只有会话 -> 用 uid 做桶，否则「换个 key 就能重置计数」。
        uid = me_uid(ident)
        if not uid:
            raise HTTPException(
                400,
                "当前身份还没有绑定归属用户，无法兑换。"
                "请先登录账号，或在管理端为密钥设置归属。")
        bucket = str((ident.get("key") or {}).get("id") or ("uid:" + uid))
        body = await payload(req)
        code = str(body.get("code") or "").strip()
        # 限流：兑换码熵足够高，但无限次尝试本身该挡。
        #
        # 计数只在 5 分钟窗口到期时归零，兑换成功也不提前清零。
        # 若成功后清零，攻击者按「19 次乱猜 + 1 次已知有效码」交错发请求，
        # 每命中一次就把计数清零，于是永远不会触发 429。
        # 限流限制的是尝试次数，与猜得对不对无关；正常用户兑错几次后兑对，
        # 窗口一过自然恢复。
        # 回归测试：tests/test_me_ratelimit.py::test_success_does_not_reset_the_counter
        now = int(time.time())
        with store.lock:
            state = store.data.setdefault("me_redeem_attempts", {})
            row = state.get(bucket) or {"count": 0, "window": now}
            if now - int(row.get("window", 0)) > 300:
                row = {"count": 0, "window": now}
            if int(row.get("count", 0)) >= 20:
                raise HTTPException(429, "兑换尝试过于频繁，请 5 分钟后再试")
            row["count"] = int(row.get("count", 0)) + 1
            state[bucket] = row
            # 清理：只删已经过期的窗口，绝不删还在计数的条目。
            #
            # 复用模块级的 _evict_stale_attempts()（它带完整的缺陷说明），
            # 而不是在这里再内联一遍 —— 两处各写一份早晚会漂移。
            if len(state) > 500:
                _evict_stale_attempts(state, now)
        result = portal.redeem(store, ident["key"] or {"owner": uid}, code)
        # ⚠️ 这里没有「成功就清计数」的分支 —— 那正是上面说的缺陷。
        security.audit({"username": ident["member"]["email"] if ident.get("member")
                               else "key:" + str((ident.get("key") or {}).get("name", "")),
                        "role": "member"},
                       "me_redeem", bucket,
                       "面额 " + str(result.get("credit")) + " 积分；来源 " + client_ip(req))
        return result

    @app.get("/me/api/keys")
    async def me_keys(req: Request):
        return {"keys": me_keys_of(me_identity(req))}

    # ── 管理端：充值 / 兑换码 ────────────────────────────────────────
    @app.get("/admin/api/billing/stats")
    async def billing_stats(req: Request):
        viewer(req)
        with store.lock:
            balances = {u: redemption.from_milli(int(v.get("milli", 0)))
                        for u, v in (store.data.get("balances") or {}).items()}
        return {"stats": redemption.stats(store), "balances": balances}

    @app.post("/admin/api/billing/recharge")
    async def billing_recharge(req: Request):
        actor = admin(req)
        body = await payload(req)
        username = str(body.get("username") or "").strip()
        if not username:
            raise HTTPException(400, "请选择要充值的用户")
        if not any(u.get("username") == username for u in store.data.get("users", [])):
            raise HTTPException(404, f"用户 {username} 不存在")
        credit = body.get("credit")
        try:
            credit = float(credit)
        except (TypeError, ValueError):
            raise HTTPException(400, "充值金额必须是数字")
        # 上限防手滑：多打一个 0 就是 10 倍的钱。
        if credit <= 0 or credit > 1_000_000:
            raise HTTPException(400, "充值金额需在 0–1,000,000 积分之间")
        result = redemption.grant(store, username, credit, kind="recharge",
                                  note=str(body.get("note") or "管理员充值")[:120],
                                  actor=actor.get("username", ""))
        security.audit(actor, "recharge", username,
                       f"充值 {credit:g} 积分；来源 {client_ip(req)}")
        return result

    @app.post("/admin/api/billing/spend")
    async def billing_spend(req: Request):
        actor = admin(req)
        body = await payload(req)
        username = str(body.get("username") or "").strip()
        if not username:
            raise HTTPException(400, "请选择用户")
        credit = body.get("credit")
        try:
            credit = float(credit)
        except (TypeError, ValueError):
            raise HTTPException(400, "扣减金额必须是数字")
        if credit <= 0:
            raise HTTPException(400, "扣减金额必须大于 0")
        result = redemption.spend(store, username, credit,
                                  note=str(body.get("note") or "管理员扣减")[:120])
        security.audit(actor, "spend", username, f"扣减 {credit:g} 积分")
        return result

    @app.get("/admin/api/billing/flows")
    async def billing_flows(req: Request):
        viewer(req)
        return {"flows": redemption.list_flows(store,
                                               username=(req.query_params.get("username") or "").strip(),
                                               limit=int(req.query_params.get("limit") or 200))}

    @app.get("/admin/api/billing/codes")
    async def billing_list_codes(req: Request):
        viewer(req)
        return {"codes": redemption.list_codes(store,
                                               batch=(req.query_params.get("batch") or "").strip(),
                                               status=(req.query_params.get("status") or "all").strip(),
                                               limit=int(req.query_params.get("limit") or 200))}

    @app.post("/admin/api/billing/codes")
    async def billing_create_codes(req: Request):
        actor = admin(req)
        body = await payload(req)
        result = redemption.create_batch(
            store, count=body.get("count"), credit=body.get("credit"),
            note=str(body.get("note") or "")[:120], actor=actor.get("username", ""),
            expires_at=body.get("expires_at"))
        security.audit(actor, "create_codes", result["batch"],
                       f"生成 {result['count']} 张 × {result['credit']:g} 积分")
        return result

    @app.post("/admin/api/billing/codes/{code_id}/disable")
    async def billing_disable_code(code_id: str, req: Request):
        actor = admin(req)
        with store.lock:
            hit = next((h for h in store.data.get("redemption_codes", {}) if h.startswith(code_id)), None)
            if not hit:
                raise HTTPException(404, "兑换码不存在")
            store.data["redemption_codes"][hit]["enabled"] = False
        store.save()
        security.audit(actor, "disable_code", code_id, "停用兑换码")
        return {"ok": True}

    # ── 用户管理（终端用户 / members）────────────────────────────────
    #
    # ⚠️ 这不是上面那个 `/admin/api/users`。 那个管的是「控制台账号」
    # （username + role ∈ admin/viewer/session_admin），用来登录 8787 后台，
    # 桌面端已有 UsersPanel 在用。这里管的是终端用户：邮箱注册、有积分余额、
    # 是 API 调用的计费主体。两者是完全不同的东西。
    #
    # 路径特意避开 `/admin/api/users`：FastAPI 同路径重复注册是「先注册者胜」，
    # 照抄契约草稿里那一行会让本段端点永远路由不到；而改现有端点会直接
    # 打爆 UsersPanel 与现有的 4 个控制台账号端点。
    #
    # ── 守卫分档 ──
    #   GET  列表 / 详情      → viewer(req)              只读角色可看
    #   PATCH 改角色 / 启停   → admin(req)               不是不可逆
    #   POST  调余额          → require_session_admin     直接改钱
    #   DELETE 删用户         → require_session_admin     不可逆 + 动钱
    # 与现有 `/admin/api/users`（增/改/删走 session_admin）同档。
    #
    # 两端都能通过：`admin(req)` → `security.current_user(req)` 优先读
    # `X-Admin-Token` 头、退回管理 cookie。桌面端 SPA 走 `credentials:"omit"`
    # + 头令牌（`src/lib/reverse-proxy.ts:82-87`），所以只认 cookie 会让它全 401。
    #
    # ── 顺序：先鉴权，后看路径参数 ──
    # 所有 handler 第一行就是拿身份。越权调用（匿名 / 只读角色 / wbt_ 令牌会话）
    # 一律拿 401 或 403，不会先返回「用户不存在」 —— 否则 404 与 403 的差别
    # 本身就是一个「这个 uid 存不存在」的探测器。
    #
    # ══ 用户账号 ══════════════════════════════════════════════
    #
    # 这一整段是终端用户的账号体系：邮箱注册 / 登录 / 会话 / 验证码。
    # 与 `/admin/api/users`（控制台账号）完全是两套东西，永不共用存储键，
    # 也永不共用端点。
    #
    # 存储键全部是新的（`members` / `member_index_email` / `member_sessions` /
    # `email_codes`），老 `state.json` 不需要迁移。`users` 键绝对不碰。

    accounts.ensure(store)

    def _set_user_cookie(response, sid: str, req: Request):
        """下发用户会话 cookie。

        `secure` **复用现有 `_cookie_secure(req)`** —— 自己另写一套判断迟早会
        和它对不上。硬编码 Secure 会让明文 HTTP 登录直接失效：
        登录返回 200、cookie 也下发了，但后续每个请求都 401，
        界面表现为「登录成功后立刻弹回登录页」，且没有任何报错可查。
        """
        response.set_cookie(accounts.COOKIE, sid,
                            **accounts.cookie_params(_cookie_secure(req)))
        return response

    def _clear_user_cookie(response, req: Request):
        """清掉用户 cookie。**path 必须与 set_cookie 一致** ——
        否则浏览器里会留下一条同名的旧 cookie，用户「点了退出却还登着」。
        """
        response.delete_cookie(accounts.COOKIE, path=accounts.COOKIE_PATH)
        return response

    def _user_json(result: dict, req: Request) -> JSONResponse:
        """注册 / 登录的 200 响应：下发（或续期）会话 cookie。

        `sid` 必须在这里被摘掉。 `accounts.register/login` 返回 {user, sid}，
        其中 sid 是明文会话 id；它只应该进 Set-Cookie 头，绝不该进响应体 ——
        httponly 的意义就是「脚本读不到它」，原样返回等于自己把它交出去。
        """
        data = dict(result)
        sid = str(data.pop("sid", ""))
        response = JSONResponse({"ok": True, "user": data["user"]})
        if sid:
            _set_user_cookie(response, sid, req)
        return response

    @app.get("/login/api/options")
    async def login_options():
        """**匿名**：登录页靠它决定「显示不显示注册入口、要不要验证码」。

        只回允许的那几个字段（见 `settings_store.public_options`），
        而且**显式挑字段**而不是「全量减字段」—— 后者在将来加字段时会默认泄露。
        特别是**不回 dev_mode**：匿名端点回任何配置细节都是在帮探测者。
        """
        return settings_store.public_options(store)

    # ── 用户系统总闸 ───────────────────────────────────────
    #
    # ⚠️ 边界：这个闸关的是「用户体系」，不是「整个反代服务」。
    #
    #   被关掉：匿名注册、匿名登录、用户中心（/login/api/me）、
    #           门户的会话身份（/portal/api/* 用 cookie 登录的那条路）
    #   不受影响：`/v1` 的 API Key 直连 —— 那是本项目的核心服务，
    #            与「用户体系」是两件事。管理员关掉用户体系之后，
    #            已经发出去的密钥必须照常能用；否则这个开关一拨，
    #            所有人的服务同时中断，它就从「功能开关」变成了「停机按钮」。
    #
    # 所以门户这里只挡会话身份：`/portal/api/*` 仍然接受 Bearer 密钥，
    # 那是密钥直连的延伸（见 portal_identity 的 kind）。
    def require_user_system() -> None:
        """总闸关着就 403。所有用户侧入口**第一行**调用它。

        用 403 而不是 404：这不是「找不到」，是「管理员关了」，
        用户需要能看懂并去联系管理员（404 会让人以为地址敲错了）。
        """
        reason = settings_store.user_system_off_reason(settings_store.load(store))
        if reason:
            raise HTTPException(403, reason)


    @app.post("/login/api/register")
    async def login_register(req: Request):
        require_user_system()
        body = await payload(req)
        result = accounts.register(
            store,
            email=str(body.get("email") or ""),
            password=body.get("password") or "",
            code=body.get("code"),
            ip=accounts.request_ip(req),
        )
        uid = result["user"]["id"]
        security.audit({"username": "member:" + uid, "role": "member"},
                       "member_register", uid,
                       "邮箱=" + str(result["user"]["email"]) + "；来源 " + client_ip(req))
        return _user_json(result, req)

    @app.post("/login/api/send-code")
    async def login_send_code(req: Request):
        require_user_system()
        body = await payload(req)
        email = str(body.get("email") or "")
        settings = settings_store.load(store)
        result = accounts.send_code(store, email, accounts.request_ip(req),
                                    purpose=str(body.get("purpose") or "register"),
                                    settings=settings)
        # 验证码只在开发出口真的生效时才出现在响应里。
        # `accounts.send_code` 用 `delivered` 告诉我们它有没有打 SMTP：
        #   · delivered=True  → 走的是真投递，`code` 恒为空串；
        #   · delivered=False → 走的 dev 出口（WB_DEV_MAIL=1 且没配 SMTP），
        #     此时才把码回给调用方，并显式标记 dev_mode。
        # 不标的话没人分得清「线上也这样」还是「只是本地」。
        #
        # ⚠️ dev 出口只对本机回码。
        #
        # 原先这里无条件 return result —— 只要进程带了 WB_DEV_MAIL=1，
        # 任何一个匿名请求都能 POST 一个别人的邮箱、拿到验证码、再用它注册。
        # 漏洞：匿名发码能拿回 code，再用别人的邮箱注册成功
        # （email_verified=true），等于冒名占用他人邮箱。
        #
        # dev 出口的全部理由是「本机自测方便」，那就把回码限制在本机来源。
        # 远程来源即使进程误开了 WB_DEV_MAIL，也只能拿到 `delivered=false`
        # 这个事实，拿不到码本身 —— 攻击面从「任何能访问到该端口的人」
        # 收敛到「已经能在本机执行请求的人」（后者本来就等于拿到了 state.json）。
        if result.get("delivered") is False and not _dev_caller(req):
            result = {**result, "code": "", "dev_mode": False}
        return result

    @app.post("/login/api/login")
    async def login_login(req: Request):
        require_user_system()
        body = await payload(req)
        result = accounts.login(store,
                                email=str(body.get("email") or ""),
                                password=body.get("password") or "",
                                ip=accounts.request_ip(req))
        return _user_json(result, req)

    @app.post("/login/api/logout")
    async def login_logout(req: Request):
        """登出。幂等 —— 没登录、会话已过期、cookie 坏了，一律 200。

        不能改成 401：前端会把失败弹 toast，用户看到的是「退不出去」。
        登出的语义是清干净当前登录态，没登录本来就是目标状态。
        """
        accounts.logout(store, req)
        return _clear_user_cookie(JSONResponse({"ok": True}), req)

    @app.get("/login/api/me")
    async def login_me(req: Request):
        require_user_system()
        return {"ok": True, "user": accounts.current_member(store, req)}

    @app.post("/login/api/password")
    async def login_password(req: Request):
        require_user_system()
        user = accounts.current_member(store, req)
        body = await payload(req)
        accounts.change_password(store, user["id"],
                                 str(body.get("old") or ""), str(body.get("new") or ""))
        # 改密码会吊销该用户全部会话（含本次）。所以必须重下发一条，
        # 否则「改完密码自己也被踢出去了」，用户会以为改坏了。
        sid = accounts.start_session(store, user["id"])
        security.audit({"username": "member:" + user["id"], "role": "member"},
                       "member_password", user["id"], "来源 " + client_ip(req))
        return _set_user_cookie(JSONResponse({"ok": True, "relogin": True}), sid, req)

    # ── `/login/api/*` 结束 ───────────────────────────────────────

    # ══ 服务设置 ══════════════════════════════════════════════
    #
    # 端点：GET/PUT /admin/api/settings、POST /admin/api/settings/smtp-test。

    @app.get("/admin/api/settings")
    async def get_settings(req: Request):
        viewer(req)   # 只读角色也能看设置（了解「注册开着吗」是合理需求）
        return settings_store.public_view(settings_store.load(store))

    @app.put("/admin/api/settings")
    async def put_settings(req: Request):
        # 改 SMTP 口令 / 开关注册 = 改服务的安全姿态，与 `/admin/api/security/config`
        # 同档，用 `session_admin`（拒 wbt_ 令牌身份，避免「令牌泄露 → 改 SMTP
        # 收走验证码」这条提权链）。
        actor = session_admin(req)
        body = await payload(req)
        saved = settings_store.apply_patch(store, body)
        # 审计绝不记录口令：这里只记「改了哪些键」。
        fields = "、".join(sorted(str(k) for k in body.keys()))
        security.audit(actor, "update_settings", "",
                       "字段=" + (fields or "(空)") + "；来源 " + client_ip(req))
        return saved

    @app.post("/admin/api/settings/smtp-test")
    async def smtp_test(req: Request):
        actor = session_admin(req)
        body = await payload(req)
        result = mailer.send_test(settings_store.load(store),
                                  to_addr=str(body.get("to") or ""),
                                  password=str(body.get("password") or ""))
        security.audit(actor, "smtp_test", "",
                       ("成功 → " if result.get("ok") else "失败 → ")
                       + str(result.get("to") or body.get("to") or "")
                       + "；来源 " + client_ip(req))
        return result

    # ── 服务设置结束 ──────────────────────────────────────────────

    # ═══════════════════════════════════════════════════════════

    # ⚠️ 本段必须注册在 `app.mount("/", converter.app)` 之前 ——
    # 那是兜底挂载，落在它后面的路由一律 404。

    def _member_actor(req: Request) -> dict:
        """把管理员身份包装成 members 层的 actor。"""
        user = security.current_user(req)
        return {"username": user.get("username", ""),
                "role": user.get("role", ""),
                "member_id": getattr(req.state, "member_id", None)}

    def _revoke_member(uid: str) -> int:
        """踢掉某成员的全部用户会话。

        `accounts` 落地后用它；没落地时返回 0（会话还不存在，没什么可踢）。
        绝不能静默失败 ——「改了角色/禁用了，但攻击者还挂着」是安全事件，
        不是可以吞掉的异常。所以这里只在「函数不存在」时跳过，
        真报了异常就让它冒出去（管理员会看到 500，而这比静默放行好得多）。
        """
        try:
            from . import accounts as accounts_mod
        except ImportError:
            return 0
        for name in ("revoke_member_sessions", "revoke_user_sessions"):
            fn = getattr(accounts_mod, name, None)
            if callable(fn):
                return int(fn(store, uid) or 0)
        return 0

    def _int_param(raw, default: int) -> int:
        """query 参数一律是字符串，脏值吃成默认值 —— 不能让它变成 500。"""
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default

    @app.post("/admin/api/members")
    async def member_create(req: Request):
        """管理员分发账号（对应「给您分发账号登录」那条出路）。

        与自助注册共用 `members.create()` —— 密码哈希、邮箱唯一性、
        索引维护只有一处实现，两边不可能漂移。

        守卫用 `admin(req)` 而不是 `viewer`：建账号能直接造出一个后台管理员，
        但它不是不可逆的（可以删/降级），所以不必上 `session_admin`。
        """
        actor = admin(req)
        body = await payload(req)
        row = members_svc.create(
            store,
            email=body.get("email", ""),
            password=body.get("password", ""),
            role=str(body.get("role") or "user"),
            # 管理员亲手分发 = 已确认身份。
            # 自助注册那条路才有「验证过没有」的问题。
            email_verified=bool(body.get("email_verified", True)),
        )
        security.audit(actor, "create_member", row["id"],
                       f"邮箱={row['email']} 角色={row['role']}；来源 {client_ip(req)}")
        return members_svc.view(store, row)

    @app.get("/admin/api/members")
    async def list_members(req: Request):
        viewer(req)   # 只读角色也能看列表
        return members_svc.list_members(
            store,
            q=req.query_params.get("q") or "",
            page=_int_param(req.query_params.get("page"), 1),
            size=_int_param(req.query_params.get("size"),
                            members_svc.DEFAULT_PAGE_SIZE),
        )

    @app.get("/admin/api/members/stats")
    async def member_stats(req: Request):
        viewer(req)
        return members_svc.stats(store)

    @app.get("/admin/api/members/{uid}")
    async def member_detail(uid: str, req: Request):
        viewer(req)
        return members_svc.detail(store, uid,
                                  flow_limit=_int_param(req.query_params.get("flows"), 50))

    @app.patch("/admin/api/members/{uid}")
    async def member_update(uid: str, req: Request):
        actor = admin(req)
        body = await payload(req)
        row = members_svc.update(store, uid, body, actor=_member_actor(req))
        # 改角色 / 禁用后必须立刻踢会话。角色变了还留着旧会话，
        # 等于「降级之后攻击者还挂着」—— 本项目在管理员侧栽过一次
        # （见 security.revoke_user_sessions 的长注释）。
        finished = members_svc.by_id(store, uid) or {}
        revoked = 0
        if finished.get("status") == "disabled" or "role" in body:
            revoked = _revoke_member(uid)
        security.audit(actor, "update_member", uid,
                       f"role={finished.get('role')} status={finished.get('status')}"
                       f" revoked={revoked}；来源 {client_ip(req)}")
        out = members_svc.view(store, finished)
        out["sessions_revoked"] = revoked
        # 禁用了自己（管理员本身也是终端用户时）→ 告诉他需要重登，
        # 与现有 /admin/api/users/{username} 的 relogin_required 口径一致。
        out["relogin_required"] = bool(
            revoked and _member_actor(req).get("member_id") == str(uid))
        return out

    @app.post("/admin/api/members/{uid}/balance")
    async def member_balance(uid: str, req: Request):
        # 直接改钱的端点。拒 `wbt_` 令牌身份 —— 与现有
        # `/admin/api/tokens`、`/admin/api/users` 的写接口同档。
        actor = session_admin(req)
        body = await payload(req)
        delta = body.get("delta_credit", body.get("delta"))
        result = members_svc.adjust_balance(
            store, uid, delta, str(body.get("reason") or ""),
            actor=actor.get("username", ""))
        # 走 redemption.grant/spend → add_flow，流水已经写了。
        # 金额按「厘」整数，禁止浮点累加（本项目被 banker's rounding 坑过）。
        security.audit(actor, "adjust_member_balance", uid,
                       f"增量 {result['delta_credit']:g} 积分（{result['balance_milli']} 厘）；"
                       f"来源 {client_ip(req)}")
        return result

    @app.delete("/admin/api/members/{uid}")
    async def member_delete(uid: str, req: Request):
        # 不可逆 + 动钱，同样拒令牌身份。
        actor = session_admin(req)
        force = str(req.query_params.get("force") or "").strip().lower() in ("1", "true", "yes")
        result = members_svc.delete(store, uid, force=force, actor=_member_actor(req))
        _revoke_member(uid)
        security.audit(actor, "delete_member", uid,
                       f"邮箱={result['email']} 解绑 {result['keys_unbound']} 枚密钥"
                       f" 释放 {result['released_credit']:g} 积分 force={force}；"
                       f"来源 {client_ip(req)}")
        return result

    # ── `/admin/api/members/*` 结束 ───────────────────────────────

    app.mount("/", converter.app)
    return app


def _forwarded_allow_ips() -> str:
    """uvicorn 采信 `X-Forwarded-*` 的来源白名单。**默认只信本机**。

    为什么要收：`"*"` 表示「信任任何人写的转发头」。uvicorn 的 `proxy_headers`
    会用 `X-Forwarded-For` 覆盖 `scope["client"]`，于是**任何能直连到这个端口的
    人都能自称来自任意 IP**。

    ⚠️ 注意影响面**比看起来小**，别把它当万灵药也别当摆设：
      · `iputil.client_ip()` **不读这个开关** —— 它只看 TCP 对端是否落在
        `WB_TRUSTED_PROXY_CIDRS`，所以 IP 白名单 / 限流**不受**它影响；
      · 真正被它影响的是 **`_cookie_secure()` 读的 `X-Forwarded-Proto`**：
        伪造 `https` 会让 cookie 带上 `Secure`，而明文 HTTP 下浏览器拒绝回传
        → 表现为「登录成功但每个请求 401」。这是**可用性**问题，方向是「更严」。
      · 所以收紧它主要是**纵深防御**，不是堵一个已证实的洞。

    逃生门：反代在另一台机器 / 容器上时，它的 IP 不在 127.0.0.1，
    此时必须显式设置 `WB_FORWARDED_ALLOW_IPS`（逗号分隔），否则
    `X-Forwarded-Proto` 读不到 → 配了 HTTPS 反代的用户会「登录成功但一直 401」，
    且**极难自查**（没有任何报错）。部署文档里必须写这一条。
    """
    raw = os.environ.get("WB_FORWARDED_ALLOW_IPS", "").strip()
    if raw:
        values = [x.strip() for x in raw.split(",") if x.strip()]
        # 值可疑时打 warning：`*` 等于没设，而用户很可能以为设了就安全了。
        if "*" in values:
            print("[reverse-proxy] 警告：WB_FORWARDED_ALLOW_IPS 含 '*'，"
                  "等于信任任意来源的 X-Forwarded-* 头。请改为具体的反代地址。",
                  file=sys.stderr)
        return ",".join(values)
    return "127.0.0.1"


if __name__ == "__main__":
    # 默认绑 127.0.0.1：`/admin/` 与用户门户/登录页同端口同进程，
    # 绑 0.0.0.0 等于把管理面一起摊到所有网卡上。对外暴露请走外部反代
    # （反代终止 TLS → 转发到 127.0.0.1:8787），
    # 或显式传 `--host 0.0.0.0` 并同步设置 WB_FORWARDED_ALLOW_IPS。
    _host = os.environ.get("WB_SERVE_HOST", "").strip() or "127.0.0.1"
    if _host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("WB_FORWARDED_ALLOW_IPS", "").strip():
        print(f"[reverse-proxy] 警告：绑定在 {_host} 上（非本机回环），"
              "但 WB_FORWARDED_ALLOW_IPS 未设置 —— 若前方有反向代理，"
              "请把它的地址填进去，否则 HTTPS 反代下的登录态会失效。",
              file=sys.stderr)
    uvicorn.run(create_app(), host=_host, port=8787, log_level="warning",
                proxy_headers=True, forwarded_allow_ips=_forwarded_allow_ips())
