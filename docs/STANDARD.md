# 洞鉴虚明 · 项目标准

本文件是仓库的唯一规范入口：目录怎么放、版本怎么加、代码怎么改、测试怎么做、
exe 怎么构建、更新怎么发布、什么该进 git。改代码前先看第 3、4、5 节。

- 仓库：<https://github.com/Xblyymdf/MirrorSorter>
- 桌面当前版本：以 `desktop/app.py` 的 `APP_VER` 为准（本文写作时为 `v0.4.13dsk`）
- 数据目录：`%USERPROFILE%\AppData\LocalLow\MirrorSorter`
- 本地服务：`127.0.0.1` 的 `8000 / 8010 / 8020`（被占用时依次回退）

---

## 1. 项目构成

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 后端 | `desktop/app.py` | 纯 Python 标准库：抓取、筛选、AI、SQLite、HTTP 服务 |
| 桌面壳 | `desktop/app_window.py` | pywebview + WebView2，无边框主窗口、标签页、悬浮窗 |
| 壳部件 | `desktop/shell_*.py` | 标签栏、控件池、页面注入、悬浮窗、右侧 AI 面板（`shell_ai.py`） |
| 前端 | `desktop/web/` | `index.html` + `app.js` + `style.css`，由本地服务直接提供 |
| AI 面板页 | `desktop/web/panel.*` | 内置浏览器右侧面板自己的小页面（`/panel`），不复用主界面样式 |
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
- **拖动/滚动这类每帧都跑的处理器里，不许调会重排全文档的函数**。主界面分栏的卡顿根因就是
  每帧 `applyPrefs()`（内部重写 6 个颜色变量 + 强制重排）；正确做法是 `requestAnimationFrame`
  合帧、只写必要的 CSS 变量，松手再做一次完整收尾（`applyPrefs()` + 落库）。
