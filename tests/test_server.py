"""End-to-end tests: a real server on localhost with real NetClient connections."""
import json
import random
import socket
import threading
import time

import pytest

from helpers import Bot, P, start_server, wait_until
from common.discovery import discover
from server.discovery import DiscoveryResponder


@pytest.fixture
def server():
    srv = start_server()
    yield srv
    srv.stop()


def room_of(srv, code):
    return srv.rooms.get(code)


# --------------------------------------------------------------------------- core
def test_concurrent_stress_all_canvases_identical(server):
    """10 clients each fire 200 strokes at the same time; every canvas must match."""
    host = Bot(server.port, "host", create=True)
    bots = [host] + [Bot(server.port, f"u{i}", room=host.room) for i in range(9)]
    start = threading.Barrier(len(bots))

    def fire(bot):
        rnd = random.Random(bot.name)
        start.wait()
        for _ in range(200):
            x, y = rnd.uniform(0, 1500), rnd.uniform(0, 800)
            bot.stroke([x, y, x + 20, y + 30, x + 40, y + 10], color="#123456")
    threads = [threading.Thread(target=fire, args=(b,)) for b in bots]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wait_until(lambda: all(len(b.model.visible_ids()) == 2000 for b in bots), bots, 30)
    fps = {b.model.fingerprint() for b in bots}
    assert len(fps) == 1, "canvases diverged"
    assert room_of(server, host.room).model.fingerprint() in fps
    # every client saw the same global order
    orders = {tuple(b.model.order) for b in bots}
    assert len(orders) == 1
    for b in bots:
        b.close()


def test_late_joiner_gets_full_state_and_chat(server):
    a = Bot(server.port, "alice", create=True)
    for i in range(30):
        a.stroke([i, i, i + 5, i + 5])
    a.net.send_board_op(P.UNDO)
    a.net.send({"type": P.CHAT, "text": "first!"})
    wait_until(lambda: len(a.model.visible_ids()) == 29 and a.chat, [a])
    late = Bot(server.port, "late", room=a.room)
    assert late.model.fingerprint() == a.model.fingerprint()
    assert [c["text"] for c in late.chat] == ["first!"]
    assert "alice" in [p["username"] for p in late.participants]


