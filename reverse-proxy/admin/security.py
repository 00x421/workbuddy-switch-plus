"""管理端鉴权（移植自 workbuddy-manager/server/security.py，改为 JSON 存储）。

与原实现的差异（适配本项目的 JSON state 架构，不引入 SQLite）：
  · 用户表 / 管理令牌 / 审计日志存进 store.data（state.json），而非 SQLite；
  · 会话沿用本项目既有的**不透明会话表**（store.sessions，进程内），
    但会话记录里带上 `username` / `role` / `iat`，从而支持多用户与角色；
  · 保留**旧的单密钥 `ADMIN_KEY` 登录**用于向后兼容（内置静态后台）。

安全要点（与原实现同口径）：
  · 口令用 PBKDF2-SHA256 加盐哈希（260k 轮），明文永不落盘；
  · 登录防爆破按 **IP + 用户名** 双维度计数，超限锁定 10 分钟；计数器
    超限只淘汰**已过窗口**的条目，绝不整体清空（否则可被灌爆以抹掉锁定）；
  · 作用域化管理令牌（`wbt_`）只存 SHA-256 哈希，明文仅创建时返回一次；
    失败不区分「不存在 / 已停用 / 已过期」，不给探测者信号；
  · 高危接口（用户 / 令牌 / 安全策略）只接受**会话**管理员，拒绝令牌调用，
    避免「令牌泄露 → 自助提权 / 持久化」。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import time

from fastapi import HTTPException, Request

from .iputil import client_ip

COOKIE = "workbuddy_admin"
TOKEN_PREFIX = "wbt_"
SCOPES = ("readonly", "admin")

MAX_FAILS = 8            # 与内置后台原有行为一致：第 9 次尝试即被锁定
LOCK_SECONDS = 600
MAX_TRACKED = 5000
AUDIT_LIMIT = 500
SESSION_TTL = 12 * 3600
LAST_USED_THROTTLE = 60


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def bearer_session(req: Request) -> tuple[str, bool]:
    """管理会话凭据：优先 X-Admin-Token（SPA 跨源调用），退回管理 cookie。

    返回 (sid, via_header)：头令牌凭「持有即证明」，不需要 CSRF；cookie 鉴权才需 CSRF。
    """
    token = req.headers.get("X-Admin-Token", "")
    return (token or req.cookies.get(COOKIE, "")), bool(token)


def new_secret() -> str:
    return secrets.token_urlsafe(48)


def session_idle_hours() -> int:
    try:
        return int(os.environ.get("WB_SESSION_IDLE_HOURS", "12"))
    except ValueError:
        return 12


# ── 口令哈希 ──────────────────────────────────────────────────────────
def make_hash(pwd: str, iterations: int = 260000) -> str:
    salt = secrets.token_hex(16)
    digest_ = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest_.hex()}"


def verify_pwd(pwd: str, stored: str) -> bool:
    try:
        algo, iters, salt, expect = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest_ = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(digest_.hex(), expect)
    except Exception:  # noqa: BLE001
        return False


def normalize_scope(value: object) -> str:
    """归一化 scope；非法值一律降级为最小权限 readonly。"""
    v = str(value or "").strip().lower()
    return v if v in SCOPES else "readonly"


def scope_to_role(scope: object) -> str:
    return "admin" if normalize_scope(scope) == "admin" else "viewer"


def _restrict_permissions(path) -> None:
    """把敏感文件权限收紧到仅属主可读写（0600）；Windows 上静默忽略失败。"""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


class Security:
    """绑定到某个 store 的管理鉴权服务。"""

    def __init__(self, store, admin_key: str = "", legacy_key_configured: bool = True):
        self.store = store
        self.admin_key = admin_key or ""
        # 这个 admin_key 是不是部署方明确配的（ADMIN_KEY / WB_ADMIN_PASSWORD）。
        # serve.py 会在没配的时候现场随机一个——那种随机值用户根本拿不到，
        # 单密钥入口摆出来就是「输什么都提示密钥不正确」，所以要能区分。
        self.legacy_key_configured = bool(legacy_key_configured)
        self._fail: dict[str, list] = {}
        self._user_fail: dict[str, list] = {}
        self.bootstrap()

    # ── 数据 ────────────────────────────────────────────────────────
    @property
    def data(self) -> dict:
        return self.store.data

    def save(self) -> None:
        try:
            self.store.save()
        except Exception:  # noqa: BLE001  # 审计/会话等旁路失败不应影响请求
            pass

    def bootstrap(self) -> None:
        """首次启动建 admin 用户；密码取 WB_ADMIN_PASSWORD / ADMIN_KEY，否则随机并打印。"""
        with self.store.lock:
            self.store.data.setdefault("secret", new_secret())
            self.store.data.setdefault("users", [])
            self.store.data.setdefault("api_tokens", {})
            self.store.data.setdefault("audit_logs", [])
            self.store.data.setdefault("ip_rules", [])
            self.store.data.setdefault("security", {"enabled": False, "mode": "blacklist"})
            self.store.data.setdefault("api_key_ips", {})
            if self.store.data["users"]:
                return
            pwd = os.environ.get("WB_ADMIN_PASSWORD", "").strip()
            generated = False
            if not pwd:
                pwd = self.admin_key if len(self.admin_key) >= 8 else ""
            if not pwd:
                pwd = secrets.token_urlsafe(9)
                generated = True
            self.store.data["users"].append({
                "username": "admin",
                "role": "admin",
                "pwd_hash": make_hash(pwd),
                "sv": 0,
            })
            self.save()
        if generated:
            import sys
            print("=" * 60, file=sys.stderr)
            print("[reverse-proxy] 已生成初始管理员账号", file=sys.stderr)
            print("  用户名: admin", file=sys.stderr)
            print(f"  密码  : {pwd}", file=sys.stderr)
            print("  请登录后立即在「用户」中修改密码。", file=sys.stderr)
            print("=" * 60, file=sys.stderr)

    # ── 用户 ────────────────────────────────────────────────────────
    def public_users(self) -> list[dict]:
        with self.store.lock:
            return [{"username": u["username"], "role": u.get("role", "viewer")}
                    for u in self.store.data.get("users", [])]

    def find_user(self, username: str) -> dict | None:
        with self.store.lock:
            return next((u for u in self.store.data.get("users", [])
                         if u.get("username") == username), None)

    def add_user(self, username: str, password: str, role: str) -> dict:
        username = str(username or "").strip()[:64]
        if not username:
            raise HTTPException(400, "用户名不能只有空格")
        if not isinstance(password, str) or len(password) < 8:
            raise HTTPException(400, "密码至少 8 位")
        if role not in ("admin", "viewer"):
            raise HTTPException(400, "角色无效")
        with self.store.lock:
            if any(u.get("username") == username for u in self.store.data["users"]):
                raise HTTPException(409, "用户名已存在")
            self.store.data["users"].append({
                "username": username, "role": role,
                "pwd_hash": make_hash(password),
                # 随机初始 sv：删除同名用户再重建时，不会让旧会话「复活」
                "sv": secrets.randbelow(1 << 30),
            })
            self.save()
        return {"username": username, "role": role}

    def update_user(self, username: str, password: str | None, role: str | None) -> dict:
        with self.store.lock:
            target = next((u for u in self.store.data["users"] if u.get("username") == username), None)
            if not target:
                raise HTTPException(404, "用户不存在")
            changed_pwd = changed_role = False
            if password:
                if len(password) < 8:
                    raise HTTPException(400, "密码至少 8 位")
                target["pwd_hash"] = make_hash(password)
                changed_pwd = True
            if role and role != target.get("role"):
                if role not in ("admin", "viewer"):
                    raise HTTPException(400, "角色无效")
                admins = [u for u in self.store.data["users"] if u.get("role") == "admin"]
                if target.get("role") == "admin" and len(admins) <= 1:
                    raise HTTPException(400, "至少保留一个管理员")
                target["role"] = role
                changed_role = True
            if changed_pwd or changed_role:
                try:
                    target["sv"] = int(target.get("sv") or 0) + 1
                except (TypeError, ValueError):
                    target["sv"] = 1
            self.save()
        if changed_pwd or changed_role:
            self.revoke_user_sessions(username)
        return {"username": username, "role": target.get("role", "viewer"),
                "sessions_revoked": changed_pwd or changed_role}

    def delete_user(self, username: str, actor: str) -> dict:
        with self.store.lock:
            users = self.store.data["users"]
            target = next((u for u in users if u.get("username") == username), None)
            if not target:
                raise HTTPException(404, "用户不存在")
            admins = [u for u in users if u.get("role") == "admin"]
            if target.get("role") == "admin" and len(admins) <= 1:
                raise HTTPException(400, "至少保留一个管理员")
            if username == actor:
                raise HTTPException(400, "不能删除当前登录用户")
            self.store.data["users"] = [u for u in users if u.get("username") != username]
            self.save()
        self.revoke_user_sessions(username)
        return {"ok": True}

    # ── 登录防爆破 ──────────────────────────────────────────────────
    def _prune(self, store: dict[str, list]) -> None:
        """把防爆破表裁到 MAX_TRACKED 以内。

        分两步，**顺序不能反**：
          1. 先淘汰已经过了锁定窗口的条目（它们已经没有作用了）；
          2. 如果还是超限，再按「最早开始计数」淘汰，直到降到上限。

        原先只有第 1 步：`expired` 为空就直接 return。而 `record_fail` 是
        在鉴权之前调用的（登录路由一进来就记），所以攻击者只要不断换
        IP / 用户名，表里塞的就全是**没过期的新条目** —— 第 1 步永远为空，
        整张表无上限增长，能一直涨到把进程内存吃光。
        声明了 MAX_TRACKED 却从不真正执行它，等于没有这个上限。

        第 2 步淘汰的是「还在窗口内」的条目：这会放过一些本该被限流的来源，
        但那只是**暂时**放宽限流；而放任内存无限增长是**稳定**的拒绝服务。
        两害相权，选前者。被淘汰的条目下次再失败会从 1 重新计数。
        """
        if len(store) <= MAX_TRACKED:
            return
        now = time.time()
        expired = [k for k, v in store.items() if (now - v[1]) >= LOCK_SECONDS]
        for k in sorted(expired, key=lambda k: store[k][1]):
            store.pop(k, None)
        # 第二步：过期条目不够多的话，按计数起始时间从早到晚继续淘汰。
        overflow = len(store) - MAX_TRACKED
        if overflow > 0:
            oldest = sorted(store, key=lambda k: store[k][1])[:overflow]
            for k in oldest:
                store.pop(k, None)

    def login_blocked(self, ip: str, username: str | None = None) -> bool:
        now = time.time()
        cnt, first = self._fail.get(ip, [0, 0.0])
        if cnt >= MAX_FAILS and (now - first) < LOCK_SECONDS:
            return True
        if username:
            ucnt, ufirst = self._user_fail.get(username.lower(), [0, 0.0])
            if ucnt >= MAX_FAILS and (now - ufirst) < LOCK_SECONDS:
                return True
        return False

    def record_fail(self, ip: str, username: str | None = None) -> None:
        now = time.time()
        cnt, first = self._fail.get(ip, [0, now])
        if (now - first) >= LOCK_SECONDS:
            cnt, first = 0, now
        self._fail[ip] = [cnt + 1, first]
        if username:
            key = username.lower()
            ucnt, ufirst = self._user_fail.get(key, [0, now])
            if (now - ufirst) >= LOCK_SECONDS:
                ucnt, ufirst = 0, now
            self._user_fail[key] = [ucnt + 1, ufirst]
        self._prune(self._fail)
        self._prune(self._user_fail)

    def clear_fail(self, ip: str, username: str | None = None) -> None:
        self._fail.pop(ip, None)
        if username:
            self._user_fail.pop(username.lower(), None)

    # ── 身份解析 ────────────────────────────────────────────────────
    def _session_record(self, req: Request) -> dict | None:
        sid, via_header = bearer_session(req)
        if not sid:
            return None
        with self.store.lock:
            record = self.store.sessions.get(digest(sid))
        if not record or record["expires"] < time.time():
            return None
        if record.get("via") == "token":
            # 令牌换来的会话只是「进门」，不是一张 12 小时的后门票：每个请求都回查
            # 令牌本身，撤销 / 停用 / 过期立刻生效，顺手把会话也清掉。
            # 另外把 token_id 挂到 state 上——`require_session_admin` 就是靠它
            # 继续拒绝「改用户 / 改令牌」这类高危接口，令牌换会话不能绕过这道闸。
            row = self.token_by_id(record.get("token_id") or "")
            if not row or not self.token_usable(row):
                with self.store.lock:
                    self.store.sessions.pop(digest(sid), None)
                raise HTTPException(401, "管理令牌已失效，请重新登录")
            req.state.token_id = row["id"]
        if not via_header and req.method not in ("GET", "HEAD"):
            if not hmac.compare_digest(req.headers.get("X-CSRF-Token", ""), record.get("csrf", "")):
                raise HTTPException(403, "页面已失效，请重新登录")
        hours = session_idle_hours()
        iat = record.get("iat")
        if hours > 0 and isinstance(iat, (int, float)) and (time.time() - iat) > hours * 3600:
            raise HTTPException(401, "登录已超时（长时间未操作），请重新登录")
        if hours > 0 and isinstance(iat, (int, float)) and (time.time() - iat) > (hours * 3600) / 3:
            record["iat"] = int(time.time())  # 滑动续期
        return record

    def _token_identity(self, req: Request, token: str) -> dict:
        row = self.resolve_token(token)
        if not row or not self.token_usable(row):
            self.record_fail(client_ip(req))
            raise HTTPException(401, "未登录")
        self.clear_fail(client_ip(req))
        self.touch_token(row["id"], client_ip(req))
        req.state.token_id = row["id"]
        return {"username": f"token:{row['name']}", "role": scope_to_role(row["scope"]), "via": "token"}

    def current_user(self, req: Request) -> dict:
        """解析请求身份。会话优先，其次作用域化管理令牌（`wbt_`）。"""
        session_err: HTTPException | None = None
        try:
            record = self._session_record(req)
        except HTTPException as exc:
            record, session_err = None, exc
        if record:
            # role 缺省用 viewer，与其余几处一致。真出现缺 role 的记录时
            # 宁可降权 —— 这里默认 admin 等于「数据缺了就自动提权」。
            return {
                "username": record.get("username", "admin"),
                "role": record.get("role") or "viewer",
                "via": record.get("via", "session"),
            }
        token = self._bearer_token(req)
        if token:
            return self._token_identity(req, token)
        raise session_err or HTTPException(401, "请先登录管理后台")

    def require_admin(self, req: Request) -> dict:
        user = self.current_user(req)
        if user.get("role") != "admin":
            raise HTTPException(403, "需要管理员权限")
        return user

    def require_session_admin(self, req: Request) -> dict:
        """要求**会话**管理员：拒绝用 API 令牌调用（高危 / 不可逆接口）。

        注意：用管理令牌换来的会话**同样被拒**。那种会话的 `via` 是 "token"，
        `_session_record` 会把 token_id 挂到 `req.state` 上，就是留给这里判的。
        换句话说，「令牌能进控制台」不等于「令牌能改用户和令牌」。
        """
        user = self.require_admin(req)
        if getattr(req.state, "token_id", None) is not None:
            raise HTTPException(403, "该接口不接受 API 令牌，请用会话登录后操作")
        return user

    @staticmethod
    def _bearer_token(req: Request) -> str:
        auth = req.headers.get("authorization", "") or ""
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not tok:
            tok = (req.headers.get("x-api-key") or "").strip()
        return tok if tok.startswith(TOKEN_PREFIX) else ""

    # ── 会话 ────────────────────────────────────────────────────────
    def start_session(self, username: str, role: str, via: str = "session",
                      token_id: str = "") -> tuple[str, str]:
        """建立会话，返回 (sid, csrf)。会话记录带 username/role/iat。

        `token_id` 只有「管理令牌换会话」这一条路会带。留着它是为了让后续每个请求
        都能回查令牌本身是否还有效（见 `_session_record`），否则撤销令牌之后，
        已经换出去的会话还能继续用满 12 小时。
        """
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        with self.store.lock:
            self.store.sessions = {k: v for k, v in self.store.sessions.items() if v["expires"] > now}
            if len(self.store.sessions) >= 100:
                self.store.sessions.pop(next(iter(self.store.sessions)))
            self.store.sessions[digest(sid)] = {
                "csrf": csrf, "expires": now + SESSION_TTL,
                "username": username, "role": role, "via": via, "iat": int(now),
                "token_id": token_id,
            }
        return sid, csrf

    def end_session(self, req: Request) -> None:
        sid, _ = bearer_session(req)
        if not sid:
            return
        with self.store.lock:
            self.store.sessions.pop(digest(sid), None)

    def session_csrf(self, req: Request) -> str:
        """返回当前会话的 CSRF（供内置静态后台写入头部）；令牌身份返回空串。"""
        try:
            record = self._session_record(req)
        except HTTPException:
            return ""
        return (record or {}).get("csrf", "")

    def revoke_user_sessions(self, username: str) -> int:
        """踢掉某用户全部已登录会话（改密码 / 改角色 / 删号后调用）。返回踢掉的数量。

        比较时必须套用和 current_user() 一样的缺省值。

        读取侧（current_user）对空 username 用的是 record.get("username", "admin")，
        所以一条 username="" 的会话是**以 admin 身份通过鉴权**的；
        而这里原先直接比 v.get("username") != username，空串永远不等于任何
        调用方传来的名字 —— 于是那条会话**既以 admin 身份有效，又踢不掉**。

        今天所有登录路径都传了非空 username，所以这是个「潜伏」的不一致；
        但只要多一个 start_session("", ...) 的调用点，它就会变成
        「改完密码，攻击者还挂在里面」。这种 bug 不值得等它发生。
        """
        with self.store.lock:
            before = len(self.store.sessions)
            self.store.sessions = {
                k: v for k, v in self.store.sessions.items()
                # 与 current_user 的读取口径保持一致：空值按 "admin" 算。
                if (v.get("username") or "admin") != (username or "admin")
            }
            return before - len(self.store.sessions)

    # ── 登录 ────────────────────────────────────────────────────────
    def verify_login(self, username: str, password: str) -> dict | None:
        row = self.find_user(username)
        if not row or not verify_pwd(password, row.get("pwd_hash", "")):
            return None
        return {"username": row["username"], "role": row.get("role", "viewer")}

    def verify_legacy_key(self, key: str) -> bool:
        """校验旧的单密钥 `ADMIN_KEY` 登录（内置静态后台用）。

        入口开不开由 `legacy_key_enabled()` 一处说了算，这里不再自己判一遍——
        「登录页藏起来了、接口却还认」是最坏的一种状态：藏起来的入口等于一条
        没人记得的旁路，而它偏偏在首启那次是有效的（随机密钥恰好就是打印出来的
        首启口令），重启后失效，于是表现为「昨天能登今天不能」，极难排查。
        """
        if not self.legacy_key_enabled():
            return False
        return bool(self.admin_key) and hmac.compare_digest(str(key or ""), self.admin_key)

    def legacy_key_enabled(self) -> bool:
        """单密钥入口在当前部署下是否真的可用（登录页和校验都看这一处）。

        两个「否」：部署方用 `WB_ALLOW_ADMIN_KEY=0` 关掉了；或者 `admin_key` 是
        serve.py 现场随机的——桌面端托管就是后者，那种值每次启动都换、也从不展示，
        摆在登录页只会让人反复被告知「管理密钥不正确」。
        """
        if os.environ.get("WB_ALLOW_ADMIN_KEY", "1") == "0":
            return False
        return bool(self.admin_key) and self.legacy_key_configured

    def has_tokens(self) -> bool:
        """是否已经建过至少一枚管理令牌（没建过就不必摆「管理令牌」入口）。"""
        with self.store.lock:
            return bool(self.store.data.get("api_tokens"))

    def issue_cookie(self, response, sid: str, secure: bool) -> None:
        response.set_cookie(COOKIE, sid, secure=secure, httponly=True,
                            samesite="strict", path="/admin", max_age=SESSION_TTL)

    def delete_cookie(self, response, secure: bool) -> None:
        response.delete_cookie(COOKIE, path="/admin", secure=secure,
                               httponly=True, samesite="strict")

    # ── 作用域化管理令牌（只读 / 管理员）───────────────────────────
    def list_tokens(self) -> list[dict]:
        with self.store.lock:
            rows = list(self.store.data.get("api_tokens", {}).items())
        rows.sort(key=lambda kv: kv[1].get("created_at", 0), reverse=True)
        return [self._public_token(tid, item) for tid, item in rows]

    @staticmethod
    def _public_token(tid: str, item: dict) -> dict:
        return {
            "id": tid, "name": item.get("name", ""), "prefix": item.get("prefix", ""),
            "scope": item.get("scope", "readonly"), "enabled": bool(item.get("enabled", True)),
            "expires_at": item.get("expires_at"), "created_at": item.get("created_at"),
            "created_by": item.get("created_by", ""), "last_used_at": item.get("last_used_at"),
            "last_used_ip": item.get("last_used_ip", ""),
        }

    def create_token(self, name: str, scope: object, expires_at, created_by: str = "") -> dict:
        name = str(name or "").strip()[:64]
        if not name:
            raise HTTPException(400, "令牌名称不能只有空格")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        tid = secrets.token_hex(8)
        exp = int(expires_at) if expires_at else None
        with self.store.lock:
            if len(self.store.data["api_tokens"]) >= 100:
                raise HTTPException(400, "最多保存 100 枚管理令牌")
            self.store.data["api_tokens"][tid] = {
                "name": name, "hash": digest(token), "prefix": token[:12],
                "scope": normalize_scope(scope), "enabled": True, "expires_at": exp,
                "created_at": int(time.time()), "created_by": str(created_by or "")[:64],
            }
            self.save()
        out = self._public_token(tid, self.store.data["api_tokens"][tid])
        out["token"] = token  # 仅此一次返回明文
        return out

    def update_token(self, tid: str, patch: dict) -> dict:
        with self.store.lock:
            item = self.store.data["api_tokens"].get(tid)
            if not item:
                raise HTTPException(404, "令牌不存在")
            if "name" in patch and patch["name"] is not None:
                if not str(patch["name"]).strip():
                    raise HTTPException(400, "令牌名称不能只有空格")
                item["name"] = str(patch["name"]).strip()[:64]
            if "enabled" in patch:
                item["enabled"] = bool(patch["enabled"])
            if "scope" in patch:
                item["scope"] = normalize_scope(patch["scope"])
            if "expires_at" in patch:
                v = patch["expires_at"]
                item["expires_at"] = int(v) if v else None
            self.save()
            return self._public_token(tid, item)

    def delete_token(self, tid: str) -> dict:
        with self.store.lock:
            if tid not in self.store.data["api_tokens"]:
                raise HTTPException(404, "令牌不存在")
            del self.store.data["api_tokens"][tid]
            self.save()
        return {"ok": True}

    def token_by_id(self, tid: object) -> dict | None:
        """按 id 取管理令牌行（不含明文——明文只在创建那一次返回）。

        「令牌换会话」的会话要靠它回查令牌是否仍然有效，所以这里必须容忍
        id 为空 / 不是字符串的情况，直接当「没找到」处理。
        """
        if not tid or not isinstance(tid, str):
            return None
        with self.store.lock:
            item = self.store.data.get("api_tokens", {}).get(tid)
        return {"id": tid, **item} if item else None

    def resolve_token(self, token: object) -> dict | None:
        if not token or not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            return None
        hashed = digest(token)
        with self.store.lock:
            for tid, item in self.store.data.get("api_tokens", {}).items():
                if hmac.compare_digest(item.get("hash", ""), hashed):
                    return {"id": tid, **item}
        return None

    @staticmethod
    def token_usable(row: dict) -> bool:
        if not row.get("enabled", True):
            return False
        exp = row.get("expires_at")
        return not exp or int(exp) > int(time.time())

    def touch_token(self, tid: str, ip: str) -> None:
        try:
            now = int(time.time())
            with self.store.lock:
                item = self.store.data.get("api_tokens", {}).get(tid)
                if not item:
                    return
                if item.get("last_used_at") and now - int(item["last_used_at"]) < LAST_USED_THROTTLE:
                    return
                item["last_used_at"] = now
                item["last_used_ip"] = str(ip or "")[:64]
        except Exception:  # noqa: BLE001
            pass

    # ── 审计 ────────────────────────────────────────────────────────
    def audit(self, actor: dict | None, action: str, target: str = "", detail: str = "") -> None:
        try:
            entry = {
                "time": int(time.time()),
                "actor": (actor or {}).get("username") or "anonymous",
                "action": str(action), "target": str(target or ""),
                "detail": str(detail or "")[:500],
            }
            with self.store.lock:
                logs = self.store.data.setdefault("audit_logs", [])
                logs.append(entry)
                if len(logs) > AUDIT_LIMIT:
                    del logs[:len(logs) - AUDIT_LIMIT]
            # 审计必须能扛住重启：多数调用点是在自己那次 store.save() 之后才 audit() 的
            # （例如改池设置），如果这里不自己落盘，这条记录就只活在内存里，
            # 服务一重启就查不到「谁改了什么」。审计本身低频，逐条写盘没有压力。
            self.store.save()
        except Exception:  # noqa: BLE001  # 审计是旁路，异常绝不影响请求
            pass

    def audit_logs(self, limit: int = 200) -> list[dict]:
        with self.store.lock:
            logs = list(self.store.data.get("audit_logs", []))
        logs.reverse()
        return logs[: max(1, min(AUDIT_LIMIT, int(limit)))]

    def clear_audit_logs(self) -> int:
        """清空审计日志，返回被清掉的条数。

        只清历史，不清「即将写入的那条」——调用方要在清空之后再 audit()，
        这样「谁清空的」本身会留在新日志里，形成可追溯链条。
        """
        with self.store.lock:
            removed = len(self.store.data.get("audit_logs", []))
            self.store.data["audit_logs"] = []
            self.store.save()
        return removed
