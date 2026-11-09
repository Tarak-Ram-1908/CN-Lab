# Speaking script: 4-member team (video + in-person demo)

Target: 12–15 minutes in total, about 3 minutes per person plus the live demo. Each member owns
some code and the matching Viva Prep questions (`docs/Viva_Prep.md`).

| Member | Area | Code they must know | Viva questions |
|---|---|---|---|
| 1 - Tarak Ram | Server networking and recovery | `server/main.py`, `server/handler.py`, `server/room.py` (sessions), `server/store.py`, `server/dashboard.py` | 1–2, 8–11, 21–30 |
| 2 - Bala Murugan | Client and teacher UI | `client/ui/app.py`, `client/ui/board.py`, `client/ui/theme.py`, `client/net.py` | 18–20, 25, 33, 42 |
| 3 - Lokesh Kumar | Protocol, consistency, security | `common/protocol.py`, `common/model.py`, `common/geometry.py`, `server/room.py` (board ops) | 3–7, 12–17, 31–32, 34–38 |
| 4 - Parthasaradhi | Testing, performance, classroom features, wrap-up | `tests/*`, `tools/benchmark.py`, `tools/bot_swarm.py`, `client/ui/export.py`, `server/media.py` | 39–41, 43 |

---

## Member 1 - Alahari Tarak Ram (CS24B1031): Server networking (about 3 min)

1. **Opening (30 s).** "ClassBoard is a whiteboard a teacher and a whole class share over the
   college Wi-Fi with the internet switched off. Everything runs on our own TCP protocol in
   plain Python."
2. **Architecture (90 s).**
   * The server binds 0.0.0.0:5050 and prints its LAN IP.
   * It has one accept thread, plus a reader thread and a writer thread with a send queue for
     each client.
   * Rooms are isolated, with one lock each. Clients never talk to each other directly; it is a
     server-authoritative star.
   * Files go over a separate TCP connection, so a video never blocks strokes.
3. **Recovery (60 s).**
   * A PING every 3 s; 10 s of silence means the connection is dead.
   * The seat is kept for 30 s. The client reconnects with its token and `last_seq` and gets
     only what it missed. Unacknowledged ops are re-sent with the same `op_id`.
   * Rooms are autosaved atomically and restored after a restart.
   - *Demo moment:* the dashboard (`start_server.bat`) shows the rooms and messages/s live.
     Turn one laptop's Wi-Fi off and on, and watch it catch up.

## Member 2 - Ponnangatti Bala Murugan (CS24B1039): Client and the teacher UI (about 3 min)

1. **Tour (60 s).**
   * Header: room code, pages, Present, Students can draw.
   * Tool rail on the left.
   * Board of white pages with ruled or grid paper.
   * Class / People / Chat panel; status bar with latency and `seq`.
2. **Optimistic drawing (30 s).** Your stroke appears instantly. The server's echo replaces the
   provisional copy, and a refusal rolls it back.
3. **Threading rule (30 s).** Only the Tk thread touches widgets. Network threads feed a queue
   that is drained every 15 ms, so the UI never freezes.
4. **Infinite canvas (30 s).** World coordinates plus a per-client view: pan and zoom never
   change the shared data. The minimap shows everyone's cursors.
   - *Demo moment:* the teacher presses **Present**, and the students' screens follow page by
     page. Then use the laser pointer.

## Member 3 - Vallipalli Lokesh Kumar (CS24B1046): Protocol, consistency, security (about 4 min)

1. **Framing (45 s).** A 4-byte big-endian length, then JSON. Explain fragmentation and
   coalescing, the 4 MB guard, and zlib with the 32 MB inflate cap for snapshots (−85%).
2. **Consistency (75 s).** The server assigns `seq` under the room lock; every client applies
   ops in `seq` order with the *same* `BoardModel.apply`, so the boards are identical.
   Moves and edits are layered ops, never overwrites, so concurrent drags and undo are
   deterministic. Say why a CRDT is not needed when there is always a server.
3. **Undo and history (45 s).** Undo is per user and flips the `active` flag of your own last
   op. Version history is just a replay of the same log.
4. **Security (45 s).** PBKDF2 passwords, roles enforced on the server, own-work-only mode,
   validation, rate limits, private messages and optional TLS.
   - *Demo moment:* untick **Students can draw**. A student tries to draw and is refused.
     Then make one student view-only.

## Member 4 - Bellala Parthasaradhi (CS24B1069): Testing, performance, classroom features, wrap-up (about 3 min)

1. **Testing (45 s).** 110 automated tests (27 drive the real GUI), including convergence with 10 clients, fault cases
   (reconnect, offline edits, a slow client, bad frames) and TLS. Show
   `python -m pytest tests -q`.
2. **Performance (45 s).** `python tools/benchmark.py`: about 2 ms average delivery with 5
   clients, 11,000 deliveries/s in a burst, identical boards.
3. **Classroom features (60 s).** Quick poll with live results, raised hands, timer, sticky
   notes with voting, flowchart and mind map (Tab / Enter), and all pages exported as PDF
   notes.
4. **Close (30 s).** Trade-offs: TCP vs UDP, a sequencer vs a CRDT, threads vs asyncio, and
   plaintext by default vs TLS. Then go into the live demo.
   - *Demo moment:* **Version history → Replay**, then restore an older version.

---

## Live demo checklist (rehearse with Wi-Fi off)

1. **Server laptop:** run `start_server.bat` and read the IP from the dashboard. The firewall
   must allow Private networks (`tools\open_firewall_windows.bat`).
2. **Teacher:** *Start a class* with a password. **Students:** pick the class from the
   discovered list.
3. **Teacher:**
   * Ruled paper, then draw a rough circle (Smart shapes) and add Page 2.
   * Present, then point with the laser.
   * Build a flowchart: Node, Tab, Tab, Enter; Connect; label *Yes* / *No*.
4. **Students:**
   * Add sticky notes. The teacher drags them into groups, everyone votes, and the teacher
     opens **Results**.
   * The teacher runs a poll and a 2-minute timer.
   * A student raises a hand and sends a private message.
5. **Access control:** untick *Students can draw* and show the refused draw; make a student
   view-only, then allow drawing again.
6. **Recovery:** Wi-Fi off on one student laptop for 15 s, draw while offline, Wi-Fi on, and
   watch it catch up.
7. **History and persistence:** Version history, Replay, Restore. Export all pages as PDF.
   Ctrl+C the server, start it again, and show the class comes back.
8. **Backup plan if a laptop fails:** `python tools/bot_swarm.py --room CODE --bots 5` adds
   simulated classmates.
