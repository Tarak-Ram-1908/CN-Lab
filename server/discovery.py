"""
UDP room discovery.

Clients do not know the server's IP address in advance, so they broadcast a
small WB_DISCOVER datagram to 255.255.255.255:5051. Every server on the LAN
answers with a unicast WB_HERE datagram listing its TCP port and rooms.

UDP is fine here: a lost discovery packet is harmless (the user presses
Refresh or types the IP), and broadcast is only possible with UDP. All
actual whiteboard traffic uses TCP.
"""

import json
import socket
import threading

from common import protocol as P
from common.discovery import local_ips  # noqa: F401  (re-exported for server.main)


class DiscoveryResponder(threading.Thread):
    def __init__(self, rooms, tcp_port, udp_port=P.DEFAULT_UDP_PORT, log=print):
        super().__init__(name="discovery", daemon=True)
        self.rooms = rooms
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.log = log
        self.sock = None

    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("", self.udp_port))      # all interfaces: receives broadcasts
        except OSError as exc:
            self.log(f"discovery disabled (UDP {self.udp_port}: {exc})")
            return
        self.log(f"discovery listening on UDP {self.udp_port}")
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except OSError:
                return
            try:
                req = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(req, dict) or req.get("type") != P.DISCOVER_MAGIC:
                continue
            reply = {"type": P.DISCOVER_REPLY, "name": socket.gethostname(),
                     "tcp_port": self.tcp_port, "version": P.PROTOCOL_VERSION,
                     "tls": getattr(self, "tls", False),
                     "rooms": [r.summary() for r in self.rooms.all()]}
            try:
                self.sock.sendto(json.dumps(reply).encode("utf-8"), addr)
            except OSError:
                pass
