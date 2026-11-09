"""
Benchmark: latency, throughput and consistency with N simultaneous clients.

    python tools/benchmark.py                       # in-process server, 5 and 10 clients
    python tools/benchmark.py --clients 8 --ops 100
    python tools/benchmark.py --host 192.168.1.20 --room K7Q2 --clients 6   # real LAN server

Every client draws `ops` strokes as fast as it can, all at the same time. Because
all clients run in this one process they share a clock, so for every operation
we know exactly when it was sent and when each *other* client received it:

    delivery latency = time the op reached client B - time client A sent it

At the end the board fingerprint of every client is compared (identical
fingerprints = identical boards), and a late joiner measures how much the
zlib compression of the snapshot saves. Results go to docs/results/.
"""

import argparse
import json
import os
import queue
import statistics
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from client.net import NetClient          # noqa: E402
from common import protocol as P          # noqa: E402
from common.model import BoardModel       # noqa: E402


def run(n_clients, n_ops, host=None, port=None, room=None, password="", rate=0.0):
    """rate = strokes per second per client (0 = burst: as fast as possible)."""
    server = None
    if host is None:
        import tempfile
        from server.main import Server
        server = Server("127.0.0.1", 0, 0, tempfile.mkdtemp(), discovery=False, autosave=0,
                        logger=lambda m: None)
        port = server.start()
        host = "127.0.0.1"
    sent = {}                                   # op_id -> send time
    lock = threading.Lock()
    nets = []
    first = NetClient(host, port, "bench0", room, password, create=room is None)
    w = first.connect()
    room = w["room"]
    nets.append(first)
    for i in range(1, n_clients):
        n = NetClient(host, port, f"bench{i}", room, password)
        n.connect()
        nets.append(n)
    models = [BoardModel() for _ in nets]
    latencies = []
    received = [0] * len(nets)
    total = n_clients * n_ops
    done = threading.Event()

    def consume(k):
        net = nets[k]
        while not done.is_set():
            try:
                ev = net.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev["type"] == "_WELCOME" and "snapshot" in ev["msg"]:
                models[k] = BoardModel.from_dict(ev["msg"]["snapshot"])
            if ev["type"] != P.OP:
                continue
            now = time.perf_counter()
            op = ev["op"]
            models[k].apply(op)
            with lock:
                t0 = sent.get(op["op_id"])
                if t0 is not None and not op["op_id"].startswith(net.client_tag):
                    latencies.append((now - t0) * 1000)
                received[k] += 1
                if all(r >= total for r in received):
                    done.set()

    threads = [threading.Thread(target=consume, args=(k,), daemon=True) for k in range(len(nets))]
    for t in threads:
        t.start()
    barrier = threading.Barrier(len(nets))

    def draw(k):
        net = nets[k]
        barrier.wait()
        for j in range(n_ops):
            pts = [float(v) for v in (k * 50 + j, j, k * 50 + j + 30, j + 20, k * 50 + j + 60, j)]
            msg_id = net.new_op_id()
            stroke = {"id": net.new_stroke_id(), "tool": "pen", "color": "#1d70b8",
                      "width": 3, "points": pts}
            with lock:
                sent[msg_id] = time.perf_counter()
            net.send({"type": P.STROKE, "op_id": msg_id, "stroke": stroke})
            if rate:
                time.sleep(1 / rate)

    start = time.perf_counter()
    workers = [threading.Thread(target=draw, args=(k,)) for k in range(len(nets))]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    ok = done.wait(60)
    elapsed = time.perf_counter() - start
    prints = {m.fingerprint() for m in models}

    # late joiner: snapshot size raw vs compressed on the wire
    late = NetClient(host, port, "benchlate", room, password)
    lw = late.connect()
    snap = lw.get("snapshot") or {}
    raw = len(json.dumps(lw, separators=(",", ":")).encode())
    wire = len(P.encode(lw))
    late.close()
    for n in nets:
        n.close()
    if server is not None:
        server.stop()
    lat = sorted(latencies)
    pct = (lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]) if lat else (lambda q: 0)
    res = {
        "mode": f"paced {rate:g} strokes/s per client" if rate else "burst",
        "clients": n_clients, "ops_per_client": n_ops, "total_ops": total,
        "all_delivered": ok, "seconds": round(elapsed, 3),
        "ops_sequenced_per_s": round(total / elapsed, 1),
        "deliveries_per_s": round(total * n_clients / elapsed, 1),
        "latency_ms": {"avg": round(statistics.mean(lat), 2) if lat else None,
                       "p50": round(pct(0.5), 2), "p95": round(pct(0.95), 2),
                       "max": round(lat[-1], 2) if lat else None, "samples": len(lat)},
        "identical_boards": len(prints) == 1, "fingerprint": next(iter(prints)),
        "late_join_snapshot": {"objects": len(snap.get("strokes", [])), "json_bytes": raw,
                               "wire_bytes": wire,
                               "saved_percent": round(100 * (1 - wire / raw), 1)},
    }
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clients", type=int, nargs="*", default=[5, 10])
    ap.add_argument("--ops", type=int, default=200, help="strokes per client")
    ap.add_argument("--rate", type=float, default=10.0,
                    help="paced run: strokes/s per client (a busy class); 0 = skip")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int, default=P.DEFAULT_TCP_PORT)
    ap.add_argument("--room")
    ap.add_argument("--password", default="")
    args = ap.parse_args()
    out_dir = os.path.join(ROOT, "docs", "results")
    os.makedirs(out_dir, exist_ok=True)
    for n, rate in [(n, r) for n in args.clients for r in ([args.rate] if args.rate else []) + [0]]:
        res = run(n, args.ops if not rate else min(args.ops, 60), args.host,
                  args.port if args.host else None, args.room, args.password, rate)
        lat = res["latency_ms"]
        print(f"[{res['mode']}] "
              f"{n:>3} clients x {res['ops_per_client']} ops: {res['ops_sequenced_per_s']:>7} "
              "ops/s sequenced, "
              f"{res['deliveries_per_s']:>8} deliveries/s | latency avg {lat['avg']} ms, "
              f"p95 {lat['p95']} ms, max {lat['max']} ms | identical boards: "
              f"{res['identical_boards']} | snapshot {res['late_join_snapshot']['json_bytes']} B "
              f"-> {res['late_join_snapshot']['wire_bytes']} B on the wire "
              f"(-{res['late_join_snapshot']['saved_percent']}%)")
        tag = "paced" if rate else "burst"
        with open(os.path.join(out_dir, f"benchmark_{n}_clients_{tag}.json"), "w") as f:
            json.dump(res, f, indent=2)


if __name__ == "__main__":
    main()
