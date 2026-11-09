"""
One ClientHandler per TCP connection.

Each handler runs two threads:

* reader  - blocks on recv_msg(), validates each message and routes it to
            the room. A socket timeout of DEAD_TIMEOUT seconds doubles as the
            heartbeat check: clients PING every 3 s, so 10 s of silence
            means the client is gone.
* writer  - drains this client's outgoing queue and does the (possibly slow)
            sendall(). The room only ever *puts* onto this queue, so one
            slow or frozen client can never block the room lock or the others.
"""

import queue
import socket
import ssl
import threading
import time

from common import protocol as P
from . import media
from .room import NOOP, JoinError

_STOP = object()
RATE_PER_S = 300
RATE_BURST = 600


class ClientHandler:
    _ids = 0
    _ids_lock = threading.Lock()

    def __init__(self, sock, addr, rooms, log=print):
        with ClientHandler._ids_lock:
            ClientHandler._ids += 1
            self.id = ClientHandler._ids
        self.sock = sock
        self.addr = addr
        self.rooms = rooms
        self.log = log
        self.outq = queue.Queue()
        self.room = None
        self.part = None
        self.closed = threading.Event()
        self.clean_exit = False
        self.bad_frames = 0
        self.last_cursor = 0.0
        # token bucket: 300 messages/s sustained, bursts of 600 (a fast pen sends ~40/s)
        self.tokens = RATE_BURST
        self.tokens_at = time.monotonic()
        self.limited_at = 0.0
        P.tune_socket(sock)
        sock.settimeout(P.DEAD_TIMEOUT)

    def start(self):
        threading.Thread(target=self._reader, name=f"reader-{self.id}", daemon=True).start()
        threading.Thread(target=self._writer, name=f"writer-{self.id}", daemon=True).start()

    # ------------------------------------------------------------------ sending
    def send(self, msg):
        """Queue a message (never blocks). Drops the client if it falls too far behind."""
        if self.closed.is_set():
            return
        if self.outq.qsize() > P.MAX_SEND_QUEUE:
            self.log(f"client {self.addr} too slow ({self.outq.qsize()} queued) - dropping")
            self.close("send queue overflow")
            return
        try:
            frame = P.encode(msg)
        except P.ProtocolError:
            return
        self.rooms.stats.tx(len(frame))
        self.outq.put_nowait(frame)

    def _writer(self):
        try:
            while True:
                frame = self.outq.get()
                if frame is _STOP:
                    break
                self.sock.sendall(frame)
        except OSError:
            pass
        finally:
            self._shutdown_socket()

    def close(self, reason="", flush=False):
        """Close the connection. flush=True lets queued messages (e.g. KICKED) go out first."""
        if self.closed.is_set():
            return
        self.closed.set()
        if flush:
            self.outq.put(_STOP)                     # writer sends what is queued, then closes
        else:
            self._shutdown_socket()
            self.outq.put(_STOP)

    def _shutdown_socket(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def send_now(self, msg):
        """Send directly from the reader thread (media connections only, no queue)."""
        P.send_msg(self.sock, msg)

    def error(self, code, message, ref=None):
        msg = {"type": P.ERROR, "code": code, "message": message}
        if isinstance(ref, str) and len(ref) <= 64:        # never echo junk back
            msg["ref_op_id"] = ref
        self.send(msg)

    # ------------------------------------------------------------------ reading
    def _reader(self):
        try:
            if isinstance(self.sock, ssl.SSLSocket):
                self.sock.do_handshake()         # TLS handshake off the accept thread
            if not self._handshake():
                return
            while not self.closed.is_set():
                msg, nbytes = P.recv_frame(self.sock)
                self.rooms.stats.rx(nbytes)
                if not self._allow():
                    continue
                try:
                    self._dispatch(msg)
                except P.ProtocolError as exc:
                    self._bad(str(exc), msg.get("op_id"))
                except (TypeError, ValueError, KeyError, AttributeError) as exc:
                    # Safety net: a field of an unexpected type must never kill this
                    # client's thread (or touch anyone else) - treat it as a bad message.
                    self._bad(f"malformed {msg.get('type')!r} message ({type(exc).__name__})",
                              msg.get("op_id"))
        except (socket.timeout, TimeoutError):
            self.log(f"client {self.addr} timed out (no heartbeat)")
        except P.ProtocolError as exc:
            self.log(f"client {self.addr} protocol error: {exc}")
        except ssl.SSLError as exc:
            self.log(f"client {self.addr} TLS error: {exc}")
        except (ConnectionError, OSError):
            pass
        finally:
            if self.room is not None and not self.clean_exit:
                self.room.disconnected(self)
            self.close()

    def _allow(self):
        """Token bucket. A client that floods the server is slowed down by pausing ITS
        reader thread: TCP flow control then pushes back on that client only, and no
        message is ever dropped (dropping a board op would silently lose work)."""
        now = time.monotonic()
        self.tokens = min(RATE_BURST, self.tokens + (now - self.tokens_at) * RATE_PER_S)
        self.tokens_at = now
        if self.tokens < 1:
            wait = (1 - self.tokens) / RATE_PER_S
            if now - self.limited_at > 5.0:
                self.limited_at = now
                self.log(f"client {self.addr} is sending too fast - throttling")
            time.sleep(wait)
            self.tokens = 1.0
            self.tokens_at = time.monotonic()
        self.tokens -= 1
        return True

    def _bad(self, text, ref=None):
        self.bad_frames += 1
        self.error(P.E_BAD_MESSAGE, text, ref)
        if self.bad_frames >= 3:
            self.log(f"client {self.addr} sent 3 bad messages - disconnecting")
            self.close("too many bad messages", flush=True)

    def _handshake(self):
        """The first message must be HELLO; reply WELCOME or ERROR."""
        msg = P.recv_msg(self.sock)
        if msg.get("type") in (P.MEDIA_PUT, P.MEDIA_GET):
            # A separate short-lived connection for one file transfer.
            self.sock.settimeout(30)
            store = self.rooms.media_store
            if msg["type"] == P.MEDIA_PUT:
                media.handle_put(self, msg, store, self.log)
            else:
                media.handle_get(self, msg, store, self.log)
            self.clean_exit = True
            self.close(flush=True)
            return False
        if msg.get("type") != P.HELLO:
            self.error(P.E_BAD_MESSAGE, "expected HELLO")
            self.close(flush=True)
            return False
        if msg.get("version") != P.PROTOCOL_VERSION:
            self.error(P.E_VERSION, f"Client and server versions differ (client protocol "
                                    f"{msg.get('version')}, server {P.PROTOCOL_VERSION}) - "
                                    "use the same release everywhere")
            self.close(flush=True)
            return False
        username = msg.get("username")
        if not P.valid_username(username):
            self.error(P.E_BAD_MESSAGE, "Username must be 1-20 letters, digits, _ or -")
            self.close(flush=True)
            return False

        ip = self.addr[0]
        password = msg.get("password") or ""
        if not isinstance(password, str) or len(password) > 64:
            self.error(P.E_BAD_MESSAGE, "bad password field")
            self.close(flush=True)
            return False

        try:
            if msg.get("create"):
                room = self.rooms.create(password, username)
                part, _ = room.join(self, msg, is_creator=True)
            else:
                room = self.rooms.get(msg.get("room"))
                if room is None:
                    raise JoinError(P.E_ROOM_NOT_FOUND, "No room with that code on this server")
                if not msg.get("session_token") and self.rooms.blocked(ip):
                    raise JoinError(P.E_RATE_LIMIT, "Too many wrong passwords - wait 30 seconds")
                try:
                    part, _ = room.join(self, msg)
                except JoinError as exc:
                    if exc.code == P.E_BAD_PASSWORD:
                        self.rooms.record_failure(ip)
                    raise
        except JoinError as exc:
            self.error(exc.code, exc.message)
            self.close(flush=True)
            return False
        self.room, self.part = room, part
        return True

    def _dispatch(self, msg):
        t = msg["type"]
        room, part = self.room, self.part
        if t == P.PING:
            self.send({"type": P.PONG, "t": msg.get("t")})
        elif t in P.BOARD_KINDS:
            err = room.board_op(part, msg)
            if err == NOOP:
                # Tell the sender it can forget this op (nothing to broadcast).
                op_id = msg.get("op_id")
                if isinstance(op_id, str):
                    self.send({"type": P.ACK, "op_id": op_id})
            elif err:
                self.error(err[0], err[1], msg.get("op_id"))
        elif t in (P.CURSOR, P.STROKE_PART, P.LASER):
            if t == P.CURSOR:                        # server-side throttle: max ~30/s
                now = time.monotonic()
                if now - self.last_cursor < 0.03:
                    return
                self.last_cursor = now
            room.relay(part, self._ephemeral(msg))
        elif t == P.CHAT:
            err = room.chat_msg(part, msg.get("text"))
            if err:
                self.error(*err)
        elif t == P.ADMIN:
            err = room.admin(part, msg)
            if err:
                self.error(*err)
        elif t in (P.VOTE, P.VOTES_CLEAR, P.TIMER, P.FOLLOW, P.VIEWPORT, P.DM, P.HAND, P.POLL,
                   P.SETTINGS):
            fn = {P.VOTE: room.vote, P.VOTES_CLEAR: room.votes_clear, P.TIMER: room.timer_op,
                  P.FOLLOW: room.follow_op, P.VIEWPORT: room.viewport_op, P.DM: room.dm,
                  P.HAND: room.hand, P.POLL: room.poll_op, P.SETTINGS: room.settings_op}[t]
            err = fn(part, msg)
            if err:
                self.error(*err)
        elif t == P.RESTORE:
            res = room.restore(part, msg)
            if isinstance(res, tuple):
                self.error(*res)
            else:
                note = f" ({res} invalid object(s) skipped)" if res else ""
                self.send({"type": P.INFO, "message": f"Board restored from file{note}"})
        elif t == P.HISTORY:
            self.send(room.history_reply())
        elif t == P.CLOSE_ROOM:
            if part.role != P.ROLE_HOST:
                self.error(P.E_FORBIDDEN, "Only the host can end the session")
            else:
                self.rooms.close(room, part.username)
        elif t == P.SAVE:
            self._save(msg)
        elif t == P.LOAD:
            self._load(msg)
        elif t == P.LIST_SAVES:
            store = self.rooms.store
            self.send({"type": P.SAVES, "saves": store.list(room.code) if store else []})
        elif t == P.BYE:
            self.clean_exit = True
            room.leave(self)
            self.close()
        else:
            raise P.ProtocolError(f"unknown message type {t!r}")

    @staticmethod
    def _ephemeral(msg):
        """Keep only the expected fields of cursor/preview traffic."""
        num = P.world_coord                      # infinite canvas: world coordinates
        if msg["type"] == P.CURSOR:
            return {"type": P.CURSOR, "x": num(msg.get("x")), "y": num(msg.get("y"))}
        if msg["type"] == P.LASER:
            pts = msg.get("points")
            if (not isinstance(pts, list) or len(pts) % 2
                    or len(pts) > 2 * P.MAX_LASER_POINTS):
                raise P.ProtocolError("bad laser points")
            return {"type": P.LASER, "points": [num(v) for v in pts],
                    "end": bool(msg.get("end"))}
        pts = msg.get("points")
        if not isinstance(pts, list) or len(pts) % 2 or len(pts) > 2000:
            raise P.ProtocolError("bad preview points")
        out = {"type": P.STROKE_PART, "stroke_id": str(msg.get("stroke_id"))[:64],
               "points": [num(v) for v in pts]}
        color, width = msg.get("color"), msg.get("width")
        if P.valid_color(color):
            out["stroke_color"] = color
        if isinstance(width, (int, float)) and 1 <= width <= 60:
            out["width"] = width
        out["tool"] = msg.get("tool") if msg.get("tool") in P.TOOLS else "pen"
        return out

    def _save(self, msg):
        if self.part.role != P.ROLE_HOST:
            return self.error(P.E_FORBIDDEN, "Only the host can save the board")
        name = msg.get("name")
        if not P.valid_save_name(name):
            return self.error(P.E_BAD_MESSAGE, "Save name: 1-40 letters, digits, _ or -")
        path = self.rooms.store.save(self.room, name)
        self.send({"type": P.INFO, "message": f"Board saved as '{name}'"})
        self.log(f"[{self.room.code}] saved to {path}")

    def _import_media(self, other_room):
        """A save from another room may show images stored there: copy them over."""
        store = self.rooms.media_store
        if store is None or other_room == self.room.code:
            return
        import shutil
        for mid, meta in store.load_index(other_room).items():
            if mid not in self.room.media:
                dst = store.path(self.room.code, mid)
                shutil.copyfile(store.path(other_room, mid), dst)
                shutil.copyfile(store.path(other_room, mid) + ".json", dst + ".json")
                self.room.add_media(meta)

    def _load(self, msg):
        if self.part.role != P.ROLE_HOST:
            return self.error(P.E_FORBIDDEN, "Only the host can load a board")
        name = msg.get("name")
        if not P.valid_save_name(name):
            return self.error(P.E_BAD_MESSAGE, "bad save name")
        data = self.rooms.store.load(self.room.code, name)
        if data is None:
            return self.error(P.E_NOT_FOUND, f"No save called '{name}'")
        self._import_media(data.get("room", self.room.code))
        self.room.load_state(data, self.part.username)
        self.log(f"[{self.room.code}] {self.part.username} loaded '{name}'")
