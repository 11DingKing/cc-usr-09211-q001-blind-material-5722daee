"""材料隔离存储：原件、回访方式、遮盖策略、去标识副本与访问记录。

设计要点：
- 原件与回访方式分开保存，回访方式只有接收窗口能读写；
- 原件每次补充产生新的 revision，遮盖策略每次更新产生新的
  policy_version，副本记录自己对应的 (revision, policy_version)，
  全部落盘，重启后仍可追查副本对应的原件版本；
- 原件补充或策略更新会使尚未领取的旧副本失效，已领取的副本
  保持可读（调查组已经拿到）；
- 访问记录只写动作与版本号，绝不写入联系方式或原文内容。
"""

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from contracts import read_records
from redaction import SpanProblem, apply_policy, normalize_spans

MAX_TEXT = 200_000
MAX_ATTACHMENT_NAME = 500
MAX_ATTACHMENT_DESC = 5_000
MAX_ATTACHMENTS = 100
MAX_CONTACT = 500
MAX_ACTOR = 128
DOCUMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ATTACHMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class StoreError(Exception):
    """存储层业务错误，接口层映射为通用错误信息。"""


class RequestProblem(StoreError, ValueError):
    """请求字段不合法；消息只含字段名，不含字段值。"""


class Missing(StoreError):
    pass


class Exists(StoreError):
    pass


class BadState(StoreError):
    pass


class UnknownToken(StoreError):
    pass


class NotReady(StoreError):
    pass


class Invalidated(StoreError):
    pass


