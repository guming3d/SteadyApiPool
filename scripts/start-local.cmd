@echo off
rem Runs start-local.ps1 where the PowerShell execution policy blocks scripts; same options.
rem   scripts\start-local.cmd -Tui
setlocal
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1" %*
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1" %*
)
exit /b %errorlevel%
