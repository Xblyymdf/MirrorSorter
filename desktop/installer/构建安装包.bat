@echo off
chcp 936 >nul
cd /d "%~dp0.."

rem ======== 构建安装包 ========
rem 版本号唯一来源: app.py 里的 APP_VER（窗口标题、安装包名、网页页脚全部取它）
rem 本脚本自动: 补资源 -> 选 Python -> 查 PyInstaller -> onedir 打包 -> NSIS 输出
rem
rem 产物:   build\dist\洞鉴虚明\                           (onedir 目录，NSIS 整个塞进去)
rem         build\dist\洞鉴虚明vX.Y.Zdsk_安装包.exe
rem 中间物: build\pybuild\  build\spec\  build\cache\    —— 全在 build\ 下，
rem         删一个目录即可清干净（「清理构建.bat」）
rem
rem 【重要】installer\redist\ 与 installer\tools\ 被 .gitignore 忽略：
rem   体积大（WebView2 离线运行时 200MB+）、会随版本过期，不适合入库。
rem   所以新 clone / 清了目录之后它们必然是空的 —— 旧版脚本遇到就直接 exit，
rem   等于"安装包功能对新人默认不可用"。现在改成自动下载补齐（见 :ensure_*），
rem   仍失败才让人手动放，并给出确切路径。

setlocal EnableExtensions
set "ROOT=%CD%"
set "BUILD=%ROOT%\build"
set "CACHE=%BUILD%\cache"
set "MAKENSIS=%ROOT%\installer\tools\NSIS-3.11\Bin\makensis.exe"
set "WV2=%ROOT%\installer\redist\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"

rem NSIS 便携包 (sourceforge, 约 2.3MB)。注意 URL 里的 %20 在 bat 里要写成 %%20
set "NSIS_URL=https://sourceforge.net/projects/nsis/files/NSIS%%203/3.11/nsis-3.11.zip/download"
rem WebView2 Evergreen Standalone x64 (微软官方 fwlink, 约 200MB)。
rem 用 Standalone 而不是 2MB 的 bootstrapper: 目标机器可能没网，必须离线可装。
set "WV2_URL=https://go.microsoft.com/fwlink/?linkid=2124701"

rem ---- 选 Python ----
rem 优先项目约定的 3.14（依赖里有只发 cp314 wheel 的包），找不到再退回 PATH 上的。
rem 与 ..\构建exe.bat 保持同一口径，否则同一个版本会产出两种行为不同的包。
set "PYCMD="
if exist "C:\Python314\python.exe" set "PYCMD=C:\Python314\python.exe"
if not defined PYCMD ( where python >nul 2>nul && set "PYCMD=python" )
if not defined PYCMD ( where py >nul 2>nul && set "PYCMD=py -3" )
if not defined PYCMD (
  echo [错误] 未检测到 Python，请先安装: https://www.python.org/downloads/
  echo 安装时记得勾选 "Add Python to PATH"
  pause
  exit /b 1
)
echo Python: %PYCMD%

echo [0/4] 检查构建资源（NSIS / WebView2 离线运行时）...
call :ensure_nsis
if errorlevel 1 ( pause & exit /b 1 )
call :ensure_wv2
if errorlevel 1 ( pause & exit /b 1 )

rem ---- 版本号 ----
%PYCMD% -c "import re,io;t=io.open(r'app.py',encoding='utf-8').read();m=re.search(r'APP_VER = .(v[0-9A-Za-z.]+)',t);io.open(r'_ver.tmp','w',encoding='ascii').write(m.group(1) if m else '')"
set /p VER=<_ver.tmp
del _ver.tmp >nul 2>nul
if not defined VER (
  echo [错误] 未能从 app.py 解析 APP_VER, 请确认格式: APP_VER = "vX.Y.Zdsk"
  pause
  exit /b 1
)
echo 版本: %VER%

rem ---- 检查 PyInstaller: 先查不装, 缺了才装（断网时也能构建） ----
%PYCMD% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo [1/4] 未检测到 PyInstaller, 正在安装 ...
  %PYCMD% -m pip install --upgrade pyinstaller
  if errorlevel 1 (
    echo [错误] PyInstaller 安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
) else (
  echo [1/4] PyInstaller 已就绪
)

