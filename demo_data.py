#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示数据：给 llama_tui 的界面灌假数据，用来预览 / 截图 / 调布局。

和主程序的关系：
    import demo_data
    demo_data.populate(tui)     # tui 是 LlamaTUI 实例

想改演示内容（换模型名、改任务、加日志）直接改这个文件，不碰 llama_tui.py。
主程序只在 `--demo` 和 `--dump-frame --demo` 时才 import 它，正常运行不会加载。
"""

import os

import llama_tui as L


# ── 演示用的后端 ──
BACKENDS = [
    {
        "url": "http://127.0.0.1:8080",
        "name": "llama-server:8080",
        "pid": 41232,
        "model": "C:/models/Qwen3-14B-Instruct-Q4_K_M.gguf",
        "n_ctx": 16384,
        "n_past": 3420,
        "slots": 4,
        "busy": 1,
        "tps": 33.2,
        "mem": 9.8 * 1024 ** 3,
        "uptime": 3720,
        "cmd": "llama-server.exe -m C:/models/Qwen3-14B-Instruct-Q4_K_M.gguf "
               "-c 16384 -ngl 99 --port 8080",
        "cwd": "C:/llama.cpp",
    },
    {
        "url": "http://127.0.0.1:8081",
        "name": "llama-server:8081",
        "pid": 49900,
        "model": "C:/models/llama-3.2-3B-Q8_0.gguf",
        "n_ctx": 8192,
        "n_past": 0,
        "slots": 2,
        "busy": 0,
        "tps": None,
        "mem": None,
        "uptime": 900,
        "cmd": "",
        "cwd": "",
    },
]

LLAMA_SERVER = "C:/llama.cpp/build/bin/llama-server.exe"

# ── 演示用的模型列表 ──
# (名字, 大小, 是否在跑, 属于哪个后端)
MODELS = [
    ("Qwen2.5-Coder-7B-Q5_K_M.gguf", 5.2 * 1024 ** 3, False, ""),
]

# ── 演示用的任务 ──
TASKS = [
    {
        "prompt": "把 01 背包改成滚动数组并解释复杂度",
        "model": "Qwen3-14B-Instruct-Q4_K_M.gguf",
        "backend": 0,
        "max_tokens": 512,
        "state": "运行",
        "ago": 41,
        "ntok": 233,
        "out": (
            "滚动数组的关键是第一维可以由大到小复用：\n\n"
            "for (int i = 0; i < n; ++i)\n"
            "  for (int j = V; j >= w[i]; --j)\n"
            "    dp[j] = max(dp[j], dp[j - w[i]] + v[i]);\n\n"
            "复杂度 O(nV)，空间 O(V)。之所以倒序，是因为内层要用上一轮的值。"
        ),
    },
    {
        "prompt": "写个探活脚本",
        "model": "llama-3.2-3B-Q8_0.gguf",
        "backend": 1,
        "max_tokens": 128,
        "state": "完成",
        "ago": 300,
        "dur": 8,
        "ntok": 46,
        "tps": 5.8,
        "out": "curl -s http://127.0.0.1:8080/health",
    },
    {
        "prompt": "解释 KV cache 量化",
        "model": "Qwen3-14B-Instruct-Q4_K_M.gguf",
        "backend": 0,
        "max_tokens": 0,
        "state": "失败",
        "ago": 600,
        "err": "HTTP 503: loading model",
        "out": "",
    },
]

# ── 演示用的日志 ──
LOGS = [
    ("find", "ok", "llama-server: " + LLAMA_SERVER),
    ("find", "info", "扫到 12 个 .gguf 模型"),
    ("find", "info", "发现 2 个 llama 服务: 8080, 8081"),
    ("server", "info", "main: server is listening on http://127.0.0.1:8080 - starting the main loop"),
    ("server", "info", "srv  update_slots: all slots are idle"),
    ("server", "info", "prompt eval time = 1234.56 ms / 4096 tokens ( 0.30 ms/token, 3316 tok/s)"),
    ("server", "warn", "slot launch_slot_: id 0 | task 2331 | processing task"),
    ("task", "info", "提交任务 #1 → http://127.0.0.1:8080"),
    ("task", "ok", "任务 #2 完成 · 46 tok · 5.8 t/s"),
    ("server", "err", "srv  send_error: task id mismatch, got 2331, expected 2330"),
    ("tui", "info", "窗口 130x38"),
]


def build_backend(spec):
    """把一条后端描述变成 L.Backend。"""
    b = L.Backend(spec["url"], spec["name"], pid=spec.get("pid"), source="scan")
    b.online = True
    b.health = "ok"
    b.model_path = spec["model"]
    b.n_ctx = spec["n_ctx"]
    b.props = {"default_generation_settings": {"n_ctx": spec["n_ctx"],
                                              "n_past": spec.get("n_past", 0)}}
    n = spec.get("slots", 0)
    if n:
        busy = spec.get("busy", 0)
        b.slots = [{"is_processing": i < busy} for i in range(n)]
        b.slots_busy = busy
    b.tps = spec.get("tps")
    b.mem = spec.get("mem")
    if spec.get("uptime"):
        b.start_time = L.now() - spec["uptime"]
    b.cmdline = spec.get("cmd", "")
    b.cwd = spec.get("cwd", "")
    return b


def build_task(spec, backends):
    b = backends[spec["backend"]] if spec.get("backend") is not None else None
    t = L.Task(spec["prompt"], spec["model"],
               b.base_url if b else "", max_tokens=spec.get("max_tokens", 0))
    t.state = spec["state"]
    t.start = L.now() - spec.get("ago", 0)
    if spec["state"] in ("完成", "失败", "取消"):
        t.end = t.start + spec.get("dur", 0)
    t.completion_tokens = spec.get("ntok", 0)
    if spec.get("tps"):
        t.tps = spec["tps"]
    if spec.get("err"):
        t.err = spec["err"]
    if spec.get("out"):
        t.out = [spec["out"]]
    return t


def populate(tui):
    """把演示数据灌进一个 LlamaTUI 实例。返回同一个实例，方便链式调用。"""
    tui.demo = True
    tui.llama_bin = {"llama-server": LLAMA_SERVER}

    backends = [build_backend(s) for s in BACKENDS]
    tui.backends = backends

    models = []
    for b in backends:
        models.append({
            "name": L.short_name(b.model_path),
            "path": b.model_path,
            "size": 0,
            "dir": os.path.dirname(b.model_path),
            "live": True,
            "backend": b.base_url,
            "note": "",
        })
    # 给跑着的模型补个体积（演示好看）
    if models:
        models[0]["size"] = 9.8 * 1024 ** 3
    if len(models) > 1:
        models[1]["size"] = 3.4 * 1024 ** 3
    for name, size, _live, _bk in MODELS:
        models.append({"name": name, "path": "D:/models/" + name, "size": size,
                       "dir": "D:/models", "live": False, "backend": "", "note": ""})
    tui.models = models
    tui.model_sel = 0

    tui.tasks = [build_task(s, backends) for s in TASKS]

    for src, kind, msg in LOGS:
        tui.log.add(msg, src, kind)
    return tui


def make(cfg=None):
    """造一个装好演示数据的 LlamaTUI。"""
    cfg = cfg or L.load_config(None)
    return populate(L.LlamaTUI(cfg, demo=True))
