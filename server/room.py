"""
Rooms: the authoritative shared state of one whiteboard session.

Concurrency model
-----------------
Every room has ONE lock. Any state change (a stroke, an undo, a join, a
chat message) happens entirely inside that lock:

    1. validate the request,
    2. assign the next sequence number,
    3. apply it to the BoardModel,
    4. append it to the replay log,
    5. put the broadcast on every client's send queue.

Because steps 2-5 are atomic, concurrent requests from different client
threads are serialised into one total order, and every client's queue
receives the broadcasts in that same order. Putting a message on a queue
never blocks (the per-client writer thread does the slow socket send), so a
slow client can never stall the lock or the other participants.
"""

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque

from common import protocol as P
from common.model import BoardModel

ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
NOOP = "noop"                    # board_op result: valid request, nothing changed
REPLAY_LOG_SIZE = 20000          # ops kept for incremental catch-up on reconnect
SEEN_OP_IDS = 50000              # op_ids remembered for duplicate detection
HISTORY_MAX = 6000               # ops kept for version history (older ones fold into the base)
PBKDF2_ITERATIONS = 120_000


def new_room_code():
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(4))


def hash_password(password, salt):
    """PBKDF2-HMAC-SHA256 with a random per-room salt: a leaked save file or
    memory dump does not reveal the password, and guessing is slow."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               PBKDF2_ITERATIONS).hex()


class Participant:
    """One user's seat in a room (survives short disconnects)."""

    def __init__(self, username, color, role, handler):
        self.username = username
        self.color = color
        self.role = role
        self.handler = handler           # None while away
        self.token = secrets.token_hex(16)
        self.joined_at = time.time()
        self.away_since = None
        self.op_counter = 0
        self.hand = False                # raised hand (classroom)

    @property
    def connected(self):
        return self.handler is not None

    def public(self):
        return {"username": self.username, "color": self.color, "role": self.role,
                "status": "online" if self.connected else "away", "hand": self.hand}


