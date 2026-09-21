"""把 data/example.json 样的 SourceRecord 记录导入为原件版本。

同一 document_id 的记录按 revision 顺序落库，要求版本号从 1 开始连续；
原文与遮盖位置随版本号一起保存，与数据约定一致。
"""

from collections import defaultdict
from datetime import datetime, timezone

from contracts import read_records
from redaction import normalize_spans


def seed(store, path):
    records = read_records(path)
    by_document = defaultdict(list)
    for record in records:
        by_document[record.document_id].append(record)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for document_id, items in sorted(by_document.items()):
        items.sort(key=lambda record: record.revision)
        if store.document_exists(document_id):
            raise ValueError(f"{document_id} 已存在，拒绝重复导入")
        for expected, record in enumerate(items, start=1):
            if record.revision != expected:
                raise ValueError(f"{document_id} 版本号不连续")
            spans = [[s, e] for s, e in normalize_spans(record.spans, len(record.text))]
            store.add_revision(document_id, record.revision, record.text, spans, [],
                               created, "seed")
    return sorted(by_document)
