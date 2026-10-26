# -*- coding: utf-8 -*-
"""单实例(shell_single): 后台已经有一个洞鉴虚明在跑时, 再点一次图标只把那个窗口叫出来。

为什么必须做: 有了「关闭后隐藏到托盘」和「后台自启动」之后, "程序其实还活着"对用户是
不可见的。这时再双击桌面图标, 旧行为会**再造一个壳进程** —— 两套窗口、两套 WebView2
用户数据目录(第二套还会报目录占用), 关掉其中一个还可能把共用的服务一起收掉。用户看到
的就是"点了图标怎么又多出一个"。

做法(Windows 原生对象, 不依赖服务端口, 也不依赖页面):
  · 命名互斥量  Local\\MirrorSorter-<scope>-run   —— 判断"是不是已经有一个窗口壳"。
  · 命名事件    Local\\MirrorSorter-<scope>-show  —— 新进程 SetEvent, 旧进程的等待线程
                                                     收到后把自己叫到前台。
  · 命名事件    Local\\MirrorSorter-<scope>-ack   —— 旧进程收到就回执, 新进程据此决定
                                                     "安静退出"还是"提示一句"。

scope 由**数据目录**派生: 同一个存档 = 同一个实例(正式使用就该这样); 自动化测试用
MIRRORSORTER_DATA_DIR 指到隔离目录, 于是各自独立, 不会被正在跑的正式实例挡回去。

四条实现约束:
  · ERROR_ALREADY_EXISTS 只能靠 ctypes.get_last_error() 读 —— WinDLL 必须开
    use_last_error=True, 否则拿到的是被 ctypes 自己重置过的值(永远是 0)。
  · 前台权限不跨进程: 用户点图标得到的前台权留在了新进程身上。旧进程要
    SetForegroundWindow 就得让新进程先 AllowSetForegroundWindow(ASFW_ANY), 否则窗口
    只在任务栏闪一下(用户以为没反应)。
  · 拿不到互斥量 / 事件句柄一律**当作唯一实例**继续启动 —— 宁可多开一个窗口, 也绝不能
    出现"点了图标什么都不发生"。
  · 唤醒回调必须"立刻返回": 旧实例可能还在启动中(冷启动要几十秒)。回调只置个待办标记,
    真正的显示交给窗口就绪后的流程, 回执照样马上发给新进程, 别让它干等。
"""
import ctypes
import hashlib
import os
import sys
import threading
import traceback
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
ASFW_ANY = 0xFFFFFFFF

_k32 = None
_u32 = None


def _k():
    """kernel32 句柄(带正确的 64 位签名: 不设 restype 会把 HANDLE 截成 32 位)。"""
    global _k32
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                   wintypes.LPCWSTR]
        k.OpenEventW.restype = wintypes.HANDLE
        k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.SetEvent.restype = wintypes.BOOL
        k.SetEvent.argtypes = [wintypes.HANDLE]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        _k32 = k
    return _k32


def _allow_foreground():
    """把本进程的前台权借给"随便谁"(= 已在跑的那个实例)。

    AllowSetForegroundWindow 在 **user32** 里, 不在 kernel32 —— 写错过一次(整条 wake
    直接抛 AttributeError 退成"叫不醒", 用户看到的就是点了图标没反应)。缺这个导出也
    不能致命: 退化成"窗口只在任务栏闪一下", 总比叫不出来强。
    """
    global _u32
    try:
        if _u32 is None:
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.AllowSetForegroundWindow.restype = wintypes.BOOL
            u.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
            _u32 = u
        _u32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass


def _exc():
    """异常落盘/回显 —— 但绝不许自己再抛。

    exe 是 --noconsole: sys.stdout/sys.stderr 可能是 None, traceback.print_exc() 直接
    AttributeError。那个异常会往外冒, 被 acquire() 的兜底吃掉 → 变成"当唯一实例"
    → 用户点了图标又多出一个窗口。异常处理绝不能再制造异常。
    """
    try:
        traceback.print_exc()
    except Exception:
        pass


def scope_of(data_dir):
    """数据目录 → 实例作用域。同一个存档目录(大小写/斜杠不同也算同一个)只允许一个窗口壳。"""
    try:
        return os.path.normcase(os.path.abspath(str(data_dir)))
    except Exception:
        return str(data_dir)


