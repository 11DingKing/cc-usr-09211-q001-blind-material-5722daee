import unittest

from redaction import MASK, SpanError, grapheme_boundaries, normalize_spans, redact


class NormalizeTests(unittest.TestCase):
    def test_overlapping_spans_merge(self):
        self.assertEqual(normalize_spans([[2, 8], [5, 12], [20, 22]], 30),
                         [(2, 12), (20, 22)])

    def test_touching_spans_merge(self):
        self.assertEqual(normalize_spans([[0, 3], [3, 5]], 10), [(0, 5)])

    def test_unsorted_input_is_sorted(self):
        self.assertEqual(normalize_spans([[9, 10], [1, 2]], 10), [(1, 2), (9, 10)])

    def test_empty_span_is_noop(self):
        self.assertEqual(normalize_spans([[4, 4]], 10), [])

    def test_invalid_spans_rejected(self):
        for bad in ([[-1, 2]], [[3, 2]], [[0, 99]], [["a", 2]], [[True, 2]],
                    [[0]], ["12"], "12"):
            with self.assertRaises(SpanError, msg=repr(bad)):
                normalize_spans(bad, 10)


class RedactTests(unittest.TestCase):
    def test_half_open_interval(self):
        self.assertEqual(redact("abcdef", [[0, 3]]), MASK + "def")

    def test_multiple_overlapping_spans_one_pass(self):
        self.assertEqual(redact("0123456789ABC", [[2, 8], [5, 10], [11, 12]]),
                         "01" + MASK + "A" + MASK + "C")

    def test_chinese_characters(self):
        self.assertEqual(redact("举报人张某反映情况", [[3, 5]]),
                         "举报人" + MASK + "反映情况")

    def test_zwj_family_emoji_not_split(self):
        # 👨‍👩‍👧 占 5 个码点（👨 ZWJ 👩 ZWJ 👧），只遮其中一个码点也要整簇遮盖
        text = "家属👨‍👩‍👧到场"
        start = text.index("👨")
        out = redact(text, [[start, start + 1]])
        self.assertEqual(out, "家属" + MASK + "到场")
        self.assertNotIn("‍", out)

    def test_flag_emoji_not_split(self):
        text = "旗帜🇨🇳飘扬"
        start = text.index("🇨")
        out = redact(text, [[start, start + 1]])
        self.assertEqual(out, "旗帜" + MASK + "飘扬")

    def test_emoji_skin_tone_not_split(self):
        text = "手势👍🏽好"
        start = text.index("👍")
        out = redact(text, [[start, start + 1]])
        self.assertEqual(out, "手势" + MASK + "好")

    def test_combining_mark_follows_base(self):
        text = "café"  # e + 组合重音符
        out = redact(text, [[3, 4]])
        self.assertEqual(out, "caf" + MASK)

    def test_crlf_not_split(self):
        out = redact("a\r\nb", [[1, 2]])
        self.assertEqual(out, "a" + MASK + "b")

    def test_variation_selector_follows_emoji(self):
        text = "附件🗂️三份"  # 🗂️ = U+1F5C2 U+FE0F
        start = text.index("🗂")
        out = redact(text, [[start, start + 1]])
        self.assertEqual(out, "附件" + MASK + "三份")

    def test_no_spans_returns_original(self):
        self.assertEqual(redact("原文不动", []), "原文不动")

    def test_boundaries_cover_ends(self):
        self.assertIn(0, grapheme_boundaries("abc"))
        self.assertIn(3, grapheme_boundaries("abc"))


if __name__ == "__main__":
    unittest.main()
