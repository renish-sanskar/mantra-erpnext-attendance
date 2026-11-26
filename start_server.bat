@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe app.py >> app.log 2>&1