class Room:
    def __init__(self, code, password="", creator=None, log=print):
        self.code = code
        self.lock = threading.RLock()
        self.model = BoardModel()
        self.seq = 0
        self.replay = deque(maxlen=REPLAY_LOG_SIZE)    # OP messages, for reconnect catch-up
        self.seen_ops = set()
        self.seen_order = deque()
        self.undo = {}                                  # username -> [op_id]
        self.redo = {}
        self.chat = deque(maxlen=P.CHAT_HISTORY)
        self.chat_id = 0
        self.participants = {}                          # username -> Participant
        self.banned = set()
        self.locked = False
        self.salt = os.urandom(16)
        self.pw_hash = hash_password(password, self.salt) if password else None
        self.created = time.time()
        self.creator = creator
        self.dirty = False                              # unsaved changes (autosave)
        self.media = {}                                 # media_id -> metadata (shared files)
        self.votes = {}                                 # stroke_id -> [usernames], vote order
        self.timer = {"running": False, "duration": 0, "remaining": 0.0, "end": None,
                      "by": None}
        self.follow = None                              # presenter's username (follow-me mode)
        self.viewport = None                            # presenter's last view [x, y, w, h]
        self.settings = dict(P.SETTINGS_DEFAULT)        # own_only, paper, pages
        self.dms = deque(maxlen=P.MAX_DM_HISTORY)       # private messages
        self.dm_id = 0
        self.poll = None                                # current quick poll
        self.poll_id = 0
        self.hist_base = BoardModel()                   # board at the start of `history`
        self.history = []                               # every op since hist_base
        self.closed = False
        self.log = log

    # ================================================================ helpers
    def check_password(self, password):
        if self.pw_hash is None:
            return True
        return hmac.compare_digest(self.pw_hash, hash_password(password or "", self.salt))

    def member(self, username, token):
        """The participant owning this session token (online or within grace), else None."""
        if not isinstance(username, str) or not isinstance(token, str):
            return None
        with self.lock:
            p = self.participants.get(username)
            if p is not None and hmac.compare_digest(p.token, token):
                return p
        return None

    def add_media(self, meta):
        with self.lock:
            self.media[meta["media_id"]] = meta

    def participant_list(self):
        return [p.public() for p in sorted(self.participants.values(),
                                           key=lambda p: p.joined_at)]

    def broadcast(self, msg, exclude=None):
        """Queue msg for every connected participant (call with the lock held)."""
        for p in self.participants.values():
            if p.handler is not None and p.handler is not exclude:
                p.handler.send(msg)

    def _next_color(self):
        used = {p.color for p in self.participants.values()}
        for c in P.USER_COLORS:
            if c not in used:
                return c
        return P.USER_COLORS[len(self.participants) % len(P.USER_COLORS)]

    def _remember_op(self, op_id):
        self.seen_ops.add(op_id)
        self.seen_order.append(op_id)
        if len(self.seen_order) > SEEN_OP_IDS:
            self.seen_ops.discard(self.seen_order.popleft())

    def _host(self):
        for p in self.participants.values():
            if p.role == P.ROLE_HOST:
                return p
        return None

    def _ensure_host(self):
        """If nobody is host, promote the longest-connected online user."""
        if self._host() is not None:
            return
        online = sorted((p for p in self.participants.values() if p.connected),
                        key=lambda p: p.joined_at)
        if online:
            online[0].role = P.ROLE_HOST
            self.broadcast({"type": P.PRESENCE, "event": "role_changed",
                            "user": online[0].username, "role": P.ROLE_HOST,
                            "participants": self.participant_list()})
            self.log(f"[{self.code}] {online[0].username} is now host")

    # ================================================================ sessions
    def join(self, handler, hello, is_creator=False):
        """Handle HELLO for this room: queue WELCOME and return (participant, welcome).

        Raises JoinError if the join is refused.
        """
        with self.lock:
            username = hello.get("username")
            token = hello.get("session_token")

            # ---- reconnect into an existing seat ---------------------------
            existing = self.participants.get(username)
            if existing and token and hmac.compare_digest(existing.token, token):
                if existing.handler is not None and existing.handler is not handler:
                    existing.handler.close("replaced by a new connection")
                existing.handler = handler
                existing.away_since = None
                welcome = self._welcome(existing, hello.get("last_seq"))
                handler.send(welcome)      # queued under the lock: arrives before any later OP
                self.broadcast({"type": P.PRESENCE, "event": "reconnected",
                                "user": username, "color": existing.color,
                                "participants": self.participant_list()}, exclude=handler)
                self.log(f"[{self.code}] {username} reconnected")
                return existing, welcome

            # ---- fresh join -------------------------------------------------
            if not is_creator and not self.check_password(hello.get("password")):
                raise JoinError(P.E_BAD_PASSWORD, "Wrong room password")
            if username in self.banned:
                raise JoinError(P.E_BANNED, "You were removed from this room by the host")
            if existing is not None:
                raise JoinError(P.E_NAME_TAKEN, f"The name '{username}' is already in use")
            if len(self.participants) >= P.MAX_USERS_PER_ROOM:
                raise JoinError(P.E_ROOM_FULL, "Room is full")

            role = P.ROLE_HOST if (is_creator or self._host() is None) else P.ROLE_EDITOR
            part = Participant(username, self._next_color(), role, handler)
            self.participants[username] = part
            self.undo.setdefault(username, [])
            self.redo.setdefault(username, [])
            welcome = self._welcome(part, None)
            handler.send(welcome)          # queued under the lock: arrives before any later OP
            self.broadcast({"type": P.PRESENCE, "event": "joined", "user": username,
                            "color": part.color, "participants": self.participant_list()},
                           exclude=handler)
            self.log(f"[{self.code}] {username} joined as {role}")
            return part, welcome

    def _welcome(self, part, last_seq):
        """Build WELCOME. On reconnect, send only the missed ops when we still have them."""
        msg = {"type": P.WELCOME, "room": self.code, "username": part.username,
               "color": part.color, "role": part.role, "session_token": part.token,
               "participants": self.participant_list(), "locked": self.locked,
               "chat_history": list(self.chat), "seq": self.seq,
               "has_password": self.pw_hash is not None,
               "canvas": [P.CANVAS_W, P.CANVAS_H],
               "votes": self.votes, "timer": self._timer_state(),
               "follow": self.follow, "viewport": self.viewport,
               "settings": self.settings,
               "poll": self._poll_public(part.role == P.ROLE_HOST),
               "my_answer": (self.poll or {}).get("answers", {}).get(part.username),
               "dms": [m for m in self.dms if part.username in (m["from"], m["to"])],
               "protocol": P.PROTOCOL_VERSION}
        if (isinstance(last_seq, int) and 0 <= last_seq <= self.seq and
                (last_seq == self.seq or
                 (self.replay and self.replay[0]["seq"] <= last_seq + 1))):
            msg["missed_ops"] = [m for m in self.replay if m["seq"] > last_seq]
        else:
            # Snapshot and seq are taken under the same lock, so no op can
            # slip in between the snapshot and the live stream.
            msg["snapshot"] = self.model.to_dict()
        return msg

    def disconnected(self, handler):
        """Socket died without BYE: keep the seat for GRACE_PERIOD seconds."""
        with self.lock:
            for p in self.participants.values():
                if p.handler is handler:
                    p.handler = None
                    p.away_since = time.time()
                    self.broadcast({"type": P.PRESENCE, "event": "away", "user": p.username,
                                    "participants": self.participant_list()})
                    self.log(f"[{self.code}] {p.username} connection lost (grace period)")
                    return

    def leave(self, handler):
        """Clean leave (BYE): remove immediately."""
        with self.lock:
            for name, p in list(self.participants.items()):
                if p.handler is handler:
                    self._remove(name, "left")
                    return

    def _remove(self, name, event):
        p = self.participants.pop(name, None)
        if p is None:
            return
        self.broadcast({"type": P.PRESENCE, "event": event, "user": name,
                        "participants": self.participant_list()})
        self.log(f"[{self.code}] {name} {event}")
        if self.follow == name:                 # presenter left: end presentation mode
            self._set_follow(None, name)
        if p.role == P.ROLE_HOST:
            self._ensure_host()

    def reap(self, now):
        """Remove participants whose grace period has expired."""
        with self.lock:
            for name, p in list(self.participants.items()):
                if p.away_since is not None and now - p.away_since > P.GRACE_PERIOD:
                    self._remove(name, "left")
            # If the host is away, keep their role during the grace period only.

    # ================================================================ board ops
    def _can_edit(self, part):
        if part.role == P.ROLE_VIEWER:
            return P.E_FORBIDDEN, "View-only: you cannot change the board"
        if self.locked and part.role != P.ROLE_HOST:
            return P.E_LOCKED, "The teacher has made the board read-only"
        return None

    def _owned(self, part, ids):
        """In own-work-only mode, editors may only change objects they created."""
        if not self.settings["own_only"] or part.role == P.ROLE_HOST:
            return ids
        return [i for i in ids if self.model.strokes[i].get("user") == part.username]

    def board_op(self, part, msg):
        """Validate and apply STROKE / MOVE / EDIT / ERASE / CLEAR / UNDO / REDO.

        Returns None on success, NOOP when the request was valid but changed
        nothing (duplicate op_id, erasing strokes someone already erased,
        clearing an empty board), or (error_code, text) on rejection.
        """
        kind = msg["type"]
        op_id = msg.get("op_id")
        if not isinstance(op_id, str) or not 1 <= len(op_id) <= 64:
            return P.E_BAD_MESSAGE, "missing op_id"

        with self.lock:
            # Duplicate (e.g. a client re-sent after reconnecting): drop it
            # silently. The original was already applied and broadcast.
            if op_id in self.seen_ops:
                return NOOP
            err = self._can_edit(part)
            if err:
                return err

            entry = {"kind": kind, "op_id": op_id, "user": part.username}
            if kind == P.STROKE:
                entry["stroke"] = P.sanitize_stroke(msg.get("stroke"))
                if entry["stroke"]["id"] in self.model.strokes:
                    return P.E_BAD_MESSAGE, "duplicate stroke id"
                if (entry["stroke"]["tool"] == "image" and
                        entry["stroke"]["media_id"] not in self.media):
                    return P.E_NOT_FOUND, "Image not uploaded to this room"
                if entry["stroke"]["tool"] == "connector":
                    for end in ("src", "dst"):
                        nid = entry["stroke"][end]
                        if not (self.model.is_visible(nid) and
                                self.model.strokes[nid]["tool"] == "node"):
                            return P.E_NOT_FOUND, "Connectors must join two nodes"
            elif kind == P.ERASE:
                ids = msg.get("stroke_ids")
                if not isinstance(ids, list) or not ids or len(ids) > 2000:
                    return P.E_BAD_MESSAGE, "bad stroke_ids"
                ids = [i for i in ids if isinstance(i, str) and self.model.is_visible(i)]
                if not ids:
                    self._remember_op(op_id)
                    return NOOP            # already gone (someone erased first)
                ids = self._owned(part, ids)
                if not ids:
                    return P.E_FORBIDDEN, "Own-work-only mode: you can only erase your own work"
                entry["stroke_ids"] = ids
            elif kind == P.MOVE:
                ids = msg.get("stroke_ids")
                if not isinstance(ids, list) or not ids or len(ids) > 2000:
                    return P.E_BAD_MESSAGE, "bad stroke_ids"
                ids = [i for i in ids if isinstance(i, str) and self.model.is_visible(i)]
                dx, dy = P.world_coord(msg.get("dx")), P.world_coord(msg.get("dy"))
                if not ids or (dx == 0 and dy == 0):
                    self._remember_op(op_id)
                    return NOOP            # object gone, or a click without a drag
                ids = self._owned(part, ids)
                if not ids:
                    return P.E_FORBIDDEN, "Own-work-only mode: you can only move your own work"
                entry.update(stroke_ids=ids, dx=dx, dy=dy)
            elif kind == P.EDIT:
                sid = msg.get("stroke_id")
                if not isinstance(sid, str) or not self.model.is_visible(sid):
                    return P.E_NOT_FOUND, "That object is no longer on the board"
                if not self._owned(part, [sid]):
                    return P.E_FORBIDDEN, "Own-work-only mode: you can only change your own work"
                cur = self.model.current(sid)
                clean = P.sanitize_edit(cur, msg.get("set"))
                if all(cur.get(k) == v for k, v in clean.items()):
                    self._remember_op(op_id)
                    return NOOP            # nothing actually changes
                entry.update(stroke_id=sid, set=clean)
            elif kind == P.CLEAR:
                if part.role != P.ROLE_HOST:
                    return P.E_FORBIDDEN, "Only the host can clear the board"
                if not self.model.visible_ids():
                    return NOOP
            elif kind == P.UNDO:
                target = self._pop_active(self.undo[part.username])
                if target is None:
                    return P.E_NOT_FOUND, "Nothing to undo"
                entry["target"] = target
                self.redo[part.username].append(target)
            elif kind == P.REDO:
                stack = self.redo[part.username]
                if not stack:
                    return P.E_NOT_FOUND, "Nothing to redo"
                entry["target"] = stack.pop()
                self.undo[part.username].append(entry["target"])

            if kind in (P.STROKE, P.MOVE, P.EDIT, P.ERASE, P.CLEAR):
                self.undo[part.username].append(op_id)
                self.redo[part.username].clear()       # a new action ends the redo chain

            self._commit(entry)
            return None

    def _pop_active(self, stack):
        while stack:
            op = stack.pop()
            if self.model.active.get(op):
                return op
        return None

    def _commit(self, entry):
        """Assign seq, apply, log and broadcast (lock must be held)."""
        self.seq += 1
        entry["seq"] = self.seq
        entry["ts"] = round(time.time(), 3)
        self.model.apply(entry)
        self._remember_op(entry["op_id"])
        msg = {"type": P.OP, "seq": self.seq, "op": entry}
        self.replay.append(msg)
        self.history.append(entry)
        if len(self.history) > HISTORY_MAX:            # fold the oldest ops into the base
            for old in self.history[:1000]:
                self.hist_base.apply(old)
            del self.history[:1000]
        self.dirty = True
        self.broadcast(msg)                    # includes the sender: that echo is its ack

    # ================================================================ chat / live
    def chat_msg(self, part, text, attachment=None):
        """Broadcast a chat message; `attachment` is media metadata for shared files."""
        if not isinstance(text, str):
            text = ""
        if not text.strip() and attachment is None:
            return P.E_BAD_MESSAGE, "empty message"
        with self.lock:
            self.chat_id += 1
            msg = {"type": P.CHAT, "id": self.chat_id, "user": part.username,
                   "color": part.color, "text": text.strip()[:P.MAX_CHAT_LEN],
                   "ts": round(time.time(), 3)}
            if attachment is not None:
                msg["attachment"] = {k: attachment[k] for k in
                                     ("media_id", "name", "mime", "size", "kind", "sha256",
                                      "duration", "dims") if k in attachment}
            self.chat.append(msg)
            self.dirty = True
            self.broadcast(msg)
        return None

    def relay(self, part, msg):
        """Relay ephemeral traffic (cursor, live preview, laser) to everyone else."""
        with self.lock:
            if msg["type"] in (P.STROKE_PART, P.LASER) and self._can_edit(part):
                return
            out = dict(msg)
            out["user"] = part.username
            out["color"] = part.color
            if msg["type"] == P.STROKE_PART:
                out["type"] = P.PREVIEW
            self.broadcast(out, exclude=part.handler)

    # ================================================================ voting
    def vote(self, part, msg):
        """Toggle part's vote on one object (dot voting, MAX_VOTES_PER_USER each).

        Voting is open to everyone, viewers included, and works while the board
        is locked: it is feedback, not editing.
        """
        sid = msg.get("stroke_id")
        with self.lock:
            if not isinstance(sid, str) or not self.model.is_visible(sid):
                return P.E_NOT_FOUND, "That object is no longer on the board"
            voters = self.votes.setdefault(sid, [])
            if part.username in voters:
                voters.remove(part.username)
            else:
                used = sum(part.username in v for s, v in self.votes.items()
                           if self.model.is_visible(s))
                if used >= P.MAX_VOTES_PER_USER:
                    return (P.E_FORBIDDEN, f"You have used all {P.MAX_VOTES_PER_USER} votes - "
                                           "remove one first")
                voters.append(part.username)
            if not voters:
                del self.votes[sid]
            self.dirty = True
            self.broadcast({"type": P.VOTES, "stroke_id": sid, "voters": list(voters)})
        return None

    def votes_clear(self, part, _msg):
        with self.lock:
            if part.role != P.ROLE_HOST:
                return P.E_FORBIDDEN, "Only the host can reset the votes"
            self.votes = {}
            self.dirty = True
            self.broadcast({"type": P.VOTES, "all": {}, "by": part.username})
        return None

    # ================================================================ session timer
    def _timer_state(self):
        t = self.timer
        remaining = t["remaining"]
        if t["running"]:
            remaining = max(0.0, t["end"] - time.monotonic())
        return {"running": t["running"] and remaining > 0, "duration": t["duration"],
                "remaining": round(remaining, 2), "by": t["by"]}

    def timer_op(self, part, msg):
        """Host controls a shared countdown. Clients receive the seconds remaining
        (not a wall-clock time), so machines with unsynchronised clocks agree."""
        action = msg.get("action")
        with self.lock:
            if part.role != P.ROLE_HOST:
                return P.E_FORBIDDEN, "Only the host can control the timer"
            t = self.timer
            now = time.monotonic()
            if action == "start":
                secs = msg.get("seconds")
                if (not isinstance(secs, (int, float)) or isinstance(secs, bool)
                        or not 1 <= secs <= P.MAX_TIMER_SECONDS):
                    return P.E_BAD_MESSAGE, "Timer length must be 1 s to 3 h"
                t.update(running=True, duration=int(secs), end=now + secs, remaining=float(secs))
            elif action == "pause" and t["running"]:
                t.update(running=False, remaining=max(0.0, t["end"] - now))
            elif action == "resume" and not t["running"] and t["remaining"] > 0:
                t.update(running=True, end=now + t["remaining"])
            elif action == "reset":
                t.update(running=False, remaining=float(t["duration"]), end=None)
            elif action not in ("pause", "resume"):
                return P.E_BAD_MESSAGE, "unknown timer action"
            t["by"] = part.username
            self.broadcast({"type": P.TIMER_STATE, **self._timer_state(), "action": action})
        return None

    # ================================================================ presentation mode
    def _set_follow(self, name, by):
        self.follow = name
        if name is None:
            self.viewport = None
        self.broadcast({"type": P.ROOM_STATE, "locked": self.locked, "follow": self.follow,
                        "by": by})

    def follow_op(self, part, msg):
        """Host turns "follow me" on/off: every other client's view is locked to theirs."""
        with self.lock:
            if part.role != P.ROLE_HOST:
                return P.E_FORBIDDEN, "Only the host can present"
            on = bool(msg.get("on"))
            self._set_follow(part.username if on else None, part.username)
            self.log(f"[{self.code}] presentation mode {'on' if on else 'off'}")
        return None

    def viewport_op(self, part, msg):
        """Presenter's view (world rectangle) -> relayed to all followers."""
        with self.lock:
            if self.follow != part.username:
                return None                     # not presenting: silently ignore
            vx, vy = P.world_coord(msg.get("x")), P.world_coord(msg.get("y"))
            vw, vh = P.world_coord(msg.get("w")), P.world_coord(msg.get("h"))
            if vw <= 0 or vh <= 0:
                raise P.ProtocolError("bad viewport")
            self.viewport = [vx, vy, vw, vh]
            self.broadcast({"type": P.VIEWPORT, "x": vx, "y": vy, "w": vw, "h": vh,
                            "user": part.username}, exclude=part.handler)
        return None

    # ================================================================ private messages
    def dm(self, part, msg):
        """Private message: delivered only to the sender and the recipient."""
        to, text = msg.get("to"), msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return P.E_BAD_MESSAGE, "empty message"
        with self.lock:
            target = self.participants.get(to) if isinstance(to, str) else None
            if target is None:
                return P.E_NOT_FOUND, "That participant is not in the room"
            if target is part:
                return P.E_BAD_MESSAGE, "You cannot message yourself"
            self.dm_id += 1
            m = {"type": P.DM, "id": self.dm_id, "from": part.username, "to": to,
                 "color": part.color, "text": text.strip()[:P.MAX_CHAT_LEN],
                 "ts": round(time.time(), 3)}
            self.dms.append(m)
            for p in (part, target):
                if p.handler is not None:
                    p.handler.send(m)
        return None

    # ================================================================ classroom
    def hand(self, part, msg):
        """Raise / lower my hand; the host may lower anyone's hand."""
        with self.lock:
            name = msg.get("target") or part.username
            up = bool(msg.get("up"))
            target = self.participants.get(name) if isinstance(name, str) else None
            if target is None:
                return P.E_NOT_FOUND, "No such participant"
            if target is not part and (part.role != P.ROLE_HOST or up):
                return P.E_FORBIDDEN, "Only the host can lower someone else's hand"
            if target.hand == up:
                return None
            target.hand = up
            self.broadcast({"type": P.PRESENCE, "event": "hand", "user": name, "up": up,
                            "by": part.username, "participants": self.participant_list()})
        return None

    def _poll_public(self, host=False):
        p = self.poll
        if p is None:
            return None
        counts = [0] * len(p["options"])
        for idx in p["answers"].values():
            counts[idx] += 1
        out = {"id": p["id"], "question": p["question"], "options": p["options"],
               "counts": counts, "total": len(p["answers"]), "open": p["open"], "by": p["by"]}
        if host:                                # only the teacher sees who chose what
            out["answers"] = dict(p["answers"])
        return out

    def _broadcast_poll(self, action, by):
        for p in self.participants.values():
            if p.handler is not None:
                p.handler.send({"type": P.POLL_STATE, "action": action, "by": by,
                                "poll": self._poll_public(p.role == P.ROLE_HOST)})

    def poll_op(self, part, msg):
        """Quick poll. Host: start / end / clear. Everyone: answer (changeable while open)."""
        action = msg.get("action")
        with self.lock:
            if action == "answer":
                if self.poll is None or not self.poll["open"]:
                    return P.E_NOT_FOUND, "There is no open poll"
                idx = msg.get("option")
                if not isinstance(idx, int) or isinstance(idx, bool) or \
                        not 0 <= idx < len(self.poll["options"]):
                    return P.E_BAD_MESSAGE, "bad option"
                self.poll["answers"][part.username] = idx
            elif part.role != P.ROLE_HOST:
                return P.E_FORBIDDEN, "Only the host can run polls"
            elif action == "start":
                q, opts = msg.get("question"), msg.get("options")
                if not isinstance(q, str) or not 1 <= len(q.strip()) <= 200:
                    return P.E_BAD_MESSAGE, "Poll question: 1-200 characters"
                if (not isinstance(opts, list) or not 2 <= len(opts) <= P.MAX_POLL_OPTIONS or
                        not all(isinstance(o, str) and 1 <= len(o.strip()) <= 80 for o in opts)):
                    return P.E_BAD_MESSAGE, f"A poll needs 2-{P.MAX_POLL_OPTIONS} options"
                self.poll_id += 1
                self.poll = {"id": self.poll_id, "question": q.strip(),
                             "options": [o.strip() for o in opts], "answers": {}, "open": True,
                             "by": part.username}
            elif action == "end":
                if self.poll is None:
                    return P.E_NOT_FOUND, "There is no poll"
                self.poll["open"] = False
            elif action == "clear":
                self.poll = None
            else:
                return P.E_BAD_MESSAGE, "unknown poll action"
            self._broadcast_poll(action, part.username)
        return None

    def settings_op(self, part, msg):
        """Host changes one room setting; everyone receives the full settings."""
        key, value = msg.get("key"), msg.get("value")
        with self.lock:
            if part.role != P.ROLE_HOST:
                return P.E_FORBIDDEN, "Only the host can change room settings"
            if key == "own_only" and isinstance(value, bool):
                pass
            elif key == "paper" and value in P.PAPERS:
                pass
            elif (key == "pages" and isinstance(value, int) and not isinstance(value, bool)
                  and 1 <= value <= P.MAX_PAGES):
                pass
            else:
                return P.E_BAD_MESSAGE, "bad setting"
            self.settings[key] = value
            self.dirty = True
            self.broadcast({"type": P.SETTINGS, "settings": dict(self.settings),
                            "key": key, "by": part.username})
        return None

    def restore(self, part, msg):
        """Host replaces the board with objects from a board file on their computer.

        Every object is validated exactly like a new STROKE.
        """
        strokes = msg.get("strokes")
        with self.lock:
            if part.role != P.ROLE_HOST:
                return P.E_FORBIDDEN, "Only the host can restore a board"
            if not isinstance(strokes, list) or len(strokes) > 20000:
                return P.E_BAD_MESSAGE, "bad board file"
            board = BoardModel()
            skipped = 0
            for i, st in enumerate(strokes):
                try:
                    clean = P.sanitize_stroke(st)
                except P.ProtocolError:
                    skipped += 1
                    continue
                if clean["id"] in board.strokes or (
                        clean["tool"] == "image" and clean["media_id"] not in self.media):
                    skipped += 1
                    continue
                if clean["tool"] == "connector" and not (
                        clean["src"] in board.strokes and clean["dst"] in board.strokes):
                    skipped += 1
                    continue
                user = st.get("user") if P.valid_username(st.get("user")) else part.username
                board.apply({"kind": P.STROKE, "op_id": f"restore-{self.seq}-{i}", "user": user,
                             "seq": 0, "stroke": clean})
            self.load_state({"board": board.to_dict()}, part.username)
            return skipped

    def history_reply(self):
        """Base board + every op since: enough for a client to rebuild any version."""
        import json
        with self.lock:
            ops = list(self.history)
            base = self.hist_base
            # Keep the reply within the frame limit: fold the oldest ops into a copy of
            # the base until it fits (only for this reply).
            sizes = [len(json.dumps(o, separators=(",", ":"))) for o in ops]
            budget = int(P.MAX_INFLATED * 0.6) - len(json.dumps(base.to_dict()))
            cut = 0
            total = sum(sizes)
            while total > budget and cut < len(ops):
                total -= sizes[cut]
                cut += 1
            if cut:
                base = BoardModel.from_dict(base.to_dict())
                for o in ops[:cut]:
                    base.apply(o)
            return {"type": P.HISTORY, "base": base.to_dict(), "ops": ops[cut:],
                    "seq": self.seq, "trimmed": cut}

    # ================================================================ admin
    def admin(self, part, msg):
        action = msg.get("action")
        target_name = msg.get("target")
        if not isinstance(target_name, (str, type(None))):
            return P.E_BAD_MESSAGE, "bad target"
        with self.lock:
            if part.role != P.ROLE_HOST:
                return P.E_FORBIDDEN, "Only the host can do that"
            if action in ("lock", "unlock"):
                self.locked = action == "lock"
                self.broadcast({"type": P.ROOM_STATE, "locked": self.locked,
                                "follow": self.follow, "by": part.username})
                self.log(f"[{self.code}] board {'locked' if self.locked else 'unlocked'}")
                return None
            if action == "lower_all_hands":
                for p in self.participants.values():
                    p.hand = False
                self.broadcast({"type": P.PRESENCE, "event": "hands_cleared",
                                "user": part.username, "participants": self.participant_list()})
                return None
            target = self.participants.get(target_name)
            if target is None:
                return P.E_NOT_FOUND, "No such participant"
            if target is part:
                return P.E_BAD_MESSAGE, "You cannot do that to yourself"
            if action == "kick":
                self.banned.add(target_name)
                handler = target.handler
                self._remove(target_name, "kicked")
                if handler is not None:
                    handler.send({"type": P.KICKED, "message": "The host removed you from the room"})
                    handler.close("kicked", flush=True)
                return None
            if action == "set_role":
                role = msg.get("role")
                if role not in P.ROLES:
                    return P.E_BAD_MESSAGE, "bad role"
                if role == P.ROLE_HOST:             # hand over host
                    part.role = P.ROLE_EDITOR
                    if self.follow == part.username:
                        self._set_follow(None, part.username)
                target.role = role
                self.broadcast({"type": P.PRESENCE, "event": "role_changed",
                                "user": target_name, "role": role,
                                "participants": self.participant_list()})
                return None
            return P.E_BAD_MESSAGE, "unknown admin action"

    # ================================================================ persistence
    def export_state(self):
        with self.lock:
            return {"format": 1, "room": self.code, "saved_at": time.time(),
                    "seq": self.seq, "board": self.model.to_dict(),
                    "chat": list(self.chat), "chat_id": self.chat_id,
                    "votes": self.votes, "settings": self.settings}

    def load_state(self, data, by):
        """Replace the board with a saved one and push it to everyone."""
        with self.lock:
            board = BoardModel.from_dict(data["board"])
            # Keep seq strictly increasing so clients never see it go backwards.
            self.seq += 1
            board.seq = self.seq
            self.model = board
            self.votes = {sid: list(v) for sid, v in data.get("votes", {}).items()
                          if sid in board.strokes}
            self.replay.clear()                 # old ops no longer describe this board
            self.hist_base = BoardModel.from_dict(board.to_dict())
            self.history = []
            for k in self.undo:
                self.undo[k] = []
                self.redo[k] = []
            self.dirty = True
            self.broadcast({"type": P.RESET, "seq": self.seq,
                            "snapshot": self.model.to_dict(), "votes": self.votes, "by": by})
            return 0

    def summary(self):
        with self.lock:
            return {"code": self.code, "users": sum(p.connected for p in self.participants.values()),
                    "has_password": self.pw_hash is not None, "locked": self.locked}


