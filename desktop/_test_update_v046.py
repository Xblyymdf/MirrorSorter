# -*- coding: utf-8 -*-
"""v0.4.6 更新器：已下载完整的包直接复用，坏包不复用。

全部走本机 HTTP 服务器，不联网、不碰用户数据目录。
"""
import hashlib
import http.server
import os
import sys
import tempfile
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["MIRRORSORTER_DATA_DIR"] = tempfile.mkdtemp(prefix="ms_upd46_")
sys.path.insert(0, ROOT)
import app  # noqa: E402

PAYLOAD = b"MZ" + os.urandom(6 * 1024 * 1024 - 2)
SHA = hashlib.sha256(PAYLOAD).hexdigest()
HITS = []
FAIL = []


def chk(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAIL.append(msg)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
        if self.path != "/ok":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]


def final_path(d):
    return os.path.join(d, "\u6d1e\u9274\u865a\u660e0.4.6dsk.exe")


def info(size=len(PAYLOAD), sha=SHA):
    return {"url": "https://github.com/Xblyymdf/MirrorSorter/releases/download/v0.4.6dsk/x.exe",
            "sha256": sha, "latest": "0.4.6dsk", "size": size}


def run_worker(d, inf=None):
    app.UPDATE_DIR = d
    app._update_sources = lambda _info: [BASE + "/ok"]
    app._UPDATE_SOCK_TIMEOUT = 5
    app._UPDATE_ROUNDS = 1
    HITS.clear()
    with app._UPDATE_LOCK:
        app._UPDATE_STATE.update({"phase": "downloading", "received": 0, "total": 0,
                                  "path": "", "error": "", "version": "", "notes": "",
                                  "source": ""})
    app._update_download_worker(inf or info())
    return dict(app._UPDATE_STATE)


# ---------- _update_file_ok 判定 ----------
d = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
f = final_path(d)
open(f, "wb").write(PAYLOAD)
chk(app._update_file_ok(f, info()), "完整包判定为可复用")
chk(not app._update_file_ok(f, info(size=len(PAYLOAD) + 1)), "大小对不上不复用")
chk(not app._update_file_ok(f, info(sha="00" * 32)), "SHA256 对不上不复用")
chk(not app._update_file_ok(os.path.join(d, "nope.exe"), info()), "文件不存在不复用")
small = os.path.join(d, "small.exe")
open(small, "wb").write(b"MZ" + b"0" * 1024)
chk(not app._update_file_ok(small, info(size=0, sha="")), "小于 5MB 不复用")
badmz = os.path.join(d, "badmz.exe")
open(badmz, "wb").write(b"PK" + b"0" * (6 * 1024 * 1024))
chk(not app._update_file_ok(badmz, info(size=0, sha="")), "不是 MZ 不复用")
chk(app._update_file_sha256(f) == SHA, "_update_file_sha256 结果正确")

# ---------- 已下完但没装成功：重试零联网 ----------
d2 = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
f2 = final_path(d2)
open(f2, "wb").write(PAYLOAD)
part2 = f2 + ".download"
open(part2, "wb").write(PAYLOAD[:2 * 1024 * 1024])
st = run_worker(d2)
chk(st["phase"] == "ready" and st["path"] == f2, "完整包已存在 → 直接 ready")
chk(st["received"] == st["total"] == len(PAYLOAD), "进度直接是满的")
chk(HITS == [], "没有发起任何网络请求")
chk(not os.path.exists(part2), "残留的 .download 已清掉")
chk(open(f2, "rb").read() == PAYLOAD, "复用的文件内容没被改动")

# ---------- 坏包（SHA 错）不复用，重新下载覆盖 ----------
d3 = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
f3 = final_path(d3)
bad = bytearray(PAYLOAD)
bad[100] ^= 0xFF
open(f3, "wb").write(bytes(bad))
st = run_worker(d3)
chk(st["phase"] == "ready", "坏包场景最终 ready")
chk(len(HITS) == 1, "坏包触发了重新下载")
chk(open(f3, "rb").read() == PAYLOAD, "下载后覆盖成正确的包")
chk(not os.path.exists(f3 + ".download"), ".download 已改名")

# ---------- 半截包不复用 ----------
d4 = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
f4 = final_path(d4)
open(f4, "wb").write(PAYLOAD[:-100])
st = run_worker(d4)
chk(st["phase"] == "ready" and len(HITS) == 1, "大小不足的残留包不复用")
chk(open(f4, "rb").read() == PAYLOAD, "重下后半截包被修正")

srv.shutdown()
print()
print("RESULT:", "OK" if not FAIL else "FAIL %d 项: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
