#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假的 llama-server，用于联调 llama_tui 的发现/探测/流式任务。"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "C:/models/Qwen3-14B-Instruct-Q4_K_M.gguf"
CTX = 16384


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/props":
            self._json({
                "model_path": MODEL,
                "total_slots": 4,
                "default_generation_settings": {"n_ctx": CTX, "n_past": 3420, "n_predict": -1},
            })
        elif p == "/slots":
            self._json([
                {"id": 0, "is_processing": True, "n_past": 3420, "next_token": {"n_decoded": 233}},
                {"id": 1, "is_processing": False, "n_past": 0},
                {"id": 2, "is_processing": False, "n_past": 0},
                {"id": 3, "is_processing": False, "n_past": 0},
            ])
        elif p == "/metrics":
            body = (
                "# HELP llamacpp:predicted_tokens_seconds\n"
                "llamacpp:predicted_tokens_seconds 33.17\n"
                "llamacpp:prompt_tokens_seconds 3316.4\n"
                "llamacpp:tokens_predicted_total 1234\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/v1/models":
            self._json({"object": "list", "data": [{"id": "gpt-3.5-turbo"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        body = json.loads(raw or b"{}")
        stream = bool(body.get("stream"))
        parts = ["滚动数组", "的关键是", "复用同一行", "，倒序枚举即可。", "\n\n复杂度 O(nV)。"]
        if not stream:
            self._json({
                "choices": [{"message": {"role": "assistant", "content": "".join(parts)},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 42, "completion_tokens": len(parts)},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(data: bytes):
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()

        for i, piece in enumerate(parts):
            obj = {"choices": [{"delta": {"content": piece}, "index": 0}]}
            chunk(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            time.sleep(0.35)
        usage = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 42, "completion_tokens": len(parts)}}
        chunk(("data: " + json.dumps(usage) + "\n\n").encode())
        chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print("fake llama-server on http://127.0.0.1:%d" % a.port, flush=True)
    srv.serve_forever()
