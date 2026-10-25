# -*- coding: utf-8 -*-
"""浏览器式收藏栏(仅供 app_window.py 窗口壳使用)。

外观照主流浏览器: 每项 = 站点图标 + 标题, 平时没有底色, 指针悬停才淡淡高亮;
放不下时右端出现「»」翻页(翻回去显示「«」), 不用滚动条。
位置(自上而下): 标签栏 → 导航工具栏 → **本收藏栏** → WebView2 内容区。
- 左端「▾」是折叠开关: 收起后整条只剩一个「▸」箭头, 内容区随之上移;
- 点收藏项 = 当前标签页打开, 若当前是信息流固定页则新开一个标签页
  (信息流本体永远不被顶掉);
- 悬停收藏项时右端浮出 × = 删除(立即生效并落库);
- 导航栏的 ☆ 收藏当前网页; 网址为空(信息流固定页)时不可点。
站点图标用**站点自己的**: 抓 `/favicon.ico`(SPA 返回 HTML 时解析页面声明的
`rel=icon` / `apple-touch-icon`), 转成 32×32 PNG 缓存到本机数据目录; 只有确实取不到
时才退化成首字母占位。

持久化: settings 表 `ui_bookmarks`, 一个 JSON:
    {"v": 1, "collapsed": false, "items": [{"t": 标题, "u": 网址}, ...]}
解析容忍旧/坏数据(裸列表、缺键、非 http(s)、重复网址): 坏数据只丢弃, 不抛错。
"""
import json
import os
import re
import threading

KEY = "ui_bookmarks"
BOOKMARK_H = 30        # 展开态逻辑高度(px)
COLLAPSED_H = 16       # 收起态逻辑高度(px)
MAX_ITEMS = 200
MAX_TITLE = 40
COLLAPSED_GLYPH = "\u25b8"   # 收起态只留这一个箭头(用户要求)
EXPANDED_GLYPH = "\u25be"    # 展开态左端的折叠开关
OVERFLOW_NEXT = "\u00bb"     # 右端还有: »
OVERFLOW_PREV = "\u00ab"     # 已翻页: «

ICON_SIZE = 32               # 图标缓存尺寸(px); 绘制时按 DPI 缩放
ICON_MAX_BYTES = 512 * 1024
ICON_MISS_TTL = 7 * 86400    # 抓取失败 7 天后再试
ICON_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

_URL_RE = re.compile(r"^https?://", re.I)


def favicon_of(url):
    """确实拿不到站点图标时的占位: 域名首字母 + 中性底色(不冒充站点 logo)。"""
    h = _host(url).lower()
    for pre in ("www.", "m."):
        if h.startswith(pre) and len(h) > len(pre):
            h = h[len(pre):]
            break
    letter = "?"
    for ch in h:
        if ch.isalnum():
            letter = ch.upper()
            break
    return {"letter": letter, "color": (96, 102, 114)}


def _origin(url):
    """收藏网址 → (scheme, host); 非 http(s) 返回 None。"""
    try:
        from urllib.parse import urlsplit
        sp = urlsplit(str(url or ""))
        if sp.scheme.lower() not in ("http", "https") or not sp.netloc:
            return None
        return (sp.scheme.lower(), sp.netloc.lower())
    except Exception:
        return None


def _icon_cache_dir():
    try:
        import app
        base = app.DATA_DIR
    except Exception:
        base = os.path.join(os.path.expanduser("~"), "AppData", "LocalLow",
                            "MirrorSorter")
    return os.path.join(base, "favicon_cache")


def _read_url(url, timeout=6):
    """取一个小资源: (最终URL, Content-Type, bytes) / None。只认 http(s), 有大小上限。"""
    try:
        from urllib.parse import urlsplit
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            return None
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": ICON_UA,
            "Accept": ("image/avif,image/webp,image/png,image/svg+xml,"
                       "image/*,*/*;q=0.8"),
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(ICON_MAX_BYTES + 1)
            if len(data) > ICON_MAX_BYTES:
                return None
            return (r.geturl() or url,
                    (r.headers.get("Content-Type") or "").lower(), data)
    except Exception:
        return None


def _sniff(data):
    """按内容判断图标/HTML, 不信 Content-Type(不少站点 .ico 其实是 PNG/HTML)。"""
    if not data:
        return None
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"BM"):
        return "bmp"
    head = data[:4096].lstrip().lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "svg"
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<html" in head:
        return "html"
    return None


