@echo off
set "PYTHONPATH=%~dp0..\..\src\windows"
python "%~dp0..\..\src\windows\concat_videos.py" %*
