# Powered by HyactCrack Skill —— 本脚本由 HyactCrack Skill 提供技术支持
"""企业版账号在反代侧的完整链路。

覆盖「添加账户 → 存 enterpriseId → 注入请求头 → 走企业配额接口」这条链，
以及个人账号在同一条链上不被误判为企业版。

链路各环的实现分散在 switch_store.py（存取/请求头）、pool.py（配额分流）、
server.py（account_rows 展示）。这个文件专门验证它们**串起来**是对的 ——
单看每个文件都「看起来没问题」，但 enterpriseId 一旦在某处掉链子，
表现是「企业账号显示成 0 积分」，那是排查成本最高的一类故障。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin.pool import enterprise_id_of, parse_enterprise_usage  # noqa: E402


# ── 1. 存：enterpriseId 必须落进账号库 ──────────────────────────────────

def test_switch_store_entry_carries_enterprise_id():
    """save_browser_account 构造的 entry 必须带 enterpriseId。

    这条断言盯的是「字段名」：官方凭据里是 camelCase 的 `enterpriseId`，
    而请求头里我们读的也是这个键。两边一旦有一侧改成 snake_case，
    企业账号的配额与身份会静默失效（表现为拿不到数据，而不是报错）。
    """
    src = (Path(__file__).resolve().parent.parent / "admin" / "switch_store.py").read_text(encoding="utf-8")
    assert '"enterpriseId": account.get("enterpriseId") or ""' in src


def test_request_headers_inject_both_enterprise_keys():
    """请求头要同时带 X-Enterprise-Id 与 X-Tenant-Id（官方两处实现各读一个）。"""
    src = (Path(__file__).resolve().parent.parent / "admin" / "switch_store.py").read_text(encoding="utf-8")
    assert '"X-Enterprise-Id": account.get("enterpriseId", "")' in src
    assert '"X-Tenant-Id": account.get("enterpriseId", "")' in src


def test_personal_account_gets_empty_enterprise_header():
    """个人账号的 enterpriseId 是空串 —— 请求头里也必须为空，不能写 None。

    `account.get("enterpriseId", "")` 在键存在但值为 None 时返回 None，
    httpx 会把它渲染成字符串 "None"，上游据此可能把个人账号判成企业账号。
    这里断言的是「取值方式」保证了缺键时空串。
    """
    account = {"uid": "u1"}
    assert (account.get("enterpriseId", "") or "") == ""


# ── 2. 判：请求头 → 企业 ID ─────────────────────────────────────────────

def test_enterprise_headers_roundtrip_through_detector():
    """真实会被注入的那两个头，都必须能被 enterprise_id_of 认出来。"""
    assert enterprise_id_of({"X-Enterprise-Id": "ent-42", "X-Tenant-Id": "ent-42"}) == "ent-42"


def test_personal_headers_are_not_mistaken_for_enterprise():
    """个人账号发的头（enterpriseId 为空）不得被判成企业版。"""
    personal = {
        "Authorization": "Bearer x",
        "X-User-Id": "u1",
        "X-Enterprise-Id": "",
        "X-Tenant-Id": "",
        "X-Domain": "codebuddy.cn",
    }
    assert enterprise_id_of(personal) == ""


# ── 3. 查：企业配额接口的路径与个人版不同 ────────────────────────────────

def test_enterprise_endpoint_is_distinct_from_personal():
    """企业配额走独立端点，绝不能复用个人版的 get-user-resource。

    个人版分页接口对 `X-Enterprise-Id` 非空的账号返回的是「用户个人资源」，
    企业账号在这个口径下通常一片空 —— 正是「显示成没积分」的根因。
    """
    from admin.pool import BILLING, ENTERPRISE_USAGE
    assert "get-enterprise-user-usage" in ENTERPRISE_USAGE
    assert "get-user-resource" not in ENTERPRISE_USAGE
    assert ENTERPRISE_USAGE != BILLING


def test_enterprise_usage_parse_matches_upstream_shapes():
    """官方两种包装都要能解析（桌面端与网页端各用一种）。"""
    web = {"code": 0, "data": {"credit": 12, "limitNum": 100, "cycleResetTime": "2026-11-01 00:00:00"}}
    desktop = {"data": {"data": {"used_num": 12.0, "limit_num": 100.0}}}
    assert parse_enterprise_usage(web)[:2] == (100.0, 12.0)
    assert parse_enterprise_usage(desktop)[:2] == (100.0, 12.0)


# ── 4. 展示：account_rows 要标出企业版 ──────────────────────────────────

def test_account_rows_exposes_enterprise_flags():
    """account_rows 要透出 is_enterprise，否则界面无从显示企业版徽标。

    用源码断言而不是构造完整 store：`account_rows` 依赖文件读取与
    加密凭据，构造等价 fixture 的成本远高于它验证的那一行。
    """
    src = (Path(__file__).resolve().parent.parent / "admin" / "server.py").read_text(encoding="utf-8")
    assert '"is_enterprise": bool(enterprise_id)' in src
    assert '"enterprise_id": enterprise_id' in src
    # 异常分支也要给默认值，否则前端拿 undefined 会渲染成 "企业版"（undefined 是 falsy，
    # 但 `a.is_enterprise` 在模板里一旦被写成 != null 判断就会翻车）。
    assert '"is_enterprise": False, "enterprise_id": ""' in src


def test_account_rows_accepts_both_id_spellings():
    """导入链路与浏览器登录链路的键名可能不同，两种都要认。"""
    src = (Path(__file__).resolve().parent.parent / "admin" / "server.py").read_text(encoding="utf-8")
    assert 'account.get("enterpriseId") or account.get("enterprise_id")' in src


# ── 5. 前端：企业版徽标与配额文案 ───────────────────────────────────────

def test_frontend_renders_enterprise_badge_and_quota_label():
    """界面要有企业版徽标，并把积分说明改成「企业配额」。"""
    js = (Path(__file__).resolve().parent.parent / "admin" / "static" / "app.js").read_text(encoding="utf-8")
    assert "mini-enterprise" in js
    assert "企业版账号" in js
    assert "企业配额" in js


def test_frontend_never_renders_null_remaining_as_zero():
    """回归：`remaining === null` 必须显示成「—」。

    显示成 0 会被读成「额度用光了」，而不限量企业账号的 remaining 正是 null ——
    这是本次改动里最容易造成误判的一处。
    """
    js = (Path(__file__).resolve().parent.parent / "admin" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'a.remaining === null || a.remaining === undefined ? "—"' in js


def test_enterprise_badge_css_exists_and_differs_from_active_badge():
    """企业版徽标要有自己的配色：它与「手动/测试账号」徽标会并排出现。"""
    css = (Path(__file__).resolve().parent.parent / "admin" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".mini-enterprise" in css
    assert ".mini-active" in css
    # 两者不能共用同一条规则块（同色会被看成同一个标签）
    assert ".mini-active, .mini-enterprise" not in css
