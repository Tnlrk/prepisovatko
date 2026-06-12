@echo off
rem Diagnostický start — nechá otevřenou konzoli, aby byly vidět případné chyby.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%~dp0python\python.exe" "%~dp0gui.py"
echo.
echo (Okno muzes zavrit. Pokud vyse vidis chybu, posli ji spravci aplikace.)
pause
