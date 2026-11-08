import sys
sys.path.insert(0, r"D:\Dsh-work\llama-tui")
import llama_tui as L

cases = [
    ("\r", 0x0D, ["enter"], "Enter"),
    ("\n", 0x0D, ["enter"], "换行"),
    ("\x1b", 0x1B, ["esc"], "Esc"),
    ("\x08", 0x08, ["backspace"], "退格"),
    (" ", 0x20, ["space"], "空格"),
    ("j", 0x4A, ["j"], "字母 j"),
    ("q", 0x51, ["q"], "字母 q"),
    ("\x00", 0x26, ["up"], "方向键上"),
    ("\x00", 0x28, ["down"], "方向键下"),
    ("\x00", 0x21, ["pageup"], "PgUp"),
    ("\x00", 0x22, ["pagedown"], "PgDn"),
    ("\x00", 0x23, ["end"], "End"),
    ("\x03", 0x43, ["q"], "Ctrl+C"),
    ("中", 0x00, ["中"], "中文（粘贴）"),
]

ok = True
for ch, vk, want, name in cases:
    got = L.decode_key(ch, vk)
    good = got == want
    ok = ok and good
    print(("  [PASS] " if good else "  [FAIL] ") + "%-12s -> %r" % (name, got) +
          ("" if good else "  期望 %r" % (want,)))

# 关键回归：弹窗处理 Enter 必须能命中
print()
print("Enter 现在会被 modal 当成:", L.decode_key("\r", 0x0D))
print("全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
