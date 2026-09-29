@echo off
rem Double-click to uninstall Castika DeckShow on Windows (same SmartScreen note as Install-Win.cmd).
rem Terminal users can run installer\uninstall.ps1 directly (PowerShell).
set DECKSHOW_ENTRY=command
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\uninstall.ps1"
echo.
pause
