@echo off
setlocal
set "SEMFIG_ROOT=%~dp0"
if exist "%SEMFIG_ROOT%.venv\Scripts\python.exe" (
  "%SEMFIG_ROOT%.venv\Scripts\python.exe" "%SEMFIG_ROOT%cli.py" %*
) else (
  python "%SEMFIG_ROOT%cli.py" %*
)
exit /b %errorlevel%
