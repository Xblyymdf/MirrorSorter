# -*- coding: utf-8 -*-
"""内置浏览器的「整页翻译」。

菜单里那项「翻译此页面 / 显示原文」落到这里。做法是**沉浸式翻译**的思路:
把页面上可见的文本块采出来 → 交给设置里那套 AI 接口批量翻译 → 原地回写,
原文留在 JS 侧的 map 里, 再点一次就整页还原(不重载页面, 视频不会重头播)。

为什么不做成"调用外部翻译服务": 项目后端是纯标准库, 也不想让用户为翻译再配一份
凭据。AI 接口本来就是用户在用的那一个, 复用它零配置。

几条硬约定:
- ★ 只发**纯文本**, 一条一块, 不带 HTML; 回来按 index 对齐 —— **宁可留空, 不可错位**
  (把 A 段的译文贴到 B 段比不翻更糟)。对齐由 `app.translate_texts` 保证。
- ★ 采集上限 160 块 / 6000 字符: 整页翻一次就够, 无上限会把一次点击变成几百次调用。
- ★ 已经是中文的段落不发(纯中文页面点了只会白烧 token), 全页都没外文就直接提示。
"""
import json
import threading

# 采集: 返回 {items: [...], chars: n, scanned: n}
_TR_COLLECT_JS = r"""
(function(){
  var SKIP = 'script,style,noscript,template,textarea,input,select,option,code,pre,kbd,'
    + 'samp,var,svg,canvas,iframe,video,audio,object,embed,picture,'
    + '[contenteditable=""],[contenteditable="true"]';
  var MAXN = 160, MAXC = 6000, MAXSCAN = 4000;
  var items = [], seen = {}, chars = 0, scanned = 0;
  var CJK = /[\u3400-\u9fff\uf900-\ufaff]/g;
  var LAT = /[A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af]/g;
  function walker(){
    return document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n){
        if (scanned++ > MAXSCAN) return NodeFilter.FILTER_REJECT;
        var raw = n.nodeValue;
        if (!raw) return NodeFilter.FILTER_REJECT;
        var t = raw.replace(/\s+/g, ' ').trim();
        if (t.length < 2) return NodeFilter.FILTER_REJECT;
        var p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        try { if (p.closest(SKIP)) return NodeFilter.FILTER_REJECT; } catch(e) {}
        /* 已经是中文的段落不发: 目标语言就是中文, 发了等于白烧 token */
        var cjk = (t.match(CJK) || []).length;
        var lat = (t.match(LAT) || []).length;
        if (lat < 2 || cjk >= lat) return NodeFilter.FILTER_REJECT;
        try {
          var r = p.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) return NodeFilter.FILTER_REJECT;
        } catch(e) {}
        return NodeFilter.FILTER_ACCEPT;
      }
    });
  }
  try {
    if (!document.body) return {items: [], chars: 0, scanned: 0, error: '页面还没加载完'};
    var w = walker(), n;
    while ((n = w.nextNode())) {
      var t = n.nodeValue.replace(/\s+/g, ' ').trim();
      if (seen[t]) continue;
      seen[t] = 1;
      items.push(t);
      chars += t.length;
      if (items.length >= MAXN || chars >= MAXC) break;
    }
  } catch(e) {
    return {items: items, chars: chars, scanned: scanned, error: String(e).slice(0, 120)};
  }
  return {items: items, chars: chars, scanned: scanned};
})()
"""

_TR_STATE_JS = r"""
(function(){
  var on = !!window.__MSX_TR_ON;
  var n = (window.__MSX_TR_ORIG || []).length;
  return {on: on, n: n};
})()
"""

_TR_RESTORE_JS = r"""
(function(){
  var arr = window.__MSX_TR_ORIG || [];
  var n = 0;
  for (var i = 0; i < arr.length; i++) {
    try { arr[i].node.nodeValue = arr[i].text; n++; } catch(e) {}
  }
  window.__MSX_TR_ORIG = [];
  window.__MSX_TR_ON = false;
  return {ok: true, restored: n};
})()
"""

# 回写: 传一个 {原文: 译文} 的 map。只替换**采集口径完全一致**的文本节点。
_TR_APPLY_TMPL = r"""
(function(map){
  var SKIP = 'script,style,noscript,template,textarea,input,select,option,code,pre,kbd,'
    + 'samp,var,svg,canvas,iframe,video,audio,object,embed,picture,'
    + '[contenteditable=""],[contenteditable="true"]';
  var CJK = /[\u3400-\u9fff\uf900-\ufaff]/g;
  var LAT = /[A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\uac00-\ud7af]/g;
  var arr = window.__MSX_TR_ORIG || (window.__MSX_TR_ORIG = []);
  var done = {};
  for (var k = 0; k < arr.length; k++) { try { done[arr[k].node] = 1; } catch(e) {} }
  var hits = 0, scanned = 0;
  try {
    var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n){
        if (scanned++ > 4000) return NodeFilter.FILTER_REJECT;
        var raw = n.nodeValue;
        if (!raw) return NodeFilter.FILTER_REJECT;
        var t = raw.replace(/\s+/g, ' ').trim();
        if (t.length < 2) return NodeFilter.FILTER_REJECT;
        var p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        try { if (p.closest(SKIP)) return NodeFilter.FILTER_REJECT; } catch(e) {}
        var cjk = (t.match(CJK) || []).length;
        var lat = (t.match(LAT) || []).length;
        if (lat < 2 || cjk >= lat) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n2;
    while ((n2 = w.nextNode())) {
      var raw2 = n2.nodeValue, t2 = raw2.replace(/\s+/g, ' ').trim();
      var tr = map[t2];
      if (typeof tr !== 'string' || !tr) continue;
      if (done[n2]) continue;
      /* 保留原有前后空白, 否则中英混排的间距会塌掉 */
      var lead = raw2.match(/^\s*/)[0], tail = raw2.match(/\s*$/)[0];
      arr.push({node: n2, text: raw2});
      n2.nodeValue = lead + tr + tail;
      hits++;
    }
  } catch(e) {}
  window.__MSX_TR_ON = hits > 0;
  return {ok: true, hits: hits, n: arr.length};
})()
"""


