# -*- coding: utf-8 -*-
"""内置浏览器右侧的 AI 助手面板(原生 WebView2 停靠 + 可拖拽分栏)。

为什么要做成"原生停靠的第二个 WebView2"而不是往网页里注入浮层:
  · 注入浮层会被目标站的 CSS / z-index / 全屏 / 懒加载重渲染挤掉, 而且每个标签页都要
    重新挂一遍; 站点一换布局就散架;
  · 面板本身是个网页(web/panel.html), 与本地服务**同源** —— 直接走 /api/chat 的 SSE
    流式接口, 不用跨域、不用把密钥塞进目标页面;
  · 面板是窗口 chrome 的一部分: 切标签、收藏栏高度变化、视频全屏, 都按同一套几何走。

拖拽为什么不卡(用户报修: 拖 AI 对话框宽度特别卡):
  · 分栏是原生 Panel, 拖动时只改**两个**控件的 Bounds(可见标签页 + 面板本身),
    不重排网页 DOM、不重算 CSS 网格、不触发整页样式失效;
  · 其余标签控件在松手时一次性对齐 —— 拖动过程中它们本来就不可见;
  · 松手才落库(ui_ai_panel_w), 拖动过程零磁盘 IO。

面板与网页的通信: 面板 → HTTP /api/chat(browser=true) → 后端 agent 调 browser_* 工具
→ app._BROWSER_BRIDGE 回调到本模块 → ExecuteScriptAsync 在**当前标签页**里执行
→ 结果原路返回给模型。模型据此多轮操作网页(看快照 → 点 → 再看快照)。
"""
import json
import threading
import time

import app

PANEL_W_DEF = 380        # 默认宽度(逻辑像素)
PANEL_W_MIN = 300        # 最窄: 再窄对话就没法看了
PANEL_W_MAX = 820        # 最宽: 超过就没法看网页了
SPLIT_W = 6              # 分栏热区宽度
TAB_MIN_W = 420          # 网页区至少留这么宽, 否则拒绝继续变宽


def panel_w_saved():
    try:
        w = int(app.sget("ui_ai_panel_w") or 0)
    except Exception:
        w = 0
    if w < PANEL_W_MIN:
        return PANEL_W_DEF
    return min(w, PANEL_W_MAX)


def panel_on_saved():
    """面板默认**开**(用户要的就是"浏览器右边有 AI 对话框")。"""
    try:
        return app.sget("ui_ai_panel_on") != "0"
    except Exception:
        return True


