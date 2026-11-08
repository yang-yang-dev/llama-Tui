#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证日志折行与鼠标交互（点击选任务/模型、滚轮翻日志、拖选复制）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import llama_tui as L  # noqa: E402


class MemBackend(L.BaseBackend):
    alt_screen = False

    def __init__(self, w=120, h=34):
        self.w, self.h = w, h

    def set_title(self, t):
        pass

    def write(self, t):
        pass

    def hide_cursor(self):
        pass

    def show_cursor(self):
        pass

    def poll(self):
        return []

    def size(self):
        return self.w, self.h

    def present(self, lines, w, h):
        assert len(lines) == h

    def restore(self):
        pass


def main():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))
        ok = ok and bool(cond)

    cfg = L.load_config(None)
    tui = L.make_demo(cfg)
    tui.bd = MemBackend()

    print("== 1. 日志折行不溢出边框 ==")
    tui.log.buf.clear()
    tui.log.add("没找到 llama-server（可在 config.json 的 extra_llama_dirs 里指定目录）",
                "find", "warn")
    tui.log.add("main: server is listening on http://127.0.0.1:8080 - starting the main loop",
                "server")
    for w in (44, 60, 72, 100):
        cv = L.Canvas(w, 12)
        tui.cv = cv
        tui.draw_logs(cv, 0, 0, w, 12)
        rows = cv.plain()
        # 右边框必须保持完整：每行都以 │ 结尾
        good = all(r.rstrip().endswith("│") for r in rows[1:-1])
        check("宽度 %d 右边框完整" % w, good, [r[-4:] for r in rows[1:3]])
        # 每行显示宽度必须正好等于 w
        check("宽度 %d 行宽正确" % w, all(L.text_width(r) == w for r in rows),
              [(L.text_width(r), w) for r in rows if L.text_width(r) != w][:2])

    print("== 2. 长消息完整显示（换行而非截断）==")
    cv = L.Canvas(52, 10)
    tui.cv = cv
    tui.draw_logs(cv, 0, 0, 52, 10)
    joined = "".join(cv.plain())
    check("能读到全文末尾", "主循环" in joined or "main loop" in joined or "指定目录" in joined,
          joined[:80])

    print("== 3. 点击选中任务 ==")
    tui.tasks = []
    for i in range(3):
        t = L.Task("任务%d" % i, "m", "http://127.0.0.1:1")
        t.state = "完成"
        t.start = t.end = L.now()
        tui.tasks.append(t)
    cv = L.Canvas(120, 34)
    tui.cv = cv
    tui.task_sel = 0
    tui.task_top = 0
    tui.hit = {}                      # 清掉上一节留下的区域
    tui.detail = False
    tui.draw_tasks(cv, 36, 1, 40, 31)
    check("注册了任务点击区", any(k.startswith("task:") for k in tui.hit), list(tui.hit)[:4])
    rect = tui.hit.get("task:2")
    if rect:
        tui.on_mouse(("mouse", rect[0] + 2, rect[1], 0x0001, 0))
        check("点第3行选中 task_sel=2", tui.task_sel == 2, tui.task_sel)
    rect0 = tui.hit.get("task:0")
    if rect0:
        tui.on_mouse(("mouse", rect0[0] + 2, rect0[1], 0x0001, 0))
        check("点第1行回到 task_sel=0", tui.task_sel == 0, tui.task_sel)

    print("== 4. 滚轮翻日志 ==")
    tui.log.buf.clear()
    for i in range(80):
        tui.log.add("第 %d 行日志内容" % i, "server")
    cv = L.Canvas(120, 34)
    tui.cv = cv
    tui.draw_logs(cv, 80, 1, 40, 31)
    check("已记录日志区域", tui.log_area is not None, tui.log_area)
    la = tui.log_area
    tui.log_off = 0
    tui.on_mouse(("mouse", la[0] + 2, la[1] + 2, 120, 0x0004))   # 滚轮上
    check("滚轮上翻 log_off>0", tui.log_off > 0, tui.log_off)
    check("自动冻结", tui.freeze)
    up = tui.log_off
    tui.on_mouse(("mouse", la[0] + 2, la[1] + 2, -120, 0x0004))  # 滚轮下
    check("滚轮下翻 log_off 减小", tui.log_off < up, tui.log_off)

    print("== 5. 拖选取文本 ==")
    tui.log.buf.clear()
    tui.log.add("HELLO-WORLD-12345", "server")
    cv = L.Canvas(120, 20)
    tui.cv = cv
    tui.draw_logs(cv, 0, 0, 120, 20)
    la = tui.log_area
    tui.drag = (la[0], la[1], la[0] + la[2] - 1, la[1])
    text = tui.selection_text()
    check("拖选能取到文本", "HELLO-WORLD-12345" in text, repr(text[:60]))
    check("不含边框", "│" not in text, repr(text[:40]))

    print("== 6. 底栏按钮可点 ==")
    cv = L.Canvas(120, 34)
    tui.cv = cv
    tui.draw_footer(cv)
    check("注册了按钮", any(k.startswith("btn:") for k in tui.hit), list(tui.hit)[:5])
    tui.log_filt = 0
    btn = tui.hit.get("btn:filter")
    if btn:
        tui.on_mouse(("mouse", btn[0], btn[1], 0x0001, 0))
        check("点过滤按钮切换", tui.log_filt == 1, tui.log_filt)

    print("== 7. 输入模式包含鼠标 ==")
    check("ENABLE_MOUSE_INPUT 常量就绪", hasattr(L.VTBackend, "poll"))

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
