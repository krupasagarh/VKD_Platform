@echo off
REM Double-click to start live app + tunnel now (no extra windows).
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0start_on_boot.ps1"
