/* 洞鉴虚明 内置AI超分(B站) — ACNet(Anime4KCPP f8b4) / Anime4K WebGPU 实时超分辨率
 *
 * 依赖:
 *   extensions/acnet-webgl.js       WebGL2 多趟跑 ACNet 12 趟(默认路径, 走 luma 重建)
 *   extensions/_acnet/acnet_f8b4.glsl 上游 GLSL 原文(由壳注入成 __MSX_ACNET_GLSL__)
 *   anime4k-webgpu.js               仅低清源回退用(lib.CNNx2M / CNNx2VL / CNNx2UL / GANUUL)
 * 开关: window.__MSX_SR__ (1/0, 服务端设置 ui_sr); 与 AI补帧(ui_fg)互斥。
 * 档位: window.__MSX_SR_LEVEL__ (轻=CNNx2M / 标准=CNNx2VL / 高=CNNx2UL / 旗舰=高+GAN —— 仅回退路用)
 * 倍率: window.__MSX_SR_SCALE__ (1|1.25|1.5|2)  —— ACNet 路径下只影响显示/上报, 见下
 *
 * ── 2026-10 重写(针对用户实测的两条: 全屏开了超分反而更糊 + 大量性能黑洞) ──────────
 * 【画质】旧版 `plan()` 里对 ≥1280 的源把超分输入按 `SCALE/2` 缩小:
 *   1.25× → 输入只有 0.625×1920=1200 宽, 先真丢 37.5% 的源细节再让网络脑补;
 *   2× 时输入=源(3840 出图)又被合成缩回屏幕宽(如 2100) —— 先降采样、再超分、再缩回。
 *   色度还走了一趟 RGB→CbCr→高分辨率重采样→逆变换回 RGB 的有损往返。
 *   现在: 超分恒定吃**源分辨率**亮度, 网络天生 2× 出图, 合成趟按显示尺寸一次定点缩放,
 *   色度直接取原画 → 同尺寸下只会更锐, 不会出现原画没有的丢失。
 * 【全屏】旧版 `syncCanvasBox` 用 `offsetWidth/offsetHeight`, 布局没量出来时退到父容器尺寸并
 *   **把不一致的框写进 style**; 而几何自愈靠"每 30 帧比 clientWidth/clientHeight, 变了就
 *   dispatch resize" → 又走一遍 `schedule()` → 500ms 后重建整条管线。进/出全屏抖动几次就是几轮
 *   重建 + 错框, 用户看到的"右边一条黑"和"卡一下"都来自这里。
 *   现在: 画布框只从**单一来源**(`getBoundingClientRect`, 即合成器真实绘制的框)推导, 位置由
 *   `mx/my` 表达(不依赖 offsetParent); 计划按"粗量化目标"比对, 轻微几何抖动不再触发重建;
 *   重建只发生在源/输入尺寸/策略三者之一真的变了的时候。
 * 【性能】旧版每次 `activate()` 都重新编译 12 趟 + 基础 program(驱动同步编译在主线程) —— 这是
 *   最响的性能黑洞。现在 program 按 (iw,ih) 缓存在 canvas 上, 进出全屏/换档不再重编; uniform
 *   位置一次性缓存(旧版每帧 50+ 次 getUniformLocation); 纹理/FBO 尺寸不变则复用; 拆除路径会
 *   真正 deleteTexture/deleteFramebuffer。另外删掉了自研 WebGPU「双三次+RCAS」那条路 ——
 *   它每次重建都新建 GPUDevice 且从不释放(泄漏), 而且合成画布已经承担同样的缩放职责。
 * 【失败必须看得见】旧版先建画布、先给 video 加 opacity:0, 之后任何 return(无 WebGL2 / 编译
 *   失败 / vw=0)都留下空白画布盖住原视频 = "开了超分更差"。现在画布出画那一刻才藏视频,
 *   激活不再有黑闪; 播放中 1.2s 没首帧才撤管线, 暂停中挂起而非拆除(暂停不会再触发
 *   "拆了建/建了拆"的闪烁循环), 原因写进 `window.__msxSrFail` / `window.__msxAcnetFail`。
 * ── 2026-10 第二轮(真机核显实测: 全屏越超越糊/闪烁/卡顿/回滚) ────────────────────────
 * 【画质】网络输入档位由"显示×倍率"一次算定(iw=clamp(显示/2×倍率, 320, 源宽), 16 对齐),
 *   固定使用, 不做任何自动升降。need 门槛加滞回(启动 1.08/退场 1.02), 进/出全屏的
 *   几何抖动不再让超分开↔关来回跳。
 * 【稳定】跳帧的帧用「最近网络输出 + 运动闸门」合成: 静止区域持续有网络细节(不再锐↔暗
 *   呼吸), 运动区域自动退回原画(不重影)。 iw/画布尺寸变化一律不拆管(只换 LRU 里的
 *   program/纹理); 被替换 <video> 的孤儿画布释放 WebGL 上下文。
 * ────────────────────────────────────────────────────────────────────────────
 */
