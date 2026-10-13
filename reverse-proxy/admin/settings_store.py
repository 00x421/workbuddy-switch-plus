"""服务设置的加密存储（`state.json` 顶层键 `service_settings`）。

设计要点：

1. **密文不落明文。** 密钥从环境变量 `WB_SETTINGS_KEY` 读 ——
   密钥本身绝不写进 state.json，否则「加密存储」只是把明文换了个写法
   （密钥和密文躺在同一个文件里）。

2. **没有密钥时明确报错，不静默降级成明文。** 静默写明文是最坏的结果：
   管理员以为它加密了，实际没有。所以 `encrypt_secret()` 在缺密钥时抛错，
   由接口回 400 并给出可操作的提示。

3. **`WB_SETTINGS_KEY` 缺失/不可用时退回本机自有密钥，而不是让服务不可用。**
   本项目的定位是单机自用（`state.json` 就在用户自己磁盘上，
   里面同样存着 API Key 明文），所以「机器上另存一份 0600 的随机密钥」
   与现有安全模型一致，同时保证**不落明文**。来源会在响应里如实说明
   （`key_source`），管理员看得到真相。

4. **合并语义是「只改传进来的字段」。** PUT 是局部更新：没提到的字段保持原样。
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
    """
    return {
        "site_name": "",
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
    """逐字段合并。非 dict 的一律忽略，保留原值。"""
    out = dict(base)
    for k, v in (patch or {}).items():
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
    if "site_name" in patch and patch["site_name"] is not None:
        out["site_name"] = str(patch["site_name"]).strip()[:60]
    return out


# ── 用户系统总闸 ───────────────────────────────────────────

#: 总闸关闭时对外的那一句话。用户能看懂是硬要求 —— 他看到的是
#: 「注册页点不动」，需要知道这不是坏了、而是管理员没开。


# ── 密钥与加密 ──────────────────────────────────────────────────────
#: 运行数据目录。由 serve.build_app 在解析完 `--data` 后写入。
#: 之所以要有这个变量：`_secret_path()` 原先只看 `WB_SWITCH_DATA` 环境变量，
#: **完全不理 `--data`**。源码运行时两者恰好一致（都在 ./data），
#: 但冻结成 exe 后 `__file__` 落在 `<_MEIPASS>/admin/`（_MEIPASS 是
#: `%TEMP%\_MEIxxxxx`，每次启动随机），密钥就跟着落进临时目录 ——
#: 重启一次换一把密钥，上一次存的 SMTP 授权码再也解不开。
#: 运行时目录是**唯一权威**，这里必须认它。
_runtime_data_root: Path | None = None


def set_runtime_data_root(root) -> None:
    """记录本次运行的 data 目录。serve.build_app 在解析完参数后调用。"""
    global _runtime_data_root, _cached_key, _cached_source
    _runtime_data_root = Path(root) if root else None
    # 目录变了，密钥缓存必须失效，否则会用上一轮的密钥去解这一轮的密文。
    _cached_key, _cached_source = None, ""


def _secret_path() -> Path:
    """密钥文件位置：运行数据目录（state.json 所在目录）旁边。

    取值优先级：显式设置的数据目录 > 环境变量 > 源码旁的 data/。

    最后一档只在「源码运行且没指定 data」时才走到 —— 此时
    `__file__` 的 parents[1] 就是仓库的 reverse-proxy/，与历史行为一致。
    冻结形态下 serve 一定会先调 set_runtime_data_root，所以不会落到这里。
    """
    if _runtime_data_root is not None:
        return _runtime_data_root / KEY_FILENAME
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


def apply_patch(store, patch: dict) -> dict:
    """把归一化后的补丁写进 store（调用方须已登录管理员）。返回脱敏后的设置。"""
    clean = normalize(patch)
    with store.lock:
        current = store.data.get(KEY)
        if not isinstance(current, dict):
            current = {}
        merged = _merge(defaults(), current)
        candidate = _merge(merged, clean)
        store.data[KEY] = candidate
        store.save()
        saved = json.loads(json.dumps(store.data[KEY], ensure_ascii=False))
    return public_view(saved)


def public_view(settings: dict) -> dict:
    """给界面看的视图。**口令永远只有 has_password 一个布尔值。**"""
    raw = dict(settings or {})
    raw.pop("password", None)
    raw["settings_key_source"] = key_source()
    return raw
