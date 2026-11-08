#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llama_tui.py —— llama.cpp 命令行小管家（零依赖单文件 TUI）

    ┌ 模型 ──────────────────┬ 任务 ───────────────┬ 日志 ───────────────┐
    │ ● Qwen3-14B-Q4_K_M     │ ◆#1 写个快排        │ 15:42:01 [server] … │
    │   运行中 · pid 41232   │   运行 233 tok …    │ 15:42:02 [task]   … │
    │   127.0.0.1:8080       │ ✓#2 解释 KV cache   │ …                   │
    │   ctx ████░░ 10k/16k   │ ⊘#3 失败            │                     │
    └────────────────────────┴─────────────────────┴─────────────────────┘
    f 发任务  v 详情  m 选模型  n 加模型  T 测速  l 过滤  r 重扫  ? 帮助  q 退出

特点
  * 零依赖：只用 Python 标准库，Windows/Linux/macOS 通用
  * 自动发现 llama.cpp 可执行文件（PATH / 注册表 / 常见目录 / 全盘浅扫）
  * 自动发现 .gguf 模型（缓存目录 / 常见文件夹 / 配置目录）
  * 自动发现正在监听服务的 llama-server 进程（WMI → Get-Process → netstat → TCP 探测）
  * 状态探测：/health /props /metrics /slots，外加一次 1-token 推理验证
  * 发出的任务自己收流式输出并计时，tok/s 是实测值
  * 中文宽字符按 2 列排版，不乱

用法
  python llama_tui.py                 # 自动发现后进入界面
  python llama_tui.py --demo          # 假数据预览
  python llama_tui.py --find          # 只打印发现了什么，不进界面
  python llama_tui.py --selftest      # 自检
  python llama_tui.py --dump-frame    # 打一帧界面（调试布局）
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback

VERSION = "1.0.0"
IS_WIN = (os.name == "nt")

# ══════════════════════════════════════════════════════════ 小工具


def now() -> float:
    return time.time()


def clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def fmt_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%d%s" % (n, unit)) if unit in ("B", "K") else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "-"


def fmt_tok(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    return ("%.1fk" % (n / 1000.0)) if n >= 10000 else str(int(n))


def fmt_dur(sec) -> str:
    sec = max(0.0, float(sec or 0))
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)
    return "%dd%02dh" % (sec // 86400, (sec % 86400) // 3600)


def short_name(*names) -> str:
    for n in names:
        if not n:
            continue
        s = str(n).strip().replace("\\", "/")
        if not s or s.lower() in ("unknown", "null", "none", "default"):
            continue
        return s.rsplit("/", 1)[-1]
    return ""


# ── 宽字符与折行 ──

_WIDE = (
    (0x1100, 0x115F), (0x2E80, 0x303E), (0x3041, 0x33FF), (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF), (0xA000, 0xA4CF), (0xA960, 0xA97F), (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF), (0xFE10, 0xFE19), (0xFE30, 0xFE6F), (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6), (0x1F300, 0x1F64F), (0x1F900, 0x1F9FF), (0x20000, 0x3FFFD),
)


def char_width(ch: str) -> int:
    o = ord(ch)
    if o < 32:
        return 0
    for lo, hi in _WIDE:
        if lo <= o <= hi:
            return 2
    return 1


def text_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def cut(s: str, width: int) -> str:
    """按显示宽度截断（不补齐）。"""
    if width <= 0:
        return ""
    out, w = [], 0
    for ch in s:
        cw = char_width(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out)


def pad(s: str, width: int) -> str:
    s = cut(s, width)
    return s + " " * (width - text_width(s))


def wrap_text(text, width):
    """按显示宽度折行，返回 [(chunk, width), ...]；\\n 强制断行。"""
    out = []
    if width <= 0:
        return out
    for raw in str(text).replace("\r", "").split("\n"):
        if not raw:
            out.append(("", 0))
            continue
        cur, curw = [], 0
        for ch in raw:
            cw = char_width(ch)
            if curw + cw > width and cur:
                out.append(("".join(cur), curw))
                cur, curw = [], 0
            cur.append(ch)
            curw += cw
        out.append(("".join(cur), curw))
    return out


SGR0 = "\x1b[0m"

C = {
    "frame": "\x1b[38;5;238m",
    "title": "\x1b[1;38;5;231m",
    "dim": "\x1b[38;5;244m",
    "ghost": "\x1b[38;5;240m",
    "text": "\x1b[38;5;252m",
    "hi": "\x1b[1;38;5;255m",
    "accent": "\x1b[38;5;81m",
    "ok": "\x1b[38;5;114m",
    "warn": "\x1b[38;5;179m",
    "bad": "\x1b[38;5;203m",
    "busy": "\x1b[38;5;215m",
    "bar": "\x1b[38;5;74m",
    "track": "\x1b[38;5;237m",
    "sel": "\x1b[48;5;237m",
    "selhi": "\x1b[48;5;60m",
    "hl": "\x1b[38;5;222m",
}


def bar_str(ratio, width, fg=None, track=None):
    """仅用于纯文本场合；画布内请用 Canvas.bar。"""
    if width <= 0:
        return ""
    n = int(round(clamp(ratio, 0.0, 1.0) * width))
    return "█" * n + "░" * (width - n)


# ══════════════════════════════════════════════════════════ 字体画布


class Canvas(object):
    """字符画布；颜色跟随每个格子，输出时自动复位，行与行互不影响。"""

    def __init__(self, w, h):
        self.w = max(1, int(w))
        self.h = max(1, int(h))
        self.buf = [[" "] * self.w for _ in range(self.h)]
        self.fg = [[""] * self.w for _ in range(self.h)]

    def put(self, x, y, text, fg="", bg=""):
        """写纯文本；颜色只走 fg/bg，文本里绝不能带转义序列。

        已经存在的背景色会被保留（除非显式传 bg），
        这样先铺的选中底色不会被后续文字覆盖掉。
        """
        if y < 0 or y >= self.h or not text:
            return
        cx = x
        for ch in str(text):
            cw = char_width(ch)
            if cw == 0:
                continue
            if cx >= self.w:
                break
            if cx >= 0:
                old_bg = ""
                prev = self.fg[y][cx]
                if prev and "\x1b[48;" in prev:
                    i = prev.find("\x1b[48;")
                    old_bg = prev[i:]
                style = fg + (bg or old_bg)
                self.buf[y][cx] = ch
                self.fg[y][cx] = style
                if cw == 2 and cx + 1 < self.w:
                    self.buf[y][cx + 1] = "\x00"
                    self.fg[y][cx + 1] = style
            cx += cw

    def bar(self, x, y, width, ratio, fg=None, track=None):
        """进度条：分两段画，颜色各自成段，不往格子里塞转义码。"""
        width = int(width)
        if width <= 0 or y < 0 or y >= self.h:
            return
        n = int(round(clamp(ratio, 0.0, 1.0) * width))
        if n > 0:
            self.put(x, y, "█" * n, fg or C["bar"])
        if width - n > 0:
            self.put(x + n, y, "░" * (width - n), track or C["track"])

    def putf(self, x, y, text, width, fg=""):
        self.put(x, y, cut(str(text), width), fg)

    def hline(self, x, y, w, ch="─", fg=""):
        if y < 0 or y >= self.h:
            return
        for i in range(max(0, int(w))):
            self.put(x + i, y, ch, fg)

    def vline(self, x, y, h, ch="│", fg=""):
        for i in range(max(0, int(h))):
            self.put(x, y + i, ch, fg)

    def fill(self, x, y, w, h, ch=" ", fg=""):
        for i in range(max(0, int(h))):
            self.hline(x, y + i, w, ch, fg)

    def render(self):
        out = []
        for row in range(self.h):
            parts, cur = [], ""
            for cx in range(self.w):
                ch = self.buf[row][cx]
                if ch == "\x00":
                    continue
                c = self.fg[row][cx]
                if c != cur:
                    parts.append(SGR0)
                    if c:
                        parts.append(c)
                    cur = c
                parts.append(ch)
            parts.append(SGR0)
            out.append("".join(parts))
        return out

    def plain(self):
        return ["".join(ch for ch in row if ch != "\x00") for row in self.buf]


# ══════════════════════════════════════════════════════════ 终端后端


KEY_MAP = {
    "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace",
    "\x1b": "esc", "\t": "tab", " ": "space",
}

VK_MAP = {
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x24: "home", 0x23: "end", 0x2E: "delete",
    0x21: "pageup", 0x22: "pagedown", 0x0D: "enter", 0x08: "backspace",
    0x1B: "esc", 0x09: "tab", 0x20: "space",
}


class BaseBackend(object):
    alt_screen = True

    def present(self, lines, w, h):
        pass

    def hide_cursor(self):
        pass

    def show_cursor(self):
        pass

    def set_title(self, t):
        pass

    def poll(self):
        return []

    def size(self):
        try:
            s = shutil.get_terminal_size((100, 30))
            return max(40, s.columns), max(12, s.lines)
        except Exception:
            return 100, 30

    def bell(self):
        pass


def parse_vt_stream(s):
    """把 VT 输入流切成按键和鼠标事件，返回 (events, 未完成的尾部)。

    支持：
      ESC [ < b ; x ; y M/m   SGR 鼠标（现代终端默认）
      ESC [ b ; x ; y M       X10 鼠标（老式，坐标偏移 32）
      方向键 / Home / End / PgUp / PgDn / Delete / 普通字符 / Esc
    尾部若可能是半个序列，会原样留着等下一次拼接，避免把 ESC[<0;1 拆坏。
    """
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch != "\x1b":
            out.append(KEY_MAP.get(ch, ch))
            i += 1
            continue

        rest = s[i:]

        # ① SGR 鼠标：ESC [ < b ; x ; y (M|m)
        m = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])", rest)
        if m:
            b = int(m.group(1))
            x = int(m.group(2)) - 1          # 终端坐标从 1 开始
            y = int(m.group(3)) - 1
            final = m.group(4)
            ev = sgr_mouse_event(b, x, y, final)
            if ev:
                out.append(ev)
            i += m.end()
            continue

        # ② X10 鼠标：ESC [ M cb cx cy（各字节减 32）
        m = re.match(r"\x1b\[M(...)", rest, re.S)
        if m:
            while len(m.group(1)) < 3:
                break
            cb = ord(m.group(1)[0]) - 32
            cx = ord(m.group(1)[1]) - 32 - 1
            cy = ord(m.group(1)[2]) - 32 - 1
            ev = x10_mouse_event(cb, cx, cy)
            if ev:
                out.append(ev)
            i += m.end()
            continue

        # ③ 光标键等功能键
        m = re.match(r"\x1b(?:\[|O)(\d*)([A-Za-z~])", rest)
        if m:
            num, fin = m.group(1), m.group(2)
            key = ({"A": "up", "B": "down", "C": "right", "D": "left",
                    "H": "home", "F": "end"}.get(fin)
                   or {"3": "delete", "5": "pageup", "6": "pagedown",
                       "1": "home", "4": "end", "2": "insert"}.get(num))
            if key:
                out.append(key)
                i += m.end()
                continue

        # ④ 不能确定是不是半个序列：留到下次
        if _looks_partial(rest):
            return out, rest

        out.append("esc")
        i += 1
    return out, ""


def _looks_partial(rest):
    """这段是不是「序列还没收完」。"""
    if rest == "\x1b":
        return True
    if len(rest) == 1:
        return True                       # ESC 后面可能跟着 [ 或 O
    if not rest.startswith("\x1b[") and not rest.startswith("\x1bO"):
        return False                      # ESC + 普通字符 = Alt 组合，不是半个序列
    body = rest[2:]
    if not body:
        return True
    if body[0] == "<":
        # SGR 鼠标必须完整到 M/m
        return not re.match(r"\x1b\[<\d+;\d+;\d+[Mm]", rest)
    if body[0] == "M":
        return len(rest) < 6              # ESC [ M + 3 字节
    return bool(re.fullmatch(r"\d*", body))


def sgr_mouse_event(b, x, y, final):
    """SGR 鼠标编码 → 内部事件元组。"""
    if b & 64:                                  # 滚轮：64=上，65=下
        up = (b & 1) == 0
        return ("mouse", x, y, (120 if up else -120), 0x0004)
    state = 0
    btn = b & 3
    release = (final == "m") or (btn == 3 and not (b & 32))
    if not release:
        if btn == 0:
            state = 0x0001                      # 左键
        elif btn == 1:
            state = 0x0004                      # 中键
        elif btn == 2:
            state = 0x0002                      # 右键
    flags = 0x0001 if (b & 32) else 0           # 拖动
    return ("mouse", x, y, state, flags)


def x10_mouse_event(cb, x, y):
    """老式 X10 鼠标编码 → 内部事件元组。64=上滚，65=下滚。"""
    if cb & 64:
        up = (cb & 1) == 0
        return ("mouse", x, y, (120 if up else -120), 0x0004)
    state = 0
    btn = cb & 3
    if btn == 0:
        state = 0x0001
    elif btn == 1:
        state = 0x0004
    elif btn == 2:
        state = 0x0002
    flags = 0x0001 if (cb & 32) else 0
    return ("mouse", x, y, state, flags)


def wheel_delta_from_buttons(button_state):
    """Win32 鼠标记录里，滚轮增量在 dwButtonState 的高 16 位，且是有符号的。"""
    hi = (button_state >> 16) & 0xFFFF
    if hi == 0:
        return None
    if hi & 0x8000:
        hi -= 0x10000
    return hi


def decode_key(ch, vk, ctrl=0):
    """把 Windows 控制台的一条按键记录翻译成键名列表。

    注意：Enter 的 uChar 是 '\\r'、Ctrl+C 是 '\\x03'，它们非空但不能直接当字符用，
    否则各弹窗里判断的 "enter" / "esc" 永远匹配不上。
    """
    # Ctrl+C：退出
    if ch == "\x03":
        return ["q"]
    # 退格
    if ch in ("\x08", "\x7f"):
        return ["backspace"]
    # Enter / 小键盘 Enter
    if ch in ("\r", "\n") or vk == 0x0D:
        return ["enter"]
    # Tab
    if ch == "\t" or vk == 0x09:
        return ["tab"]
    # Esc
    if ch == "\x1b" or vk == 0x1B:
        return ["esc"]
    # 普通可打印字符
    if ch and ch != "\x00" and ch >= " ":
        return ["space" if ch == " " else ch]
    # 方向键等功能键走虚拟键码
    if vk in VK_MAP:
        return [VK_MAP[vk]]
    if vk == 0x2E:
        return ["delete"]
    return []


