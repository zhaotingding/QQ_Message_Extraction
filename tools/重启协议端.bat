@echo off
rem ============================================================
rem  一键重置协议端：把**所有** NapCat 注入器与 QQ.exe 全退掉，再重新拉起
rem
rem  为什么需要它
rem    点「启动协议端」只是「再注入一份 + 再拉起一份 QQ」，它**不会先清场**。
rem    旧的那份如果已经卡住（连着、能应答 API，但一条消息都不来），新的一份
rem    会和它并存；两份各自往 <NapCat 目录>\cache\qrcode.png 写二维码互相覆盖，
rem    扫哪一次都可能立刻失效。
rem    所以「消息冻结」时正确的动作是：**先全部退出，再重新登录** ——
rem    本脚本就是那个动作，双击一次、点一次 UAC 就够。
rem
rem  用法
rem    双击本文件即可。也可以在命令行给一个 NapCat 目录作参数。
rem
rem  落盘格式：GBK + CRLF（.gitattributes 给 *.bat 标了 -text，保住字节原样）。
rem ============================================================

setlocal
rem 静默模式（界面调它时设 MSGX_RESTART_SILENT=1）：窗口是隐藏的，
rem 末尾的 pause 没人能按键 → 会变成一个看不见的僵尸进程。必须跳过。
set "SILENT="
if /i "%MSGX_RESTART_SILENT%"=="1" set "SILENT=1"
set "TARGET=%~1"
if not defined TARGET set "TARGET=%NAPCAT_HOME%"
if not defined TARGET if exist "D:\App\NapCat\launcher.bat" set "TARGET=D:\App\NapCat"
if not defined TARGET if exist "C:\NapCat\launcher.bat" set "TARGET=C:\NapCat"

if not defined TARGET goto nodir
if not exist "%TARGET%\launcher.bat" goto nodir

rem 先切目录再判权限：提权后的新窗口会重新执行本脚本，cd 必须在判断之前。
cd /d "%TARGET%"

net session >nul 2>&1
if not "%ERRORLEVEL%"=="0" goto elevate
goto reset

:nodir
echo [错误] 没有找到 NapCat 目录。
echo         用法: restart_napcat.bat "D:\App\NapCat"
echo         或先设置环境变量 NAPCAT_HOME。
pause
exit /b 1

:elevate
echo [提权] 正在请求管理员权限 —— 请在弹窗里点「是」，然后在新窗口里继续。
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%TARGET%' -Verb RunAs"
exit /b

:reset
echo ============================================================
echo  重置协议端：先全部退出，再重新登录
echo ============================================================
echo.
echo [1/5] 结束 NapCat 注入器 ...
taskkill /F /IM NapCatWinBootMain.exe >nul 2>&1

echo [2/5] 结束 QQ.exe ...
taskkill /F /IM QQ.exe >nul 2>&1

rem 注入器还活着会把 QQ 重新拉起来，所以等两秒再补一刀。
echo [3/5] 等 2 秒后复查补刀 ...
ping -n 3 127.0.0.1 >nul
taskkill /F /IM NapCatWinBootMain.exe >nul 2>&1
taskkill /F /IM QQ.exe >nul 2>&1

echo.
echo [4/5] 复查残留 —— 下面应当只剩「没有运行的任务匹配指定标准」
tasklist /FI "IMAGENAME eq NapCatWinBootMain.exe" /FO CSV /NH
tasklist /FI "IMAGENAME eq QQ.exe" /FO CSV /NH

echo.
echo [5/5] 重新注入并拉起 QQ —— NapCat 稍后会写下二维码，请扫码登录
if defined SILENT echo     静默模式：二维码会由界面自动弹出（%%TARGET%%\cache\qrcode.png）
echo.
call "%TARGET%\launcher.bat"

echo.
echo #### 重置流程结束。请扫码登录，然后回界面看「实时消息」是否开始滚动。####
if defined SILENT exit /b 0
pause
