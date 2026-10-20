# -*- coding: utf-8 -*-
"""v0.4.3dsk 打包产物反解校验（不启动窗口、不抢屏幕）。

    python _verify_exe_v043.py [exe]

检查：
- web 资源里的「搜索按钮自适应避让」接线与版本号（v0.4.3dsk）
- v0.4.2 的搜索按钮/BV 直达/相关度/占比=0/防扎堆不回归
- v0.4.1 自动更新、v0.4.0 侧栏拖动修复不回归
- PYZ 里的后端函数与常量、模型文件
"""
import os
import sys
import tempfile
import types
import marshal

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "build", "dist", "洞鉴虚明v0.4.3dsk.exe")
FAIL = []


def chk(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAIL.append(msg)


def payload(v):
    return v[1] if isinstance(v, tuple) else v


from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader  # noqa: E402

chk(os.path.isfile(EXE), "exe 存在: %s" % EXE)
chk(os.path.getsize(EXE) > 70 * 1024 * 1024,
    "exe 体积正常 (%.1f MB)" % (os.path.getsize(EXE) / 1048576.0))

car = CArchiveReader(EXE)
toc = {k.replace("\\", "/"): k for k in car.toc}
print("CArchive 条目数: %d" % len(toc))


def res(name):
    key = toc.get(name)
    if not key:
        return None
    return payload(car.extract(key))


js = res("web/app.js")
html = res("web/index.html")
css = res("web/style.css")
chk(js is not None and html is not None and css is not None, "web/ 三件套在包里")
js = (js or b"").decode("utf-8", "replace")
html = (html or b"").decode("utf-8", "replace")
css = (css or b"").decode("utf-8", "replace")

# ---------- 版本 ----------
chk("洞鉴虚明 v0.4.3dsk" in html and "洞鉴虚明 v0.4.2dsk" not in html,
    "index.html 页脚是 v0.4.3dsk（旧版本串已消失）")

# ---------- v0.4.3 搜索按钮避让 ----------
chk("function fitSearchBtn()" in js, "app.js: 有 fitSearchBtn")
for s in ("let fitPad0 = 0;", "getComputedStyle(box).paddingRight",
          "Math.min(Math.ceil(cover), room)", "br.right - left",
          'window.addEventListener("resize", fitSearchBtn)',
          "new ResizeObserver(fitSearchBtn)",
          '$$("#topRightWrap > *").forEach((el) => ro.observe(el));'):
    chk(s in js, "app.js 含 " + s)
i_sync = js.find("function syncTopAvatars()")
i_sync_end = js.find("// 点头像 = 进", i_sync)
chk(0 <= i_sync < i_sync_end and "fitSearchBtn();" in js[i_sync:i_sync_end],
    "登录头像显隐后立即重排（syncTopAvatars 调 fitSearchBtn）")
chk("paddingRight !== next" in js, "相同宽度不重复写样式（不触发观察循环）")

# ---------- v0.4.2 不回归：搜索按钮 / 显式触发 / BV ----------
i_in = html.find('id="searchInput"')
i_btn = html.find('id="btnSearch"')
chk(0 <= i_in < i_btn, "搜索按钮在输入框右侧（DOM 顺序）")
chk('type="button"' in html[i_btn:i_btn + 180], "搜索按钮 type=button")
chk("BV号" in html, "搜索框占位提示含 BV号")
chk("#topSearch .searchBtn" in css and "--accent-strong" in css, "搜索按钮样式在包里")
chk("$(\"#btnSearch\").onclick = runSearch" in js, "app.js: 点击按钮 → runSearch")
chk("\"Enter\"" in js and "runSearch();" in js, "app.js: Enter → runSearch")
chk("searchTimer" not in js and "debounce" not in js.lower(), "旧的防抖自动搜索已移除")
chk("exitSearch();" in js and "restartFeed();" in js, "app.js: 清空退出 + 重取首屏")

# ---------- v0.4.1 自动更新不回归 ----------
i_set, i_upd, i_top = (html.find('id="btnSettings"'), html.find('id="btnCheckUpdate"'),
                       html.find('class="railTop"'))
chk(0 <= i_set < i_upd < i_top, "检测更新按钮仍在设置按钮下方")
chk('id="i-download"' in html and "hidden>" in html[i_upd:i_upd + 170], "下载图标 + 按钮默认隐藏")
chk("#btnCheckUpdate[hidden]{display:none}" in css, "CSS 保证更新按钮隐藏生效")
chk("#btnCheckUpdate.setEntry" in css and ".updBar" in css and ".updNotes" in css,
    "更新按钮 / 进度条 / 说明样式在包里")
for s in ("async function updCheck(", "async function updDownload(", "function updWireShell(",
          "/api/update/check", "/api/update/download", "/api/update/progress",
          "a.install_update()", "info.self_update"):
    chk(s in js, "app.js 含 " + s)
chk("w0 + sign * (ev.clientX - x0)" in js, "v0.4.0 的侧栏拖动修复仍在")

# ---------- PYZ ----------
pyz_key = next((k for k in toc if k.endswith(".pyz")), None)
chk(pyz_key is not None, "找到 PYZ")
tmp = os.path.join(tempfile.gettempdir(), "msx_verify_v043.pyz")
with open(tmp, "wb") as f:
    f.write(payload(car.extract(pyz_key)))
z = ZlibArchiveReader(tmp)
ztoc = set(z.toc)
print("PYZ 模块数: %d" % len(ztoc))
for m in ("app", "shell_pages", "ftrt"):
    chk(m in ztoc, "PYZ 里有 " + m)
chk("app_window" in ztoc or "app_window" in toc, "app_window 在包里（入口脚本）")


def scan_code(obj):
    if not isinstance(obj, types.CodeType):
        return set(), set(), set()
    strings, names, funcs = set(), set(), set()

    def walk(co):
        names.update(getattr(co, "co_names", ()))
        names.update(getattr(co, "co_varnames", ()))
        names.update(getattr(co, "co_freevars", ()))
        names.update(getattr(co, "co_cellvars", ()))
        for c in getattr(co, "co_consts", ()):
            if isinstance(c, (str, bytes)):
                strings.add(c)
            elif isinstance(c, (tuple, list, set, frozenset)):
                for x in c:
                    if isinstance(x, (str, bytes)):
                        strings.add(x)
                    elif isinstance(x, types.CodeType):
                        funcs.add(x.co_name)
                        walk(x)
            elif isinstance(c, dict):
                for k, v in c.items():
                    if isinstance(k, (str, bytes)):
                        strings.add(k)
                    if isinstance(v, (str, bytes)):
                        strings.add(v)
            elif isinstance(c, types.CodeType):
                funcs.add(c.co_name)
                walk(c)
    walk(obj)
    return strings, names, funcs


def scan(mod):
    if mod in ztoc:
        obj = z.extract(mod)
        if isinstance(obj, tuple):
            obj = obj[-1]
        return scan_code(obj)
    key = toc.get(mod)
    if key:
        try:
            return scan_code(marshal.loads(payload(car.extract(key))))
        except Exception:
            pass
    return set(), set(), set()


strs, names, funcs = scan("app")
allapp = strs | names | funcs
for fn in ("update_check", "update_progress", "update_download",
           "update_script_text", "update_prepare_install",
           "xhh_weight", "_extract_bvid", "_rerank_search", "bili_video_by_bvid",
           "_search_bvid", "_sim_bands", "_spacing_keys", "serve_chunk"):
    chk(fn in funcs or fn in names, "app 里有 " + fn)
for nm in ("_MATCH_NOISE", "_SIM_NOISE", "_BVID_RE", "_TB_POOL_MIN_GAP",
           "_XHH_POOL_MIN_GAP", "_SERVE_SPACING_GAP", "_BUILD_MIN_GAP", "tb_weight"):
    chk(nm in names, "app 全局名含 " + nm)
for s in ("/api/update/check", "/api/update/progress", "/api/update/download",
          "https://api.github.com/repos/%s/releases/latest",
          "Xblyymdf/MirrorSorter", "apply_update.cmd",
          "BV直达", "https://api.bilibili.com/x/web-interface/view?bvid=",
          "https://api.bilibili.com/x/web-interface/wbi/search/type?"):
    chk(s in allapp, "app 常量/名字含 " + s)
chk(any(isinstance(s, str) and s == "v0.4.3dsk" for s in strs),
    "app 常量版本 v0.4.3dsk")
chk(not any(isinstance(s, str) and s == "v0.4.2dsk" for s in strs),
    "app 里没有残留 v0.4.2dsk 版本常量")

strs2, names2, funcs2 = scan("app_window")
allwin = strs2 | names2 | funcs2
for fn in ("install_update", "_close_for_update", "shell_info"):
    chk(fn in allwin, "app_window 定义了 " + fn)
for s in ("self_update", "cmd.exe", "CREATE_NO_WINDOW", "DETACHED_PROCESS"):
    chk(s in allwin, "app_window 常量/名字含 " + s)

model_key = next((k for k in toc if k.replace("\\", "/").endswith("model/ft_model.bin")), None)
chk(model_key is not None, "模型 ft_model.bin 仍在包里")

print()
print("RESULT:", "OK" if not FAIL else "FAIL %d 项: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
