# -*- coding: utf-8 -*-
"""v0.4.52dsk 回归: 浏览历史(存储层) + 整页翻译(对齐/降级) + 菜单接线。

不起 GUI、不联网: 翻译用假的 call_llm 顶替, 数据目录指到 build/probe 下的临时目录。
注入页面里的那两段 JS(深色兜底 / 翻译采集回写)由 `_test_menu452.js` 用 node 沙盒跑。
"""
import importlib.util
import json
import os
import shutil
import sys

PROJECT = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(PROJECT, "build", "probe", "menu452")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["MIRRORSORTER_DATA_DIR"] = TMP

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


def load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def src(fname):
    import io
    return io.open(os.path.join(PROJECT, fname), encoding="utf-8").read()


app = load("app_menu452", "app.py")
# ★ 必须挂回 sys.modules["app"]: shell_translate 里是 `import app`, 不注册的话它会
#   再导入一份**独立的** app 实例 —— 于是本脚本替换的 call_llm/translate_texts 对它无效,
#   测试会静默去连真实 AI 接口(实测撞出 401)。
sys.modules["app"] = app

# ---------------------------------------------------------------- 浏览历史
check("历史1 记录一条 http 网址", app.web_hist_add("https://www.bilibili.com/video/BV1xx", "测试视频") is True)
rows = app.web_hist_list()
check("历史2 能查回来且带标题", len(rows) == 1 and rows[0]["title"] == "测试视频", rows)
check("历史3 同一网址不新增行(去重)", app.web_hist_add("https://www.bilibili.com/video/BV1xx", "改过的标题") is True
      and len(app.web_hist_list()) == 1)
check("历史4 重复访问累加次数", app.web_hist_list()[0]["visits"] >= 2, app.web_hist_list())
check("历史5 标题为空时不冲掉已有标题",
      app.web_hist_add("https://www.bilibili.com/video/BV1xx", "") is True
      and app.web_hist_list()[0]["title"] == "改过的标题")
check("历史6 信息流(127.0.0.1)不记", app.web_hist_add("http://127.0.0.1:8000/", "首页") is False)
check("历史7 非 http(s) 不记", app.web_hist_add("javascript:alert(1)") is False
      and app.web_hist_add("file:///C:/x") is False and app.web_hist_add("") is False)
app.web_hist_add("https://tieba.baidu.com/f?kw=test", "贴吧")
app.web_hist_add("https://pan.baidu.com/s/abc", "网盘")
check("历史8 按最近访问倒序", [r["url"] for r in app.web_hist_list()][0].startswith("https://pan.baidu.com"))
check("历史9 搜索匹配标题", [r["url"] for r in app.web_hist_list("贴吧")] ==
      ["https://tieba.baidu.com/f?kw=test"], app.web_hist_list("贴吧"))
check("历史10 搜索匹配网址", len(app.web_hist_list("pan.baidu")) == 1)
check("历史11 limit 生效", len(app.web_hist_list("", 1)) == 1)
check("历史12 删除单条", app.web_hist_del("https://pan.baidu.com/s/abc") is True
      and all("pan.baidu" not in r["url"] for r in app.web_hist_list()))
check("历史13 清空", app.web_hist_clear() is True and app.web_hist_list() == [])

# ---------------------------------------------------------------- 整页翻译
check("翻译1 未配置 AI 接口时给明确错误(不静默返回空)",
      app.translate_texts(["hello"], to="简体中文")[1].find("未配置") >= 0)
app.sset("ai_key", "test-key")

calls = []


def fake_ok(msgs, max_tokens=1200, temperature=0.3, think=None):
    arr = json.loads(msgs[-1]["content"])
    calls.append(len(arr))
    return json.dumps(["译:" + x for x in arr], ensure_ascii=False), {}


app.call_llm = fake_ok
out, err = app.translate_texts(["a", "b", "c", "d", "e"], batch=2)
check("翻译2 按批发送(5 条 / 每批 2 → 3 次调用)", calls == [2, 2, 1], calls)
check("翻译3 结果与输入等长且按序对齐", out == ["译:a", "译:b", "译:c", "译:d", "译:e"], out)

# 模型多吐/少吐 → 整批降级为逐条重试, 绝不能错位
calls2 = []


