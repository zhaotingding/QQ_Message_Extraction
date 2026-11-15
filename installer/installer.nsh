!ifndef BUILD_UNINSTALLER
  ; 自定义 include 在模板载入前编译，下面的函数需先引入条件与路径宏。
  !include LogicLib.nsh
  !include FileFunc.nsh

  Var MsgxRegisteredInstallDir

  !macro customInit
    ; 检查实际安装记录，已有安装只允许在原目录升级或覆盖安装。
    ReadRegStr $0 HKLM "${INSTALL_REGISTRY_KEY}" InstallLocation
    ReadRegStr $1 HKLM "${UNINSTALL_REGISTRY_KEY}" UninstallString
    ${If} $0 != ""
    ${OrIf} $1 != ""
      MessageBox MB_OK|MB_ICONSTOP "已找到此应用的全用户安装。本安装包仅支持当前用户，已停止安装。" /SD IDOK
      SetErrorLevel 1
      Quit
    ${EndIf}
    ${If} ${isForAllUsers}
      MessageBox MB_OK|MB_ICONSTOP "此安装包仅支持为当前用户安装。" /SD IDOK
      SetErrorLevel 1
      Quit
    ${EndIf}
    ReadRegStr $MsgxRegisteredInstallDir HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
    ReadRegStr $1 HKCU "${UNINSTALL_REGISTRY_KEY}" UninstallString
    ${If} $MsgxRegisteredInstallDir == ""
    ${AndIf} $1 != ""
      MessageBox MB_OK|MB_ICONSTOP "已有安装记录缺少程序目录。请修复现有安装后再试。" /SD IDOK
      SetErrorLevel 1
      Quit
    ${EndIf}
    ${If} $MsgxRegisteredInstallDir == ""
    ${AndIf} ${isUpdated}
      MessageBox MB_OK|MB_ICONSTOP "找不到原程序目录，更新已停止。请运行完整安装包。" /SD IDOK
      SetErrorLevel 1
      Quit
    ${EndIf}
    ; 静默安装不会进入目录页，也必须让注册路径优先于 /D。
    ${If} $MsgxRegisteredInstallDir != ""
      StrCpy $INSTDIR $MsgxRegisteredInstallDir
      Call MsgxValidateInstallLocation
    ${ElseIf} ${Silent}
      Call MsgxValidateNewDirectory
    ${EndIf}
  !macroend

  ; Assisted 模式的安装范围页直接使用当前用户，保持原来的安装身份。
  !macro customInstallmode
    StrCpy $isForceCurrentInstall "1"
  !macroend

  !macro customPageAfterChangeDir
    !define MUI_PAGE_CUSTOMFUNCTION_PRE MsgxDirectoryPre
    !define MUI_PAGE_CUSTOMFUNCTION_SHOW MsgxDirectoryShow
    !define MUI_PAGE_CUSTOMFUNCTION_LEAVE MsgxDirectoryLeave
    !insertmacro MUI_PAGE_DIRECTORY

    Function MsgxDirectoryShow
      ${If} $MsgxRegisteredInstallDir != ""
        !insertmacro MUI_HEADER_TEXT "确认安装目录" "已有安装将在原目录覆盖。"
        SendMessage $mui.DirectoryPage.Text ${WM_SETTEXT} 0 "STR:确认下方目录并继续安装。程序将原位更新，个人资料保持原位。"
        SendMessage $mui.DirectoryPage.Directory ${EM_SETREADONLY} 1 0
        EnableWindow $mui.DirectoryPage.BrowseButton 0
      ${EndIf}
    FunctionEnd

    Function MsgxDirectoryPre
      ; 手动安装显示原目录供确认，应用内更新直接沿用原目录。
      ${If} $MsgxRegisteredInstallDir != ""
        StrCpy $INSTDIR $MsgxRegisteredInstallDir
      ${EndIf}
      ${If} ${isUpdated}
        Abort
      ${EndIf}
    FunctionEnd

    Function MsgxDirectoryLeave
      ${If} $MsgxRegisteredInstallDir != ""
        StrCpy $INSTDIR $MsgxRegisteredInstallDir
        Call MsgxValidateInstallLocation
      ${Else}
        Call MsgxValidateNewDirectory
      ${EndIf}
    FunctionEnd
  !macroend

  !macro customFinishPage
    !ifndef HIDE_RUN_AFTER_FINISH
      !define MUI_FINISHPAGE_RUN
      !define MUI_FINISHPAGE_RUN_TEXT "打开消息萃取并进行基础设置"
      !define MUI_FINISHPAGE_RUN_FUNCTION "MsgxLaunchFromFinishPage"
      !define MUI_PAGE_CUSTOMFUNCTION_SHOW MsgxFinishPageShow
    !endif
    !insertmacro MUI_PAGE_FINISH

    !ifndef HIDE_RUN_AFTER_FINISH
      Function MsgxFinishPageShow
        ${If} ${isUpdated}
        ${AndIf} $mui.FinishPage.Run != 0
          SendMessage $mui.FinishPage.Run ${WM_SETTEXT} 0 "STR:打开消息萃取"
        ${EndIf}
      FunctionEnd

      Function MsgxLaunchFromFinishPage
        ${If} ${isUpdated}
          StrCpy $1 "--updated"
        ${Else}
          StrCpy $1 "--msgx-open-setup"
        ${EndIf}
        ${StdUtils.ExecShellAsUser} $0 "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "open" "$1"
      FunctionEnd
    !endif
  !macroend

  !macro MsgxCheckProfilePath PROFILE
    System::Call 'kernel32::GetFullPathNameW(w "${PROFILE}", i ${NSIS_MAX_STRLEN}, w .r3, p 0) i .r7'
    ${If} $7 == 0
    ${OrIf} $7 >= ${NSIS_MAX_STRLEN}
      Goto msgxInvalidLocation
    ${EndIf}
    StrCpy $3 "$3\"
    StrLen $4 $3
    StrCpy $5 $2 $4
    StrLen $4 $2
    StrCpy $6 $3 $4
    ${If} $5 == $3
    ${OrIf} $6 == $2
      Goto msgxProtectedLocation
    ${EndIf}
  !macroend

  Function MsgxValidateInstallLocation
    ; 安装与卸载处理整棵程序目录，不能选盘根或覆盖默认个人资料目录。
    ; 使用 Win32 规范化路径，允许用户输入尚未创建的文件夹。
    System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r2, p 0) i .r7'
    ${If} $7 == 0
    ${OrIf} $7 >= ${NSIS_MAX_STRLEN}
      Goto msgxInvalidLocation
    ${EndIf}
    ${GetRoot} "$2" $3
    ${If} "$2" == "$3"
    ${OrIf} "$2" == "$3\"
      Goto msgxProtectedLocation
    ${EndIf}
    StrCpy $4 $2 1 -1
    ${If} $4 != "\"
      StrCpy $2 "$2\"
    ${EndIf}
    !insertmacro MsgxCheckProfilePath "$LocalAppData\msgx"
    !insertmacro MsgxCheckProfilePath "$LocalAppData\msgx-update-test"
    Return

    msgxProtectedLocation:
      MessageBox MB_OK|MB_ICONSTOP "安装目录不能是盘根，也不能与应用的默认资料目录重叠。" /SD IDOK
      Goto msgxStopLocation
    msgxInvalidLocation:
      MessageBox MB_OK|MB_ICONSTOP "无法识别安装目录，请选择有效的程序文件夹。" /SD IDOK
    msgxStopLocation:
      ${If} ${Silent}
        SetErrorLevel 1
        Quit
      ${EndIf}
      Abort
  FunctionEnd

  Function MsgxValidateNewDirectory
    Call MsgxValidateInstallLocation
    ; 新装只接受新目录或已有空目录，避免卸载时误删选中目录的其他文件。
    ClearErrors
    FindFirst $0 $1 "$INSTDIR\*"
    ${If} ${Errors}
      ${If} ${FileExists} "$INSTDIR"
        Goto msgxInvalidDirectory
      ${EndIf}
    ${Else}
      ${DoWhile} $1 != ""
        ${If} $1 != "."
        ${AndIf} $1 != ".."
          FindClose $0
          Goto msgxInvalidDirectory
        ${EndIf}
        FindNext $0 $1
        ${If} ${Errors}
          ${Break}
        ${EndIf}
      ${Loop}
      FindClose $0
    ${EndIf}
    ClearErrors
    Return

    msgxInvalidDirectory:
      MessageBox MB_OK|MB_ICONSTOP "请选择一个新的或空的程序文件夹，避免覆盖已有文件。" /SD IDOK
      ${If} ${Silent}
        SetErrorLevel 1
        Quit
      ${EndIf}
      Abort
  FunctionEnd

  !macro customInstall
    ${IfNot} ${Silent}
    ${AndIfNot} ${isUpdated}
      ; 首次启动可以用安装包的来源定位同仓库旧资料，安装阶段不访问资料。
      ClearErrors
      FileOpen $0 "$INSTDIR\resources\installer-origin.txt" w
      ${IfNot} ${Errors}
        FileWriteUTF16LE $0 "$EXEDIR"
        FileClose $0
      ${EndIf}
      ClearErrors
    ${EndIf}
    ${If} ${isUpdated}
    ${AndIf} ${Silent}
    ${AndIfNot} ${isForceRun}
      ; 交互安装由完成页重开，带 --force-run 的静默安装由模板重开。
      SetOutPath "$INSTDIR"
      Exec '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --updated'
    ${EndIf}
  !macroend
!endif
