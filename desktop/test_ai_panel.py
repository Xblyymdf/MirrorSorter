# -*- coding: utf-8 -*-
"""内置浏览器右侧 AI 面板(v0.4.42)的回归: 后端工具链 + 壳侧执行端 + 静态接线。

不起任何 GUI 窗口: 面板的 WebView2 摆位靠真机验证, 这里只测**纯逻辑**
(工具分派、结果排版、JS 拼装、参数校验、宽度夹取)与接线是否齐备。
页面侧 JS 的行为回归在 _test_ai_page_js.js(node 沙盒)。
"""
import importlib.util
import inspect
import io
import json
import os
import sys

PROJECT = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(PROJECT, "build", "probe", "ai-panel-tmp")
os.makedirs(TMP, exist_ok=True)
os.environ["MIRRORSORTER_DATA_DIR"] = TMP

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("FAIL  %s  %s" % (name, extra))


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def src(fname):
    return io.open(os.path.join(PROJECT, fname), encoding="utf-8").read()


app = load("app_t", "app.py")
ai = load("shell_ai_t", "shell_ai.py")

# ================================================================ 1. 面板状态
app.set_panel_hook(None)
app.set_browser_bridge(None)
st = app.panel_state()
check("1 面板状态: 壳未就绪时也能回(ok=True, shell=False)",
      st.get("ok") is True and st.get("shell") is False and "theme" in st and "ai_on" in st,
      json.dumps(st, ensure_ascii=False)[:120])
r = app.panel_set({"open": True})
check("2 面板开合: 没有壳时明确报错而不是静默", r.get("ok") is False and r.get("error"), str(r))


class FakeHook:
    def __init__(self):
        self.open = True
        self.calls = []

    def state(self):
        return {"open": self.open, "w": 380, "url": "https://www.bilibili.com/video/BV1",
                "title": "测试视频", "tab_id": 3, "tab_ok": True, "ready": True}

    def set_open(self, v):
        self.calls.append(bool(v))
        self.open = bool(v)


hk = FakeHook()
app.set_panel_hook(hk)
app.set_browser_bridge(lambda a, b: {"ok": True})   # shell 字段 = 浏览器桥是否就绪
st = app.panel_state()
app.set_browser_bridge(None)   # 后面的用例要测"没有壳"的分支
check("3 面板状态: 合并壳上报的网址/标题/宽度",
      st["url"].endswith("BV1") and st["title"] == "测试视频" and st["w"] == 380 and st["shell"] is True,
      json.dumps(st, ensure_ascii=False)[:160])
r = app.panel_set({"open": False})
check("4 面板开合: 落到壳的 set_open", hk.calls == [False] and r.get("ok") is True, str(hk.calls))

# 主题回退
app.sset("ui_theme", "light")
check("5 面板主题跟随 ui_theme", app.panel_theme() == "light")
app.sset("ui_theme", "dark")
check("6 面板主题非法值回退到深色", app.panel_theme() == "dark")
app.sset("ui_theme", "dark")

# ================================================================ 2. 网页工具分派
ctx_off = {"browser_mode": False}
ctx_on = {"browser_mode": True, "actions": []}

res = app._agent_tool("browser_snapshot", {}, ctx_on)
check("7 没有壳时网页工具直接说不可用(不抛异常)",
      res.get("ok") is False and "内置浏览器" in res.get("msg", ""), res.get("msg", "")[:80])

SNAP = {"url": "https://www.bilibili.com/video/BV1xx", "title": "【测试】视频标题",
        "scroll": {"y": 0, "h": 4200, "vh": 800},
        "media": {"paused": True, "t": 12, "dur": 600, "muted": False},
        "text": "这里是简介正文。",
        "els": [{"i": 0, "tag": "a", "t": "首页", "h": "/"},
                {"i": 1, "tag": "button", "t": "搜索"},
                {"i": 2, "tag": "input", "t": "搜点什么", "v": ""}]}


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.reply = SNAP

    def __call__(self, action, args):
        self.calls.append((action, dict(args or {})))
        if action == "boom":
            raise RuntimeError("页面崩了")
        if action == "snapshot" or "error" in self.reply:
            return self.reply
        return {"ok": True, "msg": "已%s" % action, "url": "https://x.test/1"}


