"""
ClassBoard main window - a shared whiteboard designed for teaching a class.

    +--------------------------------------------------------------------------+
    | ClassBoard  Room K7Q2  Teacher | < Page 2/5 > + | Undo Redo | - 100% + Fit |  Present  ... |
    +------+-------------------------------------------------+-----------------+
    | tool |                                                 | Class | People |  |
    | rail |        pages of the board (infinite canvas)     | Chat            |
    |      |                                     [minimap]   |  timer, poll,   |
    |      |                                                 |  hands, votes   |
    +------+-------------------------------------------------+-----------------+
    | connected - 2 ms | 24 online | seq 512                    hover info     |
    +--------------------------------------------------------------------------+

Roles are shown in classroom words: the host is the *Teacher*; editors are
*Students* who can draw; viewers are *Students (view only)*.

Threading rule: only this (Tk main) thread touches widgets. NetClient's
background threads put messages on `net.events`; `_pump()` drains that queue
every 15 ms with after() and applies each message here.
"""

import os
import queue
import sys
import tempfile
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

from common import geometry as G
from common import protocol as P
from common.model import BoardModel
from client.audio import MAX_SECONDS, Player, Recorder
from client.media import MediaClient, human_size, kind_for
from . import export
from .attachments import AttachmentCard
from .board import BoardCanvas, Minimap
from .images import ImageCache, image_size
from .theme import C, FONT, Card, IconButton, ScrollFrame, Tooltip, setup_style  # noqa: F401

PALETTE = ["#1f2933", "#e11d48", "#f97316", "#eab308", "#16a34a",
           "#0f766e", "#2563eb", "#7c3aed", "#db2777", "#ffffff"]
STICKY_FILLS = {"#1f2933": "#fff59d", "#e11d48": "#ffd6dc", "#f97316": "#ffe2c6",
                "#eab308": "#fff59d", "#16a34a": "#d4f5dd", "#0f766e": "#ccf2ec",
                "#2563eb": "#d7e5ff", "#7c3aed": "#e8dcff", "#db2777": "#ffd9ec"}

# (value, caption, key, icon, tooltip, has variants)
TOOLS = [
    ("select", "Select", "V", "select", "Select, move, resize, edit (double-click) and delete "
     "objects. Drag on empty space to select several.", False),
    ("hand", "Pan", "H", "hand", "Move around the board (also: middle mouse, Space+drag, "
     "mouse wheel). Ctrl+wheel zooms.", False),
    ("pen", "Pen", "P", "pen", "Freehand pen. With Smart shapes on, rough circles, boxes, "
     "triangles and lines become perfect shapes.", False),
    ("highlighter", "Marker", "M", "marker", "Highlighter (see-through)", False),
    ("laser", "Laser", "K", "laser", "Laser pointer: point at things for everyone, "
     "without drawing. The trail fades away.", False),
    ("eraser", "Eraser", "E", "eraser", "Erase whole objects you drag over", False),
    ("shape", "Shape", "S", "shape", "Draw a shape (click again to choose: rectangle, ellipse, "
     "triangle, diamond, line, arrow). Shift = square / circle.", True),
    ("text", "Text", "T", "text", "Click and type text on the board", False),
    ("sticky", "Note", "N", "note", "Sticky note: click and type (good for brainstorming)",
     False),
    ("node", "Node", "F", "node", "Flowchart / mind-map node (click again to choose its "
     "shape). Select a node: Tab = add child, Enter = add sibling.", True),
    ("connect", "Connect", "C", "connect", "Drag from one node to another to connect them "
     "(click again to choose arrow / line / curve).", True),
    ("vote", "Vote", "U", "vote", "Click an object to give it one of your 5 votes", False),
]
VIEW_TOOLS = ("hand", "vote", "select")          # usable while you cannot draw
SHAPE_KINDS = [("rect", "Rectangle"), ("ellipse", "Ellipse / circle"),
               ("triangle", "Triangle"), ("diamond", "Diamond"), ("line", "Line"),
               ("arrow", "Arrow")]
NODE_KINDS = [("rounded", "Rounded box"), ("rect", "Process (box)"),
              ("diamond", "Decision (diamond)"), ("parallelogram", "Input / output"),
              ("ellipse", "Start / end")]
EDGE_KINDS = [("arrow", "Arrow"), ("line", "Line"), ("curve", "Curve (mind map)")]
PAPER_NAMES = [("plain", "Plain"), ("grid", "Grid"), ("ruled", "Ruled (notebook)"),
               ("dots", "Dot grid"), ("graph", "Graph paper")]
ROLE_WORD = {P.ROLE_HOST: "Teacher", P.ROLE_EDITOR: "Student",
             P.ROLE_VIEWER: "Student (view only)"}

HELP = """ClassBoard - quick guide

TOOLS (left rail)                         KEYS
  Select / move / resize / edit             V
  Pan the board                             H  (or Space+drag, middle mouse, wheel)
  Pen / Marker                              P / M
  Laser pointer                             K
  Eraser                                    E
  Shapes (rect, ellipse, triangle...)       S  (click the tool again to pick a shape)
  Text / Sticky note                        T / N
  Flowchart node / Connect nodes            F / C
  Insert image                              I  (or Ctrl+V to paste)
  Vote                                      U  (or right-click an object)

EDITING                                   VIEW
  Undo / Redo     Ctrl+Z / Ctrl+Y            Zoom          Ctrl+wheel, Ctrl+= / Ctrl+-
  Delete          Del                         Fit all       Ctrl+0
  Duplicate       Ctrl+D                      Prev / next   PageUp / PageDown
  Select all      Ctrl+A                      Help          F1
  Edit text       double-click (or Enter)
  Mind map        select a node: Tab = child, Enter = sibling

TEACHER
  Present (follow me): every student's view follows yours.
  Students can draw: untick to make the board read-only for students.
  Class tab: timer, quick polls, raised hands, votes, paper style, pages,
             own-work-only mode, history, save / open board files, end session.
  People tab: right-click a student to message them, make them view-only,
              hand over the teacher role, or remove them.

STUDENTS
  Raise hand (header) - the teacher sees it at once.
  Answer polls in the Class tab. Export > All pages as PDF keeps the notes.
"""


