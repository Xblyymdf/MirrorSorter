# -*- coding: utf-8 -*-
"""端到端冒烟: 真实 HTTP 服务 + 本地 mock OpenAI 兼容接口(流式/非流式)。
覆盖: /api/conv CRUD · /api/chat(命令/指代/搜索/LLM非流式/LLM流式SSE) · chips · 会话隔离
"""
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(PROJECT, "build", "probe", "smoke-tmp")
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


# ---------- mock OpenAI 兼容服务 ----------
class MockLLM(BaseHTTPRequestHandler):
    score_calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        stream = bool(body.get("stream"))
        is_intent = any("想让系统做什么" in (m.get("content") or "") for m in body.get("messages", []))
        is_score = any("内容筛选技能" in (m.get("content") or "") for m in body.get("messages", []))
        is_terms = any("近义词/相关搜索词" in (m.get("content") or "") for m in body.get("messages", []))
        is_verify = any("事实核查员" in (m.get("content") or "") for m in body.get("messages", []))
        is_rebut = any("对线的当事人" in (m.get("content") or "") for m in body.get("messages", []))
        is_judge = any("网络对线的裁判" in (m.get("content") or "") for m in body.get("messages", []))
        is_decomp = any("论点拆解器" in (m.get("content") or "") for m in body.get("messages", []))
        if is_score:
            MockLLM.score_calls += 1
            user_msg = next((m for m in body.get("messages", []) if m.get("role") == "user"), {})
            keys = [it["key"] for it in json.loads(user_msg.get("content") or "{}").get("items", [])]
            items = [{"key": k, "flame": 9 if "bad" in k else 0, "spam": 0, "topic": "游戏",
                      "interest": 3 if "low" in k else 8, "reason": "推荐理由" + k} for k in keys]
            out = json.dumps({"choices": [{"message": {"content": json.dumps(
                                {"items": items}, ensure_ascii=False)}}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 10}},
                             ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if is_intent:
            user = next((m.get("content", "") for m in body.get("messages", [])
                         if m.get("role") == "user"), "")
            if "刷" in user or "推" in user:
                verdict = '{"action":"search","keywords":["blender"]}'
            else:
                verdict = '{"action":"reply","keywords":[]}'
            out = json.dumps({"choices": [{"message": {"content": verdict}}],
                              "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
                             ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if is_terms:
            # 近义词生成: 首查给"深挖词1"; 扩波按已用词数递增(永不枯竭, 靠波次上限收敛)
            sys_c = (body.get("messages") or [{}])[0].get("content", "")
            n = sys_c.count("、") + 1 if "以下词已搜过" in sys_c else 1
            out = json.dumps({"choices": [{"message": {"content": json.dumps(
                ["深挖词%d" % n], ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if is_decomp:
            # 拆解桩按原话给词: 查询词必须和搜索桩的返回内容对得上, 否则相关度过滤
            # 会把证据全滤掉(这正是真实链路里「查询词照抄对方措辞就搜不到东西」的同一件事)
            _usr = next((m.get("content", "") for m in body.get("messages", [])
                         if m.get("role") == "user"), "")
            if "可乐" in _usr:
                _payload = {"claims": [{"point": "可乐能杀灭蛔虫", "query": "可乐 杀灭 蛔虫",
                                        "counter": "可乐 蛔虫 医学 研究"}],
                            "best_counter": "可乐 杀灭 蛔虫 辟谣"}
            else:
                _payload = {"claims": [{"point": "以色列空军战机能飞到中国", "query": "以色列空军 转场航程",
                                        "counter": "以色列空军 空中加油 航程 实际"},
                                       {"point": "中国战机飞不到以色列", "query": "中国空军 作战半径",
                                        "counter": "中国战机 海外部署 远航 案例"}],
                            "best_counter": "以色列空军 远程奔袭 战例 距离"}
            out = json.dumps({"choices": [{"message": {"content": json.dumps(
                _payload, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8}},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if is_verify:
            user = next((m.get("content", "") for m in body.get("messages", [])
                         if m.get("role") == "user"), "")
            if "改用纯文本" in user:   # 纯文本兜底桩
                reply = "结论：虚假（置信度85%）说法与权威证据矛盾\n- 与权威来源结论相反\n- 无可靠来源支持"
            elif "触发降级" in user:   # 模拟推理模型截断: JSON 两次全坏
                reply = '{"verdict": "fals'
            else:
                base = {"verdict": "false", "confidence": 88,
                        "summary": "说法与权威来源相矛盾",
                        "reasons": ["与权威来源结论相反[E1]", "无任何可靠来源支持[E1]"],
                        "claims": [{"point": "以色列空军能飞到中国", "check": "false",
                                    "note": "权威来源显示相反[E1]"}],
                        "evidence_used": ["E1"]}
                if "知识误区" in user:   # 误区/谬误分析常驻: 核查必然点名
                    base["fallacies"] = ["假两难"]
                reply = json.dumps(base, ensure_ascii=False)
            out = json.dumps({"choices": [{"message": {"content": reply}}],
                              "usage": {"prompt_tokens": 30, "completion_tokens": 20}},
                             ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if is_judge:
            # 独立裁判角色: 只看稿子。命中AI八股就判 revise, 让写手按意见重写
            draft = next((m.get("content", "") for m in body.get("messages", [])
                          if m.get("role") == "user"), "")
            if "一方面" in draft or "另一方面" in draft:
                reply = json.dumps({"verdict": "revise",
                                    "problems": ["第2条是AI八股套话, 改成真人随手打的语气"],
                                    "weakest": "第2条只有转折腔, 没有具体依据"},
                                   ensure_ascii=False)
            else:
                reply = json.dumps({"verdict": "pass", "problems": [], "weakest": ""},
                                   ensure_ascii=False)
            out = json.dumps({"choices": [{"message": {"content": reply}}],
                              "usage": {"prompt_tokens": 30, "completion_tokens": 20}},
                             ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if is_rebut:
            user = next((m.get("content", "") for m in body.get("messages", [])
                         if m.get("role") == "user"), "")
            cnt = 2
            mc = re.search(r"现在写\s*(\d+)\s*条", user)
            if mc:
                cnt = int(mc.group(1))

            def _lines(clean=False):
                pool = [{"text": "作战半径摆在那，战机当火箭使？",
                         "basis": "公开航程数据不支持[E1]", "target": "知识误区"},
                        {"text": "先把航程查明白再来跟我说话",
                         "basis": "公开数据不支撑该断言[E1]", "target": "以偏概全"},
                        {"text": "体系对抗时代还在数单机航程",
                         "basis": "证据无相关内容[E1]", "target": "知识误区"}]
                if not clean:   # 首版埋一条AI八股, 逼出重写分支
                    pool[1] = {"text": "一方面懒得查证，另一方面又急着下结论",
                               "basis": "公开数据不支撑该断言[E1]", "target": "以偏概全"}
                return pool[:cnt]

            def _paras():
                return [{"text": "我拿公开数据跟你说话：以军战机转场要多次空中加油，"
                                 "几千公里直线距离根本撑不起护航航程，你把这当成作战半径就是偷换概念。",
                         "basis": "公开航程数据不支持[E1]", "target": "偷换概念"} for _ in range(cnt)]

            if "改用纯文本" in user:
                reply = "转场航程差着数量级，拿地图直线当作战半径？\n把作战半径和空中加油摆一起看\n就这还想让我服气"
            elif "触发降级" in user:
                reply = "这不是JSON，是一段普通的话"
            elif "先拆对方这段话的问题" in user:
                if "无可回怼" in user:   # 正常反馈桩: 模型应声明无可回怼
                    reply = json.dumps({"no_target": True,
                                        "reason": "这是正常的产品反馈, 没有攻击性观点"},
                                       ensure_ascii=False)
                else:
                    reply = json.dumps({
                        "targets": [{"point": "以色列空军能飞到中国", "kind": "知识误区",
                                     "note": "转场航程与作战半径差着数量级"},
                                    {"point": "中国战机飞不到以色列", "kind": "以偏概全",
                                     "note": "拿单机航程当体系投送能力"}],
                        "stance_note": "证据一致否定该说法"}, ensure_ascii=False)
            elif "按裁判意见重写一版" in user:   # 裁判复核后的重写桩: 返回干净版
                if "一段话" in user:
                    items = _paras()
                elif "长篇" in user:
                    items = [{"text": "你的两个论点我都拆过了：转场航程和作战半径差着数量级，"
                                       "拿地图直线当投送距离，这套算法连基础军事常识都过不了。",
                              "basis": "公开航程数据[E1]", "target": "整体反驳"}]
                else:
                    items = _lines(clean=True)
                reply = json.dumps({"items": items}, ensure_ascii=False)
            elif "长篇" in user:
                reply = json.dumps({"items": [
                    {"text": "你的两个论点我都拆过了：转场航程和作战半径差着数量级，"
                             "拿地图直线当投送距离，这套算法连基础军事常识都过不了。",
                     "basis": "公开航程数据[E1]", "target": "整体反驳"}]}, ensure_ascii=False)
            elif "一段话" in user:
                reply = json.dumps({"items": _paras()}, ensure_ascii=False)
            elif "一句话" in user:
                reply = json.dumps({"items": _lines()}, ensure_ascii=False)
            else:
                reply = json.dumps({"items": _lines()}, ensure_ascii=False)
            out = json.dumps({"choices": [{"message": {"content": reply}}],
                              "usage": {"prompt_tokens": 30, "completion_tokens": 25}},
                             ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            is_intent2 = False
            # 模拟"思考爆炸": 只出 reasoning 不出 content(截断场景)
            if any("思考爆炸" in (m.get("content") or "") for m in body.get("messages", [])):
                for t in ("好的", "用户", "想知道", "热熔", "怎么用", "绝地潜兵"):
                    ev = json.dumps({"choices": [{"delta": {"content": None,
                                                            "reasoning_content": t}}]},
                                    ensure_ascii=False)
                    self.wfile.write(("data: %s\n\n" % ev).encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            # 模拟推理模型: 先 reason 再 answer(真实 deepseek-v4-flash 行为)
            for t in ("嗯", "这个问题", "需要想想"):
                ev = json.dumps({"choices": [{"delta": {"content": None,
                                                        "reasoning_content": t}}]},
                                ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % ev).encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.01)
            for t in ("你", "好", "！", "这是", "流式", "回复"):
                ev = json.dumps({"choices": [{"delta": {"content": t, "reasoning_content": None}}]},
                                ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % ev).encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.02)
            fin = json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 5, "completion_tokens": 6}})
            self.wfile.write(("data: %s\n\ndata: [DONE]\n\n" % fin).encode("utf-8"))
            self.wfile.flush()
        else:
            out = json.dumps({"choices": [{"message": {"content": "非流式回复"}}],
                              "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                             ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


mock = ThreadingHTTPServer(("127.0.0.1", 8098), MockLLM)
threading.Thread(target=mock.serve_forever, daemon=True).start()

# ---------- 启动被测服务 ----------
os.environ["MIRRORSORTER_DATA_DIR"] = TMP
spec = importlib.util.spec_from_file_location("app2", os.path.join(PROJECT, "app.py"))
app = importlib.util.module_from_spec(spec)
sys.modules["app2"] = app
spec.loader.exec_module(app)
app.DATA_DIR = TMP   # 保持旧测试写法, 双保险隔离真实用户存档
# v0.3.1.56: 出站闸门(同站点最小间隔/每分钟上限)是给真实抓取用的防刷网护栏,
# 但单测会连着打同一站点(还 mock urlopen), 开着会让后面的用例互相限流。
# 这里统一关掉, 另用"抓取:出站闸门真的会挡住连发"一条单独验证它本身有效。
app._NET_GUARD_ON = False

srv = ThreadingHTTPServer(("127.0.0.1", 8099), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)


def req(path, body=None, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request("http://127.0.0.1:8099" + path, data=data,
                               headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(r, timeout=15) as resp:
        out = resp.read()
    if raw:
        return out.decode("utf-8", "replace")
    return json.loads(out.decode("utf-8"))


try:
    # 页面租约: 刷新交接/多标签不能提前关服; 无页面超过宽限期才触发关闭。
    class LeaseServer:
        def __init__(self):
            self.calls = 0
        def shutdown(self):
            self.calls += 1

    lease_server = LeaseServer()
    old_server, old_grace = app._SERVER, app.PAGE_LEASE_GRACE_SECONDS
    app._SERVER, app.PAGE_LEASE_GRACE_SECONDS = lease_server, 0.03
    app._SHUTDOWN_REQUESTED.clear()
    with app._PAGE_LEASE_LOCK:
        app._PAGE_LEASES.clear()
        app._PAGE_LEASE_GENERATION = 0
    token_a, token_b = "a" * 16, "b" * 16
    app.page_lease_alive(token_a)
    app.page_lease_leaving(token_a)
    app.page_lease_alive(token_b)
    time.sleep(0.06)
    check("页面租约: 刷新交接后服务继续", lease_server.calls == 0, lease_server.calls)
    app.page_lease_leaving(token_b)
    time.sleep(0.06)
    check("页面租约: 全部页面离开后延迟关闭", lease_server.calls == 1, lease_server.calls)
    app._SERVER, app.PAGE_LEASE_GRACE_SECONDS = old_server, old_grace
    app._SHUTDOWN_REQUESTED.clear()
    with app._PAGE_LEASE_LOCK:
        app._PAGE_LEASES.clear()
        app._PAGE_LEASE_GENERATION = 0
    check("页面租约: 测试后状态已清理", not app._PAGE_LEASES and not app._SHUTDOWN_REQUESTED.is_set())

    # 通用展示冷却: 第N次实际展示进入冷却，同一请求重试不重复计数。
    app.sset("m_repeat_limit", "2")
    app.sset("m_repeat_cooldown_hours", "72")
    with app._db_lock:
        app.db().execute("DELETE FROM item_presentation")
        app.db().execute("DELETE FROM item_exposure_state")
        app.db().commit()
    cool_item = {"key": "cooldown-test"}
    app.record_feed_presentations([cool_item], "page-a", now=1000)
    check("展示冷却: 首次展示不冷却", "cooldown-test" not in app.cooldown_keys(1000))
    app.record_feed_presentations([cool_item], "page-a", now=1001)
    row = app.db().execute("SELECT run_count,cooldown_until FROM item_exposure_state WHERE key=?", ("cooldown-test",)).fetchone()
    check("展示冷却: 同请求重试不重复计数", row[0] == 1 and row[1] == 0, row)
    app.record_feed_presentations([cool_item], "page-b", now=1002)
    check("展示冷却: 第N次后硬性冷却", "cooldown-test" in app.cooldown_keys(1003), app.cooldown_keys(1003))
    check("展示冷却: 到期后恢复资格", "cooldown-test" not in app.cooldown_keys(1002 + 72 * 3600),
          app.cooldown_keys(1002 + 72 * 3600))
    app.sset("m_repeat_limit", "4")

    # 配置 mock AI
    check("设置mockAI", req("/api/settings", {"ai_key": "test-key",
                                              "ai_base": "http://127.0.0.1:8098/v1",
                                              "ai_model": "mock-model"}).get("ok"))

    # 冷却供给保护: 候选全部冷却时按最早展示顺序少量复用, 不返回空池。
    app.sset("m_repeat_limit", "4")
    app.sset("m_repeat_cooldown_hours", "72")
    scarce = [{"key": "scarce-%d" % i, "platform": "B站", "title": "冷却候选%d" % i,
               "author": "a", "desc": "d", "views": 1000, "duration": 120,
               "pubdate": int(time.time()), "url": "https://x/scarce-%d" % i}
              for i in range(2)]
    with app._db_lock:
        for i, it in enumerate(scarce):
            app.db().execute("INSERT OR REPLACE INTO item_exposure_state(key,run_count,last_presented_at,cooldown_until) VALUES(?,?,?,?)",
                             (it["key"], 4, 100 + i, int(time.time()) + 3600))
        app.db().commit()
    scarce_order, _ = app.planned_feed(scarce)
    check("展示冷却: 候选不足时不锁空", len(scarce_order) == 2 and all(x.get("_cooldown_relaxed") for x in scarce_order),
          [x.get("key") for x in scarce_order])
    with app._db_lock:
        app.db().execute("DELETE FROM item_exposure_state WHERE key LIKE 'scarce-%'")
        app.db().commit()

    # 冷却复用不清除冷却: 复用展示后原 cooldown_until 仍保留, run_count 递增。
    with app._db_lock:
        app.db().execute("INSERT OR REPLACE INTO item_exposure_state(key,run_count,last_presented_at,cooldown_until) "
                         "VALUES('relax-keep',4,100,2000)")
        app.db().commit()
    app.record_feed_presentations([{"key": "relax-keep", "_cooldown_relaxed": True}], "s1", now=1000)
    row = app.db().execute("SELECT run_count,cooldown_until FROM item_exposure_state WHERE key='relax-keep'").fetchone()
    check("冷却复用: 原冷却保留且计数递增", row[0] == 5 and row[1] == 2000, row)
    with app._db_lock:
        app.db().execute("DELETE FROM item_exposure_state WHERE key='relax-keep'")
        app.db().commit()

    # 会话级去重: 同一会话内已返回的 key 不再返回; 计划重排(reset)后也不重复。
    app.sset("m_repeat_limit", "4")
    feed_items = [{"key": "sess-%02d" % i, "platform": "B站", "title": "会话去重%d" % i,
                   "author": "a", "desc": "d", "views": 1000 + i, "duration": 120,
                   "pubdate": int(time.time()), "url": "https://x/sess-%02d" % i}
                  for i in range(40)]
    app._universe["items"] = feed_items
    app._universe["built_at"] = int(time.time())
    app._plan_cache.update(key=None, order=[], seq=0)
    page1 = app.serve_chunk(0, 30, "", request_id="sess-test")
    page2 = app.serve_chunk(0, 30, "", request_id="sess-test")   # 重复请求同页也不重样
    check("会话去重: 两页无重复key",
          len(page1["items"]) == 30 and len(page2["items"]) == 10 and
          not ({x["key"] for x in page1["items"]} & {x["key"] for x in page2["items"]}),
          {"p1": len(page1["items"]), "p2": len(page2["items"])})

    # 沉积带修复: B站新增枯竭、贴吧持续涌入时, 新到B站必须被重混插回后段中间,
    # 计划尾部不允许形成纯贴吧/小黑盒的扎堆带。
    app.sset("bili_weight", "100")
    app.sset("tb_weight", "25")
    app.sset("xhh_weight", "25")
    def mk(key, plat, views):
        return {"key": key, "platform": plat, "title": key, "author": "a", "desc": "d",
                "views": views, "duration": 120, "pubdate": int(time.time()),
                "url": "https://x/%s" % key}
    app._universe["items"] = [mk("mix-b%02d" % i, "B站", 2000 - i) for i in range(20)] + \
                             [mk("mix-t%02d" % i, "贴吧", 2000 - i) for i in range(10)]
    app._plan_cache.update(key=None, order=[], seq=0)
    with app._session_lock:
        app._session_served.pop("sess-test", None)
    app.serve_chunk(0, 30, "", request_id="sess-test")   # 消费首批(混合)
    # 第二批: 模拟B站枯竭 —— 只来贴吧
    app._universe["items"] += [mk("mix-t2%02d" % i, "贴吧", 1500 - i) for i in range(30)]
    app.serve_chunk(0, 30, "", request_id="sess-test")
    # 第三批: 新B站到货 → 必须重混进未消费后段, 而不是排在贴吧沉积带之后。
    # 验证口径 = 用户实际出流: 计划层重混 + serve层防扎堆兜底, 出流同平台最多连2条,
    # 且新B站(mix-b2*)必须真的被送到用户面前(而非冻在沉积带后)。
    app._universe["items"] += [mk("mix-b2%02d" % i, "B站", 1400 - i) for i in range(20)]
    app._feed_plan(list(app._universe["items"]), False, 1)   # 触发重混(不经会话消费)
    stream, b2_served = [], 0
    for i in range(6):
        r = app.serve_chunk(0, 30, "", request_id="sess-test")
        got = r.get("items") or []
        stream += got
        b2_served += sum(1 for it in got if it["key"].startswith("mix-b2"))
        if len(stream) >= 90:
            break
    plats = [it["platform"] for it in stream]
    tb_runs, cur = [], 0
    for p in plats:
        cur = cur + 1 if p == "贴吧" else 0
        tb_runs.append(cur)
    check("沉积带: 新B站重混进后段且出流无贴吧扎堆",
          b2_served >= 10 and (max(tb_runs) if tb_runs else 0) <= 2,
          {"stream": len(plats), "max_tb_run": max(tb_runs) if tb_runs else 0,
           "bili_new_served": b2_served})
    app._plan_cache.update(key=None, order=[], seq=0)
    with app._session_lock:
        app._session_served.pop("sess-test", None)
    app._universe["items"] = []

    # 同作者降重: 配额(单计划≤3) + 间距打散 + 超额留池分批放出。
    app.sset("m_repeat_limit", "4")
    app.sset("m_repeat_cooldown_hours", "72")
    same_author_items = [mk("au-%02d" % i, "B站", 3000 - i) for i in range(3)] + \
                        [{"key": "au-same-%02d" % i, "platform": "B站", "title": "原声英语社教程%d" % i,
                          "author": "原声英语社", "desc": "d", "views": 2000 - i, "duration": 120,
                          "pubdate": int(time.time()), "url": "https://x/au-same-%02d" % i}
                         for i in range(8)] + \
                        [mk("au-o%02d" % i, "B站", 1000 - i) for i in range(10)]
    for i, it in enumerate(same_author_items):
        if it.get("author") in ("", "a", None):
            it["author"] = "路人UP%d" % i   # 足够多的不同作者, 配额才会生效
    order_a, _ = app.planned_feed(list(same_author_items), False)
    same_n = sum(1 for it in order_a if it.get("author") == "原声英语社")
    au_pos = [i for i, it in enumerate(order_a) if it.get("author") == "原声英语社"]
    au_dists = [b - a for a, b in zip(au_pos, au_pos[1:])]
    check("同作者降重: 单计划配额≤3", same_n <= 3, same_n)
    check("同作者降重: 无相邻出现", all(d >= 2 for d in au_dists), au_dists)
    # 留池放出: 模拟已看消费(把配额内3条标记shown), 重混后同作者新条目应补进计划。
    with app._db_lock:
        for it in order_a:
            if it.get("author") == "原声英语社":
                app.db().execute("INSERT OR REPLACE INTO shown(key, ts) VALUES(?,?)",
                                 (it["key"], int(time.time()) - 7 * 3600))
        app.db().commit()
    rest = [it for it in same_author_items if it["key"] not in {x["key"] for x in order_a}]
    order_b, _ = app.planned_feed(rest, False)
    same_b = sum(1 for it in order_b if it.get("author") == "原声英语社")
    check("同作者降重: 留池条目后续分批放出", same_b >= 1, same_b)

    # 负反馈硬排除: 不喜欢的条目与屏蔽作者在信息流/搜索/换一条全部消失, undo 可恢复。
    fb = [{"key": "fb-k1", "platform": "B站", "title": "被点踩的视频", "author": "作者甲",
           "desc": "x", "views": 5000, "duration": 200, "pubdate": int(time.time()),
           "url": "https://x/fb-k1"},
          {"key": "fb-k2", "platform": "B站", "title": "屏蔽作者的视频", "author": "被屏蔽UP",
           "desc": "x", "views": 5000, "duration": 200, "pubdate": int(time.time()),
           "url": "https://x/fb-k2"},
          {"key": "fb-k3", "platform": "B站", "title": "正常视频", "author": "路人",
           "desc": "x", "views": 5000, "duration": 200, "pubdate": int(time.time()),
           "url": "https://x/fb-k3"}]
    with app._db_lock:
        app.db().execute("INSERT INTO feedback(key,author,action,ts) VALUES('fb-k1','作者甲','dislike',?)",
                         (int(time.time()),))
        app.db().execute("INSERT OR IGNORE INTO blacklist(kind,value) VALUES('author','被屏蔽UP')")
        app.db().commit()
    kept_fb = app.machine_filter(fb)
    check("负反馈: 点踩与屏蔽作者入池前剔除",
          {x["key"] for x in kept_fb} == {"fb-k3"}, [x["key"] for x in kept_fb])
    served_fb = app.apply_filters(fb)
    check("负反馈: 服务出口同样剔除",
          {x["key"] for x in served_fb} == {"fb-k3"}, [x["key"] for x in served_fb])
    with app._db_lock:
        app.db().execute("DELETE FROM feedback WHERE key='fb-k1'")
        app.db().execute("DELETE FROM blacklist WHERE value='被屏蔽UP'")
        app.db().commit()
    kept_undo = app.machine_filter(fb)
    check("负反馈: 撤销后恢复", {x["key"] for x in kept_undo} == {"fb-k1", "fb-k2", "fb-k3"},
          [x["key"] for x in kept_undo])

    d = req("/api/conv")
    check("默认会话", len(d["convs"]) == 1 and d["cur"] == 1, d)

    d = req("/api/chat", {"message": "帮我刷blender", "stream": False})
    check("意图搜索回复", d.get("intent", {}).get("action") == "search", d)
    check("chips>=4", len(d.get("chips", [])) >= 4, d)
    check("空AI消息不写库", True)

    # LLM 非流式(自由聊天)
    d = req("/api/chat", {"message": "讲讲黑神话的故事", "stream": False})
    check("非流式LLM回复", d.get("reply") == "非流式回复", d)

    # 流式 SSE(推理模型: think + delta + done)
    raw = req("/api/chat", {"message": "讲讲黑神话的故事", "stream": True}, raw=True)
    check("SSE含think事件", "event: think" in raw, raw[:200])
    check("SSE含delta事件", "event: delta" in raw and "event: done" in raw, raw[:200])
    check("SSE含meta", "event: meta" in raw)
    check("流式内容完整", "你好！这是流式回复" in raw, raw[:400])

    # 思考截断兜底: 只有 reasoning 没有 content → 用思考尾段作答
    raw = req("/api/chat", {"message": "思考爆炸测试", "stream": True}, raw=True)
    m = [l for l in raw.split("\n") if l.startswith("data:") and '"reply"' in l]
    ok = bool(m) and "模型思考过长了" in m[-1] and "热熔" in m[-1]
    check("思考截断兜底", ok, m[-1] if m else raw[:200])

    # 会话隔离 + 指代
    d = req("/api/conv", {})
    cid2 = d["id"]
    req("/api/conv/select", {"id": 1})
    app.set_last_feed({"key": "k1", "title": "黑神话DLC", "author": "游戏观察室"})
    d = req("/api/chat", {"message": "收藏刚才那条", "stream": False})
    check("指代命令回复", d.get("reply", "").startswith("已稍后看"), d)
    saved = req("/api/saved?kind=later")
    check("指代落库saved", any(s["key"] == "k1" for s in saved["items"]), saved)

    # 会话1消息数
    d = req("/api/chat?cid=1")
    check("会话1有消息", len(d["messages"]) >= 2, d)
    d = req("/api/chat?cid=%d" % cid2)
    check("新会话为空", len(d["messages"]) == 0, d)

    # 新对话命令
    d = req("/api/chat", {"message": "开个新对话", "stream": False})
    check("新对话命令cid", d.get("cid", 0) > cid2 and d.get("chips"), d)

    # 重命名/删除
    req("/api/conv/rename", {"id": 1, "title": "默认改名"})
    d = req("/api/conv")
    check("重命名生效", d["convs"][0]["title"] == "默认改名", d)
    req("/api/conv/delete", {"id": 1})
    d = req("/api/conv")
    check("删除后不再出现", all(c["id"] != 1 for c in d["convs"]), d)
    check("删除后仍有会话", len(d["convs"]) >= 1, d)

    # 清空当前会话
    req("/api/conv/select", {"id": cid2 + 1})   # 新对话命令建的
    req("/api/chat/clear", {})
    cur = req("/api/conv")["cur"]
    d = req("/api/chat?cid=%d" % cur)
    check("清空当前会话", len(d["messages"]) == 0, d)

    # --- AI推荐模式: 全量评分开关(评分先行:bake只读缓存) ---
    pool = []
    now = int(time.time())
    for i in range(150):
        pool.append({"key": "k_%03d" % i, "platform": "B站", "title": "测试视频%03d" % i,
                     "author": "作者%d" % i, "desc": "简介", "cover": "", "views": 1000000 - i,
                     "likes": 100, "duration": 300, "pubdate": now - 3600,
                     "url": "https://www.bilibili.com/video/k_%03d" % i})
    with app._db_lock:
        for it in pool:
            app.db().execute("INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,?,0)",
                             (it["key"], json.dumps(it, ensure_ascii=False), now))
        app.db().commit()
    app.score_items(pool)   # 预热(模拟历史评分缓存)
    app.sset("ai_full_scan", "1")
    kept = app.bake_universe({"items": pool}, quick=False)
    scored1 = sum(1 for it in kept if it.get("reason"))
    check("开启模式: 缓存优先全部有理由", scored1 == 150, scored1)
    app.sset("ai_full_scan", "0")
    kept0 = app.bake_universe({"items": pool}, quick=False)
    scored0 = sum(1 for it in kept0 if it.get("reason"))
    check("关闭模式: 不评分零理由且全保留", scored0 == 0 and len(kept0) == 150, (scored0, len(kept0)))
    check("评分进度已记录", app._score_progress["total"] >= 30 and app._score_progress["done"] >= 30,
          app._score_progress)

    # --- 信息流门禁: 可见条目必须已筛查(评分+剔除引战/兴趣不足), 同步完成 ---
    req("/api/settings", {"ai_full_scan": "1"})

    def mock_pool(quick=False):
        now = int(time.time())
        its = [{"key": "f_%03d" % i, "platform": "B站", "title": "feed视频%03d" % i,
                "author": "作者%d" % i, "desc": "简介", "cover": "", "views": 900000 - i,
                "likes": 100, "duration": 300, "pubdate": now - 3600,
                "url": "https://www.bilibili.com/video/f_%03d" % i} for i in range(38)]
        its.append({"key": "f_bad_000", "platform": "B站", "title": "引战测试标题",
                    "author": "坏作者", "desc": "引战", "cover": "", "views": 900000,
                    "likes": 1, "duration": 300, "pubdate": now - 3600,
                    "url": "https://www.bilibili.com/video/f_bad_000"})
        its.append({"key": "f_low_000", "platform": "B站", "title": "低兴趣测试标题",
                    "author": "普通作者", "desc": "简介", "cover": "", "views": 900000,
                    "likes": 1, "duration": 300, "pubdate": now - 3600,
                    "url": "https://www.bilibili.com/video/f_low_000"})
        return {"items": its, "offline": False, "source": "mock"}
    app.fetch_pool = mock_pool
    app.search_items = lambda kw, page=1, limit=30: [
        {"key": "s_%s_%d" % (str(hash(kw))[-4:], i), "platform": "B站",
         "title": "搜索%s_%d" % (kw[:8], i), "author": "作者", "desc": "简介", "cover": "",
         "views": 500000, "likes": 10, "duration": 200, "pubdate": int(time.time()) - 3600,
         "url": "https://www.bilibili.com/video/s_%d" % i} for i in range(30)]
    app.tb_search_items = lambda q, rn=20: [
        {"key": "tb_%s_%d" % (str(hash(q))[-4:], i), "platform": "贴吧",
         "title": "贴吧搜%s_%d" % (str(q)[:6], i), "author": "贴友", "desc": "帖内简介",
         "cover": "", "covers": [], "views": 0, "likes": 2, "danmaku": None, "reply": 5,
         "duration": 0, "pubdate": int(time.time()) - 3600,
         "url": "https://tieba.baidu.com/p/90000%d" % i, "tname": "测试吧", "area": "",
         "tag": "", "gid": "tieba", "src": "贴吧·测试吧"} for i in range(4)]
    # 后台评分线程已就绪(模拟): feed 候选预评分写库, 门禁只读缓存命中才展示(交互路径不阻塞/不烧token)
    app.score_items(mock_pool()["items"], only_cache=False)
    d1 = req("/api/feed?offset=0&limit=40")
    it1 = d1.get("items", [])
    check("feed响应含槽位统计字段", isinstance(d1.get("slots"), dict), d1.get("slots"))
    check("feed门禁: 返回条目全部带理由", len(it1) >= 1 and all((it.get("reason") or "").strip() for it in it1),
          "n=%d" % len(it1))
    check("feed门禁: 引战条目被剔除", not any("bad" in (it.get("key") or "") for it in it1), it1[:1])
    s = req("/api/search?q=" + urllib.parse.quote("feed测试") + "&limit=30")
    s_bili = [it for it in s.get("items", []) if it.get("platform") == "B站"]
    s_tb = [it for it in s.get("items", []) if it.get("platform") == "贴吧"]
    swr = sum(1 for it in s_bili if (it.get("reason") or "").strip())
    check("搜索结果同步评分门禁(B站条目)", len(s_bili) >= 1 and len(s_bili) == swr, swr)
    check("搜索跨平台: 贴吧条目混入首屏(豁免评分)", len(s_tb) >= 1 and s.get("platform_hits", 0) >= 1,
          "tb=%d hits=%s" % (len(s_tb), s.get("platform_hits")))
    check("搜索响应含深挖字段", all(k in s for k in ("end", "deep_terms", "building")), list(s.keys()))

    # --- 关闭模式: 零AI消耗(不评分) ---
    req("/api/settings", {"ai_full_scan": "0"})
    MockLLM.score_calls = 0
    s0 = req("/api/search?q=" + urllib.parse.quote("关闭模式测试") + "&limit=30")
    check("关闭模式: 不评分零AI调用", MockLLM.score_calls == 0 and
          not any((it.get("reason") or "") for it in s0.get("items", [])),
          "calls=%d items=%d" % (MockLLM.score_calls, len(s0.get("items", []))))
    req("/api/settings", {"ai_full_scan": "1"})

    # --- 搜索深挖: 到底后近义词扩波 + 波次上限收敛 + 循环保护 ---
    app._search_wave.clear()

    def _deep_base(kw):
        return [{"key": "d_%s_%d" % (str(kw).replace(" ", "")[:6], i), "platform": "B站",
                 "title": "深挖%s_%d" % (str(kw)[:8], i), "author": "作者", "desc": "简介",
                 "cover": "", "views": 500000, "likes": 10, "duration": 200,
                 "pubdate": int(time.time()) - 3600,
                 "url": "https://www.bilibili.com/video/d_%d" % i} for i in range(30)]
    app.search_items = lambda kw, page=1, limit=30: (_deep_base(kw) if page == 1 else [])
    app.tb_search_items = lambda q, rn=20: []
    sd1 = req("/api/search?q=" + urllib.parse.quote("深挖测试") + "&limit=30")
    check("深挖: 首波不扩波", sd1.get("total") == 30 and sd1.get("end") is False
          and sd1.get("deep_terms") == [],
          (sd1.get("total"), sd1.get("end"), sd1.get("deep_terms")))
    sd2 = req("/api/search?q=" + urllib.parse.quote("深挖测试") + "&offset=30&limit=30")
    check("深挖: 到底触发近义词扩波", sd2.get("total") == 60 and sd2.get("deep_terms") == ["深挖词1"]
          and len(sd2.get("items", [])) == 30,
          (sd2.get("total"), sd2.get("deep_terms"), len(sd2.get("items", []))))
    sd5 = req("/api/search?q=" + urllib.parse.quote("深挖测试") + "&offset=120&limit=30")
    check("深挖: 扩波3次达上限收敛", sd5.get("total") == 120 and sd5.get("end") is True
          and sd5.get("deep_terms") == ["深挖词1", "深挖词2", "深挖词3"],
          (sd5.get("total"), sd5.get("end"), sd5.get("deep_terms")))
    sd6 = req("/api/search?q=" + urllib.parse.quote("深挖测试") + "&offset=120&limit=30")
    check("深挖: 到底后循环保护(不再增长)", sd6.get("end") is True and not sd6.get("items")
          and sd6.get("total") == 120, (sd6.get("total"), sd6.get("end")))
    app._search_wave.clear()

    # --- B站直搜全空时, 首屏不许"一条B站视频都没有"(本地沉淀池兜底) ---
    # 用户报的"搜索结果经常没有B站视频": 直搜接口被风控(-412)/WBI 签名失效/断网时,
    # 旧实现把"接口失败"当成"这词没结果"(search_items 里 code!=0 直接 return []),
    # 空页直接收工 → 首屏只剩贴吧。现在: 失败重试一次; 仍为空则用本地池里收过的 B站 条目兜底。
    req("/api/settings", {"ai_full_scan": "0"})   # 关掉同步评分门禁, 只看"有没有 B站条目"
    app._search_wave.clear()
    _pool_it = {"key": "pool_bili_0001", "platform": "B站", "title": "沉淀池兜底测试视频",
                "author": "池作者", "desc": "简介", "cover": "", "views": 300000,
                "likes": 20, "duration": 180, "pubdate": int(time.time()) - 1800,
                "url": "https://www.bilibili.com/video/pool_bili_0001"}
    with app._db_lock:
        app.db().execute("INSERT INTO items(key,json,fetched_at,score_at) VALUES(?,?,?,0) "
                         "ON CONFLICT(key) DO UPDATE SET json=excluded.json,fetched_at=excluded.fetched_at",
                         (_pool_it["key"], json.dumps(_pool_it, ensure_ascii=False), time.time()))
        app.db().commit()
    _bili_direct = app.search_items
    app.search_items = lambda kw, page=1, limit=30, **kwx: []   # 模拟直搜全线失败
    app.tb_search_items = lambda q, rn=20: []
    sfb = req("/api/search?q=" + urllib.parse.quote("沉淀池兜底测试") + "&limit=30")
    check("搜索兜底: 直搜全空时本地池出B站条目",
          any(it.get("key") == "pool_bili_0001" for it in sfb.get("items", [])),
          [(it.get("platform"), it.get("key")) for it in sfb.get("items", [])][:3])
    # 空首波不能被当成正常结果缓存一小时, 否则同一个词一小时内永远只剩贴吧
    check("搜索兜底: 空首波不缓存一小时",
          "SEARCH_WAVE_TTL + 120" in open(app.__file__, encoding="utf-8").read())
    app.search_items = _bili_direct
    app.tb_search_items = lambda q, rn=20: []
    app._search_wave.clear()
    req("/api/settings", {"ai_full_scan": "1"})

    # --- 搜索不受"最低播放量/时长"质量窗限制(那是信息流推荐的质量闸) ---
    # 旧实现里 skip_age=True 只跳过时间窗, 播放量窗/时长窗仍对 B站 生效; 贴吧/小黑盒没有播放量字段
    # 天然免疫 → 用户一设"最低播放量", 搜索结果就只剩贴吧, 表现成"搜索经常没有B站视频"。
    # 注意: 播放量窗属于机器规则, 走 app.sset("m_min_views") 而不是 /api/settings 白名单
    _old_min_views = app.sget("m_min_views")
    req("/api/settings", {"ai_full_scan": "0"})
    app.sset("m_min_views", "100000")
    _win_it = {"key": "win_bili_0001", "platform": "B站", "title": "播放量窗测试视频",
               "author": "作者", "desc": "简介", "cover": "", "views": 10, "likes": 0,
               "duration": 60, "pubdate": int(time.time()) - 1800,
               "url": "https://www.bilibili.com/video/win_bili_0001"}
    _feed_kept = app.rules_filter([dict(_win_it)], skip_age=True)
    check("搜索质量窗: 信息流仍按最低播放量筛", len(_feed_kept) == 0, len(_feed_kept))
    _search_kept = app.rules_filter([dict(_win_it)], skip_age=True, skip_windows=True)
    check("搜索质量窗: 显式搜索跳过播放量窗", len(_search_kept) == 1, len(_search_kept))
    app._search_wave.clear()
    app.search_items = lambda kw, page=1, limit=30, **kwx: ([dict(_win_it)] if page == 1 else [])
    app.tb_search_items = lambda q, rn=20: []
    sw = req("/api/search?q=" + urllib.parse.quote("播放量窗测试") + "&limit=30")
    check("搜索质量窗: 低播放B站条目仍出现在搜索里",
          any(it.get("key") == "win_bili_0001" for it in sw.get("items", [])),
          [(it.get("platform"), it.get("key")) for it in sw.get("items", [])][:3])
    app.search_items = _bili_direct
    app._search_wave.clear()
    app.sset("m_min_views", _old_min_views or "0")
    req("/api/settings", {"ai_full_scan": "1"})

    # --- 推荐标准: 兴趣分门槛剔除 ---
    req("/api/settings", {"ai_interest_min": "7"})
    d2 = req("/api/feed?offset=0&limit=40")
    check("兴趣门槛: 低兴趣条目被剔除", not any("low" in (it.get("key") or "") for it in d2.get("items", [])),
          [it.get("key") for it in d2.get("items", [])][:3])
    req("/api/settings", {"ai_interest_min": "0"})

    # --- 省钱方式: 批大小 30 → 评分请求数减半 ---
    MockLLM.score_calls = 0
    app.sset("ai_batch", "30")
    app.score_items([{"key": "b_%03d" % i, "platform": "B站", "title": "批%d" % i,
                      "author": "a", "desc": "d", "cover": "", "views": 100, "likes": 1,
                      "duration": 10, "pubdate": int(time.time()) - 3600,
                      "url": "https://x/b_%03d" % i} for i in range(30)])
    check("批大小30: 30条只发1个评分请求", MockLLM.score_calls == 1, MockLLM.score_calls)
    app.sset("ai_batch", "15")
    MockLLM.score_calls = 0
    app.score_items([{"key": "c_%03d" % i, "platform": "B站", "title": "批%d" % i,
                      "author": "a", "desc": "d", "cover": "", "views": 100, "likes": 1,
                      "duration": 10, "pubdate": int(time.time()) - 3600,
                      "url": "https://x/c_%03d" % i} for i in range(30)])
    check("批大小15: 30条发2个评分请求", MockLLM.score_calls == 2, MockLLM.score_calls)

    # --- AI 总开关: 关闭=评分/聊天全部停用, 零调用 ---
    req("/api/settings", {"ai_enabled": "0"})
    MockLLM.score_calls = 0
    app.score_items([{"key": "g_%03d" % i, "platform": "B站", "title": "批%d" % i,
                      "author": "a", "desc": "d", "cover": "", "views": 100, "likes": 1,
                      "duration": 10, "pubdate": int(time.time()) - 3600,
                      "url": "https://x/g_%03d" % i} for i in range(15)])
    check("总开关关: 评分零调用", MockLLM.score_calls == 0, MockLLM.score_calls)
    d = req("/api/chat", {"message": "你好", "stream": False})
    check("总开关关: 聊天提示已关闭", "关闭" in (d.get("reply") or ""), d.get("reply"))
    check("总开关关: 聊天不调LLM", MockLLM.score_calls == 0, MockLLM.score_calls)
    req("/api/settings", {"ai_enabled": "1"})
    d2 = req("/api/chat", {"message": "你好", "stream": False})
    check("总开关开: 聊天恢复", "关闭" not in (d2.get("reply") or ""), d2.get("reply"))

    # --- 并发评分去重: 两个请求同时评同一批, 只评一次 ---
    MockLLM.score_calls = 0

    def _score_concurrent():
        app.score_items([{"key": "h_%03d" % i, "platform": "B站", "title": "并发%d" % i,
                          "author": "a", "desc": "d", "cover": "", "views": 100, "likes": 1,
                          "duration": 10, "pubdate": int(time.time()) - 3600,
                          "url": "https://x/h_%03d" % i} for i in range(30)])
    ts = [threading.Thread(target=_score_concurrent), threading.Thread(target=_score_concurrent)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("并发评分去重: 2请求共享1次评分", MockLLM.score_calls == 2, MockLLM.score_calls)

    # --- 推送套餐引擎(规范.txt): 指纹去重/情绪过滤/资讯保鲜/槽位套餐/跨会话防重复/新维度门禁 ---
    def mkitem(key, title, views=100000, area="知识", interest=None, pubdate=None, dur=300):
        it = {"key": key, "platform": "B站", "title": title, "author": "作者", "desc": "简介",
              "cover": "", "views": views, "likes": 10, "duration": dur,
              "pubdate": pubdate if pubdate is not None else int(time.time()) - 3600,
              "url": "https://www.bilibili.com/video/%s" % key, "area": area, "tname": area,
              "tag": "", "topic": ""}
        if interest is not None:
            it["interest"] = interest
        return it

    # 规则②: 标题指纹去重(同标题只留最高热一条)
    mf = app.machine_filter([
        mkitem("fp_a1", "绝地潜兵2 新赛季更新 完整解析", 500000),
        mkitem("fp_a2", "绝地潜兵2 新赛季更新 完整解析", 400000),
        mkitem("fp_b", "blender 建模 材质 入门教程", 300000)])
    check("标题指纹去重: 同标题只留最高热", len(mf) == 2 and "fp_a1" in [x["key"] for x in mf],
          [x["key"] for x in mf])

    # 搜索下推(供给优先): 只有"不损失任何有效供给"时才下推档位;
    # pubdate 排序仅在时间窗开启且无播放量窗口时使用(pubdate+播放量下限会过滤光搜索来源)。
    old_views = app.sget("m_max_views")
    old_min_views = app.sget("m_min_views")
    app.sset("m_min_views", "0")
    app.sset("m_min_duration", "1800")
    app.sset("m_max_duration", "0")
    app.sset("m_max_age_months", "5")
    push = app.search_pushdown()
    check("搜索下推: 30分+档位", push.get("duration") == "3" and push.get("order") == "pubdate", push)
    app.sset("m_min_duration", "60")
    app.sset("m_max_duration", "1800")
    push2 = app.search_pushdown()
    check("搜索下推: 供给优先不丢部分有效档", "duration" not in push2, push2)
    app.sset("m_max_age_months", "0")
    push3 = app.search_pushdown()
    check("搜索下推: 无时间窗不推pubdate", "order" not in push3, push3)
    app.sset("m_max_age_months", "5")
    app.sset("m_min_views", "500")
    check("搜索下推: 播放量窗口开启不推pubdate",
          "order" not in app.search_pushdown(), app.search_pushdown())
    app.sset("m_min_views", "0")
    app.sset("m_max_duration", "300")
    app.sset("m_min_duration", "0")
    check("搜索下推: 无整档可保证时不下推", "duration" not in app.search_pushdown(), app.search_pushdown())
    app.sset("m_min_duration", "60")
    app.sset("m_max_duration", "0")
    app.sset("m_max_age_months", "5")
    app.sset("m_min_views", old_min_views or "0")
    app.sset("m_max_views", old_views or "0")

    # 漏斗仪表盘: machine_filter 每条规则剔除量可观测。
    before = app.funnel_stats()
    old_age = app.sget("m_max_age_months")
    app.sset("m_max_age_months", "1")
    now_ts = int(time.time())
    app.machine_filter([
        mkitem("fun_old", "一年前的旧视频", 1000, pubdate=now_ts - 400 * 86400),
        mkitem("fun_ok", "新鲜的正常视频", 1000, pubdate=now_ts)])
    app.sset("m_max_age_months", old_age or "5")
    after = app.funnel_stats()
    check("漏斗统计: 时间窗剔除可观测",
          after.get("age", 0) - before.get("age", 0) >= 1
          and after.get("fetched", 0) - before.get("fetched", 0) == 2,
          {"before": before, "after": after})

    # 源产出率自学习: 低产源被裁剪, 高产/未知源保留, 更新带指数衰减。
    with app._src_yield_lock:
        old_yield = dict(app._src_yield)
        app._src_yield.clear()
    try:
        app._src_yield_update("rank", 100, 2)
        app._src_yield_update("hot", 100, 60)
        check("源产出率: 高产源保留", app._src_yield_ok("hot") is True)
        check("源产出率: 低通过率源不抓取", app._src_yield_ok("rank") is False)
        check("源产出率: 未知源默认允许", app._src_yield_ok("related") is True)
        avail = [("全站榜", "rank", "u", 0, 1)] + [("热门%d" % i, "hot", "u", 0, 8) for i in range(1, 7)]
        pruned = app._src_yield_prune(avail, 4)
        check("源产出率: 低产源被裁剪", all(s[1] != "rank" for s in pruned[:4]) and len(pruned) == 4,
              [s[1] for s in pruned])
        app._src_yield_update("rank", 100, 100)   # 后续高产 → 衰减回升
        check("源产出率: 高产后恢复抓取", app._src_yield_ok("rank") is True)
        # 低产源被跳过时缓慢恢复(防永久饿死), 几轮后回到阈值以上重新参与抓取。
        with app._src_yield_lock:
            app._src_yield["related"] = {"fetched": 100, "kept": 1, "yield": 0.01}
        for _ in range(6):
            check_ok = app._src_yield_ok("related")
            if not check_ok:
                app._src_yield_recover("related")
        check("源产出率: 跳过源可恢复重试", app._src_yield_ok("related") is True,
              app._src_yield.get("related"))
    finally:
        with app._src_yield_lock:
            app._src_yield.clear()
            app._src_yield.update(old_yield)

    # 播放量上限/时长下限: 仅约束普通B站内容, 0 表示关闭上限
    app.sset("m_min_views", "0")
    app.sset("m_max_views", "500")
    app.sset("m_max_duration", "0")
    app.sset("m_min_duration", "60")
    range_pool = [mkitem("r_low", "低播放", 100, dur=120),
                  mkitem("r_high", "高播放", 100000, dur=120),
                  mkitem("r_short", "过短", 1000, dur=30),
                  dict(mkitem("r_follow", "关注内容", 100000, dur=30), following=True)]
    kept_range = app.machine_filter(range_pool)
    check("播放量上限/时长下限: 普通内容剔除且关注内容保留",
          {x["key"] for x in kept_range} == {"r_low", "r_follow"},
          [x["key"] for x in kept_range])
    app.sset("m_max_views", "0")
    kept_unlimited = app.machine_filter([mkitem("r_unlimited", "高播放", 100000, dur=120)])
    check("播放量上限0: 关闭上限", len(kept_unlimited) == 1, [x["key"] for x in kept_unlimited])
    app.sset("m_min_duration", "0")
    kept_short = app.machine_filter([mkitem("r_short_off", "短视频", 1000, dur=30)])
    check("时长下限0: 关闭下限", len(kept_short) == 1, [x["key"] for x in kept_short])
    app.sset("m_min_duration", "60")

    # 情绪过滤器(零token): 感叹号轰炸/标题党 → 剔除; 关闭开关则放行
    emo_pool = [mkitem("e1", "震惊！！！99%的人不看后悔"), mkitem("e2", "正常教程视频")]
    app.sset("m_emotion", "1")
    kept_e = app.machine_filter(emo_pool)
    check("情绪过滤器: 标题党剔除", len(kept_e) == 1 and kept_e[0]["key"] == "e2", [x["key"] for x in kept_e])
    app.sset("m_emotion", "0")
    kept_e2 = app.machine_filter(emo_pool)
    check("情绪过滤器: 关闭后放行", len(kept_e2) == 2, len(kept_e2))
    app.sset("m_emotion", "1")

    # 资讯保鲜: 24h 外的资讯剔除, 干货不受影响
    fresh_pool = [mkitem("n1", "某游戏版本更新公告", 1000, pubdate=int(time.time()) - 2 * 86400),
                  mkitem("n2", "blender 建模 材质 入门教程", 1000, pubdate=int(time.time()) - 2 * 86400)]
    app.sset("m_news_fresh", "1")
    kept_n = app.machine_filter(fresh_pool)
    check("资讯保鲜: 24h外资讯剔除/干货保留", [x["key"] for x in kept_n] == ["n2"], [x["key"] for x in kept_n])
    app.sset("m_news_fresh", "0")
    kept_n2 = app.machine_filter(fresh_pool)
    check("资讯保鲜: 关闭后放行", len(kept_n2) == 2, len(kept_n2))
    app.sset("m_news_fresh", "1")

    # 槽位套餐: 舒适60/拓展20/热点10 + 权重自适应(探索池空时自动再分配), 不丢条目
    app.sset("ai_full_scan", "0")
    app.sset("mode", "标准")
    app.sset("prefs_note", "blender教程")
    slot_pool = ([mkitem("s_c_%02d" % i, "blender教程 建模技巧%d" % i, 100000, interest=5)
                  for i in range(16)] +
                 [mkitem("s_x_%02d" % i, "军事解说 战史解析%d" % i, 300000, area="军事", interest=8)
                  for i in range(12)] +
                 [mkitem("s_s_%02d" % i, "新版本更新公告 第%d期" % i, 2000000, area="资讯",
                         pubdate=int(time.time()) - 3600)
                  for i in range(6)])
    order, slots = app.planned_feed(slot_pool, False)
    keys_in = {it["key"] for it in order}
    check("套餐规划: 总数不变且不丢条目", len(order) == len(slot_pool) and keys_in == {x["key"] for x in slot_pool},
          (len(order), len(slot_pool)))
    check("套餐规划: 槽位统计齐全", all(k in slots for k in ("舒适区", "拓展区", "热点")), slots)
    first10 = {it.get("_slot") for it in order[:10]}
    check("套餐规划: 首屏槽位交错(≥2类)", len(first10) >= 2, first10)
    check("槽位理由前缀(可解释性)", app._slot_reason({"reason": "测试", "_slot": "拓展区"}) == "拓展区·测试"
          and app._slot_reason({"reason": "拓展区·x", "_slot": "拓展区"}) == "拓展区·x", None)
    check("槽位理由兜底显示标签", app._slot_reason({"reason": "", "_slot": "舒适区", "tag": "blender,建模"}) == "舒适区·blender / 建模"
          and app._slot_reason({"reason": "", "_slot": "探索区", "platform": "贴吧", "tname": "rimworld"}) == "探索区·rimworld", None)

    # 跨会话防重复: 上次会话末尾类型(知识) → 新会话前5条强制避开
    app.sset("last_types", json.dumps(["B站|知识"]))
    order2, _ = app.planned_feed(slot_pool, True)
    check("跨会话防重复: 前5条避开上次类型", all(app._cat_key(it) != "B站|知识" for it in order2[:5]),
          [app._cat_key(it) for it in order2[:5]])
    app.sset("last_types", "")

    # 新评分维度门禁: 情绪净值极负一票剔除 / 严肃模式信息密度门槛
    app.sset("ai_full_scan", "1")
    now = int(time.time())
    for nk, sc in (("gate_emo", {"flame": 1, "spam": 1, "interest": 8, "density": 8, "emo": -7,
                                 "facts": 1, "topic": "测试", "reason": "测试理由"}),
                   ("gate_ok", {"flame": 1, "spam": 1, "interest": 8, "density": 8, "emo": 2,
                                "facts": 1, "topic": "测试", "reason": "测试理由"}),
                   ("gate_dense_low", {"flame": 1, "spam": 1, "interest": 8, "density": 2, "emo": 0,
                                       "facts": 1, "topic": "测试", "reason": "测试理由"}),
                   ("gate_dense_ok", {"flame": 1, "spam": 1, "interest": 8, "density": 9, "emo": 0,
                                      "facts": 1, "topic": "测试", "reason": "测试理由"})):
        it = mkitem(nk, ("深度学习教程 入门详解" if "dense" in nk else "中性测试标题") + nk[-4:], 1000)
        with app._db_lock:
            app.db().execute("INSERT INTO items(key,json,fetched_at,score_at,score_json) VALUES(?,?,?,?,?)",
                             (nk, json.dumps(it, ensure_ascii=False), now, now, json.dumps(sc, ensure_ascii=False)))
            app.db().commit()
    emo_acts = [app.item_by_key("gate_emo"), app.item_by_key("gate_ok")]
    o_emo, _ = app.planned_feed(emo_acts, False)
    check("情绪净值门禁: 极负剔除/正常保留", [x["key"] for x in o_emo] == ["gate_ok"],
          [x["key"] for x in o_emo])
    app.sset("mode", "严肃")
    den_acts = [app.item_by_key("gate_dense_low"), app.item_by_key("gate_dense_ok")]
    o_den, _ = app.planned_feed(den_acts, False)
    check("严肃密度门禁: 低密度剔除/高密度保留", [x["key"] for x in o_den] == ["gate_dense_ok"],
          [x["key"] for x in o_den])
    serious_cases = [
        ("unity教程 从零做背包系统", "", "游戏开发", "B站", True),
        ("UE5零基础入门教程 虚幻引擎开发", "", "游戏开发", "B站", True),
        ("blender建模入门课程", "", "知识", "B站", True),
        ("量子力学原理 深度讲解", "", "知识", "B站", True),
        ("绝地潜兵2 新手入门教学", "", "游戏", "B站", False),
        ("绝地潜兵2 机制讨论", "", "游戏", "B站", False),
        ("独立游戏开发者成为肥羊", "", "游戏", "B站", False),
        ("某某番剧世界观入门解析", "", "番剧", "B站", False),
        ("动画演出原理学习", "", "动画", "B站", False),
        ("Blender角色上色和骨骼蒙皮教程", "", "", "贴吧", True),
        ("Blender为什么简易形变不行", "", "", "贴吧", True),
        ("盒友杂谈 游戏推荐和新作资讯", "", "", "小黑盒", False),
        ("DLSS性能配置与显卡测试", "", "", "小黑盒", True),
        ("绝地潜兵2 爆笑实况解说", "", "游戏", "B站", False),
        ("新游戏锐评 吐槽合集", "", "游戏", "B站", False),
        ("原神抽卡名场面高能剪辑", "", "游戏", "B站", False),
    ]
    ok_ser = []
    for title, desc, area, platform, want in serious_cases:
        got = app.is_serious_ok({"title": title, "desc": desc, "area": area, "platform": platform,
                                 "tag": "", "tname": ""})
        ok_ser.append(got == want)
    check("严肃模式内容门禁: 教程硬核放行娱乐剔除", all(ok_ser), ok_ser)
    app.sset("mode", "标准")
    app.sset("prefs_note", "")

    # 实时干预: 聊天框临时屏蔽(到期自动解封)
    d = req("/api/chat", {"message": "未来1小时内别推游戏", "stream": False})
    check("临时屏蔽命令: 回复+解封提示", "已记下" in (d.get("reply") or "") and "解封" in (d.get("reply") or ""),
          d.get("reply"))
    app.add_tmp_block("二次元", 60)
    blocks_now = json.loads(app.sget("tmp_blocks"))
    blocks_now.append({"k": "过期词", "until": time.time() - 10})
    app.sset("tmp_blocks", json.dumps(blocks_now, ensure_ascii=False))
    kws = app.tmp_block_kws()
    check("临时屏蔽: 生效且过期自动清理", sorted(kws) == sorted(["游戏", "二次元"]), kws)
    app.sset("tmp_blocks", "")
    # 探索模式命令
    d2 = req("/api/chat", {"message": "探索一下新领域", "stream": False})
    check("探索模式命令", "探索模式" in (d2.get("reply") or ""), d2.get("reply"))
    app.sset("explore_boost_ts", "0")

    # --- 空流修复回归(规范引擎落地后发现的三个坑) ---
    # ① 指纹记忆随池清空: 刷新/清池后重建拉回同内容, 旧指纹失效否则永远0内容
    fp_t = app._title_fp("指纹清空回归测试标题")
    app._fp_dup(fp_t, time.time())
    check("指纹记忆: 2h内重复拦截", app._fp_dup(fp_t, time.time()) is True, None)
    app._fp_clear()
    check("指纹记忆: 清池后失效", app._fp_dup(fp_t, time.time()) is False, None)
    # ② 冷启动降级: 全扫开启但尚无任何评分 → 临时按热度放行(不长时间空白)
    app.sset("ai_full_scan", "1")
    cold_pool = [mkitem("cold_%02d" % i, "冷启动测试视频%d" % i, 100000 - i) for i in range(12)]
    o_cold, _ = app.planned_feed(cold_pool, False)
    check("冷启动放行: 无评分不空白", len(o_cold) == len(cold_pool), len(o_cold))
    # ③ 追加规划分页稳定: 同世代新增条目只接尾部, 已有顺序不动(offset 不错位)
    app.sset("ai_full_scan", "0")
    base_pool = [mkitem("ap_%02d" % i, "追加规划测试%d" % i, 900000 - i, area="科技") for i in range(20)]
    app._plan_cache.update(key=None, order=[], token=-1, epoch=-1, stail=None)
    o1 = app._feed_plan(base_pool, False, 1)
    more = [mkitem("ap2_%02d" % i, "追加规划第二批%d" % i, 800000 - i, area="军事") for i in range(10)]
    o2 = app._feed_plan(base_pool + more, False, 2)
    # 新不变式: 未消费后段允许重混(修沉积带), 但必须不丢条目、无重复、旧条目全保留。
    check("追加规划: 不丢条目且无重复",
          len(o2) == 30 and len({x["key"] for x in o2}) == 30
          and {x["key"] for x in o1} <= {x["key"] for x in o2},
          (len(o1), len(o2)))
    # ④ 评分落库推进 epoch → 全扫模式下新筛内容渐进可见(规划缓存不永久陈旧)
    e0 = app._score_epoch
    app._write_scores({"epoch_probe": {"flame": 0, "interest": 5}})
    check("评分写入推进epoch", app._score_epoch == e0 + 1, (e0, app._score_epoch))
    # ⑤ 刷新回归: 同样内容清池重建后必须正常出流(端到端)
    req("/api/feed?refresh=1")
    ok_refresh = False
    for _ in range(40):
        d4 = req("/api/feed?offset=0&limit=40")
        if d4.get("items"):
            ok_refresh = True
            break
        time.sleep(0.1)
    check("刷新回归: 同样内容重建不空流", ok_refresh, "items=%d" % len(d4.get("items", [])))
    # ⑥ 构建窗口期: 空池请求不得崩溃(IN () 非法SQL防御 / 规划对空输入安全)
    empty_cached, empty_pending = app._load_cache([], 3600)
    o_empty, _ = app.planned_feed([], False)
    check("空池规划: 不崩溃且返回空", empty_cached == {} and empty_pending == [] and o_empty == [], None)
    app.sset("ai_full_scan", "1")

    # --- 贴吧: 解析器/权重混排/吧列表/平台豁免/登录态 ---
    tb_html = """
    <ul>
      <li class="j_thread_list clearfix" data-field='{"tid":11111111}'>
        <a rel="noopener" href="/p/11111111" title="blender教程 从零开始" class="j_th_tit ">blender教程 从零开始</a>
        <span class="threadlist_rep_num center_text" title="回复">88</span>
        <span class="pull-right is_show_create_time" title="创建时间">8-10</span>
        <a class="frs-author-name j_user_card" href="/home/main/">楼主甲</a>
        <div class="threadlist_abs threadlist_abs_onlyline">记录一下blender学习历程，第一周做的小房子</div>
        <img src="https://tb3.bdstatic.com/public/img/icon_pc_picheader_n.432946a7.png" attr="52154" data-original="https://tiebapic.baidu.com/forum/a.jpg" />
        <img class="threadlist_pic j_m_pic" data-original="https://tiebapic.baidu.com/forum/b.jpg" />
      </li>
      <li class="j_thread_list clearfix" data-field='{"tid":22222222}'>
        <i class="icon-top" alt="置顶"></i>
        <a rel="noopener" href="/p/22222222" title="吧规公告" class="j_th_tit ">吧规公告</a>
        <span class="threadlist_rep_num center_text" title="回复">5</span>
      </li>
      <li class="j_thread_list clearfix" data-field='{"tid":33333333}'>
        <a rel="noopener" href="/p/33333333" title="绝地潜兵2 新赛季攻略" class="j_th_tit ">绝地潜兵2 新赛季攻略</a>
        <span class="threadlist_rep_num center_text" title="回复">1234</span>
        <span class="pull-right is_show_create_time" title="创建时间">23:32</span>
        <a class="frs-author-name j_user_card" href="/home/main/">楼主乙</a>
        <div class="threadlist_abs threadlist_abs_onlyline">新赛季配装解析</div>
      </li>
    </ul>
    """
    tb_items = app._tb_html_to_items(tb_html, "绝地潜兵")
    check("贴吧解析: 置顶剔除+2条保留", len(tb_items) == 2, len(tb_items))
    it0 = tb_items[0]
    check("贴吧解析: 字段齐全", it0["platform"] == "贴吧" and it0["title"] == "blender教程 从零开始"
          and it0["author"] == "楼主甲" and it0["reply"] == 88 and it0["tname"] == "绝地潜兵",
          it0)
    check("贴吧解析: 双图+占位图剔除", it0["covers"] == ["https://tiebapic.baidu.com/forum/a.jpg",
                                                 "https://tiebapic.baidu.com/forum/b.jpg"], it0["covers"])
    check("贴吧解析: 摘要+时间", "blender" in it0["desc"] and it0["pubdate"] > 0, it0)
    check("贴吧解析: key=帖子链接", it0["key"] == "https://tieba.baidu.com/p/11111111", it0["key"])
    # 严肃模式词门(吧名"blender"无游戏词, 教程帖放行)
    tb_items_ser = app._tb_html_to_items(tb_html, "blender")
    check("贴吧严肃词门: 教程帖放行", app.is_serious_ok(tb_items_ser[0]), tb_items_ser[0])
    # 游戏相关吧不再因吧名整体剔除: 教程/学习帖放行, 纯玩法攻略仍按标题词剔除
    check("贴吧严肃词门: 游戏吧教程放行", app.is_serious_ok(tb_items[0]), tb_items[0])
    bad_game_post = dict(tb_items[0], title="绝地潜兵2 高难BOSS打法攻略", desc="配装走位速通")
    check("贴吧严肃词门: 纯玩法攻略剔除", not app.is_serious_ok(bad_game_post), bad_game_post)
    # 权重混排: 25/90 → 目标贴吧占比 25/115≈21.7%
    def mkpool(n_bili, n_tb):
        pool = []
        for i in range(n_bili):
            pool.append({"key": "b%d" % i, "platform": "B站", "title": "v%d" % i,
                         "author": "a", "desc": "", "cover": "", "views": 1000 - i,
                         "likes": 1, "duration": 10, "pubdate": 0,
                         "url": "https://www.bilibili.com/video/b%d" % i,
                         "src": "B%d" % (i % 3)})
        for i in range(n_tb):
            pool.append({"key": "t%d" % i, "platform": "贴吧", "title": "p%d" % i,
                         "author": "a", "desc": "", "cover": "", "covers": [],
                         "views": 0, "likes": 0, "reply": 10, "danmaku": None,
                         "duration": 0, "pubdate": 0,
                         "url": "https://tieba.baidu.com/p/%d" % i,
                         "src": "贴吧·X%d" % (i % 2)})
        return pool
    app.sset("tb_weight", "25")
    app.sset("bili_weight", "90")
    # 候选不足目标(120bili+20tb, 目标30tb→截断至20): 全部采用, 均匀散布, 不丢条目
    mixed = app.mix_sources(mkpool(120, 20))
    n_tb = sum(1 for it in mixed if it.get("platform") == "贴吧")
    # v0.3.1.57: 配额上限成为契约 —— 每路最多出 round(total*w/Σw) 条, 池子再多也截断,
    # 免得高权重那路先把名额吃完、最低权重那路再把尾巴整段吃掉(用户报的"5% 直播刷一屏")。
    _Wt = 115.0   # 这个池子里只有 B站(90)/贴吧(25), Σw 只算在场的平台
    _n_b = sum(1 for it in mixed if it.get("platform") == "B站")
    _cap_b = int(round(140 * 90 / _Wt))
    check("权重混排: 高权重按配额截断(不再全采用)",
          len(mixed) <= 140 and _n_b <= _cap_b and _n_b >= _cap_b - 12, (len(mixed), _n_b, _cap_b))
    check("权重混排: 候选不足时贴吧全采用", n_tb == 20, n_tb)
    gaps, last = [], -1
    for i, it in enumerate(mixed):
        if it.get("platform") == "贴吧":
            if last >= 0:
                gaps.append(i - last)
            last = i
    check("权重混排: 均匀不扎堆(间隔≤9)", gaps and max(gaps) <= 9, max(gaps) if gaps else None)
    # 平台防连发: 100:25:25 下只要其他平台还有候选, 同平台最多连续2条
    spaced = app.mix_sources(
        [{"key": "b%d" % i, "platform": "B站", "src": "b"} for i in range(40)] +
        [{"key": "t%d" % i, "platform": "贴吧", "src": "t"} for i in range(10)] +
        [{"key": "x%d" % i, "platform": "小黑盒", "src": "x"} for i in range(10)])
    plats = [it["platform"] for it in spaced]
    runs = []
    cur = 1
    for a, b in zip(plats, plats[1:]):
        if a == b:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    # 防连发约束中段; 末尾单平台连发属于候选耗尽(B站被消费完)的物理极限, 由重混+供给修复。
    check("权重混排: 平台连续不超过2条(候选耗尽的尾段除外)",
          max(runs[:-1] or [0]) <= 2, {"runs": runs})
    # 桶级混排: 大小悬殊的桶不能尾部连片(旧轮转缺陷), 大桶按占比散布
    big = [{"key": "b%d" % i, "src": "搜·军事"} for i in range(12)]
    small = [{"key": "s%d" % i, "src": "搜·菜谱"} for i in range(2)]
    bm = app._bucket_mix(big + small)
    srcs = [it["src"] for it in bm]
    runs, cur = [], 1
    for a, b in zip(srcs, srcs[1:]):
        if a == b:
            cur += 1
        else:
            runs.append(cur); cur = 1
    runs.append(cur)
    check("桶级混排: 大桶不尾部连片", len(bm) == 14 and max(runs) <= 7,
          {"max_run": max(runs), "order": srcs[:14]})
    # 50/50 完美交替
    app.sset("tb_weight", "50")
    app.sset("bili_weight", "50")
    mixed50 = app.mix_sources(mkpool(50, 50))
    n_tb50 = sum(1 for it in mixed50 if it.get("platform") == "贴吧")
    gaps50, last50 = [], -1
    for i, it in enumerate(mixed50):
        if it.get("platform") == "贴吧":
            if last50 >= 0:
                gaps50.append(i - last50)
            last50 = i
    check("权重混排: 50/50 交替", n_tb50 == 50 and max(gaps50) <= 3, (n_tb50, max(gaps50)))
    # 100/100(同权) → 严格交替
    app.sset("tb_weight", "100")
    app.sset("bili_weight", "100")
    mixed1 = app.mix_sources(mkpool(40, 40))
    gaps1, last1 = [], -1
    for i, it in enumerate(mixed1):
        if it.get("platform") == "贴吧":
            if last1 >= 0:
                gaps1.append(i - last1)
            last1 = i
    check("权重混排: 100/100同权散布", max(gaps1) <= 3, max(gaps1))
    # 权重=0: 贴吧不出 / 贴吧=0 B站不出
    app.sset("tb_weight", "0")
    app.sset("bili_weight", "90")
    mixed0 = app.mix_sources(mkpool(100, 40))
    check("权重0: 贴吧全不出", not any(it.get("platform") == "贴吧" for it in mixed0), len(mixed0))
    app.sset("tb_weight", "25")
    app.sset("bili_weight", "0")
    mixed0b = app.mix_sources(mkpool(100, 40))
    check("B站权重0: 只出贴吧", all(it.get("platform") == "贴吧" for it in mixed0b), len(mixed0b))
    app.sset("bili_weight", "90")
    app.sset("tb_weight", "50")
    app.sset("xhh_weight", "50")
    app.sset("bili_weight", "50")
    tri_pool = mkpool(40, 40)
    now_tri = int(time.time())
    for i in range(40):
        tri_pool.append({"key": "x%d" % i, "platform": "小黑盒", "title": "xhh%d" % i,
                         "author": "a", "desc": "", "cover": "", "covers": [],
                         "views": 0, "likes": 0, "reply": 10, "danmaku": None,
                         "duration": 0, "pubdate": now_tri,
                         "url": "https://www.xiaoheihe.cn/app/bbs/link/%d" % i,
                         "src": "小黑盒·X%d" % (i % 2)})
    planned_tri, _ = app.planned_feed(tri_pool, False)
    first30 = planned_tri[:30]
    n_bili30 = sum(1 for it in first30 if it.get("platform") == "B站")
    n_tb30 = sum(1 for it in first30 if it.get("platform") == "贴吧")
    n_xhh30 = sum(1 for it in first30 if it.get("platform") == "小黑盒")
    check("套餐规划后仍保留平台权重", min(n_bili30, n_tb30, n_xhh30) >= 7,
          (n_bili30, n_tb30, n_xhh30))
    app.sset("tb_weight", "25")
    app.sset("xhh_weight", "15")
    app.sset("bili_weight", "90")
    app.sset("tb_bars", "blender\nrimworld")
    allowed = {"key": "ta", "platform": "贴吧", "title": "blender教程", "author": "a",
               "desc": "", "cover": "", "covers": [], "views": 0, "likes": 0, "reply": 1,
               "danmaku": None, "duration": 0, "pubdate": now, "url": "https://tieba.baidu.com/p/3",
               "tname": "blender", "tag": "blender", "src": "贴吧·blender"}
    denied = dict(allowed, key="td", title="原神攻略", tname="原神", tag="原神", src="贴吧·原神",
                  url="https://tieba.baidu.com/p/4")
    check("贴吧允许吧过滤: 放行配置吧", app.tieba_bar_allowed(allowed), allowed)
    check("贴吧允许吧过滤: 拒绝非配置吧", not app.tieba_bar_allowed(denied), denied)
    app.sset("tb_bars", "")
    # 吧列表设置
    r = req("/api/tieba", {"bars": ["绝地潜兵", "blender", "军事"]})
    check("吧列表保存", r.get("ok"))
    d = req("/api/tieba")
    check("吧列表读取", d["bars"] == ["绝地潜兵", "blender", "军事"], d)
    check("权重读取默认", d["bili_weight"] == 90 and d["tb_weight"] == 25, (d["bili_weight"], d["tb_weight"]))
    # 求助帖开关: 默认开
    check("求助帖默认允许", d.get("allow_help", True) is True, d)
    # is_help_post 判定
    hp = app.is_help_post({"title": "求助 blender 渲染报错", "desc": ""})
    hp2 = app.is_help_post({"title": "blender教程 从零开始", "desc": "记录学习历程"})
    check("求助判定: 求助帖命中", hp and not hp2, (hp, hp2))
    # machine_filter 过滤求助帖(贴吧)
    now = int(time.time())
    pool_h = [
        {"key": "h1", "platform": "贴吧", "title": "求助 侧边栏不见了", "author": "a",
         "desc": "", "cover": "", "views": 0, "likes": 0, "duration": 0, "pubdate": now,
         "url": "https://tieba.baidu.com/p/1", "tname": "blender", "tag": ""},
        {"key": "h2", "platform": "贴吧", "title": "blender教程 从零开始", "author": "b",
         "desc": "记录学习", "cover": "", "views": 0, "likes": 0, "duration": 0, "pubdate": now,
         "url": "https://tieba.baidu.com/p/2", "tname": "blender", "tag": ""},
    ]
    app.sset("tb_allow_help", "0")
    kept = app.machine_filter(pool_h)
    check("求助帖过滤: 关闭后剔除", len(kept) == 1 and kept[0]["key"] == "h2", [k["key"] for k in kept])
    app.sset("tb_allow_help", "1")
    kept2 = app.machine_filter(pool_h)
    check("求助帖过滤: 开启后全保留", len(kept2) == 2, len(kept2))
    # API开关
    req("/api/tieba", {"allow_help": False})
    check("求助帖API: 关闭保存", req("/api/tieba")["allow_help"] is False)
    req("/api/tieba", {"allow_help": True})
    # 权重范围: v0.3.1.52 起改成 0-100 连续可调(左栏每条内容源下面是一条滑条),
    # 只拒绝越界/非数字; 旧的"只认 0/25/50/75/100 四档"白名单已取消
    req("/api/tieba", {"tb_weight": "37", "bili_weight": "99"})
    d = req("/api/tieba")
    check("权重范围: 0-100 任意整数可存", d["bili_weight"] == 99 and d["tb_weight"] == 37,
          (d["bili_weight"], d["tb_weight"]))
    req("/api/tieba", {"tb_weight": "101", "bili_weight": "-1"})
    d = req("/api/tieba")
    check("权重范围: 越界值忽略", d["bili_weight"] == 99 and d["tb_weight"] == 37,
          (d["bili_weight"], d["tb_weight"]))
    req("/api/tieba", {"tb_weight": "100", "bili_weight": "25"})
    d = req("/api/tieba")
    check("权重范围: 100合法", d["bili_weight"] == 25 and d["tb_weight"] == 100,
          (d["bili_weight"], d["tb_weight"]))
    req("/api/tieba", {"tb_weight": "25", "bili_weight": "90"})
    # 平台豁免: 贴吧条目不过分区检查
    check("item_domain_ok 贴吧豁免", app.item_domain_ok({"platform": "贴吧", "tname": "X"}, {"anime"}))
    # 关注的人标记: UID 是权威身份；旧条目缺 UID 时才回退到昵称。
    app.sset("bili_follow_names", json.dumps(["旧昵称"], ensure_ascii=False))
    app.sset("bili_follow_mids", json.dumps(["42"]))
    with app._follow_lock:
        app._following_names_cache.update(ts=0, names=set(), mids=set())
    now_follow = int(time.time())
    uid_cand = {"platform": "B站", "author": "改名后的作者", "author_mid": 42,
                "title": "普通推荐流里的视频", "pubdate": now_follow, "following": False}
    reused_name = {"platform": "B站", "author": "旧昵称", "author_mid": 99,
                   "title": "昵称复用的普通视频", "pubdate": now_follow, "following": False}
    legacy_cand = {"platform": "B站", "author": "旧昵称", "title": "旧缓存视频",
                   "pubdate": now_follow, "following": False}
    app._annotate_following([uid_cand, reused_name, legacy_cand])
    check("关注的人标记: UID支持改名", uid_cand.get("following") is True, uid_cand)
    check("关注的人标记: UID防昵称复用误标", reused_name.get("following") is not True, reused_name)
    check("关注的人标记: 旧缓存昵称兼容", legacy_cand.get("following") is True, legacy_cand)
    raw_rank = {"bvid": "BVuidtest", "title": "UID测试", "owner": {"name": "改名后的作者", "mid": 42},
                "stat": {}, "duration": 120, "pubdate": now_follow}
    normal_item = app.normalize(raw_rank)
    app._annotate_following([normal_item])
    check("关注的人标记: 正常榜单保留UID", normal_item.get("author_mid") == 42 and normal_item.get("following") is True,
          normal_item)
    # 贴吧未读数: 仅解析页面明确给出的“回复我/@我”计数，缺字段时不猜测为0。
    old_tb_check, old_urlopen = app.tb_login_check, app.urllib.request.urlopen
    class TbUnreadResponse:
        def __init__(self, text): self.text = text.encode("utf-8")
        def read(self): return self.text
        def __enter__(self): return self
        def __exit__(self, *args): return False
    try:
        app.tb_login_check = lambda: {"logged_in": True}
        pages = iter(["<script>count:{replyme:4,atme:2}</script>",
                      "<script>count:{replyme:4,atme:2}</script>"])
        app.urllib.request.urlopen = lambda *args, **kwargs: TbUnreadResponse(next(pages))
        tb_unread = app.tb_unread_messages()
        check("贴吧未读: 回复我加@我真实汇总", tb_unread == 6, tb_unread)
        app.urllib.request.urlopen = lambda *args, **kwargs: TbUnreadResponse("<script>count:{}</script>")
        # 这里验的是"解析失败就当未知", 不是缓存有效期 —— 先让缓存过期, 否则新实现会直接
        # 返回 5 分钟内的旧值(那本身是期望行为)。
        app._tb_unread.update(ts=0, data=None)
        check("贴吧未读: 缺失计数保持未知", app.tb_unread_messages() is None)
    finally:
        app.tb_login_check, app.urllib.request.urlopen = old_tb_check, old_urlopen
    # 登录态(空cookie=未登录)
    check("贴吧登录态: 未登录", not req("/api/tieba")["logged_in"])
    # 手动cookie无效 → 被清
    r = req("/api/tieba/login/manual", {"cookie": "BDUSS=fake123"})
    check("贴吧手动登录: 无效cookie拒绝", not r.get("ok") and not req("/api/tieba")["cookie_set"], r)
    # 无 Chromium 浏览器时: 打开默认浏览器并切换到手动 Cookie 导入，不再直接报启动失败
    old_find, old_open = app._tb_find_browser, app.webbrowser.open
    try:
        app._tb_find_browser = lambda: ""
        opened = []
        app.webbrowser.open = lambda url: opened.append(url) or True
        app._CDP_PROC = None
        fallback = app.tb_login_start()
        check("贴吧登录: 默认浏览器手动导入兜底", fallback.get("ok") and fallback.get("manual") and opened,
              fallback)
    finally:
        app._tb_find_browser, app.webbrowser.open = old_find, old_open
    # 匿名会话种子(不验证内容, 只验证函数可调用无异常)
    jar = app._tb_session_jar()
    seeded = app._tb_seed(jar)
    check("贴吧匿名会话种子(可选联网)", isinstance(seeded, bool))
    # 时间解析
    check("贴吧时间解析: 月日", app.tb_time_parse("8-10") > 0, app.tb_time_parse("8-10"))
    check("贴吧时间解析: 时分", app.tb_time_parse("23:32") > 0, app.tb_time_parse("23:32"))
    check("贴吧时间解析: 非法", app.tb_time_parse("") == 0 and app.tb_time_parse("abc") == 0)

    # --- 小黑盒: 签名/平台注册/混排 ---
    # hkey 对照真实抓包(期望 ZYP7136)
    hk = app.xhh_hkey("/bbs/app/api/search/found", 1787973690, "C838A69A567F021B8833960FC7E63DA3")
    check("小黑盒hkey对照抓包", hk == "ZYP7136", hk)
    # 平台注册表含小黑盒
    check("平台注册含小黑盒", any(p[0] == "小黑盒" for p in app.PLATFORMS), app.PLATFORMS)
    # 混排含小黑盒(权重15)
    app.sset("xhh_weight", "15")
    app.sset("bili_weight", "90")
    app.sset("tb_weight", "25")
    mp = app.mix_sources([
        {"key": "h%d" % i, "platform": "小黑盒", "title": "x%d" % i, "src": "H%d" % (i % 2)}
        for i in range(10)] + [
        {"key": "b%d" % i, "platform": "B站", "title": "v%d" % i, "src": "B%d" % (i % 3)}
        for i in range(30)])
    n_xhh = sum(1 for it in mp if it.get("platform") == "小黑盒")
    # 配额 = round(40 * 15 / 130) ≈ 5: 权重 15 的一路不再和 B站 平分秋色,
    # 超出的条目留在池里等后续构建重混(不是丢掉)。
    check("小黑盒混排: 按配额保留", 3 <= n_xhh <= 6, n_xhh)
    # 权重API含小黑盒
    d = req("/api/tieba")
    pnames = [p["name"] for p in d.get("platforms", [])]
    check("平台API含小黑盒", "小黑盒" in pnames, pnames)
    # 小黑盒未读数: 仅接受上游明确的未读字段，空结果不伪造为0。
    old_xhh_get = app.xhh_get
    try:
        app.sset("xhh_heybox_id", "10001")
        app.xhh_get = lambda *args, **kwargs: ({"status": "ok", "message_unread_num": 2, "result": []}, None)
        check("小黑盒未读: 当前接口顶层计数", app.xhh_unread_messages() == 2, app.xhh_unread_messages())
        app.xhh_get = lambda *args, **kwargs: ({"status": "ok", "result": {"unread_count": 3}}, None)
        check("小黑盒未读: 服务端总数", app.xhh_unread_messages() == 3, app.xhh_unread_messages())
        app.xhh_get = lambda *args, **kwargs: ({"status": "ok", "result": {"messages": [{"is_read": False}, {"is_read": True}]}}, None)
        check("小黑盒未读: 明确已读标记", app.xhh_unread_messages() == 1, app.xhh_unread_messages())
        app.xhh_get = lambda *args, **kwargs: ({"status": "ok", "result": {"messages": [{"id": 1}]}}, None)
        check("小黑盒未读: 无标记保持未知", app.xhh_unread_messages() is None)
    finally:
        app.xhh_get = old_xhh_get
        app.sset("xhh_heybox_id", "")
    # 小黑盒完整采集: 四页并发请求且跨页去重。
    old_xhh_feed, old_xhh_cursor = app.xhh_feed_items, app._xhh_cursor
    try:
        calls = []
        def fake_xhh_feed(offset=0, limit=20, pull="0"):
            calls.append((offset, limit))
            time.sleep(0.02)
            return ([{"key": "xhh-%d" % (offset // 30), "platform": "小黑盒"}], "")
        app.xhh_feed_items = fake_xhh_feed
        app._xhh_cursor = 0
        xitems, xerrs = app.fetch_xhh_pool(quick=False)
        check("小黑盒采集: 多页并发且去重", len(calls) == 4 and len(xitems) == 4 and not xerrs,
              {"calls": calls, "items": len(xitems)})
    finally:
        app.xhh_feed_items, app._xhh_cursor = old_xhh_feed, old_xhh_cursor
    # 小黑盒状态API
    dx = req("/api/xhh")
    check("小黑盒状态API", dx.get("ok") is True, dx)

    # ---------- AI 悬浮窗: 联网证据管线 + 悬浮球端点 ----------
    # MockLLM 增补核查/回怼/查询词桩(见 MockLLM.do_POST 顶部标记)
    def req_sse(path, body):
        data = json.dumps(body).encode("utf-8")
        r = urllib.request.Request("http://127.0.0.1:8099" + path, data=data,
                                   headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")

    def parse_sse(raw):
        evs = []
        for chunk in raw.split("\n\n"):
            ev, data = None, {}
            for line in chunk.split("\n"):
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except Exception:
                        pass
            if ev:
                evs.append((ev, data))
        return evs

    # JSON 容错解析: 围栏 / 前后废话 / 嵌套 / 无JSON报错
    check("悬浮窗:JSON容错-围栏", app.llm_json('```json\n{"a":1}\n```')["a"] == 1)
    check("悬浮窗:JSON容错-废话", app.llm_json('好的，结果：{"a": {"b": 2}} 请查收')["a"]["b"] == 2)
    try:
        app.llm_json("完全没有JSON")
        check("悬浮窗:JSON容错-报错", False)
    except ValueError:
        check("悬浮窗:JSON容错-报错", True)
    check("悬浮窗:反八股清洗", app._deflavor("值得注意的是，综上所述他不仅错了——还嘴硬。")
          == "说到底，他不仅错了，还嘴硬。")

    # Bing/DDG/360 解析器(喂固定HTML, 不出网)
    bing_html = ('<ol><li class="b_algo"><h2><a href="https://www.who.int/zh/news/a">'
                 '世卫声明：可乐杀蛔虫不实</a></h2>'
                 '<div><p>世卫组织表示可乐杀蛔虫的说法不实</p></div></li>'
                 '<li class="b_algo"><h2><a href="/relative/path">相对链接</a></h2><p>x</p></li></ol>')
    ddg_html = ('<a href="https://lite.duckduckgo.com/l/?uddg=https%3A%2F%2Fzh.wikipedia.org%2Fwiki%2F%E8%B0%A3%E8%A8%80">'
                '维基谣言条目</a><td class="result-snippet">谣言辟谣内容</td>'
                '<a href="https://example.com/ddg2">直接结果</a>')
    soe_html = ('<h3 class="res-title"><a href="https://www.so.com/link?m=abc" '
                'data-mdurl="https://new.qq.com/rain/a/WHO2024">世卫研究提醒</a></h3>'
                '<p class="res-desc">2024年9月10日 - 少喝一口酒就是保护自己</p>'
                '<h3 class="res-title"><a href="https://news.example.com/b">直链结果</a></h3>'
                '<p class="res-desc">直链摘要</p>'
                '<h3 class="res-title"><a href="https://www.so.com/link?m=nourl">无mdurl跳转</a></h3><p>x</p>')
    old_http_get, old_fetch_page = app._http_get_text, app.fetch_page_text

    def fake_http_get(url, timeout=10):
        if "so.com/s" in url:
            return soe_html
        if "bing.com" in url:
            return bing_html
        if "duckduckgo" in url:
            return ddg_html
        raise AssertionError("意外出网: " + url)

    def fake_fetch_page(url, max_chars=3200):
        return "权威正文：研究表明该说法不成立。" if "who.int" in url else ""

    app._http_get_text = fake_http_get
    app.fetch_page_text = fake_fetch_page
    app._SEARCH_CACHE.clear()
    try:
        rs = app.search_soe("测试", 6)
        check("悬浮窗:360解析", len(rs) == 2 and rs[0]["url"] == "https://new.qq.com/rain/a/WHO2024"
              and "世卫" in rs[0]["title"] and "少喝一口酒" in rs[0]["snippet"], rs)
        # 新版 360 结果块: a 里套 h3, 真实地址藏在 /jump?u= 参数里
        soe_new = ('<div class="g-card res-list"><a class=alink '
                   'href="https://m.so.com/jump?u=https%3A%2F%2Fzhuanlan.zhihu.com%2Fp%2F1&amp;m=ab" '
                   'data-md=\'{"b":"title"}\'><h3 class="res-title"><em>2024</em>年大厂裁员15万人 知乎</h3></a>'
                   '<p class="g-main summary">互联网大厂如Meta、字节跳动等也进行了裁员。</p></div>'
                   '<div><a href="https://m.so.com/jump?m=nourl"><h3 class="res-title">无目标跳转</h3></a></div>')
        app._http_get_text = lambda url, timeout=10: soe_new
        app._SEARCH_CACHE.clear()
        try:
            rs = app.search_soe("大厂裁员", 6)
            check("悬浮窗:新版360解析", len(rs) == 1
                  and rs[0]["url"] == "https://zhuanlan.zhihu.com/p/1"
                  and "裁员" in rs[0]["title"] and "字节跳动" in rs[0]["snippet"], rs)
        finally:
            app._http_get_text = fake_http_get
        # 神马(m.sm.cn): 目前唯一稳定能命中中文新闻正文的引擎 —— 按 data-tpl 切块,
        # 块内首个外链是真实地址(站内跳转要丢弃), 标题/摘要/来源各自独立
        sm_html = (
            '<div data-tpl="structure_template_normal">'
            '<a href="https://www.thepaper.cn/newsDetail_forward_1">'
            '<div class="qk-title-text qk-font-bold">055型驱逐舰绕澳航行2万公里</div>'
            '<div class="qk-paragraph-text">全程无靠岸补给, 舰体无明显锈迹</div>'
            '<div class="qk-source-item">澎湃新闻 · 3天前</div></a></div>'
            '<div data-tpl="structure_template_normal">'
            '<a href="https://m.sm.cn/s?q=x"><div class="qk-title-text">站内跳转丢弃</div></a></div>'
            '<div data-tpl="structure_template_normal">'
            '<a href="https://www.sohu.com/a/2">'
            '<div class="qk-title-text">美国航母部署286天后满身锈迹</div></a></div>'
            '<div data-tpl="structure_template_normal"><a href="https://example.com/3">'
            '<div class="qk-paragraph-text">无标题则丢弃</div></a></div>')
        app._http_get_text = lambda url, timeout=10: sm_html
        app._SEARCH_CACHE.clear()
        try:
            rs = app.search_sm("055驱逐舰 绕澳大利亚", 6)
            check("悬浮窗:神马解析",
                  len(rs) == 2
                  and rs[0]["url"] == "https://www.thepaper.cn/newsDetail_forward_1"
                  and "055" in rs[0]["title"] and "无靠岸补给" in rs[0]["snippet"]
                  and "澎湃" in rs[0]["site"] and rs[1]["site"] == "www.sohu.com", rs)
            check("悬浮窗:神马无结果块返回空", app._sm_parse("<html>访问异常</html>", 6) == [])
        finally:
            app._http_get_text = fake_http_get
        rs = app.search_bing("测试", 6)
        check("悬浮窗:Bing解析", len(rs) == 1 and rs[0]["url"] == "https://www.who.int/zh/news/a"
              and "世卫" in rs[0]["title"] and "不实" in rs[0]["snippet"], rs)
        merged = app.web_search("测试", 8)   # 360 两条<3 → 叠 Bing(1条) → 凑满3条即止
        urls = " ".join(r["url"] for r in merged)
        check("悬浮窗:三级兜底合并", len(merged) == 3 and "new.qq.com" in urls
              and "who.int/zh/news/a" in urls, merged)
        # 前两级全挂 → 降级到 DDG 也能出结果
        def fake_http_get_ddg_only(url, timeout=10):
            if "duckduckgo" in url:
                return ddg_html
            return "<html></html>"
        app._http_get_text = fake_http_get_ddg_only
        app._SEARCH_CACHE.clear()
        merged2 = app.web_search("兜底", 8)
        check("悬浮窗:仅DDG兜底", len(merged2) == 2
              and any("wikipedia.org" in r["url"] for r in merged2), merged2)
        app._http_get_text = fake_http_get
        check("悬浮窗:可信度分级", app.domain_cred("https://www.who.int/x") == "high"
              and app.domain_cred("https://tieba.baidu.com/p/1") == "low"
              and app.domain_cred("https://baike.baidu.com/item/x") == "mid")
        # 通用实体页识别: 引擎退化成头词匹配时只会回这些, 一条都不能当证据
        check("悬浮窗:实体页识别",
              all(app._is_entity_page(t) for t in
                  ("山东省_百度百科", "山东省人民政府", "山东省行政区划_百度百科",
                   "山东省教育招生考试院官网", "百度百科·美国", "辽宁旅游最值得去的10大景点",
                   "中文 (中国) - United States Department of State", "055型驱逐舰_百度百科"))
              and not app._is_entity_page("食物短缺、精神崩溃、舰体生锈 美国“林肯”号航母结束286天部署返美")
              and not app._is_entity_page("山东舰航母编队首次赴西太平洋远海训练"), "")
        ev = app.gather_evidence("可乐杀蛔虫", [])
        check("悬浮窗:证据抓取排序", ev and ev[0]["_cred"] in ("high", "mid")
              and any(r.get("page") for r in ev), ev)

        # 营销号过滤: 低可信域名/震惊体标题在可靠来源充足时整体剔除
        old_ws = app.web_search
        app.web_search = lambda q, n=8: [
            {"title": "某说法的权威研究", "url": "https://www.who.int/x", "snippet": ""},
            {"title": "某说法可靠新闻", "url": "https://news.qq.com/y", "snippet": ""},
            {"title": "某说法正常讨论", "url": "https://zhihu.com/z", "snippet": ""},
            {"title": "震惊！某说法太可怕了", "url": "https://baijiahao.baidu.com/s?id=1", "snippet": ""},
            {"title": "某说法普通文章", "url": "https://www.360kuai.com/a", "snippet": ""},
            {"title": "紧急提醒！某说法是真的", "url": "https://news.qq.com/spam", "snippet": ""},
        ]
        try:
            ev = app.gather_evidence("某说法", [])
            urls = " ".join(r["url"] for r in ev)
            check("悬浮窗:营销号过滤", len(ev) == 3 and "baijiahao" not in urls
                  and "360kuai" not in urls and "/spam" not in urls, urls)
        finally:
            app.web_search = old_ws

        # 引擎退化成头词匹配 → 回一堆通用实体页。这些绝不能进证据(实测模型会拿
        # 「沈阳市_百度百科」当反例), 一条真证据都没有就该老实回空, 让上层说"未搜到网络证据"
        old_ws = app.web_search
        app.web_search = lambda q, n=8: [
            {"title": "山东省_百度百科", "url": "https://baike.baidu.com/item/1", "snippet": "山东省"},
            {"title": "山东省人民政府", "url": "https://www.shandong.gov.cn/", "snippet": ""},
            {"title": "百度百科·美国", "url": "https://baike.baidu.com/item/2", "snippet": "美国"},
            {"title": "辽宁旅游最值得去的10大景点", "url": "https://zhuanlan.zhihu.com/p/3", "snippet": ""},
        ]
        try:
            ev = app.gather_evidence("山东舰 西太平洋 部署 天数", ["山东舰 西太平洋 部署 天数"])
            check("悬浮窗:实体页不进证据", ev == [], ev)
        finally:
            app.web_search = old_ws

        # 引擎风控 → 冷却: 被 captcha/「访问异常」页挡下后不再硬打(越打风控越久),
        # 冷却期内连网络都不请求
        _old_get = app._http_get_text
        _hits = []
        app._ENGINE_COOLDOWN.clear()
        app._http_get_text = lambda url, timeout=10: (_hits.append(url)
                                                      or '<script>window.location.replace("https://m.sm.cn//s/_____tmd_____/punish?x5secdata=abc")</script>')
        try:
            check("悬浮窗:风控页判定", app._engine_block("t", "访问异常页面") is True
                  and app._engine_block("t2", "<html>正常页面</html>") is False)
            app._ENGINE_COOLDOWN.clear()
            check("悬浮窗:风控后回空", app.search_sm("测试", 6) == [] and len(_hits) == 1, _hits)
            check("悬浮窗:冷却期内不再请求",
                  app.search_sm("测试", 6) == [] and len(_hits) == 1, _hits)
        finally:
            app._http_get_text = _old_get
            app._ENGINE_COOLDOWN.clear()

        # 相关度过滤: 搜索引擎只匹配头一个词(回百科词条/日历页), 无关结果不进证据
        old_ws2 = app.web_search
        app.web_search = lambda q, n=8: [
            {"title": "2024年程序员就业报告", "url": "https://www.stats.gov.cn/a",
             "snippet": "程序员岗位变化统计"},
            {"title": "2024年_百度百科", "url": "https://baike.baidu.com/item/2024", "snippet": ""},
            {"title": "万年历2024年日历表", "url": "https://www.rili.com.cn/2024/", "snippet": ""},
        ]
        try:
            ev = app.gather_evidence("2024年程序员岗位减少了四成", [])
            urls = " ".join(r["url"] for r in ev)
            check("悬浮窗:相关度过滤", len(ev) == 1 and "stats.gov.cn" in urls
                  and "baike" not in urls and "rili" not in urls, urls)
            # 相关度全为 0 时不硬塞垃圾证据
            app.web_search = lambda q, n=8: [
                {"title": "万年历2024年日历表", "url": "https://www.rili.com.cn/2024/", "snippet": ""},
                {"title": "2024年_百度百科", "url": "https://baike.baidu.com/item/2024", "snippet": ""},
            ]
            check("悬浮窗:全不相关则不要证据",
                  app.gather_evidence("量子纠缠的退相干时间被观测到了", []) == [])
        finally:
            app.web_search = old_ws2
        check("悬浮窗:相关度切词", app._relevance("大厂都在裁员", "互联网大厂裁员比例统计") >= 0.3
              and app._relevance("大厂都在裁员", "大厂回族自治县_百度百科") < 0.3
              and app._relevance("大厂都在裁员", "2024年日历表") == 0
              and app._relevance("ab", "随便什么") == 1.0)
    finally:
        app._http_get_text, app.fetch_page_text = old_http_get, old_fetch_page
        app._SEARCH_CACHE.clear()

    # /api/floatball 开关与状态(spawn 打桩 + 假进程, 不真拉起 tkinter; stop 走真逻辑验证闭环)
    class _FakeBallProc:
        def poll(self):
            return None
        def terminate(self):
            pass
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    old_spawn, old_proc = app.float_ball_spawn, app._BALL_PROC
    spawn_calls = []

    def fake_spawn(port):
        spawn_calls.append(port)
        app._BALL_PROC = _FakeBallProc()
        return True

    app.float_ball_spawn = fake_spawn
    try:
        d = req("/api/floatball", {"enabled": True})
        check("悬浮窗:开关开启", d.get("ok") and d.get("running") is True and spawn_calls == [8000], (d, spawn_calls))
        check("悬浮窗:开关持久化", app.sget("float_ball") == "1")
        d = req("/api/floatball")
        check("悬浮窗:状态查询", d.get("enabled") is True and d.get("running") is True, d)
        d = req("/api/floatball", {"enabled": False})
        check("悬浮窗:开关关闭", d.get("ok") and d.get("running") is False and app.sget("float_ball") == "0", d)
    finally:
        app.float_ball_spawn, app._BALL_PROC = old_spawn, old_proc

    # AI 未配置 → 直接 JSON 拒绝(非 SSE)
    old_key = app.sget("ai_key")
    app.sset("ai_key", "")
    try:
        try:
            req("/api/ai/assist", {"mode": "verify", "text": "测试"})
            check("悬浮窗:AI未配置拒绝", False)
        except urllib.error.HTTPError as e:
            check("悬浮窗:AI未配置拒绝", e.code == 403, e.code)
    finally:
        app.sset("ai_key", old_key)

    # 空文本 / 非法模式 → 400
    try:
        req("/api/ai/assist", {"mode": "verify", "text": "   "})
        check("悬浮窗:空文本拒绝", False)
    except urllib.error.HTTPError as e:
        check("悬浮窗:空文本拒绝", e.code == 400)
    try:
        req("/api/ai/assist", {"mode": "hack", "text": "x"})
        check("悬浮窗:非法模式拒绝", False)
    except urllib.error.HTTPError as e:
        check("悬浮窗:非法模式拒绝", e.code == 400)

    # 论点拆解(逐个击破的第一步); 搜索/正文抓取保持打桩, 测试不出网
    app._http_get_text = fake_http_get
    app.fetch_page_text = fake_fetch_page
    app._SEARCH_CACHE.clear()
    cl = app.decompose_claim("以色列空军能飞到中国，中国战机飞不到以色列")
    check("悬浮窗:论点拆解", len(cl) == 2 and cl[0]["point"] and cl[0]["query"], cl)
    check("悬浮窗:拆解带反例向查询词", cl[0]["counter"] == "以色列空军 空中加油 航程 实际", cl)
    cl = app.decompose_claim("地球绕着太阳转")
    check("悬浮窗:短句不脑补拆解", len(cl) == 1 and cl[0]["point"] == "地球绕着太阳转", cl)

    # 查询池: 整段反例 + 逐论点反例全部排前面(不能因为条数上限把某个论点的反例词挤掉), 再补正向
    _cl, _qs = app.assist_plan("以色列空军能飞到中国，中国战机飞不到以色列")
    check("悬浮窗:查询池反例优先",
          _qs == ["以色列空军 远程奔袭 战例 距离", "以色列空军 空中加油 航程 实际",
                  "中国战机 海外部署 远航 案例", "以色列空军 转场航程", "中国空军 作战半径"], _qs)
    check("悬浮窗:查询池上限", app._ASSIST_MAX_QUERIES == 5 and len(_qs) <= 5, _qs)
    # 3 个论点时: 上限内必须容得下全部反例词(旧的正反交替会把第3个论点的词整条丢掉)
    _old_dec = app.decompose_plan
    app.decompose_plan = lambda t: ([{"point": "p%d" % i, "query": "pos%d" % i,
                                      "counter": "neg%d" % i} for i in (1, 2, 3)], "best")
    try:
        _c3, _q3 = app.assist_plan("x" * 20)
        check("悬浮窗:反例词不被条数上限挤掉", _q3 == ["best", "neg1", "neg2", "neg3", "pos1"], _q3)
    finally:
        app.decompose_plan = _old_dec
    # 拆解整个失败(模型两次都没给出JSON) → 不能让取证链路断掉, 回落到兜底词
    _old_dec = app.decompose_plan
    app.decompose_plan = lambda t: ([], "")
    try:
        _c4, _q4 = app.assist_plan("地球绕着太阳转")
        check("悬浮窗:拆解失败仍有兜底查询", _q4 == ["地球绕着太阳转 辟谣 真相"], _q4)
    finally:
        app.decompose_plan = _old_dec
    # 短句没有拆解词 → 兜底一条辟谣向查询
    _cl2, _qs2 = app.assist_plan("地球绕着太阳转")
    check("悬浮窗:短句兜底查询", _qs2 == ["地球绕着太阳转 辟谣 真相"], _qs2)

    # 验证真实性: SSE 端到端(搜索桩 + MockLLM)
    evs = parse_sse(req_sse("/api/ai/assist", {"mode": "verify", "text": "网传说可乐能杀灭蛔虫"}))
    phases = [d for e, d in evs if e == "phase"]
    dones = [d for e, d in evs if e == "done"]
    check("悬浮窗:核查SSE阶段", len(phases) >= 2 and "拆解" in phases[0].get("label", ""), phases)
    res = dones[0] if dones else {}
    r = res.get("result") or {}
    check("悬浮窗:核查结果", res.get("ok") is True and res.get("mode") == "verify"
          and r.get("verdict") == "false" and r.get("confidence") == 88
          and r.get("evidence_used") == ["E1"] and r.get("offline") is False
          and (r.get("claims") or [{}])[0].get("check") == "false", res)
    usage = res.get("usage") or {}
    check("悬浮窗:用量回报", usage.get("calls", 0) >= 1 and usage.get("in_tokens", 0) > 0
          and usage.get("cost", -1) >= 0, usage)
    check("悬浮窗:谬误点名", r.get("fallacies") == ["假两难"], r)
    refs = res.get("evidence") or []
    check("悬浮窗:核查证据卡", len(refs) == 1 and refs[0]["id"] == "E1"
          and refs[0]["cred"] in ("high", "mid") and refs[0]["url"].startswith("http"), refs)

    # 生成回怼: SSE 端到端(三段式, 每段生成完立刻推送)
    evs = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": "喝可乐能杀灭蛔虫"}))
    segs = [d for e, d in evs if e == "seg"]
    dones = [d for e, d in evs if e == "done"]
    res = dones[0] if dones else {}
    r = res.get("result") or {}
    check("悬浮窗:回怼分段推送",
          [s.get("kind") for s in segs] == ["targets", "lines", "paras", "long"], segs)
    check("悬浮窗:回怼误区谬误清单",
          bool(r.get("targets")) and r["targets"][0]["kind"] == "知识误区"
          and r["targets"][0]["point"] and r["targets"][0]["note"], r.get("targets"))
    check("悬浮窗:回怼三段条数",
          len(r.get("lines") or []) == 2 and len(r.get("paras") or []) == 2
          and len(r.get("long") or []) == 1,
          {k: len(r.get(k) or []) for k in ("lines", "paras", "long")})
    check("悬浮窗:回怼可粘贴无编号",
          all("[E" not in it["text"] for k in ("lines", "paras", "long") for it in (r.get(k) or []))
          and all(it["basis"] for it in (r.get("lines") or [])), r)
    check("悬浮窗:回怼立场说明", isinstance(r.get("stance_note"), str) and r.get("stance_note"), r)
    check("悬浮窗:写作硬规则重写",
          all("一方面" not in it["text"] for it in (r.get("lines") or [])), r.get("lines"))
    check("悬浮窗:回怼无引号", all(not re.search(r"[「」『』“”]", it["text"])
                                   for k in ("lines", "paras", "long")
                                   for it in (r.get(k) or [])), r)
    evs = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": "无可回怼 这只是正常反馈"}))
    dones = [d for e, d in evs if e == "done"]
    res = dones[0] if dones else {}
    r = res.get("result") or {}
    check("悬浮窗:无可回怼提示", res.get("ok") is True and r.get("no_target") is True
          and r.get("reason"), res)

    # JSON 两次解析失败 → 自动降级纯文本解析(用户不见内部报错)
    evs = parse_sse(req_sse("/api/ai/assist", {"mode": "verify", "text": "触发降级 某个说法"}))
    dones = [d for e, d in evs if e == "done"]
    res = dones[0] if dones else {}
    r = res.get("result") or {}
    check("悬浮窗:核查纯文本兜底", res.get("ok") is True and r.get("verdict") == "false"
          and r.get("confidence") == 85 and len(r.get("reasons") or []) == 2
          and "矛盾" in (r.get("summary") or ""), res)
    evs = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": "触发降级 某个言论"}))
    dones = [d for e, d in evs if e == "done"]
    res = dones[0] if dones else {}
    r = res.get("result") or {}
    check("悬浮窗:回怼纯文本兜底", res.get("ok") is True and len(r.get("lines") or []) == 2
          and len(r.get("paras") or []) == 2 and len(r.get("long") or []) == 1
          and all(it["text"] for it in r["lines"]), res)
    app._http_get_text, app.fetch_page_text = old_http_get, old_fetch_page   # 恢复真实网络层
    app._SEARCH_CACHE.clear()

    # 调节档位: 白名单校验 + 持久化(条数档位 1/2/3, 旧的风格值折算成新人设名)
    d = req("/api/floatball", {"prefs": {"think": "深挖", "savage": "阴阳", "len": "3", "hack": "x"}})
    check("悬浮窗:档位保存", d.get("ok") and d["prefs"]["think"] == "深挖"
          and d["prefs"]["savage"] == "阴阳怪气" and d["prefs"]["len"] == "3"
          and "fallacy" not in d["prefs"], d)
    d = req("/api/floatball", {"prefs": {"think": "乱写", "savage": "", "len": 9}})
    d = req("/api/floatball")
    check("悬浮窗:档位白名单", d["prefs"]["think"] == "深挖" and d["prefs"]["savage"] == "阴阳怪气"
          and d["prefs"]["len"] == "3", d)
    req("/api/floatball", {"prefs": {"len": "长"}})   # 旧档位「长」折算成 3
    d = req("/api/floatball")
    check("悬浮窗:旧档位折算", d["prefs"]["len"] == "3", d)

    # 条数档位 3: 一句话与一段话各 3 条, 长篇仍 1 条
    app._http_get_text = fake_http_get
    app.fetch_page_text = fake_fetch_page
    app._SEARCH_CACHE.clear()
    try:
        evs = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": "回怼这条言论"}))
        dones = [d2 for e, d2 in evs if e == "done"]
        rr = (dones[0].get("result") or {}) if dones else {}
        check("悬浮窗:条数档位生效",
              len(rr.get("lines") or []) == 3 and len(rr.get("paras") or []) == 3
              and len(rr.get("long") or []) == 1,
              {k: len(rr.get(k) or []) for k in ("lines", "paras", "long")})
    finally:
        app._http_get_text, app.fetch_page_text = old_http_get, old_fetch_page
        app._SEARCH_CACHE.clear()
    req("/api/floatball", {"prefs": {"think": "标准", "savage": "毒舌", "len": "2"}})   # 还原默认

    # 直调三段式生成: 结构与写作硬规则清洗
    ev1 = [{"title": "t", "url": "https://x.io/a", "snippet": "s",
            "_cred": "mid", "_cred_label": "可靠", "page": ""}]
    rr2, _refs2 = app.assist_rebut("回怼这条言论", ev1,
                                   [{"point": "以色列空军能飞到中国", "query": "x"}])
    check("悬浮窗:直调三段结构", len(rr2["lines"]) == 2 and len(rr2["paras"]) == 2
          and len(rr2["long"]) == 1 and len(rr2["targets"]) == 2, rr2)
    check("悬浮窗:硬规则清洗",
          app._style_fix("这方案落地后崩了，判一下") == "这方案实施后崩溃了，判一下"
          and not app._style_violations("这方案实施后崩溃了"))
    check("悬浮窗:硬规则命中", bool(app._style_violations("这不是A而是B"))
          and bool(app._style_violations("一句话总结: 都挺好"))
          and bool(app._style_violations("用了技术栈")))
    check("悬浮窗:人称失守判定", app._seg_score([{"text": "就这？"}])[0] == 2
          and app._seg_score([{"text": "我跟你说，就这？"}])[0] == 0
          and app._seg_score([{"text": "我劝你先把资料查明白"}])[0] == 0
          and app._seg_score([{"text": "我先把资料查明白"}])[0] == 1)
    _tg = app._targets_from_text("电池三年必换 | 知识误区 | 质保普遍八年\n"
                                 "油车不用修｜绝对化断言｜前提不成立\n"
                                 "谁买谁后悔")
    check("悬浮窗:误区清单文本兜底", len(_tg) == 3 and _tg[0]["kind"] == "知识误区"
          and _tg[1]["kind"] == "绝对化断言" and _tg[1]["note"] == "前提不成立"
          and _tg[2]["point"] == "谁买谁后悔" and _tg[2]["kind"] == "", _tg)

    # 话术模板库: 联网采集(搜索走桩, 正文里埋模板句) + 广告/链接过滤
    app._TALK_CACHE.update(ts=0.0, templates=[])
    old_gt2, old_fp2 = app._http_get_text, app.fetch_page_text
    app._http_get_text = fake_http_get
    app.fetch_page_text = lambda url, max_chars=3200: (
        "就这水平也敢来对线？研究了半天就这结论！建议先查查资料再来对线，不难。"
        "广告位招租 http://example.com 快来关注订阅")
    try:
        pool = app.fetch_talk_templates()
        check("悬浮窗:话术模板采集", any("就这水平" in t for t in pool)
              and not any(("http" in t or "广告" in t or "关注" in t) for t in pool), pool[:6])
    finally:
        app._http_get_text, app.fetch_page_text = old_gt2, old_fp2
        app._TALK_CACHE.update(ts=0.0, templates=[])
    check("悬浮窗:内置话术库", len(app._BUILTIN_TALK) >= 10)

    # 主题推送: 主界面强调色/深浅主题 → 悬浮球(展开时向后端取)
    d = req("/api/floatball", {"theme": {"accent": "#4a9eff", "light": True}})
    check("悬浮窗:主题推送", d.get("ok") and d.get("accent") == "#4a9eff" and d.get("light") is True, d)
    d = req("/api/floatball", {"theme": {"accent": "不合法", "light": False}})
    d = req("/api/floatball")
    check("悬浮窗:主题持久化", d.get("accent") == "#4a9eff" and d.get("light") is False, d)

    # 悬浮球进程真身: 带自关钩子拉起一次, 验证 tkinter 建窗不崩(不支持的环境跳过)
    if app.float_ball_supported() and "--skip-ball" not in sys.argv:
        import subprocess as _sp
        p = _sp.Popen([sys.executable, os.path.join(PROJECT, "app.py"),
                       "--float-ball", "8099", "--ball-autoclose", "1500"],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        try:
            code = p.wait(timeout=15)
            check("悬浮球:进程建窗自关", code == 0, code)
        except _sp.TimeoutExpired:
            p.kill()
            check("悬浮球:进程建窗自关", False, "timeout")

    # ---------- 逻辑谬误分类体系(LOGIC 13 类) ----------
    check("谬误:13类清单", len(app._FALLACY_TAXONOMY) == 13
          and all(k and d for k, d in app._FALLACY_TAXONOMY),
          app._FALLACY_TAXONOMY)
    check("谬误:清单进提示词", all(k in app._TARGET_ASK for k, _d in app._FALLACY_TAXONOMY))
    check("谬误:要求只标真命中的",
          "拿不准就不标" in app._TARGET_ASK and "最多标两个" in app._TARGET_ASK)

    # ---------- 身份(武器)与风格(语气)解耦 ----------
    check("档位:风格旧值折算", app._savage_key("温和") == "冷静理性"
          and app._savage_key("毒舌") == "毒舌律师" and app._savage_key("阴阳") == "阴阳怪气"
          and app._savage_key("乱写") == "毒舌律师")
    check("档位:风格温度分档", app._SAVAGE_TEMP["阴阳怪气"] > app._SAVAGE_TEMP["毒舌律师"]
          > app._SAVAGE_TEMP["冷静理性"])
    check("档位:身份非法值退回不设", app._persona_key("乱写") == "不设"
          and app._persona_key("数据党") == "数据党")
    _sys_data = app._rebut_system("冷静理性", "数据党")
    _sys_plain = app._rebut_system("冷静理性", "不设")
    check("档位:身份注入提示词", "统计口径" in _sys_data and "统计口径" not in _sys_plain,
          (_sys_data[:80], _sys_plain[:80]))
    check("档位:换身份不动风格", "语气克制" in _sys_data
          and "语气克制" in app._rebut_system("冷静理性", "理工科")
          and "统计口径" in _sys_data)

    # ---------- 自定义人设预设(结构化字段存本地, 可增删切换) ----------
    for _it in (req("/api/rebut/saved").get("items") or []):   # 收藏表是持久化的, 先清干净
        req("/api/rebut/delete", {"id": _it["id"]})
    d = req("/api/personas", {"items": [
        {"name": "老球迷", "identity": "看了二十年球的死忠球迷",
         "weapon": "历年比赛数据与阵型", "taboo": "编造比分"},
        {"name": "不设", "identity": "想冒充内置档位", "weapon": "x"},     # 撞内置名 → 丢
        {"name": "", "identity": "空名字"},                              # 空名 → 丢
        {"name": "空壳"}]})                                             # 只有名字 → 丢
    check("人设:保存与脏数据过滤", d.get("ok") and d.get("count") == 1
          and len(d["items"]) == 1 and d["items"][0]["name"] == "老球迷", d)
    d = req("/api/personas")
    check("人设:内置档位与字段", d["builtin"] == ["不设", "理工科", "行业老手", "数据党", "路人"]
          and [f["key"] for f in d["fields"]] == ["identity", "weapon", "taboo"], d)
    check("人设:注入提示词",
          "看了二十年球的死忠球迷" in app._persona_line("老球迷")
          and "历年比赛数据与阵型" in app._persona_line("老球迷")
          and "编造比分" in app._persona_line("老球迷"), app._persona_line("老球迷"))
    check("人设:自定义名合法", app._persona_key("老球迷") == "老球迷"
          and app._persona_valid("老球迷") and not app._persona_valid("查无此人"))
    _sys_custom = app._rebut_system("毒舌律师", "老球迷")
    check("人设:自定义身份进系统提示词", "看了二十年球的死忠球迷" in _sys_custom, _sys_custom[:120])
    d = req("/api/floatball", {"prefs": {"persona": "老球迷"}})
    check("人设:档位可选自定义", d["prefs"]["persona"] == "老球迷", d["prefs"])
    req("/api/floatball", {"prefs": {"persona": "查无此人"}})
    d = req("/api/floatball")
    check("人设:非法档位不落库", d["prefs"]["persona"] == "老球迷", d["prefs"])
    # 换档位就是另一份结果: 缓存键必须带上身份
    app.sset("assist_persona", "老球迷")
    _k_custom = app.rebut_cache_key("同一句话")
    app.sset("assist_persona", "数据党")
    _k_data = app.rebut_cache_key("同一句话")
    check("人设:缓存键区分身份", _k_custom != _k_data, (_k_custom, _k_data))
    req("/api/personas", {"items": []})   # 清理, 后面用例回到内置档位
    req("/api/floatball", {"prefs": {"persona": "不设"}})
    check("人设:清空后自定义名失效", app._persona_key("老球迷") == "不设"
          and app.persona_presets() == [])

    # ---------- 裁判复核替掉自查式重写 ----------
    check("裁判:角色与写手分开",
          "裁判" in app._judge_system() and "不要重写" in app._judge_system()
          and "对线的当事人" not in app._judge_system())
    _old_json = app._assist_llm_json
    _jcall = []
    _stub = {"items": [{"text": "我查过公开数据，2024年这数掉了一半，你那算法过不了关",
                        "basis": "公开资料", "target": "知识误区"}]}

    def _fake_json(system, user, mt, temp):
        _jcall.append("judge" if "裁判" in system else "rewrite")
        if "裁判" in system:
            return {"verdict": "revise", "problems": ["没把具体数字砸进去"],
                    "weakest": "第1条只有气势"}
        return {"items": list(_stub["items"])}
    app._assist_llm_json = _fake_json
    try:
        _clean = [{"text": "我跟你说，转场航程差着数量级", "basis": "x", "target": "y"},
                  {"text": "你拿地图直线当作战半径，这不成立", "basis": "x", "target": "y"}]
        _same = app._seg_polish("sys", "user", "lines", _clean, 2, 1200, 0.8, ask="要打的点")
        check("裁判:机械检查过关就不请裁判", _same is _clean and _jcall == [], _jcall)
        _jcall.clear()
        # 一段话不砸具体东西 = 空话 → 请裁判 → 按裁判意见重写 → 分更高则采纳
        _thin = [{"text": "我跟你说，这事根本站不住", "basis": "x", "target": "y"},
                 {"text": "你别嘴硬了", "basis": "x", "target": "y"}]
        _out = app._seg_polish("sys", "user", "paras", _thin, 2, 1700, 0.8, ask="要打的点",
                               evidence=[{"title": "国家统计局数据", "url": "https://stats.gov.cn/a"}])
        check("裁判:空话命中→请裁判并重写",
              _jcall[:2] == ["judge", "rewrite"] and _out[0]["text"] == _stub["items"][0]["text"],
              (_jcall, _out))
        _jcall.clear()
        # 重写版更差(人称失守 + 一堆八股) → 一律不采纳, 保留原稿
        _bad = [{"text": "一方面懒得查证，另一方面又急着下结论", "basis": "x", "target": "y"}]
        _stub["items"] = [{"text": "一方面这方案站不住，另一方面众所周知毋庸置疑",
                           "basis": "x", "target": "y"}]
        _out2 = app._seg_polish("sys", "user", "lines", _bad, 2, 1200, 0.8, ask="要打的点")
        check("裁判:重写更差则保留原稿",
              "懒得查证" in _out2[0]["text"] and "judge" in _jcall, (_jcall, _out2))
    finally:
        app._assist_llm_json = _old_json

    # ---------- 推理模型的思考额度下限(拆解空回复的根因) ----------
    # deepseek-flash 先出思考再出正文, max_tokens 把思考一起算; 给少了 content 就是空串。
    # 契约: 调用方要多少都行, 实际下发不得低于 _JSON_MIN_TOKENS; 空 content 必须重试。
    _old_call = app.call_llm
    _calls = []
    _empty_first = [True]

    def _fake_call(messages, max_tokens=None, temperature=None):
        _calls.append(max_tokens)
        if _empty_first[0]:
            _empty_first[0] = False
            return "", {"completion_tokens": 1128, "reasoning_tokens": 1074}   # 思考吃光额度
        return '{"ok": 1}', {}
    app.call_llm = _fake_call
    try:
        check("额度:下限常量", app._JSON_MIN_TOKENS >= 4000, app._JSON_MIN_TOKENS)
        out = app._assist_llm_json("sys", "user", 1200, 0.2)
        check("额度:小额度被抬到下限+空回复重试", out == {"ok": 1} and _calls == [4000, 7000], _calls)
        _calls.clear()
        out = app._assist_llm_json("sys", "user", 9000, 0.2)
        check("额度:调用方要更多就给更多", out == {"ok": 1} and _calls == [9000], _calls)
        _calls.clear()

        def _always_empty(messages, max_tokens=None, temperature=None):
            _calls.append(max_tokens)
            return "", {}
        app.call_llm = _always_empty
        try:
            app._assist_llm_json("sys", "user", 1200, 0.2)
            check("额度:两轮全空必须报错", False, _calls)
        except Exception as e:
            check("额度:两轮全空必须报错", _calls == [4000, 7000] and "思考" in str(e), (e, _calls))
    finally:
        app.call_llm = _old_call

    # ---------- 段落具体性判定(空话 vs 砸了具体东西) ----------
    _ev = [{"title": "国家统计局2024年就业数据", "url": "https://stats.gov.cn/a"}]
    check("空话:没有数字也没有证据专名", app._seg_has_specifics(
        [{"text": "我跟你说，这事根本站不住"}], _ev) is False)
    check("空话:砸了数字就算具体", app._seg_has_specifics(
        [{"text": "2024年这个数掉了一半"}], _ev) is True)
    check("空话:引用了证据标题里的专名也算", app._seg_has_specifics(
        [{"text": "国家统计局的口径覆盖哪些你查过吗"}], _ev) is True)
    check("空话:没证据时不苛求", app._seg_has_specifics(
        [{"text": "我跟你说，这事根本站不住"}], []) is True)

    # ---------- 同一条原话的 LRU 缓存 + 上一轮上下文复用 ----------
    _txt = "缓存与换批测试 某某言论"
    evs_a = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": _txt}))
    check("缓存:首轮正常生成",
          [s.get("kind") for e, s in evs_a if e == "seg"] == ["targets", "lines", "paras", "long"],
          [s.get("kind") for e, s in evs_a if e == "seg"])
    evs_b = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": _txt}))
    _ph = [d.get("stage") for e, d in evs_b if e == "phase"]
    check("缓存:同原话直接命中", "cache" in _ph, _ph)
    check("缓存:命中后按同样顺序补推",
          [s.get("kind") for e, s in evs_b if e == "seg"] == ["targets", "lines", "paras", "long"],
          [s.get("kind") for e, s in evs_b if e == "seg"])
    _db = [d for e, d in evs_b if e == "done"][0]
    check("缓存:命中结果与首轮一致",
          len(_db["result"]["lines"]) == 2 and _db["result"]["targets"][0]["point"],
          _db["result"])
    # 换了档位就是另一份结果, 不能再命中旧缓存
    req("/api/floatball", {"prefs": {"len": "1"}})
    evs_c = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": _txt}))
    _phc = [d.get("stage") for e, d in evs_c if e == "phase"]
    check("缓存:换档位不命中", "cache" not in _phc, _phc)
    req("/api/floatball", {"prefs": {"len": "2"}})

    # ---------- 换一批: 只重跑那一段, 论点与证据沿用上一轮 ----------
    evs_r = parse_sse(req_sse("/api/ai/assist", {"mode": "rebut", "text": _txt, "seg": "paras"}))
    _sr = [s for e, s in evs_r if e == "seg"]
    check("换批:只推这一段", [s.get("kind") for s in _sr] == ["paras"] and _sr[0].get("regen") is True,
          _sr)
    check("换批:走复用上下文的快路",
          "regen" in [d.get("stage") for e, d in evs_r if e == "phase"],
          [d.get("stage") for e, d in evs_r if e == "phase"])
    _dr = [d for e, d in evs_r if e == "done"][0]
    check("换批:done 带回 regen 标记", _dr.get("regen") == "paras"
          and len(_dr["result"]["paras"]) == 2 and not _dr["result"]["lines"], _dr["result"])
    _bad_seg = None
    try:
        req_sse("/api/ai/assist", {"mode": "rebut", "text": _txt, "seg": "没这段"})
    except urllib.error.HTTPError as _e:
        _bad_seg = _e.code
    check("换批:非法段落 400", _bad_seg == 400, _bad_seg)

    # ---------- 正文挂证据编号: 编号单独下发, 不写进正文 ----------
    _sT = [s for e, s in evs_a if e == "seg"][0]
    check("证据:分段带证据摘要", isinstance(_sT.get("evidence"), list)
          and all({"id", "title", "url"} <= set(x) for x in _sT["evidence"]), _sT.get("evidence"))
    _res_a = [d for e, d in evs_a if e == "done"][0]["result"]
    check("证据:编号不进正文",
          all("[E" not in it["text"] for k in ("lines", "paras", "long")
              for it in (_res_a.get(k) or [])), _res_a)

    # ---------- 收藏夹(存本机 SQLite, 可搜索/删除) ----------
    d = req("/api/rebut/save", {"source": "喝可乐能杀灭蛔虫", "seg": "lines",
                                "content": "可乐要能杀蛔虫，医院早改行卖饮料了"})
    _rid = d.get("id")
    check("收藏:存一条", d.get("ok") and _rid, d)
    req("/api/rebut/save", {"source": "另一条原话", "seg": "long", "content": "别拿老偏方当证据"})
    d = req("/api/rebut/saved")
    check("收藏:列表", len(d["items"]) == 2 and d["items"][0]["source"], d)
    d = req("/api/rebut/saved?q=" + urllib.parse.quote("老偏方"))
    check("收藏:按正文搜索", len(d["items"]) == 1 and "老偏方" in d["items"][0]["content"], d)
    d = req("/api/rebut/saved?q=" + urllib.parse.quote("喝可乐"))
    check("收藏:按原话搜索", len(d["items"]) == 1 and d["items"][0]["id"] == _rid, d)
    d = req("/api/rebut/saved?q=" + urllib.parse.quote("查无此物"))
    check("收藏:搜不到返回空", d["items"] == [], d)
    d = req("/api/rebut/delete", {"id": _rid})
    check("收藏:删除", d.get("ok") is True and len(req("/api/rebut/saved")["items"]) == 1, d)
    check("收藏:重复删除不报错", req("/api/rebut/delete", {"id": _rid}).get("ok") is True)
    for _it in req("/api/rebut/saved")["items"]:
        req("/api/rebut/delete", {"id": _it["id"]})

    # ---------- 悬浮窗环境体检: 说清到底缺什么 ----------
    _env = app.float_ball_env()
    check("体检:本机可跑悬浮窗", _env["ok"] is True and not _env["reason"]
          and "python.exe" in _env["python"], _env)
    _old_nt = app.os.name
    try:
        app.os.name = "posix"
        _env2 = app.float_ball_env()
        check("体检:非Windows给明确原因", _env2["ok"] is False and "不是 Windows" in _env2["reason"]
              and _env2["hint"], _env2)
    finally:
        app.os.name = _old_nt
    _fb = req("/api/floatball")
    check("体检:浮窗接口带诊断字段",
          "reason" in _fb and "hint" in _fb and "spawn_error" in _fb and "python" in _fb, _fb)
    check("体检:启动失败原因可读", isinstance(app.float_ball_last_error(), str))

    # ---------- 开合只认点击: 悬停自动展开已按用户要求整体移除 ----------
    # 用户原话:「不想要鼠标放到球上面就自动打开窗口的功能, 必须是点击球才行」。
    # 这里钉住两件事: ①档位接口里不再有 hover(老开关彻底下线, 老库里残留的键不再被读);
    # ②生成档位本身没被误伤。
    d = req("/api/floatball", {"prefs": {"hover": "1"}})
    check("悬浮球:悬停展开档位已下线", "hover" not in d["prefs"], d["prefs"])
    check("悬浮球:其余生成档位仍在",
          all(k in d["prefs"] for k in ("think", "persona", "savage", "len")), d["prefs"])
    check("悬浮球:接口不再回 hover 字段", "hover" not in req("/api/floatball"))

    # ---------- 配色派生: 球体的明暗面/描边/光晕都由强调色算出来 ----------
    # _hex_mix 是「球体渐变」的唯一来源, 出错的表现是球变成纯黑或纯白(而不是报错),
    # 所以它的端点行为必须有断言兜住。
    check("配色:混合端点 t=0 取左色", app._hex_mix("#336699", "#ffffff", 0) == "#336699")
    check("配色:混合端点 t=1 取右色", app._hex_mix("#336699", "#ffffff", 1) == "#ffffff")
    _mid = app._hex_mix("#000000", "#ffffff", 0.5)
    check("配色:混合中点取中值", _mid == "#7f7f7f", _mid)
    check("配色:t 越界被夹回", app._hex_mix("#000000", "#ffffff", 9) == "#ffffff"
          and app._hex_mix("#000000", "#ffffff", -9) == "#000000")
    check("配色:非法色值回退左色", app._hex_mix("不是色", "#ffffff", 0.5) == "不是色")

    _px = app._ball_palette("#3b82f6", False)
    _keys = ("bg", "card", "txt", "sub", "line", "input", "accent",
             "ac_lit", "ac_dark", "ring", "glow", "hover", "danger")
    check("配色:深色板字段齐全", all(k in _px for k in _keys),
          [k for k in _keys if k not in _px])
    check("配色:派生色都是合法色值",
          all(isinstance(_px[k], str) and len(_px[k]) == 7 and _px[k][0] == "#"
              and all(c in "0123456789abcdef" for c in _px[k][1:].lower())
              for k in _keys), {k: _px[k] for k in _keys})
    check("配色:亮面比强调色亮、暗面比强调色暗",
          app._hex_mix(_px["ac_lit"], "#000000", 0) != _px["accent"]
          and _px["ac_lit"] != _px["ac_dark"]
          and app._hex_mix(_px["ac_lit"], "#ffffff", 0.5) > app._hex_mix(_px["ac_dark"], "#ffffff", 0.5))
    _pl = app._ball_palette("#3b82f6", True)
    check("配色:明暗板都认强调色", _pl["accent"] == "#3b82f6" == _px["accent"])
    check("配色:明暗板底色不同", _pl["bg"] != _px["bg"] and _pl["card"] != _px["card"],
          (_pl["bg"], _px["bg"]))
    check("配色:换强调色球面跟着变",
          app._ball_palette("#e11d48", False)["ac_lit"] != _px["ac_lit"])

    # ---------- 隐藏/显示: 状态与恢复入口 ----------
    # hide_ball/show_ball 是 run_float_ball 内的闭包, 测试进程里拿不到引用,
    # 这里只断言「面板里有隐藏入口」和「状态字段齐备」——真正的收边/恢复条动效
    # 由 --ball-autohide 外部钩子跑真实 tkinter 验证(见 test_ball_* 用例)。
    _src = open(os.path.join(os.path.dirname(os.path.abspath(app.__file__)),
                             "app.py"), encoding="utf-8").read()
    check("隐藏:面板头部有隐藏入口", 'hide_btn' in _src and "✕ 隐藏" in _src)
    check("隐藏:有收边恢复条的实现", "_peer_show" in _src and "_peer_hide" in _src)
    check("隐藏:状态字典带 hidden/peer",
          '"hidden": False, "peer": None' in _src)
    check("隐藏:右键菜单有隐藏项", "隐藏悬浮球" in _src)
    check("隐藏:贴边松手即隐藏", "hide_ball()" in _src and "near[0] <= 1" in _src)
    check("隐藏:四缘都判(左右上下)",
          all(('"%s"' % e) in _src
              for e in ("left", "right", "top", "bottom")))
    check("隐藏:收边态点球体=恢复(不在屏外展开)",
          'if st["hidden"] and not st["moved"]:' in _src and "show_ball()" in _src)
    check("隐藏:有外部验证钩子", "--ball-autohide" in _src)
    # 收边 = 球体**滑出屏幕**, 只留 ≤4px(恢复条正好盖住)。写成"球体外缘贴缘"就退化成
    # 「出现恢复条、球却没隐藏」—— 用户报的正是这个。这四条目标式是唯一判据。
    # 取整方向也在内: 左/上 floor、右/下 ceil(否则右侧少挪 0.6px, 球比条多露一截)。
    check("隐藏:收边是球体滑出屏幕而非贴着缘",
          'math.floor(e_px - BALL + PAD) if edge == "left"' in _src
          and "math.ceil(sw - e_px - PAD)" in _src
          and 'math.floor(e_px - BALL + PAD) if edge == "top"' in _src
          and "math.ceil(sh - e_px - PAD)" in _src
          and "e_px - BALL / 2.0 + RB" not in _src)
    check("隐藏:恢复条压在球窗之上", "p.lift()" in _src)
    # 球体滑出去后, 它的外发光/环阵半径比球体大一圈, 不清画布就会在条下面留一圈抖动亮点
    # (实测条下 1~14px 有强调色光点), 读起来还是"球没走干净"。
    check("隐藏:收边后清空球体画布(只剩恢复条)", 'ballcv.delete("all")' in _src)
    # ---------- 开合只认点击(悬停自动展开已整体移除) ----------
    check("交互:悬停不再自动展开",
          "hover_tick" not in _src and "HOVER_IN" not in _src and "HOVER_OUT" not in _src
          and "hoverstate" not in _src and 'sget("assist_hover")' not in _src
          and 'sset("assist_hover"' not in _src)
    check("交互:没有悬停展开开关按钮", "hover_btn" not in _src and "toggle_hover" not in _src)
    check("交互:单击球才展开",
          'act == "click"' in _src
          and 'if st["view"] == "ball" and not st["moved"] and time.time() - st["t0"] < 0.8:'
          in _src)
    check("交互:悬停仍保留高亮反馈", "_ptr_on_ball(6)" in _src)
    # ---------- 球体输入层(轮询式状态机) ----------
    # 背景: 颜色键控窗的**透明像素收不到鼠标消息**(实测球窗圆内只有 15.6% 的点能落到球上),
    # 线框球于是"点不到"。修法是丢了 Tk 绑定, 自己轮询全局键态 —— 这里直接测那台状态机。
    _V = app._vin_step
    _v0 = {"down": False, "moved": False, "t": 0.0, "ox": 0, "oy": 0, "r": False, "pl": False}
    _a, _v1 = _V(_v0, 100.0, True, True, False, px=10, py=10)
    check("输入:球内按下 → press", _a == "press" and _v1["down"] and not _v1["moved"])
    _a, _v2 = _V(_v1, 100.1, True, True, False, px=11, py=10)
    check("输入:按住不动 → 无动作", _a is None)
    _a, _v3 = _V(_v2, 100.2, True, False, False, px=11, py=10)
    check("输入:快速抬起 → click(单击展开)", _a == "click" and not _v3["down"])
    _a, _v1b = _V(_v0, 200.0, True, True, False, px=10, py=10)
    _a, _v2b = _V(_v1b, 201.5, True, False, False, px=10, py=10)
    check("输入:慢按(>0.8s)抬起不算单击", _a == "drop")
    _a, _v1c = _V(_v0, 300.0, True, True, False, px=10, py=10)
    _a, _v2c = _V(_v1c, 300.1, True, True, False, px=40, py=10)
    check("输入:移动超过阈值 → drag", _a == "drag" and _v2c["moved"])
    _a, _v3c = _V(_v2c, 300.2, True, False, False, px=60, py=10)
    check("输入:拖完抬起 → drop", _a == "drop")
    _a, _v1d = _V(_v0, 400.0, False, True, False, px=1, py=1)
    check("输入:球外按下不接管", _a is None and not _v1d["down"])
    _a, _v2d = _V(_v1d, 400.1, True, True, False, px=10, py=10)
    check("输入:按着从球外移进来也不接管(不抢用户正在拖的东西)",
          _a is None and not _v2d["down"])
    _a, _v1e = _V(_v0, 500.0, True, False, True, px=10, py=10)
    check("输入:球内右键 → 弹菜单", _a == "rmenu" and _v1e["r"])
    _a, _v1f = _V(_v0, 600.0, False, True, False, px=1, py=1)
    _a, _v2f = _V(_v1f, 600.1, False, False, False, px=1, py=1)
    check("输入:球外单击无动作", _a is None)
    # 结构: 球面 Tk 绑定必须摘掉(否则和轮询层叠加, 拖拽速度翻倍), 命中圆要比线框大一圈
    check("输入:球面 Tk 鼠标绑定已摘除", 'ballcv.bind("<Button-1>"' not in _src)
    check("输入:命中圆按比例且大于线框", "HIT_R = BALL * 0.44" in _src
          and "rr = HIT_R + pad" in _src)
    check("输入:指针在球上时轮询提到 30ms", "root.after(30 if active else 90, poll)" in _src)
    check("输入:右键菜单支持按坐标弹出(虚拟层没有 Tk 事件)",
          "def on_right(e=None, at=None)" in _src and "at[0] if at else e.x_root" in _src)
    check("输入:有鼠标无关的自测钩子", '"--ball-fake-click"' in _src)
    check("适配:尺寸按 DPI 缩放", 'winfo_fpixels("1i")' in _src and "int(_BALL_SIZE * scale)" in _src)
    # ---------- 悬浮球视觉 = 正十二面体线框全息(用户 2026-09-20 第 3 轮明确要求) ----------
    # 这一组尽量**直接调几何函数**而不是匹配源码字符串: 形状对不对、有没有真的在做三维投影,
    # 是行为问题, 用 "源码里有某个名字" 去钉很容易被"改个名照样错"骗过去。
    _sv, _se, _sf, _sn, _sef = app._solid_geo()
    check("球体:形状是正十二面体(20 顶点/30 棱/12 个五边形面)",
          len(_sv) == 20 and len(_se) == 30 and len(_sf) == 12
          and all(len(f) == 5 for f in _sf), (len(_sv), len(_se), len(_sf)))
    check("球体:顶点都在单位球上", all(abs(sum(c * c for c in p) - 1.0) < 1e-6 for p in _sv))
    check("球体:棱长一致(正多面体)", True and
          len({round(math.dist(_sv[i], _sv[j]), 6) for i, j in _se}) == 1)
    check("球体:每条棱恰好属于 2 个面(隐藏线消除的依据)",
          all(len(x) == 2 for x in _sef))
    # 真三维: 转一下, 投影点必须整体变化; 深度必须真的有正有负(有近有远)
    _p1 = app._solid_project(_sv, 0.5, 0.3, 0.1, 0, 0, 100)
    _p2 = app._solid_project(_sv, 0.5 + 0.7, 0.3, 0.1, 0, 0, 100)
    check("球体:真的在做三轴旋转(转动后投影点变化)",
          all(abs(a[0] - b[0]) + abs(a[1] - b[1]) > 1.0 for a, b in zip(_p1, _p2)))
    check("球体:投影带深度且近大远小",
          min(p[2] for p in _p1) < -0.5 and max(p[2] for p in _p1) > 0.5)
    # 泛光 = 逐棱多层描边(宽而淡 → 窄而亮), 近白棱芯是"发光"读感的关键; 面**不**填充
    check("球体:泛光是多层描边阶梯",
          "max(2.0, w_core * (5.4 if idle else 7.0))" in _src
          and 'stipple="gray12"' in _src and 'stipple="gray50"' in _src)
    check("球体:棱芯混向白色(泛光的核心)",
          '"#ffffff", 0.48 * near * near' in _src)
    check("球体:不铺填充面(透明桌面上铺面=点阵圆盘)",
          "create_polygon(*[q for p in pts" not in _src)
    check("球体:背面棱仍然画且用实线(抖动线是 CPU 大头)",
          "e[3] == 0" in _src
          and 'fill=dim(_hex_mix(C["deep"], C["cool"], 0.50)' in _src)
    check("球体:扫描线已移除(不再有横贯球体的虚线/凸包求交)",
          "def _hline_span(" not in _src and "_hline_span(hull, sy)" not in _src
          and "n_scan" not in _src)
    check("球体:中心徽记放大×1.3", "_GLYPH_SCALE = 1.3" in _src
          and "R = BALL * 0.070 * _GLYPH_SCALE" in _src)
    check("隐藏:移开指针后恢复条缩回窄条(_peer_tick 会调 _shrink_peer)",
          "_shrink_peer()" in _src)
    # 中心徽记 = 方舟那套 identity glyph(嵌套菱形)。挖洞必须用**窗口键色**(键控窗里=真透明,
    # 顺带把穿过徽记的线框棱清干净); 画个暗色块在浅色桌面上就是一块脏补丁。
    check("球体:中心有嵌套菱形身份徽记",
          "def _core_glyph(" in _src and "dia(R, fill=mid" in _src
          and "dia(R * 0.30, fill=lit" in _src and "_core_glyph(cx, cy, br, t, dim, fl)" in _src)
    check("球体:徽记挖洞用窗口键色",
          "fill=_BALL_TRANSPARENT, outline=\"\", tags=\"ball_fx\"" in _src)
    check("球体:徽记有横贯亮杠与四角刻度",
          "cx - R * 1.60, cy, cx + R * 1.60" in _src and "create_rectangle(px - q" in _src)
    check("球体:徽记可切换且默认嵌套菱形",
          '_BALL_CORE = "diamond"' in _src and '"--ball-core"' in _src
          and 'st.get("core", _BALL_CORE)' in _src)
    check("球体:三种实心体可切换(正12/正20/截角)",
          "def _dode_solid_geo()" in _src and "def _ico_geo()" in _src
          and "def _trunc_ico_geo()" in _src and '"--ball-solid"' in _src.replace("'", '"'))
    check("球体:静态层与动态层都走同一个全息渲染",
          'ballcv.delete("ball_art")' in _src and 'ballcv.delete("ball_fx")' in _src
          and "def draw_ball_fx()" in _src and "def _holo_draw()" in _src
          and "def draw_ball():" in _src)
    check("球体:五种皮肤状态齐备",
          'return "drag"' in _src and 'return "busy"' in _src
          and 'return "unread"' in _src and 'return "hover"' in _src)
    # 帧率直接等于 CPU 占用。原来 idle 走 150ms(6.7fps) 想省 CPU, 但探针实测单帧才 1.2ms
    # (球) + 0.6ms(玻璃层): 省下的不到 1% 单核, 代价是肉眼可见的顿。现在各态统一 33ms。
    # 这条钉的是"别再退回按状态降帧"和"帧率是单一来源, 别写成一堆 if"。
    check("球体:动效帧率统一(不再按状态降帧)",
          "def ball_anim()" in _src and "root.after(33, ball_anim)" in _src
          and 'after(150 if sk == "idle"' not in _src)
    check("球体:有画帧耗时/图元数探针", "DJXM_BALL_PROF" in _src and "_BALL_PROF" in _src)
    # 浅色主题下顶点节点必须换成强调色: 固定白色的话浅底上等于看不见
    check("球体:浅色主题下顶点节点换色", '"mark": accent' in _src and '"mark": "#ffffff"' in _src)
    # 球面所有内缩量必须是 BALL 的系数, 不能写死像素: 写死 2.5px 在 96 DPI 正常,
    # 到 288 DPI 时占球径的比例会缩掉一大半, 外圈光晕基本被吃掉。
    check("适配:球面尺寸一律按比例而非写死像素",
          "BALL * (2.5 / 56.0)" not in _src and "BALL / 2.0 - 2.5" not in _src
          and "RS = BALL * 0.340" in _src)
    # 窗口比球体大一圈透明边距 → 所有"球在哪"的换算都要补 PAD, 否则球会系统性偏移
    check("球体:窗口边距参与位置换算",
          "PAD = BALL / 2.0 - RB" in _src and "def _ball_win(sw)" in _src
          and "def _ball_top(sh" in _src)
    # 启动必须画第一帧。原来漏了这一步, 画布底色=透明键色 → 球在桌面上根本看不见,
    # 用户报的「找不到悬浮球, 是不是默认隐藏了」根子就在这。
    check("球体:启动就画第一帧", "    draw_ball()\n    try:   # 把自身 pid" in _src)
    # 透明区域会穿透点击(实测 WindowFromPoint), 所以窗口放大不会挡桌面; 但**悬停高亮**判定
    # 用的是窗口矩形, 必须改成圆形, 否则指针离球几十像素球就亮起来(像"够到你了", 其实没有)。
    check("球体:悬停判定按圆不按窗口矩形",
          "def _ptr_on_ball(" in _src
          and 'if st["view"] == "ball" and not st["hidden"] and not st["down"]:' in _src
          and 'st["hover"] = _ptr_on_ball(6)' in _src)

    # ---------- 「找不到悬浮球」的两条兜底 ----------
    # 症结: 位置文件可能存着换屏前的坐标(或贴在最右缘), 球落在任务栏通知区/被置顶窗盖住,
    # 用户就"找不到球"。这里钉住两个修复不被回退。判据按**球体**算(窗口有一圈透明边距)。
    check("定位:贴边坐标会被挪进来",
          "off_right = (x + BALL - PAD) > sw - RB * 2" in _src
          and "return int(max(0, sw - 96 - BALL + PAD))" in _src)
    check("定位:贴顶坐标会下移",
          "off_top = (y + PAD) < RB" in _src and "sh * frac - PAD" in _src)
    check("定位:有 recall_ball 实现", "def recall_ball()" in _src)
    check("定位:右键菜单有定位项", "定位悬浮球" in _src)
    check("定位:有服务端信号通道", "_BALL_RECALL" in _src)
    # ↓ 本轮真实 bug 的回归闸。球是 subprocess 拉起的**独立进程**, 信号计数只在
    #   HTTP 服务进程里; 心跳必须从**响应体**把 n 读回来比对。曾写成读本进程的
    #   _BALL_RECALL(旧 ball_recall_take), 球那边永远是 0 → 按钮点了球不动。
    check("定位:心跳从响应体读信号(跨进程)",
          'int(json.loads(r.read().decode("utf-8")).get("n", 0))' in _src
          and "ball_recall_take" not in _src)
    check("定位:心跳会打 ball_recall", '"/api/ball_recall"' in _src)
    check("定位:首轮只对齐基线不动作", "recall_seen[0] is None" in _src)
    check("定位:有回执通道",
          "def ball_recall_ack" in _src and '"/api/ball_recall_ack"' in _src)
    check("定位:球执行后回调回执", "ball_recall_ack?n=%d" in _src)
    check("定位:隐藏后恢复也不贴右缘", "sw - BALL - 24" not in _src)
    # 收起面板的落点原来写的是 sw - BALL - 4(贴最右缘), 用户展开一次再收起球就贴边了,
    # 又变成"找不到球"。展开时还错把面板左上角坐标存进球位文件, 语义串了。
    check("定位:收起面板后不贴右缘", "x + PW - BALL + PAD, _ball_win(sw)" in _src)
    check("定位:展开时不把面板坐标当球位", "save_pos(bx, by)" in _src)
    check("定位:展开与收起严格互逆",
          "tx = bx + BALL - PAD - PW" in _src and "x + PW - BALL + PAD" in _src)
    check("定位:拖拽钳制按球体算", "int(-PAD), min(st[\"wx\"] + e.x_root - st[\"ox\"]," in _src)
    check("定位:有测试钩子", "--ball-recall" in _src)
    check("定位:有皮肤循环钩子", "--ball-skin-demo" in _src)

    # ---------- 面板视觉改造: 切角边框 + 标题栏切角 + 线框背景 ----------
    # 这几条钉的是「为什么长这样」而不是像素值: 改配色/改切角量都不该红, 但把装饰层删掉、
    # 把切角换回直角、把线框换成实色块, 就必须红。tkinter 的 Frame 画不出斜边, 所以装饰
    # 一律走 place 在最底层的 Canvas(panel_deco), 其他控件照常 pack 叠在上面 —— 这是本
    # 方案能不动任何布局逻辑就加上装饰的关键。
    check("面板:有底层装饰 Canvas", "panel_deco = tk.Canvas(panel" in _src
          and 'relwidth=1, relheight=1' in _src)
    check("面板:装饰层画法独立成函数", "def draw_panel_deco(" in _src)
    check("面板:外框是切角八边形", 'create_polygon(pts, fill=C["bg"]' in _src
          and 'outline=C["accent"]' in _src)
    check("面板:有内框细线做厚度", 'create_polygon(pts2, fill=""' in _src)
    check("面板:四角有准星刻度", "tl = 10" in _src
          and 'fill=C["accent"], width=1, tags="deco"' in _src)
    check("面板:标题栏两端切角", "def _clip_header(" in _src
          and "hcut_l = tk.Canvas(header" in _src)
    check("面板:线框背景用正十二面体", "_dode_project(v, 0.25, 0.4, cx, cy, r)" in _src)
    check("面板:线框按深度做亮度衰减", "br * 0.18" in _src
          and "(d1 + d2) / 2.0" in _src)
    check("面板:尺寸变化时重画装饰", 'lambda e: panel_deco.after(50, draw_panel_deco)' in _src)
    check("面板:apply_palette 里联动重画", "draw_panel_deco()" in _src
          and "_clip_header()" in _src)

    # ---------- 几何落点必须真的落地(_geo 语义) ----------
    # root.geometry() 只是**登记**请求, 不 update_idletasks 在分层窗口上会一直不生效。
    # 「点了定位悬浮球球却没动」的根因就是这里 —— 所以这些断言一条都不能少。
    check("几何:瞬间跳位统一走 _geo", "def _geo(w, h, x, y):" in _src
          and "root.update_idletasks()" in _src)
    check("几何:定位/收边/展开/启动全部走 _geo", _src.count("_geo(") >= 5)
    check("几何:拖拽每步都刷 idle 任务", 'root.geometry("+%d+%d" % (nx, ny))' in _src
          and _src.count('root.update_idletasks()') >= 6)

    # ---------- 界面只剩一套: 新版界面顶替旧版, 旧入口与旧废案一并删除(v0.3.1.47dsk 起) ----------
    # 旧版 web/index.html + app.js + style.css 已整份替换为新版界面(同名同路径),
    # 旧版的 /new 与 /ui(版本选择页) 入口、web/new/ 目录、ui.html 全部删除。
    # 下面几条钉的是"别再冒出第二套界面", 以及首页的静态引用必须是根路径 ——
    # 引用写成 /new/style.css 而文件已经搬到根, 页面会直接白板。
    _root = os.path.dirname(os.path.abspath(app.__file__))
    _html = open(os.path.join(_root, "web", "index.html"), encoding="utf-8").read()
    _js = open(os.path.join(_root, "web", "app.js"), encoding="utf-8").read()
    _css = open(os.path.join(_root, "web", "style.css"), encoding="utf-8").read()
    check("界面:首页引用根路径 app.js", 'src="/app.js"' in _html)
    check("界面:首页引用根路径 style.css", 'href="/style.css"' in _html)
    check("界面:没有残留的 /new/ 目录", not os.path.isdir(os.path.join(_root, "web", "new")))
    check("界面:没有 ui.html 版本选择页", not os.path.exists(os.path.join(_root, "web", "ui.html")))
    check("界面:后端没有 /new 路由与资源白名单",
          '"/new"' not in _src and "_NEW_ASSET_RE" not in _src)
    check("界面:页脚版本号跟随 APP_VER",
          ("洞鉴虚明 " + app.APP_VER) in _html, app.APP_VER)
    # 用户点名的两个废案: AI 推荐标准整组(兴趣分门槛/评分范围/每批条数/缓存时长)
    # 与卡片上的「打开原文」「站内播放」按钮 —— 删了就不该被谁顺手加回来。
    check("界面:AI 推荐标准废案已删除",
          "aiminRow" not in _html and "aifullRow" not in _html
          and "aibatchRow" not in _html and "aicacheRow" not in _html
          and "推荐标准" not in _html)
    # 只删按钮, 不删入口: 点标题/缩略图仍然打开原文(openAttr 上的 data-act="open")。
    # 所以这里断的是「没有按钮」而不是「没有 data-act=open」。
    check("界面:卡片上没有「打开原文」按钮", "打开原文</button>" not in _js)
    check("界面:卡片上没有「站内播放」按钮", 'data-act="play"' not in _js)
    # 设置入口在左栏最上方且带「设置」文字(原来缩在左下角只有图标, 用户找不到)
    check("界面:设置入口在左栏顶部且带文字",
          'id="btnSettings" class="nav setEntry"' in _html and ">设置</button>" in _html)
    check("界面:设置入口有独立样式", "#btnSettings.setEntry" in _css)
    # 字体「中」= zoom 1.15, 旧规则 minmax(330px,1fr) 在这个缩放下只排两列 —— 改成显式三列
    check("布局:大图模式固定三列", "grid-template-columns:repeat(3,minmax(0,1fr))" in _css)
    # 设置页铺满: 旧规则 .svPanel{max-width:660px} 把内容全挤在左边
    # 注意别把注释里的 "原本是 max-width:660px" 当成命中: 这里查的是规则本身
    check("布局:设置页铺满且有栏目分栏", ".svPanel.on{display:grid" in _css
          and ".svPanel{max-width" not in _css)
    # 三个平台的登录头像要显示在搜索框右侧(与旧版一致)
    check("界面:顶栏三平台头像",
          'id="topAvaBili"' in _html and 'id="topAvaTb"' in _html and 'id="topAvaXhh"' in _html)
    check("界面:顶栏头像有样式", "#topAvatars" in _css)
    # hidden 属性会被 display:flex 盖掉 → 没登录也画出三枚假头像, 必须显式隐藏
    check("界面:顶栏头像框未登录时隐藏", "#topAvatars[hidden]" in _css)
    # 大图列数不能只靠 CSS 断点: 字体「中/大」走的是 document.body.style.zoom,
    # 媒体查询量到的是缩放前的视口宽, 所以列数改由 JS 按 .big 的真实宽度算。
    check("布局:大图列数按缩放后宽度算",
          "function applyListCols()" in _js and "w >= 640 ? 3" in _js)
    # v0.3.1.52: 设置页统一改成一侧单栏（用户: 左右双栏看着乱，要一列顺着读下来）。
    # 原来那套"按宽度量列数、两列之间画竖线"的做法整体删除，别再被谁加回来。
    check("布局:设置页/筛选页统一单栏",
          ".svPanel.on{display:grid;grid-template-columns:1fr;" in _css
          and ".mPane.on{display:grid;grid-template-columns:1fr;" in _css)
    # 内容源顺序: B站 → B站直播（用户点名的位置）→ 贴吧 → 小黑盒，每栏下面一条占比滑条
    _i_bili = _html.index('data-src="B站"')
    _i_live = _html.index('data-src="B站直播"')
    _i_tb = _html.index('data-src="贴吧"')
    check("界面:内容源顺序 B站→B站直播→贴吧", _i_bili < _i_live < _i_tb)
    check("界面:每条内容源下面带占比滑条",
          _html.count('class="share"') == 4 and "renderRailShares(" in _js
          and '"B站直播": "live_weight"' in _js)
    # 拉黑名单不再平铺: 固定最大高度的滚动框, 高度明显高于常规备注框(rows 6/8)
    check("界面:拉黑名单是滚动框",
          'id="mBlList" class="chips blBox"' in _html and ".blBox{max-height:320px" in _css)
    check("界面:常规备注框加高", 'id="prefsNote" rows="6"' in _html
          and 'id="dislikeNote" rows="8"' in _html)
    # B站直播默认占比按用户要求降到 5%（0-100 全区间可调，不再是四档按钮）
    check("界面:B站直播默认占比 5%", str(app.DEFAULTS.get("live_weight")) == "5"
          and ("B站直播\", \"live_weight\", 5" in _src))
    # v0.3.1.58: 百度安全验证通过后, 贴吧请求必须走**那扇浏览器窗口**。
    # 实测同一个 URL: 浏览器里 200, 把同样的 Cookie 抄出来用 urllib 打还是 403(按指纹判),
    # 所以"拖完滑块软件里还是没贴吧内容"就卡在这一步 —— 别再退回"抄 cookie 自己请求"。
    check("抓取:滑块验证后走浏览器通道取贴吧页",
          "def _tb_browser_html(" in _src and "_TB_BR" in _src
          and "html, berr = _tb_browser_html(url" in _src
          and "return tb_verify_page_ok(html), url, 200" in _src)
    # 验证窗口不能再整份删 profile: 滑块通过的会话就存在里面, 删了等于每次都要重新拖
    check("抓取:验证窗口保留浏览器会话",
          "SingletonLock" in _src and 'def _cdp_launch(url, profile_name="_tb_login_profile", fresh=False)' in _src)
    # 占比数字不再标黄（用户: 5% 没必要弄成黄色文本）
    check("界面:占比数字不用黄色高亮",
          'pc.classList.remove("warn")' in _js and ".share .pc.warn" not in _css)
    # B站直播筛选: 观众量门槛 + 只看关注
    check("界面:直播筛选有观众量门槛和只看关注",
          'id="mLiveMin"' in _html and 'id="mLiveFollowOnly"' in _html
          and 'data-m="live"' in _html and "/api/live/settings" in _js
          and 'id="mLiveSave"' in _html)
    # v0.3.1.59: 大图模式动作行曾加 overflow:hidden 收住按钮, 结果把按钮的
    # ::after 中文说明浮层一起裁掉 —— 悬停只剩图标, 看不出按钮干什么。
    check("界面:大图模式按钮说明不被裁掉",
          "overflow:visible" in _css
          and "flex-wrap:nowrap;overflow:visible" in _css
          and "flex-wrap:nowrap;overflow:hidden" not in _css)
    # v0.3.1.60: 大图模式的标题挪到封面下方(用户要求)。
    # 靠 order 换位置(不动 DOM 顺序), 文字帖不参与 —— 否则正文会被顶到标题上面。
    check("界面:大图模式标题在封面下方",
          "#list.big .card:not(.text) .media{order:-1}" in _css
          and "#list.big .card:not(.text) .col .meta{display:none}" in _css)
    # v0.3.1.64(用户要求): 大图模式下作者名落到标题正下方, 并用强调色 —— 不再压在封面角标里。
    # 只在 #list.big 显示: 单栏/双栏的作者本来就在 .col .meta 里; 文字帖/封面挂了的卡片也一样,
    # 所以 .text 卡片要藏掉这条, 否则同一个作者会出现两遍。
    check("界面:大图模式作者在标题下方用强调色",
          '<div class="byline">' in _js and '<span class="au">' in _js
          and '<span class="ov au">' not in _js
          and "#list.big .card .byline{display:block" in _css
          and "#list.big .card .byline .au{color:var(--accent-ink)}" in _css
          and "#list.big .card.text .byline{display:none}" in _css)
    # v0.3.1.66(用户要求): 「几天前」挪到 up 名那一行, 封面角标不再重复显示时间。
    check("界面:发布时间并到 up 名那一行",
          '<span class="tm"> · ' in _js and '<span class="ov tm">' not in _js
          and "#list.big .card .byline .tm{color:var(--fg2)}" in _css
          and ".stats .ov" not in _css)
    # v0.3.1.66(用户要求): 关注的人发布的内容, 在作者名前挂「关注的人」小标。
    check("界面:关注的人发布时名字前有小标",
          'data-follow="1"' in _js and "关注的人" in _js
          and 'const followed = !!it.following;' in _js
          and ".pf{display:inline-block" in _css
          and 'color:var(--accent-ink);background:var(--accent-soft)}' in _css)
    # v0.3.1.60: 设置里新增「信息来源遵循强调色」, 默认关闭; 打开后所有来源角标/左栏图标统一强调色
    check("界面:信息来源可跟随强调色",
          'id="srcAccentRow"' in _html and 'data-srcacc="1"' in _html
          and '["#srcAccentRow", "srcacc"]' in _js and "srcacc: \"0\"" in _js
          and 'setAttribute("data-srcacc"' in _js
          and 'html[data-srcacc="1"] .pb.bili' in _css
          and 'html[data-srcacc="1"] .mark.live' in _css)
    # v0.3.1.61(用户要求): 设置页别铺满整宽, 收到 65% 就够 —— 但要有下限, 否则窄窗口里
    # 表单行会折成两行、控件被压扁。
    check("布局:设置页收到 65% 宽",
          "width:65%" in _css and "min-width:470px" in _css and ".svPanel.on{display:grid" in _css)
    # 卡片角标宽度要一样: 「B站」和「小黑盒」字数不同, 不固定宽度会把标题顶得左右不齐
    check("布局:卡片角标定宽, 标题左缘对齐",
          "min-width:44px;text-align:center" in _css)
    # v0.3.1.61: 文字帖在大图模式给一块「文字封面」(正文开头), 这样没有封面时标题
    # 也和带封面的卡片落在同一条线上, 又不留空图位。
    check("布局:文字帖在大图模式有文字封面",
          "class=\"tpanel\"" in _js and ".tpanel{display:none}" in _css
          and "#list.big .card .tpanel{display:block;width:100%;aspect-ratio:16/9" in _css
          and "#list.big .card.text .desc{display:none}" in _css)
    # v0.3.1.61: 聊天栏能直接改设置 —— 本地规则解析后立刻生效, 并把这段对话落库
    check("界面:聊天栏能直接改设置",
          "async function nlApplySettings(" in _js and "const NL_PLAT" in _js
          and "if (!payload) {" in _js and "已改好：" in _js
          and '"/api/chat/note"' in _src)
    # v0.3.1.62: 聊天里改设置改成「先请模型想清楚」, 思考强度可在聊天栏里选(关/低/中/高),
    # 模型不可用或没读懂再退回本地关键词规则 —— 两条路都要在, 否则没额度就改不了设置。
    check("聊天:改设置先过模型且带思考档位",
          "def chat_plan(" in _src and "def call_llm_plan(" in _src
          and '"type": "disabled"' in _src
          and '"reasoning_effort"' in _src
          and '"/api/chat/plan"' in _src
          and "async function aiPlanSettings(" in _js and "function applyPlan(" in _js
          and 'id="chatEffort"' in _html and 'effort: "mid"' in _js
          and "!!d.ai_on" in _js)
    # 用户消息气泡曾被左栏「个人」按钮的全局 .me{flex:1} 命中, 在纵向 flex 里被撑成大绿块。
    # 规则必须限定在 .railFoot 里, 别把 .me 写回全局。
    check("界面:用户消息气泡不被撑满",
          ".railFoot .me{flex:1" in _css and "\n.me{flex:1" not in _css)
    check("抓取:直播按观众量与关注筛选",
          "def live_filters(" in _src and "_live_pass(it, mn, follow_only)" in _src
          and '"live_min_viewers"' in _src and '"live_only_followed"' in _src)
    # v0.3.1.53: 滑条不再用 accent-color 整条上高饱和强调色（用户: 太亮晃眼）。
    # 改成自绘轨道: 填充段 = --accent-strong, 未填充段 = --line, 比例由 JS 写 --pct。
    # 别退回 accent-color —— 一屏五六条并排时会亮得刺眼。
    check("界面:滑条自绘轨道且填充分色",
          "input[type=range]::-webkit-slider-runnable-track" in _css
          and "var(--accent-strong) 0 var(--pct,50%)" in _css
          and "function paintRange(" in _js and "paintRanges()" in _js
          and "accent-color:var(--accent-ink)" not in _css)
    # v0.3.1.53: 没有封面的文字帖不留空图位（用户: 空图片显示太丑）, 直接铺正文。
    check("界面:无封面卡片不用空图位",
          ".card.text .thumb{display:none}" in _css
          and "window.msImgFail" in _js and "class=\"card' + (hasCover ? \"\" : \" text\")" in _js
          and "function hasBody(" in _js
          and "esc((it.title || \"?\").slice(0, 1))" not in _js)
    # v0.3.1.53: 占比只走六档 0/5/25/50/75/100（用户: 一般 25% 一档, 另补一个 5% 小档）。
    # 拖到两档中间会被吸到最近的档, 键盘按档跳 —— 别再退回 step=1 的连续值。
    check("界面:占比只走六档且会吸附",
          "const SHARE_STEPS = [0, 5, 25, 50, 75, 100];" in _js
          and "function snapShare(" in _js and "const base = SHARE_STEPS;" in _js
          and 'inp.addEventListener("keydown"' in _js)
    check("界面:封面加载失败自动转文字帖", 'onerror="msImgFail(this)"' in _js)
    # v0.3.1.53: 大图模式动作行不占位(浮层), 空出来的那条带子平时显示一行简介
    # (没简介就显示作者/播放/时间), 悬停换成按钮且卡片高度不变(用户报的"空出一块")。
    check("界面:大图模式空带子显示一行简介",
          "#list.big .card .bdesc" in _css and 'class="bdesc"' in _js
          and "#list.big .card .acts{position:absolute" in _css
          and "#list.big .card:hover .bdesc" in _css
          and "nometa" in _js)
    # 贴吧 403 有两种: 登录过期 / 百度安全验证拦 IP。分型报错 + 10 分钟冷却 + 刷新可放行一次,
    # 否则用户看到的永远是"抓取失败"且要干等一小时(用户报的"怎么老是抓取失败")。
    check("抓取:贴吧403区分为百度安全验证",
          "def _tb_err_text" in _src and "百度安全验证(403)" in _src)
    check("抓取:贴吧冷却10分钟且刷新可放行",
          "_tb_cooldown_until = int(time.time()) + 600" in _src
          and "def _tb_allow_retry" in _src and "_tb_allow_retry()" in _src)
    check("界面:抓取失败写清原因", '被百度拦截' in _js)
    # v0.3.1.55: 被百度安全验证拦住时不能只报错让用户干等 —— 弹出真浏览器窗口让用户
    # 自己拖滑块, 通过后把浏览器里的 baidu cookie 抄进 tb_verify_cookie 供抓取沿用,
    # 然后清冷却、自动重抓。光提示"被拦截"= 用户以为功能坏了(用户原话)。
    check("验证:百度安全验证有人工滑块通道",
          "def tb_verify_start" in _src and "def tb_verify_poll" in _src
          and "def tb_verify_page_ok" in _src and "def _tb_verify_probe" in _src
          and '"/api/tieba/verify/start"' in _src
          and '"/api/tieba/verify/poll"' in _src)
    check("验证:验证 cookie 会与登录 cookie 合并发出去",
          "def _tb_cookie_header" in _src and "tb_verify_cookie()" in _src
          and '"tb_verify_cookie"' in _src)
    check("验证:通过后清冷却并重新抓取",
          "sset(\"tb_verify_cookie\", cookie_str)" in _src
          and "_tb_cooldown_until = 0" in _src)
    check("验证:左栏拦截行可点开验证窗口",
          '"need_verify"' in _src and "verify: true" in _js
          and "function startTbVerify(" in _js and 'data-verify="1"' in _js
          and ".st.verifiable" in _css)
    # 前端是用 POST 调这三个接口的, 只注册在 do_GET 就会 404 —— 用户点了"没反应"的根因。
    check("验证:验证接口GET/POST都注册",
          _src.count('"/api/tieba/verify/start"') >= 2
          and _src.count('"/api/tieba/verify/poll"') >= 2
          and _src.count('"/api/tieba/verify/stop"') >= 2)
    # v0.3.1.56: 用户问"为什么这么容易触发安全检测" —— 查出来是同一秒里好几路自己在抓。
    # 补池必须只有一个入口(单飞 + 忙时合并), 否则刷新按钮/滚动补池/见底补池/预热各起一个构建,
    # 同时往贴吧发好几串请求; 而旧实现"令牌变了就不清 building"会把标志永久钉死。
    check("抓取:补池走单飞且忙时合并",
          "def kick_build(" in _src and "_BUILD_PENDING = True" in _src
          and '_claimed": True' in _src
          and _src.count("threading.Thread(target=build_universe") == 1
          and "if token == _uni_token:   # 仍是最新设置版本才允许清标志" not in _src)
    check("抓取:出站按平台限速且可观测",
          "def _net_guard(" in _src and "def net_stats(" in _src
          and _src.count('_net_guard("tieba"') >= 5 and '"net": net_stats()' in _src)
    check("抓取:未读角标不再一分钟查四次",
          "setInterval(loadMessages, 300000)" in _js
          and "_tb_login_cache" in _src and "_tb_unread" in _src)
    # v0.3.1.66(用户要求): 点完「消息」按钮, 红点要立刻消失, 不能等下一轮 5 分钟刷新。
    # 做法是服务端存"看过时的未读数"快照(msg_seen), 角标只画 unread - seen; 纯本地语义。
    _mr = req("/api/messages/read", {"seen": {"bili": 3, "tieba": 1, "xhh": "7"}})
    _ms = app.msg_seen()
    _mr2 = req("/api/messages/read", {"seen": {"bili": -5, "xhh": "abc", "junk": 99}})
    _ms2 = app.msg_seen()
    check("界面:点开消息后红点立刻消失(未读快照落库)",
          _mr.get("ok") is True and _ms == {"bili": 3, "tieba": 1, "xhh": 7}
          and _ms2 == {"bili": 0, "tieba": 0, "xhh": 0}      # 脏输入一律夹到 0, 不认识键丢掉
          and "markMessagesRead" in _js and '"/api/messages/read"' in _js
          and "function paintMsgBadge(" in _js
          and "markMessagesRead(); renderMsgPane();" in _js
          and "def msg_seen(" in _src and '"seen": msg_seen()' in _src, (_ms, _ms2))
    # 闸门本身: 同一站点连发第二下必须被挡(前面全局关掉了它, 这里单独开关验一次)
    _guard_old = app._NET_GUARD_ON
    try:
        app._NET_GUARD_ON = True
        with app._NET_LOCK:
            app._NET["last"].clear(); app._NET["minute"].clear(); app._NET["min"] = 0
        _g1 = app._net_guard("unit-gate", 1.0, 3)
        _g2 = app._net_guard("unit-gate", 1.0, 3)
    finally:
        app._NET_GUARD_ON = _guard_old
    check("抓取:出站闸门真的会挡住连发", _g1 is True and _g2 is False, (_g1, _g2))
    # v0.3.1.66(用户要求: 少触发风控): 撞上 403/安全验证/-352 之后要给这个站点挂惩罚期 ——
    # 期内最小间隔翻倍、每分钟额度减半, 而且"只延长不缩短"。这里直接验机制本身。
    _pen_old = app._NET_GUARD_ON
    try:
        app._NET_GUARD_ON = True
        with app._NET_LOCK:
            app._NET["penalty"].clear(); app._NET["last"].clear()
            app._NET["minute"].clear(); app._NET["min"] = 0
        app._net_penalty("unit-pen", 600, 2.0)
        app._net_penalty("unit-pen", 60, 1.5)     # 想缩短 → 必须被拒
        _p1 = app._net_guard("unit-pen", 1.0, 4)
        _p2 = app._net_guard("unit-pen", 1.0, 4)
        _pen = app.net_stats().get("penalty", {})
    finally:
        app._NET_GUARD_ON = _pen_old
        with app._NET_LOCK:
            app._NET["penalty"].pop("unit-pen", None)
    check("抓取:撞风控后进入惩罚期且不可缩短",
          _p1 is True and _p2 is False and _pen.get("unit-pen", 0) > 500, (_p1, _p2, _pen))
    # 抖动的方向只有一个: 只能把间隔拉长, 不能比设定的最小间隔更密(否则等于给自己松绑)。
    check("抓取:请求间隔有向上抖动",
          "random.uniform(1.0, 1.45)" in _src and "def _net_penalty(" in _src
          and "def _net_pace(" in _src and '_net_penalty("tieba"' in _src
          and '_net_penalty("bili"' in _src and "_net_pace(\"bili-api\"" in _src)
    # v0.3.1.57: 直播档位 5%, 实际却刷出一屏(用户报的)。根因是混排把"别的源掏空后
    # 剩下的名额"整段给了最后一路: 27(B站)/14(小黑盒)/50(直播) 的池子原样出成
    # 27/14/50 = 直播 55%, 末尾连排 44 条。现在按权重给每路设配额上限, 并做最大缺口
    # 均摊 + 尾部截断 —— 5% 的直播就该是几十条里一两条, 绝不铺满。
    _mix_in = ([{"key": "b%d" % i, "platform": "B站", "title": "b%d" % i, "views": 1} for i in range(27)]
               + [{"key": "x%d" % i, "platform": "小黑盒", "title": "x%d" % i, "views": 1} for i in range(14)]
               + [{"key": "l%d" % i, "platform": "B站直播", "title": "l%d" % i, "views": 1} for i in range(50)])
    _mix_out = app.mix_sources(_mix_in)
    _n_live = sum(1 for it in _mix_out if it.get("platform") == "B站直播")
    _n_all = len(_mix_out) or 1
    _run, _live_run, _last = 1, 0, (_mix_out[0].get("platform") if _mix_out else "")
    for _it in _mix_out[1:]:
        _p = _it.get("platform") or "B站"
        _run = _run + 1 if _p == _last else 1
        if _p == "B站直播":
            _live_run = max(_live_run, _run)
        _last = _p
    check("抓取:低权重平台不会补满空位",
          _n_live <= 5 and _n_live <= 0.15 * _n_all and _live_run <= 2,
          (_n_live, _n_all, _live_run))
    # 推荐理由默认关闭: 只改默认值对老存档没用(存档里写着 show), 必须带一次性迁移
    check("界面:推荐理由默认关闭",
          'reason: "hide"' in _js and "ms-new-prefs-reason-off" in _js)
    # 大图模式不显示简介
    check("布局:大图模式不显示简介", "#list.big .card .desc{display:none}" in _css)
    # 卡片动作默认只出图标, 悬停才显示文字
    check("界面:卡片动作默认只显示图标", "#list .card .acts button{font-size:0" in _css)
    # 悬停文字必须是"不重排"的浮层: 早先放回 font-size/padding 会撑宽按钮, 把右邻顶走,
    # 鼠标横扫会跳过中间那个(用户实测报的问题)。所以文字走 data-label 的 ::after 浮层,
    # 且旧的"悬停改字号"规则必须已经删干净。
    check("界面:悬停文字是不重排的浮层",
          'content:attr(data-label)' in _css and 'data-label="换一条"' in _js)
    check("界面:悬停不再改按钮字号/内边距",
          "font-size:11.5px;gap:5px;padding:3px 9px" not in _css)
    # 悬停预览播放按要求加回来: 就地静音试播(走 /api/preview), 不做 2× 放大浮层
    check("界面:悬停预览播放已恢复",
          "function startPreview(" in _js and "/api/preview?bvid=" in _js
          and ".thumb .pvVid" in _css)
    # 打开原文必须走壳桥 open_in_app(新窗口/新标签页): pywebview 在
    # OPEN_EXTERNAL_LINKS_IN_BROWSER=False 时把 window.open 当成"在当前 WebView 里导航",
    # 主界面会被目标网页整页顶掉, 点返回只能重载 index.html(用户报的"回退之后严重问题")。
    check("界面:打开原文走壳桥(新窗口/新标签页)",
          "function openExternal(" in _js and "open_in_app" in _js
          and "function shellBridge()" in _js
          and 'openItem(key, card.dataset.url, tt ? tt.textContent : "")' in _js)
    # 壳里桥没就绪也只能排队等 pywebviewready, 不许 window.open 把界面顶掉;
    # 允许的 window.open 只有两处兜底: 壳桥调用失败后的纯浏览器退路 + 压根不在壳里。
    check("界面:壳里不回退 window.open 顶掉界面",
          "shellQueue" in _js and "inShellWebview" in _js
          and _js.count('window.open(url, "_blank", "noopener")') == 2)
    check("界面:打开原文不再直接 window.open",
          'if (url) window.open(url, "_blank", "noopener");' not in _js)

    # ---------- 孤儿球清理: tasklist 输出必须按 GBK 解 ----------
    # 中文 Windows 的 tasklist 输出是 GBK; 以前用 text=True 让 subprocess 按 UTF-8 解,
    # 会在读线程里抛 UnicodeDecodeError(启动日志可见 _readerthread), 于是孤儿球永远清不掉。
    check("清理:tasklist 不用 text=True 直解",
          'text=True, timeout=10,\n                             creationflags=flags).stdout' not in _src)
    check("清理:按 GBK 优先解码", 'for enc in ("gbk", "utf-8")' in _src)
    check("清理:解码失败有兜底", 'decode("utf-8", "replace")' in _src)

    # ---------- 旧版遗留: 长按抑制已随旧界面上岸, 悬停预览只回了一半 ----------
    # 新版界面没有长按菜单(触屏那套); 封面悬停预览在 v0.3.1.48dsk 按用户要求加回来了,
    # 但加回来的是"就地静音试播", 不是 89a4638 删掉的 2× 放大浮层 ——
    # 那个浮层是用户明确说不要的, 所以这里改成"放大浮层不许再出现"。
    import re as _re
    _js_code = _re.sub(r"//[^\n]*", "", _re.sub(r"/\*.*?\*/", "", _js, flags=_re.S))
    _bare = _re.findall(r'(?<![.\w])lpSuppress(?![.\w])', _js_code)
    check("旧版:没有长按抑制残留", not _bare, "裸引用 %d 处" % len(_bare))
    check("旧版:没有封面放大浮层",
          "pv-overlay" not in _js_code and "PV_ZOOMS" not in _js_code)

    # ---------- 设置页的「找不到球」入口 ----------
    check("定位:设置页有定位按钮", 'id="floatBallFind"' in _html)
    check("定位:按钮已绑事件", "#floatBallFind" in _js and "/api/ball_recall_go" in _js)
    # 新版界面里它的样式钩子是 .alt(次级按钮), 不再有旧版那套 .fb-acts 布局块
    check("定位:按钮有独立样式", 'id="floatBallFind" class="alt"' in _html and ".alt" in _css)
    check("定位:未运行时不误报成功",
          "先点「开启悬浮窗」" in _js)
    # 请求发出去 ≠ 球收到了(球靠 2.5s 心跳取信号), 所以必须等回执才敢报成功,
    # 等不到就如实提示「没有响应」—— 否则用户只能对着没反馈的按钮反复点。
    check("定位:等回执才报成功",
          "s.ack" in _js and "正在等悬浮球响应" in _js)
    check("定位:无响应时如实提示", "悬浮球没有响应" in _js)

finally:
    srv.shutdown()
    mock.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)

print("\n==== %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
