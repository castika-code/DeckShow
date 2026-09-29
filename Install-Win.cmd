@echo off
rem Double-click to install Castika DeckShow on Windows. If SmartScreen or the
rem browser warns about a downloaded script, choose "More info" > "Run anyway".
rem Terminal users can run installer\install.ps1 directly (PowerShell).
set DECKSHOW_ENTRY=command
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
echo.
pause
