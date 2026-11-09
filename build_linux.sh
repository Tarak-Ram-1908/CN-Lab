#!/usr/bin/env bash
# Build standalone Linux executables (needs Python 3.10+ with Tkinter: sudo apt install python3-tk)
# Output: dist/ClassBoard-Server and dist/ClassBoard-Client
set -euo pipefail
cd "$(dirname "$0")"
python3 -c "import tkinter" || { echo "Tkinter missing: sudo apt install python3-tk"; exit 1; }
python3 -m pip install --upgrade pyinstaller pillow sounddevice
python3 -m PyInstaller --noconfirm --clean --onefile --name ClassBoard-Server run_server.py
python3 -m PyInstaller --noconfirm --clean --onefile --windowed --name ClassBoard-Client run_client.py
chmod +x dist/ClassBoard-Server dist/ClassBoard-Client
echo "Done. Executables are in dist/:"
ls -l dist
