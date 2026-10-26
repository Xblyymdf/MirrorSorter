/* v0.4.48 回归: 分栏拖动的预览线在「字体缩放」下必须仍然贴着鼠标。
 *
 * 用法: node _test_splitghost_zoom.js [web/app.js]
 * 退出码 0 = 全过; 1 = 有失败。
 *
 * 背景（线上症状）: 字体大小调到「中/大」时，拖分隔条的那条绿线会跑到右栏里面去，
 * 越往右错得越多，而鼠标其实还在分隔条上。
 *
 * 根因: 界面缩放是 document.body.style.zoom（见 applyPrefs）。预览线 #splitGhost
 * 原来挂在 <body> 里，于是它自己的 translate3d(Xpx) 又被 zoom 乘了一次 ——
 * getBoundingClientRect() 返回的 rect0.left 已经是缩放后的可视坐标，写进被 zoom 的
 * 子树里就成了 X × z。z=1.15 时一条本该在 674 的线画到 775，正好是"跑到右栏里"。
 *
 * 这个脚本不靠字符串断言: 它把 app.js 里**真实的**拖拽代码段（splitGhostEl /
 * uiZoom / pointerdown 监听）抽出来，配一套按 zoom 语义算几何的假 DOM 跑，
 * 逐条验证「鼠标移动多少可视像素，线就移动多少可视像素」。
 */
"use strict";
const fs = require("fs");
const path = require("path");

const APP = process.argv[2] || path.join(__dirname, "web", "app.js");
const src = fs.readFileSync(APP, "utf8");

/* ---------- 从真实源码里抽取待测代码段 ---------- */
const A = src.indexOf("let splitGhost = null;");
const B = src.indexOf('$("#toggleLeft").onclick');
if (A < 0 || B <= A) {
  console.log("FAIL 无法从 app.js 抽到分栏拖动代码段(源码结构变了?)");
  process.exit(1);
}
const SLICE = src.slice(A, B);

let pass = 0, fail = 0;
function ok(cond, name, extra) {
  if (cond) { pass++; console.log("PASS " + name); }
  else { fail++; console.log("FAIL " + name + (extra ? " :: " + extra : "")); }
}
const near = (a, b, tol) => Math.abs(a - b) <= (tol === undefined ? 1 : tol);

/* ---------- 假 DOM ---------- */
/* 几何模型（与浏览器在 body{zoom:z} 下的实际行为一致）:
 *   视口宽 W（可视像素）; body 的布局宽是 W/z（本地像素），渲染时放大 z 倍。
 *   #shell 的 grid 列是 leftW / 5 / 1fr / 5 / rightW（本地像素）。
 *   所以可视像素 = 本地像素 × z。
 */
