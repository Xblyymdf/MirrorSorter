# -*- coding: utf-8 -*-
"""v0.4.2dsk 回归自测（离线；临时数据目录；不发真实网络请求）。

覆盖：
1. 搜索框显式触发（右侧按钮 / Enter；输入过程不搜、清空退回信息流）+ 版本同步
2. BV 号直达（URL/大小写/脏文本提取、view 接口解析、分页到底、search_all 路由）
3. 搜索相关度（原标题 / UP主名 / 前缀 / 包含 / 热度同档决胜 / 稳定排序）
4. 贴吧·小黑盒占比=0 → 后台完全停抓（早退 + fetch_pool 提交门控）
5. 风控收敛（B站闸门、小黑盒顺序两页、池抓取最小间隔+抖动、设置后恢复）
6. 相似内容防扎堆（MinHash 分带、计划层打散、serve 层最近 6 条暂缓 + 小池救急）
"""
import io
import inspect
import json
import os
import re
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="msx_v042_")
os.environ["MIRRORSORTER_DATA_DIR"] = TMP

PASS = 0
FAIL = []


def ok(cond, name, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + ("  " + str(extra) if extra else ""))


def read(p):
    return io.open(p, encoding="utf-8", newline="").read().replace("\r\n", "\n")


def fsrc(fn):
    return inspect.getsource(fn).replace("\r\n", "\n")


import app  # noqa: E402

APP_SRC = read(os.path.join(HERE, "app.py"))
HTML_SRC = read(os.path.join(HERE, "web", "index.html"))
JS_SRC = read(os.path.join(HERE, "web", "app.js"))
CSS_SRC = read(os.path.join(HERE, "web", "style.css"))

NOW = time.time()


def mkitem(**kw):
    u = kw.get("url") or "https://www.bilibili.com/video/BV1xx411c7mD"
    it = {"key": u, "platform": "B站", "title": "普通标题", "author": "作者",
          "author_mid": 1, "desc": "简介", "cover": "", "views": 5000,
          "views_known": True, "likes": 10, "danmaku": 1, "reply": 1,
          "duration": 300, "duration_known": True,
          "pubdate": int(NOW - 86400), "url": u, "tag": ""}
    it.update(kw)
    return it


# ================= 1. 版本 + 搜索框显式触发 =================
ok(bool(re.fullmatch(r"v0\.4\.\d+dsk", app.APP_VER)), "APP_VER 形如 v0.4.Ndsk: " + app.APP_VER)
ok(APP_SRC.count("洞鉴虚明 · 基础版 (" + app.APP_VER + ")") == 1, "app.py 头注释版本同步")
ok(HTML_SRC.count("洞鉴虚明 " + app.APP_VER) == 2, "index.html 两处页脚版本同步")
ok('APP_VER = "' + app.APP_VER + '"' in APP_SRC, "APP_VER 常量写死")

i_in = HTML_SRC.find('id="searchInput"')
i_btn = HTML_SRC.find('id="btnSearch"')
ok(i_in > 0 and i_btn > i_in, "搜索按钮在输入框右侧（DOM 顺序）")
ok('type="button"' in HTML_SRC[i_btn:i_btn + 180], "搜索按钮 type=button（不提交表单）")
ok('aria-label="搜索"' in HTML_SRC[i_btn:i_btn + 180], "搜索按钮有无障碍标签")
ok("#topSearch .searchBtn" in CSS_SRC and "--accent-strong" in CSS_SRC, "搜索按钮样式在 CSS 里")
ok('$("#btnSearch").onclick = runSearch;' in JS_SRC, "点击按钮 → runSearch")
ok('if (e.key === "Enter")' in JS_SRC and "runSearch();" in JS_SRC, "Enter → runSearch")
i_inp = JS_SRC.find('$("#searchInput").addEventListener("input"')
i_end = JS_SRC.find("});", i_inp)
input_body = JS_SRC[i_inp:i_end]
ok(i_inp > 0 and "restartFeed" not in input_body and "runSearch" not in input_body,
   "输入过程不自动搜索（input 只处理清空退出）")
ok("exitSearch();" in input_body, "清空输入 → 自动退回信息流")
ok("searchTimer" not in JS_SRC and "debounce" not in JS_SRC.lower(), "旧的防抖自动搜索已移除")
ok('$("#searchInput").value = q;' in JS_SRC, "runSearch 在 setView 后写回搜索词")
ok("S.q = q;" in JS_SRC and "restartFeed();" in JS_SRC, "runSearch 设置搜索词并重取首屏")

