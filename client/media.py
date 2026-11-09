"""
Client side of file sharing: uploads, downloads and the local media cache.

Every transfer runs in its own background thread on its OWN TCP connection
(see server/media.py for why), so the board connection is never blocked.
Progress and results are reported to the GUI as events on net.events:

    {"type": "_XFER", "xfer": id, "state": "progress", "done": n, "size": total, ...}
    {"type": "_XFER", "xfer": id, "state": "done", "media_id": ..., "path": ...}
    {"type": "_XFER", "xfer": id, "state": "error", "message": ...}
"""

import hashlib
import itertools
import mimetypes
import os
import shutil
import socket
import threading
import time

from common import protocol as P

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".m4v", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".wma", ".opus"}

_xfer_ids = itertools.count(1)


def kind_for(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "file"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_media_dir():
    """User-visible folder where received files are kept."""
    return os.path.join(os.path.expanduser("~"), "LAN Whiteboard Files")


class MediaClient:
    def __init__(self, net, cache_dir=None):
        self.net = net
        self.dir = cache_dir or default_media_dir()
        self.lock = threading.Lock()
        self.inflight = {}             # media_id -> list of callbacks waiting for it
        self.local = {}                # media_id -> local path (cache index)

    # ------------------------------------------------------------------ cache
    def room_dir(self):
        d = os.path.join(self.dir, str(self.net.hello.get("room") or "room"))
        os.makedirs(d, exist_ok=True)
        return d

    def cached_path(self, meta_or_id, name=None):
        """Path of a file we already have (verified), else None."""
        mid = meta_or_id if isinstance(meta_or_id, str) else meta_or_id["media_id"]
        with self.lock:
            p = self.local.get(mid)
        if p and os.path.exists(p):
            return p
        d = self.room_dir()
        for f in os.listdir(d):
            if f.startswith(mid[:12] + "_") and not f.endswith(".part"):
                with self.lock:
                    self.local[mid] = os.path.join(d, f)
                return os.path.join(d, f)
        return None

    def _target(self, mid, name):
        return os.path.join(self.room_dir(), f"{mid[:12]}_{P.safe_filename(name)}")

    # ------------------------------------------------------------------ connection
    def _connect(self):
        sock = self.net.open_socket(10)
        sock.settimeout(30)
        return sock

    def _auth(self):
        return {"room": self.net.hello.get("room"), "username": self.net.hello["username"],
                "session_token": self.net.token}

    def _event(self, **kw):
        self.net.events.put({"type": "_XFER", **kw})

    # ------------------------------------------------------------------ upload
    def upload(self, path, kind=None, purpose="chat", caption="", duration=None, dims=None,
               on_done=None):
        """Start uploading `path` in the background. Returns a transfer id."""
        xid = next(_xfer_ids)
        name = os.path.basename(path)
        kind = kind or kind_for(path)
        threading.Thread(target=self._upload, daemon=True,
                         args=(xid, path, name, kind, purpose, caption, duration, dims,
                               on_done)).start()
        return xid

    def _upload(self, xid, path, name, kind, purpose, caption, duration, dims, on_done):
        try:
            size = os.path.getsize(path)
            limit = P.MAX_BOARD_IMAGE if purpose == "board" else P.MAX_MEDIA_BYTES
            if size > limit:
                raise ValueError(f"{name} is {human_size(size)}; the limit is "
                                 f"{human_size(limit)}")
            if size == 0:
                raise ValueError(f"{name} is empty")
            self._event(xfer=xid, state="start", direction="up", name=name, size=size,
                        kind=kind)
            sha = sha256_file(path)
            req = {"type": P.MEDIA_PUT, **self._auth(), "name": name, "size": size,
                   "sha256": sha, "kind": kind, "purpose": purpose, "caption": caption,
                   "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"}
            if duration is not None:
                req["duration"] = duration
            if dims:
                req["dims"] = list(dims)
            sock = self._connect()
            try:
                P.send_msg(sock, req)
                reply = P.recv_msg(sock)                    # rejected before we send bytes?
                if reply["type"] == P.ERROR:
                    raise ValueError(reply.get("message", "upload refused"))
                sent, last = 0, 0.0
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(P.MEDIA_CHUNK), b""):
                        sock.sendall(chunk)
                        sent += len(chunk)
                        now = time.monotonic()
                        if now - last > 0.15:
                            last = now
                            self._event(xfer=xid, state="progress", direction="up",
                                        name=name, done=sent, size=size)
                reply = P.recv_msg(sock)
                if reply["type"] == P.ERROR:
                    raise ValueError(reply.get("message", "upload failed"))
                mid = reply["media_id"]
                # Register our original right away: the chat broadcast announcing this
                # file can arrive before we finish copying it into the cache.
                with self.lock:
                    self.local[mid] = path
            finally:
                sock.close()
            # Keep our own copy in the cache so we never download what we sent.
            target = self._target(mid, name)
            shutil.copyfile(path, target)
            with self.lock:
                self.local[mid] = target
            self._event(xfer=xid, state="done", direction="up", name=name, media_id=mid,
                        path=target, kind=kind, purpose=purpose)
            if on_done:
                on_done(mid, target)
        except (OSError, ValueError, P.ProtocolError) as exc:
            self._event(xfer=xid, state="error", direction="up", name=name, message=str(exc))

    # ------------------------------------------------------------------ download
    def ensure(self, meta, on_ready=None, quiet=False):
        """Make sure a file is cached; call on_ready(path) when it is (any thread)."""
        path = self.cached_path(meta)
        if path:
            if on_ready:
                on_ready(path)
            return path
        mid = meta["media_id"]
        with self.lock:
            waiting = self.inflight.get(mid)
            if waiting is not None:               # already downloading: just wait for it
                if on_ready:
                    waiting.append(on_ready)
                return None
            self.inflight[mid] = [on_ready] if on_ready else []
        threading.Thread(target=self._download, args=(meta, quiet), daemon=True).start()
        return None

    def _download(self, meta, quiet):
        xid = next(_xfer_ids)
        mid = meta["media_id"]
        name = meta.get("name", "file")
        path = None
        try:
            sock = self._connect()
            try:
                P.send_msg(sock, {"type": P.MEDIA_GET, **self._auth(), "media_id": mid})
                head = P.recv_msg(sock)
                if head["type"] == P.ERROR:
                    raise ValueError(head.get("message", "download refused"))
                size, sha = head["size"], head["sha256"]
                if not quiet:
                    self._event(xfer=xid, state="start", direction="down", name=name,
                                size=size, media_id=mid)
                target = self._target(mid, head.get("name", name))
                tmp = target + ".part"
                h = hashlib.sha256()
                got, last = 0, 0.0
                with open(tmp, "wb") as f:
                    while got < size:
                        chunk = sock.recv(min(P.MEDIA_CHUNK, size - got))
                        if not chunk:
                            raise ConnectionError("download interrupted")
                        f.write(chunk)
                        h.update(chunk)
                        got += len(chunk)
                        now = time.monotonic()
                        if not quiet and now - last > 0.15:
                            last = now
                            self._event(xfer=xid, state="progress", direction="down",
                                        name=name, done=got, size=size, media_id=mid)
            finally:
                sock.close()
            if h.hexdigest() != sha:
                os.remove(tmp)
                raise ValueError("checksum mismatch - please retry")
            os.replace(tmp, target)
            path = target
            with self.lock:
                self.local[mid] = target
            self._event(xfer=xid, state="done", direction="down", name=name, media_id=mid,
                        path=target, quiet=quiet)
        except (OSError, ValueError, ConnectionError, P.ProtocolError) as exc:
            self._event(xfer=xid, state="error", direction="down", name=name, media_id=mid,
                        message=str(exc))
        finally:
            with self.lock:
                callbacks = self.inflight.pop(mid, [])
            if path:
                for cb in callbacks:
                    if cb:
                        cb(path)


def open_with_system(path):
    """Open a file with the computer's default app (offline video/audio players etc.)."""
    import subprocess
    import sys
    if sys.platform.startswith("win"):
        os.startfile(path)                                     # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
