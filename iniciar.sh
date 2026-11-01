#!/bin/bash
# Linux: doble clic (o "Ejecutar") en este archivo
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "Necesitas instalar Python 3 desde python.org" buttons {"Vale"}' >/dev/null 2>&1
  open "https://www.python.org/downloads/"; exit 1
fi
[ -x .venv/bin/python ] || { echo "Preparando Cazaprecios por primera vez..."; python3 -m venv .venv; }
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
echo; echo "  Cazaprecios se está abriendo en tu navegador."
echo "  Puedes minimizar esta ventana. Ciérrala para apagar Cazaprecios."; echo
exec .venv/bin/python app.py
