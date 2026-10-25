# -*- coding: utf-8 -*-
"""内置浏览器标签页的 WebView2 控件池(仅供 app_window.py 窗口壳使用)。

每个标签页一个独立的 WebView2 控件(共享同一浏览器进程与用户数据目录 → 登录态
互通), 切换标签 = 控件可见性切换 —— 页面状态原样保留: 首页永不重载、视频切走
再切回继续播。关闭标签 = 控件 Dispose。

所有控件操作都在 UI 线程执行(内部经 form.BeginInvoke marshal), 管理器方法可从
任意线程调用。pythonnet 注意: 控件必须在 UI 线程创建(跨线程建父子会抛
ArgumentException); Anchor 必须显式四边(主控件 Dock=Fill 时其 Anchor 值不可拷贝)。
"""
import json
import os
import threading
import time

import app   # DATA_DIR(日志/用户脚本目录); 由 app_window 一并加载, 无循环依赖

NL = "\n"   # _dbg_log 落盘换行(历史上曾漏定义 → NameError 被吞, shell_debug.log 一直是空的)

# 截屏黑屏根治(浏览器级冗余声明, v0.3.1.9dsk 回退): 新标签页控件在 home 控件参数
# 基础上显式追加该开关。环境变量 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS(app_window.py
# 设置)已覆盖主控件路; 这里是第二条路, 重复开关无害。
# ⚠️ --disable-direct-composition-video-overlays 在 Chromium 154 上已挡不住视频层
# (实证 8/8 黑), 真正生效的是**页内给 <video> 加 0.5px 圆角**(见下方 unconditional
# 样式注入)。曾经加过 --disable-direct-composition(黑帧清零)但**代价是整条呈现路径
# 退回传统合成 → 用户实测弹幕场景"特别特别卡", 已回退。详见 app_window.py 顶部注释。
MSX_WV2_CAPTURE_FLAG = (
    "" if (os.environ.get("MSX_NO_WV2_FLAG") or os.environ.get("MSX_BASIC") == "1")
    else "--disable-direct-composition-video-overlays")

# 用户报修(m03496)后已取消「外部页签左侧留白」: 窗口级留白会让 B站播放页顶栏左侧空出一块,
# 且看上去像一条独立边框。「播放页左侧宽一点」改在页面内部留(bilibili-ownui.user.js)。


# 自愈式智能深色: 脚本在页面内常驻定时器, 每1.2s重测背景亮度 ——
# 页面已深色(原生夜间/PreferredColorScheme生效) → 保持不动;
# 浅色页面(贴吧等不跟随系统的站) → 反色+色相旋转兜底(媒体类反转回原色);
# 主题晚于首次测量才应用(SPA懒应用)也会被自动追上; 白天模式由 mode 变量直接关断
# 统一主题脚本(自愈: 页面内定时器重测/重设):
# - B站: 用它自己的原生深色主题(cookie theme_style=dark, 官方顶栏开关写的就是这个),
#   效果是真正的原生深色; 白天模式清除该 cookie
# - 其他站点: 测量背景亮度, 浅色页面反色兜底(媒体类元素反转回原色), 已深色不动
# 页面诊断钩子: JS异常/资源加载失败经 window.open(msx-debug:...) 回传(不产生真实新窗)
_DEBUG_HOOK_JS = r"""
(function(){
  if (window.__msx_dbg) return;
  window.__msx_dbg = 1;
  function rep(s){
    try { window.open('msx-debug:' + String(s).slice(0, 220), '_blank'); } catch (e) {}
  }
  window.addEventListener('error', function(e){
    if (e.target && e.target !== window && (e.target.src || e.target.href)) {
      rep('RESFAIL ' + (e.target.tagName || '') + ' ' + String(e.target.src || e.target.href).slice(0, 150));
    } else {
      rep('JSERR ' + (e.message || '') + ' @' + String(e.filename || '').slice(-40) + ':' + (e.lineno || 0));
    }
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    rep('REJ ' + String(e.reason).slice(0, 150));
  }, true);
  var of = fetch;
  window.fetch = function(){
    var args = arguments, url = String((args[0] && args[0].url) || args[0] || '');
    return of.apply(this, arguments).then(function(r){
      if (!r.ok) rep('FETCH ' + r.status + ' ' + url.slice(0, 140));
      return r;
    }, function(e){
      rep('FETCHFAIL ' + url.slice(0, 140) + ' ' + (e && e.message ? e.message : ''));
      throw e;
    });
  };
  var ox = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u){
    this.__msx_u = String(u);
    return ox.apply(this, arguments);
  };
  var osend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(){
    var xhr = this, u = this.__msx_u || '';
    this.addEventListener('error', function(){ rep('XHRFAIL ' + u.slice(0, 140)); });
    this.addEventListener('load', function(){ if (xhr.status >= 400) rep('XHR' + xhr.status + ' ' + u.slice(0, 140)); });
    return osend.apply(this, arguments);
  };
})();
"""

_THEME_DARK_JS = r"""
window.__msx_theme_mode = 'dark';
(function(){
  function lum(rgb){
    var m = String(rgb).match(/[\d.]+/g);
    if (!m) return 255;
    if (m.length > 3 && parseFloat(m[3]) === 0) return -1;
    return 0.299*m[0] + 0.587*m[1] + 0.114*m[2];
  }
  function report(s){
    try { fetch("/api/shell_debug?s=" + encodeURIComponent(s), {cache:"no-store"}); } catch(e) {}
  }
  function cleanup(){
    var o = document.getElementById('__msx_dark');
    if (o) o.remove();
    document.documentElement.style.removeProperty('filter');
  }
  function tick(){
    if (window.__msx_theme_mode !== 'dark') { cleanup(); return; }
    var isBili = /(^|\.)bilibili\.com$/.test(location.hostname);
    if (isBili) {
      /* 夜间模式防白闪: B站自己的深色CSS要等页面加载才生效, document-start 先把
         html 底色压成深色, 否则加载初期整页白闪一下(用户实测反馈)。
         浅色模式由 _LIGHT_JS 移除该内联底色。 */
      document.documentElement.style.setProperty('background', '#0f0f0f', 'important');
      if (document.cookie.indexOf('theme_style=dark') === -1) {
        document.cookie = 'theme_style=dark; path=/; domain=.bilibili.com; max-age=31536000';
        report('bili-cookie-set');
        var n = +(sessionStorage.getItem('__msx_rl') || 0);
        if (n < 2) { sessionStorage.setItem('__msx_rl', String(n + 1)); location.reload(); return; }
      }
      cleanup();   // B站原生深色, 不做反色
      // 主题已定时撤下防白闪幕布(DOM 已解析才撤, 避免页面尚在解析就露白)
      if (document.readyState !== 'loading' && window.__msx_veil_off) window.__msx_veil_off();
      return;
    }
    var b = -1, h = -1;
    try { if (document.body) b = lum(getComputedStyle(document.body).backgroundColor); } catch(e) {}
    try { h = lum(getComputedStyle(document.documentElement).backgroundColor); } catch(e) {}
    var base = (b >= 0 ? b : (h >= 0 ? h : 255));
    var o = document.getElementById('__msx_dark');
    if (base < 100) {
      if (o) o.remove();
      document.documentElement.style.removeProperty('filter');
    } else if (!o) {
      var st = document.createElement('style');
      st.id = '__msx_dark';
      st.textContent = 'html{filter:invert(0.90) hue-rotate(180deg);background:#fff}'
        + 'img,video,picture,canvas,svg,iframe,embed,object'
        + '{filter:invert(1) hue-rotate(180deg)}';
      (document.head || document.documentElement).appendChild(st);
      report('invert-injected ' + location.hostname);
    }
    if (document.readyState !== 'loading' && window.__msx_veil_off) window.__msx_veil_off();
  }
  if (!window.__msx_theme_timer) window.__msx_theme_timer = setInterval(tick, 1500);
  tick();
})()
"""
_LIGHT_JS = r"""
window.__msx_theme_mode = 'light';
(function(){
  var old = document.getElementById('__msx_dark');
  if (old) old.remove();
  document.documentElement.style.removeProperty('filter');
  document.documentElement.style.removeProperty('background');   // 撤掉夜间防白闪的深色底
  if (/(^|\.)bilibili\.com$/.test(location.hostname) &&
      document.cookie.indexOf('theme_style=dark') !== -1) {
    document.cookie = 'theme_style=; path=/; domain=.bilibili.com; max-age=0';
    var n = +(sessionStorage.getItem('__msx_rl') || 0);
    if (n < 2) { sessionStorage.setItem('__msx_rl', String(n + 1)); location.reload(); }
  }
  try { if (window.__msx_veil_off) window.__msx_veil_off(); } catch(e) {}
})()
"""

# 深色模式"防白闪"幕布。外部站点(如小黑盒)自身没有深色主题, 打开时会先铺一层白底,
# 屏幕白闪刺眼。此脚本经 AddScriptToExecuteOnDocumentCreated 在 document-start(先于
# 页面任何渲染)执行, 立刻盖一层不透明深色幕布; 待 _THEME_DARK_JS 判定完主题(或页面
# 加载完成)再淡出。仅深色模式生效(经 prefers-color-scheme 判定, 与 Profile.
# PreferredColorScheme 一致)。文档创建脚本必须早于首帧, 否则白闪仍会出现。
_NOFLASH_JS = r"""
(function(){
  /* ── 防闪幕布 (document-start, 先于页面首帧) ────────────────────────────
   * 用户反馈: 打开B站/小黑盒这类站点会"闪一下" —— 我们的注入脚本(自有UI/评论
   * 筛选/主题)需要时间改版面, 这期间半成品的原生UI先露出来了。
   * 做法: 首帧前先盖一层与主题同色的不透明幕布(深色 #0f0f0f / 浅色 #fff, 看起来
   * 就是一张空白页), 等"页面 + 我们的脚本"都改完再淡出。
   *   · 我们的脚本用 __msx_veil_hold(tag, ttl) 申请"先别揭幕", 改完调
   *     __msx_veil_release(tag); ttl 到期自动失效(脚本异常也不会永久盖住页面)。
   *   · 揭幕条件: 无未过期的 hold + DOM 已解析 + (整页 load 已完成 / 主题已定 /
   *     已等够 LATE 毫秒, 防止页面卡在加载中)。
   *   · 兜底: 绝对上限 HARD 毫秒; 最短停留 MIN_ON 毫秒(防止幕布自己闪一下)。
   *   · SPA: 同一文档内再次 hold 会重新盖幕(B站站内换视频)。
   * 接口在本脚本最前面就定义好, 且与注入脚本共用 window.__msx_veil_holds 对象 ——
   * 两者执行先后颠倒也不会丢 hold。 */
  if (window.__msx_veil_api) return;
  window.__msx_veil_api = 1;
  /* 子框架不挂幕布: WebView2 的 document-created 脚本会下发到**每一个子框架**
     (广告/嵌入), 幕布只该盖顶层文档; 子框架各挂一层既无意义又添开销。 */
  try { if (window.top !== window.self) return; } catch (e) {}
  var BG_DARK = '#0f0f0f', BG_LIGHT = '#ffffff';
  var T0 = Date.now(), MIN_ON = 180, HARD = 6000, SETTLE = 110, LATE = 900;
  var H = window.__msx_veil_holds || (window.__msx_veil_holds = {});
  var veil = null, done = false, asked = false, quiet = 0, timer = null;

  function mode(){
    try {
      if (window.__msx_theme_mode) return window.__msx_theme_mode;
      return (window.matchMedia &&
              window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    } catch(e){ return 'dark'; }
  }
  function bg(){ return mode() === 'light' ? BG_LIGHT : BG_DARK; }
  function pending(now){
    var n = 0, k, d;
    for (k in H) {
      if (!Object.prototype.hasOwnProperty.call(H, k)) continue;
      d = H[k];
      if (d === 0 || d > now) n++;
    }
    return n;
  }
  function mount(){
    if (veil || done) return;
    try {
      var root = document.documentElement;
      if (!root) return;
      var old = document.getElementById('__msx_veil');
      if (old && old.parentNode) old.parentNode.removeChild(old);
      veil = document.createElement('div');
      veil.id = '__msx_veil';
      veil.setAttribute('style',
        'position:fixed;left:0;top:0;right:0;bottom:0;margin:0;padding:0;'
        + 'background:' + bg() + ';z-index:2147483000;pointer-events:none;'
        + 'opacity:1;transition:opacity .12s linear');
      root.appendChild(veil);
    } catch(e){ veil = null; }
  }
  function stop(){ if (timer) { clearInterval(timer); timer = null; } }
  function start(){ if (!timer && !done) timer = setInterval(tick, 90); }
  function off(){
    if (done) return;
    done = true;
    stop();
    var v = veil; veil = null;
    if (!v) return;
    try {
      v.style.opacity = '0';
      setTimeout(function(){
        try { if (v.parentNode) v.parentNode.removeChild(v); } catch(e){}
      }, 200);
    } catch(e){ try { if (v && v.parentNode) v.parentNode.removeChild(v); } catch(e2){} }
  }
  function tick(){
    if (done) return;
    var now = Date.now(), el = now - T0;
    if (el >= HARD) { off(); return; }   // 页面迟迟不完成(readyState 卡 loading)也要揭
    if (veil && veil.style.background !== bg()) veil.style.background = bg();
    if (document.readyState === 'loading' || el < MIN_ON) return;
    if (pending(now) > 0) { quiet = 0; return; }            // 我们的脚本还在改版面
    if (document.readyState !== 'complete' && !asked && el < LATE) return;
    if (!quiet) { quiet = now; return; }                    // 稳一帧再揭, 免得露出半成品
    if (now - quiet >= SETTLE) off();
  }
  window.__msx_veil_hold = function(tag, ttl){
    tag = String(tag || 'x');
    H[tag] = ttl ? (Date.now() + ttl) : 0;
    if (done) { done = false; quiet = 0; mount(); }         // SPA: 重新盖幕
    start();
    tick();
  };
  window.__msx_veil_release = function(tag){
    var k;
    if (tag === undefined || tag === null) {
      for (k in H) { if (Object.prototype.hasOwnProperty.call(H, k)) delete H[k]; }
    } else { delete H[String(tag)]; }
    quiet = 0;
    tick();
  };
  window.__msx_veil_off = function(){ asked = true; quiet = 0; tick(); };  // 兼容旧调用
  mount();
  [0, 16, 45, 100, 220, 500].forEach(function(t){ setTimeout(mount, t); });
  window.addEventListener('load', function(){ quiet = 0; tick(); });
  start();
  setTimeout(off, HARD + 600);                              // 硬兜底
})()
"""



