from dataclasses import dataclass, asdict
import json
from pathlib import Path

@dataclass(frozen=True)
class SourceRecord:
    document_id: str
    revision: int
    text: str
    spans: list

def read_records(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("记录集合必须是数组")
    return [SourceRecord(**item) for item in raw]

def to_mapping(record):
    return asdict(record)
