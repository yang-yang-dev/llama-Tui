#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台安全检查：模拟非 Windows 环境，看关键路径会不会崩。

GitHub 上会有 Linux / macOS 用户，这里把 IS_WIN 强制设为 False，
跑一遍启动、发现、渲染路径，抓出没被平台判断保护的调用。
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import llama_tui as L  # noqa: E402


def main():
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            fn()
            print("  [PASS] " + name)
        except Exception as e:
            ok = False
            print("  [FAIL] %s -> %s: %s" % (name, type(e).__name__, e))
            traceback.print_exc(limit=3)

    print("== 模拟 Linux 环境（IS_WIN=False）==")
    L.IS_WIN = False

    check("list_processes", lambda: L.list_processes())
    check("listening_ports", lambda: L.listening_ports())
    check("candidate_llama_dirs", lambda: L.candidate_llama_dirs({}))
    check("candidate_model_dirs", lambda: L.candidate_model_dirs({}))
    check("find_llama_bin", lambda: L.find_llama_bin({}, None))
    check("find_models", lambda: L.find_models({}, None, limit=5))
    check("scan_llama_services", lambda: L.scan_llama_services({}, None))
    check("port_open", lambda: L.port_open(1, timeout=0.1))

    # 服务器对象（不真的启动，只构造命令）
    def mk_server():
        s = L.Server("/tmp/model.gguf", {"port": 8080, "ctx": 4096},
                     "/usr/local/bin/llama-server", L.LogBuf(10))
        cmd = s.build_cmd()
        assert "--port 8080" in cmd, cmd
    check("Server.build_cmd", mk_server)

    # 界面渲染
    def render():
        import demo_data
        tui = demo_data.make(L.load_config(None))
        cv = L.Canvas(120, 34)
        tui.cv = cv
        tui.w, tui.h = 120, 34
        tui.draw_header(cv)
        tui.draw_models(cv, 0, 1, 36, 31)
        tui.draw_tasks(cv, 36, 1, 40, 31)
        tui.draw_logs(cv, 76, 1, 44, 31)
        tui.draw_footer(cv)
        assert len(cv.plain()) == 34
    check("界面渲染", render)

    # VT 鼠标解析（跨平台公用）
    check("VT 鼠标解析", lambda: L.parse_vt_stream("\x1b[<0;10;5M"))

    # 终端后端：非 Windows 会走 termios 分支（这里没 tty，应安静失败）
    def mk_backend():
        b = L.make_backend()
        b.size()
    check("make_backend", mk_backend)

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
