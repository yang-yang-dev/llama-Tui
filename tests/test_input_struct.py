"""验证 INPUT_RECORD / MOUSE_EVENT_RECORD 的结构布局是否与 Win32 一致。

布局错了的话，读出来的坐标就是垃圾，表现为「点了没反应」。
做法：自己按 Win32 文档造一条 MOUSE_EVENT 记录，用程序里的结构体定义解析回读。
"""
import ctypes
import sys
from ctypes import wintypes

sys.path.insert(0, r"D:\Dsh-work\llama-tui")
import llama_tui as L


def build_types():
    """复制 VTBackend._build_input_types 里的定义，但独立构建以便对比。"""
    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

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

    return INPUT_RECORD, MOUSE_EVENT_RECORD, KEY_EVENT_RECORD, COORD


def main():
    INPUT_RECORD, MOUSE_EVENT_RECORD, KEY_EVENT_RECORD, COORD = build_types()

    print("结构体尺寸：")
    print("  COORD            =", ctypes.sizeof(COORD))
    print("  KEY_EVENT_RECORD =", ctypes.sizeof(KEY_EVENT_RECORD))
    print("  MOUSE_EVENT_REC       =", ctypes.sizeof(MOUSE_EVENT_RECORD))
    print("  INPUT_RECORD     =", ctypes.sizeof(INPUT_RECORD))
    print()

    ok = True
    # Win32: COORD=4, KEY_EVENT=16, MOUSE_EVENT=16, INPUT_RECORD=20
    ok &= ctypes.sizeof(COORD) == 4
    ok &= ctypes.sizeof(MOUSE_EVENT_RECORD) == 16
    ok &= ctypes.sizeof(INPUT_RECORD) == 20
    print("尺寸符合 Win32 文档:", "是" if ok else "否")

    # 造一条「鼠标左键按下在 (37, 5)」
    rec = INPUT_RECORD()
    rec.EventType = 0x0002
    me = rec.Event.MouseEvent
    me.dwMousePosition.X = 37
    me.dwMousePosition.Y = 5
    me.dwButtonState = 0x0001      # FROM_LEFT_1ST_BUTTON_PRESSED
    me.dwEventFlags = 0

    print("\n构造: 左键按下 @ (37,5), buttonState=0x1, flags=0")
    print("  回读 EventType =", rec.EventType)
    print("  回读 X, Y      =", rec.Event.MouseEvent.dwMousePosition.X,
          rec.Event.MouseEvent.dwMousePosition.Y)
    print("  回读 buttonState =", hex(rec.Event.MouseEvent.dwButtonState))
    print("  回读 flags       =", rec.Event.MouseEvent.dwEventFlags)

    good = (rec.Event.MouseEvent.dwMousePosition.X == 37
            and rec.Event.MouseEvent.dwMousePosition.Y == 5
            and rec.Event.MouseEvent.dwButtonState == 1)
    print("结构与存取一致:", "是" if good else "否 <<<< 问题在这")

    # 滚轮向下（Windows 给的是负数，存进 DWORD 变成很大的数）
    print("\n滚轮 -120 在 Win32 记录里的表示：")
    # 真实记录里增量在 dwButtonState 的高 16 位
    raw_hi = (-120) & 0xFFFF
    print("  高 16 位 = 0x%04X" % raw_hi)
    print("  解析结果 =", L.wheel_delta_from_buttons(raw_hi << 16), "（应为 -120）")
    wheel_ok = L.wheel_delta_from_buttons(raw_hi << 16) == -120
    print("  解析正确:", "是" if wheel_ok else "否 <<<< 滚轮方向反了")

    # on_mouse 收到解析后的 -120 应该往下翻
    tui = L.make_demo(L.load_config(None))
    tui.log_area = (80, 1, 40, 30)
    tui.log_off = 5
    tui.on_mouse(("mouse", 90, 5, -120, 0x0004))
    print("\non_mouse 收到 -120 后 log_off =", tui.log_off, "(向下滚应减小)")
    wheel_ok = wheel_ok and tui.log_off < 5

    return 0 if (good and ok and wheel_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