fb = FakeBridge()
app.set_browser_bridge(fb)

res = app._agent_tool("browser_snapshot", {}, ctx_off)
check("8 非网页助手模式拒绝操作(避免主界面瞎点)",
      res.get("ok") is False and "网页助手模式" in res.get("msg", ""), res.get("msg", "")[:80])

res = app._agent_tool("browser_snapshot", {"limit": 30}, ctx_on)
msg = res.get("msg", "")
check("9 快照排成模型好读的文本(网址/标题/滚动/视频/元素序号)",
      res.get("ok") is True and "网址: https://www.bilibili.com" in msg
      and "标题: 【测试】视频标题" in msg and "滚动: 0 / 4200" in msg
      and "视频: 已暂停 12/600 秒" in msg and "[1] button 「搜索」" in msg
      and "正文摘要" in msg, msg[:200])
check("10 快照带动作记录(界面显示「读取网页」)",
      "读取网页" in res.get("label", "") and fb.calls[-1][0] == "snapshot", str(res.get("label")))

res = app._agent_tool("browser_click", {"index": 3}, ctx_on)
check("11 点击透传序号", fb.calls[-1] == ("click", {"index": 3}) and res.get("ok") is True, str(fb.calls[-1]))

fb.reply = {"error": "序号 9 无效, 请先 browser_snapshot"}
res = app._agent_tool("browser_click", {"index": 9}, ctx_on)
check("12 壳回报的错误原样交给模型(带「网页操作失败」)",
      res.get("ok") is False and "网页操作失败" in res.get("msg", ""), res.get("msg", "")[:80])

fb.reply = SNAP


def _boom(action, args):
    raise RuntimeError("模拟崩溃")


app.set_browser_bridge(_boom)
res = app._agent_tool("browser_scroll", {"dy": 600}, ctx_on)
check("13 执行端抛异常被兜住(不炸整轮对话)",
      res.get("ok") is False and "模拟崩溃" in res.get("msg", ""), res.get("msg", "")[:80])

app.set_browser_bridge(fb)

# ================================================================ 3. browse 重定向
fb.calls.clear()
ctx = {"browser_mode": True, "browser": None, "actions": []}
res = app._agent_tool("browse", {"url": "https://www.bilibili.com/video/BV2"}, ctx)
check("14 网页助手模式下 browse 改走当前标签(不再回给主界面)",
      ctx.get("browser") is None and fb.calls and fb.calls[-1][0] == "open"
      and res.get("ok") is True, str(fb.calls) + " browser=" + str(ctx.get("browser")))

ctx2 = {"browser_mode": False, "browser": None, "actions": []}
app._agent_tool("browse", {"url": "https://www.bilibili.com/video/BV3"}, ctx2)
check("15 主界面模式保持旧行为(交给前端开标签)",
      isinstance(ctx2.get("browser"), dict) and ctx2["browser"].get("url", "").endswith("BV3"),
      str(ctx2.get("browser")))

# ================================================================ 4. 提示词 / 参数
p_on = app.agent_system_prompt("S", browser=True, page_ctx="【当前网页】https://a.b | 标题: T")
p_off = app.agent_system_prompt("S")
check("16 网页助手模式: 提示词含「网页操作员」与当前网页",
      "网页操作员" in p_on and "https://a.b" in p_on and "browser_snapshot" in p_on)
check("17 主界面模式: 提示词不带网页段(否则模型会去点不存在的页面)",
      "网页操作员" not in p_off and "browser_snapshot" not in p_off)

sig = inspect.signature(app.agent_turn)
check("18 agent_turn 有 browser 开关", "browser" in sig.parameters)
check("19 网页工具只在助手模式挂出(工具集是拼出来的)",
      "tools_all = _AGENT_TOOLS + (_AGENT_BROWSER_TOOLS if browser else [])" in src("app.py"))

names = [t["function"]["name"] for t in app._AGENT_BROWSER_TOOLS]
check("20 网页工具齐全(看/点/填/滚/播放/历史/查找/执行)",
      names == ["browser_snapshot", "browser_open", "browser_click", "browser_type",
                "browser_scroll", "browser_media", "browser_history", "browser_find",
                "browser_eval"], str(names))
