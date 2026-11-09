"""
BoardCanvas: draws the shared board and turns mouse input into operations.

Coordinates
-----------
Objects live in unbounded *world* coordinates shared by every client. Each
client has its own view:   screen = origin + world * scale.
The board is an infinite canvas with numbered *pages* (sheets of paper laid
side by side, P.PAGE_W x P.PAGE_H each) so a teacher can prepare a lesson as a
sequence of pages and students can export them as PDF notes.

Layers (bottom to top)
----------------------
  pages + paper pattern ("bg") -> committed objects (seq order, vote badges)
  -> my provisional objects ("prov") -> others' live previews ("pv")
  -> selection ("sel") -> author tags ("authors") -> laser trails -> cursors

Responsiveness
--------------
My operations are drawn immediately (provisional) while the message travels to
the server; the server's OP echo replaces the provisional drawing, or an ERROR
rolls it back. Pan/zoom move the existing Tk items (C code) and redraw crisply
a moment later, so even big boards stay smooth.
"""

import math
import time
import tkinter as tk
import tkinter.font as tkfont

from common import geometry as G
from common import protocol as P
from common.shapes import recognize
from .export import STICKY_COLOR, text_font_size
from .theme import C, FONT

SHAPE_TOOLS = ("line", "arrow", "rect", "ellipse")
FREEHAND_TOOLS = ("pen", "highlighter")
BOX_SHAPES = ("triangle", "diamond")          # shape-tool kinds stored as polygons
MIN_SCALE, MAX_SCALE = 0.05, 8.0
SHAPE_NAMES = {"line": "line", "ellipse": "ellipse", "rect": "rectangle"}
NODE_W, NODE_H = 180, 76
NODE_TEXT = 16                                  # node label size (world px)
LASER_LIFE = 1.1                                # seconds a laser trail stays visible
RESIZABLE = ("rect", "ellipse", "line", "arrow", "polygon", "image", "node", "pen",
             "highlighter")


