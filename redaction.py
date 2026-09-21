"""按 Unicode 码点坐标对原文做遮盖。

坐标约定与 README 一致：区间左闭右开，单位为 Unicode 码点。
一次遮盖允许传入多个相互重叠的区间，内部先合并再套用。
套用前把区间边界向外对齐到字素簇（grapheme cluster）边界，
避免中文、表情等多码点字符被切开后漏出半个字符。
"""

from bisect import bisect_left, bisect_right
import unicodedata

MASK = "█"


class SpanError(ValueError):
    """区间坐标不合法。"""


def _is_regional_indicator(ch):
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_zwj(ch):
    return ord(ch) == 0x200D


def _is_extend(ch):
    cp = ord(ch)
    if unicodedata.category(ch) in ("Mn", "Me"):
        return True
    if 0xFE00 <= cp <= 0xFE0F:      # 变体选择符
        return True
    if 0xE0100 <= cp <= 0xE01EF:    # 补充变体选择符
        return True
    if 0x1F3FB <= cp <= 0x1F3FF:    # 表情肤色修饰符
        return True
    if 0xE0020 <= cp <= 0xE007F:    # 标签字符
        return True
    return False


def _is_spacing_mark(ch):
    return unicodedata.category(ch) == "Mc"


def _is_control(ch):
    return unicodedata.category(ch) in ("Cc", "Cf") and not _is_zwj(ch)


def _is_prepend(ch):
    cp = ord(ch)
    return (
        0x0600 <= cp <= 0x0605
        or cp in (0x06DD, 0x070F, 0x08E2, 0x0D4E, 0x110BD, 0x110CD)
    )


def _hangul_class(ch):
    cp = ord(ch)
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return None


def _no_break(prev, cur, ri_run):
    """UAX #29 常用子集：prev 与 cur 之间是否不断开。

    宁可多合并也不少合并——边界只会向外扩，遮盖范围只大不小，
    这是防“漏出半个字符”的安全方向。
    """
    if prev == "\r" and cur == "\n":
        return True                                   # GB3
    if _is_control(prev) or _is_control(cur):
        return False                                  # GB4/GB5
    if _is_extend(cur) or _is_zwj(cur) or _is_spacing_mark(cur):
        return True                                   # GB9/GB9a
    if _is_prepend(prev):
        return True                                   # GB9b
    if _is_zwj(prev):
        return True                                   # GB11（简化：ZWJ 后一律不断）
    prev_h = _hangul_class(prev)
    cur_h = _hangul_class(cur)
    if prev_h == "L" and cur_h in ("L", "V", "LV", "LVT"):
        return True                                   # GB6
    if prev_h in ("LV", "V") and cur_h in ("V", "T"):
        return True                                   # GB7
    if prev_h in ("LVT", "T") and cur_h == "T":
        return True                                   # GB8
    if _is_regional_indicator(prev) and _is_regional_indicator(cur):
        return ri_run % 2 == 1                        # GB12/GB13（旗帜成对）
    return False


def grapheme_boundaries(text):
    """返回 text 的全部字素簇边界（码点偏移，含 0 与 len(text)）。"""
    bounds = {0, len(text)}
    ri_run = 0
    for i in range(1, len(text)):
        prev, cur = text[i - 1], text[i]
        if not _no_break(prev, cur, ri_run):
            bounds.add(i)
        if _is_regional_indicator(cur):
            ri_run = ri_run + 1 if _is_regional_indicator(prev) else 1
        else:
            ri_run = 0
    return bounds


def merge_spans(intervals):
    """合并已排序区间中的重叠与相接段。"""
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            last_start, last_end = merged[-1]
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def normalize_spans(spans, text_length):
    """校验并合并区间，返回排序后不重叠的 (start, end) 列表。

    空区间 [x, x) 视为无操作直接丢弃；越界、倒序、非整数一律拒绝。
    """
    if spans is None:
        return []
    if not isinstance(spans, (list, tuple)):
        raise SpanError("spans 必须是数组")
    cleaned = []
    for item in spans:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise SpanError("区间必须是 [start, end] 二元组")
        start, end = item
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise SpanError("区间端点必须是整数")
        if start < 0 or end < 0 or start > end or end > text_length:
            raise SpanError("区间越界")
        if start < end:
            cleaned.append((start, end))
    return merge_spans(sorted(cleaned))


def redact(text, spans, mask=MASK):
    """按码点区间遮盖 text；区间可重叠，边界向外对齐字素簇。"""
    if not isinstance(text, str):
        raise SpanError("text 必须是字符串")
    intervals = normalize_spans(spans, len(text))
    if not intervals:
        return text
    bounds = sorted(grapheme_boundaries(text))
    snapped = []
    for start, end in intervals:
        lo = bounds[bisect_right(bounds, start) - 1]
        hi = bounds[bisect_left(bounds, end)]
        snapped.append((lo, hi))
    parts = []
    pos = 0
    for start, end in merge_spans(snapped):
        parts.append(text[pos:start])
        parts.append(mask)
        pos = end
    parts.append(text[pos:])
    return "".join(parts)
