"""
The first window: find a ClassBoard server on the LAN, then start a class
(teacher) or join one (student).

Network work (discovery, connecting) runs in background threads; results
come back to the Tk thread through a queue polled with after(), so the
window never freezes.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk

from common import protocol as P
from common.discovery import discover
from client.net import JoinFailed, NetClient
from .theme import C, FONT


class ConnectFrame(ttk.Frame):
    def __init__(self, master, on_connected, defaults=None):
        super().__init__(master)
        self.on_connected = on_connected
        self.results = queue.Queue()
        d = defaults or {}
        self.mode = tk.StringVar(value="join" if d.get("room") else "create")
        self.host = tk.StringVar(value=d.get("host", "127.0.0.1"))
        self.port = tk.StringVar(value=str(d.get("port", P.DEFAULT_TCP_PORT)))
        self.name = tk.StringVar(value=d.get("name", ""))
        self.room = tk.StringVar(value=d.get("room", ""))
        self.password = tk.StringVar(value=d.get("password", ""))
        self.tls = tk.BooleanVar(value=bool(d.get("tls")))
        self.status = tk.StringVar(value="")
        self.found = []
        self._build()
        self.after(50, self._poll)
        self.after(200, self.find_servers)

    # ------------------------------------------------------------------ layout
    def _build(self):
        head = tk.Frame(self, background=C["accent"])
        head.pack(fill="x")
        tk.Label(head, text="ClassBoard", background=C["accent"], foreground="white",
                 font=(FONT, 22, "bold")).pack(anchor="w", padx=24, pady=(18, 0))
        tk.Label(head, text="A shared whiteboard for your classroom - works on the local "
                            "network, no internet needed.", background=C["accent"],
                 foreground="#d5f2ee", font=(FONT, 10)).pack(anchor="w", padx=24, pady=(0, 16))

        body = ttk.Frame(self, padding=(24, 16, 24, 10))
        body.pack(fill="both", expand=True)

        # --- 1. who are you
        ttk.Label(body, text="Your name", font=(FONT, 10, "bold")).grid(row=0, column=0,
                                                                         sticky="w")
        name_entry = ttk.Entry(body, textvariable=self.name, width=28, font=(FONT, 11))
        name_entry.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))
        name_entry.focus_set()

        # --- 2. teacher or student
        modes = ttk.Frame(body)
        modes.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.mode_cards = {}
        for i, (value, title, sub) in enumerate((
                ("create", "Start a class", "I am the teacher"),
                ("join", "Join a class", "I have a room code"))):
            card = tk.Frame(modes, background=C["surface"], highlightthickness=2,
                            highlightbackground=C["border"], cursor="hand2", padx=14, pady=10)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 10, 0))
            modes.columnconfigure(i, weight=1)
            t = tk.Label(card, text=title, background=C["surface"], foreground=C["text"],
                         font=(FONT, 12, "bold"), cursor="hand2")
            t.pack(anchor="w")
            s = tk.Label(card, text=sub, background=C["surface"], foreground=C["muted"],
                         font=(FONT, 9), cursor="hand2")
            s.pack(anchor="w")
            for w in (card, t, s):
                w.bind("<Button-1>", lambda e, v=value: self._set_mode(v))
            self.mode_cards[value] = card

        form = ttk.Frame(body)
        form.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.room_label = ttk.Label(form, text="Room code", font=(FONT, 10, "bold"))
        self.room_label.grid(row=0, column=0, sticky="w")
        self.room_entry = ttk.Entry(form, textvariable=self.room, width=10, font=(FONT, 12))
        self.room_entry.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.pw_label = ttk.Label(form, text="Class password (optional)",
                                  font=(FONT, 10, "bold"))
        self.pw_label.grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Entry(form, textvariable=self.password, show="*", width=20).grid(
            row=1, column=1, sticky="w", padx=(18, 0), pady=(2, 0))

        # --- 3. which server
        box = ttk.LabelFrame(body, text=" ClassBoard servers on this network ", padding=10)
        box.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(16, 0))
        self.server_list = tk.Listbox(box, height=5, activestyle="none", exportselection=False,
                                      font=(FONT, 10), relief="flat", highlightthickness=1,
                                      highlightbackground=C["border"],
                                      selectbackground=C["accent_soft"],
                                      selectforeground=C["text"])
        self.server_list.grid(row=0, column=0, columnspan=6, sticky="nsew")
        self.server_list.bind("<<ListboxSelect>>", self._pick_server)
        self.server_list.bind("<Double-Button-1>", lambda e: self.connect())
        ttk.Label(box, text="Server IP").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.host, width=18).grid(row=1, column=1, sticky="w",
                                                             padx=(6, 0), pady=(8, 0))
        ttk.Label(box, text="Port").grid(row=1, column=2, sticky="e", padx=(12, 4), pady=(8, 0))
        ttk.Entry(box, textvariable=self.port, width=7).grid(row=1, column=3, sticky="w",
                                                            pady=(8, 0))
        ttk.Checkbutton(box, text="Encrypted (TLS)", variable=self.tls,
                        style="Bg.TCheckbutton").grid(row=1, column=4, sticky="w",
                                                      padx=(12, 0), pady=(8, 0))
        ttk.Button(box, text="Search again", command=self.find_servers).grid(
            row=1, column=5, sticky="e", padx=(8, 0), pady=(8, 0))
        box.columnconfigure(0, weight=0)
        box.columnconfigure(5, weight=1)
        box.rowconfigure(0, weight=1)

        bottom = ttk.Frame(body)
        bottom.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.connect_btn = ttk.Button(bottom, text="Start class", style="Accent.TButton",
                                      command=self.connect)
        self.connect_btn.pack(side="left")
        ttk.Label(bottom, textvariable=self.status, style="Muted.TLabel", wraplength=420,
                  justify="left").pack(side="left", padx=(12, 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(4, weight=1)
        self.winfo_toplevel().bind("<Return>", lambda e: self.connect())
        self._set_mode(self.mode.get())

    def _set_mode(self, value):
        self.mode.set(value)
        for v, card in self.mode_cards.items():
            sel = v == value
            card.configure(highlightbackground=C["accent"] if sel else C["border"],
                           background=C["accent_soft"] if sel else C["surface"])
            for w in card.winfo_children():
                w.configure(background=C["accent_soft"] if sel else C["surface"])
        joining = value == "join"
        self.room_entry.configure(state="normal" if joining else "disabled")
        self.connect_btn.configure(text="Join class" if joining else "Start class")
        self.pw_label.configure(text="Class password" if joining else
                                "Class password (optional)")
        if joining:
            self.room_entry.focus_set()

    # ------------------------------------------------------------------ discovery
    def find_servers(self):
        self.status.set("Looking for ClassBoard servers on this network...")
        self.server_list.delete(0, "end")

        def work():
            try:
                res = discover(timeout=1.2)
            except OSError as exc:
                res = exc
            self.results.put(("found", res))
        threading.Thread(target=work, daemon=True).start()

    def _show_found(self, res):
        self.server_list.delete(0, "end")
        self.found = []
        if isinstance(res, Exception) or not res:
            self.status.set("No server found automatically - type the IP address shown in "
                            "the server window.")
            return
        for srv in res:
            self.found.append((srv, None))
            tls = "  [TLS]" if srv.get("tls") else ""
            old = "  (different version!)" if srv.get("version") not in (None,
                                                                         P.PROTOCOL_VERSION) \
                else ""
            self.server_list.insert("end", f"{srv['name']}  -  {srv['host']}:{srv['port']}"
                                           f"{tls}{old}")
            for room in srv["rooms"]:
                lock = "  [password]" if room.get("has_password") else ""
                self.found.append((srv, room))
                self.server_list.insert("end", f"      class {room['code']}  -  "
                                               f"{room['users']} online{lock}")
        self.status.set(f"Found {len(res)} server(s). Click a class to join it.")
        self.server_list.selection_set(0)
        self._pick_server()

    def _pick_server(self, _e=None):
        sel = self.server_list.curselection()
        if not sel:
            return
        srv, room = self.found[sel[0]]
        self.host.set(srv["host"])
        self.port.set(str(srv["port"]))
        self.tls.set(bool(srv.get("tls")))
        if room:
            self.room.set(room["code"])
            self._set_mode("join")

    # ------------------------------------------------------------------ connect
    def connect(self):
        name = self.name.get().strip()
        if not P.valid_username(name):
            self.status.set("Name: 1-20 letters, digits, _ or - (no spaces).")
            return
        try:
            port = int(self.port.get())
        except ValueError:
            self.status.set("Port must be a number.")
            return
        joining = self.mode.get() == "join"
        room = self.room.get().strip().upper()
        if joining and not room:
            self.status.set("Enter the room code shown on the teacher's screen.")
            return
        self.connect_btn.configure(state="disabled")
        self.status.set(f"Connecting to {self.host.get()}:{port} ...")
        net = NetClient(self.host.get().strip(), port, name, room if joining else None,
                        self.password.get(), create=not joining, tls=self.tls.get())

        def work():
            try:
                welcome = net.connect(timeout=4.0)
                self.results.put(("ok", (net, welcome)))
            except JoinFailed as exc:
                self.results.put(("fail", exc.message))
            except OSError as exc:
                hint = " (does the server use TLS? tick 'Encrypted')" if not self.tls.get() \
                    else " (is TLS really enabled on the server?)"
                self.results.put(("fail", f"Cannot reach the server: {exc}{hint}"))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, val = self.results.get_nowait()
                if kind == "found":
                    self._show_found(val)
                elif kind == "fail":
                    self.status.set(val)
                    self.connect_btn.configure(state="normal")
                elif kind == "ok":
                    self.winfo_toplevel().unbind("<Return>")
                    self.on_connected(*val)
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll)
