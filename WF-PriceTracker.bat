@echo off
rem Double-click launcher: runs the app with no console window using the
rem project's virtual environment.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "run.pyw"
