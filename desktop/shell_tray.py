# -*- coding: utf-8 -*-
"""系统托盘(shell_tray): 「关闭后隐藏到托盘」+ 托盘菜单 + 托盘图标。

为什么要单独一层: 这里只跟 WinForms 的 NotifyIcon / FormClosing 打交道, 与标签栏、控件池
无关。托盘装不上时整层降级(没有图标、关窗回到直接退出), 其余功能一点不受影响。

三条必须遵守的实现约束(都是踩过/已知的坑):
  · NotifyIcon 必须建在**有消息泵的 UI 线程**(pywebview 的 WinForms 主线程)。在 gui_ready
    的后台线程里 new 出来的图标看得见、点不动, 右键菜单也弹不出来 —— 一律 form.Invoke 回
    UI 线程建。
  · 「关闭」的拦截点是 FormClosing + `e.Cancel = True`。自动更新替换 exe、托盘菜单「退出」
    都必须能绕过它, 否则程序再也退不掉(exe 也换不掉) —— 所以留 allow_close()。
  · 图标不要直接 `Icon(路径)`: 随包 web/favicon.ico 是单帧 PNG 压缩, .NET 解不了(会抛
    越界)。这里直接复用 shell_tabstrip.load_app_logo(它已经按 ICO 目录项取 PNG 字节),
    再转成 HICON。
"""
import sys
import threading
import time

import app

TIP_TITLE = "洞鉴虚明"


def _log(msg):
    try:
        print("[tray] %s" % msg, flush=True)
    except Exception:
        pass


def _tray_icon():
    """托盘图标 → (System.Drawing.Icon, hicon 或 None)。

    拿不到就退回系统默认图标: 宁可丑, 也绝不能没有托盘入口 —— 窗口一藏, 没有图标
    用户就再也叫不回来, 只能去任务管理器杀进程。
    """
    hicon = None
    try:
        import clr
        clr.AddReference("System.Drawing")
        import System.Drawing as Drawing
        from shell_tabstrip import load_app_logo
        bmp = load_app_logo(32)
        if bmp is not None:
            hicon = bmp.GetHicon()
            return Drawing.Icon.FromHandle(hicon), hicon
    except Exception:
        _log("icon: %r" % (sys.exc_info()[1],))
    try:
        import System.Drawing as Drawing
        return Drawing.SystemIcons.Application, hicon
    except Exception:
        return None, hicon


class Tray:
    """托盘控制器。install() 之后可以 show_window / hide_window / allow_close。"""

    def __init__(self, win, api, start_hidden=False):
        self.win = win
        self.api = api
        self.start_hidden = bool(start_hidden)
        self._form = None
        self._icon = None
        self._hicon = None
        self._menu = None
        self._quitting = False
        self._hidden = False
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- UI 线程 marshal
    def _ui(self, fn):
        form = self._form
        if form is None:
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
            return
        import System.Windows.Forms as WinForms

        def go():
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
        try:
            if form.IsDisposed:
                return
            if form.InvokeRequired:
                form.BeginInvoke(WinForms.MethodInvoker(go))
            else:
                go()
        except Exception:
            import traceback
            traceback.print_exc()

    # ---------------------------------------------------------------- 安装
    def install(self):
        end = time.time() + 30
        form = None
        while time.time() < end:
            f = getattr(self.win, "native", None)
            if f is not None and f.IsHandleCreated:
                form = f
                break
            time.sleep(0.2)
        if form is None:
            raise RuntimeError("native window form not ready")
        self._form = form
        self._create()
        if self.start_hidden:
            self._ui(self._hide_silent)
        return self

    def _create(self):
        def go():
            import System.Windows.Forms as WinForms
            ic, hicon = _tray_icon()
            n = WinForms.NotifyIcon()
            if ic is not None:
                n.Icon = ic
            try:
                n.Text = "%s %s" % (TIP_TITLE, app.APP_VER)
            except Exception:
                n.Text = TIP_TITLE
            menu = WinForms.ContextMenuStrip()
            for label, fn in (("打开洞鉴虚明", lambda: self.show_window()),
                              ("换一批信息流", lambda: self.show_window(refresh=True)),
                              (None, None),          # 分隔线
                              ("退出", self.quit)):
                if label is None:
                    menu.Items.Add(WinForms.ToolStripSeparator())
                    continue
                it = WinForms.ToolStripMenuItem(label)
                it.Click += lambda _s, _e, f=fn: self._ui(f)
                menu.Items.Add(it)
            n.ContextMenuStrip = menu
            n.MouseClick += self._on_click
            self._icon, self._hicon, self._menu = n, hicon, menu
            n.Visible = True
            form = self._form
            if form is not None:
                form.FormClosing += self._on_closing
        self._ui(go)

    # ---------------------------------------------------------------- 事件
    def _on_click(self, _sender, e):
        """左键单击托盘图标 = 打开窗口(托盘图标最自然的期待); 右键交给菜单。"""
        try:
            btn = int(getattr(e, "Button", -1))
        except Exception:
            return
        if btn == 1048576:          # MouseButtons.Left == 0x100000
            self._ui(self.show_window)

    def _on_closing(self, _sender, e):
        """点窗口关闭: 开启「关闭后隐藏到托盘」时改成缩进托盘, 不退出。"""
        if self._quitting:
            return
        try:
            if app.sget("ui_close_to_tray") != "1":
                return
        except Exception:
            return
        try:
            e.Cancel = True     # 真的关了就退了, 后台那批内容也就白准备了
        except Exception:
            pass
        self.hide_window()

    # ---------------------------------------------------------------- 显隐
    def _hide_silent(self):
        """托盘态启动: 收起窗口(开机自启不该在用户面前弹一个窗口)。"""
        form = self._form
        if form is None or form.IsDisposed:
            return
        try:
            form.Hide()
        except Exception:
            pass
        self._hidden = True

    def hide_window(self):
        """缩进托盘 + 立刻让后端换一批(下次打开不是同一屏)。"""
        self._ui(self._hide_silent)
        self._tip_once()
        self._rotate()

    def show_window(self, refresh=None):
        """从托盘回到前台。

        refresh=None: 只有"本来藏着"才换一屏(用户刚点了「换一批」以外的入口);
        refresh=True : 一定要换一屏(托盘菜单「换一批信息流」)。
        """
        was_hidden = self._hidden

        def go():
            form = self._form
            if form is None or form.IsDisposed:
                return
            import System.Windows.Forms as WinForms
            try:
                if form.WindowState == WinForms.FormWindowState.Minimized:
                    form.WindowState = WinForms.FormWindowState.Normal
                form.Show()
                form.Activate()
                form.BringToFront()
            except Exception:
                pass
            self._hidden = False
        self._ui(go)
        do_refresh = (refresh is True) or (refresh is None and was_hidden)
        threading.Thread(target=self._after_show, args=(do_refresh,),
                         daemon=True, name="msx-tray-show").start()

    def _after_show(self, do_refresh):
        """显示之后补两件事(都在后台线程, 不卡 UI):

        ① 控件补呈现 —— 窗口隐藏久了, WebView2 的合成器会停, 直接 Show 可能是一片旧画面;
        ② 信息流换一屏 —— 走与顶栏刷新按钮**同一条路**, 用户看到的就是新抓的那批。
        """
        time.sleep(0.35)
        pages = getattr(self.api, "_pages", None)
        try:
            tid = self.api._active_id()
        except Exception:
            tid = None
        if pages is None or tid is None:
            return
        try:
            pages.show(tid)                     # 让当前标签控件可见 + 拿到焦点
            pages.rescue(tid, "tray-show")      # 补一次呈现(不会重载页面)
        except Exception:
            pass
        if do_refresh and tid == 0:             # 0 号才是信息流(网页标签别去动)
            try:
                pages.exec_js(0, "window.__msxRefreshFeed && window.__msxRefreshFeed()",
                              timeout=4.0)
            except Exception:
                pass

    # ---------------------------------------------------------------- 其它
    def _rotate(self):
        """换一批: 关窗 / 缩托盘时就让后端去抓新内容, 下次打开不是同一屏。"""
        def go():
            try:
                app.feed_rotate(wait=0.0)
            except Exception:
                pass
        threading.Thread(target=go, daemon=True, name="msx-tray-rotate").start()

    def _tip_once(self):
        """第一次缩进托盘时提示一句。托盘图标不显眼, 不提示用户会以为程序已经退出了。"""
        try:
            if app.sget("ui_tray_tipped") == "1":
                return
        except Exception:
            return

        def go():
            try:
                import System.Windows.Forms as WinForms
                self._icon.ShowBalloonTip(
                    4000, "洞鉴虚明还在后台跑",
                    "点托盘图标回到窗口；要真正退出请右键托盘图标 → 退出。",
                    WinForms.ToolTipIcon.Info)
            except Exception:
                pass
        self._ui(go)
        try:
            app.sset("ui_tray_tipped", "1")
        except Exception:
            pass

    def notify(self, title, text):
        """气泡提示(菜单「换一批」这类后台动作给个回执)。"""
        def go():
            try:
                import System.Windows.Forms as WinForms
                self._icon.ShowBalloonTip(2500, title, text, WinForms.ToolTipIcon.Info)
            except Exception:
                pass
        self._ui(go)

    def allow_close(self):
        """让窗口这次真的能关。

        自动更新替换 exe 必须走这里: 不解除的话 form.Close() 会被「关闭到托盘」拦成 Hide,
        进程一直活着、exe 一直换不掉(替换脚本等 3 分钟然后失败)。
        """
        self._quitting = True

    def quit(self):
        """托盘菜单「退出」: 解除关闭拦截 → 关窗。

        换一批与停服务不在这里做 —— 关窗后 shell_main 会统一收尾(feed_rotate + shutdown),
        保证"点 ✕ 退出"和"托盘退出"走的是同一条收尾路径。
        """
        self.allow_close()
        self.dispose()

        def go():
            try:
                form = self._form
                if form is not None and not form.IsDisposed:
                    form.Close()
                else:
                    self.win.destroy()
            except Exception:
                pass
        self._ui(go)

    def dispose(self):
        def go():
            try:
                if self._icon is not None:
                    self._icon.Visible = False
                    self._icon.Dispose()
            except Exception:
                pass
            try:
                h = self._hicon
                if h is not None:
                    try:
                        h = h.ToInt64()
                    except Exception:
                        h = int(h)
                    import ctypes
                    ctypes.windll.user32.DestroyIcon(int(h))
            except Exception:
                pass
            self._icon = None
            self._hicon = None
        # 窗体已经销毁时 _ui 会直接返回, 图标就摘不掉了 —— 而"窗口关了图标还在"
        # 正是用户会以为程序没退的那种状态。窗口没了就直接在调用线程做。
        form = self._form
        if form is None or form.IsDisposed:
            go()
        else:
            self._ui(go)


def install_tray(win, api, start_hidden=False):
    """装好托盘并返回控制器(失败返回 None, 壳其余功能不受影响)。"""
    try:
        return Tray(win, api, start_hidden=start_hidden).install()
    except Exception:
        import traceback
        traceback.print_exc()
        return None
