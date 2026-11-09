"""
Application-level protocol shared by the server and the client.

Wire format (TCP)
-----------------
TCP delivers a *byte stream*, not messages: one recv() may return half a
message or several messages glued together. Every message is therefore framed
with a 4-byte big-endian length prefix followed by that many bytes of UTF-8
JSON:

    +----------------+--------------------------------+
    | length: uint32 | body: UTF-8 JSON (length bytes)|
    +----------------+--------------------------------+

Every JSON body is an object with a "type" field (see the constants below).
Board-changing messages also carry a client-generated "op_id" (used for
duplicate detection) and the server adds a global "seq" number when it
broadcasts them (used for ordering).

Large frames (snapshots, history) are zlib-compressed: the top bit of the
length field is then set ("compressed" flag) and the length is that of the
compressed body. The receiver inflates it with a hard size cap, so a tiny
malicious frame cannot expand into gigabytes.

UDP is used only for optional LAN room discovery (see DISCOVER_* below).
"""

import json
import re
import socket
import ssl
import struct
import zlib

# ---------------------------------------------------------------------------
# Network constants
# ---------------------------------------------------------------------------
DEFAULT_TCP_PORT = 5050          # session traffic (reliable, ordered)
DEFAULT_UDP_PORT = 5051          # discovery broadcasts only
PROTOCOL_VERSION = 2

MAX_FRAME = 4 * 1024 * 1024      # 4 MB on the wire: garbage sizes are rejected
MAX_INFLATED = 32 * 1024 * 1024  # a compressed frame may expand to at most 32 MB
COMPRESS_MIN = 32 * 1024         # bodies larger than this are zlib-compressed
COMPRESSED_FLAG = 0x80000000     # top bit of the length field
HEADER = struct.Struct("!I")     # network byte order, unsigned 32-bit length

HEARTBEAT_INTERVAL = 3.0         # client sends PING this often (seconds)
DEAD_TIMEOUT = 10.0              # no traffic for this long => peer considered dead
GRACE_PERIOD = 30.0              # a dropped user keeps their seat this long
MAX_SEND_QUEUE = 5000            # per-client backlog before we drop a slow client

# The board is an infinite canvas in "world" coordinates shared by every client.
# CANVAS_W x CANVAS_H is only the area shown when a board is first opened.
CANVAS_W, CANVAS_H = 1600, 900
WORLD_LIMIT = 1_000_000          # |x|, |y| are clamped to this (keeps numbers sane)

DISCOVER_MAGIC = "WB_DISCOVER"   # client -> broadcast
DISCOVER_REPLY = "WB_HERE"       # server -> client (unicast reply)

# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------
# client -> server
HELLO = "HELLO"              # create / join / reconnect
STROKE = "STROKE"            # finished freehand stroke, shape, text or sticky note
MOVE = "MOVE"                # move existing objects by (dx, dy) - undoable like any op
STROKE_PART = "STROKE_PART"  # live preview while the pen is down (not stored)
ERASE = "ERASE"              # hide whole strokes
CLEAR = "CLEAR"              # clear the board (host only)
UNDO = "UNDO"                # undo the sender's own last operation
REDO = "REDO"                # redo the sender's own last undone operation
CHAT = "CHAT"
CURSOR = "CURSOR"
ADMIN = "ADMIN"              # host controls: lock / unlock / kick / set_role
SAVE = "SAVE"                # save the board on the server
LOAD = "LOAD"                # load a saved board
LIST_SAVES = "LIST_SAVES"
VOTE = "VOTE"                # toggle my vote on an object (dot voting)
VOTES_CLEAR = "VOTES_CLEAR"  # host: reset all votes
TIMER = "TIMER"              # host: start / pause / resume / reset the session timer
FOLLOW = "FOLLOW"            # host: presentation ("follow me") mode on / off
VIEWPORT = "VIEWPORT"        # presenter's current view, relayed to followers
EDIT = "EDIT"                # change properties of an object (colour, size, text, points...)
SETTINGS = "SETTINGS"        # host: room settings (own-work-only, paper style, pages)
DM = "DM"                    # private message to one participant
LASER = "LASER"              # laser pointer trail (relayed, never stored)
HAND = "HAND"                # raise / lower hand
POLL = "POLL"                # quick poll: start / answer / end / clear
RESTORE = "RESTORE"          # host: replace the board with one from a local file
CLOSE_ROOM = "CLOSE_ROOM"    # host: end the session for everyone
HISTORY = "HISTORY"          # request (client) / reply (server) of the version history
PING = "PING"
BYE = "BYE"