def _names(scope):
    h = hashlib.sha1(("MirrorSorter|" + scope).encode("utf-8")).hexdigest()[:12]
    base = "Local\\MirrorSorter-" + h
    return base + "-run", base + "-show", base + "-ack"


class Single:
    """本进程持有的"唯一实例"凭据。start() 之后会监听唤醒事件。"""

    def __init__(self, mutex, show, ack, scope=""):
        self._mutex = mutex
        self._evt = show
        self._ack = ack
        self.scope = scope
        self._on_raise = None
        self._stop = False

    # ------------------------------------------------------------ 监听
    def start(self, on_raise):
        """开始监听"再来一次"的请求。on_raise 会被反复调用, 必须很快返回。"""
        if self._evt is None:
            return False
        self._on_raise = on_raise
        t = threading.Thread(target=self._loop, daemon=True, name="msx-single")
        t.start()
        return True

    def _loop(self):
        while not self._stop:
            try:
                r = _k().WaitForSingleObject(self._evt, 400)
            except Exception:
                return
            if r != WAIT_OBJECT_0:
                continue          # 超时(或句柄出事): 回来看看要不要停
            try:
                if self._on_raise is not None:
                    self._on_raise()
            except Exception:
                _exc()
            finally:
                # 回执放在最外面: 就算唤出失败, 也不能让新进程以为没人接、又起一个
                try:
                    if self._ack:
                        _k().SetEvent(self._ack)
                except Exception:
                    pass

    def dispose(self):
        self._stop = True
        for h in (self._mutex, self._evt, self._ack):
            try:
                if h:
                    _k().CloseHandle(h)
            except Exception:
                pass
        self._mutex = self._evt = self._ack = None


def acquire(scope):
    """抢"唯一实例"。

    → Single: 本进程就是它, 调用方接着 start(on_raise) 并继续启动窗口;
    → None:   已经有一个在跑(并且已经可能唤过它了), 调用方应当直接退出。
    """
    if os.name != "nt":
        return Single(None, None, None, scope)
    try:
        run, show, ack = _names(scope)
        k = _k()
        mutex = k.CreateMutexW(None, False, run)
        if not mutex:
            return Single(None, None, None, scope)     # 系统不给句柄: 当唯一实例
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            try:
                k.CloseHandle(mutex)
            except Exception:
                pass
            return None
        # 事件建在互斥量之后: 副实例若恰好在这个缝隙里读, 会用下面的重试兜住
        evt = k.CreateEventW(None, False, False, show)   # 手动复位=False → 自动复位
        ack_h = k.CreateEventW(None, False, False, ack)
        return Single(mutex, evt, ack_h, scope)
    except Exception:
        _exc()
        return Single(None, None, None, scope)


def wake(scope, timeout=6.0):
    """叫醒已在跑的那个实例。返回 True = 它回执了(说明窗口正在被叫出来)。

    超时说明对方没接(卡死/半死/正在崩溃), 调用方应当提示用户而不是再起一个进程 ——
    两个进程抢同一个 WebView2 数据目录比"没反应"更糟。
    """
    if os.name != "nt":
        return False
    try:
        run, show, ack = _names(scope)
        k = _k()
        # 对方可能刚创建互斥量、还没建事件(毫秒级), 所以给 3 秒的开事件重试
        h_show = None
        end = _now() + 3.0
        while True:
            h_show = k.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False, show)
            if h_show:
                break
            if _now() >= end:
                return False
            threading.Event().wait(0.1)
        try:
            # 把"用户点图标"带来的前台权借给旧进程, 否则它只能任务栏闪一下
            _allow_foreground()
            k.SetEvent(h_show)
            h_ack = k.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False, ack)
            if not h_ack:
                return True        # 没有回执通道: 事件已发出, 当作成功
            try:
                r = k.WaitForSingleObject(h_ack, int(timeout * 1000))
                return r == WAIT_OBJECT_0
            finally:
                k.CloseHandle(h_ack)
        finally:
            k.CloseHandle(h_show)
    except Exception:
        _exc()
        return False


def _now():
    import time
    return time.time()