def fake_ragged(msgs, max_tokens=1200, temperature=0.3, think=None):
    arr = json.loads(msgs[-1]["content"])
    calls2.append(len(arr))
    if len(arr) > 1:
        return json.dumps(["错位的整批结果"], ensure_ascii=False), {}   # 故意长度不符
    return json.dumps(["单:" + arr[0]], ensure_ascii=False), {}


app.call_llm = fake_ragged
out2, _e = app.translate_texts(["x", "y"], batch=2)
check("翻译4 长度不符 → 降级逐条, 结果不错位",
      out2 == ["单:x", "单:y"], out2)
check("翻译5 降级路径真的走了逐条(1 批 + 2 单条)", calls2 == [2, 1, 1], calls2)


def fake_fail(msgs, max_tokens=1200, temperature=0.3, think=None):
    raise RuntimeError("boom")


app.call_llm = fake_fail
out3, err3 = app.translate_texts(["q"], batch=2)
check("翻译6 接口炸了: 留空 + 有错误说明(不抛给 UI)", out3 == [""] and "翻译接口失败" in err3, err3)

check("翻译7 代码块包裹的 JSON 能解析",
      app._tr_parse("```json\n[\"甲\",\"乙\"]\n```", 2) == ["甲", "乙"])
check("翻译8 前后有废话也能抠出数组",
      app._tr_parse("好的:\n[\"甲\"]\n以上。", 1) == ["甲"])
check("翻译9 长度不符判失败(不错位)", app._tr_parse("[\"甲\"]", 2) is None)
check("翻译10 非 JSON 判失败", app._tr_parse("甲", 1) is None)

# ---------------------------------------------------------------- 翻译流程(不建窗)
# translate_page 的关键行为: "已翻译就还原 / 未翻译才采集翻译", 以及把译文真的回写进页面。
# 用假 pages 顶替控件池 + toast_factory 顶替提示条 → 不需要 GUI, 但走的是同一条代码路径。
import threading
import time as _time

tr_mod = load("shell_translate_452", "shell_translate.py")


class _FakePages(object):
    def __init__(self, state_on=False, items=None):
        self.state_on = state_on
        self.items = items if items is not None else ["Hello world", "Second line"]
        self.applied = None
        self.restored = 0

    def exec_js(self, tid, code, timeout=6.0):
        if code == tr_mod._TR_STATE_JS:
            return {"value": {"on": self.state_on, "n": 2 if self.state_on else 0}}
        if code == tr_mod._TR_COLLECT_JS:
            return {"value": {"items": self.items}}
        if code == tr_mod._TR_RESTORE_JS:
            self.restored += 1
            return {"value": {"ok": True, "restored": 2}}
        if code.startswith("(function(map)"):
            self.applied = code
            return {"value": {"ok": True, "hits": len(self.items), "n": len(self.items)}}
        return {"value": {}}


class _FakeApi(object):
    def __init__(self, pinned=False):
        self.pinned = pinned

    def active_page(self):
        return {"id": 7, "url": "https://example.com/a", "pinned": self.pinned}


def _run_tr(pages, api, texts, want="已翻译"):
    seen = []
    done = threading.Event()

    def fac(text, ms=2200):
        seen.append(str(text))
        if want in str(text) or "失败" in str(text) or "没有" in str(text):
            done.set()

        class _T(object):
            def set(self, t, ms=2400):
                seen.append(str(t))
                if want in str(t) or "失败" in str(t) or "没有" in str(t):
                    done.set()
            def close(self):
                pass
        return _T()

    app.translate_texts = lambda items, **kw: (["译文:" + x for x in items], "")
    tr_mod.translate_page(None, api, pages, toast_factory=fac)
    done.wait(8.0)
    return seen


pg1 = _FakePages()
seen1 = _run_tr(pg1, _FakeApi(), ["Hello world", "Second line"])
check("翻译11 未翻译的页面: 采集 → 翻译 → 回写", pg1.applied is not None)
check("翻译12 回写脚本里带的是译文而不是原文",
      pg1.applied is not None and "译文:Hello world" in pg1.applied
      and '"Hello world":' in pg1.applied, (pg1.applied or "")[:160])
check("翻译13 结束时提示「已翻译 N 段」", any("已翻译" in t for t in seen1), seen1[-2:])

