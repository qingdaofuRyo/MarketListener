@echo off
rem Legacy entry delegates to the current local web service bootstrap.
cd /d "%~dp0.."
desktop\.venv\Scripts\pythonw.exe scripts\start_marketlistener.py --target none
