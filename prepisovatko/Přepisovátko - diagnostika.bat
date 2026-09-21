@echo off
rem Diagnostický start — nechá otevřenou konzoli, aby byly vidět případné chyby.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo Spoustim Prepisovatko... (toto cerne okno nechte otevrene)
echo.
"%~dp0python\python.exe" "%~dp0gui.py"
echo.
echo ------------------------------------------------------------------
echo Pokud se okno aplikace otevrelo a slo pouzivat, je vse V PORADKU.
echo Pripadne vypisy vyse jsou jen informacni, nejde o chyby.
echo.
echo Spravci aplikace piste jen kdyz se okno aplikace vubec neotevrelo.
echo V tom pripade mu posleTE text vypsany vyse (staci foto obrazovky).
echo Zaznam o behu aplikace (log): %LOCALAPPDATA%\Prepisovatko\prepisovatko.log
echo ------------------------------------------------------------------
pause