echo [2/4] PyInstaller onedir 打包（约 1-2 分钟，请勿关闭本窗口）...
if not exist "%BUILD%\dist"     mkdir "%BUILD%\dist"
if not exist "%BUILD%\pybuild"  mkdir "%BUILD%\pybuild"
if not exist "%BUILD%\spec"     mkdir "%BUILD%\spec"
if not exist "%BUILD%\logs"     mkdir "%BUILD%\logs"

rem 先自己清掉上一轮产物, 别指望 PyInstaller 的 --noconfirm:
rem 它用 Python 的批量删除, 受管环境(安全软件/沙箱/组策略)会拦截"一次删上百个文件",
rem 结果是**打包其实成功了, 却在 Removing dir 这步报错退出**, 现象就是"第二次构建必挂"。
rem 用 cmd 自己的 rmdir 做, 失败点也说得清楚。
if exist "%BUILD%\dist\洞鉴虚明" (
  echo       清理上一轮 build\dist\洞鉴虚明 ...
  rmdir /s /q "%BUILD%\dist\洞鉴虚明"
)
if exist "%BUILD%\pybuild\洞鉴虚明" rmdir /s /q "%BUILD%\pybuild\洞鉴虚明"
if exist "%BUILD%\dist\洞鉴虚明" (
  echo [错误] 无法删除 build\dist\洞鉴虚明
  echo        多半是程序还在运行, 或文件被安全软件占用。关掉后再试。
  pause
  exit /b 1
)
rem --onedir: 安装包需要的是**目录**（NSIS 用 File /r 把目录整个塞进去），不是 onefile
rem --upx-exclude: 58MB 的模型不参与 UPX 压缩，否则启动极慢
rem --add-data 一律绝对路径: specpath 变了之后相对路径会按 build\spec 解析
%PYCMD% -m PyInstaller --noconfirm --clean --onedir --noconsole ^
  --distpath "%BUILD%\dist" --workpath "%BUILD%\pybuild" --specpath "%BUILD%\spec" ^
  --add-data "%ROOT%\web;web" --add-data "%ROOT%\extensions;extensions" ^
  --add-data "%ROOT%\WebView2Runtime;WebView2Runtime" --add-data "%ROOT%\model;model" ^
  --hidden-import ftrt --icon "%ROOT%\djxm_icon_128x128.ico" ^
  --upx-exclude ft_model.bin ^
  --name 洞鉴虚明 "%ROOT%\app_window.py"
if errorlevel 1 (
  echo [错误] onedir 打包失败。
  pause
  exit /b 1
)
if not exist "%BUILD%\dist\洞鉴虚明\洞鉴虚明.exe" (
  echo [错误] 未生成 build\dist\洞鉴虚明\洞鉴虚明.exe
  pause
  exit /b 1
)

echo [3/4] NSIS 输出安装包（APPVER=%VER%）...
echo      (压缩约 1-3 分钟, 期间无进度条, 请勿关闭)
cd installer
"%MAKENSIS%" /DAPPVER=%VER% app_installer.nsi
if errorlevel 1 (
  cd ..
  echo [错误] 安装包输出失败。
  pause
  exit /b 1
)
cd ..

if not exist "%BUILD%\dist\洞鉴虚明%VER%_安装包.exe" (
  echo [错误] NSIS 没有报错但找不到安装包，请检查 app_installer.nsi 的 OutFile
  pause
  exit /b 1
)

echo.
echo [4/4] 完成，安装包: build\dist\洞鉴虚明%VER%_安装包.exe
for %%A in ("%BUILD%\dist\洞鉴虚明%VER%_安装包.exe") do echo       体积: %%~zA 字节
pause
exit /b 0


rem ================= 资源自动补齐 =================
rem 下面两个子过程只在文件缺失时跑；已存在则秒过（不联网）。

:ensure_nsis
if exist "%MAKENSIS%" (
  echo [资源] NSIS 编译器已就绪
  exit /b 0
)
echo [资源] 未检测到 NSIS 编译器，正在下载（约 2.3 MB）...
if not exist "%CACHE%" mkdir "%CACHE%"
set "NZIP=%CACHE%\nsis-3.11.zip"
if exist "%NZIP%" goto :unzip_nsis