def _js_lit(obj):
    """把 Python 对象塞进 JS 源码。U+2028/2029 在 JS 字符串里是换行, 必须转义。"""
    s = json.dumps(obj, ensure_ascii=False)
    return s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _apply_js(mapping):
    """把 {原文: 译文} 塞进回写脚本。

    模板是一段 IIFE(`(function(map){…})()`), 末尾的 `()` 就是调用参数位 ——
    换成 `(<json>)` 即可, 不要用字符串格式化拼整个模板(里面全是 `{}`, 会被吃掉)。
    """
    body = _TR_APPLY_TMPL.strip()
    tail = "})()"
    assert body.endswith(tail), "回写脚本模板结尾变了"
    return body[: -len(tail)] + "})(%s)" % _js_lit(mapping)


def translate_page(win, api, pages, scheme=None, toast_factory=None):
    """菜单项入口: 已翻译 → 还原; 未翻译 → 采集 + 翻译 + 回写。

    立刻返回(真正的活在后台线程里), 结果与进度走 toast。返回一个说明串给菜单用。
    """
    import app
    import shell_menu

    try:
        ap = api.active_page() or {}
    except Exception:
        ap = {}
    tid = ap.get("id")

    def _toast(text, ms=2200):
        try:
            if toast_factory is not None:
                return toast_factory(text, ms)
            return shell_menu.Toast(win, text, ms=ms, scheme=scheme)
        except Exception:
            return None

    if tid is None:
        _toast("没有可翻译的标签页", 1800)
        return "no-tab"
    if ap.get("pinned"):
        # 走同一个 toast 出口: 直接 new Toast 会让无 GUI 的单测踩到 System.Windows.Forms
        _toast("信息流页面不用翻译")
        return "pinned"

    def run():
        t = _toast("正在读取页面…", 60000)
        try:
            st = pages.exec_js(tid, _TR_STATE_JS, timeout=8.0)
            val = st.get("value") if isinstance(st, dict) else None
            if isinstance(val, dict) and val.get("on"):
                r = pages.exec_js(tid, _TR_RESTORE_JS, timeout=8.0)
                n = 0
                if isinstance(r, dict) and isinstance(r.get("value"), dict):
                    n = int(r["value"].get("restored") or 0)
                if t is not None:
                    t.set("已恢复原文(%d 处)" % n, 1600)
                else:
                    _toast("已恢复原文(%d 处)" % n, 1600)
                return
            got = pages.exec_js(tid, _TR_COLLECT_JS, timeout=15.0)
            val = got.get("value") if isinstance(got, dict) else None
            if not isinstance(val, dict):
                err = (got or {}).get("error") or "页面没有返回文本"
                if t is not None:
                    t.set("翻译失败: %s" % str(err)[:60], 2600)
                return
            items = [str(x) for x in (val.get("items") or []) if str(x).strip()]
            if not items:
                if t is not None:
                    t.set("这一页没有需要翻译的外文", 2200)
                return
            total = len(items)
            if t is not None:
                t.set("正在翻译 0/%d 段…" % total, 60000)

            def prog(done, alln):
                if t is not None:
                    t.set("正在翻译 %d/%d 段…" % (done, alln), 60000)

            outs, err = app.translate_texts(items, to="简体中文", batch=10, progress=prog)
            mapping = {}
            for a, b in zip(items, outs):
                b = str(b or "").strip()
                if b and b != a:
                    mapping[a] = b
            if not mapping:
                if t is not None:
                    t.set(err or "翻译没有返回结果(检查设置 → AI)", 3000)
                return
            res = pages.exec_js(tid, _apply_js(mapping), timeout=20.0)
            hits = 0
            if isinstance(res, dict) and isinstance(res.get("value"), dict):
                hits = int(res["value"].get("hits") or 0)
            if t is not None:
                t.set("已翻译 %d 段 · 菜单里可「显示原文」" % hits, 2600)
            elif hits:
                _toast("已翻译 %d 段" % hits, 2000)
        except Exception as e:
            if t is not None:
                t.set("翻译出错: %s" % str(e)[:60], 3000)

    th = threading.Thread(target=run, daemon=True, name="msx-translate")
    th.start()
    return "started"
