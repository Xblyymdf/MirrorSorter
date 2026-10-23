/* 洞鉴虚明 · B站评论筛选(原生评论区一并生效) · v0.3.1.41
 *
 * 为什么不再走 DOM 方案:
 *   评论区是 Web Component(<bili-comments data-params="1,<aid>">, shadow DOM;
 *   2026-10-08 无头 Chrome 抓 www.bilibili.com/video/* 实证), 类名/容器随版本变动,
 *   DOM 方案天然脆弱(旧实现挂在 #comment/.comment-container/.comment —— 一个都命中不了,
 *   所以"选了5级仍看得到3级用户")。
 *
 * 现在的做法:
 *   评论区数据永远来自 api.bilibili.com/x/v2/reply*, 于是在 document-start 劫持
 *   fetch / XMLHttpRequest 的**响应体**, 本地把不该显示的评论处理掉再交给页面。
 *   与页面排版彻底解耦: 原生评论区、自建评论区、以后换任何皮肤都一致生效。
 *
 * ★ 不许拖慢评论区(2026-10-08 用户反馈"要等筛完才放, 卡评论区信息"):
 *   响应体改写**全程同步** —— 只有本地规则(等级/黑名单/空简介)参与改写;
 *   "动态抽奖 / 空首页"这两项要问壳的缓存, 最多只等 ASK_WAIT(100ms), 且一旦超时过
 *   就永久置 ASK_FAST=0: 之后只发"预热"请求、不再等 —— 评论区按 B站 自己的节奏出。
 *   (壳端 /api/bili/flags 是只读缓存的本地调用, 实测 3~45ms; 另有短超时兜底。)
 *
 * 门控(由壳注入): window.__MSX_CF__ = { blk_on, blk:[用户名...], lvl:1..5, mystery, lottery, empty }
 *   · blk_on 时, 用户名命中黑名单的评论不返回;
 *   · 用户 B站等级 ≤ lvl 的评论不返回(lvl<=0 = 不按等级);
 *   · mystery 时, 简介为空的账号(前端显示「这个人很神秘，什么都没有写」)的评论不返回 ——
 *     评论接口的 member 里**自带 sign**(2026-10-08 实测: 顶层与楼中楼都有), 完全就地判定;
 *   · lottery 时, 动态抽奖账号的评论不返回;
 *   · empty 时, 空首页账号(一条动态都没有)的评论不返回。
 *     后两项都要问壳的 author_meta 缓存(评论本身看不出这两件事)。没缓存的作者一律放行,
 *     同时丢给壳的后台限速队列慢慢补 —— 宁可漏杀, 不可误杀。
 *
 * 楼中楼展开按钮: 根评论首屏子回复被**整段摘掉**时, 把 rcount 归零 ——
 *   B站 据此决定要不要画「共N条回复，点击查看」(2026-10-08 实测: rcount=3 时首屏只给
 *   2 条, 原来只做减法 3-2=1 / 6-3=3 仍然 >0 → 按钮留着, 点开却是空的)。
 *   展开接口(/x/v2/reply/reply)整段被摘掉时, 同步把 page.count / root.rcount 归零。
 *
 * ★ v0.3.1.41 —— 主评论列表改「标记式」: 数据层不再改写内容, 渲染层整条隐藏(2026-10-10 用户反馈):
 *   ① 被屏蔽的主评论**不再从数组里删/不再改写内容**, 只把 rpid_str 记进 BLK_RPIDS;
 *      渲染层按 <bili-comment-thread-renderer> 的 .data.rpid_str 命中后整条 display:none;
 *   ② 点「显示屏蔽」时**原样放出被屏蔽评论的内容**, 并加一个纯 CSS 的「已屏蔽」角标
 *      (旧实现把 message/uname 抹成占位文案, 所以"显示屏蔽"时也看不到原文);
 *   ③ 整条 display:none 会把列表压得很短, B站评论组件的 IntersectionObserver 哨兵
 *      (#continuations .trigger)长期停在视口里, 不再产生"进入视口"事件 → 组件不再 getList(),
 *      往下滚就停在当前页 → 用 pump() 替组件翻页(见下)。
 *
 * 安全性: 只处理 code==0 的评论接口 JSON; 任何解析/改写异常一律放弃改写、原样返回,
 *         非评论接口一个字节都不碰。绝不抛错影响 B站自身逻辑。
 */