class VTBackend(BaseBackend):
    """Windows 10+/Linux/macOS：ANSI 序列整帧刷新。"""

    def __init__(self, alt_screen=True):
        self.alt_screen = alt_screen
        self.out = getattr(sys.stdout, "buffer", None) or sys.stdout
        self._keys = []
        self._win = False
        self._mouse_on = False
        self._vt_buf = ""          # VT 输入的半截序列
        if IS_WIN:
            try:
                self._init_win()
                # 只有真正拿到输入模式，才算 Windows 控制台可用
                self._win = self._in_ok
            except Exception:
                self._win = False
        if not self._win:
            try:
                import termios
                import tty
                self._fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self._fd)
                tty.setraw(self._fd)
            except Exception:
                self._old = None

    def _init_win(self):
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32 = k32
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        self.h_in = k32.GetStdHandle(-10)
        self.h_out = k32.GetStdHandle(-11)

        # 输出：开 VT 处理
        mode = wintypes.DWORD(0)
        self._old_in = self._old_out = None
        if k32.GetConsoleMode(self.h_out, ctypes.byref(mode)):
            self._old_out = mode.value
            k32.SetConsoleMode(self.h_out, wintypes.DWORD(
                mode.value | 0x0001 | 0x0004))
        # 输入：关行缓冲/回显/信号，关快速编辑（要自己处理鼠标），开鼠标事件
        self._in_ok = False
        if k32.GetConsoleMode(self.h_in, ctypes.byref(mode)):
            self._in_ok = True
            self._old_in = mode.value
            k32.SetConsoleMode(self.h_in, wintypes.DWORD(
                0x0080 | 0x0010))   # EXTENDED_FLAGS | MOUSE_INPUT（不含 QUICK_EDIT）
        # 代码页 UTF-8（否则中文变问号）
        try:
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
        except Exception:
            pass
        self._build_input_types()

    def _build_input_types(self):
        from ctypes import wintypes
        self.wintypes = wintypes

        # ctypes.wintypes 里没有 COORD / SMALL_RECT 之外的几个，COORD 要自己定义
        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        self.COORD = COORD

        class KEY_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                        ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                        ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

        class MOUSE_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("dwMousePosition", COORD), ("dwButtonState", wintypes.DWORD),
                        ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]

        class WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
            _fields_ = [("dwSize", COORD)]

        class MENU_EVENT_RECORD(ctypes.Structure):
            _fields_ = [("dwCommandId", wintypes.UINT)]

        class _UNION(ctypes.Union):
            _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD),
                        ("WindowBufferSizeEvent", WINDOW_BUFFER_SIZE_RECORD),
                        ("MenuEvent", MENU_EVENT_RECORD)]

        class INPUT_RECORD(ctypes.Structure):
            _fields_ = [("EventType", wintypes.WORD), ("Event", _UNION)]

        self.KEY_EVENT_RECORD = KEY_EVENT_RECORD
        self.INPUT_RECORD = INPUT_RECORD
        self._RE_SIZE = ctypes.sizeof(INPUT_RECORD)

    # ── 输出 ──
    def write(self, text):
        data = text.encode("utf-8", "replace")
        try:
            self.out.write(data)
            self.out.flush()
        except Exception:
            pass

    def present(self, lines, w, h):
        buf = ["\x1b[H"]
        for i, line in enumerate(lines):
            buf.append("\x1b[%d;1H" % (i + 1))
            buf.append(line)
            buf.append(SGR0)
        buf.append("\x1b[%d;1H\x1b[K" % h)
        self.write("".join(buf))

    def hide_cursor(self):
        self.write("\x1b[?25l")

    def show_cursor(self):
        self.write("\x1b[?25h")

    def set_title(self, t):
        self.write("\x1b]0;%s\x07" % str(t).replace("\x07", ""))

    def bell(self):
        self.write("\x07")

    def size(self):
        if self._win:
            from ctypes import wintypes
            class COORD(ctypes.Structure):
                _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]
            class SMALL_RECT(ctypes.Structure):
                _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                            ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]
            class CSBI(ctypes.Structure):
                _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                            ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                            ("dwMaximumWindowSize", COORD)]
            info = CSBI()
            try:
                if self.k32.GetConsoleScreenBufferInfo(self.h_out, ctypes.byref(info)):
                    return (max(40, info.srWindow.Right - info.srWindow.Left + 1),
                            max(12, info.srWindow.Bottom - info.srWindow.Top + 1))
            except Exception:
                pass
        return BaseBackend.size(self)

    # ── 输入 ──
    def poll(self):
        if self._win:
            return self._poll_win()
        return self._poll_posix()

    def _poll_win(self):
        cnt = ctypes.c_ulong(0)
        try:
            if not self.k32.GetNumberOfConsoleInputEvents(self.h_in, ctypes.byref(cnt)):
                return []
            n = int(cnt.value)
            if n <= 0:
                return []
            n = min(n, 128)
            recs = (self.INPUT_RECORD * n)()
            got = ctypes.c_ulong(0)
            if not self.k32.ReadConsoleInputW(self.h_in, ctypes.byref(recs), n, ctypes.byref(got)):
                return []
        except Exception:
            return []
        keys = []
        for i in range(int(got.value)):
            r = recs[i]
            if r.EventType == 0x0004:      # 窗口尺寸变化
                keys.append("resize")
                continue
            if r.EventType == 0x0002:      # 鼠标
                me = r.Event.MouseEvent
                if me.dwEventFlags & 0x0004:        # 滚轮：增量在高 16 位
                    delta = wheel_delta_from_buttons(me.dwButtonState) or 0
                    keys.append(("mouse", me.dwMousePosition.X, me.dwMousePosition.Y,
                                 delta, 0x0004))
                else:
                    keys.append(("mouse", me.dwMousePosition.X, me.dwMousePosition.Y,
                                 me.dwButtonState, me.dwEventFlags))
                continue
            if r.EventType != 0x0001:      # 只看键盘和鼠标
                continue
            ke = r.Event.KeyEvent
            if not ke.bKeyDown:
                continue
            ch = ke.uChar
            vk = ke.wVirtualKeyCode
            keys.extend(decode_key(ch, vk))
        return keys

    def _poll_posix(self):
        import select
        try:
            if not select.select([sys.stdin], [], [], 0)[0]:
                return []
            data = os.read(self._fd, 4096)
        except Exception:
            return []
        if not data:
            return []
        self._vt_buf += data.decode("utf-8", "replace")
        events, self._vt_buf = parse_vt_stream(self._vt_buf)
        return events

    def enable_mouse(self):
        """让终端把鼠标事件发过来。

        Windows Terminal / ConPTY 不投递 Win32 的 MOUSE_EVENT 记录，
        必须显式请求 VT 鼠标跟踪；老式 conhost 同时也会送 Win32 记录。
        """
        if self._mouse_on:
            return
        # 1000 按下/松开 · 1002 按住拖动 · 1006 SGR 坐标（支持超 223 列）
        self.write("\x1b[?1000h\x1b[?1002h\x1b[?1006h")
        self._mouse_on = True

    def disable_mouse(self):
        if not self._mouse_on:
            return
        self.write("\x1b[?1006l\x1b[?1002l\x1b[?1000l")
        self._mouse_on = False

    def restore(self):
        try:
            self.disable_mouse()
        except Exception:
            pass
        if self._win:
            try:
                if self._old_in is not None:
                    self.k32.SetConsoleMode(self.h_in, self.wintypes.DWORD(self._old_in))
                if self._old_out is not None:
                    self.k32.SetConsoleMode(self.h_out, self.wintypes.DWORD(self._old_out))
            except Exception:
                pass
        elif getattr(self, "_old", None) is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:
                pass


def parse_ansi_keys(s):
    keys, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\x1b":
            m = re.match(r"\x1b(?:\[|O)(\d*)([A-Za-z~])", s[i:])
            if m:
                num, fin = m.group(1), m.group(2)
                keys.append({"A": "up", "B": "down", "C": "right", "D": "left",
                             "H": "home", "F": "end"}.get(fin) or
                            {"3": "delete", "5": "pageup", "6": "pagedown"}.get(num, "esc"))
                i += m.end()
                continue
            keys.append("esc")
            i += 1
            continue
        keys.append(KEY_MAP.get(ch, ch))
        i += 1
    return keys


class PlainBackend(BaseBackend):
    """没有 ANSI 支持时，用 Win32 API 逐格写。"""

    def __init__(self):
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = wintypes.HANDLE
        self.k32 = k32
        self.h = k32.GetStdHandle(-11)
        self.h_in = k32.GetStdHandle(-10)
        mode = wintypes.DWORD(0)
        if k32.GetConsoleMode(self.h_in, ctypes.byref(mode)):
            k32.SetConsoleMode(self.h_in, wintypes.DWORD(0x0080))
        k32.SetConsoleOutputCP(65001)
        self.alt_screen = False
        self._keys = []

    def present(self, lines, w, h):
        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]
        written = ctypes.c_ulong(0)
        for i, line in enumerate(lines):
            self.k32.SetConsoleCursorPosition(self.h, COORD(0, i))
            plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)
            self.k32.WriteConsoleW(self.h, plain, len(plain), ctypes.byref(written), None)

    def poll(self):
        return []

    def size(self):
        return BaseBackend.size(self)

    def restore(self):
        pass


def make_backend():
    if IS_WIN:
        try:
            b = VTBackend()
            b.write("\x1b[0m")
            return b
        except Exception:
            try:
                return PlainBackend()
            except Exception:
                pass
    return VTBackend()


# ══════════════════════════════════════════════════════════ 配置


DEFAULTS = {
    "refresh_interval": 2.0,
    "max_logs": 5000,
    "max_tokens": 0,
    "temperature": 0.7,
    "scan_processes": True,
    "scan_interval": 20.0,
    "auto_scan_model_dirs": True,
    "extra_llama_dirs": [],
    "extra_model_dirs": [],
    "models": [],
    "services": [],
    "spawn": [],
    # 运行配置：在 TUI 里改，按「运行」时用
    "run": {
        "port": 8080,
        "ctx": 4096,
        "ngl": 99,
        "threads": 0,
        "batch": 512,
        "ubatch": 512,
        "parallel": 1,
        "flash_attn": True,
        "metrics": True,
        "extra_args": "",
    },
}


def deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [path] if path else [os.path.join(os.getcwd(), "config.json"),
                                 os.path.join(here, "config.json")]
    cfg = json.loads(json.dumps(DEFAULTS))
    used = None
    for p in cands:
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    cfg = deep_merge(cfg, json.load(f))
                used = os.path.abspath(p)
                break
            except Exception as e:
                sys.stderr.write("读取配置失败 %s: %s\n" % (p, e))
    cfg["_path"] = used
    cfg["site_dir"] = here
    cfg["services"] = [dict(s) for s in (cfg.get("services") or [])]
    for s in cfg["services"]:
        s["base_url"] = str(s.get("base_url") or "").rstrip("/")
        s.setdefault("name", s["base_url"] or "service")
    return cfg


def save_config(cfg):
    path = cfg.get("_path") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    data = {k: v for k, v in cfg.items() if not k.startswith("_") and k != "site_dir"}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    cfg["_path"] = path
    return path


# ══════════════════════════════════════ 自动发现：程序 / 模型 / 服务


LLAMA_TOOLS = ["llama-server", "llama-cli", "llama-bench", "llama-quantize",
               "llama-perplexity", "llama-embedding", "llama-batched", "llama-swap"]


def _exe_names(tool):
    return [tool + ".exe", tool] if IS_WIN else [tool]


def _registry_llama_dirs():
    """Windows：从 PATH 相关注册表项找 llama 目录（尽力而为）。"""
    out = []
    if not IS_WIN:
        return out
    try:
        import winreg
        keys = [
            (winreg.HKEY_CURRENT_USER, r"Environment", "Path"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "Path"),
        ]
        for root, sub, name in keys:
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, name)
                    for p in str(val).split(";"):
                        if p.strip():
                            out.append(os.path.expandvars(p.strip()))
            except Exception:
                pass
    except Exception:
        pass
    return out


def _find_official_llama():
    """找用户自己下载解压的 llama.cpp 官方发行版。

    典型位置：桌面 / 下载 下的 llama、llama-b4567-bin-win-cuda-x64 之类，
    解压出来往往是一堆 exe 直接平铺，也可能带一层 build/bin。
    判据是「那个 llama-server 真能跑起来」。
    """
    home = os.path.expanduser("~")
    roots = []
    for base in (os.path.join(home, "Desktop"), os.path.join(home, "Downloads"),
                 os.path.join(home, "Documents"), os.path.join(home, "llama.cpp")):
        if not os.path.isdir(base):
            continue
        try:
            for e in os.scandir(base):
                if e.is_dir() and "llama" in e.name.lower():
                    roots.append(e.path)
        except OSError:
            continue
        # 目录本身就叫 llama
        if os.path.isdir(os.path.join(base, "llama")):
            roots.append(os.path.join(base, "llama"))

    seen = set()
    cands = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        # exe 可能平铺，也可能在 build/bin 里
        for sub in ("", "build/bin", "build/bin/Release", "bin"):
            d = os.path.join(root, sub) if sub else root
            p = os.path.join(d, "llama-server.exe" if IS_WIN else "llama-server")
            if os.path.isfile(p):
                cands.append(p)
    for p in cands:
        if _exe_works(p):
            return os.path.abspath(p)
    return None


def _suppress_error_dialogs():
    """阻止缺 DLL / 崩溃时 Windows 弹出错误对话框。

    CREATE_NO_WINDOW 只压控制台窗口，压不住加载器和 CRT 弹的 GUI 框。
    必须用 SetErrorMode，而且子进程会继承这个设置。
    """
    if not IS_WIN:
        return
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetErrorMode.argtypes = [ctypes.c_uint]
        k32.SetErrorMode.restype = ctypes.c_uint
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        SEM_NOOPENFILEERRORBOX = 0x8000
        k32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
                         | SEM_NOOPENFILEERRORBOX)
    except Exception:
        pass


def _no_window_flag():
    """启动子进程用的 creationflags。"""
    if not IS_WIN:
        return 0
    _suppress_error_dialogs()
    return 0x08000000          # CREATE_NO_WINDOW


def _exe_works(path, timeout=10):
    """跑 `--version` 看这个 llama-server 是不是真能起来。

    LM Studio 的 CUDA 版依赖它自己的运行库，单独执行会报
    0xC0000135（找不到 DLL）；Vulkan / CPU 版则能独立运行。
    """
    if not path or not os.path.isfile(path):
        return False
    try:
        r = subprocess.run([path, "--version"], capture_output=True, timeout=timeout,
                           cwd=os.path.dirname(path) or None,
                           creationflags=_no_window_flag())
        if r.returncode != 0:
            return False
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
        return "version" in out.lower() or "build" in out.lower()
    except Exception:
        return False


def _llm_app_backends():
    """LM Studio / Ollama / Jan 这类应用自带的 llama.cpp 后端。

    LM Studio 把 llama-server.exe 埋在
      ~/.lmstudio/extensions/backends/llama.cpp-win-x86_64-<accel>-<ver>/
    同版本多个（cuda/vulkan/cpu），优先挑能独立运行的：
    先看 CUDA，不行退 Vulkan，再不行退 CPU。
    """
    home = os.path.expanduser("~")
    out = []
    lm = os.path.join(home, ".lmstudio", "extensions", "backends")
    if os.path.isdir(lm):
        cands = []
        try:
            for e in os.scandir(lm):
                if not e.is_dir():
                    continue
                exe = None
                for name in ("llama-server.exe", "llama-server"):
                    p = os.path.join(e.path, name)
                    if os.path.isfile(p):
                        exe = p
                        break
                if not exe:
                    continue
                m = re.search(r"-(\d+\.\d+\.\d+)$", e.name)
                ver = tuple(int(x) for x in m.group(1).split(".")) if m else (0, 0, 0)
                low = e.name.lower()
                accel = 3 if "cuda" in low else (2 if "vulkan" in low else
                                                 (1 if "avx" in low else 0))
                cands.append((ver, accel, exe))
        except OSError:
            pass
        # 版本优先、加速优先；但真正能不能跑要试过才知道
        cands.sort(reverse=True)
        for _ver, _accel, exe in cands:
            if _exe_works(exe):
                out.append(exe)
                break
        # 都不行也留一个，至少界面上能看到路径（后面会提示不可用）
        if not out and cands:
            out.append(cands[0][2])
    for sub in (("AppData", "Local", "Programs", "LM Studio", "resources", "app"),
                ("AppData", "Local", "Programs", "Jan", "resources", "app")):
        d = os.path.join(home, *sub)
        if not os.path.isdir(d):
            continue
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x.lower() not in ("node_modules", "locales")]
            for f in files:
                if f.lower() in ("llama-server.exe", "llama-server"):
                    p = os.path.join(root, f)
                    if _exe_works(p):
                        out.append(p)
                        return out
    return out


def candidate_llama_dirs(cfg=None):
    """可能藏着 llama-server 的目录，按可信度排序。"""
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = []
    # A. 配置目录本身（用户可能把 exe 丢在这儿）
    dirs += [here]
    # B. PATH 里的每个目录
    for p in (os.environ.get("PATH") or "").split(os.pathsep):
        if p.strip():
            dirs.append(p.strip().strip('"'))
    # C. 注册表 PATH
    dirs += _registry_llama_dirs()
    # D. llama.cpp 常见安装位置
    pats = [
        "llama.cpp", "llama.cpp/build/bin", "llama.cpp/build/bin/Release",
        "llama.cpp/build/bin/RelWithDebInfo", "llama.cpp/llama-b*",
        "llama-b*", "llama.cpp-master", "llama_cpp",
        "AppData/Local/llama.cpp", "AppData/Local/Programs/llama.cpp",
        "scoop/apps/llama.cpp/current", "scoop/shims",
        "Documents/llama.cpp", "Downloads/llama.cpp", "Desktop/llama.cpp",
        "Downloads/llama-b*", "Desktop/llama-b*",
        "Program Files/llama.cpp", "tools/llama.cpp",
        # 用户自己解压的官方发行版，最常见的落点
        "Desktop/llama", "Desktop/llama-b*", "Desktop/llama.cpp",
        "Downloads/llama", "Downloads/llama-b*", "Downloads/llama.cpp",
        "Desktop/llama*/build/bin", "Downloads/llama*/build/bin",
        # 这些应用自带 llama.cpp 的后端二进制
        ".lmstudio/extensions/backends", ".lmstudio/extensions/backends/*",
        "AppData/Local/Programs/LM Studio/resources/app/.webpack",
        "AppData/Local/Programs/Jan/resources/app",
        ".ollama/models", "AppData/Local/Programs/Ollama",
        ".cache/llama.cpp", "AppData/Local/llama.cpp",
        ".koboldcpp", "AppData/Local/Programs/koboldcpp",
        "AppData/Local/Programs/text-generation-webui",
    ]
    for base in (home, os.environ.get("LOCALAPPDATA") or home,
                 os.environ.get("APPDATA") or home, "C:/", "D:/"):
        if not base:
            continue
        for pat in pats:
            dirs.append(os.path.join(base.replace("\\", "/"), pat))
    # E. 用户额外指定
    for p in (cfg or {}).get("extra_llama_dirs") or []:
        dirs.append(os.path.expandvars(os.path.expanduser(str(p))))
    # F. 各盘根目录下一层（llama.cpp 放在盘根很常见）
    for drive in _all_drives():
        dirs.append(drive)
        try:
            for entry in os.scandir(drive):
                if entry.is_dir() and "llama" in entry.name.lower():
                    dirs.append(entry.path)
        except Exception:
            pass
    seen, out = set(), []
    for d in dirs:
        if not d:
            continue
        d = os.path.normpath(d)
        if "*" in d:
            import glob
            for g in glob.glob(d):
                gg = os.path.normpath(g)
                if gg.lower() not in seen:
                    seen.add(gg.lower())
                    out.append(gg)
            continue
        if d.lower() in seen:
            continue
        seen.add(d.lower())
        out.append(d)
    return out


