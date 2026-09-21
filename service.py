from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        healthy = self.path == "/health"
        body = json.dumps({"status": "ok"} if healthy else {"error": "接口不存在"}, ensure_ascii=False).encode()
        self.send_response(200 if healthy else 404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
