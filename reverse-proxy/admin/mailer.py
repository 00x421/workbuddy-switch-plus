"""邮件发送：SMTP（可配）+ 未配置 SMTP 时的**开发模式**。

设计要点（改这个文件前先读完）：

1. **只用一个 TCP 连接，超时全部受控。**
   `smtplib` 的默认超时是「永远等」。放在请求路径上，上游只要不吐 banner，
   整个注册请求就悬挂 —— 而且是在 ASGI 线程池里悬挂，连着挂几次就把
   线程池占满了，表现成「服务整体变慢」。所有 socket 操作都必须带超时。

2. **不做自动降级。** 用户明确否定了「没配 SMTP 就免验证直接注册」——
   那会让「验证码」这道防线看起来存在其实不存在。SMTP 不可用就是
   `smtp_ready() == False`，由调用方抛出精确的 503 文案。

3. **开发模式的验证码只在响应里出现，并且显式标记。**
   没配 SMTP 又想在本机把注册链路跑通时，把验证码回给调用方是最省事的做法；
   但**绝不能让人误以为线上也这样**，所以响应里一定带 `dev_mode: true`，
   同时往 stderr 打一条显眼的日志。

4. **不把 SMTP 原始报错回给用户。** 那些报错里常含主机名与用户名，
   对外只说「发送失败，请联系管理员」，细节进日志。
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

# 连接 / 读写的统一超时（秒）。注册是同步路径，不能让用户干等太久。
# 20 秒是「够慢的服务器也能连上」与「用户不会以为页面卡死」之间的折中。
SMTP_TIMEOUT = 20.0

# 主题里的站点名 / 发件人名称上限，防止有人往配置里塞一整个段落。
NAME_LIMIT = 60

# 允许在自定义模板里出现的变量。服务名与验证码都是我们自己产出的，
# 但仍然只做白名单替换 —— 见 render_template 的说明。
_TEMPLATE_KEYS = ("site_name", "code", "minutes")


def default_subject(site_name: str) -> str:
    name = str(site_name or "").strip()[:NAME_LIMIT] or "WorkBuddy"
    return f"{name} 邮箱验证码"


def default_body(site_name: str, code: str, minutes: int) -> str:
    """默认正文（纯文本）。不要在这里放 HTML —— 纯文本不会被任何客户端拉黑。"""
    name = str(site_name or "").strip()[:NAME_LIMIT] or "WorkBuddy"
    return (
        f"您的 {name} 验证码是：{code}\n\n"
        f"有效期 {int(minutes)} 分钟，请勿转发给他人。\n"
        f"如果这不是您本人的操作，忽略本邮件即可。\n"
    )


class MailError(Exception):
    """发信失败。调用方据此回 502 —— 不要把底层异常原文抛给用户。"""


def smtp_config(settings) -> dict:
    """把设置里的 smtp 段整理成一份带安全默认值的配置。

    脏值一律吃掉：host 字段是人工可编辑的，`port` 写成一个字符串或者 99999
    都不该让整个注册流程 500 —— 应该表现为「这份配置没配好」。
    """
    raw = (settings or {}).get("smtp") or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        port = int(raw.get("port") or 465)
    except (TypeError, ValueError):
        port = 465
    if not 0 < port < 65536:
        port = 465
    return {
        "enabled": bool(raw.get("enabled")),
        "host": str(raw.get("host") or "").strip(),
        "port": port,
        "username": str(raw.get("username") or "").strip(),
        "from_addr": str(raw.get("from_addr") or "").strip(),
        "use_ssl": bool(raw.get("use_ssl", True)),
        "has_password": bool(raw.get("password_enc")),
    }


#: 开发用邮件出口的开关环境变量。默认关闭 —— 生产上绝不能因为
#: 「忘了配 SMTP」而变成一台把验证码印在响应里的机器。
DEV_MAIL_ENV = "WB_DEV_MAIL"


def dev_mail_enabled() -> bool:
    """开发用邮件出口是否打开（`WB_DEV_MAIL=1`）。

    ⚠️ **它绕过的是「投递」，不是「校验」**：
      · 打开它之后，验证码不再走 SMTP，而是进 stderr（并由路由层决定要不要回给响应）；
      · 但「**要不要**验证码」的判定完全不变（`require_email_code`）；
      · `locked_out_reason()` 的结论也不变（SMTP 没配 + 要验证码 → 仍然 503）。

    这三条缺一条，这个开关就从「开发便利」变成「悄悄关掉一道防线」。
    """
    return str(os.environ.get(DEV_MAIL_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def delivery_ready(settings) -> bool:
    """「验证码能不能送出去」。**唯一的判定入口**。

    业务代码（`settings_store.effective_smtp_ready` / `locked_out_reason` /
    `accounts.send_code`）一律走这个函数，而不是直接看 `smtp_ready` ——
    这样「dev 出口」这个例外只有**一处**定义，不会漂移成两套互相矛盾的判断。
    """
    return bool(smtp_ready(settings) or dev_mail_enabled())


def send_code_or_dev(settings, *, to_addr: str, code: str, minutes: int = 10) -> bool:
    """发验证码。返回 True 表示真发出去了，False 表示走的 dev 出口。

    dev 出口打开且 SMTP 没配时**不打 SMTP**，只把码打到 stderr，
    由调用方决定要不要回给响应体（本机自测用）。
    """
    if not smtp_ready(settings) and dev_mail_enabled():
        dev_mode_notice(str(to_addr), str(code))
        return False
    send_code(settings, to_addr=to_addr, code=code, minutes=minutes)
    return True

def smtp_ready(settings) -> bool:
    """SMTP 是否**真的可用**（不只是开关打开）。

    判据只有两个必需项：开关 + 主机。用户名/密码可选（很多内网中继不需要认证）。
    发件人地址缺失时退回用用户名 —— 那也是合法的 envelope sender。
    """
    cfg = smtp_config(settings)
    return bool(cfg["enabled"] and cfg["host"])


def _password(settings, password: str = "") -> str:
    """取 SMTP 口令明文。

    `password` 非空时优先用它（「测试发信」允许管理员现场试一个还没保存的口令）；
    否则从设置里解密密文。**永远不会把密文当明文用** ——
    解不开就当没有口令，走匿名发信，让 SMTP 自己报错，
    而不是拿一串 base64 去认证换一个语焉不详的 535。
    """
    if password:
        return str(password)
    from .settings_store import decrypt_secret
    return decrypt_secret(((settings or {}).get("smtp") or {}).get("password_enc")) or ""


def render_template(template: str, values: dict) -> str:
    """把 `{code}` 这类占位符替换掉。

    用 `str.replace` 而不是 `str.format`：管理员可能在正文里写花括号
    （`{示例}`），`format` 会直接抛 KeyError/ValueError ——
    那是在**登录页**上炸的，用户看到的是「验证码发不出去」，
    而真正的原因是一个和验证码毫无关系的大括号。
    """
    text = str(template or "")
    if not text:
        return ""
    for key in _TEMPLATE_KEYS:
        text = text.replace("{" + key + "}", str(values.get(key, "")))
    return text


def _build_message(settings, to_addr: str, code: str, minutes: int):
    cfg = smtp_config(settings)
    site = str((settings or {}).get("site_name") or "").strip()[:NAME_LIMIT] or "WorkBuddy"
    sender = cfg["from_addr"] or cfg["username"] or "noreply@localhost"
    custom_subject = str((settings or {}).get("email_subject") or "").strip()
    custom_body = str((settings or {}).get("email_body") or "").strip()
    values = {"site_name": site, "code": str(code), "minutes": str(int(minutes))}
    subject = render_template(custom_subject, values) or default_subject(site)
    body = render_template(custom_body, values) or default_body(site, code, minutes)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(site, "utf-8")), sender))
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    return msg, sender


def _auth(server, cfg: dict, password: str) -> None:
    """有用户名就认证；没用户名就跳过 —— 内网中继常常不需要认证。"""
    user = cfg["username"]
    if not user:
        return
    server.login(user, password or "")


def send_code(settings, *, to_addr: str, code: str, minutes: int = 10,
              password: str = "") -> None:
    """真正发一封信。失败抛 `MailError`（调用方回 502，且**不落验证码**）。"""
    cfg = smtp_config(settings)
    if not cfg["enabled"] or not cfg["host"]:
        raise MailError("未配置 SMTP")
    msg, sender = _build_message(settings, to_addr, code, minutes)
    try:
        if cfg["use_ssl"]:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT,
                                  context=context) as server:
                _auth(server, cfg, _password(settings, password))
                server.sendmail(sender, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT) as server:
                # 明文连 587 是常态：先 EHLO，能 STARTTLS 就升，升不了才继续。
                # 不这样做的话，很多只认 STARTTLS 的服务器会在 AUTH 阶段回 530。
                try:
                    server.ehlo()
                    if server.has_extn("starttls"):
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                except (smtplib.SMTPException, OSError):
                    pass
                _auth(server, cfg, _password(settings, password))
                server.sendmail(sender, [to_addr], msg.as_string())
    except Exception as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc


def send_test(settings, *, to_addr: str, password: str = "") -> dict:
    """「测试发信」按钮的后端。返回结构化结果，**不抛异常**。

    抛异常会让调用方再写一段 try/except 把它翻译成人话；这里的调用点只有一个
    （管理端），直接把结果写清楚更省事，也避免把 SMTP 的原始报错
    （里面常含主机名 / 用户名）当成 HTTP detail 回给浏览器。
    """
    cfg = smtp_config(settings)
    if not cfg["host"]:
        return {"ok": False, "error": "未配置 SMTP 主机"}
    if not str(to_addr or "").strip():
        return {"ok": False, "error": "请填写收件邮箱"}
    try:
        send_code(settings, to_addr=to_addr, code="000000", minutes=10, password=password)
    except MailError as exc:
        return {"ok": False, "error": f"发送失败：{exc}"}
    return {"ok": True, "to": to_addr}


def dev_mode_notice(email: str, code: str) -> None:
    """开发模式下发验证码时的醒目标记。**只进 stderr**，不落 state.json。"""
    try:
        print(f"[accounts] 开发模式（未配置 SMTP）验证码 {code} → {email}", file=sys.stderr)
    except Exception:
        pass
