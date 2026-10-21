# -*- coding: utf-8 -*-
"""自动更新自测（离线；不碰用户数据目录、不发真实网络请求）。

覆盖：版本比较 / GitHub Releases 解析 / 更新包校验与替换脚本 / HTTP 路由 /
桌面壳 install_update 桥 / 前端接线与版本同步。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="msx_upd_")
os.environ["MIRRORSORTER_DATA_DIR"] = TMP

PASS = 0
FAIL = []


def ok(cond, name):
    global PASS
    if cond:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name)


def read(p):
    return io.open(p, encoding="utf-8", newline="").read().replace("\r\n", "\n")


import app  # noqa: E402

APP_SRC = read(os.path.join(HERE, "app.py"))
WIN_SRC = read(os.path.join(HERE, "app_window.py"))
HTML_SRC = read(os.path.join(HERE, "web", "index.html"))
JS_SRC = read(os.path.join(HERE, "web", "app.js"))
CSS_SRC = read(os.path.join(HERE, "web", "style.css"))

# ---------- 1. 版本与源码接线 ----------
NEW_VER = "v9.9.9dsk"          # 永远比当前版本新，测试不随版本号过期
NEW_NUM = NEW_VER[1:]
ok(APP_SRC.count("洞鉴虚明 · 基础版 (%s)" % app.APP_VER) == 1, "app.py 头注释版本同步")
ok(HTML_SRC.count("洞鉴虚明 %s" % app.APP_VER) == 2, "index.html 两处页脚版本同步")
ok(('APP_VER = "%s"' % app.APP_VER) in APP_SRC, "APP_VER 常量写死")
ok(APP_SRC.count('if path == "/api/update/check":') == 1, "GET /api/update/check 路由")
ok(APP_SRC.count('if path == "/api/update/progress":') == 1, "GET /api/update/progress 路由")
ok(APP_SRC.count('if path == "/api/update/download":') == 1, "POST /api/update/download 路由")
ok("import urllib.error\n" in APP_SRC, "app.py 导入 urllib.error")

# ---------- 2. 版本比较 ----------
ok(app._update_ver("v0.4.1dsk") == (0, 4, 1), "_update_ver 忽略 v/dsk 后缀")
ok(app._update_ver("0.10") == (0, 10), "_update_ver 多位数字")
ok(app._update_ver("") == (), "_update_ver 空串")
ok(app._update_newer("v0.4.1dsk", "v0.4.0dsk"), "0.4.1 > 0.4.0")
ok(not app._update_newer("v0.4.0dsk", "v0.4.0dsk"), "同版本不更新")
ok(not app._update_newer("v0.3.1.67dsk", "v0.4.0dsk"), "旧版本不更新")
ok(app._update_newer("v0.4.1", "v0.4.0dsk"), "无后缀新版仍识别")
ok(app._update_newer("v0.10.0", "v0.9.9"), "10 > 9（不是字符串比较）")
ok(not app._update_newer("garbage", "v0.4.0dsk"), "解析失败不更新")

# ---------- 3. 下载地址白名单 ----------
ok(app._update_url_ok("https://github.com/a/b/releases/download/v1/x.exe"), "允许 github.com")
ok(app._update_url_ok("https://objects.githubusercontent.com/x"), "允许 objects.githubusercontent.com")
ok(not app._update_url_ok("http://github.com/a/x.exe"), "拒绝 http")
ok(not app._update_url_ok("https://evil.com/x.exe"), "拒绝非 GitHub 域")
ok(not app._update_url_ok("https://github.com.evil.com/x.exe"), "拒绝仿冒后缀域")
ok(not app._update_url_ok(""), "拒绝空地址")

# ---------- 4. GitHub Releases 解析（假响应，不联网） ----------
_real_fetch = app._update_fetch_json


def with_fetch(fn, body):
    def fake(url, timeout=12):
        return fn(url)
    app._update_fetch_json = fake
    try:
        return body()
    finally:
        app._update_fetch_json = _real_fetch


def release(tag, assets, body="更新说明"):
    return {"tag_name": tag, "body": body, "html_url": "https://github.com/Xblyymdf/MirrorSorter/releases/tag/" + tag,
            "published_at": "2026-10-20T00:00:00Z", "assets": assets}


asset = {"name": "洞鉴虚明%s.exe" % NEW_VER, "size": 78408159,
         "browser_download_url": "https://github.com/Xblyymdf/MirrorSorter/releases/download/%s/x.exe" % NEW_VER,
         "digest": "sha256:" + "a" * 64}

r = with_fetch(lambda u: release(NEW_VER, [asset]), app.update_check)
ok(r.get("ok") and r.get("has_update") is True, "检测到新版本")
ok(r.get("latest") == NEW_NUM, "新版本号去掉 v")
ok(r.get("name", "").endswith(".exe"), "返回 exe 附件名")
ok(r.get("sha256") == "a" * 64, "解析 digest 的 sha256")
ok(r.get("supported") in (False, 0), "源码运行标记为不支持自替换")
ok(app._UPDATE_LAST["info"] is not None, "新版本会缓存给下载用")

r = with_fetch(lambda u: release(app.APP_VER, [asset]), app.update_check)
ok(r.get("ok") and r.get("has_update") is False, "同版本不提示更新")
ok(app._UPDATE_LAST["info"] is None, "无更新时清空缓存")

r = with_fetch(lambda u: release(NEW_VER, [
    {"name": "说明.txt", "browser_download_url": "https://github.com/x/y.txt"},
    asset]), app.update_check)
ok(r.get("ok") and r.get("has_update"), "跳过非 exe 附件")

installer = {"name": "洞鉴虚明%s_安装包.exe" % NEW_VER, "size": 280 * 1024 * 1024,
             "browser_download_url": "https://github.com/Xblyymdf/MirrorSorter/releases/download/%s/setup.exe" % NEW_VER}
r = with_fetch(lambda u: release(NEW_VER, [installer, asset]), app.update_check)
ok(r.get("ok") and r.get("name") == asset["name"], "同时有安装包时选独立 exe")

r = with_fetch(lambda u: release(NEW_VER, [
    {"name": "x.exe", "browser_download_url": "https://evil.com/x.exe"}]), app.update_check)
ok(r.get("ok") is False and "exe" in (r.get("error") or ""), "只认 GitHub 域名的 exe 附件")


def raise404(_u):
    raise urllib.error.HTTPError(app.UPDATE_API, 404, "Not Found", {}, None)


r = with_fetch(raise404, app.update_check)
ok(r.get("ok") is False and "Release" in (r.get("error") or ""), "404 提示还没有 Release")


def raise403(_u):
    raise urllib.error.HTTPError(app.UPDATE_API, 403, "Forbidden", {}, None)


r = with_fetch(raise403, app.update_check)
ok(r.get("ok") is False and "限流" in (r.get("error") or ""), "403 提示限流")

# ---------- 5. 替换脚本 ----------
script = app.update_script_text(r"C:\Users\a\Desktop\洞鉴虚明v0.4.1dsk.exe",
                                r"C:\Users\a\AppData\LocalLow\MirrorSorter\update\洞鉴虚明0.4.2dsk.exe")
ok("@echo off" in script, "替换脚本是 batch")
ok("for /l %%i in (1,1,180) do (" in script, "替换脚本重试等文件解锁")
ok('move /y "%NEW%" "%OLD%"' in script, "替换脚本 move 覆盖旧 exe")
ok('copy /y "%OLD%" "%OLD%.bak"' in script, "替换前备份旧 exe（可回滚）")
ok('cd /d "%OLD_DIR%"' in script and 'start "" "%OLD%"' in script, "替换后从原目录重启")
ok("%%" in app._update_bat_quote("C:\\100%\\x.exe"), "路径里的 % 被转义")
try:
    app._update_bat_quote("C:\\a\nb")
    ok(False, "拒绝换行路径")
except ValueError:
    ok(True, "拒绝换行路径")

# ---------- 6. 更新包校验（模拟 frozen + 隔离目录） ----------
upd_root = os.path.join(TMP, "update")
os.makedirs(upd_root, exist_ok=True)
prog_dir = os.path.join(TMP, "prog")
os.makedirs(prog_dir, exist_ok=True)
old_exe = os.path.join(prog_dir, "洞鉴虚明v0.4.1dsk.exe")
with open(old_exe, "wb") as f:
    f.write(b"MZ" + b"\0" * 100)
new_exe = os.path.join(upd_root, "洞鉴虚明0.4.2dsk.exe")
with open(new_exe, "wb") as f:
    f.write(b"MZ" + b"\0" * (6 * 1024 * 1024))

old_updir, old_frozen, old_exe_attr = app.UPDATE_DIR, getattr(sys, "frozen", None), sys.executable
app.UPDATE_DIR = upd_root
sys.executable = old_exe
sys.frozen = True
try:
    res = app.update_prepare_install(new_exe)
    ok(res.get("ok") is True and os.path.isfile(res.get("script") or ""), "校验通过并写出替换脚本")
    raw = open(res["script"], "rb").read()
    ok(raw.startswith(b"@echo off\r\n"), "替换脚本 CRLF")
    txt = raw.decode("gbk")
    ok("洞鉴虚明0.4.2dsk.exe" in txt and "洞鉴虚明v0.4.1dsk.exe" in txt, "脚本含新旧 exe 路径")

    small = os.path.join(upd_root, "small.exe")
    open(small, "wb").write(b"MZ" + b"\0" * 100)
    ok(app.update_prepare_install(small).get("ok") is False, "拒绝过小的更新包")

    notexe = os.path.join(upd_root, "fake.exe")
    open(notexe, "wb").write(b"PK" + b"\0" * (6 * 1024 * 1024))
    ok(app.update_prepare_install(notexe).get("ok") is False, "拒绝非 MZ 文件")

    outside = os.path.join(TMP, "outside.exe")
    shutil.copyfile(new_exe, outside)
    ok(app.update_prepare_install(outside).get("ok") is False, "拒绝更新目录之外的文件")
finally:
    app.UPDATE_DIR = old_updir
    sys.executable = old_exe_attr
    if old_frozen is None:
        del sys.frozen
    else:
        sys.frozen = old_frozen

# 非 frozen：下载/安装都应拒绝
ok(app.update_download().get("ok") is False, "源码运行拒绝下载替换")
ok(app.update_prepare_install(new_exe).get("ok") is False, "源码运行拒绝准备安装")

# ---------- 6.5 下载 worker（假 GitHub 响应，不联网） ----------
import hashlib as _hashlib
import time as _time

DATA = b"MZ" + b"\x00" * (6 * 1024 * 1024 - 2)


class _Resp:
    def __init__(self, data):
        self._data, self._pos = data, 0
        self.status = 200          # v0.4.4 的 _update_stream 会读 status
        self.headers = {"Content-Length": str(len(data))}

    def getcode(self):
        return 200

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real_urlopen = app.urllib.request.urlopen
_old_state = dict(app._UPDATE_STATE)
_old_last = dict(app._UPDATE_LAST)
app.UPDATE_DIR = upd_root
sys.frozen = True
try:
    app.urllib.request.urlopen = lambda req, timeout=40: _Resp(DATA)

    def _run_download(info):
        with app._UPDATE_LOCK:
            app._UPDATE_STATE.update({"phase": "idle", "received": 0, "total": 0,
                                      "path": "", "error": "", "version": ""})
            app._UPDATE_LAST["info"] = dict(info)
        r = app.update_download()
        assert r.get("ok"), r
        for _ in range(120):
            st = app.update_progress()
            if st["phase"] in ("ready", "error"):
                return st
            _time.sleep(0.05)
        raise AssertionError("download timeout")

    good = {"url": asset["browser_download_url"], "size": len(DATA),
            "latest": NEW_NUM, "sha256": _hashlib.sha256(DATA).hexdigest()}
    st = _run_download(good)
    ok(st["phase"] == "ready" and os.path.isfile(st.get("path") or ""),
       "下载 worker 落盘并进入 ready")
    ok(st.get("path", "").endswith("洞鉴虚明%s.exe" % NEW_NUM), "下载文件按版本命名")
    ok(os.path.getsize(st["path"]) == len(DATA), "下载文件大小正确")
    ok(not os.path.exists(st["path"] + ".download"), "成功时清理 .download 临时文件")

    bad = dict(good, sha256="b" * 64)
    st = _run_download(bad)
    ok(st["phase"] == "error" and "SHA256" in (st.get("error") or ""), "SHA256 不符时报错")
    ok(not os.path.exists(os.path.join(upd_root, "洞鉴虚明%s.exe.download" % NEW_NUM)),
       "失败时清理 .download 临时文件")
finally:
    app.urllib.request.urlopen = _real_urlopen
    app.UPDATE_DIR = old_updir
    with app._UPDATE_LOCK:
        app._UPDATE_STATE.clear()
        app._UPDATE_STATE.update(_old_state)
        app._UPDATE_LAST.clear()
        app._UPDATE_LAST.update(_old_last)
    del sys.frozen

# ---------- 7. HTTP 路由（真起服务，随机端口） ----------
_real_check, _real_dl = app.update_check, app.update_download
app.update_check = lambda: {"ok": True, "route": "check"}
app.update_download = lambda: {"ok": True, "route": "download"}
srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]


def req(path, data=None):
    r = urllib.request.urlopen(base + path, data=data, timeout=10)
    return json.loads(r.read().decode("utf-8"))


try:
    ok(req("/api/update/check").get("route") == "check", "HTTP GET check 路由")
    ok("phase" in req("/api/update/progress"), "HTTP GET progress 路由")
    ok(req("/api/update/download", data=b"{}").get("route") == "download", "HTTP POST download 路由")
finally:
    srv.shutdown()
    srv.server_close()
    app.update_check, app.update_download = _real_check, _real_dl

# ---------- 8. 桌面壳 bridge ----------
import app_window  # noqa: E402
import subprocess as _sp  # noqa: E402

api = app_window.ShellApi()
info = api.shell_info()
ok(info.get("self_update") is False, "源码壳 shell_info.self_update=False")
ok(api.install_update().startswith("err:"), "源码壳拒绝 install_update")

_real_prog = app.update_progress
_real_prep = app.update_prepare_install
_real_popen = _sp.Popen
_popen_calls = []
app.update_progress = lambda: {"ok": True, "phase": "ready", "path": "C:\\tmp\\new.exe"}
app.update_prepare_install = lambda p: {"ok": True, "script": "C:\\tmp\\apply_update.cmd", "old": "C:\\tmp\\old.exe"}
sys.frozen = True
try:
    def fake_popen(args, **kw):
        _popen_calls.append((args, kw))
        return object()
    _sp.Popen = fake_popen
    app_window.subprocess.Popen = fake_popen
    # 不要把 Timer 真挂上（否则 0.9s 后会对 None 窗口执行 destroy，无碍但会拖住测试）
    real_timer = app_window.threading.Timer
    class _Tim:
        def __init__(self, *a, **k):
            self.started = True
        def start(self):
            pass
    app_window.threading.Timer = _Tim
    try:
        api2 = app_window.ShellApi()
        ok(api2.install_update() == "ok", "frozen 壳 install_update 成功")
        ok(len(_popen_calls) == 1 and _popen_calls[0][0][:2] == ["cmd.exe", "/c"], "拉起 cmd /c 替换脚本")
        ok(api2.install_update().startswith("err:正在更新"), "防重复安装")
        flags = _popen_calls[0][1].get("creationflags", 0)
        ok(bool(flags & 0x08000000) or bool(flags & 0x8), "无控制台窗口/独立进程")
    finally:
        app_window.threading.Timer = real_timer
finally:
    _sp.Popen = _real_popen
    app_window.subprocess.Popen = _real_popen
    app.update_progress, app.update_prepare_install = _real_prog, _real_prep
    del sys.frozen

# ---------- 9. 前端接线 ----------
i_set = HTML_SRC.find('id="btnSettings"')
i_upd = HTML_SRC.find('id="btnCheckUpdate"')
i_top = HTML_SRC.find('class="railTop"')
ok(0 <= i_set < i_upd < i_top, "检测更新按钮紧随设置按钮（设置下方）")
ok('id="btnCheckUpdate"' in HTML_SRC and 'hidden>' in HTML_SRC[i_upd:i_upd + 160], "按钮默认隐藏")
ok('id="i-download"' in HTML_SRC, "下载图标已定义")
ok("#btnCheckUpdate[hidden]{display:none}" in CSS_SRC, "hidden 不被 .nav 的 flex 盖掉")
ok("body.in-settings #railLeft > #btnCheckUpdate{display:none}" in CSS_SRC, "设置视图里不重复出现")
for s in ('/api/update/check', '/api/update/download', '/api/update/progress',
          'a.install_update()', 'info.self_update', 'btn.onclick = updCheck'):
    ok(s in JS_SRC, "app.js 接入 " + s)
ok('window.addEventListener("pywebviewready", updWireShell)' in JS_SRC, "桥就绪后重新接线")

srv = None
print("\n%d PASS / %d FAIL" % (PASS, len(FAIL)))
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
