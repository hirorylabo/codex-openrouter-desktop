#!/usr/bin/env python3
"""Capture one upstream request body sent by codex exec.

Listens on 127.0.0.1:4399, saves the first request (path + headers + JSON
body) to /tmp/captured-toolwire.json, replies 200 with a minimal chat
completion, then exits. One-shot on purpose.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = "/tmp/captured-toolwire.json"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        record = {
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "body_bytes": len(body),
        }
        try:
            record["body"] = json.loads(body)
        except Exception:
            record["body_raw"] = body.decode("utf-8", "replace")[:5000]
        with open(OUT, "w") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        resp = json.dumps({
            "id": "chatcmpl-capture",
            "object": "chat.completion",
            "created": 0,
            "model": "capture",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "captured"},
            }],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)
        # one-shot: shut down after replying once
        import threading
        threading.Thread(target=self.server.shutdown).start()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("listening on 127.0.0.1:4399 ...")
    HTTPServer(("127.0.0.1", 4399), Handler).serve_forever()
