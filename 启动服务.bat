@echo off
rem 启动常驻服务。保持本窗口开启；关闭窗口 = 停止服务。
title 消息萃取 - 服务

call "%~dp0tools\launcher.cmd"
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   消息萃取 服务
echo ------------------------------------------------------------
echo   项目目录 : %PROJECT_DIR%
echo   Python   : %PYTHON_EXE%
echo ============================================================
echo   服务启动后请保持本窗口开启。
echo   停止服务：在本窗口按 Ctrl+C，或直接关闭窗口。
echo ============================================================
echo.

"%PYTHON_EXE%" -m msgx.main run

echo.
echo #### 服务已退出（退出码 %ERRORLEVEL%）####
echo #### 若上方有报错，请整屏截图。 ####
pause