- **更强的一条：拖动期间连"真实宽度"都别改。** 只要宽度变了，grid 列宽就变、内容区就 resize，
  里面几百张卡片要重新折行、封面重绘、canvas/视频按新尺寸重画 —— 合帧也救不了。
  正确做法是拖动时只移动一条 `position:fixed` 的预览线（位置用 `transform` 表达，走合成层），
  松手才把宽度一次性落地。原生控件同理：拖动只挪那条 2px 分栏线，松手才 `layout()`。

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
python test_ai_panel.py    :: 右侧 AI 面板（状态/桥/工具分派/宽度夹取/分栏修复）
python _shot_chrome.py     :: 顶栏（标签条+导航栏）真渲染成 PNG：深/浅/悬停/窄窗四张
node  _test_ai_page_js.js  :: 注入网页的助手脚本（vm + 假 DOM，断言真的发了哪些调用）
node  _test_splitghost_zoom.js :: 分栏拖动预览线（抽真实代码 + 按 zoom 语义的假 DOM）
```

`_shot_chrome.py` 是**原生自绘 UI 的验收工具**：顶栏是 WinForms GDI+ 画的，改一个半径/
间距靠读代码判断不了好不好看，也判断不了弧线方向反了没有（不会报错，只会变丑）。它把
`shell_tabstrip`/`shell_navbar` 装进一个**挪到屏幕外**的 Form，直接 `DrawToBitmap` 出图，
不抢焦点、可重复。★ 必须真 `Show()` 一次：不 Show 时子控件 `Visible=False` 且没有句柄，
`DrawToBitmap` 会把原生地址栏 TextBox 整个跳过，截出来的图里网址是空的。
产物在 `build/probe/chrome_{dark,light,hover,narrow}.png`。

`test_smoke.py` 要用系统 `C:\Python314\python.exe`（托管 3.13 无 tkinter，「体检:本机可跑悬浮窗」会 FAIL）。

### 本版专项（v0.4.44dsk 顶栏视觉重做 + 图标归位）

```bat
python _shot_chrome.py        :: 出图肉眼比对 Chrome/Edge；改顶栏必跑
python test_shell.py          :: 其中「顶栏结构1~8」是这次的结构性约定
:: 真窗口版: 打开真窗、真 WebView2、真配色, 渲染顶栏并断言"标签条末行 == 导航栏首行"
set MIRRORSORTER_DATA_DIR=%TEMP%\msx-chrome-e2e
set MIRRORSORTER_WV2_DATA=%TEMP%\msx-chrome-e2e-wv2
set MIRRORSORTER_PORT=18062
python app_window.py --e2e-chrome
:: 产物 build/probe/chrome_real.png + chrome_e2e.log
```

`_shot_chrome.py` 与 `--e2e-chrome` 是**互补**的两层：前者拿假数据单独渲染两个 Panel（快、
可重复、能造悬停/窄窗等极端态）；后者是唯一能证明"激活标签和工具栏真的连成一片"的地方——
两个 Panel 的**真实贴合**差 1px 就会露出接缝，而这在假数据图里永远看不出来。
本机 150% DPI 下实测 `bar_h=57`（= 38×1.5，`form._scale` 由 pywebview 给），
断言必须按运行时的实际像素比，不能写死 38。

**结构**（第一轮）：对标 Chrome/Edge 的差距在结构不在配色，只调颜色救不回来：

- 标签**相邻无缝隙**，激活标签顶角圆角 + 底边平直，左右各有向外张开的"脚"；
- 激活标签**下沿与工具栏齐平且同色** → 视觉上与下方内容连成一片。所以工具栏底色必须取
  scheme 的 `active`（激活标签色），标签条取 `bar`（更深一档）；收藏栏同理跟着 `active`。
  旧版两者都用 `bar`，激活标签只能靠"更亮"区分 → 永远是浮在条上的小卡片。
- 分隔线只画在两个"都非激活/非悬停"的相邻标签之间；
- ✕ / + / ─ □ ✕ 全部改成 1px 矢量线（文字符号粗细不匀、基线乱飘）；
- 悬停底色取 `active ↔ bar` 的中点，**不要**直接用页面给的 `hover`（深色下它比 `active`
  还亮，悬停一个非激活标签会比激活标签更显眼，层次整个反了）；
- 地址栏是**胶囊**（半径 = 半高），原生 TextBox 必须内缩在胶囊里，否则它这个不透明矩形
  会把圆角切方。

**"廉价感"**（第二轮，用户原话"还是一股廉价感，不如谷歌和 edge 的好"）。逐项消因，按影响
从大到小：

- ★ **ClearType**：`g.TextRenderingHint = ClearTypeGridFit`。GDI+ 默认是灰度 AA，9pt 的
  中文/拉丁字母发虚、笔画糊在一起 —— 这是廉价感最直接的来源，**比配色影响大**。两个
  Panel 的 `on_paint` 开头都要设。
- ★ **字体换 `Segoe UI`**（原来用「Microsoft YaHei」）：雅黑的拉丁字母又宽又钝，一行网址里
  英文数字占大头，一眼廉价。Segoe UI 缺中文字形时 GDI+ 会自动 font-linking 到雅黑，于是
  "英文按 Segoe UI、中文按雅黑" —— 和 Chrome/Edge 在 Windows 上的做法一致。
- 胶囊**平时不描边**（Chrome 的 omnibox 就是一块无边框实色胶囊；描一圈线立刻变成"输入框
  控件"的味道）。悬停给淡描边、聚焦给强调色描边，状态靠边表达。
- 胶囊底色 `_blend(strip, bar, 0.85)`（原 0.55，太淡显得脏）；地址栏字色未悬停时 `0.82`
  （原 0.7，那一档在深色下像"禁用态"）。
- 激活标签顶边加一道 `alpha=30` 的白内高光，让它像"浮起来的一张纸"而不是纯色矩形；
  分隔线降到 `alpha=150`（满色在深色条上像表格框线）。
- 后退/前进/刷新 **BTN_GAP=0 贴紧成一组**（Chrome 就是这样），圆形悬停底才连得起来；
  按钮直径 `-6*scale`（原 -8，太小气）。
- AI 圆钮打开时改成**强调色实心圆 + 白字**（原来 56 alpha 淡底 + 同色字，太虚）；
- 高度：标签条 38 / 导航栏 40（原 36/38），接近 Chrome 的 40 上下。

**图标**（第三轮，用户明确要求"信息流的图标改成软件图标，搜索界面的图标改成放大镜"）：

- 信息流（首页/固定页，`not closable`）→ **软件自己的图标** `web/favicon.ico`；
- 其它标签 → **真实站点图标**（`FaviconCache.image`，只读内存/磁盘命中，绝不联网等待），
  拿不到才退化成"域名取色 + 首字母"瓦片；
- 地址栏**空 / 非 http(s)** → **放大镜**（此时它是搜索框，不是网页）；https → 锁；其余 → 地球。
- ★★ 随包 `web/favicon.ico` 是**单帧 PNG 压缩** ico，`System.Drawing.Icon.ToBitmap()` /
  `BmpFrame()` 解不了（实测抛 `ArgumentOutOfRangeException: 请求的范围扩展超过了数组的结尾`）。
  必须按 ICO 目录项 `struct.unpack("<BBBBHHII", …)` 取出 PNG 字节交给 `Image.FromStream`。
- ★ 标签栏与收藏栏**共用同一个 `FaviconCache`**（`app_window` 里建 `fav_cache`，再分别传
  `favicons=` / `icons=`）。各自 new 一个会把同一站点抓两遍，两处缓存命中还不同步。
- ★ 图标是后台抓的，`favicons.subscribe(_on_icon)` 抓到就 `Invalidate`，否则要等下一次
  鼠标事件才更新。

**两个验收的坑**（都踩过）：

- `--e2e-chrome` 的"工具栏同色"断言**不能用单点采样**：中间行会撞上图标/文字，开了 ClearType
  之后连字形边缘都是彩色的（实测撞出 `#96795a` 次像素边）。改成扫首末行整条
  （`range(0, width, 37)`）—— 首末行不会被任何元素占用（胶囊上下内缩 6 逻辑像素，圆形按钮居中收窄）。
