# -*- coding: utf-8 -*-
"""内置浏览器的「历史记录」面板(Chrome 菜单里那一项)。

v0.4.53 按 Chrome 的历史记录页重排:
- 每行 **站点图标 + 标题 + 次要行(域名 · 时间)**, 悬停整行高亮并在行尾浮出 ×
  —— 上一版只有"标题\\n域名 · 时间"两行纯文字, 删除要靠右键, 用户根本看不出能删;
- 按 **日期分组**(今天 / 昨天 / 10月24日 …), 组标题是小号暗色, 与 Chrome 一致;
- 点一行 = 当前标签页打开; 点行尾 × = 删这一条(左键就能删, 右键仍然保留)。

实现: 一个 AutoScroll 容器 + 一块"内容画布"子控件, 所有行都在画布的 Paint 里画,
**不给每行建控件**。这样既有原生滚动条, 又能自由排版图标/分组/悬停; 命中测试就是
"y → 行号"一次线性扫描(最多 80 行)。上一版用 80 个 Button 拼行, 想加图标和分组
就得跟 Button 的内边距、双行文本对齐、FlatAppearance 颜色一直打架。

本模块不 import 壳: 打开/删除都走传进来的 api / app。
"""
import datetime
import time

from shell_menu import _colors, _scale_of, _rounded

ROW_H = 44          # 行高(逻辑 px)
HEAD_H = 26         # 日期分组标题高度
PAD = 14
ICON = 16           # 站点图标边长
CLOSE_W = 26        # 行尾 × 的命中宽度
MAX_ROWS = 80


def _fmt_ts(ts):
    """次要行里的时间: 组标题已经给了日期, 这里只给钟点(Chrome 也是这么排的)。"""
    try:
        t = time.localtime(int(ts))
    except Exception:
        return ""
    return "%02d:%02d" % (t.tm_hour, t.tm_min)


def _bucket(ts):
    """一条记录属于哪个日期分组。用 date 相减而不是 ts-86400(跨年/夏令时都安全)。"""
    try:
        t = time.localtime(int(ts))
    except Exception:
        return "更早"
    now = time.localtime()
    if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
        return "今天"
    try:
        d = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
        today = datetime.date(now.tm_year, now.tm_mon, now.tm_mday)
        if d == today - datetime.timedelta(days=1):
            return "昨天"
    except Exception:
        pass
    if t.tm_year == now.tm_year:
        return "%d月%d日" % (t.tm_mon, t.tm_mday)
    return "%d年%d月%d日" % (t.tm_year, t.tm_mon, t.tm_mday)


def _host_of(url):
    try:
        s = str(url or "")
        i = s.find("://")
        if i > 0:
            s = s[i + 3:]
        for sep in ("/", "?", "#"):
            j = s.find(sep)
            if j >= 0:
                s = s[:j]
        return s
    except Exception:
        return ""