def test_undo_is_per_user(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    sa = a.stroke([1, 1, 2, 2])
    wait_until(lambda: len(b.model.visible_ids()) == 1, [a, b])
    sb = b.stroke([3, 3, 4, 4])
    wait_until(lambda: len(a.model.visible_ids()) == 2, [a, b])
    a.net.send_board_op(P.UNDO)                         # alice undoes HER stroke, not bob's
    assert wait_until(lambda: b.model.visible_ids() == [sb], [a, b])
    a.net.send_board_op(P.REDO)
    assert wait_until(lambda: b.model.visible_ids() == [sa, sb], [a, b])
    b.net.send_board_op(P.UNDO)
    b.net.send_board_op(P.UNDO)                         # nothing left to undo for bob
    assert wait_until(lambda: any(e["code"] == P.E_NOT_FOUND for e in b.errors), [a, b])
    assert a.model.visible_ids() == [sa]


def test_duplicate_op_ids_are_ignored(server):
    a = Bot(server.port, "alice", create=True)
    msg = {"type": P.STROKE, "op_id": "fixed-1",
           "stroke": {"id": "dup", "tool": "pen", "color": "#000000", "width": 2,
                      "points": [1, 1, 2, 2]}}
    for _ in range(5):
        a.net.send(dict(msg))
    a.pump(0.5)
    room = room_of(server, a.room)
    assert room.seq == 1 and a.model.visible_ids() == ["dup"]
    acks = [e for e in a.other if e["type"] == P.ACK]
    assert len(acks) == 4


def test_reconnect_receives_only_missed_ops(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    a.stroke([1, 1, 2, 2])
    wait_until(lambda: len(b.model.visible_ids()) == 1, [a, b])
    token = b.net.token
    real_port = b.net.port
    b.net.port = 1                                      # keep bob's retries failing
    b.net.drop_connection()                             # pulled cable
    assert wait_until(lambda: any(p["status"] == "away" for p in a.participants), [a])
    for i in range(10):                                 # drawn while bob is away
        a.stroke([i, i, i + 1, i + 1])
    wait_until(lambda: len(a.model.visible_ids()) == 11, [a])
    b.net.port = real_port                              # network is back
    assert wait_until(lambda: b.status == "connected" and len(b.model.visible_ids()) == 11,
                      [a, b], 15)
    assert b.net.token == token                        # same seat
    assert b.model.fingerprint() == a.model.fingerprint()
    welcome = b.net.welcome
    assert "missed_ops" in welcome and "snapshot" not in welcome
    assert len(welcome["missed_ops"]) == 10


def test_ops_sent_while_disconnected_are_delivered_once(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    b.net.drop_connection()
    time.sleep(0.2)
    sid = b.stroke([5, 5, 6, 6])                        # queued while offline -> pending
    assert wait_until(lambda: a.model.visible_ids() == [sid], [a, b], 10)
    assert b.model.visible_ids() == [sid]
    assert room_of(server, a.room).seq == 1


def test_heartbeat_timeout_marks_user_away(server, monkeypatch):
    a = Bot(server.port, "alice", create=True)
    raw = socket.create_connection(("127.0.0.1", server.port))
    raw.settimeout(5)
    P.send_msg(raw, {"type": P.HELLO, "version": P.PROTOCOL_VERSION, "username": "silent", "room": a.room})
    assert P.recv_msg(raw)["type"] == P.WELCOME
    # 'silent' never sends PING; after DEAD_TIMEOUT (10 s) the server gives up on it.
    assert wait_until(lambda: any(p["username"] == "silent" and p["status"] == "away"
                                  for p in a.participants), [a], 14)
    raw.close()


# --------------------------------------------------------------------------- access control
def test_password_and_rate_limit(server):
    a = Bot(server.port, "alice", create=True, password="s3cret")
    from client.net import JoinFailed
    for i in range(3):
        with pytest.raises(JoinFailed) as e:
            Bot(server.port, f"x{i}", room=a.room, password="wrong")
        assert e.value.code == P.E_BAD_PASSWORD
    with pytest.raises(JoinFailed) as e:
        Bot(server.port, "x9", room=a.room, password="s3cret")
    assert e.value.code == P.E_RATE_LIMIT


def test_password_is_hashed_not_stored(server):
    a = Bot(server.port, "alice", create=True, password="s3cret")
    room = room_of(server, a.room)
    assert "s3cret" not in json.dumps({k: str(v) for k, v in vars(room).items()})


def test_name_taken_and_bad_room(server):
    from client.net import JoinFailed
    a = Bot(server.port, "alice", create=True)
    with pytest.raises(JoinFailed) as e:
        Bot(server.port, "alice", room=a.room)
    assert e.value.code == P.E_NAME_TAKEN
    with pytest.raises(JoinFailed) as e:
        Bot(server.port, "bob", room="ZZZZ")
    assert e.value.code == P.E_ROOM_NOT_FOUND


def test_roles_lock_kick(server):
    from client.net import JoinFailed
    h = Bot(server.port, "host", create=True)
    e = Bot(server.port, "ed", room=h.room)
    assert e.role == P.ROLE_EDITOR
    # editor cannot clear, lock or kick
    e.stroke([1, 1, 2, 2])
    e.net.send_board_op(P.CLEAR)
    e.net.send({"type": P.ADMIN, "action": "lock"})
    assert wait_until(lambda: [x["code"] for x in e.errors].count(P.E_FORBIDDEN) == 2, [h, e])
    # host locks: editor strokes refused, host still draws
    h.net.send({"type": P.ADMIN, "action": "lock"})
    wait_until(lambda: room_of(server, h.room).locked, [h, e])
    e.stroke([3, 3, 4, 4])
    h.stroke([5, 5, 6, 6])
    assert wait_until(lambda: any(x["code"] == P.E_LOCKED for x in e.errors), [h, e])
    assert wait_until(lambda: len(h.model.visible_ids()) == 2, [h, e])
    h.net.send({"type": P.ADMIN, "action": "unlock"})
    # viewer cannot draw
    h.net.send({"type": P.ADMIN, "action": "set_role", "target": "ed", "role": "viewer"})
    assert wait_until(lambda: any(p["username"] == "ed" and p["role"] == "viewer"
                                  for p in e.participants), [h, e])
    e.stroke([7, 7, 8, 8])
    assert wait_until(lambda: any(x["code"] == P.E_FORBIDDEN and "View-only" in x["message"]
                                  for x in e.errors), [h, e])
    # kick: removed, notified, cannot come back
    h.net.send({"type": P.ADMIN, "action": "kick", "target": "ed"})
    assert wait_until(lambda: any(x["type"] == P.KICKED for x in e.other), [h, e])
    assert wait_until(lambda: [p["username"] for p in h.participants] == ["host"], [h, e])
    with pytest.raises(JoinFailed) as err:
        Bot(server.port, "ed", room=h.room)
    assert err.value.code == P.E_BANNED


def test_host_handover_on_leave(server):
    h = Bot(server.port, "host", create=True)
    b = Bot(server.port, "bob", room=h.room)
    h.close()
    assert wait_until(lambda: any(p["username"] == "bob" and p["role"] == P.ROLE_HOST
                                  for p in b.participants), [b])


def test_malformed_messages_disconnect_after_three(server):
    a = Bot(server.port, "alice", create=True)
    raw = socket.create_connection(("127.0.0.1", server.port))
    raw.settimeout(5)
    P.send_msg(raw, {"type": P.HELLO, "version": P.PROTOCOL_VERSION, "username": "evil", "room": a.room})
    P.recv_msg(raw)
    for _ in range(3):
        P.send_msg(raw, {"type": P.STROKE, "op_id": "x", "stroke": {"id": "s", "tool": "nuke"}})
    codes = []
    with pytest.raises((ConnectionError, OSError)):
        while True:
            m = P.recv_msg(raw)
            if m["type"] == P.ERROR:
                codes.append(m["code"])
    assert codes.count(P.E_BAD_MESSAGE) == 3
    assert room_of(server, a.room).seq == 0


def test_handshake_requires_hello(server):
    raw = socket.create_connection(("127.0.0.1", server.port))
    raw.settimeout(5)
    P.send_msg(raw, {"type": P.CHAT, "text": "hi"})
    assert P.recv_msg(raw)["code"] == P.E_BAD_MESSAGE


# --------------------------------------------------------------------------- persistence
def test_save_load_and_autosave_restore(tmp_path):
    srv = start_server(save_dir=str(tmp_path))
    h = Bot(srv.port, "host", create=True)
    b = Bot(srv.port, "bob", room=h.room)
    for i in range(5):
        h.stroke([i, i, i + 9, i + 9])
    wait_until(lambda: len(b.model.visible_ids()) == 5, [h, b])
    h.net.send({"type": P.SAVE, "name": "v1"})
    assert wait_until(lambda: any(x["type"] == P.INFO for x in h.other), [h, b])
    h.net.send_board_op(P.CLEAR)
    assert wait_until(lambda: not b.model.visible_ids(), [h, b])
    b.net.send({"type": P.LOAD, "name": "v1"})            # editors may not load
    assert wait_until(lambda: any(x["code"] == P.E_FORBIDDEN for x in b.errors), [h, b])
    h.net.send({"type": P.LOAD, "name": "v1"})
    assert wait_until(lambda: len(b.model.visible_ids()) == 5, [h, b])
    assert b.model.fingerprint() == h.model.fingerprint()
    h.stroke([50, 50, 60, 60])                             # seq keeps increasing after load
    assert wait_until(lambda: len(b.model.visible_ids()) == 6, [h, b])
    h.net.send({"type": P.LIST_SAVES})
    assert wait_until(lambda: any(x["type"] == P.SAVES for x in h.other), [h, b])
    fp = h.model.fingerprint()
    srv.stop()                                             # autosaves dirty rooms
    srv2 = start_server(save_dir=str(tmp_path))
    room = srv2.rooms.get(h.room)
    assert room is not None and room.model.fingerprint() == fp
    srv2.stop()


# --------------------------------------------------------------------------- discovery
def test_udp_discovery(server):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    Bot(server.port, "host", create=True)
    DiscoveryResponder(server.rooms, server.port, port, log=lambda m: None).start()
    time.sleep(0.2)
    found = discover(udp_port=port, timeout=0.8)
    assert found, "no server discovered"
    assert found[0]["port"] == server.port
    assert len(found[0]["rooms"]) == 1


# --------------------------------------------------------------------------- performance
def test_slow_client_does_not_block_others(server):
    """A client that never reads must not delay delivery to everyone else."""
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    stuck = socket.create_connection(("127.0.0.1", server.port))
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    P.send_msg(stuck, {"type": P.HELLO, "version": P.PROTOCOL_VERSION, "username": "stuck", "room": a.room})
    big = list(range(0, 4000))
    t0 = time.time()
    for i in range(300):
        a.stroke(big, width=2)
    assert wait_until(lambda: len(b.model.visible_ids()) == 300, [a, b], 20)
    assert time.time() - t0 < 15
    stuck.close()


def test_latency_lan_roundtrip(server):
    a = Bot(server.port, "alice", create=True)
    b = Bot(server.port, "bob", room=a.room)
    lat = []
    for i in range(50):
        t = time.perf_counter()
        a.stroke([i, i, i + 1, i + 1])
        while len(b.model.visible_ids()) < i + 1:      # block on the queue (no busy-wait)
            ev = b.net.events.get(timeout=2)
            if ev["type"] == P.OP:
                b.model.apply(ev["op"])
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    print(f"\nstroke propagation A->server->B: median {lat[25]:.2f} ms, p95 {lat[47]:.2f} ms")
    assert lat[25] < 20
