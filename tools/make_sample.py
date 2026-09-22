"""生成 data/example.json 材料样例。

样例为虚构业务数据，不含个人联系方式。区间坐标按 Unicode 码点
由子串位置计算，保证与正文一致；坐标左闭右开。重新生成：

    python3 tools/make_sample.py
"""

import json
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / "data" / "example.json"


def span(text, needle, label):
    start = text.index(needle)
    return {"field": "text", "start": start, "end": start + len(needle), "label": label}


def build_records():
    records = []

    text = (
        "群众反映：青河区粮库夜班组长李建军在4月12日盘点后涂改台账，"
        "监控截图由同事王芳转到工作群，群里回复了👨‍👩‍👧和👍🏽，另附一张手写便条照片。"
    )
    records.append({
        "document_id": "BL-2026-0417",
        "revision": 1,
        "text": text,
        "spans": [
            span(text, "李建军", "当事人姓名"),
            span(text, "夜班组长李建军在4月12日", "岗位与日期"),
            span(text, "王芳", "知情人姓名"),
        ],
    })

    text = "反映：城东污水站运维员赵强将检测数据先发给外包群再上报，时间集中在5月3日至5月6日。"
    records.append({
        "document_id": "BL-2026-0422",
        "revision": 1,
        "text": text,
        "spans": [
            span(text, "赵强", "当事人姓名"),
        ],
    })

    text = (
        "补充反映：城东污水站运维员赵强将5月3日至5月6日的检测数据先发给外包群，"
        "群成员包括设备商代表孙丽，相关截图已留存。"
    )
    records.append({
        "document_id": "BL-2026-0422",
        "revision": 2,
        "text": text,
        "spans": [
            span(text, "赵强", "当事人姓名"),
            span(text, "孙丽", "相关人员姓名"),
            span(text, "设备商代表孙丽", "相关单位人员"),
        ],
    })

    return records


def main():
    records = build_records()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成 {OUTPUT}（{len(records)} 条记录）")


if __name__ == "__main__":
    main()
