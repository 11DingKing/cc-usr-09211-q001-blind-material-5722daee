"""SQLite 持久化：原件版本、联系方式保险库、副本、下载令牌与审计。

- revisions：原文与遮盖位置随版本号一起保存，(document_id, revision) 主键。
- contacts：当事人自愿提供的回访方式，单独成表，只有接收窗口角色读取。
- copies：去标识副本，记录 document_id + revision，重启后仍可追查来源版本。
- download_tokens：一次性取阅令牌，令牌本身不含任何业务信息。
- audit_log：访问记录，detail 只写白名单字段，绝不写入联系方式。
"""

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    text TEXT NOT NULL,
    spans TEXT NOT NULL,
    attachments TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    PRIMARY KEY (document_id, revision)
);
CREATE TABLE IF NOT EXISTS contacts (
    submitter_ref TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copies (
    copy_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    signed_by TEXT,
    signature TEXT,
    signed_at TEXT,
    claimed_at TEXT,
    invalidated_at TEXT
);
CREATE TABLE IF NOT EXISTS download_tokens (
    token TEXT PRIMARY KEY,
    copy_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

COPY_STATES = ("draft", "signed", "claimed", "invalidated")


class Store:
    """线程安全的存储门面；连接惰性建立，/health 等探针不会落盘。"""

    def __init__(self, path):
        self.path = str(path)
        self._conn = None
        self._lock = threading.RLock()

    def locked(self):
        """复合操作（如新增版本+失效旧副本）在应用层持同一把锁。"""
        return self._lock

    def _connect(self):
        if self._conn is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
        return self._conn

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- 原件版本 ----

    def add_revision(self, document_id, revision, text, spans, attachments,
                     created_at, created_by):
        with self._lock:
            self._connect().execute(
                "INSERT INTO revisions"
                " (document_id, revision, text, spans, attachments, created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, revision, text,
                 json.dumps(spans, ensure_ascii=False),
                 json.dumps(attachments, ensure_ascii=False),
                 created_at, created_by),
            )
            self._conn.commit()

    def latest_revision_number(self, document_id):
        with self._lock:
            row = self._connect().execute(
                "SELECT MAX(revision) AS r FROM revisions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return row["r"] or 0

    def document_exists(self, document_id):
        return self.latest_revision_number(document_id) > 0

    def get_revision(self, document_id, revision=None):
        with self._lock:
            if revision is None:
                row = self._connect().execute(
                    "SELECT * FROM revisions WHERE document_id = ?"
                    " ORDER BY revision DESC LIMIT 1",
                    (document_id,),
                ).fetchone()
            else:
                row = self._connect().execute(
                    "SELECT * FROM revisions WHERE document_id = ? AND revision = ?",
                    (document_id, revision),
                ).fetchone()
        if row is None:
            return None
        return {
            "document_id": row["document_id"],
            "revision": row["revision"],
            "text": row["text"],
            "spans": json.loads(row["spans"]),
            "attachments": json.loads(row["attachments"]),
            "created_at": row["created_at"],
            "created_by": row["created_by"],
        }

    # ---- 联系方式保险库（仅接收窗口角色可达） ----

    def add_contact(self, submitter_ref, document_id, channel, value, created_at):
        with self._lock:
            self._connect().execute(
                "INSERT INTO contacts (submitter_ref, document_id, channel, value, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (submitter_ref, document_id, channel, value, created_at),
            )
            self._conn.commit()

    def get_contact(self, submitter_ref):
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM contacts WHERE submitter_ref = ?", (submitter_ref,)
            ).fetchone()
        return dict(row) if row else None

    # ---- 去标识副本 ----

    def add_copy(self, copy_id, document_id, revision, content, state, created_at):
        with self._lock:
            self._connect().execute(
                "INSERT INTO copies (copy_id, document_id, revision, content, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (copy_id, document_id, revision,
                 json.dumps(content, ensure_ascii=False, sort_keys=True),
                 state, created_at),
            )
            self._conn.commit()

    @staticmethod
    def _copy_from_row(row):
        if row is None:
            return None
        return {
            "copy_id": row["copy_id"],
            "document_id": row["document_id"],
            "revision": row["revision"],
            "content": json.loads(row["content"]),
            "state": row["state"],
            "created_at": row["created_at"],
            "signed_by": row["signed_by"],
            "signature": row["signature"],
            "signed_at": row["signed_at"],
            "claimed_at": row["claimed_at"],
            "invalidated_at": row["invalidated_at"],
        }

    def get_copy(self, copy_id):
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM copies WHERE copy_id = ?", (copy_id,)
            ).fetchone()
        return self._copy_from_row(row)

    def list_copies(self, states=None):
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM copies ORDER BY created_at, copy_id"
            ).fetchall()
        copies = [self._copy_from_row(row) for row in rows]
        if states is not None:
            copies = [c for c in copies if c["state"] in states]
        return copies

    def sign_copy(self, copy_id, signed_by, signature, signed_at):
        """仅 draft 状态可签名；返回是否生效。"""
        with self._lock:
            cur = self._connect().execute(
                "UPDATE copies SET state = 'signed', signed_by = ?, signature = ?,"
                " signed_at = ? WHERE copy_id = ? AND state = 'draft'",
                (signed_by, signature, signed_at, copy_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def claim_copy(self, copy_id, claimed_at):
        """仅 signed 状态可登记领取；返回是否生效。"""
        with self._lock:
            cur = self._connect().execute(
                "UPDATE copies SET state = 'claimed', claimed_at = ?"
                " WHERE copy_id = ? AND state = 'signed'",
                (claimed_at, copy_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def invalidate_unclaimed(self, document_id, invalidated_at):
        """原件补充或遮盖策略更新后，尚未领取的副本全部失效。"""
        with self._lock:
            rows = self._connect().execute(
                "SELECT copy_id FROM copies WHERE document_id = ?"
                " AND state IN ('draft', 'signed')",
                (document_id,),
            ).fetchall()
            ids = [row["copy_id"] for row in rows]
            self._connect().execute(
                "UPDATE copies SET state = 'invalidated', invalidated_at = ?"
                " WHERE document_id = ? AND state IN ('draft', 'signed')",
                (invalidated_at, document_id),
            )
            self._conn.commit()
        return ids

    # ---- 下载令牌 ----

    def add_token(self, token, copy_id, created_at, expires_at):
        with self._lock:
            self._connect().execute(
                "INSERT INTO download_tokens (token, copy_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token, copy_id, created_at, expires_at),
            )
            self._conn.commit()

    def get_token(self, token):
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM download_tokens WHERE token = ?", (token,)
            ).fetchone()
        return dict(row) if row else None

    def use_token(self, token, used_at):
        """一次性令牌：仅未使用时可核销。"""
        with self._lock:
            cur = self._connect().execute(
                "UPDATE download_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
                (used_at, token),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # ---- 访问记录 ----

    def audit(self, ts, actor_role, action, object_type, object_id, detail):
        """detail 只接收调用方白名单字段，禁止写入联系方式。"""
        with self._lock:
            self._connect().execute(
                "INSERT INTO audit_log (ts, actor_role, action, object_type, object_id, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, actor_role, action, object_type, object_id,
                 json.dumps(detail, ensure_ascii=False, sort_keys=True)),
            )
            self._conn.commit()

    def audit_for(self, object_type, object_id):
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM audit_log WHERE object_type = ? AND object_id = ?"
                " ORDER BY id",
                (object_type, object_id),
            ).fetchall()
        return [
            {
                "ts": row["ts"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "detail": json.loads(row["detail"]),
            }
            for row in rows
        ]
