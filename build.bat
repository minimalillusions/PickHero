@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Bitte zuerst START.bat ausfuehren, um PickHero einzurichten.
  exit /b 1
)
"%PYTHON%" -c "from pickhero.selftest import check_runtime; check_runtime()"
if errorlevel 1 (
  echo Bitte scripts\setup.ps1 ausfuehren, um die benoetigte Python-Laufzeit einzurichten.
  exit /b 1
)
"%PYTHON%" -m pip install --find-links wheels -c constraints.txt -r requirements-dev.txt
if errorlevel 1 exit /b 1
"%PYTHON%" scripts\build-notices.py
if errorlevel 1 exit /b 1
"%PYTHON%" -m pytest tests -q
if errorlevel 1 exit /b 1
"%PYTHON%" -m pickhero --self-test build\source-selftest.json
if errorlevel 1 exit /b 1
"%PYTHON%" -m PyInstaller pickhero.spec --noconfirm
if errorlevel 1 exit /b 1
"%PYTHON%" scripts\build-notices.py --verify-exe dist\PickHero.exe
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-packaged.ps1
if errorlevel 1 exit /b 1
echo PickHero wurde erstellt: dist\PickHero.exe
echo Dies ist ein lokaler Build. Es wird nichts veroeffentlicht.
