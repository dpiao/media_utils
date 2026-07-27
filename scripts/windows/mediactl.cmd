@echo off
set "PYTHONPATH=%~dp0..\..\src"
start "" pythonw -m supervisor %*
