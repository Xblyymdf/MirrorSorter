/* 洞鉴虚明 · 弹幕筛选 + 弹幕数据源(v0.3.1.33) —— 注入脚本(document-start, MAIN world)
 *
 * 为什么改响应体而不是改 DOM:
 *   播放器把弹幕画在 canvas 上, DOM 里没有可删的节点; 弹幕数据永远来自
 *   api.bilibili.com/x/v2/dm/web/seg.so(protobuf 二进制), 于是在 document-start
 *   劫持 fetch / XMLHttpRequest 的**二进制响应体**, 就地摘掉不该显示的弹幕再交给播放器。
 *   protobuf 只按 wire format 走(不需要 schema): DmSegMobileReply{ repeated elems=1 },
 *   DanmakuElem{ 2=progress, 6=midHash, 7=content, 8=ctime, 12=idStr, 13=attr } ——
 *   保留的条目按**原始字节**原样回填, 不重新序列化, 不会碰坏 B站 的解析。
 *
 * 弹幕接口只给 midHash(= crc32(十进制mid) 的8位hex, 2026-10-09 实证), 没有昵称/等级/简介。
 * 所以「弹幕评论筛选」各规则在弹幕侧的落地方式:
 *   · 黑名单  → 壳注入 __MSX_CF__.dmhash(已知 mid 正算出的 hash 集合), O(1) 查表 —— 精确;
 *   · 抽奖/空首页/无简介 → midHash 由壳后台 CRC32 逆推出 mid(/api/bili/dmflags,
 *     只读本地缓存), 缓存补上后生效; 没逆出来的一律放行 —— 宁可漏杀不误杀。
 * ★ 弹幕侧**没有等级筛选**(v0.3.1.45dsk 移除): 弹幕接口不带等级, 要判等级只能靠
 *   「哈希逆推 → 候选 mid → 联网查账号资料」, 而一个 8 位哈希平均撞出 6 个候选
 *   (最多 36), 一个播放页就是上千次 B站请求 —— 实测直接把账号打进 -352 风控,
 *   风控期间该规则反而 100% 失效。等级筛选只保留**评论侧**(评论接口自带
 *   member.level_info, 纯本地零请求), 见 bilibili-cfilter.user.js。
 *
 * ★ v0.3.1.46dsk: 「弹幕屏蔽总开关」(dm_on) 只关**按发送者身份**的规则(黑名单/
 *   抽奖/空首页/无简介 —— 这些都要认人, 都要查资料)。「快速文本分析」(ft_dm) 判的是
 *   「这条说了什么」, 跟谁发的一点关系没有, 是**另一条独立通道**, 不受该开关影响。
 *   设置页对用户就是这么承诺的("与上面的弹幕屏蔽总开关完全独立")。旧代码写成
 *   `NEED_TEXT = DM_ON && !!CF.ft_dm`, 把两条通道焊死在一起 —— 总开关一关, 文本通道
 *   连一个请求都不发, fastText 在弹幕侧直接 100% 失效(实测用户 m_dm_on=0 + ft_on=1
 *   正是这个组合)。评论侧 cfilter 一直是 `!!CF.ft_cf`, 没有这个耦合。
 *
 * 同时承担「弹幕列表」面板的数据源: 解析出的(时间/内容/发送时间/midHash)发布到
 * window.__msxDanmaku, 由 bilibili-ownui 的右栏面板订阅渲染; 面板点某行 →
 * window.__msxDanmaku.resolve(midHash) / resolveFull(midHash) → 壳 /api/bili/dmmid(逆推+消歧):
 * 唯一解直接开主页; 多候选回 cands 由面板弹框让用户自己挑。
 *
 * v0.3.1.26: 被屏蔽的弹幕**不再直接丢掉**, 另存 FEED.hidden(带 why 原因标签) ——
 * 面板上的「显示屏蔽」开关只切这个列表的视图(rows / rows+hidden), 播放器里的过滤
 * 行为完全不受影响(该拦的那份字节照旧回填)。
 * 播放器只按需拉分段, 这里在它拉第一段后把**其余分段也补齐**(URL 加 &_msx=1 标记,
 * 钩子见到即跳过 —— 不自己拦自己)。
 *
 * v0.3.1.28(用户反馈「切隐藏/显示屏蔽, 列表条数一条没变」): 分类过去在**解析那一刻**
 * 定死, 而账号画像是壳在后台**限速**补的 —— 分段到手时画像基本都还没到, 且同一个 hash
 * 每页只问一次(ASKED 永久去重), 于是补上的画像永远进不了界面; 加上 ownui 的列表渲染
 * 是**只增不减**(if end > R), 源数据变了也重画不出来。现在: 原始弹幕全存 FEED.all,
 * 画像到位就 refilter() 整体重算 rows/hidden, 并用有限次 maybeRetry() 回问没查清的 hash。
 * v0.3.1.29: 无行为改动(壳侧 -404「账号不存在」处理见 app.py)。
 * v0.3.1.33(用户反馈「弹幕列表一直是空的, 有概率出现」): 补取分段的逻辑重写。
 *   老写法有四处会让人"永远停在「加载中…」", 逐个修掉:
 *     ① 只在播放器那次 seg.so **被拦到**时才触发补取 —— 没拦到就谁也不补, 列表恒空;
 *     ② 换视频时把"正在跑那一轮"的守卫令牌写掉, 新视频那一轮被 busy 挡在门外,
 *        旧轮跑完就退出 → 谁都不再补; 旧轮的响应还会串进新视频的列表;
 *     ③ 分段"解析前就记账", 一旦解析失败该段被永久跳过;
 *     ④ 播放器那次请求带 pull_mode/ps/pe(实测只给半段 52KB / 完整段 98KB),
 *        却按"整段到手"记账 → 自取跳过这一段, 列表少掉半段弹幕。
 *   现在: 只有**自取完整取回**一段才记账; 换视频用代次(gen)让旧轮自行作废;
 *   另有自愈看门狗(1.5s 起, 每 4s 一轮, 最多 8 轮)在"一条都没到"时自己认领 oid 重试。
 *
 * 安全性: 只处理 seg.so 的二进制响应; 任何解析异常一律原样放行, 绝不影响播放器。
 */
