/* 内置浏览器右侧 AI 面板的前端。
   与主界面同源(127.0.0.1:port), 所以直接走 /api/chat 的 SSE 流, 不跨域、不碰桥。
   和主界面聊天**不是同一个会话**: 这里用 localStorage 记住自己的 cid, 避免两边的
   上下文互相污染(面板在讲"这个网页", 主界面在讲"信息流")。 */
(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };
  var CID_KEY = "msx_panel_cid";
  var S = { cid: null, aiOn: true, busy: false, ctrl: null, lastState: null };

  /* ------------------------------------------------------------ 基础请求 */
  function jget(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) { return r.json(); });
  }
  function jpost(url, body) {
    return fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  /* ------------------------------------------------------------ Markdown(轻量) */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function inline(s) {
    // 行内代码先摘出来, 免得里面的 * / _ 被当成强调
    var codes = [];
    s = s.replace(/`([^`\n]+)`/g, function (_, c) {
      codes.push(c);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
    s = s.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      return "<code>" + esc(codes[+i]) + "</code>";
    });
    return s;
  }
  function md(src) {
    var text = String(src == null ? "" : src);
    if (!text) return "";
    var blocks = [];
    text = text.replace(/```([a-zA-Z0-9_+-]*)\n([\s\S]*?)(?:```|$)/g, function (_, lang, code) {
      blocks.push(code.replace(/\n$/, ""));
      return "\u0001" + (blocks.length - 1) + "\u0001";
    });
    var lines = esc(text).split("\n");
    var out = [], list = null, para = [];
    function flushPara() {
      if (para.length) {
        out.push("<p>" + inline(para.join("<br>")) + "</p>");
        para = [];
      }
    }
    function flushList() {
      if (list) { out.push("</" + list + ">"); list = null; }
    }
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      var t = ln.trim();
      if (!t) { flushPara(); flushList(); continue; }
      if (/^\u0001\d+\u0001$/.test(t)) {
        flushPara(); flushList();
        out.push('<pre><code>' + esc(blocks[+t.replace(/\u0001/g, "")]) + "</code></pre>");
        continue;
      }
      var h = t.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flushPara(); flushList();
        var lv = Math.min(3, h[1].length);
        out.push("<h" + lv + ">" + inline(h[2]) + "</h" + lv + ">");
        continue;
      }
      if (/^&gt;\s?/.test(t)) {
        flushPara(); flushList();
        out.push("<blockquote>" + inline(t.replace(/^&gt;\s?/, "")) + "</blockquote>");
        continue;
      }
      var ul = t.match(/^[-*•]\s+(.*)$/);
      var ol = t.match(/^(\d+)[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        var want = ul ? "ul" : "ol";
        if (list !== want) { flushList(); out.push("<" + want + ">"); list = want; }
        out.push("<li>" + inline(ul ? ul[1] : ol[2]) + "</li>");
        continue;
      }
      if (/^[-=]{3,}$/.test(t)) {
        flushPara(); flushList(); out.push("<hr>"); continue;
      }
      flushList();
      para.push(t);
    }
    flushPara(); flushList();
    return out.join("");
  }

  /* ------------------------------------------------------------ 消息 */
  function scroll(force) {
    var box = $("#msgs");
    var near = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    if (force || near) box.scrollTop = box.scrollHeight;
  }
  function addMsg(who, text) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + (who === "me" ? "me" : "ai");
    var b = document.createElement("div");
    b.className = "bubble";
    if (who === "me") b.textContent = text;
    else b.innerHTML = md(text);
    wrap.appendChild(b);
    $("#msgs").appendChild(wrap);
    scroll(true);
    return b;
  }
  function addAiShell() {
    var wrap = document.createElement("div");
    wrap.className = "msg ai";
    var b = document.createElement("div");
    b.className = "bubble";
    var acts = document.createElement("div");
    acts.className = "acts";
    var phase = document.createElement("div");
    phase.className = "phase";
    phase.textContent = "正在思考…";
    var body = document.createElement("div");
    body.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    b.appendChild(acts); b.appendChild(phase); b.appendChild(body);
    wrap.appendChild(b);
    $("#msgs").appendChild(wrap);
    scroll(true);
    return { bubble: b, acts: acts, phase: phase, body: body };
  }
  function pushAct(shell, label, ok) {
    var d = document.createElement("div");
    d.className = "act " + (ok === false ? "bad" : "ok");
    d.innerHTML = '<span class="k">' + (ok === false ? "✕" : "✓") + "</span><span></span>";
    d.lastChild.textContent = label || "已执行";
    shell.acts.appendChild(d);
    scroll();
  }

  /* ------------------------------------------------------------ 状态轮询 */
  function paintState(st) {
    if (!st) return;
    S.lastState = st;
    document.documentElement.setAttribute("data-theme", st.theme === "light" ? "light" : "dark");
    if (st.accent && /^#[0-9a-fA-F]{6}$/.test(st.accent)) {
      // 强调色跟随主界面(默认 B站粉 #fb7299); 半透明底也跟着算, 否则粉配蓝底很难看
      document.documentElement.style.setProperty("--accent", st.accent);
      var n = parseInt(st.accent.slice(1), 16);
      document.documentElement.style.setProperty("--accent-soft",
        "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + ",.14)");
    }
    var dot = $("#aiDot");
    dot.className = "dot " + (st.ai_on ? "on" : "off");
    dot.title = st.ai_on ? "AI 已配置" : "AI 未配置/已关闭";
    var title = st.title || (st.url ? st.url : "");
    if (!st.tab_ok) title = "当前不是网页标签（信息流自带右侧 AI 聊天）";
    else if (!title) title = "正在读取当前网页…";
    $("#pageTitle").textContent = title;
    $("#pageBar").title = st.url || "";
    S.aiOn = !!st.ai_on;
    // 占位符必须短到一行放得下: textarea 的 placeholder 会换行且不能省略号, 两行就被裁掉。
    // 未配置 AI 的引导不能塞这儿(窄面板一定被截), 改成下面的引导气泡。
    $("#input").placeholder = S.aiOn ? "问问这个页面…" : "先配置 AI";
    paintOffNotice(!S.aiOn);
  }
  // 清空消息区时必须同步补回未配置引导, 否则要等下一次 2.5s 轮询才冒出来
  function clearMsgs() {
    $("#msgs").innerHTML = "";
    paintOffNotice(!S.aiOn);
  }
  // 没配 AI 时给一条能换行的引导(占位符装不下这么多字, 窄面板会被截)
  function paintOffNotice(show) {
    var old = $("#offNotice");
    if (!show) { if (old) old.remove(); return; }
    if (old) return;
    var wrap = document.createElement("div");
    wrap.className = "msg ai";
    wrap.id = "offNotice";
    wrap.innerHTML = '<div class="bubble notice">还没配置 AI。到 <b>设置 → AI 配置</b> '
      + "填上接口地址、模型和 Key, 就能在这里一边看页面一边操作它了。</div>";
    $("#msgs").appendChild(wrap);
    scroll(true);
  }
  function pollState() {
    jget("/api/panel/state").then(paintState).catch(function () {});
  }

  /* ------------------------------------------------------------ 发送 */
  function setBusy(on) {
    S.busy = !!on;
    $("#btnStop").hidden = !on;
    $("#btnSend").disabled = !!on;
    $("#input").disabled = false;
  }

  async function send(text) {
    var t = String(text == null ? $("#input").value : text).trim();
    if (!t || S.busy) return;
    if (text == null) { $("#input").value = ""; autoGrow(); }
    addMsg("me", t);
    var shell = addAiShell();
    var replied = "", lastPaint = 0, got = false;
    S.ctrl = new AbortController();
    setBusy(true);
    var paint = function () {
      shell.body.innerHTML = md(replied);
      scroll();
    };
    try {
      var r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: t, cid: S.cid, stream: true, browser: true,
          effort: "mid", ui: {}
        }),
        signal: S.ctrl.signal
      });
      var ctype = r.headers.get("Content-Type") || "";
      if (ctype.indexOf("application/json") >= 0) {
        var d = await r.json();
        if (d.cid) rememberCid(d.cid);
        if (shell.phase) shell.phase.remove();
        shell.body.innerHTML = md(d.reply || d.error || "（没有返回内容）");
        setBusy(false);
        scroll(true);
        return;
      }
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = "";
      for (;;) {
        var rd = await reader.read();
        if (rd.done) break;
        buf += dec.decode(rd.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          var raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var ev = "message", data = {};
          raw.split("\n").forEach(function (line) {
            if (line.indexOf("event:") === 0) ev = line.slice(6).trim();
            else if (line.indexOf("data:") === 0) {
              try { data = JSON.parse(line.slice(5).trim()); } catch (e) { }
            }
          });
          if (ev === "meta") {
            if (data.cid) rememberCid(data.cid);
          } else if (ev === "phase") {
            if (shell.phase) shell.phase.textContent = data.label || "正在执行操作…";
          } else if (ev === "act") {
            pushAct(shell, data.label, data.ok);
          } else if (ev === "delta") {
            if (!got) { got = true; if (shell.phase) shell.phase.remove(); shell.phase = null; }
            replied += data.t || "";
            var now = Date.now();
            if (now - lastPaint > 90) { lastPaint = now; paint(); }
          } else if (ev === "done") {
            replied = data.reply || replied;
            if (data.cid) rememberCid(data.cid);
            if (data.actions && data.actions.length) {
              shell.acts.innerHTML = "";
              data.actions.forEach(function (a) { pushAct(shell, a.label, a.ok); });
            }
          }
        }
      }
      if (shell.phase) { shell.phase.remove(); shell.phase = null; }
      if (replied) paint();
      else if (!shell.body.textContent.trim() || shell.body.querySelector(".typing")) {
        shell.body.innerHTML = '<span class="msgHint">（没有返回内容）</span>';
      }
    } catch (e) {
      if (shell.phase) shell.phase.remove();
      if (e && e.name === "AbortError") {
        if (replied) paint();
        shell.body.insertAdjacentHTML("beforeend", '<span class="msgHint">（已停止生成）</span>');
      } else {
        shell.body.innerHTML = md("发送失败：" + (e && e.message ? e.message : e));
      }
    } finally {
      setBusy(false);
      pollState();
    }
  }

  function rememberCid(cid) {
    if (!cid) return;
    S.cid = cid;
    try { localStorage.setItem(CID_KEY, String(cid)); } catch (e) { }
  }

  /* ------------------------------------------------------------ 输入框 */
  function autoGrow() {
    var el = $("#input");
    var min = 26;
    el.style.height = "0px";
    el.style.height = Math.max(min, Math.min(132, el.scrollHeight)) + "px";
  }

  /* ------------------------------------------------------------ 启动 */
  function boot() {
    try {
      var saved = localStorage.getItem(CID_KEY);
      if (saved) S.cid = +saved || null;
    } catch (e) { }

    $("#btnSend").onclick = function () { send(null); };
    $("#btnStop").onclick = function () {
      if (S.ctrl) { try { S.ctrl.abort(); } catch (e) { } }
    };
    $("#btnClose").onclick = function () {
      jpost("/api/panel/set", { open: false }).catch(function () { });
    };
    $("#btnNew").onclick = async function () {
      try {
        var r = await jpost("/api/conv", {});
        rememberCid(r && (r.id || r.cid));
      } catch (e) { }
      S.cid = null;
      try { localStorage.removeItem(CID_KEY); } catch (e) { }
      clearMsgs();
      addMsg("ai", "新对话开始了。现在这个网页想让我做什么？");
    };
    $("#input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send(null);
      }
    });
    $("#input").addEventListener("input", autoGrow);
    $("#msgs").addEventListener("click", function (e) {
      var h = e.target.closest(".hint");
      if (!h) return;
      var q = h.dataset.q || "";
      if (/帮 我 搜|帮我搜/.test(q) && !q.trim().replace("帮我搜", "").replace("…", "").trim()) {
        $("#input").value = "帮我搜一下 ";
        $("#input").focus();
        autoGrow();
        return;
      }
      send(q);
    });

    paintState(null);
    pollState();
    setInterval(pollState, 2500);

    // 恢复上一次的对话(同一个 cid 才连得上模型那边的上下文)
    if (S.cid) {
      jget("/api/chat?cid=" + encodeURIComponent(S.cid)).then(function (d) {
        var msgs = (d && d.messages) || [];
        if (!msgs.length) return;
        clearMsgs();
        msgs.slice(-40).forEach(function (m) {
          if (m.role === "user") addMsg("me", m.content);
          else if (m.content) addMsg("ai", m.content);
        });
        scroll(true);
      }).catch(function () { });
    }
    autoGrow();
    $("#input").focus();
  }

  // 调试面: 自测脚本用它往面板里塞一段示例对话再截图(验证 markdown/动作行的真实渲染)。
  // 只是把内部函数挂出去, 不改变任何行为。
  window.__msxPanel = { md: md, add: addMsg, act: pushAct, state: S, clear: clearMsgs };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
