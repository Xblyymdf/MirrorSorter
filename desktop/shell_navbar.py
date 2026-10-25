# -*- coding: utf-8 -*-
"""窗口顶部标签栏下方的"浏览器式"导航工具栏(仅供 app_window.py 窗口壳使用)。

布局(自上而下): shell_tabstrip 标签栏 → 本工具栏 → WebView2 内容区。
- 左端三个自绘图标按钮: ← 后退 / → 前进 / ⟳ 刷新(无历史=禁用灰, 悬停高亮);
- 其余宽度是原生 TextBox 地址栏: 实时显示当前激活标签页 URL, 可编辑 ——
  回车导航(无协议头自动补 https://, 非 http(s) 拒绝并还原), Esc/失焦还原显示值。
本栏属于窗口 chrome, 不遮挡任何网页内容; 按下不触发窗口拖动(与标签栏不同)。
配色与标签栏共用 ui_bg_scheme 推送(app.js bgStripScheme), 独立线程轮询跟随。

注意(与 shell_tabstrip 同一套踩坑纪律):
- pythonnet 虚方法重写不可靠 → 全部走 Paint/MouseClick/KeyDown 事件订阅;
- Pen/Brush/Font 等在函数顶部统一 import/创建, 不得在绘制分支中途 import;
- 所有 update 经 BeginInvoke marshal 到 UI 线程。
"""
import re
import threading

NAV_H = 40   # 逻辑高度(px); DPI 换算经 nav_h(scale) —— 对标 Chrome/Edge 的 40 上下


def nav_h(scale=1.0):
    return max(32, int(NAV_H * scale))


