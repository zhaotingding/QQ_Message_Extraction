@echo off
rem ============================================================
rem  msgx 环境探测
rem  用法:  call "tools\launcher.cmd"
rem  之后即可直接使用 %PYTHON_EXE%（已 cd 到项目根、已设好 PYTHONPATH）
rem
rem  说明: 本脚本刻意不使用 setlocal —— 变量需要留给调用者使用。
rem ============================================================

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
set "PYTHON_EXE="

rem ---- 1. 项目自带虚拟环境（推荐，可复现）----
if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"

rem ---- 2. 常见 conda 环境（没有 .venv 时的退路）----
if not defined PYTHON_EXE if exist "D:\Dev\Anaconda\envs\msgx\python.exe" set "PYTHON_EXE=D:\Dev\Anaconda\envs\msgx\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\anaconda3\envs\msgx\python.exe" set "PYTHON_EXE=%USERPROFILE%\anaconda3\envs\msgx\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\envs\msgx\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\envs\msgx\python.exe"

if not defined PYTHON_EXE (
  echo [错误] 找不到可用的 Python 环境。
  echo.
  echo 已检查:
  echo   1. %PROJECT_DIR%\.venv\Scripts\python.exe
  echo   2. D:\Dev\Anaconda\envs\msgx\python.exe
  echo   3. 用户目录下的 anaconda3 / miniconda3 的 envs\msgx
  echo.
  echo 请在项目根目录执行下面两行来创建环境:
  echo     py -3.13 -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -e .
  echo.
  echo 注意: 不要用 Anaconda 基础环境的 python，依赖是装在独立环境里的。
  exit /b 1
)

rem ---- 清掉可能存在的代理设置 ----
rem 本服务要与 127.0.0.1 上的 NapCat 通信。若这些变量指向系统代理，
rem aiohttp 会把本地请求也发过去，症状是「连不上自己的服务」，很难查。
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "NO_PROXY=127.0.0.1,localhost"

cd /d "%PROJECT_DIR%"
set "PYTHONPATH=%PROJECT_DIR%\src"
set "PYTHONUTF8=1"
exit /b 0
