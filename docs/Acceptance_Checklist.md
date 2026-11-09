# Spec compliance checklist (CN Lab Project: LAN-Based Multi-User Collaborative Whiteboard)

Every requirement of the project statement, in the statement's own order, with where it is
implemented and the automated test that proves it. The whole suite runs with
`python -m pytest tests -q`: 110 tests, passing on Python 3.10, 3.12 and 3.13. GUI tests need a
display.

## Core functional requirements

### 1. Multi-user whiteboard and real-time drawing
| Requirement | Implementation | Verified by |
|---|---|---|
| Freehand strokes, erase content, select colour | Pen / Marker, Eraser (+ undo), 10-colour palette + custom colour, size slider | `test_gui.py::test_02`, `test_06`; `test_07` (recolour) |
| Real-time synchronisation with minimal delay | Each op goes to the server, gets a `seq` and is broadcast at once; live stroke previews at 20 Hz | `test_concurrent_stress_all_canvases_identical`; `tools/benchmark.py`: 2.0 ms avg (5 clients), 4.3 ms (10) |
| Server-side coordination: receive, validate, distribute; concurrent ops | `server/room.py: board_op` validates, sequences and broadcasts under one room lock | `test_concurrent_*`, `test_invalid_edits_are_rejected`, fuzz test |
| Shared canvas: consistent representation | Same `BoardModel.apply()` on server and clients, applied in `seq` order | Fingerprint comparisons in almost every integration test |
| Canvas reset (clear) with permissions | Host-only **Clear board** (undoable) | `test_roles_lock_kick`, `test_undo_clear_restores_everything` |
| Persistence: save and restore | Save/Load on the server, autosave + restore after a restart, board files, version history | `test_save_load_and_autosave_restore`, `test_server_restart_mid_class`, `test_gui.py::test_23`, `test_24` |

### 2. Multi-user session and workspace management
| Requirement | Implementation | Verified by |
|---|---|---|
| Host a session; join via server LAN address + room code | Start window: *Start a class* / *Join a class*, 4-letter room code, optional password, UDP discovery | `test_name_taken_and_bad_room`, `test_udp_discovery`, TLS start-window run |
| Participant management: list, join/leave notifications | "In class" card + People tab; chat notices for join, leave, away, reconnect and role changes | `test_gui.py::test_01`, `test_late_joiner_gets_full_state_and_chat` |
| User identification: username, visual distinction (cursors, colours) | Per-user colour; live named cursors; minimap cursor dots; hover "Node by asha - last edited by ravi"; *Show authors* tags | `test_gui.py::test_07` (`describe`), screenshots |
| Session continuity: join/leave without disruption; newcomers get the current board | `WELCOME` carries snapshot, chat, votes, poll, timer, settings and the presenter's view | `test_late_joiner_gets_full_state_and_chat`, `test_settings_validation_and_late_join` |

### 3. Real-time communication and event handling
| Requirement | Implementation | Verified by |
|---|---|---|
| Operation transmission over sockets | Length-prefixed JSON frames over TCP (`common/protocol.py`) | `test_protocol.py` |
| Event broadcasting | `Room.broadcast` puts frames on each client's writer queue | all integration tests |
| Concurrent editing without lost updates or inconsistent states | Total order + layered MOVE/EDIT ops (nothing is overwritten) | `test_concurrent_moves_are_deterministic`, `test_concurrent_edits_converge` |
| Ordering and reliability: delayed, duplicated, missing updates | `seq` ordering + buffering, `op_id` de-duplication (`ACK`), resend of pending ops | `test_duplicate_op_ids_are_ignored`, `test_ops_sent_while_disconnected_are_delivered_once` |
| Connection management: detect disconnects, timeouts, reconnect | Heartbeat (3 s / 10 s), 30 s seat grace, automatic reconnect with delta | `test_heartbeat_timeout_marks_user_away`, `test_reconnect_receives_only_missed_ops`, `test_gui.py::test_25` |

