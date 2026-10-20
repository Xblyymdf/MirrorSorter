@echo off
chcp 936 >nul
cd /d "%~dp0"

rem 一键清理构建产物: build\ 整个删掉（含 dist\pybuild\spec\logs\probe）
rem 只删构建垃圾, 不碰源码/模型/网页资源。
if not exist "build" (
  echo 没有 build 目录，无需清理。
  pause
  exit /b 0
)

echo 即将删除以下构建产物（不进回收站，删完需要重新构建）:
echo   %CD%\build
echo.
echo 包含:
dir /b build
echo.
choice /c YN /m "确认删除?"
if errorlevel 2 (
  echo 已取消。
  pause
  exit /b 0
)

rmdir /s /q build
if exist build (
  echo [警告] 未完全删除，可能有 exe 正在运行或文件被占用。请关掉程序后重试。
) else (
  echo 已清理完成。
)
pause
