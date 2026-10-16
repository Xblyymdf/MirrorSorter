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
  // 打开原文 = 记历史（/api/click）+ 新窗口打开，和旧版 openItem() 一致
  function openItem(key, url) {
    // 记历史是附带动作，失败不打断「打开原文」本身
    if (key) api("/api/click", { method: "POST", body: JSON.stringify({ key: key }) }).catch(function () {});
    if (url) window.open(url, "_blank", "noopener");
  }

  /* ================= 界面偏好 ================= */
  const PREF_KEY = "ms-new-prefs";
  const prefs = Object.assign(
    { theme: "dark", accent: "#fb7299", layout: "single", cover: "m",
      leftW: 238, rightW: 235, le: false, re: false,
      bgHex: "", bgDepth: 0, font: "small", reason: "show", statOpen: true },
    JSON.parse(localStorage.getItem(PREF_KEY) || "{}")
  );
  const savePrefs = () => localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
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
                    ["#fontRow", "font"], ["#reasonRow", "reason"]];
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
    }
    // 字体大小 = 整界面缩放。100vh 在 zoom 下会被乘大，布局高度要除回来
    // （否则全屏时界面比视口高一截，侧栏底部永远滚不到）
    const z = prefs.font === "medium" ? 1.15 : prefs.font === "large" ? 1.3 : 1;
    document.body.style.zoom = z === 1 ? "" : String(z);
    html.style.setProperty("--ui-zoom", String(z));
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
              reqGen: 0 };

  // 触屏设备(pointer: coarse, 含安卓壳)不显示「站内播放」：旧版就是 `bvid && !isTouchDevice()`，
  // 因为 WebView 里嵌 B站 播放器不可靠。桌面端才给。
  const isTouch = () => window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
  function cardHTML(it) {
    const plat = it.platform || "B站";
    const cls = plat === "贴吧" ? "tb" : plat === "小黑盒" ? "xhh" : "bili";
    const covers = (it.covers && it.covers.length) ? it.covers : (it.cover ? [it.cover] : []);
    const url = it.url || "";
    const bvid = (url.match(/BV[0-9A-Za-z]{8,12}/) || [])[0] || "";
    // 站内阅读：贴吧 wap 帖 / 小黑盒帖子都能直接解析成文（不依赖浏览器窗口）
    const tid = plat === "贴吧" ? ((url.match(/\/p\/(\d+)/) || [])[1] || "") : "";
    const xlid = plat === "小黑盒" ? ((url.match(/bbs\/link\/([0-9A-Za-z]+)/) || [])[1] || "") : "";
    const openAttr = url ? ' data-act="open" title="打开原文"' : "";
    const thumb = covers.length
      ? '<div class="thumb"' + openAttr + ">" + '<img loading="lazy" referrerpolicy="no-referrer" src="' +
        esc(covers[0]) + '" onerror="this.remove()">' +
        (it.duration_known && it.duration ? '<div class="dur">' + dur(it.duration) + "</div>" : "") +
        '<div class="stats"><span>播放 ' + fmt(it.views) + "</span>" +
        (it.danmaku != null ? "<span>弹 " + fmt(it.danmaku) + "</span>" : "") +
        (it.reply != null ? "<span>评 " + fmt(it.reply) + "</span>" : "") + "</div></div>"
      : '<div class="thumb"' + openAttr + ">" + esc((it.title || "?").slice(0, 1)) + "</div>";
    return '<article class="card" data-key="' + esc(it.key) + '" data-author="' + esc(it.author) +
      '" data-plat="' + esc(plat) + '" data-url="' + esc(url) + '" data-bvid="' + esc(bvid) + '">' +
      '<h3><span class="pb ' + cls + '">' + esc(plat) + '</span><span class="t"' + openAttr + ' title="' +
        esc(it.title) + '">' + esc(it.title) + "</span></h3>" +
      '<div class="media">' + thumb + '<div class="col">' +
        '<div class="meta"><span class="au">' + esc(it.author || "用户") + "</span> · 播放 " + fmt(it.views) +
          (it.pubdate ? " · " + ago(it.pubdate) : "") + "</div>" +
        (it.desc ? '<div class="desc">' + esc(it.desc) + "</div>" : "") +
        (it.reason ? '<div class="reason">' + ic("bulb", "xs") + "<span>" + esc(it.reason) + "</span></div>" : "") +
      "</div></div>" +
      '<div class="acts">' +
        (url ? '<button class="pri" data-act="open">' + ic("external") + "打开原文</button>" : "") +
        (bvid && !isTouch() ? '<button class="pri" data-act="play">' + ic("play") + "站内播放</button>" : "") +
        '<button class="sec" data-act="refresh">' + ic("shuffle") + "换一条</button>" +
        '<button class="sec" data-act="like">' + ic("thumb-up") + "喜欢</button>" +
        '<button class="sec" data-act="dislike">' + ic("thumb-down") + "不喜欢</button>" +
        '<button class="sec" data-act="block">' + ic("ban") + "屏蔽作者</button>" +
        (tid ? '<button class="sec" data-act="read" data-tid="' + esc(tid) + '">' + ic("list") + "站内阅读</button>"
             : xlid ? '<button class="sec" data-act="xread" data-lid="' + esc(xlid) + '">' + ic("list") + "站内阅读</button>" : "") +
        '<button class="sec" data-act="save">' + ic("star") + "收藏</button>" +
        '<button class="sec" data-act="ask">' + ic("spark") + "问问 AI</button>" +
      "</div></article>";
  }
  function renderItems(items) {
    if (!items.length) {
      if (S.offset === 0) {
        const txt = S.view === "history" ? "还没有历史记录" : S.view === "saved" ? "还没有收藏" : "没有内容";
        $("#list").innerHTML = '<div class="empty">' + ic("inbox") + "<span>" + txt + "</span></div>";
      }
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
          $("#list").innerHTML = '<div class="empty">' + ic("refresh") + "<span>" +
            esc(S.emptyMsg || "正在准备内容…") + "</span></div>";
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
    loadFeed(false);
  }

  // 内容源那一行显示什么：能动手解决的才提示，其余只说新鲜度
  function srcState(s) {
    const today = s.fetched_today ? "今日 " + fmt(s.fetched_today) + " 条" : "";
    if (s.error) return { text: "抓取失败", warn: true, tip: (s.error + " " + today).trim() };
    if (s.name === "贴吧" && !s.bars) {
      return { text: "未配置吧列表", warn: true, tip: "去「设置 → 平台账号」加入要看的贴吧" };
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
      // 内容源那一行不必播报抓取器的内部状态（"未抓取/正常"是给运维看的，用户看不懂也不需要）：
      // 默认只安静地说"更新于多久前"，只有真要用户动手时才出警示色。
      (d.sources || []).forEach((s) => {
        const el = document.querySelector('.st[data-st="' + s.name + '"]');
        if (!el) return;
        const info = srcState(s);
        el.textContent = info.text;
        el.title = info.tip;
        el.classList.toggle("warn", !!info.warn);
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
    const card = e.target.closest(".card");
    if (!card) return;
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const key = card.dataset.key, act = el.dataset.act;
    if (act === "open") { openItem(key, card.dataset.url); return; }
    if (act === "play") {
      const bvid = card.dataset.bvid;
      if (!bvid) { toast("这条没有可播放的站内地址", true); return; }
      openModal("B站 · 站内播放",
        '<div style="aspect-ratio:16/9"><iframe style="width:100%;height:100%;border:0;border-radius:10px" ' +
        'src="https://player.bilibili.com/player.html?bvid=' + esc(bvid) +
        '&page=1&high_quality=1&danmaku=0&autoplay=1" allowfullscreen scrolling="no"></iframe></div>',
        [{ t: "跳B站", fn: () => window.open("https://www.bilibili.com/video/" + bvid, "_blank", "noopener") }]);
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
  function keepFeed() {
    const l = $("#list");
    // 不要再判 S.view: 调用点已经在 prev==="feed" 时才会调, 而 setView 开头已把 S.view 改掉了
    if (!l || S.q) return;
    feedKeep = { html: l.innerHTML, top: l.scrollTop, offset: S.offset, done: S.done, gen: S.gen };
  }
  function restoreFeed() {
    const l = $("#list");
    if (!l || !feedKeep || !feedKeep.html) return false;
    l.innerHTML = feedKeep.html;
    l.scrollTop = feedKeep.top;
    S.offset = feedKeep.offset; S.done = feedKeep.done;
    if (feedKeep.gen != null) S.gen = feedKeep.gen;
    applySrcFilter();
    return true;
  }

  const PANES = { settings: "viewSettings", msg: "viewMsg" };
  function setView(v) {
    const prev = S.view;
    S.view = v;
    const pane = PANES[v] || null;
    const inPane = !!pane;
    if (!inPane) {
      S.lastList = v;
      S.q = "";
      const si = $("#searchInput"); if (si) si.value = "";
      S.offset = 0;
      if (prev !== v) {
        // 换视图必须把列表清干净再画。
        // 起因: "绝不清空列表"那条规则只该管**同一个视图里滚动**；跨视图时如果新视图这一批
        // 恰好返回空（会话游标暂时没货、正在补池），旧视图的卡片就会一直留在屏幕上，
        // 看起来就是"点了信息流不跳转"。
        if (prev === "feed") keepFeed();
        S.done = false; S.waiting = false; S.refillTries = 0;
        const l = $("#list"); if (l) l.innerHTML = "";
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
    if (v === "msg") { renderMsgPane(); return; }
    if (inPane) return;
    if (v === "feed" && restoreFeed()) return;   // 手里还有刚看的信息流 → 原样贴回，秒切不重取
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

  // 「刷新」= 重读**当前所在的视图**，绝不把用户拽回信息流。
  // 信息流上还要换一池新的：先立刻返回当前内容（不白屏），新一代落地后自动换上。
  async function refreshFeed() {
    if (S.view === "msg") { renderMsgPane(); loadMessages(); return; }
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
  $("#btnClearPool").onclick = async () => {
    if (!confirm("清空信息池：丢弃已收录的全部内容与「已看过」记录，再按当前设置重新抓取。\n\n" +
                 "不含历史 / 收藏 / 聊天（那些在「设置 → 数据」里清）。\n" +
                 "重新收录需要十几秒，这期间列表会是空的。")) return;
    if (!(await save("/api/pool/clear", {}, "清空信息池"))) return;
    // 让"清空了"这件事在界面上真的看得见：列表变成空态并说明正在重收（空态文案由这里决定）
    S.offset = 0; S.done = false; S.waiting = false; S.refillTries = 0;
    S.emptyMsg = "正在按当前设置重新收录…";
    S.reqGen++;                     // 作废在途请求，免得旧数据把空态盖掉
    S.loading = false;
    $("#list").innerHTML = "";
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
        on = (v === "feed" && S.view === "feed") || (v === "panel" && S.view === "history") ||
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
  });

  // 搜索常驻顶栏：没有开合动作了，清空输入 = 退出搜索回到信息流
  let searchTimer = null;
  $("#searchInput").oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      S.q = $("#searchInput").value.trim();
      S.offset = 0;
      S.view = "feed";
      saveState();
      $$(".nav[data-nav]").forEach((b) => b.classList.toggle("on", b.dataset.nav === "feed"));
      loadFeed(false);   // 搜索不该带 refresh=1：原来每敲一次字都可能让后端去重建内容池
    }, 450);
  };
  $("#searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#searchInput").blur(); });

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
      if (m.tieba && m.tieba.logged_in) setAva("#avaTb", "/api/tieba/avatar?t=" + Date.now(), "贴");
      set("#accXhhState", (m.xhh && m.xhh.logged_in) ? "已登录" : "未登录");
      if (m.xhh && m.xhh.logged_in) setAva("#avaXhh", "/api/xhh/avatar?t=" + Date.now(), "黑");
    } catch (e) {}
  }
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
  }
  $$(".svItem").forEach((b) => (b.onclick = () => showSec(b.dataset.sec)));
  $("#btnBack").onclick = () => setView(S.lastList || "feed");
  $("#btnSettings").onclick = () => {
    setView("settings");
    if (narrow()) document.body.classList.add("drawer");
  };
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
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
      segPick("#aiminRow", s.ai_interest_min || "0");
      segPick("#aifullRow", s.ai_full_scan || "0");
      segPick("#aibatchRow", s.ai_batch || "15");
      segPick("#aicacheRow", s.ai_cache_hours || "24");
      const pi = $("#priceIn"), po = $("#priceOut");
      if (pi) pi.value = s.price_in || "";
      if (po) po.value = s.price_out || "";
      // B站增强：超分用「开关 + 档位」两个键存，档位=关 时开关为 0（旧版同做法）
      segPick("#srLevelRow", s.ui_sr === "0" ? "关" : (s.ui_sr_level || "关"));
      segPick("#srScaleRow", s.ui_sr_scale || "2");
      segPick("#bqLevelRow", s.ui_bq === "0" ? "关闭" : (s.ui_bq_level || "中"));
      segPick("#ownuiRow", s.ui_ownui || "1");
      segPick("#openModeRow", s.ui_new_window === "0" ? "0" : "1");
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
  segBind("#aiminRow", "ai_interest_min", "保存推荐标准");
  segBind("#aifullRow", "ai_full_scan", "保存评分范围");
  segBind("#aibatchRow", "ai_batch", "保存批大小");
  segBind("#aicacheRow", "ai_cache_hours", "保存缓存时长");
  segBind("#srScaleRow", "ui_sr_scale", "保存超分倍率");
  segBind("#ownuiRow", "ui_ownui", "保存播放页自有 UI");
  segBind("#openModeRow", "ui_new_window", "保存打开方式");
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
    ["DeepSeek", ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp", "deepseek-chat", "deepseek-reasoner"]],
    ["智谱 GLM", ["glm-4-plus", "glm-4-flash"]],
    ["通义千问", ["qwen-turbo", "qwen-plus", "qwen-max"]],
    ["OpenAI 兼容", ["gpt-4o-mini", "gpt-4o"]],
    ["本地 Ollama", ["qwen3:8b", "qwen3:4b"]],
  ];
  $("#modelPresets").innerHTML = MODEL_PRESETS.map(([g, ms]) =>
    ms.map((m) => '<option value="' + esc(m) + '" label="' + esc(g) + '">').join("")).join("");
  $("#aiReset").onclick = async () => {
    if (!confirm("把 AI 推荐模式的设置恢复成默认值？")) return;
    const r = await save("/api/settings", { ai_full_scan: "0", ai_interest_min: "0", ai_batch: "15", ai_cache_hours: "24" }, "恢复默认设置");
    if (r) { loadSettings(); restartFeed(); }
  };
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
  function machineTab(name) {
    $$("#mTabs .seg").forEach((b) => b.classList.toggle("on", b.dataset.m === name));
    $$(".mPane").forEach((p) => p.classList.toggle("on", p.dataset.m === name));
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
    const base = [0, 25, 50, 75, 100];
    box.innerHTML = Object.keys(platWs).map((name) =>
      '<div class="pwRow"><span class="lbl">' + esc(name) + "</span>" +
      levelsOf(platWs[name]).map((w) => '<button class="seg ' + (platWs[name] === w ? "on" : "") + '" data-w="' + w +
        '" data-name="' + esc(name) + '">' + w + "</button>").join("") + "</div>").join("");
  }
  $("#platWeightRows").addEventListener("click", async (e) => {
    const b = e.target.closest(".seg");
    if (!b) return;
    const old = platWs[b.dataset.name];
    platWs[b.dataset.name] = parseInt(b.dataset.w, 10);
    renderPlatWeights();
    const KEYMAP = { "B站": "bili_weight", "贴吧": "tb_weight", "小黑盒": "xhh_weight" };
    const body = {};
    Object.keys(platWs).forEach((n) => { if (KEYMAP[n]) body[KEYMAP[n]] = String(platWs[n]); });
    if (!(await save("/api/tieba", body, "保存平台权重"))) {
      platWs[b.dataset.name] = old; renderPlatWeights(); return;
    }
    restartFeed();
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
      ftLoadPanel(st, d.ft);
      setC("#mFollowEmph", st.bili_follow_emph !== "0");
      const fm = $("#mFollowMonths");
      if (fm) { fm.value = st.bili_follow_months || "1"; $("#mFollowMonthsVal").textContent = fm.value; }
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
    } catch (e) { toast("读取筛选设置失败：" + e, true); }
    loadTiebaSettings();
  }
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
                     ["ftLabYy", "yy"], ["ftLabNeg", "neg"]];
  function ftStatusText(f) {
    if (!f) return "模型状态：读取中…";
    if (f.has_module === false) return "模型状态：本机缺少运行时（desktop/model/ 下应有 ftrt.py）";
    if (!f.ready && f.on) return "模型状态：加载中…（首次要几秒）" + (f.err ? "；" + f.err : "");
    if (!f.ready) return "模型状态：待机（" + (f.size_mb || 0) + " MB，开启后加载）";
    return "模型状态：已加载 " + (f.size_mb || 0) + " MB · 标签 " + (f.labels || []).length +
      " 个 · 缓存 " + (f.cache || 0) + " 条";
  }
  function ftLoadPanel(st, f) {
    if (st) {
      const sc = (id, v) => { const e = $(id); if (e) e.checked = !!v; };
      sc("#ftOn", st.ft_on === "1");
      sc("#ftVideo", st.ft_video !== "0");
      sc("#ftDm", st.ft_dm !== "0");
      sc("#ftCf", st.ft_cf !== "0");
      const have = String(st.ft_labels || "").split(",").map((x) => x.trim()).filter(Boolean);
      FT_LABELS.forEach(([id, key]) => sc("#" + id, have.indexOf(key) >= 0));
      segPick("#ftThreshRow", st.ft_thresh || "70");
    }
    const el = $("#ftStatus");
    if (el) el.textContent = ftStatusText(f);
  }
  async function ftRefreshStatus() {
    try { ftLoadPanel(null, await api("/api/textfilter/status")); } catch (e) {}
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
    const r = await save("/api/machine", {
      ft_video: $("#ftVideo").checked ? "1" : "0",
      ft_dm: $("#ftDm").checked ? "1" : "0",
      ft_cf: $("#ftCf").checked ? "1" : "0",
      ft_labels: labs.join(","),
      ft_thresh: (pick && pick.dataset.v) || "70",
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
    const verdict = r.hit ? " → 会拦"
      : (r.picked ? " → 不拦（低于阈值 " + Math.round((r.thresh || 0) * 100) + "%）" : " → 不拦（该标签没勾）");
    const top = (r.top || []).slice(0, 3).map((x) => x.name + " " + Math.round(x.score * 100) + "%").join(" · ");
    out.textContent = "判成「" + (r.name || r.label) + "」" + Math.round((r.score || 0) * 100) + "%" + verdict +
      "；前三：" + top;
  }
  $("#ftProbeBtn").onclick = ftProbe;
  $("#ftProbeIn").addEventListener("keydown", (e) => { if (e.key === "Enter") ftProbe(); });

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
  $("#ioExpDownload").onclick = () => {
    const t = $("#ioExpText").value;
    if (!t) { toast("请先生成导出内容", true); return; }
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
    ["快速文本分析", ["ft_on", "ft_video", "ft_dm", "ft_cf", "ft_thresh", "ft_labels"]],
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
    loadSettings(); loadMachine(); loadTiebaSettings(); loadBlacklist(); loadStatus();
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

  function openModal(title, html, buttons) {
    $("#modal").innerHTML =
      '<div class="mhead"><span>' + esc(title) + '</span><button class="x" id="mClose" aria-label="关闭">' + ic("x") + '</button></div>' +
      '<div class="mbody">' + html + "</div>" +
      '<div class="mfoot">' + (buttons || []).map((b, i) =>
        '<button data-i="' + i + '">' + esc(b.t) + "</button>").join("") + "</div>";
    $("#mask").classList.add("show");
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
  $("#accBiliOpen").onclick = () => window.open("https://passport.bilibili.com/login", "_blank");
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
  function renderMsgBody(body, d) {
    const line = (k, label) => {
      const o = d[k] || {};
      const state = o.logged_in ? (o.unread ? "未读 " + o.unread : "没有未读") : "未登录";
      return '<div class="srcRow"><b>' + label + '</b><span class="lbl">' + state + "</span>" +
        (o.url ? '<button class="alt" data-url="' + esc(o.url) + '">打开</button>' : "") + "</div>";
    };
    body.innerHTML = '<p class="tip">各平台消息状态（点「打开」去对应平台处理）。</p>' +
      line("bili", "B站") + line("tieba", "贴吧") + line("xhh", "小黑盒");
    body.querySelectorAll("button[data-url]").forEach((b) => (b.onclick = () => window.open(b.dataset.url, "_blank")));
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
      let n = 0;
      ["bili", "tieba", "xhh"].forEach((k) => { const u = d[k] && d[k].unread; if (u) n += u; });
      $("#navMsgBadge").textContent = n ? (n > 99 ? "99+" : n) : "";
    } catch (e) {}
  }

  /* ================= 封面悬停预览（已按用户要求整块移除）================= */
  // 原来鼠标停在封面上会放大并试播一小段（走 /api/preview → /api/video-proxy）。
  // 用户明确说不需要，整块删掉。顺带省掉的是实测每开一次界面就上百次 /api/preview 请求
  // ——那每一条都要服务端去问一次 B站 接口，只为了一次悬停。

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
  function addMsg(who, text, chips) {
    const d = document.createElement("div");
    d.className = "msg " + (who === "me" ? "me" : "ai");
    const t = document.createElement("div");
    t.className = "msg-text";
    t.textContent = text == null ? "" : String(text);
    d.appendChild(t);
    if (chips && chips.length) d.appendChild(chipsRow(chips));
    $("#msgs").appendChild(d);
    $("#msgs").scrollTop = $("#msgs").scrollHeight;
    return d;
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
  // 流式输出：增量渲染 + 可停止（旧版有，新版补上）
  async function sendChatStream(text, payload) {
    const t = (text == null ? $("#chatInput").value : text).trim();
    if (!t) return;
    if (text == null) $("#chatInput").value = "";
    addMsg("me", t);
    const aiEl = addMsg("ai", "");
    const tEl = aiEl.querySelector(".msg-text");
    let chips = [], trigger = null, cid = S.cid, ctx = undefined, replied = "";
    $("#chatStop").hidden = false;
    chatCtrl = new AbortController();
    const scroll = () => { $("#msgs").scrollTop = $("#msgs").scrollHeight; };
    try {
      const r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: t, cid: S.cid, stream: true, payload: payload || undefined }),
        signal: chatCtrl.signal,
      });
      if ((r.headers.get("Content-Type") || "").indexOf("application/json") >= 0) {
        const d = await r.json();                       // 未配置 AI / 总开关关闭时后端回普通 JSON
        tEl.textContent = d.reply || d.error || "（没有返回内容）";
        chips = d.chips || []; cid = d.cid || cid; ctx = d.ctx; trigger = d.trigger || null;
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
            else if (ev === "think") { if (!tEl.textContent) tEl.textContent = "思考中…"; }
            else if (ev === "delta") { replied += data.t || ""; tEl.textContent = replied; scroll(); }
            else if (ev === "done") { replied = data.reply || replied; chips = data.chips || chips; trigger = data.trigger || null; if (data.ctx !== undefined) ctx = data.ctx; }
          }
        }
        tEl.textContent = replied || tEl.textContent || "（没有返回内容）";
      }
    } catch (e) {
      if (e && e.name === "AbortError") tEl.textContent = (tEl.textContent || "") + "（已停止生成）";
      else { tEl.textContent = "发送失败：" + e; toast("AI 请求失败：" + e, true); }
    }
    $("#chatStop").hidden = true; chatCtrl = null;
    if (cid) S.cid = cid;
    if (ctx !== undefined) {
      S.ctx = ctx && ctx.key ? ctx : null;
      $("#ctxBar").classList.toggle("show", !!S.ctx);
      if (S.ctx) $("#ctxTitle").textContent = "正在针对：《" + S.ctx.title + "》";
    }
    if (chips && chips.length) aiEl.appendChild(chipsRow(chips));
    scroll();
    loadConvs();                                   // 首句会自动命名，列表要跟着更新
    if (trigger === "settings") { loadSettings(); loadMachine(); loadStatus(); loadFeed(false); }
    else if (trigger === "random") loadFeed(false);
    else if (trigger === "conv") { loadConvs(); loadChatHistory(); }
  }
  $("#chatSend").onclick = () => sendChatStream();
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
  document.addEventListener("pointerdown", (e) => {
    const sp = e.target.closest(".splitter");
    if (!sp) return;
    e.preventDefault();
    const which = sp.dataset.split;
    const x0 = e.clientX;
    const w0 = which === "left" ? (prefs.le ? 0 : prefs.leftW) : (prefs.re ? 0 : prefs.rightW);
    const move = (ev) => {
      const raw = w0 + (ev.clientX - x0);
      if (which === "left") {
        if (raw < 90) { prefs.le = true; }
        else { prefs.le = false; prefs.leftW = Math.max(170, Math.min(430, raw)); }
      } else {
        if (raw < 90) { prefs.re = true; }
        else { prefs.re = false; prefs.rightW = Math.max(170, Math.min(460, raw)); }
      }
      applyPrefs();
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
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
  loadSettings();

  // 恢复到上次所在的视图与筛选（不再一律跳回信息流）
  const st0 = loadState();
  if (typeof st0.sort === "string" && st0.sort) { S.sort = st0.sort; $("#sortSel").value = S.sort; }
  if (typeof st0.src === "string") S.src = st0.src;
  const VIEWS = ["feed", "history", "saved", "msg", "settings"];
  const v0 = VIEWS.indexOf(st0.view) >= 0 ? st0.view : "feed";
  if (v0 === "settings" && st0.sec) showSec(st0.sec);
  setView(v0);
  if (st0.q) {                       // 上次在搜索：接着搜，不把结果丢掉
    S.q = st0.q;
    $("#searchInput").value = st0.q;
    loadFeed(false);
  }
  loadBlacklist();
  loadMachine();
  loadTiebaBars();
  loadConvs();
  loadChatHistory();      // 打开就把服务端已有的对话读回来（含当前会话标题）
  loadMessages();
  loadAccount();
  refreshFloatBall();
  setInterval(loadMessages, 60000);

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