### 4. Text communication and collaboration
| Requirement | Implementation | Verified by |
|---|---|---|
| Group chat | Chat tab, *Everyone* | `test_gui.py::test_19` |
| Message broadcasting through the server | `CHAT` → server → all participants | `test_late_joiner_gets_full_state_and_chat` |
| Chat history in order with username and timestamp | `HH:MM  name: text`, the last 200 messages kept for late joiners and reconnects | same + `test_gui.py::test_19` |
| (extra) Private messages, files, images, video, voice messages | `DM`; separate TCP connections for files | `test_private_messages_reach_only_the_two_people`, `test_media.py` |

### 5. Whiteboard state management and recovery
| Requirement | Implementation | Verified by |
|---|---|---|
| State representation (strokes and their properties) | `BoardModel`: strokes, order, active ops, covers, moves, edits | `test_model.py` |
| State synchronisation for new clients | Snapshot in `WELCOME` (zlib-compressed above 32 KB) | `test_snapshot_roundtrip_equals_replay`, compression test |
| Recovery after disconnection | Delta (missed ops) or snapshot; rejoin even after the seat expired; offline edits kept | `test_reconnect_*`, `test_rejoin_after_grace_expired_keeps_offline_edits`, `test_server_restart_mid_class` |
| Undo / redo with defined multi-user behaviour | Per-user stacks; undo affects only your own ops | `test_undo_is_per_user`, `test_edit_recolour_resize_text_undo_and_late_join` |

## Deliverables
| Deliverable | Status |
|---|---|
| Server application: standalone executable | `build_windows.bat` / `build_linux.sh` (PyInstaller). Linux build verified; it runs with no network. **Build the Windows .exe on a Windows PC.** |
| Client application: standalone executable | Same scripts. Linux build verified (window opens and joins). **Build on Windows.** |
| Source code with comments | Every module has a docstring explaining its design; key functions are commented |
| Technical Documentation (PDF) | **Still to produce.** The content exists in `README.md` (architecture, message format, socket communication, synchronisation, concurrency, recovery, user guide) |
| Presentation (PPT) | **Still to produce.** Screenshots are in `docs/screenshots/`; the talk outline is in `docs/Speaking_Script.md` |
| Explanation video (each member) | Recorded by the team; see `docs/Speaking_Script.md` and `docs/Viva_Prep.md` |
| Everything in one zip, submitted once | Package the executables, source, PDF, PPT and videos together |

## Evaluation rubric
| Rubric item | How the project meets it |
|---|---|
| Network architecture: client-server, TCP/IP only, no internet | Central server owns sessions, state and distribution; TCP 5050 (+ UDP 5051 discovery only). Verified running in a network namespace with no internet |
| Protocol design: message format, transport choice, framing, delivery | Typed JSON messages, `op_id` + `seq`; TCP justified; 4-byte length framing + zlib flag; ACK, de-duplication, ordering (README §3) |
| User interface: canvas, toolbar, participant list, chat panel, session controls | Pages canvas, tool rail, "In class" list + People tab, Chat tab, header and Class-tab session controls |
| Performance | 2–4 ms average delivery; 11,000 deliveries/s; delta sync; 85% smaller snapshots; throttled cursors; separate file connections |
| Concurrency | Thread per client (reader + writer), one lock per room, no I/O under locks; slow clients isolated; flooding clients throttled |
| Security and access control | PBKDF2 passwords + rate limit, roles enforced on the server, lock, own-work-only, kick/ban, validation, fuzz-tested, optional TLS |
| Platform | Pure Python standard library + Tk: Windows, Linux and macOS |

## Additional features (bonus marks)
The statement lists these examples: collaborative diagram creation, flowcharts, mind maps, sticky
notes, image insertion, presentation mode, role-based editing permissions, version history,
offline editing and exporting the canvas. **All of them are implemented.** In addition:
infinite canvas + minimap, pages and paper styles, smart shapes, laser pointer, quick polls,
raised hands, session timer, dot voting, private messages, media sharing, TLS, and a server
dashboard.
