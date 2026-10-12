"""IP 解析与 CIDR 匹配工具（移植自 workbuddy-manager/server/iputil.py）。

配置来源改为环境变量，默认值与 manager 一致。
"""
from __future__ import annotations

import ipaddress
import os

from fastapi import Request

DEFAULT_TRUSTED_CIDRS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"


def trust_proxy() -> bool:
    return os.environ.get("WB_TRUST_PROXY", "1") == "1"


def trusted_proxy_cidrs() -> list[str]:
    raw = os.environ.get("WB_TRUSTED_PROXY_CIDRS", DEFAULT_TRUSTED_CIDRS)
    return [c.strip() for c in raw.split(",") if c.strip()]


def trusted_proxy_hops() -> int:
    try:
        return max(1, int(os.environ.get("WB_TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        return 1


def _clean_ip(value: str) -> str:
    """去掉端口与方括号，兼容 IPv6 形如 [::1]:1234。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("["):  # [::1]:1234
        end = raw.find("]")
        return raw[1:end] if end > 0 else raw
    # 纯 IPv6 不处理；IPv4:port 去掉端口
    if raw.count(":") == 1:
        host, _, port = raw.partition(":")
        if port.isdigit():
            return host
    return raw


def _is_trusted_proxy(peer: str) -> bool:
    """TCP 对端是否落在可信代理网段内。

    仅当对端确实是我们配置的反代（同机回环或指定私网）时，才采信它写入的
    转发头 —— 否则「服务直接暴露」时伪造的 X-Real-IP 会被采信，从而绕过
    全局 IP 白/黑名单、密钥 IP 白名单与 max_ips、以及登录失败按 IP 锁定。
    """
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for cidr in trusted_proxy_cidrs():
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """解析真实客户端 IP（取值优先级按可信度）。

      1) 仅当 TCP 对端来自可信代理网段时，才采信 `X-Real-IP`
      2) 同上条件下，从 `X-Forwarded-For` 右往左数第 N 个（N = 可信跳数）
      3) TCP 对端地址（可信度最高，永远可回退）
    """
    peer = request.client.host if request.client else ""

    if not trust_proxy():
        return peer or "0.0.0.0"
    if not _is_trusted_proxy(peer):
        return peer or "0.0.0.0"

    real = _clean_ip(request.headers.get("x-real-ip", ""))
    if real:
        return real

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        parts = [p for p in (_clean_ip(p) for p in xff.split(",")) if p]
        if parts:
            idx = len(parts) - trusted_proxy_hops()
            return parts[idx] if idx >= 0 else parts[0]

    return peer or "0.0.0.0"


def peer_ip(scope: dict) -> str:
    """从 ASGI scope 取 TCP 对端地址（中间件里拿不到 Request 时用）。"""
    client = scope.get("client")
    if not client:
        return "0.0.0.0"
    return client[0] or "0.0.0.0"


def client_ip_from_scope(scope: dict) -> str:
    """`client_ip` 的 ASGI scope 版本：同样只在 TCP 对端可信时才采信转发头。"""
    peer = peer_ip(scope)
    if not trust_proxy():
        return peer
    if not _is_trusted_proxy(peer):
        return peer
    headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
    real = _clean_ip(headers.get("x-real-ip", ""))
    if real:
        return real
    xff = headers.get("x-forwarded-for", "")
    if xff:
        parts = [p for p in (_clean_ip(p) for p in xff.split(",")) if p]
        if parts:
            idx = len(parts) - trusted_proxy_hops()
            return parts[idx] if idx >= 0 else parts[0]
    return peer


def ip_matches(ip: str, cidr: str) -> bool:
    """支持单 IP 与 CIDR；非法输入一律不匹配。

    `cidr` 两侧空白会被去掉：写入时已归一化，但存量库里可能已有带空白的条目，
    而 `ip_network(' 10.0.0.0/8')` 会解析失败 → 这条规则永远匹配不上。
    """
    try:
        addr = ipaddress.ip_address(ip)
        try:
            net = ipaddress.ip_network(str(cidr or "").strip(), strict=False)
        except ValueError:
            return False
        return addr in net
    except ValueError:
        return False


def valid_cidr(value: str) -> bool:
    try:
        ipaddress.ip_network(str(value or "").strip(), strict=False)
        return True
    except ValueError:
        return False


def evaluate(ip: str, rules: list[dict], mode: str) -> bool:
    """返回 True 表示放行。mode: whitelist（默认拒绝）| blacklist（默认放行）。"""
    if not rules:
        return mode != "whitelist"
    allow = [r for r in rules if r.get("kind") == "allow"]
    deny = [r for r in rules if r.get("kind") == "deny"]
    if any(ip_matches(ip, r.get("cidr", "")) for r in deny):
        return False
    if mode == "whitelist":
        return any(ip_matches(ip, r.get("cidr", "")) for r in allow)
    return True
