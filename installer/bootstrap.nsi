Unicode true
RequestExecutionLevel user
Name "消息萃取在线安装"
OutFile "${BOOT_OUTPUT}"
Icon "${BOOT_ICON}"
!addplugindir /x86-unicode "${BOOT_PLUGINS}"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
XPStyle on
Page instfiles
AutoCloseWindow true
ShowInstDetails show
Section
  InitPluginsDir
  StrCpy $1 "$EXEDIR\${CORE_NAME}"
  IfFileExists "$1" verify
  StrCpy $1 "$PLUGINSDIR\${CORE_NAME}"
  DetailPrint "正在下载消息萃取主程序…"
  inetc::get /CAPTION "下载消息萃取主程序" "${CORE_URL}" "$1" /END
  Pop $0
  StrCmp $0 "OK" verify failed
  verify:
  Push "SHA2-256"
  Push "$1"
  StdUtils::HashFile
  Pop $0
  StrCmp $0 "${CORE_HASH}" verified
  StrCpy $0 "安装数据与此版本的校验值不符"
  failed:
  MessageBox MB_ICONSTOP "安装准备失败：$0。请检查网络或重新下载此版本。" /SD IDOK
  SetErrorLevel 2
  Quit
  verified:
  System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_SETUP_ORIGIN", w "$EXEDIR")'
  ${GetParameters} $2
  HideWindow
  ClearErrors
  ExecWait '"$1" $2' $0
  IfErrors 0 +2
  StrCpy $0 2
  SetErrorLevel $0
SectionEnd
