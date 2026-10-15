@echo off
setlocal
cd /d "%~dp0"
title KTB Paper Backtest

if not exist ".venv32\Scripts\python.exe" (
    echo Python environment not found.
    pause
    exit /b 1
)

set "REPORT=data\paper_trades_%RANDOM%_%RANDOM%.csv"
".venv32\Scripts\python.exe" ".\paper_backtest.py" ".\data\snapshots_with_trades.csv" --settings ".\settings.json" --output "%REPORT%"
if errorlevel 1 (
    echo Replay failed. No successful report is confirmed.
    pause
    exit /b 1
)
echo.
echo Result: %REPORT%
pause