# ---- 拖入外部网页 → 内置浏览器新标签页(v0.4.27dsk) --------------------------
# 用户要求: 「拖拽浏览器网页到我们的软件上可以从我们的软件进行播放」。
# 从外部浏览器把地址栏/标签页/链接拖进 WebView2 时, 页面收到的是 DOM drop 事件,
# dataTransfer 里带 text/uri-list(Chrome/Edge)或 text/x-moz-url(Firefox)。
# 这里在 document-start 捕获: 认出 http(s) 链接就 preventDefault 并经 WebMessage
# 交给壳开新标签页; 认不出也 preventDefault(否则内核会把当前页导航走 —— 实测过).
# 文件/输入框里拖字仍放行, 页面内发起的拖拽(dragstart)不接管.
# 为什么不用 window.open: 拖放事件在部分内核上不算"用户手势", 会被弹窗拦截;
# WebMessage 是 WebView2 的宿主通道, 稳定、也不会真的弹窗口。
_DROP_OPEN_JS = r"""
(function(){
  if (window.__msx_drop_open) return;
  window.__msx_drop_open = 1;
  /* 子框架不接管: drop 只作用于顶层文档(与幕布/注入脚本同一守卫) */
  try { if (window.top !== window.self) return; } catch (e) { return; }
  /* 页面自己发起的拖拽(拖卡片/拖选中的字)不接管; 只有外部程序拖进来的才算。
     v0.4.27: 外部拖拽**一律 preventDefault** —— 认不出链接也不能让内核把当前页导航走
     (用户实测: 拖网页进来直接把当前页顶掉, 而不是新开标签)。 */
  var internal = false;
  window.addEventListener('dragstart', function(){ internal = true; }, true);
  window.addEventListener('dragend', function(){
    setTimeout(function(){ internal = false; }, 0);
  }, true);
  function host(){
    try {
      if (window.chrome && window.chrome.webview &&
          typeof window.chrome.webview.postMessage === 'function') return window.chrome.webview;
    } catch (e) {}
    return null;
  }
  function editable(el){
    try {
      for (var t = el; t && t.nodeType === 1; t = t.parentNode) {
        var n = t.nodeName;
        if (n === 'INPUT' || n === 'TEXTAREA' || n === 'SELECT') return true;
        var ce = t.getAttribute && t.getAttribute('contenteditable');
        if (ce === '' || ce === 'true' || ce === 'plaintext-only') return true;
      }
    } catch (e) {}
    return false;
  }
  /* 只接受"整行就是一个 http(s) 链接"的值: uri-list 每行一个(# 开头是注释),
     Firefox 的 text/x-moz-url 第一行是 URL、第二行是标题;
     段落文本里夹带的链接不算, 免得抢走站点自己的文字拖放。 */
  function strictUrl(s){
    s = String(s == null ? '' : s).replace(/\u0000/g, '').trim();
    if (!s) return '';
    var lines = s.split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var v = lines[i].trim();
      if (v && v.charAt(0) !== '#' && /^https?:\/\/\S+$/i.test(v)) return v;
    }
    return '';
  }
  function fromHtml(h){
    try {
      var m = /href\s*=\s*["']([^"']+)["']/i.exec(String(h || ''));
      if (m && /^https?:\/\/\S+$/i.test(m[1])) return m[1];
    } catch (e) {}
    return '';
  }
  function typesOf(dt){
    try { return Array.prototype.slice.call(dt.types || []); } catch (e) { return []; }
  }
  function pick(dt){
    if (!dt) return '';
    var types = typesOf(dt);
    var order = ['text/uri-list', 'text/x-moz-url', 'URL', 'text/plain'];
    for (var i = 0; i < order.length; i++) {
      if (types.length && types.indexOf(order[i]) < 0) continue;
      var v = '';
      try { v = dt.getData(order[i]); } catch (e) { continue; }
      var u = strictUrl(v) || fromHtml(v);
      if (u) return u;
    }
    if (types.indexOf('text/html') >= 0) {
      try { return fromHtml(dt.getData('text/html')); } catch (e) {}
    }
    return '';
  }
  /* 部分内核对外部拖拽在 drop 时 getData 读不到, 但 items 里仍有字符串:
     异步再试一次; 仍读不到就上报 miss(壳里记日志), 绝不落回内核默认导航。 */
  function asyncPick(dt, done){
    var answered = false, left = 0, items = [];
    function answer(u){ if (answered) return; answered = true; done(u || ''); }
    try { items = Array.prototype.slice.call((dt && dt.items) || []); } catch (e) { items = []; }
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var kind = String(it.kind || '');
      var ty = String(it.type || '');
      if (kind !== 'string') continue;
      if (!/^text\//i.test(ty) && ty !== 'URL') continue;
      left++;
      (function(it2){
        try {
          it2.getAsString(function(s){
            var u = strictUrl(s) || fromHtml(s);
            if (u) { answer(u); return; }
            if (--left <= 0) answer('');
          });
        } catch (e) { if (--left <= 0) answer(''); }
      })(it);
    }
    if (!left) answer('');
  }
  function blocked(e){
    try {
      var dt = e.dataTransfer;
      if (!dt) return true;
      var types = typesOf(dt);
      if (types.indexOf('Files') >= 0) return true;      /* 拖文件: 交给页面 */
      if (dt.files && dt.files.length) return true;
      if (editable(e.target)) return true;               /* 输入框/可编辑区里拖字 */
      return false;                                      /* 其余外部拖拽一律接管 */
    } catch (err) { return true; }
  }
  function openInShell(u){
    var w = host();
    if (!w) return false;
    try { w.postMessage({ t: 'msx-drop', u: u }); return true; } catch (e) { return false; }
  }
  function reportMiss(types){
    var w = host();
    if (!w) return;
    try {
      w.postMessage({ t: 'msx-drop-miss',
                      types: (types || []).join('|').slice(0, 200) });
    } catch (e) {}
  }
  document.addEventListener('dragover', function(e){
    if (blocked(e)) return;
    if (internal) return;   /* 页面内拖拽: 不干预站点自己的 dragover 逻辑 */
    try { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } catch (err) {}
  }, true);
  document.addEventListener('drop', function(e){
    if (blocked(e)) return;
    var wasInternal = internal;
    internal = false;
    var dt = e.dataTransfer;
    var u = pick(dt);
    if (!u && wasInternal) return;   /* 页面内拖拽且没有链接: 交还给站点 */
    try {
      e.preventDefault();
      e.stopPropagation();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    } catch (err) {}
    if (u) { openInShell(u); return; }
    var ts = typesOf(dt);
    asyncPick(dt, function(u2){
      if (u2) { openInShell(u2); return; }
      reportMiss(ts);
    });
  }, true);
})()

"""

# ---- 内置油猴脚本环境: Vue + GM 垫片 + 净化大师 + 用户自定义脚本 ----
# 通道: AddScriptToExecuteOnDocumentCreatedAsync(每次文档创建自动执行, MAIN 上下文,
# 等价于 document-start 的油猴脚本)。扩展 API(AddBrowserExtensionAsync)在部分
# 环境返回"不支持"(0x80070032), 因此不依赖它。
def _dbg_log(msg):
    """壳诊断日志(数据目录/shell_debug.log): 无控制台环境下排查注入链路。"""
    try:
        import time as _t
        with open(os.path.join(app.DATA_DIR, "shell_debug.log"), "a",
                  encoding="utf-8") as f:
            print("[shell] " + msg, flush=True)
            # (历史笔误: 曾写成 "[" + NL + "] %s" % (t, msg) —— 一个 %s 配二元组,
            #  每次都 TypeError 被吞, 日志文件永远是空的, 只有 stdout 有)
            f.write("[%s] %s%s" % (_t.strftime("%H:%M:%S"), msg, NL))
    except Exception:
        pass


def drop_url_from_message(raw):
    """WebMessage(JSON 文本) → 拖入的链接; 不是拖放消息返回 ""。
    只做协议解析, URL 合法性交给 ShellApi.open_in_app(与它同一道安全闸)。"""
    try:
        d = json.loads(raw) if raw else None
    except Exception:
        return ""
    if not isinstance(d, dict) or d.get("t") != "msx-drop":
        return ""
    return str(d.get("u") or "").strip()[:2000]


def drop_miss_types_from_message(raw):
    """WebMessage(JSON 文本) → 拖拽认不出时的 MIME 类型串; 不是该消息返回 ""。"""
    try:
        d = json.loads(raw) if raw else None
    except Exception:
        return ""
    if not isinstance(d, dict) or d.get("t") != "msx-drop-miss":
        return ""
    return str(d.get("types") or "").strip()[:200]


def _read_ext_file(name):
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(getattr(sys, "_MEIPASS", "") or "", "extensions"),
              os.path.join(here, "extensions")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                data = open(p, encoding="utf-8").read()
                _dbg_log("read %s from %s (%d bytes)" % (name, d, len(data)))
                return data
            except Exception as e:
                _dbg_log("read %s FAILED: %r" % (name, e))
                return ""
    _dbg_log("read %s NOT FOUND (here=%s meipass=%s)"
             % (name, here, getattr(sys, "_MEIPASS", None)))
    return ""


def _user_scripts():
    """用户自定义油猴脚本: 数据目录 userscripts/*.user.js, 丢进去即生效。"""
    try:
        d = os.path.join(app.DATA_DIR, "userscripts")
        out = []
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith((".user.js", ".js")):
                    try:
                        out.append(open(os.path.join(d, fn), encoding="utf-8").read())
                    except Exception:
                        pass
        return out
    except Exception:
        return []


def _read_ext_bytes(name):
    """extensions/ 下二进制资源(Framegen 权重 .bin)。找不到返回空 bytes。"""
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(getattr(sys, "_MEIPASS", "") or "", "extensions"),
              os.path.join(here, "extensions")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except Exception:
                return b""
    return b""


# ---- AI补帧(Framegen, github.com/MONZikWasTaken/Framegen) ----
# 原项目是 Chrome 扩展: 内容脚本依赖 chrome.runtime.getURL(取权重/运行时)与
# chrome.storage.local(设置持久化)。我们的注入跑在页面主世界 —— 引导垫片把
# 权重/运行时以 base64 内嵌转 Blob URL 供其 fetch/import(动态 import Blob 模块
# 与扩展资源行为一致), chrome.storage 落到页面 localStorage(键 fc_*, 浮窗面板
# 的调整跨刷新保留)。代码 MIT; 模型权重仅限个人/非商用(见 framegen-NOTICE.txt)。
_FG_ASSETS = (
    ("rt/rt.js", "framegen-rt.js", "text/javascript"),
    ("rt/sr.js", "framegen-sr.js", "text/javascript"),
    ("assets/rt_v7s.bin", "framegen-weights-v7s.bin", "application/octet-stream"),
    ("assets/rt_v7s.json", "framegen-weights-v7s.json", "application/json"),
    ("assets/rt_sr.bin", "framegen-weights-sr.bin", "application/octet-stream"),
    ("assets/rt_sr.json", "framegen-weights-sr.json", "application/json"),
)
_FG_B64_CACHE = {}   # 文件名 -> base64(进程级缓存: 每个新标签页都重建注入脚本, 别反复编4MB)

_FG_BOOT_JS = r"""
(function () {
  "use strict";
  if (window.__msxFgBoot) return;
  window.__msxFgBoot = 1;
  var SRC = __MSX_FG_SRC__;
  var RES = {};
  try {
    for (var k in SRC) {
      var raw = atob(SRC[k].b64), u8 = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) u8[i] = raw.charCodeAt(i);
      RES[k] = URL.createObjectURL(new Blob([u8], { type: SRC[k].mime }));
    }
    var C = window.chrome || (window.chrome = {});
    if (!C.runtime) C.runtime = {
      id: "msx-framegen",
      getURL: function (p) { return RES[p] || p; },
      sendMessage: function (_m, cb) { if (cb) cb({ ok: false }); },
      openOptionsPage: function () {},
      onMessage: { addListener: function () {} },
    };
    if (!C.storage) C.storage = {
      local: {
        get: function (defs, cb) {
          var out = {};
          try {
            for (var k2 in defs) {
              var raw2 = localStorage.getItem("fc_" + k2);
              out[k2] = raw2 === null ? defs[k2] : JSON.parse(raw2);
            }
            cb(out);
          } catch (e) { try { cb(defs); } catch (_) {} }
        },
        set: function (obj, cb) {
          try { for (var k3 in obj) localStorage.setItem("fc_" + k3, JSON.stringify(obj[k3])); }
          catch (e) {}
          if (cb) cb();
        },
      },
      onChanged: { addListener: function () {} },
    };
  } catch (e) { console.warn("[msx] framegen boot failed", e); }
})();
"""


def _framegen_boot_js():
    """组装 Framegen 引导垫片(资源 base64 表)。任一资源缺失返回空串(禁用)。"""
    import base64
    import json as _json
    src = {}
    for key, fname, mime in _FG_ASSETS:
        if fname not in _FG_B64_CACHE:
            data = _read_ext_bytes(fname)
            if not data:
                _dbg_log("framegen asset missing: %s" % fname)
                return ""
            _FG_B64_CACHE[fname] = base64.b64encode(data).decode("ascii")
        src[key] = {"mime": mime, "b64": _FG_B64_CACHE[fname]}
    return _FG_BOOT_JS.replace("__MSX_FG_SRC__", _json.dumps(src))


def _glob_match(pattern, host):
    """简化 @match 通配: *://*.bilibili.com/* 的 host 段 *.bilibili.com 含主域"""
    try:
        hostpat = pattern.split("://", 1)[-1].split("/", 1)[0]
        if hostpat.startswith("*."):
            dome = hostpat[2:]
            return host == dome or host.endswith("." + dome)
        return host == hostpat
    except Exception:
        return True


def _js_str(s):
    """Python 字符串 → 可安全拼进脚本的 JS 字面量(转义引号/换行/反斜杠)。"""
    return json.dumps(s, ensure_ascii=False)


# ── 功能层总开关(排障用): MSX_BASIC=1 ───────────────────────────────────────
# 让内置浏览器退化为**最基础的正常网页播放**: 不注入任何脚本(超分/画质增强/自有UI/
# 广告过滤/圆角兜底…), 浏览器级也不加任何附加参数 —— 等同于用纯 WebView2 打开 B站,
# 与普通浏览器行为一致。用于"先回退到最基础, 再逐层加回功能、定位是哪一层影响
# 视频播放/截屏"。默认不开(生产走完整功能)。
def _basic_mode():
    return os.environ.get("MSX_BASIC") == "1"