where curl >nul 2>nul
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try{Invoke-WebRequest -Uri '%NSIS_URL%' -OutFile '%NZIP%' -UseBasicParsing -TimeoutSec 300}catch{exit 1}"
) else (
  curl.exe -L --fail --retry 3 --max-time 300 -o "%NZIP%" "%NSIS_URL%"
)
if errorlevel 1 (
  if exist "%NZIP%" del "%NZIP%" >nul 2>nul
  goto :nsis_fail
)

:unzip_nsis
echo [资源] 解压 NSIS ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $d='%ROOT%\installer\tools'; if(-not (Test-Path $d)){ New-Item -ItemType Directory -Path $d -Force | Out-Null }; if(Test-Path (Join-Path $d 'nsis-3.11')){ Remove-Item (Join-Path $d 'nsis-3.11') -Recurse -Force }; if(Test-Path (Join-Path $d 'NSIS-3.11')){ Remove-Item (Join-Path $d 'NSIS-3.11') -Recurse -Force }; Expand-Archive -LiteralPath '%NZIP%' -DestinationPath $d -Force; Rename-Item (Join-Path $d 'nsis-3.11') 'NSIS-3.11' } catch { exit 1 }"
if not exist "%MAKENSIS%" goto :nsis_fail
echo [资源] NSIS 就绪
exit /b 0

:nsis_fail
echo.
echo [错误] NSIS 自动获取失败（网络受限或解压出错）。
echo        手动办法: 打开 https://nsis.sourceforge.io/Download 下载便携版 zip
echo        解压后让目录长这样: installer\tools\NSIS-3.11\Bin\makensis.exe
echo        (只要 makensis.exe 在 Bin\ 下即可，其余文件照原样放着)
exit /b 1


:ensure_wv2
if exist "%WV2%" (
  echo [资源] WebView2 离线运行时已就绪
  exit /b 0
)
echo [资源] 未检测到 WebView2 离线运行时，正在下载（约 200 MB，视网速 1-10 分钟）...
echo        这是安装包能在"没装过 Edge/WebView2 的机器"上跑起来的必需件，只需下一次
if not exist "%CACHE%" mkdir "%CACHE%"
set "WTMP=%CACHE%\WebView2Runtime-x64.exe"
if exist "%WTMP%" (
  echo [资源] 使用上次下载的缓存，跳过下载
  goto :wv2_check
)

where curl >nul 2>nul
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try{Invoke-WebRequest -Uri '%WV2_URL%' -OutFile '%WTMP%' -UseBasicParsing -TimeoutSec 1800}catch{exit 1}"
) else (
  curl.exe -L --fail --retry 3 --max-time 1800 -o "%WTMP%" "%WV2_URL%"
)
if errorlevel 1 (
  if exist "%WTMP%" del "%WTMP%" >nul 2>nul
  goto :wv2_fail
)

:wv2_check
rem 体积兜底: 拿到一个 HTML 错误页也算"成功"下载，这里挡掉
powershell -NoProfile -ExecutionPolicy Bypass -Command "if((Get-Item '%WTMP%' -ErrorAction SilentlyContinue).Length -lt 52428800){exit 1}"
if errorlevel 1 (
  if exist "%WTMP%" del "%WTMP%" >nul 2>nul
  goto :wv2_fail
)

if not exist "%ROOT%\installer\redist" mkdir "%ROOT%\installer\redist"
move /y "%WTMP%" "%WV2%" >nul
if not exist "%WV2%" goto :wv2_fail
echo [资源] WebView2 离线运行时就绪
exit /b 0

:wv2_fail
echo.
echo [错误] WebView2 离线运行时自动下载失败。
echo        手动办法: 打开 https://developer.microsoft.com/microsoft-edge/webview2/
echo        在 "Evergreen Standalone Installer" 里选 x64 下载，
echo        改名为 MicrosoftEdgeWebView2RuntimeInstallerX64.exe
echo        放到 installer\redist\ 下
echo        应急: 若只给自己机器用，可先装 Microsoft Edge 浏览器，
echo              应用会自动借用它的内核运行。
exit /b 1
