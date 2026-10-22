@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv32\Scripts\pythonw.exe" (
    echo Python environment not found: %cd%\.venv32
    pause
    exit /b 1
)

start "" ".venv32\Scripts\pythonw.exe" ".\spread_chart_launcher.py"
