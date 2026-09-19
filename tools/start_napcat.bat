@echo off
rem ============================================================
rem  NapCat 启动包装器
rem  用法: start_napcat.bat [NapCat目录]
rem        不给参数时依次尝试 %NAPCAT_HOME% 与几个常见位置。
rem
rem  为什么要这个包装器:
rem    NapCat 自带的 launcher.bat 全程用 %cd% 推导路径。右键「以管理员身份运行」时
rem    工作目录是 C:\Windows\System32，于是它去找 System32\NapCatWinBootMain.exe,
rem    找不到就退出 —— 表现就是窗口一闪而过。
rem    本脚本先修正工作目录，再提权，再调用 launcher.bat。
rem
rem  写法注意: 全程避免在 if(...) 块内部「先赋值再使用同一变量」。
rem    块内 %VAR% 在整块解析时就已展开，会读到空值（经典的延迟展开陷阱）。
rem ============================================================

set "TARGET=%~1"
if not defined TARGET set "TARGET=%NAPCAT_HOME%"
if not defined TARGET if exist "D:\App\NapCat\launcher.bat" set "TARGET=D:\App\NapCat"
if not defined TARGET if exist "C:\NapCat\launcher.bat" set "TARGET=C:\NapCat"

if not defined TARGET (
  echo [错误] 没有指定 NapCat 目录。
  echo.
  echo 用法: start_napcat.bat "D:\App\NapCat"
  echo 或先设置环境变量 NAPCAT_HOME 指向 NapCat 解压目录。
  pause
  exit /b 1
)

if not exist "%TARGET%\launcher.bat" (
  echo [错误] 在下面这个目录里找不到 launcher.bat:
  echo    %TARGET%
  echo.
  echo 请确认 NapCat.Shell.zip 已完整解压到该目录。
  pause
  exit /b 1
)

rem 先切到 NapCat 目录再判断权限：提权后的新进程会重新执行本脚本，
rem 因此 cd 必须在提权判断之前。
cd /d "%TARGET%"

net session >nul 2>&1
if not "%ERRORLEVEL%"=="0" goto elevate
goto checkfiles

:elevate
echo [提权] 当前不是管理员，正在请求 UAC 授权...
echo        请在弹窗里点「是」，然后在新窗口中继续。
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%TARGET%' -Verb RunAs"
echo [提权] 请求已提交，本窗口关闭。
exit /b

:checkfiles
echo ============================================================
echo  NapCat 启动检查
echo ============================================================
echo  工作目录 : %cd%
echo.
echo -- 关键文件 --

set "MISSING=0"
if not exist "%TARGET%\launcher.bat" set "MISSING=1"
if not exist "%TARGET%\NapCatWinBootMain.exe" set "MISSING=1"
if not exist "%TARGET%\NapCatWinBootHook.dll" set "MISSING=1"
if not exist "%TARGET%\napcat.mjs" set "MISSING=1"
if not exist "%TARGET%\qqnt.json" set "MISSING=1"

for %%F in (launcher.bat NapCatWinBootMain.exe NapCatWinBootHook.dll napcat.mjs qqnt.json) do (
  if exist "%TARGET%\%%F" (echo   [有] %%F) else (echo   [缺] %%F)
)

if "%MISSING%"=="1" goto incomplete

goto locateqq

:incomplete
echo.
echo [中止] 上面有文件缺失，说明解压不完整。
echo        请重新解压 NapCat.Shell.zip 到本目录。
pause
exit /b 1

:locateqq
echo.
echo -- 从注册表定位 QQ --
set "RetString="
for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v "UninstallString" 2^>nul') do set "RetString=%%~b"
echo   UninstallString = [%RetString%]

set "QQDIR="
if defined RetString for %%a in ("%RetString%") do set "QQDIR=%%~dpa"
if defined QQDIR echo   推导出 QQ.exe  = %QQDIR%QQ.exe
if not defined QQDIR echo   (未读到注册表项。若 NapCat 随后报找不到 QQ，请确认 QQ 已安装)

echo.
echo ============================================================
echo  调用 launcher.bat ...
echo ============================================================
call "%TARGET%\launcher.bat"
set "RC=%ERRORLEVEL%"

echo.
echo #### launcher.bat 已返回，退出码 = %RC% ####
echo #### 若上方有报错，请整屏截图。 ####
pause
