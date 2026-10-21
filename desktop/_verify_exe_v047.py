# -*- coding: utf-8 -*-
"""v0.4.7dsk 打包产物反解校验(不启动窗口)。用法: python _verify_exe_v047.py [exe]"""
import marshal
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader  # noqa: E402

EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "build", "dist", "洞鉴虚明v0.4.7dsk.exe")
FAIL = []
TOTAL = [0]


def chk(cond, msg):
    TOTAL[0] += 1
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAIL.append(msg)


def payload(v):
    return v[1] if isinstance(v, tuple) else v


car = CArchiveReader(EXE)
toc = car.toc


def res(name):
    key = next((k for k in toc if k.replace("\\", "/") == name), None)
    return payload(car.extract(key)) if key else None


chk(os.path.getsize(EXE) > 70 * 1024 * 1024,
    "exe 体积正常 (%.1f MB)" % (os.path.getsize(EXE) / 1048576.0))
html = (res("web/index.html") or b"").decode("utf-8", "replace")
js = (res("web/app.js") or b"").decode("utf-8", "replace")
chk("洞鉴虚明 v0.4.7dsk" in html, "index.html 页脚是 v0.4.7dsk")
chk("v0.4.6dsk" not in html, "index.html 没有残留 v0.4.6dsk")
chk("S.session = Math.random().toString(36).slice(2);" in js,
    "app.js: 清池会换会话 id")
chk('if (v0 === "feed" && st0.q)' in js, "app.js: 启动恢复搜索用 restartFeed")
i = js.find('if (v0 === "feed" && st0.q)')
chk(js.find("restartFeed();", i) > i, "app.js: 启动恢复搜索确实调用 restartFeed")

pyz_key = next((k for k in toc if k.endswith(".pyz")), None)
tmp = os.path.join(tempfile.gettempdir(), "msx_verify_v047.pyz")
with open(tmp, "wb") as f:
    f.write(payload(car.extract(pyz_key)))
z = ZlibArchiveReader(tmp)
ztoc = set(z.toc)


def scan_code(obj):
    strings, names, funcs = set(), set(), set()
    if not isinstance(obj, types.CodeType):
        return strings, names, funcs

    def walk(co):
        names.update(co.co_names)
        names.update(co.co_varnames)
        funcs.add(co.co_name)
        for c in co.co_consts:
            if isinstance(c, (str, bytes)):
                strings.add(c)
            elif isinstance(c, (tuple, list, set, frozenset)):
                for x in c:
                    if isinstance(x, (str, bytes)):
                        strings.add(x)
                    elif isinstance(x, types.CodeType):
                        walk(x)
            elif isinstance(c, types.CodeType):
                walk(c)

    walk(obj)
    return strings, names, funcs


obj = z.extract("app")
if isinstance(obj, tuple):
    obj = obj[-1]
strs, names, funcs = scan_code(obj)
allapp = strs | names | funcs

for fn in ("_feed_plan", "build_universe", "serve_chunk", "kick_build", "_fp_clear"):
    chk(fn in funcs or fn in names, "app 里有 " + fn)
chk("kick_build_empty" not in allapp, "没有残留强制绕过节流的 kick_build_empty")
blob = "\n".join(s for s in strs if isinstance(s, str))
chk("DELETE FROM items; DELETE FROM shown;" in blob, "清池批量删除 items/shown")
chk("DELETE FROM item_presentation; DELETE FROM item_exposure_state" in blob,
    "清池同时删曝光记账表")
chk(any(isinstance(s, str) and s == "v0.4.7dsk" for s in strs),
    "app 常量版本 v0.4.7dsk")
chk(not any(isinstance(s, str) and s == "v0.4.6dsk" for s in strs),
    "app 里没有残留 v0.4.6dsk 版本常量")

print("==== %d PASS / %d FAIL ====" % (TOTAL[0] - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
