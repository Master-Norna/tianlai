@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TIANLAI_BOOTSTRAP=%~dp0bootstrap_windows.ps1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%TIANLAI_BOOTSTRAP%" %*
set "TIANLAI_EXIT=%ERRORLEVEL%"
if not "%TIANLAI_EXIT%"=="0" (
  echo.
  echo Tianlai bootstrap failed with exit code %TIANLAI_EXIT%.
)
exit /b %TIANLAI_EXIT%
