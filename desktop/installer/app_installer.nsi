; 洞鉴虚明 桌面端安装包 — NSIS 3.11 Unicode
; 产物: dist\洞鉴虚明vX.Y.Zdsk_安装包.exe (应用 onedir + WebView2 离线运行时)
; 前置: ① PyInstaller 已按 洞鉴虚明*-installer.spec 产出 build/dist/洞鉴虚明/
;       ② redist/MicrosoftEdgeWebView2RuntimeInstallerX64.exe (微软官方离线包, fwlink 2124701)
Unicode true
ManifestDPIAware true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define APPNAME "洞鉴虚明"
!ifndef APPVER
!define APPVER "v0.3.0.0dsk"   ; 可被 makensis /DAPPVER=vX.Y.Zdsk 覆盖(构建安装包.bat 自动传入)
!endif
!define UNKEY "洞鉴虚明"
!define WV2 "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"   ; WebView2 Evergreen 运行时

Name "${APPNAME} ${APPVER}"
OutFile "..\build\dist\${APPNAME}${APPVER}_安装包.exe"   ; 产物统一进 build/dist/
; 注意: 注释不要以反斜杠结尾 —— NSIS 会把注释末尾的 \ 当续行符, 把下一行的
;       InstallDir 整条吃掉, 安装目录会掉回默认值。
InstallDir "$PROGRAMFILES64\${APPNAME}"
InstallDirRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "InstallLocation"
RequestExecutionLevel admin
; zlib 快速压缩: 载荷大头(WebView2 离线包/DLL)本身已是高压缩内容,
; 实测 /SOLID lzma 只省 9% 却要多压 5~10 分钟且无进度输出(像卡死)
SetCompressor zlib

Icon "..\djxm_icon_128x128.ico"
!define MUI_ICON "..\djxm_icon_128x128.ico"
!define MUI_UNICON "..\djxm_icon_128x128.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPNAME}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APPNAME}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "安装"
  ; --- 更新保护: 旧实例优雅关闭; 文件仍被占用则提示关闭后重试(更新失败的头号原因) ---
  nsExec::Exec 'taskkill /IM "${APPNAME}.exe"'
  Sleep 1000
