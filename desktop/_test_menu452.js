/* v0.4.52dsk 行为回归: 深色兜底不再反色媒体 + 整页翻译的采集/回写/还原。
 *
 * 为什么要有这个文件: 这两块都是**注入到别人页面里的 JS**, 用 Python 静态断言只能
 * 证明"源码里写了什么字", 证明不了"跑起来会改哪些元素"。上一版把视频画面整块反色,
 * 恰恰就是一行完全合法、看上去还挺对的 `filter:invert()`。
 *
 * 做法: 从 shell_pages.py / shell_translate.py 里抠出真实的 JS 源码, 配一套最小假 DOM
 * (元素 + inline style + TreeWalker) 在 node vm 里真跑, 然后断言"哪些元素被改了、改成了
 * 什么、还原之后是不是一点不剩"。
 *
 * 用法: node _test_menu452.js        (失败退出码 1)
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HERE = __dirname;
// 支持 MSX_PY_DIR 指向一份被改坏的副本 → 做**变异测试**证明这些断言有判别力
// (把 filter:invert 塞回去, T2/T3 必须立刻 FAIL)。默认测真源码。
const PYDIR = process.env.MSX_PY_DIR || HERE;
let PASS = 0, FAIL = 0;
function ok(name, cond, extra) {
  if (cond) { PASS++; console.log('PASS  ' + name); }
  else { FAIL++; console.log('FAIL  ' + name + (extra ? '  ' + extra : '')); }
}

/** 从 Python 源码里抠 `NAME = r"""..."""` 的正文。 */
function grab(pyFile, name) {
  const src = fs.readFileSync(path.join(PYDIR, pyFile), 'utf8');
  const head = name + ' = r"""';
  const i = src.indexOf(head);
  if (i < 0) throw new Error('找不到 ' + name);
  const s = i + head.length;
  const e = src.indexOf('"""', s);
  return src.slice(s, e);
}

// ---------------------------------------------------------------- 最小假 DOM
function mkStyle() {
  const p = {};
  return {
    _p: p,
    setProperty(k, v, pr) { p[k] = { v: String(v), pr: pr || '' }; },
    getPropertyValue(k) { return p[k] ? p[k].v : ''; },
    getPropertyPriority(k) { return p[k] ? p[k].pr : ''; },
    removeProperty(k) { delete p[k]; },
    has(k) { return Object.prototype.hasOwnProperty.call(p, k); },
    get cssText() { return JSON.stringify(p); }
  };
}

function mkEl(tag, props) {
  const e = Object.assign({
    tagName: tag, id: '', children: [], parentElement: null, textContent: '',
    _bg: 'rgba(0, 0, 0, 0)', _color: 'rgb(0, 0, 0)', _display: 'block',
    style: mkStyle(),
    appendChild(c) { this.children.push(c); c.parentElement = this; return c; },
    remove() {
      if (!this.parentElement) return;
      const i = this.parentElement.children.indexOf(this);
      if (i >= 0) this.parentElement.children.splice(i, 1);
      this.parentElement = null;
    },
    getBoundingClientRect() { return { width: 120, height: 20, left: 0, top: 0 }; },
    closest() { return null; },
    setAttribute() {}, getAttribute() { return null; }
  }, props || {});
  return e;
}

function mkDom(o) {
  o = o || {};
  const all = [];
  const El = (t, p) => { const e = mkEl(t, p); all.push(e); return e; };
  const html = El('HTML', { _bg: o.htmlBg || 'rgb(255, 255, 255)' });
  const head = El('HEAD');
  const body = El('BODY', { _bg: o.bodyBg || 'rgb(255, 255, 255)',
                            _color: o.bodyColor || 'rgb(0, 0, 0)' });
  html.appendChild(head);
  html.appendChild(body);
  const card = El('DIV', { _bg: 'rgb(255, 255, 255)', _color: 'rgb(51, 51, 51)' });
  const video = El('VIDEO');
  const link = El('A', { _bg: 'rgba(0, 0, 0, 0)', _color: 'rgb(10, 88, 202)' });
  const pre = El('PRE', { _bg: 'rgb(245, 245, 245)', _color: 'rgb(30, 30, 30)' });
  body.appendChild(card); body.appendChild(video);
  body.appendChild(link); body.appendChild(pre);
  body.getElementsByTagName = function () {
    const out = [];
    (function walk(n) {
      n.children.forEach(function (c) { out.push(c); walk(c); });
    })(body);
    return out;
  };
  const doc = {
    documentElement: html, head: head, body: body, readyState: 'complete', cookie: '',
    createElement(t) { return El(String(t).toUpperCase()); },
    getElementById(id) {
      // 只认"还挂在文档树上"的节点 —— 真实 DOM 里 remove() 之后 getElementById 就查不到
      return all.find(function (e) {
        if (e.id !== id) return false;
        let n = e;
        while (n) { if (n === html) return true; n = n.parentElement; }
        return false;
      }) || null;
    },
    querySelectorAll() { return []; },
    createTreeWalker(root, what, filter) {
      const nodes = [];
      (function walk(n) {
        n.children.forEach(function (c) {
          if (c._text !== undefined) nodes.push(c);
          walk(c);
        });
      })(root);
      let i = -1;
      return {
        nextNode() {
          while (++i < nodes.length) {
            const v = filter && filter.acceptNode ? filter.acceptNode(nodes[i]) : 1;
            if (v === 1) return nodes[i];
          }
          return null;
        }
      };
    }
  };
  return { doc: doc, all: all, html: html, head: head, body: body,
           card: card, video: video, link: link, pre: pre, El: El };
}

