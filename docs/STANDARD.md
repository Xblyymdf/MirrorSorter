# 洞鉴虚明 · 项目标准

本文件是仓库的唯一规范入口：目录怎么放、版本怎么加、代码怎么改、测试怎么做、
exe 怎么构建、更新怎么发布、什么该进 git。改代码前先看第 3、4、5 节。

- 仓库：<https://github.com/Xblyymdf/MirrorSorter>
- 桌面当前版本：`v0.4.1dsk`
- 数据目录：`%USERPROFILE%\AppData\LocalLow\MirrorSorter`
- 本地服务：`127.0.0.1` 的 `8000 / 8010 / 8020`（被占用时依次回退）

---

## 1. 项目构成

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 后端 | `desktop/app.py` | 纯 Python 标准库：抓取、筛选、AI、SQLite、HTTP 服务 |
| 桌面壳 | `desktop/app_window.py` | pywebview + WebView2，无边框主窗口、标签页、悬浮窗 |
| 壳部件 | `desktop/shell_*.py` | 标签栏、控件池、页面注入、悬浮窗 |
| 前端 | `desktop/web/` | `index.html` + `app.js` + `style.css`，由本地服务直接提供 |
| 注入脚本 | `desktop/extensions/` | B站自有 UI、评论筛选、超分、净化、SponsorBlock 等 userscript |
| 模型 | `desktop/model/` | 本地 fastText 权重（约 59MB，已入库） |
| 安卓 | `android/` | Chaquopy 壳，同步 `app.py + web/` 进 APK |
| 文档 | `docs/` | 本规范与发布说明 |

桌面默认入口是 `启动.bat` → `pythonw app_window.py`。`app.py` 也支持单独作为
无头服务运行；`web/` 是唯一前端，没有第二套需要同步的 UI。

---

## 2. 目录与 git 约定（什么该上传）

```
MirrorSorter/
├─ README.md              项目介绍
├─ FEEDBACK.md            反馈指南
├─ .gitattributes         换行 / 二进制类型
├─ .gitignore             构建产物、探针、私有内容
├─ docs/
│  └─ STANDARD.md         本文件
├─ desktop/
│  ├─ app.py              后端（单文件，不拆模块）
│  ├─ app_window.py       桌面壳入口
│  ├─ shell_*.py          壳部件
│  ├─ web/                前端
│  ├─ extensions/         注入脚本
│  ├─ model/              fastText 权重
│  ├─ test_*.py           常驻回归测试
│  ├─ _test_*.py/.js      本版新增的回归资产（按需白名单入库）
│  ├─ 启动.bat / 构建exe.bat / 发布更新.bat …
│  └─ build/              构建产物（git 忽略）
└─ android/
```

### 必须遵守

1. **不提交构建产物**：`desktop/build/`、`releases/`、`*.exe`、`*.apk`、`*.spec`
   都在 `.gitignore` 里。发布 exe 走 GitHub Releases（第 7 节），绝不塞进 git 历史。
2. **不提交用户数据**：`data.db*`、登录 profile、测试临时目录全部忽略。
3. **不提交探针垃圾**：`desktop/_*` 默认忽略。只有需要长期重跑的测试脚本，
   在 `.gitignore` 末尾用 `!desktop/_test_xxx.py` 单独放行。
4. **大文件**：GitHub 单文件上限 100MB。`desktop/model/ft_model.bin` 已入库；
   不要再往 git 里加接近上限的二进制。新模型优先放 Releases 或改用 Git LFS，
   并在改动前说明。
5. **换行**：仓库内存 LF，检出的 Windows 源码/脚本是 CRLF（见 `.gitattributes`）。
   用 Python 改文件时保持 `newline=""`，不要顺手把整文件换成 LF。
6. **提交信息**：中文，写“为什么”，一行说清；例如
   `修 ai 侧边栏拉伸方向反了；版本升到 v0.4.0dsk`。
