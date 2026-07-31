@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TIANLAI_RESOURCE_BOOTSTRAP=%~dp0安装全部音源.ps1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TIANLAI_RESOURCE_BOOTSTRAP%" %*
exit /b %ERRORLEVEL%
