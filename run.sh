#!/usr/bin/env bash
# run.sh — Executa el recull setmanal a Linux/Mac/WSL.
# Crea l'entorn virtual i instal·la dependències el primer cop.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "▶ Creant entorn virtual (.venv)..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "▶ Instal·lant/actualitzant dependències..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "▶ Generant el post setmanal..."
python main.py "$@"