- 断言"导航栏首行 == 导航栏底色"是**同义反复**（接缝再大也 PASS），必须写成
  "标签条末行 == 导航栏首行 == 工具栏底色"。

### 本版专项（v0.4.45dsk 顶栏 AI 入口收进胶囊）

```bat
python _shot_chrome.py        :: 新增 chrome_ai.png(悬停) / chrome_ai_on.png(展开, 强调色)
python test_ai_panel.py       :: 58 / 59 / 59b / 59c 是这次的结构性约定
python test_shell.py          :: 顶栏结构5 改成"TextBox 还得给 chip 让位"
```

- 栏尾那颗"描边圆环 + `AI` 文字"的孤岛圆钮删了：入口改成地址栏胶囊右端 28×24 的 chip
  （四角星矢量、平时**不描边**）。展开态 = accent 16% 轻底 + accent 图标，不用实心块 ——
  深色顶栏上实心强调色太重，会把视线从地址栏抢走。
- accent 不再写死 `#4C8DFF`：`install_navbar(..., accent_getter=)` 吃 `float_ball_accent`，
  与标签栏/悬浮球/AI 面板同源。以前同屏两个强调色，是最典型的廉价感来源。
- 空地址栏用 `EM_SETCUEBANNER` 打占位「搜索或输入网址」，不再是一大条空白胶囊。
- `set_ai_ok(tab_ok)`：信息流/悬浮窗上 chip 进禁用态，`toggle_ai_panel` 返回 `"blocked"`；
  以前那里点下去会高亮但面板永不出现（说谎的控件比没有按钮更糟）。
- `AIPanel.focus_input()`：打开面板后立刻把焦点送进输入框（以前还得再点一次输入框）。
- 完整设计规范与未做的 P1/P2（Alt+A、tooltip、`?` 前缀直问、胶囊限宽居中）见
  [TOPBAR.md](TOPBAR.md)。

**v0.4.46dsk 补丁（站点图标被裁）**：150% DPI 下地址栏左端的放大镜只剩右半圈 + 手柄 ——
裁切线就是 `DrawString` 的布局矩形边界。MDL2 的搜索/锁字形带**负 bearing**（墨迹比 advance
更靠左），而布局矩形只给了 `16*scale`，GDI+ 就沿矩形左边把墨迹切掉。修法：站点图标单独用一份
`sf_icon`（`StringFormatFlags.NoClip` + `Trimming=None`），矩形左右各留 `4*scale` 余量（中心不变）。

★ 这个 bug 在 `_shot_chrome.py` 里**永远看不出来**：它固定 `form._scale=1.0` 且进程 DPI-unaware。
复现必须走 `_probe_icon445dpi.py`（先 `SetProcessDpiAwareness(2)`，再按 1.25/1.5 渲染）。
改任何导航栏字形（站点图标 / 锁 / 地球 / 放大镜）都要用它过一遍。

