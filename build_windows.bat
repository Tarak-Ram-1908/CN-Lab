@echo off
REM Build standalone Windows executables (run once on a Windows machine with Python 3.10+).
REM Output: dist\ClassBoard-Server.exe and dist\ClassBoard-Client.exe
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller pillow sounddevice || goto :error
python -m PyInstaller --noconfirm --clean --onefile --console  --name ClassBoard-Server run_server.py || goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed --name ClassBoard-Client run_client.py || goto :error
echo.
echo Done. Executables are in the dist folder:
dir /b dist
goto :eof
:error
echo Build failed.
exit /b 1