def show_history(win, api, anchor=None, scheme=None, scale=None, on_close=None,
                 show=True, favicons=None):
    """弹出历史记录面板。返回 Form(失败返回 None)。

    show=False 只建不显示 —— 验收脚本用它确认"面板真的建得起来"(有原生子控件,
    必须真 Show 才能截图, 所以那边只断言构造成功)。
    favicons: shell_bookmarks.FaviconCache(与标签栏/收藏栏**共用同一份**, 别自己 new)。
    """
    import clr
    clr.AddReference("System.Drawing")
    import System.Windows.Forms as WinForms
    from System.Drawing import (Font, Point, Size, Color, Rectangle, Pen,
                                SolidBrush, ContentAlignment)
    from System.Drawing.Drawing2D import InterpolationMode, SmoothingMode
    from System.Drawing.Text import TextRenderingHint
    import app
    try:
        import shell_bookmarks as _sb     # favicon_of: 拿不到图标时的首字母占位
    except Exception:
        _sb = None

    try:
        form = win.native
    except Exception:
        form = None
    sc = _scale_of(form, scale)
    col = _colors(scheme)
    bg, hover, fg, dim, sep = (col["bg"], col["hover"], col["fg"],
                              col["dim"], col["sep"])
    danger = Color.FromArgb(226, 96, 96)   # 删除用: 悬停 × 时变红

    W = int(470 * sc)
    H = int(560 * sc)
    pop = WinForms.Form()
    pop.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
    pop.ShowInTaskbar = False
    pop.StartPosition = WinForms.FormStartPosition.Manual
    pop.BackColor = bg
    pop.Size = Size(W, H)
    pop.KeyPreview = True
    pop.DoubleBuffered = True
    st = {"closed": False, "hot": -1, "hotc": False}

    def _close(_s=None, _e=None):
        if st["closed"]:
            return
        st["closed"] = True
        try:
            if st.get("mf") is not None:
                WinForms.Application.RemoveMessageFilter(st["mf"])
        except Exception:
            pass
        try:
            pop.Close()
        except Exception:
            pass
        if on_close is not None:
            try:
                on_close()
            except Exception:
                pass

    # ★ 点值不乘 sc: Font 是"点", GDI+ 按控件 DPI 再换算一次(乘了就双重放大)
    f_title = Font("Segoe UI", 12.0)
    f_row = Font("Segoe UI", 9.0)
    f_sub = Font("Segoe UI", 8.0)
    f_head = Font("Segoe UI", 8.5)
    f_badge = Font("Segoe UI", 7.5)
    f_close = Font("Segoe UI", 11.0)

    TF = WinForms.TextFormatFlags
    FL_L = (TF.Left | TF.SingleLine | TF.EndEllipsis | TF.NoPrefix
            | TF.VerticalCenter)
    FL_C = (TF.HorizontalCenter | TF.SingleLine | TF.NoPrefix
            | TF.VerticalCenter)

    # ---- 顶部标题 + 搜索 ----
    head = WinForms.Label()
    head.Text = "历史记录"
    head.Font = f_title
    head.ForeColor = fg
    head.BackColor = bg
    head.AutoSize = False
    head.TextAlign = ContentAlignment.MiddleLeft
    head.Location = Point(int(16 * sc), int(10 * sc))
    head.Size = Size(int(200 * sc), int(26 * sc))
    pop.Controls.Add(head)

    box = WinForms.TextBox()
    box.BorderStyle = getattr(WinForms.BorderStyle, "None")
    box.Font = f_row
    # 比面板底色**深一档**(col["bar"] 就是标签条那层): 混一半会和面板糊在一起,
    # 截图里就是一条"看不见的输入框"。
    box.BackColor = col["bar"]
    box.ForeColor = fg
    box.Location = Point(int(16 * sc), int(40 * sc))
    box.Size = Size(W - int(32 * sc), int(26 * sc))
    pop.Controls.Add(box)

    def _cue():
        try:
            from shell_navbar import set_cue
            set_cue(box, "搜索历史记录")
        except Exception:
            pass

    _cue()      # 本函数只在 UI 线程跑(菜单点击/验收脚本), 直接设即可
    try:
        box.BeginInvoke(WinForms.MethodInvoker(_cue))   # 兜底: 万一 Handle 还没建好
    except Exception:
        pass

    # ---- 滚动容器 + 内容画布 ----
    listp = WinForms.Panel()
    listp.Location = Point(0, int(80 * sc))
    listp.Size = Size(W, H - int(80 * sc) - int(44 * sc))
    listp.BackColor = bg
    listp.AutoScroll = True
    pop.Controls.Add(listp)

    canvas = WinForms.Panel()
    canvas.Location = Point(0, 0)
    canvas.Size = Size(listp.ClientSize.Width, listp.ClientSize.Height)
    canvas.BackColor = bg
    listp.Controls.Add(canvas)
    try:
        # Panel.DoubleBuffered 是 protected: 自绘列表拖动滚动条时不留白闪
        import System.Reflection as Reflection
        (type(canvas).GetProperty("DoubleBuffered",
                                  Reflection.BindingFlags.Instance
                                  | Reflection.BindingFlags.NonPublic)
         .SetValue(canvas, True, None))
    except Exception:
        pass

    lay = []          # 布局结果: [{"kind":"head"/"row", "y", "h", ...}]
    pop._msx_rows = lay    # 验收脚本用: 数行数/分组, 不去碰内部闭包

    def _rows(q):
        try:
            return app.web_hist_list(q, MAX_ROWS)
        except Exception:
            return []

    def _rebuild():
        try:
            del lay[:]
            rows = _rows(box.Text)
            y, last = 0, None
            for r in rows:
                url = str(r.get("url") or "")
                title = str(r.get("title") or "").strip() or _host_of(url)
                b = _bucket(r.get("ts"))
                if b != last:
                    lay.append({"kind": "head", "label": b, "y": y,
                                "h": int(HEAD_H * sc)})
                    y += int(HEAD_H * sc)
                    last = b
                lay.append({"kind": "row", "url": url, "title": title,
                            "sub": "%s · %s" % (_host_of(url), _fmt_ts(r.get("ts"))),
                            "y": y, "h": int(ROW_H * sc)})
                y += int(ROW_H * sc)
            st["empty"] = ("没有匹配「%s」的记录" % box.Text) if box.Text \
                else "还没有浏览记录"
            st["hot"], st["hotc"] = -1, False
            canvas.Size = Size(1, max(y, listp.ClientSize.Height))
            listp.PerformLayout()
            canvas.Width = listp.ClientSize.Width
            canvas.Invalidate()
        except Exception:
            import traceback
            traceback.print_exc()

    def _at(y):
        for i, it in enumerate(lay):
            if it["y"] <= y < it["y"] + it["h"]:
                return i
        return -1

    def _close_rect(it):
        w = canvas.Width
        return Rectangle(w - int(PAD * sc) - int(CLOSE_W * sc), it["y"],
                         int(CLOSE_W * sc), it["h"])

    def _in_close(x, it):
        r = _close_rect(it)
        return r.X <= x < r.X + r.Width

    # ---- 画 ----
    def _paint(sender, e):
        g = e.Graphics
        try:
            g.SmoothingMode = SmoothingMode.AntiAlias
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
        except Exception:
            pass
        g.Clear(bg)
        cw = canvas.Width
        if not lay:
            WinForms.TextRenderer.DrawText(
                g, str(st.get("empty") or ""), f_row,
                Rectangle(0, int(24 * sc), cw, int(30 * sc)), dim, FL_C)
            return
        clip = canvas.Height
        for i, it in enumerate(lay):
            y, hh = it["y"], it["h"]
            if y + hh < 0 or y > clip:
                continue
            if it["kind"] == "head":
                WinForms.TextRenderer.DrawText(
                    g, it["label"], f_head,
                    Rectangle(int(PAD * sc), y + int(6 * sc),
                              cw - int(PAD * 2 * sc), hh - int(6 * sc)), dim, FL_L)
                continue
            hot = (i == st["hot"])
            if hot:
                g.FillPath(SolidBrush(hover),
                           _rounded(int(6 * sc), y + int(2 * sc),
                                    cw - int(12 * sc), hh - int(4 * sc), int(5 * sc)))
            # 站点图标: 真实 favicon 优先, 拿不到才用"域名首字母 + 中性底色"(不冒充 logo)
            bx = int(PAD * sc)
            by = y + (hh - int(ICON * sc)) // 2
            ico = None
            if favicons is not None:
                try:
                    ico = favicons.image(it["url"])
                except Exception:
                    ico = None
            if ico is not None:
                try:
                    g.InterpolationMode = InterpolationMode.HighQualityBicubic
                    g.DrawImage(ico, Rectangle(bx, by, int(ICON * sc), int(ICON * sc)))
                except Exception:
                    ico = None
            if ico is None:
                try:
                    fav = _sb.favicon_of(it["url"])
                    r0, g0, b0 = fav["color"]
                    g.FillPath(SolidBrush(Color.FromArgb(r0, g0, b0)),
                               _rounded(bx, by, int(ICON * sc), int(ICON * sc),
                                        int(3 * sc)))
                    WinForms.TextRenderer.DrawText(
                        g, fav["letter"], f_badge,
                        Rectangle(bx, by, int(ICON * sc), int(ICON * sc)),
                        Color.White, FL_C)
                except Exception:
                    pass
            tx = bx + int(ICON * sc) + int(10 * sc)
            tw = max(0, cw - tx - int(PAD * sc) - int(CLOSE_W * sc))
            WinForms.TextRenderer.DrawText(
                g, it["title"], f_row,
                Rectangle(tx, y + int(4 * sc), tw, int(19 * sc)), fg, FL_L)
            WinForms.TextRenderer.DrawText(
                g, it["sub"], f_sub,
                Rectangle(tx, y + int(22 * sc), tw, int(15 * sc)), dim, FL_L)
            if hot:
                WinForms.TextRenderer.DrawText(
                    g, "\u00d7", f_close, _close_rect(it),
                    danger if st["hotc"] else dim, FL_C)

    canvas.Paint += _paint

    # ---- 命中: 悬停高亮 / 点开 / 删单条 ----
    def _set_hot(i, close=False):
        """设置悬停行(验收脚本用它截"悬停态", 不必真的移动鼠标)。"""
        st["hot"], st["hotc"] = int(i), bool(close)
        try:
            canvas.Invalidate()
        except Exception:
            pass

    pop._msx_hot = _set_hot

    def _on_move(_s, e):
        i = _at(e.Y)
        ok = i >= 0 and lay[i]["kind"] == "row"
        h = i if ok else -1
        hc = bool(ok and _in_close(e.X, lay[i]))
        if (h, hc) != (st["hot"], st["hotc"]):
            st["hot"], st["hotc"] = h, hc
            canvas.Cursor = WinForms.Cursors.Hand if hc else WinForms.Cursors.Default
            canvas.Invalidate()

    def _on_leave(_s, _e):
        if (st["hot"], st["hotc"]) != (-1, False):
            st["hot"], st["hotc"] = -1, False
            canvas.Cursor = WinForms.Cursors.Default
            canvas.Invalidate()

    def _on_click(_s, e):
        i = _at(e.Y)
        if i < 0 or lay[i]["kind"] != "row":
            return
        it = lay[i]
        url = str(it["url"] or "")
        if not url:
            return
        right = False
        try:
            right = (e.Button == WinForms.MouseButtons.Right)
        except Exception:
            pass
        if right or _in_close(e.X, it):
            try:
                app.web_hist_del(url)
            except Exception:
                pass
            _rebuild()
            return
        _close()
        try:
            api.nav_goto(url)
        except Exception:
            pass

    canvas.MouseMove += _on_move
    canvas.MouseLeave += _on_leave
    canvas.MouseClick += _on_click

    def _wheel_by(n):
        step = int(ROW_H * sc)
        cur = -listp.AutoScrollPosition.Y
        maxv = max(0, canvas.Height - listp.ClientSize.Height)
        nv = max(0, min(maxv, cur + n * step))
        listp.AutoScrollPosition = Point(0, nv)

    # 滚轮: WinForms 把 WM_MOUSEWHEEL 发给**焦点控件**(这里是搜索框), 而搜索框不可
    # 滚动 → 消息就断了, 光标在列表上滚滚轮毫无反应。装一个消息过滤器, 按"光标是否
    # 落在列表矩形内"来拦 —— 比给每个子控件挂 MouseWheel 可靠(子控件不一定有焦点)。
    try:
        class _WheelFilter(WinForms.IMessageFilter):
            def PreFilterMessage(self, m):
                try:
                    if st["closed"] or int(m.Msg) != 0x020A:   # WM_MOUSEWHEEL
                        return False
                    if not listp.RectangleToScreen(
                            listp.ClientRectangle).Contains(WinForms.Cursor.Position):
                        return False
                    d = (int(m.WParam.ToInt64()) >> 16) & 0xFFFF
                    if d >= 0x8000:
                        d -= 0x10000
                    _wheel_by(-1 if d > 0 else 1)
                    return True
                except Exception:
                    return False

        st["mf"] = _WheelFilter()
        WinForms.Application.AddMessageFilter(st["mf"])
    except Exception:
        st["mf"] = None      # 装不上就退化成"只能拖滚动条", 不影响其它功能

    def _on_resize(_s=None, _e=None):
        try:
            canvas.Width = listp.ClientSize.Width
            canvas.Invalidate()
        except Exception:
            pass

    listp.Resize += _on_resize

    # ---- 底部: 清除全部 ----
    foot = WinForms.Button()
    foot.Text = "清除全部浏览记录"
    foot.FlatStyle = WinForms.FlatStyle.Flat
    foot.FlatAppearance.BorderSize = 0
    foot.FlatAppearance.MouseOverBackColor = _mix(hover, bg, 0.55)
    foot.BackColor = bg
    foot.ForeColor = dim
    foot.Font = f_sub
    foot.TextAlign = ContentAlignment.MiddleLeft
    foot.Location = Point(int(12 * sc), H - int(42 * sc))
    foot.Size = Size(int(180 * sc), int(30 * sc))
    foot.Cursor = WinForms.Cursors.Hand

    def _clear(_s=None, _e=None):
        try:
            app.web_hist_clear()
        except Exception:
            pass
        _rebuild()

    foot.Click += _clear
    pop.Controls.Add(foot)

    tip = WinForms.Label()
    tip.Text = "点开 · 悬停行尾 × 删除"
    tip.Font = f_sub
    tip.ForeColor = dim
    tip.BackColor = bg
    tip.AutoSize = False
    tip.TextAlign = ContentAlignment.MiddleRight
    tip.Location = Point(W - int(220 * sc), H - int(42 * sc))
    tip.Size = Size(int(206 * sc), int(30 * sc))
    pop.Controls.Add(tip)

    def _on_paint(_s, e):
        g = e.Graphics
        try:
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
        except Exception:
            pass
        g.DrawRectangle(Pen(sep, 1.0), 0, 0, pop.Width - 1, pop.Height - 1)
        # 标题区与列表之间一道细线: 没有它, 搜索框下面就是一片空, 分不清哪块是"头"
        y = int(78 * sc)
        g.DrawLine(Pen(sep, 1.0), float(int(12 * sc)), float(y),
                   float(W - int(12 * sc)), float(y))
        # 底部工具条上方同样来一道, 否则"清除全部"像是列表的最后一行
        y2 = H - int(46 * sc)
        g.DrawLine(Pen(sep, 1.0), float(int(12 * sc)), float(y2),
                   float(W - int(12 * sc)), float(y2))

    pop.Paint += _on_paint
    pop.Deactivate += _close
    pop.KeyDown += lambda _s, e: _close() if int(e.KeyCode) == 27 else None

    # 搜索防抖: 每敲一个字就重排整表会卡, 250ms 之后再查
    deb = {"t": None}

    def _on_text(_s=None, _e=None):
        if deb["t"] is not None:
            try:
                deb["t"].cancel()
            except Exception:
                pass
        t = __import__("threading").Timer(0.25, lambda: _ui(_rebuild))
        t.daemon = True
        deb["t"] = t
        t.start()

    def _ui(fn):
        try:
            if pop.InvokeRequired:
                pop.BeginInvoke(WinForms.MethodInvoker(fn))
            else:
                fn()
        except Exception:
            pass

    box.TextChanged += _on_text

    try:
        _rebuild()
    except Exception:
        pass

    try:
        if anchor:
            ax, ay = int(anchor[0]), int(anchor[1])
        elif form is not None:
            ax, ay = int(form.Left + form.Width) - int(20 * sc), int(form.Top) + int(60 * sc)
        else:
            ax, ay = 200, 120
        scr = WinForms.Screen.FromControl(form).WorkingArea if form is not None \
            else WinForms.Screen.PrimaryScreen.WorkingArea
        x = max(scr.X + 4, min(ax - W, scr.Right - W - 4))
        y = max(scr.Y + 4, min(ay + int(4 * sc), scr.Bottom - H - 4))
        pop.Location = Point(x, y)
        if show:
            pop.Show(form)
            pop.Activate()
            box.Focus()
    except Exception:
        import traceback
        traceback.print_exc()
    return pop


def _mix(c, bg, k):
    """把 c 按 k 混到 bg 上(k=0 → bg, k=1 → c)。"""
    from System.Drawing import Color
    return Color.FromArgb(int(bg.R + (c.R - bg.R) * k), int(bg.G + (c.G - bg.G) * k),
                          int(bg.B + (c.B - bg.B) * k))