def _all_drives():
    if not IS_WIN:
        return ["/"]
    out = []
    try:
        import string
        for letter in string.ascii_uppercase:
            root = "%s:\\" % letter
            if os.path.exists(root):
                out.append(root)
    except Exception:
        pass
    return out


def find_llama_bin(cfg=None, log=None):
    """返回 {'llama-server': path, ...}。

    顺序：PATH → 配置指定 → LM Studio/Ollama 自带后端 → 常见目录 → 浅扫盘。
    """
    found = {}
    # 0) PATH 直接命中
    for tool in LLAMA_TOOLS:
        p = shutil.which(tool)
        if p:
            found[tool] = os.path.abspath(p)
    if "llama-server" in found:
        return found

    # 1) 用户在配置里明确指定的，优先级最高（除了 PATH）
    for p in (cfg or {}).get("extra_llama_dirs") or []:
        p = os.path.expandvars(os.path.expanduser(str(p)))
        for tool in LLAMA_TOOLS:
            if tool in found:
                continue
            for name in _exe_names(tool):
                cand = os.path.join(p, name)
                if os.path.isfile(cand):
                    found[tool] = os.path.abspath(cand)
                    break
        if "llama-server" in found:
            if log:
                log.add("llama-server（配置指定）: %s" % found["llama-server"], "find", "ok")
            return found

    # 2) 用户自己下载的官方发行版（Desktop/Downloads 下的 llama 目录），优先于应用内置
    official = _find_official_llama()
    if official:
        found["llama-server"] = official
        d = os.path.dirname(official)
        for tool in LLAMA_TOOLS:
            if tool not in found:
                for name in _exe_names(tool):
                    p = os.path.join(d, name)
                    if os.path.isfile(p):
                        found[tool] = os.path.abspath(p)
                        break
        if log:
            log.add("llama-server（自己下载的发行版）: %s" % official, "find", "ok")
        return found

    # 3) LM Studio / Jan 这类自带的 llama.cpp 后端
    try:
        for exe in _llm_app_backends():
            if os.path.isfile(exe):
                found["llama-server"] = os.path.abspath(exe)
                d = os.path.dirname(exe)
                for tool in LLAMA_TOOLS:
                    if tool not in found:
                        for name in _exe_names(tool):
                            p = os.path.join(d, name)
                            if os.path.isfile(p):
                                found[tool] = os.path.abspath(p)
                                break
                if log:
                    log.add("llama-server（LM Studio/Jan 自带）: %s" % found["llama-server"],
                            "find", "ok")
                return found
    except Exception:
        pass

    dirs = candidate_llama_dirs(cfg)
    # 3) 候选目录里直接找
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for tool in LLAMA_TOOLS:
            if tool in found:
                continue
            for name in _exe_names(tool):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    found[tool] = os.path.abspath(p)
                    break
        if "llama-server" in found:
            break
    if "llama-server" in found:
        return found

    # 2) 浅扫：每个盘往下 3 层，找 llama-server*
    for root in _all_drives():
        hit = _shallow_search(root, ["llama-server.exe", "llama-server"], depth=3)
        if hit:
            found["llama-server"] = hit
            d = os.path.dirname(hit)
            for tool in LLAMA_TOOLS:
                if tool not in found:
                    p = os.path.join(d, tool + (".exe" if IS_WIN else ""))
                    if os.path.isfile(p):
                        found[tool] = os.path.abspath(p)
            break
    if log and found:
        log.add("找到 llama.cpp: %s" % found.get("llama-server"), "find", "ok")
    return found


def _shallow_search(root, names, depth=3):
    base_depth = root.rstrip("\\/").count(os.sep)
    skip = {"windows", "$recycle.bin", "system volume information", "node_modules",
            "appdata", "proc", "sys", "dev", "usr", "var"}
    stack = [(root, 0)]
    while stack:
        d, lvl = stack.pop()
        if lvl > depth:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_file() and e.name.lower() in [n.lower() for n in names]:
                            return e.path
                        if e.is_dir() and not e.is_symlink():
                            nm = e.name.lower()
                            if nm in skip:
                                continue
                            stack.append((e.path, lvl + 1))
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue
    return ""


def candidate_model_dirs(cfg=None):
    home = os.path.expanduser("~")
    dirs = [
        # 本地推理应用自己的模型库
        os.path.join(home, ".lmstudio", "models"),
        os.path.join(home, ".ollama", "models"),
        os.path.join(home, ".cache", "llama.cpp"),
        os.path.join(home, ".cache", "modelscope"),
        os.path.join(home, ".modelscope"),
        os.path.join(home, "models"), os.path.join(home, "Models"),
        os.path.join(home, "Downloads"), os.path.join(home, "Desktop"),
        os.path.join(home, "Documents"),
        "C:/models", "D:/models", "E:/models", "C:/llama.cpp/models",
        "D:/llama.cpp/models", "D:/models/gguf",
    ]
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [here] + dirs
    env = os.environ.get("LLAMA_MODEL_DIR") or os.environ.get("MODEL_DIR")
    if env:
        dirs.insert(0, env)
    for p in (cfg or {}).get("extra_model_dirs") or []:
        dirs.insert(0, os.path.expandvars(os.path.expanduser(str(p))))
    seen, out = set(), []
    for d in dirs:
        if not d:
            continue
        d = os.path.normpath(d)
        if d.lower() in seen:
            continue
        seen.add(d.lower())
        out.append(d)
    return out


# 这些目录里模型按 <厂商>/<仓库名>/xxx.gguf 摆放，要深一点才挖得到
DEEP_MODEL_DIRS = (".lmstudio", ".ollama", ".cache", ".modelscope", "hub")


def _model_depth(path):
    low = path.replace("\\", "/").lower()
    return 4 if any(k in low for k in DEEP_MODEL_DIRS) else 2


def find_models(cfg=None, log=None, limit=80):
    """扫描常见目录里的 .gguf。

    普通目录下探 2 层够了；但 LM Studio / Ollama 那种
    <厂商>/<仓库>/<文件>.gguf 的要多挖几层，否则一个都找不到。
    """
    found = []
    seen = set()
    for d in candidate_model_dirs(cfg):
        if not os.path.isdir(d):
            continue
        try:
            for model in _scan_gguf(d, depth=_model_depth(d), limit=limit):
                k = model["path"].lower()
                if k in seen:
                    continue
                # mmproj 是多模态的投影层，不是能用 -m 直接加载的模型
                if model["name"].lower().startswith("mmproj"):
                    continue
                seen.add(k)
                found.append(model)
                if len(found) >= limit:
                    break
        except Exception:
            continue
        if len(found) >= limit:
            break
    found.sort(key=lambda m: -(m.get("size") or 0))
    for i, m in enumerate(found):
        m["seq"] = i
    if log:
        log.add("扫到 %d 个 .gguf 模型" % len(found), "find")
    return found


def _scan_gguf(root, depth=2, limit=80):
    out = []
    base = root.rstrip("\\/")
    stack = [(base, 0)]
    while stack:
        d, lvl = stack.pop(0)
        if lvl > depth:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_file() and e.name.lower().endswith(".gguf"):
                            try:
                                sz = e.stat().st_size
                            except OSError:
                                sz = 0
                            out.append({"path": e.path, "name": e.name, "size": sz,
                                        "dir": d})
                            if len(out) >= limit:
                                return out
                        elif e.is_dir() and not e.is_symlink() and lvl < depth:
                            stack.append((e.path, lvl + 1))
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue
    return out


def listening_ports():
    """当前 LISTENING 的本地端口 → pid。"""
    out = {}
    if IS_WIN:
        try:
            r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                               timeout=6, creationflags=0x08000000)
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING":
                    addr = parts[1]
                    port = addr.rsplit(":", 1)[-1]
                    if port.isdigit():
                        out[int(port)] = int(parts[4])
        except Exception:
            pass
        return out
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                for line in f.readlines()[1:]:
                    p = line.split()
                    if len(p) > 9 and p[3] == "0A":
                        port = int(p[1].rsplit(":", 1)[-1], 16)
                        out[port] = int(p[9])
        except Exception:
            continue
    return out


def port_open(port, host="127.0.0.1", timeout=0.12):
    try:
        s = socket.socket()
        s.settimeout(timeout)
        rc = s.connect_ex((host, int(port)))
        s.close()
        return rc == 0
    except Exception:
        return False


PROBE_PORTS = [8080, 8081, 8082, 8000, 8001, 5000, 5001, 7860, 8085, 11434, 8090, 9000, 1234]


def scan_llama_services(cfg=None, log=None):
    """找出正在服务的 llama.cpp（返回 [{port, pid, url}]）。"""
    ports = listening_ports()
    hits = []
    seen = set()

    def _try(port, pid=None):
        if port in seen:
            return
        seen.add(port)
        if not port_open(port):
            return
        ok, info = probe_http("http://127.0.0.1:%d" % port)
        if ok:
            hits.append({"port": port, "pid": pid, "url": "http://127.0.0.1:%d" % port,
                         "model": info.get("model", "")})

    # 1) 配置里写的服务
    for s in (cfg or {}).get("services") or []:
        m = re.search(r":(\d+)", s.get("base_url", ""))
        if m:
            _try(int(m.group(1)))
    # 2) llama 进程占用的端口
    for p in list_processes():
        cmd = p.get("cmdline") or ""
        if not _is_llama_cmd(cmd, p.get("exe", "")):
            continue
        m = re.search(r"--port[= ]+(\d+)", cmd)
        if m:
            _try(int(m.group(1)), p.get("pid"))
        else:
            for port, pid in ports.items():
                if pid == p.get("pid"):
                    _try(port, pid)
    # 3) 直接探常见端口
    for port in PROBE_PORTS:
        _try(port)
    if log and hits:
        log.add("发现 %d 个 llama 服务: %s" % (
            len(hits), ", ".join(str(h["port"]) for h in hits)), "find", "ok")
    return hits


def _is_llama_cmd(cmd, exe=""):
    blob = (str(cmd) + " " + str(exe)).lower()
    return ("llama-server" in blob or "llama-cli" in blob or "llama_tts" in blob
            or "llama-bench" in blob or "llama.cpp" in blob)


def list_processes():
    if not IS_WIN:
        return _list_procs_posix()
    out = _procs_wmi()
    if not out:
        out = _procs_getprocess()
    return out


def _procs_wmi():
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        return []
    script = ("Get-CimInstance Win32_Process -Filter \"Name LIKE '%llama%'\" | "
              "Select-Object ProcessId,Name,CommandLine,WorkingSetSize | ConvertTo-Json -Compress")
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=10, creationflags=0x08000000)
        txt = r.stdout.decode("utf-8", "replace").strip()
        if not txt:
            return []
        data = json.loads(txt)
        if isinstance(data, dict):
            data = [data]
        res = []
        for d in data:
            res.append({"pid": d.get("ProcessId"), "exe": d.get("Name") or "",
                        "cmdline": d.get("CommandLine") or "",
                        "mem": d.get("WorkingSetSize"), "start": None, "cwd": ""})
        return res
    except Exception:
        return []


def _procs_getprocess():
    """没有 WMI 权限时的降级：Get-Process（拿不到命令行）。"""
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        return []
    script = ("Get-Process | Where-Object { $_.ProcessName -like '*llama*' } | "
              "Select-Object Id,ProcessName,WorkingSet64,StartTime | ConvertTo-Json -Compress")
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=10, creationflags=0x08000000)
        txt = r.stdout.decode("utf-8", "replace").strip()
        if not txt:
            return []
        data = json.loads(txt)
        if isinstance(data, dict):
            data = [data]
        out = []
        for d in data:
            st = d.get("StartTime")
            ts = None
            m = re.match(r"/Date\((-?\d+)", str(st))
            if m:
                ts = int(m.group(1)) / 1000.0
            out.append({"pid": d.get("Id"), "exe": (d.get("ProcessName") or "") + ".exe",
                        "cmdline": "", "mem": d.get("WorkingSet64"), "start": ts, "cwd": ""})
        return out
    except Exception:
        return []


def _list_procs_posix():
    out = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:
        return out
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            continue
        if not _is_llama_cmd(cmd):
            continue
        cwd, start, mem = "", None, None
        try:
            cwd = os.readlink("/proc/%s/cwd" % pid)
        except Exception:
            pass
        try:
            with open("/proc/%s/stat" % pid) as f:
                tail = f.read().rsplit(") ", 1)[-1].split()
            start = float(tail[19]) / os.sysconf("SC_CLK_TCK") + _boot_time()
        except Exception:
            pass
        try:
            with open("/proc/%s/status" % pid) as f:
                m = re.search(r"VmRSS:\s+(\d+)", f.read())
                mem = int(m.group(1)) * 1024 if m else None
        except Exception:
            pass
        out.append({"pid": int(pid), "exe": cmd.split(" ")[0], "cmdline": cmd,
                    "cwd": cwd, "start": start, "mem": mem})
    return out


_BOOT = [None]


def _boot_time():
    if _BOOT[0] is None:
        try:
            with open("/proc/stat") as f:
                m = re.search(r"btime (\d+)", f.read())
                _BOOT[0] = float(m.group(1)) if m else now()
        except Exception:
            _BOOT[0] = now()
    return _BOOT[0]


# ══════════════════════════════════════════════════════════ HTTP 探测


