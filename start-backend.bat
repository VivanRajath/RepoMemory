@echo off
cd /d "%~dp0backend"
if not exist .venv (python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt)
.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8000
