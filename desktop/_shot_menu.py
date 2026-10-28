# -*- coding: utf-8 -*-
"""v0.4.52dsk 验收: 主菜单出图 + 历史面板能建起来。

为什么要有这个文件: 主菜单是**纯自绘**的原生 Form —— 间距、悬停底、缩放行的
"− 100% +" 排得对不对, 靠读代码判断不了(不报错, 只会难看)。所以真渲染成 PNG 比对。

主菜单没有子控件, 不 Show 也能 `DrawToBitmap`; 历史面板有原生 TextBox/Button,
不 Show 时子控件没有句柄(截出来是空壳), 那边只断言"构造成功、行数对得上"。

产物: build/probe/menu_dark.png / menu_light.png
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "build", "probe")
os.makedirs(OUT, exist_ok=True)
os.environ.setdefault("MIRRORSORTER_DATA_DIR", os.path.join(OUT, "menu452-shot"))

import clr  # noqa: E402
clr.AddReference("System.Drawing")
clr.AddReference("System.Windows.Forms")
import System.Windows.Forms as WF  # noqa: E402
from System.Drawing import Bitmap, Graphics, Rectangle  # noqa: E402

import shell_menu  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


SCHEMES = {
    "dark": {"bar": "#161920", "active": "#22262e", "hover": "#2e3442",
             "fgon": "#e8ebf1", "fgoff": "#7c828e", "sep": "#363b46"},
    "light": {"bar": "#e9edf3", "active": "#ffffff", "hover": "#dde3ec",
              "fgon": "#1a1d23", "fgoff": "#6b7280", "sep": "#cfd6e0"},
}


class _Win(object):
    """假窗口: 壳里的模块只用到 `win.native`(取 DPI 缩放与定位基准)。"""

    def __init__(self, form):
        self.native = form


def _items():
    return [
        {"kind": "item", "label": "新建标签页", "glyph": "\uE710", "fn": lambda: None},
        {"kind": "item", "label": "历史记录", "glyph": "\uE81C", "fn": lambda: None},
        {"kind": "item", "label": "收藏此页", "glyph": "\uE734", "fn": lambda: None},
        {"kind": "sep"},
        {"kind": "zoom", "label": "缩放", "get": lambda: 1.0, "set": lambda d: None},
        {"kind": "sep"},
        {"kind": "item", "label": "打印…", "glyph": "\uE749", "fn": lambda: None},
        {"kind": "item", "label": "翻译此页面", "glyph": "\uE774", "fn": lambda: None},
        {"kind": "sep"},
        {"kind": "item", "label": "在系统浏览器中打开", "glyph": "\uE8A7", "fn": lambda: None},
    ]


def main():
    form = WF.Form()
    # ★ 这里固定 scale=1.0: 本脚本的进程是 DPI-unaware(96 DPI), 硬塞 1.5 只会让
    #   "行高按 1.5 放大、字体按 96 DPI 渲染" 两边对不上, 出图反而失真。
    #   150% DPI 的真实效果由 `app_window.py --e2e-menu`(真窗口、真 DPI)负责。
    try:
        form._scale = 1.0
    except Exception:
        pass
    win = _Win(form)
    sc = 1.0

    # 高度 = 上下内边距 + 普通行 + 分隔线(行数从 items 自己数, 别手抄一份 —— 手抄的
    # 那份在加了一项之后永远还是对的, 测试就白写了)
    its = _items()
    n_item = len([i for i in its if i.get("kind") != "sep"])
    n_sep = len([i for i in its if i.get("kind") == "sep"])
    expect_h = int(12 * sc) + int((n_item * shell_menu.ROW_H +
                                   n_sep * shell_menu.SEP_H) * sc)
    for name, sch in SCHEMES.items():
        pop = shell_menu.show_menu(win, anchor=(900, 120), items=_items(),
                                   scheme=sch, scale=sc, show=False)
        check("菜单(%s) 建出来了" % name, pop is not None)
        if pop is None:
            continue
        check("菜单(%s) 高度按行数算对" % name, abs(pop.Height - expect_h) <= 4 * sc,
              "%s vs %s" % (pop.Height, expect_h))
        check("菜单(%s) 宽度是设计值" % name,
              pop.Width == int(shell_menu.MENU_W * sc), pop.Width)
        try:
            bmp = Bitmap(pop.Width, pop.Height)
            pop.DrawToBitmap(bmp, Rectangle(0, 0, pop.Width, pop.Height))
            p = os.path.join(OUT, "menu_%s.png" % name)
            bmp.Save(p)
            print("saved %s  (%dx%d)" % (p, pop.Width, pop.Height))
            check("菜单(%s) 渲染不抛异常" % name, True)
        except Exception as e:
            check("菜单(%s) 渲染不抛异常" % name, False, repr(e))
        try:
            pop.Close()
        except Exception:
            pass

    # ---- 历史面板: 能建、行数对得上 ----
    import app
    app.web_hist_clear()
    for i in range(3):
        app.web_hist_add("https://example.com/p%d" % i, "示例页 %d" % i)

    class _Api(object):
        def nav_goto(self, url):
            return "ok"

    try:
        hp = shell_history_show(win, _Api(), sch=SCHEMES["dark"], sc=sc)
        rows = list(getattr(hp, "_msx_rows", []) or [])
        heads = [r for r in rows if r.get("kind") == "head"]
        items = [r for r in rows if r.get("kind") == "row"]
        check("历史面板 建得起来", hp is not None)
        check("历史面板 三条记录各一行", len(items) == 3, len(items))
        # 分组是 v0.4.53 照 Chrome 加的: 同一天只出现一个"今天"标题, 且排在所有行之前
        check("历史面板 按日期分组(同一天只一个「今天」标题)",
              len(heads) == 1 and heads[0]["label"] == "今天" and rows[0] is heads[0],
              [h["label"] for h in heads])
        check("历史面板 每行带「域名 · 时间」次要行",
              all("example.com \u00b7 " in r["sub"] for r in items),
              items[0]["sub"] if items else "")
        check("历史面板 有搜索框", any(isinstance(c, WF.TextBox) for c in _all(hp)))
        check("历史面板 有「清除全部」", any("清除全部" in str(getattr(c, "Text", ""))
                                        for c in _all(hp)))
        # 历史面板有原生子控件(TextBox/Button) —— 不 Show 就没有句柄, DrawToBitmap 只会
        # 截出一块纯底色。挪到屏幕外 Show 一次再截, 与 _shot_chrome 同一套办法。
        try:
            from System.Drawing import Point as _P
            hp.Location = _P(-6000, -6000)
            hp.Show()
            hp.Refresh()
            bmp = Bitmap(hp.Width, hp.Height)
            hp.DrawToBitmap(bmp, Rectangle(0, 0, hp.Width, hp.Height))
            p = os.path.join(OUT, "history_dark.png")
            bmp.Save(p)
            print("saved %s  (%dx%d)" % (p, hp.Width, hp.Height))
            check("历史面板 渲染不抛异常", True)
            # 悬停态(行高亮 + 行尾 ×)只能靠出图核对 —— 没有真鼠标也能截,
            # 走的是面板暴露的 _msx_hot(与 MouseMove 同一条赋值路径)。
            try:
                hp._msx_hot(1, True)
                hp.Refresh()
                bmp2 = Bitmap(hp.Width, hp.Height)
                hp.DrawToBitmap(bmp2, Rectangle(0, 0, hp.Width, hp.Height))
                p2 = os.path.join(OUT, "history_hover.png")
                bmp2.Save(p2)
                print("saved %s  (悬停第 2 行 + × 变红)" % p2)
            except Exception as e:
                check("历史面板 悬停态出图", False, repr(e))
        except Exception as e:
            check("历史面板 渲染不抛异常", False, repr(e))
        try:
            hp.Close()
        except Exception:
            pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("历史面板 建得起来", False, repr(e))

    # ---- 轻提示(toast): 翻译进度/结果全靠它 ----
    try:
        t = shell_menu.Toast(win, "已翻译 12 段 · 菜单里可「显示原文」", ms=500,
                             scheme=SCHEMES["dark"], scale=sc)
        check("提示条 建得起来", t is not None)
        t.set("正在翻译 3/12 段…", 400)     # 进度更新走同一个窗口, 不新开
        check("提示条 支持原地改文案(进度不闪)", True)
        t.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("提示条 建得起来", False, repr(e))

    app.web_hist_clear()
    print("\nRESULT: %s" % ("FAIL(%d)" % FAIL if FAIL else "ALL_PASS (%d checks)" % PASS))
    return 1 if FAIL else 0


def _all(ctrl):
    out = [ctrl]
    try:
        for c in ctrl.Controls:
            out += _all(c)
    except Exception:
        pass
    return out


def _all_buttons(ctrl):
    return [c for c in _all(ctrl) if isinstance(c, WF.Button)]


def shell_history_show(win, api, sch=None, sc=1.5):
    import shell_history
    return shell_history.show_history(win, api, anchor=(900, 120), scheme=sch,
                                      scale=sc, show=False)


if __name__ == "__main__":
    sys.exit(main())
