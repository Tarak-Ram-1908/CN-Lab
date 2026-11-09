"""
Demo / load-test tool: N headless participants join a room and draw at once.

    python tools/bot_swarm.py --host 192.168.1.20 --room K7Q2 --bots 5 --strokes 40
    python tools/bot_swarm.py --host 127.0.0.1 --room K7Q2 --password secret

Useful in the demo to show concurrent editing with only one or two laptops,
and to prove consistency: at the end every bot prints a fingerprint of its
canvas; identical fingerprints mean identical boards.
"""

import argparse
import math
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client.net import NetClient          # noqa: E402
from common import protocol as P          # noqa: E402
from common.model import BoardModel       # noqa: E402


def run_bot(i, args, results, barrier):
    name = f"bot{i}"
    net = NetClient(args.host, args.port, name, args.room, args.password)
    w = net.connect()
    model = BoardModel.from_dict(w["snapshot"]) if "snapshot" in w else BoardModel()
    color = P.USER_COLORS[(i + 3) % len(P.USER_COLORS)]
    rnd = random.Random(i)
    barrier.wait()
    for k in range(args.strokes):
        cx, cy = rnd.uniform(100, P.CANVAS_W - 100), rnd.uniform(100, P.CANVAS_H - 100)
        r = rnd.uniform(15, 60)
        pts = []
        for a in range(0, 361, 15):                      # little circles / spirals
            rr = r * (0.6 + 0.4 * a / 360)
            pts += [cx + rr * math.cos(math.radians(a)), cy + rr * math.sin(math.radians(a))]
            net.send({"type": P.CURSOR, "x": pts[-2], "y": pts[-1]}) if a % 90 == 0 else None
        net.send_board_op(P.STROKE, stroke={"id": net.new_stroke_id(), "tool": "pen",
                                            "color": color, "width": 3, "points": pts})
        time.sleep(args.delay)
    if args.chat:
        net.send({"type": P.CHAT, "text": f"{name} finished {args.strokes} strokes"})
    end = time.time() + 3
    while time.time() < end:                             # apply everything we receive
        try:
            ev = net.events.get(timeout=0.2)
        except Exception:
            continue
        if ev["type"] == P.OP:
            model.apply(ev["op"])
        elif ev["type"] == "_WELCOME" and "snapshot" in ev["msg"]:
            model = BoardModel.from_dict(ev["msg"]["snapshot"])
    results[name] = (len(model.visible_ids()), model.fingerprint())
    if not args.stay:
        net.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=P.DEFAULT_TCP_PORT)
    ap.add_argument("--room", required=True)
    ap.add_argument("--password", default="")
    ap.add_argument("--bots", type=int, default=5)
    ap.add_argument("--strokes", type=int, default=30)
    ap.add_argument("--delay", type=float, default=0.1, help="seconds between strokes")
    ap.add_argument("--chat", action="store_true", help="each bot posts a chat message")
    ap.add_argument("--stay", action="store_true", help="do not disconnect at the end")
    args = ap.parse_args()
    results = {}
    barrier = threading.Barrier(args.bots)
    threads = [threading.Thread(target=run_bot, args=(i, args, results, barrier))
               for i in range(1, args.bots + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name, (n, fp) in sorted(results.items()):
        print(f"{name}: {n} visible strokes, canvas fingerprint {fp}")
    fps = {fp for _, fp in results.values()}
    print("ALL CANVASES IDENTICAL" if len(fps) == 1 else "MISMATCH between bots!")
    if args.stay:
        input("Bots stay connected - press Enter to quit")


if __name__ == "__main__":
    main()