class WhiteboardApp(ttk.Frame):
    def __init__(self, master, net, welcome):
        super().__init__(master)
        self.root = master.winfo_toplevel()
        self.net = net
        self.model = BoardModel()
        self.me = welcome["username"]
        self.role = welcome["role"]
        self.room = welcome["room"]
        self.locked = welcome.get("locked", False)
        self.participants = []
        self.connected = True
        self.rtt = None
        self.votes = {}
        self.follow = None
        self.follow_view = None
        self.settings = dict(P.SETTINGS_DEFAULT)
        self.timer = {"running": False, "duration": 0, "remaining": 0, "by": None}
        self._timer_t0 = time.monotonic()
        self._timer_beeped = True
        self.poll = None
        self.my_answer = None
        self._vp_job = None
        self._vp_last = 0.0
        self.results_win = None
        self.history_win = None
        self.glyphs = sys.platform in ("win32", "darwin")
        self.dm_tabs = {}                 # username -> Text widget
        self.unread = 0

        self.tool = tk.StringVar(value="pen" if self.role != P.ROLE_VIEWER else "hand")
        self.color = PALETTE[5]
        self.width = tk.IntVar(value=4)
        self.fill_shapes = tk.BooleanVar(value=False)
        self.smart_shapes = tk.BooleanVar(value=True)
        self.show_map = tk.BooleanVar(value=True)
        self.show_authors = tk.BooleanVar(value=False)
        self.shape_kind = tk.StringVar(value="rect")
        self.node_shape = tk.StringVar(value="rounded")
        self.edge_style = tk.StringVar(value="arrow")
        self.zoom_text = tk.StringVar(value="100%")
        self.page_text = tk.StringVar(value="Page 1 / 1")
        self.status_text = tk.StringVar()
        self.hover_text = tk.StringVar()
        self.students_draw = tk.BooleanVar(value=not self.locked)
        self.own_only = tk.BooleanVar(value=False)
        self.paper_var = tk.StringVar(value="Grid")
        self._flash_job = None

        self.media = MediaClient(net)
        self.images = ImageCache(self.media)
        self.recorder = Recorder()
        self.player = Player()
        self.cards = {}
        self.transfers = {}
        self.board_uploads = {}
        self._rec_job = None

        self._build()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(15, self._pump)
        self.after(250, self._timer_tick)

    # ================================================================== layout
    def _build(self):
        self.pack(fill="both", expand=True)
        self._build_header()
        self._build_statusbar()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self._build_rail(body)
        side = self._build_side(body)
        side.pack(side="right", fill="y")
        tk.Frame(body, width=1, background=C["border"]).pack(side="right", fill="y")
        mid = ttk.Frame(body)
        mid.pack(side="left", fill="both", expand=True)
        self.board = BoardCanvas(mid, self)
        self.board.pack(fill="both", expand=True)
        self.board.minimap = Minimap(self.board)
        self._toggle_map()
        self.banner = tk.Label(self.board, background=C["accent"], foreground="white",
                               font=(FONT, 10, "bold"), padx=14, pady=5)
        self.tool.trace_add("write", lambda *_: self._tool_changed())
        self._tool_changed()

    def _hsep(self, parent):
        tk.Frame(parent, width=1, height=26, background=C["border"]).pack(side="left", padx=8)

    def _build_header(self):
        bar = tk.Frame(self, background=C["surface"], height=54)
        bar.pack(fill="x")
        tk.Frame(self, height=1, background=C["border"]).pack(fill="x")
        inner = tk.Frame(bar, background=C["surface"])
        inner.pack(fill="x", padx=10, pady=8)
        # The right-hand group is packed FIRST so it always keeps its space; on a
        # narrow screen the left group (page / zoom controls) gives way instead.
        hr = self.header_right = tk.Frame(inner, background=C["surface"])
        hr.pack(side="right")
        logo = tk.Canvas(inner, width=28, height=28, background=C["surface"],
                         highlightthickness=0)
        logo.create_rectangle(2, 2, 26, 26, fill=C["accent"], outline="")
        logo.create_line(8, 19, 13, 9, 19, 17, fill="white", width=2.5, capstyle="round",
                         joinstyle="round")
        logo.pack(side="left")
        ttk.Label(inner, text="ClassBoard", style="Brand.TLabel").pack(side="left", padx=(6, 10))
        self.room_chip = tk.Label(inner, background=C["accent_soft"], foreground=C["accent_dark"],
                                  font=(FONT, 10, "bold"), padx=10, pady=3, cursor="hand2")
        self.room_chip.pack(side="left")
        self.room_chip.bind("<Button-1>", lambda e: self._copy_room())
        Tooltip(self.room_chip, "Room code - click to copy. Students type this to join.")
        self.role_chip = tk.Label(inner, background=C["hover"], foreground=C["text"],
                                  font=(FONT, 9), padx=8, pady=3)
        self.role_chip.pack(side="left", padx=(6, 0))
        self._hsep(inner)

        self.prev_btn = ttk.Button(inner, text="‹", width=3, style="Ghost.TButton",
                                   command=lambda: self.go_page(-1, relative=True))
        self.prev_btn.pack(side="left")
        Tooltip(self.prev_btn, "Previous page (PageUp)")
        ttk.Label(inner, textvariable=self.page_text, style="Surface.TLabel", width=12,
                  anchor="center").pack(side="left")
        self.next_btn = ttk.Button(inner, text="›", width=3, style="Ghost.TButton",
                                   command=lambda: self.go_page(1, relative=True))
        self.next_btn.pack(side="left")
        Tooltip(self.next_btn, "Next page (PageDown)")
        self.add_page_btn = ttk.Button(inner, text="+ Page", style="Ghost.TButton",
                                       command=self.add_page)
        self.add_page_btn.pack(side="left", padx=(2, 0))
        Tooltip(self.add_page_btn, "Teacher: add a new page to the lesson")
        self.sep_pages = tk.Frame(inner, width=1, height=26, background=C["border"])
        self.sep_pages.pack(side="left", padx=8)

        self.zoom_btns = []
        for txt, cmd, tip in (("−", lambda: self._zoom(1 / 1.25), "Zoom out (Ctrl+-)"),
                              (None, None, None),
                              ("+", lambda: self._zoom(1.25), "Zoom in (Ctrl+=)"),
                              ("Fit", lambda: self.board.fit_content(),
                               "Show everything (Ctrl+0)")):
            if txt is None:
                ttk.Label(inner, textvariable=self.zoom_text, style="Surface.TLabel", width=5,
                          anchor="center").pack(side="left")
                continue
            b = ttk.Button(inner, text=txt, width=3 if len(txt) == 1 else 4,
                           style="Ghost.TButton", command=cmd)
            b.pack(side="left")
            Tooltip(b, tip)
            self.zoom_btns.append(b)

        # right side
        self.more_btn = ttk.Menubutton(hr, text="More", style="Ghost.TButton")
        self.more_btn.pack(side="right")
        self.more_menu = tk.Menu(self.more_btn, tearoff=0)
        self.more_btn["menu"] = self.more_menu
        self.more_menu.add_checkbutton(label="Show authors on every object",
                                       variable=self.show_authors,
                                       command=self.board_authors_changed)
        self.more_menu.add_checkbutton(label="Show minimap", variable=self.show_map,
                                       command=self._toggle_map)
        self.more_menu.add_checkbutton(label="Smart shapes (pen)", variable=self.smart_shapes)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Version history...", command=self.show_history)
        self.more_menu.add_command(label="Save board file...  (Ctrl+S)",
                                   command=self.save_board_file)
        self.more_menu.add_command(label="Open board file... (teacher)",
                                   command=self.open_board_file)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Keyboard shortcuts and help  (F1)",
                                   command=self.show_help)
        self.more_menu.add_command(label="Leave", command=self.quit_app)

        self.export_btn = ttk.Menubutton(hr, text="Export", style="Ghost.TButton")
        self.export_btn.pack(side="right", padx=(0, 4))
        em = tk.Menu(self.export_btn, tearoff=0)
        self.export_btn["menu"] = em
        em.add_command(label="This page as PNG...  (Ctrl+E)", command=lambda: self.export("page"))
        em.add_command(label="Whole board as PNG...", command=lambda: self.export("board"))
        em.add_command(label="All pages as PDF (class notes)...",
                       command=lambda: self.export("pdf"))
        em.add_command(label="Whole board as SVG (vector)...", command=lambda: self.export("svg"))

        self.hand_btn = ttk.Button(hr, text="Raise hand", command=self.toggle_hand)
        Tooltip(self.hand_btn, "Let the teacher know you have a question")
        self.draw_chk = ttk.Checkbutton(hr, text="Students can draw", style="Bg.TCheckbutton",
                                        variable=self.students_draw, command=self.toggle_lock)
        Tooltip(self.draw_chk, "Untick to make the board read-only for students")
        self.present_btn = ttk.Button(hr, text="Present", style="Accent.TButton",
                                      command=self.toggle_present)
        Tooltip(self.present_btn, "Follow me: every student's view follows your page, "
                                  "pan and zoom")

    def _build_rail(self, parent):
        rail = tk.Frame(parent, background=C["surface"], width=130)
        rail.pack(side="left", fill="y")
        tk.Frame(parent, width=1, background=C["border"]).pack(side="left", fill="y")
        grid = tk.Frame(rail, background=C["surface"])
        grid.pack(padx=6, pady=(8, 4))
        self.tool_buttons = {}
        menus = {"shape": self._shape_menu, "node": self._node_menu, "connect": self._edge_menu}
        for i, (value, label, key, icon, tip, variants) in enumerate(TOOLS):
            b = IconButton(grid, icon, label, lambda v=value: self.set_tool(v),
                           tooltip=f"{label}  ({key})\n{tip}",
                           menu_cb=menus.get(value) if variants else None)
            b.grid(row=i // 2, column=i % 2, padx=1, pady=1)
            self.tool_buttons[value] = b
        self.image_btn = IconButton(grid, "image", "Image", self.insert_image,
                                    tooltip="Insert a picture  (I)\nOr paste one with Ctrl+V")
        self.image_btn.grid(row=len(TOOLS) // 2, column=len(TOOLS) % 2, padx=1, pady=1)
        ur = tk.Frame(rail, background=C["surface"])
        ur.pack(fill="x", padx=10, pady=(4, 0))
        self.undo_btn = ttk.Button(ur, text="Undo", width=5, command=self.undo)
        self.undo_btn.pack(side="left", fill="x", expand=True)
        Tooltip(self.undo_btn, "Undo your last action (Ctrl+Z) - never someone else's")
        self.redo_btn = ttk.Button(ur, text="Redo", width=5, command=self.redo)
        self.redo_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        Tooltip(self.redo_btn, "Redo (Ctrl+Y)")

        tk.Frame(rail, height=1, background=C["border"]).pack(fill="x", padx=10, pady=6)
        tk.Label(rail, text="Colour", background=C["surface"], foreground=C["muted"],
                 font=(FONT, 8, "bold")).pack(anchor="w", padx=12)
        sw = tk.Frame(rail, background=C["surface"])
        sw.pack(padx=8, pady=(3, 4))
        self.swatches = {}
        for i, c in enumerate(PALETTE):
            cv = tk.Canvas(sw, width=21, height=21, background=C["surface"],
                           highlightthickness=0, cursor="hand2")
            cv.grid(row=i // 5, column=i % 5, padx=1, pady=1)
            cv.bind("<Button-1>", lambda e, c=c: self.set_color(c))
            self.swatches[c] = cv
        self.custom_btn = ttk.Button(rail, text="Custom...", command=self.pick_color)
        self.custom_btn.pack(fill="x", padx=12)
        self._draw_swatches()

        tk.Label(rail, text="Size", background=C["surface"], foreground=C["muted"],
                 font=(FONT, 8, "bold")).pack(anchor="w", padx=12, pady=(8, 0))
        row = tk.Frame(rail, background=C["surface"])
        row.pack(fill="x", padx=10)
        self.size_scale = ttk.Scale(row, from_=1, to=20, orient="horizontal",
                                    command=lambda v: self._size_changed(v))
        self.size_scale.set(self.width.get())
        self.size_scale.pack(side="left", fill="x", expand=True)
        self.size_label = tk.Label(row, text="4", width=2, background=C["surface"],
                                   foreground=C["text"], font=(FONT, 9))
        self.size_label.pack(side="left")
        self.size_scale.bind("<ButtonRelease-1>", lambda e: self._apply_style("width"))
        self.fill_chk = ttk.Checkbutton(rail, text="Fill shapes", variable=self.fill_shapes,
                                        command=lambda: self._apply_style("fill"))
        self.fill_chk.pack(anchor="w", padx=10, pady=(6, 0))
        Tooltip(self.fill_chk, "Fill new shapes and nodes with a light tint of the colour. "
                               "With objects selected, changes them too.")
        ttk.Checkbutton(rail, text="Smart shapes", variable=self.smart_shapes).pack(
            anchor="w", padx=10)

    def _draw_swatches(self):
        for c, cv in self.swatches.items():
            cv.delete("all")
            if c == self.color:
                cv.create_oval(1, 1, 20, 20, outline=C["accent"], width=2)
            cv.create_oval(4, 4, 17, 17, fill=c, outline=C["border"] if c == "#ffffff" else c)

    def _build_side(self, parent):
        side = ttk.Frame(parent, width=330)
        side.pack_propagate(False)
        self.tabs = ttk.Notebook(side)
        self.tabs.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        self.class_tab = ScrollFrame(self.tabs)
        self.people_tab = ttk.Frame(self.tabs, padding=8)
        self.chat_tab = ttk.Frame(self.tabs, padding=(8, 8, 8, 6))
        self.tabs.add(self.class_tab, text="Class")
        self.tabs.add(self.people_tab, text="People")
        self.tabs.add(self.chat_tab, text="Chat")
        self._build_class_tab(self.class_tab.inner)
        self._build_people_tab(self.people_tab)
        self._build_chat_tab(self.chat_tab)
        return side

    # ------------------------------------------------------------------ Class tab
    def _build_class_tab(self, parent):
        pad = dict(fill="x", padx=8, pady=(8, 0))
        self.online_card = Card(parent, "In class")
        self.online_card.pack(**pad)
        self.online_title = self.online_card.head.winfo_children()[0]
        self.online_body = tk.Frame(self.online_card.body, background=C["surface"])
        self.online_body.pack(fill="x")
        tc = Card(parent, "Session timer")
        tc.pack(**pad)
        trow = tk.Frame(tc.body, background=C["surface"])
        trow.pack(fill="x")
        self.timer_label = tk.Label(trow, text="--:--", background=C["surface"],
                                    foreground=C["faint"], font=(FONT, 24, "bold"))
        self.timer_label.pack(side="left")
        self.timer_info = tk.Label(trow, text="no timer", background=C["surface"],
                                   foreground=C["muted"], font=(FONT, 9))
        self.timer_info.pack(side="left", padx=(10, 0))
        self.timer_frame = tk.Frame(tc.body, background=C["surface"])
        self.timer_frame.pack(fill="x", pady=(6, 0))
        self.timer_start = ttk.Button(self.timer_frame, text="Start...",
                                      command=self.timer_start_dialog)
        self.timer_pause = ttk.Button(self.timer_frame, text="Pause",
                                      command=self.timer_pause_resume)
        self.timer_reset = ttk.Button(self.timer_frame, text="Reset",
                                      command=lambda: self._timer("reset"))
        for i, b in enumerate((self.timer_start, self.timer_pause, self.timer_reset)):
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 4, 0))
            self.timer_frame.columnconfigure(i, weight=1)

        self.poll_card = Card(parent, "Quick poll")
        self.poll_card.pack(**pad)
        self.poll_body = tk.Frame(self.poll_card.body, background=C["surface"])
        self.poll_body.pack(fill="x")

        hc = Card(parent, "Raised hands")
        hc.pack(**pad)
        self.hands_body = tk.Frame(hc.body, background=C["surface"])
        self.hands_body.pack(fill="x")
        self.lower_all_btn = ttk.Button(hc.head, text="Lower all", style="Ghost.TButton",
                                        command=lambda: self.net.send(
                                            {"type": P.ADMIN, "action": "lower_all_hands"}))
        self.lower_all_btn.pack(side="right")

        vc = Card(parent, "Votes")
        vc.pack(**pad)
        self.votes_label = tk.Label(vc.body, background=C["surface"], foreground=C["muted"],
                                    font=(FONT, 9), justify="left", anchor="w")
        self.votes_label.pack(fill="x")
        vrow = tk.Frame(vc.body, background=C["surface"])
        vrow.pack(fill="x", pady=(6, 0))
        ttk.Button(vrow, text="Results", command=self.show_results).pack(side="left")
        self.votes_clear_btn = ttk.Button(vrow, text="Reset votes", command=self.clear_votes)
        self.votes_clear_btn.pack(side="left", padx=(4, 0))

        bc = Card(parent, "Board")
        bc.pack(**pad)
        self.teacher_board = tk.Frame(bc.body, background=C["surface"])
        self.teacher_board.pack(fill="x")
        r1 = tk.Frame(self.teacher_board, background=C["surface"])
        r1.pack(fill="x")
        tk.Label(r1, text="Paper", background=C["surface"], foreground=C["muted"],
                 font=(FONT, 9)).pack(side="left")
        self.paper_box = ttk.Combobox(r1, state="readonly", width=16, textvariable=self.paper_var,
                                      values=[n for _, n in PAPER_NAMES])
        self.paper_box.pack(side="left", padx=(8, 0))
        self.paper_box.bind("<<ComboboxSelected>>", lambda e: self._paper_changed())
        self.own_chk = ttk.Checkbutton(self.teacher_board, text="Own work only",
                                       variable=self.own_only, command=self._own_changed)
        self.own_chk.pack(anchor="w", pady=(6, 0))
        Tooltip(self.own_chk, "Students may move, edit or erase only what they created "
                              "(the teacher can still change everything)")
        g = tk.Frame(self.teacher_board, background=C["surface"])
        g.pack(fill="x", pady=(6, 0))
        self.teacher_btns = []
        for i, (txt, cmd) in enumerate((("Save on server", self.save_board),
                                        ("Load from server",
                                         lambda: self.net.send({"type": P.LIST_SAVES})),
                                        ("Open board file", self.open_board_file),
                                        ("Clear board", self.clear_board))):
            b = ttk.Button(g, text=txt, command=cmd,
                           style="Danger.TButton" if txt == "Clear board" else "TButton")
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 4, 0),
                   pady=2)
            self.teacher_btns.append(b)
        g.columnconfigure(0, weight=1)
        g.columnconfigure(1, weight=1)
        common_row = self.board_common_row = tk.Frame(bc.body, background=C["surface"])
        common_row.pack(fill="x", pady=(4, 0))
        ttk.Button(common_row, text="Version history",
                   command=self.show_history).pack(side="left", fill="x", expand=True)
        ttk.Button(common_row, text="Notes as PDF",
                   command=lambda: self.export("pdf")).pack(side="left", fill="x", expand=True,
                                                            padx=(4, 0))
        self.end_btn = ttk.Button(bc.body, text="End session for everyone",
                                  style="Danger.TButton", command=self.end_session)
        self.end_btn.pack(fill="x", pady=(6, 0))
        tk.Frame(parent, height=10, background=C["bg"]).pack()

    # ------------------------------------------------------------------ People tab
    def _build_people_tab(self, parent):
        ttk.Label(parent, text="Right-click a person for options", style="Muted.TLabel",
                  font=(FONT, 9)).pack(anchor="w")
        self.people = ttk.Treeview(parent, columns=("role", "status"), show="tree headings",
                                   selectmode="browse")
        self.people.heading("#0", text="Name")
        self.people.heading("role", text="Role")
        self.people.heading("status", text="")
        self.people.column("#0", width=130)
        self.people.column("role", width=110)
        self.people.column("status", width=50, anchor="center")
        self.people.pack(fill="both", expand=True, pady=(6, 6))
        self.people.bind("<Button-3>", self._people_menu)
        self.people.bind("<Double-Button-1>", lambda e: self._dm_selected())
        self.people.tag_configure("hand", background="#fff7e6")
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Button(row, text="Private message", command=self._dm_selected).grid(
            row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
        self.admin_btns = []
        for i, (txt, fn) in enumerate((
                ("Remove", lambda: self._admin("kick")),
                ("View only", lambda: self._admin("set_role", P.ROLE_VIEWER)),
                ("Can draw", lambda: self._admin("set_role", P.ROLE_EDITOR)))):
            b = ttk.Button(row, text=txt, command=fn)
            b.grid(row=(i + 1) // 2, column=(i + 1) % 2, sticky="ew",
                   padx=(0, 4) if (i + 1) % 2 == 0 else 0, pady=2)
            self.admin_btns.append(b)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------ Chat tab
    def _build_chat_tab(self, parent):
        self.xfer_label = ttk.Label(parent, style="Muted.TLabel", font=(FONT, 9),
                                    wraplength=290, justify="left")
        self.xfer_label.pack(side="bottom", anchor="w")
        media_row = ttk.Frame(parent)
        media_row.pack(side="bottom", fill="x", pady=(4, 0))
        self.attach_btn = ttk.Button(media_row, text="Attach file", command=self.attach_file)
        self.attach_btn.pack(side="left")
        Tooltip(self.attach_btn, "Send an image, video, audio or any file (max 200 MB)")
        self.rec_btn = ttk.Button(media_row, text="Record voice", command=self.toggle_record)
        self.rec_btn.pack(side="left", padx=(4, 0))
        self.rec_cancel = ttk.Button(media_row, text="Cancel", width=7, command=self.cancel_record)
        self.close_dm_btn = ttk.Button(media_row, text="Close chat", command=self._close_dm)
        entry_row = ttk.Frame(parent)
        entry_row.pack(side="bottom", fill="x")
        self.chat_entry = ttk.Entry(entry_row)
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", lambda e: self.send_chat())
        ttk.Button(entry_row, text="Send", width=6,
                   command=self.send_chat).pack(side="left", padx=(4, 0))
        self.chat_tabs = ttk.Notebook(parent, style="Inner.TNotebook")
        self.chat_tabs.pack(fill="both", expand=True, pady=(0, 6))
        self.chat_tabs.bind("<<NotebookTabChanged>>", lambda e: self._chat_tab_changed())
        self.chat = self._chat_text(self.chat_tabs)
        self.chat_tabs.add(self.chat.master, text="Everyone")

    def _chat_text(self, nb):
        box = ttk.Frame(nb)
        t = tk.Text(box, width=34, height=4, wrap="word", state="disabled", relief="flat",
                    font=(FONT, 10), padx=8, pady=6, background=C["surface"],
                    highlightthickness=0)
        sb = ttk.Scrollbar(box, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        t.tag_configure("time", foreground=C["faint"], font=(FONT, 8))
        t.tag_configure("sys", foreground=C["muted"], font=(FONT, 9, "italic"))
        t.tag_configure("priv", foreground=C["accent"], font=(FONT, 8, "bold"))
        return t

    def _build_statusbar(self):
        bar = tk.Frame(self, background=C["surface"])
        bar.pack(fill="x", side="bottom")
        tk.Frame(self, height=1, background=C["border"]).pack(fill="x", side="bottom")
        self.conn_dot = tk.Canvas(bar, width=12, height=12, background=C["surface"],
                                  highlightthickness=0)
        self.conn_dot.pack(side="left", padx=(10, 0))
        self.conn_label = ttk.Label(bar, style="Status.TLabel")
        self.conn_label.pack(side="left")
        self.seq_label = ttk.Label(bar, style="Status.TLabel")
        self.seq_label.pack(side="left")
        self.me_label = ttk.Label(bar, style="Status.TLabel")
        self.me_label.pack(side="left")
        ttk.Label(bar, textvariable=self.hover_text, style="Status.TLabel").pack(side="right")
        tk.Label(bar, textvariable=self.status_text, background=C["surface"],
                 foreground=C["accent_dark"], font=(FONT, 9, "bold"), anchor="w").pack(
            side="left", fill="x", expand=True, padx=8)

    # ================================================================== keys
    def _bind_keys(self):
        r = self.root
        r.bind_all("<Control-z>", lambda e: self._key(self.undo))
        r.bind_all("<Control-y>", lambda e: self._key(self.redo))
        r.bind_all("<Control-Z>", lambda e: self._key(self.redo))
        r.bind_all("<Control-s>", lambda e: self._key(self.save_board_file))
        r.bind_all("<Control-e>", lambda e: self._key(lambda: self.export("page")))
        r.bind_all("<Control-v>", lambda e: self._key(self.paste, typing_ok=False))
        r.bind_all("<Control-d>", lambda e: self._key(self.board.duplicate_selection))
        r.bind_all("<Control-a>", lambda e: self._key(self.board.select_all, typing_ok=False))
        r.bind_all("<F1>", lambda e: self.show_help())
        r.bind_all("<Prior>", lambda e: self._key(lambda: self.go_page(-1, True), False))
        r.bind_all("<Next>", lambda e: self._key(lambda: self.go_page(1, True), False))
        r.bind_all("<KeyPress-i>", lambda e: self._key(self.insert_image, typing_ok=False))
        for value, _label, key, *_ in TOOLS:
            r.bind_all(f"<KeyPress-{key.lower()}>",
                       lambda e, v=value: self._key(lambda: self.set_tool(v), typing_ok=False))
        r.bind_all("<Control-equal>", lambda e: self._key(lambda: self._zoom(1.25)))
        r.bind_all("<Control-plus>", lambda e: self._key(lambda: self._zoom(1.25)))
        r.bind_all("<Control-minus>", lambda e: self._key(lambda: self._zoom(1 / 1.25)))
        r.bind_all("<Control-Key-0>", lambda e: self._key(self.board.fit_content))
        r.bind_all("<KeyPress-space>", lambda e: self._key(self._space_down, typing_ok=False))
        r.bind_all("<KeyRelease-space>", lambda e: self._space_up())
        b = self.board
        b.bind("<Delete>", lambda e: b.delete_selection())
        b.bind("<BackSpace>", lambda e: b.delete_selection())
        b.bind("<Escape>", lambda e: b.set_selection([]))
        b.bind("<Tab>", lambda e: "break" if b.mindmap_add() else None)
        b.bind("<Return>", lambda e: self._board_enter())

    def _board_enter(self):
        b = self.board
        if len(b.selection) == 1:
            if self.model.strokes[b.selection[0]]["tool"] == "node":
                b.mindmap_add(sibling=True)
            else:
                b.edit_object(b.selection[0])
        return "break"

    def _key(self, fn, typing_ok=True):
        focus = self.root.focus_get()
        typing = isinstance(focus, (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox, ttk.Combobox))
        if typing and (not typing_ok or isinstance(focus, (tk.Entry, ttk.Entry, tk.Text))):
            return None
        fn()
        return "break"

    def _zoom(self, f):
        if self.following():
            self.flash(self.follow_block_reason())
        else:
            self.board.zoom_at(f)

    def _space_down(self):
        if not self.board.space_down:
            self.board.space_down = True
            self.board.configure(cursor="fleur")

    def _space_up(self):
        self.board.space_down = False
        self.board.configure(cursor=self.board._tool_cursor())

    # ================================================================== tools & style
    def set_tool(self, value):
        if value not in VIEW_TOOLS and not self.can_edit():
            self.flash(self.edit_block_reason())
            return
        self.tool.set(value)

    def _tool_changed(self):
        t = self.tool.get()
        for v, b in self.tool_buttons.items():
            b.set_state(selected=(v == t))
        self.board.configure(cursor=self.board._tool_cursor())
        if t != "select" and self.board.selection:
            self.board.set_selection([])

    def _variant_menu(self, btn, var, choices, tool, title):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label=title, state="disabled")
        for value, label in choices:
            m.add_radiobutton(label=label, value=value, variable=var,
                              command=lambda: (self.set_tool(tool), self._variant_label()))
        m.tk_popup(btn.winfo_rootx() + btn.winfo_width(), btn.winfo_rooty())

    def _shape_menu(self, btn):
        self._variant_menu(btn, self.shape_kind, SHAPE_KINDS, "shape", "Shape")

    def _node_menu(self, btn):
        self._variant_menu(btn, self.node_shape, NODE_KINDS, "node", "Node shape")

    def _edge_menu(self, btn):
        self._variant_menu(btn, self.edge_style, EDGE_KINDS, "connect", "Connector style")

    def _variant_label(self):
        names = dict(SHAPE_KINDS)
        self.tool_buttons["shape"].set_label(names[self.shape_kind.get()].split(" ")[0])

    def set_color(self, c):
        self.color = c
        self._draw_swatches()
        self._apply_style("color")

    def pick_color(self):
        res = colorchooser.askcolor(color=self.color, parent=self.root, title="Colour")
        if res and res[1]:
            self.set_color(res[1].lower())

    def _size_changed(self, v):
        n = int(round(float(v)))
        if n != self.width.get():
            self.width.set(n)
        if hasattr(self, "size_label"):
            self.size_label.configure(text=str(n))

    def _apply_style(self, what):
        """Changing colour / size / fill with objects selected restyles them."""
        b = self.board
        if not b.selection or self.tool.get() != "select":
            return
        ch = {"color": {"color": self.color}, "width": {"width": self.width.get()},
              "fill": {"fill": self.fill_color() if self.fill_shapes.get() else None}}[what]
        if b.restyle_selection(**ch):
            self.flash(f"Updated {len(b.selection)} object(s)")

    def fill_color(self):
        """Light tint of the current colour, used to fill shapes and nodes."""
        c = self.color
        if c == "#ffffff":
            return "#ffffff"
        r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        mix = lambda v: int(v + (255 - v) * 0.78)    # noqa: E731
        return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"

    def sticky_fill(self):
        return STICKY_FILLS.get(self.color, "#fff59d")

    def color_for_node(self):
        return self.color if self.color != "#ffffff" else "#475569"

    def board_authors_changed(self):
        self.board._draw_authors()

    # ================================================================== state helpers
    def can_edit(self):
        if not self.connected or self.role == P.ROLE_VIEWER:
            return False
        return not (self.locked and self.role != P.ROLE_HOST)

    def can_laser(self):
        return self.can_edit()

    def edit_block_reason(self):
        if not self.connected:
            return "Offline - waiting to reconnect"
        if self.role == P.ROLE_VIEWER:
            return "You are in view-only mode - ask the teacher if you need to draw"
        return "The teacher has made the board read-only for students"

    def is_teacher(self):
        return self.role == P.ROLE_HOST

    def _refresh_controls(self):
        host = self.is_teacher()
        online = self.connected
        self.room_chip.configure(text=f"Room {self.room}")
        word = ROLE_WORD.get(self.role, self.role)
        self.role_chip.configure(text=word)
        self.me_label.configure(text=f"You: {self.me}")
        # header: teacher controls vs the student's Raise hand
        for w in (self.present_btn, self.draw_chk, self.hand_btn):
            w.pack_forget()
        if host:
            self.present_btn.pack(side="right", padx=(0, 8), before=self.export_btn)
            self.draw_chk.pack(side="right", padx=(0, 10), before=self.present_btn)
            self.add_page_btn.pack(side="left", padx=(2, 0), before=self.sep_pages)
        else:
            self.add_page_btn.pack_forget()
            self.hand_btn.pack(side="right", padx=(0, 8), before=self.export_btn)
        for w in (self.present_btn, self.draw_chk, self.add_page_btn):
            w.configure(state="normal" if host and online else "disabled")
        self.present_btn.configure(text="Stop presenting" if self.follow == self.me
                                   else "Present")
        self.students_draw.set(not self.locked)
        for w in (self.timer_start, self.timer_pause, self.timer_reset,
                  self.votes_clear_btn, self.end_btn, self.own_chk, *self.teacher_btns,
                  *self.admin_btns):
            w.configure(state="normal" if host and online else "disabled")
        self.paper_box.configure(state="readonly" if host and online else "disabled")
        if host:
            self.timer_frame.pack(fill="x", pady=(6, 0))
            self.teacher_board.pack(fill="x", before=self.board_common_row)
        else:
            self.timer_frame.pack_forget()
            self.teacher_board.pack_forget()
        if host:
            self.end_btn.pack(fill="x", pady=(6, 0))
            self.lower_all_btn.pack(side="right")
            self.votes_clear_btn.pack(side="left", padx=(4, 0))
            for b in self.admin_btns:
                b.grid()
        else:
            self.end_btn.pack_forget()
            self.lower_all_btn.pack_forget()
            self.votes_clear_btn.pack_forget()
            for b in self.admin_btns:
                b.grid_remove()
        can = self.can_edit()
        for value, b in self.tool_buttons.items():
            b.set_state(enabled=online and (can or value in VIEW_TOOLS))
        self.image_btn.set_state(enabled=can)
        if not can and self.tool.get() not in VIEW_TOOLS:
            self.tool.set("hand")
        for w in (self.undo_btn, self.redo_btn):
            w.configure(state="normal" if can else "disabled")
        for w in self.zoom_btns + [self.prev_btn, self.next_btn]:
            w.configure(state="disabled" if self.following() else "normal")
        me = next((p for p in self.participants if p["username"] == self.me), None)
        self.hand_btn.configure(text="Lower hand" if me and me.get("hand") else "Raise hand")
        self._refresh_banner()
        self._refresh_votes_label()
        self._refresh_hands()
        self._render_poll()
        self._refresh_conn()
        self.update_page_label()

    def _refresh_conn(self):
        online = sum(1 for p in self.participants if p["status"] == "online")
        self.conn_dot.delete("all")
        col = C["ok"] if self.connected else C["danger"]
        self.conn_dot.create_oval(2, 2, 10, 10, fill=col, outline="")
        if self.connected:
            rtt = f"  {self.rtt:.0f} ms" if self.rtt is not None else ""
            tls = "  TLS" if self.net.tls else ""
            self.conn_label.configure(text=f"Connected{rtt}{tls}   |   {online} online")
        else:
            self.conn_label.configure(text="Reconnecting...")
        self.seq_label.configure(text=f"seq {self.model.seq}")

    def flash(self, text, ms=5000):
        self.status_text.set(text)
        if self._flash_job:
            self.after_cancel(self._flash_job)
        self._flash_job = self.after(ms, lambda: self.status_text.set(""))

    def set_hover(self, text):
        if self.hover_text.get() != text:
            self.hover_text.set(text)

    def on_selection_changed(self, sids):
        if not sids:
            self.set_hover("")
        elif len(sids) == 1:
            self.set_hover(self.board.describe(sids[0]))
        elif sids:
            self.set_hover(f"{len(sids)} objects selected  -  Del deletes, Ctrl+D duplicates")

    def _tab_changed(self):
        if self.tabs.index("current") == 2:
            self.unread = 0
            self.tabs.tab(2, text="Chat")

    # ================================================================== pages & view
    def update_zoom_label(self):
        self.zoom_text.set(f"{self.board.scale * 100:.0f}%")
        self.update_page_label()

    def update_page_label(self):
        n = self.settings.get("pages", 1)
        k = self.board.current_page()
        self.page_text.set(f"Page {k + 1} / {n}" if 0 <= k < n else f"{n} page(s)")

    def go_page(self, i, relative=False):
        if self.following():
            self.flash(self.follow_block_reason())
            return
        n = self.settings.get("pages", 1)
        if relative:
            i = max(0, min(n - 1, self.board.current_page() + i))
        self.board.go_page(max(0, min(n - 1, i)))

    def add_page(self):
        n = self.settings.get("pages", 1)
        if n >= P.MAX_PAGES:
            self.flash(f"A lesson can have at most {P.MAX_PAGES} pages")
            return
        self._goto_after_pages = n
        self.net.send({"type": P.SETTINGS, "key": "pages", "value": n + 1})

    def _toggle_map(self):
        mm = self.board.minimap
        if self.show_map.get():
            mm.place(relx=1.0, rely=1.0, x=-12, y=-12, anchor="se")
            mm.after(50, mm.redraw)
        else:
            mm.place_forget()

    def on_view_changed(self):
        if self.follow != self.me or not self.connected:
            return
        wait = 0.08 - (time.monotonic() - self._vp_last)
        if wait <= 0:
            self._send_viewport()
        elif self._vp_job is None:
            self._vp_job = self.after(int(wait * 1000) + 1, self._send_viewport)

    def _send_viewport(self):
        self._vp_job = None
        self._vp_last = time.monotonic()
        x, y, w, h = self.board.view_rect()
        self.net.send({"type": P.VIEWPORT, "x": round(x, 1), "y": round(y, 1),
                       "w": round(w, 1), "h": round(h, 1)})

    # ================================================================== presentation mode
    def following(self):
        return bool(self.follow) and self.follow != self.me

    def follow_block_reason(self):
        return f"{self.follow} is presenting - your view follows theirs"

    def toggle_present(self):
        self.net.send({"type": P.FOLLOW, "on": self.follow != self.me})

    def _set_follow(self, name):
        old, self.follow = self.follow, name
        if name == self.me:
            self.chat_system("You are presenting - every student's view follows yours")
            self._send_viewport()
        elif name:
            self.chat_system(f"{name} is presenting - your view follows theirs")
            self.board.pan_anchor = None
            if self.follow_view:
                self.board.set_view(*self.follow_view)
        elif old:
            self.follow_view = None
            self.chat_system(f"{old} stopped presenting")

    def _refresh_banner(self):
        if self.follow == self.me:
            self.banner.configure(text="You are presenting - students follow your view",
                                  background=C["danger"])
            self.banner.place(relx=0.5, y=10, anchor="n")
        elif self.follow:
            self.banner.configure(text=f"Following {self.follow}'s view", background=C["accent"])
            self.banner.place(relx=0.5, y=10, anchor="n")
        else:
            self.banner.place_forget()

    # ================================================================== actions
    def undo(self):
        if self.can_edit():
            self.net.send_board_op(P.UNDO)

    def redo(self):
        if self.can_edit():
            self.net.send_board_op(P.REDO)

    def clear_board(self):
        if messagebox.askyesno("Clear board", "Clear the whole board for everyone?\n"
                               "(You can undo this.)", parent=self.root):
            self.net.send_board_op(P.CLEAR)

    def toggle_lock(self):
        self.net.send({"type": P.ADMIN, "action": "unlock" if self.students_draw.get()
                       else "lock"})

    def _paper_changed(self):
        value = {n: v for v, n in PAPER_NAMES}[self.paper_var.get()]
        self.net.send({"type": P.SETTINGS, "key": "paper", "value": value})

    def _own_changed(self):
        self.net.send({"type": P.SETTINGS, "key": "own_only", "value": self.own_only.get()})

    def end_session(self):
        if messagebox.askyesno("End session", "End the session for everyone?\nAll students "
                               "will be disconnected and the room is closed.\n\nTip: save the "
                               "board or export the notes first.", parent=self.root):
            self.net.send({"type": P.CLOSE_ROOM})

    def _admin(self, action, role=None, target=None):
        if target is None:
            sel = self.people.selection()
            if not sel:
                self.flash("Select a person first")
                return
            target = sel[0]
        if target == self.me:
            self.flash("That is you")
            return
        if action == "kick":
            if not messagebox.askyesno("Remove", f"Remove {target} from the class?\nThey will "
                                       "not be able to rejoin this room.", parent=self.root):
                return
            self.net.send({"type": P.ADMIN, "action": "kick", "target": target})
        else:
            if role == P.ROLE_HOST and not messagebox.askyesno(
                    "Hand over", f"Make {target} the teacher? You become a student.",
                    parent=self.root):
                return
            self.net.send({"type": P.ADMIN, "action": "set_role", "target": target,
                           "role": role})

    def _people_menu(self, e):
        row = self.people.identify_row(e.y)
        if not row:
            return
        self.people.selection_set(row)
        m = tk.Menu(self, tearoff=0)
        if row != self.me:
            m.add_command(label="Send private message", command=lambda: self.open_dm(row))
        p = next((x for x in self.participants if x["username"] == row), {})
        if self.is_teacher() and row != self.me:
            m.add_separator()
            if p.get("hand"):
                m.add_command(label="Lower hand", command=lambda: self.net.send(
                    {"type": P.HAND, "target": row, "up": False}))
            if p.get("role") == P.ROLE_VIEWER:
                m.add_command(label="Allow drawing", command=lambda: self._admin(
                    "set_role", P.ROLE_EDITOR, row))
            else:
                m.add_command(label="Make view-only", command=lambda: self._admin(
                    "set_role", P.ROLE_VIEWER, row))
            m.add_command(label="Make teacher (hand over)", command=lambda: self._admin(
                "set_role", P.ROLE_HOST, row))
            m.add_command(label="Remove from class...", command=lambda: self._admin(
                "kick", target=row))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def save_board(self):
        name = simpledialog.askstring("Save on server", "Name for this version:",
                                      initialvalue=time.strftime("lesson-%d%b-%H%M"),
                                      parent=self.root)
        if name:
            self.net.send({"type": P.SAVE, "name": name.strip().replace(" ", "_")[:40]})

    def _show_saves(self, saves):
        if not saves:
            messagebox.showinfo("Load board", "No saved boards on the server yet.",
                                parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("Load a saved board")
        win.transient(self.root)
        ttk.Label(win, text="Saved versions (this room first). Loading replaces the board "
                            "for everyone.", wraplength=380, padding=10).pack(anchor="w")
        lb = tk.Listbox(win, width=52, height=min(12, len(saves)), font=(FONT, 10))
        lb.pack(fill="both", expand=True, padx=10)
        for s in saves:
            lb.insert("end", f"{s['name']}   ({s['saved']}, room {s['room']})")
        lb.selection_set(0)

        def do_load():
            sel = lb.curselection()
            if sel:
                self.net.send({"type": P.LOAD, "name": saves[sel[0]]["name"]})
            win.destroy()
        ttk.Button(win, text="Load", style="Accent.TButton", command=do_load).pack(pady=10)
        lb.bind("<Double-Button-1>", lambda e: do_load())

    # ------------------------------------------------------------------ files & export
    def save_board_file(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json",
                                            initialfile=f"classboard-{self.room}.board.json",
                                            filetypes=[("ClassBoard board", "*.json")])
        if path:
            n = export.save_board_file(self.model, path, self.room, self.settings)
            self.flash(f"Saved {n} objects to {os.path.basename(path)}")

    def open_board_file(self):
        if not self.is_teacher():
            self.flash("Only the teacher can open a board file for the class")
            return
        path = filedialog.askopenfilename(parent=self.root, title="Open board file",
                                          filetypes=[("ClassBoard board", "*.json"),
                                                     ("All files", "*.*")])
        if not path:
            return
        try:
            data = export.load_board_file(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open board file", f"Cannot open this file:\n{exc}",
                                 parent=self.root)
            return
        if not messagebox.askyesno("Open board file", f"Replace the board for everyone with "
                                   f"the {len(data['strokes'])} objects in this file?",
                                   parent=self.root):
            return
        self.net.send({"type": P.RESTORE, "strokes": data["strokes"]})
        for k in ("paper", "pages"):
            v = data.get("settings", {}).get(k)
            if v is not None and v != self.settings.get(k):
                self.net.send({"type": P.SETTINGS, "key": k, "value": v})

    def export(self, what):
        if not export.HAVE_PIL and what in ("page", "board", "pdf"):
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".eps",
                                                initialfile=f"classboard-{self.room}.eps",
                                                filetypes=[("PostScript", "*.eps")])
            if path:
                export.export_postscript(self.board, path)
                self.flash("Exported the visible board (install Pillow for PNG and PDF)")
            return
        ext = {"page": ".png", "board": ".png", "pdf": ".pdf", "svg": ".svg"}[what]
        default = {"page": f"page{self.board.current_page() + 1}", "board": "board",
                   "pdf": "notes", "svg": "board"}[what]
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=ext,
                                            initialfile=f"classboard-{self.room}-{default}{ext}",
                                            filetypes=[(ext[1:].upper(), "*" + ext)])
        if path:
            self.export_to(what, path)

    def export_to(self, what, path):
        paper, pages = self.settings.get("paper"), self.settings.get("pages", 1)
        try:
            if what == "page":
                k = max(0, min(pages - 1, self.board.current_page()))
                x, y, w, h = G.page_rect(k)
                export.render_png(self.model, path, 1.25, self.media.cached_path, self.votes,
                                  box=(x, y, x + w, y + h), paper=paper, pages=pages)
            elif what == "board":
                export.render_png(self.model, path, 1.0, self.media.cached_path, self.votes)
            elif what == "pdf":
                n = export.render_pdf(self.model, path, pages, self.media.cached_path, paper)
                self.flash(f"Saved {n} page(s) as PDF: {os.path.basename(path)}")
                return
            else:
                export.render_svg(self.model, path, self.votes)
        except Exception as exc:                        # disk full, permissions...
            messagebox.showerror("Export", str(exc), parent=self.root)
            return
        self.flash(f"Exported {os.path.basename(path)}")

    def paste(self):
        """Ctrl+V: paste a picture (or picture file) onto the board, or text as a text box."""
        if not self.can_edit():
            self.flash(self.edit_block_reason())
            return
        try:
            from PIL import ImageGrab
            clip = ImageGrab.grabclipboard()
        except Exception:
            clip = None
        if isinstance(clip, list):
            files = [f for f in clip if str(f).lower().endswith((".png", ".jpg", ".jpeg", ".gif",
                                                                 ".bmp", ".webp"))]
            if files:
                self._upload_board_image(files[0])
                return
        elif clip is not None and hasattr(clip, "save"):
            path = os.path.join(tempfile.gettempdir(), f"classboard-paste-{int(time.time())}.png")
            clip.save(path, "PNG")
            self._upload_board_image(path)
            return
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if text:
            x, y = self.board.view_center()
            st = {"id": self.net.new_stroke_id(), "tool": "text",
                  "color": self.color if self.color != "#ffffff" else "#1f2933",
                  "width": self.width.get(), "points": [x - 200, y - 20],
                  "text": text[:P.MAX_TEXT_LEN]}
            self.board._send_stroke(st)
            self.flash("Pasted text")
        else:
            self.flash("Nothing to paste (copy a picture or some text first)")

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("ClassBoard help")
        win.transient(self.root)
        t = tk.Text(win, width=86, height=34, font=("Courier", 10), relief="flat", padx=14,
                    pady=12, background=C["surface"])
        t.insert("1.0", HELP)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=8)

    def _copy_room(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.room)
        self.flash(f"Room code {self.room} copied")

    def quit_app(self):
        try:
            self.player.stop()
            if self._rec_job:
                self.recorder.cancel()
        except Exception:
            pass
        try:
            self.net.close()
        finally:
            self.root.destroy()

    # ================================================================== hands
    def toggle_hand(self):
        me = next((p for p in self.participants if p["username"] == self.me), {})
        self.net.send({"type": P.HAND, "up": not me.get("hand")})

    def _refresh_hands(self):
        for w in self.hands_body.winfo_children():
            w.destroy()
        up = [p for p in self.participants if p.get("hand")]
        if not up:
            tk.Label(self.hands_body, text="No hands raised", background=C["surface"],
                     foreground=C["faint"], font=(FONT, 9)).pack(anchor="w")
        for p in up:
            row = tk.Frame(self.hands_body, background="#fff7e6")
            row.pack(fill="x", pady=1)
            tk.Label(row, text="  " + p["username"], background="#fff7e6", foreground=p["color"],
                     font=(FONT, 10, "bold")).pack(side="left", pady=3)
            if self.is_teacher():
                ttk.Button(row, text="Lower", style="Ghost.TButton",
                           command=lambda u=p["username"]: self.net.send(
                               {"type": P.HAND, "target": u, "up": False})).pack(side="right")
                ttk.Button(row, text="Message", style="Ghost.TButton",
                           command=lambda u=p["username"]: self.open_dm(u)).pack(side="right")
        self.lower_all_btn.configure(state="normal" if self.is_teacher() and up else "disabled")

    # ================================================================== polls
    def new_poll(self):
        win = tk.Toplevel(self.root)
        win.title("New quick poll")
        win.transient(self.root)
        win.configure(background=C["surface"])
        f = tk.Frame(win, background=C["surface"], padx=16, pady=14)
        f.pack(fill="both", expand=True)
        tk.Label(f, text="Question", background=C["surface"], font=(FONT, 10, "bold")).pack(
            anchor="w")
        q = ttk.Entry(f, width=48)
        q.pack(fill="x", pady=(2, 10))
        tk.Label(f, text="Options (one per line, 2-6)", background=C["surface"],
                 font=(FONT, 10, "bold")).pack(anchor="w")
        opts = tk.Text(f, width=48, height=6, font=(FONT, 10), relief="solid", bd=1)
        opts.insert("1.0", "Yes\nNo\nNot sure")
        opts.pack(fill="x", pady=(2, 10))
        quick = tk.Frame(f, background=C["surface"])
        quick.pack(fill="x")
        tk.Label(quick, text="Quick:", background=C["surface"], foreground=C["muted"]).pack(
            side="left", padx=(0, 6))
        for label, text in (("A-D", "A\nB\nC\nD"), ("True / False", "True\nFalse"),
                            ("1-5", "1\n2\n3\n4\n5")):
            ttk.Button(quick, text=label, command=lambda t=text: (
                opts.delete("1.0", "end"), opts.insert("1.0", t))).pack(side="left",
                                                                         padx=(0, 4))

        def start():
            options = [o.strip() for o in opts.get("1.0", "end").splitlines() if o.strip()]
            if not q.get().strip() or not 2 <= len(options) <= P.MAX_POLL_OPTIONS:
                messagebox.showerror("Poll", "Enter a question and 2-6 options.", parent=win)
                return
            self.net.send({"type": P.POLL, "action": "start", "question": q.get().strip()[:200],
                           "options": [o[:80] for o in options]})
            win.destroy()
        ttk.Button(f, text="Start poll", style="Accent.TButton", command=start).pack(
            anchor="e", pady=(12, 0))
        q.focus_set()

    def _render_poll(self):
        body = self.poll_body
        for w in body.winfo_children():
            w.destroy()
        p = self.poll
        host = self.is_teacher()
        if p is None:
            tk.Label(body, text="Ask the class a quick question" if host else
                     "No poll right now", background=C["surface"],
                     foreground=C["faint"], font=(FONT, 9)).pack(anchor="w")
            if host:
                ttk.Button(body, text="New poll...", style="Accent.TButton",
                           command=self.new_poll).pack(anchor="w", pady=(6, 0))
            return
        tk.Label(body, text=p["question"], background=C["surface"], foreground=C["text"],
                 font=(FONT, 11, "bold"), wraplength=270, justify="left").pack(anchor="w")
        total = max(1, p["total"])
        show_results = host or not p["open"] or self.my_answer is not None
        answers = p.get("answers", {})
        for i, opt in enumerate(p["options"]):
            row = tk.Frame(body, background=C["surface"])
            row.pack(fill="x", pady=(5, 0))
            mine = self.my_answer == i
            if p["open"] and not host:
                b = tk.Button(row, text=("> " if mine else "") + opt, anchor="w", relief="flat",
                              cursor="hand2", padx=8, pady=3,
                              background=C["accent_soft"] if mine else C["hover"],
                              activebackground=C["accent_soft"], foreground=C["text"],
                              font=(FONT, 10, "bold" if mine else "normal"),
                              command=lambda i=i: self._answer(i))
                b.pack(fill="x")
            else:
                tk.Label(row, text=opt + ("  (your answer)" if mine else ""), anchor="w",
                         background=C["surface"],
                         font=(FONT, 10, "bold" if mine else "normal")).pack(fill="x")
            if show_results:
                n = p["counts"][i]
                bar = tk.Frame(row, background=C["surface"])
                bar.pack(fill="x", pady=(2, 0))
                pb = ttk.Progressbar(bar, style="Poll.Horizontal.TProgressbar", maximum=total,
                                     value=n)
                pb.pack(side="left", fill="x", expand=True)
                pct = round(100 * n / total) if p["total"] else 0
                tk.Label(bar, text=f"{n}  ({pct}%)", background=C["surface"],
                         foreground=C["muted"], font=(FONT, 9), width=9, anchor="e").pack(
                    side="left")
                if host and answers:
                    who = [u for u, a in answers.items() if a == i]
                    if who:
                        tk.Label(row, text=", ".join(who), background=C["surface"],
                                 foreground=C["faint"], font=(FONT, 8), wraplength=270,
                                 justify="left").pack(anchor="w")
        online = sum(1 for x in self.participants
                     if x["status"] == "online" and x["role"] != P.ROLE_HOST)
        state = "open" if p["open"] else "closed"
        tk.Label(body, text=f"{p['total']} of {online} answered  -  poll {state}",
                 background=C["surface"], foreground=C["muted"], font=(FONT, 9)).pack(
            anchor="w", pady=(6, 0))
        if not show_results:
            tk.Label(body, text="Results appear after you answer", background=C["surface"],
                     foreground=C["faint"], font=(FONT, 8)).pack(anchor="w")
        if host:
            row = tk.Frame(body, background=C["surface"])
            row.pack(fill="x", pady=(6, 0))
            if p["open"]:
                ttk.Button(row, text="End poll", command=lambda: self.net.send(
                    {"type": P.POLL, "action": "end"})).pack(side="left")
            ttk.Button(row, text="Clear", command=lambda: self.net.send(
                {"type": P.POLL, "action": "clear"})).pack(side="left", padx=(4, 0))
            ttk.Button(row, text="New", command=self.new_poll).pack(side="left", padx=(4, 0))

    def _answer(self, i):
        self.my_answer = i
        self.net.send({"type": P.POLL, "action": "answer", "option": i})
        self._render_poll()

    def _on_poll(self, ev):
        old = self.poll
        self.poll = ev.get("poll")
        action = ev.get("action")
        if self.poll is None or (old and old["id"] != self.poll["id"]):
            self.my_answer = None
        if action == "start":
            self.my_answer = None
            self.chat_system(f"{ev.get('by')} started a poll: {self.poll['question']}")
            if not self.is_teacher():
                self.tabs.select(0)
                self.flash("New poll - answer it in the Class tab", ms=6000)
                self.bell()
        elif action == "end":
            self.chat_system("The poll is closed - results are in the Class tab")
        self._render_poll()

    # ================================================================== voting
    def toggle_vote(self, sid):
        if not self.connected:
            self.flash("Offline - waiting to reconnect")
            return
        self.net.send({"type": P.VOTE, "stroke_id": sid})

    def clear_votes(self):
        if messagebox.askyesno("Reset votes", "Remove everyone's votes?", parent=self.root):
            self.net.send({"type": P.VOTES_CLEAR})

    def _on_votes(self, ev):
        if "all" in ev:
            changed = set(self.votes) | set(ev["all"])
            self.votes = {k: list(v) for k, v in ev["all"].items()}
            if ev.get("by"):
                self.chat_system(f"{ev['by']} reset the votes")
        else:
            sid = ev["stroke_id"]
            changed = {sid}
            if ev["voters"]:
                self.votes[sid] = list(ev["voters"])
            else:
                self.votes.pop(sid, None)
        self.board.rerender_strokes(changed)
        self._refresh_votes_label()
        if self.results_win is not None and self.results_win.winfo_exists():
            self._fill_results()

    def _refresh_votes_label(self):
        used = sum(self.me in v for s, v in self.votes.items() if self.model.is_visible(s))
        top = self._ranking()[:3]
        txt = f"You have used {used} of {P.MAX_VOTES_PER_USER} votes. Vote tool (U) or " \
              "right-click an object."
        if top:
            txt += "\nTop: " + ";  ".join(f"{self._describe(s)} ({n})" for n, s in top)
        self.votes_label.configure(text=txt, wraplength=280)

    def _ranking(self):
        rows = [(len(v), sid) for sid, v in self.votes.items() if self.model.is_visible(sid)]
        index = {s: i for i, s in enumerate(self.model.order)}
        rows.sort(key=lambda r: (-r[0], index.get(r[1], 0)))
        return rows

    def _describe(self, sid):
        s = self.model.placed(sid) if sid in self.model.strokes else {}
        text = (s.get("text") or s.get("label") or "").replace("\n", " ")
        what = {"sticky": "Note", "text": "Text", "node": "Node"}.get(s.get("tool"),
                                                                      s.get("tool", "?").title())
        return f"{what}: {text[:30]}" if text else f"{what} by {s.get('user', '?')}"

    def show_results(self):
        if self.results_win is not None and self.results_win.winfo_exists():
            self.results_win.lift()
            self._fill_results()
            return
        win = self.results_win = tk.Toplevel(self.root)
        win.title("Vote results")
        win.transient(self.root)
        ttk.Label(win, text="Most-voted objects (updates live). Double-click to jump to one.",
                  padding=(10, 8)).pack(anchor="w")
        tv = ttk.Treeview(win, columns=("votes", "who"), show="tree headings", height=10)
        tv.heading("#0", text="Object")
        tv.heading("votes", text="Votes")
        tv.heading("who", text="Voters")
        tv.column("#0", width=260)
        tv.column("votes", width=60, anchor="center")
        tv.column("who", width=200)
        tv.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.results_tree = tv

        def jump(_e=None):
            sel = tv.selection()
            if sel and sel[0] in self.model.strokes:
                x0, y0, x1, y1 = self.model.bounds(sel[0])
                self.board.center_on((x0 + x1) / 2, (y0 + y1) / 2)
                self.tool.set("select")
                self.board.set_selection([sel[0]])
        tv.bind("<Double-Button-1>", jump)
        self._fill_results()

    def _fill_results(self):
        tv = self.results_tree
        tv.delete(*tv.get_children())
        for rank, (n, sid) in enumerate(self._ranking(), 1):
            tv.insert("", "end", iid=sid, text=f"{rank}. {self._describe(sid)}",
                      values=(n, ", ".join(self.votes[sid])))
        if not tv.get_children():
            tv.insert("", "end", text="No votes yet", values=("", ""))

    # ================================================================== session timer
    def _timer(self, action, **kw):
        self.net.send({"type": P.TIMER, "action": action, **kw})

    def timer_start_dialog(self):
        last = self.timer.get("duration") or 300
        mins = simpledialog.askfloat("Session timer", "Countdown length in minutes:",
                                     initialvalue=round(last / 60, 2), minvalue=0.05,
                                     maxvalue=P.MAX_TIMER_SECONDS / 60, parent=self.root)
        if mins:
            self._timer("start", seconds=max(1, int(round(mins * 60))))

    def timer_pause_resume(self):
        t = self.timer
        if t["running"]:
            self._timer("pause")
        elif t.get("remaining", 0) > 0:
            self._timer("resume")

    def _on_timer(self, ev, announce=True):
        self.timer = {"running": bool(ev.get("running")), "duration": ev.get("duration", 0),
                      "remaining": float(ev.get("remaining", 0)), "by": ev.get("by")}
        self._timer_t0 = time.monotonic()
        self._timer_beeped = not self.timer["running"]
        action = ev.get("action")
        if announce and action:
            text = {"start": f"started a {fmt_time(self.timer['duration'])} timer",
                    "pause": "paused the timer", "resume": "resumed the timer",
                    "reset": "reset the timer"}.get(action)
            if text:
                self.chat_system(f"{ev.get('by', 'teacher')} {text}")
        self._timer_tick(reschedule=False)

    def _timer_left(self):
        t = self.timer
        if not t["running"]:
            return t["remaining"]
        return max(0.0, t["remaining"] - (time.monotonic() - self._timer_t0))

    def _timer_tick(self, reschedule=True):
        t = self.timer
        left = self._timer_left()
        if not t["duration"]:
            self.timer_label.configure(text="--:--", foreground=C["faint"])
            self.timer_info.configure(text="no timer")
            self.timer_pause.configure(text="Pause")
        else:
            color = C["text"]
            if t["running"] and left <= 0:
                color = C["danger"]
            elif t["running"] and left <= 30:
                color = C["warn"]
            elif not t["running"]:
                color = C["faint"]
            self.timer_label.configure(text=fmt_time(left), foreground=color)
            if t["running"] and left > 0:
                info = "running"
            elif t["running"] or t["remaining"] <= 0:
                info = "time's up!"
            else:
                info = "paused" if 0 < t["remaining"] < t["duration"] else "ready"
            self.timer_info.configure(text=info)
            self.timer_pause.configure(text="Pause" if t["running"] and left > 0 else "Resume")
            if t["running"] and left <= 0 and not self._timer_beeped:
                self._timer_beeped = True
                self.bell()
                self.flash("Time's up!", ms=8000)
                self.chat_system("Time's up!")
        if reschedule:
            self.after(250, self._timer_tick)

    # ================================================================== history
    def show_history(self):
        if self.history_win is not None and self.history_win.winfo_exists():
            self.history_win.lift()
            return
        self.history_win = HistoryWindow(self)
        self.net.send({"type": P.HISTORY})

    # ================================================================== chat & private messages
    def send_chat(self):
        text = self.chat_entry.get().strip()
        if not text:
            return
        target = self._current_dm()
        if target:
            self.net.send({"type": P.DM, "to": target, "text": text[:P.MAX_CHAT_LEN]})
        else:
            self.net.send({"type": P.CHAT, "text": text[:P.MAX_CHAT_LEN]})
        self.chat_entry.delete(0, "end")

    def _current_dm(self):
        cur = self.chat_tabs.select()
        for user, t in self.dm_tabs.items():
            if str(t.master) == cur:
                return user
        return None

    def _chat_tab_changed(self):
        user = self._current_dm()
        if user:
            self.close_dm_btn.pack(side="right")
            self.chat_tabs.tab(self.dm_tabs[user].master, text=f"@ {user}")
            self.xfer_label.configure(text="Files and voice messages are shared with "
                                           "everyone (Everyone tab)")
        else:
            self.close_dm_btn.pack_forget()
            self.xfer_label.configure(text="\n".join(self.transfers.values()))

    def _dm_selected(self):
        sel = self.people.selection()
        if sel and sel[0] != self.me:
            self.open_dm(sel[0])

    def open_dm(self, user, focus=True):
        t = self.dm_tabs.get(user)
        if t is None:
            t = self.dm_tabs[user] = self._chat_text(self.chat_tabs)
        self.chat_tabs.add(t.master, text=f"@ {user}")     # re-adding a hidden tab shows it
        if focus:
            self.tabs.select(2)
            self.chat_tabs.select(t.master)
            self.chat_entry.focus_set()
        return t

    def _close_dm(self):
        user = self._current_dm()
        if user:
            self.chat_tabs.hide(self.dm_tabs[user].master)
            self.chat_tabs.select(0)

    def _on_dm(self, m, notify=True):
        other = m["to"] if m["from"] == self.me else m["from"]
        t = self.open_dm(other, focus=False)
        self._append(t, m, private=True)
        if notify and m["from"] != self.me:
            if self._current_dm() != other or self.tabs.index("current") != 2:
                self.chat_tabs.tab(t.master, text=f"* {other}")
                self._bump_unread()
                self.flash(f"Private message from {other}", ms=6000)

    def _bump_unread(self):
        if self.tabs.index("current") != 2:
            self.unread += 1
            self.tabs.tab(2, text=f"Chat ({self.unread})")

    def _append(self, t, c, private=False):
        who = c.get("user") or c.get("from")
        tag = f"c_{who}"
        t.tag_configure(tag, foreground=c.get("color", C["text"]), font=(FONT, 10, "bold"))
        stamp = time.strftime("%H:%M", time.localtime(c.get("ts", time.time())))
        t.configure(state="normal")
        t.insert("end", stamp + "  ", ("time",))
        if private:
            t.insert("end", "private  ", ("priv",))
        t.insert("end", who + ": ", (tag,))
        t.insert("end", (c.get("text") or "") + "\n")
        att = c.get("attachment")
        if att:
            card = AttachmentCard(t, self, att, who)
            self.cards.setdefault(att["media_id"], []).append(card)
            t.insert("end", "   ")
            t.window_create("end", window=card)
            t.insert("end", "\n")
        t.configure(state="disabled")
        t.see("end")

    def chat_line(self, c, notify=True):
        self._append(self.chat, c)
        if notify and c.get("user") != self.me:
            self._bump_unread()

    def chat_system(self, text):
        self.chat.configure(state="normal")
        self.chat.insert("end", time.strftime("%H:%M") + "  ", ("time",))
        self.chat.insert("end", text + "\n", ("sys",))
        self.chat.configure(state="disabled")
        self.chat.see("end")

    # ================================================================== media sharing
    def attach_file(self):
        path = filedialog.askopenfilename(parent=self.root, title="Send a file", filetypes=[
            ("All supported", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.mp4 *.mkv *.avi *.mov "
                              "*.webm *.mp3 *.wav *.ogg *.m4a *.aac *.flac *.pdf *.txt *.zip "
                              "*.docx *.pptx"),
            ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
            ("Videos", "*.mp4 *.mkv *.avi *.mov *.webm *.wmv"),
            ("Audio", "*.mp3 *.wav *.ogg *.m4a *.aac *.flac"),
            ("All files", "*.*")])
        if not path:
            return
        caption = self.chat_entry.get().strip()[:P.MAX_CHAT_LEN]
        self.chat_entry.delete(0, "end")
        kind = kind_for(path)
        dims = image_size(path) if kind == "image" else None
        self.media.upload(path, kind=kind, caption=caption, dims=dims)

    def insert_image(self):
        if not self.can_edit():
            self.flash(self.edit_block_reason())
            return
        path = filedialog.askopenfilename(parent=self.root, title="Place an image on the board",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.gif "
                                                                "*.bmp *.webp"),
                                                     ("All files", "*.*")])
        if path:
            self._upload_board_image(path)

    def _upload_board_image(self, path):
        dims = image_size(path)
        if dims is None:
            messagebox.showerror("Image", "Cannot read this image here. PNG and GIF always "
                                 "work; for JPEG/WebP/BMP install Pillow (pip install pillow).",
                                 parent=self.root)
            return
        xid = self.media.upload(path, kind="image", purpose="board", dims=dims)
        self.board_uploads[xid] = dims
        self.flash("Uploading image...")

    def place_board_image(self, media_id, path, dims=None):
        if not self.can_edit():
            self.flash(self.edit_block_reason())
            return
        dims = dims or image_size(path)
        if not dims:
            self.flash("Cannot read this image (install Pillow for JPEG/WebP)")
            return
        w, h = dims
        f = min(560 / w, 400 / h, 1.0)
        w, h = max(8, w * f), max(8, h * f)
        cx, cy = self.board.view_center()
        st = {"id": self.net.new_stroke_id(), "tool": "image", "color": "#000000", "width": 1,
              "points": [round(cx - w / 2, 1), round(cy - h / 2, 1),
                         round(cx + w / 2, 1), round(cy + h / 2, 1)],
              "media_id": media_id}
        self.board._send_stroke(st)

    def request_board_image(self, media_id):
        self.media.ensure({"media_id": media_id, "name": "image"}, quiet=True)

    def toggle_record(self):
        if self._rec_job is None:
            if not self.recorder.available:
                messagebox.showinfo("Voice messages", "No microphone recorder found on this "
                                    "computer.\n\nWindows: works out of the box.\nLinux: "
                                    "install alsa-utils (arecord).\nAny OS: pip install "
                                    "sounddevice", parent=self.root)
                return
            path = f"{self.media.room_dir()}/voice-{self.me}-{int(time.time())}.wav"
            try:
                self.recorder.start(path)
            except Exception as exc:
                messagebox.showerror("Voice message", str(exc), parent=self.root)
                return
            self.rec_cancel.pack(side="left", padx=(4, 0))
            self._rec_tick()
        else:
            self._finish_record(send=True)

    def _rec_tick(self):
        sec = self.recorder.elapsed()
        self.rec_btn.configure(text=f"Send voice  {int(sec) // 60}:{int(sec) % 60:02d}")
        if sec >= MAX_SECONDS:
            self._finish_record(send=True)
            return
        self._rec_job = self.after(250, self._rec_tick)

    def cancel_record(self):
        self._finish_record(send=False)

    def _finish_record(self, send):
        if self._rec_job:
            self.after_cancel(self._rec_job)
        self._rec_job = None
        self.rec_btn.configure(text="Record voice")
        self.rec_cancel.pack_forget()
        if not send:
            self.recorder.cancel()
            self.flash("Recording discarded")
            return
        path = self.recorder.path
        try:
            dur = self.recorder.stop()
        except Exception as exc:
            messagebox.showerror("Voice message", str(exc), parent=self.root)
            return
        self.media.upload(path, kind="voice", duration=round(dur, 1))

    def reset_play_buttons(self):
        for cards in self.cards.values():
            for card in cards:
                card.play_ended()

    def _on_transfer(self, ev):
        xid, state = ev["xfer"], ev["state"]
        verb = "Sending" if ev["direction"] == "up" else "Receiving"
        mid = ev.get("media_id")
        if state in ("start", "progress"):
            size = ev.get("size") or 1
            done = ev.get("done", 0)
            self.transfers[xid] = f"{verb} {ev['name']}  {int(done * 100 / size)}% of " \
                                  f"{human_size(size)}"
            for card in self.cards.get(mid, ()):
                card.progress(done, size)
        elif state == "done":
            self.transfers.pop(xid, None)
            if ev["direction"] == "up" and ev.get("purpose") == "board":
                self.place_board_image(mid, ev["path"], self.board_uploads.pop(xid, None))
            for card in self.cards.get(mid, ()):
                card.ready(ev["path"])
            self.board.rerender_media(mid)
        elif state == "error":
            self.transfers.pop(xid, None)
            self.board_uploads.pop(xid, None)
            for card in self.cards.get(mid, ()):
                card.failed(ev["message"])
            self.flash(f"{verb} {ev['name']} failed: {ev['message']}", ms=10000)
        self.xfer_label.configure(text="\n".join(self.transfers.values()))

    # ================================================================== network events
    def _pump(self):
        for _ in range(300):
            try:
                ev = self.net.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(ev)
            except Exception as exc:
                self.flash(f"Error handling {ev.get('type')}: {exc}")
        self.after(15, self._pump)

    def _handle(self, ev):
        t = ev["type"]
        if t == P.OP:
            op = ev["op"]
            changed = self.model.apply(op)
            self.board.on_op(op, changed)
            if op["kind"] == P.CLEAR:
                self.chat_system(f"{op['user']} cleared the board")
            self.seq_label.configure(text=f"seq {self.model.seq}")
        elif t == P.PREVIEW:
            self.board.on_preview(ev)
        elif t == P.CURSOR:
            self.board.on_cursor(ev)
        elif t == P.LASER:
            self.board.on_laser(ev)
        elif t == "_WELCOME":
            self._on_welcome(ev["msg"], ev["reconnect"])
        elif t == P.CHAT:
            self.chat_line(ev)
        elif t == P.DM:
            self._on_dm(ev)
        elif t == P.PRESENCE:
            self._on_presence(ev)
        elif t == P.RESET:
            self.model = BoardModel.from_dict(ev["snapshot"])
            self.votes = ev.get("votes", {})
            self.board.selection = []
            self.board.redraw_all()
            self.chat_system(f"{ev.get('by', 'teacher')} loaded a board")
            self._refresh_votes_label()
        elif t == P.ROOM_STATE:
            by = ev.get("by", "teacher")
            if ev["locked"] != self.locked:
                self.locked = ev["locked"]
                self.chat_system(f"{by} made the board read-only for students" if self.locked
                                 else f"{by} let students draw")
            if ev.get("follow") != self.follow:
                self._set_follow(ev.get("follow"))
            self._refresh_controls()
        elif t == P.SETTINGS:
            self._on_settings(ev["settings"], ev.get("key"), ev.get("by"))
        elif t == P.VOTES:
            self._on_votes(ev)
        elif t == P.TIMER_STATE:
            self._on_timer(ev)
        elif t == P.POLL_STATE:
            self._on_poll(ev)
        elif t == P.VIEWPORT:
            self.follow_view = (ev["x"], ev["y"], ev["w"], ev["h"])
            if self.following():
                self.board.set_view(*self.follow_view)
        elif t == P.HISTORY:
            if self.history_win is not None and self.history_win.winfo_exists():
                self.history_win.load(ev)
        elif t == P.ACK:
            self.board.on_error(ev.get("op_id"))
        elif t == P.ERROR:
            if ev.get("ref_op_id"):
                self.board.on_error(ev["ref_op_id"])
            self.flash(ev.get("message", "Error"))
        elif t == P.INFO:
            self.flash(ev["message"])
            self.chat_system(ev["message"])
        elif t == P.SAVES:
            self._show_saves(ev["saves"])
        elif t == "_STATUS":
            self._on_status(ev)
        elif t == "_XFER":
            self._on_transfer(ev)
        elif t == "_PLAY_END":
            self.reset_play_buttons()
        elif t == "_RTT":
            self.rtt = ev["ms"]
            self._refresh_conn()
        elif t == P.KICKED:
            self.connected = False
            self._offer_copy(ev.get("message", "You were removed"))
            self.quit_app()

    def _on_settings(self, settings, key=None, by=None):
        old = dict(self.settings)
        self.settings = dict(settings)
        self.own_only.set(bool(settings.get("own_only")))
        self.paper_var.set(dict(PAPER_NAMES).get(settings.get("paper"), "Grid"))
        if key == "own_only" and by:
            self.chat_system(f"{by} turned own-work-only mode "
                             f"{'on' if settings['own_only'] else 'off'}")
        if old.get("paper") != settings.get("paper") or old.get("pages") != settings.get("pages"):
            self.board._draw_background()
            if self.board.minimap:
                self.board.minimap.request()
        target = getattr(self, "_goto_after_pages", None)
        if key == "pages" and target is not None and settings["pages"] > target:
            self._goto_after_pages = None
            self.board.go_page(target)
        elif key == "pages" and by and by != self.me:
            self.chat_system(f"{by} added page {settings['pages']}")
        self.update_page_label()

    def _on_welcome(self, w, reconnect):
        self.role = w["role"]
        self.locked = w.get("locked", False)
        self.room = w["room"]
        if "snapshot" in w:
            self.model = BoardModel.from_dict(w["snapshot"])
        for m in w.get("missed_ops", []):
            self.model.apply(m["op"])
        self.participants = w["participants"]
        self.votes = w.get("votes", {})
        self.follow = w.get("follow")
        vp = w.get("viewport")
        self.follow_view = tuple(vp) if vp else None
        self._on_settings(dict(w.get("settings") or P.SETTINGS_DEFAULT))
        self.poll = w.get("poll")
        self.my_answer = w.get("my_answer")
        self._refresh_people()
        if self.following() and self.follow_view:
            self.board.set_view(*self.follow_view)
        else:
            self.board.redraw_all()
        for t in [self.chat] + list(self.dm_tabs.values()):
            t.configure(state="normal")
            t.delete("1.0", "end")
            t.configure(state="disabled")
        for cards in self.cards.values():
            for card in cards:
                card.destroy()
        self.cards.clear()
        for c in w.get("chat_history", []):
            self.chat_line(c, notify=False)
        for m in w.get("dms", []):
            self._on_dm(m, notify=False)
        if reconnect:
            n = len(w.get("missed_ops", []))
            how = f"caught up on {n} missed change(s)" if "missed_ops" in w else \
                "reloaded the full board"
            self.chat_system(f"Reconnected - {how}")
        else:
            pw = " (password protected)" if w.get("has_password") else ""
            if self.role == P.ROLE_HOST:
                self.chat_system(f"Class started in room {self.room}{pw}. Students join with "
                                 f"the code {self.room}.")
            else:
                self.chat_system(f"You joined room {self.room} as a "
                                 f"{ROLE_WORD.get(self.role, self.role).lower()}.")
        self._on_timer(w.get("timer") or {}, announce=False)
        self.root.title(f"ClassBoard - room {self.room} - {self.me}")
        self._refresh_controls()

    def _on_presence(self, ev):
        self.participants = ev["participants"]
        user, event = ev["user"], ev["event"]
        mine = next((p for p in self.participants if p["username"] == self.me), None)
        if mine and mine["role"] != self.role:
            was_teacher = self.is_teacher()
            self.role = mine["role"]
            if was_teacher and not self.is_teacher():
                self.chat_system("You handed the class over - you are now a student")
        text = {"joined": f"{user} joined", "left": f"{user} left",
                "kicked": f"{user} was removed by the teacher",
                "away": f"{user} lost connection (waiting for them to return)",
                "reconnected": f"{user} reconnected"}.get(event)
        if event == "role_changed":
            text = f"{user} is now {ROLE_WORD.get(ev['role'], ev['role']).lower()}"
            if user == self.me:
                self.role = ev["role"]
        if event == "hand":
            if ev.get("up"):
                text = f"{user} raised their hand"
                if self.is_teacher():
                    self.bell()
                    self.flash(f"{user} raised their hand", ms=6000)
            elif ev.get("by") != user:
                text = f"{ev.get('by')} lowered {user}'s hand"
            else:
                text = None
        if event == "hands_cleared":
            text = "All hands lowered"
        if event in ("left", "kicked", "away"):
            self.board.remove_user(user)
        if text:
            self.chat_system(text)
        self._refresh_people()
        self._refresh_controls()

    def _on_status(self, ev):
        state = ev["state"]
        self.connected = state == "connected"
        self.flash(ev["text"], ms=8000 if self.connected else 600000)
        self._refresh_controls()
        if state == "closed":
            self._offer_copy(ev["text"])

    def _offer_copy(self, text):
        """The server is gone (or the class ended): offer to keep a copy of the board."""
        if not self.model.visible_ids():
            messagebox.showwarning("ClassBoard", text, parent=self.root)
            return
        if messagebox.askyesno("ClassBoard", f"{text}.\n\nSave a copy of the board on this "
                               "computer before closing?", parent=self.root):
            self.save_board_file()

    def _refresh_online(self):
        """Always-visible participant list at the top of the Class tab."""
        for w in self.online_body.winfo_children():
            w.destroy()
        people = sorted(self.participants, key=lambda p: (p["role"] != P.ROLE_HOST,
                                                          p["status"] != "online",
                                                          p["username"].lower()))
        online = sum(p["status"] == "online" for p in people)
        self.online_title.configure(text=f"In class  ({online} online)")
        row = None
        for i, p in enumerate(people[:16]):
            if i % 3 == 0:
                row = tk.Frame(self.online_body, background=C["surface"])
                row.pack(fill="x")
            away = p["status"] != "online"
            mark = " (T)" if p["role"] == P.ROLE_HOST else (" (v)" if p["role"] ==
                                                             P.ROLE_VIEWER else "")
            lab = tk.Label(row, text=("\u25cf " if self.glyphs else "") + p["username"] + mark +
                           ("  [hand]" if p.get("hand") else ""), anchor="w",
                           background="#fff7e6" if p.get("hand") else C["surface"],
                           foreground=C["faint"] if away else p["color"],
                           font=(FONT, 9, "bold" if p["username"] == self.me else "normal"),
                           width=13)
            lab.pack(side="left", padx=(0, 2), pady=1)
            Tooltip(lab, f"{p['username']} - {ROLE_WORD.get(p['role'], p['role'])}"
                         f"{' - reconnecting' if away else ''}")
        if len(people) > 16:
            tk.Label(self.online_body, text=f"+ {len(people) - 16} more (People tab)",
                     background=C["surface"], foreground=C["muted"], font=(FONT, 8)).pack(
                anchor="w")

    def _refresh_people(self):
        self._refresh_online()
        sel = self.people.selection()
        self.people.delete(*self.people.get_children())
        order = sorted(self.participants, key=lambda p: (p["role"] != P.ROLE_HOST,
                                                         not p.get("hand")))
        for p in order:
            tags = (f"u_{p['username']}",) + (("hand",) if p.get("hand") else ())
            self.people.tag_configure(tags[0], foreground=p["color"] if p["status"] == "online"
                                      else C["faint"])
            name = p["username"] + ("  (you)" if p["username"] == self.me else "")
            status = "hand" if p.get("hand") else ("" if p["status"] == "online" else "away")
            self.people.insert("", "end", iid=p["username"], text=name, tags=tags,
                               values=(ROLE_WORD.get(p["role"], p["role"]), status))
        for s in sel:
            if self.people.exists(s):
                self.people.selection_set(s)