### 本版专项（v0.4.48dsk 分栏预览线跟着缩放走）

```bat
node _test_splitghost_zoom.js           :: 33 项：把手/跟手/落宽/夹取/折叠/无 NaN
python test_shell.py                    :: 「分栏预览线」三条静态守门
python _verify_exe_panel.py             :: 归档 app.js 里必须是新写法
```

- **症状**：字体大小调到「中/大」后，拖分隔条的那条绿线跑到右栏里面去，鼠标明明还在分隔条上，
  而且越往右错得越多。
- **根因**：界面缩放是 `document.body.style.zoom`（见 `applyPrefs`）。`#splitGhost` 原来挂在
  `<body>` 下，于是它自己的 `translate3d(Xpx)` 又被 zoom 乘了一次 ——
  `getBoundingClientRect()` 返回的 `rect0.left` **已经是缩放后的可视坐标**，写进被 zoom 的
  子树里就成了 `X × z`。z=1.15 时一条本该在 674 的线画到 775。
- **修法**：预览线改挂 `<html>`（不在 zoom 子树里，1px 就是 1 个可视像素）；同时把
  `clientX` 的可视位移**除 z** 换算成本地宽度、把 `(target-w0)` 的本地宽度**乘 z** 换算回可视位移。
  只修一半不行：线跟手了栏宽还是错的，反之亦然。
- ★ **一般规律（以后写任何浮层都要记住）**：本项目 `body{zoom}` 参与界面缩放，所以
  - `getBoundingClientRect()` / `clientX` = **可视像素**（含缩放）；
  - `prefs.*W` / `--leftW` / `--rightW` = **本地像素**（不含缩放）；
  - 挂 `body` 下的 `position:fixed` 元素，定位值会被再放大 z 倍，`width` 也会。
  跨这两套坐标系时必须显式乘除 z（`uiZoom()` 读 `--ui-zoom`，缺省 1）。
- ★ 这类 bug **静态断言抓不住**（写进去的数看着是对的，错在渲染时被放大一次），所以
  `_test_splitghost_zoom.js` 把 `app.js` 里真实的 `pointerdown` 代码段抽出来、配一套按
  zoom 语义算几何的假 DOM 真跑；它有敏感性自检（旧实现必挂 15 项）。
- `_verify_exe_panel.py` 现在对 `web/index.html` / `app.js` / `style.css` 做**逐字节比对**，
  堵「改完 web/ 没重新打包」这个老坑。

### 本版专项（v0.4.51dsk 单实例：再点图标只叫回已有窗口）

```bat
python _test_single451.py    :: 单元 9 项 + 真两进程 10 项(第二个必须自己退出)
python test_shell.py         :: 「单实例1~5」
python _verify_exe_panel.py  :: 归档层: PYZ 必须有 shell_single
```

- 用户报修：后台（托盘）还跑着的时候又点了一次图标，结果**又开了一个进程**。原因是没有
  判重 —— `run_server` 虽然端口占用时会复用服务，但窗口是新建的，于是两套窗口 + 第二套
  WebView2 用户数据目录（第二套还会报目录占用），关掉其中一个还可能把共用的服务一起收掉。
- `shell_single.py`：命名互斥量 `Local\MirrorSorter-<hash>-run` 判重，`...-show` 事件唤醒，
  `...-ack` 回执。scope = **数据目录**（`normcase(abspath(DATA_DIR))`）：同一个存档就是一个
  实例；`MIRRORSORTER_DATA_DIR` 指到隔离目录的自动化测试各自独立，不会被正跑的正式实例挡回去。
- 三个必须记住的点：
  1. `ERROR_ALREADY_EXISTS` 只能通过 `ctypes.get_last_error()` 读 → 必须
     `WinDLL(..., use_last_error=True)`，否则 ctypes 自己会重置错误码、永远读到 0（判重彻底失效）。
  2. `AllowSetForegroundWindow` 在 **user32**，不在 kernel32。写错过一次：整条 `wake()` 抛
     AttributeError，被兜底成"当唯一实例"，反而变回多开 —— 而且静默，不看日志发现不了。
  3. 前台权限不跨进程：新进程必须先把前台权借给旧进程（`ASFW_ANY`），否则旧窗口只在
     任务栏闪一下，用户以为点了没反应。