# server -> client
WELCOME = "WELCOME"          # handshake reply carrying the full state
OP = "OP"                    # accepted board operation with its global seq
PREVIEW = "PREVIEW"          # relayed STROKE_PART
PRESENCE = "PRESENCE"        # participant joined / left / away / role change
RESET = "RESET"              # whole board replaced (after LOAD)
SAVES = "SAVES"              # list of saved boards
ROOM_STATE = "ROOM_STATE"    # lock flag / presentation mode changed
VOTES = "VOTES"              # current voters of one object (or all, after a reset)
TIMER_STATE = "TIMER_STATE"  # timer running/paused + seconds remaining
INFO = "INFO"                # human-readable notice (e.g. "Board saved")
ERROR = "ERROR"
ACK = "ACK"                  # op accepted but changed nothing (e.g. duplicate)
PONG = "PONG"
KICKED = "KICKED"
POLL_STATE = "POLL_STATE"    # current poll with live counts

# Media transfer - each runs on its OWN short-lived TCP connection so that a
# large video never sits in front of drawing traffic on the main connection.
MEDIA_PUT = "MEDIA_PUT"      # client -> server: header, then exactly `size` raw bytes
MEDIA_READY = "MEDIA_READY"  # server -> client: header accepted, start streaming
MEDIA_OK = "MEDIA_OK"        # server -> client: stored (carries media_id)
MEDIA_GET = "MEDIA_GET"      # client -> server: request a file by media_id
MEDIA_DATA = "MEDIA_DATA"    # server -> client: header, then exactly `size` raw bytes

# Board operation kinds stored in the log (inside OP messages)
BOARD_KINDS = (STROKE, MOVE, EDIT, ERASE, CLEAR, UNDO, REDO)

# Roles
ROLE_HOST, ROLE_EDITOR, ROLE_VIEWER = "host", "editor", "viewer"
ROLES = (ROLE_HOST, ROLE_EDITOR, ROLE_VIEWER)

# Error codes
E_BAD_MESSAGE = "BAD_MESSAGE"
E_BAD_PASSWORD = "BAD_PASSWORD"
E_NAME_TAKEN = "NAME_TAKEN"
E_ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
E_ROOM_FULL = "ROOM_FULL"
E_FORBIDDEN = "FORBIDDEN"
E_LOCKED = "LOCKED"
E_RATE_LIMIT = "RATE_LIMIT"
E_BANNED = "BANNED"
E_NOT_FOUND = "NOT_FOUND"
E_VERSION = "VERSION"

# Tools a stroke may use. Shapes are stored as strokes so the server never
# needs to understand geometry.
TOOLS = ("pen", "highlighter", "line", "arrow", "rect", "ellipse", "polygon", "text", "sticky",
         "image", "node", "connector")
TWO_POINT_TOOLS = ("line", "arrow", "rect", "ellipse", "image", "node", "connector")
NODE_SHAPES = ("rounded", "rect", "diamond", "ellipse", "parallelogram")
CONNECTOR_STYLES = ("arrow", "line", "curve")
EDITABLE = ("color", "width", "fill", "text", "points", "shape", "style", "label")
PAPERS = ("plain", "grid", "ruled", "dots", "graph")
SETTINGS_DEFAULT = {"own_only": False, "paper": "grid", "pages": 1}
MAX_PAGES = 50
PAGE_W, PAGE_H, PAGE_GAP = 1600, 900, 200     # page n spans x = n*(W+GAP) .. +W
MAX_POLL_OPTIONS = 6
MAX_DM_HISTORY = 500
MAX_LASER_POINTS = 64

MEDIA_KINDS = ("image", "video", "audio", "voice", "file")
MAX_MEDIA_BYTES = 200 * 1024 * 1024   # 200 MB per file (LAN, so this is quick)
MAX_BOARD_IMAGE = 25 * 1024 * 1024    # images placed on the canvas
MEDIA_CHUNK = 64 * 1024

# 12 distinct user colours, assigned in join order.
USER_COLORS = [
    "#e63946", "#1d70b8", "#2a9d8f", "#f4a261", "#8e44ad", "#d62f8f",
    "#2b9348", "#b5651d", "#0096c7", "#6a4c93", "#c1121f", "#457b9d",
]

