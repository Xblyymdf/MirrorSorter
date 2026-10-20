@echo off
chcp 936 >nul
cd /d "%~dp0"

rem ════ 产物统一目录 ════
rem dist(成品 exe) / pybuild(PyInstaller 中间物) / spec(每次构建生成的 spec) / logs(构建日志)
rem 全部落在 build\ 下, 要清理直接删 build\ 或用 清理构建.bat
set "ROOT=%CD%"
set "BUILD=%ROOT%\build"

rem ─── 选 Python ───
rem 优先项目约定的 3.14（依赖里有只发 cp314 wheel 的包），找不到再退回 PATH 上的。
rem 与 installer\构建安装包.bat 保持同一口径，否则同一个版本会产出两种行为不同的包。
set "PYCMD="
if exist "C:/Python314/python.exe" set "PYCMD=C:/Python314/python.exe"
if not defined PYCMD ( where python >nul 2>nul && set "PYCMD=python" )
if not defined PYCMD ( where py >nul 2>nul && set "PYCMD=py -3" )
if not defined PYCMD (
  echo [错误] 未检测到 Python，请先安装: https://www.python.org/downloads/
  echo 安装时记得勾选 "Add Python to PATH"
  pause
  exit /b 1
)
echo Python: %PYCMD%

rem ─── 检查 PyInstaller: 先查不装, 缺了才装（断网时也能构建） ───
%PYCMD% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo [1/2] 未检测到 PyInstaller, 正在安装 ...
  %PYCMD% -m pip install --upgrade pyinstaller
  if errorlevel 1 (
    echo [错误] PyInstaller 安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
) else (
  echo [1/2] PyInstaller 已就绪
)

echo [2/2] 开始构建（约 1-2 分钟，请勿关闭本窗口）...
rem 从 app.py 的 APP_VER 读取版本号(如 "v0.3.1.47dsk"), 拼成 "洞鉴虚明v0.3.1.47dsk" 作为 exe 名
if not exist "%BUILD%\dist"     mkdir "%BUILD%\dist"
if not exist "%BUILD%\pybuild"  mkdir "%BUILD%\pybuild"
if not exist "%BUILD%\spec"     mkdir "%BUILD%\spec"
if not exist "%BUILD%\logs"     mkdir "%BUILD%\logs"
%PYCMD% -c "import re,io;t=io.open(r'app.py',encoding='utf-8').read();m=re.search(r'APP_VER = .(v[0-9A-Za-z.]+)',t);io.open(r'build\_appname.txt','w',encoding='gbk').write(('洞鉴虚明'+m.group(1)) if m else '')"
set /p "APPNAME=" < build\_appname.txt
del build\_appname.txt >nul 2>nul
if not defined APPNAME set "APPNAME=洞鉴虚明"
echo 版本号: %APPNAME%

rem --distpath/--workpath/--specpath: 三处产物都指到 build\, 桌面端根目录保持干净
rem --add-data 一律用绝对路径: specpath 变了之后, 相对路径会被解析成 build\spec\web 而找不到
%PYCMD% -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --distpath "%BUILD%\dist" --workpath "%BUILD%\pybuild" --specpath "%BUILD%\spec" ^
  --add-data "%ROOT%\web;web" --add-data "%ROOT%\extensions;extensions" ^
  --add-data "%ROOT%\WebView2Runtime;WebView2Runtime" --add-data "%ROOT%\model;model" ^
  --hidden-import ftrt --icon "%ROOT%\djxm_icon_128x128.ico" --upx-exclude ft_model.bin ^
  --name "%APPNAME%" "%ROOT%\app_window.py"
if errorlevel 1 (
  echo.
  echo [错误] 构建失败，请检查上面的报错信息或截图反馈。
  pause
  exit /b 1
)

echo.
echo 完成，双击运行: build\dist\%APPNAME%.exe
pause
