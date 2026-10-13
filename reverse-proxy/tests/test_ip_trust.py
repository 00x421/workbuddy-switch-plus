"""HyactCrack 技术支持 — 客户端 IP 不得由请求头自证

## 缺陷

`admin/iputil.py` 原先 `WB_TRUST_PROXY` 默认 `"1"`（`PORTING.md:173` 写的是 `0`），
且默认可信网段含整个私网。于是直连本机端口的请求（peer `127.0.0.1`）
被判为「来自可信代理」，它自带的 `X-Forwarded-For` / `X-Real-IP` 被无条件采信。

`client_ip()` 是这些控制的**唯一输入**：登录失败按 IP 锁定、全局 IP 黑/白名单、
密钥 IP 白名单与 max_ips、发码限流、审计溯源。换一个头值就换一个身份。

## 为什么收窄 CIDR 修不了

本机直连的 peer 就是 `127.0.0.1`，而回环**必须**留在可信网段里
（否则挂了本机反代的部署会失效）。所以无论怎么收窄，`127.0.0.1`
永远命中，伪造头永远被采信。唯一可靠的办法是让「是否信任」由显式
配置决定，且**默认关**。

## 这里测什么

只测 `client_ip()` 这个纯函数（不起服务），因为修的就是它。
另加一个配置项一致性测试：`iputil.trust_proxy()` 与
`serve.proxy_headers_enabled()` 必须同源 —— 只关一个没用，
uvicorn 会在应用之前就把 peer 改掉。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin import iputil                                     # noqa: E402
from starlette.requests import Request                       # noqa: E402


def make_request(headers: dict, peer: str = "127.0.0.1") -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/x",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234), "server": ("127.0.0.1", 8787),
        "scheme": "http", "query_string": b"", "root_path": "",
    })


class TrustProxyDefaultTest(unittest.TestCase):
    """默认必须**不信任**转发头。"""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("WB_TRUST_PROXY", "WB_TRUSTED_PROXY_CIDRS")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_is_off(self):
        """⭐ 默认不信任 —— 与 PORTING.md:173 的配置表一致。"""
        self.assertFalse(iputil.trust_proxy(),
                         "WB_TRUST_PROXY 默认应为关；开着等于任何直连者都能自称任意 IP")

    def test_forwarded_for_ignored_by_default(self):
        """⭐ 默认配置下，伪造的 X-Forwarded-For 不得改变 client_ip。"""
        req = make_request({"x-forwarded-for": "203.0.113.77"})
        self.assertEqual(iputil.client_ip(req), "127.0.0.1",
                         "伪造头被采信了 —— 按 IP 的限流/白名单全部可绕")

    def test_x_real_ip_ignored_by_default(self):
        """`X-Real-IP` 同样不得被采信（它和 XFF 是两条独立入口）。"""
        req = make_request({"x-real-ip": "198.51.100.9"})
        self.assertEqual(iputil.client_ip(req), "127.0.0.1")

    def test_rotation_does_not_change_identity(self):
        """⭐ 换头值不得换身份 —— 这是「打散按 IP 锁定」的直接成因。"""
        seen = {iputil.client_ip(make_request({"x-forwarded-for": f"203.0.113.{i}"}))
                for i in range(1, 12)}
        self.assertEqual(seen, {"127.0.0.1"},
                         f"11 个不同的伪造头产生了 {len(seen)} 个身份，锁定会被打散")

    def test_default_cidrs_exclude_private_ranges(self):
        """默认可信网段不含大面积私网 —— 挂了反代应显式写反代地址。"""
        cidrs = iputil.trusted_proxy_cidrs()
        for bad in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"):
            self.assertNotIn(bad, cidrs,
                             f"默认信任 {bad} 等于信任同内网任意主机写的转发头")


class TrustProxyEnabledTest(unittest.TestCase):
    """显式开启后要恢复采信 —— 不能把真实反代部署一起关死。"""

    def setUp(self):
        self._saved = os.environ.get("WB_TRUST_PROXY")
        os.environ["WB_TRUST_PROXY"] = "1"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("WB_TRUST_PROXY", None)
        else:
            os.environ["WB_TRUST_PROXY"] = self._saved

    def test_enabled_honours_forwarded_for(self):
        """⭐ 挂反代时（peer 落在可信段）应采信 XFF，否则真实客户端 IP 全错。"""
        req = make_request({"x-forwarded-for": "203.0.113.77"})
        self.assertEqual(iputil.client_ip(req), "203.0.113.77")

    def test_untrusted_peer_still_rejected(self):
        """peer 不在可信段时，即使开了开关也不采信 —— 这道闸门必须还在。"""
        req = make_request({"x-forwarded-for": "203.0.113.77"}, peer="8.8.8.8")
        self.assertEqual(iputil.client_ip(req), "8.8.8.8",
                         "公网对端不该被当成可信代理")


class CleanIpSyntaxTest(unittest.TestCase):
    """非法 IP 必须以空串表示「没有」，不得原样返回（INJ-04）。"""

    def test_valid_ipv4(self):
        self.assertEqual(iputil._clean_ip("203.0.113.7"), "203.0.113.7")

    def test_ipv4_with_port(self):
        self.assertEqual(iputil._clean_ip("203.0.113.7:1234"), "203.0.113.7")

    def test_ipv6_bracketed(self):
        self.assertEqual(iputil._clean_ip("[::1]:1234"), "::1")

    def test_garbage_rejected(self):
        """⭐ 原先非法值原样返回，会被存进 IP 访问日志。"""
        for bad in ("<img src=x onerror=alert(1)>", "not-an-ip", "1.2.3", "999.1.1.1"):
            self.assertEqual(iputil._clean_ip(bad), "",
                             f"{bad!r} 应被拒，而不是当成客户端 IP 存下来")

    def test_empty_stays_empty(self):
        self.assertEqual(iputil._clean_ip(""), "")
        self.assertEqual(iputil._clean_ip("   "), "")


class SwitchConsistencyTest(unittest.TestCase):
    """两个开关必须同源，否则只关一个等于没关。

    `iputil.trust_proxy()` 管应用层「信不信转发头」，
    `serve.proxy_headers_enabled()` 管 uvicorn 要不要在应用之前改写 peer。
    uvicorn 那层若还开着，peer 已经被换成伪造值，
    应用层的判断就永远看到伪造值 —— 实测确认过。
    """

    def test_serve_uses_same_switch(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import serve                                            # noqa: E402
        saved = os.environ.pop("WB_TRUST_PROXY", None)
        try:
            self.assertEqual(serve.proxy_headers_enabled(),
                             iputil.trust_proxy())
            os.environ["WB_TRUST_PROXY"] = "1"
            self.assertEqual(serve.proxy_headers_enabled(), True)
            os.environ["WB_TRUST_PROXY"] = "0"
            self.assertEqual(serve.proxy_headers_enabled(), False)
        finally:
            os.environ.pop("WB_TRUST_PROXY", None)
            if saved is not None:
                os.environ["WB_TRUST_PROXY"] = saved


class MetadataTargetTest(unittest.TestCase):
    """上游地址不得指向云元数据服务。

    `wb2api_base` 允许指任意主机是**功能**（用户自己的上游可能在内网），
    但触发这个出网请求的 `/admin/api/overview` 只要 viewer ——
    一枚只读令牌就能拿它探内网。元数据地址是这类探测里最值钱的目标，
    且指向它没有任何合法用法，所以单独挡掉。
    """

    def test_cloud_metadata_blocked(self):
        from fastapi import HTTPException
        from admin.server import block_metadata_target
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "http://169.254.170.2/creds",
                    "http://[fd00:ec2::254]/x",
                    "http://metadata.google.internal/computeMetadata/v1/",
                    "http://metadata.goog/x",
                    "http://169.254.1.1/x"):
            with self.subTest(url=url):
                with self.assertRaises(HTTPException, msg=f"{url} 应被拦截"):
                    block_metadata_target(url)

    def test_legitimate_upstreams_allowed(self):
        """⭐ 别把功能一起堵死 —— 本机与内网上游是最常见的用法。"""
        from admin.server import block_metadata_target
        for url in ("http://127.0.0.1:7863",
                    "http://10.0.0.5:8080",
                    "https://api.example.com/v1",
                    "http://192.168.1.20:9000/base"):
            with self.subTest(url=url):
                block_metadata_target(url)


if __name__ == "__main__":
    unittest.main()