# ================= 2. BV 号直达 =================
cases = [
    ("BV1xx411c7mD", "BV1xx411c7mD"),
    ("bv1xx411c7mD", "BV1xx411c7mD"),
    ("https://www.bilibili.com/video/BV1xx411c7mD/?p=2&spm=1", "BV1xx411c7mD"),
    ("看这个 BV1xx411c7mD 不错", "BV1xx411c7mD"),
    ("BV123", ""),
    ("xBV1xx411c7mD", ""),
    ("BV1xx411c7mDx", ""),
]
for text, want in cases:
    got = app._extract_bvid(text)
    ok(got == want, "BV 提取: %r → %r" % (text, got), got)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


_orig_urlopen = app.urllib.request.urlopen
_orig_guard = app._net_guard
_orig_video = app.bili_video_by_bvid
_orig_bg = app._request_bg_score
_orig_first = app._search_first_wave
try:
    app._net_guard = lambda *a, **k: True
    app.urllib.request.urlopen = lambda req, timeout=10: _FakeResp({
        "code": 0,
        "data": {"bvid": "BV1xx411c7mD", "aid": 1,
                 "title": "原神4.2版本前瞻直播完整回放",
                 "desc": "官方直播回放内容整理", "pic": "//i0.hdslb.com/a.jpg",
                 "owner": {"mid": 7, "name": "某UP"},
                 "stat": {"view": 12345, "like": 678, "danmaku": 90, "reply": 12},
                 "duration": 3600, "pubdate": int(NOW - 3600), "tname": "游戏"}})
    vid = app.bili_video_by_bvid("BV1xx411c7mD")
    ok(vid and vid.get("title") == "原神4.2版本前瞻直播完整回放", "view 接口解析标题")
    ok(vid and vid.get("author") == "某UP" and vid.get("views") == 12345, "view 接口解析作者/播放量")
    ok(vid and vid.get("src") == "BV直达" and vid.get("gid") == "search", "BV 条目打上直达标记")
    ok(vid and vid.get("cover", "").startswith("https://"), "封面 http→https")

    app.urllib.request.urlopen = lambda req, timeout=10: _FakeResp({"code": -404})
    ok(app.bili_video_by_bvid("BV1xx411c7mD") is None, "不存在的 BV → None")
finally:
    app.urllib.request.urlopen = _orig_urlopen
    app._net_guard = _orig_guard

fake_bv = mkitem(title="原神4.2版本前瞻直播完整回放", author="某UP",
                 url="https://www.bilibili.com/video/BV1xx411c7mD",
                 key="https://www.bilibili.com/video/BV1xx411c7mD")
try:
    app.bili_video_by_bvid = lambda b: dict(fake_bv)
    app._request_bg_score = lambda items: None
    r1 = app._search_bvid("BV1xx411c7mD", "BV1xx411c7mD", 0, 30, False)
    ok(len(r1["items"]) == 1 and r1["items"][0]["title"] == fake_bv["title"], "BV 首页返回这一条")
    ok(r1["has_more"] is False and r1["end"] is True, "BV 查询一页即到底（不再扩波）")
    r2 = app._search_bvid("BV1xx411c7mD", "BV1xx411c7mD", 30, 30, False)
    ok(r2["items"] == [] and r2["has_more"] is False, "BV 第二页为空且停止")
    app.bili_video_by_bvid = lambda b: None
    r3 = app._search_bvid("BV1xx411c7mD", "BV1xx411c7mD", 0, 30, False)
    ok(r3["items"] == [] and r3["end"] is True, "取不到 BV → 空结果到底")
finally:
    app.bili_video_by_bvid = _orig_video
    app._request_bg_score = _orig_bg

try:
    app.bili_video_by_bvid = lambda b: dict(fake_bv)
    app._request_bg_score = lambda items: None

    def _no_wave(*a, **k):
        raise AssertionError("BV 查询不应进入波次搜索")

    app._search_first_wave = _no_wave
    routed = app.search_all("https://www.bilibili.com/video/BV1xx411c7mD/?spm=x")
    ok(len(routed["items"]) == 1 and routed["has_more"] is False,
       "search_all 识别 BV 并走直达（不进波次）")
    off = app.search_all("BV1xx411c7mD", offset=30)
    ok(off["items"] == [] and off["has_more"] is False, "BV 深页直接到底")
