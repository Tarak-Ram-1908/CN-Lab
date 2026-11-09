"""Tests for the classroom / collaboration features added in protocol v2:
editing objects, own-work-only mode, flowchart nodes and connectors, room
settings, private messages, raised hands, quick polls, restore from file,
version history, ending a session, laser pointer, version check, frame
compression, PBKDF2 passwords and TLS."""
import os
import time
import shutil
import socket
import struct
import subprocess
import tempfile
import zlib

import pytest

from helpers import Bot, P, start_server, wait_until
from common.model import BoardModel


@pytest.fixture
def server():
    srv = start_server()
    yield srv
    srv.stop()


def ev(bot, typ):
    return [e for e in bot.other if e["type"] == typ]


def errors(bot):
    return [e["message"] for e in bot.errors]


def rect(bot, x=10, y=10, color="#1d70b8"):
    return bot.stroke([x, y, x + 100, y + 60], tool="rect", color=color)


def node(bot, x, y, text="Start", shape="rounded"):
    return bot.stroke([x, y, x + 160, y + 70], tool="node", color="#334155", text=text,
                      shape=shape)


# --------------------------------------------------------------------------- framing / security
def test_large_frames_are_compressed_and_bombs_rejected():
    msg = {"type": "X", "data": ["stroke"] * 40000}
    frame = P.encode(msg)
    (raw,) = P.HEADER.unpack(frame[:4])
    assert raw & P.COMPRESSED_FLAG                      # flag bit set
    assert len(frame) < len(str(msg)) / 10              # repetitive JSON compresses well
    assert P.FrameDecoder().feed(frame) == [msg]
    bomb = zlib.compress(b"{" + b" " * (P.MAX_INFLATED + 10) + b"}", 9)
    with pytest.raises(P.ProtocolError):
        P.FrameDecoder().feed(struct.pack("!I", len(bomb) | P.COMPRESSED_FLAG) + bomb)


def test_passwords_use_pbkdf2(server):
    a = Bot(server.port, "teacher", password="s3cret", create=True)
    room = server.rooms.get(a.room)
    assert len(room.pw_hash) == 64 and room.check_password("s3cret")
    assert not room.check_password("S3cret")
    import hashlib
    assert room.pw_hash == hashlib.pbkdf2_hmac("sha256", b"s3cret", room.salt, 120_000).hex()


def test_version_mismatch_is_refused_with_a_clear_message(server):
    a = Bot(server.port, "teacher", create=True)
    raw = socket.create_connection(("127.0.0.1", server.port))
    raw.settimeout(5)
    P.send_msg(raw, {"type": P.HELLO, "version": 1, "username": "old", "room": a.room})
    reply = P.recv_msg(raw)
    assert reply["type"] == P.ERROR and reply["code"] == P.E_VERSION
    assert "versions differ" in reply["message"]


# --------------------------------------------------------------------------- editing
def test_edit_recolour_resize_text_undo_and_late_join(server):
    a = Bot(server.port, "teacher", create=True)
    b = Bot(server.port, "asha", room=a.room)
    r = rect(a)
    n = a.stroke([0, 0], tool="sticky", text="old", color="#b5651d", fill="#fff59d")
    assert wait_until(lambda: len(b.model.visible_ids()) == 2, [a, b])
    a.net.send_board_op(P.EDIT, stroke_id=r, set={"color": "#e63946", "fill": "#ffe5e5"})
    a.net.send_board_op(P.EDIT, stroke_id=r, set={"points": [10, 10, 300, 200]})
    b.net.send_board_op(P.EDIT, stroke_id=n, set={"text": "new text"})
    assert wait_until(lambda: b.model.placed(n).get("text") == "new text" and
                      b.model.placed(r)["points"] == [10, 10, 300, 200], [a, b])
    s = b.model.placed(r)
    assert s["color"] == "#e63946" and s["fill"] == "#ffe5e5"
    assert b.model.last_editor(r)[0] == "teacher" and b.model.last_editor(n)[0] == "asha"
    a.net.send_board_op(P.UNDO)                          # undo the resize only
    assert wait_until(lambda: b.model.placed(r)["points"] == [10, 10, 110, 70], [a, b])
    assert b.model.placed(r)["color"] == "#e63946"
    a.net.send_board_op(P.EDIT, stroke_id=r, set={"fill": None})   # remove the fill
    assert wait_until(lambda: "fill" not in b.model.placed(r), [a, b])
    c = Bot(server.port, "late", room=a.room)
    assert c.model.fingerprint() == a.model.fingerprint() == b.model.fingerprint()
    # edits survive the snapshot round trip
    assert BoardModel.from_dict(a.model.to_dict()).fingerprint() == a.model.fingerprint()


