# -*- coding: utf-8 -*-
"""v0.4.6 安装链路回归：无控制台父进程里拉起 apply_update.cmd。

父进程用 pythonw.exe 跑子模式（无控制台，和打包后的 exe 一样），子模式：
  1) 用修复后的方式(CREATE_NO_WINDOW + DEVNULL)拉起新脚本 → 必须真的替换/重启/自删；
  2) 用 v0.4.5 的旧脚本(/dev/null) + 旧方式(DETACHED_PROCESS，不指定标准句柄)
     → 必须复现失败（负对照）。
不弹窗、不启动真实 exe：被替换后的“程序”是一个只会写标记文件的 .vbs。
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NEW_VBS = (
    'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
    'Set f = fso.CreateTextFile("restarted.flag", True)\r\n'
    'f.Write "ok"\r\n'
    'f.Close\r\n'
)
OLD_VBS = "' old\r\n"
# v0.4.5 及以前的脚本：Windows 上 /dev/null 是无效路径；配合 DETACHED_PROCESS
# 会让 cmd 在第一行就退出（rc=1），替换/重启全没发生。
LEGACY_BODY = r"""@echo off
setlocal
set "OLD=__OLD__"
set "NEW=__NEW__"
set "OLD_DIR=__DIR__"
if exist "%OLD%" copy /y "%OLD%" "%OLD%.bak" >/dev/null 2>nul
for /l %%i in (1,1,180) do (
  move /y "%NEW%" "%OLD%" >nul 2>nul && goto replaced
  >nul 2>nul ping -n 2 127.0.0.1
)
goto failed
:replaced
cd /d "%OLD_DIR%"
>nul echo replaced
del "%~f0" >nul 2>nul
exit /b 0
:failed
del "%~f0" >nul 2>nul
exit /b 1
"""


def write(path, text, enc="utf-8"):
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(text)


def child(tmp):
    os.environ["MIRRORSORTER_DATA_DIR"] = os.path.join(tmp, "data")
    sys.path.insert(0, HERE)
    import app  # noqa: E402

    out = {"ok": False}

    def run_case(case, use_old_flags, legacy=False):
        os.makedirs(case, exist_ok=True)
        old = os.path.join(case, "洞鉴虚明 v0.4.6dsk.exe.vbs")
        new = os.path.join(case, "pkg.exe.vbs")
        script = os.path.join(case, "apply_update.cmd")
        write(old, OLD_VBS)
        write(new, NEW_VBS)
        if legacy:
            body = (LEGACY_BODY.replace("__OLD__", old).replace("__NEW__", new)
                    .replace("__DIR__", case))
        else:
            body = app.update_script_text(old, new)
        write(script, body, "gbk")   # 与 update_prepare_install 一致
        cmd = ["cmd.exe", "/c", script]
        if use_old_flags:
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | \
                    int(getattr(subprocess, "DETACHED_PROCESS", 0))
            p = subprocess.Popen(cmd, cwd=case, creationflags=flags, close_fds=True)
        else:
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            p = subprocess.Popen(cmd, cwd=case, creationflags=flags, close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        got = {"rc": None, "replaced": False, "script_left": True,
               "flag": False, "log": "", "has_devnull": "/dev/null" in body,
               "has_log": "apply_update.log" in body}
        deadline = time.time() + 20
        while time.time() < deadline:
            rc = p.poll()
            got["replaced"] = os.path.isfile(old) and open(old, "rb").read() == NEW_VBS.encode()
            got["script_left"] = os.path.exists(script)
            got["flag"] = os.path.isfile(os.path.join(case, "restarted.flag"))
            if rc is not None and got["replaced"] and not got["script_left"] and got["flag"]:
                got["rc"] = rc
                break
            if rc is not None and use_old_flags:
                got["rc"] = rc
                break
            time.sleep(0.1)
        if got["rc"] is None:
            got["rc"] = p.poll()
        if got["rc"] is None:
            p.kill()
            got["rc"] = "timeout"
        log = os.path.join(case, "apply_update.log")
        if os.path.isfile(log):
            got["log"] = open(log, "rb").read().decode("gbk", "replace")
        time.sleep(0.3)
        got["flag"] = os.path.isfile(os.path.join(case, "restarted.flag"))
        got["script_left"] = os.path.exists(script)
        return got

    out["new"] = run_case(os.path.join(tmp, "更 新 测试"), False)
    out["old"] = run_case(os.path.join(tmp, "old_case"), True, legacy=True)
    out["stdio_none"] = [sys.stdin is None, sys.stdout is None, sys.stderr is None]
    out["ok"] = True
    return out


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        tmp = sys.argv[2]
        try:
            data = child(tmp)
        except Exception as e:
            import traceback
            data = {"ok": False, "error": repr(e), "tb": traceback.format_exc()}
        write(os.path.join(tmp, "result.json"), json.dumps(data, ensure_ascii=False))
        return 0

    pyw = sys.executable
    if os.path.basename(pyw).lower() == "python.exe":
        pyw = os.path.join(os.path.dirname(pyw), "pythonw.exe")
    if not os.path.isfile(pyw):
        print("FAIL 找不到 pythonw.exe:", pyw)
        return 1
    tmp = tempfile.mkdtemp(prefix="ms_apply46_")
    # 关键：用 NULL 标准句柄启动 pythonw，复刻“资源管理器双击 exe”的环境。
    # 若让 pythonw 继承本进程的管道句柄，旧方式也能跑，负对照就失去判别力。
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESTDHANDLES
    si.hStdInput = si.hStdOutput = si.hStdError = None
    p = subprocess.run([pyw, os.path.abspath(__file__), "--child", tmp],
                       timeout=120, startupinfo=si, close_fds=False,
                       creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    rf = os.path.join(tmp, "result.json")
    if not os.path.isfile(rf):
        print("FAIL 子进程没有写结果 (rc=%s)" % p.returncode)
        return 1
    d = json.load(open(rf, encoding="utf-8"))
    fail = []

    def chk(c, msg):
        print(("PASS  " if c else "FAIL  ") + msg)
        if not c:
            fail.append(msg)

    chk(d.get("ok"), "无控制台子进程跑完: %s" % d.get("error", ""))
    n = d.get("new") or {}
    o = d.get("old") or {}
    chk(not n.get("has_devnull"), "生成的脚本里没有 /dev/null")
    chk(n.get("has_log"), "生成的脚本会写 apply_update.log")
    chk(d.get("stdio_none"), "子进程确实无标准句柄（与打包 exe 一致）")
    chk(n.get("replaced"), "新方式：旧 exe 被新包覆盖（move 成功）")
    chk(not n.get("script_left"), "新方式：apply_update.cmd 已自删")
    chk(n.get("flag"), "新方式：替换后成功启动了程序（标记文件已生成）")
    lg = n.get("log") or ""
    chk("start" in lg and "replaced" in lg and "restarted" in lg,
        "新方式：apply_update.log 记录了 start/replaced/restarted")
    chk(o.get("rc") == 1, "旧脚本+旧方式(负对照)：cmd 退出码 1（实际 %r）" % o.get("rc"))
    chk(not o.get("replaced"), "旧脚本+旧方式(负对照)：替换没有发生")
    chk(o.get("script_left"), "旧脚本+旧方式(负对照)：脚本留在原地，证明批处理根本没跑")

    print()
    print("RESULT:", "OK" if not fail else "FAIL %d 项: %s" % (len(fail), fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
