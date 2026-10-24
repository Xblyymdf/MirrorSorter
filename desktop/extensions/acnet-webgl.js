/* 洞鉴虚明 · ACNet(Anime4KCPP f8b4) 在 WebGL2 里的多趟运行器
 *
 * 上游: TianZerL/ACNetGLSL (MIT, Copyright (c) 2020 TianZer), 文件 _acnet/acnet_f8b4.glsl
 *   12 趟 mpv glsl 管线: head conv(2) → body block1..4(8, TMP1/TMP2 乒乓) → upscale conv(1)
 *   → upscale pixelshuff(1, 唯一带 //!WIDTH LUMA.w 2 * 的趟, 输出 2× )。
 *
 * ── 2026-10 架构(第三轮, 针对"严重掉帧/时好时坏") ──────────────────────────────
 * A) **管线搬进 Worker**(OffscreenCanvas + WebGL2 + VideoFrame 零拷贝):
 *    4K 视频不丢帧是因为硬解码走独立固定功能单元; 而超分网络全源宽一次 ~82ms
 *    (iw=1920 核显实测) —— 在主线程上跑, 每 3 帧把整个渲染进程冻结 80ms:
 *    rAF 从 165 掉到 14、视频帧提交停滞(基线实测: 同页无超分 165fps/0 丢帧)。
 *    现在: 主线程每帧只做 `new VideoFrame(video)` + postMessage(所有权转移, 零拷贝,
 *    ~0.3ms), 上传/网络/合成全部在 worker 线程执行; 主线程不再有任何超分阻塞。
 *    canvas.transferControlToOffscreen 每元素只能调一次 → worker 与 OffscreenCanvas
 *    按 canvas 常驻(已编译 program/纹理跨激活复用), 换代只发 params/stop 消息。
 * B) **分片流水线**: 网络每帧只跑 K 趟(单片按 GPU 计时实测的"单趟真实耗时"控制在
 *    tickBudget≈8ms 内; 注意 draw 调用的 JS 墙钟是异步提交, ~0.5ms, 不能作为依据 ——
 *    必须用 EXT_disjoint_timer_query), 12 趟跨若干帧完成, 完成即自动开下一轮。
 *    快 GPU 单帧跑完整网络; 慢 GPU 自然摊薄 —— 静止区细节刷新变慢, 但画面帧率永不掉。
 *    网络整条链只读周期开始时冻结的输入快照(T.SNAP_RUN), 中途 T.LUMA 被当帧重写
 *    也不影响分片一致性。
 * C) **运动闸门 + 零偏置注入 + CAS 自适应锐化**(合成趟, 每帧都跑):
 *    合成 = cs(CAS/RCAS 式对比度自适应锐化, 档位 0~0.70) × gain;
 *    gain = 1 + (yNew − blur4(yNew))/yRef × conf × w —— 只注入网络输出的高频细节
 *    (低频亮度偏置被高通滤除, 任何内容都不会"开超分更暗"); conf 是梯度比置信度闸门
 *    (结构保证"不会比不超分更糊"); w 是运动闸门(当帧亮度 vs SNAP_OUT 逐像素比,
 *    静止区域 w=1 吃满网络细节, 运动区域按**下限 MF**(默认 0.28)弱注入 ——
 *    动态物体也有超分, 只是权重低; 快动作不重影。MF=0 回到"运动区不超"的旧行为,
 *    窗口里设 MSX_ACNET_MF 可覆盖)。
 * D) 上下文丢失自愈: GPU 被压垮/驱动重置后 GL 调用静默无效("超分忽然没了") ——
 *    webglcontextlost 登记 → kick 自停 → 上层看门狗(播放中帧计数停滞)拆管重建。
 * 前置: window.__MSX_ACNET__ = 1 且 window.__MSX_ACNET_GLSL__ = 上游 glsl 文本(由壳注入) */