# 页内兜底: 让播放器 <video> 失去"独立 DComp 平面"资格。
# 根因: Chromium 把可见视频层当 DirectComposition 独立平面(overlay)呈现时, GDI 系截屏
# (微信/QQ/PrntScr 的 BitBlt 屏幕 DC)读不到该平面 → 播放器区域整块黑、其余 UI 正常。
# ⚠️ 曾经的 border-radius:0.5px 方案在 Chromium 154 上**已失效**: 该内核支持"带圆角的
# overlay"(msedge.dll 内的 `delegated.overlay.candidate_rounded_corners` /
# `is_fast_rounded_corner` 即证), 圆角裁剪不再否决 overlay 资格 → 视频照样被提升,
# 截屏照样黑(用户实测截图即证据)。
# 现在改用**非不透明 + 恒等滤镜**: overlay 要求 quad 不透明, opacity<1 必然把它拉回
# 主合成面; 再叠一道恒等 filter 兜底(带滤镜的内容必经渲染面, 不能直接扫描输出)。
# 两者视觉无感, 也**不关 DComp**(避开 --disable-direct-composition 那种整页退传统
# 合成的"特别特别卡")。默认启用; MSX_BASIC=1 时不注入。
_OVERLAY_CSS_DEFAULT = "video{opacity:0.999!important;filter:brightness(1)!important}"
_OVERLAY_CSS_VARIANTS = {   # 实验用(MSX_NO_OVERLAY_CSS_MODE=…), 默认走上面的组合
    "radius": "video{border-radius:0.5px!important}",
    "alpha":  "video{opacity:0.999!important}",
    "filter": "video{filter:brightness(1)!important}",
    "none":   "",
}


def _overlay_css():
    mode = (os.environ.get("MSX_NO_OVERLAY_CSS_MODE") or "").strip()
    return _OVERLAY_CSS_VARIANTS.get(mode, _OVERLAY_CSS_DEFAULT)


# 画质增强(bilibili-enhance)是在 <video> 上挂 CSS 滤镜(锐化+色彩) —— 与上面那段"兜底
# CSS"同一类风险: 施加在 <video> 上的 filter 会让视频层被提升为独立合成平面, GDI 截屏黑。
# 取证: 正常的 2.9 对 <video> 无任何 CSS。⇒ 默认**暂停**(MSX_BQ=1 可恢复, 用于 A/B)。
def _bq_enabled():
    return os.environ.get("MSX_BQ", "0") == "1"


# ── 补呈现看门狗(v0.3.1.42dsk) ───────────────────────────────────────────────
# 症状: 悬浮窗/全屏/换宿主窗的瞬间, WebView2 的合成层被"宿主编排变化"打断 —— 控件内容
#   整块停在同一色(深色底 #0f1115), 且**不会自愈**。快机器往往 1s 内自己就好, 慢机器
#   (WebView2 冷启动慢 / GPU 驱动初始化慢 / 多屏混合 DPI / 混合显卡)要好几秒 →
#   于是同一份代码在两台机器上表现为"一台正常、一台必黑"。
# 旧实现(_retext_later): 在 0.35/0.9/1.6s 三个**写死**的时间点各补一次「隐藏→显示→置顶」,
#   **不校验是否真的出图、也不重试** ⇒ 慢机器三次全落空, 黑屏永久留下。这就是机器差异的根。
# 现实现: 递进式看门狗 —— 到点补呈现, 补完**校验画面是否真的出图**(GDI PrintWindow 抽样,
#   判"是否整块同一个颜色"), 出图立刻收工(快机器只花一次、零额外代价), 还黑就继续补并
#   **逐级加重手段**, 直到出图或到 deadline。全程不重载页面, 页面状态原样保留。
# A/B 开关(排障用):
#   MSX_NO_REPRESENT=1 → 退回旧行为(固定三次、不校验), 用于确认新旧差异。
_REP_TAPS = (0.25, 0.6, 1.1, 1.8, 2.8, 4.0, 5.5, 7.0)   # 递进补呈现时刻(秒, 相对触发)
_REP_LEGACY_TAPS = (0.35, 0.9, 1.6)                     # 旧行为(仅 MSX_NO_REPRESENT=1)
_REP_SETTLE = 0.22        # 补呈现后等画面稳定再校验
_REP_DEADLINE = 8.0       # 硬上限(秒)
_REP_REG = {}             # id(ctrl) -> {"ev": Event}  正在跑的看门狗(新触发会取消旧的)
_REP_LOCK = threading.Lock()


def _fs_soft():
    """全屏补呈现用哪种手段: 默认**轻补**; MSX_FS_HARD=1 才用旧的硬补。

    为什么默认改成轻补(证据来自用户真机 shell_debug.log, 不是推测):
      同一分钟连续 6 次进全屏, 其中 4 次在 0.0s/0.6s 两拍"隐藏→显示"之后 1 秒内出现
      `fullscreen=False` —— 隐藏 WebView2 控件会把浏览器踢出 HTML 全屏, 紧接着画面
      停在上一次呈现(音频走另一条进程, 照常出声)。用户报的"全屏后画布卡住/弹回"
      就是这个。实验台(`_fs_lab.py`)当初只测到 toggle 不会退全屏, 没覆盖这条路径。

    "隐藏→显示"确实能在 m02002 那类老 GPU/混合显卡上重挂合成层, 所以保留为逃生口:
    画面真黑时设 MSX_FS_HARD=1 换回旧行为, 不必发版。
    """
    return os.environ.get("MSX_FS_HARD", "0") != "1"


def _fs_taps():
    """全屏进入后的补呈现节拍: (秒, 是否轻补)。

    轻补(默认)两拍都不碰 Visible, 被顶掉全屏的风险为零:
      0.35s 赶在宿主窗体最大化之后、1.2s 赶在标签栏/导航栏收起之后 —— 这两次布局
      落定才是"合成层没跟上"的源头, 把绘制请求压在它们之后即可。
    硬补(MSX_FS_HARD=1)沿用 v0.4.21 的三拍: 0/0.6s 硬补 + 1.6s 轻补兜底。
    """
    if os.environ.get("MSX_FS_HARD", "0") == "1":
        return ((0.0, False), (0.6, False), (1.6, True))
    return ((0.35, True), (1.2, True))


def _rep_level(i):
    """第 i 次补呈现(0 起)用第几级手段: 前两次最轻, 之后加一次强制重绘。

    **曾经的第 3 级「从父容器摘下再挂回」已彻底删除** —— 它对 WebView2 是破坏性的:
    控件 HWND 与 CoreWebView2 绑定, reparent 会让合成器直接停摆, 画面再也回不来。
    用户报修: 播放视频点全屏 → 画布整块没有视频、界面像卡死, 就是这个级别在反复
    Remove/Add。普通 WinForms 控件可以这么"逼排版", WebView2 不行。
    """
    return 1 if i < 2 else 2


