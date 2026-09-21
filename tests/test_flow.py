import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import IsolationApp, make_handler
from seed import seed
from store import Store

TOKENS = {
    "intake": "t-intake",
    "custodian": "t-custodian",
    "reviewer": "t-reviewer",
    "investigator": "t-investigator",
}
SIGNING_KEY = "test-signing-key"

CONTACT_VALUE = "13800001111"


def span(text, sub, occurrence=1):
    pos = -1
    for _ in range(occurrence):
        pos = text.index(sub, pos + 1)
    return [pos, pos + len(sub)]


class Server:
    def __init__(self, app):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(lambda: app))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


def call(base, method, path, role=None, payload=None, raw_token=None):
    request = urllib.request.Request(base + path, method=method)
    if raw_token is not None:
        request.add_header("Authorization", "Bearer " + raw_token)
    elif role is not None:
        request.add_header("Authorization", "Bearer " + TOKENS[role])
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data=data) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "isolation.db")
        self.store = Store(self.db_path)
        self.app = IsolationApp(self.store, TOKENS, SIGNING_KEY)
        self.server = Server(self.app)

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, **kwargs):
        return call(self.server.base, method, path, **kwargs)

    def submit_sample(self):
        text = ("举报人周某称，八月三日晚仓库管理员吴某私自放行两辆货车，"
                "现场照片显示车牌被遮挡。周某联系电话13800001111，微信同号。")
        payload = {
            "document_id": "BL-2026-1001",
            "text": text,
            "spans": [
                span(text, "周某"),                      # 第一次出现
                span(text, "周某", occurrence=2),        # 第二次出现
                span(text, "吴某"),
                span(text, "13800001111"),
                # 与第二个“周某”区间重叠的额外区间，验证一次遮盖覆盖重叠区间
                [span(text, "周某", occurrence=2)[0] - 1,
                 span(text, "周某", occurrence=2)[1] + 2],
            ],
            "attachments": [
                {
                    "filename": "吴某值班表.pdf",
                    "description": "周某提供的现场照片说明",
                    "filename_spans": [[0, 2]],
                    "description_spans": [[0, 2]],
                }
            ],
            "contact": {"channel": "电话", "value": CONTACT_VALUE},
        }
        status, body = self.call("POST", "/intake/submissions", role="intake",
                                 payload=payload)
        self.assertEqual(status, 201, body)
        return body