7. **推送与发布分开**：`git push` 只上传源码；`gh release create` 才发布安装包。
   两者都不要在未确认时自动执行。

### 为什么这样放

- 源码路径（`app.py`、`web/`、`extensions/`、`model/`）保持不动：
  安卓同步脚本、PyInstaller 参数、注入路径都按这些坐标写死，移动会连带改一圈。
- 新增内容只往 `docs/`、新测试脚本、发布脚本里放，降低回归面。

---

## 3. 版本规范

### 桌面

格式：`v主.次.补dsk`，例如 `v0.4.1dsk`。

- **每一次用户可见的修改都必须 +0.0.1**（修 bug、加功能、改文案都算）。
- 版本号有三处必须同步，缺一处就会出现“界面显示旧版 / 更新器比较错”：
  1. `desktop/app.py` 第 3 行头注释：`洞鉴虚明 · 基础版 (vX.Y.Zdsk)`
  2. `desktop/app.py` 的 `APP_VER = "vX.Y.Zdsk"`
  3. `desktop/web/index.html` 的两处页脚：`洞鉴虚明 vX.Y.Zdsk`（各 1 处，共 2 处）
- `APP_VER` 同时用于窗口标题、设置页版本、更新检查和 Release tag。

### 安卓

格式以 `android/app/build.gradle` 的 `versionName` 为准，当前是 `0.1.5.30e`。
桌面与安卓共用 `app.py`，但**版本号各自维护**，不要互相覆盖。

### Git tag / Release

- tag 与桌面 `APP_VER` 完全一致：`v0.4.1dsk`。
- 自动更新按数字段比较（`v0.4.1dsk` → `(0,4,1)`），后缀 `dsk` 不参与比较。
- 预发布（draft / prerelease）不会被 `/releases/latest` 返回，可用于内部测试。

---

## 4. 本地开发

### 环境

- Windows 10/11，Python 3.10+（构建机使用 `C:\Python314\python.exe`）。
- 后端无第三方依赖；桌面壳需要 `pywebview` 与 WebView2 运行时。
- 构建 exe 需要 PyInstaller（`构建exe.bat` 会检查并提示）。
- Node 只用于跑注入脚本/前端的沙盒测试，不是运行时依赖。

### 入口

```bat
cd desktop
python app_window.py            :: 桌面窗口（等于 启动.bat）
python app_window.py --server   :: 无头服务 + 默认浏览器
python app.py                   :: 纯服务
```

### 改代码

- **不要用会吞掉 CRLF 的编辑器批量重写**。本项目多数源文件是 CRLF。
- 推荐用 Python 脚本做精确替换：`io.open(..., newline="")` 读 → `str.replace`
  → 断言只命中 1 次 → 原样写回 → 回读校验。
- 改完立刻语法检查：

```bat
python -m py_compile app.py app_window.py shell_pages.py
node --check web/app.js
```

- 前端只改 `web/`，不要另起一套页面；后端只改 `app.py`，不要为更新器之类的小功能
  新增 Python 模块 —— 安卓同步只拷贝 `app.py + web/`。

### 数据隔离

任何会写数据库/设置的测试，都必须设置独立数据目录：

```bat
set MIRRORSORTER_DATA_DIR=%TEMP%\msx-test
python app.py
```

端口可用 `MIRRORSORTER_PORT=18000` 固定，避免抢用户正在用的服务。

---

## 5. 测试与验收

### 常驻回归

```bat
cd desktop
python test_shell.py       :: 壳/标签页/版本同步等
python test_smoke.py       :: 后端主流程
python test_rules.py       :: 规则引擎
python test_chat.py        :: AI 聊天
```

### 本版专项（v0.4.1dsk 自动更新）

```bat
python _test_update_v041.py        :: 78 项：版本比较/Release 解析/下载/替换脚本/路由/桥
node  _test_update_ui_v041.js      :: 20 项：按钮显示、确认弹窗、下载进度、失败提示
python _verify_exe_v041.py <exe>   :: 打包后反解校验
```

