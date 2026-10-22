# AGENTS.md · 洞鉴虚明 / MirrorSorter

本文件是给所有 AI 协作会话的入口硬规矩；完整规范见 [docs/STANDARD.md](docs/STANDARD.md)。
动手改代码前，先读 STANDARD 的第 3、4、5、6 节。

## 每次代码改动都必须做

1. **版本号 +0.0.1**：任何会进入 exe 的修改（修 bug、加功能、改文案）都要升桌面版本。
   三处必须同步，缺一处会出现「界面显示旧版 / 更新器比较错」：
   - `desktop/app.py` 第 3 行头注释：`洞鉴虚明 · 基础版 (vX.Y.Zdsk)`
   - `desktop/app.py` 的 `APP_VER = "vX.Y.Zdsk"`
   - `desktop/web/index.html` 的两处页脚：`洞鉴虚明 vX.Y.Zdsk`
2. **改完必须构建 exe**：在 `desktop` 下运行 `构建exe.bat`，产物
   `desktop/build/dist/洞鉴虚明vX.Y.Zdsk.exe`（约 78MB）。
   构建脚本从 `APP_VER` 取版本名；源码改了不重新打包 = 用户拿到的还是旧 exe。
3. **构建后反解校验**：用对应版本的 `desktop/_verify_exe_*.py <exe>` 确认包内的
   `web/`、`app.py` 常量与注入脚本都是本次改动后的内容。

## 改代码的规矩

4. **CRLF 精确替换**：源文件是 CRLF。用 `io.open(path, encoding="utf-8", newline="")`
   读 → `str.replace` → 断言命中 1 次 → 原样写回 → 回读校验。
   改完跑 `python -m py_compile ...` 与 `node --check web/app.js`。
5. **测试隔离**：任何会写库的测试都设 `MIRRORSORTER_DATA_DIR=%TEMP%\...`，不碰用户真库。
6. **不抢用户屏幕**：验证走无头/静态/反解；不截屏、不夺焦点、不开可见窗口。
7. **不靠日志下结论**：`shell_debug.log` 有收尾期噪声；结论要来自源码、包内反解或可复现实验。
8. **不做多余的改动**：用户对回归极敏感；能不改的界面别改，能解耦就别加东西。

## 发布

9. `git push` 与 `发布更新.bat`（创建 GitHub Release）**只在用户明确要求时执行**；
   发布前确认 tag 与 `APP_VER` 一致、exe 是本次改动之后构建的。
