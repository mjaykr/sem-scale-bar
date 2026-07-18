@echo off
setlocal
set "SEM_READY_ROOT=%~dp0"
if exist "%SEM_READY_ROOT%.venv\Scripts\python.exe" (
  "%SEM_READY_ROOT%.venv\Scripts\python.exe" "%SEM_READY_ROOT%cli.py" %*
) else (
  python "%SEM_READY_ROOT%cli.py" %*
)
exit /b %errorlevel%
