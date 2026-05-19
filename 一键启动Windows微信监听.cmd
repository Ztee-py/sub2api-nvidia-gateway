@echo off
setlocal

set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

echo.
echo == ZteAPI Windows WeChat watcher start ==
echo Repo: %REPO_ROOT%
echo.
echo This starts the WeChat decrypt refresher and QRPay watcher only.
echo It will not connect to the server or deploy production services.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%run-system.ps1" -SkipServer

echo.
echo == Finished ==
pause
