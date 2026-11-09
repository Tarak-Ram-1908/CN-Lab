"""
End-to-end GUI tests: a real teacher window and a real student window (Tkinter)
connected to a real server over TCP, driven through mouse events and the same
methods the buttons call. Every feature is checked from the *other* user's side.

Skipped automatically when Tkinter or a display is not available
(on a headless Linux machine run:  xvfb-run python -m pytest tests/test_gui.py).
"""
import math
import os
import tempfile
import time
import xml.dom.minidom

import pytest

tk = pytest.importorskip("tkinter")
try:
    _probe = tk.Tk()
    _probe.destroy()
except tk.TclError:
    pytest.skip("no display available", allow_module_level=True)

from tkinter import filedialog, messagebox, simpledialog  # noqa: E402

from helpers import Bot, P, start_server                    # noqa: E402
from client.net import NetClient                            # noqa: E402
from client.ui import export                                # noqa: E402
from client.ui.app import WhiteboardApp                     # noqa: E402
from client.ui.theme import setup_style                     # noqa: E402
from common import geometry as G                            # noqa: E402
from common.model import BoardModel                         # noqa: E402

TMP = tempfile.mkdtemp(prefix="classboard-gui-")
DIALOG = {}                                   # answers for patched dialogs


@pytest.fixture(scope="module")
def env():
    saved = {}
    patches = {(messagebox, "askyesno"): lambda *a, **k: True,
               (messagebox, "showinfo"): lambda *a, **k: None,
               (messagebox, "showwarning"): lambda *a, **k: None,
               (messagebox, "showerror"): lambda *a, **k: DIALOG.setdefault("errors", [])
               .append(a),
               (filedialog, "asksaveasfilename"): lambda *a, **k: DIALOG.pop("save", ""),
               (filedialog, "askopenfilename"): lambda *a, **k: DIALOG.pop("open", ""),
               (simpledialog, "askstring"): lambda *a, **k: DIALOG.pop("string"),
               (simpledialog, "askfloat"): lambda *a, **k: DIALOG.pop("float")}
    for (mod, name), fn in patches.items():
        saved[(mod, name)] = getattr(mod, name)
        setattr(mod, name, fn)
    srv = start_server()
    root = tk.Tk()
    setup_style(root)
    root.withdraw()

    def make(name, create=False, room=None, geo="+0+0"):
        win = tk.Toplevel(root)
        win.geometry(f"1300x820{geo}")
        net = NetClient("127.0.0.1", srv.port, name, room, "", create=create)
        w = net.connect()
        app = WhiteboardApp(win, net, w)
        app.media.dir = os.path.join(TMP, f"media-{name}")
        return app

    t = make("teacher", create=True)
    s = make("stu", room=t.room, geo="+1320+0")
    e = {"root": root, "t": t, "s": s, "srv": srv}
    wait(e, lambda: t.board.view_ready and s.board.view_ready and len(t.participants) == 2)
    yield e
    for app in (t, s):
        try:
            app.net.close()
        except Exception:
            pass
    try:
        root.destroy()
    except tk.TclError:
        pass
    srv.stop()
    for (mod, name), fn in saved.items():
        setattr(mod, name, fn)