finally:
    app._search_first_wave = _orig_first
    app.bili_video_by_bvid = _orig_video
    app._request_bg_score = _orig_bg

# ================= 3. 搜索相关度 =================
Q = "原神4.2版本前瞻直播完整回放"
pool = [
    mkitem(title=Q, author="某UP", views=5000),
    mkitem(title="UP主的其他视频", author=Q, views=900000),
    mkitem(title="【原神】4.2版本前瞻直播完整回放 Reaction", author="搬运工", views=100000),
    mkitem(title="浅谈原神 4.2 版本前瞻直播 完整回放", author="路人", views=2000),
    mkitem(title="崩坏星穹铁道1.3版本前瞻直播", author="米哈游", views=999999),
]
rank = app._rerank_search(Q, pool)
ok(rank[0]["title"] == Q, "原标题完全一致排第一", rank[0]["title"])
ok(rank[1]["author"] == Q, "UP主名完全一致排第二（仍低于原标题）")
ok(rank[0] is pool[0], "不复制条目，返回原对象")

author_q = "影视飓风"
apool = [mkitem(title="我们拍了一部科幻短片", author=author_q, views=100),
         mkitem(title="影视飓风幕后揭秘", author="另一个UP", views=999999)]
arank = app._rerank_search(author_q, apool)
ok(arank[0]["author"] == author_q, "搜 UP 主名时该作者的视频排第一")

pref_q = "原神 4.2 前瞻"
ppool = [mkitem(title="浅谈原神 4.2 前瞻节目", author="a", views=999999),
         mkitem(title="原神 4.2 前瞻节目完整回放", author="b", views=1)]
prank = app._rerank_search(pref_q, ppool)
ok(prank[0]["title"].startswith("原神 4.2 前瞻"), "标题前缀命中高于仅包含")

hpool = [mkitem(title="浅谈原神4.2版本前瞻直播完整回放", author="a", views=100),
         mkitem(title="也谈原神4.2版本前瞻直播完整回放", author="b", views=1000000)]
hrank = app._rerank_search(Q, hpool)
ok(hrank[0]["author"] == "b", "同一档内热度高的排前")

spool = [mkitem(title="原神4.2版本前瞻直播完整回放", author="同", views=1),
         mkitem(title="原神4.2版本前瞻直播完整回放", author="同", views=1)]
srank = app._rerank_search(Q, spool)
ok(srank[0]["key"] == spool[0]["key"], "同分保持抓取顺序（稳定排序）")
ok("视频" not in app._match_terms("原神 视频"), "通用词不进匹配词元（防噪声）")

# ================= 4. 占比=0 → 后台完全停抓 =================
ok(app.xhh_weight() == 15, "xhh_weight 默认 15")
app.sset("xhh_weight", "")
ok(app.xhh_weight() == 15, "xhh_weight 空值回默认")
app.sset("xhh_weight", "abc")
ok(app.xhh_weight() == 15, "xhh_weight 非法值回默认")
app.sset("xhh_weight", "150")
ok(app.xhh_weight() == 15, "xhh_weight 越界回默认")
app.sset("xhh_weight", "0")
ok(app.xhh_weight() == 0, "xhh_weight 支持 0")

_orig_tb_users = app.tb_users
_orig_xhh_feed = app.xhh_feed_items
try:
    def _boom(*a, **k):
        raise AssertionError("占比=0 时不应触达平台")

    app.tb_users = _boom
    app.sset("tb_weight", "0")
    ok(app.fetch_tieba_pool(False) == ([], []), "贴吧占比=0: 完整抓取直接空返回")
    ok(app.fetch_tieba_pool(True) == ([], []), "贴吧占比=0: 快速抓取直接空返回")

    app.xhh_feed_items = _boom
    app._xhh_pool_last_ts = 0
    ok(app.fetch_xhh_pool(False) == ([], []), "小黑盒占比=0: 完整抓取直接空返回")
    ok(app.fetch_xhh_pool(True) == ([], []), "小黑盒占比=0: 快速抓取直接空返回")

    # 占比>0 时仍受最小间隔约束：刚抓过就再抓 → 直接空返回、不打接口
    app.sset("xhh_weight", "15")
    app._xhh_pool_last_ts = time.time()
    ok(app.fetch_xhh_pool(False) == ([], []), "小黑盒冷却期内不重复抓取")
finally:
    app.tb_users = _orig_tb_users
    app.xhh_feed_items = _orig_xhh_feed

