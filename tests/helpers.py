"""Test helpers: an in-process server on a free port and headless clients."""

import os
import queue
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client.net import NetClient          # noqa: E402
from common import protocol as P          # noqa: E402
from common.model import BoardModel       # noqa: E402
from server.main import Server            # noqa: E402


def start_server(**kw):
    save_dir = kw.pop("save_dir", None) or tempfile.mkdtemp(prefix="wb-test-")
    srv = Server("127.0.0.1", 0, 0, save_dir, discovery=False, autosave=0,
                 logger=lambda m: None, **kw)
    srv.start()
    return srv


class Bot:
    """A headless participant: NetClient + its own BoardModel, like the GUI."""

    def __init__(self, port, name, room=None, password="", create=False, tls=False):
        self.net = NetClient("127.0.0.1", port, name, room, password, create, tls=tls)
        self.model = BoardModel()
        self.name = name
        self.chat = []
        self.errors = []
        self.participants = []
        self.status = None
        self.other = []
        self.welcome = self.net.connect()
        self.room = self.welcome["room"]
        self.pump()

    def pump(self, wait=0.0):
        """Apply every queued event to the local model (what the GUI's after() loop does)."""
        end = time.time() + wait
        while True:
            try:
                ev = self.net.events.get(timeout=max(0.0, min(0.05, end - time.time())))
            except queue.Empty:
                if time.time() >= end:
                    return
                continue
            t = ev["type"]
            if t == "_WELCOME":
                w = ev["msg"]
                if "snapshot" in w:
                    self.model = BoardModel.from_dict(w["snapshot"])
                for m in w.get("missed_ops", []):
                    self.model.apply(m["op"])
                self.chat = list(w["chat_history"])
                self.participants = w["participants"]
                self.role = w["role"]
            elif t == P.OP:
                self.model.apply(ev["op"])
            elif t == P.RESET:
                self.model = BoardModel.from_dict(ev["snapshot"])
            elif t == P.CHAT:
                self.chat.append(ev)
            elif t == P.ERROR:
                self.errors.append(ev)
            elif t == P.PRESENCE:
                self.participants = ev["participants"]
            elif t == "_STATUS":
                self.status = ev["state"]
            else:
                self.other.append(ev)

    def stroke(self, pts, color="#000000", width=3, tool="pen", **extra):
        sid = self.net.new_stroke_id()
        self.net.send_board_op(P.STROKE, stroke={"id": sid, "tool": tool, "color": color,
                                                  "width": width, "points": pts, **extra})
        return sid

    def close(self):
        self.net.close()


def wait_until(cond, bots, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        for b in bots:
            b.pump()
        if cond():
            return True
        time.sleep(0.02)
    for b in bots:
        b.pump()
    return cond()
