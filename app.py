"""材料隔离后端：接收 → 去标识副本 → 复核签名 → 调查组取阅。

角色（Bearer 令牌区分）：
- intake        接收窗口：登记材料，保管当事人自愿留下的回访方式。
- custodian     独立保管员：唯一可读原件的角色；负责原件补充、遮盖策略
                更新与去标识副本生成。
- reviewer      复核员：查看去标识副本并签名，签名后副本才可交付。
- investigator  调查组（普通取阅者）：只能看到已签名副本的去标识内容，
                不能反查提交者，也接触不到任何联系方式。

调查员可见的下载链接、错误信息与访问记录均为不透明随机值或通用文案，
不带出联系方式。
"""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from redaction import SpanError, normalize_spans, redact

ROLES = ("intake", "custodian", "reviewer", "investigator")

ERR_NOT_FOUND = {"error": "接口不存在"}
ERR_UNAUTHORIZED = {"error": "未授权"}
ERR_FORBIDDEN = {"error": "无权访问"}
ERR_BAD_REQUEST = {"error": "请求格式不正确"}
ERR_MISSING = {"error": "记录不存在"}
ERR_INTERNAL = {"error": "服务内部错误"}
ERR_LINK_DEAD = {"error": "链接无效或已失效"}

DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


def _now():
    return datetime.now(timezone.utc)


def _iso(moment=None):
    return (moment or _now()).isoformat(timespec="seconds")


def _parse_iso(text):
    return datetime.fromisoformat(text)


def _validate_attachments(items):
    """附件说明与文件名同样进入遮盖范围，逐字段校验区间。"""
    # 统一成规范存储结构
    normalized = []
    for item in items:
        filename = item.get("filename")
        description = item.get("description", "")
        if not isinstance(filename, str) or not filename:
            raise ValueError("附件缺少文件名")
        if not isinstance(description, str):
            raise ValueError("附件说明必须是字符串")
        filename_spans = normalize_spans(item.get("filename_spans", []), len(filename))
        description_spans = normalize_spans(item.get("description_spans", []), len(description))
        normalized.append({
            "filename": filename,
            "description": description,
            "filename_spans": [[s, e] for s, e in filename_spans],
            "description_spans": [[s, e] for s, e in description_spans],
        })
    return normalized