def test_invalid_edits_are_rejected(server):
    a = Bot(server.port, "teacher", create=True)
    n = a.stroke([0, 0], tool="text", text="hello", color="#000000")
    r = rect(a)
    a.pump(0.3)
    for sid, change in ((n, {"text": ""}), (r, {"points": [1, 2, 3, 4, 5, 6]}),
                        (r, {"tool": "image"}), (r, {"color": "red"}), (r, {"user": "x"})):
        a.net.send_board_op(P.EDIT, stroke_id=sid, set=change)
    assert wait_until(lambda: len(a.errors) >= 5, [a])
    assert a.model.placed(n)["text"] == "hello"


def test_concurrent_edits_converge(server):
    a = Bot(server.port, "teacher", create=True)
    bots = [a] + [Bot(server.port, f"s{i}", room=a.room) for i in range(4)]
    r = rect(a)
    assert wait_until(lambda: all(r in b.model.strokes for b in bots), bots)
    colours = ["#e63946", "#1d70b8", "#2a9d8f", "#f4a261", "#8e44ad"]
    for i, b in enumerate(bots):
        for _ in range(10):
            b.net.send_board_op(P.EDIT, stroke_id=r, set={"color": colours[i]})
            b.net.send_board_op(P.MOVE, stroke_ids=[r], dx=i + 1, dy=1)
    # repeated identical colours are no-ops (ACK), so wait until nothing is pending
    assert wait_until(lambda: not any(b.net.pending for b in bots) and
                      len({b.model.seq for b in bots}) == 1, bots, timeout=10)
    assert bots[0].model.seq >= 51
    assert len({b.model.fingerprint() for b in bots}) == 1


# --------------------------------------------------------------------------- own-work-only
def test_own_work_only_mode(server):
    t = Bot(server.port, "teacher", create=True)
    s1 = Bot(server.port, "asha", room=t.room)
    s2 = Bot(server.port, "ravi", room=t.room)
    mine, theirs = rect(s1, 0, 0), rect(s2, 300, 0)
    s2.net.send({"type": P.SETTINGS, "key": "own_only", "value": True})   # not the host
    assert wait_until(lambda: s2.errors, [s2])
    t.net.send({"type": P.SETTINGS, "key": "own_only", "value": True})
    assert wait_until(lambda: s1.welcome and ev(s1, P.SETTINGS), [t, s1, s2])
    assert ev(s1, P.SETTINGS)[-1]["settings"]["own_only"] is True
    s1.net.send_board_op(P.MOVE, stroke_ids=[theirs], dx=5, dy=5)
    s1.net.send_board_op(P.EDIT, stroke_id=theirs, set={"color": "#000000"})
    s1.net.send_board_op(P.ERASE, stroke_ids=[mine, theirs])        # only "mine" goes
    assert wait_until(lambda: len(s1.errors) >= 2 and not s1.model.is_visible(mine), [t, s1, s2])
    assert all("Own-work-only" in m for m in errors(s1))
    assert s1.model.is_visible(theirs) and s1.model.placed(theirs)["points"][0] == 300
    t.net.send_board_op(P.ERASE, stroke_ids=[theirs])               # host may change anything
    assert wait_until(lambda: not s2.model.is_visible(theirs), [t, s1, s2])


# --------------------------------------------------------------------------- flowcharts
def test_nodes_and_connectors(server):
    a = Bot(server.port, "teacher", create=True)
    b = Bot(server.port, "asha", room=a.room)
    n1, n2 = node(a, 0, 0), node(a, 300, 0, "Decide", "diamond")
    r = rect(a, 0, 300)
    assert wait_until(lambda: len(a.model.visible_ids()) == 3, [a, b])
    c = a.stroke([0, 0, 300, 0], tool="connector", color="#334155", src=n1, dst=n2,
                 style="arrow", label="yes")
    bad = a.stroke([0, 0, 1, 1], tool="connector", color="#334155", src=n1, dst=r)
    assert wait_until(lambda: c in b.model.strokes and a.errors, [a, b])
    assert bad not in a.model.strokes
    assert b.model.strokes[c]["label"] == "yes" and b.model.strokes[n2]["shape"] == "diamond"
    b.net.send_board_op(P.EDIT, stroke_id=c, set={"label": "no", "style": "curve"})
    assert wait_until(lambda: a.model.placed(c).get("label") == "no", [a, b])
    for bad_node in ({"shape": "star"}, {"text": 5}):
        with pytest.raises(P.ProtocolError):
            P.sanitize_stroke({"id": "n", "tool": "node", "color": "#000000", "width": 2,
                               "points": [0, 0, 1, 1], **bad_node})


