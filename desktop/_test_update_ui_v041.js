/* v0.4.1dsk 检测更新前端行为测试：直接抽取 web/app.js 里的真实代码，在 vm 里跑。
   假 DOM + 假 fetch 队列 + 手动时钟（setInterval 只登记，不真等 500ms）。 */
const fs = require("fs");
const vm = require("vm");

const SRC = fs.readFileSync("web/app.js", "utf8");
const START = SRC.indexOf("/* ================= 自动更新");
const MARK = 'window.addEventListener("pywebviewready", updWireShell);';
const END = SRC.indexOf(MARK, START) + MARK.length;
if (START < 0 || SRC.indexOf(MARK, START) < 0) {
  console.error("FAIL  找不到自动更新代码块");
  process.exit(1);
}
const block = SRC.slice(START, END);

let PASS = 0;
const FAIL = [];
function ok(cond, name) {
  if (cond) { PASS++; console.log("PASS  " + name); }
  else { FAIL.push(name); console.log("FAIL  " + name); }
}

/* ---------- 假 DOM ---------- */
const btn = { hidden: true, disabled: false, onclick: null };
const mbody = { innerHTML: "" };
const bar = { style: { width: "" } };
const pct = { textContent: "" };
const bytes = { textContent: "" };
const $ = (sel) => {
  if (sel === "#btnCheckUpdate") return btn;
  if (sel === "#modal .mbody") return mbody;
  if (sel === "#updBarFill") return bar;
  if (sel === "#updPct") return pct;
  if (sel === "#updBytes") return bytes;
  return null;
};

const modal = { title: "", html: "", buttons: [] };
function openModal(title, html, buttons) {
  modal.title = title;
  modal.html = html;
  modal.buttons = buttons || [];
  mbody.innerHTML = "";
}
function closeModal() { modal.title = "__closed"; }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const listeners = {};
const win = {
  addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
  pywebview: null,
};

let apiImpl = async () => ({ ok: false, error: "no stub" });
let bridgeImpl = null;
const api = (...a) => apiImpl(...a);
function shellBridge() { return bridgeImpl; }

let tickFn = null;
let tickCount = 0;
function fakeSetInterval(fn) { tickFn = fn; return ++tickCount; }
function fakeClearInterval(id) { if (id === tickCount) tickFn = null; }

const sandbox = {
  console, Promise, $, document: { querySelector: $ }, window: win,
  openModal, closeModal, esc, api, shellBridge,
  setInterval: fakeSetInterval, clearInterval: fakeClearInterval,
};
vm.createContext(sandbox);
vm.runInContext(
  block + "\n;globalThis.__upd = { updCheck, updDownload, updWireShell, mdlDownload, updBoth, busy: () => updBusy };\n",
  sandbox);

async function flush() { await new Promise((r) => setTimeout(r, 0)); }

