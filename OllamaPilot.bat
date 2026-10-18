@echo off
cd /d "%~dp0"
py -3 -m ollamapilot
if errorlevel 1 pause
