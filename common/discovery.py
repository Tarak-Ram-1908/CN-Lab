"""
Client-side LAN discovery helpers (UDP broadcast) and local IP lookup.

The matching responder runs in server/discovery.py.
"""

import json
import socket
import time

from common import protocol as P


def discover(udp_port=P.DEFAULT_UDP_PORT, timeout=1.2, extra_targets=()):
    """Client side: broadcast a request and collect replies for `timeout` seconds.

    Returns a list of {"host", "port", "name", "rooms"} dicts, one per server.
    """
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)   # required to send broadcasts
    s.settimeout(0.2)
    payload = json.dumps({"type": P.DISCOVER_MAGIC, "version": P.PROTOCOL_VERSION}).encode()
    targets = ["255.255.255.255", "<broadcast>", "127.0.0.1"] + list(extra_targets)
    targets += _directed_broadcasts()
    for t in targets:
        try:
            s.sendto(payload, (t, udp_port))
        except OSError:
            pass
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            rep = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(rep, dict) and rep.get("type") == P.DISCOVER_REPLY:
            key = (addr[0], rep.get("tcp_port"))
            found[key] = {"host": addr[0], "port": rep.get("tcp_port"),
                          "tls": bool(rep.get("tls")), "version": rep.get("version"),
                          "name": rep.get("name", addr[0]), "rooms": rep.get("rooms", [])}
    s.close()
    # Prefer a real LAN address over loopback for the same server.
    lan = {k: v for k, v in found.items() if not k[0].startswith("127.")}
    names = {v["name"] for v in lan.values()}
    result = list(lan.values()) + [v for v in found.values()
                                   if v["host"].startswith("127.") and v["name"] not in names]
    return result


def _directed_broadcasts():
    """x.y.z.255 for each local IPv4 address (some Windows setups need this)."""
    out = []
    for ip in local_ips():
        parts = ip.split(".")
        if len(parts) == 4:
            out.append(".".join(parts[:3] + ["255"]))
    return out


def local_ips():
    """Best-effort list of this machine's LAN IPv4 addresses (works offline)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # UDP "connect" picks the outgoing interface without sending any packet.
    for probe in ("10.255.255.255", "192.168.255.255", "172.31.255.255"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 1))
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return sorted(ip for ip in ips if not ip.startswith("127.") and ip != "0.0.0.0")