function mkCtx(dom, host) {
  const g = {};
  g.window = g;
  g.document = dom.doc;
  g.location = { hostname: host, href: 'https://' + host + '/',
                 reload() { g.__reloaded = (g.__reloaded || 0) + 1; } };
  g.sessionStorage = { getItem() { return null; }, setItem() {} };
  g.fetch = function () { return Promise.resolve({ ok: true }); };
  g.setInterval = function () { return 1; };
  g.setTimeout = function () { return 1; };
  g.NodeFilter = { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 };
  g.getComputedStyle = function (el) {
    return { backgroundColor: el._bg, color: el._color, display: el._display,
             visibility: 'visible', borderTopWidth: '0px',
             borderTopColor: 'rgba(0, 0, 0, 0)' };
  };
  g.console = console;
  vm.createContext(g);
  return g;
}

function lum(rgb) {
  const m = String(rgb).match(/[\d.]+/g);
  if (!m) return 255;
  return 0.299 * +m[0] + 0.587 * +m[1] + 0.114 * +m[2];
}

// ---------------------------------------------------------------- 深色兜底
const THEME_DARK = grab('shell_pages.py', '_THEME_DARK_JS');
const THEME_LIGHT = grab('shell_pages.py', '_LIGHT_JS');

// T1 百度网盘这类浅色站: 只压背景板/提亮文字, 一个 filter 都不许出现
(function () {
  const dom = mkDom({});
  const ctx = mkCtx(dom, 'pan.baidu.com');
  vm.runInContext(THEME_DARK, ctx);
  const st = dom.doc.getElementById('__msx_dark');
  ok('T1 浅色站: 注入了兜底样式', !!st);
  ok('T2 兜底样式里没有任何 filter(媒体就不会被反色)',
     !!st && !/filter/i.test(st.textContent), st && st.textContent);
  ok('T3 <video> 一个 inline 样式都没被改(播放画面像素级原样)',
     !dom.video.style.has('filter') && dom.video.style.cssText === '{}',
     dom.video.style.cssText);
  const bg = dom.card.style.getPropertyValue('background-color');
  ok('T4 白色卡片被压成深色', !!bg && lum(bg) < 90, bg);
  const fg = dom.link.style.getPropertyValue('color');
  ok('T5 深色链接文字被提亮', !!fg && lum(fg) > 140, fg);
  ok('T6 提亮保留色相(蓝色仍然偏蓝)',
     !!fg && /^rgb\((\d+),(\d+),(\d+)\)$/.test(fg) &&
     (function () {
       const m = fg.match(/\d+/g);
       return +m[2] > +m[0] && +m[2] >= +m[1];
     })(), fg);
  // T7 切回白天: 逐元素还原, 一点 inline 都不剩
  vm.runInContext(THEME_LIGHT, ctx);
  ok('T7 切回白天: 样式元素被移除', !dom.doc.getElementById('__msx_dark'));
  ok('T8 切回白天: 逐元素还原(卡片/链接都没留下 inline 样式)',
     dom.card.style.cssText === '{}' && dom.link.style.cssText === '{}',
     dom.card.style.cssText + ' | ' + dom.link.style.cssText);
})();

// T9 页面本来就是深色 → 一动不动(不能给原生深色的站再压一层)
(function () {
  const dom = mkDom({ bodyBg: 'rgb(16, 16, 16)', htmlBg: 'rgb(16, 16, 16)' });
  const ctx = mkCtx(dom, 'example.com');
  vm.runInContext(THEME_DARK, ctx);
  ok('T9 已深色的页面: 不注入兜底样式、不改任何元素',
     !dom.doc.getElementById('__msx_dark') && dom.card.style.cssText === '{}');
})();

// T10 小黑盒: 用户明确要求保留整页反色
(function () {
  const dom = mkDom({});
  const ctx = mkCtx(dom, 'www.xiaoheihe.cn');
  vm.runInContext(THEME_DARK, ctx);
  const st = dom.doc.getElementById('__msx_dark');
  ok('T10 小黑盒保留整页反色(invert)',
     !!st && /invert/.test(st.textContent), st && st.textContent);
})();