def http_get(url, timeout=1.2, headers=None):
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, headers=headers or {"User-Agent": "llama-tui/" + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), r.read(2 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(64 * 1024).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body


def http_json(url, timeout=1.2):
    st, body = http_get(url, timeout=timeout)
    try:
        return st, (json.loads(body) if body.strip() else None)
    except Exception:
        return st, None


def probe_http(base_url, timeout=1.0):
    """判断某地址是不是 llama.cpp：返回 (是否线上, {model, health})。"""
    info = {"model": "", "health": "?"}
    try:
        st, body = http_get(base_url + "/health", timeout=timeout)
        if st in (200, 503):
            info["health"] = "ok" if st == 200 else "loading"
        else:
            info["health"] = "http%d" % st
    except Exception:
        try:
            st, _ = http_get(base_url + "/v1/models", timeout=timeout)
            if st != 200:
                return False, info
            info["health"] = "ok"
        except Exception:
            return False, info
    try:
        st, props = http_json(base_url + "/props", timeout=timeout)
        if st == 200 and isinstance(props, dict):
            info["model"] = short_name(props.get("model_path"), props.get("model_alias"))
            info["props"] = props
    except Exception:
        pass
    return True, info


# ══════════════════════════════════════════════════════════ 数据对象


class Task(object):
    _seq = 0

    def __init__(self, prompt, model="", endpoint="", max_tokens=0, temperature=0.7):
        Task._seq += 1
        self.id = Task._seq
        first = prompt.strip().split("\n")[0] if prompt.strip() else "(空)"
        self.title = first[:60]
        self.prompt = prompt
        self.model = model
        self.endpoint = endpoint
        self.max_tokens = int(max_tokens or 0)
        self.temperature = temperature
        self.state = "排队"
        self.start = None
        self.end = None
        self.out = []
        self.err = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tps = None
        self.tps_live = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._t0 = None

    def append(self, text):
        with self._lock:
            self.out.append(text)

    def cancel(self):
        self._cancel.set()
        if self.state == "排队":
            self.state = "取消"
        elif self.state == "运行":
            # 工作线程会在下一个 chunk 处break并把状态置为 取消
            self.state = "取消中"

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def elapsed(self):
        if self.start is None:
            return 0.0
        return (self.end or now()) - self.start

    def text(self):
        with self._lock:
            return "".join(self.out)

    def ntok(self):
        """估算 token 数：中文约 0.7 token/字，其他按 3.5 字符/token。"""
        t = self.text()
        if not t:
            return 0
        wide = sum(1 for c in t if char_width(c) == 2)
        narrow = len(t) - wide
        est = wide * 0.7 + narrow / 3.5
        return max(1, int(est))

    def cur_tokens(self):
        return self.completion_tokens or self.ntok()

    def rate(self):
        if self.tps is not None:
            return self.tps
        t = self.elapsed()
        n = self.cur_tokens()
        return (n / t) if (t > 0.3 and n) else None

    def progress(self):
        if self.state in ("完成", "失败", "取消"):
            return 1.0
        if self.max_tokens > 0:
            return min(0.99, self.cur_tokens() / float(self.max_tokens))
        return None


class Backend(object):
    """一个 llama.cpp 服务进程 / 端点。"""

    def __init__(self, base_url="", name="", pid=None, exe="", source="config"):
        self.base_url = (base_url or "").rstrip("/")
        self.name = name or self.base_url or "llama.cpp"
        self.pid = pid
        self.exe = exe
        self.source = source
        self.online = False
        self.health = "?"
        self.model_path = ""
        self.n_ctx = None
        self.props = {}
        self.slots = None
        self.slots_busy = 0
        self.metrics = {}
        self.tps = None
        self.tokens_pred = None
        self.mem = None
        self.start_time = None
        self.cwd = ""
        self.cmdline = ""
        self.last_error = ""
        self.last_ok = None
        self.extra = {}
        m = re.search(r":(\d+)", self.base_url or "")
        self.port = int(m.group(1)) if m else None

    # 展示
    def model_name(self):
        return short_name(self.model_path) or "(未知模型)"

    def quant(self):
        m = re.search(r"(I?Q\d[_A-Z0-9]*|F16|BF16|F32|Q8_0)", self.model_name(), re.I)
        return m.group(1).upper() if m else ""

    def status(self):
        if self.online:
            if self.slots is not None and len(self.slots) > 0:
                if self.slots_busy:
                    return "忙碌 %d/%d" % (self.slots_busy, len(self.slots))
                return "空闲 %d 槽" % len(self.slots)
            return "在线"
        if self.pid:
            return "进程在跑，端口没响应" if not self.base_url else "无响应"
        return "离线"

    def status_color(self):
        st = self.status()
        if st.startswith("忙碌"):
            return C["busy"]
        if st.startswith("空闲") or st == "在线":
            return C["ok"]
        if "没响应" in st or "无响应" in st or st == "离线":
            return C["warn"] if self.pid else C["ghost"]
        return C["dim"]

    def ctx_used(self):
        p = (self.props or {}).get("default_generation_settings") or {}
        return p.get("n_past") or 0


def probe_backend(b: Backend, timeout=1.2):
    """刷新后端状态。返回 bool。"""
    if not b.base_url:
        b.online = False
        b.last_error = "没有 HTTP 端口"
        return False
    try:
        ok, info = probe_http(b.base_url, timeout=timeout)
    except Exception as e:
        ok, info = False, {"health": "?"}
        b.last_error = str(e)
    b.online = ok
    b.health = info.get("health", "?")
    if not ok:
        b.last_error = b.last_error or "连接失败"
        return False
    b.last_error = ""
    b.last_ok = now()
    props = info.get("props") or {}
    if props:
        b.props = props
        b.model_path = short_name(props.get("model_path"), props.get("model_alias")) or b.model_path
        gen = props.get("default_generation_settings") or {}
        if isinstance(gen, dict):
            b.n_ctx = gen.get("n_ctx") or b.n_ctx
    # /metrics（需要 --metrics）
    try:
        st, body = http_get(b.base_url + "/metrics", timeout=timeout)
        if st == 200 and "llamacpp" in body:
            m = {}
            for key, pat in (("tps_gen", r"llamacpp:predicted_tokens_seconds\s+([\d.eE+]+)"),
                             ("tps_prompt", r"llamacpp:prompt_tokens_seconds\s+([\d.eE+]+)"),
                             ("gen_total", r"llamacpp:tokens_predicted_total\s+([\d.eE+]+)")):
                mm = re.search(pat, body)
                if mm:
                    m[key] = float(mm.group(1))
            b.metrics = m
            b.tps = m.get("tps_gen") or b.tps
            b.tokens_pred = m.get("gen_total")
            b.extra["metrics"] = True
    except Exception:
        pass
    # /slots
    try:
        st, slots = http_json(b.base_url + "/slots", timeout=timeout)
        if st == 200 and isinstance(slots, list):
            b.slots = slots
            b.slots_busy = sum(1 for s in slots if s.get("is_processing"))
    except Exception:
        pass
    return True


def activity_test(b: Backend, timeout=60.0):
    """发一个尽量短的推理请求，验证真能算。返回 (ok, msg, 耗时)。"""
    import urllib.request
    body = json.dumps({"messages": [{"role": "user", "content": "1"}],
                       "max_tokens": 1, "stream": False}).encode("utf-8")
    req = urllib.request.Request(b.base_url + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = now()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        dt = now() - t0
        try:
            u = json.loads(raw).get("usage") or {}
            return True, "推理正常 prompt=%s gen=%s" % (u.get("prompt_tokens"), u.get("completion_tokens")), dt
        except Exception:
            return True, "有响应", dt
    except Exception as e:
        return False, "推理失败: %s" % str(e)[:80], now() - t0


class Server(object):
    """由 TUI 亲手拉起的 llama-server 进程：启动、收日志、停止、看状态。

    这是「在界面里直接加载模型」的核心：不用离开 TUI 去开命令行。
    """

    def __init__(self, model_path, run_opts, bin_path, log):
        self.model_path = model_path
        self.opts = dict(run_opts or {})
        self.bin_path = bin_path
        self.log = log
        self.proc = None
        self.port = int(self.opts.get("port") or 8080)
        self.state = "待启动"        # 待启动 / 启动中 / 运行中 / 已停止 / 失败
        self.started_at = None
        self.exited_at = None
        self.returncode = None
        self.cmdline = ""
        self._lines = collections.deque(maxlen=400)
        self._lock = threading.Lock()
        self.base_url = "http://127.0.0.1:%d" % self.port

    # ── 命令构造 ──
    def build_argv(self):
        """返回参数列表。用列表而非字符串，路径带空格也不会被 cmd 拆坏。"""
        b = self.bin_path or "llama-server"
        o = self.opts
        argv = [
            b,
            "-m", self.model_path,
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-c", str(int(o.get("ctx") or 4096)),
            "-ngl", str(int(o.get("ngl") if o.get("ngl") is not None else 99)),
            "-b", str(int(o.get("batch") or 512)),
            "-ub", str(int(o.get("ubatch") or 512)),
        ]
        par = int(o.get("parallel") or 1)
        if par > 1:
            argv += ["-np", str(par)]
        th = int(o.get("threads") or 0)
        if th > 0:
            argv += ["-t", str(th)]
        if o.get("flash_attn", True):
            argv += ["-fa", "on"]
        if o.get("metrics", True):
            argv += ["--metrics"]
        extra = str(o.get("extra_args") or "").strip()
        if extra:
            import shlex
            try:
                argv += shlex.split(extra, posix=False)
            except Exception:
                argv += [extra]
        return argv

    def build_cmd(self):
        """给人看的命令行预览（带引号）。"""
        out = []
        for a in self.build_argv():
            out.append('"%s"' % a if (" " in a and not a.startswith('"')) else a)
        return " ".join(out)

    # ── 生命周期 ──
    def start(self):
        if self.proc and self.proc.poll() is None:
            self.log.add("已有进程在跑，忽略重复启动", "server", "warn")
            return False
        if not self.bin_path or not os.path.isfile(self.bin_path):
            self.state = "失败"
            self.log.add("找不到 llama-server 可执行文件：%s" % (self.bin_path or "(空)"),
                         "server", "err")
            return False
        if not os.path.isfile(self.model_path):
            self.state = "失败"
            self.log.add("模型文件不存在：%s" % self.model_path, "server", "err")
            return False
        self.cmdline = self.build_cmd()
        self.log.add("启动 llama-server", "server", "ok")
        self.log.add(self.cmdline, "server")
        # 把工作目录设到 exe 所在目录：llama.cpp 的 DLL 在旁边
        cwd = os.path.dirname(self.bin_path)
        try:
            self.proc = subprocess.Popen(
                self.build_argv(), cwd=cwd, shell=False, creationflags=_no_window_flag(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            self.state = "失败"
            self.log.add("启动失败：%s" % e, "server", "err")
            return False
        self.state = "启动中"
        self.started_at = now()
        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def _pump(self):
        """持续读进程输出，转进日志；顺便识别「模型加载完成」。"""
        ready = re.compile(r"(server is listening|starting the main loop|all slots are idle)", re.I)
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                with self._lock:
                    self._lines.append(line)
                low = line.lower()
                kind = "info"
                if "error" in low or "failed" in low or "abort" in low:
                    kind = "err"
                elif "warn" in low:
                    kind = "warn"
                self.log.add(line, "server", kind)
                if ready.search(line):
                    self.state = "运行中"
                elif re.search(r"(loading model|llama_model_load)", line, re.I):
                    self.state = "启动中"
        except Exception as e:
            self.log.add("读进程输出出错：%s" % e, "server", "err")
        try:
            rc = self.proc.wait()
        except Exception:
            rc = None
        self.returncode = rc
        self.exited_at = now()
        # 用户主动停止时 stop() 已置为「已停止」，不要被这里的收尾覆盖成失败
        if self.state not in ("失败", "已停止"):
            self.state = "已停止"
        self.log.add("llama-server 退出 code=%s" % rc, "server",
                     "warn" if rc == 0 else "err")

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            self.state = "已停止"
            return False
        self.log.add("停止 llama-server（端口 %d）" % self.port, "server", "warn")
        ok = False
        try:
            if IS_WIN:
                # Popen 起的是子进程本体，但保险起见杀进程树
                subprocess.run("taskkill /F /T /PID %d" % self.proc.pid,
                               shell=True, capture_output=True, timeout=8,
                               creationflags=0x08000000)
            else:
                self.proc.terminate()
            ok = True
        except Exception as e:
            self.log.add("停止失败：%s" % e, "server", "err")
        try:
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=4)
            except Exception:
                pass
        self.state = "已停止"
        self.exited_at = now()
        return ok

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    def uptime(self):
        if not self.started_at:
            return 0.0
        end = self.exited_at if not self.alive() else now()
        return end - self.started_at

    def tail(self, n=6):
        with self._lock:
            return list(self._lines)[-n:]

    def load_progress(self):
        """从输出里猜加载进度 0~1。"""
        with self._lock:
            lines = list(self._lines)
        for line in reversed(lines[-80:]):
            m = re.search(r"load_tensors:\s*(\d+)\s*/\s*(\d+)", line)
            if m and int(m.group(2)):
                return int(m.group(1)) / float(m.group(2))
            m = re.search(r"llama_model_loader:\s*(\d+)%", line)
            if m:
                return int(m.group(1)) / 100.0
            if re.search(r"load time|model loaded|llama_model_load: loaded", line, re.I):
                return 0.95
            if re.search(r"server is listening|starting the main loop", line, re.I):
                return 1.0
        return 0.0


def run_task(task, backend, log, on_done=None):
    """执行一个 chat 任务（流式）。"""
    import urllib.request

    task.state = "运行"
    task.start = now()
    url = backend.base_url + "/v1/chat/completions"
    payload = {
        "model": task.model or "default",
        "messages": [{"role": "user", "content": task.prompt}],
        "stream": True,
        "temperature": float(task.temperature),
    }
    if task.max_tokens > 0:
        payload["max_tokens"] = task.max_tokens
    err = ""
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            for raw in r:
                if task.cancelled:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except Exception:
                    continue
                ch = (data.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    task.append(delta["content"])
                u = data.get("usage")
                if isinstance(u, dict):
                    task.prompt_tokens = u.get("prompt_tokens") or task.prompt_tokens
                    task.completion_tokens = u.get("completion_tokens") or task.completion_tokens
                if ch.get("finish_reason"):
                    break
    except Exception as e:
        err = str(e)[:160]
    task.end = now()
    if task.cancelled:
        task.state = "取消"
        task.err = task.err or "已取消"
    elif err and not task.text():
        task.state = "失败"
        task.err = err
    else:
        task.state = "完成"
        if err:
            task.err = err
    task.tps = task.rate()
    if not task.completion_tokens:
        task.completion_tokens = task.ntok()
    if log:
        log.add("任务 #%d %s · %s · %s tok · %s" % (
            task.id, task.title[:24], task.state, fmt_tok(task.completion_tokens),
            ("%.1f t/s" % task.tps) if task.tps else "-"),
            "task", {"完成": "ok", "失败": "err", "取消": "warn"}.get(task.state, "info"))
    if on_done:
        on_done()


class LogBuf(object):
    def __init__(self, maxlen=5000):
        self.buf = collections.deque(maxlen=int(maxlen))
        self._lock = threading.Lock()

    def add(self, msg, src="tui", kind="info"):
        t = time.strftime("%H:%M:%S")
        with self._lock:
            self.buf.append({"t": t, "s": src, "k": kind, "m": str(msg)})

    def snapshot(self):
        with self._lock:
            return list(self.buf)

    def __len__(self):
        return len(self.buf)


# ══════════════════════════════════════════════════════════ 界面


FILTERS = [("全部", None, None),
           ("推理/任务", "task", None),
           ("警告与错误", None, "warn"),
           ("后端输出", "server", None),
           ("发现", "find", None),
           ("TUI", "tui", None)]

HELP_LINES = [
    ("R", "★★ 选中模型后按 R：改运行参数并直接加载启动 llama-server"),
    ("S", "停止由本程序启动的 llama-server"),
    ("Tab", "切换焦点：左栏模型 ↔ 中栏任务（j/k 跟着作用）"),
    ("j / k", "在当前焦点的栏里上下移动"),
    ("f", "写一条 prompt 发给当前模型（流式接收，右侧看输出）"),
    ("m", "弹出模型列表挑一个"),
    ("n / d", "登记 / 删除模型（写进 config.json）"),
    ("T", "对当前后端做一次 1-token 推理，验证真的能算"),
    ("l", "日志过滤：全部 / 任务 / 警告错误 / 后端 / 发现 / TUI"),
    ("PgUp PgDn", "往回翻日志，Esc 回到底部"),
    ("v", "后端详情页：模型、上下文、命令行、原始 /props"),
    ("x / e", "取消选中任务 / 导出选中任务到 tasks/"),
    ("r", "重新扫描程序、模型、进程和服务"),
    ("空格", "冻结 / 恢复日志自动滚动"),
    ("q", "退出"),
]


class LlamaTUI(object):
    def __init__(self, cfg, backend=None, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.bd = backend or make_backend()
        self.w, self.h = 0, 0
        self.cv = None
        self.running = True
        self.modal = None
        self.minput = ""
        self.msel = 0
        self.msg = ""
        self.msg_t = 0.0
        self.detail = False
        self.focus = "tasks"       # j/k 作用在哪一栏：tasks / models
        self.help_scroll = 0

        self.log = LogBuf(cfg.get("max_logs", 5000))
        self.backends = []
        self.servers = []          # 由 TUI 拉起的 Server 对象
        self.form_model = None
        self.form_values = {}
        self.form_sel = 0
        self.form_edit = None
        self._log_src_w = 0
        self._log_prefix_w = 9
        self.drag = None           # 鼠标拖选：(x0, y0, x1, y1)
        self.hit = {}              # 上一帧的可点区域：name -> (x, y, w, h)
        self.log_area = None
        self.last_mouse = 0.0
        self.models = []          # [{name, path, size, dir, live, backend}]
        self.tasks = []
        self.llama_bin = {}
        self.task_sel = 0
        self.task_top = 0
        self.model_sel = 0
        self.log_off = 0
        self.log_filt = 0
        self.freeze = False
        self.tick_n = 0
        self.last_probe = 0.0
        self.last_scan = 0.0
        self.dirty = True
        self._lock = threading.Lock()
        self.scanning = False
        self.scan_status = ""

    # ── 生命周期 ──
    def run(self):
        self.bd.set_title("llama.cpp TUI")
        if self.bd.alt_screen:
            self.bd.write("\x1b[?1049h")
        self.bd.write("\x1b[2J")
        self.bd.hide_cursor()
        if hasattr(self.bd, "enable_mouse"):
            try:
                self.bd.enable_mouse()
            except Exception:
                pass
        try:
            self.log.add("llama.cpp TUI v%s 启动" % VERSION, "tui", "ok")
            self.log.add("配置: %s" % (self.cfg.get("_path") or "（默认，未落盘）"), "tui")
            self.scan_status = "正在查找 llama.cpp / 模型 / 服务…"
            threading.Thread(target=self._first_scan, daemon=True).start()
            while self.running:
                self.handle_keys(self.bd.poll())
                if not self.scanning:
                    if self.dirty or now() - self.last_probe >= float(self.cfg.get("refresh_interval", 2.0)):
                        self.tick()
                else:
                    self.tick_n += 1          # 扫描动画
                self.draw()
                time.sleep(0.06)
        except KeyboardInterrupt:
            pass
        finally:
            self.cleanup()

    def _first_scan(self):
        self.scanning = True
        try:
            self.scan_all()
        finally:
            self.scanning = False
            self.last_probe = 0
            self.dirty = True

    def cleanup(self):
        try:
            self.bd.show_cursor()
            if self.bd.alt_screen:
                self.bd.write("\x1b[?1049l")
            self.bd.write(SGR0)
            if hasattr(self.bd, "restore"):
                self.bd.restore()
        except Exception:
            pass

    # ── 扫描 ──
    def scan_all(self, first=False):
        if self.demo:
            return
        self.log.add("扫描: llama.cpp / 模型 / 进程 / 服务 ...", "find")
        try:
            self.llama_bin = find_llama_bin(self.cfg, self.log)
            if self.llama_bin.get("llama-server"):
                self.log.add("llama-server: %s" % self.llama_bin["llama-server"], "find", "ok")
            else:
                self.log.add("没找到 llama-server（可在 config.json 的 extra_llama_dirs 里指定目录）",
                             "find", "warn")
        except Exception as e:
            self.log.add("查找 llama.cpp 失败: %s" % e, "find", "err")
        try:
            found = find_models(self.cfg, self.log)
            models = self._merge_models(found)
            with self._lock:
                self.models = models
        except Exception as e:
            self.log.add("查找模型失败: %s" % e, "find", "err")
        try:
            self.rebuild_backends()
        except Exception as e:
            self.log.add("服务发现失败: %s" % e, "find", "err")
        self.last_scan = now()
        self.last_probe = 0
        self.dirty = True

    def _merge_models(self, found):
        models = []
        seen = set()
        for m in found:
            key = m["path"].lower()
            if key in seen:
                continue
            seen.add(key)
            models.append(dict(m, live=False, backend=""))
        for entry in self.cfg.get("models") or []:
            p = str(entry.get("path") or "")
            if p and p.lower() in seen:
                for m in models:
                    if m["path"].lower() == p.lower():
                        m["note"] = entry.get("note") or ""
                continue
            models.append({"name": entry.get("name") or short_name(p) or "?",
                           "path": p, "size": 0, "dir": os.path.dirname(p),
                           "live": False, "backend": "", "note": entry.get("note") or ""})
        # 运行中的模型排前面
        live = [b for b in self.backends if b.online]
        for b in live:
            name = b.model_name()
            hit = next((m for m in models if short_name(m["path"]).lower() == name.lower()), None)
            if hit:
                hit["live"] = True
                hit["backend"] = b.base_url
            elif name and name != "(未知模型)":
                models.insert(0, {"name": name, "path": b.model_path, "size": 0,
                                  "dir": os.path.dirname(b.model_path.replace("\\", "/")),
                                  "live": True, "backend": b.base_url, "note": ""})
        models.sort(key=lambda m: (not m.get("live"), -int(m.get("size") or 0)))
        return models

    def rebuild_backends(self):
        old = dict((b.base_url or "pid:%s" % b.pid, b) for b in self.backends)
        out = []
        seen = set()
        for hit in scan_llama_services(self.cfg, self.log):
            key = hit["url"]
            if key in seen:
                continue
            seen.add(key)
            b = Backend(hit["url"], "llama-server:%d" % hit["port"], pid=hit["pid"],
                        source="scan")
            b.model_path = hit.get("model") or ""
            prev = old.get(key)
            if prev:
                b.props = prev.props
                b.model_path = prev.model_path or b.model_path
                b.n_ctx = prev.n_ctx
                b.tps = prev.tps
                b.start_time = prev.start_time
                b.mem = prev.mem
            out.append(b)
        # 配置里明确写了但没扫到的，也保留（可能暂未启动）
        for s in self.cfg.get("services") or []:
            url = s.get("base_url")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(Backend(url, s.get("name") or url, source="config"))
        # 进程信息回填
        procs = {p.get("pid"): p for p in list_processes()}
        for b in out:
            p = procs.get(b.pid)
            if p:
                b.cmdline = p.get("cmdline") or ""
                b.cwd = p.get("cwd") or ""
                b.mem = b.mem or p.get("mem")
                b.start_time = b.start_time or p.get("start")
                if not b.model_path:
                    m = re.search(r"(?:-m|--model)[= ]+(\"[^\"]+\"|\S+)", b.cmdline)
                    if m:
                        b.model_path = m.group(1).strip('"')
        self.backends = out
        self.model_sel = clamp(self.model_sel, 0, max(0, len(self.models) - 1))

    def tick(self):
        self.tick_n += 1
        if not self.demo:
            probeable = [b for b in self.backends if b.base_url]
            for b in probeable:
                try:
                    probe_backend(b)
                except Exception as e:
                    b.last_error = str(e)[:80]
            if now() - self.last_scan > float(self.cfg.get("scan_interval", 20.0)):
                self.last_scan = now()
                try:
                    self.rebuild_backends()
                except Exception:
                    pass
        self.refresh_model_live()
        self.last_probe = now()
        self.dirty = False
        self.prune_log_scroll()

    def refresh_model_live(self):
        if not self.models:
            return
        live = {}
        for b in self.backends:
            if b.online:
                live[short_name(b.model_path).lower()] = b.base_url
        for m in self.models:
            key = short_name(m.get("path") or "").lower() or (m.get("name") or "").lower()
            m["live"] = key in live
            m["backend"] = live.get(key, "")

    def current_backend(self):
        if not self.backends:
            return None
        m = self.models[self.model_sel] if 0 <= self.model_sel < len(self.models) else None
        if m and m.get("backend"):
            for b in self.backends:
                if b.base_url == m["backend"]:
                    return b
        for b in self.backends:
            if b.online:
                return b
        return self.backends[0]

    # ── 输入 ──
    def flash(self, text, secs=2.5):
        self.msg = text
        self.msg_t = now() + secs

    def handle_keys(self, keys):
        for k in keys:
            if isinstance(k, tuple) and k and k[0] == "mouse":
                self.on_mouse(k)
                continue
            if self.modal:
                self.key_modal(k)
            elif self.detail:
                if k in ("v", "esc", "q", "enter"):
                    self.detail = False
                    self.dirty = True
                elif k in ("j", "down"):
                    self.model_sel = min(len(self.models) - 1, self.model_sel + 1)
                    self.dirty = True
                elif k in ("k", "up"):
                    self.model_sel = max(0, self.model_sel - 1)
                    self.dirty = True
            else:
                self.key_main(k)

    def key_main(self, k):
        if k in ("q", "Q"):
            self.running = False
        elif k == "resize":
            self.dirty = True
        elif k == "tab":
            # 焦点在「模型 ↔ 任务」之间切换，j/k 跟着作用到当前栏
            self.focus = "tasks" if self.focus == "models" else "models"
            self.flash("焦点: %s（j/k 切换，R 运行）" % ("左栏·模型" if self.focus == "models" else "中栏·任务"))
            self.dirty = True
        elif k in ("j", "down") and self.focus == "models":
            if self.models:
                self.model_sel = min(len(self.models) - 1, self.model_sel + 1)
            self.dirty = True
        elif k in ("k", "up") and self.focus == "models":
            if self.models:
                self.model_sel = max(0, self.model_sel - 1)
            self.dirty = True
        elif k in ("j", "down"):
            if self.tasks:
                self.task_sel = min(len(self.tasks) - 1, self.task_sel + 1)
            self.dirty = True
        elif k in ("k", "up"):
            if self.tasks:
                self.task_sel = max(0, self.task_sel - 1)
            self.dirty = True
        elif k == "home":
            self.task_sel = 0
            self.log_off = 0
            self.dirty = True
        elif k == "end":
            self.task_sel = max(0, len(self.tasks) - 1)
            self.log_off = 0
            self.dirty = True
        elif k == "pageup":
            self.log_off += 5
            self.dirty = True
        elif k == "pagedown":
            self.log_off = max(0, self.log_off - 5)
            self.dirty = True
        elif k == "esc":
            self.log_off = 0
            self.dirty = True
        elif k == "l":
            self.log_filt = (self.log_filt + 1) % len(FILTERS)
            self.log_off = 0
            self.dirty = True
        elif k == " ":
            self.freeze = not self.freeze
            self.flash("日志滚动已冻结" if self.freeze else "日志滚动已恢复")
            self.dirty = True
        elif k == "r":
            self.flash("重新扫描中…")
            threading.Thread(target=self.scan_all_async, daemon=True).start()
        elif k == "v":
            self.detail = True
            self.dirty = True
        elif k == "m":
            self.open_picker()
        elif k == "n":
            self.modal = "add_model"
            self.minput = ""
        elif k == "d":
            self.modal = "del_model"
            self.msel = clamp(self.model_sel, 0, max(0, len(self.models) - 1))
        elif k == "f":
            self.open_composer()
        elif k == "T":
            self.run_activity_test()
        elif k == "s":
            self.spawn_servers()
        elif k == "R":
            self.open_run_form()
        elif k == "S":
            self.stop_server()
        elif k == "x":
            t = self.sel_task()
            if t:
                t.cancel()
                self.log.add("取消任务 #%d" % t.id, "task", "warn")
            else:
                self.flash("先选一个任务")
        elif k == "e":
            self.export_task()
        elif k in ("?", "h"):
            self.modal = "help"
            self.help_scroll = 0
            self.dirty = True

    def key_modal(self, k):
        m = self.modal
        if m == "help":
            if k in ("esc", "q", "?", "enter", "h"):
                self.modal = None
            elif k in ("j", "down"):
                self.help_scroll += 1
            elif k in ("k", "up"):
                self.help_scroll = max(0, self.help_scroll - 1)
            self.dirty = True
            return
        if m in ("picker", "del_model"):
            items = self.pick_items()
            if k in ("j", "down"):
                self.msel = min(max(0, len(items) - 1), self.msel + 1)
            elif k in ("k", "up"):
                self.msel = max(0, self.msel - 1)
            elif k == "enter":
                self.pick_confirm()
            elif k == "R":
                # 选中即进运行配置
                self.pick_confirm()
                self.open_run_form()
            elif k in ("esc", "q"):
                self.modal = None
            self.dirty = True
            return
        if m == "run_form":
            self.key_run_form(k)
            return
        if m in ("composer", "add_model"):
            if k == "esc":
                self.modal = None
                self.minput = ""
            elif k == "enter":
                if m == "composer":
                    self.submit_prompt()
                else:
                    self.add_model()
            elif k == "backspace":
                self.minput = self.minput[:-1]
                self.dirty = True
            elif k == "space":
                self.minput += " "
                self.dirty = True
            elif len(k) == 1 and k >= " ":
                self.minput += k
                self.dirty = True
            return
        self.modal = None

    # ── 运行配置表单 ──
    RUN_FIELDS = [
        ("port", "端口", int, 1, 65535),
        ("ctx", "上下文", int, 128, 1048576),
        ("ngl", "GPU 层 (-ngl)", int, 0, 999),
        ("threads", "CPU 线程", int, 0, 256),
        ("batch", "batch (-b)", int, 1, 8192),
        ("ubatch", "ubatch (-ub)", int, 1, 8192),
        ("parallel", "并发槽 (-np)", int, 1, 64),
        ("flash_attn", "FlashAttn", bool, None, None),
        ("metrics", "开 metrics", bool, None, None),
        ("extra_args", "额外参数", str, None, None),
    ]

    def open_run_form(self):
        m = self.models[self.model_sel] if 0 <= self.model_sel < len(self.models) else None
        if not m:
            self.flash("先选一个模型（m），再按 R 运行")
            return
        if m.get("live"):
            self.flash("这个模型已经在跑了；按 S 可停止")
            return
        path = m.get("path") or ""
        if not os.path.isfile(path):
            self.flash("模型文件不存在：%s" % path)
            return
        run = deep_merge(DEFAULTS["run"], self.cfg.get("run") or {})
        # 端口自动避开已占用的
        used = {b.port for b in self.backends if getattr(b, "port", None)}
        port = int(run.get("port") or 8080)
        while port in used and port < 65535:
            port += 1
        run["port"] = port
        self.form_model = m
        self.form_values = run
        self.form_sel = 0
        self.form_edit = None
        self.modal = "run_form"
        self.dirty = True

    def form_field_value(self, key):
        return self.form_values.get(key)

    def key_run_form(self, k):
        n = len(self.RUN_FIELDS)
        if k in ("esc", "q"):
            if self.form_edit is not None:
                self.form_edit = None
            else:
                self.modal = None
            self.dirty = True
            return
        if self.form_edit is not None:
            key, buf = self.form_edit
            if k == "esc":
                # 放弃本次编辑，保留原值
                self.form_edit = None
                self.flash("已取消修改")
            elif k == "enter":
                self.commit_form_edit(key, buf)
            elif k == "backspace":
                self.form_edit = (key, buf[:-1])
            elif k == "space":
                self.form_edit = (key, buf + " ")
            elif len(k) == 1 and k >= " ":
                self.form_edit = (key, buf + k)
            self.dirty = True
            return
        if k in ("j", "down"):
            self.form_sel = (self.form_sel + 1) % n
        elif k in ("k", "up"):
            self.form_sel = (self.form_sel - 1) % n
        elif k in ("enter", "right", "l"):
            self.activate_form_field()
        elif k in ("left", "h"):
            self.adjust_form_field(-1)
        elif k in ("right",):
            self.adjust_form_field(1)
        elif k == "R":
            self.start_server_from_form()
        elif k == " ":
            self.activate_form_field()
        self.dirty = True

    def activate_form_field(self):
        key, label, typ, lo, hi = self.RUN_FIELDS[self.form_sel]
        val = self.form_field_value(key)
        if typ is bool:
            self.form_values[key] = not bool(val)
        elif typ is str:
            self.form_edit = (key, str(val or ""))
        else:
            self.form_edit = (key, str(val if val is not None else ""))

    def adjust_form_field(self, delta):
        key, label, typ, lo, hi = self.RUN_FIELDS[self.form_sel]
        if typ is not bool:
            return
        self.form_values[key] = not bool(self.form_field_value(key))

    def commit_form_edit(self, key, text):
        self.form_edit = None
        typ = next((t for k, _l, t, _lo, _hi in self.RUN_FIELDS if k == key), str)
        rng = next(((lo, hi) for k, _l, _t, lo, hi in self.RUN_FIELDS if k == key), (None, None))
        text = (text or "").strip()
        if typ is int:
            if not text:
                self.flash("已恢复原值")
                return
            try:
                v = int(text)
            except ValueError:
                self.flash("要填数字：%s" % text)
                return
            if rng[0] is not None:
                v2 = int(clamp(v, rng[0], rng[1]))
                if v2 != v:
                    self.flash("已限制到 %d..%d：%d" % (rng[0], rng[1], v2))
                v = v2
            self.form_values[key] = v
        else:
            self.form_values[key] = text

    def start_server_from_form(self):
        m = self.form_model
        run = dict(self.form_values)
        self.cfg["run"] = run
        try:
            p = save_config(self.cfg)
            self.log.add("运行配置已保存 → %s" % p, "config", "ok")
        except Exception as e:
            self.log.add("配置保存失败：%s" % e, "config", "err")
        bin_path = self.llama_bin.get("llama-server")
        if not bin_path:
            self.flash("没找到 llama-server，先在 config.json 写 extra_llama_dirs")
            self.log.add("缺少 llama-server：无法启动", "server", "err")
            return
        # 停掉同端口的旧实例，避免抢占
        port = int(run.get("port") or 8080)
        for s in self.servers:
            if s.port == port and s.alive():
                self.log.add("端口 %d 已有实例，先停掉" % port, "server", "warn")
                s.stop()
        srv = Server(m.get("path"), run, bin_path, self.log)
        self.servers.insert(0, srv)
        self.modal = None
        self.form_edit = None
        ok = srv.start()
        if ok:
            self.flash("正在启动：%s" % os.path.basename(m.get("path") or ""), 4)
            self.log.add("模型: %s" % m.get("path"), "server")
            self.log.add("端口: %d · ctx %s · ngl %s" % (
                srv.port, run.get("ctx"), run.get("ngl")), "server")
            # 起来后自动探测
            threading.Thread(target=self._await_server, args=(srv,), daemon=True).start()
        else:
            self.flash("启动失败，看日志")
        self.dirty = True

    def _await_server(self, srv, timeout=300):
        """等模型真正加载完（health=ok）再并入后端列表。

        注意 /health 返回 503 表示「还在加载模型」，这时候端口通了也
        不能算就绪，否则发任务会直接失败。
        """
        t0 = now()
        deadline = t0 + timeout
        while now() < deadline and srv.alive():
            if port_open(srv.port, timeout=0.25):
                b = Backend(srv.base_url, "自启:%d" % srv.port, pid=getattr(srv.proc, "pid", None),
                            source="spawn")
                b.model_path = srv.model_path
                b.start_time = srv.started_at
                b.extra["server"] = srv
                while now() < deadline:
                    if not srv.alive():
                        self.flash("服务启动后退出，看日志")
                        return
                    probe_backend(b, timeout=2.0)
                    if b.online and b.health == "ok":
                        break
                    time.sleep(1.0)
                else:
                    self.flash("等待模型加载超时：%d" % srv.port)
                if not (b.online and b.health == "ok"):
                    return
                with self._lock:
                    self.backends = [x for x in self.backends if x.base_url != b.base_url] + [b]
                srv.state = "运行中"
                self.log.add("模型已加载：%s (%.1fs)" % (
                    short_name(b.model_path), srv.uptime()), "server", "ok")
                self.flash("已就绪：%s" % short_name(b.model_path), 4)
                self.refresh_model_live()
                self.dirty = True
                return
            time.sleep(0.5)
        if not srv.alive():
            self.flash("服务启动后退出，看日志")
        else:
            self.flash("等待端口超时：%d" % srv.port)

    def stop_server(self):
        alive = [s for s in self.servers if s.alive()]
        if not alive:
            # 也支持停掉扫描来的外部进程
            b = self.current_backend()
            if b and b.pid and b.source == "spawn":
                pass
            self.flash("没有由 TUI 启动的服务在跑")
            return
        for s in alive:
            s.stop()
        self.log.add("已停止 %d 个服务" % len(alive), "server", "ok")
        self.flash("已停止 %d 个服务" % len(alive))
        self.dirty = True

    # ── 鼠标 ──
    LEFT_BTN = 0x0001
    MOUSE_MOVED = 0x0001
    MOUSE_WHEEL = 0x0004
    WHEEL_DELTA = 120

    def on_mouse(self, ev):
        _, mx, my, state, flags = ev
        self.last_mouse = now()
        if flags & self.MOUSE_WHEEL:
            # state 已经是「有符号增量」：>0 向上滚，<0 向下滚
            delta = 1 if state > 0 else -1
            if self.log_area and self._in(self.log_area, mx, my):
                self.freeze = True
                self.log_off = max(0, self.log_off + 3 * delta)
            else:
                self.flash("鼠标滚轮：请把指针移到日志区")
            self.dirty = True
            return

        pressed = bool(state & self.LEFT_BTN)
        if pressed and flags & self.MOUSE_MOVED and self.drag:
            # 拖拽中：扩展选区
            self.drag = (self.drag[0], self.drag[1], mx, my)
            self.dirty = True
            return
        if pressed:
            # 先看有没有落在可点控件上（任务行、模型行、底栏按钮）
            for name, rect in self.hit.items():
                if self._in(rect, mx, my):
                    self.drag = None
                    self.hit_action(name)
                    return
            # 再看是否点在日志区，是则开始拖选
            if self.log_area and self._in(self.log_area, mx, my):
                self.drag = (mx, my, mx, my)
                self.dirty = True
                return
            self.drag = None
            return
        # 松开
        if self.drag and self.drag[2:] != self.drag[:2]:
            text = self.selection_text()
            if text.strip():
                self.copy_to_clipboard(text)
                self.flash("已复制 %d 字符" % len(text), 2)
            self.drag = None
            self.dirty = True
            return
        self.drag = None

    @staticmethod
    def _in(rect, x, y):
        return rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]

    def click_at(self, mx, my):
        """点各栏：任务行选中、模型行选中、标题栏按钮。"""
        for name, rect in self.hit.items():
            if self._in(rect, mx, my):
                self.hit_action(name)
                return

    def hit_action(self, name):
        kind, _, arg = name.partition(":")
        if kind == "task":
            try:
                self.task_sel = int(arg)
            except ValueError:
                return
            self.detail = False
            self.focus = "tasks"
        elif kind == "model":
            try:
                self.model_sel = int(arg)
            except ValueError:
                return
            self.focus = "models"
            m = self.models[self.model_sel] if 0 <= self.model_sel < len(self.models) else None
            if m:
                self.flash("已选: %s%s" % (short_name(m.get("name") or "?"),
                                          "（运行中）" if m.get("live") else " · 按 R 启动"))
        elif kind == "btn":
            fn = {"run": self.open_run_form, "stop": self.stop_server,
                  "compose": self.open_composer, "pick": self.open_picker,
                  "filter": lambda: setattr(self, "log_filt",
                                            (self.log_filt + 1) % len(FILTERS)),
                  "help": lambda: setattr(self, "modal", "help"),
                  "rescan": lambda: threading.Thread(
                      target=self.scan_all_async, daemon=True).start()}
            f = fn.get(arg)
            if f:
                f()
        self.dirty = True

    def selection_text(self):
        """把拖选矩形里的字符抠出来（跳过边框和空白）。"""
        if not self.drag or not self.cv:
            return ""
        x0, y0, x1, y1 = self.drag
        if y0 > y1:
            y0, y1 = y1, y0
        if y0 == y1 and x0 > x1:
            x0, x1 = x1, x0
        cv = self.cv
        lines = []
        for y in range(max(0, y0), min(cv.h, y1 + 1)):
            row = cv.buf[y]
            lo = x0 if y == y0 else 0
            hi = x1 if y == y1 else cv.w - 1
            chars = []
            for x in range(max(0, lo), min(cv.w, hi + 1)):
                c = row[x]
                if c == "\x00":
                    continue
                chars.append(c)
            lines.append("".join(chars).rstrip())
        return "\n".join(lines).strip("\n")

    def copy_to_clipboard(self, text):
        """优先用 Win32 剪贴板；失败就退到 OSC52。"""
        if IS_WIN:
            try:
                self.bd.write("\x1b]52;c;%s\x07" % __import__("base64").b64encode(
                    text.encode("utf-8")).decode())
            except Exception:
                pass
            try:
                import ctypes
                from ctypes import wintypes
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                u32 = ctypes.WinDLL("user32", use_last_error=True)
                u32.OpenClipboard.argtypes = [wintypes.HWND]
                u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
                k32.GlobalAlloc.restype = wintypes.HANDLE
                k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
                k32.GlobalLock.argtypes = [wintypes.HANDLE]
                k32.GlobalLock.restype = ctypes.c_void_p
                k32.GlobalUnlock.argtypes = [wintypes.HANDLE]
                if not u32.OpenClipboard(None):
                    return False
                try:
                    u32.EmptyClipboard()
                    data = text.encode("utf-16-le") + b"\x00\x00"
                    GMEM_MOVEABLE = 0x0002
                    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                    if not h:
                        return False
                    p = k32.GlobalLock(h)
                    ctypes.memmove(p, data, len(data))
                    k32.GlobalUnlock(h)
                    CF_UNICODETEXT = 13
                    u32.SetClipboardData(CF_UNICODETEXT, h)
                finally:
                    u32.CloseClipboard()
                return True
            except Exception:
                return False
        return False

    # ── 动作 ──
    def scan_all_async(self):
        if self.scanning:
            return
        self.scanning = True
        try:
            self.scan_all()
        finally:
            self.scanning = False
            self.last_probe = 0
            self.dirty = True

    def sel_task(self):
        with self._lock:
            if 0 <= self.task_sel < len(self.tasks):
                return self.tasks[self.task_sel]
        return None

    def open_composer(self):
        if not self.backends:
            self.flash("没有后端：先启动 llama-server，或按 r 重扫")
            return
        b = self.current_backend()
        if not b or not b.online:
            self.flash("当前没有在线服务，按 T 可测试，或先启动 llama-server")
            return
        self.modal = "composer"
        self.minput = ""

    def submit_prompt(self):
        prompt = self.minput.strip()
        if not prompt:
            return
        b = self.current_backend()
        self.modal = None
        self.minput = ""
        if not b or not b.base_url:
            self.flash("没有可用的 HTTP 端点")
            return
        m = self.models[self.model_sel] if 0 <= self.model_sel < len(self.models) else {}
        t = Task(prompt, model=m.get("name") or b.model_name(),
                 endpoint=b.base_url,
                 max_tokens=int(self.cfg.get("max_tokens") or 0),
                 temperature=float(self.cfg.get("temperature") or 0.7))
        with self._lock:
            self.tasks.insert(0, t)
        self.task_sel = 0
        self.log.add("提交任务 #%d → %s" % (t.id, b.base_url), "task")
        threading.Thread(target=run_task,
                         args=(t, b, self.log, self._mark_dirty), daemon=True).start()

    def _mark_dirty(self):
        self.dirty = True

    def run_activity_test(self):
        b = self.current_backend()
        if not b or not b.base_url or not b.online:
            self.flash("没有在线后端可测")
            return
        self.log.add("开始 1-token 推理探测: %s" % b.base_url, "find")

        def work():
            ok, msg, dt = activity_test(b)
            self.log.add("%s · %s · %.2fs" % (b.name, msg, dt), "find",
                         "ok" if ok else "err")
            self.flash(("推理正常 %.2fs" % dt) if ok else "探测失败，看日志")
            self.dirty = True
        threading.Thread(target=work, daemon=True).start()

    def spawn_servers(self):
        cmds = self.cfg.get("spawn") or []
        if not cmds:
            self.flash("config.json 里没有 spawn 命令")
            return
        server = self.llama_bin.get("llama-server") or "llama-server"
        for item in cmds:
            if isinstance(item, dict):
                cmd = str(item.get("cmd") or "").replace("{llama-server}", server)
                cwd = item.get("cwd")
            else:
                cmd = str(item).replace("{llama-server}", server)
                cwd = None
            threading.Thread(target=self._spawn_one, args=(cmd, cwd), daemon=True).start()
        self.flash("已按配置启动 %d 个服务" % len(cmds))

    def _spawn_one(self, cmd, cwd):
        self.log.add("启动: %s" % cmd, "server")
        try:
            p = subprocess.Popen(cmd, cwd=cwd or None, shell=True,
                                 creationflags=0x08000000 if IS_WIN else 0,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for raw in p.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    kind = "err" if ("error" in line.lower() or "fail" in line.lower()) else "info"
                    self.log.add(line, "server", kind)
                    self.dirty = True
            p.wait()
            self.log.add("进程退出 code=%s" % p.returncode, "server", "warn")
        except Exception as e:
            self.log.add("启动失败: %s" % e, "server", "err")
        self.dirty = True

    def export_task(self):
        t = self.sel_task()
        if not t:
            self.flash("先选一个任务")
            return
        d = os.path.join(self.cfg.get("site_dir") or os.getcwd(), "tasks")
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "task-%d-%s.txt" % (t.id, time.strftime("%H%M%S")))
            with open(p, "w", encoding="utf-8") as f:
                f.write("prompt:\n%s\n\nmodel: %s\nendpoint: %s\nstatus: %s\n\noutput:\n%s\n"
                        % (t.prompt, t.model, t.endpoint, t.state, t.text()))
            self.log.add("任务 #%d 导出到 %s" % (t.id, p), "task", "ok")
            self.flash("已导出 task-%d" % t.id)
        except Exception as e:
            self.flash("导出失败: %s" % e)

    # ── 选择器 ──
    def open_picker(self):
        self.modal = "picker"
        self.msel = clamp(self.model_sel, 0, max(0, len(self.models) - 1))
        if not self.models:
            self.flash("还没扫到模型，按 r 重扫或 n 手动登记")

    def pick_items(self):
        return self.models

    def pick_confirm(self):
        items = self.pick_items()
        if not items:
            self.modal = None
            return
        idx = clamp(self.msel, 0, len(items) - 1)
        m = items[idx]
        if self.modal == "del_model":
            self.delete_model(idx)
            return
        self.model_sel = idx
        self.modal = None
        self.flash("当前模型: %s" % m.get("name"))
        if m.get("backend"):
            b = next((x for x in self.backends if x.base_url == m["backend"]), None)
            if b:
                threading.Thread(target=probe_backend, args=(b,), daemon=True).start()
        self.dirty = True

    def delete_model(self, idx):
        m = self.models[idx]
        if m.get("live"):
            self.flash("运行中的模型不能从列表删除")
            self.modal = None
            return
        self.models.pop(idx)
        self.cfg["models"] = [x for x in (self.cfg.get("models") or [])
                             if str(x.get("path", "")).lower() != str(m.get("path", "")).lower()]
        try:
            p = save_config(self.cfg)
            self.log.add("已移除 %s（%s）" % (m.get("name"), p), "find", "warn")
        except Exception as e:
            self.log.add("写配置失败: %s" % e, "find", "err")
        self.modal = None
        self.model_sel = clamp(self.model_sel, 0, max(0, len(self.models) - 1))
        self.dirty = True

    def add_model(self):
        raw = self.minput.strip().strip('"')
        self.modal = None
        self.minput = ""
        if not raw:
            return
        if not raw.lower().endswith(".gguf"):
            raw = raw + ".gguf" if os.path.isfile(raw + ".gguf") else raw
        entry = {"name": short_name(raw), "path": raw}
        self.cfg.setdefault("models", []).append(entry)
        self.models.insert(0, {"name": entry["name"], "path": raw, "size": 0,
                               "dir": os.path.dirname(raw), "live": False,
                               "backend": "", "note": ""})
        try:
            p = save_config(self.cfg)
            self.log.add("登记模型 %s → %s" % (entry["name"], p), "find", "ok")
            self.flash("已登记: " + entry["name"])
        except Exception as e:
            self.log.add("写配置失败: %s" % e, "find", "err")
        self.dirty = True

    def cycle_filter(self):
        self.log_filt = (self.log_filt + 1) % len(FILTERS)

    # ══════════════════════════════ 渲染
    def draw(self):
        w, h = self.bd.size()
        if (w, h) != (self.w, self.h):
            self.w, self.h = w, h
            self.log.add("窗口 %dx%d" % (w, h), "tui")
        cv = Canvas(w, h)
        self.cv = cv
        self.hit = {}
        self.log_area = None
        if w < 76 or h < 16:
            cv.put(1, 1, "窗口太小（需要至少 76x16）", C["warn"])
            self.bd.present(cv.render(), w, h)
            return
        self.draw_header(cv)
        if self.detail:
            self.draw_detail(cv)
        else:
            mw = max(24, min(46, int(w * 0.29)))
            tw = max(28, min(54, int(w * 0.33)))
            lw = w - mw - tw
            if lw < 26:
                tw = max(24, tw - (26 - lw))
                lw = w - mw - tw
            self.draw_models(cv, 0, 1, mw, h - 3)
            self.draw_tasks(cv, mw, 1, tw, h - 3)
            self.draw_logs(cv, mw + tw, 1, lw, h - 3)
        self.draw_footer(cv)
        if self.modal:
            self.draw_modal(cv)
        self.bd.present(cv.render(), w, h)

    # ── 顶栏 ──
    def draw_header(self, cv):
        w = cv.w
        cv.hline(0, 0, w, "─", C["frame"])
        cv.put(0, 0, "▒", C["accent"])
        cv.put(1, 0, " llama.cpp TUI ", C["title"])
        x = 17
        if self.backends:
            for b in self.backends[:8]:
                cv.put(x, 0, "●", b.status_color())
                x += 1
        else:
            cv.put(x, 0, "○", C["ghost"])
            x += 1
        # 圆点后面的横线保持装饰样式
        cv.hline(x, 0, 3, "─", C["frame"])
        x += 3
        n_on = sum(1 for b in self.backends if b.online)
        info = "%d 服务 · %d 在线 · %d 模型" % (len(self.backends), n_on, len(self.models))
        cv.put(x, 0, info, C["dim"])
        x += text_width(info) + 2
        nrun = sum(1 for t in self.tasks if t.state in ("运行", "排队"))
        if nrun:
            cv.put(x, 0, "◆ %d 跑着" % nrun, C["warn"])
            x += text_width("◆ %d 跑着" % nrun) + 2
        ls = self.llama_bin.get("llama-server")
        right = "llama.cpp: " + (os.path.basename(ls) if ls else "未找到")
        room = w - 2 - x
        if text_width(right) <= room:
            cv.put(w - 2 - text_width(right), 0, right, C["ok"] if ls else C["ghost"])

    # ── 左：模型 ──
    def draw_models(self, cv, x, y, w, h):
        self.box(cv, x, y, w, h, "模型", "%d" % len(self.models),
                 focused=(self.focus == "models"))
        ix, iy, iw, ih = x + 1, y + 1, w - 2, h - 2
        rows = []
        # 由 TUI 拉起、还没进入后端列表的实例（加载中）
        pending = [s for s in self.servers if s.alive() and s.state in ("启动中", "运行中")
                   and not any(b.base_url == s.base_url and b.online for b in self.backends)]
        for s in pending:
            rows += self.server_rows(s, iw)
        if self.backends:
            for bi, b in enumerate(self.backends):
                block = self.backend_rows(b, iw)
                # 后端卡片整块可点：点哪都算选中这个后端对应的模型
                mi = self.model_index_for_backend(b)
                if mi is not None:
                    self.hit["model:%d" % mi] = (ix - 1, iy + len(rows), iw + 2, len(block))
                rows += block
        elif not pending:
            rows.append(("ghost", "没发现 llama 服务"))
            rows.append(("ghost", "选中模型按 R 直接启动"))
        cfg_rows = []
        idle_models = [(i, m) for i, m in enumerate(self.models) if not m.get("live")]
        # 先算好这个区块在屏幕上的起始行（rows 还没把 cfg_rows 拼进去）
        block_top = len(rows) + (2 if idle_models else 0)   # +2 = sep + label
        offset = 0
        for i, m in idle_models:
            block = self.model_rows(m, iw, i == self.model_sel)
            self.hit["model:%d" % i] = (ix - 1, iy + block_top + offset, iw + 2, len(block))
            offset += len(block)
            cfg_rows += block
        if cfg_rows:
            rows.append(("sep", ""))
            rows.append(("label", "未运行的模型 (%d)" % len(idle_models)))
            rows += cfg_rows
        elif self.models:
            rows.append(("sep", ""))
            rows.append(("ghost", "所有模型都在跑"))
        # 选中高亮：给整张卡片加底色
        sel_rows = set()
        for name, rect in self.hit.items():
            if name.startswith("model:") and name.split(":")[1] == str(self.model_sel):
                for yy in range(rect[1], rect[1] + rect[3]):
                    sel_rows.add(yy)
        for i, (kind, payload) in enumerate(rows[:ih]):
            yy = iy + i
            if yy in sel_rows:
                cv.fill(ix - 1, yy, iw + 2, 1, " ", C["sel"])
            self.draw_row(cv, ix, yy, iw, kind, payload)

    def model_index_for_backend(self, b):
        """后端对应的模型在 self.models 里的下标。"""
        name = short_name(b.model_path).lower()
        if not name:
            return None
        for i, m in enumerate(self.models):
            key = short_name(m.get("path") or m.get("name") or "").lower()
            if key == name:
                return i
        return None

    def server_rows(self, s, iw):
        """TUI 自启实例：显示加载进度，让「按运行」有可见反馈。"""
        out = [("big", "◐ " + short_name(s.model_path))]
        if s.state == "启动中":
            p = s.load_progress()
            out.append(("col", (C["warn"], "  加载模型中… %d%%" % int(p * 100))))
            out.append(("bar", (max(0.02, p), "端口 %d" % s.port)))
            # 最后一行输出，让加载过程可见
            tail = s.tail(1)
            if tail:
                out.append(("ghost", "  " + cut(tail[-1], iw - 3)))
        else:
            out.append(("col", (C["ok"], "  运行中 · 端口 %d · %s" % (s.port, fmt_dur(s.uptime())))))
        out.append(("dim", "  由本程序启动 · S 停止"))
        out.append(("sep", ""))
        return out

    def backend_rows(self, b, iw):
        out = [("big", ("● " if b.online else "○ ") + b.model_name())]
        st = b.status()
        extra = []
        if b.pid:
            extra.append("pid %d" % b.pid)
        if b.start_time:
            extra.append(fmt_dur(now() - b.start_time))
        out.append(("col", (b.status_color(), "  " + st + (" · " + " · ".join(extra) if extra else ""))))
        if b.base_url:
            out.append(("kv", ("地址", b.base_url.replace("http://", ""))))
        if b.n_ctx:
            used = b.ctx_used()
            out.append(("bar", ((used / float(b.n_ctx)) if b.n_ctx else 0,
                                "%s/%s ctx" % (fmt_tok(used), fmt_tok(b.n_ctx)))))
        line = []
        if b.tps:
            line.append("%.1f t/s" % b.tps)
        if b.quant():
            line.append(b.quant())
        if b.mem:
            line.append(fmt_bytes(b.mem))
        if line:
            out.append(("dim", "  " + " · ".join(line)))
        out.append(("sep", ""))
        return out
    def model_rows(self, m, iw, sel):
        out = []
        cur = "> " if sel else "  "
        tag = "*" if m.get("live") else "-"
        name = short_name(m.get("name") or m.get("path") or "?")
        col = C["hi"] if sel else C["text"]
        out.append(("col", (col, cur + tag + " " + cut(name, iw - 4))))
        sub = []
        if m.get("size"):
            sub.append(fmt_bytes(m["size"]))
        q = re.search(r"(I?Q\d[_A-Z0-9]*|F16|BF16|F32)", name, re.I)
        if q:
            sub.append(q.group(1).upper())
        if m.get("live"):
            sub.append("运行中")
        if sub:
            out.append(("ghost", "     " + " · ".join(sub)))
        return out

    def draw_row(self, cv, x, y, w, kind, payload):
        if kind == "sep":
            cv.hline(x, y, w, "·", C["ghost"])
        elif kind == "label":
            cv.putf(x, y, payload, w, C["accent"])
        elif kind == "big":
            cv.putf(x, y, payload, w, C["hi"])
        elif kind == "col":
            col, text = payload
            cv.putf(x, y, text, w, col)
        elif kind == "dim":
            cv.putf(x, y, payload, w, C["dim"])
        elif kind == "ghost":
            cv.putf(x, y, payload, w, C["ghost"])
        elif kind == "kv":
            k, v = payload
            cv.put(x, y, k + " ", C["dim"])
            off = text_width(k) + 1
            cv.putf(x + off, y, v, w - off, C["text"])
        elif kind == "bar":
            ratio, label = payload
            bw = max(6, w - text_width(label) - 2)
            cv.bar(x, y, bw, ratio)
            cv.putf(x + bw + 1, y, label, w - bw - 1, C["dim"])

    # ── 中：任务 ──
    def draw_tasks(self, cv, x, y, w, h):
        nrun = sum(1 for t in self.tasks if t.state in ("运行", "排队"))
        self.box(cv, x, y, w, h, "任务", ("%d" % len(self.tasks)) if self.tasks else "",
                 focused=(self.focus == "tasks"))
        ix, iy, iw, ih = x + 1, y + 1, w - 2, h - 2
        if not self.tasks:
            b = self.current_backend()
            cv.putf(ix, iy, "还没有任务", iw, C["dim"])
            cv.putf(ix, iy + 1, "按 f 发一条给模型", iw, C["ghost"])
            if b and b.online:
                cv.putf(ix, iy + 3, "当前服务", iw, C["accent"])
                cv.putf(ix, iy + 4, b.model_name(), iw, C["text"])
                cv.putf(ix, iy + 5, b.base_url, iw, C["dim"])
                if b.slots is not None:
                    cv.putf(ix, iy + 6, "槽位 %d/%d" % (b.slots_busy, len(b.slots)), iw, C["dim"])
            else:
                loading = [s for s in self.servers if s.alive()]
                if loading:
                    s = loading[0]
                    cv.putf(ix, iy + 3, "正在加载模型", iw, C["warn"])
                    cv.putf(ix, iy + 4, short_name(s.model_path), iw, C["text"])
                    p = s.load_progress()
                    cv.bar(ix, iy + 5, max(6, iw - 8), max(0.02, p))
                    cv.putf(ix, iy + 5 + 1, "%.0f%%" % (p * 100), iw, C["dim"])
                else:
                    cv.putf(ix, iy + 3, "没有在线服务", iw, C["warn"])
                    cv.putf(ix, iy + 4, "左栏选模型 → 按 R 启动", iw, C["hl"])
                    cv.putf(ix, iy + 5, "或按 m 挑模型", iw, C["ghost"])
            return
        list_h = max(2, min(len(self.tasks), int(ih * 0.45)))
        detail_h = ih - list_h - 1
        self.task_sel = clamp(self.task_sel, 0, len(self.tasks) - 1)
        if self.task_sel < self.task_top:
            self.task_top = self.task_sel
        if self.task_sel >= self.task_top + list_h:
            self.task_top = self.task_sel - list_h + 1
        for i in range(list_h):
            idx = self.task_top + i
            if idx >= len(self.tasks):
                break
            t = self.tasks[idx]
            sel = (idx == self.task_sel)
            icon, col = self.task_style(t)
            info = ""
            if t.state == "运行":
                info = "%s tok" % fmt_tok(t.cur_tokens())
            elif t.state in ("完成", "失败", "取消"):
                info = fmt_dur(t.elapsed())
            iw_info = text_width(info) + 1 if info else 0
            line = "%s #%d %s" % (icon, t.id, t.title)
            self.hit["task:%d" % idx] = (ix - 1, iy + i, iw + 2, 1)
            if sel:
                cv.fill(ix - 1, iy + i, iw + 1, 1, " ", C["sel"])
            cv.putf(ix, iy + i, line, max(4, iw - iw_info), C["hi"] if sel else col)
            if info:
                cv.putf(ix + iw - text_width(info), iy + i, info, text_width(info), C["dim"])
        dy = iy + list_h
        cv.hline(ix, dy, iw, "─", C["frame"])
        t = self.sel_task()
        if not t:
            return
        rows = [
            ("状态", t.state + ((" · " + t.err[:40]) if t.err else "")),
            ("模型", short_name(t.model) or "-"),
            ("端点", (t.endpoint or "").replace("http://", "")),
            ("token", "%s%s" % (fmt_tok(t.cur_tokens()),
                                (" / %s" % fmt_tok(t.max_tokens)) if t.max_tokens else "")),
            ("速度", ("%.1f t/s" % t.rate()) if t.rate() else "-"),
            ("耗时", fmt_dur(t.elapsed())),
        ]
        yy = dy + 1
        for k, v in rows:
            if yy >= iy + ih:
                break
            cv.put(ix, yy, k, C["dim"])
            cv.putf(ix + 6, yy, v, iw - 6, C["text"])
            yy += 1
        p = t.progress()
        if p is not None and t.state == "运行" and yy < iy + ih:
            cv.bar(ix, yy, max(6, iw - 2), p)
            yy += 1
        if yy < iy + ih:
            cv.hline(ix, yy, iw, "─", C["frame"])
            yy += 1
        avail = iy + ih - yy
        if avail <= 0:
            return
        txt = t.text()
        if txt:
            lines = wrap_text(txt, iw)
            vis = lines[-avail:]
            for j, (chunk, _) in enumerate(vis):
                cv.put(ix, yy + j, chunk, C["text"])
        else:
            dots = "." * ((self.tick_n % 4) or 1)
            cv.putf(ix, yy, "等待首个 token" + dots if t.state == "运行" else t.state, iw, C["ghost"])

    def task_style(self, t):
        return {"排队": ("·", C["dim"]), "运行": ("▶", C["warn"]),
                "完成": ("+", C["ok"]), "失败": ("!", C["bad"]),
                "取消": ("~", C["dim"])}.get(t.state, ("?", C["text"]))

    # ── 右：日志 ──
    def draw_logs(self, cv, x, y, w, h):
        fname = FILTERS[self.log_filt][0]
        self.box(cv, x, y, w, h, "日志", fname)
        ix, iy, iw, ih = x + 1, y + 1, w - 2, h - 2
        if ih <= 0 or iw <= 0:
            return
        self.log_area = (ix, iy, iw, ih)
        entries = self.filtered_logs()
        rows = self.layout_log_rows(entries, iw)
        total = len(rows)
        if not self.freeze:
            self.log_off = 0
        self.log_off = clamp(self.log_off, 0, max(0, total - ih))
        end = total - self.log_off
        start = max(0, end - ih)
        for i, (t, src, chunk, col, first) in enumerate(rows[start:end]):
            yy = iy + i
            if yy >= iy + ih:
                break
            if first:
                cv.put(ix, yy, t, C["ghost"])
            if src and self._log_src_w:
                cv.putf(ix + 9, yy, src, self._log_src_w, C["accent"])
            cv.putf(ix + self._log_prefix_w, yy, chunk,
                    self._log_msg_w, col)
        # 拖选高亮
        if self.drag:
            x0, y0, x1, y1 = self.drag
            if y0 > y1:
                y0, y1 = y1, y0
            if y0 == y1 and x0 > x1:
                x0, x1 = x1, x0
            for yy in range(max(y0, iy), min(y1, iy + ih - 1) + 1):
                lo = x0 if yy == y0 else ix
                hi = x1 if yy == y1 else ix + iw - 1
                for xx in range(max(lo, ix), min(hi, ix + iw - 1) + 1):
                    if cv.buf[yy][xx] != "\x00":
                        cv.fg[yy][xx] = cv.fg[yy][xx] + "\x1b[48;5;60m"
        if self.log_off:
            tag = " ↑%d 行 " % self.log_off
            cv.putf(x + w - text_width(tag) - 1, y + h - 1, tag, text_width(tag), C["warn"])

    def filtered_logs(self):
        _, src, kind = FILTERS[self.log_filt]
        out = []
        for e in self.log.snapshot():
            if src and e.get("s") != src:
                continue
            if kind == "warn":
                if e.get("k") not in ("warn", "err"):
                    continue
            elif kind and e.get("k") != kind:
                continue
            out.append(e)
        return out

    def layout_log_rows(self, entries, iw):
        """把日志摊平成屏幕行：按列宽折行，不再硬截断。

        列布局：  HH:MM:SS [src] 正文
        正文过长时折到下一行，续行左边留白对齐，保持左边距整齐。
        """
        TIMEW = 8
        src_w = 0
        if iw >= 52 and entries:
            longest = max((text_width(e.get("s") or "-") for e in entries), default=0)
            src_w = min(9, longest + 2)           # "[src]" 的显示宽度
        # 前缀 = 时间(8) + 空格(1) + [来源](src_w) + 空格(1)
        self._log_src_w = src_w
        self._log_prefix_w = TIMEW + 1 + (src_w + 1 if src_w else 0)
        # 正文可用宽度严格等于绘制正文的那一段，避免折行算宽与实际绘制的错位
        self._log_msg_w = max(8, iw - self._log_prefix_w)
        rows = []
        for e in entries:
            col = {"err": C["bad"], "warn": C["warn"], "ok": C["ok"]}.get(e.get("k"), C["text"])
            body = (e.get("m") or "").replace("\t", "    ")
            chunks = wrap_text(body, self._log_msg_w) or [("", 0)]
            tag = None
            if src_w:
                s = e.get("s") or "-"
                tag = pad("[%s]" % s, src_w)
            for j, (chunk, _cw) in enumerate(chunks):
                rows.append((e.get("t") if j == 0 else "",
                             tag if j == 0 else " " * src_w,
                             chunk, col, j == 0))
        return rows

    def prune_log_scroll(self):
        pass

    # ── 底栏 ──
    def draw_footer(self, cv):
        y = cv.h - 2
        cv.hline(0, y, cv.w, "─", C["frame"])
        keys = [("R", "运行模型", "run"), ("S", "停止", "stop"), ("f", "发任务", "compose"),
                ("m", "选模型", "pick"), ("l", "过滤", "filter"), ("r", "重扫", "rescan"),
                ("?", "帮助", "help")]
        x = 1
        for k, label, action in keys:
            need = 2 + text_width(label) + 2
            if x + need > cv.w - 1:
                break
            cv.put(x, y, k, C["hl"])
            cv.put(x + 1, y, " " + label, C["dim"])
            self.hit["btn:%s" % action] = (x - 1, y, need, 1)
            x += need
        # 末行
        if self.msg and now() < self.msg_t:
            cv.putf(1, cv.h - 1, self.msg, cv.w - 2, C["warn"])
            return
        b = self.current_backend()
        if b and not b.online and b.last_error:
            cv.putf(1, cv.h - 1, "连不上 %s：%s" % (b.name, b.last_error[:70]), cv.w - 2, C["bad"])
            return
        left = "服务 %d · 在线 %d · 模型 %d" % (
            len(self.backends), sum(1 for b in self.backends if b.online), len(self.models))
        ls = self.llama_bin.get("llama-server")
        if ls:
            left += " · " + os.path.basename(ls)
        else:
            left += " · 未找到 llama-server（n 登记模型 / 改 config.json）"
        cv.putf(1, cv.h - 1, left, cv.w - 20, C["ghost"])
        if self.last_mouse:
            hint = "鼠标：点任务/模型选择 · 滚轮翻日志 · 拖选复制"
        elif getattr(self.bd, "_mouse_on", False):
            hint = "鼠标模式已开启（m 测试：点任意任务行）"
        else:
            hint = "鼠标不可用（终端未支持），用键盘操作"
        tw = text_width(hint)
        if cv.w > tw + 62:
            cv.put(cv.w - tw - 2, cv.h - 1, hint, C["ghost"])

    # ── 框 ──
    def box(self, cv, x, y, w, h, title, right="", focused=False):
        if w < 4 or h < 3:
            return
        frame = C["accent"] if focused else C["frame"]
        cv.hline(x + 1, y, w - 2, "─", frame)
        cv.hline(x + 1, y + h - 1, w - 2, "─", frame)
        cv.vline(x, y + 1, h - 2, "│", frame)
        cv.vline(x + w - 1, y + 1, h - 2, "│", frame)
        cv.put(x, y, "┌", frame)
        cv.put(x + w - 1, y, "┐", frame)
        cv.put(x, y + h - 1, "└", frame)
        cv.put(x + w - 1, y + h - 1, "┘", frame)
        cv.put(x + 2, y, " %s " % title, C["hi"] if focused else C["accent"])
        if right:
            r = " %s " % right
            cv.putf(x + w - 2 - text_width(r), y, r, text_width(r), C["dim"])

    # ── 详情页 ──
    def draw_detail(self, cv):
        b = self.current_backend()
        cv.hline(0, 1, cv.w, "─", C["frame"])
        cv.put(1, 1, " 后端详情 ", C["accent"])
        if not b:
            cv.put(3, 3, "没有可用后端。启动 llama-server 后按 r 重扫。", C["warn"])
            return
        y = 2
        rows = [
            ("名称", b.name),
            ("地址", b.base_url or "（无 HTTP 端口）"),
            ("状态", "%s · health=%s" % (b.status(), b.health)),
            ("模型", b.model_path or "（服务没给出 /props）"),
            ("上下文", ("%s（当前会话已用 %s）" % (fmt_tok(b.n_ctx or "?"), fmt_tok(b.ctx_used())))
             if b.n_ctx else "-"),
            ("吞吐", ("%.1f tok/s" % b.tps) if b.tps else "需要 --metrics 启动才有"),
            ("槽位", ("%d/%d" % (b.slots_busy, len(b.slots))) if b.slots is not None else "-"),
            ("pid", str(b.pid or "-")),
            ("内存", fmt_bytes(b.mem) if b.mem else "-"),
            ("启动", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b.start_time))
             if b.start_time else "-"),
            ("目录", b.cwd or "-"),
            ("命令", b.cmdline or "-"),
        ]
        for k, v in rows:
            if y >= cv.h - 4:
                break
            cv.put(2, y, pad(k, 8), C["accent"])
            chunks = wrap_text(v, cv.w - 14)
            for j, (chunk, _) in enumerate(chunks[:2]):
                cv.put(10, y, chunk, C["text"])
                y += 1
        y += 1
        if y < cv.h - 3:
            cv.put(2, y, "原始 /props", C["accent"])
            y += 1
            raw = json.dumps(b.props, ensure_ascii=False, indent=1) if b.props else "（空）"
            for chunk, _ in wrap_text(raw, cv.w - 4)[:cv.h - 4 - y]:
                cv.put(3, y, chunk, C["dim"])
                y += 1
        cv.put(2, cv.h - 2, "v / Esc 返回 · j/k 切换到下一个模型", C["ghost"])

    # ── 弹窗 ──
    def draw_modal(self, cv):
        if self.modal == "help":
            self.draw_help(cv)
        elif self.modal == "picker":
            self.draw_picker(cv, "选择模型（Enter 设为当前，d 删除）", self.models, self.msel)
        elif self.modal == "del_model":
            self.draw_picker(cv, "删除登记模型（Enter 确认）", self.models, self.msel)
        elif self.modal == "composer":
            self.draw_input(cv, "发任务",
                            "前往: %s" % (self.current_backend().model_name() if self.current_backend() else "-"),
                            "Enter 提交 · Esc 取消")
        elif self.modal == "run_form":
            self.draw_run_form(cv)
        elif self.modal == "add_model":
            self.draw_input(cv, "登记模型",
                            "例: D:\\models\\Qwen3-8B-Q4_K_M.gguf",
                            "Enter 保存到 config.json · Esc 取消")

    def _overlay(self, cv, x, y, w, h):
        cv.fill(x, y, w, h, " ", "")
        self.box(cv, x, y, w, h, "")

    def draw_run_form(self, cv):
        nfields = len(self.RUN_FIELDS)
        w = min(88, cv.w - 6)
        # 标题1 + 模型1 + 空1 + 字段n + 空1 + 命令提示1 + 命令行最多2 + 提示1 + 边框2
        h = min(nfields + 10, cv.h - 2)
        x, y = (cv.w - w) // 2, (cv.h - h) // 2
        self._overlay(cv, x, y, w, h)
        cv.put(x + 2, y, " 运行 llama-server ", C["accent"])
        m = self.form_model or {}
        cv.putf(x + 2, y + 1, "模型: " + short_name(m.get("path") or "?"), w - 4, C["hi"])

        row = y + 3
        first_field = row
        for i, (key, label, typ, lo, hi) in enumerate(self.RUN_FIELDS):
            if row >= y + h - 4:          # 给命令预览和提示留位置
                break
            sel = (i == self.form_sel)
            val = self.form_field_value(key)
            if self.form_edit and self.form_edit[0] == key:
                shown = self.form_edit[1] + "_"
            elif typ is bool:
                shown = "开" if val else "关"
            else:
                shown = str(val if val is not None else "")
            if sel:
                cv.fill(x + 1, row, w - 2, 1, " ", C["selhi"])
            cv.put(x + 3, row, pad(label, 16), C["text"] if sel else C["dim"])
            cv.putf(x + 21, row, shown, w - 25, C["hl"] if sel else C["text"])
            row += 1

        # 命令行预览：放在提示行上方，最多两行
        cmd = Server(m.get("path") or "", self.form_values,
                     self.llama_bin.get("llama-server") or "llama-server", self.log).build_cmd()
        hint_y = y + h - 1
        cmd_lines = wrap_text(cmd, w - 8)[:2]
        base = hint_y - 1 - len(cmd_lines)
        cv.put(x + 2, base - 1, "将执行:", C["accent"])
        for j, (chunk, _) in enumerate(cmd_lines):
            cv.putf(x + 3, base + j, chunk, w - 6, C["dim"])
        cv.putf(x + 2, hint_y, "Enter 改值/切换 · Esc 取消 · R 启动", w - 4, C["ghost"])

    def draw_help(self, cv):
        w = min(76, cv.w - 6)
        h = min(len(HELP_LINES) + 5, cv.h - 4)
        x, y = (cv.w - w) // 2, (cv.h - h) // 2
        self._overlay(cv, x, y, w, h)
        cv.put(x + 2, y, " 快捷键 ", C["accent"])
        for i in range(min(len(HELP_LINES), h - 3)):
            key, desc = HELP_LINES[(i + self.help_scroll) % len(HELP_LINES)]
            cv.put(x + 2, y + 1 + i, pad(key, 11), C["hl"])
            cv.putf(x + 14, y + 1 + i, desc, w - 16, C["text"])
        cv.putf(x + 2, y + h - 1, "j/k 滚动 · 任意键关闭", w - 4, C["ghost"])

    def draw_picker(self, cv, title, items, sel):
        w = min(76, cv.w - 6)
        h = min(len(items) + 5, cv.h - 6) if items else 6
        x, y = (cv.w - w) // 2, (cv.h - h) // 2
        self._overlay(cv, x, y, w, h)
        cv.put(x + 2, y, " %s " % title, C["accent"])
        if not items:
            cv.put(x + 2, y + 2, "列表是空的（按 n 手动登记，或 r 重扫）", C["dim"])
            return
        inner = h - 3
        top = clamp(sel - inner + 1, 0, max(0, len(items) - inner))
        for i in range(min(inner, len(items))):
            idx = top + i
            if idx >= len(items):
                break
            m = items[idx]
            is_sel = (idx == sel)
            if is_sel:
                cv.fill(x + 1, y + 1 + i, w - 2, 1, " ", C["selhi"])
            tag = "●" if m.get("live") else "○"
            name = short_name(m.get("name") or m.get("path") or "?")
            cv.putf(x + 2, y + 1 + i, "%s %s" % (tag, cut(name, w - 22)), w - 4,
                    C["hi"] if is_sel else C["text"])
            cv.putf(x + w - 14, y + 1 + i, fmt_bytes(m.get("size") or 0), 12, C["dim"])
        cv.putf(x + 2, y + h - 1, "Enter 确认 · j/k 移动 · Esc 取消", w - 4, C["ghost"])

    def draw_input(self, cv, title, hint, footer):
        w = min(80, cv.w - 6)
        h = 7
        x, y = (cv.w - w) // 2, (cv.h - h) // 2
        self._overlay(cv, x, y, w, h)
        cv.put(x + 2, y, " %s " % title, C["accent"])
        cv.putf(x + 2, y + 2, hint, w - 4, C["ghost"])
        avail = w - 8
        shown = self.minput[-avail:] if text_width(self.minput) > avail else self.minput
        cv.putf(x + 2, y + 3, "> " + shown + "▏", w - 4, C["text"])
        cv.putf(x + 2, y + h - 1, footer, w - 4, C["ghost"])


# ══════════════════════════════════════════════════════════ demo / 自检


def make_demo(cfg=None):
    """演示数据放在 demo_data.py，主程序只在需要时才导入。"""
    try:
        import demo_data
    except ImportError:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "demo_data", os.path.join(here, "demo_data.py"))
        demo_data = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo_data)
    return demo_data.make(cfg)