def _frame_flat(hwnd):
    """GDI PrintWindow(PW_RENDERFULLCONTENT) 抓窗口内容 → 是否"整块同一个颜色"。

    True = 平色(合成层没出图, 可疑) / False = 有内容(健康) / None = 判不了(当作还没好)。
    为什么要用"平色"而不是"是不是黑": 视频层被提升为 DComp 独立平面时, GDI 本来就抓不到
    那块(会读到黑), 但页面其余部分(播放器控件/页面底色/弹幕层)是能抓到的 ⇒ 只有**整块
    完全同一个颜色**才是"根本没呈现"的特征, 单看黑会把正常的播放页误判成坏。
    纯 ctypes 实现, 不依赖 PIL(打包体积/依赖都省)。
    """
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32

        class _BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        rc = wintypes.RECT()
        if not u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
            return None
        w, h = rc.right - rc.left, rc.bottom - rc.top
        if w < 32 or h < 32:
            return None
        hdc = u32.GetWindowDC(wintypes.HWND(hwnd))
        mem = g32.CreateCompatibleDC(hdc)
        bmp = g32.CreateCompatibleBitmap(hdc, w, h)
        old = g32.SelectObject(mem, bmp)
        # 先刷成品红: PrintWindow 若压根没画, 结果就是整片品红 → 判"抓不到"而不是"平色"
        br = g32.CreateSolidBrush(0x00FF00FF)
        fr = wintypes.RECT(0, 0, w, h)
        u32.FillRect(mem, ctypes.byref(fr), br)
        g32.DeleteObject(br)
        u32.PrintWindow(wintypes.HWND(hwnd), mem, 2)
        bi = _BIH()
        bi.biSize = ctypes.sizeof(_BIH)
        bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        got = g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        g32.SelectObject(mem, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(wintypes.HWND(hwnd), hdc)
        if not got:
            return None
        raw = buf.raw
        step = max(3, int(((w * h) / 2600.0) ** 0.5))
        seen = {}
        sent = n = 0
        for yy in range(0, h, step):
            base = yy * w * 4
            for xx in range(0, w, step):
                o = base + xx * 4
                b, g, r = raw[o], raw[o + 1], raw[o + 2]
                n += 1
                if r > 200 and b > 200 and g < 90:
                    sent += 1          # 预填品红 = 这一块没画出来
                    continue
                k = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
                seen[k] = seen.get(k, 0) + 1
        if n == 0:
            return None
        if sent * 100.0 / n > 97.0:
            return None                # 基本全是预填色 → 没呈现
        if not seen:
            return True
        top = max(seen.values())
        # 量化后只有 1~2 种色、且占比 ≥98.5% ⇒ 整块平色(深色底), 判定"没出图"
        return len(seen) <= 2 and top >= (n - sent) * 0.985
    except Exception:
        return None


def _rep_cancel(cc):
    """取消某个控件上正在跑的看门狗(控件被换掉/隐藏/销毁时调)。"""
    try:
        with _REP_LOCK:
            st = _REP_REG.pop(id(cc), None)
        if st is not None:
            st["ev"].set()
    except Exception:
        pass


def _rep_tap(ui, ctrls, cc, key, hwnd_box, level, gentle=False):
    """补呈现(在 UI 线程执行)。level 越高手段越重, 全程**不重载页面**:
      1 隐藏→显示→置顶(与旧实现一致)
      2 加一次强制重绘(Invalidate+Update)

    gentle=True: **绝不碰 Visible**, 只置顶 + 重绘。全屏时的兜底拍用它。
    全屏首拍仍走硬补: `_fs_soft` 的实验台实测一次"隐藏→显示"不会退出全屏, 而且是重挂
    合成层的唯一手段; 但个别机器会退出, 所以全屏路径带 fs_guard —— 一旦发现 fs=False
    立刻收工, 不会像旧版那样一路 toggle, 把"卡一下"放大成"卡死"。
    """
    def _do():
        try:
            if key is not None and ctrls.get(key) is not cc:
                return          # 控件已被换掉
            if cc.IsDisposed:
                return
            if not gentle:
                try:
                    hwnd_box["h"] = int(cc.Handle.ToInt32())
                except Exception:
                    pass
                cc.Visible = False
                cc.Visible = True
            # 补呈现有没有把浏览器**踢出全屏**? 在 UI 线程读一次放进 hwnd_box
            # (工作线程别碰 .NET), 供看门狗复核 / 日志留证。
            try:
                core = cc.CoreWebView2
                hwnd_box["fs"] = (None if core is None
                                  else bool(core.ContainsFullScreenElement))
            except Exception:
                hwnd_box["fs"] = None
            cc.BringToFront()
            try:
                _dbg_log("rep tap key=%s gentle=%s vis=%s bounds=%s dock=%s fs=%s"
                         % (key, gentle, cc.Visible, cc.Bounds, cc.Dock,
                            hwnd_box.get("fs")))
            except Exception:
                pass
            if level >= 2 or gentle:
                try:
                    cc.Invalidate(True)
                    cc.Update()
                except Exception:
                    pass
                # DwmFlush: 让 DWM 立刻把这一帧合完。轻补不能切 Visible, 只能靠
                # "重绘 + 刷新合成"把停住的画面推一下; 失败无所谓(纯兜底)。
                try:
                    import ctypes as _ct
                    _ct.windll.dwmapi.DwmFlush()
                except Exception:
                    pass
        except Exception:
            pass
    ui(_do)


def _represent(ui, ctrls, cc, key=None, why="", taps=None, settle=None,
               gentle=False, verify=None, fs_guard=False):
    """补呈现看门狗(模块级, 便于离线测试)。

    到点补呈现 → 校验是否真出图 → 出图即收工; 还黑就继续补并加重手段, 直到 deadline。
    同一控件上重复触发会取消上一个看门狗(避免两个 worker 同时 toggle 造成抖动)。
    任意线程可调。

    gentle=True: 只"置顶+重绘", 不切 Visible(全屏那条路默认是**硬补**, gentle 只是
    MSX_FS_SOFT=1 时的退化形态)。

    taps 的元素可以是秒数, 也可以是 (秒数, gentle) —— 逐拍指定手段。全屏那条路要
    "第一拍立刻硬补、后面兜底不碰 Visible", 靠的就是它。

    verify=False: 不做 GDI 抓屏校验。**视频全屏必须关掉校验** —— 视频层是 DComp 独立
    平面, GDI 抓到的是平色, 校验必然误判"没出图", 于是一直补到 deadline(8 秒), 期间
    反复打扰合成器, 用户体感就是卡死。None = 自动(gentle/legacy 时不校验)。

    fs_guard=True: 每次补完复核浏览器是否还在全屏; 被踢出来就立刻收工。
    以前只在 gentle 时查, 硬补把全屏顶掉了还会继续补 —— 越补越糟。
    """
    try:
        legacy = os.environ.get("MSX_NO_REPRESENT") == "1"
        if verify is None:
            # 默认"补完校验": 但视频全屏那条路必须关掉 —— GDI 抓不到 DComp 视频平面,
            # 会把正常画面judge 成"没出图", 然后一路补到 deadline(体感=卡死)。
            verify = not (gentle or legacy)
        sched = tuple(taps if taps is not None else
                      (_REP_LEGACY_TAPS if legacy else _REP_TAPS))
        wait = _REP_SETTLE if settle is None else settle
        _rep_cancel(cc)
        ev = threading.Event()
        with _REP_LOCK:
            _REP_REG[id(cc)] = {"ev": ev}
        hb = {"h": 0}

        def worker():
            t0 = time.time()
            n = 0
            for i, item in enumerate(sched):
                if isinstance(item, (tuple, list)):
                    at, g = float(item[0]), bool(item[1])
                else:
                    at, g = float(item), gentle
                if ev.wait(max(0.0, at - (time.time() - t0))):
                    return                       # 被取消(控件已换/已隐藏)
                if time.time() - t0 > _REP_DEADLINE:
                    break
                n += 1
                _rep_tap(ui, ctrls, cc, key, hb, _rep_level(i), g)
                if ev.wait(wait):
                    return
                if fs_guard and hb.get("fs") is False:
                    _dbg_log("represent STOP why=%s (补呈现把浏览器踢出了全屏 → 立刻收工)"
                             % why)
                    return
                if not verify:
                    continue        # 不校验: 见 verify 的注释
                if _frame_flat(hb.get("h")) is False:
                    _dbg_log("represent ok why=%s taps=%d dt=%.2f"
                             % (why, n, time.time() - t0))
                    return
            if verify:
                _dbg_log("represent TIMEOUT why=%s taps=%d key=%s (画面仍无出图)"
                         % (why, n, key))
        threading.Thread(target=worker, daemon=True,
                         name="msx-represent").start()
    except Exception:
        pass


def _log_runtime_env():
    """把"和机器强相关"的环境字段落一次盘。

    悬浮窗黑屏这类问题几乎全是机器差异(GPU/驱动/多屏 DPI/WebView2 内核版本) ——
    没有这些字段, 远程定位就只能靠猜。用户报修时第一句该问的就是它们。
    """
    try:
        import platform
        bits = ["os=%s" % platform.platform()]
        try:
            import ctypes
            u32 = ctypes.windll.user32
            try:
                bits.append("sysdpi=%s" % u32.GetDpiForSystem())
            except Exception:
                pass
            bits.append("monitors=%s" % u32.GetSystemMetrics(80))   # SM_CMONITORS
        except Exception:
            pass
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2Environment
            bits.append("wv2=%s"
                        % CoreWebView2Environment.GetAvailableBrowserVersionString())
        except Exception as e:
            bits.append("wv2=?(%r)" % e)
        bits.append("args=%r" % (os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or ""))
        bits.append("basic=%s no_represent=%s"
                    % (_basic_mode(), os.environ.get("MSX_NO_REPRESENT") == "1"))
        _dbg_log("env " + " ".join(bits))
    except Exception:
        pass


def _build_inject_script(port):
    """组装文档创建脚本: Vue + GM垫片 + 内置净化大师 + 用户脚本。"""
    if _basic_mode():
        _dbg_log("build_inject: MSX_BASIC=1 -> 纯网页播放, 不注入任何脚本")
        return ""
    vue = _read_ext_file("vue.global.prod.js")
    shim = _read_ext_file("gm-shim.js")
    # 净化大师默认禁用(其脚本环境曾干扰B站页面图片/评论加载, 用户反馈后移除);
    # 需要时设环境变量 MSX_CLEANER=1 重新启用
    builtin = _read_ext_file("bilibili-cleaner.user.js") if os.environ.get("MSX_CLEANER") == "1" else ""
    if not shim:
        _dbg_log("build_inject: NO SHIM -> inject disabled")
        return ""
    parts = [
        "window.__MSX_PORT__ = %d;" % port,
        'try { fetch("http://127.0.0.1:" + window.__MSX_PORT__ +'
        ' "/api/shell_debug?s=userscript-env-injected", {mode: "no-cors"}); } catch (e) {}',
    ]
    # ── 视频样式干预: **v0.3.1.11dsk 起默认全部撤销** ──────────────────────────
    # 取证(解包 dist\洞鉴虚明v0.2.9.21dsk.exe 里内嵌的 shell_pages):
    #   "能正常工作"的 2.9 对 <video> **没有任何 CSS** —— 没有圆角、没有 opacity、
    #   没有 filter, 且 app_window 里**没有任何浏览器参数**。它注入的只有主题脚本、
    #   广告过滤、赞助跳过。
    # 而我们后来往 <video> 上挂的每一样东西(0.5px 圆角 → opacity:0.999 + filter;
    # 以及画质增强挂在 <video> 上的 CSS 滤镜), 都让视频层**被提升为独立合成平面**,
    # GDI 系截屏(QQ/微信/PrntScr 的 BitBlt)读不到 → 播放器区域黑、UI 正常。
    # 用户实证: 不注入任何脚本(MSX_BASIC=1)时完全正常; 一旦注入就黑。
    # ⇒ 结论: 对 <video> 的任何样式改动本身就是风险源, 默认回到与 2.9 一致的
    #    "完全不碰视频样式"。
    # 排查开关: MSX_OVERLAY_CSS=1 可重新打开本段(配合 MSX_NO_OVERLAY_CSS_MODE 选变体),
    #           用于逐层 A/B 定位。
    if os.environ.get("MSX_OVERLAY_CSS") == "1" and not os.environ.get("MSX_NO_OVERLAY_CSS"):
        _css = _overlay_css()
        if _css:
            parts.append(
                'if ((function(){var h=location.hostname; return h==="bilibili.com"||'
                'h.endsWith(".bilibili.com")})()) { try {\n'
                '  var __msxNs = document.createElement("style");\n'
                '  __msxNs.id = "msx-nooverlay-style";\n'
                '  __msxNs.textContent = ' + _js_str(_css) + ';\n'
                '  (document.head || document.documentElement).appendChild(__msxNs);\n'
                '} catch (e) {} }'
            )
    # Vue/GM垫片只为净化大师服务 —— 它们会污染页面全局(曾致 B 站页面 Vue.use
    # 崩溃 → 评论/懒加载图片全挂), 净化大师禁用时绝不注入
    if builtin:
        if vue:
            parts.append(vue)   # var Vue = ...(全局, 供净化大师使用)
        if shim:
            parts.append(shim)
    if builtin:
        parts.append(
            'if ((function(){var h=location.hostname; return h==="bilibili.com"||'
            'h.endsWith(".bilibili.com")})()) {\n' + builtin + '\n}')
    # 页面诊断钩子: 默认**关闭**。它会在每次请求失败/资源加载失败时调一次
    # window.open("msx-debug:...") —— 每个事件一次 NewWindowRequested IPC + 一次落盘。
    # 实测一个普通播放页就有 ~190 次(96 FETCHFAIL + 97 RESFAIL), 明显拖慢加载、卡 UI。
    # 排查注入链路时临时置 MSX_DEBUG=1 打开。
    if os.environ.get("MSX_DEBUG") == "1":
        parts.append(_DEBUG_HOOK_JS)
    bsb = "" if os.environ.get("MSX_NO_INJECT") else _read_ext_file("bilibili-sponsorblock.js")
    if bsb:
        parts.append('try {\n' + bsb + '\n} catch (e) { console.warn("[msx] sponsorblock", e); }')
    bq = _read_ext_file("bilibili-enhance.user.js")
    if bq and _bq_enabled():
        # 画质增强(锐化+色彩滤镜): 无 Vue/GM 依赖, 纯 CSS/SVG, 开关与档位走服务端设置
        bq_on = "0" if app.sget("ui_bq") == "0" else "1"
        bq_level = (app.sget("ui_bq_level") or "中").strip()[:6]
        if "<" in bq_level or '"' in bq_level:
            bq_level = "中"   # 白名单化: 该值会被拼进页面脚本
        parts.append('window.__MSX_BQ__ = %s; window.__MSX_BQ_LEVEL__ = "%s";\ntry {\n'
                     % (bq_on, bq_level) + bq + '\n} catch (e) { console.warn("[msx] quality", e); }')
    # 评论筛选(其他设置 · 评论筛选): **与「自有UI」解耦** —— 不管 ui_ownui 开关如何、
    # 也不管页面用的是自建评论区还是 B站原生评论区, 都必须生效。
    # 做法: extensions/bilibili-cfilter.user.js 在 document-start 劫持
    # api.bilibili.com/x/v2/reply* 的 fetch/XHR **响应体**, 本地把该隐藏的评论摘掉。
    # (旧实现只在"自建评论区"内过滤; 而 B站 现在的评论区根元素是 #commentapp,
    #  自建面板的挂载选择器 #comment/.comment-container/.comment 全部落空 → 过滤从未执行。)
    # 黑名单 = 左侧「黑白名单 → 作者」+ (已开启同步时)B站拉黑名单(ablock)。
    # 另有两个账号级开关(都在「评论筛选 → B站」里, 默认关):
    #   · mystery —— 无个性简介(空简介)账号的评论: 用响应自带的 member.sign 就地判定,
    #                零请求零延迟(顶层与楼中楼实测都带 sign);
    #   · lottery —— 动态抽奖账号的评论: 评论本身看不出, 由页面脚本问壳的
    #                /api/bili/flags(只读 author_meta 缓存), 未命中的作者进后台限速
    #                补图队列 —— 网络等待永远不在评论加载的关键路径上。
    #   · empty   —— 空首页账号(个人空间一条动态都没有)的评论: 同 lottery, 走同一份缓存。
    # 读失败注入 null → 脚本自检后不装钩子(回退现状)。
    cf_js = "null"
    try:
        _cf_blk = {r[0] for r in app.db().execute(
            "SELECT value FROM blacklist WHERE kind='author'")}
        if app.sget("m_blocklist_sync") == "1":
            _cf_blk |= {r[0] for r in app.db().execute("SELECT name FROM ablock")}
        try:
            _cf_lvl = int(app.sget("cf_min_level") or 2)
        except Exception:
            _cf_lvl = 2
        # ★ v0.3.1.45dsk: lvl 现在**只作用于评论**(cfilter + ownui 的自建评论区) ——
        #   评论接口响应自带 member.level_info, 纯本地零请求。
        #   弹幕侧已不再读它: 弹幕接口不带等级, 判等级必须「逆推哈希 → 联网查资料」,
        #   候选爆炸会把账号打进 -352 风控(详见 bilibili-dmfilter.user.js 文件头)。
        # 弹幕侧黑名单: 已知 mid(B站拉黑名单接口自带 mid / 评论区见过的 name→mid 索引)
        # 正算 crc32 得到 midHash 集合, 页面端 O(1) 查表就地摘弹幕。
        # v0.3.1.32: 弹幕总开关关掉时连这张表都不算(省一次全名单 crc32), 页面端也拿不到。
        _dm_on = app.sget("m_dm_on") != "0"
        try:
            _cf_dmhash = app.dm_blocked_hashes() if _dm_on else []
        except Exception:
            _cf_dmhash = []
        _text_engine = app.text_engine()
        cf_js = json.dumps({
            "dm_on": _dm_on,
            "blk_on": app.sget("cf_blk_on") != "0",
            "blk": sorted(x for x in _cf_blk if x),
            "dmhash": _cf_dmhash,
            "lvl": min(5, max(0, _cf_lvl)),
            "mystery": app.sget("m_block_mystery_bio") == "1",
            "lottery": app.sget("m_block_lottery") == "1",
            "empty": app.sget("m_block_empty_space") == "1",
            # v0.3.1.43dsk 快速文本分析: 只有"总开关 + 该作用范围"都开时才注入 true,
            # 页面端据此决定要不要把弹幕/评论文本送到本地模型 —— 关闭时连请求都不发。
            "ft_on": app.sget("ft_on") == "1",
            "ft_dm": app.sget("ft_on") == "1" and app.sget("ft_dm") == "1",
            "ft_cf": app.sget("ft_on") == "1" and app.sget("ft_cf") == "1",
            # v0.4.15dsk: BERT 单条约 13ms，页面需要收小批量并放宽首屏等待；
            # fastText 回退保持原来的 700 条/400ms，行为不变。
            "ft_engine": _text_engine,
            "ft_batch": 120 if _text_engine == "bert" else 700,
            "ft_wait": 1500 if _text_engine == "bert" else 400,
        }, ensure_ascii=True)
    except Exception:
        import traceback
        traceback.print_exc()
    parts.append('window.__MSX_CF__ = %s;\n' % cf_js)
    cfilter = "" if os.environ.get("MSX_NO_INJECT") else _read_ext_file("bilibili-cfilter.user.js")
    if cfilter:
        parts.append('try {\n' + cfilter + '\n} catch (e) { console.warn("[msx] cfilter", e); }')
    # 弹幕筛选(v0.3.1.22): 劫持 x/v2/dm/web/seg.so 的 protobuf 响应体, 按「弹幕评论筛选」
    # 同一套开关就地摘弹幕; 同时把解析出的弹幕发布到 window.__msxDanmaku 供右栏
    # 「弹幕列表」面板渲染(含点击跳发送者主页)。与评论筛选解耦: ownui 关闭时照样过滤。
    dmfilter = "" if os.environ.get("MSX_NO_INJECT") else _read_ext_file("bilibili-dmfilter.user.js")
    if dmfilter:
        parts.append('try {\n' + dmfilter + '\n} catch (e) { console.warn("[msx] dmfilter", e); }')
    # 滚动停摆自愈(v0.4.18dsk): 播放页 rAF 停跳 → 页面脚本 POST /api/win/repaint,
    # 壳做一次轻量"补呈现"(隐藏→显示 + 置顶 + 强制重绘), 不重载页面、不丢评论。
    repaint = "" if os.environ.get("MSX_NO_INJECT") else _read_ext_file("bilibili-repaint.user.js")
    if repaint:
        parts.append('try {\n' + repaint + '\n} catch (e) { console.warn("[msx] repaint", e); }')
    # 播放页自有UI(评论区/右侧推荐替换为自建面板): 开关 ui_ownui(默认开), 环境变量
    # MSX_NO_OWNUI=1 可整体停用。脚本自身按 hostname+路径门控, 只在 /video/ 激活;
    # 播放器 DOM 一律不碰 —— 超分管线红线。
    ownui = "" if os.environ.get("MSX_NO_OWNUI") else _read_ext_file("bilibili-ownui.user.js")
    if ownui:
        ownui_on = "0" if app.sget("ui_ownui") == "0" else "1"
        # 播放页推荐筛选规则(m_play_related): 与信息流 machine_filter 同源的页面端子集,
        # 词表/正则/拉黑/厌恶词在此序列化, 供 ownui 脚本本地执行; 读失败注入 null → 页面回退现状。
        try:
            mf = {
                "on": app.sget("m_play_related") != "0",
                "min_views": int(app.sget("m_min_views") or 0),
                "max_views": int(app.sget("m_max_views") or 0),
                "max_dur": int(app.sget("m_max_duration") or 0),
                "min_dur": int(app.sget("m_min_duration") or 0),
                "max_age": int(app.sget("m_max_age_months") or 0),
                "dedup": app.sget("m_dedup") == "1",
                "emotion": app.sget("m_emotion") != "0",
                "flame": list(getattr(app, "FLAME_SLURS", ())),
                "cb": list(getattr(app, "CLICKBAIT_KWS", ())),
                "cbre": getattr(app, "CLICKBAIT_RE").pattern if hasattr(app, "CLICKBAIT_RE") else "",
                "dkws": app.dislike_keywords(),
                "ablock": [r[0] for r in app.db().execute(
                    "SELECT name FROM ablock")] if app.sget("m_blocklist_sync") == "1" else [],
            }
            mf_js = json.dumps(mf, ensure_ascii=True)
        except Exception:
            import traceback
            traceback.print_exc()
            mf_js = "null"
        parts.append('window.__MSX_MF__ = %s;\n' % mf_js)
        parts.append('window.__MSX_OWNUI__ = %s;\ntry {\n' % ownui_on + ownui
                     + '\n} catch (e) { console.warn("[msx] ownui", e); }')
    # 页面诊断探针(只读 computed style, sendBeacon 回传): MSX_PROBE=1 时启用
    probe = _read_ext_file("_probe_header.js") if os.environ.get("MSX_PROBE") == "1" else ""
    if probe:
        parts.append('try {\n' + probe + '\n} catch (e) { }')
    # 内置「最高画质」脚本已下线: 用户明确说"让播放器用上账号本就有权的最高档没有意义"
    # (extensions/bilibili-quality.user.js 保留备查, 不再注入)
    # ── 超分(ACNet + Anime4K 回退)注入: 宿主门控 + 按需注入 ────────────────────
    # 2026-10-08 用户反馈"B站加载变慢" + "打开别人个人界面有概率卡很久", 取证结论:
    #   · 超分只服务 <video>, 而播放器只在 www.bilibili.com 的播放页; 旧门控
    #     h.endsWith(".bilibili.com") 把 space.bilibili.com(个人空间)也算了进来,
    #     让无播放器的页面白挂一套 MutationObserver + 900ms 轮询。
    #   · anime4k WebGPU 库(3.4MB) 只有 ACNet 关闭时的 CNN 回退路才用到; ACNet 默认开,
    #     它在每个 B站 文档上都是纯死重(str.transfer + 解析) —— 只在 MSX_ACNET=0 时注入。
    #   · 整包脚本还会被下发到每个子框架(见函数收尾的顶层守卫), 这里再把宿主收紧。
    fg_on = (os.environ.get("MSX_FG") == "1" and app.sget("ui_fg") == "1"
             and not os.environ.get("MSX_NO_FG"))
    sr_on = app.sget("ui_sr") == "1"
    if fg_on and sr_on:
        # 冲突兜底: 补帧与超分都要用画布接管视频画面, GPU 负担也翻倍 —— 只跑补帧
        # (设置页已做互斥, 这里防旧存档/环境变量组合出双开)
        sr_on = False
        _dbg_log("ui_fg + ui_sr both on -> sr suppressed")
    _SR_HOST_JS = ('(function(){var h=location.hostname;'
                   'return h==="bilibili.com"||h==="www.bilibili.com";})()')
    # ACNet(Anime4KCPP f8b4) WebGL2 多趟超分: 上游 GLSL 原文当字符串注入, 运行器在
    # extensions/acnet-webgl.js。默认开(由超分门控决定何时真正启用: need>1.2 且输出≤4.2Mpx);
    # 需要关掉时设环境变量 MSX_ACNET=0。只在播放器宿主 + 超分开启时注入。
    if sr_on and os.environ.get("MSX_ACNET") != "0":
        _acnet_js = _read_ext_file("acnet-webgl.js")
        _acnet_glsl = _read_ext_file(os.path.join("_acnet", "acnet_f8b4.glsl"))
        if _acnet_js and _acnet_glsl:
            _acnet_body = ('window.__MSX_ACNET__ = 1;\nwindow.__MSX_ACNET_GLSL__ = %s;\n'
                           % json.dumps(_acnet_glsl))
            if os.environ.get("MSX_ACNET_DEBUG"):
                _acnet_body += ('window.__MSX_ACNET_DEBUG__ = %s;\n'
                                % json.dumps(os.environ["MSX_ACNET_DEBUG"]))
            if os.environ.get("MSX_ACNET_MAXPASS"):
                _acnet_body += ('window.__MSX_ACNET_MAXPASS__ = %s;\n'
                                % json.dumps(os.environ["MSX_ACNET_MAXPASS"]))
            # 运动区域弱超分权重(0~1): 0 = 旧行为(运动区不超分), 缺省 0.28
            if os.environ.get("MSX_ACNET_MF"):
                _acnet_body += ('window.__MSX_ACNET_MF__ = %s;\n'
                                % json.dumps(os.environ["MSX_ACNET_MF"]))
            if os.environ.get("MSX_ACNET_WANTW"):
                _acnet_body += ('window.__MSX_ACNET_WANTW__ = %s;\n'
                                % json.dumps(os.environ["MSX_ACNET_WANTW"]))
            if os.environ.get("MSX_ACNET_IWMAX"):
                _acnet_body += ('window.__MSX_ACNET_IWMAX__ = %s;\n'
                                % json.dumps(os.environ["MSX_ACNET_IWMAX"]))
            _acnet_body += ('try {\n' + _acnet_js
                            + '\n} catch (e) { console.warn("[msx] acnet", e); }')
            parts.append('if (' + _SR_HOST_JS + ') {\n' + _acnet_body + '\n}')
            _dbg_log("acnet: injected glsl %d chars + runner %d chars (player host only)"
                     % (len(_acnet_glsl), len(_acnet_js)))
    # AI补帧(Framegen)暂时下线(易出bug: document-start注入曾致渲染进程崩溃, 已改
    # DOMContentLoaded, 但保守起见整体停用)。注入代码保留, 需要>0重新启用:
    # 设环境变量 MSX_FG=1 并把设置键 ui_fg 置 1。
    if sr_on:
        # 超分胶水(bilibili-sr.user.js): 只在播放器宿主注入(见上方 _SR_HOST_JS 说明)。
        # anime4k WebGPU 库(3.4MB) 只有 ACNet 关闭时的 CNN 回退路才用得到 —— 默认 ACNet 开,
        # 它纯属死重(每文档/每框架都要传 3.4MB 并解析) ⇒ 只在 MSX_ACNET=0 时才注入。
        sr_lib = ("" if os.environ.get("MSX_ACNET") != "0"
                  else _read_ext_file("anime4k-webgpu.js"))
        sr_glue = _read_ext_file("bilibili-sr.user.js")
        if sr_glue:
            # 档位: 轻/标准/高/旗舰(默认高=CNNx2UL, 与旧版一致) —— 白名单化后拼进页面脚本
            sr_level = (app.sget("ui_sr_level") or "高").strip()[:4]
            if sr_level not in ("轻", "标准", "高", "旗舰"):
                sr_level = "高"
            # 超分分辨率倍率: 1 / 1.25 / 1.5 / 2 (白名单, 会被拼进页面脚本)。
            # 默认必须与设置页 UI 的默认(web/app.js 读档回显 `s.ui_sr_scale || "2"`)一致 ——
            # 旧壳默认 1.25 而 UI 显示 2, 用户看到的和实际跑的不一样。它决定网络输入档位
            # (档位越高越清晰越吃显卡, 无自动升降); 显卡吃不下时帧节奏门隔帧跑网络兜底。
            sr_scale = (app.sget("ui_sr_scale") or "2").strip()[:4]
            if os.environ.get("MSX_SR_SCALE"):        # 测试用覆盖
                sr_scale = os.environ["MSX_SR_SCALE"].strip()[:4]
            if sr_scale not in ("1", "1.25", "1.5", "2"):
                sr_scale = "2"
            # MSX_SR_DEBUG=1: 周期性回报超分状态(画布是否创建/原视频是否隐藏), 排障用
            sr_probe = ""
            if os.environ.get("MSX_SR_DEBUG"):
                # 排障探针: 回报超分画布状态 + 源/显示分辨率(1080p 是否进门控) + 弹幕层是否在
                # 画布之上(逐层读 computed z-index, 只要有一层 <= 画布层级就算不合格)
                sr_probe = ('var __srN = 0;\n'
                            'var __srT = setInterval(function () {\n'
                            '  try {\n'
                            '    var c = document.querySelector("canvas.msx-sr-canvas");\n'
                            '    var v = document.querySelector("#bilibili-player video, .bpx-player-container video");\n'
                            '    var zc = c ? (parseInt(getComputedStyle(c).zIndex, 10) || 0) : null;\n'
                            '    if (!window.__srTick) { window.__srTick = { f: 0, lt: Date.now() };\n'
                            '      (function l(){ window.__srTick.f++; requestAnimationFrame(l); })(); }\n'
                            '    var tk = window.__srTick, nw = Date.now();\n'
                            '    var fps = Math.round((tk.f - (tk.lf || 0)) * 1000 / Math.max(1, nw - tk.lt) * 10) / 10;\n'
                            '    tk.lf = tk.f; tk.lt = nw;\n'
                            '    var dq = -1, tq = -1;\n'
                            '    try { var q = v.getVideoPlaybackQuality();\n'
                            '      dq = q.droppedVideoFrames - (window.__srD0 || 0);\n'
                            '      tq = q.totalVideoFrames - (window.__srT0 || 0);\n'
                            '      window.__srD0 = q.droppedVideoFrames; window.__srT0 = q.totalVideoFrames; } catch (e) {}\n'
                            '    var dm = document.querySelectorAll(".bpx-player-row-dm-wrap,'
                            ' .bpx-player-bas-dm-wrap, .bpx-player-adv-dm-wrap,'
                            ' .bpx-player-cmd-dm-wrap, .bpx-player-dm-mask-wrap, .bili-danmaku-x-dm");\n'
                            '    var dmz = [], dmAbove = true;\n'
                            '    for (var i = 0; i < dm.length && i < 40; i++) {\n'
                            '      var z = parseInt(getComputedStyle(dm[i]).zIndex, 10);\n'
                            '      dmz.push(isNaN(z) ? "auto" : z);\n'
                            '      if (zc !== null && (isNaN(z) || z <= zc)) dmAbove = false;\n'
                            '    }\n'
                            '    window.open("msx-debug:SRPROBE" + __srN + " canvas=" + (c ? c.width + "x" + c.height : "none") +\n'
                            '      " videoHidden=" + !!(v && /msx-sr-hide/.test(v.className)) +\n'
                            '      " level=" + window.__MSX_SR_LEVEL__ +\n'
                            '      " src=" + (v && v.videoWidth ? v.videoWidth + "x" + v.videoHeight : "none") +\n'
                            '      " dispW=" + (v ? Math.round(v.getBoundingClientRect().width) : 0) +\n'
                            '      " dpr=" + (window.devicePixelRatio || 1) +\n'
                            '      " canvasZ=" + zc + " fps=" + fps +\n'
                            '      " dropPer5s=" + dq + "/" + tq +\n'
                            '      " mode=" + (window.__msxSrMode || "none") +\n'
                            '      " builds=" + (window.__msxSrBuilds || 0) +\n'
                            '      " dmCount=" + dm.length + " dmAboveCanvas=" + dmAbove +\n'
                            '      " dmZ=" + dmz.join("/"), "_blank");\n'
                            '  } catch (e) {}\n'
                            '  if (++__srN >= 12) clearInterval(__srT);\n'
                            '}, 5000);\n')
            parts.append(
                'if (' + _SR_HOST_JS + ') {\nwindow.__MSX_SR__ = 1;\n'
                'window.__MSX_SR_LEVEL__ = "%s";\n' % sr_level
                + 'window.__MSX_SR_SCALE__ = %s;\n' % sr_scale
                + ('window.__MSX_SR_FORCE_DISP__ = %s;\n' % os.environ["MSX_SR_FORCE_DISP"]
                   if os.environ.get("MSX_SR_FORCE_DISP") else "")
                + ('window.__MSX_SR_BYPASS_GATE__ = 1;\n' if os.environ.get("MSX_SR_BYPASS_GATE") else "")
                + ('window.__MSX_SR_FORCE_SR__ = 1;\n' if os.environ.get("MSX_SR_FORCE_SR") else "")
                + ('window.__MSX_SR_FORCE_UP2X__ = 1;\n' if os.environ.get("MSX_SR_FORCE_UP2X") else "")
                + ('window.__MSX_SR_FORCE_K1__ = 1;\n' if os.environ.get("MSX_SR_FORCE_K1") else "")
                + ('window.__MSX_SR_TRY_FSR__ = 1;\n' if os.environ.get("MSX_SR_TRY_FSR") else "")
                + sr_lib + '\n' + sr_glue + '\n' + sr_probe + '\n}')
    if fg_on:
        # AI补帧(Framegen): 运行时+权重(~4MB base64)只在 B站页注入, 默认关
        fg_boot = _framegen_boot_js()
        cad = _read_ext_file("framegen-cadence.js")
        prof = _read_ext_file("framegen-profiles.js")
        cont = _read_ext_file("framegen-content.js")
        if fg_boot and cad and prof and cont:
            n = len(fg_boot) + len(cad) + len(prof) + len(cont)
            # MSX_FG_STUB=1: 照常注入全部资源(体积一致)但让 content.js 立即退出 ——
            # 区分"注入体积/IPC 问题"与"Framegen 运行时问题"的调试钩子
            stub = "window.__framegen = 1;\n" if os.environ.get("MSX_FG_STUB") else ""
            if os.environ.get("MSX_FG_DEBUG"):
                # 调试钩子: 预置 localStorage( Framegen 的 chrome.storage 垫片从这里读),
                # 打开它的调试 HUD, 验证引擎是否真的在跑(画面上应出现性能面板)
                stub += ('try { localStorage.setItem("fc_debug", "true");'
                         ' localStorage.setItem("fc_showFps", "true"); } catch (e) {}\n')
                # 25s后回报引擎状态: 视频存在/覆盖画布(overlay canvas)是否已创建/FG浮层是否就位
                stub += ('setTimeout(function () {\n'
                         '  try {\n'
                         '    var v = document.querySelector("#bilibili-player video, .bpx-player-container video");\n'
                         '    var sib = v && v.previousElementSibling;\n'
                         '    var any = v && v.parentElement && v.parentElement.querySelector("canvas");\n'
                         '    var msg = "FGPROBE video=" + !!v + " webgpu=" + !!navigator.gpu +\n'
                         '      " fg=" + !!window.__framegen +\n'
                         '      " sibCanvas=" + !!(sib && sib.tagName === "CANVAS") +\n'
                         '      " anyCanvas=" + !!any +\n'
                         '      " btn=" + !!document.querySelector(".fc-controls-root, .fc-panel");\n'
                         '    window.open("msx-debug:" + msg, "_blank");\n'
                         '  } catch (e) {}\n'
                         '}, 25000);\n')
            # Framegen 原版走 Chrome 扩展的 document_idle; 我们的通道是 document-created
            # (早于 DOM 解析), 实测 document-start 就跑它的引导/观察器会触发渲染进程
            # RESULT_CODE_KILLED_BAD_MESSAGE —— 垫片+引擎推迟到 DOMContentLoaded 执行
            parts.append(
                'if ((function(){var h=location.hostname; return h==="bilibili.com"||'
                'h.endsWith(".bilibili.com")})()) {(function(){\n'
                'function __msxFgRun() {\nwindow.__MSX_FG__ = 1;\n' + stub +
                fg_boot + '\n;\n' + cad + '\n;\n' + prof + '\n;\n' + cont + '\n}\n'
                'try { if (document.readyState === "loading")\n'
                '  document.addEventListener("DOMContentLoaded", __msxFgRun, { once: true });\n'
                'else __msxFgRun(); } catch (e) { console.warn("[msx] framegen", e); }\n'
                # Framegen 原设计要手动点浮层按钮才开始补帧 —— 桌面端开关(ui_fg)开着就
                # 自动点火: 检测到"播放中的视频 + 按钮未激活"时替用户点一次(每文档一次)
                '(function () {\n'
                '  var tries = 0;\n'
                '  var t = setInterval(function () {\n'
                '    tries++;\n'
                '    var b = document.querySelector(\'button[aria-label="Toggle Framegen"]\');\n'
                '    var v = document.querySelector("#bilibili-player video, .bpx-player-container video");\n'
                '    if (b && v && !v.paused && b.dataset.active !== "true") {\n'
                '      try { b.click(); } catch (e) {}\n'
                '      clearInterval(t);\n'
                '    }\n'
                '    if (tries > 60) clearInterval(t);\n'
                '  }, 1000);\n'
                '})();\n'
                '})();}')
            _dbg_log("framegen inject assembled (%d chars)" % n)
        else:
            _dbg_log("framegen inject SKIPPED (asset files missing)")
    if os.environ.get("MSX_TEST_PLAYBACK"):
        # 对照测试钩子: MSX_TEST_PLAYBACK="定位秒,暂停秒" (如 "120,126") ——
        # B站页自动: 跳到定位秒 → 静音播放 → 到暂停秒暂停 → 点「网页全屏」。
        # 全关/开超分两组对照都走同一时间点, 截图才有可比性。
        try:
            _t0, _t1 = [float(x) for x in os.environ["MSX_TEST_PLAYBACK"].split(",")[:2]]
            parts.append(
                'if ((function(){var h=location.hostname; return h==="bilibili.com"||'
                'h.endsWith(".bilibili.com")})()) {(function(){\n'
                'var tries = 0;\n'
                '(function go(){\n'
                '  var v = document.querySelector("#bilibili-player video");\n'
                '  if (!v || !v.videoWidth) { if (++tries < 90) setTimeout(go, 500); return; }\n'
                '  v.muted = true;\n'
                '  try { v.currentTime = %d; } catch (e) {}\n'
                '  v.play();\n'
                '  var iv = setInterval(function () {\n'
                '    if (v.currentTime >= %d) {\n'
                '      v.pause(); clearInterval(iv);\n'
                '      var b = document.querySelector(".bpx-player-ctrl-web");\n'
                '      if (b) b.click();\n'
                '    }\n'
                '  }, 150);\n'
                '})();\n'
                '})();}' % (_t0, _t1))
            _dbg_log("test playback hook: seek=%s pause=%s" % (_t0, _t1))
        except Exception:
            _dbg_log("MSX_TEST_PLAYBACK parse failed")
    for us in _user_scripts():
        parts.append('try { (function(){\n' + us + '\n})(); }'
                     ' catch (e) { console.warn("[msx] userscript", e); }')
    body = "\n;\n".join(parts)
    if not body:
        return body
    # ── 顶层守卫(Req D 核心) ─────────────────────────────────────────────────
    # WebView2 的 document-created 脚本会下发到**每一个 top-level 与子框架文档**
    # (官方原话: applies to all future top level document and child frame navigations)。
    # 个人空间/首页上的广告与嵌入 iframe 会各自把整包注入(含超分资源)再解析一遍 ——
    # 3.4MB × N 个 iframe 就是"打开别人个人界面有概率卡很久"的根源。
    # 所有注入功能(幕布/主题/评论筛选/自有UI/超分)都只面向顶层页面, 故子框架直接跳过。
    # 用块语句(不是函数)包住: var 仍是全局作用域, 不影响 vue.global 的全局 `var Vue`。
    return ('if (window.top === window.self) {\n' + body + '\n}')


def create_pool(win, on_title, on_newtab, theme_getter=None, port=8000, on_nav=None,
                on_fs=None, on_drop=None):
    """创建控件池。须在窗口壳启动后调用(任意线程, 内部自行 marshal)。

    on_title(tab_key, title) / on_newtab(tab_key, url): 站点控件的
    DocumentTitleChanged / NewWindowRequested(UI 线程)回调。
    on_nav(tab_key, url, can_back, can_fwd): 导航状态变化回调 —— 历史可用态/URL
    变化(含首页标签)都经这里推给 ShellApi → 导航工具栏; 非激活标签的推送由
    ShellApi 侧忽略。控件未就绪/标签已关时不上报。
    on_fs(tab_key, on): 网页(视频)进入/退出全屏(bug6) —— 窗口壳据此隐藏标签栏与
    导航栏; 控件本身的几何(铺满客户区/还原)由池自己改, 调用方不用管。
    on_drop(tab_key, url): 外部浏览器把网页/链接拖进窗口(注入脚本经 WebMessage 上报)。
    返回管理器: init/create/show/hide/dispose/get_nav/push_nav/reload/goto/
    navigate_history —— 均以 tab_key 为键(key 0 = 信息流主控件); 控件对象由池内部持有。
    """
    import System.Windows.Forms as WinForms  # noqa: F401  提前加载 CLR(pythonnet)
    box = {"form": None, "theme": None, "stopping": False, "port": port,
           "on_nav": on_nav, "on_fs": on_fs, "on_drop": on_drop}

    def _tab_geo(form, home, key):
        """标签控件的常规几何(锚在信息流主控件 home 上)。创建与全屏退出都走这里。

        用户报修(m03496): 之前给外部页签加了按窗口宽度比例的左留白(约 5.5%), 结果那条
        留白既太宽又像一条独立边框 —— B站播放页顶部的横栏是页面内的元素, 控件整体右移后
        顶栏左侧就空出一块底色。现按用户要求**取消窗口级左留白**(控件与窗口等宽, 顶栏从左缘
        开始, 视觉连续); 「播放页左侧宽一点」改为在页面内部留(见 extensions\\bilibili-ownui.user.js)。"""
        from System.Drawing import Point, Size
        if home is None:
            return (Point(0, 0),
                    Size(max(1, form.ClientSize.Width), max(1, form.ClientSize.Height)))
        return (Point(home.Location.X, home.Location.Y),
                Size(max(1, home.Size.Width), max(1, home.Size.Height)))

    def _ui(fn):
        """marshal 到 UI 线程(窗体未绑定时直接执行, 供测试注入)。"""
        form = box.get("form")
        if form is None:
            fn()
            return

        def go():
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
        try:
            if form.InvokeRequired:
                form.BeginInvoke(WinForms.MethodInvoker(go))
            else:
                go()
        except Exception:
            import traceback
            traceback.print_exc()

    def _ui_later(fn):
        """总是投递到 UI 消息队列(即使当前就在 UI 线程, 也不内联执行)。

        网页全屏的回调(ContainsFullScreenElementChanged)里**不能**直接最大化窗口 /
        改 WebView2 几何: 那是在浏览器的全屏切换回调里重入 SetWindowPos + Resize,
        合成器偶发停摆 —— 用户体感就是"点全屏后画面卡住半天"。投递一拍, 让浏览器
        先把本次全屏切换收尾, 我们下一拍再动窗口。
        """
        form = box.get("form")
        if form is None:
            fn()
            return

        def go():
            try:
                fn()
            except Exception:
                import traceback
                traceback.print_exc()
        try:
            if form.IsDisposed:
                return
            form.BeginInvoke(WinForms.MethodInvoker(go))
        except Exception:
            pass

    def _retext_later(cc, key=None, why="", **kw):
        """补呈现看门狗(见模块级 _represent)。

        旧实现在 0.35/0.9/1.6s 三个写死的时间点各补一次、不校验也不重试 —— 慢机器三次
        全落空就把黑屏永久留下(用户机 vs 开发机的差异根源)。现在改成"补完校验、还黑就
        继续补并加重手段", 出图即收工。需要 UI 线程 → 内部走 _ui; 控件已被销毁/换掉就放弃。
        """
        _represent(_ui, _Pool.ctrls, cc, key, why, **kw)

    class _Pool:
        ctrls = {0: None}   # tab_key -> WebView2 控件(0 = 信息流主控件)
        _bili_reloaded = {}  # id(ctrl) -> 上次重载时的主题(B站深色cookie生效防循环)
        _dead = set()        # 已关闭标签的key: 迟到的create直接丢弃(防僵尸控件)
        # bug6: 网页(视频)全屏状态 —— on 时当前控件铺满整个客户区(标签栏/导航栏让位)
        fs = {"on": False, "key": None}
        _fs_last = {}    # tab_key -> 上次处理过的全屏状态: 同一状态重复事件直接丢
                         # (播放器常常先 requestFullscreen 再改 CSS, WebView2 会连发好几次;
                         #  每次都重排窗口/控件, 画面就抖成"卡死")
        _fs_rec = None   # bug10: 进入网页全屏时的现场(宿主窗体/控件几何/被藏起来的兄弟控件)
        float_of = {}    # bug6: 控件被迁进悬浮窗时记下宿主窗体 → 全屏回调只对主窗口壳隐藏chrome
                         # (名字必须是 float_of: dispose/send_to_window/dock_to_main/on_fs_changed
                         #  四处都按 float_of 读写。曾误写成 _float_of, 导致四处全部 AttributeError,
                         #  且 dispose 里被 except 吞掉 → 控件不销毁、页面继续出声 —— 用户报修 m02020)

        def _is_dark(self):
            try:
                return (theme_getter() or "dark") != "light"
            except Exception:
                return True

        def _apply_theme(self, c, dark):
            """给站点页面注入/移除自动深色(反色+色相旋转, 媒体类元素反转回原色)。"""
            def go():
                try:
                    core = c.CoreWebView2
                    if core is None:
                        return
                    try:   # 站点自身支持 prefers-color-scheme 的跟随(如已适配暗色的站点)
                        from Microsoft.Web.WebView2.Core import CoreWebView2PreferredColorScheme
                        core.Profile.PreferredColorScheme = CoreWebView2PreferredColorScheme(
                            2 if dark else 1)   # Dark/Light
                    except Exception:
                        pass
                    # ---- B站原生深色: 官方深色开关就是 theme_style cookie
                    #      (顶栏头像区的深浅切换写它), 用 CookieManager 直接设置 ----
                    try:
                        cm = core.CookieManager
                        want = "dark" if dark else ""
                        if want:
                            cm.AddOrUpdateCookie(cm.CreateCookie(
                                "theme_style", "dark", ".bilibili.com", "/"))
                        else:
                            cm.DeleteCookies("theme_style", "https://www.bilibili.com")
                        src = str(c.Source or "")
                        if "bilibili.com" in src:
                            # 当前页在B站且该标签还没为此主题重载过 → 重载一次让原生
                            # 深色生效(防循环: 同一标签同一主题只重载一次)
                            if self._bili_reloaded.get(id(c)) != dark:
                                self._bili_reloaded[id(c)] = dark
                                _dbg_log("bili cookie set dark=%s -> reload" % dark)
                                core.Reload()
                        else:
                            # 尚未导航(新建标签在导航前就按主题设好了 cookie): 记下状态。
                            # 否则首个文档 NavigationCompleted 时会白做一次整页重载 ——
                            # 那正是"点开B站闪一下"的来源之一(用户反馈)。
                            self._bili_reloaded[id(c)] = dark
                    except Exception:
                        pass   # 关窗时序中控件可能刚好销毁, 静默即可
                    # ---- 其他站点: 智能反色兜底(自愈定时器在脚本内) ----
                    core.ExecuteScriptAsync(_THEME_DARK_JS if dark else _LIGHT_JS)
                except Exception:
                    import traceback
                    print("[theme] ERROR:", flush=True)
                    traceback.print_exc()
            _ui(go)

        def _apply_page_bg(self, dark):
            """窗体底衬色 = 外部页签左侧留白露出来的那块。取 B站播放页背景色, 留白与页面
            自身底色连成一片(用户本轮要求)。夜间 rgb(23,24,26), 白天白 —— 随主题切换。"""
            def go():
                try:
                    from System.Drawing import Color as _C
                    form = box.get("form")
                    if form is None:
                        return
                    form.BackColor = _C.White if not dark else _C.FromArgb(23, 24, 26)
                except Exception:
                    pass
            _ui(go)

        def _theme_watch(self):
            """轮询主题(主页切黑夜/白天会实时写库) → 对所有站点页面重新应用。"""
            import time as _t
            while not box["stopping"]:
                try:
                    dark = self._is_dark()
                    if dark != box["theme"]:
                        box["theme"] = dark
                        self._apply_page_bg(dark)
                        for k, c in list(_Pool.ctrls.items()):
                            if k != 0 and c is not None:
                                self._apply_theme(c, dark)
                except Exception:
                    pass
                _t.sleep(3)

        def _wire_nav(self, c, tab_key):
            """给控件挂导航状态上报(HistoryChanged/SourceChanged → on_nav 回调)。
            控件 core 未就绪时等 CoreWebView2InitializationCompleted 再挂 ——
            主控件(0号)常在 gui_ready 时早已就绪, 走立即挂分支。"""
            if not box.get("on_nav"):
                nav_on = None
            else:
                nav_on = box["on_nav"]

            def emit(_s=None, _a=None):
                try:
                    if tab_key in _Pool._dead:   # 标签已关: 迟到事件丢弃
                        return
                    core = c.CoreWebView2
                    if core is None:
                        return
                    box["on_nav"](tab_key, str(c.Source or core.Source or ""),
                                  bool(core.CanGoBack), bool(core.CanGoForward))
                except Exception:
                    pass

            def on_fs_changed(_s=None, _a=None):
                """bug6: 网页(视频)全屏状态变化 —— 池改控件几何, 窗口壳显隐标签栏/导航栏。"""
                try:
                    core = c.CoreWebView2
                    on = bool(core.ContainsFullScreenElement) if core is not None else False
                except Exception:
                    on = False
                _dbg_log("fullscreen=%s tab=%s" % (on, tab_key))
                last = _Pool._fs_last.get(tab_key)
                if last is on:
                    return          # 同状态重复事件: 不去重就会反复最大化/还原
                _Pool._fs_last[tab_key] = on

                def _apply_fs():
                    try:
                        self.set_fullscreen(tab_key, on)
                    except Exception:
                        pass
                    cb = box.get("on_fs")
                    if cb is not None:
                        # bug6: 只有主窗口里的页面全屏才动窗口壳的标签栏/导航栏 ——
                        # 悬浮窗里的全屏由 set_fullscreen 自己藏迷你标题栏与贴边热区,
                        # 不该顺手把主窗口的chrome也摘掉。
                        if _Pool.float_of.get(tab_key) is None:
                            try:
                                cb(tab_key, on)
                            except Exception:
                                _dbg_log("on_fs callback FAILED tab=%s" % tab_key)
                # 不在 WebView2 的事件回调里直接改窗口几何(见 _ui_later 的说明)
                _ui_later(_apply_fs)

            def guard_home_nav(_s, a):
                """首页(信息流)只该停在本地服务: 任何外部导航一律取消。
                v0.4.27 用户实测"拖网页进来把首页顶掉"—— 页面 JS 没拦住(或内核直接
                走了原生拖放导航)时, 这里兜底把链接交给 on_drop 开新标签页。"""
                try:
                    uri = str(getattr(a, "Uri", "") or "")
                    low = uri.lower()
                    if not low:
                        return
                    if (low.startswith("http://127.0.0.1:%d" % port) or
                            low.startswith("http://localhost:%d" % port)):
                        return          # 本地界面自身的刷新/回退
                    if not low.startswith(("http://", "https://", "file:")):
                        return          # about:/data:/blob: 等不动
                    a.Cancel = True
                    _dbg_log("home-guard: cancel nav %s" % uri[:300])
                    cb = box.get("on_drop")
                    if cb is not None and low.startswith(("http://", "https://")):
                        threading.Thread(target=cb, args=(0, uri), daemon=True).start()
                except Exception:
                    pass

            def on_core_ready(_s, _a):
                try:
                    core = c.CoreWebView2
                    if nav_on:
                        core.HistoryChanged += emit
                        core.SourceChanged += emit
                    try:   # bug6: 全屏事件与导航上报同一处挂(不依赖 on_nav 是否存在)
                        core.ContainsFullScreenElementChanged += on_fs_changed
                    except Exception as e:
                        _dbg_log("wire_fs(%s) subscribe FAILED: %r" % (tab_key, e))
                    if tab_key == 0:
                        try:   # 首页只允许本地服务: 外部网址一律取消并开新标签页
                            core.NavigationStarting += guard_home_nav
                        except Exception as e:
                            _dbg_log("wire_guard(0) subscribe FAILED: %r" % e)
                except Exception:
                    _dbg_log("wire_nav(%s) subscribe FAILED: %r" % (tab_key, _s))

            try:
                if c.CoreWebView2 is not None:
                    on_core_ready(None, None)
                else:
                    c.CoreWebView2InitializationCompleted += on_core_ready
            except Exception as e:
                _dbg_log("wire_nav(%s) FAILED: %r" % (tab_key, e))

        def init(self):
            """等原生窗体就绪, 记录主控件, 打无边框缩放补丁(WS_THICKFRAME)。"""
            end = time.time() + 30
            form = None
            while time.time() < end:
                f = win.native
                if f is not None and f.IsHandleCreated:
                    form = f
                    break
                time.sleep(0.2)
            if form is None:
                raise RuntimeError("native window form not ready")
            box["form"] = form
            box["theme"] = self._is_dark()
            threading.Thread(target=self._theme_watch, daemon=True,
                             name="msx-theme-watch").start()

            def init_ui():
                try:
                    import ctypes
                    from System.Drawing import Color as _C
                    # 机器相关环境字段落一次盘(OS/DPI/显示器数/WebView2 内核版本/浏览器参数):
                    # 悬浮窗黑屏这类问题几乎全是机器差异, 没有这些字段就只能靠猜。
                    _log_runtime_env()
                    form.BackColor = (_C.FromArgb(23, 24, 26) if self._is_dark() else _C.White)
                    # ↑ 窗体底衬 = 外部页签左侧留白露出来的那块: 取 B站播放页背景色
                    # (夜间 rgb(23,24,26) / 白天白), 与页面底色连成一片(用户本轮要求);
                    # 原来固定 (15,17,21) 会与 B站页面底色之间出现一条分界线。
                    u32 = ctypes.windll.user32
                    hwnd = form.Handle.ToInt32()
                    # 缩放走自建贴边热区(edge 面板), 不再用 THICKFRAME ——
                    # THICKFRAME 会让 DWM 在 Win10 上画一圈浅色边框线(不可调色), 白线的来源
                    try:   # 关掉 DWM 非客户区渲染
                        val = ctypes.c_int(2)   # DWMWA_NCRENDERING_POLICY = DISABLED
                        ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd, 2, ctypes.byref(val), ctypes.sizeof(val))
                    except Exception:
                        pass
                    try:   # Win11 无边框窗仍会被画一圈系统色边框 → 直接请求"不画边框"
                        none_c = ctypes.c_int(-2)   # DWMWA_COLOR_NONE = 0xFFFFFFFE
                        ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd, 34, ctypes.byref(none_c), ctypes.sizeof(none_c))   # DWMWA_BORDER_COLOR
                    except Exception:
                        pass
                    try:   # Win10 给所有窗口画 1px 系统色边框(浅色主题下发白) → 强制深色边框
                        on = ctypes.c_int(1)   # TRUE
                        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE(新旧两个属性号)
                            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                                hwnd, attr, ctypes.byref(on), ctypes.sizeof(on))
                    except Exception:
                        pass
                    # pywebview 的无边框窗口(CreateParams 走 FormBorderStyle.None)丢了
                    # WS_MINIMIZEBOX/WS_SYSMENU, 于是"单击 / 双击任务栏图标最小化、再点还原"
                    # 整个失效(pywebview #1357)。补回这两个样式位: 窗口仍无边框(没有
                    # WS_CAPTION, 不画标题栏), 只是把任务栏按钮的标准行为还回来。
                    try:
                        GWL_STYLE = -16
                        WS_MINIMIZEBOX, WS_SYSMENU = 0x00020000, 0x00080000
                        _st = u32.GetWindowLongW(hwnd, GWL_STYLE)
                        u32.SetWindowLongW(hwnd, GWL_STYLE, _st | WS_MINIMIZEBOX | WS_SYSMENU)
                    except Exception:
                        pass
                    _Pool.ctrls[0] = form.webview
                    # 信息流主控件也上报导航状态(地址栏显示/后退前进按钮启停)
                    self._wire_nav(form.webview, 0)
                except Exception:
                    import traceback
                    traceback.print_exc()
            _ui(init_ui)

        def create(self, url, tab_key):
            """新标签页控件(异步初始化; 标题/新页事件回调 app_window)。"""

            def go():
                try:
                    if tab_key in _Pool._dead:   # 标签已关: 丢弃迟到的创建(否则造出不可见
                        _Pool._dead.discard(tab_key)   # 僵尸控件, 还会抢走键盘焦点)
                        return
                    from Microsoft.Web.WebView2.WinForms import (
                        WebView2, CoreWebView2CreationProperties)
                    from System.Drawing import Color, Point, Size
                    from System import Uri
                    form = box["form"]
                    home = self._ctrl(0)
                    cp = home.CreationProperties
                    props = CoreWebView2CreationProperties()
                    props.UserDataFolder = cp.UserDataFolder
                    props.AdditionalBrowserArguments = (
                        ((cp.AdditionalBrowserArguments or "").strip() + " " +
                         MSX_WV2_CAPTURE_FLAG).strip())
                    c = WebView2()
                    c.CreationProperties = props
                    try:
                        c.DefaultBackgroundColor = Color.FromArgb(255, 15, 17, 21)
                    except Exception:
                        pass
                    # 外部页签画面整体右移20px(逻辑): 左侧留空隙, 右缘与滚动条位置不变;
                    # Anchor 四边让窗口缩放时这个 inset 原样保持。home(信息流)不动。
                    # 几何算法集中在 _tab_geo —— 全屏退出时也用同一套还原。
                    c.Location, c.Size = _tab_geo(form, home, tab_key)
                    c.Anchor = (WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Bottom |
                                WinForms.AnchorStyles.Left | WinForms.AnchorStyles.Right)
                    c.Visible = False

                    def on_ready(_s, _a):
                        try:   # 初始化失败时 CoreWebView2 为 None: 记下真实原因再退出, 否则
                            # 后面每句都抛 NoneType 异常, 标签永远是一块黑控件(排障无据可查)
                            _dbg_log("on_ready key=%s success=%s err=%r" % (
                                tab_key, getattr(_a, "IsSuccess", None),
                                getattr(_a, "InitializationException", None)))
                        except Exception:
                            pass
                        core = c.CoreWebView2
                        if core is None:
                            _dbg_log("on_ready key=%s: CoreWebView2 为 None, 放弃本次初始化" % tab_key)
                            return
                        core.DocumentTitleChanged += lambda _s2, _a2: on_title(
                            tab_key, core.DocumentTitle)
                        try:   # 防闪幕布(document-start 先于首帧): **先注册** ——
                            # 注入脚本要能调到 window.__msx_veil_hold/release
                            core.AddScriptToExecuteOnDocumentCreatedAsync(_NOFLASH_JS)
                            _dbg_log("on_ready: AddScript(noflash) called")
                        except Exception as e:
                            _dbg_log("noflash register FAILED: %r" % e)
                        try:   # 拖入外部网页(document-start 捕获) → WebMessage
                            core.AddScriptToExecuteOnDocumentCreatedAsync(_DROP_OPEN_JS)
                            _dbg_log("on_ready: AddScript(drop-open) called")
                        except Exception as e:
                            _dbg_log("drop-open register FAILED: %r" % e)
                        try:   # 内置油猴环境: 净化大师 + 用户自定义脚本(每次文档创建自动执行)
                            _dbg_log("on_ready: registering document-created script")
                            inj = _build_inject_script(box.get("port") or 8000)
                            if inj:
                                core.AddScriptToExecuteOnDocumentCreatedAsync(inj)
                                _dbg_log("on_ready: AddScript called (%d chars)" % len(inj))
                            else:
                                _dbg_log("on_ready: empty inject script!")
                        except Exception as e:
                            _dbg_log("on_ready FAILED: %r" % e)

                        def on_newwin(_s2, a2):
                            a2.Handled = True
                            u = str(a2.get_Uri())
                            if u.startswith("msx-debug:"):
                                _dbg_log(u[len("msx-debug:"):])
                                return
                            on_newtab(tab_key, u)
                        core.NewWindowRequested += on_newwin

                        def on_msg(_s3, a3):
                            """外部网页被拖进来: 注入脚本发的 WebMessage → 开新标签页。
                            其余消息(含 pywebview 自己的桥消息)一律忽略。"""
                            if tab_key in _Pool._dead:
                                return
                            cb = box.get("on_drop")
                            if cb is None:
                                return
                            try:
                                raw = a3.get_WebMessageAsJson()
                            except Exception:
                                raw = ""
                            u = drop_url_from_message(raw)
                            if not u:
                                mt = drop_miss_types_from_message(raw)
                                if mt:
                                    _dbg_log("drop-miss tab=%s types=%s" % (tab_key, mt))
                                return
                            try:
                                cb(tab_key, u)
                            except Exception:
                                _dbg_log("drop-open callback FAILED tab=%s" % tab_key)
                        core.WebMessageReceived += on_msg

                        def on_nav_done(_s2, _a2):   # 每次整页加载完按当前主题补注入
                            self._apply_theme(c, self._is_dark())
                        core.NavigationCompleted += on_nav_done
                        self._apply_theme(c, self._is_dark())
                        try:   # 脚本就绪后再导航: 保证首个文档也吃到 document-created 脚本
                            c.Source = Uri(url)
                        except Exception as e:
                            _dbg_log("navigate FAILED: %r" % e)
                    c.CoreWebView2InitializationCompleted += on_ready
                    self._wire_nav(c, tab_key)   # 导航状态上报(地址栏/按钮启停)
                    form.Controls.Add(c)
                    _Pool.ctrls[tab_key] = c
                    try:   # 排障(实测有用): EnsureCoreWebView2Async 失败时标签永远不导航,
                        # 屏幕上就是一块黑控件 —— 日志里必须留下真实原因(如 0x80040154 没有注册类)
                        _t = c.EnsureCoreWebView2Async(None)
                        import threading as _thr

                        def _watch(_tt=_t, _k=tab_key):
                            try:
                                _tt.Wait()
                            except Exception as _e:
                                _dbg_log("ensure key=%s FAILED: %r" % (_k, _e))
                                for _ie in list(getattr(_e, "InnerExceptions", []) or []):
                                    _dbg_log("  inner: %r" % (_ie,))
                        _thr.Thread(target=_watch, daemon=True).start()
                    except Exception as _e:
                        _dbg_log("ensure key=%s raise: %r" % (tab_key, _e))
                    # 首次导航延后到 on_ready 内(脚本注册完成后)再发起, 见上面
                except Exception:
                    import traceback
                    traceback.print_exc()
            _ui(go)

        def _ctrl(self, key):
            c = _Pool.ctrls.get(key)
            if c is None and key == 0 and box.get("form") is not None:
                # 自愈: init_ui 若因时序失败, ctrls[0] 永久缺位 → show(0) 永远失活,
                # 关掉外部标签后信息流再也回不来(整窗黑屏)。
                try:
                    c = box["form"].webview
                    _Pool.ctrls[0] = c
                except Exception:
                    pass
            return c

        def _fs_restore(self, ctrl=True):
            """还原进入网页全屏时的现场: 控件几何/锚定、被隐藏的悬浮窗 chrome、宿主窗状态。
            ctrl=False 供「全屏标签被关掉」时用 —— 控件马上要 Dispose, 不再摆位置。"""
            rec = _Pool._fs_rec
            _Pool._fs_rec = None
            if rec is None:
                return
            host, wstate, c, loc, size, anchor, dock, hid, is_float = rec
            # 顺序要紧: 先还原宿主窗体状态。四边锚定的控件会在窗体「最大化 → 普通」时被
            # 按客户区变化重新缩放(实测 980x554 会被缩成 273x130), 所以几何必须放在最后设。
            # 悬浮窗(未动过 WindowState)不用重设, 免得把它的最大化状态也一起改掉。
            if not is_float:
                try:
                    host.WindowState = wstate
                except Exception:
                    pass
            for s in hid:
                try:
                    s.Visible = True
                except Exception:
                    pass
            if ctrl and c is not None:
                for setter in (lambda: setattr(c, "Dock", dock),
                               lambda: setattr(c, "Anchor", anchor),
                               lambda: setattr(c, "Location", loc),
                               lambda: setattr(c, "Size", size)):
                    try:
                        setter()
                    except Exception:
                        pass

        def _same_form(self, a, b):
            """两个 Form 是不是同一个窗口。

            **不能用 is**: pywebview 的 native 每次访问可能返回新的托管包装对象,
            同一个窗口 `a is b` 会是 False —— 那会让"主窗口"被判成"悬浮窗",
            走进悬浮窗分支(不最大化、不 Dock=Fill), 全屏就只能靠页面自己铺满。
            按 HWND 比较才是可靠的。
            """
            if a is None or b is None:
                return a is b
            try:
                return int(a.Handle.ToInt64()) == int(b.Handle.ToInt64())
            except Exception:
                try:
                    return bool(a.Equals(b))
                except Exception:
                    return False

        def _fs_desc(self, c):
            """一行描述控件关键状态(全屏诊断用; 必须在 UI 线程调)。"""
            try:
                z = -1
                try:
                    z = int(c.Parent.Controls.GetChildIndex(c))
                except Exception:
                    pass
                return ("vis=%s bounds=%s dock=%s z=%s parent=%s"
                        % (c.Visible, c.Bounds, c.Dock, z, type(c.Parent).__name__))
            except Exception as e:
                return "desc-err:%r" % (e,)

        def set_fullscreen(self, key, on):
            """bug6/bug10: 网页(视频)全屏 → 控件铺满客户区; **主窗口**的宿主窗体一起最大化。
            只把控件铺满窗口是不够的: 窗口本身比屏幕小时四周(尤其左缘)会直接露出桌面,
            用户报的「视频播放页窗口全屏时左侧有缝能看到桌面」就是这个。
            主窗口的标签栏/导航栏由窗口壳的 on_fs 回调隐藏; 悬浮窗的迷你标题栏与贴边热区
            是控件的兄弟控件, 这里顺手藏掉。退出时按进入前的现场原样还原。

            用户报修(m03496): 在**悬浮窗**里点视频的全屏按钮时, 画布应当只铺满这个悬浮窗,
            而不是铺满整个屏幕 —— 所以宿主是悬浮窗时**不动 WindowState**, 只把控件铺满它的
            客户区(悬浮窗无边框, 客户区即整个悬浮窗)。"""
            _Pool.fs["on"] = bool(on)
            _Pool.fs["key"] = key if on else None

            def go():
                import System.Windows.Forms as WF
                c = _Pool.ctrls.get(key)
                if c is None:
                    return
                if on:
                    host = c.Parent or box.get("form")
                    if host is None:
                        return
                    try:
                        if not isinstance(host, WF.Form):
                            host = host.FindForm() or host
                    except Exception:
                        pass
                    is_float = not self._same_form(host, box.get("form"))
                    _dbg_log("fs enter key=%s float=%s host=%s state=%s %s"
                             % (key, is_float, type(host).__name__,
                                host.WindowState, self._fs_desc(c)))
                    if _Pool._fs_rec is None:
                        hid = []
                        try:
                            if is_float:   # 悬浮窗: 迷你标题栏+贴边热区
                                for s in list(host.Controls):
                                    try:
                                        if s is not c and s.Visible:
                                            hid.append(s)
                                            s.Visible = False
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                        # 9 项, 与 _fs_restore 的解包一一对应(几何在改 Dock 之前取快照)
                        _Pool._fs_rec = (host, host.WindowState, c,
                                         c.Location, c.Size, c.Anchor, c.Dock, hid, is_float)
                    try:
                        if is_float:
                            # 悬浮窗内全屏: 只铺满悬浮窗自身(位置/尺寸都不变), 不最大化到屏幕
                            c.Dock = getattr(WF.DockStyle, "None")   # None 是 Python 关键字
                            from System.Drawing import Point as _Pt   # WF 里没有 Point
                            c.Location = _Pt(0, 0)
                            c.Size = host.ClientSize
                            c.Anchor = (WF.AnchorStyles.Top | WF.AnchorStyles.Left
                                        | WF.AnchorStyles.Right | WF.AnchorStyles.Bottom)
                        else:
                            # 宿主窗体最大化 + 控件铺满(最大化后客户区尺寸变了, Dock=Fill 自动跟随)
                            host.WindowState = WF.FormWindowState.Maximized
                            c.Dock = WF.DockStyle.Fill
                        c.BringToFront()
                        # ---- 全屏黑屏修复(用户报修 m02002) ----
                        # 实测(探针 _black_probe.py + 桌面 DC 抓屏): 网页一进入全屏, WebView2 的合成层
                        # 就被打断 —— 画面整块停在上一次呈现(全黑 #0f1115, 连渲染器出图都卡住: CDP
                        # Page.captureScreenshot 超时), 且不会自己恢复(0.7~3.1s 五次采样全黑);
                        # 而在同一控件上补一次"隐藏→显示→置顶"立刻有画面(0.75s 起就正常)。
                        # 与最大化/Dock/chrome 隐藏/--disable-direct-composition-video-overlays 都无关。
                        # ⚠️ 默认**不碰 Visible**(取证见 _fs_soft / _fs_taps): 隐藏 WebView2
                        # 控件会把浏览器踢出 HTML 全屏, 之后画面停住(声音还在), 用户只能自己
                        # 退出全屏。轻补只把重绘压在那两次布局落定之后。
                        _retext_later(c, key, "fullscreen", gentle=_fs_soft(),
                                      verify=False, fs_guard=True, settle=0.25,
                                      taps=_fs_taps())
                        _dbg_log("fs taps key=%s hard=%s" % (key, not _fs_soft()))
                        _dbg_log("fs enter done key=%s state=%s %s"
                                 % (key, host.WindowState, self._fs_desc(c)))
                    except Exception:
                        import traceback
                        _dbg_log("fs enter FAILED key=%s: %s"
                                 % (key, traceback.format_exc().replace("\n", " | ")))
                    finally:
                        _dbg_log("fs enter end key=%s %s"
                                 % (key, self._fs_desc(c)))
                else:
                    self._fs_restore()
                    c.BringToFront()
                    _dbg_log("fs exit key=%s %s" % (key, self._fs_desc(c)))
            _ui(go)

        def show(self, key):
            def go():
                try:
                    c = self._ctrl(key)
                    if c is not None:
                        if _Pool.fs["on"]:
                            # bug6: 全屏期间切标签 → 新控件同样铺满(否则顶部/左缘露出窗体底色)
                            try:
                                c.Dock = WinForms.DockStyle.Fill
                                c.BringToFront()
                            except Exception:
                                pass
                        c.Visible = True
                        c.Focus()
                        _dbg_log("show key=%s fs=%s loc=%s size=%s dock=%s"
                                 % (key, _Pool.fs["on"], c.Location, c.Size, c.Dock))
                except Exception:
                    import traceback
                    traceback.print_exc()
            _ui(go)

        def set_content_top(self, top):
            """窗口 chrome(导航栏/收藏栏)高度变化 → 主窗口内容区整体下移/回缩。
            全屏期间不动(退出全屏按进入前的几何快照还原)。悬浮窗里的控件不归这里管。"""
            def go():
                form = box.get("form")
                if form is None or _Pool.fs.get("on"):
                    return
                from System.Drawing import Point, Size
                top_ = max(0, int(top))
                for c in list(_Pool.ctrls.values()):
                    if c is None:
                        continue
                    try:
                        if not self._same_form(c.FindForm(), form):
                            continue
                    except Exception:
                        continue
                    try:
                        c.Location = Point(0, top_)
                        c.Size = Size(max(1, form.ClientSize.Width),
                                      max(1, form.ClientSize.Height - top_))
                    except Exception:
                        pass
            _ui(go)

        def hide(self, key):
            def go():
                try:
                    c = self._ctrl(key)
                    if c is not None:
                        _rep_cancel(c)   # 控件要藏了: 停掉看门狗, 否则它会把它又 toggle 出来
                        c.Visible = False
                        _dbg_log("hide key=%s loc=%s size=%s fs=%s"
                                 % (key, c.Location, c.Size, _Pool.fs["on"]))
                except Exception:
                    pass
            _ui(go)

        def represent(self, key, why=""):
            """公开的补呈现入口 —— 悬浮窗管理器在"宿主编排变化"(激活/缩放/最大化还原/
            换显示器/DPI 变)时调用。任意线程可调。控件不存在时返回 False。"""
            c = _Pool.ctrls.get(key)
            if c is None:
                return False
            _represent(_ui, _Pool.ctrls, c, key, why or "represent")
            return True

        def rescue(self, key, why="scroll"):
            """页面脚本报「滚动后画布停摆」时的补呈现入口(任意线程可调)。

            只做一次隐藏→显示 + 置顶 + 强制重绘, **不校验 GDI**(停摆的是更新, 不是画面
            有没有内容, 抓屏判不出来), 也不重载页面。页面侧如果还停着, 6 秒后会再问,
            最多 3 次 —— 这样第一次就修好时不会再闪。全屏时退化为 gentle(不碰 Visible),
            否则会把浏览器踢出全屏。
            """
            c = _Pool.ctrls.get(key)
            if c is None:
                return False
            _represent(_ui, _Pool.ctrls, c, key, why or "scroll", taps=(0.0,),
                       verify=False, gentle=bool(_Pool.fs.get("on")))
            return True

        def dispose(self, key):
            def go():
                try:
                    _Pool._dead.add(key)   # 迟到的create看到即弃
                    _Pool.float_of.pop(key, None)
                    _rep_cancel(_Pool.ctrls.get(key))   # 控件即将销毁: 停掉看门狗
                    if _Pool.fs.get("key") == key:   # bug6: 全屏的那个标签被关 →
                        _Pool.fs["on"] = False       # 标签栏/导航栏必须回来, 否则整窗没chrome
                        _Pool.fs["key"] = None
                        try:
                            # bug10: 控件马上要 Dispose, 只还原宿主窗状态与被藏起来的兄弟控件
                            self._fs_restore(False)
                        except Exception:
                            pass
                        cb = box.get("on_fs")
                        if cb is not None:
                            try:
                                cb(key, False)
                            except Exception:
                                pass
                    c = _Pool.ctrls.pop(key, None)
                    if c is not None:
                        c.Visible = False
                        # 控件可能已被迁到悬浮窗(v0.3.1.34) → 按**实际父容器**移除,
                        # 否则 Remove 落空, 控件留在已关窗体的 Controls 里泄漏。
                        par = c.Parent
                        if par is not None:
                            par.Controls.Remove(c)
                        # 用户报修 m02020(关掉悬浮窗后后台还有声音): Dispose 之前先静音 ——
                        # 双保险, 即使 WebView2 的销毁/回收晚一拍, 也不会再出声。
                        try:
                            core = c.CoreWebView2
                            if core is not None:
                                core.IsMuted = True
                        except Exception:
                            pass
                        c.Dispose()
                except Exception:
                    pass
            _ui(go)

        def control(self, key):
            """取控件对象(仅 UI 线程内使用; 供悬浮窗管理器迁移父容器)。"""
            return _Pool.ctrls.get(key)

        def send_to_window(self, key, form, top, after=None):
            """把标签 key 的控件从主窗口迁进独立悬浮窗 form(顶部让出 top 像素给迷你标题栏)。
            控件实例不变 —— 页面状态原样保留, **不重载**。after: reparent 完成后回调(UI 线程)。"""
            def go():
                try:
                    from System.Drawing import Point, Size
                    c = _Pool.ctrls.get(key)
                    if c is None:
                        return
                    old = c.Parent
                    if old is not None:
                        old.Controls.Remove(c)
                    form.Controls.Add(c)
                    _Pool.float_of[key] = form   # bug6: 记下宿主, 全屏回调据此判断要不要摘主窗口chrome
                    c.Location = Point(0, top)
                    c.Size = Size(max(1, form.ClientSize.Width),
                                  max(1, form.ClientSize.Height - top))
                    c.Anchor = (WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Bottom |
                                WinForms.AnchorStyles.Left | WinForms.AnchorStyles.Right)
                    c.Visible = True
                    c.BringToFront()
                    try:
                        c.Focus()
                    except Exception:
                        pass
                    # 用户报修 m02020(悬浮窗播放黑屏): 控件被迁到另一个窗体=同一种"宿主编排变化",
                    # 合成层同样会被打断 → 迁完补几次呈现(实例不变, 页面不重载)。
                    # v0.3.1.42dsk 起改走**补呈现看门狗**: 补完校验是否真出图, 还黑就继续补并加重手段
                    # (慢机器不再因为"三次补救全部落空"而永久黑)。
                    _retext_later(c, key, "to-float")
                except Exception:
                    import traceback
                    traceback.print_exc()
                finally:
                    if after is not None:
                        try:
                            after()
                        except Exception:
                            pass
            _ui(go)

        def dock_to_main(self, key, after=None):
            """把悬浮窗里的控件迁回主窗口内容区(几何按信息流主控件对齐, 右侧留 20px 空隙)。
            控件先置为隐藏, 由 ShellApi._activate 再显示(避免闪一下旧位置)。"""
            def go():
                try:
                    from System.Drawing import Point, Size
                    form = box["form"]
                    c = _Pool.ctrls.get(key)
                    home = self._ctrl(0)
                    if c is None or form is None or home is None:
                        return
                    _rep_cancel(c)   # 控件马上要被隐藏、稍后由 _activate 重新显示 → 先停旧看门狗
                    old = c.Parent
                    if old is not None:
                        old.Controls.Remove(c)
                    form.Controls.Add(c)
                    _Pool.float_of.pop(key, None)
                    c.Location, c.Size = _tab_geo(form, home, key)
                    c.Anchor = (WinForms.AnchorStyles.Top | WinForms.AnchorStyles.Bottom |
                                WinForms.AnchorStyles.Left | WinForms.AnchorStyles.Right)
                    c.Visible = False
                except Exception:
                    import traceback
                    traceback.print_exc()
                finally:
                    if after is not None:
                        try:
                            after()
                        except Exception:
                            pass
            _ui(go)

        def navigate_history(self, key, back):
            """当前标签页内回退/前进(back=False)。执行了导航返回 True, 无历史返回 False。
            必须在 UI 线程调用(鼠标侧键过滤器就在 UI 线程) —— 跨线程时 BeginInvoke
            异步返回 False, 调用方会误判"无历史"。"""
            done = {"v": False}

            def go():
                try:
                    c = self._ctrl(key)
                    core = c.CoreWebView2 if c is not None else None
                    if core is None:
                        return
                    if back and core.CanGoBack:
                        core.GoBack()
                        done["v"] = True
                    elif not back and core.CanGoForward:
                        core.GoForward()
                        done["v"] = True
                except Exception:
                    pass
            _ui(go)
            return done["v"]

        def push_nav(self, key):
            """读当前控件导航状态并主动推一次 on_nav(切标签后补推, 防地址栏显示旧 URL)。"""
            if not box.get("on_nav"):
                return

            def go():
                try:
                    c = self._ctrl(key)
                    core = c.CoreWebView2 if c is not None else None
                    if core is None:
                        return
                    box["on_nav"](key, str(c.Source or ""),
                                  bool(core.CanGoBack), bool(core.CanGoForward))
                except Exception:
                    pass
            _ui(go)

        def reload(self, key):
            """当前标签页整页重载(⟳)。"""
            def go():
                try:
                    c = self._ctrl(key)
                    core = c.CoreWebView2 if c is not None else None
                    if core is not None:
                        core.Reload()
                except Exception:
                    pass
            _ui(go)

        def goto(self, key, url):
            """当前标签页内导航到 url(地址栏回车)。调用方已做 http(s) 白名单校验。"""
            def go():
                try:
                    from System import Uri
                    c = self._ctrl(key)
                    if c is not None:
                        c.Source = Uri(url)
                except Exception:
                    import traceback
                    traceback.print_exc()
            _ui(go)

    return _Pool()
