# -*- coding: utf-8 -*-
"""v0.4.41dsk 反解校验: 智能体引擎/前端动作落地/备注归一化是否进包。"""
import os
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader  # noqa: E402

EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "build", "dist", "洞鉴虚明v0.4.41dsk.exe")
FAIL = []
TOTAL = [0]


def chk(cond, msg):
    TOTAL[0] += 1
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAIL.append(msg)


def payload(v):
    return v[1] if isinstance(v, tuple) else v


car = CArchiveReader(EXE)
toc = car.toc


def res(name):
    key = next((k for k in toc if k.replace("\\", "/") == name), None)
    return payload(car.extract(key)) if key else None


chk(os.path.getsize(EXE) > 70 * 1024 * 1024,
    "exe 体积正常 (%.1f MB)" % (os.path.getsize(EXE) / 1048576.0))
html = (res("web/index.html") or b"").decode("utf-8", "replace")
js = (res("web/app.js") or b"").decode("utf-8", "replace")
css = (res("web/style.css") or b"").decode("utf-8", "replace")
chk("v0.4.41dsk" in html, "index.html 页脚是 v0.4.41dsk")
chk("v0.4.40dsk" not in html, "index.html 没有残留 v0.4.40dsk")
chk("ui: { theme: prefs.theme" in js, "app.js 上报界面状态给智能体")
chk("function actsRow(acts)" in js, "app.js 渲染智能体动作行")
chk("if (data.cid) cid = data.cid;" in js, "app.js 接受智能体新对话 cid")
chk("uiActs" in js and "feedQuery" in js and "feed_reload" in js,
    "app.js 落地 ui_actions / feed_query / feed_reload")
chk("aiPlanSettings" not in js, "app.js 已移除旧 /api/chat/plan 预判")
chk(".acts-row" in css and ".agentPhase" in css, "style.css 有动作行与阶段提示样式")

pyz_key = next((k for k in toc if k.endswith(".pyz")), None)
tmp = os.path.join(tempfile.gettempdir(), "msx_verify_v041.pyz")
with open(tmp, "wb") as f:
    f.write(payload(car.extract(pyz_key)))
z = ZlibArchiveReader(tmp)


def scan_code(obj):
    strings, names, funcs = set(), set(), set()
    if not isinstance(obj, types.CodeType):
        return strings, names, funcs

    def walk(co):
        names.update(co.co_names)
        names.update(co.co_varnames)
        funcs.add(co.co_name)
        for c in co.co_consts:
            if isinstance(c, (str, bytes)):
                strings.add(c)
            elif isinstance(c, (tuple, list, set, frozenset)):
                for x in c:
                    if isinstance(x, (str, bytes)):
                        strings.add(x)
                    elif isinstance(x, types.CodeType):
                        walk(x)
            elif isinstance(c, types.CodeType):
                walk(c)

    walk(obj)
    return strings, names, funcs


def module_scan(name):
    obj = z.extract(name)
    if isinstance(obj, tuple):
        obj = obj[-1]
    return scan_code(obj)


strs, names, funcs = module_scan("app")
allapp = strs | names | funcs
blob = "\n".join(s for s in strs if isinstance(s, str))

for fn in ("agent_turn", "agent_system_prompt", "agent_state_text", "_agent_tool",
           "_agent_apply_settings", "_clean_legacy", "_note_add"):
    chk(fn in funcs or fn in names, "app 里有 " + fn)
chk("AGENT_MAX_ROUNDS" in names or "AGENT_MAX_ROUNDS" in allapp, "app 里有 AGENT_MAX_ROUNDS")
chk("AGENT_FAST_CMDS" in names or "AGENT_FAST_CMDS" in allapp, "app 里有 AGENT_FAST_CMDS")
for tool in ("get_state", "set_interface", "set_info_filters", "set_moderation",
             "set_partitions", "manage_dislike", "manage_prefs", "manage_blacklist",
             "temp_block", "search_feed", "refresh_feed", "browse", "feed_items",
             "get_stats", "daily_report", "act_on_current", "set_ai", "floatball",
             "new_conversation"):
    chk(tool in allapp, "工具常量: " + tool)
chk("工具就是你的手" in blob, "系统提示词: 工具就是你的手")
chk("我改不了" in blob and "你去设置页点" in blob, "提示词明确禁止推诿")
chk(any(isinstance(s, str) and s == "v0.4.41dsk" for s in strs), "app 常量版本 v0.4.41dsk")
chk(not any(isinstance(s, str) and s == "v0.4.40dsk" for s in strs), "app 里没有残留 v0.4.40dsk")

print("==== %d PASS / %d FAIL ====" % (TOTAL[0] - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