check_lock:
  ${If} ${FileExists} "$INSTDIR\${APPNAME}.exe"
    ClearErrors
    FileOpen $9 "$INSTDIR\${APPNAME}.exe" a
    ${If} ${Errors}
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION \
        "检测到 ${APPNAME} 正在运行，无法更新。$\r$\n请先退出应用（关闭主窗口），然后点「重试」。" \
        IDRETRY check_lock
      Abort "用户取消更新。"
    ${Else}
      FileClose $9
    ${EndIf}
  ${EndIf}

  SetOutPath "$INSTDIR"
  File /r "..\build\dist\${APPNAME}\*.*"
  SetOutPath "$INSTDIR\redist"
  File "redist\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
  SetOutPath "$INSTDIR"

  ; --- WebView2 运行时: 三个标准注册表位都无有效 pv → 用离线包静默安装(本安装器已提权) ---
  ClearErrors
  ReadRegStr $0 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WV2}" "pv"
  ReadRegStr $1 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WV2}" "pv"
  ReadRegStr $2 HKCU "Software\Microsoft\EdgeUpdate\Clients\${WV2}" "pv"
  StrCpy $3 ""
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $3 $0
  ${ElseIf} $1 != ""
  ${AndIf} $1 != "0.0.0.0"
    StrCpy $3 $1
  ${ElseIf} $2 != ""
  ${AndIf} $2 != "0.0.0.0"
    StrCpy $3 $2
  ${EndIf}
  ${If} $3 == ""
    ; --- 修复常见安装故障(实测: 装到20%即败, 多为 EdgeUpdate 状态损坏/服务被禁/文件锁) ---
    DetailPrint "修复 WebView2 安装环境..."
    nsExec::Exec 'taskkill /IM msedgewebview2.exe /F'          ; 宿主进程文件锁
    nsExec::Exec 'sc config edgeupdate start= demand'          ; 被禁用的更新服务恢复为手动
    nsExec::Exec 'sc config edgeupdatem start= demand'
    nsExec::Exec 'sc config MicrosoftEdgeElevationService start= demand'
    ReadRegStr $5 HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "ProgramData"
    ${If} $5 == ""
      StrCpy $5 "C:\ProgramData"
    ${EndIf}
    nsExec::Exec 'cmd /c rd /s /q "$5\Microsoft\EdgeUpdate"'   ; 损坏状态(安装器自动重建)
    DeleteRegKey HKLM "SOFTWARE\Microsoft\EdgeUpdate"
    DeleteRegKey HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate"
    ; C 盘空间预警: 运行时装在系统盘, 空间不足时任何安装方式都必败
    ${DriveSpace} "C:\" "/D=F /S=M" $4
    IntOp $4 $4 + 0   ; 查询失败时归0(不误报)
    ${If} $4 > 0
    ${AndIf} $4 < 1500
      MessageBox MB_ICONEXCLAMATION "系统盘(C:)剩余约 $4 MB(不足 1.5GB)。WebView2 运行时大概率无法安装，建议先清理 C 盘；本次仍会尝试。"
    ${EndIf}
    DetailPrint "安装 WebView2 运行时(离线包，约1分钟)..."
    ExecWait '"$INSTDIR\redist\MicrosoftEdgeWebView2RuntimeInstallerX64.exe" /silent /install' $1
    ${If} $1 != 0
      ; 实测故障: 精简系统 %TEMP% 缺失/不可写 → 微软安装器报"无法创建临时目录"(0x80040902)。
      ; 把 TEMP/TMP 重定向到安装目录内的临时目录(本安装器已提权, 必然可写)后重试。
      DetailPrint "静默安装未成功(错误码 $1)，重定向临时目录后重试..."
      CreateDirectory "$INSTDIR\redist\temp"
      System::Call 'kernel32::SetEnvironmentVariable(t "TEMP", t "$INSTDIR\redist\temp")'
      System::Call 'kernel32::SetEnvironmentVariable(t "TMP", t "$INSTDIR\redist\temp")'
      ExecWait '"$INSTDIR\redist\MicrosoftEdgeWebView2RuntimeInstallerX64.exe" /silent /install' $1
    ${EndIf}
    ${If} $1 != 0
      ; 仍失败 → 交互式安装(弹微软官方界面, 用户能看到真实原因)
      DetailPrint "仍失败(错误码 $1)，尝试交互式安装(请按微软安装器提示操作)..."
      ExecWait '"$INSTDIR\redist\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"' $1
    ${EndIf}
    ${If} $1 == 0
      DetailPrint "WebView2 运行时安装完成。"
    ${Else}
      MessageBox MB_ICONEXCLAMATION "WebView2 运行时安装未成功(错误码 $1)。$\r$\n常见原因: 系统盘(C:)空间不足、Edge 更新服务缺失或被禁、安全软件拦截。$\r$\n替代方案: 安装 Microsoft Edge 浏览器后重开应用(会自动借用其内核，无需装运行时)；应用仍可先以外置浏览器模式运行。"
    ${EndIf}
    RMDir /r "$INSTDIR\redist\temp"
  ${Else}
    DetailPrint "WebView2 运行时已存在($3)，跳过。"
  ${EndIf}

  ; --- 快捷方式 + 卸载器 + 添加/删除程序登记 ---
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${APPNAME}.exe"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${APPNAME}.exe"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\卸载${APPNAME}.lnk" "$INSTDIR\Uninstall.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "DisplayVersion" "${APPVER}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "Publisher" "CORN STARCH"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "DisplayIcon" "$INSTDIR\${APPNAME}.exe"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "EstimatedSize" 0x00030d40
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "NoModify" 1
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  ; 用户数据(LocalLow\MirrorSorter)保留不动 —— 卸载不丢设置/历史/收藏
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNKEY}"
SectionEnd