check("21 每个网页工具都有阶段提示文案",
      all(n in app._AGENT_PHASE for n in names), str([n for n in names if n not in app._AGENT_PHASE]))
check("22 网页工具结果放宽截断(快照比设置类结果大得多)",
      "_lim = 9000 if name.startswith(\"browser_\")" in src("app.py"))

app.set_panel_hook(None)
check("23 没有壳时当前网页简报不炸", isinstance(app._browser_page_brief(), str))
app.set_panel_hook(hk)
brief = app._browser_page_brief()
check("24 当前网页简报含网址与标题", "https://www.bilibili.com/video/BV1" in brief and "测试视频" in brief, brief)


class _HookNoUrl(FakeHook):
    def state(self):
        return {"url": "", "title": "", "tab_ok": False}


app.set_panel_hook(_HookNoUrl())
check("25 当前标签是信息流时说明白", "信息流" in app._browser_page_brief())
app.set_panel_hook(hk)

# ================================================================ 5. 壳侧执行端
check("26 注入脚本: 拼成完整 IIFE(ExecuteScriptAsync 只吃表达式体)",
      ai.page_js("__MSXAI__.snap(1,2)").startswith("(function(){")
      and ai.page_js("__MSXAI__.snap(1,2)").rstrip().endswith("})()")
      and "return (__MSXAI__.snap(1,2));" in ai.page_js("__MSXAI__.snap(1,2)"))
check("27 JS 字符串转义: 引号/换行/中文",
      ai.js_str('a"b\nc中文') == '"a\\"b\\nc中文"', ai.js_str('a"b\nc中文'))


class FakeApi:
    def __init__(self, page):
        self.page = page
        self.log = []

    def active_page(self):
        return dict(self.page)

    def nav_back(self):
        self.log.append("back")

    def nav_forward(self):
        self.log.append("fwd")

    def nav_reload(self):
        self.log.append("reload")

    def nav_goto(self, u):
        self.log.append(("goto", u))
        return "ok"

    def open_in_app(self, u, title="", mode=None):
        self.log.append(("open", u, mode))
        return "ok"


class FakePages:
    def __init__(self, reply=None):
        self.codes = []
        self.reply = reply if reply is not None else {"value": {"ok": True, "msg": "done"}}
        self.inset = 0
        self.win_w = 1200

    def exec_js(self, key, code, timeout=6.0):
        self.codes.append((key, code))
        return self.reply

    def geometry(self):
        return {"top": 100, "inset": self.inset, "w": self.win_w, "h": 900}

    def set_on_geom(self, fn):
        self.on_geom = fn

    def set_right_inset(self, px):
        self.inset = px

    def ctrl(self, key):
        return None

    def home_ctrl(self):
        return None


pg = FakePages()
ap = FakeApi({"id": 5, "url": "https://a.b/1", "title": "T", "pinned": False})
panel = ai.AIPanel(None, ap, pg, 8000)

r = panel.browser_call("snapshot", {"limit": 20, "text": 500})
check("28 快照: 序号/字数上限传给页面脚本",
      "__MSXAI__.snap(20, 500)" in pg.codes[-1][1] and pg.codes[-1][0] == 5, pg.codes[-1][1][-60:])

pg.codes.clear()
panel.browser_call("click", {"index": 4})
check("29 点击: 拼出 click(4)", "__MSXAI__.click(4)" in pg.codes[-1][1])

pg.codes.clear()
panel.browser_call("type", {"index": 2, "text": '猫"猫', "submit": True})
code = pg.codes[-1][1]
check("30 填写: 文本转义 + submit=true",
      '__MSXAI__.type(2, "猫\\"猫", true)' in code, code[-80:])

pg.codes.clear()
panel.browser_call("scroll", {"bottom": True})
check("31 滚动: bottom 走 true 分支", "__MSXAI__.scroll(null, null, true)" in pg.codes[-1][1])
pg.codes.clear()
panel.browser_call("scroll", {"dy": -300})
check("32 滚动: 相对 -300", "__MSXAI__.scroll(-300, null, false)" in pg.codes[-1][1])