tb_src = fsrc(app.fetch_tieba_pool)
xhh_src = fsrc(app.fetch_xhh_pool)
fp_src = fsrc(app.fetch_pool)
ok("if tb_weight() <= 0:\n        return [], []" in tb_src, "fetch_tieba_pool 顶部有权重早退")
ok("if xhh_weight() <= 0:\n        return [], []" in xhh_src, "fetch_xhh_pool 顶部有权重早退")
ok("if tb_weight() > 0:" in fp_src and "if xhh_weight() > 0:" in fp_src, "fetch_pool 有权重门控")
def _gate_before(src, needle, gate):
    """needle 所在语句向上最近的 8 空格缩进行必须是 gate（即整个提交处在 if 门控块里）。"""
    pos = src.find(needle)
    if pos < 0:
        return False
    for ln in reversed(src[:pos].split("\n")):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) == 8:
            return ln.strip() == gate
    return False


for pool_name in ("_FX", "_SERIOUS_X"):
    for fn_name, w in (("fetch_tieba_pool", "tb_weight"), ("fetch_xhh_pool", "xhh_weight")):
        needle = "%s.submit(%s" % (pool_name, fn_name)
        pos = 0
        while True:
            pos = fp_src.find(needle, pos)
            if pos < 0:
                break
            ok(_gate_before(fp_src[:pos + len(needle)], needle, "if %s() > 0:" % w),
               "%s 提交处在 %s 门控块内" % (fn_name + "." + pool_name, w))
            pos += len(needle)
ok("tb_weight" not in fsrc(app.tb_search_items),
   "显式搜索不受占比=0 影响（tb_search_items 不读权重）")

# ================= 5. 风控收敛 =================
ok(app._BUILD_MIN_GAP == 40, "补池最小间隔 40s")
ok(app._TB_POOL_MIN_GAP == 150, "贴吧池抓取最小间隔 150s")
ok(app._XHH_POOL_MIN_GAP == 90, "小黑盒池抓取最小间隔 90s")
ok(app._SERVE_SPACING_GAP == 6, "serve 防扎堆窗口 6 条")
ok("random.uniform(1.0, 1.5)" in tb_src, "贴吧抓取间隔带 1.0~1.5 倍抖动")
ok("random.uniform(1.0, 1.4)" in xhh_src, "小黑盒抓取间隔带抖动")
ok("_net_guard(\"bili\", 0.35, 110, wait=1.5)" in fsrc(app.search_items), "B站搜索有出站闸门")
ok("_net_guard(\"bili\", 0.4, 90, wait=1.0)" in fsrc(app._safe_json), "B站榜单有出站闸门")
ok("for offset in offsets:" in xhh_src and "range(2)" in xhh_src, "小黑盒完整抓取改为 2 页顺序")
ok("_FX.map(xhh_feed_items" not in APP_SRC and "_FX.submit(xhh_feed_items" not in APP_SRC,
   "小黑盒不再并发多页")
ok(fsrc(app.xhh_get).count('_net_pace("xhh-api", 0.8)') == 1, "xhh_get 请求间隔 0.8s")
ok(fsrc(app.xhh_post).count('_net_pace("xhh-api", 0.8)') == 1, "xhh_post 请求间隔 0.8s")
ok("240" in fsrc(app._build_backoff), "空抓退避上限存在")

_orig_net = app._NET
_orig_on = app._NET_GUARD_ON
try:
    app._NET = {"last": {}, "minute": {}, "min": 0, "total": {}, "penalty": {}, "pace": {}}
    app._NET_GUARD_ON = True
    ok(app._net_guard("t042", 60, 10) is True, "闸门: 首次放行")
    ok(app._net_guard("t042", 60, 10) is False, "闸门: 间隔不足直接放弃（不排队）")

    app._NET = {"last": {}, "minute": {}, "min": 0, "total": {}, "penalty": {}, "pace": {}}
    got = [app._net_guard("t042q", 0, 2) for _ in range(3)]
    ok(got == [True, True, False], "闸门: 每分钟额度用尽后拒绝", got)

    app._net_penalty("t042p", 60, 2.0)
    ok(app.net_stats()["penalty"].get("t042p", 0) > 0, "惩罚期进入 net_stats（诊断可见）")

    class _FakeTime:
        def __init__(self):
            self.t = 5000.0

        def time(self):
            return self.t

        def sleep(self, s):
            slept.append(s)
            self.t += s

    real_time = app.time
    slept = []
    app.time = _FakeTime()
    try:
        app._net_pace("t042pace", 0.5)
        first = len(slept)
        app._net_pace("t042pace", 0.5)
        second = len(slept)
    finally:
        app.time = real_time
    ok(first == 0 and second == 1, "net_pace: 同站点连打会拉开间隔", (first, second))
    ok(slept and 0 < slept[0] <= 2.5, "net_pace 睡眠有上限", slept)
