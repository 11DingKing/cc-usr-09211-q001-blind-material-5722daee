import json
from pathlib import Path
import unittest
from contracts import read_records, to_mapping, SourceRecord

class ContractTests(unittest.TestCase):
    def test_sample_roundtrip(self):
        path = Path(__file__).resolve().parents[1] / "data/example.json"
        self.assertEqual([to_mapping(r) for r in read_records(path)], json.loads(path.read_text()))
    def test_missing_fields_rejected(self):
        with self.assertRaises(TypeError):
            SourceRecord()
    def test_health_and_unknown_route(self):
        import threading, urllib.request, urllib.error
        from http.server import ThreadingHTTPServer
        from service import Handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            with urllib.request.urlopen(base + "/health") as response:
                self.assertEqual(json.load(response), {"status": "ok"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/unknown")
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown(); server.server_close(); thread.join()
