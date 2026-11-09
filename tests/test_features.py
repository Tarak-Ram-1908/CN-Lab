"""Tests for the advanced features: moving objects, voting, session timer,
presentation ("follow me") mode, infinite canvas and smart shape recognition."""
import math
import random
import threading
import time

import pytest

from helpers import Bot, P, start_server, wait_until
from common.model import BoardModel
from common.shapes import recognize


@pytest.fixture
def server():
    srv = start_server()
    yield srv
    srv.stop()


def ev(bot, typ):
    return [e for e in bot.other if e["type"] == typ]


def sticky(bot, x, y, text="idea"):
    return bot.stroke([x, y], tool="sticky", text=text, color="#b5651d", fill="#fff59d")


# --------------------------------------------------------------------------- moving objects
def test_move_and_undo(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    sid = sticky(a, 100, 100)
    wait_until(lambda: b.model.visible_ids() == [sid], [a, b])
    a.net.send_board_op(P.MOVE, stroke_ids=[sid], dx=250, dy=-40)
    assert wait_until(lambda: b.model.placed(sid)["points"] == [350, 60], [a, b])
    assert b.model.strokes[sid]["points"] == [100, 100]     # stored points never change
    a.net.send_board_op(P.UNDO)
    assert wait_until(lambda: b.model.placed(sid)["points"] == [100, 100], [a, b])
    a.net.send_board_op(P.REDO)
    assert wait_until(lambda: b.model.placed(sid)["points"] == [350, 60], [a, b])
    # a late joiner sees the moved position
    c = Bot(server.port, "carol", room=a.room)
    assert c.model.placed(sid)["points"] == [350, 60]
    assert c.model.fingerprint() == a.model.fingerprint()


def test_concurrent_moves_are_deterministic(server):
    """Five users drag the same note 40 times each, all at once: every client ends
    with the same position = the sum of all moves (nothing lost, nothing diverges)."""
    host = Bot(server.port, "host", create=True)
    sid = sticky(host, 0, 0)
    wait_until(lambda: host.model.visible_ids() == [sid], [host])
    bots = [host] + [Bot(server.port, f"u{i}", room=host.room) for i in range(4)]
    barrier = threading.Barrier(len(bots))
    total = [0.0, 0.0]
    lock = threading.Lock()

    def drag(bot):
        rnd = random.Random(bot.name)
        barrier.wait()
        for _ in range(40):
            dx, dy = rnd.randint(-20, 20), rnd.randint(-20, 20)
            if dx == 0 and dy == 0:
                dx = 1
            with lock:
                total[0] += dx
                total[1] += dy
            bot.net.send_board_op(P.MOVE, stroke_ids=[sid], dx=dx, dy=dy)
    ts = [threading.Thread(target=drag, args=(b,)) for b in bots]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert wait_until(lambda: all(b.model.placed(sid)["points"] == [round(total[0], 1),
                                                                    round(total[1], 1)]
                                  for b in bots), bots, 15)
    assert len({b.model.fingerprint() for b in bots}) == 1


def test_move_permissions(server):
    a = Bot(server.port, "alice", create=True)
    v = Bot(server.port, "vic", room=a.room)
    sid = sticky(a, 10, 10)
    a.net.send({"type": P.ADMIN, "action": "set_role", "target": "vic", "role": "viewer"})
    wait_until(lambda: any(p["username"] == "vic" and p["role"] == "viewer"
                           for p in v.participants), [a, v])
    v.net.send_board_op(P.MOVE, stroke_ids=[sid], dx=5, dy=5)
    assert wait_until(lambda: any(e["code"] == P.E_FORBIDDEN for e in v.errors), [a, v])
    assert a.model.placed(sid)["points"] == [10, 10]


# --------------------------------------------------------------------------- voting
def test_voting_toggle_limit_and_viewers(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    notes = [sticky(a, i * 300, 0, f"idea {i}") for i in range(7)]
    wait_until(lambda: len(b.model.visible_ids()) == 7, [a, b])
    for sid in notes[:5]:
        b.net.send({"type": P.VOTE, "stroke_id": sid})
    b.net.send({"type": P.VOTE, "stroke_id": notes[5]})          # 6th vote: over the limit
    assert wait_until(lambda: any("votes" in e["message"] for e in b.errors), [a, b])
    assert wait_until(lambda: len(ev(a, P.VOTES)) == 5, [a, b])
    b.net.send({"type": P.VOTE, "stroke_id": notes[0]})          # toggle off frees a vote
    b.net.send({"type": P.VOTE, "stroke_id": notes[5]})
    assert wait_until(lambda: ev(a, P.VOTES)[-1] == {"type": P.VOTES, "stroke_id": notes[5],
                                                     "voters": ["bob"]}, [a, b])
    room = server.rooms.get(a.room)
    assert notes[0] not in room.votes and room.votes[notes[5]] == ["bob"]
    # viewers may vote, and voting works on a locked board
    b.net.send({"type": P.VOTE, "stroke_id": notes[1]})          # free one of bob's 5 votes
    a.net.send({"type": P.ADMIN, "action": "set_role", "target": "bob", "role": "viewer"})
    a.net.send({"type": P.ADMIN, "action": "lock"})
    wait_until(lambda: room.locked, [a, b])
    b.net.send({"type": P.VOTE, "stroke_id": notes[6]})
    assert wait_until(lambda: room.votes.get(notes[6]) == ["bob"], [a, b])
    # late joiner gets the tallies; only the host can reset
    c = Bot(server.port, "carol", room=a.room)
    assert c.welcome["votes"][notes[5]] == ["bob"]
    b.net.send({"type": P.VOTES_CLEAR})
    assert wait_until(lambda: any(e["code"] == P.E_FORBIDDEN for e in b.errors), [b])
    a.net.send({"type": P.VOTES_CLEAR})
    assert wait_until(lambda: room.votes == {}, [a])


def test_votes_survive_save_and_load(tmp_path):
    srv = start_server(save_dir=str(tmp_path))
    a = Bot(srv.port, "alice", create=True)
    sid = sticky(a, 0, 0)
    wait_until(lambda: a.model.visible_ids() == [sid], [a])
    a.net.send({"type": P.VOTE, "stroke_id": sid})
    wait_until(lambda: srv.rooms.get(a.room).votes, [a])
    a.net.send({"type": P.SAVE, "name": "v1"})
    wait_until(lambda: any(e["type"] == P.INFO for e in a.other), [a])
    a.net.send({"type": P.VOTES_CLEAR})
    wait_until(lambda: not srv.rooms.get(a.room).votes, [a])
    a.net.send({"type": P.LOAD, "name": "v1"})
    assert wait_until(lambda: srv.rooms.get(a.room).votes == {sid: ["alice"]}, [a])
    srv.stop()


# --------------------------------------------------------------------------- timer
def test_session_timer(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    b.net.send({"type": P.TIMER, "action": "start", "seconds": 60})
    assert wait_until(lambda: any(e["code"] == P.E_FORBIDDEN for e in b.errors), [a, b])
    a.net.send({"type": P.TIMER, "action": "start", "seconds": 300})
    assert wait_until(lambda: ev(b, P.TIMER_STATE), [a, b])
    st = ev(b, P.TIMER_STATE)[-1]
    assert st["running"] and 299 <= st["remaining"] <= 300 and st["duration"] == 300
    time.sleep(0.6)
    a.net.send({"type": P.TIMER, "action": "pause"})
    assert wait_until(lambda: not ev(b, P.TIMER_STATE)[-1]["running"], [a, b])
    paused = ev(b, P.TIMER_STATE)[-1]["remaining"]
    assert 298.5 <= paused <= 299.6
    time.sleep(0.4)                                  # paused: no time passes
    c = Bot(server.port, "carol", room=a.room)        # late joiner sees the paused timer
    assert c.welcome["timer"]["remaining"] == paused and not c.welcome["timer"]["running"]
    a.net.send({"type": P.TIMER, "action": "resume"})
    assert wait_until(lambda: ev(b, P.TIMER_STATE)[-1]["running"], [a, b])
    a.net.send({"type": P.TIMER, "action": "reset"})
    assert wait_until(lambda: ev(b, P.TIMER_STATE)[-1]["remaining"] == 300, [a, b])
    a.net.send({"type": P.TIMER, "action": "start", "seconds": 0})
    assert wait_until(lambda: any(e["code"] == P.E_BAD_MESSAGE for e in a.errors), [a])


# --------------------------------------------------------------------------- follow me
def test_presentation_mode(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    b.net.send({"type": P.FOLLOW, "on": True})
    assert wait_until(lambda: any(e["code"] == P.E_FORBIDDEN for e in b.errors), [a, b])
    a.net.send({"type": P.VIEWPORT, "x": 0, "y": 0, "w": 800, "h": 450})   # not presenting
    a.net.send({"type": P.FOLLOW, "on": True})
    assert wait_until(lambda: any(e.get("follow") == "alice" for e in ev(b, P.ROOM_STATE)),
                      [a, b])
    assert not ev(b, P.VIEWPORT)                      # the earlier viewport was ignored
    a.net.send({"type": P.VIEWPORT, "x": -2000, "y": 500, "w": 1200, "h": 675})
    assert wait_until(lambda: ev(b, P.VIEWPORT), [a, b])
    vp = ev(b, P.VIEWPORT)[-1]
    assert (vp["x"], vp["y"], vp["w"], vp["h"], vp["user"]) == (-2000, 500, 1200, 675, "alice")
    assert not ev(a, P.VIEWPORT)                      # not echoed to the presenter
    b.net.send({"type": P.VIEWPORT, "x": 0, "y": 0, "w": 10, "h": 10})     # follower: ignored
    c = Bot(server.port, "carol", room=a.room)        # late joiner jumps to the presenter
    assert c.welcome["follow"] == "alice" and c.welcome["viewport"] == [-2000, 500, 1200, 675]
    a.close()                                         # presenter leaves: mode ends
    assert wait_until(lambda: any(e.get("follow") is None for e in ev(b, P.ROOM_STATE)), [b])


# --------------------------------------------------------------------------- infinite canvas
def test_infinite_canvas_coordinates(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    sid = a.stroke([-50000, -3000, 120000, 80000, 250000, -9000])
    assert wait_until(lambda: b.model.visible_ids() == [sid], [a, b])
    assert b.model.strokes[sid]["points"] == [-50000, -3000, 120000, 80000, 250000, -9000]
    a.net.send({"type": P.CURSOR, "x": -123456.7, "y": 98765.4})
    assert wait_until(lambda: any(e["type"] == P.CURSOR and e["x"] == -123456.7
                                  for e in b.other), [a, b])


# --------------------------------------------------------------------------- model
def test_model_snapshot_keeps_moves():
    m = BoardModel()
    m.apply({"kind": "STROKE", "op_id": "1", "seq": 1, "user": "a",
             "stroke": {"id": "S", "tool": "pen", "color": "#000000", "width": 2,
                        "points": [0, 0, 10, 10]}})
    m.apply({"kind": "MOVE", "op_id": "2", "seq": 2, "user": "a", "stroke_ids": ["S"],
             "dx": 5, "dy": 7})
    m.apply({"kind": "MOVE", "op_id": "3", "seq": 3, "user": "b", "stroke_ids": ["S"],
             "dx": -1, "dy": 1})
    m.apply({"kind": "UNDO", "op_id": "4", "seq": 4, "user": "a", "target": "2"})
    assert m.placed("S")["points"] == [-1, 1, 9, 11]
    m2 = BoardModel.from_dict(m.to_dict())
    assert m2.placed("S")["points"] == [-1, 1, 9, 11]
    assert m2.fingerprint() == m.fingerprint()


# --------------------------------------------------------------------------- shapes
def _circle(rnd, rx=100, ry=100, noise=4):
    ph = rnd.uniform(0, 6)
    return [v + rnd.uniform(-noise, noise) for i in range(66)
            for v in (300 + rx * math.cos(ph + 2 * math.pi * i / 60),
                      300 + ry * math.sin(ph + 2 * math.pi * i / 60))]


def _poly(rnd, verts, noise=4, per=15):
    out = []
    for (x0, y0), (x1, y1) in zip(verts, verts[1:] + verts[:1]):
        for i in range(per):
            t = i / per
            out += [x0 + (x1 - x0) * t + rnd.uniform(-noise, noise),
                    y0 + (y1 - y0) * t + rnd.uniform(-noise, noise)]
    out += [verts[0][0], verts[0][1]]
    return out


@pytest.mark.parametrize("seed", range(10))
def test_shape_recognition(seed):
    rnd = random.Random(seed)
    assert recognize(_circle(rnd))[0] == "ellipse"
    assert recognize(_circle(rnd, 170, 60))[0] == "ellipse"
    tool, pts = recognize(_poly(rnd, [(100, 100), (420, 100), (420, 300), (100, 300)]))
    assert tool == "rect" and abs(pts[0] - 100) < 12 and abs(pts[3] - 300) < 12
    tool, pts = recognize(_poly(rnd, [(300, 80), (460, 380), (140, 380)]))
    assert tool == "polygon" and len(pts) == 6
    line = [v for i in range(40) for v in (100 + i * 10 + rnd.uniform(-3, 3),
                                           100 + i * 5 + rnd.uniform(-3, 3))]
    assert recognize(line)[0] == "line"
    scribble = [v for i in range(60) for v in (100 + i * 6, 300 + 80 * math.sin(i * 0.5))]
    assert recognize(scribble) is None
    assert recognize([0, 0, 3, 3, 5, 1, 2, 2, 1, 4, 0, 1]) is None          # too small
