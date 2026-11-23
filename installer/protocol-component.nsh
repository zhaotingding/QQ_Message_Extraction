; 在卸载旧程序前完成组件下载、摘要校验和同卷解压。
Var MsgxWantProtocol
Var MsgxProtocolBox
Var MsgxProtocolStage
!macro MsgxRuntimeFile NAME
  File /oname=$MsgxProtocolStage\${NAME} "${PROTOCOL_SUPPORT}\${NAME}"
!macroend
!macro MsgxProtocolFunctions
Function MsgxChooseProtocol
  ${If} ${isUpdated}
    Abort
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "选择安装组件" "只下载本次需要的内容。"
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateCheckbox} 0 8u 100% 16u "安装 QQ 消息接收组件（NapCat）"
  Pop $MsgxProtocolBox
  ${NSD_SetState} $MsgxProtocolBox $MsgxWantProtocol
  ${NSD_CreateLabel} 0 33u 100% 32u "接收 QQ 群消息时需要此组件。仅查看本地消息或使用外部 NapCat 时可以取消。"
  Pop $0
  nsDialogs::Show
FunctionEnd

Function MsgxReadProtocol
  ${NSD_GetState} $MsgxProtocolBox $MsgxWantProtocol
FunctionEnd

Function MsgxCleanupStage
  StrCmp $MsgxProtocolStage "" done
  RMDir /r "$MsgxProtocolStage"
  StrCpy $MsgxProtocolStage ""
  done:
FunctionEnd

Function MsgxPrepareProtocol
  Call MsgxCleanupStage
  StrCmp $MsgxWantProtocol 1 0 done
  InitPluginsDir
  StrCpy $R4 "$LOCALAPPDATA\${APP_FILENAME}-components\${PROTOCOL_HASH}"
  StrCpy $R1 "$R4\${PROTOCOL_NAME}"
  IfFileExists "$R1" 0 adjacent
  Push "SHA2-256"
  Push "$R1"
  StdUtils::HashFile
  Pop $0
  StrCmp $0 "${PROTOCOL_HASH}" stage
  adjacent:
  ReadEnvStr $R2 MSGX_SETUP_ORIGIN
  StrCpy $R1 "$EXEDIR\${PROTOCOL_NAME}"
  IfFileExists "$R1" stage
  StrCmp $R2 "" network
  StrCpy $R1 "$R2\${PROTOCOL_NAME}"
  IfFileExists "$R1" stage
  network:
  StrCpy $R1 "$PLUGINSDIR\${PROTOCOL_NAME}"
  inetc::get /CAPTION "下载 QQ 消息接收组件" "${PROTOCOL_URL}" "$R1" /END
  Pop $0
  StrCmp $0 "OK" stage failed
  stage:
  ${GetParent} "$INSTDIR" $R2
  ClearErrors
  CreateDirectory "$R2"
  GetTempFileName $R3 "$R2"
  IfErrors failed
  Delete "$R3"
  CreateDirectory "$R3"
  IfErrors failed
  StrCpy $MsgxProtocolStage $R3
  File /oname=$PLUGINSDIR\prepare-protocol.ps1 "${MSGX_PREPARE_SCRIPT}"
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\prepare-protocol.ps1" "$R1" "${PROTOCOL_HASH}" "$MsgxProtocolStage"'
  Pop $0
  StrCmp $0 "0" 0 failed
  File /oname=$MsgxProtocolStage\NapCat-LICENSE.txt "${PROTOCOL_SUPPORT}\NapCat-LICENSE.txt"
  File /oname=$MsgxProtocolStage\Node-LICENSE.txt "${PROTOCOL_SUPPORT}\Node-LICENSE.txt"
  File /oname=$MsgxProtocolStage\NOTICE.txt "${PROTOCOL_SUPPORT}\NOTICE.txt"
  !insertmacro MsgxRuntimeFile msvcp140.dll
  !insertmacro MsgxRuntimeFile MSVCP140_1.dll
  !insertmacro MsgxRuntimeFile vcruntime140.dll
  !insertmacro MsgxRuntimeFile vcruntime140_1.dll
  StrCmp $R1 "$R4\${PROTOCOL_NAME}" done
  CreateDirectory "$R4"
  CopyFiles /SILENT "$R1" "$R4"
  Goto done
  failed:
  Call MsgxCleanupStage
  MessageBox MB_ICONSTOP "QQ 组件准备失败，请检查网络、磁盘空间及组件校验，再重试安装。" /SD IDOK
  SetErrorLevel 2
  Quit
  done:
FunctionEnd
!macroend