class FullFlowTests(FlowTestCase):
    def test_receive_redact_review_deliver(self):
        submitted = self.submit_sample()
        self.assertEqual(submitted["document_id"], "BL-2026-1001")
        self.assertEqual(submitted["revision"], 1)
        submitter_ref = submitted["submitter_ref"]
        self.assertTrue(submitter_ref)

        # 原件只对独立保管员开放
        status, original = self.call("GET", "/custodian/originals/BL-2026-1001",
                                     role="custodian")
        self.assertEqual(status, 200)
        self.assertIn(CONTACT_VALUE, original["text"])  # 保管员可见原文
        for role in ("intake", "reviewer", "investigator"):
            status, _ = self.call("GET", "/custodian/originals/BL-2026-1001", role=role)
            self.assertEqual(status, 403, role)

        # 生成去标识副本：正文、附件说明、文件名都被遮盖
        status, copy_info = self.call(
            "POST", "/custodian/originals/BL-2026-1001/copies", role="custodian",
            payload={})
        self.assertEqual(status, 201, copy_info)
        copy_id = copy_info["copy_id"]
        self.assertEqual(copy_info["state"], "draft")

        # 未复核签名前不得交付
        status, _ = self.call("POST", f"/investigator/copies/{copy_id}/link",
                              role="investigator", payload={})
        self.assertEqual(status, 404)
        status, listing = self.call("GET", "/investigator/copies", role="investigator")
        self.assertEqual(listing, {"copies": []})

        # 复核员查看去标识内容并签名
        status, review = self.call("GET", f"/reviewer/copies/{copy_id}", role="reviewer")
        self.assertEqual(status, 200)
        content = review["content"]
        self.assertIn("私自放行两辆货车", content["text"])  # 调查组需要知道发生了什么
        for leaked in ("周某", "吴某", CONTACT_VALUE):
            self.assertNotIn(leaked, content["text"])
        self.assertEqual(content["attachments"][0]["filename"], "█值班表.pdf")
        self.assertEqual(content["attachments"][0]["description"], "█提供的现场照片说明")

        status, signed = self.call("POST", f"/reviewer/copies/{copy_id}/sign",
                                   role="reviewer", payload={})
        self.assertEqual(status, 200, signed)
        self.assertTrue(signed["signature"])

        # 签名后出现在调查组列表，可签发下载链接
        status, listing = self.call("GET", "/investigator/copies", role="investigator")
        self.assertEqual([c["copy_id"] for c in listing["copies"]], [copy_id])
        status, link = self.call("POST", f"/investigator/copies/{copy_id}/link",
                                 role="investigator", payload={})
        self.assertEqual(status, 201, link)
        self.assertTrue(link["url"].startswith("/download/"))

        status, delivered = self.call("GET", link["url"], role="investigator")
        self.assertEqual(status, 200, delivered)
        self.assertEqual(delivered["revision"], 1)
        self.assertEqual(delivered["signature"]["signed_by"], "reviewer")
        self.assertNotIn(CONTACT_VALUE, json.dumps(delivered, ensure_ascii=False))

        # 一次性链接，核销后不可再用
        status, _ = self.call("GET", link["url"], role="investigator")
        self.assertEqual(status, 404)

        # 访问记录可见且不含联系方式
        status, access = self.call("GET", f"/investigator/copies/{copy_id}/access",
                                   role="investigator")
        self.assertEqual(status, 200)
        actions = [r["action"] for r in access["records"]]
        self.assertEqual(actions, ["copy_generated", "copy_signed", "link_issued",
                                   "copy_downloaded"])
        self.assertNotIn(CONTACT_VALUE, json.dumps(access, ensure_ascii=False))
        self.assertNotIn(submitter_ref, json.dumps(access, ensure_ascii=False))

        # 接收窗口保留回访方式；调查员不能反查提交者
        status, callback = self.call("GET", f"/intake/callbacks/{submitter_ref}",
                                     role="intake")
        self.assertEqual(status, 200)
        self.assertEqual(callback["value"], CONTACT_VALUE)
        status, denied = self.call("GET", f"/intake/callbacks/{submitter_ref}",
                                   role="investigator")
        self.assertEqual(status, 403)
        self.assertNotIn(CONTACT_VALUE, json.dumps(denied, ensure_ascii=False))

    def test_supplement_invalidates_unclaimed_copies(self):
        self.submit_sample()
        _, copy1 = self.call("POST", "/custodian/originals/BL-2026-1001/copies",
                             role="custodian", payload={})
        _, copy2 = self.call("POST", "/custodian/originals/BL-2026-1001/copies",
                             role="custodian", payload={})
        self.call("POST", f"/reviewer/copies/{copy1['copy_id']}/sign", role="reviewer",
                  payload={})
        self.call("POST", f"/reviewer/copies/{copy2['copy_id']}/sign", role="reviewer",
                  payload={})
        # copy1 先被领取
        _, link = self.call("POST", f"/investigator/copies/{copy1['copy_id']}/link",
                            role="investigator", payload={})
        status, _ = self.call("GET", link["url"], role="investigator")
        self.assertEqual(status, 200)
        # copy2 已签名未领取，另备一个链接稍后验证失效
        _, dead_link = self.call("POST", f"/investigator/copies/{copy2['copy_id']}/link",
                                 role="investigator", payload={})

        # 原件补充 → 新版本
        supplemented = ("举报人周某称，八月三日晚仓库管理员吴某私自放行两辆货车，"
                        "现场照片显示车牌被遮挡。周某联系电话13800001111，微信同号。"
                        "补充：当晚监控主机时间被调快十分钟。")
        status, revision = self.call(
            "POST", "/custodian/originals/BL-2026-1001/revisions", role="custodian",
            payload={"text": supplemented,
                     "spans": [span(supplemented, "周某"),
                               span(supplemented, "周某", occurrence=2),
                               span(supplemented, "吴某"),
                               span(supplemented, "13800001111")]})
        self.assertEqual(status, 201, revision)
        self.assertEqual(revision["revision"], 2)
        self.assertEqual(revision["invalidated"], [copy2["copy_id"]])

        # 未领取的旧副本与其链接一起失效
        status, _ = self.call("GET", dead_link["url"], role="investigator")
        self.assertEqual(status, 404)
        status, _ = self.call("POST", f"/investigator/copies/{copy2['copy_id']}/link",
                              role="investigator", payload={})
        self.assertEqual(status, 410)
        # 已领取的副本不受影响，仍可再次取阅
        status, link = self.call("POST", f"/investigator/copies/{copy1['copy_id']}/link",
                                 role="investigator", payload={})
        self.assertEqual(status, 201)
        status, delivered = self.call("GET", link["url"], role="investigator")
        self.assertEqual(status, 200)
        self.assertEqual(delivered["revision"], 1)

        # 新副本来自新版本，包含补充内容
        _, copy3 = self.call("POST", "/custodian/originals/BL-2026-1001/copies",
                             role="custodian", payload={})
        self.assertEqual(copy3["revision"], 2)
        status, review = self.call("GET", f"/reviewer/copies/{copy3['copy_id']}",
                                   role="reviewer")
        self.assertIn("监控主机时间被调快十分钟", review["content"]["text"])

    def test_policy_update_invalidates_unclaimed_copies(self):
        self.submit_sample()
        _, copy = self.call("POST", "/custodian/originals/BL-2026-1001/copies",
                            role="custodian", payload={})
        status, revision = self.call(
            "POST", "/custodian/originals/BL-2026-1001/revisions", role="custodian",
            payload={"spans": [[0, 3]]})  # 只更新遮盖策略
        self.assertEqual(status, 201, revision)
        self.assertEqual(revision["revision"], 2)
        self.assertEqual(revision["invalidated"], [copy["copy_id"]])
        # 策略更新后复核旧副本被拒绝
        status, _ = self.call("POST", f"/reviewer/copies/{copy['copy_id']}/sign",
                              role="reviewer", payload={})
        self.assertEqual(status, 409)
        # 新策略立即生效
        _, copy2 = self.call("POST", "/custodian/originals/BL-2026-1001/copies",
                             role="custodian", payload={})
        status, review = self.call("GET", f"/reviewer/copies/{copy2['copy_id']}",
                                   role="reviewer")
        self.assertTrue(review["content"]["text"].startswith("█"))

    def test_restart_keeps_copy_to_revision_mapping(self):
        self.submit_sample()
        _, copy = self.call("POST", "/custodian/originals/BL-2026-1001/copies",
                            role="custodian", payload={})
        self.call("POST", f"/reviewer/copies/{copy['copy_id']}/sign", role="reviewer",
                  payload={})
        self.server.stop()
        self.store.close()

        # 模拟重启：同一数据库文件重新建应用
        self.store = Store(self.db_path)
        self.app = IsolationApp(self.store, TOKENS, SIGNING_KEY)
        self.server = Server(self.app)
        status, meta = self.call("GET", f"/custodian/copies/{copy['copy_id']}",
                                 role="custodian")
        self.assertEqual(status, 200)
        self.assertEqual(meta["document_id"], "BL-2026-1001")
        self.assertEqual(meta["revision"], 1)
        self.assertEqual(meta["state"], "signed")
        # 重启后签名仍可验真、副本仍可交付
        _, link = self.call("POST", f"/investigator/copies/{copy['copy_id']}/link",
                            role="investigator", payload={})
        status, delivered = self.call("GET", link["url"], role="investigator")
        self.assertEqual(status, 200)
        self.assertEqual(delivered["revision"], 1)

    def test_emoji_and_overlap_via_api(self):
        text = "证人甲（家属👨‍👩‍👧）反映情况属实。"
        # 区间右端落在 ZWJ 序列中间，验证不露出半个表情
        start = text.index("（")
        end = text.index("👩") + 1
        status, submitted = self.call(
            "POST", "/intake/submissions", role="intake",
            payload={"document_id": "BL-2026-1002", "text": text,
                     "spans": [[start, end]]})
        self.assertEqual(status, 201, submitted)
        _, copy = self.call("POST", "/custodian/originals/BL-2026-1002/copies",
                            role="custodian", payload={})
        status, review = self.call("GET", f"/reviewer/copies/{copy['copy_id']}",
                                   role="reviewer")
        redacted = review["content"]["text"]
        self.assertEqual(redacted, "证人甲█）反映情况属实。")
        self.assertNotIn("‍", redacted)
        self.assertNotIn("👩", redacted)

    def test_auth_and_unknown_routes(self):
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))
        status, body = self.call("GET", "/unknown")
        self.assertEqual((status, body), (404, {"error": "接口不存在"}))
        status, _ = self.call("GET", "/custodian/copies")
        self.assertEqual(status, 401)                      # 无令牌
        status, _ = self.call("GET", "/custodian/copies", raw_token="wrong")
        self.assertEqual(status, 401)                      # 令牌错误
        status, _ = self.call("GET", "/custodian/copies", role="investigator")
        self.assertEqual(status, 403)                      # 角色不符
        status, _ = self.call("POST", "/intake/submissions", role="custodian",
                              payload={})
        self.assertEqual(status, 403)

    def test_investigator_never_sees_contact(self):
        submitted = self.submit_sample()
        submitter_ref = submitted["submitter_ref"]
        _, copy = self.call("POST", "/custodian/originals/BL-2026-1001/copies",
                            role="custodian", payload={})
        self.call("POST", f"/reviewer/copies/{copy['copy_id']}/sign", role="reviewer",
                  payload={})
        _, link = self.call("POST", f"/investigator/copies/{copy['copy_id']}/link",
                            role="investigator", payload={})
        responses = [
            self.call("GET", "/investigator/copies", role="investigator"),
            self.call("GET", link["url"], role="investigator"),
            self.call("GET", f"/investigator/copies/{copy['copy_id']}/access",
                      role="investigator"),
            self.call("GET", f"/intake/callbacks/{submitter_ref}", role="investigator"),
            self.call("GET", "/download/not-a-token", role="investigator"),
        ]
        for status, body in responses:
            blob = json.dumps(body, ensure_ascii=False)
            self.assertNotIn(CONTACT_VALUE, blob, (status, body))
            self.assertNotIn(submitter_ref, blob, (status, body))