class IsolationApp:
    def __init__(self, store, tokens, signing_key, link_ttl_seconds=900):
        self.store = store
        self.tokens = dict(tokens)
        if isinstance(signing_key, str):
            signing_key = signing_key.encode("utf-8")
        self.signing_key = signing_key
        self.link_ttl_seconds = link_ttl_seconds

    # ---------------- 框架 ----------------

    def _routes(self):
        return [
            ("POST", r"^/intake/submissions$", {"intake"}, self._intake_submit),
            ("GET", r"^/intake/callbacks/(?P<ref>[^/]+)$", {"intake"}, self._intake_callback),
            ("GET", r"^/custodian/originals/(?P<did>[^/]+)$", {"custodian"}, self._custodian_original),
            ("GET", r"^/custodian/originals/(?P<did>[^/]+)/revisions/(?P<rev>\d+)$", {"custodian"}, self._custodian_revision),
            ("POST", r"^/custodian/originals/(?P<did>[^/]+)/revisions$", {"custodian"}, self._custodian_add_revision),
            ("POST", r"^/custodian/originals/(?P<did>[^/]+)/copies$", {"custodian"}, self._custodian_make_copy),
            ("GET", r"^/custodian/copies$", {"custodian"}, self._custodian_list_copies),
            ("GET", r"^/custodian/copies/(?P<cid>[^/]+)$", {"custodian"}, self._custodian_get_copy),
            ("GET", r"^/reviewer/copies$", {"reviewer"}, self._reviewer_list),
            ("GET", r"^/reviewer/copies/(?P<cid>[^/]+)$", {"reviewer"}, self._reviewer_get),
            ("POST", r"^/reviewer/copies/(?P<cid>[^/]+)/sign$", {"reviewer"}, self._reviewer_sign),
            ("GET", r"^/investigator/copies$", {"investigator"}, self._investigator_list),
            ("POST", r"^/investigator/copies/(?P<cid>[^/]+)/link$", {"investigator"}, self._investigator_link),
            ("GET", r"^/investigator/copies/(?P<cid>[^/]+)/access$", {"investigator"}, self._investigator_access),
            ("GET", r"^/download/(?P<token>[^/]+)$", {"investigator"}, self._download),
        ]

    def handle(self, method, path, headers, body):
        """返回 (status, payload)；不抛业务异常。"""
        path = path.split("?", 1)[0]
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        for route_method, pattern, roles, handler in self._routes():
            match = re.match(pattern, path)
            if not match or route_method != method:
                continue
            role = self._authenticate(headers)
            if role is None:
                return 401, dict(ERR_UNAUTHORIZED)
            if role not in roles:
                return 403, dict(ERR_FORBIDDEN)
            try:
                return handler(match.groupdict(), body, role)
            except (SpanError, ValueError, KeyError):
                return 400, dict(ERR_BAD_REQUEST)
        return 404, dict(ERR_NOT_FOUND)

    def _authenticate(self, headers):
        get = headers.get if hasattr(headers, "get") else None
        if get is None:
            return None
        auth = get("Authorization") or get("authorization")
        if not auth or not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        for role, expected in self.tokens.items():
            if hmac.compare_digest(token, expected):
                return role
        return None

    @staticmethod
    def _json(body):
        if not body:
            return {}
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是对象")
        return data

    # ---------------- 接收窗口 ----------------

    def _intake_submit(self, params, body, role):
        data = self._json(body)
        text = data.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("正文缺失")
        spans = [[s, e] for s, e in normalize_spans(data.get("spans", []), len(text))]
        attachments = _validate_attachments(data.get("attachments", []))

        document_id = data.get("document_id")
        if document_id is None:
            document_id = "BL-" + secrets.token_hex(4).upper()
        if not isinstance(document_id, str) or not DOCUMENT_ID_PATTERN.match(document_id):
            raise ValueError("编号不合法")

        contact = data.get("contact")
        if contact is not None:
            if not isinstance(contact, dict):
                raise ValueError("contact 必须是对象")
            channel, value = contact.get("channel"), contact.get("value")
            if not isinstance(channel, str) or not channel:
                raise ValueError("contact.channel 缺失")
            if not isinstance(value, str) or not value:
                raise ValueError("contact.value 缺失")

        created = _iso()
        with self.store.locked():
            if self.store.document_exists(document_id):
                return 409, {"error": "编号已存在"}
            self.store.add_revision(document_id, 1, text, spans, attachments, created, role)
            submitter_ref = None
            if contact is not None:
                submitter_ref = "SR-" + secrets.token_hex(8)
                self.store.add_contact(submitter_ref, document_id,
                                       contact["channel"], contact["value"], created)
            self.store.audit(created, role, "submit", "document", document_id,
                             {"revision": 1, "has_contact": contact is not None})
        return 201, {
            "document_id": document_id,
            "revision": 1,
            "submitter_ref": submitter_ref,
        }

    def _intake_callback(self, params, body, role):
        contact = self.store.get_contact(params["ref"])
        if contact is None:
            return 404, dict(ERR_MISSING)
        return 200, {
            "submitter_ref": contact["submitter_ref"],
            "document_id": contact["document_id"],
            "channel": contact["channel"],
            "value": contact["value"],
        }

    # ---------------- 独立保管员 ----------------

    def _custodian_original(self, params, body, role):
        revision = self.store.get_revision(params["did"])
        if revision is None:
            return 404, dict(ERR_MISSING)
        return 200, revision

    def _custodian_revision(self, params, body, role):
        revision = self.store.get_revision(params["did"], int(params["rev"]))
        if revision is None:
            return 404, dict(ERR_MISSING)
        return 200, revision

    def _custodian_add_revision(self, params, body, role):
        """原件补充（换正文）或遮盖策略更新（换区间），都产生新版本号。

        新版本落地的同时，该原件所有尚未领取的旧副本即刻失效。
        """
        data = self._json(body)
        did = params["did"]
        created = _iso()
        with self.store.locked():
            current = self.store.get_revision(did)
            if current is None:
                return 404, dict(ERR_MISSING)
            text = data.get("text", current["text"])
            if not isinstance(text, str) or not text:
                raise ValueError("正文缺失")
            spans = ([[s, e] for s, e in normalize_spans(data["spans"], len(text))]
                     if "spans" in data else current["spans"])
            attachments = (_validate_attachments(data["attachments"])
                           if "attachments" in data else current["attachments"])
            if (text == current["text"] and spans == current["spans"]
                    and attachments == current["attachments"]):
                raise ValueError("内容没有变化")
            revision = current["revision"] + 1
            self.store.add_revision(did, revision, text, spans, attachments, created, role)
            invalidated = self.store.invalidate_unclaimed(did, created)
            self.store.audit(created, role, "revision_added", "document", did,
                             {"revision": revision})
            for copy_id in invalidated:
                self.store.audit(created, "system", "copy_invalidated", "copy", copy_id,
                                 {"document_id": did, "revision": revision})
        return 201, {
            "document_id": did,
            "revision": revision,
            "invalidated": invalidated,
        }

    def _custodian_make_copy(self, params, body, role):
        did = params["did"]
        created = _iso()
        with self.store.locked():
            revision = self.store.get_revision(did)
            if revision is None:
                return 404, dict(ERR_MISSING)
            content = {
                "text": redact(revision["text"], revision["spans"]),
                "attachments": [
                    {
                        "attachment_id": f"att-{index}",
                        "filename": redact(item["filename"], item["filename_spans"]),
                        "description": redact(item["description"], item["description_spans"]),
                    }
                    for index, item in enumerate(revision["attachments"])
                ],
            }
            copy_id = "CP-" + secrets.token_hex(8)
            self.store.add_copy(copy_id, did, revision["revision"], content, "draft", created)
            self.store.audit(created, role, "copy_generated", "copy", copy_id,
                             {"document_id": did, "revision": revision["revision"]})
        return 201, {
            "copy_id": copy_id,
            "document_id": did,
            "revision": revision["revision"],
            "state": "draft",
        }

    @staticmethod
    def _copy_meta(copy):
        return {
            "copy_id": copy["copy_id"],
            "document_id": copy["document_id"],
            "revision": copy["revision"],
            "state": copy["state"],
            "created_at": copy["created_at"],
            "signed_by": copy["signed_by"],
            "signed_at": copy["signed_at"],
            "claimed_at": copy["claimed_at"],
            "invalidated_at": copy["invalidated_at"],
        }

    def _custodian_list_copies(self, params, body, role):
        return 200, {"copies": [self._copy_meta(c) for c in self.store.list_copies()]}

    def _custodian_get_copy(self, params, body, role):
        copy = self.store.get_copy(params["cid"])
        if copy is None:
            return 404, dict(ERR_MISSING)
        return 200, self._copy_meta(copy)

    # ---------------- 复核员 ----------------

    def _reviewer_list(self, params, body, role):
        return 200, {"copies": [self._copy_meta(c) for c in self.store.list_copies()]}

    def _reviewer_get(self, params, body, role):
        copy = self.store.get_copy(params["cid"])
        if copy is None:
            return 404, dict(ERR_MISSING)
        payload = self._copy_meta(copy)
        payload["content"] = copy["content"]
        return 200, payload

    def _signature_payload(self, copy, signer):
        content_json = json.dumps(copy["content"], ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        return "\n".join([
            copy["copy_id"], copy["document_id"], str(copy["revision"]), digest, signer,
        ]).encode("utf-8")

    def _reviewer_sign(self, params, body, role):
        cid = params["cid"]
        signed_at = _iso()
        with self.store.locked():
            copy = self.store.get_copy(cid)
            if copy is None:
                return 404, dict(ERR_MISSING)
            if copy["state"] != "draft":
                return 409, {"error": "副本状态不允许复核"}
            signature = hmac.new(
                self.signing_key, self._signature_payload(copy, role), hashlib.sha256
            ).hexdigest()
            if not self.store.sign_copy(cid, role, signature, signed_at):
                return 409, {"error": "副本状态不允许复核"}
            self.store.audit(signed_at, role, "copy_signed", "copy", cid,
                             {"document_id": copy["document_id"],
                              "revision": copy["revision"]})
        return 200, {"copy_id": cid, "state": "signed", "signature": signature}

    # ---------------- 调查组（普通取阅者） ----------------

    def _investigator_list(self, params, body, role):
        copies = self.store.list_copies(states=("signed", "claimed"))
        return 200, {"copies": [
            {
                "copy_id": c["copy_id"],
                "document_id": c["document_id"],
                "revision": c["revision"],
                "state": c["state"],
            }
            for c in copies
        ]}

    def _investigator_link(self, params, body, role):
        cid = params["cid"]
        now = _now()
        with self.store.locked():
            copy = self.store.get_copy(cid)
            if copy is None or copy["state"] == "draft":
                return 404, dict(ERR_MISSING)
            if copy["state"] == "invalidated":
                return 410, {"error": "副本已失效"}
            token = secrets.token_urlsafe(24)
            expires = now + timedelta(seconds=self.link_ttl_seconds)
            self.store.add_token(token, cid, _iso(now), _iso(expires))
            self.store.audit(_iso(now), role, "link_issued", "copy", cid, {})
        return 201, {"url": f"/download/{token}", "expires_at": _iso(expires)}

    def _investigator_access(self, params, body, role):
        cid = params["cid"]
        copy = self.store.get_copy(cid)
        if copy is None or copy["state"] not in ("signed", "claimed"):
            return 404, dict(ERR_MISSING)
        records = [
            {"ts": r["ts"], "actor_role": r["actor_role"], "action": r["action"]}
            for r in self.store.audit_for("copy", cid)
        ]
        return 200, {"copy_id": cid, "records": records}

    def _download(self, params, body, role):
        token = params["token"]
        now = _now()
        with self.store.locked():
            ticket = self.store.get_token(token)
            copy = self.store.get_copy(ticket["copy_id"]) if ticket else None
            if (
                ticket is None
                or ticket["used_at"] is not None
                or _parse_iso(ticket["expires_at"]) <= now
                or copy is None
                or copy["state"] not in ("signed", "claimed")
            ):
                return 404, dict(ERR_LINK_DEAD)
            expected = hmac.new(
                self.signing_key,
                self._signature_payload(copy, copy["signed_by"] or ""),
                hashlib.sha256,
            ).hexdigest()
            if not copy["signature"] or not hmac.compare_digest(expected, copy["signature"]):
                return 500, dict(ERR_INTERNAL)
            if not self.store.use_token(token, _iso(now)):
                return 404, dict(ERR_LINK_DEAD)
            if copy["state"] == "signed":
                self.store.claim_copy(copy["copy_id"], _iso(now))
            self.store.audit(_iso(now), role, "copy_downloaded", "copy",
                             copy["copy_id"], {})
        return 200, {
            "copy_id": copy["copy_id"],
            "document_id": copy["document_id"],
            "revision": copy["revision"],
            "content": copy["content"],
            "signature": {
                "signed_by": copy["signed_by"],
                "value": copy["signature"],
                "signed_at": copy["signed_at"],
            },
        }


def make_handler(app_factory):
    """构造 HTTP 处理器；app_factory 是返回 IsolationApp 的惰性工厂。"""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            try:
                status, payload = app_factory().handle(
                    self.command, self.path, self.headers, body)
            except Exception:
                status, payload = 500, dict(ERR_INTERNAL)
            self._respond(status, payload)

        def _respond(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

        do_GET = _dispatch
        do_POST = _dispatch

    return Handler
