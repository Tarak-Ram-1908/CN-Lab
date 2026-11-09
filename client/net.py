"""
Client networking: connection, heartbeat, ordering and automatic reconnect.

The GUI never touches sockets. NetClient runs three background threads
(receiver, writer, heartbeat) and hands every incoming message to the GUI
through a thread-safe queue (`self.events`). The GUI drains that queue
with Tk's after() timer, because Tkinter widgets may only be used from the
main thread.

Reliability features
--------------------
* Ordering: board ops carry a global seq. Ops are delivered to the GUI in
  strictly increasing seq order; duplicates (seq <= last_seq) are dropped and
  an out-of-order op would be buffered until the gap is filled.
* Heartbeat: PING every 3 s; if nothing arrives for 10 s the connection is
  treated as dead (this also catches a pulled cable, which TCP alone may not
  notice for minutes).
* Reconnect: exponential backoff 1, 2, 4, 8 s. The HELLO carries the
  session_token (to get the same seat back) and last_seq (so the server sends
  only the missed ops).
* Pending ops: board ops sent but not yet echoed back by the server are kept
  and re-sent after a reconnect. Because each keeps its op_id, the server
  discards any that it had already applied - no duplicates.
"""

import itertools
import queue
import secrets
import socket
import threading
import time

from common import protocol as P

_STOP = object()


