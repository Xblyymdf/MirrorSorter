@echo off
setlocal EnableExtensions
chcp 936 >nul
cd /d "%~dp0"

set "REPO=Xblyymdf/MirrorSorter"
set "MODE=%~1"
set "TAG="

for /f "tokens=2 delims== " %%v in ('findstr /b /c:"APP_VER = " "%~dp0app.py"') do set "TAG=%%v"
set "TAG=%TAG:"=%"
if not defined TAG (
  echo [错误] 没能从 app.py 读出 APP_VER。
  goto :fail
)

set "EXE=%~dp0build\dist\洞鉴虚明%TAG%.exe"

echo ============================================================
echo   洞鉴虚明 · 发布更新
echo   版本    : %TAG%
echo   仓库    : %REPO%
echo   独立 exe: %EXE%
echo ============================================================
echo.

set "PROBLEM="
where git >nul 2>nul || set "PROBLEM=%PROBLEM% [git 未安装]"
where gh  >nul 2>nul || set "PROBLEM=%PROBLEM% [gh 未安装]"
git rev-parse --git-dir >nul 2>nul || set "PROBLEM=%PROBLEM% [不在 git 仓库里]"
if not exist "%EXE%" set "PROBLEM=%PROBLEM% [exe 不存在，请先运行 构建exe.bat]"

if defined PROBLEM goto :report

gh auth status >nul 2>nul
if errorlevel 1 set "PROBLEM=%PROBLEM% [gh 未登录]"

findstr /c:"%TAG%" "%~dp0web\index.html" >nul 2>nul
if errorlevel 1 set "PROBLEM=%PROBLEM% [web/index.html 页脚版本未同步]"

gh release view "%TAG%" --repo "%REPO%" >nul 2>nul
if not errorlevel 1 set "PROBLEM=%PROBLEM% [Release %TAG% 已存在]"

set "DIRTY="
for /f "usebackq delims=" %%s in (`git status --porcelain 2^>nul`) do set "DIRTY=1"
if defined DIRTY if /i not "%MODE%"=="check" set "PROBLEM=%PROBLEM% [工作区有未提交改动]"

:report
if defined PROBLEM (
  echo [自检未通过]
  echo   %PROBLEM%
  echo.
  echo 请先处理以上问题。版本号要同步 app.py 头注释、APP_VER、web/index.html 两处页脚。
  goto :fail
)
echo [自检通过] git / gh / exe / 版本号 / tag 冲突 都正常。
echo.
if defined DIRTY echo [提示] 工作区有未提交改动；发布前请先提交，发布脚本会再检查一次。
echo.

if /i "%MODE%"=="check" goto :done

echo 接下来会做两件事：
echo   1. git push origin HEAD  （把当前分支推到 GitHub）
echo   2. gh release create %TAG% ... --target ^<当前提交^>
echo      只上传上面那个独立 exe，不会上传安装包，也不会把 exe 写进 git。
echo.
set "OK="
set /p "OK=确认发布请输入 YES（其他任意内容取消）: "
if /i not "%OK%"=="YES" goto :cancel

set "SHA="
for /f "usebackq delims=" %%c in (`git rev-parse HEAD`) do set "SHA=%%c"
if not defined SHA (
  echo [错误] 取不到当前提交。
  goto :fail
)

echo.
echo [1/2] 推送当前分支…
git push origin HEAD
if errorlevel 1 (
  echo [错误] 推送失败。请检查网络/权限后重试。
  goto :fail
)

echo.
echo [2/2] 创建 Release %TAG%…
gh release create "%TAG%" "%EXE%" --repo "%REPO%" --target "%SHA%" --title "洞鉴虚明 %TAG%" --generate-notes --latest
if errorlevel 1 (
  echo [错误] Release 创建失败。源码已推送，可修复后重新执行本脚本（tag 若已建好需先处理）。
  goto :fail
)

echo.
echo [完成] 已发布 %TAG%。
echo 请打开 https://github.com/%REPO%/releases 确认：
echo   - tag 是 %TAG%
echo   - 附件只有独立 exe（没有安装包）
echo   - 不是 prerelease
echo 然后用旧版点一次「检测更新」做真实升级验证。
goto :done

:fail
echo.
echo [未继续] 请处理上面的问题后重试。
pause
exit /b 1

:cancel
echo.
echo 已取消，没有推送也没有创建 Release。
pause
exit /b 0

:done
echo.
pause
exit /b 0
