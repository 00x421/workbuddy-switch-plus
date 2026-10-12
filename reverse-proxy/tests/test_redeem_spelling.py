"""兑换码归一化 / 前缀歧义的回归测试。

背景：`WB` 既是发行时我们自己加的前缀，又可能出现在正文里（ALPHABET 含 W/B）。
正文以 `WB` 开头的码约占 1/1234；用户一旦省略 `WB-` 或只去掉连字符，
输入就无法从字符串本身判断是哪种读法 —— 早先的实现只认一种，
那张码会**永远兑不出来**，且报「无效或已被使用」，用户无从自查。

测试原则：一律用**真实**的 create_batch 发行、真实的 redeem 核销；
不手工伪造库内容，也不构造「先发行再清空」这种绕来绕去的状态。
"""
import threading
import unittest

from admin import redemption as rd


class _Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}

    def save(self):
        pass


def _spellings(code):
    """一张码的常见手输写法，互不相同。"""
    body = code[len("WB-"):]
    return [
        ("原样", code),
        ("小写", code.lower()),
        ("去全部横线", code.replace("-", "")),
        ("省略 WB- 前缀", body),
        ("省略前缀并去横线", body.replace("-", "")),
        ("前后空白", "  %s  " % code),
    ]


def _issue_odd(store, want):
    """发行并收集「正文以 WB 开头」的码（歧义唯一会咬人的情形）。"""
    out = []
    for _ in range(300):
        if len(out) >= want:
            break
        for code in rd.create_batch(store, count=100, credit=1, note="")["codes"]:
            if code[len("WB-"):].replace("-", "").startswith("WB"):
                out.append(code)
    return out[:want]


class NormalizeTest(unittest.TestCase):
    def test_canonical_form_strips_leading_wb(self):
        self.assertEqual(rd.normalize_code("WB-ABCDE-FGHIJ"), "ABCDEFGHIJ")
        self.assertEqual(rd.normalize_code("WBABCDEFGHIJ"), "ABCDEFGHIJ")
        self.assertEqual(rd.normalize_code("  wb-abcde-fghij  "), "ABCDEFGHIJ")

    def test_candidates_cover_both_readings(self):
        cands = rd.candidate_keys("WBWB72WENY2A")
        self.assertIn("WB72WENY2A", cands, "把前导 WB 当前缀的读数")
        self.assertIn("WBWB72WENY2A", cands, "把前导 WB 当正文的读数")
        self.assertEqual(len(cands), len(set(cands)), "候选不能重复")

    def test_empty_input_yields_no_candidates(self):
        for raw in ("", "---", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(rd.candidate_keys(raw), [])


class RedeemSpellingTest(unittest.TestCase):
    def test_ambiguous_batch_redeems_with_prefix_omitted(self):
        """正文以 WB 开头的码，用「省略 WB- 前缀」的写法也必须能兑。

        这是原实现真正会挂掉的那一类：输入与正文都以 WB 开头。
        """
        store = _Store()
        odd = _issue_odd(store, 1)
        self.assertTrue(odd, "没能造出正文以 WB 开头的码，测试前提不成立")
        for code in odd[:1]:
            raw = code[len("WB-"):]
            with self.subTest(code=code):
                res = rd.redeem(store, raw, "u")
                self.assertEqual(res["milli"], 1000)

    def test_ambiguous_batch_redeems_with_prefix_and_dashes_omitted(self):
        store = _Store()
        odd = _issue_odd(store, 1)
        self.assertTrue(odd)
        for code in odd[:1]:
            raw = code[len("WB-"):].replace("-", "")
            with self.subTest(code=code):
                self.assertEqual(rd.redeem(store, raw, "u")["milli"], 1000)

    def test_bulk_rotating_spellings_all_redeem(self):
        """整批 500 张 × 轮换 6 种写法，全量核销，一张都不能漏。"""
        store = _Store()
        codes = rd.create_batch(store, count=500, credit=1, note="")["codes"]
        self.assertEqual(len(set(codes)), 500, "生成的码本身不能重复")
        failed = []
        for i, code in enumerate(codes):
            name, raw = _spellings(code)[i % 6]
            try:
                rd.redeem(store, raw, "u")
            except Exception as exc:  # noqa: BLE001
                failed.append((code, name, raw, str(exc)))
        self.assertEqual(failed, [], "有码核销失败：%s" % failed[:3])
        self.assertEqual(rd.balance_milli(store, "u"), 500 * 1000)

    def test_a_code_still_cannot_be_redeemed_twice(self):
        """候选机制不能变成「重复兑换」的后门。"""
        store = _Store()
        code = rd.create_batch(store, count=1, credit=5, note="")["codes"][0]
        rd.redeem(store, code, "u")
        for name, raw in _spellings(code)[1:]:
            with self.subTest(form=name):
                with self.assertRaises(Exception):
                    rd.redeem(store, raw, "u")
        self.assertEqual(rd.balance_milli(store, "u"), 5000)


class CandidateSafetyTest(unittest.TestCase):
    def test_candidates_across_a_batch_never_collide(self):
        """不同码的候选不能撞车，否则会出现「兑了别人的码」。"""
        store = _Store()
        codes = rd.create_batch(store, count=500, credit=1, note="")["codes"]
        seen = {}
        for code in codes:
            for cand in rd.candidate_keys(code):
                self.assertNotIn(cand, seen,
                                 "候选 %s 同时属于 %s 与 %s"
                                 % (cand, seen.get(cand), code))
                seen[cand] = code


if __name__ == "__main__":
    unittest.main()
