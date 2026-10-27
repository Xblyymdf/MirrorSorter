# -*- coding: utf-8 -*-
"""v0.4.42dsk 归档层验收：右侧 AI 面板的四个新文件到底有没有打进 exe。

为什么要单独验：`shell_ai` 是在 `app_window.gui_ready()` 里**局部 import** 的
（`from shell_ai import install_ai_panel`），PyInstaller 静态分析漏掉它也不会报错 ——
源码态一切正常，打包后一点开网页标签就 ImportError，面板根本不出现。
同理 `web/panel.*` 是新增静态资源，靠 `--add-data web;web` 整目录带进去，
但必须逐字节比对，确认打包时采到的是最终版（改完 web/ 没重新打包是最常见的坑）。
"""
import os
import re
import sys

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "build", "dist")
def _latest_exe():
    """默认取 dist 下最新的 洞鉴虚明v*.exe —— 这样脚本不用每版改一遍。"""
    if not os.path.isdir(DIST):
        return ""
    cands = [os.path.join(DIST, f) for f in os.listdir(DIST)
             if f.startswith("洞鉴虚明v") and f.endswith(".exe") and "安装包" not in f]
    if not cands:
        return ""
    return max(cands, key=lambda p: (os.path.getmtime(p), p))


exe = sys.argv[1] if len(sys.argv) > 1 else _latest_exe()
if not exe or not os.path.isfile(exe):
    raise SystemExit("exe 不存在: " + (exe or "(dist 下没有 洞鉴虚明v*.exe)"))
print("校验:", os.path.basename(exe), "%.1f MB" % (os.path.getsize(exe) / 1048576.0))

# 版本号从 exe 文件名反推(洞鉴虚明v0.4.44dsk.exe → v0.4.44dsk): 升版本不用改本脚本
_m = re.search(r"(v[0-9A-Za-z.]+)\.exe$", os.path.basename(exe))
VER = _m.group(1) if _m else ""

PASS = 0
FAILS = []


def ok(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("PASS  " + name + (("  " + str(extra)) if extra else ""))
    else:
        FAILS.append(name)
        print("FAIL  " + name + (("  " + str(extra)) if extra else ""))


car = CArchiveReader(exe)
toc = car.toc  # name -> (offset, length, uncompressed, compress, typecode)
names = set(toc)

# ---------- 1. 新增静态资源必须在归档里，且字节与源码一致 ----------
for rel in ("web/panel.html", "web/panel.js", "web/panel.css"):
    hit = [n for n in names if n.replace("\\", "/").endswith(rel)]
    ok("归档含 " + rel, len(hit) == 1, hit[:2])
    if hit:
        blob = car.extract(hit[0])
        src = open(os.path.join(HERE, rel.replace("/", os.sep)), "rb").read()
        ok("归档 " + rel + " 与源码字节一致", blob == src,
           "%d vs %d bytes" % (len(blob or b""), len(src)))

# ---------- 2. 旧的静态资源仍在（没被 --clean 打漏），且必须与源码逐字节一致 ----------
# 「改完 web/ 没重新打包」是这个项目最常见的坑，所以这里不只查存在，还查字节。
for rel in ("web/index.html", "web/app.js", "web/style.css"):
    hit = [n for n in names if n.replace("\\", "/").endswith(rel)]
    ok("归档仍含 " + rel, len(hit) == 1, hit[:2])
    if hit:
        blob = car.extract(hit[0])
        src = open(os.path.join(HERE, rel.replace("/", os.sep)), "rb").read()
        ok("归档 " + rel + " 与源码字节一致", blob == src,
           "%d vs %d bytes" % (len(blob or b""), len(src)))

# ---------- 2b. 分栏预览线必须挂 <html> ----------
# v0.4.48: 界面缩放走 document.body.style.zoom，预览线原来挂在 <body> 里，
# translate 位移被 zoom 再乘一次 → 字体「中/大」时那条绿线飘进右栏。
# 这里直接扫归档里的 app.js，确认打的不是旧版本。
_js_hit = [n for n in names if n.replace("\\", "/").endswith("web/app.js")]
if _js_hit:
    _js = (car.extract(_js_hit[0]) or b"").decode("utf-8", "replace")
    ok("归档 app.js: 预览线挂 <html>(不受 body{zoom} 二次缩放)",
       "document.documentElement.appendChild(splitGhost)" in _js
       and "document.body.appendChild(splitGhost)" not in _js)
    ok("归档 app.js: 拖动位移按 zoom 换算(鼠标位移除 z / 线位移乘 z)",
       "function uiZoom()" in _js and "sign * ((ev.clientX - x0) / z)" in _js
       and "(target - w0) * sign * z" in _js)

# ---------- 3. PYZ 里必须有 shell_ai（局部 import 最容易被漏掉） ----------
pyz_name = [n for n in names if n.endswith(".pyz") or n.endswith("PYZ-00.pyz")]
pyz_mods = set()
if pyz_name:
    # PYZ 是 CArchive 里的一段，toc 里的 dpos 是相对 PKG 数据区的偏移，
    # 不能直接当绝对 offset 喂给 ZlibArchiveReader —— 先取字节落临时文件再读。
    import tempfile
    blob = car.extract(pyz_name[0])
    tmp = os.path.join(tempfile.mkdtemp(prefix="msx-pyz-"), "PYZ.pyz")
    with open(tmp, "wb") as f:
        f.write(blob)
    pyz = ZlibArchiveReader(tmp, 0)
    pyz_mods = set(pyz.toc)
    ok("PYZ 含 shell_ai（局部 import 的模块）", "shell_ai" in pyz_mods,
       "共 %d 个模块" % len(pyz_mods))
    for m in ("shell_pages", "shell_navbar", "app_window", "shell_tray",
              "shell_single"):
        ok("PYZ 含 " + m, m in pyz_mods)
else:
    ok("找到 PYZ", False, pyz_name[:2])

# ---------- 4/5. 关键字符串常量真的在字节码里 ----------
# pyz.extract 给的是 code object；字符串常量在 co_consts 里（小整数走 LOAD_SMALL_INT，
# 但这里查的都是名字/路由，是字符串，扫 co_consts 就够）。
import types


def consts(code, out=None):
    """模块里出现过的所有标识符/字面量。

    函数名和局部变量名**不在** co_consts 里：`def foo()` 的名字是 code.co_name，
    被引用的全局名在 co_names，**局部变量在 co_varnames**；被内层函数引用的局部变量
    会变成闭包变量, 落在 **co_cellvars**（`FAV_COLORS` 就是这样 —— 只查 varnames 会
    把"明明打进去了"误判成 FAIL）。
    """
    if out is None:
        out = []
    out.append(code.co_name)
    out.extend(code.co_names)
    out.extend(code.co_varnames)
    out.extend(code.co_cellvars)
    for c in code.co_consts:
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, types.CodeType):
            consts(c, out)
    return out


