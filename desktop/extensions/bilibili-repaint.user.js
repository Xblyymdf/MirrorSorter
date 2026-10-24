/* 洞鉴虚明 · 滚动/全屏停摆自愈 (v0.4.25dsk)
 *
 * 现象: B站播放页评论区往下拉时, 偶发整页画布不再更新(rAF 也停了), 看起来像"卡死"。
 * 这不是 DOM/接口卡住 —— 页面自己的渲染循环停了, 只能由壳做一次"补呈现"
 * (隐藏→显示 / 置顶 / 强制重绘, 不重载页面、不丢已加载的评论)。
 *
 * 触发条件全部满足才发请求, 避免把后台节流当故障:
 *   · 页面可见;
 *   · 非全屏: 用户最近 2.5 秒内滚动过, 且 rAF 超过 1.6 秒没有跳动, 最多 3 次;
 *   · 全屏: rAF 超过 2.4 秒没有跳动(切全屏本身会短暂停一拍), 最多 2 次。
 *     壳端在全屏时只做 gentle(置顶+重绘), 不会把浏览器踢出全屏。
 */
(function () {
  if (window.__msxRepaint) return;
  window.__msxRepaint = 1;
  if (window.top !== window.self) return;
  try {
    var h = location.hostname || "";
    if (h !== "bilibili.com" && !/\.bilibili\.com$/.test(h)) return;
  } catch (e) { return; }

  var TICK = 700, RAF_STALL = 1600, SCROLL_WARM = 2500;
  var FS_STALL = 2400;             // 全屏里 rAF 停摆更久才认(切全屏会短暂停一拍)
  var lastRaf = Date.now(), lastScroll = 0, lastAsk = 0, tries = 0;
  var healthySince = 0, asking = false;
  var raf = window.requestAnimationFrame;

  function pathOK() {
    try {
      var p = location.pathname || "";
      return p.indexOf("/video/") === 0 || p.indexOf("/bangumi/play/") === 0;
    } catch (e) { return false; }
  }
  function beat() {
    if (document.visibilityState === "visible") lastRaf = Date.now();
    if (raf) raf(beat);
  }
  if (raf) raf(beat);

  function onScroll() {
    if (document.visibilityState !== "visible") return;
    lastScroll = Date.now();
  }
  var acts = ["scroll", "wheel", "touchmove"];
  for (var i = 0; i < acts.length; i++) {
    try { window.addEventListener(acts[i], onScroll, { passive: true, capture: true }); }
    catch (e) {}
  }
  try {
    document.addEventListener("visibilitychange", function () {
      lastRaf = Date.now(); lastScroll = 0; tries = 0;
    });
  } catch (e) {}

  function ask(why) {
    if (asking) return;
    asking = true;
    var port = String(window.__MSX_PORT__ || "8000");
    try {
      fetch("http://127.0.0.1:" + port + "/api/win/repaint", {
        method: "POST", mode: "no-cors", cache: "no-store",
        credentials: "omit", keepalive: true, targetAddressSpace: "loopback"
      }).catch(function () {});
    } catch (e) {}
    setTimeout(function () { asking = false; }, 3000);
    try { console.warn("[msx] repaint:", why, "tries=" + tries); } catch (e) {}
  }

  setInterval(function () {
    var now = Date.now();
    if (!pathOK() || document.visibilityState !== "visible") return;
    if (document.fullscreenElement) {
      /* 全屏: 用户不会滚动, 不看 scroll; 但 rAF 停摆同样要补 —— 切全屏时合成层
         偶发停摆, 不补就一直冻在最后一帧(用户报修: 全屏后画布卡住半天)。
         暂停时不报(暂停本来就没有帧); 每次停摆最多 2 次, 间隔 8 秒。
         健康 3 秒就把次数归零: 旧版计数一旦用完, 这一整个会话的全屏停摆都不再有人管。 */
      var vid = document.querySelector("video");
      if (vid && vid.paused) { lastRaf = now; return; }
      if (now - lastRaf < FS_STALL) {
        if (now - lastRaf < 500) {
          if (!healthySince) healthySince = now;
          if (now - healthySince > 3000) tries = 0;
        }
        return;
      }
      if (tries >= 2) return;
      if (now - lastAsk < 8000) return;
      tries++;
      lastAsk = now;
      ask("fullscreen-stall " + tries);
      return;
    }
    if (now - lastRaf < RAF_STALL) {
      if (now - lastRaf < 500) {
        if (!healthySince) healthySince = now;
        if (now - healthySince > 3000) tries = 0;
      }
      return;
    }
    healthySince = 0;
    if (now - lastScroll > SCROLL_WARM) return;   // 不是滚动引起的停摆, 不代劳
    if (tries >= 3) return;
    if (now - lastAsk < 6000) return;
    tries++;
    lastAsk = now;
    ask("scroll-stall " + tries);
  }, TICK);
})();
