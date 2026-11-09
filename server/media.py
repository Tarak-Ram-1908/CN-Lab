"""
Media sharing: images, videos, audio files, voice messages and other files.

Why a separate TCP connection per transfer?
-------------------------------------------
TCP delivers bytes strictly in order. If a 100 MB video were sent over the
same connection as the drawing traffic, every stroke queued behind it would
wait until the whole video had passed (head-of-line blocking). So a client
opens a short-lived extra connection to the same port just for one upload or
download, while its main connection keeps carrying strokes, chat and cursors
with normal sub-millisecond latency.

Transfer protocol (first frame decides what the connection is for)
------------------------------------------------------------------
Upload:
    C -> S  {"type":"MEDIA_PUT", room, username, session_token, name, size,
             sha256, kind, mime, purpose:"chat"|"board", caption?, duration?}
    S -> C  {"type":"MEDIA_READY"}          (or ERROR - rejected before any bytes flow)
    C -> S  exactly `size` raw bytes
    S -> C  {"type":"MEDIA_OK", media_id}   (or ERROR if the SHA-256 does not match)
Download:
    C -> S  {"type":"MEDIA_GET", room, username, session_token, media_id}
    S -> C  {"type":"MEDIA_DATA", size, name, mime, sha256}  then `size` raw bytes

Only members of the room (proved by their session token) can upload or
download. Files are stored under <save-dir>/media/<ROOM>/<media_id>.
"""

import hashlib
import json
import mimetypes
import os
import secrets
import time

from common import protocol as P


class MediaStore:
    def __init__(self, base_dir):
        self.dir = os.path.join(os.path.abspath(base_dir), "media")
        os.makedirs(self.dir, exist_ok=True)

    def _room_dir(self, room_code):
        d = os.path.join(self.dir, "".join(c for c in room_code.upper() if c.isalnum()))
        os.makedirs(d, exist_ok=True)
        return d

    def path(self, room_code, media_id):
        return os.path.join(self._room_dir(room_code), media_id)

    def load_index(self, room_code):
        """All media metadata of one room (used when a room is restored)."""
        out = {}
        d = self._room_dir(room_code)
        for f in os.listdir(d):
            if f.endswith(".json") and P.valid_media_id(f[:-5]):
                try:
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        meta = json.load(fh)
                    if os.path.exists(os.path.join(d, f[:-5])):
                        out[f[:-5]] = meta
                except (OSError, ValueError):
                    pass
        return out


def _err(handler, code, message):
    handler.send_now({"type": P.ERROR, "code": code, "message": message})


def _member(handler, req):
    """Find the room and participant that this transfer request belongs to."""
    room = handler.rooms.get(req.get("room"))
    if room is None:
        return None, None
    part = room.member(req.get("username"), req.get("session_token"))
    return room, part


def handle_put(handler, req, store, log):
    room, part = _member(handler, req)
    if part is None:
        return _err(handler, P.E_FORBIDDEN, "Not a member of this room")
    size, sha = req.get("size"), req.get("sha256")
    kind = req.get("kind")
    purpose = req.get("purpose", "chat")
    if not isinstance(size, int) or size <= 0:
        return _err(handler, P.E_BAD_MESSAGE, "bad size")
    limit = P.MAX_BOARD_IMAGE if purpose == "board" else P.MAX_MEDIA_BYTES
    if size > limit:
        return _err(handler, P.E_BAD_MESSAGE,
                    f"File too large (limit {limit // (1024 * 1024)} MB)")
    if kind not in P.MEDIA_KINDS or purpose not in ("chat", "board"):
        return _err(handler, P.E_BAD_MESSAGE, "bad kind")
    if purpose == "board" and kind != "image":
        return _err(handler, P.E_BAD_MESSAGE, "only images can be placed on the board")
    if not P.valid_sha256(sha):
        return _err(handler, P.E_BAD_MESSAGE, "bad sha256")
    name = P.safe_filename(req.get("name"))
    mime = req.get("mime") if isinstance(req.get("mime"), str) else None
    mime = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream")[:100]
    caption = req.get("caption") if isinstance(req.get("caption"), str) else ""
    duration = req.get("duration")
    duration = round(float(duration), 1) if isinstance(duration, (int, float)) else None
    dims = req.get("dims")
    dims = dims if (isinstance(dims, list) and len(dims) == 2 and
                    all(isinstance(v, int) and 0 < v < 100000 for v in dims)) else None

    media_id = secrets.token_hex(16)
    final = store.path(room.code, media_id)
    tmp = final + ".part"
    handler.send_now({"type": P.MEDIA_READY})

    # Stream the raw bytes straight to disk, hashing as we go.
    h = hashlib.sha256()
    remaining = size
    try:
        with open(tmp, "wb") as f:
            while remaining:
                chunk = handler.sock.recv(min(P.MEDIA_CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("upload interrupted")
                f.write(chunk)
                h.update(chunk)
                remaining -= len(chunk)
    except (OSError, ConnectionError):
        _cleanup(tmp)
        log(f"[{room.code}] upload of {name} from {part.username} interrupted")
        return None
    if h.hexdigest() != sha:
        _cleanup(tmp)
        return _err(handler, P.E_BAD_MESSAGE, "Checksum mismatch - file corrupted in transit")
    os.replace(tmp, final)
    meta = {"media_id": media_id, "name": name, "mime": mime, "size": size, "sha256": sha,
            "kind": kind, "user": part.username, "ts": round(time.time(), 3)}
    if duration is not None:
        meta["duration"] = duration
    if dims:
        meta["dims"] = dims
    with open(final + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    room.add_media(meta)
    handler.send_now({"type": P.MEDIA_OK, "media_id": media_id})
    log(f"[{room.code}] {part.username} uploaded {kind} '{name}' ({size / 1048576:.1f} MB)")
    if purpose == "chat":
        room.chat_msg(part, caption, attachment=meta)
    return None


def handle_get(handler, req, store, log):
    room, part = _member(handler, req)
    if part is None:
        return _err(handler, P.E_FORBIDDEN, "Not a member of this room")
    media_id = req.get("media_id")
    meta = room.media.get(media_id) if P.valid_media_id(media_id) else None
    path = store.path(room.code, media_id) if meta else None
    if meta is None or not os.path.exists(path):
        return _err(handler, P.E_NOT_FOUND, "No such file")
    handler.send_now({"type": P.MEDIA_DATA, "size": meta["size"], "name": meta["name"],
                      "mime": meta["mime"], "sha256": meta["sha256"]})
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(P.MEDIA_CHUNK)
                if not chunk:
                    break
                handler.sock.sendall(chunk)
    except OSError:
        log(f"[{room.code}] download of {meta['name']} by {part.username} interrupted")
    return None


def _cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass
