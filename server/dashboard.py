"""
Live server dashboard (python run_server.py --dashboard).

A small Tk window, refreshed every second, that shows what the server is doing:
the addresses students should connect to, every room with its users and board
size, and the traffic rate. Handy during the demo to *show* the networking.
"""

import time
import tkinter as tk
from tkinter import ttk


def run_dashboard(server, ips, port):
    root = tk.Tk()
    root.title("ClassBoard server")
    root.geometry("760x460")
    root.configure(background="#f6f7f9")
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure(".", background="#f6f7f9", font=("Helvetica", 10))
    style.configure("Big.TLabel", font=("Helvetica", 15, "bold"), foreground="#0f766e")
    style.configure("Num.TLabel", font=("Helvetica", 18, "bold"), foreground="#1f2933")
    style.configure("Cap.TLabel", foreground="#5f6b7a", font=("Helvetica", 9))

    top = ttk.Frame(root, padding=(14, 12, 14, 4))
    top.pack(fill="x")
    ttk.Label(top, text="ClassBoard server is running", style="Big.TLabel").pack(anchor="w")
    addr = "   ".join(f"{ip}:{port}" for ip in ips) or f"127.0.0.1:{port}"
    ttk.Label(top, text=f"Students connect to:  {addr}"
                        f"{'   (TLS on)' if server.ssl_context else ''}").pack(anchor="w",
                                                                              pady=(2, 0))

    tiles = ttk.Frame(root, padding=(14, 8))
    tiles.pack(fill="x")
    values = {}
    for i, (key, cap) in enumerate((("rooms", "rooms"), ("users", "online"),
                                    ("in", "messages/s in"), ("out", "messages/s out"),
                                    ("kbin", "KB/s in"), ("kbout", "KB/s out"))):
        f = ttk.Frame(tiles, padding=(10, 4))
        f.grid(row=0, column=i, sticky="w")
        values[key] = ttk.Label(f, text="0", style="Num.TLabel")
        values[key].pack(anchor="w")
        ttk.Label(f, text=cap, style="Cap.TLabel").pack(anchor="w")

    cols = ("users", "objects", "seq", "state")
    tv = ttk.Treeview(root, columns=cols, show="tree headings", height=10)
    tv.heading("#0", text="Room")
    tv.column("#0", width=90)
    for c, w, t in (("users", 260, "Participants"), ("objects", 80, "Objects"),
                    ("seq", 80, "Last seq"), ("state", 200, "State")):
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor="w")
    tv.pack(fill="both", expand=True, padx=14, pady=(4, 6))
    foot = ttk.Label(root, style="Cap.TLabel", padding=(14, 0, 14, 8))
    foot.pack(anchor="w")

    prev = {"snap": server.rooms.stats.snapshot(), "t": time.monotonic()}

    def refresh():
        snap = server.rooms.stats.snapshot()
        now = time.monotonic()
        dt = max(0.001, now - prev["t"])
        p = prev["snap"]
        rooms = server.rooms.all()
        tv.delete(*tv.get_children())
        online = 0
        for r in rooms:
            with r.lock:
                people = []
                for part in r.participants.values():
                    mark = {"host": "*", "viewer": "(v)"}.get(part.role, "")
                    if not part.connected:
                        mark += " away"
                    people.append(part.username + mark)
                    online += part.connected
                state = []
                if r.locked:
                    state.append("locked")
                if r.follow:
                    state.append(f"{r.follow} presenting")
                if r.poll and r.poll["open"]:
                    state.append("poll open")
                if r.pw_hash:
                    state.append("password")
                tv.insert("", "end", text=r.code,
                          values=(", ".join(people) or "-", len(r.model.visible_ids()), r.seq,
                                  ", ".join(state)))
        values["rooms"].configure(text=str(len(rooms)))
        values["users"].configure(text=str(online))
        values["in"].configure(text=f"{(snap['msgs_in'] - p['msgs_in']) / dt:.0f}")
        values["out"].configure(text=f"{(snap['msgs_out'] - p['msgs_out']) / dt:.0f}")
        values["kbin"].configure(text=f"{(snap['bytes_in'] - p['bytes_in']) / dt / 1024:.1f}")
        values["kbout"].configure(text=f"{(snap['bytes_out'] - p['bytes_out']) / dt / 1024:.1f}")
        foot.configure(text=f"Total: {snap['msgs_in']} messages in, {snap['msgs_out']} out, "
                            f"{snap['bytes_out'] / 1048576:.2f} MB sent   |   up "
                            f"{int(snap['uptime'] // 60)} min   |   * = host, (v) = viewer   "
                            "|   close this window to stop the server")
        prev["snap"], prev["t"] = snap, now
        root.after(1000, refresh)

    refresh()
    root.mainloop()
