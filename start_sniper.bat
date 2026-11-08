@echo off
rem Starts the Goodwill Sniper web UI (always in DRY RUN; go live from the page)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setting up the virtual environment, one time only...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" webapp.py %*
pause