class JoinFailed(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class NetClient:
    def __init__(self, host, port, username, room=None, password="", create=False, tls=False):
        self.host, self.port = host, port
        self.tls = tls
        self.hello = {"type": P.HELLO, "username": username, "room": room,
                      "password": password, "create": create,
                      "version": P.PROTOCOL_VERSION}
        self.events = queue.Queue()        # -> GUI (or tests)
        self.client_tag = secrets.token_hex(3)
        self._counter = itertools.count(1)
        self.sock = None
        self.outq = None
        self.pending = {}                  # op_id -> msg awaiting the server's echo
        self.pending_lock = threading.Lock()
        self.last_seq = 0
        self.buffer = {}                   # out-of-order ops: seq -> msg
        self.token = None
        self.welcome = None
        self.rtt_ms = None
        self.last_rx = time.monotonic()
        self.stopping = threading.Event()  # set by close(): no reconnect
        self.conn_lock = threading.Lock()
        self.connected = False
        self.generation = 0                # bumps on every (re)connect

    # ================================================================== ids
    def new_op_id(self):
        return f"{self.client_tag}-{next(self._counter)}"

    def new_stroke_id(self):
        return f"{self.client_tag}-s{next(self._counter)}"

    # ================================================================== connect
    def connect(self, timeout=5.0):
        """Blocking first connect + handshake. Raises JoinFailed / OSError."""
        welcome = self._open(timeout, reconnect=False)
        if self.hello.get("create"):
            # After creating, any reconnect must JOIN the room we created.
            self.hello["create"] = False
            self.hello["room"] = welcome["room"]
        return welcome

    def open_socket(self, timeout):
        """TCP connection to the server (TLS-wrapped when the server uses TLS)."""
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        P.tune_socket(sock)
        if self.tls:
            try:
                sock = P.client_tls_wrap(sock)
            except Exception:
                sock.close()
                raise
        return sock

    def _open(self, timeout, reconnect):
        sock = self.open_socket(timeout)
        hello = dict(self.hello)
        if self.token:
            hello["session_token"] = self.token
            hello["last_seq"] = self.last_seq
        try:
            P.send_msg(sock, hello)
            sock.settimeout(timeout)
            reply = P.recv_msg(sock)
        except Exception:
            sock.close()
            raise
        if reply["type"] == P.ERROR:
            sock.close()
            raise JoinFailed(reply.get("code"), reply.get("message", "refused"))
        if reply["type"] != P.WELCOME:
            sock.close()
            raise JoinFailed(P.E_BAD_MESSAGE, "unexpected reply from server")
        sock.settimeout(1.0)               # short timeout so threads notice shutdown
        self.token = reply["session_token"]
        self.welcome = reply
        # Hand the WELCOME to the GUI and reset ordering state BEFORE the
        # receiver thread starts, so no OP can overtake it.
        self.buffer.clear()
        self.last_seq = reply["seq"]
        with self.pending_lock:
            for m in reply.get("missed_ops", []):
                self.pending.pop(m["op"]["op_id"], None)
        self.events.put({"type": "_WELCOME", "msg": reply, "reconnect": reconnect})
        self.events.put({"type": "_STATUS", "state": "connected",
                         "text": "Reconnected" if reconnect else "Connected"})
        with self.conn_lock:
            self.sock = sock
            self.outq = queue.Queue()
            self.generation += 1
            gen = self.generation
            self.connected = True
        self.last_rx = time.monotonic()
        threading.Thread(target=self._receiver, args=(sock, gen), daemon=True).start()
        threading.Thread(target=self._writer, args=(sock, self.outq), daemon=True).start()
        threading.Thread(target=self._heartbeat, args=(gen,), daemon=True).start()
        return reply

    # ================================================================== sending
    def send(self, msg):
        """Queue a message for the server. Board ops are remembered until echoed."""
        if msg.get("type") in P.BOARD_KINDS and "op_id" in msg:
            with self.pending_lock:
                self.pending[msg["op_id"]] = msg
        with self.conn_lock:
            q = self.outq if self.connected else None
        if q is not None:
            try:
                q.put_nowait(P.encode(msg))
            except P.ProtocolError as exc:
                self.events.put({"type": P.ERROR, "code": P.E_BAD_MESSAGE, "message": str(exc)})

    def send_board_op(self, kind, **fields):
        op_id = self.new_op_id()
        msg = {"type": kind, "op_id": op_id, **fields}
        self.send(msg)
        return op_id

    def _writer(self, sock, outq):
        try:
            while True:
                frame = outq.get()
                if frame is _STOP:
                    return
                sock.sendall(frame)
        except OSError:
            pass

    # ================================================================== receiving
    def _receiver(self, sock, gen):
        try:
            while not self.stopping.is_set():
                try:
                    msg = P.recv_msg(sock)
                except socket.timeout:
                    if time.monotonic() - self.last_rx > P.DEAD_TIMEOUT:
                        raise ConnectionError("no data from server for 10 s")
                    continue
                self.last_rx = time.monotonic()
                self._handle(msg)
        except (ConnectionError, OSError, P.ProtocolError) as exc:
            reason = str(exc)
        else:
            reason = "closed"
        self._connection_lost(gen, reason)

    def _handle(self, msg):
        """Route one message. A malformed message from the server is ignored instead of
        killing the receiver thread."""
        try:
            self._route(msg)
        except (KeyError, TypeError, ValueError) as exc:
            self.events.put({"type": "_BADMSG", "message": f"ignored a malformed message: {exc}"})

    def _route(self, msg):
        t = msg["type"]
        if t == P.OP:
            self._ordered(msg)
        elif t == P.RESET:
            self.buffer.clear()
            self.last_seq = msg["seq"]
            with self.pending_lock:
                self.pending.clear()
            self.events.put(msg)
        elif t == P.PONG:
            if isinstance(msg.get("t"), (int, float)):
                self.rtt_ms = (time.monotonic() - msg["t"]) * 1000
                self.events.put({"type": "_RTT", "ms": self.rtt_ms})
        elif t == P.ACK:
            with self.pending_lock:
                self.pending.pop(msg.get("op_id"), None)
            self.events.put(msg)
        elif t == P.ERROR:
            ref = msg.get("ref_op_id")
            if ref:
                with self.pending_lock:
                    self.pending.pop(ref, None)
            self.events.put(msg)
        elif t == P.KICKED:
            self.stopping.set()
            self.events.put(msg)
        else:
            self.events.put(msg)

    def _ordered(self, msg):
        seq = msg["seq"]
        if seq <= self.last_seq:
            return                                   # duplicate / already applied
        self.buffer[seq] = msg
        while self.last_seq + 1 in self.buffer:      # deliver every contiguous op
            m = self.buffer.pop(self.last_seq + 1)
            self.last_seq = m["seq"]
            with self.pending_lock:
                self.pending.pop(m["op"]["op_id"], None)
            self.events.put(m)

    def _heartbeat(self, gen):
        while not self.stopping.is_set() and gen == self.generation:
            self.send({"type": P.PING, "t": time.monotonic()})
            for _ in range(int(P.HEARTBEAT_INTERVAL * 10)):
                if self.stopping.is_set() or gen != self.generation:
                    return
                time.sleep(0.1)

    # ================================================================== reconnect
    def _connection_lost(self, gen, reason):
        with self.conn_lock:
            if gen != self.generation or not self.connected:
                return
            self.connected = False
            if self.outq is not None:
                self.outq.put(_STOP)
            try:
                self.sock.close()
            except OSError:
                pass
        if self.stopping.is_set():
            self.events.put({"type": "_STATUS", "state": "closed", "text": "Disconnected"})
            return
        self.events.put({"type": "_STATUS", "state": "reconnecting",
                         "text": f"Connection lost ({reason}) - reconnecting..."})
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self):
        delay = 1.0
        attempt = 0
        while not self.stopping.is_set():
            attempt += 1
            try:
                self._open(timeout=3.0, reconnect=True)
            except JoinFailed as exc:
                self.stopping.set()
                self.events.put({"type": "_STATUS", "state": "closed",
                                 "text": f"Could not rejoin: {exc.message}"})
                return
            except OSError:
                self.events.put({"type": "_STATUS", "state": "reconnecting",
                                 "text": f"Server unreachable - retry {attempt} in {int(delay)} s"})
                for _ in range(int(delay * 10)):
                    if self.stopping.is_set():
                        return
                    time.sleep(0.1)
                delay = min(delay * 2, 8.0)
                continue
            # Re-send ops the server may never have received (it ignores duplicates).
            with self.pending_lock:
                again = list(self.pending.values())
            for m in again:
                self.send(m)
            return

    def drop_connection(self):
        """Testing/demo helper: kill the socket abruptly as if the cable was pulled."""
        with self.conn_lock:
            sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self):
        """Leave cleanly (BYE) and stop all threads."""
        if self.stopping.is_set() and not self.connected:
            return
        self.stopping.set()
        with self.conn_lock:
            if self.connected and self.outq is not None:
                try:
                    self.outq.put_nowait(P.encode({"type": P.BYE}))
                except P.ProtocolError:
                    pass
                self.outq.put(_STOP)
        time.sleep(0.05)
        with self.conn_lock:
            self.connected = False
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()
