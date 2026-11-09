"""Framing and validation tests."""
import socket
import threading

import pytest

import helpers  # noqa: F401  (sets sys.path)
from common import protocol as P


def test_frame_split_byte_by_byte():
    dec = P.FrameDecoder()
    data = P.encode({"type": "CHAT", "text": "hello"}) + P.encode({"type": "PING", "t": 1})
    out = []
    for i in range(len(data)):                 # worst case: one byte per recv()
        out += dec.feed(data[i:i + 1])
    assert out == [{"type": "CHAT", "text": "hello"}, {"type": "PING", "t": 1}]


def test_frames_coalesced_in_one_chunk():
    dec = P.FrameDecoder()
    msgs = [{"type": "OP", "seq": i} for i in range(50)]
    assert dec.feed(b"".join(P.encode(m) for m in msgs)) == msgs


def test_recv_msg_over_real_socket_with_fragmentation():
    a, b = socket.socketpair()
    payload = {"type": "STROKE", "points": list(range(5000))}
    frame = P.encode(payload)

    def slow_sender():
        for i in range(0, len(frame), 7):      # dribble 7 bytes at a time
            a.sendall(frame[i:i + 7])
    threading.Thread(target=slow_sender).start()
    assert P.recv_msg(b) == payload
    a.close()
    b.close()


def test_oversized_frame_rejected():
    a, b = socket.socketpair()
    a.sendall(P.HEADER.pack(P.MAX_FRAME + 1))
    with pytest.raises(P.ProtocolError):
        P.recv_msg(b)


def test_invalid_json_rejected():
    a, b = socket.socketpair()
    a.sendall(P.HEADER.pack(5) + b"{oops")
    with pytest.raises(P.ProtocolError):
        P.recv_msg(b)


def test_sanitize_stroke_clamps_and_validates():
    # infinite canvas: negative and large world coordinates are fine; only absurd
    # values are clamped
    s = P.sanitize_stroke({"id": "x1", "tool": "pen", "color": "#AABBCC", "width": 3,
                           "points": [-50, 10, 99999, 1e12]})
    assert s["points"] == [-50, 10, 99999, P.WORLD_LIMIT]
    assert s["color"] == "#aabbcc"
    for bad in [{"tool": "laser"}, {"color": "red"}, {"width": 0}, {"points": [1, 2, 3]},
                {"points": [1, "a"]}]:
        base = {"id": "x", "tool": "pen", "color": "#000000", "width": 2, "points": [1, 2]}
        base.update(bad)
        with pytest.raises(P.ProtocolError):
            P.sanitize_stroke(base)
    with pytest.raises(P.ProtocolError):           # shapes need exactly 2 points
        P.sanitize_stroke({"id": "r", "tool": "rect", "color": "#000000", "width": 2,
                           "points": [1, 2, 3, 4, 5, 6]})
    with pytest.raises(P.ProtocolError):           # polygons need 3+ vertices
        P.sanitize_stroke({"id": "p", "tool": "polygon", "color": "#000000", "width": 2,
                           "points": [1, 2, 3, 4]})
    tri = P.sanitize_stroke({"id": "p", "tool": "polygon", "color": "#000000", "width": 2,
                             "points": [0, 0, 10, 0, 5, 8]})
    assert tri["tool"] == "polygon"
    with pytest.raises(P.ProtocolError):           # text needs text
        P.sanitize_stroke({"id": "t", "tool": "text", "color": "#000000", "width": 2,
                           "points": [1, 2]})


def test_usernames():
    assert P.valid_username("alice_01")
    for bad in ["", "a b", "x" * 21, "<script>", None]:
        assert not P.valid_username(bad)