_ATTR_RE = re.compile(r"""([a-zA-Z_:][-\w:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")


def _icon_links(html, base_url):
    """从首页 HTML 里收集声明的图标地址(普通 icon 优先, apple-touch-icon 兜底)。"""
    from urllib.parse import urljoin
    normal, apple = [], []
    for tag in re.findall(r"<link\b[^>]*>", html or "", re.I)[:200]:
        attrs = {}
        for m in _ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = (m.group(3) or m.group(4) or
                                         m.group(5) or "")
        rel = attrs.get("rel", "").lower()
        href = (attrs.get("href") or "").strip()
        if not href or "icon" not in rel or href.startswith("data:"):
            continue
        u = urljoin(base_url, href)
        if "apple-touch-icon" in rel:
            apple.append(u)
        elif not u.lower().split("?")[0].endswith(".svg"):
            normal.append(u)
    return normal + apple


def _icon_to_png(data, kind=None):
    """ICO/PNG/JPEG/GIF/BMP → 32×32 透明底 PNG; 失败 None。"""
    kind = kind or _sniff(data)
    if kind not in ("ico", "png", "jpg", "gif", "bmp"):
        return None
    try:
        import clr
        clr.AddReference("System.Drawing")
        import System.Drawing as Drawing
        from System.Drawing import Bitmap, Color, Graphics, Rectangle
        from System.Drawing.Drawing2D import (CompositingQuality,
                                              InterpolationMode,
                                              PixelOffsetMode)
        from System.Drawing.Imaging import ImageFormat, PixelFormat
        from System.IO import MemoryStream

        ms = MemoryStream(bytes(data))
        if kind == "ico":
            src = Drawing.Icon(ms).ToBitmap()
        else:
            src = Drawing.Image.FromStream(ms)
        try:
            if src.Width <= 0 or src.Height <= 0:
                return None
            out = Bitmap(ICON_SIZE, ICON_SIZE, PixelFormat.Format32bppArgb)
            g = Graphics.FromImage(out)
            try:
                g.Clear(Color.Transparent)
                g.CompositingQuality = CompositingQuality.HighQuality
                g.InterpolationMode = InterpolationMode.HighQualityBicubic
                g.PixelOffsetMode = PixelOffsetMode.HighQuality
                k = min(ICON_SIZE / float(src.Width), ICON_SIZE / float(src.Height))
                dw = max(1, int(round(src.Width * k)))
                dh = max(1, int(round(src.Height * k)))
                g.DrawImage(src, Rectangle((ICON_SIZE - dw) // 2,
                                           (ICON_SIZE - dh) // 2, dw, dh))
            finally:
                g.Dispose()
            om = MemoryStream()
            out.Save(om, ImageFormat.Png)
            png = bytes(om.ToArray())
            out.Dispose()
            return png
        finally:
            src.Dispose()
    except Exception:
        return None


def fetch_site_icon(scheme, host):
    """抓站点自己的图标 → 32×32 PNG bytes / None。

    先 /favicon.ico; 若返回 HTML(SPA 常见, 如 chat.deepseek.com)就解析页面里的
    <link rel="icon"> / apple-touch-icon; 再兜底根目录 apple-touch-icon.png。
    """
    base = "%s://%s" % (scheme, host)
    got = _read_url(base + "/favicon.ico")
    if got:
        final, _ct, data = got
        kind = _sniff(data)
        if kind in ("ico", "png", "jpg", "gif", "bmp"):
            png = _icon_to_png(data, kind)
            if png:
                return png
        elif kind == "html":
            html = data.decode("utf-8", "replace")
            for u in _icon_links(html, final)[:6]:
                r = _read_url(u)
                if not r:
                    continue
                k = _sniff(r[2])
                if k in ("ico", "png", "jpg", "gif", "bmp"):
                    png = _icon_to_png(r[2], k)
                    if png:
                        return png
    for name in ("apple-touch-icon.png", "apple-touch-icon-precomposed.png"):
        r = _read_url(base + "/" + name)
        if not r:
            continue
        k = _sniff(r[2])
        if k in ("ico", "png", "jpg", "gif", "bmp"):
            png = _icon_to_png(r[2], k)
            if png:
                return png
    return None


def _load_icon_png(path):
    try:
        import clr
        clr.AddReference("System.Drawing")
        import System.Drawing as Drawing
        from System.IO import MemoryStream
        with open(path, "rb") as f:
            data = f.read()
        src = Drawing.Image.FromStream(MemoryStream(bytes(data)))
        try:
            return Drawing.Bitmap(src)   # 复制一份, 不锁文件
        finally:
            src.Dispose()
    except Exception:
        return None


def _write_atomic(path, data):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


class FaviconCache:
    """站点图标磁盘缓存 + 单线程后台抓取。

    - 绘制线程只做内存/磁盘命中, **绝不联网等待**;
    - 磁盘命中失败才排队抓取(每次间隔 gap 秒), 成功后通知订阅者重绘;
    - 失败写 .miss 标记, ICON_MISS_TTL 内不再重试。
    fetcher(scheme, host) 可注入(测试用), 返回 PNG bytes 或 None。
    """

    def __init__(self, cache_dir=None, fetcher=None, gap=0.25):
        self._dir = cache_dir if cache_dir is not None else _icon_cache_dir()
        self._fetch = fetcher or fetch_site_icon
        self._gap = max(0.0, float(gap))
        self._lock = threading.RLock()
        self._imgs = {}
        self._queue = []
        self._queued = set()
        self._thread = None
        self._subs = []
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception:
            pass

    def subscribe(self, fn):
        with self._lock:
            self._subs.append(fn)
        return fn

    def _notify(self):
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn()
            except Exception:
                pass

    def _path(self, host):
        import hashlib
        key = hashlib.sha1(host.encode("utf-8")).hexdigest()
        return os.path.join(self._dir, key + ".png")

    def _miss_path(self, host):
        return self._path(host)[:-4] + ".miss"

    def _miss_fresh(self, host):
        try:
            import time
            return time.time() - os.path.getmtime(self._miss_path(host)) < ICON_MISS_TTL
        except Exception:
            return False

    def image(self, url):
        """同步拿图标(内存/磁盘); 没有就排队抓取并返回 None。"""
        o = _origin(url)
        if o is None:
            return None
        host = o[1]
        with self._lock:
            if host in self._imgs:
                return self._imgs[host]
        img = _load_icon_png(self._path(host))
        with self._lock:
            if host in self._imgs:
                return self._imgs[host]
            if img is not None:
                self._imgs[host] = img
                return img
        if not self._miss_fresh(host):
            self._enqueue(o)
        return None

    def preload(self, urls):
        for u in urls:
            o = _origin(u)
            if o is None:
                continue
            host = o[1]
            with self._lock:
                if host in self._imgs:
                    continue
            if os.path.exists(self._path(host)) or self._miss_fresh(host):
                continue
            self._enqueue(o)

    def _enqueue(self, origin):
        host = origin[1]
        with self._lock:
            if host in self._queued or host in self._imgs:
                return
            if len(self._queue) > 400:
                return
            self._queue.append((origin[0], host))
            self._queued.add(host)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="msx-favicons")
                self._thread.start()

    def _run(self):
        import time
        while True:
            with self._lock:
                if not self._queue:
                    self._thread = None
                    return
                scheme, host = self._queue.pop(0)
            img = None
            try:
                p = self._path(host)
                img = _load_icon_png(p)
                if img is None:
                    data = None
                    try:
                        data = self._fetch(scheme, host)
                    except Exception:
                        data = None
                    if data:
                        _write_atomic(p, data)
                        img = _load_icon_png(p)
                    else:
                        _write_atomic(self._miss_path(host), b"")
                with self._lock:
                    self._imgs[host] = img
                    self._queued.discard(host)
                if img is not None:
                    self._notify()
            except Exception:
                with self._lock:
                    self._queued.discard(host)
                    self._imgs.setdefault(host, None)
            if self._gap:
                time.sleep(self._gap)


def bookmark_h(scale=1.0, collapsed=False):
    base = COLLAPSED_H if collapsed else BOOKMARK_H
    return max(12, int(base * scale))


def _norm_key(url):
    u = str(url or "").strip()
    if not _URL_RE.match(u):
        return ""
    return u.rstrip("/")


def _host(url):
    try:
        from urllib.parse import urlsplit
        h = urlsplit(url).netloc or url
        return h.rsplit("@", 1)[-1]
    except Exception:
        return url


def _clamp_title(title, url):
    t = " ".join(str(title or "").split())
    if t and t not in ("页面", "新标签页"):
        return t[:MAX_TITLE]
    return _host(url)[:MAX_TITLE]


def parse_bookmarks(raw):
    """把库里存的 JSON 解析成 (items, collapsed); 任何异常/坏条目都安全丢弃。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
    except Exception:
        data = None
    collapsed = False
    if isinstance(data, dict):
        collapsed = bool(data.get("collapsed"))
        arr = data.get("items")
    elif isinstance(data, list):
        arr = data
    else:
        arr = None
    items, seen = [], set()
    for it in (arr or []):
        if isinstance(it, dict):
            url = it.get("u") or it.get("url") or ""
            title = it.get("t") or it.get("title") or ""
        elif isinstance(it, str):
            url, title = it, ""
        else:
            continue
        url = str(url).strip()
        key = _norm_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        items.append({"title": _clamp_title(title, url), "url": url})
        if len(items) >= MAX_ITEMS:
            break
    return items, collapsed


def dump_bookmarks(items, collapsed=False):
    arr = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        if not _URL_RE.match(url):
            continue
        arr.append({"t": str(it.get("title") or ""), "u": url})
    return json.dumps({"v": 1, "collapsed": bool(collapsed), "items": arr},
                      ensure_ascii=False, separators=(",", ":"))


def _default_get():
    import app
    return app.sget(KEY)


def _default_set(value):
    import app
    app.sset(KEY, value)


class BookmarkStore:
    """收藏数据 + settings 落库。getter/setter 可注入(测试用, 不碰真库)。"""

    def __init__(self, getter=None, setter=None):
        self._get = getter or _default_get
        self._set = setter or _default_set
        self._lock = threading.RLock()
        self._items = []
        self._collapsed = False
        self._subs = []
        self.reload()

    def reload(self):
        try:
            raw = self._get()
        except Exception:
            raw = None
        items, collapsed = parse_bookmarks(raw)
        with self._lock:
            self._items = items
            self._collapsed = collapsed

    def items(self):
        with self._lock:
            return [dict(x) for x in self._items]

    def has(self, url):
        key = _norm_key(url)
        if not key:
            return False
        with self._lock:
            return any(_norm_key(x["url"]) == key for x in self._items)

    @property
    def collapsed(self):
        with self._lock:
            return self._collapsed

    def subscribe(self, fn):
        self._subs.append(fn)
        return fn

    def _save_locked(self):
        try:
            self._set(dump_bookmarks(self._items, self._collapsed))
        except Exception:
            pass

    def _notify(self):
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn()
            except Exception:
                pass

    def add(self, url, title=""):
        url = str(url or "").strip()
        key = _norm_key(url)
        if not key:
            return False
        changed = False
        with self._lock:
            hit = next((x for x in self._items if _norm_key(x["url"]) == key), None)
            if hit is not None:
                # 已收藏: 只刷新标题(站点标题常在页面加载后才更新)
                t = _clamp_title(title, hit["url"])
                if t != hit["title"]:
                    hit["title"] = t
                    changed = True
            else:
                if len(self._items) >= MAX_ITEMS:
                    del self._items[0]
                self._items.append({"title": _clamp_title(title, url), "url": url})
                changed = True
            if changed:
                self._save_locked()
        if changed:
            self._notify()
        return True

    def remove(self, url):
        key = _norm_key(url)
        if not key:
            return False
        with self._lock:
            before = len(self._items)
            self._items = [x for x in self._items if _norm_key(x["url"]) != key]
            removed = len(self._items) != before
            if removed:
                self._save_locked()
        if removed:
            self._notify()
        return removed

    def toggle(self, url, title=""):
        if self.has(url):
            return "removed" if self.remove(url) else "na"
        return "added" if self.add(url, title) else "na"

    def set_collapsed(self, on):
        on = bool(on)
        with self._lock:
            if on == self._collapsed:
                return False
            self._collapsed = on
            self._save_locked()
        self._notify()
        return True


def open_bookmark(api, url):
    """收藏项点击的落点: 信息流固定页 → 新标签; 其他页面 → 当前标签内导航。
    返回 "tab"/"current"(仅便于测试断言)。"""
    try:
        page = api.active_page()
    except Exception:
        page = None
    if page and page.get("pinned"):
        try:
            api.open_in_app(url, "", mode="tab")
        except Exception:
            pass
        return "tab"
    try:
        api.nav_goto(url)
    except Exception:
        pass
    return "current"


def install_bookmarks(win, api, top=0, store=None, on_height=None, bg_getter=None,
                      icons=None):
    """在窗口 y=top 处安装收藏栏(须在 install_navbar 之后调用)。

    on_height(h): 本栏高度变化时回调(窗口壳用它把 WebView2 内容区整体下移/回缩)。
    icons: FaviconCache(测试注入; 默认磁盘缓存 + 后台抓取站点真实图标)。
    返回可调用对象, 附带 .bar(Panel) / .set_fullscreen(on) / .store。
    """
    import clr
    clr.AddReference("System.Drawing")
    from System.Drawing import (Color, Font, FontStyle, Point, Rectangle,
                                Size, Pen, SolidBrush)
    from System.Drawing.Drawing2D import (GraphicsPath, InterpolationMode,
                                          PixelOffsetMode, SmoothingMode)
    import System.Windows.Forms as WinForms

    form = win.native
    scale = float(getattr(form, "_scale", 1.0) or 1.0)
    store = store or BookmarkStore()
    icons = icons or FaviconCache()
    st = {
        "bar": Color.FromArgb(22, 25, 32),
        "hover": Color.FromArgb(38, 43, 54),
        "fg": Color.FromArgb(226, 230, 238),
        "fg_dim": Color.FromArgb(118, 124, 136),
        "sep": Color.FromArgb(48, 52, 62),
        "danger": Color.FromArgb(226, 96, 96),
        "hover_toggle": False,
        "hover_item": -1,
        "hover_close": -1,
        "hover_arrow": None,
        "start": 0,           # 展开态当前页从第几项开始(超过一页时翻页用)
    }
    item_font = Font("Microsoft YaHei", 8.5 * scale)
    badge_font = Font("Microsoft YaHei", 7.0 * scale, FontStyle.Bold)
    close_font = Font("Microsoft YaHei", 8.0 * scale)
    arrow_font = Font("Microsoft YaHei", 10.0 * scale)

    FLAGS_L = (WinForms.TextFormatFlags.Left |
               WinForms.TextFormatFlags.VerticalCenter |
               WinForms.TextFormatFlags.EndEllipsis |
               WinForms.TextFormatFlags.NoPrefix |
               WinForms.TextFormatFlags.SingleLine)
    FLAGS_C = (WinForms.TextFormatFlags.HorizontalCenter |
               WinForms.TextFormatFlags.VerticalCenter |
               WinForms.TextFormatFlags.NoPrefix |
               WinForms.TextFormatFlags.SingleLine)

    def _p(k):
        return int(k * scale)

    def _toggle_w():
        return _p(24)

    def _close_w():
        return _p(18)

    icon_w = _p(16)
    icon_gap = _p(6)
    pad = _p(6)
    gap = _p(4)
    ARROW_W = _p(22)

    bar = WinForms.Panel()
    bar.DoubleBuffered = True
    bar.Location = Point(0, int(top))
    bar.Size = Size(form.ClientSize.Width, bookmark_h(scale, store.collapsed))
    bar.Anchor = (WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Left |
                  WinForms.AnchorStyles.Right)
    bar.BackColor = st["bar"]

    def _rounded(x, y, w, hh, rad):
        p = GraphicsPath()
        d = rad * 2
        p.AddArc(x, y, d, d, 180, 90)
        p.AddArc(x + w - d, y, d, d, 270, 90)
        p.AddArc(x + w - d, y + hh - d, d, d, 0, 90)
        p.AddArc(x, y + hh - d, d, d, 90, 90)
        p.CloseFigure()
        return p

    wcache = {"key": None, "ws": None}

    def _widths():
        """项宽缓存: 鼠标移动每次都要命中测试, 不能每次对所有标题重新 MeasureText。"""
        its = store.items()
        key = tuple((it["title"], it["url"]) for it in its)
        if wcache["key"] == key:
            return wcache["ws"]
        ws = []
        for it in its:
            tw = int(WinForms.TextRenderer.MeasureText(it["title"], item_font).Width)
            w = pad + icon_w + icon_gap + tw + _p(3) + _close_w() + pad
            ws.append((it, min(_p(210), max(_p(64), w))))
        wcache["key"] = key
        wcache["ws"] = ws
        return ws

    def _layout():
        """一次算出展开态的项位置(绘制与命中共用, 避免两边算法漂移)。

        只摆**整项**能放下的收藏(不做半截裁剪, TextRenderer 不吃 Graphics 裁剪区);
        放不下时从「»」翻到下一页, 翻过页后出现「«」。
        返回值: over / boxes / start / end / max_start / offset / area_x / area_w /
                can_prev / can_next。
        """
        ws = _widths()
        n = len(ws)
        area_x = _toggle_w()
        raw = bar.Width - area_x - pad
        total = sum(w for _it, w in ws) + gap * max(0, n - 1)
        over = total > raw
        zone = (2 * ARROW_W + _p(4)) if over else 0
        area_w = max(1, raw - zone)
        # 窗口变宽/删项后 start 可能已越界: 收紧到"后缀能放下"的最早位置(只往回, 不会跳项)
        max_start, acc = n, 0
        for i in range(n - 1, -1, -1):
            add = ws[i][1] + (gap if max_start < n else 0)
            if acc + add > area_w:
                break
            acc += add
            max_start = i
        start = max(0, int(st["start"]))
        if start > max_start:
            start = max_start
        st["start"] = start

        boxes, x, end = [], area_x, start - 1
        for i in range(start, n):
            it, w = ws[i]
            bx = x if i == start else x + gap
            if bx + w > area_x + area_w:
                break
            cx = bx + w - pad - _close_w()
            boxes.append((it, bx, w, cx, _close_w()))
            x = bx + w
            end = i
        offset = 0
        for i in range(start):
            offset += ws[i][1] + gap
        return {"over": over, "boxes": boxes, "start": start, "end": end,
                "max_start": max_start, "offset": offset,
                "area_x": area_x, "area_w": area_w,
                "can_prev": start > 0, "can_next": end < n - 1}

    def _arrow_rects(over):
        if not over:
            return {}
        right = bar.Width - _p(6)
        return {"prev": (right - 2 * ARROW_W - _p(2), 0, ARROW_W, bar.Height),
                "next": (right - ARROW_W, 0, ARROW_W, bar.Height)}

    def _paint_arrows(g, lay, hh):
        for name, (ax, _ay, aw, ah) in _arrow_rects(lay["over"]).items():
            if not (lay["can_prev"] if name == "prev" else lay["can_next"]):
                continue
            if st["hover_arrow"] == name:
                g.FillPath(SolidBrush(st["hover"]),
                           _rounded(ax + _p(2), _p(3), aw - _p(4),
                                    ah - _p(6), _p(4)))
            WinForms.TextRenderer.DrawText(
                g, OVERFLOW_PREV if name == "prev" else OVERFLOW_NEXT, arrow_font,
                Rectangle(ax, 0, aw, hh), st["fg"], FLAGS_C)

    def on_paint(_sender, e):
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        g.InterpolationMode = InterpolationMode.HighQualityBicubic
        g.PixelOffsetMode = PixelOffsetMode.HighQuality
        g.Clear(st["bar"])
        hh = bar.Height
        try:
            g.DrawLine(Pen(st["sep"], 1.0), 0, hh - 1, bar.Width, hh - 1)
        except Exception:
            pass
        n = len(store.items())
        if store.collapsed:
            # 收起态只留一个箭头; 整条都是展开热区(见 _hit)
            if st["hover_toggle"]:
                g.FillPath(SolidBrush(st["hover"]),
                           _rounded(_p(2), _p(1), _toggle_w() - _p(4),
                                    max(6, hh - _p(2)), _p(4)))
            WinForms.TextRenderer.DrawText(
                g, COLLAPSED_GLYPH, close_font,
                Rectangle(0, 0, _toggle_w(), hh), st["fg"], FLAGS_C)
            return
        # 展开态: 左端只有一个折叠箭头, 与浏览器收藏栏一样不放标题块
        if st["hover_toggle"]:
            g.FillPath(SolidBrush(st["hover"]),
                       _rounded(_p(2), _p(3), _toggle_w() - _p(4),
                                hh - _p(6), _p(4)))
        WinForms.TextRenderer.DrawText(
            g, EXPANDED_GLYPH, arrow_font, Rectangle(0, 0, _toggle_w(), hh),
            st["fg"] if st["hover_toggle"] else st["fg_dim"], FLAGS_C)
        lay = _layout()
        boxes = lay["boxes"]
        if not boxes:
            if n == 0:
                WinForms.TextRenderer.DrawText(
                    g, "暂无收藏：打开网页后点地址栏右侧的 \u2606 收藏", item_font,
                    Rectangle(lay["area_x"] + _p(2), 0,
                              max(0, lay["area_w"]), hh),
                    st["fg_dim"], FLAGS_L)
            _paint_arrows(g, lay, hh)
            return
        py = _p(4)
        ph = max(8, hh - _p(8))
        for idx, (it, x, w, cx, cw) in enumerate(boxes):
            hot = st["hover_item"] == idx or st["hover_close"] == idx
            if hot:   # 浏览器式: 平时无底色, 悬停才浮出淡高亮
                g.FillPath(SolidBrush(st["hover"]),
                           _rounded(x, py, w, ph, _p(4)))
            fav = favicon_of(it["url"])
            fr, fgc, fb = fav["color"]
            bx = x + pad
            by = (hh - icon_w) // 2
            ico = icons.image(it["url"])
            if ico is not None:
                try:
                    g.DrawImage(ico, Rectangle(bx, by, icon_w, icon_w))
                except Exception:
                    ico = None
            if ico is None:   # 站点没给图标 / 还没抓到: 首字母占位, 不冒充 logo
                g.FillPath(SolidBrush(Color.FromArgb(fr, fgc, fb)),
                           _rounded(bx, by, icon_w, icon_w, _p(3)))
                WinForms.TextRenderer.DrawText(
                    g, fav["letter"], badge_font,
                    Rectangle(bx, by, icon_w, icon_w), Color.White, FLAGS_C)
            tx = bx + icon_w + icon_gap
            tw = max(0, (x + w - pad - _close_w()) - tx)
            WinForms.TextRenderer.DrawText(
                g, it["title"], item_font, Rectangle(tx, 0, tw, hh),
                st["fg"], FLAGS_L)
            if hot:   # × 只在悬停该项时浮出, 平时保持浏览器的干净排版
                WinForms.TextRenderer.DrawText(
                    g, "\u00d7", close_font, Rectangle(cx, 0, cw, hh),
                    st["danger"] if st["hover_close"] == idx else st["fg_dim"],
                    FLAGS_C)
        _paint_arrows(g, lay, hh)

    def _hit(x, y):
        if y < 0 or y > bar.Height:
            return (None, None, -1)
        if store.collapsed:
            return ("toggle", None, -1)   # 收起态整条都是展开热区(只画一个箭头)
        if x <= _toggle_w():
            return ("toggle", None, -1)
        lay = _layout()
        for name, (ax, _ay, aw, _ah) in _arrow_rects(lay["over"]).items():
            on = lay["can_prev"] if name == "prev" else lay["can_next"]
            if on and ax <= x <= ax + aw:
                return (name, None, -1)
        if x > lay["area_x"] + lay["area_w"]:
            return (None, None, -1)
        for idx, (it, bx, w, cx, cw) in enumerate(lay["boxes"]):
            if bx <= x <= bx + w:
                if cx <= x <= cx + cw:
                    return ("close", it, idx)
                return ("item", it, idx)
        return (None, None, -1)

    def on_mouse_move(_sender, args):
        kind, _it, idx = _hit(args.X, args.Y)
        ht = (kind == "toggle")
        hi = idx if kind in ("item", "close") else -1
        hc = idx if kind == "close" else -1
        ha = kind if kind in ("prev", "next") else None
        if (ht, hi, hc, ha) != (st["hover_toggle"], st["hover_item"],
                                st["hover_close"], st["hover_arrow"]):
            st["hover_toggle"], st["hover_item"] = ht, hi
            st["hover_close"], st["hover_arrow"] = hc, ha
            bar.Invalidate()

    def on_mouse_leave(_sender, _args):
        if (st["hover_toggle"] or st["hover_item"] >= 0 or
                st["hover_close"] >= 0 or st["hover_arrow"] is not None):
            st["hover_toggle"], st["hover_item"] = False, -1
            st["hover_close"], st["hover_arrow"] = -1, None
            bar.Invalidate()

    def on_click(_sender, args):
        if args.Button != WinForms.MouseButtons.Left:
            return
        kind, it, _idx = _hit(args.X, args.Y)
        if kind == "toggle":
            store.set_collapsed(not store.collapsed)
        elif kind == "prev":
            lay = _layout()
            st["start"] = max(0, lay["start"] - max(1, len(lay["boxes"])))
            st["hover_item"], st["hover_close"] = -1, -1
            bar.Invalidate()
        elif kind == "next":
            lay = _layout()
            st["start"] = min(lay["end"] + 1, lay["max_start"])
            st["hover_item"], st["hover_close"] = -1, -1
            bar.Invalidate()
        elif kind == "close" and it is not None:
            store.remove(it["url"])
        elif kind == "item" and it is not None:
            open_bookmark(api, it["url"])

    def _apply_layout():
        want = bookmark_h(scale, store.collapsed)
        try:
            if bar.Height != want:
                bar.Height = want
        except Exception:
            pass
        if on_height is not None:
            try:
                on_height(bar.Height)
            except Exception:
                pass
        try:
            if not store.collapsed:
                _layout()   # 删项/窗口变窄后把翻页位置收回有效范围
        except Exception:
            pass
        try:
            icons.preload([it["url"] for it in store.items()])
        except Exception:
            pass
        bar.Invalidate()

    def _on_store_change():
        try:
            if bar.InvokeRequired:
                bar.BeginInvoke(WinForms.MethodInvoker(_apply_layout))
            else:
                _apply_layout()
        except Exception:
            pass

    store.subscribe(_on_store_change)

    def _on_icon_ready():
        try:
            if bar.InvokeRequired:
                bar.BeginInvoke(WinForms.MethodInvoker(bar.Invalidate))
            else:
                bar.Invalidate()
        except Exception:
            pass

    icons.subscribe(_on_icon_ready)

    bar.Paint += on_paint
    bar.MouseMove += on_mouse_move
    bar.MouseLeave += on_mouse_leave
    bar.MouseClick += on_click

    def on_resize(_s=None, _e=None):
        if not store.collapsed:
            try:
                _layout()
            except Exception:
                pass
        bar.Invalidate()
    bar.Resize += on_resize

    def _install():
        try:
            form.Controls.Add(bar)
            bar.BringToFront()
        except Exception:
            import traceback
            traceback.print_exc()
        _apply_layout()

    if form.InvokeRequired:
        form.BeginInvoke(WinForms.MethodInvoker(_install))
    else:
        _install()

    def set_scheme(raw):
        try:
            sch = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
            if not isinstance(sch, dict):
                return

            def _c(key, fallback):
                v = str(sch.get(key) or "")
                if re.match(r"^#[0-9a-fA-F]{6}$", v):
                    return Color.FromArgb(int(v[1:3], 16), int(v[3:5], 16),
                                          int(v[5:7], 16))
                return fallback

            # 与导航栏同一块"底板": 取 active(激活标签色)而不是 bar, 否则收藏栏会在
            # 导航栏下面接一条更深的色带, 顶栏看起来像被切开
            st["bar"] = _c("active", st["bar"])
            st["hover"] = _c("hover", st["hover"])
            st["fg"] = _c("fgon", st["fg"])
            st["fg_dim"] = _c("fgoff", st["fg_dim"])
            st["sep"] = _c("sep", st["sep"])

            def apply():
                try:
                    bar.BackColor = st["bar"]
                except Exception:
                    pass
                bar.Invalidate()

            if bar.InvokeRequired:
                bar.BeginInvoke(WinForms.MethodInvoker(apply))
            else:
                apply()
        except Exception:
            pass

    def scheme_watch():
        import time as _t
        last = None
        while True:
            try:
                if bg_getter:
                    cur = bg_getter()
                    if cur != last:
                        last = cur
                        set_scheme(cur)
            except Exception:
                pass
            _t.sleep(2)

    if bg_getter:
        threading.Thread(target=scheme_watch, daemon=True,
                         name="msx-bookmarks-scheme").start()

    def set_fullscreen(on):
        """网页/视频全屏 → 整条收藏栏让位(内容区铺满由 shell_pages 负责)。"""
        def _apply():
            try:
                bar.Visible = not bool(on)
                if not on:
                    bar.BringToFront()
            except Exception:
                pass
        try:
            if bar.InvokeRequired:
                bar.BeginInvoke(WinForms.MethodInvoker(_apply))
            else:
                _apply()
        except Exception:
            pass

    def update():
        return bar.Height

    update.bar = bar
    update.store = store
    update.icons = icons
    update.set_fullscreen = set_fullscreen
    update.offset = lambda: int(_layout()["offset"])   # 当前页首项之前的像素宽度(测试用)
    # 测试用: 布局结果与命中判定直接暴露, 免得测试里再抄一份几何常量
    update.layout = _layout
    update.hit = _hit
    return update