def install_navbar(win, api, top=0, bg_getter=None, bookmarks=None,
                   title_getter=None):
    """在窗口 y=top 处安装导航工具栏(须在标签栏 install_strip 之后调用)。

    bookmarks: shell_bookmarks.BookmarkStore —— 给出时在刷新右侧多一个 ☆/★ 按钮
    (收藏当前网页; 网址为空的信息流固定页上禁用)。title_getter: 取当前标签标题。
    返回 update(url, can_back, can_fwd, loading=False) —— ShellApi 经它推送状态。
    """
    import clr
    clr.AddReference("System.Drawing")
    from System.Drawing import (Color, Font, FontStyle, Pen, RectangleF,
                                SolidBrush, Point, Size, StringFormat,
                                StringAlignment, StringFormatFlags, StringTrimming)
    from System.Drawing.Drawing2D import GraphicsPath, SmoothingMode
    from System.Drawing.Text import TextRenderingHint
    import System.Windows.Forms as WinForms

    form = win.native
    scale = float(getattr(form, "_scale", 1.0) or 1.0)
    h = nav_h(scale)
    st = {
        "url": "", "back": False, "fwd": False, "loading": False,
        "ai_on": False,       # 右侧 AI 助手面板是否展开(按钮高亮用)
        "hover": None,        # "back" / "fwd" / "reload"
        "edit": False,        # 地址栏正在编辑(聚焦) —— 期间不覆盖显示值
        "tb_hover": False,    # 指针在地址栏文字上(未悬停时文字降为 70% 不透明度)
        # 配色: 与标签栏同一套 ui_bg_scheme(watcher 轮询推送)
        # ★ 工具栏底色取 scheme 的 **active**(= 激活标签色), 而不是 bar: 这样激活标签
        #   的底边与工具栏同色, 两者连成一片 —— 浏览器顶栏最关键的一条结构。scheme 里的
        #   bar(更深一档)留给标签条本身做背景, 于是形成"深色框 + 浅色内容板"的层次。
        "bar": Color.FromArgb(22, 25, 32),
        "strip": Color.FromArgb(10, 12, 16),   # 标签条底色(只用来推地址栏胶囊的深浅)
        "hover_bg": Color.FromArgb(30, 34, 44),
        "fg": Color.FromArgb(226, 230, 238),
        "fg_dim": Color.FromArgb(96, 102, 114),
        "tb_bg": Color.FromArgb(17, 19, 24),   # 地址栏胶囊底色(由 bar/strip 混出来)
        "tb_fg": Color.FromArgb(226, 230, 238),
        "tb_border": Color.FromArgb(52, 57, 68),
        "accent": Color.FromArgb(76, 141, 255),
    }

    # 图标字体: 优先 Segoe MDL2 Assets(Win10+ 自带), 缺则退回雅黑符号
    try:
        from System.Drawing import FontFamily
        FontFamily("Segoe MDL2 Assets")
        icon_font = Font("Segoe MDL2 Assets", 11.0 * scale)
        glyphs = {"back": "\uE72B", "fwd": "\uE72A", "reload": "\uE72C",
                  "lock": "\uE72E", "globe": "\uE774", "search": "\uE721",
                  "star": "\uE734", "star_on": "\uE735"}
    except Exception:
        icon_font = Font("Microsoft YaHei", 10.5 * scale)
        glyphs = {"back": "\u2190", "fwd": "\u2192", "reload": "\u21BB",
                  "lock": "", "globe": "\u25CB", "search": "\u2315",
                  "star": "\u2606", "star_on": "\u2605"}
    # ★ 地址栏用 Segoe UI(缺中文时 GDI+ 自动 font-linking 到雅黑): 雅黑的拉丁字母
    #   又宽又钝, 一行网址里英文/数字占大头, 用雅黑一眼就廉价。
    tb_font = Font("Segoe UI", 9.0 * scale)
    ai_font = Font("Segoe UI", 8.0 * scale, FontStyle.Bold)   # AI 圆钮里的 "AI"

    # 按钮贴紧成一"组"(Chrome 的后退/前进/刷新之间没有间隙), 圆形悬停底才连得起来
    BTN_W, BTN_GAP, MARGIN = int(32 * scale), 0, int(8 * scale)
    PILL_INSET = int(6 * scale)      # 地址栏胶囊的上下内缩 → 胶囊高 = h - 2*inset

    bar = WinForms.Panel()
    bar.DoubleBuffered = True
    bar.Location = Point(0, int(top))
    bar.Size = Size(form.ClientSize.Width, h)
    bar.Anchor = (WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Left |
                  WinForms.AnchorStyles.Right)
    bar.BackColor = st["bar"]

    # ---- 布局几何 ----
    def _names():
        ns = ["back", "fwd", "reload"]
        if bookmarks is not None:
            ns.append("star")
        return ns

    def _enabled(name):
        return {"back": st["back"], "fwd": st["fwd"], "reload": True,
                "star": bool(st["url"])}[name]

    # ---- 右端 AI 助手开关(常驻: 面板开合的唯一入口) ----
    AI_W = int(40 * scale)

    def _ai_rect():
        return (bar.Width - MARGIN - AI_W, 0, AI_W, h)

    def _btn_rects():
        x, bw, gap = MARGIN, BTN_W, BTN_GAP
        return {n: (x + i * (bw + gap), 0, bw, h)
                for i, n in enumerate(_names())}

    def _pill_rect():
        """地址栏胶囊(Chrome/Edge 的 omnibox): 高度撑满工具栏减去上下内缩, 两端全圆。"""
        rects = _btn_rects()
        last = _names()[-1]
        end = rects[last][0] + rects[last][2]
        tx = end + int(10 * scale)
        right = bar.Width - MARGIN - AI_W - int(8 * scale)
        ph = h - PILL_INSET * 2
        return (tx, (h - ph) // 2, max(int(60 * scale), right - tx), ph)

    def _site_icon_w():
        return int(16 * scale)

    def _tb_rect():
        """原生 TextBox 的位置 —— 必须**内缩在胶囊里**: 它是不透明矩形, 贴到胶囊两端
        就会把圆角切方。左边让出站点图标, 右边让出一个圆角半径。"""
        px, py, pw, ph = _pill_rect()
        rad = ph // 2
        tx = px + int(9 * scale) + _site_icon_w() + int(6 * scale)
        right = px + pw - rad - int(2 * scale)
        hh = _tb_height()
        return (tx, py + (ph - hh) // 2, max(int(40 * scale), right - tx), hh)

    def _tb_height():
        """单行 TextBox 的高度由字体决定(Size 里给的高度会被忽略), 只能问 PreferredHeight。"""
        try:
            return int(tb.PreferredHeight)
        except Exception:
            return int(20 * scale)

    # ---- 地址栏(原生 TextBox: 输入法/光标/全选白送) ----
    tb = WinForms.TextBox()
    tb.BorderStyle = getattr(WinForms.BorderStyle, "None")   # 边框自绘(系统色在深色下发白)
    tb.Font = tb_font
    tb.BackColor = st["tb_bg"]
    tb.ForeColor = st["tb_fg"]
    tb.TabStop = True

    def _place_tb():
        r = _tb_rect()
        tb.Location = Point(r[0], r[1])
        tb.Size = Size(r[2], r[3])

    _place_tb()

    # ---- bug7: 地址栏文字不对视 ----
    # 未悬停/未聚焦时, 地址栏文字不再用满对比的纯白(深色)/纯黑(浅色), 而是把它按
    # 70% 不透明度混到地址栏底色上; 悬停或聚焦才回到完整对比度。底色/前景跟随配色
    # 方案变化, 所以这里每次都按当前 st 重算(见 set_scheme)。
    def _blend(c, bg, k):
        return Color.FromArgb(int(bg.R + (c.R - bg.R) * k),
                              int(bg.G + (c.G - bg.G) * k),
                              int(bg.B + (c.B - bg.B) * k))

    def _recalc_omni():
        """地址栏胶囊底色 = 工具栏底与标签条底混出来。

        两个主题下都靠"比工具栏深一档"把胶囊从底板上分出来(Chrome 同款): 深色主题
        工具栏是浅一档的 active, 胶囊回到更深的 bar 附近; 浅色主题工具栏近白, 胶囊
        退成浅灰。写死一个颜色在两个主题下必有一边糊在一起。
        """
        st["tb_bg"] = _blend(st["strip"], st["bar"], 0.85)

    def _apply_tb_fg():
        # 0.82 而不是 0.7: 0.7 那档在深色主题下像"禁用态", 地址栏显得死气沉沉;
        # 0.82 既压住了纯白的刺眼, 又保住了"这是当前地址"的可读性。
        try:
            tb.ForeColor = (st["fg"] if (st["edit"] or st["tb_hover"])
                            else _blend(st["fg"], st["tb_bg"], 0.82))
        except Exception:
            pass

    def _set_tb_hover(v):
        v = bool(v)
        if v != st["tb_hover"]:
            st["tb_hover"] = v
            _apply_tb_fg()
            try:                       # 胶囊描边随悬停加深, 得重画底板
                bar.Invalidate()
            except Exception:
                pass

    _recalc_omni()
    try:
        tb.BackColor = st["tb_bg"]
    except Exception:
        pass
    _apply_tb_fg()

    sf = StringFormat()
    sf.Alignment = StringAlignment.Center
    sf.LineAlignment = StringAlignment.Center
    sf.FormatFlags = StringFormatFlags.NoWrap
    sf.Trimming = StringTrimming.EllipsisCharacter

    def _rounded_path(x, y, w, h_, rad):
        p = GraphicsPath()
        d = rad * 2
        p.AddArc(x, y, d, d, 180, 90)
        p.AddArc(x + w - d, y, d, d, 270, 90)
        p.AddArc(x + w - d, y + h_ - d, d, d, 0, 90)
        p.AddArc(x, y + h_ - d, d, d, 90, 90)
        p.CloseFigure()
        return p

    def _circle(bx, by, bw, bh):
        """按钮框 → 居中圆形(Chrome/Edge 的按钮悬停是正圆, 不是圆角方块)。"""
        d = min(bw, bh) - int(6 * scale)
        return (bx + (bw - d) / 2.0, by + (bh - d) / 2.0, d)

    def on_paint(_sender, e):
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        # ★ ClearType: GDI+ 默认灰度 AA, 图标字形和小字号网址都会发虚(廉价感主因之一)
        try:
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
        except Exception:
            pass
        g.Clear(st["bar"])
        rects = _btn_rects()
        for name in _names():
            bx, by, bw, bh = rects[name]
            enabled = _enabled(name)
            hov = (st["hover"] == name and enabled)
            if hov:
                cx, cy, d = _circle(bx, by, bw, bh)
                g.FillEllipse(SolidBrush(st["hover_bg"]),
                              float(cx), float(cy), float(d), float(d))
            glyph = glyphs[name]
            if name == "star" and enabled:
                try:
                    if bookmarks.has(st["url"]):
                        glyph = glyphs.get("star_on", glyph)
                except Exception:
                    pass
            g.DrawString(glyph, icon_font,
                         SolidBrush(st["fg"] if enabled else st["fg_dim"]),
                         RectangleF(float(bx), float(by), float(bw), float(bh)), sf)
        # ---- 地址栏胶囊(omnibox): 全圆两端 + 左侧站点图标 + 聚焦/悬停描边 ----
        px, py, pw, ph = _pill_rect()
        prad = ph // 2
        g.FillPath(SolidBrush(st["tb_bg"]),
                   _rounded_path(px, py, pw, ph, prad))
        # 平时**不描边**(Chrome 的 omnibox 就是一块无边框的实色胶囊, 描一圈线立刻变成
        # "输入框控件"的味道); 悬停给一圈淡描边, 聚焦给强调色描边 —— 状态靠边表达。
        if st["edit"]:
            g.DrawPath(Pen(st["accent"], max(1.0, 1.4 * scale)),
                       _rounded_path(px, py, pw, ph, prad))
        elif st["tb_hover"]:
            g.DrawPath(Pen(st["fg_dim"], 1.0), _rounded_path(px, py, pw, ph, prad))
        # 站点图标: 空地址/非 http(s) = 这是搜索框 → 放大镜; https = 锁; 其余 = 地球
        u = str(st["url"]).strip().lower()
        if not u or not (u.startswith("http://") or u.startswith("https://")):
            sic = glyphs.get("search")
        elif u.startswith("https://"):
            sic = glyphs.get("lock")
        else:
            sic = glyphs.get("globe")
        if sic:
            iw = _site_icon_w()
            g.DrawString(sic, icon_font, SolidBrush(st["fg_dim"]),
                         RectangleF(float(px + int(9 * scale)),
                                    float(py), float(iw), float(ph)), sf)
        # ---- 右端 AI 助手按钮: 圆形, 开着 = 强调色实心底 + 强调色文字 ----
        ar = _ai_rect()
        acx, acy, ad = _circle(ar[0], ar[1], ar[2], ar[3])
        ai_on = bool(st.get("ai_on"))
        if ai_on:
            # 开着 = 强调色实心圆 + 白字: 一眼看出"面板现在开着"(淡底+同色字太虚)
            g.FillEllipse(SolidBrush(st["accent"]),
                          float(acx), float(acy), float(ad), float(ad))
        elif st["hover"] == "ai":
            g.FillEllipse(SolidBrush(st["hover_bg"]),
                          float(acx), float(acy), float(ad), float(ad))
        else:
            # 关着时给一圈细描边: 不然 "AI" 两个字母像个静态标签, 看不出是按钮
            g.DrawEllipse(Pen(st["tb_border"], 1.0),
                          float(acx), float(acy), float(ad), float(ad))
        g.DrawString("AI", ai_font,
                     SolidBrush(Color.White if ai_on
                                else (st["fg"] if st["hover"] == "ai" else st["fg_dim"])),
                     RectangleF(float(ar[0]), float(ar[1]), float(ar[2]), float(ar[3])), sf)

    # ---- 交互: 按钮悬停/点击(禁用态不响应) ----
    def _btn_at(x, y):
        for name, (bx, by, bw, bh) in _btn_rects().items():
            if bx <= x <= bx + bw and 0 <= y <= bh:
                return name
        return None

    def on_mouse_move(_sender, args):
        name = _btn_at(args.X, args.Y)
        if name and not _enabled(name):
            name = None   # 禁用按钮不给悬停高亮
        if name is None:
            ar = _ai_rect()
            if ar[0] <= args.X <= ar[0] + ar[2] and ar[1] <= args.Y <= ar[1] + ar[3]:
                name = "ai"
        if name != st["hover"]:
            st["hover"] = name
            bar.Invalidate()
        # 地址栏的悬停态按**整个胶囊**算(不是按文字框): 指针落在胶囊边缘也算悬停。
        # 注意指针落到子控件 TextBox 上时本 Panel 会收到 MouseLeave, 所以这里不据
        # MouseLeave 收尾, 只看坐标(以及 tb 自己的进出)。
        pr = _pill_rect()
        _set_tb_hover(pr[0] <= args.X <= pr[0] + pr[2] and
                      pr[1] <= args.Y <= pr[1] + pr[3])

    def on_mouse_leave(_sender, _args):
        if st["hover"] is not None:
            st["hover"] = None
            bar.Invalidate()

    def on_click(_sender, args):
        name = _btn_at(args.X, args.Y)
        if name is None:
            ar = _ai_rect()
            if ar[0] <= args.X <= ar[0] + ar[2] and ar[1] <= args.Y <= ar[1] + ar[3]:
                name = "ai"
        if name == "ai":
            try:
                api.toggle_ai_panel()
            except Exception:
                pass
            return
        if name == "back" and st["back"]:
            api.nav_back()
        elif name == "fwd" and st["fwd"]:
            api.nav_forward()
        elif name == "reload":
            api.nav_reload()
        elif name == "star" and st["url"]:
            title = ""
            if title_getter is not None:
                try:
                    title = str(title_getter() or "")
                except Exception:
                    title = ""
            try:
                bookmarks.toggle(st["url"], title)
            except Exception:
                pass
            bar.Invalidate()

    # ---- 地址栏: 回车导航 / Esc 还原 / 失焦还原 / 点击聚焦全选 ----
    st["tb_click"] = False

    def on_tb_down(_s=None, _e=None):
        # 用户按下地址栏。GotFocus 先于 MouseDown 触发(Control.WmMouseDown 先
        # Focus() 再 OnMouseDown), 所以全选要延后一拍看这个标记。
        st["tb_click"] = True

    def on_tb_focus(_s=None, _a=None):
        st["edit"] = True
        _apply_tb_fg()
        try:                     # 聚焦 → 胶囊描边变强调色, 要重画底板
            bar.Invalidate()
        except Exception:
            pass

        def _sel_all():
            # 只给"用户自己点进来"的焦点全选。被动获焦不能全选 —— 隐藏 WebView2 时
            # Windows 会把焦点临时甩给本框, 否则切标签瞬间网址会闪一下高亮。
            want = bool(st.get("tb_click"))
            st["tb_click"] = False
            if want and tb.Focused:
                try:
                    tb.SelectAll()
                except Exception:
                    pass

        try:
            tb.BeginInvoke(WinForms.MethodInvoker(_sel_all))
        except Exception:
            pass

    def on_tb_blur(_s=None, _a=None):
        st["tb_click"] = False
        st["edit"] = False
        try:
            tb.Text = st["url"]
        except Exception:
            pass
        _apply_tb_fg()
        try:
            bar.Invalidate()
        except Exception:
            pass

    def on_tb_key(_s, e):
        code = int(e.KeyCode)
        if code == 13:   # Enter
            st["edit"] = False
            # 输入的是网址 → 直接去; 是普通文字 → 按「设置 → 常规 → 浏览器」的
            # 搜索引擎搜索(见 ShellApi.nav_input)
            api.nav_input(str(tb.Text or ""))
            try:
                tb.Text = st["url"]
            except Exception:
                pass
            e.Handled = True
            e.SuppressKeyPress = True
        elif code == 27:   # Esc
            st["edit"] = False
            try:
                tb.Text = st["url"]
            except Exception:
                pass
            e.Handled = True
            e.SuppressKeyPress = True

    tb.MouseDown += on_tb_down
    tb.GotFocus += on_tb_focus
    tb.LostFocus += on_tb_blur
    tb.KeyDown += on_tb_key
    tb.MouseEnter += lambda _s, _e: _set_tb_hover(True)
    tb.MouseLeave += lambda _s, _e: _set_tb_hover(False)
    # 编辑期间点击地址栏不再下传按钮命中; 工具栏 Panel 上按下不拖动窗口(与标签栏不同)

    bar.Paint += on_paint
    bar.MouseMove += on_mouse_move
    bar.MouseLeave += on_mouse_leave
    bar.MouseClick += on_click
    bar.Resize += lambda _s, _e: (_place_tb(), bar.Invalidate())

    def _install():
        try:
            form.Controls.Add(bar)
            # tb 必须是 bar 的子控件(坐标相对工具栏) —— 挂到 form 上会落到标签栏那行
            bar.Controls.Add(tb)
            tb.BringToFront()
        except Exception:
            import traceback
            traceback.print_exc()

    if form.InvokeRequired:
        form.BeginInvoke(WinForms.MethodInvoker(_install))
    else:
        _install()

    def update(url, can_back, can_fwd, loading=False):
        st["url"] = str(url or "")
        st["back"] = bool(can_back)
        st["fwd"] = bool(can_fwd)
        st["loading"] = bool(loading)

        def _apply():
            try:
                if not st["edit"]:   # 编辑期间不覆盖用户输入
                    tb.Text = st["url"]
                bar.Invalidate()
            except Exception:
                pass
        if bar.InvokeRequired:
            bar.BeginInvoke(WinForms.MethodInvoker(_apply))
        else:
            _apply()

    def set_scheme(raw):
        """页面推送的配色方案(与标签栏 set_bg_scheme 同一套 json: bar/active/hover/
        fgon/fgoff/sep), 同步按钮/地址栏配色。"""
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

            # ★ 工具栏底取 active(激活标签色), 标签条底取 bar —— 见 st 初始化处的说明。
            st["bar"] = _c("active", st["bar"])
            st["strip"] = _c("bar", st["strip"])
            st["hover_bg"] = _c("hover", st["hover_bg"])
            st["fg"] = _c("fgon", st["fg"])
            st["fg_dim"] = _c("fgoff", st["fg_dim"])
            st["tb_border"] = _c("sep", st["tb_border"])

            def apply():
                try:
                    bar.BackColor = st["bar"]
                    _recalc_omni()          # 胶囊底色由 bar/strip 混出, 必须重算
                    tb.BackColor = st["tb_bg"]
                    _apply_tb_fg()      # 未悬停时按新底色重算 70% 混色
                except Exception:
                    pass
                bar.Invalidate()

            if bar.InvokeRequired:
                bar.BeginInvoke(WinForms.MethodInvoker(apply))
            else:
                apply()
        except Exception:
            pass

    def scheme_watch():
        import time as _t
        last = None
        while True:
            try:
                if bg_getter:
                    cur = bg_getter()
                    if cur != last:
                        last = cur
                        set_scheme(cur)
            except Exception:
                pass
            _t.sleep(2)

    if bg_getter:
        threading.Thread(target=scheme_watch, daemon=True, name="msx-navbar-scheme").start()

    def set_fullscreen(on):
        """bug6: 网页/视频全屏 → 整条导航栏(按钮+地址栏)让位, 整窗只剩视频画面。
        内容区铺满由 shell_pages 负责; 退出时把本栏重新提到最前。可从任意线程调用。"""
        def _apply():
            try:
                bar.Visible = not bool(on)
                if not on:
                    bar.BringToFront()
            except Exception:
                pass
        try:
            if bar.InvokeRequired:
                bar.BeginInvoke(WinForms.MethodInvoker(_apply))
            else:
                _apply()
        except Exception:
            pass

    # 收藏增删(收藏栏 ✕ / ☆)后 ☆/★ 状态要立刻重画
    if bookmarks is not None:
        def _refresh_star():
            try:
                if bar.InvokeRequired:
                    bar.BeginInvoke(WinForms.MethodInvoker(bar.Invalidate))
                else:
                    bar.Invalidate()
            except Exception:
                pass
        try:
            bookmarks.subscribe(_refresh_star)
        except Exception:
            pass

    def set_ai(on):
        """右侧 AI 面板开合状态 → 按钮高亮(可从任意线程调)。"""
        on = bool(on)
        if on == st.get("ai_on"):
            return
        st["ai_on"] = on

        def _apply():
            try:
                bar.Invalidate()
            except Exception:
                pass
        if bar.InvokeRequired:
            bar.BeginInvoke(WinForms.MethodInvoker(_apply))
        else:
            _apply()

    update.set_ai = set_ai

    update.bar = bar      # bug6: 网页全屏时窗口壳要整条隐藏导航栏(按钮+地址栏)
    update.set_fullscreen = set_fullscreen   # bug6: 窗口壳按网页全屏状态显隐导航栏
    # 视觉回归用出口(见 shell_tabstrip 同名注释 / _shot_chrome.py)
    update.st = st
    update.set_scheme = set_scheme
    return update
