/* 新版界面逻辑 · 左栏结构（导航 / 内容源 / 左下角个人+设置）+ 全屏设置视图
   全部走真实后端 API（与旧版共用同一个 app.py 与同一份 data.db） */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.prototype.slice.call(document.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  // 图标统一走 index.html 里的 sprite，一套画法（16 网格 / 1.5 描边 / 跟随 currentColor）
  const ic = (n, cls) => '<svg class="ic' + (cls ? " " + cls : "") + '" aria-hidden="true">' +
    '<use href="#i-' + n + '"/></svg>';
  // 封面图加载失败(防盗链/过期/404): 别留一个空图位 —— 给卡片补 .text 当文字帖排版
  // (见 style.css .card.text), 正文顶上来, 缩略图那一列整个去掉。
  window.msImgFail = function (img) {
    const card = img && img.closest ? img.closest(".card") : null;
    if (card) card.classList.add("text");
    if (img && img.parentNode) img.parentNode.removeChild(img);
  };

  async function api(path, opt) {
    const o = Object.assign({ headers: { "Content-Type": "application/json" } }, opt || {});
    const r = await fetch(path, o);
    const ct = r.headers.get("Content-Type") || "";
    return ct.indexOf("json") >= 0 ? r.json() : { ok: r.ok };
  }
  // 写操作统一走这里：失败一律提示（成功不提示，见 toast 的约定）
  async function save(path, body, label) {
    try {
      const r = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
      // 后端拒绝（{ok:false, error}）也算失败 —— 原来只看网络异常，参数不全这类会被当成功继续走
      if (r && r.ok === false) {
        toast((label || "操作") + "失败：" + (r.error || "服务端拒绝"), true);
        return null;
      }
      return r;
    } catch (e) { toast((label || "操作") + "失败：" + e, true); return null; }
  }
  // 操作成功一律静默；只有失败 / 消费提醒 / 未实现占位才弹（warn=true）
  function toast(msg, warn) {
    if (!warn) return;
    const d = document.createElement("div");
    d.className = "toast";
    d.textContent = msg;
    $("#toastWrap").appendChild(d);
    setTimeout(() => d.remove(), 3200);
  }
  function fmt(n) {
    n = Number(n) || 0;
    if (n >= 1e8) return (n / 1e8).toFixed(1) + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
    return String(n);
  }
  function dur(s) {
    s = Math.floor(Number(s) || 0);
    if (!s) return "";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const p = (v) => String(v).padStart(2, "0");
    return (h ? h + ":" + p(m) : String(m)) + ":" + p(x);
  }
  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 3600) return Math.max(1, Math.floor(s / 60)) + "分钟前";
    if (s < 86400) return Math.floor(s / 3600) + "小时前";
    return Math.floor(s / 86400) + "天前";
  }
  // 强调色派生：从十六进制算半透明变体和两个「能当文字/能托白字」的版本写进 CSS 变量，
  // 换强调色时整套界面跟着走（不用 color-mix：老 Android WebView 不支持）
  function rgbTriple(hex) {
    const m = String(hex || "").replace("#", "");
    const s = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
    if (!/^[0-9a-f]{6}$/i.test(s)) return null;
    const n = parseInt(s, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function rgba(hex, a) {
    const t = rgbTriple(hex);
    if (!t) return "rgba(251,114,153," + a + ")";
    return "rgba(" + t[0] + "," + t[1] + "," + t[2] + "," + a + ")";
  }
  function mix(hexA, hexB, t) {
    const a = rgbTriple(hexA), b = rgbTriple(hexB);
    if (!a || !b) return hexA;
    return "#" + a.map((v, i) => Math.round(v * (1 - t) + b[i] * t).toString(16).padStart(2, "0")).join("");
  }
  /* ---------- 链接跳转统一出口（和旧版 openExternal 走同一条路） ----------
     桌面壳(pywebview)里交给壳：open_in_app → 按「信息以新窗口形式打开」开悬浮窗，
     关掉就是标签栏右边多一个标签页；普通浏览器 / 安卓壳 → window.open。
     ⚠️ 壳里绝不能退回 window.open：pywebview 在 OPEN_EXTERNAL_LINKS_IN_BROWSER=False 时
     把 window.open 当成"在当前 WebView 里导航"(on_new_window_request → load_url)，主界面
     会被目标网页整页顶掉，点返回只能重新加载 index.html —— 信息流、滚动位置全丢。 */
  function shellBridge() {
    const a = window.pywebview && window.pywebview.api;
    return (a && typeof a.open_in_app === "function") ? a : null;
  }
  // window.chrome.webview 只在内嵌 WebView2 壳里有，普通浏览器没有 —— 用它判「桥还没注入」
  const inShellWebview = () => !!(window.chrome && window.chrome.webview);
  const shellQueue = [];
  function deliverOpen(url, title) {
    const api = shellBridge();
    if (!api) { window.open(url, "_blank", "noopener"); return; }   // 纯浏览器/安卓壳
    Promise.resolve(api.open_in_app(url, title || "")).then(function (r) {
      if (r !== "ok") toast("打不开这个链接", true);   // 壳里打不开也不许 window.open 顶掉界面
    }).catch(function () { toast("打不开这个链接", true); });
  }
  // AI 下发浏览器指令(打开网页/搜索) → 主窗口新标签页。
  // 壳里必须走桥, 不能 window.open(会把信息流页面顶掉, 见上一条注释)。
  const browserQueue = [];
  function openBrowserTab(url, title) {
    if (!url) return;
    const api = shellBridge();
    if (api) {
      Promise.resolve(api.open_in_app(url, title || "", "tab")).then(function (r) {
        if (r !== "ok") toast("打不开这个链接", true);
      }).catch(function () { toast("打不开这个链接", true); });
      return;
    }
    if (inShellWebview()) { browserQueue.push([url, title || ""]); return; }
    window.open(url, "_blank", "noopener");
  }
  window.addEventListener("pywebviewready", function () {
    while (browserQueue.length) {
      const q = browserQueue.shift();
      openBrowserTab(q[0], q[1]);
    }
  });
  function openExternal(url, title) {
    if (!url) return;
    if (shellBridge()) { deliverOpen(url, title); return; }
    if (inShellWebview()) { shellQueue.push([url, title]); return; }   // 桥未就绪: 排队, 等 ready 再开
    window.open(url, "_blank", "noopener");
  }
  window.addEventListener("pywebviewready", function () {
    while (shellQueue.length) { const q = shellQueue.shift(); deliverOpen(q[0], q[1]); }
  });

  /* ---------- 外部网页拖入 → 内置浏览器新标签（v0.4.27dsk） ----------
     从外部浏览器把地址栏 / 标签页 / 链接拖进窗口: 是 http(s) 链接就开一个新标签页。
     v0.4.27: **认不出链接也一律 preventDefault** —— 旧实现只要漏一次, 内核就把首页
     导航到拖进来的网页(用户实测: 信息流被顶掉)。文件/纯文本只给提示, 绝不导航。
     页面自己发起的拖拽(dragstart)不接管; 普通浏览器里完全不接管。 */
  const dropQueue = [];
  let dropInternal = false;
  document.addEventListener("dragstart", function () { dropInternal = true; }, true);
  document.addEventListener("dragend", function () {
    setTimeout(function () { dropInternal = false; }, 0);
  }, true);
  function dropOpenWith(api, url) {
    Promise.resolve(api.open_in_app(url, "", "tab")).then(function (r) {
      if (r !== "ok") toast("打不开这个链接", true);
    }).catch(function () { toast("打不开这个链接", true); });
  }
  function dropDeliver(url) {
    const api = shellBridge();
    if (api) { dropOpenWith(api, url); return; }
    dropQueue.push(url);   // 只可能是壳里(普通浏览器 dropTargetOk 已整体放行)
  }
  window.addEventListener("pywebviewready", function () {
    while (dropQueue.length) {
      const api = shellBridge(), u = dropQueue.shift();
      if (!api) { dropQueue.unshift(u); break; }
      dropOpenWith(api, u);
    }
  });
  function dropStrictUrl(s) {
    s = String(s == null ? "" : s).replace(/\u0000/g, "").trim();
    if (!s) return "";
    const lines = s.split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const v = lines[i].trim();
      if (v && v.charAt(0) !== "#" && /^https?:\/\/\S+$/i.test(v)) return v;
    }
    return "";
  }
  function dropFromHtml(h) {
    try {
      const m = /href\s*=\s*["']([^"']+)["']/i.exec(String(h || ""));
      if (m && /^https?:\/\/\S+$/i.test(m[1])) return m[1];
    } catch (e) { /* 读不到就算了 */ }
    return "";
  }
  function dropTypes(dt) {
    try { return Array.prototype.slice.call(dt.types || []); } catch (e) { return []; }
  }
  function dropPick(dt) {
    if (!dt) return "";
    const types = dropTypes(dt);
    const order = ["text/uri-list", "text/x-moz-url", "URL", "text/plain"];
    for (let i = 0; i < order.length; i++) {
      if (types.length && types.indexOf(order[i]) < 0) continue;
      let v = "";
      try { v = dt.getData(order[i]); } catch (e) { continue; }
      const u = dropStrictUrl(v) || dropFromHtml(v);
      if (u) return u;
    }
    if (types.indexOf("text/html") >= 0) {
      try { return dropFromHtml(dt.getData("text/html")); } catch (e) { /* 忽略 */ }
    }
    return "";
  }
  /* 部分内核对外部拖拽在 drop 时 getData 读不到, 但 items 里仍有字符串 */
  function dropAsyncPick(dt, done) {
    let answered = false, left = 0;
    let items = [];
    function answer(u) { if (!answered) { answered = true; done(u || ""); } }
    try { items = Array.prototype.slice.call((dt && dt.items) || []); } catch (e) { items = []; }
    for (let i = 0; i < items.length; i++) {
      const it = items[i], kind = String(it.kind || ""), ty = String(it.type || "");
      if (kind !== "string") continue;
      if (!/^text\//i.test(ty) && ty !== "URL") continue;
      left++;
      try {
        it.getAsString(function (s) {
          const u = dropStrictUrl(s) || dropFromHtml(s);
          if (u) { answer(u); return; }
          if (--left <= 0) answer("");
        });
      } catch (e) { if (--left <= 0) answer(""); }
    }
    if (!left) answer("");
  }
  function dropMiss(types) {
    const api = shellBridge();
    if (!api || typeof api.drop_miss !== "function") return;
    try { api.drop_miss(String(types || "").slice(0, 200)); } catch (e) { /* 忽略 */ }
  }
  function dropEditable(el) {
    for (let t = el; t && t.nodeType === 1; t = t.parentNode) {
      const n = t.nodeName;
      if (n === "INPUT" || n === "TEXTAREA" || n === "SELECT") return true;
      const ce = t.getAttribute && t.getAttribute("contenteditable");
      if (ce === "" || ce === "true" || ce === "plaintext-only") return true;
    }
    return false;
  }
  function dropTargetOk(e) {
    if (!shellBridge() && !inShellWebview()) return false;   // 普通浏览器: 不接管
    if (!e.dataTransfer) return false;
    if (dropEditable(e.target)) return false;                // 输入框里拖字交给页面
    return true;                                             // 其余(含文件)一律由壳接管
  }
  document.addEventListener("dragover", function (e) {
    if (!dropTargetOk(e)) return;
    e.preventDefault();
    try { e.dataTransfer.dropEffect = "copy"; } catch (err) { /* 忽略 */ }
  }, true);
  document.addEventListener("drop", function (e) {
    if (!dropTargetOk(e)) return;
    const wasInternal = dropInternal;
    dropInternal = false;
    const dt = e.dataTransfer;
    const u = dropPick(dt);
    if (!u && wasInternal) return;                           // 页面内拖拽: 交还给页面
    e.preventDefault();
    e.stopPropagation();
    if (u) { dropDeliver(u); return; }
    const types = dropTypes(dt);
    dropAsyncPick(dt, function (u2) {
      if (u2) { dropDeliver(u2); return; }
      dropMiss(types.join("|"));
      toast("拖入的内容里没有网页链接", true);
    });
  }, true);


  /* ================= 自动更新  /* ================= 自动更新（仅 Windows 桌面版 exe） ================= */
  let updBusy = false, updTimer = null;
  function updSize(n) {
    n = Number(n) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  }
  function updStopPoll() { if (updTimer) { clearInterval(updTimer); updTimer = null; } }
  function updBody(html) { const b = $("#modal .mbody"); if (b) b.innerHTML = html; }

  async function updCheck() {
    if (updBusy) return;
    updBusy = true;
    const btn = $("#btnCheckUpdate");
    if (btn) btn.disabled = true;
    openModal("检测更新", "<p>正在检查程序与模型更新…</p>", []);
    let exe = null, mdl = null;
    const errs = [];
    try {
      const d = await api("/api/update/check");
      if (d && d.ok !== false) exe = d;
      else if (d && d.error) errs.push("程序：" + d.error);
    } catch (e) { errs.push("程序：" + String((e && e.message) || e)); }
    try {
      const m = await api("/api/model/check");
      if (m && m.ok !== false) mdl = m;
      else if (m && m.error) errs.push("模型：" + m.error);
    } catch (e) { errs.push("模型：" + String((e && e.message) || e)); }
    updBusy = false;
    if (btn) btn.disabled = false;
    const hasExe = !!(exe && exe.has_update);
    const hasMdl = !!(mdl && mdl.has_update);
    if (!hasExe && !hasMdl) {
      if (!exe && !mdl) {
        openModal("检测更新", "<p>检查更新失败。</p>" +
          (errs.length ? '<p class="tip">' + esc(errs.join("；")) + "</p>" : ""),
          [{ t: "知道了", fn: closeModal }]);
        return;
      }
      const parts = [];
      if (exe) parts.push("程序 v" + (exe.current || "—"));
      if (mdl) parts.push("模型 " + (mdl.current || "未安装") +
        (mdl.message ? "（" + mdl.message + "）" : ""));
      openModal("检测更新", "<p>" + esc(parts.join("、")) + "，均为最新。</p>" +
        (errs.length ? '<p class="tip">' + esc(errs.join("；")) + "</p>" : ""),
        [{ t: "知道了", fn: closeModal }]);
      return;
    }
    let body = "";
    if (hasExe) {
      const notes = String(exe.notes || "").trim();
      body += "<p>程序：v" + esc(exe.current) + " → <b>v" + esc(exe.latest) + "</b>" +
        (exe.size ? "（" + esc(updSize(exe.size)) + "）" : "") + "</p>" +
        (notes ? '<div class="updNotes">' + esc(notes) + "</div>" : "");
    }
    if (hasMdl) {
      const notes = String(mdl.notes || "").trim();
      body += "<p>模型：" + esc(mdl.current || "未安装") + " → <b>" + esc(mdl.latest) + "</b>" +
        (mdl.size ? "（" + esc(updSize(mdl.size)) + "）" : "") + "</p>" +
        (notes ? '<div class="updNotes">' + esc(notes) + "</div>" : "");
    }
    body += '<p class="tip">程序更新会下载 exe，校验后自动重启；模型更新只替换模型文件，设置与数据都保留。</p>';
    if (errs.length) body += '<p class="tip">' + esc(errs.join("；")) + "</p>";
    const btns = [{ t: "稍后", fn: closeModal }];
    if (hasExe) btns.push({ t: "更新程序", fn: () => updDownload(exe) });
    if (hasMdl) btns.push({ t: "更新模型", fn: () => mdlDownload(mdl) });
    if (hasExe && hasMdl) btns.push({ t: "都更新", fn: () => updBoth(exe, mdl) });
    openModal("发现更新", body, btns);
  }

  async function updDownload(d) {
    if (updBusy) return;
    updBusy = true;
    updStopPoll();
    openModal("更新到 v" + esc(d.latest),
      "<p>正在下载更新包…</p>" +
      '<div class="updBar"><i id="updBarFill"></i></div>' +
      '<div class="updMeta"><span id="updPct">0%</span><span id="updBytes"></span></div>' +
      '<p class="tip">下载期间此窗口会保持打开，完成后自动重启。</p>',
      [], { locked: true });
    const paint = (st) => {
      const total = Number(st.total) || 0, got = Number(st.received) || 0;
      const pct = total ? Math.min(100, Math.round(got * 100 / total)) : 0;
      const fill = $("#updBarFill"), p = $("#updPct"), b = $("#updBytes");
      if (fill) fill.style.width = pct + "%";
      if (p) p.textContent = total ? pct + "%" : "下载中";
      if (b) b.textContent = updSize(got) + (total ? " / " + updSize(total) : "");
    };
    const fail = (msg) => {
      updStopPoll();
      updBusy = false;
      openModal("更新失败", "<p>" + esc(String(msg)) + "</p>", [{ t: "关闭", fn: closeModal }]);
    };
    try {
      const r = await api("/api/update/download", { method: "POST", body: "{}" });
      if (!r || r.ok === false) throw new Error((r && r.error) || "开始下载失败");
    } catch (e) {
      fail((e && e.message) || e);
      return;
    }
    updTimer = setInterval(async () => {
      let st;
      try { st = await api("/api/update/progress"); } catch (e) { return; }   // 单次失败不算失败
      if (!st || st.ok === false) return;
      paint(st);
      if (st.phase === "error") { fail(st.error || "下载失败"); return; }
      if (st.phase !== "ready") return;
      updStopPoll();
      const a = shellBridge();
      if (!a || typeof a.install_update !== "function") {
        fail("当前窗口不支持自动安装，请手动下载更新包。");
        return;
      }
      updBody("<p>下载完成，正在关闭程序并安装…</p>" +
        '<p class="tip">安装包：' + esc(st.path || "") + "</p>" +
        '<p class="tip">程序会自动重启，请稍候。</p>');
      try {
        const rr = await a.install_update();
        if (rr !== "ok") { fail(String(rr || "安装失败").replace(/^err:/, "")); return; }
        updBody("<p>更新已就绪，正在重启…</p>" +
          '<p class="tip">如果没有自动打开，请手动双击程序。</p>');
        updBusy = false;
      } catch (e) {
        fail(String(e));
      }
    }, 500);
  }

  function mdlDownload(d, silent) {
    if (updBusy) return Promise.resolve(false);
    return new Promise((resolve) => {
      updBusy = true;
      updStopPoll();
      openModal("更新模型 " + esc(d.latest || ""),
        "<p>正在下载模型文件" + (d.size ? "（约 " + esc(updSize(d.size)) + "）" : "") + "…</p>" +
        '<div class="updBar"><i id="updBarFill"></i></div>' +
        '<div class="updMeta"><span id="updPct">0%</span><span id="updBytes"></span></div>' +
        '<p class="tip">下载完成后会校验 SHA256 并原子替换；期间请保持程序打开。</p>',
        [], { locked: true });
      const paint = (st) => {
        const total = Number(st.total) || 0, got = Number(st.received) || 0;
        const pct = total ? Math.min(100, Math.round(got * 100 / total)) : 0;
        const fill = $("#updBarFill"), p = $("#updPct"), b = $("#updBytes");
        if (fill) fill.style.width = pct + "%";
        if (p) p.textContent = total ? pct + "%" : "下载中";
        if (b) b.textContent = updSize(got) + (total ? " / " + updSize(total) : "") +
          (st.file ? " · " + st.file : "");
      };
      let settled = false;
      const done = (ok, msg) => {
        if (settled) return;
        settled = true;
        updStopPoll();
        updBusy = false;
        resolve(!!ok);
        if (silent) return;
        if (ok) {
          openModal("模型已更新", "<p>模型 <b>" + esc(d.latest || "") + "</b> 已安装并加载。</p>" +
            '<p class="tip">已经打开的B站页面要刷新一次，新的批量和上下文参数才会生效。</p>',
            [{ t: "知道了", fn: closeModal }]);
          try { loadMachine(); } catch (e) {}
        } else {
          openModal("模型更新失败", "<p>" + esc(String(msg || "下载失败")) + "</p>",
            [{ t: "关闭", fn: closeModal }]);
        }
      };
      (async () => {
        try {
          const r = await api("/api/model/download", { method: "POST", body: "{}" });
          if (!r || r.ok === false) throw new Error((r && r.error) || "开始下载失败");
        } catch (e) { done(false, (e && e.message) || e); return; }
        updTimer = setInterval(async () => {
          let st;
          try { st = await api("/api/model/progress"); } catch (e) { return; }
          if (!st || st.ok === false) return;
          paint(st);
          if (st.phase === "error") { done(false, st.error || "下载失败"); return; }
          if (st.phase !== "ready") return;
          try { await api("/api/model/reload", { method: "POST", body: "{}" }); } catch (e) {}
          done(true);
        }, 500);
      })();
    });
  }
  async function updBoth(exe, mdl) {
    const ok = await mdlDownload(mdl, true);
    if (!ok) return;
    updDownload(exe);
  }

  function updWireShell() {
    const a = shellBridge();
    if (!a || typeof a.shell_info !== "function") return;
    try {
      Promise.resolve(a.shell_info()).then(function (info) {
        const btn = $("#btnCheckUpdate");
        if (btn && info && info.self_update) {
          btn.hidden = false;
          btn.onclick = updCheck;
        }
      }).catch(function () {});
    } catch (e) { /* 桥还没就绪：等 pywebviewready 再来一次 */ }
  }
  updWireShell();
  window.addEventListener("pywebviewready", updWireShell);
  // 打开原文 = 记历史（/api/click）+ 交给统一出口（新窗口 / 新标签页）
  function openItem(key, url, title) {
    // 记历史是附带动作，失败不打断「打开原文」本身
    if (key) api("/api/click", { method: "POST", body: JSON.stringify({ key: key }) }).catch(function () {});
    if (url) openExternal(url, title);
  }

  /* ================= 界面偏好 ================= */
  const PREF_KEY = "ms-new-prefs";
  const prefs = Object.assign(
    { theme: "dark", accent: "#fb7299", layout: "single", cover: "m",
      leftW: 238, rightW: 235, le: false, re: false,
      bgHex: "", bgDepth: 0, font: "small", reason: "hide", statOpen: true,
      // 信息来源是否跟随强调色(用户要求, 默认关闭): "1" 时 B站/贴吧/小黑盒/直播 的来源标识
      // 统一走 --accent-ink, 关着就各用各的品牌色。存成字符串, 和 seg 按钮的 data-srcacc 对齐。
      srcacc: "0",
    // 聊天里改设置前的思考强度: off / low / mid / high(默认中)
    effort: "mid" },
    JSON.parse(localStorage.getItem(PREF_KEY) || "{}")
  );
  const savePrefs = () => localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
  // 推荐理由默认关闭（用户要求）。只改默认值对老存档没用 —— 存档里明明写着 "show"，
  // 所以做一次性迁移：这一版第一次跑起来就把它关掉并写回；之后用户自己改，跟着存档走。
  if (!localStorage.getItem("ms-new-prefs-reason-off")) {
    prefs.reason = "hide";
    try {
      localStorage.setItem("ms-new-prefs-reason-off", "1");
      localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    } catch (e) {}
  }
  // 界面状态（与之区分：prefs 是"设置"，这个是"你现在在哪"）。
  // 刷新前端/重新打开时要回到原处，而不是一律跳回信息流。
  const STATE_KEY = "ms-new-state";
  function loadState() {
    try { return JSON.parse(localStorage.getItem(STATE_KEY) || "{}") || {}; } catch (e) { return {}; }
  }
  function saveState() {
    try {
      localStorage.setItem(STATE_KEY, JSON.stringify({
        view: S.view, lastList: S.lastList, sort: S.sort, src: S.src, q: S.q,
        sec: (typeof currentSec === "function") ? currentSec() : "general",
      }));
    } catch (e) {}
  }
  const SEG_ROWS = [["#themeRow", "theme"], ["#layoutRow", "layout"], ["#coverRow", "cover"],
                    ["#fontRow", "font"], ["#reasonRow", "reason"], ["#srcAccentRow", "srcacc"]];
  const CUSTOM_ACCENT_KEY = "ms-new-custom-accent";
  const ACCENTS = ["#fb7299", "#4a9eff", "#67c23a", "#a06bff", "#ff9f43", "#22c3c3", "#8a94a6", "#e8c341"];
  // 背景颜色家族：空串 = 标准（蓝灰底）。选了颜色就整界面底色跟着它走，色相全程保持，深度只推明度。
  const BGS = [{ hex: "", name: "标准" }, { hex: "#f7f1e6", name: "米白" }, { hex: "#eaf5f6", name: "淡青" },
               { hex: "#ecf6ee", name: "淡绿" }, { hex: "#f3f0fa", name: "淡紫" }, { hex: "#f9eff3", name: "淡粉" }];
  const BG_CUSTOM_KEY = "ms-new-custom-bg";
  function hexToHsl(hex) {
    const t = rgbTriple(hex);
    if (!t) return null;
    const r = t[0] / 255, g = t[1] / 255, b = t[2] / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d) {
      if (mx === r) h = ((g - b) / d) % 6;
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h = Math.round(h * 60); if (h < 0) h += 360;
    }
    const l = (mx + mn) / 2;
    const s = d ? d / (1 - Math.abs(2 * l - 1)) : 0;
    return { h: h, s: Math.round(s * 100) };
  }
  function hslToHex(h, s, l) {
    s /= 100; l /= 100;
    const k = (n) => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = (n) => Math.round(255 * (l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1))));
    return "#" + [f(0), f(8), f(4)].map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, "0")).join("");
  }
  // 深度 1（白天最亮）～ 10（黑夜最暗）。0 = 跟随界面模式（白天 1 / 黑夜 10）。
  const bgDepthNow = () => {
    const d = parseInt(prefs.bgDepth, 10);
    if (d >= 1 && d <= 10) return d;
    return prefs.theme === "light" ? 1 : 10;
  };
  const bgHslAt = (depth) => {
    const t = (depth - 1) / 9;
    const c = hexToHsl(prefs.bgHex);
    const h = c ? c.h : 220;
    const s1 = c ? c.s : 44, s2 = c ? c.s : 18;
    return { h: h, s: Math.round(s1 + (s2 - s1) * t), t: t };
  };
  function applyBg() {
    const el = document.documentElement;
    const { h, s, t } = bgHslAt(bgDepthNow());
    const set = (k, a, b) => el.style.setProperty(k, "hsl(" + h + ", " + s + "%, " +
      (Math.round((a + (b - a) * t) * 10) / 10) + "%)");
    set("--bg", 96, 7);      set("--panel", 100, 11);
    set("--card", 100, 13);  set("--panel2", 93, 14);
    set("--hover", 93, 14);  set("--line", 91, 19);
  }
  function renderBgRow() {
    const box = $("#bgRow");
    if (!box) return;
    const depth = bgDepthNow();
    // 拖动分割线时 applyPrefs 会被逐帧调用，色卡没变就别重建 DOM
    const key = depth + "|" + prefs.bgHex;
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    const customs = (() => { try { return JSON.parse(localStorage.getItem(BG_CUSTOM_KEY) || "[]"); } catch (e) { return []; } })();
    const all = BGS.concat((Array.isArray(customs) ? customs : []).map((c) => ({ hex: c, name: c })));
    box.innerHTML = all.map((x) => {
      const preview = x.hex ? (() => { const c = hexToHsl(x.hex); return c ? hslToHex(c.h, c.s, Math.round(96 + (7 - 96) * ((depth - 1) / 9))) : x.hex; })() : "";
      return '<span class="sw ' + (prefs.bgHex === x.hex ? "on" : "") + '" data-hex="' + esc(x.hex) +
        '" title="' + esc(x.name) + '"' + (preview ? ' style="background:' + preview + '"' : "") + "></span>";
    }).join("");
  }

  // 大图模式的列数要按**缩放后**的真实宽度算: 字体缩放走的是 CSS zoom, 媒体查询量不到这段宽度差,
  // 于是纯 CSS 的 minmax(330px) 在「字体=中」时只排得下两列(用户报的那个 bug)。这里直接量宽度。
  function applyListCols() {
    const el = $("#list");
    if (!el) return;
    if (prefs.layout !== "big") { el.style.gridTemplateColumns = ""; return; }
    const w = el.clientWidth || 0;
    if (!w) return;   // 列表可能正被设置页盖住(display:none), 量不到就别乱设
    const n = w >= 640 ? 3 : w >= 420 ? 2 : 1;
    el.style.gridTemplateColumns = "repeat(" + n + ",minmax(0,1fr))";
  }

  // 设置页/筛选页 v0.3.1.52 起固定单栏（原来按宽度算多列、还要在中间画一条竖线），
  // 列数不再需要 JS 量 —— 量列数的函数和那条竖线的样式一起删了。

  function applyPrefs() {
    const html = document.documentElement;
    // 底色深度就是明暗轴（1 = 白天深度，10 = 黑夜深度），所以正文配色跟着深度走 ——
    // 否则在「黑夜」模式下把深度拉到 3 会得到浅底 + 浅字，什么都看不见。
    // 深度选「跟随模式」时由界面模式决定深度，两者不会打架。
    const dark = bgDepthNow() >= 6;
    html.setAttribute("data-theme", dark ? "dark" : "light");
    html.style.setProperty("--accent", prefs.accent);
    html.style.setProperty("--accent-soft", rgba(prefs.accent, dark ? 0.13 : 0.12));
    html.style.setProperty("--accent-soft2", rgba(prefs.accent, dark ? 0.22 : 0.2));
    // 强调色当「文字」/当「实底托白字」时都要够对比：深色提亮、浅色压暗，两套主题都过 4.5:1
    html.style.setProperty("--accent-ink", dark ? mix(prefs.accent, "#ffffff", 0.22) : mix(prefs.accent, "#000000", 0.34));
    html.style.setProperty("--accent-strong", dark ? mix(prefs.accent, "#0c0f13", 0.42) : mix(prefs.accent, "#000000", 0.28));
    html.style.setProperty("--thumbW", ({ s: 110, m: 156, l: 200, xl: 260 }[prefs.cover] || 156) + "px");
    // 信息来源跟不跟强调色: 整个开关就一个根属性, 样式表里按 html[data-srcacc="1"] 覆盖
    html.setAttribute("data-srcacc", prefs.srcacc === "1" ? "1" : "0");
    const shell = $("#shell");
    shell.classList.toggle("le", !!prefs.le);
    shell.classList.toggle("re", !!prefs.re);
    // 收起 = 栏宽收到 0（分割线落到屏幕边缘当边框线），无动画
    html.style.setProperty("--leftW", (prefs.le ? 0 : prefs.leftW) + "px");
    html.style.setProperty("--rightW", (prefs.re ? 0 : prefs.rightW) + "px");
    const tl = $("#toggleLeft"), tr = $("#toggleRight");
    tl.classList.toggle("collapsed", !!prefs.le);
    tl.title = (prefs.le ? "展开" : "收起") + "左栏";
    tl.setAttribute("aria-label", tl.title);
    tr.classList.toggle("collapsed", !!prefs.re);
    tr.title = (prefs.re ? "展开" : "收起") + "右栏";
    tr.setAttribute("aria-label", tr.title);
    // 左栏「AI 聊天」不再点亮：它不是视图，右栏展开/收起本身就是反馈，
    // 和「信息流」同时高亮会看成两个选中项
    // 只改布局类，不能用 className 整体覆写（会把 hidden 等状态一起抹掉）
    const listEl = $("#list");
    listEl.classList.remove("single", "dual", "big");
    listEl.classList.add(prefs.layout);
    SEG_ROWS.forEach(([sel, key]) => {
      // 界面模式那一行显示的是「当前实际是深还是浅」——深度可能已经把它带过去了
      const val = key === "theme" ? (dark ? "dark" : "light") : prefs[key];
      $$(sel + " .seg").forEach((b) => b.classList.toggle("on", String(b.dataset[key]) === String(val)));
    });
    $$("#accentRow .sw").forEach((b) => b.classList.toggle("on", b.dataset.c === prefs.accent));
    applyBg();
    renderBgRow();
    const sl = $("#bgDepth");
    if (sl) {
      const d = parseInt(prefs.bgDepth, 10);
      sl.value = (d >= 1 && d <= 10) ? d : (prefs.theme === "light" ? 1 : 10);
      $("#bgDepthVal").textContent = (d >= 1 && d <= 10) ? String(d)
        : ("跟随模式 · " + (prefs.theme === "light" ? 1 : 10));
      paintRanges();   // 滑条填充段跟着值走
    }
    // 字体大小 = 整界面缩放。100vh 在 zoom 下会被乘大，布局高度要除回来
    // （否则全屏时界面比视口高一截，侧栏底部永远滚不到）
    const z = prefs.font === "medium" ? 1.15 : prefs.font === "large" ? 1.3 : 1;
    document.body.style.zoom = z === 1 ? "" : String(z);
    html.style.setProperty("--ui-zoom", String(z));
    applyListCols();   // 缩放变了 → 大图模式的列数要重算
    listEl.classList.toggle("noreason", prefs.reason !== "show");
    applyStatCard();
    pushTheme();
  }
  // 今日状态卡的收放。收起来是「界面偏好」而不是「你现在在哪」，所以跟着 prefs 存，
  // 并且要由 applyPrefs 统一应用 —— 只写在点击处理里的话，刷新一次就自己展开了，
  // 用户会觉得上一次收起来白收了。
  function applyStatCard() {
    const body = $("#statBody"), btn = $("#metaToggle");
    if (!body || !btn) return;
    const open = prefs.statOpen !== false;
    body.hidden = !open;
    btn.classList.toggle("off", !open);
    const lbl = open ? "收起状态卡" : "展开状态卡";
    btn.title = lbl;
    btn.setAttribute("aria-label", lbl);
  }
  // 把当前主题/强调色推给服务端：桌面窗口壳与悬浮球读这份，页面重开也能恢复。
  // 拖动分割线会逐帧走 applyPrefs，这里必须合并请求，否则一拖动就是一串 POST。
  let pushTimer = null;
  function pushTheme() {
    if (!themeReady) return;
    if (pushTimer) return;
    pushTimer = setTimeout(function () { pushTimer = null; pushThemeNow(); }, 300);
  }
  function pushThemeNow() {
    const d = bgDepthNow();
    const { h, s, t } = bgHslAt(d);
    const dark = d >= 6;
    const L = (a, b) => Math.round((a + (b - a) * t) * 10) / 10;
    fetch("/api/floatball", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: {
        accent: prefs.accent, light: !dark, accent_id: prefs.accent,
        bg: { bar: hslToHex(h, s, L(92, 3)), active: hslToHex(h, s, L(100, 11)),
              hover: hslToHex(h, s, L(93, 15)), fgon: dark ? "#e8eaf0" : "#20242f",
              fgoff: dark ? "#969eac" : "#5d6675", sep: dark ? "#343944" : "#dfe3ec" },
      } }),
    }).catch(function () {});
  }
  // 桌面壳内 WebView2 的 localStorage 与常用浏览器相互独立：壳内首次启动（本地无偏好）时
  // 从服务端设置恢复主题/强调色。恢复必须先于「把主题推回服务端」，否则开机那一下
  // 会把默认主题推上去，把要恢复的值盖掉。
  let themeReady = !!localStorage.getItem(PREF_KEY);
  (function restoreThemeFromServer() {
    if (themeReady) return;
    fetch("/api/ui_theme", { cache: "no-store" }).then((r) => r.json()).then((d) => {
      if (d && d.theme) {
        prefs.theme = d.theme;
        if (d.accent_hex) prefs.accent = d.accent_hex;
        savePrefs();
      }
    }).catch(function () {}).then(function () {
      themeReady = true;
      applyPrefs();
    });
  })();

  /* ================= 信息流 ================= */
  const S = { view: "feed", lastList: "feed", q: "", sort: "default", offset: 0, src: "",
              session: Math.random().toString(36).slice(2), liked: new Set(), cid: null,
              loading: false, done: false, waiting: false, ctx: null, gen: null, emptyMsg: "",
              // 后端说"一条来源都没配好"时为 true —— 空态据此换成"去配来源"的卡，
              // 而不是一直显示"正在准备内容…"（那种情况下后端根本没有东西可抓）
              setupNeeded: false,
              reqGen: 0 };

  // 直播卡片(v0.3.1.51)：字段语义和视频不同 —— views 是「人气」不是播放量，没有时长/弹幕/评论，
  // 时间显示的是「开播多久」。所以单独一套 markup，不硬塞进视频卡片那套标签里。
  function liveCardHTML(it) {
    const url = it.url || "";
    const online = it.online != null ? it.online : it.views;
    const follow = it.following || it.src === "关注";
    const stat = "人气 " + fmt(online) + (it.live_time ? " · 开播 " + ago(it.live_time) : "");
    // 没封面就不画那个空图位(用户: 空图片显示太丑) —— 直播信息挪到正文那一列,
    // 卡片走文字帖那套排版(.card.text), 高度由文字撑。
    const thumb = it.cover
      ? '<div class="thumb" data-act="open" title="进直播间">' +
        '<img loading="lazy" referrerpolicy="no-referrer" src="' + esc(it.cover) + '" onerror="msImgFail(this)">' +
        '<div class="dur live">直播中</div>' +
        '<div class="stats"><span>人气 ' + fmt(online) + "</span>" +
          (it.live_time ? "<span>开播 " + ago(it.live_time) + "</span>" : "") + "</div></div>"
      : "";
    return '<article class="card live' + (it.cover ? "" : " text") + '" data-key="' + esc(it.key) +
      '" data-author="' + esc(it.author) +
      '" data-plat="' + esc(it.platform || "B站直播") + '" data-url="' + esc(url) + '" data-bvid="">' +
      // 同上: 没有封面的直播间也给一块文字封面, 标题才能和别家卡片对齐
      (it.cover ? "" : '<div class="tpanel" data-act="open" title="进直播间"><div class="tq">' +
        esc(hasBody(it.desc) ? it.desc : (follow ? "关注的主播正在直播" : "直播推荐")) + "</div></div>") +
      '<h3><span class="pb live">直播中</span>' +
        (follow ? '<span class="pb follow">关注</span>' : "") +
        '<span class="t" data-act="open" title="' + esc(it.title) + '">' + esc(it.title) + "</span></h3>" +
      // 同上: 大图模式作者落在标题下方(强调色)。直播卡片原来把作者/分类放在 .col 那行 meta 里,
      // 而大图模式会把整行 meta 藏掉、封面也不是缩略图那种角标结构 —— 分类跟着作者一起挪到这行。
      (it.cover ? '<div class="byline"><span class="au">' + esc(it.author || "主播") + "</span>" +
        (it.tname ? '<span class="tn"> · ' + esc(it.tname) + "</span>" : "") + "</div>" : "") +
      '<div class="media">' + thumb +
        '<div class="col"><div class="meta"><span class="au">' + esc(it.author || "主播") + "</span>" +
          (it.tname ? " · " + esc(it.tname) : "") + "</div>" +
        (it.cover ? "" : '<div class="meta">' + stat + "</div>") +
        (hasBody(it.desc) ? '<div class="desc">' + esc(it.desc) + "</div>" : "") +
      "</div></div>" +
      // 大图模式那条带子(见 style.css): 直播间多半没有简介, 就写"关注的主播/直播推荐" ——
      // 人气/开播时间已经画在封面上了, 再抄一遍是重复。
      (it.cover ? '<div class="bdesc">' + (hasBody(it.desc) ? esc(it.desc)
        : (follow ? "关注的主播正在直播" : "直播推荐")) + "</div>" : "") +
      '<div class="acts">' +
        '<button class="sec" data-act="refresh" data-label="换一条">' + ic("shuffle") + "换一条</button>" +
        '<button class="sec" data-act="like" data-label="喜欢">' + ic("thumb-up") + "喜欢</button>" +
        '<button class="sec" data-act="dislike" data-label="不喜欢">' + ic("thumb-down") + "不喜欢</button>" +
        '<button class="sec" data-act="block" data-label="屏蔽作者">' + ic("ban") + "屏蔽作者</button>" +
        '<button class="sec" data-act="save" data-label="收藏">' + ic("star") + "收藏</button>" +
        '<button class="sec" data-act="open" data-label="进直播间">' + ic("play") + "进直播间</button>" +
      "</div></article>";
  }
  function hasBody(t) {
    // 有的源简介就是一个 "-" 或 "…"(等于没内容): 文字卡里别留一行孤零零的破折号
    const s = String(t || "").trim();
    if (!s) return false;
    return !/^[-—–_.。…·,，、;；:：!！?？"“”'‘’()（）\[\]【】~\s]+$/.test(s);
  }
  function cardHTML(it) {
    if ((it.platform || "") === "B站直播" || it.is_live) return liveCardHTML(it);
    const plat = it.platform || "B站";
    const cls = plat === "贴吧" ? "tb" : plat === "小黑盒" ? "xhh" : "bili";
    const covers = (it.covers && it.covers.length) ? it.covers : (it.cover ? [it.cover] : []);
    const url = it.url || "";
    const bvid = (url.match(/BV[0-9A-Za-z]{8,12}/) || [])[0] || "";
    // 站内阅读：贴吧 wap 帖 / 小黑盒帖子都能直接解析成文（不依赖浏览器窗口）
    const tid = plat === "贴吧" ? ((url.match(/\/p\/(\d+)/) || [])[1] || "") : "";
    const xlid = plat === "小黑盒" ? ((url.match(/bbs\/link\/([0-9A-Za-z]+)/) || [])[1] || "") : "";
    // v0.3.1.65 用户要求: 封面悬停试播时不要再冒出"打开原文"的提示条 —— 那是 title 原生
    // tooltip, 鼠标停在封面上等预览就一定会弹出来挡住画面。入口不变(点封面仍打开原文),
    // 只是不再有 title; 读屏用户靠 aria-label 拿到同样的说明。
    const openAttr = url ? ' data-act="open" aria-label="打开原文"' : "";
    // 文字帖(没有封面): 不画占位字、不留空图位 —— 直接铺正文(用户: 空图片位太丑)。
    // 封面图挂了也走同一套排版, 由 msImgFail() 给卡片补 .text。
    const hasCover = covers.length > 0;
    // v0.3.1.66(用户要求): ①「几天前」跟着 up 名走到同一行(大图模式的 .byline);
    // ②作者是我关注的人时, 在名字**前面**挂一个「关注的人」小标。
    // it.following 由后端 _annotate_following 打(设置里开了「关注的人强调」才会带),
    // 直播卡早就用自己的 .pb.follow, 这里管的是普通视频/帖子卡。
    const followed = !!it.following;
    const tmTxt = it.pubdate ? ago(it.pubdate) : "";
    const followBadge = followed
      ? '<span class="pf" data-follow="1" title="你关注的人发布">关注的人</span>' : "";
    const thumb = hasCover
      ? '<div class="thumb"' + openAttr + ">" + '<img loading="lazy" referrerpolicy="no-referrer" src="' +
        esc(covers[0]) + '" onerror="msImgFail(this)">' +
        (it.duration_known && it.duration ? '<div class="dur">' + dur(it.duration) + "</div>" : "") +
        // 这条角标行只留互动数据。发布时间 v0.3.1.66 起挪到 up 名那一行(.byline 里的 .tm,
        // 用户要求), 大图模式再在封面角标里显示一遍就是同一个信息两处出现。
        '<div class="stats">' +
        "<span>播放 " + fmt(it.views) + "</span>" +
        (it.danmaku != null ? "<span>弹 " + fmt(it.danmaku) + "</span>" : "") +
        (it.reply != null ? "<span>评 " + fmt(it.reply) + "</span>" : "") + "</div></div>"
      : "";
    const body = hasBody(it.desc);
    const metaLine = esc(it.author || "用户") + " · 播放 " + fmt(it.views) +
      (it.pubdate ? " · " + ago(it.pubdate) : "");
    return '<article class="card' + (hasCover ? "" : " text") + (body ? "" : " nometa") +
      '" data-key="' + esc(it.key) +
      '" data-author="' + esc(it.author) +
      '" data-plat="' + esc(plat) + '" data-url="' + esc(url) + '" data-bvid="' + esc(bvid) + '">' +
      // 文字帖的「文字封面」: 大图模式下用正文开头填满封面那块位置, 让标题和带封面的
      // 卡片落在同一条视觉线上(用户: 纯文本的标题也要对齐), 又不留空图位(用户: 空图位太丑)。
      // 只在 #list.big 显示 —— 单栏/双栏里正文本来就在卡片里, 不再重复一遍。
      (hasCover ? "" : '<div class="tpanel"' + openAttr + '><div class="tq">' +
        esc(body ? it.desc : metaLine) + "</div></div>") +
      '<h3><span class="pb ' + cls + '">' + esc(plat) + '</span><span class="t"' + openAttr + ' title="' +
        esc(it.title) + '">' + esc(it.title) + "</span></h3>" +
      // 大图模式的作者行(v0.3.1.64 用户要求): 作者名紧跟在标题下面, 用强调色。
      // 单栏/双栏不显示(那时作者本来就在正文那行的 meta 里); 封面图加载失败会被 msImgFail()
      // 补上 .text, 那种卡片同样靠 .col .meta 显示作者, 所以 CSS 里 .text 卡片会藏掉这行。
      (hasCover ? '<div class="byline">' + followBadge +
        '<span class="au">' + esc(it.author || "用户") + "</span>" +
        (tmTxt ? '<span class="tm"> · ' + esc(tmTxt) + "</span>" : "") + "</div>" : "") +
      '<div class="media">' + thumb + '<div class="col">' +
        '<div class="meta">' + followBadge + '<span class="au">' + esc(it.author || "用户") + "</span> · 播放 " +
          fmt(it.views) + (tmTxt ? " · " + tmTxt : "") + "</div>" +
        (body ? '<div class="desc">' + esc(it.desc) + "</div>" : "") +
        (it.reason ? '<div class="reason">' + ic("bulb", "xs") + "<span>" + esc(it.reason) + "</span></div>" : "") +
      "</div></div>" +
      // 大图模式下动作行是小号浮层(悬停才出现), 不占位 —— 这条带子平时显示一行简介,
      // 没有简介就显示作者/播放/时间; 否则卡片底下会空出一条空带(v0.3.1.54 用户报的)。
      (hasCover ? '<div class="bdesc">' + (body ? esc(it.desc) : metaLine) + "</div>" : "") +
      '<div class="acts">' +
        // 打开原文 / 站内播放两个按钮已删除：点标题或缩略图就是打开原文，那两个按钮是重复入口
        // data-label 是给悬停浮层用的文字: 按钮本身只留图标(尺寸固定), 文字由 ::after 浮层显示,
        // 不改变按钮占位 —— 否则文字一撑宽就把右邻顶走, 鼠标横扫会跳过按钮(用户报的第二个问题)
        '<button class="sec" data-act="refresh" data-label="换一条">' + ic("shuffle") + "换一条</button>" +
        '<button class="sec" data-act="like" data-label="喜欢">' + ic("thumb-up") + "喜欢</button>" +
        '<button class="sec" data-act="dislike" data-label="不喜欢">' + ic("thumb-down") + "不喜欢</button>" +
        '<button class="sec" data-act="block" data-label="屏蔽作者">' + ic("ban") + "屏蔽作者</button>" +
        (tid ? '<button class="sec" data-act="read" data-tid="' + esc(tid) + '" data-label="站内阅读">' + ic("list") + "站内阅读</button>"
             : xlid ? '<button class="sec" data-act="xread" data-lid="' + esc(xlid) + '" data-label="站内阅读">' + ic("list") + "站内阅读</button>" : "") +
        '<button class="sec" data-act="save" data-label="收藏">' + ic("star") + "收藏</button>" +
        '<button class="sec" data-act="ask" data-label="问问 AI">' + ic("spark") + "问问 AI</button>" +
      "</div></article>";
  }
  /* 空态：不再只丢一句"没有内容"。每一态都写清「现在是什么情况 + 下一步点哪里」。
     按钮统一带 data-empty-act，由 #list 的事件委托接住 —— 空态不是 .card，卡片那套委托接不到。 */
  const EMPTY_SPEC = {
    first:   { ic: "spark",   t: "先告诉它你想看什么", d: "选几个你感兴趣的领域，之后信息流就照这个口味收录。半分钟就能完成。", b: "开始设置", act: "intro" },
    waiting: { ic: "refresh", t: "正在准备内容", d: "池子还在补，通常十几秒。这段时间可以先调调口味或选来源。", b: "调整口味", act: "general" },
    stale:   { ic: "inbox",   t: "这里还没有内容", d: "按现在的设置没收到东西。先看看平台账号是不是没配好。", b: "检查平台账号", act: "sources" },
    history: { ic: "clock",   t: "还没有历史记录", d: "点开过的内容会存在这里，方便回头找。", b: "去信息流看看", act: "feed" },
    saved:   { ic: "star",    t: "还没有收藏", d: "卡片上的「收藏」会存到这里，只存本机，不同步到平台。", b: "去挑几条", act: "feed" },
  };
  function renderEmpty(kind, extra) {
    const s = EMPTY_SPEC[kind] || EMPTY_SPEC.stale;
    const isSearch = kind === "search";
    const title = isSearch ? "没搜到「" + (extra || "") + "」" : s.t;
    const desc = isSearch ? "换个词试试，或者清掉搜索看看现在这一池内容。" : s.d;
    const btn = isSearch ? "清空搜索" : s.b;
    const act = isSearch ? "clearq" : s.act;
    return '<div class="empty em">' + ic(s.ic) +
      '<b class="emTitle">' + esc(title) + "</b>" +
      '<span class="emDesc">' + esc(desc) + "</span>" +
      '<button type="button" class="emAct" data-empty-act="' + act + '">' + esc(btn) + "</button></div>";
  }
  function emptyKind() {
    if (S.q) return "search";
    if (S.view === "history") return "history";
    if (S.view === "saved") return "saved";
    return "stale";
  }
  // 空态里的按钮：一律"直达"，不让人自己找设置入口
  function onEmptyAct(act) {
    if (act === "intro") { openIntro(); return; }
    if (act === "clearq") {
      S.q = ""; const si = $("#searchInput"); if (si) si.value = "";
      S.offset = 0; S.done = false; restartFeed(); return;
    }
    if (act === "feed") { setView("feed"); return; }
    if (act === "refresh") { refreshFeed(); return; }
    goSettings(act);          // general / sources / ai …
  }
  // v0.4.38: 首页降权只算「真的被翻到」的卡片。旧口径是服务端一返回就计数,
  // 4 列布局下没滑到的半页也照样被降权。这里用 IntersectionObserver 看露出比例,
  // 过半才把 key 攒起来批量上报; 同一张卡(元素)只报一次。
  const viewSeen = new WeakSet();
  let viewQueue = [], viewTimer = null;
  function flushViewed() {
    viewTimer = null;
    const keys = viewQueue.splice(0, viewQueue.length);
    if (!keys.length || S.view !== "feed" || S.q || !S.session) return;
    api("/api/viewed", { method: "POST", body: JSON.stringify({ rid: S.session, keys: keys }) })
      .catch(function () {});
  }
  const viewObs = ("IntersectionObserver" in window) ? new IntersectionObserver(function (es) {
    let hit = false;
    for (const e of es) {
      if (!e.isIntersecting || e.intersectionRatio < 0.5) continue;
      const el = e.target;
      viewObs.unobserve(el);
      if (viewSeen.has(el)) continue;
      viewSeen.add(el);
      const k = el.dataset && el.dataset.key;
      if (k) { viewQueue.push(k); hit = true; }
    }
    if (hit && !viewTimer) viewTimer = setTimeout(flushViewed, 700);
  }, { threshold: [0.5] }) : null;

  function renderItems(items) {
    if (!items.length) {
      if (S.offset === 0) $("#list").innerHTML = renderEmpty(emptyKind(), S.q);
      return;
    }
    const wrap = document.createElement("div");
    wrap.innerHTML = items.map(cardHTML).join("");
    const list = $("#list");
    if (S.offset === 0) list.innerHTML = "";
    while (wrap.firstChild) list.appendChild(wrap.firstChild);
    // "一直刷下去"不能让 DOM 无限增长：超过上限就从最老的丢起。
    // 必须把丢掉的像素高度从 scrollTop 里扣掉，否则视口会往下跳。
    if (list.children.length > MAX_CARDS) {
      const gap = parseFloat(getComputedStyle(list).rowGap) || 0;
      let removed = 0;
      for (let i = 0; i < TRIM_CARDS && list.firstElementChild; i++) {
        const el = list.firstElementChild;
        removed += el.getBoundingClientRect().height + gap;
        el.remove();
      }
      list.scrollTop = Math.max(0, list.scrollTop - removed);
    }
    applySrcFilter();
    if (viewObs && S.view === "feed" && !S.q) {
      // 已上报过的元素是 unobserve 状态; 重复 observe 无副作用。隐藏(别的平台)的卡
      // 不算 intersecting, 不会被计入。
      list.querySelectorAll(".card").forEach(function (c) { viewObs.observe(c); });
    }
  }
  async function loadFeed(refresh) {
    if (S.loading) return;
    S.loading = true;
    const myGen = S.reqGen;
    const stale = () => myGen !== S.reqGen;   // 界面已经不认这次请求了
    const isFeed = S.view === "feed";
    if (refresh || !isFeed) S.offset = 0;
    try {
      let d;
      if (S.view === "history") d = await api("/api/history");
      else if (S.view === "saved") d = await api("/api/saved?kind=fav");   // 旧版「收藏」写的就是 fav
      else {
        const p = [];
        if (S.q) p.push("q=" + encodeURIComponent(S.q), "sort=" + S.sort);
        else p.push("rid=" + S.session);
        if (refresh) p.push("refresh=1");
        // 平台筛选交给后端：前端只把别的平台藏起来时，"看不见的卡片"照样占着会话额度与 DOM，
        // 真正要看的那一路被稀释 → 快速滑动很快就没得看（用户报的那个等待）。
        if (S.src) p.push("plat=" + encodeURIComponent(S.src));
        // 一页取到后端允许的上限 50 条：多列布局下每屏条目数是单栏的好几倍，
        // 一页 30 条在四列下一屏就吃完了，垫不住快速滑动。
        p.push("offset=" + S.offset, "limit=50");
        d = await api((S.q ? "/api/search" : "/api/feed") + "?" + p.join("&"));
      }
      if (stale()) return;                      // 过期响应：丢弃，别往已经变了的界面上追加
      if (Number.isInteger(d.gen)) S.gen = d.gen;
      const items = d.items || d.list || [];
      // 空响应 = 池子正在补（正常等待态）。两条铁律：
      //   ① 绝不清空已经刷出来的列表 —— 用户滑到池子边界时整屏内容消失是最刺眼的一种失败；
      //   ② 信息流永不"到底" —— 后端一直在补池，静默退避重试直到等到，界面上一句提示都不出。
      if (!items.length && isFeed && !S.q) {
        if (!document.querySelector("#list .card")) {
          if (S.src) S.emptyMsg = "「" + S.src + "」暂时没有可看的内容，正在继续找…";
          // 只有真的什么都没有（首次进入/清空后重收）才需要说明一句；
          // 具体说什么由触发方决定（清空信息池 → 「正在按当前设置重新收录…」）
          // 一次都没配好来源（没吧列表 / 没登录 / 从没抓过）时，"正在准备内容…"会一直转下去 ——
          // 后端根本没有东西可抓。这时换成一张能点走的卡：去配来源。
          $("#list").innerHTML = S.emptyMsg
            ? '<div class="empty">' + ic("refresh") + "<span>" + esc(S.emptyMsg) + "</span></div>"
            : renderEmpty(S.setupNeeded ? "first" : "waiting");
        }
        S.refillTries = (S.refillTries || 0) + 1;
        S.loading = false;
        S.waiting = true;      // 等下一轮再取，期间预取不重复触发
        loadStatus();          // 这里也要刷状态行，否则顶栏/内容源会停在「加载中…」
        // 重试间隔必须跟得上后端的补池节奏。池子补充期间服务端游标会间歇性吐空页
        // （实测连翻 14 页里有 9 页是空的，池子从 303 慢慢涨到 549），旧版靠 300~700ms
        // 的快速轮询把这些空档抹平，用户感觉不到。这里原来退避到 1.8s、连丢几次后放到 5s，
        // 同一个空档就变成用户眼里的"卡住" ——「不用等待」就是这么没的。
        // 现在与旧版同量级：构建中 700ms，其它 300ms；只在长时间仍无内容时才放宽，
        // 依然不设"放弃"，后台补到货就自动接上。
        if (S.refillTries === 3) {
          // 连续几次都取不到 = 池子尾部被"同作者配额"压住了（那批要等新的构建才分批放出）。
          // 只靠重试是不够的，得踢后端立刻去补一批新内容 —— 只踢一次，不刷屏。
          api("/api/feed?refresh=1&limit=1&rid=" + S.session).catch(function () {});
        }
        const waitMs = S.refillTries > 12 ? 1500 : (d.building ? 700 : 300);
        setTimeout(() => { S.waiting = false; loadFeed(false); }, waitMs);
        return;
      }
      S.refillTries = 0;
      S.waiting = false;
      S.emptyMsg = "";          // 有内容了，清掉"正在重收"之类的说明
      renderItems(items);
      S.offset = Number.isInteger(d.next_offset) ? d.next_offset : S.offset + items.length;
      // 到底了没有：信息流是无限的（池子会一直补，绝不置 done，否则预取永久停住）；
      // 历史/收藏一次给全；搜索是一组**有限**结果 —— 后端 has_more 说没有、或这一页已经空，
      // 就得停，否则预取会拿着同一个 offset 反复要空页（实测过 3625 次请求）。
      S.done = !isFeed || (!!S.q && (!d.has_more || !items.length));
    } catch (e) { if (!stale()) toast("加载失败：" + e, true); }
    if (!stale()) S.loading = false;            // 过期响应不负责交还 loading（新请求已经接手）
    // 必须在放开 loading 之后再判：否则 ensureBuffer 被自己的 loading 挡掉，
    // 一次只取一页就断链，快速滑动时余量永远补不满（改前就是这个问题）
    ensureBuffer();
    loadStatus();
  }
  // 作废在途的信息流请求，然后立刻重取一屏。
  // 必须这么做：切换筛选/取消筛选/清空信息池时，如果正好有一次预取在飞，
  // 新请求会被 S.loading 挡掉，而在途的那次（用的是旧参数）随后把不属于当前筛选的内容追加进来
  // —— 表现就是"刚点完只看 B站，列表里还混着别的平台"。
  function restartFeed() {
    S.reqGen++;
    S.loading = false;
    S.offset = 0; S.done = false; S.waiting = false; S.refillTries = 0;
    if (S.view === "live") { loadLive(true); return; }   // 平台权重/筛选变了，直播页也跟着重取
    loadFeed(false);
  }

  // 内容源那一行显示什么：能动手解决的才提示，其余只说新鲜度
  function srcState(s) {
    const today = s.fetched_today ? "今日 " + fmt(s.fetched_today) + " 条" : "";
    const err = String(s.error || "");
    // v0.3.1.59: 浏览器窗口通道接通时先说"走窗口通道", 不报"被拦截" —— 否则用户拖完
    // 滑块、通道明明在用, 左栏还挂着 403, 会以为验证没生效(这就是原来那个"验证完也没用")。
    if (s.name === "贴吧" && s.browser_channel) {
      // 通道在用但窗口里也被拦(403)时别报"正常" —— 那时要重新验证, 而不是让用户等着
      const bad = /安全验证|403|wappass/.test(err);
      return { text: bad ? "窗口通道 · 需重新验证" : ("窗口通道 · " + (s.last_fetch ? "更新于 " + ago(s.last_fetch) : "待抓取")),
               warn: bad, verify: bad,
               tip: ("已验证的浏览器窗口在接管贴吧抓取（窗口闲置 30 分钟才会自动关闭，关掉后回落直连）\n"
                     + (err ? (bad ? "验证窗口自己也被拦了，点这一行重新拖一次滑块：" : "上次直连报错：") + err + " " : "")
                     + today).trim() };
    }
    if (err) {
      // 只写"抓取失败"用户不知道该怎么办(是没登录? 是网络? 是被封?), 常见的两类原因直接写在行上,
      // 完整报错留给悬停提示。贴吧 403 基本是百度安全验证拦出口 IP, 跟登录/cookie 没关系。
      const e = err;
      // v0.3.1.55: 被百度安全验证拦住时, 这一行变成"能动手救"的入口 —— 点一下打开
      // 验证窗口, 拖个滑块就恢复; 光提示"被拦截"等于让用户干等冷却。
      if (s.need_verify && s.name === "贴吧") {
        return { text: "点这里验证", warn: true, verify: true,
                 tip: "被百度安全验证拦住了 — 点这一行打开验证窗口，拖动滑块完成后自动重新抓取\n"
                      + (e + " " + today).trim() };
      }
      const short = /安全验证|风控|403|429/.test(e)
        ? (s.name === "贴吧" ? "被百度拦截" : "被限流")
        : /超时|timeout|连接|resolve|SSL|EOF|proxy/i.test(e) ? "网络不通" : "抓取失败";
      return { text: short, warn: true, tip: (e + " " + today).trim() };
    }
    if (s.name === "贴吧" && !s.bars) {
      return { text: "未配置吧列表", warn: true, goto: "sources",
               tip: "点这一行去「设置 → 平台账号」加入要看的贴吧" };
    }
    if (!s.last_fetch) return { text: "还没抓过", warn: true, tip: "点顶栏「刷新」试一次" };
    return { text: "更新于 " + ago(s.last_fetch), warn: false, tip: today };
  }
  async function loadStatus() {
    try {
      const d = await api("/api/status");
      const sm = d.summary || {};
      // 状态卡（左栏）：一个数一行，比原来挤在顶栏的一长串好读
      $("#stReceived").textContent = fmt(sm.received || 0) + " 条";
      $("#stFiltered").textContent = fmt(sm.filtered || 0) + " 条";
      $("#stCalls").textContent = fmt(sm.ai_calls || 0) + " 次";
      $("#stCost").textContent = "¥" + (sm.cost || 0);
      const warn = $("#metaWarn");
      warn.hidden = !!d.ai_on;
      warn.textContent = "AI 未配置 · 按热度排序";
      S.aiOn = !!d.ai_on;          // 聊天改设置要走了模型才知道能不能"先想再改"
      S.setupNeeded = !d.configured;
      // 没配 AI 时右栏默认收起（用户要求）：一个发不出话的输入框占掉 1/3 屏宽，
      // 对新用户纯属噪声。只自动收一次 —— 之后用户自己拉开还是收起，跟着 prefs 走。
      if (!d.ai_on && !narrow() && !localStorage.getItem("ms-new-rail-auto")) {
        try { localStorage.setItem("ms-new-rail-auto", "1"); } catch (e) {}
        if (!prefs.re) { prefs.re = true; savePrefs(); applyPrefs(); }
      }
      paintChatHint(!d.ai_on);
      // 内容源那一行不必播报抓取器的内部状态（"未抓取/正常"是给运维看的，用户看不懂也不需要）：
      // 默认只安静地说"更新于多久前"，只有真要用户动手时才出警示色。
      (d.sources || []).forEach((s) => {
        const el = document.querySelector('.st[data-st="' + s.name + '"]');
        if (!el) return;
        const info = srcState(s);
        el.textContent = info.text;
        el.title = info.tip;
        el.classList.toggle("warn", !!info.warn);
        // 「点这里验证」: 这一行变成按钮(仍留在内容源行里), 点它打开百度验证窗口
        if (info.verify) el.dataset.verify = "1"; else delete el.dataset.verify;
        el.classList.toggle("verifiable", !!info.verify);
        // 需要"去某个设置面板"才能解决的提示（目前是贴吧吧列表），也做成可点
        if (info.goto) el.dataset.goto = info.goto; else delete el.dataset.goto;
        el.classList.toggle("goable", !!info.goto);
      });
    } catch (e) {}
  }
  function applySrcFilter() {
    $$("#list .card").forEach((c) => {
      c.style.display = (!S.src || c.dataset.plat === S.src) ? "" : "none";
    });
    $$(".nav.src").forEach((b) => b.classList.toggle("on", b.dataset.src === S.src));
    const old = $("#srcChip");
    if (old) old.remove();
    if (S.src) {
      const chip = document.createElement("button");
      chip.id = "srcChip"; chip.className = "tb"; chip.title = "取消只看该平台";
      chip.innerHTML = "只看 " + esc(S.src) + ic("x", "xs");
      chip.onclick = () => {
        S.src = ""; applySrcFilter(); saveState();
        restartFeed();              // 取消筛选也要重取，否则流还是那一平台的
      };
      $("#listTools").appendChild(chip);
    }
  }
  // ---------- 预取：滑动时下方始终留着够看的余量 ----------
  // 用户要的是"一直刷下去不用等"，页脚已经彻底删掉，任何等待都不再提示。
  // 余量必须**按条目数**算，不能只看屏高：四列布局下同一屏高里的条目数是单栏的四倍，
  // 按屏高留的余量在四列下几眼就滑没了 —— 用户划快一点就撞到底。
  const PREFETCH_SCREENS = 4;    // 至少留着几屏高度
  const PREFETCH_ITEMS = 300;    // 且至少留着这么多条（覆盖任意列数；四列下一屏约 23 条，300 条 ≈ 13 屏）
  const MAX_CARDS = 1500;        // 卡片 DOM 上限：一直刷下去也不能无限堆
  const TRIM_CARDS = 500;        // 超限时从最老的丢掉这么多张
  let prefetchTick = false;
  function aheadCount() {        // 视口下方还有多少张**看得见的**卡
    const el = $("#list");
    if (!el) return 0;
    // 必须排掉被平台筛选藏起来的卡片：display:none 的元素 offsetTop 是 0，
    // 会破坏"按 offsetTop 单调递增"这个前提，二分数出来的余量就是假的。
    const cards = Array.prototype.filter.call(el.querySelectorAll(".card"),
      (c) => c.style.display !== "none");
    const fold = el.scrollTop + el.clientHeight;
    let lo = 0, hi = cards.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (cards[mid].offsetTop < fold) lo = mid + 1; else hi = mid;
    }
    return cards.length - lo;
  }
  function ensureBuffer() {
    // 搜索也走同一条预取：后端 /api/search 支持 offset，原来 S.q 直接 return 导致搜索只有一页
    if (S.view !== "feed" || S.done || S.loading || S.waiting) return;
    const el = $("#list");
    if (!el || !el.clientHeight) return;
    const shortByHeight = el.scrollHeight - (el.scrollTop + el.clientHeight) < PREFETCH_SCREENS * el.clientHeight;
    if (shortByHeight || aheadCount() < PREFETCH_ITEMS) loadFeed(false);
  }
  $("#list").addEventListener("scroll", () => {
    if (prefetchTick) return;                 // 每帧最多判一次，别在快速滑动时狂算
    prefetchTick = true;
    const run = () => { prefetchTick = false; ensureBuffer(); };
    if (window.requestAnimationFrame) requestAnimationFrame(run);
    // rAF 在后台标签页/掉帧时可能根本不执行 —— 那样旗子永远放不下来，
    // 后续滚动事件会被一条条吞掉，预取就此静默死掉（实测踩过）。用定时器兜底。
    setTimeout(run, 80);
  }, { passive: true });
  // 兜底巡检：无限滚动不该只依赖滚动事件。每隔一会儿看一眼缓冲够不够，
  // 不够就补 —— 这样即便滚动事件丢了、旗子被卡住，内容也照样接得上。
  setInterval(() => { if (S.view === "feed" && !S.done) ensureBuffer(); }, 1500);
  $("#list").addEventListener("click", async (e) => {
    const ea = e.target.closest("[data-empty-act]");
    if (ea) { onEmptyAct(ea.dataset.emptyAct); return; }
    const card = e.target.closest(".card");
    if (!card) return;
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const key = card.dataset.key, act = el.dataset.act;
    if (act === "open") {
      const tt = card.querySelector("h3 .t");
      openItem(key, card.dataset.url, tt ? tt.textContent : "");
      return;
    }
    if (act === "refresh") {
      const r = await save("/api/feed/refresh", { key: key, q: S.q }, "换一条");
      if (!r) return;                                   // 已提示失败，不再叠一条
      if (r.ok && r.item) { card.outerHTML = cardHTML(r.item); applySrcFilter(); }
      else toast("暂无可用内容替换", true);
      return;
    }
    if (act === "like") {
      const btn = el;
      const on = !S.liked.has(key);
      if (on) S.liked.add(key); else S.liked.delete(key);
      btn.classList.toggle("liked", on);
      // 写失败就把高亮退回去，不让界面替失败背书
      if (!(await save("/api/feedback", { key: key, action: on ? "like" : "undolike" }, "记录喜欢"))) {
        if (on) S.liked.delete(key); else S.liked.add(key);
        btn.classList.toggle("liked", !on);
      }
      return;
    }
    if (act === "dislike") {
      if (!(await save("/api/feedback", { key: key, action: "dislike" }, "记录不喜欢"))) return;
      card.remove();
      return;
    }
    if (act === "read" || act === "xread") {
      openReader(act === "read" ? { tid: el.dataset.tid } : { lid: el.dataset.lid });
      return;
    }
    if (act === "save") {
      const btn = el, kind = "fav";
      const on = !btn.classList.contains("on");
      if (!(await save("/api/saved", { key: key, kind: kind, remove: !on }, "收藏"))) return;
      btn.classList.toggle("on", on);
      return;
    }
    if (act === "block") {
      if (!(await save("/api/blacklist", { kind: "author", value: card.dataset.author }, "屏蔽作者"))) return;
      $$('.card[data-author="' + CSS.escape(card.dataset.author) + '"]').forEach((c) => c.remove());
      loadBlacklist();
      return;
    }
    if (act === "ask") {
      S.ctx = { key: key, title: (card.querySelector("h3 .t") || {}).textContent || "" };
      $("#ctxBar").classList.add("show");
      $("#ctxTitle").textContent = "正在针对：《" + S.ctx.title + "》";
      save("/api/chat/ctx", { cid: S.cid, key: S.ctx.key, title: S.ctx.title }, "设置上下文");
      if (narrow()) { document.body.classList.add("chatOpen"); }
      else { prefs.re = false; savePrefs(); applyPrefs(); }
      $("#chatInput").focus();
      return;
    }
  });

  /* ================= 中区顶栏 ================= */
  // 中间的内容视图表：不在表里的就是"列表视图"（信息流/历史/收藏）
  // 切走信息流时把它的 DOM 和分页状态存下来, 切回来原样贴回。
  // 否则每次"历史→信息流"都要清空重取, 用户要看着空列表等 —— 而这批内容其实还在手里。
  // （不影响"切视图必须清掉旧视图内容"那条: 存的是信息流自己的东西, 不是别的视图的。）
  let feedKeep = null;
  function keepFeed(view) {
    const l = $("#list");
    // 调用点已经在 S.view 被改掉之后, 所以视图名由调用方传进来
    if (!l || !view) return;
    const si = $("#searchInput");
    // 搜索词/排序/平台筛选也要一起存: 原来只存了卡片和分页, 于是"搜着搜着进设置再回来"
    // 搜索框空了、结果也变了。存进来的东西必须能原样还原, 否则还原就是一次假刷新。
    feedKeep = { view: view, html: l.innerHTML, top: l.scrollTop,
                 offset: S.offset, done: S.done, gen: S.gen,
                 q: S.q, sort: S.sort, src: S.src, input: si ? si.value : S.q };
  }
  function restoreFeed(view) {
    const l = $("#list");
    if (!l || !feedKeep || feedKeep.view !== view || !feedKeep.html) return false;
    l.innerHTML = feedKeep.html;
    l.scrollTop = feedKeep.top;
    S.offset = feedKeep.offset; S.done = feedKeep.done;
    if (feedKeep.gen != null) S.gen = feedKeep.gen;
    S.q = feedKeep.q || "";
    S.sort = feedKeep.sort || "default";
    S.src = feedKeep.src || "";
    const si = $("#searchInput"); if (si) si.value = feedKeep.input || "";
    const ss = $("#sortSel"); if (ss) ss.value = S.sort;
    applySrcFilter();
    return true;
  }

  const PANES = { settings: "viewSettings", msg: "viewMsg", live: "viewLive" };
  function setView(v) {
    const prev = S.view;
    const pane = PANES[v] || null;
    const inPane = !!pane;
    const prevPane = !!PANES[prev];
    // 离开信息流（去设置/消息，或去历史/收藏）之前先把它整块存下来。
    // 这里的 prev 判断必须在下面那个"换视图"分支之外 —— 进设置时 inPane 为 true，
    // 那个分支根本进不去，于是从来没存过 —— 这就是"点开设置再回来，首页被刷新了"的根因。
    if (prev === "feed" && v !== "feed") keepFeed("feed");
    S.view = v;
    const backFromPane = prevPane && !inPane;   // 从设置/消息返回列表：刚才那屏内容还在手里
    if (!inPane) {
      S.lastList = v;
      if (!backFromPane) {   // 列表视图之间互切才清空重取；从设置回来要原样贴回
        S.q = "";
        const si = $("#searchInput"); if (si) si.value = "";
        S.offset = 0;
        if (prev !== v) {
          // 换视图必须把列表清干净再画。
          // 起因: "绝不清空列表"那条规则只该管**同一个视图里滚动**；跨视图时如果新视图这一批
          // 恰好返回空（会话游标暂时没货、正在补池），旧视图的卡片就会一直留在屏幕上，
          // 看起来就是"点了信息流不跳转"。
          S.done = false; S.waiting = false; S.refillTries = 0;
          const l = $("#list"); if (l) l.innerHTML = "";
        }
      }
    }
    // 中间是内容展示区：列表 / 设置 / 消息 都在这一层切换，不用浮层
    $$(".viewPane").forEach((p) => p.classList.toggle("show", p.id === pane));
    ["#listTools", "#list"].forEach((s) => $(s).classList.toggle("hidden", inPane));
    // 二级界面：左上角收放开关换成「返回」（也不让它收侧栏）；设置时左栏被设置导航顶替
    document.body.classList.toggle("in-settings", v === "settings");
    $("#toggleLeft").style.display = inPane ? "none" : "";
    $("#toggleRight").style.display = inPane ? "none" : "";
    $("#btnBack").style.display = inPane ? "" : "none";
    $$(".nav[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === v));
    $("#btnSettings").classList.toggle("on", v === "settings");
    // 窄屏：切视图时把抽屉和聊天面板都收起来，否则它们会盖住刚切过去的视图
    if (narrow()) document.body.classList.remove("drawer", "chatOpen");
    syncTabs();
    saveState();
    S.reqGen++;        // 切视图时作废在途请求，免得旧视图的数据追加到新视图上
    S.loading = false;
    // 点开消息 = 看过了: 先把角标清掉(不等 5 分钟那一轮刷新), 再渲染
    if (v === "msg") { markMessagesRead(); renderMsgPane(); return; }
    if (v === "live") { loadLive(false); return; }
    if (inPane) return;
    if (backFromPane && restoreFeed(v)) { saveState(); return; }   // 手里还有刚才那屏 → 原样贴回，秒切不重取
    loadFeed(false);   // 否则只取当前视图的内容（不换池子）
  }
  $("#sortSel").onchange = () => { S.sort = $("#sortSel").value; S.offset = 0; saveState(); loadFeed(true); };
  // 把还没上过屏的条目插到列表**最前面**（刷新后新抓到的那批），
  // 并按插入的总高度补偿 scrollTop —— 插在上面会把用户正在看的内容往下推，不补就会跳。
  async function insertFresh() {
    let d;
    try {
      const p = ["rid=" + S.session, "offset=0", "limit=50"];
      if (S.src) p.push("plat=" + encodeURIComponent(S.src));
      d = await api("/api/feed?" + p.join("&"));
    } catch (e) { return 0; }
    const have = new Set(Array.prototype.map.call(document.querySelectorAll("#list .card"), (c) => c.dataset.key));
    const fresh = (d.items || []).filter((it) => it && it.key && !have.has(it.key));
    if (!fresh.length) return 0;
    const list = $("#list");
    if (!list) return 0;
    const h0 = list.scrollHeight;
    const wrap = document.createElement("div");
    wrap.innerHTML = fresh.map(cardHTML).join("");
    const first = list.firstElementChild;
    if (first) { while (wrap.lastChild) list.insertBefore(wrap.lastChild, first); }
    else { while (wrap.firstChild) list.appendChild(wrap.firstChild); }
    // 已经往下滑了 → 按插入高度补偿，保住他正在看的那几条；停在顶部 → 不补偿，
    // 让新内容直接出现在眼前（刷新后本来就该先看到新的）。
    if (list.scrollTop > 0) list.scrollTop += (list.scrollHeight - h0);
    applySrcFilter();
    return fresh.length;
  }

  /* ================= 直播页（v0.3.1.51） ================= */
  // 数据来自 /api/live：关注的人正在直播（followed）+ 热门推荐（hot），服务端 60 秒缓存。
  // 接口失败只降级成空列表 + error 文案，页面不白屏。
  let liveLoaded = false;
  function liveTileHTML(it, follow) {
    const online = it.online != null ? it.online : it.views;
    return '<button class="liveTile' + (follow ? " follow" : "") + '" data-key="' + esc(it.key) +
      '" data-url="' + esc(it.url || "") + '" data-title="' + esc(it.title) + '" title="' + esc(it.title) + '">' +
      '<span class="ltCov">' + (it.cover ? '<img loading="lazy" referrerpolicy="no-referrer" src="' +
        esc(it.cover) + '" onerror="this.remove()">' : "") +
        '<em class="ltLive">直播中</em><em class="ltHot">人气 ' + fmt(online) + "</em></span>" +
      '<span class="ltT">' + esc(it.title) + "</span>" +
      '<span class="ltM">' + esc(it.author || "主播") + (it.tname ? " · " + esc(it.tname) : "") +
        (it.live_time ? " · 开播 " + ago(it.live_time) : "") + "</span></button>";
  }
  function renderLive(d) {
    const box = $("#liveBody");
    if (!box) return;
    liveLoaded = true;
    const fu = d.followed || [], hot = d.hot || [];
    const meta = $("#liveMeta");
    if (meta) {
      meta.textContent = (!d.ok && d.error) ? "拉取失败"
        : (fu.length ? "关注 " + fu.length + " 人在播" : "") + (hot.length ? (fu.length ? " · " : "") + "热门 " + hot.length : "");
    }
    if (!fu.length && !hot.length) {
      const msg = d.error ? d.error
        : (d.need_login ? "登录 B站 并同步关注后，这里会显示关注的人正在直播" : "暂时没有在播的直播");
      box.innerHTML = '<div class="empty">' + ic("video") + "<span>" + esc(msg) + "</span></div>";
      return;
    }
    let html = "";
    if (fu.length) {
      html += '<div class="liveSec"><h3>关注的人正在直播 <em>' + fu.length + "</em></h3>" +
        '<div class="liveGrid">' + fu.map((x) => liveTileHTML(x, true)).join("") + "</div></div>";
    }
    if (hot.length) {
      html += '<div class="liveSec"><h3>热门直播 <em>' + hot.length + "</em></h3>" +
        '<div class="liveGrid">' + hot.map((x) => liveTileHTML(x, false)).join("") + "</div></div>";
    }
    if (d.need_login) {
      html += '<p class="liveTip">还没同步 B站 关注列表 —— 到「设置 → 平台账号」登录 B站 后，这里会列出关注的主播。</p>';
    }
    box.innerHTML = html;
  }
  async function loadLive(refresh) {
    const box = $("#liveBody");
    if (!box) return;
    if (!refresh && liveLoaded) return;   // 来回切页面不重复拉（服务端也有 60 秒缓存）
    box.innerHTML = '<div class="empty">' + ic("video") + "<span>正在拉取直播列表…</span></div>";
    let d;
    try { d = await api("/api/live" + (refresh ? "?refresh=1" : "")); }
    catch (e) { d = { ok: false, followed: [], hot: [], error: "连不上服务：" + e }; }
    renderLive(d);
  }
  if ($("#btnLiveRefresh")) $("#btnLiveRefresh").onclick = () => loadLive(true);
  $("#liveBody").addEventListener("click", (e) => {
    const t = e.target.closest(".liveTile");
    if (t) openItem(t.dataset.key, t.dataset.url, t.dataset.title);
  });

  // 「刷新」= 重读**当前所在的视图**，绝不把用户拽回信息流。
  // 信息流上还要换一池新的：先立刻返回当前内容（不白屏），新一代落地后自动换上。
  async function refreshFeed() {
    if (S.view === "msg") { renderMsgPane(); loadMessages(); return; }
    if (S.view === "live") { loadLive(true); loadStatus(); return; }
    if (S.view === "settings") { loadSettings(); loadBlacklist(); loadAccount(); loadStatus(); refreshFloatBall(); return; }
    if (S.view !== "feed") { S.offset = 0; S.done = false; await loadFeed(false); loadStatus(); return; }
    // 信息流：立刻换一屏 —— 不等后台重建（全量要 10-30 秒，旧实现就是在这儿干等）。
    const gen0 = S.gen;
    S.reqGen++;                     // 在途的预取不要再来追加旧参数的内容
    S.loading = false;
    S.offset = 0; S.done = false; S.waiting = false; S.refillTries = 0;
    $("#btnRefresh").classList.add("busy");
    await loadFeed(true);
    loadStatus();
    $("#btnRefresh").classList.remove("busy");
    // 后台那批新内容落地后再补一次：插到最前面（不是整体替换），滚动位置由 insertFresh 补偿，不跳
    for (let i = 0; i < 5; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      if (S.view !== "feed") return;
      if (S.gen !== gen0) { if (await insertFresh()) loadStatus(); return; }
    }
  }
  $("#btnRefresh").onclick = refreshFeed;
  // 状态卡收放（原来收放的是顶栏那条状态行，现在它成了左栏的卡片）
  $("#metaToggle").onclick = () => {
    prefs.statOpen = !(prefs.statOpen !== false);
    savePrefs();
    applyStatCard();
  };
  // 这行以前只是说明，点了没反应；现在是去配置 AI 的入口
  $("#metaWarn").onclick = () => goSettings("ai");
  $("#btnClearPool").onclick = async () => {
    if (!confirm("清空信息池：丢弃已收录的全部内容与「已看过」记录，再按当前设置重新抓取。\n\n" +
                 "不含历史 / 收藏 / 聊天（那些在「设置 → 数据」里清）。\n" +
                 "重新收录需要十几秒，这期间列表会是空的。")) return;
    if (!(await save("/api/pool/clear", {}, "清空信息池"))) return;
    // 让"清空了"这件事在界面上真的看得见：列表变成空态并说明正在重收（空态文案由这里决定）
    // 清池后必须退出搜索态并换一个会话：搜索路径根本不读池子（清了个寂寞），而会话 id 不变时
    // 后端会把"重建后重新抓回的同一批内容"全算成本会话已返回 → 清完怎么刷都是空。
    S.q = "";
    const si = $("#searchInput"); if (si) si.value = "";
    S.session = Math.random().toString(36).slice(2);
    S.offset = 0; S.done = false; S.waiting = false; S.refillTries = 0;
    S.emptyMsg = "正在按当前设置重新收录…";
    S.reqGen++;                     // 作废在途请求，免得旧数据把空态盖掉
    S.loading = false;
    $("#list").innerHTML = "";
    saveState();
    loadFeed(true);
  };

  /* ================= 左栏：顶部搜索 / 导航 / 内容源 / 个人 / 设置 ================= */
  // 窄屏（手机）：左栏是抽屉，右栏 AI 聊天是整屏面板 —— 都由 body 上的类控制显隐，
  // 免得它们要么不可达、要么一直盖住信息流。
  function narrow() { return window.matchMedia("(max-width:860px)").matches; }
  function closeDrawer() { document.body.classList.remove("drawer"); }
  function syncTabs() {
    const chatOpen = document.body.classList.contains("chatOpen");
    $$("#tabbar button").forEach((x) => {
      const v = x.dataset.v;
      let on = false;
      if (v === "chat") on = chatOpen;
      else if (!chatOpen) {
        on = (v === "feed" && S.view === "feed") || (v === "live" && S.view === "live") ||
             (v === "panel" && S.view === "history") ||
             (v === "settings" && S.view === "settings");
      }
      x.classList.toggle("on", on);
    });
  }
  $("#btnDrawer").onclick = () => document.body.classList.toggle("drawer");
  $("#scrim").onclick = closeDrawer;
  window.addEventListener("resize", () => {
    if (!narrow()) document.body.classList.remove("drawer", "chatOpen");
    syncTabs();
    applyListCols();    // 窗口变宽变窄, 大图模式的列数跟着重算
  });

  // 搜索常驻顶栏：显式触发。输入过程中**不搜** —— 每敲一个字就打一次 B站接口，
  // 既慢又喂风控；点右侧「搜索」或按 Enter 才发。清空输入自动退回信息流。
  function runSearch() {
    const q = $("#searchInput").value.trim();
    if (!q) { exitSearch(); return; }
    if (S.view !== "feed") setView("feed");   // 从设置/历史切回来（setView 会清空输入，下面写回）
    $("#searchInput").value = q;
    S.q = q;
    S.emptyMsg = "";
    saveState();
    $$(".nav[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === "feed"));
    restartFeed();   // 作废在途请求 + 立刻重取一屏（搜索不带 refresh=1）
  }
  function exitSearch() {
    if (!S.q) return;
    S.q = "";
    S.emptyMsg = "";
    saveState();
    restartFeed();
  }
  $("#btnSearch").onclick = runSearch;
  $("#searchInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); runSearch(); $("#searchInput").blur(); }
    else if (e.key === "Escape") { $("#searchInput").value = ""; exitSearch(); }
  });
  // 只处理"清空即退出搜索"；有词时不自动搜，等按钮/回车（用户要求的显式搜索）。
  $("#searchInput").addEventListener("input", () => {
    if (S.q && !$("#searchInput").value.trim()) exitSearch();
  });

  // 顶栏右侧组空间不够时会整组向左溢出(justify-content:flex-end), 画到搜索框右端上。
  // 溢出宽度随窗口宽度/登录头像数量/「只看某平台」芯片变化, 写死偏移必然有的窗口不适用。
  // 量出被压住的宽度, 等量补到搜索框右内边距 —— 输入框和按钮一起左让, 空间够了自动归位。
  let fitPad0 = 0;
  function fitSearchBtn() {
    const box = $("#topSearch"), btn = $("#btnSearch"), wrap = $("#topRightWrap");
    if (!box || !btn || !wrap) return;
    if (!fitPad0) fitPad0 = parseFloat(getComputedStyle(box).paddingRight) || 9;
    let left = Infinity;
    Array.from(wrap.children).forEach((el) => {
      if (!el.getClientRects().length) return;
      left = Math.min(left, el.getBoundingClientRect().left);
    });
    const br = box.getBoundingClientRect();
    const cover = Number.isFinite(left) ? Math.max(0, br.right - left) : 0;
    const room = Math.max(0, br.width - 100);      // 再挤也要留出图标、按钮和一点输入区
    const next = (fitPad0 + Math.min(Math.ceil(cover), room)) + "px";
    if (box.style.paddingRight !== next) box.style.paddingRight = next;
  }
  fitSearchBtn();
  window.addEventListener("resize", fitSearchBtn);
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(fitSearchBtn);
    [$("#topbar"), $("#topRightWrap")].forEach((el) => el && ro.observe(el));
    $$("#topRightWrap > *").forEach((el) => ro.observe(el));
  }

  $$(".nav[data-nav]").forEach((b) => (b.onclick = () => {
    const v = b.dataset.nav;
    if (v === "chat") {
      // AI 聊天不是一个视图，是右栏的开关：宽屏收放右栏，窄屏整屏盖出来（显式开关，不再让面板默认盖住页面）
      if (narrow()) { document.body.classList.toggle("chatOpen"); syncTabs(); }
      else { prefs.re = !prefs.re; savePrefs(); applyPrefs(); if (!prefs.re) $("#chatInput").focus(); }
      closeDrawer();
      return;
    }
    closeDrawer();
    if (v === "msg") return setView("msg");
    setView(v);
  }));
  $$(".nav.src").forEach((b) => (b.onclick = () => {
    S.src = (S.src === b.dataset.src) ? "" : b.dataset.src;
    applySrcFilter();
    saveState();
    closeDrawer();
    restartFeed();      // 换一路内容：作废在途请求 + 重取（后端只送这个平台的流）
  }));

  /* ---------- 百度安全验证：人工拖滑块的那条恢复路（v0.3.1.55） ---------- */
  // 贴吧 403 是出口 IP 被百度 WAF 拦，代码绕不过去，但用户拖一下滑块就能拿到
  // "已验证"的会话。点左栏那行「点这里验证」→ 打开真浏览器窗口 → 用户拖滑块 →
  // 这里轮询，后端用浏览器里的 cookie 真打一次列表页，通了就保存并自动重抓。
  let tbVerifyTimer = 0;
  function stopTbVerifyPoll() {
    if (tbVerifyTimer) { clearInterval(tbVerifyTimer); tbVerifyTimer = 0; }
  }
  async function pollTbVerify() {
    try {
      const r = await api("/api/tieba/verify/poll");
      const st = r.status || "";
      if (st === "ok") {
        stopTbVerifyPoll();
        toast("验证通过，正在重新抓取贴吧");
        loadStatus();
        restartFeed();
        return;
      }
      if (st === "expired" || st === "closed" || st === "stopped") {
        stopTbVerifyPoll();
        if (st === "expired") toast(r.message || "验证超时，请再点一次", true);
        loadStatus();
        return;
      }
      if (r.message) toast(r.message);
    } catch (e) {
      stopTbVerifyPoll();
      toast("验证状态查询失败：" + e, true);
    }
  }
  async function startTbVerify() {
    const r = await api("/api/tieba/verify/start", {});
    if (!r || r.ok === false) {
      toast((r && r.error) || "打不开验证窗口", true);
      return;
    }
    toast(r.message || "请在验证窗口里拖动滑块");
    stopTbVerifyPoll();
    // 2 秒一次, 最多 5 分钟(后端超时是 10 分钟, 这里只是别无限轮询)
    let n = 0;
    tbVerifyTimer = setInterval(() => {
      n += 1;
      if (n > 150) { stopTbVerifyPoll(); return; }
      pollTbVerify();
    }, 2000);
  }
  // 「点这里验证」那一行(只在贴吧被拦时出现) —— 用事件委托, 状态每次刷新都会重画
  $("#railLeft").addEventListener("click", (e) => {
    // 「未配置吧列表」这类提示行：点它直接去对应设置面板，而不是让人自己找
    const go = e.target.closest && e.target.closest(".st[data-goto]");
    if (go) {
      e.preventDefault();
      e.stopPropagation();        // 别顺带触发"只看该平台"的筛选
      goSettings(go.dataset.goto);
      return;
    }
    const box = e.target.closest && e.target.closest('.st[data-verify="1"]');
    if (!box) return;
    e.preventDefault();
    e.stopPropagation();
    startTbVerify();
  });

  /* ---------- 个人面板（左下角头像） ---------- */
  // 个人中心不再单开一页：左下角头像直接进「设置 → 平台账号」（避免两处重复一套）
  async function loadAccount() {
    // /api/account 是毫秒级, /api/messages 要真去三个平台查未读(2.7–3.7 秒)。
    // 原来两个串起来 await, 于是刷新后左下角会假显示"未登录"三四秒 —— 现在快慢分开。
    try {
      const d = await api("/api/account");
      const name = d.ok ? (d.nickname || "已登录") : "未登录";
      $("#meName").textContent = name;
      // B站头像图床按 Referer 防盗链，直连会被拒 —— 走 /api/avatar 本地代理（旧版同做法）
      const ava = $("#meAva"), initial = name.slice(0, 1);
      if (d.ok && d.face) {
        const img = new Image();
        img.alt = "";
        img.src = "/api/avatar?t=" + Date.now();
        img.onerror = () => { ava.textContent = initial; };   // 代理也取不到就退回首字母，不留空圈
        ava.textContent = "";
        ava.appendChild(img);
      } else {
        ava.textContent = initial;
      }
      const set = (sel, txt) => { const e = $(sel); if (e) e.textContent = txt; };
      set("#accBiliState", d.ok ? "已登录：" + name : "未登录");
      if (d.ok) setAva("#avaBili", "/api/avatar?t=" + Date.now(), name.slice(0, 1));
      else $("#avaBili").textContent = "";
      // 顶栏（搜索框右侧）的 B站 头像：和旧版一样，登录了才显示
      const bBox = $("#topAvaBiliBox");
      if (bBox) {
        bBox.hidden = !d.ok;
        if (d.ok) setAva("#topAvaBili", "/api/avatar?t=" + Date.now(), name.slice(0, 1));
        syncTopAvatars();
      }
      // 账号画像（旧版在个人中心里显示）：知道它到底拿你的历史学到了什么，同步才不是黑盒
      const p = $("#accProfile");
      if (p) {
        if (!d.ok) p.innerHTML = "";
        else {
          const line = (label, list) => "<b>" + label + "</b>：" + (list.length ? list.join("、") : "暂无");
          p.innerHTML = "<div>" +
            line("常看作者", (d.authors || []).map((a) => esc(a.name))) + "<br>" +
            line("高频关键词", (d.keywords || []).map(esc)) + "<br>" +
            line("收藏夹", (d.favs || []).map((f) => esc(f.title) + " (" + f.count + ")")) + "</div>";
        }
      }
    } catch (e) {}
    try {                                   // 贴吧/小黑盒的状态依赖慢接口, 单独走
      const m = await api("/api/messages");
      const set = (sel, txt) => { const e = $(sel); if (e) e.textContent = txt; };
      set("#accTbState", (m.tieba && m.tieba.logged_in) ? "已登录" : "未登录");
      const tbOn = !!(m.tieba && m.tieba.logged_in);
      if (tbOn) setAva("#avaTb", "/api/tieba/avatar?t=" + Date.now(), "贴");
      set("#accXhhState", (m.xhh && m.xhh.logged_in) ? "已登录" : "未登录");
      const xhhOn = !!(m.xhh && m.xhh.logged_in);
      if (xhhOn) setAva("#avaXhh", "/api/xhh/avatar?t=" + Date.now(), "黑");
      // 顶栏头像：贴吧 / 小黑盒
      const tbBox = $("#topAvaTbBox"), xBox = $("#topAvaXhhBox");
      if (tbBox) {
        tbBox.hidden = !tbOn;
        if (tbOn) setAva("#topAvaTb", "/api/tieba/avatar?t=" + Date.now(), "贴");
      }
      if (xBox) {
        xBox.hidden = !xhhOn;
        if (xhhOn) setAva("#topAvaXhh", "/api/xhh/avatar?t=" + Date.now(), "盒");
      }
      syncTopAvatars();
    } catch (e) {}
  }
  // 三枚头像都没有登录态时，整块（含间隙）一起收起来，顶栏不留空位
  function syncTopAvatars() {
    const box = $("#topAvatars");
    if (!box) return;
    box.hidden = !$$("#topAvatars .tava").some((b) => !b.hidden);
    fitSearchBtn();
  }
  // 点头像 = 进「设置 → 平台账号」（旧版点头像也是去账号页）
  $("#topAvatars").onclick = (e) => {
    if (!e.target.closest(".tava")) return;
    setView("settings");
    showSec("sources");
  };
  // （原「个人面板」弹窗已删除：并入 设置 → 平台账号，头像直接跳过去）

  /* ---------- 设置视图：详情在中间，导航在左栏（顶替原来的导航） ---------- */
  function currentSec() { const on = $(".svItem.on"); return on ? on.dataset.sec : "general"; }
  function showSec(sec) {
    $$(".svItem").forEach((b) => b.classList.toggle("on", b.dataset.sec === sec));
    $$(".svPanel").forEach((p) => p.classList.toggle("on", p.dataset.sec === sec));
    // 中区顶部显示当前分组名：二级界面里也要知道「你在哪」
    const on = $(".svItem.on"), t = $("#svTitle");
    if (t && on) t.textContent = (on.textContent || "").trim();
    saveState();
    closeDrawer();          // 窄屏选完分组就收起抽屉，详情才看得见
    syncBlFade();           // 切到筛选页时才量得到拉黑名单框的真实高度
  }
  $$(".svItem").forEach((b) => (b.onclick = () => showSec(b.dataset.sec)));
  // 从任何地方"直达某个设置面板"（空态按钮、状态卡警告、左栏提示行都走这里）
  function goSettings(sec) {
    setView("settings");
    if (sec) showSec(sec);
    document.body.classList.remove("drawer");
  }
  // 右栏在没有 AI 时别摆一个"发不出话"的输入框：换成一张去配置的卡。
  // 只在「一条真消息都没有」时替换，绝不覆盖用户已有的对话。
  function paintChatHint(show) {
    const box = $("#msgs");
    if (!box) return;
    const only = box.children.length === 1 && box.firstElementChild &&
                 box.firstElementChild.classList.contains("msg") &&
                 /告诉 AI 你想看什么样的信息/.test(box.textContent);
    if (show && only) {
      box.innerHTML = '<div class="chatHint"><b>AI 还没配置</b>' +
        "<span>不配也能用：信息流按热度排序。配一个 API Key 之后，它能按你的口味挑内容，还能在聊天里改设置。</span>" +
        '<button type="button" data-empty-act="ai">去配置 AI</button></div>';
      return;
    }
    if (!show && box.querySelector(".chatHint")) {
      box.innerHTML = '<div class="msg ai">告诉 AI 你想看什么样的信息</div>';
    }
  }
  $("#msgs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-empty-act]");
    if (b) onEmptyAct(b.dataset.emptyAct);
  });
  $("#btnBack").onclick = () => setView(S.lastList || "feed");
  $("#btnSettings").onclick = () => {
    setView("settings");
    if (narrow()) document.body.classList.add("drawer");
  };
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // 引导在最上层：Esc 先关它，别让用户觉得"卡住了"
    if ($("#intro") && !$("#intro").hidden) { closeIntro(); introDone(); return; }
    // 弹窗优先：它盖住整屏，而且里面有正在播放的播放器 —— 键盘也得能关掉它
    if ($("#mask").classList.contains("show")) { closeModal(); return; }
    if (document.body.classList.contains("drawer")) { closeDrawer(); return; }
    if (PANES[S.view]) setView(S.lastList || "feed");
  });

  /* ================= 设置项：读 / 写 ================= */
  async function loadSettings() {
    try {
      const s = await api("/api/settings");
      $("#aiBase").value = s.ai_base || "";
      $("#aiKey").value = s.ai_key || "";
      $("#aiModel").value = s.ai_model || "";
      $("#prefsNote").value = s.prefs_note || "";
      $("#dislikeNote").value = s.dislike_note || "";
      $("#modeSel").value = s.mode || "严格";
      const m2 = $("#modeSel2"); if (m2) m2.value = s.mode || "严格";
      const on = s.ai_enabled === "1";
      $("#aiToggle").textContent = on ? "已开启 AI 功能" : "开启 AI 功能";
      $("#aiToggle").classList.toggle("on", on);
      $("#aiState").textContent = s.ai_ok ? "已配置" : "未配置";
      const pi = $("#priceIn"), po = $("#priceOut");
      if (pi) pi.value = s.price_in || "";
      if (po) po.value = s.price_out || "";
      // B站增强：超分用「开关 + 档位」两个键存，档位=关 时开关为 0（旧版同做法）
      segPick("#srLevelRow", s.ui_sr === "0" ? "关" : (s.ui_sr_level || "关"));
      segPick("#srScaleRow", s.ui_sr_scale || "2");
      segPick("#bqLevelRow", s.ui_bq === "0" ? "关闭" : (s.ui_bq_level || "中"));
      segPick("#ownuiRow", s.ui_ownui || "1");
      segPick("#openModeRow", s.ui_new_window === "0" ? "0" : "1");
      segPick("#browserRow", s.ui_search_engine || "bing");
      segPick("#browserStartRow", s.ui_browser_on_start === "1" ? "1" : "0");
    } catch (e) { toast("读取设置失败：" + e, true); }
  }
  // 分段选择器：读取时点亮当前档，点击即保存（旧版同做法，省一次"保存"点击）
  function segPick(sel, val) {
    $$(sel + " .seg").forEach((b) => b.classList.toggle("on", b.dataset.v === String(val)));
  }
  // 只改高亮、不落库的那种档位行（所在分组自己有「保存」按钮）
  function segSelect(sel) {
    const box = $(sel);
    if (!box) return;
    box.addEventListener("click", (e) => {
      const b = e.target.closest(".seg");
      if (!b) return;
      $$(sel + " .seg").forEach((x) => x.classList.toggle("on", x === b));
    });
  }
  segSelect("#cfLvlRow");
  segSelect("#cfMultiRow");
  segSelect("#cfSpeedRow");
  segSelect("#ftThreshRow");
  segSelect("#ftPointRow");
  function segBind(sel, key, label) {
    const box = $(sel);
    if (!box) return;
    box.addEventListener("click", async (e) => {
      const b = e.target.closest(".seg");
      if (!b) return;
      const old = (box.querySelector(".seg.on") || {}).dataset?.v;
      segPick(sel, b.dataset.v);
      if (!(await save("/api/settings", { [key]: b.dataset.v }, label || "保存 AI 设置"))) segPick(sel, old);
      else loadSettings();
    });
  }
  segBind("#srScaleRow", "ui_sr_scale", "保存超分倍率");
  segBind("#ownuiRow", "ui_ownui", "保存播放页自有 UI");
  segBind("#openModeRow", "ui_new_window", "保存打开方式");
  segBind("#browserRow", "ui_search_engine", "保存搜索引擎");
  segBind("#browserStartRow", "ui_browser_on_start", "保存启动行为");
  // 超分 / 增强强度：界面上是一个「档位」，后端存的是「开关 + 档位」两个键。
  // 档位选「关」= 开关写 0（脚本据此整段不启用），选别的 = 开关写 1 + 档位名。
  const segBindPair = (sel, onKey, lvlKey, offVal, label) => {
    const box = $(sel);
    if (!box) return;
    box.addEventListener("click", async (e) => {
      const b = e.target.closest(".seg");
      if (!b) return;
      const old = (box.querySelector(".seg.on") || {}).dataset?.v;
      segPick(sel, b.dataset.v);
      const off = b.dataset.v === offVal;
      const r = await save("/api/settings", { [onKey]: off ? "0" : "1", [lvlKey]: b.dataset.v }, label);
      if (!r) segPick(sel, old); else loadSettings();
    });
  };
  segBindPair("#srLevelRow", "ui_sr", "ui_sr_level", "关", "保存超分档位");
  segBindPair("#bqLevelRow", "ui_bq", "ui_bq_level", "关闭", "保存增强强度");
  $("#priceSave").onclick = async () => {
    const num = (id) => String(Math.max(0, parseFloat(($(id) || {}).value) || 0));
    const r = await save("/api/settings", { price_in: num("#priceIn"), price_out: num("#priceOut") }, "保存单价");
    if (r) loadSettings();
  };
  // 模型预置（旧版有个 ▾ 下拉；新版用原生 datalist，输入框本身仍是自由输入）
  const MODEL_PRESETS = [
    ["DeepSeek", ["deepseek-flash", "deepseek-v4-pro"]],
    ["智谱 GLM", ["glm-4-plus", "glm-4-flash"]],
    ["通义千问", ["qwen-turbo", "qwen-plus", "qwen-max"]],
    ["OpenAI 兼容", ["gpt-4o-mini", "gpt-4o"]],
    ["本地 Ollama", ["qwen3:8b", "qwen3:4b"]],
  ];
  $("#modelPresets").innerHTML = MODEL_PRESETS.map(([g, ms]) =>
    ms.map((m) => '<option value="' + esc(m) + '" label="' + esc(g) + '">').join("")).join("");
  /* 屏幕悬浮球（仅 Windows 电脑版；其它平台后端会如实回 supported=false + 原因） */
  function applyFloatBall(d) {
    const btn = $("#floatBallToggle"), st = $("#floatBallState");
    if (!btn) return;
    btn.classList.toggle("on", !!d.enabled);
    btn.textContent = d.enabled ? "关闭悬浮窗" : "开启悬浮窗";
    st.textContent = !d.supported ? ("状态：本机不支持 —— " + (d.reason || "环境不支持"))
      : (d.enabled ? (d.running ? "状态：已开启，球在屏幕上" : "状态：已开启但进程没起来" + (d.spawn_error ? "（" + d.spawn_error + "）" : ""))
                   : "状态：未开启");
  }
  async function refreshFloatBall() {
    try { applyFloatBall(await api("/api/floatball")); } catch (e) {}
  }
  $("#floatBallToggle").onclick = async () => {
    const on = $("#floatBallToggle").classList.contains("on");
    try {
      const d = await api("/api/floatball", { method: "POST", body: JSON.stringify({ enabled: !on }) });
      // POST 的回执里没有 enabled（只有 GET 有），开关状态用刚才那次操作的结果补上
      applyFloatBall(Object.assign({}, d, { enabled: !on }));
      if (d.supported) toast(!on ? "悬浮球已出现在屏幕上" : "悬浮窗已关闭", !on);
      else toast("本机无法显示悬浮球：" + (d.reason || "环境不支持"), true);
      // 拉起后复查一次：进程闪退（例如缺 tkinter）要如实说出来，别让按钮看着像成功了
      if (!on && d.supported) setTimeout(refreshFloatBall, 1500);
    } catch (e) { toast("操作失败：" + (e.message || e), true); }
  };
  // 「找不到球？」：球可能被拖到屏幕边缘外、被置顶窗口盖住、或位置记在换屏前的坐标上。
  // 球是独立进程，靠 2.5 秒一次的心跳取信号 —— 发出请求不等于球收到，必须等回执才算数。
  $("#floatBallFind").onclick = async () => {
    try {
      const d = await api("/api/floatball");
      if (!d.supported) { toast("本机无法显示悬浮球：" + (d.reason || "环境不支持"), true); return; }
      if (!d.running) { toast("悬浮球当前没在运行，先点「开启悬浮窗」", true); return; }
      const r = await api("/api/ball_recall_go");
      const want = (r && r.n) || 0;
      toast("已发出定位请求，正在等悬浮球响应…");
      for (let i = 0; i < 9; i++) {
        await new Promise((ok) => setTimeout(ok, 600));
        const s = await api("/api/ball_recall");
        if ((s.ack || 0) >= want) { toast("悬浮球已回到屏幕右侧（闪三下的那个就是它）", true); return; }
      }
      toast("悬浮球没有响应，可能已经退出 —— 重新点「开启悬浮窗」试试", true);
      refreshFloatBall();
    } catch (e) { toast("定位失败：" + (e.message || e), true); }
  };
  $("#aiToggle").onclick = async () => {
    const on = !$("#aiToggle").classList.contains("on");
    const r = await save("/api/settings", { ai_enabled: on ? "1" : "0" }, "切换 AI 开关");
    if (!r) return;            // 失败了就不再回读，避免同一次操作弹两条
    loadSettings();
    // 只有「开启」要提醒（会花钱）；关闭是成功操作，界面自己会表达，不弹
    if (on) toast("AI 功能已开启（会消耗 token）", true);
  };
  $("#aiSave").onclick = async () => {
    const r = await save("/api/settings", {
      ai_base: $("#aiBase").value.trim(), ai_key: $("#aiKey").value.trim(), ai_model: $("#aiModel").value.trim()
    }, "保存 AI 配置");
    if (r) loadSettings();     // 失败了就不再回读，避免同一次操作弹两条
  };
  $("#noteSave").onclick = async () => {
    await save("/api/settings", { prefs_note: $("#prefsNote").value, dislike_note: $("#dislikeNote").value }, "保存备注");
  };
  const onMode = async (v) => {
    await save("/api/settings", { mode: v }, "切换档位");
    $("#modeSel").value = v;
    const m2 = $("#modeSel2"); if (m2) m2.value = v;
  };
  $("#modeSel").onchange = () => onMode($("#modeSel").value);
  const m2el = $("#modeSel2"); if (m2el) m2el.onchange = () => onMode(m2el.value);

  SEG_ROWS.forEach(([sel, key]) => $$(sel + " .seg").forEach((b) => (b.onclick = () => {
    prefs[key] = b.dataset[key];
    // 换界面模式 = 深度回到「跟随模式」，否则手动拉过的深度会把模式盖回去
    if (key === "theme") prefs.bgDepth = 0;
    savePrefs(); applyPrefs();
  })));
  $("#accentRow").addEventListener("click", (e) => {
    const b = e.target.closest(".sw"); if (!b) return;
    prefs.accent = b.dataset.c; savePrefs(); applyPrefs();
  });
  // 自定义强调色：调色盘没有的颜色也能用（旧版有，新版补上）
  $("#accentCustomApply").onclick = () => {
    const c = ($("#accentCustom").value || "").trim();
    if (!/^#[0-9a-f]{6}$/i.test(c)) { toast("颜色格式不对", true); return; }
    prefs.accent = c;
    localStorage.setItem(CUSTOM_ACCENT_KEY, c);
    savePrefs(); applyPrefs();
  };
  /* 背景颜色 / 颜色深度（旧版有，新版补上）：只动底色那一族变量，不影响强调色与布局 */
  $("#bgRow").addEventListener("click", (e) => {
    const b = e.target.closest(".sw");
    if (!b) return;
    prefs.bgHex = b.dataset.hex || "";
    savePrefs(); applyPrefs();
  });
  $("#bgColorApply").onclick = () => {
    const c = ($("#bgColor").value || "").trim().toLowerCase();
    if (!/^#[0-9a-f]{6}$/i.test(c)) { toast("颜色格式不对", true); return; }
    let list = [];
    try { list = JSON.parse(localStorage.getItem(BG_CUSTOM_KEY) || "[]"); } catch (err) {}
    if (!Array.isArray(list)) list = [];
    list = Array.from(new Set([c].concat(list))).slice(0, 12);
    localStorage.setItem(BG_CUSTOM_KEY, JSON.stringify(list));
    prefs.bgHex = c;
    savePrefs(); applyPrefs();
  };
  $("#bgReset").onclick = () => {
    prefs.bgHex = ""; prefs.bgDepth = 0;
    savePrefs(); applyPrefs();
  };
  $("#bgDepth").addEventListener("input", (e) => {
    const d = parseInt(e.target.value, 10);
    prefs.bgDepth = d;
    $("#bgDepthVal").textContent = String(d);
    savePrefs(); applyBg(); renderBgRow(); pushTheme();
  });
  $("#bgDepth").addEventListener("change", savePrefs);

  /* ================= 机器筛选（纯程序规则，先于 AI 执行）================= */
  // 契约来自旧版：读 /api/machine（settings + source_groups + blocked + account_ok）、
  // 分区与阈值写 /api/machine、平台权重与吧列表写 /api/tieba、拉黑名单同步与移除走 /api/machine/blocklist-*。
  let srcGroups = [], platWs = {};
  // 平台显示名 → 设置 key。左栏滑条与设置里的档位按钮共用这张表。
  const PLAT_KEY = { "B站": "bili_weight", "贴吧": "tb_weight", "小黑盒": "xhh_weight", "B站直播": "live_weight" };
  /* 占比只走这几档(v0.3.1.53 用户要求: 一般 25% 一档, 另外补一个 5% 的小档)。
     拖到两档中间会被吸到最近的一档, 所以滑条不会停在 37% 这种没意义的值上。 */
  const SHARE_STEPS = [0, 5, 25, 50, 75, 100];
  function snapShare(v) {
    let best = SHARE_STEPS[0], bd = Math.abs(v - best);
    for (const s of SHARE_STEPS) {
      const d = Math.abs(v - s);
      if (d < bd || (d === bd && s > best)) { best = s; bd = d; }
    }
    return best;
  }
  /* 拉黑名单滚动框的"下面还有"提示: 只在真能滚动、且没滚到底时露渐隐。
     系统滚动条在 Windows 11 上是悬浮条(平时不画), 所以这里同时维护自绘的那条。 */
  function syncBlFade() {
    const w = $("#mBlWrap"), l = $("#mBlList");
    if (!w || !l) return;
    const more = l.scrollHeight > l.clientHeight + 4 &&
                 l.scrollTop + l.clientHeight < l.scrollHeight - 6;
    w.classList.toggle("more", more);
    const over = l.scrollHeight > l.clientHeight + 4;
    w.classList.toggle("ovl", over);
    if (!over) return;
    const bar = $("#mBlBar"), th = bar && bar.firstElementChild;
    if (!th) return;
    const h = Math.max(24, Math.round(l.clientHeight * l.clientHeight / l.scrollHeight));
    const room = Math.max(1, l.scrollHeight - l.clientHeight);
    const top = Math.round((l.scrollTop / room) * (l.clientHeight - h));
    th.style.height = h + "px";
    th.style.transform = "translateY(" + top + "px)";
  }
  (function blBarDrag() {
    const bar = $("#mBlBar"), l = $("#mBlList");
    if (!bar || !l) return;
    const seek = (e) => {
      const r = bar.getBoundingClientRect(), th = bar.firstElementChild;
      const h = parseFloat(th.style.height) || 24;
      const room = Math.max(1, r.height - h);
      const y = Math.max(0, Math.min(room, e.clientY - r.top - h / 2));
      l.scrollTop = (y / room) * (l.scrollHeight - l.clientHeight);
      syncBlFade();
    };
    bar.addEventListener("pointerdown", (e) => { bar.setPointerCapture(e.pointerId); seek(e); });
    bar.addEventListener("pointermove", (e) => { if (bar.hasPointerCapture(e.pointerId)) seek(e); });
  })();
  function machineTab(name) {
    $$("#mTabs .seg").forEach((b) => b.classList.toggle("on", b.dataset.m === name));
    $$(".mPane").forEach((p) => p.classList.toggle("on", p.dataset.m === name));
    syncBlFade();   // 面板刚显示时才有真实高度, 这时候量才对
  }
  $$("#mTabs .seg").forEach((b) => (b.onclick = () => machineTab(b.dataset.m)));

  function renderSrcChips() {
    const box = $("#srcChips");
    if (!box) return;
    box.innerHTML = srcGroups.map((g) =>
      '<span class="chip ' + (g.on ? "on2" : "") + '" data-gid="' + esc(g.id) + '">' + esc(g.name) + "</span>").join("");
  }
  $("#srcChips").addEventListener("click", async (e) => {
    const c = e.target.closest(".chip");
    if (!c) return;
    const g = srcGroups.find((x) => x.id === c.dataset.gid);
    if (!g) return;
    g.on = !g.on;
    renderSrcChips();
    const ids = srcGroups.filter((x) => x.on).map((x) => x.id);
    if (!(await save("/api/machine", { m_sources: ids }, "切换分区"))) { g.on = !g.on; renderSrcChips(); return; }
    restartFeed();      // 分区变了 = 换一路内容
  });

  function renderPlatWeights() {
    const box = $("#platWeightRows");
    if (!box) return;
    const levelsOf = (cur) => base.indexOf(cur) >= 0 ? base : base.concat([cur]).sort((x, y) => x - y);
    void levelsOf;
    // 档位 0/25/50/75/100 里未必包含当前值（默认 B站 90、小黑盒 15 都不在），
    // 那样"当前选中的档"就显示不出来 —— 把当前值也作为一个可选档补进去。
    const base = SHARE_STEPS;
    box.innerHTML = Object.keys(platWs).map((name) =>
      '<div class="pwRow"><span class="lbl">' + esc(name) + "</span>" +
      levelsOf(platWs[name]).map((w) => '<button class="seg ' + (platWs[name] === w ? "on" : "") + '" data-w="' + w +
        '" data-name="' + esc(name) + '">' + w + "</button>").join("") + "</div>").join("");
    renderRailShares();   // 左栏那几条滑条与这里是同一份数据, 一改就同步
  }
  $("#platWeightRows").addEventListener("click", (e) => {
    const b = e.target.closest(".seg");
    if (!b) return;
    platWs[b.dataset.name] = parseInt(b.dataset.w, 10);
    renderPlatWeights();
    commitShares(120);   // 与左栏滑条同一条写库路径(攒一下再发, 连点也不会互相覆盖)
  });

  /* 滑条已填充那一段由 --pct 画(见 style.css input[type=range])：
     只用 accent-color 的话整条都是高饱和强调色, 一屏几条并排非常晃眼(用户报过)。
     值变了就同步一次; 设置里的底色深度/关注月份和左栏占比都走这一套。 */
  function paintRange(inp) {
    const min = +inp.min || 0, max = +inp.max || 100, v = +inp.value || 0;
    const pct = max > min ? ((v - min) * 100) / (max - min) : 0;
    inp.style.setProperty("--pct", Math.max(0, Math.min(100, pct)).toFixed(1) + "%");
  }
  function paintRanges() { $$("input[type=range]").forEach(paintRange); }
  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t && t.type === "range") paintRange(t);
  });

  /* 左栏内容源每栏下面那条占比滑条(v0.3.1.52) —— 和设置→筛选里的档位按钮共用 platWs,
     在哪边改另一边立刻跟着变; 拖完(change)才写库并重建信息流, 拖动过程中只更新数字。
     权重语义没变: 占比 = w / Σw, 0 = 这一栏不出现。 */
  function paintShare(inp) {
    const it = inp.closest(".srcItem");
    const pc = it && it.querySelector(".pc");
    const cur = parseInt(inp.value, 10) || 0;
    if (pc) {
      pc.textContent = cur + "%";
      pc.classList.remove("warn");   // v0.3.1.57: 不再标黄, 档位数字用中性色
    }
    inp.title = (inp.dataset.w || "") + " 混排占比 " + cur +
      "%（档位 0 / 5 / 25 / 50 / 75 / 100，按权重与其它栏比例混排；0 = 这一栏不出现）" +
      ((inp.dataset.w === "B站直播" && cur > 0 && cur < 15) ? " —— 比例这么低, 直播会很稀疏" : "");
    paintRange(inp);
  }
  function renderRailShares() {
    $$(".railSec.grow .srcItem input[type=range]").forEach((inp) => {
      const w = platWs[inp.dataset.w];
      if (typeof w === "number") inp.value = String(w);
      paintShare(inp);
    });
    paintRanges();
  }
  // 连按方向键会一秒内触发十几次 change: 攒到停手后再写一次库, 免得并发 POST 互相覆盖、
  // 也没必要每按一下都重建整条信息流(实测会打出空响应、把界面上的数字回滚成旧值)。
  let shareSaveTimer = 0;
  function commitShares(delay) {
    clearTimeout(shareSaveTimer);
    shareSaveTimer = setTimeout(async () => {
      const body = {};
      Object.keys(platWs).forEach((n) => { if (PLAT_KEY[n]) body[PLAT_KEY[n]] = String(platWs[n]); });
      if (await save("/api/tieba", body, "保存平台比例")) restartFeed();
      else loadTiebaSettings();   // 写失败就按服务器的真实值重画, 不留一个骗人的数字
    }, delay);
  }
  $$(".railSec.grow .srcItem input[type=range]").forEach((inp) => {
    inp.addEventListener("input", () => {
      // 拖动/点击轨道都先吸到档位上, 再画数字
      const v = parseInt(inp.value, 10) || 0;
      const s = snapShare(v);
      if (s !== v) inp.value = String(s);
      paintShare(inp);
    });
    // 原生 step 是 1, 按方向键只挪 1% 就会被吸回原档(等于按不动) ——
    // 键盘自己按档位走: ←→ 一档, Home/End 到两端。
    inp.addEventListener("keydown", (e) => {
      const k = e.key;
      const dir = (k === "ArrowRight" || k === "ArrowUp") ? 1
        : (k === "ArrowLeft" || k === "ArrowDown") ? -1 : 0;
      if (!dir && k !== "Home" && k !== "End") return;
      e.preventDefault();
      const cur = snapShare(parseInt(inp.value, 10) || 0);
      let i = SHARE_STEPS.indexOf(cur) + dir;
      if (k === "Home") i = 0;
      if (k === "End") i = SHARE_STEPS.length - 1;
      i = Math.max(0, Math.min(SHARE_STEPS.length - 1, i));
      inp.value = String(SHARE_STEPS[i]);
      paintShare(inp);
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    });
    inp.addEventListener("change", () => {
      const name = inp.dataset.w;
      platWs[name] = parseInt(inp.value, 10) || 0;
      renderPlatWeights();   // 设置里那组档位跟着走
      commitShares(300);
    });
  });

  async function loadTiebaSettings() {
    try {
      const d = await api("/api/tieba");
      const el = $("#tbBars");
      if (el) el.value = (d.bars || []).join("\n");
      const ah = $("#tbAllowHelp");
      if (ah) ah.checked = d.allow_help !== false;
      platWs = {};
      (d.platforms || []).forEach((p) => { platWs[p.name] = p.weight; });
      renderPlatWeights();
      const sel = $("#tbBrowseKw");
      if (sel) {
        const bars = d.bars || [];
        sel.innerHTML = bars.length ? bars.map((b) => '<option>' + esc(b) + "</option>").join("")
                                    : '<option value="">（先填吧列表）</option>';
      }
    } catch (e) {}
  }

  async function loadMachine() {
    try {
      const d = await api("/api/machine");
      const st = d.settings || {};
      srcGroups = (d.source_groups || []).map((g) => ({ id: g.id, name: g.name, on: !!g.on }));
      renderSrcChips();
      const setV = (id, v) => { const e = $(id); if (e) e.value = v; };
      const setC = (id, v) => { const e = $(id); if (e) e.checked = !!v; };
      setV("#mMinViews", st.m_min_views || "0");
      setV("#mMaxViews", st.m_max_views || "0");
      setV("#mMaxDur", Math.round((parseInt(st.m_max_duration, 10) || 0) / 60));
      setV("#mMinDur", Math.round((parseInt(st.m_min_duration, 10) || 60) / 60));
      setV("#mMaxAge", st.m_max_age_months || "5");
      setV("#mSerMaxAge", st.ser_max_age_months || "30");
      setV("#mSeenHours", st.m_seen_hours || "6");
      setV("#mRepeatLimit", st.m_repeat_limit || "4");
      setV("#mRepeatCooldown", st.m_repeat_cooldown_hours || "72");
      setC("#mDedup", st.m_dedup === "1");
      setC("#mEmotion", st.m_emotion !== "0");
      setC("#mNewsFresh", st.m_news_fresh !== "0");
      setC("#mPlayRel", st.m_play_related !== "0");
      setC("#mBlockSync", st.m_blocklist_sync === "1");
      // 评论 / 弹幕筛选（B站播放页内生效）
      setC("#cfDmOn", st.m_dm_on !== "0");
      setC("#cfBlkOn", st.cf_blk_on !== "0");
      setC("#cfBlkMystery", st.m_block_mystery_bio === "1");
      setC("#cfBlkLottery", st.m_block_lottery === "1");
      setC("#cfBlkEmpty", st.m_block_empty_space === "1");
      segPick("#cfLvlRow", st.cf_min_level || "2");
      segPick("#cfMultiRow", st.m_dm_multicand || "1");
      segPick("#cfSpeedRow", st.m_filter_speed || "2");
      ftLoadPanel(st, d.ft, d.bert);
      setC("#mFollowEmph", st.bili_follow_emph !== "0");
      const fm = $("#mFollowMonths");
      if (fm) { fm.value = st.bili_follow_months || "1"; $("#mFollowMonthsVal").textContent = fm.value; }
      paintRanges();
      const coldV = String(st.m_cold_start_n || "10");
      $$("#mColdRow .seg").forEach((b) => b.classList.toggle("on", b.dataset.v === coldV));
      const xc = $("#xhhComm"); if (xc) xc.value = st.xhh_comm || "";
      const xt = $("#xhhTopic"); if (xt) xt.value = st.xhh_topic || "";
      $("#mBlStatus").textContent = d.account_ok
        ? "已登录 B站账号，可同步你的拉黑名单（登录时也会自动导入）"
        : "需要先登录 B站账号（设置 → 平台账号）";
      const bl = d.blocked || [];
      $("#mBlList").innerHTML = bl.length
        ? bl.map((b) => '<span class="chip">' + esc(b.name) + '<b data-name="' + esc(b.name) + '">' +
            ic("x", "xs") + "</b></span>").join("")
        : '<span class="lbl">暂无已同步的拉黑名单</span>';
      // 框内滚动: 明确告诉用户"后面还有", 光靠滚动条容易被系统悬浮滚动条藏起来
      const hint = $("#mBlHint");
      if (hint) hint.textContent = bl.length
        ? "共 " + bl.length + " 个账号 · 超出部分在框内上下滚动查看（点名字后面的 × 可单独移除）"
        : "";
      syncBlFade();
    } catch (e) { toast("读取筛选设置失败：" + e, true); }
    loadTiebaSettings();
    loadLiveFilter();
  }
  $("#mBlList").addEventListener("scroll", syncBlFade);
  $("#mBlList").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-name]");
    if (!b) return;
    if (!(await save("/api/machine/blocklist-remove", { name: b.dataset.name }, "移除屏蔽"))) return;
    loadMachine(); restartFeed();
  });
  $("#mFollowMonths").addEventListener("input", (e) => { $("#mFollowMonthsVal").textContent = e.target.value; });
  $("#mColdRow").addEventListener("click", async (e) => {
    const b = e.target.closest(".seg");
    if (!b) return;
    $$("#mColdRow .seg").forEach((x) => x.classList.toggle("on", x === b));
    await save("/api/machine", { m_cold_start_n: b.dataset.v }, "保存预缓存档位");
  });
  $("#mGeneralSave").onclick = async () => {
    const num = (id, dflt) => String(parseInt(($(id) || {}).value, 10) || dflt);
    const r = await save("/api/machine", {
      m_min_views: num("#mMinViews", 0),
      m_max_views: num("#mMaxViews", 0),
      m_max_duration: String((parseInt($("#mMaxDur").value, 10) || 0) * 60),
      m_min_duration: String((parseInt($("#mMinDur").value, 10) || 1) * 60),
      m_max_age_months: num("#mMaxAge", 0),
      ser_max_age_months: num("#mSerMaxAge", 0),
      m_seen_hours: num("#mSeenHours", 0),
      m_repeat_limit: String(Math.max(1, parseInt($("#mRepeatLimit").value, 10) || 4)),
      m_repeat_cooldown_hours: String(Math.max(0, parseInt($("#mRepeatCooldown").value, 10) || 72)),
    }, "保存筛选设置");
    if (r) { loadMachine(); restartFeed(); }
  };
  $("#mSave").onclick = async () => {
    const r = await save("/api/machine", {
      m_blocklist_sync: $("#mBlockSync").checked ? "1" : "0",
      m_dedup: $("#mDedup").checked ? "1" : "0",
      m_emotion: $("#mEmotion").checked ? "1" : "0",
      m_news_fresh: $("#mNewsFresh").checked ? "1" : "0",
      m_play_related: $("#mPlayRel").checked ? "1" : "0",
      bili_follow_emph: $("#mFollowEmph").checked ? "1" : "0",
      bili_follow_months: String(parseInt($("#mFollowMonths").value, 10) || 0),
      m_sources: srcGroups.filter((g) => g.on).map((g) => g.id),
    }, "保存 B站筛选");
    if (r) { loadMachine(); restartFeed(); }
  };
  // 评论 / 弹幕筛选单独一个保存按钮（旧版也是分开的：那边管「谁发的」，和上面的规则不共用一次保存）
  $("#cfSave").onclick = async () => {
    const pick = (sel, dflt) => {
      const b = $(sel + " .seg.on");
      return (b && b.dataset.v) || dflt;
    };
    const r = await save("/api/machine", {
      m_dm_on: $("#cfDmOn").checked ? "1" : "0",
      cf_blk_on: $("#cfBlkOn").checked ? "1" : "0",
      m_block_mystery_bio: $("#cfBlkMystery").checked ? "1" : "0",
      m_block_lottery: $("#cfBlkLottery").checked ? "1" : "0",
      m_block_empty_space: $("#cfBlkEmpty").checked ? "1" : "0",
      cf_min_level: pick("#cfLvlRow", "2"),
      m_dm_multicand: pick("#cfMultiRow", "1"),
      m_filter_speed: pick("#cfSpeedRow", "2"),
    }, "保存评论 / 弹幕筛选");
    if (r) loadMachine();
  };
  $("#mBlSync").onclick = async () => {
    const r = await save("/api/machine/blocklist-sync", {}, "同步拉黑名单");
    if (r) { toast("已同步 " + (r.count || 0) + " 位拉黑用户", true); loadMachine(); restartFeed(); }
  };
  $("#xhhSave").onclick = async () => {
    const r = await save("/api/machine", { xhh_comm: $("#xhhComm").value.trim(), xhh_topic: $("#xhhTopic").value.trim() }, "保存小黑盒筛选");
    if (!r) return;
    $("#xhhStatus").textContent = "已保存";
    loadMachine(); restartFeed();
  };
  $("#mLiveSave").onclick = async () => {
    const r = await save("/api/live/settings", {
      min_viewers: parseInt($("#mLiveMin").value, 10) || 0,
      only_followed: $("#mLiveFollowOnly").checked,
    }, "保存直播筛选");
    if (!r) return;
    $("#mLiveStatus").textContent = "已保存（门槛 " + (r.min_viewers || 0) + " 人" +
      (r.only_followed ? "，只看关注" : "") + "）";
    restartFeed();
  };
  async function loadLiveFilter() {
    try {
      const d = await api("/api/live/settings");
      $("#mLiveMin").value = String(d.min_viewers || 0);
      $("#mLiveFollowOnly").checked = !!d.only_followed;
    } catch (e) { /* 状态接口没起来就算了, 不挡别的设置 */ }
  }
  $("#mTiebaSave").onclick = async () => {
    const bars = $("#tbBars").value.split(/[\n,，、;；]+/).map((x) => x.trim()).filter(Boolean);
    const r = await save("/api/tieba", { bars: bars, allow_help: $("#tbAllowHelp").checked }, "保存贴吧筛选");
    if (!r) return;
    $("#mTiebaStatus").textContent = "已保存";
    loadTiebaSettings(); loadStatus(); restartFeed();
  };
  // 风控应急通道：需要本机有 Chrome/Edge（走 CDP 打开贴吧页再抓），没有时会如实报错
  $("#tbBrowseOpen").onclick = async () => {
    const kw = $("#tbBrowseKw").value;
    if (!kw) { toast("请先在「平台账号」里填吧名", true); return; }
    // 这个端点是 GET（旧版也走 GET）。原来误用了 save()，那是 POST ——
    // 服务端没有 POST 分支，请求直接 404，整条应急通道的第一步根本打不开。
    let r = null;
    try { r = await api("/api/tieba/browse?kw=" + encodeURIComponent(kw)); } catch (e) {}
    $("#tbBrowseHint").textContent = r && r.ok ? (r.message || "已打开") : ("打开失败：" + ((r && r.error) || "需要本机有 Chrome/Edge"));
  };
  $("#tbBrowseGrab").onclick = async () => {
    $("#tbBrowseHint").textContent = "抓取中…";
    // 同样走原始 api()：save() 遇到 {ok:false} 会吞掉响应只回 null，
    // 结果提示行只剩「抓取失败：」后面空白，后端那句「窗口未打开…」就传不出来。
    let r = null;
    try { r = await api("/api/tieba/grab", { method: "POST", body: "{}" }); } catch (e) {}
    if (r && r.ok) {
      $("#tbBrowseHint").textContent = r.message || ("已抓取 " + (r.items || 0) + " 条");
      loadStatus(); restartFeed();
    } else {
      const why = (r && r.error) || "未知原因";
      $("#tbBrowseHint").textContent = "抓取失败：" + why;
      toast("抓取失败：" + why, true);
    }
  };

  /* ================= 快速文本分析（本地 fastText：管「说了什么」，与身份类规则独立）================= */
  const FT_LABELS = [["ftLabFlame", "flame"], ["ftLabSpam", "spam"], ["ftLabClickbait", "clickbait"],
                     ["ftLabYy", "yy"], ["ftLabNeg", "neg"], ["ftLabMisinfo", "misinfo"],
                     ["ftLabUnpatriotic", "antisocial"], ["ftLabForeignWorship", "foreign_worship"]];
  function ftStatusText(f, b) {
    if (!f) return "模型状态：读取中…";
    if (b && b.available) {
      const ver = b.version ? " v" + b.version : "";
      const size = (b.size_mb || 0) + " MB";
      if (b.ready) return "模型状态：智能模型" + ver + " 已加载 · " + size +
        (b.infer_ms ? " · 最近一批 " + b.infer_ms + " ms" : "");
      if (!b.enabled) return "模型状态：智能模型" + ver + " 已安装（" + size + "），开启总开关后加载";
      return "模型状态：智能模型" + ver + " 加载中…" + (b.err ? "；" + b.err : "");
    }
    if (b && b.err) return "模型状态：智能模型不可用（" + b.err + "）";
    if (f.has_module === false) return "模型状态：本机缺少运行时";
    if (!f.ready && f.on) return "模型状态：加载中…（首次要几秒）" + (f.err ? "；" + f.err : "");
    if (!f.ready) return "模型状态：未安装智能模型（fastText " + (f.size_mb || 0) + " MB 回退待机）";
    return "模型状态：fastText 回退已加载 " + (f.size_mb || 0) + " MB";
  }
  function ftLoadPanel(st, f, b) {
    if (st) {
      const sc = (id, v) => { const e = $(id); if (e) e.checked = !!v; };
      sc("#ftOn", st.ft_on === "1");
      sc("#ftVideo", st.ft_video !== "0");
      sc("#ftDm", st.ft_dm !== "0");
      sc("#ftCf", st.ft_cf !== "0");
      const have = String(st.ft_labels || "").split(",").map((x) => x.trim()).filter(Boolean);
      // 旧版「不爱国」的键是 unpatriotic, 现在这个槽位就是反社会 → 迁移显示
      if (have.indexOf("unpatriotic") >= 0 && have.indexOf("antisocial") < 0) have.push("antisocial");
      FT_LABELS.forEach(([id, key]) => sc("#" + id, have.indexOf(key) >= 0));
      segPick("#ftThreshRow", st.ft_thresh || "70");
      segPick("#ftPointRow", st.ft_point || "strict");
      const bt = $("#ftBthr");
      if (bt) bt.value = st.ft_bthr || "";
    }
    const el = $("#ftStatus");
    if (el) el.textContent = ftStatusText(f, b);
  }
  async function ftRefreshStatus() {
    try {
      const s = await api("/api/textfilter/status");
      ftLoadPanel(null, s, s && s.bert);
    } catch (e) {}
  }
  $("#ftOn").onclick = async () => {
    const on = $("#ftOn").checked;
    const r = await save("/api/machine", { ft_on: on ? "1" : "0" }, "切换文本分析");
    if (!r) { $("#ftOn").checked = !on; return; }
    loadMachine();                                  // 模型在后台加载，状态行靠补刷两次跟上
    setTimeout(ftRefreshStatus, 1200);
    setTimeout(ftRefreshStatus, 4000);
  };
  $("#ftSave").onclick = async () => {
    const labs = FT_LABELS.filter(([id]) => $("#" + id).checked).map((x) => x[1]);
    const pick = $("#ftThreshRow .seg.on");
    const pt = $("#ftPointRow .seg.on");
    const bt = ($("#ftBthr").value || "").trim();
    if (bt !== "" && !(Number(bt) >= 50 && Number(bt) <= 99.99)) {
      toast("自定义阈值请填 50 ~ 99.99（留空 = 跟随档位）", true); return;
    }
    const r = await save("/api/machine", {
      ft_video: $("#ftVideo").checked ? "1" : "0",
      ft_dm: $("#ftDm").checked ? "1" : "0",
      ft_cf: $("#ftCf").checked ? "1" : "0",
      ft_labels: labs.join(","),
      ft_thresh: (pick && pick.dataset.v) || "70",
      ft_point: (pt && pt.dataset.v) || "strict",
      ft_bthr: bt,
    }, "保存文本分析设置");
    if (r) { loadMachine(); restartFeed(); }
  };
  async function ftProbe() {
    const t = ($("#ftProbeIn").value || "").trim();
    const out = $("#ftProbeOut");
    if (!t) { out.textContent = "先输入一句话。"; return; }
    out.textContent = "判定中…";
    let r;
    try { r = await api("/api/textfilter/probe", { method: "POST", body: JSON.stringify({ text: t }) }); }
    catch (e) { out.textContent = "判定失败：" + e; return; }
    if (!r || !r.ok) { out.textContent = "没判出来：" + ((r && r.reason) || "未知原因"); return; }
    const pct = (v) => (Number(v || 0) * 100).toFixed(1) + "%";
    const verdict = r.hit ? " → 会拦"
      : (r.picked ? " → 不拦（低于阈值 " + pct(r.thresh) + "）" : " → 不拦（该标签没勾）");
    const top = (r.top || []).slice(0, 3).map((x) => x.name + " " + pct(x.score)).join(" · ");
    out.textContent = "判成「" + (r.name || r.label) + "」" + pct(r.score) + verdict +
      "；前三：" + top;
  }
  $("#ftProbeBtn").onclick = ftProbe;
  $("#ftProbeIn").addEventListener("keydown", (e) => { if (e.key === "Enter") ftProbe(); });
  async function ftModelCheck() {
    openModal("模型更新", "<p>正在查询模型发布…</p>", []);
    let d;
    try { d = await api("/api/model/check"); }
    catch (e) { openModal("模型更新", "<p>检查失败：" + esc(String(e)) + "</p>", [{ t: "知道了", fn: closeModal }]); return; }
    if (!d || d.ok === false) {
      openModal("模型更新", "<p>" + esc((d && (d.error || d.message)) || "检查失败") + "</p>",
        [{ t: "知道了", fn: closeModal }]);
      return;
    }
    if (!d.has_update) {
      openModal("模型更新",
        d.message ? "<p>" + esc(d.message) + "。</p>"
          : "<p>当前模型 <b>" + esc(d.current || "未安装") + "</b> 已是最新。</p>",
        [{ t: "知道了", fn: closeModal }]);
      return;
    }
    openModal("发现模型更新",
      "<p>" + esc(d.current || "未安装") + " → <b>" + esc(d.latest) + "</b>" +
      (d.size ? "（" + esc(updSize(d.size)) + "）" : "") + "</p>" +
      (d.notes ? '<div class="updNotes">' + esc(d.notes) + "</div>" : "") +
      '<p class="tip">只替换模型文件，设置与数据都保留。下载完成后自动加载。</p>',
      [{ t: "稍后", fn: closeModal }, { t: "下载并安装", fn: () => mdlDownload(d) }]);
  }
  $("#ftModelCheck").onclick = ftModelCheck;

  /* 黑白名单 */
  async function loadBlacklist() {
    try {
      const d = await api("/api/blacklist");
      const kw = d.keywords || [], au = d.authors || d.black_author || [];
      $("#kwList").innerHTML = kw.map((k) =>
        '<span class="chip">' + esc(k) + '<b data-kind="keyword" data-v="' + esc(k) + '">' + ic("x", "xs") + '</b></span>').join("");
      $("#auList").innerHTML = au.map((a) =>
        '<span class="chip">' + esc(a) + '<b data-kind="author" data-v="' + esc(a) + '">' + ic("x", "xs") + '</b></span>').join("");
    } catch (e) { toast("读取黑白名单失败：" + e, true); }
  }
  async function addBl(kind, value) {
    if (!value) return;
    if (!(await save("/api/blacklist", { kind: kind, value: value }, "添加"))) return;
    loadBlacklist();
  }
  $("#kwAdd").onclick = () => { const v = $("#kwInput").value.trim(); $("#kwInput").value = ""; addBl("keyword", v); };
  $("#auAdd").onclick = () => { const v = $("#auInput").value.trim(); $("#auInput").value = ""; addBl("author", v); };
  $("#kwInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#kwAdd").click(); });
  $("#auInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#auAdd").click(); });
  $("#svDetail").addEventListener("click", async (e) => {
    const b = e.target.closest(".chip b");
    if (!b) return;
    if (!(await save("/api/blacklist", { kind: b.dataset.kind, value: b.dataset.v, remove: true }, "移除"))) return;
    loadBlacklist();
  });
  /* ---------- 设置导入 / 导出 ---------- */
  let ioParsed = null;
  function ioPrefsSnapshot() {
    const v = localStorage.getItem(PREF_KEY);
    return v === null ? {} : { [PREF_KEY]: v };
  }
  $("#ioExportGen").onclick = async () => {
    const r = await save("/api/settings/export", { credentials: $("#ioExpCred").checked }, "导出设置");
    if (!r || !r.payload) return;
    const p = r.payload;
    p.io_prefs = ioPrefsSnapshot();          // 界面偏好也存在本机，一并带走
    $("#ioExpText").value = JSON.stringify(p);
    const bl = p.blacklist || {};
    $("#ioExpHint").textContent = "已生成 " + Object.keys(p.settings || {}).length + " 项设置 · 名单 " +
      (((bl.authors || []).length) + ((bl.keywords || []).length)) + " 条";
  };
  $("#ioExpCopy").onclick = async () => {
    const t = $("#ioExpText").value;
    if (!t) { toast("请先生成导出内容", true); return; }
    try { await navigator.clipboard.writeText(t); $("#ioExpHint").textContent = "已复制到剪贴板"; }
    catch (e) { $("#ioExpText").select(); $("#ioExpHint").textContent = "浏览器不给复制，已帮你选中，按 Ctrl+C"; }
  };
  $("#ioExpDownload").onclick = async () => {
    const t = $("#ioExpText").value;
    if (!t) { toast("请先生成导出内容", true); return; }
    // WebView2 壳里 <a download> 不触发(壳没接 DownloadStarting), 走原生「另存为」;
    // 纯浏览器 / 桥未就绪时才用 Blob 下载兜底。
    const nat = window.pywebview && window.pywebview.api;
    if (nat && typeof nat.save_text === "function") {
      try {
        const r = String(await nat.save_text("mirrorsorter-settings.json", t) || "");
        if (r === "cancel") return;
        if (r.slice(0, 3) === "ok:") {
          $("#ioExpHint").textContent = "已保存到：" + r.slice(3);
          return;
        }
        toast("保存失败：" + r.replace(/^err:/, ""), true);
        return;
      } catch (e) { /* 桥异常 → 落到 Blob 兜底 */ }
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([t], { type: "application/json" }));
    a.download = "mirrorsorter-settings.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  };
  function ioExtractJson(text) {
    const t = String(text || "").trim();
    if (!t) throw new Error("内容为空");
    const i = t.indexOf("{"), j = t.lastIndexOf("}");
    if (i < 0 || j <= i) throw new Error("没找到配置内容");
    return JSON.parse(t.slice(i, j + 1));
  }
  // 导入按类别勾选：解析出内容后先列出有哪些类别、各几项，用户自己决定导哪几类。
  // 类别表按后端可导入白名单写全 —— 不在表里的键归到「其他设置」，别让它们悄悄消失。
  const IO_CATS = [
    ["AI 配置", ["ai_base", "ai_key", "ai_model", "ai_enabled", "ai_full_scan",
                 "ai_interest_min", "ai_batch", "ai_cache_hours", "price_in", "price_out"]],
    ["筛选规则", ["m_min_views", "m_max_views", "m_max_duration", "m_min_duration",
                  "m_max_age_months", "m_seen_hours", "m_repeat_limit",
                  "m_repeat_cooldown_hours", "m_dedup", "m_blocklist_sync", "m_emotion",
                  "m_news_fresh", "ser_max_age_months", "m_sources", "tb_allow_help",
                  "tb_bars", "xhh_comm", "xhh_topic", "m_play_related", "m_cold_start_n"]],
    ["评论 / 弹幕筛选", ["cf_blk_on", "cf_min_level", "m_block_mystery_bio", "m_block_lottery",
                         "m_block_empty_space", "m_dm_on", "m_dm_multicand", "m_filter_speed"]],
    ["快速文本分析", ["ft_on", "ft_video", "ft_dm", "ft_cf", "ft_thresh", "ft_labels",
                       "ft_point", "ft_bthr"]],
    ["平台权重", ["bili_weight", "tb_weight", "xhh_weight"]],
    ["B站关注强调", ["bili_follow_emph", "bili_follow_months"]],
    ["偏好备注", ["prefs_note", "dislike_note"]],
    ["基础", ["mode"]],
  ];
  const IO_CRED_CAT = "账号与密钥";
  const IO_CRED_KEYS = ["ai_key", "bili_cookie", "tb_cookie", "tb_user", "tb_avatar", "xhh_cookie",
                        "xhh_heybox_id", "xhh_pkey", "xhh_user", "xhh_avatar",
                        "acct_nickname", "acct_profile", "bili_follow_mids", "bili_follow_names"];
  const IO_OTHER_CAT = "其他设置";
  function ioCatOf(key) {
    for (let i = 0; i < IO_CATS.length; i++) if (IO_CATS[i][1].indexOf(key) >= 0) return IO_CATS[i][0];
    return IO_CRED_KEYS.indexOf(key) >= 0 ? IO_CRED_CAT : IO_OTHER_CAT;
  }
  function ioRenderPreview(p) {
    const box = $("#ioImpPreview");
    const keys = Object.keys(p.settings || {});
    const order = IO_CATS.map((c) => c[0]).concat([IO_CRED_CAT, IO_OTHER_CAT]);
    const counts = {};
    keys.forEach((k) => { const c = ioCatOf(k); counts[c] = (counts[c] || 0) + 1; });
    const bl = p.blacklist || {};
    const blN = ((bl.authors || []).length) + ((bl.keywords || []).length);
    let h = '<label class="lbl">解析成功，勾选要导入的部分：</label>';
    order.forEach((name) => {
      if (!counts[name]) return;
      const warn = name === IO_CRED_CAT
        ? ' <span class="tip-inline">含 aiKey / 登录 Cookie，导入后免扫码直接登录</span>' : "";
      h += '<label class="mCheck"><input type="checkbox" data-cat="' + esc(name) + '" checked>' +
           "<span>" + esc(name) + "（" + counts[name] + " 项）" + warn + "</span></label>";
    });
    if (blN) h += '<label class="mCheck"><input type="checkbox" data-cat="黑白名单" checked>' +
                  "<span>黑白名单（" + blN + " 条）</span></label>";
    if (p.sanitized) h += '<p class="tip">此文件已脱敏（导出时没勾「包含平台登录凭证」），不含 aiKey 与登录账号。</p>';
    box.innerHTML = h;
    box.style.display = "";
  }
  function ioRunParse(text) {
    const el = $("#ioImpConfirm");
    const box = $("#ioImpPreview");
    try {
      const p = ioExtractJson(text);
      if (!p || typeof p.settings !== "object") throw new Error("这不是本软件的导出内容");
      ioParsed = p;
      ioRenderPreview(p);
      $("#ioImpHint").textContent = "导出时间 " +
        (p.ts ? new Date(p.ts * 1000).toLocaleString() : "未知") + "（点「确认导入」生效）";
      el.disabled = false;
    } catch (e) {
      ioParsed = null;
      box.style.display = "none";
      box.innerHTML = "";
      $("#ioImpHint").textContent = "解析失败：" + e.message;
      el.disabled = true;
    }
  }
  $("#ioImpParse").onclick = () => ioRunParse($("#ioImpText").value);
  $("#ioImpClip").onclick = async () => {
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch (e) {
      $("#ioImpHint").textContent = "浏览器不给读剪贴板，请手动粘贴到上面的输入框";
      return;
    }
    if (!text || !text.trim()) { $("#ioImpHint").textContent = "剪贴板是空的"; return; }
    $("#ioImpText").value = text;
    ioRunParse(text);
  };
  $("#ioImpFile").onclick = () => $("#ioImpFileInput").click();
  $("#ioImpFileInput").addEventListener("change", () => {
    const f = $("#ioImpFileInput").files && $("#ioImpFileInput").files[0];
    if (!f) return;
    const fr = new FileReader();
    fr.onload = () => { $("#ioImpText").value = String(fr.result || ""); ioRunParse($("#ioImpText").value); $("#ioImpHint").textContent += "（已读取文件 " + f.name + "）"; };
    fr.onerror = () => toast("文件读取失败", true);
    fr.readAsText(f, "utf-8");
  });
  $("#ioImpConfirm").onclick = async () => {
    if (!ioParsed) { toast("请先解析", true); return; }
    // 只送勾上的类别；没勾的键原样留在文件里，本机设置不受影响
    const picked = new Set(Array.prototype.slice.call($("#ioImpPreview").querySelectorAll("input:checked"))
      .map((c) => c.dataset.cat));
    const settings = {};
    Object.keys(ioParsed.settings || {}).forEach((k) => {
      if (picked.has(ioCatOf(k))) settings[k] = ioParsed.settings[k];
    });
    const bl = ioParsed.blacklist || {};
    const blacklist = picked.has("黑白名单") ? bl : {};
    if (!Object.keys(settings).length && !Object.keys(blacklist).length) { toast("什么都没勾", true); return; }
    if (!confirm("导入会覆盖本机对应的设置项（不影响已收录的内容池）。继续？")) return;
    const r = await save("/api/settings/import", { settings: settings, blacklist: blacklist }, "导入设置");
    if (!r) return;
    const prefs0 = (ioParsed.io_prefs || {})[PREF_KEY];
    if (prefs0) { try { localStorage.setItem(PREF_KEY, prefs0); } catch (e) {} }
    $("#ioImpHint").textContent = "已导入 " + ((r.imported || []).length) + " 项" +
      ((r.skipped || []).length ? "，跳过 " + r.skipped.length + " 项" : "");
    loadSettings(); loadMachine(); loadTiebaSettings(); loadLiveFilter(); loadBlacklist(); loadStatus();
    if (prefs0) location.reload();   // 界面偏好要重载才生效
  };

  $("#btnClearData").onclick = async () => {
    if (!confirm("清空历史 / 收藏 / 聊天记录？不可恢复。\n\n不含已收录的内容池（那个要用信息流顶栏的「清空信息池」）。")) return;
    if (!(await save("/api/reset", {}, "清空数据"))) return;
    loadFeed(true);
  };

  /* ================= 弹窗 / 登录 ================= */
  const DISCLAIMER =
    "<p>本软件仅用于个人学习、研究与技术交流，<b>严禁商用</b>。内容聚合自各平台公开接口，" +
    "版权归原平台及权利人所有；登录凭证与数据只存本机。软件按“现状”提供，不提供任何担保。</p>";
  const db = $("#disclaimerBody"); if (db) db.innerHTML = DISCLAIMER;
  $("#btnDisclaimer").onclick = async () => {
    await save("/api/disclaimer/ack", { version: "2026-09-03" }, "确认免责声明");
  };

  let modalLocked = false;   // 更新下载/安装期间锁住: 点遮罩、×、Esc 都不能收起来
  function setModalLock(on) {
    modalLocked = !!on;
    const x = $("#mClose");
    if (x) x.style.display = modalLocked ? "none" : "";
  }
  function openModal(title, html, buttons, opts) {
    $("#modal").innerHTML =
      '<div class="mhead"><span>' + esc(title) + '</span><button class="x" id="mClose" aria-label="关闭">' + ic("x") + '</button></div>' +
      '<div class="mbody">' + html + "</div>" +
      '<div class="mfoot">' + (buttons || []).map((b, i) =>
        '<button data-i="' + i + '">' + esc(b.t) + "</button>").join("") + "</div>";
    $("#mask").classList.add("show");
    setModalLock(opts && opts.locked);
    $("#mClose").onclick = closeModal;
    (buttons || []).forEach((b, i) => {
      const el = document.querySelector('#modal [data-i="' + i + '"]');
      if (el) el.onclick = b.fn;
    });
  }
  // 图片地址可能是字符串，也可能是 {url} 结构（小黑盒评论里就是后者）
  function imgUrl(x) { return typeof x === "string" ? x : ((x && x.url) || ""); }
  function rdImgs(list, cls) {
    return (list || []).map((x) => {
      const u = imgUrl(x);
      return u ? '<img class="' + cls + '" loading="lazy" referrerpolicy="no-referrer" src="' + esc(u) +
        '" alt="" onerror="this.remove()">' : "";
    }).join("");
  }
  async function openReader(opt) {
    const title = opt.tid ? "贴吧 · 站内阅读" : "小黑盒 · 站内阅读";
    openModal(title, '<div class="lbl">正在加载…</div>', []);
    $("#modal").classList.add("wide");
    let d;
    try {
      d = await api(opt.tid ? "/api/tieba/thread?tid=" + encodeURIComponent(opt.tid)
                            : "/api/xhh/thread?link_id=" + encodeURIComponent(opt.lid));
    } catch (e) {
      $("#modal .mbody").innerHTML = '<div class="lbl">加载失败：' + esc(String(e)) + "</div>";
      return;
    }
    if (!d || !d.ok) {
      $("#modal .mbody").innerHTML = '<div class="lbl">' + esc((d && d.error) || "加载失败") + "</div>";
      return;
    }
    let h = '<div class="rdTitle">' + esc(d.title || "") + "</div>";
    if (d.floors) {                       // 贴吧：楼层
      d.floors.forEach((f) => {
        h += '<div class="rdFloor"><div class="rdHead"><b>' + esc(f.author || "匿名") + "</b>" +
          (f.lz ? '<em class="rdLz">楼主</em>' : "") +
          '<span class="lbl">' + esc(f.time || "") + (f.floor ? " · " + esc(String(f.floor)) + " 楼" : "") + "</span></div>" +
          (f.text ? '<div class="rdText">' + esc(f.text) + "</div>" : "") +
          rdImgs(f.images, "rdImg") + "</div>";
      });
    } else {                              // 小黑盒：正文 + 图 + 评论
      if (d.content) h += '<div class="rdText">' + esc(d.content) + "</div>";
      h += rdImgs(d.images, "rdImg");
      const cs = d.comments || [];
      if (cs.length) {
        h += '<div class="rdSub">评论 ' + (d.comment_total || cs.length) + "</div>";
        cs.forEach((c) => {
          h += '<div class="rdFloor"><div class="rdHead"><b>' + esc(c.author || "匿名") + "</b>" +
            (c.create_at ? '<span class="lbl">' + ago(c.create_at) + "</span>" : "") + "</div>" +
            (c.text ? '<div class="rdText">' + esc(c.text) + "</div>" : "") +
            rdImgs(c.images, "rdImg") + "</div>";
        });
      }
      if (d.author) h = '<div class="lbl">' + esc(d.author) + (d.create_at ? " · " + ago(d.create_at) : "") + "</div>" + h;
    }
    $("#modal .mbody").innerHTML = h + '<p class="tip">只含首批内容；看全帖点下面的「打开原文」。</p>';
  }
  // 关闭时必须把内容真的拆掉，不能只是藏起来：站内播放的 iframe 留在 DOM 里会继续出声，
  // 用户看到的就是"点了 × 还在后台播"。移除节点同时也会释放它的解码资源。
  function closeModal() {
    if (modalLocked) return;   // 更新进行中: 不许把进度窗口关掉(否则 updBusy 永远回不来)
    $("#mask").classList.remove("show");
    $("#modal").classList.remove("wide");
    $("#modal").innerHTML = "";
    clearInterval(qrTimer);
  }
  $("#mask").addEventListener("click", (e) => { if (e.target.id === "mask") closeModal(); });

  let qrTimer = null;
  // 扫码 / 粘 Cookie 都内联渲染到指定容器里（不弹窗）
  async function mountQr(box, kind) {
    if (!box) return;
    clearInterval(qrTimer);
    box.innerHTML = '<div class="lbl">正在生成二维码…</div>';
    const r = kind === "bili" ? await save("/api/login/qr", {}, "生成二维码") : await api("/api/xhh/qr");
    if (!r.ok) { box.innerHTML = '<div class="lbl">生成失败：' + esc(r.error || "") + "</div>"; return; }
    box.innerHTML = "";
    const qr = document.createElement("div");
    box.appendChild(qr);
    new QRCode(qr, { text: r.url, width: 180, height: 180, correctLevel: QRCode.CorrectLevel.M });
    const st = document.createElement("div");
    st.className = "lbl";
    st.textContent = kind === "bili" ? "用手机 B站 App「扫一扫」登录" : "用手机「小黑盒 App」扫码登录";
    box.appendChild(st);
    qrTimer = setInterval(async () => {
      const p = kind === "bili"
        ? await api("/api/login/qr/poll?key=" + encodeURIComponent(r.key))
        : await api("/api/xhh/qr/poll");
      if (kind === "xhh" && p.message) st.textContent = p.message;
      if (p.status === "ok") {
        clearInterval(qrTimer);
        st.textContent = "登录成功" + (p.nickname ? "：@" + p.nickname : "");
        toast("登录成功" + (p.nickname ? "：@" + p.nickname : ""), true);
        loadMessages(); loadAccount(); loadStatus();
      } else if (p.status === "expired" || p.status === "error") {
        clearInterval(qrTimer);
        st.textContent = p.message || "二维码已失效";
      }
    }, 2000);
  }
  function mountTiebaCookie(box) {
    if (!box) return;
    clearInterval(qrTimer);
    box.innerHTML =
      '<p class="tip">登录 tieba.baidu.com → F12 → Application → Cookies，复制 BDUSS / STOKEN 等（或从请求头整段复制 Cookie）。</p>' +
      '<textarea id="tbCookie" rows="3" style="width:100%" placeholder="BDUSS=...; STOKEN=..."></textarea>' +
      '<div class="row" style="margin-top:6px"><button id="tbCookieSave">保存并登录</button></div>';
    $("#tbCookieSave").onclick = async () => {
      const cookie = $("#tbCookie").value.trim();
      if (!cookie) { toast("请先粘贴 Cookie", true); return; }
      const r = await save("/api/tieba/login/manual", { cookie: cookie }, "贴吧登录");
      if (r.ok) { box.innerHTML = '<div class="lbl">贴吧登录成功</div>'; loadMessages(); loadAccount(); loadStatus(); }
      else toast("登录失败：" + (r.error || "Cookie 无效"), true);
    };
  }
  // 头像统一走本地代理（图床按 Referer 防盗链，直连会被拒）
  function setAva(sel, url, initial) {
    const el = $(sel);
    if (!el) return;
    const img = new Image();
    img.alt = "";
    img.src = url;
    img.onerror = () => { el.textContent = initial || "?"; };
    el.textContent = "";
    el.appendChild(img);
  }
  $("#accSync").onclick = async () => {
    const r = await save("/api/account/sync", {}, "同步账号数据");
    if (!r) return;
    loadAccount(); loadMessages(); loadStatus();   // 同步的内容会体现在头像/昵称/消息上
  };
  $("#accBiliQr").onclick = () => mountQr($("#accQrBox"), "bili");
  $("#accXhhQr").onclick = () => mountQr($("#accQrBox"), "xhh");
  $("#accTbCookie").onclick = () => mountTiebaCookie($("#accQrBox"));
  // 贴吧没有官方二维码接口，走「本机浏览器登录 → 服务端抓 Cookie」：
  // 有 Chrome/Edge 时弹一个窗口，用户登完轮询拿凭证；没有浏览器时后端回 manual=true，
  // 那就退回手动粘 Cookie（和旧版同一套行为）。
  let tbTimer = null;
  function tbPollStop() { if (tbTimer) { clearInterval(tbTimer); tbTimer = null; } }
  const tbState = (t) => { const e = $("#tbLoginState"); if (e) e.textContent = t; };
  $("#accTbQr").onclick = async () => {
    tbPollStop();
    $("#tbLoginRow").style.display = "";
    tbState("正在启动登录窗口…");
    const r = await api("/api/tieba/login", { method: "POST", body: "{}" });
    if (!r || !r.ok) { tbState("启动失败：" + ((r && r.error) || "未知原因")); return; }
    if (r.manual) {
      tbState("已用默认浏览器打开登录页；登完请用「手动粘 Cookie」保存");
      mountTiebaCookie($("#accQrBox"));
      return;
    }
    tbState("浏览器窗口已弹出，请在窗口中完成登录…");
    tbTimer = setInterval(async () => {
      let p;
      try { p = await api("/api/tieba/login/poll"); } catch (e) { return; }
      tbState(p.message || p.status || "等待中…");
      if (p.status === "ok") {
        tbPollStop();
        tbState("登录成功，已保存凭证");
        toast("贴吧登录成功", true);
        loadMessages(); loadAccount(); loadStatus(); restartFeed();
      } else if (p.status === "expired" || p.status === "error" || p.status === "closed") {
        tbPollStop();
        if (p.status !== "closed") toast(p.status === "expired" ? "登录超时，请重试" : (p.message || "登录失败"), true);
      }
    }, 2500);
  };
  $("#accTbLoginStop").onclick = async () => {
    await api("/api/tieba/login/stop", { method: "POST", body: "{}" });
    tbPollStop();
    tbState("窗口已关闭");
  };
  $("#tbImport").onclick = async () => {
    const hint = $("#tbImportHint");
    hint.textContent = "拉取中…";
    const r = await api("/api/tieba/mylike");
    if (!r || !r.ok) { hint.textContent = ""; toast("导入失败：" + ((r && r.error) || ""), true); return; }
    const cur = ($("#tbBars").value || "").split(/[\n,，、;；]+/).map((x) => x.trim()).filter(Boolean);
    const merged = Array.from(new Set(cur.concat(r.bars || [])));
    $("#tbBars").value = merged.join("\n");
    hint.textContent = "已导入 " + ((r.bars || []).length) + " 个吧";
    if (!(await save("/api/tieba", { bars: merged }, "保存吧列表"))) return;
    loadTiebaBars(); loadStatus(); restartFeed();
  };
  async function loadTiebaBars() {
    try {
      const d = await api("/api/tieba");
      const el = $("#tbBars");
      if (el) el.value = (d.bars || []).join("\n");
    } catch (e) {}
  }
  $("#tbBarsSave").onclick = async () => {
    const bars = $("#tbBars").value.split("\n").map((x) => x.trim()).filter(Boolean);
    if (!(await save("/api/tieba", { bars: bars }, "保存吧列表"))) return;
    loadTiebaBars(); loadStatus();
  };
  $("#accBiliOpen").onclick = () => openExternal("https://passport.bilibili.com/login", "B站登录");
  const logout = (path, label) => async () => {
    if (!confirm("退出" + label + "登录？")) return;
    await save(path, {}, "退出" + label);
    loadAccount(); loadMessages(); loadStatus();
  };
  $("#accOutBili").onclick = logout("/api/account/logout", "B站");
  $("#accOutTb").onclick = logout("/api/tieba/logout", "贴吧");
  $("#accOutXhh").onclick = logout("/api/xhh/logout", "小黑盒");

  /* 消息：渲染到中间的展示区（不弹窗） */
  // /api/messages 每次都要真去三个平台查未读数（实测 2.7–3.7 秒），所以：
  // 启动时就取一次缓存起来 → 进消息页用缓存**秒开** → 再后台刷新一次（有变化才重画）。
  let msgCache = null;
  // v0.3.1.66(用户要求): 点完「消息」按钮, 红点要立刻消失 —— 不能等下一轮 5 分钟刷新。
  // 做法: 服务端存一份"看过时的未读数"快照(msg_seen), 角标只画 unread - seen。
  // 纯本地语义, 不去打各平台的"标记已读"接口(没稳定契约, 还多喂一次风控)。
  const MSG_KEYS = ["bili", "tieba", "xhh"];
  let msgSeenSent = null;             // 上次上报成功的快照, 免得同一次浏览反复 POST
  function msgSeenMap(d) {
    const out = {};
    MSG_KEYS.forEach((k) => {
      const o = (d && d[k]) || {};
      out[k] = Math.max(0, Number(o.unread) || 0);
    });
    return out;
  }
  function paintMsgBadge(d) {
    const seen = (d && d.seen) || {};
    let n = 0;
    MSG_KEYS.forEach((k) => {
      const o = (d || {})[k] || {};
      const u = Math.max(0, Number(o.unread) || 0);
      n += Math.max(0, u - Math.max(0, Number(seen[k]) || 0));
    });
    const el = $("#navMsgBadge");
    if (el) el.textContent = n ? (n > 99 ? "99+" : n) : "";
  }
  function markMessagesRead() {
    const el = $("#navMsgBadge");
    if (el) el.textContent = "";              // 先清掉: 用户已经点开消息了
    if (!msgCache) return;                    // 还没拿到未读数 → 等本轮 loadMessages 回来再记
    const snap = msgSeenMap(msgCache);
    const key = MSG_KEYS.map((k) => snap[k]).join(",");
    if (key === msgSeenSent) return;          // 这次浏览已经报过同一份快照
    msgSeenSent = key;
    api("/api/messages/read", { method: "POST", body: JSON.stringify({ seen: snap }) })
      .then((r) => { if (r && r.ok) { msgCache.seen = r.seen || snap; } else { msgSeenSent = null; } })
      .catch(() => { msgSeenSent = null; });  // 失败不弹窗: 下次进消息页会再试
  }
  function renderMsgBody(body, d) {
    const line = (k, label) => {
      const o = d[k] || {};
      const state = o.logged_in ? (o.unread ? "未读 " + o.unread : "没有未读") : "未登录";
      return '<div class="srcRow"><b>' + label + '</b><span class="lbl">' + state + "</span>" +
        (o.url ? '<button class="alt" data-url="' + esc(o.url) + '">打开</button>' : "") + "</div>";
    };
    body.innerHTML = '<p class="tip">各平台消息状态（点「打开」去对应平台处理）。</p>' +
      line("bili", "B站") + line("tieba", "贴吧") + line("xhh", "小黑盒");
    body.querySelectorAll("button[data-url]").forEach((b) => (b.onclick = () => openExternal(b.dataset.url, "消息")));
  }
  async function renderMsgPane() {
    const body = $("#msgBody");
    if (!body) return;
    if (msgCache) renderMsgBody(body, msgCache);      // 秒开
    else body.innerHTML = '<div class="lbl">读取中…</div>';
    try {
      const d = await api("/api/messages");            // 后台刷新，不阻塞显示
      msgCache = d;
      if (S.view === "msg") renderMsgBody(body, d);
    } catch (e) {
      if (!msgCache) body.innerHTML = '<div class="lbl">读取失败：' + esc(String(e)) + "</div>";
    }
  }
  // 左下角头像 → 设置里的「平台账号」（个人与设置合并，不再各有一套）
  $("#btnMe").onclick = () => { setView("settings"); showSec("sources"); };
  async function loadMessages() {
    try {
      const d = await api("/api/messages");
      msgCache = d;                 // 缓存给消息页秒开（未读数角标也用它）
      paintMsgBadge(d);             // 只画"快照之后新增的"未读
      // 消息页正开着 → 用户就看着这一页, 顺手把新到的未读也记成已看(角标保持干净)
      if (S.view === "msg") markMessagesRead();
    } catch (e) {}
  }

  /* ================= 封面悬停预览（停一下就地静音试播）================= */
  // 契约没变：GET /api/preview?bvid= 直接给代理路径 /api/video-proxy?...&cid=...，
  // 前端塞进一个 <video> 就行（cid 由后端缓存 1 小时，不用自己拼）。
  // 只做「就地播放」：不放大、不浮层、不改变布局。上一版被删掉的是 2× 放大预览浮层
  // （用户当时明确不要），这次要回来的是「封面动起来」这件事本身。
  const pvCache = new Map();               // bvid -> src | null（取不到也记住，别反复问）
  let pvTimer = null, pvVideo = null, pvToken = 0;
  const noHover = () => window.matchMedia("(hover:none)").matches;
  function stopPreview() {
    clearTimeout(pvTimer);
    pvTimer = null;
    pvToken++;                             // 作废在途的 /api/preview 回调
    if (pvVideo) {
      try { pvVideo.pause(); } catch (e) {}
      if (pvVideo.parentNode) pvVideo.parentNode.removeChild(pvVideo);
    }
    pvVideo = null;
  }
  function startPreview(thumb) {
    if (noHover() || !thumb) return;
    const card = thumb.closest(".card");
    const bvid = card && card.dataset.bvid;
    if (!bvid || !thumb.isConnected) return;          // 只有 B站 有预览流
    const token = ++pvToken;
    pvTimer = null;
    const mount = (src) => {
      if (!src || token !== pvToken || !thumb.isConnected) return;
      const v = document.createElement("video");
      v.className = "pvVid";
      v.muted = true; v.loop = true; v.playsInline = true; v.preload = "auto";
      v.setAttribute("aria-hidden", "true");
      // 能出画面了才淡入，否则会先闪一块黑
      v.addEventListener("canplay", () => {
        if (token !== pvToken) return;
        v.classList.add("on");
        v.play().catch(function () {});
      });
      v.addEventListener("error", stopPreview);
      v.src = src;
      thumb.appendChild(v);
      pvVideo = v;
    };
    const cached = pvCache.get(bvid);
    if (cached !== undefined) { mount(cached); return; }
    api("/api/preview?bvid=" + encodeURIComponent(bvid))
      .then((d) => { const src = d && d.ok ? d.src : null; pvCache.set(bvid, src); mount(src); })
      .catch(() => { pvCache.set(bvid, null); });
  }
  // 悬停 220ms 才播：鼠标扫过一排卡片时不会每张都发一次请求（上一版每开页面打上百次的教训）
  $("#list").addEventListener("mouseover", (e) => {
    const t = e.target.closest(".thumb");
    if (!t || noHover()) return;
    if (e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest(".thumb") === t) return;
    clearTimeout(pvTimer);
    pvTimer = setTimeout(() => startPreview(t), 220);
  });
  $("#list").addEventListener("mouseout", (e) => {
    const t = e.target.closest(".thumb");
    if (!t) return;
    if (e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest(".thumb") === t) return;
    stopPreview();
  });
  window.addEventListener("scroll", stopPreview, true);   // 一滚动就收，预览别跟着卡片飘
  window.addEventListener("resize", stopPreview);

  /* ================= AI 聊天 ================= */
  // 对话列表：切换 / 重命名 / 删除（旧版有，新版补上）
  async function loadConvs() {
    try {
      const d = await api("/api/conv");
      if (d.cur) S.cid = d.cur;
      const cur = (d.convs || []).find((c) => c.id === d.cur);
      $("#convTitle").textContent = cur && cur.title ? cur.title : "AI 聊天";
      $("#convList").innerHTML = (d.convs || []).map((c) =>
        '<div class="convItem ' + (c.id === d.cur ? "on" : "") + '" data-id="' + c.id + '">' +
          '<span class="ct">' + esc(c.title || "新对话") + "</span>" +
          '<b data-rn="' + c.id + '" title="重命名">' + ic("sliders", "xs") + "</b>" +
          '<b data-del="' + c.id + '" title="删除">' + ic("x", "xs") + "</b></div>").join("")
        || '<div class="convItem">还没有对话</div>';
    } catch (e) {}
  }
  $("#convBtn").onclick = (e) => {
    e.stopPropagation();
    const el = $("#convList");
    el.classList.toggle("show");
    if (el.classList.contains("show")) loadConvs();
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#railRight .sidehead") && !e.target.closest("#convList")) $("#convList").classList.remove("show");
  });
  $("#convList").addEventListener("click", async (e) => {
    const rn = e.target.closest("[data-rn]");
    const del = e.target.closest("[data-del]");
    if (rn) {
      const id = Number(rn.dataset.rn);
      const cur = (await api("/api/conv")).convs.find((c) => c.id === id);
      const name = prompt("重命名这个对话", (cur && cur.title) || "");
      if (name == null) return;
      if (await save("/api/conv/rename", { id: id, title: name.trim() }, "重命名")) loadConvs();
      return;
    }
    if (del) {
      if (!confirm("删除这个对话及其消息？不可恢复。")) return;
      if (!(await save("/api/conv/delete", { id: Number(del.dataset.del) }, "删除对话"))) return;
      await loadConvs(); await loadChatHistory();
      return;
    }
    const item = e.target.closest(".convItem[data-id]");
    if (!item) return;
    if (!(await save("/api/conv/select", { id: Number(item.dataset.id) }, "切换对话"))) return;
    $("#convList").classList.remove("show");
    await loadConvs(); await loadChatHistory();
  });
  function scrollMsgs(force) {
    const m = $("#msgs");
    if (!m) return;
    if (force || m.scrollHeight - m.scrollTop - m.clientHeight < 90) m.scrollTop = m.scrollHeight;
  }
  /* 轻量 Markdown: 先转义 HTML 再识别结构, 只放行 http(s) 链接。
     模型给的标题/列表/表格/代码块不渲染的话, 聊天里全是星号和竖线。 */
  function mdToHtml(src) {
    const e2 = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const codes = [];
    let s = String(src || "").replace(/\r\n?/g, "\n");
    if ((s.match(/^```/gm) || []).length % 2 === 1) s += "\n```";   // 流式未写完的围栏先补上
    s = s.replace(/```([^\n`]*)\n([\s\S]*?)```/g, (m, lang, code) => {
      codes.push('<pre class="mdCode"><code>' + e2(code.replace(/\n$/, "")) + "</code></pre>");
      return "\u0000C" + (codes.length - 1) + "\u0000";
    });
    const il = (t) => {
      t = e2(t);
      t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
      t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
      t = t.replace(/__([^_\n]+)__/g, "<b>$1</b>");
      t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
      t = t.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
      t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
      return t;
    };
    const lines = s.split("\n"), out = [];
    let list = null, para = [];
    const flush = () => { if (para.length) { out.push("<p>" + para.map(il).join("<br>") + "</p>"); para = []; } };
    const closeList = () => { if (list) { out.push("</" + list + ">"); list = null; } };
    const cells = (row) => row.replace(/^\s*\|/, "").replace(/\|\s*$/, "")
      .split("|").map((c) => c.trim());
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i], tr = ln.trim();
      if (/^\u0000C\d+\u0000$/.test(tr)) { flush(); closeList(); out.push(tr); continue; }
      if (!tr) { flush(); closeList(); continue; }
      const h = ln.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flush(); closeList();
        out.push("<h" + h[1].length + ">" + il(h[2]) + "</h" + h[1].length + ">"); continue; }
      if (/^\s*(---+|\*\*\*+|___+)\s*$/.test(ln)) { flush(); closeList(); out.push("<hr>"); continue; }
      if (/^\s*>/.test(ln)) { flush(); closeList();
        out.push("<blockquote>" + il(ln.replace(/^\s*>\s?/, "")) + "</blockquote>"); continue; }
      if (ln.indexOf("|") >= 0 && lines[i + 1] &&
          /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
        flush(); closeList();
        const head = cells(ln); i++;
        let tb = "<table><thead><tr>" + head.map((c) => "<th>" + il(c) + "</th>").join("") +
          "</tr></thead><tbody>";
        while (i + 1 < lines.length && lines[i + 1].indexOf("|") >= 0 && lines[i + 1].trim()) {
          i++;
          tb += "<tr>" + cells(lines[i]).map((c) => "<td>" + il(c) + "</td>").join("") + "</tr>";
        }
        out.push(tb + "</tbody></table>"); continue;
      }
      const ul = ln.match(/^\s*[-*+]\s+(.*)$/), ol = ln.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flush();
        const want = ul ? "ul" : "ol";
        if (list !== want) { closeList(); list = want; out.push("<" + want + ">"); }
        out.push("<li>" + il((ul || ol)[1]) + "</li>"); continue;
      }
      closeList(); para.push(ln);
    }
    flush(); closeList();
    return out.join("").replace(/\u0000C(\d+)\u0000/g, (m, n) => codes[+n] || "");
  }

  /* 思维链: 思考时展开流动, 正文开始时自动收起(DeepSeek 网页式), 点标题可再看。 */
  function addThinkBox(aiEl) {
    const box = document.createElement("div");
    box.className = "thinkBox show";
    box.innerHTML = '<button type="button" class="thinkHead">' +
      '<span class="thinkDot"></span><span class="thinkTitle">正在深度思考…</span>' +
      '<svg class="ic thinkChev"><use href="#i-chevron-right"/></svg></button>' +
      '<div class="thinkBody"></div>';
    aiEl.insertBefore(box, aiEl.firstChild);
    box.querySelector(".thinkHead").onclick = () => box.classList.toggle("open");
    return {
      _t: "", _last: 0, _done: false, _start: performance.now(),
      box, body: box.querySelector(".thinkBody"), title: box.querySelector(".thinkTitle"),
      add(t) {
        this._t += t || "";
        const now = performance.now();
        if (now - this._last < 100) return;
        this._last = now;
        this.body.textContent = this._t;
        this.body.scrollTop = this.body.scrollHeight;
        scrollMsgs();
      },
      finish() {
        if (this._done) return;
        this._done = true;
        this.body.textContent = this._t;
        const secs = Math.max(1, Math.round((performance.now() - this._start) / 1000));
        this.title.textContent = "已深度思考（" + secs + " 秒）";
        box.classList.remove("show", "open");
      },
    };
  }

  function addMsg(who, text, chips, raw) {
    const d = document.createElement("div");
    d.className = "msg " + (who === "me" ? "me" : "ai");
    const t = document.createElement("div");
    t.className = "msg-text";
    if (who === "me" || raw) t.textContent = text == null ? "" : String(text);
    else { t.classList.add("md"); t.innerHTML = mdToHtml(text); }
    d.appendChild(t);
    if (chips && chips.length) d.appendChild(chipsRow(chips));
    $("#msgs").appendChild(d);
    scrollMsgs(true);
    return d;
  }
  // 智能体这次真正做过的操作(可折叠成一行小标签), 让人一眼看到"它确实动了手"
  function actsRow(acts) {
    const row = document.createElement("div");
    row.className = "acts-row";
    acts.forEach((a) => {
      const sp = document.createElement("span");
      sp.className = "actTag" + (a.ok === false ? " bad" : "");
      sp.textContent = (a.ok === false ? "✕ " : "✓ ") + (a.label || "");
      if (a.tool) sp.title = a.tool;
      row.appendChild(sp);
    });
    return row;
  }
  // 后端每条回复都会带一组快捷指令（chips），旧版就渲染在气泡下方；点了等于把 cmd 当新消息发出去
  function chipsRow(chips) {
    const row = document.createElement("div");
    row.className = "chips-row";
    chips.forEach((c) => {
      const b = document.createElement("button");
      b.textContent = c.label || c.cmd || "";
      if (c.cmd) b.title = c.cmd;
      b.onclick = () => sendChatStream(c.cmd || c.label || "", c.payload);
      row.appendChild(b);
    });
    return row;
  }
  // 打开页面/切会话时把服务端已有的对话读回来（原来从不 GET /api/chat，刷新后右侧永远空白）
  async function loadChatHistory() {
    try {
      const d = await api("/api/chat");
      if (d.cid) S.cid = d.cid;
      const box = $("#msgs");
      box.innerHTML = "";
      const msgs = d.messages || [];
      if (!msgs.length) box.innerHTML = '<div class="msg ai">告诉 AI 你想看什么样的信息</div>';
      // 没有历史 + 没配 AI：别退回一句发不出话的引导语。
      // （loadStatus 与这里谁先跑不确定，两边都要兜住）
      if (S.aiOn === false) paintChatHint(true);
      else msgs.forEach((m) => addMsg(m.role === "user" ? "me" : "ai", m.content));
      loadChatCtx();
    } catch (e) {}
  }
  async function loadChatCtx() {
    try {
      const d = await api("/api/conv");
      const c = d.ctx || {};
      S.ctx = c.key ? { key: c.key, title: c.title } : null;
      $("#ctxBar").classList.toggle("show", !!S.ctx);
      if (S.ctx) $("#ctxTitle").textContent = "正在针对：《" + S.ctx.title + "》";
    } catch (e) {}
  }
  let chatCtrl = null;    // 流式请求的 AbortController（停止生成用）
  /* ================= 聊天里直接改设置 =================
     用户: "告诉 AI 想改什么设置, 它就帮忙改" —— 所以这句指令必须当场生效, 不能只是
     回答"你去设置页改 xxx"。这里用本地规则解析, 不走模型: 不用等一次往返, 没有 AI 额度
     也照样能用; 解析不出来就返回 null, 交回原来的聊天流程(命令引擎/模型)。
     只管"设置", 不碰抓取逻辑与数据结构; 每改一项都回执, 再说一次就能改回来。 */
  const NL_COLORS = [[/粉/, "#fb7299"], [/蓝绿|青/, "#22c3c3"], [/蓝/, "#4a9eff"], [/绿/, "#67c23a"],
                     [/紫/, "#a06bff"], [/橙/, "#ff9f43"], [/灰/, "#8a94a6"], [/黄/, "#e8c341"]];
  const NL_PLAT = { "B站直播": "B站直播", "直播": "B站直播", "B站": "B站", "贴吧": "贴吧", "小黑盒": "小黑盒" };
  async function nlApplySettings(raw) {
    const t = String(raw || "").replace(/\s+/g, "");
    if (!t) return null;
    const done = [];
    const setPref = (k, v, label) => {
      if (String(prefs[k]) === String(v)) { done.push(label + "（本来就是这样）"); return; }
      prefs[k] = v; done.push(label);
    };
    // 主题 / 布局 / 封面 / 字号 / 推荐理由 —— 都只是界面偏好, 存 localStorage
    if (/白天|日间|浅色/.test(t)) setPref("theme", "light", "界面模式 → 白天");
    else if (/黑夜|夜间|深色|暗色/.test(t)) setPref("theme", "dark", "界面模式 → 黑夜");
    if (/大图/.test(t)) setPref("layout", "big", "信息流 → 大图模式");
    else if (/双栏|两栏|两列/.test(t)) setPref("layout", "dual", "信息流 → 双栏");
    else if (/单栏|一栏|单列/.test(t)) setPref("layout", "single", "信息流 → 单栏");
    if (/封面/.test(t)) {
      if (/特大/.test(t)) setPref("cover", "xl", "封面大小 → 特大");
      else if (/大/.test(t)) setPref("cover", "l", "封面大小 → 大");
      else if (/中/.test(t)) setPref("cover", "m", "封面大小 → 中");
      else if (/小/.test(t)) setPref("cover", "s", "封面大小 → 小");
    }
    if (/字号|字体/.test(t)) {
      if (/大/.test(t)) setPref("font", "large", "字体 → 大");
      else if (/小/.test(t)) setPref("font", "small", "字体 → 小");
      else if (/中/.test(t)) setPref("font", "medium", "字体 → 中");
    }
    if (/推荐理由/.test(t)) {
      if (/显示|打开|开|要/.test(t)) setPref("reason", "show", "推荐理由 → 显示");
      else if (/隐藏|关|不要|别/.test(t)) setPref("reason", "hide", "推荐理由 → 隐藏");
    }
    if (/来源/.test(t) && /强调色|统一/.test(t)) {
      const off = /关闭|关掉|不要|不用|取消|否/.test(t);
      setPref("srcacc", off ? "0" : "1", "来源标识 → " + (off ? "各用品牌色" : "跟随强调色"));
    }
    if (/强调色|主色|主题色/.test(t)) {
      const hit = NL_COLORS.find((c) => c[0].test(t));
      if (hit) setPref("accent", hit[1], "强调色 → " + hit[1]);
    }
    // 平台占比: "B站直播占比 25%" / "贴吧 10%"
    let weightSaved = false;
    const wm = t.match(/(B站直播|B站|贴吧|小黑盒|直播)[^0-9%]{0,6}(\d{1,3})%/);
    if (wm && PLAT_KEY[NL_PLAT[wm[1]]]) {
      const name = NL_PLAT[wm[1]];
      const v = snapShare(Math.max(0, Math.min(100, parseInt(wm[2], 10))));
      if (platWs[name] !== v) {
        platWs[name] = v;
        renderPlatWeights(); renderRailShares();
        done.push(name + " 占比 → " + v + "%");
        weightSaved = true;
      }
    }
    // 直播筛选: "直播只看关注" / "观众少于 1000 不显示"
    let liveSaved = false;
    if (/直播/.test(t) || /观众|人气/.test(t)) {
      const minEl = $("#mLiveMin"), folEl = $("#mLiveFollowOnly");
      let mn = minEl ? (parseInt(minEl.value, 10) || 0) : 0;
      let fol = !!(folEl && folEl.checked);
      const vm = t.match(/(?:观众|人气)[^0-9]{0,6}(\d{1,6})/);
      if (vm) mn = parseInt(vm[1], 10) || 0;
      if (/只看关注|只要关注|仅关注/.test(t) && !/取消|不要|不用|别/.test(t)) fol = true;
      else if (/不限|全部直播|所有直播/.test(t) || (/关注/.test(t) && /取消|不要|不用|别|关掉/.test(t))) fol = false;
      if (vm || fol !== (!!(folEl && folEl.checked))) {
        const r = await save("/api/live/settings", { min_viewers: mn, only_followed: !!fol }, "保存直播筛选");
        if (r) {
          if (minEl) minEl.value = String(mn);
          if (folEl) folEl.checked = fol;
          done.push("直播筛选 → " + (mn > 0 ? "人气 ≥ " + mn : "不限人数") + (fol ? " · 只看关注" : ""));
          liveSaved = true;
        }
      }
    }
    if (!done.length) return null;
    savePrefs(); applyPrefs();
    if (weightSaved) {
      const body = {};
      Object.keys(platWs).forEach((n) => { if (PLAT_KEY[n]) body[PLAT_KEY[n]] = String(platWs[n]); });
      if (await save("/api/tieba", body, "保存平台比例")) restartFeed();
      else { done.push("（占比保存失败, 已按服务器上的值还原）"); loadTiebaSettings(); }
    }
    if (liveSaved) restartFeed();
    return { reply: "已改好：" + done.join("；") + "。想改回来直接说一声就行。" };
  }

  // ===== 模型先想再改（v0.3.1.62）=====
  // 上一版纯本地正则: "比例调高一点""别用大图了"这类说法匹配不上或者匹错。
  // 现在先请模型理解成结构化改动(带思考强度), 模型不可用/没读懂再退回本地规则。
  const PLAN_LBL = {
    theme: { dark: "界面模式 → 黑夜", light: "界面模式 → 白天" },
    layout: { single: "信息流 → 单栏", dual: "信息流 → 双栏", big: "信息流 → 大图模式" },
    cover: { s: "封面大小 → 小", m: "封面大小 → 中", l: "封面大小 → 大", xl: "封面大小 → 特大" },
    font: { small: "字体 → 小", medium: "字体 → 中", large: "字体 → 大" },
    reason: { hide: "推荐理由 → 隐藏", show: "推荐理由 → 显示" },
    srcacc: { "0": "来源标识 → 各用品牌色", "1": "来源标识 → 跟随强调色" },
  };
  async function applyPlan(actions) {
    const done = [];
    let prefSaved = false, weightSaved = false, liveSaved = false;
    let liveMn = null, liveFol = null;
    const wKey = { bili_weight: "B站", tb_weight: "贴吧", xhh_weight: "小黑盒", live_weight: "B站直播" };
    for (const a of actions || []) {
      const k = String(a.k || ""), v = String(a.v);
      if (PLAN_LBL[k] && PLAN_LBL[k][v]) {
        if (String(prefs[k]) === v) { done.push(PLAN_LBL[k][v] + "（本来就是这样）"); continue; }
        prefs[k] = v; prefSaved = true; done.push(PLAN_LBL[k][v]);
      } else if (k === "accent" && /^#[0-9a-f]{6}$/i.test(v)) {
        if (String(prefs.accent).toLowerCase() === v.toLowerCase()) { done.push("强调色 → " + v + "（本来就是这样）"); continue; }
        prefs.accent = v; prefSaved = true; done.push("强调色 → " + v);
      } else if (k === "mode") {
        const sel = $("#modeSel2");
        if (sel) sel.value = v;
        const r = await save("/api/settings", { mode: v }, "保存过滤档位");
        if (r) done.push("过滤档位 → " + v);
      } else if (wKey[k]) {
        const n = snapShare(Math.max(0, Math.min(100, parseInt(v, 10) || 0)));
        platWs[wKey[k]] = n; weightSaved = true;
        done.push(wKey[k] + " 占比 → " + n + "%");
      } else if (k === "live_min_viewers") {
        liveMn = Math.max(0, parseInt(v, 10) || 0); liveSaved = true;
      } else if (k === "live_only_followed") {
        liveFol = !!parseInt(v, 10); liveSaved = true;
      }
    }
    if (prefSaved) { savePrefs(); applyPrefs(); }
    if (weightSaved) { renderPlatWeights(); renderRailShares(); }
    if (weightSaved) {
      const body = {};
      Object.keys(platWs).forEach((n) => { if (PLAT_KEY[n]) body[PLAT_KEY[n]] = String(platWs[n]); });
      if (await save("/api/tieba", body, "保存平台比例")) restartFeed();
      else done.push("（占比没存上, 已按服务器上的值还原）");
    }
    if (liveSaved) {
      const mnEl = $("#mLiveMin"), folEl = $("#mLiveFollowOnly");
      const mn = liveMn == null ? (parseInt(mnEl && mnEl.value, 10) || 0) : liveMn;
      const fol = liveFol == null ? !!(folEl && folEl.checked) : liveFol;
      const r = await save("/api/live/settings", { min_viewers: mn, only_followed: !!fol }, "保存直播筛选");
      if (r) {
        if (mnEl) mnEl.value = String(mn);
        if (folEl) folEl.checked = !!fol;
        done.push("直播筛选 → " + (mn > 0 ? "人气 ≥ " + mn : "不限人数") + (fol ? " · 只看关注" : ""));
        restartFeed();
      }
    }
    return done;
  }
  // 普通聊天别为了识别"改设置"白等一轮模型：只有命中设置名词/相对调整说法才走 plan，
  // 长句多半是在问问题，也直接进流式回答（原来每句话都要多等 1~3 秒）。
  const SETTING_HINT = /(设置|主题|黑夜|夜间|深色|暗色|白天|日间|浅色|布局|单栏|双栏|两栏|两列|单列|大图|封面|字号|字体|推荐理由|来源标识|强调色|主色|主题色|配色|占比|百分比|%|换回去|改回来|恢复原样|退回去|调大|调小|变大|变小|大一点|小一点|高一点|低一点|暗一点|亮一点)|(?:直播|关注|人气|观众|占比)[^，。]{0,8}(?:直播|关注|人气|观众|占比|筛选|门槛)/;
  function looksLikeSetting(t) {
    const s = String(t || "").trim();
    return s.length <= 80 && SETTING_HINT.test(s);
  }

  // 流式输出：思维链单独折叠、正文按 Markdown 增量渲染、可停止。
  async function sendChatStream(text, payload) {
    const t = (text == null ? $("#chatInput").value : text).trim();
    if (!t) return;
    if (text == null) $("#chatInput").value = "";
    // v0.4.41: AI 开启时不再本地预判"改设置" —— 整句话连界面状态一起交给智能体,
    // 由它读状态、调工具、自己总结; 只有 AI 关闭时才用本机规则兜底。
    if (!payload && !S.aiOn) {
      const act = looksLikeSetting(t) ? await nlApplySettings(t) : null;
      if (act) {
        addMsg("me", t);
        addMsg("ai", act.reply);
        try {
          const r = await api("/api/chat/note", { method: "POST", body: JSON.stringify({ cid: S.cid, me: t, ai: act.reply }) });
          if (r && r.cid) S.cid = r.cid;
        } catch (e) {}
        $("#msgs").scrollTop = $("#msgs").scrollHeight;
        loadConvs();
        return;
      }
    }
    addMsg("me", t);
    const aiEl = addMsg("ai", "");
    const tEl = aiEl.querySelector(".msg-text");
    tEl.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    let chips = [], trigger = null, browser = null, cid = S.cid,
        ctx = undefined, replied = "", think = null, gotContent = false, lastPaint = 0,
        acts = [], uiActs = [], feedQuery, feedReload = false, phaseEl = null;
    $("#chatStop").hidden = false;
    chatCtrl = new AbortController();
    const paint = (force) => { tEl.innerHTML = mdToHtml(replied); scrollMsgs(force); };
    try {
      const r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: t, cid: S.cid, stream: true,
                               effort: prefs.effort || "mid",
                               // 智能体要"看见"用户现在的界面与搜索状态, 否则只能瞎猜
                               ui: { theme: prefs.theme, layout: prefs.layout,
                                     cover: prefs.cover, font: prefs.font,
                                     reason: prefs.reason, srcacc: prefs.srcacc,
                                     accent: prefs.accent, q: S.q || "",
                                     view: S.view || "" },
                               payload: payload || undefined }),
        signal: chatCtrl.signal,
      });
      if ((r.headers.get("Content-Type") || "").indexOf("application/json") >= 0) {
        const d = await r.json();                       // 未配置 AI / 总开关关闭时后端回普通 JSON
        tEl.classList.remove("md");
        tEl.textContent = d.reply || d.error || "（没有返回内容）";
        chips = d.chips || []; cid = d.cid || cid; ctx = d.ctx; trigger = d.trigger || null;
        browser = d.browser || null;
        acts = d.actions || []; uiActs = d.ui_actions || [];
        feedQuery = d.feed_query; feedReload = !!d.feed_reload;
      } else {
        const reader = r.body.getReader(), dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const raw = buf.slice(0, i); buf = buf.slice(i + 2);
            let ev = "message", data = {};
            raw.split("\n").forEach((line) => {
              if (line.indexOf("event:") === 0) ev = line.slice(6).trim();
              else if (line.indexOf("data:") === 0) { try { data = JSON.parse(line.slice(5).trim()); } catch (e) {} }
            });
            if (ev === "meta") { chips = data.chips || []; cid = data.cid || cid; if (data.ctx !== undefined) ctx = data.ctx; }
            else if (ev === "think" || ev === "reason") {
              if (!think) think = addThinkBox(aiEl);   // think: 兼容旧后端的空事件
              if (ev === "reason") think.add(data.t || "");
            }
            else if (ev === "phase") {
              // 正在调工具: 在正文上方显示一行"正在…"（正文开始后清掉）
              if (!phaseEl) {
                phaseEl = document.createElement("div");
                phaseEl.className = "agentPhase";
                aiEl.insertBefore(phaseEl, tEl);
              }
              phaseEl.textContent = data.label || "正在执行操作…";
              scrollMsgs();
            }
            else if (ev === "act") {
              acts.push(data);
              if (phaseEl) phaseEl.textContent = (data.ok === false ? "✕ " : "✓ ") + (data.label || "");
            }
            else if (ev === "delta") {
              if (!gotContent) {
                gotContent = true; if (think) think.finish();
                if (phaseEl) { phaseEl.remove(); phaseEl = null; }
              }
              replied += data.t || "";
              const now = performance.now();
              if (now - lastPaint > 90) { lastPaint = now; paint(); }
            }
            else if (ev === "done") {
              replied = data.reply || replied;
              if (think) think.finish();
              chips = data.chips || chips; trigger = data.trigger || null;
              if (data.ctx !== undefined) ctx = data.ctx;
              if (data.browser) browser = data.browser;
              if (data.actions) acts = data.actions;
              if (data.cid) cid = data.cid;      // 智能体可能开了新对话
              uiActs = data.ui_actions || uiActs;
              if (data.feed_query !== undefined) feedQuery = data.feed_query;
              feedReload = !!data.feed_reload || feedReload;
            }
          }
        }
        if (think) think.finish();
        if (replied) paint();
        else if (!tEl.textContent || tEl.querySelector(".typing")) tEl.textContent = "（没有返回内容）";
      }
    } catch (e) {
      if (e && e.name === "AbortError") {
        if (replied) { paint(); tEl.insertAdjacentHTML("beforeend", '<span class="msgHint">（已停止生成）</span>'); }
        else tEl.textContent = "（已停止生成）";
      } else {
        tEl.classList.remove("md");
        tEl.textContent = "发送失败：" + e;
        toast("AI 请求失败：" + e, true);
      }
    }
    $("#chatStop").hidden = true; chatCtrl = null;
    if (cid) S.cid = cid;
    if (ctx !== undefined) {
      S.ctx = ctx && ctx.key ? ctx : null;
      $("#ctxBar").classList.toggle("show", !!S.ctx);
      if (S.ctx) $("#ctxTitle").textContent = "正在针对：《" + S.ctx.title + "》";
    }
    if (phaseEl) { phaseEl.remove(); phaseEl = null; }
    if (acts && acts.length) aiEl.appendChild(actsRow(acts));
    if (chips && chips.length) aiEl.appendChild(chipsRow(chips));
    // 智能体在服务端已改完设置; 这里的界面动作(主题/布局/封面/字号/强调色)要由前端落地
    if (uiActs && uiActs.length) {
      try { await applyPlan(uiActs); } catch (e) {}
    }
    let feedTouched = false;
    if (feedQuery !== undefined) {
      feedTouched = true;
      if (feedQuery) {
        if (S.view !== "feed") setView("feed");
        $("#searchInput").value = feedQuery;
        S.q = feedQuery; S.emptyMsg = "";
        saveState();
        $$(".nav[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === "feed"));
        restartFeed();
      } else if (S.q) {
        exitSearch();
      }
    }
    if (feedReload && !feedTouched) { feedTouched = true; restartFeed(); }
    if (browser && browser.url) openBrowserTab(browser.url, browser.title || "");
    scrollMsgs(true);
    loadConvs();                                   // 首句会自动命名，列表要跟着更新
    if (trigger === "settings") {
      loadSettings(); loadMachine(); loadStatus();
      if (!feedTouched) loadFeed(false);
    }
    else if (trigger === "random") { if (!feedTouched) loadFeed(false); }
    else if (trigger === "conv") { loadConvs(); loadChatHistory(); }
  }
  $("#chatSend").onclick = () => sendChatStream();
  // 思考强度档位（常驻输入框左边）：同时管聊天作答和"聊天里改设置"的思考量
  (function initEffort() {
    const sel = $("#chatEffort");
    if (!sel) return;
    prefs.effort = prefs.effort || "mid";
    sel.value = prefs.effort;
    sel.onchange = () => { prefs.effort = sel.value; savePrefs(); };
  })();
  $("#chatStop").onclick = () => { if (chatCtrl) { try { chatCtrl.abort(); } catch (e) {} } };
  $("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChatStream(); });
  $("#ctxClear").onclick = () => {
    S.ctx = null; $("#ctxBar").classList.remove("show");
    save("/api/chat/ctx", { cid: S.cid, key: "", title: "" }, "清除上下文");   // 服务端也要清，否则 AI 还在聊上一条
  };
  $("#newConv").onclick = async () => {
    try {
      const r = await save("/api/conv", {}, "新建对话");
      if (!r) return;
      // 后端 /api/conv 返回的是 {id}（不是 cid）—— 原来读 r.cid 会拿不到，继续用旧会话
      S.cid = r.id || null;
    } catch (e) { toast("新建对话失败：" + e, true); return; }
    $("#msgs").innerHTML = '<div class="msg ai">新对话开始了</div>';
    $("#ctxBar").classList.remove("show"); S.ctx = null;
    loadConvs();
  };
  $("#clearChat").onclick = async () => {
    // 真清空：后端也删（原来只清 DOM，刷新回来还在，AI 也还带着旧上下文）
    if (!(await save("/api/chat/clear", { cid: S.cid }, "清空对话"))) return;
    $("#msgs").innerHTML = '<div class="msg ai">告诉 AI 你想看什么样的信息</div>';
  };

  /* ================= 分栏拖动 / 开关 ================= */
  // 拖动时**只改两根宽度变量**。旧实现每帧调 applyPrefs(), 而它里面做的事跟拖拽毫无关系:
  //   · applyBg() 往 documentElement 上重写 6 个颜色变量 → 全文档样式失效(几百张卡片一起重算);
  //   · applyListCols() 读 clientWidth → 每帧一次强制同步重排;
  //   · renderBgRow()/paintRanges()/applyStatCard() 各自碰 DOM。
  // 体感就是"拖 AI 对话框特别卡"。现在: 每帧一次 rAF, 只写 2 个 CSS 变量 + 2 个 class;
  // 松手才跑一次 applyPrefs() 把其余样式补齐。拖动期间还关掉中间栏的 pointer-events,
  // 免得鼠标扫过卡片时一路触发 hover 状态。
  let splitRaf = 0;
  function applySplitOnly() {
    const html = document.documentElement, shell = $("#shell");
    shell.classList.toggle("le", !!prefs.le);
    shell.classList.toggle("re", !!prefs.re);
    html.style.setProperty("--leftW", (prefs.le ? 0 : prefs.leftW) + "px");
    html.style.setProperty("--rightW", (prefs.re ? 0 : prefs.rightW) + "px");
    const tl = $("#toggleLeft"), tr = $("#toggleRight");
    if (tl) tl.classList.toggle("collapsed", !!prefs.le);
    if (tr) tr.classList.toggle("collapsed", !!prefs.re);
  }
  document.addEventListener("pointerdown", (e) => {
    const sp = e.target.closest(".splitter");
    if (!sp) return;
    e.preventDefault();
    const which = sp.dataset.split;
    const x0 = e.clientX;
    const w0 = which === "left" ? (prefs.le ? 0 : prefs.leftW) : (prefs.re ? 0 : prefs.rightW);
    // ★ 右栏在屏幕右侧，它的「变宽」方向和鼠标位移是**相反**的：
    //   往左拖(dx<0) → 右栏变宽；往右拖(dx>0) → 右栏变窄。
    //   之前两栏共用 w0 + dx，右栏就是反的（往左拉反而缩、往右拉反而展）。
    const sign = which === "left" ? 1 : -1;
    document.body.classList.add("splitting");
    const move = (ev) => {
      const raw = w0 + sign * (ev.clientX - x0);
      if (which === "left") {
        if (raw < 90) { prefs.le = true; }
        else { prefs.le = false; prefs.leftW = Math.max(170, Math.min(430, raw)); }
      } else {
        if (raw < 90) { prefs.re = true; }
        else { prefs.re = false; prefs.rightW = Math.max(170, Math.min(460, raw)); }
      }
      if (!splitRaf) splitRaf = requestAnimationFrame(() => { splitRaf = 0; applySplitOnly(); });
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      if (splitRaf) { cancelAnimationFrame(splitRaf); splitRaf = 0; }
      document.body.classList.remove("splitting");
      applyPrefs();     // 收尾: 一次性补齐底色变量/色卡/列数/按钮文案
      savePrefs();
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  });
  $("#toggleLeft").onclick = () => { prefs.le = !prefs.le; savePrefs(); applyPrefs(); };
  $("#toggleRight").onclick = () => { prefs.re = !prefs.re; savePrefs(); applyPrefs(); };

  /* 手机端底部导航 */
  $$("#tabbar button").forEach((b) => (b.onclick = () => {
    const v = b.dataset.v;
    if (v === "chat") { document.body.classList.toggle("chatOpen"); syncTabs(); return; }
    document.body.classList.remove("chatOpen");
    if (v === "feed") setView("feed");
    if (v === "live") setView("live");
    if (v === "panel") setView("history");
    if (v === "settings") { setView("settings"); document.body.classList.add("drawer"); }
    syncTabs();
  }));

  /* ================= 启动 ================= */
  // 两道保险：关掉自动填充；把「本来就该是空的」输入框强制清空
  // （否则浏览器会把同源其它字段的值/表单状态灌进关键词、作者、搜索框）
  $$("input, textarea").forEach((e) => {
    e.setAttribute("autocomplete", "off");
    e.setAttribute("spellcheck", "false");
  });
  ["#kwInput", "#auInput", "#searchInput"].forEach((s) => { const e = $(s); if (e) e.value = ""; });

  $("#accentRow").innerHTML = ACCENTS.map(
    (c) => '<button class="sw" data-c="' + c + '" style="background:' + c + '"></button>').join("");
  applyPrefs();
  syncTabs();
  /* ================= 首次使用引导 ================= */
  // 只解决两件事：看什么、从哪来。AI 配置故意不放进来 —— 需要的人自己会去配，
  // 不需要的人被劝一次只会觉得烦。
  // 要不要弹由后端判定（本机吧列表 / 登录凭证 / 抓取记录都没有 且 没做过引导），
  // 不用 localStorage：换浏览器内核或清缓存不该让引导重新弹一遍。
  let introPage = 1;
  function introShow(p) {
    introPage = p || 1;
    $$("#intro .introPage").forEach((el) => { el.hidden = el.dataset.p !== String(introPage); });
    $$("#intro .introDots i").forEach((el, i) => el.classList.toggle("on", i + 1 === introPage));
    $("#introPrev").hidden = introPage <= 1;
    $("#introNext").textContent = introPage >= 2 ? "开始使用" : "下一步";
  }
  function openIntro() {
    const box = $("#intro");
    if (!box) return;
    box.hidden = false;
    document.body.classList.add("introOpen");
    introShow(1);
  }
  function closeIntro() {
    const box = $("#intro");
    if (box) box.hidden = true;
    document.body.classList.remove("introOpen");
  }
  // 无论"完成"还是"跳过"都记一次：不能每次启动都拦一遍
  function introDone() {
    api("/api/intro/done", { method: "POST", body: "{}" }).catch(function () {});
  }
  async function introFinish() {
    const picks = $$("#introChips .chip.on").map((b) => b.dataset.k);
    const extra = ($("#introNote").value || "").trim();
    let note = picks.length ? "我平时爱看：" + picks.join("、") : "";
    if (extra) note = note ? note + "。" + extra : extra;
    const prev = ($("#prefsNote").value || "").trim();
    if (note && prev) note = prev + "。" + note; else if (!note) note = prev;
    const bars = ($("#introBars").value || "").split("\n").map((x) => x.trim()).filter(Boolean);
    closeIntro();
    introDone();
    if (note && note !== prev) {
      if (await save("/api/settings", { prefs_note: note }, "保存偏好")) $("#prefsNote").value = note;
    }
    if (bars.length) {
      await save("/api/tieba", { bars: bars }, "保存吧列表");
      loadTiebaBars();
    }
    loadStatus();
    restartFeed();          // 刚配好就重取一屏，不用让用户再点刷新
  }
  async function maybeIntro() {
    try {
      const d = await api("/api/intro");
      if (d && d.show) openIntro();
    } catch (e) {}
  }
  $("#introX").onclick = () => { closeIntro(); introDone(); };
  $("#introSkip").onclick = () => { closeIntro(); introDone(); };
  $("#introPrev").onclick = () => introShow(introPage - 1);
  $("#introNext").onclick = () => { if (introPage >= 2) introFinish(); else introShow(introPage + 1); };
  $$("#introChips .chip").forEach((b) => (b.onclick = () => b.classList.toggle("on")));

  loadSettings();

  // 恢复到上次所在的视图与筛选（不再一律跳回信息流）
  const st0 = loadState();
  if (typeof st0.sort === "string" && st0.sort) { S.sort = st0.sort; $("#sortSel").value = S.sort; }
  if (typeof st0.src === "string") S.src = st0.src;
  const VIEWS = ["feed", "history", "saved", "live", "msg", "settings"];
  const v0 = VIEWS.indexOf(st0.view) >= 0 ? st0.view : "feed";
  if (v0 === "settings" && st0.sec) showSec(st0.sec);
  setView(v0);
  // 上次在搜索：接着搜，不把结果丢掉。这里必须用 restartFeed 而不是 loadFeed：
  // setView 刚刚已经用空搜索词发了一屏，S.loading=true 会把这次的 loadFeed 直接挡掉，
  // 之后 S.q 虽然挂上了、首屏却还是普通信息流 —— 搜索框和列表对不上。
  if (v0 === "feed" && st0.q) {
    S.q = st0.q;
    $("#searchInput").value = st0.q;
    restartFeed();
  }
  loadBlacklist();
  loadMachine();
  loadTiebaBars();
  loadConvs();
  loadChatHistory();      // 打开就把服务端已有的对话读回来（含当前会话标题）
  loadMessages();
  loadAccount();
  refreshFloatBall();
  // 未读角标 5 分钟刷一次(v0.3.1.55)。原来 60 秒一次, 而一次 /api/messages 要在贴吧翻
  // 4 个页面(登录态 + 回复我 + @我 + 又查一次登录态) —— 挂着不动就是 240 次/小时,
  // 正是把出口 IP 打成"刷站"的那股流量。角标晚几分钟不误事, 被拦了才真误事。
  setInterval(loadMessages, 300000);

  // 首次使用：判定权在后端（本机一条来源都没配 + 没做过引导）
  maybeIntro();

  /* ================= 页面租约 + 前端自动更新 ================= */
  // 页面租约：服务端据此知道「还有没有界面在跑」。全部离开后（3 秒宽限）才关停服务与悬浮球；
  // 浏览器崩溃发不出 leaving 时，看门狗按最后心跳超时收尾。
  const leaseToken = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
    : (Date.now().toString(36) + Math.random().toString(36).slice(2));
  function sendLease(action, keepalive) {
    const text = JSON.stringify({ action: action, token: leaseToken });
    if (keepalive && navigator.sendBeacon) {
      navigator.sendBeacon("/api/shutdown", new Blob([text], { type: "application/json" }));
      return;
    }
    fetch("/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" },
                             body: text, keepalive: !!keepalive }).catch(function () {});
  }
  sendLease("alive");
  window.addEventListener("pageshow", () => sendLease("alive"));
  window.addEventListener("pagehide", () => sendLease("leaving", true));
  // 后台隐藏页的定时器会被节流到约 1 分钟，间隔取 30 秒、服务端宽限 150 秒
  setInterval(() => sendLease("alive"), 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) sendLease("alive"); });

  // 前端文件指纹变了就自动重载（改了 index.html/app.js/style.css 不用手动 Ctrl+F5）
  let uiVer = null;
  setInterval(async () => {
    try {
      const r = await fetch("/api/ui_version", { cache: "no-store" });
      const d = await r.json();
      if (!uiVer) { uiVer = d.version; return; }
      if (d.version && d.version !== uiVer) location.reload();
    } catch (e) {}
  }, 30000);
})();
