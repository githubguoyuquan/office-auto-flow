@echo off
setlocal

set "launcher=%~dp0start_oa_login.ps1"

if not exist "%launcher%" (
    echo ERROR: start_oa_login.ps1 was not found in this folder.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%launcher%"
set "app_exit_code=%errorlevel%"

if not "%app_exit_code%"=="0" (
    echo.
    echo The launcher exited with code %app_exit_code%.
    pause
)

exit /b %app_exit_code%
