#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 VT 鼠标序列解析（Windows Terminal / ConPTY 下鼠标的唯一来源）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import llama_tui as L  # noqa: E402


def main():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))
        ok = ok and bool(cond)

    print("== 1. SGR 鼠标：左键按下 / 松开 ==")
    ev, rest = L.parse_vt_stream("\x1b[<0;38;6M")
    check("解析出 1 个事件", len(ev) == 1, ev)
    if ev:
        e = ev[0]
        check("是鼠标事件", isinstance(e, tuple) and e[0] == "mouse", e)
        check("x 偏移正确(38->37)", e[1] == 37, e[1])
        check("y 偏移正确(6->5)", e[2] == 5, e[2])
        check("左键按下 state=0x1", e[3] == 0x0001, hex(e[3]))
    check("无残留", rest == "", repr(rest))

    ev, _ = L.parse_vt_stream("\x1b[<0;38;6m")       # 小写 m = 松开
    check("松开 state=0", ev and ev[0][3] == 0, ev and hex(ev[0][3]))

    print("== 2. SGR 鼠标：滚轮方向 ==")
    ev, _ = L.parse_vt_stream("\x1b[<64;10;10M")     # 64 = 上滚
    check("上滚 delta 为正", ev and ev[0][3] > 0, ev and ev[0][3])
    check("上滚 flags=WHEEL", ev and ev[0][4] == 0x0004, ev and hex(ev[0][4]))
    ev, _ = L.parse_vt_stream("\x1b[<65;10;10M")     # 65 = 下滚
    check("下滚 delta 为负", ev and ev[0][3] < 0, ev and ev[0][3])

    print("== 3. 拖动 ==")
    ev, _ = L.parse_vt_stream("\x1b[<32;20;8M")      # 32 = 按住拖动
    check("拖动带 MOVED", ev and (ev[0][4] & 0x0001), ev and hex(ev[0][4]))
    check("拖动仍是左键", ev and ev[0][3] == 0x0001, ev and hex(ev[0][3]))

    print("== 4. 分片到达（半截序列不能丢）==")
    ev1, rest1 = L.parse_vt_stream("\x1b[<0;3")
    check("半截不产生事件", ev1 == [], ev1)
    check("半截被保留", rest1 == "\x1b[<0;3", repr(rest1))
    ev2, rest2 = L.parse_vt_stream(rest1 + "8;6M")
    check("拼回后能解析", len(ev2) == 1 and ev2[0][1] == 37, ev2)
    check("拼接后无残留", rest2 == "", repr(rest2))

    print("== 5. 键盘与鼠标混流 ==")
    ev, rest = L.parse_vt_stream("j\x1b[<0;5;5M\x1b[Aq")
    kinds = [e[0] if isinstance(e, tuple) else e for e in ev]
    check("顺序正确", kinds == ["j", "mouse", "up", "q"], kinds)

    print("== 6. X10 老式鼠标 ==")
    # ESC [ M  cb=32(cb+32=64) cx=43(11+32) cy=37(5+32)
    seq = "\x1b[M" + chr(32) + chr(43) + chr(37)
    ev, _ = L.parse_vt_stream(seq)
    check("X10 左键解析", ev and ev[0][:3] == ("mouse", 10, 4), ev)

    print("== 7. 普通文本不被当成序列 ==")
    ev, rest = L.parse_vt_stream("hello")
    check("逐字符返回", ev == list("hello"), ev)
    check("无残留", rest == "", repr(rest))
    ev, rest = L.parse_vt_stream("中")
    check("中文正常", ev == ["中"], ev)

    print("== 8. Win32 滚轮高 16 位 ==")
    # 上滚：高字 120
    check("上滚 +120", L.wheel_delta_from_buttons(120 << 16) == 120,
          L.wheel_delta_from_buttons(120 << 16))
    # 下滚：高字 -120（即 0xFF88）
    raw = (-120 & 0xFFFF) << 16
    check("下滚 -120", L.wheel_delta_from_buttons(raw) == -120,
          L.wheel_delta_from_buttons(raw))
    check("无滚轮返回 None", L.wheel_delta_from_buttons(1) is None)

    print("== 9. 端到端：靠 VT 序列点击任务 ==")
    import demo_data
    tui = demo_data.make(L.load_config(None))
    cv = L.Canvas(120, 34)
    tui.cv = cv
    tui.task_sel = 0
    tui.hit = {}
    tui.draw_tasks(cv, 36, 1, 40, 31)
    rect = tui.hit.get("task:2")
    check("有 task:2 区域", rect is not None, rect)
    if rect:
        # 造一条真实的 SGR 序列（终端坐标 +1）
        seq = "\x1b[<0;%d;%dM" % (rect[0] + 2 + 1, rect[1] + 1)
        events, _ = L.parse_vt_stream(seq)
        for e in events:
            if isinstance(e, tuple):
                tui.on_mouse(e)
        check("点击后选中第3个任务", tui.task_sel == 2, tui.task_sel)

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
