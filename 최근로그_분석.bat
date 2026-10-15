@echo off
setlocal
cd /d "%~dp0"
title KTB RV Summary

if not exist ".venv32\Scripts\python.exe" (
    echo 32-bit Python environment was not found.
    pause
    exit /b 1
)

".venv32\Scripts\python.exe" ".\analyze_snapshots.py" ".\data\snapshots_with_trades.csv" --settings ".\settings.json"
echo.
pause