// T11 B站: 走原生深色 cookie, 不做任何反色
(function () {
  const dom = mkDom({});
  const ctx = mkCtx(dom, 'www.bilibili.com');
  vm.runInContext(THEME_DARK, ctx);
  const st = dom.doc.getElementById('__msx_dark');
  ok('T11 B站不注入反色样式', !st);
  ok('T12 B站仍先压深色底(防白闪)',
     /0f0f0f/i.test(dom.html.style.getPropertyValue('background') || ''),
     dom.html.style.getPropertyValue('background'));
})();

// ---------------------------------------------------------------- 整页翻译
const TR_COLLECT = grab('shell_translate.py', '_TR_COLLECT_JS');
const TR_RESTORE = grab('shell_translate.py', '_TR_RESTORE_JS');
const TR_STATE = grab('shell_translate.py', '_TR_STATE_JS');
const TR_APPLY = grab('shell_translate.py', '_TR_APPLY_TMPL');

function mkTrDom() {
  const dom = mkDom({});
  const t = function (text, parent) {
    const n = { nodeValue: text, _text: text, parentElement: parent, children: [] };
    parent.children.push(n);
    return n;
  };
  const p1 = dom.El('P', { _bg: 'rgba(0, 0, 0, 0)', _color: 'rgb(20, 20, 20)' });
  const p2 = dom.El('P', { _bg: 'rgba(0, 0, 0, 0)', _color: 'rgb(20, 20, 20)' });
  const p3 = dom.El('P', { _bg: 'rgba(0, 0, 0, 0)', _color: 'rgb(20, 20, 20)' });
  const code = dom.El('CODE', { _bg: 'rgba(0, 0, 0, 0)', _color: 'rgb(20, 20, 20)' });
  // <code> 里的文本必须被跳过 —— 翻代码只会把示例改坏
  code.closest = function () { return {}; };
  dom.body.appendChild(p1); dom.body.appendChild(p2);
  dom.body.appendChild(p3); dom.body.appendChild(code);
  const n1 = t('  Hello world, this is a test.  ', p1);
  const n2 = t('Another English sentence here.', p2);
  const n3 = t('这已经是中文了，不需要翻译。', p3);
  const n4 = t('const x = 1;', code);
  return { dom: dom, n1: n1, n2: n2, n3: n3, n4: n4 };
}

(function () {
  const d = mkTrDom();
  const ctx = mkCtx(d.dom, 'example.com');
  const r = vm.runInContext(TR_COLLECT, ctx);
  ok('T13 采集: 只收外文文本块', r.items.length === 2, JSON.stringify(r.items));
  ok('T14 采集: 纯中文段落不发', r.items.indexOf('这已经是中文了，不需要翻译。') < 0);
  ok('T15 采集: <code> 里的文本跳过', r.items.indexOf('const x = 1;') < 0);
  ok('T16 采集: 文本已 trim', r.items[0] === 'Hello world, this is a test.', r.items[0]);

  const map = { 'Hello world, this is a test.': '你好世界，这是一次测试。',
                'Another English sentence here.': '这里还有一句英文。' };
  const applyJs = TR_APPLY.trimEnd().replace(/\}\)\(\)$/, '})(' + JSON.stringify(map) + ')');
  const a = vm.runInContext(applyJs, ctx);
  ok('T17 回写: 两处都替换成功', a.hits === 2, JSON.stringify(a));
  ok('T18 回写: 保留原有前后空白', d.n1.nodeValue === '  你好世界，这是一次测试。  ',
     JSON.stringify(d.n1.nodeValue));
  ok('T19 回写: 中文段落没被动', d.n3.nodeValue === '这已经是中文了，不需要翻译。');
  ok('T20 回写: 标记已翻译', vm.runInContext(TR_STATE, ctx).on === true);

  // 再回写一次不能重复记录原文(否则"显示原文"还原出来的是译文)
  vm.runInContext(applyJs, ctx);
  const st2 = vm.runInContext(TR_STATE, ctx);
  ok('T21 重复回写不重复记原文', st2.n === 2, JSON.stringify(st2));

  const back = vm.runInContext(TR_RESTORE, ctx);
  ok('T22 显示原文: 还原成功', back.restored === 2, JSON.stringify(back));
  ok('T23 显示原文: 文本逐字回到原样',
     d.n1.nodeValue === '  Hello world, this is a test.  ' &&
     d.n2.nodeValue === 'Another English sentence here.');
  ok('T24 显示原文: 状态复位', vm.runInContext(TR_STATE, ctx).on === false);
})();

console.log('\nRESULT: ' + (FAIL ? 'FAIL(' + FAIL + ')' : 'ALL_PASS (' + PASS + ' checks)'));
process.exit(FAIL ? 1 : 0);