# --------------------------------------------------------------------------- settings
def test_settings_validation_and_late_join(server):
    t = Bot(server.port, "teacher", create=True)
    for key, value in (("paper", "ruled"), ("pages", 4), ("pages", 0), ("paper", "x"),
                       ("bogus", 1), ("pages", True)):
        t.net.send({"type": P.SETTINGS, "key": key, "value": value})
    assert wait_until(lambda: len(t.errors) >= 4 and len(ev(t, P.SETTINGS)) >= 2, [t])
    late = Bot(server.port, "late", room=t.room)
    assert late.welcome["settings"] == {"own_only": False, "paper": "ruled", "pages": 4}


# --------------------------------------------------------------------------- private messages
def test_private_messages_reach_only_the_two_people(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    r = Bot(server.port, "ravi", room=t.room)
    a.net.send({"type": P.DM, "to": "teacher", "text": "Can you repeat step 3?"})
    t.net.send({"type": P.DM, "to": "asha", "text": "Sure"})
    a.net.send({"type": P.DM, "to": "nobody", "text": "x"})
    assert wait_until(lambda: len(ev(t, P.DM)) == 2 and len(ev(a, P.DM)) == 2 and a.errors,
                      [t, a, r])
    r.pump(0.3)
    assert not ev(r, P.DM)
    assert sorted(m["text"] for m in ev(a, P.DM)) == ["Can you repeat step 3?", "Sure"]
    # after a reconnect the conversation is still there
    a.net.drop_connection()
    assert wait_until(lambda: a.status == "connected" and
                      len(a.net.welcome.get("dms", [])) == 2, [t, a, r], timeout=8)
    late = Bot(server.port, "late", room=t.room)
    assert late.welcome["dms"] == []


# --------------------------------------------------------------------------- hands
def test_raise_and_lower_hands(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    r = Bot(server.port, "ravi", room=t.room)

    def hand(bot, name):
        return next(p["hand"] for p in bot.participants if p["username"] == name)
    a.net.send({"type": P.HAND, "up": True})
    r.net.send({"type": P.HAND, "up": True})
    assert wait_until(lambda: hand(t, "asha") and hand(t, "ravi"), [t, a, r])
    r.net.send({"type": P.HAND, "target": "asha", "up": False})     # students can't
    t.net.send({"type": P.HAND, "target": "asha", "up": False})     # the teacher can
    assert wait_until(lambda: r.errors and not hand(r, "asha") and hand(r, "ravi"), [t, a, r])
    t.net.send({"type": P.ADMIN, "action": "lower_all_hands"})
    assert wait_until(lambda: not hand(a, "ravi"), [t, a, r])


# --------------------------------------------------------------------------- polls
def test_quick_poll(server):
    t = Bot(server.port, "teacher", create=True)
    s = [Bot(server.port, f"s{i}", room=t.room) for i in range(3)]
    s[0].net.send({"type": P.POLL, "action": "start", "question": "x", "options": ["a", "b"]})
    t.net.send({"type": P.POLL, "action": "start", "question": "Is TCP reliable?",
                "options": ["Yes", "No", "Not sure"]})
    assert wait_until(lambda: all(ev(b, P.POLL_STATE) for b in s), [t] + s)
    assert s[0].errors
    for i, b in enumerate(s):
        b.net.send({"type": P.POLL, "action": "answer", "option": 0 if i < 2 else 1})
    s[2].net.send({"type": P.POLL, "action": "answer", "option": 0})   # change of mind
    s[1].net.send({"type": P.POLL, "action": "answer", "option": 7})   # invalid
    assert wait_until(lambda: ev(t, P.POLL_STATE)[-1]["poll"]["counts"] == [3, 0, 0],
                      [t] + s)
    student_view = ev(s[0], P.POLL_STATE)[-1]["poll"]
    assert "answers" not in student_view                 # anonymous for students
    assert ev(t, P.POLL_STATE)[-1]["poll"]["answers"] == {"s0": 0, "s1": 0, "s2": 0}
    late = Bot(server.port, "s2x", room=t.room)
    assert late.welcome["poll"]["total"] == 3 and late.welcome["my_answer"] is None
    t.net.send({"type": P.POLL, "action": "end"})
    assert wait_until(lambda: not ev(s[0], P.POLL_STATE)[-1]["poll"]["open"], [t] + s)
    s[0].net.send({"type": P.POLL, "action": "answer", "option": 1})
    assert wait_until(lambda: len(s[0].errors) >= 2, [s[0]])


# --------------------------------------------------------------------------- restore / history
def test_restore_board_from_file(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    rect(t)
    a.pump(0.2)
    n1 = {"id": "f-1", "tool": "node", "color": "#334155", "width": 2, "points": [0, 0, 100, 50],
          "text": "A", "shape": "rect", "user": "bob"}
    n2 = dict(n1, id="f-2", points=[300, 0, 400, 50])
    conn = {"id": "f-3", "tool": "connector", "color": "#334155", "width": 2,
            "points": [0, 0, 1, 1], "src": "f-1", "dst": "f-2"}
    junk = {"id": "f-4", "tool": "laser", "color": "#000000", "width": 2, "points": [0, 0]}
    a.net.send({"type": P.RESTORE, "strokes": [n1]})                  # not the host
    t.net.send({"type": P.RESTORE, "strokes": [n1, n2, conn, junk]})
    assert wait_until(lambda: len(a.model.visible_ids()) == 3 and a.errors, [t, a])
    assert a.model.strokes["f-1"]["user"] == "bob"
    assert any("1 invalid" in e["message"] for e in ev(t, P.INFO))
    assert a.model.fingerprint() == t.model.fingerprint()


def test_version_history_rebuilds_every_version(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    r = rect(t)
    a.stroke([0, 0, 50, 50, 80, 20])
    t.net.send_board_op(P.MOVE, stroke_ids=[r], dx=40, dy=0)
    t.net.send_board_op(P.EDIT, stroke_id=r, set={"color": "#e63946"})
    a.net.send_board_op(P.UNDO)
    assert wait_until(lambda: t.model.seq == 5, [t, a])
    a.net.send({"type": P.HISTORY})
    assert wait_until(lambda: ev(a, P.HISTORY), [t, a])
    h = ev(a, P.HISTORY)[-1]
    assert sorted(o["kind"] for o in h["ops"]) == ["EDIT", "MOVE", "STROKE", "STROKE", "UNDO"]
    m = BoardModel.from_dict(h["base"])
    for o in h["ops"]:
        m.apply(o)
    assert m.fingerprint() == a.model.fingerprint()
    m2 = BoardModel.from_dict(h["base"])
    for o in h["ops"]:
        if o["kind"] == "MOVE":
            break
        m2.apply(o)
    assert m2.placed(r)["points"][0] == 10               # the version before the move


def test_history_is_folded_when_long(server, monkeypatch):
    from server import room as room_mod
    monkeypatch.setattr(room_mod, "HISTORY_MAX", 1500)
    t = Bot(server.port, "teacher", create=True)
    for i in range(1600):
        t.stroke([i, 0, i + 1, 1])
    assert wait_until(lambda: t.model.seq == 1600, [t], timeout=15)
    t.net.send({"type": P.HISTORY})
    assert wait_until(lambda: ev(t, P.HISTORY), [t], timeout=10)
    h = ev(t, P.HISTORY)[-1]
    assert len(h["ops"]) == 600 and len(h["base"]["strokes"]) == 1000
    m = BoardModel.from_dict(h["base"])
    for o in h["ops"]:
        m.apply(o)
    assert m.fingerprint() == t.model.fingerprint()


# --------------------------------------------------------------------------- ending a session
def test_host_ends_the_session(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    a.net.send({"type": P.CLOSE_ROOM})
    assert wait_until(lambda: a.errors, [t, a])
    t.net.send({"type": P.CLOSE_ROOM})
    assert wait_until(lambda: any(e.get("closed") for e in ev(a, P.KICKED)), [t, a])
    assert server.rooms.get(t.room) is None
    with pytest.raises(Exception):
        Bot(server.port, "again", room=t.room)


# --------------------------------------------------------------------------- laser pointer
def test_laser_pointer_is_relayed_not_stored(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    v = Bot(server.port, "viewer", room=t.room)
    t.net.send({"type": P.ADMIN, "action": "set_role", "target": "viewer", "role": "viewer"})
    v.pump(0.3)
    t.net.send({"type": P.LASER, "points": [10, 10, 20, 20], "end": False})
    v.net.send({"type": P.LASER, "points": [1, 1]})
    assert wait_until(lambda: ev(a, P.LASER), [t, a, v])
    a.pump(0.3)
    assert [e["user"] for e in ev(a, P.LASER)] == ["teacher"]
    assert t.model.seq == 0


# --------------------------------------------------------------------------- TLS
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_tls_encrypted_session():
    d = tempfile.mkdtemp()
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=classboard", "-keyout", key, "-out", cert],
                   check=True, capture_output=True)
    srv = start_server(ssl_context=P.server_tls_context(cert, key))
    try:
        a = Bot(srv.port, "teacher", create=True, tls=True)
        b = Bot(srv.port, "asha", room=a.room, tls=True)
        rect(a)
        assert wait_until(lambda: len(b.model.visible_ids()) == 1, [a, b])
        with pytest.raises(Exception):                   # plain TCP client is refused
            Bot(srv.port, "plain", room=a.room)
    finally:
        srv.stop()


# --------------------------------------------------------------------------- long outage
def test_rejoin_after_grace_expired_keeps_offline_edits(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    room = server.rooms.get(t.room)
    old_token = a.net.token
    with room.lock:                               # hold the room so the seat expires first
        a.net.drop_connection()                   # cable pulled ...
        sid = rect(a, 500, 500)                   # ... asha keeps drawing offline
        import time as _t
        _t.sleep(0.3)
        room._remove("asha", "left")              # ... and her seat expires meanwhile
    assert wait_until(lambda: a.model.is_visible(sid) and t.model.is_visible(sid), [t, a],
                      timeout=10)
    assert any(p["username"] == "asha" for p in t.participants)
    assert a.model.fingerprint() == t.model.fingerprint()
    assert a.net.token != old_token               # really a fresh seat, not a resume


# --------------------------------------------------------------------------- robustness fixes
def test_flooding_client_is_throttled_but_loses_nothing(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    start = time.time()
    for i in range(1500):                         # far beyond the 600-message burst
        t.stroke([i, 0, i + 1, 1])
    assert wait_until(lambda: len(a.model.visible_ids()) == 1500, [t, a], timeout=20)
    assert time.time() - start > 2.0              # ~(1500-600)/300 s of throttling
    assert a.model.fingerprint() == t.model.fingerprint()


def test_votes_on_erased_objects_are_given_back(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    notes = [t.stroke([i * 300, 0], tool="sticky", text=f"n{i}", color="#b5651d")
             for i in range(6)]
    assert wait_until(lambda: len(a.model.visible_ids()) == 6, [t, a])
    for n in notes[:5]:
        a.net.send({"type": P.VOTE, "stroke_id": n})
    assert wait_until(lambda: sum(e.get("voters") == ["asha"] for e in ev(t, P.VOTES)) == 5,
                      [t, a])                         # all 5 of asha's votes are in
    t.net.send_board_op(P.ERASE, stroke_ids=notes[:2])   # two voted notes disappear
    assert wait_until(lambda: not a.model.is_visible(notes[0]), [t, a])
    a.net.send({"type": P.VOTE, "stroke_id": notes[5]})  # allowed again: 3 live votes
    assert wait_until(lambda: any(e.get("stroke_id") == notes[5] and "asha" in e["voters"]
                                  for e in ev(t, P.VOTES)), [t, a])
    assert not a.errors


def test_booleans_are_not_numbers():
    base = {"id": "x", "tool": "pen", "color": "#000000", "width": True, "points": [1, 2]}
    with pytest.raises(P.ProtocolError):
        P.sanitize_stroke(base)


def test_history_reply_always_fits_in_a_frame(server, monkeypatch):
    t = Bot(server.port, "teacher", create=True)
    for i in range(60):
        t.stroke([float(v) for v in range(400)])     # big strokes
    assert wait_until(lambda: t.model.seq == 60, [t])
    monkeypatch.setattr(P, "MAX_INFLATED", 60000)    # pretend frames are tiny
    reply = server.rooms.get(t.room).history_reply()
    monkeypatch.undo()
    assert reply["trimmed"] > 0 and len(reply["ops"]) < 60
    m = BoardModel.from_dict(reply["base"])
    for o in reply["ops"]:
        m.apply(o)
    assert m.fingerprint() == t.model.fingerprint()  # still rebuilds the same board


def test_hand_over_demotes_the_old_teacher_in_the_participant_list(server):
    t = Bot(server.port, "teacher", create=True)
    a = Bot(server.port, "asha", room=t.room)
    t.net.send({"type": P.ADMIN, "action": "set_role", "target": "asha", "role": "host"})
    roles = lambda b: {p["username"]: p["role"] for p in b.participants}    # noqa: E731
    assert wait_until(lambda: roles(t) == {"teacher": "editor", "asha": "host"}, [t, a])
    t.net.send({"type": P.CLEAR, "op_id": "x-1"})        # no longer allowed
    assert wait_until(lambda: t.errors, [t])


def test_fuzzing_with_malformed_messages_never_breaks_the_class(server):
    """~2,000 random messages with fields of every wrong type, from a student and the
    teacher: no thread may die and the class must keep working with identical boards."""
    import random
    import threading
    import traceback
    died = []
    old_hook = threading.excepthook
    threading.excepthook = lambda a: died.append("".join(traceback.format_exception(
        a.exc_type, a.exc_value, a.exc_traceback)))
    try:
        t = Bot(server.port, "teacher", create=True)
        good = Bot(server.port, "good", room=t.room)
        kinds = [P.STROKE, P.MOVE, P.EDIT, P.ERASE, P.CLEAR, P.UNDO, P.REDO, P.CHAT, P.CURSOR,
                 P.STROKE_PART, P.LASER, P.VOTE, P.VOTES_CLEAR, P.TIMER, P.FOLLOW, P.VIEWPORT,
                 P.DM, P.HAND, P.POLL, P.SETTINGS, P.ADMIN, P.SAVE, P.LOAD, P.RESTORE, "BOGUS"]
        values = [None, True, 0, -1, 1e308, "", "x" * 3000, [], [1, 2], ["a"], {}, {"a": 1},
                  3.5, "teacher", "good", "host", "start", "answer", "lock", "kick", "set_role",
                  "pages", {"color": 5}, [None]]
        fields = ["op_id", "stroke", "stroke_ids", "stroke_id", "set", "dx", "dy", "x", "y",
                  "w", "h", "points", "text", "to", "up", "target", "action", "option",
                  "question", "options", "key", "value", "role", "name", "strokes", "on",
                  "seconds"]
        rnd = random.Random(7)
        for rnd_i in range(12):
            raw = socket.create_connection(("127.0.0.1", server.port))
            P.send_msg(raw, {"type": P.HELLO, "version": P.PROTOCOL_VERSION,
                             "username": f"fz{rnd_i}", "room": t.room})
            P.recv_msg(raw)
            # the teacher's junk goes straight onto its socket queue (host privileges
            # reach more server code); NetClient itself never builds such messages
            for sender in (lambda m: P.send_msg(raw, m),
                           lambda m: t.net.outq.put_nowait(P.encode(m))):
                for _ in range(80):
                    m = {"type": rnd.choice(kinds)}
                    for k in rnd.sample(fields, rnd.randint(0, 5)):
                        m[k] = rnd.choice(values)
                    try:
                        sender(m)
                    except OSError:
                        break                         # the server dropped the fuzzer: fine
            raw.close()
        time.sleep(0.5)
        sid = good.stroke([5, 5, 50, 50])
        assert wait_until(lambda: t.model.is_visible(sid), [t, good], timeout=8)
        assert t.model.fingerprint() == good.model.fingerprint()
        assert not died, died[0]
    finally:
        threading.excepthook = old_hook


def test_server_restart_mid_class(tmp_path):
    """Ctrl+C on the server, start it again: rooms come back from the autosave and the
    clients reconnect by themselves, keeping the board."""
    import socket as _s
    probe = _s.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    from server.main import Server
    mk = lambda: Server("127.0.0.1", port, 0, str(tmp_path), discovery=False,   # noqa: E731
                        autosave=0, logger=lambda m: None)
    srv = mk()
    srv.start()
    t = Bot(port, "teacher", create=True)
    a = Bot(port, "asha", room=t.room)
    rect(t)
    a.stroke([0, 0, 10, 10])
    assert wait_until(lambda: len(t.model.visible_ids()) == 2 == len(a.model.visible_ids()),
                      [t, a])
    fp = t.model.fingerprint()
    srv.stop()
    assert wait_until(lambda: a.status == "reconnecting", [t, a], timeout=5)
    srv = mk()
    srv.start()
    try:
        assert wait_until(lambda: t.status == "connected" and a.status == "connected",
                          [t, a], timeout=15)
        assert t.model.fingerprint() == fp == a.model.fingerprint()
        sid = a.stroke([20, 20, 40, 40])
        assert wait_until(lambda: t.model.is_visible(sid), [t, a])
    finally:
        srv.stop()
