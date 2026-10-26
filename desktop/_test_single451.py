# -*- coding: utf-8 -*-
"""v0.4.51 单实例验收(_test_single451.py)。

两层:
  ① 单元层 —— 同进程内 acquire/wake/dispose 的语义(含 scope 隔离、路径归一);
  ② 进程层 —— 真起两个 app_window.py: 第二个必须**自己退出**(退出码 0、几秒内),
     并且把第一个(托盘隐藏态)的窗口叫到前台(IsWindowVisible 由 False → True)。

进程层用隔离的数据目录与端口, 不碰用户正式存档, 也不影响正在跑的正式实例。
结果落 build/probe/single_e2e.log。
"""
import ctypes
import io
import os
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
EXE = None
if "--exe" in sys.argv:
    import glob
    _c = sorted(glob.glob(os.path.join(ROOT, "build", "dist", "*.exe")),
                key=os.path.getmtime)
    assert _c, "build/dist 下没有 exe"
    EXE = _c[-1]
    print("exe:", EXE, flush=True)


def app_cmd():
    return [EXE, "--tray"] if EXE else [sys.executable, "app_window.py", "--tray"]
PROBE = os.path.join(ROOT, "build", "probe")
os.makedirs(PROBE, exist_ok=True)
LOG = os.path.join(PROBE, "single_e2e.log")
_out = []


def ok(name, cond, extra=""):
    _out.append(("PASS" if cond else "FAIL") + "  " + name + (("  [%s]" % extra) if extra else ""))
    print(_out[-1], flush=True)
    return bool(cond)


import shell_single  # noqa: E402

# ---------------------------------------------------------------- ① 单元层
print("== 单元层 ==", flush=True)
s = shell_single.scope_of(r"D:\Temp\MSX\..\msx-a")
ok("scope_of 归一(.. 展开)", s == os.path.normcase(os.path.abspath(r"D:\Temp\MSX\..\msx-a")), s)
ok("scope_of 大小写无关",
   shell_single.scope_of(r"d:\temp\msx-a") == shell_single.scope_of(r"D:\TEMP\MSX-A"))

sc = "\u6d4b\u8bd5-single-unit-%d" % os.getpid()   # 独立 scope, 不碰真实例
a = shell_single.acquire(sc)
ok("首次 acquire 拿到实例", a is not None)
hits = []
a.start(lambda: hits.append(time.time()))
b = shell_single.acquire(sc)
ok("再次 acquire 应被挡回(None)", b is None)
ok("wake 能叫到监听方", shell_single.wake(sc, timeout=3.0))
time.sleep(0.3)
ok("回调被调用一次", len(hits) == 1, "hits=%d" % len(hits))
ok("wake 重复仍有效", shell_single.wake(sc, timeout=3.0))
a.dispose()
time.sleep(0.2)
c = shell_single.acquire(sc)
ok("dispose 之后可以重新占用", c is not None)
c.dispose()
ok("空 scope 的 wake 应失败(3 秒重试后)", not shell_single.wake(sc + "-nobody", timeout=0.5))

# ---------------------------------------------------------------- ② 进程层
print("== 进程层(真窗口) ==", flush=True)
DATA = r"D:\temp\msx-single-a"
WV2 = r"D:\temp\msx-single-a-wv2"
PORT = "18091"
for d in (DATA, WV2):
    if os.path.isdir(d):
        subprocess.call(["cmd", "/c", "rmdir", "/s", "/q", d])
env = dict(os.environ)
env["MIRRORSORTER_DATA_DIR"] = DATA
env["MIRRORSORTER_WV2_DATA"] = WV2
env["MIRRORSORTER_PORT"] = PORT

user32 = ctypes.windll.user32


class _PE32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def proc_tree(root):
    """pid 及其所有后代。

    ★ onefile exe 在 Windows 上是**父 bootloader + 子进程**两个进程：窗口属于子进程,
    拿 Popen 的 pid 去 EnumWindows 会一个窗口都找不到(踩过)。
    """
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32W)]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32W)]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    pairs = []
    pe = _PE32W()
    pe.dwSize = ctypes.sizeof(_PE32W)
    try:
        if k.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                pairs.append((int(pe.th32ProcessID), int(pe.th32ParentProcessID)))
                if not k.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k.CloseHandle(snap)
    kids = {}
    for pid, ppid in pairs:
        kids.setdefault(ppid, []).append(pid)
    out, stack = [], [int(root)]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(kids.get(cur, []))
    return set(out)


def our_windows(pids):
    """这些进程的顶层窗口 [(hwnd, 是否可见, 标题)]。"""
    pids = pids if isinstance(pids, (set, frozenset)) else {int(pids)}
    out = []
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _l):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value in pids:
            t = ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(h, t, 300)
            if "\u6d1e\u9274\u865a\u660e" in t.value:
                out.append((int(h), bool(user32.IsWindowVisible(h)), t.value))
        return True
    user32.EnumWindows(PROC(cb), 0)
    return out


def wait_for(fn, timeout, step=0.4):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(step)
    return None


log_a = io.open(os.path.join(PROBE, "single_a.log"), "wb")
log_b = io.open(os.path.join(PROBE, "single_b.log"), "wb")
a_proc = subprocess.Popen(app_cmd(), cwd=ROOT, env=env,
                          stdout=log_a, stderr=subprocess.STDOUT)
try:
    wins = wait_for(lambda: our_windows(proc_tree(a_proc.pid)) or None, 120)
    ok("A 起来了(窗口已创建)", bool(wins), str(wins))
    hidden_ok = bool(wins) and all(not w[1] for w in wins)
    ok("A(--tray) 启动即隐藏", hidden_ok, str(wins))
    # 再等几秒让托盘/唤醒目标装好(serve 完页面 + 各控件)
    time.sleep(10)

    t0 = time.time()
    b_proc = subprocess.run(app_cmd(), cwd=ROOT, env=env,
                            stdout=log_b, stderr=subprocess.STDOUT, timeout=90)
    dt = time.time() - t0
    ok("B 自己退出了", b_proc.returncode == 0, "rc=%d" % b_proc.returncode)
    ok("B 退出得够快(不是又开了个窗口)", dt < 20, "%.1fs" % dt)

    shown = wait_for(
        lambda: [w for w in our_windows(proc_tree(a_proc.pid)) if w[1]], 20)
    ok("A 的窗口被叫到前台(可见)", bool(shown), str(shown))
    ok("A 进程还活着(没有变成第二个实例)", a_proc.poll() is None)

    # 两个进程共用同一个数据目录 → 都往这一个 shell_err.log 里写(sh_dir 里的行是 B 写的)
    try:
        errlog = io.open(os.path.join(DATA, "shell_err.log"), encoding="utf-8",
                         errors="replace").read()
    except Exception:
        errlog = ""
    if EXE:
        _out.append("SKIP  打包态无 stdout(B 的判重靠退出码 + A 的唤醒日志验证)")
    else:
        # B 走的是"判重 → wake 成功"那条路(而不是被兜底成"当唯一实例")
        ok("B 认出了已有实例(没走成\"我是唯一实例\")", "single:wake-ok" in errlog,
           errlog[-200:])
        ok("B 没抛异常(ctypes 签名/导出名错了会静默退成\"当唯一实例\")",
           "Traceback" not in errlog)
    ok("A 日志记录了唤醒", "single:raise" in errlog or "single:late-raise" in errlog)
    ok("A 日志无跨线程异常", "不能成为在另一个线程" not in errlog)
finally:
    try:
        subprocess.call(["taskkill", "/PID", str(a_proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    log_a.close()
    log_b.close()

fails = [l for l in _out if l.startswith("FAIL")]
passes = [l for l in _out if l.startswith("PASS")]
_out.append("RESULT  %d/%d PASS" % (len(passes), len(passes) + len(fails)))
io.open(LOG, "w", encoding="utf-8").write("\n".join(_out) + "\n")
print(_out[-1], flush=True)
sys.exit(1 if fails else 0)
