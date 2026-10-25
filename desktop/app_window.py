# -*- coding: utf-8 -*-
"""洞鉴虚明 · 桌面窗口壳(内置浏览器·标签页版, v0.2.9 起)。

把服务装进自带的应用窗口: pywebview 驱动系统自带的 WebView2(Edge Chromium 内核),
- 双击启动 → 弹出应用主窗口, 不再依赖外部浏览器;
- 点 feed 卡片/消息/账号等任何视频·帖子链接 → 主窗口内**新增一个内置浏览器标签页**
  (窗口顶部原生"浏览器式"标签栏, 信息流为固定首页标签, 不产生任何新窗口);
- 未装 WebView2 运行时 → 先跑随包的微软官方引导安装包自动装上(一次UAC授权, 需联网);
  缺 pywebview 库或安装失败/被拒 → 自动回退外部浏览器模式(旧行为, 弹提示);
- `python app_window.py --server` → 纯服务模式(等同 python app.py --auto-open)。

实现说明: pywebview 一个窗口只有一个 WebView, 标签页 = Python 端维护的标签表 +
切换时导航同一 WebView; 标签栏是窗口顶部的原生部件(见 shell_tabstrip.py), 不向
页面注入任何元素。已知取舍: 切换标签会重新加载页面(视频播放不跨标签续播);
站内链接在当前标签内正常跳转。

开源依赖: pywebview (MIT, github.com/r0x0r/pywebview), Windows 走 WebView2 运行时。
"""
import io
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app  # noqa: E402  本地服务(纯标准库)

APP_NAME = "洞鉴虚明"
APP_VER = app.APP_VER

# ── 整屏截屏黑屏根治 ─────────────────────────────────────────────────────────
# 症状: 微信/QQ/PrntScr 等 GDI BitBlt 截屏, 播放器区域整块黑(概率性; 开不开超分都黑)。
# 根因(实证定位): Chromium 经 **DirectComposition** 把视频层当独立平面呈现到屏幕,
#   GDI 系截屏读不到该平面 → 截到黑色播放器背景; 窗口/全屏都会犯, 全屏更频繁。
# 关键实证(本机 2560x1600, `_fs_cap.py` 连发 BitBlt 判黑):
#   · 只带 --disable-direct-composition-video-overlays → 8/8、5/8 黑(无效; Chromium 154
#     上这条开关已挡不住视频层, 页内 CSS 圆角才是真正的硬条件)。
#   · 加 --disable-direct-composition → 黑帧清零, 但它把"呈现到窗口"整条从 DComp 换回
#     传统路径: 大窗口 + 弹幕(大量 DOM 动画)时合成代价暴涨, 用户实测"特别特别卡"
#     → **v0.3.1.9dsk 已回退**, 见下。
#   · --disable-gpu 也清零, 但渲染全退软件(极卡、超分 WebGL 报废); --disable-gpu-
#     compositing 会把全屏窗口截成空白 —— 均不采用。
# ★ v0.3.1.9dsk 改为**页内根治**(零性能代价): 给播放器 <video> 一个视觉无感的
#   0.5px 圆角, 使其在 Chromium 的 overlay 候选判定里**失去独立平面资格**
#   (带圆角裁剪的 quad 一律不提升为 overlay), 视频帧并入主合成面 → 既能被 GDI
#   截屏取到, 又完全保留 DComp/GPU 合成。实现见 shell_pages.py 的无条件样式注入
#   (与 ui_sr/ui_ownui 开关无关: 用户实证"不开超分也黑")。
#   这里保留旧开关作冗余声明(无害); setdefault: 用户自设该环境变量则以用户的为准。
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                      "" if os.environ.get("MSX_BASIC") == "1"
                      else "--disable-direct-composition-video-overlays")
MAIN_W, MAIN_H = 1440, 920
MAIN_MIN = (1080, 700)
BG_DARK = "#0f1115"   # 与 web/style.css 暗色 --bg 一致, 避免窗口加载瞬间白闪
# 窗体(控件底衬)背景色: 外部页签左侧留白露出来的就是它。取 B站播放页夜间背景色,
# 这样播放页左边那条留白与页面自身底色连成一片(用户本轮要求: 不要再出现另一种底色)。
# 数值取自用户实机截图采样: 页面底色 rgb(23,24,26)。
BG_PAGE = "#17181a"

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# 地址栏搜索: 设置 → 常规 → 浏览器 里选, 默认 Bing。
# 设置值直接读库(与主页面同一份), 不在壳里再存一份。
SEARCH_ENGINES = {
    "google": ("谷歌", "https://www.google.com/search?q=",
               "https://www.google.com/"),
    "baidu": ("百度", "https://www.baidu.com/s?wd=",
              "https://www.baidu.com/"),
    "bing": ("Bing", "https://www.bing.com/search?q=",
             "https://www.bing.com/"),
}
DEFAULT_SEARCH_ENGINE = "bing"


def search_engine_key():
    try:
        v = str(app.sget("ui_search_engine") or "").strip().lower()
    except Exception:
        v = ""
    return v if v in SEARCH_ENGINES else DEFAULT_SEARCH_ENGINE


def search_engine_url(query, engine=None):
    from urllib.parse import quote_plus
    key = engine if engine in SEARCH_ENGINES else search_engine_key()
    return SEARCH_ENGINES[key][1] + quote_plus(str(query or "").strip())


def search_engine_home(engine=None):
    key = engine if engine in SEARCH_ENGINES else search_engine_key()
    return SEARCH_ENGINES[key][2], SEARCH_ENGINES[key][0]


def _norm_nav_url(raw):
    """地址栏输入 → 可导航 URL; 不合法返回 None。
    无协议头自动补 https://; 只放行 http(s) 且主机名像主机(有点分/localhost)。"""
    u = (raw or "").strip()
    if not u:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", u):
        u = "https://" + u
    if not _URL_RE.match(u):
        return None   # file:/javascript:/data: 等一律拒绝(安全约束同 open_in_app)
    try:
        p = urlparse(u)
    except ValueError:
        return None
    host = p.hostname or ""
    if not host or " " in host:
        return None
    if host != "localhost" and "." not in host:
        return None
    return u
_WV2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"   # WebView2 Evergreen 运行时
_WV2_BOOTSTRAP = ("WebView2Runtime", "MicrosoftEdgeWebview2Setup.exe")   # 微软官方引导包(随包分发)


def webview2_available():
    """检测系统 WebView2 运行时: 注册表 pv 值, 或环境变量指向的可用内核
    (Edge 浏览器兜底, 见 find_edge_browser_dir)。检测不了/没装都按缺失。"""
    if os.name != "nt":
        return False
    env_dir = os.environ.get("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER") or ""
    if env_dir and (os.path.isfile(os.path.join(env_dir, "msedgewebview2.exe"))
                    or os.path.isfile(os.path.join(env_dir, "msedge.exe"))):
        return True
    try:
        import winreg
    except ImportError:
        return False
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % _WV2_CLIENT_ID),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % _WV2_CLIENT_ID),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\%s" % _WV2_CLIENT_ID),
    ):
        try:
            with winreg.OpenKey(root, path) as k:
                pv = str(winreg.QueryValueEx(k, "pv")[0]).strip()
                if pv and pv != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def find_edge_browser_dir():
    """找已安装的 Edge 浏览器内核目录(取最高版本, 含 msedge.exe)。
    WebView2 加载器官方支持直接用 Edge 浏览器二进制充当运行时 —— 运行时装不上
    (EdgeUpdate 机制损坏)的机器借此免安装获得完整 WebView2 能力。找不到返回 None。"""
    if os.name != "nt":
        return None
    roots = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
        base = os.environ.get(var)
        if base:
            roots.append(os.path.join(base, "Microsoft", "Edge", "Application"))
    best, best_ver = None, ()
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not re.match(r"^\d+\.\d+\.\d+\.\d+$", name):
                continue
            p = os.path.join(root, name)
            if os.path.isfile(os.path.join(p, "msedge.exe")):
                ver = tuple(int(x) for x in name.split("."))
                if ver > best_ver:
                    best, best_ver = p, ver
    return best


def _bundled_resource(*parts):
    """随包资源定位: PyInstaller onefile 在 _MEIPASS 解包目录; 源码运行在脚本目录。"""
    rel = os.path.join(*parts)
    for base in (getattr(sys, "_MEIPASS", ""), HERE):
        if base:
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
    return None


def _run_elevated_wait(args, timeout_s=600, verb="runas"):
    """ShellExecute 拉起外部程序并等待结束, 返回退出码(负数=启动失败/超时)。
    verb="runas" 强制走 UAC 提权 —— 引导安装包清单是 asInvoker, 用 subprocess 直接
    拉起会以普通权限静默失败(全机安装需要管理员), 连 UAC 都不弹(线上实测)。"""
    import ctypes
    from ctypes import wintypes

    class _SEEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", wintypes.LPVOID),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS, SEE_MASK_NOASYNC = 0x40, 0x100
    sei = _SEEI()
    sei.cbSize = ctypes.sizeof(_SEEI)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.lpVerb = verb
    sei.lpFile = args[0]
    sei.lpParameters = " ".join(args[1:]) if len(args) > 1 else None
    sei.nShow = 1   # SW_SHOWNORMAL(/silent 无界面, 不弹窗)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return -1   # 拒绝提权/被策略拦截
    try:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            code = wintypes.DWORD(0)
            if ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 500) == 0:
                ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
                return code.value
        return -2   # 超时
    finally:
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)


def install_webview2_runtime():
    """系统缺 WebView2 运行时 → 跑随包分发的微软官方 Evergreen 引导安装包。
    verb=runas 强制提权(UAC 弹窗), 全机安装必须管理员; 成功后复检返回 True。
    取消授权/断网/被精简系统拦服务 → False, 调用方走外部浏览器兜底(旧行为)。"""
    src = _bundled_resource(*_WV2_BOOTSTRAP)
    if not src:
        return False
    _msg_box("首次使用需要安装 WebView2 运行时（微软官方组件，约需 1-2 分钟）。\n\n"
             "点「确定」开始；若弹出「用户账户控制」窗口请点「是」。\n"
             "需要联网；跳过将以外部浏览器模式打开。")
    code = _run_elevated_wait([src, "/silent", "/install"], timeout_s=600)
    if not (code == 0 and webview2_available()):
        # 实测故障: 精简系统 %TEMP% 缺失/不可写 → 微软安装器报"无法创建临时目录"(0x80040902)。
        # 把 TEMP/TMP 指到 Windows\Temp(提权子进程可写)重试一次; 提权子进程继承本进程环境。
        fallback = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Windows", "Temp")
        old_env = (os.environ.get("TEMP"), os.environ.get("TMP"))
        try:
            if os.path.isdir(fallback):
                os.environ["TEMP"] = fallback
                os.environ["TMP"] = fallback
                code = _run_elevated_wait([src, "/silent", "/install"], timeout_s=600)
        finally:
            if old_env[0] is not None:
                os.environ["TEMP"] = old_env[0]
            if old_env[1] is not None:
                os.environ["TMP"] = old_env[1]
    if not (code == 0 and webview2_available()):
        # 仍失败 → 交互式安装(弹微软官方界面, 用户能看到真实原因)
        code = _run_elevated_wait([src], timeout_s=900)
    if code == 0 and webview2_available():
        return True
    _msg_box("WebView2 运行时安装未完成（错误码 %s）。\n\n"
             "常见原因: 系统临时目录(%%TEMP%%)不可用、精简系统禁用 Edge 更新服务、安全软件拦截。\n"
             "替代方案: 安装 Microsoft Edge 浏览器后重开本应用（应用会自动借用其内核，\n"
             "无需安装 WebView2 运行时）；本次将以外部浏览器模式打开，下次启动会再次尝试。" % code)
    return False


