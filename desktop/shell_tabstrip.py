# -*- coding: utf-8 -*-
"""窗口顶部原生"浏览器式"标签栏(WinForms 自绘, 仅供 app_window.py 窗口壳使用)。

原理: pywebview 的 WinForms 窗体(window.native)里,
- 标签栏 = Dock=Top 的 Panel, Paint 事件自绘标签(圆角/标题/✕), MouseClick 命中测试;
- WebView2 控件从 Dock=Fill 改为手动定位在标签栏下方(Anchor 四边), 窗口缩放自动跟随。
标签栏属于窗口 chrome, 不向页面注入任何元素 —— 任何网站都不被遮挡、行为不变。
窗口为无边框(frameless): 本栏兼任标题栏 —— 空白处可拖动窗口/双击最大化,
右端有 ─ □ ✕ 窗口按钮; 无边框缩放由 shell_pages.init 的 WS_THICKFRAME 补丁恢复。

注意: pythonnet 对 .NET 虚方法重写(OnPaint 等)不可靠, 必须用 Paint/MouseClick 事件订阅。

视觉(v0.4.43dsk 重做, 对标 Chrome/Edge) —— 差距在**结构**不在配色, 改色救不回来:
- 标签相邻无缝隙, 激活标签顶角圆角 + 底边平直, 且左右各有向外张开的"脚";
- 激活标签**下沿与工具栏齐平**, 且与工具栏同色 → 视觉上与下方内容连成一片;
  所以工具栏(shell_navbar)的底色必须取 scheme 的 active, 标签条取 bar;
- 分隔线只画在两个"都非激活/非悬停"的相邻标签之间;
- ✕ / + / ─ □ ✕ 全部改成 1px 矢量线(旧版用文字符号, 粗细不匀、基线乱飘);
- 每个标签带 16px 站点图标瓦片(域名取色 + 首字母, 没有真实 favicon 时的兜底)。
验收: `_shot_chrome.py` 把本模块与导航栏真渲染成 PNG(深/浅/悬停/窄窗四张), 肉眼比对。
"""
import re
import threading