- 唤醒复用托盘「打开洞鉴虚明」那条路（`ShellApi.raise_window` → `Tray.show_window`），
  所以从图标叫回来和从托盘叫回来行为一致（本来藏着就顺带换一屏新内容）。
- 监听要在 `acquire` 成功那一刻就开（`single.start(_single_raise)`），不能等窗口 —— 冷启动
  几十秒，用户完全可能在这期间再点一次。壳没就绪时只记 `_RAISE_TARGET["pending"]`，装好
  之后补一次；**回执照样立刻发**，让第二个进程安静退出（实测 0.4 秒）。
- 叫不醒（旧进程卡死）时给 MessageBox 指路（托盘右键 / 任务管理器），**绝不再起一个**去抢
  同一个 WebView2 目录 —— 两个进程一起坏比"没反应"更糟。
- 拿不到互斥量一律当唯一实例继续启动：宁可多开一个窗口，也不能"点了图标什么都不发生"。

### 本版专项（v0.4.50dsk 后台常驻：托盘 / 开机自启 / 关窗换一批）

```bat
python app_window.py --e2e-tray   :: 真窗口托盘自测 9 项, 自己退出(约 25 秒)
python test_shell.py              :: 「常驻1~7」+「顶栏结构9」
python _test_tray450.py           :: 运行层 13 项(含真写注册表后原样还原)
python _verify_exe_panel.py       :: 归档层: PYZ 必须有 shell_tray
```

- 用户诉求：每次打开都要十几秒到半分钟才凑够第一屏。做法是让它在后台先把内容抓好 ——
  ① 开机自启（`--tray`，直接缩托盘不弹窗）② 关窗只缩托盘 ③ 关窗/缩托盘时换一批。
  两个设置键 `ui_autostart` / `ui_close_to_tray` **默认都关**，且只在 `shell_info().tray`
  为真的桌面壳里显示（安卓/浏览器打开时这两行不显示，否则点了没反应）。
- **换一批 = `app.feed_rotate(wait)`，绝不清池**。池子清了而新内容还没抓到，下次打开就是
  空白 + 干等，正好是要避免的那件事。缩托盘 `wait=0`（进程活着，构建跑完自己落冷缓存）；
  真退出 `wait=2.5`（窗口没了看不到等待，但留太久会挡住"关掉马上又打开"）。
- 托盘态启动用 `create_window(..., hidden=True)` + 后台 `_tray_warm()` 主动抓一轮：
  **隐藏窗口里的 WebView2 定时器会被系统节流**，只靠页面轮询补池可能半天不动。
- 真退出顺序：托盘图标 `dispose()` → `feed_rotate` → `app.shutdown`。`_ui()` 在
  `form.IsDisposed` 时直接 return，所以"窗口已关还想摘图标"必须绕过它直接执行。
- ★ `--e2e-tray` 的五项断言里有两条是"看不见就出事"的：**启动即隐藏**（开机不该弹窗）、
  **点关闭被拦成隐藏但窗口没被销毁**（真关了 = 用户以为程序还在，实际后台抓取全白费）。

### 本版专项（v0.4.49dsk 悬浮窗全屏黑屏）

```bat
python test_shell.py        :: 「悬浮窗全屏」「全屏轻补」「全屏可见性(行为级)×6」
python _verify_exe_panel.py :: 归档 shell_pages 必须含 _fs_ensure_visible / _same_ctrl
```

- **症状**：在悬浮窗里播视频，点全屏后画面整块黑，**要过好几秒才出画面**。
- **取证**（用户机 `shell_debug.log`，2026-10-25 23:09，两次复现 2/2）：
  ```
  fs enter key=1 float=True  ... vis=True  bounds={0,45,1680,1095}   ← 进去之前是可见的
  fs enter done key=1        ... vis=False bounds={0,0,1680,1140}    ← 几何改完就不可见了
  rep tap key=1 gentle=True  ... vis=False                           ← 两拍轻补都不碰 Visible
  rep tap key=1 gentle=False ... vis=True                            ← 只有别的硬补才救回来
  ```
  **控件被自己藏了**：主窗口那条路（`float=False`）历次都是 `vis=True`，只有悬浮窗这条必黑。