finally:
    app._NET = _orig_net
    app._NET_GUARD_ON = _orig_on

# ================= 6. 相似内容防扎堆 =================
near1 = "原神4.2版本前瞻直播完整回放"
near2 = "《原神》4.2版本前瞻直播完整回放（4K修复）"
unrel = "Blender从零开始建模教程"
b1, b2 = set(app._sim_bands(near1)), set(app._sim_bands(near2))
ok(len(b1 & b2) >= 2, "近重复标题共享 ≥2 个分带", len(b1 & b2))
ok(not (set(app._sim_bands(near1)) & set(app._sim_bands(unrel))), "无关标题不共享分带")
ok("42" in app._sim_tokens(near1), "4.2 归一为 42（标点不参与分带）")
keys = app._spacing_keys(mkitem(title=near1, author="某UP"))
ok(any(k.startswith("a:") for k in keys), "间距键含作者")
ok(any(k.startswith("f:") for k in keys), "间距键含标题指纹")
ok(any(k.startswith("s:") for k in keys), "间距键含 MinHash 分带")

pool6 = ([mkitem(title="甲的作品%d" % i, author="甲", key="k-a%d" % i) for i in range(4)] +
         [mkitem(title="乙的作品%d" % i, author="乙", key="k-b%d" % i) for i in range(4)] +
         [mkitem(title="丙的作品%d" % i, author="丙", key="k-c%d" % i) for i in range(4)])
spaced = app._space_same_author(pool6)
ok(len(spaced) == len(pool6), "计划层打散不丢条目")
ok({x["key"] for x in spaced} == {x["key"] for x in pool6}, "计划层打散后条目集合不变")
ok(all(spaced[i]["author"] != spaced[i + 1]["author"] for i in range(len(spaced) - 1)),
   "计划层同作者不相邻（3 作者小池）")

_orig_filters = app.apply_filters
_orig_plan = app._feed_plan
_orig_kick = app.kick_build
_orig_preheat = app._cold_start_preheat
_orig_bg2 = app._request_bg_score
_orig_uni = app._universe
try:
    app.apply_filters = lambda items, q: list(items)
    app._feed_plan = lambda acts, new_session, gen: list(acts)
    app.kick_build = lambda quick=False: None
    app._cold_start_preheat = lambda: False
    app._request_bg_score = lambda items: None
    app._universe = {"gen": 1, "items": list(pool6), "source": "test", "offline": False,
                     "ai": False, "mode": "标准", "account": False, "building": False,
                     "built_at": int(time.time()), "partitions": "全部"}
    app._session_served.clear()
    p1 = app.serve_chunk(0, 6, "", "t042serve", "")
    p2 = app.serve_chunk(6, 6, "", "t042serve", "")
    keys1 = [x["key"] for x in p1["items"]]
    keys2 = [x["key"] for x in p2["items"]]
    authors1 = [x["author"] for x in p1["items"]]
    authors2 = [x["author"] for x in p2["items"]]
    ok(len(keys1) == 6, "serve 首页在 3 作者小池里也能吐满", keys1)
    ok(len(keys2) == 6, "serve 第二页在暂缓后仍能救急吐满（不空转）", keys2)
    ok(not (set(keys1) & set(keys2)), "serve 跨页不重复")
    ok(len(set(keys1 + keys2)) == 12, "serve 全池 12 条最终全部送出")
    ok(all(authors1[i] != authors1[i + 1] for i in range(len(authors1) - 1)),
       "serve 首页同作者不相邻", authors1)
    ok(all(authors2[i] != authors2[i + 1] for i in range(len(authors2) - 1)),
       "serve 第二页同作者不相邻", authors2)
finally:
    app.apply_filters = _orig_filters
    app._feed_plan = _orig_plan
    app.kick_build = _orig_kick
    app._cold_start_preheat = _orig_preheat
    app._request_bg_score = _orig_bg2
    app._universe = _orig_uni
    app._session_served.clear()

print()
print("RESULT: %d PASS, %d FAIL%s" % (PASS, len(FAIL), (" -> " + ", ".join(FAIL)) if FAIL else ""))
sys.exit(1 if FAIL else 0)
