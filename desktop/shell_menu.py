# -*- coding: utf-8 -*-
"""导航栏栏尾的主菜单(Chrome 式弹出菜单) + 轻提示(toast)。

为什么自绘而不是 `ContextMenuStrip`: 与标签条「+」的选择框同一个理由 —— 系统菜单在
深色主题下会露出一圈白边, 也没法跟随页面推来的顶栏配色(`ui_bg_scheme`)。所以这里
是一个无边框 Form + 逐行自绘: 点外面 / Esc / 选中一项都关闭。

本模块**不 import 壳**: 菜单项(文字、图标、回调)由 `app_window` 侧组装好传进来,
这里只负责"画 + 命中 + 关闭", 于是 shell_menu ← app_window 是单向的, 不会成环。
"""
import re
import threading

MENU_W = 268     # 逻辑宽度(px), 与 Chrome 的菜单同一量级
ROW_H = 32
SEP_H = 9


def _scale_of(form, scale=None):
    if scale:
        return float(scale)
    try:
        return float(getattr(form, "_scale", 1.0) or 1.0)
    except Exception:
        return 1.0


def _colors(raw):
    """ui_bg_scheme(json) → 画菜单要用的几个色。缺项/解析失败用深色主题默认。"""
    from System.Drawing import Color

    def c(key, fb):
        v = ""
        try:
            v = str((raw or {}).get(key) or "")
        except Exception:
            v = ""
        if re.match(r"^#[0-9a-fA-F]{6}$", v):
            return Color.FromArgb(int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16))
        return Color.FromArgb(fb[0], fb[1], fb[2])

    return {
        "bg": c("active", (34, 38, 46)),      # 菜单面板 = 工具栏同色, 像从顶栏长出来的
        "bar": c("bar", (22, 25, 32)),
        "hover": c("hover", (48, 54, 66)),
        "fg": c("fgon", (232, 235, 241)),
        "dim": c("fgoff", (124, 130, 142)),
        "sep": c("sep", (54, 59, 70)),
    }


def _blend(c, bg, k):
    from System.Drawing import Color
    return Color.FromArgb(int(bg.R + (c.R - bg.R) * k), int(bg.G + (c.G - bg.G) * k),
                          int(bg.B + (c.B - bg.B) * k))


def _rounded(x, y, w, h, rad):
    from System.Drawing.Drawing2D import GraphicsPath
    p = GraphicsPath()
    d = max(2, int(rad * 2))
    p.AddArc(x, y, d, d, 180, 90)
    p.AddArc(x + w - d, y, d, d, 270, 90)
    p.AddArc(x + w - d, y + h - d, d, d, 0, 90)
    p.AddArc(x, y + h - d, d, d, 90, 90)
    p.CloseFigure()
    return p


