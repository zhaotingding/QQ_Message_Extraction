!ifndef BUILD_UNINSTALLER
  ; 自定义 include 在模板载入前编译，下面的函数需先引入条件与路径宏。
  !include LogicLib.nsh
  !include FileFunc.nsh
  !include nsDialogs.nsh
  !define MSGX_PREPARE_SCRIPT "${__FILEDIR__}\prepare-protocol.ps1"
  !include "${__FILEDIR__}\protocol-component.nsh"

  !define MSGX_RELOCATION_SCRIPT "${__FILEDIR__}\validate-relocation.ps1"
  Var MsgxRegisteredInstallDir
  Var MsgxRelocating

  !macro customInit
    ; 检查实际安装记录，默认沿用原目录。手动安装可在目录页确认迁移。
    StrCpy $MsgxRelocating "0"
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
    StrCpy $MsgxWantProtocol 1
    ${If} $MsgxRegisteredInstallDir != ""
    ${AndIf} ${FileExists} "$MsgxRegisteredInstallDir\resources\protocol-skipped"
      StrCpy $MsgxWantProtocol 0
    ${EndIf}
    ${If} ${Silent}
      ClearErrors
      ${GetOptions} $CMDLINE "/NOQQ" $0
      ${IfNot} ${Errors}
        StrCpy $MsgxWantProtocol 0
      ${EndIf}
      Call MsgxPrepareProtocol
    ${EndIf}
  !macroend

  ; Assisted 模式的安装范围页直接使用当前用户，保持原来的安装身份。
  !macro customInstallmode
    StrCpy $isForceCurrentInstall "1"
  !macroend

  !macro customPageAfterChangeDir
    !insertmacro MsgxProtocolFunctions
    !define MUI_PAGE_CUSTOMFUNCTION_PRE MsgxDirectoryPre
    !define MUI_PAGE_CUSTOMFUNCTION_SHOW MsgxDirectoryShow
    !define MUI_PAGE_CUSTOMFUNCTION_LEAVE MsgxDirectoryLeave
    !insertmacro MUI_PAGE_DIRECTORY
    Page custom MsgxChooseProtocol MsgxReadProtocol
    !define MUI_PAGE_CUSTOMFUNCTION_PRE MsgxPrepareProtocol

    Function MsgxDirectoryShow
      ${If} $MsgxRegisteredInstallDir != ""
        !insertmacro MUI_HEADER_TEXT "选择安装目录" "默认沿用原目录，也可以迁移程序。"
        SendMessage $mui.DirectoryPage.Text ${WM_SETTEXT} 0 "STR:保留当前路径以原位更新，或浏览并选择新的或空的程序目录。个人资料仍保留在原位。"
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
      StrCpy $MsgxRelocating "0"
      ${If} $MsgxRegisteredInstallDir == ""
        Call MsgxValidateNewDirectory
        Return
      ${EndIf}

      ; 把当前输入与注册路径统一成带末尾反斜杠的绝对路径后再判断是否迁移。
      Call MsgxValidateInstallLocation
      System::Call 'kernel32::GetFullPathNameW(w "$MsgxRegisteredInstallDir", i ${NSIS_MAX_STRLEN}, w .r8, p 0) i .r7'
      ${If} $7 == 0
      ${OrIf} $7 >= ${NSIS_MAX_STRLEN}
        MessageBox MB_OK|MB_ICONSTOP "无法识别原安装目录，已停止。请修复现有安装后再试。" /SD IDOK
        Abort
      ${EndIf}
      StrCpy $4 $8 1 -1
      ${If} $4 != "\"
        StrCpy $8 "$8\"
      ${EndIf}
      ${If} $2 == $8
        StrCpy $INSTDIR $MsgxRegisteredInstallDir
        Return
      ${EndIf}

      Call MsgxValidateNewDirectory
      Call MsgxValidateRelocation
      MessageBox MB_OKCANCEL|MB_ICONQUESTION|MB_DEFBUTTON2 "将程序迁移到：$\r$\n$INSTDIR$\r$\n$\r$\n旧卸载器会清理原程序目录，个人资料仍保留在原位。继续安装？" /SD IDCANCEL IDOK +2
      Abort
      StrCpy $MsgxRelocating "1"
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

  !macro customHeader
    Function MsgxValidateRelocation
      InitPluginsDir
      File /oname=$PLUGINSDIR\validate-relocation.ps1 "${MSGX_RELOCATION_SCRIPT}"
      Delete "$PLUGINSDIR\relocation-error.txt"
      ReadRegStr $0 HKCU "${UNINSTALL_REGISTRY_KEY}" UninstallString

      ; 通过当前安装器进程的环境传递原始路径与注册命令，避免命令参数重新解释引号。
      StrCpy $2 "error"
      StrCpy $3 $MsgxRegisteredInstallDir
      StrCpy $4 $INSTDIR
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_OLD_DIRECTORY", w r3) i .r7'
      ${If} $7 == 0
        Goto msgxRelocationClearEnvironment
      ${EndIf}
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_NEW_DIRECTORY", w r4) i .r7'
      ${If} $7 == 0
        Goto msgxRelocationClearEnvironment
      ${EndIf}
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_EXECUTABLE_NAME", w "${APP_EXECUTABLE_FILENAME}") i .r7'
      ${If} $7 == 0
        Goto msgxRelocationClearEnvironment
      ${EndIf}
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_UNINSTALL_COMMAND", w r0) i .r7'
      ${If} $7 == 0
        Goto msgxRelocationClearEnvironment
      ${EndIf}
      nsExec::Exec /TIMEOUT=15000 '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\validate-relocation.ps1" -FromEnvironment'
      Pop $2

      msgxRelocationClearEnvironment:
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_OLD_DIRECTORY", p 0)'
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_NEW_DIRECTORY", p 0)'
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_EXECUTABLE_NAME", p 0)'
      System::Call 'kernel32::SetEnvironmentVariableW(w "MSGX_RELOCATION_UNINSTALL_COMMAND", p 0)'
      ${If} $2 == "0"
        Return
      ${EndIf}

      StrCpy $1 ""
      ClearErrors
      FileOpen $0 "$PLUGINSDIR\relocation-error.txt" r
      ${IfNot} ${Errors}
        FileReadUTF16LE $0 $1
        FileClose $0
      ${EndIf}
      ${If} $1 == ""
        StrCpy $1 "无法完成迁移预检。请保留原程序目录，或重试后再更改目录。"
      ${EndIf}
      MessageBox MB_OK|MB_ICONSTOP "$1" /SD IDOK
      Abort
    FunctionEnd
  !macroend

  !macro customUnInstallCheck
    ${If} $MsgxRelocating == "1"
      ; 迁移时必须确认旧程序已卸载，防止模板在卸载器启动失败后继续解包。
      IfErrors msgxRelocationUninstallFailed
      ${If} $R0 != 0
      ${OrIf} ${FileExists} "$MsgxRegisteredInstallDir\${APP_EXECUTABLE_FILENAME}"
        Goto msgxRelocationUninstallFailed
      ${EndIf}
      Return
      msgxRelocationUninstallFailed:
      MessageBox MB_OK|MB_ICONSTOP "原程序未能完成卸载，迁移已停止。新目录没有安装程序。" /SD IDOK
      SetErrorLevel 2
      Quit
    ${EndIf}

    ; 原位覆盖保留 electron-builder 原来的卸载结果处理。
    IfErrors 0 +3
    DetailPrint `Uninstall was not successful. Not able to launch uninstaller!`
    Return
    ${If} $R0 != 0
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0"
      DetailPrint `Uninstall was not successful. Uninstaller error code: $R0.`
      SetErrorLevel 2
      Quit
    ${EndIf}
  !macroend

  !macro customInstall
    ${If} $MsgxWantProtocol == 1
      ClearErrors
      Rename "$MsgxProtocolStage" "$INSTDIR\NapCat"
      ${If} ${Errors}
        MessageBox MB_ICONSTOP "无法将 QQ 组件写入所选安装目录，请检查文件占用后重试。" /SD IDOK
        SetErrorLevel 2
        Quit
      ${EndIf}
      StrCpy $MsgxProtocolStage ""
    ${Else}
      FileOpen $0 "$INSTDIR\resources\protocol-skipped" w
      FileClose $0
    ${EndIf}
    ${IfNot} ${Silent}
    ${AndIfNot} ${isUpdated}
      ; 首次启动可以用安装包的来源定位同仓库旧资料，安装阶段不访问资料。
      ClearErrors
      FileOpen $0 "$INSTDIR\resources\installer-origin.txt" w
      ${IfNot} ${Errors}
        ReadEnvStr $1 MSGX_SETUP_ORIGIN
        ${If} $1 == ""
          StrCpy $1 "$EXEDIR"
        ${EndIf}
        FileWriteUTF16LE $0 "$1"
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
  Function .onGUIEnd
    Call MsgxCleanupStage
  FunctionEnd
!endif
