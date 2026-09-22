"""材料隔离接口进程：接收、去标识副本生成、复核签名与调查组取阅。

角色与令牌（环境变量配置，默认值仅供本地开发）：
- BLIND_TOKEN_INTAKE     接收窗口：提交/补充原件，保管回访方式
- BLIND_TOKEN_CUSTODIAN  独立保管员：唯一可读原件，维护遮盖策略并生成副本
- BLIND_TOKEN_REVIEWER   复核员：复核副本并签名，签名后产生下载链接
- 调查员无需令牌，凭下载链接取阅去标识副本

下载链接、错误信息与访问记录均不带出联系方式；未知接口返回 404。
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import traceback

from redaction import SpanProblem
from store import (
    BadState,
    Exists,
    Invalidated,
    Missing,
    NotReady,
    PolicyStale,
    RequestProblem,
    Store,
    UnknownToken,
)

MAX_BODY = 1 << 20


class Forbidden(Exception):
    pass


class TooLarge(Exception):
    pass


def load_config(env=None):
    env = os.environ if env is None else env
    tokens = {
        "intake": env.get("BLIND_TOKEN_INTAKE", "intake-dev-token"),
        "custodian": env.get("BLIND_TOKEN_CUSTODIAN", "custodian-dev-token"),
        "reviewer": env.get("BLIND_TOKEN_REVIEWER", "reviewer-dev-token"),
    }
    if len(set(tokens.values())) != len(tokens):
        raise RuntimeError("角色令牌必须互不相同")
    return {
        "roles": {token: role for role, token in tokens.items()},
        "sign_secret": env.get("BLIND_SIGN_SECRET", "dev-only-sign-secret"),
    }


_DEFAULTS = {}


def _default_store():
    # 兼容未显式装配存储的调用方（如探针测试），仅内存运行
    if "store" not in _DEFAULTS:
        _DEFAULTS["store"] = Store(None, sign_secret="dev-only-sign-secret")
    return _DEFAULTS["store"]


def _default_config():
    if "config" not in _DEFAULTS:
        _DEFAULTS["config"] = load_config({})
    return _DEFAULTS["config"]


def _need(body, key):
    if key not in body:
        raise RequestProblem(f"缺少字段 {key}")
    return body[key]


def _health(handler, body):
    return 200, {"status": "ok"}


def _submit(handler, body):
    result = handler._store().submit_document(
        _need(body, "document_id"),
        _need(body, "text"),
        body.get("attachments"),
        body.get("contact"),
    )
    return 201, result


def _supplement(handler, body, document_id):
    result = handler._store().add_revision(document_id, _need(body, "text"), body.get("attachments"))
    return 201, result


def _put_contact(handler, body, document_id):
    return 200, handler._store().put_contact(document_id, _need(body, "channel"), _need(body, "detail"))


def _get_contact(handler, body, document_id):
    return 200, handler._store().get_contact(document_id)


def _delete_contact(handler, body, document_id):
    return 200, handler._store().delete_contact(document_id)


def _list_documents(handler, body):
    return 200, handler._store().list_documents()


def _get_original(handler, body, document_id):
    return 200, handler._store().get_original(document_id)


def _put_policy(handler, body, document_id):
    return 200, handler._store().set_policy(document_id, _need(body, "spans"))


def _post_copy(handler, body, document_id):
    return 201, handler._store().create_copy(document_id)


def _get_copy_trace(handler, body, copy_id):
    return 200, handler._store().get_copy_trace(copy_id)


def _get_audit(handler, body):
    return 200, handler._store().audit_log()


def _get_copy_for_review(handler, body, copy_id):
    return 200, handler._store().get_copy_for_review(copy_id)


def _sign(handler, body, copy_id):
    return 200, handler._store().sign_copy(copy_id, _need(body, "reviewer"))


def _reject(handler, body, copy_id):
    return 200, handler._store().reject_copy(copy_id, _need(body, "reviewer"), body.get("reason"))


def _download(handler, body, token):
    return 200, handler._store().download(token)


ROUTES = [
    ("GET", re.compile(r"/health"), None, _health),
    ("POST", re.compile(r"/intake/documents"), "intake", _submit),
    ("POST", re.compile(r"/intake/documents/(?P<document_id>[^/]+)/revisions"), "intake", _supplement),
    ("PUT", re.compile(r"/intake/documents/(?P<document_id>[^/]+)/contact"), "intake", _put_contact),
    ("GET", re.compile(r"/intake/documents/(?P<document_id>[^/]+)/contact"), "intake", _get_contact),
    ("DELETE", re.compile(r"/intake/documents/(?P<document_id>[^/]+)/contact"), "intake", _delete_contact),
    ("GET", re.compile(r"/custodian/documents"), "custodian", _list_documents),
    ("GET", re.compile(r"/custodian/documents/(?P<document_id>[^/]+)"), "custodian", _get_original),
    ("PUT", re.compile(r"/custodian/documents/(?P<document_id>[^/]+)/policy"), "custodian", _put_policy),
    ("POST", re.compile(r"/custodian/documents/(?P<document_id>[^/]+)/copies"), "custodian", _post_copy),
    ("GET", re.compile(r"/custodian/copies/(?P<copy_id>[^/]+)"), "custodian", _get_copy_trace),
    ("GET", re.compile(r"/custodian/audit"), "custodian", _get_audit),
    ("GET", re.compile(r"/reviewer/copies/(?P<copy_id>[^/]+)"), "reviewer", _get_copy_for_review),
    ("POST", re.compile(r"/reviewer/copies/(?P<copy_id>[^/]+)/sign"), "reviewer", _sign),
    ("POST", re.compile(r"/reviewer/copies/(?P<copy_id>[^/]+)/reject"), "reviewer", _reject),
    ("GET", re.compile(r"/download/(?P<token>[^/]+)"), None, _download),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "BlindMaterial/0.1"

    def log_message(self, *args):
        pass

    def _store(self):
        return getattr(self.server, "store", None) or _default_store()

    def _config(self):
        return getattr(self.server, "config", None) or _default_config()

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            raise RequestProblem("请求体缺失")
        size = int(length)
        if size > MAX_BODY:
            raise TooLarge()
        if size == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestProblem("请求格式错误")
        if not isinstance(data, dict):
            raise RequestProblem("请求格式错误")
        return data

    def _require(self, role):
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if self._config()["roles"].get(token) != role:
            raise Forbidden()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        try:
            status, payload = self._route(method)
        except Forbidden:
            status, payload = 403, {"error": "无权访问"}
        except SpanProblem as exc:
            status, payload = 400, {"error": str(exc)}
        except RequestProblem as exc:
            status, payload = 400, {"error": str(exc)}
        except TooLarge:
            status, payload = 413, {"error": "请求过大"}
        except Missing:
            status, payload = 404, {"error": "材料或副本不存在"}
        except Exists:
            status, payload = 409, {"error": "编号已存在"}
        except UnknownToken:
            status, payload = 404, {"error": "链接不存在"}
        except NotReady:
            status, payload = 403, {"error": "副本尚未复核"}
        except Invalidated:
            status, payload = 410, {"error": "副本已失效"}
        except PolicyStale:
            status, payload = 409, {"error": "遮盖策略与当前原件版本不匹配"}
        except BadState:
            status, payload = 409, {"error": "当前状态不允许该操作"}
        except Exception:
            traceback.print_exc()
            status, payload = 500, {"error": "服务内部错误"}
        self._respond(status, payload)

    def _route(self, method):
        path = self.path.split("?", 1)[0]
        for route_method, pattern, role, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if match is None:
                continue
            if role is not None:
                self._require(role)
            body = None
            if method in ("POST", "PUT") and self.headers.get("Content-Length"):
                body = self._read_body()
            return handler(self, body or {}, **match.groupdict())
        return 404, {"error": "接口不存在"}


def main():
    config = load_config()
    state_path = os.environ.get("BLIND_STATE_PATH", "data/state.json")
    seed_path = os.environ.get("BLIND_SEED_PATH", "data/example.json")
    store = Store(state_path, sign_secret=config["sign_secret"], seed_path=seed_path)
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    server.store = store
    server.config = config
    print(f"监听端口 {server.server_port}，状态文件 {state_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
