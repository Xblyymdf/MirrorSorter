# -*- coding: utf-8 -*-
"""窗口顶部原生"浏览器式"标签栏(WinForms 自绘, 仅供 app_window.py 窗口壳使用)。

原理: pywebview 的 WinForms 窗体(window.native)里,
- 标签栏 = Dock=Top 的 Panel, Paint 事件自绘标签(圆角/标题/✕), MouseClick 命中测试;
- WebView2 控件从 Dock=Fill 改为手动定位在标签栏下方(Anchor 四边), 窗口缩放自动跟随。
标签栏属于窗口 chrome, 不向页面注入任何元素 —— 任何网站都不被遮挡、行为不变。
窗口为无边框(frameless): 本栏兼任标题栏 —— 空白处可拖动窗口/双击最大化,
右端有 ─ □ ✕ 窗口按钮; 无边框缩放由 shell_pages.init 的 WS_THICKFRAME 补丁恢复。

注意: pythonnet 对 .NET 虚方法重写(OnPaint 等)不可靠, 必须用 Paint/MouseClick 事件订阅。
"""
import re
import threading


# ---------------------------------------------------------------- 撕出悬浮窗的拖拽判定
# ★ 提到模块级是为了**可单测**。旧版把判据内联在 MouseMove 里:
#     abs(dx) + abs(dy) > 22
#   —— 任意方向的曼哈顿距离, 于是"点一下标签"手抖二十来像素就被拽成悬浮窗(用户反馈);
#   而"往下拽"因为标签栏只有 ~32px 高、拖出去立刻触发 MouseLeave 被取消, 反而永远
#   拽不出来。方向语义整个是反的, 而内联写法在无 GUI 环境下没法回归测试。
DETACH_DY = 26     # 向下拖出的阈值(逻辑像素 × scale); 标签栏高 ≈ 32 × scale
HINT_DY = 8        # 到这个位移先给视觉提示(还没到撕出阈值)


def detach_verdict(dx, dy, scale=1.0):
    """按住可关闭标签时的位移 → 处置。

    返回 "detach"(撕出为悬浮窗) / "hint"(只给视觉提示) / ""(什么也不做)。

    判据: **只认向下**的拖拽 —— dy > 0 且 dy > |dx|(明显偏纵向)。
    横向拖 / 往上拖 / 原地手抖一律不撕出。
    """
    if dy <= 0 or dy <= abs(dx):
        return ""
    if dy >= DETACH_DY * scale:
        return "detach"
    if dy >= HINT_DY * scale:
        return "hint"
    return ""