function harness(opt) {
  const W = opt.W === undefined ? 1280 : opt.W;
  const z = opt.z === undefined ? 1 : opt.z;
  const prefs = {
    leftW: opt.leftW === undefined ? 238 : opt.leftW,
    rightW: opt.rightW === undefined ? 235 : opt.rightW,
    le: !!opt.le,
    re: !!opt.re,
  };
  const zoomVar = opt.zoomVar === undefined ? String(z) : opt.zoomVar;
  const effLeft = prefs.le ? 0 : prefs.leftW;
  const effRight = prefs.re ? 0 : prefs.rightW;
  const splitterLeft = (which) => which === "left"
    ? effLeft * z
    : W - effRight * z - 5 * z;

  const appended = { html: [], body: [] };
  const ghost = {
    id: "", isConnected: true, style: {}, _cls: new Set(),
    classList: {
      add: (c) => ghost._cls.add(c),
      remove: function () { for (const c of arguments) ghost._cls.delete(c); },
      toggle: (c, on) => { if (on) ghost._cls.add(c); else ghost._cls.delete(c); },
    },
  };
  const bodyCls = new Set();
  const listeners = {};
  const doc = {
    documentElement: { appendChild: (n) => appended.html.push(n) },
    body: {
      appendChild: (n) => appended.body.push(n),
      classList: {
        add: (c) => bodyCls.add(c),
        remove: (c) => bodyCls.delete(c),
        toggle: (c, on) => { if (on) bodyCls.add(c); else bodyCls.delete(c); },
      },
    },
    createElement: () => ghost,
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => {
      const a = listeners[t] || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    },
  };
  const gcs = () => ({ getPropertyValue: (k) => (k === "--ui-zoom" ? zoomVar : "") });

  // 在隔离函数作用域里执行真实代码段, prefs/applyPrefs 由外部注入
  const factory = new Function("document", "getComputedStyle", "prefs", "applyPrefs", "savePrefs",
    SLICE + "\n//# splitGhost slice end");
  factory(doc, gcs, prefs, function () {}, function () {});

  const gx = () => {
    const m = /translate3d\((-?\d+)px/.exec(ghost.style.transform || "");
    return m ? Number(m[1]) : NaN;
  };
  /* 线在屏幕上**实际**落在哪一列可视像素。
   * 这里必须模拟渲染语义: 挂在 <html> 下 → 1:1; 挂在 <body> 下 → body{zoom:z}
   * 会把定位值再放大 z 倍（这正是线上那条绿线跑到右栏里的原因）。
   * 只断言 transform 数值是不够的 —— 旧实现写进去的数也可能"看起来对"。 */
  const rx = () => {
    const v = gx();
    const underBody = appended.body.length > 0 && appended.html.length === 0;
    return underBody ? v * z : v;
  };
  const opacities = () => ghost._cls;

  /* 在某个分隔条上按下, 返回拖动/松手两个动作 */
  function grab(which, pointerX) {
    const spEl = {
      dataset: { split: which },
      getBoundingClientRect: () => ({
        left: splitterLeft(which), width: 5 * z, right: splitterLeft(which) + 5 * z,
        top: 0, height: 800,
      }),
    };
    let prevented = false;
    const ev = {
      target: { closest: (sel) => (sel === ".splitter" ? spEl : null) },
      clientX: pointerX,
      preventDefault: () => { prevented = true; },
    };
    (listeners.pointerdown || []).forEach((fn) => fn(ev));
    return {
      prevented,
      move: (x) => (listeners.pointermove || []).forEach((fn) => fn({ clientX: x })),
      up: () => (listeners.pointerup || []).forEach((fn) => fn({})),
    };
  }

  return { W, z, prefs, ghost, gx, rx, opacities, grab, appended, bodyCls, splitterLeft, listeners, effLeft, effRight };
}

/* ---------- 1. 预览线必须挂在 <html> 下（避开 body{zoom} 的二次缩放） ---------- */
{
  const H = harness({ z: 1.15 });
  H.grab("left", H.splitterLeft("left")).move(0);
  ok(H.appended.html.length === 1 && H.appended.body.length === 0,
     "预览线挂在 <html> 下(不落在 body{zoom} 子树里)",
     "html=" + H.appended.html.length + " body=" + H.appended.body.length);
}

/* ---------- 2. 拖动时线必须 1:1 跟手（三种缩放各验一遍） ---------- */
[1, 1.15, 1.3].forEach((z) => {
  const H = harness({ z });
  const L = H.splitterLeft("left");
  const x0 = L + 2.5 * z;                    // 按在分隔条正中
  const d = H.grab("left", x0);
  const g0 = H.rx();
  ok(near(g0, L), "z=" + z + " 按下时线就在分隔条左沿", "线上实际位置=" + g0 + " 期望=" + L);
  d.move(x0 + 100);                          // 鼠标右移 100 可视像素
  const delta = H.rx() - g0;
  ok(near(delta, 100), "z=" + z + " 鼠标右移 100px → 线在屏幕上右移 100px",
     "线走了 " + delta.toFixed(1) + "px, 旧实现会走 " + Math.round(100 * z) + "px");
  // 敏感性自检: 旧公式在 z≠1 时必然不同, 说明这条断言真的能抓住那个 bug
  if (z !== 1) {
    ok(Math.round(100 * z) !== Math.round(delta),
       "z=" + z + " 该断言对旧实现敏感(旧值 " + Math.round(100 * z) + " ≠ 新值 " + Math.round(delta.toFixed(0)) + ")");
  }
  d.up();
});

/* ---------- 3. 松手落地的宽度要把可视位移换算回本地宽度（除 z） ---------- */
[1, 1.15, 1.3].forEach((z) => {
  const H = harness({ z, leftW: 238 });
  const x0 = H.splitterLeft("left") + 2.5 * z;
  const d = H.grab("left", x0);
  d.move(x0 + 115);                          // 可视 +115px
  d.up();
  const want = 238 + 115 / z;                // 本地宽度
  ok(near(H.prefs.leftW, want, 0.01),
     "z=" + z + " 松手宽度 = 238 + 115/" + z + " (本地像素)",
     "实际 " + H.prefs.leftW.toFixed(2) + " 期望 " + want.toFixed(2));
});

/* ---------- 4. 右栏: 方向取反 + 同样跟手 ---------- */
[1, 1.15].forEach((z) => {
  const H = harness({ z, rightW: 235 });
  const L = H.splitterLeft("right");
  const x0 = L + 2.5 * z;
  const d = H.grab("right", x0);
  const g0 = H.rx();
  ok(near(g0, L), "z=" + z + " 右栏按下时线在分隔条左沿", "线上实际位置=" + g0 + " 期望=" + L);
  d.move(x0 - 120);                          // 往左拖 → 右栏变宽, 线跟随向左
  ok(near(H.rx() - g0, -120), "z=" + z + " 右栏: 左移 120px → 线在屏幕上左移 120px",
     "线走了 " + (H.rx() - g0).toFixed(1));
  d.up();
  ok(near(H.prefs.rightW, 235 + 120 / z, 0.01),
     "z=" + z + " 右栏变宽 120/" + z + " 本地像素",
     "实际 " + H.prefs.rightW.toFixed(2));
});

/* ---------- 5. clamp: 线不能飘出上下限 ---------- */
{
  const z = 1.3, H = harness({ z, leftW: 238 });
  const L = H.splitterLeft("left");
  const d = H.grab("left", L + 2.5 * z);
  d.move(L + 5000);
  ok(near(H.rx(), L + (430 - 238) * z), "z=" + z + " 左栏顶到上限 430 时线停在 430 处",
     "线上实际位置=" + H.rx() + " 期望=" + (L + 192 * z));
  d.up();
  ok(H.prefs.leftW === 430, "左栏上限 430 生效", String(H.prefs.leftW));
}
{
  const z = 1.15, H = harness({ z, rightW: 235 });
  const L = H.splitterLeft("right");
  const d = H.grab("right", L + 2.5 * z);
  d.move(L - 5000);
  ok(near(H.rx(), L - (460 - 235) * z), "z=" + z + " 右栏顶到上限 460 时线停在 460 处",
     "线上实际位置=" + H.rx() + " 期望=" + (L - 225 * z));
  d.up();
  ok(H.prefs.rightW === 460, "右栏上限 460 生效", String(H.prefs.rightW));
}

/* ---------- 6. 拖很窄 → 折叠（线贴到 0 宽位置） ---------- */
{
  const z = 1.15, H = harness({ z, leftW: 238 });
  const L = H.splitterLeft("left");
  const d = H.grab("left", L + 2.5 * z);
  d.move(L - 188);                           // 238 - 188/1.15 ≈ 74.5 < 90 → 判定折叠
  ok(H.opacities().has("collapsing"), "折叠态有视觉区分(.collapsing)");
  d.up();
  ok(H.prefs.le === true && near(H.rx(), L - 238 * z),
     "折叠后线落到 0 宽位置且 prefs.le=true", "线上实际位置=" + H.rx());
}

/* ---------- 7. z=1 时行为与改动前完全一致（老用户零感知） ---------- */
{
  const H = harness({ z: 1, leftW: 238 });
  const L = H.splitterLeft("left");
  const d = H.grab("left", L + 2);
  d.move(L + 2 + 37);
  ok(H.rx() === Math.round(L + 37), "z=1 左栏位移 37 → 线在 L+37", "线上实际位置=" + H.rx());
  d.up();
  ok(H.prefs.leftW === 275, "z=1 松手宽度 238+37=275", String(H.prefs.leftW));
}

/* ---------- 8. --ui-zoom 没写/写坏 → 按 1 处理，不能出 NaN ---------- */
["", "0", "abc"].forEach((v) => {
  const H = harness({ z: 1, zoomVar: v });
  const L = H.splitterLeft("left");
  const d = H.grab("left", L + 2);
  d.move(L + 2 + 50);
  ok(!Number.isNaN(H.rx()) && near(H.rx() - L, 50),
     "--ui-zoom=" + JSON.stringify(v) + " 时按 1 处理(线跟手, 无 NaN)",
     "线上实际位置=" + H.rx());
});

/* ---------- 9. 拖动期间不能改真实宽度（性能红线） ---------- */
{
  const H = harness({ z: 1.15, leftW: 238 });
  const L = H.splitterLeft("left");
  const d = H.grab("left", L + 2);
  d.move(L + 60); d.move(L + 90); d.move(L + 120);
  ok(H.prefs.leftW === 238, "拖动过程中 prefs.leftW 一次都没变", String(H.prefs.leftW));
  ok(H.bodyCls.has("splitting"), "拖动期间 body.splitting 打开(关掉中间栏指针事件)");
  d.up();
  ok(!H.bodyCls.has("splitting"), "松手后 body.splitting 关闭");
  ok(H.prefs.leftW !== 238, "松手才落地宽度", String(H.prefs.leftW));
}

console.log("\nRESULT: " + (fail ? "FAIL(" + fail + ")" : "ALL PASS") + "  pass=" + pass + " fail=" + fail);
process.exit(fail ? 1 : 0);
