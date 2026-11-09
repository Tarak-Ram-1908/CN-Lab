"""
Whiteboard server entry point.

    python run_server.py                 # TCP 5050, UDP discovery 5051
    python run_server.py --port 6000 --save-dir boards
    python run_server.py --dashboard     # live window: rooms, users, messages/s, KB/s
    python run_server.py --stats 10      # print a traffic line every 10 s
    python run_server.py --tls-cert cert.pem --tls-key key.pem   # encrypted (tools/gen_cert)

The server accepts TCP connections, starts one ClientHandler per client, and
runs a housekeeping thread that removes participants whose 30-second grace
period has expired and autosaves changed rooms every 30 seconds.
"""

import argparse
import datetime
import os
import socket
import sys
import threading
import time

from common import protocol as P
from .discovery import DiscoveryResponder, local_ips
from .handler import ClientHandler
from .media import MediaStore
from .room import RoomManager
from .store import Store

_log_lock = threading.Lock()


def log(msg):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    with _log_lock:
        print(f"{stamp}  {msg}", flush=True)


class Server:
    def __init__(self, host="0.0.0.0", port=P.DEFAULT_TCP_PORT, udp_port=P.DEFAULT_UDP_PORT,
                 save_dir="saves", discovery=True, autosave=30.0, logger=log, ssl_context=None):
        self.host, self.port, self.udp_port = host, port, udp_port
        self.log = logger
        self.store = Store(save_dir)
        self.rooms = RoomManager(self.store, log=logger, media_store=MediaStore(save_dir))
        self.discovery = discovery
        self.autosave = autosave
        self.sock = None
        self.ssl_context = ssl_context
        self.running = threading.Event()

    def start(self):
        """Bind and start background threads. Returns the actual TCP port."""
        restored = self.store.restore_rooms(self.rooms)
        if restored:
            self.log(f"restored rooms from autosave: {', '.join(restored)}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            # Windows' SO_REUSEADDR would let a SECOND server bind the same port and
            # silently steal connections; exclusive use gives a clean error instead.
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.host, self.port))
        except OSError as exc:
            self.sock.close()
            raise OSError(f"port {self.port} is already in use - is another server running? "
                          f"Close it or start with --port {self.port + 1} ({exc})") from exc
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.running.set()
        threading.Thread(target=self._accept_loop, name="accept", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()
        if self.discovery:
            responder = DiscoveryResponder(self.rooms, self.port, self.udp_port, log=self.log)
            responder.tls = self.ssl_context is not None
            responder.start()
        return self.port

    def _accept_loop(self):
        while self.running.is_set():
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            if not self.running.is_set():          # raced with stop()
                conn.close()
                break
            self.log(f"connection from {addr[0]}:{addr[1]}")
            if self.ssl_context is not None:
                try:
                    conn = self.ssl_context.wrap_socket(conn, server_side=True,
                                                        do_handshake_on_connect=False)
                except (OSError, ValueError) as exc:
                    self.log(f"TLS setup failed for {addr[0]}: {exc}")
                    conn.close()
                    continue
            ClientHandler(conn, addr, self.rooms, log=self.log).start()

    def _housekeeping(self):
        last_save = time.time()
        while self.running.is_set():
            time.sleep(1.0)
            now = time.time()
            for room in self.rooms.all():
                room.reap(now)
            if self.autosave and now - last_save >= self.autosave:
                last_save = now
                self.save_all()

    def save_all(self):
        for room in self.rooms.all():
            if room.dirty:
                try:
                    self.store.autosave(room)
                except OSError as exc:
                    self.log(f"[{room.code}] autosave failed: {exc}")

    def stop(self):
        """Save every room, then close the listening socket and every client connection
        (clients notice and keep retrying until the server is back)."""
        self.running.clear()
        self.save_all()
        try:
            # On Linux close() alone does not wake a thread blocked in accept(), so a
            # "stopped" server could still take one more connection: shut it down first.
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        for room in self.rooms.all():
            with room.lock:
                handlers = [p.handler for p in room.participants.values() if p.handler]
            for h in handlers:
                h.close("server shutting down")


def main(argv=None):
    ap = argparse.ArgumentParser(description="LAN collaborative whiteboard server")
    ap.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_TCP_PORT, help="TCP port (default 5050)")
    ap.add_argument("--udp-port", type=int, default=P.DEFAULT_UDP_PORT,
                    help="UDP discovery port (default 5051)")
    ap.add_argument("--save-dir", default=None, help="folder for saved boards (default ./saves)")
    ap.add_argument("--no-discovery", action="store_true", help="disable UDP discovery")
    ap.add_argument("--dashboard", action="store_true",
                    help="open a live window with rooms, users and traffic")
    ap.add_argument("--stats", type=float, default=0, metavar="SECONDS",
                    help="print a traffic summary every SECONDS")
    ap.add_argument("--tls-cert", help="PEM certificate: enables TLS encryption")
    ap.add_argument("--tls-key", help="PEM private key for --tls-cert")
    args = ap.parse_args(argv)

    ctx = None
    if args.tls_cert or args.tls_key:
        if not (args.tls_cert and args.tls_key):
            log("--tls-cert and --tls-key must be given together")
            return 1
        try:
            ctx = P.server_tls_context(args.tls_cert, args.tls_key)
        except (OSError, ValueError) as exc:
            log(f"cannot load the TLS certificate/key: {exc}")
            return 1
    save_dir = args.save_dir or os.path.join(_base_dir(), "saves")
    server = Server(args.host, args.port, args.udp_port, save_dir, not args.no_discovery,
                    ssl_context=ctx)
    try:
        port = server.start()
    except OSError as exc:
        log(f"cannot start: {exc}")
        return 1

    print("=" * 60)
    print("  ClassBoard server running")
    print(f"  TCP port {port}   |   discovery UDP {args.udp_port}")
    ips = local_ips()
    if ips:
        print("  Clients can connect to:  " + "   ".join(ips))
    else:
        print("  No LAN address found - clients on this machine can use 127.0.0.1")
    print(f"  Saved boards: {save_dir}")
    print(f"  Encryption: {'TLS on' if ctx else 'off (plain TCP)'}")
    print("  Press Ctrl+C to stop (rooms are autosaved).")
    print("=" * 60, flush=True)
    if args.stats > 0:
        threading.Thread(target=_stats_loop, args=(server, args.stats), daemon=True).start()
    try:
        if args.dashboard:
            try:
                from .dashboard import run_dashboard
                run_dashboard(server, ips, port)   # returns when the window is closed
            except Exception as exc:               # no display / no Tk on this machine
                log(f"dashboard unavailable ({exc}) - running in the console")
                while True:
                    time.sleep(1)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    log("shutting down - saving rooms")
    server.stop()
    return 0


def _stats_loop(server, every):
    prev = server.rooms.stats.snapshot()
    while server.running.is_set():
        time.sleep(every)
        cur = server.rooms.stats.snapshot()
        rooms = server.rooms.all()
        users = sum(r.summary()["users"] for r in rooms)
        log(f"stats: {len(rooms)} room(s), {users} online | "
            f"in {(cur['msgs_in'] - prev['msgs_in']) / every:.0f} msg/s "
            f"{(cur['bytes_in'] - prev['bytes_in']) / every / 1024:.1f} KB/s | "
            f"out {(cur['msgs_out'] - prev['msgs_out']) / every:.0f} msg/s "
            f"{(cur['bytes_out'] - prev['bytes_out']) / every / 1024:.1f} KB/s")
        prev = cur


def _base_dir():
    """Folder next to the executable (PyInstaller) or the project root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    sys.exit(main())
