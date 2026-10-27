# -*- coding: utf-8 -*-
"""内置浏览器的「历史记录」面板(Chrome 菜单里那一项)。

同样是自绘无边框 Form: 顶部搜索框 + 中间可滚动列表 + 底部「清除全部」。
列表行用 Button(FlatStyle.Flat) 而不是自绘命中 —— 行数不多(上限 80), 原生滚动条
就能用, 也省掉一整套"滚动 + 命中 + 高亮"的几何计算。

交互约定(与 Chrome 一致):
- 左键一行 → 在当前标签页打开(不新开标签);
- 右键一行 → 删掉这一条(不用额外控件, 也不容易误触);
- Esc / 点外面 → 关闭。
"""
import time

from shell_menu import _colors, _scale_of

ROW_H = 46
MAX_ROWS = 80


def _fmt_ts(ts):
    try:
        t = time.localtime(int(ts))
    except Exception:
        return ""
    now = time.localtime()
    if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
        return "今天 %02d:%02d" % (t.tm_hour, t.tm_min)
    if int(ts) > time.time() - 86400 * 2:
        return "昨天 %02d:%02d" % (t.tm_hour, t.tm_min)
    if t.tm_year == now.tm_year:
        return "%d月%d日 %02d:%02d" % (t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min)
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
                 show=True):
    """弹出历史记录面板。返回 Form(失败返回 None)。

    show=False 只建不显示 —— 验收脚本用它确认"面板真的建得起来"(有原生子控件,
    必须真 Show 才能截图, 所以那边只断言构造成功)。
    """
    import clr
    clr.AddReference("System.Drawing")
    import System.Windows.Forms as WinForms
    from System.Drawing import Font, Point, Size, Color, ContentAlignment, Pen
    import app

    try:
        form = win.native
    except Exception:
        form = None
    sc = _scale_of(form, scale)
    col = _colors(scheme)

    W = int(430 * sc)
    H = int(540 * sc)
    pop = WinForms.Form()
    pop.FormBorderStyle = getattr(WinForms.FormBorderStyle, "None")
    pop.ShowInTaskbar = False
    pop.StartPosition = WinForms.FormStartPosition.Manual
    pop.BackColor = col["bg"]
    pop.Size = Size(W, H)
    pop.KeyPreview = True
    pop.DoubleBuffered = True
    st = {"closed": False}

    def _close(_s=None, _e=None):
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

    # ★ 点值不乘 sc: Font 是"点", GDI+ 按控件 DPI 再换算一次(乘了就双重放大)
    f_title = Font("Segoe UI", 12.0)
    f_body = Font("Segoe UI", 9.0)
    f_small = Font("Segoe UI", 8.5)

    # ---- 顶部标题 + 搜索 ----
    head = WinForms.Label()
    head.Text = "历史记录"
    head.Font = f_title
    head.ForeColor = col["fg"]
    head.BackColor = col["bg"]
    head.AutoSize = False
    head.TextAlign = ContentAlignment.MiddleLeft
    head.Location = Point(int(16 * sc), int(10 * sc))
    head.Size = Size(int(200 * sc), int(26 * sc))
    pop.Controls.Add(head)

    box = WinForms.TextBox()
    box.BorderStyle = getattr(WinForms.BorderStyle, "None")
    box.Font = f_body
    # 比面板底色**深一档**(col["bar"] 就是标签条那层): 混一半会和面板糊在一起,
    # 截图里就是一条"看不见的输入框"。
    box.BackColor = col["bar"]
    box.ForeColor = col["fg"]
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

    # ---- 列表(可滚动) ----
    listp = WinForms.Panel()
    listp.Location = Point(int(10 * sc), int(74 * sc))
    listp.Size = Size(W - int(20 * sc), H - int(74 * sc) - int(46 * sc))
    listp.BackColor = col["bg"]
    listp.AutoScroll = True
    pop.Controls.Add(listp)

    empty = WinForms.Label()
    empty.Text = "还没有浏览记录"
    empty.Font = f_body
    empty.ForeColor = col["dim"]
    empty.AutoSize = False
    empty.TextAlign = ContentAlignment.MiddleCenter
    empty.Size = Size(listp.Width, int(60 * sc))
    empty.Visible = False
    listp.Controls.Add(empty)

    def _rows(q):
        try:
            return app.web_hist_list(q, MAX_ROWS)
        except Exception:
            return []

    def _rebuild():
        try:
            for c in list(listp.Controls):
                if c is not empty:
                    listp.Controls.Remove(c)
                    try:
                        c.Dispose()
                    except Exception:
                        pass
            rows = _rows(box.Text)
            empty.Visible = not rows
            empty.Text = ("没有匹配「%s」的记录" % box.Text) if box.Text else "还没有浏览记录"
            y = 0
            for r in rows:
                url = str(r.get("url") or "")
                title = str(r.get("title") or "").strip() or _host_of(url)
                b = WinForms.Button()
                b.FlatStyle = WinForms.FlatStyle.Flat
                b.FlatAppearance.BorderSize = 0
                b.FlatAppearance.MouseOverBackColor = _mix(col["hover"], col["bg"], 0.55)
                b.FlatAppearance.MouseDownBackColor = _mix(col["hover"], col["bg"], 0.35)
                b.BackColor = col["bg"]
                b.ForeColor = col["fg"]
                b.Font = f_body
                b.UseMnemonic = False          # 否则标题里的 & 会被当成快捷键吃掉
                b.TextAlign = ContentAlignment.MiddleLeft
                b.Text = "%s\n%s · %s" % (title[:60], _host_of(url), _fmt_ts(r.get("ts")))
                b.Location = Point(0, y)
                b.Size = Size(listp.Width - int(18 * sc), int(ROW_H * sc))
                b.Tag = url
                b.Cursor = WinForms.Cursors.Hand
                b.Click += _on_open
                b.MouseUp += _on_right
                listp.Controls.Add(b)
                y += int(ROW_H * sc)
            empty.BringToFront()
        except Exception:
            import traceback
            traceback.print_exc()

    def _on_open(_s=None, _e=None):
        url = str(getattr(_s, "Tag", "") or "")
        if not url:
            return
        _close()
        try:
            api.nav_goto(url)
        except Exception:
            pass

    def _on_right(sender, e):
        try:
            if e.Button != WinForms.MouseButtons.Right:
                return
        except Exception:
            return
        url = str(getattr(sender, "Tag", "") or "")
        if not url:
            return
        try:
            app.web_hist_del(url)
        except Exception:
            pass
        _rebuild()

    # ---- 底部: 清除全部 ----
    foot = WinForms.Button()
    foot.Text = "清除全部浏览记录"
    foot.FlatStyle = WinForms.FlatStyle.Flat
    foot.FlatAppearance.BorderSize = 0
    foot.FlatAppearance.MouseOverBackColor = _mix(col["hover"], col["bg"], 0.55)
    foot.BackColor = col["bg"]
    foot.ForeColor = col["dim"]
    foot.Font = f_small
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
    tip.Text = "右键单条可删除"
    tip.Font = f_small
    tip.ForeColor = col["dim"]
    tip.BackColor = col["bg"]
    tip.AutoSize = False
    tip.TextAlign = ContentAlignment.MiddleRight
    tip.Location = Point(W - int(160 * sc), H - int(42 * sc))
    tip.Size = Size(int(146 * sc), int(30 * sc))
    pop.Controls.Add(tip)

    def _on_paint(_s, e):
        g = e.Graphics
        g.DrawRectangle(Pen(col["sep"], 1.0), 0, 0, pop.Width - 1, pop.Height - 1)
        # 标题区与列表之间一道细线: 没有它, 搜索框下面就是一片空, 分不清哪块是"头"
        y = int(72 * sc)
        g.DrawLine(Pen(col["sep"], 1.0), float(int(12 * sc)), float(y),
                   float(W - int(12 * sc)), float(y))

    pop.Paint += _on_paint
    pop.Deactivate += _close
    pop.KeyDown += lambda _s, e: _close() if int(e.KeyCode) == 27 else None

    # 搜索防抖: 每敲一个字就重建 80 个按钮会卡, 250ms 之后再查
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
