@echo off
REM Start the ClassBoard server with the live dashboard window.
cd /d "%~dp0"
python run_server.py --dashboard
pause
