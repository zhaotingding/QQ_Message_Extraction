@echo off
rem ============================================================
rem  消息萃取 msgx 主入口
rem    双击进入菜单；START_HERE.cmd --cli 等价。
rem
rem  说明: choice 返回的 errorlevel 必须从大到小判断。
rem        /c 有 8 个字符，所以最后一个选项对应 errorlevel 8。
rem ============================================================

if /i "%~1"=="--cli" goto console
goto console

:console
call "%~dp0tools\launcher.cmd"
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

:menu
cls
echo ============================================================
echo   消息萃取 msgx
echo ------------------------------------------------------------
echo   项目目录 : %PROJECT_DIR%
echo   Python   : %PYTHON_EXE%
echo ============================================================
echo   1. 启动服务（常驻；关闭窗口即停止）
echo   2. 查看运行状态与用量
echo   3. 立即生成摘要
echo   4. 生成当日日报
echo   5. 探测模型是否支持图片
echo   6. 清理过期数据
echo   7. NapCat 环境体检
echo   0. 退出
echo ============================================================
choice /c 12345670 /n /m "请选择: "

if errorlevel 8 goto opt0
if errorlevel 7 goto opt7
if errorlevel 6 goto opt6
if errorlevel 5 goto opt5
if errorlevel 4 goto opt4
if errorlevel 3 goto opt3
if errorlevel 2 goto opt2
if errorlevel 1 goto opt1
goto menu

:opt0
exit /b 0

:opt1
echo.
echo 正在启动服务。保持本窗口开启，按 Ctrl+C 可停止。
echo.
"%PYTHON_EXE%" -m msgx.main run
echo.
echo #### 服务已退出 ####
pause
goto menu

:opt2
echo.
"%PYTHON_EXE%" -m msgx.main stats
echo.
pause
goto menu

:opt3
echo.
"%PYTHON_EXE%" -m msgx.main summary --kind periodic
echo.
pause
goto menu

:opt4
echo.
"%PYTHON_EXE%" -m msgx.main summary --kind daily
echo.
pause
goto menu

:opt5
echo.
"%PYTHON_EXE%" -m msgx.main vision --force
echo.
pause
goto menu

:opt6
echo.
set /p CONFIRM=将按 config.toml 的保留策略删除过期数据，确认？
if /i not "%CONFIRM%"=="y" goto menu
"%PYTHON_EXE%" -m msgx.main prune
echo.
pause
goto menu

:opt7
echo.
"%PYTHON_EXE%" "%~dp0tools\check_napcat_env.py" --probe 6700 6701
echo.
pause
goto menu
