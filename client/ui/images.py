"""
Loading and scaling images for chat thumbnails and images placed on the board.

With Pillow installed every common format works (PNG, JPEG, GIF, BMP, WebP)
and scaling is smooth. Without Pillow, Tk itself still reads PNG and GIF and
scales them by small fractions (zoom/subsample), so it degrades instead of breaking.
"""

import tkinter as tk

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except ImportError:                     # pragma: no cover
    HAVE_PIL = False

TK_NATIVE = (".png", ".gif")


def image_size(path):
    """(width, height) of an image file, or None if it cannot be read here."""
    if HAVE_PIL:
        try:
            with Image.open(path) as im:
                return im.size
        except Exception:
            return None
    if path.lower().endswith(TK_NATIVE):
        try:
            img = tk.PhotoImage(file=path)
            return img.width(), img.height()
        except tk.TclError:
            return None
    return None


def load_scaled(path, max_w, max_h, exact=False):
    """PhotoImage that fits in max_w x max_h (or fills it exactly when exact=True)."""
    max_w, max_h = max(1, int(max_w)), max(1, int(max_h))
    if HAVE_PIL:
        try:
            with Image.open(path) as im:
                im.load()
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA")
                if exact:
                    im = im.resize((max_w, max_h), Image.LANCZOS)
                else:
                    im.thumbnail((max_w, max_h), Image.LANCZOS)
                return ImageTk.PhotoImage(im)
        except Exception:
            return None
    if not path.lower().endswith(TK_NATIVE):
        return None
    try:
        img = tk.PhotoImage(file=path)
    except tk.TclError:
        return None
    # Without Pillow Tk scales only by whole numbers, so approximate the ratio
    # with a fraction a/b: zoom by a, then subsample by b (a, b <= 6).
    ratio = min(max_w / img.width(), max_h / img.height())
    if not exact:
        ratio = min(ratio, 1.0)
    if abs(ratio - 1.0) < 0.02:
        return img
    # best fraction a/b with small a, b (bigger values make zoom() slow)
    a, b = min(((x, y) for x in range(1, 7) for y in range(1, 7)),
               key=lambda xy: (abs(xy[0] / xy[1] - ratio), xy[0]))
    if a > 1:
        img = img.zoom(a, a)
    if b > 1:
        img = img.subsample(b, b)
    return img


class ImageCache:
    """Scaled PhotoImages for board images, keyed by (media_id, w, h)."""

    def __init__(self, media):
        self.media = media
        self.scaled = {}

    def get(self, media_id, w, h):
        key = (media_id, int(w), int(h))
        img = self.scaled.get(key)
        if img is not None:
            return img
        path = self.media.cached_path(media_id)
        if not path:
            return None
        img = load_scaled(path, w, h, exact=True)
        if img is not None:
            self.scaled[key] = img
        return img

    def clear(self):
        self.scaled.clear()
