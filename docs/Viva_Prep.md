# Viva prep: likely questions and model answers (ClassBoard)

Keep each answer short, then point at the code. File references are in brackets.

---

## A. Networking fundamentals

**1. Why TCP and not UDP?**
A whiteboard must not lose or reorder updates. A lost erase, or a stroke applied before the
undo that hid it, would leave screens permanently different. TCP gives reliable, ordered,
connection-oriented delivery. With UDP we would have to rebuild acknowledgements,
retransmission, ordering and fragmentation ourselves, because snapshots are far larger than one
datagram. On a LAN TCP costs about a millisecond: we measured 2–4 ms average delivery with 5–10
clients. UDP is used only for discovery, where a lost packet is harmless.
(`common/protocol.py`, `server/discovery.py`)

**2. What trade-off did you accept with TCP?**
Head-of-line blocking: if one segment is lost, later data waits. On a LAN loss is rare. For big
files it would matter, because a 100 MB video would block strokes, so every file transfer uses
its **own** short TCP connection (`server/media.py`). Stroke latency during a 60 MB upload stayed
at 0.4 ms.

**3. TCP is a byte stream. How do you know where a message ends?**
Length-prefix framing: a 4-byte unsigned big-endian length followed by that many bytes of UTF-8
JSON. `recv_exact()` loops until all N bytes have arrived (`common/protocol.py`).

**4. Fragmentation and coalescing?**
One `send` can arrive in several `recv`s, and several sends can arrive in one `recv`.
`recv_exact` handles the first case. `FrameDecoder.feed()` buffers bytes, extracts every
complete frame and keeps the partial tail. Tests feed the data one byte at a time and as one
big chunk (`tests/test_protocol.py`).

**5. Why big-endian?**
It is network byte order (`struct` format `!I`), so every CPU reads the header the same way.

**6. What stops a header claiming 4 GB?**
`MAX_FRAME` (4 MB) is checked before anything is allocated. Compressed frames are inflated with
a 32 MB cap, so a tiny "zip bomb" frame is rejected too (test
`test_large_frames_are_compressed_and_bombs_rejected`).

**7. Why compress, and why only big frames?**
Snapshots and history are repetitive JSON. zlib shrinks a 2,000-object snapshot from 482 KB to
70 KB (−85%). Small messages such as strokes and cursors are left alone: compressing them costs
CPU and saves almost nothing. The top bit of the length field says "compressed".

**8. Why does the server bind 0.0.0.0?**
To accept connections on every interface: Wi-Fi, Ethernet, a hotspot and localhost. Binding
127.0.0.1 would refuse other machines. The server prints its LAN addresses at start-up.

**9. How do you find the LAN IP without internet?**
`connect()` a UDP socket to a private address. No packet is sent; the OS just chooses the
outgoing interface, and `getsockname()` reads its address (`common/discovery.py`).

