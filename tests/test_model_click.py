#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证左栏模型可点：跑着的后端卡片、未运行的模型，都应该能点选。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import llama_tui as L  # noqa: E402
import demo_data  # noqa: E402


def main():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))
        ok = ok and bool(cond)

    tui = demo_data.make(L.load_config(None))
    cv = L.Canvas(120, 34)
    tui.cv = cv
    tui.hit = {}
    tui.model_sel = 0
    tui.draw_models(cv, 0, 1, 36, 31)

    print("模型:", [(i, m["name"], m.get("live")) for i, m in enumerate(tui.models)])
    print("点击区:", {k: v for k, v in tui.hit.items() if k.startswith("model:")})

    print("== 1. 每个模型都有点击区 ==")
    for i, m in enumerate(tui.models):
        key = "model:%d" % i
        check("模型[%d] %s 可点" % (i, m["name"][:28]), key in tui.hit, tui.hit.get(key))

    print("== 2. 点击运行中的模型（后端卡片）==")
    rect = tui.hit.get("model:1")
    if rect:
        before = tui.model_sel
        tui.on_mouse(("mouse", rect[0] + 3, rect[1] + 1, 0x0001, 0))
        check("点第2个（运行中）能选中", tui.model_sel == 1, "%s -> %s" % (before, tui.model_sel))

    print("== 3. 点击未运行的模型 ==")
    rect = tui.hit.get("model:2")
    if rect:
        tui.on_mouse(("mouse", rect[0] + 3, rect[1], 0x0001, 0))
        check("点第3个（未运行）能选中", tui.model_sel == 2, tui.model_sel)

    print("== 4. 点击卡片第二行也算 ==")
    rect = tui.hit.get("model:0")
    if rect:
        tui.on_mouse(("mouse", rect[0] + 5, rect[1] + 3, 0x0001, 0))
        check("点卡片中部能选中", tui.model_sel == 0, tui.model_sel)

    print("== 5. 点击区不越界、不重叠 ==")
    rects = [(k, v) for k, v in tui.hit.items() if k.startswith("model:")]
    for k, (x, y, w, h) in rects:
        check("%s 在栏内" % k, x >= 0 and y >= 1 and x + w <= 37 and y + h <= 32,
              (x, y, w, h))
    ys = sorted((v[1], v[1] + v[3], k) for k, v in rects)
    for i in range(len(ys) - 1):
        check("%s 与 %s 不重叠" % (ys[i][2], ys[i + 1][2]), ys[i][1] <= ys[i + 1][0],
              (ys[i], ys[i + 1]))

    print("== 6. 选中后 R 的行为 ==")
    tui.modal = None
    tui.model_sel = 0          # 运行中的
    tui.open_run_form()
    check("运行中的模型不进表单", tui.modal != "run_form", tui.modal)
    check("给了提示", "已" in tui.msg or "跑" in tui.msg, tui.msg)

    print("== 7. 键盘：Tab 切焦点，j/k 选模型 ==")
    tui.focus = "tasks"
    tui.model_sel = 0
    tui.key_main("tab")
    check("Tab 切到模型栏", tui.focus == "models", tui.focus)
    tui.key_main("j")
    check("j 选中下一个模型", tui.model_sel == 1, tui.model_sel)
    tui.key_main("j")
    check("再 j 到最末", tui.model_sel == 2, tui.model_sel)
    tui.key_main("j")
    check("不会越界", tui.model_sel == 2, tui.model_sel)
    tui.key_main("k")
    check("k 往回", tui.model_sel == 1, tui.model_sel)
    tui.key_main("tab")
    check("Tab 切回任务栏", tui.focus == "tasks", tui.focus)
    before = tui.model_sel
    tui.key_main("j")
    check("任务栏下 j 不动模型", tui.model_sel == before, tui.model_sel)

    print("== 8. 点左栏会把焦点切过去 ==")
    tui.focus = "tasks"
    tui.hit = {}
    cv2 = L.Canvas(120, 34)
    tui.cv = cv2
    tui.draw_models(cv2, 0, 1, 36, 31)
    r = tui.hit.get("model:1")
    if r:
        tui.on_mouse(("mouse", r[0] + 3, r[1], 0x0001, 0))
        check("点后焦点在模型栏", tui.focus == "models", tui.focus)

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