def show_menu(win, anchor=None, items=None, scheme=None, scale=None, on_close=None,
              show=True):
    """弹出主菜单。items 是行列表:

    {"kind": "sep"}                                       分隔线
    {"kind": "item", "label": "历史记录", "glyph": "\\uE81C",
     "fn": callable, "hint": "Ctrl+H", "dim": False}       普通行(选中即关闭并执行)
    {"kind": "zoom", "get": callable, "set": callable}      缩放行(− 100% +)

    anchor: 屏幕坐标(菜单按钮右下角)。取不到就弹在窗口右上角。
    show=False 时只建不显示(视觉验收脚本用它 DrawToBitmap 出图)。
    """
    import clr
    clr.AddReference("System.Drawing")
    import System.Windows.Forms as WinForms
    from System.Drawing import (Font, Point, RectangleF, Size, SolidBrush, Pen,
                                StringFormat, StringAlignment, StringTrimming,
                                StringFormatFlags)
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Drawing.Text import TextRenderingHint

    try:
        form = win.native
    except Exception:
        form = None
    sc = _scale_of(form, scale)
    col = _colors(scheme)
    rows = [it for it in (items or []) if isinstance(it, dict)]
    if not rows:
        return None

    pad = int(6 * sc)
    w = int(MENU_W * sc)
    h = pad * 2 + sum(int((SEP_H if r.get("kind") == "sep" else ROW_H) * sc) for r in rows)

    try:
        from System.Drawing import FontFamily
        FontFamily("Segoe MDL2 Assets")
        icon_font = Font("Segoe MDL2 Assets", 11.0)
    except Exception:
        icon_font = Font("Microsoft YaHei", 10.5)
    # ★ 点值不要乘 sc: Font 的尺寸是"点", GDI+ 会按控件 DPI 再换算一次 ——
    #   再乘 scale 就是双重放大(150% 下 9pt 变 20pt, 缩放行的 "100%" 直接折行)。
    label_font = Font("Segoe UI", 9.0)      # 与导航栏同一条纪律: 别用雅黑排英文

    pop = WinForms.Form()
    pop.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
    pop.ShowInTaskbar = False
    pop.StartPosition = WinForms.FormStartPosition.Manual
    pop.BackColor = col["bg"]
    pop.Size = Size(w, h)
    pop.DoubleBuffered = True
    pop.KeyPreview = True
    st = {"hover": -1, "closed": False}

    def _close():
        if st["closed"]:
            return
        st["closed"] = True
        try:
            pop.Close()
        except Exception:
            pass
        if on_close is not None:
            try:
                on_close()
            except Exception:
                pass

    def _layout():
        """每行的 y/高 —— 绘制与命中共用一份, 免得两边各算一遍对不上。"""
        out, y = [], pad
        for it in rows:
            hh = int((SEP_H if it.get("kind") == "sep" else ROW_H) * sc)
            out.append((it, y, hh))
            y += hh
        return out

    def _zoom_rects(y, hh):
        """缩放行右侧的 − / 数值 / + 三个热区。"""
        bw = int(24 * sc)
        vw = int(46 * sc)
        right = w - int(10 * sc)
        plus = (right - bw, y + (hh - bw) // 2, bw, bw)
        val = (plus[0] - vw, y, vw, hh)
        minus = (val[0] - bw, y + (hh - bw) // 2, bw, bw)
        return minus, val, plus

    sf_l = StringFormat()
    sf_l.Alignment = StringAlignment.Near
    sf_l.LineAlignment = StringAlignment.Center
    sf_l.FormatFlags = StringFormatFlags.NoWrap
    sf_l.Trimming = StringTrimming.EllipsisCharacter
    sf_c = StringFormat()
    sf_c.Alignment = StringAlignment.Center
    sf_c.LineAlignment = StringAlignment.Center

    def on_paint(_s, e):
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        try:
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
        except Exception:
            pass
        g.Clear(col["bg"])
        g.DrawRectangle(Pen(col["sep"], 1.0), 0, 0, pop.Width - 1, pop.Height - 1)
        lay = _layout()
        hov_bg = _blend(col["hover"], col["bg"], 0.55)   # 别直接用页面推的 hover(深色下过亮)
        for idx, (it, y, hh) in enumerate(lay):
            kind = it.get("kind")
            if kind == "sep":
                g.DrawLine(Pen(col["sep"], 1.0), float(int(10 * sc)),
                           float(y + hh // 2), float(w - int(10 * sc)), float(y + hh // 2))
                continue
            if idx == st["hover"]:
                g.FillPath(SolidBrush(hov_bg),
                           _rounded(int(4 * sc), y + int(1 * sc),
                                    w - int(8 * sc), hh - int(2 * sc), int(4 * sc)))
            dim = bool(it.get("dim"))
            fg = col["dim"] if dim else col["fg"]
            if kind == "zoom":
                g.DrawString(str(it.get("label") or "缩放"), label_font, SolidBrush(fg),
                             RectangleF(float(int(14 * sc)), float(y),
                                        float(w - int(120 * sc)), float(hh)), sf_l)
                minus, val, plus = _zoom_rects(y, hh)
                try:
                    cur = float(it["get"]())
                except Exception:
                    cur = 1.0
                for (bx, by, bw, bh), glyph in ((minus, "\uE738"), (plus, "\uE710")):
                    g.DrawString(glyph, icon_font, SolidBrush(col["dim"]),
                                 RectangleF(float(bx), float(by), float(bw), float(bh)), sf_c)
                g.DrawString("%d%%" % int(round(cur * 100)), label_font,
                             SolidBrush(fg),
                             RectangleF(float(val[0]), float(y), float(val[2]), float(hh)), sf_c)
                continue
            gx = int(14 * sc)
            if it.get("glyph"):
                g.DrawString(str(it["glyph"]), icon_font,
                             SolidBrush(col["dim"] if dim else _blend(col["fg"], col["bg"], 0.75)),
                             RectangleF(float(gx), float(y), float(int(18 * sc)), float(hh)), sf_c)
            tx = int(42 * sc)
            hint = str(it.get("hint") or "")
            tw = w - tx - (int(78 * sc) if hint else int(14 * sc))
            g.DrawString(str(it.get("label") or ""), label_font, SolidBrush(fg),
                         RectangleF(float(tx), float(y), float(tw), float(hh)), sf_l)
            if hint:
                sf_r = StringFormat()
                sf_r.Alignment = StringAlignment.Far
                sf_r.LineAlignment = StringAlignment.Center
                g.DrawString(hint, label_font, SolidBrush(col["dim"]),
                             RectangleF(float(w - int(76 * sc)), float(y),
                                        float(int(66 * sc)), float(hh)), sf_r)

    def _row_at(x, y):
        for idx, (it, ry, hh) in enumerate(_layout()):
            if ry <= y < ry + hh:
                return idx, it
        return -1, None

    def on_move(_s, a):
        idx, _it = _row_at(a.X, a.Y)
        if idx != st["hover"]:
            st["hover"] = idx
            try:
                pop.Cursor = (WinForms.Cursors.Hand if idx >= 0 and
                              rows[idx].get("kind") != "sep" else WinForms.Cursors.Default)
            except Exception:
                pass
            pop.Invalidate()

    def on_click(_s, a):
        idx, it = _row_at(a.X, a.Y)
        if it is None or it.get("kind") == "sep":
            return
        if it.get("kind") == "zoom":
            minus, val, plus = _zoom_rects(_layout()[idx][1], _layout()[idx][2])
            delta = 0
            if minus[0] <= a.X <= minus[0] + minus[2]:
                delta = -1
            elif plus[0] <= a.X <= plus[0] + plus[2]:
                delta = 1
            else:
                return          # 点在数值上 = 复位, 但 Chrome 这里是菜单项, 不做隐式行为
            try:
                it["set"](delta)
            except Exception:
                pass
            pop.Invalidate()
            return
        fn = it.get("fn")
        _close()
        if fn is not None:
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()

    pop.Paint += on_paint
    pop.MouseMove += on_move
    pop.MouseClick += on_click
    pop.Deactivate += lambda _s, _e: _close()
    pop.KeyDown += lambda _s, e: _close() if int(e.KeyCode) == 27 else None

    try:
        if anchor:
            ax, ay = int(anchor[0]), int(anchor[1])
        elif form is not None:
            ax, ay = int(form.Left + form.Width) - int(20 * sc), int(form.Top) + int(60 * sc)
        else:
            ax, ay = 200, 120
        scr = WinForms.Screen.FromControl(form).WorkingArea if form is not None \
            else WinForms.Screen.PrimaryScreen.WorkingArea
        x = max(scr.X + 4, min(ax - w, scr.Right - w - 4))
        y = ay + int(4 * sc)
        if y + h > scr.Bottom - 4:
            y = max(scr.Y + 4, ay - h - int(36 * sc))
        pop.Location = Point(x, y)
        if show:
            pop.Show(form)
            pop.Activate()
    except Exception:
        import traceback
        traceback.print_exc()
    return pop


class Toast(object):
    """轻提示(右下角浮一条短消息)。用于"正在翻译 3/12""未配置 AI 接口"这类反馈。

    ★ 不抢焦点: 只 Show 不 Activate —— 一 Activate 就会把焦点从 WebView2 抢走,
      视频/输入框当场失焦。所以它也不能有关闭按钮, 到点自己消失。
    """

    def __init__(self, win, text="", ms=2200, scheme=None, scale=None):
        import clr
        clr.AddReference("System.Drawing")
        import System.Windows.Forms as WinForms
        from System.Drawing import Font, Point, Size

        self._wf = WinForms
        self._form = None
        self._timer = None
        try:
            form = win.native
        except Exception:
            form = None
        self._owner = form
        self._sc = _scale_of(form, scale)
        self._col = _colors(scheme)
        self._font = Font("Segoe UI", 9.0)   # 点值不乘 scale(见 show_menu 里的说明)
        self._text = str(text or "")

        f = WinForms.Form()
        f.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
        f.ShowInTaskbar = False
        f.StartPosition = WinForms.FormStartPosition.Manual
        f.BackColor = self._col["bg"]
        f.DoubleBuffered = True
        f.TopMost = False
        f.Paint += self._paint
        self._form = f
        self._size_to_text()
        self._place()
        try:
            f.Show(form)
        except Exception:
            try:
                f.Show()
            except Exception:
                self._form = None
        self._arm(ms)

    # ---- 内部 ----
    def _size_to_text(self):
        from System.Drawing import Size
        w = int(max(140 * self._sc, (len(self._text) * 9 + 40) * self._sc))
        self._form.Size = Size(min(int(520 * self._sc), w), int(40 * self._sc))

    def _place(self):
        from System.Drawing import Point
        try:
            scr = self._wf.Screen.FromControl(self._owner).WorkingArea
        except Exception:
            scr = self._wf.Screen.PrimaryScreen.WorkingArea
        w, h = self._form.Width, self._form.Height
        x = scr.X + (scr.Width - w) // 2
        y = scr.Bottom - h - int(72 * self._sc)
        self._form.Location = Point(x, y)

    def _paint(self, _s, e):
        from System.Drawing import (RectangleF, SolidBrush, Pen, StringFormat,
                                    StringAlignment, StringFormatFlags)
        from System.Drawing.Drawing2D import SmoothingMode
        from System.Drawing.Text import TextRenderingHint
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        try:
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
        except Exception:
            pass
        g.Clear(self._col["bg"])
        g.DrawRectangle(Pen(self._col["sep"], 1.0), 0, 0,
                        self._form.Width - 1, self._form.Height - 1)
        sf = StringFormat()
        sf.Alignment = StringAlignment.Center
        sf.LineAlignment = StringAlignment.Center
        sf.FormatFlags = StringFormatFlags.NoWrap
        g.DrawString(self._text, self._font, SolidBrush(self._col["fg"]),
                     RectangleF(0.0, 0.0, float(self._form.Width), float(self._form.Height)), sf)

    def _arm(self, ms):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
        t = threading.Timer(max(0.4, float(ms or 2200) / 1000.0), self.close)
        t.daemon = True
        self._timer = t
        t.start()

    # ---- 对外 ----
    def set(self, text, ms=2400):
        """改文案(进度更新走这里, 不新开一个窗口 —— 否则会闪)。"""
        self._text = str(text or "")

        def go():
            try:
                self._size_to_text()
                self._place()
                self._form.Invalidate()
            except Exception:
                pass
        try:
            if self._form is not None:
                self._form.BeginInvoke(self._wf.MethodInvoker(go))
        except Exception:
            pass
        self._arm(ms)
        return self

    def close(self):
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None
        f = self._form
        if f is None:
            return
        self._form = None
        try:
            if f.InvokeRequired:
                f.BeginInvoke(self._wf.MethodInvoker(lambda: _safe_close(f)))
            else:
                _safe_close(f)
        except Exception:
            _safe_close(f)


def _safe_close(f):
    try:
        f.Close()
    except Exception:
        pass
    try:
        f.Dispose()
    except Exception:
        pass