class BoardCanvas(tk.Canvas):
    def __init__(self, master, app, preview=False, **kw):
        super().__init__(master, background=C["canvas"], highlightthickness=0,
                         cursor="crosshair", **kw)
        self.app = app
        self.preview = preview          # history preview: read-only, no network
        self.scale = 1.0
        self.ox = self.oy = 0.0
        self.view_ready = False
        self.items = {}                 # stroke id -> [canvas item ids] (committed)
        self.prov = {}                  # op_id -> ("stroke", stroke, items) | ("erase", sids)
        #                                           | ("move", sids, dx, dy)
        self.previews = {}              # (user, stroke_id) -> {"items", "pts", "t"}
        self.cursors = {}               # user -> {"items", "t", "x", "y", "color"}
        self.lasers = {}                # user -> {"pts": [(x, y, t)], "color"}
        self.laser_job = None
        self.my_laser_sent = 0
        self.cur_stroke = None
        self.cur_box_kind = None
        self.erase_hits = []
        self.last_preview_sent = 0.0
        self.preview_sent_upto = 0
        self.last_cursor_sent = 0.0
        self._resize_job = self._zoom_job = self._bg_job = self._authors_job = None
        self.pan_anchor = None
        self.space_down = False
        self.selection = []             # selected stroke ids (Select tool)
        self.drag = None                # select-tool drag state
        self.live = {}                  # sid -> (dx, dy) while dragging (for connectors)
        self.editor = None              # inline text editor
        self.minimap = None

        self.bind("<Configure>", self._on_resize)
        if preview:
            self.bind("<ButtonPress-1>", self._pan_start)
            self.bind("<B1-Motion>", self._pan_move)
            self.bind("<ButtonRelease-1>", self._pan_end)
        else:
            self.bind("<ButtonPress-1>", self._on_press)
            self.bind("<B1-Motion>", self._on_drag)
            self.bind("<ButtonRelease-1>", self._on_release)
            self.bind("<Double-Button-1>", self._on_double)
            self.bind("<ButtonPress-3>", self._context_menu)
            self.bind("<Motion>", self._on_motion)
            self.bind("<Leave>", lambda e: self.app.set_hover(""))
            self.after(500, self._expire_ephemeral)
        self.bind("<ButtonPress-2>", self._pan_start)
        self.bind("<B2-Motion>", self._pan_move)
        self.bind("<ButtonRelease-2>", self._pan_end)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self._on_wheel(e, 120))
        self.bind("<Button-5>", lambda e: self._on_wheel(e, -120))

    # ================================================================ geometry
    def to_screen(self, pts):
        out = []
        for i in range(0, len(pts), 2):
            out.append(self.ox + pts[i] * self.scale)
            out.append(self.oy + pts[i + 1] * self.scale)
        return out

    def to_world(self, x, y):
        return (round((x - self.ox) / self.scale, 1), round((y - self.oy) / self.scale, 1))

    def view_rect(self):
        w, h = max(self.winfo_width(), 50), max(self.winfo_height(), 50)
        x0, y0 = self.to_world(0, 0)
        return x0, y0, w / self.scale, h / self.scale

    def view_center(self):
        x, y, w, h = self.view_rect()
        return x + w / 2, y + h / 2

    def set_view(self, x, y, w, h, redraw=True):
        ww, wh = max(self.winfo_width(), 50), max(self.winfo_height(), 50)
        self.scale = max(MIN_SCALE, min(MAX_SCALE, min(ww / max(w, 1), wh / max(h, 1))))
        self.ox = ww / 2 - (x + w / 2) * self.scale
        self.oy = wh / 2 - (y + h / 2) * self.scale
        self.view_ready = True
        if redraw:
            self.redraw_all()
        self._view_moved(redraw_done=True)

    def content_bounds(self):
        model = self.app.model
        boxes = [model.bounds(sid) for sid in model.visible_ids()]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def fit_content(self):
        if self.app.following():
            return
        b = self.content_bounds()
        if b is None:
            self.go_page(0)
            return
        pad = 60
        w, h = max(b[2] - b[0], 400), max(b[3] - b[1], 250)
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        self.set_view(cx - w / 2 - pad, cy - h / 2 - pad, w + 2 * pad, h + 2 * pad)

    def go_page(self, i):
        if self.app.following():
            return
        x, y, w, h = G.page_rect(i)
        m = 30
        self.set_view(x - m, y - m, w + 2 * m, h + 2 * m)

    def current_page(self):
        cx, cy = self.view_center()
        return G.page_of(cx, cy)

    def center_on(self, wx, wy):
        if self.app.following():
            return
        cx, cy = self.view_center()
        self.pan_by((cx - wx) * self.scale, (cy - wy) * self.scale)

    # ================================================================ pan & zoom
    def pan_by(self, dx, dy):
        if not dx and not dy:
            return
        self.ox += dx
        self.oy += dy
        self.move("world", dx, dy)
        self._schedule_bg()
        self._view_moved()

    def zoom_at(self, factor, sx=None, sy=None):
        if sx is None:
            sx, sy = self.winfo_width() / 2, self.winfo_height() / 2
        new = max(MIN_SCALE, min(MAX_SCALE, self.scale * factor))
        f = new / self.scale
        if abs(f - 1) < 1e-6:
            return
        self.scale = new
        self.ox = sx - (sx - self.ox) * f
        self.oy = sy - (sy - self.oy) * f
        super().scale("world", sx, sy, f, f)
        if self._zoom_job:
            self.after_cancel(self._zoom_job)
        self._zoom_job = self.after(120, self.redraw_all)
        self._view_moved()

    def _view_moved(self, redraw_done=False):
        self.app.update_zoom_label()
        if self.minimap:
            self.minimap.request()
        self.app.on_view_changed()
        if self.editor:
            self.editor.reposition()

    def _pan_start(self, e):
        if self.app.following():
            self.app.flash(self.app.follow_block_reason())
            return
        self.pan_anchor = (e.x, e.y)
        self.configure(cursor="fleur")

    def _pan_move(self, e):
        if self.pan_anchor is None:
            return
        self.pan_by(e.x - self.pan_anchor[0], e.y - self.pan_anchor[1])
        self.pan_anchor = (e.x, e.y)

    def _pan_end(self, _e=None):
        self.pan_anchor = None
        self.configure(cursor=self._tool_cursor())

    def _on_wheel(self, e, delta=None):
        if self.app.following():
            return "break"
        d = delta if delta is not None else e.delta
        if abs(d) < 10:
            d *= 40
        steps = d / 120
        if e.state & 0x0004:
            self.zoom_at(1.15 ** steps, e.x, e.y)
        elif e.state & 0x0001:
            self.pan_by(steps * 60, 0)
        else:
            self.pan_by(0, steps * 60)
        return "break"

    def _tool_cursor(self):
        tool = self.app.tool.get()
        return {"hand": "fleur", "select": "arrow", "vote": "hand2", "laser": "dotbox",
                "text": "xterm", "connect": "plus"}.get(tool, "crosshair")

    # ================================================================ rendering
    def _on_resize(self, _event=None):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._resized)

    def _resized(self):
        self._resize_job = None
        if not self.view_ready:
            if self.app.following() and self.app.follow_view:
                self.set_view(*self.app.follow_view)
            else:
                x, y, w, h = G.page_rect(0)
                self.set_view(x - 30, y - 30, w + 60, h + 60)
            return
        if self.app.following() and self.app.follow_view:
            self.set_view(*self.app.follow_view)
        else:
            self.redraw_all()
            self._view_moved()

    def redraw_all(self):
        """Full redraw from the model (zoom, resize, welcome, reset)."""
        self._zoom_job = None
        self.delete("all")
        if self.editor:                       # the editor's window item was just deleted
            self.editor.reattach()
        self.items.clear()
        self.app.images.clear()
        self.previews.clear()
        self.cursors.clear()
        self._draw_background()
        model = self.app.model
        for sid in model.order:
            self._render(model.placed(sid), committed=True)
            if not model.is_visible(sid):
                for it in self.items.get(sid, ()):
                    self.itemconfigure(it, state="hidden")
        for op_id, entry in list(self.prov.items()):
            if entry[0] == "stroke":
                self.prov[op_id] = ("stroke", entry[1], self._render(entry[1], committed=False))
            elif entry[0] == "erase":
                self.refresh_visibility(entry[1])
            elif entry[0] == "move":
                for sid in entry[1]:
                    self.move(f"s:{sid}", entry[2] * self.scale, entry[3] * self.scale)
        self._draw_selection()
        self._draw_authors()
        self._raise_overlays()
        if self.minimap:
            self.minimap.request()
        self.app.update_zoom_label()

    # ---------------------------------------------------------------- pages & paper
    def _schedule_bg(self):
        if self._bg_job is None:
            self._bg_job = self.after(80, self._redraw_bg)

    def _redraw_bg(self):
        self._bg_job = None
        self._draw_background()

    def _draw_background(self):
        """White pages on a grey desk, with the room's paper pattern (grid/ruled/...)."""
        self.delete("bg")
        settings = self.app.settings
        pages = settings.get("pages", 1)
        paper = settings.get("paper", "grid")
        vx, vy, vw, vh = self.view_rect()
        s = self.scale
        tags = ("bg", "world")
        for i in range(pages):
            px, py, pw, ph = G.page_rect(i)
            if px > vx + vw or px + pw < vx or py > vy + vh or py + ph < vy:
                continue
            x0, y0 = self.to_screen([px, py])
            x1, y1 = self.to_screen([px + pw, py + ph])
            sh = max(2, 5 * s)
            self.create_rectangle(x0 + sh, y0 + sh, x1 + sh, y1 + sh, fill=C["shadow"],
                                  outline="", tags=tags)
            self.create_rectangle(x0, y0, x1, y1, fill=C["page"], outline=C["border"],
                                  tags=tags)
            if s > 0.12:
                self.create_text(x0, y0 - 6, text=f"Page {i + 1}", anchor="sw",
                                 fill=C["muted"], font=(FONT, max(8, int(11 * min(s, 1.4)))),
                                 tags=tags)
            # paper pattern, only for the visible part of the page
            cx0, cy0 = max(px, vx), max(py, vy)
            cx1, cy1 = min(px + pw, vx + vw), min(py + ph, vy + vh)
            self._paper(paper, px, py, pw, ph, cx0, cy0, cx1, cy1, tags)
        self.tag_lower("bg")

    def _paper(self, paper, px, py, pw, ph, cx0, cy0, cx1, cy1, tags):
        s = self.scale
        if paper == "plain" or cx1 <= cx0 or cy1 <= cy0:
            return
        step = 40.0
        if paper == "graph":
            step = 20.0
        while step * s < 9:
            step *= 2
        sx0, sy0 = self.to_screen([px, py])
        sx1, sy1 = self.to_screen([px + pw, py + ph])
        col = C["paper"]
        if paper in ("grid", "graph"):
            gx = px + math.ceil((cx0 - px) / step) * step
            while gx <= cx1:
                major = paper == "graph" and abs(((gx - px) / step) % 5) < 1e-6
                x = self.ox + gx * s
                self.create_line(x, max(sy0, 0), x, min(sy1, self.winfo_height()),
                                 fill="#cfd8e3" if major else col, tags=tags)
                gx += step
        if paper in ("grid", "graph", "ruled"):
            gy = py + math.ceil((cy0 - py) / step) * step
            if paper == "ruled":
                gy = max(gy, py + 2 * step)
            while gy <= cy1:
                major = paper == "graph" and abs(((gy - py) / step) % 5) < 1e-6
                y = self.oy + gy * s
                self.create_line(max(sx0, 0), y, min(sx1, self.winfo_width()), y,
                                 fill="#cfd8e3" if major or paper == "ruled" else col,
                                 tags=tags)
                gy += step
            if paper == "ruled":
                mx = self.ox + (px + 90) * s
                self.create_line(mx, sy0, mx, sy1, fill=C["margin"], width=max(1, 1.5 * s),
                                 tags=tags)
        if paper == "dots":
            r = max(1.0, 1.6 * s)
            gx0 = px + math.ceil((cx0 - px) / step) * step
            gy = py + math.ceil((cy0 - py) / step) * step
            n = 0
            while gy <= cy1 and n < 6000:
                gx = gx0
                while gx <= cx1 and n < 6000:
                    x, y = self.ox + gx * s, self.oy + gy * s
                    self.create_oval(x - r, y - r, x + r, y + r, fill="#c3ccd6", outline="",
                                     tags=tags)
                    gx += step
                    n += 1
                gy += step

    # ---------------------------------------------------------------- objects
    def _draw(self, stroke, tags):
        """Create the canvas items for one stroke; returns their ids."""
        tags = tuple(tags) + ("world",)
        tool = stroke["tool"]
        color = stroke["color"]
        s = self.scale
        width = max(1.0, stroke["width"] * s)
        pts = self.to_screen(stroke["points"])
        ids = []
        if tool in FREEHAND_TOOLS:
            if tool == "highlighter":
                width = max(2.0, stroke["width"] * 3 * s)
            if len(pts) == 2:
                r = width / 2
                ids.append(self.create_oval(pts[0] - r, pts[1] - r, pts[0] + r, pts[1] + r,
                                            fill=color, outline="", tags=tags))
            else:
                kw = dict(fill=color, width=width, capstyle=tk.ROUND, joinstyle=tk.ROUND,
                          smooth=True, splinesteps=8, tags=tags)
                if tool == "highlighter":
                    kw["stipple"] = "gray50"
                ids.append(self.create_line(*pts, **kw))
        elif tool in ("line", "arrow"):
            kw = dict(fill=color, width=width, capstyle=tk.ROUND, tags=tags)
            if tool == "arrow":
                a = max(8, width * 3)
                kw.update(arrow=tk.LAST, arrowshape=(a, a * 1.2, a / 2.5))
            ids.append(self.create_line(*pts, **kw))
        elif tool in ("rect", "ellipse"):
            fn = self.create_rectangle if tool == "rect" else self.create_oval
            ids.append(fn(*pts, outline=color, width=width, fill=stroke.get("fill") or "",
                          tags=tags))
        elif tool == "polygon":
            ids.append(self.create_polygon(*pts, outline=color, width=width,
                                           fill=stroke.get("fill") or "", joinstyle=tk.ROUND,
                                           tags=tags))
        elif tool == "node":
            ids += self._draw_node(stroke, tags)
        elif tool == "connector":
            ids += self._draw_connector(stroke, tags)
        elif tool == "image":
            x0, y0, x1, y1 = min(pts[0], pts[2]), min(pts[1], pts[3]), \
                max(pts[0], pts[2]), max(pts[1], pts[3])
            img = None
            if (x1 - x0) * (y1 - y0) < 4e7:
                img = self.app.images.get(stroke["media_id"], max(1, x1 - x0), max(1, y1 - y0))
            if img is not None:
                ids.append(self.create_image(x0, y0, image=img, anchor="nw", tags=tags))
                ids.append(self.create_rectangle(x0, y0, x0 + img.width(), y0 + img.height(),
                                                 outline="#c3c9d1", width=1, tags=tags))
            else:
                ids.append(self.create_rectangle(x0, y0, x1, y1, outline="#9aa3ad", dash=(4, 3),
                                                 fill="#f4f5f7", tags=tags))
                ids.append(self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="image",
                                            fill="#6b7480", font=(FONT, 9), tags=tags))
                self.app.request_board_image(stroke["media_id"])
        elif tool in ("text", "sticky"):
            size = max(6, int(text_font_size(stroke["width"]) * s))
            font = tkfont.Font(family=FONT, size=-size)
            if tool == "text":
                ids.append(self.create_text(pts[0], pts[1], text=stroke.get("text", ""),
                                            fill=color, font=font, anchor="nw",
                                            width=max(40, 900 * s), tags=tags))
            else:
                t = self.create_text(pts[0] + 12 * s, pts[1] + 12 * s,
                                     text=stroke.get("text", ""), fill="#1f2328", font=font,
                                     anchor="nw", width=max(30, 216 * s), tags=tags)
                bx = self.bbox(t)
                pad = 12 * s
                r = self.create_rectangle(pts[0], pts[1], max(bx[2] + pad, pts[0] + 240 * s),
                                          max(bx[3] + pad, pts[1] + 120 * s),
                                          fill=stroke.get("fill") or STICKY_COLOR,
                                          outline=color, width=max(1, 2 * s), tags=tags)
                self.tag_lower(r, t)
                ids = [r, t]
        return ids

    def _node_box(self, sid):
        """World box of a node, including a live drag offset."""
        st = self.app.model.placed(sid)
        x0, y0, x1, y1 = G.norm_box(st["points"])
        dx, dy = self.live.get(sid, (0, 0))
        return (x0 + dx, y0 + dy, x1 + dx, y1 + dy), st.get("shape", "rounded")

    def _draw_node(self, stroke, tags):
        s = self.scale
        x0, y0, x1, y1 = G.norm_box(stroke["points"])
        shape = stroke.get("shape", "rounded")
        fill = stroke.get("fill") or "#ffffff"
        width = max(1.0, stroke["width"] * s)
        ids = []
        if shape == "ellipse":
            ids.append(self.create_oval(*self.to_screen([x0, y0, x1, y1]), fill=fill,
                                        outline=stroke["color"], width=width, tags=tags))
        else:
            poly = G.node_outline(shape, (x0, y0, x1, y1))
            ids.append(self.create_polygon(*self.to_screen(poly), fill=fill,
                                           outline=stroke["color"], width=width,
                                           joinstyle=tk.ROUND, tags=tags))
        text = stroke.get("text", "")
        if text:
            cx, cy = self.to_screen([(x0 + x1) / 2, (y0 + y1) / 2])
            inner = (x1 - x0) * (0.62 if shape in ("diamond", "ellipse") else 0.86)
            size = max(6, int(NODE_TEXT * s))
            ids.append(self.create_text(cx, cy, text=text, fill="#1f2933", justify="center",
                                        font=tkfont.Font(family=FONT, size=-size),
                                        width=max(20, inner * s), tags=tags))
        return ids

    def _draw_connector(self, stroke, tags):
        model = self.app.model
        src, dst = stroke.get("src"), stroke.get("dst")
        if not (src in model.strokes and dst in model.strokes and
                model.is_visible(src) and model.is_visible(dst)):
            return []
        (ba, sa), (bb, sb) = self._node_box(src), self._node_box(dst)
        style = stroke.get("style", "arrow")
        path = G.connector_path(ba, sa, bb, sb, style)
        s = self.scale
        width = max(1.0, stroke["width"] * s)
        kw = dict(fill=stroke["color"], width=width, capstyle=tk.ROUND, tags=tags)
        if style in ("arrow", "curve"):
            a = max(7, width * 3)
            kw.update(arrow=tk.LAST, arrowshape=(a, a * 1.2, a / 2.5))
        if style == "curve":
            kw.update(smooth=True, splinesteps=24)
        ids = [self.create_line(*self.to_screen(path), **kw)]
        label = stroke.get("label")
        if label:
            mx, my = self.to_screen(list(G.path_midpoint(path, style)))
            t = self.create_text(mx, my, text=label, fill="#1f2933",
                                 font=(FONT, max(7, int(12 * s))), tags=tags)
            bx = self.bbox(t)
            r = self.create_rectangle(bx[0] - 4, bx[1] - 1, bx[2] + 4, bx[3] + 1, fill="white",
                                      outline=C["border"], tags=tags)
            self.tag_lower(r, t)
            ids += [r, t]
        return ids

    def _render(self, stroke, committed):
        sid = stroke["id"]
        if committed:
            ids = self._draw(stroke, ("stroke", f"s:{sid}"))
            ids += self._draw_badge(sid, ids)
            self.items[sid] = ids
            self._raise_overlays()
            return ids
        return self._draw(stroke, ("prov",))

    def _draw_badge(self, sid, ids):
        voters = self.app.votes.get(sid)
        if not voters or not ids:
            return []
        bx = self.bbox(*ids)
        if not bx:
            return []
        mine = self.app.me in voters
        accent = C["accent"]
        tags = ("stroke", f"s:{sid}", "world", "badge")
        t = self.create_text(bx[2], bx[1], text=f"▲ {len(voters)}"
                             if self.app.glyphs else str(len(voters)),
                             font=(FONT, 10, "bold"), fill="white" if mine else accent,
                             tags=tags)
        tb = self.bbox(t)
        r = self.create_rectangle(tb[0] - 6, tb[1] - 2, tb[2] + 6, tb[3] + 2,
                                  fill=accent if mine else "white", outline=accent, width=1.5,
                                  tags=tags)
        self.tag_lower(r, t)
        return [r, t]

    def connectors_of(self, sids):
        sids = set(sids)
        if not sids:
            return []
        m = self.app.model
        return [c for c in m.order if m.strokes[c]["tool"] == "connector" and
                (m.strokes[c].get("src") in sids or m.strokes[c].get("dst") in sids)]

    def rerender_strokes(self, sids):
        """Redraw some committed objects in place (moved, edited, image loaded, votes
        changed), keeping their z-order; connectors attached to them follow."""
        model = self.app.model
        sids = [s for s in dict.fromkeys(sids) if s in model.strokes]
        sids += [c for c in self.connectors_of(sids) if c not in sids]
        if not sids:
            return
        index = {sid: i for i, sid in enumerate(model.order)}
        for sid in sorted(sids, key=lambda s: index.get(s, 0)):
            for it in self.items.get(sid, ()):
                self.delete(it)
            new = self._render(model.placed(sid), committed=True)
            for nxt in model.order[index[sid] + 1:]:
                if self.items.get(nxt):
                    for it in new:
                        self.tag_lower(it, self.items[nxt][0])
                    break
            if not new:
                continue
            if not model.is_visible(sid):
                for it in new:
                    self.itemconfigure(it, state="hidden")
        self._draw_selection()
        self._schedule_authors()
        self._raise_overlays()
        if self.minimap:
            self.minimap.request()

    def rerender_media(self, media_id):
        model = self.app.model
        self.rerender_strokes([sid for sid in model.order
                               if model.strokes[sid].get("media_id") == media_id])

    def _raise_overlays(self):
        for tag in ("prov", "pv", "sel", "authors", "laser", "cur"):
            self.tag_raise(tag)

    # ---------------------------------------------------------------- author tags
    def _schedule_authors(self):
        if self.app.show_authors.get() and self._authors_job is None:
            self._authors_job = self.after(150, self._draw_authors)

    def _draw_authors(self):
        """'Show authors': a small name tag on every object (who made it)."""
        self._authors_job = None
        self.delete("authors")
        if not self.app.show_authors.get():
            return
        model = self.app.model
        colors = {p["username"]: p["color"] for p in self.app.participants}
        for sid in model.visible_ids():
            bx = self.bbox(f"s:{sid}")
            user = model.strokes[sid].get("user", "?")
            if not bx:
                continue
            col = colors.get(user, "#64748b")
            t = self.create_text(bx[0], bx[1] - 2, text=user, anchor="sw", fill="white",
                                 font=(FONT, 8, "bold"), tags=("authors", "world"))
            tb = self.bbox(t)
            r = self.create_rectangle(tb[0] - 3, tb[1], tb[2] + 3, tb[3], fill=col, outline="",
                                      tags=("authors", "world"))
            self.tag_lower(r, t)
        self._raise_overlays()

    # ================================================================ model events
    def on_op(self, op, changed):
        """Called by the app after model.apply(op)."""
        kind = op["kind"]
        model = self.app.model
        mine = self.prov.pop(op["op_id"], None)
        if mine and mine[0] == "stroke":
            for it in mine[2]:
                self.delete(it)
        if kind == P.STROKE:
            sid = op["stroke"]["id"]
            self._drop_preview_for(sid)
            self._render(model.placed(sid), committed=True)
        elif kind in (P.MOVE, P.EDIT) or (kind in (P.UNDO, P.REDO) and
                                          model.kinds.get(op.get("target")) in (P.MOVE, P.EDIT)):
            self.rerender_strokes(changed)
        else:
            self.refresh_visibility(changed)
            conns = self.connectors_of(changed)
            if conns:
                self.rerender_strokes(conns)
        if mine and mine[0] == "erase":
            self.refresh_visibility(mine[1])         # erased only part (own-work-only mode)
        self.selection = [s for s in self.selection if model.is_visible(s)]
        self._draw_selection()
        self._schedule_authors()
        if self.minimap:
            self.minimap.request()

    def refresh_visibility(self, sids):
        model = self.app.model
        hidden_prov = set()
        for kind, *rest in self.prov.values():
            if kind == "erase":
                hidden_prov.update(rest[0])
        for sid in sids:
            state = "normal" if (model.is_visible(sid) and sid not in hidden_prov) else "hidden"
            for it in self.items.get(sid, ()):
                self.itemconfigure(it, state=state)

    def on_error(self, op_id):
        mine = self.prov.pop(op_id, None)
        if not mine:
            return
        if mine[0] == "stroke":
            for it in mine[2]:
                self.delete(it)
        elif mine[0] == "erase":
            self.refresh_visibility(mine[1])
            self.rerender_strokes(self.connectors_of(mine[1]))
        elif mine[0] == "move":
            self.rerender_strokes(mine[1])

    # ================================================================ previews, cursors, laser
    def on_preview(self, msg):
        key = (msg["user"], msg.get("stroke_id"))
        tool = msg.get("tool", "pen")
        p = self.previews.get(key)
        if p is None:
            p = self.previews[key] = {"items": [], "pts": []}
        if tool in SHAPE_TOOLS or tool == "polygon":
            p["pts"] = list(msg["points"]) if tool == "polygon" else list(msg["points"])[-4:]
        else:
            p["pts"].extend(msg["points"])
        p["t"] = time.time()
        for it in p["items"]:
            self.delete(it)
        p["items"] = []
        if len(p["pts"]) < 2 or (tool in SHAPE_TOOLS and len(p["pts"]) != 4) or \
                (tool == "polygon" and len(p["pts"]) < 6):
            return
        stroke = {"id": "pv", "tool": tool, "color": msg.get("stroke_color", msg["color"]),
                  "width": msg.get("width", 3), "points": p["pts"]}
        p["items"] = self._draw(stroke, ("pv",))
        if tool in SHAPE_TOOLS or tool == "polygon":
            for it in p["items"]:
                try:
                    self.itemconfigure(it, dash=(4, 3))
                except tk.TclError:
                    pass
        self._raise_overlays()

    def _drop_preview_for(self, sid):
        for key in [k for k in self.previews if k[1] == sid]:
            for it in self.previews.pop(key)["items"]:
                self.delete(it)

    def on_cursor(self, msg):
        user = msg["user"]
        x, y = self.to_screen([msg["x"], msg["y"]])
        c = self.cursors.get(user)
        if c is None:
            tags = ("cur", "world")
            arrow = self.create_polygon(0, 0, 0, 0, 0, 0, fill=msg["color"], outline="white",
                                        width=1, tags=tags)
            lab = self.create_text(0, 0, text=user, anchor="nw", fill="white",
                                   font=(FONT, 9, "bold"), tags=tags)
            bg = self.create_rectangle(0, 0, 0, 0, fill=msg["color"], outline="", tags=tags)
            self.tag_lower(bg, lab)
            c = self.cursors[user] = {"items": [arrow, lab, bg]}
        arrow, lab, bg = c["items"]
        self.coords(arrow, x, y, x + 3, y + 14, x + 11, y + 9)
        self.coords(lab, x + 12, y + 12)
        bx = self.bbox(lab)
        if bx:
            self.coords(bg, bx[0] - 4, bx[1] - 1, bx[2] + 4, bx[3] + 1)
        for it in c["items"]:
            self.itemconfigure(it, state="normal")
        c.update(t=time.time(), x=msg["x"], y=msg["y"], color=msg["color"])
        self.tag_raise("cur")
        if self.minimap:
            self.minimap.request()

    def on_laser(self, msg):
        tr = self.lasers.setdefault(msg["user"], {"pts": [], "color": msg.get("color")})
        now = time.time()
        pts = msg["points"]
        tr["pts"] += [(pts[i], pts[i + 1], now) for i in range(0, len(pts), 2)]
        tr["head"] = now
        self._start_laser()

    def _start_laser(self):
        if self.laser_job is None:
            self.laser_job = self.after(30, self._laser_tick)

    def _laser_tick(self):
        """Redraw fading laser trails (~30 fps while any trail is alive)."""
        self.laser_job = None
        self.delete("laser")
        now = time.time()
        alive = False
        for user, tr in list(self.lasers.items()):
            tr["pts"] = [p for p in tr["pts"] if now - p[2] < LASER_LIFE]
            if not tr["pts"]:
                del self.lasers[user]
                continue
            alive = True
            scr = []
            for x, y, _t in tr["pts"]:
                scr += self.to_screen([x, y])
            if len(scr) >= 4:
                self.create_line(*scr, fill="#fecaca", width=9, capstyle=tk.ROUND,
                                 joinstyle=tk.ROUND, smooth=True, tags=("laser",))
                self.create_line(*scr, fill=C["laser"], width=3.5, capstyle=tk.ROUND,
                                 joinstyle=tk.ROUND, smooth=True, tags=("laser",))
            hx, hy = scr[-2], scr[-1]
            if now - tr["pts"][-1][2] < 0.5:
                self.create_oval(hx - 7, hy - 7, hx + 7, hy + 7, fill=C["laser"],
                                 outline="#fee2e2", width=3, tags=("laser",))
                if user != self.app.me:
                    self.create_text(hx + 10, hy - 10, text=user, anchor="sw",
                                     fill=C["laser"], font=(FONT, 9, "bold"), tags=("laser",))
        self.tag_raise("laser")
        self.tag_raise("cur")
        if alive:
            self.laser_job = self.after(33, self._laser_tick)

    def remove_user(self, user):
        c = self.cursors.pop(user, None)
        if c:
            for it in c["items"]:
                self.delete(it)
        for key in [k for k in self.previews if k[0] == user]:
            for it in self.previews.pop(key)["items"]:
                self.delete(it)

    def _expire_ephemeral(self):
        now = time.time()
        for c in self.cursors.values():
            if now - c.get("t", now) > 4:
                for it in c["items"]:
                    self.itemconfigure(it, state="hidden")
        for key in [k for k, p in self.previews.items() if now - p.get("t", now) > 6]:
            for it in self.previews.pop(key)["items"]:
                self.delete(it)
        self.after(500, self._expire_ephemeral)

    # ================================================================ selection
    def set_selection(self, sids):
        model = self.app.model
        self.selection = [s for s in dict.fromkeys(sids) if model.is_visible(s)]
        self._draw_selection()
        self.app.on_selection_changed(self.selection)

    def _sel_bbox(self, sid):
        bx = self.bbox(f"s:{sid}")
        return bx

    def _draw_selection(self):
        self.delete("sel")
        if not self.selection:
            return
        boxes = []
        for sid in self.selection:
            bx = self._sel_bbox(sid)
            if bx:
                boxes.append(bx)
                self.create_rectangle(bx[0] - 4, bx[1] - 4, bx[2] + 4, bx[3] + 4,
                                      outline=C["select"], dash=(5, 3), width=1.5,
                                      tags=("sel", "world"))
        if len(self.selection) == 1 and boxes and self._resizable(self.selection[0]):
            bx = boxes[0]
            x, y = bx[2] + 4, bx[3] + 4
            self.create_rectangle(x - 6, y - 6, x + 6, y + 6, fill="white", outline=C["select"],
                                  width=2, tags=("sel", "handle", "world"))
        self.tag_raise("sel")

    def _resizable(self, sid):
        st = self.app.model.strokes.get(sid)
        return st is not None and st["tool"] in RESIZABLE and self.app.can_edit()

    def select_all(self):
        self.app.tool.set("select")
        self.set_selection(self.app.model.visible_ids())

    def delete_selection(self):
        if not self.selection or not self.app.can_edit():
            return
        sids = list(self.selection)
        op_id = self.app.net.send_board_op(P.ERASE, stroke_ids=sids)
        self.prov[op_id] = ("erase", sids)
        for sid in sids:
            for it in self.items.get(sid, ()):
                self.itemconfigure(it, state="hidden")
        self.rerender_strokes(self.connectors_of(sids))
        self.set_selection([])

    def duplicate_selection(self):
        """Ctrl+D: copies of the selected objects, offset a little (connectors are
        copied when both of their nodes are copied)."""
        if not self.selection or not self.app.can_edit():
            return
        model = self.app.model
        mapping, new_ids = {}, []
        order = sorted(self.selection, key=lambda s: model.order.index(s))
        for sid in order:
            st = dict(model.placed(sid))
            if st["tool"] == "connector":
                continue
            new = self._clean_copy(st)
            new["points"] = [round(v + 30, 1) for v in st["points"]]
            mapping[sid] = new["id"]
            self._send_stroke(new)
            new_ids.append(new["id"])
        for sid in order:
            st = model.placed(sid)
            if st["tool"] == "connector" and st["src"] in mapping and st["dst"] in mapping:
                new = self._clean_copy(st)
                new.update(src=mapping[st["src"]], dst=mapping[st["dst"]])
                self._send_stroke(new)
        self.app.flash(f"Duplicated {len(new_ids)} object(s)")

    def _clean_copy(self, st):
        keep = ("tool", "color", "width", "points", "text", "fill", "media_id", "shape", "style",
                "label", "src", "dst")
        out = {k: st[k] for k in keep if k in st}
        out["id"] = self.app.net.new_stroke_id()
        return out

    def restyle_selection(self, **changes):
        """Apply the current colour / size / fill to the selected objects (EDIT ops)."""
        if not self.selection or not self.app.can_edit():
            return False
        model = self.app.model
        for sid in self.selection:
            st = model.current(sid)
            ch = {}
            for k, v in changes.items():
                if k == "fill" and st["tool"] not in ("rect", "ellipse", "polygon", "node",
                                                      "sticky"):
                    continue
                if k == "width" and st["tool"] == "image":
                    continue
                if st.get(k) != v:
                    ch[k] = v
            if ch:
                self.app.net.send_board_op(P.EDIT, stroke_id=sid, set=ch)
        return True

    # ================================================================ input
    def _on_press(self, e):
        app = self.app
        self.focus_set()
        if self.editor:
            self.editor.commit()
        tool = app.tool.get()
        if tool == "hand" or self.space_down:
            self._pan_start(e)
            return
        if tool == "vote":
            sid = self.stroke_at(e.x, e.y)
            if sid:
                app.toggle_vote(sid)
            else:
                app.flash("Click an object (e.g. a sticky note) to vote for it")
            return
        if tool == "laser":
            if not app.can_laser():
                app.flash(app.edit_block_reason())
                return
            self.my_laser = []
            self._laser_point(e)
            return
        if tool == "select":
            self._select_press(e)
            return
        if not app.can_edit():
            app.flash(app.edit_block_reason())
            return
        x, y = self.to_world(e.x, e.y)
        if tool == "eraser":
            self.erase_hits = []
            self.last_erase = (e.x, e.y)
            self._erase_at(e.x, e.y)
            return
        if tool in ("text", "sticky"):
            self.open_editor(tool, x, y)
            return
        if tool == "node":
            self.open_editor("node", x - NODE_W / 2, y - NODE_H / 2)
            return
        if tool == "connect":
            sid = self.stroke_at(e.x, e.y, kinds=("node",))
            if sid:
                self.drag = {"mode": "connect", "src": sid}
            else:
                app.flash("Drag from one node to another to connect them")
            return
        if tool == "shape":
            kind = app.shape_kind.get()
            if kind in BOX_SHAPES:
                tool_name = "polygon"
                self.cur_box_kind = kind
            else:
                tool_name = kind
                self.cur_box_kind = None
        else:
            tool_name = tool
            self.cur_box_kind = None
        self.cur_stroke = {"id": app.net.new_stroke_id(), "tool": tool_name, "color": app.color,
                           "width": app.width.get(), "points": [x, y]}
        if tool_name in SHAPE_TOOLS or self.cur_box_kind:
            self.cur_stroke["points"] = [x, y, x, y]
            self.cur_stroke["box"] = [x, y, x, y]
            if tool_name in ("rect", "ellipse", "polygon") and app.fill_shapes.get():
                self.cur_stroke["fill"] = app.fill_color()
        self.preview_sent_upto = 0
        self._redraw_current()

    def _on_drag(self, e):
        app = self.app
        if self.pan_anchor is not None:
            self._pan_move(e)
            return
        self._maybe_send_cursor(e)
        tool = app.tool.get()
        if tool == "laser" and hasattr(self, "my_laser"):
            self._laser_point(e)
            return
        if self.drag is not None:
            self._select_drag(e)
            return
        if tool == "eraser" and app.can_edit():
            lx, ly = getattr(self, "last_erase", (e.x, e.y))
            steps = max(1, int(max(abs(e.x - lx), abs(e.y - ly)) / 4))
            for i in range(1, steps + 1):
                self._erase_at(lx + (e.x - lx) * i / steps, ly + (e.y - ly) * i / steps)
            self.last_erase = (e.x, e.y)
            return
        st = self.cur_stroke
        if st is None:
            return
        x, y = self.to_world(e.x, e.y)
        if "box" in st:
            bx = st["box"]
            if e.state & 0x0001 and st["tool"] != "line" and st["tool"] != "arrow":
                d = max(abs(x - bx[0]), abs(y - bx[1]))
                x = bx[0] + math.copysign(d, x - bx[0])
                y = bx[1] + math.copysign(d, y - bx[1])
            bx[2:4] = [x, y]
            if self.cur_box_kind:
                st["points"] = G.shape_polygon(self.cur_box_kind, *bx)
            else:
                st["points"] = list(bx)
        else:
            lx, ly = st["points"][-2], st["points"][-1]
            if (abs(x - lx) + abs(y - ly)) * self.scale < 1.5:
                return
            if len(st["points"]) >= P.MAX_POINTS - 2:
                return
            st["points"] += [x, y]
        self._redraw_current()
        self._maybe_send_preview()

    def _on_release(self, e):
        app = self.app
        if self.pan_anchor is not None:
            self._pan_end()
            return
        tool = app.tool.get()
        if tool == "laser" and hasattr(self, "my_laser"):
            self._laser_point(e, end=True)
            del self.my_laser
            return
        if self.drag is not None:
            self._select_release(e)
            return
        if tool == "eraser":
            if self.erase_hits:
                sids = list(dict.fromkeys(self.erase_hits))
                op_id = app.net.send_board_op(P.ERASE, stroke_ids=sids)
                self.prov[op_id] = ("erase", sids)
                self.rerender_strokes(self.connectors_of(sids))
            self.erase_hits = []
            return
        st = self.cur_stroke
        if st is None:
            return
        self.cur_stroke = None
        self.delete("current_stroke")
        box = st.pop("box", None)
        if box is not None and abs(box[0] - box[2]) + abs(box[1] - box[3]) < 3 / self.scale:
            return
        if st["tool"] == "pen" and app.smart_shapes.get():
            shape = recognize(st["points"])
            if shape:
                st["tool"], st["points"] = shape
                if st["tool"] != "line" and app.fill_shapes.get():
                    st["fill"] = app.fill_color()
                name = SHAPE_NAMES.get(st["tool"]) or \
                    ("triangle" if len(st["points"]) == 6 else "rectangle")
                app.flash(f"Smart shape: {name}  (Ctrl+Z to undo)", ms=2500)
        self._send_stroke(st)
        self._raise_overlays()

    def _send_stroke(self, st):
        op_id = self.app.net.send_board_op(P.STROKE, stroke=st)
        self.prov[op_id] = ("stroke", st, self._render(st, committed=False))
        return op_id

    # ---------------------------------------------------------------- select tool
    def _select_press(self, e):
        handle = self.find_withtag("handle")
        if handle and self.selection and self.app.can_edit():
            hx0, hy0, hx1, hy1 = self.coords(handle[0])
            if hx0 - 4 <= e.x <= hx1 + 4 and hy0 - 4 <= e.y <= hy1 + 4:
                sid = self.selection[0]
                st = self.app.model.placed(sid)
                xs, ys = st["points"][0::2], st["points"][1::2]
                self.drag = {"mode": "resize", "sid": sid, "box": (min(xs), min(ys),
                                                                   max(xs), max(ys)),
                             "start": self.to_world(e.x, e.y), "st": st}
                return
        sid = self.stroke_at(e.x, e.y)
        shift = e.state & 0x0001
        if sid:
            if shift:
                sel = [s for s in self.selection if s != sid] if sid in self.selection \
                    else self.selection + [sid]
                self.set_selection(sel)
            elif sid not in self.selection:
                self.set_selection([sid])
            if self.selection and self.app.can_edit():
                self.drag = {"mode": "move", "x": e.x, "y": e.y, "dx": 0.0, "dy": 0.0}
        else:
            if not shift:
                self.set_selection([])
            self.drag = {"mode": "band", "x0": e.x, "y0": e.y}

    def _select_drag(self, e):
        d = self.drag
        mode = d["mode"]
        if mode == "move":
            ddx, ddy = e.x - d["x"], e.y - d["y"]
            for sid in self.selection:
                self.move(f"s:{sid}", ddx, ddy)
            self.move("sel", ddx, ddy)
            d["x"], d["y"] = e.x, e.y
            d["dx"] += ddx / self.scale
            d["dy"] += ddy / self.scale
            moving = set(self.selection)
            self.live = {sid: (d["dx"], d["dy"]) for sid in moving}
            conns = [c for c in self.connectors_of(moving) if c not in moving]
            for c in conns:                              # connectors follow live
                for it in self.items.get(c, ()):
                    self.delete(it)
                self.items[c] = self._draw(self.app.model.placed(c), ("stroke", f"s:{c}"))
        elif mode == "band":
            self.delete("band")
            self.create_rectangle(d["x0"], d["y0"], e.x, e.y, outline=C["select"], dash=(4, 3),
                                  fill="", tags=("band",))
        elif mode == "resize":
            self.delete("resize_preview")
            st = self._resized_stroke(d, e)
            for it in self.items.get(d["sid"], ()):
                self.itemconfigure(it, state="hidden")
            self._draw(st, ("resize_preview",))
            d["result"] = st
        elif mode == "connect":
            self.delete("band")
            (bx, _shape) = self._node_box(d["src"])
            cx, cy = self.to_screen([(bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2])
            self.create_line(cx, cy, e.x, e.y, fill=C["select"], dash=(5, 3), width=2,
                             arrow=tk.LAST, tags=("band",))

    def _resized_stroke(self, d, e):
        x0, y0, x1, y1 = d["box"]
        wx, wy = self.to_world(e.x, e.y)
        nx1, ny1 = max(x0 + 10, x1 + wx - d["start"][0]), max(y0 + 10, y1 + wy - d["start"][1])
        fx = (nx1 - x0) / max(x1 - x0, 1e-6)
        fy = (ny1 - y0) / max(y1 - y0, 1e-6)
        st = dict(d["st"])
        if st["tool"] == "image" or e.state & 0x0001:      # keep the aspect ratio
            fx = fy = max(fx, fy)
        if x1 - x0 < 1e-6:
            fx = 1
        if y1 - y0 < 1e-6:
            fy = 1
        pts = st["points"]
        st["points"] = [round(x0 + (v - x0) * fx, 1) if i % 2 == 0 else
                        round(y0 + (v - y0) * fy, 1) for i, v in enumerate(pts)]
        return st

    def _select_release(self, e):
        d, self.drag = self.drag, None
        mode = d["mode"]
        self.delete("band")
        self.live = {}
        if mode == "move":
            dx, dy = round(d["dx"], 1), round(d["dy"], 1)
            if abs(dx) + abs(dy) >= 0.5:
                sids = list(self.selection)
                op_id = self.app.net.send_board_op(P.MOVE, stroke_ids=sids, dx=dx, dy=dy)
                self.prov[op_id] = ("move", sids, dx, dy)
        elif mode == "band":
            x0, x1 = sorted((d["x0"], e.x))
            y0, y1 = sorted((d["y0"], e.y))
            if x1 - x0 < 3 and y1 - y0 < 3:
                return
            found = []
            for it in self.find_enclosed(x0, y0, x1, y1):
                if self.itemcget(it, "state") == "hidden":
                    continue
                for tag in self.gettags(it):
                    if tag.startswith("s:"):
                        found.append(tag[2:])
            sel = self.selection + found if e.state & 0x0001 else found
            self.set_selection(sel)
        elif mode == "resize":
            self.delete("resize_preview")
            sid = d["sid"]
            st = d.get("result")
            if st is None:
                self.refresh_visibility([sid])
                return
            ox, oy = self.app.model.offset(sid)
            base = [round(v - (ox if i % 2 == 0 else oy), 1) for i, v in enumerate(st["points"])]
            self.app.net.send_board_op(P.EDIT, stroke_id=sid, set={"points": base})
            self.refresh_visibility([sid])
        elif mode == "connect":
            dst = self.stroke_at(e.x, e.y, kinds=("node",))
            if dst and dst != d["src"]:
                self.make_connector(d["src"], dst, self.app.edge_style.get())
            elif dst is None:
                self.app.flash("Release over another node to connect")

    def make_connector(self, src, dst, style, label=""):
        st = {"id": self.app.net.new_stroke_id(), "tool": "connector", "color": "#475569",
              "width": 2, "points": [0, 0, 1, 1], "src": src, "dst": dst, "style": style}
        if label:
            st["label"] = label
        # points are only a fallback: a connector is always drawn between its two nodes,
        # which may still be in flight to the server (sent just before, so ordered first)
        self.app.net.send_board_op(P.STROKE, stroke=st)
        return st["id"]

    # ---------------------------------------------------------------- inline text editor
    def open_editor(self, kind, x, y, sid=None, initial="", on_done=None):
        """Type directly on the board. kind: text | sticky | node | label | edit."""
        if self.editor:
            self.editor.commit()
        self.editor = InlineEditor(self, kind, x, y, sid, initial, on_done)

    def _editor_done(self, ed, text, how):
        self.editor = None
        app = self.app
        if ed.on_done:
            ed.on_done(text, how)
            return
        if how == "cancel":
            return
        if ed.sid:                                      # editing an existing object
            st = app.model.current(ed.sid)
            if ed.kind == "label":
                if text != st.get("label", ""):
                    app.net.send_board_op(P.EDIT, stroke_id=ed.sid, set={"label": text or None})
            elif text != st.get("text", ""):
                if not text and st["tool"] in ("text", "sticky"):
                    op_id = app.net.send_board_op(P.ERASE, stroke_ids=[ed.sid])
                    self.prov[op_id] = ("erase", [ed.sid])
                else:
                    app.net.send_board_op(P.EDIT, stroke_id=ed.sid, set={"text": text})
            return
        if ed.kind == "node":
            st = {"id": app.net.new_stroke_id(), "tool": "node", "color": app.color_for_node(),
                  "width": 2, "points": [ed.x, ed.y, ed.x + NODE_W, ed.y + NODE_H],
                  "shape": app.node_shape.get(), "text": text}
            if app.fill_shapes.get():
                st["fill"] = app.fill_color()
            self._send_stroke(st)
            return
        if not text:
            return
        st = {"id": app.net.new_stroke_id(), "tool": ed.kind, "color": app.color,
              "width": app.width.get(), "points": [ed.x, ed.y], "text": text}
        if ed.kind == "sticky":
            st["fill"] = app.sticky_fill()
            st["color"] = "#b08900"
        self._send_stroke(st)

    def edit_object(self, sid):
        """Double-click / Enter on an object: edit its text or label in place."""
        model = self.app.model
        if not self.app.can_edit() or not model.is_visible(sid):
            return
        st = model.placed(sid)
        tool = st["tool"]
        if tool in ("text", "sticky"):
            self.open_editor(tool, st["points"][0], st["points"][1], sid=sid,
                             initial=st.get("text", ""))
        elif tool == "node":
            x0, y0, _x1, _y1 = G.norm_box(st["points"])
            self.open_editor("node", x0, y0, sid=sid, initial=st.get("text", ""))
        elif tool == "connector":
            geo = G.connector_geometry(model, st)
            if geo:
                mx, my = geo[1]
                self.open_editor("label", mx - 60, my - 14, sid=sid,
                                 initial=st.get("label", ""))

    # ---------------------------------------------------------------- mind map
    def mindmap_add(self, sibling=False):
        """Tab: child of the selected node. Enter: sibling (same parent)."""
        model = self.app.model
        if len(self.selection) != 1 or not self.app.can_edit():
            return False
        sel = self.selection[0]
        st = model.placed(sel)
        if st["tool"] != "node":
            return False
        x0, y0, x1, y1 = G.norm_box(st["points"])
        w, h = x1 - x0, y1 - y0
        parent = sel
        if sibling:
            parents = [model.strokes[c]["src"] for c in self.connectors_of([sel])
                       if model.strokes[c].get("dst") == sel and model.is_visible(c)]
            if not parents:
                nx, ny = x0, y1 + 40
                parent = None
            else:
                parent = parents[0]
        if parent is not None:
            pb = G.norm_box(model.placed(parent)["points"])
            kids = [model.strokes[c]["dst"] for c in self.connectors_of([parent])
                    if model.strokes[c].get("src") == parent and model.is_visible(c)
                    and model.is_visible(model.strokes[c]["dst"])]
            if kids:
                bottoms = [G.norm_box(model.placed(k)["points"])[3] for k in kids]
                nx = G.norm_box(model.placed(kids[0])["points"])[0]
                ny = max(bottoms) + 30
            else:
                nx, ny = pb[2] + 110, (pb[1] + pb[3]) / 2 - h / 2

        shape = st.get("shape", "rounded")

        def done(text, how):
            if how == "cancel":
                return
            nid = self.app.net.new_stroke_id()
            node = {"id": nid, "tool": "node", "color": st["color"], "width": st["width"],
                    "points": [nx, ny, nx + w, ny + h], "shape": shape, "text": text}
            if st.get("fill"):
                node["fill"] = st["fill"]
            self._send_stroke(node)
            if parent is not None:
                self.make_connector(parent, nid, "curve")
            self.after(80, lambda: self._after_mindmap(nid, how))
        self.open_editor("node", nx, ny, initial="", on_done=done)
        self.editor.box = (w, h)
        self.editor.reposition()
        return True

    def _after_mindmap(self, nid, how):
        if self.app.model.is_visible(nid) or any(
                e[0] == "stroke" and e[1]["id"] == nid for e in self.prov.values()):
            self.selection = [nid]
            self.app.model.is_visible(nid) and self._draw_selection()
            if how == "tab":
                self.after(60, lambda: self.app.model.is_visible(nid) and self.mindmap_add())

    # ---------------------------------------------------------------- laser
    def _laser_point(self, e, end=False):
        x, y = self.to_world(e.x, e.y)
        self.my_laser.append((x, y))
        tr = self.lasers.setdefault(self.app.me, {"pts": [], "color": C["laser"]})
        tr["pts"].append((x, y, time.time()))
        self._start_laser()
        now = time.monotonic()
        if end or now - self.last_preview_sent > 0.04:
            self.last_preview_sent = now
            pts = [v for p in self.my_laser[-P.MAX_LASER_POINTS:] for v in p]
            self.my_laser = []
            self.app.net.send({"type": P.LASER, "points": pts, "end": end})

    # ---------------------------------------------------------------- live preview, cursor
    def _redraw_current(self):
        self.delete("current_stroke")
        if self.cur_stroke:
            st = self.cur_stroke
            if st["tool"] == "polygon" and len(st["points"]) < 6:
                return
            self._draw(st, ("current_stroke", "prov"))

    def _maybe_send_preview(self):
        now = time.monotonic()
        if now - self.last_preview_sent < 0.05:
            return
        self.last_preview_sent = now
        st = self.cur_stroke
        if st["tool"] in SHAPE_TOOLS or st["tool"] == "polygon":
            pts = st["points"]
        else:
            start = max(0, self.preview_sent_upto - 2)
            pts = st["points"][start:]
            self.preview_sent_upto = len(st["points"])
        self.app.net.send({"type": P.STROKE_PART, "stroke_id": st["id"], "tool": st["tool"],
                           "color": st["color"], "width": st["width"], "points": pts[-2000:]})

    def _maybe_send_cursor(self, e):
        now = time.monotonic()
        if now - self.last_cursor_sent < 0.05:
            return
        self.last_cursor_sent = now
        x, y = self.to_world(e.x, e.y)
        self.app.net.send({"type": P.CURSOR, "x": x, "y": y})

    def _on_motion(self, e):
        self._maybe_send_cursor(e)
        sid = self.stroke_at(e.x, e.y)
        if sid:
            self.app.set_hover(self.describe(sid))
        else:
            x, y = self.to_world(e.x, e.y)
            self.app.set_hover(f"x {x:.0f}  y {y:.0f}")

    def describe(self, sid):
        """'Rectangle by asha - last edited by ravi - 2 votes' (contributor identification)."""
        model = self.app.model
        s = model.strokes.get(sid, {})
        names = {"pen": "Drawing", "highlighter": "Highlight", "rect": "Rectangle",
                 "ellipse": "Ellipse", "polygon": "Shape", "line": "Line", "arrow": "Arrow",
                 "text": "Text", "sticky": "Note", "image": "Image", "node": "Node",
                 "connector": "Connector"}
        out = f"{names.get(s.get('tool'), 'Object')} by {s.get('user', '?')}"
        le = model.last_editor(sid)
        if le and le[0] and le[0] != s.get("user"):
            out += f"  ·  last edited by {le[0]}" if self.app.glyphs else \
                f"  -  last edited by {le[0]}"
        elif le and le[0]:
            out += "  (edited)"
        v = len(self.app.votes.get(sid, ()))
        if v:
            out += f"  -  {v} vote{'s' if v != 1 else ''}"
        return out

    def stroke_at(self, x, y, r=3, kinds=None):
        for it in reversed(self.find_overlapping(x - r, y - r, x + r, y + r)):
            if self.itemcget(it, "state") == "hidden":
                continue
            for tag in self.gettags(it):
                if tag.startswith("s:"):
                    sid = tag[2:]
                    if kinds and self.app.model.strokes.get(sid, {}).get("tool") not in kinds:
                        break
                    return sid
        return None

    def _on_double(self, e):
        """Tk delivers a quick second press as <Double-Button-1> INSTEAD of
        <ButtonPress-1>. Double-clicking text, a note, a node or a connector edits it;
        any other double press is handled as a normal press (so you can click an
        object and immediately drag it, or draw two quick dots)."""
        sid = self.stroke_at(e.x, e.y)
        editable = sid and self.app.model.strokes.get(sid, {}).get("tool") in (
            "text", "sticky", "node", "connector")
        if editable and self.app.tool.get() in ("select", "text", "sticky", "node") \
                and self.app.can_edit():
            if self.editor:
                self.editor.cancel()
            self.drag = None
            self.edit_object(sid)
            return
        self._on_press(e)

    def _context_menu(self, e):
        sid = self.stroke_at(e.x, e.y, r=4)
        if not sid:
            return
        app = self.app
        if sid not in self.selection:
            self.set_selection([sid])
        voted = app.me in app.votes.get(sid, ())
        tool = app.model.strokes[sid]["tool"]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Remove my vote" if voted else "Vote for this",
                      command=lambda: app.toggle_vote(sid))
        if app.can_edit():
            if tool in ("text", "sticky", "node"):
                m.add_command(label="Edit text", command=lambda: self.edit_object(sid))
            if tool == "connector":
                m.add_command(label="Edit label", command=lambda: self.edit_object(sid))
                sub = tk.Menu(m, tearoff=0)
                for st in P.CONNECTOR_STYLES:
                    sub.add_command(label=st.title(), command=lambda st=st: app.net.send_board_op(
                        P.EDIT, stroke_id=sid, set={"style": st}))
                m.add_cascade(label="Style", menu=sub)
            if tool == "node":
                sub = tk.Menu(m, tearoff=0)
                for sh in P.NODE_SHAPES:
                    sub.add_command(label=sh.title(), command=lambda sh=sh: app.net.send_board_op(
                        P.EDIT, stroke_id=sid, set={"shape": sh}))
                m.add_cascade(label="Node shape", menu=sub)
                m.add_command(label="Add child (Tab)", command=lambda: self.mindmap_add())
            m.add_command(label="Apply current colour",
                          command=lambda: self.restyle_selection(color=app.color))
            m.add_command(label="Duplicate (Ctrl+D)", command=self.duplicate_selection)
            m.add_separator()
            m.add_command(label="Delete (Del)", command=self.delete_selection)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _erase_at(self, x, y):
        r = max(6, self.app.width.get() * self.scale * 1.5)
        hidden = []
        for it in self.find_overlapping(x - r, y - r, x + r, y + r):
            if self.itemcget(it, "state") == "hidden":
                continue
            for tag in self.gettags(it):
                if tag.startswith("s:"):
                    sid = tag[2:]
                    if self.app.model.is_visible(sid):
                        self.erase_hits.append(sid)
                        hidden.append(sid)
        for sid in hidden:
            for it in self.items.get(sid, ()):
                self.itemconfigure(it, state="hidden")


