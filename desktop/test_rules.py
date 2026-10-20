# -*- coding: utf-8 -*-
"""新规则回归测试(纯函数级, 不走HTTP): 情绪/广告过滤器 · 搜索屏蔽开关 · 偏好词豁免。
运行: python test_rules.py"""
import importlib.util
import json
import os
import shutil
import sys
import time

PROJECT = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(PROJECT, "build", "probe", "rules-tmp")
if os.path.isdir(TMP):
    shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


os.environ["MIRRORSORTER_DATA_DIR"] = TMP
spec = importlib.util.spec_from_file_location("app_rules", os.path.join(PROJECT, "app.py"))
app = importlib.util.module_from_spec(spec)
sys.modules["app_rules"] = app
spec.loader.exec_module(app)
app.DATA_DIR = TMP

NOW = time.time()


def mkitem(**kw):
    it = {"key": kw.get("url", "https://www.bilibili.com/video/BVtest00001"),
          "platform": "B站", "title": "普通标题", "author": "作者", "desc": "简介",
          "cover": "", "views": 50000, "views_known": True, "likes": 100,
          "danmaku": 10, "reply": 5, "duration": 300, "duration_known": True,
          "pubdate": int(NOW - 86400), "url": "https://www.bilibili.com/video/BVtest00001",
          "tag": "标签"}
    it.update(kw)
    return it


# ---------- 1. 情绪/标题党/广告判定 ----------
check("感叹号: 3个拦截", app._emo_bad(mkitem(title="太离谱了！！！")) == "情绪")
check("感叹号: 2个且短标题拦截", app._emo_bad(mkitem(title="惊了！！")) == "情绪")
check("感叹号: 2个且长标题放行", app._emo_bad(mkitem(title="这个版本更新确实改善了不少细节哦！！")) == "")
check("感叹号: ‼计入合计", app._emo_bad(mkitem(title="惊‼！")) == "情绪")
check("问号轰炸: 4个拦截", app._emo_bad(mkitem(title="这游戏还行？？？？")) == "情绪")
check("问号轰炸: 3个且短标题拦截", app._emo_bad(mkitem(title="离谱吗？？？")) == "情绪")
check("混合轰炸: 1叹2问拦截", app._emo_bad(mkitem(title="这就是夏日祭！？？")) == "情绪")
check("问号: 2个放行", app._emo_bad(mkitem(title="什么是好设计？？")) == "")
check("叠词: 同字4连拦截", app._emo_bad(mkitem(title="哈哈哈哈这游戏太好玩了")) == "情绪")
check("叠词: 二字词3连拦截", app._emo_bad(mkitem(title="完了完了完了停服了")) == "情绪")
check("叠词: 拉丁重复不误伤", app._emo_bad(mkitem(title="wwww 笑死")) == "")
check("标题党词: 震惊拦截", app._emo_bad(mkitem(title="震惊！新机发布")) == "标题党")
check("标题党句: 绝对化拦截", app._emo_bad(mkitem(title="全网最强拍照手机横评")) == "标题党")
check("标题党句: 悬念党拦截", app._emo_bad(mkitem(title="事件内幕竟然是这样")) == "标题党")
check("标题党句: 诱导党拦截", app._emo_bad(mkitem(title="这个设置再不看就亏了")) == "标题党")
check("标题党句: 拉踩式拦截", app._emo_bad(mkitem(title="新引擎吊打全场实测")) == "标题党")
check("标题党句: 拉踩数字对比不误伤", app._emo_bad(mkitem(title="4090吊打4080测试")) == "")
check("标题党句: 挑衅式拦截", app._emo_bad(mkitem(title="不服来辩")) == "标题党")
check("正常标题放行", app._emo_bad(mkitem(title="原神3.0版本实机演示与解析")) == "")
check("正常教程放行", app._emo_bad(mkitem(title="Blender从零开始建模教程01")) == "")

check("广告: 硬标记命中", app._ad_bad(mkitem(desc="需要的加微信领取")) is True)
check("广告: 软标记1个放行", app._ad_bad(mkitem(desc="欢迎关注公众号看更多教程")) is False)
check("广告: 软标记2个拦截", app._ad_bad(mkitem(desc="粉丝群已建，进群领资料")) is True)
check("广告: 正常简介放行", app._ad_bad(mkitem(desc="纯教程内容，工程文件在简介")) is False)
check("广告: 空简介放行", app._ad_bad(mkitem(desc="")) is False)
check("情绪/广告过滤漏斗标签", app._FUNNEL_LABELS.get("emotion") == "情绪/广告过滤")