(function () {
  "use strict";
  if (window.__msxCf) return;
  window.__msxCf = 1;

  var CF = window.__MSX_CF__;
  if (!CF || typeof CF !== "object") return;

  var LV = Number(CF.lvl) || 0;
  var BLK_ON = !!CF.blk_on;
  var MYSTERY = !!CF.mystery;      // 屏蔽无个性简介(空简介)账号的评论
  var LOT = !!CF.lottery;          // 屏蔽动态抽奖账号的评论
  var EMPTY = !!CF.empty;          // 屏蔽空首页(没有任何动态)账号的评论
  /* v0.3.1.43dsk: 「快速文本分析」对评论生效(壳在总开关+作用范围都开时注入 true)。
     与上面几条账号级规则完全独立 —— 那几条判"谁发的", 这条判"说了什么"。 */
  var NEED_TEXT = !!CF.ft_cf;
  var BLK = null;
  if (BLK_ON && CF.blk && CF.blk.length) {
    BLK = Object.create(null);
    for (var i = 0; i < CF.blk.length; i++) {
      var nm = String(CF.blk[i] == null ? "" : CF.blk[i]).trim();
      if (nm) BLK[nm] = 1;
    }
    if (!Object.keys(BLK).length) BLK = null;
  }
  if (LV <= 0 && !BLK && !MYSTERY && !LOT && !EMPTY) return;   // 没有任何过滤条件 → 不装钩子

  var hid = 0;                  // 本次文档累计隐藏条数

  /* (昵称, mid) 对上报: 评论区见过的作者进壳的 name_mid 索引(v0.3.1.22)。
   * 弹幕接口只有 midHash, 黑名单里"按昵称拉黑"的作者要管到弹幕, 就得知道 mid ——
   * 顺手把评论里看到的 (uname, mid) 存进壳(后台落库), 名单越用越全。
   * sendBeacon 发出、不读响应、绝不等它 —— 不碰评论加载的关键路径。 */
  var NAMESENT = Object.create(null);
  function reportNames(d) {
    try {
      var pairs = [];
      function take(arr) {
        if (!arr || !arr.length) return;
        for (var i = 0; i < arr.length && pairs.length < 40; i++) {
          var r = arr[i];
          if (!r || typeof r !== "object" || !r.member) continue;
          var nm = String(r.member.uname == null ? "" : r.member.uname).trim();
          var mid = String(r.member.mid == null ? "" : r.member.mid);
          if (nm && mid && !NAMESENT[nm + "\u0001" + mid]) {
            NAMESENT[nm + "\u0001" + mid] = 1;
            pairs.push([nm, mid]);
          }
          if (r.replies && r.replies.length) take(r.replies);
        }
      }
      take(d.replies); take(d.top_replies); take(d.hots);
      if (d.top && d.top.member) take([d.top]);
      if (d.upper && d.upper.top && d.upper.top.member) take([d.upper.top]);
      if (!pairs.length) return;
      var port = String(window.__MSX_PORT__ || "8000");
      /* v0.3.1.32: Blob 类型必须是 **CORS 安全列表**里的(text/plain)。
         用 application/json 会触发 OPTIONS 预检, 而壳端当时没有 do_OPTIONS,
         预检 501 → 上报被浏览器静默丢弃(sendBeacon 只表示"已入队", 页面察觉不到),
         name_mid 表因此一直是空的, 「按昵称拉黑」的弹幕屏蔽算不出任何哈希。
         壳端现在补了 do_OPTIONS(双保险), 但这里免掉预检能少一次往返、也更稳。
         壳端 read_json_body 只看 Content-Length, 不校验 Content-Type。 */
      var blob = new Blob([JSON.stringify({ pairs: pairs })], { type: "text/plain" });
      if (navigator.sendBeacon) navigator.sendBeacon("http://127.0.0.1:" + port + "/api/bili/names", blob);
    } catch (e) {}
  }

  /* 只在播放页生效 —— 用请求时刻的路径判断, 兼容 B站 SPA 站内跳转
   * (document-start 时路径可能还是首页, 那时就退出会导致整页失效)。 */
  function active() {
    return /^\/(video|list|bangumi\/play)\//.test(location.pathname);
  }

  /* 只认评论接口: /x/v2/reply, /x/v2/reply/reply, /x/v2/reply/wbi/main ... */
  function isReplyAPI(u) {
    return typeof u === "string" && u.indexOf("/x/v2/reply") >= 0 && u.indexOf("api.bilibili.com") >= 0;
  }

  /* 主评论列表(第一层) ≠ 楼中楼展开: 两者被筛光时的后果完全不同 ——
   * 楼中楼整段消失没问题(旧逻辑把 rcount/count 归零即可), 但**主列表整页被筛光
   * 会让 B站自己的评论组件卡死**: 组件 getList 只在拿到非空数组时才
   * `h.length && this.updateList(...)`, 空数组既不追加、也不置 showEnd,
   * 而 showContinuations 已在请求前被复位 → 列表底部既没有新行、也没有「到底」标记、
   * 也没有加载入口, IntersectionObserver/scroll 再无事件可触发。
   * 所以主列表**一条都不删**: 被屏蔽的条目原样留在数组里(长度与内容都不变), 只做标记,
   * 渲染后由渲染层整条藏掉 —— 见 filtArr / sweep。 */
  function isMainList(u) {
    if (typeof u !== "string") return false;
    return u.indexOf("/x/v2/reply/wbi/main") >= 0 || u.indexOf("/x/v2/reply/main") >= 0;
  }

  /* ---------- 数据层: 主评论只"标记"(不改写内容), 楼中楼照旧摘掉 ---------- */
  var BLK_RPIDS = Object.create(null);   // 被屏蔽的主评论 rpid_str → 1
  var BLK_WHY = Object.create(null);     // rpid_str → 原因文案(只有内容判定会写, 账号级规则没有)
  var hidMain = 0;                       // 被屏蔽的主评论条数(去重)
  var MUTATED = 0;                       // 本轮是否真的改写了响应数据(只有楼中楼删除才算)

  function rpidOf(r) {
    var v = r && (r.rpid_str != null ? r.rpid_str : r.rpid);
    return v == null ? "" : String(v);
  }
  function markBlocked(r) {
    var k = rpidOf(r);
    if (!k) return;
    if (!BLK_RPIDS[k]) { BLK_RPIDS[k] = 1; hidMain++; }
    /* 内容判定命中时把原因带上, 渲染层好把角标写成「已屏蔽 · 引战」而不是干巴巴三个字。 */
    if (NEED_TEXT && !BLK_WHY[k]) {
      var t = msgOf(r);
      if (t && TXTWHY[t]) BLK_WHY[k] = TXTWHY[t];
    }
  }

  /* 过滤一层评论数组, 返回摘掉的条数。
   * isMain=true(仅主列表第一层): 命中的条目**不删不改**, 只标记 —— 数组长度与内容都不变,
   *   渲染后由渲染层整条藏掉(见 sweep)。
   * isMain=false(楼中楼): 命中的条目直接摘掉, 并同步 rcount/count, 让「共N条回复」对得上。 */
  function filtArr(arr, isMain) {
    if (!arr || !arr.length) return 0;
    var cut = 0, out = [], n = arr.length;
    for (var i = 0; i < n; i++) {
      var r = arr[i];
      if (!r || typeof r !== "object") { out.push(r); continue; }
      noteText(r);          // 登记「正文 → rpid」, 供文本判定回来时定位要拦哪几条
      if (!keep(r)) {
        cut++;
        if (isMain) { markBlocked(r); out.push(r); continue; }   // 整条交给渲染层藏, 内容不动
        continue;                                               // 楼中楼: 直接摘掉
      }
      // 楼中楼: 父评论里内嵌的首屏子回复同样过滤, 并同步计数免得数字对不上
      // (子回复层不保留: 楼中楼整段消失是安全的, 展开面板就地收起即可)
      if (r.replies && r.replies.length) {
        var rc0 = (typeof r.rcount === "number") ? r.rcount : -1;
        var ct0 = (typeof r.count === "number") ? r.count : -1;
        var sub = filtArr(r.replies, false);
        if (sub) {
          MUTATED = 1;
          cut += sub;
          if (r.replies.length === 0) {
            // 首屏子回复被整段摘掉 → 「共N条回复，点击查看」没有意义了, 直接归零。
            // (只做减法的话 rcount 仍 >0, 按钮留着, 点开是空的 —— 用户 2026-10-08 反馈)
            r.rcount = 0;
            if (ct0 >= 0 && ct0 === rc0) r.count = 0;   // count 与 rcount 同义时才一起归零
          } else if (rc0 > 0) {
            r.rcount = Math.max(0, rc0 - sub);
            if (ct0 >= 0 && ct0 === rc0) r.count = r.rcount;
          }
        }
      }
      out.push(r);
    }
    if (out.length !== arr.length) {   // 只有真的删过东西才回写(主列表只是标记 → 数组原样)
      MUTATED = 1;
      arr.length = 0;
      for (var k = 0; k < out.length; k++) arr.push(out[k]);
    }
    return cut;
  }

  function filtAll(d, isMain) {
    if (!d || typeof d !== "object") return 0;
    var cut = 0;
    var reps0 = (d.replies && d.replies.length) || 0;
    if (d.replies && d.replies.length) cut += filtArr(d.replies, isMain);
    if (d.top_replies && d.top_replies.length) cut += filtArr(d.top_replies, isMain);
    if (d.hots && d.hots.length) cut += filtArr(d.hots, isMain);
    if (d.top && typeof d.top === "object" && !Array.isArray(d.top) && d.top.rpid) {
      noteText(d.top);
      if (!keep(d.top)) {
        cut++;
        if (isMain) markBlocked(d.top); else { d.top = null; MUTATED = 1; }
      }
    }
    if (d.upper && d.upper.top) {
      noteText(d.upper.top);
      if (!keep(d.upper.top)) {
        cut++;
        if (isMain) markBlocked(d.upper.top); else { d.upper.top = null; MUTATED = 1; }
      }
    }
    /* 本批评论正文已登记完 → 异步问一次本地模型(不等待, 不阻塞评论加载)。
       命中的由 applyText 补进 BLK_RPIDS 并 kick(), 下一帧就藏好。 */
    try { askText(); } catch (e) {}
    // 展开楼中楼的响应(/x/v2/reply/reply): 整段子回复被摘光 → 计数归零
    // (isMain 模式只用于主列表, 主列表数组长度不变, 这里不会命中)
    if (!isMain && reps0 > 0 && (!d.replies || d.replies.length === 0)) {
      if (d.page && typeof d.page === "object" && typeof d.page.count === "number") d.page.count = 0;
      if (d.root && typeof d.root === "object") {
        d.root.rcount = 0;
        if (typeof d.root.count === "number") d.root.count = 0;
      }
      MUTATED = 1;
    }
    return cut;
  }

  /* B站把空简介显示成「这个人很神秘，什么都没有写」; 接口里空简介就是空串。
   * 没有 sign 字段 → 放行(宁可漏杀)。 */
  var MYSTERY_TXT = "这个人很神秘，什么都没有写";
  function bioEmpty(m) {
    if (m.sign == null) return false;
    var s = String(m.sign).replace(/\s+/g, "");
    return s === "" || s === MYSTERY_TXT;
  }

  /* ---------------- 抽奖号 / 空首页: 只读壳的缓存 ---------------- */
  var FLAGGED = Object.create(null);   // 已确认是抽奖号的 mid
  var EMPTYD = Object.create(null);    // 已确认是空首页(0 条动态)的 mid
  var ASKED_MIDS = Object.create(null); // 本次文档已经问过的 mid(避免反复问)
  var ASK_OFF = 0;                     // 壳不可达 / 未登录 B站 → 不再问
  var ASK_FAST = 1;                    // 还没超时过 → 允许等一小会; 超时过 = 0(永不等)
  var ASK_WAIT = 100;                  // 首次问壳最多等多久(ms) —— 评论区不能停在这

  function midList(d) {
    var out = [], seen = Object.create(null);
    function take(arr) {
      if (!arr || !arr.length) return;
      for (var i = 0; i < arr.length; i++) {
        var r = arr[i];
        if (!r || typeof r !== "object") continue;
        var m = r.member && r.member.mid;
        if (m != null) {
          var k = String(m);
          if (!seen[k]) { seen[k] = 1; out.push(k); }
        }
        if (r.replies && r.replies.length) take(r.replies);
      }
    }
    if (d.replies && d.replies.length) take(d.replies);
    if (d.top_replies && d.top_replies.length) take(d.top_replies);
    if (d.hots && d.hots.length) take(d.hots);
    if (d.top && d.top.member && d.top.member.mid != null) out.push(String(d.top.member.mid));
    if (d.upper && d.upper.top && d.upper.top.member && d.upper.top.member.mid != null) {
      out.push(String(d.upper.top.member.mid));
    }
    return out;
  }

  /* 画像回问(v0.3.1.45dsk): 壳侧补图是限速 + 撞风控硬退避的(退避最长 30 分钟),
   * 而 ASKED_MIDS 是永久去重 —— 一个 mid 问过一次就再也不问, 于是"补上即生效"
   * 在同一个页面里其实永远等不到(实测: 60 秒只落库 26/73 个候选, 其余全部丢弃)。
   * 这里把壳回给我们的 pending 记下来, 按 RETRY_GAP 重问, 直到拿到或超出轮数。
   * 每轮只是一次本地 HTTP(实测 3~45ms), 代价可忽略。 */
  var PEND = Object.create(null);
  var RETRY = 0, RETRY_MAX = 300, RETRY_GAP = 2000, RETRY_TIMER = 0;

  function applyFlags(j) {
    if (!j || j.ok !== true) return;
    if (!j.cookie) ASK_OFF = 1;                 // 没登录 B站 → 以后不再问
    var f = j.flags || {}, k;
    for (k in f) {
      if (!f[k]) continue;
      delete PEND[k];                           // 已拿到画像 → 退出回问队列
      if (f[k].lottery) FLAGGED[k] = 1;
      if (f[k].nodyn) EMPTYD[k] = 1;
    }
    var p = j.pending || [];
    for (var i = 0; i < p.length; i++) PEND[String(p[i])] = 1;
    if (Object.keys(f).length) { try { kick(); } catch (e) {} }   // 画像变了 → 立刻重算 DOM
    retryPending();
  }

  function retryPending() {
    if (RETRY_TIMER || RETRY >= RETRY_MAX || ASK_OFF || (!LOT && !EMPTY)) return;
    var need = [], k;
    for (k in PEND) need.push(k);
    if (!need.length) return;
    RETRY++;
    RETRY_TIMER = setTimeout(function () {
      RETRY_TIMER = 0;
      if (!active()) return;
      try {
        fetch(flagsURL(need.slice(0, 60)),
              { cache: "no-store", credentials: "omit", targetAddressSpace: "loopback" })
          .then(function (r) { return r.json(); })
          .then(applyFlags)
          .catch(function () { retryPending(); });   // 失败也要续上, 否则重问链断掉
      } catch (e) { retryPending(); }
    }, RETRY_GAP);
  }

  function flagsURL(need) {
    var port = String(window.__MSX_PORT__ || "8000");
    return "http://127.0.0.1:" + port + "/api/bili/flags?bio=0&lot=" + (LOT ? 1 : 0)
      + "&nodyn=" + (EMPTY ? 1 : 0) + "&mids=" + need.join(",");
  }

  /* 问壳要这批 mid 的账号画像(只读缓存, 壳不等网络)。
   * · 正常: 最多等 ASK_WAIT —— 拿到的标记当次就能用来过滤;
   * · 超时过(ASK_FAST=0): 不再等, 只发预热请求 —— 不影响本次, 下次(翻页/切页)生效;
   * · 拿不到一律放行(宁可漏杀)。返回 Promise, fetch 路径用。 */
  function askFlags(mids) {
    if ((!LOT && !EMPTY) || ASK_OFF || !mids || !mids.length) return Promise.resolve();
    var need = [], n = mids.length;
    for (var i = 0; i < n && need.length < 60; i++) {
      var m = mids[i];
      if (!ASKED_MIDS[m]) { ASKED_MIDS[m] = 1; need.push(m); }
    }
    if (!need.length) return Promise.resolve();
    /* v0.3.1.32: LNA(本地网络访问)声明。Chrome/Edge 142+ 起, 公网源(https://*.bilibili.com)
     * 访问 loopback(127.0.0.1) 属于 Local Network Access 请求(目标地址空间比页面更私有),
     * 需要走 LNA 授权流程。显式写 targetAddressSpace:"loopback" 让浏览器知道这是有意的本地
     * 访问 —— 否则可能被判成 unknown 直接拦掉, 连授权提示都不弹。不支持的浏览器会忽略它。
     * 壳端已补 OPTIONS 预检 + Access-Control-Allow-Private-Network: true(两者都是 LNA 必需)。
     * 若仍拿不到画像: 到浏览器里把 bilibili.com 的「本地网络访问」权限设为允许。 */
    var url = flagsURL(need);
    if (!ASK_FAST) {                            // 只预热, 不等
      try {
        fetch(url, { cache: "no-store", credentials: "omit", targetAddressSpace: "loopback" })
          .then(function (r) { return r.json(); })
          .then(applyFlags)
          .catch(function () { for (var k = 0; k < need.length; k++) delete ASKED_MIDS[need[k]]; });
      } catch (e) {}
      return Promise.resolve();
    }
    return new Promise(function (res) {
      var done = 0, tm = 0;
      var back = function (ok) {
        if (done) return;
        done = 1;
        if (tm) { try { clearTimeout(tm); } catch (e) {} }
        if (!ok) {          // 失败/超时 → 允许下次重问, 否则这个作者永远问不到
          for (var k = 0; k < need.length; k++) delete ASKED_MIDS[need[k]];
        }
        res();
      };
      tm = setTimeout(function () {
        ASK_FAST = 0;       // ★ 壳慢/不可达 → 之后彻底不等, 评论区不再被拖住
        back(false);
      }, ASK_WAIT);
      try {
        fetch(url, { cache: "no-store", credentials: "omit", targetAddressSpace: "loopback" })
          .then(function (r) { return r.json(); })
          .then(function (j) { applyFlags(j); back(true); })
          .catch(function () { ASK_FAST = 0; back(false); });
      } catch (e) { ASK_FAST = 0; back(false); }
    });
  }

  /* ---------------- 快速文本分析(本地 fastText): 按评论**内容**判定 ----------------
   * 与弹幕侧不同, 这里**完全不需要**阻塞评论加载:
   *   主评论本来就是"标记式"过滤 —— 命中的只把 rpid_str 记进 BLK_RPIDS, 由 sweep()
   *   定时重扫落成 DOM 属性(见 cfApply)。所以文本判定晚几百毫秒回来也毫无影响:
   *   拿回来补进 BLK_RPIDS, 再 kick() 一次就立刻显示出来了。
   * 好处是评论列表的加载/翻页节奏完全不受筛选影响 —— 这是用户 2026-10-10 明确要的。
   */
  var TXTWHY = Object.create(null);      // 评论正文 -> 原因文案(空串=放行)
  var TXTASKED = Object.create(null);
  var TXT_RPID = Object.create(null);    // 评论正文 -> [rpid_str...](同一句话可能多人发)
  var TEXT_OF = Object.create(null);     // rpid_str -> 评论正文(查 parent 用)
  var TXT_PARENT = Object.create(null);  // 评论正文 -> 被回复的上文
  var TEXT_N = 0;
  var TXT_OFF = 0, TXT_N = 0, TXT_TOTAL = 6000;
  var TXT_PER = Number(CF.ft_batch) || 900;   // BERT 120 条/批；fastText 回退 900
  var TXT_CAP = 4000;                    // TXT_RPID 条目上限, 超了就整表重来(防长会话膨胀)

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

  function msgOf(r) {
    try {
      var c = r && r.content;
      var m = c ? c.message : "";
      m = String(m == null ? "" : m).replace(/\s+/g, " ").trim();
      return m.length > 64 ? m.slice(0, 64) : m;   // 与壳/训练的 64 字上限一致
    } catch (e) { return ""; }
  }
  function noteText(r) {
    if (!NEED_TEXT) return;
    var k = rpidOf(r), t = msgOf(r);
    if (!k || !t) return;
    if (TXT_N > TXT_CAP) { return; }
    var a = TXT_RPID[t];
    if (!a) { a = TXT_RPID[t] = []; }
    if (a.length < 40 && a.indexOf(k) < 0) a.push(k);
    /* 上文: 楼中楼回复的 parent 指向被回复的 rpid。训练就是「标题+上文+语句」,
       只送文本会让模型丢掉判断阴阳怪气最关键的那半句。 */
    if (!TEXT_OF[k] && TEXT_N < 12000) { TEXT_OF[k] = t; TEXT_N++; }
    if (!TXT_PARENT[t]) {
      var pr = r.parent == null ? "" : String(r.parent);
      var root = r.root == null ? "" : String(r.root);
      var pt = (pr && pr !== "0" && TEXT_OF[pr]) || (root && root !== k && TEXT_OF[root]) || "";
      if (pt && pt !== t) TXT_PARENT[t] = pt;
    }
  }
  function applyText(need, j) {
    if (!j || j.ok !== true) return;
    if (j.on !== true) { TXT_OFF = 1; return; }    // 壳侧没开 → 之后不再发请求
    var w = j.why || [], hit = 0, i, v, k, a;
    for (i = 0; i < need.length; i++) {
      v = w[i] || "";
      TXTWHY[need[i]] = v;
      if (!v) continue;
      a = TXT_RPID[need[i]] || [];
      for (k = 0; k < a.length; k++) {
        if (!BLK_RPIDS[a[k]]) { BLK_RPIDS[a[k]] = 1; hidMain++; }
        if (!BLK_WHY[a[k]]) BLK_WHY[a[k]] = v;
        hit++;
      }
    }
    if (hit) { try { kick(); } catch (e) {} }      // 立刻落到 DOM, 不等下一次 900ms 巡检
  }
  function askText() {
    if (!NEED_TEXT || TXT_OFF) return;
    if (TXT_N > TXT_CAP) {                         // 长会话: 整表重置, 重新开始记
      TXT_RPID = Object.create(null);
      TXTASKED = Object.create(null);
      TEXT_OF = Object.create(null);
      TXT_PARENT = Object.create(null);
      TEXT_N = 0;
      TXT_N = 0;
    }
    var need = [], items = [], t, i;
    for (t in TXT_RPID) {
      if (need.length >= TXT_PER) break;
      if (TXTASKED[t]) continue;
      TXTASKED[t] = 1; TXT_N++; need.push(t);
      items.push({ text: t, parent: TXT_PARENT[t] || "" });
    }
    if (!need.length) return;
    var port = String(window.__MSX_PORT__ || "8000");
    try {
      fetch("http://127.0.0.1:" + port + "/api/textfilter", {
        method: "POST", cache: "no-store", credentials: "omit",
        targetAddressSpace: "loopback",
        headers: { "Content-Type": "text/plain;charset=UTF-8" },
        body: JSON.stringify({ kind: "cf", texts: need, items: items, ctx: videoCtx() })
      }).then(function (r) { return r.json(); })
        .then(function (j) { applyText(need, j); })
        .catch(function () { for (var k = 0; k < need.length; k++) delete TXTASKED[need[k]]; });
    } catch (e) {}
  }

  /* 单条评论是否保留 */
  function keep(r) {
    var m = r && r.member;
    if (!m) return true;
    if (BLK) {
      var nm = String(m.uname == null ? "" : m.uname).trim();
      if (nm && BLK[nm]) return false;
    }
    if (MYSTERY && bioEmpty(m)) return false;
    if (m.mid != null) {
      var mk = String(m.mid);
      if ((LOT && FLAGGED[mk]) || (EMPTY && EMPTYD[mk])) return false;
    }
    if (LV > 0) {
      var li = m.level_info;
      var cur = li ? Number(li.current_level) : NaN;
      if (isFinite(cur) && cur <= LV) return false;   // 取不到等级 → 放行
    }
    /* 内容判定放最后(账号级规则更硬): 只有这一句文本已经拿到标记时才拦。
       首次遇到的句子这里是查不到的 —— 那种情况交给 askText 异步补, 见 applyText。 */
    if (NEED_TEXT) {
      var t = msgOf(r);
      if (t && TXTWHY[t]) return false;
    }
    return true;
  }

  /* ---------------- 渲染层: 被屏蔽的主评论整条藏掉 / 显示屏蔽时原样放出 + 角标 ----------------
   * 用户 2026-10-09 反馈(m04703): 逐条显示「已屏蔽」占位块「看起来好奇怪」——
   *   ① 被屏蔽的评论**整条不出现在列表里**(不留占位、不留空行);
   *   ② 屏蔽条数只在**评论区最上方**显示一处;
   *   ③ 旁边一个「显示屏蔽 / 隐藏屏蔽」开关, 摆放与交互照弹幕筛选那一套。
   * 用户 2026-10-10 反馈(三项): ① 屏蔽后评论列表翻不动; ② 显示屏蔽看不到原文; ③ 筛选要更快。
   * 做法: 数据层只标记(见上), 渲染层按 <bili-comment-thread-renderer> 的 .data.rpid_str 命中,
   *   默认整条 display:none(用注入样式里的 !important 规则, 不怕组件自己改 inline display);
   *   显示屏蔽时去掉隐藏标记, 评论**原样**出现, 只在其上方加一个纯 CSS 的「已屏蔽」角标。 */
  var BLK_KEY = "msx_cf_show_blocked";
  var showBlocked = false;
  try { showBlocked = localStorage.getItem(BLK_KEY) === "1"; } catch (e) {}
  var BLK_HOSTS = [];              // 当前被我们藏起来的条目宿主(每轮重算, 组件复用节点也不会误藏)

  function cfDark() {
    try { return /(?:^|;\s*)theme_style=dark/.test(String(document.cookie || "")); } catch (e) { return true; }
  }
  function cfPal() {
    return cfDark()
      ? { card: "#222325", text: "#e3e5e7", sub: "#9499a0", line: "#2f3238", accent: "#fb7299" }
      : { card: "#ffffff", text: "#18191c", sub: "#9499a0", line: "#e3e5e7", accent: "#fb7299" };
  }

  /* 评论组件本身(<bili-comments>): 评论列表在它的 shadowRoot 里, getList/continueTrigger
   * 也挂在它身上(反编译 _tmp_bilicomments.js 实证: kn("bili-comments"))。 */
  function cfCommentEl() {
    try {
      var list = document.querySelectorAll("bili-comments");
      for (var i = 0; i < list.length; i++) {
        try { if (list[i].shadowRoot) return list[i]; } catch (e) {}
      }
      return list[0] || null;
    } catch (e) { return null; }
  }
  /* 需要扫描的根: 评论组件的 shadow 根(列表在这里); 组件不可用时退回评论区容器(light DOM)。 */
  function cfRoots() {
    var roots = [], comp = cfCommentEl(), sr = null;
    if (comp) { try { sr = comp.shadowRoot; } catch (e) {} }
    if (sr) roots.push(sr);
    if (!roots.length) {
      var cont = null;
      try { cont = document.getElementById("commentapp") || document.getElementById("comment"); } catch (e) {}
      if (cont) roots.push(cont); else if (comp) roots.push(comp);
    }
    return roots;
  }
  /* 往 shadow 根注入样式: 隐藏态 + 「已屏蔽」角标(纯 CSS, 绝不改评论内容) */
  function cfStyle(root) {
    try {
      if (root.__msxCfStyle) return;
      var st = document.createElement("style");
      st.setAttribute("data-msx-cf", "1");
      st.textContent =
        'bili-comment-thread-renderer[data-msx-blk="1"][data-msx-hide="1"]{display:none !important;}'
        + 'bili-comment-thread-renderer[data-msx-blk="1"]{box-shadow:inset 3px 0 0 0 #fb7299;}'
        + 'bili-comment-thread-renderer[data-msx-blk="1"]::before{content:attr(data-msx-why);display:block;'
        + 'width:max-content;margin:8px 0 2px 4px;padding:0 8px;border-radius:4px;'
        + 'font:12px/18px "PingFang SC","Microsoft YaHei",sans-serif;color:#fff;background:#fb7299;}';
      root.appendChild(st);
      root.__msxCfStyle = st;
    } catch (e) {}
  }
  function cfApply(el, blk, why) {
    try {
      if (blk) {
        if (el.getAttribute("data-msx-blk") !== "1") el.setAttribute("data-msx-blk", "1");
        /* 角标文案: 有原因就写「已屏蔽 · 引战」, 没有就是「已屏蔽」。
           纯 CSS content:attr() 取值 —— 绝不碰评论内容本身。 */
        var w = why ? ("已屏蔽 · " + why) : "已屏蔽";
        if (el.getAttribute("data-msx-why") !== w) el.setAttribute("data-msx-why", w);
        if (showBlocked) el.removeAttribute("data-msx-hide");
        else el.setAttribute("data-msx-hide", "1");
      } else if (el.getAttribute("data-msx-blk") === "1") {
        el.removeAttribute("data-msx-blk");
        el.removeAttribute("data-msx-hide");
        el.removeAttribute("data-msx-why");
      }
    } catch (e) {}
  }

  /* 扫一遍评论区: 命中被屏蔽的主评论 → 整条藏掉(默认) / 原样放出+角标(显示屏蔽)。
     之前藏过、这轮不再命中的还给它(组件会复用 DOM 节点, 只认"这一轮还在不在")。
     v0.3.1.41: 从"全文档 querySelectorAll('*') + 逐个读 textContent"改成
     "只查评论组件 shadow 根里的 thread-renderer 标签 + Set O(1) 命中" —— 一轮亚毫秒级。 */
  function sweep() {
    if (!hidMain) return;
    var roots = cfRoots(), found = [], i, j;
    for (i = 0; i < roots.length; i++) {
      cfStyle(roots[i]);
      var nodes;
      try { nodes = roots[i].querySelectorAll("bili-comment-thread-renderer"); }
      catch (e) { continue; }
      for (j = 0; j < nodes.length; j++) {
        var el = nodes[j], d = null;
        try { d = el.data; } catch (e) {}
        var rp = (d ? rpidOf(d) : "");
        var blk = !!(rp && BLK_RPIDS[rp]);
        if (blk) found.push(el);
        cfApply(el, blk, blk ? BLK_WHY[rp] : "");
      }
    }
    for (i = 0; i < BLK_HOSTS.length; i++) {
      var o = BLK_HOSTS[i];
      if (found.indexOf(o) < 0) cfApply(o, false);   // 组件复用了节点 / 内容换了 → 还给它
    }
    BLK_HOSTS = found;
  }

  /* 翻页补泵(2026-10-10 用户反馈 ①):
   * 被屏蔽的评论整条 display:none 会把列表压短, B站评论组件的哨兵(#continuations .trigger)
   * 长期停在视口里 → IntersectionObserver 不再产生"进入视口"事件 → 组件不再 getList(),
   * 往下滚就停在当前页(组件 getList 只在 list 非空且未被拦截时才追加)。
   * 这里替组件盯着那个哨兵: 只要它还在视口里、组件又没在加载/没到底, 就替它翻下一页。 */
  var pumpBusy = false, pumpStall = 0, pumpLastLen = -1;
  var pumpBegin = 0, pumpHoldAt = 0, pumpWakeAt = 0;
  var PUMP_HOLD = 9000;   // getList 超过这么久不落地/加载态不消失 → 当卡死强拆
  function cfInst() {
    var c = cfCommentEl();
    return (c && typeof c.getList === "function") ? c : null;
  }
  /* 用户一动滚动就复位「连续扑空」计数: 旧实现扑空 6 次后永久收手,
     哪怕继续往下拉也不再补页 —— 这是「拉着拉着就卡死」的来源之一。 */
  function wakePump() {
    pumpStall = 0;
    var now = Date.now();
    if (now - pumpWakeAt < 350) return;
    pumpWakeAt = now;
    try { setTimeout(pump, 60); } catch (e) {}
  }
  function pump() {
    if (!hidMain) return;
    var now = Date.now();
    if (pumpBusy) {
      if (now - pumpBegin < PUMP_HOLD) return;
      pumpBusy = false;   // 组件 Promise 一直不落地 → 解锁, 下一轮重试
      pumpStall++;
    }
    var inst = cfInst();
    if (!inst) return;
    // 组件的加载态(showContinuations/showSpinner)可能因为一次失败请求永远挂住;
    // 挂太久就替它清掉, 否则下面每一轮补泵都会被这行挡住。
    if (inst.showSpinner || inst.showContinuations) {
      if (!pumpHoldAt) pumpHoldAt = now;
      if (now - pumpHoldAt < PUMP_HOLD) return;
      try { inst.showSpinner = false; inst.showContinuations = false; } catch (e) {}
      pumpHoldAt = 0;
    } else {
      pumpHoldAt = 0;
    }
    if (inst.showEnd) return;
    var cur = (inst.list && inst.list.length) || 0;
    if (cur !== pumpLastLen) { pumpLastLen = cur; pumpStall = 0; }   // 列表有长进 → 复位
    if (pumpStall >= 6) return;                                      // 连续扑空 → 收手(滚动会复位)
    var trig = null;
    try { trig = inst.continueTrigger; } catch (e) {}
    if (!trig || trig.isConnected === false) return;
    var rc = null;
    try { rc = trig.getBoundingClientRect(); } catch (e) { return; }
    var vh = window.innerHeight || document.documentElement.clientHeight || 0;
    if (!(rc.top < vh + 500)) return;      // 哨兵还在屏幕外 → 用户没滚到底, 不代劳
    pumpBusy = true;
    pumpBegin = now;
    var before = cur;
    var done = function () {
      // getList 可能同步返回、评论却稍后才追加; 等一拍再数, 避免把成功当扑空。
      setTimeout(function () {
        pumpBusy = false;
        var after = (inst.list && inst.list.length) || 0;
        if (after > before) { pumpStall = 0; try { hint(); } catch (e) {} }
        else pumpStall++;
      }, 320);
    };
    try { Promise.resolve(inst.getList()).then(done, done); }
    catch (e) { pumpBusy = false; }
  }
  /* 捕获阶段监听滚动: 组件自己 stopPropagation 也不影响收到。 */
  try {
    window.addEventListener("scroll", function () {
      if (!hidMain) return;
      try { wakePump(); } catch (e) {}
    }, { passive: true, capture: true });
  } catch (e) {}

  /* 评论区最上方那一行: 「评论筛选：已屏蔽 N 条评论」+ 显示/隐藏开关 */
  function barHost() {
    try {
      return document.getElementById("commentapp") || document.getElementById("comment")
        || document.querySelector(".comment-container") || null;
    } catch (e) { return null; }
  }
  function syncBar() {
    var host = barHost();
    if (!host) return false;
    var p = cfPal();
    var bar = document.getElementById("msx-cf-bar");
    if (!bar || !bar.parentNode || bar.parentNode !== host) {
      var old = document.getElementById("msx-cf-bar");
      if (old && old.parentNode) { try { old.parentNode.removeChild(old); } catch (e) {} }
      bar = document.createElement("div");
      bar.id = "msx-cf-bar";
      bar.setAttribute("style", "display:flex;align-items:center;gap:10px;"
        + "margin:10px 0 6px;padding:2px 2px;font:13px/1.6 'PingFang SC','Microsoft YaHei',sans-serif;"
        + "color:" + p.sub);
      var sp = document.createElement("span");
      sp.id = "msx-cf-bar-t";
      var bt = document.createElement("button");
      bt.type = "button";
      bt.id = "msx-cf-bar-b";
      bt.addEventListener("click", function () {
        showBlocked = !showBlocked;
        try { localStorage.setItem(BLK_KEY, showBlocked ? "1" : "0"); } catch (e) {}
        pumpStall = 0;
        try { sweep(); } catch (e) {}
        syncBar();
      });
      bar.appendChild(sp);
      bar.appendChild(bt);
      try { host.insertBefore(bar, host.firstChild); }
      catch (e) { try { host.appendChild(bar); } catch (e2) { return false; } }
    }
    var t = document.getElementById("msx-cf-bar-t");
    var b = document.getElementById("msx-cf-bar-b");
    if (t) t.textContent = "评论筛选：已屏蔽 " + hid + " 条评论";
    if (b) {
      b.textContent = (showBlocked ? "隐藏屏蔽" : "显示屏蔽") + (hid ? " " + hid : "");
      b.title = showBlocked
        ? "把被「评论筛选」屏蔽掉的评论收起来（列表里不再显示）"
        : "在列表里显示被「评论筛选」屏蔽掉的评论（原样展示，附「已屏蔽」角标；筛选规则不受影响）";
      b.setAttribute("style", "cursor:pointer;padding:2px 9px;font-size:12px;line-height:18px;"
        + "border-radius:6px;border:1px solid " + (showBlocked ? p.accent : p.line)
        + ";background:" + (showBlocked ? p.accent : "transparent")
        + ";color:" + (showBlocked ? "#fff" : p.text));
    }
    return true;
  }

  /* SPA 站内切到别的视频: 清掉上一页的标记, 免得旧 rpid 干扰新页(新页的评论会重新标) */
  var lastHref = "";
  try { lastHref = location.href; } catch (e) {}
  function cfResetIfNavigated() {
    try {
      if (location.href === lastHref) return;
      lastHref = location.href;
      BLK_RPIDS = Object.create(null);
      BLK_WHY = Object.create(null);
      TXT_RPID = Object.create(null);
      TXTASKED = Object.create(null);
      TXT_N = 0;
      hidMain = 0; hid = 0;
      BLK_HOSTS = []; pumpStall = 0; pumpLastLen = -1;
      pumpBusy = false; pumpBegin = 0; pumpHoldAt = 0; pumpWakeAt = 0;
    } catch (e) {}
  }

  /* 计数变化/定时巡检: 顶部那一行 + 把被屏蔽条目藏好 + 必要时补翻页。
     刚改写完的那一下渲染很快, 所以先连打几个短延时, 之后交给定时巡检兜底。 */
  function hint() {
    cfResetIfNavigated();
    try { syncBar(); } catch (e) {}
    try { sweep(); } catch (e) {}
  }
  function kick() {
    hint();
    var ds = [120, 420, 900];
    for (var i = 0; i < ds.length; i++) {
      (function (d) { try { setTimeout(hint, d); } catch (e) {} })(ds[i]);
    }
  }
  try {
    setInterval(function () {
      if (!hid) return;
      try { hint(); } catch (e) {}
      try { pump(); } catch (e) {}
    }, 900);
  } catch (e) {}

  /* 已解析的响应体 → 过滤后的文本; 无变化/不可序列化 → null(原样放行)
   * isMain: 主列表请求 → 命中的评论只做标记(数据不动), 因此多数时候返回 null,
   *         页面直接用原始响应即可; 只有楼中楼被摘掉时才会真正改写 JSON。 */
  function rewriteObj(o, isMain) {
    try { reportNames(o.data); } catch (e) {}
    var cut;
    MUTATED = 0;
    try { cut = filtAll(o.data, isMain); } catch (e) { return null; }
    if (!cut) return null;
    hid += cut;
    kick();
    if (!MUTATED) return null;   // 主列表只是"标记"(内容/长度都没动) → 让页面用原始响应
    try { return JSON.stringify(o); } catch (e) { return null; }
  }

  /* 把评论接口的响应文本改写成过滤后的文本; 无变化/不可解析 → 返回 null(原样放行) */
  function rewrite(text, isMain) {
    if (!text || typeof text !== "string" || text.charAt(0) !== "{") return null;
    var o;
    try { o = JSON.parse(text); } catch (e) { return null; }
    if (!o || typeof o !== "object" || o.code !== 0 || !o.data || typeof o.data !== "object") return null;
    // 顺手把未知作者预热到壳的队列里(不等待): 下一次(同文档内的翻页/切页)就有标记可用
    if (LOT || EMPTY) { try { askFlags(midList(o.data)); } catch (e) {} }
    return rewriteObj(o, isMain);
  }

  /* 同一个 XHR 只改写一次: 第二次调用时评论已从响应里摘掉, 再算会得到"无变化"
   * → 反而把原始文本还回去。用实例缓存 __msxT 兜住(null = 无需改写)。 */
  function textOut(self, raw) {
    if (self.__msxT !== undefined) return self.__msxT;
    var out = null;
    try { out = rewrite(raw, isMainList(self.__msxU)); } catch (e) { out = null; }
    self.__msxT = out;
    return out;
  }

  function isBinary(v) {
    try {
      if (v instanceof ArrayBuffer || ArrayBuffer.isView(v)) return true;
      if (typeof Blob !== "undefined" && v instanceof Blob) return true;
      if (typeof Document !== "undefined" && v instanceof Document) return true;
    } catch (e) {}
    return false;
  }

  /* ---------------- fetch ---------------- */
  try {
    var _fetch = window.fetch;
    if (typeof _fetch === "function") {
      window.fetch = function (input, init) {
        var url = "";
        try { url = (typeof input === "string") ? input : (input && input.url) || ""; } catch (e) {}
        var p = _fetch.apply(this, arguments);
        if (!isReplyAPI(url) || !active()) return p;
        return p.then(function (res) {
          try {
            if (!res || !res.ok) return res;
            return res.clone().text().then(function (t) {
              var o = null;
              try { o = JSON.parse(t); } catch (e) { o = null; }
              if (!o || o.code !== 0 || !o.data || typeof o.data !== "object") return res;
              var mids = (LOT || EMPTY) ? midList(o.data) : [];
              // 至多等 ASK_WAIT(超时过就 0 等待) —— 本地改写在拿到标记后**同步**完成,
              // 所以评论列表只会被拖 ASK_WAIT, 绝不会"等筛完才放"。
              return askFlags(mids).then(function () {
                var out = null;
                try { out = rewriteObj(o, isMainList(url)); } catch (e) { out = null; }
                if (out === null) return res;
                var r2 = new Response(out, { status: res.status, statusText: res.statusText, headers: res.headers });
                try { Object.defineProperty(r2, "url", { value: res.url }); } catch (e) {}
                return r2;
              }).catch(function () { return res; });
            }).catch(function () { return res; });
          } catch (e) { return res; }
        });
      };
    }
  } catch (e) {}

  /* ---------------- XMLHttpRequest ---------------- */
  try {
    var XP = XMLHttpRequest.prototype;
    var _open = XP.open;
    XP.open = function (m, u) {
      try { this.__msxU = String(u == null ? "" : u); } catch (e) {}
      return _open.apply(this, arguments);
    };
    var dT = Object.getOwnPropertyDescriptor(XP, "responseText");
    var dR = Object.getOwnPropertyDescriptor(XP, "response");
    if (dT && dT.get) {
      Object.defineProperty(XP, "responseText", {
        configurable: true, enumerable: !!dT.enumerable,
        get: function () {
          if (!this.__msxU || !isReplyAPI(this.__msxU) || !active()) return dT.get.call(this);
          var raw;
          try { raw = dT.get.call(this); } catch (e) { return dT.get.call(this); }  // 非 text 类型 → 交给原生照常抛
          var out = textOut(this, raw);
          return out === null ? raw : out;
        }
      });
    }
    if (dR && dR.get) {
      Object.defineProperty(XP, "response", {
        configurable: true, enumerable: !!dR.enumerable,
        get: function () {
          var v = dR.get.call(this);
          if (!this.__msxU || !isReplyAPI(this.__msxU) || !active()) return v;
          try {
            if (typeof v === "string") {   // responseType '' → 与 responseText 同源, 复用同一份缓存
              var o = textOut(this, v);
              return o === null ? v : o;
            }
            if (v && typeof v === "object" && !isBinary(v)) {
              if (v.code === 0 && v.data) {
                try { reportNames(v.data); } catch (e) {}
                if (LOT || EMPTY) { try { askFlags(midList(v.data)); } catch (e) {} }
                MUTATED = 0;
                var cut = filtAll(v.data, isMainList(this.__msxU));
                if (cut) { hid += cut; kick(); }
              }
              return v;
            }
          } catch (e) {}
          return v;
        }
      });
    }
  } catch (e) {}
})();
