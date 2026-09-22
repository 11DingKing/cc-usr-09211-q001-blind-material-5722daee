import unittest

from redaction import (
    MASK,
    SpanProblem,
    apply_policy,
    extend_to_cluster,
    merge_intervals,
    normalize_spans,
    redact_text,
)


class RedactTextTests(unittest.TestCase):
    def test_overlapping_intervals_merge_into_single_mask(self):
        text = "夜班组长李建军在4月12日涂改台账"
        spans = [
            (text.index("李建军"), text.index("李建军") + 3),
            (text.index("组长"), text.index("12日") + 3),
        ]
        self.assertEqual(redact_text(text, spans), "夜班" + MASK + "涂改台账")

    def test_adjacent_intervals_merge(self):
        self.assertEqual(redact_text("甲乙丙丁", [(0, 1), (1, 2)]), MASK + "丙丁")

    def test_disjoint_intervals_stay_separate(self):
        self.assertEqual(redact_text("甲乙丙丁", [(0, 1), (2, 3)]), MASK + "乙" + MASK + "丁")

    def test_chinese_counted_by_codepoint(self):
        self.assertEqual(redact_text("李建军王芳", [(0, 3)]), MASK + "王芳")

    def test_zwj_emoji_not_split(self):
        text = "甲👨‍👩‍👧乙"
        start = text.index("👨")
        self.assertEqual(redact_text(text, [(start, start + 1)]), "甲" + MASK + "乙")

    def test_emoji_modifier_not_split(self):
        text = "好👍🏽！"
        start = text.index("👍")
        self.assertEqual(redact_text(text, [(start, start + 1)]), "好" + MASK + "！")

    def test_regional_indicator_pair_not_split(self):
        text = "旗🇺🇳毕"
        start = text.index("🇺")
        self.assertEqual(redact_text(text, [(start, start + 1)]), "旗" + MASK + "毕")

    def test_combining_mark_not_split(self):
        text = "Cafe\u0301厅"  # e + 组合符，分解形式
        start = text.index("e")
        self.assertEqual(redact_text(text, [(start, start + 1)]), "Caf" + MASK + "厅")

    def test_crlf_not_split(self):
        self.assertEqual(redact_text("甲\r\n乙", [(1, 2)]), "甲" + MASK + "乙")

    def test_extend_to_cluster_boundaries(self):
        text = "甲👨‍👩‍👧乙"
        self.assertEqual(extend_to_cluster(text, 1, 2), (1, 6))

    def test_merge_intervals(self):
        self.assertEqual(merge_intervals([(5, 9), (1, 3), (2, 6)]), [(1, 9)])


REVISION = {
    "revision": 1,
    "text": "李建军涂改台账",
    "attachments": [
        {"attachment_id": "att-1", "filename": "李建军电话清单.png", "description": "王芳提供的截图"},
    ],
}


class PolicyTests(unittest.TestCase):
    def test_text_filename_description_all_redacted(self):
        spans = normalize_spans([
            {"field": "text", "start": 0, "end": 3},
            {"field": "attachment_filename", "attachment_id": "att-1", "start": 0, "end": 3},
            {"field": "attachment_description", "attachment_id": "att-1", "start": 0, "end": 2},
        ], REVISION)
        content = apply_policy(REVISION, spans)
        self.assertEqual(content["text"], MASK + "涂改台账")
        attachment = content["attachments"][0]
        self.assertEqual(attachment["filename"], MASK + "电话清单.png")
        self.assertEqual(attachment["description"], MASK + "提供的截图")

    def test_out_of_range_rejected(self):
        with self.assertRaises(SpanProblem):
            normalize_spans([{"field": "text", "start": 0, "end": 99}], REVISION)

    def test_empty_span_rejected(self):
        with self.assertRaises(SpanProblem):
            normalize_spans([{"field": "text", "start": 2, "end": 2}], REVISION)

    def test_unknown_attachment_rejected(self):
        with self.assertRaises(SpanProblem):
            normalize_spans(
                [{"field": "attachment_filename", "attachment_id": "nope", "start": 0, "end": 1}],
                REVISION,
            )

    def test_unknown_field_rejected(self):
        with self.assertRaises(SpanProblem):
            normalize_spans([{"field": "contact", "start": 0, "end": 1}], REVISION)

    def test_unknown_key_rejected(self):
        with self.assertRaises(SpanProblem):
            normalize_spans([{"field": "text", "start": 0, "end": 1, "note": "x"}], REVISION)

    def test_error_message_carries_no_content(self):
        try:
            normalize_spans([{"field": "text", "start": 0, "end": 99}], REVISION)
        except SpanProblem as exc:
            self.assertNotIn("李建军", str(exc))
        else:
            self.fail("应当抛出 SpanProblem")


if __name__ == "__main__":
    unittest.main()
