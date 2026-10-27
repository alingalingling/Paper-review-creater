@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 scripts\public_site.py start
pause