def has(code, kw):
    return any(kw == c or kw in c for c in consts(code))


if "shell_ai" in pyz_mods:
    c = pyz.extract("shell_ai")
    # 注意：壳侧接的是**去掉 browser_ 前缀**的 action 名（app.py 才用 browser_* 全名），
    # 所以这里查 "click" 而不是 "browser_click"。
    for kw in ("install_ai_panel", "snapshot", "click", "type", "scroll", "media",
               "find", "eval", "history", "ui_ai_panel_w", "set_right_inset"):
        ok("shell_ai 字节码含 " + kw, has(c, kw))

if "app" in pyz_mods:
    c = pyz.extract("app")
    for kw in ("set_browser_bridge", "_AGENT_BROWSER_TOOLS", "api/panel/state",
               "panel.html",
               # v0.4.50dsk 后台常驻: 开机自启注册表 + 关窗换一批
               "autostart_apply", "_AUTOSTART_NAME", "autostart_sync",
               "feed_rotate", "ui_close_to_tray", "ui_autostart"):
        ok("app 字节码含 " + kw, has(c, kw))

# ---------- 6. 顶栏视觉重做（v0.4.43dsk）真的进了包 ----------
# 这两个模块是 app_window 顶层 import 的，不会漏；但**改完没重新打包**是常见坑
# （PyInstaller 在 Analysis 阶段采集源码，打包中途改文件不会进包）。
for mod, kws in (("shell_tabstrip", ("install_strip", "_tab_path", "_fav", "_mix",
                                     "FAV_COLORS", "hover_close", "set_bg_scheme",
                                     # v0.4.44dsk: 首页画软件图标 + 其它标签画真实 favicon
                                     "load_app_logo", "_logo_path", "_tab_icon",
                                     "_LOGO_CACHE", "favicons")),
                 ("shell_navbar", ("install_navbar", "_pill_rect", "_recalc_omni",
                                   "_circle", "NAV_H",
                                   # v0.4.44dsk: 空/非 http 的地址栏给放大镜
                                   "search",
                                   # v0.4.50dsk: 地址栏占位文案(顺带修了"输入框根本没装上")
                                   "搜索或输入网址")),
                 # v0.4.49dsk: 悬浮窗进全屏时把控件自己藏掉 → 黑屏不恢复。
                 # 修法是把"藏兄弟"改成按身份比较 + 几何改完把控件救回可见态。
                 ("shell_pages", ("_fs_ensure_visible", "_same_ctrl",
                                  "fs hid siblings", "rep-gentle", "fs-enter")),
                 # v0.4.50dsk: 系统托盘(关闭到托盘 / 托盘菜单 / 图标)
                 ("shell_tray", ("install_tray", "NotifyIcon", "allow_close",
                                 "_on_closing", "load_app_logo", "ShowBalloonTip")),
                 # v0.4.51dsk: 后台已有实例时再点图标只叫回窗口(不许再开一个进程)
                 ("shell_single", ("CreateMutexW", "ERROR_ALREADY_EXISTS",
                                   "AllowSetForegroundWindow", "scope_of"))):
    if mod in pyz_mods:
        c = pyz.extract(mod)
        for kw in kws:
            ok("%s 字节码含 %s" % (mod, kw), has(c, kw))
    else:
        ok("PYZ 含 " + mod, False)