# ---------------------------------------------------------------- 撕出悬浮窗的拖拽判定
# ★ 提到模块级是为了**可单测**。旧版把判据内联在 MouseMove 里:
#     abs(dx) + abs(dy) > 22
#   —— 任意方向的曼哈顿距离, 于是"点一下标签"手抖二十来像素就被拽成悬浮窗(用户反馈);
#   而"往下拽"因为标签栏只有 ~32px 高、拖出去立刻触发 MouseLeave 被取消, 反而永远
#   拽不出来。方向语义整个是反的, 而内联写法在无 GUI 环境下没法回归测试。
DETACH_DY = 26     # 向下拖出的阈值(逻辑像素 × scale); 标签栏高 ≈ 36 × scale
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
    from System.Drawing import (Color, ContentAlignment, Font, FontStyle, Pen,
                                Rectangle, SolidBrush, StringFormat,
                                StringAlignment, StringFormatFlags, StringTrimming,
                                RectangleF, Point, Size)
    from System.Drawing.Drawing2D import GraphicsPath, SmoothingMode
    import System.Windows.Forms as WinForms

    form = win.native
    scale = float(getattr(form, "_scale", 1.0) or 1.0)
    # ---- 比例对标 Chrome/Edge ----
    # 标签条 36 逻辑像素(旧版 32 偏矮, 标题挤在一起显得廉价); 标签从距顶 5px 起画,
    # **下沿与工具栏齐平** —— 齐平 + 与工具栏同色, 激活标签才会和下方内容连成一片。
    # 旧版标签是"浮在深色条上的一张独立圆角小卡片", 这是最致命的差距: 结构不像浏览器,
    # 再怎么调颜色也救不回来。
    bar_h = max(30, int(36 * scale))
    tab_top = int(5 * scale)          # 标签顶部留白(上方露出的是标签条底色)
    tab_h = bar_h - tab_top           # 标签高度 = 一直到栏底(不减去底部留白)
    font = Font("Microsoft YaHei", 8.5 * scale)
    btn_font = Font("Microsoft YaHei", 10.0 * scale)   # 自绘弹出菜单用
    fav_font = Font("Segoe UI", 8.0 * scale, FontStyle.Bold)   # 站点图标瓦片里的字母
    TAB_ACTIVE = Color.FromArgb(22, 25, 32)     # 激活标签(= 工具栏底色, 两者必须同色)
    TAB_HOVER = Color.FromArgb(30, 34, 44)      # 非激活悬停
    BG = Color.FromArgb(10, 12, 16)             # 标签条底色(比工具栏深一档)
    FG_ON = Color.White
    FG_OFF = Color.FromArgb(150, 158, 172)
    # 站点图标瓦片的兜底配色(没有真实 favicon 时用首字母色块, Edge 也是这么兜的)
    FAV_COLORS = ((0x4C, 0x8D, 0xFF), (0xE0, 0x5C, 0x7B), (0x3F, 0xB9, 0x8C),
                  (0xE8, 0xA3, 0x3D), (0x8B, 0x77, 0xF0), (0x2F, 0xAE, 0xC9),
                  (0xE2, 0x6B, 0x4C), (0x5E, 0x8C, 0x3E))
    st = {"tabs": [], "active": 0, "hover_btn": None, "hover_tab": None,
          "hover_add": False,   # 「+」新建按钮悬停
          "hover_close": None,  # 指针落在哪个标签的 ✕ 上(画圆形悬停底)
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
    sf_c = StringFormat()   # 居中(站点图标字母/地球锁形): 字形再宽也不会越出保留区被裁
    sf_c.Alignment = StringAlignment.Center
    sf_c.LineAlignment = StringAlignment.Center

    ADD_W = int(28 * scale)   # 标签条右端的「+」新建按钮
    TAB_MIN = int(72 * scale)    # 标签最窄(再窄标题就没法读了)
    TAB_MAX = int(232 * scale)   # 标签最宽

    def _layout(width):
        """标签几何: **相邻无缝隙**(靠 1px 分隔线区分, 与 Chrome 一致)。

        旧版留 4px 间隙 + 每张标签独立圆角 → 一排"小卡片", 不像浏览器标签。
        """
        n = len(st["tabs"])
        if not n:
            return []
        margin = int(8 * scale)
        btn_zone = BTN_W() * 3 + int(4 * scale)
        slack = width - margin - btn_zone - ADD_W - int(10 * scale)
        tw = min(TAB_MAX, max(TAB_MIN, slack // n))
        if tw * n > slack:      # 标签太多: 宁可挤窄, 也不能顶出栏外把最右边的 ✕ 裁掉
            tw = max(int(44 * scale), slack // n)
        return [(margin + i * tw, tw) for i in range(n)]

    def _add_rect():
        """「+」按钮矩形 —— 紧贴最后一个标签右侧; 标签多到挤满时钉在窗口按键左侧。"""
        boxes = _layout(strip.Width)
        x = (boxes[-1][0] + boxes[-1][1] + int(8 * scale)) if boxes else int(10 * scale)
        limit = strip.Width - BTN_W() * 3 - ADD_W - int(6 * scale)
        x = max(int(4 * scale), min(x, limit))
        return (x, tab_top, ADD_W, tab_h)

    def _hit_add(x, yv):
        ax, ay, aw, ah = _add_rect()
        return ax <= x <= ax + aw and ay <= yv <= ay + ah

    def _close_rect(x, tw):
        """标签 ✕ 的圆形热区(正方形框, 里面画圆)。"""
        d = int(20 * scale)
        return (x + tw - d - int(3 * scale), tab_top + (tab_h - d) // 2, d, d)

    def _rounded_path(x, y, w, h, rad):
        p = GraphicsPath()
        d = rad * 2
        p.AddArc(x, y, d, d, 180, 90)             # 左上
        p.AddArc(x + w - d, y, d, d, 270, 90)     # 右上
        p.AddArc(x + w - d, y + h - d, d, d, 0, 90)    # 右下
        p.AddArc(x, y + h - d, d, d, 90, 90)      # 左下
        p.CloseFigure()
        return p

    def _tab_path(x, y, w, hh, rad, foot):
        """浏览器标签轮廓: 上方圆角 + 下方两只向外张开的"脚", 底边平直。

        foot>0 时底边比上边左右各宽 foot, 用四分之一圆过渡 —— 这就是 Chrome/Edge
        标签那种"坐"在内容区上的形状。激活标签的底边与工具栏齐平且同色, 于是两者
        在视觉上连成一体; 脚的溢出部分会压住左右邻居, 所以激活标签必须**最后画**。

        foot=0 → 普通的"上方圆角、下方平底"矩形(非激活悬停底色用)。
        """
        p = GraphicsPath()
        r = max(1, int(rad))
        f = max(0, int(foot))
        b = y + hh
        p.AddArc(x, y, r * 2, r * 2, 180, 90)                    # 左上角
        p.AddArc(x + w - r * 2, y, r * 2, r * 2, 270, 90)        # 右上角
        if f > 0:
            p.AddLine(x + w, y + r, x + w, b - f)                # 右边直下
            p.AddArc(x + w, b - f * 2, f * 2, f * 2, 180, -90)   # 右脚
            p.AddLine(x + w + f, b, x - f, b)                    # 底边
            p.AddArc(x - f * 2, b - f * 2, f * 2, f * 2, 90, -90)   # 左脚
            p.AddLine(x, b - f, x, y + r)                        # 左边直上
        else:
            p.AddLine(x + w, y + r, x + w, b)
            p.AddLine(x + w, b, x, b)
            p.AddLine(x, b, x, y + r)
        p.CloseFigure()
        return p

    def _fav(t):
        """(瓦片底色, 首字母) —— 没有真实 favicon 时的兜底站点图标。

        域名取色(同一站点颜色稳定), 首页/固定页用强调色 + 标题首字。
        """
        if not t.get("closable"):
            return st["accent"], (str(t.get("title") or "?")[:1] or "?")
        host = ""
        try:
            m = re.match(r"^https?://([^/:?#]+)", str(t.get("url") or ""))
            if m:
                host = m.group(1).lower()
                if host.startswith("www."):
                    host = host[4:]
        except Exception:
            host = ""
        if not host:
            return st["accent"], (str(t.get("title") or "?")[:1] or "?")
        h = 0
        for ch in host:
            h = (h * 131 + ord(ch)) & 0xFFFFFF
        c = FAV_COLORS[h % len(FAV_COLORS)]
        return Color.FromArgb(c[0], c[1], c[2]), host[:1].upper()

    def on_paint(_sender, e):
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        g.Clear(st["bar"])
        boxes = _layout(strip.Width)
        rad = int(8 * scale)                     # 顶角半径(Chrome ≈ 8)
        foot = max(2, int(4 * scale))            # 标签底部"脚"的外扩量
        n = len(st["tabs"])
        act_id, hov_id = st["active"], st["hover_tab"]
        pen_sep = Pen(st["sep"], 1.0)

        # ---- 1) 分隔线: 只在"两侧都不是激活/悬停"的相邻标签之间(Chrome 同款) ----
        for i in range(1, min(n, len(boxes))):
            a, c = st["tabs"][i - 1], st["tabs"][i]
            if a["id"] == act_id or c["id"] == act_id or hov_id in (a["id"], c["id"]):
                continue
            x = float(boxes[i][0])
            g.DrawLine(pen_sep, x, float(tab_top + int(8 * scale)),
                       x, float(bar_h - int(9 * scale)))

        # ---- 2) 标签: 非激活先画, 激活最后画在最上层(它的"脚"要压住左右邻居) ----
        for i in sorted(range(n), key=lambda k: (st["tabs"][k]["id"] == act_id)):
            if i >= len(boxes):
                continue
            t = st["tabs"][i]
            x, tw = boxes[i]
            act = (t["id"] == act_id)
            hov = (t["id"] == hov_id and not act)
            if act:
                g.FillPath(SolidBrush(st["tab_active"]),
                           _tab_path(x, tab_top, tw, tab_h, rad, foot))
            elif hov:
                # 悬停: 内缩 1px 的"上圆下平"底, 不画脚(脚是激活态专属)
                g.FillPath(SolidBrush(st["tab_hover"]),
                           _tab_path(x + int(1 * scale), tab_top,
                                     max(2, tw - int(2 * scale)), tab_h, rad, 0))
            # v0.3.1.44: 正在往下拖这个标签 → 染强调色, 让用户看得见"再往下就是悬浮窗"
            if st.get("drag_hint") and st.get("drag_tid") == t["id"]:
                try:
                    ac = st["accent"]
                    dp = _tab_path(x, tab_top, tw, tab_h, rad, foot)
                    g.FillPath(SolidBrush(Color.FromArgb(64, ac.R, ac.G, ac.B)), dp)
                    g.DrawPath(Pen(ac, max(1.0, 2.0 * scale)), dp)
                except Exception:
                    pass
            fg = st["fg_on"] if act else st["fg_off"]
            # ---- 站点图标瓦片: 16px 圆角方块 + 首字母(窄标签自动省掉) ----
            pad = int(10 * scale)
            tile = int(16 * scale)
            left = x + pad
            if tw >= int(104 * scale):
                fc, letter = _fav(t)
                ty_ = tab_top + (tab_h - tile) // 2
                g.FillPath(SolidBrush(fc),
                           _rounded_path(left, ty_, tile, tile, int(4 * scale)))
                g.DrawString(letter, fav_font, SolidBrush(Color.White),
                             RectangleF(float(left), float(ty_), float(tile), float(tile)),
                             sf_c)
                left += tile + int(8 * scale)
            close_w = int(26 * scale) if t.get("closable") else int(4 * scale)
            right = x + tw - close_w
            if right - left >= int(6 * scale):
                g.DrawString(t.get("title") or "页面", font, SolidBrush(fg),
                             RectangleF(float(left), float(tab_top),
                                        float(right - left), float(tab_h)), sf)
            # ---- ✕: 1px 矢量细线 + 圆形悬停底(旧版是 10pt 的"✕"字符, 又粗又虚) ----
            if t.get("closable"):
                cr = _close_rect(x, tw)
                if st.get("hover_close") == t["id"]:
                    g.FillEllipse(SolidBrush(Color.FromArgb(56, st["bar"].R,
                                                            st["bar"].G, st["bar"].B)),
                                  float(cr[0]), float(cr[1]), float(cr[2]), float(cr[3]))
                cx = cr[0] + cr[2] / 2.0
                cy = cr[1] + cr[3] / 2.0
                s = max(3.0, 3.6 * scale)
                xp = Pen(fg, max(1.0, 1.1 * scale))
                g.DrawLine(xp, cx - s, cy - s, cx + s, cy + s)
                g.DrawLine(xp, cx - s, cy + s, cx + s, cy - s)

        # ---- 3) 「+」新建标签: 细线加号 + 圆形悬停底 ----
        ax, ay, aw, ah = _add_rect()
        acx, acy = ax + aw / 2.0, ay + ah / 2.0
        arr = min(aw, ah) / 2.0 - 1.0
        if st.get("hover_add"):
            g.FillEllipse(SolidBrush(st["tab_hover"]), float(acx - arr), float(acy - arr),
                          float(arr * 2), float(arr * 2))
        pp = Pen(st["fg_on"] if st.get("hover_add") else st["fg_off"],
                 max(1.0, 1.2 * scale))
        ps = max(4.0, 4.5 * scale)
        g.DrawLine(pp, acx - ps, acy, acx + ps, acy)
        g.DrawLine(pp, acx, acy - ps, acx, acy + ps)

        # ---- 4) 右端窗口按钮: 细线矢量图标(旧版用 ─ □ ✕ 文字符号, 粗细不匀、基线乱飘) ----
        # (Pen 必须用顶部 import: 函数中段 import 会让前面的绘制碰到未绑定局部名)
        bw = BTN_W()
        zx = strip.Width - bw * 3
        is_max = (form.WindowState == WinForms.FormWindowState.Maximized
                  or fill_state["saved"] is not None)
        for i, name in enumerate(("min", "max", "close")):
            bx = zx + bw * i
            hov = (st["hover_btn"] == name)
            bg = st["bar"]
            if hov:
                bg = Color.FromArgb(232, 17, 35) if name == "close" else st["tab_hover"]
                g.FillRectangle(SolidBrush(bg), float(bx), 0.0, float(bw), float(bar_h))
            fg = Color.White if (hov and name == "close") else st["fg_on"]
            cx, cy = bx + bw / 2.0, bar_h / 2.0
            s = max(4.0, 5.0 * scale)
            wp = Pen(fg, max(1.0, 1.0 * scale))
            if name == "min":
                g.DrawLine(wp, cx - s, cy, cx + s, cy)
            elif name == "max":
                if is_max:
                    # 还原图标: 两方块右上角部分重叠(前块左下完整, 后块右上被遮)
                    s2, off = max(3.0, 4.0 * scale), max(2.0, 2.4 * scale)
                    x0, y0 = cx - s2 - off / 2.0, cy - s2 - off / 2.0
                    g.DrawRectangle(wp, x0 + off, y0, s2 * 2, s2 * 2)   # 后块(右上)
                    g.FillRectangle(SolidBrush(bg), float(x0 - 1), float(y0 + off - 1),
                                    float(s2 * 2 + 2), float(s2 * 2 + 2))   # 遮重叠线
                    g.DrawRectangle(wp, x0, y0 + off, s2 * 2, s2 * 2)   # 前块(左下)
                else:
                    g.DrawRectangle(wp, cx - s, cy - s, s * 2, s * 2)
            else:
                g.DrawLine(wp, cx - s, cy - s, cx + s, cy + s)
                g.DrawLine(wp, cx - s, cy + s, cx + s, cy - s)

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

    BTN_W = lambda: int(46 * scale)   # ─ □ ✕ 窗口按钮宽(Chrome 是 46 逻辑像素)

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

    def _hit_close(x, yv):
        """指针是否落在某个标签的 ✕ 圆钮上 → 返回标签 id(画圆形悬停底用)。"""
        boxes = _layout(strip.Width)
        for i, t in enumerate(st["tabs"]):
            if i >= len(boxes):
                break
            bx, tw = boxes[i]
            if not t.get("closable") or not (bx <= x <= bx + tw):
                continue
            cx, cy, cw, chh = _close_rect(bx, tw)
            if cx <= x <= cx + cw and cy <= yv <= cy + chh:
                return t["id"]
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
        hc = _hit_close(args.X, args.Y)
        if hc != st.get("hover_close"):
            st["hover_close"] = hc
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
        if st.get("hover_close") is not None:
            st["hover_close"] = None
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
            # ✕ 的命中区与绘制区共用 _close_rect, 不再各写一份宽度(旧版两处 24/20 不一致)
            if t.get("closable") and _hit_close(args.X, args.Y) == t["id"]:
                st["hover_close"] = None
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

    def _mix(c, base, k):
        """把 c 按 k 比例混向 base(0=base, 1=c)。"""
        return Color.FromArgb(int(base.R + (c.R - base.R) * k),
                              int(base.G + (c.G - base.G) * k),
                              int(base.B + (c.B - base.B) * k))

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
            st["fg_on"] = _c("fgon", st["fg_on"])
            st["fg_off"] = _c("fgoff", st["fg_off"])
            st["sep"] = _c("sep", st["sep"])
            # ★ 悬停底色不用页面给的 hover, 而是"栏底 → 激活"之间取中点。
            #   页面给的 hover 在深色下比 active 还亮(L15 vs L11), 悬停一个非激活标签
            #   会比激活标签更显眼 —— 层次整个反了。中点保证: 栏底 < 悬停 < 激活。
            st["tab_hover"] = _mix(st["tab_active"], st["bar"], 0.5)

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
    # 视觉回归用出口(与 shell_ai 暴露 st 的做法一致): _shot_chrome.py 直接驱动这几项,
    # 不依赖真鼠标/真窗口, 就能把顶栏渲染成图做肉眼与像素级比对。
    update.st = st
    update.strip = strip
    update.set_scheme = set_bg_scheme
    return update
