@echo off
rem Přepisovátko — spouštěč (konzole jen krátce problikne)
cd /d "%~dp0"
start "" "%~dp0python\pythonw.exe" "%~dp0gui.py"
