"""AUDIT — admin/wb2api.py. Reproductions for audit findings only."""
import unittest

from admin import wb2api

FUTURE = "2099-01-01T00:00:00Z"


class ParseTimeInputTypesTest(unittest.TestCase):
    """BUG-A1: _parse_time() only understands str; numeric instants return None.

    Callers cannot tell "upstream omitted the field" from "upstream gave us a
    number we could not parse" — both collapse to None.
    """

    def test_unix_seconds_number_is_not_parsed(self):
        self.assertIsNone(wb2api._parse_time(1791747520))

    def test_bool_is_rejected(self):
        self.assertIsNone(wb2api._parse_time(True))


class ViewZeroAbsoluteTest(unittest.TestCase):
    """BUG-A2: view() treats until == 0 as a real absolute instant (epoch 1970).

    absolute_of() only checks isinstance(value, (int, float)), so 0 becomes 0.0
    and is treated as a parseable deadline. Consequences:
      * left_seconds(0, fallback) computes a negative number -> None, so the
        upstream cool_remaining_sec snapshot is thrown away;
      * cooling becomes falsy when upstream only wrote until=0, so the whole row
        silently degrades to "not cooling" and the early-return branch drops
        cool_kind entirely.
    """

    def test_zero_until_falls_back_to_snapshot_remaining(self):
        info = {"cooling": True, "until": 0, "cool_remaining_sec": 300,
                "rate_limited_models": []}
        shown = wb2api.view(info, now=1_700_000_000.0)
        self.assertEqual(shown.get("cool_remaining_sec"), 300)

    def test_zero_until_alone_is_not_cooling(self):
        """until=0 是「没有截止时刻」，不是「1970 年到期」。

        原实现把它当成合法时刻，left_seconds 算出一个大负数 → None，
        于是连 cool_remaining_sec 快照都被丢掉。现在 0 按未设置处理，
        没有别的冷却信号时就是「没在冷却」。

        复审结论：audit 原先还断言这里必须有 cool_kind 字段，
        那条判定为过度指定 —— cooling=False 时本来就没有「冷却类型」
        可报，缺这个键不是缺陷。所以只锁「不该被判成冷却」这一条。
        """
        shown = wb2api.view({"until": 0, "reason": "429 rate limit"},
                            now=1_700_000_000.0)
        self.assertFalse(shown["cooling"])
        self.assertIsNone(shown["cool_remaining_sec"])
        # 但原因要透出来：这是「上游说了什么」，与冷不冷却无关。
        self.assertEqual(shown["reason"], "429 rate limit")


class ViewModelDeadlineTest(unittest.TestCase):
    """BUG-A3: model-level `until` selection uses a raw truthiness `or`.

    absolute = item.get("until") or item.get("reset_at")

    A Go zero-value timestamp ("0001-01-01T00:00:00Z") is a non-empty string,
    so it is truthy and wins over a perfectly good reset_at. The zero value is
    then normalised to None by _parse_time, and remaining_sec is reported as
    None even though upstream told us exactly when the model recovers.
    """

    def test_model_go_zero_until_falls_through_to_reset_at(self):
        info = {"cooling": True, "rate_limited_models": [{
            "model": "a",
            "until": "0001-01-01T00:00:00Z",
            "reset_at": FUTURE,
            "reason": "6004",
        }]}
        item = wb2api.view(info, now=1_700_000_000.0)["rate_limited_models"][0]
        self.assertIsNotNone(item["remaining_sec"])

    def test_model_deadlines_are_normalised_to_epoch_seconds(self):
        info = {"cooling": True, "rate_limited_models": [{
            "model": "a", "until": FUTURE, "reset_at": FUTURE, "reason": ""}]}
        item = wb2api.view(info, now=1_700_000_000.0)["rate_limited_models"][0]
        self.assertIsInstance(item["until"], float)


if __name__ == "__main__":
    unittest.main()
