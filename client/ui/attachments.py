"""
Attachment cards shown inside the chat: images, voice messages, audio,
video and other files.

A card first shows what the file is (name, size, length). Small images and
voice messages download automatically; bigger files download when the user
clicks Play/Open. Video and non-WAV audio open in the computer's own offline
player; voice messages play inside the app.
"""

import os
import tkinter as tk
from tkinter import filedialog, messagebox

from client.media import human_size, open_with_system
from .images import load_scaled

AUTO_DOWNLOAD_IMAGE = 15 * 1024 * 1024
CARD_BG = "#f1f3f6"
KIND_LABEL = {"image": "Image", "video": "Video", "audio": "Audio",
              "voice": "Voice message", "file": "File"}
KIND_COLOR = {"image": "#2a9d8f", "video": "#d62f8f", "audio": "#6a4c93",
              "voice": "#1d70b8", "file": "#5f6b7a"}


def fmt_duration(sec):
    sec = int(round(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


class AttachmentCard(tk.Frame):
    def __init__(self, master, app, meta, sender):
        super().__init__(master, bg=CARD_BG, padx=8, pady=6, highlightthickness=1,
                         highlightbackground="#d5dae1")
        self.app = app
        self.meta = meta
        self.kind = meta.get("kind", "file")
        self.path = app.media.cached_path(meta)
        self.pending = None            # action to run once the download finishes
        self.thumb = None

        head = tk.Frame(self, bg=CARD_BG)
        head.pack(fill="x")
        tk.Label(head, text=KIND_LABEL.get(self.kind, "File"), bg=KIND_COLOR[self.kind],
                 fg="white", font=("Helvetica", 8, "bold"), padx=5).pack(side="left")
        info = human_size(meta.get("size", 0))
        if meta.get("duration"):
            info = fmt_duration(meta["duration"]) + "  |  " + info
        tk.Label(head, text=info, bg=CARD_BG, fg="#5f6b7a",
                 font=("Helvetica", 8)).pack(side="left", padx=6)
        if self.kind != "voice":
            tk.Label(self, text=meta.get("name", "file"), bg=CARD_BG, fg="#1f2328",
                     font=("Helvetica", 9), wraplength=230, justify="left",
                     anchor="w").pack(fill="x", pady=(3, 0))

        self.preview = tk.Label(self, bg=CARD_BG, cursor="hand2")
        self.preview.bind("<Button-1>", lambda e: self.open())
        self.status = tk.Label(self, text="", bg=CARD_BG, fg="#5f6b7a", font=("Helvetica", 8))
        self.status.pack(anchor="w")

        row = tk.Frame(self, bg=CARD_BG)
        row.pack(fill="x", pady=(3, 0))
        self.main_btn = self._btn(row, self._main_label(), self.main_action)
        self.main_btn.pack(side="left")
        if self.kind == "image":
            self._btn(row, "Put on board", self.to_board).pack(side="left", padx=(4, 0))
        self._btn(row, "Save as...", self.save_as).pack(side="left", padx=(4, 0))

        if self.path:
            self.ready(self.path)
        elif (self.kind == "voice" or
              (self.kind == "image" and meta.get("size", 0) <= AUTO_DOWNLOAD_IMAGE)):
            self.download()

    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, font=("Helvetica", 9), relief="groove",
                         bg="#ffffff", activebackground="#e6eaf0", padx=6, pady=1, bd=1,
                         cursor="hand2")

    def _main_label(self):
        if self.kind in ("voice", "audio", "video"):
            return "Play"
        return "Open"

    # ------------------------------------------------------------------ download
    def download(self, then=None):
        if then:
            self.pending = then
        self.status.configure(text="Downloading...")
        self.app.media.ensure(self.meta, quiet=self.kind in ("voice", "image"))

    def progress(self, done, size):
        pct = int(done * 100 / max(size, 1))
        self.status.configure(text=f"Downloading {pct}%  ({human_size(done)} of {human_size(size)})")

    def failed(self, message):
        self.status.configure(text=f"Failed: {message}", fg="#b42318")

    def ready(self, path):
        self.path = path
        self.status.configure(text="", fg="#5f6b7a")
        if self.kind == "image" and self.thumb is None:
            self.thumb = load_scaled(path, 230, 170)
            if self.thumb is not None:
                self.preview.configure(image=self.thumb)
                self.preview.pack(anchor="w", pady=(4, 0), before=self.status)
            else:
                self.status.configure(text="(preview needs Pillow - click Open)")
        if self.pending:
            action, self.pending = self.pending, None
            action()

    # ------------------------------------------------------------------ actions
    def main_action(self):
        if self.kind in ("voice", "audio", "video"):
            self.play()
        else:
            self.open()

    def play(self):
        if not self.path:
            return self.download(then=self.play)
        player = self.app.player
        if player.playing == self.path:
            player.stop()
            self.main_btn.configure(text="Play")
            return
        how = player.play(self.path, on_end=lambda: self.app.net.events.put(
            {"type": "_PLAY_END", "path": self.path}))
        if how != "system":
            self.app.reset_play_buttons()
            self.main_btn.configure(text="Stop")
        else:
            self.status.configure(text="Opened in your media player")

    def play_ended(self):
        self.main_btn.configure(text="Play")

    def open(self):
        if not self.path:
            return self.download(then=self.open)
        try:
            open_with_system(self.path)
        except OSError as exc:
            messagebox.showerror("Open file", str(exc), parent=self.app.root)

    def save_as(self):
        if not self.path:
            return self.download(then=self.save_as)
        name = self.meta.get("name", "file")
        dest = filedialog.asksaveasfilename(parent=self.app.root, initialfile=name,
                                            defaultextension=os.path.splitext(name)[1])
        if dest:
            import shutil
            shutil.copyfile(self.path, dest)
            self.app.flash(f"Saved {dest}")

    def to_board(self):
        if not self.path:
            return self.download(then=self.to_board)
        self.app.place_board_image(self.meta["media_id"], self.path, self.meta.get("dims"))
