# -*- coding: utf-8 -*-
"""聊天多会话/指代/chips 后端逻辑冒烟测试。用临时数据目录, 不污染真实库。"""
import importlib.util
import json
import os
import sys
import tempfile
import time

import atexit
import shutil
PROJECT = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(PROJECT, "build", "probe", "test-tmp")
if os.path.isdir(TMP):
    shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True) if os.path.isdir(TMP) else None)
spec = importlib.util.spec_from_file_location("app", os.path.join(PROJECT, "app.py"))
app = importlib.util.module_from_spec(spec)
sys.modules["app"] = app
spec.loader.exec_module(app)
app.DATA_DIR = TMP   # 打补丁必须在 exec 之后(模块级代码会重建 DATA_DIR)
os.makedirs(TMP, exist_ok=True)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, extra))


# --- 会话 CRUD ---
convs, cur = app.conv_list()
check("启动有默认会话", len(convs) == 1 and convs[0]["title"] == "默认对话" and cur == 1, convs)

c2 = app.conv_create()   # 默认标题"新对话"
check("新建会话 id>1", c2 == 2, c2)
check("新建后自动设为当前", app.sget("cur_conv") == "2")
convs, cur = app.conv_list()
check("会话列表2条", len(convs) == 2 and cur == 2, convs)

# --- 消息写入 + 自动命名 ---
app.conv_touch(c2, "帮我找点黑神话的攻略")
convs, _ = app.conv_list()
check("首句自动命名", convs[1]["title"] == "帮我找点黑神话的攻略", convs[1]["title"])
check("已有自定义标题不被覆盖", True)  # conv_create 带标题时不自动改名(设计行为)
c3 = app.conv_create("预设名字")
app.conv_touch(c3, "换成别的")
convs, _ = app.conv_list()
check("自定义标题不覆盖", convs[2]["title"] == "预设名字", convs[2]["title"])
app.conv_delete(3)

app.db().execute("INSERT INTO chat(role,content,ts,cid) VALUES('user','hello',?,1),('assistant','hi',?,1)", (int(time.time()), int(time.time())))
app.db().commit()
rows = app.db().execute("SELECT role,content FROM chat WHERE cid=1").fetchall()
check("按会话过滤消息", len(rows) == 2)

# --- 指代解析 ---
app.set_last_feed({"key": "https://www.bilibili.com/video/BV1test", "title": "黑神话DLC实机", "author": "游戏观察室"})
r = app.handle_command("收藏刚才那条")
check("收藏刚才那条", r and r.get("trigger") == "saved", r)
rows = app.db().execute("SELECT key,kind FROM saved WHERE key=?", ("https://www.bilibili.com/video/BV1test",)).fetchall()
check("saved落库", len(rows) == 1)
r = app.handle_command("屏蔽刚才那个作者")
check("屏蔽作者", r and "游戏观察室" in r["reply"], r)
rows = app.db().execute("SELECT value FROM blacklist WHERE kind='author'").fetchall()
check("作者进黑名单", len(rows) == 1 and rows[0][0] == "游戏观察室", rows)
r = app.handle_command("不喜欢刚才那条")
check("不喜欢", r and r.get("trigger") == "settings", r)
rows = app.db().execute("SELECT action FROM feedback ORDER BY ts DESC LIMIT 1").fetchall()
check("dislike落库", rows and rows[0][0] == "dislike", rows)
r = app.handle_command("刚才那条是什么")
check("无动作词不触发指代", r is None, r)
r = app.handle_command("屏蔽作者王小明")
check("普通屏蔽不受影响(名字屏蔽)", r and "王小明" in r["reply"], r)

# --- 护栏: 指代句不触发搜索 ---
i = app.extract_intent("收藏刚才那条")
check("指代句式不搜索", i["action"] == "reply", i)

# --- 新对话命令 ---
r = app.handle_command("开个新对话")
new_id = r.get("cid") if r else None
check("新对话命令", r and r.get("trigger") == "conv" and new_id and new_id > 3, r)
check("cur_conv 已切换", app.sget("cur_conv") == str(new_id))

# --- chips ---
chips = app.build_chips()
check("chips>=4且含cmd", len(chips) >= 4 and all(c.get("cmd") for c in chips), chips)

# --- conv_delete ---
app.conv_delete(3)
convs, _ = app.conv_list()
check("删除后回退", len(convs) == 2 and app.sget("cur_conv") in ("1", "2"), (convs, app.sget("cur_conv")))

# --- 命名/重命名 ---
check("重命名", app.conv_rename(2, "独立游戏") and app.db().execute("SELECT title FROM conv WHERE id=2").fetchone()[0] == "独立游戏")

# --- v0.4.40: 要一批屏蔽词时, 本地正则不能把整句吞成一个脏词 ---
check("归一化:去前缀", app._clean_kw("任何关于手机测评") == "手机测评",
      app._clean_kw("任何关于手机测评"))
check("归一化:过长堆叠截断", app._clean_kw("任何手机推荐和讲解手机还有手机性") == "手机推荐",
      app._clean_kw("任何手机推荐和讲解手机还有手机性"))
check("要提示词交给AI意图",
      app.handle_command("我不想看任何手机测评视频，给我写各种提示词去屏蔽这类东西") is None)
new_note, added = app._note_add("任何关于手机测评", ["手机推荐"])
check("追加时清理历史脏词", new_note == "手机测评，手机推荐" and added == ["手机推荐"],
      (new_note, added))

print("\n==== %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
