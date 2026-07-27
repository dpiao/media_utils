@echo off
set "PYTHONPATH=%~dp0..\..\src"
python -m sync %*
