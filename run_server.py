"""Start the whiteboard server:  python run_server.py [--port 5050] [--save-dir saves]"""
import sys

from server.main import main

if __name__ == "__main__":
    sys.exit(main())