(function () {
  "use strict";
  if (window.__MSX_ACNET__ !== 1) return;
  var SRC = window.__MSX_ACNET_GLSL__ || "";
  if (!SRC || !window.WebGL2RenderingContext) return;
  if (window.__msxAcnetReady) return;
  window.__msxAcnetReady = 1;

  /* ═══════════ 管线核心(主线程回退路与 Worker 跑同一份代码) ═══════════
     本函数完全自包含(以 toString() 进入 worker blob), 不引用闭包外的东西。
     env: {glsl, canvas, preserve, oldReuse, debugSrc, maxPass, tickBudget,
           onFirst(), onStats(obj), onFail(msg)}
     对外: params(p)/reset(gen)/resize(w,h)/kick(src,meta)/stop() */
  function __msxAcnetCore(env) {
    "use strict";
    var SRC = env.glsl || "";
    var canvas = env.canvas;

    /* ---------- 1) 拆趟 + 读指令(mpv user-shader 格式) ---------- */
    function parse(src) {
      var chunks = src.split(/\/\/!DESC\s*/).slice(1);
      return chunks.map(function (c) {
        var nl = c.indexOf("\n"), d = { desc: c.slice(0, nl), when: "", components: 4,
          hook: "LUMA", save: "", binds: [], width: null, height: null };
        var lines = c.slice(nl + 1).split("\n"), i = 0;
        while (i < lines.length && lines[i].indexOf("//!") === 0) {
          var s = lines[i].slice(3).trim(), m;
          if ((m = /^WHEN\s+(.+)$/.exec(s))) d.when = m[1];
          else if ((m = /^COMPONENTS\s+(\d+)/.exec(s))) d.components = +m[1];
          else if ((m = /^HOOK\s+(\S+)/.exec(s))) d.hook = m[1];
          else if ((m = /^SAVE\s+(\S+)/.exec(s))) d.save = m[1];
          else if ((m = /^BIND\s+(\S+)/.exec(s))) d.binds.push(m[1]);
          else if ((m = /^WIDTH\s+(\S+)\s*(\S*)/.exec(s))) d.width = [m[1], m[2]];
          else if ((m = /^HEIGHT\s+(\S+)\s*(\S*)/.exec(s))) d.height = [m[1], m[2]];
          i++;
        }
        d.body = lines.slice(i).join("\n");
        return d;
      });
    }
    var PASSES = parse(SRC);
    if (!PASSES.length) { env.onFail && env.onFail("no-passes"); return null; }

    /* ---------- 2) GL 基础 ---------- */
    var VERT = "#version 300 es\n" +
      "const vec2 P[4] = vec2[4](vec2(-1.0,-1.0),vec2(1.0,-1.0),vec2(-1.0,1.0),vec2(1.0,1.0));\n" +
      "void main(){ gl_Position = vec4(P[gl_VertexID], 0.0, 1.0); }\n";

    function compile(gl, type, src, tag) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        env.onFail && env.onFail("compile " + tag + ": " + String(gl.getShaderInfoLog(s)).slice(0, 200));
        return null;
      }
      return s;
    }

    function program(gl, frag, tag) {
      var vs = compile(gl, gl.VERTEX_SHADER, VERT, tag + "-vs");
      var fs = compile(gl, gl.FRAGMENT_SHADER, frag, tag + "-fs");
      if (!vs || !fs) return null;
      var p = gl.createProgram();
      gl.attachShader(p, vs);
      gl.attachShader(p, fs);
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
        env.onFail && env.onFail("link " + tag + ": " + String(gl.getProgramInfoLog(p)).slice(0, 200));
        return null;
      }
      return p;
    }

    /* mpv glsl 宏环境: X_pos = fragCoord/目标尺寸 × 纹理缩放 */
    function prelude(d, texSizes, targetSize) {
      var names = d.binds.slice();
      if (names.indexOf("LUMA") < 0) names.push("LUMA");
      var out = "#version 300 es\nprecision highp float;\nprecision highp sampler2D;\n";
      names.forEach(function (n) { out += "uniform sampler2D " + n + "_smp;\n"; });
      names.forEach(function (n) {
        var S = texSizes[n] || texSizes.LUMA;
        var scale = S[0] / targetSize[0];
        out += "uniform vec2 " + n + "_size;\n";
        out += "uniform vec2 " + n + "_pt;\n";
        out += "#define " + n + "_mul vec4(1.0)\n";
        out += "#define " + n + "_pos (gl_FragCoord.xy * " + n + "_pt * " + scale.toFixed(6) + ")\n";
        out += "#define " + n + "_tex(c) texture(" + n + "_smp, (c))\n";
        out += "#define " + n + "_texOff(o) texture(" + n + "_smp, " + n + "_pos + " + n + "_pt * (o))\n";
      });
      out += "out vec4 msx_out;\n";
      return out;
    }

    var LUMA_FRAG =
      "#version 300 es\nprecision highp float;\nuniform vec2 u_outSize;\n" +
      "uniform sampler2D vid_smp;\n" +
      "out vec4 o;\n" +
      "void main(){ vec2 uv = gl_FragCoord.xy / u_outSize;" +
      "  vec3 c = texture(vid_smp, uv).rgb;" +
      "  float y = dot(c, vec3(0.2126, 0.7152, 0.0722));\n" +
      "  o = vec4(y, 0.0, 0.0, 1.0); }\n";

    /* ── 档位 → 合成强度(sharp = CAS/RCAS 式自适应锐化强度) ── */
    var LEVELS = {
      "轻":   { lo: 0.85, hi: 1.18, cut: 1.05, span: 0.30, floor: "0.02", sharp: "0.20" },
      "标准": { lo: 0.82, hi: 1.28, cut: 0.95, span: 0.35, floor: "0.02", sharp: "0.35" },
      "高":   { lo: 0.75, hi: 1.45, cut: 0.70, span: 0.40, floor: "0.02", sharp: "0.50" },
      "旗舰": { lo: 0.60, hi: 1.75, cut: 0.30, span: 0.50, floor: "0.01", sharp: "0.70" }
    };
    function levelParams(name) {
      return LEVELS[name] || LEVELS["高"];
    }

    /* 运动区弱超分权重: 环境变量 MSX_ACNET_MF(0~1) 覆盖, 缺省 0.28。
       -1 = 用缺省值(parseFloat 失败/负数/未设置都走这里)。 */
    var _mfEnv = parseFloat(env.motionFloor);
    var MF_FIX = (isFinite(_mfEnv) && _mfEnv >= 0) ? Math.min(1, _mfEnv) : -1;

    function combineFrag(P, wMode) {
      /* 运动闸门: 静止 → 1(完整网络细节); 运动 → MF(弱超分, 而不是 0)。
         ⚠️ 网络细节来自"周期开始时的输入快照", 运动区全量注入会拖影; 0 又等于
         动态物体完全不超。取一个下限: 权重低到看不出重影, 但仍有细节提升。
         MF=0 = 旧行为; MSX_ACNET_MF 可整体覆盖(见 MF_FIX)。 */
      var mf = (MF_FIX >= 0 ? MF_FIX : 0.28).toFixed(3);
      var wExpr = wMode === 1 ? "1.0"
        : ("(mix(" + mf + ", 1.0, 1.0 - smoothstep(0.045, 0.10, min(abs(yCur - ySnap), 1.0))))");
      return "#version 300 es\nprecision highp float;\nuniform vec2 u_outSize;\n" +
        "const vec3 WC = vec3(0.2126, 0.7152, 0.0722);\n" +
        "uniform vec4 u_rect;\nuniform vec2 u_vidSize;\n" +
        "uniform sampler2D luma2x_smp;\nuniform sampler2D vid_smp;\n" +
        "uniform sampler2D snap_smp;\nuniform sampler2D lumaN_smp;\n" +
        "out vec4 o;\n" +
        "void main(){\n" +
        "  vec2 uv = gl_FragCoord.xy / u_outSize;\n" +
        "  vec2 vuv = (uv - u_rect.xy) / u_rect.zw;\n" +
        "  vec2 inside = step(vec2(0.0), vuv) * step(vuv, vec2(1.0));\n" +
        "  if (inside.x * inside.y < 0.5) { o = vec4(0.0, 0.0, 0.0, 1.0); return; }\n" +
        "  vuv = clamp(vuv, vec2(0.0), vec2(1.0));\n" +
        "  float yNew = texture(luma2x_smp, vuv).r;\n" +
        "  vec3 c = texture(vid_smp, vuv).rgb;\n" +
        /* CAS/RCAS 式对比度自适应锐化: 源被合成器双线性拉伸到显示尺寸, 模糊在这里;
           强度按局部对比度自适应(强边缘减量→不出光晕), 亮度软限幅在邻域 min/max 附近。
           yRef/增益数学仍用未锐化的 c, 两套效果互不干扰。 */
        "  vec2 ut = 1.0 / u_outSize;\n" +
        "  vec3 cN = texture(vid_smp, clamp(vuv + vec2(0.0, -ut.y), vec2(0.0), vec2(1.0))).rgb;\n" +
        "  vec3 cS = texture(vid_smp, clamp(vuv + vec2(0.0,  ut.y), vec2(0.0), vec2(1.0))).rgb;\n" +
        "  vec3 cE = texture(vid_smp, clamp(vuv + vec2( ut.x, 0.0), vec2(0.0), vec2(1.0))).rgb;\n" +
        "  vec3 cW = texture(vid_smp, clamp(vuv + vec2(-ut.x, 0.0), vec2(0.0), vec2(1.0))).rgb;\n" +
        "  float yl0 = dot(c, WC), yl1 = dot(cN, WC), yl2 = dot(cS, WC), yl3 = dot(cE, WC), yl4 = dot(cW, WC);\n" +
        "  float ymn = min(min(yl0, yl1), min(min(yl2, yl3), yl4));\n" +
        "  float ymx = max(max(yl0, yl1), max(max(yl2, yl3), yl4));\n" +
        "  float yrange = ymx - ymn;\n" +
        "  float amt = " + P.sharp + " * clamp(1.25 - yrange * 2.5, 0.2, 1.0);\n" +
        "  vec3 cs = c + (c - (cN + cS + cE + cW) * 0.25) * amt;\n" +
        "  float ysh = dot(cs, WC);\n" +
        "  float ycl = clamp(ysh, max(ymn - yrange * 0.25, 0.0), min(ymx + yrange * 0.25, 1.0));\n" +
        "  cs *= ycl / max(ysh, 1e-4);\n" +
        "  float yRef = max(dot(c, WC), " + P.floor + ");\n" +
        "  float ySnap = texture(snap_smp, vuv).r;\n" +
        "  float yCur = texture(lumaN_smp, vuv).r;\n" +
        "  float w = " + wExpr + ";\n" +
        "  float gN = length(vec2(dFdx(yNew), dFdy(yNew)));\n" +
        "  float gR = length(vec2(dFdx(yRef), dFdy(yRef)));\n" +
        "  float conf = clamp((gN / max(gR, 0.002) - " + P.cut + ") / " + P.span + ", 0.0, 1.0);\n" +
        /* 零偏置细节注入: 只取网络输出的高频(dNet), 低频亮度偏置被高通滤除 ——
           任何内容都不会"开超分更暗"; 平坦区 dNet≈0 → 增益精确回 1(输出=原画)。 */
        "  float yB1 = texture(luma2x_smp, clamp(vuv + vec2( ut.x, 0.0), vec2(0.0), vec2(1.0))).r;\n" +
        "  float yB2 = texture(luma2x_smp, clamp(vuv + vec2(-ut.x, 0.0), vec2(0.0), vec2(1.0))).r;\n" +
        "  float yB3 = texture(luma2x_smp, clamp(vuv + vec2(0.0,  ut.y), vec2(0.0), vec2(1.0))).r;\n" +
        "  float yB4 = texture(luma2x_smp, clamp(vuv + vec2(0.0, -ut.y), vec2(0.0), vec2(1.0))).r;\n" +
        "  float dNet = yNew - (yB1 + yB2 + yB3 + yB4) * 0.25;\n" +
        "  float gain = clamp(1.0 + (dNet / yRef) * conf * w, " + P.lo + ", " + P.hi + ");\n" +
        "  o = vec4(clamp(cs * gain, vec3(0.0), vec3(1.0)), 1.0);\n" +
        "}\n";
    }

    var BLIT_FRAG =
      "#version 300 es\nprecision highp float;\nuniform vec2 u_outSize;\n" +
      "uniform sampler2D vid_smp;\n" +
      "out vec4 o;\nvoid main(){ o = vec4(texture(vid_smp, gl_FragCoord.xy / u_outSize).rgb, 1.0); }\n";

    /* ---------- 3) 纹理 / FBO ---------- */
    function make8(gl, w, h) {
      var t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      var f = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, f);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      return { tex: t, fbo: f, w: w, h: h };
    }

    function make(gl, w, h) {
      var t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      /* ACNet 中间纹理装特征图(有负值): RGBA16F 需 EXT_color_buffer_float,
         FBO 不完整时 WebGL 不报错而是全 0 → 必须查完整性, 不完整退回 RGBA8 */
      var half = !!gl.__msxHalfFloat;
      if (half) gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, null);
      else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      var f = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, f);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
      var st = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      if (st !== gl.FRAMEBUFFER_COMPLETE) {
        gl.__msxFboBad = st;
        if (half) {
          gl.__msxHalfFloat = false;
          gl.deleteTexture(t);
          gl.deleteFramebuffer(f);
          return make8(gl, w, h);
        }
      }
      return { tex: t, fbo: f, w: w, h: h };
    }

    /* ---------- 4) program 编译(uniform 位置一次性缓存) ---------- */
    function cacheUniforms(gl, prog, pass) {
      if (!pass || !pass.d || !pass.d.binds) {
        env.onFail && env.onFail("cacheUniforms: bad pass");
        return null;
      }
      var names = pass.d.binds.slice();
      if (names.indexOf("LUMA") < 0) names.push("LUMA");
      var u = {};
      names.forEach(function (n) {
        u[n] = {
          smp: gl.getUniformLocation(prog, n + "_smp"),
          size: gl.getUniformLocation(prog, n + "_size"),
          pt: gl.getUniformLocation(prog, n + "_pt"),
        };
      });
      return { byName: u, outSize: gl.getUniformLocation(prog, "u_outSize"),
               rect: gl.getUniformLocation(prog, "u_rect"), vidSize: gl.getUniformLocation(prog, "u_vidSize") };
    }

    function cacheBasic(gl, prog, names) {
      var m = {};
      names.forEach(function (n) {
        m[n] = gl.getUniformLocation(prog, n + "_smp");
      });
      return { smp: m, outSize: gl.getUniformLocation(prog, "u_outSize"),
               rect: gl.getUniformLocation(prog, "u_rect"), vidSize: gl.getUniformLocation(prog, "u_vidSize") };
    }

    function compileAll(gl, iw, ih) {
      var texs = { LUMA: { tex: null, w: iw, h: ih } };
      var passes = [];
      var MAXP = parseInt(env.maxPass, 10) || PASSES.length;
      if (MAXP > PASSES.length) MAXP = PASSES.length;
      PASSES.forEach(function (d, i) {
        if (i >= MAXP) return;
        var tw = iw, th = ih;
        if (d.width && d.width[0] === "LUMA.w") tw = Math.round(iw * (parseFloat(d.width[1]) || 1));
        if (d.height && d.height[0] === "LUMA.h") th = Math.round(ih * (parseFloat(d.height[1]) || 1));
        var sizes = {};
        d.binds.forEach(function (n) {
          var t = texs[n] || texs.LUMA;
          sizes[n] = [t.w || iw, t.h || ih];
        });
        sizes.LUMA = [iw, ih];
        var key = (i === MAXP - 1) ? "__OUT__" : (d.save || "__OUT__");
        var frag = prelude(d, sizes, [tw, th]) + d.body + "\nvoid main(){ msx_out = hook(); }\n";
        var p = program(gl, frag, "acnet" + i);
        var entry = null;
        if (p) {
          entry = { p: p, d: d, w: tw, h: th, key: key, u: null };
          entry.u = cacheUniforms(gl, p, entry);
        }
        passes.push(entry);
        if (!texs[key] || texs[key].w !== tw || texs[key].h !== th) texs[key] = { w: tw, h: th };
        if (i === MAXP - 1 && d.save) texs[d.save] = { w: tw, h: th };
      });
      var ok = passes.filter(Boolean).length;
      return { iw: iw, ih: ih, passes: passes, ok: ok, total: PASSES.length };
    }

    /* ---------- 5) 状态 ---------- */
    var gl = null, alive = false, gen = 0, lost = false;
    var curIw = 0, curIh = 0, vw = 0, vh = 0, level = "?";
    var c = null, T = null, B = null, lastIdx = -1;
    var cyclePass = 12, cycleCount = 0, passCostEma = 0;
    var frames = 0, tPrev = 0, worstGap = 0;
    var vidTex = null;
    var firstLatch = false;
    var UNITS = 8;

    function ensureGL() {
      if (gl) return true;
      try {
        gl = canvas.getContext("webgl2", { alpha: false, premultipliedAlpha: false,
                                           antialias: false, depth: false, stencil: false,
                                           preserveDrawingBuffer: !!env.preserve });
      } catch (e) { gl = null; }
      if (!gl) { env.onFail && env.onFail("no-webgl2"); return false; }
      gl.__msxHalfFloat = !!gl.getExtension("EXT_color_buffer_float");
      try {
        gl.__msxTimerExt = gl.getExtension("EXT_disjoint_timer_query_webgl2") || null;
      } catch (e) { gl.__msxTimerExt = null; }
      /* 上传视频时按 GL 的行序翻转: 不加这句画面会上下颠倒 */
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      gl.disable(gl.BLEND);
      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.CULL_FACE);
      /* 上下文丢失(GPU 被压垮/驱动重置)后 GL 调用静默无效 → 登记, kick 自停 */
      canvas.addEventListener && canvas.addEventListener("webglcontextlost", function (e) {
        try { e.preventDefault(); } catch (e2) {}
        lost = true;
        env.onFail && env.onFail("webgl-context-lost");
      }, false);
      return true;
    }

    function ensureAssets() {
      var pkey = curIw + "x" + curIh;
      var LRU = canvas.__msxAcnetLRU || (canvas.__msxAcnetLRU = []);
      c = null;
      for (var li = 0; li < LRU.length; li++) {
        if (LRU[li].key === pkey) { c = LRU[li].c; if (li > 0) LRU.unshift(LRU.splice(li, 1)[0]); break; }
      }
      if (!c) {
        c = compileAll(gl, curIw, curIh);
        if (!c.ok) {
          env.onFail && env.onFail("compile " + c.ok + "/" + c.total);
          return false;
        }
        LRU.unshift({ key: pkey, c: c });
        while (LRU.length > 2) {
          var ev = LRU.pop();
          ev.c.passes.forEach(function (it) { try { if (it) gl.deleteProgram(it.p); } catch (e) {} });
        }
      }
      lastIdx = -1;
      for (var fi = 0; fi < c.passes.length; fi++) if (c.passes[fi]) lastIdx = fi;

      var P = levelParams(level);
      var basicKey = level || "?";
      if (canvas.__msxAcnetBasic && canvas.__msxAcnetBasicKey !== basicKey) {
        try { gl.deleteProgram(canvas.__msxAcnetBasic.pLuma); } catch (e) {}
        try { gl.deleteProgram(canvas.__msxAcnetBasic.pCombine); } catch (e) {}
        try { if (canvas.__msxAcnetBasic.pBlit) gl.deleteProgram(canvas.__msxAcnetBasic.pBlit); } catch (e) {}
        canvas.__msxAcnetBasic = null;
      }
      if (!canvas.__msxAcnetBasic) {
        var fragC = combineFrag(P, env.oldReuse ? 1 : 0);
        var pLuma = program(gl, LUMA_FRAG, "luma");
        var pCombine = program(gl, fragC, "combine");
        var pBlit = program(gl, BLIT_FRAG, "blit");
        if (!pLuma || !pCombine) return false;
        canvas.__msxAcnetBasicKey = basicKey;
        canvas.__msxAcnetBasic = {
          pLuma: pLuma, pCombine: pCombine, pBlit: pBlit,
          uLuma: cacheBasic(gl, pLuma, ["vid"]),
          uCombine: cacheBasic(gl, pCombine, ["luma2x", "vid", "snap", "lumaN"]),
          uBlit: pBlit ? cacheBasic(gl, pBlit, ["vid"]) : null,
        };
      }
      c.passes.forEach(function (it) {
        if (it && !it.u) it.u = cacheUniforms(gl, it.p, it);
      });

      T = canvas.__msxAcnetTex;
      if (!T || canvas.__msxAcnetTexKey !== curIw + "x" + curIh) {
        if (T) {
          Object.keys(T).forEach(function (k) {
            try { gl.deleteTexture(T[k].tex); gl.deleteFramebuffer(T[k].fbo); } catch (e) {}
          });
        }
        T = {};
        try {
          var ow2 = curIw * 2, oh2 = curIh * 2;
          T.LUMA = make(gl, curIw, curIh);
          T.SNAP_RUN = make(gl, curIw, curIh);    // 进行中周期的网络输入快照(周期开始时冻结)
          T.SNAP_OUT = make(gl, curIw, curIh);    // 与 __OUT__ 配对的输入快照(运动闸门比对基准)
          T.__OUT__ = make(gl, ow2, oh2);
          c.passes.forEach(function (it) {
            if (!it) return;
            if (!T[it.key]) T[it.key] = make(gl, it.w, it.h);
          });
          if (c.passes[lastIdx] && c.passes[lastIdx].d.save && !T[c.passes[lastIdx].d.save]) {
            T[c.passes[lastIdx].d.save] = make(gl, c.passes[lastIdx].w, c.passes[lastIdx].h);
          }
        } catch (e) {
          env.onFail && env.onFail("texture: " + e);
          return false;
        }
        canvas.__msxAcnetTex = T;
        canvas.__msxAcnetTexKey = curIw + "x" + curIh;
      }
      if (c.passes[lastIdx] && c.passes[lastIdx].d.save) T[c.passes[lastIdx].d.save] = T.__OUT__;
      B = canvas.__msxAcnetBasic;
      return true;
    }

    /* 绑定与 uniform 上传必须在 useProgram 之后(draw 的 fn 里), 否则写到别的 program 上 */
    function draw(p, target, fn) {
      if (target) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, target.fbo);
        gl.viewport(0, 0, target.w, target.h);
      } else {
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, canvas.width, canvas.height);
      }
      gl.useProgram(p);
      if (fn) fn();
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    function bindOne(loc, unit, t, w, h) {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, t);
      if (loc.smp) gl.uniform1i(loc.smp, unit);
      if (loc.size) gl.uniform2f(loc.size, w, h);
      if (loc.pt) gl.uniform2f(loc.pt, 1 / w, 1 / h);
    }

    function bindRaw(loc, unit, t) {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, t);
      if (loc) gl.uniform1i(loc, unit);
    }

    function bindPass(it) {
      var names = it.d.binds.slice();
      if (names.indexOf("LUMA") < 0) names.push("LUMA");
      var unit = 0;
      names.forEach(function (n) {
        /* 网络整条链只读周期开始时冻结的输入快照 —— T.LUMA 每帧被重写为"当帧",
           分片跨帧执行时若读它会混入不同帧的亮度 */
        var t = (n === "LUMA") ? T.SNAP_RUN : (T[n] || T.SNAP_RUN);
        var loc = it.u.byName[n];
        if (!loc || !loc.smp) return;
        bindOne(loc, unit, t.tex, t.w, t.h);
        unit = (unit + 1) % UNITS;
      });
    }

    function makeVidTex() {
      var t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      return t;
    }

    function gateString() {
      return "lvl=" + level + " mf=" + (MF_FIX >= 0 ? MF_FIX : 0.28) +
             " iw=" + curIw +
             " pass=" + Math.min(cyclePass, 12) + "/12" +
             " passCost=" + Math.round(passCostEma * 100) / 100 +
             " cycles=" + cycleCount + " frames=" + frames;
    }

    function stats() {
      env.onStats && env.onStats({
        frames: frames, netRuns: cycleCount, gate: gateString(),
        lumaSrc: cycleCount ? "net(cycle" + cycleCount + ")" : "warmup",
      });
    }

    /* ---------- 6) 对外 API ---------- */
    var api = {
      params: function (p) {
        var needRebuild = (curIw !== (p.iw | 0)) || (curIh !== (p.ih | 0)) || (level !== (p.level || "?"));
        curIw = p.iw | 0; curIh = p.ih | 0; vw = p.vw | 0; vh = p.vh | 0;
        level = p.level || "?";
        if (!(curIw >= 16) || !(curIh >= 16) || !vw || !vh) {
          env.onFail && env.onFail("bad-params");
          return false;
        }
        if (!ensureGL()) return false;
        /* ⚠️ 还必须判纹理是否还在: stop() 会释放 T(退出超分时省显存), 再次激活时
           iw/ih 往往没变(同一视频/同一档位) —— 只判 needRebuild 会跳过 ensureAssets,
           T 保持 null, 之后每帧 kick 在 T.LUMA 上抛异常, 画布永远停在上一帧
           (用户体感: 进全屏画布卡住半天, 声音还在放), 直到看门狗拆管重建才恢复。 */
        if (needRebuild || !c || !canvas.__msxAcnetTex) {
          if (!ensureAssets()) return false;
          cyclePass = 12;                        // 尺寸/档位变了: 旧周期作废, 下帧开新周期
        }
        alive = true;
        return true;
      },
      reset: function (g) { gen = g; alive = true; tPrev = 0; firstLatch = false; },
      resize: function (w, h) {
        try {
          if (canvas.width !== w) canvas.width = w;
          if (canvas.height !== h) canvas.height = h;
        } catch (e) {}
      },
      /* 每帧主体: source = VideoFrame(worker) 或 <video> 元素(主线程回退路)。
         meta: {force, paused, expectedDisplayTime}
         返回 true = 这一帧确实画上了(上层拿它当"画布是否还在更新"的看门狗信号)。 */
      kick: function (source, meta) {
        meta = meta || {};
        var painted = false;
        try {
          if (!alive || lost || canvas.__msxAcnetCtxLost) return false;
          if (!source || !vw) return false;
          var t0 = 0, frameGap = 0;
          try { t0 = meta.expectedDisplayTime || 0; } catch (e) { t0 = 0; }
          if (tPrev && t0) {
            frameGap = t0 - tPrev;
            if (frameGap > worstGap) worstGap = frameGap;
          }
          if (t0) tPrev = t0;

          /* 上传当帧(VideoFrame 与 <video> 元素走同一 texImage2D 形态)。
             帧的 close 统一放在函数级 finally —— 任何提前 return 的路径也不能漏。 */
          if (!vidTex) vidTex = makeVidTex();
          gl.bindTexture(gl.TEXTURE_2D, vidTex);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);

          var cw = canvas.width, chh = canvas.height;
          /* 画面在画布里的实际矩形(contain 居中) —— 宽高比不同留黑边不拉伸 */
          var sc = Math.min(cw / vw, chh / vh);
          var rw = vw * sc / cw, rh = vh * sc / chh;
          var rx = (1 - rw) / 2, ry = (1 - rh) / 2;
          canvas.__msxAcnetSinceNet = (canvas.__msxAcnetSinceNet || 0) + 1;

          // ① luma: 当帧亮度(合成趟的 lumaN/yRef 用, 每帧都跑)
          draw(B.pLuma, T.LUMA, function () {
            bindRaw(B.uLuma.smp.vid, 0, vidTex);
            if (B.uLuma.outSize) gl.uniform2f(B.uLuma.outSize, curIw, curIh);
          });

          // ② 网络 12 趟, 分片执行(只读 SNAP_RUN 冻结快照)
          var nullTargets = 0;
          if (cyclePass >= 12) {
            draw(B.pBlit, T.SNAP_RUN, function () {
              bindRaw(B.uBlit.smp.vid, 0, T.LUMA.tex);
              if (B.uBlit.outSize) gl.uniform2f(B.uBlit.outSize, curIw, curIh);
            });
            cyclePass = 0;
          }
          /* K 决策: 播放中按单趟实测 GPU 耗时控制单片 ≤ tickBudget(默认 8ms);
             一口气跑完(K=12)仅限: 探针同步/暂停/强制单帧(没有节奏可保护)。
             首周期 passCostEma=0 → K=1, 12 帧内渐进成型, 激活无一次性大冻结。 */
          var K = 12;
          if (!meta.force && !meta.paused) {
            var budgetMs = parseFloat(env.tickBudget) || 8;
            K = passCostEma > 0 ? Math.max(1, Math.min(12, Math.floor(budgetMs / passCostEma))) : 1;
          }
          var tq = null, lastK = K;
          if (gl.__msxTimerExt && K < 12) {
            try {
              tq = gl.createQuery();
              gl.beginQuery(gl.__msxTimerExt.TIME_ELAPSED_EXT, tq);
            } catch (e) { tq = null; }
          }
          for (var k = 0; k < K && cyclePass < 12; k++) {
            var it = c.passes[cyclePass];
            if (!it) { cyclePass++; continue; }
            if (!T[it.key]) nullTargets++;
            draw(it.p, T[it.key], (function (pass) {
              return function () { bindPass(pass); };
            })(it));
            cyclePass++;
          }
          if (tq) {
            try { gl.endQuery(gl.__msxTimerExt.TIME_ELAPSED_EXT); }
            catch (e) { try { gl.deleteQuery(tq); } catch (e2) {} tq = null; }
          }
          /* 回收上一片的 GPU 计时(下一帧 poll, 按趟数均摊 → 单趟真实 GPU 耗时) */
          if (gl.__msxTimerExt) {
            try {
              if (canvas.__msxAcnetQ) {
                var pq = canvas.__msxAcnetQ;
                var avail = gl.getQueryParameter(pq, gl.QUERY_RESULT_AVAILABLE);
                var disjoint = gl.getParameter(gl.__msxTimerExt.GPU_DISJOINT_EXT);
                if (avail && !disjoint) {
                  var ns = gl.getQueryParameter(pq, gl.QUERY_RESULT);
                  var perPass = (ns / 1e6) / Math.max(1, canvas.__msxAcnetQK || 1);
                  passCostEma = passCostEma > 0 ? passCostEma * 0.7 + perPass * 0.3 : perPass;
                }
                if (avail || disjoint) { gl.deleteQuery(pq); canvas.__msxAcnetQ = null; }
              }
              if (tq && !canvas.__msxAcnetQ) {
                canvas.__msxAcnetQ = tq;
                canvas.__msxAcnetQK = lastK;
              } else if (tq) { try { gl.deleteQuery(tq); } catch (e2) {} }
            } catch (e) { try { gl.deleteQuery(tq); } catch (e2) {} }
          }
          if (cyclePass >= 12) {
            /* 周期完成: __OUT__ 的输入是 SNAP_RUN → 运动闸门的 SNAP_OUT 与之配对 */
            draw(B.pBlit, T.SNAP_OUT, function () {
              bindRaw(B.uBlit.smp.vid, 0, T.SNAP_RUN.tex);
              if (B.uBlit.outSize) gl.uniform2f(B.uBlit.outSize, curIw, curIh);
            });
            cycleCount++;
            canvas.__msxAcnetSinceNet = 0;
          }

          // ③ 合成 → 画布(每帧都跑: 画布节奏=视频节奏)
          if (env.debugSrc && B.pBlit) {
            var srcT = env.debugSrc === "luma" ? T.LUMA
                     : env.debugSrc === "vid" ? { tex: vidTex, w: vw, h: vh }
                     : (T[env.debugSrc] || T.__OUT__);
            draw(B.pBlit, null, function () {
              bindRaw(B.uBlit.smp.vid, 0, srcT.tex);
              if (B.uBlit.outSize) gl.uniform2f(B.uBlit.outSize, cw, chh);
            });
          } else {
            draw(B.pCombine, null, function () {
              bindRaw(B.uCombine.smp.luma2x, 0, T.__OUT__.tex);
              bindRaw(B.uCombine.smp.vid, 1, vidTex);
              bindRaw(B.uCombine.smp.snap, 2, T.SNAP_OUT.tex);
              bindRaw(B.uCombine.smp.lumaN, 3, T.LUMA.tex);
              if (B.uCombine.outSize) gl.uniform2f(B.uCombine.outSize, cw, chh);
              if (B.uCombine.rect) gl.uniform4f(B.uCombine.rect, rx, ry, rw, rh);
              if (B.uCombine.vidSize) gl.uniform2f(B.uCombine.vidSize, vw, vh);
            });
          }

          frames++;
          if (!firstLatch) {
            firstLatch = true;
            env.onFirst && env.onFirst();
          }
          if (frames % 30 === 1 || frames < 5) stats();
          painted = true;
        } catch (e) {
          env.onFail && env.onFail("frame: " + e);
        } finally {
          /* VideoFrame 必须**无论成败都 close**, 包括上面所有提前 return 的路径:
             泄漏的帧会占死解码器缓冲池, 表现就是"声音还在放、视频卡死、seek 也没用"
             (全屏真机复现过)。<video> 元素没有 close, 这里自动跳过。 */
          try { if (source && source.close) source.close(); } catch (e2) {}
        }
        return painted;
      },
      stop: function () {
        alive = false;
        try {
          if (T) {
            Object.keys(T).forEach(function (k) {
              try { gl.deleteTexture(T[k].tex); gl.deleteFramebuffer(T[k].fbo); } catch (e) {}
            });
            T = null;
            canvas.__msxAcnetTex = null;
            canvas.__msxAcnetTexKey = "";
          }
        } catch (e) {}
        /* program 不删(LRU 保留两档), 复用免掉下一次的同步编译 */
      },
    };

    return api;
  }

  /* ═══════════ Worker 端(仅 worker 上下文执行; toString 进 blob) ═══════════ */
  function __msxAcnetWorkerMain() {
    "use strict";
    var core = null, latest = null, latestMeta = null, started = false;
    function drain() {
      if (!started || !core || !latest) return;
      var src = latest, meta = latestMeta;
      latest = null; latestMeta = null;
      var ok = false;
      try { ok = core.kick(src, meta) === true; } catch (e) { /* core 内部已上报 */ }
      /* 回执 = 主线程的背压闸门与"画布还在更新"的时间戳。没有回执, 主线程就不再
         构造新的 VideoFrame(见 __msxRunAcnet 的 feed) —— 慢 GPU 上宁可少超几帧,
         也不能让解码器缓冲池被积压帧占死。 */
      postMessage({ type: "drained", ok: ok });
    }
    self.onmessage = function (e) {
      var m = e.data || {};
      try {
        if (m.cmd === "init") {
          core = __msxAcnetCore({
            glsl: m.glsl, canvas: m.canvas, preserve: m.preserve,
            oldReuse: m.oldReuse, debugSrc: m.debugSrc, maxPass: m.maxPass,
            tickBudget: m.tickBudget, motionFloor: m.motionFloor,
            onFirst: function () { postMessage({ type: "first" }); },
            onStats: function (s) { postMessage({ type: "stats", s: s }); },
            onFail: function (msg) { postMessage({ type: "fail", msg: String(msg).slice(0, 300) }); },
          });
          if (!core) { postMessage({ type: "fail", msg: "core-init-failed" }); return; }
          core.params(m);
          core.reset(m.gen || 0);
          started = true;
        } else if (m.cmd === "params") {
          if (!core) return;
          /* 重新参数化 = 上一代喂帧链作废: 手里攥着的旧帧必须还给解码器,
             否则它永远不会被 close, 背压闸门也在等一个不会来的回执 */
          if (latest && latest.close) { try { latest.close(); } catch (e2) {} }
          latest = null; latestMeta = null;
          core.params(m);
          core.reset(m.gen || 0);
          started = true;
        } else if (m.cmd === "size") {
          if (core) core.resize(m.w, m.h);
        } else if (m.cmd === "frame") {
          if (!started || !core) {
            if (m.vf && m.vf.close) { try { m.vf.close(); } catch (e2) {} }
            postMessage({ type: "drained", ok: false });
            return;
          }
          /* 只留最新帧: 网络忙时旧帧直接丢弃(运动闸门保证画面正确), 不积压 */
          if (latest && latest.close) { try { latest.close(); } catch (e2) {} }
          latest = m.vf;
          latestMeta = m.meta || {};
          setTimeout(drain, 0);
        } else if (m.cmd === "stop") {
          started = false;
          if (latest && latest.close) { try { latest.close(); } catch (e2) {} }
          latest = null; latestMeta = null;
          if (core) core.stop();
        }
      } catch (e) {
        postMessage({ type: "fail", msg: "worker: " + e });
      }
    };
  }

  /* ═══════════ 主线程入口 ═══════════ */
  function buildWorkerBlob() {
    /* 必须用 var 声明: "(function 名字(){})" 是具名函数表达式, 名字不进外层
       作用域, 末尾的 __msxAcnetWorkerMain() 会 ReferenceError(实测 worker 静默死亡) */
    return "var __msxAcnetCore = " + __msxAcnetCore.toString() + ";\n" +
           "var __msxAcnetWorkerMain = " + __msxAcnetWorkerMain.toString() + ";\n" +
           "__msxAcnetWorkerMain();";
  }


  window.__msxRunAcnet = function (video, canvas, gen, o) {
    window.__msxAcnetEnter = (window.__msxAcnetEnter || 0) + 1;
    if (!o || !o.iw || !o.ih) {
      try { window.__msxAcnetFail = "bad-plan"; } catch (e) {}
      return null;
    }
    if (canvas.__msxAcnetGen === gen) return canvas.__msxAcnetStop;   // 同世代重复调用: 复用
    if (canvas.__msxAcnetStop) { try { canvas.__msxAcnetStop(); } catch (e) {} }

    var statsIn = function (s) {
      try {
        window.__msxAcnetFrames = s.frames;
        window.__msxAcnetNetRuns = s.netRuns;
        window.__msxAcnetNetGate = s.gate;
        window.__msxAcnetLumaSrc = s.lumaSrc;
        if (s.fail) window.__msxAcnetFail = s.fail;
      } catch (e) {}
    };

    /* ---- Worker 路(默认): 主线程零阻塞 ---- */
    var canWorker = !window.__MSX_ACNET_NO_WORKER__ && !!window.Worker &&
                    !!window.OffscreenCanvas && typeof VideoFrame === "function" &&
                    typeof video.requestVideoFrameCallback === "function" &&
                    typeof canvas.transferControlToOffscreen === "function";
    if (canWorker) {
      try {
          if (canvas.__msxAcnetWorkerDead) {
            /* 上一轮 worker 崩溃: 该画布的 OffscreenCanvas 已随 worker 丢失,
               不能再 transfer —— 上层 ensureCanvas 换新画布后才会重新走到这里 */
            try { window.__msxAcnetFail = "worker-dead-canvas"; } catch (e) {}
            return null;
          }
        if (!canvas.__msxAcnetWorker) {
          /* 先建 Worker 再转移画布: 若 Worker 构造失败(blob 被 CSP/来源拦截等),
             画布还没被转移, 主线程回退路仍能正常 getContext */
          var w = new Worker(URL.createObjectURL(
            new Blob([buildWorkerBlob()], { type: "text/javascript" })));
          var off = canvas.transferControlToOffscreen();
          canvas.__msxAcnetTransferred = 1;
          canvas.__msxAcnetWorker = w;
          w.onerror = function () {
            /* worker 崩溃(脚本错误/CSP)若不可见, 画布会永远黑着 —— 标记死画布,
               上层看门狗(帧计数停滞)拆管, ensureCanvas 换新画布后重建 */
            try { window.__msxAcnetFail = "worker-error"; } catch (e2) {}
            try { canvas.__msxAcnetWorkerDead = 1; } catch (e3) {}
            canvas.__msxAcnetWorker = null;
          };
          w.onmessage = function (e) {
            var m = e.data || {};
            if (m.type === "first") {
              try { window.__msxSrFirstFrame && window.__msxSrFirstFrame(canvas); } catch (e2) {}
            } else if (m.type === "stats") {
              statsIn(m.s || {});
            } else if (m.type === "fail") {
              try { window.__msxAcnetFail = m.msg; } catch (e2) {}
            } else if (m.type === "drained") {
              /* ⚠️ 回执必须交给**当前激活**的回调。worker 在同一块画布上跨世代复用,
                 onmessage 只在第一次创建时装一次; 旧实现把回执写进"首次激活"闭包里的
                 inFlight —— 退出全屏再进入后, 新世代的闸门永远解不开 → 只喂第一帧,
                 画面定格而声音继续(用户报修: 第一次进全屏正常, 退出再进就卡死)。 */
              var dn = canvas.__msxAcnetDrained;
              if (dn) { try { dn(m); } catch (e3) {} }
            }
          };
          w.postMessage({ cmd: "init", glsl: window.__MSX_ACNET_GLSL__, canvas: off,
                          preserve: !!window.__MSX_ACNET_PRESERVE__,
                          oldReuse: !!window.__MSX_ACNET_OLD_REUSE__,
                          debugSrc: window.__MSX_ACNET_DEBUG__ || "",
                          maxPass: window.__MSX_ACNET_MAXPASS__ || "",
                          tickBudget: window.__MSX_ACNET_TICK_BUDGET__ || "",
                          motionFloor: window.__MSX_ACNET_MF__ || "",
                          iw: o.iw, ih: o.ih, vw: o.vw, vh: o.vh, level: o.level, gen: gen,
                          w: canvas.width, h: canvas.height }, [off]);
        } else {
          canvas.__msxAcnetWorker.postMessage({ cmd: "params", iw: o.iw, ih: o.ih,
                                                vw: o.vw, vh: o.vh, level: o.level, gen: gen,
                                                w: canvas.width, h: canvas.height });
        }
        var stopped = false;
        var lastFeed = performance.now();
        var inFlight = false;                // worker 手里还有一帧没啃完
        /* 每次激活都替换: drained 回执解的必须是**这一代**的闸门(见 onmessage 处说明)。
           同一 worker 被复用时, 上一代闭包里的 inFlight 已无人读取, 往那里写等于丢回执。 */
        canvas.__msxAcnetDrained = function (d) {
          inFlight = false;                  // 背压闸门: 可以喂下一帧了
          if (d && d.ok) {
            try { canvas.__msxAcnetLastPaint = performance.now(); } catch (e2) {}
          }
        };
        /* 喂帧: new VideoFrame(零拷贝) → 转交 worker, **带背压**。
           旧实现每来一个视频帧就 new + postMessage; worker 在全屏啃不动时(画布像素
           ×4、网络满源宽), 未处理的消息队列会堆几十上百个 VideoFrame —— 解码器输出
           缓冲池被占死 → 声音还在放、画面永远停在最后一帧、seek 也没用(真机复现过)。
           现在最多只有一帧在途: 收到 worker 的 drained 回执才构造下一帧。 */
        function feed() {
          if (stopped || video.__msxSrGen !== gen) return;
          if (inFlight) return;
          var vf = null;
          try {
            vf = new VideoFrame(video);
            inFlight = true;
            canvas.__msxAcnetWorker.postMessage({ cmd: "frame", vf: vf, meta: {} }, [vf]);
            vf = null;                        // 所有权已随消息转移
          } catch (e) {
            inFlight = false;
            if (vf && vf.close) { try { vf.close(); } catch (e2) {} }
          }
          lastFeed = performance.now();
        }
        /* rVFC 泵: 每个呈现的视频帧喂一次。sr.user.js 已 patch rVFC(带世代门控),
           换代后本泵自动停。 */
        try {
          video.requestVideoFrameCallback(function pump(meta) {
            if (stopped || video.__msxSrGen !== gen) return;
            feed();
            video.requestVideoFrameCallback(pump);
          });
        } catch (e) {}
        /* 喂帧兜底: 全屏切换/seek 的时序竞争会让 rVFC 静默(帧不再"呈现"就不回调),
           画布冻在最后一帧而声音继续 —— 播放中 >600ms 没喂过帧且 rVFC 也没动静,
           就用 rAF 直接取帧(不依赖"呈现"), 直到 rVFC 恢复。 */
        var lastRvfc = lastFeed;
        try {
          video.requestVideoFrameCallback(function rvfcMark() {
            if (stopped || video.__msxSrGen !== gen) return;
            lastRvfc = performance.now();
            video.requestVideoFrameCallback(rvfcMark);
          });
        } catch (e) {}
        var revive = setInterval(function () {
          if (stopped || video.__msxSrGen !== gen) { clearInterval(revive); return; }
          if (video.paused || video.ended || !video.videoWidth) return;
          var now = performance.now();
          if (now - lastFeed > 600 && now - lastRvfc > 600) {
            feed();                            // worker 端只留最新帧, 不积压
          }
        }, 300);
        var stop = function () {
          if (stopped) return;
          stopped = true;
          inFlight = false;
          try { clearInterval(revive); } catch (e) {}
          try { canvas.__msxAcnetWorker.postMessage({ cmd: "stop" }); } catch (e) {}
        };
        canvas.__msxAcnetGen = gen;
        canvas.__msxAcnetStop = stop;
        canvas.__msxAcnetShown = 0;
        canvas.__msxAcnetPost = function (m) { try { canvas.__msxAcnetWorker.postMessage(m); } catch (e) {} };
        /* 暂停激活: rVFC 不会来 → 立刻转交一帧(force), worker 一口气成型 */
        canvas.__msxAcnetOnce = function () {
          var vf = null;
          try {
            vf = new VideoFrame(video);
            inFlight = true;
            canvas.__msxAcnetWorker.postMessage({ cmd: "frame", vf: vf,
                                                  meta: { force: true, paused: true } }, [vf]);
            vf = null;
          } catch (e) {
            inFlight = false;
            if (vf && vf.close) { try { vf.close(); } catch (e2) {} }
          }
        };
        canvas.__msxAcnetLastPaint = performance.now();
        if (video.paused) { try { canvas.__msxAcnetOnce(); } catch (e) {} }
        try { window.__msxAcnetFail = null; } catch (e) {}
        return stop;
      } catch (e) {
        console.warn("[msx] worker path failed, fallback to main thread:", e);
        /* 落到下面的主线程回退路 */
      }
    }

    /* ---- 主线程回退路(worker 不可用/失败): 同一份管线核心, 在主线程跑 ---- */
    try {
      var core = canvas.__msxAcnetCoreInst;
      if (!core) {
        core = __msxAcnetCore({
          glsl: window.__MSX_ACNET_GLSL__, canvas: canvas,
          preserve: !!window.__MSX_ACNET_PRESERVE__,
          oldReuse: !!window.__MSX_ACNET_OLD_REUSE__,
          debugSrc: window.__MSX_ACNET_DEBUG__ || "",
          maxPass: window.__MSX_ACNET_MAXPASS__ || "",
          tickBudget: window.__MSX_ACNET_TICK_BUDGET__ || "",
          motionFloor: window.__MSX_ACNET_MF__ || "",
          onFirst: function () { try { window.__msxSrFirstFrame && window.__msxSrFirstFrame(canvas); } catch (e) {} },
          onStats: statsIn,
          onFail: function (m) { try { window.__msxAcnetFail = m; } catch (e) {} },
        });
        if (!core) return null;
        canvas.__msxAcnetCoreInst = core;
      }
      if (!core.params({ iw: o.iw, ih: o.ih, vw: o.vw, vh: o.vh, level: o.level })) return null;
      core.reset(gen);
      var stopped2 = false;
      canvas.__msxAcnetGen = gen;
      canvas.__msxAcnetShown = 0;
      var stop2 = function () {
        if (stopped2) return;
        stopped2 = true;
        core.stop();
      };
      canvas.__msxAcnetStop = stop2;
      canvas.__msxAcnetLastPaint = performance.now();
      canvas.__msxAcnetOnce = function () {
        try {
          if (core.kick(video, { force: true, paused: true })) {
            canvas.__msxAcnetLastPaint = performance.now();
          }
        } catch (e) {}
      };
      if (video.paused) { try { canvas.__msxAcnetOnce(); } catch (e) {} }
      if (o.sync) {
        /* 探针: 一次调用把 maxFrames 帧全部画完(独立计数防帧体抛错导致无限递归) */
        for (var si = 0; si < (o.maxFrames || 1); si++) {
          if (core.kick(video, { force: true })) canvas.__msxAcnetLastPaint = performance.now();
        }
        return stop2;
      }
      try {
        video.requestVideoFrameCallback(function pump2(meta) {
          if (stopped2 || video.__msxSrGen !== gen) return;
          if (core.kick(video, { expectedDisplayTime: meta && meta.expectedDisplayTime })) {
            canvas.__msxAcnetLastPaint = performance.now();
          }
          video.requestVideoFrameCallback(pump2);
        });
      } catch (e) {}
      try { window.__msxAcnetFail = null; } catch (e) {}
      return stop2;
    } catch (e) {
      try { window.__msxAcnetFail = "entry: " + e; } catch (e2) {}
      return null;
    }
  };
})();