pg2 = _FakePages(state_on=True)
seen2 = _run_tr(pg2, _FakeApi(), [], want="已恢复原文")
check("翻译14 已翻译的页面再点一次 = 显示原文(不重复翻)", pg2.restored == 1 and pg2.applied is None)
check("翻译15 还原后提示「已恢复原文」", any("已恢复原文" in t for t in seen2), seen2[-2:])

pg3 = _FakePages(items=[])
_run_tr(pg3, _FakeApi(), [], want="没有需要翻译")
check("翻译16 纯中文页面: 直接提示没有需要翻译的(不烧 token)", pg3.applied is None)

pg4 = _FakePages()
_run_tr(pg4, _FakeApi(pinned=True), [], want="信息流页面不用翻译")
check("翻译17 信息流页面上不翻译", pg4.applied is None)

# ---------------------------------------------------------------- 接线(静态)
nav = src("shell_navbar.py")
aw = src("app_window.py")
pg = src("shell_pages.py")
menu = src("shell_menu.py")
hist = src("shell_history.py")
tr = src("shell_translate.py")
theme = pg[pg.index("_THEME_DARK_JS = r\"\"\""):pg.index("_LIGHT_JS = r\"\"\"")]

check("接线1 导航栏有栏尾菜单按钮且胶囊给它让位",
      '"menu": "\\uE712"' in nav and "def _menu_rect()" in nav
      and "BTN_W - int(2 * scale)" in nav)
check("接线2 点菜单走 on_menu(不是写死的内部逻辑)",
      "on_menu=None" in nav and "if on_menu is not None" in nav and "on_menu()" in nav)
check("接线3 窗口壳真的把 on_menu 接上(参数加了没人传 = 死代码)",
      "on_menu=api.open_menu" in aw)
check("接线4 菜单里有「历史记录」和「翻译此页面」",
      '"label": "历史记录"' in aw and '"label": "翻译此页面"' in aw)
check("接线5 导航时记浏览历史(所有标签都记, 不只激活标签)",
      "app.web_hist_add" in aw and "args=(u, title)" in aw
      and "def _hist_note(" in aw
      and aw.index("self._hist_note(tab_key, url)") < aw.index("if tab_key != active:"))
check("接线5b 历史落库不占 UI 线程(事件回调里同步写库会卡界面)",
      "threading.Thread(target=app.web_hist_add" in aw)
check("接线6 缩放落到控件池(ZoomFactor), 且换标签会补上",
      "c.ZoomFactor = f" in pg and "pages.set_zoom(tab[\"id\"], self._zoom)" in aw)
check("接线7 打印走 WebView2 自己的打印对话框",
      "ShowPrintUI" in pg and "CoreWebView2PrintDialogKind" in pg)
check("接线8 无深色主题的站点一律不处理(小黑盒除外): 其余站点分支里不许注入样式/改 inline",
      "xiaoheihe" in theme and "var OLD_CSS" in theme
      and "filter:invert" in theme.split("var OLD_CSS")[1].split("var ")[0]
      and "ensureCss" not in theme.split("/* ---- 其余站点")[1].split("if (!window.__msx_theme_timer")[0]
      and ".style." not in theme.split("/* ---- 其余站点")[1].split("if (!window.__msx_theme_timer")[0])
check("接线9 不处理的站点要停掉自愈定时器(否则压暗→测成已深色→还原→再压暗, 黑白闪烁)",
      "function stopTimer()" in theme and "clearInterval" in theme
      and "stopTimer();" in theme.split("/* ---- 其余站点")[1])
check("接线10 翻译面板/菜单/历史三个模块都不 import 壳(不成环)",
      "import app_window" not in menu and "import app_window" not in hist
      and "import app_window" not in tr)
check("接线11 翻译走设置里那套 AI 接口, 不引外部服务",
      "app.translate_texts" in tr and "def translate_texts(" in src("app.py")
      and "call_llm(" in src("app.py").split("def translate_texts(")[1][:2600])
check("接线12 历史面板支持搜索/单条删除/清空",
      "web_hist_list" in hist and "web_hist_del" in hist and "web_hist_clear" in hist)

print("\nRESULT: %s" % ("FAIL(%d)" % FAIL if FAIL else "ALL_PASS (%d checks)" % PASS))
sys.exit(1 if FAIL else 0)
