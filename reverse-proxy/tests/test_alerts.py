# Powered by HyactCrack Skill —— 本脚本由 HyactCrack Skill 提供技术支持
"""管理员告警通知（advice/alerts）。

覆盖：
  1. 三类告警的识别（封禁 / 冻结 / 积分告急）；
  2. 去重靠状态跃迁，不是每轮重发；
  3. 不限量企业账号（remaining=None）**绝不**被误报成积分告急；
  4. 静默时段（含跨零点）；
  5. 配置归一化：非法值报错、口令加密落盘、回显只给掩码；
  6. 邮件组装与阈值边界。

这个模块只发信给管理员自己，不是用户系统 —— 没有注册/找回密码/验证码。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin import alerts  # noqa: E402

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=alerts.CN)
NOW_TS = int(NOW.timestamp())


def cfg(**over):
    base = alerts.defaults()
    base.update(over)
    return base


def row(**over):
    base = {"id": "a", "name": "账号A", "enabled": True,
            "pool_state": "available", "remaining": 1000, "packages": 1}
    base.update(over)
    return base


# ── 1. 三类告警识别 ─────────────────────────────────────────────────────

def test_detects_banned_account():
    out = alerts.detect([row(pool_state="invalid", last_error="凭据已失效")], cfg(), {}, NOW)
    kinds = [a["kind"] for a in out]
    assert "banned" in kinds
    banned = next(a for a in out if a["kind"] == "banned")
    assert "账号A" in banned["lines"][0]
    assert "凭据已失效" in banned["lines"][0]


def test_detects_frozen_account_with_cooldown():
    out = alerts.detect(
        [row(pool_state="cooling", cooldown_until=NOW_TS + 1800)], cfg(), {}, NOW,
    )
    frozen = next(a for a in out if a["kind"] == "frozen")
    assert "30 分钟" in frozen["lines"][0]


def test_detects_credits_below_threshold():
    out = alerts.detect([row(remaining=50)], cfg(credits_threshold=100), {}, NOW)
    credits = next(a for a in out if a["kind"] == "credits")
    assert "剩余 50" in credits["lines"][0]


def test_no_alerts_when_everything_healthy():
    out = alerts.detect([row(remaining=5000)], cfg(credits_threshold=100), {}, NOW)
    assert out == []


def test_disabled_accounts_are_skipped():
    """暂停的账号本来就不参与调度，报它的告警只会制造噪声。"""
    out = alerts.detect(
        [row(enabled=False, pool_state="invalid", remaining=0)], cfg(), {}, NOW,
    )
    assert out == []


# ── 2. 企业版 / 不限量账号的边界 ────────────────────────────────────────

def test_unlimited_enterprise_account_never_reported_as_low_credits():
    """回归：不限量账号 remaining 是 None。当成 0 会误报「积分告急」。"""
    out = alerts.detect(
        [row(name="企业号", remaining=None, packages=0)], cfg(credits_threshold=100), {}, NOW,
    )
    assert out == [], "不限量账号不该出现在任何告警里"


def test_zero_remaining_with_packages_is_reported():
    """真的用光是 0 + 有积分包 —— 这个必须报。"""
    out = alerts.detect([row(remaining=0, packages=2)], cfg(credits_threshold=100), {}, NOW)
    assert any(a["kind"] == "credits" for a in out)


def test_zero_remaining_without_packages_is_not_reported():
    """没查到积分包说明还没查过，不是「用光了」——不能报。"""
    out = alerts.detect([row(remaining=0, packages=0)], cfg(credits_threshold=100), {}, NOW)
    assert not any(a["kind"] == "credits" for a in out)


def test_boolean_remaining_is_not_treated_as_number():
    """bool 是 int 的子类，不显式排除会把 True 当成 1 报「告急」。"""
    out = alerts.detect([row(remaining=True, packages=1)], cfg(credits_threshold=100), {}, NOW)
    assert not any(a["kind"] == "credits" for a in out)


@pytest.mark.parametrize("value,expected", [
    (100, True),    # 正好等于阈值：算告急（`<=`）
    (101, False),
    (0, True),
    (-5, True),     # 负数（不该出现，但也不能漏报）
])
def test_threshold_boundary(value, expected):
    out = alerts.detect([row(remaining=value)], cfg(credits_threshold=100), {}, NOW)
    assert any(a["kind"] == "credits" for a in out) is expected


# ── 3. 去重：靠状态跃迁 ─────────────────────────────────────────────────

def test_same_alert_is_not_repeated_on_next_scan():
    rows = [row(remaining=10)]
    first = alerts.detect(rows, cfg(), {}, NOW)
    assert first
    fired = {a["kind"]: a["fingerprint"] for a in first}
    assert alerts.detect(rows, cfg(), fired, NOW) == [], "同一状态不该重发"


def test_changed_account_set_triggers_new_alert():
    """多了一个告急账号 —— 指纹变了，应该重报（新信息值得知道）。"""
    first = alerts.detect([row(id="a", name="A", remaining=10)], cfg(), {}, NOW)
    fired = {a["kind"]: a["fingerprint"] for a in first}
    second = alerts.detect(
        [row(id="a", name="A", remaining=10), row(id="b", name="B", remaining=20)],
        cfg(), fired, NOW,
    )
    assert any(a["kind"] == "credits" for a in second)


def test_fingerprint_is_order_independent():
    """账号顺序变了不该算「新告警」，否则每次重排都发一封信。"""
    a = row(id="a", name="A", remaining=10)
    b = row(id="b", name="B", remaining=20)
    first = alerts.detect([a, b], cfg(), {}, NOW)
    fired = {x["kind"]: x["fingerprint"] for x in first}
    assert alerts.detect([b, a], cfg(), fired, NOW) == []


def test_all_three_kinds_coexist():
    out = alerts.detect([
        row(id="1", name="封", pool_state="invalid"),
        row(id="2", name="冻", pool_state="cooling", cooldown_until=NOW_TS + 600),
        row(id="3", name="缺", remaining=5),
    ], cfg(), {}, NOW)
    assert {a["kind"] for a in out} == {"banned", "frozen", "credits"}


def test_individual_kinds_can_be_disabled():
    c = cfg(notify_on_banned=False, notify_on_frozen=False, notify_on_credits=True)
    out = alerts.detect([
        row(id="1", pool_state="invalid"),
        row(id="2", pool_state="cooling", cooldown_until=NOW_TS + 600),
        row(id="3", remaining=5),
    ], c, {}, NOW)
    assert {a["kind"] for a in out} == {"credits"}


# ── 4. 静默时段 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("start,end,hour,expected", [
    (23, 7, 2, True),      # 跨零点，凌晨在静默内
    (23, 7, 12, False),
    (23, 7, 23, True),     # 起点含
    (23, 7, 7, False),     # 终点不含
    (9, 17, 9, True),
    (9, 17, 16, True),
    (9, 17, 17, False),
    (9, 17, 8, False),
])
def test_quiet_hours(start, end, hour, expected):
    c = cfg(quiet_from=start, quiet_to=end)
    assert alerts.in_quiet_hours(c, datetime(2026, 6, 1, hour, 0, tzinfo=alerts.CN)) is expected


def test_quiet_hours_unset_never_quiet():
    assert alerts.in_quiet_hours(cfg(), NOW) is False
    assert alerts.in_quiet_hours(cfg(quiet_from=9), NOW) is False
    assert alerts.in_quiet_hours(cfg(quiet_from=9, quiet_to=9), NOW) is False


# ── 5. 配置归一化 ───────────────────────────────────────────────────────

def test_normalize_rejects_bad_values():
    from fastapi import HTTPException
    for bad in ({"port": "abc"}, {"port": 0}, {"port": 99999},
                {"enabled": "yes"}, {"tls": "unknown"},
                {"credits_threshold": "x"}, {"credits_threshold": -1},
                {"to": "not-an-email"}, {"quiet_from": 99}):
        with pytest.raises(HTTPException):
            alerts.normalize(bad)


def test_normalize_accepts_valid_values():
    out = alerts.normalize({
        "enabled": True, "host": "smtp.example.com", "port": 587,
        "tls": "starttls", "username": "u@example.com",
        "to": "a@x.com, b@y.com", "credits_threshold": 200,
        "quiet_from": 23, "quiet_to": 7,
    })
    assert out["enabled"] is True
    assert out["port"] == 587
    assert out["tls"] == "starttls"
    assert out["to"] == "a@x.com,b@y.com"
    assert out["quiet_from"] == 23


def test_normalize_encrypts_password_never_plaintext():
    """口令必须以加密串落盘 —— 明文 SMTP 授权码泄露等于邮箱被接管。"""
    out = alerts.normalize({"password": "s3cret-app-password"})
    assert "password" in out
    assert out["password"] != "s3cret-app-password"
    assert out["password"].startswith("enc:v1:")


def test_normalize_mask_means_keep_existing():
    """回显掩码再提交，不能把已存的口令抹成字面量 '********'。"""
    out = alerts.normalize({"password": alerts.MASKED})
    assert "password" not in out


def test_normalize_clears_quiet_hours_with_empty():
    out = alerts.normalize({"quiet_from": "", "quiet_to": None})
    assert out["quiet_from"] is None and out["quiet_to"] is None


def test_normalize_rejects_too_many_recipients():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        alerts.normalize({"to": ",".join(f"u{i}@x.com" for i in range(20))})


def test_public_view_masks_password():
    view = alerts.public_view({"password": "enc:v1:abc", "host": "smtp.x.com"})
    assert view["password"] == alerts.MASKED
    assert view["password_set"] is True
    assert "enc:v1:abc" not in str(view)

    empty = alerts.public_view({"password": ""})
    assert empty["password"] == ""
    assert empty["password_set"] is False


def test_defaults_returns_fresh_object():
    a, b = alerts.defaults(), alerts.defaults()
    a["enabled"] = True
    assert b["enabled"] is False, "默认值被共享会污染全局状态"


# ── 6. 邮件组装 ─────────────────────────────────────────────────────────

def test_build_message_contains_every_alert():
    alerts_list = alerts.detect([
        row(id="1", name="封号", pool_state="invalid"),
        row(id="2", name="缺额", remaining=5),
    ], cfg(), {}, NOW)
    message = alerts.build_message(cfg(to="admin@x.com", username="bot@x.com", sender="bot@x.com"),
                                   alerts_list, NOW)
    body = message.get_content()
    assert "账号封禁告急" in message["Subject"]
    assert "积分用量告急" in message["Subject"]
    assert "封号" in body
    assert "缺额" in body
    assert message["To"] == "admin@x.com"


def test_build_message_truncates_huge_account_lists():
    """一次全池封禁不该撑爆邮件（也避免邮件网关按体积拦截）。"""
    rows = [row(id=str(i), name=f"账号{i}", pool_state="invalid") for i in range(50)]
    alerts_list = alerts.detect(rows, cfg(), {}, NOW)
    body = alerts.build_message(cfg(to="a@x.com"), alerts_list, NOW).get_content()
    assert "另有" in body
    assert body.count("账号49") == 0, "超出上限的账号应被折叠"


def test_build_message_handles_empty_lines_gracefully():
    message = alerts.build_message(cfg(to="a@x.com"), [
        {"kind": "test", "title": "测试邮件", "lines": []},
    ], NOW)
    assert "测试邮件" in message["Subject"]


# ── 7. 发信绝不静默失败 ─────────────────────────────────────────────────

def test_send_without_host_raises():
    with pytest.raises(ValueError):
        alerts.send(cfg(host=""), alerts.build_message(cfg(to="a@x.com"), [], NOW))


def test_send_async_reports_error_via_callback():
    """发信失败要留痕 —— 否则「告警没收到」永远查不出原因。"""
    errors = []
    message = alerts.build_message(cfg(to="a@x.com"), [{"kind": "x", "title": "t", "lines": []}], NOW)
    import unittest.mock as mock
    with mock.patch.object(alerts, "build_message", return_value=message), \
         mock.patch.object(alerts, "send", side_effect=RuntimeError("SMTP 拒绝")):
        thread = alerts.send_alerts_async(cfg(host="smtp.x.com", to="a@x.com"),
                                          [{"kind": "x", "title": "t", "lines": []}],
                                          on_error=errors.append)
        assert thread is not None
        thread.join(timeout=5)
    assert errors and "SMTP 拒绝" in errors[0]


def test_send_async_no_thread_when_nothing_to_send():
    assert alerts.send_alerts_async(cfg(), [], None) is None


def test_alert_kinds_table_is_complete():
    assert set(alerts.ALERT_KINDS) == {
        "banned", "frozen", "credits",
        "upstream_disabled", "account_failing", "quota", "pool_empty", "pool_thin",
        "stale", "refresh_failed", "key_rejected",
    }
    # 每个类型都要有中文标题 —— 邮件里出现英文 key 说明漏配了。
    for kind, title in alerts.ALERT_KINDS.items():
        assert title and title != kind, kind


def test_every_kind_has_a_flag_and_a_default():
    """新增类型最容易漏的两处：默认值、开关。漏了就会「配了但不生效」。"""
    missing = [k for k in alerts.ALERT_KINDS if f"notify_on_{k}" not in alerts.FLAG_KEYS]
    assert not missing, f"这些类型没有对应开关：{missing}"
    cfg = alerts.defaults()
    for kind in alerts.ALERT_KINDS:
        assert f"notify_on_{kind}" in cfg, f"{kind} 没有默认值"
        assert type(cfg[f"notify_on_{kind}"]) is bool, kind


def test_normalize_accepts_every_flag():
    """开关必须都能被 normalize 收下，否则界面改不动。"""
    for flag in alerts.FLAG_KEYS:
        assert alerts.normalize({flag: False}) == {flag: False}
        assert alerts.normalize({flag: True}) == {flag: True}
        with pytest.raises(HTTPException) as err:
            alerts.normalize({flag: "yes"})
        assert err.value.status_code == 400


# ── 收件地址归一化 ───────────────────────────────────────────────────────
#
# 这一片是补票的。原先只有「有 @、两侧非空、无空白」三条判据，用
# `partition("@")` 实现 —— 而 partition 只看第一个 @，于是 `a@b@c`
# 得到 local="a" / domain="b@c" 双双非空，「通过」。
# 那不是地址，发信必然失败，而且是投递到一半才失败的形态。
#
# 同时空项被静默丢弃：用户以为配了三个抄送、实际只有两个生效，
# 且没有任何提示。

def test_single_address_round_trips():
    assert alerts._clean_addresses("a@b.com") == "a@b.com"


def test_multiple_addresses_are_normalized_without_spaces():
    assert alerts._clean_addresses("a@b.com, c@d.com") == "a@b.com,c@d.com"


def test_semicolons_are_accepted_as_separators():
    """有些邮件客户端复制出来是分号分隔的，容下这个不算放纵。"""
    assert alerts._clean_addresses("a@b.com; c@d.com") == "a@b.com,c@d.com"


def test_unusual_but_legal_addresses_survive():
    """带点、加号、多级域都合法 —— 严格正则只会把合法地址挡在外面。"""
    for addr in ["user.name+tag@sub.example.co.uk", "a@b.co", "x_y-z@m.example.com"]:
        assert alerts._clean_addresses(addr) == addr


def test_empty_and_none_mean_no_recipients():
    assert alerts._clean_addresses("") == ""
    assert alerts._clean_addresses(None) == ""
    assert alerts._clean_addresses("   ") == ""


def test_multiple_at_signs_are_rejected():
    """回归：`a@b@c` 曾经通过校验。"""
    for bad in ["a@b@c", "a@@b.com", "@a@b.com"]:
        with pytest.raises(HTTPException) as err:
            alerts._clean_addresses(bad)
        assert err.value.status_code == 400


def test_missing_at_or_empty_side_is_rejected():
    for bad in ["not-an-email", "@b.com", "a@", "@"]:
        with pytest.raises(HTTPException) as err:
            alerts._clean_addresses(bad)
        assert err.value.status_code == 400


def test_whitespace_inside_an_address_is_rejected():
    for bad in ["a b@c.com", "a@b c.com", "a\tb@c.com", "a@b.com\nc@d.com"]:
        with pytest.raises(HTTPException) as err:
            alerts._clean_addresses(bad)
        assert err.value.status_code == 400


def test_empty_item_in_a_list_is_rejected_not_silently_dropped():
    """回归：`a@b.com, ,` 曾经被静默接受，丢掉一项而不告诉任何人。"""
    for bad in ["a@b.com, ,", "a@b.com,,c@d.com", ",a@b.com", "a@b.com,"]:
        with pytest.raises(HTTPException) as err:
            alerts._clean_addresses(bad)
        assert err.value.status_code == 400


def test_more_than_ten_addresses_are_rejected():
    ok = ",".join(f"u{i}@e.com" for i in range(10))
    assert alerts._clean_addresses(ok).count("@") == 10
    with pytest.raises(HTTPException) as err:
        alerts._clean_addresses(",".join(f"u{i}@e.com" for i in range(11)))
    assert err.value.status_code == 400


def test_a_list_input_is_accepted():
    assert alerts._clean_addresses(["a@b.com", "c@d.com"]) == "a@b.com,c@d.com"


# ── 2. 新增告警类型 ─────────────────────────────────────────────────────
#
# 每一条都配了「不该报」的反例。告警系统的失败模式不是漏报，
# 而是**误报**：一旦开始响得没道理，真的那封封禁通知也会被一起忽略。

def kinds_of(out):
    return {a["kind"] for a in out}


# ── 上游停用 ──

def test_upstream_disabled_is_reported():
    out = alerts.detect(
        [row(up_disabled=True, up_consecutive_fails=3, up_reason="quota exceeded")],
        cfg(), {}, NOW)
    assert "upstream_disabled" in kinds_of(out)
    line = next(a for a in out if a["kind"] == "upstream_disabled")["lines"][0]
    assert "账号A" in line
    assert "连续失败 3 次" in line
    assert "quota exceeded" in line


def test_manual_disable_is_labelled_not_treated_as_incident():
    """手动停用是运维自己的动作，必须标出来 —— 否则会以为自己误操作了。"""
    out = alerts.detect([row(up_disabled=True, up_manual_disabled=True)], cfg(), {}, NOW)
    line = next(a for a in out if a["kind"] == "upstream_disabled")["lines"][0]
    assert "手动停用" in line


def test_upstream_disabled_does_not_fire_when_absent_or_false():
    for value in (None, False):
        out = alerts.detect([row(up_disabled=value)], cfg(), {}, NOW)
        assert "upstream_disabled" not in kinds_of(out), value


# ── 积分快照过期 ──

def test_stale_snapshot_is_reported_with_minutes():
    old = NOW_TS - 3 * 3600
    out = alerts.detect([row(credits_updated=old)], cfg(stale_minutes=120), {}, NOW)
    line = next(a for a in out if a["kind"] == "stale")["lines"][0]
    assert "180 分钟未更新" in line


def test_fresh_snapshot_is_not_reported():
    out = alerts.detect([row(credits_updated=NOW_TS - 60)], cfg(stale_minutes=120), {}, NOW)
    assert "stale" not in kinds_of(out)


def test_stale_threshold_is_the_configured_one_not_a_hardcoded_600s():
    """界面上配的分钟数必须真的生效。

    这里刻意用一个「小于 600 秒」的阈值：如果实现里还留着硬编码的
    600 秒（rows() 的 credits_stale 就是），这一条会漏报。
    """
    out = alerts.detect([row(credits_updated=NOW_TS - 300)],
                        cfg(stale_minutes=3), {}, NOW)
    assert "stale" in kinds_of(out)
    # 反过来：阈值设得比实际年龄大，就不该报
    out = alerts.detect([row(credits_updated=NOW_TS - 300)],
                        cfg(stale_minutes=60), {}, NOW)
    assert "stale" not in kinds_of(out)


def test_account_never_synced_is_not_reported_as_stale():
    """从来没同步过（0/None）说明是刚加的号，不是「快照过期」。"""
    for value in (0, None):
        out = alerts.detect([row(credits_updated=value)], cfg(stale_minutes=1), {}, NOW)
        assert "stale" not in kinds_of(out), value


# ── 凭据刷新失败 ──

def test_refresh_failure_is_reported_and_kept_distinct_from_ban():
    out = alerts.detect([row(last_error="连接上游超时")], cfg(), {}, NOW)
    assert "refresh_failed" in kinds_of(out)
    assert "banned" not in kinds_of(out)
    assert "连接上游超时" in next(
        a for a in out if a["kind"] == "refresh_failed")["lines"][0]


def test_banned_is_not_double_reported_as_refresh_failure():
    """封禁自带 last_error，两者判据必须互斥，否则一封事故发两封信。"""
    out = alerts.detect(
        [row(pool_state="invalid", last_error="凭据已失效")], cfg(), {}, NOW)
    assert "banned" in kinds_of(out)
    assert "refresh_failed" not in kinds_of(out)


def test_no_error_means_no_refresh_alert():
    out = alerts.detect([row(last_error=None)], cfg(), {}, NOW)
    assert "refresh_failed" not in kinds_of(out)


# ── 密钥额度 ──

def key(**over):
    base = {"id": "k1", "name": "测试密钥", "enabled": True,
            "quota": 1000, "used_tokens": 100}
    base.update(over)
    return base


def test_key_quota_threshold_fires():
    out = alerts.detect([row()], cfg(quota_percent=90), {}, NOW,
                        keys=[key(quota=1000, used_tokens=950)])
    line = next(a for a in out if a["kind"] == "quota")["lines"][0]
    assert "测试密钥" in line
    assert "95%" in line


def test_key_below_threshold_does_not_fire():
    out = alerts.detect([row()], cfg(quota_percent=90), {}, NOW,
                        keys=[key(quota=1000, used_tokens=899)])
    assert "quota" not in kinds_of(out)


def test_unlimited_key_is_never_reported():
    """quota 为 0 表示不限量 —— 绝不能算成「已用 ∞%」。

    这是本模块最要紧的一条不变式：企业版不限量账号的 remaining 是 None，
    密钥的 quota 是 0，两者都必须被当成「没有上限」，而不是「上限为零」。
    """
    for quota in (0, None):
        out = alerts.detect([row()], cfg(quota_percent=1), {}, NOW,
                            keys=[key(quota=quota, used_tokens=10 ** 9)])
        assert "quota" not in kinds_of(out), quota


def test_disabled_key_is_skipped():
    out = alerts.detect([row()], cfg(quota_percent=90), {}, NOW,
                        keys=[key(enabled=False, used_tokens=1000)])
    assert "quota" not in kinds_of(out)


def test_keys_omitted_does_not_crash_or_fire():
    """缺数据就少报一类，不是抛异常。老调用点只传账号也要能跑。"""
    out = alerts.detect([row()], cfg(quota_percent=90), {}, NOW)
    assert "quota" not in kinds_of(out)


# ── 池级 ──

def test_pool_empty_fires_when_no_account_is_available():
    rows = [row(id="a", pool_state="cooling"), row(id="b", pool_state="invalid")]
    out = alerts.detect(rows, cfg(), {}, NOW)
    line = next(a for a in out if a["kind"] == "pool_empty")["lines"][0]
    assert "一个可用的都没有" in line


def test_pool_empty_does_not_fire_when_one_is_available():
    rows = [row(id="a", pool_state="cooling"), row(id="b", pool_state="available")]
    out = alerts.detect(rows, cfg(), {}, NOW)
    assert "pool_empty" not in kinds_of(out)


def test_pool_empty_does_not_fire_on_an_empty_pool():
    """一个账号都没配，不该报「池见底」—— 那是没配置，不是故障。"""
    out = alerts.detect([], cfg(), {}, NOW)
    assert "pool_empty" not in kinds_of(out)


def test_pool_thin_is_off_by_default():
    rows = [row(id="a", pool_state="available")]
    out = alerts.detect(rows, cfg(), {}, NOW)
    assert "pool_thin" not in kinds_of(out)


def test_pool_thin_fires_when_enabled():
    rows = [row(id="a", pool_state="available")]
    out = alerts.detect(rows, cfg(notify_on_pool_thin=True, pool_min_available=1), {}, NOW)
    assert "pool_thin" in kinds_of(out)


def test_pool_thin_only_fires_when_the_pool_is_not_empty():
    """池子空了报「见底」就够了，不必再报一条「偏少」。"""
    rows = [row(id="a", pool_state="invalid")]
    out = alerts.detect(rows, cfg(notify_on_pool_thin=True, pool_min_available=2), {}, NOW)
    assert "pool_empty" in kinds_of(out)
    assert "pool_thin" not in kinds_of(out)


# ── 密钥连续被拒 ──

def log(status, kid="k1"):
    return {"status": status, "key_id": kid, "ok": status < 400}


def test_key_rejected_fires_at_threshold():
    logs = [log(401) for _ in range(5)]
    out = alerts.detect([row()], cfg(key_reject_threshold=5), {}, NOW,
                        keys=[key()], logs=logs)
    line = next(a for a in out if a["kind"] == "key_rejected")["lines"][0]
    assert "测试密钥" in line
    assert "5 次" in line


def test_key_rejected_below_threshold_does_not_fire():
    logs = [log(401) for _ in range(4)]
    out = alerts.detect([row()], cfg(key_reject_threshold=5), {}, NOW, logs=logs)
    assert "key_rejected" not in kinds_of(out)


def test_successful_requests_do_not_count():
    logs = [log(200) for _ in range(50)]
    out = alerts.detect([row()], cfg(key_reject_threshold=5), {}, NOW, logs=logs)
    assert "key_rejected" not in kinds_of(out)


def test_only_401_and_403_count_as_rejection():
    """500 是上游挂了，不是密钥被拒 —— 混进来会让「密钥问题」变成噪声。"""
    logs = [log(500) for _ in range(50)] + [log(429) for _ in range(50)]
    out = alerts.detect([row()], cfg(key_reject_threshold=5), {}, NOW, logs=logs)
    assert "key_rejected" not in kinds_of(out)


def test_rejections_are_tallied_per_key_not_globally():
    """两个密钥各被拒 3 次，不该凑成 6 次报出来。"""
    logs = [log(401, "k1") for _ in range(3)] + [log(401, "k2") for _ in range(3)]
    out = alerts.detect([row()], cfg(key_reject_threshold=5), {}, NOW,
                        keys=[key(id="k1"), key(id="k2", name="密钥二")], logs=logs)
    assert "key_rejected" not in kinds_of(out)


def test_request_without_key_is_not_counted():
    """直连 / 鉴权前就被拒的请求没有 key_id —— 那些不是密钥的问题。"""
    logs = [{"status": 401, "key_id": None} for _ in range(50)]
    out = alerts.detect([row()], cfg(key_reject_threshold=5), {}, NOW, logs=logs)
    assert "key_rejected" not in kinds_of(out)


def test_unknown_key_falls_back_to_id():
    logs = [log(401, "gone-key") for _ in range(5)]
    out = alerts.detect([row()], cfg(key_reject_threshold=5), {}, NOW, keys=[], logs=logs)
    line = next(a for a in out if a["kind"] == "key_rejected")["lines"][0]
    assert "gone-key" in line


# ── 新增类型的去重与静默 ──

def test_new_kinds_participate_in_dedup():
    """报过一次、指纹没变，就不该再报 —— 新类型也必须守这条规矩。"""
    rows = [row(pool_state="cooling")]
    first = alerts.detect(rows, cfg(), {}, NOW)
    fired = {a["key"]: a["fingerprint"] for a in first}
    second = alerts.detect(rows, cfg(), fired, NOW)
    assert second == []


def test_new_kinds_respect_their_own_switches():
    rows = [row(up_disabled=True, last_error="x", credits_updated=NOW_TS - 10 ** 6)]
    off = cfg(notify_on_upstream_disabled=False, notify_on_refresh_failed=False,
              notify_on_stale=False)
    assert not (kinds_of(alerts.detect(rows, off, {}, NOW))
                & {"upstream_disabled", "refresh_failed", "stale"})


# ── 3. 邮件正文 ──

def body_of(alerts_list, **over):
    msg = alerts.build_message(cfg(**over), alerts_list, NOW)
    return msg.get_content()


def test_email_lists_new_kinds_with_correct_units():
    """密钥类告警不能写成「N 个账号」—— 那会把人指向错误的排查方向。"""
    detected = alerts.detect(
        [row(up_disabled=True)],
        cfg(), {}, NOW,
        keys=[key(quota=100, used_tokens=99)],
        logs=[log(401) for _ in range(6)])
    text = body_of(detected)
    assert "上游停用告急" in text
    assert "密钥额度告急（1 个密钥）" in text
    assert "密钥连续被拒（1 个密钥）" in text
    assert "账号封禁" not in text


def test_email_says_accounts_for_account_kinds():
    detected = alerts.detect([row(pool_state="invalid")], cfg(), {}, NOW)
    assert "账号封禁告急（1 个账号）" in body_of(detected)


def test_email_lists_count_only_kinds_without_detail_cap():
    """池见底只有一条结论，不该被 MAX_ROWS 截断。"""
    detected = alerts.detect([row(pool_state="invalid")], cfg(), {}, NOW)
    text = body_of(detected)
    assert "一个可用的都没有" in text


def test_email_still_caps_long_account_lists():
    rows = [row(id=f"a{i}", name=f"账号{i}", pool_state="invalid") for i in range(30)]
    detected = alerts.detect(rows, cfg(), {}, NOW)
    text = body_of(detected)
    assert "…另有 10 个账号" in text