# ---------- 2. 搜索屏蔽开关 ----------
app.sset("dislike_note", "原神")


def rf(title, **kw):
    it = mkitem(title=title, **kw)
    return it


kept_block = app.rules_filter([rf("原神新版本解析")], skip_age=True, apply_blocks=True)
kept_free = app.rules_filter([rf("原神新版本解析")], skip_age=True, apply_blocks=False)
check("屏蔽开: 厌恶词命中剔除", len(kept_block) == 0)
check("屏蔽关: 厌恶词不生效", len(kept_free) == 1)

app.sset("m_blocklist_sync", "0")
with app._db_lock:
    app.db().execute("INSERT OR IGNORE INTO blacklist(kind,value) VALUES('author','黑作者')")
    app.db().commit()
kept_block2 = app.rules_filter([rf("某测试视频", author="黑作者")], skip_age=True, apply_blocks=True)
kept_free2 = app.rules_filter([rf("某测试视频", author="黑作者")], skip_age=True, apply_blocks=False)
check("屏蔽开: 黑名单作者剔除", len(kept_block2) == 0)
check("屏蔽关: 黑名单作者不生效", len(kept_free2) == 1)

# search_all 的 * 前缀解析(捕获波次函数入参, 不真联网)
captured = {}
orig_first, orig_expand = app._search_first_wave, app._search_expand_wave
app._search_first_wave = lambda q, apply_blocks=True: (captured.update(q=q, blocks=apply_blocks), [])[1]
app._search_expand_wave = lambda q, used, apply_blocks=True: ([], [])
app.search_all("*原神", 0, 10)
check("搜索: *前缀 → 开屏蔽", captured.get("blocks") is True and captured.get("q") == "原神")
app.search_all("原神", 0, 10)
check("搜索: 无* → 关屏蔽", captured.get("blocks") is False and captured.get("q") == "原神")
check("搜索: *与无*不串缓存", len(app._search_wave) == 2)
app._search_first_wave, app._search_expand_wave = orig_first, orig_expand

# ---------- 3. 偏好词时间窗(偏好不再豁免; 宽松档多给6个月) ----------
app.sset("mode", "标准")
app.sset("m_max_age_months", "3")          # 设定: 只看最近3个月
app.sset("ser_max_age_months", "30")
old_item_pref = mkitem(title="四年前的绘画教程", url="https://www.bilibili.com/video/BVprefaged",
                       pubdate=int(NOW - 400 * 86400), pref_kw="绘画")
fresh_pref = mkitem(title="本月绘画过程", url="https://www.bilibili.com/video/BVpreffresh",
                    pubdate=int(NOW - 10 * 86400), pref_kw="绘画")
old_item_plain = mkitem(title="十年前的老歌合集", url="https://www.bilibili.com/video/BVold00001",
                        pubdate=int(NOW - 400 * 86400))
kept = app.machine_filter([old_item_pref, fresh_pref, old_item_plain])
kept_t = {it["title"] for it in kept}
check("时间窗: 偏好超窗内容剔除(标准档)", "四年前的绘画教程" not in kept_t, str(kept_t))
check("时间窗: 偏好窗内内容保留", "本月绘画过程" in kept_t, str(kept_t))
check("时间窗: 非偏好老内容仍剔除", "十年前的老歌合集" not in kept_t, str(kept_t))

app.sset("mode", "宽松")
check("偏好窗: 宽松档 = 设定+6", app.pref_age_months() == 9)
p8 = mkitem(title="八个月前绘画", url="https://www.bilibili.com/video/BVpref8m",
            pubdate=int(NOW - 8 * 30.44 * 86400), pref_kw="绘画")
p10 = mkitem(title="十个月前绘画", url="https://www.bilibili.com/video/BVpref10m",
             pubdate=int(NOW - 10 * 30.44 * 86400), pref_kw="绘画")
kept2 = {it["title"] for it in app.machine_filter([p8, p10])}
check("时间窗: 宽松档8月偏好保留/10月剔除", kept2 == {"八个月前绘画"}, str(kept2))
app.sset("mode", "严格")
check("偏好窗: 严格档 = 设定值", app.pref_age_months() == 3)
kept3 = {it["title"] for it in app.machine_filter([p8])}
check("时间窗: 严格档偏好不放宽(8月剔除)", kept3 == set(), str(kept3))
app.sset("mode", "标准")

music_pref = mkitem(title="后朋克音乐推荐", area="音乐", tname="音乐综合", pref_kw="后朋克音乐")
music_plain = mkitem(title="流行音乐盘点", area="音乐", tname="音乐综合",
                     url="https://www.bilibili.com/video/BVmus00001")
app.sset("mode", "严肃")
order, _stat = app.planned_feed([music_pref, music_plain], False)
got = {it["title"] for it in order}
check("严肃模式: 偏好豁免保留", "后朋克音乐推荐" in got)
check("严肃模式: 非偏好音乐仍拦截", "流行音乐盘点" not in got)

# AI全扫门禁: 未评分的偏好直搜放行, 未评分普通条目拦下
app.sset("mode", "标准")
app.sset("ai_key", "sk-test")
app.sset("ai_full_scan", "1")
scored_item = mkitem(title="已评分的优质视频", url="https://www.bilibili.com/video/BVscd00001")
with app._db_lock:
    app.db().execute("INSERT OR REPLACE INTO items(key,json,score_json,score_at) VALUES(?,?,?,?)",
                     (scored_item["key"], json.dumps(scored_item, ensure_ascii=False),
                      json.dumps({"flame": 0, "spam": 0, "density": 8, "emo": 2, "facts": 1,
                                  "topic": "测试", "interest": 8, "reason": "ok"}),
                     time.time()))
    app.db().commit()
unscored_pref = mkitem(title="未评分偏好直搜", url="https://www.bilibili.com/video/BVupr00001",
                       pref_kw="后朋克音乐")
unscored_plain = mkitem(title="未评分普通内容", url="https://www.bilibili.com/video/BVupl00001")
order2, _s2 = app.planned_feed([scored_item, unscored_pref, unscored_plain], False)
got2 = {it["title"]: it for it in order2}
check("AI门禁: 已评分放行", "已评分的优质视频" in got2)
check("AI门禁: 未评分偏好放行", "未评分偏好直搜" in got2)
check("AI门禁: 未评分普通拦下", "未评分普通内容" not in got2)
check("AI门禁: 偏好放行带默认理由", got2.get("未评分偏好直搜", {}).get("reason", "").startswith("来自你的偏好备注"))
app.sset("ai_key", "")
app.sset("ai_full_scan", "0")
app.sset("mode", "标准")
app.sset("dislike_note", "")

# ---------- 4. 偏好漏斗标签 ----------
check("偏好漏斗标签齐全", all(k in app._FUNNEL_LABELS
                              for k in ("pref_fetch", "pref_kept", "pref_drop", "pref_age")))

# ---------- 5. 搜索排序(综合/播放/最新/最新热门) ----------
SORT_POOL = [
    {"key": "https://www.bilibili.com/video/BVsrt0000A", "platform": "B站",
     "title": "A 高播放老视频", "views": 1000000, "pubdate": int(NOW - 365 * 86400)},
    {"key": "https://www.bilibili.com/video/BVsrt0000B", "platform": "B站",
     "title": "B 低播放新视频", "views": 50000, "pubdate": int(NOW - 3600)},
    {"key": "https://www.bilibili.com/video/BVsrt0000C", "platform": "B站",
     "title": "C 中播放中龄", "views": 300000, "pubdate": int(NOW - 30 * 86400)},
    {"key": "https://www.bilibili.com/video/BVsrt0000D", "platform": "贴吧",
     "title": "D 无播放老帖", "views": 0, "pubdate": int(NOW - 100 * 86400)},
    {"key": "https://www.bilibili.com/video/BVsrt0000E", "platform": "B站",
     "title": "E 无pubdate条目", "views": 10, "pubdate": 0},
]
orig_first_s, orig_expand_s = app._search_first_wave, app._search_expand_wave
app._search_first_wave = lambda q, apply_blocks=True: [dict(x) for x in SORT_POOL]
app._search_expand_wave = lambda q, used, apply_blocks=True: ([], [])