class SeedFlowTests(FlowTestCase):
    def test_seed_sample_then_full_flow(self):
        sample = Path(__file__).resolve().parents[1] / "data" / "example.json"
        seeded = seed(self.store, sample)
        self.assertEqual(seeded, ["BL-2026-0001", "BL-2026-0002"])

        # 沿用样例中的原件编号与片段坐标生成副本
        _, copy = self.call("POST", "/custodian/originals/BL-2026-0001/copies",
                            role="custodian", payload={})
        self.assertEqual(copy["revision"], 2)  # 样例含两个版本，取最新
        status, review = self.call("GET", f"/reviewer/copies/{copy['copy_id']}",
                                   role="reviewer")
        text = review["content"]["text"]
        self.assertIn("巡检记录被人补登", text)
        self.assertIn("监控离线🚨", text)
        for leaked in ("林某某", "杜某"):
            self.assertNotIn(leaked, text)

        # 样例中的 ZWJ 表情整簇遮盖
        _, copy2 = self.call("POST", "/custodian/originals/BL-2026-0002/copies",
                             role="custodian", payload={})
        status, review2 = self.call("GET", f"/reviewer/copies/{copy2['copy_id']}",
                                    role="reviewer")
        text2 = review2["content"]["text"]
        self.assertNotIn("陈某", text2)
        self.assertNotIn("‍", text2)
        self.assertIn("验收单编号YC-0618", text2)

        # 复核签名后交付，走通样例原件的完整链路
        self.call("POST", f"/reviewer/copies/{copy['copy_id']}/sign", role="reviewer",
                  payload={})
        _, link = self.call("POST", f"/investigator/copies/{copy['copy_id']}/link",
                            role="investigator", payload={})
        status, delivered = self.call("GET", link["url"], role="investigator")
        self.assertEqual(status, 200)
        self.assertEqual(delivered["document_id"], "BL-2026-0001")
        self.assertEqual(delivered["revision"], 2)


if __name__ == "__main__":
    unittest.main()