class InlineEditor:
    """A text box placed directly on the board at a world position.
    Enter = done, Shift+Enter = new line, Esc = cancel, Tab = done (+ next node)."""

    def __init__(self, board, kind, x, y, sid, initial, on_done):
        self.board, self.kind, self.x, self.y = board, kind, x, y
        self.sid, self.on_done = sid, on_done
        self.box = (NODE_W, NODE_H) if kind == "node" else None
        self.done = False
        app = board.app
        size = {"node": NODE_TEXT, "label": 12}.get(kind)
        if size is None:
            w = app.model.strokes[sid]["width"] if sid else app.width.get()
            size = text_font_size(w)
        self.size = size
        self.text = tk.Text(board, wrap="word", undo=True, relief="flat", padx=6, pady=4,
                            highlightthickness=2, highlightcolor=C["accent"],
                            highlightbackground=C["accent"],
                            background="#fffbea" if kind == "sticky" else "white",
                            foreground="#1f2933", insertbackground=C["accent"])
        self.text.insert("1.0", initial)
        self.text.bind("<Return>", self._enter)
        self.text.bind("<KP_Enter>", self._enter)
        self.text.bind("<Escape>", lambda e: self.cancel() or "break")
        self.text.bind("<Tab>", lambda e: self.commit("tab") or "break")
        self.text.bind("<FocusOut>", lambda e: self.board.after(10, self.commit))
        self.win = board.create_window(0, 0, window=self.text, anchor="nw", tags=("editor",))
        self.reposition()
        self.text.focus_set()
        self.text.tag_add("sel", "1.0", "end")
        hint = {"node": "Type the node text - Enter: done, Tab: done + add a child",
                "label": "Connector label - Enter: done"}.get(
            kind, "Type, then Enter (Shift+Enter: new line, Esc: cancel)")
        app.flash(hint, ms=4000)

    def reattach(self):
        self.win = self.board.create_window(0, 0, window=self.text, anchor="nw",
                                            tags=("editor",))
        self.reposition()

    def reposition(self):
        b = self.board
        sx, sy = b.to_screen([self.x, self.y])
        s = b.scale
        self.text.configure(font=(FONT, -max(9, int(self.size * s))))
        if self.box:
            w, h = self.box[0] * s, self.box[1] * s
        elif self.kind == "label":
            w, h = 160 * s, 30 * s
        else:
            w, h = (260 if self.kind == "sticky" else 420) * s, \
                (130 if self.kind == "sticky" else self.size * 3.2) * s
        b.coords(self.win, sx, sy)
        b.itemconfigure(self.win, width=max(120, w), height=max(34, h))
        b.tag_raise("editor")

    def _enter(self, e):
        if e.state & 0x0001:
            return None                       # Shift+Enter: newline
        self.commit()
        return "break"

    def commit(self, how="commit"):
        if self.done:
            return
        self.done = True
        txt = self.text.get("1.0", "end-1c").strip()[:P.MAX_TEXT_LEN]
        self._close()
        self.board._editor_done(self, txt if self.kind != "label" else txt[:P.MAX_LABEL_LEN],
                                how)

    def cancel(self):
        if self.done:
            return
        self.done = True
        self._close()
        self.board._editor_done(self, "", "cancel")

    def _close(self):
        try:
            self.board.delete(self.win)
            self.text.destroy()
        except tk.TclError:
            pass
        self.board.focus_set()


