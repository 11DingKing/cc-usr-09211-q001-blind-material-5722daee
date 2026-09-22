import json
import tempfile
import unittest
from pathlib import Path

from store import (
    BadState,
    Exists,
    Invalidated,
    Missing,
    NotReady,
    PolicyStale,
    Store,
    UnknownToken,
)

TEXT = "李建军在4月12日涂改台账，王芳转了截图"
ATTACHMENTS = [
    {"attachment_id": "a1", "filename": "李建军电话清单.png", "description": "王芳提供的截图"},
]
SPANS = [
    {"field": "text", "start": 0, "end": 3, "label": "当事人"},
    {"field": "text", "start": TEXT.index("王芳"), "end": TEXT.index("王芳") + 2},
    {"field": "attachment_filename", "attachment_id": "a1", "start": 0, "end": 3},
    {"field": "attachment_description", "attachment_id": "a1", "start": 0, "end": 2},
]
CONTACT = {"channel": "手机", "detail": "13800001111"}


def make_store():
    store = Store(None, sign_secret="test-secret")
    store.submit_document("BL-2026-0901", TEXT, ATTACHMENTS, CONTACT)
    store.set_policy("BL-2026-0901", SPANS)
    return store


def token_of(copy_view):
    return copy_view["download_path"].rsplit("/", 1)[1]


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_full_flow_and_contact_isolation(self):
        created = self.store.create_copy("BL-2026-0901")
        self.assertEqual(created["status"], "pending_review")
        signed = self.store.sign_copy(created["copy_id"], "复核员-甲")
        delivered = self.store.download(token_of(signed))
        self.assertEqual((delivered["revision"], delivered["policy_version"]), (1, 1))
        self.assertIn("〔已遮盖〕", delivered["content"]["text"])
        blob = json.dumps(delivered, ensure_ascii=False)
        for secret in ("13800001111", "李建军", "王芳"):
            self.assertNotIn(secret, blob)
        self.assertEqual(delivered["review"]["reviewer"], "复核员-甲")
        # 已领取的副本可以重复取阅
        again = self.store.download(token_of(signed))
        self.assertEqual(again["copy_id"], delivered["copy_id"])

    def test_download_requires_signature(self):
        created = self.store.create_copy("BL-2026-0901")
        self.store.state["tokens"]["fake-token"] = created["copy_id"]
        with self.assertRaises(NotReady):
            self.store.download("fake-token")

    def test_unknown_token(self):
        with self.assertRaises(UnknownToken):
            self.store.download("不存在的令牌")

    def test_contact_kept_only_for_intake(self):
        contact = self.store.get_contact("BL-2026-0901")
        self.assertEqual(contact["contact"]["detail"], "13800001111")
        original_blob = json.dumps(self.store.get_original("BL-2026-0901"), ensure_ascii=False)
        self.assertNotIn("13800001111", original_blob)
        audit_blob = json.dumps(self.store.audit_log(), ensure_ascii=False)
        self.assertNotIn("13800001111", audit_blob)

    def test_contact_update_and_delete(self):
        self.store.put_contact("BL-2026-0901", "邮箱", "example@example.invalid")
        self.assertEqual(
            self.store.get_contact("BL-2026-0901")["contact"]["channel"], "邮箱"
        )
        self.store.delete_contact("BL-2026-0901")
        self.assertIsNone(self.store.get_contact("BL-2026-0901")["contact"])

    def test_supplement_invalidates_unclaimed_copy(self):
        signed = self.store.sign_copy(self.store.create_copy("BL-2026-0901")["copy_id"], "复核员-甲")
        self.store.add_revision("BL-2026-0901", "补充：孙丽也参与了")
        with self.assertRaises(Invalidated):
            self.store.download(token_of(signed))
        trace = self.store.get_copy_trace(signed["copy_id"])
        self.assertEqual(trace["status"], "invalidated")
        self.assertEqual(trace["invalidated_reason"], "原件已补充")

    def test_claimed_copy_survives_supplement(self):
        signed = self.store.sign_copy(self.store.create_copy("BL-2026-0901")["copy_id"], "复核员-甲")
        self.store.download(token_of(signed))
        self.store.add_revision("BL-2026-0901", "补充：孙丽也参与了")
        delivered = self.store.download(token_of(signed))
        self.assertEqual(delivered["copy_id"], signed["copy_id"])

    def test_policy_update_invalidates_unclaimed_copy(self):
        signed = self.store.sign_copy(self.store.create_copy("BL-2026-0901")["copy_id"], "复核员-甲")
        self.store.set_policy("BL-2026-0901", [{"field": "text", "start": 0, "end": 3}])
        with self.assertRaises(Invalidated):
            self.store.download(token_of(signed))

    def test_pending_copy_invalidated_before_review(self):
        created = self.store.create_copy("BL-2026-0901")
        self.store.add_revision("BL-2026-0901", "补充：新内容")
        self.assertEqual(self.store.get_copy_trace(created["copy_id"])["status"], "invalidated")
        with self.assertRaises(BadState):
            self.store.sign_copy(created["copy_id"], "复核员-甲")

    def test_stale_policy_blocks_copy_generation(self):
        self.store.add_revision("BL-2026-0901", "短")
        with self.assertRaises(PolicyStale):
            self.store.create_copy("BL-2026-0901")
        self.store.set_policy("BL-2026-0901", [{"field": "text", "start": 0, "end": 1}])
        created = self.store.create_copy("BL-2026-0901")
        self.assertEqual((created["revision"], created["policy_version"]), (2, 2))

    def test_reject_then_sign_fails(self):
        created = self.store.create_copy("BL-2026-0901")
        self.store.reject_copy(created["copy_id"], "复核员-乙", "遮盖不足")
        with self.assertRaises(BadState):
            self.store.sign_copy(created["copy_id"], "复核员-乙")

    def test_duplicate_document_rejected(self):
        with self.assertRaises(Exists):
            self.store.submit_document("BL-2026-0901", "重复")

    def test_missing_document(self):
        with self.assertRaises(Missing):
            self.store.get_original("BL-0000-0000")


class PersistenceTests(unittest.TestCase):
    def test_restart_keeps_copy_trace_and_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            store = Store(path, sign_secret="s")
            store.submit_document("BL-2026-0902", TEXT, ATTACHMENTS, CONTACT)
            store.set_policy("BL-2026-0902", SPANS)
            signed = store.sign_copy(store.create_copy("BL-2026-0902")["copy_id"], "复核员-乙")

            reopened = Store(path, sign_secret="s")
            trace = reopened.get_copy_trace(signed["copy_id"])
            self.assertEqual((trace["revision"], trace["policy_version"]), (1, 1))
            self.assertEqual(trace["document_id"], "BL-2026-0902")
            delivered = reopened.download(token_of(signed))
            self.assertEqual(delivered["copy_id"], signed["copy_id"])
            self.assertEqual(
                reopened.get_contact("BL-2026-0902")["contact"]["detail"], "13800001111"
            )

    def test_restart_keeps_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            store = Store(path, sign_secret="s")
            store.submit_document("BL-2026-0903", TEXT, ATTACHMENTS, CONTACT)
            store.set_policy("BL-2026-0903", SPANS)
            signed = store.sign_copy(store.create_copy("BL-2026-0903")["copy_id"], "复核员-丙")
            store.add_revision("BL-2026-0903", "补充内容")

            reopened = Store(path, sign_secret="s")
            with self.assertRaises(Invalidated):
                reopened.download(token_of(signed))


class SeedTests(unittest.TestCase):
    def test_seed_from_sample(self):
        sample = Path(__file__).resolve().parents[1] / "data" / "example.json"
        store = Store(None, sign_secret="s", seed_path=str(sample))
        listing = store.list_documents()["documents"]
        ids = [item["document_id"] for item in listing]
        self.assertIn("BL-2026-0417", ids)
        self.assertIn("BL-2026-0422", ids)
        original = store.get_original("BL-2026-0422")
        self.assertEqual([r["revision"] for r in original["revisions"]], [1, 2])
        created = store.create_copy("BL-2026-0417")
        blob = json.dumps(created["content"], ensure_ascii=False)
        self.assertNotIn("李建军", blob)
        self.assertNotIn("王芳", blob)
        self.assertIn("〔已遮盖〕", created["content"]["text"])
        # 样例中的表情保持完整，不被切出半个字符
        self.assertIn("👨‍👩‍👧", created["content"]["text"])
        self.assertIn("👍🏽", created["content"]["text"])


if __name__ == "__main__":
    unittest.main()
