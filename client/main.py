"""
ClassBoard client entry point.

    python run_client.py                         # opens the connect window
    python run_client.py --host 192.168.1.20 --name alice --create
    python run_client.py --host 192.168.1.20 --name bob --room K7Q2 --password secret
    python run_client.py --tls ...               # server started with --tls-cert/--tls-key

With --name plus --create or --room, the client connects straight away
(handy for demos); otherwise the connect window lets you discover servers.
"""

import argparse
import sys
import tkinter as tk
from tkinter import messagebox

from common import protocol as P
from client.net import JoinFailed, NetClient
from client.ui.app import WhiteboardApp
from client.ui.theme import setup_style
from client.ui.connect import ConnectFrame


def main(argv=None):
    ap = argparse.ArgumentParser(description="ClassBoard - LAN collaborative whiteboard client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=P.DEFAULT_TCP_PORT)
    ap.add_argument("--name", default="")
    ap.add_argument("--room", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--create", action="store_true", help="host a new room immediately")
    ap.add_argument("--tls", action="store_true", help="the server uses TLS encryption")
    ap.add_argument("--geometry", default="1400x860")
    args = ap.parse_args(argv)

    root = tk.Tk()
    root.title("ClassBoard")
    root.geometry(args.geometry)
    root.minsize(1000, 600)
    setup_style(root)

    def start_app(net, welcome):
        for child in root.winfo_children():
            child.destroy()
        root.geometry(args.geometry)
        WhiteboardApp(root, net, welcome)

    auto = args.name and (args.create or args.room)
    if auto:
        net = NetClient(args.host, args.port, args.name, args.room or None,
                        args.password, create=args.create, tls=args.tls)
        try:
            welcome = net.connect()
        except (JoinFailed, OSError) as exc:
            root.withdraw()
            messagebox.showerror("Cannot connect", getattr(exc, "message", str(exc)))
            return 1
        start_app(net, welcome)
    else:
        frame = ConnectFrame(root, start_app, defaults={
            "host": args.host, "port": args.port, "name": args.name,
            "room": args.room, "password": args.password, "tls": args.tls})
        frame.pack(fill="both", expand=True)
        root.geometry("640x660")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