def fmt_time(sec):
    sec = int(round(max(0, sec)))
    h, rest = divmod(sec, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


# ====================================================================== version history
class _PreviewApp:
    """The small slice of the app a read-only preview BoardCanvas needs."""

    def __init__(self, app):
        self.app = app
        self.model = BoardModel()
        self.votes = {}
        self.me = app.me
        self.images = ImageCache(app.media)
        self.settings = app.settings
        self.participants = app.participants
        self.glyphs = app.glyphs
        self.tool = tk.StringVar(value="hand")
        self.show_authors = tk.BooleanVar(value=False)
        self.follow_view = None

    def request_board_image(self, mid):
        self.app.request_board_image(mid)

    def following(self):
        return False

    def flash(self, *a, **k):
        pass

    def set_hover(self, _t):
        pass

    def update_zoom_label(self):
        pass

    def on_view_changed(self):
        pass

    def can_edit(self):
        return False


class HistoryWindow(tk.Toplevel):
    """Time travel: drag the slider to see the board at any earlier moment, replay the
    lesson step by step, and (teacher) restore an old version for everyone. Built by
    replaying the server's op log with the same BoardModel.apply() as the live board."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Version history")
        self.geometry("980x640")
        self.configure(background=C["bg"])
        self.base = None
        self.ops = []
        self.models = {}
        self.playing = None
        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill="x")
        self.info = ttk.Label(top, text="Loading history from the server...",
                              font=(FONT, 11, "bold"))
        self.info.pack(anchor="w")
        self.detail = ttk.Label(top, style="Muted.TLabel")
        self.detail.pack(anchor="w")
        ctl = ttk.Frame(self, padding=12)
        ctl.pack(fill="x", side="bottom")
        self.shim = _PreviewApp(app)
        self.canvas = BoardCanvas(self, self.shim, preview=True)
        self.canvas.pack(fill="both", expand=True, padx=12)
        self.pos = tk.IntVar(value=0)
        ttk.Button(ctl, text="‹", width=3, command=lambda: self.step(-1)).pack(side="left")
        self.scale = ttk.Scale(ctl, from_=0, to=1, orient="horizontal",
                               command=lambda v: self.show(int(float(v))))
        self.scale.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(ctl, text="›", width=3, command=lambda: self.step(1)).pack(side="left")
        self.play_btn = ttk.Button(ctl, text="Replay", command=self.replay)
        self.play_btn.pack(side="left", padx=(8, 0))
        self.restore_btn = ttk.Button(ctl, text="Restore this version", style="Accent.TButton",
                                      command=self.restore)
        self.restore_btn.pack(side="left", padx=(8, 0))
        if not app.is_teacher():
            self.restore_btn.configure(state="disabled")
            Tooltip(self.restore_btn, "Only the teacher can restore an old version")
        self.protocol("WM_DELETE_WINDOW", self.close)

    def load(self, ev):
        self.base = ev["base"]
        self.ops = ev["ops"]
        self.models = {}
        n = len(self.ops)
        self.scale.configure(to=max(n, 1))
        self.scale.set(n)
        self.show(n)
        self.after(200, self.canvas.fit_content)

    def model_at(self, k):
        """Board after the first k ops (checkpoints every 100 ops keep scrubbing fast)."""
        starts = [c for c in self.models if c <= k]
        if starts:
            start = max(starts)
            m = BoardModel.from_dict(self.models[start].to_dict())
        else:
            start = 0
            m = BoardModel.from_dict(self.base)
        for i in range(start, k):
            m.apply(self.ops[i])
            if (i + 1) % 100 == 0 and (i + 1) not in self.models:
                self.models[i + 1] = BoardModel.from_dict(m.to_dict())
        return m

    def show(self, k):
        if self.base is None:
            return
        k = max(0, min(len(self.ops), k))
        if k == self.pos.get() and self.shim.model.seq and k:
            return
        self.pos.set(k)
        self.shim.model = self.model_at(k)
        self.canvas.redraw_all()
        n = len(self.ops)
        self.info.configure(text=f"Version {k} of {n}" + ("   (now)" if k == n else ""))
        if k == 0:
            self.detail.configure(text="Start of the recorded history")
        else:
            op = self.ops[k - 1]
            what = {"STROKE": "added a " + op.get("stroke", {}).get("tool", "object"),
                    "MOVE": "moved objects", "EDIT": "edited an object",
                    "ERASE": "erased objects", "CLEAR": "cleared the board",
                    "UNDO": "undid an action", "REDO": "redid an action"}.get(op["kind"],
                                                                             op["kind"])
            when = time.strftime("%H:%M:%S", time.localtime(op.get("ts", 0)))
            self.detail.configure(text=f"{when}   {op['user']} {what}   (seq {op['seq']})")

    def step(self, d):
        k = self.pos.get() + d
        self.scale.set(k)
        self.show(k)

    def replay(self):
        if self.playing:
            self.after_cancel(self.playing)
            self.playing = None
            self.play_btn.configure(text="Replay")
            return
        self.play_btn.configure(text="Stop")
        n = len(self.ops)
        step = max(1, n // 120)
        self.scale.set(0)
        self.show(0)

        def tick(k=0):
            k = min(n, k + step)
            self.scale.set(k)
            self.show(k)
            if k < n:
                self.playing = self.after(60, lambda: tick(k))
            else:
                self.playing = None
                self.play_btn.configure(text="Replay")
        self.playing = self.after(300, tick)

    def restore(self):
        k = self.pos.get()
        m = self.shim.model
        if not messagebox.askyesno("Restore", f"Replace the board for everyone with version "
                                   f"{k}?\n\nTip: use More > Save board file first if you may "
                                   "want today's board back.", parent=self):
            return
        keep = ("id", "tool", "color", "width", "points", "text", "fill", "media_id", "shape",
                "style", "label", "src", "dst", "user")
        strokes = [{kk: s[kk] for kk in keep if kk in s} for s in m.visible_strokes()]
        self.app.net.send({"type": P.RESTORE, "strokes": strokes})
        self.close()

    def close(self):
        if self.playing:
            self.after_cancel(self.playing)
        self.destroy()