MAX_USERS_PER_ROOM = 20
MAX_VOTES_PER_USER = 5           # dot voting: each participant has 5 votes
MAX_TIMER_SECONDS = 3 * 3600
MAX_POINTS = 10000               # coordinates per stroke (5000 points)
MAX_CHAT_LEN = 500
MAX_TEXT_LEN = 1000
MAX_LABEL_LEN = 80
CHAT_HISTORY = 200

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ProtocolError(Exception):
    """Raised when a peer sends something that violates the protocol."""


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------
def encode(obj):
    """Serialise one message into a length-prefixed frame (bytes).

    Bodies over COMPRESS_MIN are zlib-compressed (flag bit set in the header)
    when that makes them smaller - snapshots shrink to about a fifth.
    """
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    flag = 0
    if len(body) > COMPRESS_MIN:
        if len(body) > MAX_INFLATED:
            raise ProtocolError("message too large")
        packed = zlib.compress(body, 6)
        if len(packed) < len(body):
            body, flag = packed, COMPRESSED_FLAG
    if len(body) > MAX_FRAME:
        raise ProtocolError("message too large")
    return HEADER.pack(len(body) | flag) + body


def _parse_header(raw):
    """-> (body_length, compressed?) with the size guard applied."""
    compressed = bool(raw & COMPRESSED_FLAG)
    length = raw & ~COMPRESSED_FLAG
    if length > MAX_FRAME:
        raise ProtocolError(f"frame too large ({length} bytes)")
    return length, compressed


def _decode_body(body, compressed):
    if compressed:
        d = zlib.decompressobj()
        try:
            body = d.decompress(body, MAX_INFLATED)
        except zlib.error as exc:
            raise ProtocolError(f"bad compressed frame: {exc}") from None
        if d.unconsumed_tail:
            raise ProtocolError("compressed frame expands too much")
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ProtocolError("message must be an object with a 'type'")
    return obj


def send_msg(sock, obj):
    """Send one message. sendall() loops until every byte is written."""
    sock.sendall(encode(obj))


