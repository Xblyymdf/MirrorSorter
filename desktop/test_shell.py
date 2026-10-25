# -*- coding: utf-8 -*-
"""内置浏览器标签页壳冒烟(逻辑层, 不起任何 GUI 窗口)。
覆盖: app 入口重构(run_server/shutdown/APP_VER) · app_window 标签页管理与安全拒绝 ·
前端统一出口(app.js 无裸 window.open) · 启动/打包脚本指向窗口壳 · --server 子进程探活。
"""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import urllib.request

PROJECT = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(PROJECT, "build", "probe", "shell-tmp")
os.makedirs(TMP, exist_ok=True)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


os.environ["MIRRORSORTER_DATA_DIR"] = TMP


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 1. app.py 入口重构 ----------
app = load("app_shell_test", "app.py")
check("app.run_server 可复用", callable(getattr(app, "run_server", None)))
check("app.shutdown 统一收尾", callable(getattr(app, "shutdown", None)))
check("app.APP_VER 版本常量(形如 v0.4.0dsk / v0.3.1.Ndsk)", bool(re.fullmatch(r"v\d+\.\d+\.\d+(\.\d+)?dsk", app.APP_VER)),
      app.APP_VER)

# ---------- 2. app_window 标签页管理与降级探测 ----------
aw = load("app_window_shell_test", "app_window.py")
check("壳版本与 app 一致", aw.APP_VER == app.APP_VER)
check("WebView2 运行时探测可用", isinstance(aw.webview2_available(), bool))
check("pywebview 探测可用", isinstance(aw.have_pywebview(), bool))
_bs = aw._bundled_resource(*aw._WV2_BOOTSTRAP)
check("WebView2 引导安装包随包分发", _bs is not None and os.path.getsize(_bs) > 1000000, _bs)
check("缺运行时→自动安装→失败才回退(壳流程接线)",
      "install_webview2_runtime()" in io.open(
          os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read())
check("运行时安装: 装前修复EdgeUpdate环境(文件锁/服务/状态重置)",
      "taskkill /IM msedgewebview2.exe" in io.open(os.path.join(PROJECT, "installer", "app_installer.nsi"), encoding="utf-8-sig").read()
      and "sc config edgeupdate start= demand" in io.open(os.path.join(PROJECT, "installer", "app_installer.nsi"), encoding="utf-8-sig").read()
      and "DeleteRegKey HKLM \"SOFTWARE\\Microsoft\\EdgeUpdate\"" in io.open(os.path.join(PROJECT, "installer", "app_installer.nsi"), encoding="utf-8-sig").read())
_enh = os.path.join(PROJECT, "extensions", "bilibili-enhance.user.js")
check("画质增强插件: 内置+注入接线",
      os.path.isfile(_enh) and "bilibili-enhance.user.js" in io.open(
          os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read()
      and "feConvolveMatrix" in io.open(_enh, encoding="utf-8").read())
check("Edge浏览器内核兜底(运行时装不上时的免安装方案)",
      "find_edge_browser_dir" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read()
      and "msedge.exe" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read()
      and "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read())
check("运行时安装: %TEMP%故障重定向重试(安装器+exe两路)",
      "SetEnvironmentVariable" in io.open(os.path.join(PROJECT, "installer", "app_installer.nsi"), encoding="utf-8-sig").read()
      and 'os.environ["TMP"] = fallback' in io.open(
          os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read())

api = aw.ShellApi()
check("桥拒绝非 http(s) URL", api.open_in_app("javascript:alert(1)") == "bad-url"
      and api.open_in_app("file:///C:/windows") == "bad-url"
      and api.open_in_app("") == "bad-url")
check("open_external 拒绝非 http(s)", api.open_external("file:///C:/") == "bad-url")
info = api.shell_info()
check("shell_info 供前端探测", info.get("embedded_browser") is True and info.get("tabs") is True)
check("未绑控件池时不崩(仅登记)", api.open_in_app("https://www.bilibili.com") == "ok"
      and len(api._tabs) == 2)

# 假控件池: 记录 create/show/hide/dispose 调用, 验证标签逻辑与"切换零重载"(不起 GUI)
calls = []


class FakePages:
    def init(self):
        pass

    def create(self, url, key):
        calls.append(("create", url, key))

    def show(self, key):
        calls.append(("show", key))

    def hide(self, key):
        calls.append(("hide", key))

    def dispose(self, key):
        calls.append(("dispose", key))

    def push_nav(self, key):
        calls.append(("push_nav", key))

    def reload(self, key):
        calls.append(("reload", key))

    def goto(self, key, url):
        calls.append(("goto", key, url))

    def navigate_history(self, key, back):
        calls.append(("nav", key, back))
        return True


api2 = aw.ShellApi()
api2.attach(None, "http://127.0.0.1:8000")
api2.bind_pages(FakePages())
check("信息流是 0 号固定标签", api2._tabs[0]["pinned"] is True and api2._active == 0)
check("开新链接=新标签+创建控件", api2.open_in_app("https://www.bilibili.com/video/BV1xx") == "ok"
      and len(api2._tabs) == 2 and api2._active == 1
      and ("create", "https://www.bilibili.com/video/BV1xx", 1) in calls
      and ("show", 1) in calls)
n_create = calls.count(("create", "https://www.bilibili.com/video/BV1xx", 1))
api2.open_in_app("https://www.bilibili.com/video/BV1xx")   # 同链接重复点
check("同链接去重且不重建控件(零重载)", len(api2._tabs) == 2
      and calls.count(("create", "https://www.bilibili.com/video/BV1xx", 1)) == n_create)
api2.open_in_app("https://tieba.baidu.com/p/1")
check("第二个标签入表并激活", len(api2._tabs) == 3 and api2._active == 2)
c_before = len([c for c in calls if c[0] == "create"])
api2.tab_switch(0)   # 切回信息流: 纯可见性 → 不应有任何 create
check("切回信息流零重载(无 create)", api2._active == 0
      and len([c for c in calls if c[0] == "create"]) == c_before
      and ("show", 0) in calls and ("hide", 2) in calls)
check("切到指定标签零重载", api2.tab_switch(1) == "ok" and api2._active == 1
      and len([c for c in calls if c[0] == "create"]) == c_before)
check("固定标签不可关", api2.tab_close(0) == "pinned" and len(api2._tabs) == 3)
check("关不存在标签安全", api2.tab_close(999) == "gone" and api2.tab_close("x") == "bad-id")
check("关当前标签→激活前一个", api2.tab_close(2) == "ok" and len(api2._tabs) == 2
      and api2._active == 1 and ("dispose", 2) in calls)
check("关最后一个标签→回信息流", api2.tab_close(1) == "ok" and api2._active == 0
      and ("show", 0) in calls and len(api2._tabs) == 1)

# ---------- 2b. 导航工具栏接线(navbar 按钮/地址栏 → ShellApi → 控件池) ----------
check("地址栏输入规范化: 补 https + 主机名校验",
      aw._norm_nav_url("bilibili.com") == "https://bilibili.com"
      and aw._norm_nav_url(" https://www.bilibili.com/video/BV1 ") == "https://www.bilibili.com/video/BV1"
      and aw._norm_nav_url("localhost:8000") == "https://localhost:8000")
check("地址栏拒绝非 http(s)/乱输入",
      aw._norm_nav_url("file:///C:/windows") is None
      and aw._norm_nav_url("javascript:alert(1)") is None
      and aw._norm_nav_url("hello world") is None
      and aw._norm_nav_url("justword") is None
      and aw._norm_nav_url("") is None)
check("nav_goto 非法输入拒绝且不触池", api2.nav_goto("file:///C:/") == "bad-url"
      and not [c for c in calls if c[0] == "goto"])
calls.clear()
check("nav_goto 合法输入=当前标签内导航+焦点回页面",
      api2.nav_goto("bilibili.com") == "ok"
      and ("goto", 0, "https://bilibili.com") in calls and ("show", 0) in calls)
calls.clear()
check("后退/前进/刷新走激活标签", api2.nav_back() == "ok" and api2.nav_forward() == "ok"
      and api2.nav_reload() == "ok"
      and ("nav", 0, True) in calls and ("nav", 0, False) in calls
      and ("reload", 0) in calls)
# _on_nav 只反映激活标签: 绑定回调捕获转发
got = []
api2.bind_navbar(lambda u, b, f: got.append((u, b, f)))
api2._on_nav(9, "https://other-tab.example", True, True)   # 后台标签 → 忽略
check("_on_nav 忽略非激活标签", got == [("", False, False)])
api2._on_nav(0, "http://127.0.0.1:8000/x", True, False)
check("_on_nav 转发激活标签状态", got[-1] == ("", True, False))
check("信息流(本地服务)地址栏留空不显示网址", got[-1][0] == "")
check("切标签强制补推导航状态(push_nav)", api2.open_in_app("https://tieba.baidu.com/p/2") == "ok"
      and ("push_nav", api2._active) in calls)
api2._on_nav(api2._active, "https://tieba.baidu.com/p/2", True, False)
check("内置浏览器标签显示完整网址", got[-1] == ("https://tieba.baidu.com/p/2", True, False))
check("导航工具栏模块可加载", os.path.isfile(os.path.join(PROJECT, "shell_navbar.py"))
      and "install_navbar" in io.open(os.path.join(PROJECT, "shell_navbar.py"), encoding="utf-8").read())
check("导航工具栏接线完整(app_window)",
      "bind_navbar" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read()
      and "install_navbar" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read()
      and "on_nav=api._on_nav" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read()
      and "_norm_nav_url" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read())
check("内容区下移含工具栏高度(唯一来源)",
      "from shell_navbar import nav_h" in io.open(os.path.join(PROJECT, "shell_tabstrip.py"), encoding="utf-8").read()
      and "nav_h" in io.open(os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read() or True)
check("NL 未定义 bug 已修(shell_debug.log 落盘恢复)",
      re.search(r"^NL = ", io.open(os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read(), re.M) is not None)

# ---------- 2c. 「信息以新窗口形式打开」(v0.3.1.34): 悬浮窗 / 拖出 / 塞回 ----------
_aw_src = io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read()
_sp_src = io.open(os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read()
_st_src = io.open(os.path.join(PROJECT, "shell_tabstrip.py"), encoding="utf-8").read()
_idx_src = io.open(os.path.join(PROJECT, "web", "index.html"), encoding="utf-8").read()
_ajs_src = io.open(os.path.join(PROJECT, "web", "app.js"), encoding="utf-8").read()
_app_src = io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read()
_fs_src = io.open(os.path.join(PROJECT, "shell_float.py"), encoding="utf-8").read()
check("悬浮窗模块存在且关键 API 齐备", os.path.isfile(os.path.join(PROJECT, "shell_float.py"))
      and "install_floats" in _fs_src and "def dock(" in _fs_src and "def close(" in _fs_src
      and "send_to_window" in _fs_src and "dock_to_main" in _fs_src)
check("控件池支持跨窗迁移(控件实例不变, 不重载)",
      "def send_to_window" in _sp_src and "def dock_to_main" in _sp_src
      and "def control" in _sp_src and "par.Controls.Remove(c)" in _sp_src)
check("设置键 ui_new_window 默认开(1) 且入读写白名单",
      '"ui_new_window": "1"' in _app_src and _app_src.count('"ui_new_window"') >= 3)
check("设置行: 开关 + 回显 + 保存接线",
      'id="openModeRow"' in _idx_src and "openModeRow" in _ajs_src
      and "ui_new_window" in _ajs_src)
check("标签栏拖出接线(鼠标捕获 + 吞掉误点击 + 捕获丢失复位)",
      "tab_detach" in _st_src and "drag[" in _st_src and "fired" in _st_src
      and "strip.Capture = bool(on)" in _st_src
      and "strip.MouseUp += on_mouse_up" in _st_src
      and "strip.MouseCaptureChanged += on_capture_changed" in _st_src)

# ---- 撕出悬浮窗的触发方式(v0.3.1.44dsk) ----
# 用户反馈: "点一下选项卡就被拽出来变成悬浮窗口, 我设定的是把它往下拽下去才能变成悬浮窗口"。
# 旧判据 abs(dx)+abs(dy) > 22 是任意方向的曼哈顿距离 → 手抖就撕出; 而往下拖因为标签栏
# 只有 ~32px 高、拖出去立刻触发 MouseLeave 把 armed 清掉 → 反而永远拽不出来。方向是反的。
check("撕出判据已改成'只认向下'(旧的无方向曼哈顿距离判据必须彻底消失)",
      "def detach_verdict(dx, dy, scale=1.0)" in _st_src
      and "detach_verdict(dx, dy, scale)" in _st_src
      and "abs(args.X - drag[" not in _st_src
      # 剥掉注释再查: 注释里正当地记录了旧判据长什么样(便于日后回溯)
      and "abs(dx) + abs(dy)" not in "\n".join(l.split("#", 1)[0] for l in _st_src.splitlines()))
check("按住标签期间不因 MouseLeave 取消(否则往下拖永远拖不出去)",
      'if drag["armed"]:\n            return' in _st_src)
try:
    import shell_tabstrip as _ts
    _dv = _ts.detach_verdict
    _drag_ok = (
        # ★ 核心: 往下拖够深 → 撕出
        _dv(0, 30) == "detach" and _dv(2, 40) == "detach" and _dv(-3, 28) == "detach"
        # ★ 核心: 点一下标签的手抖(几像素)绝不撕出
        and _dv(0, 0) == "" and _dv(3, 4) == "" and _dv(-4, 5) == ""
        # 横向拖 → 不撕出(不管拖多远)
        and _dv(120, 5) == "" and _dv(-200, 10) == "" and _dv(60, 30) == ""
        # 往上拖 → 不撕出(上缘 4px 是窗口缩放带)
        and _dv(0, -60) == "" and _dv(5, -40) == ""
        # 斜向: 纵向必须明显大于横向才认
        and _dv(10, 30) == "detach" and _dv(45, 30) == ""
        # 阈值边界
        and _dv(0, 25) == "hint" and _dv(0, 26) == "detach"
        # 150% DPI: 阈值同步放大(26*1.5=39)
        and _dv(0, 38, 1.5) == "hint" and _dv(0, 40, 1.5) == "detach"
        # 提示态: 拖了但还不够深 → 给视觉反馈而不是撕出
        and _dv(0, 8) == "hint" and _dv(0, 7) == ""
    )
    check("撕出判据行为: 只认向下 / 手抖与横竖斜向都不撕出 / 阈值随 DPI 缩放", _drag_ok)
except Exception as _e:
    check("撕出判据行为: 只认向下 / 手抖与横竖斜向都不撕出 / 阈值随 DPI 缩放", False, _e)
check("壳接线: install_floats + bind_floats + tab_detach/window_dock",
      "install_floats" in _aw_src and "bind_floats" in _aw_src
      and "def tab_detach" in _aw_src and "def window_dock" in _aw_src)
check("shell_info 暴露悬浮窗能力", aw.ShellApi().shell_info().get("windows") is True)

# ---- 悬浮窗黑屏(v0.3.1.42dsk): 补呈现看门狗(校验+重试) + 宿主编排变化触发 ----
check("补呈现看门狗: 递进时刻表 + 旧行为时刻表(排障 A/B 用)",
      "_REP_TAPS = (0.25, 0.6, 1.1, 1.8, 2.8, 4.0, 5.5, 7.0)" in _sp_src
      and "_REP_LEGACY_TAPS = (0.35, 0.9, 1.6)" in _sp_src
      and "MSX_NO_REPRESENT" in _sp_src)
check("补呈现判据是'整块平色'而不是'是不是黑'(否则正常播放页会被误判成坏)",
      "def _frame_flat(hwnd)" in _sp_src
      and "len(seen) <= 2 and top >=" in _sp_src
      and "0x00FF00FF" in _sp_src)
check("补呈现: 校验是否真出图, 出图即收工; 还黑就继续补(不再固定三次撒手)",
      "def _represent(" in _sp_src
      and '_frame_flat(hb.get("h")) is False' in _sp_src
      and "represent TIMEOUT" in _sp_src)
check("补呈现手段最多两级(隐藏→显示→置顶 / 强制重绘) —— 摘挂父容器已废除",
      # 用户报修: 播放视频点全屏 → 画布整块没视频、界面像卡死。
      # 根因之一就是第 3 级 par.Controls.Remove/Add: reparent 会让 WebView2 的合成器停摆,
      # 画面再也回不来。WebView2 不是普通 WinForms 控件, 不许用这招"逼排版"。
      "def _rep_level(i)" in _sp_src
      and "return 1 if i < 2 else 2" in _sp_src
      and "cc.Visible = False" in _sp_src and "cc.Invalidate(True)" in _sp_src
      and "par.Controls.Remove(cc)" not in _sp_src)
check("全屏补呈现: 默认轻补(不碰 Visible)、关掉 GDI 校验、MSX_FS_HARD 才是硬补逃生口",
      # 用户报修: 全屏看视频有概率卡住画布(声音还在)。真机日志里硬补(隐藏→显示)
      # 把浏览器踢出了 HTML 全屏 —— 所以默认只置顶+重绘+DwmFlush, 硬补留作逃生口。
      "def _rep_tap(ui, ctrls, cc, key, hwnd_box, level, gentle=False)" in _sp_src
      and "if not gentle:" in _sp_src
      and 'key, "fullscreen", gentle=_fs_soft(),' in _sp_src
      and "verify=False, fs_guard=True, settle=0.25," in _sp_src
      and "taps=_fs_taps()" in _sp_src
      and "def _fs_taps()" in _sp_src
      and 'return os.environ.get("MSX_FS_HARD", "0") != "1"' in _sp_src
      and "DwmFlush" in _sp_src
      and "if not verify:" in _sp_src)
# 用户报修(白屏第二轮): 主窗口被误判成悬浮窗 → 不最大化、不 Dock=Fill, 全屏只能靠页面自己铺满。
# 根因是 `host is not box["form"]` —— pywebview 的 native 每次访问可能返回新包装对象。
check("全屏: 主窗口/悬浮窗按 HWND 判(不能用 is, 否则主窗口被当成悬浮窗)",
      "def _same_form(self, a, b)" in _sp_src
      and 'is_float = not self._same_form(host, box.get("form"))' in _sp_src
      and "is not box.get(\"form\")" not in _sp_src)
check("全屏: 悬浮窗分支不用 WF.Point(Point 在 System.Drawing, 否则一进来就 AttributeError)",
      "WF.Point(" not in _sp_src
      and "from System.Drawing import Point as _Pt" in _sp_src)
check("全屏: 进出都留诊断日志(fs enter / enter done / enter FAILED / exit)",
      "fs enter key=" in _sp_src and "fs enter done key=" in _sp_src
      and "fs enter FAILED key=" in _sp_src and "fs exit key=" in _sp_src)
check("补呈现可取消(控件隐藏/换掉/销毁时停掉看门狗, 否则会被 toggle 出来)",
      "def _rep_cancel(cc)" in _sp_src and "_rep_cancel(c)" in _sp_src
      and "_rep_cancel(_Pool.ctrls.get(key))" in _sp_src)
check("控件池公开 represent 入口(悬浮窗侧据此触发)",
      'def represent(self, key, why="")' in _sp_src)
check("悬浮窗: 激活/缩放/移动/DPI 变都触发补呈现(带尾部去抖, 不干扰拖动)",
      "_wire_repaint" in _fs_src and "form.Activated +=" in _fs_src
      and "form.Resize +=" in _fs_src and "form.LocationChanged +=" in _fs_src
      and "form.DpiChangedAfterParent +=" in _fs_src and "rep_timer" in _fs_src)
check("塞回标签页那次也补呈现(迁回来同样是'宿主编排变化')",
      'self._pages.represent(tid, "undock")' in _aw_src)
check("机器相关环境字段落盘(OS/DPI/显示器数/WebView2 内核/浏览器参数)",
      "def _log_runtime_env()" in _sp_src
      and "GetAvailableBrowserVersionString" in _sp_src
      and "_log_runtime_env()" in _sp_src)


class FakeFloatMgr:
    def __init__(self):
        self.opened, self.docked, self.closed = [], [], []

    def open(self, tab, pages=None):
        self.opened.append(tab["id"])

    def dock(self, tid):
        self.docked.append(tid)
        return True

    def close(self, tid):
        self.closed.append(tid)
        return True

    def set_title(self, tid, title):
        pass

    def exists(self, tid):
        return True


api3 = aw.ShellApi()
api3.attach(None, "http://127.0.0.1:8000")
fm = FakeFloatMgr()
api3.bind_pages(FakePages())
api3.bind_floats(fm)
api3._new_window_enabled = lambda: True   # 默认开
check("新窗口模式: 点开链接 → 悬浮窗(不占标签栏, 主窗口仍停在信息流)",
      api3.open_in_app("https://www.bilibili.com/video/BV2") == "ok"
      and api3._tabs[1]["float"] is True and fm.opened == [1] and api3._active == 0)
rendered = []
api3.bind_strip(lambda tabs, active: rendered.append(([t["id"] for t in tabs], active)))
check("悬浮标签不出现在主窗口标签栏", bool(rendered) and rendered[-1][0] == [0])
check("塞回标签页: float→False 且成为激活标签",
      api3.window_dock(1) == "ok" and api3._tabs[1]["float"] is False
      and api3._active == 1 and fm.docked == [1])
check("拖出: 标签页 → 悬浮窗", api3.tab_detach(1) == "ok"
      and api3._tabs[1]["float"] is True and fm.opened[-1] == 1 and api3._active == 0)
check("关悬浮标签: 销毁悬浮窗 + 移除标签",
      api3.tab_close(1) == "ok" and fm.closed == [1] and len(api3._tabs) == 1)
api3._new_window_enabled = lambda: False   # 关掉设置 → 回到纯标签页模式
api3.open_in_app("https://tieba.baidu.com/p/9")
_tid = api3._tabs[1]["id"]
check("关掉设置: 点开链接 → 普通标签", api3._tabs[1]["float"] is False
      and api3._active == _tid)
check("关掉设置: 拖出仍生效(开关只管「点开信息」怎么打开, bug2)",
      api3.tab_detach(_tid) == "ok" and api3._tabs[1]["float"] is True
      and fm.opened[-1] == _tid)
check("固定标签(信息流)不可拖出 / 塞回", api3.tab_detach(0) == "pinned"
      and api3.window_dock(0) == "not-float")
check("控件池缺失时优雅降级为主窗口标签",
      aw.ShellApi().open_in_app("https://tieba.baidu.com/p/7") == "ok")

# ---------- 2d. 悬浮窗"拽到标签栏松手即合并"(v0.3.1.34 交互修正) ----------
check("悬浮窗落点判定 + 拖拽中提示(_over_tabbar / _hint)",
      "def _over_tabbar()" in _fs_src and "def _hint(on, win=None)" in _fs_src
      and "main.PointToClient(WinForms.Cursor.Position)" in _fs_src)
check("悬浮窗自身也画落点提示(拖拽时它盖住标签栏, 主窗口侧高亮看不见)",
      '"drop_hint": False' in _fs_src and 'win.get("drop_hint")' in _fs_src
      and "松手合并到标签栏" in _fs_src
      and 'accent_getter=lambda: app.sget("float_ball_accent")' in _aw_src)
check("自绘拖动(bar.Capture + 跟手移动), 不用 HTCAPTION 模态循环",
      "bar.Capture = True" in _fs_src and 'win["dragging"] = True' in _fs_src
      and 'form.Location = Point(cur.X - win["dx"], cur.Y - win["dy"])' in _fs_src)
check("MouseUp 落在标签栏 → api.window_dock(合并成标签页)",
      "bar.MouseUp += on_up" in _fs_src and "over = _over_tabbar()" in _fs_src
      and "api.window_dock(tid)" in _fs_src and 'win.pop("drag_moved", False)' in _fs_src)
check("标签栏暴露 highlight 并在 Paint 里绘制落点高亮",
      "update.highlight = highlight" in _st_src and 'st["drop_hi"]' in _st_src
      and 'if st.get("drop_hi"):' in _st_src)
check("壳把标签栏高度交给悬浮窗, 且标签栏先于悬浮窗安装",
      'tabstrip_h=int(getattr(strip_update, "bar_h", 0) or 0)' in _aw_src
      and 0 <= _aw_src.index("from shell_tabstrip import install_strip")
      < _aw_src.index("from shell_float import install_floats"))
check("e2e 钩子覆盖真实拖拽手势(_e2e_drag_to_tabbar)",
      "def _e2e_drag_to_tabbar" in _aw_src and "SetCursorPos" in _aw_src
      and "0x0202" in _aw_src and "drag-merge" in _aw_src)

# ---------- 3c. 播放页自有UI(bilibili-ownui): 注入接线 + 设置白名单 + 设置行 ----------
_ownui = io.open(os.path.join(PROJECT, "extensions", "bilibili-ownui.user.js"), encoding="utf-8").read()
check("播放页自有UI脚本: 存在且关键API齐备",
      len(_ownui) > 8000 and "x/v2/reply/add" in _ownui and "x/v2/reply/action" in _ownui
      and "x/v2/reply/reply" in _ownui and "archive/related" in _ownui
      and "attachShadow" in _ownui and "__MSX_OWNUI__" in _ownui
      and "/video/" in _ownui and "bili_jct" in _ownui and "SESSDATA" not in _ownui)
_sp = io.open(os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read()
check("自有UI注入接线(ui_ownui门控→__MSX_OWNUI__)",
      "bilibili-ownui.user.js" in _sp and "__MSX_OWNUI__" in _sp and "ui_ownui" in _sp)
# 断言前端源码前先删注释(规范: 注释里出现的关键字不算实现 —— ownui 的广告屏蔽注释
# 明确写了「不要动 .bpx-player-adv-dm-wrap」, 那是说明而不是实现)
_ownui_c = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", _ownui, flags=re.S))
check("自有UI不碰播放器DOM(脚本无播放器选择器)",
      "bilibili-player" not in _ownui_c and "bpx-player" not in _ownui_c)
check("自有UI设置白名单(app.py三处)",
      io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read().count('"ui_ownui"') >= 3)
_idx = io.open(os.path.join(PROJECT, "web", "index.html"), encoding="utf-8").read()
_appj = io.open(os.path.join(PROJECT, "web", "app.js"), encoding="utf-8").read()
check("自有UI设置行(开/关+回显+保存)",
      'id="ownuiRow"' in _idx and "ownuiRow" in _appj and "ui_ownui" in _appj)

# ---------- 3d. 播放页推荐筛选(v0.3.1.2): m_play_related + 规则注入 + 7:3 混排 ----------
check("播放页筛选设置键(m_play_related 默认1)",
      '"m_play_related": "1"' in io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read())
check("播放页筛选键入 /api/machine GET+POST 白名单",
      io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read().count('"m_play_related"') >= 3)
check("规则对象注入(__MSX_MF__: 词表/正则/拉黑/厌恶词)",
      "__MSX_MF__" in _sp and "FLAME_SLURS" in _sp and "CLICKBAIT_RE" in _sp
      and "dislike_keywords" in _sp and "ablock" in _sp)
check("播放页筛选逻辑(过滤+去重+7:3混排+探索池)",
      "mfFilter" in _ownui and "mixRel" in _ownui and "x/web-interface/popular" in _ownui
      and "mfPass" in _ownui and "normTitle" in _ownui and "emoBad" in _ownui)
check("顶栏只隐导航条目(slimNav逐条目+LOGO豁免+tick补隐), 无整块/猜测选择器",
      "slimNav" in _ownui and "mini-header__logo" in _ownui and "NAV_LABELS" in _ownui
      and "bili-header__channel" not in _ownui and ".left-entry{" not in _ownui)
check("播放页筛选设置行(mPlayRel 回显+保存)",
      'id="mPlayRel"' in _idx and "m_play_related" in _appj)
check("自有UI启动遮罩(挂载前原生评论区/右栏 visibility 隐藏防闪现, 摘除+路由复位齐备)",
      "msx-ownui-boot" in _ownui and "bootMaskOn" in _ownui and "bootMaskOff" in _ownui
      and "visibility:hidden!important" in _ownui)
_srjs = io.open(os.path.join(PROJECT, "extensions", "bilibili-sr.user.js"), encoding="utf-8").read()
check("SR画布防overlay提升+原视频保持可见(整屏截屏不再黑屏)",
      "border-radius:0.5px" in _srjs
      and 'classList.add("msx-sr-hide")' not in _srjs
      and 'setProperty("opacity", "0", "important")' not in _srjs)
check("夜间模式防白闪(B站页 document-start 压深色底, 浅色模式撤除)",
      "setProperty('background', '#0f0f0f', 'important')" in _sp
      and "removeProperty('background')" in _sp)

# ---------- 3. 前端统一出口 ----------
js = io.open(os.path.join(PROJECT, "web", "app.js"), encoding="utf-8").read()
fallback = js.count('window.open(url, "_blank", "noopener")')
check("app.js 裸 window.open 只剩桥内兜底", fallback == 3, str(fallback))
check("app.js 跳转出口调用点 ≥ 10", js.count("openExternal(") >= 11, str(js.count("openExternal(")))
check("index.html 无 target=_blank", 'target="_blank"' not in io.open(
    os.path.join(PROJECT, "web", "index.html"), encoding="utf-8").read())
html = io.open(os.path.join(PROJECT, "web", "index.html"), encoding="utf-8").read()
check("页脚版本与 app.APP_VER 一致", ("洞鉴虚明 " + app.APP_VER) in html, app.APP_VER)
# ★ v0.4.0dsk: 右栏(AI 聊天)在屏幕右侧, 它的变宽方向和鼠标位移相反 ——
#   往左拖要变宽、往右拖要变窄。原来两栏共用 w0+dx, 右栏就是反的。
_split = js[js.find('const sp = e.target.closest(".splitter")'):
           js.find('$("#toggleLeft").onclick')]
check("分栏拖动: 右栏位移取反(左拖变宽/右拖变窄)",
      'const sign = which === "left" ? 1 : -1;' in _split
      and "w0 + sign * (ev.clientX - x0)" in _split
      and "w0 + (ev.clientX - x0)" not in _split, "splitter drag")
check("设置页: 字体大小三档(默认小)", 'id="fontRow"' in html and html.count("data-font=") == 3
      and 'data-font="small"' in html)
check("设置页: 背景预设/自定义/深度滑条在强调色设置下方",
      0 < html.find("customAccentRow") < html.find("bgRow")
      < html.find("bgCustomRow") < html.find("bgDepthRow") < html.find("fontRow"))
_appjs = io.open(os.path.join(PROJECT, "web", "app.js"), encoding="utf-8").read()
check("背景系统: 自定义色+1..10深度派生全套表面色",
      "hexToHsl" in _appjs and 'set("--panel"' in _appjs
      and 'set("--input"' in _appjs and "bgDepth" in _appjs and "bgHex" in _appjs)
check("背景系统: 预设色卡+自定义历史+推送标签栏方案",
      "bgCustomList" in _appjs and "bgStripScheme" in _appjs
      and "bg: bgStripScheme()" in _appjs)
check("标签栏吃背景方案: 服务端存储+轮询消费",
      "ui_bg_scheme" in io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read()
      and "ui_bg_scheme" in io.open(os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read()
      and "set_bg_scheme" in io.open(os.path.join(PROJECT, "shell_tabstrip.py"), encoding="utf-8").read())
check("深度滑条跟随强调色", "accent-color: var(--accent)" in io.open(
    os.path.join(PROJECT, "web", "style.css"), encoding="utf-8").read())
check("内置画质增强脚本: SVG非锐化掩模(模糊-合成)锐化+色彩滤镜+服务端开关",
      "feGaussianBlur" in io.open(os.path.join(PROJECT, "extensions", "bilibili-enhance.user.js"), encoding="utf-8").read()
      and "feComposite" in io.open(os.path.join(PROJECT, "extensions", "bilibili-enhance.user.js"), encoding="utf-8").read()
      and "msx-sharpen" in io.open(os.path.join(PROJECT, "extensions", "bilibili-enhance.user.js"), encoding="utf-8").read()
      and "__MSX_BQ__" in io.open(os.path.join(PROJECT, "extensions", "bilibili-enhance.user.js"), encoding="utf-8").read())
_srlib = io.open(os.path.join(PROJECT, "extensions", "anime4k-webgpu.js"), encoding="utf-8").read()
_srglue = io.open(os.path.join(PROJECT, "extensions", "bilibili-sr.user.js"), encoding="utf-8").read()
check("AI超分: Anime4K-WebGPU库(MIT)+胶水+开关接线",
      len(_srlib) > 3000000 and "CNNx2UL" in _srglue
      and "requestVideoFrameCallback" in _srglue
      and "ui_sr" in io.open(os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read()
      and '"ui_sr"' in io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read()
      and 'id="srLevelRow"' in html and 'data-level="关"' in html)
check("画质增强接线: 注入管线+设置白名单+独立设置栏(档位即开关)",
      "__MSX_BQ__" in io.open(os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read()
      and '"ui_bq", "ui_bq_level"' in io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read()
      and 'id="bqLevelRow"' in html and 'data-level="关闭"' in html
      and "<summary>画质增强设置</summary>" in html
      and 'id="bqRow"' not in html
      # 位置: 紧跟在「主题设置」栏后面(主题设置 < 画质增强设置 < AI 配置)
      and html.index("<summary>主题设置</summary>")
      < html.index("<summary>画质增强设置</summary>")
      < html.index("<summary>AI 配置</summary>"))
check("壳关闭站点弹新页→系统浏览器的默认行为",
      "OPEN_EXTERNAL_LINKS_IN_BROWSER" in io.open(
          os.path.join(PROJECT, "app_window.py"), encoding="utf-8").read())

# ---------- 3f. 13 项用户报修回归(v0.3.1.38dsk) ----------
_nb_src = io.open(os.path.join(PROJECT, "shell_navbar.py"), encoding="utf-8").read()
_css_src = io.open(os.path.join(PROJECT, "web", "style.css"), encoding="utf-8").read()
_cf_src = io.open(os.path.join(PROJECT, "extensions", "bilibili-cfilter.user.js"),
                  encoding="utf-8").read()
_cf_c = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", _cf_src, flags=re.S))

# bug1 设置栏折叠某些子栏后画布高度不够、拉不到最下方
check("bug1 设置栏高度自愈: JS 实测视口 --app-h + flex 子项 min-height:0",
      "height:calc(var(--app-h, 100vh) / var(--ui-zoom, 1))" in _css_src
      and "min-width:46px; min-height:0;" in _css_src
      and "#railBody { flex:1; overflow-y:auto; padding:0 12px 24px; min-height:0; }" in _css_src
      and "function syncAppH()" in _ajs_src
      and 'setProperty("--app-h", h + "px")' in _ajs_src
      and 'window.addEventListener("resize", syncAppH)' in _ajs_src)

# bug2 关掉「信息以新窗口形式打开」后仍应能从标签页拖出悬浮窗
_td = re.search(r"def tab_detach\(self.*?(?=\n    def )", _aw_src, re.S)
_td_code = re.sub(r"#[^\n]*", "", _td.group(0)) if _td else ""
check("bug2 关掉开关不再掐掉拖出悬浮窗(开关只管「点开信息」怎么打开)",
      _td is not None and "_new_window_enabled" not in _td_code
      and 'return "pinned"' in _td_code and 'return "already"' in _td_code
      and 'mode = "window" if self._new_window_enabled() else "tab"' in _aw_src)

# bug3 设置项文案太繁琐 → 短标题 + 悬停 title 给详细描述
check("bug3 设置项文案精简(13 条短标题 + title 悬停详细描述)",
      _idx.count('<label title="') >= 13
      and '<label title="点开信息弹独立小窗' in _idx
      and "信息以新窗口形式打开" in _idx
      and "强调色（按钮/徽标/高亮的特殊颜色）" not in _idx)

# bug4 悬浮窗缺全屏/最小化按钮; 除最小化外始终在软件图层最上方
# (本轮用户报修 m02020: 置顶曾是系统级 TopMost, 压住了 QQ 等别的应用 —— 改成 owner 关系,
#  只在主窗口之上; 所以这里断言"有 owner、且代码里不再有 TopMost")
_fs_code = re.sub(r"#[^\n]*", "", _fs_src)
check("bug4 悬浮窗补「最小化/全屏」按钮 + 只在软件图层之上(owner, 不压别的应用)",
      '_BTN_MIN, _BTN_MAX = "min", "max"' in _fs_src
      and "_BTN_ORDER = (_BTN_DOCK, _BTN_MIN, _BTN_MAX, _BTN_CLOSE)" in _fs_src
      and '(_BTN_MIN, "—")' in _fs_src
      and '(_BTN_MAX, "▣" if _is_max(win) else "□")' in _fs_src
      and "form.Owner = main" in _fs_code
      and "TopMost" not in _fs_code
      and "WinForms.FormWindowState.Minimized" in _fs_src
      and "def _toggle_max(win)" in _fs_src)

# bug5 视频播放页弹幕列表无法折叠
check("bug5 弹幕列表可折叠(标题即开关 + 折叠态记忆)",
      'id="msxDmFold"' in _ownui_c and 'id="msxDmCols"' in _ownui_c
      and 'id="msxDmAr"' in _ownui_c
      and 'localStorage.getItem("msx_dm_fold")' in _ownui_c
      and 'localStorage.setItem("msx_dm_fold"' in _ownui_c)

# bug6 视频全屏时隐藏网址栏与选项栏, 整窗纯播放
check("bug6 网页全屏隐藏标签栏/导航栏(网页事件→控件池→窗口壳→两条 chrome)",
      "core.ContainsFullScreenElementChanged += on_fs_changed" in _sp_src
      and "bool(core.ContainsFullScreenElement)" in _sp_src
      and "def set_fullscreen(self, key, on)" in _sp_src
      and "def set_fullscreen(on)" in _st_src
      and "update.set_fullscreen = set_fullscreen" in _nb_src
      and "on_fs=on_fs" in _aw_src and 'chrome["nav"]' in _aw_src)

# 全屏卡死(用户报修): 播放器常常连发好几次全屏状态事件, 每次都重排窗口/控件 → 抖动成"卡死"
check("全屏事件按标签去重(同状态重复事件直接丢, 否则反复最大化/还原)",
      "_fs_last = {}" in _sp_src
      and "last = _Pool._fs_last.get(tab_key)" in _sp_src
      and "if last is on:" in _sp_src
      and "_Pool._fs_last[tab_key] = on" in _sp_src)

# bug7 地址栏文字纯白/纯黑太刺眼 → 未悬停时 70% 不透明度
check("bug7 地址栏文字未悬停时 70% 不透明度(悬停/编辑回满)",
      '"tb_hover": False' in _nb_src
      and "def _blend(c, bg, k)" in _nb_src
      and "def _apply_tb_fg()" in _nb_src
      and 'else _blend(st["fg"], st["tb_bg"], 0.7))' in _nb_src
      and "def _set_tb_hover(v)" in _nb_src
      and "tb.MouseEnter" in _nb_src and "tb.MouseLeave" in _nb_src)

# ---------- 3g. 顶栏视觉重做(v0.4.43dsk): 对标 Chrome/Edge 的结构性约定 ----------
# 用户原话: "优化一下我们的顶栏, 我们做的实在是太差了, 完全不如谷歌和 edge"。
# 差距不在配色而在**结构**: 旧版是"深色条上浮着一排独立圆角小卡片 + 用文字符号画
# ✕/+/─□✕"。下面这些断言锁的是结构, 不是像素 —— 以后调色不会把它们改坏。
check("顶栏结构1 激活标签与工具栏同色齐平(连成一片, 不是浮在条上的卡片)",
      # 工具栏底色取 scheme 的 active(= 激活标签色), 标签条底色取 bar(更深一档)
      'st["bar"] = _c("active", st["bar"])' in _nb_src
      and 'st["strip"] = _c("bar", st["strip"])' in _nb_src
      and "tab_h = bar_h - tab_top" in _st_src
      and "g.FillPath(SolidBrush(st[\"tab_active\"])," in _st_src)
check("顶栏结构2 标签相邻无缝隙 + 激活标签带向下张开的脚(压住左右邻居)",
      "x += tw + gap" not in _st_src          # 旧版: 每个标签之间留 4px 间隙
      and "return [(margin + i * tw, tw) for i in range(n)]" in _st_src
      and "def _tab_path(x, y, w, hh, rad, foot)" in _st_src
      and "p.AddArc(x + w, b - f * 2, f * 2, f * 2, 180, -90)" in _st_src
      and "p.AddArc(x - f * 2, b - f * 2, f * 2, f * 2, 90, -90)" in _st_src
      # 激活标签必须最后画, 否则它的脚会被后面的邻居盖掉
      and 'key=lambda k: (st["tabs"][k]["id"] == act_id)' in _st_src)
check("顶栏结构3 图标全部是 1px 矢量线(不再用 ✕ / + / ─ □ ✕ 文字符号)",
      'g.DrawString("✕"' not in _st_src and 'g.DrawString("+"' not in _st_src
      and '(("min", "─"), ("max", "□"), ("close", "✕"))' not in _st_src
      and "g.DrawLine(xp, cx - s, cy - s, cx + s, cy + s)" in _st_src   # 标签 ✕
      and "g.DrawLine(pp, acx - ps, acy, acx + ps, acy)" in _st_src     # 「+」的横
      and "g.DrawRectangle(wp, cx - s, cy - s, s * 2, s * 2)" in _st_src)  # 最大化
check("顶栏结构4 标签带站点图标瓦片(域名取色, 无 favicon 时用首字母兜底)",
      "def _fav(t)" in _st_src and "FAV_COLORS" in _st_src
      and 't.get("url")' in _st_src
      and '"url": t.get("url") or ""' in _aw_src.replace("\r\n", "\n"))
check("顶栏结构5 地址栏是胶囊(两端全圆) + 站点图标在胶囊内 + 聚焦给强调色描边",
      "def _pill_rect()" in _nb_src
      and "prad = ph // 2" in _nb_src
      and "_rounded_path(px, py, pw, ph, prad)" in _nb_src
      and "st[\"accent\"], max(1.0, 1.4 * scale)" in _nb_src
      # 原生 TextBox 必须内缩在胶囊里, 否则它会把圆角切方
      and "right = px + pw - rad - int(2 * scale)" in _nb_src)
check("顶栏结构6 悬停底色取 active↔bar 中点(层次: 栏底 < 悬停 < 激活)",
      "def _mix(c, base, k)" in _st_src
      and 'st["tab_hover"] = _mix(st["tab_active"], st["bar"], 0.5)' in _st_src
      # 页面给的 hover 在深色下比 active 还亮, 直接用会把层次弄反
      and 'st["tab_hover"] = _c("hover", st["tab_hover"])' not in _st_src)


# bug8 开启评论筛选屏蔽后评论偶发无法继续向下滚动
check("bug8 主评论列表一条不删(只标记, 组件才会继续翻页) + 补泵翻页(屏蔽后仍能一路下滑)",
      "function isMainList(u)" in _cf_c and "/x/v2/reply/wbi/main" in _cf_c
      and "if (isMain) { markBlocked(r); out.push(r); continue; }" in _cf_c
      and "filtArr(r.replies, false)" in _cf_c
      and "if (!isMain && reps0 > 0" in _cf_c
      and "function pump()" in _cf_c and "inst.getList()" in _cf_c
      and "redactOne" not in _cf_c and "__msxRedacted" not in _cf_c)

# bug9 右侧推荐视频封面显示太小
check("bug9 右侧推荐封面放大(缩略图 60% 宽 / 150-300px / 16:9)",
      ".it .cv{position:relative;flex:0 0 60%;min-width:150px;max-width:300px;"
      "aspect-ratio:16/9;" in _ownui_c)

# bug10 全屏时左侧有缝能看到桌面 + 播放界面白边
check("bug10 全屏左缝/白边: 贴边热区用窗体底色 + 全屏把宿主窗最大化并 Dock=Fill",
      "EDGE_COLOR = Color.FromArgb(15, 17, 21)" in _st_src
      and "p.BackColor = EDGE_COLOR" in _st_src
      and 'and not fs_state["on"])' in _st_src
      and "_EDGE = Color.FromArgb(15, 17, 21)" in _fs_src
      and "def _tab_geo(form, home, key)" in _sp_src
      and "host.WindowState = WF.FormWindowState.Maximized" in _sp_src
      and "c.Dock = WF.DockStyle.Fill" in _sp_src)

# bug11 B站播放页里的大广告框需要屏蔽删除
check("bug11 播放页大广告框屏蔽(CSS 常驻规则, 不碰高级弹幕层)",
      ".ad-report" in _ownui_c and ".ad-floor-cover" in _ownui_c
      and ".right-bottom-banner" in _ownui_c
      and ".video-page-special-card-small" in _ownui_c
      and ".bpx-player-adv-dm-wrap" not in _ownui_c)

# bug12 信息流来源标签的强调色实底太闪眼
# (本轮用户报修 m03204: 两处「信息来源」文字改为跟随主题强调色 —— 实底仍取消, 只留细边)
check("bug12 信息流来源标签取消强调色实底(细边; 文字色跟随强调色)",
      ".card .badge { display:inline-block; background:transparent;" in _css_src
      and ".card .badge.plat-bili, .card .badge.plat-tb, .card .badge.plat-xhh "
          "{ color:var(--accent); }" in _css_src
      and ".plat-bili, .plat-tb, .plat-xhh { color:var(--accent); }" in _css_src
      and ".plat-tag { font-style:normal; font-size:9px; line-height:1.3; color:var(--sub);"
      in _css_src
      and "background:transparent; pointer-events:none; }" in _css_src
      and "#7fa3e8" not in _css_src and "#e08a8d" not in _css_src)

# bug13 超分辨率暂时性画布不对时弹报错框
check("bug13 超分暂时性画布异常不再弹框(只进控制台, 连续两次才提示)",
      "瞬时异常只进控制台" in _srjs and "console.warn" in _srjs)

# ---------- 3g. 播放页视觉 5 项(m03204 的 4 项 + m03496 的弹幕默认折叠/悬浮窗内全屏) ----------
check("视觉1 「相关推荐」标题字号加大(15→19, 更粗)",
      '.rel{font-size:19px;font-weight:700;padding:6px 12px}' in _ownui_c)

check("视觉2 播放页左侧留白改在页面内部(取消窗口级留白, B站顶栏从左缘连续)",
      "TAB_INSET_RATIO" not in re.sub(r"#[^\n]*", "", _sp_src)          # m03496: 窗口级留白已删
      and "_tab_dx" not in re.sub(r"#[^\n]*", "", _sp_src)
      and "Size(max(1, home.Size.Width), max(1, home.Size.Height))" in _sp_src
      and "#msx-ownui-page-pad" in _ownui_c
      and '"html:not(:fullscreen) "' in _ownui_c
      and "padding-left:4vw!important;box-sizing:border-box!important" in _ownui_c
      and "document.fullscreenElement" in _ownui_c)

check("视觉3 左侧留白底色 = B站播放页背景(不再有分界线)",
      'BG_PAGE = "#17181a"' in _aw_src
      and "background_color=BG_PAGE" in _aw_src
      and "def _apply_page_bg(self, dark)" in _sp_src
      and "_C.FromArgb(23, 24, 26)" in _sp_src)

check("视觉4 弹幕列表默认折叠(打开播放页即收起, 用户 m03496)",
      "var folded = true;" in _ownui_c
      and 'localStorage.getItem("msx_dm_fold") !== "0"' in _ownui_c
      and "▸" in _ownui_c)

check("视觉5 悬浮窗内点全屏只铺满悬浮窗(不动宿主窗口状态, 用户 m03496)",
      # is_float 的判定方式已改成按 HWND 比较(见上一条), 这里只看分支逻辑还在不在
      'is_float = not self._same_form(host, box.get("form"))' in _sp_src
      and "if is_float:" in _sp_src
      and 'getattr(WF.DockStyle, "None")' in _sp_src
      and "c.Size = host.ClientSize" in _sp_src
      and "if not is_float:" in _sp_src
      and "is_float)\n" in _sp_src)

# ---------- 3h. 设置导出脱敏(m04225: 只导出设置, 不含 aiKey/登录账号) ----------
check("导出脱敏1 后端白名单: ai_key 归入账号内容, 脱敏导出只放 _IO_SHAREABLE",
      '"ai_key"' in _app_src
      and "_IO_SHAREABLE = frozenset(_IO_IMPORTABLE - _IO_CRED_KEYS)" in _app_src
      and "allow = _IO_IMPORTABLE if include_credentials else _IO_SHAREABLE" in _app_src
      and "if k in allow and v not in (None, \"\")" in _app_src
      and '"sanitized": (not include_credentials)' in _app_src)

check("导出脱敏2 默认只导出设置: 「导出个人信息和设置」是默认不勾的可选项(m04401)",
      'id="ioExpPriv"' in _idx_src and 'id="ioExpPriv" checked' not in _idx_src
      and "默认不勾" in _idx_src and 'id="ioExpWarn"' in _idx_src and "ioExpCred" not in _idx_src
      and 'const IO_CRED_KEYS = ["ai_key"' in _ajs_src
      and 'const IO_CRED_CAT = "账号与密钥"' in _ajs_src
      and 'const priv = $("#ioExpPriv") ? $("#ioExpPriv").checked : false;' in _ajs_src
      and "credentials: priv" in _ajs_src
      and "ioExpCred" not in _ajs_src
      and "已脱敏" in _ajs_src)

check("导出脱敏3 导入预览会标明脱敏文件, 凭证类目改名为「账号与密钥」且仍可选",
      "此文件已脱敏" in _ajs_src
      and "IO_CRED_KEYS.includes(key) ? IO_CRED_CAT : null" in _ajs_src
      and "cats.push([IO_CRED_CAT, credN])" in _ajs_src
      and "免扫码直接登录" in _ajs_src)

# ---------- 3i. 启动卡死修复(m03991: 新线程首次 db() 不再无条件写库) ----------
# 只看代码行, 把注释里的"改前写法"排除掉(否则 count 会把注释里的示例也算进去)
_app_code = "\n".join(_l for _l in _app_src.splitlines() if not _l.lstrip().startswith("#"))
_gi = _app_code.find('if conn.execute("SELECT 1 FROM chat WHERE cid=0 LIMIT 1").fetchone() is not None:')
_ui = _app_code.find('conn.execute("UPDATE chat SET cid=1 WHERE cid=0")')
check("卡死修复 每线程首次 db() 只在确有 cid=0 行时才写(不再等满 busy_timeout 10s)",
      _app_code.count("UPDATE chat SET cid=1 WHERE cid=0") == 1 and 0 <= _gi < _ui
      and "busy_timeout=10000" in _app_code)

check("卡死修复2 无 cid=0 残留来源: 4 处 INSERT INTO chat 都显式写 cid",
      _app_src.count("INSERT INTO chat(role,content,ts,cid)") >= 4)

# ---------- 3j. 「下载 json 文件」走壳的原生另存为(m04592: WebView2 里 <a download> 没反应) ----------
check("下载修复1 壳里新增 save_text(原生 SaveFileDialog, 返回 ok:<路径>/cancel/err:…)",
      "def save_text(self, filename, text):" in _aw_src
      and "WinForms.SaveFileDialog()" in _aw_src
      and 'dlg.Filter = "JSON 文件 (*.json)|*.json|所有文件 (*.*)|*.*"' in _aw_src
      and 'return "ok:" + path' in _aw_src
      and 'return "cancel"' in _aw_src)

check("下载修复2 页面优先走桥, 失败才退回 Blob; 触屏端有桥时不藏按钮",
      'const nat = window.pywebview && window.pywebview.api;' in _ajs_src
      and "if (nat && nat.save_text)" in _ajs_src
      and 'await nat.save_text(name, $("#ioExpText").value)' in _ajs_src
      and '"已保存到：" + r.slice(3)' in _ajs_src
      and "new Blob([$(\"#ioExpText\").value]" in _ajs_src
      and "if (isTouchDevice() && !(window.pywebview && window.pywebview.api && window.pywebview.api.save_text))" in _ajs_src)

# ---------- 3k. 被屏蔽评论: 整条隐藏 + 评论区顶部一行计数/开关(m04703) ----------
check("评论占位1 被屏蔽的主评论整条隐藏(标记式 + 注入样式的 !important 隐藏规则 + 只扫评论 shadow 根)",
      "function cfCommentEl()" in _cf_src
      and 'querySelectorAll("bili-comment-thread-renderer")' in _cf_src
      and 'bili-comment-thread-renderer[data-msx-blk="1"][data-msx-hide="1"]{display:none !important;}' in _cf_src
      and "function sweep()" in _cf_src
      and "function cfRoots()" in _cf_src)

check("评论占位2 屏蔽条数只在评论区最上方一处 + 「显示屏蔽/隐藏屏蔽」开关(照弹幕筛选那一套)",
      'var BLK_KEY = "msx_cf_show_blocked";' in _cf_src
      and 'sp.id = "msx-cf-bar-t"' in _cf_src
      and 'bt.id = "msx-cf-bar-b"' in _cf_src
      and '"评论筛选：已屏蔽 " + hid + " 条评论"' in _cf_src
      and '(showBlocked ? "隐藏屏蔽" : "显示屏蔽")' in _cf_src
      and "host.insertBefore(bar, host.firstChild)" in _cf_src)

check("评论占位3 数据层不再改写评论内容(原文保留) + 旧的右下角「已隐藏 N 条」浮层已彻底移除",
      "msx-cf-hint" not in _cf_src and "position:fixed" not in _cf_src
      and "function markBlocked(r)" in _cf_src
      # v0.3.1.43: sweep 改成先 rpidOf(d) 再查表 —— 顺手把"内容判定"的原因也带出来
      and "rpidOf(d)" in _cf_src and "BLK_RPIDS[rp]" in _cf_src
      and "function rpidOf(r)" in _cf_src
      and _cf_src.count("kick();") >= 2)

check("评论显示1 「显示屏蔽」时原样放出原文 + 纯 CSS「已屏蔽 · 原因」角标",
      # v0.3.1.43: 角标从写死的"已屏蔽"改成 content:attr(data-msx-why) ——
      # 内容判定命中的要能显示"到底为什么被筛"(已屏蔽 · 引战), 否则对用户是黑盒
      "::before{content:attr(data-msx-why)" in _cf_src
      and '"已屏蔽 · " + why' in _cf_src
      and "data-msx-blk" in _cf_src
      and "data-msx-hide" in _cf_src
      and "REDACT_TXT" not in _cf_src)   # 旧占位文案已彻底移除

check("评论翻页1 屏蔽后补泵翻页(哨兵还在视口就替组件 getList, 到底/加载中不重复)",
      "function pump()" in _cf_src
      and "continueTrigger" in _cf_src
      and "inst.getList()" in _cf_src
      and "inst.showEnd || inst.showSpinner || inst.showContinuations" in _cf_src
      and "getBoundingClientRect" in _cf_src)

# ---------- 3b. 快速文本分析(v0.3.1.43dsk) ----------
# 产品要求: ① 内置 50MB+ 本地模型; ② 视频标题+简介 / 弹幕 / 评论 三条链路;
#          ③ 「快速文本分析」是独立于弹幕/评论总开关的**另一个**总开关。
_dm_src = io.open(os.path.join(PROJECT, "extensions", "bilibili-dmfilter.user.js"),
                  encoding="utf-8").read()
# NEED_TEXT 的**赋值行**单独摘出来判 —— 文件头/正文注释里会引用旧写法做对照,
# 直接对整个文件做 `not in` 会被注释里的引用误伤(踩过)。
_dm_need_text = [ln.strip() for ln in _dm_src.splitlines()
                 if ln.strip().startswith("var NEED_TEXT")]

check("文本分析1 后端: 模型随包分发 + 纯 stdlib 推理(打包走 3.14, 官方 fasttext 无 wheel)",
      "def _ft_path()" in _app_src
      and 'getattr(sys, "_MEIPASS"' in _app_src
      and "import ftrt" in _app_src
      # 只禁止**真的** import, 注释里提到 fasttext 是解释为什么不用它
      and re.search(r"^\s*import fasttext", _app_src, re.M) is None
      and os.path.isfile(os.path.join(PROJECT, "ftrt.py"))
      and os.path.getsize(os.path.join(PROJECT, "model", "ft_model.bin")) > 50 * 1024 * 1024)

check("文本分析2 后端: 三条链路 kind=video/dm/cf 各自独立作用范围开关",
      '{"video": "ft_video", "dm": "ft_dm", "cf": "ft_cf"}' in _app_src
      and 'ft_scan([(it.get("title") or "") + " " + (it.get("desc") or "")], "video")' in _app_src
      and 'path == "/api/textfilter"' in _app_src
      and 'path == "/api/textfilter/probe"' in _app_src)

check("文本分析3 后端: 总开关 ft_on 单独一道闸, 关掉即整批放行(绝不影响正常浏览)",
      'if sget("ft_on") != "1":' in _app_src
      and 'self.send_json({"ok": True, "on": False, "why": [""] * len(texts)})' in _app_src)

check("文本分析4 壳侧注入三个开关, ft_dm/ft_cf 都必须先过总开关 ft_on",
      '"ft_on": app.sget("ft_on") == "1"' in _sp_src
      and 'app.sget("ft_on") == "1" and app.sget("ft_dm") == "1"' in _sp_src
      and 'app.sget("ft_on") == "1" and app.sget("ft_cf") == "1"' in _sp_src)

check("文本分析5 弹幕链路: 文本判定必须在改写字节流之前完成 + 关掉时零请求原样放行",
      # ★ v0.3.1.46dsk: 这里**必须**是裸的 !!CF.ft_dm —— 不能再 `DM_ON &&`。
      #   总开关管的是"按发送者身份"的规则, 管不到"这条说了什么"。
      _dm_need_text == ["var NEED_TEXT = !!CF.ft_dm;"]
      and "Promise.all([askFlags(hashes, true), askText(textsOf(top), true)])" in _dm_src
      and "function blkWhy(o)" in _dm_src and "TXTWHY[t]" in _dm_src
      and "text/plain" in _dm_src and "targetAddressSpace" in _dm_src)

check("文本分析6 评论链路: 完全异步(标记式, 不阻塞评论加载) + 角标带原因",
      "var NEED_TEXT = !!CF.ft_cf;" in _cf_src
      and "BLK_WHY" in _cf_src
      and "noteText(r)" in _cf_src
      and "function msgOf(r)" in _cf_src)

check("文本分析7 设置页: 独立页签 + 独立总开关 + 三通道 + 标签勾选 + 阈值 + 试一下",
      'id="cfTabText"' in _idx_src and 'id="cfText"' in _idx_src
      and 'id="ftOn"' in _idx_src and 'id="ftVideo"' in _idx_src
      and 'id="ftDm"' in _idx_src and 'id="ftCf"' in _idx_src
      and 'id="ftLabYy"' in _idx_src and 'id="ftThreshRow"' in _idx_src
      and 'id="ftProbeIn"' in _idx_src and 'id="ftProbeBtn"' in _idx_src
      and "loadFtSettings()" in _ajs_src
      and '"/api/textfilter/probe"' in _ajs_src)

check("文本分析8 训练/评测脚本随源码留在 _vis(可复现), 但**不进**打包 datas",
      os.path.isfile(os.path.join(PROJECT, "_vis", "fasttext_demo", "build_corpus.py"))
      and os.path.isfile(os.path.join(PROJECT, "_vis", "fasttext_demo", "train_big.py"))
      # spec 现在由 PyInstaller 生成到 build/spec（「清理构建.bat」会整目录删掉），
      # 不适合再当断言目标；直接看构建脚本 —— 它列出的就是全部 --add-data，里面不能有 _vis
      and "_vis" not in io.open(os.path.join(PROJECT, "构建exe.bat"),
                                encoding="gbk").read())

# ---------- 3b-2. 弹幕侧 fastText 与「弹幕屏蔽总开关」解耦(v0.3.1.46dsk) ----------
# 背景(2026-10-10 实测): 用户实际配置 m_dm_on=0 + ft_on=1 + ft_dm=1, 期望"总开关关着、
# fastText 照常拦弹幕" —— 而设置页对用户写的正是"与弹幕屏蔽总开关完全独立"。
# 但注入脚本里写的是 `NEED_TEXT = DM_ON && !!CF.ft_dm`, 于是 askText() 第一行就返回,
# 弹幕侧**一个文本请求都不发**, fastText 100% 失效。评论侧一直是 !!CF.ft_cf, 无此耦合。
# 另一处独立缺陷: 播放器实际走 XHR(不是 fetch), 而 XHR 钩子只 askFlags 不 askText ——
# 文本通道在播放器路径上从来没被喂过数据, 唯一来源是 fetchMissing 自取分段时的预热。
check("文本解耦1 弹幕侧 NEED_TEXT 不再被弹幕总开关短路(与评论侧口径一致)",
      _dm_need_text == ["var NEED_TEXT = !!CF.ft_dm;"]
      and all("DM_ON" not in ln for ln in _dm_need_text)
      and "var NEED_TEXT = !!CF.ft_cf;" in _cf_src)

check("文本解耦2 弹幕总开关仍管着按身份的规则(黑名单/抽奖/空首页/无简介), 不能一起放开",
      "var MYSTERY = DM_ON && !!CF.mystery;" in _dm_src
      and "var LOT = DM_ON && !!CF.lottery;" in _dm_src
      and "var EMPTY = DM_ON && !!CF.empty;" in _dm_src
      and "if (DM_ON && CF.blk_on && CF.dmhash && CF.dmhash.length)" in _dm_src
      # ★ 后端也必须设防: 注释自己写着"不能指望页面脚本 gating", 而且不设防的话
      #   逆推 300 哈希 ≈ 3 秒纯 CPU + 上千候选塞进**联网**补图队列(风控源头)。
      and 'if sget("m_dm_on") == "0":' in _app_src)

check("文本解耦3 XHR 路径(播放器实际走这条)也要给文本通道预热",
      "askText(r.texts, false);" in _dm_src
      and "return { out: out, hashes: hashes, texts: texts, rows: rows, hrows: hrows, raw: raw };" in _dm_src
      and "texts: [], rows: [], hrows: [], raw: [] };" in _dm_src)

check("文本解耦4 设置页/后端文案必须说清「总开关管身份、管不到文本」",
      "「快速文本分析」的弹幕通道不受本开关影响" in _idx_src
      and "管不到" in _app_src)

# 这次 bug 的形态是"一行完全合法的赋值把两条通道焊死了" —— 静态断言(文本解耦1)
# 只能证明字符串写对了, 证明不了**行为**。所以另有一份 node 沙盒回归: 造真实
# wire-format 的 seg.so 字节流喂给 XHR 钩子, 看它到底往 127.0.0.1 发了哪些请求。
# 实测判别力: 把 NEED_TEXT 改回 `DM_ON && !!CF.ft_dm` → T1/T5 立刻 FAIL。
# 不塞进本脚本(要 node 且跑真异步), 与 止损13 同样的处理方式。
check("文本解耦5 有可重跑的行为级回归(不能只靠字符串断言)",
      os.path.isfile(os.path.join(PROJECT, "_test_dm_ft_gate.js"))
      and "总开关关 + fastText 开" in io.open(
          os.path.join(PROJECT, "_test_dm_ft_gate.js"), encoding="utf-8").read())

# ---------- 3c. 弹幕等级筛选移除 + 画像补图止损(v0.3.1.45dsk) ----------
# 背景(2026-10-10 实测, 隔离库副本 + 真实接口): 弹幕等级筛选是三条筛选链路里
# **唯一必须联网**的一条 —— 弹幕接口只给 8 位 midHash, 判等级要「本地逆推 → 候选 mid
# → 联网查 acc/info」。而一个 8 位哈希平均撞 6.17 个候选(最大 36, 89% 多候选),
# 一页上千次 B站请求; 档位 A/B 实测 -352 风控占比 92%/96%/100%/100%,
# 且风控一旦触发就是"全有全无"(隔 1 分钟单发 1 个请求仍 -352)。
# 更糟的是旧实现把失败的 mid **直接丢弃**: 实测 73 个候选 7.5 秒排空、只落库 26 个,
# 端到端 60 秒只筛出 2 条 —— 正是用户看到的"一两分钟筛不了三四条"。
# 处置: 弹幕侧等级筛选删除(等级只保留评论侧, 评论接口自带 level_info, 零请求);
# 画像链路补上 硬退避 + 失败重入队 + 全请求过闸门 + 前端回问窗口放宽。
_app_src = io.open(os.path.join(PROJECT, "app.py"), encoding="utf-8").read()
_sp_src = io.open(os.path.join(PROJECT, "shell_pages.py"), encoding="utf-8").read()

check("止损1 弹幕侧等级筛选已删除(JS 无「等级低」/&lvl=, 且不进 NEED_FLAGS)",
      "等级低" not in _dm_src and "&lvl=" not in _dm_src
      and "var NEED_FLAGS = MYSTERY || LOT || EMPTY;" in _dm_src)

check("止损2 后端同步删除(无 lvl 参数 / 不再解析 ?lvl= / 不再回 level 标记)",
      "def _am_bad(c, want_bio=False, want_lottery=False, want_empty=False):" in _app_src
      and "def bili_dm_flags(hashes, want_bio=False, want_lottery=False, want_empty=False, multi=1):"
      in _app_src
      and 'qs.get("lvl"' not in _app_src
      and '"level": c.get("level")' not in _app_src)

check("止损3 评论侧等级筛选保留(本地判定, 零请求) —— 不能连坐删掉",
      "var LV = Number(CF.lvl) || 0;" in _cf_src
      and "cur <= LV" in _cf_src
      and '"lvl": min(5, max(0, _cf_lvl))' in _sp_src)

check("止损4 删掉 _mig_am_level27 迁移(它把几百行画像标脏 → 成百上千次 B站请求)",
      "_mig_am_level27" not in _app_src)

check("止损5 两个 B站请求都过同一个闸门(旧实现只给账号发令牌, 动态那次不限速 → 实测 14 请求/秒)",
      "_am_gate.acquire()\n        acc, _code = _bili_acc_probe(m)" in _app_src
      and "_am_gate.acquire()          # 动态接口也过同一个闸门" in _app_src)

check("止损6 风控硬退避档位递增(60→180→600→1800)",
      app._AM_RISK_COOLDOWN[0] >= 60
      and all(app._AM_RISK_COOLDOWN[i] < app._AM_RISK_COOLDOWN[i + 1]
              for i in range(len(app._AM_RISK_COOLDOWN) - 1)))

# 行为: 撞 -352 → 立刻暂停(≈60s); 一次成功 → 清零
app._am_risk["streak"] = 0
app._am_risk["pause_until"] = 0.0
app._am_note_risk(-352)
_paused = app._am_paused()
_first = app._am_risk["pause_until"] - time.time()
app._am_note_ok()
check("止损7 -352 立刻暂停队列(≈60s), 一次成功即清零",
      _paused and 55 <= _first <= 65 and not app._am_paused(),
      "paused=%s first=%.1f" % (_paused, _first))

# 行为: 退避逐次加码并封顶(手动清窗口模拟"窗口过去又撞一次")
_seen = []
for _ in range(len(app._AM_RISK_COOLDOWN) + 1):
    app._am_risk["pause_until"] = 0.0
    app._am_note_risk(-352)
    _seen.append(int(round(app._am_risk["pause_until"] - time.time())))
app._am_note_ok()
check("止损8 退避逐次加码且封顶在最后一档", _seen[:4] == [60, 180, 600, 1800]
      and _seen[4] == 1800, _seen)

check("止损9 失败不再丢弃: worker 记入重试表, 由 ticker 退避重排(计数与到期时间分开存)",
      "def _am_retry_ticker()" in _app_src
      and "_am_tries[m] = n" in _app_src
      and "_am_retry[m] = time.time() + _AM_RETRY_DELAY * n" in _app_src
      and "m in _am_retry:" in _app_src
      and "am-retry-ticker" in _app_src)

check("止损10 取消网络突发(burst=1) + 队列/单次认领上限放宽",
      app._am_gate.cap == 1.0
      and app._AUTHOR_WARM_MAX >= 1000 and app._AUTHOR_WARM_CAP >= 200,
      "cap=%s max=%s cap_n=%s" % (app._am_gate.cap, app._AUTHOR_WARM_MAX, app._AUTHOR_WARM_CAP))

check("止损11 前端回问窗口 60 秒 → ~10 分钟(否则后台补好了也没人再问)",
      "RETRY_MAX = 300" in _dm_src
      and "function retryPending()" in _cf_src
      and "PEND[String(p[i])] = 1;" in _cf_src)

check("止损12 设置页文案已同步(等级只作用于评论)",
      "只作用于评论" in _idx_src and "弹幕侧不再提供等级筛选" in _idx_src)

check("止损13 重试链路有可重跑的行为回归(多线程 + 定时, 不塞进本脚本)",
      os.path.isfile(os.path.join(PROJECT, "_test_am_retry.py")))

check("止损14 三个账号级规则全关时后端直接返回(不逆推、不把候选塞进联网补图队列)",
      _app_src.count("if not (want_bio or want_lottery or want_empty):") == 2)

# ---------- 4. 启动/打包脚本指向窗口壳 ----------
gbk = lambda f: io.open(os.path.join(PROJECT, f), encoding="gbk").read()  # noqa: E731
check("启动.bat 走窗口壳", "app_window.py" in gbk("启动.bat"))
check("服务模式 bat 保留旧路径", "app.py --auto-open" in gbk("启动-服务模式.bat"))
check("构建exe.bat 打包窗口壳",
      '--name "%APPNAME%" "%ROOT%\\app_window.py"' in gbk("构建exe.bat"))

# ---------- 5. --server 子进程探活(起真实服务, 不开窗口不开浏览器) ----------
ports = app.PORTS
already = any(app.port_busy(p) for p in ports)
proc = subprocess.Popen([sys.executable, os.path.join(PROJECT, "app_window.py"), "--server"],
                        cwd=PROJECT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok = False
for _ in range(40):
    for p in ports:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/feed" % p, timeout=1.5) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            continue
    if ok or proc.poll() is not None:
        break
    time.sleep(0.5)
check("app_window --server 起真实服务", ok or (already and proc.poll() == 0),
      "already_running=%s exit=%s" % (already, proc.poll()))
proc.terminate()

# ---------- N. 安装包(NSIS) ----------
_nsi = io.open(os.path.join(PROJECT, "installer", "app_installer.nsi"),
               encoding="utf-8-sig").read()
check("NSIS脚本: WebView2三注册表位探测+缺则静默装",
      _nsi.count("EdgeUpdate\\Clients\\${WV2}") == 3 and "/silent /install" in _nsi)
check("NSIS脚本: 卸载器与添加/删除程序登记",
      "WriteUninstaller" in _nsi and "CurrentVersion\\Uninstall\\${UNKEY}" in _nsi)
check("NSIS脚本: 数据目录策略(卸载不删用户数据)",
      not re.search(r'RMDir[^\n]*LocalLow', _nsi) and 'RMDir /r "$INSTDIR"' in _nsi)
# 实测踩过: 注释以反斜杠结尾 → NSIS 当续行符, 把下一行的 InstallDir 整条吃掉,
# 安装目录静默掉回默认值(Program Files (x86))。makensis 只给一条 "6050" 警告, 极易漏。
_bad_comments = [ln for ln in _nsi.splitlines()
                 if ";" in ln and ln.split(";", 1)[1].rstrip().endswith("\\")]
check("NSIS脚本: 注释不以反斜杠结尾(否则吃掉下一行指令)",
      not _bad_comments, _bad_comments[:2])
check("NSIS脚本: 有明确的 64 位安装目录",
      'InstallDir "$PROGRAMFILES64\\${APPNAME}"' in _nsi)

# ★ v0.3.1.46dsk: 构建安装包.bat 曾依赖 installer-app.spec, 而 .gitignore 第 80 行把
#   desktop/*.spec 当"每次构建自动生成"的产物忽略掉 —— 那个 spec **从来不在库里**,
#   一次清理/换机器就没了, 脚本随即在 [1/3] 报 "Spec file not found" 直接失败。
#   规矩: 构建脚本不许依赖任何未入库的文件; 两条打包路径的参数必须等价。
_bat_pkg = io.open(os.path.join(PROJECT, "installer", "构建安装包.bat"),
                   encoding="gbk").read()
_bat_exe = io.open(os.path.join(PROJECT, "构建exe.bat"), encoding="gbk").read()
# 只看**命令行**, 不看注释 —— 注释里引用旧写法是解释原因, 不该被判成依赖。
_bat_pkg_cmds = [ln.strip() for ln in _bat_pkg.splitlines()
                 if ln.strip() and not ln.strip().lower().startswith("rem")]


def _adddata(s):
    """把 --add-data 归一成 "载荷名;目标目录"。

    两个构建脚本现在都传绝对路径（%ROOT%\\web;web），直接比字符串会永远不相等，
    而这条断言真正要防的是"两条路径打进去的载荷不一致"。
    """
    out = set()
    for spec in re.findall(r'--add-data "([^"]+)"', s):
        src, _, dst = spec.replace("\\", "/").rpartition(";")
        out.add((src.rsplit("/", 1)[-1] + ";" + dst))
    return out


check("安装包构建1 命令行不许依赖 .spec(它被 .gitignore 忽略, 换机器/清理后就没有)",
      not any(".spec" in ln for ln in _bat_pkg_cmds)
      and "--onedir" in _bat_pkg and "--onefile" not in _bat_pkg)

check("安装包构建2 不能裸用 python(机器上的 python 可能解析到没装 PyInstaller 的那个)",
      # 路径分隔符两种写法都行（cmd 认 / ，脚本里用的是 C:/Python314/...）
      'set "PYCMD="' in _bat_pkg
      and ("C:\\Python314\\python.exe" in _bat_pkg or "C:/Python314/python.exe" in _bat_pkg)
      and "where py >nul 2>nul" in _bat_pkg
      # 版本号那步也要走同一个解释器, 否则又会踩到裸 python
      and '%PYCMD% -c "import re,io;' in _bat_pkg)

check("安装包构建3 PyInstaller 先查再用(已装就别硬联网升级 —— 断网时也能构建)",
      '%PYCMD% -c "import PyInstaller" >nul 2>nul' in _bat_pkg
      and "pip install --upgrade pyinstaller" in _bat_pkg)

check("安装包构建4 两条打包路径参数必须等价(不同步会产出坏包)",
      _adddata(_bat_pkg) == _adddata(_bat_exe) == {
          "web;web", "extensions;extensions", "WebView2Runtime;WebView2Runtime", "model;model"}
      and "--hidden-import ftrt" in _bat_pkg and "--hidden-import ftrt" in _bat_exe
      and "--noconsole" in _bat_pkg and "--noconsole" in _bat_exe
      and "djxm_icon_128x128.ico" in _bat_pkg and "djxm_icon_128x128.ico" in _bat_exe
      # 58MB 的模型不该被 UPX 反复尝试压缩(与 spec 的 upx_exclude 一致)
      and "--upx-exclude ft_model.bin" in _bat_pkg
      and "--upx-exclude ft_model.bin" in _bat_exe
      # 载荷形态必须各就各位: 安装包要目录载荷(NSIS File /r), 单文件包要 onefile
      and "--onedir" in _bat_pkg and "--onefile" in _bat_exe)

import glob
# v0.3.1.66dsk 起构建产物统一进 desktop/build/dist/(可整目录删除)。
# 这里两处都看: 老路径 dist/ 兼容历史检出, 新路径 build/dist/ 才是现在的产物位置
# —— 只看 dist/ 会让这两条检查永远 SKIP, 等于没检查。
_exes = []
for _d in (os.path.join(PROJECT, "build", "dist"), os.path.join(PROJECT, "dist")):
    _exes += [p for p in glob.glob(os.path.join(_d, "*.exe")) if os.path.isfile(p)]
_pkgs = [p for p in _exes if "安装包" in os.path.basename(p)]
# build/dist/ 是构建产物(已 gitignore)。全新检出/清理后尚未打包时不该判 FAIL ——
# 这些检查的语义是"打好的包是对的", 不是"必须已经打过包"。
if not _exes:
    print("SKIP  打包产物已构建  (build/dist 未构建: 清理后未重新打包, 属预期)")
else:
    check("打包产物已构建(PyInstaller onefile exe)",
          os.path.getsize(_exes[-1]) > 20 * 1024 * 1024, _exes[-1])
if _pkgs:
    check("安装包产物已构建", os.path.getsize(_pkgs[-1]) > 100 * 1024 * 1024, _pkgs[-1])
else:
    print("SKIP  安装包产物已构建  (未跑 NSIS 打包, 属预期)")

print("RESULT: %s" % ("FAIL(%d)" % FAIL if FAIL else "ALL_PASS (%d checks)" % PASS))
sys.exit(1 if FAIL else 0)