**10. How does discovery work?**
The client broadcasts a small UDP `WB_DISCOVER` packet to port 5051 (on the limited broadcast
address and on each subnet's directed broadcast). Every server replies with unicast `WB_HERE`:
its name, TCP port, protocol version, whether it uses TLS, and its rooms. After that everything
uses TCP.

**11. TCP_NODELAY?**
It turns off Nagle's algorithm, which holds back small segments in order to merge them. Drawing
messages are small and latency-sensitive, so we send immediately and do our own batching
(stroke previews at 20 Hz).

---

## B. Protocol and consistency

**12. Message format?**
JSON objects with a `type`. Board operations carry a client-generated `op_id`, used for
acknowledgement and de-duplication. The server adds `seq`, `user` and `ts` before broadcasting.
Every object is a *stroke* with a `tool` (pen, shapes, text, sticky, image, node, connector), so
the server validates one format with whitelisted fields (`sanitize_stroke`, `sanitize_edit`).

**13. How do all clients end up with the same board?**
The server is the single sequencer. Under the room lock it does `seq += 1`, applies the op, logs
it, and queues the same message to everyone. Every client applies ops strictly in `seq` order
with the same deterministic `BoardModel.apply()` (`common/model.py`). Same start, same ops, same
order means the same board. Tests compare board fingerprints across 5–10 clients.

**14. Two people move or recolour the same sticky note at the same moment. What happens?**
Both ops reach the server, which puts them in some order, for example 41 then 42. Every client
applies 41, then 42, so the later one wins **identically everywhere**. Nothing is overwritten:
moves are offsets and edits are layers, so undoing 42 brings back exactly the result of 41
(test `test_concurrent_edits_converge`).

**15. One team used a CRDT. Why didn't you?**
A CRDT (for example per-field last-writer-wins registers with Lamport clocks) converges without
any central order, which is great for peer-to-peer or offline-first apps. A LAN class always has
a server, and a single sequencer gives the same guarantee with much less machinery and an easy
proof: same log, same function, same board. It also makes undo, history replay and restore
simple, because they are all just replays of one ordered log.

**16. What if a client receives seq 12 before 11?**
`NetClient._ordered` buffers 12 and delivers ops only when the sequence is contiguous. Gaps can
happen across a reconnect. The server's `WELCOME` then sends every op after the client's
`last_seq`.

**17. How do you avoid applying an op twice?**
On the server, every `op_id` is remembered: a re-sent op is not re-applied, and the server
replies `ACK`. On the client, any `seq <= last_seq` is ignored.

**18. What is optimistic (provisional) rendering?**
Your own stroke is drawn immediately. When its sequenced echo comes back, the confirmed version
replaces the provisional drawing. If the server refuses it (for example, the board was locked),
an `ERROR` carrying the `op_id` rolls it back (`board.py: prov`, `on_error`).

**19. How do flowchart connectors stay attached when nodes move?**
A connector stores only `src` and `dst` node ids. Its line is computed every time it is drawn,
from the nodes' current positions, clipped to each node's outline (`common/geometry.py`). If a
node is erased, the connector is hidden; undoing the erase brings both back.

**20. How does the infinite canvas work with a fixed protocol?**
All coordinates are *world* coordinates (clamped to ±1,000,000). Each client has its own view
transform (`screen = origin + world × scale`), so pan and zoom never touch the shared data.
Pages are just fixed world rectangles laid side by side.

---

## C. Concurrency

**21. How is the server concurrent?**
There is an accept thread. Each client gets a reader thread (read, validate, dispatch) and a
writer thread draining its own queue. A housekeeping thread handles grace-period expiry and
autosave. There is a UDP discovery thread, and optionally the dashboard.

**22. Why do you need locks?**
Several reader threads can change the same room at once. Without a lock, two ops could take the
same `seq`, or one could be lost. Each room has one `RLock` around validate → seq → apply → log →
enqueue.

**23. How do you avoid deadlocks?**
There is one lock order (manager lock, then room lock), and no network I/O while holding a lock:
broadcasting only puts messages on queues.

**24. What if one student's laptop is very slow?**
Only its own writer thread blocks on `sendall`. The room and the other clients are unaffected.
If its queue passes 5,000 messages, that client is disconnected; it can reconnect and catch up.

**25. How does the GUI stay responsive?**
Tkinter is single-threaded. Network threads never touch widgets: they put events on a
`queue.Queue`, which the Tk loop drains every 15 ms with `after()`, up to 300 events per tick.

---

## D. Reliability and recovery

**26. How do you detect a dead connection?**
The client sends a `PING` every 3 s and shows the round-trip time. After 10 s with no data, the
connection is declared dead on both sides. This catches Wi-Fi drops that TCP alone would not
notice for minutes. `SO_KEEPALIVE` is on as a second line of defence.

**27. Walk me through a reconnect.**
1. The status bar shows *Reconnecting…* and the client retries with backoff (1, 2, 4, 8 s).
2. Its `HELLO` carries the session token and `last_seq`.
3. The server finds the seat it kept for 30 s, with the same role and colour, and sends only
   the missed ops, or a snapshot if they are no longer in the replay log.
4. The client re-sends its unacknowledged ops with their original `op_id`s; the server
   discards any it already has.

**28. What if they were away longer than the grace period?**
The seat is gone. The client simply joins again under the same name and gets a snapshot, and
its offline edits are still applied (test
`test_rejoin_after_grace_expired_keeps_offline_edits`).

**29. What if the teacher's laptop disconnects?**
The class keeps running. If the teacher really leaves, the longest-present participant becomes
host. The teacher can also hand over explicitly with *Make teacher*.

**30. How is data persisted?**
JSON files. Rooms are autosaved every 30 s and on shutdown, and restored at start-up. Writes are
atomic: write a temporary file, then `os.replace`, so a crash cannot leave half a file. The
teacher can also save named versions on the server, or a board file on their own laptop.

---

## E. Undo, history

**31. How does undo work with many users?**
Each user has their own undo and redo stacks on the server. Undo flips the `active` flag of
*your* last op, and the server broadcasts that as a sequenced `UNDO` op. You never undo someone
else's work. Undoing an erase brings strokes back; undoing a move or edit returns the previous
position or look.

**32. Edge cases?**
* Undoing a stroke that someone already erased has no visible effect, and is skipped.
* Simultaneous undos are serialised by the room lock.
* Any new action clears your redo stack.
* Clearing the board is undoable.

**33. How does version history work?**
The server keeps a base board plus every op since then. When the log passes 6,000 ops, the
oldest 1,000 are folded into the base. The History window rebuilds version *k* by replaying the
first *k* ops with the **same** `apply()` function, using checkpoints every 100 ops so the slider
stays fast. *Restore* sends the old board back as a validated `RESTORE`.

---

## F. Security

**34. How are passwords stored?**
Never in plaintext: PBKDF2-HMAC-SHA256 with 120,000 iterations and a random per-room salt,
compared in constant time (`hmac.compare_digest`). Three wrong guesses in a minute block that IP
for 30 s.

**35. A student edits the client to draw anyway. What stops them?**
Permissions are enforced **on the server** for every message: view-only students, a locked
board, own-work-only mode and teacher-only actions are all checked there. Disabled buttons are
just a convenience; forced attempts get `ERROR FORBIDDEN/LOCKED` (tests in `test_server.py`
and `test_classroom.py`).

**36. What input validation is done?**
* Message types and fields are whitelisted.
* Numbers are bounds-checked, with NaN and booleans rejected.
* Colour, name and id formats are checked, with limits on text length, point counts and list
  sizes.
* A connector must join two existing nodes; images must have been uploaded to that room; an
  edit must leave a valid object.
* Three bad messages disconnect only that client.

**37. Is the traffic encrypted?**
Not by default: it is plaintext JSON, fine on a trusted lab LAN but readable in Wireshark.
`--tls-cert/--tls-key` turns on TLS 1.2+ for both session and file connections. With a
self-signed certificate the traffic is encrypted, but the server is authenticated only if the
certificate is distributed to the clients.

**38. Are private messages private?**
The server sends a `DM` only to the sender and the recipient, never broadcasts it, and replays
it only to those two on reconnect (`test_private_messages_reach_only_the_two_people`). Poll
answers are anonymous to students; only the teacher sees who chose what.

---

## G. Performance, testing, design

**39. What did you measure?**
On one laptop with `tools/benchmark.py`:
* 5 clients at 10 strokes/s each: **2.0 ms** average delivery, 4.1 ms p95.
* 10 clients at the same rate: 4.3 ms average.
* A burst of 1,000 strokes: 2,222 ops/s sequenced and 11,110 deliveries/s.
* Every run ended with identical boards.

**40. What optimisations?**
* Stroke previews streamed in increments at 20 Hz.
* Sub-pixel mouse moves dropped.
* Cursors throttled.
* Each broadcast encoded once.
* Delta sync on reconnect.
* zlib for big frames.
* Separate connections for files.
* Pan and zoom move existing canvas items and redraw crisply after a short pause.

**41. How did you test it?**
110 pytest tests:
* framing, validation and the model;
* 27 GUI tests driving a real teacher window and a real student window;
* fuzzing with malformed messages, a flooding client, and a server restart mid-class;
* real-socket integration: convergence with up to 10 clients, late join, reconnect, offline ops,
  heartbeat, slow clients, roles, lock, kick, own-work-only, TLS, media;
* classroom features: polls, hands, private messages, history, restore, end session.

They pass on Python 3.10, 3.12 and 3.13. The executables were also built with PyInstaller
and run inside a network namespace with no internet.

**41b. What if a student's program floods the server?**
Each connection has a token bucket (300 messages/s, bursts of 600). When it is empty, the
server pauses only *that* client's reader thread, so TCP flow control slows that sender down.
Messages are never dropped, because dropping a board op would silently lose someone's drawing.

**42. Smart shapes: is that AI?**
No. It is plain geometry (`common/shapes.py`):
1. Resample the stroke to 64 points and trim the overshoot.
2. An open stroke is a **line** if every point is close to the chord.
3. A closed stroke is an **ellipse** if its normalised radius stays close to 1.
4. Otherwise, Ramer–Douglas–Peucker finds the corners: 3 corners make a **triangle**, and 4
   right angles make a **rectangle**.

Scribbles, spirals and hearts stay freehand.

**43. What would you improve with more time?**
* `selectors`/asyncio for hundreds of clients.
* Persist the full op log so history survives restarts.
* Certificate pinning for TLS.
* Live audio from the teacher (UDP with a jitter buffer).
* Pixel-level erasing.
* LaTeX/equation objects for maths classes.