def dump_frame(cfg, demo, w, h):
    tui = make_demo(cfg) if demo else LlamaTUI(cfg, demo=True)
    if not demo:
        tui.llama_bin = find_llama_bin(cfg, None)
        tui.models = tui._merge_models(find_models(cfg, None))
        tui.rebuild_backends()
        for b in tui.backends:
            probe_backend(b)
        tui.log.add("dump-frame 模式", "tui")
    tui.w, tui.h = w, h
    cv = Canvas(w, h)
    tui.cv = cv
    tui.draw_header(cv)
    mw = max(24, min(46, int(w * 0.29)))
    tw = max(28, min(54, int(w * 0.33)))
    lw = w - mw - tw
    if lw < 26:
        tw = max(24, tw - (26 - lw))
        lw = w - mw - tw
    tui.draw_models(cv, 0, 1, mw, h - 3)
    tui.draw_tasks(cv, mw, 1, tw, h - 3)
    tui.draw_logs(cv, mw + tw, 1, lw, h - 3)
    tui.draw_footer(cv)
    return cv


def cmd_find(cfg):
    print("== llama.cpp 程序 ==")
    bins = find_llama_bin(cfg, None)
    if not bins:
        print("  没找到 llama-server")
        print("  提示：把 llama.cpp 目录加到 PATH，或在 config.json 里写 extra_llama_dirs")
    for k, v in sorted(bins.items()):
        print("  %-16s %s" % (k, v))
    print("\n== 模型 (.gguf) ==")
    models = find_models(cfg, None)
    if not models:
        print("  没找到 .gguf")
    for m in models[:40]:
        print("  %-56s %s" % (cut(m["name"], 56), fmt_bytes(m["size"])))
    if len(models) > 40:
        print("  ... 共 %d 个" % len(models))
    print("\n== 服务 ==")
    hits = scan_llama_services(cfg, None)
    if not hits:
        print("  没有发现正在运行的 llama.cpp HTTP 服务")
    for h in hits:
        ok, info = probe_http(h["url"])
        print("  %-24s pid=%-7s health=%-8s model=%s"
              % (h["url"], h["pid"], info.get("health"), short_name(info.get("model"))))
    print("\n== 进程 ==")
    procs = list_processes()
    if not procs:
        print("  没有 llama 进程")
    for p in procs:
        print("  pid=%-7s %s" % (p["pid"], cut(p.get("cmdline") or p.get("exe") or "", 100)))
    return 0


