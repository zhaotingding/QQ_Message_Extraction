!macro customInstall
  ${If} ${isUpdated}
    ; 按用户安装，重启时沿用更新进程的用户上下文。
    SetOutPath "$INSTDIR"
    Exec '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --updated'
  ${EndIf}
!macroend
