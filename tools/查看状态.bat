@echo off
rem 查看运行状态与今日用量。服务运行中也可执行。
rem 本文件已移入 tools\；日常使用请双击项目根目录的「消息萃取.exe」。
title 消息萃取 - 状态

call "%~dp0launcher.cmd"
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   消息萃取 运行状态
echo ============================================================
echo.

"%PYTHON_EXE%" -m msgx.main stats

echo.
pause
