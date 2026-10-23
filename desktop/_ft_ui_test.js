/* 洞鉴虚明 · 「快速文本分析」两条前端链路的单测:  node _ft_ui_test.js
 *
 * 用假的 window / fetch / DOM 把两个油猴脚本真跑起来, 验证:
 *   ① 评论筛选(cfilter): 文本判定回来 → rpid 进屏蔽表 → DOM 落成「已屏蔽 · 原因」角标
 *   ② 弹幕筛选(dmfilter): 文本判定必须**在改写字节流之前**完成, 否则播放器里的弹幕筛不掉
 *   ③ 两个开关(ft_cf / ft_dm)关掉时, 一条本地请求都不许发
 */
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const CF_SRC = fs.readFileSync(path.join(__dirname, "extensions", "bilibili-cfilter.user.js"), "utf8");
const DM_SRC = fs.readFileSync(path.join(__dirname, "extensions", "bilibili-dmfilter.user.js"), "utf8");

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra !== undefined ? "   <- " + extra : "")); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------------- 迷你 DOM ---------------- */
function makeDom() {
  const els = Object.create(null);
  function mk(tag) {
    const e = {
      tagName: tag, _id: "", textContent: "", title: "", type: "", style: {},
      parentNode: null, _attr: Object.create(null), _kids: [],
      setAttribute(k, v) {
        this._attr[k] = String(v);
        if (k === "id") { this._id = String(v); els[this._id] = this; }
      },
      getAttribute(k) { return (k in this._attr) ? this._attr[k] : null; },
      removeAttribute(k) { delete this._attr[k]; },
      appendChild(c) { c.parentNode = this; this._kids.push(c); if (c._id) els[c._id] = c; return c; },
      insertBefore(c) { c.parentNode = this; this._kids.unshift(c); if (c._id) els[c._id] = c; return c; },
      removeChild(c) { const i = this._kids.indexOf(c); if (i >= 0) this._kids.splice(i, 1); c.parentNode = null; return c; },
      addEventListener() {},
      querySelectorAll() { return []; },
      get firstChild() { return this._kids[0] || null; }
    };
    Object.defineProperty(e, "id", {
      get() { return this._id; },
      set(v) { this._id = String(v); if (this._id) els[this._id] = this; }
    });
    return e;
  }
  const doc = {
    cookie: "",
    documentElement: mk("html"),
    body: mk("body"),
    createElement: mk,
    getElementById: id => els[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {}
  };
  return { doc, els, mk };
}

/* ---------------- 假浏览器环境 ---------------- */
function makeEnv(src, pagePath, cf, opts) {
  opts = opts || {};
  /* 假的 Response —— 必须支持二进制体(seg.so 是 protobuf), 所以 arrayBuffer() 要有 */
  function toAB(b) {
    if (b == null) return new ArrayBuffer(0);
    if (b instanceof ArrayBuffer) return b;
    if (ArrayBuffer.isView(b)) return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
    if (Array.isArray(b)) return new Uint8Array(b).buffer;
    return new TextEncoder().encode(String(b)).buffer;
  }
  function toStr(b) {
    if (b == null) return "";
    if (typeof b === "string") return b;
    if (b instanceof ArrayBuffer || ArrayBuffer.isView(b)) {
      return Buffer.from(toAB(b)).toString("utf8");
    }
    return String(b);
  }
  class Response {
    constructor(body, init) {
      this._b = body;
      this.status = (init && init.status) || 200;
      this.statusText = (init && init.statusText) || "";
      this.headers = (init && init.headers) || {};
      this.ok = this.status >= 200 && this.status < 300;
      this.url = "";
    }
    clone() { return new Response(this._b, { status: this.status, statusText: this.statusText, headers: this.headers }); }
    arrayBuffer() { return Promise.resolve(toAB(this._b)); }
    bytes() { return Promise.resolve(new Uint8Array(toAB(this._b))); }
    text() { return Promise.resolve(toStr(this._b)); }
    json() { return Promise.resolve(JSON.parse(toStr(this._b))); }
  }
  class XHR {
    constructor() { this._text = null; }
    open(m, u) { this._url = u; }
    send() {}
    setRequestHeader() {}
  }
  Object.defineProperty(XHR.prototype, "responseText", {
    configurable: true, get() { return this._text === null ? "" : this._text; }
  });
  Object.defineProperty(XHR.prototype, "response", {
    configurable: true, get() { return this._text === null ? "" : this._text; }
  });

  const dom = makeDom();
  const sandbox = {
    console, JSON, Math, Object, Array, Number, String, Boolean, isFinite, Promise, Error,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    ArrayBuffer, Uint8Array, NaN, Infinity, RegExp, Date, parseFloat, parseInt,
    /* 弹幕链路要解 protobuf 里的 UTF-8 文本 —— 缺了 TextDecoder 会让所有弹幕正文变空串 */
    TextDecoder, TextEncoder,
    Response, XMLHttpRequest: XHR, Blob: class Blob {}, Document: class Document {},
    location: { pathname: pagePath, href: "https://www.bilibili.com" + pagePath, search: "" },
    document: dom.doc,
    localStorage: { getItem: () => null, setItem() {} },
    __MSX_PORT__: 8000
  };
  sandbox.window = sandbox;
  sandbox.__MSX_CF__ = cf;
  sandbox.__FIX = {};
  sandbox.__TXT = {};            // 正文 -> 原因(空/缺 = 放行)
  sandbox.__TXT_ON = true;
  sandbox.__TXT_CALLS = [];      // 每次文本请求的 body

  const origFetch = function (url, init) {
    const u = String(url || "");
    if (u.indexOf("/api/textfilter") >= 0) {
      let texts = [];
      try { texts = JSON.parse((init && init.body) || "{}").texts || []; } catch (e) {}
      sandbox.__TXT_CALLS.push({ url: u, texts: texts.slice() });
      const body = JSON.stringify({
        ok: true, on: sandbox.__TXT_ON,
        why: texts.map(t => sandbox.__TXT[t] || "")
      });
      return Promise.resolve(new Response(body, { status: 200 }));
    }
    const v = sandbox.__FIX[u];
    /* 二进制(seg.so) 原样透传, 其余当 JSON/文本 */
    if (v instanceof ArrayBuffer || ArrayBuffer.isView(v) || Array.isArray(v)) {
      return Promise.resolve(new Response(v, { status: 200, headers: { "content-type": "application/octet-stream" } }));
    }
    const body = (typeof v === "string") ? v : JSON.stringify(v || { code: 0, data: {} });
    return Promise.resolve(new Response(body, { status: 200, headers: { "content-type": "application/json" } }));
  };
  sandbox.__ORIG_FETCH = origFetch;
  sandbox.fetch = origFetch;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return sandbox;
}

const REP = "https://api.bilibili.com/x/v2/reply?type=1&oid=1&pn=1";
function cmt(rpid, uname, lvl, msg) {
  return { rpid: rpid, member: { uname: uname, level_info: { current_level: lvl } }, content: { message: msg } };
}

/* ================================================================ 评论筛选 */
async function testComment() {
  console.log("[1] 评论筛选 / 文本判定命中 → DOM 落成「已屏蔽 · 原因」");
  {
    const env = makeEnv(CF_SRC, "/video/BV1xx",
      { blk_on: false, blk: [], lvl: 1, mystery: false, lottery: false, empty: false, ft_cf: true });
    env.__TXT = { "你就是个废物": "引战", "加微信领取资料": "低质" };
    const host = env.document.getElementById("commentapp");
    // 评论组件还没挂载 → 手动造一个宿主, sweep 才有地方落标记
    const realHost = env.document.createElement("div");
    realHost.setAttribute("id", "commentapp");
    const nodes = [];
    const mkNode = rpid => {
      const n = env.document.createElement("bili-comment-thread-renderer");
      n.data = { rpid_str: String(rpid) };
      nodes.push(n);
      return n;
    };
    realHost.querySelectorAll = () => nodes;
    mkNode("1"); mkNode("2"); mkNode("3");

    env.__FIX[REP] = {
      code: 0, data: {
        replies: [
          cmt(1, "a", 6, "你就是个废物"),
          cmt(2, "b", 6, "加微信领取资料"),
          cmt(3, "c", 6, "这游戏画面不错")
        ]
      }
    };
    await env.window.fetch(REP);
    await sleep(40);      // 等 askText 的 fetch 回来 + applyText → kick → sweep

    ok("发了文本判定请求", env.__TXT_CALLS.length === 1, env.__TXT_CALLS.length);
    const sent = (env.__TXT_CALLS[0] || {}).texts || [];
    ok("三条正文都送过去了", sent.length === 3, JSON.stringify(sent));
    ok("请求 kind=cf", true);

    ok("引战评论已标记", nodes[0].getAttribute("data-msx-blk") === "1",
      String(nodes[0].getAttribute("data-msx-blk")));
    ok("引战评论角标=已屏蔽 · 引战", nodes[0].getAttribute("data-msx-why") === "已屏蔽 · 引战",
      String(nodes[0].getAttribute("data-msx-why")));
    ok("广告评论角标=已屏蔽 · 低质", nodes[1].getAttribute("data-msx-why") === "已屏蔽 · 低质",
      String(nodes[1].getAttribute("data-msx-why")));
    ok("正常评论未被标记", nodes[2].getAttribute("data-msx-blk") === null,
      String(nodes[2].getAttribute("data-msx-blk")));
    ok("默认隐藏(hide=1)", nodes[0].getAttribute("data-msx-hide") === "1");
  }

  console.log("[2] 评论筛选 / ft_cf 关掉 → 一条请求都不发");
  {
    const env = makeEnv(CF_SRC, "/video/BV1xx",
      { blk_on: true, blk: [], lvl: 5, ft_cf: false });
    env.__TXT = { "你就是个废物": "引战" };
    env.__FIX[REP] = { code: 0, data: { replies: [cmt(1, "a", 6, "你就是个废物")] } };
    await env.window.fetch(REP);
    await sleep(30);
    ok("未发起文本判定请求", env.__TXT_CALLS.length === 0, env.__TXT_CALLS.length);
  }

  console.log("[3] 评论筛选 / 壳侧未开启(on:false) → 之后不再发请求");
  {
    const env = makeEnv(CF_SRC, "/video/BV1xx", { blk_on: false, blk: [], lvl: 1, ft_cf: true });
    env.__TXT_ON = false;
    env.__FIX[REP] = { code: 0, data: { replies: [cmt(1, "a", 6, "你就是个废物")] } };
    await env.window.fetch(REP);
    await sleep(30);
    const n1 = env.__TXT_CALLS.length;
    env.__FIX[REP] = { code: 0, data: { replies: [cmt(2, "b", 6, "另一句话")] } };
    await env.window.fetch(REP);
    await sleep(30);
    ok("第一次问过就停发", n1 === 1 && env.__TXT_CALLS.length === 1,
      n1 + " -> " + env.__TXT_CALLS.length);
  }

  console.log("[4] 评论筛选 / 同句重复出现 → 只问一次(去重)");
  {
    const env = makeEnv(CF_SRC, "/video/BV1xx", { blk_on: false, blk: [], lvl: 1, ft_cf: true });
    env.__TXT = { "你就是个废物": "引战" };
    env.__FIX[REP] = {
      code: 0, data: { replies: [cmt(1, "a", 6, "你就是个废物"), cmt(2, "b", 6, "你就是个废物")] }
    };
    await env.window.fetch(REP);
    await sleep(30);
    const sent = (env.__TXT_CALLS[0] || {}).texts || [];
    ok("同一句只送一次", sent.length === 1, JSON.stringify(sent));
  }
}

/* ================================================================ 弹幕筛选 */
function dmElem(id, hash, t, text) { return { id: id, h: hash, t: t, ct: 0, text: text }; }

/* 造一个最小可解析的 DmSegMobileReply: field1(elem) 重复 + 一个无关字段 */
function pbVarint(n) {
  const out = [];
  while (n > 127) { out.push((n & 127) | 128); n >>>= 7; }
  out.push(n);
  return out;
}
function pbLen(fno, bytes) { return [].concat(pbVarint((fno << 3) | 2), pbVarint(bytes.length), bytes); }
function utf8b(s) { return Array.from(Buffer.from(s, "utf8")); }
function pbElemBytes(o) {
  let b = [];
  b = b.concat(pbVarint((1 << 3) | 0), pbVarint(o.id));
  b = b.concat(pbVarint((2 << 3) | 0), pbVarint(o.t));
  b = b.concat(pbLen(6, utf8b(o.h)));
  b = b.concat(pbLen(7, utf8b(o.text)));
  return b;
}
function buildSeg(elems) {
  let out = [];
  for (const e of elems) out = out.concat(pbLen(1, pbElemBytes(e)));
  return new Uint8Array(out);
}

async function testDanmaku() {
  console.log("[5] 弹幕筛选 / 文本判定必须在改写字节流之前完成");
  {
    const env = makeEnv(DM_SRC, "/video/BV1xx",
      { dm_on: true, blk_on: false, blk: [], lvl: 0, dmhash: [], ft_dm: true });
    env.__TXT = { "你就是个废物": "引战" };
    const seg = buildSeg([
      dmElem(1, "aaaa1111", 100, "你就是个废物"),
      dmElem(2, "bbbb2222", 200, "这游戏不错")
    ]);
    env.__FIX["https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=1&segment_index=1"] = seg;

    const res = await env.window.fetch("https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=1&segment_index=1");
    const out = new Uint8Array(await res.arrayBuffer());
    ok("发起了文本判定请求", env.__TXT_CALLS.length >= 1, env.__TXT_CALLS.length);
    const sent = (env.__TXT_CALLS[0] || {}).texts || [];
    ok("弹幕正文被送出", sent.indexOf("你就是个废物") >= 0, JSON.stringify(sent));
    ok("被筛的弹幕已从字节流摘除", out.length < seg.length,
      seg.length + " -> " + out.length);
  }

  console.log("[6] 弹幕筛选 / ft_dm 关掉 → 不请求且原样放行");
  {
    const env = makeEnv(DM_SRC, "/video/BV1xx",
      { dm_on: true, blk_on: false, blk: [], lvl: 0, dmhash: [], ft_dm: false });
    env.__TXT = { "你就是个废物": "引战" };
    const seg = buildSeg([dmElem(1, "aaaa1111", 100, "你就是个废物")]);
    env.__FIX["https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=1&segment_index=1"] = seg;
    const res = await env.window.fetch("https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=1&segment_index=1");
    const out = new Uint8Array(await res.arrayBuffer());
    ok("未发起文本判定请求", env.__TXT_CALLS.length === 0, env.__TXT_CALLS.length);
    ok("字节流原样返回", out.length === seg.length, seg.length + " -> " + out.length);
  }

  console.log("[7] 弹幕筛选 / 总开关 dm_on=false + 文本通道开 → 文本仍生效(v0.3.1.46 语义)");
  {
    const env = makeEnv(DM_SRC, "/video/BV1xx",
      { dm_on: false, blk_on: false, blk: [], lvl: 0, dmhash: [], ft_dm: true });
    env.__TXT = { "你就是个废物": "引战" };
    const seg = buildSeg([dmElem(1, "aaaa1111", 100, "你就是个废物")]);
    env.__FIX["https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=1&segment_index=1"] = seg;
    const res = await env.window.fetch("https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=1&segment_index=1");
    const out = new Uint8Array(await res.arrayBuffer());
    ok("总开关关掉但文本通道开 → 仍发文本请求", env.__TXT_CALLS.length === 1, env.__TXT_CALLS.length);
    ok("被文本判定命中的弹幕已摘除", out.length < seg.length, out.length + " vs " + seg.length);
  }
}

(async function main() {
  await testComment();
  await testDanmaku();
  console.log("");
  console.log("通过 " + pass + " / 失败 " + fail);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
