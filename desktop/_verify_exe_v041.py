# -*- coding: utf-8 -*-
"""v0.4.1dsk 打包产物反解校验（不启动窗口、不抢屏幕）。

    python _verify_exe_v041.py [exe]

检查：web 资源 / 更新按钮与脚本 / app.py 的更新端点与常量 /
app_window.py 的 install_update 桥 / 版本号 / 模型仍在包里。
"""
import os
import sys
import tempfile
import types
import marshal

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "build", "dist", "洞鉴虚明v0.4.1dsk.exe")
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

chk("洞鉴虚明 v0.4.1dsk" in html and "洞鉴虚明 v0.4.0dsk" not in html,
    "index.html 页脚是 v0.4.1dsk（旧版本串已消失）")
i_set, i_upd, i_top = (html.find('id="btnSettings"'), html.find('id="btnCheckUpdate"'),
                       html.find('class="railTop"'))
chk(0 <= i_set < i_upd < i_top, "检测更新按钮在设置按钮下方")
chk('id="i-download"' in html and "hidden>" in html[i_upd:i_upd + 170], "下载图标 + 按钮默认隐藏")
chk("#btnCheckUpdate[hidden]{display:none}" in css, "CSS 保证按钮隐藏生效")
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
tmp = os.path.join(tempfile.gettempdir(), "msx_verify_v041.pyz")
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
    # PyInstaller 把入口脚本(PyInstaller 的 __main__)单独放在 CArchive,
    # 不在 PYZ 里 —— app_window.py 是入口，所以走这条。
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
           "update_script_text", "update_prepare_install"):
    chk(fn in allapp, "app 里定义了 " + fn)
for s in ("/api/update/check", "/api/update/progress", "/api/update/download",
          "https://api.github.com/repos/%s/releases/latest",
          "Xblyymdf/MirrorSorter", "apply_update.cmd"):
    chk(s in allapp, "app 常量/名字含 " + s)
chk(b"MZ" in strs, "app 含 exe 文件头校验 MZ")
chk(any(isinstance(s, str) and "APP_VER" not in s and s == "v0.4.1dsk" for s in strs),
    "app 常量版本 v0.4.1dsk")

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
