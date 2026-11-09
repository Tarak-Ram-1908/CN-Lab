"""
The whiteboard state model, shared by server and client.

Both sides run exactly this code to apply operations, which is the core
guarantee that every canvas ends up identical: the server assigns one global
order (seq) and every participant applies the same operations, in the same
order, with the same deterministic function.

State
-----
* strokes      stroke_id -> stroke dict (tool, colour, width, points, ...).
               Strokes are never deleted, only hidden, so undo is possible.
* order        stroke ids in creation (seq) order = drawing z-order.
* active       op_id -> bool. False once that operation has been undone.
* creator      stroke_id -> op_id of the STROKE op that created it.
* covers       op_id of an ERASE/CLEAR -> stroke ids it hid.
* hidden_by    stroke_id -> op_ids of ERASE/CLEAR ops that cover it.
* moves        op_id of a MOVE -> (stroke ids, dx, dy).
* moved_by     stroke_id -> op_ids of MOVE ops that apply to it.
* edits        op_id of an EDIT -> (stroke id, {field: new value}).
* edited_by    stroke_id -> op_ids of EDIT ops that apply to it.

Editing objects
---------------
EDIT changes properties (colour, width, fill, text, label, size/points ...).
Like moves, edits never overwrite the stored stroke: the current look is
    stored stroke  <-  every *active* EDIT for it, applied in seq order,
so undo/redo of an edit is again just flipping `active`, and two people
recolouring the same note at once end up with the same (later) colour on
every screen.

Moving objects
--------------
A stroke's stored points never change. Its position on the board is
    points + sum of (dx, dy) over the *active* MOVE ops that include it,
so a move is undone exactly like anything else: UNDO sets the MOVE op
inactive and the object jumps back. Two people dragging the same note at once
is also deterministic: both moves are applied, in seq order, on every client.

Visibility rule
---------------
A stroke is visible  <=>  its creating op is active
                     AND  no *active* ERASE/CLEAR op covers it.

UNDO/REDO simply flip `active` for a target op, so undoing an erase or a
clear brings the hidden strokes back, and undoing a stroke hides it.
"""

STROKE, MOVE, EDIT, ERASE, CLEAR, UNDO, REDO = ("STROKE", "MOVE", "EDIT", "ERASE", "CLEAR",
                                              "UNDO", "REDO")