def install_strip(win, api, bg_rgb=(15, 17, 21), accent_getter=None, bg_getter=None):
    """在窗口顶部安装原生标签栏并下移 WebView2 内容区。须在 GUI 就绪后调用(GUI 线程之外
    调用安全, 内部已 marshal)。返回 update(tabs, active) —— 传入
    [{"id":..,"title":..,"closable":..}] 与激活 id 即重绘。"""
    import clr
    clr.AddReference("System.Drawing")
    from System.Drawing import (Color, ContentAlignment, Font, Pen, Rectangle,
                                SolidBrush, StringFormat, StringAlignment,
                                StringFormatFlags, StringTrimming, RectangleF,
                                Point, Size)
    from System.Drawing.Drawing2D import GraphicsPath, SmoothingMode
    import System.Windows.Forms as WinForms

    form = win.native
    scale = float(getattr(form, "_scale", 1.0) or 1.0)
    bar_h = max(28, int(32 * scale))
    tab_h = bar_h - 8
    font = Font("Microsoft YaHei", 8.0 * scale)
    btn_font = Font("Microsoft YaHei", 10.0 * scale)   # 按钮符号=1.25倍字号(─□✕/标签✕), 标题不动
    TAB_ACTIVE = Color.FromArgb(22, 25, 32)     # 激活标签(=应用 --panel)
    TAB_HOVER = Color.FromArgb(30, 34, 44)      # 非激活悬停
    BG = Color.FromArgb(10, 12, 16)             # 标签栏底色(比窗口更深一档)
    FG_ON = Color.White
    FG_OFF = Color.FromArgb(150, 158, 172)
    st = {"tabs": [], "active": 0, "hover_btn": None, "hover_tab": None,
          "hover_add": False,   # 「+」新建按钮悬停
          "drop_hi": False,   # 悬浮窗被拖到标签栏上方 → 整条高亮(提示"松手即合并成标签页")
          "accent": Color.FromArgb(251, 114, 153),   # 默认主题粉; accent_watch 会用设置文件的强调色覆盖
          "bar": Color.FromArgb(10, 12, 16), "tab_active": Color.FromArgb(22, 25, 32),
          "tab_hover": Color.FromArgb(30, 34, 44), "fg_on": Color.FromArgb(226, 230, 238),
          "fg_off": Color.FromArgb(150, 158, 172), "sep": Color.FromArgb(52, 57, 68)}
    # v0.3.1.44 撕出悬浮窗的拖拽可视态: 正在被拖的标签 id / 是否已进入"向下拖"提示
    st["drag_tid"] = None
    st["drag_hint"] = False
    lock = threading.Lock()
    form = win.native

    strip = WinForms.Panel()
    strip.DoubleBuffered = True
    strip.Height = bar_h
    strip.Dock = WinForms.DockStyle.Top
    strip.BackColor = BG
    strip.Cursor = WinForms.Cursors.Default

    sf = StringFormat()
    sf.Alignment = StringAlignment.Near
    sf.LineAlignment = StringAlignment.Center
    sf.FormatFlags = StringFormatFlags.NoWrap
    sf.Trimming = StringTrimming.EllipsisCharacter
    sf_c = StringFormat()   # 符号居中(✕/─□✕): 字形再宽也不会越出保留区被裁
    sf_c.Alignment = StringAlignment.Center
    sf_c.LineAlignment = StringAlignment.Center

    ADD_W = int(24 * scale)   # 标签条右端的「+」新建按钮

    def _layout(width):
        n = len(st["tabs"])
        if not n:
            return []
        gap, margin = int(4 * scale), int(6 * scale)
        btn_zone = BTN_W() * 3 + int(2 * scale)
        # 「+」占一格(含与前一个标签的间距), 从可用宽度里扣掉
        slack = width - margin - btn_zone - gap - ADD_W - gap * (n - 1)
        # 挤不下时收窄标签(浏览器习惯), 不能硬保下限: 旧下限110会把总宽顶出栏外,
        # 最右标签被面板右缘裁掉 → ✕只剩半个(用户截图)。60(逻辑)保住✕+点击区完整。
        tw = min(int(180 * scale), max(int(60 * scale), slack // n))
        out, x = [], margin
        for _ in range(n):
            out.append((x, tw))
            x += tw + gap
        return out

    def _add_rect():
        """「+」按钮矩形 —— 紧贴最后一个标签右侧; 标签多到挤满时钉在窗口按键左侧。"""
        boxes = _layout(strip.Width)
        x = (boxes[-1][0] + boxes[-1][1] + int(4 * scale)) if boxes else int(6 * scale)
        limit = strip.Width - BTN_W() * 3 - ADD_W - int(4 * scale)
        x = max(int(2 * scale), min(x, limit))
        return (x, int(3 * scale), ADD_W, bar_h - int(6 * scale))

    def _hit_add(x, yv):
        ax, ay, aw, ah = _add_rect()
        return ax <= x <= ax + aw and ay <= yv <= ay + ah

    def _rounded_path(x, y, w, h, rad):
        p = GraphicsPath()
        d = rad * 2
        p.AddArc(x, y, d, d, 180, 90)             # 左上
        p.AddArc(x + w - d, y, d, d, 270, 90)     # 右上
        p.AddArc(x + w - d, y + h - d, d, d, 0, 90)    # 右下
        p.AddArc(x, y + h - d, d, d, 90, 90)      # 左下
        p.CloseFigure()
        return p

    def on_paint(_sender, e):
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        g.Clear(st["bar"])
        boxes = _layout(strip.Width)
        rad = int(5 * scale)
        ty, th = int(3 * scale), bar_h - int(6 * scale)
        last_right = None
        for i, t in enumerate(st["tabs"]):
            if i >= len(boxes):
                break
            x, tw = boxes[i]
            act = (t["id"] == st["active"])
            hov = (st["hover_tab"] == t["id"] and not act)
            if not act and last_right is not None:   # 非激活标签之间的细分隔线
                g.DrawLine(Pen(st["sep"], 1.0),
                           float(x - int(2 * scale)), float(ty + th * 0.25),
                           float(x - int(2 * scale)), float(ty + th * 0.75))
            g.FillPath(SolidBrush(st["tab_active"] if act else (st["tab_hover"] if hov else st["bar"])),
                       _rounded_path(x, ty, tw, th, rad))
            # v0.3.1.44: 正在往下拖这个标签 → 染强调色, 让用户看得见"再往下就是悬浮窗"
            # (旧版一点就撕出, 用户根本不知道有拖拽手势; 现在手势变严了, 必须给反馈)
            if st.get("drag_hint") and st.get("drag_tid") == t["id"]:
                try:
                    ac = st["accent"]
                    g.FillPath(SolidBrush(Color.FromArgb(64, ac.R, ac.G, ac.B)),
                               _rounded_path(x, ty, tw, th, rad))
                    g.DrawPath(Pen(ac, max(1.0, 2.0 * scale)),
                               _rounded_path(x, ty, tw, th, rad))
                except Exception:
                    pass
            fg = st["fg_on"] if act else st["fg_off"]
            close_w = int(20 * scale) if t.get("closable") else int(6 * scale)
            rect = RectangleF(float(x + int(8 * scale)), float(ty),
                              float(tw - close_w - int(8 * scale)), float(th))
            g.DrawString(t.get("title") or "页面", font, SolidBrush(fg), rect, sf)
            if t.get("closable"):
                cx = x + tw - close_w
                g.DrawString("✕", btn_font, SolidBrush(fg),
                             RectangleF(float(cx), float(ty), float(close_w), float(th)), sf_c)
            last_right = x + tw
        # 「+」新建标签按钮(浏览器式): 悬停给底色, 加号本身不变形
        ax, ay, aw, ah = _add_rect()
        if st.get("hover_add"):
            g.FillPath(SolidBrush(st["tab_hover"]),
                       _rounded_path(ax, ty, aw, th, rad))
        g.DrawString("+", btn_font,
                     SolidBrush(st["fg_on"] if st.get("hover_add") else st["fg_off"]),
                     RectangleF(float(ax), float(ty), float(aw), float(th)), sf_c)
        # 右端窗口按钮: ─ □ ✕ —— 白色符号 + 分隔线
        # (Pen 必须用顶部 import: 函数中段 import 会让前面的分隔线绘制碰到未绑定局部名,
        #  "点回信息流(激活标签后有非激活标签)"的重绘即爆 UnboundLocalError 整窗异常)
        bw = BTN_W()
        zx = strip.Width - bw * 3
        g.DrawLine(Pen(st["sep"], 1.0),
                   float(zx), float(int(4 * scale)), float(zx), float(bar_h - int(4 * scale)))
        for i, (name, sym) in enumerate((("min", "─"), ("max", "□"), ("close", "✕"))):
            bx = zx + bw * i
            hov = (st["hover_btn"] == name)
            if name == "close" and hov:
                g.FillRectangle(SolidBrush(Color.FromArgb(232, 17, 35)),
                                float(bx), 0.0, float(bw), float(bar_h))
            elif hov:
                g.FillRectangle(SolidBrush(st["tab_hover"]),
                                float(bx), 0.0, float(bw), float(bar_h))
            if name == "max" and (form.WindowState == WinForms.FormWindowState.Maximized
                                  or fill_state["saved"] is not None):
                # 还原态图标: 两方块右上角部分重叠(前块左下完整, 后块右上被遮)
                fg = st["fg_on"]
                s = max(6, int(9 * scale))
                off = max(2, int(3 * scale))
                px = int(bx + (bw - s - off) / 2.0)
                py = int((bar_h - s - off) / 2.0)
                pen = Pen(fg, max(1.0, 1.4 * scale))
                g.DrawRectangle(pen, px + off, py, s, s)              # 后块(右上)
                g.FillRectangle(SolidBrush(st["tab_hover"] if hov else st["bar"]),
                                px - 1, py + off - 1, s + 2, s + 2)    # 遮后块与前块重叠的线
                g.DrawRectangle(pen, px, py + off, s, s)              # 前块(左下)
            else:
                g.DrawString(sym, btn_font, SolidBrush(st["fg_on"]),
                             RectangleF(float(bx), 0.0, float(bw), float(bar_h)), sf_c)
        # 悬浮窗拖到标签栏上方 → 整条染色 + 强调色描边(松手即合并成标签页)
        if st.get("drop_hi"):
            try:
                ac = st["accent"]
                g.FillRectangle(SolidBrush(Color.FromArgb(40, ac.R, ac.G, ac.B)),
                                0.0, 0.0, float(strip.Width), float(bar_h))
                g.DrawRectangle(Pen(ac, max(1.0, 2.0 * scale)),
                                1.0, 1.0, float(strip.Width - 2), float(bar_h - 2))
            except Exception:
                pass

    BTN_W = lambda: int(36 * scale)   # ─ □ ✕ 窗口按钮宽

    def _btn_zone(x):
        return x >= strip.Width - BTN_W() * 3

    def _btn_at(x):
        """返回 0=─ 1=□ 2=✕ 或 None。"""
        if not _btn_zone(x):
            return None
        i = (x - (strip.Width - BTN_W() * 3)) // BTN_W()
        return ("min", "max", "close")[int(i)]

    # ---- 窗口化全屏: 拉伸窗口(还原态)铺满屏幕, 不进系统最大化状态 ----
    # 普通窗口永远不会被 shell 判成"独占全屏", 自动隐藏任务栏照常呼出。
    # 矩形向下多出2px(屏幕外不可见), 与屏幕矩形错开双保险; 常驻任务栏机器铺工作区。
    fill_state = {"saved": None}   # 非None = 处于拉伸全屏态, 存还原位置
    # bug6: 网页(视频)全屏中 —— 标签栏/贴边热区隐藏, 画面铺满整窗
    fs_state = {"on": False}
    # bug10: 贴边热区面板固定用窗体底色(不再是标签栏底色)。它们压在页面边缘 4px 上,
    # 白天/浅色主题下原先会跟着标签栏变浅 → 播放页四周一圈浅色边框(用户报"白色边框,
    # 窗口好像可被选中")。取窗体 BackColor(init_ui: 15,17,21), 与控件缝隙同色不可见。
    EDGE_COLOR = Color.FromArgb(15, 17, 21)

    def _fill_bounds():
        scr = WinForms.Screen.FromControl(form)
        mon, wa = scr.Bounds, scr.WorkingArea
        if (wa.X == mon.X and wa.Y == mon.Y
                and wa.Width == mon.Width and wa.Height == mon.Height):
            # 自动隐藏任务栏: 实测(SHQueryUserNotificationState)窗口把屏幕盖满——
            # 哪怕矩形超出屏幕——就会被判"独占全屏"压住任务栏; 必须留至少1px不盖。
            # 底部留1px(任务栏弹出时会盖住, 基本不可见)。
            return Rectangle(mon.X, mon.Y, mon.Width, mon.Height - 1)
        return wa   # 常驻任务栏: 铺到任务栏上缘为止

    edge_panels = []   # 贴边缩放热区(_install 创建, sync_edges 按窗口状态显隐)

    def sync_edges():
        try:
            vis = (form.WindowState != WinForms.FormWindowState.Maximized
                   and fill_state["saved"] is None
                   and not fs_state["on"])   # bug6: 视频全屏时热区也要让开(否则左缘显缩放光标)
            for p in edge_panels:
                p.Visible = vis
        except Exception:
            pass

    def _toggle_max():
        try:
            if fill_state["saved"] is None:   # 进入窗口化全屏
                if form.WindowState == WinForms.FormWindowState.Maximized:
                    b = form.RestoreBounds    # 系统路径最大化过 → 回到之前的位置
                    form.WindowState = WinForms.FormWindowState.Normal
                    fill_state["saved"] = Rectangle(b.X, b.Y, b.Width, b.Height)
                else:
                    fill_state["saved"] = Rectangle(form.Bounds.X, form.Bounds.Y,
                                                    form.Bounds.Width, form.Bounds.Height)
                form.Bounds = _fill_bounds()
            else:                             # 退出: 回到进入前的位置
                b = fill_state["saved"]
                fill_state["saved"] = None
                form.WindowState = WinForms.FormWindowState.Normal
                form.Bounds = Rectangle(b.X, b.Y, b.Width, b.Height)
            sync_edges()
        except Exception:
            pass

    def sync_max_bounds(_s=None, _e=None):
        """兜底: 系统路径(Win+Up/拖到屏幕顶)仍会走真最大化, MaximizedBounds 跟随
        工作区/错开屏幕矩形, 避免盖住任务栏(详见 _fill_bounds 注释)。"""
        try:
            form.MaximizedBounds = _fill_bounds()
        except Exception:
            pass

    def _hit_tab(x, yv):
        boxes = _layout(strip.Width)
        for i, t in enumerate(st["tabs"]):
            if i >= len(boxes):
                break
            bx, tw = boxes[i]
            if bx <= x <= bx + tw and 0 <= yv <= bar_h:
                return st["tabs"][i]
        return None

    def _nc_drag(ht):
        """进入系统模态循环(移动 HTCAPTION / 缩放 HTLEFT...) —— 无边框窗口标准技巧。"""
        import ctypes
        try:
            u32 = ctypes.windll.user32
            u32.ReleaseCapture()
            u32.SendMessageW(form.Handle.ToInt32(), 0xA1, ht, 0)
        except Exception:
            pass

    def _open_add_menu():
        """「+」→ 标签栏下方的小选择框: 浏览器 / 副信息流。

        用无边框 Form 自绘(而不是 ContextMenuStrip): 后者的系统配色在深色主题下会
        露出一圈白边, 且没法跟随页面推来的标签栏配色。点外面 / Esc / 选中一项都关闭。
        """
        from System.Drawing import Point as SPoint, Size as SSize
        try:
            ow, oh = int(150 * scale), int(80 * scale)
            row_h = int(35 * scale)
            pop = WinForms.Form()
            pop.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
            pop.ShowInTaskbar = False
            pop.StartPosition = WinForms.FormStartPosition.Manual
            pop.BackColor = st["tab_active"]
            pop.Size = SSize(ow, oh)
            pop.KeyPreview = True

            def _close(_s=None, _e=None):
                try:
                    pop.Close()
                except Exception:
                    pass

            pop.Deactivate += _close
            pop.Paint += lambda _s, e: e.Graphics.DrawRectangle(
                Pen(st["sep"], 1.0), 0, 0, pop.Width - 1, pop.Height - 1)
            pop.KeyDown += lambda _s, e: _close() if int(e.KeyCode) == 27 else None

            for i, (label, kind) in enumerate((("浏览器", "browser"),
                                               ("副信息流", "feed"))):
                b = WinForms.Button()
                b.Text = label
                b.FlatStyle = WinForms.FlatStyle.Flat
                b.FlatAppearance.BorderSize = 0
                b.FlatAppearance.MouseOverBackColor = st["tab_hover"]
                b.FlatAppearance.MouseDownBackColor = st["tab_hover"]
                b.BackColor = st["tab_active"]
                b.ForeColor = st["fg_on"]
                b.Font = font
                b.Cursor = WinForms.Cursors.Hand
                b.TextAlign = ContentAlignment.MiddleLeft
                b.Padding = WinForms.Padding(int(10 * scale), 0, 0, 0)
                b.Location = SPoint(int(5 * scale),
                                    int(5 * scale) + i * row_h)
                b.Size = SSize(ow - int(10 * scale), row_h - int(2 * scale))

                def _pick(_s=None, _e=None, k=kind):
                    _close()
                    try:
                        api.tab_new(k)
                    except Exception:
                        pass
                b.Click += _pick
                pop.Controls.Add(b)

            ax, _ay, _aw, _ah = _add_rect()
            pt = strip.PointToScreen(SPoint(ax, bar_h))
            scr = WinForms.Screen.FromControl(form).WorkingArea
            x = max(scr.X, min(pt.X, scr.Right - ow))
            y = pt.Y
            if y + oh > scr.Bottom:
                y = max(scr.Y, pt.Y - bar_h - oh)
            pop.Location = SPoint(x, y)
            pop.Show(form)
            pop.Activate()
        except Exception:
            import traceback
            traceback.print_exc()

    # 拖出成悬浮窗(v0.3.1.34; 触发方式 v0.3.1.44 修正):
    #   判定在模块级 detach_verdict() 里(可单测), 这里只负责"按住/捕获/吞点击"。
    #   ★ 按住期间必须用鼠标捕获跟住指针 —— 往下拖会立刻离开这 32px 高的标签栏,
    #     不捕获就收不到后续 MouseMove, "往下拽"永远拽不出来。
    #   ★ 按下状态用**自己的** drag["down"] 记, 不查 Control.MouseButtons ——
    #     那是全局物理鼠标状态, 既不适合做局部手势判定, 也让这段逻辑没法脱离真鼠标测试。
    #   fired 标记用于吞掉随后的 MouseClick, 避免"拖出后顺手把标签切了/关了"。
    drag = {"tid": None, "x": 0, "y": 0, "armed": False, "down": False, "fired": False}

    def _drag_capture(on):
        """按住标签期间抓住鼠标 —— 往下拖必然离开这 30px 高的标签栏,
        不捕获就收不到后续 MouseMove, "往下拽"永远拽不出来。"""
        try:
            if bool(strip.Capture) != bool(on):
                strip.Capture = bool(on)
        except Exception:
            pass

    def _drag_visual(tid, hint):
        """把"正在被拖的标签"同步给绘制层(状态没变就不重绘)。"""
        if st.get("drag_tid") == tid and st.get("drag_hint") == bool(hint):
            return
        st["drag_tid"] = tid
        st["drag_hint"] = bool(hint)
        try:
            strip.Invalidate()
        except Exception:
            pass

    def on_mouse_down(_sender, args):
        if args.Button != WinForms.MouseButtons.Left:
            return
        drag["down"] = True
        # 每次左键按下都复位: 上一次"撕出后吞掉点击"的标记不该跨手势残留
        drag["fired"] = False
        if _btn_at(args.X) is not None:
            return
        if _hit_add(args.X, args.Y):
            return   # 「+」点击自己处理; 不能落到下面的窗口拖动
        t = _hit_tab(args.X, args.Y)
        if t is not None:
            if t.get("closable"):
                drag["tid"] = t["id"]
                drag["x"] = args.X
                drag["y"] = args.Y
                drag["armed"] = True
                _drag_capture(True)
            return
        if (args.Y <= int(4 * scale) and
                form.WindowState != WinForms.FormWindowState.Maximized):
            _nc_drag(12)   # HTTOP: 标签栏上缘 4px = 顶边缩放带
            return
        _nc_drag(2)   # HTCAPTION: 空白区拖动窗口

    def on_mouse_move(_sender, args):
        if drag["armed"]:
            if not drag["down"]:        # 按钮已松开却没收到 MouseUp(极罕见) → 复位
                drag["armed"] = False
                _drag_capture(False)
                _drag_visual(None, False)
            else:
                dx = args.X - drag["x"]
                dy = args.Y - drag["y"]
                v = detach_verdict(dx, dy, scale)
                if v == "detach":
                    drag["armed"] = False
                    drag["fired"] = True
                    _drag_visual(None, False)
                    tid = drag["tid"]
                    if tid is not None:
                        try:
                            api.tab_detach(tid)   # 标签 → 独立悬浮窗(控件实例不变)
                        except Exception:
                            pass
                    # ⚠️ 这里**不放开**鼠标捕获: 放开后 MouseUp 会落到 WebView2 上,
                    #    on_mouse_click 不触发 → fired 标记没人消费 → 下一次真点击被误吞。
                    #    统一交给 on_mouse_up 收尾。
                elif v == "hint":
                    _drag_visual(drag["tid"], True)
                else:
                    _drag_visual(drag["tid"], False)   # 横向/向上 → 明确不给提示
        name = _btn_at(args.X)
        if name != st["hover_btn"]:
            st["hover_btn"] = name
            strip.Invalidate()
        hov_add = _hit_add(args.X, args.Y)
        if hov_add != st["hover_add"]:
            st["hover_add"] = hov_add
            strip.Invalidate()
        ht = _hit_tab(args.X, args.Y)
        hid = ht["id"] if ht is not None else None
        if hid != st["hover_tab"]:
            st["hover_tab"] = hid
            strip.Invalidate()

    def on_mouse_up(_sender, args):
        """松手收尾: 放开鼠标捕获 + 清掉拖拽可视态。"""
        if args.Button != WinForms.MouseButtons.Left:
            return
        drag["down"] = False
        drag["armed"] = False
        _drag_capture(False)
        _drag_visual(None, False)

    def on_mouse_leave(_sender, _args):
        # ★ 按住左键时**不**取消 armed: 往下拖必然立刻离开这 30px 高的标签栏,
        #   而那正是"撕出悬浮窗"要的动作 —— 旧实现恰恰在这里 armed=False,
        #   所以往下拽永远拽不出来。真正的收尾走 on_mouse_up。
        if drag["armed"]:
            return
        ch = False
        if st["hover_btn"]:
            st["hover_btn"] = None
            ch = True
        if st["hover_tab"] is not None:
            st["hover_tab"] = None
            ch = True
        if st["hover_add"]:
            st["hover_add"] = False
            ch = True
        if ch:
            strip.Invalidate()

    def on_window_btn(name):
        import System.Windows.Forms as WinForms
        try:
            if name == "min":
                form.WindowState = WinForms.FormWindowState.Minimized
            elif name == "max":
                _toggle_max()
            elif name == "close":
                form.Close()
        except Exception:
            pass

    def on_mouse_click(_sender, args):
        if drag["fired"]:   # 刚拖出成悬浮窗 → 吞掉这次点击(不误切/误关)
            drag["fired"] = False
            return
        name = _btn_at(args.X)
        if name is not None:
            on_window_btn(name)
            return
        if _hit_add(args.X, args.Y):
            _open_add_menu()
            return
        t = _hit_tab(args.X, args.Y)
        if t is not None:
            boxes = _layout(strip.Width)
            for i, tt in enumerate(st["tabs"]):
                if tt is t and i < len(boxes):
                    x, tw = boxes[i]
                    if t.get("closable") and args.X > x + tw - int(24 * scale):
                        api.tab_close(t["id"])
                    else:
                        api.tab_switch(t["id"])
                    return

    def on_capture_changed(_sender, _args):
        """捕获被意外抢走(Alt+Tab / 系统弹窗 / 别的窗口抢焦点) → 拖拽状态必须复位。
        否则 armed 会永远停在 True, on_mouse_leave 一直提前返回 → 悬停高亮卡死。"""
        try:
            if strip.Capture:
                return
            if drag["armed"]:
                drag["armed"] = False
            _drag_visual(None, False)
        except Exception:
            pass

    strip.MouseDown += on_mouse_down
    strip.MouseMove += on_mouse_move
    strip.MouseUp += on_mouse_up
    strip.MouseLeave += on_mouse_leave
    strip.MouseCaptureChanged += on_capture_changed
    def set_accent(hex_str):
        try:
            h = str(hex_str or "").strip()
            if not re.match(r"^#[0-9a-fA-F]{6}$", h):
                return
            c = Color.FromArgb(int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))
            if st["accent"].ToArgb() != c.ToArgb():
                st["accent"] = c
                strip.Invalidate()
        except Exception:
            pass

    def set_bg_scheme(raw):
        """页面推送的标签栏配色方案(app.js bgStripScheme: 栏底/激活/悬停/文字/分隔线,
        深浅文字极性由页面按深度算好), 这里只解析上色; 贴边热区面板同步吃栏底色。"""
        try:
            import json as _json
            sch = _json.loads(raw) if isinstance(raw, str) and raw.strip() else None
            if not isinstance(sch, dict):
                return

            def _c(key, fallback):
                v = str(sch.get(key) or "")
                if re.match(r"^#[0-9a-fA-F]{6}$", v):
                    return Color.FromArgb(int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16))
                return fallback

            st["bar"] = _c("bar", st["bar"])
            st["tab_active"] = _c("active", st["tab_active"])
            st["tab_hover"] = _c("hover", st["tab_hover"])
            st["fg_on"] = _c("fgon", st["fg_on"])
            st["fg_off"] = _c("fgoff", st["fg_off"])
            st["sep"] = _c("sep", st["sep"])

            def apply():
                try:
                    strip.BackColor = st["bar"]
                    for p in edge_panels:
                        p.BackColor = EDGE_COLOR   # bug10: 不再跟随标签栏底色
                except Exception:
                    pass
                strip.Invalidate()

            if strip.InvokeRequired:
                strip.BeginInvoke(WinForms.MethodInvoker(apply))
            else:
                apply()
        except Exception:
            pass

    def accent_watch():
        import time as _t
        last = None
        last_bg = None
        while True:
            try:
                if accent_getter:
                    cur = accent_getter()
                    if cur != last:
                        last = cur
                        set_accent(cur)
                if bg_getter:
                    cur_bg = bg_getter()
                    if cur_bg != last_bg:
                        last_bg = cur_bg
                        set_bg_scheme(cur_bg)
            except Exception:
                pass
            _t.sleep(2)

    if accent_getter or bg_getter:
        threading.Thread(target=accent_watch, daemon=True, name="msx-accent").start()

    strip.Paint += on_paint
    strip.MouseClick += on_mouse_click
    strip.Resize += lambda _s, _e: strip.Invalidate()

    def _install():
        try:
            wv = form.webview
            wv.Dock = getattr(WinForms.DockStyle, "None")
            form.Controls.Add(strip)
            strip.BringToFront()
            # 内容区 = 标签栏 + 导航工具栏(shell_navbar) 之下; 高度唯一来源在
            # shell_navbar.nav_h(), 本模块只消费, 不得再写一份常量
            from shell_navbar import nav_h as _nav_h
            content_top = bar_h + _nav_h(scale)
            wv.Location = Point(0, content_top)
            wv.Size = Size(form.ClientSize.Width, form.ClientSize.Height - content_top)
            wv.Anchor = (WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Bottom |
                         WinForms.AnchorStyles.Left | WinForms.AnchorStyles.Right)

            # ---- 无边框边缘缩放热区: 客户区被 WebView2 全覆盖, THICKFRAME 的
            #      缩放带永远收不到鼠标 → 自己贴边放热区, 按下进入系统缩放循环 ----
            def edge(name, loc, size, anchor, cursor):
                p = WinForms.Panel()
                p.BackColor = EDGE_COLOR
                p.Location = loc
                p.Size = size
                p.Anchor = anchor
                p.Cursor = cursor
                ht = {"left": 10, "right": 11, "bottom": 15}[name]

                def down(_s, a):
                    if (a.Button == WinForms.MouseButtons.Left and
                            form.WindowState != WinForms.FormWindowState.Maximized):
                        _nc_drag(ht)
                p.MouseDown += down
                edge_panels.append(p)
                form.Controls.Add(p)
                p.BringToFront()
            ew = int(4 * scale)
            cw, ch = form.ClientSize.Width, form.ClientSize.Height
            # 右/下热区外扩 2px: 150% DPI 下逻辑坐标→物理像素有舍入差,
            # 精确贴边会露出窗体背景 1px 白线; 多覆盖的部分被窗口裁剪, 无副作用
            edge("left", Point(0, 0), Size(ew, ch),
                 WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Bottom |
                 WinForms.AnchorStyles.Left, WinForms.Cursors.SizeWE)
            edge("right", Point(cw - ew - 2, 0), Size(ew + 2, ch),
                 WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Bottom |
                 WinForms.AnchorStyles.Right, WinForms.Cursors.SizeWE)
            edge("bottom", Point(0, ch - ew - 2), Size(cw + 2, ew + 2),
                 WinForms.AnchorStyles.Bottom | WinForms.AnchorStyles.Left |
                 WinForms.AnchorStyles.Right, WinForms.Cursors.SizeNS)

            # 贴边热区随窗口状态显隐(见 sync_edges): 系统最大化/拉伸全屏态都不可缩放,
            # 热区留着只会拦鼠标/显示缩放光标, 挡住自动隐藏任务栏(用户实测)。
            def on_resize(_s, _e):
                sync_edges()

            form.Resize += on_resize
            sync_edges()
            # 系统最大化路径(Win+Up/拖到屏幕顶)的 MaximizedBounds 兜底
            form.Move += sync_max_bounds
            sync_max_bounds()
        except Exception:
            import traceback
            traceback.print_exc()

    if form.InvokeRequired:
        form.BeginInvoke(WinForms.MethodInvoker(_install))
    else:
        _install()

    def update(tabs, active):
        with lock:
            st["tabs"] = list(tabs)
            st["active"] = active

        def _repaint():
            try:
                strip.Invalidate()
            except Exception:
                pass
        if strip.InvokeRequired:
            strip.BeginInvoke(WinForms.MethodInvoker(_repaint))
        else:
            _repaint()

    def highlight(on):
        """拖拽悬浮窗经过标签栏时整条高亮 —— "松手即合并成标签页"的落点提示
        (shell_float._hint 调用; 可从任意线程调用, 内部 marshal 到 UI 线程重绘)。"""
        def _apply():
            try:
                st["drop_hi"] = bool(on)
                strip.Invalidate()
            except Exception:
                pass
        try:
            if strip.InvokeRequired:
                strip.BeginInvoke(WinForms.MethodInvoker(_apply))
            else:
                _apply()
        except Exception:
            pass

    def set_fullscreen(on):
        """bug6: 网页/视频全屏 → 隐藏标签栏与贴边热区, 内容区交给 shell_pages 铺满客户区
        (整窗只剩画面); 退出时按当前窗口状态还原。可从任意线程调用。"""
        def _apply():
            try:
                fs_state["on"] = bool(on)
                strip.Visible = not fs_state["on"]
                if not fs_state["on"]:
                    strip.BringToFront()
                sync_edges()
            except Exception:
                pass
        try:
            if strip.InvokeRequired:
                strip.BeginInvoke(WinForms.MethodInvoker(_apply))
            else:
                _apply()
        except Exception:
            pass

    update.set_fullscreen = set_fullscreen   # bug6: 窗口壳按网页全屏状态显隐标签栏
    update.highlight = highlight      # 悬浮窗拖拽落点提示(shell_float 拖拽期间调用)
    update.fill_toggle = _toggle_max   # 窗口化全屏开关(测试可直调; app_window 只用调用签名)
    update.bar_h = bar_h               # 标签栏逻辑高度(app_window 定位导航工具栏用)
    return update
