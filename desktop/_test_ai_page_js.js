/* 内置浏览器 AI 面板的"注入网页 JS"行为回归(v0.4.42)。
 *
 * 为什么要在 node 沙盒里跑: 字符串断言只能证明"写对了字", 证明不了行为 ——
 * 元素序号怎么分配、隐藏元素排不排、点的是不是同一个节点、填值走不走原生 setter,
 * 这些只有真跑一遍才知道。做法同 _test_dm_ft_gate.js: vm 造一个假 DOM。
 *
 * 被测源码: desktop/shell_ai.py 里的 _AI_PAGE_JS(直接读文件抽取, 不是另抄一份)。
 *   MSX_AI_SRC=<路径> 可覆盖 → 变异测试用。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let pass = 0, fail = 0;
const failures = [];
function ok(name, cond, detail) {
  if (cond) { pass++; console.log("PASS  " + name); }
  else { fail++; failures.push(name); console.log("FAIL  " + name + (detail ? "  " + detail : "")); }
}

/* ---------------------------------------------------------------- 抽取被测脚本 */
function loadPageJs() {
  const src = process.env.MSX_AI_SRC ||
    path.join(__dirname, "shell_ai.py");
  const py = fs.readFileSync(src, "utf8");
  const start = py.indexOf('_AI_PAGE_JS = r"""');
  if (start < 0) throw new Error("找不到 _AI_PAGE_JS");
  const from = start + '_AI_PAGE_JS = r"""'.length;
  const end = py.indexOf('"""', from);
  return py.slice(from, end);
}
const PAGE_JS = loadPageJs();

/* ---------------------------------------------------------------- 假 DOM */
function makeEnv(opts) {
  opts = opts || {};
  const log = { clicked: [], dispatched: [], scrolled: [], played: 0, paused: 0 };

  function mkEl(spec) {
    const el = {
      tagName: spec.tag || "DIV",
      innerText: spec.text || "",
      value: spec.value || "",
      disabled: !!spec.disabled,
      isContentEditable: !!spec.editable,
      _attrs: Object.assign({}, spec.attrs || {}),
      _rect: spec.rect || { width: 100, height: 20, top: 10, bottom: 30 },
      getAttribute(k) {
        if (k === "href" && spec.href) return spec.href;
        return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
      },
      getBoundingClientRect() { return Object.assign({ left: 0, right: 100 }, this._rect); },
      scrollIntoView() { log.scrolled.push("intoView"); },
      focus() { log.focused = this; },
      click() { log.clicked.push(spec.id); },
      dispatchEvent(ev) { log.dispatched.push({ id: spec.id, type: ev.type, key: ev.key }); return true; },
    };
    if (spec.form) el.form = { requestSubmit() { log.dispatched.push({ id: spec.id, type: "submit" }); } };
    return el;
  }

  const els = [
    mkEl({ id: "link1", tag: "A", text: "  首页   推荐  ", href: "/home" }),
    mkEl({ id: "btn1", tag: "BUTTON", text: "搜索" }),
    mkEl({ id: "input1", tag: "INPUT", attrs: { placeholder: "搜点什么", type: "text" }, form: true }),
    mkEl({ id: "hidden", tag: "BUTTON", text: "看不见的", rect: { width: 0, height: 0, top: 0, bottom: 0 } }),
    mkEl({ id: "hiddenInput", tag: "INPUT", attrs: { type: "hidden" } }),
    mkEl({ id: "disabledBtn", tag: "BUTTON", text: "禁用", disabled: true }),
    mkEl({ id: "faraway", tag: "A", text: "很远的链接", href: "/far", rect: { width: 90, height: 20, top: 90000, bottom: 90020 } }),
  ];
  const visibleSet = new Set(["link1", "btn1", "input1", "disabledBtn"]);

  const video = opts.noVideo ? null : {
    paused: true, currentTime: 0, duration: 100, muted: false, volume: 0.8,
    play() { log.played++; this.paused = false; return Promise.resolve(); },
    pause() { log.paused++; this.paused = true; },
  };

  const doc = {
    title: "测试页面",
    readyState: "complete",
    documentElement: { scrollHeight: 5000, scrollTop: 0 },
    body: { innerText: "第一段\n\n\n第二段  内容", scrollHeight: 5000 },
    querySelectorAll() { return els; },
    querySelector(sel) { return sel === "video" ? video : null; },
    createTreeWalker() {
      const nodes = [{ nodeValue: "这里有目标词", parentElement: els[1] },
                     { nodeValue: "别的东西", parentElement: els[0] }];
      let i = -1;
      return { nextNode() { i++; return i < nodes.length ? nodes[i] : null; } };
    },
  };

  const sandbox = {
    document: doc,
    location: { href: "https://www.bilibili.com/video/BV1xx" },
    innerHeight: 800,
    scrollY: 0,
    getComputedStyle() { return { visibility: "visible", display: "block", opacity: "1" }; },
    NodeFilter: { SHOW_TEXT: 4 },
    Event: class { constructor(t) { this.type = t; } },
    KeyboardEvent: class { constructor(t, o) { this.type = t; this.key = (o || {}).key; } },
    MouseEvent: class { constructor(t) { this.type = t; } },
    Math, JSON, String, Number, Object, Array, RegExp, Promise, parseFloat, isNaN,
  };
  sandbox.scrollTo = function (x, y) { log.scrolled.push(y); sandbox.scrollY = y; };
  // 假的原生构造器: 让"走原生 setter"这条分支也被跑到(实例自己有 value, 不会递归)
  class FakeInput {}
  Object.defineProperty(FakeInput.prototype, "value", {
    configurable: true,
    set(v) { this.value = v; },
    get() { return this.value; },
  });
  sandbox.HTMLInputElement = FakeInput;
  sandbox.HTMLTextAreaElement = FakeInput;
  vm.createContext(sandbox);
  // 浏览器里 window 就是全局对象 —— 沙盒也要这样, 否则 window.__MSXAI__ 变不成全局
  vm.runInContext("this.window = this; this.globalThis = this;", sandbox);
  // 记录每个元素 id(用于 click 断言)
  els.forEach((e, i) => { e.__id = ["link1", "btn1", "input1", "hidden", "hiddenInput", "disabledBtn", "faraway"][i]; });
  return { sandbox, log, els, video, doc };
}

