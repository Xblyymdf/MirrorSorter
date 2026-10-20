# -*- coding: utf-8 -*-
"""
洞鉴虚明 · 基础版 (v0.4.5dsk)
==========================
本地运行的洞鉴虚明：读取 B站 内容 → 黑白名单/AI 分拣 → 推荐流 + 数据源状态 + AI 聊天。

运行:   python app.py
访问:   http://127.0.0.1:8000

特性:
  - 零第三方依赖(只用 Python 标准库)
  - SQLite 持久化: 反馈/黑白名单/历史/收藏/聊天/统计
  - AI 可选: 在左侧设置里填 API Key(OpenAI 兼容, 默认 DeepSeek)后,
    开启 引战/低质 评分过滤 + 推荐理由 + AI 聊天; 未配置时一切照常可用
  - 模式档位: 严格/标准/宽松(引战剔除阈值)
  - 只存「标题+作者+封面+热度+链接」元数据, 不搬运内容
  - AI 悬浮窗(仅电脑版): 屏幕常悬浮球 → 对话窗提供「验证真实性」(联网取证判真伪)
    与「生成回怼」两个选项卡; 回怼为第一人称对线, 先点出对方的知识误区与逻辑谬误,
    再按条数档位(1/2/3)逐段产出「一句话 n 条 + 一段话 n 条 + 长篇 1 条」并即时显示
"""
import base64
import collections
import contextlib
import ctypes
import hashlib
import html
import html.parser
import http.cookiejar
import json
import math
import os
import queue
import random
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTS = (8000, 8010, 8020)
# v0.3.1.34: 「信息以新窗口形式打开」(主题设置, 默认开) —— 点开信息弹**独立悬浮窗**,
#           悬浮窗可塞回标签页, 标签页也能拖出成悬浮窗(控件实例跨窗迁移, 页面不重载)。
# v0.3.1.33: 「弹幕列表一直是空的(有概率)」根治 —— 分段补取重写成"自愈看门狗 + 代次守卫",
#           并修掉"播放器那次响应被当成整段到手"(实测只有半段: 52KB vs 98KB)。
# v0.3.1.32: 弹幕屏蔽总开关 m_dm_on(默认开) —— 关掉即完全不筛弹幕; 面板文案精简。
# v0.3.1.31: 筛选链路上的 N+1 SQL 往返与重复 fsync 全部消除(索引 + 批量 + 合并提交)。
# v0.3.1.30: MIRRORSORTER_PORT 可指定单一端口(不回落) —— 供自测/多开隔离用。
# 没有它的话, 测试实例会去抢用户正在用的 8000, 然后被页面的 /api/shutdown 关掉。
_PORT_ENV = os.environ.get("MIRRORSORTER_PORT")
if _PORT_ENV:
    try:
        PORTS = (int(_PORT_ENV),)
    except Exception:
        pass
APP_VER = "v0.4.5dsk"   # 与 web/index.html 页脚保持一致; 窗口壳(app_window.py)标题也用它
# 本机回环请求必须绕过系统代理: 用户开着 Clash/V2Ray/公司代理时, 环境里的 http_proxy 会把
# 127.0.0.1 的请求转给代理服务器, 轻则 502, 重则心跳连续失败 -> 悬浮球探活 2 轮不过自我了断,
# 表现为「球莫名消失/点定位没反应」。这里建一个禁用代理的 opener 专供本地调用; 外网 API 仍走
# 默认 urlopen 保留代理能力(国内访问境外 LLM API 常需要代理)。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_open(url, timeout=5):
    """打 127.0.0.1 的 GET: 不走任何代理。返回类文件响应(with 语法可用)。"""
    return _NO_PROXY.open(url, timeout=timeout)
PAGE_LEASE_GRACE_SECONDS = 3
PAGE_LEASE_EXPIRE_SECONDS = 150   # 租约心跳超时: 兼容后台隐藏页被节流到~1分钟的定时器
_SERVER = None
_SHUTDOWN_REQUESTED = threading.Event()
_PAGE_LEASES = set()
_PAGE_LEASE_LAST = {}    # token -> 最后一次存活心跳时间(看门狗据此识别"没发出leaving的死亡页面")
_PAGE_LEASE_LOCK = threading.Lock()
_PAGE_LEASE_GENERATION = 0
CACHE_TTL = 300          # B站数据缓存 5 分钟
PREWARM_TICK = 30        # 常驻预热巡检间隔(秒); v0.4.2 由 20 放宽: 预热本身也是抓取
PREWARM_IDLE = 900       # 池子超过这么久没有新内容 → 后台提前抓一轮(15分钟)
PREWARM_MIN = 120        # 池内可用条目少于这个数 → 后台提前补池
SCORE_TTL_DEFAULT = 86400        # AI 评分缓存 24 小时(可经 ai_cache_hours 设置 24/48)
AI_BATCH_DEFAULT = 15            # 每次 AI 评分的条目数(省钱法: 可设 30)
SPAM_CUT = 7             # 低质/营销分 ≥7 一律剔除
MODE_THRESH = {"严格": 4, "标准": 6, "宽松": 8, "严肃": 5}   # 引战分 ≥ 阈值即剔除(严肃=严格偏中)
DISCLAIMER_VERSION = "2026-09-03"

# ---------------- 设置导入导出 ----------------
# 运行状态/会话类键: 永不导出(导了也没意义, 还会把错误状态带过去)
_tieba_thread_cache = {}          # tid -> {"payload":..., "ts":...} 帖子首页缓存(10分钟, 防重复抓)
_TIEBA_THREAD_TTL = 600
_xhh_thread_cache = {}            # link_id -> {"payload":..., "ts":...} 小黑盒帖子缓存(10分钟)
_XHH_THREAD_TTL = 600
_IO_STATE_KEYS = {"fetch_error", "fetch_last", "last_feed", "last_types", "scorer_v",
                  "tb_cursor", "tb_error", "tb_last_fetch", "xhh_error", "xhh_last_fetch",
                  "xhh_qr_code", "xhh_qr_tokenid", "explore_boost_ts", "feedback_sig",
                  "tmp_blocks", "cur_conv", "disclaimer_version"}
# 账号内容/登录凭证类键: 敏感, **绝不进入脱敏导出**, 只在用户显式取消
# 「只导出设置」勾选(完整备份)时才写进文件。
# v0.3.1.39: ai_key 以前不在这个集合里 ⇒ 用户勾了「只导出设置」也照样把 aiKey 写进
# 导出文本, 分享出去就等于泄露自己的 AI 密钥(用户明确要求: aiKey、登陆账号这类账号
# 内容必须不出现在导出文件里)。同类项一并归入: 各平台 Cookie/账号画像/关注列表。
_IO_CRED_KEYS = {"bili_cookie", "tb_cookie", "tb_verify_cookie", "tb_user", "tb_avatar", "xhh_cookie",
                 "xhh_heybox_id", "xhh_pkey", "xhh_user", "xhh_avatar",
                 "acct_nickname", "acct_profile", "bili_follow_mids", "bili_follow_names",
                 "ai_key"}
# 允许导入的白名单(= 可导出的全集): 防止导入文件里塞进任意键污染 settings 表
_IO_IMPORTABLE = ({"mode", "ai_base", "ai_key", "ai_model", "ai_enabled", "ai_full_scan",
                   "ai_interest_min", "ai_batch", "ai_cache_hours", "price_in", "price_out",
                   "m_min_views", "m_max_views", "m_max_duration", "m_min_duration",
                   "m_max_age_months", "m_seen_hours", "m_repeat_limit",
                   "m_repeat_cooldown_hours", "m_dedup", "m_blocklist_sync", "m_emotion",
                   "m_news_fresh", "ser_max_age_months", "m_sources", "tb_allow_help",
                   "tb_bars", "xhh_comm", "xhh_topic", "bili_weight", "tb_weight",
                   "xhh_weight", "bili_follow_emph", "bili_follow_months",
                   "prefs_note", "dislike_note", "cf_blk_on", "cf_min_level",
                   "m_block_mystery_bio", "m_block_lottery",
                   "m_block_empty_space", "m_dm_on",
                   "ft_on", "ft_video", "ft_dm", "ft_cf", "ft_thresh", "ft_labels"} | _IO_CRED_KEYS) - _IO_STATE_KEYS
# 脱敏导出白名单(白名单, 不是黑名单): 只有列在这里的键才会进「只导出设置」的文件。
# 将来新增设置项默认**不导出**, 必须显式加进来 —— 避免新字段悄悄把敏感信息带出去。
_IO_SHAREABLE = frozenset(_IO_IMPORTABLE - _IO_CRED_KEYS)


def settings_export_payload(include_credentials):
    """收集可导出的设置 + 黑名单。

    include_credentials=False(默认, 界面「只导出设置」勾选态):
        只取脱敏白名单 _IO_SHAREABLE —— ai_key / 各平台 Cookie / 账号画像/关注列表
        一个都不进文件, 生成物可以直接分享给别人配置软件。
    include_credentials=True(用户显式取消勾选 = 完整备份):
        含账号与密钥, 界面会给出「请勿外传」告警。
    """
    allow = _IO_IMPORTABLE if include_credentials else _IO_SHAREABLE
    rows = db().execute("SELECT k,v FROM settings").fetchall()
    st = {k: v for k, v in rows if k in allow and v not in (None, "")}
    bl = {"authors": [r[0] for r in db().execute("SELECT value FROM blacklist WHERE kind='author'")],
          "keywords": [r[0] for r in db().execute("SELECT value FROM blacklist WHERE kind='keyword'")]}
    return {"app": "洞鉴虚明", "kind": "mirror-sorter-settings", "v": 1,
            "ts": int(time.time()), "sanitized": (not include_credentials),
            "settings": st, "blacklist": bl}

APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
# 正式运行统一读用户存档目录; 测试/隔离开发可用 MIRRORSORTER_DATA_DIR 覆盖。
DATA_DIR = os.environ.get("MIRRORSORTER_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), "AppData", "LocalLow", "MirrorSorter")

# 数据目录迁移(旧路径 ~/信息分拣器 → 新路径 MirrorSorter): 保留设置/登录/历史/收藏
_DATA_OLD = os.path.join(os.path.expanduser("~"), "信息分拣器")
if os.path.exists(os.path.join(_DATA_OLD, "data.db")) and not os.path.exists(os.path.join(DATA_DIR, "data.db")):
    os.makedirs(DATA_DIR, exist_ok=True)
    for _f in ("data.db", "data.db-wal", "data.db-shm"):
        _s, _d = os.path.join(_DATA_OLD, _f), os.path.join(DATA_DIR, _f)
        if os.path.exists(_s) and not os.path.exists(_d):
            try:
                shutil.move(_s, _d)
            except Exception:
                pass
os.makedirs(DATA_DIR, exist_ok=True)

if getattr(sys, "frozen", False):   # 一次性迁移: exe旁旧库 → 用户目录
    _old = os.path.dirname(sys.executable)
    for _f in ("data.db", "data.db-wal", "data.db-shm"):
        _src, _dst = os.path.join(_old, _f), os.path.join(DATA_DIR, _f)
        if os.path.exists(_src) and not os.path.exists(_dst):
            try:
                shutil.move(_src, _dst)
            except Exception:
                pass


# ---------------------------------------------------------------- 自动更新 (v0.4.1dsk)
# 更新包放 GitHub Releases（tag 形如 v0.4.1dsk，附件是对应 exe）。应用只读公开 API
# 的 releases/latest，选带 .exe 附件的版本；78MB 的二进制不进 git 历史。
# 源码运行也允许"检查"，但下载/替换只在 Windows 的 PyInstaller exe 里开放。
UPDATE_REPO = os.environ.get("MIRRORSORTER_UPDATE_REPO") or "Xblyymdf/MirrorSorter"
UPDATE_API = "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO
UPDATE_DIR = os.path.join(DATA_DIR, "update")
_UPDATE_LOCK = threading.Lock()
_UPDATE_LAST = {"info": None, "ts": 0.0}
_UPDATE_STATE = {"phase": "idle", "received": 0, "total": 0, "path": "", "error": "",
                 "version": "", "notes": "", "source": ""}
_UPDATE_ALLOW_HOSTS = ("github.com", "objects.githubusercontent.com",
                       "github-releases.githubusercontent.com")
# 国内直连 release-assets.githubusercontent.com 实测只有 30~40KB/s；公共镜像能到 MB/s。
# 只有拿到官方 SHA256 才允许走镜像，下载完必须逐字节校验一致。
_UPDATE_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/")
_UPDATE_CHUNK = 256 * 1024
_UPDATE_SOCK_TIMEOUT = 20
_UPDATE_ROUNDS = 2


def _update_ver(v):
    """v0.4.1dsk → (0, 4, 1)。只比较数字段，dsk 后缀不参与比较。"""
    m = re.match(r"^[vV]?(\d+(?:\.\d+)*)", str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _update_newer(latest, current):
    a, b = _update_ver(latest), _update_ver(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _update_url_ok(url):
    """只允许 GitHub 官方的 https 附件地址，避免被伪造的 API 响应带去任意下载源。"""
    try:
        p = urllib.parse.urlparse(str(url or ""))
        host = (p.hostname or "").lower()
        return p.scheme == "https" and (host in _UPDATE_ALLOW_HOSTS
                                        or host.endswith(".githubusercontent.com"))
    except Exception:
        return False


def _update_fetch_json(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": "MirrorSorter/%s" % APP_VER,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def update_check():
    """读 GitHub Releases 最新版。只读网络，不改任何本地文件。"""
    with _UPDATE_LOCK:
        _UPDATE_LAST["info"] = None
    cur = APP_VER.lstrip("v")
    try:
        data = _update_fetch_json(UPDATE_API)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            msg = "仓库还没有发布可用的版本（没有 Release）。"
        elif e.code in (403, 429):
            msg = "GitHub 接口被限流，请稍后再试。"
        else:
            msg = "检查更新失败：HTTP %s" % e.code
        return {"ok": False, "current": cur, "error": msg}
    except Exception as e:
        return {"ok": False, "current": cur,
                "error": "检查更新失败：%s" % str(e)[:140]}
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    exe = None
    # 只认独立运行的 exe；安装包/安装器即使也是 .exe 也不能拿来覆盖自身
    _bad = ("安装包", "installer", "setup", "install")
    _cands = []
    for a in (data.get("assets") or []):
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if not name.lower().endswith(".exe") or not _update_url_ok(url):
            continue
        _cands.append((0 if any(k in name.lower() for k in _bad) else 1, a))
    if _cands:
        _cands.sort(key=lambda x: x[0], reverse=True)
        exe = _cands[0][1]
    if exe is None:
        return {"ok": False, "current": cur, "latest": tag.lstrip("v"),
                "error": "最新版本没有附带 exe 安装包。"}
    latest = tag or str(exe.get("name") or "")
    digest = str(exe.get("digest") or "")
    info = {
        "ok": True,
        "supported": (os.name == "nt" and bool(getattr(sys, "frozen", False))),
        "current": cur,
        "latest": (latest.lstrip("v") or latest),
        "has_update": _update_newer(latest, APP_VER),
        "name": str(exe.get("name") or ""),
        "size": int(exe.get("size") or 0),
        "notes": str(data.get("body") or "")[:4000],
        "page": str(data.get("html_url") or ""),
        "published": str(data.get("published_at") or ""),
        "url": str(exe.get("browser_download_url") or ""),
        "sha256": digest[7:] if digest.lower().startswith("sha256:") else "",
    }
    if info["has_update"]:
        with _UPDATE_LOCK:
            _UPDATE_LAST["info"] = dict(info)
            _UPDATE_LAST["ts"] = time.time()
    return info


def update_progress():
    with _UPDATE_LOCK:
        st = dict(_UPDATE_STATE)
    st["ok"] = True
    return st


class _UpdateCorrupt(Exception):
    pass


def _update_sources(info):
    """镜像优先、官方兜底；没有官方 SHA256 时只走官方地址。"""
    url = str(info.get("url") or "")
    sha = str(info.get("sha256") or "").strip()
    srcs = []
    if sha and _update_url_ok(url):
        srcs = [m + url for m in _UPDATE_MIRRORS]
    if url and url not in srcs:
        srcs.append(url)
    return srcs


def _update_stream(src, part, got, total):
    """把 src 的字节续写到 part，返回 (已下载, 总量)。服务器忽略 Range 时从头重下。"""
    headers = {"User-Agent": "MirrorSorter/%s" % APP_VER}
    if got:
        headers["Range"] = "bytes=%d-" % got
    req = urllib.request.Request(src, headers=headers)
    with urllib.request.urlopen(req, timeout=_UPDATE_SOCK_TIMEOUT) as r:
        status = int(getattr(r, "status", 0) or r.getcode() or 0)
        if got and status != 206:
            got = 0
        if status == 206:
            m = re.match(r"bytes\s+(\d+)-", str(r.headers.get("Content-Range") or ""))
            if m and int(m.group(1)) != got:
                raise ValueError("续传起点不一致")
        clen = int(r.headers.get("Content-Length") or 0)
        if clen:
            total = max(total, got + clen)
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({"total": total, "received": got,
                                  "source": urllib.parse.urlparse(src).hostname or ""})
        with open(part, "ab" if got else "wb") as f:
            while True:
                chunk = r.read(_UPDATE_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                with _UPDATE_LOCK:
                    _UPDATE_STATE["received"] = got
    if not total:
        total = got
    return got, total


def _update_download_worker(info):
    part = ""
    try:
        os.makedirs(UPDATE_DIR, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z._-]", "", str(info.get("latest") or "latest")) or "latest"
        final = os.path.join(UPDATE_DIR, "洞鉴虚明%s.exe" % safe)
        part = final + ".download"
        total = int(info.get("size") or 0)
        got = os.path.getsize(part) if os.path.isfile(part) else 0
        if total and got > total:
            os.remove(part)
            got = 0
        with _UPDATE_LOCK:
            _UPDATE_STATE["received"] = got
        sources = _update_sources(info)
        last_err = ""
        if not total or got < total:
            for _round in range(_UPDATE_ROUNDS):
                if total and got >= total:
                    break
                for src in sources:
                    try:
                        got, total = _update_stream(src, part, got, total)
                    except Exception as e:
                        host = urllib.parse.urlparse(src).hostname or src
                        last_err = "%s: %s" % (host, str(e)[:80])
                        try:
                            got = os.path.getsize(part)
                        except OSError:
                            got = 0
                        continue
                    if not total or got >= total:
                        break
                    last_err = "下载未完成（%d/%d 字节）" % (got, total)
                if total and got >= total:
                    break
        if total and got < total:
            raise ValueError(last_err or "下载未完成")
        if got < 5 * 1024 * 1024:
            raise _UpdateCorrupt("下载文件太小，可能不是完整的 exe")
        with open(part, "rb") as f:
            if f.read(2) != b"MZ":
                raise _UpdateCorrupt("下载的文件不是 Windows exe")
            f.seek(0)
            h = hashlib.sha256()
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        want = str(info.get("sha256") or "").lower()
        if want and h.hexdigest().lower() != want:
            raise _UpdateCorrupt("文件校验失败（SHA256 不一致）")
        os.replace(part, final)
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({"phase": "ready", "path": final,
                                  "received": got, "total": got})
    except Exception as e:
        if part and isinstance(e, _UpdateCorrupt) and os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({"phase": "error", "error": str(e)[:200]})


def update_download():
    """后台下载最近一次检查到的 Release exe；页面轮询 progress 看进度。
    镜像 + 官方多源，Range 续传：关掉程序后再点更新会从已下载的位置接着下。"""
    if not (os.name == "nt" and getattr(sys, "frozen", False)):
        return {"ok": False, "error": "自动更新只在 Windows 桌面版 exe 里可用。"}
    with _UPDATE_LOCK:
        info = dict(_UPDATE_LAST["info"] or {})
        if not info or not info.get("url"):
            return {"ok": False, "error": "请先检测更新。"}
        if _UPDATE_STATE.get("phase") == "downloading":
            return {"ok": False, "error": "已经在下载了。"}
        if not _update_url_ok(info["url"]):
            return {"ok": False, "error": "更新地址不是 GitHub 官方地址，已拒绝。"}
        _UPDATE_STATE.update({"phase": "downloading", "received": 0,
                              "total": int(info.get("size") or 0), "path": "",
                              "error": "", "version": info.get("latest") or "",
                              "source": ""})
    threading.Thread(target=_update_download_worker, args=(info,),
                     name="update-download", daemon=True).start()
    return {"ok": True}


def _update_bat_quote(p):
    """batch 的 set "K=V" 行里 % 要写成 %%；换行/控制字符直接拒绝。"""
    s = str(p)
    if any(ch in s for ch in "\r\n\0"):
        raise ValueError("路径里含换行/控制字符")
    return s.replace("%", "%%")


def update_script_text(old_exe, new_exe):
    """生成替换脚本：旧 exe 被占用时每秒重试（最长约 3 分钟），换好后从原目录重启。
    保留旧文件名 —— 用户的快捷方式/任务栏固定都指向这个路径，升级不能换名。"""
    old_exe = os.path.abspath(old_exe)
    new_exe = os.path.abspath(new_exe)
    old_dir = os.path.dirname(old_exe)
    return "\r\n".join([
        "@echo off",
        "setlocal",
        'set "OLD=%s"' % _update_bat_quote(old_exe),
        'set "NEW=%s"' % _update_bat_quote(new_exe),
        'set "OLD_DIR=%s"' % _update_bat_quote(old_dir),
        'if exist "%OLD%" copy /y "%OLD%" "%OLD%.bak" >/dev/null 2>nul',
        "for /l %%i in (1,1,180) do (",
        '  move /y "%NEW%" "%OLD%" >nul 2>nul && goto replaced',
        "  >nul 2>nul ping -n 2 127.0.0.1",
        ")",
        "goto failed",
        ":replaced",
        'cd /d "%OLD_DIR%"',
        'start "" "%OLD%"',
        'del "%~f0" >nul 2>nul',
        "exit /b 0",
        ":failed",
        'del "%~f0" >nul 2>nul',
        "exit /b 1",
        "",
    ])


def update_prepare_install(new_exe):
    """校验下载文件并写好替换脚本。返回 {ok, script, old} / {ok:false,error}。
    真正的关窗与拉起 cmd 在 app_window.ShellApi.install_update 里做。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return {"ok": False, "error": "自动更新只在 Windows 桌面版 exe 里可用"}
    new_exe = os.path.abspath(str(new_exe or ""))
    updir = os.path.abspath(UPDATE_DIR)
    try:
        if os.path.normcase(os.path.commonpath([new_exe, updir])) != os.path.normcase(updir):
            return {"ok": False, "error": "更新文件不在更新目录里"}
    except ValueError:
        return {"ok": False, "error": "更新文件路径无效"}
    if not new_exe.lower().endswith(".exe") or not os.path.isfile(new_exe):
        return {"ok": False, "error": "更新文件不存在"}
    if os.path.getsize(new_exe) < 5 * 1024 * 1024:
        return {"ok": False, "error": "更新文件不完整"}
    try:
        with open(new_exe, "rb") as f:
            if f.read(2) != b"MZ":
                return {"ok": False, "error": "更新文件不是 exe"}
    except OSError as e:
        return {"ok": False, "error": "读取更新文件失败：%s" % str(e)[:100]}
    old_exe = os.path.abspath(sys.executable)
    old_dir = os.path.dirname(old_exe)
    probe = os.path.join(old_dir, ".msx_upd_probe")
    try:
        with open(probe, "wb") as f:
            f.write(b"1")
        os.remove(probe)
    except OSError:
        return {"ok": False, "error": "程序目录不可写，请把 exe 放到用户目录（如桌面/下载）后再试"}
    try:
        script = os.path.join(updir, "apply_update.cmd")
        with open(script, "w", encoding="gbk", newline="") as f:
            f.write(update_script_text(old_exe, new_exe))
    except Exception as e:
        return {"ok": False, "error": "写入更新脚本失败：%s" % str(e)[:120]}
    return {"ok": True, "script": script, "old": old_exe}



def web_path(name):
    base = getattr(sys, "_MEIPASS", APP_DIR)   # PyInstaller onefile 时资源在临时目录
    return os.path.join(base, "web", name)


# ---------------------------------------------------------------- 数据库

_db_lock = threading.RLock()   # RLock: 允许嵌套(锁内再调 sget 等); 用于多语句复合
_tls = threading.local()       # 每线程独立连接: 共享连接并发读写会竞态(InterfaceError/空值)


# ── 合并提交(v0.3.1.31) ──────────────────────────────────────────────────
# WAL 下每次 commit 都是一次 fsync, 实测 ~1.2ms。一次 /api/feed 的尾部原本要提交 4 次
# (record_feed_presentations / shown 回写 / set_last_feed / last_types), 合计 ~5ms
# 全花在磁盘同步上 —— 而这 4 批写没有任何一批需要"立刻对别的线程可见"。
# 用 _batch_commit() 圈起来后只落 1 次 fsync。同连接内的读不受影响(未提交的写对本连接
# 立即可见), 代价只是别的线程晚几毫秒看到这批写。
_defer = threading.local()


def _commit():
    """在 _batch_commit() 区域内 → 攒着, 出区时统一提交; 否则立刻提交。"""
    if getattr(_defer, "n", 0) > 0:
        _defer.dirty = True
        return
    db().commit()


@contextlib.contextmanager
def _batch_commit():
    """把区域内所有 _commit() 合并成一次真正的 commit。可嵌套(只最外层提交)。"""
    _defer.n = getattr(_defer, "n", 0) + 1
    if _defer.n == 1:
        _defer.dirty = False
    try:
        yield
    finally:
        _defer.n -= 1
        if _defer.n == 0 and getattr(_defer, "dirty", False):
            _defer.dirty = False
            try:
                db().commit()
            except Exception:
                pass


def db():
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(os.path.join(DATA_DIR, "data.db"), check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")   # 并发写等待10s, 防 database is locked
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS items(key TEXT PRIMARY KEY, json TEXT, fetched_at INTEGER,
                                          score_json TEXT, score_at INTEGER);
        CREATE TABLE IF NOT EXISTS feedback(key TEXT, author TEXT, action TEXT, ts INTEGER);
        /* 负反馈硬排除每次出页都要跑 SELECT DISTINCT key FROM feedback WHERE action='dislike';
           没有索引时 EXPLAIN QUERY PLAN 是 "SCAN feedback | USE TEMP B-TREE FOR DISTINCT"
           (v0.3.1.31 实测)。(action, key) 复合索引让 action 走等值查找、key 天然有序,
           DISTINCT 免排序; 表长大以后这就是 O(n) → O(log n)。 */
        CREATE INDEX IF NOT EXISTS idx_feedback_action_key ON feedback(action, key);
        CREATE TABLE IF NOT EXISTS blacklist(kind TEXT, value TEXT, PRIMARY KEY(kind, value));
        CREATE TABLE IF NOT EXISTS saved(key TEXT, kind TEXT, ts INTEGER, PRIMARY KEY(key, kind));
        CREATE TABLE IF NOT EXISTS history(key TEXT, ts INTEGER, PRIMARY KEY(key));
        CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, ts INTEGER, cid INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS conv(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, ts INTEGER, updated INTEGER);
        CREATE TABLE IF NOT EXISTS stats(day TEXT PRIMARY KEY, in_tokens INTEGER, out_tokens INTEGER,
                                         calls INTEGER, received INTEGER, filtered INTEGER);
        CREATE TABLE IF NOT EXISTS ablock(name TEXT PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS dm_sender(hash TEXT PRIMARY KEY, mids TEXT,
                                              ncand INTEGER, ts INTEGER);
        CREATE TABLE IF NOT EXISTS name_mid(name TEXT PRIMARY KEY, mid TEXT, ts INTEGER);
        /* 已确认**不存在**的账号(acc/info 返回 -404「啥都木有」)。
           多候选判定时这类候选不可能是发送者, 必须排除 —— 否则「全部候选都命中才拦」
           永远等不到那几个查不出画像的候选, 规则永不生效(v0.3.1.28)。 */
        CREATE TABLE IF NOT EXISTS absent_mid(mid TEXT PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS kw(kw TEXT PRIMARY KEY, last_used INTEGER, new_cnt INTEGER,
                                      ok_cnt INTEGER, gid TEXT);
        CREATE TABLE IF NOT EXISTS shown(key TEXT PRIMARY KEY, ts INTEGER);
        CREATE TABLE IF NOT EXISTS item_presentation(key TEXT, request_id TEXT, presented_at INTEGER,
                                                      PRIMARY KEY(key, request_id));
        CREATE TABLE IF NOT EXISTS item_exposure_state(key TEXT PRIMARY KEY, run_count INTEGER NOT NULL DEFAULT 0,
                                                       last_presented_at INTEGER NOT NULL DEFAULT 0,
                                                       cooldown_until INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS rebut_saved(id INTEGER PRIMARY KEY AUTOINCREMENT,
                                               source TEXT, seg TEXT, content TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS author_meta(mid TEXT PRIMARY KEY, sign TEXT,
                                               mystery INTEGER DEFAULT 0,
                                               lottery INTEGER DEFAULT 0,
                                               nodyn INTEGER, ts INTEGER);
        """)
        try:   # 老库补 gid 列
            conn.execute("ALTER TABLE kw ADD COLUMN gid TEXT")
        except Exception:
            pass
        try:   # 老库补 nodyn 列(空首页判定: 该账号是否一条动态都没有)
            # 新列留 NULL = 未知 —— _am_row_get 见到 NULL 当作过期, 下次按需重取
            conn.execute("ALTER TABLE author_meta ADD COLUMN nodyn INTEGER")
        except Exception:
            pass
        try:   # 老库补 level 列(弹幕侧等级筛选: 弹幕接口不提供等级, 只能按缓存账号资料判)
            conn.execute("ALTER TABLE author_meta ADD COLUMN level INTEGER")
        except Exception:
            pass
        try:   # 老库补 ablock.mid 列(拉黑名单接口自带 mid, 存下来供弹幕按 hash 就地屏蔽)
            conn.execute("ALTER TABLE ablock ADD COLUMN mid TEXT")
        except Exception:
            pass
        try:   # 老库补 cid 列(多会话); 旧消息归为默认会话
            conn.execute("ALTER TABLE chat ADD COLUMN cid INTEGER DEFAULT 0")
        except Exception:
            pass
        try:   # 分区源重构后清理旧内容池(一次性迁移)
            if conn.execute("SELECT v FROM settings WHERE k='feed_version'").fetchone() is None:
                conn.execute("DELETE FROM items")
                conn.execute("DELETE FROM shown")
                conn.execute("DELETE FROM kw")
                conn.execute("INSERT INTO settings(k,v) VALUES('feed_version','2')")
        except Exception:
            pass
        try:   # v0.3.1.52: B站直播默认权重 25 → 5(一次性迁移)
            # 老存档里 live_weight 只会是旧默认 25(上个版本三档按钮里挑出来的也是它),
            # 用户这次明确要求直播占 5 —— 已经是别的值(用户自己调过)就不动。
            if conn.execute("SELECT v FROM settings WHERE k='live_weight_mig52'").fetchone() is None:
                row = conn.execute("SELECT v FROM settings WHERE k='live_weight'").fetchone()
                if row is not None and str(row[0]) == "25":
                    conn.execute("INSERT INTO settings(k,v) VALUES('live_weight','5') "
                                 "ON CONFLICT(k) DO UPDATE SET v='5'")
                conn.execute("INSERT INTO settings(k,v) VALUES('live_weight_mig52','1') "
                             "ON CONFLICT(k) DO UPDATE SET v='1'")
        except Exception:
            pass
        if conn.execute("SELECT COUNT(*) FROM conv").fetchone()[0] == 0:
            try:   # 多线程首建连接可能同时插入, 冲突忽略
                conn.execute("INSERT INTO conv(id,title,ts,updated) VALUES(1,'默认对话',?,?)",
                             (int(time.time()), int(time.time())))
            except Exception:
                pass
        # 老库消息(无cid)归入默认会话1。
        # ⚠ 必须先判有无再写: 这条 UPDATE 曾是本连接的**唯一写语句**, 无条件执行时
        # 只要别的线程/进程正持写锁, 就要在 PRAGMA busy_timeout=10000 上等满 10 秒,
        # 然后抛 `sqlite3.OperationalError: database is locked`。
        # 实测(desktop\_db_lock_bench.py --steps, 用户库副本 + 另一连接 BEGIN IMMEDIATE):
        #   合计 10884ms  ⇒  UPDATE chat SET cid=1 WHERE cid=0  10883ms  <<< 全部耗时在此
        #   WAL pragma 0.3ms / 3233 字符 DDL executescript 0.1ms / 5 条 ALTER 各 0.0ms
        #   / SELECT feed_version 0.0ms / SELECT COUNT(*) FROM conv 0.0ms
        # 窗口壳的注入串构建(_build_inject_script)跑在 WinForms UI 线程的 on_ready 回调里,
        # 中途要读十几次设置(sget 是纯读) —— 一旦撞上这 10 秒, 整窗「未响应」、注入串
        # 永不落地, 用户看到的就是「双击后一直卡死」。改成先判有无后, 稳态下这条写不再
        # 发生, 新连接首访纯读(WAL 下读不被写阻塞, 实测 1ms), 卡死根因消除。
        if conn.execute("SELECT 1 FROM chat WHERE cid=0 LIMIT 1").fetchone() is not None:
            conn.execute("UPDATE chat SET cid=1 WHERE cid=0")
        conn.commit()
        _tls.conn = conn
    return conn


DEFAULTS = {
    "ai_base": "https://api.deepseek.com/v1",
    "ai_key": "",
    "ai_model": "deepseek-chat",
    "ai_enabled": "1",        # AI 总开关: 0=全部AI功能停用(评分/聊天/添词都不烧钱), 1=按其它设置运行
    "mode": "标准",
    "prefs_note": "",
    "dislike_note": "",
    "price_in": "1.0",      # ¥/百万输入token
    "price_out": "2.0",     # ¥/百万输出token
    "m_min_views": "0",     # 机器筛选: 低于此播放量剔除(0=关闭)
    "m_max_views": "0",     # 机器筛选: 高于此播放量剔除(0=关闭)
    "m_max_duration": "0",  # 机器筛选: 时长超过此秒数剔除(0=关闭)
    "m_min_duration": "60", # 机器筛选: 时长低于此秒数剔除(默认1分钟)
    "m_dedup": "1",         # 机器筛选: 相似标题去重
    "m_blocklist_sync": "1",  # 机器筛选: 同步B站账号拉黑名单并屏蔽
    "m_block_mystery_bio": "0",  # 评论筛选(B站): 屏蔽无个性简介(空简介/"这个人很神秘")账号的评论(默认关)
    "m_block_lottery": "0",    # 评论筛选(B站): 屏蔽动态抽奖账号的评论(默认关; 需登录B站, 只读缓存)
    "m_block_empty_space": "0",  # 评论筛选(B站): 屏蔽空首页账号(个人空间一条动态都没有)的评论(默认关; 需登录B站)
    "m_dm_multicand": "1",    # 弹幕筛选: 一个哈希对上多个账号时怎么办(0=放行 1=全部候选都命中才拦(零误杀, 默认) 2=任一命中即拦)
    "m_filter_speed": "2",    # 筛选加速档位: 后台补画像/逆推哈希的并发与速率(0=兼容 1=标准 2=快速(默认) 3=极速)
    # v0.3.1.32 弹幕屏蔽总开关: 0=不筛**按发送者身份**的弹幕(不查发送者资料, 注入端连哈希表都不建)。
    # 只影响弹幕; 评论筛选(mystery/lottery/empty/level/黑名单)照常。
    # ★ v0.3.1.46dsk: 它**管不到**「快速文本分析」的弹幕通道(ft_dm) —— 那是"发了什么",
    #   与"谁发的"是两条独立通道, 设置页也是这么对用户承诺的。旧代码在注入脚本里
    #   写成 NEED_TEXT = DM_ON && ft_dm, 导致"关总开关 + 开 fastText"静默失效。
    "m_dm_on": "1",
    # ---- v0.3.1.43dsk 快速文本分析(fastText 本地小模型) ----
    # 与 m_dm_on / cf_* 相互独立的总开关: 上面那两组管"谁发的"(账号身份),
    # 这里管"发了什么"(文本内容)。两边各自开关, 互不牵连。
    "ft_on": "0",          # 总开关: 0=关闭(默认), 1=启用(加载本地模型做内容分拣)
    "ft_video": "1",       # 作用范围: 信息筛选(视频标题+简介/贴吧标题+摘要)
    "ft_dm": "1",          # 作用范围: 弹幕
    "ft_cf": "1",          # 作用范围: 评论
    "ft_thresh": "70",     # 判定阈值(%), 越高越宽松(70=概率≥0.70 才拦)
    "ft_labels": "flame,spam,clickbait,yy",   # 启用哪些标签(逗号分隔)
    # v0.3.1.34 信息打开方式(桌面窗口壳): 1=点开信息弹独立悬浮窗(默认; 悬浮窗可塞回标签页,
    # 标签页也能拖出成悬浮窗) 0=旧的标签页内打开。仅桌面壳生效, 安卓/浏览器忽略。
    "ui_new_window": "1",
    "m_max_age_months": "5",  # 机器筛选: 只显示最近N个月发布的内容(0=不限)
    "ser_max_age_months": "30",  # 严肃模式独立时间窗: 知识/教程内容历史长, 默认30个月(0=不限)
    "m_seen_hours": "6",      # 通用筛选: 已看信息N小时内不重复出现(0=不限)
    "m_cold_start_n": "200",  # 启动预缓存: 退出时缓存池首未展示条目N条, 下次打开直接展示(0=关闭)
                              # 原来是 10 —— 重启后只有 10 条垫底, 其余全靠重建(10~30 秒), 这与
                              # "打开就能刷"的目标正相反。缓存是纯本地 JSON, 不联网。
    "m_repeat_limit": "4",    # 通用筛选: 同一信息累计展示N次后进入冷却
    "m_repeat_cooldown_hours": "72",  # 通用筛选: 冷却N小时后再显示
    "m_play_related": "1",    # 机器筛选: 播放页推荐视频按规则筛选并与泛热门7:3混排(默认开)
    "m_emotion": "1",         # 机器筛选: 情绪过滤器(感叹号轰炸/引战黑话/标题党句式, 零token)
    "m_news_fresh": "1",      # 机器筛选: 资讯/热点类只推送24小时内(干货/教程不受此限)
    "bili_follow_emph": "1",  # B站关注的人: 强调关注up发布的视频(入池+视觉强调)
    "bili_follow_months": "1",  # B站关注的人: 只强调最近N个月发布(0=不限, 滑条0-12)
    "cf_blk_on": "1",         # 评论筛选: 屏蔽黑名单(作者名单+已同步B站拉黑)内用户评论(1=开)
    "cf_min_level": "2",      # 评论筛选: 屏蔽B站等级低于N级的用户评论(1=不过滤)
    "ai_full_scan": "0",      # AI推荐模式: 1=所有候选都AI评分+推荐理由(费token), 0=只评热度前120条
    "ai_interest_min": "0",   # 推荐标准: 兴趣分门槛(0=不限, 5=一般, 7=严格)
    "ai_batch": "15",         # 省钱方式: 每批评分条数(15=稳, 30=省一半请求)
    "ai_cache_hours": "24",   # 省钱方式: 评分缓存时长(24h/48h)
    "tb_bars": "",            # 贴吧吧名列表(每行一个/逗号分隔), 空=不采集
    "tb_weight": "25",        # 贴吧平台权重%(0/25/50/75/90), 与 bili_weight 互比
    "bili_weight": "90",      # B站平台权重%(0/25/50/75/90)
    "tb_cookie": "",          # 贴吧登录凭证(BDUSS/STOKEN 等), 空=匿名会话
    # v0.3.1.55: 百度安全验证(403)走人工滑块后写回的 cookie。与 tb_cookie 合并成
    # 请求头发出 —— 只带登录 cookie 时, 验证窗口里过掉的那道 WAF 会话带不过去。
    "tb_verify_cookie": "",   # 验证窗口拿到的 baidu.com cookie(只用于贴吧请求)
    "tb_user": "",            # 贴吧登录昵称(展示用)
    "tb_avatar": "",          # 贴吧头像URL(登录时从页面抓取)
    "tb_allow_help": "1",     # 贴吧: 允许求助帖(0=过滤"求助/求教/请问"类提问帖)
    "xhh_weight": "15",       # 小黑盒平台权重%(0/25/50/75/100)
    # v0.3.1.51: B站直播作为独立内容源(进信息流, 也有自己的直播页)。
    # v0.3.1.52: 用户要求把直播占比下调到 5%(它只是信息流里的补充, 不该和 B站 抢版面)。
    "live_weight": "5",      # B站直播平台权重%(0-100, 相对另外几个平台的权重)
    "live_last_fetch": "0",   # 直播源最近一次抓取时间戳(状态卡/内容源状态用)
    "live_error": "",         # 直播源最近一次抓取错误(空=正常)
    "xhh_cookie": "",         # 小黑盒设备tokenid(x_xhh_tokenid, 扫码登录后保存)
    "xhh_pkey": "",           # 小黑盒登录会话token(pkey, 网页版真实凭证)
    "xhh_heybox_id": "",      # 小黑盒用户ID(heybox_id/userid)
    "xhh_user": "",           # 小黑盒登录昵称
    "xhh_avatar": "",         # 小黑盒头像URL(登录时抓取)
    "xhh_comm": "",           # 小黑盒社区/板块筛选(逗号分隔, 空=全部)  >> 匹配帖子 topics
    "xhh_topic": "",          # 小黑盒话题筛选(逗号分隔, 空=全部)      >> 匹配帖子 hashtags
    "xhh_qr_code": "",        # 当前登录二维码code
    "xhh_qr_tokenid": "",     # 扫码登录固定tokenid(生成与轮询必须一致, 登录态绑定发码方)
    "tmp_blocks": "",         # 临时屏蔽(聊天指令): [{"k":词,"until":ts}] 到点自动解封
    "last_types": "",         # 上次会话末尾5条的一级分类(跨会话防重复: 新会话前5条避开)
    "explore_boost_ts": "0",  # 探索模式(聊天指令): 生效截止时间戳(0=未启用)
    "scorer_v": "0",          # 评分器版本(升级新维度时一次性标记)
    "disclaimer_version": "", # 用户已确认的免责声明版本
    "float_ball": "0",        # AI 悬浮窗(仅电脑版): 1=屏幕常驻悬浮球, 点击展开 AI 验证/回怼对话面板
    "float_ball_accent": "",  # 悬浮球强调色(主界面切换强调色时推送; 空=默认粉)
    "float_ball_light": "0",  # 悬浮球浅色主题(跟随主界面深浅主题)
    "assist_think": "标准",   # 悬浮窗思考档位: 快/标准/深挖(输出额度+推理提示)
    "assist_persona": "不设",  # 悬浮窗身份档位: 不设/理工科/行业老手/数据党/路人(决定拿什么当武器)
    "assist_savage": "毒舌律师",  # 悬浮窗风格档位: 冷静理性/毒舌律师/阴阳怪气(旧值温和/毒舌/阴阳自动折算)
    "assist_len": "2",        # 悬浮窗输出档位: 1/2/3, 只调节条数(一句话与一段话各 N 条, 长篇恒 1 条)
    # 注: 旧的 assist_hover(悬停自动展开)键已弃用 —— 该功能按用户要求整体移除, 只剩点击展开。
    # 老库里残留的这个键不再被读取, 留着不删以免动到用户数据。
    "assist_personas": "[]",  # 自定义人设预设(JSON 数组, 见 persona_presets)
}


# v0.3.1.31: settings 表只有几十行, 但一次 /api/feed 要读 ~28 次 —— 每次都 _db_lock
# + 一条 SQL。全进程只有 sset 一个写入点(实测: 只有建库初始化与 sset 两处写 settings;
# /api/settings/import 也是走 sset), 所以在 sset 里同步更新缓存即可, 不需要失效扫描。
_SET_CACHE = {}
_SET_LOCK = threading.Lock()
_SET_MISS = object()


def _set_cache_clear():
    """极端情况兜底(外部直接改库/换库文件后调用)。"""
    with _SET_LOCK:
        _SET_CACHE.clear()


def sget(key):
    v = _SET_CACHE.get(key, _SET_MISS)
    if v is not _SET_MISS:
        return v
    # 锁保护: _FX 8线程并发 sget 会导致共享连接读竞态(InterfaceError/空值)
    with _db_lock:
        row = db().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    v = row[0] if row else DEFAULTS.get(key, "")
    with _SET_LOCK:
        _SET_CACHE[key] = v
    return v


def sset(key, value):
    value = str(value)
    with _db_lock:
        db().execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (key, value))
        _commit()   # v0.3.1.31: 区域内合并提交
    with _SET_LOCK:
        _SET_CACHE[key] = value


def today():
    return time.strftime("%Y-%m-%d")


def bump_stats(**kw):
    with _db_lock:
        conn = db()
        cols = ",".join("%s=IFNULL(%s,0)+?" % (k, k) for k in kw)
        conn.execute("INSERT INTO stats(day,%s) VALUES(?,%s) ON CONFLICT(day) DO UPDATE SET %s"
                     % (",".join(kw), ",".join("?" * len(kw)), cols),
                     (today(),) + tuple(kw.values()) + tuple(kw.values()))
        conn.commit()


def day_stats():
    row = db().execute("SELECT IFNULL(in_tokens,0),IFNULL(out_tokens,0),IFNULL(calls,0),"
                       "IFNULL(received,0),IFNULL(filtered,0) FROM stats WHERE day=?",
                       (today(),)).fetchone()
    if not row:
        return {"ai_calls": 0, "cost": 0.0, "received": 0, "filtered": 0}
    pi, po = float(sget("price_in")), float(sget("price_out"))
    cost = (row[0] * pi + row[1] * po) / 1e6
    return {"ai_calls": row[2], "cost": round(cost, 4), "received": row[3], "filtered": row[4]}


# ---------------------------------------------------------------- 筛选漏斗仪表盘(今日统计, 进程内)
# 记录"抓回多少条 → 每条规则各扔多少条 → 最终留多少", 用于发现异常漏斗并做抓取侧优化。
_funnel_lock = threading.Lock()
_funnel = {"day": today(), "counts": {}}
_FUNNEL_LABELS = {
    "fetched": "抓回候选", "partition": "分区不符", "age": "超时间窗", "news": "资讯保鲜",
    "views": "播放量窗口", "duration": "时长窗口", "emotion": "情绪/广告过滤", "dislike": "厌恶词",
    "ft": "快速文本分析",
    "blocklist": "拉黑名单", "help": "求助帖", "xhh": "小黑盒筛选", "dedup": "重复标题",
    "follow_window": "关注超强调窗", "cooldown": "展示冷却", "cooldown_relaxed": "冷却不足复用", "ai_gate": "AI门禁", "kept": "最终入池",
    "pref_fetch": "偏好·搜到", "pref_kept": "偏好·入池", "pref_drop": "偏好·被拦",
    "pref_age": "偏好·超时间窗",
    "build_full": "全量构建", "build_quick": "快速构建",
}


def _funnel_bump(key, n=1):
    with _funnel_lock:
        if _funnel["day"] != today():
            _funnel.update(day=today(), counts={})
        _funnel["counts"][key] = _funnel["counts"].get(key, 0) + n


def funnel_stats():
    with _funnel_lock:
        if _funnel["day"] != today():
            _funnel.update(day=today(), counts={})
        return dict(_funnel["counts"])


# ---------------------------------------------------------------- B站数据源

# 每次刷新轮换取 6 个源(交错组合), 保证刷新时出现新内容
# 每项: (显示名, 分组id, 接口地址); 分组可手动开关(默认全开)
SOURCE_GROUPS = [
    ("rank", "全站榜"), ("hot", "热门"),
    ("anime", "动画"), ("bangumi", "番剧"), ("guochuang", "国创"), ("guichu", "鬼畜"),
    ("music", "音乐"), ("dance", "舞蹈"), ("game", "游戏"), ("esports", "电竞"),
    ("knowledge", "知识"), ("tech", "科技"), ("digital", "数码"),
    ("life", "生活"), ("food", "美食"), ("animal", "动物圈"), ("vlog", "生活Vlog"),
    ("film", "影视"), ("movie", "电影"), ("series", "电视剧"), ("documentary", "纪录片"),
    ("sports", "运动"), ("events", "赛事"), ("auto", "汽车"),
    ("fashion", "时尚"), ("beauty", "美妆"), ("ent", "娱乐"), ("comic", "漫画"),
    ("news", "资讯"), ("live", "直播"), ("military", "军事"),
]
SOURCES_PER_FETCH = 6
_src_cursor = 0
_page_cursor = 0

# 源产出率自学习: 记录每个数据源"抓回→机器筛选存活"的通过率(指数衰减)。
# 通过率过低的源少抓/跳过, 把抓取预算让给高产源 —— 避免"抓1万条只剩几条"的漏斗浪费。
_SRC_YIELD_MIN = 0.12        # 低于此通过率的源被裁剪(从未抓过的源不裁, 给冷启动机会)
_SRC_YIELD_DECAY = 0.7       # 每轮旧数据衰减系数, 近期表现权重更高
_src_yield = {}              # gid -> {"kept": n, "fetched": n, "yield": f}
_src_yield_lock = threading.Lock()


def _src_yield_update(gid, fetched, kept):
    if not gid or fetched <= 0:
        return
    rate = kept / float(fetched)
    with _src_yield_lock:
        cur = _src_yield.get(gid) or {}
        old = cur.get("yield", rate)
        _src_yield[gid] = {"fetched": cur.get("fetched", 0) + fetched,
                           "kept": cur.get("kept", 0) + kept,
                           "yield": _SRC_YIELD_DECAY * old + (1 - _SRC_YIELD_DECAY) * rate}


def _src_yield_ok(gid):
    """该源历史通过率是否允许本轮抓取(未知=允许)。"""
    with _src_yield_lock:
        v = _src_yield.get(gid)
    return not v or v["yield"] >= _SRC_YIELD_MIN


def _src_yield_recover(gid, amount=0.15):
    """被跳过的源缓慢恢复通过率(epsilon-greedy 探索): 防止低产源被裁后不再被抓、
    通过率永不更新而永久饿死。每跳过一轮回升一档, 几轮后回到阈值以上重新参与抓取。"""
    with _src_yield_lock:
        v = _src_yield.get(gid)
        if v:
            v["yield"] = min(1.0, v["yield"] + (1.0 - v["yield"]) * amount)


def _src_yield_prune(avail, per):
    """低产源裁剪: 按传入(轮转)顺序优先取高产/未知源, 不足 per 个时用低产源补齐(宁多勿空)。"""
    if len(avail) <= per:
        return avail
    with _src_yield_lock:
        low = {g for g, v in _src_yield.items() if v["yield"] < _SRC_YIELD_MIN}
    if not low:
        return avail[:per]
    good = [s for s in avail if s[1] not in low][:per]
    if len(good) < per:
        good += [s for s in avail if s[1] in low][:per - len(good)]
    picked_gids = {s[1] for s in good}
    for g in low:   # 本轮没被选中的低产源缓慢恢复, 保证之后仍有重试机会
        if g not in picked_gids:
            _src_yield_recover(g)
    return good

# 分区内容走"搜索词引擎"(真实按分区); 这里只有全球性榜单(实测 popular 的 rid 参数被B站无视!)
# (显示名, 分组id, URL模板, rid, 可翻页数)
BILI_SOURCES = [
    ("全站榜", "rank", "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all", 0, 1),
    ("热门1", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
    ("热门2", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
    ("热门3", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
    ("热门4", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
    ("热门5", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
    ("热门6", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
    ("热门7", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
    ("热门8", "hot", "https://api.bilibili.com/x/web-interface/popular?ps=50&pn={p}", 0, 8),
]


def enabled_source_ids():
    raw = sget("m_sources")
    if not raw:
        return {g[0] for g in SOURCE_GROUPS}   # 默认全部允许
    try:
        ids = json.loads(raw)
    except Exception:
        return {g[0] for g in SOURCE_GROUPS}
    return {i for i in ids if any(g[0] == i for g in SOURCE_GROUPS)}


# ---------------------------------------------------------------- 相关推荐链 + 搜索词引擎(WBI)

MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
             33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
             61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
             11, 36, 20, 34, 44, 52]
_wbi_cache = {"ts": 0, "img": "", "sub": ""}


def wbi_keys():
    if time.time() - _wbi_cache["ts"] > 3600:
        try:
            d = bili_get("https://api.bilibili.com/x/web-interface/nav")
            img = (d.get("data") or {}).get("wbi_img", {})
            def key(u):
                return (u.rsplit("/", 1)[-1].split(".")[0]) if u else ""
            _wbi_cache.update(ts=time.time(), img=key(img.get("img_url")), sub=key(img.get("sub_url")))
        except Exception:
            return "", ""
    return _wbi_cache["img"], _wbi_cache["sub"]


def wbi_enc(params):
    """对请求参数做 WBI 签名(社区公开算法)。"""
    img, sub = wbi_keys()
    if not img:
        return urllib.parse.urlencode(params)
    mixin = "".join((img + sub)[i] for i in MIXIN_TAB)[:32]
    p = dict(params)
    p["wts"] = int(time.time())
    q = urllib.parse.urlencode(sorted(p.items()))
    q = re.sub(r"[\x21\x27\x28\x29\x2a]", "", q)
    p["w_rid"] = hashlib.md5((q + mixin).encode("utf-8")).hexdigest()
    return urllib.parse.urlencode(p)


def normalize_search(it):
    """搜索接口返回条目 → 统一格式(标题带html标签/播放是字符串/时长 mm:ss)。"""
    title = re.sub(r"<[^>]+>", "", html.unescape(it.get("title", "")))
    views = it.get("play", 0)
    if isinstance(views, str):
        m = re.match(r"([\d.]+)(万|亿)?", views)
        views = int(float(m.group(1)) * (10000 if m.group(2) == "万" else 100000000 if m.group(2) == "亿" else 1)) if m else 0
    dur = it.get("duration") or "00:00"
    try:
        parts = [int(x) for x in str(dur).split(":")]
        duration = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)
    except Exception:
        duration = 0
    pic = (it.get("pic") or "").replace("http://", "https://")
    if pic.startswith("//"):
        pic = "https:" + pic
    tag = it.get("tag")
    if isinstance(tag, str):
        tag = tag.strip()   # B站搜索/相关返回: 逗号分隔字符串(可能带html标签)
        tag = re.sub(r"<[^>]+>", "", html.unescape(tag))
    elif isinstance(tag, list):   # 有些接口返回数组
        tag = ",".join(str(x) for x in tag)
    else:
        tag = ""
    return {"key": "https://www.bilibili.com/video/%s" % it.get("bvid", ""),
            "platform": "B站", "title": title, "author": it.get("author", ""),
            "author_mid": it.get("mid") or it.get("author_mid") or it.get("uid") or 0,
            "desc": (it.get("description") or "")[:500], "cover": pic,
            "views": views, "views_known": True, "likes": 0, "duration": duration, "duration_known": True,
            "danmaku": it.get("danmaku", 0), "reply": it.get("reply", 0),
            "pubdate": it.get("pubdate", 0),
            "url": "https://www.bilibili.com/video/%s" % it.get("bvid", ""),
            "gid": "search", "tag": tag[:200],
            "area": it.get("parent_area_name") or it.get("typename") or "",
            "tname": it.get("typename") or ""}


def fetch_related(seed):
    """相关推荐链: 从种子视频拉最多40条相关(免登录免签名)。"""
    bvid = (seed.get("url") or "").rsplit("/", 1)[-1]
    if not bvid:
        return []
    try:
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/archive/related?bvid=%s" % bvid,
            headers=HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
    except Exception:
        return []
    return [it for it in (d.get("data") or []) if it.get("bvid")]


def pick_seeds(items):
    """选高价值种子: 用户点过赞的 > 账号常看作者 > 高热兜底, 最多4个。"""
    seeds = []
    for (k,) in db().execute(
            "SELECT key FROM feedback WHERE action='like' ORDER BY ts DESC LIMIT 6").fetchall():
        r = db().execute("SELECT json FROM items WHERE key=?", (k,)).fetchone()
        if r:
            try:
                seeds.append(json.loads(r[0]))
            except Exception:
                pass
    if not seeds:
        acct = account_slim()
        for a in (acct.get("authors") or [])[:3]:
            for it in items:
                if it["author"] == a["name"]:
                    seeds.append(it)
                    break
    if not seeds:
        seeds = sorted(items, key=lambda x: -x["views"])[:4]
    out, seen_url = [], set()
    for s in seeds:
        if s.get("url") and s["url"] not in seen_url:
            seen_url.add(s["url"])
            out.append(s)
        if len(out) >= 4:
            break
    return out


WORD_BANK = {
    "anime": ["新番推荐", "动漫混剪", "动画解说"],
    "bangumi": ["新番推荐", "追番", "四月新番", "十月新番"],
    "guochuang": ["国漫", "国产动画", "国创"],
    "guichu": ["鬼畜搞笑", "鬼畜调教"],
    "dance": ["宅舞", "舞蹈翻跳"],
    "life": ["生活vlog", "美食教程", "收纳整理"],
    "food": ["美食探店", "家常菜", "烘焙", "地方美食"],
    "animal": ["萌宠", "猫咪", "狗狗", "动物世界"],
    "vlog": ["日常vlog", "旅行vlog", "记录生活"],
    "film": ["电影解说", "影视混剪", "剧集推荐"],
    "movie": ["电影预告", "电影解说", "影评"],
    "series": ["电视剧", "追剧", "剧集解说"],
    "documentary": ["纪录片", "自然纪录片", "历史纪录片"],
    "music": ["音乐翻唱", "live现场", "原创音乐"],
    "game": ["独立游戏", "单机游戏", "游戏评测"],
    "esports": ["电竞", "LPL", "KPL", "赛事集锦"],
    "knowledge": ["科普视频", "硬核知识", "学习方法"],
    "tech": ["人工智能", "前沿科技", "科技资讯"],
    "digital": ["数码评测", "手机体验", "装机教程"],
    "sports": ["健身", "体育", "球类", "运动"],
    "events": ["体育赛事", "联赛", "比赛集锦"],
    "beauty": ["美妆教程", "护肤测评"],
    "fashion": ["穿搭分享", "时尚搭配"],
    "auto": ["汽车评测", "新能源车"],
    "ent": ["综艺", "娱乐", "明星", "影视娱乐"],
    "comic": ["漫画", "漫评", "黑白漫画"],
    "news": ["资讯", "新闻", "时政"],
    "live": ["直播回放", "直播录像"],
    "military": ["军事", "军武", "武器装备", "军事历史", "军迷"],   # 聚合分区(B站无独立军事区, 关键词聚合)
}
KW_SUFFIXES = ["教程", "推荐", "盘点", "实测", "对比", "合集", "评测", "解析", "入门", "安利"]
# 随机采样词库(分区专属): 随机只在你选的分区内随机
RANDOM_BY_GROUP = {
    "anime": ["动漫", "国漫", "番剧", "动画电影", "二次元", "手办模型", "动画短片", "新番"],
    "bangumi": ["新番", "番剧", "追番", "动画新番", "一月新番", "七月新番"],
    "guochuang": ["国漫", "国产动画", "国创", "国产漫画"],
    "guichu": ["鬼畜", "调教", "恶搞", "搞笑", "整活"],
    "dance": ["宅舞", "街舞", "舞蹈", "编舞", "舞蹈教程"],
    "life": ["日常vlog", "美食教程", "收纳整理", "手作", "木工", "露营", "骑行", "钓鱼"],
    "food": ["探店", "家常菜", "烘焙", "地方美食", "美食", "吃播"],
    "animal": ["萌宠", "猫咪", "狗狗", "动物", "宠物", "仓鼠"],
    "vlog": ["vlog", "日常vlog", "旅行vlog", "生活记录"],
    "film": ["电影解说", "剧集推荐", "纪录片", "影视混剪", "电影"],
    "movie": ["电影", "预告片", "影评", "电影幕后"],
    "series": ["电视剧", "追剧", "剧集", "网剧"],
    "documentary": ["纪录片", "自然纪录片", "历史纪录片", "人文纪录片"],
    "music": ["翻唱", "音乐现场", "民谣", "live", "器乐", "原创音乐", "电音"],
    "game": ["游戏实况", "单机游戏", "独立游戏", "游戏攻略", "主机游戏", "手游", "电竞", "游戏评测"],
    "esports": ["电竞", "赛事", "LPL", "KPL", "比赛集锦", "战队"],
    "knowledge": ["科普", "历史", "地理", "天文", "心理", "哲学", "经济", "物理", "化学"],
    "tech": ["科技", "人工智能", "黑科技", "前沿", "机器人", "芯片"],
    "digital": ["数码评测", "手机", "装机", "外设", "摄影摄像", "相机"],
    "sports": ["健身", "体育", "球类", "跑步", "武术", "滑板"],
    "events": ["赛事", "联赛", "比赛", "锦标赛"],
    "beauty": ["美妆", "护肤", "化妆", "口红"],
    "fashion": ["穿搭", "时尚", "潮流", "箱包"],
    "auto": ["汽车", "新能源", "摩托", "自驾", "赛车"],
    "ent": ["综艺", "娱乐", "明星", "娱乐圈", "选秀"],
    "comic": ["漫画", "漫评", "黑白漫画", "绘本"],
    "news": ["资讯", "新闻", "时政", "科技新闻"],
    "live": ["直播", "直播回放", "直播录像", "云直播"],
    "military": ["军事", "军武", "武器", "战争", "军迷", "战术", "国防"],
}
# 相关链白名单: tname → 分组id; 只开部分分区时, 未收录的一律丢弃(严格制)
TNAME_GROUP = {
    "MAD·AMV": "anime", "短片·手书": "anime", "综合": "anime", "动画短片": "anime",
    "同人·手书": "anime", "特摄": "anime", "短片": "anime", "绘画": "anime",
    "音乐现场": "music", "MV": "music", "演奏": "music", "翻唱": "music", "音乐教学": "music",
    "音乐综合": "music", "音乐V": "music", "电音": "music", "说唱": "music",
    "原创音乐": "music", "乐评盘点": "music", "VOCALOID·UTAU": "music",
    "宅舞": "dance", "街舞": "dance", "舞蹈教程": "dance", "舞蹈综合": "dance",
    "颜值·网红舞": "dance",
    "单机游戏": "game", "手机游戏": "game", "网络游戏": "game", "电子竞技": "game",
    "游戏实况": "game", "游戏日常": "game", "GMV": "game", "主机游戏": "game",
    "人文历史": "knowledge", "社科·法律·心理": "knowledge", "财经·商学": "knowledge",
    "校园学习": "knowledge", "职业职场": "knowledge", "科学科普": "knowledge",
    "野生技能协会": "knowledge", "财经商业": "knowledge",
    "数码综合": "digital", "手机平板": "digital", "摄影摄像": "digital",
    "影音智能": "digital", "电脑装机": "digital", "电脑硬件": "digital", "数码": "digital",
    "美妆护肤": "beauty", "仿妆cos": "beauty",
    "穿搭": "fashion", "时尚潮流": "fashion",
    "汽车": "auto", "汽车生活": "auto", "摩托车": "auto",
    "日常": "life", "搞笑": "life", "美食制作": "life", "出行": "life",
    "美食测评": "life", "美食记录": "life", "美食侦探": "life", "田园美食": "life",
    "三农": "life", "汪星人": "life", "亲子": "life", "小剧场": "life",
    "电影": "film", "影视杂谈": "film", "影视剪辑": "film", "短剧": "film",
    "番剧": "bangumi", "连载动画": "bangumi", "资讯": "bangumi", "完结动画": "bangumi",
    "国创": "guochuang", "国产动画": "guochuang", "国产原创相关": "guochuang",
    "科技": "tech", "科学科普": "knowledge", "知识": "tech", "社科·法律·心理": "knowledge",
    "人工智能": "tech", "黑科技": "tech", "健康": "life", "社会": "life",
    "纯爱": "bangumi", "搞笑": "life", "鬼畜": "guichu", "鬼畜剧场": "guichu",
    "美食检测": "food", "美食制作": "food", "美食测评": "food", "美食侦探": "food",
    "田园美食": "food", "美食品鉴": "food", "火锅": "food", "烹饪": "food",
    "宠物": "animal", "萌宠": "animal", "动物": "animal", "喵星人": "animal",
    "汪星人": "animal", "小宠": "animal", "野生动物": "animal",
    "日常": "life", "生活": "life", "户外": "life", "Vlog": "vlog",
    "综艺": "ent", "娱乐": "ent", "娱乐圈": "ent", "明星": "ent",
    "体育": "sports", "健身": "sports", "篮球": "sports", "足球": "sports",
    "运动": "sports", "户外运动": "sports", "竞技": "sports",
    "赛事": "events", "赛事综合": "events", "体育赛事": "events", "直播": "live",
    "电竞": "esports", "竞技游戏": "esports", "英雄联盟": "esports", "王者荣耀": "esports",
    "漫画": "comic", "漫画综合": "comic", "黑白漫画": "comic", "绘本": "comic",
    "新闻": "news", "时政": "news", "社会新闻": "news",
    "纪录片": "documentary", "人文·历史·自然": "documentary",
    "电视剧": "series", "国产剧": "series", "欧美剧": "series", "日剧": "series",
    "泰剧": "series", "电视剧综合": "series",
}
# 搜索条目的一级分区名 → 分组id(严格制, B站一级分区全集)
AREA_GROUP = {"动画": "anime", "番剧": "bangumi", "国创": "guochuang",
              "音乐": "music", "舞蹈": "dance", "游戏": "game", "电竞": "esports",
              "知识": "knowledge", "科技": "tech", "数码": "digital",
              "生活": "life", "美食": "food", "动物圈": "animal", "Vlog": "vlog",
              "影视": "film", "电影": "movie", "电视剧": "series", "纪录片": "documentary",
              "运动": "sports", "赛事": "events", "汽车": "auto",
              "时尚": "fashion", "美妆": "beauty", "娱乐": "ent", "漫画": "comic",
              "资讯": "news", "直播": "live", "鬼畜": "guichu"}
_recent_rand = []
_kw_cursor = 0
_kw_page = 0
_fetch_count = 0
_pref_cursor = 0   # 偏好词轮换游标(严肃模式变体池仍按此轮换)
_pref_page = 0     # 偏好词翻页计数(保留兼容, 实际取页已改为随机)
_pref_bag = []     # 偏好词洗牌袋: 一轮内全覆盖 + 顺序随机
_pref_page_hist = []   # 最近用过的页码(防连续命中同一页)
_PREF_PAGE_MAX = 4 # 偏好词翻页深度(4页×30条/词, 覆盖小众主题的近期供给)
PREF_LOOSE_BONUS_MONTHS = 6   # 宽松档: 偏好备注内容额外宽限月数(其余档位不加)


def pref_keywords(enabled):
    """偏好备注 → 搜索词(逗号分词, 2-12字); 每轮取2个, 洗牌袋保证覆盖全部偏好但顺序随机。"""
    return _pref_pick(pref_all_kws(), 2)


def pref_all_kws():
    """偏好备注全部关键词(不轮换), 供严肃模式等场景使用。"""
    raw = sget("prefs_note") or ""
    out = []
    for part in re.split(r"[，,、;；\n。！？!?]+", raw):
        p = part.strip()
        if 2 <= len(p) <= 12:
            out.append(p)
    return out


def pref_age_months():
    """偏好备注内容的发布时间窗(月, 0=不限) —— 偏好同样受时间窗约束, 不再整段豁免。
    严格/标准: 与设定窗完全一致(设定3个月就绝不出现3个月前的内容);
    宽松: 额外宽限 PREF_LOOSE_BONUS_MONTHS 个月(偏好主题常是小众长尾, 多给半年保供给);
    严肃: 走严肃独立时间窗(知识类内容历史长), 同样不再豁免。
    设定为 0(不限)时偏好也随之为不限。"""
    try:
        if sget("mode") == "严肃":
            base = int(sget("ser_max_age_months") or 0)
        else:
            base = int(sget("m_max_age_months") or 0)
    except Exception:
        base = 0
    if not base:
        return 0
    if sget("mode") == "宽松":
        return base + PREF_LOOSE_BONUS_MONTHS
    return base   # 严格/标准/其它档位: 一分不多给


def pref_age_cut():
    """偏好时间窗的绝对时间戳(0=不限), 供抓取侧预过滤使用。"""
    m = pref_age_months()
    return int(time.time() - m * 30.44 * 86400) if m else 0


def _pref_pick(pool, n):
    """洗牌袋取词: 一轮内每个偏好词都被轮到, 但顺序随机。
    旧版按固定游标取相邻两词, 顺序完全可预期 → 用户感知"每次刷出来都一样"。"""
    global _pref_bag
    pool = list(dict.fromkeys([w for w in pool if w]))
    if not pool or n <= 0:
        _pref_bag = []
        return []
    bag = [w for w in _pref_bag if w in pool]
    if len(bag) < n:                     # 袋见底 → 补入新一轮洗好的全量词(只补袋里没有的)
        rest = [w for w in pool if w not in bag]
        random.shuffle(rest)
        bag += rest
    picked = bag[:n]
    _pref_bag = bag[n:]
    return picked


def _pref_pick_page():
    """随机页码(避开最近用过的页): 旧版 1→2→3→4 顺序循环, 4轮后完全复现同一批视频。"""
    global _pref_page_hist
    cands = [p for p in range(1, _PREF_PAGE_MAX + 1) if p not in _pref_page_hist]
    if not cands:
        _pref_page_hist = []
        cands = list(range(1, _PREF_PAGE_MAX + 1))
    pg = random.choice(cands)
    _pref_page_hist = (_pref_page_hist + [pg])[-2:]
    return pg


def _pref_search_terms(enabled, quick):
    """本轮要搜的(偏好词, 页码): 完整构建取2词/快速开屏取1词。
    词走洗牌袋、页码随机 —— 两者都随机才不会固定周期复现同一批视频
    (旧版是双顺序游标, 每 N×4 轮就完整重放一轮)。"""
    prefs = _pref_pick(pref_all_kws(), 1 if quick else 2)
    return [(kw, _pref_pick_page()) for kw in prefs]


def serious_kw_pool(enabled):
    """严肃模式定向词池: 用户偏好优先, 每个主题生成教程/课程/原理变体, 通用词只作补充。"""
    pref = []
    for raw in pref_all_kws():
        p = raw.strip()
        low = p.lower()
        if any(k in low for k in ("教程", "学习", "入门", "课程", "培训", "教学", "公开课", "开发")):
            variants = [p]
        else:
            variants = [p + " 教程", p + " 课程", p + " 原理", p + " 开发"]
        pref.extend(variants)
    rest = list(SERIOUS_EXTRA)
    idx = _pref_cursor % len(rest)
    rotated = rest[idx:] + rest[:idx]
    return list(dict.fromkeys(pref + rotated))


def _subset_mode(enabled):
    return bool(enabled) and enabled != {g[0] for g in SOURCE_GROUPS}


# 军事聚合分区: B站无独立军事一级分区, 按关键词归属(开启军事时生效)
MILITARY_KWS = ("军事", "军武", "军迷", "武器装备", "枪械", "战争", "装甲", "战机",
                "舰艇", "弹药", "战术", "阅兵", "飞行员", "航母", "导弹", "征兵", "国防")


def item_domain_ok(it, enabled):
    """统一分区校验: 子集模式下按 tname/area 归类, 无法归类(或明确非本域)一律丢弃。
    非B站平台(贴吧/小黑盒等)豁免: 独立平台各自管理, 不随B站分区开关。"""
    if it.get("platform") != "B站":
        return True
    if not _subset_mode(enabled):
        return True
    if "military" in enabled:   # 军事聚合: 关键词命中(标题/tag/分区名) → 归军事
        text = ((it.get("title") or "") + (it.get("tname") or "") +
                (it.get("tag") or "") + (it.get("typename") or "")).lower()
        if any(k in text for k in MILITARY_KWS):
            return True
    t = it.get("tname") or ""
    a = it.get("area") or it.get("typename") or ""
    g = TNAME_GROUP.get(t) or AREA_GROUP.get(a)
    return g in enabled if g else False


def random_word_candidates(enabled):
    words = []
    for gid, ws in RANDOM_BY_GROUP.items():
        if gid in enabled:
            words += ws
    return words or [w for ws in RANDOM_BY_GROUP.values() for w in ws]


def ensure_kw_pool():
    """可生长的词池: 词带分区标记, 切换分区自动清非本域旧词; 少于20个时补足。"""
    global _fetch_count
    gids = enabled_source_ids()
    all_gids = {g[0] for g in SOURCE_GROUPS}
    cnt = db().execute("SELECT COUNT(*) FROM kw").fetchone()[0]
    added, tags = [], []
    t = int(time.time())
    if cnt and gids != all_gids:   # 只开了部分分区 → 清掉非本域旧词(AI词保留)
        with _db_lock:
            db().execute("DELETE FROM kw WHERE gid IS NULL OR (gid != 'ai' AND gid NOT IN (%s))"
                         % ",".join("?" * len(gids)), tuple(gids))
            db().commit()
        cnt = db().execute("SELECT COUNT(*) FROM kw").fetchone()[0]
    if cnt < 20:
        for gid, words in WORD_BANK.items():
            if gid in gids:
                for w in words:
                    for sfx in KW_SUFFIXES:
                        cand = w + sfx
                        if len(cand) <= 12:
                            added.append(cand)
                            tags.append(gid)
        for k in list((account_slim().get("keywords") or []))[:6]:
            added.append(k)
            tags.append("ai")
        if ai_on():
            try:   # LLM 补充新搜索词(每次1次调用)
                content, _ = call_llm([
                    {"role": "system", "content":
                        "为B站内容推荐生成12个搜索词, 每个2-10字, 与已有词不同角度, 只输出JSON: {\"keywords\": [\"\",\"\"]}"},
                    {"role": "user", "content": "用户信息:\n" + (user_context() or "(无)")[:800]}],
                    max_tokens=300)
                for k in [x.strip() for x in json.loads(content).get("keywords", []) if x.strip()]:
                    added.append(k)
                    tags.append("ai")
            except Exception:
                pass
    elif ai_on() and _fetch_count % 12 == 0 and cnt < 120:
        try:   # 定期扩充词池, 防止词库用尽
            content, _ = call_llm([
                {"role": "system", "content":
                    "再为B站推荐生成10个全新的搜索词(避开常用词), 只输出JSON: {\"keywords\": [\"\",\"\"]}"},
                {"role": "user", "content": "用户信息:\n" + (user_context() or "(无)")[:800]}],
                max_tokens=300)
            for k in [x.strip() for x in json.loads(content).get("keywords", []) if x.strip()]:
                added.append(k)
                tags.append("ai")
        except Exception:
            pass
    with _db_lock:
        for w, g in zip(added, tags):
            db().execute("INSERT OR IGNORE INTO kw(kw,last_used,new_cnt,ok_cnt,gid) VALUES(?,?,0,0,?)",
                         (w, t, g))
        db().commit()


def pick_kws(n=4):
    """LRU 选词: 最久没用过的优先。"""
    rows = db().execute("SELECT kw FROM kw ORDER BY last_used ASC LIMIT ?", (n,)).fetchall()
    return [r[0] for r in rows]


def search_pushdown():
    """按当前通用筛选规则推导搜索下推参数(供给优先原则):
    duration: 仅当某档整档通过时长窗口、且其余档整档无效(不丢任何有效供给)时才下推;
    order: 时间窗开启且未启用播放量窗口时才用 pubdate —— 新发布视频播放量低,
    若同时有播放量下限会把搜索来源整体过滤光(B站供给塌方的根因, 已修复)。
    返回 {} 表示无可下推条件(抓回后再筛)。"""
    try:
        min_dur = int(sget("m_min_duration") or 0)
        max_dur = int(sget("m_max_duration") or 0)
        min_views = int(sget("m_min_views") or 0)
        max_views = int(sget("m_max_views") or 0)
    except Exception:
        min_dur = max_dur = min_views = max_views = 0
    buckets = {1: (0, 600), 2: (600, 1800), 3: (1800, None)}

    def fully_valid(lo, hi):
        return (not min_dur or lo >= min_dur) and (not max_dur or (hi is not None and hi <= max_dur))

    def fully_invalid(lo, hi):
        return (min_dur and hi is not None and hi <= min_dur) or (max_dur and lo >= max_dur)

    fully_valid_buckets = [b for b, (lo, hi) in buckets.items() if fully_valid(lo, hi)]
    partial_buckets = [b for b, (lo, hi) in buckets.items()
                       if not fully_valid(lo, hi) and not fully_invalid(lo, hi)]
    push = {}
    # 下推档位 X 的前提: 其余档位全部整档无效(部分有效的档位里藏着本可通过的供给, 不能丢)。
    if len(fully_valid_buckets) == 1 and not partial_buckets:
        push["duration"] = str(fully_valid_buckets[0])
    try:
        if int(sget("m_max_age_months") or 0) > 0 and min_views == 0 and max_views == 0:
            push["order"] = "pubdate"
    except Exception:
        pass
    return push


# B站直搜最近一次失败的原因(进程内诊断用): 搜索"没有B站视频"时先看这里
_search_fail = {"n": 0, "ts": 0.0, "err": ""}


def _note_search_fail(keyword, page, err):
    try:
        _search_fail["n"] += 1
        _search_fail["ts"] = time.time()
        _search_fail["err"] = "kw=%s page=%s %s" % (str(keyword)[:30], page, str(err)[:140])
    except Exception:
        pass


def search_items(keyword, page=1, limit=30, order=None, duration=None, pub_begin=None):
    """WBI签名搜索(实测免登录可用, 可翻页)。失败返回[]。
    接口返回非 0 码(风控 -412 / WBI 签名失效)时不再当成"这词没结果": 先重试一次,
    两次都失败才算空 —— 否则上层看到空页就收工, 搜索结果里会一条B站视频都没有。
    order/duration: 筛选条件下推(见 search_pushdown), 不传则行为与旧版一致。
    pub_begin: 只收该时间戳之后发布的内容(秒级), 服务端下推偏好时间窗 —— 后端时间窗是硬约束,
    能下推就下推, 抓回来的才多是窗内新内容; 若下推后整页为空(小众词无近期供给)则回退免窗重取,
    最终仍由 pref_age_cut 在本地兜底过滤。"""

    def _once(extra):
        # v0.3.1.66(用户要求: 降低风控概率): 搜索是 fan-out 最猛的一条(偏好词/快速开屏 8 路并发),
        # 过闸门把同一瞬间的点击摊开; 太密就放弃这一次(上层按空页处理, 下一轮还会来查)。
        if not _net_guard("bili", 0.35, 110, wait=1.5):
            raise RuntimeError("本地限速(未发出)")
        params = {"search_type": "video", "keyword": keyword,
                  "page": str(page), "page_size": str(limit)}
        if order:
            params["order"] = order
        if duration:
            params["duration"] = duration
        if extra:
            params.update(extra)
        qs = wbi_enc(params)
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/wbi/search/type?" + qs,
            headers=dict(HEADERS, Cookie=account_cookie()) if account_cookie() else HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
        if d.get("code") != 0:
            try:
                _code = int(d.get("code"))
            except Exception:
                _code = 0
            # -352/-412/-509 都是"站方觉得你不对劲"(风控/签名校验/超频), 412 是 412 状态码。
            # 撞上就挂惩罚期: 搜索间隔翻倍、额度减半, 别在风控边上反复试探。
            if _code in (-352, -412, -403, -509, 412):
                _net_penalty("bili", 900, 2.0)
            raise RuntimeError("code=%s %s" % (d.get("code"), str(d.get("message") or "")[:40]))
        res = (d.get("data") or {}).get("result") or []
        return [normalize_search(it) for it in res[:limit] if it.get("bvid")]

    last = None
    for attempt in (1, 2):
        try:
            if pub_begin:
                got = _once({"pubtime_begin_s": str(int(pub_begin))})
                if got:
                    return got
                return _once(None)   # 窗内无供给 → 回退免窗重取(本地仍会按窗过滤)
            return _once(None)
        except Exception as e:
            last = e
            if attempt == 1:
                # v0.3.1.66: 原来 0.6 秒就重打, 正好撞在风控窗口上; 改成 1.2~3.0 秒随机,
                # 像人手动重试, 也给正在被限的接口一点恢复时间。
                time.sleep(1.2 + random.uniform(0, 1.8))
    _note_search_fail(keyword, page, last)
    return []
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}

_feed_cache = {"ts": 0, "payload": None}
_FX = ThreadPoolExecutor(max_workers=8)   # 常规并行抓取池
_SERIOUS_X = ThreadPoolExecutor(max_workers=16)  # 严肃模式多平台 fan-out 抓取池
_SCORE_X = ThreadPoolExecutor(max_workers=3)   # 评分池(独立, 3并发防DeepSeek限流)
_TAG_X = ThreadPoolExecutor(max_workers=3)     # 标签补全池(独立, 3并发防B站风控)


def _safe_json(url, timeout=10):
    """带错误收集的GET JSON。返回 (data, error)。
    v0.3.1.66: 榜单页是一次性并发好几页, 也过同一道闸门 —— 太密就放弃这一页(下一轮再抓),
    不要为了"这一屏多几条"把出口 IP 送上风控名单。"""
    if not _net_guard("bili", 0.4, 90, wait=1.0):
        return None, "本地限速(未发出)"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except Exception as e:
        return None, str(e)[:80]


def normalize(it):
    stat = it.get("stat") or {}
    owner = it.get("owner") or {}
    pic = it.get("pic") or ""
    if pic.startswith("//"):
        pic = "https:" + pic
    else:
        pic = pic.replace("http://", "https://")
    tag = it.get("tag")
    if isinstance(tag, (list, tuple)):
        tag = ",".join(str(x) for x in tag)
    elif isinstance(tag, str):
        tag = re.sub(r"<[^>]+>", "", html.unescape(tag.strip()))
    else:
        tag = ""
    return {
        "key": "https://www.bilibili.com/video/%s" % it.get("bvid", ""),
        "platform": "B站",
        "title": it.get("title", ""),
        "author": owner.get("name", ""),
        "author_mid": owner.get("mid") or it.get("mid") or 0,
        "desc": (it.get("desc") or "")[:500],
        "cover": pic,
        "views": stat.get("view", 0), "views_known": stat.get("view") is not None,
        "likes": stat.get("like", 0),
        "danmaku": stat.get("danmaku", 0),
        "reply": stat.get("reply", 0),
        "duration": it.get("duration", 0), "duration_known": it.get("duration") is not None,
        "pubdate": it.get("pubdate", 0),
        "url": "https://www.bilibili.com/video/%s" % it.get("bvid", ""),
        "tname": it.get("tname", ""),
        "area": it.get("parent_area_name") or it.get("typename") or "",
        "tag": tag[:200],
    }


# ---------------------------------------------------------------- 贴吧数据源
#
# 无官方API: 走"BAIDUID 会话"通路(实测) —— 先访问 forumpage 种下 BAIDUID cookie,
# 再用同一会话访问经典版 /f?kw= 列表页(无 cookie 时 403)。登录(BDUSS)后更稳。
# 解析目标: li.j_thread_list 块 → tid/标题/楼主/摘要/回复数/最多2张缩略图/时间。

TB_RE = re.compile(r'<li[^>]*class="[^"]*j_thread_list[^"]*"[^>]*>')

# 贴吧原始数据调试记录(用于定位"求助"分类字段): 保留最近若干条, 用户触发采集后由运维读取
_tb_debug_buf = []
_tb_debug_lock = threading.Lock()


def _tb_dump(rec):
    try:
        with _tb_debug_lock:
            _tb_debug_buf.append(rec)
            if len(_tb_debug_buf) > 150:
                del _tb_debug_buf[:len(_tb_debug_buf) - 150]
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tb_debug.jsonl"),
                      "w", encoding="utf-8") as f:
                for r in _tb_debug_buf:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass

_tb_sess_jars = []          # [cookiejar] 匿名会话池(轮换, 防单会话被风控)
_tb_sess_idx = 0
_tb_sess_lock = threading.Lock()
_tb_last_err = ""
_tb_last_fetch = 0          # 最近一次贴吧抓取时间戳(状态面板用)
_tb_fetch_total = 0         # 本次会话贴吧累计条目(状态面板用)

_CDP_PORT = 0               # 浏览器调试端口(0=未启动)
_CDP_PROC = None            # 浏览器子进程
_CDP_LOGIN = {"ts": 0, "status": "idle", "message": ""}
# 百度安全验证通过后的"浏览器抓取"通道(v0.3.1.57):
# 用户拖完滑块后, 同一窗口里的会话才是被百度认的; 用 urllib 带着同样的 Cookie 再去打,
# 服务端照样 403(实测: 浏览器里页面正常、urllib 同 URL 403) —— 所以贴吧请求改走这个窗口,
# 用 CDP 导航 + 读 DOM 拿 HTML, 不再自己拼请求。
_TB_BR = {"ws": "", "ok": False, "ts": 0}
_TB_BR_LOCK = threading.Lock()
_CDP_HOST_KEYS = ("BDUSS", "STOKEN", "TIEBA_UID", "BAIDUID", "BAIDUID_BFESS",
                  "TIEBA_UID_BFESS", "H_WISE_SIDS", "H_PS_PSSID", "Hm_lvt")
# 浏览器通道要跨进程记住(v0.3.1.59): 应用一重启, _CDP_PROC / _CDP_PORT / _TB_BR 全没了,
# 而用户桌面上那个已经拖过滑块的窗口还在 —— 旧实现从此再也接不上它, 贴吧请求全部回落
# urllib, 于是"验证完了还是抓不到"。落盘调试端口就够了, 用的时候按需把窗口接回来。
_TB_BR_FILE = os.path.join(DATA_DIR, "tb_browser.json")
_TB_BR_IDLE_CLOSE = 1800    # 通道闲置多久才自动关窗(秒)


def _tb_br_save():
    """把浏览器通道的调试端口落盘, 供应用重启后接回同一个窗口。"""
    try:
        with open(_TB_BR_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": int(_CDP_PORT or 0), "ws": _TB_BR.get("ws") or "",
                       "ok": bool(_TB_BR.get("ok")), "ts": int(_TB_BR.get("ts") or 0)}, f)
    except Exception:
        pass


def _tb_br_forget():
    """窗口没了 / 通道失效: 清掉内存与落盘状态, 免得下次去连一个死端口。"""
    _TB_BR.update(ws="", ok=False)
    try:
        os.remove(_TB_BR_FILE)
    except Exception:
        pass


def _port_alive(port):
    """这个调试端口后面还有浏览器吗。"""
    if not port:
        return False
    try:
        with local_open("http://127.0.0.1:%d/json/version" % int(port), timeout=2) as resp:
            return bool(resp.read())
    except Exception:
        return False


def _read_devtools_port(path):
    try:
        with open(path) as f:
            return int((f.readline() or "").strip() or 0)
    except Exception:
        return 0


def _cdp_alive():
    """浏览器窗口还活着吗 —— 问调试端口, 不看 _CDP_PROC(应用重启后它就没有了)。
    旧代码只看 Popen 对象, 于是重启后的第一次轮询就报 closed, 前端立刻收手。"""
    return _port_alive(_CDP_PORT)


def _cdp_close_remote():
    """窗口不是本进程开的(应用重启后接回来的), Popen 杀不到 —— 走 CDP 把页面关掉,
    Chrome 关掉最后一个页面就会自己退出。"""
    if not _CDP_PORT:
        return False
    try:
        with local_open("http://127.0.0.1:%d/json/list" % int(_CDP_PORT), timeout=3) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    closed = False
    for t in targets or []:
        if t.get("type") != "page" or not t.get("id"):
            continue
        try:
            with local_open("http://127.0.0.1:%d/json/close/%s" % (int(_CDP_PORT), t["id"]),
                            timeout=3) as resp:
                resp.read()
            closed = True
        except Exception:
            pass
    return closed


_TB_BR_RELAUNCH = {"ts": 0}   # 自动重开验证窗口的退避(10 分钟内只试一次)


def _tb_br_restore(relaunch=True):
    """把上一次的浏览器窗口接回来(应用重启 / 通道被清之后)。返回通道是否已可用。

    v0.3.1.65: 用户验证完之后随手把那个窗口关了 —— 之后每一次抓取都回落到 urllib,
    于是又是 403、又是"验证完还是抓不到"。现在如果落盘状态是"验证通过"、但端口连不上,
    就用**同一个 profile**(会话/cookie 都在里面)自动把窗口重新拉起来并接回通道;
    10 分钟退避一次, 免得用户真不想要窗口时被反复弹窗。"""
    global _CDP_PORT
    if _TB_BR.get("ws") and _TB_BR.get("ok"):
        return True
    info = {}
    try:
        with open(_TB_BR_FILE, encoding="utf-8") as f:
            info = json.load(f) or {}
    except Exception:
        info = {}
    port = int(_CDP_PORT or 0) or int(info.get("port") or 0)
    if port:
        _CDP_PORT = port
        if _cdp_alive():
            ws = _cdp_page_ws()
            if ws:
                _TB_BR.update(ws=ws, ok=bool(info.get("ok")), ts=int(info.get("ts") or 0))
                return bool(_TB_BR.get("ok"))
            return bool(_TB_BR.get("ok"))
    if not relaunch or not info.get("ok"):
        if port:
            _tb_br_forget()     # 窗口真的没了: 状态作废, 下次点"验证"重新开
        return False
    now = time.time()
    if now - int(_TB_BR_RELAUNCH.get("ts") or 0) < 600:
        return False
    _TB_BR_RELAUNCH["ts"] = now
    _proc, port2, err = _cdp_launch(_tb_verify_url())
    if err or not port2:
        return False
    _CDP_PORT = port2
    ws = _cdp_page_ws()
    if not ws:
        return False
    _TB_BR.update(ws=ws, ok=True, ts=int(now))
    _tb_br_save()
    _tb_log("验证窗口被关掉了 —— 已用同一个浏览器配置自动重新拉起(会话还在窗口里)")
    return True


def _tb_br_channel():
    """贴吧抓取可用的浏览器通道 ws; 没有(未验证 / 窗口已关)返回 ""。"""
    if _TB_BR.get("ok") and _TB_BR.get("ws") and _cdp_alive():
        return _TB_BR["ws"]
    _tb_br_restore()
    return (_TB_BR.get("ws") or "") if _TB_BR.get("ok") else ""


def _tb_br_heal(old_ws=""):
    """当前 ws 指向的标签页失效(被关 / 崩溃)时换一个还活着的页; 一个都没有就作废通道。

    不修这个会出现"浏览器还在、贴吧永远抓不到": 关掉那个标签页后 ws 成了死地址,
    每次 eval 都失败, 但 _tb_br_channel() 又从落盘文件里把同一个死 ws 读回来 ——
    用户重开窗口都没用, 只能重启应用。"""
    ws = _cdp_page_ws()
    if ws and ws != old_ws:
        _TB_BR["ws"] = ws
        if _TB_BR.get("ok"):
            _tb_br_save()
        return ws
    if not ws:
        _tb_br_forget()
        _TB_BR["ws"] = ""
        _TB_BR["ok"] = False
    return ""


# ---------------- 出站请求闸门 (v0.3.1.55) ----------------
# 为什么要有: 用户实测一分钟内就被百度安全验证拦了。查下来是我们自己打得太密 ——
# 顶栏未读角标每 60 秒刷一次 /api/messages, 那一次要打 4 个贴吧请求(登录态 1 + 未读页 2
# + 又查一次登录态 1), 光挂在那儿不动就是 240 次/小时; 再叠上每次补池 3 次、改设置立刻
# force_rebuild 又 3 次 —— 这不是人在刷站, 是定时器在刷站。
# 治理放在请求层: 同站点"单飞 + 最小间隔 + 每分钟上限", 超限就这一次不发(调用方按空结果
# 处理, 下次自然再试), 绝不排队堆积。真正的解析/筛选逻辑一行没动。
_NET_LOCK = threading.Lock()
_NET = {"last": {}, "minute": {}, "min": 0, "total": {}, "penalty": {}, "pace": {}}
# 测试要多次连打同一站点(还会 mock urlopen), 闸门会让它们互相限流 —— 给个总开关;
# 生产默认开。关掉时 net_stats 仍照常计数, 便于断言"到底发了几个请求"。
_NET_GUARD_ON = os.environ.get("MIRRORSORTER_NET_GUARD", "1") != "0"


def _net_penalty(host, secs=600.0, factor=2.0):
    """被抓过(403/安全验证/-352)之后给这个站点挂一段冷却惩罚。
    v0.3.1.66(用户要求: 降低触发风控的概率): 旧版 403 之后只是干等 10 分钟, 然后照原节奏继续打,
    解封后常常立刻又被抓 —— 因为期间"平时那点请求量"本来就是站在风控线上的。
    惩罚期内: 最小间隔 ×factor、每分钟额度 ÷factor, 让重试一开始就比正常慢;
    已经挂着更长的惩罚就不缩短。"""
    if not _NET_GUARD_ON:
        return
    until = time.time() + max(30.0, float(secs))
    with _NET_LOCK:
        cur = _NET["penalty"].get(host)
        if cur and cur[0] >= until:
            return
        _NET["penalty"][host] = [until, max(1.1, float(factor))]


def _net_pace(host, gap=0.25):
    """打散同一瞬间的一堆请求: 距上次不到 gap 就睡够再发, **永不拒绝**。
    给"不能因为限流而失败"的调用用(登录态/未读数/收藏这类: 被拒绝会直接把功能表现成没登录)。
    v0.3.1.66: 8 个线程同时开火, 从站方看是一个 IP 一秒打十下 —— 这就是风控样本。"""
    if not _NET_GUARD_ON:
        return
    want = gap * random.uniform(1.0, 1.6)
    with _NET_LOCK:
        now = time.time()
        wait = (_NET["pace"].get(host) or 0.0) - now
        if wait <= 0:
            _NET["pace"][host] = now + want
            return
        _NET["pace"][host] = now + wait + want   # 排队: 后面的再等这么多
    time.sleep(min(2.5, wait))


def _net_guard(host, min_gap=1.0, per_min=8, wait=0.0):
    """返回 True 表示这次可以发; False 表示太密了, 直接放弃本次请求。
    wait>0 时允许先睡一小会儿(最多 wait 秒)再发 —— 补池走这条路, 免得把"太密"变成假报错。
    v0.3.1.66: ①间隔加向上抖动(每个请求的间距不一样, 但绝不比设定值更密);
    ②惩罚期内按 _net_penalty 的倍率放慢。"""
    now = time.time()
    if not _NET_GUARD_ON:
        with _NET_LOCK:
            _NET["total"][host] = _NET["total"].get(host, 0) + 1
        return True
    deadline = now + max(0.0, float(wait))
    while True:
        minute = int(now // 60)
        with _NET_LOCK:
            if _NET["min"] != minute:
                _NET["min"] = minute
                _NET["minute"] = {}
            factor = 1.0
            pen = _NET["penalty"].get(host)
            if pen:
                if pen[0] > now:
                    factor = float(pen[1] or 2.0)
                else:
                    _NET["penalty"].pop(host, None)      # 惩罚期过了, 回归正常节奏
            need = min_gap * factor * random.uniform(1.0, 1.45)
            quota = per_min if factor <= 1.0 else max(1, int(round(per_min / factor)))
            gap = now - (_NET["last"].get(host) or 0)
            if gap >= need:
                if _NET["minute"].get(host, 0) < quota:
                    _NET["last"][host] = now
                    _NET["minute"][host] = _NET["minute"].get(host, 0) + 1
                    _NET["total"][host] = _NET["total"].get(host, 0) + 1
                    return True
                return False      # 一分钟额度用完: 等下一个分钟窗口, 不排队
        if now >= deadline:
            return False
        time.sleep(min(0.25, max(0.05, need - gap)))
        now = time.time()


def net_stats():
    """最近一分钟各站点实际发出的请求数(诊断"是不是在刷站"用)。"""
    with _NET_LOCK:
        minute = int(time.time() // 60)
        fresh = minute == _NET["min"]
        now = time.time()
        return {"minute": {h: n for h, n in (_NET["minute"] if fresh else {}).items()},
                "total": dict(_NET["total"]),
                # 还在惩罚期的站点 → 剩余秒数(诊断用: 一看就知道"现在慢是为什么")
                "penalty": {h: int(v[0] - now) for h, v in _NET["penalty"].items() if v[0] > now}}


# 最近真正吐给信息流的 200 条按平台计数(v0.3.1.57): 用于核对"档位 5% 是否真按 5% 出"。
# 只记展示层的结果, 不改任何选材逻辑。
_mix_lock = threading.Lock()
_mix_recent = collections.deque(maxlen=200)


def mix_stats():
    """最近 200 条的真实平台配比: {"total": n, "by": {平台: 条数}, "share": {平台: 百分比}}。"""
    with _mix_lock:
        by = dict(collections.Counter(_mix_recent))
        n = sum(by.values())
    share = {k: round(100.0 * v / n, 1) for k, v in by.items()} if n else {}
    return {"total": n, "by": by, "share": share,
            "weights": platform_weights()}

_tb_avatar_cache = {"ts": 0, "data": None, "ctype": "image/jpeg"}
_xhh_avatar_cache = {"ts": 0, "data": None, "ctype": "image/jpeg"}
# v0.3.1.55: 登录态/未读的短缓存 —— 界面轮询不该每次都去百度翻页(见 tb_login_check)。
_tb_login_cache = {"ts": 0, "data": None}
_tb_unread = {"ts": 0, "data": None}


def tb_cookie():
    return sget("tb_cookie") or ""


def tb_verify_cookie():
    """百度安全验证通过后写回的 cookie 串(v0.3.1.55)。
    只用来过 WAF: 同一个出口 IP + 同一套 BAIDUID, 抓取就能继续。"""
    return sget("tb_verify_cookie") or ""


def _tb_cookie_header():
    """拼请求用的 Cookie 头: 登录 cookie + 验证 cookie, 同名以后者为准。
    以前只发 tb_cookie, 所以验证窗口拿到的会话根本没被带上(改前就是这个坑)。"""
    merged = {}
    for part in (tb_cookie(), tb_verify_cookie()):
        for chunk in part.split(";"):
            k, sep, v = chunk.strip().partition("=")
            if sep and k:
                merged[k] = v
    return "; ".join("%s=%s" % (k, v) for k, v in merged.items())


def tb_users():
    """吧名列表: 每行/逗号分隔, 去重, 最多50个。"""
    raw = sget("tb_bars") or ""
    out = []
    for part in re.split(r"[\n,，、;；]+", raw):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out[:50]


def tb_set_bars(bars):
    sset("tb_bars", "\n".join(b.strip() for b in bars if b.strip()))


def user_configured():
    """本机是不是已经配好了至少一条内容来源。

    "配好"只认三件事：填过吧列表 / 登录过任一平台 / 真抓过内容。
    只看 prefs_note 不算 —— 偏好写了但一条来源都没有，信息流照样是空的。
    """
    try:
        if tb_users():
            return True
        if sget("bili_cookie") or sget("xhh_heybox_id"):
            return True
        if int(sget("fetch_last") or 0) > 0:
            return True
    except Exception:
        # 判定失败当成"已配置"：宁可少弹一次引导，也别在启动路径上抛异常
        return True
    return False


def intro_needed():
    """要不要弹首次引导：没做过引导，且本机什么都还没配。"""
    if sget("intro_done") == "1":
        return False
    return not user_configured()


def tieba_bar_allowed(it):
    """贴吧候选只允许来自用户配置的吧; 未配置吧列表时不限制。"""
    bars = [b.lower() for b in tb_users()]
    if not bars:
        return True
    text = " ".join(str(it.get(k) or "") for k in ("tname", "tag", "src")).lower()
    return any(b in text for b in bars)


def tb_weight():
    try:
        w = int(sget("tb_weight") or 25)
    except Exception:
        w = 25
    return w if 0 <= w <= 100 else 25


def bili_weight():
    try:
        w = int(sget("bili_weight") or 90)
    except Exception:
        w = 90
    return w if 0 <= w <= 100 else 90


def xhh_weight():
    """小黑盒当前权重; 0=后台完全不出不抓(显式搜索不受影响)。"""
    try:
        w = int(sget("xhh_weight") or 15)
    except Exception:
        w = 15
    return w if 0 <= w <= 100 else 15


def _tb_session_jar():
    """获取/创建匿名会话 jar(池轮换)。失效时重建。"""
    global _tb_sess_idx
    with _tb_sess_lock:
        if not _tb_sess_jars:
            _tb_sess_jars.append(http.cookiejar.CookieJar())
        jar = _tb_sess_jars[_tb_sess_idx % len(_tb_sess_jars)]
        _tb_sess_idx += 1
        return jar


def _tb_seed(jar):
    """匿名会话: 访问 forumpage 种下 BAIDUID(实测 /f?kw= 无cookie必403)。
    v0.3.1.59: jar 里已经有 BAIDUID 就直接返回 —— 之前每次抓取都白多打一个
    forumpage, 请求量翻倍, 也是喂大风控的帮手。"""
    try:
        if any(c.name == "BAIDUID" for c in jar):
            return True
    except Exception:
        pass
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        req = urllib.request.Request(
            "https://tieba.baidu.com/f/index/forumpage?kw=steam",
            headers=dict(TB_HEADERS, Referer="https://tieba.baidu.com/"))
        with opener.open(req, timeout=10) as resp:
            resp.read()
        return any(c.name == "BAIDUID" for c in jar)
    except Exception:
        return False


def _tb_err_text(e, page=""):
    """403 有两种: 登录态过期(带 cookie 时) 和 百度安全验证 / WAF 拦 IP。
    后者换个说法, 否则用户会一直以为是没登录/没配 cookie。"""
    if not page:
        try:
            page = e.read().decode("utf-8", "ignore")[:4000]
        except Exception:
            page = ""
    try:
        if "安全验证" in page or "wappass" in page or "unusualTraffic" in page:
            return "百度安全验证(403)"
    except Exception:
        pass
    return "HTTP %d" % e.code


def _tb_log(msg):
    """贴吧抓取流水账（DATA_DIR\\tieba.log）。

    用户问过"日志里是怎么显示的" —— 之前贴吧这一路什么都不留，出问题只能靠猜。
    现在每次请求写一行（走浏览器通道还是 urllib、状态码/字节数/错误），
    只留最近 ~300KB：这是排障日志，不是审计系统。
    """
    try:
        p = os.path.join(DATA_DIR, "tieba.log")
        if os.path.exists(p) and os.path.getsize(p) > 300000:
            try:
                os.remove(p)
            except Exception:
                pass
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


_TB_VIA = {"last": ""}   # 这一次到底走了哪条通路(br=验证窗口, url=直连), 只用于日志


def _tb_fetch(url, referer="https://tieba.baidu.com/", timeout=12):
    html, err = _tb_fetch_raw(url, referer, timeout)
    _tb_log("GET %s -> %s" % (url[:140],
                              ("OK %d 字节%s" % (len(html), "（浏览器通道）" if _TB_VIA.get("last") == "br" else ""))
                              if html else ("失败: %s" % err)))
    return html, err


def _tb_risk(err):
    """贴吧出现风控证据(403/安全验证/429)时挂一段惩罚期(v0.3.1.66, 降低风控概率)。
    被拦之后马上照原节奏继续打, 常常还没出验证就又踩进去; 惩罚期把后续请求的间隔翻倍、
    每分钟额度减半, 给出口 IP 一点"像人"的喘息, 解封后也不至于立刻又被抓。"""
    s = str(err or "")
    if ("403" in s) or ("安全验证" in s) or ("429" in s):
        _net_penalty("tieba", 1200, 2.0)


def _tb_fetch_raw(url, referer="https://tieba.baidu.com/", timeout=12):
    """贴吧 GET: 登录cookie(含人工验证拿到的会话)优先; 匿名走"种BAIDUID→请求"通路,
    403/302/空壳自动重试1次。
    登录cookie失效时(302→登录页/403)自动降级匿名重试, 不把"登录过期"当采集故障。
    403 时快速失败(不再无谓重试) —— 限流会由 fetch_tieba_pool 的冷却机制兜底。
    v0.3.1.55: 进门先过出站闸门(同站点最小间隔 + 每分钟上限), 免得补池/刷新/验证
    三条路各自打一份, 叠起来看着像刷站。"""
    if not _net_guard("tieba", 1.8, 8, wait=2.0):
        return None, "本地限流(未发出)"
    # 过了滑块的会话只在浏览器里有效(urllib 带同样 Cookie 仍 403) → 有那扇窗就先走它。
    # v0.3.1.59: _tb_br_channel() 会在应用重启后按落盘端口把窗口接回来;
    # 浏览器自己都被拦(403)时直接失败 —— 再用 urllib 补打一遍只会让这个出口 IP
    # 更难从验证里出来, 也是"每 60 秒一条 403"的来源。
    if _tb_br_channel():
        html, berr = _tb_browser_html(url, timeout=15)
        if html:
            _TB_VIA["last"] = "br"
            return html, None
        # v0.3.1.65: 通道还在就**不再用 urllib 补打**。补打必 403, 既把 tb_error 写成
        # "百度安全验证"(前端于是显示"需重新验证", 可窗口明明是好的), 又继续给出口 IP 加压。
        # 这一页没拿到就是这一页的事: 交给调用方按失败处理, 下一轮还走同一个窗口。
        if _cdp_alive():
            return None, berr or "浏览器通道读取失败"
        _tb_br_forget()          # 窗口真没了: 通道作废, 回落 urllib
    _TB_VIA["last"] = "url"
    cookie = _tb_cookie_header()
    for attempt in range(2):   # 0=带cookie 1=匿名重试(限流时不重试)
        if attempt == 0 and cookie:
            try:
                req = urllib.request.Request(url, headers=dict(
                    TB_HEADERS, Referer=referer, Cookie=cookie))
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8", "ignore")
                if "安全验证" in body[:4000]:
                    _tb_risk("百度安全验证(403)")
                    return None, "百度安全验证(403)"   # 200 的拦截页也不能当数据吞掉
                if "j_thread_list" in body or "data-field" in body:
                    return body, None
            except urllib.error.HTTPError as e:
                if e.code in (302, 403, 401):
                    if e.code == 403:
                        _tb_risk("HTTP 403")
                    continue   # 登录态失效 → 降级匿名
                return None, "HTTP %d" % e.code
            except Exception as e:
                return None, str(e)[:80]
        # 匿名: 种 BAIDUID → 请求
        jar = _tb_session_jar()
        _tb_seed(jar)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            req = urllib.request.Request(url, headers=dict(TB_HEADERS, Referer=referer))
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "ignore")
            if "安全验证" in body[:4000]:
                return None, "百度安全验证(403)"   # 200 也可能是拦截页, 别当空壳页吞掉
            if "j_thread_list" in body or "data-field" in body:
                return body, None
        except urllib.error.HTTPError as e:
            try:
                page = e.read().decode("utf-8", "ignore")[:4000]
            except Exception:
                page = ""
            _e = _tb_err_text(e, page)
            _tb_risk(_e)
            return None, _e   # 匿名403=限流/安全验证, 快速失败(不重试)
        except Exception as e:
            return None, str(e)[:80]
        return body, None   # 空壳页: 这次就这样(下次轮换新会话)
    return None, "请求失败"


def _tb_html_to_items(body, fname, max_items=50):
    """解析列表页 → 贴吧条目(统一 item 格式)。
    置顶/广告判定: 只看标题锚点前方局部区(广告div在</li>后、归入下一块, 不能整块判)。"""
    items = []
    blocks = TB_RE.split(body)
    for blk in blocks[1:]:
        if len(items) >= max_items:
            break
        m = re.search(r'href="(/p/(\d{8,}))"[^>]*title="([^"]*)"', blk)
        if not m:
            continue   # 广告块无帖子链接, 天然跳过
        pre = blk[max(0, m.start() - 500):m.start()]
        if "icon-top" in pre or "icon_thread_hidden" in pre:
            continue   # 置顶/隐藏帖
        tid, title = m.group(2), html.unescape(m.group(3)).strip()
        if not title:
            continue
        # 贴吧求助分类标签(span.pb-title-prefix, 如"求助"): F12确认的class, 命中即求助帖
        helpm = re.search(r'pb-title-prefix[^>]*>\s*([^<]+?)\s*<', blk)
        tclass = html.unescape(helpm.group(1)).strip() if helpm else ""
        if not tclass:
            mtc = re.search(r'【([^】]{1,8})】', title)
            tclass = mtc.group(1) if mtc else ""
        _tb_dump({"src": "html", "tid": tid, "title": title, "tclass": tclass,
                  "prefix_html": blk[max(0, m.start() - 400):m.start()]})
        rm = re.search(r'threadlist_rep_num[^>]*>\s*(\d+)', blk)
        reply = int(rm.group(1)) if rm else 0
        cm = re.search(r'is_show_create_time[^>]*>\s*([^<]+?)\s*<', blk)
        pubdate = tb_time_parse(cm.group(1)) if cm else 0
        am = re.search(r'class="frs-author-name[^"]*"[^>]*>([^<]+)<', blk)
        author = html.unescape(am.group(1)).strip() if am else ""
        absm = re.search(r'threadlist_abs[^>]*>\s*(.*?)\s*</div>', blk, re.S)
        abstract = ""
        if absm:
            abstract = re.sub(r"<[^>]+>", "", absm.group(1)).strip()[:600]
        imgs = []
        # 优先 bpic(580宽大图, 预览清晰); 退回 data-original(200×90缩略图)
        for u in re.findall(r'\bbpic="([^"]+)"', blk):
            u = u.strip()
            if u.startswith("//"):
                u = "https:" + u
            if "bdstatic" in u and "public/img" in u:
                continue   # 占位图
            imgs.append(u)
            if len(imgs) >= 2:
                break
        if not imgs:
            for u in re.findall(r'data-original="([^"]+)"', blk):
                u = u.strip()
                if u.startswith("//"):
                    u = "https:" + u
                if "bdstatic" in u and "public/img" in u:
                    continue   # 占位图
                # 缩略图 → 大图: wh%3D200%2C90%3B → w%3D580%3B
                u = re.sub(r"wh%3D200%2C90%3B", "w%3D580%3B", u)
                imgs.append(u)
                if len(imgs) >= 2:
                    break
        url = "https://tieba.baidu.com/p/%s" % tid
        items.append({
            "key": url, "platform": "贴吧", "title": title[:120],
            "author": author[:30],
            "desc": abstract[:500],
            "cover": imgs[0] if imgs else "",
            "covers": imgs[:2],
            "views": 0, "likes": 0, "danmaku": None,
            "reply": reply, "duration": 0,
            "pubdate": pubdate,
            "url": url,
            "tname": fname, "area": "", "tag": "", "tclass": tclass[:12],
            "gid": "tieba", "src": "贴吧·%s" % fname,
        })
    return items


def tb_time_parse(raw):
    """贴吧时间: '7-30' / '23:32' / '08-23 12:00' → 时间戳(今年/最近)。"""
    raw = (raw or "").strip()
    if not raw:
        return 0
    now = time.localtime()
    m = re.match(r"^(\d{1,2})-(\d{1,2})", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            t = time.mktime((now.tm_year, month, day, 12, 0, 0, 0, 0, -1))
            if t > time.time() + 86400:   # 未来 → 去年
                t = time.mktime((now.tm_year - 1, month, day, 12, 0, 0, 0, 0, -1))
            return int(t)
    m = re.match(r"^(\d{1,2}):(\d{2})", raw)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))
            return int(t)
    return 0


def fetch_tieba_bar(fname, page=0):
    """单个吧列表页 → (items, err)。页游标外部控制。"""
    url = "https://tieba.baidu.com/f?kw=%s&ie=utf-8&pn=%d" % (
        urllib.parse.quote(fname), page * 50)
    body, err = _tb_fetch(url)
    if not body:
        return [], err or "无数据"
    items = _tb_html_to_items(body, fname)
    if not items:
        return [], "解析为空(吧名无效/改版?)"
    return items, ""


_tb_cooldown_until = 0   # 贴吧全403冷却截止时间戳(避免每次构建都白等重试链)
_tb_retry_lock = threading.Lock()
_tb_retry_at = 0         # 上次手动放行重试的时间戳(最短间隔 60 秒)
# 贴吧补池的最小间隔(v0.3.1.59): 信息流补池是"滚动就补", 滚动一分钟能补十几次;
# 贴吧按这个频率打, 一分钟就是二三十个请求 —— 触发风控只是时间问题。贴吧本来就不需要
# 那么新, v0.4.2 起 150 秒一轮(抖动到 225 秒), 而且和本地闸门(每分钟 10 个)互不冲突。
_TB_POOL_MIN_GAP = 150
_tb_pool_last_ts = 0.0


def _tb_allow_retry():
    """用户按「刷新」时放行一次贴吧重试: 清掉 403 冷却。
    冷却 10 分钟是给自动构建用的, 用户主动点刷新还要干等就太反直觉了;
    但连点会加重百度风控, 所以 60 秒内只放行一次。"""
    global _tb_cooldown_until, _tb_retry_at, _tb_pool_last_ts
    now = time.time()
    if now - _tb_retry_at < 60:
        return False
    with _tb_retry_lock:
        if now - _tb_retry_at < 60:
            return False
        _tb_retry_at = now
        _tb_cooldown_until = 0
        _tb_pool_last_ts = 0   # 手动放行: 贴吧最小间隔也一并放掉
    return True


def fetch_tieba_pool(quick=False):
    """吧池轮换采集: 每轮取1-2个吧(并行), 每吧1页。返回 (items, errors)。
    403冷却: 一旦全部403进入冷却期, 期间直接空返回(不再拖慢内容池构建)。"""
    global _tb_last_err, _tb_last_fetch, _tb_fetch_total, _tb_cooldown_until
    global _tb_pool_last_ts
    if tb_weight() <= 0:
        return [], []   # 占比=0: 后台完全停抓(显式搜索走 tb_search_items, 不受影响)
    bars = tb_users()
    if not bars:
        return [], []
    if time.time() < _tb_cooldown_until:
        return [], []   # 冷却中: 快速返回, 不浪费构建时间
    now_ts = time.time()
    # v0.3.1.66: 间隔也加抖动。固定整点一到就抓, 从站方看是"表一样准时"的脚本特征;
    # v0.4.2: 基础间隔 150 秒 + 1.0~1.5 倍抖动(150~225 秒), 进一步弱化定时脚本特征。
    if now_ts - _tb_pool_last_ts < _TB_POOL_MIN_GAP * random.uniform(1.0, 1.5):
        return [], []   # 距上次贴吧抓取太近: 这次不抓(不是失败, 不写 tb_error)
    _tb_pool_last_ts = now_ts
    items, errors, seen = [], [], set()
    per = 1 if quick else 2
    idx = int(sget("tb_cursor") or 0)
    picked = [bars[(idx + i) % len(bars)] for i in range(min(per, len(bars)))]
    sset("tb_cursor", (idx + per) % len(bars))
    # 每吧随机页, 偏向前几页但引入乱序。
    # v0.3.1.66: 两个吧改**顺序**抓, 中间插 0.9~2.2 秒随机间隔 —— 并行时是同一秒从同一个
    # 出口打两个吧, 正是最容易触发百度风控的那种"多点齐发"形状; 一轮多花不到两秒。
    allow_help_fetch = sget("tb_allow_help") != "0"
    results = []
    for _bi, _fname in enumerate(picked):
        if _bi:
            time.sleep(random.uniform(0.9, 2.2))
        results.append(fetch_tieba_bar(_fname, random.choice([0, 0, 1, 2])))
    for fname, (its, err) in zip(picked, results):
        if its:
            for it in its:
                if it["key"] in seen:
                    continue
                if not allow_help_fetch and is_help_post(it):
                    _funnel_bump("help")
                    continue   # 求助帖抓取期直接剔除, 不占池子名额与筛选预算
                seen.add(it["key"])
                items.append(it)
        else:
            # v0.3.1.55: 被本地闸门拦下的那次不算"抓取失败"(它根本没发出去),
            # 否则界面上会冒出一片假报错, 掩盖真正的 403/网络原因。
            if err == "本地限流(未发出)":
                continue
            errors.append("%s: %s" % (fname, err or "空"))
    if items:
        _tb_last_fetch = int(time.time())
        _tb_fetch_total += len(items)
        sset("tb_last_fetch", _tb_last_fetch)
        sset("tb_error", "")
        _tb_last_err = ""
        _tb_cooldown_until = 0
    elif errors and all("403" in e or "402" in e or "429" in e for e in errors):
        # 全部限流 → 冷却 10 分钟后再试。早先是 60 分钟: 百度风控往往是短时的,
        # 一小时的"抓取失败"用户只会以为功能坏了(还以为是登录过期), 而同一出口 IP
        # 重试的边际风险很低 —— 真正的持续封锁(实测本机连 8 次全 403)靠放宽冷却解决不了。
        # 用户仍可点顶栏「刷新」手动放行一次重试(_tb_allow_retry)。
        _tb_last_err = "贴吧被百度安全验证拦截(403), 10 分钟后自动重试"
        sset("tb_error", _tb_last_err)
        _tb_cooldown_until = int(time.time()) + 600
        _net_penalty("tieba", 1800, 2.5)   # 解封后也慢一点: 一解封就照原速打, 往往几分钟内再被抓
        return [], errors
    elif errors:
        _tb_last_err = "; ".join(errors)[:300]
        sset("tb_error", _tb_last_err)
    else:
        sset("tb_error", "")
        _tb_last_err = ""
    return items, errors


def _tb_search_fetch(url, referer="https://tieba.baidu.com/", timeout=12):
    """贴吧 wap JSON 接口 GET: 登录cookie(含人工验证会话)优先, 匿名走"种BAIDUID→请求"。
    与 _tb_fetch 不同: 纯JSON响应, 不做 j_thread_list 内容标记校验。"""
    if not _net_guard("tieba", 1.8, 8, wait=2.0):
        return None, "本地限流(未发出)"
    # 过了滑块的会话只在浏览器里有效(urllib 带同样 Cookie 仍 403) → 有那扇窗就先走它。
    # 注意这里要的是**文本**: 这个接口返回 JSON, 浏览器会把它渲染进 <pre>,
    # 拿 outerHTML 去 json.loads 永远失败(旧实现就是这样, 搜索一直是空的)。
    if _tb_br_channel():
        txt, berr = _tb_browser_text(url, timeout=15)
        if txt:
            return txt, None
        if berr == "百度安全验证(403)":
            return None, berr
        if not _cdp_alive():
            _tb_br_forget()
    cookie = _tb_cookie_header()
    if cookie:
        try:
            req = urllib.request.Request(url, headers=dict(
                TB_HEADERS, Referer=referer, Cookie=cookie))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore"), None
        except Exception:
            pass   # 登录态失效/限流 → 降级匿名
    jar = _tb_session_jar()
    _tb_seed(jar)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        req = urllib.request.Request(url, headers=dict(TB_HEADERS, Referer=referer))
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore"), None
    except Exception as e:
        return None, str(e)[:80]


def _tb_json_body(body):
    """把 JSON 接口的响应洗成纯 JSON 文本。

    浏览器通道拿回来的是**渲染后的 HTML**(浏览器会把 JSON 放进 <pre>),
    也可能前面挂着 <!DOCTYPE html> 之类。直接 json.loads 必失败 —— 贴吧搜索
    因此长期返回空列表。这里按"原文 / <pre> 内容 / 第一个 { 或 [ 之后"依次尝试。"""
    s = (body or "").strip()
    if not s:
        return ""
    if s[0] in "{[":
        return s
    m = re.search(r"<pre[^>]*>(.*?)</pre>", s, re.S | re.I)
    if m:
        s = html.unescape(m.group(1)).strip()
        if s[:1] in "{[":
            return s
    i = min([p for p in (s.find("{"), s.find("[")) if p >= 0] or [-1])
    if i >= 0:
        return s[i:]
    return ""


def _tb_int(v):
    """贴吧接口数字字段兼容: "1.2W"/"8600"/"931" 等都能转成 int"""
    try:
        s = str(v).strip()
        m = re.match(r"([\d.]+)([Ww万]?)$", s)
        if not m:
            return 0
        n = float(m.group(1))
        if m.group(2):
            n *= 10000
        return int(n)
    except Exception:
        return 0


def tb_search_items(q, rn=20, fname=""):
    """贴吧帖子搜索; fname 非空时只搜索指定吧, 防止严肃模式扫全站。"""
    q = (q or "").strip()
    fname = (fname or "").strip()
    if not q or time.time() < _tb_cooldown_until:
        return []
    url = ("https://tieba.baidu.com/mo/q/search/thread?word=%s&pn=1&rn=%d"
           % (urllib.parse.quote(q), rn))
    if fname:
        url += "&kw=" + urllib.parse.quote(fname)
    
    body, err = _tb_search_fetch(url)
    if not body:
        return []
    try:
        d = json.loads(_tb_json_body(body))
    except Exception:
        _tb_log("搜索响应不是 JSON(前 80 字): %s" % (body[:80].replace("\n", " "),))
        return []
    if d.get("no") != 0:
        return []
    items = []
    for p in ((d.get("data") or {}).get("post_list") or []):
        tid = str(p.get("tid") or "")
        title = re.sub(r"</?em>", "", html.unescape(p.get("title") or "")).strip()
        if not tid or not title:
            continue
        _tb_dump({"src": "search", "tid": tid, "title": title,
                  "keys": list(p.keys()),
                  "fields": {k: str(v)[:180] for k, v in p.items()}})
        # 贴吧分类文本字段(thread_types 是复杂位掩码, 无求助专属位, 不作判据) / 标题【求助】
        tclass = ""
        for fk in ("topic", "types", "is_help", "has_help", "is_help_thread"):
            v = p.get(fk)
            if not v:
                continue
            if isinstance(v, list):
                for el in v:
                    nm = el.get("name") if isinstance(el, dict) else str(el)
                    if nm:
                        tclass = nm
                        break
            elif isinstance(v, dict):
                tclass = v.get("name") or ""
            else:
                tclass = str(v)
            if tclass:
                break
        if not tclass:
            mtc = re.search(r'【([^】]{1,8})】', title)
            tclass = mtc.group(1) if mtc else ""
        cover = ""
        for m_ in (p.get("media") or []):
            u = m_.get("small_pic") or m_.get("water_pic") or ""
            if u:
                cover = u
                break
        user = p.get("user") or {}
        fname = p.get("forum_name") or (p.get("forum_info") or {}).get("forum_name") or ""
        u_ = "https://tieba.baidu.com/p/" + tid
        it = {
            "key": u_, "platform": "贴吧", "title": title[:120],
            "author": (user.get("show_nickname") or user.get("user_name") or "")[:30],
            "desc": re.sub(r"<[^>]+>", "", p.get("content") or "")[:500],
            "cover": cover, "covers": [cover] if cover else [],
            "views": 0, "likes": _tb_int(p.get("like_num")), "danmaku": None,
            "reply": _tb_int(p.get("post_num")), "duration": 0,
            "pubdate": int(p.get("time") or p.get("create_time") or 0),
            "url": u_, "tname": fname, "area": "", "tag": fname, "tclass": tclass[:12],
            "gid": "tieba", "src": "贴吧·%s" % (fname or "搜索"),
        }
        if tieba_bar_allowed(it):
            items.append(it)
    return items


def _tieba_thread_cache_put(key, payload):
    _tieba_thread_cache[key] = {"payload": payload, "ts": time.time()}
    if len(_tieba_thread_cache) > 80:   # 容量控制: 丢最旧
        oldest = min(_tieba_thread_cache, key=lambda k: _tieba_thread_cache[k]["ts"])
        _tieba_thread_cache.pop(oldest, None)


def tieba_thread_content(tid):
    """抓取并解析贴吧 wap 帖子页(mo/q/m) → 站内阅读数据。
    wap 页固定返回首批楼层(不支持pn翻页, AJAX加载的后续楼层拿不到);
    图片用 pb_img_item 的完整 data-url(带签名参数)——不带签名直连原图会得到
    "打开贴吧App"的 4KB 占位图。杂项(diversBanner/脚本/操作按钮栏)全部剥离。"""
    key = str(tid).strip()
    cached = _tieba_thread_cache.get(key)
    if cached and time.time() - cached["ts"] < _TIEBA_THREAD_TTL:
        return cached["payload"]
    body, err = _tb_fetch("https://tieba.baidu.com/mo/q/m?kz=%s" % key, timeout=15)
    if err or not body:
        return {"ok": False, "error": err or "抓取失败"}
    if "list_item_top_name" not in body:
        return {"ok": False, "error": "被贴吧风控拦截，稍后再试或点右上角跳转App"}
    t_m = re.search(r"<title>([^<]+)</title>", body)
    title = html.unescape(t_m.group(1)).strip() if t_m else "贴吧帖子"
    title = re.sub(r"[_\-]?\s*贴吧\s*$", "", title)
    floors = []
    chunks = re.split(r'<li [^>]*?data-tid="\d+"', re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.S))[1:]
    for ch in chunks:
        ch = re.split(r'<div class="father-cut-daoliu-normal-box"|<div class="operate_statistic_wrapper"', ch)[0]   # 楼层内容在其之前, 之后是导流页脚/回复收藏栏
        a = re.search(r'class="user_name"[^>]*>([^<]+)<', ch)
        t = re.search(r'list_item_time">([^<]+)<', ch)
        av = re.search(r'<img src="([^"]+)"[^>]*class="user_img"', ch)
        lz = re.search(r'<div class="content"[^>]* lz="1"', ch)
        fn = re.search(r'fn="(\d+)"', ch)
        cm = re.search(r'<div class="content"[^>]*>(.*)', ch, re.S)
        if not cm:
            continue
        content = cm.group(1)
        images = []
        for u in re.findall(r"data-url='([^']+)'", content):
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http") and u not in images:
                images.append(u)
        text = re.sub(r'<div id="diversBanner".*?</div>', "", content, flags=re.S)
        text = re.sub(r'<div class="pb_less_imgs">.*$', "", text, flags=re.S)
        text = re.sub(r"<img[^>]*>", " ", text)
        text = re.sub(r"<br\s*/?>", chr(10), text)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text).strip()
        if not text and not images:
            continue
        floors.append({"floor": int(fn.group(1)) if fn else len(floors) + 1,
                       "author": html.unescape(a.group(1)).strip() if a else "匿名",
                       "avatar": (av.group(1) if av else "").replace("http://", "https://"),
                       "lz": bool(lz),
                       "time": t.group(1).strip() if t else "",
                       "text": text[:2000], "images": images[:6]})
    payload = {"ok": True, "title": title, "floors": floors[:30]}
    _tieba_thread_cache_put(key, payload)
    return payload


def _xhh_thread_cache_put(key, payload):
    _xhh_thread_cache[key] = {"payload": payload, "ts": time.time()}
    if len(_xhh_thread_cache) > 80:
        oldest = min(_xhh_thread_cache, key=lambda k: _xhh_thread_cache[k]["ts"])
        _xhh_thread_cache.pop(oldest, None)


def xhh_thread_content(link_id):
    """小黑盒帖子站内阅读: /bbs/app/link/tree → link.text 富文本块 + 首页评论。
    link.text 是 JSON 数组字符串: {"text":"<p>..</p>"} 文本块 与 {"url":图} 图块交替。"""
    key = str(link_id).strip()
    cached = _xhh_thread_cache.get(key)
    if cached and time.time() - cached["ts"] < _XHH_THREAD_TTL:
        return cached["payload"]
    d, err = xhh_get("/bbs/app/link/tree", {"link_id": key})
    if err or not d:
        return {"ok": False, "error": err or "接口失败"}
    if d.get("status") == "show_captcha":
        # 验证码 → 降级: 用本地池里已入库的描述/封面拼一个预览, 不让用户白点
        try:
            row = db().execute(
                "SELECT json FROM items WHERE json LIKE ? ORDER BY fetched_at DESC LIMIT 1",
                ('%' + key + '%',)).fetchone()
            if row:
                it = json.loads(row[0])
                if it.get("platform") == "小黑盒":
                    return {"ok": True, "degraded": True,
                            "title": it.get("title") or "小黑盒帖子",
                            "author": it.get("author") or "",
                            "create_at": int(it.get("pubdate") or 0),
                            "content": (it.get("desc") or "")[:500],
                            "images": [c for c in (it.get("covers") or []) if c][:2],
                            "comments": [], "comment_total": it.get("reply")}
        except Exception:
            pass
        return {"ok": False, "error": "小黑盒要求人机验证(访问过频或未登录)：稍后再试，或在设置登录小黑盒账号后重试；也可点右上角跳转App查看"}
    if d.get("status") != "ok":
        return {"ok": False, "error": "接口状态异常: %s" % d.get("status")}
    res = d.get("result") or {}
    link = res.get("link") or {}
    blocks = []
    try:
        blocks = json.loads(link.get("text") or "[]")
        if not isinstance(blocks, list):
            blocks = []
    except Exception:
        blocks = []
    texts, images = [], []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        u = b.get("url")
        if u and u not in images:
            images.append(u)
        t = b.get("text")
        if t:
            t = t.replace("</p>", chr(10))
            t = t.replace("<br>", chr(10)).replace("<br/>", chr(10)).replace("<br />", chr(10))
            t = re.sub(r"<[^>]+>", "", t)
            t = html.unescape(t).strip()
            if t:
                texts.append(t)
    for u in (link.get("imgs") or []):
        if u and u not in images:
            images.append(u)
    comments = []
    for w in (res.get("comments") or [])[:20]:
        # 防御: 这个接口的 comments 元素有时把 comment 包成一层数组, 有时 w 本身不是 dict。
        # 原实现直接 (w or {}).get("comment") 后当 dict 用, 遇到这两种就 AttributeError,
        # HTTP 处理器异常会把连接掐断 —— 前端只看到网络错误, 站内阅读直接不可用。
        if not isinstance(w, dict):
            continue
        c = w.get("comment")
        if isinstance(c, list):
            c = c[0] if c and isinstance(c[0], dict) else {}
        if not isinstance(c, dict):
            continue
        u = c.get("user") if isinstance(c.get("user"), dict) else {}
        txt = re.sub(r"\[\w+\]|\[cube_[^\]]*\]", "", c.get("description") or c.get("text") or c.get("content") or "")
        txt = html.unescape(txt).strip()
        cimgs = [i for i in (c.get("imgs") or c.get("pics") or []) if i]
        if not txt and not cimgs:
            continue
        comments.append({"author": (u.get("username") or "匿名")[:30],
                         "text": txt[:300], "images": cimgs[:3],
                         "create_at": int(c.get("create_at") or 0)})
    payload = {"ok": True,
               "title": (link.get("title") or "")[:120],
               "xlid": str(link.get("linkid") or ""),
               "ltag": str(link.get("link_tag") or ""),
               "author": ((link.get("user") or {}).get("username") or "")[:30],
               "create_at": int(link.get("create_at") or 0),
               "content": chr(10).join(x for x in texts if x)[:8000],
               "images": images[:12],
               "comments": comments,
               "comment_total": res.get("total_floor_num")}
    _xhh_thread_cache_put(key, payload)
    return payload


def tb_login_check():
    """贴吧登录态: dc/common/tbs → is_login。返回 dict(含昵称/头像)。
    判定前提: 必须含 BDUSS(半截cookie如有STOKEN无BDUSS视为未登录)。
    v0.3.1.55: 结果缓存 5 分钟 —— 这个函数原来被未读角标每 60 秒调两次(未读里又调一次),
    一次界面轮询就是 4 个贴吧请求, 挂着不动一天几千次, 正是招风控的那种节奏。"""
    now = time.time()
    if _tb_login_cache["data"] is not None and now - _tb_login_cache["ts"] < 300:
        return dict(_tb_login_cache["data"])
    nickname, avatar = sget("tb_user") or "", sget("tb_avatar") or ""
    cookie = tb_cookie()
    if not cookie or "BDUSS=" not in cookie:
        out = {"logged_in": False, "nickname": nickname, "avatar": avatar}
        _tb_login_cache.update(ts=now, data=out)
        return dict(out)
    if not _net_guard("tieba", 2.0, 10):
        # 太密: 这次不查, 沿用上次结论(没有就按"有 cookie 就算登录")
        prev = _tb_login_cache["data"]
        return dict(prev) if prev else {"logged_in": True, "nickname": nickname or "已登录",
                                        "avatar": avatar}
    try:
        req = urllib.request.Request("https://tieba.baidu.com/dc/common/tbs",
                                     headers=dict(TB_HEADERS, Cookie=cookie))
        d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
        if d.get("is_login") == 1:
            out = {"logged_in": True, "nickname": nickname or "已登录",
                   "avatar": avatar, "tbs": d.get("tbs", "")}
        else:
            out = {"logged_in": False, "nickname": "", "avatar": ""}
    except Exception:
        out = {"logged_in": bool(cookie), "nickname": nickname, "avatar": avatar}
    _tb_login_cache.update(ts=time.time(), data=out)
    return dict(out)


def tb_profile_from_cookie():
    """用BDUSS拉昵称/头像(mo/q/newmoindex, 手机版网页接口, 免CDP)。
    portrait 形如 tb.1.xxxx → 头像 https://tb.himg.baidu.com/sys/portrait/item/<portrait>。
    返回 (nickname, avatar_url), 失败返回 ("", "")。"""
    cookie = tb_cookie()
    if not cookie or "BDUSS=" not in cookie:
        return "", ""
    try:
        req = urllib.request.Request("https://tieba.baidu.com/mo/q/newmoindex",
                                     headers=dict(TB_HEADERS, Referer="https://tieba.baidu.com/",
                                                  Cookie=cookie))
        d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore"))
        data = d.get("data") or {}
        portrait = str(data.get("portrait") or "").strip()
        name = str(data.get("name") or data.get("nickname") or "").strip()
        avatar = ("https://tb.himg.baidu.com/sys/portrait/item/" + portrait) if portrait else ""
        return name, avatar
    except Exception:
        return "", ""


# ---- 消息角标「看过」快照 (v0.3.1.66, 用户要求: 点完消息按钮红点立刻消失) ----
# 语义纯本地: 记下用户点开消息页那一刻各平台的未读数, 前端角标只画"这之后新增的量"。
# 不去打各平台的"标记已读"接口 —— 那些接口没有稳定契约, 而且多打一次就多一次风控样本。
# 存法: settings 表里一条 JSON, 形如 {"bili": 3, "tieba": 0, "xhh": 12}。
_MSG_PLATFORMS = ("bili", "tieba", "xhh")


def msg_seen():
    """读已读快照; 坏数据一律当 0(宁可多显示一个红点, 也不能因为脏数据把提示吞掉)。"""
    try:
        d = json.loads(sget("msg_seen") or "{}")
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    out = {}
    for k in _MSG_PLATFORMS:
        try:
            out[k] = max(0, int(d.get(k) or 0))
        except Exception:
            out[k] = 0
    return out


def msg_seen_save(snap):
    """把前端上报的未读数存成快照。只收三个平台的非负整数, 其它键忽略。"""
    clean = {}
    for k in _MSG_PLATFORMS:
        try:
            clean[k] = max(0, int((snap or {}).get(k) or 0))
        except Exception:
            clean[k] = 0
    sset("msg_seen", json.dumps(clean))
    return clean


def tb_unread_messages():
    """读取贴吧“回复我 + @我”的真实未读数，无法确认时返回None。
    v0.3.1.55: 缓存 5 分钟 + 出门前过闸门。未读角标是"看一眼"的信息, 不值得每 60 秒
    去百度翻两页 HTML —— 那正是把人打成"刷站"的那类流量。"""
    now = time.time()
    if _tb_unread["ts"] and now - _tb_unread["ts"] < 300:
        return _tb_unread["data"]
    state = tb_login_check()
    if not state.get("logged_in"):
        return None
    cookie = tb_cookie()
    total = 0
    try:
        for page, key in (("https://tieba.baidu.com/mo/q/replyme", "replyme"),
                          ("https://tieba.baidu.com/mo/q/atme", "atme")):
            if not _net_guard("tieba", 2.0, 10):
                return _tb_unread["data"]
            req = urllib.request.Request(page, headers=dict(TB_HEADERS,
                                         Referer="https://tieba.baidu.com/", Cookie=cookie))
            raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
            text = html.unescape(raw)
            # 页面组件配置中的 count.replyme/count.atme 是服务端提供的真实未读数。
            match = re.search(r"(?:[\"']?%s[\"']?\s*[:=]\s*)[\"']?(\d+)" % key, text)
            if not match:
                return None
            total += int(match.group(1))
        _tb_unread.update(ts=now, data=max(0, total))
        return _tb_unread["data"]
    except Exception:
        return None


def tb_mylike():
    """登录后拉取关注吧列表(最多3页)。返回 (bars, err)。"""
    cookie = tb_cookie()
    if not cookie:
        return [], "未登录"
    bars, seen = [], set()
    for pn in range(1, 4):
        url = "https://tieba.baidu.com/f/like/mylike?ie=utf-8&pn=%d" % pn
        if not _net_guard("tieba", 1.5, 10, wait=2.0):
            return bars or None, "本地限流(未发出)"
        try:
            req = urllib.request.Request(url, headers=dict(TB_HEADERS, Cookie=cookie))
            body = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        except Exception as e:
            return bars or None, str(e)[:80]
        found = False
        for m in re.finditer(r'href="/f\?kw=([^"&]+)"', body):
            name = urllib.parse.unquote(m.group(1))
            if name and name not in seen and not any(c in name for c in " ￥%#"):
                seen.add(name)
                bars.append(name)
                found = True
        if not found and pn == 1:
            return None, "未找到关注吧(登录失效?)"
    return bars[:50], ""


# ---------------------------------------------------------------- 贴吧一键登录(CDP)
#
# 扫码接口已死(实测getqrcode/getqrcodestatus均返回同一张固定图);
# 客户端API需私有签名。方案: 弹出真实浏览器窗口(Edge/Chrome, 独立profile),
# 用户像平时一样登录(短信/扫码/第三方任选), 后台经 CDP getAllCookies 抓 BDUSS。

_TB_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
    r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
    r"C:\Program Files\Opera\launcher.exe",
]


def _tb_find_browser():
    """寻找支持 CDP 的 Chromium 浏览器; 不要求必须 Edge/Chrome。"""
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = list(_TB_BROWSERS) + [
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(local, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        os.path.join(local, "Vivaldi", "Application", "vivaldi.exe"),
        os.path.join(local, "Programs", "Opera", "opera.exe"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    for name in ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe", "chromium.exe"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def _cdp_launch(url, profile_name="_tb_login_profile", fresh=False):
    """启动一个带远程调试端口的独立浏览器窗口(独立 profile)。
    返回 (proc, port, err): port 就绪才算成功; 失败时 err 是给用户看的原因。
    v0.3.1.55 从 tb_login_start 里抽出来 —— 登录和「百度安全验证」共用同一套启动逻辑。
    v0.3.1.57: **不再每次把 profile 整份删掉**, 只清残留的单例锁文件。
    原因: 百度滑块验证通过后写下的会话就在 profile 里, 删了等于每次都要重新拖一遍;
    实测"浏览器里能看贴吧、程序里 403"正是会话没被留住的后果。
    只有真起不来(profile 损坏)时才退回"整份删掉重来"。
    v0.3.1.59: 连"整份删掉"也不做了 —— 删掉的正是过了滑块的会话。现在:
    ① 上次的窗口还开着(端口文件连得上)就直接复用它;
    ② 新进程被 Chrome 单例顶回来时, 再等一下端口文件, 连得上就复用;
    ③ 确实起不来才把 profile 改名备份(**不删**)再来一次。"""
    browser = _tb_find_browser()
    if not browser:
        return None, 0, "没找到可用的 Chromium 浏览器(Chrome/Edge)"
    profile = os.path.join(DATA_DIR, profile_name)
    port_file = os.path.join(profile, "DevToolsActivePort")
    # 上次的窗口可能还开着(单例机制会把这个 profile 交给它): 端口文件连得上就复用
    stale = _read_devtools_port(port_file)
    if stale and _port_alive(stale):
        return None, stale, ""
    for _n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile, _n))
        except Exception:
            pass
    os.makedirs(profile, exist_ok=True)
    try:
        proc = subprocess.Popen([
            browser, "--remote-debugging-port=0",
            "--user-data-dir=%s" % profile,
            "--no-first-run", "--no-default-browser-check",
            # 关键: 跳过Edge首启同步确认页(edge://sync-confirmation-dialog会拦截一切导航,
            # 导致登录后贴吧页面无法加载、头像抓取失败)
            "--disable-features=msEdgeFirstRunExperience,msEdgeSidebarV2,msEdgeWalletCheckout",
            "--disable-sync", "--disable-background-networking", "--disable-component-update",
            "--disable-gpu",
            url,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return None, 0, "启动失败: %s" % str(e)[:100]
    # 读 DevToolsActivePort 文件拿到实际端口(port=0 自动分配)
    port = 0
    for _ in range(60):
        if os.path.exists(port_file):
            try:
                with open(port_file) as f:
                    port = int(f.readline().strip())
                break
            except Exception:
                pass
        if proc.poll() is not None:
            # 新进程被顶回来了: 多半是已有窗口占着这个 profile。它可能刚写完端口文件,
            # 再等 10 秒看一次 —— 连得上就直接用那个窗口(会话就在里面)。
            for _ in range(20):
                p2 = _read_devtools_port(port_file)
                if p2 and _port_alive(p2):
                    return None, p2, ""
                time.sleep(0.5)
            if fresh:
                return proc, 0, "浏览器进程退出"
            # 真起不来: profile 可能损坏。整份**改名备份**(不删, 里面可能有过滑块的会话)再试一次
            try:
                os.rename(profile, "%s.bak-%s" % (profile, time.strftime("%Y%m%d%H%M%S")))
            except Exception:
                pass
            return _cdp_launch(url, profile_name, fresh=True)
        time.sleep(0.5)
    if not port:
        return proc, 0, "调试端口未就绪(10s)"
    return proc, port, ""


def tb_login_start():
    """启动浏览器窗口(独立profile, 打开贴吧登录页)。"""
    global _CDP_PROC, _CDP_PORT, _CDP_LOGIN
    if _CDP_PROC and _CDP_PROC.poll() is None:
        return {"ok": True, "message": "登录窗口已打开"}
    browser = _tb_find_browser()
    if not browser:
        try:
            webbrowser.open("https://passport.baidu.com/v2/?login")
            _CDP_LOGIN = {"ts": int(time.time()), "status": "manual",
                          "message": "已用默认浏览器打开贴吧登录；登录后请粘贴 Cookie 保存"}
            return {"ok": True, "manual": True,
                    "message": "已用默认浏览器打开贴吧登录，请登录后粘贴 Cookie 保存"}
        except Exception as e:
            return {"ok": False, "error": "未找到支持自动登录的 Chromium 浏览器，且无法打开默认浏览器: %s" % str(e)[:80]}
    profile = os.path.join(DATA_DIR, "_tb_login_profile")
    proc, port, err = _cdp_launch("https://passport.baidu.com/v2/?login")
    if err:
        _CDP_PROC = proc
        _CDP_LOGIN.update(status="error", message=err)
        return {"ok": False, "error": err}
    # proc 为 None 表示复用的是已开着的窗口(见 _cdp_launch), 不能当成"窗口没开"
    _CDP_PROC, _CDP_PORT = proc, port
    _CDP_LOGIN = {"ts": int(time.time()), "status": "waiting",
                  "message": "请在窗口中完成登录(短信/扫码/第三方均可)"}
    return {"ok": True, "message": "登录窗口已打开" if proc else "已复用打开的浏览器窗口"}


def tb_login_poll():
    """轮询 CDP 取 cookie; 抓到 BDUSS 即保存、顺路抓昵称/头像、关闭窗口。"""
    global _CDP_PROC, _CDP_PORT, _CDP_LOGIN
    if not _cdp_alive():
        _CDP_LOGIN.update(status="closed", message="窗口已关闭")
        return dict(_CDP_LOGIN)
    if time.time() - _CDP_LOGIN["ts"] > 600:
        tb_login_stop()
        return {"status": "expired", "message": "登录超时(10分钟), 请重试"}
    try:
        import json as _json
        with local_open("http://127.0.0.1:%d/json/list" % _CDP_PORT,
                                    timeout=3) as resp:
            targets = _json.loads(resp.read().decode("utf-8"))
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            return {"status": "waiting", "message": "等待登录页面…"}
        ws = page["webSocketDebuggerUrl"]
        cookies = _cdp_get_cookies(ws)
        if not cookies:
            return {"status": "waiting", "message": "轮询中…"}
        got = {}
        for c in cookies:
            dom = c.get("domain") or ""
            if "baidu.com" in dom and c.get("name") in _CDP_HOST_KEYS:
                got[c["name"]] = c["value"]
        if got.get("BDUSS"):
            cookie = "; ".join("%s=%s" % (k, v) for k, v in got.items())
            sset("tb_cookie", cookie)
            # 顺路抓昵称/头像: 导航贴吧首页 → 读 DOM(失败不阻塞登录)
            nickname, portrait = tb_profile_from_page(ws)
            sset("tb_user", nickname or "已登录")
            sset("tb_avatar", portrait)
            tb_login_stop()
            return {"status": "ok", "message": "贴吧登录成功", "cookie_saved": True,
                    "nickname": nickname or "已登录", "avatar": portrait}
        return {"status": "waiting", "message": "正在登录…(检测到登录状态后自动完成)"}
    except Exception as e:
        _CDP_LOGIN.update(status="waiting", message="轮询: %s" % str(e)[:60])
        return dict(_CDP_LOGIN)


def tb_login_stop():
    global _CDP_PROC, _CDP_PORT, _CDP_LOGIN
    if _CDP_PROC and _CDP_PROC.poll() is None:
        try:
            # taskkill /T: 杀整棵进程树(仅terminate主进程会残留子进程, 锁住profile)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(_CDP_PROC.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            try:
                _CDP_PROC.terminate()
                _CDP_PROC.wait(timeout=5)
            except Exception:
                try:
                    _CDP_PROC.kill()
                except Exception:
                    pass
    elif _cdp_alive():
        # 窗口不是本进程开的(应用重启后接回来的): Popen 杀不到, 走 CDP 关页面
        _cdp_close_remote()
    _CDP_PROC = None
    _CDP_PORT = 0
    _tb_br_forget()                  # 窗口没了, 浏览器抓取通道(含落盘状态)跟着失效
    _CDP_LOGIN.update(status="stopped", message="窗口已关闭")
    return {"ok": True}


def _tb_br_idle_watch():
    """通道闲置太久才关窗。

    旧实现是"验证通过 3 分钟就关", 而贴吧补池是持续发生的: 3 分钟一到窗口被关,
    后面每一次请求都回落到 urllib → 又 403, 用户看到的就是"验证完还是抓不到"。
    现在按"多久没用到这个通道"收工(_tb_browser_html 每次成功都会刷新 ts)。"""
    try:
        if not _TB_BR.get("ok"):
            return
        idle = time.time() - int(_TB_BR.get("ts") or 0)
        if idle < _TB_BR_IDLE_CLOSE:
            threading.Timer(max(60, _TB_BR_IDLE_CLOSE - idle), _tb_br_idle_watch).start()
            return
        _tb_log("浏览器通道闲置 %d 分钟, 自动关窗(下次验证会重新打开)" % (idle // 60))
        tb_login_stop()
        _CDP_VERIFY.update(status="stopped", message="验证窗口闲置太久, 已自动关闭")
    except Exception:
        pass


# ---------------- 百度安全验证(403)的人工滑块通道 (v0.3.1.55) ----------------
# 现场: tieba 列表页返回 403 + "百度安全验证"(BIOC 滑块页)。这是出口 IP 级的 WAF,
# 请求头怎么伪装都没用(实测 8/8 全 403)。能过它的只有"真人在真浏览器里拖一下滑块"。
# 做法: 把登录窗口那套 CDP 浏览器导航到**被拦的那个吧列表页**, 用户在窗口里完成验证,
# 然后从浏览器里把 baidu.com 的 cookie 原样抄进 tb_verify_cookie —— 同一出口 IP +
# 同一套 cookie, 抓取就能接着跑。用户不用复制粘贴、不用重启。
_CDP_VERIFY = {"ts": 0, "status": "idle", "message": "", "url": ""}


def _tb_verify_url():
    """验证页地址: 用吧池第一个吧(和抓取走同一个入口), 没配吧就开国际吧。"""
    bars = tb_users()
    if bars:
        return "https://tieba.baidu.com/f?kw=%s&pn=0" % urllib.parse.quote(bars[0])
    return "https://tieba.baidu.com/f?kw=%E5%9B%BD%E9%99%85&pn=0"


def tb_verify_page_ok(html_text):
    """这段响应是真列表页还是验证页/空壳?"""
    if not html_text:
        return False
    head = html_text[:6000]
    if "安全验证" in head or "wappass" in head:
        return False
    return ("j_thread_list" in html_text) or ("data-field" in html_text)


def _tb_browser_read(url, expr, timeout=15):
    """用验证窗口取一次页面: 导航 → 等加载完 → 在页面里求值 expr。返回 (value, err)。

    v0.3.1.57 起这是"过了滑块"之后**唯一**能真拿到贴吧内容的通路:
    实测同一个窗口里页面正常、拿同一份 Cookie 用 urllib 再打同一个 URL 依旧 403
    (百度按浏览器指纹判, 不只看 Cookie)。所以别再"抄 cookie 出来自己请求"了。"""
    ws = _tb_br_channel() or _TB_BR.get("ws") or _cdp_page_ws()
    if not ws:
        return "", "没有可用的浏览器窗口"
    if not _TB_BR_LOCK.acquire(timeout=timeout):
        return "", "浏览器通道忙"
    try:
        try:
            _cdp_eval(ws, "location.href=%s" % json.dumps(url), timeout=8)
        except Exception as e:
            _tb_br_heal(ws)     # ws 可能指向已经关掉的标签页: 换一个活着的页, 没有就作废通道
            return "", "导航失败: %s" % str(e)[:60]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            try:
                st = _cdp_eval(ws, "document.readyState", timeout=4)
            except Exception:
                st = ""
            if st == "complete":
                time.sleep(0.5)   # 留一拍给前端渲染列表
                break
        try:
            val = _cdp_eval(ws, expr, timeout=10)
        except Exception:
            val = ""
        if not isinstance(val, str) or not val:
            _tb_br_heal(ws)
            return "", "读不到页面内容"
        _TB_BR["ts"] = int(time.time())
        return val, ""
    finally:
        _TB_BR_LOCK.release()


def _tb_browser_html(url, timeout=15):
    """取一整页 HTML(列表页用)。返回 (html, err); 验证页换算成 403。"""
    html, err = _tb_browser_read(url, "document.documentElement.outerHTML", timeout=timeout)
    if err:
        return "", err
    head = html[:6000]
    if "安全验证" in head or "wappass" in head:
        return "", "百度安全验证(403)"
    return html, ""


def _tb_browser_text(url, timeout=15):
    """取一个"文本"响应(JSON 接口用): 浏览器渲染 JSON 时 <pre> 里就是原文,
    innerText 拿到的正好是它。旧实现拿 outerHTML 去 json.loads, 永远解析失败 ——
    贴吧搜索因此一直是空的。"""
    txt, err = _tb_browser_read(url, "(document.body ? document.body.innerText : '')",
                                timeout=timeout)
    if err:
        return "", err
    head = txt[:2000]
    if "安全验证" in head or "wappass" in head:
        return "", "百度安全验证(403)"
    return txt, ""


def _tb_verify_probe(cookie_str):
    """拿**浏览器自己**看一眼真实吧列表页: 页面里真是帖子列表才算过。
    cookie_str 只用于落盘(给别的路径参考), 判定一律走浏览器 ——
    换成 urllib 带 cookie 去试, 实测永远 403, 用户就会觉得"拖完还是没反应"。

    先看当前这一页, 不导航: 用户通常就是在这个窗口里拖完滑块、页面自己刷新成列表页,
    这时再导航一次既白打一个请求, 也可能把刚过的验证又踢回去。"""
    url = _tb_verify_url()
    ws = _TB_BR.get("ws") or _cdp_page_ws()
    if ws:
        try:
            html = _cdp_eval(ws, "document.documentElement.outerHTML", timeout=10)
        except Exception:
            html = ""
        if isinstance(html, str) and tb_verify_page_ok(html):
            _TB_BR["ts"] = int(time.time())
            return True, url, 200
    html, err = _tb_browser_html(url)
    if err:
        return False, url, (403 if "安全验证" in err else 0)
    return tb_verify_page_ok(html), url, 200


def _cdp_page_ws(prefer="tieba.baidu.com"):
    """当前浏览器窗口的页面调试地址(没有就返回 "").

    v0.3.1.65: 优先挑 URL 里含 tieba.baidu.com 的那个标签页。
    旧实现取 /json/list 里的**第一个** page —— 用户多半会在窗口里顺手新开一个标签页去看,
    或者在验证页旁边再开一页; 第一个 target 往往正是那个停在验证页/新标签页的死页,
    于是 _tb_browser_read 一直读错页: 界面上"验证完了还是抓不到", 日志里全是读不到内容。
    找不到贴吧页时仍退回第一个 page(刚开窗、还没导航到贴吧时就是这种情形)。"""
    if not _CDP_PORT:
        return ""
    try:
        with local_open("http://127.0.0.1:%d/json/list" % _CDP_PORT, timeout=3) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ""
    pages = [t for t in targets or [] if t.get("type") == "page"]
    if not pages:
        return ""
    if prefer:
        hit = next((t for t in pages if prefer in (t.get("url") or "")), None)
        if hit and hit.get("webSocketDebuggerUrl"):
            return hit["webSocketDebuggerUrl"]
    return pages[0].get("webSocketDebuggerUrl") or ""


def tb_verify_start():
    """打开(或复用)浏览器窗口到验证页, 交给用户拖滑块。"""
    global _CDP_PROC, _CDP_PORT
    url = _tb_verify_url()
    # 先试着接回上一次的窗口(应用重启后 _CDP_PORT 是 0, 但窗口还在桌面上, 端口落在
    # tb_browser.json 里)。接得上就别再开一个: 同一个 profile 再开一个会被 Chrome 的
    # 单例机制挡回来, 新进程立刻退出, 用户看到的就是"点验证没反应"。
    _tb_br_restore()
    ws = _cdp_page_ws()
    if ws:
        try:
            _cdp_eval(ws, "location.href=%s" % json.dumps(url))
            _TB_BR.update(ws=ws, ts=int(time.time()))
            _tb_br_save()
            _CDP_VERIFY.update(ts=int(time.time()), status="waiting", url=url,
                               message="请在浏览器窗口里拖动滑块完成验证")
            return {"ok": True, "reused": True, "url": url,
                    "message": "已在浏览器窗口打开验证页, 请拖动滑块完成验证"}
        except Exception:
            pass
    proc, port, err = _cdp_launch(url)
    if err:
        _CDP_PROC = proc
        _CDP_VERIFY.update(ts=int(time.time()), status="error", message=err, url=url)
        return {"ok": False, "error": err}
    _CDP_PROC, _CDP_PORT = proc, port
    _TB_BR.update(ws=_cdp_page_ws(), ts=int(time.time()))
    _tb_br_save()
    _CDP_VERIFY.update(ts=int(time.time()), status="waiting", url=url,
                       message="请在浏览器窗口里拖动滑块完成验证")
    return {"ok": True, "url": url, "message": "验证窗口已打开, 请拖动滑块完成验证"}


def tb_verify_poll():
    """轮询: 浏览器里一旦出现可用的 baidu cookie, 就用它真打一次列表页 ——
    过得去才写进 tb_verify_cookie、清掉 403 冷却。窗口**不关**: 之后的抓取走它。"""
    global _CDP_VERIFY, _tb_cooldown_until, _tb_retry_at
    # 窗口活没活, 问调试端口, 不看 Popen: 应用一重启 Popen 就没了, 而桌面上那个
    # 已经拖着滑块的窗口还在 —— 旧代码在这里直接判 closed, 前端随即收手。
    _tb_br_restore()
    if not _cdp_alive() and not (_CDP_PROC and _CDP_PROC.poll() is None):
        _tb_br_forget()
        _CDP_VERIFY.update(status="closed", message="验证窗口已关闭")
        return dict(_CDP_VERIFY)
    if time.time() - (_CDP_VERIFY.get("ts") or 0) > 600:
        # 超时只标状态, 不再杀窗口: 用户可能正拖着滑块, 关掉只会更让人摸不着头脑;
        # 窗口留着, 再点一次"验证"会直接复用。
        _CDP_VERIFY.update(status="expired", message="验证超时(10分钟), 请重新点验证")
        return dict(_CDP_VERIFY)
    ws = _cdp_page_ws()
    if not ws:
        return dict(_CDP_VERIFY, status="waiting", message="等待验证页加载…")
    # 先在本地看一眼页面还在不在验证页上 —— 滑块没拖完就每 2 秒去敲一次百度,
    # 等于替 WAF 加压, 也容易把已经判过的会话又惹毛。这里只在页面离开验证页后才探。
    try:
        txt = _cdp_eval(ws, "(document.body ? document.body.innerText : '').slice(0,600)") or ""
    except Exception:
        txt = ""
    if "安全验证" in txt or "滑块" in txt or "拖动" in txt:
        return dict(_CDP_VERIFY, status="waiting",
                    message="窗口里还停着验证页 —— 请拖动滑块, 松手后会自动继续")
    try:
        cookies = _cdp_get_cookies(ws) or []
    except Exception:
        cookies = []
    got = {}
    for c in cookies:
        dom = c.get("domain") or ""
        if "baidu.com" in dom and c.get("name"):
            got[c["name"]] = c["value"]
    if not got.get("BAIDUID"):
        return dict(_CDP_VERIFY, status="waiting", message="等待验证页加载…")
    # 两次真探测之间至少隔 5 秒
    if time.time() - (_CDP_VERIFY.get("probe_ts") or 0) < 5:
        return dict(_CDP_VERIFY, status="waiting", message="正在确认验证结果…")
    _CDP_VERIFY["probe_ts"] = time.time()
    cookie_str = "; ".join("%s=%s" % (k, v) for k, v in got.items())
    ok, _url, code = _tb_verify_probe(cookie_str)
    if ok:
        sset("tb_verify_cookie", cookie_str)
        _tb_cooldown_until = 0
        _tb_retry_at = 0
        # 窗口**别关**: 接下去的贴吧请求就走这个窗口 —— 它才是被百度认的会话。
        _TB_BR.update(ws=ws, ok=True, ts=int(time.time()))
        _tb_br_save()          # 落盘: 应用重启后还能接回这个已验证的窗口
        try:
            kick_build(quick=True, pending=True)
        except Exception:
            pass
        try:   # 闲置 30 分钟才关窗(旧实现 3 分钟就关, 之后所有请求又回落 403)
            threading.Timer(60, _tb_br_idle_watch).start()
        except Exception:
            pass
        _CDP_VERIFY.update(status="ok",
                           message="验证通过, 正在重新抓取贴吧(窗口先别关, 抓完会自动关)")
        _tb_log("验证通过: 浏览器会话可用, 贴吧请求改走验证窗口")
        return {"status": "ok", "message": "验证通过, 正在重新抓取贴吧(窗口先别关, 抓完会自动关)",
                "cookie_saved": True, "http": code}
    _tb_log("验证探测: 还没通过(HTTP %s)" % code)
    return dict(_CDP_VERIFY, status="waiting", http=code,
                message="还没通过(HTTP %s) —— 请在窗口里把滑块做完" % code)


def tb_verify_stop():
    r = tb_login_stop()
    _CDP_VERIFY.update(status="stopped", message="验证窗口已关闭")
    return r


def _cdp_set_cookie(ws_url, name, value, url):
    """CDP Network.setCookie: 向指定URL域写入cookie。"""
    try:
        s = socket.create_connection(_cdp_addr(ws_url), timeout=5)
    except Exception:
        return False
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" %
               (_cdp_path(ws_url), *_cdp_addr(ws_url), key))
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                return False
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n")[0]:
            return False
        _cdp_ws_send(s, {"id": 1, "method": "Network.setCookie", "params": {
            "name": name, "value": value, "url": url, "path": "/"}})
        s.settimeout(6)
        while True:
            try:
                m = _cdp_ws_recv(s)
            except Exception:
                break
            if not m:
                break
            if m.get("id") == 1:
                return bool((m.get("result") or {}).get("success"))
        return False
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _cdp_eval(ws_url, expression, timeout=8):
    """CDP Runtime.evaluate: 同一连接发 Runtime.evaluate 命令(自动重试3次, 导航期WS偶发断)。
    返回解析后的 result.value(dict/list/str) 或 None。"""
    for attempt in range(3):
        try:
            s = socket.create_connection(_cdp_addr(ws_url), timeout=5)
        except Exception:
            time.sleep(0.8)
            continue
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" %
                   (_cdp_path(ws_url), *_cdp_addr(ws_url), key))
            s.sendall(req.encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if b" 101 " not in resp.split(b"\r\n")[0]:
                s.close()
                time.sleep(0.8)
                continue
            _cdp_ws_send(s, {"id": 1, "method": "Runtime.evaluate", "params": {
                "expression": expression, "returnByValue": True, "awaitPromise": True}})
            deadline = time.time() + timeout
            s.settimeout(timeout)
            while time.time() < deadline:
                msg = _cdp_ws_recv(s)
                if not msg:
                    break
                if msg.get("id") == 1:
                    r = (msg.get("result") or {}).get("result") or {}
                    return r.get("value")
            s.close()
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            time.sleep(0.8)
            continue
        try:
            s.close()
        except Exception:
            pass
        break
    return None


# 贴吧列表页: 登录态下 PageData.user(user_name/portrait) 是最稳定来源(服务端渲染)
TB_USERINFO_JS = """
(function(){
  try {
    if (window.PageData && window.PageData.user) {
      var u = window.PageData.user;
      var name = (u.user_name || "").trim();
      var pt = u.portrait || "";
      var avatar = "";
      if (pt && pt !== "0" && pt !== "None" && pt.indexOf("/") >= 0) {
        avatar = pt.indexOf("//") === 0 ? "https:" + pt : pt;
      } else if (pt && pt !== "0" && pt !== "None") {
        // 贴吧portrait规则: https://tb1.bdstatic.com/tb/cms/portrait/<hash>
        avatar = "https://tb1.bdstatic.com/tb/cms/portrait/" + pt;
      }
      return {name: name, avatar: avatar, portrait: pt, is_login: !!u.is_login};
    }
    return {name: "", avatar: "", is_login: false};
  } catch (e) { return {name: "", avatar: "", err: String(e).slice(0, 80)}; }
})()
"""


# 贴吧首页登录态: 昵称/头像 DOM 探测(多选择器兜底; 头像排除 header/banner/logo 等装饰图)
TB_PROFILE_JS = """
(function(){
  try {
    var out = {name: "", avatar: ""};
    // ① 优先: 新版页面 img.user-avatar / img.avatar-img(实测 gss0.bdstatic.com/portrait/item/...)
    var aImg = document.querySelector('img.user-avatar') || document.querySelector('img.avatar-img');
    if (aImg) out.avatar = aImg.src || "";
    // ② PageData.user(旧版列表页服务端渲染)
    if (!out.avatar && window.PageData && window.PageData.user && window.PageData.user.user_name) {
      var u = window.PageData.user;
      out.name = (u.user_name || "").trim();
      var pt = u.portrait || "";
      if (pt && pt !== "0" && pt !== "None") {
        if (pt.indexOf("/") >= 0) {
          out.avatar = pt.indexOf("//") === 0 ? "https:" + pt : pt;
        } else {
          out.avatar = "https://tb1.bdstatic.com/tb/cms/portrait/" + pt;
        }
      }
    }
    // ③ 昵称: home/main链接 / 用户区文本
    if (!out.name) {
      var link = document.querySelector('a[href*="home/main"][href*="un="]');
      if (link) out.name = (link.textContent || "").trim();
    }
    if (!out.name) {
      var names = document.querySelectorAll('.u_name, .user-name, #tuser a, .userbar a[title]');
      for (var i = 0; i < names.length; i++) {
        var t = (names[i].textContent || "").trim();
        if (t && t.length < 30 && !/首页|主页|我的|登录|注册|关注/.test(t)) { out.name = t; break; }
      }
    }
    // ④ 兜底: 任何 portrait/avatar 图(排除装饰)
    if (!out.avatar) {
      var BAD = /header|banner|logo|topic|default|static|icon|cms|cap|puzzle|emotion|thumb/i;
      var imgs = document.querySelectorAll('img');
      for (var j = 0; j < imgs.length; j++) {
        var src = imgs[j].getAttribute("src") || "";
        if (src.indexOf("data:") === 0) continue;
        if (/portrait|avatar/.test(src) && !BAD.test(src)) { out.avatar = src; break; }
      }
    }
    return out;
  } catch (e) { return {name: "", avatar: "", err: String(e).slice(0, 80)}; }
})()
"""


def tb_profile_from_page(ws_url):
    """导航贴吧列表页(服务端渲染, 含 PageData.user) → 抓昵称/头像。
    重定向环/错误页/滑块验证直接放弃(不阻塞登录)。失败返回 ("", "")。"""
    try:
        _cdp_eval(ws_url, "location.href='https://tieba.baidu.com/f?kw=steam&ie=utf-8&pn=0'; true")
        # 等页面就绪(最多6次×2.5s); chrome-error/wappass重定向/passMod滑块→提前放弃
        for _ in range(6):
            time.sleep(2.5)
            href = _cdp_eval(ws_url, "location.href")
            if href is None:
                continue
            if "chromewebdata" in str(href):
                return "", ""   # 错误页
            has_cap = _cdp_eval(ws_url,
                                "document.querySelector('.passMod_puzzle, .passMod, #passport-verify') ? true : false")
            if has_cap:
                return "", ""   # 滑块验证(非自然访问), 放弃头像
            ready = _cdp_eval(ws_url, "document.readyState === 'complete'")
            if ready:
                probe = _cdp_eval(ws_url, TB_USERINFO_JS)
                if isinstance(probe, dict) and probe.get("name"):
                    return str(probe.get("name") or "").strip()[:40], \
                        str(probe.get("avatar") or "").strip()[:300]
                # PageData 不可用 → 退回 DOM 探测
                probe2 = _cdp_eval(ws_url, TB_PROFILE_JS)
                if isinstance(probe2, dict):
                    if probe2.get("name") or probe2.get("avatar"):
                        return str(probe2.get("name") or "").strip()[:40], \
                            str(probe2.get("avatar") or "").strip()[:300]
                    if probe2.get("name") is not None:   # 页面已就绪但无用户信息(未登录)
                        return "", ""
    except Exception:
        pass
    return "", ""


def tb_browser_grab(ws_url):
    """浏览器辅助采集: 读取登录窗口当前贴吧列表页的DOM, 解析为条目。
    新版贴吧为React渲染, 直接读DOM(thread-card-wrapper结构)。
    等待安全验证滑块通过 + 帖子卡片渲染(最长30s)。"""
    try:
        href = _cdp_eval(ws_url, "location.href") or ""
        if "tieba.baidu.com/f" not in href:
            return [], "", "当前页面不是贴吧列表页(请打开 tieba.baidu.com/f?kw=吧名)"
        m = re.search(r"kw=([^&]+)", href)
        fname = urllib.parse.unquote(m.group(1)) if m else ""
        # 等安全验证滑块通过 + 卡片渲染(最长30s, 每2.5s检查)
        for _ in range(12):
            has_cap = _cdp_eval(ws_url,
                                "document.querySelector('.passMod_puzzle, .passMod, #passport-verify') ? true : false")
            cards = _cdp_eval(ws_url, "document.querySelectorAll('.thread-card-wrapper').length")
            if cards and int(cards) > 0:
                break
            time.sleep(2.5)
        items = _tb_html_to_items_dom(ws_url, fname or "未知名")
        return items, fname, ""
    except Exception as e:
        return [], "", str(e)[:80]


def _tb_html_to_items_dom(ws_url, fname):
    """新版贴吧列表(React): 从DOM提取帖数据(thread-card-wrapper结构)。"""
    data = _cdp_eval(ws_url, """
    (function(){
      var out = [];
      var cards = document.querySelectorAll('.thread-card-wrapper, .top-thread-card');
      for (var i = 0; i < cards.length && out.length < 50; i++) {
        var c = cards[i];
        // 置顶/广告剔除
        if (c.closest('.top-thread-card') || c.closest('.ad-card')) continue;
        var a = c.querySelector('a.thread-content-link, a[href*="/p/"]');
        if (!a) continue;
        var tm = (a.getAttribute('href') || '').match(/\\/p\\/(\\d+)/);
        if (!tm) continue;
        var titleEl = c.querySelector('.thread-title, a.thread-content-link');
        var title = titleEl ? (titleEl.getAttribute('title') || titleEl.textContent || '').trim() : '';
        if (!title) continue;
        var img = c.querySelector('.image-card-with-fallback img, img[src*="tiebapic"], img[src*="forum"]');
        var rep = c.querySelector('.reply-num, .thread-reply, [class*="reply"]');
        var au = c.querySelector('.author, [class*="author"]');
        var isrc = img ? (img.getAttribute('src') || '') : '';
        // 缩略图→大图: wh%3D200%2C90%3B(200×90) → w%3D580%3B(580宽)
        if (isrc.indexOf('wh%3D200%2C90%3B') >= 0) {
          isrc = isrc.replace('wh%3D200%2C90%3B', 'w%3D580%3B');
        }
        out.push({
          tid: tm[1], title: title.slice(0, 120),
          img: isrc,
          reply: rep ? (parseInt((rep.textContent || '').replace(/[^0-9]/g, '')) || 0) : 0,
          author: au ? au.textContent.trim().slice(0, 30) : ''
        });
      }
      return JSON.stringify(out);
    })()
    """)
    if not data:
        return []
    try:
        rows = json.loads(data) if isinstance(data, str) else (data or [])
    except Exception:
        return []
    items = []
    for r in rows or []:
        tid, title = r.get("tid", ""), (r.get("title") or "").strip()
        if not tid or not title:
            continue
        url = "https://tieba.baidu.com/p/%s" % tid
        img = r.get("img") or ""
        items.append({
            "key": url, "platform": "贴吧", "title": title[:120],
            "author": (r.get("author") or "")[:30],
            "desc": "", "cover": img, "covers": [img] if img else [],
            "views": 0, "likes": 0, "danmaku": None,
            "reply": int(r.get("reply") or 0), "duration": 0,
            "pubdate": 0, "url": url,
            "tname": fname, "area": "", "tag": "",
            "gid": "tieba", "src": "贴吧·%s" % fname,
        })
    return items


def _cdp_get_cookies(ws_url):
    """极简 WebSocket 客户端: 发 Network.getAllCookies, 收响应。纯标准库。"""
    try:
        s = socket.create_connection(_cdp_addr(ws_url), timeout=5)
    except Exception:
        return None
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" %
               (_cdp_path(ws_url), *_cdp_addr(ws_url), key))
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                return None
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n")[0]:
            return None
        # 发送 enable + getAllCookies
        _cdp_ws_send(s, {"id": 1, "method": "Network.enable", "params": {}})
        _cdp_ws_send(s, {"id": 2, "method": "Network.getAllCookies", "params": {}})
        deadline = time.time() + 8
        s.settimeout(8)
        while time.time() < deadline:
            msg = _cdp_ws_recv(s)
            if not msg:
                continue
            if msg.get("id") == 2:
                return (msg.get("result") or {}).get("cookies") or []
        return None
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def _cdp_addr(ws_url):
    u = urllib.parse.urlparse(ws_url)
    return (u.hostname or "127.0.0.1", u.port or 80)


def _cdp_path(ws_url):
    return urllib.parse.urlparse(ws_url).path or "/"


def _cdp_ws_send(s, obj):
    payload = json.dumps(obj).encode("utf-8")
    header = bytearray([0x81])   # FIN + text
    n = len(payload)
    mask = os.urandom(4)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(header) + masked)


def _cdp_ws_recv(s):
    try:
        h = s.recv(2)
        if len(h) < 2:
            return None
        opcode = h[0] & 0x0F
        length = h[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", s.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", s.recv(8))[0]
        data = b""
        while len(data) < length:
            chunk = s.recv(length - len(data))
            if not chunk:
                return None
            data += chunk
        if opcode in (0x1, 0x2):
            return json.loads(data.decode("utf-8"))
        return None   # ping/pong/close 忽略
    except Exception:
        return None


TB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------- 小黑盒数据源
#
# 网页版API需 hkey/_time/nonce 签名(防重放) —— 算法已逆向移植(纯标准库):
#   nonce = md5(时间戳+随机).upper() ; hkey = 字符集映射交错MD5前缀 + Km校验和
# 帖子流: /bbs/app/feeds (pull/offset/dw 分页) ; 搜索: /bbs/app/api/search/found

XHH_CHARSET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _xhh_vm(e):
    return ((255 & ((e << 1) ^ 27)) if (e & 128) else (e << 1)) & 255


def _xhh_qm(e):
    return _xhh_vm(e) ^ e


def _xhh_dollar_m(e):
    return _xhh_qm(_xhh_vm(e))


def _xhh_ym(e):
    return _xhh_dollar_m(_xhh_qm(_xhh_vm(e)))


def _xhh_gm(e):
    return _xhh_ym(e) ^ _xhh_dollar_m(e) ^ _xhh_qm(e)


def _xhh_km(e_arr):
    e = list(e_arr)
    t = [0, 0, 0, 0]
    t[0] = _xhh_gm(e[0]) ^ _xhh_ym(e[1]) ^ _xhh_dollar_m(e[2]) ^ _xhh_qm(e[3])
    t[1] = _xhh_qm(e[0]) ^ _xhh_gm(e[1]) ^ _xhh_ym(e[2]) ^ _xhh_dollar_m(e[3])
    t[2] = _xhh_dollar_m(e[0]) ^ _xhh_qm(e[1]) ^ _xhh_gm(e[2]) ^ _xhh_ym(e[3])
    t[3] = _xhh_ym(e[0]) ^ _xhh_dollar_m(e[1]) ^ _xhh_qm(e[2]) ^ _xhh_gm(e[3])
    e[0], e[1], e[2], e[3] = t[0], t[1], t[2], t[3]
    return e


def _xhh_av(s, n):
    i = XHH_CHARSET[:n]
    return "".join(i[ord(c) % len(i)] for c in s)


def _xhh_sv(s):
    return "".join(XHH_CHARSET[ord(c) % len(XHH_CHARSET)] for c in s)


def xhh_hkey(url_path, timestamp, nonce):
    """hkey 生成(与网页版一致, 实测对照抓包ZYP71前缀+校验和全对)。"""
    parts = [p for p in url_path.split("/") if p]
    np = "/" + "/".join(parts) + "/"
    c1 = _xhh_av(str(timestamp), -2)
    c2 = _xhh_sv(np)
    c3 = _xhh_sv(nonce)
    comps = [c1, c2, c3]
    ml = max(len(c) for c in comps)
    it = ""
    for k in range(ml):
        for c in comps:
            if k < len(c):
                it += c[k]
    md = hashlib.md5(it[:20].encode()).hexdigest()
    pref = _xhh_av(md[:5], -4)
    km = _xhh_km([ord(c) for c in md[-6:]])
    return pref + str(sum(km) % 100).zfill(2)


def xhh_nonce():
    return hashlib.md5((str(int(time.time() * 1000)) + str(uuid.uuid4())).encode()).hexdigest().upper()


def xhh_token():
    """网页版tokenid: B + 32位随机hex(urlsafe), 可本地生成(实测接受)。"""
    return "B" + base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")


def xhh_tok():
    """取/建持久化设备tokenid(x_xhh_tokenid)。所有小黑盒请求复用同一tokenid。"""
    tok = sget("xhh_qr_tokenid")
    if not tok:
        tok = xhh_token()
        sset("xhh_qr_tokenid", tok)
    return tok


def xhh_parse_login(res):
    """从登录接口 result 提取 (pkey, heybox_id, nickname, avatar)。
    实测网页端扫码成功返回的 result 含 nickname/avatar/account_detail.userid/heyboxid,
    但无 pkey(会话不由 pkey 承载)。字段: result.heyboxid | account_detail.userid;
    昵称 profile.nickname | account_detail.username | result.nickname;
    头像 profile.avatar | account_detail.avatar | account_detail.avartar(官方typo) | result.avatar。"""
    res = res or {}
    prof = res.get("profile") or {}
    ad = res.get("account_detail") or {}
    pkey = res.get("pkey") or ""
    heybox_id = (res.get("heybox_id") or res.get("heyboxid")
                 or prof.get("heybox_id") or ad.get("userid") or "")
    nickname = prof.get("nickname") or ad.get("username") or res.get("nickname") or ""
    avatar = (prof.get("avatar") or ad.get("avatar")
              or ad.get("avartar") or res.get("avatar") or "")
    return pkey, heybox_id, nickname, avatar


XHH_BASE = {
    "os_type": "web", "app": "heybox", "client_type": "web",
    "version": "999.0.4", "web_version": "2.5", "x_client_type": "web",
    "x_app": "heybox_website", "heybox_id": "", "x_os_type": "Windows",
    "device_info": "Chrome", "device_id": "2c2fef8385ccef915e3b3caf94e3aa06",
}


def xhh_get(path, extra=None, token=""):
    """签名GET小黑盒API。返回 (json_dict, err)。
    token: x_xhh_tokenid(网页版设备标识), 缺省用持久化tokenid。
    登录后自动附 pkey/heybox_id(网页版真实会话凭证)。"""
    # v0.4.2: 小黑盒所有读写共用一道节奏闸 —— 补池/消息/登录同时打到同一个域名时,
    # 站方看到的是一秒多下, 正是风控样本。
    _net_pace("xhh-api", 0.8)
    t = int(time.time())
    n = xhh_nonce()
    hk = xhh_hkey(path, t, n)
    params = dict(XHH_BASE)
    params.update(extra or {})
    params.update({"hkey": hk, "_time": t, "nonce": n})
    params["x_xhh_tokenid"] = token or xhh_tok()
    pk = sget("xhh_pkey")
    hid = sget("xhh_heybox_id")
    if pk:
        params["pkey"] = pk
    if hid:
        params["heybox_id"] = str(hid)
        params["user_heybox_id"] = str(hid)
    url = "https://api.xiaoheihe.cn" + path + "?" + urllib.parse.urlencode(params)
    hdr = {"User-Agent": HEADERS["User-Agent"], "Referer": "https://www.xiaoheihe.cn/",
           "Accept": "application/json, text/plain, */*"}
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore")), None
    except Exception as e:
        return None, str(e)[:100]


def xhh_unread_messages():
    """读取小黑盒评论消息未读数；上游未明确提供未读字段时返回None。"""
    if not sget("xhh_heybox_id"):
        return None
    d, err = xhh_get("/bbs/app/user/message", {
        "list_type": "0", "offset": "0", "limit": "50", "no_more": "false"})
    if err or not d or d.get("status") != "ok":
        return None
    # 当前接口将评论未读总数直接放在响应顶层，空 result 列表仍是有效响应。
    for key in ("message_unread_num", "unread_count", "unread_num", "unread", "new_count"):
        value = d.get(key)
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str) and value.isdigit():
            return max(0, int(value))
    result = d.get("result")
    if isinstance(result, dict):
        for key in ("unread_count", "unread_num", "unread", "new_count"):
            value = result.get(key)
            if isinstance(value, (int, float)):
                return max(0, int(value))
        messages = next((value for value in result.values() if isinstance(value, list)), None)
    elif isinstance(result, list):
        messages = result
    else:
        messages = None
    if messages is None:
        return None
    markers = ("is_read", "read", "unread", "is_unread", "has_read")
    items = [item for item in messages if isinstance(item, dict)]
    if not items or not all(any(marker in item for marker in markers) for item in items):
        return None
    unread = 0
    for item in items:
        if item.get("unread") is True or item.get("is_unread") is True or item.get("is_read") is False or item.get("read") is False or item.get("has_read") is False:
            unread += 1
    return unread


def xhh_feed_items(offset=0, limit=20, pull="0"):
    """小黑盒社区帖子流 → 统一item格式列表。"""
    d, err = xhh_get("/bbs/app/feeds", {"pull": pull, "offset": str(offset), "dw": "844"})
    if err or not d or d.get("status") != "ok":
        return [], err or "接口失败"
    links = (d.get("result") or {}).get("links") or []
    items = []
    for lk in links[:limit]:
        if not lk.get("linkid"):
            continue
        url = ""
        # 帖子真实URL = /app/bbs/link/<link_id>(hex); link_id 取自 share_url 查询参数
        su = lk.get("share_url") or ""
        link_id = (urllib.parse.parse_qs(urllib.parse.urlparse(su).query).get("link_id", [""])[0]
                   or lk.get("link_id") or "")
        if link_id:
            url = "https://www.xiaoheihe.cn/app/bbs/link/%s" % link_id
        else:
            url = "https://www.xiaoheihe.cn/app/detail/%s" % lk.get("linkid", "")
        usr = lk.get("user") or {}
        imgs = [u for u in (lk.get("imgs") or []) if u][:2]
        comm = [t.get("name", "") for t in (lk.get("topics") or []) if t.get("name")]
        htags = [h.get("name", "") for h in (lk.get("hashtags") or []) if h.get("name")]
        tags = comm + htags
        # 点击量: 帖子曝光量藏在 h_src(base64) 的 app_feeds_exposure_count 里, 解码尽力取
        views, views_known = 0, False
        hs = lk.get("h_src")
        if hs:
            try:
                dec = base64.b64decode(hs).decode("utf-8", "ignore")
                exp = re.search(r"app_feeds_exposure_count__(\d+)", dec)
                if exp:
                    views = int(exp.group(1))
                    views_known = True
            except Exception:
                pass
        items.append({
            "key": url, "platform": "小黑盒",
            "title": (lk.get("title") or (lk.get("description") or "")[:30])[:120],
            "author": (usr.get("username") or "")[:30],
            "desc": re.sub(r"\[\w+\]|\[cube_[^\]]*\]", "", lk.get("description") or "")[:500],
            "cover": imgs[0] if imgs else "",
            "covers": imgs,
            "views": views, "views_known": views_known, "likes": int(lk.get("link_award_num") or lk.get("up") or 0),
            "danmaku": None, "reply": int(lk.get("comment_num") or 0),
            "duration": 0, "duration_known": False, "pubdate": int(lk.get("create_at") or 0),
            "xcomm": "、".join(comm)[:200],   # 社区/板块(topics), 供小黑盒社区筛选
            "xtopic": "、".join(htags)[:200],  # 话题(hashtags), 供小黑盒话题筛选
            "url": url, "tname": "、".join(tags[:2])[:20], "area": "", "tag": ",".join(tags)[:100],
            "xlid": str(lk.get("linkid") or ""),
            "ltag": str(lk.get("link_tag") or ""),   # 帖子类型标记, 唤起App时用(heybox://link)
            "gid": "xhh", "src": "小黑盒·%s" % (tags[0] if tags else "社区"),
        })
    return items, ""


_xhh_cursor = 0   # 小黑盒帖流偏移游标
_XHH_POOL_MIN_GAP = 90   # 后台补池最小间隔(秒): 和贴吧同因, 不跟着每轮构建反复打
_xhh_pool_last_ts = 0.0
_xhh_fetch_total = 0   # 小黑盒累计条目(状态面板用)
_xhh_last_fetch = 0


def xhh_post(path, query, body=None):
    """签名POST小黑盒API: query+body 均带 x_xhh_tokenid。返回 (json_dict, err)。"""
    _net_pace("xhh-api", 0.8)
    t = int(time.time())
    n = xhh_nonce()
    hk = xhh_hkey(path, t, n)
    params = dict(XHH_BASE)
    params.update(query or {})
    params.update({"hkey": hk, "_time": t, "nonce": n})
    tok = xhh_tok()
    params["x_xhh_tokenid"] = tok
    pk = sget("xhh_pkey")
    hid = sget("xhh_heybox_id")
    if pk:
        params["pkey"] = pk
    if hid:
        params["heybox_id"] = str(hid)
        params["user_heybox_id"] = str(hid)
    url = "https://api.xiaoheihe.cn" + path + "?" + urllib.parse.urlencode(params)
    b = dict(body or {})
    b["x_xhh_tokenid"] = tok
    hdr = {"User-Agent": HEADERS["User-Agent"], "Referer": "https://www.xiaoheihe.cn/",
           "Accept": "application/json, text/plain, */*",
           "Content-Type": "application/x-www-form-urlencoded"}
    try:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(b).encode(), headers=hdr)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore")), None
    except Exception as e:
        return None, str(e)[:100]


def fetch_xhh_pool(quick=False):
    """小黑盒采集: 帖流翻页(每轮偏移+limit, 覆盖更多)。返回 (items, errors)。
    full: 每轮抓2页(offset + offset+30, 各30条), 单轮最多~60条, 满足高权重下小黑盒的供给。"""
    global _xhh_cursor, _xhh_fetch_total, _xhh_last_fetch, _xhh_pool_last_ts
    if xhh_weight() <= 0:
        return [], []   # 占比=0: 后台完全停抓(显式搜索不受影响)
    now_ts = time.time()
    if now_ts - _xhh_pool_last_ts < _XHH_POOL_MIN_GAP * random.uniform(1.0, 1.4):
        return [], []   # 距上次抓取太近: 这次不抓(不是失败, 不写 xhh_error)
    _xhh_pool_last_ts = now_ts
    items, errs = [], []
    if quick:
        its, err = xhh_feed_items(offset=_xhh_cursor, limit=10)
        if its:
            items = its
            _xhh_cursor = (_xhh_cursor + 10) % 400
        elif err:
            errs.append(err[:200])
    else:
        limit = 30
        # v0.4.2: 由"4 页并发"改成"2 页顺序" —— 并发齐发是小黑盒风控样本里最显眼的形状,
        # 而 2 页 × 30 条对信息流已经够用; xhh_get 里的 _net_pace 再把两页拉开。
        offsets = [(_xhh_cursor + k * limit) % 400 for k in range(2)]
        for offset in offsets:
            its, err = xhh_feed_items(offset=offset, limit=limit)
            if its:
                seen = {x["key"] for x in items}
                items += [x for x in its if x["key"] not in seen]
            elif err and not items:
                errs.append(err[:200])
        _xhh_cursor = (_xhh_cursor + limit) % 400
    if not items:
        if errs:
            sset("xhh_error", errs[0])
        return [], [errs[0]] if errs else ["无内容"]
    _xhh_fetch_total += len(items)
    _xhh_last_fetch = int(time.time())
    sset("xhh_last_fetch", _xhh_last_fetch)
    sset("xhh_error", "")
    return items, []


def fetch_pool(quick=False):
    """轮换取源 + 页游标 + 相关推荐链 + 搜索词引擎 + 随机采样 + 贴吧(网络请求全部并行)。
    quick=True: 只取2个源(快速开屏, 后续后台补全)。"""
    global _src_cursor, _page_cursor, _kw_cursor, _kw_page, _fetch_count, _pref_cursor, _pref_page
    errors, seen, items, ok, names = [], {}, [], 0, []
    enabled = enabled_source_ids()
    avail = [s for s in BILI_SOURCES if s[1] in enabled] or BILI_SOURCES
    n = len(avail)
    per = 2 if quick else SOURCES_PER_FETCH
    if n <= per:                        # 开启的分区很少时, 全部取
        picked, _src_cursor = avail[:], 0
    else:                                # 轮转全量列表 → 低产源裁剪(产出率自学习) → 取 per 个
        rotated = [avail[(_src_cursor + i) % n] for i in range(n)]
        picked = _src_yield_prune(rotated, per)
        _src_cursor = (_src_cursor + per) % n

    # --- 榜单源: 并行抓取(严肃模式跳过: 榜单内容非教程类, 抓了也全被滤掉) ---
    ser_mode = sget("mode") == "严肃"
    tb_future = xhh_future = fol_future = live_future = local_serious_future = None
    serious_search_futures = []
    quick_search_futures = []
    if ser_mode and not quick:
        ser_terms = serious_kw_pool(enabled)[:10]
        # B站严肃关键词搜索 + 允许吧内搜索统一 fan-out, 结果随后统一去重/门禁
        for i, kw in enumerate(ser_terms):
            serious_search_futures.append(("bili", kw, _SERIOUS_X.submit(search_items, kw, 1)))
            if i < 4:
                serious_search_futures.append(("bili", kw, _SERIOUS_X.submit(search_items, kw, 2)))
        if tb_weight() > 0:
            for fname in tb_users():
                for kw in ser_terms[:4]:
                    serious_search_futures.append(("tb", fname, _SERIOUS_X.submit(tb_search_items, kw, 20, fname)))
            tb_future = _SERIOUS_X.submit(fetch_tieba_pool, False)
        if xhh_weight() > 0:
            xhh_future = _SERIOUS_X.submit(fetch_xhh_pool, False)
        local_serious_future = _SERIOUS_X.submit(_local_pool_hits, ser_terms[:8], set(), 160)
        if sget("bili_follow_emph") == "1":
            fol_future = _SERIOUS_X.submit(fetch_following_videos)
    elif quick:
        if tb_weight() > 0:
            tb_future = _FX.submit(fetch_tieba_pool, True)
        if xhh_weight() > 0:
            xhh_future = _FX.submit(fetch_xhh_pool, True)
        if int(sget("live_weight") or 25) > 0:
            live_future = _FX.submit(fetch_live_pool, True)
        # 快速开屏搜索词与榜单阶段同时开跑(此前先等榜单完成再搜, 两段串行拖慢首屏)
        kws = []
        if ser_mode:
            kws = serious_kw_pool(enabled)[:6]
        for gid, words in WORD_BANK.items():
            if gid in enabled and not ser_mode:
                kws += words
        if kws:
            sample_n = min(8 if ser_mode else 2, len(kws))
            picked_kws = kws[:sample_n] if ser_mode else random.sample(kws, sample_n)
            _push = search_pushdown()
            pool = _SERIOUS_X if ser_mode else _FX
            quick_search_futures = [
                (kw, pool.submit(search_items, kw, pg,
                                 order=_push.get("order"), duration=_push.get("duration")))
                for kw in picked_kws
                for pg in ([1, 2] if ser_mode else [random.choice([1, 1, 2])])]
    else:
        # 非严肃完整构建: 与B站榜单阶段同时启动独立平台和关注投稿抓取。
        if tb_weight() > 0:
            tb_future = _FX.submit(fetch_tieba_pool, False)
        if xhh_weight() > 0:
            xhh_future = _FX.submit(fetch_xhh_pool, False)
        if int(sget("live_weight") or 25) > 0:
            live_future = _FX.submit(fetch_live_pool, False)
        if sget("bili_follow_emph") == "1":
            fol_future = _FX.submit(fetch_following_videos)
    # --- 偏好词引擎(提前并行提交): 用户偏好备注=最高优先级的主动意图 ---
    # 与榜单/贴吧/小黑盒同批发出请求; 结果在榜单消费后立即收编(见下方消费块),
    # 保证预算不被相关链/搜索词引擎挤占 —— 旧版排最末且带 len(items)<420 闸门,
    # 相关链+词引擎把池子灌满后偏好词整段被跳过(用户"偏好几乎刷不到"的根因);
    # 快速开屏也搜1词(旧版quick完全不搜偏好); 取词走洗牌袋、取页随机, 防同一批视频反复。
    # 偏好内容同样受时间窗硬约束(见 pref_age_months): 服务端能下推就下推, 不再整段豁免。
    pref_terms = _pref_search_terms(enabled, quick)
    _push0 = search_pushdown()
    _pref_cut0 = pref_age_cut()
    pref_futures = [(kw, pg, _FX.submit(search_items, kw, pg,
                                        order=_push0.get("order"),
                                        duration=_push0.get("duration"),
                                        pub_begin=_pref_cut0 or None))
                    for kw, pg in pref_terms]
    cur_gids = []
    jobs = []
    for i, (name, gid, tpl, rid, pages) in enumerate(picked if not ser_mode else []):
        page = random.randint(0, pages - 1)          # 随机页: 打破顺序轮转造成的可预期重复
        url = tpl.format(rid=rid, p=page)
        shown = name if pages == 1 else "%s·第%d页" % (name, page)
        names.append(shown)
        cur_gids.append(gid)
        jobs.append(url)
    for shown, gid, (data, err) in zip(names[len(names) - len(picked):], cur_gids,
                                       _FX.map(_safe_json, jobs)):
        if data and data.get("code") == 0 and (data.get("data") or {}).get("list"):
            for it in data["data"]["list"]:
                if it.get("bvid") and it["bvid"] not in seen:
                    seen[it["bvid"]] = 1
                    item = normalize(it)
                    item["gid"] = gid   # 记录来源分组, 沉淀池按其过滤
                    item["src"] = shown   # 混排桶标记(榜单源按页)
                    if not item_domain_ok(item, enabled):   # 分区严格校验
                        continue
                    items.append(item)
            ok += 1
        else:
            errors.append("%s: %s" % (shown, (data or {}).get("code") or err))

    # --- 偏好词引擎(消费): 紧跟榜单之后、贴吧/相关链/词引擎之前收编, 预算必有富余 ---
    # 偏好备注是用户明写的主动意图, 优先级高于分区白名单与严肃模式硬拒(仍在 planned_feed 豁免);
    # 但时间窗是用户对"新鲜度"的硬设定, 不再豁免 —— 抓回即按偏好窗预过滤, 老视频不占池子名额。
    _pref_cut = pref_age_cut()   # 0=不限
    for kw, pg, fut in pref_futures:
        try:
            res = fut.result() or []
        except Exception:
            res = []
        if len(res) > 1:
            random.shuffle(res)   # 同词同页返回结果顺序固定 → 打散, 不再每轮池顶同一批
        made = 0
        for x in res:
            bvid = (x.get("url") or "").rsplit("/", 1)[-1]
            if not bvid or bvid in seen or len(items) >= 450:
                continue
            if _pref_cut and (x.get("pubdate") or 0) and x["pubdate"] < _pref_cut:
                _funnel_bump("pref_age")   # 超偏好时间窗 → 丢弃(标准/严格档绝不放行)
                continue
            seen[bvid] = 1
            x["src"] = "偏·" + kw[:6]   # 混排桶标记(偏好词)
            x["pref_kw"] = kw           # 偏好意图标记: 下游据此加成/豁免分区与严肃硬拒
            items.append(x)
            made += 1
        if made:
            names.append("偏好·%s×%d" % (kw, made))
            _funnel_bump("pref_fetch", made)
    if pref_terms:
        _pref_cursor += 1 if quick else 2   # 严肃模式变体池仍按此游标轮换

    # --- 快速开屏: 消费与榜单同时发出的搜索词请求(见 quick 分支的提前提交) ---
    if quick:
        for kw, future in quick_search_futures:
            try:
                res = future.result() or []
            except Exception:
                res = []
            made = 0
            for x in res:
                if not item_domain_ok(x, enabled):
                    continue
                bvid = (x.get("url") or "").rsplit("/", 1)[-1]
                if bvid and bvid not in seen and len(items) < 450:
                    seen[bvid] = 1
                    x["src"] = "搜·" + kw[:6]   # 混排桶标记(按词)
                    items.append(x)
                    made += 1
            if made:
                names.append("搜索·%s×%d" % (kw, made))

    if ser_mode and not quick:
        # 严肃模式 fan-out 结果统一汇总: B站严肃词 + 每个允许吧的严肃词。
        for kind, label, future in serious_search_futures:
            try:
                result = future.result() or []
            except Exception:
                result = []
            made = 0
            for x in result:
                if kind == "tb":
                    if not tieba_bar_allowed(x) or not is_serious_ok(x):
                        continue
                    k = x.get("key") or x.get("url") or ""
                else:
                    if not is_serious_ok(x) or not item_domain_ok(x, enabled):
                        continue
                    k = (x.get("url") or "").rsplit("/", 1)[-1]
                if k and k not in seen and len(items) < 450:
                    seen[k] = 1
                    x["src"] = ("严·贴吧·" if kind == "tb" else "严·") + str(label)[:10]
                    items.append(x)
                    made += 1
            if made:
                names.append(("严肃贴吧·" if kind == "tb" else "严肃·") + str(label)[:10] + "×%d" % made)
        if local_serious_future:
            try:
                local_items = local_serious_future.result() or []
            except Exception:
                local_items = []
            made = 0
            for x in local_items:
                if x.get("platform") == "贴吧" and not tieba_bar_allowed(x):
                    continue
                if not is_serious_ok(x):
                    continue
                k = x.get("key") or x.get("url") or ""
                if k and k not in seen and len(items) < 450:
                    seen[k] = 1
                    x["src"] = "严·本地·" + str(x.get("platform") or "")
                    items.append(x)
                    made += 1
            if made:
                names.append("严肃本地×%d" % made)

    # --- 贴吧/小黑盒/关注的人: 放在 B站 相关链/搜索洪水之前抓取,
    #    避免被"相关链×300+"先灌满条目上限而整批丢弃(否则贴吧/小黑盒永远出不来)。
    #    quick 首屏也少量抓取非B站平台, 否则高权重平台只能等 full 补全后才出现。 ---
    # v0.4.2: 贴吧/小黑盒各自按权重独立门控 —— 旧版一个总的 if 会在 full 构建时
    # 绕过 0 权重继续调用 fetch_tieba_pool, 用户把比例调到 0 仍在被抓。
    if tb_weight() > 0:
        if tb_future:
            tb_items, tb_errs = tb_future.result()
        else:
            tb_items, tb_errs = fetch_tieba_pool(quick=quick)
        for x in tb_items:
            if x["key"] not in seen and len(items) < 400:
                seen[x["key"]] = 1
                items.append(x)
        if tb_items:
            names.append("贴吧×%d" % len(tb_items))
        elif tb_errs and not quick:
            errors += tb_errs

    if xhh_weight() > 0:
        if xhh_future:
            xhh_items, xhh_errs = xhh_future.result()
        else:
            xhh_items, xhh_errs = fetch_xhh_pool(quick=quick)
        for x in xhh_items:
            if x["key"] not in seen and len(items) < 400:
                seen[x["key"]] = 1
                items.append(x)
        if xhh_items:
            names.append("小黑盒×%d" % len(xhh_items))
        elif xhh_errs and not quick:
            errors += xhh_errs

    # --- B站直播源(v0.3.1.51): 关注的人正在直播 + 热门推荐, 独立平台「B站直播」进信息流 ---
    # 与贴吧/小黑盒同批消费; 权重 0 时连抓取都不做(省一次直播接口请求)。
    if int(sget("live_weight") or 25) > 0:
        if live_future:
            live_items, live_errs = live_future.result()
        else:
            live_items, live_errs = fetch_live_pool(quick=quick)
        for x in live_items:
            if x["key"] not in seen and len(items) < 400:
                seen[x["key"]] = 1
                items.append(x)
        if live_items:
            names.append("B站直播×%d" % len(live_items))
        elif live_errs and not quick:
            errors += live_errs

    if sget("bili_follow_emph") == "1":
        if not quick:
            if fol_future:
                fol_items, fol_err = fol_future.result()
            else:
                fol_items, fol_err = fetch_following_videos()
            for x in fol_items:
                if x["key"] in seen:
                    for existing in items:
                        if existing["key"] == x["key"]:
                            existing.update({"following": True, "author_mid": x.get("author_mid") or existing.get("author_mid") or 0,
                                             "src": "关注"})
                            break
                elif len(items) < 400:
                    seen[x["key"]] = 1
                    items.append(x)
            if fol_items:
                names.append("关注的人×%d" % len(fol_items))
            elif fol_err:
                errors.append("关注的人: " + fol_err)
        # quick 首屏不做同步关注列表扫描；已有UID缓存会即时标注，完整补池随后异步抓取投稿。

    # --- 相关推荐链: 种子 → 40相关 → 再开第二跳(每跳并行) ---
    # 产出率过低时整节跳过(相关链以老视频/重复居多, 常是"超时间窗"漏斗的最大来源);
    # 跳过时缓慢恢复通过率, 几轮后会重新尝试(防永久饿死)。
    if items and not quick and not ser_mode and not _src_yield_ok("related"):
        _src_yield_recover("related")
    if items and not quick and not ser_mode and _src_yield_ok("related"):
        max_age_related = int(sget("m_max_age_months") or 0)
        _rel_cut = (time.time() - max_age_related * 30.44 * 86400) if max_age_related else 0

        def _related_fresh(raw):
            # 抓回后先按发布时间预过滤, 老视频不再占池子名额与筛选预算。
            if not _rel_cut:
                return True
            return (raw.get("pubdate") or 0) >= _rel_cut

        related_n = 0
        for raw in _FX.map(fetch_related, pick_seeds(items)):
            if related_n >= 120:   # 相关链封顶, 防止单节灌满池子挤掉其他来源
                break
            for it in raw:
                if len(items) >= 450:
                    break
                if it.get("bvid") and it["bvid"] not in seen:
                    if not _related_fresh(it) or not item_domain_ok(it, enabled):   # 时间预过滤+分区白名单校验
                        continue
                    seen[it["bvid"]] = 1
                    item = normalize(it)
                    item["gid"] = "related"
                    item["tname"] = it.get("tname", "")
                    item["src"] = "相关"
                    items.append(item)
                    related_n += 1
        if related_n:
            hop2 = sorted([it for it in items if it.get("gid") == "related"],
                          key=lambda x: -x["views"])[:4]
            for raw in _FX.map(fetch_related, hop2):
                for it in raw:
                    if len(items) >= 450:
                        break
                    if it.get("bvid") and it["bvid"] not in seen:
                        if not _related_fresh(it) or not item_domain_ok(it, enabled):
                            continue
                        seen[it["bvid"]] = 1
                        item = normalize(it)
                        item["gid"] = "related"
                        item["tname"] = it.get("tname", "")
                        item["src"] = "相关"
                        items.append(item)
                        related_n += 1
            names.append("相关链×%d" % related_n)

    # --- 搜索词引擎: 词池(LRU) × 多页翻页 × 自学习淘汰(并行; 严肃模式由严肃引擎替代) ---
    if items and not quick and not ser_mode:
        ensure_kw_pool()
        kws = serious_kw_pool(enabled) if ser_mode else pick_kws(6)   # B站供给: 每轮6词(热榜跨构建重叠大, 靠搜索补新)
        _push = search_pushdown()
        jobs2 = [(kw, random.choice([1, 1, 2, 3])) for kw in kws]   # 随机页: 每词独立乱序
        for (kw, page), res in zip(jobs2, _FX.map(lambda a: search_items(a[0], a[1], order=_push.get("order"), duration=_push.get("duration")), jobs2)):
            keys = [x["key"] for x in res]
            if keys:
                ph = ",".join("?" * len(keys))
                known = db().execute("SELECT COUNT(*) FROM items WHERE key IN (%s)" % ph,
                                     tuple(keys)).fetchone()[0]
                new_n = max(0, len(keys) - known)
                with _db_lock:
                    db().execute("UPDATE kw SET last_used=?, new_cnt=new_cnt+?, ok_cnt=ok_cnt+1 WHERE kw=?",
                                 (int(time.time()), new_n, kw))
                    if new_n == 0:   # 连续不出新内容的词淘汰(自学习)
                        row = db().execute("SELECT ok_cnt FROM kw WHERE kw=?", (kw,)).fetchone()
                        if row and row[0] >= 3:
                            db().execute("DELETE FROM kw WHERE kw=?", (kw,))
                    db().commit()
            made = 0
            for x in res:
                if not item_domain_ok(x, enabled):   # 分区白名单(严格)
                    continue
                bvid = (x.get("url") or "").rsplit("/", 1)[-1]
                if bvid and bvid not in seen and len(items) < 450:
                    seen[bvid] = 1
                    items.append(x)
                    made += 1
            if made:
                names.append("搜索·%s×%d" % (kw, made))

    # --- 严肃模式搜索已在函数开头通过 _SERIOUS_X fan-out 并行完成 ---
    if False and not quick and len(items) < 420 and ser_mode:
        terms = serious_kw_pool(enabled)[:10]
        jobs_ser = []
        for i, kw in enumerate(terms):
            jobs_ser.append((kw, 1))
            if i < 4:
                jobs_ser.append((kw, 2))
        for (kw, page), res in zip(jobs_ser, _FX.map(lambda a: search_items(a[0], a[1]), jobs_ser)):
            made = 0
            for x in res:
                if not is_serious_ok(x):
                    continue
                if not item_domain_ok(x, enabled):
                    continue
                bvid = (x.get("url") or "").rsplit("/", 1)[-1]
                if bvid and bvid not in seen and len(items) < 450:
                    seen[bvid] = 1
                    x["src"] = "严·" + kw[:6]
                    items.append(x)
                    made += 1
            if made:
                names.append("严肃·%s×%d" % (kw, made))

    # --- 随机采样: 分区专属词库 × 随机深页 ---
    if not quick and len(items) < 420 and not ser_mode:
        for _ in range(2):
            allw = serious_kw_pool(enabled) if ser_mode else random_word_candidates(enabled)
            candidates = [w for w in allw if w not in _recent_rand]
            if not candidates:
                while _recent_rand:
                    _recent_rand.pop()
                candidates = allw
            kw = random.choice(candidates)
            _recent_rand.append(kw)
            if len(_recent_rand) > 6:
                _recent_rand.pop(0)
            page = random.choice([1, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15])
            made = 0
            for x in search_items(kw, page, **search_pushdown()):
                if not item_domain_ok(x, enabled):   # 分区白名单(严格)
                    continue
                bvid = (x.get("url") or "").rsplit("/", 1)[-1]
                if bvid and bvid not in seen and len(items) < 450:
                    seen[bvid] = 1
                    x["src"] = "随·" + kw[:6]   # 混排桶标记(随机词)
                    items.append(x)
                    made += 1
            if made:
                names.append("随机·%s×%d" % (kw, made))

    # (偏好词引擎已上移: 提前并行提交 + 榜单消费后立即收编, 见 fetch_pool 前段。
    #  旧版排在此处且带 len(items)<420 闸门, 相关链+词引擎灌满池子后整段被跳过,
    #  快速开屏也完全不搜 —— 这是"偏好几乎刷不到"的根因, 已修。)

    # --- 严肃模式旧串行深挖已由上方并行搜索替代, 保留代码路径关闭以便回滚参考 ---
    if False and not quick and len(items) < 420 and ser_mode:
        def _dig_one(kw):
            got = 0
            for pg in range(1, 3):   # 最多2页(单页30条, 60条原始够; 减少full补全耗时)
                try:
                    res = search_items(kw, pg)
                except Exception:
                    break
                fresh = 0
                for x in res:
                    if not is_serious_ok(x):
                        continue
                    if not item_domain_ok(x, enabled):
                        continue
                    bvid = (x.get("url") or "").rsplit("/", 1)[-1]
                    if bvid and bvid not in seen and len(items) < 450:
                        seen[bvid] = 1
                        x["src"] = "严·" + kw[:6]
                        items.append(x)
                        fresh += 1
                got += fresh
                if fresh == 0:
                    break   # 该词到底
                if got >= 20:
                    break   # 该词够20条, 换其他词(更快覆盖更多词)
                time.sleep(0.15)
            return kw, got

        # 8并发深挖; 每批5词, 最多两批(限时, 不让补全拖太久); 单词20条达标即换词
        for batch in range(2):
            pool_kws = serious_kw_pool(enabled)
            if not pool_kws:
                break
            for kw, got in _FX.map(lambda a: _dig_one(a), pool_kws[:5]):
                if got:
                    names.append("严肃·%s×%d" % (kw, got))
            _pref_cursor += 5
            if len(items) >= 300 or batch >= 1:
                break

    _fetch_count += 1
    if not items:
        return {"ok": False, "offline": True, "source": "内置示例(离线)",
                "items": MOCK_ITEMS, "error": "; ".join(errors)}
    return {"ok": True, "offline": False, "source": " + ".join(names),
            "items": items[:400], "error": ""}


def purge_items_by_parts(enabled):
    """分区切换后清理不匹配的沉淀池条目(按 json 内 gid/分区属性判断)。返回清理数。
    贴吧条目豁免(独立平台, 不随B站分区)。"""
    if not _subset_mode(enabled):
        return 0
    keys = []
    for (k, j) in db().execute("SELECT key, json FROM items").fetchall():
        try:
            it = json.loads(j)
        except Exception:
            continue
        if it.get("platform") != "B站":
            continue   # 非B站平台(贴吧/小黑盒等)不清
        if not item_domain_ok(it, enabled):
            keys.append(k)
    if keys:
        with _db_lock:
            db().executemany("DELETE FROM items WHERE key=?", [(k,) for k in keys])
            db().commit()
    return len(keys)


def get_feed_raw(force=False):
    now = time.time()
    if force or _feed_cache["payload"] is None or now - _feed_cache["ts"] > CACHE_TTL:
        payload = fetch_pool()
        payload["updated"] = int(now)
        _feed_cache.update(ts=now, payload=payload)
        sset("fetch_last", int(now))
        sset("fetch_error", payload.get("error", ""))
        if not payload["offline"]:
            with _db_lock:
                # v0.3.1.31: 与 build_universe 里那处同源 —— 旧实现每条先 SELECT json 再 UPSERT,
                # 而取回的 old/prev **从未被使用**(UPSERT 恒以 excluded.json 覆盖), 纯浪费往返。
                db().executemany("INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,?,0) "
                                 "ON CONFLICT(key) DO UPDATE SET json=excluded.json,fetched_at=excluded.fetched_at",
                                 [(it["key"], json.dumps(it, ensure_ascii=False), now)
                                  for it in payload["items"]])
                db().execute("DELETE FROM items WHERE fetched_at < ?", (now - 7 * 86400,))  # 只留7天历史
                db().commit()
    return _feed_cache["payload"]


def backlog_items(exclude_keys, enabled_ids, limit=300):
    """历史沉淀池: 之前几轮抓过、但当前新鲜页没有的条目(严格按分区开关过滤)。

    原来 limit=100、只扫最新 600 行, 注释写的理由是"避免历史内容反复占满首页导致重复率高"。
    实测(用户真实数据): 本地 items 存量 2.9 万条, 其中 1 万多条**从没推给过该用户**,
    而当前内存池只有两千条上下 —— 前端要保持 300 条缓冲, 很快见底, 滑到底就得等下一次构建;
    可源上此刻往往没有新内容(实测一次全量构建只多出 12 条)。也就是说用户等的主要不是网络,
    是这个被卡在 100 条的本地回流。
    现在放大到 300 条、扫描窗口 3000 行, 并**优先挑没进过 shown 的**: 回流进来的确实是他没看过的,
    而不是把刚看过的又端回来。重复率交给 shown 表把关, 不再靠"少回流"来压。
    本函数只读本地库, 不联网 —— 加深池子不增加任何抓取请求。"""
    seen = set(exclude_keys)
    try:
        shown = {r[0] for r in db().execute("SELECT key FROM shown").fetchall()}
    except Exception:
        shown = set()
    fresh, stale = [], []
    for (j,) in db().execute(
            "SELECT json FROM items ORDER BY fetched_at DESC LIMIT 3000").fetchall():
        if len(fresh) >= limit:
            break
        try:
            it = json.loads(j)
        except Exception:
            continue
        k = it.get("key")
        if not k or k in seen:
            continue
        if not item_domain_ok(it, enabled_ids):
            continue   # 统一分区校验: 旧池里不属于当前分区的全部不会回流
        seen.add(k)
        (stale if k in shown else fresh).append(it)
    # 没看过的排前面; 不够 limit 才用看过的补满(总回流上限仍是 limit)
    return (fresh + stale)[:limit]
    return out


# ---------------------------------------------------------------- AI 模块 (OpenAI 兼容)

def ai_ok():
    """是否已配置 API Key(总开关关闭不影响此判断, 用于状态显示)。"""
    return bool(sget("ai_key").strip())


def ai_on():
    """是否真正使用 AI: 总开关(ai_enabled) + 已配置 Key。"""
    return sget("ai_enabled") != "0" and ai_ok()


_COST_TL = threading.local()   # 当前请求累计的 token 用量(供 /api/ai/assist 回报本次花费)


def record_usage(usage):
    if not usage:
        return
    try:   # 线程内累计: 悬浮窗请求结束时把本次花费回传给前端显示
        _COST_TL.in_tokens = getattr(_COST_TL, "in_tokens", 0) + usage.get("prompt_tokens", 0)
        _COST_TL.out_tokens = getattr(_COST_TL, "out_tokens", 0) + usage.get("completion_tokens", 0)
        _COST_TL.calls = getattr(_COST_TL, "calls", 0) + 1
    except Exception:
        pass
    try:
        with _db_lock:
            # IFNULL: 行可能由 bump_stats 先建(NULL), 直接相加会永远 NULL
            db().execute("INSERT INTO stats(day,in_tokens,out_tokens,calls) VALUES(?,?,?,1) "
                         "ON CONFLICT(day) DO UPDATE SET "
                         "in_tokens=IFNULL(in_tokens,0)+excluded.in_tokens,"
                         "out_tokens=IFNULL(out_tokens,0)+excluded.out_tokens,"
                         "calls=IFNULL(calls,0)+1",
                         (today(), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))
            db().commit()
    except Exception:
        pass   # 统计失败不影响聊天; 下次调用继续累计


def llm_body(messages, max_tokens, temperature=0.3):
    # 注意: 非流式请求**不能**带 "stream": False 字段——v4-flash 会返回空content/finish=length(bug)
    return json.dumps({
        "model": sget("ai_model"), "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode("utf-8")


def call_llm(messages, max_tokens=1200, temperature=0.3):
    """调用 OpenAI 兼容接口, 返回 (content, usage) 或抛异常。"""
    body = llm_body(messages, max_tokens, temperature)
    req = urllib.request.Request(
        sget("ai_base").rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % sget("ai_key")})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    usage = data.get("usage") or {}
    record_usage(usage)
    return data["choices"][0]["message"]["content"], usage


# ---- 让 AI「先想再改设置」(v0.3.1.62) -------------------------------------
# deepseek-flash 默认就会吐 reasoning_content, 所以「关掉思考」必须显式发
# thinking.disabled, 只靠不发参数是关不掉的(实测: plain 请求 reasoning_tokens 也是 39)。
CHAT_EFFORT_TOKENS = {"off": 500, "low": 700, "mid": 900, "high": 1400}
CHAT_EFFORT_PARAM = {"low": "low", "mid": "medium", "high": "high"}


def call_llm_plan(messages, effort="mid"):
    """改设置专用的模型调用: effort 控制思考强度, 返回 (content, 思考字符数, usage)。"""
    eff = effort if effort in ("off", "low", "mid", "high") else "mid"
    payload = {"model": sget("ai_model"), "messages": messages,
               "max_tokens": CHAT_EFFORT_TOKENS[eff], "temperature": 0.1}
    if eff == "off":
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["reasoning_effort"] = CHAT_EFFORT_PARAM[eff]
    req = urllib.request.Request(
        sget("ai_base").rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % sget("ai_key")})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    usage = data.get("usage") or {}
    record_usage(usage)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or "",
            len(msg.get("reasoning_content") or ""), usage)


# 只允许模型改这些键 —— 其它一律丢弃, 免得它顺手改到抓取/数据类设置
_CHAT_PLAN_ENUMS = {
    "theme": {"dark", "light"},
    "layout": {"single", "dual", "big"},
    "cover": {"s", "m", "l", "xl"},
    "font": {"small", "medium", "large"},
    "reason": {"hide", "show"},
    "srcacc": {"0", "1"},
    "mode": {"严格", "标准", "宽松", "严肃"},
}
_CHAT_PLAN_WEIGHTS = {"bili_weight", "tb_weight", "xhh_weight", "live_weight"}

# 模型只能看到"当前设置"，看不到"刚才改成了什么"。用户说「换回去」时它无从下手，
# 只能回一句"你自己去设置里点"。所以每次真的改了东西就记一笔现状 → 旧值。
# 键 → 上面 cur 里对应的中文名（weights 那几项在 cur 里是带"占比"的）
_CHAT_PLAN_CURKEY = {
    "theme": "主题", "layout": "信息流布局", "cover": "封面大小", "font": "字体",
    "reason": "推荐理由", "srcacc": "来源标识跟随强调色", "accent": "强调色",
    "mode": "过滤档位", "bili_weight": "B站占比", "tb_weight": "贴吧占比",
    "xhh_weight": "小黑盒占比", "live_weight": "B站直播占比",
    "live_min_viewers": "直播人气门槛", "live_only_followed": "直播只看关注",
}
_CHAT_PLAN_LAST_TTL = 600      # 10 分钟内的「换回去」才认它；隔很久再说不该套到远古操作上


def _chat_plan_sanitize(actions):
    out = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        k = str(a.get("k") or "").strip()
        v = a.get("v")
        if k in _CHAT_PLAN_ENUMS:
            if str(v) in _CHAT_PLAN_ENUMS[k]:
                out.append({"k": k, "v": str(v)})
        elif k in _CHAT_PLAN_WEIGHTS:
            try:
                n = int(float(v))
            except Exception:
                continue
            out.append({"k": k, "v": max(0, min(100, n))})
        elif k == "live_min_viewers":
            try:
                n = int(float(v))
            except Exception:
                continue
            out.append({"k": k, "v": max(0, min(10000000, n))})
        elif k == "live_only_followed":
            on = str(v).strip().lower() in ("1", "true", "yes", "on", "开", "是", "只看关注")
            out.append({"k": k, "v": 1 if on else 0})
        elif k == "accent":
            s = str(v or "").strip().lower()
            if re.match(r"^#[0-9a-f]{6}$", s):
                out.append({"k": k, "v": s})
    return out[:8]      # 一句话最多改 8 项, 多了多半是模型在瞎编


def chat_plan(text, effort="mid", prefs=None, weights=None, live=None, history=None):
    """把一句自然语言交给模型理解成设置改动; 模型不可用时回 ok=False, 让前端退回本地规则。

    history: 本次会话最近的对话（从旧到新）。必须带 —— 「换回去」「再大一点」这类话
    离开上下文就没有意义，只给当前设置的话模型只能反问或瞎猜。
    """
    if not ai_ok():
        return {"ok": False, "error": "no_ai"}
    prefs, weights, live = prefs or {}, weights or {}, live or {}
    cur = {
        "主题": prefs.get("theme"), "信息流布局": prefs.get("layout"),
        "封面大小": prefs.get("cover"), "字体": prefs.get("font"),
        "推荐理由": prefs.get("reason"), "来源标识跟随强调色": prefs.get("srcacc"),
        "强调色": prefs.get("accent"), "过滤档位": sget("mode"),
        "B站占比": weights.get("B站"), "贴吧占比": weights.get("贴吧"),
        "小黑盒占比": weights.get("小黑盒"), "B站直播占比": weights.get("B站直播"),
        "直播人气门槛": live.get("min_viewers"), "直播只看关注": live.get("only_followed"),
    }
    # 上一轮真改过什么（10 分钟内有效）—— 给「换回去」一个能落地的依据
    last = []
    try:
        raw = sget("chat_plan_last")
        obj = json.loads(raw) if raw else {}
        if isinstance(obj, dict) and (time.time() - float(obj.get("ts") or 0)) < _CHAT_PLAN_LAST_TTL:
            last = obj.get("items") if isinstance(obj.get("items"), list) else []
    except Exception:
        last = []

    hist = ""
    for h in (history or [])[-8:]:
        if not isinstance(h, dict):
            continue
        c = str(h.get("content") or "").strip()
        if not c:
            continue
        hist += ("用户: " if h.get("role") == "user" else "助手: ") + c[:200] + "\n"

    sys_prompt = (
        "你在帮用户改一个信息聚合软件的设置。只输出一个 JSON 对象, 不要解释、不要代码块:\n"
        '{"setting": true, "say": "20 字内的中文回执", "actions": [{"k": "键", "v": 值}]}\n'
        "setting: 这句是不是在说界面/设置本身。不是设置请求(问内容、闲聊)就 setting=false、"
        "actions 留空、say 留空 —— 系统会把这句交给普通聊天。\n"
        "可用键: theme=dark|light, layout=single|dual|big, cover=s|m|l|xl, "
        "font=small|medium|large, reason=hide|show, srcacc=0|1, "
        "mode=严格|标准|宽松|严肃, accent=#rrggbb, "
        "bili_weight/tb_weight/xhh_weight/live_weight=0..100 整数, "
        "live_min_viewers=整数, live_only_followed=0|1。\n"
        "规矩: 用户只提某项就只改那项; 一句话里有多条要求就全部列进 actions; "
        "拿不准别猜, actions 留空并在 say 里问一句。\n"
        "相对说法必须换算成具体值(对照上面的当前设置): 「字号大一点/调大」= 往上走一档"
        "(small→medium→large), 「调到最大」= large; 「封面大一点」同理; "
        "占比「高一点/调高」= 当前值 +10, 「低一点」= 当前值 -10(限制在 0..100); "
        "「全都要」「都调高」这类要分别列出每一项。"
        "「换回去/改回来/恢复原样/退回去」= 把下面『上一轮改过的设置』逐项改回 old 值; "
        "上一轮没有记录就 actions 留空, 在 say 里问一句是哪一项。"
        "「再大一点/再高一点」= 在**当前值**基础上再走一档, 不是回到默认值。"
    )
    ctx = ""
    if hist:
        ctx += "最近对话(从旧到新):\n" + hist
    if last:
        ctx += "上一轮改过的设置: " + json.dumps(last, ensure_ascii=False) + "\n"
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "当前设置: " + json.dumps(cur, ensure_ascii=False)
         + "\n" + ctx + "用户说: " + str(text)},
    ]
    try:
        content, rchars, _usage = call_llm_plan(messages, effort)
    except Exception as e:
        return {"ok": False, "error": "AI 调用失败: %s" % e}
    obj = {}
    try:
        obj = llm_json(content)
    except Exception:
        obj = {}        # 模型没按格式吐 JSON: 当作"没读懂", 交前端回退本地规则
    if not isinstance(obj, dict):
        obj = {}
    acts = _chat_plan_sanitize(obj.get("actions"))
    # 老模型不一定吐 setting, 缺省按"有没有动作"推断 —— 有动作就一定是设置请求。
    # 也别直接 bool(): 模型常把布尔值写成字符串, bool("false") 会是 True。
    sv = obj.get("setting")
    if isinstance(sv, bool):
        is_setting = sv
    elif sv is None:
        is_setting = bool(acts)
    else:
        is_setting = str(sv).strip().lower() in ("1", "true", "yes", "是", "对")
    if acts:
        items = []
        for a in acts:
            ck = _CHAT_PLAN_CURKEY.get(a["k"])
            items.append({"k": a["k"], "name": ck or a["k"],
                          "old": cur.get(ck, "") if ck else "", "new": a["v"]})
        try:
            sset("chat_plan_last", json.dumps({"ts": time.time(), "items": items},
                                              ensure_ascii=False))
        except Exception:
            pass        # 记不上就只是"换回去"不好使, 不能因此让这次改动失败
    return {"ok": True, "setting": is_setting, "say": str(obj.get("say") or "")[:80],
            "actions": acts, "effort": effort, "reasoning_chars": rchars}


def llm_stream(messages, max_tokens=1200, on_reasoning=None):
    """流式调用: 逐段 yield 增量文本; 推理模型(如 deepseek-v4-flash)先出
    reasoning_content 再出 content——思考期间通过 on_reasoning 回调通知,
    思考字节忽略(否则 max_tokens 会被思考吃光导致 content 为空)。"""
    body = json.dumps({
        "model": sget("ai_model"), "messages": messages,
        "max_tokens": max_tokens, "temperature": 0.3, "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        sget("ai_base").rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % sget("ai_key"),
                 "Accept": "text/event-stream"})
    usage = {}
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("reasoning_content") and on_reasoning:
                on_reasoning(delta["reasoning_content"])
            c = delta.get("content")
            if c:
                yield c
    record_usage(usage)


# ---------------------------------------------------------------- 联网证据: 事实核查 / 回怼 (AI 悬浮窗)

_ASSIST_MAX_TEXT = 2000    # 待核查/回怼文本长度上限(防超长粘贴撑爆请求)
# 取证用的搜索词条数上限。每条查询要 2~4 秒(神马单次搜索 + 后续并发抓正文),
# 5 条 ≈ 15 秒; 再多用户就要盯着「正在搜集证据」等太久了。
_ASSIST_MAX_QUERIES = 5
_SEARCH_TTL = 1800         # 搜索结果缓存 30 分钟(同一问题反复查不重复出网)
_SEARCH_CACHE = {}         # query -> (ts, results)
_EVIDENCE_X = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ev")   # 网页正文抓取池

_UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 域名可信度分级: high=权威(政府/教育/主流媒体/学术), mid=可靠(门户/百科), low=论坛自媒体(仅参考)
_CRED_HIGH = (".gov.cn", ".gov", ".edu.cn", ".edu", "who.int", "un.org",
              "xinhuanet.com", "people.com.cn", "cctv.com", "thepaper.cn",
              "caixin.com", "chinanews.com", "gmw.cn", "ce.cn", "reuters.com",
              "apnews.com", "nature.com", "science.org", "wikipedia.org",
              "britannica.com", "bbc.com", "gov.uk")
_CRED_MID = ("baike.baidu.com", "zhihu.com", "163.com", "sina.com.cn",
             "sohu.com", "qq.com", "ifeng.com", "bjnews.com.cn",
             "nationalgeographic.com", "scientificamerican.com", "cnki.net")
_CRED_LOW = ("baijiahao.baidu.com", "tieba.baidu.com", "weibo.com", "weibo.cn",
             "zhidao.baidu.com", "douyin.com", "toutiao.com", "bilibili.com",
             "xiaohongshu.com", "facebook.com", "twitter.com", "x.com", "reddit.com",
             "360kuai.com", "k.sina.com.cn")   # 后两个: 内容农场/自媒体转载池
_CRED_LABEL = {"high": "权威", "mid": "可靠", "low": "参考"}

# 营销号标题特征: 命中即降级为「参考」
_SPAM_TITLE_RE = re.compile(
    r"震惊|惊呆|吓死|速看|删前|转疯|99%的人|太可怕|细思极恐|看完惊|"
    r"网友.{0,4}亮了|太意外|紧急提醒|赶紧看|必看|惊魂|吓坏了")


def domain_cred(url):
    """域名可信度: 返回 high/mid/low。"""
    host = urllib.parse.urlparse(url).netloc.lower()
    if not host:
        return "low"
    for pool, val in ((_CRED_HIGH, "high"), (_CRED_MID, "mid"), (_CRED_LOW, "low")):
        for d in pool:
            if host == d or host.endswith("." + d):
                return val
    return "mid"


def _http_get_text(url, timeout=10):
    """带浏览器 UA 的 GET, 返回解码后的文本(GBK/UTF-8 自适应)。只读前 600KB。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA_BROWSER,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(600000)
    head = raw[:2048].decode("ascii", "replace").lower()
    enc = "gb18030" if ("gb2312" in head or "gbk" in head or "gb18030" in head) else "utf-8"
    return raw.decode(enc, "replace")


def _strip_tags(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


# 360 的结果块有两代结构: 旧版 h3 里套 a(链接带 data-mdurl=), 新版 a 里套 h3
# (链接是 /jump?u= 或 /link?url= 跳转, 真实地址在参数里)。
_SOE_BLOCK = re.compile(
    r"<h3[^>]*>\s*<a[^>]*>.*?</a>\s*</h3>"
    r"|<a[^>]+href=\"[^\"]*\"[^>]*>\s*<h3[^>]*>.*?</h3>", re.S)
_SOE_DESC = re.compile(
    r'class="[^"]*(?:res-desc|summary|g-main)[^"]*"[^>]*>(.*?)(?:</div>|</p>|</span>)', re.S)


def _soe_real_url(raw):
    """还原 360 结果链接: 直接地址原样返回, /jump?u= 与 /link?url= 解出参数里的目标。"""
    u = html.unescape(str(raw or "")).strip()
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        u = "https://www.so.com" + u
    if not u.startswith("http"):
        return ""
    if "so.com/link" in u or "so.com/jump" in u:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        for k in ("url", "u"):
            for v in qs.get(k, []):
                v = html.unescape(v).strip()
                if v.startswith("http"):
                    return v
        return ""   # 只有 m= 标记没有目标地址 → 是广告位/跳转壳, 不收
    return u


def _soe_parse(page, n):
    """解析 360 结果页, 兼容新旧两代结构。"""
    out = []
    blocks = [(m.start(), m.end(), m.group(0)) for m in _SOE_BLOCK.finditer(page)]
    for pos, end, tag in blocks:
        ti = re.search(r"<h3[^>]*>(.*?)</h3>", tag, re.S) or re.search(r"<a[^>]*>(.*?)</a>", tag, re.S)
        if not ti:
            continue
        title = _strip_tags(ti.group(1))
        if not title:
            continue
        md = re.search(r'data-mdurl="([^"]+)"', tag)
        hr = re.search(r'href="([^"]+)"', tag)
        u = _soe_real_url(md.group(1) if md else (hr.group(1) if hr else ""))
        if not u:
            continue
        nxt = next((p for p, _e, _t in blocks if p > pos), len(page))
        ab = _SOE_DESC.search(page[end:nxt])
        out.append({"title": title, "url": u,
                    "snippet": _strip_tags(ab.group(1))[:200] if ab else ""})
        if len(out) >= n:
            break
    return out


def search_soe(query, n=6):
    """360 搜索(曾经国内最稳, 近期长期风控)。
    桌面站被风控时只会回「访问异常页面」, 这时改用移动站再试一次; 两个站都被风控就冷却。"""
    if not _engine_ready("soe"):
        return []
    for host in ("https://www.so.com/s?", "https://m.so.com/s?"):
        try:
            page = _http_get_text(host + urllib.parse.urlencode(
                {"q": query, "rn": str(n)}), timeout=8)
        except Exception:
            continue
        if _engine_block("soe", page):
            continue
        out = _soe_parse(page, n)
        if out:
            return out
    return []


# 引擎冷却: 被风控页挡下就别继续硬打(越打风控越久), 先歇一会儿再试。
# 实测神马 m.sm.cn 打太密会回 captcha 跳转页(858 字节, 见 _SM_BLOCK),
# 360 回「访问异常页面」(9847 字节)。冷却期内直接跳过, 省一次请求也少惹一次风控。
_ENGINE_COOLDOWN = {}      # 引擎名 → 冷却截止时间戳
_ENGINE_COOLDOWN_SECS = 180


def _engine_ready(name):
    return time.time() >= _ENGINE_COOLDOWN.get(name, 0)


def _engine_block(name, page):
    """命中风控页 → 该引擎冷却一段时间。返回是否被风控。"""
    if page and _BLOCK_PAGE_RE.search(page[:4000]):
        _ENGINE_COOLDOWN[name] = time.time() + _ENGINE_COOLDOWN_SECS
        return True
    return False


_BLOCK_PAGE_RE = re.compile(r"访问异常|/punish|x5secdata|captcha|验证码|安全验证|robot check",
                            re.I)


def search_sm(query, n=6):
    """神马搜索(m.sm.cn, 阿里/UC 系移动搜索): 目前唯一稳定好用的中文新闻源。
    360 长期回「访问异常」风控页(带 Cookie 也没用), Bing 对中文多词查询只匹配头一个词
    并且退化成百科实体页(搜「055驱逐舰 绕澳大利亚 5万海里」只回 055 百度百科词条),
    只有神马既给真实外链又能命中新闻正文。被风控时冷却 3 分钟。失败返回 []。"""
    if not _engine_ready("sm"):
        return []
    try:
        page = _http_get_text("https://m.sm.cn/s?" + urllib.parse.urlencode({"q": query}),
                              timeout=10)
    except Exception:
        return []
    if _engine_block("sm", page):
        return []
    return _sm_parse(page, n)


_SM_TPL = 'data-tpl="structure_template_normal"'   # 每个自然结果块的标记
_SM_TITLE = re.compile(r'class="qk-title-text[^"]*"[^>]*>(.*?)</div>', re.S)
_SM_DESC = re.compile(r'class="qk-paragraph-text"[^>]*>(.*?)</div>', re.S)
_SM_SRC = re.compile(r'class="qk-source-item[^"]*"[^>]*>(.*?)</div>', re.S)
_SM_HREF = re.compile(r'href="(https?://[^"]+)"')


def _sm_parse(page, n=6):
    """神马结果块: 按 data-tpl 切块, 块内取首个外链(真实地址) + qk-title-text(标题)
    + qk-paragraph-text(摘要) + qk-source-item(来源·时间)。"""
    if not page or _SM_TPL not in page:
        return []
    out, seen = [], set()
    for blk in page.split(_SM_TPL)[1:]:
        m = _SM_HREF.search(blk)
        if not m:
            continue
        url = html.unescape(m.group(1))
        if not url.startswith("http"):
            continue
        u = urllib.parse.urlparse(url)
        if not u.netloc or u.netloc.endswith("sm.cn"):
            continue   # 神马自家聚合页/跳转页不要
        t = _SM_TITLE.search(blk)
        title = _strip_tags(t.group(1)) if t else ""
        if not title:
            continue
        d = _SM_DESC.search(blk)
        s = _SM_SRC.search(blk)
        k = u.netloc + u.path
        if k in seen:
            continue
        seen.add(k)
        out.append({"title": title[:120], "url": url,
                    "snippet": _strip_tags(d.group(1))[:300] if d else "",
                    "site": _strip_tags(s.group(1))[:40] if s else u.netloc})
        if len(out) >= n:
            break
    return out


def search_bing(query, n=6):
    """Bing 网页搜索(HTML 解析, 免 key)。失败/被风控返回 []。
    注意: 中文多词查询会退化成头一个词的百科/门户页, 只适合英文查询与兜底。"""
    try:
        page = _http_get_text("https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "count": str(n), "mkt": "zh-CN"}), timeout=8)
    except Exception:
        return []
    out = []
    for m in re.finditer(r'<li class="b_algo"[^>]*>(.*?)</li>', page, re.S):
        block = m.group(1)
        a = (re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
             or re.search(r'<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', block, re.S))
        if not a:
            continue
        u = html.unescape(a.group(1)).strip()
        if not u.startswith("http"):
            continue   # 相对链接/内站跳转不收
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        out.append({"title": _strip_tags(a.group(2)), "url": u,
                    "snippet": _strip_tags(p.group(1)) if p else ""})
        if len(out) >= n:
            break
    return out


def search_ddg(query, n=6):
    """DuckDuckGo Lite 兜底搜索(Bing 被风控时用)。失败返回 []。"""
    try:
        page = _http_get_text("https://lite.duckduckgo.com/lite/?" +
                              urllib.parse.urlencode({"q": query}), timeout=8)
    except Exception:
        return []
    out = []
    anchors = [(m.start(), m.end(), html.unescape(m.group(1)), m.group(2))
               for m in re.finditer(r'<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', page, re.S)]
    snips = [(m.start(), _strip_tags(m.group(1)))
             for m in re.finditer(r'class="result-snippet"[^>]*>(.*?)</td>', page, re.S)]
    for pos, _end, u, title_html in anchors:
        if "duckduckgo.com" in u:   # 站内跳转: 解包真实目标
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            u = (q.get("uddg") or [""])[0]
            if not u:
                continue
        if not u.startswith("http"):
            continue
        nxt = next((a_pos for a_pos, *_ in anchors if a_pos > pos), len(page))
        snip = next((t for s_pos, t in snips if pos < s_pos < nxt), "")
        out.append({"title": _strip_tags(title_html), "url": u, "snippet": snip})
        if len(out) >= n:
            break
    return out


def web_search(query, n=8):
    """多引擎兜底(凑不满 3 条才降级下一家); 合并去重 + 30 分钟缓存。
    引擎顺序按语言分流: 中文查询走神马(m.sm.cn)——目前唯一能稳定命中中文新闻正文的一家;
    英文查询走 Bing(海外索引好)。之后依次 Bing/360/DDG 兜底。
    360 近段时间长期回风控页, 放在后面只当恢复后的备用。"""
    key = query.strip()[:120]
    now = time.time()
    hit = _SEARCH_CACHE.get(key)
    if hit and now - hit[0] < _SEARCH_TTL:
        return [dict(r) for r in hit[1]]

    def merge(results, more):
        got = {urllib.parse.urlparse(r["url"]).netloc + urllib.parse.urlparse(r["url"]).path
               for r in results}
        for r in more:
            u = urllib.parse.urlparse(r["url"])
            k = u.netloc + u.path
            if k not in got:
                got.add(k)
                results.append(r)
        return results

    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", query))
    order = ((search_sm, search_bing, search_soe, search_ddg) if has_cjk
             else (search_bing, search_sm, search_soe, search_ddg))
    results = []
    for engine in order:
        if len(results) >= 3:
            break
        try:
            results = merge(results, engine(query, n))
        except Exception:
            continue   # 单个引擎炸了不影响其它引擎
    results = [r for r in results
               if r["url"].startswith("http") and "duckduckgo.com" not in r["url"]][:n]
    if len(_SEARCH_CACHE) > 64:   # 防膨胀: 超限清掉最旧一半
        olds = sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])[:32]
        for k in olds:
            _SEARCH_CACHE.pop(k, None)
    _SEARCH_CACHE[key] = (now, results)
    return [dict(r) for r in results]


class _TextExtract(html.parser.HTMLParser):
    """网页正文提取: 跳过 script/style/nav 等噪音标签, 收集可见文本与 <title>。"""
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title = [], ""
        self._skip = self._in_title = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = 0

    def handle_data(self, data):
        t = data.strip()
        if not t:
            return
        if self._in_title:
            self.title = (self.title + " " + t).strip()
        elif not self._skip:
            self.parts.append(t)


def fetch_page_text(url, max_chars=3200):
    """抓网页正文纯文本(容错), 非 HTML/失败返回 ""。"""
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA_BROWSER,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not any(x in ctype for x in ("html", "xml", "text/plain")):
                return ""
            raw = resp.read(500000)
        head = raw[:2048].decode("ascii", "replace").lower()
        enc = "gb18030" if ("gb2312" in head or "gbk" in head or "gb18030" in head) else "utf-8"
        page = raw.decode(enc, "replace")
    except Exception:
        return ""
    p = _TextExtract()
    try:
        p.feed(page)
    except Exception:
        pass
    return re.sub(r"\s{2,}", " ", " ".join(p.parts)).strip()[:max_chars]


# 搜索引擎对中文查询常只匹配头一个词(搜「大厂都在裁员」回一堆大厂排名, 搜
# 「2024程序员岗位减少40%」回 2024 年日历页)。这类页面混进证据会让模型拿日历页当论据,
# 所以按查询词与结果文本的字符重合度筛一遍: 重合太低的直接不要。
_REL_STOP = set("的了和与及或在是有为对不也就都还你我他她它这那吗呢吧啊么"
                "什么怎样如何为何没有会要很太可能已经正在并且而且但是所以因为如果虽然可以")
_REL_MIN = 0.3
# 权威域名放宽到这(同一件事换个说法也值得留), 但不能零门槛。0.15 是实测分界:
# 真实命中在短句场景下会被「辟谣 真相」这类兜底后缀稀释到 0.18 左右
# (查询「网传说可乐能杀灭蛔虫 辟谣 真相」vs 标题「世卫声明：可乐杀蛔虫不实」),
# 而引擎退化成头词匹配时回的实体页只有 0.14 及以下(「山东省_百度百科」vs「山东舰 西太平洋 部署 天数」)。
_REL_RELAX = 0.15

# 通用实体页不是证据: 引擎退化成「头词匹配」时只会回这类页面(实测搜「山东舰 西太平洋
# 部署 天数」回「山东省_百度百科」「山东省人民政府」「山东省教育招生考试院官网」,
# 搜「美国航母生锈」回「百度百科·美国」「美国历史…_百度百科」)。它们域名权威度高、
# 与具体说法却毫无关系, 放进去模型就会拿「沈阳市_百度百科」当反例。
# 注意只拦地名/国家/机构这类通用实体页; 具体装备词条(「055型驱逐舰_百度百科」)也一样拦,
# 因为百科对「这艘舰跑过多少海里」这种具体事实给不出证据, 真正管用的是新闻。
_ENTITY_TITLE_RE = re.compile(
    r"百度百科|维基百科|搜狗百科|360百科|互动百科|"
    r"人民政府|政务服务|招生考试|国家概况|行政区划|省情|市情|"
    r"旅游攻略|旅游景点|值得去的|必去的|"
    r"官方网站|官网$|Department of State")


def _is_entity_page(title):
    return bool(_ENTITY_TITLE_RE.search(str(title or "")))


def _rel_grams(text):
    """切词: 英文数字按整词, 中文按两字滑窗; 丢掉全由停用字拼成的碎片。"""
    t = str(text or "").lower()
    out = set(w for w in re.findall(r"[a-z0-9]+", t) if len(w) >= 2)
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g[0] in _REL_STOP and g[1] in _REL_STOP:
                continue
            out.add(g)
    return out


def _relevance(query, *texts):
    """查询词与结果文本的重合度(0~1)。查询词太短无法判断时返回 1(不做过滤)。"""
    qg = _rel_grams(query)
    if len(qg) < 2:
        return 1.0
    tg = _rel_grams(" ".join(str(t or "") for t in texts))
    return len(qg & tg) / float(len(qg))


def gather_evidence(claim, extra_queries=(), per_query=6, want_pages=4, max_evidence=10):
    """围绕说法联网搜索并抓正文, 证据按可信度与相关度排序(权威且贴题的在前)。
    全程容错: 搜不到/抓不到只返回已得部分, 完全失败返回 []。"""
    queries = []
    for q in extra_queries:
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q[:64])
    raw = claim.strip()[:64]
    # 论点拆解出的短查询词命中率远高于原话整段(整段口语句子只会搜回一堆泛化页面),
    # 短查询词够两个时就不拿原话整段去搜
    if raw and raw not in queries and len(queries) < 2:
        queries.append(raw)
    results, seen = [], set()
    for q in queries:
        for r in web_search(q, per_query):
            u = urllib.parse.urlparse(r["url"])
            k = u.netloc + u.path
            if k in seen:
                continue
            seen.add(k)
            r["_q"] = q
            r["_cred"] = domain_cred(r["url"])
            r["_cred_label"] = _CRED_LABEL[r["_cred"]]
            results.append(r)
    if not results:
        return []
    rank = {"high": 0, "mid": 1, "low": 2}
    for r in results:
        if _SPAM_TITLE_RE.search(r["title"] + " " + r.get("snippet", "")):
            r["_cred"], r["_cred_label"] = "low", "参考"   # 营销号腔标题 → 降级
        r["_rel"] = _relevance(r["_q"], r["title"], r.get("snippet", ""))
    # 通用实体页(百科/政府门户/旅游攻略)先剔掉: 引擎退化时它们会占满结果, 而它们
    # 恰恰是最不可能是证据的东西
    results = [r for r in results if not _is_entity_page(r["title"])]
    # 权威来源即便措辞与查询词不同也留着(可能是同一件事的另一种说法), 但不能零门槛:
    # 否则一条只碰巧提到查询词头两个字的高权重页面也会被当成证据
    keep = [r for r in results
            if r["_rel"] >= _REL_MIN or (r["_cred"] == "high" and r["_rel"] >= _REL_RELAX)]
    if not keep:   # 全军覆没: 只留最贴题的一条, 一条都不贴题就干脆不拿证据
        best = max(results, key=lambda r: r["_rel"]) if results else None
        keep = [best] if best and best["_rel"] >= _REL_MIN else []
    results = keep
    results.sort(key=lambda r: (rank.get(r["_cred"], 3), -r["_rel"]))
    solid = [r for r in results if r["_cred"] != "low"]
    if len(solid) >= 3:
        results = solid   # 权威/可靠来源够用时, 低可信(含营销号)整个剔除
    results = results[:max_evidence]
    # 并发抓正文: 优先权威/可靠来源; 全是低可信时也抓前 2 条(有总比没有强)
    todo = [r for r in results if r["_cred"] != "low"][:want_pages] or results[:2]

    def _grab(r):
        r["page"] = fetch_page_text(r["url"])

    list(_EVIDENCE_X.map(_grab, todo))
    return results


def evidence_block(evidence):
    """把证据排成给 LLM 看的文本块(带编号, 供回复引用)。"""
    lines = []
    for i, r in enumerate(evidence, 1):
        r["id"] = "E%d" % i
        site = urllib.parse.urlparse(r["url"]).netloc
        seg = "[%s] (%s·%s) %s" % (r["id"], r["_cred_label"], site, r["title"])
        if r.get("snippet"):
            seg += "\n摘要: " + r["snippet"][:160]
        if r.get("page"):
            seg += "\n正文摘录: " + r["page"][:1200]
        lines.append(seg)
    return "\n\n".join(lines)


def llm_json(content):
    """从 LLM 回复中提取第一个 JSON 对象(容忍代码围栏/前后废话)。失败抛 ValueError。"""
    c = (content or "").strip()
    m = re.search(r"\{", c)
    if not m:
        raise ValueError("回复中没有 JSON")
    try:
        obj, _ = json.JSONDecoder().raw_decode(c[m.start():])
    except Exception as e:
        raise ValueError("JSON 解析失败: %s" % e)
    if not isinstance(obj, dict):
        raise ValueError("JSON 不是对象")
    return obj


# ---------------- 对线话术模板库(回怼润色参考) ----------------

_BUILTIN_TALK = [
    "就这？研究了半天就得出这结论？",
    "按你这逻辑，那不是更说不过去？",
    "数字都摆在这儿了，建议背完再来对线。",
    "不愧是你，这种角度都能圆回来。",
    "先查查资料再发言，不难。",
    "这话早就被辟谣了，二手传播还这么理直气壮。",
    "懂了，你比专业机构还专业。",
    "您这逻辑，建议申请专利。",
    "一看就是没查过资料，全凭一张嘴。",
    "行行行，你赢了，事实算什么。",
    "造谣一张嘴，辟谣跑断腿，说的就是你这种。",
    "自信是好事，但盲信数据会打脸。",
    "拿个例当规律，这理解能力我服气。",
    "别人的科普你看不进，你自己的脑补倒是全信。",
    "结论先行再找理由，这叫预设立场。",
    "建议把这段话裱起来，当反面教材。",
    "绷不住，这也能有人信。",
    "就离谱，这逻辑自己就闭环了。",
    "你开心就好，反正确实没人拦着你。",
    "这结论是怎么蹦出来的，给我也整一个。",
    "参照系不同结论就是不同，你拿地球当参照物，太阳不也绕着地球转。",
]

_TALK_CACHE = {"ts": 0.0, "templates": []}
_TALK_TTL = 86400   # 话术模板联网采集结果缓存 24 小时


def fetch_talk_templates():
    """联网采集对线话术模板(从讨论页正文里抽 8~60 字的短句), 24h 缓存。
    失败/采不到返回 [](回退用内置库)。"""
    now = time.time()
    if _TALK_CACHE["templates"] and now - _TALK_CACHE["ts"] < _TALK_TTL:
        return _TALK_CACHE["templates"]
    pool = []
    for q in ("网络对线 阴阳怪气 话术模板", "怼人语录 高赞 简短", "对线语录 回复 模板"):
        try:
            results = web_search(q, 6)
        except Exception:
            results = []
        for r in results:
            page = fetch_page_text(r["url"], max_chars=4000)
            if not page:
                continue
            for seg in re.split(r"[。！？\n]", page):
                seg = seg.strip(" \t·-—…「」【】“”\"'、,，;；")
                if 8 <= len(seg) <= 60 and not re.search(r"http|www\.|广告|关注|订阅|下载|APP", seg):
                    if seg not in pool:
                        pool.append(seg)
            if len(pool) >= 48:
                break
        if len(pool) >= 48:
            break
    if pool:
        _TALK_CACHE.update(ts=now, templates=pool[:60])
        return pool[:60]
    return []


def decompose_plan(text):
    """把言论拆成可验证的事实论点, 每个论点配「正向 + 反例向」搜索词, 外加一条整段话的
    最有力反例词。返回 (claims, best_counter); 失败返回 ([], "")。
    短句(≤12字)只有一层意思: 不拆不脑补, 直接按原文单论点处理; 长句才走 LLM 拆解。"""
    t = text.strip()
    if len(t) <= 12:
        return [{"point": t[:60], "query": "", "counter": ""}], ""
    if not ai_on():
        return [], ""
    # query 是「顺着原话查」, counter 是「反例向查」——后者才是能打脸的关键。
    # 只给正向词时模型会照抄对方措辞(「中国航母 不生锈 远洋任务」), 搜回来全是同温层内容
    # (「辽宁号也生锈」); 换成事实措辞(「055驱逐舰 绕澳 5万海里」)才能搜到直接反例。
    # best_counter 更进一步: 允许跳出原话点名的对象去找同类装备/同类事件的纪录——
    # 「中国航母没跑过远洋」最有力的一击其实来自驱逐舰的远洋纪录, 逐论点拆解抓不到这种跨类反例。
    ask = ("把下面这段言论拆成2-4个独立的事实论点(剥掉修辞和情绪, 只留可验证的事实主张, "
           "按原意保留错误主张不要纠正), 并给每个论点配两条中文搜索词:\n%s\n\n"
           "query: 顺着原话查证的词, 用来找与该主张直接相关的资料。\n"
           "counter: 反例向的词, 用来找能直接打脸该说法的真实案例。写 counter 的硬性要求:\n"
           "  1) 必须能搜到「具体对象 + 具体数字」的实例(装备型号/舰机名/事件名/天数/海里数), "
           "不许用形容词和评价词;\n"
           "  2) 绝对不许照抄原话里的措辞, 一个字都不许复用原话的词组;\n"
           "  3) 想清楚「要推翻这个说法, 网上该有什么事实」, 再把这个事实描述成搜索词。\n"
           "例: 原话「中国航母不生锈是因为没执行远洋长时间任务」→ counter 写"
           "「山东舰 西太平洋 训练 天数」, 绝不写成「中国航母 不生锈 远洋任务」。\n\n"
           "best_counter: 跳出原话点名的对象, 想一下「整段话最容易被哪个公开事实打脸」, "
           "给一条搜索词去搜那个事实——可以是同类装备、同类事件的纪录。\n"
           "例: 整段话在说「中国航母没跑过远洋所以不生锈」→ best_counter 写"
           "「055型驱逐舰 远洋部署 天数」或「052D驱逐舰 亚丁湾护航 连续部署 天数」, "
           "因为驱逐舰跑过更长时间的远洋任务, 最能反证「没跑过远洋」这个前提。\n\n"
           "硬性要求: 论点必须是对原话的直接拆分, 严禁添加原话里没有的主张、"
           "严禁脑补对方的其他观点; 原话只有一个主张就只输出1个论点。\n"
           '只输出JSON: {"claims": [{"point": "事实论点(简短陈述句)", '
           '"query": "顺着查的词(不超过20字)", "counter": "反例向的词(不超过20字)"}], '
           '"best_counter": "整段话最有力的反例搜索词(不超过20字)"}'
           % t[:_ASSIST_MAX_TEXT])
    try:
        # 推理模型容易把输出额度花在思考上导致JSON截断 → 交给带重试的解析(额度加大再试一次),
        # 否则拆解一失败, 后面的取证与「要打的点」就全丢了
        res = _assist_llm_json("你是论点拆解器, 只输出JSON对象, 不要任何解释。", ask, 1200, 0.2)
    except Exception:
        return [], ""   # 拆解失败不致命: 后续按原文整体检索
    out = []
    for c in (res.get("claims") or [])[:4]:
        if not isinstance(c, dict):
            continue
        p = str(c.get("point") or "").strip()[:120]
        q = str(c.get("query") or "").strip()[:40]
        k = str(c.get("counter") or "").strip()[:40]
        if p:
            out.append({"point": p, "query": q, "counter": k})
    return out, str(res.get("best_counter") or "").strip()[:40]


def decompose_claim(text):
    """只要论点列表(旧接口, 测试与内部调用沿用)。"""
    return decompose_plan(text)[0]


def assist_plan(text):
    """论点拆解 + 汇总搜索词(整段反例 + 逐论点反例优先, 再补正向; 上限 5 条)。
    返回 (claims, queries)。

    排序有讲究: gather_evidence 会把查询词逐条搜完再合并, 但条数越多越慢(每条 2~4 秒),
    所以必须裁到固定条数。裁的时候先保反例向——正向词照抄对方措辞只能搜到同温层内容,
    反例向词才是能打脸的素材; 按「正反交替」排的话, 3 个论点时第 3 个论点的词会被整条丢掉,
    而往往最后那个论点(「中国航母没执行过远洋任务」)才是真正要打的那个。
    """
    claims, best = decompose_plan(text)
    pos = [c["query"] for c in claims if c.get("query")]
    neg = [c["counter"] for c in claims if c.get("counter")]
    queries = []
    for q in ([best] if best else []) + neg + pos:
        if q not in queries:
            queries.append(q)
    if len(queries) < 2:
        queries.append(text.strip()[:30] + " 辟谣 真相")
    return claims, queries[:_ASSIST_MAX_QUERIES]


def _norm_evidence(evidence, ids=None):
    """规整成前端要展示的证据卡: ids 给定则只留被引用的(一个没引则全留)。"""
    out = []
    for i, r in enumerate(evidence, 1):
        if ids and "E%d" % i not in ids:
            continue
        out.append({"id": "E%d" % i, "title": r["title"], "url": r["url"],
                    "site": urllib.parse.urlparse(r["url"]).netloc,
                    "cred": r["_cred"], "cred_label": r["_cred_label"],
                    "snippet": (r.get("snippet") or "")[:220]})
    return out


def _evidence_brief(refs):
    """给悬浮球用的证据摘要(编号+标题+链接): 让每条回复后面的编号能点开溯源。"""
    out = []
    for e in refs or []:
        u = str(e.get("url") or "")
        if not u:
            continue
        out.append({"id": e.get("id") or "", "title": str(e.get("title") or "")[:60],
                    "url": u, "cred": e.get("cred_label") or ""})
    return out[:10]


# 推理模型的思考额度下限。deepseek-flash 这类模型先产出思考再产出正文, 而 max_tokens 把
# 思考一起算进去: 实测同一句论点拆解在 max_tokens=1200 时 6 次有 5 次思考占满额度、
# content 是空串; 2000 时 3/6; 4000 时 1/6; 6000 时 0/6。思考长度本身波动很大
# (实测 372~4137), 所以任何要 JSON 的调用都不能按「正文有多长」给额度。
# 额度给大不会多花钱——模型不用就按实际 completion_tokens 计费, 只是别再让它截断。
_JSON_MIN_TOKENS = 4000


def _assist_llm_json(system, user, max_tokens, temperature):
    """要求 LLM 按 JSON 回答。
    两轮: 第一轮按调用方给的额度(但不低于 _JSON_MIN_TOKENS, 给思考留够空间);
    第二轮再加大额度并强调格式。content 为空串是「思考把额度吃光」的典型症状,
    加多少「只输出JSON」的强调都没用, 只能给更多额度重来。"""
    last = None
    base = max(int(max_tokens or 0), _JSON_MIN_TOKENS)
    for i, mt in enumerate((base, base + 3000)):
        msg = user if i == 0 else user + "\n\n再次强调: 只输出JSON对象本身, 不要任何解释文字, 不要代码块围栏。"
        try:
            content, _ = call_llm([{"role": "system", "content": system},
                                   {"role": "user", "content": msg}],
                                  max_tokens=mt, temperature=temperature)
        except Exception as e:
            last = e
            continue
        if not str(content or "").strip():
            last = ValueError("模型把输出额度花在思考上, 没留下正文")
            continue
        try:
            return llm_json(content)
        except ValueError as e:
            last = e
    raise last if isinstance(last, Exception) else ValueError("模型没有给出可用回复")


def _verify_from_text(content, evidence):
    """纯文本兜底解析核查结果: 首行「结论：真实/虚假/存疑（置信度NN%）」, 其后 - 开头理由行。"""
    lines = [l.strip() for l in (content or "").splitlines() if l.strip()]
    first = lines[0] if lines else ""
    if "虚假" in first[:24] or "不实" in first[:24]:
        verdict = "false"
    elif "真实" in first[:24] or "属实" in first[:24]:
        verdict = "true"
    else:
        verdict = "uncertain"
    m = re.search(r"(\d{1,3})\s*%", first)
    try:
        conf = max(0, min(100, int(m.group(1))))
    except Exception:
        conf = 60
    reasons = []
    for l in lines[1:]:
        l = re.sub(r"^[-•·*\d.、)\s]+", "", l).strip()
        if l and not l.startswith(("【", "证据")):
            reasons.append(l[:300])
        if len(reasons) >= 4:
            break
    return {"verdict": verdict, "confidence": conf,
            "summary": re.sub(r"[（(]?置信度\d{1,3}%[）)]?", "", first).strip()[:200],
            "reasons": reasons, "evidence_used": [], "offline": not evidence}


def _rebut_from_text(content, evidence):
    """纯文本兜底解析回怼: 每行一条可粘贴回复。"""
    lines = [re.sub(r"^[-•·*\d.、)\s]+", "", l).strip()[:100]
             for l in (content or "").splitlines() if l.strip()]
    refs = _norm_evidence(evidence)
    return {"points": [{"point": "", "reply": t, "basis": "", "evidence": refs[:2]}
                       for t in lines if len(t) >= 4][:4],
            "finale": "", "stance_note": "", "offline": not evidence}


def _floatball_prefs():
    """悬浮窗调节档位(给前端与悬浮球读的当前值; 旧值一律折算成新档位)。

    只含**点击展开后仍生效**的生成档位: 思考/身份/风格/条数。悬停自动展开已移除,
    所以不再有 hover 这一项(见 run_float_ball 里的开合说明)。
    """
    return {"think": sget("assist_think") or "标准",
            "persona": _persona_key(sget("assist_persona")),
            "savage": _savage_key(sget("assist_savage")),
            "len": str(assist_len_n())}


# ---------------- 对线收藏(回怼结果存本地, 可搜索复用) ----------------

def rebut_save(source, seg, content):
    """收藏一条回怼。返回新记录 id(内容为空返回 0)。"""
    content = str(content or "").strip()
    if not content:
        return 0
    with _db_lock:
        cur = db().execute("INSERT INTO rebut_saved(source, seg, content, ts) VALUES(?,?,?,?)",
                           (_norm_rebut_text(source), str(seg or "")[:16], content[:2000],
                            int(time.time())))
        db().commit()
        return int(cur.lastrowid or 0)


def rebut_saved_list(q="", limit=60):
    """收藏列表(按时间倒序); q 非空时按原话或正文模糊匹配。"""
    sql = "SELECT id, source, seg, content, ts FROM rebut_saved"
    args = []
    q = str(q or "").strip()
    if q:
        sql += " WHERE source LIKE ? OR content LIKE ?"
        args = ["%" + q + "%", "%" + q + "%"]
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _db_lock:
        rows = db().execute(sql, args).fetchall()
    return [{"id": r[0], "source": r[1] or "", "seg": r[2] or "",
             "content": r[3] or "", "ts": r[4]} for r in rows]


def rebut_saved_delete(rid):
    with _db_lock:
        db().execute("DELETE FROM rebut_saved WHERE id=?", (int(rid),))
        db().commit()
    return True


# ---------------- 反八股正则: 识别/清洗回复里的 AI 腔(网上常见 AI 味检测式样) ----------------

_AIFLAVOR_SUB = [   # (正则, 替换): 套话删除或换成口语
    (r"^(首先|其次|再者|再次|最后)[，,]\s*", ""),
    (r"值得注意的是[，,]?\s*", ""), (r"需要注意的是[，,]?\s*", ""),
    (r"不得不说[，,]?\s*", ""), (r"不可否认[，,]?\s*", ""),
    (r"毫无疑问[，,]?\s*", ""), (r"显而易见[，,]?\s*", "明摆着，"),
    (r"众所周知[，,]?\s*", "谁都"),
    (r"总的来说[，,]?\s*", "说白了，"), (r"总而言之[，,]?\s*", "说到底，"),
    (r"综上所述[，,]?\s*", "说到底，"), (r"归根结底[，,]?\s*", "说到底，"),
    (r"简而言之[，,]?\s*", "说白了，"), (r"换句话说[，,]?\s*", "说白了，"),
    (r"事实上[，,]?\s*", "其实"), (r"某种程度上[，,]?\s*", "多少"),
    (r"在这个[^，。]{0,10}的时代[，,]?\s*", ""), (r"从[^，。]{0,8}角度(来看|而言)[，,]?\s*", ""),
    (r"——+", "，"), (r"─+", "，"),
]
_AIFLAVOR_DETECT = re.compile(
    r"首先|其次|再者|值得注意的是|需要注意的是|不得不说|不可否认|毫无疑问|显而易见|"
    r"总的来说|总而言之|综上所述|归根结底|简而言之|换句话说|事实上|某种程度上|"
    r"众所周知|在这个[^，。]{0,10}的时代|从[^，。]{0,8}角度|一方面|另一方面|毋庸置疑|"
    r"深度剖析|一针见血|让我们|普罗大众|这不仅仅是")


def _deflavor(text):
    """反八股清洗: 套话替换/删除 + 引号剥离(真人打评论不加引号)。"""
    for pat, rep in _AIFLAVOR_SUB:
        text = re.sub(pat, rep, text)
    return re.sub(r"[「」『』“”‘’\"']", "", text).strip()


# ---------------- 写作硬规则(用户给定: 每次生成都注入提示词, 产出后再清洗一遍) ----------------

_STYLE_RULES = (
    "写作硬规则(逐条强制, 违反任意一条都算失败, 没有例外):\n"
    "1. 禁止「不是…而是…」「要…而不是…」「而不是」以及一切同类对比句式; "
    "没有需要对比的对象时, 不要凭空对比。\n"
    "2. 禁止分阶段措辞(先做第一版、之后再观察、分稳妥与激进), 直接给最终结果。\n"
    "3. 禁止罗列不满足要求的无关内容, 搜到用不上的东西一律不写出来。\n"
    "4. 禁止总结与总起: 不许出现「上述内容…下面详细拆开」「一句话总结:…」「综上」这类开头或结尾。\n"
    "5. 用词用两个及以上的完整形式, 禁止单字缩写: 崩溃不写崩, 死亡不写死, 判定不写判, "
    "推断不写推, 抛出不写抛, 挂起不写挂。代码标识符保持英文原名。禁止生造名词。\n"
    "6. 禁止「落地」「钉死」「对齐」这类黑话, 用任何人都看得懂的日常词汇。\n"
    "7. 禁止「栈」字(技术栈、模型栈), 直接说具体事物。"
)

# 事后清洗: 黑话换日常词 + 单字缩写补全 + 总结式开头删除
_STYLE_SUB = [
    (r"技术栈", "使用的技术"), (r"模型栈", "全部模型"), (r"全栈", "全部环节"),
    (r"落地", "实施"), (r"钉死", "确定"), (r"对齐", "统一"),
    (r"崩(?=[了掉]|$|[，。！？；、\s])", "崩溃"),
    (r"死(?=[了掉]|$|[，。！？；、\s])", "死亡"),
    (r"判(?=[了掉]|$|[，。！？；、\s])", "判定"),
    (r"推(?=[了掉]|$|[，。！？；、\s])", "推断"),
    (r"抛(?=[了掉]|$|[，。！？；、\s])", "抛出"),
    (r"挂(?=[了掉]|$|[，。！？；、\s])", "挂起"),
    (r"^(?:一句话总结|总结一下|总结|综上|综上所述)[:：,，]?\s*", ""),
    (r"^(?:上述内容|上面这些|以上内容)[^。！？]{0,14}[，,]?\s*(?:下面|接下来)[^。！？]{0,12}[。！？]\s*", ""),
    (r"一句话总结[:：,，]?\s*", ""),
]

_STYLE_DETECT = re.compile(
    r"不是[^，。；！？]{0,20}而是|要[^，。；！？]{0,20}而不是|而不是|"
    r"一句话总结|综上所述|上述内容|下面详细|下面拆开|"
    r"落地|钉死|对齐|栈|"
    r"(?:^|[，。！？；、\s])[崩死判推抛挂](?=[了掉]|$|[，。！？；、\s])")


def _style_fix(text):
    """写作硬规则清洗: 黑话换日常词、单字缩写补全、总结式开头删除。"""
    t = str(text or "")
    for pat, rep in _STYLE_SUB:
        t = re.sub(pat, rep, t, flags=re.M)
    return t.strip()


def _style_violations(*texts):
    """收集命中写作硬规则的写法(供重写重试点名)。"""
    hits = []
    for t in texts:
        for m in _STYLE_DETECT.finditer(str(t or "")):
            s = m.group(0).strip()
            if s and s not in hits:
                hits.append(s)
    return hits


def _claims_block(claims):
    """把拆解出的论点排成提示词清单。"""
    return "\n".join("%d. %s" % (i, c["point"]) for i, c in enumerate(claims, 1)) if claims else ""


def assist_result(mode, text, evidence, claims=(), emit=None):
    """基于已收集的证据+论点拆解调 LLM 生成结构化结果, 返回 (result, 证据卡列表)。
    思考/刻薄/条数档位从设置读取(面板 ⚙ 调节, 服务端持久化)。
    mode=rebut 时走三段式对线生成, emit(kind, payload) 让每段产出后立刻推给用户。"""
    cb = _claims_block(claims)
    think = sget("assist_think")
    savage = sget("assist_savage")
    mt_mul = {"快": 0.6, "标准": 1.0, "深挖": 1.5}.get(think, 1.0)
    think_line = {"快": "\n(用户选了快速模式: 直接给结论, 不要展开推理过程。)",
                  "深挖": "\n(用户选了深挖模式: 多角度交叉验证每条证据, 谨慎权衡后再下结论。)"}.get(think, "")
    if mode == "rebut":
        return assist_rebut(text, evidence, claims, emit)
    # 知识误区与逻辑谬误分析常驻: 不再由开关控制
    fallacy_note = ("必须点名说法里的知识误区(事实性错误)与逻辑谬误, 谬误名称只从这份清单里挑、"
                    "照抄不要自创: " + "、".join(k for k, _d in _FALLACY_TAXONOMY)
                    + "; 拿不准的不标, 并把命中的名称写进 fallacies 字段。")
    system = ("你是严谨的事实核查员。只依据给出的网络证据判断说法真伪, 不臆测; "
              "证据不足或互相矛盾时必须判存疑。只输出一个JSON对象, 不要输出任何其它文字。")
    cseg = ("【说法拆解出的分论点】\n%s\n\n" % cb) if cb else ""
    ask = ("判断该说法整体真伪, 并对每个分论点分别给出判定。只输出JSON: "
           "{\"verdict\": \"true|false|uncertain\", \"confidence\": 0到100的整数(判定置信度), "
           "\"summary\": \"一句话总结论\", \"reasons\": [\"总体理由(引用证据编号如[E1])\"共2到4条], "
           "\"claims\": [{\"point\": \"分论点原文\", \"check\": \"true|false|uncertain\", "
           "\"note\": \"一句话判定依据(引用证据编号)\"}], "
           "\"fallacies\": [\"知识误区或谬误名称\"], "
           "\"evidence_used\": [\"E1\"等实际使用的证据编号]}。"
           "注意: 判true就给证明其为真的理由, 判false就给证明其为假的理由, "
           "理由必须能在证据中找到依据; 分论点没有对应证据时判 uncertain。" + fallacy_note)
    if evidence:
        user = ("【待核查说法】\n%s\n\n%s【网络证据】(可信度: 权威>可靠>参考; "
                "优先采用权威与可靠级里的具体数字、名称、时间、机构名; "
                "参考级为自媒体/论坛, 不得作为结论的唯一依据)\n%s\n\n%s\n\n%s"
                % (text[:_ASSIST_MAX_TEXT], cseg, evidence_block(evidence), ask, _STYLE_RULES))
    else:
        user = ("【待核查说法】\n%s\n\n%s(网络搜索暂不可用, 请基于你已有的知识谨慎判断, "
                "证据不足就判 uncertain)\n只输出JSON: {\"verdict\": \"true|false|uncertain\", "
                "\"confidence\": 0到100的整数, \"summary\": \"一句话结论\", "
                "\"reasons\": [\"理由\"共2到4条], "
                "\"claims\": [{\"point\": \"分论点\", \"check\": \"true|false|uncertain\", \"note\": \"一句话\"}], "
                "\"evidence_used\": []}\n\n%s"
                % (text[:_ASSIST_MAX_TEXT], cseg, _STYLE_RULES))
    try:
        res = _assist_llm_json(system, user + think_line, int(2000 * mt_mul), 0.2)
    except ValueError:
        # JSON 两次都没按格式来 → 纯文本兜底(保证总有结果)
        fb = ("【待核查说法】\n%s\n\n%s\n\n改用纯文本回答: 第一行必须是「结论：真实」「结论：虚假」"
              "或「结论：存疑」，并附（置信度NN%%）与一句话说明; 随后用2-4行、每行以-开头给出理由"
              "(不要贴链接)。不要输出JSON。\n\n%s"
              % (text[:_ASSIST_MAX_TEXT],
                 ("【网络证据】\n" + evidence_block(evidence)) if evidence
                 else "(网络搜索暂不可用, 请谨慎判断)", _STYLE_RULES))
        content, _ = call_llm([{"role": "system", "content": system},
                               {"role": "user", "content": fb}],
                              max_tokens=1000, temperature=0.2)
        res = _verify_from_text(content, evidence)
    verdict = res.get("verdict")
    res["verdict"] = verdict if verdict in ("true", "false", "uncertain") else "uncertain"
    try:
        res["confidence"] = max(0, min(100, int(res.get("confidence") or 60)))
    except Exception:
        res["confidence"] = 60
    res["summary"] = _style_fix(str(res.get("summary") or "").strip())[:200]
    res["reasons"] = [_style_fix(str(x).strip())[:300]
                      for x in (res.get("reasons") or []) if str(x).strip()][:5]
    claims_out = []
    for c in (res.get("claims") or [])[:4]:
        chk = c.get("check")
        claims_out.append({"point": str(c.get("point") or "").strip()[:120],
                           "check": chk if chk in ("true", "false", "uncertain") else "uncertain",
                           "note": _style_fix(str(c.get("note") or "").strip())[:200]})
    res["claims"] = claims_out
    res["fallacies"] = [str(x).strip()[:20] for x in (res.get("fallacies") or [])
                        if str(x).strip()][:4]
    refs = _norm_evidence(evidence, set(res.get("evidence_used") or []) or None)
    res["evidence_used"] = [e["id"] for e in refs]
    res["offline"] = not evidence
    return res, refs


# ---------------- 三段式对线生成(一句话/一段话/长篇, 每段产出后立刻推送) ----------------

_LEN_TIER = {"1": 1, "2": 2, "3": 3, "短": 1, "中": 2, "长": 3}   # 后三个为旧档位兼容
_SEG_SPEC = {
    "lines": ("一句话", 40,
              "每条只写一句话, 不超过30字, 不带编号、不带前缀、不带解释"),
    "paras": ("一段话", 170,
              "每条写一段话, 80到140字, 2到4句, 至少把一个证据里的具体数字、名称或时间写进句子"),
    "long":  ("长篇", 520,
              "写一整篇长的, 250到400字, 3到5句: 先把对方的知识误区与逻辑谬误整体驳一遍, "
              "再逐条拆掉最要命的两三个, 收尾甩一句狠话"),
}
_SEG_MAX = {"lines": 4, "paras": 4, "long": 1}   # 长篇最多 1 条
_SEG_ORDER = ("lines", "paras", "long")


def assist_len_n():
    """输出档位 → 条数(1/2/3)。档位只调节条数: 一句话与一段话各 N 条, 长篇恒 1 条。"""
    return _LEN_TIER.get(str(sget("assist_len") or "2").strip(), 2)


# 风格档位: 有代入感的人设名(参考怼怼侠的做法: 档位要有名字才好挑)。
# 旧值 温和/毒舌/阴阳 自动折算到新档位, 老存档不用改。
_SAVAGE_TIER = {"冷静理性": "冷静理性", "毒舌律师": "毒舌律师", "阴阳怪气": "阴阳怪气",
                "温和": "冷静理性", "毒舌": "毒舌律师", "阴阳": "阴阳怪气"}
_SAVAGE_TEMP = {"冷静理性": 0.6, "毒舌律师": 0.85, "阴阳怪气": 0.95}
# 身份档位: 决定「我」拿什么当武器。与语气风格解耦——风格是说话方式, 身份是底气来源。
_PERSONA_TIER = ("不设", "理工科", "行业老手", "数据党", "路人")
_PERSONA_LINE = {
    "理工科": "你的底气来自原理与机制: 讲清事情怎么运转、因果链哪一环被对方说反了, "
              "用「按原理说」「这个机制是」这类口吻。",
    "行业老手": "你的底气来自一线经验: 用行业惯例、真实流程、干过才知道的细节压对方, "
                "用「我在这行干过」「实际流程是」这类口吻。",
    "数据党": "你的底气来自数据与出处: 盯住统计口径、样本范围、发布机构, "
              "用「这个数谁发布的」「口径覆盖哪些」这类口吻。",
    "路人": "你的底气来自常识与生活经验: 用大白话讲常理, "
            "用「常识上」「过日子的人都明白」这类口吻。",
}
# 自定义人设预设(参考 Persona-Forge 的做法: 把身份拆成结构化字段存本地, 可随时切换)
# 存 settings.assist_personas, 形如 [{"name","identity","weapon","taboo"}]
_PERSONA_MAX = 12
_PERSONA_FIELD = (("identity", "身份", 40), ("weapon", "底气/武器", 40), ("taboo", "绝不", 30))


def persona_presets():
    """读自定义人设预设。坏数据一律当空处理(不能因为一条脏 JSON 让整个对线功能挂掉)。"""
    raw = sget("assist_personas")
    try:
        arr = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
    except Exception:
        return []
    out = []
    for it in (arr if isinstance(arr, list) else [])[:_PERSONA_MAX]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:12]
        if not name or name in _PERSONA_TIER:
            continue   # 名字空或撞内置档位 → 丢弃(否则身份行会出现两个同名按钮)
        row = {"name": name}
        for k, _lbl, lim in _PERSONA_FIELD:
            row[k] = str(it.get(k) or "").strip()[:lim]
        out.append(row)
    return out


def persona_presets_save(items):
    """整表覆盖保存, 返回保存后的条数。"""
    clean = []
    for it in (items or [])[:_PERSONA_MAX]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:12]
        if not name or name in _PERSONA_TIER:
            continue
        row = {"name": name}
        for k, _lbl, lim in _PERSONA_FIELD:
            row[k] = str(it.get(k) or "").strip()[:lim]
        if any(row[k] for k, _l, _x in _PERSONA_FIELD):
            clean.append(row)   # 只有名字没有内容的预设没用, 不收
    sset("assist_personas", json.dumps(clean, ensure_ascii=False))
    return len(clean)


def _savage_key(savage):
    """风格档位折算(旧值 温和/毒舌/阴阳 → 新的人设名)。"""
    return _SAVAGE_TIER.get(str(savage or "").strip(), "毒舌律师")


def _persona_valid(persona):
    """档位名合法: 内置身份, 或用户自定义预设名。"""
    p = str(persona or "").strip()
    return p in _PERSONA_TIER or any(pp["name"] == p for pp in persona_presets())


def _persona_key(persona):
    p = str(persona or "").strip()
    return p if _persona_valid(p) else "不设"


def _persona_line(persona):
    """身份设定文案: 内置档位取内置文案, 自定义预设按用户填的字段拼。"""
    p = _persona_key(persona)
    if p == "不设":
        return ""
    if p in _PERSONA_LINE:
        return _PERSONA_LINE[p]
    for pp in persona_presets():
        if pp["name"] == p:
            seg = []
            if pp.get("identity"):
                seg.append("你的身份是" + pp["identity"] + "。")
            if pp.get("weapon"):
                seg.append("你的底气来自" + pp["weapon"] + "。")
            if pp.get("taboo"):
                seg.append("你绝不" + pp["taboo"] + "。")
            return "".join(seg)
    return ""


def _savage_line(savage):
    return {"冷静理性": "语气克制, 平静指出对方错在哪, 不讽刺; 但仍然是第一人称直接对线, 不做旁观点评。",
            "阴阳怪气": "大量反讽与阴阳怪气(假装夸赞、捧杀式讽刺), 用贴吧对线的辛辣用语"
                        "(就这、绷不住、乐、你开心就好、建议回炉重造、先把基础补上), 刻薄值拉满。"}.get(
        _savage_key(savage), "犀利刻薄带讽刺, 像贴吧评论区真人直接开怼, 句子短、节奏快、不留情面。")


def _rebut_system(savage, persona="不设"):
    pl = _persona_line(persona)
    return ("你是这场网络对线的当事人。第一人称「我」直接对着对方「你」说话, 拿自己的立场"
            "和对方吵到底; 不许当旁观顾问, 不许出现「你可以这样回复」「建议你这么说」"
            "「你可以告诉他」这类替第三方出主意的话。每一条都必须带出「我」的立场"
            "(我拿数据跟你说话、我劝你别急着下结论、我看到的是、我这儿的数据是), "
            "对手一律称「你」, 不许写成没有主语的旁白或客观陈述。"
            + (("你的身份设定: " + pl + " ") if pl else "")
            + "语气要求: %s "
            "禁止脏话、歧视、攻击外貌与家人、编造数据。"
            "只输出一个JSON对象, 不要输出任何其它文字。" % _savage_line(savage))


def _rebut_context(text, evidence, claims, think):
    """三段共用的上下文: 对方言论 + 拆解论点 + 网络证据 + 参考话术 + 写作硬规则。"""
    think_line = {"快": "\n(用户选了快速模式: 直接开怼, 不要展开推理过程。)",
                  "深挖": "\n(用户选了深挖模式: 每条都交叉核对证据, 拿准了再开口。)"}.get(think, "")
    cb = _claims_block(claims)
    cseg = ("【对方言论拆解出的论点】\n%s\n\n" % cb) if cb else ""
    talk_pool = fetch_talk_templates()
    sampled = (random.sample(talk_pool, min(8, len(talk_pool))) if talk_pool else []) + \
        random.sample(_BUILTIN_TALK, 5)
    random.shuffle(sampled)
    talk_block = ("【参考对线模板】(只借鉴句式和语气, 禁止照抄原句, 事实必须来自下面的证据)\n"
                  + "\n".join("- " + t for t in sampled) + "\n\n") if sampled else ""
    if evidence:
        ev = ("【网络证据】(可信度: 权威>可靠>参考; 优先把权威与可靠级里的具体数字、名称、时间、"
              "机构名砸进句子里; 参考级是论坛与自媒体, 只能借语气, 不能当论据)\n%s\n\n"
              % evidence_block(evidence))
    else:
        ev = "(网络搜索暂不可用, 基于公开常识开怼)\n\n"
    return ("【对方言论】\n%s\n\n%s%s%s%s%s"
            % (text[:_ASSIST_MAX_TEXT], cseg, ev, talk_block, _STYLE_RULES, think_line))


# 谬误分类体系取自 LOGIC 数据集(EMNLP 2022 Findings, causalNLP/logical-fallacy)的 13 类,
# 并给每类配一句"判定骨架"(对应原数据集里的 Logical Form 字段): 只给名称时模型容易乱扣帽子,
# 给它一个可套用的形式, 点名会准得多。
_FALLACY_TAXONOMY = (
    ("以偏概全", "少数样本推出整体: 样本只占群体极小一部分, 结论却套到整个群体上"),
    ("虚假因果", "先后即因果: A 发生在 B 之前, 就断定 A 导致了 B"),
    ("循环论证", "X 成立因为 Y, Y 成立因为 X, 绕回起点什么都没证明"),
    ("诉诸流行", "很多人相信 X, 所以 X 成立"),
    ("人身攻击", "不驳论点, 转而攻击提出论点的人"),
    ("形式逻辑错误", "肯定后件: 若 A 则 B, B 成立, 就断定 A 成立"),
    ("诉诸情感", "论点拿不出证据, 改用情绪代替论证"),
    ("假两难", "只摆两个选项, 而实际还有别的选择: 要么 X 要么 Y"),
    ("偷换概念", "同一个词在前提和结论里含义不同"),
    ("稻草人", "把对方观点歪曲夸大成另一个版本, 再打那个版本"),
    ("无关论据", "拿与论点毫无关系的前提来支持结论"),
    ("诉诸不可信", "不驳论点, 只说对方不是可靠信源"),
    ("故意错误论证", "明知主张站不住, 仍用错误的论证硬撑"),
)
_FALLACY_LIST = "".join("%d) %s——%s；" % (i, k, d)
                        for i, (k, d) in enumerate(_FALLACY_TAXONOMY, 1))

_TARGET_ASK = (
    "先拆对方这段话的问题, 分两类:\n"
    "一、知识误区: 事实性错误(数字、时间、机构、机制、因果说错了)。\n"
    "二、逻辑谬误: 只从下面这份清单里挑, 名称照抄, 不要自创名称——\n" + _FALLACY_LIST + "\n"
    "每条给出: 对方原话里的说法、类型名称(知识误区, 或清单里的名称)、"
    "一句话说清错在哪(要说明为什么命中该类型的判定骨架)。\n"
    "硬性要求: 只标真正命中的, 拿不准就不标; 同一句最多标两个类型; 宁少勿滥, "
    "标错类型比少标更糟。\n"
    "如果这段话根本没有可怼的点(正常反馈、客观陈述、感谢、闲聊), 输出 "
    "{\"no_target\": true, \"reason\": \"为什么没有可怼点(一句话)\"}。\n"
    "只输出JSON: {\"targets\": [{\"point\": \"对方原话里的说法\", \"kind\": \"类型名称\", "
    "\"note\": \"一句话说清错在哪\"}], \"stance_note\": \"一句话说明证据整体倾向\"}")


def _seg_ask(seg, n, targets):
    label, _limit, spec = _SEG_SPEC[seg]
    tseg = ("【要打的点】\n" + "\n".join(
        "%d. %s(%s): %s" % (i, t["point"], t["kind"], t["note"])
        for i, t in enumerate(targets, 1)) + "\n\n") if targets else ""
    return (tseg + "现在写 %d 条%s。硬性要求: %s。\n"
            "每条都直接对着对方「你」说, 第一人称, 一条换一条攻击路线(反问、摆事实、反讽、"
            "短平快戳一点), 不要条条一个句式。\n"
            "去AI味: 不许出现引号与书名号, 不许把证据编号写进正文(编号单独放 evidence 字段, "
            "读评论的人看不到编号), 不许说教式收尾。\n"
            "只输出JSON: {\"items\": [{\"text\": \"可粘贴的回复\", \"basis\": \"一句话事实依据\", "
            "\"target\": \"这条打的是哪个误区或谬误\", "
            "\"evidence\": [\"E1\"等本条实际用到的证据编号, 没用证据就留空数组]}]}" % (n, label, spec))


def _norm_targets(raw):
    """规整误区/谬误清单。"""
    out = []
    for t in (raw or [])[:6]:
        if isinstance(t, str):
            p, k, nt = str(t).strip()[:80], "", ""
        else:
            p = str(t.get("point") or "").strip()[:80]
            k = _style_fix(str(t.get("kind") or "").strip())[:24]
            nt = _style_fix(str(t.get("note") or "").strip())[:160]
        if p or k:
            out.append({"point": p, "kind": k, "note": nt})
    return out


def _targets_from_text(content):
    """纯文本兜底解析误区/谬误清单: 每行「对方原话 | 问题类型 | 说明」。"""
    out = []
    for ln in (content or "").splitlines():
        ln = re.sub(r"^[-•·*\d.、)\s]+", "", ln).strip()
        if len(ln) < 4:
            continue
        parts = [p.strip() for p in re.split(r"[|｜\t]+", ln) if p.strip()]
        if len(parts) >= 3:
            p, k, nt = parts[0], parts[1], " ".join(parts[2:])
        elif len(parts) == 2:
            p, k, nt = parts[0], parts[1], ""
        else:
            p, k, nt = ln, "", ""
        out.append({"point": _style_fix(p)[:80], "kind": _style_fix(k)[:24],
                    "note": _style_fix(nt)[:160]})
        if len(out) >= 6:
            break
    return out


_TARGET_FB = ("改用纯文本回答: 每行一条, 用竖线分隔三个字段——"
              "对方原话 | 问题类型 | 一句话说清错在哪; 不要编号、不要解释、不要JSON。")


def _norm_seg_items(raw, limit, cap):
    """规整一段的条目: 剥引号、清洗写作硬规则、按段长截断。"""
    out = []
    for it in (raw or [])[:cap]:
        if isinstance(it, str):
            txt, basis, target, evs = it, "", "", []
        else:
            txt = str(it.get("text") or it.get("reply") or "").strip()
            basis = str(it.get("basis") or "").strip()[:200]
            target = str(it.get("target") or "").strip()[:60]
            evs = [str(x).strip().upper()[:8] for x in (it.get("evidence") or [])
                   if str(x).strip()][:4]
        txt = _style_fix(_deflavor(txt))
        if len(txt) < 4:
            continue
        out.append({"text": txt[:limit], "basis": _style_fix(_deflavor(basis)),
                    "target": target, "evidence": evs})
    return out


def _seg_from_text(content, limit, cap):
    """纯文本兜底: 一行一条。"""
    out = []
    for ln in (content or "").splitlines():
        t = _style_fix(_deflavor(re.sub(r"^[-•·*\d.、)\s]+", "", ln).strip()))
        if len(t) >= 4:
            out.append({"text": t[:limit], "basis": "", "target": ""})
        if len(out) >= cap:
            break
    return out


def _seg_call(system, user, seg, n, mt, temp):
    """生成一段(一句话/一段话/长篇): JSON 两次解析失败 → 纯文本兜底。返回条目列表。"""
    _label, limit, _spec = _SEG_SPEC[seg]
    cap = min(n, _SEG_MAX[seg])
    try:
        res = _assist_llm_json(system, user, mt, temp)
        items = _norm_seg_items(res.get("items"), limit, cap)
        if items:
            return items
    except ValueError:
        pass
    try:
        content, _ = call_llm([{"role": "system", "content": system},
                               {"role": "user", "content":
                                user + "\n\n改用纯文本回答: 每条一行, 直接写可粘贴的回复, "
                                       "不要编号、不要解释、不要JSON, 最多 %d 行。" % cap}],
                              max_tokens=int(900 * (1.0 if seg != "long" else 1.6)),
                              temperature=temp)
        return _seg_from_text(content, limit, cap)
    except Exception:
        return []


def _seg_has_specifics(items, evidence=()):
    """段落里有没有把具体东西砸进去(数字, 或证据标题里的专名)。
    没有证据时无从判断, 一律算通过。"""
    if not evidence:
        return True
    joined = " ".join(it["text"] for it in items)
    if re.search(r"\d", joined):
        return True
    terms = set()
    for e in evidence:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{1,}|[\u4e00-\u9fff]{2,6}",
                            str(e.get("title") or "")):
            terms.add(w)
    return any(t in joined for t in terms)


def _seg_score(items, evidence=()):
    """一段的质量分(越小越好): 人称失守 > 写作硬规则 > 空话 > AI八股。"""
    texts = [it["text"] for it in items]
    person = (0 if any("我" in t for t in texts) else 1) + (0 if any("你" in t for t in texts) else 1)
    return (person, len(_style_violations(*texts)),
            0 if _seg_has_specifics(items, evidence) else 1,
            sum(len(_AIFLAVOR_DETECT.findall(t)) for t in texts))


def _judge_system():
    """裁判角色: 与写手分开一个角色, 只看稿子不写稿(同一个角色自查容易顺着自己第一版附和)。"""
    return ("你是这场网络对线的裁判, 立场中立, 不替任何一方写稿, 也不重写。"
            "只判断这一版稿子能不能直接拿去用, 并指出最该改的地方。\n"
            "淘汰标准(命中任意一条就判 revise):\n"
            "1) 人称失守: 整段没有第一人称「我」, 或没有第二人称「你」, "
            "或写成没有主语的旁白, 或出现替第三方出主意的口气;\n"
            "2) 踩了写作硬规则;\n"
            "3) AI 腔: 套话、排比、总分结构、说教式收尾;\n"
            "4) 空话: 没有任何具体数字、机构名、时间、机制, 只是气势汹汹; "
            "或者打的点跟交给写手的【要打的点】对不上。\n"
            "只输出JSON: {\"verdict\": \"pass\"或\"revise\", "
            "\"problems\": [\"最该改的地方, 最多4条, 每条一句话\"], "
            "\"weakest\": \"哪一条最站不住, 为什么\"}。不要重写, 不要给替代文案。")


def _seg_judge(ask, items, mt):
    """请裁判过一遍稿子。返回 {"verdict","problems","weakest"} 或 None(裁判没答上来)。"""
    draft = "\n".join("%d. %s" % (i, it["text"]) for i, it in enumerate(items, 1))
    try:
        res = _assist_llm_json(
            _judge_system(),
            "【交给写手的要求】\n%s\n\n【写手交上来的稿子】\n%s\n\n请裁判。"
            % ((ask or "")[-1600:], draft),
            min(mt, 900), 0.2)
    except Exception:
        return None
    if not isinstance(res, dict):
        return None
    problems = [_style_fix(str(p).strip())[:120]
                for p in (res.get("problems") or []) if str(p).strip()][:4]
    return {"verdict": str(res.get("verdict") or "").strip().lower(),
            "problems": problems,
            "weakest": _style_fix(str(res.get("weakest") or "").strip())[:160]}


def _seg_polish(system, user, seg, items, n, mt, temp, ask="", evidence=()):
    """独立裁判复核: 机械检查过了就直接放行; 没过就请裁判点名, 再让写手按裁判意见重写一版,
    最后用机械分择优(裁判的审美不管用, 分低的那版一律不采纳)。"""
    _label, limit, _spec = _SEG_SPEC[seg]
    if not items:
        return items
    cap = min(n, _SEG_MAX[seg])
    texts = [it["text"] for it in items]
    bad = _style_violations(*texts)
    # 人称失守: 对线必须是我对你说, 整段没有第一人称或没有第二人称都算没站住
    if not any("我" in t for t in texts):
        bad = bad + ["整段没有第一人称我"]
    if not any("你" in t for t in texts):
        bad = bad + ["整段没有第二人称你"]
    flavor = sorted({m for t in texts for m in _AIFLAVOR_DETECT.findall(t)})
    hits = sum(len(_AIFLAVOR_DETECT.findall(t)) for t in texts)
    # 一句话本来就短, 不强求塞数字; 一段话与长篇不砸具体东西就算空话
    thin = seg != "lines" and not _seg_has_specifics(items, evidence)
    if not bad and hits < 2 and not thin:
        return items   # 机械检查全过就不请裁判, 省一次调用
    problems = list(bad) + list(flavor)
    if thin:
        problems.append("通篇没有具体数字、机构名或时间, 全是空话")
    judge = _seg_judge(ask, items, mt)
    if judge:
        problems += judge["problems"]
        if judge.get("weakest"):
            problems.append("最弱的一条: " + judge["weakest"])
        if judge["verdict"] == "pass" and not bad and not thin:
            return items
    listed = "、".join(dict.fromkeys(p for p in problems if p))[:400]
    try:
        res = _assist_llm_json(
            system, user + "\n\n裁判意见: %s。按裁判意见重写一版, 条数与JSON结构不变, "
            "被点名的问题全部改掉, 像真人随手打的评论。只输出JSON。" % listed, mt, temp)
        items2 = _norm_seg_items(res.get("items"), limit, cap)
    except ValueError:
        return items
    if not items2:
        return items
    return items2 if _seg_score(items2, evidence) < _seg_score(items, evidence) else items


def assist_rebut(text, evidence, claims=(), emit=None, only=None, targets=()):
    """三段式对线: 知识误区与逻辑谬误分析 → n 条一句话 → n 条一段话 → 1 条长篇。
    每段生成完立即通过 emit(kind, payload) 推送, 用户马上就能看到并复制。
    only 指定某一段时只重跑那一段(「换一批」用), 论点沿用 targets, 证据沿用调用方给的。"""
    n = assist_len_n()
    think = sget("assist_think")
    savage = _savage_key(sget("assist_savage"))
    persona = _persona_key(sget("assist_persona"))
    mt_mul = {"快": 0.6, "标准": 1.0, "深挖": 1.5}.get(think, 1.0)
    temp = _SAVAGE_TEMP.get(savage, 0.85)
    system = _rebut_system(savage, persona)
    ctx = _rebut_context(text, evidence, claims, think)
    refs = _norm_evidence(evidence)
    out = {"targets": [], "lines": [], "paras": [], "long": [], "stance_note": "",
           "offline": not evidence}
    if only:   # 「换一批」: 只重跑这一段, 拆解与取证都沿用上一轮
        seg = only if only in _SEG_SPEC else "lines"
        cnt = _SEG_MAX[seg] if seg == "long" else n
        ask = _seg_ask(seg, cnt, targets)
        user = ctx + "\n\n" + ask
        mt = int({"lines": 1200, "paras": 1700, "long": 2400}[seg] * mt_mul)
        items = _seg_polish(system, user, seg,
                            _seg_call(system, user, seg, cnt, mt, temp), cnt, mt, temp,
                            ask=ask, evidence=evidence)
        out[seg] = items
        if emit:
            emit(seg, {"items": items, "regen": True})
        return out, refs
    # 一) 知识误区与逻辑谬误分析(常驻, 不再有开关)
    # 这一步是三段正文的骨架, 给足输出额度: 推理模型在「快」档也容易把JSON写截断
    mt_t = max(2000, int(1800 * mt_mul))
    try:
        res = _assist_llm_json(system, ctx + "\n\n" + _TARGET_ASK, mt_t, 0.4)
    except ValueError:
        try:   # JSON 两次都不成 → 纯文本兜底(分析空了三段就没了要打的点)
            content, _ = call_llm([{"role": "system", "content": system},
                                   {"role": "user", "content": ctx + "\n\n" + _TARGET_ASK
                                    + "\n\n" + _TARGET_FB}],
                                  max_tokens=1200, temperature=0.4)
            res = {"targets": _targets_from_text(content)}
        except Exception:
            res = {}
    if res.get("no_target"):   # 模型判定无可回怼: 立即止损, 不再触发任何生成
        out["no_target"] = True
        out["reason"] = _style_fix(str(res.get("reason") or
                                       "这条是正常反馈或客观陈述, 没有可回怼的点"))[:200]
        return out, refs
    out["targets"] = _norm_targets(res.get("targets"))
    out["stance_note"] = _style_fix(str(res.get("stance_note") or ""))[:200]
    if emit:
        # 顺带把证据摘要推给界面, 后面几段的编号就能点开溯源
        emit("targets", {"items": out["targets"], "stance_note": out["stance_note"],
                         "evidence": _evidence_brief(refs)})
    # 二) 三段正文: 每段独立生成, 生成完立刻推送
    for seg in _SEG_ORDER:
        cnt = _SEG_MAX[seg] if seg == "long" else n
        ask = _seg_ask(seg, cnt, out["targets"])
        user = ctx + "\n\n" + ask
        mt = int({"lines": 1200, "paras": 1700, "long": 2400}[seg] * mt_mul)
        items = _seg_polish(system, user, seg,
                            _seg_call(system, user, seg, cnt, mt, temp), cnt, mt, temp,
                            ask=ask, evidence=evidence)
        out[seg] = items
        if emit:
            emit(seg, {"items": items})
    if not (out["lines"] or out["paras"] or out["long"]):
        raise RuntimeError("没能生成出可用的回复")
    return out, refs


# ---------------- 回怼结果缓存 + 上一轮上下文(供「换一批」复用论点与证据) ----------------

# 同一段原话 + 同一套档位 → 直接命中, 省掉拆解/取证/四次生成(参考抖音直播助手的 LRU 回复缓存)
_REBUT_CACHE = {}
_REBUT_TTL = 1800
_REBUT_MAX = 24
_LAST_REBUT = {"key": "", "ts": 0.0, "evidence": [], "claims": [], "targets": []}


def _norm_rebut_text(text):
    return re.sub(r"\s+", "", str(text or ""))[:300]


def rebut_cache_key(text):
    """缓存键必须带上全部档位: 换了身份/风格/条数就是另一份结果, 不能命中旧缓存。"""
    return "|".join((_norm_rebut_text(text), str(sget("assist_think")),
                     _savage_key(sget("assist_savage")),
                     _persona_key(sget("assist_persona")), str(assist_len_n())))


def rebut_cache_get(key):
    hit = _REBUT_CACHE.get(key)
    if hit and time.time() - hit[0] < _REBUT_TTL:
        return hit[1]
    _REBUT_CACHE.pop(key, None)
    return None


def rebut_cache_put(key, payload):
    if len(_REBUT_CACHE) >= _REBUT_MAX:   # 防膨胀: 超限清掉最旧一半
        for k in sorted(_REBUT_CACHE, key=lambda k: _REBUT_CACHE[k][0])[:_REBUT_MAX // 2]:
            _REBUT_CACHE.pop(k, None)
    _REBUT_CACHE[key] = (time.time(), payload)


def last_rebut_remember(text, evidence, claims, targets):
    _LAST_REBUT.update(key=_norm_rebut_text(text), ts=time.time(),
                       evidence=list(evidence or []), claims=list(claims or []),
                       targets=list(targets or []))


def last_rebut_recall(text):
    """取上一次同一条原话的上下文; 原话不同或过期就返回 None。"""
    if _LAST_REBUT["key"] and _LAST_REBUT["key"] == _norm_rebut_text(text) \
            and time.time() - _LAST_REBUT["ts"] < _REBUT_TTL:
        return _LAST_REBUT
    return None


def user_context():
    """用户偏好上下文: 偏好备注 + 聊天中的意愿 + 反馈摘要。"""
    parts = []
    note = sget("prefs_note").strip()
    if note:
        parts.append("用户偏好备注: " + note)
    dislikes = sget("dislike_note").strip()
    if dislikes:
        parts.append("用户厌恶/不想看的内容: " + dislikes)
    rows = db().execute(
        "SELECT content FROM chat WHERE role='user' AND cid=? ORDER BY id DESC LIMIT 5",
        (conv_cur(),)).fetchall()
    if rows:
        parts.append("用户近期的聊天意愿: " + " / ".join(r[0][:60] for r in reversed(rows)))
    likes = db().execute("SELECT author FROM feedback WHERE action='like' GROUP BY author ORDER BY COUNT(*) DESC LIMIT 5").fetchall()
    dislikes = db().execute("SELECT author FROM feedback WHERE action='dislike' GROUP BY author ORDER BY COUNT(*) DESC LIMIT 5").fetchall()
    if likes:
        parts.append("用户喜欢的作者: " + ", ".join(r[0] for r in likes))
    if dislikes:
        parts.append("用户不喜欢的作者: " + ", ".join(r[0] for r in dislikes))
    try:   # 隐式信号(感知层): 近期点击内容的主题/标签/分区
        hrows = db().execute("SELECT key FROM history ORDER BY ts DESC LIMIT 30").fetchall()
        hitems = [item_by_key(k) for (k,) in hrows]
        hitems = [it for it in hitems if isinstance(it, dict)]
        if hitems:
            topts = []
            for it in hitems:
                for t in ((it.get("topic") or "").split("、") + (it.get("tag") or "").split(",")
                          + [it.get("area") or "", it.get("tname") or ""]):
                    t = t.strip()
                    if 2 <= len(t) <= 8 and t not in topts:
                        topts.append(t)
            if topts:
                parts.append("用户最近点击内容的主题(隐式兴趣): " + "、".join(topts[:15]))
    except Exception:
        pass
    acct = account_slim()
    if acct.get("ok"):
        if acct["authors"]:
            parts.append("B站账号常看作者: " + ", ".join(a["name"] for a in acct["authors"][:6]))
        if acct["keywords"]:
            parts.append("B站账号历史高频词: " + ", ".join(acct["keywords"][:8]))
        if acct["favs"]:
            parts.append("B站账号收藏夹: " + "、".join(f["title"] for f in acct["favs"][:3]))
    return "\n".join(parts)


# ---------------------------------------------------------------- B站账号(登录后优化推荐)

ACCOUNT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
STOPWORDS = set("""这个 那个 什么 怎么 为什么 如何 一个 不是 就是 还是 但是 因为 所以 如果 然后 以及
可以 自己 我们 他们 你们 现在 没有 已经 真的 虽然 不过 可能 应该 觉得 知道 看到 都是 都是 我是 爱你 喜欢
视频 动画 游戏 电影 系列 官方 完整 实况 合集 盘点 评测 试玩 解说 攻略 教程 第 第几 原创 转载 资讯 新闻 进 了 过 吧
日常 一天 最后 最新 神作 双人 推荐 高分 短评 剧集 片花 预告 正片 花絮 特辑 合集 未删减 高清 中字 生肉""".split())


def account_cookie():
    return sget("bili_cookie")


def bili_get(url):
    # v0.3.1.66: 账号类接口(登录态/未读/收藏/标签/播放地址)也排一下队。这里只拖延不拒绝 ——
    # 被限流挡回去会让登录态误判成"未登录", 那比慢 200 毫秒糟得多。
    _net_pace("bili-api", 0.25)
    req = urllib.request.Request(url, headers=dict(ACCOUNT_HEADERS, Cookie=account_cookie()))
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))


def bili_is_logged_in():
    """用B站认证接口判断当前Cookie是否有效。"""
    if not account_cookie():
        return False
    try:
        nav = bili_get("https://api.bilibili.com/x/web-interface/nav")
        return nav.get("code") == 0 and bool((nav.get("data") or {}).get("isLogin"))
    except Exception:
        return False


def bili_unread_messages():
    """读取B站账号真实未读消息数; 接口失败或未登录时返回None。"""
    if not bili_is_logged_in():
        return None
    try:
        d = bili_get("https://api.bilibili.com/x/msgfeed/unread")
        if d.get("code") != 0:
            return None
        data = d.get("data") or {}
        # 只统计评论/回复与@我，不将点赞、系统通知或关注动态计入消息红点。
        keys = ("reply", "at")
        values = [data.get(k) for k in keys if isinstance(data.get(k), (int, float))]
        return max(0, int(sum(values))) if values else 0
    except Exception:
        return None


def account_slim():
    """返回给前端/评分用的账号画像摘要(带缓存)。"""
    try:
        profile = json.loads(sget("acct_profile"))
    except Exception:
        profile = None
    if not profile:
        return {"ok": False, "nickname": sget("acct_nickname"), "profile": None,
                "authors": [], "keywords": [], "favs": [], "history_n": 0, "face": ""}
    return {"ok": True, "nickname": profile.get("nickname", ""),
            "synced_at": profile.get("synced_at", 0),
            "authors": profile.get("authors", []),
            "keywords": profile.get("keywords", []),
            "favs": profile.get("favs", []),
            "history_n": profile.get("history_n", 0),
            "face": profile.get("face", "")}


def fetch_account_profile():
    """拉取观看历史/收藏夹 → 统计常看作者与高频关键词。失败抛异常。"""
    nav = bili_get("https://api.bilibili.com/x/web-interface/nav")
    if nav.get("code") != 0 or not (nav.get("data") or {}).get("isLogin"):
        raise RuntimeError("Cookie 无效或已过期，请重新登录")
    data = nav["data"]
    nickname, mid = data.get("uname", ""), data.get("mid", 0)

    authors, titles = [], []
    try:
        h = bili_get("https://api.bilibili.com/x/web-interface/history/cursor?ps=30&type=video")
        for it in (h.get("data") or {}).get("list") or []:
            a = it.get("author_name") or ((it.get("owner") or {}) or {}).get("name", "")
            if a:
                authors.append(a)
            if it.get("title"):
                titles.append(it["title"])
    except Exception:
        pass

    favs = []
    try:
        f = bili_get("https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid=%d" % mid)
        for fo in (f.get("data") or {}).get("list") or []:
            favs.append({"title": fo.get("title", ""), "count": fo.get("media_count", 0)})
        if favs:
            r = bili_get("https://api.bilibili.com/x/v3/fav/resource/list?media_id=%s&pn=1&ps=20&platform=web"
                         % favs[0].get("id", ""))
            for m in ((r.get("data") or {}).get("medias") or [])[:20]:
                if m.get("title"):
                    titles.append(m["title"])
    except Exception:
        pass

    auth_cnt = {}
    for a in authors:
        auth_cnt[a] = auth_cnt.get(a, 0) + 1
    top_authors = [{"name": k, "count": v} for k, v in
                   sorted(auth_cnt.items(), key=lambda x: -x[1])[:8]]
    kw_cnt = {}
    for t in titles:
        for tok in re.split(r"[\s:：|丨_·\-~\[\]【】()（）,，。！？!?\"'“”]+", t):
            tok = tok.strip()
            if 2 <= len(tok) <= 12 and tok not in STOPWORDS:
                kw_cnt[tok] = kw_cnt.get(tok, 0) + 1
    top_kw = [k for k, _ in sorted(kw_cnt.items(), key=lambda x: -x[1])[:12]]

    profile = {"nickname": nickname, "mid": mid, "synced_at": int(time.time()),
               "history_n": len(authors), "authors": top_authors,
               "keywords": top_kw, "favs": favs[:5],
               "face": data.get("face", "")}
    sset("acct_profile", json.dumps(profile, ensure_ascii=False))
    sset("acct_nickname", nickname)
    return profile


_ABLOCK_PAGE = 50          # 接口硬上限: ps 传多大都只回 50 条(实测 ps=500 仍回 50)
_ABLOCK_MAX_PAGES = 80     # 上限保护(4000 人), 防接口异常时无限翻页
_ABLOCK_GAP = 0.4          # 页间隔(秒): 16 页约 8-10 秒。实测连发会把 B站风控招来
                           # (acc/info 随即返回 -352「风控校验失败」), 所以宁可慢一点。


def fetch_account_blocklist():
    """读取 B站账号拉黑名单 → 写入本地 ablock 表。返回写入条数。

    v0.3.1.32c: 这个接口是**分页**的, 且 ps 传多大都只回 50 条。
    以前只取第一页 —— 而实测账号里有 778 个拉黑 = 16 页, 本地名单因此只覆盖 1/16,
    「屏蔽黑名单内用户的弹幕」只能命中极小一部分(753 个哈希里只算得出 50 个),
    这就是"按昵称拉黑了却还看得见它弹幕"的根子。
    现在翻完全部页; 只有**完整拿全**时才做镜像替换(清掉已不在名单里的老行),
    任何后续页失败都退化成"只增量补写", 绝不误删。
    """
    seen = {}
    fetched = 0                 # 注意: 必须数"取回条数", 不能数 len(seen) ——
                                # 同一昵称可能有多人(实测 778 条里只有 753 个不同昵称),
                                # 用 len(seen) 判"翻完了"会永远判不出来。
    total = None
    complete = False
    for pn in range(1, _ABLOCK_MAX_PAGES + 1):
        d = bili_get("https://api.bilibili.com/x/relation/blacks?pn=%d&ps=%d" % (pn, _ABLOCK_PAGE))
        if d.get("code") != 0:
            if pn == 1:
                if d.get("code") == -101:
                    raise RuntimeError("未登录B站账号，无法同步拉黑名单")
                raise RuntimeError("拉黑名单读取失败(code=%s)" % d.get("code"))
            break                       # 后续页失败: 拿多少算多少, 不删数据
        data = d.get("data") or {}
        if total is None:
            try:
                total = int(data.get("total") or 0)
            except Exception:
                total = 0
        lst = data.get("list") or []
        if not lst:
            complete = (total == 0)     # 空页 + total 也是 0 → 确实一个拉黑都没有
            break
        for it in lst:
            n = it.get("uname") or it.get("name")
            if not n:
                continue
            m = it.get("mid")
            seen[n] = str(m) if m else None
            fetched += 1
        if total and fetched >= total:
            complete = True
            break
        time.sleep(_ABLOCK_GAP)         # 别把风控招来
    rows = [(n, m) for n, m in seen.items()]
    if not rows and not complete:
        return 0                        # 一页都没成功 → 什么都不动
    with _db_lock:
        if complete:
            # 名单是权威的 → 清掉不在里面的老行。它们 mid 恒 NULL, 既算不出哈希,
            # 又会让内容过滤误伤已经解禁的作者。
            db().execute("DELETE FROM ablock")
        if rows:
            now = int(time.time())
            db().executemany("INSERT OR REPLACE INTO ablock(name, ts, mid) VALUES(?,?,?)",
                             [(n, now, m) for n, m in rows])
        db().commit()
    if complete:
        try:
            sset("ablock_v", "2")       # 标记"本地已是完整镜像", 启动修复不再重复拉
        except Exception:
            pass
    return len(rows)


def repair_blocklist_mids():
    """v0.3.1.32: 补齐/重建本地拉黑名单(只在需要时打一次网络)。

    两个历史问题叠在一起, 让「屏蔽黑名单内用户的弹幕」几乎失效:
      ① ablock.mid 是后加的列 —— 它之前同步进来的行 mid 全 NULL, dm_blocked_hashes()
         一条哈希都算不出来(实测用户库里 50 行全是 NULL mid, dmhash = 0 条);
      ② 拉黑接口是分页的(50 条/页), 老代码只取第一页 ——
         实测账号 778 个拉黑只同步了 50 个。
    用 ablock_v 版本号兜底: 不等于 '2' 就重拉一次全量(分页), 成功后才打标记,
    之后不再联网。失败(如离线)不打标记, 下次启动再试。
    """
    try:
        if sget("m_blocklist_sync") != "1":
            return 0
        if sget("ablock_v") == "2":
            return 0
        old = db().execute("SELECT COUNT(*) FROM ablock").fetchone()
        old = old[0] if old else 0
        n = fetch_account_blocklist()
        print("[v0.3.1.32] 重建本地拉黑名单: 写入 %d 行(原有 %d 行)" % (n, old))
        return n
    except Exception:
        return 0


def maybe_sync_blocklist():
    """登录后自动同步拉黑名单(仅当设置开启时)。"""
    if sget("m_blocklist_sync") != "1":
        return
    try:
        fetch_account_blocklist()
    except Exception:
        pass


def _norm_space(it, author_mid=0):
    """B站空间投稿 vlist → 统一条目格式(扁平结构, 与推荐接口不同的字段名)。标记 following。"""
    dur = it.get("length") or "0:0"
    try:
        parts = [int(x) for x in str(dur).split(":")]
        dsec = parts[-1] + (parts[-2] * 60 if len(parts) >= 2 else 0)
    except Exception:
        dsec = 0
    pic = it.get("pic") or ""
    if pic.startswith("//"):
        pic = "https:" + pic
    else:
        pic = pic.replace("http://", "https://")
    pubdate = it.get("pubdate") or it.get("created") or 0
    try:
        pubdate = int(pubdate)
        if pubdate > 10 ** 12:      # 毫秒时间戳 → 秒
            pubdate //= 1000
        if pubdate < 0:
            pubdate = 0
    except Exception:
        pubdate = 0
    return {
        "key": "https://www.bilibili.com/video/%s" % it.get("bvid", ""),
        "platform": "B站",
        "title": it.get("title", ""),
        "author": it.get("author", ""),
        "author_mid": author_mid or it.get("mid") or 0,
        "desc": (it.get("description") or "")[:500],
        "cover": pic,
        "views": it.get("play", 0),
        "likes": it.get("like", 0),
        "danmaku": it.get("video_review", 0),
        "reply": it.get("comment", 0),
        "duration": dsec,
        "pubdate": pubdate,
        "url": "https://www.bilibili.com/video/%s" % it.get("bvid", ""),
        "tname": it.get("typename", ""),
        "area": "",
        "tag": "",
        "following": True,   # 关注的人强调标记(前端视觉强调 + 后续豁免机器过滤)
        "src": "关注",
    }


# 关注的人身份缓存: UID 是权威标识; 昵称仅用于旧缓存条目的兼容回退。
_following_names_cache = {"ts": 0, "names": set(), "mids": set()}
_follow_lock = threading.Lock()


def _set_following_identities(names, mids):
    names = {n for n in (names or []) if n}
    mids = {str(m) for m in (mids or []) if str(m).isdigit() and int(m) > 0}
    with _follow_lock:
        _following_names_cache.update(ts=time.time(), names=names, mids=mids)
    try:
        sset("bili_follow_names", json.dumps(sorted(names), ensure_ascii=False))
        sset("bili_follow_mids", json.dumps(sorted(mids)))
    except Exception:
        pass


def following_identities():
    """当前关注UP的 UID 集合与旧昵称集合；UID 匹配优先。"""
    with _follow_lock:
        if time.time() - _following_names_cache["ts"] < 900 and (
                _following_names_cache["mids"] or _following_names_cache["names"]):
            return _following_names_cache["mids"], _following_names_cache["names"]
    try:
        names = set(json.loads(sget("bili_follow_names") or "[]"))
    except Exception:
        names = set()
    try:
        mids = {str(m) for m in json.loads(sget("bili_follow_mids") or "[]") if str(m).isdigit() and int(m) > 0}
    except Exception:
        mids = set()
    if mids or names:
        with _follow_lock:
            _following_names_cache.update(ts=time.time(), names=names, mids=mids)
    return mids, names


def following_names():
    """兼容旧调用方: 返回缓存的关注昵称集合。"""
    return following_identities()[1]


def _annotate_following(items):
    """按UP UID标记关注视频；旧条目缺UID时才退回昵称匹配。"""
    if sget("bili_follow_emph") != "1":
        return
    mids, names = following_identities()
    if not (mids or names) and account_cookie():
        try:
            threading.Thread(target=fetch_following_videos, kwargs={"total_cap": 1}, daemon=True).start()
        except Exception:
            pass
        mids, names = following_identities()
    if not (mids or names):
        return
    months = int(sget("bili_follow_months") or 1)
    cut = 0 if months <= 0 else time.time() - months * 30.44 * 86400
    for it in items:
        if it.get("platform") != "B站":
            continue
        mid = str(it.get("author_mid") or "")
        is_following = mid in mids if mid else bool(it.get("author") and it.get("author") in names)
        if not is_following or not it.get("pubdate") or (cut and it["pubdate"] < cut):
            it.pop("following", None)
            continue
        it["following"] = True


def _space_videos(mid, ps=30):
    """B站 up 空间投稿(按发布时间), wbi 签名; 失败返回空列表。"""
    try:
        qs = wbi_enc({"mid": mid, "pn": 1, "ps": ps, "order": "pubdate"})
        d = bili_get("https://api.bilibili.com/x/space/wbi/arc/search?" + qs)
        if d.get("code") != 0:
            return []
        return ((d.get("data") or {}).get("list") or {}).get("vlist") or []
    except Exception:
        return []


def fetch_following_videos(max_up=20, per_up=30, total_cap=60):
    """抓取「关注的人」投稿入池(强调功能)。返回 (items, err)，失败不抛异常。
    语义: 只保留「强调月数内发布」的关注视频并标 following=True(强调); 超期/无发布时间的一律丢弃,
    不进池。bili_follow_months(0=不限). 未登录/风控 → 空。"""
    if not account_cookie():
        return [], "未登录B站账号"
    try:
        nav = bili_get("https://api.bilibili.com/x/web-interface/nav")
        if nav.get("code") != 0 or not (nav.get("data") or {}).get("isLogin"):
            return [], "未登录或Cookie失效"
        mid = (nav.get("data") or {}).get("mid", 0)
        months = int(sget("bili_follow_months") or 1)
        cut = 0 if months <= 0 else time.time() - months * 30.44 * 86400
    except Exception as e:
        return [], str(e)[:80]
    # 关注列表身份全集(分页): UID 用于标注所有正常池候选；仅前 max_up 位再抓投稿入池。
    ups, names, mids = [], set(), set()
    try:
        for pn in range(1, 21):
            d = bili_get("https://api.bilibili.com/x/relation/followings?vmid=%d&pn=%d&ps=50" % (mid, pn))
            if d.get("code") != 0:
                return [], "关注列表获取失败(code=%s)" % d.get("code")
            rows = (d.get("data") or {}).get("list") or []
            for it in rows:
                if it.get("mid"):
                    ups.append((it["mid"], it.get("uname", "")))
                    mids.add(str(it["mid"]))
                    if it.get("uname"):
                        names.add(it["uname"])
            if len(rows) < 50:
                break
    except Exception as e:
        return [], str(e)[:80]
    _set_following_identities(names, mids)   # 缓存UID供正常视频稳定标注，昵称仅兼容旧条目
    if not ups:
        return [], ""
    ups = ups[:max_up]
    items, seen = [], set()
    try:
        for (umid, _uname), d in zip(ups, _FX.map(
                lambda u: _space_videos(u[0], per_up), ups)):
            for raw in (d or []):
                it = _norm_space(raw, umid)
                if it["key"] in seen:
                    continue
                seen.add(it["key"])
                if not it["pubdate"]:
                    continue   # 无发布时间 → 无法判断, 直接丢弃
                if cut and it["pubdate"] < cut:
                    continue   # 超出强调月数(如1个月) → 直接丢弃, 不进池
                it["following"] = True
                items.append(it)   # 符合月数 → 入池并标 following=True(强调)
    except Exception as e:
        return items, str(e)[:80]
    items.sort(key=lambda x: -x["pubdate"])
    return items[:total_cap], ""


# ---------------------------------------------------------------- B站直播(v0.3.1.51)
# 用户要求: 内容源里加「B站直播」(默认占比 25, 与小黑盒/贴吧同档), 并有独立直播页;
# 信息流里要能刷到**关注的人正在直播**。
# 接口探测结论(本机实测):
#   ① Referer/Origin 必须是 https://live.bilibili.com/ —— 缺了直接吃风控 code=-352;
#   ② /xlive/web-interface/v1/index/getList?platform=web → data.room_list(模块数组)+recommend_room_list;
#   ③ /room/v1/room/get_user_recommend → data 直接是房间数组(上面的接口抽风时兜底);
#   ④ /room/v1/Room/get_status_info_by_uids (POST {"uids":[...]}) → {uid: room}, 带 live_status 判断在播;
#   ⑤ second/getList 与 getListByArea 需要 wbi 签名, 返回 -352 —— 不用它们。
LIVE_PLATFORM = "B站直播"
LIVE_API = "https://api.live.bilibili.com"
LIVE_HEADERS = dict(ACCOUNT_HEADERS, Referer="https://live.bilibili.com/",
                    Origin="https://live.bilibili.com",
                    Accept="application/json, text/plain, */*")
_live_cache = {"ts": 0.0, "data": None}
_live_lock = threading.Lock()
_live_last_fetch = 0
_live_fetch_total = 0


def live_api(path, params=None, body=None, timeout=10):
    """直播接口 GET/POST(带登录 Cookie)。返回 data 字段; code!=0 时抛异常。"""
    url = LIVE_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    hdr = dict(LIVE_HEADERS)
    if account_cookie():
        hdr["Cookie"] = account_cookie()   # 登录后个人化推荐更准; 未登录也能出热门
    if payload is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read().decode("utf-8", "ignore"))
    if d.get("code") != 0:
        raise RuntimeError("code=%s %s" % (d.get("code"), (d.get("message") or "")[:60]))
    return d.get("data")


def _live_roomid(room):
    """房间号: 列表接口给 roomid, 状态接口给 room_id, 个别模块只给 link("/24011597?hotRank=0")。"""
    try:
        rid = room.get("roomid") or room.get("room_id")
        if not rid:
            link = str(room.get("link") or "")
            rid = link.strip("/").split("?")[0].split("/")[0]
        return int(rid or 0)
    except Exception:
        return 0


def _live_item(room, following=False, src="直播"):
    """直播房间 → 信息流条目(字段与视频条目对齐, 下游筛选/排序/卡片无需特判)。

    语义差别:
      views   = 人气值(排序/展示用, 不是播放量);
      pubdate = 抓取时刻 —— 直播是「此刻正在发生」, 用开播时间当发布日期会被
                m_max_age_months / 资讯24h 那套窗口误杀, 关注的人那套月数窗也会误判;
      live_time = 真实开播时间戳(卡片显示「开播 N 分钟前」用)。
    """
    roomid = _live_roomid(room)
    uid = int(room.get("uid") or 0)
    cover = (room.get("user_cover") or room.get("cover") or room.get("keyframe")
             or room.get("system_cover") or "")
    try:
        online = int(room.get("online") or 0)
    except Exception:
        online = 0
    tags = (room.get("tags") or room.get("tag_name") or "").strip()
    now = int(time.time())
    try:
        live_time = int(room.get("live_time") or 0)
    except Exception:
        live_time = 0
    if live_time <= 0 or live_time > now:
        live_time = now
    url = "https://live.bilibili.com/%d" % roomid
    return {
        "key": url, "platform": LIVE_PLATFORM, "gid": "live", "src": src,
        "title": (room.get("title") or "").strip() or "直播中",
        "author": (room.get("uname") or "").strip() or "主播",
        "author_mid": uid,
        "desc": (tags or (room.get("area_v2_parent_name") or ""))[:200],
        "cover": cover, "views": online, "likes": 0, "danmaku": 0, "reply": 0,
        "duration": 0, "pubdate": now, "url": url,
        "tname": room.get("area_v2_name") or room.get("area_name") or "",
        "area": room.get("area_v2_parent_name") or room.get("areaName") or "",
        "tag": tags[:80],
        "roomid": roomid, "online": online, "live_time": live_time,
        "live_status": 1, "is_live": True, "following": bool(following),
    }


def live_status_by_uids(mids):
    """批量查「这些 UID 里谁正在直播」→ {uid: room}; 失败抛异常(由调用方兜底)。"""
    ids = []
    for m in mids or ():
        try:
            v = int(m)
        except Exception:
            continue
        if v > 0:
            ids.append(v)
    if not ids:
        return {}
    data = live_api("/room/v1/Room/get_status_info_by_uids", body={"uids": ids[:100]})
    out = {}
    for k, room in (data or {}).items():
        if not isinstance(room, dict):
            continue
        try:
            uid = int(room.get("uid") or k)
        except Exception:
            continue
        if int(room.get("live_status") or 0) != 1:
            continue   # 有关注但没开播: 不进任何列表
        out[uid] = room
    return out


def fetch_live_hot(limit=24):
    """首页推荐直播(热门)。index/getList 的推荐模块为主, 抽风时退回 get_user_recommend。"""
    rooms = []
    try:
        data = live_api("/xlive/web-interface/v1/index/getList", params={"platform": "web"})
        for mod in (data or {}).get("room_list") or []:
            for r in (mod or {}).get("list") or []:
                if isinstance(r, dict):
                    rooms.append(r)
        for r in (data or {}).get("recommend_room_list") or []:
            if isinstance(r, dict):
                rooms.append(r)
    except Exception:
        rooms = []
    if not rooms:
        try:
            data = live_api("/room/v1/room/get_user_recommend",
                            params={"page": 1, "page_size": max(12, int(limit)), "source": 54})
            rooms = [r for r in (data or []) if isinstance(r, dict)]
        except Exception:
            rooms = []
    out, seen = [], set()
    for r in rooms:
        rid = _live_roomid(r)
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(_live_item(r, src="直播"))
        if len(out) >= limit:
            break
    return out


def live_filters():
    """直播筛选档位(v0.3.1.57): (观众量门槛, 只显示关注的人的直播)。
    门槛只对"热门直播"生效 —— 关注的主播本来就没几个, 再被人数门槛筛掉就成了"我关注的开播了却看不到"。"""
    try:
        mn = int(sget("live_min_viewers") or 0)
    except Exception:
        mn = 0
    return max(0, mn), sget("live_only_followed") == "1"


def _live_pass(it, mn, follow_only):
    followed = bool(it.get("following"))
    if follow_only and not followed:
        return False
    if mn and not followed and int(it.get("online") or 0) < mn:
        return False
    return True


def fetch_live_pool(quick=False):
    """直播源入信息流: 关注的人正在直播(src=关注, following=True) + 热门直播(src=直播)。
    返回 (items, errors)。关注部分依赖本地关注 UID 缓存(与「关注的人投稿」同源), 未登录只出热门。"""
    global _live_last_fetch, _live_fetch_total
    items, errs, seen = [], [], set()
    mn, follow_only = live_filters()

    def push(it):
        if not it.get("roomid") or it["key"] in seen:
            return False
        if not _live_pass(it, mn, follow_only):
            return False
        seen.add(it["key"])
        items.append(it)
        return True

    mids, _names = following_identities()
    if mids:
        try:
            for _uid, room in live_status_by_uids(mids).items():
                push(_live_item(room, following=True, src="关注"))
        except Exception as e:
            errs.append("关注直播: " + str(e)[:80])
    try:
        for it in fetch_live_hot(limit=8 if quick else 24):
            push(it)
    except Exception as e:
        errs.append("热门直播: " + str(e)[:80])
    if not items:
        if errs:
            sset("live_error", errs[0])
            return [], errs
        # 全被筛选档位筛掉不算故障(别在左栏报"抓取失败")
        _live_last_fetch = int(time.time())
        sset("live_last_fetch", _live_last_fetch)
        sset("live_error", "")
        return [], []
    _live_fetch_total += len(items)
    _live_last_fetch = int(time.time())
    sset("live_last_fetch", _live_last_fetch)
    sset("live_error", "")
    return items, []


def live_snapshot(refresh=False, ttl=60):
    """直播页数据: {followed, hot, ts, logged_in, need_login, error}。60 秒缓存, refresh 强刷。"""
    now = time.time()
    with _live_lock:
        c = _live_cache
        if not refresh and c["data"] and now - c["ts"] < ttl:
            return dict(c["data"], cached=True)
    followed, hot, errs = [], [], []
    mids, _names = following_identities()
    if mids:
        try:
            for _uid, room in live_status_by_uids(mids).items():
                it = _live_item(room, following=True, src="关注")
                if it.get("roomid"):
                    followed.append(it)
        except Exception as e:
            errs.append("关注直播: " + str(e)[:80])
    try:
        hot = [it for it in fetch_live_hot(limit=24) if it.get("roomid")]
    except Exception as e:
        errs.append("热门直播: " + str(e)[:80])
    fk = {it["key"] for it in followed}
    hot = [it for it in hot if it["key"] not in fk]
    # 与信息流同一套筛选档位, 免得"直播页有一堆、信息流里一条没有"
    mn, follow_only = live_filters()
    followed = [it for it in followed if _live_pass(it, mn, follow_only)]
    hot = [it for it in hot if _live_pass(it, mn, follow_only)]
    data = {"ok": bool(followed or hot), "followed": followed, "hot": hot,
            "count": len(followed) + len(hot),
            "followed_n": len(followed),
            "min_viewers": mn, "only_followed": follow_only,
            "logged_in": bool(account_cookie()),
            "need_login": not bool(mids),
            "error": "; ".join(errs)[:200],
            "ts": int(now), "last_fetch": _live_last_fetch}
    with _live_lock:
        _live_cache.update(ts=now, data=data)
    return dict(data, cached=False)


def parse_scores(content):
    """从 LLM 输出中提取评分数组, 容错处理。"""
    try:
        data = json.loads(content)
    except Exception:
        m = re_search_json(content)
        data = json.loads(m.group(0)) if m else None
    if isinstance(data, dict):
        data = data.get("items", [])
    return data if isinstance(data, list) else []


def re_search_json(text):
    import re
    for m in re.finditer(r"\[.*?\]|\{.*?\}", text, re.S):
        try:
            return json.loads(m.group(0)) or None
        except Exception:
            continue
    return None


def score_ttl():
    """评分缓存时长(秒): 省钱法 48h。"""
    return int(sget("ai_cache_hours") or 24) * 3600


def clear_score_cache():
    """清评分缓存(保留内容 json): 画像类规则(厌恶/偏好备注)变更后强制重评, 避免旧语义分数过时。"""
    with _db_lock:
        db().execute("UPDATE items SET score_json='', score_at=0")
        db().commit()


def score_batch():
    """每批评分条目数(省钱法 30/批, 省一半请求)。"""
    n = int(sget("ai_batch") or 15)
    return 30 if n == 30 else 15


SCORER_SKILL = """
【内容筛选技能 v2】你是一名专业内容筛选员，为「洞鉴虚明」工作。你的唯一任务：对每条内容给出质量与匹配度判断，供系统决定是否推送给用户。

【内容形态】条目可能来自三个平台，评分口径略有差异：
- B站视频(platform=B站): 视频内容, 判据照常(标题/作者/简介)。
- 贴吧帖子(platform=贴吧): 文字论坛帖, title=帖子标题, desc=首楼摘要, author=楼主。
  引战分判据: 对线/吵架/挂人/带节奏; 低质分判据: 广告帖/纯水贴/无信息量搬运/引流帖;
  兴趣分判据: 主题是否与用户画像相关(教程/知识/军事解析/数码等), 水贴闲聊给低分。
- 小黑盒帖子(platform=小黑盒): 游戏社区图文帖, title=帖子标题, desc=正文摘要。
  引战分判据: 带节奏/互喷/挂人开团/主机与装备攀比/玩家阵营对立拱火;
  低质分判据: 纯图无文字水贴/无信息量搬运/代购广告引流帖;
  兴趣分判据: 与用户游戏画像的匹配度(平台/品类/玩法), 闲聊灌水给低分。

【评分维度】每条内容必须给出全部 8 项：
1. flame 引战分 0-10：内容挑起对立/拉踩/煽动情绪/恶意碰瓷的程度。判据：地域性别等对立话题、引战标题("XX都看不下去了")、挂人/开团/扣帽子、评论区天天吵架的选题、明知争议还要拱火。0=完全平和，10=最恶劣的挑事内容。
2. spam 低质分 0-10：营销广告/无信息量搬运/粗制滥造/货不对板程度。判据：大量水军营销号、AI生成图片配空洞文案骗点击、标题说A内容讲B、纯水时长拼凑、赤裸推销。**标题党算低质**: 震惊党开头/悬念党/数字党/货不对板骗点击, spam 至少 6 分。0=制作认真，10=纯垃圾。
3. interest 兴趣分 0-10：与"用户信息"中画像的匹配度。判据：主题相关、作者熟悉、玩法/内容形式符合偏好；用户明显不感兴趣的主题给低分。无法判断时给 5。
4. density 信息密度 0-10：能否用一句话概括全部有效信息。判据：废话/注水/车轱辘话/空镜凑时长越多分越低；教程干货、数据密集内容高分。0=全是废话，10=句句有信息。
5. emo 情绪净值 -10~10：正数=建设性/正向价值(即使讲坏事，给了解决方案或深度剖析也给正分)，负数=纯负能量/煽动焦虑/贩卖恐慌。0=中性陈述。判据：讲问题但给方法→正；只输出情绪、制造对立焦虑、贩卖恐慌→负。
6. facts 事实锚点 0/1：资讯/评论/事件类内容是否包含具体数据、引用来源、时间地点等可验证锚点。有任一锚点=1；纯情绪输出/空泛观点=0。非资讯类(教程/娱乐/生活)默认1，不算错。
7. topic 主题标签：2-6 字中文，如"独立游戏""数码评测""二次元"。用具体内容概括，不要用"综合""其他"。
8. reason 推荐理由：一句 20-40 字，像朋友安利那样自然口语。要点：说出具体亮点(玩法机制/内容质量/独特卖点/强情绪点)，可结合用户画像针对性推荐，**禁止**负面词汇(别/不/差/烂/争议)，**禁止**空泛套话(推荐/值得一看/不错)。示例："评测只讲干货没废话，上手门槛说得明明白白""这作者考据特别细，你爱看三国应该会喜欢"。

【判定原则】
- 引战与低质是"一票否决"级: flame≥8 或 spam≥8 必须标出; 6-7 属于"边缘", 若与用户画像无关也标出。
- **厌恶是最高优先级**: "用户信息"中的厌恶清单一经语义命中(不要字面匹配, 要理解内容是否属于该类别),
  强制 interest=0 且 topic 加"回避-"前缀(如"回避-二次元游戏")。语义相近但非同类不要误伤
  (如厌恶"二次元游戏"时, 正常的单机/3A大作/电竞内容照常评分; 厌恶"流量明星八卦"时, 正常娱乐向但不炒作的也不误伤)。
- 不冤枉好内容: 有态度但不引战、有观点但不拉踩的内容, flame 给 3-4。
- 系列作品/同一UP内容: 若用户在追该系列, interest 加成。
- 信息不足(纯图片/无简介)给中性分: flame 3, spam 3, interest 5, topic "综合"。
- 每条都必须有结果, 与输入一一对应, 顺序一致。

【输出】只输出 JSON, 不要任何其他文字/注释/代码块围栏:
{"items":[{"key":"...","flame":0,"spam":0,"density":8,"emo":3,"facts":1,"topic":"主题","interest":0,"reason":"..."}]}
""".strip()


_score_lock = threading.Lock()   # 评分全局串行: 并发请求共享同一批评分结果, 不重复烧钱


def _load_cache(items, ttl):
    """从 items 表读取评分缓存, 返回 (cached, pending)。"""
    if not items:
        return {}, []   # 空列表: 防生成 "key IN ()" 非法SQL
    with _db_lock:
        rows = {r[0]: (r[1], r[2]) for r in db().execute(
            "SELECT key,score_json,score_at FROM items WHERE key IN (%s)"
            % ",".join("?" * len(items)), tuple(i["key"] for i in items)).fetchall()}
    now = time.time()
    cached, pending = {}, []
    for it in items:
        scr = rows.get(it["key"])
        if scr and scr[0] and scr[1] and now - scr[1] < ttl:
            cached[it["key"]] = json.loads(scr[0])
        else:
            pending.append(it)
    return cached, pending


def _write_scores(cached):
    global _score_epoch
    with _db_lock:
        for key, sc in cached.items():
            # upsert: 条目不在 items 表时也写入评分缓存(防并发去重失效); json 列仅在新建时用空占位,
            # 真实条目由构建流程 INSERT 覆盖, 已有条目不触碰 json
            db().execute(
                "INSERT INTO items(key,json,fetched_at,score_at,score_json) VALUES(?,?,0,?,?) "
                "ON CONFLICT(key) DO UPDATE SET score_json=excluded.score_json, score_at=excluded.score_at",
                (key, "{}", int(time.time()), json.dumps(sc, ensure_ascii=False)))
        db().commit()
    _score_epoch += 1   # 新评分落库 → 套餐规划缓存失效(全扫模式下新筛内容渐进可见)


def score_items(items, only_cache=False, chunk_size=None):
    """为条目打分(带缓存)。返回 {key: score}。失败不影响主流程。
    only_cache=True: 只读缓存不调LLM(供bake快速过滤用), 未缓存条目后续由serve门禁补评。
    chunk_size: 每批条目数(缺省=score_batch); 首屏用小批可更快出分。
    并发说明: pending 评分段用 _score_lock 串行, 排队线程重查缓存直接复用结果,
    —— 多个并发 feed 请求(用户页面多开/滚动)不会重复评分同一批条目。"""
    ttl = score_ttl()
    cached, pending = _load_cache(items, ttl)
    if pending and ai_on() and not only_cache:
        with _score_lock:
            # 排队线程可能在等待期间已评完 → 重查缓存, 避免重复烧钱
            cached2, pending = _load_cache(items, ttl)
            cached = dict(cached2) or cached
            if not pending:
                _write_scores(cached)
                return cached
            ctx = user_context() or "(无)"
            batch = chunk_size or score_batch()
            chunks = [pending[i:i + batch] for i in range(0, len(pending), batch)]
            _score_progress.update(total=len(pending), done=len(cached), ts=int(time.time()))

            def _score_chunk(chunk):
                last_err = None
                for attempt in range(3):   # 限流/网络抖动重试
                    try:
                        content, _ = call_llm([
                            {"role": "system", "content": SCORER_SKILL + "\n\n【用户信息】\n" + ctx},
                            {"role": "user", "content": json.dumps(
                                {"items": [{"key": it["key"], "platform": it.get("platform", "B站"),
                                            "title": it["title"][:60],
                                            "author": it["author"][:20],
                                            "desc": it["desc"][:60]} for it in chunk]},
                                ensure_ascii=False)},
                        ], max_tokens=8192)
                        parsed = parse_scores(content)
                        if parsed:
                            return parsed
                        last_err = "解析失败(" + str(len(content)) + "字)"
                    except Exception as e:
                        last_err = str(e)[:100]
                    time.sleep(1.5 * (attempt + 1))
                with _db_lock:
                    _score_progress["last_err"] = last_err
                return []

            for sc_list in _SCORE_X.map(_score_chunk, chunks):   # 3并发评分(防限流)
                for sc in sc_list:
                    if isinstance(sc, dict) and sc.get("key"):
                        cached[sc["key"]] = sc
                with _db_lock:   # 进度(线程安全)
                    _score_progress["done"] = min(_score_progress["total"],
                                                  _score_progress["done"] + len(sc_list))
            _write_scores(cached)   # 锁内写库, 后续线程重查即可命中
            return cached
    _write_scores(cached)   # 仅缓存命中或 only_cache
    return cached


# --- 后台渐进评分: 让交互路径(只读缓存)秒回, 评分在后台逐步推进, 避免阻塞 HTTP 请求 ---
# 根因: serve/search 门禁原先用 score_items(chunk) 同步调 LLM, 首次筛查/刷新命中未评分条目
# 会阻塞 30-60s(表现为"获取太长时间/刷新卡住"); 改为只读缓存后, 由本守护线程预评分写库,
# 前端刷新时渐进命中缓存, 首屏秒回, 未评分条目按门禁暂时不展示(不误判"已到底").
_score_bg_q = collections.deque()      # 待评分条目队列(iterable of item dicts)
_score_bg_seen = set()                 # 已入队 key(防重复入队; 失败保留避免无限重评)
_score_bg_lock = threading.Lock()
_score_bg_started = False
_score_bg_gen = 0                      # 世代计数: universe 重建时清掉旧队列


def _needs_score(it, rows, now, ttl):
    """条目是否需要评分: 非 B站(无评分语义)/已有未过期缓存/已被本次入队 → False。"""
    if (it.get("platform") or "B站") != "B站":
        return False
    key = it["key"]
    if key in _score_bg_seen:
        return False
    sat = rows.get(key)
    if sat and now - sat < ttl:
        return False
    return True


def _request_bg_score(items):
    """把需要评分的候选入队, 由后台线程渐进评分写库。只处理 B站(其他平台不过 AI 门禁)。"""
    if not (ai_on() and sget("ai_full_scan") == "1"):
        return
    keys = [it["key"] for it in items if (it.get("platform") or "B站") == "B站"]
    if not keys:
        return
    ttl = score_ttl()
    now = time.time()
    try:
        with _db_lock:
            rows = {r[0]: r[1] for r in db().execute(
                "SELECT key,score_at FROM items WHERE key IN (%s)"
                % ",".join("?" * len(keys)), tuple(keys)).fetchall()}
    except Exception:
        rows = {}
    with _score_bg_lock:
        for it in items:
            if _needs_score(it, rows, now, ttl):
                _score_bg_seen.add(it["key"])
                _score_bg_q.append(it)


_bg_cold = True   # 评分冷启动: 首批用小批(5条)更快出分


def _drain_bg_queue():
    """消费队列给候选评分; 冷启动用小批(5条)更快出首批分, 常规一次取3批并行评(加快补充)。"""
    global _bg_cold
    batch = []
    with _score_bg_lock:
        want = 5 if _bg_cold else score_batch() * 3   # 常规一次45条 → score_items 内部3并发并行
        while _score_bg_q and len(batch) < want:
            batch.append(_score_bg_q.popleft())
    if not batch:
        return
    chunk = 5 if _bg_cold else None   # 冷启动首屏: 5条/请求, 3并发更快出首批分数
    _bg_cold = False
    try:
        scores = score_items(batch, only_cache=False, chunk_size=chunk)
    except Exception:
        scores = {}
    # 成功评分的 key 从 seen 移除(已写库, 未来可因过期再评); 失败保留避免无限重评
    with _score_bg_lock:
        for it in batch:
            if it["key"] in scores:
                _score_bg_seen.discard(it["key"])


def _score_bg_loop():
    """守护线程: 后台渐进评分。AI 关闭/非全扫时空转; 队列空时休眠。"""
    while True:
        if ai_on() and sget("ai_full_scan") == "1":
            _drain_bg_queue()
        else:
            time.sleep(1.0)


def _ensure_score_bg():
    global _score_bg_started
    with _score_bg_lock:
        if _score_bg_started:
            return
        _score_bg_started = True
    threading.Thread(target=_score_bg_loop, daemon=True).start()


_prewarm_lock = threading.Lock()
_prewarm_started = False
_prewarm_gen = [0]        # 预热线程自己发起的构建世代, 便于诊断


def _prewarm_loop():
    """常驻预热: 让内容池始终有货 —— 打开界面、滚到底、点刷新都不必干等构建。
    只在"池子空 / 条目太少 / 太久没有新内容"时才抓, 且同一时刻只有一个构建
    (build_universe 自带 building 门, 重复进入直接返回 None), 因此不会打并发抓取。"""
    time.sleep(10)        # 让启动路径与首屏构建先跑, 不与它抢网络
    while not _SHUTDOWN_REQUESTED.is_set():
        try:
            with _uni_lock:
                n = len(_universe["items"])
                building = _universe["building"]
                built_at = _universe["built_at"]
            if not building and (n == 0 or n < PREWARM_MIN
                                 or time.time() - built_at > PREWARM_IDLE):
                _prewarm_gen[0] += 1
                # v0.3.1.55: 走 kick_build 的单飞/合并, 别让预热和用户刷新各起一个构建
                kick_build(quick=False)
        except Exception:
            pass
        time.sleep(PREWARM_TICK)


def _ensure_prewarm():
    global _prewarm_started
    with _prewarm_lock:
        if _prewarm_started:
            return
        _prewarm_started = True
    threading.Thread(target=_prewarm_loop, daemon=True).start()


# ---------------------------------------------------------------- 封面缩略播放(B站预览)

_tag_done = set()
_tag_lock = threading.Lock()


def ensure_tags(items, limit=60):
    """后台补视频标签(无tag条目, 3并发): 供厌恶备注匹配使用(B站官方标签)。
    写回 items 库并同步到当前世代, 下一批条目不重复查。"""
    todo = []
    for it in (items or [])[:limit]:
        if (it.get("tag") or "").strip():
            continue
        bvid = (it.get("url") or "").rsplit("/", 1)[-1]
        if not bvid.startswith("BV"):
            continue
        with _tag_lock:
            if bvid in _tag_done:
                continue
            _tag_done.add(bvid)
        todo.append(it)
    if not todo:
        return

    def _one(it):
        bvid = (it.get("url") or "").rsplit("/", 1)[-1]
        try:
            d = bili_get("https://api.bilibili.com/x/tag/archive/tags?bvid=" +
                         urllib.parse.quote(bvid))
            names = [x.get("tag_name", "") for x in (d.get("data") or []) if x.get("tag_name")]
        except Exception:
            names = []
        if not names:
            return
        tag = ",".join(names)[:200]
        try:
            with _db_lock:
                row = db().execute("SELECT json FROM items WHERE key=?", (it.get("key"),)).fetchone()
                if row:
                    rec = json.loads(row[0])
                    rec["tag"] = tag
                    db().execute("UPDATE items SET json=? WHERE key=?",
                                 (json.dumps(rec, ensure_ascii=False), it.get("key")))
                else:
                    db().execute("INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,0,0)",
                                 (it.get("key"), json.dumps(dict(it, tag=tag),
                                                           ensure_ascii=False)))
                db().commit()
        except Exception:
            pass
        with _uni_lock:   # 同步到当前世代, 即时生效
            for u in _universe["items"]:
                if u.get("key") == it.get("key") and not (u.get("tag") or "").strip():
                    u["tag"] = tag

    try:
        _TAG_X.map(_one, todo)
    except Exception:
        pass


_preview_cache = {}   # bvid -> {"cid": int, "url": str, "ts": float}
_PREVIEW_TTL = 3600


def preview_src(bvid):
    """返回 (proxy_path, None) 或 (None, err)。缓存 1 小时。"""
    if not re.match(r"^BV[0-9A-Za-z]{8,12}$", bvid):
        return None, "bvid格式不对"
    c = _preview_cache.get(bvid)
    now = time.time()
    if c and now - c["ts"] < _PREVIEW_TTL and c.get("url"):
        return "/api/video-proxy?bvid=%s&cid=%s" % (urllib.parse.quote(bvid), c["cid"]), None
    try:
        d = bili_get("https://api.bilibili.com/x/player/pagelist?bvid=" + urllib.parse.quote(bvid))
        data = d.get("data") or []
        if not data:
            return None, "无分P信息"
        cid = data[0].get("cid") or data[0].get("id")
        if not cid:
            return None, "无cid"
        qs = wbi_enc({"bvid": bvid, "cid": cid, "qn": 6, "fnval": 0,
                      "fourk": 0, "platform": "html5"})
        d2 = bili_get("https://api.bilibili.com/x/player/wbi/playurl?" + qs)
        durls = ((d2.get("data") or {}).get("durl")) or []
        if not durls:
            return None, "无mp4预览流"
        # 预览用最低清 qn=6(240P) + 第一段(视频开头), 起播快且流畅
        _preview_cache[bvid] = {"cid": cid, "url": durls[0].get("url", ""), "ts": now}
        return "/api/video-proxy?bvid=%s&cid=%s" % (urllib.parse.quote(bvid), cid), None
    except Exception as e:
        return None, str(e)[:80]


# ---------------------------------------------------------------- B站扫码登录

_LOGIN_QR = {"key": "", "ts": 0}
_avatar_cache = {"ts": 0, "data": None, "ctype": "image/jpeg"}
QR_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")


def login_qr_generate():
    req = urllib.request.Request(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        headers={"User-Agent": HEADERS["User-Agent"],
                 "Referer": "https://passport.bilibili.com/login"})
    d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
    if d.get("code") != 0 or not (d.get("data") or {}).get("qrcode_key"):
        raise RuntimeError("二维码生成失败(code=%s)" % d.get("code"))
    data = d["data"]
    _LOGIN_QR.update(key=data["qrcode_key"], ts=int(time.time()))
    return {"key": data["qrcode_key"], "url": data["url"]}


def login_qr_poll(key):
    req = urllib.request.Request(
        "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=%s" % key,
        headers={"User-Agent": HEADERS["User-Agent"],
                 "Referer": "https://passport.bilibili.com/login"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
        setcookies = resp.headers.get_all("Set-Cookie") or []
    d = json.loads(raw)
    data = d.get("data") or {}
    code = data.get("code")
    if code == 0:   # 扫码成功 → 从 url 参数 + Set-Cookie 两处收集登录凭证
        parts = {}
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(data.get("url", "")).query)
        for k in QR_KEYS:
            if qs.get(k):
                parts[k] = qs[k][0]
        for sc in setcookies:
            pair = sc.split(";", 1)[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k in QR_KEYS:
                    parts[k] = v
        cookie = "; ".join("%s=%s" % (k, v) for k, v in parts.items())
        if not cookie:
            return {"status": "error", "message": "登录信息解析失败，请重试"}
        sset("bili_cookie", cookie)
        try:
            profile = fetch_account_profile()
        except Exception as e:
            return {"status": "error", "message": "登录成功但画像失败: %s" % str(e)[:100]}
        maybe_sync_blocklist()
        return {"status": "ok", "nickname": profile.get("nickname", ""), "profile": profile}
    if code == 86038:
        return {"status": "expired", "message": "二维码已过期，请点击「刷新二维码」"}
    if code == 86090:
        return {"status": "confirmed", "message": "已扫码，请在手机上确认登录"}
    if code == 86101:
        return {"status": "waiting", "message": "等待手机扫码…"}
    return {"status": "waiting", "message": data.get("message", "") or "等待扫码…"}


GROUP_NAMES = {"全站榜": "rank", "热门": "hot", "动画": "anime", "番剧": "bangumi",
               "国创": "guochuang", "鬼畜": "guichu", "舞蹈": "dance", "生活": "life",
               "美食": "food", "动物圈": "animal", "生活Vlog": "vlog",
               "影视": "film", "电影": "movie", "电视剧": "series", "纪录片": "documentary",
               "音乐": "music", "游戏": "game", "电竞": "esports",
               "知识": "knowledge", "科技": "tech", "数码": "digital",
               "运动": "sports", "赛事": "events", "美妆": "beauty",
               "时尚": "fashion", "汽车": "auto", "娱乐": "ent",
               "漫画": "comic", "资讯": "news", "直播": "live", "军事": "military"}
_ID2NAME = {v: k for k, v in GROUP_NAMES.items()}
SUGGESTIONS = ["今日成本", "只看游戏和数码分区", "最近1个月", "刷随机内容", "关掉热门", "屏蔽引战"]


# ---------------------------------------------------------------- 多会话(AI聊天)

def conv_list():
    """全部会话 + 当前会话id。"""
    cur = sget("cur_conv") or "1"
    rows = db().execute(
        "SELECT c.id, c.title, c.ts, c.updated, "
        "(SELECT COUNT(*) FROM chat WHERE cid=c.id) "
        "FROM conv c ORDER BY c.id").fetchall()
    convs = [{"id": r[0], "title": r[1], "ts": r[2], "updated": r[3], "count": r[4]} for r in rows]
    if not any(c["id"] == int(cur) for c in convs):
        cur = str(convs[0]["id"]) if convs else "1"
    return convs, int(cur)


def conv_cur(cid=None):
    """返回有效会话id: 指定cid 或 当前会话; 不存在则回退默认/新建。"""
    if cid is not None:
        try:
            cid = int(cid)
        except Exception:
            cid = None
    if cid is None or db().execute("SELECT 1 FROM conv WHERE id=?", (cid,)).fetchone() is None:
        _, cur = conv_list()
        cid = cur
    if db().execute("SELECT 1 FROM conv WHERE id=?", (cid,)).fetchone() is None:   # 库空则建默认
        db().execute("INSERT INTO conv(id,title,ts,updated) VALUES(1,'默认对话',?,?)",
                     (int(time.time()), int(time.time())))
        db().commit()
        cid = 1
    return cid


def conv_set_cur(cid):
    cid = conv_cur(cid)
    sset("cur_conv", str(cid))
    return cid


def conv_create(title=None, set_cur=True):
    title = (title or "").strip() or "新对话"
    ts = int(time.time())
    with _db_lock:
        cur = db().execute("INSERT INTO conv(title,ts,updated) VALUES(?,?,?)",
                           (title[:30], ts, ts)).lastrowid
        db().commit()
    if set_cur:
        sset("cur_conv", str(cur))
    return cur


def conv_touch(cid, title_hint=None):
    """更新会话活跃时间; 若标题还是默认名, 用首条消息自动命名。"""
    with _db_lock:
        if title_hint:
            row = db().execute("SELECT title FROM conv WHERE id=?", (cid,)).fetchone()
            if row and row[0] in ("新对话", "默认对话") and len(title_hint) >= 2:
                db().execute("UPDATE conv SET title=?, updated=? WHERE id=?",
                             (title_hint[:24], int(time.time()), cid))
            else:
                db().execute("UPDATE conv SET updated=? WHERE id=?", (int(time.time()), cid))
        else:
            db().execute("UPDATE conv SET updated=? WHERE id=?", (int(time.time()), cid))
        db().commit()


def conv_rename(cid, title):
    title = (title or "").strip()[:30]
    if not title:
        return False
    with _db_lock:
        db().execute("UPDATE conv SET title=? WHERE id=?", (title, conv_cur(cid)))
        db().commit()
    return True


def conv_delete(cid):
    cid = conv_cur(cid)
    with _db_lock:
        db().execute("DELETE FROM chat WHERE cid=?", (cid,))
        db().execute("DELETE FROM conv WHERE id=?", (cid,))
        db().commit()
    if sget("cur_conv") == str(cid):
        convs, _ = conv_list()
        if convs:
            sset("cur_conv", str(convs[-1]["id"]))   # 回退到最近一个
        else:
            conv_create(set_cur=True)


def last_feed_item():
    """用户最近看到/点开的条目, 用于“刚才那条”指代。"""
    try:
        raw = sget("last_feed")
        return json.loads(raw) if raw else None
    except Exception:
        return None


def set_last_feed(item):
    if not item:
        return
    try:
        sset("last_feed", json.dumps({"key": item.get("key", ""), "title": item.get("title", ""),
                                      "author": item.get("author", ""),
                                      "desc": (item.get("desc") or "")[:300],
                                      "platform": item.get("platform", "")}, ensure_ascii=False))
    except Exception:
        pass


# --- 会话条目上下文("问AI"): cid → 用户正在聊的条目, 由卡片长按"问AI"写入 ---
# 进程内存即可: 服务重启后丢失 → 降级为无上下文聊天, 不报错。
_conv_item_ctx = {}


def chat_ctx_set(cid, key, title):
    key = (key or "").strip()
    if not key:
        _conv_item_ctx.pop(cid, None)
        return None
    _conv_item_ctx[cid] = {"key": key, "title": (title or "")[:40]}
    return _conv_item_ctx[cid]


def chat_ctx_clear(cid):
    _conv_item_ctx.pop(cid, None)


def chat_ctx_get(cid):
    return _conv_item_ctx.get(cid)


def chat_item_context(cid):
    """把会话正在聊的条目拼成AI可读上下文(评分取缓存 score_json)。无条目返回''。"""
    ctx = _conv_item_ctx.get(cid)
    if not ctx:
        return ""
    it = item_by_key(ctx.get("key", ""))
    if not isinstance(it, dict):
        return ""
    sc = {}
    with _db_lock:
        row = db().execute("SELECT score_json FROM items WHERE key=?", (it.get("key", ""),)).fetchone()
    if row and row[0]:
        try:
            sc = json.loads(row[0])
        except Exception:
            sc = {}
    parts = ["【用户当前正在聊的条目】",
             "平台: %s ｜ 标题: %s ｜ 作者: %s" % (it.get("platform", ""), it.get("title", ""), it.get("author", ""))]
    if it.get("desc"):
        parts.append("简介: " + (it.get("desc") or "")[:400])
    if it.get("tag"):
        parts.append("标签: " + (it.get("tag") or "")[:100])
    if it.get("src"):
        parts.append("来源: " + it.get("src", ""))
    if sc:
        parts.append("AI评分: 兴趣%s/10, 引战%s, 低质%s, 主题「%s」, 推荐理由: %s" % (
            sc.get("interest", "?"), sc.get("flame", "?"), sc.get("spam", "?"),
            sc.get("topic", ""), (sc.get("reason") or "")[:80]))
    return "\n".join(parts)


def chat_last_context(msg):
    """用户消息提到"刚才那条/这条"等指代时, 附上最近点开条目的简介(供"讲讲刚才那条")。"""
    if not (REF_NOUN.search(msg or "") and last_feed_item()):
        return ""
    it = last_feed_item()
    line = "【用户指代的最近条目】%s《%s》作者:%s" % (it.get("platform", ""), it.get("title", ""), it.get("author", ""))
    if it.get("desc"):
        line += " 简介:" + it.get("desc", "")[:300]
    return line


# 指代句式: 动作 + 最近条目
REF_ACTIONS = [
    (re.compile(r"收藏|稍后看|留一下|留个|先看|想看"), "saved"),
    (re.compile(r"屏蔽|拉黑|别让我看到|别让我刷到"), "author"),
    (re.compile(r"不喜欢|别推|别刷|不看|删掉|踩一下|不感兴趣"), "dislike"),
    (re.compile(r"点个赞|赞一下|给.*赞|不错|好看|喜欢这条"), "like"),
]
REF_NOUN = re.compile(r"刚才|上一条|那条|这条|刚刚|前面|这个|那个|它|刚才那条")


def handle_reference(m):
    """“刚才那条”→ 收藏/屏蔽/不喜欢/点赞。命中返回命令结果, 否则 None。"""
    if not REF_NOUN.search(m):
        return None
    act = None
    for pat, name in REF_ACTIONS:
        if pat.search(m):
            act = name
            break
    if not act:
        return None
    it = last_feed_item()
    if not it or not it.get("key"):
        return {"reply": "我还没记住你看过的内容，先去左侧刷几条再来指它吧～", "trigger": None, "chips": None}
    key, title, author = it.get("key", ""), it.get("title", "(那条)"), it.get("author", "")
    if act == "saved":
        db().execute("INSERT OR REPLACE INTO saved(key,kind,ts) VALUES(?,'later',?)",
                     (key, int(time.time())))
        db().commit()
        return {"reply": "已稍后看「%s」" % title[:26], "trigger": "saved", "chips": None}
    if act == "author":
        if not author:
            return {"reply": "那条没有作者信息，没法屏蔽～", "trigger": None, "chips": None}
        db().execute("INSERT OR IGNORE INTO blacklist(kind,value) VALUES('author',?)", (author,))
        db().commit()
        return {"reply": "已屏蔽作者「%s」，他的内容不再出现" % author, "trigger": "settings", "chips": None}
    if act == "dislike":
        db().execute("INSERT INTO feedback(key,author,action,ts) VALUES(?,?,?,?)",
                     (key, author, "dislike", int(time.time())))
        db().commit()
        return {"reply": "好，已记录不喜欢「%s」，以后同类内容少推" % title[:26],
                "trigger": "settings", "chips": None}
    if act == "like":
        db().execute("INSERT INTO feedback(key,author,action,ts) VALUES(?,?,?,?)",
                     (key, author, "like", int(time.time())))
        db().commit()
        return {"reply": "已收藏进偏好「%s」，之后会多推这类" % title[:26], "trigger": "settings", "chips": None}
    return None


def build_chips():
    """回复下方的快捷指令(可点击, 点击即作为新消息发送)。"""
    try:
        enabled = enabled_source_ids()
        game_on = "game" in enabled or not _subset_mode(enabled)
    except Exception:
        game_on = False
    mode = sget("mode")
    chips = [
        {"label": "🎲 换一批", "cmd": "随机来一批"},
        {"label": "📋 今日简报", "cmd": "今日简报"},
        {"label": "📊 今日统计", "cmd": "今日统计"},
        {"label": "⚙️ 当前配置", "cmd": "当前配置"},
    ]
    chips.append({"label": "🙅 关掉游戏" if game_on else "🎮 只看游戏",
                  "cmd": "关掉游戏" if game_on else "只看游戏"})
    chips.append({"label": "🔒 更严格点" if mode != "严格" else "🫧 宽松点",
                  "cmd": "更严格点" if mode != "严格" else "宽松点"})
    return chips


def apply_partition_ids(ids):
    """写入分区开关并立即生效(清不匹配沉淀池 + 重建)。返回展示名串。"""
    sset("m_sources", json.dumps(sorted(set(ids))))
    purge_items_by_parts(set(ids))
    force_rebuild()
    return "、".join(_ID2NAME[i] for i in sorted(ids) if i in _ID2NAME)


def _open_explore():
    """探索模式执行器(命令引擎与AI意图共用): 30分钟内拉高探索区配额。"""
    sset("explore_boost_ts", str(int(time.time() + 30 * 60)))
    preview = _explore_candidates(set(), limit=5)
    lines = " / ".join("《%s》" % ((it.get("title") or "")[:22]) for it in preview) or "暂无跨领域候选"
    return "已开启探索模式（30分钟）：探索区配额临时拉高到 30%。候选预览：" + lines


def handle_command(msg):
    """聊天框命令引擎(无需AI): 识别并直接执行设置/屏蔽/统计/随机等指令。"""
    m = msg.strip()
    # --- 会话指代("刚才那条" → 收藏/屏蔽/不喜欢/点赞) ---
    ref = handle_reference(m)
    if ref:
        return ref
    # --- 新建/切换对话 ---
    if any(w in m for w in ("新对话", "新建对话", "换个话题", "重新开个", "开个新对话", "开新对话")):
        cid = conv_create()
        return {"reply": "已开新对话。说说这次想看点什么？", "trigger": "conv", "cid": cid}
    # --- 今日简报(AI评价+偏好/厌恶增减建议; 未配置AI时模板兜底) ---
    if any(w in m for w in ("今日简报", "简报", "今天看了什么", "今天看了啥", "总结今天", "今天刷了什么")):
        reply, rep_chips, _trig = daily_report_reply()
        return {"reply": reply, "trigger": _trig, "chips": rep_chips}
    # --- 统计 ---
    if any(w in m for w in ("成本", "花了多少", "统计", "调用次数", "花了多少钱", "花了多少")):
        st = day_stats()
        pct = round(st["filtered"] / st["received"] * 100) if st["received"] else 0
        return {"reply": "今日收录 %s 条，剔除 %s 条（%s%%）；AI 调用 %s 次，成本约 ¥%s。" % (
            st["received"], st["filtered"], pct, st["ai_calls"],
            st["cost"] if st["cost"] >= 0.01 else "%.4f" % st["cost"]), "trigger": None}
    # --- 档位 ---
    for w in ("严格", "标准", "宽松", "严肃"):
        if w in m and ("档" in m or "模式" in m or len(m) <= 4):
            sset("mode", w)
            return {"reply": "已切到【%s】档（引战剔除阈值 %s，%s）" % (
                w, MODE_THRESH[w],
                "严肃模式：只推硬核教程与知识" if w == "严肃" else ""), "trigger": "settings"}
    # --- 时间窗 ---
    mm = re.search(r"最近\s*(\d+)\s*个?月", m)
    if mm:
        sset("m_max_age_months", mm.group(1))
        return {"reply": "已设置：只显示最近 %s 个月的内容（0=不限）" % mm.group(1), "trigger": "settings"}
    # --- 分区切换(只认明确指令句式, 避免聊天误触) ---
    found = [g for g in GROUP_NAMES if g in m]
    verbs = ("只看", "只刷", "只要", "只开", "就刷", "就开", "只留", "光刷", "就留",
             "关掉", "关闭", "不要", "打开", "加上", "加个", "开个", "切换", "改成", "设置为", "调成")
    if found and any(v in m for v in verbs):
        want = {GROUP_NAMES[g] for g in found}
        if any(v in m for v in ("只看", "只刷", "只要", "只开", "就刷", "就开", "只留", "光刷", "就留", "切换", "改成", "设置为", "调成")):
            ids = sorted(want)
        elif any(v in m for v in ("关掉", "关闭", "不要")):
            cur = enabled_source_ids()
            base = cur if (cur and cur != {g[0] for g in SOURCE_GROUPS}) else {g[0] for g in SOURCE_GROUPS}
            ids = sorted(base - want)
        else:
            cur = enabled_source_ids()
            base = cur if (cur and cur != {g[0] for g in SOURCE_GROUPS}) else set()
            ids = sorted(base | want)
        if ids:
            names = apply_partition_ids(ids)
            return {"reply": "已切换分区：%s（%d 个，点击即生效）" % (names, len(ids)), "trigger": "settings"}
    # --- 临时屏蔽(实时干预): "未来1小时内别推游戏" / "接下来30分钟少推二次元" ---
    # 放在厌恶追加之前: 带时限的"别推"是临时屏蔽, 不应写入永久厌恶备注
    mt = re.search(r"(?:未来|接下来|之后)?\s*(\d+)\s*(?:个)?(小时|分钟)(?:内|之内)?\s*(?:都)?(?:别|少|不要)\s*(?:再)?\s*(?:给我)?\s*(?:推|刷|来|看|整)\s*([^，。,\s]{1,12})", m)
    if mt:
        try:
            n = int(mt.group(1))
        except Exception:
            n = 1
        unit = mt.group(2)
        mins = n * 60 if unit == "小时" else n
        kw = mt.group(3).strip("的了啊吧 ，, ")
        if 2 <= len(kw) <= 12 and 1 <= mins <= 24 * 60:
            add_tmp_block(kw, mins)
            until = time.strftime("%H:%M", time.localtime(time.time() + mins * 60))
            return {"reply": "已记下：未来 %s 内不推「%s」，%s 自动解封。" % (
                "%d个%s" % (n, unit), kw, until), "trigger": "random"}
    # --- 探索模式(实时干预): 临时拉高探索区配额并预览新领域 ---
    if any(w in m for w in ("探索", "发现新领域", "来点新东西", "看看别的", "换换领域", "新鲜领域")):
        return {"reply": _open_explore(), "trigger": "random"}
    # --- 厌恶追加(把不想看的内容写进厌恶备注) ---
    # 隐含句式: "我不想看/别看/别推/少来 X" (取动词后的关键词, 到标点为止)
    mdis = re.search(r"(?:别|不要|别再|不想再|不想|少来|别看|别刷|别推)[^。，,\s]*?(?:推|看|刷|来)\s*([^。，,\s]{2,16})", m)
    # 显式句式: "把 X 写到/加入 厌恶备注"
    mex = re.search(r"把\s*([^。，,\s]{2,16})\s*(?:写到|写进|加入|加到|添加到|记到|放入)\s*厌恶备注", m)
    # 直接句式: "少来/别来/别整 X" (无推看刷动词)
    mdir = re.search(r"(?:少来|别来|别整|别看)\s*([^。，,\s]{2,16})", m)
    extra = (mdis.group(1) if mdis else (mex.group(1) if mex else (mdir.group(1) if mdir else "")))
    if extra:
        # 去掉常见后缀(把"男女对立视频"归一成核心词"男女对立", 匹配更通用)
        extra = re.sub(r"(视频|内容|话题|帖子|相关|之类|东西|这类|这些)+$", "", extra).strip("的了啊吧 ，, ")
        if 2 <= len(extra) <= 16:
            cur = sget("dislike_note")
            sset("dislike_note", (cur + "，" + extra).strip("，") if cur else extra)
            clear_score_cache()   # 厌恶画像变化 → 旧语义分数过时, 强制重评
            force_rebuild()       # 非全扫模式下厌恶词靠机器层过滤, 必须重建池子才立即生效
            return {"reply": "已记下：以后不给你看「%s」相关的内容" % extra, "trigger": "settings"}
    # --- 屏蔽作者 ---
    mbl = re.search(r"(?:屏蔽|拉黑|别让我看到)(?:作者|up主|UP主|up|UP)?\s*([\w\u4e00-\u9fa5·]{2,20})", m)
    if mbl:
        name = mbl.group(1).strip("的了 ")
        with _db_lock:
            db().execute("INSERT OR IGNORE INTO blacklist(kind,value) VALUES('author',?)", (name,))
            db().commit()
        force_rebuild()   # 黑名单即刻生效: 重建池子并重排计划
        return {"reply": "已屏蔽作者：%s（他的内容不再出现）" % name, "trigger": "settings"}
    # --- 当前配置回显 ---
    if any(w in m for w in ("现在的设置", "当前设置", "我看下设置", "当前配置", "现在什么配置", "看看设置", "设置是什么")):
        enabled = enabled_source_ids()
        names = "、".join(_ID2NAME[i] for i in sorted(enabled) if i in _ID2NAME) or "默认全部"
        return {"reply": "当前配置| 分区：%s ｜ 档位：%s ｜ 时间窗：%s 个月 ｜ AI：%s" % (
            names, sget("mode"), sget("m_max_age_months"),
            "已开启" if ai_on() else ("已配置(总开关关)" if ai_ok() else "未配置")), "trigger": None}
    # --- 随机 ---
    if any(w in m for w in ("随机", "随便", "惊喜", "换换口味", "来点新鲜", "随便来点")):
        return {"reply": "好，随机来一批！已刷新为新内容。", "trigger": "random"}
    return None


def _note_add(note, kws):
    """把关键词追加进逗号分隔备注(去重)。返回 (新备注, 实际新增列表)。"""
    new, added = note, []
    for k in kws:
        k = (k or "").strip()
        if not k:
            continue
        if k not in new:
            new = (new + "，" + k).strip("，") if new else k
            added.append(k)
    return new, added


def _note_remove(note, kws):
    """从逗号分隔备注中剔除含关键词的片段。返回 (新备注, 实际移除列表)。"""
    parts = [p.strip() for p in re.split(r"[，,、;；]", note or "") if p.strip()]
    removed = []
    for k in kws:
        k = (k or "").strip().lower()
        if not k:
            continue
        keep = []
        for p in parts:
            if k in p.lower() and p not in removed:
                removed.append(p)
            else:
                keep.append(p)
        parts = keep
    return "，".join(parts), removed


def handle_chat_payload(payload):
    """chips 确认通道: 带结构化 payload 的消息直接执行对应操作(不进命令引擎/意图识别)。
    写入类操作统一清评分缓存+重建, 与既有厌恶写入路径一致。"""
    op = (payload or {}).get("op", "")
    kws = [str(k).strip() for k in (payload or {}).get("keywords", []) if str(k).strip()]
    if op == "add_pref":
        if not kws:
            return {"reply": "没有要写入的偏好词。", "trigger": None}
        cur = sget("prefs_note")
        new, added = _note_add(cur, kws)
        if added:
            sset("prefs_note", new)
            clear_score_cache()
            force_rebuild()
        return {"reply": "已写进偏好备注：%s。之后会多推这类内容。" % ("、".join(added) or "(已在备注中)"),
                "trigger": "settings"}
    if op == "remove_pref":
        new, removed = _note_remove(sget("prefs_note"), kws)
        if removed:
            sset("prefs_note", new)
            clear_score_cache()
            force_rebuild()
        return {"reply": ("已从偏好备注移除：%s" % "、".join(removed)) if removed
                else "偏好备注里没有这些词，无需改动。", "trigger": "settings"}
    if op == "add_dislike":
        new, added = _note_add(sget("dislike_note"), kws)
        if added:
            sset("dislike_note", new)
            clear_score_cache()
            force_rebuild()
        return {"reply": "已写进厌恶备注：%s。之后会避开这些内容。" % ("、".join(added) or "(已在备注中)"),
                "trigger": "settings"}
    if op == "remove_dislike":
        new, removed = _note_remove(sget("dislike_note"), kws)
        if removed:
            sset("dislike_note", new)
            clear_score_cache()
            force_rebuild()
        return {"reply": ("已从厌恶备注移除：%s，相关内容可能重新出现" % "、".join(removed)) if removed
                else "厌恶备注里没有这些词，无需改动。", "trigger": "settings"}
    if op == "partition_only":
        ids = sorted({GROUP_NAMES[g] for g in kws if g in GROUP_NAMES})
        if not ids:
            return {"reply": "没有可切换的分区。", "trigger": None}
        names = apply_partition_ids(ids)
        return {"reply": "已切换分区：只看 %s（%d 个）" % (names, len(ids)), "trigger": "settings"}
    if op == "noop":
        return {"reply": "好的，没有做任何改动。", "trigger": None}
    return {"reply": "未知操作，已忽略。", "trigger": None}


# 每日简报: 聚合今日数据(观看/喜欢/不喜欢/漏斗/成本/画像) → LLM 评价与偏好建议
def daily_report_data():
    """纯本地数据聚合(零AI)。返回 (数据文本, 观看数, 喜欢数, 不喜欢数)。"""
    midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))

    def _brief(key):
        it = item_by_key(key)
        if not isinstance(it, dict):
            return None
        return {"t": (it.get("title") or "")[:30], "a": (it.get("author") or "")[:14],
                "topic": (it.get("topic") or (it.get("tag") or "").split(",")[0] or it.get("tname") or "")[:10],
                "p": it.get("platform", ""), "src": it.get("src", "")}

    seen_rows = db().execute("SELECT key, ts FROM shown WHERE ts >= ? ORDER BY ts DESC",
                             (int(midnight),)).fetchall()
    seen = [b for b in (_brief(k) for (k, _ts) in seen_rows) if b]
    fb_rows = db().execute("SELECT key, action FROM feedback WHERE ts >= ? AND action IN ('like','dislike')",
                           (int(midnight),)).fetchall()
    likes = [b for b in (_brief(k) for (k, act) in fb_rows if act == "like") if b]
    dislikes = [b for b in (_brief(k) for (k, act) in fb_rows if act == "dislike") if b]

    def _top(seq, field, n):
        cnt = {}
        for x in seq:
            v = (x.get(field) or "").strip()
            if v:
                cnt[v] = cnt.get(v, 0) + 1
        return "、".join("%s×%d" % (k, c) for k, c in sorted(cnt.items(), key=lambda kv: -kv[1])[:n]) or "无"

    pref_hits = sum(1 for x in seen if (x.get("src") or "").startswith("偏·"))
    funnel = funnel_stats()
    top_blocks = sorted(((k, v) for k, v in funnel.items()
                         if k not in ("fetched", "kept", "build_full", "build_quick")),
                        key=lambda kv: -kv[1])[:3]   # 拦截数Top3的规则
    st = day_stats()
    blocks = ["今日观看: %d 条 (偏好直搜命中 %d)" % (len(seen), pref_hits),
              "观看主题Top: " + _top(seen, "topic", 5),
              "观看作者Top: " + _top(seen, "author", 5),
              "平台分布: " + _top(seen, "p", 3),
              "今日点喜欢 %d 条: %s" % (len(likes),
                                       "; ".join("《%s》(%s)" % (x["t"], x["a"]) for x in likes[:12]) or "无"),
              "今日点不喜欢 %d 条: %s" % (len(dislikes),
                                          "; ".join("《%s》(%s)" % (x["t"], x["a"]) for x in dislikes[:12]) or "无"),
              "筛选漏斗: 抓回%s条, 最终入池%s条, 拦截大头: %s" % (
                  funnel.get("fetched", 0), funnel.get("kept", 0),
                  "、".join("%s×%s" % (_FUNNEL_LABELS.get(k, k), v) for k, v in top_blocks) or "无"),
              "AI成本: ¥%s (调用%s次)" % (st["cost"] if st["cost"] >= 0.01 else "%.4f" % st["cost"], st["ai_calls"]),
              "当前偏好备注: %s" % ((sget("prefs_note") or "（空）")[:200]),
              "当前厌恶备注: %s" % ((sget("dislike_note") or "（空）")[:200])]
    return "\n".join(blocks), len(seen), len(likes), len(dislikes)


def daily_report_reply():
    """今日简报: AI 开启 → LLM 评价+偏好/厌恶增减建议(收尾行解析成确认chips);
    未配置 → 模板统计兜底。返回 (reply, chips, trigger)。"""
    data, n_seen, n_like, n_dis = daily_report_data()
    if n_seen == 0 and n_like == 0 and n_dis == 0:
        return "今天还没有看/互动记录，先去信息流刷几条，晚上再来找我复盘吧～", None, None
    if not ai_on():
        return ("今天看了 %d 条内容。\n%s\n\n（配置 AI 后我可以给出口味分析和偏好调整建议）" % (
            n_seen, data.split("\n", 1)[1].split("\nAI成本")[0])), None, None
    prompt = ("你是「洞鉴虚明」的信息分拣分析师。以下用户今天的使用数据(本地统计), 请输出:\n"
              "①总体评价(2-3句, 口语化, 说今天内容质量如何/偏好命中如何)\n"
              "②口味归纳(今天明显偏爱什么)\n"
              "③偏好与厌恶调整建议: 结合已有备注给出具体可执行的增减(减少=写得太宽或已过时的词);\n"
              "输出末尾必须恰好以这两行收尾(没有建议写'无'):\n"
              "建议新增偏好：词1、词2\n"
              "建议新增厌恶：词A、词B\n"
              "不要输出markdown符号。\n\n使用数据:\n" + data)
    try:
        content, _ = call_llm([{"role": "system", "content": prompt}], max_tokens=900)
    except Exception as e:
        return "简报生成失败（%s）。数据如下：\n%s" % (str(e)[:80], data), None, None
    reply = (content or "").strip() or "今天数据不多，没有特别的建议。"

    def _tail(line_name):
        m = None
        for line in reply.splitlines()[::-1]:   # 取最后出现的收尾行(防正文引用干扰)
            if line.strip().startswith(line_name):
                m = line.split("：", 1)[-1].split(":", 1)[-1]
                break
        if not m:
            return []
        return [w.strip() for w in re.split(r"[，,、;；]", m) if 1 < len(w.strip()) <= 16][:6]

    chips = []
    add_pref, add_dis = _tail("建议新增偏好"), _tail("建议新增厌恶")
    if add_pref and "无" not in add_pref:
        chips.append({"label": "✅ 采纳偏好建议：" + "、".join(add_pref[:3]), "cmd": "确认写入偏好建议",
                      "payload": {"op": "add_pref", "keywords": add_pref}})
    if add_dis and "无" not in add_dis:
        chips.append({"label": "🚫 采纳厌恶建议：" + "、".join(add_dis[:3]), "cmd": "确认写入厌恶建议",
                      "payload": {"op": "add_dislike", "keywords": add_dis}})
    return reply, (chips or None), None


def extract_intent(msg):
    """从聊天消息提取意图(JSON动作集, LLM识别+句式兜底):
    search / add_dislike / add_pref / set_mode / set_window / toggle_partition /
    open_explore / digest / reply"""
    msg = (msg or "").strip()
    if not msg:
        return {"action": "reply", "keywords": []}
    # 指代句式("刚才那条")由命令引擎处理, 不触发搜索
    if REF_NOUN.search(msg) and any(p.search(msg) for p, _ in REF_ACTIONS):
        return {"action": "reply", "keywords": []}
    if ai_on():
        try:
            content, _ = call_llm([
                {"role": "system", "content":
                    "用户在洞鉴虚明(内容筛选器)的聊天框说话, 判断他想让系统做什么, 只输出JSON(不要多余文字):\n"
                    "1) 想刷/推/看/找某类内容 → {\"action\":\"search\",\"keywords\":[\"1-2个搜索词\"]}\n"
                    "2) 想把某些词/内容加进厌恶备注(不想看/不喜欢/屏蔽/别再推) → "
                    "{\"action\":\"add_dislike\",\"keywords\":[\"词1\",\"词2\",...]}\n"
                    "   关键词要具体(如:性别对立、两性对立、彩礼纠纷), 可补充相关词, 2-15个, 每个2-16字\n"
                    "3) 想多看某类内容/喜欢某类(多推/以后常推/写进偏好) → "
                    "{\"action\":\"add_pref\",\"keywords\":[\"词1\",\"词2\",...]}, 关键词要求同上\n"
                    "4) 想切换档位(严格/标准/宽松/严肃) → {\"action\":\"set_mode\",\"mode\":\"严格\"}\n"
                    "5) 想调时间窗(只看最近N个月/新鲜一点的) → {\"action\":\"set_window\",\"months\":N}\n"
                    "   (N为月数整数; 「不限/都要」→ months=0)\n"
                    "6) 想调分区(只看某几个分区/加上/关掉某分区) → "
                    "{\"action\":\"toggle_partition\",\"groups\":[\"分区名\"],\"mode\":\"only|add|remove\"}\n"
                    "   分区名必须是: 全站榜/热门/动画/番剧/国创/鬼畜/音乐/舞蹈/游戏/电竞/知识/科技/数码/\n"
                    "   生活/美食/动物圈/生活Vlog/影视/电影/电视剧/纪录片/运动/赛事/汽车/时尚/美妆/娱乐/\n"
                    "   漫画/资讯/直播/军事 之一; 只看→only, 加上→add, 关掉→remove\n"
                    "7) 想看点新领域/探索模式 → {\"action\":\"open_explore\"}\n"
                    "8) 想要今日简报/复盘今天看了什么 → {\"action\":\"digest\"}\n"
                    # 改界面/显示这类话不能当搜索: "帮我换个布局"曾被判成 search,
                    # 回了句"已按你的要求刷「换个布局」相关视频"。设置通道在前端先跑,
                    # 这里兜的是它没接住的残句(如"换回去"), 让它落到聊天而不是搜索。
                    "9) 想改界面/显示设置(主题、单栏/双栏/大图、字号、封面、推荐理由、"
                    "强调色、来源标识、平台占比、直播筛选) → "
                    "{\"action\":\"reply\",\"keywords\":[]}; 这类交给设置通道, 别当搜索词\n"
                    "10) 其他闲聊/提问 → {\"action\":\"reply\",\"keywords\":[]}"},
                {"role": "user", "content": msg[:300]}], max_tokens=1500)
            d = json.loads(content)
            if isinstance(d, dict):
                act = d.get("action")
                if act == "search":
                    kws = [k.strip() for k in d.get("keywords", []) if k.strip()]
                    if kws:
                        return {"action": "search", "keywords": kws[:2]}
                    return {"action": "reply", "keywords": []}
                if act == "add_dislike":
                    kws = [k.strip() for k in d.get("keywords", [])
                           if k.strip() and 2 <= len(k.strip()) <= 16]
                    if kws:
                        return {"action": "add_dislike", "keywords": kws[:15]}
                    return {"action": "reply", "keywords": []}
                if act == "add_pref":
                    kws = [k.strip() for k in d.get("keywords", [])
                           if k.strip() and 2 <= len(k.strip()) <= 16]
                    if kws:
                        return {"action": "add_pref", "keywords": kws[:15]}
                    return {"action": "reply", "keywords": []}
                if act == "set_mode":
                    md = (d.get("mode") or "").strip()
                    if md in MODE_THRESH:
                        return {"action": "set_mode", "mode": md}
                    return {"action": "reply", "keywords": []}
                if act == "set_window":
                    try:
                        months = int(d.get("months"))
                    except Exception:
                        months = -1
                    if 0 <= months <= 60:
                        return {"action": "set_window", "months": months}
                    return {"action": "reply", "keywords": []}
                if act == "toggle_partition":
                    groups = [str(g).strip() for g in d.get("groups", []) if str(g).strip() in GROUP_NAMES]
                    mode = d.get("mode")
                    if groups and mode in ("only", "add", "remove"):
                        return {"action": "toggle_partition", "groups": groups[:6], "mode": mode}
                    return {"action": "reply", "keywords": []}
                if act == "open_explore":
                    return {"action": "open_explore"}
                if act == "digest":
                    return {"action": "digest"}
                if act == "reply":
                    return {"action": "reply", "keywords": []}
        except Exception:
            pass
    # 无AI规则的句式兜底: 前置词 → 动词 → 尾缀 逐层剥离
    m = msg
    for pre in ("帮我刷", "给我刷", "帮我看", "给我看", "帮我找", "给我找", "帮我", "给我",
                "我想", "我要", "想要", "请", "现在"):
        if m.startswith(pre):
            m = m[len(pre):]
            break
    for v in ("多推点", "多来点", "来点", "多推", "多来", "搜索一下", "搜索", "推荐", "找点",
              "刷刷", "刷", "看", "推", "找", "来", "搜"):
        if m.startswith(v):
            m = m[len(v):]
            break
    for suf in ("视频", "内容", "一下吧", "一下", "吧", "啊", "的", "些", "点"):
        if m.endswith(suf):
            m = m[:-len(suf)]
            break
    m = m.strip(" ，,、的")
    # 疑问句/日常闲聊/寒暄不触发搜索
    if any(w in m for w in ("怎么样", "如何", "怎么", "吗", "呢", "啥", "哪", "为什么", "什么",
                            "好不好", "行不行", "可以吗", "有没有", "你觉得", "你认为", "谁")):
        return {"action": "reply", "keywords": []}
    if any(w in m for w in ("你好", "谢谢", "再见", "哈哈", "在吗", "早安", "午安", "晚安",
                            "嗯嗯", "666", "好的", "讲个故事", "讲个笑话", "废话", "聊聊")):
        return {"action": "reply", "keywords": []}
    if 2 <= len(m) <= 30:
        return {"action": "search", "keywords": [m]}
    return {"action": "reply", "keywords": []}


# ---------------------------------------------------------------- 分拣管线

def negative_feedback_filter(items):
    """负反馈硬排除(所有出口通用): 已点"不喜欢"的条目 + 黑名单作者/关键词 + 临时屏蔽。
    这三类用户信号此前只写入不读取, 是"不想看的内容反复出现"的直接原因。"""
    disliked = {r[0] for r in db().execute(
        "SELECT DISTINCT key FROM feedback WHERE action='dislike'").fetchall()}
    bl_authors = {r[0] for r in db().execute("SELECT value FROM blacklist WHERE kind='author'")}
    bl_kws = [r[0].lower() for r in db().execute("SELECT value FROM blacklist WHERE kind='keyword'")]
    tmp_kws = tmp_block_kws()
    if not (disliked or bl_authors or bl_kws or tmp_kws):
        return items
    out = []
    for it in items:
        if it.get("key") in disliked:
            continue
        if it.get("author") in bl_authors:
            continue
        text = ((it.get("title") or "") + " " + (it.get("desc") or "") + " " +
                (it.get("author") or "") + " " + (it.get("tag") or "") + " " +
                (it.get("tname") or "") + " " + (it.get("topic") or "")).lower()
        if bl_kws and any(k in text for k in bl_kws):
            continue
        if tmp_kws and any(k in text for k in tmp_kws):
            continue
        out.append(it)
    return out


def apply_filters(items, q=""):
    out = []
    for it in negative_feedback_filter(items):
        if q:   # q 非空=搜索子集过滤; 空 q 时只做负反馈过滤, 保持世代顺序
            text = (it["title"] + " " + it["desc"] + " " + it["author"]).lower()
            if q.lower() not in text:
                continue
        out.append(it)
    return out


_last_top = {"key": None}   # 上次顶部条目(防连续刷新顶部重复)


DISLIKE_EXPAND = [   # 厌恶词含"二次元"时启用: 常见二游IP/题材识别词(机器层, 无需AI)
    "崩坏", "星穹铁道", "星穹", "原神", "米哈游", "明日方舟", "碧蓝", "绝区零", "战双",
    "少女前线", "型月", "fgo", "东方project", "魔法禁书", "约战", "蔚蓝档案", "nikke",
    "恋与深空", "光与夜", "乙女游戏", "galgame", "二游", "抽卡", "二次元游戏", "米游", "崩3"]


def dislike_keywords():
    """从厌恶备注提取屏蔽关键词(逗号分隔/去前缀, 机器层直接过滤)。
    厌恶词含"二次元"时自动并入二游IP/题材扩展词, 弥补字面匹配盲区。"""
    raw = sget("dislike_note") or ""
    if not raw.strip():
        return []
    prefix = ("讨厌", "不喜欢", "不要", "别推", "别", "反感", "烦", "拒绝", "屏蔽", "尽量别", "少推", "避免")
    out = []
    for part in re.split(r"[，,、;；\n。！？!?]+", raw):
        p = part.strip()
        for pre in prefix:
            if p.startswith(pre):
                p = p[len(pre):]
                break
        p = p.strip("的了和与及，,。 ")
        if 2 <= len(p) <= 12:
            out.append(p.lower())
    if any("二次元" in k or "动漫" in k for k in out):
        out += [k.lower() for k in DISLIKE_EXPAND]
    return list(dict.fromkeys(out))



# ------------------------------------------------- B站作者画像(评论区: 无个性简介 / 动态抽奖)

# 用途: 「其他设置 · 评论筛选 → B站」里那两个账号级开关 —— 在播放页评论区把这两类
# 账号的评论摘掉。**不再作用于信息流**(v0.3.1.20 起; 信息流不再按简介/抽奖过滤)。
#
# "无个性简介": B站把**空简介**显示为「这个人很神秘，什么都没有写」——
# 实测(2026-10-08 用真实 Cookie): 空简介账号 sign 就是空串, 那句"神秘"只是前端占位
# 文案。而评论接口(x/v2/reply*)的 member 里**自带 sign**(顶层与楼中楼都实测有), 所以
# 这一项在页面端就能就地判定: 零请求、零延迟 —— 这里只保留同规则的兜底实现。
#
# "动态抽奖": 评论本身看不出抽奖, 只能按作者最近 3 条动态判, 且必须登录 B站 ——
# 于是走下面的 author_meta 缓存: 页面端只读缓存(命中即拦), 未命中的作者丢进后台
# 限速队列慢慢补, 绝不把网络等待放在评论加载的关键路径上。
MYSTERY_SIGN = "这个人很神秘，什么都没有写"
# B站「这个账号不存在」的返回码。实测 acc/info 对不存在的 mid 返回 -404 + message「啥都木有」;
# 风控(-352 等)是**另一回事** —— 那是"查不到", 绝不能当成"不存在"(会误杀)。
_BILI_NO_SUCH_MID = frozenset((-404, -400))
# 抽奖文本标记(B站抽奖动态普遍带这些话题/短语)
LOTTERY_KW = ("互动抽奖", "动态抽奖", "转发抽奖", "抽奖活动", "抽奖福利", "宠粉福利",
              "粉丝福利", "福利抽奖")
# 只出现"抽奖"两字不判(可能是在聊抽奖机制) —— 必须同时出现"参与类"线索
LOTTERY_CUE = ("转发", "关注", "参与", "转赞评", "点赞", "评论", "开奖", "中奖", "获奖",
               "抽1", "抽一", "抽送", "送出", "奖品", "名额", "恭喜", "欢迎", "拼手气")

_author_meta = {}                  # "mid" -> {"sign","mystery","lottery","ts"}
_author_meta_lock = threading.Lock()
_AUTHOR_META_TTL = 24 * 3600       # 作者画像缓存 24h(内存 + DB 双层)
# v0.3.1.45dsk: 队列/单次认领上限放宽 —— 旧值 400/60 面对"一个播放页上千个候选"
# 会当场丢掉大部分 mid(实测 73 个候选就丢了 13 个), 而丢掉的既不进 seen 也不重试。
_AUTHOR_WARM_MAX = 3000            # 后台补图队列上限(防无界增长)
_AUTHOR_WARM_GAP = 0.6             # 保留: 兼容旧配置读取(实际限速由令牌桶给)
_AUTHOR_WARM_CAP = 400             # 单次请求最多认领的未知作者数
_AM_RETRY_MAX = 6                  # 同一个 mid 最多补图尝试次数(超了就不再排队, 免得死循环)
_AM_RETRY_DELAY = 45.0             # 失败后至少隔这么久才允许重入队(秒); 第 n 次退避 = n × 它
_AM_RETRY_TICK = 3.0               # 重试调度线程的巡检间隔(秒)
_am_warm_q = []                    # 待补图的 mid(有序 + 去重)
_am_warm_seen = set()
_am_warm_lock = threading.Lock()
# v0.3.1.45dsk: 失败重试状态。两张表分工 —— 计数与到期时间分开存,
# 否则"重入队时把条目从 _am_retry 删掉"会把尝试次数一起清零, 退避永远回到第 1 档
# (实测: 8 个 mid 在 14 秒里被打了 192 次, 封顶形同虚设)。
#   _am_tries: mid -> 已失败次数(成功/确认不存在时清除)
#   _am_retry: mid -> 最早可重试时间戳
_am_tries = {}
_am_retry = {}
# v0.3.1.30: 单线程 _am_warm_thread 已换成 _am_pool 线程池(见下方「筛选加速」段)
_am_conn_tls = threading.local()    # /api/bili/flags 专用连接(短 busy_timeout)
_AM_DB_BUSY_MS = 200                # 上述连接 busy_timeout(ms): DB 被写锁占住就立刻放弃读取


# ── v0.3.1.30 筛选加速: 有界线程池 + 令牌桶 ─────────────────────────────
# 实测瓶颈(2026-10-09, 用户库副本 + 真实接口):
#   · 逆推队列 1 线程 + _DMREV_GAP=0.25s → 3.4 个/秒; 逆推稳态其实只要 15.2ms
#     (66 个/秒) —— 那 0.25s 是**纯白等**, 白白浪费 19 倍。
#   · 画像队列 1 线程 + _AUTHOR_WARM_GAP=0.6s + 每账号两次串行 HTTP(557ms)
#     → 0.86 账号/秒; 200 个账号要 231 秒。
# 改法: 两个队列都改**有界线程池**; 网络侧再叠一层**令牌桶** ——
#   并发只用来掩盖延迟, **速率才是风控闸门**(并发开大不会提高请求速率)。
#
# ★★ v0.3.1.45dsk 重做速率口径(2026-10-10 实测, 隔离库副本 + 真实接口):
#   旧口径是「每**账号**一个令牌」, 而一个账号要打 **2 次** B站接口
#   (acc/info + 动态), 于是标称 7/s 实际是 **14 请求/秒**, 且动态那次完全不受闸门管。
#   A/B 实测(各 50 个候选 mid): 档位0(并发1) 92% 被 -352 拒、档位1 96%、
#   档位2 100%、档位3 100%; 而**顺序单发 16 次(6.7 请求/秒) 是 0%**。
#   ⇒ 触发风控的是"总请求速率 + 并发突发", 不是"账号数"。
#   现在: 速率口径改为 **B站请求/秒**(每个画像消耗 2 个令牌), 并**取消突发额度**
#   (burst=1, 不再让 6 个线程在入队瞬间同时打同一 cookie)。
_FILTER_GEARS = {
    # 档位 -> (画像并发, 画像请求速率/秒, 逆推并发, 逆推速率/秒)
    # 画像速率是 **B站请求/秒**(一个画像 = acc/info + 动态 = 2 个请求)
    # 逆推速率 0 = 不限速(纯本地 CPU, 单线程上限 ~66/s)
    "0": (1, 3.3, 1, 4.0),                 # 兼容: 约 1.7 个画像/秒(≈ v0.3.1.29 的 0.6s/个)
    "1": (2, 6.0, 2, 20.0),                # 标准: 约 3 个画像/秒
    "2": (3, 8.0, 3, 45.0),                # 快速(默认): 约 4 个画像/秒
    "3": (4, 14.0, 4, 0.0),                # 极速: 约 7 个画像/秒(请求量约默认 1.75 倍, 风险自评)
}
_gear_cache = {"v": None, "ts": 0.0}


class _TokenBucket(object):
    """令牌桶限速: 按 rate 匀速放行; 线程安全; rate <= 0 表示不限速。

    ★ v0.3.1.45dsk: 网络闸门**取消突发额度**(burst=1)。
      旧实现 burst = 并发数, 入队瞬间 N 个线程同时打同一 cookie —— 实测正是这种
      "同账号并发突发"最容易被 B站判成脚本行为并回 -352。宁可慢一点。
      短页面(条目数少)不再"一瞬间过完", 改为按 1/rate 逐条放行(8/s → 125ms/条), 可接受。
    """
    __slots__ = ("rate", "cap", "_t", "_last", "_lk")

    def __init__(self, rate, burst):
        self._lk = threading.Lock()
        self.rate = 0.0
        self.cap = 1.0
        self._t = 0.0
        self._last = time.time()
        self.set(rate, burst)
        with self._lk:
            self._t = self.cap

    def set(self, rate, burst):
        """改速率/额度。会先把「距上次结算这段时间」的令牌结息记上, 再按新额度调整 ——
        否则每次入队都调 set() 会把攒下的令牌悄悄清零。"""
        with self._lk:
            now = time.time()
            if self.rate > 0:
                self._t = min(self.cap, self._t + (now - self._last) * self.rate)
            new_cap = float(max(1.0, burst))
            if new_cap > self.cap:
                self._t += (new_cap - self.cap)
            self.rate = float(rate)
            self.cap = new_cap
            self._t = min(self.cap, self._t)
            self._last = now

    def acquire(self):
        """取一个令牌; 取不到就睡到能取为止。"""
        if self.rate <= 0:
            return True
        while True:
            with self._lk:
                now = time.time()
                self._t = min(self.cap, self._t + (now - self._last) * self.rate)
                self._last = now
                if self._t >= 1.0:
                    self._t -= 1.0
                    return True
                wait = (1.0 - self._t) / self.rate
            time.sleep(min(wait, 0.25))


def _filter_gear():
    """当前「筛选加速」档位。2 秒缓存 —— 免得每页都查一次库。"""
    now = time.time()
    if _gear_cache["v"] is not None and now - _gear_cache["ts"] < 2.0:
        return _gear_cache["v"]
    try:
        g = str(sget("m_filter_speed") or "2")
    except Exception:
        g = "2"
    v = _FILTER_GEARS.get(g) or _FILTER_GEARS["2"]
    _gear_cache["v"] = v
    _gear_cache["ts"] = now
    return v


_am_gate = _TokenBucket(0, 1)          # 画像请求闸门(每个 B站请求取 1 个令牌)
_dmrev_gate = _TokenBucket(0, 1)       # 逆推闸门(纯本地, 控的是 CPU 占空比)
_am_pool = []                          # 画像池的活线程
_dmrev_pool = []                       # 逆推池的活线程
# ★ v0.3.1.45dsk 风控硬退避。旧实现是"连续 3 次才把速率砍半, 5 分钟恢复", 后果很糟:
#   -352 一旦出现就是**全有全无**(账号/IP 已被标记), 继续按原速率打只会把标记打得更深;
#   而砍半后的速率面对"一页上千个候选"仍等于持续撞击。实测 73 个候选入队 → 7.5 秒排空
#   → 只落库 26 个, 剩下全部丢弃, 端到端 60 秒只筛出 2 条。
# 现在: 撞一次就**暂停整个画像队列**并指数退避(60→180→600→1800s), 期间不重试不写缓存;
#   任何一次成功立刻清零。队列条目留在队里, 恢复后继续, 不再丢。
_AM_RISK_COOLDOWN = (60.0, 180.0, 600.0, 1800.0)
_am_risk = {"streak": 0, "pause_until": 0.0, "hits": 0}


def _am_note_ok():
    """一次成功的 B站画像请求 → 退避清零(说明风控已解除)。

    同时把失败计数整表清零: 之前那些"重试次数用尽"的 mid 是在风控期间耗掉的,
    接口既然已经恢复, 它们就该重新获得一次机会(否则会一直死到重启)。
    """
    with _am_warm_lock:
        if _am_risk["streak"]:
            print("[filter] 画像请求已恢复, 风控退避清零")
        _am_risk["streak"] = 0
        _am_risk["pause_until"] = 0.0
        if _am_tries:
            _am_tries.clear()


def _am_note_risk(code):
    """acc/info 返回风控码(-352 等) → 立刻暂停画像队列并指数退避。"""
    if code is None or code == 0 or code in _BILI_NO_SUCH_MID:
        return
    now = time.time()
    if now < _am_risk["pause_until"]:
        return                      # 已在退避窗口内, 不重复加码
    idx = min(_am_risk["streak"], len(_AM_RISK_COOLDOWN) - 1)
    cool = _AM_RISK_COOLDOWN[idx]
    _am_risk["streak"] = min(_am_risk["streak"] + 1, len(_AM_RISK_COOLDOWN) - 1)
    _am_risk["hits"] += 1
    _am_risk["pause_until"] = now + cool
    print("[filter] 撞到 B站风控(返回码 %s): 画像队列暂停 %.0f 秒后重试(第 %d 次)"
          % (code, cool, _am_risk["hits"]))


def _am_paused():
    """撞风控后是否处于退避窗口内。"""
    return time.time() < _am_risk["pause_until"]


def _am_apply_gear():
    """把档位写进画像闸门, 返回并发数。速率口径 = B站请求/秒(每个画像 2 个请求)。"""
    conc, rate, _rc, _rr = _filter_gear()
    if _am_risk["streak"] and not _am_paused():
        _am_risk["streak"] = 0          # 退避窗口过完 → 从最小档重新开始(而非直接满速)
    # burst 恒为 1: 不允许入队瞬间并发突发(见 _TokenBucket 注释)。
    _am_gate.set(rate, 1.0)
    return int(conc)


def _dmrev_apply_gear():
    """把档位写进逆推闸门, 返回并发数。"""
    _c, _r, rconc, rrate = _filter_gear()
    _dmrev_gate.set(rrate, max(1.0, rconc))
    return int(rconc)


def _pool_fill(pool, conc, target):
    """保证 pool 里有 conc 个活线程(worker 队列空就退出, 入队时再补)。
    调用方须持有对应队列锁。"""
    pool[:] = [t for t in pool if t.is_alive()]
    for _ in range(max(0, int(conc) - len(pool))):
        t = threading.Thread(target=target, daemon=True)
        t.start()
        pool.append(t)


def _bili_get_t(url, timeout=5):
    """B站 GET, 短超时(作者画像用; 拖慢构建要不得)。"""
    req = urllib.request.Request(url, headers=dict(ACCOUNT_HEADERS, Cookie=account_cookie()))
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def _dyn_text(*objs):
    """从动态对象里抽取可见文本(desc.text / major.opus.summary / draw.description ...)。"""
    out = []

    def walk(o, depth=0):
        if depth > 7 or len(out) > 500:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str):
                    if k in ("text", "title", "description", "desc", "summary"):
                        out.append(v)
                elif isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o[:10]:
                walk(v, depth + 1)

    for o in objs:
        walk(o)
    return " ".join(out)


def _dyn_is_lottery(it):
    """单条动态是否抽奖:
    ① 硬标记 —— 官方"互动抽奖"附加模块(additional.type 含 LOTTERY / reserve / upower_lottery);
    ② 文本标记 —— 含"互动抽奖/动态抽奖/转发抽奖"等, 或 "抽奖" + 参与线索。"""
    if not isinstance(it, dict):
        return False
    for node in (it, it.get("orig")):
        if not isinstance(node, dict):
            continue
        add = (((node.get("modules") or {}).get("module_dynamic") or {}).get("additional") or {})
        if "LOTTERY" in str(add.get("type") or "").upper():
            return True
        if add.get("reserve") or add.get("upower_lottery") or add.get("lottery"):
            return True
    text = _dyn_text(it)
    if any(k in text for k in LOTTERY_KW):
        return True
    return ("抽奖" in text) and any(c in text for c in LOTTERY_CUE)


def _bili_acc_probe(mid):
    """返回 (data, code)。code == 0 才有 data。
    必须把「账号不存在」(-404 啥都木有)和「风控/失败」区分开: 前者是确定的结论,
    后者是"还不知道"。混为一谈会让「全命中才拦」永远等一个查不出画像的候选。"""
    qs = wbi_enc({"mid": mid, "token": "", "platform": "web", "web_location": 1550101})
    d = _bili_get_t("https://api.bilibili.com/x/space/wbi/acc/info?" + qs)
    if not isinstance(d, dict):
        return {}, None
    code = d.get("code")
    return ((d.get("data") or {}) if code == 0 else {}), code


def _bili_acc_info(mid):
    return _bili_acc_probe(mid)[0]


def _bili_recent_dynamics(mid, n=3):
    """作者最近 n 条动态。**None = 接口失败(未知)**, [] = 确实一条动态都没有 ——
    调用方必须区分: 失败时绝不能把作者写进缓存当成"没有动态"(空首页判定会误伤)。"""
    qs = wbi_enc({"host_mid": mid, "offset": "", "timezone_offset": -480,
                  "features": "itemOpusStyle", "web_location": 1550101})
    try:
        d = _bili_get_t("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?" + qs)
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("code") != 0:
        return None
    return ((d.get("data") or {}).get("items") or [])[:n]


def _am_conn():
    """页面脚本(/api/bili/flags)专用连接: 短 busy_timeout。
    评论加载的关键路径上, DB 被别的写事务占住时宁可立刻"读不到"(→ 放行),
    也绝不能让这个读把页面卡住。"""
    c = getattr(_am_conn_tls, "c", None)
    if c is None:
        c = sqlite3.connect(os.path.join(DATA_DIR, "data.db"),
                            check_same_thread=False, timeout=_AM_DB_BUSY_MS / 1000.0)
        try:
            c.execute("PRAGMA busy_timeout=%d" % _AM_DB_BUSY_MS)
            c.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        _am_conn_tls.c = c
    return c


_am_absent = {}                # mid -> 1(内存镜像; 表 absent_mid 是持久层)
_am_absent_ok = False
_am_absent_lock = threading.Lock()


def _am_is_absent(m, fast=False):
    """该 mid 是否已被确认**不存在**(acc/info 返回 -404「啥都木有」)。
    不存在的候选不可能是弹幕发送者 —— 多候选判定时必须排除它, 而不是当成"画像还没到"
    (否则「全部候选都命中才拦」永远等不到那几个永远查不出画像的候选, 规则永不生效)。
    读不到就返回 False(保守: 只会少拦, 不会误杀)。"""
    global _am_absent_ok
    m = str(m or "")
    with _am_absent_lock:
        if _am_absent_ok:
            return m in _am_absent
    try:
        rows = [str(r[0]) for r in (_am_conn() if fast else db()).execute(
            "SELECT mid FROM absent_mid") if r[0]]
    except Exception:
        return False
    with _am_absent_lock:
        for x in rows:
            _am_absent[x] = 1
        _am_absent_ok = True
        return m in _am_absent


def _am_mark_absent(m):
    m = str(m or "")
    if not m:
        return
    with _am_absent_lock:
        _am_absent[m] = 1
    try:
        db().execute("INSERT OR REPLACE INTO absent_mid(mid,ts) VALUES(?,?)",
                     (m, int(time.time())))
        db().commit()
    except Exception:
        pass


def _am_row_get(m, fast=False):
    """只读缓存(内存 → DB), 不联网。命中且未过期返回画像, 否则 None。
    fast=True: 给页面脚本用 —— 走短超时连接(_am_conn), 不长等 DB 锁。"""
    now = time.time()
    with _author_meta_lock:
        c = _author_meta.get(m)
    if c and now - c["ts"] < _AUTHOR_META_TTL:
        return c
    try:
        conn = _am_conn() if fast else db()
        row = conn.execute("SELECT sign,mystery,lottery,nodyn,level,ts FROM author_meta WHERE mid=?",
                           (m,)).fetchone()
    except Exception:
        return None
    if not row or now - int(row[5] or 0) >= _AUTHOR_META_TTL:
        return None
    if row[3] is None:      # 老记录(没有 nodyn 列 / 当时取动态失败) → 当作未知, 触发重取
        return None
    c = {"sign": row[0] or "", "mystery": bool(row[1]), "lottery": bool(row[2]),
         "nodyn": bool(row[3]), "level": (int(row[4]) if row[4] is not None else None),
         "ts": int(row[5])}
    with _author_meta_lock:
        _author_meta[m] = c
    return c


def bili_author_meta(mid, want_bio=True, want_lottery=True):
    """按 mid 取作者画像(简介是否空 / 最近动态是否含抽奖 / 是否一条动态都没有)。内存+DB 缓存。
    未登录或接口异常 → 返回 None(调用方一律放行, 宁可漏杀不可误杀)。
    一份缓存记录**永远是完整画像**(简介 + 动态一起取): 只取一半会把另一半写成
    "干净", 之后按缓存判定就会漏杀 —— 那两个 want_* 参数只为兼容旧调用点保留。"""
    m = str(mid or "")
    if not m.isdigit() or int(m) <= 0:
        return None
    c = _am_row_get(m)
    if c is not None:
        return c
    if not account_cookie():
        return None
    try:
        # ★ v0.3.1.45dsk: 每个 B站请求各取一个令牌(速率口径 = 请求/秒)。
        #   旧实现只给"账号"发令牌, 动态那次请求完全不受闸门管 —— 标称 7/s 实际 14 请求/秒,
        #   正是撞 -352 的直接原因。
        _am_gate.acquire()
        acc, _code = _bili_acc_probe(m)
        if _code == 0:
            _am_note_ok()           # 有一次成功 → 说明风控已解除, 退避清零
        else:
            _am_note_risk(_code)    # 撞风控 → 暂停整个画像队列并指数退避
        if not acc:
            # v0.3.1.27: 接口返回空(风控/失败)时**绝不写缓存** —— 旧实现会让 acc={}
            # 一路走到 sign="" → mystery=True, 把"查不到"记成"无简介"(误杀源)。
            # v0.3.1.28: 但 -404「啥都木有」是**确定的"这个账号不存在"**, 要单独记下来 ——
            # 多候选判定时它不可能是发送者, 不能当成"画像还没到"一直等下去。
            if _code in _BILI_NO_SUCH_MID:
                _am_mark_absent(m)
            return None
        sign = (acc.get("sign") or "").strip()
        # 等级: 2026-10-09 实测 acc/info **已不返回 level_info**, 真实等级在顶层 level;
        # 且 0 是合法等级(LV0 小号)—— 旧写法把"等级为 0"和"等级未知"混成同一个 None, 把 LV0 记成
        # "未知", 于是「等级低」规则对最该拦的 LV0 永远不生效。
        level = None
        _li = acc.get("level_info") or {}
        if isinstance(_li, dict) and _li.get("current_level") is not None:
            try:
                level = int(_li["current_level"])
            except Exception:
                level = None
        elif acc.get("level") is not None:
            try:
                level = int(acc["level"])
            except Exception:
                level = None
        mystery = (not sign) or (re.sub(r"\s+", "", sign) == MYSTERY_SIGN)
        _am_gate.acquire()          # 动态接口也过同一个闸门(旧实现这里完全不限速)
        items = _bili_recent_dynamics(m)
        if items is None:
            return None     # 动态接口失败 → 不写缓存(否则会把"不知道"记成"没有动态")
        lottery = any(_dyn_is_lottery(x) for x in items)
        nodyn = (len(items) == 0)   # 一条动态都没有 = 空首页(判定口径由用户 2026-10-08 定)
        c = {"sign": sign, "mystery": bool(mystery), "lottery": bool(lottery),
             "nodyn": bool(nodyn), "level": level, "ts": int(time.time())}
    except Exception:
        return None
    with _author_meta_lock:
        _author_meta[m] = c
    try:
        db().execute("INSERT INTO author_meta(mid,sign,mystery,lottery,nodyn,level,ts) "
                     "VALUES(?,?,?,?,?,?,?) "
                     "ON CONFLICT(mid) DO UPDATE SET sign=excluded.sign, mystery=excluded.mystery, "
                     "lottery=excluded.lottery, nodyn=excluded.nodyn, level=excluded.level, ts=excluded.ts",
                     (m, c["sign"], int(c["mystery"]), int(c["lottery"]), int(c["nodyn"]),
                      c["level"], c["ts"]))
        db().commit()
    except Exception:
        pass
    return c


# ── v0.3.1.27 一次性数据修复 —— v0.3.1.45dsk 已**停用** ─────────────────────
# 原来做的事: `level` 列后加, 老行 level 全为 NULL(用户库实测 567 行), 于是把
# `nodyn` 置 NULL 让 `_am_row_get` 视为过期、按需重取, 好把真实等级补上。
# 为什么删掉: 弹幕侧等级筛选已移除(见 _am_bad), `level` 列不再被任何规则使用 ——
# 而这个迁移会让**几百行画像同时过期**, 页面一打开就触发成百上千次 B站请求,
# 正是把账号打进 -352 风控的推手之一。现在它只花钱不办事, 所以整块拿掉。
# `level` 列本身保留(不再写入/读取), 免得为删列做一次表重建。


def _am_warm_worker():
    """画像池 worker: 取一个 mid → 过令牌桶 → 取画像。
    队列空就退出(下次入队 _pool_fill 再补线程) —— 不再空转占资源。
    v0.3.1.30: 旧实现是**单线程 + 每账号 sleep 0.6s**(0.86 账号/秒); 现在并发
    由池给、速率由令牌桶给 —— 两者解耦, 才能既快又不失控。
    ★ v0.3.1.45dsk: 失败**不再丢弃** —— 记进 _am_retry 由 _am_retry_ticker 按退避重排;
    撞风控(_am_paused)期间原地等, 不 pop 队列(条目留在队里, 恢复后接着跑)。"""
    while True:
        if _am_paused():
            time.sleep(1.0)
            with _am_warm_lock:
                if not _am_warm_q:
                    return
            continue
        with _am_warm_lock:
            if not _am_warm_q:
                return
            m = _am_warm_q.pop(0)
        try:
            ok = bili_author_meta(m) is not None
        except Exception:
            ok = False
        if ok:
            with _am_warm_lock:
                _am_retry.pop(m, None)
                _am_tries.pop(m, None)
            continue
        # 失败: 放回 seen 之外, 记一次尝试; 未超上限的交由 ticker 退避重排
        with _am_warm_lock:
            _am_warm_seen.discard(m)
            if len(_am_tries) > 20000:
                _am_tries.clear()
                _am_retry.clear()       # 防长期驻留膨胀
            n = _am_tries.get(m, 0) + 1
            _am_tries[m] = n
            if n <= _AM_RETRY_MAX:
                _am_retry[m] = time.time() + _AM_RETRY_DELAY * n
            else:
                _am_retry.pop(m, None)  # 超上限 → 不再排队(等下次页面重新问)


def _am_retry_ticker():
    """退避重试调度(常驻 daemon, 3 秒一轮): 把到期的失败 mid 放回队列并唤醒线程池。

    ★ 为什么必须有: 旧实现失败即丢弃(只从 seen 里删掉), 队列一空 worker 就退出 ——
      实测 73 个候选 7.5 秒排空、只落库 26 个, 剩下 47 个**再也不会被重试**,
      端到端 60 秒只筛出 2 条。撞风控时 100% 的请求都会失败, 于是"永远筛不出来"。
    """
    while True:
        time.sleep(_AM_RETRY_TICK)
        try:
            if _am_paused():
                continue
            now = time.time()
            with _am_warm_lock:
                due = [m for m, ts in _am_retry.items() if ts <= now]
            if not due:
                continue
            added = False
            for m in due:
                if _am_is_absent(m):
                    with _am_warm_lock:
                        _am_retry.pop(m, None)
                        _am_tries.pop(m, None)
                    continue
                with _am_warm_lock:
                    if m in _am_warm_seen:
                        continue
                    if len(_am_warm_q) >= _AUTHOR_WARM_MAX:
                        _am_retry[m] = now + _AM_RETRY_DELAY   # 队满, 下轮再来
                        continue
                    del _am_retry[m]
                    _am_warm_seen.add(m)
                    _am_warm_q.append(m)
                    added = True
            if added:
                conc = _am_apply_gear()
                with _am_warm_lock:
                    _pool_fill(_am_pool, conc, _am_warm_worker)
        except Exception:
            pass


_am_ticker_on = [False]


def _am_enqueue(mids):
    """把未知 mid 塞进后台补图队列(去重 / 有上限 / 不阻塞调用方)。
    v0.3.1.30: 改为按档位补足线程池(旧实现只保证 1 个线程)。"""
    add = []
    conc = _am_apply_gear()      # 先读档位(可能查库) —— 在锁外, 不占锁
    with _am_warm_lock:
        for m in mids:
            m = str(m or "")
            if len(add) >= _AUTHOR_WARM_CAP:
                break
            if not m.isdigit() or int(m) <= 0 or m in _am_warm_seen:
                continue
            if m in _am_retry:
                continue        # 已在退避队列里等着, 不打断它的退避节奏
            if _am_is_absent(m):
                continue        # 已确认不存在 → 别再白跑接口
            if len(_am_warm_q) >= _AUTHOR_WARM_MAX:
                break
            _am_warm_seen.add(m)
            _am_warm_q.append(m)
            add.append(m)
        if add:
            _pool_fill(_am_pool, conc, _am_warm_worker)
    if add and not _am_ticker_on[0]:
        _am_ticker_on[0] = True     # 退避重试调度线程: 只在真有补图需求时才起
        try:
            threading.Thread(target=_am_retry_ticker, daemon=True,
                             name="am-retry-ticker").start()
        except Exception:
            _am_ticker_on[0] = False
    return add


def _am_rows_get(ms, fast=True):
    """v0.3.1.31: 批量版 _am_row_get —— 内存优先, 剩余一次 IN 查询取回。
    旧实现每个 mid 一条 SELECT(200 个作者 = 200 次往返, 实测 6.1ms);
    批量后固定 1~2 条语句。语义与 _am_row_get 逐条版完全一致:
    TTL 过期当未知; nodyn 为 NULL 的老记录当未知(触发重取); 读失败一律当未知(放行)。"""
    now = time.time()
    out, miss = {}, []
    with _author_meta_lock:
        for m in ms:
            c = _author_meta.get(m)
            if c and now - c["ts"] < _AUTHOR_META_TTL:
                out[m] = c
            else:
                miss.append(m)
    if not miss:
        return out
    try:
        conn = _am_conn() if fast else db()
    except Exception:
        return out
    for i in range(0, len(miss), _SQL_CHUNK):
        part = miss[i:i + _SQL_CHUNK]
        ph = ",".join("?" for _ in part)
        try:
            rows = conn.execute("SELECT mid,sign,mystery,lottery,nodyn,level,ts FROM author_meta "
                                "WHERE mid IN (" + ph + ")", part).fetchall()
        except Exception:
            continue
        for row in rows:
            if now - int(row[6] or 0) >= _AUTHOR_META_TTL:
                continue
            if row[4] is None:      # 老记录(没有 nodyn 列 / 当时取动态失败) → 未知, 触发重取
                continue
            c = {"sign": row[1] or "", "mystery": bool(row[2]), "lottery": bool(row[3]),
                 "nodyn": bool(row[4]), "level": (int(row[5]) if row[5] is not None else None),
                 "ts": int(row[6])}
            out[row[0]] = c
            with _author_meta_lock:
                _author_meta[row[0]] = c
    return out


def bili_author_flags(mids, want_bio=True, want_lottery=True, want_empty=True):
    """评论区(页面脚本)调用: **只读缓存**, 立刻返回, 绝不联网等待。
    返回 {"flags": {"mid": {"mystery","lottery","nodyn"}}, "pending":[未知 mid],
          "cookie": 是否已登录 B站(没登录时页面端就不必再问了)}。
    走 _am_rows_get(fast=True): 短 busy_timeout, DB 被写锁占住时立刻返回未知(放行)。
    v0.3.1.31: 批量 IN, 不再逐 mid 一条 SQL。"""
    flags, pending, unknown = {}, [], []
    if not (want_bio or want_lottery or want_empty):
        # 同 bili_dm_flags: 规则全关就别把作者塞进联网补图队列(见那边的注释)。
        return {"flags": flags, "pending": pending, "cookie": bool(account_cookie())}
    valid, seen = [], set()
    for m in (mids or []):
        m = str(m or "")
        if not m.isdigit() or int(m) <= 0 or m in seen:
            continue
        seen.add(m)
        valid.append(m)
    cache = _am_rows_get(valid, fast=True) if valid else {}
    for m in valid:
        c = cache.get(m)
        if c is None:
            unknown.append(m)
            pending.append(m)
            continue
        if ((want_bio and c.get("mystery")) or (want_lottery and c.get("lottery"))
                or (want_empty and c.get("nodyn"))):
            flags[m] = {"mystery": bool(c.get("mystery")), "lottery": bool(c.get("lottery")),
                        "nodyn": bool(c.get("nodyn"))}
    cookie = bool(account_cookie())
    if cookie and unknown:
        _am_enqueue(unknown)
    return {"flags": flags, "pending": pending, "cookie": cookie}


# ───────────────────────── 弹幕筛选(v0.3.1.22) ─────────────────────────
# 弹幕接口(x/v2/dm/web/seg.so)是 protobuf, 发送者只有 midHash = crc32(str(mid))
# 的 8 位十六进制 —— 没有昵称/等级/简介。CRC32 对"纯数字串"可以 meet-in-the-middle
# 本地逆推(≤9 位基本唯一, 位数越多碰撞候选越多), 于是弹幕侧数据链:
#   · 黑名单: 已知 mid(B站拉黑名单接口自带 / 评论区见过的 name→mid 索引 name_mid)
#     → 正算 hash, 注入页面按 hash 集合就地摘弹幕 —— 精确、零延迟;
#   · 抽奖/空首页/无简介/等级: midHash 由后台线程逆推出 mid(存 dm_sender, 跨视频复用)
#     → 复用 author_meta 画像。候选不唯一时一律放行 —— 宁可漏杀, 绝不误杀。

def _crc_m1(x):
    """CRC32 消费一个 0x00 字节的线性转移(对初态线性)。"""
    return (x >> 8) ^ _CRC_T[x & 0xFF]


def _crc_apply(x, n):
    for _ in range(n):
        x = _crc_m1(x)
    return x


_CRC_T = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (0xEDB88320 if _c & 1 else 0)
    _CRC_T.append(_c)


def crc32_mid(mid):
    """B站弹幕 midHash 正算: crc32(十进制mid字符串) → 8位十六进制。"""
    return "%08x" % (zlib.crc32(str(mid).encode("ascii")) & 0xFFFFFFFF)


_crc_suf_cache = {}     # b -> {reg0(s): s}(与目标 hash 无关, 可跨查询复用)


def _crc_suffix_dict(b):
    d = _crc_suf_cache.get(b)
    if d is not None:
        return d
    d = {}
    stack = [(b"", 0)]
    while stack:
        pre, st = stack.pop()
        if len(pre) == b:
            d[st] = pre.decode()
            continue
        for c in range(10):
            stack.append((pre + bytes([48 + c]),
                          (st >> 8) ^ _CRC_T[(st ^ (48 + c)) & 0xFF]))
    if len(_crc_suf_cache) > 6:
        _crc_suf_cache.clear()
    _crc_suf_cache[b] = d
    return d


def crc32_reverse(target_hex, max_len=11, budget_s=8.0):
    """逆推: 返回长度 2..max_len 内**所有**满足 crc32==target 的候选 mid 串。
    32 位校验值, 候选数随长度指数涨(≈10^L/2^32) —— 调用方必须消歧, 多候选别猜。

    ⚠️ v0.3.1.25 起**已无调用点**, 一律改用 crc32_reverse_fast(前缀预表, 2..10 位完整, ~20ms)。
    不要再在点击/批量路径上用它: 它按 budget_s 截断, 算不完会返回**残缺候选集** ——
    被落库当负缓存后就永远查不出真身了(这正是 v0.3.1.24 弹幕跳转大面积失败的真因)。
    保留仅为对照/离线研究。"""
    try:
        target = int(target_hex, 16) & 0xFFFFFFFF
    except Exception:
        return []
    t0 = time.time()
    out = []
    for L in range(2, max_len + 1):
        if time.time() - t0 > budget_s:
            break
        b = min(5, L - 1)
        a = L - b
        K = target ^ (_crc_apply(0xFFFFFFFF, L) ^ 0xFFFFFFFF)
        suf = _crc_suffix_dict(b)
        stack = [(b"", 0)]
        while stack:
            pre, st = stack.pop()
            if len(pre) == a:
                s = suf.get(K ^ _crc_apply(st, b))
                if s is not None:
                    out.append(pre.decode() + s)
                continue
            first = 1 if not pre else 0
            for c in range(first, 10):
                stack.append((pre + bytes([48 + c]),
                              (st >> 8) ^ _CRC_T[(st ^ (48 + c)) & 0xFF]))
        if time.time() - t0 > budget_s:
            break
    return out


# ---- 快速批量逆推(v0.3.1.24) ----
# 痛点: crc32_reverse 每个 hash 都现场 DFS 枚举前缀, max_len=11 时光 L=11 一轮就要
# 走 90 万个栈节点(~2s 纯 Python CPU), 队列 800 个 = 半小时满载, GIL 把壳的窗口消息泵
# 和 HTTP 一起拖死(用户反馈「特别特别卡」的真因)。
# 改法: 前缀段的状态与目标 hash 无关 → 按(前缀位数a, 后缀位数b)预算一次、跨查询复用;
# 每 hash 剩下的只是 9 万次异或+字典查表(~20ms)。后台批量逆推封顶 10 位:
# 10 位以上的新 UID 逆不动 → 空结果落库当负缓存, 绝不让它拖死进程。
_crc_pre_cache = {}     # (a, b) -> [(st, st_after_b_zeros, prefix_str)]


def _crc_pre_states(a, b):
    d = _crc_pre_cache.get((a, b))
    if d is not None:
        return d
    lst = []
    stack = [(b"", 0)]
    while stack:
        pre, st = stack.pop()
        if len(pre) == a:
            lst.append((st, pre.decode()))
            continue
        first = 1 if not pre else 0
        for c in range(first, 10):
            stack.append((pre + bytes([48 + c]),
                          (st >> 8) ^ _CRC_T[(st ^ (48 + c)) & 0xFF]))
    d = [(st, _crc_apply(st, b), s) for st, s in lst]
    if len(_crc_pre_cache) > 8:
        _crc_pre_cache.clear()
    _crc_pre_cache[(a, b)] = d
    return d


def crc32_reverse_fast(target_hex):
    """后台批量用: 语义与 crc32_reverse(max_len=10) 完全一致, 但前缀段走预表。
    每 hash ~20ms(9999/10000 的工作量在查表), 不再是每个 2s 的 CPU 风暴。"""
    try:
        target = int(target_hex, 16) & 0xFFFFFFFF
    except Exception:
        return []
    out = []
    for L in range(2, 11):
        b = min(5, L - 1)
        a = L - b
        K = target ^ (_crc_apply(0xFFFFFFFF, L) ^ 0xFFFFFFFF)
        suf = _crc_suffix_dict(b)
        if a <= 3:                      # 表太小不值得缓存, 现算只有 9/90/900 个节点
            stack = [(b"", 0)]
            while stack:
                pre, st = stack.pop()
                if len(pre) == a:
                    s = suf.get(K ^ _crc_apply(st, b))
                    if s is not None:
                        out.append(pre.decode() + s)
                    continue
                first = 1 if not pre else 0
                for c in range(first, 10):
                    stack.append((pre + bytes([48 + c]),
                                  (st >> 8) ^ _CRC_T[(st ^ (48 + c)) & 0xFF]))
        else:
            for st, stb, s in _crc_pre_states(a, b):
                r = suf.get(K ^ stb)
                if r is not None:
                    out.append(s + r)
    return out


_dmrev_q = []                # 待逆推 hash(有序去重)
_dmrev_seen = set()
_dmrev_lock = threading.Lock()
# v0.3.1.30: 单线程 _dmrev_thread 已换成 _dmrev_pool 线程池
_DMREV_MAX = 800             # 队列上限(防无界增长)
_DMREV_CAP = 120             # 单次请求最多认领
_DMREV_GAP = 0.25            # 两个 hash 之间的间隔(纯本地计算)


def _dm_sender_row(h, fast=False):
    try:
        conn = _am_conn() if fast else db()
        return conn.execute("SELECT mids,ncand,ts FROM dm_sender WHERE hash=?", (h,)).fetchone()
    except Exception:
        return None


def _dm_pick_mid(mids, fast=False):
    """候选消歧: 唯一候选直接用; 多候选时用本地已知 mid(name_mid/ablock.mid)撞唯一解。
    返回 (mid or None, ncand)。撞不出唯一解返回 None —— 绝不猜。"""
    if not mids:
        return None, 0
    if len(mids) == 1:
        return mids[0], 1
    try:
        conn = _am_conn() if fast else db()
        q = ",".join("?" * len(mids))
        known = {str(r[0]) for r in conn.execute(
            "SELECT mid FROM name_mid WHERE mid IN (%s)" % q, tuple(mids)) if r[0]}
        if sget("m_blocklist_sync") == "1":
            known |= {str(r[0]) for r in conn.execute(
                "SELECT mid FROM ablock WHERE mid IN (%s)" % q, tuple(mids)) if r[0]}
        hits = [m for m in mids if m in known]
        if len(hits) == 1:
            return hits[0], len(mids)
    except Exception:
        pass
    return None, len(mids)


def _dmrev_store(h, cands):
    uniq = sorted(set(cands))[:8]
    db().execute("INSERT OR REPLACE INTO dm_sender(hash,mids,ncand,ts) VALUES(?,?,?,?)",
                 (h, ",".join(uniq), len(set(cands)), int(time.time())))
    db().commit()
    return uniq


def _dmrev_worker():
    """逆推池 worker(纯本地 CPU, 不联网)。队列空就退出。
    v0.3.1.30: 旧实现**单线程 + 每 hash sleep 0.25s** → 3.4 个/秒;
    而逆推稳态只要 15.2ms(66 个/秒) —— 那 0.25s 是纯白等, 白白浪费 19 倍。
    闸门在这里控的是 **CPU 占空比**(逆推是纯 Python, GIL 下总 CPU = 速率 × 15.2ms),
    所以档位同时决定了会不会把界面拖卡。"""
    while True:
        with _dmrev_lock:
            if not _dmrev_q:
                return
            h = _dmrev_q.pop(0)
        _dmrev_gate.acquire()
        try:
            if _dm_sender_row(h) is None:
                uniq = _dmrev_store(h, crc32_reverse_fast(h))   # ~15ms/个, 封顶10位(负缓存落库)
                m, _nc = _dm_pick_mid(uniq)
                if m:
                    _am_enqueue([m])     # 画像(简介/动态/等级)走既有补图队列
        except Exception:
            pass


def _dmrev_enqueue(hashes):
    """v0.3.1.30: 改为按档位补足线程池(旧实现只保证 1 个线程)。"""
    add = []
    conc = _dmrev_apply_gear()
    with _dmrev_lock:
        if len(_dmrev_seen) > 20000:
            _dmrev_seen.clear()      # 防长期驻留膨胀; 被清的会借 pending 再次进队
        for h in hashes:
            if len(add) >= _DMREV_CAP:
                break
            if not h or h in _dmrev_seen:
                continue
            if len(_dmrev_q) >= _DMREV_MAX:
                break
            _dmrev_seen.add(h)
            _dmrev_q.append(h)
            add.append(h)
        if add:
            _pool_fill(_dmrev_pool, conc, _dmrev_worker)
    return add


def _am_bad(c, want_bio=False, want_lottery=False, want_empty=False):
    """一条账号画像是否命中「弹幕评论筛选」的账号级规则。

    v0.3.1.45dsk: **等级条件已删除** —— 弹幕接口不带等级, 判等级只能靠
    「哈希逆推 → 候选 mid → 联网查账号资料」, 一个 8 位哈希平均撞 6 个候选
    (实测最大 36), 一个播放页上千次 B站请求, 直接把账号打进 -352 风控;
    而风控期间这条规则 100% 失效(请求全被拒), 得不偿失。
    等级筛选只保留评论侧(bilibili-cfilter, 评论接口自带 level_info, 零请求)。
    """
    if not c:
        return False
    return bool((want_bio and c.get("mystery")) or (want_lottery and c.get("lottery"))
                or (want_empty and c.get("nodyn")))


def _dm_flag_of(c, multi):
    """回给页面端的标记。multi = 这个哈希对上了几个账号(>1 时页面端标注「N候选」)。"""
    return {"mystery": bool(c.get("mystery")), "lottery": bool(c.get("lottery")),
            "nodyn": bool(c.get("nodyn")),
            "multi": int(multi)}


def bili_dm_flags(hashes, want_bio=False, want_lottery=False, want_empty=False, multi=1):
    """弹幕版 bili_author_flags: 按 midHash 查(先逆推出 mid, 再查账号画像)。
    只读本地缓存, 绝不联网等待; 没逆过的 hash 进后台队列慢慢算。

    multi —— 一个 8 位哈希常常对上 2~8 个真实账号(B站只给 crc32, 32 位有损;
    用户库实测 361 个哈希里 329 个是多候选 = 91%), 这时无法唯一确定发送者:
      · 0 = 放行           —— 旧行为(宁可漏杀), 代价是这些规则对 91% 的弹幕形同虚设;
      · 1 = 全部候选都命中 —— 零误杀(不管真身是哪个候选, 他都该被拦), 默认;
      · 2 = 任一候选命中   —— 宁可误杀, 拦得最狠(候选一多几乎等于清空弹幕, 慎用)。
    多候选时**所有候选都会进后台补画像队列** —— 旧实现在这里直接 continue,
    候选永远没有画像, 规则永远等不到数据(死循环)。
    """
    flags, pending = {}, []
    if sget("m_dm_on") == "0":
        # ★ v0.3.1.46dsk: 「弹幕屏蔽总开关」。它管的是**按发送者身份**的弹幕规则
        #   (黑名单 / 抽奖 / 空首页 / 无简介) —— 关掉时这条链路整条停摆: 不逆推、
        #   不查画像、不入队。页面脚本自己也会 gating(bilibili-dmfilter 的 NEED_FLAGS),
        #   但正如下面那句"不能指望它", 这里必须再设一道 —— 逆推 300 个哈希 ≈ 3 秒
        #   纯 Python CPU, 多候选分支还会把上千个 mid 塞进**联网**补图队列(风控源头)。
        #   注意: 「快速文本分析」的弹幕通道走 /api/textfilter, 不经过这里, 不受本开关影响。
        return {"flags": flags, "pending": pending, "cookie": bool(account_cookie())}
    if not (want_bio or want_lottery or want_empty):
        # ★ v0.3.1.45dsk: 三个账号级规则全关 → 弹幕侧没有任何要判的东西。
        #   直接返回, **连哈希逆推都不做** —— 300 个哈希 ≈ 3 秒纯 Python CPU,
        #   而且多候选分支会 `_am_enqueue(unknown)` 把上千个候选 mid 塞进**联网**补图队列,
        #   那正是把账号打进 -352 风控的源头(页面脚本虽然会先自行 gating, 但不能指望它)。
        return {"flags": flags, "pending": pending, "cookie": bool(account_cookie())}
    for h in (hashes or [])[:300]:
        h = str(h or "").lower()
        if not re.fullmatch(r"[0-9a-f]{8}", h):
            continue
        row = _dm_sender_row(h, fast=True)
        if row is None:
            pending.append(h)
            _dmrev_enqueue([h])
            continue
        mids = [m for m in str(row[0] or "").split(",") if m]
        m, _nc = _dm_pick_mid(mids, fast=True)
        if m:
            # 唯一解: 直接按画像判
            c = _am_row_get(m, fast=True)
            if c is None:
                _am_enqueue([m])
                continue
            if _am_bad(c, want_bio, want_lottery, want_empty):
                flags[h] = _dm_flag_of(c, 1)
            continue
        if not mids or multi == 0:
            continue          # 逆不出来(≥11 位新 UID) / 用户选了放行
        # ── 多候选: 逐个候选查画像 ──
        # 候选分三类: 有画像 / 确认不存在(-404, 不可能是发送者) / 还不知道。
        # 只有"还不知道"才值得等 —— 把"不存在"也当成"等不到画像"是「全命中才拦」
        # 永不生效的真因: 实测哈希 e6f1d278 的 6 个候选里有 4 个是 -404。
        prof, unknown = [], []
        for x in mids:
            c = _am_row_get(x, fast=True)
            if c is not None:
                prof.append(c)
            elif not _am_is_absent(x, fast=True):
                unknown.append(x)
        if unknown:
            _am_enqueue(unknown)   # ← 关键修复: 多候选也要补画像, 否则规则永远不生效
        bad = [c for c in prof if _am_bad(c, want_bio, want_lottery, want_empty)]
        if bad:
            # 展示用原因与页面端 blkWhy 的优先级保持一致(无简介 > 抽奖 > 空首页)
            bad.sort(key=lambda c: (not (want_bio and c.get("mystery")),
                                    not (want_lottery and c.get("lottery")),
                                    not (want_empty and c.get("nodyn"))))
        if multi == 2:
            hit = bad                     # 任一命中即拦
        elif prof and not unknown:
            # 候选全部查清了(有画像的 + 确认不存在的), 且每个有画像的都命中
            # → 真身无论是谁都该拦。零误杀。
            hit = bad if len(bad) == len(prof) else []
        else:
            hit = []                      # 还有候选没查清 → 断不了"全部命中"
        if hit:
            flags[h] = _dm_flag_of(hit[0], len(mids))
    return {"flags": flags, "pending": pending, "cookie": bool(account_cookie())}


_card_ok = {}                  # mid -> {"mid","name","face"} / None(确认不存在) / "?"(查不通)
_card_ok_lock = threading.Lock()


def _card_info(mid, timeout=5):
    """card 接口一次拿全: 账号是否存在 + 昵称 + 头像(未注册 → code -404「啥都木有」)。
    返回 {"mid","name","face"} / None(确认不存在) / "?"(查不通, 不参与消歧)。
    只在"点击弹幕找发送者"这条慢路径上用, 带内存缓存, 绝不进弹幕/评论加载路径。
    (v0.3.1.25: 原来只回布尔, 消歧后还得为每个候选再打一次 acc/info 拿昵称 —— 现在一次到位。)"""
    m = str(mid or "")
    with _card_ok_lock:
        v = _card_ok.get(m, 0)
    if v != 0:
        return v
    try:
        d = _bili_get_t("https://api.bilibili.com/x/web-interface/card?mid=%s&photo=false" % m,
                        timeout=timeout)
        if d.get("code") == 0:
            c = ((d.get("data") or {}).get("card") or {})
            v = {"mid": m, "name": (c.get("name") or "").strip(),
                 "face": (c.get("face") or "").replace("http:", "https:")}
        else:
            v = None             # -404 啥都木有 → 确认不存在
            if d.get("code") in _BILI_NO_SUCH_MID:
                _am_mark_absent(m)      # 点过主页的账号顺手记上, 多候选判定直接排除
    except Exception:
        return "?"               # 查不了 → 不拿它消歧, 也不误判
    with _card_ok_lock:
        if len(_card_ok) > 2000:
            _card_ok.clear()
        _card_ok[m] = v
    return v


def bili_dm_mid(hash_hex, budget_s=2.5):
    """弹幕列表点「发送者」时用: 逆推 midHash → 主页 mid(唯一解 / 多候选 / 查不到)。
    消歧顺序: 本地已知 mid(name_mid/ablock) → card 接口验真(≤8 个候选并发查, 每个 4s 超时,
    一次拿全 存在性+昵称+头像)。结果落 dm_sender 供全局复用。

    返回 {"ok":True, "mid":唯一解或None, "ncand":候选总数,
          "cands":[{"mid","name","face"},...], "why":""}
      · mid 非空            → 唯一解, 前端直接跳主页;
      · mid 空 + cands 多个 → hash 撞了(几个账号都能对上), 前端弹候选框让用户自己挑;
      · mid 空 + cands 空   → 真身是 ≥11 位新 UID(或接口查不通), why 说明原因。

    v0.3.1.25 修: 旧实现走带时间预算的 crc32_reverse, 算不完就把**残缺候选**当负缓存
    落库, 之后永远查不出真身; 且"恰好 1 个存活"才算成功 → 多候选一律失败。现在改成
    完整逆推(2..10 位, ~20ms)并与库里旧结果取并集覆盖落库, 多候选照实返回。
    (budget_s 仅为兼容旧调用签名保留, 已不参与计算。)"""
    h = str(hash_hex or "").lower().strip()
    if not re.fullmatch(r"[0-9a-f]{8}", h):
        return {"ok": False, "mid": None, "ncand": 0, "cands": [], "why": "bad"}
    row = _dm_sender_row(h)
    mids = [m for m in str(row[0] or "").split(",") if m] if row else []
    try:
        fresh = crc32_reverse_fast(h)      # 2..10 位完整候选, 不吃时间预算
    except Exception:
        fresh = []
    if fresh:
        merged = sorted(set(mids) | set(fresh))
        if merged != mids:
            try:
                _dmrev_store(h, merged)    # 覆盖式落库: 顺手把历史残缺候选修回来
            except Exception:
                pass
        mids = merged
    if not mids:
        _dmrev_enqueue([h])                # 真身多半 ≥11 位 → 后台队列(负缓存标记)
        return {"ok": True, "mid": None, "ncand": 0, "cands": [], "why": "newuid"}
    m, ncand = _dm_pick_mid(mids)
    if m:
        _am_enqueue([m])
        return {"ok": True, "mid": m, "ncand": ncand, "cands": [], "why": ""}
    # 本地索引撞不出唯一解 → 用 card 接口验真(一次拿全 存在性+昵称+头像)。
    # 并发查(≤8 个, 每个 4s 超时): 串行最坏 8×5s ≈ 40s, 点击路径上不能这么晾着用户。
    cands = []
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [(ex.submit(_card_info, c, 4), c) for c in mids[:8]]
            for f, c in futs:
                try:
                    v = f.result(timeout=6)
                except Exception:
                    v = "?"
                if v and v != "?":
                    cands.append(v)
    except Exception:
        cands = []
    cands.sort(key=lambda d: (len(d["mid"]), d["mid"]))
    if len(cands) == 1:
        _am_enqueue([cands[0]["mid"]])
        return {"ok": True, "mid": cands[0]["mid"], "ncand": ncand, "cands": [], "why": ""}
    if not cands:
        _dmrev_enqueue([h])                # ≤10 位全不存在 → 真身多半是 11 位以上
        return {"ok": True, "mid": None, "ncand": ncand, "cands": [], "why": "newuid"}
    return {"ok": True, "mid": None, "ncand": ncand, "cands": cands, "why": "multi"}


def dm_blocked_hashes():
    """注入用: 该拦的 midHash 集合 = B站拉黑名单的 mid ∪ 黑名单作者(经 name→mid 索引)。
    注入时一次正算好, 页面端 O(1) 查表 —— 不做逐条扫描。"""
    mids = set()
    try:
        if sget("m_blocklist_sync") == "1":
            mids |= {str(r[0]) for r in db().execute(
                "SELECT mid FROM ablock WHERE mid IS NOT NULL") if r[0]}
        names = [r[0] for r in db().execute(
            "SELECT value FROM blacklist WHERE kind='author'") if r[0]]
        if names:
            q = ",".join("?" * len(names))
            mids |= {str(r[0]) for r in db().execute(
                "SELECT mid FROM name_mid WHERE name IN (%s)" % q, tuple(names)) if r[0]}
    except Exception:
        pass
    return sorted({crc32_mid(m) for m in mids if str(m).isdigit()})


def dm_names_save(pairs):
    """页面端评论区见过的 (昵称, mid) 对 → name_mid 索引(黑名单作者因此能管到弹幕)。"""
    n = 0
    try:
        with _db_lock:
            for name, mid in (pairs or [])[:200]:
                name = str(name or "").strip()
                mid = str(mid or "").strip()
                if not name or not mid.isdigit():
                    continue
                db().execute("INSERT OR REPLACE INTO name_mid(name, mid, ts) VALUES(?,?,?)",
                             (name, mid, int(time.time())))
                n += 1
            db().commit()
    except Exception:
        pass
    return n


def machine_filter(items):
    """纯程序规则过滤(不耗AI): 时间范围/最低播放/最大时长/相似标题去重/账号拉黑名单。
    贴吧条目无播放/时长语义(views=0), 跳过播放量/时长规则, 仅保留时间/去重/黑名单;
    求助帖过滤(tb_allow_help=0): 剔除"求助/求教/请问"类提问帖。"""
    min_views = int(sget("m_min_views") or 0)
    max_views = int(sget("m_max_views") or 0)
    max_dur = int(sget("m_max_duration") or 0)
    min_dur = int(sget("m_min_duration") or 0)
    if sget("mode") == "严肃":
        max_age = int(sget("ser_max_age_months") or 0)   # 严肃模式独立时间窗(默认30月, 知识内容历史长)
    else:
        max_age = int(sget("m_max_age_months") or 0)
    pref_age = pref_age_months()   # 偏好内容的独立窗: 宽松档 +6 月, 严格/标准档与设定窗一致
    dedup = sget("m_dedup") == "1"
    emotion = sget("m_emotion") != "0"
    news_fresh = sget("m_news_fresh") != "0"
    sync_bl = sget("m_blocklist_sync") == "1"
    ablock_authors = {r[0] for r in db().execute("SELECT name FROM ablock")} if sync_bl else set()
    dkws = dislike_keywords()
    allow_help = sget("tb_allow_help") != "0"
    xhcomm = [k.strip() for k in (sget("xhh_comm") or "").split(",") if k.strip()]
    xhtop = [k.strip() for k in (sget("xhh_topic") or "").split(",") if k.strip()]
    n_before_fb = len(items)
    items = negative_feedback_filter(items)   # 已不喜欢/黑名单/临时屏蔽: 入池前硬排除
    if len(items) != n_before_fb:
        _funnel_bump("dislike", n_before_fb - len(items))
    if sget("mode") == "严肃":
        items = sorted(items, key=lambda x: (-serious_relevance(x),
                                              -(x.get("pubdate") or 0), -x.get("views", 0)))
    else:
        items = sorted(items, key=lambda x: (0 if x.get("platform") != "B站" else 1, -x["views"]))   # 非B站先保留, B站再按播放量去重留优质
    now = time.time()
    fm = int(sget("bili_follow_months") or 1)   # 关注的人强调时间窗(0=不限)
    _funnel_bump("fetched", len(items))
    seen_titles, out = set(), []
    for it in items:
        # v0.3.1.51 直播条目: 时间/播放量/时长窗天然不适用(pubdate=抓取时刻, views=人气, 无时长),
        # 情绪/广告/fastText 也放宽 —— B站官方直播标题多为「【直播】…」式, 套视频那套会误杀。
        # 拉黑/厌恶词/标题去重继续生效。
        is_live = (it.get("platform") == "B站直播")
        if it.get("following") and not is_live:
            if not it["pubdate"]:
                _funnel_bump("follow_window")
                continue   # 关注视频无发布时间 → 直接丢弃
            if fm and now - it["pubdate"] > fm * 30.44 * 86400:
                _funnel_bump("follow_window")
                continue   # 超出强调月数 → 直接丢弃(单独机制, 不符合规则不进池)
        if not it.get("following") and not is_live:
            # 偏好词直搜结果不再豁免时间窗, 只按偏好专属窗判定(宽松档多给6个月, 其余与设定一致);
            # 关注的人走 bili_follow_months 单独机制(上面已判)。
            win = pref_age if it.get("pref_kw") else max_age
            if win and it["pubdate"] and now - it["pubdate"] > win * 30.44 * 86400:
                _funnel_bump("pref_age" if it.get("pref_kw") else "age")
                continue   # 只保留最近 N 个月内发布的内容
        if news_fresh and not is_live and it.get("pubdate") and _is_newsy(it) and now - it["pubdate"] > 86400:
            _funnel_bump("news")
            continue   # 时效分层: 资讯/热点只推24小时内(干货/教程不受此限)
        if not it.get("following") and not is_live:
            views_known = it.get("views_known") is True or it.get("platform") == "B站"
            duration_known = it.get("duration_known") is True or it.get("platform") == "B站"
            if views_known:
                if min_views and it.get("views", 0) < min_views:
                    _funnel_bump("views")
                    continue
                if max_views and it.get("views", 0) > max_views:
                    _funnel_bump("views")
                    continue
            if duration_known:
                if max_dur and it.get("duration", 0) > max_dur:
                    _funnel_bump("duration")
                    continue
                if min_dur and it.get("duration", 0) < min_dur:
                    _funnel_bump("duration")
                    continue
        if it.get("platform") == "贴吧" and not allow_help:
            if is_help_post(it):
                _funnel_bump("help")
                continue   # 求助帖(用户关闭允许)
        if it.get("platform") == "小黑盒":   # 社区/话题筛选(allow-list, 命中任一保留)
            if xhcomm and not any(k in (it.get("xcomm") or "") for k in xhcomm):
                _funnel_bump("xhh")
                continue
            if xhtop and not any(k in (it.get("xtopic") or "") for k in xhtop):
                _funnel_bump("xhh")
                continue
        if it["author"] in ablock_authors:
            _funnel_bump("blocklist")
            continue
        if dkws and any(k in (it["title"] + " " + it["desc"] + " " + it["author"] +
                              " " + (it.get("tag") or "")).lower() for k in dkws):
            _funnel_bump("dislike")
            continue   # 厌恶备注关键词命中(含tag) → 直接剔除
        if emotion and not is_live:
            why = _emo_bad(it)
            if not why and _ad_bad(it):
                why = "广告"   # 广告识别查简介(硬标记/软标记≥2), 与情绪过滤共占漏斗
            if why:
                _funnel_bump("emotion")
                continue   # 情绪过滤器: 轰炸/引战/标题党(标题) + 广告引流(简介), 纯规则零token
        # v0.3.1.43dsk 快速文本分析: 规则层没抓到的语义垃圾(引战/低质/标题党/阴阳怪气)
        # 交给本地 fastText 兜一遍。词表命中的上面已经拦了, 这里只处理"词表覆盖不到"的。
        # 只在 m_emotion 之后跑 —— 省一次模型调用, 也让漏斗计数各归各的。
        if not is_live and ft_enabled() and sget("ft_video") == "1":
            _w = ft_scan([(it.get("title") or "") + " " + (it.get("desc") or "")], "video")[0]
            if _w:
                _funnel_bump("ft")
                continue
        if dedup:
            fp = _title_fp(it["title"])
            if fp in seen_titles:
                _funnel_bump("dedup")
                continue   # 规则②: 本次推送内同指纹标题只留最高热一条
            seen_titles.add(fp)
        out.append(it)
        _funnel_bump("kept")
    return out


# 贴吧求助帖判定词(标题/摘要命中即为求助/提问帖; 保守词表防误伤)
HELP_POST_KWS = ("求助", "求教", "求大佬", "求指导", "求推荐", "求解答", "求帮助",
                 "请教", "帮帮我", "在线等", "急!", "急！", "有懂", "有没有人",
                 "新手求助", "新人求助", "求解", "求助帖", "萌新", "小白求",
                 "求个", "求了", "谁能教", "谁能帮", "哪位大佬", "怎么解决",
                 "有没有懂", "帮忙看看", "求问",
                 # 咨询/求教意图(防"问前景/值得学/想问问"这类求助帖漏过)
                 "想问问", "想问", "求建议", "求指教", "求指点", "求经验", "求意见",
                 "有前景", "前景怎么样", "前景如何", "值得学", "值得入", "怎么入门",
                 "零基础", "新手小白", "刚想学")


# 贴吧求助/提问帖语义模式(标题+摘要): 覆盖 "求建议/求支招/有没有大佬/请帮忙/求解惑/想问问/怎么解决" 等
HELP_RE = re.compile(
    r"(求(?:教|大佬|大神|推荐|建议|解答|支招|指点|经验|帮助|方法|解决|问|指导|个|了|解惑)|"
    r"有没有(?:大佬|人|懂|谁)|谁知道|哪位(?:大佬|高人)|"
    r"想问问|想问下|想问|请问|请(?:帮忙|教|高人|大佬)|帮忙|帮帮我|帮我看看|"
    r"怎么(?:解决|弄)|如何(?:解决|弄)|怎么办|解惑)")


def is_help_post(it):
    """求助/提问帖判定:
    ① 帖子求助分类标签(文本/【求助】) → 直接判求助;
    ② 标题/摘要命中求助语义模式(求X/有没有大佬/想问问/请帮忙/怎么解决等) 或求助词表。"""
    tclass = (it.get("tclass") or "").lower()
    if any(k in tclass for k in ("求助", "求教", "提问", "咨询")):
        return True
    text = ((it.get("title") or "") + " " + (it.get("desc") or "")).lower()
    if HELP_RE.search(text):
        return True
    return any(k.lower() in text for k in HELP_POST_KWS)


def rules_filter(items, skip_age=False, apply_blocks=True, skip_windows=False):
    """机器规则过滤(保留原顺序, 用于显式搜索): 时间窗/播放量/时长/拉黑名单。
    skip_age=True 时跳过时间窗(主动搜索想找特定主题, 老内容也应出现)。
    skip_windows=True 时连同播放量/时长窗一起跳过 —— 只给**显式搜索**用:
    播放量/时长窗是"信息流推荐"的质量闸(m_min_views 之类), 套到主动搜索上会把冷门但
    切题的B站视频全挡掉, 而贴吧/小黑盒没有播放量字段天然免疫 → 表现就是
    "搜索里经常没有B站视频"(用户报过)。要筛播放量就不该用搜索找。
    apply_blocks=False 时跳过屏蔽类机制(负反馈/账号拉黑/厌恶词): 搜索默认不受用户屏蔽影响,
    搜索词前缀 * 表示"保留屏蔽"(见 search_all)。求助帖规则不受这两个开关影响。"""
    if apply_blocks:
        items = negative_feedback_filter(items)
    min_views = int(sget("m_min_views") or 0)
    max_views = int(sget("m_max_views") or 0)
    max_dur = int(sget("m_max_duration") or 0)
    min_dur = int(sget("m_min_duration") or 0)
    if sget("mode") == "严肃":
        max_age = int(sget("ser_max_age_months") or 0)   # 严肃模式独立时间窗(默认30月)
    else:
        max_age = int(sget("m_max_age_months") or 0)
    sync_bl = sget("m_blocklist_sync") == "1"
    ablock_authors = ({r[0] for r in db().execute("SELECT name FROM ablock")}
                      if (sync_bl and apply_blocks) else set())
    dkws = dislike_keywords() if apply_blocks else []
    allow_help = sget("tb_allow_help") != "0"
    now = time.time()
    fm = int(sget("bili_follow_months") or 1)   # 关注的人强调时间窗(0=不限)
    out = []
    for it in items:
        if it.get("following"):
            if not it["pubdate"]:
                continue   # 关注视频无发布时间 → 直接丢弃
            if fm and now - it["pubdate"] > fm * 30.44 * 86400:
                continue   # 超出强调月数 → 直接丢弃(单独机制, 不符合规则不进池)
        if not skip_age and max_age and not it.get("following") and it["pubdate"] and now - it["pubdate"] > max_age * 30.44 * 86400:
            continue
        # skip_windows: 显式搜索不套播放量/时长质量窗(见 docstring; 否则B站条目被全挡掉)
        if not it.get("following") and not skip_windows:
            views_known = it.get("views_known") is True or it.get("platform") == "B站"
            duration_known = it.get("duration_known") is True or it.get("platform") == "B站"
            if views_known:
                if min_views and it.get("views", 0) < min_views:
                    continue
                if max_views and it.get("views", 0) > max_views:
                    continue
            if duration_known:
                if max_dur and it.get("duration", 0) > max_dur:
                    continue
                if min_dur and it.get("duration", 0) < min_dur:
                    continue
        if it.get("platform") == "贴吧" and not allow_help:
            if is_help_post(it):
                continue   # 求助帖(用户关闭允许) — 搜索路径也生效
        if it["author"] in ablock_authors:
            continue
        if dkws and any(k in (it["title"] + " " + it["desc"] + " " + it["author"] +
                              " " + (it.get("tag") or "")).lower() for k in dkws):
            continue   # 厌恶备注关键词命中(含tag) → 直接剔除
        out.append(it)
    return out


_search_deep_cache = {}   # 近义词缓存(进程内, 1h): q -> {"terms": [...], "ts": ts}
_search_wave = {}         # 搜索波次缓存: q -> {"items","ts","used_terms","deep_terms","waves","end","building"}
_search_wave_lock = threading.Lock()
SEARCH_WAVE_TTL = 3600    # 搜索波次缓存1h(与近义词缓存同频)
SEARCH_MAX_WAVES = 3      # 近义词扩波上限(首波+3波, 防B站/贴吧风控+LLM无限绕圈)


def search_expand_terms(q, exclude=None):
    """为搜索主题生成近义/相关搜索词(LLM)。无AI时返回空(不做深挖)。
    exclude: 已搜过的词(扩波时排除防原地打转); 带exclude的调用不缓存(每次要新词)。"""
    q = (q or "").strip()
    if not q or not ai_on():
        return []
    key = "__terms__" + q
    if not exclude:
        c = _search_deep_cache.get(key)
        if c and time.time() - c["ts"] < 3600:
            return c["terms"]
    terms = []
    try:
        sys_p = ("给定一个内容搜索主题(视频/帖子/软件/游戏/教程/话题等), 输出5-8个"
                 "近义词/相关搜索词(用于在视频站和贴吧扩搜更多内容), "
                 "只输出JSON数组字符串, 例如[\"A\",\"B\",\"C\"]")
        if exclude:
            sys_p += "。以下词已搜过, 不要再输出: " + "、".join(list(exclude)[-18:])
        content, _ = call_llm([
            {"role": "system", "content": sys_p},
            {"role": "user", "content": q[:100]}], max_tokens=1500)
        arr = json.loads(content)
        if isinstance(arr, list):
            terms = [str(t).strip() for t in arr
                     if str(t).strip() and str(t).strip() != q][:8]
    except Exception:
        terms = []
    if exclude:
        terms = [t for t in terms if t not in exclude]
    else:
        _search_deep_cache[key] = {"terms": terms, "ts": time.time()}
    return terms


def _interleave3(a, b):
    """a每3条插1条b(搜索结果的跨平台交错, 保证贴吧/小黑盒在首屏可见)。"""
    out, i, j = [], 0, 0
    while i < len(a) or j < len(b):
        for _ in range(3):
            if i < len(a):
                out.append(a[i]); i += 1
        if j < len(b):
            out.append(b[j]); j += 1
    return out


def _local_pool_hits(terms, seen, limit=100, platforms=("贴吧", "小黑盒")):
    """本地沉淀池分词命中: 任一token命中即算, 命中数多者优先/标题命中加权。
    旧版要求整句连续子串命中 → 多词/短词查询几乎必空(跨平台搜索形同虚设)。
    platforms: 参与兜底的平台。默认只兜贴吧/小黑盒(它们没有稳定可用的直搜接口);
    传 ("B站",) 时给B站直搜失败的情况兜底 —— 见 _search_first_wave。"""
    toks = [t for t in {str(x).strip().lower() for x in (terms or [])} if len(t) >= 2][:12]
    plats = [p for p in (platforms or ()) if p]
    if not toks or not plats:
        return []
    try:
        where = " OR ".join(["json LIKE ?"] * len(plats))
        rows = db().execute(
            "SELECT json FROM items WHERE (" + where + ") "
            "ORDER BY fetched_at DESC LIMIT 3000",
            ['%"platform": "' + p + '"%' for p in plats]).fetchall()
    except Exception:
        return []
    scored = []
    for (js,) in rows:
        try:
            it = json.loads(js)
        except Exception:
            continue
        if not isinstance(it, dict) or not it.get("key") or it["key"] in seen:
            continue
        if it.get("platform") == "贴吧" and not tieba_bar_allowed(it):
            continue
        text = ((it.get("title") or "") + " " + (it.get("desc") or "") + " " +
                (it.get("author") or "") + " " + (it.get("tag") or "") + " " +
                (it.get("tname") or "") + " " + (it.get("xcomm") or "") + " " +
                (it.get("xtopic") or "") + " " + (it.get("src") or "")).lower()
        hits = [t for t in toks if t in text]
        if not hits:
            continue
        score = len(hits)
        if any(t in (it.get("title") or "").lower() for t in hits):
            score += 2
        for f in ("views", "likes", "reply", "pubdate", "duration"):
            it.setdefault(f, 0)
        it.setdefault("danmaku", None)
        scored.append((score, it))
        seen.add(it["key"])
    scored.sort(key=lambda p: -p[0])
    return [it for _, it in scored[:limit]]


# ---------------- 显式搜索: 相关度排序 + BV 号直达 (v0.4.2) ----------------
# 旧版第一波完全按 B站搜索接口的翻页顺序返回, 再把贴吧/本地 3:1 插进来; 搜原标题时,
# 真正的原视频可能在第 3 页, 用户却在首屏看到一堆标题里只沾了几个字的搬运/切片。
# 现在按"命中强度"稳定排序: 标题完全一致 > UP主名完全一致 > 标题前缀 > 标题包含 >
# 查询词覆盖, 同一档内热的靠前 —— 只调整顺序, 不增删条目, 也不改变跨平台来源。
_MATCH_NOISE = set("""视频 一个 什么 怎么 为什么 如何 这个 那个 我们 你们 他们
自己 就是 不是 可以 已经 没有 真的 还是 但是 因为 所以 如果 然后 最新 完整 官方
合集 教程 分享 推荐 高清 中文 字幕""".split())
_BVID_RE = re.compile(r"(?i)(?<![0-9a-z])bv([0-9a-z]{10})(?![0-9a-z])")


def _extract_bvid(q):
    """从查询里抠出 BV 号(支持整段 URL / 前后带说明文字); 没有则返回空串。"""
    m = _BVID_RE.search(q or "")
    return ("BV" + m.group(1)) if m else ""


def _match_norm(s):
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    return re.sub(r"[\s\W_]+", "", s)


def _match_terms(q):
    """查询词元: 拉丁词 + 中文二元组(整句去标点后再切, 防空格/全角符号隔断)。"""
    raw = unicodedata.normalize("NFKC", str(q or "")).lower()
    raw = re.sub(r"(?<=\d)[.\-_](?=\d)", "", raw)
    terms = [w for w in re.split(r"[\s\W_]+", raw) if len(w) >= 2]
    zh = re.sub(r"[^\u4e00-\u9fff]", "", _match_norm(q))
    if len(zh) == 1:
        terms.append(zh)
    else:
        terms += [zh[i:i + 2] for i in range(max(0, len(zh) - 1))]
    return list(dict.fromkeys(t for t in terms if t and t not in _MATCH_NOISE))


def _search_rel_score(q, it):
    """单条搜索结果的相关度分(纯排序用, 不参与过滤)。
    严格分档: 标题完全一致 > UP主名完全一致 > 标题前缀 > 标题包含 > UP主名包含。
    档位固定隔 1000, 档内加成(词元覆盖≤400 + 热度≤8)跨不过去 —— 旧版直接相加时,
    "标题前缀+词覆盖"能反超"UP主名完全一致", 搜 UP 主会被别人的仿制标题压过去。"""
    qn = _match_norm(q)
    if not qn:
        return 0.0
    tn = _match_norm(it.get("title") or "")
    an = _match_norm(it.get("author") or "")
    if tn == qn:
        tier = 5
    elif an == qn:
        tier = 4
    elif tn.startswith(qn):
        tier = 3
    elif qn in tn:
        tier = 2
    elif an and qn in an:
        tier = 1
    else:
        tier = 0
    score = tier * 1000.0
    terms = _match_terms(q)
    if terms:
        score += 280.0 * sum(1 for t in terms if t in tn) / len(terms)
        score += 80.0 * sum(1 for t in terms if t in an) / len(terms)
        extra = _match_norm((it.get("tag") or "") + " " + (it.get("desc") or "")[:120])
        score += 40.0 * sum(1 for t in terms if t in extra) / len(terms)
    if tier >= 2:   # 同一档内用热度决胜, 绝不越过档位
        score += min(8.0, math.log10(max(1, int(it.get("views") or 0))))
    return score


def _rerank_search(q, items):
    """稳定排序: 同分保持抓取顺序(跨平台交错结构不被破坏)。"""
    if not items:
        return items
    ranked = sorted(enumerate(items), key=lambda p: (-_search_rel_score(q, p[1]), p[0]))
    return [it for _, it in ranked]


def bili_video_by_bvid(bvid):
    """BV号直达: 用公开 view 接口取这一条(免登录); 失败返回 None。"""
    if not _net_guard("bili", 0.35, 110, wait=1.5):
        return None
    try:
        url = "https://api.bilibili.com/x/web-interface/view?bvid=" + urllib.parse.quote(bvid)
        req = urllib.request.Request(
            url, headers=dict(HEADERS, Cookie=account_cookie()) if account_cookie() else HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        if d.get("code") != 0:
            try:
                code = int(d.get("code"))
            except Exception:
                code = 0
            if code in (-352, -412, -403, -509, 412):
                _net_penalty("bili", 900, 2.0)
            return None
        data = d.get("data") or {}
        if not data.get("bvid"):
            return None
        it = normalize(data)
        it["gid"] = "search"
        it["src"] = "BV直达"
        return it
    except Exception:
        return None


def _search_bvid(q, bvid, offset, limit, apply_blocks):
    """BV 查询只返回这一条: 不做近义词扩波(用户要的就是"这个视频")。"""
    def _empty():
        return {"ok": True, "q": q, "items": [], "offset": offset, "total": 0,
                "next_offset": offset, "end": True, "has_more": False,
                "deep_terms": [], "building": False, "platform_hits": 0}
    if offset > 0:
        return _empty()
    it = bili_video_by_bvid(bvid)
    if not it:
        return _empty()
    found = rules_filter([it], skip_age=True, apply_blocks=apply_blocks, skip_windows=True)
    if found:
        _annotate_following(found)
    chunk = found[:max(1, int(limit))]
    if chunk:
        now = time.time()
        try:
            with _db_lock:
                for x in chunk:
                    db().execute("INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,?,0) "
                                 "ON CONFLICT(key) DO UPDATE SET json=excluded.json,fetched_at=excluded.fetched_at",
                                 (x["key"], json.dumps(x, ensure_ascii=False), now))
                db().commit()
        except Exception:
            pass
        _request_bg_score(chunk)
    return {"ok": True, "q": q, "items": chunk, "offset": offset + len(chunk),
            "next_offset": offset + len(chunk), "total": len(found), "end": True,
            "has_more": False, "deep_terms": [], "building": False,
            "platform_hits": len(chunk)}


def _search_first_wave(q, apply_blocks=True):
    """搜索第一波: B站WBI直搜(≤5页, 空页即止) + 贴吧全站实时搜索(与B站翻页并行)
    + 本地沉淀池分词命中 → 跨平台3:1交错 → 机器规则过滤。"""
    seen, bili = set(), []
    tb_x = _FX.submit(tb_search_items, q)
    for pg in range(1, 6):
        page = search_items(q, pg)
        for x in page:
            bv = (x.get("url") or "").rsplit("/", 1)[-1]
            if bv and bv not in seen:
                seen.add(bv)
                bili.append(x)
        if not page:
            break   # 空页=直搜见底, 不再空翻
    if not bili:
        # B站直搜一条都没回来(风控 -412 / WBI 签名失效 / 断网 / 这词确实没视频):
        # 用本地沉淀池已经收过的 B站 条目按词兜底。没有这一步, 搜索里就会一条B站视频都没有 ——
        # 这正是用户报的"经常没有B站视频"。
        bili = _local_pool_hits([q], seen, limit=60, platforms=("B站",))
    tb = []
    try:
        for x in tb_x.result() or []:
            if x.get("key") and x["key"] not in seen:
                seen.add(x["key"])
                tb.append(x)
    except Exception:
        pass
    # 分平台各自按相关度排, 再 3:1 交错 —— 不能对交错后的整表再重排: 否则同档同覆盖时,
    # B站靠热度就能把贴吧/本地整段挤到 30 条之后, 搜索首屏又变回"只有B站"(跨平台混排回归)。
    bili = _rerank_search(q, bili)
    tb = _rerank_search(q, tb)
    local = _rerank_search(q, _local_pool_hits([q], seen))
    candidates = _interleave3(bili, tb + local)
    _annotate_following(candidates)
    return rules_filter(candidates, skip_age=True, apply_blocks=apply_blocks,
                        skip_windows=True)


def _search_expand_wave(q, used_terms, apply_blocks=True):
    """近义词深挖扩波: LLM生成新近义词(排除已用) → 每词并行搜B站1页+贴吧10条
    + 本地池分词命中 → 去重过滤。返回 (new_items, terms)。terms为空=深挖到头。"""
    terms = search_expand_terms(q, exclude=list(used_terms))
    if not terms:
        return [], []
    terms = terms[:6]

    def _one(t):
        out = []
        try:
            out.extend(search_items(t, 1))
        except Exception:
            pass
        try:
            out.extend(tb_search_items(t, rn=10))
        except Exception:
            pass
        return out

    try:
        per = _FX.map(_one, terms)
    except Exception:
        per = []
    seen, out = set(), []
    for lst in per:
        for x in lst:
            k = x.get("key") or (x.get("url") or "")
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(x)
    out.extend(_local_pool_hits(terms, seen))
    out = _rerank_search(q, out)
    _annotate_following(out)
    return rules_filter(out, skip_age=True, apply_blocks=apply_blocks,
                        skip_windows=True), terms


def _prune_search_waves():
    """波次缓存容量上限(保留最近12个查询, 防长驻进程内存膨胀)。调用方需持锁。"""
    if len(_search_wave) <= 12:
        return
    for k in sorted(_search_wave, key=lambda k: _search_wave[k]["ts"])[:len(_search_wave) - 12]:
        _search_wave.pop(k, None)


SEARCH_SORTS = ("default", "views", "pubdate", "hot")


def _hot_score(it, now):
    """最新热门重力分(HN式): 播放量/(小时龄+2)^1.5 — 新发布内容获得显著加成,
    老视频要靠播放量硬堆。贴吧/小黑盒的 views 是点击量, 与B站播放量共用一套分
    (量级差异在混合排序下可接受)。pubdate 缺失按0(极老)沉底。"""
    views = it.get("views") or 0
    age_h = max(0.0, (now - (it.get("pubdate") or 0)) / 3600.0)
    return views / (age_h + 2.0) ** 1.5


def _sort_search_pool(items, mode, now):
    """搜索结果排序: 返回快照列表, 不改动主池顺序(综合序仍是翻页稳定基准)。
    views=播放量降序; pubdate=最新发布在前(缺失沉底); hot=最新热门重力分降序。
    sorted 为稳定排序, 同分保持抓取时的相对顺序。"""
    if mode == "views":
        return sorted(items, key=lambda it: -(it.get("views") or 0))
    if mode == "pubdate":
        return sorted(items, key=lambda it: -(it.get("pubdate") or 0))
    if mode == "hot":
        return sorted(items, key=lambda it: -_hot_score(it, now))
    return list(items)


def search_all(q, offset=0, limit=30, refresh=False, sort="default"):
    """显式搜索(波次制+无限深挖):
    第1波 = B站直搜 + 贴吧实时搜索 + 本地池; 滚到底(offset+limit≥total)自动触发
    近义词深挖扩波(LLM生成新词, 每词B站+贴吧+本地池), 直到无新词/无新内容/达波次上限。
    波次缓存1h: 翻页不再重打B站接口(旧版每滚一屏全量重搜, 既慢又喂风控)。
    排序(sort): default=综合(抓取相关度序, 分页零漂移) / views=播放量 / pubdate=最新 /
    hot=最新热门。四种排序共享同一波次池 — 切换排序只重排不重抓; 非default排序按
    w["orders"][mode] 缓存快照, 扩波追加了新内容后(池长度变化)自动重排。
    屏蔽开关: 搜索默认不受用户屏蔽机制影响(负反馈/账号拉黑/厌恶词/AI语义厌恶);
    搜索词前缀 * 表示"保留屏蔽"(如 *原神 → 屏蔽了原神就搜不到)。缓存键=原始输入,
    "原神"与"*原神"互不串缓存。refresh=True: 丢弃该词的波次与近义词缓存, 强制重搜(前端↻刷新)。"""
    raw = (q or "").strip()
    apply_blocks = raw.startswith("*")
    q = raw[1:].strip() if apply_blocks else raw   # *只作开关, 不进搜索词
    if not q:
        return {"ok": True, "q": q, "items": [], "offset": 0, "total": 0,
                "end": True, "deep_terms": [], "building": False, "platform_hits": 0}
    bvid = _extract_bvid(q)
    if bvid:
        return _search_bvid(q, bvid, offset, limit, apply_blocks)
    with _search_wave_lock:
        if refresh:
            _search_wave.pop(raw, None)
            _search_deep_cache.pop("__terms__" + q, None)
    work = False   # 本请求是否负责干活(建波/扩波, 其余并发请求仅读快照)
    with _search_wave_lock:
        w = _search_wave.get(raw)
        if w and time.time() - w["ts"] > SEARCH_WAVE_TTL:
            _search_wave.pop(raw, None)
            w = None
        if w is None:
            w = {"items": [], "ts": time.time(), "used_terms": [q], "deep_terms": [],
                 "waves": 0, "end": False, "building": False, "orders": {}}
            _search_wave[raw] = w
            _prune_search_waves()
        if not w["building"]:
            w["building"] = True
            work = True
        building = w["building"]
    if work:
        try:
            if not w["items"]:   # 首建: 第一波
                w["items"] = _search_first_wave(q, apply_blocks)
                w["ts"] = time.time()
            while (not w["end"] and w["waves"] < SEARCH_MAX_WAVES
                   and offset + limit > len(w["items"])):
                new_items, terms = _search_expand_wave(q, w["used_terms"], apply_blocks)
                terms = [t for t in terms if t not in w["used_terms"]]
                if terms:
                    exist = {it.get("key") for it in w["items"]}
                    new_items = [x for x in new_items if x.get("key") and x["key"] not in exist]
                    w["used_terms"].extend(terms)
                    w["deep_terms"].extend(terms)
                    w["waves"] += 1
                    w["items"].extend(new_items)
                    w["ts"] = time.time()
                if not terms or not new_items:
                    w["end"] = True   # 无新词或新词全撞重复 → 到底(防前端空转循环)
            if not w["end"] and offset + limit > len(w["items"]):
                w["end"] = True   # 波次用尽仍不够 → 到底(否则前端会无限重试)
        except Exception:
            w["end"] = True
        finally:
            with _search_wave_lock:
                w["building"] = False
        # 首波一条都没搜到(接口失败/网络抖动/风控)时别把这个空结果缓存一小时:
        # 否则同一个词接下来一小时里永远只剩贴吧。缩短到 2 分钟后重试。
        if not w["items"]:
            with _search_wave_lock:
                w["ts"] = time.time() - SEARCH_WAVE_TTL + 120
    with _search_wave_lock:
        pool = w["items"]
        if sort in SEARCH_SORTS and sort != "default":
            snap = w["orders"].get(sort)
            if snap is None or len(snap) != len(pool):   # 扩波追加了新内容 → 快照失配自动重排
                snap = _sort_search_pool(pool, sort, time.time())
                w["orders"][sort] = snap
            view = snap
        else:
            view = pool   # 综合=原序(与旧版分页行为一致, 零漂移)
        raw_chunk = list(view[offset:offset + limit])
        chunk = list(raw_chunk)
        consumed = len(raw_chunk)
        total = len(view)
        end = w["end"]
        deep_terms = list(w["deep_terms"])
        building = w["building"]
        cross = sum(1 for it in w["items"] if it.get("platform") != "B站")
    if chunk:   # 入库, 供本地沉淀池复用(搜索结果回流)
        now = time.time()
        try:
            with _db_lock:
                for it in chunk:
                    db().execute("INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,?,0) "
                                 "ON CONFLICT(key) DO UPDATE SET json=excluded.json,fetched_at=excluded.fetched_at",
                                 (it["key"], json.dumps(it, ensure_ascii=False), now))
                db().commit()
        except Exception:
            pass
        # 搜索结果交给后台评分(兜底): 若同步评分失败, 后台线程重试写库, 后续命中
        _request_bg_score(chunk)
        if ai_on() and sget("ai_full_scan") == "1":   # 搜索=显式动作, 保留同步补评(与历史一致); feed主路径已改只读缓存
            try:
                cut = MODE_THRESH.get(sget("mode"), 6)
                imin = int(sget("ai_interest_min") or 0) if apply_blocks else 0
                dkws = dislike_keywords() if apply_blocks else []
                scored = score_items(chunk)
                kept, dropped = [], 0
                for it in chunk:
                    if it.get("platform") != "B站":   # 贴吧/小黑盒不走AI评分门禁(无评分语义)
                        kept.append(it)
                        continue
                    sc = scored.get(it["key"])
                    if not sc:
                        continue   # 门禁: 无分数不展示
                    if sc.get("flame", 0) >= cut or sc.get("spam", 0) >= SPAM_CUT:
                        dropped += 1
                        continue   # 质量 门禁(引战/低质)不受屏蔽开关影响, 搜索也不该递垃圾
                    if apply_blocks and (sc.get("interest", 5) or 0) == 0:   # AI语义厌恶命中 → 一票剔除
                        dropped += 1
                        continue
                    if imin and sc.get("interest", 5) < imin:
                        dropped += 1
                        continue
                    if dkws and any(k in (it.get("title", "") + sc.get("topic", "") +
                                          it.get("desc", "")) for k in dkws):
                        dropped += 1   # 标题/主题字面命中厌恶词(未拆词, 防误伤普通游戏)
                        continue
                    it["reason"] = sc.get("reason", "") or ""
                    it["topic"] = sc.get("topic", "") or ""
                    it["interest"] = sc.get("interest", 5)
                    kept.append(it)
                if dropped:
                    bump_stats(filtered=dropped)
                    _funnel_bump("ai_gate", dropped)
                chunk = kept
            except Exception:
                pass
    return {"ok": True, "q": q, "items": chunk, "offset": offset + consumed,
            "next_offset": offset + consumed, "total": total, "end": end,
            "has_more": offset + consumed < total or not end, "deep_terms": deep_terms,
            "building": building, "platform_hits": cross}


# ---------------------------------------------------------------- 信息流世代(无限滚动)

_universe = {"gen": 0, "items": [], "source": "", "offline": True, "ai": False,
             "mode": "", "account": False, "building": False, "built_at": 0,
             "partitions": "全部"}
_uni_lock = threading.Lock()
_uni_token = 0   # 设置版本令牌: 切分区等变更时+1, 旧构建结果作废
_last_force_ts = 0.0   # v0.3.1.55: force_rebuild 合并窗口(见 force_rebuild)
_force_pending = False
_score_progress = {"done": 0, "total": 0, "ts": 0, "full": False}
MAX_UNIVERSE = 2000   # 信息池上限(防内存无限增长); 持续追加到达上限后裁最旧头部


def bake_universe(payload, quick=False):
    """候选池 → 机器筛选 → AI评分(后台补全时) → LRU排序, 产出定型队列。"""
    # 关注标记必须先于机器筛选, 否则关注视频会先按普通播放量/时长规则被剔除。
    for it in payload["items"]:
        if it.get("src") == "关注":
            it["following"] = True
    _annotate_following(payload["items"])
    items = machine_filter(payload["items"])   # 时间窗/播放量/时长/去重/拉黑名单
    # 偏好词直搜结果存活统计: 数据面板可见"偏好·搜到/入池/被拦", 不再只混在通用桶里
    n_pref = sum(1 for it in payload["items"] if it.get("pref_kw"))
    if n_pref:
        n_pref_kept = sum(1 for it in items if it.get("pref_kw"))
        if n_pref_kept:
            _funnel_bump("pref_kept", n_pref_kept)
        if n_pref_kept < n_pref:
            _funnel_bump("pref_drop", n_pref - n_pref_kept)
    # 源产出率自学习: 统计各可裁剪源"抓回→筛选存活"通过率, 低产源后续少抓/跳过。
    _g_fetched, _g_kept = {}, {}
    for it in payload["items"]:
        g = it.get("gid")
        if g in ("rank", "hot", "related"):
            _g_fetched[g] = _g_fetched.get(g, 0) + 1
    for it in items:
        g = it.get("gid")
        if g in _g_fetched:
            _g_kept[g] = _g_kept.get(g, 0) + 1
    for g, f in _g_fetched.items():
        _src_yield_update(g, f, _g_kept.get(g, 0))
    _annotate_following(items)   # 正常刷到的视频里, 作者是关注的人且月内 → 标 following(作者名强调色+标签)
    acct = account_slim()
    acct_authors = {a["name"] for a in (acct.get("authors") or [])}
    acct_kws = [k for k in (acct.get("keywords") or []) if k] + pref_all_kws()   # 偏好备注参与排序加成
    ai = ai_on()
    full = sget("ai_full_scan") == "1"
    _score_progress.update(full=full)
    pool_items = items
    if full and ai and not quick:
        # 开启模式: 只读缓存快速过滤(评分在serve门禁做, 不占评分池/不阻塞世代替换)
        order = sorted(items, key=lambda x: -x["views"])
        scores = score_items(order, only_cache=True)
        for i, it in enumerate(order):
            it["_range_ok"] = True
    else:
        pool_items = items
        scores = {}   # 关闭模式: 不做AI筛查, 零token消耗
    mode = sget("mode")
    cut = MODE_THRESH.get(mode, 6)
    imin = int(sget("ai_interest_min") or 0)
    # 短时间内看到过的不再出现(小时数可调, 0=不限); 仅当剔除后不足一页时保留(最后手段)
    seen_h = int(sget("m_seen_hours") or 6)
    if seen_h > 0:
        recent_seen = {r[0] for r in db().execute(
            "SELECT key FROM shown WHERE ts > ?", (int(time.time()) - seen_h * 3600,)).fetchall()}
        fresh_pool = [it for it in pool_items if it.get("key") not in recent_seen]
        if len(fresh_pool) >= 30:
            pool_items = fresh_pool
    ser = sget("mode") == "严肃"
    if ser:
        # 严肃模式先筛出合格内容; 候选不足一批时恢复近期看过的合格条目, 避免首屏只剩个位数。
        # 偏好词直搜结果(pref_kw)豁免: 用户明写的主动意图不被分区硬拒拦杀。
        fresh_serious = [it for it in pool_items if is_serious_ok(it) or it.get("pref_kw")]
        if len(fresh_serious) < 30:
            fresh_keys = {it.get("key") for it in fresh_serious}
            fresh_serious += [it for it in items if it.get("key") not in fresh_keys
                              and (is_serious_ok(it) or it.get("pref_kw"))]
        pool_items = fresh_serious
    kept, filtered = [], 0
    for it in pool_items:
        sc = scores.get(it["key"])
        if full and ai:
            if sc is None:
                it["_scored"] = False
                it.setdefault("reason", "")
                it.setdefault("topic", "")
                it.setdefault("interest", 7 if it.get("pref_kw") else 5)
                kept.append(it)   # 无分数: 保留进池供后台评分, 展示门禁在 serve_chunk
                continue
            if sc.get("flame", 0) >= cut or sc.get("spam", 0) >= SPAM_CUT:
                filtered += 1
                continue
            if sc.get("interest", 5) == 0:
                continue   # AI语义厌恶命中 → 一票剔除
            if imin and sc.get("interest", 5) < imin:
                continue   # 推荐标准: 兴趣分门槛
            if "emo" in sc and (sc.get("emo") or 0) <= -6:
                filtered += 1
                continue   # 情绪净值极负(纯负能量/贩卖焦虑) → 一票剔除
            if ser and "density" in sc and (sc.get("density") or 0) < 4:
                filtered += 1
                continue   # 严肃模式: 信息密度门槛(废话多不放行)
            if "facts" in sc and not sc.get("facts") and _is_newsy(it) and (sc.get("density") or 0) < 6:
                filtered += 1
                continue   # 资讯无事实锚点+低密度 → 纯情绪输出
            it["_scored"] = True
            it["reason"] = sc.get("reason", "") or ""
            it["topic"] = sc.get("topic", "") or ""
            it["interest"] = sc.get("interest", 5)
        else:
            it["reason"] = ""
            it["topic"] = ""
            it["interest"] = 5
        kept.append(it)
    shown_map = {r[0]: r[1] for r in db().execute("SELECT key, ts FROM shown").fetchall()}
    if full and ai:
        # 已评分内容排前, 未评分垫底(供后台评分; 前端按序加载先展示已筛查内容)
        kept.sort(key=lambda x: (0 if x.get("_scored") else 1, -x["interest"], -x["views"]))
    else:
        kept.sort(key=lambda x: (
            0 if x["author"] in acct_authors else 1,
            0 if x["key"] not in shown_map else 1,
            shown_map.get(x["key"], 0),
            0 if acct_kws and any(k in x["title"] for k in acct_kws) else 1,
            -x["views"]))
    kept = mix_sources(kept)   # 混排: 来源交错, 同类(词/源)不扎堆
    if len(kept) > 1 and kept[0]["key"] == _last_top["key"]:   # 防重复保险
        for j in range(1, len(kept)):
            if kept[j]["key"] != _last_top["key"]:
                kept[0], kept[j] = kept[j], kept[0]
                break
    _last_top["key"] = kept[0]["key"] if kept else None
    bump_stats(filtered=filtered)
    return kept


SEEN_DEDUP_H = 6   # 已展示去重窗口(小时)

# 严肃模式: 只推教程、技能学习与硬核知识; 不靠泛"解析/解说"放行娱乐内容
LEARNING_KWS = ("教程", "教学", "课程", "公开课", "讲座", "学堂", "入门", "自学", "学习",
                "从零", "实操", "精讲", "训练", "练手", "练习", "讲义", "课堂",
                "系统课", "手把手", "初学者", "进阶", "实战", "网课", "培训")
MAKING_KWS = ("编程", "代码", "游戏开发", "unity", "虚幻引擎", "ue5", "ue4", "godot", "blender", "源码", "工程实践")
DEPTH_KWS = ("详解", "原理", "机制", "系统", "架构", "底层", "万字", "深度", "全史",
             "全流程", "拆解", "硬核", "论文", "研究", "实验", "源码", "源码解析")
SERIOUS_AREAS = ("知识", "科技", "纪录片", "人文历史", "军事")
SERIOUS_BLOCK_AREAS = ("番剧", "动画", "国创", "影视", "电影", "电视剧", "娱乐", "鬼畜", "舞蹈", "音乐", "漫画")
SERIOUS_BLOCK_TNAMES = ("番剧", "连载动画", "完结动画", "资讯", "国产动画", "国产原创相关", "MAD·AMV",
                        "短片·手书", "同人·手书", "影视杂谈", "影视剪辑", "电影", "短剧")
SERIOUS_EXTRA = ["教程", "入门", "自学", "从零开始", "精讲", "系统课", "手把手", "初学者",
                 "进阶", "实战", "网课", "公开课", "技能", "学习", "科普", "编程",
                 "剪辑教程", "绘画教程", "音乐制作教程", "健身课程",
                 "科学", "物理", "化学", "生物", "天文", "历史", "哲学", "经济", "心理学",
                 "人工智能", "机器学习", "计算机科学", "法律", "医学", "数学", "社会学"]
ENTERTAINMENT_KWS = ("娱乐", "搞笑", "整活", "鬼畜", "reaction", "反应", "名场面", "高能", "爆笑",
                     "吐槽", "锐评", "吃瓜", "盘点", "剪辑合集", "混剪", "二创", "直播切片",
                     "解说", "评测", "游戏杂谈", "开箱", "皮肤", "抽卡", "氪金", "挑战", "经典镜头")
LOW_VALUE_SERIOUS_KWS = ("招聘", "招募", "有偿", "求一个", "求个", "有没有好用", "推荐一手", "不得不出手", "疑问",
                         "肥羊", "拜鬼", "反思")
TECHNICAL_HELP_KWS = ("教程", "教学", "课程", "建模", "渲染", "拓扑", "蒙皮", "骨骼", "材质", "贴图",
                      "编程", "代码", "开发", "源码", "算法", "原理", "性能", "配置", "硬件",
                      "网络", "服务器", "引擎", "unity", "unreal", "ue5", "godot", "blender")
XHH_TECHNICAL_KWS = ("开发", "源码", "编程", "代码", "引擎", "unity", "unreal", "ue5", "godot",
                     "dlss", "fsr", "显卡", "硬件", "性能", "帧率", "配置", "驱动", "网络", "服务器")
GAME_DOMAIN_KWS = ("游戏", "手游", "端游", "主机", "steam", "绝地潜兵", "helldivers", "战争雷霆",
                   "war thunder", "rimworld", "环世界", "原神", "星穹铁道", "鸣潮", "王者荣耀")
GAME_TASTE_KWS = ("游戏实况", "实况", "通关", "攻略", "配装", "走位", "速通", "身法",
                  "武斗", "开黑", "排位", "上分", "教学视频", "最阴间", "高难", "boss", "BOSS打法", "关卡")
SUBJECT_KWS = ("物理", "化学", "生物", "天文", "历史", "哲学", "经济", "心理", "编程",
               "医学", "法律", "数学", "机器学习", "人工智能", "计算机", "工程", "论文",
               "研究", "分子", "量子", "相对论", "基因", "细胞", "金融", "社会学",
               "结构", "原理", "系统", "机制", "军事", "军工", "装备", "战术")


def serious_relevance(it):
    """严肃模式抓取前排序: 用户偏好和学习/硬核信号优先, 减少抓后过滤损耗。"""
    title = (it.get("title") or "").lower()
    desc = (it.get("desc") or "").lower()
    text = title + " " + desc
    score = 0
    for kw in pref_all_kws():
        k = kw.lower()
        if k and k in text:
            score += 100 + min(len(k), 12)
    score += 30 * sum(1 for k in LEARNING_KWS if k in title)
    score += 24 * sum(1 for k in MAKING_KWS if k in title)
    score += 20 * sum(1 for k in DEPTH_KWS if k in title)
    score += 8 * sum(1 for k in SUBJECT_KWS if k in title)
    score -= 80 * sum(1 for k in ENTERTAINMENT_KWS if k in title)
    score -= 100 * sum(1 for k in GAME_TASTE_KWS if k in title)
    return score


def is_serious_ok(it):
    """严肃模式: 只放教程/制作/开发/硬核知识; 娱乐、实况、泛评测优先剔除。"""
    title = (it.get("title") or "").lower()
    desc = (it.get("desc") or "").lower()
    body = title + " " + desc
    meta = ((it.get("tag") or "") + " " + (it.get("tname") or "") + " " +
            (it.get("area") or "") + " " + (it.get("xcomm") or "") + " " +
            (it.get("xtopic") or "")).lower()
    text = body + " " + meta
    if (it.get("platform") or "B站") == "B站":
        area0 = it.get("area") or ""
        tname0 = it.get("tname") or ""
        if area0 in SERIOUS_BLOCK_AREAS or tname0 in SERIOUS_BLOCK_TNAMES:
            return False
    has_title_learning = any(k in title for k in LEARNING_KWS) or any(k in title for k in MAKING_KWS)
    has_learning = has_title_learning or any(k in body for k in ("公开课", "系统课", "课程", "教程", "教学"))
    game_dev = any(k in title for k in MAKING_KWS) and not any(k in title for k in ("开发者", "制作人", "工作室"))
    title_hard = any(k in title for k in DEPTH_KWS)
    has_hard = title_hard and any(k in title + " " + meta for k in SUBJECT_KWS)
    game_domain = (it.get("area") or "") == "游戏" or any(k in text for k in GAME_DOMAIN_KWS)
    if any(k in body for k in LOW_VALUE_SERIOUS_KWS):
        return False
    if any(k in title for k in GAME_TASTE_KWS):
        return False
    if game_domain and not game_dev:
        return False
    if it.get("platform") == "贴吧":
        has_technical = any(k in title for k in TECHNICAL_HELP_KWS)
        if not has_technical and not has_hard:
            return False
        if has_technical and not any(k in title for k in ENTERTAINMENT_KWS):
            return True
    if it.get("platform") == "小黑盒":
        has_xhh_technical = any(k in title for k in XHH_TECHNICAL_KWS)
        if not has_xhh_technical:
            return False
        if not any(k in title for k in ENTERTAINMENT_KWS):
            return True
    if any(k in title for k in ENTERTAINMENT_KWS) and not has_learning:
        return False
    if has_learning or has_hard:
        return True
    return False


def _bucket_mix(kept):
    """按来源桶加权交错混排: 同词/同源/同平台内容按占比均匀散布, 避免整屏同类扎堆。
    旧版普通轮转的缺陷: 桶大小不均时小桶先耗尽, 大桶剩余内容尾部连片输出(扎堆根源);
    现改为 credit 加权轮转(权重=桶大小), 大桶按比例散布而非堆积; 桶内先洗牌打散抓取顺序。"""
    buckets = {}
    for it in kept:
        buckets.setdefault(it.get("src") or it.get("gid") or "other", []).append(it)
    for b in buckets.values():
        random.shuffle(b)
    if len(buckets) <= 1:
        return list(next(iter(buckets.values()), []))
    live = dict(buckets)
    W = float(sum(len(b) for b in live.values()))
    credit = {k: 0.0 for k in live}
    out = []
    for _ in range(len(kept)):
        cand = [k for k in live if live[k]]
        if not cand:
            break
        for k in cand:
            credit[k] += len(live[k])
        best = max(credit[k] for k in cand)
        ties = [k for k in cand if credit[k] == best]
        pick = random.choice(ties) if len(ties) > 1 else ties[0]
        out.append(live[pick].pop())
        credit[pick] -= W
        if not live[pick]:
            del live[pick]
    return out


# 平台注册表: (platform显示名, 权重设置key, 默认权重); 新增平台在此注册即可(如小黑盒)
PLATFORMS = [
    ("B站", "bili_weight", 90),
    ("贴吧", "tb_weight", 25),
    ("小黑盒", "xhh_weight", 15),
    ("B站直播", "live_weight", 5),    # v0.3.1.51 直播独立成源; v0.3.1.52 默认降到 5(用户要求)
]
_PLATFORM_WKEYS = {p[1] for p in PLATFORMS}


def platform_weights():
    """当前所有平台权重 dict(name→w)。0=该平台不出。"""
    out = {}
    for name, key, default in PLATFORMS:
        try:
            w = int(sget(key) or default)
        except Exception:
            w = default
        out[name] = w if 0 <= w <= 100 else default
    return out


def _enforce_platform_spacing(items, max_run=2):
    """最终出口防平台连发: 只要存在其他平台候选, 同平台最多连续 max_run 条。
    通过向后寻找其他平台并交换, 不丢条目、不改变各平台总量。"""
    out = list(items)
    for i in range(len(out)):
        pname = out[i].get("platform") or "B站"
        start = max(0, i - max_run + 1)
        if i - start + 1 < max_run + 1:
            continue
        if all((out[j].get("platform") or "B站") == pname for j in range(start, i + 1)):
            swap = next((j for j in range(i + 1, len(out))
                         if (out[j].get("platform") or "B站") != pname), None)
            if swap is not None:
                out[i], out[swap] = out[swap], out[i]
    return out


def mix_sources(kept):
    """N平台加权混排(credit加权轮转): 每个平台累计信用额 = 权重, 每步取信用最高的平台,
    取后扣减总权重 → 严格按权重比例均匀交错, 同平台不会扎堆。
    权重语义: 占比 = w_platform / Σw; 0=该平台不出; 全0兜底用默认。"""
    weights = platform_weights()
    groups = {}
    for it in kept:
        pname = it.get("platform") or "B站"
        groups.setdefault(pname, []).append(it)
    if len(groups) <= 1:
        return _enforce_platform_spacing(_bucket_mix(kept))
    active = {n: w for n, w in weights.items() if n in groups and w > 0}
    if not active:
        active = {n: weights.get(n, 5) for n in groups}
    if len(active) == 1:
        return _enforce_platform_spacing(_bucket_mix(groups[next(iter(active))]))
    mixed = {n: _bucket_mix(g) for n, g in groups.items() if n in active}
    total = sum(len(v) for v in mixed.values())
    if total == 0:
        return []
    W = float(sum(active.values()))
    # 配额上限(v0.3.1.57): 每个平台最多出 round(total*w/W) 条(权重>0 至少 1 条)。
    # 池子不够就**少出**, 绝不把别的平台空出来的名额补给它 —— 原来的 credit 轮转在
    # 先把权重高的两路掏空后, 剩下的名额会被权重最低的那一路整段吃掉:
    # 实测 27(B站)/14(小黑盒)/50(直播) 的池子会原样出成 27/14/50, 直播占 55%,
    # 末尾连排 44 条。用户把直播档位设成 5% 却刷出一屏直播, 根因就在这里。
    cap = {}
    for n in mixed:
        cap[n] = max(1, int(math.floor(total * active[n] / W + 0.5)))
    # 均匀铺开(最大缺口法): 每步给各路加"应得份额" w/Σw, 取缺口最大的那路出队并扣 1。
    # 这样 5% 的直播就是每 20 条上下一条, 不会因为 B站/小黑盒先掏空就把剩下的名额
    # 全塞给它(旧的 credit 轮转实测会在末尾连排 44 条直播), 也不会在末尾堆一路。
    share = {n: active[n] / W for n in mixed}
    deficit = {n: 0.0 for n in mixed}
    used = {n: 0 for n in mixed}
    out = []
    pos = {n: 0 for n in mixed}
    while True:
        for n in mixed:
            deficit[n] += share[n]
        # 每路都有自己的上限(前面按权重算好的 cap), 所以这里可以放心把能出的都出了:
        # 池子偏科时最多把某一路堆到它的配额, 不会再出现"把剩下名额整段塞给最低权重那路"。
        cand = [n for n in mixed if used[n] < min(len(mixed[n]), cap[n])]
        if not cand:
            break
        # 同平台不连排三条: 只要还有别的源能出, 就不让第三张连在一起(旧规则, 用户认过)。
        if len(out) >= 2 and (out[-1].get("platform") or "B站") == (out[-2].get("platform") or "B站"):
            lastp = out[-1].get("platform") or "B站"
            alt = [n for n in cand if n != lastp]
            if alt:
                cand = alt
        pick = max(cand, key=lambda n: deficit[n])
        out.append(mixed[pick][pos[pick]])
        pos[pick] += 1
        used[pick] += 1
        deficit[pick] -= 1.0
    return _enforce_platform_spacing(out)


# 同作者降重: 同一作者在单个计划窗口内最多出现 AUTHOR_PLAN_QUOTA 条, 超出的留在池中
# 不入计划 —— 随着已看内容被消费, 后续构建的全局重混会把它们分批"填回来"(视觉降重)。
AUTHOR_PLAN_QUOTA = 3
_AUTHOR_SPACING_GAP = 6   # 同作者/同指纹标题在计划中的最小间隔(尽力满足, 不丢失条目)
_SERVE_SPACING_GAP = 6    # 实际吐出的流里, 最近 N 条参与相似度防扎堆(跨页接缝也算)

_SIM_NOISE = set("""视频 一个 什么 怎么 为什么 如何 这个 那个 我们 你们 他们 自己
就是 不是 可以 已经 没有 真的 还是 但是 因为 所以 如果 然后 最新 完整 官方 合集
教程 分享 推荐 高清 中文 字幕""".split())


def _sim_tokens(title):
    """近重复标题的 shingle: 拉丁词 + 中文二元组, 去掉高频通用词(它们会把无关标题拉到一起)。"""
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"(?<=\d)[.\-_](?=\d)", "", t)   # 4.2 / 4-2 / 4_2 都归一成 42
    toks = [w for w in re.findall(r"[a-z0-9]{2,}", t) if w not in _SIM_NOISE]
    zh = re.sub(r"[^\u4e00-\u9fff]", "", t)
    toks += [zh[i:i + 2] for i in range(max(0, len(zh) - 1))
             if zh[i:i + 2] not in _SIM_NOISE]
    return toks


def _sim_bands(title, bands=4):
    """MinHash 分带: 近重复标题(增减几个字/换一版措辞)至少共享一个 band 键。
    与 f: 指纹互补 —— 指纹要排序后的前 28 个 token 完全一致, 对"同一个新闻换个说法"太严;
    band 只要大部分 shingle 相同就会撞上, 又几乎不会误伤无关内容。"""
    toks = _sim_tokens(title)
    if len(toks) < 6:   # 短标题(7 个中文二元组)也要能判近重复; 6 个仍有足够的区分度
        return []
    out = []
    for b in range(bands):
        salt = str(b) + ":"
        out.append("s:%d:%08x" % (b, min(zlib.crc32((salt + t).encode("utf-8")) & 0xffffffff
                                        for t in toks)))
    return out


def _spacing_keys(it):
    """视觉降重的间距键(多维): 同作者任意标题算同类; 同/近指纹标题跨作者也算同类。"""
    keys = []
    author = it.get("author") or ""
    if author:
        keys.append("a:" + author)
    title = it.get("title") or ""
    if title.strip():
        fp = _title_fp(title)
        if fp:
            keys.append("f:" + fp)
        keys.extend(_sim_bands(title))
    return keys


def _author_quota_hold(order):
    """同作者配额: 超出配额的条目本轮不进计划(留在池中, 等后续构建重混分批放出)。
    池子本身作者极少(≤3)时跳过配额 —— 否则会掏空供给, 此时只靠间距打散。"""
    authors = {it.get("author") or "" for it in order}
    if len(authors) <= 3:
        return order
    counts = {}
    out = []
    for it in order:
        a = it.get("author") or ""
        if a:
            counts[a] = counts.get(a, 0) + 1
            if counts[a] > AUTHOR_PLAN_QUOTA:
                continue   # 留池: 不丢弃, 后续构建达到配额空位时自然重新进入计划
        out.append(it)
    return out


def _space_same_author(items, gap=_AUTHOR_SPACING_GAP):
    """同作者/相似标题打散: 贪心重排, 优先取"间距键不在近期窗口"的条目;
    实在避不开时重置窗口放行(宁相邻不丢失), 保持各平台混排比例不变。"""
    recent = collections.deque()
    recent_set = set()
    # 间距键现在含 MinHash 分带, 重算不便宜 —— 预计算一次, 别在 O(n²) 扫描里反复算。
    pool = []
    for it in items:
        ks = _spacing_keys(it)
        pool.append((it, ks, set(ks)))
    out = []
    while pool:
        pick_idx = None
        for i, (_, _, kset) in enumerate(pool):
            if not (kset & recent_set):
                pick_idx = i
                break
        if pick_idx is None:
            pick_idx = 0       # 剩余全是同作者/相似标题: 放最前并重置窗口, 避免死循环
            recent.clear()
            recent_set.clear()
        it, ks, _ = pool.pop(pick_idx)
        for k in ks:
            recent.append(k)
            recent_set.add(k)
        while len(recent) > gap * 2:
            old = recent.popleft()
            recent_set.discard(old)
        out.append(it)
    return out


def _declump_cat(items, win=5, win_max=3, look=8, max_run=2):
    """同分类轻度去扎堆(有界局部换位, 不做全局重排)。

    用户反馈"一大堆同类型信息挤在一起": mix_sources 只管平台层间距(B站/贴吧/小黑盒),
    平台内部完全不管, 所以同一分区/同一个吧的条目一多就成片出现。
    为什么不是全局轮转重排: 试过按分类队列逐条挑(轮转), 两种分类条目数不相等时会把剩下
    的同类条目全挤到计划尾部 —— 尾部变成单平台长带, serve 层防扎堆只能把它们全部暂缓,
    一页吐不出几条, 反而变成"抓不到内容"(回归测试 '沉积带' 就是这么挂的)。
    这里只做局部交换: 同一分类在最近 win 条里已出现 win_max 次时, 到后面 look 条内找一个
    不同类的条目换上来; 换位后必须仍满足平台连排 <= max_run, 否则跳过。只换位不重排,
    计划整体排序/热度分层/尾部构成都不动。"""
    n = len(items)
    if n < win or look < 1:
        return list(items)
    out = list(items)
    cat = [_cat_key(it) for it in out]
    plat = [(it.get("platform") or "B站") for it in out]

    def _swap_ok(i, j):
        """交换 i/j 后, 两点附近的平台连排是否仍 <= max_run(连排只可能被局部改动影响)。"""
        lo = max(0, min(i, j) - max_run)
        hi = min(n, max(i, j) + max_run + 1)
        seg = plat[lo:hi]
        seg[i - lo], seg[j - lo] = seg[j - lo], seg[i - lo]
        run, last = 0, None
        for p in seg:
            run = run + 1 if p == last else 1
            last = p
            if run > max_run:
                return False
        return True

    for i in range(n):
        c = cat[i]
        if cat[max(0, i - win + 1):i].count(c) < win_max:
            continue
        for j in range(i + 1, min(n, i + 1 + look)):
            cj = cat[j]
            if cj == c:
                continue
            if cat[max(0, i - win + 1):i].count(cj) >= win_max:
                continue          # 换上来也是扎堆, 没意义
            if not _swap_ok(i, j):
                continue
            out[i], out[j] = out[j], out[i]
            cat[i], cat[j] = cat[j], cat[i]
            plat[i], plat[j] = plat[j], plat[i]
            break
    return out


# ================================================================
# 推送套餐引擎(依据 规范.txt 五层蓝图落地):
#   规则① 平台+一级分类 插空轮询打散(在槽位交错内实现)
#   规则② 标题指纹去重(本次推送 + 2h 跨构建窗口)
#   槽位套餐: 舒适60% / 拓展20% / 探索10% / 热点10%(候选不足自动重分配, 宁多勿空)
#   时效分层: 资讯24h保鲜(machine_filter), 干货常青(探索区可复现老干货)
#   跨会话防重复: 记住上次会话末尾5条类型, 新会话前5条避开
#   时段适应: 晚间(22-6)多轻内容, 周末早晨(6-10)多深度内容
#   实时干预: 聊天框"未来X小时内别推Y"临时屏蔽 / "探索"临时拉高探索区配额
# ================================================================

SLOT_WEIGHTS = {"comfort": 60, "expand": 20, "explore": 10, "social": 10}
SLOT_NAMES = {"comfort": "舒适区", "expand": "拓展区", "explore": "探索区", "social": "热点"}

NEWS_KWS = ("新闻", "资讯", "突发", "官宣", "发布", "上线", "更新公告", "版本更新", "降价", "涨价",
            "发布会", "首曝", "宣布", "上新", "开售", "发售", "停售", "下架", "翻车", "道歉", "回应",
            "热搜", "热点", "实锤", "最新进展", "辟谣", "通报", "公示", "直播", "预告",
            "爆料", "被曝", "被爆", "吃瓜", "偷跑", "泄露", "传闻")
CLICKBAIT_KWS = ("惊了", "居然", "竟然", "万万没想到", "99%的人", "不看后悔", "看完惊呆",
                 "炸裂", "颠覆认知", "彻底改变", "一夜之间", "背后真相", "细思极恐", "你绝对",
                 "必看", "秒懂", "天塌了", "全网疯传", "刷屏", "爆火", "神反转", "当场", "惊呆了",
                 "最后悔", "血赚", "血亏", "逆天", "还能这样", "直接看傻", "不会吧不会吧",
                 "震惊", "惊爆", "离大谱", "删前速看", "速看", "手慢无", "内幕", "黑幕",
                 "毁三观", "颠覆三观", "你敢信", "敢信", "别怪我没提醒", "全网最低", "史上最",
                 "后悔没早", "紧急提醒", "紧急通知", "再不看就", "错过再等")
FLAME_SLURS = ("开喷", "对线", "孝子", "结晶", "薄纱", "暴杀", "滚出", "别洗了", "急了", "破防了",
               "带节奏", "拉踩", "碰瓷", "饭圈", "脑残粉", "黑子", "水军", "恰烂钱", "割韭菜", "智商税",
               "乐子人", "杠精", "喷子", "挂人", "网暴", "回旋镖", "老登", "拱火", "泼脏水", "扣帽子",
               "骂战", "对喷", "互喷", "人身攻击", "引战")
# 刻意不收"开团/团建/杂鱼/小鬼"等词: 它们在游戏区/生活区有大量正常用法(开团打本/公司团建), 收进来会误伤。
# 标题党句式兜底(词表只能抓固定词): 开头党/诱导党/绝对化/悬念党/拉踩式/数字党/挑衅式
CLICKBAIT_RE = re.compile(
    r"^(?:震惊|惊呆|突发|重磅|紧急|劲爆|炸裂|离谱|离大谱|活久见)"
    r"|不看后悔|后悔没早|错过再等|删前速看|删前必看|再不看就"
    r"|(?:全网|全球|全服)(?:第一|首个|唯一|最强|最全)"
    r"|(?:真相|内幕|黑幕)[^，。！？]{0,6}(?:竟然?|居然|曝光|大白)"
    r"|(?:吊打|完爆)(?:全场|同行|所有|一切|同级)"
    r"|\d{2}%的人"
    r"|不服来[辩战]|不接受反驳|杠就是你对|你敢信[?？!！]*$")
# 广告/引流识别(查简介, 非标题): 硬标记任一命中即判广告;
# 软标记需≥2个同时出现才判(正常教程简介单提一句"欢迎关注公众号"不会被拦)
AD_HARD = ("加微信", "加qq", "加vx", "优惠券", "优惠码", "返利", "折扣码", "私聊获取",
           "私信领取", "扫码关注", "闲鱼", "带货", "下单链接", "拼团", "秒杀价", "代购",
           "有偿", "付费进群")
AD_SOFT = ("公众号", "粉丝群", "微信群", "扫码", "回复关键词", "领取资料", "私信我",
           "后台回复", "进群", "赞助", "推广")
LIGHT_AREAS = ("娱乐", "搞笑", "日常", "生活", "音乐", "鬼畜", "综艺", "影视")
DEEP_AREAS = ("纪录片", "知识", "科技", "人文历史")

_fp_recent = {}          # 标题指纹 → 时间戳(2h 跨构建窗口, 防同一条新闻反复重推)
_fp_lock = threading.Lock()
_plan_lock = threading.Lock()
_plan_cache = {"key": None, "order": [], "token": -1, "epoch": -1, "stail": None}   # 套餐规划缓存(分页顺序稳定)
_last_serve_ts = [0.0]   # 上次出流时间(30分钟无请求 = 新会话)
_score_epoch = 0         # 评分写入世代: 每次有新评分落库 +1(规划缓存随之失效, 全扫模式渐进出新内容)


def _title_fp(title):
    """标题指纹(规则②): 拉丁词/数字 + 中文二元组, 去重排序取前28字符。
    同主题不同措辞的标题共享大量二元组 → 指纹相同被合并, 杜绝"同一新闻换个说法反复推"。"""
    t = (title or "").lower()
    toks = re.findall(r"[a-z0-9]{2,}", t)
    zh = re.sub(r"[^\u4e00-\u9fff]", "", t)
    toks += [zh[i:i + 2] for i in range(max(0, len(zh) - 1))]
    if not toks:
        return "x" + t[:20]
    return "".join(sorted(set(toks)))[:28]


def _fp_dup(fp, now):
    """指纹是否在2h窗口内已推过(顺带记录与清理过期指纹)。"""
    with _fp_lock:
        for k in [k for k, ts in _fp_recent.items() if now - ts > 7200]:
            _fp_recent.pop(k, None)
        hit = fp in _fp_recent
        _fp_recent[fp] = now
        return hit


def _fp_clear():
    """清空指纹记忆: 信息池清空(刷新/清池)时同步调用,
    否则重建会拉回同样内容 → 被旧指纹全部拦截 → 信息流永远为空。"""
    with _fp_lock:
        _fp_recent.clear()


def _cat_key(it):
    """一级分类键(规则①的桶键): 平台 + 粗分类。"""
    p = it.get("platform") or "B站"
    cat = (it.get("area") or it.get("tname") or it.get("xcomm")
           or (it.get("topic") or "").split("、")[0] or (it.get("tag") or "").split(",")[0]
           or it.get("src") or "").strip()
    return "%s|%s" % (p, cat[:10] or "综合")


def _item_tags(it):
    vals = []
    if it.get("platform") == "贴吧":
        vals = [it.get("tname"), it.get("tclass"), it.get("tag")]
    elif it.get("platform") == "小黑盒":
        vals = [it.get("xtopic"), it.get("xcomm"), it.get("tag"), it.get("tname")]
    else:
        vals = [it.get("tag"), it.get("tname"), it.get("area"), it.get("topic")]
    out = []
    for v in vals:
        for part in re.split(r"[,，、/|;；\s]+", str(v or "")):
            p = part.strip("#[]【】()（） ")
            if p and p not in out:
                out.append(p)
            if len(out) >= 3:
                return " / ".join(out)
    return " / ".join(out) if out else (it.get("platform") or "综合")


def _is_newsy(it):
    """资讯/热点判定(时效分层用): 标题或来源命中新闻词。"""
    t = ((it.get("title") or "") + (it.get("tname") or "")).lower()
    return any(k in t for k in NEWS_KWS)


def _is_evergreen(it):
    """常青干货判定: 教程/深度词或知识类来源(探索区可跨时间窗复现)。"""
    text = ((it.get("title") or "") + (it.get("tag") or "") + (it.get("tname") or "")).lower()
    if any(k in text for k in LEARNING_KWS + DEPTH_KWS):
        return True
    return (it.get("area") or it.get("tname") or "") in DEEP_AREAS


def _emo_bad(it):
    """情绪过滤器(纯规则零token): 感叹号/问号/混合/叠词轰炸、引战黑话、标题党词表与句式
    → 返回原因, 通过返回''。只对标题判定(简介可能引用原文, 防误伤)。"""
    t = (it.get("title") or "").lower()
    ex = t.count("!") + t.count("！") + t.count("‼") + t.count("⁉")
    qm = t.count("?") + t.count("？")
    if ex >= 3 or (ex >= 2 and len(t) <= 16):
        return "情绪"
    if qm >= 4 or (qm >= 3 and len(t) <= 14) or (ex >= 1 and qm >= 2):
        return "情绪"
    if re.search(r"([\u4e00-\u9fff])\1{3}", t) or re.search(r"([\u4e00-\u9fff]{2})\1\1", t):
        return "情绪"   # 叠词轰炸: 同字4连(哈哈哈哈)/同二字词3连(完了完了完了)
    if any(k in t for k in FLAME_SLURS):
        return "引战"
    if any(k in t for k in CLICKBAIT_KWS):
        return "标题党"
    if CLICKBAIT_RE.search(t):
        return "标题党"
    return ""


def _ad_bad(it):
    """广告/引流判定(查简介, 非标题): 硬标记任一命中即判广告;
    软标记≥2个同时出现才判, 防误伤正常教程简介。"""
    d = (it.get("desc") or "").lower()
    if not d:
        return False
    if any(k in d for k in AD_HARD):
        return True
    return sum(1 for k in AD_SOFT if k in d) >= 2


# ---------------------------------------------------------------- 快速文本分析(fastText 本地小模型)
# v0.3.1.43dsk: 内置一个约 60MB 的中文 fastText 多标签分类模型, 在**本地**判断
# "这条文本是不是引战/低质/标题党/阴阳怪气" —— 零 token、零联网、纯 CPU。
#
# 定位(与既有两层的关系):
#   · 账号身份层(弹幕/评论筛选: 黑名单/等级/简介/抽奖号) 判"谁发的" —— 本模块不参与。
#   · 规则层(_emo_bad 词表/正则) 判"命中没命中固定词" —— 覆盖不到的就漏。
#   · 本模块判"语义像不像垃圾话" —— 补规则层的漏, 且不烧 token(补 AI 层的贵)。
# 总开关 ft_on 与 m_dm_on / cf_* 相互独立: 那两组关掉不影响这里, 反之亦然。
_FT = {"m": None, "err": "", "path": "", "loaded_at": 0, "tried": False}
_FT_LOCK = threading.Lock()
_FT_WS = re.compile(r"\s+")
# 标签 → 用户看得懂的原因文案(面板「显示屏蔽」拿它做行内标签)
_FT_WHY = {"flame": "引战", "spam": "低质", "clickbait": "标题党",
           "yy": "阴阳怪气", "neg": "负面"}
_FT_ALL = ("flame", "spam", "clickbait", "yy", "neg")
_FT_MAXLEN = 64          # ★ 必须与 build_corpus.py 的 MAXLEN 一致
_FT_CACHE = {}           # 文本 → 原因(空串=放行)。弹幕/评论重复率极高, 这一层最省 CPU。
_FT_CACHE_CAP = 40000
_FT_CACHE_SIG = [None]   # 缓存对应的配置签名(标签集合+阈值); 配置一变就整表作废


def _ft_path():
    """定位模型文件: 兼容 PyInstaller onefile(_MEIPASS 解包目录) 与源码运行。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "model", "ft_model.bin"),
              os.path.join(base, "ft_model.bin"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "model", "ft_model.bin"),
              os.path.join(DATA_DIR, "ft_model.bin")):
        if os.path.isfile(p):
            return p
    return ""


def ft_load():
    """惰性加载模型(线程安全)。失败不抛异常, 只记 err —— 绝不能让筛选功能拖垮主流程。

    ★ 用自研的纯 stdlib 运行时 ftrt, 不用官方 fasttext 包:
      本产品打包走 Python 3.14, 而 fasttext 只有源码包(要 MSVC), 社区预编译 wheel
      最高 cp311 —— 在 exe 里 import fasttext 必然失败。详见 ftrt.py 顶部说明。
    """
    with _FT_LOCK:
        if _FT["m"] is not None:
            return _FT["m"]
        if _FT["tried"] and _FT["err"]:
            return None
        _FT["tried"] = True
        p = _ft_path()
        _FT["path"] = p
        if not p:
            _FT["err"] = "模型文件缺失(未随包分发?)"
            return None
        try:
            import ftrt
            _FT["m"] = ftrt.FastTextLite(p, maxlen=_FT_MAXLEN, temp=3.0)
            _FT["loaded_at"] = int(time.time())
            _FT["err"] = ""
        except Exception as e:
            _FT["err"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        return _FT["m"]


def ft_enabled():
    """总开关 + 模型可用。任何一步不满足都当作"不启用", 调用方直接放行。"""
    if sget("ft_on") != "1":
        return False
    return ft_load() is not None


def _ft_tok(t):
    """与训练脚本 build_corpus.py 的 tok() 必须逐字一致: **整句不切字**, 只折叠空白。

    中文靠模型自带的字符级 n-gram(minn=2..maxn=4)自动切, 手工分词反而更差。
    """
    t = _FT_WS.sub(" ", str(t or "")).strip()
    t = t.replace("\u200b", "").replace("\ufeff", "")
    return t[:_FT_MAXLEN]


def ft_labels_on():
    """启用中的标签集合(设置里可勾)。非法/空 → 退回默认四个。"""
    raw = (sget("ft_labels") or "").strip()
    got = set(x.strip() for x in raw.split(",") if x.strip()) & set(_FT_ALL)
    return got or set(("flame", "spam", "clickbait", "yy"))


def ft_scan(texts, kind="video"):
    """批量判定。返回与 texts 等长的列表, 元素为命中原因(空串=放行)。

    kind: video / dm / cf —— 各自有独立的作用范围开关, 关掉则整批放行。
    """
    n = len(texts)
    blank = [""] * n
    if n == 0:
        return blank
    if sget("ft_on") != "1":
        return blank
    scope = {"video": "ft_video", "dm": "ft_dm", "cf": "ft_cf"}.get(kind, "ft_video")
    if sget(scope) != "1":
        return blank
    m = ft_load()
    if m is None:
        return blank
    want = ft_labels_on()
    try:
        thr = max(0.05, min(0.99, float(sget("ft_thresh") or 70) / 100.0))
    except Exception:
        thr = 0.70

    out = list(blank)
    labels = m.labels
    if not (set(labels) & want):
        return out
    # ★ 缓存键只含文本, 所以判定口径一变(勾选的标签/阈值)必须整表作废,
    #   否则用户改了设置却看不到变化 —— 这是实测踩到的坑。
    sig = (",".join(sorted(want)), round(thr, 4))
    if _FT_CACHE_SIG[0] != sig:
        _FT_CACHE.clear()
        _FT_CACHE_SIG[0] = sig
    for i, raw in enumerate(texts):
        t = _ft_tok(raw)
        if len(t) < 3:            # 空/纯标点不进模型
            continue
        hit = _FT_CACHE.get(t)
        if hit is None:
            try:
                sc = m.scores(t)
            except Exception:
                continue
            best, bestp = "", 0.0
            for li, name in enumerate(labels):
                if name in want and sc[li] > bestp:
                    best, bestp = name, sc[li]
            hit = _FT_WHY.get(best, best) if (best and bestp >= thr) else ""
            if len(_FT_CACHE) < _FT_CACHE_CAP:
                _FT_CACHE[t] = hit
        if hit:
            out[i] = hit
    return out


def ft_probe(text):
    """单条文本的**原始**判定结果 —— 给设置页「试一下」用。

    与 ft_scan 的区别: ft_scan 只回"拦不拦", 这里回完整的标签概率排名。
    用户能直接看见模型到底怎么想, 调阈值/勾标签时心里有数 —— 否则这功能
    对用户就是个黑盒(调了阈值却不知道为什么拦/不拦)。
    """
    if sget("ft_on") != "1":
        return {"ok": False, "reason": "快速文本分析总开关未开启"}
    m = ft_load()
    if m is None:
        return {"ok": False, "reason": _FT["err"] or "模型未加载"}
    t = _ft_tok(text)
    if len(t) < 3:
        return {"ok": False, "reason": "文本太短(不足 3 个字, 不进模型)"}
    try:
        sc = m.scores(t)
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:80])}
    labels = m.labels
    rows = sorted(((sc[i], labels[i]) for i in range(len(labels))), reverse=True)
    if not rows:
        return {"ok": False, "reason": "模型标签表为空"}
    want = ft_labels_on()
    try:
        thr = max(0.05, min(0.99, float(sget("ft_thresh") or 70) / 100.0))
    except Exception:
        thr = 0.70
    best_p, best_l = rows[0]
    hit = bool(best_l in want and best_p >= thr)
    return {"ok": True,
            "text": t,
            "label": best_l,
            "name": _FT_WHY.get(best_l, best_l),
            "score": round(best_p, 4),
            "hit": hit,
            "picked": best_l in want,
            "thresh": round(thr, 4),
            "why": (_FT_WHY.get(best_l, best_l) if hit else ""),
            "top": [{"label": l, "name": _FT_WHY.get(l, l), "score": round(p, 4)}
                    for p, l in rows[:5]]}


def ft_status():
    """给设置页显示的状态(不触发加载, 只看已加载状态 + 文件是否存在)。"""
    p = _FT["path"] or _ft_path()
    size = os.path.getsize(p) if p and os.path.isfile(p) else 0
    labels = []
    m = _FT["m"]
    if m is not None:
        try:
            labels = list(m.labels)
        except Exception:
            labels = []
    return {"on": sget("ft_on") == "1",
            "ready": m is not None,
            "path": p,
            "size_mb": round(size / 1048576.0, 1),
            "err": _FT["err"],
            "labels": labels,
            "loaded_at": _FT["loaded_at"],
            "has_module": _ft_module_ok(),
            "cache": len(_FT_CACHE)}


def _ft_module_ok():
    """运行期 ftrt 这个模块本身能不能用(和模型文件是否存在是两回事)。"""
    try:
        import ftrt
        return hasattr(ftrt, "FastTextLite")
    except Exception:
        return False


def ft_warmup():
    """后台预热: 打开开关后立刻把模型读进内存, 免得第一次筛选卡一下。"""
    def _run():
        try:
            if sget("ft_on") == "1":
                ft_load()
        except Exception:
            pass
    try:
        threading.Thread(target=_run, name="ft-warmup", daemon=True).start()
    except Exception:
        pass


def tmp_block_kws():
    """临时屏蔽词(聊天指令写入, 到点自动解封并清理)。"""
    raw = sget("tmp_blocks") or ""
    try:
        blocks = json.loads(raw) if raw else []
    except Exception:
        blocks = []
    now = time.time()
    alive = [b for b in blocks if isinstance(b, dict) and b.get("k")
             and float(b.get("until") or 0) > now]
    if len(alive) != len(blocks):
        sset("tmp_blocks", json.dumps(alive, ensure_ascii=False))
    return [str(b["k"]).lower() for b in alive]


def add_tmp_block(kw, minutes):
    """写入一条临时屏蔽(与旧块合并去重)。"""
    raw = sget("tmp_blocks") or ""
    try:
        blocks = json.loads(raw) if raw else []
    except Exception:
        blocks = []
    blocks = [b for b in blocks if isinstance(b, dict) and b.get("k") and b.get("k") != kw]
    blocks.append({"k": kw, "until": time.time() + max(1, minutes) * 60})
    sset("tmp_blocks", json.dumps(blocks, ensure_ascii=False))


def _profile_signals():
    """舒适区信号(感知层): 偏好词 + 喜欢作者 + 近期点击主题/作者/领域(隐式信号)。"""
    prefs = [k.lower() for k in pref_all_kws()]
    authors = {r[0] for r in db().execute(
        "SELECT author FROM feedback WHERE action='like'").fetchall() if r[0]}
    topics, areas = set(), set()
    try:
        rows = db().execute("SELECT key FROM history ORDER BY ts DESC LIMIT 60").fetchall()
    except Exception:
        rows = []
    hmap = items_by_keys([k for (k,) in rows])   # v0.3.1.31: 原本逐条 item_by_key(60 次往返)
    for (k,) in rows:
        it = hmap.get(k)
        if not it:
            continue
        for t in ((it.get("topic") or "").split("、") + (it.get("tag") or "").split(",")
                  + [it.get("area") or "", it.get("tname") or ""]):
            t = t.strip()
            if 2 <= len(t) <= 8:
                topics.add(t)
        if it.get("author"):
            authors.add(it["author"])
        areas.add(_cat_key(it))
    return {"prefs": prefs, "authors": authors,
            "topics": {t.lower() for t in topics}, "areas": areas}


def _slot_of(it, sig, med, now):
    """槽位分类: 舒适(画像命中) / 热点(资讯+显著高热) / 拓展(优质) / 兜底舒适。"""
    text = ((it.get("title") or "") + " " + (it.get("desc") or "") + " " +
            (it.get("tag") or "") + " " + (it.get("tname") or "") +
            " " + (it.get("topic") or "") + " " + (it.get("author") or "")).lower()
    if any(k in text for k in sig["prefs"]) or (it.get("author") or "") in sig["authors"]:
        return "comfort"
    topic = (it.get("topic") or "").strip()
    if topic and topic.lower() in sig["topics"]:
        return "comfort"
    if _cat_key(it) in sig["areas"]:
        return "comfort"
    if _is_newsy(it) and it.get("pubdate") and now - it["pubdate"] < 86400:
        return "social"
    if (it.get("views") or 0) >= med * 2:
        return "social"
    if (it.get("interest") or 5) >= 7 or (it.get("views") or 0) >= med:
        return "expand"
    return "comfort"


def _explore_candidates(exclude, limit=30):
    """探索区候选池(进化层): 库里7天前入库的老而优干货(播放≥10w 或常青判定),
    打破茧房跨领域复现; 排除近期已看/厌恶/情绪/资讯类与全部负反馈内容。"""
    dkws = dislike_keywords()
    neg_disliked = {r[0] for r in db().execute(
        "SELECT DISTINCT key FROM feedback WHERE action='dislike'").fetchall()}
    neg_authors = {r[0] for r in db().execute("SELECT value FROM blacklist WHERE kind='author'")}
    neg_kws = [r[0].lower() for r in db().execute("SELECT value FROM blacklist WHERE kind='keyword'")]
    neg_tmp = tmp_block_kws()
    out = []
    try:
        rows = db().execute(
            "SELECT json FROM items WHERE fetched_at < ? AND key NOT IN "
            "(SELECT key FROM shown WHERE ts > ?) ORDER BY RANDOM() LIMIT 120",
            (time.time() - 7 * 86400, time.time() - 6 * 3600)).fetchall()
    except Exception:
        return out
    for (j,) in rows:
        try:
            it = json.loads(j)
        except Exception:
            continue
        if not isinstance(it, dict) or it.get("key") in exclude or not it.get("views"):
            continue
        if _emo_bad(it) or _is_newsy(it):
            continue
        text = ((it.get("title") or "") + " " + (it.get("desc") or "") +
                " " + (it.get("tag") or "")).lower()
        if dkws and any(k in text for k in dkws):
            continue
        # 探索区同样执行负反馈: 已不喜欢/黑名单/临时屏蔽的内容不得借"探索"回流。
        if it.get("key") in neg_disliked or it.get("author") in neg_authors:
            continue
        if (neg_kws or neg_tmp) and (any(k in text for k in neg_kws) if neg_kws else False):
            continue
        if neg_tmp and any(k in (text + " " + (it.get("author") or "")).lower() for k in neg_tmp):
            continue
        if it["views"] >= 100000 or _is_evergreen(it):
            out.append(it)
        if len(out) >= limit:
            break
    return out


def _time_boost(it, now):
    """时段适应(短周期): 晚间轻内容加权, 周末早晨深度内容加权(桶内稳定排序用)。"""
    lt = time.localtime(now)
    h, wd = lt.tm_hour, lt.tm_wday
    b = 0
    if h >= 22 or h < 6:
        dur = it.get("duration") or 0
        if 0 < dur <= 360 or (it.get("tname") or "") in LIGHT_AREAS:
            b += 3
    if wd >= 5 and 6 <= h < 10:
        if (it.get("duration") or 0) >= 1200 or (it.get("tname") or "") in DEEP_AREAS or _is_evergreen(it):
            b += 3
    return b


def _interleave_slots(buckets, weights):
    """槽位 credit 加权交错(同 mix_sources): 平局随机, 保证各槽按比例均匀散布不扎堆。"""
    total = sum(len(v) for v in buckets.values())
    if total == 0:
        return []
    keys = [k for k in buckets if buckets[k]]
    if len(keys) == 1:
        return list(buckets[keys[0]])
    random.shuffle(keys)
    wsum = float(sum(weights.get(k, 0) for k in keys))
    credit = {k: 0.0 for k in keys}
    pos = {k: 0 for k in keys}
    out = []
    for _ in range(total):
        for k in keys:
            credit[k] += weights.get(k, 0)
        cand = [k for k in keys if pos[k] < len(buckets[k])]
        if not cand:
            break
        best = max(credit[k] for k in cand)
        ties = [k for k in cand if credit[k] == best]
        pick = random.choice(ties) if len(ties) > 1 else ties[0]
        out.append(buckets[pick][pos[pick]])
        pos[pick] += 1
        credit[pick] -= wsum
    return out


def planned_feed(acts, new_session=False, exclude=None):
    """信息流套餐规划(策略层): 门禁(严肃/AI新维度/临时屏蔽) → 槽位分类 → 加权交错。
    返回 (有序列表, 槽位统计); 池太薄时按需退化, 绝不丢条目。
    exclude: 已在上游规划中的 key(追加规划时用, 防探索候选重复进入)。"""
    exclude = exclude or set()
    mode = sget("mode")
    full = ai_on() and sget("ai_full_scan") == "1"
    cut = MODE_THRESH.get(mode, 6)
    imin = int(sget("ai_interest_min") or 0)
    now = time.time()
    # --- 门禁(信息流路径总入口; 搜索路径仍走原逻辑) ---
    # 偏好词直搜结果(pref_kw)豁免严肃硬拒: 备注里的主题是用户主动要看的, 不被分区拦杀。
    if mode == "严肃":
        acts = [it for it in acts if is_serious_ok(it) or it.get("pref_kw")]
    keep, dropped = [], 0
    tmp = tmp_block_kws()
    scores = {}
    dkws = dislike_keywords() if full else []
    if full:
        try:
            cached, _ = _load_cache(acts, score_ttl())
            scores = cached
        except Exception:
            scores = {}
        if not scores:
            full = False   # 冷启动降级: 尚无任何评分 → 临时按热度放行, 首批分出来后再恢复门禁(避免长时间空白)
    sig = _profile_signals()
    for it in acts:
        if it.get("_range_ok") is False:
            continue
        if tmp:
            text = ((it.get("title") or "") + (it.get("tag") or "") + (it.get("tname") or "")
                    + (it.get("topic") or "") + (it.get("desc") or "")).lower()
            if any(k in text for k in tmp):
                dropped += 1
                continue
        if full:
            sc = scores.get(it["key"])
            if not sc:
                if it.get("is_live"):
                    # 直播是「此刻正在发生」的内容: 等 AI 评分落库时直播可能已经结束,
                    # 所以不受「未评分不展示」门禁拦(与偏好词同理, 属于即时性豁免)。
                    it["reason"] = "正在直播"
                    it["interest"] = it.get("interest") or 7
                    keep.append(it)
                    continue
                if not it.get("pref_kw"):
                    dropped += 1
                    continue   # 门禁: 未评分不展示(后台渐进补评分)
                # 偏好词直搜结果: 用户主动意图, 未评分也放行(评分落库后仍受质量门禁约束)
                it["reason"] = "来自你的偏好备注，评分稍后补上"
                it["topic"] = ""
                it["interest"] = 7
                keep.append(it)
                continue
            if sc.get("flame", 0) >= cut or sc.get("spam", 0) >= SPAM_CUT:
                dropped += 1
                continue
            if (sc.get("interest", 5) or 0) == 0:
                dropped += 1
                continue
            if imin and sc.get("interest", 5) < imin:
                dropped += 1
                continue
            if "emo" in sc and (sc.get("emo") or 0) <= -6:
                dropped += 1
                continue   # 情绪净值极负(纯负能量/贩卖焦虑) → 一票剔除
            if mode == "严肃" and "density" in sc and (sc.get("density") or 0) < 4:
                dropped += 1
                continue   # 严肃模式: 信息密度门槛(废话多不放行)
            if "facts" in sc and not sc.get("facts") and _is_newsy(it) and (sc.get("density") or 0) < 6:
                dropped += 1
                continue   # 资讯无事实锚点+低密度 → 纯情绪输出
            if dkws and any(k in (it.get("title", "") + sc.get("topic", "") + it.get("desc", "")) for k in dkws):
                dropped += 1
                continue
            it["reason"] = sc.get("reason", "") or ""
            it["topic"] = sc.get("topic", "") or ""
            it["interest"] = sc.get("interest", 5)
        keep.append(it)
    if dropped:
        bump_stats(filtered=dropped)
        _funnel_bump("ai_gate", dropped)
    acts = keep
    # 冷却是硬性排除，不因候选池变薄而放宽。
    cooling = cooldown_keys(now)
    if cooling:
        dropped_cool = sum(1 for it in acts if it.get("key") in cooling)
        if dropped_cool:
            _funnel_bump("cooldown", dropped_cool)
        acts = [it for it in acts if it.get("key") not in cooling]
    # 已看窗口过滤: 近期看过的条目不再进规划(池子太薄时自动放宽, 与 bake 同策略)
    seen_h = int(sget("m_seen_hours") or 6)
    if seen_h > 0:
        try:
            recent = {r[0] for r in db().execute(
                "SELECT key FROM shown WHERE ts > ?", (now - seen_h * 3600,)).fetchall()}
        except Exception:
            recent = set()
        if recent:
            fresh = [it for it in acts if it["key"] not in recent]
            # 已看窗口默认严格执行; 仅当剔除后不足一页(最后手段)才保留近期已看, 避免空页。
            if len(fresh) >= 30:
                acts = fresh
    # 候选不足一页时，按最近一次展示时间最早的冷却项少量复用，避免冷却策略把信息流锁空。
    if len(acts) < 30 and cooling:
        active_keys = {it.get("key") for it in acts}
        cooled = [it for it in keep if it.get("key") in cooling and it.get("key") not in active_keys]
        if cooled:
            state_rows = {r[0]: r[1] for r in db().execute(
                "SELECT key,last_presented_at FROM item_exposure_state WHERE key IN (%s)" %
                ",".join("?" * len(cooled)), tuple(it["key"] for it in cooled)).fetchall()}
            cooled.sort(key=lambda it: state_rows.get(it.get("key"), 0))
            for it in cooled[:30 - len(acts)]:
                it["_cooldown_relaxed"] = True
                acts.append(it)
                _funnel_bump("cooldown_relaxed")
    if len(acts) <= 8:   # 池太薄: 不强行套餐, 保持原有排序(宁多勿空)
        for it in acts:
            it.pop("_slot", None)
        return acts, {}
    # --- 槽位分类 ---
    views = sorted(it.get("views", 0) for it in acts)
    med = views[len(views) // 2] if views else 0
    avoid = set()
    if new_session:
        try:
            avoid = set(json.loads(sget("last_types") or "[]"))
        except Exception:
            avoid = set()
    buckets = {"comfort": [], "expand": [], "explore": [], "social": []}
    for it in acts:
        if it.get("key") in exclude:
            continue   # 上游规划已含该条目, 不重复进套餐
        slot = _slot_of(it, sig, med, now)
        it["_slot"] = SLOT_NAMES[slot]
        buckets[slot].append(it)
    for it in _explore_candidates({x["key"] for x in acts} | exclude):
        if it.get("key") in cooling:
            continue
        it["_slot"] = SLOT_NAMES["explore"]
        buckets["explore"].append(it)
    # 时段适应: 桶内按 (时段加权, 原顺序) 稳定排序(保持热度大序, 只做轻推)
    for k in buckets:
        order = list(enumerate(buckets[k]))
        order.sort(key=lambda p: (-_time_boost(p[1], now), p[0]))
        buckets[k] = [b for _, b in order]
    # 权重自适应: 候选不足的槽位把额度分给有货的槽位; 探索指令生效时临时拉高探索区
    weights = dict(SLOT_WEIGHTS)
    if float(sget("explore_boost_ts") or 0) > now:
        weights = {"comfort": 40, "expand": 15, "explore": 30, "social": 15}
    active = {k: v for k, v in weights.items() if buckets.get(k)}
    if not active:
        return acts, {}
    total_w = float(sum(active.values()))
    weights = {k: (active[k] / total_w * 100.0) for k in active}
    order = _interleave_slots(buckets, weights)
    order = mix_sources(order)   # 最终出口再按平台权重重排, 避免槽位套餐冲淡贴吧/小黑盒比例
    order = _author_quota_hold(order)   # 同作者配额: 超出的留池, 后续构建分批放出(视觉降重)
    order = _space_same_author(order)   # 同作者/相似标题打散: 不再相邻出现
    order = _declump_cat(order)         # 同分类(分区/吧/话题)局部去扎堆: 不再"一堆同类挤在一起"
    # 跨会话防重复: 新会话前5条强制避开上次会话末尾的类型
    if avoid and len(order) > 5:
        head, rest, used = [], [], set()
        for it in order:
            if len(head) < 5 and _cat_key(it) not in avoid:
                head.append(it)
                used.add(id(it))
        for it in order:
            if id(it) not in used:
                rest.append(it)
        order = head + rest
    slots = {}
    for k, name in SLOT_NAMES.items():
        n = sum(1 for it in order if it.get("_slot") == name)
        if n:
            slots[name] = n
    return order, slots


def _slot_reason(it):
    """可解释性: 槽位标签 + AI理由; 无AI理由时显示内容标签/吧名/话题。"""
    slot = it.get("_slot") or ""
    reason = (it.get("reason") or "").strip()
    if not slot:
        return reason or _item_tags(it)
    if reason.startswith(slot + "·"):
        return reason
    return "%s·%s" % (slot, reason or _item_tags(it))


def cooldown_keys(now=None):
    now = int(now or time.time())
    try:
        return {r[0] for r in db().execute(
            "SELECT key FROM item_exposure_state WHERE cooldown_until > ?", (now,)).fetchall()}
    except Exception:
        return set()


_UPSERT_EXPOSURE = ("INSERT INTO item_exposure_state(key,run_count,last_presented_at,cooldown_until) "
                    "VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "run_count=excluded.run_count,last_presented_at=excluded.last_presented_at,"
                    "cooldown_until=excluded.cooldown_until")
_SQL_CHUNK = 300     # IN / 多值 VALUES 分片大小(SQLite 变量上限远大于此, 留足余量)


def record_feed_presentations(items, request_id, now=None):
    """只在卡片实际返回给信息流时累计展示；同一请求重试不会重复计数。

    v0.3.1.31: 旧实现是**逐条 3 条语句**(30 条 = 94 条语句 / 6.2ms, 实测),
    现在固定 3 条批量语句。语义完全不变:
      · 同一 request_id 内重复的 key 只登记一次 —— 旧实现靠 INSERT OR IGNORE 的
        rowcount==0 去重, 这里改用 INSERT..RETURNING(只返回真正插入的行)等价实现;
      · _cooldown_relaxed 的条目计数继续递增、冷却截止原样保留。
    """
    if not request_id:
        return
    now = int(now or time.time())
    try:
        limit = max(1, int(sget("m_repeat_limit") or 4))
        hours = max(0, int(sget("m_repeat_cooldown_hours") or 72))
    except Exception:
        limit, hours = 4, 72
    seen, keys, relaxed = set(), [], set()
    for it in items:
        k = it.get("key")
        if not k:
            continue
        if it.get("_cooldown_relaxed"):
            relaxed.add(k)
        if k not in seen:
            seen.add(k)
            keys.append(k)
    if not keys:
        return
    # 限时拿锁: 后台重建会长时间占着 _db_lock, 一次记账不值得让整页信息流一起等。
    # 拿不到就直接跳过本次登记(下面的 shown 回写也一样, 在 serve_chunk 里同样限时)。
    if not _db_lock.acquire(timeout=0.4):
        return
    try:
        conn = db()
        # ① 批量登记展示, RETURNING 只把"本次真正新登记"的 key 取回来
        fresh = []
        for i in range(0, len(keys), _SQL_CHUNK):
            part = keys[i:i + _SQL_CHUNK]
            ph = ",".join("(?,?,?)" for _ in part)
            params = []
            for k in part:
                params.extend((k, request_id, now))
            for r in conn.execute("INSERT OR IGNORE INTO item_presentation(key,request_id,presented_at) "
                                  "VALUES " + ph + " RETURNING key", params):
                fresh.append(r[0])
        if not fresh:
            _commit()
            return
        # ② 一次 IN 取回这批 key 的曝光状态
        state = {}
        for i in range(0, len(fresh), _SQL_CHUNK):
            part = fresh[i:i + _SQL_CHUNK]
            ph = ",".join("?" for _ in part)
            for r in conn.execute("SELECT key,run_count,cooldown_until FROM item_exposure_state "
                                  "WHERE key IN (" + ph + ")", part):
                state[r[0]] = (r[1], r[2])
        # ③ 一次 executemany 落库
        rows = []
        for k in fresh:
            row = state.get(k)
            if k in relaxed:
                # 冷却期间被复用: 计数继续递增, 严格保留原冷却截止(既不清零也不续期)。
                rows.append((k, (row[0] if row else 1) + 1, now, (row[1] if row else 0)))
                continue
            run = 1 if not row or (row[1] > 0 and row[1] <= now) else row[0] + 1
            cooldown = now + hours * 3600 if hours and run >= limit else 0
            rows.append((k, run, now, cooldown))
        conn.executemany(_UPSERT_EXPOSURE, rows)
        _commit()
    finally:
        _db_lock.release()


def _feed_plan(acts, new_session, gen):
    """套餐规划带缓存, 返回有序列表(无限滚动分页顺序稳定):
    - 设置令牌/评分epoch/设置项都未变, 且规划条目仍是当前池子集 → 视为纯追加:
      新条目单独规划后接在尾部, 已有顺序不动(offset 不错位);
    - 令牌变化(刷新/清池/切分区)/评分落库/设置变化/新会话 → 全量重规划(应用跨会话防重复)。
    seq: 重规划代数; 每次全量重规划 +1, 会话游标据此归零重扫(已返回 key 由会话集合跳过)。"""
    stail = (sget("mode"), ai_on(), sget("ai_full_scan"),
             sget("ai_interest_min"), sget("m_seen_hours"),
             sget("dislike_note") or "", sget("tmp_blocks") or "",
             sget("explore_boost_ts") or "0",
             sget("feedback_sig") or "0")   # 负反馈信号: 点踩后强制重排, 计划中的不喜欢条目立即剔除
    key = (len(acts), _uni_token, _score_epoch, stail)
    with _plan_lock:
        c = _plan_cache
        if c["key"] == key:
            return c["order"]
        plan_keys = {it["key"] for it in c["order"]} if c.get("order") else set()
        act_keys = {it["key"] for it in acts}
        appended_only = (not new_session and c.get("token") == _uni_token
                         and c.get("epoch") == _score_epoch and c.get("stail") == stail
                         and plan_keys and plan_keys <= act_keys)
        if appended_only:
            # 追加时不做"新批拼尾": 未消费后段整体重新混排。
            # 原因: B站热榜跨构建重叠大(新批B站越来越少), 贴吧/小黑盒游标轮换持续出新,
            # 逐批拼尾会让非B站沉积在计划末尾形成扎堆带; 全局重混让新到的B站插回中间。
            # 前缀只保留"已实际返回"的条目: serve层防扎堆暂缓的未返回条目若按游标冻结在
            # 前缀里, 会把沉积带一并冻住, 新B站永远插不进去(游标只代表扫描位置, 不代表消费)。
            with _session_lock:
                latest = max(_session_served.values(), key=lambda s: s["ts"]) if _session_served else None
                boundary = latest["cursor"] if latest else 0
                served_union = set(latest["keys"]) if latest else set()
            prefix = [it for it in (c["order"] or [])[:boundary] if it["key"] in served_union]
            served_keys = {p["key"] for p in prefix} | served_union
            suffix_in = [it for it in acts if it["key"] not in served_keys]
            t, _ = planned_feed(suffix_in, False)
            c["order"] = prefix + t
            c["seq"] = c.get("seq", 0) + 1   # 后段已重混 → 会话游标归零重扫(已返回 key 被跳过)
        else:
            c["order"], _ = planned_feed(acts, new_session)
            c["seq"] = c.get("seq", 0) + 1   # 重规划代数+1 → 会话游标归零重扫(防重复/防漏)
        c.update(key=key, token=_uni_token, epoch=_score_epoch, stail=stail)
        return c["order"]


# 会话级已返回集合: sid → {"keys":已返回key, "cursor":计划游标, "seq":重规划代数, "ts"}
_session_served = {}
_session_lock = threading.Lock()


def _session_state(sid):
    """取/建会话状态(容量8, 淘汰最旧); 无 sid 返回 None。"""
    if not sid:
        return None
    with _session_lock:
        s = _session_served.get(sid)
        if s is None:
            if len(_session_served) >= 8:
                for old in sorted(_session_served, key=lambda k: _session_served[k]["ts"])[:len(_session_served) - 7]:
                    del _session_served[old]
            s = {"keys": set(), "cursor": 0, "seq": 0, "ts": time.time(), "spacing": []}
            _session_served[sid] = s
        s["ts"] = time.time()
        return s


_last_full_build_ts = 0   # 上次完整补池完成时间(节流冗余全量抓取)
_BUILD_PENDING = False    # v0.3.1.55: 构建忙时收到的补池请求, 合并成"跑完再补一次"
# 构建节流(v0.3.1.59): 只有"忙/不忙"两个状态不够 —— 滚动时 serve_chunk 每 5~10 秒
# 就会因为"未送出条目不够"再拉一次补池(旧日志实测 5 秒一次), 每次都要把贴吧/小黑盒/
# 搜索源全打一遍。这既是贴吧风控的源头, 也让缓存里明明有存货时反复空转。
# 现在非用户发起的补池有最小间隔; 连续几次什么都没抓到时按 streak 退避。
_BUILD_MIN_GAP = 40
_next_build_ts = 0.0
_build_empty_streak = 0


def _build_backoff(added_n):
    """一次构建结束后决定下一次最早什么时候能再补。

    added_n 为 0(全重复/全部源失败)时越退越远, 最多 4 分钟 —— 这时候快速重试
    只会继续打已有且已经被拒的源, 除了喂风控没有别的作用。"""
    global _next_build_ts, _build_empty_streak
    now = time.time()
    if added_n is None:          # 作废/异常: 只按最小间隔来, 不改退避档位
        _next_build_ts = now + _BUILD_MIN_GAP
        return
    if added_n > 0:
        _build_empty_streak = 0
        _next_build_ts = now + _BUILD_MIN_GAP
        return
    _build_empty_streak = min(_build_empty_streak + 1, 6)
    _next_build_ts = now + min(_BUILD_MIN_GAP + 25 * _build_empty_streak, 240)


def kick_build(quick=True, pending=False):
    """补池入口(唯一): 单飞 + 合并。
    为什么要收敛到一个入口: 原来刷新按钮、滚动补池、见底补池、冷启动各自
    `Thread(target=build_universe)`, 而它们读到的 building 是快照里的旧值 ——
    两次点击就能起两个构建, 同时对贴吧发两串请求。这正是"看起来在刷站"的来源之一。
    现在先原子占位再起线程; 忙时只记一个 pending, 等当前这次跑完自动补一次。"""
    global _BUILD_PENDING
    # 非用户发起的补池(滚动/见底/开屏轮询)受节流约束; pending=True 是"用户按了刷新/
    # 改了设置", 必须立刻生效。
    if not pending and time.time() < _next_build_ts:
        return False
    with _uni_lock:
        if _universe["building"]:
            # pending=True 只给"用户明确要一次"(刷新/改设置): 这类请求不能丢。
            # 滚动补池/开屏轮询传 False —— 它们马上还会再来, 丢掉正好省一次抓取,
            # 也避免"构建刚结束又被轮询立刻拉起"的死循环。
            if pending:
                _BUILD_PENDING = True
            return False
        _universe["building"] = True          # 先占位, 线程里按已占位启动
        _universe["mode"] = "quick" if quick else "full"
    threading.Thread(target=build_universe, kwargs={"quick": quick, "_claimed": True},
                     daemon=True).start()
    return True


def build_universe(quick=False, _claimed=False):
    """构建新世代(绑定当前设置令牌)。quick=True 只取2个源秒开屏, 之后后台线程补全。
    令牌变更(切分区等)后, 正在跑的旧构建结果自动作废, 不会覆盖新设置。
    注: 原"首批≥10条立即发布"的特殊首页机制已删除——刷新/启动时全部内容统一
    走同一套 抓取→入库→bake→发布 管线, 不再有前几条的捷径(它导致刷新后内容不变)。"""
    global _BUILD_PENDING    # finally 里会读它并清掉; 不声明就成局部变量, UnboundLocalError
    with _uni_lock:
        if not _claimed:
            if _universe["building"]:
                return None
            _universe["building"] = True
        _universe["mode"] = "quick" if quick else "full"   # full 构建期间不被 quick 打断
        token = _uni_token
        _funnel_bump("build_quick" if quick else "build_full")

    added_n = None   # 本次真正新增的池内条目数(给节流退备用; None=作废/异常, 不算空转)
    try:
        payload = fetch_pool(quick=quick)
        if not payload["offline"]:
            now = time.time()
            # 真实抓取路径就在这里: 状态面板的"上次抓取"必须由这里写。
            # 唯一另一处写入点在 get_feed_raw(), 而它当前没有任何调用方 —— 不在这里写,
            # 状态面板的"上次抓取"永远是"未抓取"。
            sset("fetch_last", int(now))
            keys = [it["key"] for it in payload["items"]]
            ph = ",".join("?" * len(keys)) if keys else ""
            exist = {r[0] for r in db().execute(
                "SELECT key FROM items WHERE key IN (%s)" % ph, tuple(keys))} if ph else set()
            new_n = len(set(keys) - exist)   # 只计真正新增的条目
            with _db_lock:   # 入库: 沉淀池基础 + 7天清理
                # v0.3.1.31: 旧实现每条先 SELECT json 再 UPSERT, 而取回的 old/prev **从未被使用**
                # (UPSERT 恒以 excluded.json 覆盖) —— 纯浪费: 冷启动一次实测 61 条无用 SELECT。
                db().executemany("INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,?,0) "
                                 "ON CONFLICT(key) DO UPDATE SET json=excluded.json,fetched_at=excluded.fetched_at",
                                 [(it["key"], json.dumps(it, ensure_ascii=False), now)
                                  for it in payload["items"]])
                db().execute("DELETE FROM items WHERE fetched_at < ?", (now - 7 * 86400,))
                db().commit()
            bump_stats(received=new_n)   # 今日收录只累计新增
            # 回流时把**当前池子里已有的**也排除掉, 不能只排这一轮抓回来的。
            # 否则每次构建都从"最新的 3000 行"里挑同样的前 300 条, 池子就卡在同一个深度
            # 不再增长 —— 实测连做 5 轮补池, 池深一直是 482, 本地一万多条没推过的存货
            # 一条都没被端上来。排掉池内已有的之后, 每一轮都往更旧的那批走, 池子才会
            # 真的随着构建逐轮加深(仍然是纯本地读, 不联网)。
            with _uni_lock:
                _in_pool = {it.get("key") for it in _universe["items"]}
            exclude = {it["key"] for it in payload["items"]} | {k for k in _in_pool if k}
            payload["items"] = payload["items"] + backlog_items(exclude, enabled_source_ids())
        kept = bake_universe(payload, quick=quick)
        threading.Thread(target=ensure_tags, kwargs={"items": kept}, daemon=True).start()   # 后台补标签(厌恶匹配用)
        offline, source = payload["offline"], payload["source"]
        if token != _uni_token:   # 设置已变, 本构建作废
            return None
        with _uni_lock:
            first_fill = not _universe["items"]
            if first_fill:
                new_items, added = kept, kept   # 池空: 首次构建直接填充
            else:
                # 持续追加(去重): 无限滚动不清空不重置; 超上限裁最旧头部(offset 极深时才触发)
                existing = {it["key"] for it in _universe["items"]}
                _now = time.time()
                added = [it for it in kept if it["key"] not in existing
                         and not _fp_dup(_title_fp(it["title"]), _now)]   # 2h同指纹只推一次
                new_items = list(_universe["items"]) + added
                if len(new_items) > MAX_UNIVERSE:
                    new_items = new_items[len(new_items) - MAX_UNIVERSE:]
            added_n = len(added)
            # built_at 仅在产出新条目时刷新: 空 quick 构建(抓回全是重复)不再推迟补池,
            # 否则薄池状态会被"空构建刷新时间戳"饿死, 用户始终等不到全量补池。
            _universe.update(gen=_universe["gen"] + 1, items=new_items, source=source,
                             offline=offline, ai=ai_on(), mode=sget("mode"),
                             account=account_slim().get("ok", False),
                             partitions=current_partition_names(),
                             built_at=int(time.time()) if (added or first_fill) else _universe["built_at"])
        _request_bg_score(kept)   # 当前批次候选入队, 后台渐进评分写库(交互路径只读缓存秒回)
        persist_cold_cache()   # 冷启动预缓存(5分钟节流)
        if not quick:
            global _last_full_build_ts
            _last_full_build_ts = time.time()
        if quick:   # 开屏后立即后台补全完整世代(榜单+相关链+搜索+随机)
            # 防堆叠: 刚做过完整补池时跳过这次全量(60s 内); 否则正常补全。
            if time.time() - _last_full_build_ts < 60:
                return _universe["gen"]

            def _full():
                # 等父线程 finally 清 building 标志, 消除启动竞态(否则补全线程入口即放弃)
                for _ in range(200):
                    with _uni_lock:
                        if not _universe["building"]:
                            break
                    time.sleep(0.05)
                # pending=True: 刚跑完的 quick 构建已把节流闸门推到 25 秒后,
                # 这次全量是"开屏后补全", 属于必须跑的那一次, 不能被节流吃掉。
                kick_build(quick=False, pending=True)
            threading.Thread(target=_full, daemon=True).start()
        return _universe["gen"]
    except Exception:
        return None
    finally:
        # v0.3.1.55: 标志必须无条件清掉 —— 旧实现"令牌变了就不清", 于是一次设置变更
        # 会把 building 永久钉死在 True, 后面所有补池直接 return: 池子再也不动。
        # 之前靠刷新分支里手动 `building=False` 兜这个坑, 结果又和并发构建撞在一起。
        with _uni_lock:
            _universe["building"] = False
            again = _BUILD_PENDING
            _BUILD_PENDING = False
        _build_backoff(added_n)
        if again:
            kick_build(quick=quick, pending=True)


def feed_refresh_item(key, q=""):
    """把 key 标记为「已看」(复用 shown 表 + m_seen_hours 机制), 然后从池子随机返回一条
    未看过的内容作为替换; 池子没有可用内容时先 fetch_pool 补池再抽。返回 item dict 或 None。
    q 非空(搜索模式): 只从「与搜索词相关(标题/摘要分词命中)」的未看内容里换, 不串台到无关内容。"""
    now_ts = int(time.time())
    with _db_lock:
        db().execute("INSERT OR REPLACE INTO shown(key, ts) VALUES(?,?)", (key, now_ts))
        db().execute("DELETE FROM shown WHERE ts < ?", (now_ts - 3 * 86400,))
        db().commit()
    seen_h = int(sget("m_seen_hours") or 6)
    seen_cut = now_ts - seen_h * 3600
    q = (q or "").strip()

    def _recent_seen():
        if seen_h <= 0:
            return set()
        try:
            with _db_lock:
                return {r[0] for r in db().execute(
                    "SELECT key FROM shown WHERE ts > ?", (seen_cut,)).fetchall()}
        except Exception:
            return set()

    def _pick():
        # 负反馈硬排除: 已不喜欢/已屏蔽/临时屏蔽的 key 与作者, "换一条"绝不递回。
        disliked = {r[0] for r in db().execute(
            "SELECT DISTINCT key FROM feedback WHERE action='dislike'").fetchall()}
        bl_rows = db().execute("SELECT kind,value FROM blacklist").fetchall()
        bl_authors = {v for k, v in bl_rows if k == "author"}
        bl_kws = [v.lower() for k, v in bl_rows if k == "keyword"]
        tmp_kws = tmp_block_kws()
        try:
            with _db_lock:
                if q:
                    # 搜索模式: 拉候选(最近入库) → python 分词过滤 → 随机换同主题未看内容
                    rows = db().execute(
                        "SELECT json FROM items WHERE key != ? ORDER BY fetched_at DESC LIMIT 600",
                        (key,)).fetchall()
                elif seen_h > 0:
                    row = db().execute(
                        "SELECT json FROM items WHERE key != ? AND key NOT IN "
                        "(SELECT key FROM shown WHERE ts > ?) AND key NOT IN "
                        "(SELECT key FROM item_exposure_state WHERE cooldown_until > ?) ORDER BY RANDOM() LIMIT 1",
                        (key, seen_cut, now_ts)).fetchone()
                    return row
                else:
                    row = db().execute(
                        "SELECT json FROM items WHERE key != ? ORDER BY RANDOM() LIMIT 1",
                        (key,)).fetchone()
                    return row
        except Exception:
            return None
        if not q:
            return None
        toks = [t for t in re.split(r"[\s,，。！？!?、;；]+", q.lower()) if len(t) >= 2]
        if not toks:
            return None
        rse = _recent_seen()
        cand = []
        for (j,) in rows:
            try:
                it = json.loads(j)
            except Exception:
                continue
            if it.get("key") == key or it.get("key") in rse or it.get("key") in disliked:
                continue
            if it.get("author") in bl_authors:
                continue
            text = ((it.get("title") or "") + " " + (it.get("desc") or "") +
                    " " + (it.get("tag") or "") + " " + (it.get("tname") or "") +
                    " " + (it.get("author") or "")).lower()
            if bl_kws and any(k in text for k in bl_kws):
                continue
            if tmp_kws and any(k in text for k in tmp_kws):
                continue
            if any(t in text for t in toks):
                cand.append(it)
        if not cand:
            return None
        it = random.choice(cand)
        return (json.dumps(it, ensure_ascii=False),)

    row = _pick()
    if not row:
        # 池子没有未看过的 → 先补池(抓新内容入池), 再抽一次
        try:
            fetch_pool(quick=True)
        except Exception:
            pass
        row = _pick()
    if not row:
        return None
    try:
        it = json.loads(row[0])
    except Exception:
        return None
    if not isinstance(it, dict):
        return None
    # 保证前端卡片渲染所需字段齐全
    for f in ("views", "likes", "reply", "pubdate", "duration", "author", "tname",
              "title", "desc", "cover", "url", "platform", "key", "reason"):
        it.setdefault(f, "" if f in ("author", "tname", "title", "desc", "cover",
                                     "url", "platform", "key", "reason") else (0 if f != "duration" else 0))
    _annotate_following([it])
    # 替换卡片同样记入 shown(否则它仍留在计划原位, 用户滚到那里会再看到一次)。
    try:
        with _db_lock:
            db().execute("INSERT OR REPLACE INTO shown(key, ts) VALUES(?,?)", (it["key"], int(time.time())))
            db().commit()
    except Exception:
        pass
    it.setdefault("danmaku", None)
    it.setdefault("covers", [it["cover"]] if it.get("cover") else [])
    it.setdefault("key", key)
    return it


def force_rebuild():
    """设置变更(切分区)时调用: 作废旧构建并在最新设置下立即重建。
    v0.3.1.55: 加 2 秒合并窗口 —— 拖一次占比滑条会连着 POST 好几个值, 每个都触发一次
    重建(=3 个贴吧请求)。窗口内的后续调用只挂一个 1.5 秒后的补建, 最终状态照样是
    最新设置, 但不会一条一条地连发。"""
    global _uni_token, _last_force_ts, _force_pending
    now = time.time()
    with _uni_lock:
        _uni_token += 1
        too_soon = now - _last_force_ts < 2.0
        _last_force_ts = now
        if too_soon:
            if _force_pending:
                return          # 已经排了一次合并重建, 别叠
            _force_pending = True
    if too_soon:
        threading.Timer(1.5, _force_later).start()
        return
    kick_build(quick=True, pending=True)
    # 等这次(或正在跑的那次)构建落地, 最多 8 秒: 权重/筛选改完要立刻生效, 否则前端
    # restartFeed 拿到的还是旧池子的混排结果(改前这里是同步重建, 语义一致)。
    # 等不到就算了 —— 构建还在后台跑, 请求不该被无限期挂住(单飞保证不会重复抓)。
    end = time.time() + 8.0
    while time.time() < end:
        with _uni_lock:
            if not _universe["building"]:
                return
        time.sleep(0.05)


def _force_later():
    global _force_pending
    _force_pending = False
    try:
        kick_build(quick=True, pending=True)
    except Exception:
        pass


_cold_last_save = [0]   # 预缓存节流游标


def persist_cold_cache(force=False):
    """把池首未展示条目按设置上限存库(冷启动预缓存)。
    持续持久化而非退出时保存: 手机上进程随时可能被系统杀掉。"""
    n = int(sget("m_cold_start_n") or 0)
    if n <= 0:
        if sget("cold_start_cache"):
            sset("cold_start_cache", "")
        return
    if not force and time.time() - _cold_last_save[0] < 300:   # 5分钟节流
        return
    with _uni_lock:
        items = list(_universe["items"])
    if not items:
        return
    try:
        shown = {r[0] for r in db().execute("SELECT key FROM shown")}
    except Exception:
        shown = set()
    keep = [it for it in items if it.get("key") not in shown][:n]
    if not keep:
        keep = items[:n]   # 兜底: 全部已展示过(极少)也缓存池首
    fields = ("key", "platform", "title", "author", "desc", "cover", "covers", "views",
              "views_known", "likes", "danmaku", "reply", "duration", "duration_known",
              "pubdate", "url", "tname", "area", "tag", "tclass", "gid", "src",
              "reason", "topic", "interest", "following", "xcomm", "xtopic", "xlid", "ltag")
    slim = [{k: it.get(k) for k in fields} for it in keep]
    sset("cold_start_cache", json.dumps({"ts": int(time.time()), "items": slim}, ensure_ascii=False))
    _cold_last_save[0] = time.time()


def _cold_start_preheat():
    """冷启动: 设置开启且有缓存 → 缓存条目注入空池立即展示。返回是否注入。
    注入后由后台 quick 构建补全完整池, 构建合并逻辑天然去重(按 key)。
    缓存用后不清空(手机进程随时可能死在"消费→重建保存"之间, 清了下次就空白):
    注入时按 shown 表过滤已看条目, 没看过的排前面, 全看过才原样注入(空屏比重复更糟)。
    下次 build_universe 末尾的 persist_cold_cache 会用新快照覆盖。"""
    n = int(sget("m_cold_start_n") or 0)
    if n <= 0:
        return False
    raw = sget("cold_start_cache")
    if not raw:
        return False
    try:
        cached = json.loads(raw)
    except Exception:
        return False
    if isinstance(cached, list):          # 旧格式(无时间戳): 视为已过期, 不再使用
        return False
    if not isinstance(cached, dict) or not isinstance(cached.get("items"), list):
        return False
    if time.time() - int(cached.get("ts") or 0) > 7 * 86400:   # 超7天: 内容太旧, 弃用重建
        sset("cold_start_cache", "")
        return False
    items = cached["items"]
    if not items:
        return False
    with _uni_lock:
        if _universe["items"]:
            return False
        # 已看过滤: 上次会话展示过的排后面(内容池去重按 key, 注入不会造成双份)
        try:
            seen = {r[0] for r in db().execute("SELECT key FROM shown")}
        except Exception:
            seen = set()
        fresh = [it for it in items if it.get("key") not in seen]
        old = [it for it in items if it.get("key") in seen]
        _universe["items"] = (fresh + old)[:max(n, len(items))]
        _universe["built_at"] = int(time.time())
    return True


def serve_chunk(offset=0, limit=30, q="", request_id="", plat=""):
    """取当前世代的 [offset:offset+limit]; 到底时自动触发新生代构建。"""
    with _uni_lock:
        uni = dict(_universe)
        items = list(_universe["items"])
        building = _universe["building"]
    # 平台筛选在**服务端**做: 前端只是把别的平台的卡片 display:none 藏起来时,
    # 那些"看不见的卡片"照样占着会话额度与 DOM, 真正要看的那一路被稀释 ——
    # 缓冲和流对不上, 快速滑动时很快就没得看。放在这里过滤, 流里就只有这一个平台。
    if plat:
        items = [it for it in items if (it.get("platform") or "B站") == plat]
    if not items:
        # 冷启动预缓存: 有缓存 → 注入空池并立即吐出(后台同时构建完整池); 没有才纯等待
        preheated = _cold_start_preheat()
        if not building:
            kick_build(quick=True)
        if not preheated:
            return {"ok": True, "items": [], "building": True, "gen": uni["gen"],
                    "source": uni["source"], "offline": uni["offline"],
                    "ai": uni["ai"], "mode": uni["mode"], "account": uni["account"],
                    "partitions": uni.get("partitions", "全部"), "offset": offset,
                    "next_offset": offset, "has_more": True}
        with _uni_lock:
            uni = dict(_universe)
            items = list(_universe["items"])
            building = _universe["building"]   # 构建线程已接管, 正常服务缓存条目
    acts = apply_filters(items, q)   # 黑名单/不喜欢/临时屏蔽始终生效; q 非空时额外做子集过滤
    new_session = (time.time() - _last_serve_ts[0]) > 1800   # 30分钟无请求 = 新会话(跨会话防重复)
    _last_serve_ts[0] = time.time()
    if not q:
        acts = _feed_plan(acts, new_session, uni["gen"])   # 推送套餐规划(含门禁)
    # 会话级游标分页(仅信息流): 同会话已返回的 key 永不重复; 计划重排后游标归零重扫,
    # 已返回 key 被跳过、未返回 key 绝不漏 —— 彻底消除"重排导致 offset 漂移"的重复/丢失。
    sid = (request_id or "").split(":")[0]
    sess = _session_state(sid) if not q else None
    if sess is not None:
        with _plan_lock:
            plan_seq = _plan_cache.get("seq", 0)
        if sess["seq"] != plan_seq:
            sess["seq"] = plan_seq
            sess["cursor"] = 0   # 计划已重排: 从头重扫(已返回 key 在下方循环中跳过)
        chunk, idx = [], sess["cursor"]
        # 平台防扎堆(服务层兜底): 某轮构建单平台新批偏大(如B站抓空时贴吧一批五六十条)时,
        # 计划虽经 mix_sources 交错, 但会话游标跳过"已返回"的旧条目后, 用户实际看到的
        # 未返回流就是连片新批 —— 开屏刷着刷着贴吧成堆出现。这里对实际吐出的流兜底:
        # 同平台最多连2条(与 mix_sources 同标准), 多余的暂缓(不入已返回集合, 留在池中
        # 等下次重排再上); 暂缓导致本页不满时按"构建中"处理, 让后台补池接续, 不误报到底。
        # 单平台池不启用(否则吞吐退化为每页2条轮询)。
        plat_multi = len({(it.get("platform") or "B站") for it in acts}) > 1
        spaced_defer = 0
        last_p, run = sess.get("last_p"), sess.get("last_run", 0)   # 跨页持续: 上页末尾的连排不算清零
        # 相似内容防扎堆(v0.4.2): 计划层已经按作者/标题指纹打散过, 但会话游标跳过"已返回"
        # 条目后, 实际吐出的流仍可能把同一作者/近重复标题接连送出(尤其是跨页接缝)。
        # 这里记住最近 6 条内容的间距键, 撞键的先暂缓, 留到后面再出。
        spacing_recent = sess.setdefault("spacing", [])
        spacing_set = set()
        for ks in spacing_recent:
            spacing_set |= ks
        while idx < len(acts) and len(chunk) < limit:
            it = acts[idx]
            if it.get("key") not in sess["keys"]:
                p = it.get("platform") or "B站"
                keys = set(_spacing_keys(it))
                if (plat_multi and p == last_p and run >= 2) or (spacing_set and (keys & spacing_set)):
                    spaced_defer += 1
                    idx += 1
                    continue
                chunk.append(it)
                sess["keys"].add(it.get("key"))
                last_p, run = (p, run + 1) if p == last_p else (p, 1)
                spacing_recent.append(keys)
                if len(spacing_recent) > _SERVE_SPACING_GAP:
                    del spacing_recent[:len(spacing_recent) - _SERVE_SPACING_GAP]
                spacing_set = set()
                for ks in spacing_recent:
                    spacing_set |= ks
            idx += 1
        # 救急: 本页被暂缓压得只剩一两条, 或已扫到计划末尾仍凑不满一页 —— 此时宁可放宽
        # 连排/相似度限制, 也要把池里已有的内容发出去。候选是**本会话所有未送出**的条目
        # (不只本次暂缓的那批): 旧实现只回捞本次暂缓, 小池里本页吐 3 条、其余甩到游标之后,
        # 下一页从尾部扫描一条都看不到, 会一直空转到补池 —— 表现为"抓不到内容"。
        # 贪心挑不与最近 6 条撞间距键的; 实在全撞就清空窗口放行(宁相邻不丢失)。
        if len(chunk) < limit and (len(chunk) <= 2 or idx >= len(acts)):
            pending = [it for it in acts if it.get("key") not in sess["keys"]]
            pkeys = [set(_spacing_keys(it)) for it in pending]
            while pending and len(chunk) < limit:
                pick = None
                for j, ks in enumerate(pkeys):
                    if not (ks & spacing_set):
                        pick = j
                        break
                if pick is None:
                    pick = 0
                    spacing_recent.clear()
                    spacing_set.clear()
                it = pending.pop(pick)
                keys = pkeys.pop(pick)
                p = it.get("platform") or "B站"
                chunk.append(it)
                sess["keys"].add(it.get("key"))
                last_p, run = (p, run + 1) if p == last_p else (p, 1)
                spacing_recent.append(keys)
                if len(spacing_recent) > _SERVE_SPACING_GAP:
                    del spacing_recent[:len(spacing_recent) - _SERVE_SPACING_GAP]
                spacing_set = set()
                for ks in spacing_recent:
                    spacing_set |= ks
        sess["last_p"], sess["last_run"] = last_p, run
        consumed_end = idx
        sess["cursor"] = consumed_end
        if spaced_defer:
            sess["defer_ts"] = time.time()
            if len(chunk) < limit and not building \
                    and time.time() - uni["built_at"] > 10:
                need_full = time.time() - _last_full_build_ts > 90
                kick_build(quick=not need_full)
            if len(chunk) < limit:
                building = True   # 前端按"构建中"静默轮询续上, 不显示"已到底"
    else:
        chunk = acts[offset:offset + limit]
        consumed_end = min(offset + limit, len(acts))
    # 提前补池: 剩余未看内容不多时后台就开始抓, 用户滚到底时新内容已经就位(不必干等)。
    # 剩余量必须按"本会话未返回"计(池子总数-已返回key), 不能按游标尾部条数: 评分落库/
    # 负反馈都会触发计划全量重排, 已返回条目被混到游标之后, 尾部计数虚高 → 服务端一直
    # 以为池子还厚, 补池迟迟不触发, 用户刷到底才干等读条(此前阈值=60仍复现的根因)。
    # 阈值=150(约5页): 全量构建需10-30s, 快刷十几秒烧一页, 两页缓冲会被追上, 5页才稳跑赢。
    # 快/全动态决策: 余量<60(两页)时quick补的量追不上快刷消耗, 直接跑全量; 距上次全量
    # >90s也跑全量, 其余用轻量 quick(防止小水慢添)。
    unserved = max(0, len(acts) - (len(sess["keys"]) if sess else 0))
    if not q and not building and chunk and unserved < 150 \
            and time.time() - uni["built_at"] > 10:
        need_full = unserved < 60 or time.time() - _last_full_build_ts > 90
        kick_build(quick=not need_full)
    if not chunk:
        # 到底: 未在构建且距上次构建>5s → 后台抓新内容追加到池(持续无限滚动, 不清空);
        # 同样按缺量决定 quick/全量(池子见底时缺得多, 优先全量)。
        if not building and time.time() - uni["built_at"] > 5:
            need_full = time.time() - _last_full_build_ts > 90
            kick_build(quick=not need_full)
            building = True
        if not chunk:
            # 最近发生过平台防扎堆暂缓: 构建补充中, 让前端继续轮询, 不误报"已到底"
            if (building or (sess and time.time() - sess.get("defer_ts", 0) < 120)) and not q:
                return {"ok": True, "items": [], "building": True, "gen": uni["gen"],
                        "source": uni["source"], "offline": uni["offline"],
                        "ai": uni["ai"], "mode": uni["mode"], "account": uni["account"],
                        "partitions": uni.get("partitions", "全部"),
                        "offset": offset}
            # 真到底(抓取后仍无新内容) → 已到底, 不再无限 building
            return {"items": [], "building": False, "gen": uni["gen"],
                    "source": uni["source"], "offline": uni["offline"],
                    "ai": uni["ai"], "mode": uni["mode"], "account": uni["account"],
                    "partitions": uni.get("partitions", "全部"),
                    "offset": offset}
    if q:   # --- 搜索路径: 原逻辑(严肃兜底 + AI门禁), 只对当前 chunk 生效 ---
        if chunk and sget("mode") == "严肃":
            chunk = [it for it in chunk if is_serious_ok(it)]
        if chunk and ai_on() and sget("ai_full_scan") == "1":
            cut = MODE_THRESH.get(sget("mode"), 6)
            imin = int(sget("ai_interest_min") or 0)
            dkws = dislike_keywords()
            scores = score_items(chunk, only_cache=True)   # 只读缓存, 秒回不阻塞; 未评分条目由后台评分线程渐进补齐
            kept_chunk, dropped = [], 0
            for it in chunk:
                if not it.get("_range_ok", True):   # 关闭模式: 超出热度前120范围→不展示
                    continue
                sc = scores.get(it["key"])
                if not sc:
                    continue   # 门禁: 必须筛查后才显示, 无分数不展示
                if sc.get("flame", 0) >= cut or sc.get("spam", 0) >= SPAM_CUT:
                    dropped += 1
                    continue
                if (sc.get("interest", 5) or 0) == 0:   # AI语义厌恶命中(评分器强制0分) → 一票剔除
                    dropped += 1
                    continue
                if imin and sc.get("interest", 5) < imin:
                    dropped += 1
                    continue
                if "emo" in sc and (sc.get("emo") or 0) <= -6:
                    dropped += 1
                    continue   # 情绪净值极负(纯负能量/贩卖焦虑)
                if sget("mode") == "严肃" and "density" in sc and (sc.get("density") or 0) < 4:
                    dropped += 1
                    continue   # 严肃模式: 信息密度门槛
                if "facts" in sc and not sc.get("facts") and _is_newsy(it) and (sc.get("density") or 0) < 6:
                    dropped += 1
                    continue   # 资讯无事实锚点+低密度 → 纯情绪输出
                if dkws and any(k in (it.get("title", "") + sc.get("topic", "") +
                                      it.get("desc", "")) for k in dkws):
                    dropped += 1   # 标题/主题字面命中厌恶词(未拆词, 防误伤普通游戏)
                    continue
                it["reason"] = sc.get("reason", "") or ""
                it["topic"] = sc.get("topic", "") or ""
                it["interest"] = sc.get("interest", 5)
                kept_chunk.append(it)
            if dropped:
                bump_stats(filtered=dropped)
                _funnel_bump("ai_gate", dropped)
            chunk = kept_chunk
        # 门禁后 chunk 可能被清空: 开启全扫但评分未就绪 → 让前端稍后重拉, 不误判"已到底"
        if not chunk and ai_on() and sget("ai_full_scan") == "1":
            return {"ok": True, "items": [], "building": True, "gen": uni["gen"],
                    "source": uni["source"], "offline": uni["offline"],
                    "ai": uni["ai"], "mode": uni["mode"], "account": uni["account"],
                    "partitions": uni.get("partitions", "全部"),
                    "offset": offset}
    else:   # --- 信息流路径: 门禁已在套餐规划内完成; 空规划=评分未就绪 → 稍后重拉 ---
        if not chunk and ai_on() and sget("ai_full_scan") == "1":
            return {"ok": True, "items": [], "building": True, "gen": uni["gen"],
                    "source": uni["source"], "offline": uni["offline"],
                    "ai": uni["ai"], "mode": uni["mode"], "account": uni["account"],
                    "partitions": uni.get("partitions", "全部"),
                    "offset": offset}
        chunk = [dict(it, reason=_slot_reason(it)) for it in chunk]   # 槽位理由前缀(可解释性)
    # 本页槽位统计(供前端"套餐"行显示)
    slots_info = {}
    for it in chunk:
        s = it.get("_slot")
        if s:
            slots_info[s] = slots_info.get(s, 0) + 1
    now_ts = int(time.time())
    # v0.3.1.31: 尾部这一整段(展示登记 + shown 回写 + 刚才那条 + 会话分类)原本各自
    # 提交一次 = 4 次 fsync(~5ms)。圈进 _batch_commit() 后只落 1 次磁盘同步。
    #
    # 这一整段都是"读完正文之后的记账", 必须尽力而为: 它失败绝不能让已经取好的这一页
    # 跟着失败。实测(池子在后台重建时)这里的写语句会撞上 SQLite 的写锁, 等满
    # busy_timeout 10s 后抛 "database is locked"; 异常一路上抛到 do_GET, 请求线程直接
    # 死掉、连接被掐断 —— 客户端拿不到任何响应, 界面就一直停在「正在准备内容…」,
    # 也就是用户报的"明明该预加载好, 却还要等"。
    #
    # 而且光包住异常还不够: 撞上写锁时请求仍会干等最多 10 秒才算完, 用户看到的就是
    # "滑到底还是转圈"。所以这一段单独把 busy_timeout 压到 400ms —— 拿不到写锁就
    # 立刻放弃本次记账, 把这一页原样发出去。丢一次记账只是这一页的曝光/去重少记一笔,
    # 下一页就会补上; 让用户等十秒去换一次记账, 代价完全不成比例。
    with _batch_commit():
        _conn = None
        try:
            _conn = db()
            _conn.execute("PRAGMA busy_timeout=400")   # 只作用于这条线程的连接, 下面 finally 还原
            if chunk and not q:
                record_feed_presentations(chunk, request_id or uuid.uuid4().hex, now_ts)
            # _db_lock 也要限时拿: 池子重建那一大笔写入会长时间占着它, 而 `with` 是无期限等待 ——
            # 这就成了"滑到底转圈"的另一半原因。拿不到就当这次记账没做成, 直接往下返回。
            if not _db_lock.acquire(timeout=0.4):
                raise RuntimeError("等 _db_lock 超时(后台写入占用中)")
            try:
                db().executemany("INSERT OR REPLACE INTO shown(key, ts) VALUES(?,?)",
                                 [(it["key"], now_ts) for it in chunk if it.get("key")])
                db().execute("DELETE FROM shown WHERE ts < ?", (now_ts - 3 * 86400,))
                _commit()
            finally:
                _db_lock.release()
            if chunk:
                set_last_feed(chunk[-1])   # “刚才那条”指代目标
                try:   # 跨会话防重复: 记住本次会话末尾5条的一级分类(下次启动前5条避开)
                    cats = [_cat_key(it) for it in chunk[-5:]]
                    sset("last_types", json.dumps(cats, ensure_ascii=False))
                except Exception:
                    pass
        except Exception as e:
            # 回滚掉半截事务, 否则这条线程的连接会把写锁一直攥着, 拖垮后面的请求
            try:
                db().rollback()
            except Exception:
                pass
            print("[feed] 记账失败已跳过(本页照常返回): %s: %s"
                  % (type(e).__name__, str(e)[:120]), flush=True)
        finally:
            if _conn is not None:
                try:
                    _conn.execute("PRAGMA busy_timeout=10000")
                except Exception:
                    pass
    next_offset = consumed_end
    # 实际吐出的平台配比(滚动窗口 200 条): 档位说 5% 就应该是 5% 上下, 供状态接口当场核对
    if chunk and not q:
        with _mix_lock:
            for it in chunk:
                _mix_recent.append(it.get("platform") or "B站")
    return {"ok": True, "items": chunk, "building": building, "gen": uni["gen"],
            "source": uni["source"], "offline": uni["offline"],
            "ai": uni["ai"], "mode": uni["mode"], "account": uni["account"],
            "partitions": uni.get("partitions", "全部"),
            "slots": slots_info, "offset": next_offset, "next_offset": next_offset,
            "has_more": next_offset < len(acts) or building}


def current_partition_names():
    """当前启用分区名(用于界面显示, 杜绝"以为切了实际没切"的困惑)。"""
    enabled = enabled_source_ids()
    if not _subset_mode(enabled):
        return "全部"
    names = [_ID2NAME[i] for i in sorted(enabled) if i in _ID2NAME]
    return "仅" + "、".join(names) if names else "全部"


# ---------------------------------------------------------------- HTTP 服务

def read_json_body(handler):
    n = int(handler.headers.get("Content-Length", 0))
    return json.loads(handler.rfile.read(n).decode("utf-8") or "{}")


def _page_lease_shutdown_check(generation):
    global _PAGE_LEASE_GENERATION
    with _PAGE_LEASE_LOCK:
        if generation != _PAGE_LEASE_GENERATION or _PAGE_LEASES or _SHUTDOWN_REQUESTED.is_set():
            return
        _SHUTDOWN_REQUESTED.set()
        server = _SERVER
    float_ball_stop()   # 页面已全部离开: 先收悬浮球(防孤儿进程), 再关停服务
    if server:
        server.shutdown()


def _lease_watchdog():
    """兜底看门狗: 页面异常退出(浏览器崩溃/直接杀进程)发不出 leaving, 租约会永久滞留,
    服务和悬浮球就都不退。连续两轮(间隔5s)所有租约的存活心跳都超时(宽限150s,
    兼容后台隐藏页定时器被节流到~1分钟) → 判定页面已消失: 先收球, 再走同一关停出口。"""
    global _PAGE_LEASE_GENERATION
    stale_rounds = 0
    while not _SHUTDOWN_REQUESTED.is_set():
        time.sleep(5)
        now = time.time()
        with _PAGE_LEASE_LOCK:
            if _SHUTDOWN_REQUESTED.is_set():
                return
            expired = bool(_PAGE_LEASES) and all(
                now - _PAGE_LEASE_LAST.get(t, 0) > PAGE_LEASE_EXPIRE_SECONDS
                for t in _PAGE_LEASES)
        stale_rounds = stale_rounds + 1 if expired else 0
        if stale_rounds < 2:
            continue   # 复检一轮: 唤醒/节流恢复时页面心跳会立刻续上
        with _PAGE_LEASE_LOCK:
            if _SHUTDOWN_REQUESTED.is_set() or any(
                    now - _PAGE_LEASE_LAST.get(t, 0) <= PAGE_LEASE_EXPIRE_SECONDS
                    for t in _PAGE_LEASES):
                continue   # 复检: 期间有页面回来
            _PAGE_LEASES.clear()
            _PAGE_LEASE_LAST.clear()
            _PAGE_LEASE_GENERATION += 1
            generation = _PAGE_LEASE_GENERATION
            server = _SERVER
        float_ball_stop()
        if server:
            server.shutdown()
        return


def page_lease_alive(token):
    with _PAGE_LEASE_LOCK:
        if _SHUTDOWN_REQUESTED.is_set():
            return False
        _PAGE_LEASES.add(token)
        _PAGE_LEASE_LAST[token] = time.time()
    return True


def page_lease_leaving(token):
    global _PAGE_LEASE_GENERATION
    with _PAGE_LEASE_LOCK:
        if _SHUTDOWN_REQUESTED.is_set():
            return False
        _PAGE_LEASES.discard(token)
        _PAGE_LEASE_LAST.pop(token, None)
        _PAGE_LEASE_GENERATION += 1
        generation = _PAGE_LEASE_GENERATION
    timer = threading.Timer(PAGE_LEASE_GRACE_SECONDS, _page_lease_shutdown_check, args=(generation,))
    timer.daemon = True
    timer.start()
    return True


def ui_version():
    """前端文件指纹(内容hash): 前端据此检测服务端已更新并自动刷新页面。"""
    import hashlib
    h = hashlib.md5()
    for name in ("index.html", "app.js", "style.css"):
        try:
            with open(web_path(name), "rb") as f:
                h.update(f.read())
        except Exception:
            pass
    return h.hexdigest()[:12]


# 「定位悬浮球」的一次性信号: 前端点按钮 -> 服务端计数 +1 -> 悬浮球心跳打
# /api/ball_recall 把 n 读回去比对 -> 变了就执行定位。
# 用内存标志而不是文件: 它只在同一次运行里有意义, 进程重启就该忘掉。
#
# !!! 这个计数只活在 **HTTP 服务进程** 里 !!!
# 悬浮球是 float_ball_spawn() 用 subprocess 拉起来的**独立进程**(--float-ball),
# 它 import 本模块时会重新执行这一行、拿到自己的 _BALL_RECALL = {"n": 0}。
# 所以球那边**绝不能**直接读 _BALL_RECALL 来判断有没有新信号 —— 它自己的计数
# 永远是 0, 而服务端的计数它看不见, 比出来的结果永远是「没有新信号」,
# 按钮点了没反应就是这个原因。球只能从 /api/ball_recall 的**响应体**里取 n。
#
# n   = 累计请求次数(前端每点一次 +1)
# ack = 球已经执行到的 n(球跑完 recall_ball 后回调 /api/ball_recall_ack 回执)
# 前端拿 ack >= n 判定「球确实响应了」, 从而能明确区分「已定位」和「球没响应」——
# 否则用户只能对着一个没有反馈的按钮猜, 这正是「点了没反应」这类报障的来源。
_BALL_RECALL = {"n": 0, "ack": 0}


def ball_recall_request():
    """请求把球拉回屏幕右侧(前端「定位悬浮球」按钮调用)。"""
    _BALL_RECALL["n"] += 1
    return _BALL_RECALL["n"]


def ball_recall_ack(n):
    """悬浮球执行完定位后的回执(球进程调用), 供前端确认「球真的动了」。"""
    try:
        _BALL_RECALL["ack"] = max(_BALL_RECALL["ack"], int(n))
    except (TypeError, ValueError):
        pass
    return _BALL_RECALL["ack"]


def item_by_key(key):
    row = db().execute("SELECT json FROM items WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def items_by_keys(keys):
    """v0.3.1.31: 批量版 item_by_key —— 一次 IN 取回多条, 返回 {key: item}。
    调用方原本都是 `for k in ks: item_by_key(k)`(历史页 200 条 = 200 次往返)。"""
    out = {}
    ks = [k for k in keys if k]
    for i in range(0, len(ks), _SQL_CHUNK):
        part = ks[i:i + _SQL_CHUNK]
        ph = ",".join("?" for _ in part)
        try:
            rows = db().execute("SELECT key,json FROM items WHERE key IN (" + ph + ")", part).fetchall()
        except Exception:
            continue
        for k, j in rows:
            try:
                out[k] = json.loads(j)
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- AI 悬浮窗(仅电脑版)

_BALL_LOCK = threading.Lock()
_BALL_PROC = None   # 悬浮球子进程(独立进程承载 tkinter, 避免在服务线程里跑 GUI)
_BALL_JOB = None    # Windows Job 句柄(kill-on-close): 服务进程无论怎么死, 球都会被系统连带回收

_BALL_TRANSPARENT = "#010101"   # 悬浮球画布透明键色(近黑, 避开真实配色)
_BALL_ACCENT = "#fb7299"        # 默认强调色(主界面切换强调色时会推送覆盖)

# 悬浮球窗口边长(逻辑px)。球体装甲本身只占 0.54*BALL, 剩下的边距是留给**外发光**和
# **全息环阵**的 —— 这两样都得画在球体外圈, 窗口给不够就只能贴着边裁掉, 看着像被切了一刀。
# 窗口整体是颜色键控透明的, 透明区域的鼠标事件会穿透到下层(Windows 分层窗口的行为),
# 所以窗口比球大不会挡住桌面点击。实测 112 逻辑px(@150% = 168 物理px)在桌面上的观感合适。
_BALL_SIZE = 112


def _ball_pos_path():
    return os.path.join(DATA_DIR, "float_ball.json")


def float_ball_env():
    """悬浮窗运行环境体检: 返回 {ok, reason, hint, python}。
    分开报告原因, 免得「环境不支持」这种笼统提示让人查不出到底是系统不对还是 Python 缺库。"""
    py = "%s (%s)" % (sys.executable, sys.version.split()[0])
    if os.name != "nt":
        return {"ok": False, "python": py,
                "reason": "当前系统不是 Windows（悬浮窗依赖 Windows 桌面窗口）",
                "hint": "悬浮窗只在 Windows 电脑版可用。"}
    try:
        import tkinter   # noqa: F401  仅探测可用性, 不建窗口
    except Exception as e:
        return {"ok": False, "python": py,
                "reason": "当前 Python 环境缺少 tkinter 图形库（%s）" % type(e).__name__,
                "hint": "启动本程序的 Python 没有自带 tcl/tk。请改用 python.org 官方安装包"
                        "（安装时保持勾选 tcl/tk and IDLE），或直接运行打包好的 exe 版本。"}
    return {"ok": True, "python": py, "reason": "", "hint": ""}


def float_ball_supported():
    """悬浮窗仅 Windows 桌面可用(需要 tkinter; Android 的 Chaquopy 没有 tkinter)。"""
    return float_ball_env()["ok"]


def float_ball_running():
    with _BALL_LOCK:
        return bool(_BALL_PROC and _BALL_PROC.poll() is None)


_BALL_ERR = {"msg": ""}   # 最近一次拉起悬浮球失败的原因(供界面直接显示)


def float_ball_last_error():
    """最近一次悬浮球启动失败原因。优先用内存里的记录, 其次读球进程留下的错误日志
    (windowed exe 没有 stdout, 崩溃栈会落在 DATA_DIR/float_ball_err.log)。"""
    if _BALL_ERR["msg"]:
        return _BALL_ERR["msg"]
    try:
        p = os.path.join(DATA_DIR, "float_ball_err.log")
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                txt = f.read().strip()
            if txt:
                last = [l for l in txt.splitlines() if l.strip()][-1]
                return "悬浮球进程启动后异常退出: " + last[:160]
    except Exception:
        pass
    return ""


def float_ball_spawn(port):
    """拉起悬浮球进程(幂等)。frozen 单文件 exe 直接再入自身。返回是否成功。
    失败原因写进 _BALL_ERR, 界面据此给出可操作的提示。"""
    global _BALL_PROC, _BALL_JOB
    _BALL_ERR["msg"] = ""
    try:   # 清掉上次的崩溃日志, 否则会把旧错误当成这次的原因报出来
        _lg = os.path.join(DATA_DIR, "float_ball_err.log")
        if os.path.exists(_lg):
            os.remove(_lg)
    except Exception:
        pass
    env = float_ball_env()
    if not env["ok"]:
        _BALL_ERR["msg"] = env["reason"] + "。" + env["hint"]
        return False
    with _BALL_LOCK:
        if _BALL_PROC and _BALL_PROC.poll() is None:
            return True
        cmd = [sys.executable]
        if not getattr(sys, "frozen", False):
            cmd.append(os.path.abspath(__file__))
        cmd += ["--float-ball", str(port)]
        try:
            _BALL_PROC = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000 if os.name == "nt" else 0)   # CREATE_NO_WINDOW 防闪黑框
        except Exception as e:
            _BALL_PROC = None
            _BALL_ERR["msg"] = "拉起悬浮球进程失败: %s" % str(e)[:120]
            return False
        if os.name == "nt":
            try:   # Job kill-on-close: 服务被强杀/崩溃时, 系统自动连带回收球进程(心跳是慢兜底)
                import ctypes

                class _IO(ctypes.Structure):
                    _fields_ = [(n, ctypes.c_int64) for n in
                                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

                class _LIM(ctypes.Structure):
                    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                                ("PerJobUserTimeLimit", ctypes.c_int64),
                                ("LimitFlags", ctypes.c_uint32),
                                ("MinimumWorkingSetSize", ctypes.c_size_t),
                                ("MaximumWorkingSetSize", ctypes.c_size_t),
                                ("ActiveProcessLimit", ctypes.c_uint32),
                                ("Affinity", ctypes.c_size_t),
                                ("PriorityClass", ctypes.c_uint32),
                                ("SchedulingClass", ctypes.c_uint32),
                                ("IoCounters", _IO)]

                k32 = ctypes.windll.kernel32
                from ctypes import wintypes
                k32.CreateJobObjectW.restype = wintypes.HANDLE
                k32.CreateJobObjectW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
                k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                        ctypes.c_void_p, ctypes.c_uint32]
                k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
                if not _BALL_JOB:
                    _BALL_JOB = k32.CreateJobObjectW(None, None)
                    lim = _LIM()
                    lim.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    k32.SetInformationJobObject(_BALL_JOB, 9, ctypes.byref(lim), ctypes.sizeof(lim))
                k32.AssignProcessToJobObject(_BALL_JOB, int(_BALL_PROC._handle))
            except Exception:
                pass   # 挂不上 Job 也不影响主流程: 还有心跳自毁兜底
        return True


def float_ball_stop():
    """终止悬浮球进程(若在跑)。"""
    global _BALL_PROC
    with _BALL_LOCK:
        p, _BALL_PROC = _BALL_PROC, None
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def _kill_stale_ball():
    """清理上次未正常退出遗留的孤儿悬浮球: 按 float_ball.json 里的 pid 定位。
    tasklist 校验进程映像名(必须是 python 或本应用 exe), 防止 pid 被复用时误杀无辜进程。"""
    if os.name != "nt":
        return
    try:
        with open(_ball_pos_path(), encoding="utf-8") as f:
            pid = int(json.load(f).get("pid") or 0)
    except Exception:
        return
    if pid <= 0 or pid == os.getpid():
        return
    try:
        flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
        # 注意: 中文 Windows 的 tasklist 输出是 GBK, 不是 UTF-8。若用 text=True 会让
        # subprocess 按 UTF-8 解码而在读线程里抛 UnicodeDecodeError(表现为启动日志里
        # 一段 _readerthread 异常), 结果 tasklist 永远拿不到结果 → 孤儿球清不掉。
        # 所以这里拿 bytes 自己按 OEM 代码页解, 解不开就退 UTF-8 且忽略坏字节。
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=10, creationflags=flags)
        raw = r.stdout or b""
        for enc in ("gbk", "utf-8"):
            try:
                out = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            out = raw.decode("utf-8", "replace")
        img = out.strip().split(",")[0].strip('"') if out.strip() else ""
        if "python" not in img.lower() and "洞鉴虚明" not in img:
            return   # pid 已被其他程序复用 → 不动它
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       timeout=10, creationflags=flags)
    except Exception:
        pass


def _server_port():
    try:
        return _SERVER.server_address[1]
    except Exception:
        return 8000


# 前端图标贴图(web/icons/*.svg 与 web/icons/png/*.png)的静态白名单: 只放行这两种形状的路径, 杜绝目录穿越
_ICON_PATH_RE = re.compile(r"^/icons/(?:png/[a-z]+-(?:24|48|96|192)\.png|[a-z]+\.svg)$")


def _assist_usage_snapshot():
    """本次请求累计的 token 用量与花费(按设置里的单价)。"""
    try:
        p_in = float(sget("price_in") or 0)
        p_out = float(sget("price_out") or 0)
    except Exception:
        p_in = p_out = 0.0
    it = getattr(_COST_TL, "in_tokens", 0)
    ot = getattr(_COST_TL, "out_tokens", 0)
    return {"calls": getattr(_COST_TL, "calls", 0), "in_tokens": it, "out_tokens": ot,
            "cost": round(it / 1e6 * p_in + ot / 1e6 * p_out, 4)}


class Handler(BaseHTTPRequestHandler):
    # B站页面里的脚本跑在 https://*.bilibili.com 源上, 读 127.0.0.1 需要显式放行:
    #   · bilibili-sr.user.js 轮询 /api/ui_settings 做超分热更新;
    #   · bilibili-cfilter.user.js 打 /api/bili/flags 问评论作者的账号画像;
    #   · bilibili-cfilter.user.js 用 navigator.sendBeacon 往 /api/bili/names 上报
    #     (评论里见到的昵称, mid) 对。
    # 只有这几个"不含任何凭据、只回开关键/布尔标记"的端点对 B 站源回显 ACAO,
    # 其余端点(含 ai_key 的)一律不给。
    _CORS_PATHS = ("/api/ui_settings", "/api/bili/flags", "/api/bili/dmflags",
                   "/api/bili/dmmid", "/api/bili/names", "/api/textfilter")

    def _cors_headers(self):
        """对 B站源回显 ACAO。预检(OPTIONS)与实际请求(POST)必须回同一个值,
        否则浏览器会把请求整体判失败。"""
        if getattr(self, "path", "").split("?")[0] not in self._CORS_PATHS:
            return
        try:
            origin = self.headers.get("Origin") or ""
            host = origin.split("://", 1)[-1].split("/", 1)[0] if "://" in origin else ""
            if host == "bilibili.com" or host.endswith(".bilibili.com"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        except Exception:
            pass

    def do_OPTIONS(self):
        """CORS 预检(v0.3.1.32 新增 —— 这个缺失导致弹幕黑名单一直是空的)。

        bilibili-cfilter 从 https://www.bilibili.com 用 navigator.sendBeacon 往
        http://127.0.0.1 上报 (昵称, mid) 对。Blob 的 type 是 application/json,
        **不在 CORS 安全列表里** → 浏览器先发 OPTIONS 预检。此前 Handler 没有
        do_OPTIONS, BaseHTTPRequestHandler 直接回 501, 预检失败, 整个上报被静默丢掉
        (sendBeacon 只返回"已入队"=true, 页面完全察觉不到)。
        实测后果: name_mid 表恒为 0 行 → dm_blocked_hashes() 恒返回 []
        → 「屏蔽黑名单内用户…弹幕」这条规则从未真正生效过。
        """
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        # Chrome 的 Private Network Access: 公网源(bilibili.com)访问本机(127.0.0.1)
        # 会额外索要这个头, 否则预检过了请求仍被拦。
        if (self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # v0.3.1.31: 客户端(浏览器/curl 超时)提前断开时 socketserver 会打一整段
            # traceback —— 这不是错误, 是正常现象, 静默忽略。
            pass

    def send_file(self, name, ctype):
        try:
            with open(web_path(name), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 无验证器时 no-cache = 每次都重新获取, 防止浏览器启发式缓存旧前端文件
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path in ("/", "/index.html"):
            return self.send_file("index.html", "text/html")
        if path == "/favicon.ico":
            return self.send_file("favicon.ico", "image/x-icon")
        if path == "/app.js":
            return self.send_file("app.js", "application/javascript")
        if path == "/qrcode.js":
            return self.send_file("qrcode.js", "application/javascript")
        if path == "/style.css":
            return self.send_file("style.css", "text/css")
        if _ICON_PATH_RE.match(path):
            return self.send_file(path[1:], "image/svg+xml" if path.endswith(".svg") else "image/png")
        if path == "/api/update/check":
            return self.send_json(update_check())
        if path == "/api/update/progress":
            return self.send_json(update_progress())
        if path == "/api/search":
            q = qs.get("q", [""])[0].strip()
            offset = int(qs.get("offset", ["0"])[0] or 0)
            limit = min(int(qs.get("limit", ["30"])[0] or 30), 50)
            refresh = qs.get("refresh", ["0"])[0] == "1"
            sort = qs.get("sort", ["default"])[0]
            if sort not in SEARCH_SORTS:
                sort = "default"   # 非法值回退综合排序
            return self.send_json(search_all(q, offset, limit, refresh=refresh, sort=sort))
        if path == "/api/feed":
            q = qs.get("q", [""])[0]
            refresh = qs.get("refresh", ["0"])[0] == "1"
            offset = int(qs.get("offset", ["0"])[0] or 0)
            limit = min(int(qs.get("limit", ["30"])[0] or 30), 50)
            request_id = qs.get("rid", [""])[0][:128]
            plat = qs.get("plat", [""])[0][:32]
            if refresh:
                global _uni_token
                # "刷新"= 后台立刻去补一批新的, 同时**照常用当前池把这一屏发回去**。
                # 两个关键点:
                #  ① 不清空池子(旧实现先把 items 清空, 前端只能干等重建, 屏上直接空掉);
                #  ② 用 quick 而不是 full: 全量要 10-30 秒, 刷新应该立刻有反馈。
                #     quick 只取少量源, 且 build_universe 末尾会自动接力一次全量。
                # _uni_token +1 同时作废旧构建、并让 _feed_plan 的缓存 key 失效 → 重新规划顺序。
                with _uni_lock:
                    _uni_token += 1
                # 用户主动刷新 = 明确要求重试贴吧: 解掉 403 冷却(否则要干等 10 分钟)。
                # 60 秒内只放行一次, 连点不会把百度风控招得更狠。
                _tb_allow_retry()
                _fp_clear()   # 指纹拦的是"进池资格", 补池后要失效, 否则新内容进不来;
                              # 去重由 shown 窗口 + 随机页/源 + 新会话已返回集合承担
                kick_build(quick=True, pending=True)
            return self.send_json(serve_chunk(offset, limit, q, request_id=request_id, plat=plat))
        if path == "/api/messages":
            bili_logged_in = bili_is_logged_in()
            bili_unread = bili_unread_messages() if bili_logged_in else None
            tb_state = tb_login_check()
            tb_logged_in = bool(tb_state.get("logged_in"))
            tb_unread = tb_unread_messages() if tb_logged_in else None
            xhh_logged_in = bool(sget("xhh_heybox_id"))
            xhh_unread = xhh_unread_messages() if xhh_logged_in else None
            # seen = 用户上次点开消息页时的未读数快照。前端角标只画 unread - seen(不小于0),
            # 所以点完消息按钮红点会立刻消失, 之后真有新消息才会再冒出来。
            return self.send_json({"seen": msg_seen(),
                                   "bili": {"logged_in": bili_logged_in,
                                             "unread": bili_unread,
                                             "url": "https://message.bilibili.com/"},
                                   "tieba": {"logged_in": tb_logged_in,
                                             "unread": tb_unread,
                                             "url": "https://tieba.baidu.com/"},
                                   "xhh": {"logged_in": xhh_logged_in,
                                           "unread": xhh_unread,
                                           "url": "https://www.xiaoheihe.cn/message/home/comment"}})
        if path.startswith("/api/shell_debug"):
            print("[shell-debug] %s" % path[len("/api/shell_debug?s="):], flush=True)
            return self.send_json({"ok": True})

        if path == "/api/ui_theme":
            # 桌面窗口壳: 页面首次启动(壳内localStorage为空)时从这里恢复主界面主题/强调色
            acc_id = sget("ui_accent_id")
            acc_hex = sget("float_ball_accent")
            theme = sget("ui_theme")
            if theme not in ("light", "dark"):
                theme = "light" if sget("float_ball_light") == "1" else "dark"
            return self.send_json({"theme": theme, "accent_id": acc_id,
                                   "accent_hex": acc_hex if re.match(r"^#[0-9a-fA-F]{6}$", acc_hex or "") else ""})
        
        if path == "/api/ui_version":
            # 悬浮球心跳打这里(兼作「定位悬浮球」通道); 前端也用它检测更新
            return self.send_json({"version": ui_version()})
        if path == "/api/ball_recall":
            # 悬浮球心跳打这里(兼取「定位悬浮球」信号): 球从响应体读 n 与自己记的比对。
            # 前端也打这里等 ack, 判断球到底响应了没有。
            return self.send_json({"ok": True, "n": _BALL_RECALL["n"],
                                   "ack": _BALL_RECALL["ack"]})
        if path == "/api/ball_recall_go":
            return self.send_json({"ok": True, "n": ball_recall_request()})
        if path == "/api/ball_recall_ack":
            return self.send_json({"ok": True,
                                   "ack": ball_recall_ack(qs.get("n", ["0"])[0])})
        if path == "/api/status":
            last = int(sget("fetch_last") or 0)
            st = day_stats()
            tb_bars_n = len(tb_users())
            tb_login = tb_login_check()
            tb_last = int(sget("tb_last_fetch") or 0)
            # "正常"= 最近一小时内真的抓过; 只判"抓过没有"会把一小时前的陈旧数据也报成正常。
            bili_status = ("未抓取" if not last
                           else "正常" if time.time() - last < 3600 else "久未更新")
            with _uni_lock:
                pool_n = len(_universe["items"])
            sources = [{"name": "B站", "status": bili_status,
                        "last_fetch": last, "fetched_today": st["received"],
                        "filtered_today": st["filtered"], "error": sget("fetch_error")}]
            tb_status = ("未配置吧列表" if not tb_bars_n
                         else "正常" if tb_last else "未抓取")
            sources.append({"name": "贴吧", "status": tb_status,
                            "last_fetch": tb_last,
                            "fetched_today": _tb_fetch_total, "filtered_today": 0,
                            "error": sget("tb_error"),
                            # v0.3.1.55: 被百度安全验证拦了 —— 前端据此把这一行做成
                            # "点一下打开验证窗口, 拖个滑块就好", 而不是干等冷却。
                            "need_verify": "安全验证" in (sget("tb_error") or ""),
                            # 浏览器窗口通道是否接通(跨重启也能接回来): 前端优先显示
                            # "窗口通道", 免得通道明明在用、左栏还挂着 403。
                            "browser_channel": bool(_tb_br_channel()),
                            "bars": tb_bars_n,
                            "logged_in": tb_login.get("logged_in", False)})
            xhh_last = int(sget("xhh_last_fetch") or 0)
            sources.append({"name": "小黑盒", "status": "正常" if xhh_last else "未抓取",
                            "last_fetch": xhh_last,
                            "fetched_today": _xhh_fetch_total, "filtered_today": 0,
                            "error": sget("xhh_error") or "",
                            "logged_in": bool(sget("xhh_heybox_id"))})
            # v0.3.1.51: 直播源也进「内容源」状态卡(与左栏按钮 data-st 对应)
            live_last = int(sget("live_last_fetch") or 0)
            with _uni_lock:
                live_n = sum(1 for x in _universe["items"] if x.get("platform") == LIVE_PLATFORM)
            sources.append({"name": LIVE_PLATFORM,
                            "status": "正常" if live_last else "未抓取",
                            "last_fetch": live_last,
                            "fetched_today": _live_fetch_total, "filtered_today": 0,
                            "error": sget("live_error") or "",
                            "items": live_n,
                            "logged_in": bool(account_cookie())})
            return self.send_json({"ai": ai_ok(), "ai_on": ai_on(), "sources": sources,
                                   # 前端用它决定空态是"正在准备内容…"还是"去配来源"
                                   "configured": user_configured(),
                                   "items": pool_n,
                                   "summary": st, "ai_scoring": dict(_score_progress),
                                   "funnel": {"labels": _FUNNEL_LABELS, "counts": funnel_stats()},
                                   # 最近一分钟各站点真实发出的请求数(排查"是不是在刷站")
                                   "net": net_stats(), "mix": mix_stats()})
        if path == "/api/live":
            # 直播页数据(v0.3.1.51): 关注的人正在直播(followed) + 热门推荐(hot)。
            # 60 秒缓存; refresh=1 强刷。接口失败只降级成空列表+error, 不抛给前端。
            try:
                return self.send_json(live_snapshot(refresh=(qs.get("refresh", ["0"])[0] == "1")))
            except Exception as e:
                return self.send_json({"ok": False, "followed": [], "hot": [], "count": 0,
                                       "error": str(e)[:200], "need_login": False})
        if path == "/api/blacklist":
            rows = db().execute("SELECT kind,value FROM blacklist").fetchall()
            return self.send_json({"authors": [r[1] for r in rows if r[0] == "author"],
                                   "keywords": [r[1] for r in rows if r[0] == "keyword"]})
        if path == "/api/history":
            rows = db().execute("SELECT key,ts FROM history ORDER BY ts DESC LIMIT 200").fetchall()
            hmap = items_by_keys([k for k, _t in rows])   # v0.3.1.31: 批量, 原本 200 次往返
            items = [dict(hmap.get(k) or {"key": k, "title": "(已下架)", "platform": "-"},
                          **{"ts": t}) for k, t in rows]
            return self.send_json({"items": items})
        if path == "/api/saved":
            kind = qs.get("kind", ["later"])[0]
            rows = db().execute("SELECT key,ts FROM saved WHERE kind=? ORDER BY ts DESC LIMIT 200", (kind,)).fetchall()
            smap = items_by_keys([k for k, _t in rows])   # v0.3.1.31: 批量, 原本 200 次往返
            items = [dict(smap.get(k) or {"key": k, "title": "(已下架)", "platform": "-"},
                          **{"ts": t}) for k, t in rows]
            return self.send_json({"items": items})
        if path == "/api/conv":
            convs, cur = conv_list()
            return self.send_json({"convs": convs, "cur": cur, "ctx": chat_ctx_get(cur)})
        if path == "/api/chat":
            cid = conv_cur(qs.get("cid", [None])[0])
            rows = db().execute("SELECT role,content FROM chat WHERE cid=? ORDER BY id DESC LIMIT 200",
                                (cid,)).fetchall()
            title = db().execute("SELECT title FROM conv WHERE id=?", (cid,)).fetchone()
            return self.send_json({"messages": [{"role": r[0], "content": r[1]} for r in reversed(rows)],
                                   "cid": cid, "title": title[0] if title else ""})
        if path == "/api/preview":
            bvid = (qs.get("bvid", [""])[0] or "").strip()
            src, err = preview_src(bvid)
            if src:
                return self.send_json({"ok": True, "src": src, "bvid": bvid})
            return self.send_json({"ok": False, "error": err or "无预览地址"})
        if path == "/api/video-proxy":
            bvid = (qs.get("bvid", [""])[0] or "").strip()
            cid = (qs.get("cid", [""])[0] or "").strip()
            c = _preview_cache.get(bvid)
            if not c or str(c.get("cid")) != cid or not c.get("url"):
                self.send_error(404, "预览地址不存在或已过期")
                return
            headers = dict(ACCOUNT_HEADERS)
            rng = self.headers.get("Range")
            if rng:
                headers["Range"] = rng
            try:
                req = urllib.request.Request(c["url"], headers=headers)
                up = urllib.request.urlopen(req, timeout=20)
            except Exception as e:
                self.send_error(502, "预览流获取失败: %s" % str(e)[:100])
                return
            self.send_response(up.status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                v = up.headers.get(h)
                if v:
                    self.send_header(h, v)
            self.end_headers()
            try:
                while True:
                    buf = up.read(65536)
                    if not buf:
                        break
                    self.wfile.write(buf)
            except Exception:
                pass
            finally:
                up.close()
            return
        if path == "/api/avatar":
            face = account_slim().get("face", "")
            if not face:
                self.send_error(404)
                return
            if _avatar_cache["data"] is None or time.time() - _avatar_cache["ts"] > 300:
                try:
                    req = urllib.request.Request(face, headers={
                        "User-Agent": HEADERS["User-Agent"],
                        "Referer": "https://www.bilibili.com/"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        _avatar_cache.update(ts=time.time(), data=resp.read(),
                                             ctype=resp.headers.get("Content-Type", "image/jpeg"))
                except Exception:
                    _avatar_cache.update(ts=time.time(), data=None, ctype="image/jpeg")
            if not _avatar_cache["data"]:
                self.send_error(404)
                return
            body = _avatar_cache["data"]
            self.send_response(200)
            self.send_header("Content-Type", _avatar_cache["ctype"])
            self.send_header("Cache-Control", "max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        if path == "/api/machine":
            keys = ("m_min_views", "m_max_views", "m_max_duration", "m_min_duration", "m_repeat_limit", "m_repeat_cooldown_hours", "m_dedup", "m_blocklist_sync",
                    "m_max_age_months", "ser_max_age_months", "m_seen_hours", "m_emotion",
                    "m_news_fresh", "m_play_related", "xhh_comm", "xhh_topic", "bili_follow_emph", "bili_follow_months",
                    "m_cold_start_n", "cf_blk_on", "cf_min_level",
                    "m_block_mystery_bio", "m_block_lottery", "m_block_empty_space",
                    "m_dm_multicand", "m_filter_speed", "m_dm_on",
                    "ft_on", "ft_video", "ft_dm", "ft_cf", "ft_thresh", "ft_labels")
            enabled = enabled_source_ids()
            blocked = [{"name": r[0], "ts": r[1]} for r in
                       db().execute("SELECT name, ts FROM ablock ORDER BY ts DESC").fetchall()]
            return self.send_json({"settings": {k: sget(k) for k in keys},
                                   "ft": ft_status(),
                                   "source_groups": [{"id": g[0], "name": g[1], "on": g[0] in enabled}
                                                     for g in SOURCE_GROUPS],
                                   "blocked": blocked,
                                   "account_ok": account_slim().get("ok", False)})
        if path == "/api/account":
            return self.send_json(account_slim() | {"has_cookie": bool(account_cookie())})
        if path == "/api/live/settings":
            mn, only = live_filters()
            return self.send_json({"ok": True, "min_viewers": mn, "only_followed": only})
        if path == "/api/tieba":
            login = tb_login_check()
            return self.send_json({
                "ok": True, "bars": tb_users(),
                "tb_weight": tb_weight(), "bili_weight": bili_weight(),
                "allow_help": sget("tb_allow_help") != "0",
                "platforms": [{"name": n, "key": k, "default": d,
                               "weight": platform_weights().get(n, d)}
                              for n, k, d in PLATFORMS],
                "logged_in": login.get("logged_in", False),
                "nickname": login.get("nickname", ""),
                "avatar": login.get("avatar", ""),
                "cookie_set": bool(tb_cookie()),
                "verify_cookie_set": bool(tb_verify_cookie()),
                "verify_status": dict(_CDP_VERIFY),
                # 浏览器通道是否可用(跨应用重启也能接回来): 前端据此显示"窗口通道接管中",
                # 而不是只看 tb_error —— 否则验证过了界面还在报 403, 用户以为没生效。
                "browser_channel": bool(_tb_br_channel()),
                "browser_port": int(_CDP_PORT or 0),
                "last_fetch": int(sget("tb_last_fetch") or 0),
                "error": sget("tb_error") or "",
                "fetch_total": _tb_fetch_total,
                "login_status": dict(_CDP_LOGIN),
            })
        if path == "/api/tieba/verify/start":
            return self.send_json(tb_verify_start())
        if path == "/api/tieba/verify/poll":
            return self.send_json(tb_verify_poll())
        if path == "/api/tieba/verify/stop":
            return self.send_json(tb_verify_stop())
        if path == "/api/tieba/login/poll":
            return self.send_json(tb_login_poll())
        if path == "/api/tieba/browse":
            # 浏览器辅助采集: 打开(或复用)登录窗口并导航到指定吧列表页
            kw = (qs.get("kw", [""])[0] or "").strip()
            if not kw:
                return self.send_json({"ok": False, "error": "缺少吧名参数"})
            r = tb_login_start()
            if not r.get("ok"):
                return self.send_json(r)
            # 导航到吧列表页(浏览器无风控, 能加载)
            with local_open(
                    "http://127.0.0.1:%d/json/list" % _CDP_PORT, timeout=3) as resp:
                targets = json.loads(resp.read().decode())
            page = next((t for t in targets if t.get("type") == "page"), None)
            if not page:
                return self.send_json({"ok": False, "error": "窗口未就绪"})
            # 注入已有登录cookie(若有), 让贴吧页面正常渲染帖子列表
            cookie = tb_cookie()
            if cookie:
                for part in cookie.split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        try:
                            _cdp_set_cookie(page["webSocketDebuggerUrl"], k, v,
                                            "https://tieba.baidu.com/")
                        except Exception:
                            pass
            _cdp_eval(page["webSocketDebuggerUrl"],
                      "location.href='https://tieba.baidu.com/f?kw=%s&ie=utf-8&pn=0'; true"
                      % urllib.parse.quote(kw))
            return self.send_json({"ok": True, "message": "已打开贴吧列表页, 稍后点击「抓取此页」"})
        if path == "/api/tieba/mylike":
            bars, err = tb_mylike()
            if bars:
                return self.send_json({"ok": True, "bars": bars})
            return self.send_json({"ok": False, "error": err or "获取失败"})
        if path == "/api/tieba/avatar":
            # 贴吧头像本地代理: 规避图床防盗链(与B站 /api/avatar 同模式)
            url = sget("tb_avatar") or ""
            if not url:
                self.send_error(404)
                return
            if _tb_avatar_cache["data"] is None or time.time() - _tb_avatar_cache["ts"] > 300:
                try:
                    req = urllib.request.Request(url, headers={
                        "User-Agent": HEADERS["User-Agent"],
                        "Referer": "https://tieba.baidu.com/"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        _tb_avatar_cache.update(ts=time.time(), data=resp.read(),
                                                ctype=resp.headers.get("Content-Type", "image/jpeg"))
                except Exception:
                    _tb_avatar_cache.update(ts=time.time(), data=None, ctype="image/jpeg")
            if not _tb_avatar_cache["data"]:
                self.send_error(404)
                return
            body = _tb_avatar_cache["data"]
            self.send_response(200)
            self.send_header("Content-Type", _tb_avatar_cache["ctype"])
            self.send_header("Cache-Control", "max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/xhh/qr":
            # 小黑盒扫码登录: 生成二维码(app=web=网页端通道)。用固定tokenid(登录态绑定发码方)。
            try:
                # 若无持久化tokenid则生成并保存(生成与轮询必须同一tokenid, 否则登录态对不上)
                if not sget("xhh_qr_tokenid"):
                    sset("xhh_qr_tokenid", xhh_token())
                qtok = sget("xhh_qr_tokenid")
                d, err = xhh_get("/account/get_qrcode_url/", {"app": "web"}, token=qtok)
                if err or not d or d.get("status") != "ok":
                    return self.send_json({"ok": False, "error": err or d or "生成失败"})
                url = (d.get("result") or {}).get("qr_url", "")
                import urllib.parse as _up
                code = _up.parse_qs(_up.urlparse(url).query).get("qr", [""])[0]
                sset("xhh_qr_code", code)
                return self.send_json({"ok": True, "url": url, "code": code,
                                       "expire": (d.get("result") or {}).get("expire", 120)})
            except Exception as ex:
                return self.send_json({"ok": False, "error": str(ex)[:100]})
        if path == "/api/xhh/qr/poll":
            code = sget("xhh_qr_code")
            if not code:
                return self.send_json({"status": "error", "message": "请先生成二维码"})
            # 复用生成时的固定tokenid(登录态绑定发码方, 轮询须一致)
            qtok = sget("xhh_qr_tokenid") or xhh_token()
            d, err = xhh_get("/account/qr_state/", {"qr": code, "app": "web"}, token=qtok)
            if err:
                return self.send_json({"status": "waiting", "message": "轮询: %s" % err[:60]})
            res = d.get("result") or {}
            err_st = res.get("error", "wait")
            err_msg = res.get("error_msg", "")
            # 状态细分(实测: wait=未扫码, ready=已扫码待确认, ok=确认成功, cancel=过期/取消)
            if err_st == "wait":
                return self.send_json({"status": "waiting", "message": "等待扫码…"})
            if err_st == "ready":
                return self.send_json({"status": "waiting", "message": "已扫码，请在手机上点「登录」确认"})
            if err_st == "ok":
                # 确认成功! 实测 result 直接带 nickname/avatar/heyboxid/account_detail.userid
                # 网页版登录不返回 pkey → 用 heybox_id(用户ID)作为"已登录"判据
                pkey, hid, nick, avatar = xhh_parse_login(res)
                sset("xhh_cookie", qtok)
                sset("xhh_pkey", "")
                sset("xhh_heybox_id", str(hid) if hid else "")
                sset("xhh_user", nick or "已登录")
                sset("xhh_avatar", avatar)
                return self.send_json({"status": "ok", "message": "小黑盒登录成功",
                                       "nickname": nick or "已登录", "avatar": avatar})
            if err_st in ("cancel", "expired") or "过期" in err_msg or "取消" in err_msg:
                return self.send_json({"status": "expired", "message": "二维码已过期，请点「生成登录二维码」重新扫码"})
            # 其余未知状态: 透传出真实进度(不吞状态)
            return self.send_json({"status": "waiting", "message": err_msg or err_st})
        if path == "/api/xhh":
            return self.send_json({"ok": True, "logged_in": bool(sget("xhh_heybox_id")),
                                   "nickname": sget("xhh_user") or "",
                                   "avatar": sget("xhh_avatar") or ""})
        if path == "/api/xhh/avatar":
            # 小黑盒头像本地代理: 规避防盗链
            url = sget("xhh_avatar") or ""
            if not url:
                self.send_error(404)
                return
            if _xhh_avatar_cache["data"] is None or time.time() - _xhh_avatar_cache["ts"] > 300:
                try:
                    req = urllib.request.Request(url, headers={
                        "User-Agent": HEADERS["User-Agent"],
                        "Referer": "https://www.xiaoheihe.cn/"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        _xhh_avatar_cache.update(ts=time.time(), data=resp.read(),
                                                 ctype=resp.headers.get("Content-Type", "image/jpeg"))
                except Exception:
                    _xhh_avatar_cache.update(ts=time.time(), data=None, ctype="image/jpeg")
            if not _xhh_avatar_cache["data"]:
                self.send_error(404)
                return
            body = _xhh_avatar_cache["data"]
            self.send_response(200)
            self.send_header("Content-Type", _xhh_avatar_cache["ctype"])
            self.send_header("Cache-Control", "max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/login/qr/poll":
            key = qs.get("key", [""])[0]
            if not key:
                return self.send_json({"status": "error", "message": "缺少参数"}, 400)
            try:
                return self.send_json(login_qr_poll(key))
            except Exception as e:
                return self.send_json({"status": "error", "message": "轮询失败: %s" % str(e)[:100]})
        if path == "/api/xhh/thread":
            lid = (qs.get("link_id", [""])[0] or "").strip()
            if not lid:
                return self.send_json({"ok": False, "error": "link_id无效"}, 400)
            return self.send_json(xhh_thread_content(lid))
        if path == "/api/tieba/thread":
            tid = (qs.get("tid", [""])[0] or "").strip()
            if not tid.isdigit():
                return self.send_json({"ok": False, "error": "tid无效"}, 400)
            return self.send_json(tieba_thread_content(tid))
        if path == "/api/ui_settings":
            # 内置浏览器标签页里的超分/画质脚本(bilibili-sr.user.js 等)轮询此端点做热更新。
            # 只回 UI 开关键(绝不含 ai_key 等敏感项); CORS 仅对 B 站源回显, 其余网站一律不给。
            return self.send_json({k: sget(k) for k in ("ui_bq", "ui_bq_level", "ui_sr",
                                                        "ui_fg", "ui_sr_level", "ui_sr_scale",
                                                        "ui_ownui")})
        if path == "/api/bili/flags":
            # 「评论筛选 · B站」那两个账号级开关用: 页面脚本(bilibili-cfilter)带着本页
            # 评论作者的 mid 来问"谁是空简介 / 谁是抽奖号"。
            # 只读 author_meta 缓存 + 把未知作者丢进后台限速队列, **绝不联网等待** ——
            # 评论加载的关键路径上不能有网络查询。只回布尔标记, 不含任何凭据。
            mids = [x.strip() for x in (qs.get("mids", [""])[0] or "").split(",") if x.strip()]
            r = bili_author_flags(mids[:60],
                                  want_bio=qs.get("bio", ["1"])[0] != "0",
                                  want_lottery=qs.get("lot", ["1"])[0] != "0",
                                  want_empty=qs.get("nodyn", ["1"])[0] != "0")
            return self.send_json({"ok": True} | r)
        if path == "/api/bili/dmflags":
            # 「弹幕评论筛选」: 页面脚本(bilibili-dmfilter)带着本页弹幕发送者的 midHash
            # 来问"谁该拦"。只读本地缓存(逆推表 + 账号画像), 绝不联网等待 ——
            # 弹幕加载的关键路径上不能有网络查询/长计算。只回布尔标记, 不含任何凭据。
            hs = [x.strip().lower() for x in (qs.get("hashes", [""])[0] or "").split(",") if x.strip()]
            try:
                _mc = int(sget("m_dm_multicand") or 1)
            except Exception:
                _mc = 1
            # v0.3.1.45dsk: 不再收 lvl —— 弹幕侧等级筛选已移除(判等级必须联网查资料,
            # 候选爆炸会把账号打进 -352 风控)。等级只保留评论侧, 见 bilibili-cfilter。
            r = bili_dm_flags(hs,
                              want_bio=qs.get("bio", ["0"])[0] != "0",
                              want_lottery=qs.get("lot", ["0"])[0] != "0",
                              want_empty=qs.get("nodyn", ["0"])[0] != "0",
                              multi=max(0, min(2, _mc)))
            return self.send_json({"ok": True} | r)
        if path == "/api/textfilter/status":
            return self.send_json({"ok": True} | ft_status())
        if path == "/api/bili/dmmid":
            # 弹幕列表点「发送者」→ 逆推 midHash 找主页。点击场景可以等一两秒:
            # 完整逆推(2..10位) + card 接口验真; 多候选时原样回给前端让用户自己挑。
            r = bili_dm_mid(qs.get("hash", [""])[0])
            return self.send_json({"ok": True} | r)
        if path == "/api/intro":
            # 首次引导的入口判定。放后端而不是 localStorage：清缓存/换内核不该让引导重弹，
            # 真正决定"要不要引导"的是本机有没有配上来源。
            return self.send_json({"ok": True, "done": sget("intro_done") == "1",
                                   "configured": user_configured(),
                                   "show": intro_needed()})
        if path == "/api/settings":
            return self.send_json({k: sget(k) for k in ("ai_base", "ai_key", "ai_model", "mode",
                                                        "prefs_note", "dislike_note",
                                                        "price_in", "price_out", "ai_full_scan",
                                                        "ai_interest_min", "ai_batch", "ai_cache_hours",
                                                        "ai_enabled", "disclaimer_version",
                                                        "ui_bq", "ui_bq_level", "ui_sr",
                                                        "ui_fg", "ui_sr_level", "ui_sr_scale",
                                                        "ui_ownui", "ui_new_window")}
                                  | {"ai_ok": ai_ok(), "ai_on": ai_on()})
        if path == "/api/floatball":
            env = float_ball_env()
            return self.send_json({"ok": True, "supported": env["ok"],
                                   "reason": env["reason"], "hint": env["hint"],
                                   "python": env["python"],
                                   "enabled": sget("float_ball") == "1",
                                   "running": float_ball_running(),
                                   "spawn_error": float_ball_last_error(),
                                   "accent": sget("float_ball_accent") or "#fb7299",
                                   "light": sget("float_ball_light") == "1",
                                   "prefs": _floatball_prefs()})
        if path == "/api/rebut/saved":
            # 对线收藏夹: q 非空时按原话或正文模糊搜索
            return self.send_json({"ok": True,
                                   "items": rebut_saved_list((qs.get("q", [""])[0] or ""))})
        if path == "/api/personas":
            # 自定义人设预设: 内置档位 + 用户自建
            return self.send_json({"ok": True, "builtin": list(_PERSONA_TIER),
                                   "fields": [{"key": k, "label": lbl, "limit": lim}
                                              for k, lbl, lim in _PERSONA_FIELD],
                                   "max": _PERSONA_MAX, "items": persona_presets()})
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = read_json_body(self)
        except Exception:
            return self.send_json({"ok": False, "error": "请求体不是合法JSON"}, 400)

        if path == "/api/shutdown":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self.send_json({"ok": False, "error": "仅允许本机请求"}, 403)
            action = body.get("action")
            token = body.get("token")
            if action not in ("alive", "leaving") or not isinstance(token, str) or not (16 <= len(token) <= 128):
                return self.send_json({"ok": False, "error": "无效的页面租约请求"}, 400)
            ok = page_lease_alive(token) if action == "alive" else page_lease_leaving(token)
            return self.send_json({"ok": ok, "grace_seconds": PAGE_LEASE_GRACE_SECONDS})

        if path == "/api/update/download":
            return self.send_json(update_download())

        if path == "/api/textfilter":
            # v0.3.1.43dsk 快速文本分析: 页面脚本(弹幕/评论筛选)把本页的**文本**送过来,
            # 问"这几条该不该拦"。纯本地 fastText 前向, 零联网、零 token。
            # 关键路径约束: 这是弹幕/评论加载路径上的一次同步等待, 必须快 ——
            # 纯 Python 运行时实测 5,000~9,000 条/秒(带缓存更快), 500 条 ≈ 50ms。
            # 未开启/模型缺失 → on=False, 前端原样放行(绝不因为筛选功能坏掉而卡住页面)。
            kind = (body.get("kind") or "dm")
            if kind not in ("video", "dm", "cf"):
                kind = "dm"
            texts = body.get("texts")
            if not isinstance(texts, list):
                texts = []
            texts = [str(t)[:_FT_MAXLEN] for t in texts[:1200]]
            if not ft_enabled():
                return self.send_json({"ok": True, "on": False, "why": [""] * len(texts)})
            whys = ft_scan(texts, kind)
            return self.send_json({"ok": True, "on": True, "why": whys})

        if path == "/api/textfilter/probe":
            # v0.3.1.43dsk 设置页「试一下」: 输入一句话, 看模型判成什么、多少概率。
            # 同源(127.0.0.1 设置页)调用, 不需要 CORS。
            try:
                return self.send_json({"ok": True, **ft_probe(body.get("text") or "")})
            except Exception as e:
                return self.send_json({"ok": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:100])})

        if path == "/api/bili/names":
            # bilibili-cfilter 从评论响应里顺手收集的 (作者昵称, mid) 对 → name_mid 索引。
            # v0.3.1.32: 页面**必须**拿到 CORS 回显, 否则预检/实际请求被浏览器拦掉,
            # 上报静默丢失(以前这里写着"不需要回显", 正是这个误判让 name_mid 恒为空)。
            # cfilter 已改用 text/plain 免预检, 壳端 do_OPTIONS 是双保险。
            try:
                n = dm_names_save(body.get("pairs") or [])
                return self.send_json({"ok": True, "n": n})
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)[:100]})
        if path == "/api/login/qr":
            try:
                return self.send_json({"ok": True, **login_qr_generate()})
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)[:150]})

        if path == "/api/messages/read":
            # 用户点开了消息页 = 这些未读看过了。前端把当前各平台未读数报上来, 存成快照。
            return self.send_json({"ok": True, "seen": msg_seen_save(body.get("seen"))})

        if path == "/api/account":
            sset("bili_cookie", (body.get("cookie") or "").strip())
            try:
                profile = fetch_account_profile()
                maybe_sync_blocklist()
                return self.send_json({"ok": True, "profile": profile})
            except Exception as e:
                if str(e).startswith("Cookie"):   # Cookie 无效时清掉, 避免残留
                    sset("bili_cookie", "")
                return self.send_json({"ok": False, "error": str(e)[:200]})
        if path == "/api/account/sync":
            try:
                profile = fetch_account_profile()
                maybe_sync_blocklist()
                return self.send_json({"ok": True, "profile": profile})
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)[:200]})
        if path == "/api/account/logout":
            sset("bili_cookie", "")
            sset("acct_profile", "")
            sset("acct_nickname", "")
            return self.send_json({"ok": True})

        if path == "/api/xhh/logout":
            sset("xhh_cookie", "")
            sset("xhh_pkey", "")
            sset("xhh_heybox_id", "")
            sset("xhh_user", "")
            sset("xhh_avatar", "")
            return self.send_json({"ok": True})

        # 百度安全验证的人工滑块通道(v0.3.1.55) —— 前端是 POST 过来的,
        # 只挂 do_GET 会让点击变成 404("点了没反应"), 这里必须同样注册一份。
        if path == "/api/tieba/verify/start":
            return self.send_json(tb_verify_start())
        if path == "/api/tieba/verify/poll":
            return self.send_json(tb_verify_poll())
        if path == "/api/tieba/verify/stop":
            return self.send_json(tb_verify_stop())

        if path == "/api/live/settings":
            if "min_viewers" in body:
                try:
                    sset("live_min_viewers", str(max(0, int(body["min_viewers"]))))
                except Exception:
                    pass
            if "only_followed" in body:
                # 前端可能传 true/false, 也可能传 "1"/"0" 字符串 —— 字符串 "0" 是**真值**,
                # 直接 if body[...] 会把"取消只看关注"存成开启(实测踩过), 这里显式判一遍。
                _v = str(body["only_followed"]).strip().lower()
                sset("live_only_followed", "0" if _v in ("0", "false", "off", "", "none") else "1")
            force_rebuild()
            mn, only = live_filters()
            return self.send_json({"ok": True, "min_viewers": mn, "only_followed": only})

        if path == "/api/tieba":
            if "bars" in body:
                bars = body.get("bars") or []
                if not isinstance(bars, list):
                    return self.send_json({"ok": False, "error": "bars 需为数组"}, 400)
                tb_set_bars([str(b).strip()[:30] for b in bars if str(b).strip()])
            if "allow_help" in body:
                sset("tb_allow_help", "1" if body["allow_help"] else "0")
            # 平台权重通用保存(PLATFORMS 注册的所有平台, 含小黑盒)
            for _, key, _d in PLATFORMS:
                if key in body:
                    try:
                        w = int(body[key])
                    except Exception:
                        continue
                    # 0-100 全区间可调(左栏每条内容源下面就是一条滑条, 不再只认 4 个档位)
                    if 0 <= w <= 100:
                        sset(key, str(w))
            force_rebuild()   # 吧列表/权重变更 → 立即重建
            return self.send_json({"ok": True})
        if path == "/api/tieba/grab":
            global _tb_last_fetch, _tb_fetch_total
            # 从窗口抓当前贴吧列表页内容(浏览器通道, 绕开服务端403封锁)
            if not _CDP_PROC or _CDP_PROC.poll() is not None:
                return self.send_json({"ok": False, "error": "窗口未打开(请先点「在窗口中打开贴吧页」)"})
            with local_open(
                    "http://127.0.0.1:%d/json/list" % _CDP_PORT, timeout=3) as resp:
                targets = json.loads(resp.read().decode())
            page = next((t for t in targets if t.get("type") == "page"), None)
            if not page:
                return self.send_json({"ok": False, "error": "窗口未就绪"})
            items, fname, err = tb_browser_grab(page["webSocketDebuggerUrl"])
            if not items:
                return self.send_json({"ok": False, "error": err or "该页未解析到帖子"})
            now = int(time.time())
            with _db_lock:
                for it in items:
                    db().execute(
                        "INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,?,0) "
                        "ON CONFLICT(key) DO UPDATE SET json=excluded.json,fetched_at=excluded.fetched_at",
                        (it["key"], json.dumps(it, ensure_ascii=False), now))
                db().commit()
            _tb_last_fetch = now
            _tb_fetch_total += len(items)
            sset("tb_last_fetch", now)
            sset("tb_error", "")
            return self.send_json({"ok": True, "items": len(items), "fname": fname,
                                   "message": "已从浏览器抓取 %s×%d" % (fname, len(items))})
        if path == "/api/tieba/login":
            return self.send_json(tb_login_start())
        if path == "/api/tieba/login/stop":
            return self.send_json(tb_login_stop())
        if path == "/api/tieba/login/manual":
            cookie = (body.get("cookie") or "").strip()
            if not cookie or "BDUSS=" not in cookie:
                return self.send_json({"ok": False, "error": "Cookie 不含 BDUSS（需完整贴吧登录凭据）"})
            sset("tb_cookie", cookie)
            check = tb_login_check()
            if check.get("logged_in"):
                # 顺路拉真实昵称/头像(手机版登录没有CDP DOM可抓, 走newmoindex接口)
                nick, av = tb_profile_from_cookie()
                sset("tb_user", nick or check.get("nickname") or "已登录")
                if av:
                    sset("tb_avatar", av)
                return self.send_json({"ok": True, "nickname": nick, "avatar": av})
            sset("tb_cookie", "")   # 无效凭证清掉
            return self.send_json({"ok": False, "error": "Cookie 无效或已过期（需含有效 BDUSS）"})
        if path == "/api/tieba/logout":
            sset("tb_cookie", "")
            sset("tb_user", "")
            sset("tb_avatar", "")
            return self.send_json({"ok": True})

        if path == "/api/machine":
            for k in ("m_min_views", "m_max_views", "m_max_duration", "m_min_duration", "m_repeat_limit", "m_repeat_cooldown_hours", "m_dedup", "m_blocklist_sync",
                      "m_max_age_months", "ser_max_age_months", "m_seen_hours", "m_emotion",
                      "m_news_fresh", "m_play_related", "xhh_comm", "xhh_topic", "bili_follow_emph", "bili_follow_months",
                    "m_cold_start_n", "cf_blk_on", "cf_min_level",
                      "m_block_mystery_bio", "m_block_lottery", "m_block_empty_space",
                      "m_dm_multicand", "m_filter_speed", "m_dm_on",
                      "ft_on", "ft_video", "ft_dm", "ft_cf", "ft_thresh", "ft_labels"):
                if k in body:
                    sset(k, body[k])
            if "m_cold_start_n" in body:
                persist_cold_cache(force=True)   # 上限改动立即重存当前池快照
            if "ft_on" in body:
                ft_warmup()                      # 开关一动就在后台把模型读进来(或确认不需要)
            if "m_sources" in body:   # 允许的来源/分区 id 列表
                sset("m_sources", json.dumps(body["m_sources"]))
                n = purge_items_by_parts(enabled_source_ids())   # 切分区后清旧池
                force_rebuild()   # 作废旧构建, 立即按新分区重建(不等待)
                return self.send_json({"ok": True, "purged": n})
            return self.send_json({"ok": True})
        if path == "/api/machine/blocklist-sync":
            if not account_cookie():
                return self.send_json({"ok": False, "error": "请先登录B站账号（个人 → B站账号）"})
            try:
                n = fetch_account_blocklist()
                return self.send_json({"ok": True, "count": n})
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)[:150]})
        if path == "/api/machine/blocklist-remove":
            db().execute("DELETE FROM ablock WHERE name=?", (body.get("name", ""),))
            db().commit()
            return self.send_json({"ok": True})

        if path == "/api/feedback":
            key = body.get("key", "")
            action = body.get("action", "")
            changed = False
            if action in ("undolike", "undodislike"):
                cur = db().execute("DELETE FROM feedback WHERE key=? AND action=?",
                                   (key, action[4:]))
                changed = cur.rowcount > 0
            elif key and action:
                it = item_by_key(key) or {}
                # 去重: 同一条目重复点踩只记一次(重复行只会污染画像统计)
                exists = db().execute("SELECT 1 FROM feedback WHERE key=? AND action=?",
                                      (key, action)).fetchone()
                if not exists:
                    db().execute("INSERT INTO feedback(key,author,action,ts) VALUES(?,?,?,?)",
                                 (key, it.get("author", ""), action, int(time.time())))
                    changed = True
            db().commit()
            if changed and action in ("dislike", "undodislike"):
                # 负反馈生效信号: 计划缓存键含 feedback_sig → 下次规划强制重排,
                # 已在计划中的不喜欢条目立即被 planned_feed 的负反馈过滤剔除。
                sset("feedback_sig", str(int(time.time())))
            return self.send_json({"ok": True})
        if path == "/api/feed/refresh":
            key = (body.get("key") or "").strip()
            if not key:
                return self.send_json({"ok": False, "error": "缺少参数"}, 400)
            it = feed_refresh_item(key, q=(body.get("q") or "").strip())   # 搜索模式限定同主题
            if not it:
                return self.send_json({"ok": False, "error": "暂无可用内容替换"})
            if not (body.get("q") or "").strip():
                record_feed_presentations([it], uuid.uuid4().hex)
            return self.send_json({"ok": True, "item": it})

        if path == "/api/blacklist":
            kind, value, remove = body.get("kind"), (body.get("value") or "").strip(), body.get("remove")
            if not kind or not value:
                return self.send_json({"ok": False, "error": "参数不完整"}, 400)
            if remove:
                db().execute("DELETE FROM blacklist WHERE kind=? AND value=?", (kind, value))
            else:
                db().execute("INSERT OR IGNORE INTO blacklist(kind,value) VALUES(?,?)", (kind, value))
            db().commit()
            # 黑名单/屏蔽变化 → 计划重排信号(apply_filters/negative_feedback_filter 即时生效)
            sset("feedback_sig", str(int(time.time())))
            return self.send_json({"ok": True})

        if path == "/api/saved":
            key, kind, remove = body.get("key", ""), body.get("kind", "later"), body.get("remove")
            if remove:
                db().execute("DELETE FROM saved WHERE key=? AND kind=?", (key, kind))
            else:
                db().execute("INSERT OR REPLACE INTO saved(key,kind,ts) VALUES(?,?,?)",
                             (key, kind, int(time.time())))
            db().commit()
            return self.send_json({"ok": True})

        if path == "/api/click":
            db().execute("INSERT OR REPLACE INTO history(key,ts) VALUES(?,?)",
                         (body.get("key", ""), int(time.time())))
            db().commit()
            set_last_feed(item_by_key(body.get("key", "")))   # 点开即“刚才那条”
            return self.send_json({"ok": True})

        if path == "/api/disclaimer/ack":
            version = body.get("version")
            if not isinstance(version, str) or version != DISCLAIMER_VERSION:
                return self.send_json({"ok": False, "error": "免责声明版本无效或已过期"}, 400)
            sset("disclaimer_version", version)
            return self.send_json({"ok": True, "version": version})

        if path == "/api/intro/done":
            # 完成与跳过都写这一条：不能每次启动都拦一遍
            sset("intro_done", "1")
            return self.send_json({"ok": True})

        if path == "/api/settings":
            old_prefs = sget("prefs_note") or ""
            old_dislike = sget("dislike_note") or ""
            for k in ("ai_base", "ai_key", "ai_model", "mode", "prefs_note", "dislike_note",
                      "price_in", "price_out", "ai_full_scan", "ai_interest_min",
                      "ai_batch", "ai_cache_hours", "ai_enabled", "ui_bq", "ui_bq_level",
                      "ui_sr", "ui_fg", "ui_sr_level", "ui_sr_scale", "ui_ownui",
                      "ui_new_window"):
                if k in body and body[k] is not None:
                    if k in ("ai_base", "ai_model") and not str(body[k]).strip():
                        continue   # 地址/模型不允许空值覆盖(保留默认/原值)
                    if k == "mode" and str(body[k]) not in MODE_THRESH:
                        continue   # 档位白名单: 防止乱码/非法值污染
                    sset(k, body[k])
            if any(k in body for k in ("ai_full_scan", "ai_interest_min", "ai_batch",
                                       "ai_cache_hours", "ai_enabled")):
                force_rebuild()   # 评分范围/标准/粒度/总开关变了, 立即重建
            # 画像类规则(厌恶/偏好备注)变化 → 旧语义分数过时, 清评分缓存强制重评
            if (("prefs_note" in body and body.get("prefs_note") is not None and
                 (body.get("prefs_note") or "") != old_prefs) or
                ("dislike_note" in body and body.get("dislike_note") is not None and
                 (body.get("dislike_note") or "") != old_dislike)):
                clear_score_cache()
                force_rebuild()
            if "mode" in body and body.get("mode") != sget("mode"):
                with _db_lock:   # 档位(含严肃模式)切换: 清展示池并立即重建, 不让旧内容回流
                    db().execute("DELETE FROM shown")
                    db().commit()
                force_rebuild()
            return self.send_json({"ok": True, "ai_ok": ai_ok(), "ai_on": ai_on()})

        if path == "/api/settings/export":
            payload = settings_export_payload(bool(body.get("credentials")))
            return self.send_json({"ok": True, "payload": payload})

        if path == "/api/settings/import":
            incoming = body.get("settings")
            if not isinstance(incoming, dict):
                return self.send_json({"ok": False, "error": "缺少 settings 对象"}, 400)
            imported, skipped = [], []
            for k, v in incoming.items():
                if k not in _IO_IMPORTABLE:
                    skipped.append(k); continue
                if v is None:
                    continue
                if k in ("ai_base", "ai_model") and not str(v).strip():
                    continue   # 与手动保存一致: 不允许空值覆盖
                if k == "mode" and str(v) not in MODE_THRESH:
                    skipped.append(k); continue   # 档位白名单
                sset(k, v)
                imported.append(k)
            bl = body.get("blacklist") or {}
            add_a = add_k = 0
            if isinstance(bl, dict):
                with _db_lock:
                    for kind, vals in (("author", bl.get("authors") or []),
                                       ("keyword", bl.get("keywords") or [])):
                        if not isinstance(vals, list):
                            continue
                        have = {r[0] for r in db().execute(
                            "SELECT value FROM blacklist WHERE kind=?", (kind,))}
                        for val in vals:
                            val = str(val).strip()
                            if val and val not in have:
                                db().execute("INSERT INTO blacklist(kind,value) VALUES(?,?)",
                                             (kind, val))
                                have.add(val)
                                if kind == "author": add_a += 1
                                else: add_k += 1
                    db().commit()
            # 语义相关的改动 → 清缓存重建, 与手动保存行为一致
            if any(k in imported for k in ("prefs_note", "dislike_note")):
                clear_score_cache()
            if "mode" in imported:
                with _db_lock:
                    db().execute("DELETE FROM shown")
                    db().commit()
            force_rebuild()
            return self.send_json({"ok": True, "imported": len(imported),
                                   "skipped": skipped[:12],
                                   "bl_authors": add_a, "bl_keywords": add_k})

        if path == "/api/conv":
            cid = conv_create((body.get("title") or "").strip() or None, set_cur=True)
            return self.send_json({"ok": True, "id": cid})
        if path == "/api/conv/select":
            cid = conv_set_cur(body.get("id"))
            return self.send_json({"ok": True, "cid": cid})
        if path == "/api/conv/rename":
            ok = conv_rename(body.get("id"), body.get("title", ""))
            return self.send_json({"ok": ok})
        if path == "/api/conv/delete":
            conv_delete(body.get("id"))
            return self.send_json({"ok": True})

        if path == "/api/rebut/save":
            rid = rebut_save(body.get("source") or body.get("text") or "",
                             body.get("seg") or "", body.get("content") or "")
            return self.send_json({"ok": bool(rid), "id": rid})
        if path == "/api/rebut/delete":
            return self.send_json({"ok": rebut_saved_delete(body.get("id"))})
        if path == "/api/personas":
            # 整表覆盖保存自定义人设预设
            n = persona_presets_save(body.get("items") or [])
            return self.send_json({"ok": True, "count": n, "items": persona_presets()})

        if path == "/api/floatball":
            theme = body.get("theme")
            if isinstance(theme, dict):
                # 主界面把当前强调色/深浅主题推给悬浮球(切换颜色时也走这里, 不影响开关)
                acc = str(theme.get("accent") or "").strip()
                if re.match(r"^#[0-9a-fA-F]{6}$", acc):
                    sset("float_ball_accent", acc)
                light = bool(theme.get("light"))
                sset("float_ball_light", "1" if light else "0")
                sset("ui_theme", "light" if light else "dark")   # 正式主题键(桌面窗口壳读这份)
                acc_id = str(theme.get("accent_id") or "").strip()
                if acc_id and len(acc_id) <= 40 and "<" not in acc_id:
                    sset("ui_accent_id", acc_id)   # 强调色原始标识(green/pink/custom:#xxx, 供壳内页面恢复)
                bg = theme.get("bg")
                if isinstance(bg, dict):
                    # 页面算好的原生标签栏配色方案(hex串), shell_tabstrip 轮询消费
                    _need = ("bar", "active", "hover", "fgon", "fgoff", "sep")
                    if all(re.match(r"^#[0-9a-fA-F]{6}$", str(bg.get(k) or "")) for k in _need):
                        try:
                            sset("ui_bg_scheme", json.dumps(bg, ensure_ascii=False))
                        except Exception:
                            pass
            prefs_in = body.get("prefs")
            if isinstance(prefs_in, dict):
                # 悬浮窗调节档位: 白名单校验, 非法值不落库
                if prefs_in.get("think") in ("快", "标准", "深挖"):
                    sset("assist_think", prefs_in["think"])
                if _persona_valid(prefs_in.get("persona")):
                    sset("assist_persona", _persona_key(prefs_in["persona"]))
                if str(prefs_in.get("savage")) in _SAVAGE_TIER:
                    sset("assist_savage", _savage_key(prefs_in["savage"]))   # 旧值折算后落库
                if str(prefs_in.get("len")) in _LEN_TIER:
                    sset("assist_len", str(_LEN_TIER[str(prefs_in["len"])]))   # 统一存 1/2/3
                # 注: 老的 prefs.hover(悬停自动展开开关)已不再受理 —— 功能整体移除。
            if "enabled" in body:
                enabled = bool(body.get("enabled"))
                sset("float_ball", "1" if enabled else "0")
                if enabled:
                    float_ball_spawn(_server_port())
                else:
                    float_ball_stop()
            env = float_ball_env()
            return self.send_json({"ok": True, "supported": env["ok"],
                                   "reason": env["reason"], "hint": env["hint"],
                                   "python": env["python"],
                                   "running": float_ball_running(),
                                   "spawn_error": float_ball_last_error(),
                                   "accent": sget("float_ball_accent") or "#fb7299",
                                   "light": sget("float_ball_light") == "1",
                                   "prefs": _floatball_prefs()})

        if path == "/api/ai/assist":
            # AI 悬浮窗对话窗: 验证真实性/生成回怼(联网取证 + LLM 结构化输出)
            mode = body.get("mode") or "verify"
            text = (body.get("text") or "").strip()
            seg_only = str(body.get("seg") or "").strip() or None   # 指定则只重跑这一段(换一批)
            if mode not in ("verify", "rebut"):
                return self.send_json({"ok": False, "error": "未知模式"}, 400)
            if seg_only and seg_only not in _SEG_SPEC:
                return self.send_json({"ok": False, "error": "未知的段落"}, 400)
            if not text:
                return self.send_json({"ok": False, "error": "内容不能为空"}, 400)
            if not ai_on():
                return self.send_json({"ok": False,
                                       "error": "AI 未配置或已关闭, 请先在主界面设置里配置 API Key"}, 403)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def sse(event, data):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (
                    event, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()

            t0 = time.time()
            _COST_TL.in_tokens = _COST_TL.out_tokens = _COST_TL.calls = 0   # 本次请求用量清零

            def emit(kind, payload):
                # 三段式对线: 每段生成完立刻推送(seg 事件), 用户不用等整轮结束
                sse("seg", dict(payload, kind=kind))

            def push_all(res, refs):
                """缓存命中时把整轮结果按同样的顺序补推一遍, 前端渲染逻辑不用分叉。"""
                emit("targets", {"items": res.get("targets") or [],
                                 "stance_note": res.get("stance_note") or "",
                                 "evidence": _evidence_brief(refs)})
                for k in ("lines", "paras", "long"):
                    emit(k, {"items": res.get(k) or []})

            try:
                if mode == "rebut" and seg_only:
                    # 「换一批」: 只重跑这一段, 论点与证据沿用上一轮(不再拆解与取证)
                    memo = last_rebut_recall(text)
                    if memo:
                        sse("phase", {"stage": "regen", "label": "这一段换一批中…"})
                        res, refs = assist_rebut(text, memo["evidence"], memo["claims"],
                                                 emit=emit, only=seg_only,
                                                 targets=memo["targets"])
                    else:
                        sse("phase", {"stage": "search",
                                      "label": "上下文已过期, 正在重新拆解与取证…"})
                        claims, queries = assist_plan(text)
                        evidence = gather_evidence(text, queries or [text.strip()[:64]])
                        res, refs = assist_rebut(text, evidence, claims, emit=emit,
                                                 only=seg_only)
                elif mode == "rebut":
                    key = rebut_cache_key(text)
                    hit = rebut_cache_get(key)
                    if hit:
                        # 同一条原话 + 同一套档位 → 直接命中, 不再拆解/取证/生成
                        sse("phase", {"stage": "cache", "label": "同一条原话刚生成过, 直接给出上次结果…"})
                        res, refs = hit
                        push_all(res, refs)
                    else:
                        sse("phase", {"stage": "search", "label": "正在拆解论点、联网搜集证据与话术模板…"})
                        claims, queries = assist_plan(text)
                        if not queries:
                            queries = [text.strip()[:64]]   # 拆解失败: 按原文整体检索
                        evidence = gather_evidence(text, queries)
                        hint = ("逐点取证完成" if claims else "取证完成") if evidence else "未搜到网络证据"
                        sse("phase", {"stage": "think",
                                      "label": ("已核对 %d 个来源(%s), 正在找对方的知识误区与逻辑谬误…"
                                                % (len(evidence), hint))
                                      if evidence else "未搜到网络证据, 仅凭已有知识开怼…"})
                        res, refs = assist_rebut(text, evidence, claims, emit=emit)
                        if not res.get("no_target"):
                            rebut_cache_put(key, (res, refs))
                            last_rebut_remember(text, evidence, claims, res.get("targets"))
                else:
                    sse("phase", {"stage": "search", "label": "正在拆解论点、联网搜集证据…"})
                    claims, queries = assist_plan(text)
                    if not queries:
                        queries = [text.strip()[:64]]
                    evidence = gather_evidence(text, queries)
                    sse("phase", {"stage": "think",
                                  "label": ("已核对 %d 个来源, AI 分析中…" % len(evidence))
                                  if evidence else "未搜到网络证据, 仅凭已有知识判断…"})
                    res, refs = assist_result(mode, text, evidence, claims)
                sse("done", {"ok": True, "mode": mode, "result": res, "evidence": refs,
                             "regen": seg_only or "",
                             "usage": _assist_usage_snapshot(),
                             "elapsed": round(time.time() - t0, 1)})
            except Exception as e:
                sse("done", {"ok": False, "mode": mode, "regen": seg_only or "",
                             "error": "AI 这次没能给出有效回复(%s)，请再试一次" % str(e)[:80]})
            return

        # v0.3.1.62: 把"用户说的这句改动"先交给模型理解(带可调的思考强度),
        # 再回给前端落到现有设置接口; 模型不可用时前端退回本地关键词规则。
        if path == "/api/chat/plan":
            txt = str(body.get("text") or "").strip()
            if not txt:
                return self.send_json({"ok": False, "error": "空消息"}, 400)
            eff = str(body.get("effort") or "mid")
            prefs = body.get("prefs") if isinstance(body.get("prefs"), dict) else {}
            weights = body.get("weights") if isinstance(body.get("weights"), dict) else {}
            live = body.get("live") if isinstance(body.get("live"), dict) else {}
            # 带上本会话最近的对话: "换回去"/"再大一点" 离开上下文没法解
            hist = []
            try:
                cid = conv_cur(body.get("cid"))
                rows = db().execute(
                    "SELECT role,content FROM chat WHERE cid=? ORDER BY id DESC LIMIT 8",
                    (cid,)).fetchall()
                hist = [{"role": r, "content": c} for r, c in reversed(rows)]
            except Exception:
                hist = []
            return self.send_json(chat_plan(txt, eff, prefs, weights, live, hist))

        if path == "/api/chat":
            msg = (body.get("message") or "").strip()
            if not msg:
                return self.send_json({"ok": False, "error": "空消息"}, 400)
            stream = bool(body.get("stream")) and ai_on()
            chips = build_chips()
            # chips确认通道: 带payload的消息直接执行对应操作(跳过命令引擎/意图识别)
            payload = body.get("payload") if isinstance(body.get("payload"), dict) else None
            has_payload = bool(payload and payload.get("op"))
            # 命令引擎优先(纯规则, 不烧token): 指代/分区/统计/新对话等
            cmd = None if has_payload else handle_command(msg)
            cid = conv_cur(body.get("cid") or (cmd.get("cid") if cmd else None))
            conv_touch(cid, msg)   # 首句自动命名
            db().execute("INSERT INTO chat(role,content,ts,cid) VALUES('user',?,?,?)",
                         (msg, int(time.time()), cid))
            db().commit()

            def sse_send(event, data):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (
                    event, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()

            def finish(reply, ok=True, err="", trigger=None):
                db().execute("INSERT INTO chat(role,content,ts,cid) VALUES('assistant',?,?,?)",
                             (reply, int(time.time()), cid))
                db().commit()
                return self.send_json({"ok": ok, "reply": reply, "intent": intent,
                                       "chips": chips, "cid": cid, "error": err,
                                       "trigger": trigger, "ctx": chat_ctx_get(cid)})

            def sse_reply(reply, intent, trigger=None):
                """一次性回复的 SSE 事件流(无增量): meta → done。"""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sse_send("meta", {"intent": intent, "chips": chips, "cid": cid,
                                  "ctx": chat_ctx_get(cid)})
                sse_send("done", {"reply": reply, "chips": chips, "ok": True,
                                  "trigger": trigger, "ctx": chat_ctx_get(cid)})
                db().execute("INSERT INTO chat(role,content,ts,cid) VALUES('assistant',?,?,?)",
                             (reply, int(time.time()), cid))
                db().commit()

            if has_payload:
                intent = {"action": "reply", "keywords": []}
                res = handle_chat_payload(payload)
                reply = res.get("reply", "")
                if stream:
                    sse_reply(reply, intent, res.get("trigger"))
                    return
                return finish(reply, trigger=res.get("trigger"))

            if cmd:
                intent = {"action": "reply", "keywords": []}
                reply = cmd.get("reply", "")
                trig = cmd.get("trigger")
                if stream:
                    sse_reply(reply, intent, trig)
                    return
                return finish(reply, trigger=trig)

            intent = extract_intent(msg)
            # 意图搜索: 固定短回复(不调用LLM, 省token又快), 前端切搜索流
            if intent["action"] == "add_dislike":
                # 真正执行: 把关键词(含AI补的相关词)追加到厌恶备注
                kws = intent.get("keywords") or []
                cur = sget("dislike_note")
                new, added = cur, []
                for k in kws:
                    k = k.strip()
                    if not k:
                        continue
                    if k not in new:
                        new = (new + "，" + k).strip("，") if new else k
                        added.append(k)
                sset("dislike_note", new)
                clear_score_cache()   # 厌恶画像变化 → 旧语义分数过时, 强制重评
                reply = "已把「%s」写进厌恶备注，之后会避开这些内容。" % (
                    "、".join(added) if added else "、".join(kws))
                intent = {"action": "reply", "keywords": []}
                if stream:
                    sse_reply(reply, intent, "settings")
                    return
                return finish(reply, trigger="settings")

            if intent["action"] == "set_mode":
                md = intent.get("mode")
                sset("mode", md)
                with _db_lock:   # 档位切换: 清展示池并立即重建
                    db().execute("DELETE FROM shown")
                    db().commit()
                force_rebuild()
                reply = "已切换到【%s】档，信息流已按新档位重建。" % md
                intent = {"action": "reply", "keywords": []}
                if stream:
                    sse_reply(reply, intent, "settings")
                    return
                return finish(reply, trigger="settings")

            if intent["action"] == "add_pref":
                # 确认制: 写偏好备注会清评分缓存并重建(全扫模式下有重评费用), chips确认后经payload执行
                kws = intent.get("keywords") or []
                fresh = [k for k in kws if k not in sget("prefs_note")]
                if fresh:
                    reply = ("要把这些写进偏好备注吗？\n%s\n写入后我会重建信息流并对内容重新评分。" %
                             "、".join(fresh))
                    chips = chips + [
                        {"label": "✅ 确认写入", "cmd": "确认写入偏好：" + "、".join(fresh),
                         "payload": {"op": "add_pref", "keywords": fresh}},
                        {"label": "❌ 先不了", "cmd": "先不写入偏好", "payload": {"op": "noop"}}]
                else:
                    reply = "这些已经在偏好备注里了：%s" % "、".join(kws)
                intent = {"action": "reply", "keywords": []}
                if stream:
                    sse_reply(reply, intent, None)
                    return
                return finish(reply)

            if intent["action"] == "set_window":
                months = intent.get("months", 0)
                sset("m_max_age_months", months)
                force_rebuild()
                reply = "已设置：只显示最近 %s 个月的内容%s，信息流已重建。" % (
                    months, "（0=不限）" if months == 0 else "")
                intent = {"action": "reply", "keywords": []}
                if stream:
                    sse_reply(reply, intent, "settings")
                    return
                return finish(reply, trigger="settings")

            if intent["action"] == "toggle_partition":
                groups, mode = intent.get("groups") or [], intent.get("mode")
                ids = {GROUP_NAMES[g] for g in groups if g in GROUP_NAMES}
                if mode == "only":
                    names = "、".join(groups)
                    reply = ("要切换成只看「%s」吗？其余分区的沉淀池会被清理（之后可再切回来）。" % names)
                    chips = chips + [
                        {"label": "✅ 确认只看：" + names, "cmd": "确认切换分区：" + names,
                         "payload": {"op": "partition_only", "keywords": groups}},
                        {"label": "❌ 先不了", "cmd": "先不切换分区", "payload": {"op": "noop"}}]
                    intent = {"action": "reply", "keywords": []}
                    if stream:
                        sse_reply(reply, intent, None)
                        return
                    return finish(reply)
                full = {g[0] for g in SOURCE_GROUPS}
                cur = enabled_source_ids()
                if mode == "remove":
                    base = cur if (cur and cur != full) else full
                    ids = base - ids
                else:
                    base = cur if (cur and cur != full) else set()
                    ids = base | ids
                names = apply_partition_ids(ids)
                reply = "已%s分区：%s（%d 个）" % ("关掉" if mode == "remove" else "加上", names, len(ids))
                intent = {"action": "reply", "keywords": []}
                if stream:
                    sse_reply(reply, intent, "settings")
                    return
                return finish(reply, trigger="settings")

            if intent["action"] == "open_explore":
                reply = _open_explore()
                intent = {"action": "reply", "keywords": []}
                if stream:
                    sse_reply(reply, intent, "random")
                    return
                return finish(reply, trigger="random")

            if intent["action"] == "digest":
                reply, rep_chips, _t = daily_report_reply()
                if rep_chips:
                    chips = chips + rep_chips   # 采纳建议的确认chips追加到回复下方
                intent = {"action": "reply", "keywords": []}
                if stream:
                    sse_reply(reply, intent, _t)
                    return
                return finish(reply)

            if intent["action"] == "search":
                reply = ("收到！已按你的要求刷「%s」相关视频，左侧正在搜索，下滑看更多～" %
                         "、".join(intent["keywords"]))
                if stream:
                    sse_reply(reply, intent)
                    return
                return finish(reply)

            if not ai_on():
                reply = ("AI 功能已关闭或未配置。请到左侧「设置」检查 AI 功能总开关（开启后）"
                         "并填入 OpenAI 兼容的 API Key（默认 DeepSeek）。开启后我能听懂你说的话"
                         "并直接刷视频；现在你可以直接说「给我刷某某视频」，我会按句式识别并搜索。")
                if stream:
                    sse_reply(reply, intent)
                    return
                return finish(reply)

            # --- LLM 自由聊天(或流式) ---
            rows = db().execute("SELECT role,content FROM chat WHERE cid=? ORDER BY id DESC LIMIT 16",
                                (cid,)).fetchall()
            history = [{"role": r, "content": c} for r, c in reversed(rows) if (c or "").strip()]  # 过滤空消息, 防API拒绝
            extra_ctx = "\n".join(x for x in (chat_item_context(cid), chat_last_context(msg)) if x)
            messages = ([{"role": "system", "content":
                          "你是「洞鉴虚明」的偏好助手。帮用户定义想看什么内容，"
                          "回复要简短友好。"
                          # v0.3.1.67: 这句原来写的是"你只能口头回答，无法真正切换" ——
                          # 那是 AI 改设置(v62)之前的话。模型当真了，用户说"换回去"它就答
                          # "我确实动不了你的页面布局，得你自己去点"。能力边界必须写准。
                          "你能真正改动界面与筛选设置：主题、信息流布局（单栏/双栏/大图）、"
                          "封面大小、字号、推荐理由显示、来源标识、强调色、过滤档位、"
                          "各平台占比、直播筛选。用户直接说「换成双栏」「字号大一点」时，"
                          "系统会在你回答之前就把它执行掉。"
                          "所以绝不要说「我改不了」「动不了你的页面布局」或让他自己去设置页点；"
                          "你不确定他想改哪一项时，就问一句「是想换成单栏吗」。"
                          "你确实做不到的是推送/搜索/切换视频，也看不到页面上的执行结果，"
                          "因此不要声称「已推送」「已切换到左侧」「已改好」。"
                          "用户想刷视频时会直接说「给我刷/推/看XX」，系统会自动执行并另行确认。"
                          "如果上下文里有【用户当前正在聊的条目】或【用户指代的最近条目】，"
                          "围绕它可以做总结、点评、回答追问；没有依据的部分不要编造。"
                          "可参考的用户画像:\n" + (user_context() or "(空)")
                          + (("\n\n" + extra_ctx) if extra_ctx else "")}]
                        + history)
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sse_send("meta", {"intent": intent, "chips": chips, "cid": cid})
                reply = ""
                thinking_txt = []
                aborted = False

                def on_reasoning(t):
                    try:
                        if not thinking_txt:   # 推理模型: 思考阶段前端显示"思考中…"
                            sse_send("think", {})
                        thinking_txt.append(t)
                    except Exception:
                        pass
                try:
                    for delta in llm_stream(messages, max_tokens=4096, on_reasoning=on_reasoning):
                        reply += delta
                        try:
                            sse_send("delta", {"t": delta})
                        except Exception:
                            aborted = True
                            break
                except Exception as e:
                    if not reply:
                        reply = "AI 调用失败（%s）。请检查左侧设置里的 API 地址 / Key / 模型。" % str(e)[:120]
                if aborted and not reply:
                    reply = "（已停止生成）"
                if not reply and not aborted:
                    # 思考过长被截断时, 用思考尾段兜底(结论通常在后半段)
                    tail = "".join(thinking_txt)[-160:].strip()
                    reply = ("模型思考过长了：%s（以上为模型思考过程，供参考）" % tail) if tail else \
                            "模型思考后没有给出文字回答，再问一次或换个说法试试～"
                if not aborted and reply and "AI 调用失败" not in reply:
                    # v4-flash 流式不返回 usage → 按字数估算记账(意图判断已是真实值)
                    record_usage({"prompt_tokens": int(sum(
                                     len((m.get("content") or "")) for m in messages) * 0.8) + 8,
                                  "completion_tokens": int(len(reply) * 0.8) + 8})
                db().execute("INSERT INTO chat(role,content,ts,cid) VALUES('assistant',?,?,?)",
                             (reply, int(time.time()), cid))
                db().commit()
                try:
                    sse_send("done", {"reply": reply, "chips": chips, "ok": True})
                except Exception:
                    pass
                return
            try:
                content, _ = call_llm(messages, max_tokens=4096)
                reply = content.strip()
            except Exception as e:
                reply = "AI 调用失败（%s）。请检查左侧设置里的 API 地址 / Key / 模型。" % str(e)[:120]
            if not reply:
                reply = "模型思考后没有给出文字回答，再问一次或换个说法试试～"
            return finish(reply)

        if path == "/api/chat/ctx":
            # "问AI"上下文: 卡片长按菜单写入, 前端✕或换会话清除(key为空=清除)
            cid = conv_cur(body.get("cid"))
            ctx = chat_ctx_set(cid, body.get("key", ""), body.get("title", ""))
            return self.send_json({"ok": True, "ctx": ctx})

        if path == "/api/chat/note":
            # 只记一笔对话, 不做任何推理。
            # 用途: 聊天栏里"帮我改设置"这类指令是前端规则直接执行的(不等模型、不烧 token),
            # 但对话历史仍要落库 —— 否则切会话/重开页面这段对话就没了, 用户以为自己没说过。
            me = (body.get("me") or "").strip()
            ai = (body.get("ai") or "").strip()
            if not me and not ai:
                return self.send_json({"ok": False, "error": "空消息"}, 400)
            cid = conv_cur(body.get("cid"))
            ts = int(time.time())
            db().execute("INSERT INTO chat(role,content,ts,cid) VALUES('user',?,?,?)", (me, ts, cid))
            if ai:
                db().execute("INSERT INTO chat(role,content,ts,cid) VALUES('assistant',?,?,?)",
                             (ai, ts, cid))
            db().commit()
            conv_touch(cid, me)
            return self.send_json({"ok": True, "cid": cid})

        if path == "/api/chat/clear":
            cid = conv_cur(body.get("cid"))
            db().execute("DELETE FROM chat WHERE cid=?", (cid,))
            db().commit()
            return self.send_json({"ok": True, "cid": cid})

        if path == "/api/reset":
            # 必须用 executescript: sqlite3 的 execute() 一次只接受一条语句,
            # 原来把 5 条 DELETE 用分号连成一条传给 execute() → 必然抛
            # ProgrammingError("You can only execute one statement at a time"),
            # 处理器异常把连接掐断, 前端只看到 NetworkError —— 这个功能从来没成功过。
            with _db_lock:
                db().executescript("DELETE FROM feedback; DELETE FROM history; DELETE FROM saved; "
                                   "DELETE FROM chat; DELETE FROM conv")
                db().commit()
            conv_create(set_cur=True)
            return self.send_json({"ok": True})

        if path == "/api/pool/clear":
            # 清空信息池(沉淀库+评分缓存)与已看记录, 并作废当前世代替换令牌
            # (不在此同步重建, 由前端 loadFeed(true)→/api/feed?refresh=1 触发重新收录)
            global _uni_token
            with _db_lock:
                db().execute("DELETE FROM items")
                db().execute("DELETE FROM shown")
                db().commit()
            with _uni_lock:
                _uni_token += 1
                _universe["items"] = []
                _universe["gen"] += 1
            _fp_clear()   # 指纹记忆随池清空(重建拉回同内容不被误拦)
            return self.send_json({"ok": True})

        self.send_error(404)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))


# ---------------------------------------------------------------- 内置示例数据(断网降级用)

_NOW = int(time.time())
MOCK_ITEMS = [
    {"key": "https://www.bilibili.com/video/BV1mock001", "platform": "B站",
     "title": "《黑神话：悟空》DLC 实机演示曝光：你最期待哪个BOSS？", "author": "游戏观察室",
     "desc": "DLC 实机演示公开，各BOSS战设计细节一次看全。", "cover": "",
     "views": 1284500, "likes": 45210, "duration": 872, "pubdate": _NOW - 3600 * 2,
     "url": "https://www.bilibili.com/video/BV1mock001"},
    {"key": "https://www.bilibili.com/video/BV1mock002", "platform": "B站",
     "title": "为什么说2025年别再盲目买新能源车了？三个关键数据告诉你", "author": "老王的选车笔记",
     "desc": "三个数据看懂今年选车逻辑。", "cover": "",
     "views": 865300, "likes": 21890, "duration": 615, "pubdate": _NOW - 3600 * 5,
     "url": "https://www.bilibili.com/video/BV1mock002"},
    {"key": "https://www.bilibili.com/video/BV1mock003", "platform": "B站",
     "title": "程序员自研的效率工具开源了！一键清理重复文件（附教程）", "author": "码农阿伟",
     "desc": "开源工具演示与安装教程。", "cover": "",
     "views": 302400, "likes": 15230, "duration": 1043, "pubdate": _NOW - 3600 * 8,
     "url": "https://www.bilibili.com/video/BV1mock003"},
    {"key": "https://www.bilibili.com/video/BV1mock004", "platform": "B站",
     "title": "实测10款千元旗舰耳机，性价比之王出乎意料", "author": "数码小雷达",
     "desc": "千元价位横评。", "cover": "",
     "views": 512200, "likes": 19800, "duration": 1265, "pubdate": _NOW - 3600 * 11,
     "url": "https://www.bilibili.com/video/BV1mock004"},
    {"key": "https://www.bilibili.com/video/BV1mock005", "platform": "B站",
     "title": "一个人也能做的家常菜：番茄牛腩的零失败做法", "author": "深夜食堂说",
     "desc": "零失败家常菜教程。", "cover": "",
     "views": 218700, "likes": 9900, "duration": 532, "pubdate": _NOW - 3600 * 14,
     "url": "https://www.bilibili.com/video/BV1mock005"},
    {"key": "https://www.bilibili.com/video/BV1mock006", "platform": "B站",
     "title": "考研倒计时100天：这份复习计划表建议收藏", "author": "上岸学长",
     "desc": "100天复习规划。", "cover": "",
     "views": 154800, "likes": 7200, "duration": 458, "pubdate": _NOW - 3600 * 20,
     "url": "https://www.bilibili.com/video/BV1mock006"},
]


# ---------------- 悬浮球进程入口 (--float-ball <port>, 独立进程承载 tkinter GUI) ----------------

_BALL_PANEL_W, _BALL_PANEL_H = 380, 520   # 展开后的对话面板逻辑尺寸(按 DPI 缩放)
_FONT = "Microsoft YaHei"
_VERDICT_STYLE = {"true": ("真实 ✓", "#67c23a"), "false": ("虚假 ✕", "#f56c6c"),
                  "uncertain": ("存疑 ?", "#e6a23c")}

_EVQ = queue.Queue()   # 聊天工作线程 → tkinter 主线程 的事件队列

# 球体绘制性能探针(设 DJXM_BALL_PROF=1 时每 5s 往 stdout 打一行):
# [是否开启, 帧数, 累计耗时, 上一帧图元数, 上次打印时刻]。悬浮球是常驻小窗, 帧率×单帧成本
# 就是它常驻占用的 CPU, 改动画参数前应该先量一下, 别凭"看着不卡"下结论。
_BALL_PROF = [os.environ.get("DJXM_BALL_PROF") == "1", 0, 0.0, 0, 0.0]
# 极简档(只在测量/低配机器上用): 关掉大面积抖动泛光, 只留棱芯(扫描线已整块移除)。用来分离
# "抖动光栅化的代价"和"分层窗口合成的固定代价"。
_BALL_LEAN = [os.environ.get("DJXM_BALL_LEAN") == "1"]


def _hex_mix(c1, c2, t):
    """两色线性混合(t=0 取 c1, t=1 取 c2)。用于从强调色派生出球体的明暗面。"""
    try:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    except Exception:
        return c1
    t = max(0.0, min(1.0, float(t)))
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ---------------------------------------------------------------- 全息实心体几何
# 球体形态 = **真·三维线框全息**(正二十面体: 12 顶点 / 30 棱 / 20 个三角面)。
# 上一轮画的是"切角八边形装甲 + 六边形环阵", 用户判词: 「这个八角形好丑」「像个钱币」,
# 并且明确要「正20面体 + 3D 效果 + 泛光」。所以这里彻底换掉: 顶点真的在三维里转、
# 真的分正/背面、真的按深度排序遮挡, 泛光靠"轮廓外泛光 + 正面棱多层描边"堆出来。
#
# 为什么这次敢把 30 条棱画在 112px 上(上一版注释里写过"30 条边会糊成一团所以只用六边形"):
# 那时**只画线**, 前后棱全叠在一起当然读不出结构。现在按画家算法先铺**面**(抖动填充,
# 近亮远暗), 面把背面的棱压下去, 于是可见棱收敛成 ~15 条; 再叠深度衰减 + 泛光, 结构就立住了。
# 另外全是直线段: 颜色键控抠边不吃抗锯齿, 没有圆形的锯齿问题。
_BALL_CORE = "diamond"     # 中心徽记: "diamond"(嵌套菱形+横杠+四角刻度, 默认)
                           # | "ripple"(只留菱形+涟漪) | "reticle"(四角括号瞄具) | "none"
_BALL_SOLID = "dode"       # "dode"=正十二面体(12 个**五边形**面 / 20 顶点 / 30 棱) —— 用户要的形状,
                           # 与附件那份「伪3d全息.shader」同形 | "ico"=正二十面体(20 三角面)
                           # | "trunc"=截角二十面体(足球形: 20 六边形 + 12 五边形)

_SOLID_CACHE = {}


def _vsub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vcross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _vdot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vnorm(a):
    n = math.sqrt(_vdot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def _ico_geo():
    """正二十面体: 12 顶点 / 30 棱 / 20 三角面。

    顶点取 (0,±1,±φ) 的三个循环置换(φ=黄金比, 12 个点全在外接球上);
    棱按「距离 = 棱长 2」筛出来(手写 30 条边太容易错一位);
    面 = 两两相邻的三元组 —— 二十面体图里这样的三元组恰好 20 个, 与 20 个面一一对应。
    """
    phi = (1 + 5 ** 0.5) / 2
    v = []
    for a in (-1, 1):
        for b in (-1, 1):
            v += [(0, a, b * phi), (a, b * phi, 0), (a * phi, 0, b)]
    e = [(i, j) for i in range(12) for j in range(i + 1, 12)
         if abs(math.dist(v[i], v[j]) - 2.0) < 1e-6]
    adj = {i: set() for i in range(12)}
    for i, j in e:
        adj[i].add(j)
        adj[j].add(i)
    f = [(i, j, k) for i in range(12) for j in range(i + 1, 12)
         for k in range(j + 1, 12) if j in adj[i] and k in adj[i] and k in adj[j]]
    return v, e, f


def _dode_solid_geo():
    """正十二面体(20 顶点 / 30 棱 / 12 个五边面): 顶点/棱/面表照抄用户那份
    「伪3d全息.shader」, 这样"我那个 shader 长这样"能一比一复现。默认不用它, 留作切换。"""
    phi = (1 + 5 ** 0.5) / 2
    ip = 1.0 / phi
    v = [(1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1),
         (-1, 1, 1), (-1, 1, -1), (-1, -1, 1), (-1, -1, -1),
         (0, ip, phi), (0, ip, -phi), (0, -ip, phi), (0, -ip, -phi),
         (ip, phi, 0), (ip, -phi, 0), (-ip, phi, 0), (-ip, -phi, 0),
         (phi, 0, ip), (phi, 0, -ip), (-phi, 0, ip), (-phi, 0, -ip)]
    e = [(0, 8), (0, 12), (0, 16), (1, 9), (1, 12), (1, 17), (2, 10), (2, 13), (2, 16),
         (3, 11), (3, 13), (3, 17), (4, 8), (4, 14), (4, 18), (5, 9), (5, 14), (5, 19),
         (6, 10), (6, 15), (6, 18), (7, 11), (7, 15), (7, 19), (8, 10), (9, 11),
         (12, 14), (13, 15), (16, 17), (18, 19)]
    f = [(0, 8, 10, 2, 16), (1, 9, 11, 3, 17), (4, 8, 0, 12, 14), (5, 9, 1, 12, 14),
         (6, 10, 2, 13, 15), (7, 11, 3, 13, 15), (0, 16, 17, 1, 12), (2, 16, 17, 3, 13),
         (4, 18, 19, 5, 14), (6, 18, 19, 7, 15), (5, 19, 7, 11, 9), (4, 18, 6, 10, 8)]
    return v, e, f


def _trunc_ico_geo():
    """截角二十面体: 60 顶点 / 90 棱 / 32 面 = **20 个六边形 + 12 个五边形**。

    用户要「20 面体、每个面是六边形」: 正二十面体本身是 20 个**三角**面, 面上是六边形的是它的
    截角体 —— 截掉 12 个顶点: 每个顶点变成一个五边形, 每个三角面变成一个六边形。这就是足球
    (也是富勒烯 C60) 的形状。注意**不可能只由六边形围成球**: 欧拉公式逼着必须有 12 个五边形
    (每个六边形贡献 3 条棱, 12 个五边形 + 20 个六边形正好凑出 90 条棱、60-90+32=2)。

    不从 60 个点里"凑面"(3.4 万个三元组逐个判定法线, 起球会卡好几秒), 而是**从二十面体截**:
      · 每条棱上取 1/3 与 2/3 两个截点(共 12×5=60 个);
      · 五边形 = 某个顶点周围那 5 个 1/3 点(邻居按环绕角排序);
      · 六边形 = 某个三角面三条棱上的 6 个截点。
    面与棱都是构造出来的, 没有数值判定误差。
    """
    v, e, f = _ico_geo()
    adj = {i: set() for i in range(len(v))}
    for i, j in e:
        adj[i].add(j)
        adj[j].add(i)
    P, pts = {}, []

    def pid(i, j):
        if (i, j) not in P:
            P[(i, j)] = len(pts)
            pts.append(tuple(v[i][k] + (v[j][k] - v[i][k]) / 3.0 for k in range(3)))
        return P[(i, j)]

    for i, j in e:
        pid(i, j)
        pid(j, i)
    pents = []
    for i in range(len(v)):
        nb, base, n0 = list(adj[i]), v[i], v[list(adj[i])[0]]
        e1 = _vsub(n0, base)
        e2 = _vcross(e1, base)          # 面内基底 → 按极角排出邻居的环绕顺序
        nb.sort(key=lambda j: math.atan2(_vdot(_vsub(v[j], base), e2),
                                         _vdot(_vsub(v[j], base), e1)))
        pents.append([pid(i, j) for j in nb])
    hexs = [[pid(a, b), pid(b, a), pid(b, c), pid(c, b), pid(c, a), pid(a, c)]
            for (a, b, c) in f]
    faces = pents + hexs
    ed = set()
    for face in faces:
        for k in range(len(face)):
            x, y = face[k], face[(k + 1) % len(face)]
            ed.add((min(x, y), max(x, y)))
    return pts, sorted(ed), faces


def _prep_solid(v, f):
    """顶点归一化到单位球 + 每个面排成「从外面看逆时针」并给出外向法线。

    shader 的面表只给顶点集合, 顺序不定; 画多边形必须按环绕顺序连, 判正/背面又必须靠法线,
    所以统一在这里做一次: 建面内基底按极角排序 → 法线朝内就整体反转。
    """
    nv = [_vnorm(p) for p in v]
    faces, nrm = [], []
    for face in f:
        c = tuple(sum(nv[i][k] for i in face) / float(len(face)) for k in range(3))
        n = _vcross(_vsub(nv[face[1]], nv[face[0]]), _vsub(nv[face[2]], nv[face[0]]))
        if _vdot(n, c) < 0:                       # 法线朝内 → 反向
            face = tuple(reversed(face))
            n = (-n[0], -n[1], -n[2])
        n = _vnorm(n)
        e1 = _vnorm(_vsub(nv[face[0]], c))
        e2 = _vcross(n, e1)
        face = tuple(sorted(face, key=lambda i: math.atan2(_vdot(_vsub(nv[i], c), e2),
                                                           _vdot(_vsub(nv[i], c), e1))))
        faces.append(face)
        nrm.append(n)
    return nv, faces, nrm


def _solid_geo():
    """当前要画的实心体: (单位顶点, 棱表, 面表(逆时针朝外), 面向外法线, 每条棱所在的面)。

    最后一项是给**隐藏线消除**用的: 一条棱被几个"正面朝前的面"用到 ——
    2 个 = 内部前棱; 1 个 = **轮廓棱**(剪影, 最亮最粗); 0 个 = 背面棱(最暗最细)。
    凸体靠这个就能得到正确的遮挡关系, 不需要画任何填充面。
    """
    if _BALL_SOLID not in _SOLID_CACHE:
        if _BALL_SOLID == "dode":
            v, e, f = _dode_solid_geo()
        elif _BALL_SOLID == "ico":
            v, e, f = _ico_geo()
        else:
            v, e, f = _trunc_ico_geo()
        v, f, n = _prep_solid(v, f)
        ef = {}
        for fi, face in enumerate(f):
            for k in range(len(face)):
                a, b = face[k], face[(k + 1) % len(face)]
                ef.setdefault((min(a, b), max(a, b)), []).append(fi)
        efaces = [ef.get((min(i, j), max(i, j)), []) for i, j in e]
        _SOLID_CACHE[_BALL_SOLID] = (v, e, f, n, efaces)
    return _SOLID_CACHE[_BALL_SOLID]


def _rot3(p, ax, ay, az):
    """三轴旋转。照那份 shader 的转法(X/Y/Z 同时缓慢翻滚): 只绕一根轴转像"转盘",
    三轴一起转才有"全息体浮在空气里翻"的立体感。"""
    x, y, z = p
    ca, sa = math.cos(ax), math.sin(ax)
    cb, sb = math.cos(ay), math.sin(ay)
    cc, sc = math.cos(az), math.sin(az)
    x1, z1 = x * cb + z * sb, -x * sb + z * cb
    y1, z2 = y * ca - z1 * sa, y * sa + z1 * ca
    return (x1 * cc - y1 * sc, x1 * sc + y1 * cc, z2)


def _solid_project(pts, ax, ay, az, cx, cy, r, persp=0.24, radii=None):
    """三轴旋转 + 透视投影 → [(sx, sy, depth, rx, ry, rz)]。

    depth ∈ [-1,1], 1 = 离观察者最近 —— 后面所有"近亮远暗"都靠它(那份 shader 的
    applyDepthBrightness/applyDepthColor 就是这套)。返回旋转后的三维坐标是为了算法线光照,
    没有它面就只能靠深度涂色, 看着像塑料片而不是有体积的实体。
    radii 是逐顶点的半径系数(顶点呼吸波动用), None 就都取 r。
    """
    out = []
    for i, p in enumerate(pts):
        rr = r * (radii[i] if radii else 1.0)
        rx, ry, rz = _rot3(p, ax, ay, az)
        f = 1.0 / (1.0 - persp * rz) if persp else 1.0
        out.append((cx + rx * rr * f, cy + ry * rr * f, rz, rx, ry, rz))
    return out


def _hull2d(pts):
    """二维凸包(单调链)。线框的**外泛光**要贴着实心体轮廓往外散: 逐条棱铺光晕太贵
    (30 棱 × 3 层 = 90 个图元), 一个凸包多边形铺 4 层就够, 而且形状天然是"光的轮廓"。"""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and ((h[-1][0] - h[-2][0]) * (p[1] - h[-2][1])
                                   - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0])) <= 0:
                h.pop()
            h.append(p)
        return h
    lo = half(pts)
    up = half(list(reversed(pts)))
    return lo[:-1] + up[:-1]


_DODE_CACHE = []


def _dode_geo():
    """正十二面体: 20 顶点 / 30 边。

    顶点用标准坐标(8 个 (±1,±1,±1) + 3 组各 4 个黄金比例坐标); 边不手写表, 直接按
    「两点距离 = 棱长 2/φ」把所有棱筛出来 —— 手写 30 条边太容易错一位。
    """
    if _DODE_CACHE:
        return _DODE_CACHE[0]
    phi = (1 + 5 ** 0.5) / 2
    v = [(a, b, c) for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)]
    for a in (-1, 1):
        for b in (-1, 1):
            v += [(0, a / phi, b * phi), (a / phi, b * phi, 0), (a * phi, 0, b / phi)]
    el, e = 2.0 / phi, []
    for i in range(len(v)):
        for j in range(i + 1, len(v)):
            if abs(math.dist(v[i], v[j]) - el) < 1e-6:
                e.append((i, j))
    _DODE_CACHE.append((v, e))
    return _DODE_CACHE[0]


def _dode_project(pts, ax, ay, cx, cy, r, persp=0.18):
    """绕 Y/X 旋转 + 透视投影。返回 [(sx, sy, depth)], depth∈[-1,1], 1 = 离观察者最近。

    depth 是后面做**深度亮度衰减**用的: 近处的棱画亮、远处的棱画暗, 这是那份全息 shader
    里 applyDepthBrightness 的核心, 也是让线框看起来有体积感(而不是一堆平面线条)的关键。
    """
    ca, sa = math.cos(ax), math.sin(ax)
    cb, sb = math.cos(ay), math.sin(ay)
    out = []
    for x, y, z in pts:
        x1, z1 = x * cb + z * sb, -x * sb + z * cb
        y1, z2 = y * ca - z1 * sa, y * sa + z1 * ca
        f = 1.0 / (1.0 - persp * z2) if persp else 1.0
        out.append((cx + x1 * r * f, cy + y1 * r * f, z2))
    return out


# (原 _hline_span 已删: 它只为扫描线服务 —— 求水平线与球体轮廓的交集跨度。
#  扫描线移除后没有别的调用点, 留着就是死代码。)


# 全息打光方向: 左上前方(与"泛光最强在左上"的视觉习惯一致)。面亮度 = max(0, N·L),
# 没有它面只能按深度涂色, 看着像塑料片而不是有体积的实体。
_HOLO_LIGHT = _vnorm((-0.42, -0.58, 0.70))


def _vin_step(prev, now, inside, l, r, px=0, py=0, click_s=0.8, move_px=5):
    """悬浮球的**轮询式**输入状态机(纯函数, 便于单测): (上一状态, 本帧输入) → (动作, 新状态)。

    为什么需要它: 球窗是颜色键控的异形窗, **透明像素穿透点击**(实测圆内只有 15.6% 的点能落到
    球上, 其余全穿到桌面)。以前球是实心装甲球所以不觉得, 换成镂空线框后"点不到"就成了主要问题。
    消息既然根本不会送到那些像素上, 就自己读全局键态判按下/抬起。

    动作语义:
      "press" 指针在命中圆内按下(开始可能的拖拽/单击)
      "drag"  按住并移动超过 move_px
      "click" 按住→抬起, 没移动且耗时 < click_s  → 单击(展开)
      "drop"  按住→抬起, 但移动过(拖拽结束: 落盘 + 判贴边收起)
      "rmenu" 右键按下且指针在命中圆内
      None    什么都不做
    状态字段: down(按住) / moved(移动过) / t(按下时刻) / ox,oy(按下时的指针) / r(右键已处理)
              pl(上一帧左键态 —— 用来区分"在球内刚按下"和"在别处按着拖过来")
    """
    s2 = dict(prev)
    fresh = l and not prev.get("pl")      # 左键**这一帧刚按下**(而不是一直按着)
    s2["pl"] = l
    if fresh and not prev["down"]:
        if not inside:
            return None, s2
        s2.update(down=True, moved=False, t=now, ox=px, oy=py)
        return "press", s2
    if prev["down"]:
        if l:
            if not s2["moved"] and abs(px - s2["ox"]) + abs(py - s2["oy"]) > move_px:
                s2["moved"] = True
            return ("drag" if s2["moved"] else None), s2
        s2["down"] = False
        if s2["moved"]:
            return "drop", s2
        return ("click" if now - prev["t"] < click_s else "drop"), s2
    if r and not prev["r"]:
        s2["r"] = True
        return ("rmenu" if inside else None), s2
    if not r:
        s2["r"] = False
    return None, s2


def _ball_palette(accent, light):
    """面板配色: 跟随主界面强调色/深浅主题(主界面切换时经 /api/floatball 推送到这里)。
    除了原有的 bg/card/txt/sub/line/input/accent, 再多派生几个球体与阴影要用的色:
    ac_lit/ac_dark 是强调色的亮面与暗面(球体渐变), ring 是描边, glow 是光晕,
    hover 是悬停时的整体提亮色, danger 是「隐藏/退出」这类破坏性动作的提示色。

    后面这一组(armor/well/mark/glow_src/holo/core/deep/cool/warm)是**全息线框**专用的:
      armor/well 面的填充底色(远面用 armor, 近面用 well, 都压得很暗, 只负责"挡住背后的棱")
      mark     顶点节点(线框的 12 个顶点)的颜色。**浅色主题下必须是强调色**: 原来固定用白色,
               球底变浅之后白点等于看不见
      glow_src 外泛光(轮廓光晕)的源色。**用强调色本身而不是混向背景** —— 混向背景在浅色桌面上
               是一圈脏点、深色桌面上又看不见; 饱和色两种桌面上都读得出"在发光"
      holo     棱的基色(亮面强调色), 泛光的中间层也用它
      core     棱芯/最近棱的颜色(接近白), 泛光最亮的那层
      deep     最远那批棱的颜色(压到接近黑), 深度差全靠它和 core 拉出来
      cool/warm 深度色调偏移: 远=偏冷(青), 近=偏暖 —— 直接抄那份 shader 的 applyDepthColor
    """
    ac_lit = _hex_mix(accent, "#ffffff", 0.34)
    ac_dark = _hex_mix(accent, "#000000", 0.24)
    if light:
        # 浅色主题: 桌面是浅的, 线框要**压深**才看得见(强调色本身做棱色), 光晕才用浅色
        return {"bg": "#f2f4f9", "card": "#ffffff", "txt": "#20242f", "sub": "#5d6675",
                "line": "#dfe3ec", "input": "#f4f6fa", "accent": accent, "light": True,
                "ac_lit": ac_lit, "ac_dark": ac_dark,
                "ring": _hex_mix(accent, "#ffffff", 0.55), "glow": _hex_mix(accent, "#f2f4f9", 0.72),
                "hover": _hex_mix(accent, "#000000", 0.1), "danger": "#d9534f",
                "armor": _hex_mix(accent, "#ffffff", 0.86),
                "well": _hex_mix(accent, "#ffffff", 0.94),
                "mark": accent, "glow_src": _hex_mix(accent, "#ffffff", 0.30),
                "holo": _hex_mix(accent, "#000000", 0.12),
                "core": _hex_mix(accent, "#000000", 0.34),
                "deep": _hex_mix(accent, "#ffffff", 0.62),
                "cool": _hex_mix(accent, "#2f7fd0", 0.40),
                "warm": _hex_mix(accent, "#d2761f", 0.22)}
    return {"bg": "#161920", "card": "#1a1d24", "txt": "#e8eaf0", "sub": "#8a91a0",
            "line": "#2a2e38", "input": "#0d0f14", "accent": accent, "light": False,
            "ac_lit": ac_lit, "ac_dark": ac_dark,
            "ring": _hex_mix(accent, "#ffffff", 0.3), "glow": _hex_mix(accent, "#161920", 0.78),
            "hover": _hex_mix(accent, "#ffffff", 0.14), "danger": "#f56c6c",
            "armor": _hex_mix(accent, "#0b0d12", 0.62),
            "well": _hex_mix(accent, "#05060a", 0.80),
            "mark": "#ffffff", "glow_src": accent,
            "holo": _hex_mix(accent, "#ffffff", 0.42),
            "core": _hex_mix(accent, "#ffffff", 0.78),
            "deep": _hex_mix(accent, "#05060a", 0.72),
            "cool": _hex_mix(accent, "#6fd6ff", 0.45),
            "warm": _hex_mix(accent, "#ffcf8a", 0.35)}


def _ball_theme(port):
    try:
        with local_open("http://127.0.0.1:%d/api/floatball" % port, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        acc = d.get("accent") or _BALL_ACCENT
        if not re.match(r"^#[0-9a-fA-F]{6}$", acc):
            acc = _BALL_ACCENT
        return _ball_palette(acc, d.get("light") is True)
    except Exception:
        return _ball_palette(_BALL_ACCENT, False)


def _ball_prefs_fetch(port):
    """取服务端已存的悬浮窗档位(思考/身份/风格/条数/悬停)。失败返回空字典。"""
    try:
        with local_open("http://127.0.0.1:%d/api/floatball" % port, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        p = d.get("prefs") or {}
        return {k: str(v) for k, v in p.items() if v not in (None, "")}
    except Exception:
        return {}


def _personas_fetch(port):
    """取用户自定义人设预设列表。失败返回空列表(界面上就只显示内置身份)。"""
    try:
        with local_open("http://127.0.0.1:%d/api/personas" % port, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d.get("items") or []
    except Exception:
        return []


def _ball_worker(port, mode, text, holder, seg=None):
    """后台线程: 调 /api/ai/assist 并解析 SSE, 事件投递给 tkinter 主线程(tkinter 非线程安全)。
    seg 非空时只重跑该段(「换一批」)。"""
    try:
        payload = {"mode": mode, "text": text}
        if seg:
            payload["seg"] = seg
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/ai/assist" % port,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with local_open(req, timeout=180) as resp:
            if "json" in (resp.headers.get("Content-Type") or ""):
                # AI 未配置等场景: 后端回普通 JSON 而非 SSE
                try:
                    d = json.loads(resp.read().decode("utf-8"))
                    _EVQ.put(("error", holder, d.get("error") or "请求失败"))
                except Exception:
                    _EVQ.put(("error", holder, "请求失败"))
                return
            buf = ""
            for raw in resp:
                buf += raw.decode("utf-8", "replace")
                while "\n\n" in buf:
                    chunk, buf = buf.split("\n\n", 1)
                    ev, data = None, {}
                    for ln in chunk.split("\n"):
                        if ln.startswith("event:"):
                            ev = ln[6:].strip()
                        elif ln.startswith("data:"):
                            try:
                                data = json.loads(ln[5:].strip())
                            except Exception:
                                pass
                    if ev == "phase":
                        _EVQ.put(("phase", holder, data.get("label") or ""))
                    elif ev == "seg":
                        _EVQ.put(("seg", holder, data))   # 每段生成完立刻推给界面
                    elif ev == "done":
                        _EVQ.put(("done", holder, data))
    except Exception as e:
        _EVQ.put(("error", holder, str(e)[:160]))


def run_float_ball(port):
    """悬浮球主循环: 置顶小球单击后**原地展开**成 AI 对话面板(聊天流+输入框), 可再收回成球。
    可拖拽(位置记忆), 服务失联自动销毁。测试钩子: --ball-autoclose <毫秒>。"""
    import tkinter as tk
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 高分屏不糊
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    root.title("洞鉴虚明 AI")
    root.overrideredirect(True)          # 无边框(球与面板都是)
    for attr, val in (("-topmost", True), ("-alpha", 0.97),
                      ("-transparentcolor", _BALL_TRANSPARENT)):
        try:
            root.attributes(attr, val)
        except Exception:
            pass
    try:
        scale = max(1.0, root.winfo_fpixels("1i") / 96.0)   # 像素尺寸按 DPI 缩放(字体是磅值会自动缩放)
    except Exception:
        scale = 1.0
    BALL = int(_BALL_SIZE * scale)
    PW, PH = int(_BALL_PANEL_W * scale), int(_BALL_PANEL_H * scale)
    RB = BALL * 0.270   # 「球体」半径: 收边/定位/拖拽钳制都按它算球体的边(与绘制无关)
    RF = BALL * 0.400   # 全息外圈弧的半径(悬停/生成时的两段锁定弧贴在这里)
    # 窗口四边到球体外缘的透明边距。窗口比球体大这一圈, 于是所有「球应该在哪」的换算
    # (默认落点/拖拽钳制/贴边判据/展开对齐)都得把 PAD 补回来 —— 直接沿用旧公式会让球
    # 系统性偏移 PAD 像素(实测 PAD≈39 物理px, 肉眼看得出来"没回到位")。
    PAD = BALL / 2.0 - RB

    def _ball_win(sw):
        """让**球体本身**离屏幕右缘 96px 的窗口 x。

        96 这个数是修「找不到球」时定的: 贴到最右缘那条会被任务栏通知区/置顶小窗盖住。
        换成窗口坐标后要减去 PAD, 否则球实际会离边缘 96+PAD ≈ 135px, 比原来远一截。
        """
        return int(max(0, sw - 96 - BALL + PAD))

    def _ball_top(sh, frac=0.28):
        """让**球体本身**落在屏幕上 frac 处的窗口 y(默认位置/定位都用它)。"""
        return int(max(0, min(sh * frac - PAD, sh - BALL)))
    C = _ball_theme(port)   # 当前配色(展开时刷新)

    # ---------------- 容器: 球(圆形画布) ⇄ 面板(聊天) 共用同一个置顶窗口 ----------------
    container = tk.Frame(root, bg=_BALL_TRANSPARENT, highlightthickness=0, bd=0)
    container.pack(fill="both", expand=True)
    ballcv = tk.Canvas(container, width=BALL, height=BALL, bg=_BALL_TRANSPARENT,
                       highlightthickness=0)

    # 球体画法(当前形态 = 「正二十面体线框全息」): 真三维顶点/面/棱 + 深度排序 + 轮廓泛光。
    # 全部几何在 _holo_draw 里每帧重画 —— 它在转, 没有静态层可留(旧版"装甲/发光静态 + 环阵动态"
    # 的分层是因为那时只有环在动; 现在整颗都在转, 分层没有意义)。
    #
    # 特效在 tkinter 里的三条可行路径(都实测过, 见 _probe_fx.py):
    #   ① stipple(gray12/25/50/75) + -transparentcolor = 逐像素真半透明, 不依赖任何 Win32 调用。
    #      实测 gray75/gray50 盖在纯灰上的均值与理论 alpha 混合只差 ±2/255 —— 所以光晕、面填充、
    #      扫描线全用抖动做, 这是本方案最核心的一招(也是"泛光"在没有模糊的前提下唯一的实现方式)。
    #   ② Canvas 没有 alpha 通道、没有渐变、没有模糊、没有 shader, 所以「径向渐变」只能靠
    #      同心多边形一层层叠出来, 不能指望 fill 支持渐变。
    #   ③ SetWindowCompositionAttribute 的亚克力(真毛玻璃)与颜色键控**互相冲突**: 一开亚克力,
    #      -transparentcolor 就失效, 整个窗口变成不透明矩形(实测方差从 16217 掉到 13, 说明
    #      背景被整块盖住而不是透出来)。球的异形透明全靠颜色键控, 所以毛玻璃只能放弃 ——
    #      要真模糊就走「截屏 → StretchBlt HALFTONE 降采样再升采样」, 那是面板那边的事。
    def _ball_skin_state():
        """当前该用哪套球体皮肤。优先级: 拖拽 > 生成中 > 有未读 > 悬停 > 静止。

        集中算一处, 而不是让 press/done/poll 各自去改状态 —— 那些地方互相覆盖, 迟早会出现
        「结果都出来了球还在转圈」这种状态残留。
        """
        if st.get("skin_force"):   # 测试钩子 --ball-skin-demo 用: 直接钉住某个状态
            return st["skin_force"]
        if st.get("drag"):
            return "drag"
        if sending["on"]:
            return "busy"
        if st.get("unread"):
            return "unread"
        if st.get("hover"):
            return "hover"
        return "idle"

    def sync_skin():
        """把皮肤状态同步成当前该有的样子; 变了才重画 fx 层。"""
        sk = _ball_skin_state()
        if sk != st.get("skin"):
            st["skin"] = sk
            if st["view"] == "ball" and not st["hidden"]:
                try:
                    draw_ball_fx()
                except Exception:
                    pass
        return sk

    # ---------------- 球体: 正二十面体线框全息(真三维, 整颗每帧重画) ----------------
    def _holo_state():
        """皮肤状态 → 全息参数。集中一处: 以前各状态分散在各自分支里改绘制, 迟早互相覆盖。"""
        return {
            # idle 的 spin 从 0.30 降到 0.18: 转速慢下来之后, 提帧率才看得出"稳"而不是"快"。
            # 两者要一起动 —— 只提帧率会变成"转得更快的匀速转盘", 只降转速会变成"一卡一卡的慢转"。
            "idle":   {"spin": 0.18, "br": 0.88, "glow": 0.78, "wave": 0.030},
            "hover":  {"spin": 0.60, "br": 1.06, "glow": 1.05, "wave": 0.045},
            "busy":   {"spin": 1.02, "br": 1.00, "glow": 1.00, "wave": 0.055},
            "unread": {"spin": 0.20, "br": 0.94, "glow": 0.86, "wave": 0.035},
            "drag":   {"spin": 0.52, "br": 1.06, "glow": 1.10, "wave": 0.038},
        }[st.get("skin", "idle")]

    _GLYPH_SCALE = 1.3   # 中心徽记放大系数: 原半径约 12 物理px, 在 167px 的球上偏小,
    # 扫一眼认不出"这是个标记"。菱形/涟漪/四角括号一律乘它, 保持同一套构造比例。

    def _core_glyph(cx, cy, br, t, dim, fl):
        """中心徽记。形态取自方舟系 identity glyph: 嵌套菱形 + 横贯亮杠 + 四角刻度 + 菱形涟漪。

        画序有讲究(自下而上): 涟漪 → 外菱形 → **键色挖洞** → 内核实心 → 横杠 → 四角刻度。
        · 挖洞用窗口键色填: 键控窗里这就是真透明, 顺带把穿过徽记的线框棱清干净(旧版"内井"同招);
        · 横杠放在洞**之后**画, 否则会被洞一起挖掉;
        · 斜线一律做成**实心菱形**而不是 1px 描边: 键控下斜线没有抗锯齿, 描边会成锯齿。
        """
        g = st.get("core", _BALL_CORE)
        if g == "none":
            return
        R = BALL * 0.070 * _GLYPH_SCALE       # 外菱形半径(×1.3; 原约 12 物理px, 与官方 14px 标记同量级)
        bright = min(1.35, br + fl)
        lit = dim(C["core"], 1.0 - min(1.0, 0.50 + 0.50 * bright))
        mid = dim(C["holo"], 1.0 - min(1.0, 0.42 + 0.46 * bright))
        glow = dim(C["glow_src"], 0.68 - 0.34 * min(1.0, bright))

        def dia(r, **kw):
            ballcv.create_polygon(cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy, **kw)

        # 中心泛光: 两层抖动菱形(比描边便宜, 也不会出现锯齿斜线)。
        # **别做大**: 试过 2.7R/1.8R, 铺出来是一大块点阵, 把穿过中心的线框棱全压没了 ——
        # 中心是"标记"不是"面板"。瞄具档更是只要一层。
        for k, stp in (((1.95, "gray12"), (1.45, "gray25")) if g == "reticle"
                       else ((1.95, "gray12"), (1.45, "gray25"), (1.15, "gray50"))):
            dia(R * k, fill=glow, outline="", stipple=stp, tags="ball_fx")
        if g == "reticle":     # 四角**不相接**的括号 = 瞄具语言；闭合框 + 中心徽记才像奖章
            L, W = BALL * 0.052 * _GLYPH_SCALE, max(1, int(BALL * 0.010 * _GLYPH_SCALE))
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                x0, y0 = cx + sx * R * 1.02, cy + sy * R * 1.02
                ballcv.create_line(x0, y0, x0 + sx * L, y0, fill=mid, width=W, tags="ball_fx")
                ballcv.create_line(x0, y0, x0, y0 + sy * L, fill=mid, width=W, tags="ball_fx")
            dia(R * 0.30, fill=lit, outline="", tags="ball_fx")
            return
        # 菱形涟漪: 每 ~1.6s 生一个环(向外扩 + 淡出)。**不加挖洞** —— 挖洞会把球体线框一起清掉,
        # 用抖动填充就够读出"波纹"了(而且更便宜)。
        ph = (t * (0.85 if st.get("skin") == "busy" else 0.52)) % 1.0
        # 涟漪最外圈**锁死在放大前的绝对半径**(BALL*0.203): 徽记放大是为了让"标记"更醒目,
        # 不是让它的涟漪跟着一起长大 —— 涨到 2.9R 就快贴上球体轮廓(RB≈0.27*BALL),
        # 抖动点阵会糊到线框棱上, 中心看着就脏了。
        dia(min(R * (1.05 + 1.85 * ph), BALL * 0.203),
            fill=dim(C["glow_src"], 0.30 + 0.55 * ph),
            outline="", stipple="gray25" if ph < 0.45 else "gray12", tags="ball_fx")
        # 嵌套菱形: 外实心 → 键色挖洞 → 内核实心。**内环是断开的**(左右两个人字),
        # 不是闭合的一圈: 同心闭合环 + 中心点正好是奖章/硬币的构造, 读起来像徽章。
        dia(R, fill=mid, outline="", tags="ball_fx")
        hr = R * 0.40
        for sx in (-1, 1):
            ballcv.create_polygon(cx + sx * R * 0.60, cy, cx, cy - hr, cx, cy + hr,
                                  fill=_BALL_TRANSPARENT, outline="", tags="ball_fx")
        dia(R * 0.30, fill=lit, outline="", tags="ball_fx")
        if g != "ripple":
            # 横贯亮杠: 「鉴」那个意象, 也让徽记不是纯旋转对称(对称=徽章感)
            ballcv.create_line(cx - R * 1.60, cy, cx + R * 1.60, cy, fill=lit,
                               width=max(1, int(BALL * 0.009)), tags="ball_fx")
            # 四角刻度: 轴对齐小方块 —— 斜置刻度在这个尺寸会糊
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                q = BALL * 0.011
                px, py = cx + dx * R * 1.36, cy + dy * R * 1.36
                ballcv.create_rectangle(px - q, py - q, px + q, py + q,
                                        fill=mid, outline="", tags="ball_fx")

    def _holo_draw():
        """画整颗全息球。

        画序每一步都有理由, 别随手调:
          ① 轮廓泛光: 沿实心体凸包描 3 层**粗抖动线**(不是填充)。填充版实测会把整个轮廓罩成
             一块点阵圆盘, 线框立刻糊掉 —— 泛光要"从轮廓往外散", 不是"给球垫个底"。
          ② 背面: 面极淡填充 + 棱一条暗虚线(偏冷)。全息必须能看穿, 但背面必须明显更暗,
             深浅一拉开, 体积感就出来了(那份 shader 的 applyDepthBrightness)。
          ③ 正面面: 按法线·光向分 4 档**平坦亮度桶**(不是连续渐变: 112px 下 4 档读起来更立体,
             连续插值反而糊成一片)。面把背面的棱压下去 —— 这才是 30 条棱在小尺寸下能读出来的原因。
          ④ 正面棱: 三层描边(宽而淡的光晕 / 中间层 / 近白棱芯)。**近白棱芯是"在发光"的关键**:
             眼睛会把最亮最细的那条当成光源, 外面宽的淡层是它的溢出。这就是双半径辉光
             (text-shadow 0 0 24px + 0 0 60px)在没有模糊时等价的做法。
          ⑤ 顶点节点: 完整菱形亮点(近大远小), 呼应方舟那套"菱形身份标记"。
          ⑥ 扫描线**已整块移除**(横贯球体的虚线在浅色底上像"屏幕脏了", 且每帧求交白搭 CPU);
             顶替它的是 ①b 的半透明体色 —— 白底上真正救场的是体色, 不是再多加几根线。
          ⑦ 状态特效: 悬停/生成时才出现的两段**断开**外弧(断开=仪表读数, 闭合=奖章),
             未读呼吸点, 定位时的轮廓亮边。

        图元数 ≈ 100/帧, 实测 11~22fps 能扛。转速是**匀速**的(不缓动): 缓动转起来像转硬币,
        匀速才像扫描仪。
        """
        ballcv.delete("ball_art")
        ballcv.delete("ball_fx")
        prof = _BALL_PROF[0]
        if prof:
            _BALL_PROF[1] += 1
            _t_prof = time.perf_counter()
        sk = st.get("skin", "idle")
        P = _holo_state()
        t = st.get("skin_t", 0.0)
        fl = st.get("flash", 0.0)              # 「定位悬浮球」的闪三下: 叠在亮度上
        DIM = "#f2f4f9" if C.get("light") else "#000000"

        def dim(col, k):
            """压暗(深色主题混向黑) / 压淡(浅色主题混向白) —— 同一套参数两种主题都成立。"""
            return _hex_mix(col, DIM, max(0.0, min(1.0, k)))

        br = min(1.7, P["br"] + fl)
        # 全息闪烁: 两层不同频率的正弦叠加, 避免"呼吸"那种规律感(规律=假)
        flick = 0.93 + 0.05 * math.sin(t * 9.3) + 0.02 * math.sin(t * 23.7)
        v, ed, faces, fnorm, efaces = _solid_geo()
        cx = cy = BALL / 2.0
        RS = BALL * 0.340          # 实心体外接半径(外面还要留一圈泛光, 不能顶满窗口)
        # 顶点呼吸(照 shader 的 _WaveAmount): 顶点各自轻微起伏, 线框就"活"了
        radii = [1.0 + P["wave"] * math.sin(t * 1.6 + i * 1.9) for i in range(len(v))]
        # 三轴翻滚: 起步角与转速比照 shader(45°/30°/15° 起, 30/45/20 的转速比)
        a = t * P["spin"]
        ax, ay, az = 0.62 + a * 0.52, 0.42 + a * 0.86, 0.26 + a * 0.36
        proj = _solid_project(v, ax, ay, az, cx, cy, RS, radii=radii)
        # 信号抖动: 每 ~2.4s 抽一下(横向错位 1~2px), 只加在正面棱的中间层上 —— 整颗乱抖会晕
        gph = (t * 0.42) % 1.0
        gx = (BALL * 0.016 * math.sin(t * 41.0)) if gph < 0.05 else 0.0

        # ① 轮廓只用来给扫描线求跨度, 不画描边: 沿凸包画的"泛光边"和真实线框轮廓错开一层,
        #    读起来就是一圈重影描边(而且是闭合的 —— 闭合描边 = 奖章/币边, 用户上一轮骂的正是这个)。
        #    泛光全部交给下面**逐棱**的多层描边, 光才是从结构本身发出来的。
        hull = _hull2d([(p[0], p[1]) for p in proj])
        # 这两个开关必须在**所有绘制之前**定下来: 体色层要用 lean 判极简档, 而画序后面的
        # 棱/顶点也用它们 —— 以前它们定义在 ② 之后, 体色层一插到前面就 UnboundLocalError
        # (第 10548 行用 lean, 第 10565 行才赋值)。画序敏感的函数里, 凡是多层共用的开关
        # 一律提到最前面, 别让"后面插一层"变成炸弹。
        idle = (sk == "idle") or (sk == "unread") or _BALL_LEAN[0]
        lean = _BALL_LEAN[0]

        # ---- ①b 体色: 交给**独立的玻璃体层**(见 _glass_make), 这里不画 ----
        #     键控窗只有"全透明/全不透明"两档; 用 stipple 抖动铺出来的"半透明"本质是一堆点阵
        #     圆点, 浅色桌面上尤其明显(用户点名不要)。真半透明由垫在球下面、带 LWA_ALPHA 的那
        #     层窗提供: 圆外键色真透明, 圆内由合成器做 alpha 混合; 线框这层保持完全不透明,
        #     近白棱芯才不会被体色一起冲淡。

        # ---- ② 棱分三类 + 隐藏线消除 ----
        #      这里**不铺任何面**: 桌面是透明的, 抖动填充铺满轮廓就成一块点阵圆盘(上一版正是
        #      被这个读成"钱币")。凸体的遮挡关系只用"棱属于几个朝前的面"就能算出来:
        #        nf=2 内部前棱(中等) · nf=1 **轮廓棱**(剪影, 最亮最粗) · nf=0 背面棱(最暗最细)。
        #      轮廓比内部线更亮更粗, 是"看起来像个实体"的关键 —— 只画均匀线框会糊成一团。
        front = [_rot3(fnorm[fi], ax, ay, az)[2] > 0 for fi in range(len(faces))]
        edraw = []
        for ei, (i, j) in enumerate(ed):
            nf = sum(1 for fi in efaces[ei] if front[fi])
            edraw.append(((proj[i][2] + proj[j][2]) / 2.0, proj[i], proj[j],
                          2 if nf == 1 else (1 if nf == 2 else 0)))
        for d, p1, p2, cl in sorted([e for e in edraw if e[3] == 0], key=lambda x: x[0]):
            # 背棱始终画: 能看穿背面才是"全息投影"而不是"实体网格"。实测这套 1px 抖动线很便宜
            # (整个极简档也只省 0.5% CPU), 砍它换不到性能, 只会让球看着变实心。
            # **实线而不是抖动线**: 实测 45 条 1px 抖动线要多吃 ~14% 单核(Tk 把抖动当画刷逐像素铺),
            # 换成实线几乎免费, 而 1px 暗线本来也读不出"点阵感"。
            ballcv.create_line(p1[0], p1[1], p2[0], p2[1],
                               fill=dim(_hex_mix(C["deep"], C["cool"], 0.50),
                                        0.34 - 0.22 * d), width=1, tags="ball_fx")

        # ---- ③ 正面棱: 远→近, 四层描边(泛光阶梯) ----
        #    宽度阶梯 9px gray12 → 5px gray25 → 2.5px gray50 → 1px 近白实线。
        #    **近白棱芯是"在发光"的关键**: 眼睛把最亮最细的那条当光源, 外面宽的淡层是它的溢出。
        #    这就是双半径辉光(text-shadow 0 0 24px + 0 0 60px)在"没有模糊"时的等价实现。
        w_core = max(1.0, BALL * 0.0068)
        for d, p1, p2, cl in sorted([e for e in edraw if e[3] > 0], key=lambda x: x[0]):
            near = min(1.0, max(0.0, 0.28 + 0.72 * d))
            sil = (cl == 2)                      # 轮廓棱: 更亮更粗(它是剪影)
            k = min(1.5, (near + (0.28 if sil else 0.0)) * br * flick)
            core = _hex_mix(_hex_mix(C["holo"], C["core"], 0.15 + 0.85 * near),
                            "#ffffff", 0.48 * near * near)
            glow = _hex_mix(C["glow_src"], C["cool"], 0.30 + 0.34 * near)
            if sil and not lean:
                ballcv.create_line(p1[0], p1[1], p2[0], p2[1],
                                   fill=dim(glow, 0.24 - 0.20 * min(1.0, P["glow"])),
                                   width=max(2.0, w_core * (5.4 if idle else 7.0)),
                                   stipple="gray12", tags="ball_fx")
                if not idle:   # 轮廓的第二层光晕只在交互态出现(静止时一层就够读)
                    ballcv.create_line(p1[0], p1[1], p2[0], p2[1],
                                       fill=dim(glow, 0.16 - 0.12 * min(1.0, P["glow"])),
                                       width=max(2.0, w_core * 3.4), stipple="gray25",
                                       tags="ball_fx")
            if sil or not idle:
                ballcv.create_line(p1[0] + gx, p1[1], p2[0] + gx, p2[1], fill=glow,
                                   width=max(2.0, w_core * (2.2 if sil else 1.6)),
                                   stipple="gray50", tags="ball_fx")
            ballcv.create_line(p1[0], p1[1], p2[0], p2[1], fill=dim(core, 1.0 - k),
                               width=max(1.0, w_core * (1.35 if sil else 1.0)),
                               tags="ball_fx")

        # ---- ④ 顶点节点: 完整菱形亮点(背面顶点不画: 正面已经挡着了) ----
        for p in proj:
            # 截角体有 60 个顶点, 全画就是一片噪点; 只点最靠前的几颗(静止时更少)
            if p[2] < (0.55 if not idle else 0.80):
                continue
            near = (p[2] + 1.0) / 2.0
            r = BALL * (0.0075 + 0.008 * near) * (1.0 + 0.35 * fl)
            col = dim(_hex_mix(C["mark"], C["core"], 0.35),
                      1.0 - min(1.0, (0.35 + 0.65 * near) * br))
            ballcv.create_polygon(p[0], p[1] - r * 2.5, p[0] + r * 2.5, p[1],
                                  p[0], p[1] + r * 2.5, p[0] - r * 2.5, p[1],
                                  fill=col, outline="", stipple="gray25", tags="ball_fx")
            ballcv.create_polygon(p[0], p[1] - r, p[0] + r, p[1],
                                  p[0], p[1] + r, p[0] - r, p[1],
                                  fill=col, outline="", tags="ball_fx")

        # ⑤ 扫描线整块移除: 横贯球体的一道道虚线在深色壁纸上读起来像"屏幕脏了/有划痕",
        # 而且每帧要跟凸包求交算跨度, 是纯亏的 CPU。编号留空, 下面的 ⑥⑦ 不动。

        # ---- ⑥ 中心徽记 ----
        _core_glyph(cx, cy, br, t, dim, fl)

        # ---- ⑦ 状态特效 ----
        if sk in ("hover", "busy", "drag"):
            # 两段**断开**的长弧(方舟那套"锁定/读数"语言; 断开才不会读成奖章外圈)。
            # 只在有交互时出现: 静止时球是干净的, 也省得一直"发光"抢桌面注意力。
            a0 = (t * (34.0 if sk != "busy" else 90.0)) % 360.0
            for st_a, ext in ((a0, 42), (a0 + 180, 26)):
                ballcv.create_arc(cx - RF * 1.06, cy - RF * 1.06, cx + RF * 1.06,
                                  cy + RF * 1.06, start=st_a, extent=ext, style="arc",
                                  outline=dim(C["core"], 0.15 + 0.25 * (1 - br)),
                                  width=max(1, int(BALL * 0.008)), tags="ball_fx")
        if sk == "unread":      # 右上角呼吸点: 结果回来了但球是收起的, 得让用户看见
            pr = BALL * 0.034
            px, py = cx + RS * 0.80, cy - RS * 0.80
            b = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 2.6))
            ballcv.create_oval(px - pr * 2.2, py - pr * 2.2, px + pr * 2.2, py + pr * 2.2,
                               fill=dim(C["glow_src"], 0.30), outline="",
                               stipple="gray25", tags="ball_fx")
            ballcv.create_oval(px - pr * b, py - pr * b, px + pr * b, py + pr * b,
                               fill=C["core"], outline="", tags="ball_fx")
        if fl > 0.01 and len(hull) >= 3:
            # 定位提示: 沿真实轮廓闪一圈亮边(以前画的八边形会浮在球外面)
            ballcv.create_polygon(*[q for x, y in hull for q in (x, y)], fill="",
                                  outline=C["core"], width=max(2, int(BALL * 0.012)),
                                  tags="ball_fx")
        if prof:
            _BALL_PROF[2] += time.perf_counter() - _t_prof
            _BALL_PROF[3] = len(ballcv.find_all())

    def draw_ball():
        """重画整颗球(启动首帧 / 恢复显示 / 主题变化 / 定位闪烁都走它)。"""
        _holo_draw()

    def draw_ball_fx():
        """每帧重画整颗球 —— 线框在转, 没有静态层可留。名字沿用旧接口, 调用点不用改。"""
        _holo_draw()

    ballcv.pack(fill="both", expand=True)   # 初始形态=球(不打包的话整个窗口全透明, 球"隐身")

    panel = tk.Frame(container, bg=C["bg"])
    # 面板装饰层: 切角边框 + 刻度 + 线框背景(放在最底层, 其他控件叠在上面)
    panel_deco = tk.Canvas(panel, bg=C["bg"], highlightthickness=0)
    panel_deco.place(x=0, y=0, relwidth=1, relheight=1)
    # 窗口尺寸变了重画装饰。delay 一下: Configure 触发时 panel_deco 的 winfo 还是旧值,
    # 直接画会用上一次的宽高, 边框会错位一帧。
    panel.bind("<Configure>", lambda e: panel_deco.after(50, draw_panel_deco))
    header = tk.Frame(panel, bg=C["card"], height=36)
    header.pack(fill="x")
    header.pack_propagate(False)
    # 标题栏两端"切角": Canvas 是唯一能画斜边的东西, 用两个小 Canvas 贴在 header 左右,
    # 画一个底色三角把直角吃掉。比把整条 header 换成 Canvas 简单得多 —— 里面 5 个 Label
    # 的 pack 逻辑一个字都不用动。切角量与 panel_deco 的外框对齐。
    _hch = min(16, int(36 * 0.32))

    def _clip_header(_e=None):
        for _c in (hcut_l, hcut_r):
            _c.delete("all")
            _hh = _c.winfo_height()
            if _hh < 4:
                continue
            if _c is hcut_l:   # 左上角: 保留左下, 斜边吃掉右上
                _c.create_polygon(0, 0, _hch, 0, 0, min(_hch, _hh),
                                  fill=C["bg"], outline="", tags="cut")
            else:              # 右上角: 保留右下, 斜边吃掉左上
                _c.create_polygon(_hch, 0, _hch, min(_hch, _hh), 0, 0,
                                  fill=C["bg"], outline="", tags="cut")
    hcut_l = tk.Canvas(header, width=_hch, height=36, bg=C["card"],
                       highlightthickness=0, bd=0)
    hcut_r = tk.Canvas(header, width=_hch, height=36, bg=C["card"],
                       highlightthickness=0, bd=0)
    hcut_l.pack(side="left", fill="y")
    hcut_r.pack(side="right", fill="y")
    _clip_header()
    header.bind("<Configure>", lambda e: _clip_header())
    dot = tk.Label(header, text="●", bg=C["card"], fg=C["accent"], font=(_FONT, 9))
    dot.pack(side="left", padx=(10, 4))
    title = tk.Label(header, text="AI 助手 · 拖动移动", bg=C["card"],
                     fg=C["txt"], font=(_FONT, 10, "bold"))
    title.pack(side="left")
    # 头部动作: 从左到右「新对话 · ⚙ · ─ 收起」; 每个按钮带悬停提亮(见 _hoverable)
    hide_btn = tk.Label(header, text="✕ 隐藏", bg=C["card"], fg=C["sub"],
                        font=(_FONT, 9), cursor="hand2", padx=8)
    hide_btn.pack(side="right", fill="y")
    collapse_btn = tk.Label(header, text="─ 收起", bg=C["card"], fg=C["sub"],
                            font=(_FONT, 9), cursor="hand2", padx=8)
    collapse_btn.pack(side="right", fill="y")
    prefs_btn = tk.Label(header, text="⚙", bg=C["card"], fg=C["sub"],
                         font=(_FONT, 10), cursor="hand2", padx=7)
    prefs_btn.pack(side="right", fill="y")
    new_btn = tk.Label(header, text="新对话", bg=C["card"], fg=C["sub"],
                       font=(_FONT, 9), cursor="hand2", padx=7)
    new_btn.pack(side="right", fill="y")

    modebar = tk.Frame(panel, bg=C["bg"])
    modebar.pack(fill="x", padx=8, pady=(7, 3))
    mode = {"cur": "verify"}
    mode_btns = {}
    for _key, _label in (("verify", "🔍 验证真实性"), ("rebut", "⚔️ 生成回怼")):
        b = tk.Label(modebar, text=_label, bg=C["card"] if _key != "verify" else C["accent"],
                     fg=C["sub"] if _key != "verify" else "#ffffff", cursor="hand2",
                     font=(_FONT, 9), padx=6, pady=6)
        b.pack(side="left", fill="x", expand=True, padx=3)
        mode_btns[_key] = b

    def style_modes(_e=None):
        for k, w in mode_btns.items():
            on = k == mode["cur"]
            w.config(bg=C["accent"] if on else C["card"],
                     fg="#ffffff" if on else C["sub"])

    def _mode_hover(w, key):
        """模式按钮悬停: 未选中的亮成 accent 的暗面(不抢当前选中态的存在感)。"""
        def _in(_e=None):
            if mode["cur"] != key:
                w.config(bg=C["ac_dark"], fg="#ffffff")
        def _out(_e=None):
            style_modes()
        w.bind("<Enter>", _in)
        w.bind("<Leave>", _out)

    def switch_mode(e, k):
        mode["cur"] = k
        style_modes()
    for k, w in mode_btns.items():
        w.bind("<Button-1>", lambda e, k=k: switch_mode(e, k))
        _mode_hover(w, k)

    # ---- 调节抽屉(⚙): 思考/身份/风格/条数 档位, 点选即存服务端 ----
    # 身份(拿什么当武器)与风格(说话方式)分开, 换风格不用丢身份
    prefs = {"think": "标准", "persona": "不设", "savage": "毒舌律师", "len": "2"}
    prefs.update({k: v for k, v in _ball_prefs_fetch(port).items() if k in prefs})
    plocal = {"items": _personas_fetch(port)}   # 自定义人设预设(本地镜像, 免得每次重建都联网)
    prefbar = tk.Frame(panel, bg=C["bg"])
    segs = tk.Frame(prefbar, bg=C["bg"])   # 档位行独立成框: 重建时不影响下面的提示与工具栏
    segs.pack(fill="x")
    pref_segs = {}

    def _seg_row(label, options, key):
        row = tk.Frame(segs, bg=C["bg"])
        tk.Label(row, text=label, bg=C["bg"], fg=C["sub"], font=(_FONT, 8)).pack(side="left")
        for opt in options:
            b = tk.Label(row, text=opt, bg=C["card"], fg=C["sub"], cursor="hand2",
                         font=(_FONT, 8), padx=4, pady=2)
            b.pack(side="left", padx=(2, 0))
            pref_segs[(key, opt)] = b
            b.bind("<Button-1>", lambda e, k=key, o=opt: set_pref(k, o))
        row.pack(fill="x", padx=6, pady=2)
        return row

    def build_pref_rows():
        """重建档位行: 自定义人设增删后, 身份行要跟着多出/少掉按钮。"""
        for w in segs.winfo_children():
            w.destroy()
        pref_segs.clear()
        names = list(_PERSONA_TIER) + [p["name"] for p in plocal["items"]]
        _seg_row("🧠 思考", ("快", "标准", "深挖"), "think")
        _seg_row("🎭 身份", names, "persona")
        _seg_row("😈 风格", ("冷静理性", "毒舌律师", "阴阳怪气"), "savage")
        _seg_row("📏 条数", ("1", "2", "3"), "len")
        if prefs.get("persona") not in names:
            prefs["persona"] = "不设"   # 人设被删掉了 → 退回不设, 免得没有高亮项
        style_prefs()

    tk.Label(prefbar, text="条数只调节条数：一句话与一段话各 N 条，长篇 1 条；身份决定拿什么当武器",
             bg=C["bg"], fg=C["sub"], font=(_FONT, 7), anchor="w", justify="left",
             wraplength=int(350 * scale)).pack(fill="x", padx=8, pady=(0, 2))

    # ---- 自定义人设(结构化字段存本地, 参考 Persona-Forge: 身份/武器/底线分开填) ----
    def show_personas():
        win = tk.Toplevel(root)
        win.title("✎ 自定义人设")
        win.configure(bg=C["bg"])
        win.geometry("%dx%d" % (int(404 * scale), int(470 * scale)))
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(win, text="人设决定「我」是谁、拿什么当武器。留空则不写进提示词。",
                 bg=C["bg"], fg=C["sub"], font=(_FONT, 8), justify="left",
                 wraplength=int(370 * scale)).pack(fill="x", padx=10, pady=(8, 4))
        body = tk.Frame(win, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=10)
        cv = tk.Canvas(body, bg=C["bg"], highlightthickness=0)
        sb2 = tk.Scrollbar(body, orient="vertical", command=cv.yview, width=10)
        cv.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        inn = tk.Frame(cv, bg=C["bg"])
        cw = cv.create_window((0, 0), anchor="nw", window=inn)
        inn.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(cw, width=e.width))
        cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        entries = []
        cap = [12]

        def add_row(item=None):
            if cap[0] <= 0:
                return
            cap[0] -= 1
            item = item or {}
            card = tk.Frame(inn, bg=C["card"])
            card.pack(fill="x", pady=4)
            ents = {}

            def _line(key, label, value, width=8):
                rr = tk.Frame(card, bg=C["card"])
                rr.pack(fill="x", padx=8, pady=2)
                tk.Label(rr, text=label, bg=C["card"], fg=C["sub"], font=(_FONT, 8),
                         width=width, anchor="w").pack(side="left")
                v = tk.StringVar(value=value)
                tk.Entry(rr, textvariable=v, bg=C["input"], fg=C["txt"], relief="flat",
                         insertbackground=C["txt"], font=(_FONT, 9)).pack(
                    side="left", fill="x", expand=True, ipady=3)
                ents[key] = v

            _line("name", "名字", str(item.get("name") or ""))
            for k, lbl, _lim in _PERSONA_FIELD:
                _line(k, lbl, str(item.get(k) or ""), width=9)
            dl = tk.Label(card, text="删除这个人设", bg=C["card"], fg="#f56c6c",
                          cursor="hand2", font=(_FONT, 8), anchor="e")
            dl.pack(fill="x", padx=8, pady=(2, 6))

            def _del(_e=None, c=card, e=ents):
                cap[0] += 1
                if e in entries:
                    entries.remove(e)
                c.destroy()
            dl.bind("<Button-1>", _del)
            entries.append(ents)
            cv.after_idle(lambda: cv.configure(scrollregion=cv.bbox("all")))

        for it in plocal["items"]:
            add_row(it)
        if not plocal["items"]:
            add_row()

        bar = tk.Frame(win, bg=C["bg"])
        bar.pack(fill="x", padx=10, pady=(4, 8))

        def _btn(parent, text, cmd, bg=None, fg=None):
            b = tk.Label(parent, text=text, bg=bg or C["card"], fg=fg or C["sub"],
                         cursor="hand2", font=(_FONT, 9), padx=8, pady=4)
            b.pack(side="left", padx=(0, 6))
            b.bind("<Button-1>", lambda e: cmd(b))
            return b

        def _save(btn):
            items = []
            for e in entries:
                items.append({k: (e[k].get() if k in e else "").strip() for k in
                              ("name",) + tuple(f[0] for f in _PERSONA_FIELD)})
            _flash(btn, "保存中…")

            def _do():
                try:
                    local_open(urllib.request.Request(
                        "http://127.0.0.1:%d/api/personas" % port,
                        data=json.dumps({"items": items}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}), timeout=6)
                except Exception:
                    pass
                plocal["items"] = _personas_fetch(port)

                def _after():
                    build_pref_rows()
                    _push_prefs()
                    win.destroy()
                root.after(0, _after)
            threading.Thread(target=_do, daemon=True).start()

        _btn(bar, "＋ 新增人设", lambda b: add_row())
        _btn(bar, "保存", _save, C["accent"], "#ffffff")
        _btn(bar, "取消", lambda b: win.destroy())

    # ---- 收藏夹(本地回怼收藏: 可搜索 / 复制 / 删除, 存服务端 SQLite) ----
    def show_saved():
        win = tk.Toplevel(root)
        win.title("⭐ 回怼收藏夹")
        win.configure(bg=C["bg"])
        win.geometry("%dx%d" % (int(348 * scale), int(430 * scale)))
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        top = tk.Frame(win, bg=C["bg"])
        top.pack(fill="x", padx=8, pady=(8, 4))
        qv = tk.StringVar()
        qent = tk.Entry(top, textvariable=qv, bg=C["input"], fg=C["txt"], relief="flat",
                        insertbackground=C["txt"], font=(_FONT, 9))
        qent.pack(side="left", fill="x", expand=True, ipady=4)
        box = tk.Frame(win, bg=C["bg"])
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        holder = {"cv": None}

        def refresh(_e=None):
            for w in box.winfo_children():
                w.destroy()
            holder["cv"] = None
            items = []
            try:
                url = "http://127.0.0.1:%d/api/rebut/saved?q=%s" % (
                    port, urllib.parse.quote(qv.get().strip()))
                with local_open(url, timeout=6) as r:
                    items = (json.loads(r.read().decode("utf-8")) or {}).get("items") or []
            except Exception:
                pass
            if not items:
                tk.Label(box, text=("没有匹配的收藏。\n在回怼结果里点「⭐ 收藏」就会存到这里。"
                                    if qv.get().strip() else
                                    "还没有收藏。\n在回怼结果里点「⭐ 收藏」就会存到这里。"),
                         bg=C["bg"], fg=C["sub"], font=(_FONT, 9), justify="center").pack(
                    pady=40, fill="x")
                return
            cv = tk.Canvas(box, bg=C["bg"], highlightthickness=0)
            sb2 = tk.Scrollbar(box, orient="vertical", command=cv.yview, width=10)
            cv.configure(yscrollcommand=sb2.set)
            sb2.pack(side="right", fill="y")
            cv.pack(side="left", fill="both", expand=True)
            inn = tk.Frame(cv, bg=C["bg"])
            cw = cv.create_window((0, 0), anchor="nw", window=inn)
            inn.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
            cv.bind("<Configure>", lambda e: cv.itemconfig(cw, width=e.width))
            cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
            holder["cv"] = cv
            for it in items:
                card = tk.Frame(inn, bg=C["card"])
                card.pack(fill="x", pady=3)
                src = (it.get("source") or "").strip()
                if src:
                    tk.Label(card, text="原话: " + src[:70], bg=C["card"], fg=C["sub"],
                             font=(_FONT, 7), wraplength=int(300 * scale), justify="left",
                             anchor="nw").pack(fill="x", padx=8, pady=(5, 0))
                tk.Label(card, text=it.get("content") or "", bg=C["card"], fg=C["txt"],
                         font=(_FONT, 9), wraplength=int(300 * scale), justify="left",
                         anchor="nw").pack(fill="x", padx=8)
                acts = tk.Frame(card, bg=C["card"])
                acts.pack(fill="x", padx=8, pady=(3, 6))
                _chip(acts, "复制", lambda b, t=it.get("content") or "": _copy_text(b, t))

                def _del(b, rid=it.get("id")):
                    def _do():
                        try:
                            local_open(urllib.request.Request(
                                "http://127.0.0.1:%d/api/rebut/delete" % port,
                                data=json.dumps({"id": rid}).encode("utf-8"),
                                headers={"Content-Type": "application/json"}), timeout=6)
                        except Exception:
                            pass
                        root.after(0, refresh)
                    threading.Thread(target=_do, daemon=True).start()
                    _flash(b, "已删除")
                _chip(acts, "删除", _del, "#f56c6c")

        sbtn = tk.Label(top, text="搜索", bg=C["accent"], fg="#ffffff", cursor="hand2",
                        font=(_FONT, 9), padx=10)
        sbtn.pack(side="left", padx=(6, 0), fill="y")
        sbtn.bind("<Button-1>", lambda e: refresh())
        qent.bind("<Return>", lambda e: (refresh(), "break")[1])
        tk.Label(win, text="收藏存在本机数据库里, 关掉悬浮球也不会丢。", bg=C["bg"], fg=C["sub"],
                 font=(_FONT, 7)).pack(side="bottom", pady=(0, 6))
        refresh()

    toolrow = tk.Frame(prefbar, bg=C["bg"])
    toolrow.pack(fill="x", padx=6, pady=(2, 6))

    def _tool_btn(parent, text, cmd):
        b = tk.Label(parent, text=text, bg=C["card"], fg=C["sub"], cursor="hand2",
                     font=(_FONT, 8), padx=6, pady=3)
        b.pack(side="left", padx=(0, 4))
        b.bind("<Button-1>", lambda e: cmd())
        return b

    _tool_btn(toolrow, "⭐ 收藏夹", lambda: show_saved())
    _tool_btn(toolrow, "✎ 人设", lambda: show_personas())
    _tool_btn(toolrow, "🧹 清空对话", lambda: new_conversation())
    # 原来的「🖱 悬停展开 开/关」开关随功能移除(用户要求: 鼠标停上去不许自动开, 必须点球)。
    # 悬停不再有任何开合动作之后, "点一下"这件事就得写在用户看得见的地方, 所以留这句提示。
    tk.Label(toolrow, text="🖱 单击展开", bg=C["bg"], fg=C["sub"],
             font=(_FONT, 8)).pack(side="left", padx=(2, 0))

    def style_prefs():
        for (k, opt), w in pref_segs.items():
            on = prefs.get(k) == opt
            w.config(bg=C["accent"] if on else C["card"],
                     fg="#ffffff" if on else C["sub"])

    def _push_prefs(extra=None):
        """档位改动推给服务端落库(后台线程, 不卡界面)。"""
        payload = dict(prefs)
        payload.update(extra or {})

        def _push():
            try:
                local_open(urllib.request.Request(
                    "http://127.0.0.1:%d/api/floatball" % port,
                    data=json.dumps({"prefs": payload}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}), timeout=5)
            except Exception:
                pass
        threading.Thread(target=_push, daemon=True).start()

    def set_pref(k, o):
        prefs[k] = o
        style_prefs()
        _push_prefs()

    build_pref_rows()   # 档位行首次渲染(此时 set_pref / style_prefs 都已就位)

    def toggle_prefs(_e=None):
        if prefbar.winfo_ismapped():
            prefbar.pack_forget()
        else:
            prefbar.pack(fill="x", before=mid, pady=(0, 2))

    # ---- 消息流(主体) + 输入行 ----
    # 打包顺序很关键: 输入行先以 side=bottom 占住底边, 消息区再 expand 吃剩余空间
    # (Canvas 默认自请求尺寸过大, 若消息区先打包, 输入行会被挤出窗口看不见)
    mid = tk.Frame(panel, bg=C["bg"])
    canvas = tk.Canvas(mid, width=int(120 * scale), height=int(60 * scale),
                       bg=C["bg"], highlightthickness=0, bd=0)
    sb = tk.Scrollbar(mid, orient="vertical", command=canvas.yview, width=9,
                      relief="flat", bd=0)
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", padx=(3, 0))
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=C["bg"])
    _win = canvas.create_window((0, 0), anchor="nw", window=inner)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(_win, width=e.width))

    def _wheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
    canvas.bind_all("<MouseWheel>", _wheel)

    hintbox = {}

    def show_hint():
        hintbox["w"] = tk.Label(inner, text="粘贴对方的话开始\n「验证真实性」或「生成回怼」",
                                bg=C["bg"], fg=C["sub"], justify="center", font=(_FONT, 9))
        hintbox["w"].pack(pady=(100, 0), fill="x")
    show_hint()

    inputrow = tk.Frame(panel, bg=C["bg"])
    # width=4 防默认80字符请求宽度挤掉发送按钮; 发送按钮先打包占住右侧
    inp = tk.Text(inputrow, height=2, width=4, wrap="word", bg=C["input"], fg=C["txt"],
                  insertbackground=C["txt"], relief="flat",
                  font=(_FONT, 9), padx=8, pady=5)
    sendbtn = tk.Label(inputrow, text="发送", bg=C["accent"], fg="#ffffff", cursor="hand2",
                       font=(_FONT, 9, "bold"), padx=16, pady=6)
    sendbtn.pack(side="right", fill="y", padx=(6, 0))
    inp.pack(side="left", fill="both", expand=True)
    inputrow.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
    mid.pack(fill="both", expand=True, padx=8, pady=(2, 0))

    st = {"view": "ball", "down": False, "moved": False,
          "ox": 0, "oy": 0, "wx": 0, "wy": 0, "t0": 0.0,
          "hidden": False, "peer": None,   # hidden=收进屏幕边缘; peer=隐藏后留的恢复条
          # 收起时贴的是哪条边: "left"/"right"/"top"/"bottom" —— show_ball 按它决定
          # 从哪侧把球拉回来, _peer_show 按它决定恢复条横放还是竖放。
          "edge": "right",
          # 球体皮肤状态(见 _ball_skin_state): hover/drag/unread 是这里记的, busy 由
          # sending["on"] 推出来, idle 是兜底。skin_t 是动画相位(秒)。
          "skin": "idle", "skin_t": 0.0, "hover": False, "drag": False, "unread": False,
          "flash": 0.0}   # flash: 「定位悬浮球」的闪三下亮度加成(叠到全息亮度上)
    sending = {"on": False}
    gen = {"n": 0}   # 会话代数: 🆕清空后旧请求的事件全部作废

    def scroll_end():
        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.yview_moveto(1.0)

    def save_pos(x, y):
        try:
            with open(_ball_pos_path(), "w", encoding="utf-8") as f:
                json.dump({"x": int(x), "y": int(y), "pid": os.getpid()}, f)
        except Exception:
            pass

    def draw_panel_deco(_e=None):
        """画面板装饰: 切角边框 + 四角刻度 + 正十二面体线框背景。

        放在 panel 的底层(place + 最先创建 = 最底的 z-order), 其他控件正常 pack 叠在上面,
        于是装饰只在**边缘**露出来, 不挡住任何内容。
        """
        panel_deco.delete("deco")
        w = panel_deco.winfo_width()
        h = panel_deco.winfo_height()
        if w < 20 or h < 20:
            return
        ch = min(16, int(w * 0.025))   # 切角量, 与球体装甲同源
        # 外框切角八边形
        pts = [ch, 0, w-ch, 0, w, ch, w, h-ch, w-ch, h, ch, h, 0, h-ch, 0, ch]
        panel_deco.create_polygon(pts, fill=C["bg"], outline=C["accent"], width=1,
                                  tags="deco")
        # 内框细线: 让切角边框有"厚度", 不是一根单薄的描边
        m = 2
        pts2 = [ch+m, m, w-ch-m, m, w-m, ch+m, w-m, h-ch-m, w-ch-m, h-m, ch+m, h-m,
                m, h-ch-m, m, ch+m]
        panel_deco.create_polygon(pts2, fill="",
                                  outline=_hex_mix(C["accent"], C["bg"], 0.55),
                                  width=1, tags="deco")
        # 四角刻度(准星): 短横 + 短竖, 让四角不至于光秃秃
        tl = 10
        for x0, y0, dx, dy in ((0, 0, 1, 1), (w, 0, -1, 1),
                               (w, h, -1, -1), (0, h, 1, -1)):
            panel_deco.create_line(x0 + ch*dx, y0, x0 + (ch+tl)*dx, y0,
                                   fill=C["accent"], width=1, tags="deco")
            panel_deco.create_line(x0, y0 + ch*dy, x0, y0 + (ch+tl)*dy,
                                   fill=C["accent"], width=1, tags="deco")
        # 正十二面体线框背景: 与全息 shader 同一套几何(20 顶点/30 边), 但调得很淡 ——
        # 面板是阅读区, 背景只是"这里有东西在转"的氛围, 抢戏就输了。
        v, e = _dode_geo()
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) * 0.28
        proj = _dode_project(v, 0.25, 0.4, cx, cy, r)
        for i, j in e:
            x1, y1, d1 = proj[i]
            x2, y2, d2 = proj[j]
            # 深度亮度衰减(取两端棱深的均值): 近处亮、远处暗, 线框才有体积感
            br = 0.25 + 0.75 * ((d1 + d2) / 2.0 + 1.0) / 2.0
            panel_deco.create_line(x1, y1, x2, y2,
                                   fill=_hex_mix(C["bg"], C["holo"], br * 0.18),
                                   width=1, tags="deco")

    def apply_palette():
        in_panel = st["view"] == "panel"
        container.config(bg=C["bg"] if in_panel else _BALL_TRANSPARENT,
                         highlightthickness=1 if in_panel else 0,
                         highlightbackground=C["line"])
        draw_panel_deco()   # 主題色变了, 面板装饰跟着换色
        _clip_header()      # 标题栏切角落色: 三角要用新的底色
        draw_ball()   # 主题色变了, 球体的渐变/高光/图标一起重画
        for w, key in ((panel, "bg"), (header, "card"), (dot, "card"), (title, "card"),
                       (modebar, "bg"), (mid, "bg"), (canvas, "bg"), (inner, "bg"),
                       (inputrow, "bg"), (prefbar, "bg"), (segs, "bg")):
            w.config(bg=C[key])
        title.config(bg=C["card"], fg=C["txt"])
        collapse_btn.config(bg=C["card"], fg=C["sub"])
        sb.config(troughcolor=C["bg"], activebackground=C["accent"])
        if hintbox.get("w"):
            hintbox["w"].config(bg=C["bg"], fg=C["sub"])
        inp.config(bg=C["input"], fg=C["txt"], insertbackground=C["txt"])
        sendbtn.config(bg=C["accent"], fg="#ffffff")
        prefs_btn.config(bg=C["card"], fg=C["sub"])
        new_btn.config(bg=C["card"], fg=C["sub"])
        prefbar.config(bg=C["bg"])
        build_pref_rows()   # 连档位行一起重画: 主题色变了, 行内按钮底色也得跟着换
        style_modes()

    # ---------------- 聊天气泡与结果渲染 ----------------
    def user_bubble(text, tag_text):
        if hintbox.get("w"):
            hintbox["w"].destroy()
            hintbox["w"] = None
        wrap = tk.Frame(inner, bg=C["bg"])
        wrap.pack(anchor="e", padx=4, pady=3)
        tk.Label(wrap, text=tag_text, bg=C["bg"], fg=C["sub"], font=(_FONT, 7),
                 anchor="e").pack(anchor="e")
        tk.Label(wrap, text=text, wraplength=int(250 * scale), justify="left",
                 bg=C["accent"], fg="#ffffff", font=(_FONT, 9), padx=10, pady=6).pack(anchor="e")
        scroll_end()

    def ai_bubble():
        wrap = tk.Frame(inner, bg=C["bg"])
        wrap.pack(anchor="w", padx=4, pady=3, fill="x")
        card = tk.Frame(wrap, bg=C["card"])
        card.pack(anchor="w", fill="x")
        status = tk.Label(card, text="…", wraplength=int(280 * scale), justify="left",
                          bg=C["card"], fg=C["sub"], font=(_FONT, 9), padx=10, pady=8,
                          anchor="nw")
        status.pack(anchor="w", fill="x")
        scroll_end()
        return {"card": card, "status": status, "seg_drawn": False}

    def set_status(holder, text, color=None):
        """状态行: 已经被分段内容顶掉时(置空)按需重建。"""
        w = holder.get("status")
        if w is None:
            w = tk.Label(holder["card"], text="", wraplength=int(280 * scale), justify="left",
                         bg=C["card"], fg=C["sub"], font=(_FONT, 9), padx=10, pady=8,
                         anchor="nw")
            w.pack(anchor="w", fill="x")
            holder["status"] = w
        w.config(text=text)
        if color:
            w.config(fg=color)
        scroll_end()

    def link_label(parent, text, url):
        lb = tk.Label(parent, text=text, wraplength=int(280 * scale), justify="left",
                      bg=C["card"], fg=C["accent"], font=(_FONT, 8), cursor="hand2", anchor="nw")
        lb.pack(anchor="w")
        lb.bind("<Button-1>", lambda e: webbrowser.open(url))

    def render_verify(card, status, res, evidence):
        status.destroy()
        head = tk.Frame(card, bg=C["card"])
        head.pack(fill="x", padx=10, pady=(8, 2))
        vlabel, vcolor = _VERDICT_STYLE.get(res.get("verdict"), _VERDICT_STYLE["uncertain"])
        tk.Label(head, text=vlabel, bg=vcolor, fg="#ffffff",
                 font=(_FONT, 9, "bold"), padx=8).pack(side="left")
        tk.Label(head, text="  置信度 %d%%" % res.get("confidence", 60), bg=C["card"],
                 fg=C["sub"], font=(_FONT, 8)).pack(side="left")
        if res.get("summary"):
            tk.Label(card, text=res["summary"], wraplength=int(280 * scale), justify="left",
                     bg=C["card"], fg=C["txt"], font=(_FONT, 9, "bold"),
                     padx=10, anchor="nw").pack(fill="x")
        for r in res.get("reasons") or []:
            tk.Label(card, text="· " + r, wraplength=int(280 * scale), justify="left",
                     bg=C["card"], fg=C["txt"], font=(_FONT, 8), padx=10,
                     anchor="nw").pack(fill="x")
        if res.get("fallacies"):
            tk.Label(card, text="🧩 知识误区与逻辑谬误: " + "、".join(res["fallacies"]),
                     wraplength=int(280 * scale), justify="left", bg=C["card"], fg=C["accent"],
                     font=(_FONT, 8), padx=10, anchor="nw").pack(fill="x")
        chk_style = {"true": ("✓", "#67c23a"), "false": ("✗", "#f56c6c"),
                     "uncertain": ("?", "#e6a23c")}
        cl = res.get("claims") or []
        if cl:
            tk.Label(card, text="分论点判定", bg=C["card"], fg=C["sub"],
                     font=(_FONT, 8), padx=10).pack(anchor="w", pady=(5, 0))
            for c in cl:
                mark, mcolor = chk_style.get(c.get("check"), chk_style["uncertain"])
                row = tk.Frame(card, bg=C["card"])
                row.pack(fill="x", padx=10)
                tk.Label(row, text=mark, bg=C["card"], fg=mcolor,
                         font=(_FONT, 9, "bold")).pack(side="left")
                txt = c.get("point", "") + ("：" + c["note"] if c.get("note") else "")
                tk.Label(row, text=txt, wraplength=int(255 * scale), justify="left",
                         bg=C["card"], fg=C["txt"], font=(_FONT, 8),
                         anchor="nw").pack(side="left", fill="x", expand=True)
        if res.get("offline"):
            tk.Label(card, text="⚠ 未搜到网络证据, 仅凭模型知识", bg=C["card"], fg="#e6a23c",
                     font=(_FONT, 8), padx=10).pack(anchor="w", pady=(3, 0))
        for e in (evidence or [])[:3]:
            if e.get("url"):
                link_label(card, "▪ %s · %s(%s)" % (e.get("title", "")[:26],
                             e.get("cred_label", ""), e.get("site", "")), e["url"])
        tk.Label(card, bg=C["card"]).pack(pady=2)
        scroll_end()

    def _copy_btn(parent, txt, label="复制"):
        """复制按钮(评论直接粘走)。"""
        b = tk.Label(parent, text=label, bg=C["card"], fg=C["accent"], cursor="hand2",
                     font=(_FONT, 8), padx=4)
        b.pack(side="right", anchor="n")

        def on_click(e, t=txt):
            root.clipboard_clear()
            root.clipboard_append(t)
            e.widget.config(text="已复制")
            root.after(1200, lambda w=e.widget: w.config(text=label))
        b.bind("<Button-1>", on_click)
        return b

    _SEG_TITLE = {"lines": "⚡ 一句话", "paras": "📄 一段话", "long": "📜 长篇"}

    def _chip(parent, text, cmd, color=None):
        """小动作按钮(复制 / 收藏 / 换一批 / 证据编号)。"""
        b = tk.Label(parent, text=text, bg=C["card"], fg=color or C["accent"], cursor="hand2",
                     font=(_FONT, 7), padx=3)
        b.pack(side="left", padx=(4, 0))
        b.bind("<Button-1>", lambda e: cmd(b))
        return b

    def _flash(btn, text):
        old = btn.cget("text")
        btn.config(text=text)
        root.after(1200, lambda: btn.config(text=old))

    def _copy_text(btn, txt):
        root.clipboard_clear()
        root.clipboard_append(txt)
        _flash(btn, "已复制")

    def _save_item(btn, source, seg, txt):
        """收藏这条回怼(存本地, 之后能在收藏夹里翻出来复用)。"""
        def _do():
            try:
                local_open(urllib.request.Request(
                    "http://127.0.0.1:%d/api/rebut/save" % port,
                    data=json.dumps({"source": source, "seg": seg,
                                     "content": txt}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}), timeout=5)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()
        _flash(btn, "已收藏")

    def regen_seg(holder, kind, btn):
        """换一批: 只重跑这一段, 论点与证据沿用上一轮(不再拆解与取证)。"""
        if sending["on"]:
            _flash(btn, "先等这轮跑完")
            return
        src = holder.get("src") or ""
        if not src:
            return
        sending["on"] = True
        sendbtn.config(text="…")
        holder["gen"] = gen["n"]
        # 记下按钮: 重跑成功后整块被 seg_append 换掉(按钮随之销毁), 失败时靠它把文字恢复
        holder["regen"] = {"kind": kind, "btn": btn}
        btn.config(text="换…")
        threading.Thread(target=_ball_worker, args=(port, "rebut", src, holder, kind),
                         daemon=True).start()

    def _regen_done(holder):
        """收尾: 把「换一批」按钮文字恢复(换批成功时按钮已随旧块销毁, 需要判存活)。"""
        rg = holder.pop("regen", None)
        if not rg:
            return
        try:
            if rg["btn"].winfo_exists():
                rg["btn"].config(text="🔄 换一批")
        except Exception:
            pass

    def seg_append(holder, data):
        """一段生成完就立刻画出来, 用户不用等整轮结束。
        同一段「换一批」重跑时, 把这一段旧内容整块换掉。"""
        card = holder["card"]
        if holder.get("status"):
            holder["status"].destroy()
            holder["status"] = None
        holder["seg_drawn"] = True
        kind = data.get("kind")
        items = data.get("items") or []
        if data.get("evidence"):
            holder["evmap"] = {e["id"]: e for e in data["evidence"] if e.get("id")}
        frames = holder.setdefault("segframes", {})
        if frames.get(kind) is not None:
            frames[kind].destroy()   # 换一批: 整块换掉这一段
        if kind == "targets" and not items and not data.get("stance_note"):
            frames.pop(kind, None)
            return
        box = tk.Frame(card, bg=C["card"])
        box.pack(fill="x")
        frames[kind] = box
        src = holder.get("src") or ""

        if kind == "targets":
            tk.Label(box, text="🎯 知识误区与逻辑谬误", bg=C["card"], fg=C["accent"],
                     font=(_FONT, 8, "bold"), padx=10).pack(anchor="w", pady=(6, 0))
            for i, t in enumerate(items, 1):
                head = "%d. %s" % (i, t.get("point") or "")
                if t.get("kind"):
                    head += "　[" + t["kind"] + "]"
                tk.Label(box, text=head, wraplength=int(280 * scale), justify="left",
                         bg=C["card"], fg=C["txt"], font=(_FONT, 8, "bold"),
                         padx=10, anchor="nw").pack(fill="x")
                if t.get("note"):
                    tk.Label(box, text="　" + t["note"], wraplength=int(275 * scale),
                             justify="left", bg=C["card"], fg=C["sub"], font=(_FONT, 8),
                             padx=10, anchor="nw").pack(fill="x")
            if data.get("stance_note"):
                tk.Label(box, text="证据倾向: " + data["stance_note"],
                         wraplength=int(280 * scale), justify="left", bg=C["card"], fg=C["sub"],
                         font=(_FONT, 8), padx=10, anchor="nw").pack(fill="x")
            scroll_end()
            return

        head = tk.Frame(box, bg=C["card"])
        head.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(head, text=_SEG_TITLE.get(kind, kind or ""), bg=C["card"], fg=C["accent"],
                 font=(_FONT, 8, "bold")).pack(side="left")
        _chip(head, "🔄 换一批", lambda b, k=kind: regen_seg(holder, k, b))
        if not items:
            tk.Label(box, text="（这一段没生成出内容，点换一批再来）", bg=C["card"], fg=C["sub"],
                     font=(_FONT, 8), padx=10, anchor="w").pack(fill="x")
            scroll_end()
            return
        evmap = holder.get("evmap") or {}
        for it in items:
            txt = it.get("text", "")
            tk.Label(box, text=txt, wraplength=int(280 * scale), justify="left",
                     bg=C["card"], fg=C["txt"], font=(_FONT, 10 if kind != "long" else 9),
                     padx=10, anchor="nw").pack(fill="x")
            acts = tk.Frame(box, bg=C["card"])
            acts.pack(fill="x", padx=10, pady=(1, 0))
            _chip(acts, "复制", lambda b, t=txt: _copy_text(b, t))
            _chip(acts, "⭐ 收藏", lambda b, t=txt, k=kind: _save_item(b, src, k, t))
            for x in (it.get("evidence") or []):
                e = evmap.get(x)
                if e and e.get("url"):
                    _chip(acts, "[" + e["id"] + "]", lambda b, u=e["url"]: webbrowser.open(u),
                          C["sub"])
            if it.get("basis"):
                tk.Label(box, text="依据: " + it["basis"], wraplength=int(280 * scale),
                         justify="left", bg=C["card"], fg=C["sub"], font=(_FONT, 7),
                         padx=10, anchor="nw").pack(fill="x")
        scroll_end()

    def rebut_tail(holder, res, evidence):
        """三段画完后的收尾: 证据链接与离线提示。"""
        card = holder["card"]
        if res.get("offline"):
            tk.Label(card, text="⚠ 未搜到网络证据, 仅凭模型知识", bg=C["card"], fg="#e6a23c",
                     font=(_FONT, 8), padx=10).pack(anchor="w", pady=(3, 0))
        for e in (evidence or [])[:4]:
            if e.get("url"):
                link_label(card, "▪ %s · %s(%s)" % (e.get("title", "")[:26],
                             e.get("cred_label", ""), e.get("site", "")), e["url"])
        tk.Label(card, bg=C["card"]).pack(pady=2)
        scroll_end()

    # ---------------- 发送 / 工作线程事件轮询 ----------------
    def send(_e=None):
        if sending["on"]:
            return
        text = inp.get("1.0", "end").strip()
        if not text:
            return
        inp.delete("1.0", "end")
        cur = mode["cur"]
        user_bubble(text, {"verify": "查证", "rebut": "回怼"}[cur])
        holder = ai_bubble()
        holder["gen"] = gen["n"]
        holder["src"] = text   # 「换一批」要拿原话重跑同一段
        holder["status"].config(text="🔍 正在拆解论点、联网搜索证据…")
        sending["on"] = True
        sendbtn.config(text="…")
        threading.Thread(target=_ball_worker, args=(port, cur, text, holder),
                         daemon=True).start()
    sendbtn.bind("<Button-1>", send)
    inp.bind("<Control-Return>", lambda e: (send(), "break")[1])

    def new_conversation(_e=None):
        gen["n"] += 1            # 在途请求的事件全部作废
        sending["on"] = False
        sendbtn.config(text="发送")
        for w in inner.winfo_children():
            w.destroy()
        hintbox["w"] = None
        show_hint()
        scroll_end()
    new_btn.bind("<Button-1>", new_conversation)
    prefs_btn.bind("<Button-1>", toggle_prefs)

    # ---------------- 球体输入层(轮询式) ----------------
    # 关键事实: 颜色键控的透明像素**收不到鼠标消息**。旧球是实心装甲球, 整块圆都能点; 现在换成
    # 镂空线框, 只剩那几根线能点(实测球窗圆内 15.6% 可点, 其余穿透到桌面) —— 用户报的
    # 「我经常点不到」就是它。消息送不到的地方没法用 Tk 绑定, 于是自己轮询全局键态。
    HIT_R = BALL * 0.44          # 命中圆半径(约 0.44*BALL ≈ 74 物理px): 线框本身只有 ~0.34*BALL,
                                 # 放大一圈当手感余量, 又不至于把窗口四角也吃掉(角上仍可点桌面)
    vin = {"down": False, "moved": False, "t": 0.0, "ox": 0, "oy": 0,
           "wx": 0, "wy": 0, "r": False, "pl": False}
    # 测试钩子 --ball-fake-click / --ball-fake-drag 的时间线:
    # [(dt_秒, x, y, 左键, 右键), ...] —— 只覆盖"指针位置 + 键态", **不碰真实鼠标**。
    fake = {"t0": None, "seq": []}

    def _btn_l():
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)   # VK_LBUTTON

    def _btn_r():
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x02) & 0x8000)   # VK_RBUTTON

    def _hit_xy(px, py, pad=0):
        """给定指针坐标是否落在**命中圆**里(不是按画出来的线, 也不是按窗口矩形)。"""
        dx = px - (root.winfo_x() + BALL / 2.0)
        dy = py - (root.winfo_y() + BALL / 2.0)
        rr = HIT_R + pad
        return dx * dx + dy * dy <= rr * rr

    def _hit_circle(pad=0):
        px, py = root.winfo_pointerxy()
        return _hit_xy(px, py, pad)

    def _input_now():
        """读 (左键, 右键, 指针x, 指针y); 测试时间线优先, 否则读真实键鼠。"""
        px, py = root.winfo_pointerxy()
        if fake["t0"] is None:
            return _btn_l(), _btn_r(), px, py
        cur = None
        for it in fake["seq"]:
            if time.time() - fake["t0"] >= it[0]:
                cur = it
        return (bool(cur[3]), bool(cur[4]), cur[1], cur[2]) if cur else \
               (_btn_l(), _btn_r(), px, py)

    def _release_settle():
        """松手结算(球形态): 落盘 + 拖到屏幕缘就收起。与面板头部那条路径同一套判据。"""
        if not st["hidden"]:
            save_pos(root.winfo_x(), root.winfo_y())
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        near = min((root.winfo_x() + PAD, "left"),
                   (sw - root.winfo_x() - BALL + PAD, "right"),
                   (root.winfo_y() + PAD, "top"),
                   (sh - root.winfo_y() - BALL + PAD, "bottom"))
        if st["view"] == "ball" and st["moved"] and near[0] <= 1:
            st["edge"] = near[1]
            hide_ball()

    def virtual_input():
        """轮询式输入。返回 True = 指针在球上或正在操作 → 调用方把轮询间隔提到 30ms。"""
        if st["view"] != "ball" or st["hidden"]:
            vin.update(down=False, moved=False, r=False)
            return False
        l, r, px, py = _input_now()
        inside = _hit_xy(px, py, 6)
        act, s2 = _vin_step(vin, time.time(), inside, l, r, px=px, py=py)
        vin.update(s2)
        if act == "press":
            vin["wx"], vin["wy"] = root.winfo_x(), root.winfo_y()
            st.update(down=True, moved=False, drag=True, t0=time.time())
        elif act == "drag":
            st["moved"] = True
            # 钳制按**球体**算(与面板头部拖拽一致): 允许窗口越出屏幕 PAD 像素, 球体才贴得到边
            nx = max(int(-PAD), min(vin["wx"] + px - vin["ox"],
                                    int(root.winfo_screenwidth() - BALL + PAD)))
            ny = max(int(-PAD), min(vin["wy"] + py - vin["oy"],
                                    int(root.winfo_screenheight() - BALL + PAD)))
            _geo(BALL, BALL, nx, ny)
            return True
        elif act == "click":
            st.update(down=False, drag=False)
            expand()          # 单击(命中圆内任意位置) = 展开成对话面板
        elif act == "drop":
            st.update(down=False, drag=False)
            _release_settle()
        elif act == "rmenu":
            on_right(at=(px, py))
        return bool(act) or inside or vin["down"]

    def poll():
        try:
            while True:
                kind, holder, data = _EVQ.get_nowait()
                if holder.get("gen") != gen["n"]:
                    continue   # 🆕 清空后的旧请求事件作废
                if kind == "phase":
                    set_status(holder, data or "…")
                elif kind == "seg":
                    seg_append(holder, data or {})
                elif kind == "done":
                    sending["on"] = False
                    sendbtn.config(text="发送")
                    d = data or {}
                    regen = str(d.get("regen") or "")
                    _regen_done(holder)
                    # 球是收起状态时结果出来了 → 右上角点个呼吸点。用户可能已经把面板收起来了,
                    # 不给这个提示的话, 结果出来了球还是原样, 完全看不出有东西回来。
                    if st["view"] == "ball" and not regen:
                        st["unread"] = True
                    if d.get("ok"):
                        card = holder["card"]
                        r0 = d.get("result") or {}
                        if regen:
                            pass   # 换一批: 这一段已由 seg 事件就地替换, 不再补画收尾
                        elif r0.get("no_target"):   # 模型判定无可回怼: 说明原因即可
                            set_status(holder, "🤷 " + (r0.get("reason") or "这条没有可回怼的点"))
                        elif d.get("mode") == "verify":
                            if holder.get("status"):
                                render_verify(card, holder["status"], r0, d.get("evidence") or [])
                                holder["status"] = None
                        else:
                            ev = d.get("evidence") or []
                            if not holder.get("seg_drawn"):   # 没收到分段(异常) → 一次性补齐
                                seg_append(holder, {"kind": "targets",
                                                    "items": r0.get("targets") or [],
                                                    "stance_note": r0.get("stance_note") or "",
                                                    "evidence": ev})
                                for _k in ("lines", "paras", "long"):
                                    seg_append(holder, {"kind": _k, "items": r0.get(_k) or []})
                            rebut_tail(holder, r0, ev)
                        u = d.get("usage") or {}
                        if u.get("calls") and not regen:   # 本次花费小字(同样计入主界面每日成本)
                            tk.Label(card, text="⏱ %ss · %s 次调用 · ¥%s" % (
                                d.get("elapsed", "?"), u["calls"], u.get("cost", 0)),
                                bg=C["card"], fg=C["sub"], font=(_FONT, 8), padx=10,
                                anchor="w").pack(fill="x")
                    else:
                        set_status(holder, "⚠ " + (d.get("error") or "失败"), "#f56c6c")
                    scroll_end()
                elif kind == "error":
                    sending["on"] = False
                    sendbtn.config(text="发送")
                    _regen_done(holder)
                    set_status(holder, "⚠ " + str(data), "#f56c6c")
        except queue.Empty:
            pass
        try:
            # 球的高亮**只**是视觉反馈, 不驱动任何开合: 面板一律靠单击球展开(见 virtual_input),
            # 靠「─ 收起」/右键菜单收回 —— 悬停自动展开/离开自动收起已按用户要求整体移除。
            if st["view"] == "ball" and not st["hidden"] and not st["down"]:
                st["hover"] = _ptr_on_ball(6)
            else:
                st["hover"] = False
            sync_skin()    # 球体皮肤跟着「生成中/有未读/悬停」走
        except Exception:
            pass
        try:
            # 输入层要在最后调: 它可能触发展开/收边, 那些动作会改 view/hidden
            active = virtual_input()
        except Exception:
            active = False
        # 指针在球上(或正在拖拽)时把轮询提到 30ms —— 单击最短也就几十毫秒, 90ms 会漏。
        # 只有"手在球上"这一小段时间是密的, 平时仍是 90ms, 不额外吃 CPU。
        root.after(30 if active else 90, poll)

    # ---------------- 球体动效驱动 ----------------
    # 静止 150ms 一帧(约 6.7fps)、交互 45ms 一帧(约 22fps)。
    # 为什么静止敢放到 150ms: 静止转速只有 0.30rad/s, 每帧转 2.6°, 这个幅度看不出台阶; 而
    # 帧率直接决定 CPU —— 悬浮球是常驻小窗, 用户还会在同一台机器上打游戏, 实测(见
    # DJXM_BALL_PROF 探针)每帧 cost×帧率就是它占的核数, 11fps 下能吃掉 31% 单核。
    # 相位用**真实时间差**累加而不是每帧加固定值: 定时器被拖动/被系统节流时不会突然变慢。
    anim = {"t0": time.time()}

    def ball_anim():
        now = time.time()
        _glass_sync()          # 玻璃体跟着球走(拖动/收边/展开都在这一处兜住)
        dt = min(0.5, max(0.0, now - anim["t0"]))
        anim["t0"] = now
        st["skin_t"] = st.get("skin_t", 0.0) + dt
        if st["view"] == "ball" and not st["hidden"]:
            _tg0 = time.perf_counter()
            try:
                _glass_draw()      # 下层: 半透明的面(真 alpha 那层)
            except Exception as _ge:
                if os.environ.get("DJXM_GLASS_DEBUG"):
                    print("[glass] 画失败: %r" % (_ge,), flush=True)
            _tg = time.perf_counter() - _tg0
            try:
                draw_ball_fx()     # 上层: 线框(不透明, 保持清晰)
            except Exception:
                pass
            if _BALL_PROF[0]:      # 玻璃层的耗时/图元也计进来, 否则探针数字虚低
                _BALL_PROF[2] += _tg
                _BALL_PROF[3] += len(glass["cv"].find_all())
        if _BALL_PROF[0] and now - _BALL_PROF[4] >= 5.0:
            n = max(1, _BALL_PROF[1])
            print("[ball-prof] %.1fms/帧 · %d 图元 · %.1f 帧/s"
                  % (1000.0 * _BALL_PROF[2] / n, _BALL_PROF[3], n / (now - _BALL_PROF[4])),
                  flush=True)
            _BALL_PROF[1] = _BALL_PROF[2] = 0
            _BALL_PROF[4] = now
        # 帧率统一 33ms(~30fps): 探针实测单帧球 1.2ms + 玻璃层 0.6ms, 满帧也就 ~5% 单核。
        # 原来 idle 走 150ms(6.7fps)"省 CPU"是白省 —— 省下的不到 1%, 代价是肉眼可见的顿;
        # 交互态也一并统一, 免得出现"静止比悬停还顺"这种自相矛盾的观感。
        root.after(33, ball_anim)

    # ---------------- 玻璃体: 真半透明的一层(独立窗, 垫在球**下面**) ----------------
    # 为什么要单独一层: 球的键控窗(-transparentcolor)只有全透明/全不透明两档, Tk 的 -alpha
    # 又是**整窗统一** alpha, 而且跟键色互斥(后设的顶掉前者, 实测窗口变成不透明矩形)。要
    # "体色真半透明 + 线框仍然清晰", 只能分层 —— 下面这层用一次 Win32 调用把
    # LWA_COLORKEY|LWA_ALPHA **一起**给(实测 GetLayeredWindowAttributes 返回 flags=3):
    # 圆外键色真透明(所以不是一块矩形), 圆内由合成器做 alpha 混合(所以没有点阵)。
    _GLASS_ALPHA = 108      # 体色不透明度(0~255): 108 ≈ 42%。想更浓/更淡改这一个数
    glass = {"w": None, "cv": None, "oval": None, "shown": False, "col": None}

    def _top_hwnd(w):
        """Tk 的 winfo_id() 给的是内层子窗, 分层属性在**顶层窗**上(它的父窗)。"""
        try:
            return ctypes.windll.user32.GetParent(w.winfo_id()) or w.winfo_id()
        except Exception:
            return 0

    def _glass_alpha_on(w):
        try:
            _hw = _top_hwnd(w)
            ctypes.windll.user32.SetLayeredWindowAttributes(
                _hw, 0x010101,              # COLORREF(BGR) = 键色 #010101
                _GLASS_ALPHA, 3)            # 3 = LWA_COLORKEY|LWA_ALPHA
            if os.environ.get("DJXM_GLASS_DEBUG"):
                # 自检开关: 回读分层属性, 确认"键色+真 alpha"真的同时生效(flags 必须是 3)。
                # 光看代码"应该生效"是不够的 —— 这台机器上 Tk 的 -alpha 就曾经把键色顶掉。
                _cr, _al, _fl = (ctypes.c_uint32(), ctypes.c_ubyte(),
                                 ctypes.c_uint32())
                _q = ctypes.windll.user32.GetLayeredWindowAttributes(
                    _hw, ctypes.byref(_cr), ctypes.byref(_al), ctypes.byref(_fl))
                print("[glass] hwnd=%s ok=%s alpha=%d flags=%d (3=键色+alpha)"
                      % (hex(_hw), _q, _al.value, _fl.value), flush=True)
        except Exception as _e:
            if os.environ.get("DJXM_GLASS_DEBUG"):
                print("[glass] 失败: %r" % (_e,), flush=True)

    def _ball_front():
        """把球窗压在玻璃体**上面**(后建的窗默认在上面)。NOACTIVATE: 不抢焦点。"""
        try:
            ctypes.windll.user32.SetWindowPos(_top_hwnd(root), -1, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0010)
        except Exception:
            pass

    def _glass_make():
        w = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.geometry("%dx%d+%d+%d" % (BALL, BALL, root.winfo_x(), root.winfo_y()))
        w.attributes("-transparentcolor", _BALL_TRANSPARENT)
        cv = tk.Canvas(w, width=BALL, height=BALL, bg=_BALL_TRANSPARENT,
                       highlightthickness=0)
        cv.pack()
        w.update_idletasks()
        glass.update(w=w, cv=cv, shown=True)
        _glass_alpha_on(w)
        _ball_front()

    def _glass_sync():
        """玻璃层跟着球: 位置同步 + 该不该出现(展开成面板/收进边缘时不出现)。"""
        w = glass.get("w")
        if w is None:
            return
        want = (st["view"] == "ball" and not st["hidden"])
        try:
            if want:
                if not glass["shown"]:
                    w.deiconify()
                    glass["shown"] = True
                    _glass_alpha_on(w)      # 重新显示后分层属性要重设
                    _ball_front()
                # 位置没变就别再设 geometry: 每帧都调会白白吃掉几毫秒(实测拖累帧率),
                # 拖动时位置当然每帧都变, 那是该设的。
                _pos = (root.winfo_x(), root.winfo_y())
                if glass.get("pos") != _pos:
                    w.geometry("%dx%d+%d+%d" % (BALL, BALL, _pos[0], _pos[1]))
                    glass["pos"] = _pos
            elif glass["shown"]:
                w.withdraw()
                glass["shown"] = False
        except Exception:
            pass

    def _glass_draw():
        """把**正面朝前的每个面**铺在这层上 —— 半透明的是面, 不是背后一个圆。

        为什么面必须画在这层: 球窗是键控窗(只有全透明/全不透明两档), 面铺上去就是实心塑料片;
        这层是 LWA_COLORKEY|LWA_ALPHA, 面交给合成器做**真 alpha**混合 —— 于是"每个面半透明、
        线框依然清晰"(线框在不透明的上层)。

        颜色: **所有面同一个中间调**(armor↔holo 的中值), 不再按深度压暗。有了真半透明, 层次
        本来就由"面和桌面的混合 + 上层线框的近亮远暗"承担了; 再叠一层人为压暗只会把整颗球
        拖暗(用户反馈"亮度有点低")。armor 在深色主题里是压到近黑的底色, 单独用它必然发闷,
        和 holo(亮面强调色)对半混出来才是"玻璃"该有的中间调 —— 浅色主题下同理成立。
        背面不铺(全息要能看穿)。轮廓用同色 outline 描一圈: 相邻面共享边, 不描会露出发丝缝。
        """
        cv = glass.get("cv")
        if cv is None:
            return
        cv.delete("gf")
        t = st.get("skin_t", 0.0)
        P = _holo_state()
        v, ed, faces, fnorm, efaces = _solid_geo()
        cx = cy = BALL / 2.0
        RS = BALL * 0.340
        radii = [1.0 + P["wave"] * math.sin(t * 1.6 + i * 1.9) for i in range(len(v))]
        a = t * P["spin"]
        ax, ay, az = 0.62 + a * 0.52, 0.42 + a * 0.86, 0.26 + a * 0.36
        proj = _solid_project(v, ax, ay, az, cx, cy, RS, radii=radii)
        col = _hex_mix(C["armor"], C["holo"], 0.50)
        for fi, f in enumerate(faces):
            if _rot3(fnorm[fi], ax, ay, az)[2] <= 0:
                continue                                   # 背面不铺
            cv.create_polygon(*[q for i in f for q in (proj[i][0], proj[i][1])],
                              fill=col, outline=col, width=1, tags="gf")

    _glass_make()

    # ---------------- 球 ⇄ 面板 形态切换(原地展开/收回) ----------------
    def expand(_e=None, focus=True):
        if st["view"] != "ball":
            return
        if st["hidden"]:   # 收边态没有"原地"可展开: 先把球拉回屏内, 否则面板会开在屏幕外
            show_ball()
            return
        C.update(_ball_theme(port))   # 展开时取最新强调色/主题
        try:   # 同步最新档位设置(思考/身份/风格/条数)与自定义人设
            _sp = _ball_prefs_fetch(port)
            for _k in ("think", "persona", "savage", "len"):
                if _sp.get(_k):
                    prefs[_k] = str(_sp[_k])
            plocal["items"] = _personas_fetch(port)
        except Exception:
            pass
        apply_palette()   # 里面会重建档位行(主题色 + 自定义人设都在这一步生效)
        bx, by = root.winfo_x(), root.winfo_y()
        st["unread"] = False   # 面板打开了, 结果就在眼前, 未读提示该撤了
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # 面板右上角贴**球体**右缘(不是窗口右缘): 窗口那圈透明边距不参与构图, 拿窗口算
        # 会让面板和球之间空出 PAD 像素的缝。
        tx = bx + BALL - PAD - PW   # 默认往左展开
        if tx < 4:
            tx = min(bx, sw - PW - 4)   # 左边放不下 → 往右展开
        tx = max(4, tx)
        ty = max(4, min(by + PAD - 40, sh - PH - 4))
        ballcv.pack_forget()
        panel.pack(fill="both", expand=True)
        for i in (1, 2, 3):   # 快速展开动画
            t = i / 3.0
            root.geometry("%dx%d+%d+%d" % (int(BALL + (PW - BALL) * t),
                                           int(BALL + (PH - BALL) * t),
                                           int(bx + (tx - bx) * t), int(by + (ty - by) * t)))
            root.update_idletasks()
            time.sleep(0.02)
        _geo(PW, PH, tx, ty)   # 收尾帧: 走 _geo 确保落地
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        st["view"] = "panel"
        # 位置文件记的**永远是球的落点**, 不是面板的左上角 —— 展开时窗口虽然变成了面板,
        # 但下次启动要按"球该在哪儿"来摆。原来这里写的 (tx, ty) 是面板坐标, 语义串了,
        # 会让下次启动的球落在按面板尺寸算出来的怪位置。展开前的 (bx, by) 才是球位。
        save_pos(bx, by)
        if focus:
            inp.focus_set()   # 展开=用户点了球, 直接把光标给输入框; focus=False 留给测试钩子

    def collapse(_e=None):
        if st["view"] != "panel":
            return
        x, y = root.winfo_x(), root.winfo_y()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # 球收回面板右上角(与展开时"面板右上角贴着球"对称), 而不是跳到面板左上角。
        # 右侧要留 96px 边距: 贴到最右缘那条会被任务栏通知区/置顶小窗盖住, 用户展开一次
        # 再收起就"又找不到球"了(左缘没有这个问题, 仍留 4px)。
        # 加减 PAD 是为了抵消窗口的透明边距, 让**球体**落在该落的地方(与 expand 严格互逆:
        # expand 里 tx = bx + BALL - PAD - PW, 这里反解回去正好还原 bx)。
        tx = max(4, min(x + PW - BALL + PAD, _ball_win(sw)))
        ty = max(4, min(y + 40 - PAD, sh - BALL + PAD - 4))
        panel.pack_forget()
        container.config(bg=_BALL_TRANSPARENT, highlightthickness=0)
        ballcv.pack(fill="both", expand=True)
        for i in (1, 2, 3):   # 收回动画: 右缘不动, 向右收缩成球
            t = i / 3.0
            root.geometry("%dx%d+%d+%d" % (int(PW - (PW - BALL) * t), int(PH - (PH - BALL) * t),
                                           int(x + (tx - x) * t), int(y + (ty - y) * t)))
            root.update_idletasks()
            time.sleep(0.02)
        _geo(BALL, BALL, tx, ty)   # 收尾帧: 走 _geo 确保落地
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        st["view"] = "ball"
        save_pos(tx, ty)
    collapse_btn.bind("<Button-1>", lambda e: collapse())

    # ---------------- 隐藏 ⇄ 显示 ----------------
    # 「隐藏」= 球横向滑出屏幕, 只在贴边处留一条 4px 的恢复条(hover 变宽并显示箭头)。
    # 不真销毁窗口: 一下午的调用记录/档位都在这个进程里, 销毁就得重来;
    # 也不做成"最小化成 1px", 那样用户找不到恢复入口。收边比 full-hide 更好用。
    def _anim(seq, dur=0.012, step=None):
        """按 (w,h,x,y) 序列逐帧改窗口几何; 每帧 update_idletasks 让画面真的动起来。"""
        for geo in seq:
            root.geometry("%dx%d+%d+%d" % geo)
            root.update_idletasks()
            time.sleep(dur)

    def _geo(w, h, x, y):
        """瞬间改窗口几何, 并**把请求刷下去**(等价于 _anim 的单帧)。

        为什么必须有那一句 update_idletasks: `root.geometry(...)` 只是**登记**一个几何
        请求, 真正落到窗口上要等 Tk 跑 idle 任务。普通回调里这几乎立刻发生, 但悬浮球是
        overrideredirect + 颜色键控的**分层窗口**, 实测这里会**一直不生效** ——
        调完 geometry 之后 winfo_x/y 还是旧值, 外部 GetWindowRect 量出来也纹丝不动
        (t=9.9s 调 geometry, 之后 16 秒窗口一动不动)。

        这正是用户报的「按了『定位悬浮球』, 提示说球移到右侧了, 可球根本没动」的根子:
        位置算对了, save_pos 也写对了, **只有"挪窗口"这一步没落地**。
        `_anim` 每帧都跟一句 update_idletasks, 所以收展/收边这些动效一直是好的 ——
        偏偏这几个"瞬间跳位"的路径漏了。
        """
        root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        root.update_idletasks()

    def _ease(t):
        """缓出: 起步快收尾慢, 比线性更像"物理"的收展。"""
        return 1.0 - (1.0 - t) ** 3

    def _nearest_edge(x, y):
        """球体(窗口左上角 x,y)离哪条屏幕边最近: "left"/"right"/"top"/"bottom"。

        收起方向必须跟用户实际把球扔到的那条边一致: 拖到下缘松手却往左收, 球会横着飞过
        半个屏幕, 恢复条也贴在错的边上。四缘统一按**球体边缘**到屏幕边的距离取最近。
        """
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        return min((x + PAD, "left"),
                   (sw - x - BALL + PAD, "right"),
                   (y + PAD, "top"),
                   (sh - y - BALL + PAD, "bottom"))[1]

    def hide_ball(_e=None):
        """把球收进屏幕边缘, 留一条恢复条。已在隐藏态则忽略。"""
        if st["hidden"]:
            return
        if st["view"] == "panel":
            collapse()
        C.update(_ball_theme(port))
        if hintbox.get("w"):
            hintbox["w"].destroy()
            hintbox["w"] = None
        x, y = root.winfo_x(), root.winfo_y()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # 靠哪条边就往哪条边收。没拖拽过直接调本函数时(隐藏按钮/自动收边钩子)按当前位置
        # 算最近边; 默认位在右侧 → 收右边, 与从前一致。
        edge = _nearest_edge(x, y)
        e_px = 4
        # 收边只挪坐标, **窗口尺寸保持不变**: 球体只占窗口中间那块, 把窗口缩到 4px 会把球
        # 一起裁掉, 屏幕上什么都看不见(以前窗口=球, 缩窄才对)。
        #
        # 目标是「球体**滑出屏幕**, 只在缘上留 4px」, 不是「球体贴着缘站好」。
        # 球体 = 窗口内缩 PAD 的那一圈: 球体左缘 = 窗口x + PAD, 球体右缘 = 窗口x + BALL - PAD,
        # 于是"留 4px"= 球体**内侧**那条边落在离缘 e_px 处, 其余全在屏幕外:
        #   左(往左滑): 球体右缘 = e_px        →  x = e_px - BALL + PAD
        #   右(往右滑): 球体左缘 = sw - e_px   →  x = sw - e_px - PAD
        #   上(往上滑): 球体底缘 = e_px        →  y = e_px - BALL + PAD
        #   下(往下滑): 球体顶缘 = sh - e_px   →  y = sh - e_px - PAD
        #
        # 用户报的「出现恢复条, 球却没隐藏」就是这四条写反了语义: 原来写的是"球体**外缘**贴缘"
        # (左: x = e_px - BALL/2 + RB, 右: x = sw - e_px - BALL/2 - RB, ...), 球体完整停在
        # 离缘 4px 的屏内 —— 收起后球和恢复条同时在桌上, 那条 4px 细线只是紧挨着球, 看着就是
        # "点了隐藏但没隐藏"。球体露出的那 ≤4px 正好被恢复条(同缘、同中点、4px 厚)盖住。
        # 取整方向必须"往外推": 左/上往负方向 floor, 右/下往正方向 ceil。PAD = BALL/2 - RB
        # 不是整数, 用 int() 截断时右侧会少挪 0.6px —— 球体于是比恢复条多露一截(实测露出
        # 4.6px vs 条厚 4px), 条的下沿会漏出一条 1px 的球体亮线。
        if edge in ("left", "right"):
            tx = (math.floor(e_px - BALL + PAD) if edge == "left"
                  else math.ceil(sw - e_px - PAD))
            ty = max(int(-PAD), min(y, int(sh - BALL + PAD)))
        else:
            ty = (math.floor(e_px - BALL + PAD) if edge == "top"
                  else math.ceil(sh - e_px - PAD))
            tx = max(int(-PAD), min(x, int(sw - BALL + PAD)))
        tx, ty = int(tx), int(ty)
        _anim([(BALL, BALL, int(x + (tx - x) * _ease(i / 6.0)),
                int(y + (ty - y) * _ease(i / 6.0))) for i in range(1, 7)])
        _geo(BALL, BALL, tx, ty)   # 收尾一帧: 必须走 _geo, 否则末位可能不落地
        st["hidden"] = True
        st["edge"] = edge
        st["hover"] = False
        # 收边后**把球体画布清空**: 球体虽然滑出去了, 它的外发光/全息环阵半径比球体大一圈
        # (最外到 RF*1.24 ≈ 0.5*BALL), 屏内那一侧还会留下几十像素高的抖动光晕 —— 实测
        # 条下面 1~14px 能看到稀稀拉拉的强调色亮点, 读起来还是"球没走干净"。
        # 清空之后屏上只剩恢复条本身(窗口那点余量是透明键色, 连点击都会穿透)。
        # show_ball 开头会重新 draw_ball(), 所以不需要在这里保存任何东西。
        try:
            ballcv.delete("all")
        except Exception:
            pass
        # 恢复条按**球心**对齐: 竖条贴左右缘(y 居中对球心), 横条贴上下缘(x 居中对球心)
        # 先把球窗重新压成置顶, 再建恢复条: 球体那 4px 余量现在正好在条下面, 顺序反了
        # (先建条、后给球设 -topmost)会把球抬到条上面, 屏上露出的就是球体的斜角切片而不是条。
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        _peer_show(edge, int(tx + BALL / 2.0), int(ty + BALL / 2.0))

    def _peer_show(edge, cx, cy):
        """恢复条: 独立小窗, 贴在球收进去的那条边上。默认 4px 细线, 指针靠近时变宽并显箭头。

        长边按**球体**取而不是按窗口取: 窗口比球大一圈透明边距, 照窗口高画出来的条会比球
        长一截, 看着不像"球收进去了", 像贴了根不相干的胶带。

        四缘通用: 左右缘是**竖条**(沿 y 长), 上下缘是**横条**(沿 x 长), 长边中点对齐球心。
        """
        w = st.get("peer")
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        p = tk.Toplevel(root)
        p.overrideredirect(True)
        horiz = edge in ("top", "bottom")     # 横条(上下缘)还是竖条(左右缘)
        LONG = max(8, int(RB * 2.6))          # 条子长边(比球体略高一点, 好瞄准)
        THIN = 4                              # 收起态露出的厚度
        cv = tk.Canvas(p, width=(LONG if horiz else THIN),
                       height=(THIN if horiz else LONG),
                       bg=C["accent"], highlightthickness=0)
        cv.pack()
        try:
            p.attributes("-topmost", True)
            p.attributes("-alpha", 0.92)
            p.lift()   # 必须压在球窗(也是置顶)之上: 条薄到 4px 时正盖着球体的余量
        except Exception:
            pass
        peer = {"w": p, "cv": cv, "edge": edge, "open": False,
                "by": int(cy - LONG / 2.0), "bh": LONG, "busy": False,
                "ww": THIN}   # ww=当前露出的厚度, _peer_tick 的"保持展开"判据要用

        def _draw(ww):
            """按当前厚度 ww 重画恢复条: 底 + 内缘亮线 + 三根刻度 + 箭头。"""
            cv.delete("all")
            if horiz:
                cv.config(width=LONG, height=ww)
                cv.create_rectangle(0, 0, LONG, ww,
                                    fill=_hex_mix(C["accent"], "#000000", 0.28),
                                    outline="")
                # 朝向屏幕内侧的那条亮边 —— 让 4px 细条也有一点"科技感", 不是一根纯色胶带
                iy = ww - 1 if edge == "top" else 0
                cv.create_line(0, iy, LONG, iy, fill=C["accent"], width=1)
                if ww >= 8:
                    for k in (0.24, 0.5, 0.76):
                        xx = int(LONG * k)
                        cv.create_line(xx, 2, xx, ww - 3,
                                       fill=_hex_mix(C["accent"], "#ffffff", 0.45), width=1)
                if ww >= 12:
                    cv.create_text(LONG // 2, ww // 2,
                                   text="︿" if edge == "top" else "﹀",
                                   fill="#ffffff", font=(_FONT, 8, "bold"))
            else:
                cv.config(width=ww, height=LONG)
                cv.create_rectangle(0, 0, ww, LONG,
                                    fill=_hex_mix(C["accent"], "#000000", 0.28),
                                    outline="")
                ix = ww - 1 if edge == "left" else 0
                cv.create_line(ix, 0, ix, LONG, fill=C["accent"], width=1)
                if ww >= 8:
                    for k in (0.24, 0.5, 0.76):
                        yy = int(LONG * k)
                        cv.create_line(2, yy, ww - 3, yy,
                                       fill=_hex_mix(C["accent"], "#ffffff", 0.45), width=1)
                if ww >= 12:
                    cv.create_text(ww // 2, LONG // 2, text="‹" if edge == "left" else "›",
                                   fill="#ffffff", font=(_FONT, 8, "bold"))

        def _place(ww):
            """按当前厚度 ww 摆窗口(贴边 + 长边居中对齐球心)并重画。"""
            peer["ww"] = ww
            if horiz:
                py = 0 if edge == "top" else (root.winfo_screenheight() - ww)
                p.geometry("%dx%d+%d+%d" % (LONG, ww, int(cx - LONG / 2.0), py))
            else:
                px = 0 if edge == "left" else (root.winfo_screenwidth() - ww)
                p.geometry("%dx%d+%d+%d" % (ww, LONG, px, int(cy - LONG / 2.0)))
            _draw(ww)
            p.update_idletasks()

        _place(THIN)   # 初始 4px 细条

        def _resize(tw):
            for i in range(1, 6):
                _place(int(THIN + (tw - THIN) * _ease(i / 5.0)))
                time.sleep(0.012)

        THICK = int(15 * scale)

        def _expand_peer():
            if peer["busy"] or peer["open"]:
                return
            peer["busy"] = True
            try:
                _resize(THICK)
                peer["open"] = True
            finally:
                peer["busy"] = False

        def _shrink_peer():
            if peer["busy"] or not peer["open"]:
                return
            peer["busy"] = True
            try:
                for i in range(4, -1, -1):
                    _place(int(THIN + (THICK - THIN) * _ease(i / 5.0)))
                    time.sleep(0.012)
                peer["open"] = False
            finally:
                peer["busy"] = False

        def _peer_tick():
            if not st["hidden"]:
                return
            # 指针贴近屏幕边缘那条 4px → 展开恢复条给个明确的"看得见"的抓手; 移开 → 缩回细线。
            # **缩回这一半原来漏了**: 展开后 peer["open"] 为真, 判据写成了
            # `if not peer["open"] and st["hidden"]` —— 一展开就短路, _shrink_peer 再没人调用,
            # 条子只要变宽一次就永远宽着(用户报的"移走之后没有恢复成窄条")。
            px_, py_ = root.winfo_pointerxy()
            sw_, sh_ = root.winfo_screenwidth(), root.winfo_screenheight()
            # 保持判据比展开判据宽一截(滞后): 条展开到 22px 后, 指针往里挪到十几 px 处明明
            # 还压在条上, 判据仍是"离屏缘<=6"的话条会立刻缩回, 用户根本点不到它。
            reach = 6 if not peer["open"] else peer["ww"] + 8
            if horiz:
                near = ((py_ <= reach) if edge == "top" else (py_ >= sh_ - reach)) \
                    and (cx - LONG / 2 - 10) <= px_ < (cx + LONG / 2 + 10)
            else:
                near = ((px_ <= reach) if edge == "left" else (px_ >= sw_ - reach)) \
                    and (cy - LONG / 2 - 10) <= py_ < (cy + LONG / 2 + 10)
            if near:
                if not peer["open"]:
                    _expand_peer()
            elif peer["open"]:
                _shrink_peer()
            p.after(160, _peer_tick)
        p.after(200, _peer_tick)
        cv.bind("<Button-1>", lambda e: show_ball())
        cv.config(cursor="hand2")
        st["peer"] = p

    def _peer_hide():
        w = st.get("peer")
        st["peer"] = None
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass

    def show_ball(_e=None, notify=True):
        """从屏幕边缘把球拉回来(恢复条还在时点它也能回来)。"""
        if not st["hidden"]:
            return
        _peer_hide()
        C.update(_ball_theme(port))
        draw_ball()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x, y = root.winfo_x(), root.winfo_y()
        edge = st.get("edge", "right")
        # 恢复落点跟启动默认位保持一致(球体离右缘 96px, 不贴边): 贴到最右缘那条会被
        # 任务栏通知区/置顶小窗盖住, 用户刚把球拉回来就"又找不到了"。
        # 左/上缘没有这个问题, 让球体边落在 24px 处; 下缘要避开任务栏, 同样收 96px。
        if edge == "left":
            tx, ty = int(24 - PAD), max(int(-PAD), min(y, int(sh - BALL + PAD)))
        elif edge == "right":
            tx, ty = _ball_win(sw), max(int(-PAD), min(y, int(sh - BALL + PAD)))
        elif edge == "top":
            tx, ty = max(int(-PAD), min(x, int(sw - BALL + PAD))), int(24 - PAD)
        else:   # bottom: 球体底缘落在离屏幕底 96px, 避开任务栏
            tx, ty = max(int(-PAD), min(x, int(sw - BALL + PAD))), int(sh - 96 - BALL + PAD)
        # 窗口尺寸全程不变, 只挪 x/y —— 窗口比球大一圈, 缩宽度会把球裁掉(见 hide_ball)
        _anim([(BALL, BALL, int(x + (tx - x) * _ease(i / 6.0)),
                int(y + (ty - y) * _ease(i / 6.0))) for i in range(1, 7)])
        _geo(BALL, BALL, tx, ty)   # 收尾帧: 走 _geo 确保落地
        st["hidden"] = False
        save_pos(tx, ty)
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

    hide_btn.bind("<Button-1>", lambda e: hide_ball())

    # ---------------- 开合只认点击(悬停自动展开已移除) ----------------
    # 用户明确要求: 鼠标停在球上**不许**自动打开面板, 必须点球才开。原来这里有一整套轮询
    # 指针位置的展开/收起逻辑(悬停 0.35s 自动展开、离开 1.2s 自动收起, 面板抽屉里还有个开关),
    # 已整体删除 —— 拖着球松手时指针正好停在球心, 面板会在 0.35 秒后自己弹出来, 既像
    # "球没点就开了", 也让"贴边隐藏"变难(手一停就展开)。现在:
    #   展开 = 单击球(on_release 里 not moved 且按下<0.8s) / 右键菜单「打开 AI 助手」
    #   收起 = 面板头部「─ 收起」/ 右键菜单「收起为悬浮球」
    #   隐藏 = 拖到屏幕边缘松手 / 面板头部「✕ 隐藏」/ 右键菜单; 恢复 = 点屏幕缘的恢复条
    # 悬停只保留**视觉**反馈(球体提亮 + 外圈弧, 见 poll 里的 _ptr_on_ball)。
    # 注意: 这套逻辑被删干净了 —— 源码里不应再出现它的名字(回归闸在 test_smoke 里),
    # 所以后面所有注释也只用中文描述, 不再写那些标识符。


    # ---------------- 拖拽(球面与面板头部通用) ----------------
    def on_press(e):
        st.update(down=True, moved=False, ox=e.x_root, oy=e.y_root,
                  wx=root.winfo_x(), wy=root.winfo_y(), t0=time.time())
        if st["view"] == "ball":
            st["drag"] = True   # 皮肤换成"拖拽中"(见 _ball_skin_state)

    def on_move(e):
        if not st["down"]:
            return
        if abs(e.x_root - st["ox"]) + abs(e.y_root - st["oy"]) > 5:
            st["moved"] = True
        # 钳制按**球体**算: 窗口比球大一圈透明边距, 用窗口宽钳会让球永远够不到屏幕边,
        # 「拖到边上松手 = 收起」就废了。允许窗口越出屏幕 PAD 像素, 正好让球体贴到边。
        nx = max(int(-PAD), min(st["wx"] + e.x_root - st["ox"],
                                int(root.winfo_screenwidth() - BALL + PAD)))
        ny = max(int(-PAD), min(st["wy"] + e.y_root - st["oy"],
                                int(root.winfo_screenheight() - BALL + PAD)))
        root.geometry("+%d+%d" % (nx, ny))
        root.update_idletasks()

    def on_release(_e):
        if not st["down"]:
            return
        st["down"] = False
        st["drag"] = False
        if not st["hidden"]:
            # 收边态的窗口坐标在屏外, 存下来没有意义(只会让下次启动白走一遍贴边纠正)。
            # 恢复/拖出来之后各有自己的落点写入(见下面两个分支)。
            save_pos(root.winfo_x(), root.winfo_y())
        # 拖到屏幕最边上松手 = 想收起来(与大多数贴边工具一致), 自动隐藏。
        # 判据同样是球体边缘而不是窗口边缘, 且**四缘都判**: 上下缘漏判是最早的回归 ——
        # 拖到上/下缘松手, 球就完整停在那儿, 因为没有任何一条分支认得这两个方向。
        # 取离得最近的那条边, 贴上了(<=1px)就往那个方向收。
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        near = min((root.winfo_x() + PAD, "left"),
                   (sw - root.winfo_x() - BALL + PAD, "right"),
                   (root.winfo_y() + PAD, "top"),
                   (sh - root.winfo_y() - BALL + PAD, "bottom"))
        if st["hidden"] and not st["moved"]:
            # 收边态下点到球体 = "把它拉回来", 而不是在**屏幕外**原地展开面板。
            # 原来这里会直接走下面的单击展开: 面板在屏外摆开、hidden 还是 True ——
            # 恢复条继续挂着, 球却已经不在那条边上了, 状态自相矛盾。
            show_ball()
            return
        if st["hidden"]:
            # 从收边态把球**拖**了出来: 先解除隐藏(撤掉恢复条), 再按正常拖拽结算 ——
            # 拖到中间就是"拿出来放这儿", 又拖到缘上松手则下面那条会再把它收进去。
            st["hidden"] = False
            _peer_hide()
            save_pos(root.winfo_x(), root.winfo_y())
        if st["view"] == "ball" and st["moved"] and near[0] <= 1:
            st["edge"] = near[1]
            hide_ball()
            return
        if st["view"] == "ball" and not st["moved"] and time.time() - st["t0"] < 0.8:
            expand()   # 原地快速松手 = 单击 → 展开成对话面板(悬停不会展开, 只有点它才开)

    def recall_ball():
        """把球拉回屏幕右侧一个明确可见的位置 —— 「我找不到球了」的兜底入口。
        不做动画、不离开屏幕, 就是稳稳放到右侧中间偏上(球体离右缘 96px, 避开任务栏通知区),
        然后置顶闪一下。用户点设置栏的「找不到球？点这里定位」或右键菜单走的就是这里:
        前端 -> GET /api/ball_recall_go(服务端计数 +1) -> 球的**心跳**打
        /api/ball_recall 读到计数变了 -> 调本函数。"""
        if st["hidden"]:
            show_ball()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        tx = _ball_win(sw)
        ty = _ball_top(sh)
        if st["view"] == "panel":
            collapse()
            root.update_idletasks()
        # 必须走 _geo: 光调 root.geometry 在这类分层窗口上不落地(见 _geo 的注释),
        # 表现就是"提示说球移回来了, 球却还在原地"。
        _geo(BALL, BALL, tx, ty)
        save_pos(tx, ty)
        try:
            root.attributes("-topmost", True)
            root.lift()
        except Exception:
            pass
        draw_ball()
        st["unread"] = False
        # 闪三下: 整颗球亮度抬起来 + 沿轮廓闪一圈亮边(比以前画个八边形准确得多, 因为现在
        # 的轮廓是实心体投影出来的凸包, 贴着球体走)。闪的是球自己, 不再另画"球外的一圈"。
        flash = {"n": 0}

        def _tick():
            if flash["n"] >= 6:
                st["flash"] = 0.0
                draw_ball_fx()
                return
            st["flash"] = 0.55 if flash["n"] % 2 == 0 else 0.0
            draw_ball_fx()
            flash["n"] += 1
            root.after(180, _tick)
        _tick()

    def on_right(e=None, at=None):
        """右键菜单。at=(x,y) 给虚拟输入层用(那条路径没有 Tk 事件)。"""
        menu = tk.Menu(root, tearoff=0)
        if st["view"] == "ball":
            menu.add_command(label="打开 AI 助手", command=expand)
        else:
            menu.add_command(label="收起为悬浮球", command=collapse)
        menu.add_command(label="定位悬浮球（拉回屏幕右侧）", command=recall_ball)
        menu.add_command(label="隐藏悬浮球（收进屏幕边缘）", command=hide_ball)
        menu.add_separator()
        menu.add_command(label="退出悬浮球", command=root.destroy)
        try:
            menu.tk_popup(at[0] if at else e.x_root, at[1] if at else e.y_root)
        finally:
            menu.grab_release()

    def _hoverable(w, base="card", hover_key="accent"):
        """给头部按钮加悬停反馈: 指针进入时底色转强调色、字变白, 离开还原。
        tkinter 的 Label 没有 :hover, 只能自己绑 Enter/Leave。"""
        def _in(_e=None):
            w.config(bg=C[hover_key], fg="#ffffff")
        def _out(_e=None):
            w.config(bg=C[base], fg=C["sub"])
        w.bind("<Enter>", _in)
        w.bind("<Leave>", _out)

    for w in (header, dot, title):
        w.bind("<Button-1>", on_press)
        w.bind("<B1-Motion>", on_move)
        w.bind("<ButtonRelease-1>", on_release)
        w.bind("<Button-3>", on_right)
    for _w in (collapse_btn, prefs_btn, new_btn):
        _hoverable(_w)
    _hoverable(hide_btn)
    # 球的悬停提亮改由 poll 里的**圆形**指针测试驱动(见 _ptr_on_ball), 不再用 <Enter>/<Leave>:
    # 窗口比球体大一圈, 而且外发光是抖动画的(只有一部分像素不透明), 指针扫过光晕时
    # Enter/Leave 会随抖动的点疯狂触发, 亮一下灭一下。轮询 90ms 一次反而稳。
    # 球面**不绑 Tk 鼠标事件**: 键控窗里只有画了东西的像素能收到消息(圆内 15.6%), 绑了也是
    # "偶尔生效"; 而且虚拟输入层同时在拖拽, 两条路径叠加会让球以两倍速度跑。球面交互一律走
    # virtual_input(); 面板头部是实心窗, 绑定照旧(见上面 header 那几个 bind)。
    ballcv.config(cursor="hand2")

    def _ptr_on_ball(pad=0):
        """指针是否贴在球体上(按圆判, 半径含全息环阵)。"""
        try:
            px, py = root.winfo_pointerxy()
        except Exception:
            return False
        cx = root.winfo_x() + BALL / 2.0
        cy = root.winfo_y() + BALL / 2.0
        rr = HIT_R + pad   # 与输入层的命中圆同一个半径: 看到"球亮了"的地方就一定能点到
        return (px - cx) ** 2 + (py - cy) ** 2 <= rr * rr

    # ---------------- 初始位置 / 心跳 / 主循环 ----------------
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    # 默认停在**右侧中间偏上**, 不要贴到最右缘: 最右缘正好是任务栏通知区/收起图标那一带,
    # 球贴在那儿很容易被别的置顶窗口或任务栏弹出面板盖住, 用户就"找不到球"。
    # 留出 96px 边距, 让球落在空白桌面上, 一眼就能看见。
    x, y = _ball_win(sw), _ball_top(sh)
    try:
        with open(_ball_pos_path(), encoding="utf-8") as f:
            pos = json.load(f)
        x, y = int(pos.get("x", x)), int(pos.get("y", y))
    except Exception:
        pass
    x, y = max(int(-PAD), min(x, int(sw - BALL + PAD))), \
        max(int(-PAD), min(y, int(sh - BALL + PAD)))
    # 位置文件是在**另一个会话/另一种屏幕布局**下写的时, 存下来的坐标可能落在当前屏幕之外,
    # 或贴着边缘(换显示器、拔掉外接屏、改缩放都会这样)。贴着右缘/上缘的球会被任务栏通知区、
    # 聊天软件贴边条、各种置顶小窗盖住 —— 用户就"找不到球"。所以只要贴边就主动往里挪。
    #
    # 判据分两档, 覆盖「越界」「贴边」和「越界后钳制恰好落回边缘」三种情况:
    #   a) 越界(x/y 超出屏幕) → 钳制之后一定紧贴边缘, 于是也满足下面的贴边判据;
    #   b) 贴边(离右缘不足一个球宽 / 顶上不足一个球高) → 挪到舒适位置。
    # 注意 y 要单独看「球顶离屏幕上缘」: y=4 这种几乎是贴着屏幕最上沿的, 会被顶部的
    # 各种悬浮工具条压住, 也该挪下来(实测用户上一次存的正是 (1967, 4))。
    #
    # 判据一律按**球体本身**算, 不按窗口矩形: 窗口比球体大一圈透明边距, 拿窗口判会把
    # "球离边缘还有大半个球"也算成贴边, 球就会莫名其妙被挪走。
    off_right = (x + BALL - PAD) > sw - RB * 2   # 球体右缘已进右侧一球宽的带子
    off_top = (y + PAD) < RB                     # 球体上缘离屏幕上沿不足一个球半径
    if off_right:
        x = _ball_win(sw)
    if off_top:
        y = _ball_top(sh)
    _geo(BALL, BALL, x, y)   # 启动落点: 走 _geo 确保落地
    # 画第一帧。**这一步原来漏了**: 画布底色是透明键色, 不画就等于什么都没有, 于是球起来
    # 之后在桌面上完全看不见 —— 直到用户碰巧展开一次再收起(走 apply_palette → draw_ball)
    # 或点「定位悬浮球」才第一次显形。用户报的「我找不到悬浮球啊, 是不是默认隐藏了」
    # 根子就在这: 不是隐藏, 是根本没画。
    draw_ball()
    try:   # 把自身 pid 写进位置文件: 主程序下次启动按它清理孤儿球(本次启动即写, 不等拖动)
        with open(_ball_pos_path(), "w", encoding="utf-8") as f:
            json.dump({"x": int(x), "y": int(y), "pid": os.getpid()}, f)
    except Exception:
        pass

    # 心跳: 服务重启/退出后自动销毁(连续 2 次探活失败即退, 防孤儿悬浮球);
    # 顺带取「定位悬浮球」信号 —— 用户在主界面点按钮时, 球可能正躲在屏幕边缘或某个
    # 想不起来的地方, 这一趟心跳就是把它叫回来的通道。
    misses = [0]
    recall_seen = [None]   # None = 还没跟服务端对齐过基线(见下)

    def heartbeat():
        try:
            with local_open("http://127.0.0.1:%d/api/ball_recall" % port,
                                        timeout=2) as r:
                ok = r.status == 200
                # 计数必须从**响应体**里读: 球是独立进程, 自己这边的 _BALL_RECALL 恒为 0,
                # 拿它去比就永远得不出「有新信号」(前端点了按钮球却不动, 就是这里踩的坑)。
                try:
                    cur = int(json.loads(r.read().decode("utf-8")).get("n", 0))
                except Exception:
                    cur = recall_seen[0]
                if recall_seen[0] is None:
                    # 首轮只对齐基线不动作: 球刚起来时本来就在可见位置,
                    # 而且服务端的 n 可能已经攒了好几次点击, 不校准的话会凭空闪一下。
                    recall_seen[0] = cur
                elif cur != recall_seen[0]:
                    recall_seen[0] = cur
                    try:
                        recall_ball()
                    except Exception:
                        pass
                    # 回执: 让主界面能确定「球真的响应了」, 而不是只显示一句
                    # 「已发送请求」让用户去猜。失败也无所谓, 前端会等到超时并如实提示。
                    try:
                        local_open(
                            "http://127.0.0.1:%d/api/ball_recall_ack?n=%d" % (port, cur),
                            timeout=2).read()
                    except Exception:
                        pass
        except Exception:
            ok = False
        if ok:
            misses[0] = 0
        else:
            misses[0] += 1
            if misses[0] >= 2:   # 连续两轮探活失败(最坏约5秒): 服务没了, 收球
                root.destroy()
                return
        root.after(2500, heartbeat)
    root.after(1500, heartbeat)
    poll()
    ball_anim()
    if "--ball-autoclose" in sys.argv:   # 测试钩子: N 毫秒后自动关闭(验证能正常建窗)
        try:
            root.after(int(sys.argv[sys.argv.index("--ball-autoclose") + 1]), root.destroy)
        except (ValueError, IndexError):
            pass
    if "--ball-autoexpand" in sys.argv:   # 测试钩子: N 毫秒后自动展开(验证面板布局)
        try:
            root.after(int(sys.argv[sys.argv.index("--ball-autoexpand") + 1]), expand)
        except (ValueError, IndexError):
            pass
    if "--ball-panel" in sys.argv:   # 测试钩子: 打开收藏夹/人设窗口(验证渲染不报错)
        try:
            _which = sys.argv[sys.argv.index("--ball-panel") + 1]
        except IndexError:
            _which = "saved"

        def _open_panel():
            expand()
            (show_saved if _which == "saved" else show_personas)()
        root.after(1200, _open_panel)
    if "--ball-autohide" in sys.argv:   # 测试钩子: N 毫秒后隐藏再显示(验证收边动效与恢复条)
        try:
            _d = int(sys.argv[sys.argv.index("--ball-autohide") + 1])
        except (ValueError, IndexError):
            _d = 800

        def _hide_show():
            hide_ball()
            root.after(max(300, _d), show_ball)
        root.after(_d, _hide_show)
    if "--ball-recall" in sys.argv:   # 测试钩子: N 毫秒后执行「定位悬浮球」
        try:
            _d = int(sys.argv[sys.argv.index("--ball-recall") + 1])
        except (ValueError, IndexError):
            _d = 800
        root.after(_d, recall_ball)
    if "--ball-solid" in sys.argv:   # 测试钩子: 切换实心体(ico=正20面体 / dode=正12面体, 与 shader 同形)
        try:
            _s = sys.argv[sys.argv.index("--ball-solid") + 1]
        except IndexError:
            _s = "ico"
        if _s in ("ico", "dode", "trunc"):
            globals()["_BALL_SOLID"] = _s
    def _fake_center():
        return root.winfo_x() + BALL // 2, root.winfo_y() + BALL // 2

    if "--ball-fake-click" in sys.argv:
        # 测试钩子: N 毫秒后注入"球心按下 → 0.18s 后抬起", 走虚拟输入层同一条状态机。
        # 用户明确要求别动他的鼠标(他在打游戏), 所以这条自测路径全程只改软件状态。
        try:
            _fc = int(sys.argv[sys.argv.index("--ball-fake-click") + 1])
        except (ValueError, IndexError):
            _fc = 3000

        def _arm_click():
            cx, cy = _fake_center()
            fake["seq"] = [(0.0, cx, cy, 1, 0), (0.18, cx, cy, 0, 0)]
            fake["t0"] = time.time()
        root.after(_fc, _arm_click)
    if "--ball-fake-drag" in sys.argv:
        # 测试钩子: N 毫秒后注入"球心按下 → 拖到屏幕右缘 → 松手", 验证**拖拽 + 贴边收起**
        # 这条最容易出错的路径(同样不碰真实鼠标)。
        try:
            _fd = int(sys.argv[sys.argv.index("--ball-fake-drag") + 1])
        except (ValueError, IndexError):
            _fd = 3000

        def _arm_drag():
            cx, cy = _fake_center()
            tx = root.winfo_screenwidth() - 2
            fake["seq"] = [(0.0, cx, cy, 1, 0), (0.25, tx, cy, 1, 0), (0.5, tx, cy, 0, 0)]
            fake["t0"] = time.time()
        root.after(_fd, _arm_drag)
    if "--ball-core" in sys.argv:   # 预览钩子: 换中心徽记(diamond/ripple/reticle/none)
        try:
            _c = sys.argv[sys.argv.index("--ball-core") + 1]
        except IndexError:
            _c = "diamond"
        if _c in ("diamond", "ripple", "reticle", "none"):
            globals()["_BALL_CORE"] = _c
    if "--ball-skin-demo" in sys.argv:   # 测试钩子: 循环切换球体皮肤(验证四种状态渲染)
        _SEQ = ["idle", "hover", "busy", "unread", "drag"]

        def _skin_demo(i=0):
            if i >= len(_SEQ) * 2:
                return
            _k = _SEQ[i % len(_SEQ)]
            st["hover"] = (_k == "hover")
            st["drag"] = (_k == "drag")
            st["unread"] = (_k == "unread")
            st["skin_force"] = _k   # 直接钉住状态, 绕开 sending["on"] 这类外部条件
            sync_skin()
            root.after(1500, lambda: _skin_demo(i + 1))
        root.after(900, _skin_demo)
    if "--ball-demo" in sys.argv:   # 测试钩子: N 毫秒后灌一版假的三段式结果(验证逐段渲染)
        def _demo():
            expand()
            user_bubble("喝可乐能杀灭蛔虫，这是老偏方了", "回怼")
            holder = ai_bubble()
            holder["gen"] = gen["n"]
            holder["src"] = "喝可乐能杀灭蛔虫，这是老偏方了"
            for payload in (
                {"kind": "targets", "stance_note": "证据一致否定该说法",
                 "evidence": [{"id": "E1", "title": "驱虫药使用说明", "url": "https://www.who.int/",
                               "cred": "权威"},
                              {"id": "E2", "title": "可乐成分与胃内稀释",
                               "url": "https://www.msdmanuals.com/", "cred": "可靠"}],
                 "items": [
                     {"point": "喝可乐能杀灭蛔虫", "kind": "知识误区", "note": "胃酸与稀释让可乐达不到杀虫浓度"},
                     {"point": "老偏方都靠谱", "kind": "诉诸传统", "note": "流传久不等于有效"}]},
                {"kind": "lines", "items": [
                    {"text": "可乐要能杀蛔虫，医院早改行卖饮料了", "basis": "公开医学资料",
                     "target": "知识误区", "evidence": ["E1"]},
                    {"text": "拿老偏方当论据，这思路我服", "basis": "公开资料",
                     "target": "诉诸传统", "evidence": []}]},
                {"kind": "paras", "items": [
                    {"text": "我拿公开资料跟你说话：可乐进胃就被胃酸和食物稀释，"
                             "根本到不了能杀虫的浓度，把这当偏方就是拿感觉代替剂量。",
                     "basis": "公开医学资料", "target": "知识误区", "evidence": ["E2"]}]},
                {"kind": "long", "items": [
                    {"text": "你这两条我都拆了：杀虫要靠剂量，可乐在胃里被稀释得剩不下什么，"
                             "拿老偏方压人更是把流传当成证据。要真有这效果，驱虫药早该停产了。",
                     "basis": "公开医学资料", "target": "整体反驳", "evidence": ["E1", "E2"]}]},
            ):
                seg_append(holder, payload)
                root.update_idletasks()
                time.sleep(0.25)
            rebut_tail(holder, {"offline": False}, [
                {"title": "驱虫药使用说明", "url": "https://www.who.int/", "cred_label": "权威",
                 "site": "www.who.int"}])
        try:
            root.after(int(sys.argv[sys.argv.index("--ball-demo") + 1]), _demo)
        except (ValueError, IndexError):
            pass
    root.mainloop()


def port_busy(port):
    try:
        with local_open("http://127.0.0.1:%d/api/feed" % port, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    # 悬浮球模式: 本进程只承载 tkinter 小球, 不起服务(必须在任何 DB 初始化之前分流)
    if "--float-ball" in sys.argv:
        try:
            _port = int(sys.argv[sys.argv.index("--float-ball") + 1])
        except (ValueError, IndexError):
            _port = 8000
        try:
            run_float_ball(_port)
        except Exception:
            import traceback
            try:   # windowed exe 没有 stdout, 错误落盘便于排查
                with open(os.path.join(DATA_DIR, "float_ball_err.log"), "w",
                          encoding="utf-8") as _f:
                    traceback.print_exc(file=_f)
            except Exception:
                pass
        sys.exit(0)
def run_server(auto_open=False):
    """启动本地服务: 评分守护/选端口/孤儿球清理/租约看门狗/悬浮球带出。

    返回 (server, port); 已有实例在跑 → (None, port), 端口全占 → (None, None)。
    serve_forever 由调用方决定跑在哪个线程: CLI 主线程 / 窗口壳(app_window.py)后台线程。
    """
    global _SERVER
    # v0.3.1.32: 自动化测试必须能关掉"自动开浏览器" —— frozen exe 的 --server 模式
    # 恒为 auto_open=True, 会弹一个浏览器页指向隔离测试服务; 页面一关就发
    # navigator.sendBeacon("/api/shutdown") 把测试服务杀掉(表现为后续请求全 502,
    # 且白白在用户桌面上弹窗)。置 MIRRORSORTER_NO_OPEN=1 即完全不开。
    if os.environ.get("MIRRORSORTER_NO_OPEN") == "1":
        auto_open = False
    # 评分器升级(v2: 标题党判低质/挂人开团扣帽子判引战/补小黑盒口径): 只做版本标记,
    # 不整体清评分缓存(全量清缓存会让全扫模式下首屏长时间空白; 新判据随条目自然重评生效)
    if sget("scorer_v") != "3":
        sset("scorer_v", "3")
    _ensure_score_bg()   # 启动后台渐进评分守护线程(评分写库, 交互路径只读缓存不阻塞)
    _ensure_prewarm()    # 启动常驻预热线程(池子薄/过久没新内容就后台提前抓, 用户不必等)
    threading.Thread(target=repair_blocklist_mids, daemon=True).start()   # v0.3.1.32: 补 ablock.mid
    for port in PORTS:
        if port_busy(port):
            print("服务已在 http://127.0.0.1:%d 运行" % port)
            if auto_open:
                webbrowser.open("http://127.0.0.1:%d" % port)
            return None, port
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        print("端口 %s 均被占用, 无法启动" % "/".join(str(x) for x in PORTS))
        return None, None
    _SERVER = server
    print("洞鉴虚明 " + APP_VER + " 已启动: http://127.0.0.1:%d" % port)
    print("按 Ctrl+C 停止服务")
    if os.name == "nt":
        _kill_stale_ball()   # 上次异常退出遗留的孤儿悬浮球(正常退出已被租约收走)
        threading.Thread(target=_lease_watchdog, daemon=True,
                         name="page-lease-watchdog").start()   # 兜底: 页面发不出leaving时收尾
    if auto_open:
        webbrowser.open("http://127.0.0.1:%d" % port)
    if sget("float_ball") == "1" and os.name == "nt":
        float_ball_spawn(port)   # 上次开着悬浮窗 → 本次启动自动带出
    return server, port


def shutdown(server=None):
    """统一收尾: 停服务/收悬浮球/停线程池与登录会话。CLI 与窗口壳(app_window.py)共用。"""
    global _SERVER
    if server is not None:
        try:
            server.shutdown()      # 退出 serve_forever 循环(不能在服务线程自身调用)
        except Exception:
            pass
        try:
            server.server_close()  # 关监听 socket
        except Exception:
            pass
    float_ball_stop()   # 服务退出时收球, 不留孤儿
    for executor in (_FX, _SERIOUS_X, _SCORE_X, _TAG_X, _EVIDENCE_X):
        executor.shutdown(wait=False, cancel_futures=True)
    tb_login_stop()
    _SERVER = None


if __name__ == "__main__":
    # 悬浮球模式: 本进程只承载 tkinter 小球, 不起服务(必须在任何 DB 初始化之前分流)
    if "--float-ball" in sys.argv:
        try:
            _port = int(sys.argv[sys.argv.index("--float-ball") + 1])
        except (ValueError, IndexError):
            _port = 8000
        try:
            run_float_ball(_port)
        except Exception:
            import traceback
            try:   # windowed exe 没有 stdout, 错误落盘便于排查
                with open(os.path.join(DATA_DIR, "float_ball_err.log"), "w",
                          encoding="utf-8") as _f:
                    traceback.print_exc(file=_f)
            except Exception:
                pass
        sys.exit(0)
    _server, _port = run_server(
        auto_open=("--auto-open" in sys.argv) or bool(getattr(sys, "frozen", False)))
    if _server is None:
        sys.exit(0 if _port is not None else 1)
    try:
        _server.serve_forever()
    finally:
        shutdown(_server)