class JoinError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class RoomManager:
    """All rooms on this server, plus the failed-password rate limiter."""

    def __init__(self, store=None, log=print, media_store=None):
        self.media_store = media_store
        self.rooms = {}
        self.lock = threading.Lock()
        self.store = store
        self.log = log
        self.failures = {}            # ip -> [timestamps of failed password attempts]
        self.stats = Stats()

    def create(self, password, creator):
        with self.lock:
            code = new_room_code()
            while code in self.rooms:
                code = new_room_code()
            room = Room(code, password, creator, log=self.log)
            self.rooms[code] = room
        self.log(f"[{code}] room created by {creator}")
        return room

    def get(self, code):
        if not isinstance(code, str):
            return None
        with self.lock:
            return self.rooms.get(code.strip().upper())

    def all(self):
        with self.lock:
            return list(self.rooms.values())

    def close(self, room, by):
        """Host ends the session: everyone is disconnected and the room is deleted."""
        with self.lock:
            self.rooms.pop(room.code, None)
        with room.lock:
            room.closed = True
            for p in list(room.participants.values()):
                if p.handler is not None:
                    p.handler.send({"type": P.KICKED, "closed": True,
                                    "message": f"{by} ended the session"})
                    p.handler.clean_exit = True
                    p.handler.close("room closed", flush=True)
            room.participants.clear()
        if self.store is not None:
            self.store.delete_autosave(room.code)
        self.log(f"[{room.code}] session ended by {by}")

    # rate limiting of password guessing: 3 failures/minute => 30 s block
    def blocked(self, ip):
        now = time.time()
        recent = [t for t in self.failures.get(ip, []) if now - t < 60]
        self.failures[ip] = recent
        return len(recent) >= 3 and now - recent[-1] < 30

    def record_failure(self, ip):
        self.failures.setdefault(ip, []).append(time.time())


class Stats:
    """Server-wide traffic counters (for the dashboard and --stats)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.msgs_in = self.msgs_out = self.bytes_in = self.bytes_out = 0
        self.ops = 0
        self.started = time.time()

    def rx(self, nbytes):
        with self.lock:
            self.msgs_in += 1
            self.bytes_in += nbytes

    def tx(self, nbytes):
        with self.lock:
            self.msgs_out += 1
            self.bytes_out += nbytes

    def snapshot(self):
        with self.lock:
            return {"msgs_in": self.msgs_in, "msgs_out": self.msgs_out,
                    "bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
                    "uptime": time.time() - self.started}
