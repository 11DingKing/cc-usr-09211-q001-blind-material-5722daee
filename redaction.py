"""去标识遮盖引擎。

坐标一律采用 Unicode 码点，区间左闭右开。区间边界落在字符簇
（组合字符、表情序列、旗帜等）内部时自动外扩到完整字符簇，
保证中文与表情都不会被切出半个字符。一次遮盖可覆盖多个
重叠或相邻的区间，合并后统一替换为固定占位符，不外泄原文长度。
"""

import unicodedata

MASK = "〔已遮盖〕"

FIELDS = ("text", "attachment_filename", "attachment_description")
_ATTACHMENT_FIELDS = ("attachment_filename", "attachment_description")
_SPAN_KEYS = {"field", "start", "end", "attachment_id", "label"}

MAX_SPANS = 2000
_MAX_LABEL = 128


class SpanProblem(ValueError):
    """遮盖区间不合法。消息只含区间序号与字段名，不含原文内容。"""


def _is_extend_char(ch):
    """不可与前一字符拆开的扩展字符：组合符、变体选择符、表情
    肤色修饰符、ZWJ、keycap、tag 序列字符。"""
    code = ord(ch)
    if unicodedata.category(ch) in ("Mn", "Mc", "Me"):
        return True
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return True
    if 0x1F3FB <= code <= 0x1F3FF:
        return True
    if code in (0x200D, 0x20E3):
        return True
    if 0xE0020 <= code <= 0xE007F:
        return True
    return False


def _is_regional_indicator(ch):
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_cluster_break(text, index):
    """text[index-1] 与 text[index] 之间是否允许切开。"""
    left = text[index - 1]
    right = text[index]
    if left == "\r" and right == "\n":
        return False
    if _is_extend_char(right) or left == "\u200d":
        return False
    if _is_regional_indicator(left) and _is_regional_indicator(right):
        run = 0
        pos = index - 1
        while pos >= 0 and _is_regional_indicator(text[pos]):
            run += 1
            pos -= 1
        if run % 2 == 1:
            return False
    return True


def extend_to_cluster(text, start, end):
    """把 [start, end) 外扩到字符簇边界，避免切出半个字符。"""
    while start > 0 and not _is_cluster_break(text, start):
        start -= 1
    while end < len(text) and not _is_cluster_break(text, end):
        end += 1
    return start, end


def merge_intervals(intervals):
    """合并重叠或相邻的区间。"""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def redact_text(text, intervals):
    """按码点区间遮盖 text，返回遮盖后的字符串。"""
    merged = merge_intervals(extend_to_cluster(text, s, e) for s, e in intervals)
    if not merged:
        return text
    parts = []
    pos = 0
    for start, end in merged:
        parts.append(text[pos:start])
        parts.append(MASK)
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _is_index(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _target_text(revision, span, index):
    field = span["field"]
    if field == "text":
        if "attachment_id" in span:
            raise SpanProblem(f"第{index}条区间：text 不接受 attachment_id")
        return revision["text"]
    attachment_id = span.get("attachment_id")
    if not isinstance(attachment_id, str) or not attachment_id:
        raise SpanProblem(f"第{index}条区间：缺少 attachment_id")
    for attachment in revision["attachments"]:
        if attachment["attachment_id"] == attachment_id:
            key = "filename" if field == "attachment_filename" else "description"
            return attachment[key]
    raise SpanProblem(f"第{index}条区间：附件编号不存在")


def normalize_spans(spans, revision):
    """按 revision 校验并规范化区间列表；不合法时抛出 SpanProblem。

    revision 形如 {"text": str, "attachments": [{"attachment_id", "filename",
    "description"}]}。返回的每条区间只保留必要字段，坐标为码点左闭右开。
    """
    if not isinstance(spans, list) or len(spans) > MAX_SPANS:
        raise SpanProblem("遮盖区间列表不符合要求")
    normalized = []
    for index, raw in enumerate(spans, start=1):
        if not isinstance(raw, dict):
            raise SpanProblem(f"第{index}条区间格式错误")
        if set(raw) - _SPAN_KEYS:
            raise SpanProblem(f"第{index}条区间含未知字段")
        field = raw.get("field")
        if field not in FIELDS:
            raise SpanProblem(f"第{index}条区间 field 无效")
        start, end = raw.get("start"), raw.get("end")
        if not _is_index(start) or not _is_index(end):
            raise SpanProblem(f"第{index}条区间坐标必须是整数")
        target = _target_text(revision, raw, index)
        if not 0 <= start < end <= len(target):
            raise SpanProblem(f"第{index}条区间越界或为空")
        label = raw.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > _MAX_LABEL):
            raise SpanProblem(f"第{index}条区间 label 无效")
        item = {"field": field, "start": start, "end": end}
        if field in _ATTACHMENT_FIELDS:
            item["attachment_id"] = raw["attachment_id"]
        if label is not None:
            item["label"] = label
        normalized.append(item)
    return normalized


def apply_policy(revision, spans):
    """把规范化后的区间应用到 revision，返回去标识内容。

    正文、附件文件名、附件说明都在遮盖范围内。
    """
    text_intervals = [(s["start"], s["end"]) for s in spans if s["field"] == "text"]
    attachments = []
    for attachment in revision["attachments"]:
        attachment_id = attachment["attachment_id"]
        filename_intervals = [
            (s["start"], s["end"]) for s in spans
            if s["field"] == "attachment_filename" and s["attachment_id"] == attachment_id
        ]
        description_intervals = [
            (s["start"], s["end"]) for s in spans
            if s["field"] == "attachment_description" and s["attachment_id"] == attachment_id
        ]
        attachments.append({
            "attachment_id": attachment_id,
            "filename": redact_text(attachment["filename"], filename_intervals),
            "description": redact_text(attachment["description"], description_intervals),
        })
    return {
        "text": redact_text(revision["text"], text_intervals),
        "attachments": attachments,
    }