function run(env, call) {
  const code = "(function(){\n" + PAGE_JS + "\nreturn (" + call + ");\n})()";
  return vm.runInContext(code, env.sandbox);
}

/* ---------------------------------------------------------------- 用例 */
(function T1() {
  const env = makeEnv();
  const snap = run(env, "__MSXAI__.snap(60, 200)");
  ok("T1 snap 返回网址/标题/滚动/正文", snap && snap.url.indexOf("bilibili") > 0
    && snap.title === "测试页面" && snap.scroll.h === 5000 && snap.text.indexOf("第一段") === 0,
    JSON.stringify(snap && snap.scroll));
  const ids = snap.els.map((e) => env.els[e.i].__id);
  ok("T2 隐藏/禁用/超远的元素被过滤, 序号连续",
    ids.join(",") === "link1,btn1,input1" && snap.els.every((e, i) => e.i === i), ids.join(","));
  ok("T3 文本压缩空白 + 输入框带 placeholder",
    snap.els[0].t === "首页 推荐" && snap.els[2].tag === "input" && snap.els[2].t === "搜点什么",
    JSON.stringify(snap.els));
  ok("T4 链接带 href", snap.els[0].h === "/home");
  ok("T5 视频状态被读到", snap.media && snap.media.paused === true && snap.media.dur === 100);
})();

(function T2() {
  const env = makeEnv();
  run(env, "__MSXAI__.snap(60, 200)");
  const r = run(env, "__MSXAI__.click(1)");
  ok("T6 click(1) 点的是第 2 个可见元素", r.ok === true && env.log.clicked.join(",") === "btn1",
    env.log.clicked.join(","));
  ok("T7 点击前先滚到元素", env.log.scrolled.indexOf("intoView") >= 0);
  const bad = run(env, "__MSXAI__.click(99)");
  ok("T8 越界序号给明确错误而不是抛异常", bad.ok === false && /snapshot/.test(bad.msg), bad.msg);
  const stale = run(env, "__MSXAI__.click(-1)");
  ok("T9 负序号也拦住", stale.ok === false);
})();