Node 若不在 PATH，可用 Qoder 自带 Node：

```bat
set ELECTRON_RUN_AS_NODE=1
"E:\Qoder\Qoder.exe" --check web/app.js
```

### 三层验收（改壳/打包相关时必须做）

1. **源码层**：`py_compile` + 对应单元/行为测试。
2. **归档层**：反解 exe，确认 `web/app.js`、`index.html`、注入脚本、关键常量都在包里。
3. **运行层**：用隔离数据目录与随机端口真起服务，逐接口发请求断言；
   不抢用户屏幕、不截屏、不弹可见窗口。

### UI 改动

- 版式/CSS 用无头 Chrome 渲染截图检查，不靠脑补。
- WebView2 壳的行为优先用无头/静态证据；必须开窗的测试才开，且要能自动退出。
- 前端交互可以用 `node + vm + 假 DOM` 抽取真实代码跑（参考 `_test_update_ui_v041.js`）。

### 已知基线

`test_shell.py` 历史上有 17 条与本轮无关的失败（2026-10-19 记录为 163 PASS / 17 FAIL）。
判断回归时先和这个基线比，不要一看到失败就归因给本次改动。

---

## 6. 构建

```bat
cd desktop
构建exe.bat
```

- 产物：`desktop\build\dist\洞鉴虚明vX.Y.Zdsk.exe`（单文件，约 78MB）。
- 中间件：`desktop\build\pybuild\`、`build\spec\`。
- 安装包：`desktop\installer\构建安装包.bat` →
  `desktop\build\dist\洞鉴虚明vX.Y.Zdsk_安装包.exe`。
- 清理：`desktop\清理构建.bat`（会连缓存一起删）。
- 构建脚本从 `app.py` 读 `APP_VER` 决定文件名；**先改版本再构建**。
- 构建完成后用 `_verify_exe_v041.py`（或对应版本的反解脚本）检查包内容。
- `build/` 永远不进 git。

---

## 7. 自动更新（v0.4.1dsk 起）

### 用户看到什么

- Windows 桌面版 exe 的左栏，**「设置」按钮正下方**有「检测更新」。
- 点击 → 读 GitHub Releases 最新版 → 有新版则弹窗显示版本号、大小、更新说明，
  询问「立即更新」。
- 确认后下载 → 显示进度 → 校验 → 自动关闭程序 → 覆盖旧 exe → 从原目录重启。
- 源码运行、安卓、非 Windows 不显示该按钮（`shell_info.self_update = false`）。

### 技术约束

- 更新包只从 **GitHub Releases** 取；仓库里不存 exe。
- 只接受 GitHub 官方域名（`github.com` / `*.githubusercontent.com`）的 https 附件。
- 下载到 `%USERPROFILE%\AppData\LocalLow\MirrorSorter\update\`。
- 校验：大小 ≥ 5MB、文件头 `MZ`、Release 提供 `digest` 时校验 SHA256。
- 安装包/安装器（名字含「安装包 / installer / setup / install」）会被跳过，
  避免把 280MB 的 NSIS 安装包当成主程序。
- 替换脚本是 GBK + CRLF 的 `.cmd`：
  - 先把旧 exe 备份成 `<原名>.bak`；
  - 每秒重试 `move /y`，最多约 3 分钟，等 Windows 释放正在运行的 exe；
  - 覆盖后保留**原文件名**（快捷方式/任务栏固定项不失效），从原目录 `start`；
  - 脚本执行完自删。
- 回滚：关闭程序，把 `<原名>.bak` 改名回 `<原名>` 即可。
- 更新失败不会丢数据：设置、登录、历史都在数据目录，exe 只替换程序本体。

### 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/update/check` | 读 `releases/latest`，返回是否有新版 |
| POST | `/api/update/download` | 后台下载最近一次检查到的 exe |
| GET | `/api/update/progress` | 下载进度 / `ready` / `error` |
| JS 桥 | `pywebview.api.install_update()` | 校验并拉起替换脚本，然后关窗 |