(function () {
  "use strict";
  if (window.__msxDm) return;
  window.__msxDm = 1;

  var CF = window.__MSX_CF__ || null;
  if (!CF || typeof CF !== "object") CF = {};

  /* v0.3.1.32: 「弹幕屏蔽总开关」(__MSX_CF__.dm_on, 缺省视为开)。
     关掉后**按发送者身份**的弹幕规则一次性失效 —— 不建黑名单表、不问壳要画像(零请求)、
     那几条规则在 blkWhy 里恒不命中 → 播放器里的弹幕**按身份规则看是原样保留**;
     「弹幕列表」面板照旧可用(只是不过滤)。评论筛选走的是 cfilter / ownui, 与这里无关。
     ★ v0.3.1.46dsk: 它**管不到**「快速文本分析」(见下面 NEED_TEXT) —— 那是"说了什么",
       不是"谁发的"。旧注释写的"所有弹幕侧规则一次性失效"正是这个误解的源头。 */
  var DM_ON = CF.dm_on !== false;

  var MYSTERY = DM_ON && !!CF.mystery;
  var LOT = DM_ON && !!CF.lottery;
  var EMPTY = DM_ON && !!CF.empty;
  /* 账号级规则只剩这三项(都默认关) —— 等级不再进这条链路, 于是默认配置下
     弹幕侧**一个画像请求都不发**, 纯本地(黑名单查表 + fastText)判定。 */
  var NEED_FLAGS = MYSTERY || LOT || EMPTY;
  /* v0.3.1.43dsk: 「快速文本分析」对弹幕生效(CF.ft_dm, 由壳在总开关+作用范围都开时注入 true)。
     与账号画像完全独立 —— 那是"谁发的", 这是"说了什么"。两者可以各自单独开。
     ★ v0.3.1.46dsk: 去掉 `DM_ON &&`。总开关管的是"认人"那几条规则, 管不到文本通道;
       焊在一起会让「关掉弹幕屏蔽总开关但开着 fastText」变成静默失效(见文件头)。 */
  var NEED_TEXT = !!CF.ft_dm;
  var BLKH = null;                       // 黑名单用户的 midHash 集合(O(1) 查表, 不是逐条扫描)
  if (DM_ON && CF.blk_on && CF.dmhash && CF.dmhash.length) {
    BLKH = Object.create(null);
    for (var i = 0; i < CF.dmhash.length; i++) {
      var h0 = String(CF.dmhash[i] || "").toLowerCase();
      if (h0) BLKH[h0] = 1;
    }
    if (!Object.keys(BLKH).length) BLKH = null;
  }

  function active() {
    return /^\/(video|list|bangumi\/play)\//.test(location.pathname);
  }
  function isDmAPI(u) {
    /* 2026-10-09 实证(无头 Chrome CDP 探针): 播放器已改走 /x/v2/dm/wbi/web/seg.so,
     * 路径里多了 wbi/ 段 —— 旧匹配只认 /x/v2/dm/web/seg.so 会整条漏掉(弹幕列表
     * 永远加载中、筛选不生效的真因)。改按 /x/v2/dm/ 前缀 + seg.so 判定, 兼容
     * web / wbi/web / list 等一切变体; 自取分段的 &_msx=1 标记照旧豁免。 */
    return typeof u === "string" && u.indexOf("seg.so") >= 0
      && u.indexOf("api.bilibili.com") >= 0
      && u.indexOf("/x/v2/dm/") >= 0
      && u.indexOf("_msx=1") < 0;        // 自己取的分段不拦
  }

  /* ---------------- protobuf 最小解析 ---------------- */
  var TD = (typeof TextDecoder !== "undefined") ? new TextDecoder("utf-8") : null;
  function utf8(u8, a, b) {
    if (TD) { try { return TD.decode(u8.subarray(a, b)); } catch (e) {} }
    return "";
  }
  /* 读 varint → 值; r[0]=值 r[1]=下一位置(用数组传引用, 兼容老语法) */
  function pbVarint(u8, i, r) {
    var v = 0, sh = 0, b, n = u8.length;
    do {
      if (i >= n) { r[0] = 0; r[1] = n; return 0; }
      b = u8[i++];
      v += (b & 0x7f) * Math.pow(2, sh);
      sh += 7;
    } while (b & 0x80);
    r[0] = v; r[1] = i;
    return v;
  }
  /* DanmakuElem: 只抽取展示/判定需要的字段 */
  function pbElem(sub) {
    var o = {}, r = [0, 0], i = 0;
    while (i < sub.length) {
      var tag = pbVarint(sub, i, r); i = r[1];
      var fno = tag >> 3, wt = tag & 7;
      if (wt === 0) {
        var v = pbVarint(sub, i, r); i = r[1];
        if (fno === 1) o.id = v;
        else if (fno === 2) o.t = v;      // progress ms
        else if (fno === 8) o.ct = v;     // ctime 秒
      } else if (wt === 2) {
        var ln = pbVarint(sub, i, r); i = r[1];
        if (fno === 6) o.h = utf8(sub, i, i + ln);
        else if (fno === 7) o.text = utf8(sub, i, i + ln);
        else if (fno === 12 && o.id == null) o.id = utf8(sub, i, i + ln);
        i += ln;
      } else if (wt === 5) i += 4;
      else if (wt === 1) i += 8;
      else return null;                   // 未知 wire type → 整体放弃, 原样放行
    }
    return o;
  }
  /* 顶层 DmSegMobileReply。返回 spans(含字段头的完整 span, 供原样回填)与 elems(e.sp 指回 span) */
  function pbTop(u8) {
    var spans = [], elems = [], r = [0, 0], i = 0, n = u8.length;
    while (i < n) {
      var hs = i;
      var tag = pbVarint(u8, i, r); i = r[1];
      var fno = tag >> 3, wt = tag & 7;
      if (wt === 2) {
        var ln = pbVarint(u8, i, r); i = r[1];
        var bs = i, be = i + ln;
        if (be > n) return null;
        i = be;
        var sp = { hs: hs, he: be, bs: bs, fno: fno };
        spans.push(sp);
        if (fno === 1) {
          var o = pbElem(u8.subarray(bs, be));
          if (o) elems.push({ o: o, sp: sp });
        }
      } else if (wt === 0) {
        pbVarint(u8, i, r); i = r[1];
        spans.push({ hs: hs, he: i, bs: i, fno: fno });
      } else if (wt === 5) {
        i += 4;
        spans.push({ hs: hs, he: i, bs: i, fno: fno });
      } else if (wt === 1) {
        i += 8;
        spans.push({ hs: hs, he: i, bs: i, fno: fno });
      } else return null;
    }
    return { spans: spans, elems: elems };
  }

  /* ---------------- 帐号画像(按 midHash): 只读壳的缓存 ---------------- */
  var HASHFLAGS = Object.create(null);   // hash -> {mystery, lottery, nodyn, multi}
  var ASKED = Object.create(null);
  var ASK_OFF = 0, ASK_FAST = 1, ASK_WAIT = 100;
  var ASK_N = 0, ASK_OID = "", ASK_TOTAL = 900;   // 每视频最多问 900 个 hash(防画像请求风暴)
  /* 画像回填后的回问(v0.3.1.28): 见 maybeRetry()。
     ★ v0.3.1.45dsk: 窗口从 40×1.5s(60 秒) 放宽到 300×2s(约 10 分钟)。
     原因: 壳侧的补图是**限速 + 撞风控硬退避**的(退避最长 30 分钟), 60 秒窗口
     根本等不到画像 —— 后台补好了, 页面却已经停止回问, 用户看到的就是"永远筛不出来"。
     每轮只是一次本地 HTTP(实测 7~60ms), 代价可忽略。 */
  var RETRY = 0, RETRY_MAX = 300, RETRY_GAP = 2000, RETRY_TIMER = 0;

  function applyFlags(j) {
    if (!j || j.ok !== true) return;
    if (!j.cookie) ASK_OFF = 1;
    var f = j.flags || {}, got = false;
    for (var k in f) { HASHFLAGS[k] = f[k]; got = true; }
    if (got) refilter();          // 画像变了 → 列表立刻重算(不必刷新页面)
    maybeRetry();                 // 还没拿到的 → 等壳后台补完再问一轮
  }
  function flagsURL(need) {
    var port = String(window.__MSX_PORT__ || "8000");
    return "http://127.0.0.1:" + port + "/api/bili/dmflags?bio=" + (MYSTERY ? 1 : 0)
      + "&lot=" + (LOT ? 1 : 0) + "&nodyn=" + (EMPTY ? 1 : 0)
      + "&hashes=" + need.join(",");
  }
  function askFlags(hashes, wait) {
    if (!NEED_FLAGS || ASK_OFF || !hashes || !hashes.length) return Promise.resolve();
    if (ASK_OID !== FEED.oid) { ASK_OID = FEED.oid; ASK_N = 0; }   // 换视频重置配额
    var need = [];
    for (var i = 0; i < hashes.length && need.length < 300; i++) {
      var h = hashes[i];
      if (h && !ASKED[h]) {
        if (ASK_N >= ASK_TOTAL) break;    // 配额用完: 剩下的宁可漏杀, 不再拖壳
        ASKED[h] = 1; ASK_N++; need.push(h);
      }
    }
    if (!need.length) return Promise.resolve();
    /* v0.3.1.32: LNA(本地网络访问)声明。Chrome/Edge 142+ 起, 公网源(https://*.bilibili.com)
     * 访问 loopback(127.0.0.1) 属于 Local Network Access 请求(目标地址空间比页面更私有),
     * 需要走 LNA 授权流程。显式写 targetAddressSpace:"loopback" 让浏览器知道这是有意的本地
     * 访问 —— 否则可能被判成 unknown 直接拦掉, 连授权提示都不弹。不支持的浏览器会忽略它。
     * 壳端已补 OPTIONS 预检 + Access-Control-Allow-Private-Network: true(两者都是 LNA 必需)。
     * 若仍拿不到画像: 到浏览器里把 bilibili.com 的「本地网络访问」权限设为允许。 */
    var url = flagsURL(need);
    if (!wait || !ASK_FAST) {             // 只预热, 不等(不影响当次)
      try {
        fetch(url, { cache: "no-store", credentials: "omit", targetAddressSpace: "loopback" })
          .then(function (r) { return r.json(); }).then(applyFlags)
          .catch(function () { for (var k = 0; k < need.length; k++) delete ASKED[need[k]]; });
      } catch (e) {}
      return Promise.resolve();
    }
    return new Promise(function (res) {
      var done = 0, tm = 0;
      var back = function (ok) {
        if (done) return;
        done = 1;
        if (tm) { try { clearTimeout(tm); } catch (e) {} }
        if (!ok) { for (var k = 0; k < need.length; k++) delete ASKED[need[k]]; }
        res();
      };
      tm = setTimeout(function () { ASK_FAST = 0; back(false); }, ASK_WAIT);
      try {
        fetch(url, { cache: "no-store", credentials: "omit", targetAddressSpace: "loopback" })
          .then(function (r) { return r.json(); })
          .then(function (j) { applyFlags(j); back(true); })
          .catch(function () { ASK_FAST = 0; back(false); });
      } catch (e) { ASK_FAST = 0; back(false); }
    });
  }

  /* 画像回填后的**有限次回问**(v0.3.1.28)。
     壳的画像查询是后台限速的(0.6s/个, 一个播放页几百个候选), 第一次问必然问不到;
     而 askFlags 用 ASKED[h]=1 做永久去重 —— 一个 hash 在一个播放页里只会问一次,
     画像补上了也不会再问, 除非整页刷新。于是列表永远停在"一条都没屏蔽"。
     这里把仍无标记的 hash 从 ASKED 放出来, 隔 RETRY_GAP 再问, 最多 RETRY_MAX 轮:
     后台查到一个就 refilter 一次, 界面跟着一条条更新。 */
  function maybeRetry() {
    if (RETRY_TIMER || RETRY >= RETRY_MAX || ASK_OFF || !NEED_FLAGS) return;
    var pend = [], i, h;
    for (i = 0; i < FEED.all.length; i++) {
      h = FEED.all[i].h ? String(FEED.all[i].h).toLowerCase() : "";
      if (h && !HASHFLAGS[h]) pend.push(h);
    }
    if (!pend.length) return;
    RETRY++;
    RETRY_TIMER = setTimeout(function () {
      RETRY_TIMER = 0;
      if (!active()) return;
      for (var k = 0; k < pend.length; k++) delete ASKED[pend[k]];
      try {
        fetch(flagsURL(pend.slice(0, 300)), { cache: "no-store", credentials: "omit", targetAddressSpace: "loopback" })
          .then(function (r) { return r.json(); })
          .then(applyFlags)          // applyFlags 内部会接着排下一轮
          .catch(function () { maybeRetry(); });   // 失败也要续上, 否则重问链断掉
      } catch (e) { maybeRetry(); }
    }, RETRY_GAP);
  }

  /* ---------------- 快速文本分析(本地 fastText): 按弹幕**内容**判定 ----------------
   * 壳里跑着一个约 58MB 的中文多标签模型(引战/低质/标题党/阴阳怪气/负面), 纯本地前向,
   * 零联网零 token。这里只负责把本段弹幕的**文本**批量送过去, 拿回"该拦的原因标签"。
   *
   * 关键路径约束与 askFlags 完全一致:
   *   · 用 text/plain 免 CORS 预检(否则 OPTIONS 往返会把等待翻倍);
   *   · 最多等 TXT_WAIT, 超时就降级成"只预热不等" —— 之后的段仍然会命中壳的缓存,
   *     而且 refilter() 会把新拿到的标记补到「弹幕列表」上, 不会永久漏筛;
   *   · 壳端未开启/模型缺失 → on:false, 这里直接停发(绝不因为筛选功能坏掉拖慢页面)。
   */
  var TXTWHY = Object.create(null);      // 弹幕文本 -> 原因标签(空串=放行)
  var TXTASKED = Object.create(null);
  var TXT_OFF = 0, TXT_FAST = 1, TXT_WAIT = Number(CF.ft_wait) || 400;
  var TXT_N = 0, TXT_OID = "", TXT_TOTAL = 4000;
  var TXT_PER = Number(CF.ft_batch) || 700;   // BERT 120 条/批；fastText 回退 700

  /* 视频上下文: BERT 是靠「这句话在这个视频里是否阴阳/引战」判断的，
     只送文本会把正常玩梗误杀。字段与训练时的 ctx_text 一致。 */
  function videoCtx() {
    var d = (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.videoData) || {};
    var t = d.title || document.title || "";
    t = String(t).replace(/\s*[-_]?\s*哔哩哔哩.*$/, "").trim().slice(0, 60);
    var up = (d.owner && d.owner.name) || "";
    if (!up) {
      var el = document.querySelector(".up-name, .up-info--name, .up-detail-top a");
      up = el ? (el.textContent || "") : "";
    }
    var tags = "";
    try {
      var m = document.querySelector('meta[name="keywords"]');
      tags = m ? (m.getAttribute("content") || "") : "";
    } catch (e) {}
    tags = String(tags).split(",").slice(0, 3).join(",").slice(0, 16);
    return { title: t, up: String(up).replace(/\s+/g, " ").trim().slice(0, 24), tags: tags };
  }

  function applyText(need, j) {
    if (!j || j.ok !== true) return;
    if (j.on !== true) { TXT_OFF = 1; return; }     // 壳侧没开 → 之后不再发请求
    var w = j.why || [], got = false, k, v;
    for (k = 0; k < need.length; k++) {
      v = w[k] || "";
      if (TXTWHY[need[k]] !== v) { TXTWHY[need[k]] = v; got = true; }
    }
    if (got) refilter();      // 内容标记变了 → 列表立刻重算(保留/屏蔽两边都动)
  }
  function askText(texts, wait) {
    if (!NEED_TEXT || TXT_OFF || !texts || !texts.length) return Promise.resolve();
    if (TXT_OID !== FEED.oid) { TXT_OID = FEED.oid; TXT_N = 0; }
    var need = [], i, t;
    for (i = 0; i < texts.length && need.length < TXT_PER; i++) {
      t = texts[i];
      if (!t) continue;
      if (t.length > 64) t = t.slice(0, 64);        // 与壳/训练的 64 字上限一致
      if (TXTASKED[t]) continue;
      if (TXT_N >= TXT_TOTAL) break;
      TXTASKED[t] = 1; TXT_N++; need.push(t);
    }
    if (!need.length) return Promise.resolve();
    var port = String(window.__MSX_PORT__ || "8000");
    var req = {
      method: "POST", cache: "no-store", credentials: "omit",
      targetAddressSpace: "loopback",
      headers: { "Content-Type": "text/plain;charset=UTF-8" },
      body: JSON.stringify({ kind: "dm", texts: need, ctx: videoCtx() })
    };
    function undo() { for (var k = 0; k < need.length; k++) delete TXTASKED[need[k]]; }
    if (!wait || !TXT_FAST) {                       // 只预热, 不等(不影响当次)
      try {
        fetch("http://127.0.0.1:" + port + "/api/textfilter", req)
          .then(function (r) { return r.json(); })
          .then(function (j) { applyText(need, j); })
          .catch(undo);
      } catch (e) { undo(); }
      return Promise.resolve();
    }
    return new Promise(function (res) {
      var done = 0, tm = 0;
      var back = function (ok) {
        if (done) return;
        done = 1;
        if (tm) { try { clearTimeout(tm); } catch (e) {} }
        if (!ok) { TXT_FAST = 0; undo(); }
        res();
      };
      tm = setTimeout(function () { back(false); }, TXT_WAIT);
      try {
        fetch("http://127.0.0.1:" + port + "/api/textfilter", req)
          .then(function (r) { return r.json(); })
          .then(function (j) { applyText(need, j); back(true); })
          .catch(function () { back(false); });
      } catch (e) { back(false); }
    });
  }

  /* 单条弹幕被屏蔽的原因(空串 = 保留)。面板「显示屏蔽」拿它做行内标签,
     让用户看得见"这条到底为什么被筛掉了" —— 否则筛选设置对用户就是个黑盒。 */
  function blkWhy(o) {
    var h = o.h ? String(o.h).toLowerCase() : "";
    if (BLKH && h && BLKH[h]) return "黑名单";
    var f = HASHFLAGS[h];
    if (f) {
      var w = "";
      if (MYSTERY && f.mystery) w = "无简介";
      else if (LOT && f.lottery) w = "抽奖";
      else if (EMPTY && f.nodyn) w = "空首页";
      /* 等级不在这里判: 弹幕接口不带等级, 判它必须联网查账号资料(见文件头)。 */
      if (w) {
        /* v0.3.1.27: 8 位哈希对上多个账号时, 这条是按"候选策略"判的, 不是铁证 —— 标注出来,
           否则用户会以为我们认定了发送者(用户反馈: 一个哈希对上一个空首页账号, 却看不出歧义)。 */
        var n = Number(f.multi) || 1;
        return n > 1 ? (w + "·" + n + "候选") : w;
      }
    }
    /* 内容判定放最后: 账号级规则(黑名单/画像)是铁证, 优先显示; 都没有才看文本。
       TXTWHY 里存的就是壳端给的中文原因(引战/低质/标题党/阴阳怪气/负面)。 */
    if (NEED_TEXT) {
      var t = o.text ? String(o.text).slice(0, 64) : "";
      if (t && TXTWHY[t]) return TXTWHY[t];
    }
    return "";
  }
  /* 单条弹幕是否保留 */
  function blocked(o) { return !!blkWhy(o); }

  /* ---------------- 弹幕列表数据源(ownui 面板订阅) ---------------- */
  var FEED = window.__msxDanmaku = {
    oid: "", pid: "",
    rows: [],          // 保留的弹幕 {t(ms), text, ct, h, id} 按 t 升序
    hidden: [],        // 被屏蔽的弹幕(同上 + why 原因标签); 面板「显示屏蔽」时才渲染
    /* v0.3.1.28: 原始弹幕(未分类)。壳的账号画像是后台限速补的, 分段到手时基本都还没
       画像 —— 旧实现在解析那一刻算完就定死了, 于是画像补上也不生效(用户反馈:
       「切显示屏蔽条数一条没变」)。留着 all 才能在画像到达时重算。 */
    all: [],
    onChange: null,
    /* 本页还有多少条弹幕的发送者画像没拿到(面板用它显示「正在补画像」) */
    pendingCount: function () {
      var n = 0, i, h;
      for (i = 0; i < FEED.all.length; i++) {
        h = FEED.all[i].h ? String(FEED.all[i].h).toLowerCase() : "";
        if (h && !HASHFLAGS[h]) n++;
      }
      return n;
    },
    /* 只要 mid(兼容旧调用点) */
    resolve: function (hash) {
      return FEED.resolveFull(hash).then(function (j) {
        return (j && j.mid) ? String(j.mid) : null;
      });
    },
    /* 完整结果: {ok, mid, ncand, cands:[{mid,name,face}], why}
       —— 多候选时由面板(ownui buildDanmaku)弹选择框 */
    resolveFull: function (hash) {
      var port = String(window.__MSX_PORT__ || "8000");
      return fetch("http://127.0.0.1:" + port + "/api/bili/dmmid?hash=" + String(hash).toLowerCase(),
                   { cache: "no-store", credentials: "omit", targetAddressSpace: "loopback" })
        .then(function (r) { return r.json(); })
        .catch(function () { return null; });
    },
  };
  var SEEN = Object.create(null);        // 弹幕 id 去重
  var SEGIDS = Object.create(null);      // **已成功入列**的 segment_index

  /* 认 oid / 换视频时重置列表。返回 segment_index(拿不到给 0)。
     ⚠️ v0.3.1.33: 这里**不记账**(SEGIDS)。老写法在解析**之前**就 SEGIDS[seg]=1,
     一旦解析抛异常(畸形/被截断的响应体)这个分段就被永久跳过, fetchMissing 再也
     不会补, 面板就永远停在「加载中…（弹幕按分段陆续到位）」。
     现在 hook 一律不记账, 只有 fetchMissing **自己完整取回一段**才 markSeg —— 见
     fetch 钩子里关于 ps/pe 窗口的说明(播放器那次只给半段, 不能算数)。 */
  function noteSeg(url) {
    var seg = 0;
    try {
      var q = url.split("?")[1] || "";
      var oid = (q.match(/(?:^|&)oid=(\d+)/) || [])[1];
      var pid = (q.match(/(?:^|&)pid=(\d+)/) || [])[1] || "";
      seg = Number((q.match(/(?:^|&)segment_index=(\d+)/) || [])[1] || 0) || 0;
      if (oid && oid !== FEED.oid) {      // 换视频/换P → 重置列表
        FEED.oid = oid;
        FEED.rows = [];
        FEED.hidden = [];
        FEED.all = [];
        RETRY = 0;                       // 换视频 → 回问配额重置
        SEEN = Object.create(null);
        SEGIDS = Object.create(null);
        /* ⚠️ v0.3.1.33: 这里**绝不能**写 SELF.oid0 —— 那是"正在跑那一轮"的守卫令牌。
           写掉等于把守卫拆了: 旧轮醒来发现 oid0 === FEED.oid, 以为还是自己的活, 拿着
           旧的 i/total 继续跑; 跑完 i>total 就 SELF.busy=false 退出, 而新视频那一轮
           早在 50ms 后因为 busy=true 被挡在门外 —— 于是谁都不再补分段, 列表永远空。
           改成只递增代次, 让旧轮自己作废。 */
        SELF.gen++;
        /* ⚠️ v0.3.1.33: 看门狗**延后一拍**再重置, 不要在这里同步 kick ——
           此刻 feedAdd 还没跑(FEED.all 还是空的), 同步跑会让看门狗误判"一条都没到",
           白跑一轮; 而且那时 SEGIDS 刚清空, 会把播放器**已经给过的第 1 段**再取一遍。
           补取本身由 hook 拿到响应后调用 fetchMissing() 触发, 看门狗只兜底。 */
        setTimeout(function () { kick(oid); }, 0);
      }
      if (pid) FEED.pid = pid;
    } catch (e) {}
    return seg;
  }
  /* 记账: 这一段**我们已经完整取回**了。只有 fetchMissing 取回后调它。
     空分段是合法结果("这段时间没人发弹幕"), 照样记账, 否则会反复重取。 */
  function markSeg(seg) { if (seg > 0) SEGIDS[seg] = 1; }

  /* 从 __INITIAL_STATE__ 兜底取 oid(=cid)。播放器那次 seg.so 请求没被拦到时,
     这是唯一的 oid 来源 —— 拿不到 oid 就永远不会去补分段。 */
  function guessOid() {
    try {
      var st = window.__INITIAL_STATE__ || {};
      var vd = st.videoData || {};
      var cid = vd.cid || st.cid || 0;
      if (!cid) {
        var p = Number(st.p || (location.search.match(/[?&]p=(\d+)/) || [])[1] || 1) || 1;
        var pages = vd.pages || [];
        for (var i = 0; i < pages.length; i++) {
          if (Number(pages[i].page) === p && pages[i].cid) { cid = pages[i].cid; break; }
        }
        if (!cid && pages.length === 1 && pages[0].cid) cid = pages[0].cid;
      }
      return cid ? String(cid) : "";
    } catch (e) { return ""; }
  }

  var hid = 0, hinted = -1;
  function hint() {
    if (hinted === hid) return;
    hinted = hid;
    try {
      var el = document.getElementById("msx-dm-hint");
      if (!el) {
        el = document.createElement("div");
        el.id = "msx-dm-hint";
        el.setAttribute("style", "position:fixed;right:16px;bottom:52px;z-index:2147483000;"
          + "padding:7px 12px;border-radius:8px;background:rgba(18,20,26,.92);color:#e8eaf0;"
          + "font:12px/1.5 system-ui,-apple-system,'Microsoft YaHei',sans-serif;"
          + "box-shadow:0 4px 16px rgba(0,0,0,.45);pointer-events:none;"
          + "transition:opacity .35s;opacity:0;border:1px solid rgba(255,255,255,.08)");
        (document.body || document.documentElement).appendChild(el);
      }
      el.textContent = "弹幕筛选：已隐藏 " + hid + " 条弹幕";
      el.style.opacity = "1";
      if (el.__t) clearTimeout(el.__t);
      el.__t = setTimeout(function () { try { el.style.opacity = "0"; } catch (e) {} }, 4200);
    } catch (e) {}
  }

  function _rowId(o) { return (o.id != null) ? String(o.id) : (o.h + "|" + o.t + "|" + o.text); }
  function _row(o) {
    return { t: o.t || 0, text: o.text || "", ct: o.ct || 0, h: o.h || "", id: _rowId(o) };
  }
  function _byT(a, b) { return (a.t || 0) - (b.t || 0); }
  /* 按**当前**已知标记把 FEED.all 整体重算成 保留 / 屏蔽 两个列表。
     v0.3.1.28: 分类不再在解析时定死 —— 画像回填后必须能重算, 否则被屏蔽的行
     永远进不了 FEED.hidden(面板「显示屏蔽」切了没反应的真因)。
     每次都是**新数组**, 面板靠引用变化判定"需要整体重画"。 */
  function refilter() {
    var rows = [], hidden = [], i, o, r, why;
    for (i = 0; i < FEED.all.length; i++) {
      o = FEED.all[i];
      why = blkWhy(o);
      r = _row(o);
      if (why) { r.why = why; hidden.push(r); }
      else { rows.push(r); }
    }
    rows.sort(_byT); hidden.sort(_byT);
    FEED.rows = rows;
    FEED.hidden = hidden;
    try { FEED.onChange && FEED.onChange(); } catch (e) {}
    /* 列表一变就试着把还没画像的哈希再问一轮。必须挂在这里而不是只挂在 applyFlags 上:
       fetch 路径是 askFlags().then(process) —— 问的时候 FEED.all 还是空的, 那时候
       maybeRetry 找不到可问的, 重问链就永远起不来。 */
    maybeRetry();
  }
  /* raw = 本次分段解析出的**全部**弹幕(未分类)。按 id 去重后并入 FEED.all, 再整体重算。 */
  function feedAdd(raw) {
    var added = false, i, o, id;
    raw = raw || [];
    for (i = 0; i < raw.length; i++) {
      o = raw[i];
      id = _rowId(o);
      if (SEEN[id]) continue;
      SEEN[id] = 1;
      FEED.all.push(o);
      added = true;
    }
    if (added) refilter();
  }

  /* 解析 + 按当前已知标记过滤; 返回改写后的字节(null=未改动)与保留的行。
     ★ v0.3.1.46dsk: 顺带回**本段去重后的文本清单**(texts)。XHR 路径必须拿到它才能
       给文本通道预热 —— 以前 XHR 只 askFlags, 播放器那条路径压根没喂过文本。 */
  function process(u8) {
    var top = pbTop(u8);
    if (!top) return { out: null, hashes: [], texts: [], rows: [], hrows: [], raw: [] };
    var hashes = [], hseen = {}, keep = [], rows = [], hrows = [], raw = [], cut = 0, i;
    var texts = [], tseen = {};
    for (i = 0; i < top.elems.length; i++) {
      var e = top.elems[i];
      var h = e.o.h ? String(e.o.h).toLowerCase() : "";
      if (h && !hseen[h]) { hseen[h] = 1; hashes.push(h); }
      var tt = e.o.text;
      if (tt) {                                    // 与 textsOf() 同一口径: 截 64 字 + 去重
        if (tt.length > 64) tt = tt.slice(0, 64);
        if (!tseen[tt]) { tseen[tt] = 1; texts.push(tt); }
      }
      raw.push(e.o);            // 全部留下(保留的 + 被摘的): 画像回填后要重算分类
      var why = blkWhy(e.o);
      if (why) { cut++; hrows.push({ o: e.o, why: why }); continue; }
      keep.push(e);
      rows.push(e.o);
    }
    if (cut) { hid += cut; hint(); }
    var out = null;
    if (cut) {
      var parts = [], total = 0, j;
      for (j = 0; j < top.spans.length; j++) {
        if (top.spans[j].fno === 1) continue;   // elems 用 keep 清单重建
        parts.push(u8.subarray(top.spans[j].hs, top.spans[j].he));
        total += top.spans[j].he - top.spans[j].hs;
      }
      for (j = 0; j < keep.length; j++) {
        parts.push(u8.subarray(keep[j].sp.hs, keep[j].sp.he));
        total += keep[j].sp.he - keep[j].sp.hs;
      }
      out = new Uint8Array(total);
      var off = 0;
      for (j = 0; j < parts.length; j++) { out.set(parts[j], off); off += parts[j].length; }
    }
    return { out: out, hashes: hashes, texts: texts, rows: rows, hrows: hrows, raw: raw };
  }

  /* fetch 路径: 先问壳要标记(最多等 ASK_WAIT/TXT_WAIT, 超时过就 0 等待), 再同步过滤改写 */
  function textsOf(top) {
    var out = [], seen = {}, i, t;
    for (i = 0; i < top.elems.length; i++) {
      t = top.elems[i].o.text;
      if (!t) continue;
      if (t.length > 64) t = t.slice(0, 64);
      if (seen[t]) continue;
      seen[t] = 1; out.push(t);
    }
    return out;
  }
  function processAsync(u8) {
    var top = pbTop(u8);
    if (!top) return Promise.resolve({ out: null, hashes: [], texts: [], rows: [], hrows: [], raw: [] });
    var hashes = [], hseen = {};
    for (var i = 0; i < top.elems.length; i++) {
      var h = top.elems[i].o.h;
      if (h && !hseen[h]) { hseen[h] = 1; hashes.push(String(h).toLowerCase()); }
    }
    /* 两条请求并行等: 账号画像最多 100ms, 文本最多 400ms。任一条超时都只是"这一轮先不筛",
       绝不卡住播放器 —— 拿不到的部分之后会被 refilter() 补上, 不会永久漏筛。 */
    return Promise.all([askFlags(hashes, true), askText(textsOf(top), true)])
      .then(function () {
        return process(u8);
      });
  }

  /* ---------------- 自取缺失分段(补全弹幕列表) ----------------
   * v0.3.1.33: 弹幕数据其实只有两个来源 —— 播放器自己那一次 seg.so(实测**只有
   * segment_index=1**), 以及我们在这里自取的 2..N 段。所以自取一旦没跑起来
   * (播放器那次请求没被拦到 → FEED.oid 一直是空的), 列表就永远停在「加载中…」。
   * 用户反馈的"弹幕列表一直是空的, 有概率出现"就是这个。 */
  var SELF = { busy: false, oid0: "", gen: 0 };
  /* WD.n = 已发起的补取轮次(上限 8); WD.w = "还没有 oid" 的空等次数(上限 12) */
  var WD = { oid: "", n: 0, w: 0, t: 0 };
  var WD_ROUNDS = 8, WD_GAP = 4000;      // 每 4 秒一轮, 最多 8 轮 ≈ 32 秒
  var WD_WAITS = 12, WD_WGAP = 1500;     // 等 oid 就绪: 每 1.5 秒看一眼, 最多 12 次 ≈ 18 秒

  /* 自愈看门狗: 只要还在播放页且一条弹幕都没拿到, 就每 4 秒重试一轮(最多 8 轮)。
     拿到数据且没有在跑的轮次就停。成因可能很多(接口变体/拦截没生效/上一轮竞态卡住),
     与其逐个堵, 不如让它自己恢复 —— "一直加载中"是用户直接看得见的症状。

     ⚠️ v0.3.1.33: "还没有 oid" 时**必须**继续排下一次, 不能直接 return ——
     __INITIAL_STATE__ 在 1.5s 时常常还没就绪, 一旦在这里退出, 看门狗就永久死了,
     用户看到的还是"列表一直空"。等就绪有独立预算(WD.w), 不占用补取轮次(WD.n)。 */
  function kick(oid) {
    if (oid && WD.oid !== oid) { WD.oid = oid; WD.n = 0; WD.w = 0; }
    if (WD.t) { clearTimeout(WD.t); WD.t = 0; }
    (function tick() {
      if (!active()) return;
      if (FEED.all.length && !SELF.busy) return;     // 有数据了 → 收工
      if (!FEED.oid) {                               // 播放器那次没拦到 → 自己认领 oid
        var g = guessOid();
        if (g) { FEED.oid = g; SEEN = Object.create(null); SEGIDS = Object.create(null); }
        else if (WD.w++ < WD_WAITS) { WD.t = setTimeout(tick, WD_WGAP); return; }
        else return;                                 // 等够了还是没有 → 放弃(不刷屏)
      }
      if (WD.n >= WD_ROUNDS) return;
      WD.n++;
      fetchMissing();
      WD.t = setTimeout(tick, WD_GAP);
    })();
  }

  function durationSec() {
    try {
      var st = window.__INITIAL_STATE__ || {};
      var vd = st.videoData || {};
      if (vd.duration) return Number(vd.duration) || 0;
      var p = Number(st.p || (location.search.match(/[?&]p=(\d+)/) || [])[1] || 1) || 1;
      var pages = vd.pages || [];
      for (var i = 0; i < pages.length; i++) {
        if (Number(pages[i].page) === p && pages[i].duration) return Number(pages[i].duration) || 0;
      }
      if (pages.length === 1 && pages[0].duration) return Number(pages[0].duration) || 0;
      /* v0.3.1.33: 再退一步问 <video>。拿不到时长就只能猜段数, 猜少了会漏掉后半程弹幕。 */
      var v = document.querySelector && document.querySelector("video");
      if (v && isFinite(v.duration) && v.duration > 0) return Number(v.duration);
    } catch (e) {}
    return 0;
  }
  function fetchMissing() {
    if (!FEED.oid || !active()) return;
    /* 同一视频已有一轮在跑 → 不重复起。注意判据是 SELF.oid0(本轮负责的视频),
       不是 FEED.oid —— 换了视频时旧轮还占着 busy, 新视频必须能立刻起新轮。 */
    if (SELF.busy && SELF.oid0 === FEED.oid) return;
    var myOid = FEED.oid, myGen = SELF.gen;
    SELF.busy = true;
    SELF.oid0 = myOid;
    var dur = durationSec();
    /* 段长固定 360 秒。拿不到时长时按 10 段(≈60 分钟)猜 —— v0.3.1.33 从 6 提到 10:
       长视频后半程的分段过去直接被"段数猜少了"截断, 表现同样是列表不完整。 */
    var total = Math.min(30, dur ? Math.ceil(dur / 360) : 10);
    var i = 1;
    function done() {
      if (SELF.gen === myGen && SELF.oid0 === myOid) SELF.busy = false;
    }
    (function step() {
      if (!active() || SELF.gen !== myGen || FEED.oid !== myOid) { done(); return; }   // 换视频 → 停
      while (i <= total && SEGIDS[i]) i++;
      if (i > total) { done(); return; }
      var si = i++;
      fetch("https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=" + myOid
            + (FEED.pid ? "&pid=" + FEED.pid : "") + "&segment_index=" + si + "&_msx=1",
            { cache: "no-store", credentials: "include" })
        .then(function (r) { return r.ok ? r.arrayBuffer() : null; })
        .then(function (buf) {
          if (!buf) return;
          if (SELF.gen !== myGen || FEED.oid !== myOid) return;   // 换视频了 → 丢弃这批
          var u8 = new Uint8Array(buf);
          var top = pbTop(u8);
          var r = process(u8);
          markSeg(si);                                             // 解析完成才记账
          feedAdd(r.raw);
          askFlags(r.hashes, false);
          /* 自取分段只喂「弹幕列表」, 不参与播放器字节流 → 文本判定走**非阻塞**预热,
             拿回来由 applyText → refilter() 更新列表, 不拖慢补段节奏。 */
          askText(top ? textsOf(top) : [], false);
        })
        .catch(function () {})
        .then(function () {
          if (SELF.gen === myGen && FEED.oid === myOid) setTimeout(step, 350);
        });
    })();
  }
  /* 开机就挂看门狗: 不依赖"播放器请求被拦到"这件事。1.5s 后才开始, 给播放器先手。 */
  setTimeout(function () { try { kick(""); } catch (e) {} }, 1500);

  /* ---------------- fetch 钩子 ---------------- */
  try {
    var _fetch = window.fetch;
    if (typeof _fetch === "function") {
      window.fetch = function (input, init) {
        var url = "";
        try { url = (typeof input === "string") ? input : (input && input.url) || ""; } catch (e) {}
        var p = _fetch.apply(this, arguments);
        if (!isDmAPI(url) || !active()) return p;
        return p.then(function (res) {
          try {
            if (!res || !res.ok) return res;
            return res.clone().arrayBuffer().then(function (buf) {
              noteSeg(url);                  // 只用来认 oid / 换视频时重置列表
              return processAsync(new Uint8Array(buf)).then(function (r) {
                feedAdd(r.raw);
                /* ⚠️ v0.3.1.33: 这里**不记账**。播放器那次请求带 pull_mode/ps/pe
                   (实测 ps=0&pe=120000), 只覆盖分段的一部分 —— 实测同一段:
                   播放器 52KB vs 我们自取 98KB, 只到一半。当成"整段到手"会让自取
                   跳过这一段, 列表里就少掉半段弹幕。只有我们自己取的完整分段才算数。 */
                askFlags(r.hashes, false);     // 顺手预热后续分段
                if (FEED.oid) fetchMissing();  // v0.3.1.33: fetch 路径也要补其余分段(与 XHR 对称)
                if (!r.out) return res;
                var r2 = new Response(r.out.buffer, { status: res.status, statusText: res.statusText, headers: res.headers });
                try { Object.defineProperty(r2, "url", { value: res.url }); } catch (e) {}
                return r2;
              });
            }).catch(function () { return res; });
          } catch (e) { return res; }
        });
      };
    }
  } catch (e) {}

  /* ---------------- XHR 钩子(播放器实际走这条) ----------------
   * 响应必须同步给出, 没法等壳 → 用当前已知标记就地过滤 + 顺手预热(下一段/下一次生效)。 */
  try {
    var XP = XMLHttpRequest.prototype;
    var _open = XP.open;
    XP.open = function (m, u) {
      try { this.__msxDmU = String(u == null ? "" : u); } catch (e) {}
      return _open.apply(this, arguments);
    };
    var dR = Object.getOwnPropertyDescriptor(XP, "response");
    if (dR && dR.get) {
      Object.defineProperty(XP, "response", {
        configurable: true, enumerable: !!dR.enumerable,
        get: function () {
          var v = dR.get.call(this);
          if (!this.__msxDmU || !isDmAPI(this.__msxDmU) || !active()) return v;
          try {
            if (v instanceof ArrayBuffer) {
              if (this.__msxDmB === undefined) {          // 同一个 XHR 只处理一次
                this.__msxDmB = null;                     // 先占位: 解析失败也不重复处理
                noteSeg(this.__msxDmU);
                var r = process(new Uint8Array(v));
                feedAdd(r.raw);
                /* 同样不记账 —— 播放器那次是 ps/pe 窗口, 只覆盖分段的一部分
                   (理由见 fetch 钩子里那段说明)。只有自取的完整分段才记账。 */
                askFlags(r.hashes, false);
                /* ★ v0.3.1.46dsk: 文本也要预热。XHR 的 response 必须**同步**给出, 等不了
                   壳, 所以播放器当次那一段永远用不上"这一轮"的文本判定 —— 但预热出去之后:
                   ①拖进度条/重看时同一段会再问一次, 那时命中;
                   ②「弹幕列表」靠 applyText → refilter() 把标记补上。
                   以前这里只 askFlags 不 askText, 播放器路径等于从没给文本通道喂过数据
                   (唯一的数据来源是 fetchMissing 自取分段时的预热)。 */
                askText(r.texts, false);
                this.__msxDmB = r.out;                    // Uint8Array 或 null(未改动)
                if (FEED.oid) fetchMissing();             // 播放器在拉弹幕了 → 补齐所有分段
              }
              return this.__msxDmB ? this.__msxDmB.buffer : v;
            }
          } catch (e) {}
          return v;
        }
      });
    }
  } catch (e) {}
})();