# ---------------------------------------------------------------- 注入网页的助手 JS
# 这段脚本不常驻页面: 每次工具调用现拼现执行(幂等, 只定义一次)。
# 好处: 页面加载零开销、零污染; 坏处: 每次多解析 ~5KB —— 相比一次 LLM 往返可忽略。
_AI_PAGE_JS = r"""
(function(){
  var A = window.__MSXAI__;
  if (A && A.v === 3) return null;
  A = window.__MSXAI__ = {v: 3};
  function vis(el){
    try{
      var r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return false;
      if (r.bottom < -3000 || r.top > 40000) return false;
      var cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') return false;
      if (parseFloat(cs.opacity || '1') < 0.05) return false;
      return true;
    }catch(e){ return false; }
  }
  function txt(el, n){
    var t = '';
    try{
      t = el.innerText || el.textContent || '';
      if (!t) t = el.value || el.getAttribute('placeholder') ||
                 el.getAttribute('aria-label') || el.getAttribute('title') || '';
    }catch(e){}
    return String(t).replace(/\s+/g, ' ').trim().slice(0, n || 40);
  }
  A.collect = function(limit){
    var sel = 'a[href],button,input,textarea,select,summary,[role=button],' +
              '[role=link],[role=tab],[onclick],[contenteditable=true]';
    var all = document.querySelectorAll(sel);
    var out = [], els = [];
    for (var i = 0; i < all.length && out.length < limit; i++){
      var el = all[i];
      var tag = (el.tagName || '').toLowerCase();
      var type = (el.getAttribute('type') || '').toLowerCase();
      if (tag === 'input' && type === 'hidden') continue;
      try{ if (el.disabled) continue; }catch(e){}
      if (!vis(el)) continue;
      var item = {i: out.length, tag: tag};
      if (tag === 'a'){
        item.t = txt(el, 36);
        var h = el.getAttribute('href') || '';
        if (h) item.h = h.slice(0, 90);
      } else if (tag === 'input' || tag === 'textarea'){
        item.t = (el.getAttribute('placeholder') || el.getAttribute('name') ||
                  el.getAttribute('type') || '').slice(0, 26);
        if (el.value) item.v = String(el.value).slice(0, 40);
      } else {
        item.t = txt(el, 36);
      }
      if (!item.t && tag !== 'input' && tag !== 'textarea'){
        item.t = (el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 30);
      }
      out.push(item); els.push(el);
    }
    window.__MSXAI_ELS__ = els;
    return out;
  };
  A.snap = function(limit, tlen){
    var de = document.documentElement, b = document.body || de;
    var v = document.querySelector('video');
    var media = null;
    if (v) media = {paused: !!v.paused, t: Math.round(v.currentTime || 0),
                    dur: Math.round(v.duration || 0), muted: !!v.muted,
                    vol: Math.round((v.volume || 0) * 100)};
    var main = document.querySelector('article,main,#app,.video-info,.article-content,.content') || b;
    var text = '';
    try{
      text = String((main && (main.innerText || main.textContent)) || '')
        .replace(/\n{3,}/g, '\n\n').replace(/[ \t]{2,}/g, ' ').trim().slice(0, tlen || 1600);
    }catch(e){}
    return {url: location.href, title: document.title, ready: document.readyState,
            scroll: {y: Math.round(window.scrollY || de.scrollTop || 0),
                     h: Math.round(Math.max(de.scrollHeight, b.scrollHeight) || 0),
                     vh: window.innerHeight || 0},
            media: media, text: text, els: A.collect(limit || 60)};
  };
  A.click = function(i){
    var el = (window.__MSXAI_ELS__ || [])[i];
    if (!el) return {ok: false, msg: '序号 ' + i + ' 无效, 请先 browser_snapshot'};
    try{ el.scrollIntoView({block: 'center', inline: 'center'}); }catch(e){}
    var label = txt(el, 30) || (el.getAttribute('aria-label') || '') || (el.tagName || '');
    try{
      el.click();
      return {ok: true, msg: '已点击「' + label + '」', href: el.href || ''};
    }catch(e){
      try{
        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        return {ok: true, msg: '已点击「' + label + '」'};
      }catch(e2){}
      return {ok: false, msg: '点击失败: ' + e};
    }
  };
  A.type = function(i, text, submit){
    var el = (window.__MSXAI_ELS__ || [])[i];
    if (!el) return {ok: false, msg: '序号 ' + i + ' 无效, 请先 browser_snapshot'};
    var tag = (el.tagName || '').toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && !el.isContentEditable){
      return {ok: false, msg: '第 ' + i + ' 个元素不是输入框(' + tag + ')'};
    }
    try{ el.focus(); }catch(e){}
    try{
      if (el.isContentEditable){
        el.innerText = String(text);
      } else {
        var Ctor = (tag === 'textarea') ? window.HTMLTextAreaElement : window.HTMLInputElement;
        var desc = (Ctor && Ctor.prototype)
          ? Object.getOwnPropertyDescriptor(Ctor.prototype, 'value') : null;
        if (desc && desc.set) desc.set.call(el, String(text)); else el.value = String(text);
      }
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
    }catch(e){ return {ok: false, msg: '填写失败: ' + e}; }
    var sent = false;
    if (submit){
      try{
        ['keydown', 'keypress', 'keyup'].forEach(function(t){
          el.dispatchEvent(new KeyboardEvent(t, {key: 'Enter', code: 'Enter',
            keyCode: 13, which: 13, bubbles: true, cancelable: true}));
        });
        sent = true;
      }catch(e){}
      try{
        var f = el.form;
        if (f && f.requestSubmit){ f.requestSubmit(); sent = true; }
      }catch(e){}
    }
    return {ok: true, msg: '已填入「' + String(text).slice(0, 30) + '」' + (sent ? ' 并提交' : '')};
  };
  A.scroll = function(dy, to, bottom){
    var de = document.documentElement, b = document.body || de;
    var max = Math.max(de.scrollHeight, b.scrollHeight) - (window.innerHeight || 0);
    var y = Math.round(window.scrollY || de.scrollTop || 0);
    var target = y;
    if (bottom) target = max;
    else if (to !== null && to !== undefined && to !== '') target = Number(to) || 0;
    else target = y + (Number(dy) || 600);
    target = Math.max(0, Math.min(Math.max(0, max), target));
    try{ window.scrollTo(0, target); }catch(e){}
    var now = Math.round(window.scrollY || de.scrollTop || 0);
    return {ok: true, msg: '已滚动到 ' + now + ' / ' + Math.round(Math.max(0, max)),
            scroll: {y: now, h: Math.round(Math.max(de.scrollHeight, b.scrollHeight)),
                     vh: window.innerHeight || 0}};
  };
  A.media = function(action, secs){
    var v = document.querySelector('video');
    if (!v) return {ok: false, msg: '当前页面没有视频'};
    try{
      if (action === 'play'){ var p = v.play(); if (p && p.catch) p.catch(function(){}); return {ok: true, msg: '已播放'}; }
      if (action === 'pause'){ v.pause(); return {ok: true, msg: '已暂停'}; }
      if (action === 'mute'){ v.muted = true; return {ok: true, msg: '已静音'}; }
      if (action === 'unmute'){ v.muted = false; return {ok: true, msg: '已取消静音'}; }
      if (action === 'seek'){
        var s = Number(secs) || 0;
        v.currentTime = Math.max(0, Math.min(s, v.duration || s));
        return {ok: true, msg: '已跳到 ' + Math.round(v.currentTime) + ' 秒'};
      }
      if (action === 'fullscreen'){
        var f = v.webkitEnterFullscreen || v.requestFullscreen;
        if (f){ f.call(v); return {ok: true, msg: '已进入全屏'}; }
        return {ok: false, msg: '这个播放器不支持全屏'};
      }
      return {ok: false, msg: '未知动作: ' + action};
    }catch(e){ return {ok: false, msg: '播放器操作失败: ' + e}; }
  };
  A.find = function(q){
    q = String(q || '');
    if (!q) return {ok: false, msg: '没有给出要查找的文字'};
    var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var n, hit = null, count = 0;
    while ((n = w.nextNode())){
      if (n.nodeValue && n.nodeValue.indexOf(q) >= 0){
        count++;
        if (!hit && n.parentElement) hit = n.parentElement;
      }
      if (count > 200) break;
    }
    if (hit){ try{ hit.scrollIntoView({block: 'center'}); }catch(e){} }
    return {ok: true, hits: count,
            msg: count ? ('找到 ' + count + ' 处, 已滚到第一处')
                       : ('页面上没有「' + q.slice(0, 20) + '」')};
  };
  return null;
})();
"""