仓库地址可用环境变量 `MIRRORSORTER_UPDATE_REPO` 覆盖（默认 `Xblyymdf/MirrorSorter`），
方便 fork 自测。

### 发布一次更新（维护者）

1. 改版本号（三处，见第 3 节），跑测试。
2. `构建exe.bat`，确认 `build\dist\洞鉴虚明vX.Y.Zdsk.exe` 存在。
3. 提交并推送源码（`git push`）。
4. 双击 `desktop\发布更新.bat`：
   - 先做 `check` 自检（工具、登录、exe、tag 冲突、工作区）；
   - 确认后推送当前分支并创建 Release，**只上传独立 exe**；
   - Release 说明用 GitHub 自动生成。
5. 打开 Release 页面确认：tag 是 `vX.Y.Zdsk`、附件是独立 exe、不是 prerelease。
6. 用旧版点一次「检测更新」，走完下载/替换/重启。

> 发布脚本不会自动运行；本次只做本地提交，不推送、不创建 Release。
> 真正发布时由维护者手动执行并二次确认。

---

## 8. 安全与隐私

- 登录凭证（B站 Cookie / 贴吧 BDUSS / 小黑盒 session）只存本机 `data.db`。
- 仓库不包含任何用户数据、密钥、cookie、日志。
- 页面调用本地服务的 CORS 白名单只放行 `bilibili.com` / `*.bilibili.com`
  的两个端点（`/api/ui_settings`、`/api/bili/flags`）；新增页面端点时必须一起改。
- 更新下载只允许 GitHub 官方 https 地址；替换脚本路径做转义，拒绝换行。
- 不把本地服务暴露到 `0.0.0.0`；默认只监听 `127.0.0.1`。

---

## 9. WebView2 壳的红线

- **绝不给 `<video>` 加 CSS**（圆角/透明度/filter 会让视频变成独立合成层，
  GDI 截屏取到黑）。
- 对 SPA（B站）做 DOM 手术要等站点头部切换完成；能用接口响应做切面就别用 DOM 选择器。
- 注入脚本必须：`document-start` + MAIN world、hostname/path 门控、幂等标记、
  顶层框架守卫（`window.top === window.self`）。
- 大图模式行高依赖封面留在文档流里；叠加效果只能放在绝对定位的辅元素上。
- 加载幕布（`_NOFLASH_JS`）的 hold/release 契约不要破坏；ownui 挂载流程必须保留
  `bootMaskOn/bootMaskOff` 两个出口。
- 调试钩子 `_DEBUG_HOOK_JS` 默认不注入（`MSX_DEBUG=1` 才开），否则每个失败请求
  都会走一次 IPC + 落盘，明显拖慢加载。

更细的踩坑记录在 `desktop/` 的测试脚本注释与历史提交里；改壳前先搜相关关键词。

---

## 10. 常见问题

| 现象 | 处理 |
| --- | --- |
| 点「启动.bat」还是旧界面 | 确认 `app_window.py` 导航到 `/`，且 `web/index.html` 已更新；旧版 exe 不会被源码影响，需重新构建 |
| 构建出的 exe 版本号不对 | 先改 `app.py` 的 `APP_VER`，`构建exe.bat` 按它命名 |
| 检测更新提示没有 Release | 仓库还没发布，或 Release 没有附带 `.exe` |
| 更新提示“程序目录不可写” | 把 exe 放到桌面/下载等用户目录再更新 |
| 更新后想回退 | 关闭程序，把同目录的 `<原名>.bak` 改名回 `<原名>` |
| 端口被占用 | 服务会自动回退 8000 → 8010 → 8020；测试时用 `MIRRORSORTER_PORT` 固定 |

---

反馈与交流：QQ 群 **533068204**。提 issue 前先看 [FEEDBACK.md](../FEEDBACK.md)。
