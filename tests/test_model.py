"""Board model: visibility rule and multi-user undo semantics."""
import random

import helpers  # noqa: F401
from common.model import BoardModel


class Log:
    def __init__(self):
        self.seq = 0
        self.entries = []

    def add(self, kind, user, op_id, **kw):
        self.seq += 1
        e = {"kind": kind, "user": user, "op_id": op_id, "seq": self.seq, **kw}
        self.entries.append(e)
        return e


def stroke(sid):
    return {"id": sid, "tool": "pen", "color": "#000000", "width": 2, "points": [0, 0, 5, 5]}


def run(entries):
    m = BoardModel()
    for e in entries:
        m.apply(e)
    return m


def test_design_doc_example():
    """Alice draws S, Bob erases S, Alice undoes, Bob undoes -> S stays hidden."""
    log = Log()
    log.add("STROKE", "alice", "a1", stroke=stroke("S"))
    log.add("ERASE", "bob", "b1", stroke_ids=["S"])
    m = run(log.entries)
    assert m.visible_ids() == []
    log.add("UNDO", "alice", "a2", target="a1")       # alice undoes her stroke
    log.add("UNDO", "bob", "b2", target="b1")         # bob undoes his erase
    m = run(log.entries)
    assert m.visible_ids() == []                      # creator op inactive -> hidden
    log.add("REDO", "alice", "a3", target="a1")
    assert run(log.entries).visible_ids() == ["S"]


def test_undo_clear_restores_everything():
    log = Log()
    for i in range(5):
        log.add("STROKE", "u", f"s{i}", stroke=stroke(f"S{i}"))
    log.add("CLEAR", "host", "c1")
    assert run(log.entries).visible_ids() == []
    log.add("STROKE", "u", "s9", stroke=stroke("S9"))     # drawn after the clear
    log.add("UNDO", "host", "c2", target="c1")
    assert run(log.entries).visible_ids() == ["S0", "S1", "S2", "S3", "S4", "S9"]


def test_clear_only_covers_strokes_visible_at_that_time():
    log = Log()
    log.add("STROKE", "a", "1", stroke=stroke("A"))
    log.add("STROKE", "a", "2", stroke=stroke("B"))
    log.add("UNDO", "a", "3", target="2")              # B hidden before the clear
    log.add("CLEAR", "h", "4")
    log.add("REDO", "a", "5", target="2")              # B comes back: clear never covered it
    assert run(log.entries).visible_ids() == ["B"]


def test_z_order_is_seq_order():
    log = Log()
    for sid in "XYZ":
        log.add("STROKE", "u", sid.lower(), stroke=stroke(sid))
    assert run(log.entries).visible_ids() == ["X", "Y", "Z"]


def test_snapshot_roundtrip_equals_replay():
    rnd = random.Random(7)
    log = Log()
    ops = []
    for i in range(400):
        r = rnd.random()
        if r < 0.6 or not ops:
            op = f"s{i}"
            log.add("STROKE", "u", op, stroke=stroke(f"S{i}"))
            ops.append(op)
        elif r < 0.75:
            log.add("ERASE", "u", f"e{i}", stroke_ids=[f"S{rnd.randrange(i)}"])
        elif r < 0.8:
            log.add("CLEAR", "u", f"c{i}")
        elif r < 0.9:
            log.add("UNDO", "u", f"u{i}", target=rnd.choice(ops))
        else:
            log.add("REDO", "u", f"r{i}", target=rnd.choice(ops))
    m = run(log.entries)
    m2 = BoardModel.from_dict(m.to_dict())
    assert m.visible_ids() == m2.visible_ids()
    assert m.fingerprint() == m2.fingerprint()
    # snapshot half-way + replay of the rest == full replay (late joiner path)
    half = run(log.entries[:200])
    late = BoardModel.from_dict(half.to_dict())
    for e in log.entries[200:]:
        late.apply(e)
    assert late.fingerprint() == m.fingerprint()


def test_duplicate_stroke_id_ignored():
    log = Log()
    log.add("STROKE", "a", "1", stroke=stroke("A"))
    log.add("STROKE", "a", "2", stroke=stroke("A"))
    m = run(log.entries)
    assert m.visible_ids() == ["A"]