(async function main() {
  /* ---------- 1. 壳能力探测 ---------- */
  let infoCalls = 0;
  btn.hidden = true;
  bridgeImpl = { shell_info: async () => { infoCalls++; return { self_update: true }; } };
  sandbox.__upd.updWireShell();
  await flush();
  ok(infoCalls === 1, "updWireShell 调 shell_info");
  ok(btn.hidden === false, "self_update=true 时显示检测更新按钮");
  ok(typeof btn.onclick === "function", "按钮绑定 updCheck");

  btn.hidden = true;
  bridgeImpl = { shell_info: async () => ({ self_update: false }) };
  sandbox.__upd.updWireShell();
  await flush();
  ok(btn.hidden === true, "self_update=false（源码/安卓）不显示按钮");

  /* ---------- 2. 检测到新版本 → 询问 → 下载 → 进度 → 安装 ---------- */
  bridgeImpl = {
    shell_info: async () => ({ self_update: true }),
    install_update: async () => { bridgeImpl.installs = (bridgeImpl.installs || 0) + 1; return "ok"; },
  };
  const calls = [];
  let state = "check";
  apiImpl = async (path) => {
    calls.push(path);
    if (path === "/api/update/check") {
      return { ok: true, has_update: true, current: "0.4.1dsk", latest: "0.4.2dsk",
               size: 10 * 1024 * 1024, notes: "修复 A\n修复 B" };
    }
    if (path === "/api/model/check") return { ok: true, has_update: false, current: "1.0.0" };
    if (path === "/api/update/download") return { ok: true };
    if (path === "/api/update/progress") {
      if (state === "down") return { ok: true, phase: "downloading", received: 5 * 1024 * 1024, total: 10 * 1024 * 1024 };
      return { ok: true, phase: "ready", received: 10 * 1024 * 1024, total: 10 * 1024 * 1024, path: "C:\\tmp\\new.exe" };
    }
    return { ok: false, error: "unexpected " + path };
  };
  await sandbox.__upd.updCheck();
  ok(modal.title === "发现更新", "弹窗显示更新");
  ok(modal.html.includes("程序：v0.4.1dsk → <b>v0.4.2dsk</b>"), "弹窗显示程序当前/新版本");
  ok(modal.html.includes("10.0 MB"), "弹窗显示安装包大小");
  ok(modal.html.includes("修复 A\n修复 B"), "弹窗显示 Release 说明");
  ok(modal.buttons.length === 2 && modal.buttons[1].t === "更新程序", "有「稍后 / 更新程序」两个按钮");

  state = "down";
  const p = modal.buttons[1].fn();
  await p;
  ok(calls.includes("/api/update/download"), "点立即更新 → POST download");
  ok(typeof tickFn === "function", "开始轮询下载进度");
  await tickFn();
  ok(bar.style.width === "50%" && pct.textContent === "50%", "进度条 50% 与百分比同步");
  ok(bytes.textContent.includes("5.0 MB") && bytes.textContent.includes("10.0 MB"), "进度显示已下载/总量");

  state = "ready";
  await tickFn();
  await flush();
  ok(bridgeImpl.installs === 1, "下载完成 → 调壳 install_update");
  ok(mbody.innerHTML.includes("正在重启"), "安装后提示即将重启");
  ok(tickFn === null, "就绪后停止轮询");

  /* ---------- 3. 已是最新版本 ---------- */
  modal.title = ""; modal.html = "";
  apiImpl = async (path) => {
    if (path === "/api/update/check") return { ok: true, has_update: false, current: "0.4.1dsk" };
    if (path === "/api/model/check") return { ok: true, has_update: false, current: "1.0.0" };
    return { ok: false };
  };
  await sandbox.__upd.updCheck();
  ok(modal.title === "检测更新" && modal.html.includes("均为最新"), "无更新时明确提示均为最新");

  /* ---------- 4. 检查失败 ---------- */
  modal.title = ""; modal.html = "";
  apiImpl = async () => ({ ok: false, error: "仓库还没有发布可用的版本（没有 Release）。" });
  await sandbox.__upd.updCheck();
  ok(modal.title === "检测更新" && modal.html.includes("没有 Release"), "检查失败显示后端原因");

  /* ---------- 5. 下载失败 ---------- */
  modal.title = ""; modal.html = "";
  state = "down";
  apiImpl = async (path) => {
    if (path === "/api/update/check") return { ok: true, has_update: true, current: "0.4.1dsk", latest: "0.4.2dsk" };
    if (path === "/api/model/check") return { ok: true, has_update: false, current: "1.0.0" };
    if (path === "/api/update/download") return { ok: true };
    return { ok: true, phase: "error", error: "下载文件太小，可能不是完整的 exe" };
  };
  await sandbox.__upd.updCheck();
  await modal.buttons[1].fn();
  await tickFn();
  ok(modal.title === "更新失败" && modal.html.includes("下载文件太小"), "下载失败给出原因并可关闭");

  /* ---------- 6. 安装失败 ---------- */
  modal.title = ""; modal.html = "";
  bridgeImpl.install_update = async () => "err:程序目录不可写，请把 exe 放到用户目录后再试";
  state = "ready";
  apiImpl = async (path) => {
    if (path === "/api/update/check") return { ok: true, has_update: true, current: "0.4.1dsk", latest: "0.4.2dsk" };
    if (path === "/api/model/check") return { ok: true, has_update: false, current: "1.0.0" };
    if (path === "/api/update/download") return { ok: true };
    return { ok: true, phase: "ready", received: 10, total: 10 };
  };
  await sandbox.__upd.updCheck();
  await modal.buttons[1].fn();
  await tickFn();
  await flush();
  ok(modal.title === "更新失败" && modal.html.includes("程序目录不可写"), "安装失败去掉 err: 前缀展示原因");

  /* ---------- 7. 仅模型更新 → 下载 → 重新加载 ---------- */
  modal.title = ""; modal.html = "";
  let modelPhase = "down";
  let reloadCalled = false;
  apiImpl = async (path) => {
    if (path === "/api/update/check") return { ok: true, has_update: false, current: "0.4.15dsk" };
    if (path === "/api/model/check") {
      return { ok: true, has_update: true, current: "1.0.0", latest: "1.0.1",
               size: 200 * 1024 * 1024, notes: "更准" };
    }
    if (path === "/api/model/download") return { ok: true };
    if (path === "/api/model/reload") { reloadCalled = true; return { ok: true }; }
    if (path === "/api/model/progress") {
      if (modelPhase === "down") return { ok: true, phase: "downloading", received: 50, total: 200 };
      return { ok: true, phase: "ready", received: 200, total: 200 };
    }
    return { ok: false, error: "unexpected " + path };
  };
  await sandbox.__upd.updCheck();
  ok(modal.buttons.length === 2 && modal.buttons[1].t === "更新模型", "仅模型更新时出现「更新模型」");
  ok(modal.html.includes("模型：1.0.0 → <b>1.0.1</b>"), "模型弹窗显示版本");
  const mp = modal.buttons[1].fn();
  await flush();
  modelPhase = "ready";
  await tickFn();
  await flush();
  await mp;
  ok(reloadCalled, "模型下载完成 → POST /api/model/reload");
  ok(modal.title === "模型已更新", "模型更新完成提示");

  console.log("\n" + PASS + " PASS / " + FAIL.length + " FAIL");
  if (FAIL.length) { console.log("FAILED: " + FAIL.join("; ")); process.exit(1); }
})().catch((e) => { console.error(e); process.exit(1); });
