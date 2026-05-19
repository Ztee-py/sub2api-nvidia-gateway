@echo off
setlocal

set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

if exist "%REPO_ROOT%run-system.local.cmd" (
  call "%REPO_ROOT%run-system.local.cmd"
)

echo.
echo == ZteAPI one-click start ==
echo Repo: %REPO_ROOT%
echo.
echo This starts the Windows WeChat watcher, then opens SSH for server deploy.
echo Enter the server password when SSH asks; deploy will continue automatically.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%run-system.ps1" -OpenServerLogin

echo.
echo == Finished ==
pause