r = app.search_all("排序测试", 0, 10, sort="default")
check("排序: 综合保序", [it["title"][0] for it in r["items"]] == list("ABCDE"))
r = app.search_all("排序测试", 0, 10, sort="views")
check("排序: 播放量降序", [it["views"] for it in r["items"]] == [1000000, 300000, 50000, 10, 0])
r = app.search_all("排序测试", 0, 10, sort="pubdate")
check("排序: 最新发布在前且缺失沉底", [it["title"][0] for it in r["items"]] == ["B", "C", "D", "A", "E"])
r = app.search_all("排序测试", 0, 10, sort="hot")
hot_order = [it["title"][0] for it in r["items"]]
# 1小时前5万播放 > 30天前30万播放 > 1年前100万播放(重力分兼顾新与热)
check("排序: 最新热门新热兼顾", hot_order[:3] == ["B", "C", "A"],
      "actual=%s" % hot_order)
check("排序: 非法sort回退综合", app.search_all("排序测试", 0, 10, sort="bogus")
      ["items"][0]["title"][0] == "A")

p1 = app.search_all("排序测试", 0, 2, sort="views")
p2 = app.search_all("排序测试", 2, 2, sort="views")
keys2 = [it["key"] for it in p1["items"]] + [it["key"] for it in p2["items"]]
check("排序: 分页无缝无重", len(keys2) == len(set(keys2)) == 4)
check("排序: next_offset推进", p1["next_offset"] == 2 and p2["total"] == 5)

# 切换排序不重抓 + 扩波追加后快照自动重排
calls = {"n": 0}
def counting_first(q, apply_blocks=True):
    calls["n"] += 1
    return [dict(x) for x in SORT_POOL]
app._search_first_wave = counting_first
app.search_all("排序测试2", 0, 10, sort="default")
app.search_all("排序测试2", 0, 10, sort="views")
app.search_all("排序测试2", 0, 10, sort="hot")
check("排序: 三种排序共享一次抓取", calls["n"] == 1, "calls=%d" % calls["n"])
new_item = {"key": "https://www.bilibili.com/video/BVsrt0000F", "platform": "B站",
            "title": "F 扩波新到爆量视频", "views": 99999999,
            "pubdate": int(NOW - 600)}
w2 = app._search_wave.get("排序测试2")
if w2:
    w2["items"].append(new_item)
r = app.search_all("排序测试2", 0, 10, sort="views")
check("排序: 扩波后快照自动重排", r["items"][0]["title"][0] == "F" and r["total"] == 6)
app._search_first_wave, app._search_expand_wave = orig_first_s, orig_expand_s

# ---------- 6. 偏好词引擎供给(上移修复: 提前并行+立即收编; 随机取词/取页) ----------
app.sset("prefs_note", "后朋克音乐")
app._pref_bag = []
app._pref_page_hist = []

terms_quick = app._pref_search_terms("", True)
check("偏好供给: 快速开屏搜1词", len(terms_quick) == 1 and terms_quick[0][0] == "后朋克音乐")
check("偏好供给: 单词备注不重复搜同词", len(app._pref_search_terms("", False)) == 1)

app.sset("prefs_note", "后朋克音乐, 器乐摇滚, 氛围电子")
app._pref_bag = []
app._pref_page_hist = []
terms_full = app._pref_search_terms("", False)
check("偏好供给: 完整构建搜2词",
      len(terms_full) == 2 and all(k in ("后朋克音乐", "器乐摇滚", "氛围电子") for k, _ in terms_full),
      str(terms_full))
check("偏好供给: 页码在翻页深度内",
      all(1 <= p <= app._PREF_PAGE_MAX for _, p in terms_full), str(terms_full))

# 随机性: 洗牌袋两轮必覆盖全部偏好词(旧的固定游标可预期); 多轮页码不再 1→2→3→4 循环
app._pref_bag = []
app._pref_page_hist = []
first = app._pref_search_terms("", False)
second = app._pref_search_terms("", False)
covered = {k for k, _ in first} | {k for k, _ in second}
check("偏好供给: 两轮覆盖全部偏好词",
      covered == {"后朋克音乐", "器乐摇滚", "氛围电子"}, "first=%s second=%s" % (first, second))
pages, words = set(), set()
for _ in range(12):
    for k, p in app._pref_search_terms("", False):
        words.add(k)
        pages.add(p)
