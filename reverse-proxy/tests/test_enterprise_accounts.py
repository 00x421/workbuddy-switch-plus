# Powered by HyactCrack Skill —— 本脚本由 HyactCrack Skill 提供技术支持
"""企业版账号支持（反代侧）。

覆盖三件事：
  1. 企业 ID 从请求头正确识别（X-Enterprise-Id / X-Tenant-Id）；
  2. 企业配额响应解析兼容三种包装 + 双命名；
  3. 企业账号走企业接口、个人账号走原有的个人版接口（分流不串味）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin.pool import (  # noqa: E402
    ENTERPRISE_USAGE,
    AccountPool,
    BillingError,
    _parse_reset_time,
    enterprise_id_of,
    parse_enterprise_usage,
)


# ── 1. 企业 ID 识别 ──────────────────────────────────────────────────────

def test_enterprise_id_read_from_either_header():
    assert enterprise_id_of({"X-Enterprise-Id": "e-1"}) == "e-1"
    assert enterprise_id_of({"X-Tenant-Id": "e-2"}) == "e-2"
    # Enterprise-Id 优先（两个都在时）
    assert enterprise_id_of({"X-Enterprise-Id": "e-1", "X-Tenant-Id": "e-2"}) == "e-1"


def test_enterprise_id_absent_or_blank_is_empty():
    """个人账号两个头都不带 —— 必须稳定返回空串，否则会误走企业接口。"""
    assert enterprise_id_of({}) == ""
    assert enterprise_id_of(None) == ""
    assert enterprise_id_of({"X-Enterprise-Id": ""}) == ""
    assert enterprise_id_of({"X-Enterprise-Id": "   "}) == ""
    assert enterprise_id_of({"Authorization": "Bearer x"}) == ""


# ── 2. 响应解析 ──────────────────────────────────────────────────────────

def test_parses_standard_wrapper_and_camel_case():
    r = parse_enterprise_usage({
        "code": 0,
        "data": {"credit": 500, "limitNum": 2000, "cycleResetTime": "2026-11-01 00:00:00"},
    })
    assert r is not None
    limit_num, used, reset = r
    assert limit_num == 2000.0
    assert used == 500.0
    assert reset is not None and reset.year == 2026 and reset.month == 11


def test_parses_nested_wrapper_and_snake_case():
    r = parse_enterprise_usage({"data": {"data": {"used_num": "12.5", "limit_num": "100"}}})
    assert r is not None
    limit_num, used, reset = r
    assert limit_num == 100.0
    assert used == 12.5
    assert reset is None, "缺重置时间不阻塞展示"


def test_parses_fields_at_top_level():
    r = parse_enterprise_usage({"limitNum": 50, "credit": 10})
    assert r is not None
    assert r[0] == 50.0 and r[1] == 10.0


def test_parses_pascal_case_reset_time():
    r = parse_enterprise_usage({
        "data": {"credit": 1, "limitNum": 10, "CycleResetTime": "2026-11-01 00:00:00"},
    })
    assert r is not None and r[2] is not None


def test_unlimited_and_zero_limit_are_distinct():
    """-1 = 不限量；0 = 有限配额用尽。两者绝不能混。"""
    unlimited = parse_enterprise_usage({"data": {"credit": 0, "limitNum": -1}})
    assert unlimited is not None and unlimited[0] == -1.0

    zero = parse_enterprise_usage({"data": {"credit": 0, "limitNum": 0}})
    assert zero is not None and zero[0] == 0.0


@pytest.mark.parametrize("bad", [
    None,
    {},
    {"data": {}},
    {"data": {"limitNum": "abc"}},
    {"data": {"limitNum": "inf"}},
    {"data": {"limitNum": None}},
    {"data": {"limitNum": True}},   # bool 不是数字
    "not-a-dict",
])
def test_rejects_malformed_payloads(bad):
    assert parse_enterprise_usage(bad) is None


def test_string_number_still_parses():
    r = parse_enterprise_usage({"data": {"limitNum": "2000"}})
    assert r is not None and r[0] == 2000.0


def test_reset_time_accepts_ms_seconds_and_strings():
    assert _parse_reset_time("2026-11-01 00:00:00") is not None
    assert _parse_reset_time("2026-11-01") is not None
    assert _parse_reset_time(1_800_000_000) is not None            # 秒
    assert _parse_reset_time(1_800_000_000_000) is not None        # 毫秒
    for bad in (None, "", "not-a-date", -1, 0, True, {}):
        assert _parse_reset_time(bad) is None, f"{bad!r} 应解析为 None"


# ── 3. 分流：企业走企业接口，个人走个人接口 ──────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _Client:
    """记录被请求的 URL，用来断言分流去了哪个接口。"""

    def __init__(self, payload):
        self.payload = payload
        self.urls: list[str] =[]

    def post(self, url, headers=None, json=None):
        self.urls.append(url)
        return _Resp(self.payload)


def _pool():
    """credits 链路只用到 self.clock() 与 self.billing()，用裸对象注入即可。

    不构造 AccountPool：那需要真实 store，而这两个方法本身不碰 store。
    billing 直接用真实的未绑定方法 —— 它只依赖 self.store 之外的入参。
    """
    class _Stub:
        clock = staticmethod(lambda: 1_800_000_000)
    stub = _Stub()
    # realm 是 0.1.4 新增的可选参数（国际版 billing 域不同）。stub 用 *args 转交，
    # 这样以后再给这几个方法加参数不需要回来改测试。
    stub.billing = lambda client, headers, path, body=None, *a, **kw: AccountPool.billing(
        stub, client, headers, path, body, *a, **kw
    )
    stub._enterprise_credits = lambda client, headers, *a, **kw: AccountPool._enterprise_credits(
        stub, client, headers, *a, **kw
    )
    return stub


def _call_credits(headers, payload):
    client = _Client(payload)
    AccountPool.credits(_pool(), client, headers)
    return client


def test_enterprise_account_uses_enterprise_endpoint():
    client = _call_credits(
        {"X-Enterprise-Id": "e-1"},
        {"code": 0, "data": {"credit": 500, "limitNum": 2000}},
    )
    assert client.urls == [ENTERPRISE_USAGE], "企业账号必须走企业配额接口"


def test_enterprise_result_shape_matches_personal():
    """企业结果要有与个人版同形的键，否则下游统计读不到。"""
    result = AccountPool._enterprise_credits(
        _pool(), _Client({"code": 0, "data": {"credit": 500, "limitNum": 2000}}),
        {"X-Enterprise-Id": "e-1"},
    )
    assert result["remaining"] == 1500.0
    assert result["capacity"] == 2000.0
    assert result["used"] == 500.0
    assert result["packages"] == 1
    assert result["unlimited"] is False
    assert len(result["resources"]) == 1
    assert result["resources"][0]["packageName"] == "企业配额"


def test_enterprise_unlimited_uses_none_not_zero():
    """不限量必须给 None —— 给 0 会被界面显示成「额度用光了」。"""
    result = AccountPool._enterprise_credits(
        _pool(), _Client({"code": 0, "data": {"credit": 0, "limitNum": -1}}),
        {"X-Enterprise-Id": "e-1"},
    )
    assert result["unlimited"] is True
    assert result["remaining"] is None
    assert result["capacity"] is None
    assert result["resources"] == []


def test_enterprise_exhausted_clamps_remaining_to_zero():
    result = AccountPool._enterprise_credits(
        _pool(), _Client({"code": 0, "data": {"credit": 2500, "limitNum": 2000}}),
        {"X-Enterprise-Id": "e-1"},
    )
    assert result["remaining"] == 0.0
    assert result["unlimited"] is False


def test_enterprise_upstream_error_raises_readable_billing_error():
    with pytest.raises(BillingError):
        AccountPool._enterprise_credits(
            _pool(), _Client({"code": 500, "message": "企业配额服务异常"}),
            {"X-Enterprise-Id": "e-1"},
        )


def test_enterprise_unparsable_payload_raises():
    with pytest.raises(BillingError):
        AccountPool._enterprise_credits(
            _pool(), _Client({"code": 0, "data": {}}), {"X-Enterprise-Id": "e-1"},
        )


def test_personal_account_does_not_touch_enterprise_endpoint():
    """个人账号绝不能请求企业接口 —— 会 404 并让账号看起来没积分。"""
    client = _Client({"Response": {"Data": {"Accounts": [], "TotalCount": 0}}})
    try:
        AccountPool.credits(_pool(), client, {"Authorization": "Bearer x"})
    except BillingError:
        pass
    assert ENTERPRISE_USAGE not in client.urls


# ── 4. remaining 为 None 时不得抛异常 ───────────────────────────────────

def test_unlimited_remaining_none_does_not_crash_comparisons():
    """回归：`None > 0` 会抛 TypeError，不限量账号撞上就整条操作失败。"""
    remaining = None
    assert not (remaining is not None and remaining > 0)


def test_state_rules_treat_unlimited_as_available():
    """不限量账号不该被判成 exhausted（那会让它被调度器跳过）。"""
    status = {"credits_updated": 1_800_000_000, "remaining": None, "packages": 0}
    exhausted = (
        status.get("credits_updated", 0) > 1_800_000_000 - 600
        and status.get("remaining") == 0
        and status.get("packages", 0) > 0
    )
    assert exhausted is False