def have_pywebview():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _msg_box(text):
    """无控制台场景(frozen windowed)也能弹的提示框。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, 0x40)   # MB_ICONINFORMATION
    except Exception:
        pass


class ShellApi:
    """暴露给页面的 JS 桥: window.pywebview.api.* —— 兼内置浏览器标签页管理器。

    单窗口单 WebView: 标签页 = Python 端标签表, 切换 = 导航同一 WebView;
    标签条由 Python 在每次页面加载后注入渲染(应用页与远程站都显示)。

    安全: open_in_app/open_external 只接受 http(s):// URL —— 页面侧无法借此
    触发本地文件或命令。
    """

    HOME_ID = 0   # 信息流(应用本体)固定为 0 号标签, 不可关

    def __init__(self):
        self._win = None          # 主窗口, attach() 绑定
        self._app_url = None
        self._pages = None        # WebView2 控件池, bind_pages() 注入
        self._visible_id = self.HOME_ID
        self._strip_update = None  # 原生标签栏重绘回调, bind_strip() 注入
        self._navbar_update = None  # 导航工具栏重绘回调, bind_navbar() 注入
        self._floats = None       # 独立悬浮窗管理器(shell_float), bind_floats() 注入
        self._ai = None           # 内置浏览器右侧 AI 面板(shell_ai), bind_ai_panel() 注入
        self._lock = threading.Lock()
        self._update_started = False
        # float=True 的标签不在主窗口标签栏显示, 而由独立悬浮窗承载(见 shell_float.py)
        self._tabs = [{"id": self.HOME_ID, "url": None, "title": "信息流",
                       "pinned": True, "opened": True, "float": False}]
        self._active = self.HOME_ID
        self._next_id = 1
        # 激活标签最近一次导航状态(收藏 ☆ 的「当前网页」); 0 号信息流永远为空
        self._live_id = self.HOME_ID
        self._live_url = ""

    def attach(self, win, app_url):
        """主窗口创建后绑定窗口引用(供控件池 marshal 与窗口按钮使用)。"""
        self._win = win
        self._app_url = app_url
        self._tabs[0]["url"] = app_url

    # ---- 桥: 前端探测(存在即说明跑在桌面壳里) ----
    def shell_info(self):
        return {"name": "desktop-shell", "version": APP_VER.lstrip("v"),
                "embedded_browser": True, "tabs": True,
                "windows": True, "new_window": self._new_window_enabled(),
                # 只有 Windows 上打包出来的 exe 才能自我替换；源码运行/安卓不显示更新入口
                "self_update": (os.name == "nt" and bool(getattr(sys, "frozen", False)))}

    def _new_window_enabled(self):
        """「信息以新窗口形式打开」开关(设置键 ui_new_window, 默认开)。"""
        try:
            return app.sget("ui_new_window") != "0"
        except Exception:
            return True

    # ---- 桥: 自动更新(关闭窗口 -> 独立 cmd 等文件解锁 -> 覆盖 -> 重启) ----
    def install_update(self):
        """把已下载好的更新包落到原地并重启。

        页面只负责点按钮；校验与替换脚本都在 app.update_prepare_install 里。
        Windows 会锁住正在运行的 exe，所以先拉起独立 cmd，等本进程退出后
        重试 move，换好再从原目录启动 —— 保留原文件名，桌面快捷方式/任务栏
        固定项不会失效。"""
        if os.name != "nt" or not getattr(sys, "frozen", False):
            return "err:自动更新只在 Windows 桌面版 exe 里可用"
        with self._lock:
            if self._update_started:
                return "err:正在更新"
            self._update_started = True
        try:
            st = app.update_progress()
            if st.get("phase") != "ready":
                raise ValueError("更新包还没下载好")
            res = app.update_prepare_install(st.get("path") or "")
            if not res.get("ok"):
                raise ValueError(str(res.get("error") or "准备更新失败"))
            # 无控制台 exe 里 DETACHED_PROCESS + 旧脚本(/dev/null) 会让 cmd 直接退出
            # (实测 rc=1，批处理根本没跑)；只留 CREATE_NO_WINDOW 并显式 DEVNULL。
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # 不能让 cmd/新 exe 继承本进程的 PyInstaller onefile 环境(_PYI_*)：
            # 新 exe 与原 exe 同路径，bootloader 会把它当成子进程去复用旧 _MEI，
            # 旧进程退出删除该目录后，新进程就报 Failed to load Python DLL。
            subprocess.Popen(["cmd.exe", "/c", res["script"]],
                             cwd=os.path.dirname(res["script"]),
                             env=app.update_child_env(),
                             creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as e:
            with self._lock:
                self._update_started = False
            return "err:" + str(e)[:160]
        # 先把返回值交给页面，再关窗；直接在这里 destroy 会让 JS 收不到结果
        threading.Timer(0.9, self._close_for_update).start()
        return "ok"

    def _close_for_update(self):
        try:
            if self._floats is not None:
                self._floats.close_all()
        except Exception:
            pass
        try:
            if self._win is not None:
                self._win.destroy()
        except Exception:
            _log_exc("update/close")

    # ---- 桥: 另存为(导出设置用) ----
    def save_text(self, filename, text):
        """把文本写到用户选定的路径(原生「另存为」对话框)。

        壳里没接 WebView2 的 DownloadStarting, 页面里 `<a download>` + Blob 点了
        没反应(用户 2026-10-09 反馈「下载json文件怎么不生效啊」), 所以桌面壳改走
        原生对话框。返回 "ok:<路径>" / "cancel" / "err:<原因>"。"""
        name = (filename or "").strip() or "导出.json"
        try:
            body = text if isinstance(text, str) else str(text or "")
        except Exception:
            return "err:bad-text"
        win = self._win
        if win is None:
            return "err:no-window"
        try:
            import System.Windows.Forms as WinForms
            form = win.native
        except Exception as e:
            return "err:no-ui(%r)" % (e,)
        box = {}

        def _ask():
            try:
                dlg = WinForms.SaveFileDialog()
                dlg.Title = "保存导出文件"
                dlg.FileName = name
                dlg.Filter = "JSON 文件 (*.json)|*.json|所有文件 (*.*)|*.*"
                dlg.RestoreDirectory = True
                # 默认目录: 用户「下载」夹(没有就桌面/文档), 别让对话框落在程序自己的目录里
                _home = os.path.expanduser("~")
                for _d in ("Downloads", "Desktop", "Documents"):
                    _p = os.path.join(_home, _d)
                    if os.path.isdir(_p):
                        try:
                            dlg.InitialDirectory = _p
                        except Exception:
                            pass
                        break
                res = dlg.ShowDialog(form)
                box["res"] = res
                try:
                    box["ok"] = int(res) == 1     # DialogResult.OK == 1
                except Exception:
                    box["ok"] = str(res).upper().endswith("OK")
                box["path"] = dlg.FileName
            except Exception as e:
                box["err"] = repr(e)

        try:
            if getattr(form, "InvokeRequired", False):
                form.Invoke(WinForms.MethodInvoker(_ask))   # marshal 到 UI 线程(弹模态框)
            else:
                _ask()
        except Exception as e:
            return "err:invoke(%r)" % (e,)
        if box.get("err"):
            return "err:" + str(box["err"])
        if not box.get("ok"):
            return "cancel"
        path = box.get("path") or ""
        if not path:
            return "cancel"
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        except Exception as e:
            return "err:" + repr(e)
        return "ok:" + path

    # ---- 桥: 打开/切换/关闭标签页(app.js openExternal 走 open_in_app) ----
    def open_in_app(self, url, title="", mode=None):
        """打开链接。mode=None → 按设置(默认「新窗口」); "tab"/"window" 强制指定。
        新窗口模式: 建独立悬浮窗承载页面; 标签模式: 老行为(主窗口内新标签)。"""
        url = (url or "").strip()
        if not _URL_RE.match(url):
            return "bad-url"
        if mode not in ("tab", "window"):
            mode = "window" if self._new_window_enabled() else "tab"
        want_float = (mode == "window")
        if _DIAG["trace_open"]:   # 仅 e2e 自测时落盘, 免得平时刷日志
            _log_line("open_in_app mode=%r want_float=%s pages=%s mgr=%s url=%s" % (
                mode, want_float, self._pages is not None, self._floats is not None, url))
        with self._lock:
            tab = next((t for t in self._tabs if t["url"] == url), None)   # 同链接去重
            if tab is None:
                tab = {"id": self._next_id, "url": url,
                       "title": (title or url)[:24], "pinned": False,
                       "opened": False, "float": want_float}
                self._tabs.append(tab)
                self._next_id += 1
                if not want_float:
                    self._active = tab["id"]
        if tab.get("float"):
            self._open_float(tab)
        else:
            self._activate(tab)
        self._render()   # 即时反馈: 标签栏立刻高亮新标签(不等页面加载完)
        return "ok"

    def drop_miss(self, types=""):
        """拖拽诊断: 页面认不出拖入内容时上报 MIME 类型, 只记日志(用户不可见)。"""
        try:
            _log_line("drop-miss types=%s" % str(types or "")[:200])
        except Exception:
            pass
        return "ok"

    def on_drop(self, tab_key, url):
        """外部浏览器把网页/链接拖进窗口(站点控件的 document-start 脚本经 WebMessage 上报)。
        拖进来的链接一律作为**主窗口新标签页**打开 —— 拖进窗口 = 窗口内新开, 不打断
        当前页; 「信息以新窗口形式打开」只决定点开信息的默认方式, 不跟着变。"""
        u = _norm_nav_url(url)
        if not u:
            return "bad-url"
        _log_line("drop-open tab=%s url=%s" % (tab_key, u[:200]))
        return self.open_in_app(u, mode="tab")

    def _open_float(self, tab):
        """把标签的页面放进一个独立悬浮窗(控件池缺失时退化为主窗口标签)。"""
        pages, mgr = self._pages, self._floats
        if pages is None or mgr is None:
            _log_line("open-float 降级为标签: pages=%s mgr=%s" % (pages is not None,
                                                                mgr is not None))
            tab["float"] = False
            with self._lock:
                self._active = tab["id"]
            self._activate(tab)
            return
        if not tab["opened"]:
            tab["opened"] = True
            pages.create(tab["url"], tab["id"])
        mgr.open(tab, pages)

    def tab_new(self, kind="browser"):
        """标签栏「+」新建菜单: browser=搜索引擎首页, feed=副信息流。
        两者都可关闭; 0 号信息流仍是固定首页。"""
        kind = str(kind or "").strip()
        if kind not in ("browser", "feed"):
            return "bad-kind"
        if kind == "browser":
            url, title = search_engine_home()
        else:
            url, title = self._app_url, "副信息流"
        with self._lock:
            tab = {"id": self._next_id, "url": url, "title": title,
                   "pinned": False, "opened": False, "float": False}
            self._next_id += 1
            self._tabs.append(tab)
            self._active = tab["id"]
        self._activate(tab)
        self._render()
        return "ok"

    def open_startup_browser(self):
        """设置「启动时打开浏览器」= 开启 → 窗口就绪后开一个搜索引擎首页标签。
        由 gui_ready 延迟调用(不能更早: 新控件要借 0 号控件的 WebView2 数据目录)。"""
        try:
            if app.sget("ui_browser_on_start") != "1":
                return False
            return self.tab_new("browser") == "ok"
        except Exception:
            _log_exc("startup-browser")
            return False

    def tab_switch(self, tid=None):
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return "bad-id"
        with self._lock:
            t = next((x for x in self._tabs if x["id"] == tid), None)
            if t is None:
                return "gone"
            self._active = tid
        self._activate(t)
        self._render()
        return "ok"

    def tab_close(self, tid=None):
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return "bad-id"
        with self._lock:
            idx = next((i for i, x in enumerate(self._tabs) if x["id"] == tid), None)
            if idx is None:
                return "gone"
            if self._tabs[idx]["pinned"]:
                return "pinned"
            is_float = bool(self._tabs[idx].get("float"))
            self._tabs.pop(idx)
            if not is_float and self._active == tid:   # 关的是主窗口当前页 → 激活前一个
                prev = next((x for x in reversed(self._tabs[:idx])
                             if not x.get("float")), None)
                self._active = prev["id"] if prev else self.HOME_ID
            nxt = next((x for x in self._tabs
                        if x["id"] == self._active and not x.get("float")), None)
            if nxt is None:   # 激活项是悬浮标签/已消失 → 兜底回信息流
                self._active = self.HOME_ID
                nxt = next((x for x in self._tabs if x["id"] == self.HOME_ID), None)
        if is_float and self._floats is not None:
            self._floats.close(tid)   # 摘出控件并销毁悬浮窗(控件随后由控件池销毁)
        if nxt is not None:
            self._activate(nxt)       # 先显示邻居, 再销毁旧控件(避免黑屏)
        if self._pages is not None:
            self._pages.dispose(tid)
            if nxt is not None:
                # 销毁兜底: WebView2(DirectComposition)销毁偶发打断窗口合成层, 内容区整块
                # 变黑而标签栏正常(用户截图) —— 强制再补一次邻居可见+焦点, 让其重新present。
                self._pages.show(self._active)
        self._render()
        return "ok"

    def tab_detach(self, tid=None):
        """把主窗口里的标签页拽出成独立悬浮窗(标签栏拖拽 → 这里)。
        控件实例不变(不重载); 被拖出的标签不再出现在标签栏。"""
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return "bad-id"
        with self._lock:
            t = next((x for x in self._tabs if x["id"] == tid), None)
            if t is None:
                return "gone"
            if t["pinned"]:
                return "pinned"
            if t.get("float"):
                return "already"
        # ⚠️ bug2: 这里曾用 _new_window_enabled() 把拖出一并挡掉(返回 "off")。
        # 「信息以新窗口形式打开」只决定"点开信息"默认怎么打开(见 open_in_app), 不该把
        # "把已有标签页拽成悬浮窗"这条入口一起关掉 —— 该开关关掉后依然要能拖出。
        pages, mgr = self._pages, self._floats
        if pages is None or mgr is None:
            return "no-pages"
        with self._lock:
            was_active = (self._active == tid)
            t["float"] = True
            if was_active:   # 主窗口切到相邻标签(没有则回信息流)
                nxt = next((x for x in self._tabs
                            if x["id"] != tid and not x.get("float")), None)
                self._active = nxt["id"] if nxt else self.HOME_ID
            active_tab = next((x for x in self._tabs
                               if x["id"] == self._active and not x.get("float")), None)
        # 顺序关键: 先把主窗口切走(隐藏该控件) → 再把它迁进悬浮窗, 否则 hide 会把
        # 悬浮窗里的控件一起隐藏。
        if active_tab is not None:
            self._activate(active_tab)
        if not t["opened"]:
            t["opened"] = True
            pages.create(t["url"], tid)
        try:
            mgr.open(t, pages)
        except Exception:   # 悬浮窗创建失败时别把 pywebview 的 'Uncaught' 丢给用户: 记栈并回因
            import traceback as _tb
            _tb.print_exc()
            _log_exc("tab_detach/float-open")
            return "float-open-failed: " + (_tb.format_exc().strip().splitlines() or [""])[-1]
        self._render()
        return "ok"

    def window_dock(self, tid=None):
        """把悬浮窗塞回主窗口变成一个标签页(悬浮窗迷你标题栏 ⤓)。"""
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return "bad-id"
        with self._lock:
            t = next((x for x in self._tabs if x["id"] == tid), None)
            if t is None:
                return "gone"
            if not t.get("float"):
                return "not-float"
        if self._floats is not None:
            self._floats.dock(tid)    # 控件迁回主窗口(隐藏), 销毁悬浮窗
        with self._lock:
            t["float"] = False
            self._active = tid
        self._activate(t)             # 显示到主窗口内容区 + 补推导航状态
        if self._pages is not None:
            # 从悬浮窗塞回标签页 = 又一次"宿主编排变化"(控件换回父窗体), 合成层同样会被
            # 打断 → 补一次呈现。悬浮窗侧只负责"迁出去"那次, "迁回来"这次得在这里补。
            try:
                self._pages.represent(tid, "undock")
            except Exception:
                pass
        self._render()
        return "ok"

    def open_external(self, url):
        """用户显式要求用系统浏览器时(标签条 ⧉ 按钮)。"""
        url = (url or "").strip()
        if not _URL_RE.match(url):
            return "bad-url"
        webbrowser.open(url)
        return "ok"

    def on_side_button(self, back=True):
        """鼠标侧键(后退/前进): 当前标签页内回退/前进; 后退到本标签最早的页(无历史)
        时整个标签视为一次"返回" —— 关掉它回到来源标签/信息流。信息流是应用本体,
        没有"返回"概念, 忽略。仅在 UI 线程调用(由 _install_mouse_side_buttons 触发)。"""
        pages = self._pages
        if pages is None:
            return
        with self._lock:
            t = next((x for x in self._tabs if x["id"] == self._active), None)
            tid = t["id"] if t else None
            pinned = bool(t and t["pinned"])
        if tid is None or pinned:
            return
        if pages.navigate_history(tid, back):
            return
        if not back:
            return   # 前进无历史: 无事可做
        self.tab_close(tid)

    # ---- 导航工具栏(navbar 按钮/地址栏走这套; 语义与鼠标侧键不同 —— 首页无历史
    #      时按钮禁用/无事发生, 绝不"关标签") ----
    def _active_id(self):
        with self._lock:
            return self._active

    def active_page(self):
        """当前激活标签页的网址/标题/是否固定首页(收藏栏与 ☆ 按钮用)。
        信息流固定页是本地服务, 网址对外一律视为空。"""
        with self._lock:
            t = next((x for x in self._tabs if x["id"] == self._active), None)
            if t is None:
                return {"id": None, "url": "", "title": "", "pinned": True}
            pinned = bool(t.get("pinned"))
            url = "" if pinned else (self._live_url if self._live_id == t["id"] else "")
            return {"id": t["id"], "url": url,
                    "title": str(t.get("title") or ""), "pinned": pinned}

    def nav_back(self):
        pages = self._pages
        if pages is not None:
            pages.navigate_history(self._active_id(), True)
        return "ok"

    def nav_forward(self):
        pages = self._pages
        if pages is not None:
            pages.navigate_history(self._active_id(), False)
        return "ok"

    def nav_reload(self):
        pages = self._pages
        if pages is not None:
            pages.reload(self._active_id())
        return "ok"

    def nav_goto(self, url):
        """地址栏回车: 当前标签页内导航(不新开标签, 与浏览器一致)。"""
        u = _norm_nav_url(url)
        if u is None:
            return "bad-url"
        pages = self._pages
        if pages is None:
            return "no-pages"
        tid = self._active_id()
        pages.goto(tid, u)
        pages.show(tid)   # 焦点交回页面(浏览器习惯: 导航后地址栏失焦)
        return "ok"

    def nav_input(self, raw):
        """地址栏回车入口: 网址直接去; 普通文字按默认搜索引擎搜。
        带协议头却不是 http(s) 的输入(file:/javascript: 等)仍然拒绝。"""
        raw = str(raw or "").strip()
        u = _norm_nav_url(raw)
        if u is not None:
            return self.nav_goto(u)
        if not raw or re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw):
            return "bad-url"
        pages = self._pages
        if pages is None:
            return "no-pages"
        tid = self._active_id()
        pages.goto(tid, search_engine_url(raw))
        pages.show(tid)
        return "ok"

    # ---- 内部 ----
    def _activate(self, tab):
        """显示 tab(无控件则异步创建), 隐藏当前控件 —— 纯可见性切换, 页面零重载。"""
        self._sync_ai(tab)   # 网页标签才显示右侧 AI 面板
        pages = self._pages
        if pages is None or tab.get("float"):
            return   # 悬浮标签不在主窗口显示(由 shell_float 承载)
        with self._lock:
            prev_id = self._visible_id
            self._visible_id = tab["id"]
        if not tab["pinned"] and not tab["opened"]:
            tab["opened"] = True
            pages.create(tab["url"], tab["id"])
        # 顺序要紧: 先 show 新控件再 hide 旧的。直接隐藏持有键盘焦点的 WebView2
        # 会让 Windows 把焦点甩给地址栏(它因 GotFocus 全选), 切标签时网址会闪一下
        # 被选中。新控件先拿到焦点, 焦点就不会经过地址栏。
        pages.show(tab["id"])
        if prev_id != tab["id"]:
            pages.hide(prev_id)
        pages.push_nav(tab["id"])   # 切标签后补推导航状态(防地址栏显示旧 URL)

    def update_title(self, tab_key, title):
        """站点控件的 DocumentTitleChanged 回调 → 刷新标签标题(悬浮窗同步迷你标题栏)。"""
        title = str(title or "").strip()[:24]
        if not title:
            return
        is_float = False
        with self._lock:
            t = next((x for x in self._tabs if x["id"] == tab_key), None)
            if t is not None and not t["pinned"]:
                t["title"] = title
                is_float = bool(t.get("float"))
        if is_float and self._floats is not None:
            self._floats.set_title(tab_key, title)
        self._render()

    def bind_pages(self, pages):
        """注入 WebView2 控件池(shell_pages.create_pool)并显示首页标签。"""
        self._pages = pages
        try:
            # 页面 rAF 停摆(评论区下拉卡死) → /api/win/repaint → 当前标签补呈现一次。
            app.set_repaint_hook(lambda: pages.rescue(self._active_id(), "page-stall"))
        except Exception:
            pass
        self._activate(self._tabs[0])
        self._render()

    def bind_floats(self, mgr):
        """注入悬浮窗管理器(shell_float.install_floats 的返回值)。"""
        self._floats = mgr

    def bind_ai_panel(self, panel):
        """注入内置浏览器右侧 AI 面板(shell_ai.install_ai_panel 的返回值)。"""
        self._ai = panel
        if panel is not None:
            try:
                # 面板自己开合(面板里的 ✕ / 展开按钮)后, 导航栏那颗 AI 按钮要跟着变
                panel.on_change = self._push_ai_state
            except Exception:
                pass
        self._sync_ai()

    def _sync_ai(self, tab=None):
        """按当前标签决定面板显不显示: 只有网页标签显示(信息流自带右栏 AI 聊天)。"""
        p = self._ai
        if p is None:
            return
        try:
            if tab is None:
                with self._lock:
                    tid = self._active
                    tab = next((x for x in self._tabs if x["id"] == tid), None)
            browser_tab = bool(tab) and not tab.get("pinned") and not tab.get("float")
            p.sync_for_tab(browser_tab)
        except Exception:
            _log_exc("ai/sync")

    def _push_ai_state(self, on=None):
        fn = self._navbar_update
        if fn is None:
            return
        try:
            setter = getattr(fn, "set_ai", None)
            if setter is not None:
                setter(bool(on) if on is not None else
                       bool(self._ai is not None and self._ai.is_open()))
        except Exception:
            pass

    def toggle_ai_panel(self):
        """导航栏 AI 按钮: 开/关右侧助手面板。"""
        p = self._ai
        if p is None:
            return "no-panel"
        try:
            on = p.toggle()
        except Exception:
            _log_exc("ai/toggle")
            return "err"
        self._push_ai_state(on)
        return "on" if on else "off"

    def bind_strip(self, update_fn):
        """注入原生标签栏重绘回调(shell_tabstrip.install_strip 的返回值)。"""
        self._strip_update = update_fn
        self._render()

    def bind_navbar(self, update_fn):
        """注入导航工具栏重绘回调(shell_navbar.install_navbar 的返回值)。"""
        self._navbar_update = update_fn
        self._push_nav("", False, False)   # 初值: 首页是本地服务, 地址栏留空

    def _on_nav(self, tab_key, url, can_back, can_fwd):
        """控件池的导航状态回调(HistoryChanged/SourceChanged/push_nav)。
        只反映激活标签 —— 后台标签的导航不刷地址栏。"""
        with self._lock:
            active = self._active
            pinned = next((x["pinned"] for x in self._tabs if x["id"] == active), True)
        if tab_key != active:
            return
        # 信息流是本地服务(127.0.0.1), 网址没有意义 —— 地址栏留空(用户要求)
        u = "" if pinned else str(url or "")
        with self._lock:
            self._live_id = tab_key
            self._live_url = u
        self._push_nav(u, can_back, can_fwd)

    def _push_nav(self, url, can_back, can_fwd):
        fn = self._navbar_update
        if fn is None:
            return
        try:
            fn(str(url or ""), bool(can_back), bool(can_fwd))
        except Exception:
            pass

    def _render(self):
        """把标签表同步到原生标签栏(跨线程安全, 由 strip 侧 marshal 到 UI 线程)。
        悬浮标签(float)不在标签栏显示 —— 它们由独立悬浮窗承载。"""
        fn = self._strip_update
        if fn is None:
            return
        try:
            with self._lock:
                tabs = [{"id": t["id"], "title": t["title"] or "页面",
                         "closable": not t["pinned"]}
                        for t in self._tabs if not t.get("float")]
                active = self._active
            if not any(t["id"] == active for t in tabs):
                active = self.HOME_ID
            fn(tabs, active)
        except Exception:
            pass


def _start_server_thread():
    """起服务(或复用已运行的实例)。返回 (server_or_None, port)。"""
    server, port = app.run_server(auto_open=False)
    if server is not None:
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="http-server").start()
    return server, port


def _serve_forever_cli(server):
    """--server 模式: 与 python app.py 一致的阻塞语义(Ctrl+C 退出并收尾)。"""
    try:
        server.serve_forever()
    finally:
        app.shutdown(server)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    e2e_url = None
    if "--e2e-open" in argv:   # 测试钩子: 启动后自动经桥开一个内置浏览器标签页
        try:
            e2e_url = argv[argv.index("--e2e-open") + 1]
        except IndexError:
            e2e_url = None
    e2e_float_url = None
    if "--e2e-float" in argv:  # 测试钩子: 走一遍 新窗口→塞回标签页→拖出→关闭 全链路
        try:
            e2e_float_url = argv[argv.index("--e2e-float") + 1]
        except IndexError:
            e2e_float_url = None
    e2e_ai_url = None
    if "--e2e-ai" in argv:     # 测试钩子: 内置浏览器右侧 AI 面板(停靠/让位/网页操作)
        try:
            e2e_ai_url = argv[argv.index("--e2e-ai") + 1]
        except IndexError:
            e2e_ai_url = None
    e2e_split = "--e2e-split" in argv   # 测试钩子: 量化拖动分栏时的布局/重绘次数
    e2e_float_resize_url = None
    if "--e2e-float-resize" in argv:  # 测试钩子: 真鼠标消息驱动悬浮窗八向缩放
        try:
            e2e_float_resize_url = argv[argv.index("--e2e-float-resize") + 1]
        except IndexError:
            e2e_float_resize_url = None
    # 悬浮球模式: frozen exe 再入自身时走这里; 源码模式 float_ball_spawn 直接调 app.py
    if "--float-ball" in argv:
        try:
            _port = int(argv[argv.index("--float-ball") + 1])
        except (ValueError, IndexError):
            _port = 8000
        try:
            app.run_float_ball(_port)
        except Exception:
            import traceback
            try:
                with open(os.path.join(app.DATA_DIR, "float_ball_err.log"), "w",
                          encoding="utf-8") as _f:
                    traceback.print_exc(file=_f)
            except Exception:
                pass
        return 0
    if "--server" in argv:   # 纯服务模式(不建窗口)
        sys.argv = [sys.argv[0]] + [a for a in argv if a != "--server"]
        server, port = app.run_server(
            auto_open=("--auto-open" in sys.argv) or bool(getattr(sys, "frozen", False)))
        if server is None:
            return 0 if port is not None else 1
        _serve_forever_cli(server)
        return 0
    return shell_main(e2e_url=e2e_url, e2e_float_url=e2e_float_url,
                      e2e_float_resize_url=e2e_float_resize_url,
                      e2e_ai_url=e2e_ai_url, e2e_split=e2e_split)


def _install_mouse_side_buttons(api, win):
    """低级鼠标钩子(WH_MOUSE_LL)拦截本窗口内的侧键(后退/前进), 不交给 WebView2
    原生处理。

    原生路径: WebView2 收到 XButton 后在浏览器进程内同步执行 GoBack, 与本壳的
    无边框窗口 + 控件可见性切换叠加时有概率整窗卡死(用户实测)。侧键语义改由
    ShellApi.on_side_button 在 UI 线程实现: 标签页内回退, 到头关标签回信息流。
    钩子按光标落点查根窗口, 只吞本窗口的侧键, 不影响其他应用; 独立线程装钩并
    跑消息泵 —— 低级钩子所在线程不泵消息会被系统按超时静默摘除。pythonnet 3
    无法子类化 IMessageFilter(WinForms 消息过滤器方案不可行), 故走 ctypes。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WH_MOUSE_LL = 14
    WM_XBUTTONDOWN, WM_XBUTTONUP, WM_XBUTTONDBLCLK = 0x020B, 0x020C, 0x020D
    GA_ROOT = 2

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    LOWPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                 ctypes.c_size_t, ctypes.c_ssize_t)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      ctypes.c_size_t, ctypes.c_ssize_t]
    root = int(win.native.Handle.ToInt64())   # pythonnet IntPtr 无 int() 转换

    def _in_our_window(pt):
        try:
            h = user32.WindowFromPoint(wintypes.POINT(pt.x, pt.y))
            if not h:
                return False
            top = user32.GetAncestor(h, GA_ROOT)
            return int(top or 0) == root or int(h or 0) == root
        except Exception:
            return False

    def _proc(n_code, w_param, l_param):
        try:
            if n_code >= 0 and w_param in (WM_XBUTTONDOWN, WM_XBUTTONUP,
                                           WM_XBUTTONDBLCLK):
                info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if _in_our_window(info.pt):
                    if w_param == WM_XBUTTONDOWN:
                        # mouseData 高16位: 1=XBUTTON1(后退) 2=XBUTTON2(前进)
                        back = ((info.mouseData >> 16) & 0xFFFF) == 1
                        try:
                            import System.Windows.Forms as WinForms
                            form = api._win.native
                            form.BeginInvoke(WinForms.MethodInvoker(
                                lambda: api.on_side_button(back=back)))
                        except Exception:
                            pass
                    return 1   # 吞掉: WebView2/页面收不到这套按键
        except Exception:
            pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    proc_ref = LOWPROC(_proc)   # 持引用防 GC(钩子回调悬空会崩进程)

    def _run():
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, proc_ref, None, 0)
        if not hook:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)

    threading.Thread(target=_run, daemon=True,
                     name="msx-side-btn-hook").start()