pg.codes.clear()
panel.browser_call("media", {"action": "seek", "seconds": 90})
check("33 播放器: seek 90", '__MSXAI__.media("seek", 90)' in pg.codes[-1][1])
pg.codes.clear()
panel.browser_call("find", {"text": "关键词"})
check("34 页内查找: 关键词进脚本", '__MSXAI__.find("关键词")' in pg.codes[-1][1])

pg.codes.clear()
panel.browser_call("eval", {"code": "return document.title"})
check("35 eval: 包成函数体(允许 return)", "(function(){return document.title})()" in pg.codes[-1][1])

r = panel.browser_call("click", {})
check("36 缺少 index 明确报错", r.get("error") and "index" in r["error"], str(r))
r = panel.browser_call("eval", {"code": "  "})
check("37 eval 空代码被拒", r.get("error") and "code" in r["error"], str(r))
r = panel.browser_call("nope", {})
check("38 未知动作被拒", r.get("error") and "未知" in r["error"], str(r))

pg.reply = {"error": "网页响应超时(脚本可能卡住了)"}
r = panel.browser_call("snapshot", {})
check("39 页面超时: 错误原样上报", r.get("error") and "超时" in r["error"], str(r))
pg.reply = {"value": {"ok": True, "msg": "done"}}

ap.log.clear()
panel.browser_call("history", {"action": "back"})
panel.browser_call("history", {"action": "forward"})
panel.browser_call("history", {"action": "reload"})
check("40 前进/后退/刷新走原生导航(不靠 JS, 页面卡住也能用)",
      ap.log == ["back", "fwd", "reload"], str(ap.log))

ap.log.clear()
r = panel.browser_call("open", {"url": "https://www.bilibili.com/video/BV9"})
check("41 打开网址: 默认新标签(不打断当前页)",
      ap.log and ap.log[-1][0] == "open" and ap.log[-1][2] == "tab" and r.get("ok"), str(ap.log))
ap.log.clear()
r = panel.browser_call("open", {"url": "https://www.bilibili.com/video/BV9", "new_tab": False})
check("42 new_tab=false: 当前标签内跳转", ap.log and ap.log[-1][0] == "goto", str(ap.log))
ap.log.clear()
r = panel.browser_call("open", {"url": "file:///C:/windows"})
check("43 非 http(s) 网址被拒", r.get("error") and "不合法" in r["error"], str(r))
ap.log.clear()
r = panel.browser_call("open", {"query": "猫猫视频"})
check("44 搜索词走搜索引擎(不是当网址)",
      ap.log and ap.log[-1][0] == "open" and "q=" in ap.log[-1][1] or "%" in ap.log[-1][1],
      str(ap.log))

# 信息流标签 / 悬浮窗标签
p2 = ai.AIPanel(None, FakeApi({"id": 0, "url": "", "title": "信息流", "pinned": True}),
                FakePages(), 8000)
r = p2.browser_call("snapshot", {})
check("45 信息流标签上没有网页可操作", r.get("error") and "信息流" in r["error"], str(r))
p3 = ai.AIPanel(None, FakeApi({"id": 7, "url": "https://a.b", "title": "T",
                               "pinned": False, "float": True}), FakePages(), 8000)
check("46 悬浮窗里的标签仍然能操作(控件实例不随窗口变)", p3.browser_call("snapshot", {}) is not None)

# 宽度夹取
pg.inset = 386
panel.st["w"] = 380
check("47 宽度下限 300", panel._clamp_w(120) == ai.PANEL_W_MIN, str(panel._clamp_w(120)))
pg.win_w = 2400
check("48 宽度上限 820", panel._clamp_w(3000) == ai.PANEL_W_MAX, str(panel._clamp_w(3000)))
pg.win_w = 1200
check("49 给网页区留至少 420px(1200 宽时最宽 774)",
      panel._clamp_w(820) == 1200 - ai.TAB_MIN_W - ai.SPLIT_W, str(panel._clamp_w(820)))

check("50 面板宽度/开关落库键名稳定", ai.panel_w_saved() > 0 and isinstance(ai.panel_on_saved(), bool))

# ================================================================ 6. 静态接线
a = src("app.py")
check("51 app.py: /panel 三个静态路由 + 两个面板接口",
      '("/panel", "/panel.html")' in a and '"/panel.js"' in a and '"/panel.css"' in a
      and '"/api/panel/state"' in a and '"/api/panel/set"' in a)
