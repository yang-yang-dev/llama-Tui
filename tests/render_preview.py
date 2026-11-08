#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Canvas 渲染成 PNG 预览图（用内置 Pillow，仅用于展示，不是 TUI 依赖）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
DOCS = os.path.join(os.path.dirname(HERE), "docs")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import llama_tui as L  # noqa: E402

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\consola.ttf",
]

PALETTE = {
    231: (255, 255, 255), 255: (255, 255, 255), 252: (229, 229, 229),
    253: (229, 229, 229), 244: (160, 160, 160), 240: (120, 120, 120),
    238: (90, 90, 90), 237: (75, 75, 75), 222: (255, 214, 130),
    215: (255, 175, 95), 203: (255, 110, 110), 179: (222, 190, 100),
    114: (135, 215, 135), 81: (100, 200, 230), 74: (95, 155, 220),
    60: (55, 85, 120), 244 + 1: (170, 170, 170),
}
BG = (18, 20, 26)


def parse_sgr(line):
    """返回 [(char, rgb, bg_rgb)]。"""
    import re
    out = []
    fg = (229, 229, 229)
    bg = None
    i = 0
    for m in re.finditer(r"\x1b\[([0-9;]*)m", line):
        seg = line[i:m.start()]
        for ch in seg:
            out.append((ch, fg, bg))
        codes = [int(x) for x in m.group(1).split(";") if x]
        j = 0
        while j < len(codes):
            c = codes[j]
            if c == 0:
                fg, bg = (229, 229, 229), None
            elif c == 1:
                fg = (255, 255, 255)
            elif c == 38 and j + 2 < len(codes) and codes[j + 1] == 5:
                fg = PALETTE.get(codes[j + 2], (229, 229, 229))
                j += 2
            elif c == 48 and j + 2 < len(codes) and codes[j + 1] == 5:
                bg = PALETTE.get(codes[j + 2], None)
                j += 2
            j += 1
        i = m.end()
    for ch in line[i:]:
        out.append((ch, fg, bg))
    return out


def render(canvas, path, cw=9, ch=19):
    font = None
    for f in FONT_CANDIDATES:
        if os.path.isfile(f):
            try:
                font = ImageFont.truetype(f, 15)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    pad = 12
    W = canvas.w * cw + pad * 2
    H = canvas.h * ch + pad * 2
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for y, line in enumerate(canvas.render()):
        cells = parse_sgr(line)
        x = pad
        for c, fg, bg in cells:
            w = L.char_width(c)
            if c == "\x00":
                continue
            if bg:
                d.rectangle([x, pad + y * ch, x + cw * w, pad + (y + 1) * ch - 1], fill=bg)
            # 方块字符部分字体缺字形，直接画矩形，保证预览与终端一致
            if c in "█░▏▎▍▌▋▊▉":
                top = pad + y * ch + 4
                bot = pad + (y + 1) * ch - 5
                if c == "█":
                    d.rectangle([x + 1, top, x + cw * w - 2, bot], fill=fg)
                elif c == "░":
                    d.rectangle([x + 1, top, x + cw * w - 2, bot], fill=(52, 56, 66))
                elif c in "▏▎▍▌▋▊▉":
                    frac = "▏▎▍▌▋▊▉".index(c) + 1
                    d.rectangle([x + 1, top, x + max(1, cw * frac // 7), bot], fill=fg)
            elif c.strip():
                d.text((x, pad + y * ch + 1), c, font=font, fill=fg)
            x += cw * w
    img.save(path)
    return path


if __name__ == "__main__":
    mode = sys.argv[3] if len(sys.argv) > 3 else "main"
    w, h = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (132, 38)
    cfg = L.load_config(None)
    if mode == "run":
        # 运行配置表单截图
        tui = L.make_demo(cfg)
        cv = L.Canvas(w, h)
        tui.cv = cv
        tui.w, tui.h = w, h
        tui.form_model = tui.models[2]
        tui.form_values = dict(L.DEFAULTS["run"])
        tui.form_values.update({"port": 8080, "ctx": 16384, "ngl": 99})
        tui.form_sel = 1
        tui.draw_header(cv)
        mw = max(24, min(46, int(w * 0.29)))
        tw = max(28, min(54, int(w * 0.33)))
        lw = w - mw - tw
        tui.draw_models(cv, 0, 1, mw, h - 3)
        tui.draw_tasks(cv, mw, 1, tw, h - 3)
        tui.draw_logs(cv, mw + tw, 1, lw, h - 3)
        tui.draw_footer(cv)
        tui.draw_run_form(cv)
        out = os.path.join(DOCS, "run-form.png")
        os.makedirs(DOCS, exist_ok=True)
        print(render(cv, out))
        sys.exit(0)
    cv = L.dump_frame(cfg, True, w, h)
    out = os.path.join(DOCS, "screenshot.png")
    os.makedirs(DOCS, exist_ok=True)
    print(render(cv, out))