def recv_exact(sock, n):
    """Read exactly n bytes, looping because recv() may return fewer."""
    chunks = []
    remaining = n
    while remaining:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:                       # orderly shutdown by the peer
            raise ConnectionError("peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock):
    """Read one complete message -> (message, bytes on the wire)."""
    (raw,) = HEADER.unpack(recv_exact(sock, HEADER.size))
    length, compressed = _parse_header(raw)
    return _decode_body(recv_exact(sock, length), compressed), HEADER.size + length


def recv_msg(sock):
    """Read one complete message (blocks until it has fully arrived)."""
    return recv_frame(sock)[0]


class FrameDecoder:
    """Incremental decoder: feed it arbitrary byte chunks, get whole messages.

    Not used by the threaded server/client (they use recv_msg), but handy for
    tests and for explaining framing: it shows exactly how partial and
    coalesced TCP segments are reassembled.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf.extend(data)
        out = []
        while len(self._buf) >= HEADER.size:
            (raw,) = HEADER.unpack_from(self._buf)
            length, compressed = _parse_header(raw)
            if len(self._buf) < HEADER.size + length:
                break                                   # wait for more bytes
            body = bytes(self._buf[HEADER.size:HEADER.size + length])
            del self._buf[:HEADER.size + length]
            out.append(_decode_body(body, compressed))
        return out


def tune_socket(sock):
    """Low-latency settings for small, frequent drawing messages."""
    # Disable Nagle's algorithm so a small stroke message is sent immediately
    # instead of waiting to be coalesced with later data.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # OS-level keepalive as a second line of defence behind our heartbeats.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


def server_tls_context(certfile, keyfile):
    """Optional TLS for the server (python run_server.py --tls-cert c.pem --tls-key k.pem)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


def client_tls_wrap(sock, cafile=None):
    """Wrap a client socket in TLS. With a self-signed certificate (the usual lab
    setup) traffic is encrypted but the server is not authenticated unless the
    certificate is given as `cafile`."""
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    ctx.check_hostname = False
    if not cafile:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(sock)


# ---------------------------------------------------------------------------
# Validation (the server never trusts the client)
# ---------------------------------------------------------------------------
def valid_username(name):
    return isinstance(name, str) and bool(_USERNAME_RE.match(name))


def valid_color(color):
    return isinstance(color, str) and bool(_COLOR_RE.match(color))


def valid_save_name(name):
    return isinstance(name, str) and bool(_SAVE_NAME_RE.match(name))


def valid_media_id(mid):
    return isinstance(mid, str) and bool(_MEDIA_ID_RE.match(mid))


def valid_sha256(h):
    return isinstance(h, str) and bool(_SHA_RE.match(h))


def safe_filename(name):
    """Strip any directory part and unsafe characters from a client-supplied file name."""
    if not isinstance(name, str):
        return "file"
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._\- ()]", "_", name).strip(" .")
    return (name or "file")[:120]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sanitize_stroke(stroke):
    """Validate a stroke coming from a client and return a clean copy.

    Raises ProtocolError on anything malformed. Coordinates are world
    coordinates on the infinite canvas, clamped to +-WORLD_LIMIT.
    """
    if not isinstance(stroke, dict):
        raise ProtocolError("stroke must be an object")
    sid = stroke.get("id")
    tool = stroke.get("tool")
    color = stroke.get("color")
    width = stroke.get("width")
    pts = stroke.get("points")
    if not isinstance(sid, str) or not 1 <= len(sid) <= 64:
        raise ProtocolError("bad stroke id")
    if tool not in TOOLS:
        raise ProtocolError("unknown tool")
    if not valid_color(color):
        raise ProtocolError("bad colour")
    if not isinstance(width, (int, float)) or isinstance(width, bool) or not 1 <= width <= 60:
        raise ProtocolError("bad width")
    if (not isinstance(pts, list) or len(pts) < 2 or len(pts) % 2
            or len(pts) > MAX_POINTS):
        raise ProtocolError("bad points")
    clean_pts = []
    for i, v in enumerate(pts):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ProtocolError("bad coordinate")
        clean_pts.append(round(_clamp(float(v), -WORLD_LIMIT, WORLD_LIMIT), 1))
    if tool in TWO_POINT_TOOLS and len(clean_pts) != 4:
        raise ProtocolError("shapes need exactly two points")
    if tool == "polygon" and not 6 <= len(clean_pts) <= 40:
        raise ProtocolError("polygons need 3-20 vertices")
    clean = {"id": sid, "tool": tool, "color": color.lower(),
             "width": round(float(width), 1), "points": clean_pts}
    if tool in ("text", "sticky"):
        text = stroke.get("text")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_LEN:
            raise ProtocolError("bad text")
        clean["text"] = text
    if tool == "node":
        if stroke.get("shape", "rounded") not in NODE_SHAPES:
            raise ProtocolError("bad node shape")
        clean["shape"] = stroke.get("shape", "rounded")
        text = stroke.get("text", "")
        if not isinstance(text, str) or len(text) > MAX_TEXT_LEN:
            raise ProtocolError("bad text")
        clean["text"] = text
    if tool == "connector":
        for end in ("src", "dst"):
            v = stroke.get(end)
            if not isinstance(v, str) or not 1 <= len(v) <= 64:
                raise ProtocolError("connector needs src and dst node ids")
            clean[end] = v
        if clean["src"] == clean["dst"]:
            raise ProtocolError("a connector needs two different nodes")
        if stroke.get("style", "arrow") not in CONNECTOR_STYLES:
            raise ProtocolError("bad connector style")
        clean["style"] = stroke.get("style", "arrow")
        label = stroke.get("label", "")
        if not isinstance(label, str) or len(label) > MAX_LABEL_LEN:
            raise ProtocolError("bad label")
        if label:
            clean["label"] = label
    if tool == "image":
        if not valid_media_id(stroke.get("media_id")):
            raise ProtocolError("bad media_id")
        clean["media_id"] = stroke["media_id"]
    if stroke.get("fill") is not None:
        if not valid_color(stroke["fill"]):
            raise ProtocolError("bad fill")
        clean["fill"] = stroke["fill"].lower()
    return clean


def sanitize_edit(stroke, changes):
    """Validate an EDIT of an existing (current) stroke.

    Returns the clean {field: value} dict to store; a value of None removes the
    field (e.g. fill=None = no fill). The edited stroke as a whole must still
    be a valid stroke, so an edit can never create something a STROKE could not.
    """
    if not isinstance(changes, dict) or not changes or len(changes) > len(EDITABLE):
        raise ProtocolError("bad edit")
    if any(k not in EDITABLE for k in changes):
        raise ProtocolError("field cannot be edited")
    merged = dict(stroke)
    for k, v in changes.items():
        if v is None or v == "" and k in ("fill", "label"):
            merged.pop(k, None)
        else:
            merged[k] = v
    if merged["tool"] in ("text", "sticky") and not merged.get("text"):
        raise ProtocolError("text cannot be empty")
    clean = sanitize_stroke(merged)
    if "points" in changes and len(clean["points"]) != len(stroke["points"]) \
            and stroke["tool"] not in ("pen", "highlighter", "polygon"):
        raise ProtocolError("wrong number of points")
    return {k: clean.get(k) for k in changes}


def world_coord(v):
    """Validate one world coordinate (cursor, viewport, move offset)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
        raise ProtocolError("bad coordinate")
    return round(_clamp(float(v), -WORLD_LIMIT, WORLD_LIMIT), 1)
