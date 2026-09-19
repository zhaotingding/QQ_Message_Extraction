@echo off
rem ============================================================
rem  打开图形界面（由 START_HERE.vbs 调用，无控制台窗口）
rem
rem  界面自己负责窗口，所以这里用 pythonw.exe 启动，避免多弹一个黑框。
rem ============================================================

call "%~dp0launcher.cmd"
if errorlevel 1 exit /b 1

rem 把 python.exe 换成同目录的 pythonw.exe
set "PYW=%PYTHON_EXE:python.exe=pythonw.exe%"

if exist "%PYW%" (
  start "" "%PYW%" "%~dp0gui_entry.py" %*
) else (
  rem 没有 pythonw 就退回普通解释器（会短暂出现一个窗口，可接受）
  "%PYTHON_EXE%" "%~dp0gui_entry.py" %*
)
exit /b 0