(function () {
  "use strict";
  if (window.__MSX_SR__ !== 1) return;
  if (window.__MSX_FG__) return;   // 补帧已接管画面: 超分不再注入(双画布互相打架)
  /* 门控: 只在**播放器宿主**生效(www.bilibili.com / bilibili.com)。
     旧门控 /(^|\.)bilibili\.com$/ 把 space.bilibili.com(个人空间)也算了进来 ——
     个人空间没有 <video> 播放器, 却白挂一整套 MutationObserver + 900ms 轮询 +
     设置轮询(用户反馈"打开别人个人界面有概率卡很久")。 */
  if (location.hostname !== "www.bilibili.com" && location.hostname !== "bilibili.com") return;

  var lib = window["anime4k-webgpu"];
  var hasLib = !!(lib && lib.render && lib.CNNx2UL);
  var hasAcnet = function () {
    return window.__MSX_ACNET__ !== 0 && typeof window.__msxRunAcnet === "function";
  };
  /* 什么都没得用就别注入(MutationObserver / interval 也是开销) */
  if (!hasLib && !(window.__MSX_ACNET__ !== 0)) return;

  var LEVEL = window.__MSX_SR_LEVEL__ || "高";
  function curLevel() { return LEVEL; }
  /* 世代号: 每条渲染链都属于某一世代。拆除时 +1, 旧链醒来发现世代变了就自己退出。
     踩过的坑: 早先用 video.__msxSrKill 这个布尔开关, 新建管线在同一个同步 tick 里又把它置回
     false → 旧链继续跑, 每重建一次多留一条全速管线(实测 builds=4 时最轻档也丢帧 77~83%)。 */
  var GEN = 0;
  var VIDEO_SEL = "#bilibili-player video, .bpx-player-container video";
  var cur = null;

  /* ── 设置热更新 ────────────────────────────────────────────────────────────
     档位/倍率是 document-created 时注入的, 用户改完不重开标签页 → 页面里还是旧值,
     于是"换哪档都一样"。壳的 HTTP API 在本机可达, 所以这里轮询它: 变了就地更新。
     端口用壳注入的 __MSX_PORT__(壳按占用从 8000/8010/8020 里挑了一个, 猜是猜不中的);
     服务端对 B 站源回显 CORS 头(app.py send_json), 变了就地重建, 失败静默不影响播放。 */
  var SYNCED = 0;
  function startSettingSync() {
    if (SYNCED) return;
    SYNCED = 1;
    var last = String(window.__MSX_SR_LEVEL__ || "") + "/" + String(window.__MSX_SR_SCALE__ || "");
    var port = String(window.__MSX_PORT__ || "8000");
    setInterval(function () {
      try {
        /* 专用端点: 只含 UI 开关键(不含敏感项), 且服务端只对它回 CORS 头 */
        fetch("http://127.0.0.1:" + port + "/api/ui_settings",
              { cache: "no-store", targetAddressSpace: "loopback" })
          .then(function (r) { if (!r.ok) throw 0; return r.json(); })
          .then(function (s) {
            if (!s) return;
            if (s.ui_sr !== "1") { teardown("setting-off"); return; }
            var lvl = s.ui_sr_level || "高", sc = String(s.ui_sr_scale || "2");
            var now = lvl + "/" + sc;
            if (now !== last) {
              last = now;
              window.__MSX_SR_LEVEL__ = lvl;
              window.__MSX_SR_SCALE__ = sc;
              console.warn("[msx] SR 设置热更新 ->", now);
              schedule();
            }
          })
          .catch(function () {});
      } catch (e) {}
    }, 3000);
  }

  function cnn(level, d, t) {
    if (level === "轻") return new lib.CNNx2M({ device: d, inputTexture: t });
    if (level === "标准") return new lib.CNNx2VL({ device: d, inputTexture: t });
    return new lib.CNNx2UL({ device: d, inputTexture: t });
  }

  /* ── 几何 / 计划 ──────────────────────────────────────────────────────────
     框的唯一来源是 getBoundingClientRect: 它就是合成器真正绘制 <video> 的矩形, 也是画布要盖住的
     那个矩形。旧版为了躲 B 站全屏时的 CSS transform 改用 offsetWidth/offsetLeft, 结果在布局没量
     出来时把父容器的尺寸写进 style —— 那才是"右边一条黑"的来源。现在把 rect 相对**父容器**的
     偏移存进 geo, 由 CSS 的 left/top 表达, 父容器有 transform 时 CSS 与 vfc 会自动一起变换。 */
  function geometry(video) {
    try {
      if (!video.videoWidth || !video.videoHeight) return null;
      var r = video.getBoundingClientRect();
      if (!(r.width > 2) || !(r.height > 2)) return null;
      var par = video.parentElement;
      var pr = par ? par.getBoundingClientRect() : { left: 0, top: 0 };
      var dpr = window.devicePixelRatio || 1;
      /* 自检: 画布(CSS 尺寸 = r.width/height)要盖住的就是这个 rect; 相机(luma)与画布 1:1 时
         vfc 的 k 也等于 vw/r.width。 */
      return { rw: r.width, rh: r.height, mx: r.left - pr.left, my: r.top - pr.top,
               dw: Math.max(2, Math.round(r.width * dpr)),
               dh: Math.max(2, Math.round(r.height * dpr)), dpr: dpr };
    } catch (e) { return null; }
  }

  function quantize(geo) { return Math.round(geo.dw / 8) + "x" + Math.round(geo.dh / 8); }

  /* 计划 = 「超分吃什么」+「画多大」。粗量化: 只有源分辨率 / 策略种类变了才重建;
     网络输入档位(iw/ih)变了也不重建(只换 LRU 里的 program/纹理, 画布/视频/DOM 不动)。
     active: 已有管线在跑时传 true —— 让 need 的门槛带滞回(启动 1.08 / 退场 1.02)。 */
  function plan(video, active) {
    try {
      var geo = geometry(video);
      if (!geo) return null;
      var w = video.videoWidth | 0, h = video.videoHeight | 0;
      if (!w || !h) return null;
      var sig = w + "x" + h + "/" + quantize(geo);
      if (!geo.rw || geo.rw < 8) return null;
      if (!geo.dw || geo.dw < 16) return null;
      var need = geo.dw / w;
      if (window.__MSX_SR_FORCE_DISP__) need = (+window.__MSX_SR_FORCE_DISP__) / w;   // 测试: 模拟真全屏几何
      var lvl = curLevel();

      /* ACNet 路径(默认)。网络是**固定 2× 放大**的模型。网络输入档位在这里一次算定:
         iw = clamp(ceil(显示设备像素宽 / 2) × 倍率, 320, 源宽) 再对齐 16。 */
      var NET_MIN = 320;
      /* 倍率(1|1.25|1.5|2) = 允许网络看到多少源细节: 1× 只按屏幕需要处理(最省),
         2× 通常顶到源宽(最清晰、最吃显卡)。 */
      function netInput(dispW, srcW) {
        var lo = Math.min(srcW, Math.max(NET_MIN, Math.ceil(dispW / 2)));
        var sc = parseFloat(window.__MSX_SR_SCALE__ || "2") || 2;
        if (!(sc >= 1)) sc = 1;
        if (sc > 1) lo = Math.min(srcW, Math.round(lo * sc));
        var iw = Math.round(lo / 16) * 16;              // 对齐 16: 卷积对小尺寸更友好
        return Math.min(srcW, Math.max(NET_MIN, iw));
      }

      /* 显示已经不需要放大(need≈1)时, 任何"先缩再超分"的往返都只会变差 —— 浏览器 1:1 显示
         原画就是最优, 网络再花 4 倍算力把 1080p 缩到 960 再重建回 1920, 只会更软。
         滞回(Schmitt): 启动要 need>1.08, 已启动的要 need<1.02 才退场 —— 进/出全屏时几何
         抖动会让 need 在 1.05 附近来回跳, 没有滞回就是一遍遍"超分开↔关"的闪跳/回滚感。
         原因写进 __msxSrMode, 界面上看得见。 */
      var NO_GAIN = active ? 1.02 : 1.08;
      if (hasAcnet() && !window.__MSX_ACNET_DISABLE__) {
        if (!window.__MSX_SR_FORCE_SR__ && need <= NO_GAIN) {
          window.__msxSrMode = "skip:no-upscale(need" + (Math.round(need * 100) / 100) + "<=" + NO_GAIN + ")";
          return null;
        }
        var iw = netInput(geo.dw, w);
        var ih = Math.max(16, Math.round(iw * h / w));
        return { kind: "acnet", sig: sig, level: lvl, need: Math.round(need * 100) / 100,
                 w: w, h: h, iw: iw, ih: ih, dw: geo.dw, dh: geo.dh };
      }

      /* 回退路: Anime4K WebGPU 卷积超分(只在低清源上划算; 1080p 源实测丢帧 62% 以上) */
      if (hasLib) {
        if (w < 1280 && need > 1.15) {
          var gan = (lvl === "旗舰") && w <= 1280 && !!lib.GANUUL;
          return { kind: "cnn", sig: sig, level: lvl, gan: gan, need: Math.round(need * 100) / 100,
                   w: w, h: h, cw: w * 2, ch: h * 2, dw: geo.dw, dh: geo.dh };
        }
        window.__msxSrMode = "skip:" + (w >= 1280 ? "hdsrc" : "need") + w +
                             "/disp" + geo.dw + "/need" + Math.round(need * 100) / 100;
        return null;
      }
      window.__msxSrMode = "skip:no-path";
      return null;
    } catch (e) { return null; }
  }

  /* ── 弹幕必须在超分画布**之上** ───────────────────────────────────────────
     画布 z-index:0 与"z-index:auto 的定位元素"同组, 按 DOM 次序排在弹幕容器之前, 通常已经够;
     这里只对已知弹幕容器做自愈: 仅当它定位且层级不高于画布时抬升, 绝不降低播放器原有层级。
     超分管线只读 <video>(输入纹理来自 video frame), DOM 弹幕不会被卷进处理层。 */
  var DM_WRAPS = [
    ".bpx-player-dm-mask-wrap", ".bpx-player-row-dm-wrap", ".bpx-player-adv-dm-wrap",
    ".bpx-player-bas-dm-wrap", ".bpx-player-cmd-dm-wrap", ".bpx-player-render-dm-wrap",
    ".bas-danmaku", ".bili-danmaku-x-dm"
  ];

  function canvasZ(canvas) {
    var z = parseInt(getComputedStyle(canvas).zIndex, 10);
    return isNaN(z) ? 0 : z;
  }

  /* ── 图层自检(只在真发现异常时弹面板, 正常情况下用户什么都看不到) ─────────────
     用户现场报"全屏严重重影", 而离线单帧自相关查不出位移拷贝 —— 这类重影最可能来自**两层
     同时可见且几何不同**: ①没被藏住的 <video>; ②上一轮留下的、还在显示的旧超分画布(尺寸/位置
     与新的那块不同)。静态截图看不出来是因为两层内容几乎重合, 只有错开量在边缘才露出来。
     所以这里把"到底有几层、各自什么几何"直接量出来;
     window.__msxSrLayer 始终写着这次的结果, 有异常才额外弹一个可截图的面板。 */
  function layerProbe(video, canvas) {
    try {
      var all = document.querySelectorAll("canvas.msx-sr-canvas");
      var vis = [], i;
      for (i = 0; i < all.length; i++) {
        var cs = getComputedStyle(all[i]), r = all[i].getBoundingClientRect();
        if (cs.display !== "none" && cs.visibility !== "hidden" && r.width > 1) {
          vis.push(Math.round(r.left) + "," + Math.round(r.top) + " " +
                   Math.round(r.width) + "x" + Math.round(r.height) +
                   " buf" + all[i].width + "x" + all[i].height);
        }
      }
      var vc = getComputedStyle(video), vr = video.getBoundingClientRect();
      var cr = canvas.getBoundingClientRect();
      /* 画布被停滞自愈藏起来时 rect 全是 0, 拿它比几何只会误报一次"图层异常" */
      var shown = String(getComputedStyle(canvas).display) !== "none";
      var diff = shown
        ? Math.abs(vr.left - cr.left) + Math.abs(vr.top - cr.top) +
          Math.abs(vr.width - cr.width) + Math.abs(vr.height - cr.height)
        : 0;
      var info = { canvases: all.length, visibleCanvases: vis.length, list: vis,
                   videoOpacity: vc.opacity, videoVisibility: vc.visibility,
                   videoDisplay: vc.display, videoObjectFit: vc.objectFit,
                   videoFilter: String(vc.filter).slice(0, 60),
                   videoRect: [Math.round(vr.left), Math.round(vr.top),
                               Math.round(vr.width), Math.round(vr.height)],
                   canvasRect: [Math.round(cr.left), Math.round(cr.top),
                                Math.round(cr.width), Math.round(cr.height)],
                   rectDiffPx: Math.round(diff),
                   canvasBuf: canvas.width + "x" + canvas.height,
                   src: video.videoWidth + "x" + video.videoHeight,
                   dpr: window.devicePixelRatio || 1,
                   win: window.innerWidth + "x" + window.innerHeight,
                   mode: window.__msxSrMode || "" };
      window.__msxSrLayer = info;
      var bad = [];
      if (info.visibleCanvases > 1) bad.push("可见画布 " + info.visibleCanvases + " 块: " + vis.join(" / "));
      /* 2026-10(晚): 原视频**永久保持可见**(不再 opacity:0) —— 藏视频会让B站播放器
         截图(drawImage(video))间歇性黑帧。画布同框覆盖, 视频可见是预期状态, 无异常可报 */
      if (shown && diff > 3) bad.push("画布与视频框不重合 " + info.rectDiffPx + "px");
      if (bad.length) {
        _layerBadRun++;
        window.__msxSrBadRun = _layerBadRun;
        if (_layerBadRun >= 2) {
          showDiag("SR 图层异常: " + bad.join(" | ") +
            " || video[" + info.videoRect + "] canvas[" + info.canvasRect + "] fit=" +
            info.videoObjectFit + " dpr=" + info.dpr + " buf=" + info.canvasBuf +
            " src=" + info.src + " win=" + info.win);
        } else {
          /* 瞬时异常只进控制台。用户反馈: 超分"暂时性画布不对"就弹报错框, 没必要 */
          console.warn("[msx] SR 图层瞬时异常(不弹框):", bad.join(" | "));
        }
      } else {
        _layerBadRun = 0;
        hideDiag();
      }
      return info;
    } catch (e) { return null; }
  }

  /* 与 window.__msxSrLayer / window.__msxSrBadRun 同类的诊断出口: 让离线/真机探针能直接驱动
     一次图层自检, 验证"瞬时异常不弹框、连续 2 次才弹"(用户报修: 暂时性画布不对就弹报错框)。 */
  try { window.__msxSrProbe = layerProbe; } catch (e) {}

  /* 暂时性异常不弹框(用户反馈"没必要"): 自检只在激活/全屏切换后点 2~3 次(300/1500/4000ms),
     而这段时间正是布局尚未落定、画布与视频框短暂错位的时候。改成**连续 2 次自检都异常**
     (即跨过 1.5s 依然不对) 才显示面板; 一旦恢复干净立即撤掉面板。 */
  var _layerBadRun = 0;

  function hideDiag() {
    try {
      var d = document.getElementById("msx-sr-diag");
      if (d && d.parentNode) d.parentNode.removeChild(d);
    } catch (e) {}
  }

  function showDiag(msg) {
    try {
      var d = document.getElementById("msx-sr-diag");
      if (!d) {
        d = document.createElement("div");
        d.id = "msx-sr-diag";
        d.style.cssText = "position:fixed!important;left:6px!important;top:44px!important;" +
          "z-index:2147483000!important;max-width:92vw!important;padding:6px 8px!important;" +
          "background:rgba(0,0,0,.86)!important;color:#ffd24a!important;" +
          "font:12px/1.45 Consolas,monospace!important;white-space:pre-wrap!important;" +
          "pointer-events:none!important;border:1px solid #ffd24a!important";
        (document.body || document.documentElement).appendChild(d);
      }
      d.textContent = "[洞鉴虚明 诊断] " + msg;
    } catch (e) {}
  }

  function raiseDanmaku(canvas) {
    try {
      if (!canvas || !canvas.isConnected) return;
      var z0 = canvasZ(canvas);
      for (var i = 0; i < DM_WRAPS.length; i++) {
        var els = document.querySelectorAll(DM_WRAPS[i]);
        for (var j = 0; j < els.length; j++) {
          var el = els[j], cs = getComputedStyle(el);
          if (cs.position === "static") continue;
          var z = parseInt(cs.zIndex, 10);
          if (isNaN(z) || z <= z0) {
            try { el.style.zIndex = String(z0 + 2); } catch (e) {}
          }
        }
      }
    } catch (e) {}
  }

  /* ── 画布 ────────────────────────────────────────────────────────────────
     每个 <video> 一张画布, 建一次就不再销毁: teardown 只是"停渲染 + 撤样式", 画布留在 DOM 里,
     下次 activate 直接复用(连 WebGL 上下文和已编译的 program 都还在)。 */
  function ensureCanvas(video) {
    var c = video.__msxSrCanvas;
    if (c && c.isConnected && c.parentElement === video.parentElement) {
      if (c.__msxAcnetWorkerDead) {
        /* OffscreenCanvas 随崩溃的 worker 一起丢了 → 换一块全新画布(旧的就地丢弃) */
        try { c.remove(); } catch (e) {}
        video.__msxSrCanvas = null;
      } else {
        if (c.style.display !== "block") c.style.display = "block";
        return c;
      }
    }
    c = document.createElement("canvas");
    c.className = "msx-sr-canvas";
    video.insertAdjacentElement("afterend", c);
    video.__msxSrCanvas = c;
    return c;
  }

  function ensureStyle() {
    if (document.getElementById("msx-sr-style")) return;
    var st = document.createElement("style");
    st.id = "msx-sr-style";
    st.textContent =
      "canvas.msx-sr-canvas{position:absolute!important;pointer-events:none!important;" +
      "z-index:0!important;opacity:0.999!important;filter:brightness(1)!important}" +
      /* ⚠️ opacity:0.999 + filter:brightness(1)(视觉无感)是故意的: 不加, 超分画布 =
         不透明+轴对齐+无滤镜的简单四边形, 会被 Chromium 提升为 DirectComposition 独立
         平面; overlay 平面屏幕上可见但 GDI 系截屏(微信/QQ/PrntScr 等 BitBlt 屏幕DC)取不到
         → 播放器区域黑屏。非不透明 + 恒等滤镜强制走渲染通道, 画布留在主合成面里。
         ⚠️ 旧方案是 border-radius:0.5px —— 在 Chromium 154 上**已失效**(该内核支持带圆角的
         overlay, msedge.dll 内 delegated.overlay.candidate_rounded_corners 即证), 不要再改回去。
         引擎级 --disable-direct-composition 能清零黑帧但整条呈现路径退回传统合成, 弹幕场景
         "特别特别卡", 已弃用。 */
      /* 不写 display: 未激活时要能被内联 display:none 藏起来 */
    (document.head || document.documentElement).appendChild(st);
  }

  /* 真正释放一块画布的 WebGL 上下文(WEBGL_lose_context)。
     只对"确定不会再复用"的画布调用(别的 <video> 留下的孤儿) —— 上下文是稀缺资源,
     Chromium 有活跃 WebGL 上下文上限, 泄漏的后果是"用着用着超分忽然没了"。 */
  function releaseGL(c) {
    try {
      if (c.__msxAcnetWorker) { c.__msxAcnetWorker.terminate(); c.__msxAcnetWorker = null; }
      var gl = c.__msxAcnetGL;
      if (gl && gl.getExtension) {
        var le = gl.getExtension("WEBGL_lose_context");
        if (le) le.loseContext();
      }
      c.__msxAcnetGL = null;
      c.__msxAcnetLRU = null;
    } catch (e) {}
  }

  function syncCanvasBox(video, geo) {
    try {
      var c = video.__msxSrCanvas;
      if (!c) return;
      /* ⚠️ 被"停滞自愈"藏起来的画布(__msxSrHold), 只有**真的画出新帧**才许放回来。
         这里过去是无条件 display:block, 而 reconcile 每 900ms 就会调到它 —— 于是看门狗
         刚藏掉的、冻在最后一帧的画布, 1 秒内又盖回正在播放的原视频上。用户看到的就是
         "全屏后画面卡住不动、声音还在放; 退出全屏才好"(退出全屏 need 掉到门槛下 →
         超分退场 → 露出原视频)。 */
      if (c.style.display !== "block" && !c.__msxSrHold) c.style.display = "block";
      var rw = Math.max(2, geo.rw), rh = Math.max(2, geo.rh);
      var left = geo.mx + "px", top = geo.my + "px";
      var wid = rw + "px", hei = rh + "px";
      if (c.style.left !== left) c.style.left = left;
      if (c.style.top !== top) c.style.top = top;
      if (c.style.width !== wid) c.style.width = wid;
      if (c.style.height !== hei) c.style.height = hei;
      if (c.__msxAcnetTransferred) {
        /* 画布已 transferControlToOffscreen(worker 路): 尺寸归 worker 管,
           主线程只发消息(直接设 width/height 会与 offscreen 冲突) */
        try { c.__msxAcnetPost && c.__msxAcnetPost({ cmd: "size", w: geo.dw, h: geo.dh }); } catch (e2) {}
      } else if (c.width !== geo.dw || c.height !== geo.dh) {
        c.width = geo.dw;                 // 变尺寸 → 画布清空, 下一帧会整幅重画
        c.height = geo.dh;
      }
    } catch (e) {}
  }

  /* ── 拆除 ──────────────────────────────────────────────────────────────── */
  function teardown(reason) {
    try { window.__msxSrTeardown = reason || "?"; } catch (e) {}
    _layerBadRun = 0; hideDiag();   // 拆管即清掉自检状态与诊断面板
    if (!cur) { try { window.__msxSrMode = "off"; } catch (e) {} return; }
    var v = cur.video;
    GEN++;                                // 旧 rVFC 链醒来即退出
    try { v.__msxSrGen = GEN; } catch (e) {}
    try { if (cur.stop) cur.stop(); } catch (e) {}
    /* 管线停滞/上下文丢失的拆管: worker 侧状态不可信(喂帧链可能已坏), 把已转移的
       画布标记为死 → ensureCanvas 换新画布 → 下次激活用全新 worker 重建,
       避免"冻住 → 重建 → 还是冻住"的死循环(全屏音画分离的恢复路径) */
    try {
      if (cur.canvas && cur.canvas.__msxAcnetTransferred &&
          String(reason || "").indexOf("stall") >= 0) {
        cur.canvas.__msxAcnetWorkerDead = 1;
        if (cur.canvas.__msxAcnetWorker) { cur.canvas.__msxAcnetWorker.terminate(); }
        cur.canvas.__msxAcnetWorker = null;
      }
    } catch (e) {}
    try { cur.ro.disconnect(); } catch (e) {}
    /* <video> 已经不在文档里(被播放器替换/页面跳转) → 这块画布不可能再复用,
       把它的 WebGL 上下文一并释放(泄漏的上下文会吃掉浏览器的活跃上下文配额) */
    try { if (!v.isConnected) releaseGL(cur.canvas); } catch (e) {}
    /* 画布**保留**在 DOM 里(只是 display:none), 不 remove —— WebGL 上下文与已编译的 12 趟
       program 都挂在 canvas 上, remove 掉就全丢了, 下次激活又要在主线程同步重编一遍。
       用 display:none 而不是留着: 留着的画布会把最后一帧冻在视频上面盖住原视频。 */
    try {
      cur.canvas.style.display = "none";
      v.__msxSrCanvas = cur.canvas;
    } catch (e) {}
    try { v.classList.remove("msx-sr-hide"); } catch (e) {}
    try { v.style.removeProperty("opacity"); } catch (e) {}   // 兼容旧版残留(现已不再藏视频)
    /* 内联 important 必须撤掉, 否则视频永远看不见(退化成黑屏) */
    try { v.style.removeProperty("opacity"); } catch (e) {}
    /* 同一 <video> 名下若还留着别的画布(换过容器/换过父节点), 一并藏掉 —— 见 activate 里的说明 */
    try {
      var others = document.querySelectorAll("canvas.msx-sr-canvas");
      for (var i = 0; i < others.length; i++) others[i].style.display = "none";
    } catch (e) {}
    cur = null;
    try { window.__msxSrMode = "off"; } catch (e) {}
  }

  /* ── 激活 ──────────────────────────────────────────────────────────────── */
  function activate(video, p) {
    if (cur && cur.video === video) teardown("replan");   // 复用同一个 video 时只停渲染
    if (!video || !video.parentElement) return;
    if (!video.videoWidth) {   // 元数据未就绪: 等 loadedmetadata 再排一次
      video.addEventListener("loadedmetadata", schedule, { once: true });
      return;
    }
    ensureStyle();
    var geo = geometry(video);
    if (!geo) return;
    var canvas = ensureCanvas(video);
    syncCanvasBox(video, geo);
    /* 重新激活时画布上还留着上一次的最后一帧; 首帧出来前先藏起来, 否则它会盖着
       原视频"冻住"(用户看到的"全屏后画面卡住不动")。首帧到达时放回来。
       ⚠️ 必须同时立 hold: 否则 900ms 的 reconcile → syncCanvasBox 会把这块残帧又显示
       出来, 而冷启动(worker 里编译 16 趟 program)最长 3s, 这段时间用户看到的就是冻屏。 */
    try {
      if (canvas.__msxSrPainted) {
        canvas.__msxSrHold = 1;
        canvas.__msxSrHoldAt = performance.now();
        canvas.style.display = "none";
      }
    } catch (e) {}

    /* patch video.requestVideoFrameCallback: 让每条链带上"注册时的世代", 换代即自退 */
    if (!video.__msxSrPatched) {
      video.__msxSrPatched = true;
      var orig = video.requestVideoFrameCallback.bind(video);
      video.requestVideoFrameCallback = function (cb) {
        var self = this, args = arguments;
        var g = video.__msxSrGen;
        return orig(function () {
          if (g !== video.__msxSrGen) return;
          try { window.__msxSrLastFrame = performance.now(); } catch (e) {}
          return cb.apply(self, args);
        });
      };
    }
    video.__msxSrGen = ++GEN;
    /* ── 结构性保证: 任何时刻只允许**一块**超分画布可见 ──────────────────────────
       历史 bug: teardown 只把"当前 cur 记录的那一块"画布 display:none。只要中途换过 <video>
       或换过容器(进/出全屏时 B 站会挪 DOM), `video.__msxSrCanvas` 就会指向新画布, 而**旧的
       那块还留在原来的父节点里显示着** —— 两块画布各自按当时的几何绘制同一帧, 尺寸/位置不同,
       叠在一起就是用户报的"全屏严重重影"。所以每次激活都全量清理其它画布; 被清掉的画布若是
       别的 <video> 留下的, 它的 WebGL 上下文也一并释放(WEBGL_lose_context) —— 否则每换一次
       视频元素就常驻一个 12-program 的上下文, 攒到浏览器上限后新建上下文直接失败,
       表现就是"用一会儿超分忽然没了"。 */
    try {
      var others = document.querySelectorAll("canvas.msx-sr-canvas");
      for (var oi = 0; oi < others.length; oi++) {
        if (others[oi] !== canvas) {
          others[oi].style.display = "none";
          var op = others[oi].parentElement;
          if (op && op !== video.parentElement) {
            releaseGL(others[oi]);
            try { others[oi].remove(); } catch (e2) {}
          }
        }
      }
    } catch (e) {}
    /* ⚠️ 这里**不**再立刻把原视频藏掉(旧版 opacity:0 先行):
       旧版先藏后画, 一旦首帧迟迟出不来(核显全屏第一帧可能 >1s)或管线启动失败, 用户看到的
       就是"开了超分黑一下/闪一下"。现在画布本来就盖在视频正上方(syncCanvasBox 保证同框),
       首帧画出前让原视频继续可见 —— 画布出画的那一刻才藏(见 __msxSrFirstFrame),
       "激活"从 黑一下→出画 变成 无缝覆盖。藏视频的时机只剩一个原则: 画布真的有画面了。 */

    var ro = null;
    if (window.ResizeObserver) {
      try {
        ro = new ResizeObserver(function () { schedule(); });
        ro.observe(video);
      } catch (e) {}
    }

    window.__msxSrBuilds = (window.__msxSrBuilds || 0) + 1;
    window.__msxSrMode = p.kind + "/" + p.level + "/" + (window.__MSX_SR_SCALE__ || "2") + "x" +
                         (p.gan ? "+GAN" : "") + "/in" + p.w + "x" + p.h +
                         (p.kind === "acnet" ? "(net" + p.iw + "x" + p.ih + ")" :
                          p.kind === "cnn" ? "->out" + p.cw + "x" + p.ch : "") +
                         "/canvas" + geo.dw + "x" + geo.dh +
                         "/need" + p.need;
    window.__msxSrFail = "";
    window.__msxSrFrames = 0;
    try { window.__msxAcnetFail = null; } catch (e) {}
    var built = Date.now();
    cur = { video: video, canvas: canvas, plan: p, ro: ro, stop: null, built: built };
    raiseDanmaku(canvas);
    /* WebGL 上下文丢失(GPU 被压垮/驱动重置)→ 立即拆管; reconcile 会在下个周期
       自动重建(新 canvas 上下文)。不处理的话丢失后 GL 调用静默无效, 画布冻结,
       表现就是"超分时好时坏/忽然没了"。 */
    canvas.__msxAcnetOnLost = null;

    /* 首帧真的画出来了 → 记录。⚠️ 不再隐藏原视频(用户反馈: 超分下整屏截屏视频区域黑屏):
       画布 + 被藏的原视频都不进截屏时, 截到的就是黑色播放器背景。原视频保持可见
       对画质零影响(画布同框覆盖), 并作为截屏兜底 —— 至少截到原画面而非黑屏。 */
    window.__msxSrFirstFrame = function (c) {
      if (!cur || cur.canvas !== c) return;
      cur.firstFrame = performance.now();
      window.__msxSrFirst = Math.round(cur.firstFrame - built);
      c.__msxAcnetLastPaint = performance.now();
      c.__msxSrPainted = 1;
      c.__msxSrHold = 0;                    // 真的有画面了: 才允许重新显示
      try { c.style.display = "block"; } catch (e) {}
    };

    if (p.kind === "acnet") {
      var stop = null;
      try {
        stop = window.__msxRunAcnet(video, canvas, video.__msxSrGen,
                                    {iw: p.iw, ih: p.ih, vw: p.w, vh: p.h, level: p.level});
      } catch (e) {
        console.warn("[msx] acnet 启动失败:", e);
        window.__msxAcnetFail = "throw: " + e;
      }
      if (!stop || !cur) {
        var why = window.__msxAcnetFail || "start-failed";
        console.warn("[msx] ACNet 未能启动, 恢复原视频:", why);
        teardown("acnet:" + why);
        return;
      }
      cur.stop = stop;
      /* 激活时视频正暂停: rVFC 永远不来 → 主动同步画一帧当前画面
         (画布立即有内容、视频立即被藏, 不用等 fail-safe) */
      if (video.paused) {
        try { if (canvas.__msxAcnetOnce) canvas.__msxAcnetOnce(); } catch (e) {}
      }
    } else if (p.kind === "cnn") {
      /* CNN 回退路(lib.render)没有"首帧已画出"的信号 —— 同 acnet: 不藏原视频,
         画布就绪出画即覆盖(见 __msxSrFirstFrame 注释), 截图黑屏问题一并规避。 */
      var cancelled = false;
      cur.stop = function () { cancelled = true; };
      lib.render({
        video: video, canvas: canvas,
        pipelineBuilder: function (device, inputTexture) {
          var stages = [];
          var up = cnn(p.level, device, inputTexture);
          stages.push(up);
          if (p.gan) stages.push(new lib.GANUUL({ device: device, inputTexture: up.getOutputTexture() }));
          return stages;
        },
      }).then(function () { if (cancelled) teardown("cnn-cancel"); })
        .catch(function (e) {
          console.warn("[msx] SR 管线失败:", e);
          teardown("cnn:" + e);
        });
    } else {
      teardown("unknown-kind");
      return;
    }

    /* ── fail-safe(进度制): 只有"管线真的停了"才拆管 ────────────────────────────
       固定 1.2s 超时是"时好时坏"的机制之一: 高负载下首帧可能 >1.2s(编译+首轮网络),
       拆掉 → reconcile 重建 → 再拆…… 循环。现在只看**画布帧计数是否前进**:
       有进度(哪怕还没凑齐首帧标记)就继续等; 停滞/上下文丢失/暂停以外无进度才拆。 */
    if (p.kind === "acnet") {
      try { canvas.__msxAcnetCtxLost = 0; } catch (e) {}
      var built0 = built, stalls = 0;
      /* ⚠️ 判"画布还在更新"必须用**真的画上了**的时间戳(worker 的 drained.ok),
         不能用 __msxAcnetFrames —— 后者每 30 帧才回报一次, 慢 GPU 上会把健康的
         低帧率误判成停滞。 (用户报修: 全屏有概率卡死在一个画面, 声音还在放。) */
      var watchdog = setInterval(function () {
        if (!cur || cur.canvas !== canvas || cur.built !== built0) {
          clearInterval(watchdog); return;         // 已被更替: 本看门狗退役
        }
        if (document.hidden) return;
        var err = String(window.__msxAcnetFail || "");
        if (canvas.__msxAcnetCtxLost || err.indexOf("context-lost") >= 0 ||
            err === "worker-error") {
          clearInterval(watchdog);
          window.__msxSrFail = err || "webgl-context-lost";
          teardown("stall:" + window.__msxSrFail); // reconcile 随后自动重建(新上下文)
          return;
        }
        if (video.paused) { stalls = 0; return; }  // 暂停: 无帧是正常的
        /* 首次装配(worker 里编译 16 趟 program + 分配上百 MB 渲染目标)可能超过 1.8s,
           这段时间本来就没帧, 不是"停了"。给 3s 冷启动豁免, 否则会把它判成停滞、
           拆管重建, 又从头编译一遍(用户体感: 进全屏卡半天)。 */
        var since = Math.max(canvas.__msxAcnetLastPaint || 0, built0 + 3000);
        var age = performance.now() - since;
        if (age > 1800) {
          /* 画布停在最后一帧而视频还在走: 先把画布藏掉 —— 底层原视频立刻可见,
             用户不会继续盯着一张冻住的画面; 下一拍还停就拆管重建。
             ⚠️ hold 必须一起立: 900ms 的 reconcile 会调 syncCanvasBox, 不立 hold 它
             1 秒内就把冻住的这一帧又盖回去(藏了等于没藏)。 */
          try {
            if (!canvas.__msxSrHold) canvas.__msxSrHoldAt = performance.now();
            canvas.__msxSrHold = 1;
            canvas.style.display = "none";
          } catch (e) {}
          /* 补喂一帧: 背压闸门若卡在 inFlight(回执丢了), 只有主动投一帧能解开 */
          try { canvas.__msxAcnetOnce && canvas.__msxAcnetOnce(); } catch (e) {}
          if (++stalls >= 2) {
            clearInterval(watchdog);
            window.__msxSrFail = (window.__msxAcnetFail || "no-progress") +
                                 " paintAge=" + Math.round(age);
            console.warn("[msx] 超分画布停滞, 拆管重建:", window.__msxSrFail);
            /* 30s 内反复停滞 = 不是偶发(多半 GPU 吃不下): 冷却 8s 再试,
               避免"重建→立刻又冻"的高频重编把画面拖得更卡。 */
            var nowT = Date.now(), prevT = window.__msxSrStallAt || 0;
            window.__msxSrStallCount = (nowT - prevT < 30000)
              ? (window.__msxSrStallCount || 0) + 1 : 1;
            window.__msxSrStallAt = nowT;
            window.__msxSrCooldownUntil = (window.__msxSrStallCount >= 2)
              ? nowT + 8000 : 0;
            teardown("stall:" + window.__msxSrFail);
          }
          return;
        }
        stalls = 0;
        if (canvas.__msxSrHold) {
          /* hold 只由"藏起来之后新画出来的帧"解除: 时间戳必须晚于 holdAt。 */
          if ((canvas.__msxAcnetLastPaint || 0) > (canvas.__msxSrHoldAt || 0) + 100) {
            canvas.__msxSrHold = 0;
            try { canvas.style.display = "block"; } catch (e) {}   // 恢复了: 把画布放回来
          }
        }
      }, 1000);
    }

    /* 图层自检: 刚激活、以及全屏切换后各查几次(几何要等布局落定) —— 发现异常才弹面板 */
    var probe1 = function () { if (cur && cur.canvas === canvas) layerProbe(video, canvas); };
    setTimeout(probe1, 300);
    setTimeout(probe1, 1500);
    setTimeout(probe1, 4000);
  }

  /* ── 调度 ────────────────────────────────────────────────────────────────
     事件(全屏/尺寸/可见性/DOM 变动)只做去抖; 另有 900ms 兜底轮询, 覆盖"播放器悄悄换了
     <video> 元素"这类没有事件的场景。B 站页面弹幕/评论每秒几十次 DOM 变动, 所以 MutationObserver
     只当"可能有变化"的信号, 真正的判定始终是便宜的 plan() 比对。 */
  var pending = 0;
  function reconcile() {
    try {
      if (document.hidden) { if (cur) teardown("hidden"); return; }
      var v = document.querySelector(VIDEO_SEL);
      if (!v) { if (cur) teardown("no-video"); return; }
      if (cur && (!cur.video.isConnected || cur.video !== v)) teardown("video-changed");
      if (window.__msxSrCooldownUntil && Date.now() < window.__msxSrCooldownUntil) {
        if (cur) teardown("stall-cooldown");
        return;
      }
      var p = plan(v, !!cur);
      if (!p) { if (cur) teardown("no-plan"); return; }
      if (cur && cur.video === v) {
        /* 源/策略没变(画布框、网络输入档位怎么变都一样): 只自愈几何(便宜), 绝不重建。
           iw 变化只换 LRU 里的 program/纹理(见 acnet-webgl.js), 不拆管线 ——
           旧版把 iw 变化当重建理由(还有个 4s 冷却), 进/出全屏一次就重编 12 趟 program
           (主线程冻结几百 ms), 几何一抖再抖就来回重建 = 用户看到的"卡一下/回滚"。 */
        var stable = cur.plan.kind === p.kind && cur.plan.level === p.level &&
                     cur.plan.gan === p.gan && cur.plan.w === p.w && cur.plan.h === p.h;
        if (stable) {
          var geo = geometry(v);
          if (geo) syncCanvasBox(v, geo);
          cur.plan = p;
          window.__msxSrMode = p.kind + "/" + p.level + "/" + (window.__MSX_SR_SCALE__ || "2") + "x" +
                               (p.gan ? "+GAN" : "") + "/in" + p.w + "x" + p.h +
                               "(net" + p.iw + "x" + p.ih + ")" +
                               "/canvas" + p.dw + "x" + p.dh + "/need" + p.need;
          return;
        }
      }
      activate(v, p);
    } catch (e) {
      try { window.__msxSrFail = "reconcile: " + e; } catch (e2) {}
    }
  }
  function schedule() {
    if (pending) return;
    pending = setTimeout(function () { pending = 0; try { reconcile(); } catch (e) {} }, 250);
  }

  function startWatch() {
    try {
      new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
    ["fullscreenchange", "webkitfullscreenchange"].forEach(function (ev) {
      document.addEventListener(ev, function () {
        schedule();
        setTimeout(function () { if (cur) raiseDanmaku(cur.canvas); }, 300);
        setTimeout(function () { if (cur) raiseDanmaku(cur.canvas); }, 900);
        /* 全屏切换是重影最容易出现的时刻(容器被挪、几何被改写): 切换后多点几次自检 */
        [400, 1200, 3000].forEach(function (t) {
          setTimeout(function () { if (cur) layerProbe(cur.video, cur.canvas); }, t);
        });
      }, true);
    });
    document.addEventListener("visibilitychange", schedule);
    window.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);
    setInterval(reconcile, 900);          // 兜底: 大小/播放器替换没有事件时也能跟上
    startSettingSync();
    reconcile();
  }

  if (document.body) {
    startWatch();
  } else {
    document.addEventListener("DOMContentLoaded", startWatch, { once: true });
  }
})();
