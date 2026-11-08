#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假的 llama-server 可执行文件（配合 llama_tui 的「按 R 运行」流程测试）。

用法（llama_tui 会这样调用）:
  fake_llama_server.py -m <model> --host 127.0.0.1 --port 8080 -c 4096 -ngl 99 ...

它会像真的 llama-server 一样打印加载日志，然后在本进程里起 HTTP 服务。
"""
import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"model": "", "ctx": 4096}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":
            self._send(200, '{"status":"ok"}')
        elif p == "/props":
            self._send(200, json.dumps({
                "model_path": STATE["model"],
                "total_slots": 1,
                "default_generation_settings": {"n_ctx": STATE["ctx"], "n_past": 0},
            }))
        elif p == "/slots":
            self._send(200, json.dumps([{"id": 0, "is_processing": False, "n_past": 0}]))
        elif p == "/metrics":
            self._send(200, "llamacpp:predicted_tokens_seconds 41.5\n"
                            "llamacpp:tokens_predicted_total 100\n", "text/plain")
        elif p == "/v1/models":
            self._send(200, '{"object":"list","data":[{"id":"local"}]}')
        else:
            self._send(404, '{"error":"nf"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._send(200, json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model", default="model.gguf")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("-c", "--ctx", type=int, default=4096)
    ap.add_argument("-ngl", "--ngl", default="99")
    ap.add_argument("-b", "--batch", default="512")
    ap.add_argument("-ub", "--ubatch", default="512")
    ap.add_argument("-fa", "--flash-attn", default="on")
    ap.add_argument("-np", "--parallel", default="1")
    ap.add_argument("-t", "--threads", default="0")
    ap.add_argument("--metrics", action="store_true")
    a, _unknown = ap.parse_known_args()

    STATE["model"] = a.model
    STATE["ctx"] = a.ctx

    # 模仿 llama.cpp 的启动输出
    print("llama_model_loader: loaded meta data with 24 key-value pairs", flush=True)
    print("llama_model_loader: - kv   0: general.architecture str = llama", flush=True)
    time.sleep(0.3)
    total = 32
    for i in range(1, total + 1):
        print("load_tensors: loading model tensors, this can take a while... %d/%d" % (i, total),
              flush=True)
        time.sleep(0.05)
    print("llama_model_load: model loaded", flush=True)
    print("llama_new_context_with_model: n_ctx = %d" % a.ctx, flush=True)
    print("main: model loaded", flush=True)

    srv = ThreadingHTTPServer((a.host, a.port), H)
    print("main: server is listening on http://%s:%d - starting the main loop" % (a.host, a.port),
          flush=True)
    print("srv  update_slots: all slots are idle", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