class PolicyStale(StoreError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _check_text(value, name, limit, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value):
        raise RequestProblem(f"字段 {name} 不符合要求")
    return value


def _check_document_id(document_id):
    if not isinstance(document_id, str) or not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise RequestProblem("字段 document_id 不符合要求")


def _normalize_attachments(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ATTACHMENTS:
        raise RequestProblem("字段 attachments 不符合要求")
    result = []
    seen = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise RequestProblem("附件格式错误")
        attachment_id = item.get("attachment_id") or f"att-{index}"
        if not isinstance(attachment_id, str) or not ATTACHMENT_ID_PATTERN.fullmatch(attachment_id):
            raise RequestProblem("附件编号不符合要求")
        if attachment_id in seen:
            raise RequestProblem("附件编号重复")
        seen.add(attachment_id)
        result.append({
            "attachment_id": attachment_id,
            "filename": _check_text(item.get("filename"), "filename", MAX_ATTACHMENT_NAME),
            "description": _check_text(item.get("description", ""), "description", MAX_ATTACHMENT_DESC, allow_empty=True),
        })
    return result


def _normalize_contact(raw):
    if not isinstance(raw, dict):
        raise RequestProblem("字段 contact 不符合要求")
    return {
        "channel": _check_text(raw.get("channel"), "channel", 64),
        "detail": _check_text(raw.get("detail"), "detail", MAX_CONTACT),
        "updated_at": _now(),
    }


class Store:
    """线程安全的材料隔离存储；path 为 None 时仅内存运行（测试用）。"""

    def __init__(self, path, sign_secret, seed_path=None):
        self.path = str(path) if path else None
        self._sign_secret = sign_secret.encode("utf-8") if isinstance(sign_secret, str) else sign_secret
        self._lock = threading.RLock()
        self.state = {"documents": {}, "contacts": {}, "copies": {}, "tokens": {}, "audit": []}
        if self.path and Path(self.path).exists():
            self.state = json.loads(Path(self.path).read_text(encoding="utf-8"))
        elif seed_path and Path(seed_path).exists():
            self._seed(Path(seed_path))
            self._save()

    # ---- 内部工具 ----

    def _save(self):
        if not self.path:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path + ".tmp"
        Path(tmp).write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _audit(self, actor, action, target, result, detail=None):
        entry = {"at": _now(), "actor": actor, "action": action, "target": target, "result": result}
        if detail is not None:
            entry["detail"] = detail
        self.state["audit"].append(entry)

    def _seed(self, seed_path):
        records = read_records(seed_path)
        for record in records:
            document = self.state["documents"].get(record.document_id)
            expected = 1 if document is None else len(document["revisions"]) + 1
            if record.revision != expected:
                raise ValueError("样例版本号不连续")
            if document is None:
                document = {"document_id": record.document_id, "revisions": [], "policies": []}
                self.state["documents"][record.document_id] = document
            revision = self._make_revision(record.revision, record.text, [])
            document["revisions"].append(revision)
            document["policies"].append({
                "policy_version": len(document["policies"]) + 1,
                "spans": normalize_spans([dict(span) for span in record.spans], revision),
                "updated_at": _now(),
            })
        self._audit("system", "seed.load", "-", "ok", f"记录数 {len(records)}")

    def _make_revision(self, number, text, attachments):
        _check_text(text, "text", MAX_TEXT)
        return {
            "revision": number,
            "text": text,
            "attachments": _normalize_attachments(attachments),
            "received_at": _now(),
        }

    def _doc(self, document_id):
        document = self.state["documents"].get(document_id)
        if document is None:
            raise Missing()
        return document

    def _copy(self, copy_id):
        record = self.state["copies"].get(copy_id)
        if record is None:
            raise Missing()
        return record

    def _invalidate(self, record, reason):
        record["status"] = "invalidated"
        record["invalidated_at"] = _now()
        record["invalidated_reason"] = reason
        self._audit("system", "copy.invalidate", record["copy_id"], "ok", reason)

    def _invalidate_unclaimed(self, document, reason):
        current = (len(document["revisions"]), len(document["policies"]))
        for record in self.state["copies"].values():
            if record["document_id"] != document["document_id"]:
                continue
            if record["status"] not in ("pending_review", "signed") or record["claimed_at"] is not None:
                continue
            if (record["revision"], record["policy_version"]) != current:
                self._invalidate(record, reason)

    def _refresh_staleness(self, record):
        if record["status"] not in ("pending_review", "signed") or record["claimed_at"] is not None:
            return
        document = self.state["documents"].get(record["document_id"])
        if document is None:
            return
        current = (len(document["revisions"]), len(document["policies"]))
        if (record["revision"], record["policy_version"]) != current:
            self._invalidate(record, "版本已被取代")

    def _copy_public(self, record):
        return {
            "copy_id": record["copy_id"],
            "document_id": record["document_id"],
            "revision": record["revision"],
            "policy_version": record["policy_version"],
            "status": record["status"],
            "created_at": record["created_at"],
            "claimed_at": record["claimed_at"],
            "invalidated_at": record["invalidated_at"],
            "invalidated_reason": record["invalidated_reason"],
            "review": copy.deepcopy(record["review"]),
            "content": copy.deepcopy(record["content"]),
            "download_path": f"/download/{record['download_token']}" if record["download_token"] else None,
        }

    # ---- 接收窗口 ----

    def submit_document(self, document_id, text, attachments=None, contact=None):
        with self._lock:
            _check_document_id(document_id)
            if document_id in self.state["documents"]:
                raise Exists()
            revision = self._make_revision(1, text, attachments)
            self.state["documents"][document_id] = {
                "document_id": document_id,
                "revisions": [revision],
                "policies": [],
            }
            if contact is not None:
                self.state["contacts"][document_id] = _normalize_contact(contact)
            self._audit("intake", "intake.submit", document_id, "ok")
            self._save()
            return {"document_id": document_id, "revision": 1}

    def add_revision(self, document_id, text, attachments=None):
        with self._lock:
            document = self._doc(document_id)
            revision = self._make_revision(len(document["revisions"]) + 1, text, attachments)
            document["revisions"].append(revision)
            self._invalidate_unclaimed(document, "原件已补充")
            self._audit("intake", "intake.supplement", document_id, "ok", f"revision {revision['revision']}")
            self._save()
            return {"document_id": document_id, "revision": revision["revision"]}

    def put_contact(self, document_id, channel, detail):
        with self._lock:
            self._doc(document_id)
            self.state["contacts"][document_id] = _normalize_contact({"channel": channel, "detail": detail})
            self._audit("intake", "intake.contact.update", document_id, "ok")
            self._save()
            return {"document_id": document_id, "contact": "已保留"}

    def get_contact(self, document_id):
        with self._lock:
            self._doc(document_id)
            self._audit("intake", "intake.contact.read", document_id, "ok")
            self._save()
            return {"document_id": document_id, "contact": copy.deepcopy(self.state["contacts"].get(document_id))}

    def delete_contact(self, document_id):
        with self._lock:
            self._doc(document_id)
            self.state["contacts"].pop(document_id, None)
            self._audit("intake", "intake.contact.delete", document_id, "ok")
            self._save()
            return {"document_id": document_id, "contact": "已移除"}

    # ---- 独立保管员 ----

    def list_documents(self):
        with self._lock:
            return {"documents": [
                {
                    "document_id": document_id,
                    "current_revision": len(document["revisions"]),
                    "policy_version": len(document["policies"]),
                    "copies": sum(1 for c in self.state["copies"].values() if c["document_id"] == document_id),
                }
                for document_id, document in sorted(self.state["documents"].items())
            ]}

    def get_original(self, document_id):
        with self._lock:
            document = self._doc(document_id)
            self._audit("custodian", "custodian.original.read", document_id, "ok")
            self._save()
            return copy.deepcopy(document)

    def set_policy(self, document_id, spans):
        with self._lock:
            document = self._doc(document_id)
            normalized = normalize_spans(spans, document["revisions"][-1])
            version = len(document["policies"]) + 1
            document["policies"].append({
                "policy_version": version,
                "spans": normalized,
                "updated_at": _now(),
            })
            self._invalidate_unclaimed(document, "遮盖策略已更新")
            self._audit("custodian", "custodian.policy.update", document_id, "ok", f"policy_version {version}")
            self._save()
            return {"document_id": document_id, "policy_version": version}

    def create_copy(self, document_id):
        with self._lock:
            document = self._doc(document_id)
            if not document["policies"]:
                raise BadState()
            revision = document["revisions"][-1]
            policy = document["policies"][-1]
            try:
                spans = normalize_spans(policy["spans"], revision)
            except SpanProblem:
                raise PolicyStale()
            while True:
                copy_id = "CP-" + secrets.token_hex(8)
                if copy_id not in self.state["copies"]:
                    break
            record = {
                "copy_id": copy_id,
                "document_id": document_id,
                "revision": revision["revision"],
                "policy_version": policy["policy_version"],
                "content": apply_policy(revision, spans),
                "status": "pending_review",
                "created_at": _now(),
                "claimed_at": None,
                "download_token": None,
                "invalidated_at": None,
                "invalidated_reason": None,
                "review": None,
            }
            self.state["copies"][copy_id] = record
            detail = f"{document_id} r{record['revision']} p{record['policy_version']}"
            self._audit("custodian", "custodian.copy.create", copy_id, "ok", detail)
            self._save()
            return self._copy_public(record)

    def get_copy_trace(self, copy_id):
        with self._lock:
            return self._copy_public(self._copy(copy_id))

    def audit_log(self):
        with self._lock:
            return {"entries": copy.deepcopy(self.state["audit"])}

    # ---- 复核员 ----

    def get_copy_for_review(self, copy_id):
        with self._lock:
            record = self._copy(copy_id)
            return {
                "copy_id": record["copy_id"],
                "document_id": record["document_id"],
                "revision": record["revision"],
                "policy_version": record["policy_version"],
                "status": record["status"],
                "created_at": record["created_at"],
                "content": copy.deepcopy(record["content"]),
            }

    def sign_copy(self, copy_id, reviewer):
        with self._lock:
            record = self._copy(copy_id)
            if record["status"] != "pending_review":
                raise BadState()
            _check_text(reviewer, "reviewer", MAX_ACTOR)
            content_hash = hashlib.sha256(
                json.dumps(record["content"], ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            payload = "\n".join([
                record["copy_id"], record["document_id"],
                str(record["revision"]), str(record["policy_version"]), content_hash,
            ])
            signature = hmac.new(self._sign_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
            token = secrets.token_urlsafe(32)
            record["review"] = {
                "decision": "signed",
                "reviewer": reviewer,
                "signed_at": _now(),
                "content_hash": content_hash,
                "signature": signature,
            }
            record["status"] = "signed"
            record["download_token"] = token
            self.state["tokens"][token] = copy_id
            self._audit("reviewer", "reviewer.copy.sign", copy_id, "ok")
            self._save()
            return self._copy_public(record)

    def reject_copy(self, copy_id, reviewer, reason=None):
        with self._lock:
            record = self._copy(copy_id)
            if record["status"] != "pending_review":
                raise BadState()
            _check_text(reviewer, "reviewer", MAX_ACTOR)
            record["review"] = {
                "decision": "rejected",
                "reviewer": reviewer,
                "rejected_at": _now(),
                "reason": _check_text(reason or "", "reason", MAX_CONTACT, allow_empty=True),
            }
            record["status"] = "rejected"
            self._audit("reviewer", "reviewer.copy.reject", copy_id, "ok")
            self._save()
            return self._copy_public(record)

    # ---- 取阅 ----

    def download(self, token):
        with self._lock:
            copy_id = self.state["tokens"].get(token)
            if copy_id is None:
                self._audit("reader", "copy.download", "-", "unknown")
                self._save()
                raise UnknownToken()
            record = self.state["copies"][copy_id]
            self._refresh_staleness(record)
            if record["status"] == "invalidated":
                self._audit("reader", "copy.download", copy_id, "invalidated")
                self._save()
                raise Invalidated()
            if record["status"] != "signed":
                self._audit("reader", "copy.download", copy_id, "pending")
                self._save()
                raise NotReady()
            if record["claimed_at"] is None:
                record["claimed_at"] = _now()
            self._audit("reader", "copy.download", copy_id, "ok")
            self._save()
            review = record["review"]
            return {
                "copy_id": record["copy_id"],
                "document_id": record["document_id"],
                "revision": record["revision"],
                "policy_version": record["policy_version"],
                "content": copy.deepcopy(record["content"]),
                "review": {
                    "reviewer": review["reviewer"],
                    "signed_at": review["signed_at"],
                    "content_hash": review["content_hash"],
                    "signature": review["signature"],
                },
                "delivered_at": _now(),
            }
