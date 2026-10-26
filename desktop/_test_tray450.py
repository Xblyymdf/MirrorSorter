# -*- coding: utf-8 -*-
"""v0.4.50dsk 运行层验收: 后台常驻两行设置 + 开机自启注册表 + 关闭换一批。

隔离: MIRRORSORTER_DATA_DIR / MIRRORSORTER_PORT 都指到临时处, 不碰用户真库、不抢 8000。
注册表: 先备份用户原有的 MirrorSorter 自启项, 测完原样还回去。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="msx-tray450-")
os.environ["MIRRORSORTER_DATA_DIR"] = TMP
os.environ["MIRRORSORTER_PORT"] = "18077"

import app  # noqa: E402

PASS, FAILS = 0, []


def ok(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("PASS  %s  %s" % (name, extra))
    else:
        FAILS.append(name)
        print("FAIL  %s  %s" % (name, extra))


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


server, port = app.run_server(auto_open=False)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port
print("服务:", base)
time.sleep(0.4)

# ---------- 1. 两行设置默认关, 且能读出来 ----------
s = get(base + "/api/settings")
ok("默认值: ui_autostart=0 / ui_close_to_tray=0",
   s.get("ui_autostart") == "0" and s.get("ui_close_to_tray") == "0", s.get("ui_autostart"))

# ---------- 2. 关闭到托盘: 写入 / 回读 / 非法值不落库 ----------
r = post(base + "/api/settings", {"ui_close_to_tray": "1"})
s2 = get(base + "/api/settings")
ok("关闭到托盘可开启并回读", r.get("ok") is True and s2.get("ui_close_to_tray") == "1")
post(base + "/api/settings", {"ui_close_to_tray": "2"})
s3 = get(base + "/api/settings")
ok("非法值(2)不落库, 仍是 1", s3.get("ui_close_to_tray") == "1", s3.get("ui_close_to_tray"))
post(base + "/api/settings", {"ui_close_to_tray": "0"})

# ---------- 3. 开机自启: 真写注册表, 但先备份用户原有项 ----------
orig = app._autostart_value()
print("原有自启项:", repr(orig))
try:
    ok("自启命令行带 --tray(开机即缩托盘, 不弹窗)", "--tray" in app.autostart_command(),
       app.autostart_command())
    r = app.autostart_apply(True)
    ok("autostart_apply(True) 成功", r == "ok", r)
    ok("注册表里真的有这一项", app.autostart_state() == "1")
    ok("写进去的命令 == autostart_command()",
       app._autostart_value() == app.autostart_command(), app._autostart_value())
    # 走 HTTP 关掉(与界面同一条路)
    r = post(base + "/api/settings", {"ui_autostart": "0"})
    ok("HTTP 关闭自启: ok + 注册表已删",
       r.get("ok") is True and app.autostart_state() == "0", r)
    # 打开再对齐一次: sync 不该反复写(内容一致就 no-op)
    app.autostart_apply(True)
    app.sset("ui_autostart", "1")
    ok("autostart_sync() 幂等(内容一致返回 ok)", app.autostart_sync() == "ok")
    app.sset("ui_autostart", "0")
    ok("设置说关 → sync 会清掉残留项", app.autostart_sync() == "ok" and app.autostart_state() == "0")
finally:
    # 原样还原用户的自启项
    try:
        if orig:
            app.autostart_apply(True)
            if orig != app.autostart_command():
                import winreg
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app._AUTOSTART_RUN) as k:
                    winreg.SetValueEx(k, app._AUTOSTART_NAME, 0, winreg.REG_SZ, orig)
        else:
            app.autostart_apply(False)
    except Exception as e:
        print("还原注册表失败:", e)
    app.sset("ui_autostart", "0")
ok("测试后注册表已还原到原值", app._autostart_value() == orig, app._autostart_value())

# ---------- 4. 关闭换一批: 不清池 + 换代 + 冷启动缓存被重写 ----------
kick_saved = app.kick_build
try:
    app.kick_build = lambda quick=True, pending=False: True
    app._universe["building"] = False
    app._universe["items"] = [{"key": "k1", "title": "a"}, {"key": "k2", "title": "b"}]
    tk0 = app._uni_token
    app.sset("m_cold_start_n", "20")
    r = app.feed_rotate(wait=0.5)
    ok("feed_rotate: ok + 池子原样 + 令牌+1",
       r.get("ok") is True and app._uni_token == tk0 + 1
       and [it["key"] for it in app._universe["items"]] == ["k1", "k2"], r)
    cache = app.sget("cold_start_cache")
    ok("退出前把池子落进冷启动缓存(下次打开秒出)",
       bool(cache) and '"k1"' in cache, (cache or "")[:60])
finally:
    app.kick_build = kick_saved
    app._universe["items"] = []

app.shutdown(server)
try:
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass

print("\nRESULT: %s" % ("PASS(%d)" % PASS if not FAILS else "FAIL(%d): %s" % (len(FAILS), " | ".join(FAILS))))
sys.exit(1 if FAILS else 0)
