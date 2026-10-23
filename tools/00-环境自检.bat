@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check-env.ps1"
echo.
pause
