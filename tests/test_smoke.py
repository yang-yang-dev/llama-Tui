#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无窗口冒烟测试：用假终端驱动完整主循环，不创建任何控制台窗口。

覆盖：启动 -> 扫描 -> 渲染多帧 -> 按键各种弹窗 -> 退出 -> 终端状态恢复。
"""
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import llama_tui as L  # noqa: E402


class FakeBackend(L.BaseBackend):
    """内存终端：可以喂按键，记录帧。"""

    alt_screen = False

    def __init__(self, keys, w=120, h=34):
        self.keys = list(keys)
        self.w, self.h = w, h
        self.frames = []
        self.title = ""
        self.presented = 0

    def set_title(self, t):
        self.title = t

    def write(self, t):
        pass

    def hide_cursor(self):
        pass

    def show_cursor(self):
        pass

    def poll(self):
        return [self.keys.pop(0)] if self.keys else []

    def size(self):
        return self.w, self.h

    def present(self, lines, w, h):
        assert len(lines) == h, "帧行数 %d != 终端高 %d" % (len(lines), h)
        for line in lines:
            plain = L.re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)
            assert L.text_width(plain) == w, "行宽 %d != %d" % (L.text_width(plain), w)
        self.presented += 1
        if self.presented % 5 == 0:
            self.frames.append(lines)

    def restore(self):
        pass


def mk_backend(url, name="fake", port=8080):
    b = L.Backend(url, name)
    L.probe_backend(b, timeout=2)
    return b


def run_ui(keys, setup, demo_models=True):
    cfg = L.load_config(None)
    bd = FakeBackend(keys)
    tui = L.LlamaTUI(cfg, backend=bd, demo=True)
    setup(tui)
    tui.run()
    return tui, bd


def main():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))
        ok = ok and bool(cond)

    print("== 1. 空环境（没服务没模型）==")
    def setup_empty(t):
        t.backends = []
        t.models = []
        t.tasks = []
    tui, bd = run_ui(["j", "k", "l", "r", "?", "esc", "v", "esc", "q"], setup_empty)
    check("能跑起来并退出", bd.presented > 0, "帧数 %d" % bd.presented)
    check("渲染不崩", len(bd.frames) > 0)

    print("== 2. 有服务 + 有任务 ==")
    b = mk_backend("http://127.0.0.1:18080", "fake:18080")
    t = L.Task("测试任务", "m.gguf", b.base_url, max_tokens=100)
    t.state = "运行"
    t.start = time.time() - 5
    t.completion_tokens = 40
    t.out = ["输出内容 appears here"]
    def setup_live(x):
        x.backends = [b] if b.online else []
        x.models = x._merge_models([]) if b.online else []
        x.tasks = [t]
    tui, bd = run_ui(["j", "k", "l", "l", "l", " ", " ", "x", "e", "v", "j", "k", "esc", "q"],
                     setup_live)
    check("渲染帧数", bd.presented > 5, bd.presented)

    print("== 3. 弹窗流程 ==")
    tui, bd = run_ui(["m", "j", "k", "enter", "n", "a", "b", "c", "backspace", "esc",
                      "f", "esc", "?", "j", "k", "esc", "q"], setup_live)
    check("弹窗不崩", bd.presented > 5)
    check("最小输入留空", tui.minput == "", repr(tui.minput))

    print("== 4. 窄窗口降级 ==")
    bd2 = FakeBackend(["q"], w=70, h=14)
    tui2 = L.LlamaTUI(L.load_config(None), backend=bd2, demo=True)
    setup_live(tui2)
    tui2.run()
    check("窄窗口给提示而非崩溃", bd2.presented > 0)

    print("== 5. 任务取消 ==")
    t2 = L.Task("长任务", "m", "http://x")
    t2.state = "排队"
    t2.cancel()
    check("排队任务直接变取消", t2.cancelled and t2.state == "取消", t2.state)
    t3 = L.Task("长任务2", "m", "http://x")
    t3.state = "运行"
    t3.cancel()
    check("运行中任务标记取消中", t3.cancelled and t3.state == "取消中", t3.state)

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
