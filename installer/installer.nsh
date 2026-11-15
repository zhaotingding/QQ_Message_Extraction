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
    !define MUI_PAGE_CUSTOMFUNCTION_LEAVE MsgxValidateNewDirectory
    !insertmacro MUI_PAGE_DIRECTORY
  !macroend

  Function MsgxDirectoryPre
    ; 模板的安装范围步骤可能重读 /D，因此在交互升级跳过页面前再次锁定。
    ${If} $MsgxRegisteredInstallDir != ""
      StrCpy $INSTDIR $MsgxRegisteredInstallDir
      Abort
    ${EndIf}
  FunctionEnd

  !macro MsgxCheckProfilePath PROFILE
    GetFullPathName $3 "${PROFILE}"
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
    GetFullPathName $2 "$INSTDIR"
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