check("偏好供给: 多轮词不固定(全部出现过)", words == {"后朋克音乐", "器乐摇滚", "氛围电子"}, str(words))
check("偏好供给: 页码随机(非固定单页)", len(pages) >= 3, str(sorted(pages)))

# fetch_pool 集成(全量 mock 网络函数): 偏好词在 quick/full 构建都必须出内容, 且时间窗下推
calls_log = []


def fake_pref_search(keyword, page=1, limit=30, order=None, duration=None, pub_begin=None):
    calls_log.append((keyword, page, pub_begin))
    if keyword == "后朋克音乐":
        return [mkitem(title="后朋克测试视频%d-%d" % (page, i),
                       url="https://www.bilibili.com/video/BVpref%d%03d" % (page, i),
                       pref_kw=keyword)
                for i in range(3)]
    return []


orig_si = app.search_items
orig_sj = app._safe_json
orig_tb, orig_xhh, orig_fol, orig_rel = (app.fetch_tieba_pool, app.fetch_xhh_pool,
                                         app.fetch_following_videos, app.fetch_related)
app.search_items = fake_pref_search
app._safe_json = lambda url, timeout=10: (None, "offline")
app.fetch_tieba_pool = lambda quick=False: ([], [])
app.fetch_xhh_pool = lambda quick=False: ([], [])
app.fetch_following_videos = lambda: ([], None)
app.fetch_related = lambda seed: []
app.sset("prefs_note", "后朋克音乐")   # 单词备注: 集成断言不受随机取词影响
app._pref_bag = []
app._pref_page_hist = []
try:
    calls_log.clear()
    rq = app.fetch_pool(quick=True)
    pref_q = [it for it in rq["items"] if it.get("pref_kw")]
    check("偏好供给: quick开屏也搜偏好(旧版整段跳过)", len(pref_q) == 3
          and all(it["src"].startswith("偏·") for it in pref_q))
    pref_calls = [c for c in calls_log if c[0] == "后朋克音乐"]
    check("偏好供给: quick页码在翻页深度内",
          bool(pref_calls) and all(1 <= c[1] <= app._PREF_PAGE_MAX for c in pref_calls),
          str(pref_calls))
    check("偏好供给: 偏好时间窗下推到搜索请求",
          bool(pref_calls) and all(c[2] for c in pref_calls), str(pref_calls))

    calls_log.clear()
    rf = app.fetch_pool(quick=False)
    pref_f = [it for it in rf["items"] if it.get("pref_kw")]
    check("偏好供给: full构建偏好出内容且在预算内", len(pref_f) == 3)

    app.sset("m_max_age_months", "0")     # 时间窗设为不限 → 不再下推
    calls_log.clear()
    app.fetch_pool(quick=True)
    check("偏好供给: 时间窗不限时不下推",
          all(c[2] is None for c in calls_log if c[0] == "后朋克音乐"), str(calls_log))
    app.sset("m_max_age_months", "3")
finally:
    app.search_items, app._safe_json = orig_si, orig_sj
    app.fetch_tieba_pool, app.fetch_xhh_pool = orig_tb, orig_xhh
    app.fetch_following_videos, app.fetch_related = orig_fol, orig_rel
    app.sset("prefs_note", "")
    app._pref_bag = []
    app._pref_page_hist = []

# ---------- 7. AI聊天: payload确认执行器 ----------
orig_fr = app.force_rebuild
app.force_rebuild = lambda: None   # 打桩: 测试内不真重建(重建会联网)
try:
    app.sset("prefs_note", "")
    app.sset("dislike_note", "")
    app.handle_chat_payload({"op": "add_pref", "keywords": ["后朋克", "器乐摇滚"]})
    check("payload: add_pref 写入两条", "后朋克" in app.sget("prefs_note")
          and "器乐摇滚" in app.sget("prefs_note"))
    app.handle_chat_payload({"op": "add_pref", "keywords": ["后朋克"]})
    check("payload: add_pref 去重", app.sget("prefs_note").count("后朋克") == 1)
    app.handle_chat_payload({"op": "remove_pref", "keywords": ["器乐摇滚"]})
    check("payload: remove_pref 剔除", "器乐摇滚" not in app.sget("prefs_note")
          and "后朋克" in app.sget("prefs_note"))
    app.handle_chat_payload({"op": "add_dislike", "keywords": ["性别对立"]})
    check("payload: add_dislike 写入", "性别对立" in app.sget("dislike_note"))
    app.handle_chat_payload({"op": "remove_dislike", "keywords": ["性别对立"]})
    check("payload: remove_dislike 剔除", "性别对立" not in app.sget("dislike_note"))
    r = app.handle_chat_payload({"op": "noop"})
    check("payload: noop 不动备注", "没有做任何改动" in r["reply"])
    r = app.handle_chat_payload({"op": "bogus"})
    check("payload: 未知op忽略", "未知操作" in r["reply"])