def page_js(call):
    """把"定义助手 + 执行一次动作"拼成一个完整脚本(ExecuteScriptAsync 只能跑表达式体)。"""
    return "(function(){\n" + _AI_PAGE_JS + "\nreturn (" + call + ");\n})()"


def js_str(s):
    """Python 字符串 → JS 字面量(用 json 转义, 中文原样保留)。"""
    return json.dumps(str(s), ensure_ascii=False)


class AIPanel:
    """面板控制器。所有几何/可见性操作内部 marshal 到 UI 线程。"""

    def __init__(self, win, api, pages, port):
        self.win = win
        self.api = api
        self.pages = pages
        self.port = int(port)
        self.url = "http://127.0.0.1:%d/panel" % self.port
        self.st = {"open": panel_on_saved(), "w": panel_w_saved(),
                   "fs": False,        # 网页/视频全屏中(整窗让给视频)
                   "tab_ok": False,    # 当前标签是不是网页(信息流标签不显示面板)
                   "ready": False, "laying": False, "drag": None,
                   "ghost_w": None,    # 拖动中的预览宽度(松手才落到 w)
                   "theme": "dark", "ctrl": None, "split": None}
        self.on_change = None   # 开合变化回调(导航栏按钮高亮)
        self._form = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- UI 帮助
    def _ui(self, fn):
        form = self._form
        if form is None:
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
            return
        import System.Windows.Forms as WinForms

        def go():
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
        try:
            if form.IsDisposed:
                return
            if form.InvokeRequired:
                form.BeginInvoke(WinForms.MethodInvoker(go))
            else:
                go()
        except Exception:
            import traceback
            traceback.print_exc()

    # ---------------------------------------------------------------- 安装
    def install(self):
        """建分栏热区 + 面板 WebView2(须在窗口壳启动后调用, 任意线程)。"""
        end = time.time() + 30
        form = None
        while time.time() < end:
            f = getattr(self.win, "native", None)
            if f is not None and f.IsHandleCreated:
                form = f
                break
            time.sleep(0.2)
        if form is None:
            raise RuntimeError("native window form not ready")
        self._form = form
        self._make_split()
        self._make_panel()
        # 面板几何跟着内容区走: 收藏栏高度/窗口尺寸变化都由控件池通知
        try:
            self.pages.set_on_geom(lambda: self._ui(self.layout))
        except Exception:
            pass
        # 窗口缩放: 标签控件靠 Anchor 自动跟, 面板/分栏是手动摆的, 得跟着重排
        try:
            form.Resize += lambda _s, _e: self._ui(self.layout)
        except Exception:
            pass
        # 浏览器工具的执行端挂到 app.py 的回调槽
        try:
            app.set_browser_bridge(self.browser_call)
            app.set_panel_hook(self)
        except Exception:
            import traceback
            traceback.print_exc()
        return self

    def _make_split(self):
        def go():
            import System.Windows.Forms as WinForms
            from System.Drawing import Color
            p = WinForms.Panel()
            p.Width = SPLIT_W
            p.Visible = False
            p.Cursor = WinForms.Cursors.SizeWE
            p.BackColor = Color.FromArgb(15, 17, 21)
            p.DoubleBuffered = True

            def paint(_s, e):
                g = e.Graphics
                try:
                    from System.Drawing import SolidBrush
                    c = self._line_color()
                    g.FillRectangle(SolidBrush(c), SPLIT_W // 2 - 1, 0, 2, p.Height)
                except Exception:
                    pass

            def hover(on):
                try:
                    p.BackColor = self._hover_color(on)
                    p.Invalidate()
                except Exception:
                    pass

            p.Paint += paint
            p.MouseEnter += lambda _s, _e: hover(True)
            p.MouseLeave += lambda _s, _e: (None if self.st.get("drag") else hover(False))
            p.MouseDown += self._drag_down
            p.MouseMove += self._drag_move
            p.MouseUp += self._drag_up
            self._form.Controls.Add(p)
            p.BringToFront()
            self.st["split"] = p
        self._ui(go)

    def _make_panel(self):
        def go():
            try:
                from Microsoft.Web.WebView2.WinForms import (
                    WebView2, CoreWebView2CreationProperties)
                from System.Drawing import Color
                from System import Uri
                home = self.pages.home_ctrl()
                if home is None or home.CreationProperties is None:
                    app.sset("ui_ai_panel_err", "home-webview-missing")
                    return
                cp = home.CreationProperties
                props = CoreWebView2CreationProperties()
                props.UserDataFolder = cp.UserDataFolder
                try:   # 与标签控件同参数: 否则 WebView2 会另起一套浏览器进程
                    props.AdditionalBrowserArguments = cp.AdditionalBrowserArguments
                except Exception:
                    pass
                c = WebView2()
                c.CreationProperties = props
                try:
                    c.DefaultBackgroundColor = Color.FromArgb(255, 15, 17, 21)
                except Exception:
                    pass
                c.Visible = False
                import System.Windows.Forms as _WF
                c.Anchor = getattr(_WF.AnchorStyles, "None")   # 手动摆位(几何由 layout 统一算)

                def on_ready(_s, a):
                    try:
                        core = c.CoreWebView2
                        if core is None:
                            app.sset("ui_ai_panel_err", "core-none:%r" % (
                                getattr(a, "InitializationException", None),))
                            return
                        # 面板里禁止弹新窗口(它只跑我们自己的页面)
                        def on_newwin(_s2, a2):
                            try:
                                a2.Handled = True
                            except Exception:
                                pass
                        core.NewWindowRequested += on_newwin
                        c.Source = Uri(self.url)
                        self.st["ready"] = True
                        app.sset("ui_ai_panel_err", "")
                        self.layout()
                    except Exception:
                        import traceback
                        traceback.print_exc()

                c.CoreWebView2InitializationCompleted += on_ready
                self._form.Controls.Add(c)
                self.st["ctrl"] = c
                try:
                    c.EnsureCoreWebView2Async(None)
                except Exception as e:
                    app.sset("ui_ai_panel_err", "ensure:%r" % (e,))
            except Exception:
                import traceback
                traceback.print_exc()
                try:
                    app.sset("ui_ai_panel_err", "create-failed")
                except Exception:
                    pass
        self._ui(go)

    # ---------------------------------------------------------------- 配色
    def _dark(self):
        try:
            v = app.sget("ui_theme")
            if v not in ("light", "dark"):
                v = "light" if app.sget("float_ball_light") == "1" else "dark"
            return v != "light"
        except Exception:
            return True

    def _line_color(self):
        from System.Drawing import Color
        return Color.FromArgb(52, 57, 68) if self._dark() else Color.FromArgb(214, 218, 226)

    def _hover_color(self, on):
        from System.Drawing import Color
        if on:
            return Color.FromArgb(40, 46, 58) if self._dark() else Color.FromArgb(232, 236, 244)
        return Color.FromArgb(15, 17, 21) if self._dark() else Color.FromArgb(246, 247, 250)

    # ---------------------------------------------------------------- 几何
    def _clamp_w(self, w):
        geo = self.pages.geometry() or {}
        total = int(geo.get("w") or 0)
        w = int(w)
        w = max(PANEL_W_MIN, min(PANEL_W_MAX, w))
        if total > 0:
            w = min(w, max(PANEL_W_MIN, total - TAB_MIN_W - SPLIT_W))
        return w

    def layout(self):
        """按当前状态摆分栏与面板, 并让出网页区宽度。UI 线程调用。"""
        if self.st.get("laying"):
            return
        split, ctrl = self.st.get("split"), self.st.get("ctrl")
        if split is None:
            return
        self.st["laying"] = True
        try:
            from System.Drawing import Point, Size
            form = self._form
            if form is None or form.IsDisposed:
                return
            geo = self.pages.geometry() or {}
            top = int(geo.get("top") or 0)
            W = int(form.ClientSize.Width)
            H = int(form.ClientSize.Height)
            if W <= 8 or H <= 8:
                return   # 最小化/尚未布局: 客户区是 0, 摆出来就是负坐标。恢复时会再走一遍
            want = 0
            if self.st["open"] and self.st["tab_ok"] and not self.st["fs"] and self.st["ready"]:
                want = self._clamp_w(self.st["w"])
                self.st["w"] = want
            show = want > 0
            inset = (want + SPLIT_W) if show else 0
            try:
                split.Visible = show
                if ctrl is not None:
                    ctrl.Visible = show
            except Exception:
                pass
            if show:
                try:
                    split.SetBounds(W - want - SPLIT_W, top, SPLIT_W, max(1, H - top))
                    split.BringToFront()
                    if ctrl is not None:
                        ctrl.SetBounds(W - want, top, max(1, want), max(1, H - top))
                        ctrl.BringToFront()
                except Exception:
                    pass
            # 无条件下发: 窗口缩放时标签控件被 Anchor 拉成整窗宽, 不重设就会压到面板下面。
            # 内部会回调 on_geom → layout, 被 laying 挡住, 不会递归。
            self.pages.set_right_inset(inset)
        finally:
            self.st["laying"] = False

    # ---------------------------------------------------------------- 拖拽
    def _drag_down(self, _s, e):
        try:
            import System.Windows.Forms as WinForms
            if e.Button != WinForms.MouseButtons.Left:
                return
            self.st["drag"] = {"x": WinForms.Cursor.Position.X, "w": self.st["w"]}
            self.st["ghost_w"] = self.st["w"]
            self.st["split"].Capture = True
        except Exception:
            pass

    def _drag_move(self, _s, _e):
        d = self.st.get("drag")
        if not d:
            return
        try:
            import System.Windows.Forms as WinForms
            dx = WinForms.Cursor.Position.X - d["x"]
            w = self._clamp_w(d["w"] - dx)
            if w == self.st.get("ghost_w"):
                return
            self.st["ghost_w"] = w
            self._move_split_only(w)
        except Exception:
            pass

    def _move_split_only(self, w):
        """拖动中只挪分栏那条线, 面板和标签控件一动不动。

        ★ 以前这里每帧 SetBounds 面板 + 当前标签的 WebView2: 控件尺寸一变, WebView2 就要
        重新布局整页(视频画布按新尺寸重绘、B站 SPA 还会触发 resize 观察者), 一次拖动
        就是几十次全量重排 —— 这是"拖面板特别卡"的真正来源。现在拖动全程 WebView2 尺寸
        不变, 松手才 layout() 一次性对齐。
        """
        form = self._form
        if form is None:
            return
        geo = self.pages.geometry() or {}
        top = int(geo.get("top") or 0)
        W = int(form.ClientSize.Width)
        H = int(form.ClientSize.Height)
        if W <= 8 or H <= 8:
            return   # 最小化时客户区是 0, 别把控件摆到负坐标
        try:
            self.st["split"].SetBounds(W - w - SPLIT_W, top, SPLIT_W, max(1, H - top))
        except Exception:
            pass

    def _drag_up(self, _s, _e):
        if not self.st.get("drag"):
            return
        self.st["drag"] = None
        try:
            self.st["split"].Capture = False
            self.st["split"].BackColor = self._hover_color(False)
        except Exception:
            pass
        w = self.st.get("ghost_w")
        if isinstance(w, int) and w > 0:
            self.st["w"] = w
        self.st["ghost_w"] = None
        try:
            app.sset("ui_ai_panel_w", str(int(self.st["w"])))
        except Exception:
            pass
        self.layout()   # 唯一一次真正改宽度: 对齐面板 + 所有标签控件

    # ---------------------------------------------------------------- 对外状态
    def is_open(self):
        return bool(self.st["open"])

    def set_open(self, v, remember=True):
        v = bool(v)
        if v == self.st["open"] and self.st.get("ready"):
            return
        self.st["open"] = v
        if remember:
            try:
                app.sset("ui_ai_panel_on", "1" if v else "0")
            except Exception:
                pass
        self._ui(self.layout)
        if v and self.st.get("tab_ok"):
            # 打开就接管焦点: 用户点 AI 是为了马上说话, 不该再点一次输入框
            self._ui(self.focus_input)
        cb = self.on_change
        if cb is not None:
            try:
                cb(v)
            except Exception:
                pass

    def toggle(self):
        self.set_open(not self.st["open"])
        return self.st["open"]

    def focus_input(self):
        """把键盘焦点送进面板的输入框(UI 线程调用; 失败不影响开合)。"""
        c = self.st.get("ctrl")
        if c is None:
            return
        try:
            core = c.CoreWebView2
            if core is None:
                return
            try:
                c.Focus()
            except Exception:
                pass
            core.ExecuteScriptAsync(
                "(function(){var e=document.getElementById('input');"
                "if(e){e.focus();}})()")
        except Exception:
            pass

    def sync_for_tab(self, browser_tab):
        """激活标签变化: 信息流标签不显示面板(它自己右栏就有 AI 聊天)。"""
        b = bool(browser_tab)
        if b == self.st.get("tab_ok"):
            return
        self.st["tab_ok"] = b
        self._ui(self.layout)

    def set_fullscreen(self, on):
        """网页/视频全屏: 面板与分栏一起让位(整窗只留画面)。"""
        on = bool(on)
        if on == self.st.get("fs"):
            return
        self.st["fs"] = on
        self._ui(self.layout)

    def state(self):
        """给 /api/panel/state 的实时信息(面板据此显示"当前网页")。"""
        try:
            ap = self.api.active_page() or {}
        except Exception:
            ap = {}
        return {"open": bool(self.st["open"]), "w": int(self.st["w"]),
                "url": str(ap.get("url") or ""), "title": str(ap.get("title") or ""),
                "tab_id": ap.get("id"),
                "tab_ok": bool(self.st.get("tab_ok")),
                "ready": bool(self.st.get("ready"))}

    def dispose(self):
        def go():
            c = self.st.get("ctrl")
            if c is not None:
                try:
                    c.Visible = False
                    self._form.Controls.Remove(c)
                    c.Dispose()
                except Exception:
                    pass
            s = self.st.get("split")
            if s is not None:
                try:
                    self._form.Controls.Remove(s)
                    s.Dispose()
                except Exception:
                    pass
        self._ui(go)

    # ---------------------------------------------------------------- 浏览器工具执行端
    def browser_call(self, action, args):
        """app.py 的 browser_* 工具落到这里。同步等结果(页面卡住就超时报错)。"""
        args = args if isinstance(args, dict) else {}
        try:
            ap = self.api.active_page() or {}
        except Exception:
            ap = {}
        tid = ap.get("id")
        if tid is None:
            return {"error": "没有激活的标签页"}
        if ap.get("pinned"):
            return {"error": "当前标签页是信息流(不是网页), 请先打开一个网页标签"}

        def _int(key, default=None):
            try:
                v = args.get(key)
                if v is None or v == "":
                    return default
                return int(float(v))
            except Exception:
                return default

        if action == "open":
            return self._act_open(args)
        if action == "history":
            a = str(args.get("action") or "reload")
            if a == "back":
                self.api.nav_back()
                return {"ok": True, "msg": "已后退"}
            if a == "forward":
                self.api.nav_forward()
                return {"ok": True, "msg": "已前进"}
            self.api.nav_reload()
            return {"ok": True, "msg": "已刷新"}

        if action == "snapshot":
            limit = min(160, max(10, _int("limit", 60) or 60))
            tlen = min(4000, max(200, _int("text", 1600) or 1600))
            call = "__MSXAI__.snap(%d, %d)" % (limit, tlen)
        elif action == "click":
            idx = _int("index")
            if idx is None:
                return {"error": "缺少 index(元素序号)"}
            call = "__MSXAI__.click(%d)" % idx
        elif action == "type":
            idx = _int("index")
            if idx is None:
                return {"error": "缺少 index(输入框序号)"}
            call = "__MSXAI__.type(%d, %s, %s)" % (
                idx, js_str(args.get("text") or ""),
                "true" if args.get("submit") else "false")
        elif action == "scroll":
            call = "__MSXAI__.scroll(%s, %s, %s)" % (
                "null" if args.get("dy") in (None, "") else str(_int("dy", 0)),
                "null" if args.get("to") in (None, "") else str(_int("to", 0)),
                "true" if args.get("bottom") else "false")
        elif action == "media":
            call = "__MSXAI__.media(%s, %s)" % (
                js_str(args.get("action") or "play"),
                "null" if args.get("seconds") in (None, "") else str(_int("seconds", 0)))
        elif action == "find":
            call = "__MSXAI__.find(%s)" % js_str(args.get("text") or "")
        elif action == "eval":
            code = str(args.get("code") or "").strip()
            if not code:
                return {"error": "缺少 code"}
            call = "(function(){%s})()" % code
        else:
            return {"error": "未知的网页操作: %s" % action}

        res = self.pages.exec_js(tid, page_js(call))
        if not isinstance(res, dict):
            return {"error": "网页没有返回结果"}
        if res.get("error"):
            return {"error": res.get("error")}
        val = res.get("value")
        if isinstance(val, dict):
            out = dict(val)
            out.setdefault("ok", True)
            if action == "snapshot":
                out["url"] = str(out.get("url") or "")
                out["title"] = str(out.get("title") or "")
            return out
        return {"ok": True, "value": val}

    def _act_open(self, args):
        url = str(args.get("url") or "").strip()
        q = str(args.get("query") or "").strip()
        new_tab = args.get("new_tab")
        if new_tab is None:
            new_tab = True
        import app_window as aw
        if url:
            u = aw._norm_nav_url(url)
            if u is None:
                return {"error": "网址不合法(只支持 http/https): %s" % url[:60]}
        elif q:
            u = aw.search_engine_url(q[:80])
        else:
            u, _nm = aw.search_engine_home()
        if not new_tab and not (self.api.active_page() or {}).get("pinned"):
            self.api.nav_goto(u)
            return {"ok": True, "url": u, "msg": "已在当前标签打开 %s" % u}
        r = self.api.open_in_app(u, ("搜索：" + q[:20]) if q else "网页", mode="tab")
        if r != "ok":
            return {"error": "打不开这个网址: %s" % u[:80]}
        return {"ok": True, "url": u, "title": (("搜索：" + q[:20]) if q else u)[:40],
                "msg": "已在新标签打开 %s" % u}


def install_ai_panel(win, api, pages, port):
    """装好面板并返回控制器(失败返回 None, 壳其余功能不受影响)。"""
    try:
        return AIPanel(win, api, pages, port).install()
    except Exception:
        import traceback
        traceback.print_exc()
        return None
