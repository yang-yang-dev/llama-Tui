#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端联调：连假 llama-server，验证发现/探测/流式任务/渲染。"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import llama_tui as L  # noqa: E402


def main(port=18080):
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))
        ok = ok and bool(cond)

    print("== 1. 探测 /health /props /slots /metrics ==")
    b = L.Backend("http://127.0.0.1:%d" % port, "fake", pid=None, source="test")
    alive = L.probe_backend(b, timeout=3)
    if not alive:
        print("  [FAIL] backend 在线")
        print("\n连不上 http://127.0.0.1:%d —— 先另开一个终端跑：" % port)
        print("    python tests/fake_server.py --port %d" % port)
        print("这不是代码问题，是假服务没起来。")
        return 1
    check("backend 在线", alive and b.online)
    check("health=ok", b.health == "ok", b.health)
    check("读到模型名", "Qwen3-14B" in b.model_name(), b.model_name())
    check("n_ctx=16384", b.n_ctx == 16384, b.n_ctx)
    check("槽位 1/4", b.slots_busy == 1 and len(b.slots or []) == 4,
          (b.slots_busy, len(b.slots or [])))
    check("metrics 吞吐", round(b.tps or 0, 1) == 33.2, b.tps)
    check("ctx 已用 3420", b.ctx_used() == 3420, b.ctx_used())

    print("== 2. 流式任务 ==")
    log = L.LogBuf(500)
    t = L.Task("把 01 背包改成滚动数组", model=b.model_name(),
               endpoint=b.base_url, max_tokens=512)
    t0 = time.time()
    L.run_task(t, b, log)
    dt = time.time() - t0
    check("任务完成", t.state == "完成", t.state)
    check("收到内容", "滚动数组" in t.text(), t.text()[:40])
    check("usage 计数", t.completion_tokens > 0, t.completion_tokens)
    check("有耗时", t.elapsed() > 0.5, round(t.elapsed(), 2))
    check("有速度", (t.rate() or 0) > 0, t.rate())
    print("     输出: %r" % t.text())
    print("     耗时 %.2fs（服务器每条间隔 0.35s × 5）" % dt)

    print("== 3. 1-token 活动探测 ==")
    ok2, msg, dur = L.activity_test(b, timeout=10)
    check("推理可用", ok2, msg)
    print("     %s · %.2fs" % (msg, dur))

    print("== 4. 界面渲染（真实数据）==")
    cfg = L.load_config(None)
    tui = L.LlamaTUI(cfg, demo=True)     # demo=True 只关闭自动扫描
    tui.backends = [b]
    tui.models = tui._merge_models([])
    with tui._lock:
        tui.tasks = [t]
    tui.w, tui.h = 120, 34
    cv = L.Canvas(120, 34)
    tui.cv = cv
    tui.draw_header(cv)
    tui.draw_models(cv, 0, 1, 36, 31)
    tui.draw_tasks(cv, 36, 1, 40, 31)
    tui.draw_logs(cv, 76, 1, 44, 31)
    tui.draw_footer(cv)
    plain = cv.plain()
    text = "\n".join(plain)
    check("画布 34 行", len(plain) == 34)
    check("宽度一致", all(L.text_width(r) == 120 for r in plain))
    check("左栏显示模型", "Qwen3-14B" in text)
    check("中栏显示任务输出", "滚动数组" in text)
    tui.log.add("任务 #1 完成", "task", "ok")
    cv2 = L.Canvas(120, 34)
    tui.cv = cv2
    tui.draw_logs(cv2, 76, 1, 44, 31)
    check("右栏有日志", "任务 #1" in "\n".join(cv2.plain()))

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 18080))
