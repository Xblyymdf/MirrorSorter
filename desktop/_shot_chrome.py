# -*- coding: utf-8 -*-
"""顶栏(标签条 + 导航栏)视觉验收: 真渲染成 PNG, 供肉眼比对 Chrome/Edge。

为什么需要它: 顶栏是 WinForms GDI+ 自绘, 不是 HTML —— 改一个半径/间距, 靠读代码
判断不了"好不好看", 也判断不了有没有画错(脚的弧线方向反了不会报错, 只会变丑)。
本脚本把 shell_tabstrip / shell_navbar 装进一个**不显示**的 Form, 直接 DrawToBitmap
出图, 因此不需要真窗口、不抢焦点、可重复。

用法:
    C:\\Python314\\python.exe _shot_chrome.py            # 深色 + 浅色各出一张
    C:\\Python314\\python.exe _shot_chrome.py --light     # 只出浅色
产物: build/probe/chrome_dark.png / chrome_light.png
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "build", "probe")

DARK = ('{"bar":"#0a0c10","active":"#161920","hover":"#1e222c",'
        '"fgon":"#e8eaf0","fgoff":"#969eac","sep":"#343944"}')
LIGHT = ('{"bar":"#e9ecf2","active":"#ffffff","hover":"#eef0f6",'
         '"fgon":"#20242f","fgoff":"#5d6675","sep":"#dfe3ec"}')

# 三种宽度的标签都要看: 首页(无 url, 强调色瓦片) / 正常站点 / 标题很长(省略号)
TABS = [
    {"id": 1, "title": "信息流", "closable": False, "url": "http://127.0.0.1:8000/"},
    {"id": 2, "title": "【硬核】为什么现在的推荐算法越来越难看?- 哔哩哔哩",
     "closable": True, "url": "https://www.bilibili.com/video/BV1xx411c7mD"},
    {"id": 3, "title": "GitHub · Build software better", "closable": True,
     "url": "https://github.com/"},
    {"id": 4, "title": "知乎 - 有问题, 就会有答案", "closable": True,
     "url": "https://www.zhihu.com/"},
]
ACTIVE = 2          # 激活标签放中间: 才能看出它的"脚"压住左右邻居
URL = "https://www.bilibili.com/video/BV1xx411c7mD"


class _Api(object):
    """install_strip / install_navbar 只在回调里用这些, 渲染期不会调到。"""
    def tab_new(self, *a, **k):
        pass

    def tab_close(self, *a, **k):
        pass

    def tab_switch(self, *a, **k):
        pass

    def tab_detach(self, *a, **k):
        pass

    def nav_back(self):
        pass

    def nav_forward(self):
        pass

    def nav_reload(self):
        pass

    def nav_input(self, *a, **k):
        pass

    def toggle_ai_panel(self):
        pass


class _Win(object):
    def __init__(self, form):
        self.native = form


def _render(scheme, path, states=None, width=1200, url=URL):
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    import System.Windows.Forms as WF
    from System.Drawing import Bitmap, Graphics, Point, Rectangle, Size

    from shell_navbar import install_navbar
    from shell_tabstrip import install_strip

    form = WF.Form()
    form.FormBorderStyle = getattr(WF.FormBorderStyle, "None")
    form.ShowInTaskbar = False
    form.StartPosition = WF.FormStartPosition.Manual
    # ★ 必须真的 Show 一次(挪到屏幕外): 不 Show 时子控件 Visible=False 且没有句柄,
    #   DrawToBitmap 会把原生地址栏 TextBox 整个跳过 —— 截出来的图里网址是空的,
    #   看起来像"地址栏坏了"。挪到屏幕外则用户完全看不到。
    form.Location = Point(-4000, -4000)
    form._scale = 1.0
    form.webview = WF.Panel()          # install_strip 会把它当成内容区挪位置
    form.ClientSize = Size(width, 400)
    form.Show()
    WF.Application.DoEvents()

    api, win = _Api(), _Win(form)
    u_strip = install_strip(win, api)
    u_nav = install_navbar(win, api, top=u_strip.bar_h)
    u_strip.set_scheme(scheme)
    u_nav.set_scheme(scheme)
    u_strip(TABS, ACTIVE)
    u_nav(url, True, False)
    for k, v in (states or {}).items():
        # 直接改内部状态: 悬停态在无真鼠标的环境下没法靠事件触发, 但绘制分支必须覆盖到
        if k.startswith("nav:"):
            u_nav.st[k[4:]] = v
        else:
            u_strip.st[k] = v

    strip, nav = u_strip.strip, u_nav.bar
    w = max(strip.Width, nav.Width)
    img = Bitmap(w, strip.Height + nav.Height)
    g = Graphics.FromImage(img)
    b1 = Bitmap(strip.Width, strip.Height)
    strip.DrawToBitmap(b1, Rectangle(0, 0, strip.Width, strip.Height))
    g.DrawImageUnscaled(b1, 0, 0)
    b2 = Bitmap(nav.Width, nav.Height)
    nav.DrawToBitmap(b2, Rectangle(0, 0, nav.Width, nav.Height))
    g.DrawImageUnscaled(b2, 0, strip.Height)
    g.Dispose()

    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    img.Save(path)
    try:
        form.Close()
    except Exception:
        pass
    print("saved %s  (%dx%d)  strip_h=%d nav_h=%d"
          % (path, img.Width, img.Height, strip.Height, nav.Height))
    return img


def main(argv):
    which = "both"
    if "--light" in argv:
        which = "light"
    elif "--dark" in argv:
        which = "dark"
    if which in ("both", "dark"):
        _render(DARK, os.path.join(OUT, "chrome_dark.png"))
    if which in ("both", "light"):
        _render(LIGHT, os.path.join(OUT, "chrome_light.png"))
    # 搜索态(空地址): 地址栏左侧应该是放大镜, 而不是地球/锁
    _render(DARK, os.path.join(OUT, "chrome_search.png"), url="")
    # 悬停态全家桶: 悬停标签 / 悬停 ✕ / 悬停 + / 悬停窗口关闭键 / 悬停地址栏
    # 窄窗: 标签会被挤窄(旧版这里会把最右标签的 ✕ 顶出栏外, 用户截过图)
    _render(DARK, os.path.join(OUT, "chrome_narrow.png"), width=760,
            states={"hover_tab": 3})
    _render(DARK, os.path.join(OUT, "chrome_hover.png"), states={
        "hover_tab": 3, "hover_close": 3, "hover_add": True, "hover_btn": "close",
        "nav:hover": "back", "nav:tb_hover": True, "nav:ai_on": True,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
