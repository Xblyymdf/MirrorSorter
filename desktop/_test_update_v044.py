# -*- coding: utf-8 -*-
"""v0.4.4dsk 更新器：多源切换 / Range 续传 / 忽略 Range / SHA 校验 / 进度粒度。

全部走本机 HTTP 服务器，不联网、不碰用户数据目录。
"""
import hashlib
import http.server
import os
import re
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ["MIRRORSORTER_DATA_DIR"] = tempfile.mkdtemp(prefix="ms_upd_")
sys.path.insert(0, ROOT)
import app  # noqa: E402

PAYLOAD = b"MZ" + os.urandom(6 * 1024 * 1024 - 2)
SHA = hashlib.sha256(PAYLOAD).hexdigest()
REQS = []
FAIL = []


def chk(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAIL.append(msg)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _full(self, ignore_range=False):
        rng = "" if ignore_range else (self.headers.get("Range") or "")
        REQS.append((self.path, rng, ignore_range))
        start = 0
        m = re.match(r"bytes=(\d+)-", rng)
        if m:
            start = int(m.group(1))
        if start >= len(PAYLOAD):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = PAYLOAD[start:]
        self.send_response(206 if m else 200)
        self.send_header("Content-Length", str(len(body)))
        if m:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, len(PAYLOAD) - 1, len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ok":
            self._full()
        elif self.path == "/norange":
            self._full(ignore_range=True)
        elif self.path == "/stall":
            REQS.append((self.path, self.headers.get("Range") or "", False))
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            try:
                self.wfile.write(PAYLOAD[:1024 * 1024])
                self.wfile.flush()
                time.sleep(3)
            except Exception:
                pass
        elif self.path == "/slow":
            REQS.append((self.path, self.headers.get("Range") or "", False))
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            for i in range(0, len(PAYLOAD), app._UPDATE_CHUNK):
                self.wfile.write(PAYLOAD[i:i + app._UPDATE_CHUNK])
                self.wfile.flush()
                time.sleep(0.02)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
PARTNAME = "\u6d1e\u9274\u865a\u660e0.4.4dsk.exe.download"


def reset_state():
    with app._UPDATE_LOCK:
        app._UPDATE_STATE.update({"phase": "downloading", "received": 0, "total": 0,
                                  "path": "", "error": "", "version": "", "notes": "",
                                  "source": ""})


def run_worker(sources, tmp, sha=SHA, size=len(PAYLOAD), timeout=0.5, rounds=1):
    app.UPDATE_DIR = tmp
    app._update_sources = lambda info: sources
    app._UPDATE_SOCK_TIMEOUT = timeout
    app._UPDATE_ROUNDS = rounds
    reset_state()
    info = {"url": "https://github.com/Xblyymdf/MirrorSorter/releases/download/v0.4.4dsk/x.exe",
            "sha256": sha, "latest": "0.4.4dsk", "size": size}
    app._update_download_worker(info)
    return dict(app._UPDATE_STATE)


def part_path(tmp):
    return os.path.join(tmp, PARTNAME)


def final_path(tmp):
    return os.path.join(tmp, "\u6d1e\u9274\u865a\u660e0.4.4dsk.exe")


# ---------- 源顺序 ----------
url = "https://github.com/Xblyymdf/MirrorSorter/releases/download/v0.4.4dsk/x.exe"
srcs = app._update_sources({"url": url, "sha256": "ab" * 32})
chk(srcs == [app._UPDATE_MIRRORS[0] + url, app._UPDATE_MIRRORS[1] + url, url],
    "有官方 SHA256：两个镜像在前，官方地址兜底")
srcs2 = app._update_sources({"url": url, "sha256": ""})
chk(srcs2 == [url], "没有官方 SHA256：只走官方地址")
chk(app._update_url_ok("https://github.com/a/b.exe") and
    not app._update_url_ok("http://github.com/a/b.exe") and
    not app._update_url_ok("https://evil.example/a.exe"), "附件地址白名单仍然生效")

# ---------- 断点：网络失败保留 .download，下一次接着下 ----------
t1 = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
st = run_worker([BASE + "/stall"], t1, timeout=0.4)
chk(st["phase"] == "error", "源卡住 → phase=error")
chk(os.path.isfile(part_path(t1)) and os.path.getsize(part_path(t1)) >= 1024 * 1024,
    "网络失败保留 .download（可供续传）")
kept = os.path.getsize(part_path(t1))
REQS.clear()
st = run_worker([BASE + "/ok"], t1, timeout=5)
chk(st["phase"] == "ready", "第二次运行续传成功 → phase=ready")
chk(any(r[1].startswith("bytes=") for r in REQS), "续传请求带 Range")
chk(any(r[1] == "bytes=%d-" % kept for r in REQS), "Range 起点等于已下载字节数 %d" % kept)
chk(os.path.isfile(final_path(t1)) and open(final_path(t1), "rb").read() == PAYLOAD,
    "续传后的文件与源文件逐字节一致")
chk(not os.path.exists(part_path(t1)), "完成后 .download 已改名为正式文件")

# ---------- 服务器忽略 Range：从头重下 ----------
t2 = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
with open(part_path(t2), "wb") as f:
    f.write(PAYLOAD[:2 * 1024 * 1024])
REQS.clear()
st = run_worker([BASE + "/norange"], t2, timeout=5)
chk(st["phase"] == "ready", "忽略 Range 的源也能完成")
chk(any(r[2] for r in REQS), "确实请求了忽略 Range 的源")
chk(open(final_path(t2), "rb").read() == PAYLOAD, "忽略 Range 后文件没有拼接错位")

# ---------- SHA 不一致：报错并删掉坏文件 ----------
t3 = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
st = run_worker([BASE + "/ok"], t3, sha="00" * 32, timeout=5)
chk(st["phase"] == "error" and "SHA256" in st["error"], "SHA256 不一致 → error")
chk(not os.path.exists(part_path(t3)), "校验失败后删除 .download")

# ---------- 进度粒度：256KB 级 ----------
t4 = tempfile.mkdtemp(dir=os.environ["MIRRORSORTER_DATA_DIR"])
samples = []
stop = threading.Event()


def sampler():
    while not stop.is_set():
        with app._UPDATE_LOCK:
            samples.append(int(app._UPDATE_STATE.get("received") or 0))
        time.sleep(0.01)


th = threading.Thread(target=sampler, daemon=True)
th.start()
st = run_worker([BASE + "/slow"], t4, timeout=5)
stop.set()
th.join(1)
chk(st["phase"] == "ready" and st["received"] == len(PAYLOAD), "慢速源下载完成")
chk(any(0 < v < len(PAYLOAD) for v in samples),
    "下载中进度是逐步增长的（%d 个中间值）" % len([v for v in samples if 0 < v < len(PAYLOAD)]))
chk(app._UPDATE_CHUNK == 256 * 1024, "读块 256KB")

srv.shutdown()
print()
print("RESULT:", "OK" if not FAIL else "FAIL %d 项: %s" % (len(FAIL), FAIL))
sys.exit(1 if FAIL else 0)
