import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from service import Handler, load_config
from store import Store

TOKENS = {"intake": "tok-intake", "custodian": "tok-custodian", "reviewer": "tok-reviewer"}
ENV = {
    "BLIND_TOKEN_INTAKE": TOKENS["intake"],
    "BLIND_TOKEN_CUSTODIAN": TOKENS["custodian"],
    "BLIND_TOKEN_REVIEWER": TOKENS["reviewer"],
    "BLIND_SIGN_SECRET": "svc-secret",
}

TEXT = "李雷在5月把台账照片发给了韩梅梅，群里回复了👍🏽"
ATTACHMENTS = [
    {"attachment_id": "a1", "filename": "李雷的台账照片.png", "description": "韩梅梅提供的原件照片"},
]
CONTACT = {"channel": "手机", "detail": "13800001111"}
SPANS = [
    {"field": "text", "start": 0, "end": 2, "label": "当事人"},
    {"field": "text", "start": TEXT.index("韩梅梅"), "end": TEXT.index("韩梅梅") + 3},
    {"field": "attachment_filename", "attachment_id": "a1", "start": 0, "end": 2},
    {"field": "attachment_description", "attachment_id": "a1", "start": 0, "end": 3},
]
SECRETS = ("13800001111", "李雷", "韩梅梅")