(function T3() {
  const env = makeEnv();
  run(env, "__MSXAI__.snap(60, 200)");
  const r = run(env, "__MSXAI__.type(2, '猫猫视频', true)");
  ok("T10 填值成功 + 触发 input/change", r.ok === true
    && env.els[2].value === "猫猫视频"
    && env.log.dispatched.some((d) => d.id === "input1" && d.type === "input")
    && env.log.dispatched.some((d) => d.id === "input1" && d.type === "change"), r.msg);
  ok("T11 submit 触发回车 + 表单提交", env.log.dispatched.some((d) => d.type === "keydown" && d.key === "Enter")
    && env.log.dispatched.some((d) => d.type === "submit"));
  const bad = run(env, "__MSXAI__.type(0, 'x', false)");
  ok("T12 往链接里填字被拒绝", bad.ok === false && /不是输入框/.test(bad.msg), bad.msg);
})();

(function T4() {
  const env = makeEnv();
  const r = run(env, "__MSXAI__.scroll(600, null, false)");
  ok("T13 相对滚动 600", r.ok === true && env.sandbox.scrollY === 600, String(env.sandbox.scrollY));
  const b = run(env, "__MSXAI__.scroll(null, null, true)");
  ok("T14 bottom 滚到底(总高-视口)", b.ok === true && env.sandbox.scrollY === 4200, String(env.sandbox.scrollY));
  const t = run(env, "__MSXAI__.scroll(null, 999999, false)");
  ok("T15 绝对位置越界被夹到最大值", t.ok === true && env.sandbox.scrollY === 4200, String(env.sandbox.scrollY));
})();

(function T5() {
  const env = makeEnv();
  const p = run(env, "__MSXAI__.media('play', null)");
  ok("T16 play 调到 video.play", p.ok === true && env.log.played === 1);
  const s = run(env, "__MSXAI__.media('seek', 30)");
  ok("T17 seek 设 currentTime", s.ok === true && env.video.currentTime === 30);
  const bad = run(env, "__MSXAI__.media('xxx', null)");
  ok("T18 未知动作报错", bad.ok === false);
  const env2 = makeEnv({ noVideo: true });
  const nv = run(env2, "__MSXAI__.media('play', null)");
  ok("T19 没有视频时给明确提示", nv.ok === false && /没有视频/.test(nv.msg), nv.msg);
})();

(function T6() {
  const env = makeEnv();
  const r = run(env, "__MSXAI__.find('目标词')");
  ok("T20 页内查找计数并滚动", r.ok === true && r.hits === 1 && env.log.scrolled.indexOf("intoView") >= 0,
    JSON.stringify(r));
  const none = run(env, "__MSXAI__.find('不存在的词')");
  ok("T21 找不到时 hits=0 且不滚动", none.ok === true && none.hits === 0);
})();

(function T7() {
  const env = makeEnv();
  const a = run(env, "typeof __MSXAI__");
  const b = run(env, "typeof __MSXAI__");
  ok("T22 脚本幂等(重复执行不重复定义, 序号表不丢)", a === "object" && b === "object");
  // 先 snap 再执行一次 boot, 元素表应还在
  run(env, "__MSXAI__.snap(60, 100)");
  const keep = run(env, "__MSXAI__.click(1)");
  ok("T23 两次调用之间元素序号表保持有效", keep.ok === true && env.log.clicked.join(",") === "btn1");
})();

(function T8() {
  // 变异哨兵: 版本号被改掉时, 说明脚本被重写过, 提醒同步更新本测试
  ok("T24 助手脚本带版本号 v3(改脚本要一起改测试)", /A\.v === 3|A = window\.__MSXAI__ = \{v: 3\}/.test(PAGE_JS));
})();

console.log("\nRESULT: " + (fail ? "FAIL(" + fail + ")" : "PASS(" + pass + ")"));
if (fail) { console.log("失败: " + failures.join(" | ")); process.exit(1); }