class BoardModel:
    def __init__(self):
        self.strokes = {}
        self.order = []
        self.active = {}
        self.creator = {}
        self.covers = {}
        self.hidden_by = {}
        self.kinds = {}        # op_id -> kind (needed to resolve UNDO targets)
        self.owners = {}       # op_id -> username
        self.moves = {}        # op_id -> [stroke_ids, dx, dy]
        self.moved_by = {}     # stroke_id -> [move op_ids]
        self.edits = {}        # op_id -> [stroke_id, {field: value}]
        self.edited_by = {}    # stroke_id -> [edit op_ids]
        self.op_seq = {}       # op_id -> seq (for "last edited by")
        self.seq = 0           # seq of the last applied op

    # ------------------------------------------------------------------ queries
    def is_visible(self, sid):
        op = self.creator.get(sid)
        if op is None or not self.active.get(op, False):
            return False
        return not any(self.active.get(c, False) for c in self.hidden_by.get(sid, ()))

    def visible_ids(self):
        """Visible stroke ids in z-order (bottom first)."""
        return [sid for sid in self.order if self.is_visible(sid)]

    def offset(self, sid):
        """Total (dx, dy) of the active MOVE ops applied to a stroke."""
        dx = dy = 0.0
        for op in self.moved_by.get(sid, ()):
            if self.active.get(op, False):
                dx += self.moves[op][1]
                dy += self.moves[op][2]
        return dx, dy

    def current(self, sid):
        """The stroke with its active edits applied (not yet moved)."""
        s = self.strokes[sid]
        ops = [op for op in self.edited_by.get(sid, ()) if self.active.get(op, False)]
        if not ops:
            return s
        s = dict(s)
        for op in ops:
            for k, v in self.edits[op][1].items():
                if v is None:
                    s.pop(k, None)
                else:
                    s[k] = v
        return s

    def placed(self, sid):
        """The stroke as it looks now: edits applied, then moved into position."""
        s = self.current(sid)
        dx, dy = self.offset(sid)
        if not dx and not dy:
            return s
        s = dict(s)
        pts = s["points"]
        s["points"] = [round(v + (dx if i % 2 == 0 else dy), 1) for i, v in enumerate(pts)]
        return s

    def last_editor(self, sid):
        """(user, seq) of the latest active MOVE/EDIT of a stroke, or None."""
        best = None
        for op in list(self.moved_by.get(sid, ())) + list(self.edited_by.get(sid, ())):
            if self.active.get(op, False):
                q = self.op_seq.get(op, 0)
                if best is None or q > best[1]:
                    best = (self.owners.get(op), q)
        return best

    def visible_strokes(self):
        """Visible strokes, bottom first, at their current positions."""
        return [self.placed(sid) for sid in self.visible_ids()]

    def bounds(self, sid, text_w=240, text_h=120):
        """Approximate world bounding box (x0, y0, x1, y1) of a placed stroke."""
        s = self.placed(sid)
        xs, ys = s["points"][0::2], s["points"][1::2]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        if s["tool"] in ("text", "sticky"):
            x1, y1 = x0 + text_w, y0 + text_h
        pad = s.get("width", 1) / 2
        return x0 - pad, y0 - pad, x1 + pad, y1 + pad

    def affected_by(self, op_id):
        """Stroke ids whose visibility depends on op_id."""
        kind = self.kinds.get(op_id)
        if kind == STROKE:
            return [sid for sid, op in self.creator.items() if op == op_id]
        if kind in (ERASE, CLEAR):
            return list(self.covers.get(op_id, ()))
        if kind == MOVE:
            return list(self.moves[op_id][0])
        if kind == EDIT:
            return [self.edits[op_id][0]]
        return []

    # ------------------------------------------------------------------ apply
    def apply(self, entry):
        """Apply one logged operation and return the stroke ids that changed.

        `entry` is the server's log entry: {seq, user, op_id, kind, ...}.
        The caller guarantees entries arrive in strictly increasing seq order.
        """
        kind = entry["kind"]
        op_id = entry["op_id"]
        user = entry.get("user")
        self.seq = max(self.seq, entry.get("seq", self.seq))
        if kind != UNDO and kind != REDO:
            self.op_seq[op_id] = entry.get("seq", self.seq)
        changed = []

        if kind == STROKE:
            stroke = dict(entry["stroke"])
            stroke["user"] = user
            stroke["seq"] = entry.get("seq")
            sid = stroke["id"]
            if sid in self.strokes:              # duplicate id: ignore safely
                return changed
            self.strokes[sid] = stroke
            self.order.append(sid)
            self.creator[sid] = op_id
            self.active[op_id] = True
            self.kinds[op_id] = kind
            self.owners[op_id] = user
            changed.append(sid)

        elif kind in (ERASE, CLEAR):
            if kind == ERASE:
                # Only strokes that are visible right now can be erased.
                targets = [s for s in entry.get("stroke_ids", []) if self.is_visible(s)]
            else:
                targets = self.visible_ids()     # clear = erase everything visible
            self.covers[op_id] = targets
            for sid in targets:
                self.hidden_by.setdefault(sid, []).append(op_id)
            self.active[op_id] = True
            self.kinds[op_id] = kind
            self.owners[op_id] = user
            changed.extend(targets)

        elif kind == MOVE:
            targets = [s for s in entry.get("stroke_ids", []) if s in self.strokes]
            self.moves[op_id] = [targets, float(entry.get("dx", 0)), float(entry.get("dy", 0))]
            for sid in targets:
                self.moved_by.setdefault(sid, []).append(op_id)
            self.active[op_id] = True
            self.kinds[op_id] = kind
            self.owners[op_id] = user
            changed.extend(targets)

        elif kind == EDIT:
            sid = entry.get("stroke_id")
            if sid not in self.strokes:
                return changed
            self.edits[op_id] = [sid, dict(entry.get("set", {}))]
            self.edited_by.setdefault(sid, []).append(op_id)
            self.active[op_id] = True
            self.kinds[op_id] = kind
            self.owners[op_id] = user
            changed.append(sid)

        elif kind in (UNDO, REDO):
            target = entry["target"]
            if target in self.active:
                self.active[target] = (kind == REDO)
                changed.extend(self.affected_by(target))
        return changed

    # ------------------------------------------------------------------ (de)serialise
    def to_dict(self):
        """Compact snapshot sent to late joiners and written to save files."""
        return {
            "seq": self.seq,
            "strokes": [self.strokes[s] for s in self.order],
            "active": self.active,
            "creator": self.creator,
            "covers": self.covers,
            "kinds": self.kinds,
            "owners": self.owners,
            "moves": self.moves,
            "edits": self.edits,
            "op_seq": self.op_seq,
        }

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.seq = d.get("seq", 0)
        for s in d.get("strokes", []):
            m.strokes[s["id"]] = s
            m.order.append(s["id"])
        m.active = dict(d.get("active", {}))
        m.creator = dict(d.get("creator", {}))
        m.covers = {k: list(v) for k, v in d.get("covers", {}).items()}
        m.kinds = dict(d.get("kinds", {}))
        m.owners = dict(d.get("owners", {}))
        for op_id, sids in m.covers.items():
            for sid in sids:
                m.hidden_by.setdefault(sid, []).append(op_id)
        m.moves = {k: [list(v[0]), float(v[1]), float(v[2])]
                   for k, v in d.get("moves", {}).items()}
        for op_id, (sids, _dx, _dy) in m.moves.items():
            for sid in sids:
                m.moved_by.setdefault(sid, []).append(op_id)
        m.op_seq = dict(d.get("op_seq", {}))
        m.edits = {k: [v[0], dict(v[1])] for k, v in d.get("edits", {}).items()}
        # edits must be re-applied in seq order
        for op_id in sorted(m.edits, key=lambda o: m.op_seq.get(o, 0)):
            m.edited_by.setdefault(m.edits[op_id][0], []).append(op_id)
        for sid in m.moved_by:
            m.moved_by[sid].sort(key=lambda o: m.op_seq.get(o, 0))
        return m

    def fingerprint(self):
        """Hash of what is visible: equal fingerprints => identical canvases."""
        import hashlib
        import json
        data = json.dumps([[s["id"], s["tool"], s["color"], s["points"], s.get("text"),
                            s.get("fill"), s.get("width")]
                           for s in self.visible_strokes()], separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()[:16]
