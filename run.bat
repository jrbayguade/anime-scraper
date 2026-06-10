@echo off
REM run.bat - Executa el recull setmanal a Windows.
REM Crea l'entorn virtual i instal-la dependencies el primer cop.
cd /d "%~dp0"

if not exist ".venv" (
  echo Creant entorn virtual (.venv)...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Instal-lant/actualitzant dependencies...
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo Generant el post setmanal...
python main.py %*

pause
