@echo off
setlocal
set "ROOT=%~dp0..\.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
if exist "%ROOT%\.venv\Scripts\python.exe" (
  "%ROOT%\.venv\Scripts\python.exe" -m batch %*
) else (
  python -m batch %*
)
