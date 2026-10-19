"""限时免费模型判定（`is_free_model` / `get_model_catalog_with_free`）。

反向护栏是这个文件的主体：**误判成免费比漏判危险得多**。
把收费模型标成免费，用户会拿它跑大活，账单出来才发现。
所以没有 tags 就是收费，「看不太懂」也是收费。
"""
import pytest

from core import converter


@pytest.mark.parametrize("tags,expected", [
    (["限时免费"], True),
    (["免费"], True),
    (["free"], True),
    (["Free"], True),
    (["vision", "限时免费"], True),
    (["text-to-image"], False),
    (["vision"], False),
    ([], False),
    (None, False),
])
def test_is_free_model(tags, expected):
    assert converter.is_free_model({"tags": tags}) is expected


def test_missing_tags_key_is_not_free():
    # 上游绝大多数模型根本没有 tags 字段 —— 那些全是收费的
    assert converter.is_free_model({"id": "glm-5.2"}) is False


def test_credits_text_is_not_a_judge():
    """`credits` 是计费说明文本，不能拿来判免费。

    真机上出现过 `credits == "免费"` 但 tags 为空的历史情况；把文案当判据会在
    上游措辞变一次（"免费" → "0 积分"）之后整体失效，而且是**静默失效**。
    """
    assert converter.is_free_model({"credits": "免费"}) is False
    assert converter.is_free_model({"credits": "x0.00 credits"}) is False


def test_catalog_with_free_annotates_every_entry(monkeypatch):
    monkeypatch.setattr(converter, "_load_model_entries_from_workbuddy", lambda: [
        {"id": "a", "vendor": "f", "tags": ["限时免费"]},
        {"id": "b", "vendor": "f"},
    ])
    rows = converter.get_model_catalog_with_free()
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[0]["free"] is True
    assert rows[1]["free"] is False
    # 原字段不能被抹掉
    assert rows[0]["vendor"] == "f"


def test_catalog_with_free_on_fallback(monkeypatch):
    monkeypatch.setattr(converter, "_load_model_entries_from_workbuddy", lambda: [])
    rows = converter.get_model_catalog_with_free()
    # 兜底条目的 tags 是 ["fallback"]，不是免费
    assert all(r["free"] is False for r in rows)
    assert all(r["tags"] == ["fallback"] for r in rows)


def test_tags_are_coerced_to_strings():
    out = converter._catalog_entry({"id": "x", "tags": [1, "限时免费", None, ""]})
    assert out["tags"] == ["1", "限时免费"]   # None 与空串被丢掉，不会被 str() 成 "None"
