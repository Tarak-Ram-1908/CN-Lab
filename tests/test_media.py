"""File sharing: images, voice messages, videos, board images - all over the LAN."""
import hashlib
import os
import socket
import time
import wave

import pytest

from helpers import Bot, P, start_server, wait_until
from client.media import MediaClient


@pytest.fixture
def server(tmp_path):
    srv = start_server(save_dir=str(tmp_path / "srv"))
    yield srv
    srv.stop()


def make_file(path, size, seed=1):
    data = hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1)
    with open(path, "wb") as f:
        f.write(data[:size])
    return path


def make_wav(path, seconds=1.5, rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x10" * int(seconds * rate))
    return path


def with_media(bot, tmp_path):
    bot.media = MediaClient(bot.net, cache_dir=str(tmp_path / f"cache-{bot.name}"))
    return bot


def xfers(bot, state):
    return [e for e in bot.other if e["type"] == "_XFER" and e["state"] == state]


def test_send_image_with_caption_and_download(server, tmp_path):
    a = with_media(Bot(server.port, "alice", create=True), tmp_path)
    b = with_media(Bot(server.port, "bob", room=a.room), tmp_path)
    src = make_file(str(tmp_path / "photo.png"), 300_000)
    a.media.upload(src, kind="image", caption="look at this", dims=(800, 600))
    assert wait_until(lambda: any(c.get("attachment") for c in b.chat), [a, b], 10)
    msg = [c for c in b.chat if c.get("attachment")][0]
    att = msg["attachment"]
    assert msg["text"] == "look at this" and msg["user"] == "alice"
    assert att["kind"] == "image" and att["name"] == "photo.png" and att["size"] == 300_000
    assert att["dims"] == [800, 600]
    b.media.ensure(att)
    assert wait_until(lambda: xfers(b, "done"), [b], 10)
    got = b.media.cached_path(att)
    assert open(got, "rb").read() == open(src, "rb").read()
    # the sender never downloads its own file
    assert a.media.cached_path(att["media_id"])


def test_voice_message(server, tmp_path):
    a = with_media(Bot(server.port, "alice", create=True), tmp_path)
    b = with_media(Bot(server.port, "bob", room=a.room), tmp_path)
    from client.audio import wav_duration
    src = make_wav(str(tmp_path / "voice.wav"), 2.0)
    a.media.upload(src, kind="voice", duration=2.0)
    assert wait_until(lambda: any(c.get("attachment") for c in b.chat), [a, b], 10)
    att = [c for c in b.chat if c.get("attachment")][0]["attachment"]
    assert att["kind"] == "voice" and att["duration"] == 2.0
    b.media.ensure(att)
    assert wait_until(lambda: b.media.cached_path(att), [b], 10)
    assert abs(wav_duration(b.media.cached_path(att)) - 2.0) < 0.01


def test_large_video_does_not_delay_drawing(server, tmp_path):
    """A 60 MB upload runs on its own connection: strokes keep flowing at full speed."""
    a = with_media(Bot(server.port, "alice", create=True), tmp_path)
    b = with_media(Bot(server.port, "bob", room=a.room), tmp_path)
    src = make_file(str(tmp_path / "lecture.mp4"), 60 * 1024 * 1024)
    a.media.upload(src, kind="video")
    time.sleep(0.3)                                   # upload is now streaming
    lat = []
    for i in range(30):
        t = time.perf_counter()
        a.stroke([i, i, i + 1, i + 1])
        while len(b.model.visible_ids()) < i + 1:
            ev = b.net.events.get(timeout=5)
            if ev["type"] == P.OP:
                b.model.apply(ev["op"])
            elif ev["type"] == P.CHAT:
                b.chat.append(ev)
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    print(f"\nstroke latency during a 60 MB upload: median {lat[15]:.2f} ms, max {lat[-1]:.2f} ms")
    assert lat[15] < 30
    assert wait_until(lambda: any(c.get("attachment") for c in b.chat), [a, b], 60)
    att = [c for c in b.chat if c.get("attachment")][0]["attachment"]
    assert att["kind"] == "video" and att["size"] == 60 * 1024 * 1024
    b.media.ensure(att)
    assert wait_until(lambda: b.media.cached_path(att), [b], 60)
    assert os.path.getsize(b.media.cached_path(att)) == att["size"]


def test_image_on_board(server, tmp_path):
    a = with_media(Bot(server.port, "alice", create=True), tmp_path)
    b = with_media(Bot(server.port, "bob", room=a.room), tmp_path)
    src = make_file(str(tmp_path / "diagram.png"), 50_000)
    a.media.upload(src, kind="image", purpose="board", dims=(400, 300))
    assert wait_until(lambda: xfers(a, "done"), [a], 10)
    mid = xfers(a, "done")[0]["media_id"]
    assert not any(c.get("attachment") for c in a.chat)      # board uploads are not chat posts
    sid = a.net.new_stroke_id()
    a.net.send_board_op(P.STROKE, stroke={"id": sid, "tool": "image", "color": "#000000",
                                          "width": 1, "points": [100, 100, 500, 400],
                                          "media_id": mid})
    assert wait_until(lambda: b.model.visible_ids() == [sid], [a, b])
    assert b.model.strokes[sid]["media_id"] == mid
    # an image that was never uploaded is refused
    a.net.send_board_op(P.STROKE, stroke={"id": "x-s1", "tool": "image", "color": "#000000",
                                          "width": 1, "points": [1, 1, 50, 50],
                                          "media_id": "0" * 32})
    assert wait_until(lambda: any(e["code"] == P.E_NOT_FOUND for e in a.errors), [a, b])
    # undo and erase work on images like on any stroke
    a.net.send_board_op(P.UNDO)
    assert wait_until(lambda: b.model.visible_ids() == [], [a, b])


def test_late_joiner_can_download_shared_files(server, tmp_path):
    a = with_media(Bot(server.port, "alice", create=True), tmp_path)
    src = make_file(str(tmp_path / "notes.pdf"), 120_000)
    a.media.upload(src, kind="file", caption="slides")
    assert wait_until(lambda: any(c.get("attachment") for c in a.chat), [a], 10)
    late = with_media(Bot(server.port, "late", room=a.room), tmp_path)
    att = [c for c in late.chat if c.get("attachment")][0]["attachment"]
    late.media.ensure(att)
    assert wait_until(lambda: late.media.cached_path(att), [late], 10)


def _raw(server, req):
    s = socket.create_connection(("127.0.0.1", server.port))
    s.settimeout(5)
    P.send_msg(s, req)
    return s


def test_access_control_and_limits(server, tmp_path):
    a = with_media(Bot(server.port, "alice", create=True), tmp_path)
    src = make_file(str(tmp_path / "x.bin"), 10_000)
    a.media.upload(src, kind="file")
    assert wait_until(lambda: xfers(a, "done"), [a], 10)
    mid = xfers(a, "done")[0]["media_id"]
    # wrong token: no download
    s = _raw(server, {"type": P.MEDIA_GET, "room": a.room, "username": "alice",
                      "session_token": "f" * 32, "media_id": mid})
    assert P.recv_msg(s)["code"] == P.E_FORBIDDEN
    # oversized upload refused BEFORE any bytes are sent
    s = _raw(server, {"type": P.MEDIA_PUT, "room": a.room, "username": "alice",
                      "session_token": a.net.token, "name": "huge.mkv",
                      "size": P.MAX_MEDIA_BYTES + 1, "sha256": "0" * 64, "kind": "video"})
    reply = P.recv_msg(s)
    assert reply["type"] == P.ERROR and "too large" in reply["message"]
    # corrupted data (hash mismatch) is rejected and never shared
    s = _raw(server, {"type": P.MEDIA_PUT, "room": a.room, "username": "alice",
                      "session_token": a.net.token, "name": "../../evil.txt", "size": 5,
                      "sha256": "0" * 64, "kind": "file"})
    assert P.recv_msg(s)["type"] == P.MEDIA_READY
    s.sendall(b"hello")
    assert "Checksum" in P.recv_msg(s)["message"]
    a.pump(0.3)
    assert len([c for c in a.chat if c.get("attachment")]) == 1
    # stored file names cannot escape the media folder
    room = server.rooms.get(a.room)
    for meta in room.media.values():
        assert "/" not in meta["name"] and ".." not in meta["name"].split(".")[0]


def test_media_survives_server_restart(tmp_path):
    srv = start_server(save_dir=str(tmp_path / "srv"))
    a = with_media(Bot(srv.port, "alice", create=True), tmp_path)
    src = make_file(str(tmp_path / "keep.png"), 20_000)
    a.media.upload(src, kind="image")
    assert wait_until(lambda: any(c.get("attachment") for c in a.chat), [a], 10)
    att = [c for c in a.chat if c.get("attachment")][0]["attachment"]
    srv.stop()
    srv2 = start_server(save_dir=str(tmp_path / "srv"))
    b = with_media(Bot(srv2.port, "bob", room=a.room), tmp_path)
    assert any(c.get("attachment", {}).get("media_id") == att["media_id"] for c in b.chat)
    b.media.ensure(att)
    assert wait_until(lambda: b.media.cached_path(att), [b], 10)
    srv2.stop()