check("52 app.py: 面板走 SSE 且带 browser 标记",
      'want_browser = bool(body.get("browser"))' in a
      and "browser=want_browser" in a)
check("53 app.py: AI 没开时网页助手给明确指引(不落到正则/搜索链路)",
      "网页助手需要先配置 AI" in a)

aw = src("app_window.py")
check("54 app_window: 装面板 + 绑定 + 按标签显隐",
      "from shell_ai import install_ai_panel" in aw and "api.bind_ai_panel(_ai)" in aw
      and "self._sync_ai(tab)" in aw and "p.sync_for_tab(browser_tab)" in aw)
check("55 app_window: 视频全屏时面板一起让位",
      'chrome = {"strip": None, "nav": None, "bm": None, "ai": None}' in aw
      and "ai.set_fullscreen(hidden)" in aw)

sp = src("shell_pages.py")
check("56 shell_pages: 右侧让位 + 页面 JS 执行通道",
      "def set_right_inset(self, px)" in sp and "def exec_js(self, key, code" in sp
      and 'box["inset_right"] = max(0, int(px))' in sp
      and "form.ClientSize.Width - inset" in sp)
check("57 shell_pages: exec_js 只把发起投到 UI 线程(在 UI 线程等 Task 会死锁)",
      "holder[\"task\"] = core.ExecuteScriptAsync(code)" in sp
      and "if task.IsCompleted:" in sp)

nb = src("shell_navbar.py")
check("58 导航栏: AI 开关按钮 + set_ai 接口",
      'AI_W = int(42 * scale)' in nb and "def set_ai(on)" in nb
      and "update.set_ai = set_ai" in nb and 'api.toggle_ai_panel()' in nb
      and "def _ai_rect()" in nb)
check("59 导航栏: 地址栏给 AI 按钮让位(不被压住)",
      "right = bar.Width - MARGIN - AI_W - int(8 * scale)" in nb)

for f, need in (("web/panel.html", 'id="msgs"'), ("web/panel.css", "--accent"),
                ("web/panel.js", "browser: true"), ("web/panel.js", "/api/panel/set")):
    check("60 面板前端文件齐备: %s" % f, os.path.isfile(os.path.join(PROJECT, f))
          and need in src(f))

js = src("web/app.js")
split = js[js.find('const sp = e.target.closest(".splitter")'):
           js.find('$("#toggleLeft").onclick')]
check("61 分栏拖动: 每帧只写宽度变量(requestAnimationFrame + applySplitOnly)",
      "requestAnimationFrame(() => { splitRaf = 0; applySplitOnly(); })" in split
      and "applySplitOnly();" in split)
check("62 分栏拖动: 拖动中不再逐帧调 applyPrefs(卡顿根因)",
      split.count("applyPrefs()") == 1 and "applyPrefs();     // 收尾" in split, str(split.count("applyPrefs()")))
check("63 分栏拖动: 收尾才落库一次", split.count("savePrefs()") == 1)
check("64 拖动期间关掉中间栏指针事件(避免 hover 抖动)",
      "body.splitting #center" in src("web/style.css"))

# 65/66 是 300px 窄面板真机截图里看出来的两处走形, 静态断言兜住别回归
pcss = src("web/panel.css")
check("65 面板: [hidden] 压过 .iconBtn 的 display(grid)",
      "[hidden] { display: none !important; }" in pcss)
pjs = src("web/panel.js")
_ph = [ln for ln in pjs.splitlines() if "$(\"#input\").placeholder" in ln]
check("66 面板: 输入框占位符短到一行放得下(textarea 的 placeholder 不能省略号)",
      len(_ph) == 1 and len(_ph[0]) < 120, str(_ph[:1]))
check("67 面板: 未配置 AI 的引导走可换行气泡, 不塞占位符",
      "function paintOffNotice(show)" in pjs and 'wrap.id = "offNotice"' in pjs
      and ".bubble.notice" in pcss)

print("\nRESULT: %s" % ("PASS(%d)" % PASS if not FAIL else "FAIL(%d)" % FAIL))
if FAIL:
    print("失败: " + " | ".join(FAILS))
sys.exit(1 if FAIL else 0)