# index.html 里的版本页脚必须**就是** exe 的版本（防止打了旧 web/）。
# ★ 别写成 "含 v0.4.43dsk 且不含 v0.4.42dsk" —— 那种断言每升一版就要改一次,
#   漏改就变成假 PASS。这里改成"页脚里出现的所有版本号都等于 exe 的版本"。
hit = [n for n in names if n.replace("\\", "/").endswith("web/index.html")]
if hit:
    html = car.extract(hit[0]).decode("utf-8", "replace")
    foot = re.findall(r"洞鉴虚明 (v[0-9A-Za-z.]+)", html)
    ok("归档 web/index.html 页脚已是 %s" % (VER or "(未知版本)"),
       bool(VER) and bool(foot) and all(v == VER for v in foot),
       "页脚 %s / exe %s" % (sorted(set(foot)), VER))
    # v0.4.50dsk 常规设置里那两行(后台自启 / 关闭到托盘)必须真的在包里
    for _id in ('id="autostartRow"', 'id="closeToTrayRow"',
                'id="autostartFld" hidden', 'id="closeToTrayFld" hidden'):
        ok("归档 index.html 含 " + _id, _id in html)
_ajs_hit = [n for n in names if n.replace("\\", "/").endswith("web/app.js")]
if _ajs_hit:
    _ajs = (car.extract(_ajs_hit[0]) or b"").decode("utf-8", "replace")
    ok("归档 app.js: 托盘回来即换一屏(__msxRefreshFeed)",
       "window.__msxRefreshFeed = function" in _ajs)
    ok("归档 app.js: 两行设置只在 info.tray 时显示",
       "if (info && info.tray)" in _ajs)
    ok("归档 app.js: 开机自启单独提示失败原因(不静默回滚)",
       "ui_autostart: b.dataset.v" in _ajs)

# ---------- v0.4.52dsk: 栏尾主菜单 / 历史记录 / 整页翻译 ----------
# 三个新模块都是 app_window 里**函数内 import**(与 shell_ai/shell_tray 同一模式,
# modulegraph 照样会收进 PYZ), 但"函数内 import 漏打包"是这套壳最容易翻车的地方 ——
# 打包后必须逐个确认真的在包里。
def _vtuple(v):
    try:
        parts = re.findall(r"\d+", str(v))
        return tuple(int(x) for x in parts[:3])
    except Exception:
        return (0, 0, 0)


if _vtuple(VER) >= (0, 4, 52):
    for mod, kws in (
            ("shell_menu", ("show_menu", "Toast", "MENU_W")),
            ("shell_history", ("show_history", "web_hist_list", "web_hist_del")),
            ("shell_translate", ("translate_page", "_TR_COLLECT_JS", "translate_texts")),
            ("shell_navbar", ("_menu_rect", "menu_anchor", "on_menu")),
            ("app", ("web_hist_add", "web_hist_list", "translate_texts", "web_hist"))):
        if mod in pyz_mods:
            c = pyz.extract(mod)
            for kw in kws:
                ok("%s 字节码含 %s" % (mod, kw), has(c, kw))
        else:
            ok("PYZ 含 " + mod, False)

print("\nRESULT: %s" % ("PASS(%d)" % PASS if not FAILS else "FAIL(%d): %s" % (len(FAILS), " | ".join(FAILS))))
sys.exit(1 if FAILS else 0)
