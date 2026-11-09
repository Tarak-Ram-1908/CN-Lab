"""Saving and loading boards as JSON files on the server machine."""

import json
import os
import re
import time

from common import protocol as P


class Store:
    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, room_code, name):
        safe_room = re.sub(r"[^A-Z0-9]", "", room_code.upper())
        return os.path.join(self.dir, f"{safe_room}__{name}.json")

    def save(self, room, name):
        """Write atomically (temp file + rename) so a crash never leaves half a file."""
        path = self._path(room.code, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(room.export_state(), f, separators=(",", ":"))
        os.replace(tmp, path)
        room.dirty = False
        return path

    def load(self, room_code, name):
        if not P.valid_save_name(name):
            return None
        path = self._path(room_code, name)
        if not os.path.exists(path):
            # A save from another room with this name is also allowed.
            for f in os.listdir(self.dir):
                if f.endswith(f"__{name}.json"):
                    path = os.path.join(self.dir, f)
                    break
            else:
                return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("format") != 1 or "board" not in data:
            return None
        return data

    def list(self, room_code):
        """Saves for this room first (newest first), then other rooms' saves."""
        out = []
        for f in os.listdir(self.dir):
            m = re.match(r"^([A-Z0-9]+)__([A-Za-z0-9_\-]+)\.json$", f)
            if not m:
                continue
            mtime = os.path.getmtime(os.path.join(self.dir, f))
            out.append({"room": m.group(1), "name": m.group(2),
                        "saved": time.strftime("%d %b %H:%M", time.localtime(mtime)),
                        "mtime": mtime})
        out.sort(key=lambda s: (s["room"] != room_code, -s["mtime"]))
        return out

    def autosave(self, room):
        return self.save(room, "autosave")

    def delete_autosave(self, room_code):
        try:
            os.remove(self._path(room_code, "autosave"))
        except OSError:
            pass

    def restore_rooms(self, manager):
        """On startup, recreate rooms from their autosaves (seats are not restored)."""
        from .room import Room
        from common.model import BoardModel
        restored = []
        for f in os.listdir(self.dir):
            m = re.match(r"^([A-Z0-9]{4})__autosave\.json$", f)
            if not m:
                continue
            try:
                with open(os.path.join(self.dir, f), encoding="utf-8") as fh:
                    data = json.load(fh)
                room = Room(m.group(1), log=manager.log)
                room.model = BoardModel.from_dict(data["board"])
                room.seq = data.get("seq", room.model.seq)
                room.model.seq = room.seq
                for c in data.get("chat", []):
                    room.chat.append(c)
                room.chat_id = data.get("chat_id", 0)
                room.votes = {sid: list(v) for sid, v in data.get("votes", {}).items()
                              if sid in room.model.strokes}
                room.settings.update({k: v for k, v in data.get("settings", {}).items()
                                      if k in room.settings})
                room.hist_base = BoardModel.from_dict(data["board"])
                if manager.media_store is not None:
                    room.media = manager.media_store.load_index(room.code)
                with manager.lock:
                    manager.rooms[room.code] = room
                restored.append(room.code)
            except (OSError, ValueError, KeyError):
                continue
        return restored