class Minimap(tk.Canvas):
    """Radar overview in the corner: pages, all content, everyone's cursor and the
    current view. Click or drag to jump there."""

    W, H = 210, 132

    def __init__(self, board):
        super().__init__(board, width=self.W, height=self.H, background="#f8fafc",
                         highlightthickness=1, highlightbackground=C["border"], cursor="hand2")
        self.board = board
        self.job = None
        self.map = None
        self.bind("<ButtonPress-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)

    def request(self):
        if self.job is None and self.winfo_ismapped():
            self.job = self.after(120, self.redraw)

    def redraw(self):
        self.job = None
        b = self.board
        model = b.app.model
        self.delete("all")
        vx, vy, vw, vh = b.view_rect()
        boxes = [model.bounds(sid) for sid in model.visible_ids()]
        pages = [G.page_rect(i) for i in range(b.app.settings.get("pages", 1))]
        xs = [vx, vx + vw] + [v for bx in boxes for v in (bx[0], bx[2])] + \
            [v for p in pages for v in (p[0], p[0] + p[2])]
        ys = [vy, vy + vh] + [v for bx in boxes for v in (bx[1], bx[3])] + \
            [v for p in pages for v in (p[1], p[1] + p[3])]
        cur = [c for c in b.cursors.values() if "x" in c and time.time() - c["t"] < 4]
        xs += [c["x"] for c in cur]
        ys += [c["y"] for c in cur]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        pad = 0.06 * max(x1 - x0, y1 - y0, 1)
        x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
        s = min((self.W - 8) / max(x1 - x0, 1), (self.H - 8) / max(y1 - y0, 1))
        ox = (self.W - (x1 - x0) * s) / 2 - x0 * s
        oy = (self.H - (y1 - y0) * s) / 2 - y0 * s
        self.map = (ox, oy, s)
        for p in pages:
            self.create_rectangle(ox + p[0] * s, oy + p[1] * s, ox + (p[0] + p[2]) * s,
                                  oy + (p[1] + p[3]) * s, fill="white", outline=C["border"])
        step = max(1, len(boxes) // 600)
        for bx in boxes[::step]:
            self.create_rectangle(ox + bx[0] * s, oy + bx[1] * s,
                                  max(ox + bx[2] * s, ox + bx[0] * s + 1.5),
                                  max(oy + bx[3] * s, oy + bx[1] * s + 1.5),
                                  fill="#94a3b8", outline="")
        self.create_rectangle(ox + vx * s, oy + vy * s, ox + (vx + vw) * s, oy + (vy + vh) * s,
                              outline=C["accent"], width=2)
        for c in cur:
            cx, cy = ox + c["x"] * s, oy + c["y"] * s
            self.create_oval(cx - 3.5, cy - 3.5, cx + 3.5, cy + 3.5, fill=c["color"],
                             outline="white")
        self.create_text(6, 4, text="map", anchor="nw", fill=C["faint"], font=(FONT, 8))

    def _jump(self, e):
        if self.map is None:
            return
        if self.board.app.following():
            self.board.app.flash(self.board.app.follow_block_reason())
            return
        ox, oy, s = self.map
        self.board.center_on((e.x - ox) / s, (e.y - oy) / s)
        self.redraw()
