/* v0.4.3dsk: 搜索按钮自适应避让的纯逻辑测试(假 DOM, 不发请求)。
   从 web/app.js 抽真实 fitSearchBtn 源码运行, 避免测试和实现各写一套。 */
const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "web", "app.js"), "utf8").replace(/\r\n/g, "\n");
let pass = 0, fail = [];

function ok(cond, name, extra) {
  if (cond) { pass++; return; }
  fail.push(name + (extra === undefined ? "" : " -> " + JSON.stringify(extra)));
  console.log("FAIL " + name + (extra === undefined ? "" : " -> " + JSON.stringify(extra)));
}

function extractFn(name, text) {
  const start = text.indexOf("function " + name + "(");
  if (start < 0) throw new Error("web/app.js 里找不到 " + name);
  const open = text.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return text.slice(start, i + 1);
  }
  throw new Error(name + " 大括号不闭合");
}

const fnSrc = extractFn("fitSearchBtn", SRC);
const factory = new Function("$", "getComputedStyle",
  "let fitPad0 = 0;\n" + fnSrc + "\nreturn fitSearchBtn;");

function el(left, opts) {
  const o = opts || {};
  const right = o.right === undefined ? left + 28 : o.right;
  return {
    hidden: !!o.hidden,
    style: {},
    children: [],
    getClientRects: () => (o.visible === false || o.hidden ? [] : [{}]),
    getBoundingClientRect: () => ({ left, right, width: right - left, height: 28 }),
  };
}

function rig(opts) {
  const o = opts || {};
  const box = {
    style: {},
    getBoundingClientRect: () => ({
      left: o.boxLeft === undefined ? 501.5 : o.boxLeft,
      right: o.boxRight === undefined ? 901.5 : o.boxRight,
      width: o.boxWidth === undefined ? 400 : o.boxWidth,
      height: 28,
    }),
  };
  const wrap = el(o.wrapLeft === undefined ? 909.5 : o.wrapLeft, { right: 1391 });
  wrap.children = o.children || [];
  const $ = (sel) => ({
    "#topSearch": box,
    "#btnSearch": el(0),
    "#topRightWrap": wrap,
  }[sel]);
  const fit = factory($, () => ({ paddingRight: o.pad0 || "9px" }));
  return { fit, box };
}

// 1) 截图同款几何: 头像左溢出 70.8px -> 右内边距 9+71
{
  const { fit, box } = rig({ children: [el(830.7), el(1180), el(1250)] });
  fit();
  ok(box.style.paddingRight === "80px", "压住 70.8px -> paddingRight 80px", box.style.paddingRight);
}

// 2) 空间充足 -> 不产生任何偏移
{
  const { fit, box } = rig({ children: [el(912), el(1180), el(1250)] });
  fit();
  ok(box.style.paddingRight === "9px", "未压住 -> 保持 9px", box.style.paddingRight);
}

// 3) 头像未登录隐藏时, 以右侧组里下一个可见控件(排序/清空/芯片)为准
{
  const { fit, box } = rig({
    children: [el(0, { visible: false, hidden: true }), el(850), el(1180)],
  });
  fit();
  ok(box.style.paddingRight === "61px", "头像隐藏 -> 按下一个可见控件避让", box.style.paddingRight);
}

// 4) 右侧组全隐藏(设置页) -> 不避让
{
  const { fit, box } = rig({
    children: [el(0, { visible: false }), el(0, { visible: false }), el(0, { visible: false })],
  });
  fit();
  ok(box.style.paddingRight === "9px", "右侧组全隐藏 -> 9px", box.style.paddingRight);
}

// 5) 极端窄窗: 避让宽度必须夹住, 不能把按钮顶出搜索框
{
  const { fit, box } = rig({
    boxLeft: 300, boxRight: 580, boxWidth: 280,
    children: [el(100)],
  });
  fit();
  ok(box.style.paddingRight === "189px", "窄窗夹紧到 宽-100", box.style.paddingRight);
}

// 6) 多次调用不叠加(不能每次把上一轮内联值当基准)
{
  const { fit, box } = rig({ children: [el(830.7)] });
  fit(); fit(); fit();
  ok(box.style.paddingRight === "80px", "重复调用不累计", box.style.paddingRight);
}

// 7) 0.2px 级别的溢出也补 1px, 避免亚像素仍压住按钮
{
  const { fit, box } = rig({ children: [el(901.3)] });
  fit();
  ok(box.style.paddingRight === "10px", "亚像素溢出向上取整", box.style.paddingRight);
}

// 8) 静态接线: 启动一次 / 窗口缩放 / 右侧组尺寸变化 / 登录态变化
ok(SRC.indexOf("function fitSearchBtn()") > 0, "app.js 有 fitSearchBtn");
ok(SRC.indexOf("  fitSearchBtn();\n  window.addEventListener(\"resize\", fitSearchBtn);") > 0,
  "启动即量一次 + resize 监听");
ok(SRC.indexOf("new ResizeObserver(fitSearchBtn)") > 0, "ResizeObserver 监听右侧组");
ok(SRC.indexOf("$$(\"#topRightWrap > *\").forEach((el) => ro.observe(el));") > 0,
  "右侧组每个子项都观察(芯片/头像显隐会改变宽度)");
const syncBlock = SRC.slice(SRC.indexOf("function syncTopAvatars()"), SRC.indexOf("// 点头像 = 进"));
ok(syncBlock.indexOf("fitSearchBtn();") > 0, "syncTopAvatars 后立即重排");

console.log("\nRESULT: " + (fail.length ? "FAIL " + fail.length : "OK") + " (" + pass + " PASS)");
process.exit(fail.length ? 1 : 0);