_DIAG = {"trace_open": False}   # e2e 自测打开它 → open_in_app 的决策落盘(平时不写)


def _log_line(msg):
    """诊断行落盘(打包 exe 无 stdout): 排查"某个部件没装上/走了降级分支"。"""
    try:
        with open(os.path.join(app.DATA_DIR, "shell_err.log"), "a",
                  encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _log_exc(tag):
    """窗口壳里的异常落盘: 打包后的 exe 是 --noconsole(无 stdout), print 会丢,
    排查"某个部件没装上"时看不到原因 —— 这里同时 print 与写 <数据目录>/shell_err.log。"""
    import traceback
    try:
        traceback.print_exc()
    except Exception:
        pass
    try:
        with open(os.path.join(app.DATA_DIR, "shell_err.log"), "a",
                  encoding="utf-8") as f:
            f.write("\n==== %s ====\n" % tag)
            traceback.print_exc(file=f)
    except Exception:
        pass


def _e2e_float_resize_run(api, url):
    """v0.3.1.51 自测: 用**真实鼠标消息**驱动悬浮窗八向缩放, 记录每步 Bounds 变化。

    为什么不用 form.Size=...: 那只是调 API, 证明不了「用户拖得动」。这里往热区 Panel 投递
    WM_LBUTTONDOWN/MOUSEMOVE/WM_LBUTTONUP —— 而 shell_float 的 _step_resize 读的是
    `WinForms.Cursor.Position`, 所以投递前必须把真实光标摆到热区上再挪动, 与
    _e2e_drag_to_tabbar 同一套办法, 等价于真人拖拽。

    每一步都断言「尺寸真的变了 + 被拖的那条边方向对 + 钉住的边没动」, 最后压一遍最小尺寸
    钳制; 结果同时写 <数据目录>/float_resize_e2e.log, 供外部脚本判定(pass/fail 计数)。
    """
    import ctypes
    import System.Windows.Forms as WinForms
    from System.Drawing import Point as SPoint
    u32 = ctypes.windll.user32
    logp = os.path.join(app.DATA_DIR, "float_resize_e2e.log")
    lines, fails = [], []

    def say(s):
        lines.append("[float-resize] " + s)
        try:
            print(lines[-1], flush=True)
        except Exception:
            pass
        try:
            with open(logp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def ok(name, cond, detail=""):
        say("%s %s %s" % ("PASS" if cond else "FAIL", name, detail))
        if not cond:
            fails.append(name)

    def zones_of(f):
        """热区 Panel → {方向: 控件}。缩放过程中面板会跟着重排, 每次都要重算。"""
        out = {}
        for i in range(f.Controls.Count):
            c = f.Controls[i]
            try:
                if c.GetType().Name != "Panel":
                    continue
                cur = c.Cursor
                n = None
                if cur == WinForms.Cursors.SizeWE:
                    n = "w" if c.Left <= 2 else "e"
                elif cur == WinForms.Cursors.SizeNS:
                    n = "n" if c.Top <= 2 else "s"
                elif cur == WinForms.Cursors.SizeNWSE:
                    n = "nw" if (c.Left <= 2 and c.Top <= 2) else "se"
                elif cur == WinForms.Cursors.SizeNESW:
                    n = "ne" if c.Left > 2 else "sw"
                if n:
                    out[n] = c
            except Exception:
                continue
        return out

    # 假光标: 缩放读的是 shell_float 的 _cursor_pos(), 注入后与真鼠标无关 ——
    # 无人值守跑在真桌面上时, 真人一动鼠标也不会把位移量搅乱(见 shell_float.set_cursor_probe)。
    _cur = [(0, 0)]

    def drag(f, name, dx, dy):
        z = zones_of(f).get(name)
        if z is None:
            ok("热区-" + name, False, "没找到该方向的热区")
            return None
        b0 = f.Bounds
        cx = z.Left + max(1, z.Width // 2)
        cy = z.Top + max(1, z.Height // 2)
        p = f.PointToScreen(SPoint(cx, cy))
        lp = (((cy & 0xFFFF) << 16) | (cx & 0xFFFF))
        hwnd = z.Handle.ToInt32()
        # 光标摆到热区 → 按下(记起点) → 挪光标 → 投递 MOVE(改 Bounds) → 松手。
        # 投递的是真鼠标消息, 走的是产品代码里热区 Panel 的事件链, 只是"光标在哪"由我们喂。
        _cur[0] = (p.X, p.Y)
        u32.SendMessageW(hwnd, 0x0201, 1, lp)
        _cur[0] = (p.X + dx, p.Y + dy)
        u32.SendMessageW(hwnd, 0x0200, 1, lp)
        time.sleep(0.06)
        u32.SendMessageW(hwnd, 0x0202, 0, lp)
        time.sleep(0.35)
        b1 = f.Bounds
        say("drag %-2s b=%s→%s 位移 期望%+d/%+d 实际%+d/%+d"
            % (name, (b0.X, b0.Y, b0.Width, b0.Height),
               (b1.X, b1.Y, b1.Width, b1.Height), dx, dy,
               b1.X - b0.X, b1.Y - b0.Y))
        return (b0, b1)

    try:
        time.sleep(8)   # 等主窗口与服务就绪
        scale = float(getattr(api._win.native, "_scale", 1.0) or 1.0)
        # 用带 query 的地址: open_in_app 按 url 去重, 首页标签已经占了不带参数的同一个地址,
        # 直接传原地址会命中已有标签(切过去), 根本不会开悬浮窗。
        furl = url.rstrip("/") + "/?e2e=float-resize"
        say("scale=%s open -> %s" % (scale, api.open_in_app(furl, "缩放自测", mode="window")))
        time.sleep(7)
        tid = next((t["id"] for t in api._tabs if t.get("float") and t.get("url") == furl), None)
        if tid is None:
            tid = next((t["id"] for t in api._tabs if t.get("float")), None)
        ctl = api._pages.control(tid) if tid is not None else None
        if ctl is None or ctl.Parent is None:
            ok("悬浮窗已打开", False, "找不到悬浮窗控件 tid=%s" % tid)
            return
        f = ctl.Parent
        bar = next((c for i in range(f.Controls.Count) for c in [f.Controls[i]]
                    if getattr(c, "Dock", None) == WinForms.DockStyle.Top), None)
        bar_h = int(bar.Height) if bar is not None else 0
        b = f.Bounds
        say("initial bounds=%s client=%s min=%s bar_h=%s state=%s"
            % (b, f.ClientSize, f.MinimumSize, bar_h, f.WindowState))
        ok("初始是普通窗口", f.WindowState == WinForms.FormWindowState.Normal and b.Width > 300,
           "state=%s w=%d" % (f.WindowState, b.Width))
        ok("八向热区齐全", len(zones_of(f)) == 8, "zones=%s" % sorted(zones_of(f)))
        import shell_float as _sf
        _sf.set_cursor_probe(lambda: _cur[0])

        r = drag(f, "e", 140, 0)
        if r:
            ok("右边缘最右拖 +140 → 宽度 +140, 左边不动",
               abs((r[1].Width - r[0].Width) - 140) <= 12 and r[1].X == r[0].X,
               "dw=%d dx=%d" % (r[1].Width - r[0].Width, r[1].X - r[0].X))
        r = drag(f, "w", 80, 0)
        if r:
            ok("左边缘右拖 +80 → 左边右移, 宽度 -80",
               abs((r[1].X - r[0].X) - 80) <= 12 and abs((r[1].Width - r[0].Width) + 80) <= 12,
               "dx=%d dw=%d" % (r[1].X - r[0].X, r[1].Width - r[0].Width))
        r = drag(f, "s", 0, 90)
        if r:
            ok("下边缘下拖 +90 → 高度 +90, 上边不动",
               abs((r[1].Height - r[0].Height) - 90) <= 12 and r[1].Y == r[0].Y,
               "dh=%d dy=%d" % (r[1].Height - r[0].Height, r[1].Y - r[0].Y))
        r = drag(f, "se", 120, 70)
        if r:
            ok("右下角拖 +120/+70 → 宽高同时变大",
               abs((r[1].Width - r[0].Width) - 120) <= 14 and abs((r[1].Height - r[0].Height) - 70) <= 14,
               "dw=%d dh=%d" % (r[1].Width - r[0].Width, r[1].Height - r[0].Height))
        r = drag(f, "n", 0, 60)
        if r:
            ok("上边缘下拖 +60 → 上边下移, 高度 -60",
               abs((r[1].Y - r[0].Y) - 60) <= 12 and abs((r[1].Height - r[0].Height) + 60) <= 12,
               "dy=%d dh=%d" % (r[1].Y - r[0].Y, r[1].Height - r[0].Height))
        # 最小尺寸钳制: 往内拖 3000px, 宽度/高度必须停在最小尺寸上
        mw, mh = int(f.MinimumSize.Width), int(f.MinimumSize.Height)
        r = drag(f, "e", -3000, 0)
        if r:
            ok("右边缘往左拖到底 → 宽度停在最小 %d" % mw, r[1].Width == mw and r[1].X == r[0].X,
               "w=%d" % r[1].Width)
        r = drag(f, "s", 0, -3000)
        if r:
            ok("下边缘往上拖到底 → 高度停在最小 %d" % mh, r[1].Height == mh,
               "h=%d" % r[1].Height)
        # 缩到最小后网页内容区必须跟着缩(不是只换了外框)
        cw, ch = f.ClientSize.Width, f.ClientSize.Height
        say("after-min client=%s webview=%s" % (f.ClientSize, ctl.Size))
        ok("网页内容区随窗口变化", int(ctl.Width) == int(cw) and int(ctl.Height) <= int(ch) - bar_h + 2,
           "ctl=%s client=%s bar_h=%d" % (ctl.Size, f.ClientSize, bar_h))
        # 放大回来再确认一次(缩小→放大这条路上也得分得到 Move)
        r = drag(f, "se", 200, 160)
        if r:
            ok("右下角再放大 +200/+160", r[1].Width >= r[0].Width + 150 and r[1].Height >= r[0].Height + 120,
               "dw=%d dh=%d" % (r[1].Width - r[0].Width, r[1].Height - r[0].Height))
        say("final bounds=%s" % f.Bounds)
        # 最大化态拖边: 应先还原成上次的浮动尺寸, 再从那个框继续缩放(与系统窗口一致)。
        # 用标题栏右数第二枚「□」按钮进最大化(走产品自己的按钮判定 + _toggle_max)。
        norm = f.Bounds
        bw = int(round(34 * scale))
        bx = int(bar.Width) - int(bw * 1.5)
        by = max(2, int(bar.Height) // 2)
        lp2 = (((by & 0xFFFF) << 16) | (bx & 0xFFFF))
        u32.SendMessageW(bar.Handle.ToInt32(), 0x0201, 1, lp2)
        u32.SendMessageW(bar.Handle.ToInt32(), 0x0202, 0, lp2)
        time.sleep(0.8)
        ok("点标题栏「□」进入最大化", f.WindowState == WinForms.FormWindowState.Maximized,
           "state=%s" % f.WindowState)
        r = drag(f, "e", 100, 0)
        if r:
            want_w = int(norm.Width) + 100
            ok("最大化态拖边 → 先还原成浮动尺寸再缩放",
               f.WindowState == WinForms.FormWindowState.Normal
               and r[1].X == norm.X and abs(r[1].Width - want_w) <= 14,
               "state=%s w=%d 期望w=%d x=%d 期望x=%d"
               % (f.WindowState, r[1].Width, want_w, r[1].X, norm.X))
    except Exception:
        _log_exc("e2e-float-resize")
        fails.append("异常")
    finally:
        try:
            _sf.set_cursor_probe(None)   # 恢复真鼠标: 别把测试缝留在运行中的窗口壳里
        except Exception:
            pass
        say("DONE pass=%d fail=%d" % (len([x for x in lines if x.startswith("[float-resize] PASS")]),
                                      len(fails)))
        try:
            api.tab_close(tid)
        except Exception:
            pass


def _e2e_panel_cdp(form, ctrl, method, params="{}", timeout=8.0):
    """在面板控件上跑一次 CDP 调用, 返回原始结果字符串(失败返回 "err:...")。

    WebView2 是 DirectComposition 合成的, GDI/DrawToBitmap 抓不到画面, 截图只能走 CDP。
    非 UI 线程等 Task: 只在 UI 线程"发起", 等待留在当前线程(否则死锁)。
    """
    holder = {}
    ev = threading.Event()

    def start():
        try:
            holder["t"] = ctrl.CoreWebView2.CallDevToolsProtocolMethodAsync(method, params)
        except Exception as e:
            holder["e"] = repr(e)
        finally:
            ev.set()

    try:
        import System.Windows.Forms as _WF
        form.BeginInvoke(_WF.MethodInvoker(start))
    except Exception as e:
        return "err:begininvoke %r" % (e,)
    if not ev.wait(5.0):
        return "err:start-timeout"
    if holder.get("e"):
        return "err:" + holder["e"]
    t = holder.get("t")
    deadline = time.time() + timeout
    while time.time() < deadline and not t.IsCompleted:
        time.sleep(0.03)
    if not t.IsCompleted:
        return "err:timeout"
    try:
        return str(t.Result)
    except Exception as e:
        return "err:task %r" % (e,)


def _e2e_panel_shot(form, ctrl, path):
    """给面板控件截一张真图(CDP Page.captureScreenshot)。"""
    import base64
    import json as _json
    raw_res = _e2e_panel_cdp(form, ctrl, "Page.captureScreenshot", '{"format":"png"}')
    if raw_res.startswith("err:"):
        return raw_res
    try:
        data = _json.loads(raw_res).get("data") or ""
        raw = base64.b64decode(data)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return "ok:%d" % len(raw)
    except Exception as e:
        return "decode:%r" % (e,)


def _e2e_split_perf(form, ctrl):
    """量化: 拖动左栏分栏 30 帧, 中间内容区到底被 resize 了几次。

    "拖一下卡一下"的机制是每帧改 --leftW → #shell 的 grid 列宽变 → #center 宽度变 →
    里面几百张卡片重新折行、封面图重绘、有 canvas/视频时还要按新尺寸重画。
    所以最贴切的指标不是"布局耗时", 而是**内容区尺寸变了几次**: 用 ResizeObserver 数,
    拖动全程应该是 0 次(只有松手那一次)。

    (WebView2 的 CDP 里 Performance.getMetrics 返回空数组, 拿不到 LayoutCount,
     所以走页面内 ResizeObserver —— 语义还更准。)

    只在 `--e2e-split` 下跑。
    """
    import json as _json
    lines, fails = [], [0]

    def say(m):
        lines.append(str(m))
        print("[e2e-split] " + str(m))

    def ok(name, cond, extra=""):
        if not cond:
            fails[0] += 1
        say("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                          ("  " + str(extra)) if extra else ""))

    def ev(expr, timeout=40.0):
        r = _e2e_panel_cdp(form, ctrl, "Runtime.evaluate",
                           _json.dumps({"expression": expr, "returnByValue": True,
                                        "awaitPromise": True}), timeout=timeout)
        try:
            return (_json.loads(r).get("result") or {}).get("value")
        except Exception:
            return None

    RAF = "await new Promise(function(k){requestAnimationFrame(k);});"
    FIND_SPLIT = ('(function(){var s=document.querySelector(".splitter[data-split=left]");'
                  'if(!s)return null;var r=s.getBoundingClientRect();'
                  'if(r.width<1||r.height<1)return null;'
                  'return {x:r.left+r.width/2,y:Math.round(innerHeight/2)};})()')

    try:
        ok("0 拿到主界面 WebView2 控件", ctrl is not None, ctrl)
        if ctrl is None:
            return fails[0]
        # 主界面刚 show 出来时页面可能还在加载, 轮询等它出现(冷启动可能要十几秒)
        box = None
        for _ in range(40):
            box = ev(FIND_SPLIT, timeout=10.0)
            if isinstance(box, dict) and box.get("x") is not None:
                break
            time.sleep(0.5)
        ok("1 找得到左栏分栏热区", isinstance(box, dict) and box.get("x") is not None, box)
        if not isinstance(box, dict):
            return fails[0]
        lw0 = (ev("getComputedStyle(document.documentElement)"
                  ".getPropertyValue('--leftW')") or "").strip()
        ok("2 起始宽度可读", lw0.endswith("px"), lw0)

        # ---- 被测: 真实拖动 30 帧(合成 PointerEvent, 每次 move 之间让出一帧) ----
        DRAG_JS = ('(async function(){'
                   'var c=document.getElementById("center");'
                   'var n=0, ro=new ResizeObserver(function(){n++;}); ro.observe(c);'
                   + RAF + RAF +    # observe() 必然先报一次初始尺寸; 它排在 rAF 之后,
                   'n=0;'           # 所以要等两帧再清零, 否则会把初始回调算进来
                   'var sp=document.querySelector(".splitter[data-split=left]");'
                   'var r=sp.getBoundingClientRect();'
                   'var x=r.left+r.width/2, y=Math.round(innerHeight/2);'
                   'function pe(t,cx){return new PointerEvent(t,{bubbles:true,cancelable:true,'
                   'clientX:cx,clientY:y,button:0,buttons:t==="pointerup"?0:1,pointerId:1,'
                   'pointerType:"mouse",isPrimary:true});}'
                   'var out={};'
                   'sp.dispatchEvent(pe("pointerdown",x));'
                   + RAF +
                   'for(var i=1;i<=30;i++){'
                   'document.dispatchEvent(pe("pointermove",x+i*3));'
                   + RAF +
                   'if(i===15){var g=document.getElementById("splitGhost");'
                   'out.midLw=getComputedStyle(document.documentElement)'
                   '.getPropertyValue("--leftW").trim();'
                   'out.on=!!(g&&g.classList.contains("on"));'
                   'out.gx=g?String(g.style.transform):"";}}'
                   'out.resizesDuring=n;'
                   'document.dispatchEvent(pe("pointerup",x+90));'
                   + RAF + RAF +
                   'out.resizesTotal=n; ro.disconnect();'
                   'out.lwAfter=getComputedStyle(document.documentElement)'
                   '.getPropertyValue("--leftW").trim();'
                   'return out;})()')
        drag = ev(DRAG_JS) or {}
        ok("3 拖动中预览线已显示", bool(drag.get("on")) and "translate3d" in str(drag.get("gx")),
           drag)
        ok("4 拖动中真实宽度一次都没变(--leftW 保持)", drag.get("midLw") == lw0,
           "%r vs %r" % (drag.get("midLw"), lw0))
        ok("5 松手后宽度才落地", str(drag.get("lwAfter") or "").strip() != lw0,
           "%r -> %r" % (lw0, drag.get("lwAfter")))
        ok("6 松手后预览线隐藏",
           ev("(function(){var g=document.getElementById('splitGhost');"
              "return !g||!g.classList.contains('on');})()", timeout=10.0) is True)
        n_new = int(drag.get("resizesDuring") or 0)
        n_tot = int(drag.get("resizesTotal") or 0)
        ok("7 拖动 30 帧: 内容区一次都没被 resize(卡片不重排/画布不重绘)",
           n_new == 0, "resize %d 次(拖动中)" % n_new)
        ok("8 整段(含松手)内容区只 resize 一次", n_tot <= 1, "resize %d 次(全程)" % n_tot)

        # ---- 对照组: 同一套测法, 但每帧真的改 --leftW(旧做法) ----
        # 没有这组, "0 次" 可能只是测法失效; 旧做法必须炸出几十次才算有判别力。
        OLD_JS = ('(async function(){'
                  'var c=document.getElementById("center");'
                  'var n=0, ro=new ResizeObserver(function(){n++;}); ro.observe(c);'
                  + RAF + RAF +    # 同样等两帧吃掉初始回调
                  'n=0;'
                  'var h=document.documentElement;'
                  'var w0=parseInt(getComputedStyle(h).getPropertyValue("--leftW"))||238;'
                  'for(var i=1;i<=30;i++){h.style.setProperty("--leftW",(w0+i*3)+"px");'
                  + RAF +
                  '}'
                  'h.style.setProperty("--leftW",w0+"px");'
                  + RAF + RAF +
                  'ro.disconnect(); return n;})()')
        n_old = int(ev(OLD_JS) or 0)
        ok("9 对照: 旧做法(每帧改宽度)确实反复 resize 内容区(证明测法有判别力)",
           n_old >= 20, "resize %d 次" % n_old)
        ok("10 新实现 vs 旧做法", n_new == 0 and n_old >= 20,
           "拖动 30 帧: 新 %d 次 / 旧 %d 次" % (n_new, n_old))
    except Exception as e:
        ok("异常", False, repr(e)[:200])
    finally:
        try:
            os.makedirs(_probe_dir(), exist_ok=True)
            with io.open(os.path.join(_probe_dir(), "split_perf_e2e.log"),
                         "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            print("[e2e-split] 日志落盘失败: %r" % (e,))
        say("RESULT: %s" % ("FAIL(%d)" % fails[0] if fails[0] else "PASS"))
    return fails[0]


def _probe_dir():
    """探针产物目录: 源码态用工程目录; 打包态用 exe 所在目录。

    frozen 下 `__file__` 指向 _MEIPASS(临时解压目录), 退出就被删 —— 日志和截图
    必须在跑完之后还能看, 所以按 sys.executable 落盘。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "build", "probe")
    return os.path.join(HERE, "build", "probe")


def _e2e_ai_panel(api, panel, pages, url):
    """真机自测: 开一个标签页 → 面板停靠/让位 → 网页快照/查找/填写/滚动 → 开合。

    只在 `--e2e-ai <url>` 下跑, 结果落 build/probe/ai_panel_e2e.log(打包态在 exe 同级)。
    为什么必须真机: WebView2 的 ExecuteScriptAsync 只有在真控件里才有意义(控件创建、
    Task 完成回调、UI 线程 marshalling 全是原生行为, 单测替不了)。
    """
    import json as _json
    lines = []
    fails = [0]

    def say(msg):
        lines.append(str(msg))
        print("[ai-e2e] %s" % msg, flush=True)

    def ok(name, cond, detail=""):
        if not cond:
            fails[0] += 1
        say(("PASS  " if cond else "FAIL  ") + name + (("  " + str(detail)) if detail else ""))
        return bool(cond)

    try:
        try:
            api.open_in_app(url, "面板自测", mode="tab")
        except Exception as e:
            say("open_in_app 失败: %r" % (e,))
        # 等页面真的可交互(最多 25 秒)
        snap = None
        for _i in range(50):
            time.sleep(0.5)
            r = panel.browser_call("snapshot", {"limit": 30, "text": 300})
            if isinstance(r, dict) and r.get("ok") and str(r.get("url") or "").find("panel") >= 0:
                snap = r
                break
        ok("1 标签页可被面板读到(快照有网址/标题)", bool(snap),
           (snap or {}).get("url", ""))
        if snap:
            els = snap.get("els") or []
            ok("2 快照返回可点元素(带连续序号)",
               len(els) >= 3 and all(e.get("i") == i for i, e in enumerate(els)),
               "%d 个" % len(els))
            tags = [str(e.get("tag")) for e in els]
            ok("3 快照里能看到输入框(textarea)", "textarea" in tags, str(tags[:12]))
            ta = next((e for e in els if str(e.get("tag")) == "textarea"), None)
            if ta is not None:
                r = panel.browser_call("type", {"index": ta["i"], "text": "面板自测文本",
                                                "submit": False})
                ok("4 往页面输入框填字(原生 setter + input 事件)", r.get("ok") is True,
                   r.get("msg") or r.get("error"))
                r2 = panel.browser_call("snapshot", {"limit": 30, "text": 100})
                got = ""
                for e in (r2.get("els") or []):
                    if str(e.get("tag")) == "textarea":
                        got = str(e.get("v") or "")
                        break
                ok("5 填进去的字被页面读到(说明事件真的生效)", got == "面板自测文本", got)
            r = panel.browser_call("find", {"text": "AI 助手"})
            ok("6 页内查找命中", r.get("ok") is True and int(r.get("hits") or 0) >= 1,
               str(r.get("hits")))
            r = panel.browser_call("scroll", {"bottom": True})
            ok("7 滚动到底", r.get("ok") is True, r.get("msg") or r.get("error"))
            r = panel.browser_call("click", {"index": 9999})
            ok("8 越界序号给的是错误而不是崩溃",
               bool(r.get("error")) or r.get("ok") is False, str(r)[:80])
            r = panel.browser_call("eval", {"code": "return document.title"})
            ok("9 eval 能拿到页面数据", r.get("ok") is True and "AI" in str(r.get("value")), str(r.get("value"))[:40])

        # ---- 几何: 面板停靠 + 网页区让位 ----
        time.sleep(1.0)
        geo = pages.geometry()
        ok("10 面板占住了内容区右侧(inset>0)", int(geo.get("inset") or 0) > 0, str(geo))
        try:
            import System.Windows.Forms as WinForms
            form = api._win.native
            W = int(form.ClientSize.Width)
            tid = (api.active_page() or {}).get("id")
            c = pages.ctrl(tid)
            want = W - int(geo.get("inset") or 0)
            ok("11 网页区宽度 = 窗口宽 - 面板让位", c is not None and abs(int(c.Size.Width) - want) <= 2,
               "ctrl=%s want=%s" % (getattr(c, "Size", None), want))
            pw = panel.st.get("ctrl")
            ok("12 面板控件可见且宽度合理", pw is not None and bool(pw.Visible)
               and int(pw.Size.Width) >= 300, getattr(pw, "Size", None))
            sp = panel.st.get("split")
            ok("13 分栏热区在面板左侧", sp is not None and bool(sp.Visible)
               and abs(int(sp.Location.X) + int(sp.Size.Width) - int(pw.Location.X)) <= 1,
               "split=%s panel=%s" % (getattr(sp, "Location", None), getattr(pw, "Location", None)))
            ok("14 面板上沿与网页区对齐(让开标签栏/导航栏)",
               abs(int(pw.Location.Y) - int(geo.get("top") or 0)) <= 1,
               "y=%s top=%s" % (int(pw.Location.Y), geo.get("top")))
            # 拖动: 只挪那条线, 网页区**不该动** —— 这是"拖面板不卡"的核心
            # (以前每帧 SetBounds WebView2, 每帧触发整页重排)
            panel.st["drag"] = {"x": 0, "w": 380}
            panel.st["ghost_w"] = 460
            panel._move_split_only(460)
            time.sleep(0.3)
            ok("15 拖动中只挪分栏线, 网页区尺寸不变(不触发 WebView2 重排)",
               abs(int(c.Size.Width) - (W - 380 - 6)) <= 2, int(c.Size.Width))
            ok("15b 拖动中分栏线已跟到目标位置",
               abs(int(sp.Location.X) - (W - 460 - 6)) <= 2, int(sp.Location.X))
            panel.st["ghost_w"] = 380
            panel._drag_up(None, None)
            time.sleep(0.6)
            geo2 = pages.geometry()
            ok("16 松手后才按新宽度重新对齐(inset 同步)", int(geo2.get("inset") or 0) == 380 + 6,
               str(geo2))
            # 面板本身的真实渲染(WebView2 里 380/300 两档, 供人眼检查窄面板版式)
            probe = _probe_dir()
            pctrl = panel.st.get("ctrl")
            panel.st["w"] = 380
            panel.layout()
            time.sleep(0.8)
            demo = (
                "(function(){var P=window.__msxPanel;if(!P)return 'no-hook';"
                "P.clear();"
                "P.add('me','帮我把这个视频的简介要点整理一下');"
                "var b=P.add('ai','');"
                "var a=document.createElement('div');a.className='acts';b.appendChild(a);"
                "P.act({acts:a},'读取网页: 【测试】视频标题');"
                "P.act({acts:a},'已点击「展开简介」');"
                "b.insertAdjacentHTML('beforeend',P.md("
                "'## 这页讲了什么\\n\\n这是**测试**视频，主要讲了三件事：\\n\\n"
                "1. 第一点\\n2. 第二点，带 `code` 片段\\n3. 第三点\\n\\n"
                "> 引用一句原文\\n\\n想让我点开第一个推荐视频吗？'));"
                "return 'ok';})()"
            )
            r = _e2e_panel_cdp(form, pctrl, "Runtime.evaluate",
                               _json.dumps({"expression": demo, "returnByValue": True}))
            ok("16b 面板示例对话注入", '"ok"' in r, r[:80])
            time.sleep(0.6)
            r = _e2e_panel_shot(form, pctrl, os.path.join(probe, "panel_w380.png"))
            ok("16c 面板 380px 真图落盘(深色/含对话)", str(r).startswith("ok:"), r)
            # 浅色主题
            _e2e_panel_cdp(form, pctrl, "Runtime.evaluate", _json.dumps(
                {"expression": "document.documentElement.setAttribute('data-theme','light')"}))
            time.sleep(0.5)
            r = _e2e_panel_shot(form, pctrl, os.path.join(probe, "panel_light.png"))
            ok("16d 面板浅色真图落盘", str(r).startswith("ok:"), r)
            _e2e_panel_cdp(form, pctrl, "Runtime.evaluate", _json.dumps(
                {"expression": "document.documentElement.setAttribute('data-theme','dark')"}))
            # 最窄档
            panel.st["w"] = 300
            panel.layout()
            time.sleep(0.8)
            r = _e2e_panel_shot(form, pctrl, os.path.join(probe, "panel_w300.png"))
            ok("16e 面板 300px 真图落盘", str(r).startswith("ok:"), r)
            panel.st["w"] = 380
            panel.layout()
            time.sleep(0.5)
        except Exception as e:
            ok("10-16 几何断言", False, repr(e)[:120])

        # ---- 开合: 收起后面板让位归零, 网页区恢复整宽 ----
        try:
            panel.set_open(False)
            time.sleep(1.0)
            geo3 = pages.geometry()
            ok("17 收起面板: 让位归零", int(geo3.get("inset") or 0) == 0, str(geo3))
            pw = panel.st.get("ctrl")
            ok("18 收起面板: 面板控件不可见", pw is None or not bool(pw.Visible))
            panel.set_open(True)
            time.sleep(1.0)
            geo4 = pages.geometry()
            ok("19 再展开: 让位恢复", int(geo4.get("inset") or 0) > 0, str(geo4))
        except Exception as e:
            ok("17-19 开合断言", False, repr(e)[:120])

        # ---- 面板页面本身(它自己也是网页: 能读到自己的输入框) ----
        try:
            st = panel.state()
            ok("20 面板上报状态带当前网页网址", "panel" in str(st.get("url") or "")
               or str(st.get("url") or "").startswith("http"), str(st.get("url"))[:60])
        except Exception as e:
            ok("20 面板状态", False, repr(e)[:120])
    finally:
        try:
            # 目录和文件必须用同一个根: frozen 下 HERE 是 _MEIPASS(临时), 建在那儿
            # 再往 exe 同级写就会因目录不存在而静默失败 —— 打包态又没有 stdout,
            # 日志一丢就彻底没有证据了。
            os.makedirs(_probe_dir(), exist_ok=True)
            with io.open(os.path.join(_probe_dir(), "ai_panel_e2e.log"),
                         "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            # 别静默吞: 之前这里 except: pass 把 "io 没 import" 的 NameError 藏了,
            # 结果日志从来没写成功过, 却以为写好了。
            print("[ai-e2e] 日志落盘失败: %r" % (e,))
        say("RESULT: %s" % ("FAIL(%d)" % fails[0] if fails[0] else "PASS"))
        # RESULT 行在写文件之后才产生, 补写一次, 否则日志里看不到最终结论
        try:
            with io.open(os.path.join(_probe_dir(), "ai_panel_e2e.log"),
                         "a", encoding="utf-8") as f:
                f.write("RESULT: %s\n" % ("FAIL(%d)" % fails[0] if fails[0] else "PASS"))
        except Exception:
            pass
    return fails[0]


def _e2e_float_run(api, url):
    """v0.3.1.34 自测: 新窗口 → 塞回标签页 → 拖出悬浮窗 → 关闭, 每步 dump 控件归属
    (父窗体标题/可见性 + 悬浮窗存在性), 验证控件实例跨窗口迁移、页面不重载。

    打包后的 exe 是 --noconsole(无 stdout), 所以除了 print 还落一份到
    <数据目录>/float_e2e.log —— 打包产物也能被同一套断言验证。
    """
    log_path = os.path.join(app.DATA_DIR, "float_e2e.log")
    _DIAG["trace_open"] = True   # 打开 open_in_app 决策落盘

    def say(msg):
        try:
            print(msg, flush=True)
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def dump(tag):
        with api._lock:
            tabs = [{"id": t["id"], "float": bool(t.get("float")),
                     "opened": t["opened"]} for t in api._tabs]
        ctrls = getattr(api._pages, "ctrls", None) or {}
        info = []
        for k, c in list(ctrls.items()):
            if c is None:
                continue
            try:
                par = c.Parent
                info.append("%s->%s(vis=%s)" % (
                    k, (par.Text if par is not None else "None"), c.Visible))
            except Exception as e:
                info.append("%s->err:%r" % (k, e))
        mgr = api._floats
        fex = [t["id"] for t in tabs if t["float"] and mgr is not None
               and mgr.exists(t["id"])]
        say("[float-e2e] %s tabs=%s ctrls=%s floats=%s" % (tag, tabs, info, fex))

    time.sleep(8)   # 等主窗口与服务就绪
    say("[float-e2e] init floats=%s new_window=%s tabs=%s" % (
        api._floats is not None, api._new_window_enabled(),
        [(t["id"], t.get("url")) for t in api._tabs]))
    _su = getattr(api, "_strip_update", None)
    say("[float-e2e] strip: bar_h=%s highlight=%s" % (
        getattr(_su, "bar_h", None), callable(getattr(_su, "highlight", None))))
    say("[float-e2e] open -> %s" % api.open_in_app(url, "E2E 悬浮窗", mode="window"))
    time.sleep(6)
    dump("after-open")
    # 优先挑**我们自己开的**那个悬浮窗(信息流可能自动把别的链接也弹成了悬浮窗)
    fid = next((t["id"] for t in api._tabs if t.get("float") and t.get("url") == url), None)
    if fid is None:
        fid = next((t["id"] for t in api._tabs if t.get("float")), None)
    if fid is None:
        say("[float-e2e] no float tab")
        return

    # ---- 拖到标签栏松手即合并(真实鼠标手势: 光标按下在迷你标题栏 → 移到标签栏 → 松手) ----
    merged = _e2e_drag_to_tabbar(api, fid, say)
    time.sleep(4)
    dump("after-drag-merge")

    def _is_float(tab_id):
        """按 id 查标签(标签 id 不连续, 不能当列表下标用)。"""
        with api._lock:
            t = next((x for x in api._tabs if x["id"] == tab_id), None)
        return None if t is None else bool(t.get("float"))

    still_float = _is_float(fid)
    say("[float-e2e] drag-merge -> merged=%s (still_float=%s)" % (merged, still_float))
    if still_float:
        # 输入注入在某些会话/无头环境不可用 → 退回按钮路径, 保证后续步骤仍被验证
        say("[float-e2e] drag-merge 未生效, 回退 window_dock: %s" % api.window_dock(fid))
        time.sleep(4)
        dump("after-dock-fallback")
    say("[float-e2e] detach -> %s" % api.tab_detach(fid))
    time.sleep(4)
    dump("after-detach")
    say("[float-e2e] close -> %s" % api.tab_close(fid))
    time.sleep(3)
    dump("after-close")
    say("[float-e2e] DONE")


def _e2e_drag_to_tabbar(api, tid, say):
    """模拟"拽着悬浮窗标题栏拖到主窗口标签栏松手"的完整鼠标手势。

    做法: 把**真实光标**移到主窗口标签栏区域, 再用 SendMessage 往悬浮窗迷你标题栏投递
    LEFTDOWN/MOUSEMOVE/LBUTTONUP —— shell_float 的拖动与落点判定读的是
    `WinForms.Cursor.Position`(而不是消息坐标), 所以这样投递等价于真人拖拽。
    返回 True 表示松手后确实合并成了标签页。
    """
    try:
        import ctypes
        import System.Windows.Forms as WinForms
        from System.Drawing import Point as SPoint
        u32 = ctypes.windll.user32
        mgr = api._floats
        c = api._pages.control(tid)
        if mgr is None or c is None or c.Parent is None:
            return False
        form = c.Parent
        bar = None
        for i in range(form.Controls.Count):   # 迷你标题栏 = 第一个 Dock=Top 的 Panel
            ctl = form.Controls[i]
            try:
                if ctl.Dock == WinForms.DockStyle.Top:
                    bar = ctl
                    break
            except Exception:
                pass
        if bar is None:
            return False
        main = api._win.native
        tabstrip_h = int(getattr(getattr(api, "_strip_update", None), "bar_h", 0) or 0)
        if tabstrip_h <= 0:
            say("[float-e2e] drag: tabstrip_h 未知, 放弃鼠标手势")
            return False
        # 1) 光标落点 = 主窗口标签栏中部(落点判定看的就是它)
        dst = main.PointToScreen(SPoint(int(main.ClientSize.Width * 0.5),
                                        int(tabstrip_h * 0.5)))
        # 2) 按下点 = 悬浮窗迷你标题栏左侧空白(避开右侧两枚按钮)
        src = form.PointToScreen(SPoint(int(60 * float(getattr(main, "_scale", 1.0) or 1.0)),
                                        max(6, tabstrip_h // 2)))
        # 3) 顺带用探针记录落点高亮(update.highlight)被调用的时序, 验证提示真的亮过
        su = getattr(api, "_strip_update", None)
        seen, orig = [], getattr(su, "highlight", None)
        if orig is not None:
            def spy(on):
                seen.append(bool(on))
                return orig(on)
            su.highlight = spy
        lp = (max(6, tabstrip_h // 2) << 16) | 60
        merged, diag = False, None
        for attempt in range(3):
            # 光标可能被真人鼠标抢走(无人值守测试跑在用户桌面上), 所以:
            # 每轮都重新按下 → **立刻**把光标钉到落点 → 紧接着投递 MOVE/UP, 中间不 sleep。
            # 三个调用都是同步 SendMessage, 中间没有窗口让光标被改掉。
            hwnd = bar.Handle.ToInt32()
            u32.SetCursorPos(int(src.X), int(src.Y))
            time.sleep(0.35)
            u32.SendMessageW(hwnd, 0x0201, 1, lp)      # WM_LBUTTONDOWN → 开始拖动
            u32.SetCursorPos(int(dst.X), int(dst.Y))   # 光标进入标签栏
            cp = WinForms.Cursor.Position
            back = main.PointToClient(cp)
            diag = "dst=%s cursor=%s back=%s client=%s state=%s tabstrip_h=%s" % (
                (dst.X, dst.Y), (cp.X, cp.Y), back, main.ClientSize,
                main.WindowState, tabstrip_h)
            u32.SendMessageW(hwnd, 0x0200, 1, lp)      # WM_MOUSEMOVE → 高亮提示
            u32.SendMessageW(hwnd, 0x0202, 0, lp)      # WM_LBUTTONUP → 落点判定
            time.sleep(0.5)
            with api._lock:
                t = next((x for x in api._tabs if x["id"] == tid), None)
            merged = t is not None and not bool(t.get("float"))
            if merged:
                break
        say("[float-e2e] drag: %s" % diag)
        say("[float-e2e] drag: highlight 调用序列=%s" % (seen,))
        return merged
    except Exception:
        _log_exc("e2e-drag-merge")
        return False


def shell_main(e2e_url=None, e2e_float_url=None, e2e_float_resize_url=None,
               e2e_ai_url=None, e2e_split=False):
    """窗口壳主流程: 起/复用服务 → WebView2 检测 → 主窗口(含标签页) → 全关后收尾。

    e2e_url 仅测试用(_e2e_shell.py): 主窗口就绪后自动走一次 open_in_app 桥调用,
    与用户点击卡片进入内置浏览器标签页的代码路径完全一致, 不碰真实鼠标。
    """
    server, port = _start_server_thread()
    if port is None:
        _msg_box("端口 8000/8010/8020 均被占用，无法启动。")
        return 1
    url = "http://127.0.0.1:%d" % port
    py_ok = have_pywebview()
    if py_ok and not webview2_available():
        # 缺运行时: 优先借用已装的 Edge 浏览器内核(免安装, 零系统改动);
        # 真没有 Edge 才跑随包的官方引导安装包(一次授权, 约1-2分钟)
        edge_dir = find_edge_browser_dir()
        if edge_dir:
            os.environ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"] = edge_dir
            print("WebView2 运行时缺失 → 借用 Edge 浏览器内核: %s" % edge_dir)
        else:
            install_webview2_runtime()
    if not py_ok or not webview2_available():
        # 降级: 保留服务的旧行为(外部浏览器), 绝不白屏
        print("未检测到内置浏览器组件(WebView2 运行时), 回退外部浏览器模式: %s" % url)
        _msg_box("未检测到 WebView2 运行时，本次将以外部浏览器模式打开。\n\n"
                 "如需应用窗口与内置浏览器：安装 Microsoft Edge 浏览器后重开本应用\n"
                 "（应用会自动借用其内核，无需安装 WebView2 运行时）；\n"
                 "或到微软官网下载 WebView2 Runtime Evergreen 安装包。")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            app.shutdown(server)
        return 0
    import webview
    # 站点内 target=_blank/弹新页 → 当前标签页内导航, 绝不放去系统浏览器、绝不弹新窗口
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    api = ShellApi()
    win = webview.create_window("%s %s" % (APP_NAME, APP_VER), url,
                                width=MAIN_W, height=MAIN_H, min_size=MAIN_MIN,
                                background_color=BG_PAGE, js_api=api,
                                frameless=True, easy_drag=False)   # easy_drag=False: 只允许标签栏拖动, 网页区域拖拽不再移动窗口
    api.attach(win, url)

    def gui_ready():
        # GUI 就绪(在 pywebview 的后台线程): 装控件池与标签栏, 再跑测试钩子
        pages = None
        try:
            from shell_pages import create_pool
            # 站点页面深浅跟随主界面: 读正式设置键(applyTheme 每次切主题实时落库),
            # 旧存档无 ui_theme 时回退悬浮球镜像键
            def _current_theme():
                v = app.sget("ui_theme")
                if v not in ("light", "dark"):
                    v = "light" if app.sget("float_ball_light") == "1" else "dark"
                return v
            # bug6: 网页(视频)全屏 → 隐藏标签栏与导航栏, 窗口内只剩画面(整窗纯播放);
            # 退出全屏再显回来。strip/nav 实例的装配晚于 create_pool, 这里用字典迟绑定。
            chrome = {"strip": None, "nav": None, "bm": None, "ai": None}

            def on_fs(_key, on):
                hidden = bool(on)
                st = chrome["strip"]
                if st is not None:
                    try:
                        st.set_fullscreen(hidden)
                    except Exception:
                        _log_exc("fullscreen/strip")
                nb = chrome["nav"]
                if nb is not None:
                    try:
                        # 优先走导航栏自己的全屏接口(内部 marshal 到 UI 线程 + 退出时 BringToFront)
                        nbfs = getattr(nb, "set_fullscreen", None)
                        if nbfs is not None:
                            nbfs(hidden)
                        else:
                            nb.Visible = not hidden
                    except Exception:
                        _log_exc("fullscreen/navbar")
                ai = chrome.get("ai")
                if ai is not None:
                    try:
                        ai.set_fullscreen(hidden)
                    except Exception:
                        _log_exc("fullscreen/ai")
                bm = chrome.get("bm")
                if bm is not None:
                    try:
                        bmfs = getattr(bm, "set_fullscreen", None)
                        if bmfs is not None:
                            bmfs(hidden)
                        else:
                            bm.Visible = not hidden
                    except Exception:
                        _log_exc("fullscreen/bookmarks")

            # 站点自身弹新页(target=_blank 等)一律进主窗口标签, 不弹悬浮窗(旧行为)
            pages = create_pool(
                win, api.update_title, lambda _k, u: api.open_in_app(u, mode="tab"),
                theme_getter=_current_theme, port=port, on_nav=api._on_nav, on_fs=on_fs,
                on_drop=api.on_drop)
            pages.init()          # 等窗体就绪 + WS_THICKFRAME 无边框缩放补丁
            api.bind_pages(pages)
        except Exception:
            _log_exc("shell_pages/init")
        try:
            from shell_tabstrip import install_strip
            strip_update = install_strip(
                win, api, accent_getter=lambda: app.sget("float_ball_accent"),
                bg_getter=lambda: app.sget("ui_bg_scheme"))
            api.bind_strip(strip_update)
            chrome["strip"] = strip_update
        except Exception:
            _log_exc("shell_tabstrip/install")
            strip_update = None
        try:
            from shell_float import install_floats
            # 悬浮窗管理器: 承载「新窗口形式打开」的页面。
            # 必须在 install_strip 之后 —— 需要标签栏高度做「拖到标签栏松手即合并」的落点判定。
            if pages is not None:
                api.bind_floats(install_floats(
                    win, api, pages, bg_getter=lambda: app.sget("ui_bg_scheme"),
                    accent_getter=lambda: app.sget("float_ball_accent"),
                    tabstrip_h=int(getattr(strip_update, "bar_h", 0) or 0)))
        except Exception:
            _log_exc("shell_float/install")
        bm_store = None
        try:
            from shell_bookmarks import BookmarkStore
            bm_store = BookmarkStore()
        except Exception:
            _log_exc("shell_bookmarks/store")
        try:
            from shell_navbar import install_navbar
            # top=标签栏高度(install_strip 暴露): 工具栏紧贴标签栏下沿
            nav_update = install_navbar(
                win, api, top=getattr(strip_update, "bar_h", 32) if strip_update else 32,
                bg_getter=lambda: app.sget("ui_bg_scheme"),
                bookmarks=bm_store,
                title_getter=lambda: (api.active_page() or {}).get("title") or "")
            api.bind_navbar(nav_update)
            chrome["nav"] = getattr(nav_update, "bar", None)
        except Exception:
            _log_exc("shell_navbar/install")
        try:
            from shell_bookmarks import install_bookmarks
            if bm_store is not None:
                from shell_navbar import nav_h as _nav_h
                _scale = float(getattr(win.native, "_scale", 1.0) or 1.0)
                _nav_top = int(getattr(strip_update, "bar_h", 32) if strip_update else 32)
                _bm_top = _nav_top + _nav_h(_scale)

                def _bm_size(hh, _top=_bm_top):
                    if pages is not None:
                        pages.set_content_top(_top + int(hh))

                bm_update = install_bookmarks(
                    win, api, top=_bm_top, store=bm_store, on_height=_bm_size,
                    bg_getter=lambda: app.sget("ui_bg_scheme"))
                chrome["bm"] = bm_update
        except Exception:
            _log_exc("shell_bookmarks/install")
        # 内置浏览器右侧 AI 面板(v0.4.42)。装在收藏栏之后 —— 它要按内容区高度定位,
        # 而内容区高度由「标签栏+导航栏+收藏栏」三段算出来。
        try:
            from shell_ai import install_ai_panel
            if pages is not None:
                _ai = install_ai_panel(win, api, pages, port)
                if _ai is not None:
                    chrome["ai"] = _ai
                    api.bind_ai_panel(_ai)
        except Exception:
            _log_exc("shell_ai/install")
        try:
            _install_mouse_side_buttons(api, win)
        except Exception:
            _log_exc("mouse-side-buttons")
        # 设置「启动时打开浏览器」: 等标签栏/导航栏装好再开, 新标签立刻有 chrome;
        # 稍等一拍是让首页控件完成初始化(新标签要复制它的 WebView2 配置)。
        _stb = threading.Timer(0.8, api.open_startup_browser)
        _stb.daemon = True
        _stb.start()
        if e2e_url:
            time.sleep(8)   # 等主窗口与服务就绪
            api.open_in_app(e2e_url, "E2E 视频标签页", mode="tab")

            def dump_controls():
                try:
                    import System.Windows.Forms as WinForms
                    f = win.native
                    print("[dump] client=%s" % f.ClientSize, flush=True)
                    for i in range(f.Controls.Count):
                        c = f.Controls[i]
                        try:
                            print("[dump] z=%d %s vis=%s loc=%s size=%s dock=%s" % (
                                i, c.GetType().Name, c.Visible, c.Location, c.Size,
                                c.Dock), flush=True)
                        except Exception as _e:
                            print("[dump] z=%d err %r" % (i, _e), flush=True)
                except Exception:
                    import traceback
                    traceback.print_exc()
            time.sleep(6)
            import threading as _th
            _th.Thread(target=dump_controls, daemon=True).start()
            print("[float-e2e] DONE", flush=True)
        if e2e_float_resize_url:
            # v0.3.1.51 测试钩子: 悬浮窗八向缩放(真鼠标消息), 结果落 float_resize_e2e.log
            try:
                _e2e_float_resize_run(
                    api, url if e2e_float_resize_url == "local" else e2e_float_resize_url)
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                try:
                    time.sleep(1)
                    win.destroy()
                except Exception:
                    pass
                try:
                    app.shutdown(server)
                except Exception:
                    pass
                os._exit(0)
        if e2e_split:
            # 量化自测: 拖动分栏 30 帧 → 读 Performance 指标(布局/样式重算次数)
            try:
                _e2e_split_perf(api._win.native, pages.home_ctrl())
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                try:
                    time.sleep(0.6)
                    win.destroy()
                except Exception:
                    pass
                try:
                    app.shutdown(server)
                except Exception:
                    pass
                os._exit(0)
        if e2e_ai_url:
            # 面板自测: 不能最小化 —— 最小化后 ClientSize 变成 0, 几何类断言全量不出来。
            # 跑完自动关窗退出(约 15 秒)。
            try:
                _e2e_ai_panel(api, chrome.get("ai"), pages, e2e_ai_url)
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                try:
                    time.sleep(1)
                    win.destroy()
                except Exception:
                    pass
                try:
                    app.shutdown(server)
                except Exception:
                    pass
                os._exit(0)
        if e2e_float_url:
            # v0.3.1.34 测试钩子: 走一遍 新窗口 → 塞回标签页 → 拖出悬浮窗 → 关闭 全链路,
            # 每步 dump 控件归属(父窗体/可见性), 验证控件实例跨窗口迁移、页面不重载。
            try:
                _e2e_float_run(api, e2e_float_url)
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                # 自测是无人值守钩子: 关窗失败也强制退出, 绝不留悬挂进程/窗口
                try:
                    time.sleep(1)
                    win.destroy()
                except Exception:
                    pass
                try:
                    app.shutdown(server)
                except Exception:
                    pass
                os._exit(0)

    # 非无痕: 内置浏览器标签页里的登录态(B站等)跨重启保留, 数据落用户目录
    # MIRRORSORTER_WV2_DATA: 把 WebView2 的用户数据目录也隔离开(默认共用一份)。
    # 共用时第二个实例的 WebView2 初始化会失败(0x8007139F, 同一目录被占用),
    # 于是第二个窗口是一片空白/无响应 —— 自测脚本靠它并行跑而不干扰正在用的窗口。
    webview.start(func=gui_ready, private_mode=False,
                  storage_path=os.environ.get("MIRRORSORTER_WV2_DATA") or None,
                  debug=os.environ.get("MSX_DEBUG") == "1")
    # start() 返回 = 主窗口已关闭 → 先收掉所有悬浮窗, 再收尾退出(收球/停线程池/关服务)
    try:
        if api._floats is not None:
            api._floats.close_all()
    except Exception:
        pass
    app.shutdown(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