def start_server(store):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.store = store
    server.config = load_config(ENV)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def call(server, method, path, body=None, role=None):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if role is not None:
        headers["Authorization"] = "Bearer " + TOKENS[role]
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.server, self.thread = start_server(Store(None, sign_secret="svc-secret"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, *args, **kwargs):
        return call(self.server, *args, **kwargs)

    def submit_and_prepare(self):
        status, _ = self.call("POST", "/intake/documents", {
            "document_id": "BL-2026-0901",
            "text": TEXT,
            "attachments": ATTACHMENTS,
            "contact": CONTACT,
        }, role="intake")
        self.assertEqual(status, 201)
        status, _ = self.call("PUT", "/custodian/documents/BL-2026-0901/policy", {"spans": SPANS}, role="custodian")
        self.assertEqual(status, 200)
        status, created = self.call("POST", "/custodian/documents/BL-2026-0901/copies", role="custodian")
        self.assertEqual(status, 201)
        return created["copy_id"]

    def sign(self, copy_id):
        status, signed = self.call("POST", f"/reviewer/copies/{copy_id}/sign", {"reviewer": "复核员-甲"}, role="reviewer")
        self.assertEqual(status, 200)
        return signed

    def test_full_flow_without_contact_leak(self):
        copy_id = self.submit_and_prepare()
        responses = []

        status, original = self.call("GET", "/custodian/documents/BL-2026-0901", role="custodian")
        self.assertEqual(status, 200)
        self.assertIn("李雷", json.dumps(original, ensure_ascii=False))

        status, review_view = self.call("GET", f"/reviewer/copies/{copy_id}", role="reviewer")
        responses.append(review_view)
        self.assertEqual(status, 200)
        self.assertEqual(review_view["status"], "pending_review")

        signed = self.sign(copy_id)
        responses.append(signed)
        download_path = signed["download_path"]
        self.assertTrue(download_path.startswith("/download/"))

        # 调查员无需令牌，凭链接取阅
        status, delivered = self.call("GET", download_path)
        responses.append(delivered)
        self.assertEqual(status, 200)
        self.assertEqual((delivered["revision"], delivered["policy_version"]), (1, 1))
        self.assertIn("〔已遮盖〕", delivered["content"]["text"])
        self.assertEqual(delivered["review"]["reviewer"], "复核员-甲")

        # 访问记录可供保管员查阅
        status, audit = self.call("GET", "/custodian/audit", role="custodian")
        responses.append(audit)
        self.assertEqual(status, 200)
        self.assertTrue(any(e["action"] == "copy.download" for e in audit["entries"]))

        # 副本追踪视图
        status, trace = self.call("GET", f"/custodian/copies/{copy_id}", role="custodian")
        responses.append(trace)
        self.assertEqual(trace["status"], "signed")
        self.assertIsNotNone(trace["claimed_at"])

        # 以上响应一律不得带出联系方式或被遮盖的人名
        for payload in responses:
            blob = json.dumps(payload, ensure_ascii=False)
            for secret in SECRETS:
                self.assertNotIn(secret, blob)

        # 回访方式仅接收窗口可见
        status, contact = self.call("GET", "/intake/documents/BL-2026-0901/contact", role="intake")
        self.assertEqual(status, 200)
        self.assertEqual(contact["contact"]["detail"], "13800001111")

    def test_role_isolation(self):
        self.submit_and_prepare()
        # 无令牌
        for path in ("/custodian/documents/BL-2026-0901", "/intake/documents/BL-2026-0901/contact", "/custodian/audit"):
            status, payload = self.call("GET", path)
            self.assertEqual(status, 403)
            self.assertEqual(payload, {"error": "无权访问"})
        # 角色混用：接收窗口也不能读原件，保管员也不能看回访方式
        status, _ = self.call("GET", "/custodian/documents/BL-2026-0901", role="intake")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/intake/documents/BL-2026-0901/contact", role="custodian")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/custodian/audit", role="reviewer")
        self.assertEqual(status, 403)

    def test_supplement_invalidates_unclaimed_copy(self):
        copy_id = self.submit_and_prepare()
        signed = self.sign(copy_id)
        new_text = "补充：还有新情况，孙丽也参与了"
        status, _ = self.call("POST", "/intake/documents/BL-2026-0901/revisions", {"text": new_text}, role="intake")
        self.assertEqual(status, 201)
        status, payload = self.call("GET", signed["download_path"])
        self.assertEqual(status, 410)
        self.assertEqual(payload, {"error": "副本已失效"})
        self.assertNotIn("13800001111", json.dumps(payload, ensure_ascii=False))
        # 补充后旧策略与新版本不匹配，需先更新策略
        status, _ = self.call("POST", "/custodian/documents/BL-2026-0901/copies", role="custodian")
        self.assertEqual(status, 409)
        spans = [{"field": "text", "start": new_text.index("孙丽"), "end": new_text.index("孙丽") + 2}]
        status, _ = self.call("PUT", "/custodian/documents/BL-2026-0901/policy", {"spans": spans}, role="custodian")
        self.assertEqual(status, 200)
        # 重新生成并复核后可正常取阅
        status, created = self.call("POST", "/custodian/documents/BL-2026-0901/copies", role="custodian")
        self.assertEqual(status, 201)
        signed = self.sign(created["copy_id"])
        status, delivered = self.call("GET", signed["download_path"])
        self.assertEqual(status, 200)
        self.assertEqual((delivered["revision"], delivered["policy_version"]), (2, 2))
        self.assertNotIn("孙丽", json.dumps(delivered, ensure_ascii=False))

    def test_policy_update_invalidates_unclaimed_copy(self):
        copy_id = self.submit_and_prepare()
        signed = self.sign(copy_id)
        status, _ = self.call("PUT", "/custodian/documents/BL-2026-0901/policy",
                              {"spans": [{"field": "text", "start": 0, "end": 2}]}, role="custodian")
        self.assertEqual(status, 200)
        status, _ = self.call("GET", signed["download_path"])
        self.assertEqual(status, 410)

    def test_download_requires_signature(self):
        copy_id = self.submit_and_prepare()
        # 未签名时没有下载链接；伪造链接一律 404
        status, payload = self.call("GET", "/download/not-a-real-token")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "链接不存在"})

    def test_stale_policy_blocks_copy(self):
        self.submit_and_prepare()
        status, _ = self.call("POST", "/intake/documents/BL-2026-0901/revisions", {"text": "短"}, role="intake")
        self.assertEqual(status, 201)
        status, payload = self.call("POST", "/custodian/documents/BL-2026-0901/copies", role="custodian")
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": "遮盖策略与当前原件版本不匹配"})

    def test_contact_lifecycle(self):
        self.submit_and_prepare()
        status, _ = self.call("PUT", "/intake/documents/BL-2026-0901/contact",
                              {"channel": "邮箱", "detail": "callback@example.invalid"}, role="intake")
        self.assertEqual(status, 200)
        status, contact = self.call("GET", "/intake/documents/BL-2026-0901/contact", role="intake")
        self.assertEqual(contact["contact"]["channel"], "邮箱")
        status, _ = self.call("DELETE", "/intake/documents/BL-2026-0901/contact", role="intake")
        self.assertEqual(status, 200)
        status, contact = self.call("GET", "/intake/documents/BL-2026-0901/contact", role="intake")
        self.assertIsNone(contact["contact"])

    def test_unknown_route_and_health(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        status, payload = self.call("GET", "/unknown")
        self.assertEqual((status, payload), (404, {"error": "接口不存在"}))

    def test_error_messages_carry_no_contact(self):
        self.submit_and_prepare()
        probes = [
            ("GET", "/custodian/documents/BL-0000-0000", "custodian"),
            ("GET", "/reviewer/copies/CP-0000000000000000", "reviewer"),
            ("POST", "/intake/documents", "intake"),
        ]
        for method, path, role in probes:
            _, payload = self.call(method, path, role=role)
            blob = json.dumps(payload, ensure_ascii=False)
            for secret in SECRETS:
                self.assertNotIn(secret, blob)


class RestartTests(unittest.TestCase):
    def test_restart_keeps_trace_and_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            server, thread = start_server(Store(state_path, sign_secret="svc-secret"))
            try:
                call(server, "POST", "/intake/documents", {
                    "document_id": "BL-2026-0902", "text": TEXT,
                    "attachments": ATTACHMENTS, "contact": CONTACT,
                }, role="intake")
                call(server, "PUT", "/custodian/documents/BL-2026-0902/policy", {"spans": SPANS}, role="custodian")
                _, created = call(server, "POST", "/custodian/documents/BL-2026-0902/copies", role="custodian")
                _, signed = call(server, "POST", f"/reviewer/copies/{created['copy_id']}/sign",
                                 {"reviewer": "复核员-乙"}, role="reviewer")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

            # 重启：从同一状态文件恢复
            server, thread = start_server(Store(state_path, sign_secret="svc-secret"))
            try:
                status, trace = call(server, "GET", f"/custodian/copies/{created['copy_id']}", role="custodian")
                self.assertEqual(status, 200)
                self.assertEqual((trace["revision"], trace["policy_version"]), (1, 1))
                self.assertEqual(trace["document_id"], "BL-2026-0902")
                status, delivered = call(server, "GET", signed["download_path"])
                self.assertEqual(status, 200)
                self.assertEqual(delivered["copy_id"], created["copy_id"])
                blob = json.dumps(delivered, ensure_ascii=False)
                for secret in SECRETS:
                    self.assertNotIn(secret, blob)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