finally:
    app.force_rebuild = orig_fr
    app.sset("prefs_note", "")
    app.sset("dislike_note", "")

# ---------- 8. 问AI上下文 ----------
ctx_item = mkitem(title="问AI测试条目", desc="这是一条关于后朋克音乐的介绍视频")
with app._db_lock:
    app.db().execute("INSERT OR REPLACE INTO items(key,json,score_json,score_at) VALUES(?,?,?,?)",
                     (ctx_item["key"], json.dumps(ctx_item, ensure_ascii=False),
                      json.dumps({"interest": 8, "flame": 0, "spam": 1, "topic": "后朋克",
                                  "reason": "小众好货"}), time.time()))
    app.db().commit()
app.chat_ctx_set(1, ctx_item["key"], "问AI测试条目")
ctxs = app.chat_item_context(1)
check("问AI: 上下文含标题/简介/评分", "问AI测试条目" in ctxs and "后朋克音乐的介绍" in ctxs
      and "兴趣8" in ctxs and "小众好货" in ctxs)
app.chat_ctx_clear(1)
check("问AI: 清除后为空", app.chat_item_context(1) == "")
app.chat_ctx_set(2, "https://www.bilibili.com/video/BVnotexist", "t")
check("问AI: 条目不存在返回空", app.chat_item_context(2) == "")
app.chat_ctx_clear(2)

app.set_last_feed(mkitem(title="指代测试条目", desc="指代简介内容"))
check("指代: last_feed 带简介注入", "指代简介内容" in app.chat_last_context("刚才那条讲了什么"))
check("指代: 无指代词不注入", app.chat_last_context("今天天气如何") == "")

# ---------- 9. 每日简报 ----------
rep_item = mkitem(title="简报测试视频A", desc="简报测试")
rep_item["topic"] = "后朋克"
with app._db_lock:
    app.db().execute("INSERT OR REPLACE INTO items(key,json,score_json,score_at) VALUES(?,?,?,?)",
                     (rep_item["key"], json.dumps(rep_item, ensure_ascii=False), "", time.time()))
    app.db().execute("INSERT OR REPLACE INTO shown(key,ts) VALUES(?,?)",
                     (rep_item["key"], int(time.time())))
    app.db().execute("INSERT INTO feedback(key,author,action,ts) VALUES(?,?,?,?)",
                     (rep_item["key"], "测试作者", "like", int(time.time())))
    app.db().commit()
data, n_seen, n_like, n_dis = app.daily_report_data()
check("简报: 本地数据聚合", n_seen == 1 and n_like == 1 and n_dis == 0
      and "简报测试视频A" in data, "seen=%s like=%s" % (n_seen, n_like))

orig_llm = app.call_llm
try:
    app.sset("ai_key", "sk-test")
    app.call_llm = lambda msgs, max_tokens=1200: (
        "今天内容不错，偏好命中正常。\n建议新增偏好：器乐摇滚、氛围电子\n建议新增厌恶：无", {})
    reply, chips, _t = app.daily_report_reply()
    check("简报: AI建议解析成chips", bool(chips) and chips[0]["payload"]["op"] == "add_pref"
          and "器乐摇滚" in chips[0]["payload"]["keywords"], "chips=%s" % chips)
    check("简报: 建议'无'不生成厌恶chip", all(c["payload"]["op"] != "add_dislike" for c in (chips or [])))
    app.sset("ai_key", "")
    reply, chips, _t = app.daily_report_reply()
    check("简报: 无AI模板兜底", chips is None and "配置 AI" in reply)
finally:
    app.call_llm = orig_llm
    app.sset("ai_key", "")

print("\n==== %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