def cmd_selftest(cfg):
    print("llama.cpp TUI v%s 自检" % VERSION)
    print("  配置      :", cfg.get("_path") or "（未找到，用默认值）")
    print("  Python    :", sys.version.split()[0], "|", sys.platform)
    try:
        bd = make_backend()
        w, h = bd.size()
        print("  终端后端  :", type(bd).__name__, "| %dx%d" % (w, h))
        bd.restore() if hasattr(bd, "restore") else None
    except Exception as e:
        print("  终端后端  : 失败", e)
    print("  宽字符    :", "中文=2列 OK" if text_width("中文abc") == 7 else "异常")
    print("  折行      :", "OK" if len(wrap_text("测试" * 40, 20)) >= 4 else "异常")
    # 鼠标链路
    try:
        bd2 = make_backend()
        is_win = getattr(bd2, "_win", False)
        has_in = getattr(bd2, "_in_ok", False)
        print("  鼠标      : 输入句柄=%s | Win32 记录=%s | VT 请求=可用%s" % (
            "可用" if has_in else "不可用",
            "会送达" if is_win else "不会（靠 VT 序列）",
            "" if (is_win or not IS_WIN) else "（已开启 1000/1002/1006）"))
        e, _ = parse_vt_stream("\x1b[<0;10;5M")
        print("            VT 解析: %s" % ("OK" if e and e[0][0] == "mouse" else "异常"))
        if hasattr(bd2, "restore"):
            bd2.restore()
    except Exception as e:
        print("  鼠标      : 检测失败", e)
    try:
        bins = find_llama_bin(cfg, None)
        print("  llama.cpp :", bins.get("llama-server") or "未找到")
    except Exception as e:
        print("  llama.cpp : 查找失败", e)
    try:
        models = find_models(cfg, None)
        print("  模型      :", "%d 个 .gguf" % len(models))
    except Exception as e:
        print("  模型      : 查找失败", e)
    try:
        procs = list_processes()
        print("  进程      :", "%d 个 llama 进程" % len(procs))
    except Exception as e:
        print("  进程      : 失败", e)
    try:
        hits = scan_llama_services(cfg, None)
        print("  服务      :", "%d 个" % len(hits), [h["port"] for h in hits])
        for h in hits:
            ok, info = probe_http(h["url"])
            print("     %-22s health=%-8s model=%s" % (h["url"], info.get("health"),
                                                       short_name(info.get("model"))))
    except Exception as e:
        print("  服务      : 失败", e)
    try:
        tui = make_demo(cfg)
        cv = Canvas(130, 38)
        tui.cv = cv
        tui.w, tui.h = 130, 38
        tui.draw_header(cv)
        tui.draw_models(cv, 0, 1, 38, 35)
        tui.draw_tasks(cv, 38, 1, 42, 35)
        tui.draw_logs(cv, 80, 1, 50, 35)
        tui.draw_footer(cv)
        plain = cv.plain()
        assert len(plain) == 38, "行数不一致"
        assert all(text_width(row) == 130 for row in plain), "某行显示宽度不是 130"
        assert "模型" in plain[1] and "任务" in plain[1] and "日志" in plain[1], "三栏标题缺失"
        assert any("发任务" in r for r in plain), "底栏快捷键缺失"
        print("  渲染      : OK（130x38，三栏齐全）")
    except Exception:
        print("  渲染      : 失败")
        traceback.print_exc()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="llama.cpp 命令行 TUI（零依赖）")
    ap.add_argument("-c", "--config", help="配置文件路径")
    ap.add_argument("--demo", action="store_true", help="假数据预览界面")
    ap.add_argument("--find", action="store_true", help="只打印发现了什么")
    ap.add_argument("--selftest", action="store_true", help="自检")
    ap.add_argument("--dump-frame", action="store_true", help="打印一帧后退出")
    ap.add_argument("--size", default="130x38")
    args = ap.parse_args(argv)

    # 全局抑制「缺 DLL」之类的系统错误框，免得探测时弹窗打断
    _suppress_error_dialogs()

    cfg = load_config(args.config)
    if args.find:
        return cmd_find(cfg)
    if args.selftest:
        return cmd_selftest(cfg)
    if args.dump_frame:
        w, h = 130, 38
        m = re.match(r"(\d+)[xX](\d+)", args.size)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        cv = dump_frame(cfg, args.demo, w, h)
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        print("\n".join(cv.render()))
        return 0
    tui = make_demo(cfg) if args.demo else LlamaTUI(cfg)
    tui.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