# ------------------------------------------------------------------ helpers
def wait(e, cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        e["root"].update()
        try:
            if cond():
                return True
        except (KeyError, IndexError, AttributeError):
            pass
        time.sleep(0.01)
    return cond()


def pump(e, sec=0.3):
    wait(e, lambda: False, sec)


def scr(app, x, y):
    return app.board.to_screen([x, y])


def drag(app, pts, state=0):
    b = app.board
    x, y = pts[0]
    b.event_generate("<ButtonPress-1>", x=int(x), y=int(y), state=state)
    for x, y in pts[1:]:
        b.event_generate("<B1-Motion>", x=int(x), y=int(y), state=0x100 | state)
    b.event_generate("<ButtonRelease-1>", x=int(x), y=int(y), state=0x100 | state)


def wdrag(app, a, b, n=12, state=0):
    """Drag in world coordinates from a to b."""
    pts = [scr(app, a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
           for i in range(n + 1)]
    drag(app, pts, state)


def click(app, x, y):
    drag(app, [scr(app, x, y)])


def tools(app):
    m = app.model
    return sorted(m.strokes[s]["tool"] for s in m.visible_ids())


def new_ids(app, before):
    return [s for s in app.model.visible_ids() if s not in before]


def type_in(app, text, how="commit"):
    ed = app.board.editor
    assert ed is not None, "the inline editor did not open"
    ed.text.delete("1.0", "end")
    ed.text.insert("1.0", text)
    ed.commit(how)


def same(e):
    t, s = e["t"], e["s"]
    return wait(e, lambda: t.model.seq == s.model.seq and not t.net.pending and
                not s.net.pending) and t.model.fingerprint() == s.model.fingerprint()


def by_text(app, text):
    return next(s for s in app.model.visible_ids() if app.model.placed(s).get("text") == text)


# ------------------------------------------------------------------ tests (run in order)
def test_01_join_roles_and_participants(env):
    t, s = env["t"], env["s"]
    assert t.role == P.ROLE_HOST and s.role == P.ROLE_EDITOR
    assert t.role_chip.cget("text") == "Teacher" and s.role_chip.cget("text") == "Student"
    assert "teacher" in t.me_label.cget("text")
    assert {p["username"] for p in s.participants} == {"teacher", "stu"}
    assert t.people.exists("stu") and s.people.exists("teacher")
    assert t.present_btn.winfo_ismapped() and not s.present_btn.winfo_ismapped()
    assert s.hand_btn.winfo_ismapped() and not t.hand_btn.winfo_ismapped()


def test_02_freehand_and_smart_shapes(env):
    t, s = env["t"], env["s"]
    t.set_tool("pen")
    t.smart_shapes.set(False)
    wdrag(t, (100, 100), (300, 160), n=20)                           # freehand
    t.smart_shapes.set(True)
    pts = [scr(t, 250 + 90 * math.cos(2 * math.pi * i / 50),
               400 + 60 * math.sin(2 * math.pi * i / 50)) for i in range(56)]
    drag(t, pts)                                                     # rough circle
    assert wait(env, lambda: tools(s) == ["ellipse", "pen"])
    assert wait(env, lambda: not t.board.prov)                       # provisional replaced
    assert same(env)


def test_03_every_shape_kind(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    before = list(s.model.visible_ids())
    for i, kind in enumerate(("rect", "ellipse", "triangle", "diamond", "line", "arrow")):
        t.shape_kind.set(kind)
        t.set_tool("shape")
        wdrag(t, (500 + i * 130, 80), (600 + i * 130, 180))
    assert wait(env, lambda: len(new_ids(s, before)) == 6)
    got = sorted(s.model.strokes[x]["tool"] for x in new_ids(s, before))
    assert got == ["arrow", "ellipse", "line", "polygon", "polygon", "rect"]
    polys = [s.model.strokes[x]["points"] for x in new_ids(s, before)
             if s.model.strokes[x]["tool"] == "polygon"]
    assert sorted(len(p) for p in polys) == [6, 8]                  # triangle, diamond


def test_04_text_and_sticky_notes_inline(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.set_tool("text")
    click(t, 100, 600)
    type_in(t, "Newton's laws")
    t.set_tool("sticky")
    click(t, 400, 600)
    type_in(t, "Idea one")
    click(t, 700, 600)
    type_in(t, "Idea two")
    assert wait(env, lambda: {"Newton's laws", "Idea one", "Idea two"} <=
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})


def test_05_edit_text_and_empty_text_erases(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.set_tool("select")
    sid = by_text(t, "Idea two")
    t.board.edit_object(sid)
    type_in(t, "Idea two (edited)")
    assert wait(env, lambda: s.model.placed(sid).get("text") == "Idea two (edited)")
    t.board.edit_object(by_text(t, "Newton's laws"))
    type_in(t, "")                                                   # emptied -> erased
    assert wait(env, lambda: "Newton's laws" not in
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})
    t.undo()                                                         # undo brings it back
    assert wait(env, lambda: "Newton's laws" in
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})


def test_06_eraser_undo_redo(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    n = len(s.model.visible_ids())
    t.set_tool("eraser")
    wdrag(t, (200, 90), (200, 180), n=20)                            # across the freehand line
    assert wait(env, lambda: len(s.model.visible_ids()) == n - 1)
    t.undo()
    assert wait(env, lambda: len(s.model.visible_ids()) == n)
    t.redo()
    assert wait(env, lambda: len(s.model.visible_ids()) == n - 1)
    t.undo()
    assert wait(env, lambda: len(s.model.visible_ids()) == n)


def test_07_select_move_resize_restyle(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.set_tool("select")
    rect = next(x for x in t.model.visible_ids() if t.model.strokes[x]["tool"] == "rect")
    x0, y0, x1, y1 = G.norm_box(t.model.placed(rect)["points"])
    click(t, x0 + 1, (y0 + y1) / 2)                                  # click its edge
    pump(env, 0.05)
    assert t.board.selection == [rect]
    wdrag(t, (x0 + 1, (y0 + y1) / 2), (x0 + 41, (y0 + y1) / 2 + 300))   # move (quick 2nd press)
    assert wait(env, lambda: abs(s.model.placed(rect)["points"][0] - (x0 + 40)) < 2)
    assert wait(env, lambda: abs(s.model.placed(rect)["points"][0] - (x0 + 40)) < 2)
    pump(env, 0.1)
    hx0, hy0, hx1, hy1 = t.board.coords(t.board.find_withtag("handle")[0])
    hx, hy = (hx0 + hx1) / 2, (hy0 + hy1) / 2
    drag(t, [(hx + i * 5, hy + i * 5) for i in range(11)])           # resize bigger
    assert wait(env, lambda: G.norm_box(s.model.placed(rect)["points"])[2] -
                G.norm_box(s.model.placed(rect)["points"])[0] > (x1 - x0) + 30)
    t.set_color("#e11d48")                                            # restyle selection
    assert wait(env, lambda: s.model.placed(rect)["color"] == "#e11d48")
    assert s.board.describe(rect).startswith("Rectangle by teacher")


def test_08_band_select_duplicate_delete(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.set_tool("select")
    t.board.set_selection([])
    wdrag(t, (380, 560), (1000, 760))                                # around both notes
    pump(env, 0.05)
    sel = sorted(t.model.strokes[x]["tool"] for x in t.board.selection)
    assert sel == ["sticky", "sticky"]
    n = len(s.model.visible_ids())
    t.board.duplicate_selection()
    assert wait(env, lambda: len(s.model.visible_ids()) == n + 2)
    t.board.delete_selection()                                       # the originals
    assert wait(env, lambda: len(s.model.visible_ids()) == n)
    assert same(env)


def test_09_flowchart_connectors_labels_and_mind_map(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.set_tool("node")
    click(t, 1200, 500)
    type_in(t, "Start")
    click(t, 1600, 500)
    type_in(t, "End")
    assert wait(env, lambda: tools(s).count("node") == 2)
    assert same(env)                                                 # teacher confirmed too
    a, b = by_text(t, "Start"), by_text(t, "End")
    pa, pb = t.model.placed(a)["points"], t.model.placed(b)["points"]
    t.set_tool("connect")
    wdrag(t, (pa[0] + 50, pa[1] + 30), (pb[0] + 50, pb[1] + 30))
    assert wait(env, lambda: "connector" in tools(s))
    assert same(env)                                                 # teacher confirmed too
    conn = next(x for x in s.model.visible_ids() if s.model.strokes[x]["tool"] == "connector")
    assert (s.model.strokes[conn]["src"], s.model.strokes[conn]["dst"]) == (a, b)
    t.board.edit_object(conn)                                        # label it
    type_in(t, "Yes")
    assert wait(env, lambda: s.model.placed(conn).get("label") == "Yes")
    # mind map: Tab = child (and Tab again = grandchild), Enter = sibling
    t.set_tool("select")
    t.board.set_selection([a])
    assert t.board.mindmap_add()
    type_in(t, "Child", how="tab")
    assert wait(env, lambda: t.board.editor is not None and "Child" in
                {t.model.placed(x).get("text") for x in t.model.visible_ids()})
    type_in(t, "Grandchild")
    assert wait(env, lambda: "Grandchild" in
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})
    t.board.set_selection([by_text(t, "Child")])
    assert t.board.mindmap_add(sibling=True)
    type_in(t, "Sibling")
    assert wait(env, lambda: tools(s).count("node") == 5 and tools(s).count("connector") == 4)
    sib = by_text(s, "Sibling")
    parent = [s.model.strokes[c]["src"] for c in s.model.visible_ids()
              if s.model.strokes[c]["tool"] == "connector" and s.model.strokes[c]["dst"] == sib]
    assert parent == [a]                                             # same parent as Child
    # connectors follow a moved node
    before = G.connector_geometry(s.model, s.model.placed(conn))[0]
    t.board.set_selection([b])
    pb = t.model.placed(b)["points"]
    wdrag(t, (pb[0] + 50, pb[1] + 30), (pb[0] + 50, pb[1] + 230))
    assert wait(env, lambda: G.connector_geometry(s.model, s.model.placed(conn))[0] != before)
    assert same(env)


def test_10_laser_pointer_is_seen_but_not_stored(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    seq = s.model.seq
    t.set_tool("laser")
    wdrag(t, (100, 800), (600, 820), n=25)
    assert wait(env, lambda: "teacher" in s.board.lasers)
    assert s.model.seq == seq
    assert wait(env, lambda: "teacher" not in s.board.lasers, timeout=3)   # it fades


def test_11_votes_badges_and_results(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    note = by_text(s, "Idea one")
    s.set_tool("vote")
    x0, y0, x1, y1 = s.model.bounds(note)
    click(s, x0 + 20, y0 + 20)
    assert wait(env, lambda: t.votes.get(note) == ["stu"])
    assert any("badge" in t.board.gettags(i) for i in t.board.items[note])
    t.show_results()
    assert wait(env, lambda: t.results_tree.get_children() == (note,))
    t.results_win.destroy()


def test_12_pages_and_paper(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.add_page()
    assert wait(env, lambda: s.settings["pages"] == 2 and t.board.current_page() == 1)
    assert "Page 2 / 2" in t.page_text.get()
    t.paper_var.set("Ruled (notebook)")
    t._paper_changed()
    assert wait(env, lambda: s.settings["paper"] == "ruled")
    t.go_page(0)
    assert t.board.current_page() == 0


def test_13_present_follow_me(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.toggle_present()
    assert wait(env, lambda: s.following() and s.banner.winfo_ismapped())
    t.go_page(1)
    tx, ty = t.board.view_center()
    assert wait(env, lambda: math.hypot(s.board.view_center()[0] - tx,
                                        s.board.view_center()[1] - ty) < 5)
    s.go_page(0)                                                    # students cannot wander
    assert s.board.current_page() == 1
    t.toggle_present()
    assert wait(env, lambda: not s.following() and not s.banner.winfo_ismapped())
    t.go_page(0)
    s.go_page(0)


def test_14_lock_view_only_and_forced_draw_refused(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.students_draw.set(False)
    t.toggle_lock()
    assert wait(env, lambda: s.locked and not s.can_edit())
    assert s.tool.get() in ("hand", "vote", "select")
    s.set_tool("pen")
    assert s.tool.get() != "pen"                                    # UI refuses
    n = len(t.model.visible_ids())
    s.net.send_board_op(P.STROKE, stroke={"id": s.net.new_stroke_id(), "tool": "pen",
                                          "color": "#000000", "width": 3, "points": [1, 1, 9, 9]})
    assert wait(env, lambda: "read-only" in s.status_text.get())    # server refuses too
    assert len(t.model.visible_ids()) == n
    t.students_draw.set(True)
    t.toggle_lock()
    assert wait(env, lambda: s.can_edit())
    t._admin("set_role", P.ROLE_VIEWER, "stu")
    assert wait(env, lambda: s.role == P.ROLE_VIEWER and not s.can_edit())
    assert s.role_chip.cget("text") == "Student (view only)"
    t._admin("set_role", P.ROLE_EDITOR, "stu")
    assert wait(env, lambda: s.role == P.ROLE_EDITOR and s.can_edit())


def test_15_own_work_only(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.own_only.set(True)
    t._own_changed()
    assert wait(env, lambda: s.settings["own_only"])
    rect = next(x for x in s.model.visible_ids() if s.model.strokes[x]["tool"] == "rect")
    before = list(s.model.placed(rect)["points"])
    bx = s.board.bbox(f"s:{rect}")
    s.set_tool("select")
    x0, y0, x1, y1 = G.norm_box(before)
    wdrag(s, (x0 + 1, (y0 + y1) / 2), (x0 + 200, (y0 + y1) / 2))
    assert wait(env, lambda: "Own-work-only" in s.status_text.get())
    pump(env, 0.2)
    assert s.model.placed(rect)["points"] == before
    assert s.board.bbox(f"s:{rect}") == bx                          # drawing rolled back
    t.own_only.set(False)
    t._own_changed()
    assert wait(env, lambda: not s.settings["own_only"])


def test_16_timer(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    DIALOG["float"] = 2.0
    t.timer_start_dialog()
    assert wait(env, lambda: s.timer["running"] and s.timer["duration"] == 120)
    assert s.timer_label.cget("text") in ("02:00", "01:59")
    t.timer_pause_resume()
    assert wait(env, lambda: not s.timer["running"] and s.timer_info.cget("text") == "paused")
    t._timer("reset")
    assert wait(env, lambda: s.timer_info.cget("text") == "ready")


def test_17_quick_poll(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.net.send({"type": P.POLL, "action": "start", "question": "Is TCP reliable?",
                "options": ["Yes", "No"]})
    assert wait(env, lambda: s.poll and s.poll["open"])
    s._answer(0)
    assert wait(env, lambda: t.poll["counts"] == [1, 0] and t.poll["answers"] == {"stu": 0})
    assert "answers" not in s.poll                                  # anonymous for students
    t.net.send({"type": P.POLL, "action": "end"})
    assert wait(env, lambda: not s.poll["open"])
    t.net.send({"type": P.POLL, "action": "clear"})
    assert wait(env, lambda: s.poll is None)


def test_18_raise_hand(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    s.toggle_hand()
    assert wait(env, lambda: any(p["username"] == "stu" and p.get("hand")
                                 for p in t.participants))
    assert len(t.hands_body.winfo_children()) == 1 and s.hand_btn.cget("text") == "Lower hand"
    t.net.send({"type": P.HAND, "target": "stu", "up": False})
    assert wait(env, lambda: s.hand_btn.cget("text") == "Raise hand")


def test_19_group_chat_and_private_messages(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    spy = Bot(env["srv"].port, "spy", room=t.room)
    s.chat_tabs.select(0)
    s.chat_entry.insert(0, "Hello class")
    s.send_chat()
    assert wait(env, lambda: "stu: Hello class" in t.chat.get("1.0", "end"))
    s.open_dm("teacher")
    s.chat_entry.insert(0, "Can you repeat that?")
    s.send_chat()
    assert wait(env, lambda: "stu" in t.dm_tabs and
                "Can you repeat that?" in t.dm_tabs["stu"].get("1.0", "end"))
    assert "Can you repeat that?" not in t.chat.get("1.0", "end")
    spy.pump(0.3)
    assert not [m for m in spy.other if m["type"] == P.DM]
    spy.close()
    s._close_dm()


def test_20_images_and_chat_attachments(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    from PIL import Image
    path = os.path.join(TMP, "diagram.png")
    Image.new("RGB", (320, 200), "#2563eb").save(path)
    t._upload_board_image(path)
    assert wait(env, lambda: "image" in tools(s), timeout=10)
    img = next(x for x in s.model.visible_ids() if s.model.strokes[x]["tool"] == "image")
    mid = s.model.strokes[img]["media_id"]
    assert wait(env, lambda: s.media.cached_path(mid), timeout=10)  # student downloaded it
    t.media.upload(path, kind="image", caption="today's diagram", dims=(320, 200))
    assert wait(env, lambda: any(c for c in s.cards.values()), timeout=10)


def test_21_paste_text(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    env["root"].clipboard_clear()
    env["root"].clipboard_append("Homework: page 42")
    t.paste()
    assert wait(env, lambda: "Homework: page 42" in
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})


def test_22_zoom_keeps_an_open_text_editor(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.set_tool("text")
    click(t, 300, 820)
    t.board.zoom_at(1.4)
    pump(env, 0.3)                                                  # crisp redraw happened
    assert t.board.editor is not None and t.board.editor.text.winfo_ismapped()
    type_in(t, "still here")
    assert wait(env, lambda: "still here" in
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})
    t.board.go_page(0)


def test_23_exports_and_board_files(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    page, pdf, svg = (os.path.join(TMP, f) for f in ("p.png", "n.pdf", "b.svg"))
    t.export_to("page", page)
    t.export_to("pdf", pdf)
    t.export_to("svg", svg)
    from PIL import Image
    assert Image.open(page).size == (2000, 1125)
    assert open(pdf, "rb").read(5) == b"%PDF-"
    xml.dom.minidom.parse(svg)
    board = os.path.join(TMP, "lesson.board.json")
    DIALOG["save"] = board
    t.save_board_file()
    saved = export.load_board_file(board)
    assert len(saved["strokes"]) == len(t.model.visible_ids())
    fp = t.model.fingerprint()
    t.clear_board()
    assert wait(env, lambda: not s.model.visible_ids())
    DIALOG["open"] = board
    t.open_board_file()                                             # restore for everyone
    assert wait(env, lambda: len(s.model.visible_ids()) == len(saved["strokes"]))
    assert s.model.fingerprint() == fp                              # identical board back


def test_24_version_history_and_restore(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t.set_tool("sticky")
    click(t, 1000, 300)
    type_in(t, "added after restore")
    assert wait(env, lambda: "added after restore" in
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})
    t.show_history()
    h = t.history_win
    assert wait(env, lambda: h.base is not None and h.ops)
    h.show(0)
    old = h.shim.model
    assert "added after restore" not in {old.placed(x).get("text") for x in old.visible_ids()}
    h.restore()
    assert wait(env, lambda: "added after restore" not in
                {s.model.placed(x).get("text") for x in s.model.visible_ids()})
    assert s.model.fingerprint() == old.fingerprint()


def test_25_reconnect_with_offline_drawing(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    s.net.drop_connection()
    assert wait(env, lambda: not s.connected or s.net.generation > 1, timeout=3)
    sid = s.net.new_stroke_id()
    s.net.send_board_op(P.STROKE, stroke={"id": sid, "tool": "pen", "color": "#16a34a",
                                          "width": 3, "points": [50, 850, 300, 870]})
    assert wait(env, lambda: s.connected and t.model.is_visible(sid), timeout=10)
    assert "Reconnected" in s.chat.get("1.0", "end")
    assert same(env)


def test_26_hand_over_teacher_role(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    t._admin("set_role", P.ROLE_HOST, "stu")
    assert wait(env, lambda: s.is_teacher() and not t.is_teacher())
    assert s.present_btn.winfo_ismapped() and not t.present_btn.winfo_ismapped()
    assert "Student" in t.role_chip.cget("text")
    s._admin("set_role", P.ROLE_HOST, "teacher")                    # and back
    assert wait(env, lambda: t.is_teacher() and not s.is_teacher())


def test_27_remove_student_then_end_session(env):
    t, s = env["t"], env["s"]
    same(env)                                                        # both sides settled
    other = Bot(env["srv"].port, "late", room=t.room)
    assert wait(env, lambda: t.people.exists("late"))
    t._admin("kick", target="late")
    assert wait(env, lambda: not t.people.exists("late"))
    other.pump(0.3)
    assert any(m["type"] == P.KICKED for m in other.other) or other.status == "closed"
    other.close()
    t.end_session()
    assert wait(env, lambda: env["srv"].rooms.get(t.room) is None)
    # both windows close (after offering to save a copy of the board)
    assert wait(env, lambda: not _alive(s) and not _alive(t))


def _alive(app):
    try:
        return bool(app.root.winfo_exists())
    except tk.TclError:
        return False