- **根因**：悬浮窗分支为了藏掉迷你标题栏/贴边热区，遍历 `host.Controls` 并 `if s is not c` 跳过
  全屏控件本身。但 `Controls` 里取出的包装对象未必是同一个 Python 对象（`_same_form` 已经为
  这个坑写过注释），于是"跳过自己"落空，**把正在全屏的控件也 `Visible=False` 了**。
  而全屏那两拍兜底是**轻补**——按设计绝不碰 `Visible`（隐藏会把浏览器踢出 HTML 全屏，
  见 `_fs_soft` 的取证），所以黑屏不会自愈；用户点一下窗口触发了别的硬补，画面才回来。
- **修法三层**（缺一层都还会有洞）：
  1. `_same_ctrl(a, b)`：按 HWND 比身份，藏兄弟时用它跳过全屏控件；
  2. `set_fullscreen` 几何改完立刻 `_fs_ensure_visible(c, key, "fs-enter")` —— 兜住"任何原因
     导致的隐藏"，不依赖第 1 层判断对；
  3. `_rep_tap` 轻补分支：**只在"已进入全屏 且 控件确实不可见"时**把它显示出来（可见时零操作，
     不打断合成器；非全屏时绝不翻出隐藏的后台标签）。
- ★ 教训：轻补"不碰 Visible"是对的，但它有个隐含前提——**控件本来就是可见的**。一旦哪个
  分支把它藏了，这条路上就再没有任何人能救回来。所以"救回可见"必须显式写在链路里。

### 本版专项（v0.4.42dsk 右侧 AI 面板）

```bat
:: 真机端到端：开真窗、真 WebView2，20+ 项断言 + 三档真图，跑完自己退出
set MIRRORSORTER_DATA_DIR=%TEMP%\msx-ai-e2e
set MIRRORSORTER_WV2_DATA=%TEMP%\msx-ai-e2e-wv2
set MIRRORSORTER_PORT=18061
python app_window.py --e2e-ai http://127.0.0.1:18061/panel
:: 产物: build/probe/panel_w380.png / panel_light.png / panel_w300.png
:: 打包态跑同一套: 日志和截图落在 exe 同级的 build/probe/ (exe 没有 stdout, 只能看文件)

python _verify_exe_panel.py      :: 归档层: panel.* 字节一致 + shell_ai 真的进了 PYZ
python app_window.py --e2e-split :: 量化: 拖动 30 帧内容区 resize 几次(自带旧做法对照组)
                                   :: 产物 build/probe/split_perf_e2e.log
```

跑真机 e2e 的三个坑：① 别先 `win.minimize()`（ClientSize 归零，几何全测不出来）；
② 端口和两个数据目录都要隔离；③ 面板页面本身也是网页标签，所以 `tab_ok` 断言按"非信息流"写。

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

`test_shell.py` 历史上有 20 条与本轮无关的失败（2026-10-25 记录为 163 PASS / 20 FAIL，
多为早期功能重构后没跟着改的旧断言：背景系统、AI 超分、文本分析设置页文案等）。
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
- **`ExecuteScriptAsync` 返回的 .NET Task 绝不能在 UI 线程上等**（`.Result`/`Wait()` 直接死锁）。
  正确姿势：在 UI 线程只负责"发起"，回到调用线程轮询 `task.IsCompleted` 再取 `task.Result`，
  见 `shell_pages.PagesPool.exec_js`。同理，任何控件操作都要 `form.BeginInvoke` 回 UI 线程。
- **WebView2 的画面只能走 CDP 抓**（`CallDevToolsProtocolMethodAsync("Page.captureScreenshot")`）。
  WebView2 是 DComp 合成，GDI `DrawToBitmap` / `PrintWindow` 抓出来是黑的或空白。
- 多标签共用一个 `UserDataFolder` 和 `AdditionalBrowserArguments`，新开的 WebView2（含 AI 面板）
  必须复制这两项，否则登录态/代理参数不一致。
- 面板/分栏这类"跟着窗口走"的原生控件，几何一律由 `layout()` 统一下发；
  拖动过程中只改控件 `Bounds`，松手才写配置，避免每帧落库 + 重排。
- 面板宽度是"内容区让位"（`pages.set_right_inset`），不是覆盖；全屏时让位必须归零。

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
