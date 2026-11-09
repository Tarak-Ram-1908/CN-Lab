"""
ClassBoard look and feel: one calm, high-contrast light theme designed for a
classroom projector and for long study sessions.

* Colours are defined once here (teal accent, slate text, soft grey surround
  so the white "pages" of the board stand out like sheets of paper).
* Tool icons are drawn with Canvas vector shapes instead of emoji or icon
  fonts, so they look identical on Windows, macOS and Linux lab machines.
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

C = {
    "bg": "#f3f5f8",          # window background
    "surface": "#ffffff",     # panels, header
    "border": "#dde3ea",
    "text": "#1f2933",
    "muted": "#64748b",
    "faint": "#94a3b8",
    "accent": "#0f766e",      # teal
    "accent_dark": "#115e59",
    "accent_soft": "#d5f2ee",
    "hover": "#eef2f6",
    "warn": "#b45309",
    "hand": "#f59e0b",
    "danger": "#b91c1c",
    "ok": "#15803d",
    "canvas": "#e6eaef",      # around the pages
    "page": "#ffffff",
    "shadow": "#cfd6de",
    "paper": "#e3e8ee",       # grid / ruled lines
    "margin": "#f3b4b4",      # ruled paper margin line
    "laser": "#ef4444",
    "select": "#0f766e",
}

FONT = "Helvetica"


def pick_font(root):
    """Best available UI font on this machine."""
    global FONT
    families = set(tkfont.families(root))
    for f in ("Segoe UI", "Helvetica Neue", "Inter", "Noto Sans", "DejaVu Sans", "Helvetica"):
        if f in families:
            FONT = f
            break
    return FONT


def setup_style(root):
    pick_font(root)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    root.configure(background=C["bg"])
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=FONT, size=10)
        except tk.TclError:
            pass
    style.configure(".", background=C["bg"], foreground=C["text"], font=(FONT, 10),
                    bordercolor=C["border"], lightcolor=C["surface"], darkcolor=C["border"],
                    focuscolor=C["accent"])
    style.configure("TFrame", background=C["bg"])
    style.configure("Surface.TFrame", background=C["surface"])
    style.configure("TLabel", background=C["bg"])
    style.configure("Surface.TLabel", background=C["surface"])
    style.configure("Title.TLabel", font=(FONT, 18, "bold"), foreground=C["text"])
    style.configure("Brand.TLabel", font=(FONT, 13, "bold"), foreground=C["accent"],
                    background=C["surface"])
    style.configure("Head.TLabel", font=(FONT, 10, "bold"), background=C["surface"])
    style.configure("Muted.TLabel", foreground=C["muted"])
    style.configure("SMuted.TLabel", foreground=C["muted"], background=C["surface"],
                    font=(FONT, 9))
    style.configure("Big.TLabel", font=(FONT, 22, "bold"), background=C["surface"])
    style.configure("Status.TLabel", background=C["surface"], foreground=C["muted"],
                    padding=(10, 3), font=(FONT, 9))
    style.configure("TButton", padding=(10, 4), background=C["surface"])
    style.map("TButton", background=[("active", C["hover"]), ("disabled", C["bg"])],
              foreground=[("disabled", C["faint"])])
    style.configure("Accent.TButton", foreground="white", background=C["accent"],
                    bordercolor=C["accent"], padding=(12, 5))
    style.map("Accent.TButton", background=[("active", C["accent_dark"]),
                                            ("disabled", "#9cc9c3")],
              foreground=[("disabled", "white")])
    style.configure("Ghost.TButton", background=C["surface"], bordercolor=C["surface"],
                    padding=(6, 4))
    style.map("Ghost.TButton", background=[("active", C["hover"])])
    style.configure("Danger.TButton", foreground=C["danger"], background=C["surface"])
    style.configure("TCheckbutton", background=C["surface"])
    style.configure("Bg.TCheckbutton", background=C["bg"])
    style.configure("TRadiobutton", background=C["surface"])
    style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab", padding=(14, 6), font=(FONT, 10), background=C["bg"],
                    foreground=C["muted"], borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", C["surface"])],
              foreground=[("selected", C["accent"])])
    style.configure("Inner.TNotebook", background=C["surface"])
    style.configure("Inner.TNotebook.Tab", padding=(10, 4), font=(FONT, 9))
    style.configure("Treeview", rowheight=26, background=C["surface"],
                    fieldbackground=C["surface"], bordercolor=C["border"])
    style.configure("Treeview.Heading", font=(FONT, 9, "bold"), background=C["bg"],
                    foreground=C["muted"])
    style.map("Treeview", background=[("selected", C["accent_soft"])],
              foreground=[("selected", C["text"])])
    style.configure("TCombobox", padding=3)
    style.configure("Horizontal.TScale", background=C["surface"])
    style.configure("Poll.Horizontal.TProgressbar", troughcolor=C["hover"],
                    background=C["accent"], bordercolor=C["hover"], thickness=10)
    return style


# ====================================================================== icons
def draw_icon(cv, name, cx, cy, s, col):
    """Draw tool icon `name` centred at (cx, cy), roughly s x s pixels."""
    h = s / 2
    lw = max(1.6, s / 12)
    L = lambda *p, **k: cv.create_line(*p, fill=col, width=k.pop("w", lw), capstyle="round",
                                       joinstyle="round", **k)
    if name == "select":
        pts = [cx - h * .45, cy - h * .8, cx - h * .45, cy + h * .55, cx - h * .1, cy + h * .22,
               cx + h * .15, cy + h * .8, cx + h * .35, cy + h * .7, cx + h * .1, cy + h * .12,
               cx + h * .55, cy + h * .12]
        cv.create_polygon(*pts, fill=col, outline=col, joinstyle="round")
    elif name == "hand":
        a = h * .8
        L(cx - a, cy, cx + a, cy, arrow="both", arrowshape=(5, 6, 3))
        L(cx, cy - a, cx, cy + a, arrow="both", arrowshape=(5, 6, 3))
    elif name == "pen":
        L(cx - h * .7, cy + h * .7, cx + h * .55, cy - h * .55, w=lw * 2.2)
        L(cx - h * .7, cy + h * .7, cx - h * .85, cy + h * .85, w=lw)
        L(cx - h * .3, cy + h * .85, cx + h * .8, cy + h * .85, w=lw * .8)
    elif name == "marker":
        cv.create_rectangle(cx - h * .8, cy + h * .25, cx + h * .8, cy + h * .7, fill="#fde68a",
                            outline="")
        L(cx - h * .45, cy + h * .35, cx + h * .45, cy - h * .65, w=lw * 3.2)
    elif name == "laser":
        cv.create_oval(cx - h * .3, cy - h * .3, cx + h * .3, cy + h * .3, fill=C["laser"],
                       outline="")
        for ang in range(0, 360, 45):
            import math
            r0, r1 = h * .5, h * .85
            a = math.radians(ang)
            L(cx + r0 * math.cos(a), cy + r0 * math.sin(a), cx + r1 * math.cos(a),
              cy + r1 * math.sin(a), w=lw * .8)
    elif name == "eraser":
        pts = [cx - h * .85, cy + h * .25, cx - h * .15, cy - h * .5, cx + h * .75, cy + h * .35,
               cx + h * .1, cy + h * .95 - h * .1]
        cv.create_polygon(*pts, fill="", outline=col, width=lw)
        cv.create_polygon(cx - h * .85, cy + h * .25, cx - h * .5, cy - h * .12, cx + h * .38,
                          cy + h * .72, cx + h * .1, cy + h * .85, fill=col, outline=col)
    elif name == "shape":
        cv.create_rectangle(cx - h * .85, cy - h * .7, cx + h * .2, cy + h * .35, outline=col,
                            width=lw)
        cv.create_oval(cx - h * .15, cy - h * .2, cx + h * .85, cy + h * .8, outline=col,
                       width=lw, fill="")
    elif name == "text":
        cv.create_text(cx, cy, text="T", fill=col, font=(FONT, int(s * .75), "bold"))
    elif name == "note":
        p = [cx - h * .8, cy - h * .8, cx + h * .8, cy - h * .8, cx + h * .8, cy + h * .3,
             cx + h * .3, cy + h * .8, cx - h * .8, cy + h * .8]
        cv.create_polygon(*p, fill="#fef3c7", outline=col, width=lw)
        L(cx + h * .8, cy + h * .3, cx + h * .3, cy + h * .3, cx + h * .3, cy + h * .8)
    elif name == "node":
        cv.create_rectangle(cx - h * .9, cy - h * .45, cx + h * .9, cy + h * .45, outline=col,
                            width=lw)
        L(cx - h * .45, cy, cx + h * .45, cy, w=lw * .8)
    elif name == "connect":
        cv.create_rectangle(cx - h * .95, cy - h * .9, cx - h * .25, cy - h * .3, outline=col,
                            width=lw)
        cv.create_rectangle(cx + h * .25, cy + h * .3, cx + h * .95, cy + h * .9, outline=col,
                            width=lw)
        L(cx - h * .6, cy - h * .3, cx - h * .6, cy + h * .6, cx + h * .2, cy + h * .6,
          arrow="last", arrowshape=(5, 6, 3))
    elif name == "image":
        cv.create_rectangle(cx - h * .9, cy - h * .7, cx + h * .9, cy + h * .7, outline=col,
                            width=lw)
        cv.create_polygon(cx - h * .7, cy + h * .5, cx - h * .2, cy - h * .1, cx + h * .15,
                          cy + h * .3, cx + h * .4, cy + h * .05, cx + h * .7, cy + h * .5,
                          fill=col, outline="")
        cv.create_oval(cx + h * .25, cy - h * .5, cx + h * .55, cy - h * .2, fill=col, outline="")
    elif name == "vote":
        cv.create_oval(cx - h * .85, cy - h * .85, cx + h * .85, cy + h * .85, outline=col,
                       width=lw)
        L(cx - h * .4, cy + h * .02, cx - h * .08, cy + h * .35, cx + h * .45, cy - h * .35)
    elif name == "hand_up":
        cv.create_text(cx, cy, text="✋", fill=col, font=(FONT, int(s * .7)))


class IconButton(tk.Canvas):
    """A tool button: vector icon + small caption; selected / hover / disabled states.
    `menu` (optional) is shown when the already-selected tool is clicked again, or
    on right-click - used for tools with variants (shape kind, node shape...)."""

    W, H = 58, 50

    def __init__(self, master, icon, label, command, tooltip="", menu_cb=None):
        super().__init__(master, width=self.W, height=self.H, highlightthickness=0,
                         background=C["surface"], cursor="hand2")
        self.icon, self.label, self.command, self.menu_cb = icon, label, command, menu_cb
        self.selected = False
        self.enabled = True
        self.hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        if menu_cb:
            self.bind("<ButtonPress-3>", lambda e: self.enabled and menu_cb(self))
        if tooltip:
            Tooltip(self, tooltip)
        self.redraw()

    def _set_hover(self, on):
        self.hover = on
        self.redraw()

    def _click(self, e):
        if not self.enabled or not (0 <= e.x <= self.W and 0 <= e.y <= self.H):
            return
        if self.selected and self.menu_cb:
            self.menu_cb(self)
        else:
            self.command()

    def set_state(self, selected=None, enabled=None):
        if selected is not None:
            self.selected = selected
        if enabled is not None:
            self.enabled = enabled
        self.redraw()

    def set_label(self, label):
        self.label = label
        self.redraw()

    def redraw(self):
        self.delete("all")
        bg = C["surface"]
        if self.selected:
            bg = C["accent_soft"]
        elif self.hover and self.enabled:
            bg = C["hover"]
        self.create_rectangle(3, 2, self.W - 3, self.H - 2, fill=bg, outline="")
        col = C["accent"] if self.selected else (C["text"] if self.enabled else C["faint"])
        if self.selected:
            self.create_rectangle(3, 8, 6, self.H - 8, fill=C["accent"], outline="")
        draw_icon(self, self.icon, self.W / 2, 18, 20, col)
        self.create_text(self.W / 2, self.H - 9, text=self.label, fill=col,
                         font=(FONT, 8, "bold" if self.selected else "normal"))
        if self.menu_cb:
            self.create_polygon(self.W - 10, self.H - 5, self.W - 5, self.H - 5, self.W - 5,
                                self.H - 10, fill=C["faint"], outline="")


class Tooltip:
    """Small hover tooltip (appears after a short delay)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.win, self.job = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _e=None):
        self.job = self.widget.after(450, self.show)

    def show(self):
        self.job = None
        if self.win or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        self.win.wm_geometry(f"+{x}+{y}")
        tk.Label(self.win, text=self.text, background="#1f2933", foreground="white",
                 padx=8, pady=4, font=(FONT, 9), justify="left", wraplength=320).pack()

    def hide(self, _e=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None


class Card(tk.Frame):
    """White rounded-looking panel with a title row, used in the side panel."""

    def __init__(self, master, title, **kw):
        super().__init__(master, background=C["surface"], highlightthickness=1,
                         highlightbackground=C["border"], padx=12, pady=10, **kw)
        self.head = tk.Frame(self, background=C["surface"])
        self.head.pack(fill="x")
        tk.Label(self.head, text=title, background=C["surface"], foreground=C["text"],
                 font=(FONT, 10, "bold")).pack(side="left")
        self.body = tk.Frame(self, background=C["surface"])
        self.body.pack(fill="x", pady=(6, 0))


class ScrollFrame(ttk.Frame):
    """Vertically scrollable container (mouse wheel works over its content)."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, background=C["bg"])
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win,
                                                                            width=e.width))
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda e: self._wheel(True))
            w.bind("<Leave>", lambda e: self._wheel(False))

    def _wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._scroll)
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")

    def _scroll(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
