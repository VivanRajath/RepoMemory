@echo off
start "RepoMemory backend" cmd /k "%~dp0start-backend.bat"
start "RepoMemory frontend" cmd /k "%~dp0start-frontend.bat"
timeout /t 8 >nul
start http://127.0.0.1:5173
