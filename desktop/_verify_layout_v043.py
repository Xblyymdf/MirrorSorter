# -*- coding: utf-8 -*-
"""v0.4.3dsk: 搜索按钮避让的真实布局验证（无头 Chrome，不碰用户屏幕）。

做法：把 web/index.html 的真实 <header id="topbar"> + web/style.css 放进测试页，
补上用户截图里在场的「只看 B站」芯片，量三态：
  1) 有芯片（用户报的场景）—— 右侧组左溢压住按钮；
  2) 跑真实 fitSearchBtn() —— 按钮/输入框左让到压入元素左侧；
  3) 去掉芯片再跑一次 —— 右内边距自动回到 9px，不留固定偏移。
并生成 before/after 两张无头截图，供人工核对。
"""
import html
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
SHOT = os.path.join(HERE, "build", "_shot")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PASS, FAIL = 0, []


def ok(cond, name, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("PASS", name)
    else:
        FAIL.append(name)
        print("FAIL", name, extra)


def read(p):
    return io.open(p, encoding="utf-8", newline="").read()


def extract_fn(name, src):
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise RuntimeError("function braces not closed")


def build_page():
    h = read(os.path.join(HERE, "web", "index.html"))
    top = re.search(r'<header id="topbar">.*?</header>', h, re.S).group(0)
    chip = ('<button id="srcChip" class="tb" title="取消只看该平台">只看 B站'
            '<svg class="ic xs"><use href="#i-x"/></svg></button>')
    top = top.replace('<button id="btnClearPool" class="tb quiet dangerHover">清空信息池</button>',
                      '<button id="btnClearPool" class="tb quiet dangerHover">清空信息池</button>' + chip, 1)
    js = read(os.path.join(HERE, "web", "app.js"))
    fn = extract_fn("fitSearchBtn", js)
    page = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="file:///D:/Information%20Sorter/desktop/web/style.css">
</head><body>""" + top + """
<script>
document.querySelectorAll('#topAvatars, #topAvatars .tava').forEach(function(e){e.hidden=false;});
var $ = function(s){ return document.querySelector(s); };
var fitPad0 = 0;
""" + fn + """
function rr(el){ var b=el.getBoundingClientRect();
  return {l:+b.left.toFixed(2), r:+b.right.toFixed(2), w:+b.width.toFixed(2)}; }
function snap(tag){
  var box=$('#topSearch'), btn=$('#btnSearch'), wrap=$('#topRightWrap');
  var lefts=[].slice.call(wrap.children).filter(function(e){return e.getClientRects().length;})
    .map(function(e){return e.getBoundingClientRect().left;});
  return {tag:tag, box:rr(box), btn:rr(btn), ava:rr($('#topAvatars')),
    leftmost:+Math.min.apply(null,lefts).toFixed(2),
    pad:getComputedStyle(box).paddingRight, inputW:rr($('#searchInput')).w};
}
window.addEventListener('load', function(){ setTimeout(function(){
  var shot=(new URLSearchParams(location.search).get('shot')||'');
  var out={shot:shot};
  if (shot === 'before') {            // 截图: 保持被盖住的原状
    out.before=snap('before');
    document.body.setAttribute('data-m', JSON.stringify(out));
    return;
  }
  if (shot === 'after') {             // 截图: 只跑修复, 保留芯片
    fitSearchBtn();
    out.after=snap('after');
    document.body.setAttribute('data-m', JSON.stringify(out));
    return;
  }
  out.before=snap('before');
  fitSearchBtn();
  out.after=snap('after');
  var chip=$('#srcChip');
  if (chip) chip.remove();
  document.body.offsetWidth;
  out.noChip=snap('noChip');
  fitSearchBtn();
  out.restored=snap('restored');
  out.overlapBefore=+(out.before.box.r-out.before.leftmost).toFixed(2);
  out.overlapAfter=+(out.after.box.r-out.after.leftmost).toFixed(2);
  out.btnClearAfter=+(out.after.leftmost-out.after.btn.r).toFixed(2);
  out.overlapNoChip=+(out.noChip.box.r-out.noChip.leftmost).toFixed(2);
  document.body.setAttribute('data-m', JSON.stringify(out));
}, 80); });
</script></body></html>"""
    return page


def chrome(width, *args, timeout=60):
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
           "--force-device-scale-factor=1", "--window-size=%d,300" % width,
           "--user-data-dir=" + os.path.join(SHOT, "prof_v043_%d" % width)] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r


def measure(url, width):
    r = chrome(width, "--virtual-time-budget=1500", "--dump-dom", url)
    dom = r.stdout.decode("utf-8", "replace")
    m = re.search(r'data-m="([^"]*)"', dom)
    return json.loads(html.unescape(m.group(1))) if m else None


def main():
    if not os.path.exists(CHROME):
        print("FAIL 找不到 Chrome:", CHROME)
        return 1
    os.makedirs(SHOT, exist_ok=True)
    page_path = os.path.join(SHOT, "searchbtn_v043.html")
    io.open(page_path, "w", encoding="utf-8", newline="").write(build_page())
    url = "file:///" + page_path.replace("\\", "/").replace(" ", "%20")

    # 1) 几何量测（默认窗口 1425；最小窗口 1080）
    d = measure(url, 1425)
    ok(d is not None, "页面完成量测")
    if not d:
        return 1
    print(json.dumps(d, ensure_ascii=False, indent=1))

    ok(d["before"]["box"]["w"] == 400, "搜索框宽 400（桌面档）", d["before"]["box"]["w"])
    ok(d["overlapBefore"] > 10, "复现：有芯片时右侧组压进搜索框", d["overlapBefore"])
    ok(d["before"]["btn"]["r"] > d["before"]["leftmost"],
       "复现：按钮右端在压入元素左侧之外（被盖住）",
       [d["before"]["btn"]["r"], d["before"]["leftmost"]])
    want_pad = 9 + math.ceil(d["overlapBefore"])
    ok(abs(float(d["after"]["pad"].replace("px", "")) - want_pad) < 0.01,
       "避让量 = 基准 9px + 上取整的压入宽度", [d["after"]["pad"], want_pad])
    ok(d["btnClearAfter"] >= 5, "修复后按钮与压入元素至少留 5px", d["btnClearAfter"])
    ok(d["after"]["btn"]["r"] < d["after"]["box"]["r"] - 20, "按钮仍在搜索框内",
       [d["after"]["btn"]["r"], d["after"]["box"]["r"]])
    ok(d["after"]["btn"]["l"] >= d["after"]["box"]["l"] + 20, "按钮没有被顶出左边界",
       [d["after"]["btn"]["l"], d["after"]["box"]["l"]])
    ok(d["after"]["inputW"] >= 60, "输入区仍可用（>=60px）", d["after"]["inputW"])
    ok(abs(d["after"]["leftmost"] - d["before"]["leftmost"]) < 0.6,
       "避让没有挪动右侧组本身的布局",
       [d["after"]["leftmost"], d["before"]["leftmost"]])
    ok(d["overlapNoChip"] <= 0, "去掉芯片后本来就不该压住", d["overlapNoChip"])
    ok(d["restored"]["pad"] == "9px", "空间恢复后右内边距回到 9px", d["restored"]["pad"])

    # 最小窗口 1080：溢出更大，夹紧后按钮仍必须在压入元素左侧
    d2 = measure(url, 1080)
    ok(d2 is not None, "最小窗口量测完成")
    if d2:
        print(json.dumps({k: d2[k] for k in ("before", "after", "overlapBefore", "btnClearAfter")},
                         ensure_ascii=False, indent=1))
        ok(d2["before"]["box"]["w"] < 400, "1080 窗口下搜索框已按 32vw 收窄",
           d2["before"]["box"]["w"])
        ok(d2["overlapBefore"] > 150, "1080 下确实严重溢出", d2["overlapBefore"])
        ok(d2["btnClearAfter"] >= 5, "1080 下按钮仍避让成功", d2["btnClearAfter"])
        ok(d2["after"]["btn"]["r"] < d2["after"]["box"]["r"], "1080 下按钮仍在框内",
           [d2["after"]["btn"]["r"], d2["after"]["box"]["r"]])
        ok(d2["after"]["inputW"] >= 20, "1080 下输入区没有被挤成负宽", d2["after"]["inputW"])

    # 2) 无头截图（before 不跑修复 / after 跑修复）
    for mode in ("before", "after"):
        p = os.path.join(SHOT, "searchbtn_v043_%s.html" % mode)
        io.open(p, "w", encoding="utf-8", newline="").write(build_page())
        shot = os.path.join(SHOT, "searchbtn_v043_%s.png" % mode)
        u = "file:///" + p.replace("\\", "/").replace(" ", "%20") + "?shot=" + mode
        r = chrome(1425, "--virtual-time-budget=1500", "--screenshot=" + shot, u)
        ok(os.path.exists(shot) and os.path.getsize(shot) > 5000, "生成 %s 截图" % mode,
           os.path.getsize(shot) if os.path.exists(shot) else "missing")
    for w in (1425, 1080):
        shutil.rmtree(os.path.join(SHOT, "prof_v043_%d" % w), ignore_errors=True)
    print("\nRESULT:", "OK" if not FAIL else "FAIL " + ", ".join(FAIL), "(%d PASS)" % PASS)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
