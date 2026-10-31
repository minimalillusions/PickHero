@echo off
rem Startet PickHero aus der lokalen virtuellen Umgebung.
rem Die Einrichtung nutzt eine gepatchte, projektlokale Python-Laufzeit.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 || goto :fehler
)
.venv\Scripts\python.exe -c "import sys, struct, pyexpat; sys.exit(not (sys.version_info[:2] == (3, 12) and sys.version_info[:3] >= (3, 12, 14) and struct.calcsize('P') == 8 and pyexpat.version_info >= (2, 8, 3)))"
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 || goto :fehler
)
.venv\Scripts\python.exe -m pickhero
if errorlevel 1 goto :fehler
goto :eof
:fehler
echo.
echo PickHero konnte nicht gestartet werden. Fehler siehe oben.
pause
