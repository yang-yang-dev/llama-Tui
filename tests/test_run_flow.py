#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「在 TUI 里直接加载模型」：选模型 -> 改参数 -> 按 R -> 真启动 -> 就绪。

用一个假 llama-server（真进程、真端口）跑通全流程，不弹出任何窗口。
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import llama_tui as L  # noqa: E402

PY = sys.executable
FAKE = os.path.join(HERE, "fake_llama_server.py")


class MemBackend(L.BaseBackend):
    alt_screen = False

    def __init__(self, keys=(), w=120, h=34):
        self.keys = list(keys)
        self.w, self.h = w, h
        self.frames = 0

    def set_title(self, t):
        pass

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
        assert len(lines) == h
        self.frames += 1

    def restore(self):
        pass


def main():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))
        ok = ok and bool(cond)

    # 准备：假模型文件 + 假 server（用 .bat 包一层，模拟 llama-server.exe 的形式）
    tmp_base = os.path.join(HERE, "_tuitest")
    os.makedirs(tmp_base, exist_ok=True)
    tmp = os.path.join(tmp_base, "case1")
    os.makedirs(tmp, exist_ok=True)
    for old in os.listdir(tmp):
        try:
            os.remove(os.path.join(tmp, old))
        except OSError:
            pass
    model = os.path.join(tmp, "Tiny-Test-Q4_K_M.gguf")
    with open(model, "wb") as f:
        f.write(b"GGUF" + b"\0" * 1024)
    # 假的 llama-server：真实 exe 不存在，这里用 python 解释器跑 fake_llama_server.py。
    # 做法是给 Server.build_argv 打补丁：把 argv[0] 换成交互器、把脚本顶到前面。
    # 其余流程（启动、收日志、探测、停止）走的是生产代码原样。
    server_exe = PY

    _orig_build_argv = L.Server.build_argv

    def patched_build_argv(self):
        argv = _orig_build_argv(self)
        return [PY, FAKE] + argv[1:]

    L.Server.build_argv = patched_build_argv
    print("临时目录:", tmp)

    cfg = L.load_config(None)
    # 关键：把落盘路径指到临时目录，绝不碰仓库里的 config.json
    cfg["_path"] = os.path.join(tmp, "config.json")
    cfg["models"] = [{"name": "Tiny-Test-Q4_K_M.gguf", "path": model}]
    tui = L.LlamaTUI(cfg, backend=MemBackend(), demo=True)
    tui.llama_bin = {"llama-server": server_exe}
    tui.models = tui._merge_models([{"path": model, "name": "Tiny-Test-Q4_K_M.gguf",
                                     "size": 1024, "dir": tmp}])
    tui.models[0]["path"] = model

    print("== 1. 选模型后按 R 打开运行表单 ==")
    tui.model_sel = 0
    tui.detail = False
    tui.modal = None
    tui.handle_keys(["R"])
    check("表单已打开", tui.modal == "run_form", tui.modal)
    check("已绑定模型", tui.form_model is not None and
          tui.form_model.get("path") == model, tui.form_model)

    print("== 2. 改参数（端口 / 上下文）==")
    # 找到 port 和 ctx 字段位置
    keys = [k for k, _l, _t, _lo, _hi in L.LlamaTUI.RUN_FIELDS]
    tui.form_sel = keys.index("port")
    tui.handle_keys(["enter"] + ["backspace"] * 8 + list("18091") + ["enter"])
    check("端口已改", int(tui.form_values["port"]) == 18091, tui.form_values["port"])
    tui.form_sel = keys.index("ctx")
    tui.handle_keys(["enter"] + ["backspace"] * 8 + list("2048") + ["enter"])
    check("上下文已改", int(tui.form_values["ctx"]) == 2048, tui.form_values["ctx"])
    # 切换 flash_attn
    tui.form_sel = keys.index("flash_attn")
    before = bool(tui.form_values["flash_attn"])
    tui.handle_keys(["enter"])
    check("布尔项可切换", bool(tui.form_values["flash_attn"]) != before,
          "%s -> %s" % (before, tui.form_values["flash_attn"]))
    tui.handle_keys(["enter"])  # 切回来

    print("== 3. 命令行预览 ==")
    srv_prev = L.Server(model, tui.form_values, server_exe, tui.log)
    cmd = srv_prev.build_cmd()
    print("    ", cmd)
    check("命令含模型", model in cmd)
    check("命令含端口", "--port 18091" in cmd)
    check("命令含 ctx", "-c 2048" in cmd)

    print("== 4. 按 R 真正启动 ==")
    tui.handle_keys(["R"])
    check("表单已关闭", tui.modal is None, tui.modal)
    check("创建了 Server", len(tui.servers) == 1, len(tui.servers))
    srv = tui.servers[0]
    check("进程已拉起", srv.alive(), srv.state)

    print("== 5. 等待就绪并自动并回后端列表 ==")
    for _ in range(60):
        tui.tick()
        if any(b.online and b.base_url == srv.base_url for b in tui.backends):
            break
        time.sleep(0.5)
    ready = [b for b in tui.backends if b.base_url == srv.base_url and b.online]
    check("服务已上线", bool(ready), [(b.name, b.online) for b in tui.backends])
    if ready:
        b = ready[0]
        check("读回模型名", "Tiny-Test" in b.model_name(), b.model_name())
        check("读回 ctx=2048", b.n_ctx == 2048, b.n_ctx)
        check("能发任务", True)
        log = L.LogBuf(100)
        t = L.Task("hi", b.model_name(), b.base_url)
        L.run_task(t, b, log)
        check("任务是完成的", t.state == "完成", t.state + " " + t.err)

    print("== 6. 渲染含自启实例 ==")
    tui.w, tui.h = 130, 36
    cv = L.Canvas(130, 36)
    tui.cv = cv
    tui.draw_header(cv)
    tui.draw_models(cv, 0, 1, 38, 33)
    tui.draw_tasks(cv, 38, 1, 42, 33)
    tui.draw_logs(cv, 80, 1, 50, 33)
    tui.draw_footer(cv)
    text = "\n".join(cv.plain())
    check("渲染无异常", len(cv.plain()) == 36)
    check("日志里有启动记录", "启动 llama-server" in "".join(
        e["m"] for e in tui.log.snapshot()), "见日志")

    print("== 7. 按 S 停止 ==")
    tui.handle_keys(["S"])
    time.sleep(1.5)
    check("进程已停", not srv.alive(), srv.state)
    check("端口已释放", not L.port_open(18091, timeout=0.5))

    print("\n" + ("全部通过" if ok else "有失败项"))
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
