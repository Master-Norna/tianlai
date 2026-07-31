@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TIANLAI_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TIANLAI_PYTHON%" (
  echo Tianlai environment is missing. Run "%~dp0安装运行环境.cmd" first. 1>&2
  exit /b 2
)
"%TIANLAI_PYTHON%" -m tianlai %*
exit /b %ERRORLEVEL%
