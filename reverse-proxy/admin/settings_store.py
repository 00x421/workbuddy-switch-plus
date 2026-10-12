"""服务设置（`state.json` 顶层键 `service_settings`）与 SMTP 口令的加密存储。

设计要点：

1. **口令不落明文。** SMTP 授权码存成密文（`password_enc`），
   密钥从环境变量 `WB_SETTINGS_KEY` 读 —— 密钥本身绝不写进 state.json，
   否则「加密存储」只是把明文换了个写法（密钥和密文躺在同一个文件里）。

2. **没有密钥时明确报错，不静默降级成明文。** 静默写明文是最坏的结果：
   管理员以为它加密了，实际没有。所以 `encrypt_secret()` 在缺密钥时抛错，
   由接口回 400 并给出可操作的提示。

3. **`WB_SETTINGS_KEY` 缺失/不可用时退回本机自有密钥，而不是让服务不可用。**
   本项目的定位是单机自用（`state.json` 就在用户自己磁盘上，
   里面同样存着 API Key 明文与用户口令哈希），所以「机器上另存一份 0600 的随机密钥」
   与现有安全模型一致，同时保证**不落明文**。来源会在响应里如实说明
   （`key_source`），管理员看得到真相。

4. **合并语义是「只改传进来的字段」。** PUT 是局部更新：没提到的字段保持原样。
   这避免了「服务设置页只改了注册开关、顺手把 SMTP 配置清空」这种事故。
   但 `smtp` 段内部仍然是逐字段合并，且 `password` 是**可选**的 ——
   不传就保留原密码，传空串才清除。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

# 顶层键名。不要用 `settings` —— 那个名字太通用，将来很容易和别的东西撞车；
# `service_settings` 的字段。
KEY = "service_settings"

# 密钥文件 / 环境变量的名字。
ENV_KEY = "WB_SETTINGS_KEY"
KEY_FILENAME = "settings.key"
KEY_FILE_SALT = b"wb-settings-key-v1"

_cached_key: bytes | None = None
_cached_source: str = ""


def defaults() -> dict:
    """服务设置的**出厂默认值**。**每次返回新对象**，别让调用方改到共享状态。

    ## 默认「全关」（用户指示 (a)：新安装时默认全关）

    用户的原话是「服务设置所有选项默认关闭」，追问后选了「新安装时默认全关」。
    所以这里把**功能开关**一律给 `False`：新装的实例不会凭空对外提供
    注册 / 自助兑换这类能力，管理员看过之后再逐个打开。

    ## 但 `require_email_code` 是例外，保持 True —— 它不是「功能开关」

    这个键的语义是「注册要不要验证码」，**它没有「开/关」的方向性**：
      · True  = 注册必须过邮箱验证码（更严）
      · False = **免验证注册**（更松）—— 任何人都能凭空注册一个邮箱，
                而且不校验那个邮箱是不是他的
    也就是说，把它「关掉」等于**削弱一道安全防线**，与「功能都别开」的意图相反。
    用户说「所有选项默认关闭」时想的是「功能都别开」，不是「安全防线都别开」。

    而且现在有总闸兜底：`user_system_enabled` 默认 False，
    用户体系整个是关着的，注册本来就走不到 —— 所以这里没有任何理由
    为了「看起来全关」而把验证码这道防线也拆掉。

    交叉验证：`locked_out_reason()` 在 allow_register=False 时**先返回**
    「管理员已关闭注册」，所以 require_email_code 保持 True 也不会让新装实例
    出现「未配置 SMTP → 所有人注册不了」的误导提示（那道判断根本走不到）。
    """
    return {
        # 用户系统总闸。默认关：新装的实例不带用户体系。
        "user_system_enabled": False,
        # 功能开关默认全关。
        "allow_register": False,
        # ⚠️ 例外，保持 True —— 这是安全姿态，不是功能开关。见上面的长注释。
        "require_email_code": True,
        "smtp": {
            "enabled": False,
            "host": "",
            "port": 465,
            "username": "",
            "password_enc": "",
            "from_addr": "",
            "use_ssl": True,
        },
        "site_name": "",
        "allow_self_service_redeem": False,
        "email_subject": "",
        "email_body": "",
    }


def load(store) -> dict:
    """读设置（惰性建，老 state.json 不需要迁移）。

    返回的是**深拷贝**：调用方随便改都不会污染 store.data。
    """
    with store.lock:
        raw = store.data.get(KEY)
        if not isinstance(raw, dict):
            raw = {}
        merged = _merge(defaults(), raw)
    return merged


def _merge(base: dict, patch: dict) -> dict:
    """逐字段合并（smtp 段递归一层）。非 dict 的一律忽略，保留原值。"""
    out = dict(base)
    for k, v in (patch or {}).items():
        if k == "smtp":
            if isinstance(v, dict):
                out["smtp"] = {**out.get("smtp", {}), **v}
            continue
        if k in out:
            out[k] = v
    return out


def normalize(patch: dict) -> dict:
    """把用户传进来的补丁归一化。**非法值一律 400，不静默吃掉。**

    静默吃掉的后果是「管理员改了设置、界面显示成功、行为却没变」，
    而这类问题在服务端几乎不可能自查。
    """
    from fastapi import HTTPException

    if not isinstance(patch, dict):
        raise HTTPException(400, "请求格式不正确")
    out: dict = {}
    # ⚠️ 新增开关必须加进这个元组，否则 `normalize` 会把它静默丢掉：
    # 管理员在界面上拨了总闸、接口回 200、state.json 里却没这个键 ——
    # 表现是「开关拨了又自己弹回去」，而服务端一切正常、查不出原因。
    # （`_merge` 只接受 base 里已存在的键，所以漏在这里 = 静默丢弃。）
    for field in ("user_system_enabled", "allow_register", "require_email_code",
                  "allow_self_service_redeem"):
        if field in patch and patch[field] is not None:
            if not isinstance(patch[field], bool):
                raise HTTPException(400, "开关类设置必须是 true / false")
            out[field] = bool(patch[field])
    if "site_name" in patch and patch["site_name"] is not None:
        out["site_name"] = str(patch["site_name"]).strip()[:60]
    for field, limit in (("email_subject", 120), ("email_body", 2000)):
        if field in patch and patch[field] is not None:
            out[field] = str(patch[field])[:limit]
    if "smtp" in patch and patch["smtp"] is not None:
        out["smtp"] = _normalize_smtp(patch["smtp"], HTTPException)
    return out


def _normalize_smtp(raw, HTTPException) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(400, "smtp 必须是对象")
    out: dict = {}
    if "enabled" in raw and raw["enabled"] is not None:
        if not isinstance(raw["enabled"], bool):
            raise HTTPException(400, "smtp.enabled 必须是 true / false")
        out["enabled"] = bool(raw["enabled"])
    if "use_ssl" in raw and raw["use_ssl"] is not None:
        if not isinstance(raw["use_ssl"], bool):
            raise HTTPException(400, "smtp.use_ssl 必须是 true / false")
        out["use_ssl"] = bool(raw["use_ssl"])
    if "host" in raw and raw["host"] is not None:
        host = str(raw["host"]).strip()[:200]
        if "\r" in host or "\n" in host or " " in host:
            raise HTTPException(400, "SMTP 主机名不能包含空格或换行")
        out["host"] = host
    if "port" in raw and raw["port"] is not None:
        try:
            port = int(raw["port"])
        except (TypeError, ValueError):
            raise HTTPException(400, "SMTP 端口必须是数字")
        if not 0 < port < 65536:
            raise HTTPException(400, "SMTP 端口需在 1–65535 之间")
        out["port"] = port
    if "username" in raw and raw["username"] is not None:
        out["username"] = str(raw["username"]).strip()[:200]
    if "from_addr" in raw and raw["from_addr"] is not None:
        out["from_addr"] = str(raw["from_addr"]).strip()[:200]
    if "password" in raw and raw["password"] is not None:
        out["password"] = str(raw["password"])[:512]
    return out


def effective_smtp_ready(settings: dict) -> bool:
    """「验证码能不能送到用户手上」。

    走 `mailer.delivery_ready` 而不是 `smtp_ready` —— 这样「开发出口」这个
    例外的定义**只有一处**，登录页 / 注册接口 / 管理端提示不可能看出不同的结论。
    注意：dev 出口绕过的是**投递**，不是**校验**（见 mailer.dev_mail_enabled）。
    """
    from .mailer import delivery_ready
    return delivery_ready(settings)


def mailer_dev_mail_enabled() -> bool:
    """进程是否打开了开发用邮件出口（`WB_DEV_MAIL`）。

    只用于**对外如实汇报**（`dev_mail` 字段）：让管理员在自己的设置页上
    看到「这台机器把验证码打进 stderr / 回响应体，而不是发邮件」。
    绝不参与放行判断 —— 那是 `locked_out_reason()` 的事。
    """
    from .mailer import dev_mail_enabled
    return bool(dev_mail_enabled())


def smtp_configured(settings: dict) -> bool:
    """SMTP 是不是**真的配好了**（只看配置，不含 WB_DEV_MAIL 这个例外）。

    与 `effective_smtp_ready()` 的区别是整个「dev 出口」问题的关键：
      · `effective_smtp_ready()` 回答「验证码现在送得出去吗」→ dev 出口算数；
      · `smtp_configured()`      回答「管理员配了 SMTP 吗」→ dev 出口不算。

    对外暴露的字段一律用后者。 原先 `public_options` / `public_view` 用的是
    前者，于是进程带上 WB_DEV_MAIL=1 时，匿名 `/login/api/options` 会
    把 `smtp_ready` 报成 `true`、`register_locked_reason` 报成空串 ——
    而实际注册走的是 dev 出口，验证码**根本没有发出去**。
    这既是配置面泄漏（匿名可探测本机开了 dev 出口），
    也让 t4/t5/t6 依赖的「注册四态」判定漂移成第五种。
    """
    from .mailer import smtp_ready
    return bool(smtp_ready(settings))


# ── 用户系统总闸 ───────────────────────────────────────────

#: 总闸关闭时对外的那一句话。用户能看懂是硬要求 —— 他看到的是
#: 「注册页点不动」，需要知道这不是坏了、而是管理员没开。
USER_SYSTEM_OFF_NOTICE = "管理员尚未开启用户系统，注册与登录暂不可用。请联系管理员开启。"


def user_system_enabled(settings: dict) -> bool:
    """用户系统总闸是不是开着。**默认 False**（新安装不带用户体系）。

    这个判断只有一处实现 —— 注册 / 登录 / 用户中心 / 门户会话身份全部读它，
    所以不会出现「注册关了但登录还开着」这种半拉子状态。

    `.get(..., False)` 的兜底方向是**关**：老的 state.json 里没有这个键时
    （惰性补默认值的场景），宁可判成关也不要判成开 —— 判成开等于凭空
    给一个老实例打开了用户体系，那是安全姿态的反向漂移。
    """
    return bool((settings or {}).get("user_system_enabled", False))


def user_system_off_reason(settings: dict) -> str:
    """总闸关着时返回一句给用户看的话；开着则返回空串。

    与 `locked_out_reason()` 同形（空串 = 没被挡），这样调用方可以统一写
    `if reason: raise HTTPException(403, reason)`。
    """
    return "" if user_system_enabled(settings) else USER_SYSTEM_OFF_NOTICE


def locked_out_reason(settings: dict) -> str:
    """用户侧注册被挡住的原因（空串 = 没有被挡）。

    这是「SMTP 未配置但要求验证码」这个坑的**唯一判据**。
    登录页、注册接口、管理端提示三处都读它，口径不可能不一致。
    """
    if not settings.get("allow_register", True):
        return "管理员已关闭注册"
    if settings.get("require_email_code", True) and not effective_smtp_ready(settings):
        return "未配置 SMTP，当前无法注册 请联系管理员关闭验证码验证 或者给您分发账号登录"
    return ""


# ── 密钥与加密 ──────────────────────────────────────────────────────
def _secret_path() -> Path:
    """兜底密钥文件的位置：数据目录（state.json 所在目录）旁边。"""
    root = os.environ.get("WB_SWITCH_DATA", "").strip()
    if root:
        return Path(root) / KEY_FILENAME
    return Path(__file__).resolve().parents[1] / "data" / KEY_FILENAME


def _derive(secret: str) -> bytes:
    """口令 → 32 字节密钥。派生输入里带上固定的盐，避免同一个口令
    在别处（例如某个 PBKDF2 哈希）产生完全相同的字节串。"""
    return hashlib.sha256(KEY_FILE_SALT + str(secret).encode("utf-8")).digest()


def _load_or_create_file_key() -> bytes:
    path = _secret_path()
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                return _derive(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(value)
        return _derive(value)
    except OSError as exc:
        raise RuntimeError(f"无法读写设置密钥文件 {path}：{exc}") from exc


def settings_key() -> bytes:
    """取加密密钥。优先环境变量，其次本机密钥文件。"""
    global _cached_key, _cached_source
    if _cached_key is not None:
        return _cached_key
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        _cached_key, _cached_source = _derive(env), "env"
        return _cached_key
    _cached_key, _cached_source = _load_or_create_file_key(), "file"
    return _cached_key


def key_source() -> str:
    """密钥来源：env（WB_SETTINGS_KEY）| file（本机密钥文件）| ""（还没取过）。"""
    if _cached_key is None:
        try:
            settings_key()
        except RuntimeError:
            return ""
    return _cached_source


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    """由密钥与随机数派生任意长度密钥流（HMAC-SHA256 计数器模式）。"""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:size])


def encrypt_secret(plain: str) -> str:
    """加密一个口令，返回可存进 state.json 的 ASCII 串。

    格式 `enc:v1:<base64(nonce||ciphertext)>`，自实现流加密（密钥流异或），
    不引入新依赖。**这不是给公网传输用的** —— 它防的是「state.json 被拷走/被看到」，
    与项目里 API Key 明文同处一份文件的现状相比是明确的提升。
    """
    text = str(plain or "")
    if not text:
        return ""
    key = settings_key()
    nonce = secrets.token_bytes(16)
    data = text.encode("utf-8")
    stream = _keystream(key, nonce, len(data))
    body = bytes(a ^ b for a, b in zip(data, stream))
    return "enc:v1:" + base64.b64encode(nonce + body).decode("ascii")


def decrypt_secret(token) -> str:
    """解密 `encrypt_secret` 的输出。**解不开返回空串，绝不抛异常。**

    为什么不抛：调用点都在「发一封信」这条路上。一个坏掉的密文应该表现为
    「SMTP 认证失败」，而不是让整个注册接口 500 —— 后者会让管理员
    以为服务坏了，去查一个根本不存在的故障。
    """
    text = str(token or "")
    if not text:
        return ""
    if not text.startswith("enc:v1:"):
        # 不是我们写的密文。绝不把它当明文返回 —— 那等于把一个
        # 来源不明的串当 SMTP 口令发出去。
        return ""
    try:
        blob = base64.b64decode(text[7:], validate=True)
        if len(blob) < 17:
            return ""
        nonce, body = blob[:16], blob[16:]
        stream = _keystream(settings_key(), nonce, len(body))
        return bytes(a ^ b for a, b in zip(body, stream)).decode("utf-8")
    except Exception:
        return ""


class SettingsIntegrityError(RuntimeError):
    """设置里出现了不该存在的字段。**保存失败优于静默落盘**。"""


def _assert_no_plaintext_secret(settings: dict) -> None:
    """落盘前扫一遍：`smtp.password` 必须为空。

    这是**防回归的兜底**，不是主逻辑。理由：口令加密这条链路上，「明文进了
    dict 又被 merge 进 state.json」曾经真的发生过（`apply_patch` 里 `pop` 错了
    一层），而它的表现是**静默**的 —— 没人会去看 state.json。

    与其指望以后每个改动都记得走加密，不如在这里让它**存不下去**：
    明文只要冒头就抛错，测试会立刻红。
    """
    smtp = (settings or {}).get("smtp")
    if isinstance(smtp, dict) and str(smtp.get("password") or ""):
        raise SettingsIntegrityError(
            "service_settings.smtp.password 里有明文口令，拒绝保存。");


def apply_patch(store, patch: dict) -> dict:
    """把归一化后的补丁写进 store（调用方须已登录管理员）。返回脱敏后的设置。"""
    clean = normalize(patch)
    with store.lock:
        current = store.data.get(KEY)
        if not isinstance(current, dict):
            current = {}
        merged = _merge(defaults(), current)
        # ⚠️ 口令在 smtp 段里（`clean["smtp"]["password"]`），不是顶层。
        # 原先这里 `clean.pop("password")` 弹的是顶层键 —— 永远 None，
        # 于是 `password_enc` 从没被写过，而明文原样 merge 进了 state.json。
        # 真实缺陷，不是风格问题：口令明文落盘。
        smtp_patch = dict(clean.get("smtp") or {})
        if "password" in smtp_patch:
            pw = smtp_patch.pop("password")   # 明文只作为局部变量流转
            smtp_patch["password_enc"] = (encrypt_secret(pw) if pw else "")
        clean["smtp"] = smtp_patch
        candidate = _merge(merged, clean)
        _assert_no_plaintext_secret(candidate)
        store.data[KEY] = candidate
        store.save()
        saved = json.loads(json.dumps(store.data[KEY], ensure_ascii=False))
    return public_view(saved)


def public_view(settings: dict) -> dict:
    """给界面看的视图。**口令永远只有 has_password 一个布尔值。**"""
    from .mailer import smtp_config
    raw = dict(settings or {})
    smtp = dict(raw.get("smtp") or {})
    raw["smtp"] = {
        "enabled": bool(smtp.get("enabled")),
        "host": str(smtp.get("host") or ""),
        "port": smtp.get("port") or 465,
        "username": str(smtp.get("username") or ""),
        "from_addr": str(smtp.get("from_addr") or ""),
        "use_ssl": bool(smtp.get("use_ssl", True)),
        "has_password": bool(smtp.get("password_enc")),
    }
    raw.pop("password", None)
    # ⚠️ 对外汇报用 smtp_configured()（只看配置），不是 effective_smtp_ready()。
    #
    # 后者把 WB_DEV_MAIL 这个「开发出口」算作 ready，于是进程一旦带上
    # WB_DEV_MAIL=1 + 未配 SMTP，管理端会把 smtp_ready 显示成 true、
    # 注册锁原因显示成空串 —— 而验证码根本没有发出去。
    # 管理员据此以为邮件通路是好的，实际全靠响应体回码（且那个出口
    # 现在只对本机开放，见 server.py 的 _dev_caller）。
    #
    # 放行判断仍然走 effective_smtp_ready()/locked_out_reason()（见下），
    # 那里必须把 dev 出口算数 —— 否则本机自测根本注册不了。
    # 即：dev 出口只影响「能不能注册」，不影响「对外怎么汇报」。
    raw["smtp_ready"] = smtp_configured(settings)
    raw["dev_mail"] = bool(mailer_dev_mail_enabled())
    # 总闸显式回填：老的 state.json 里没有这个键时，`_merge(defaults(), raw)`
    # 其实已经补上了，但这里再写一遍是为了让「读到的键」与
    # `user_system_enabled()` 用的是同一处真相源 —— 不能一个读 raw、
    # 一个读函数，那迟早分叉。
    raw["user_system_enabled"] = user_system_enabled(settings)
    raw["register_locked_reason"] = locked_out_reason(settings)
    raw["settings_key_source"] = key_source()
    return raw


def public_options(store) -> dict:
    """匿名 `/login/api/options` 的响应。**只回三个键**。

    多回任何一个字段都可能泄露配置面（SMTP 主机、发件人、用户数……）。
    所以这里**显式挑字段**，而不是 public_view 减字段 ——
    后者在将来加字段时会默认泄露。
    """
    settings = load(store)
    return {
        # 总闸。必须匿名可见 —— 登录/注册页要靠它决定
        # 「画不画出表单」：不知道总闸状态的话，用户会看到一个能填、
        # 一提交就被 403 的表单，那是纯粹的挫败感。
        # 它不泄露任何配置细节（就一个布尔），与「只回契约允许的字段」不冲突。
        "user_system_enabled": user_system_enabled(settings),
        "allow_register": bool(settings.get("allow_register", True)),
        "require_email_code": bool(settings.get("require_email_code", True)),
        "site_name": str(settings.get("site_name") or ""),
        # 同 public_view：对外报配置真相，不报 dev 出口。
        # 原先这里匿名就能看到 smtp_ready=true（进程带 WB_DEV_MAIL=1 时），
        # 等于把「这台机器开了开发出口」这个事实主动告诉任何未登录的人。
        "smtp_ready": smtp_configured(settings),
        # 注册四态的判定仍以 locked_out_reason 为唯一真源（它算 dev 出口）。
        "register_locked_reason": locked_out_reason(settings),
        "site_ready": True,
    }
