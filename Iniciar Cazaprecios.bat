@echo off
title Cazaprecios
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Necesitas instalar Python (marca "Add Python to PATH" al instalar^).
  echo  Abriendo la pagina de descarga...
  start https://www.python.org/downloads/
  pause
  exit /b
)
if not exist ".venv\Scripts\python.exe" (
  echo  Preparando Cazaprecios por primera vez, un momento...
  %PY% -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
echo.
echo  Cazaprecios se esta abriendo en tu navegador.
echo  Puedes minimizar esta ventana. Cierrala para apagar Cazaprecios.
".venv\Scripts\python.exe" app.py
