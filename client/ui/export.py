"""
Exporting the board.

* PNG  - one page, or the whole board cropped to its content (Pillow).
* PDF  - every page of the lesson, one PDF page each: the "class notes" a
         student can keep (Pillow).
* SVG  - the whole board as a vector drawing (no extra packages needed).
* JSON - a board file that a teacher can open again later (Restore).
* EPS  - fallback without Pillow (Tk's own PostScript export of the view).
"""

import json
import math
import time
from xml.sax.saxutils import escape

from common import geometry as G
from common import protocol as P

STICKY_COLOR = "#fff59d"
BOARD_FILE_FORMAT = "classboard-board"


def text_font_size(width):
    """Text objects reuse 'width' as the size setting (1..20 -> 17..74 world px)."""
    return int(14 + width * 3)


try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:            # pragma: no cover - depends on the machine
    HAVE_PIL = False

MAX_EXPORT_PX = 6000
PAD = 40
PAPER_LINE = (227, 232, 238)


def _font(size):
    try:
        return ImageFont.load_default(size=size)       # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def content_box(model):
    """World rectangle covering every visible object (the canvas is infinite)."""
    boxes = [model.bounds(sid) for sid in model.visible_ids()]
    if not boxes:
        return 0, 0, P.CANVAS_W, P.CANVAS_H
    return (min(b[0] for b in boxes) - PAD, min(b[1] for b in boxes) - PAD,
            max(b[2] for b in boxes) + PAD, max(b[3] for b in boxes) + PAD)


# ====================================================================== raster (Pillow)
def render_image(model, box, scale=1.0, media_path=None, votes=None, paper=None, pages=0):
    """Pillow image of the world rectangle `box` = (x0, y0, x1, y1)."""
    bx0, by0, bx1, by1 = box
    scale = min(scale, MAX_EXPORT_PX / max(bx1 - bx0, by1 - by0, 1))
    w, h = max(1, int((bx1 - bx0) * scale)), max(1, int((by1 - by0) * scale))
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)

    def tr(x, y):
        return (x - bx0) * scale, (y - by0) * scale

    if paper and paper != "plain":
        for i in range(max(pages, 1)):
            _paper(d, paper, G.page_rect(i), tr, scale)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hl = ImageDraw.Draw(layer)
    for s in model.visible_strokes():
        _draw_stroke(img, d, hl, model, s, tr, scale, media_path)
    img.paste(layer, (0, 0), layer)
    if votes:
        font = _font(max(14, int(20 * scale)))
        for sid, voters in votes.items():
            if voters and model.is_visible(sid):
                x0, y0, x1, y1 = model.bounds(sid)
                cx, cy = tr(x1, y0)
                label = f"+{len(voters)}"
                tb = d.textbbox((cx, cy), label, font=font, anchor="mm")
                d.rounded_rectangle([tb[0] - 6, tb[1] - 3, tb[2] + 6, tb[3] + 3], radius=8,
                                    fill="#0f766e")
                d.text((cx, cy), label, fill="white", font=font, anchor="mm")
    return img


def _paper(d, paper, page, tr, scale):
    px, py, pw, ph = page
    step = 20 if paper == "graph" else 40
    if paper in ("grid", "graph"):
        x = px
        while x <= px + pw:
            major = paper == "graph" and round((x - px) / step) % 5 == 0
            d.line([tr(x, py), tr(x, py + ph)], fill=(207, 216, 227) if major else PAPER_LINE)
            x += step
    if paper in ("grid", "graph", "ruled"):
        y = py + (2 * step if paper == "ruled" else 0)
        while y <= py + ph:
            major = paper == "graph" and round((y - py) / step) % 5 == 0
            d.line([tr(px, y), tr(px + pw, y)],
                   fill=(207, 216, 227) if major or paper == "ruled" else PAPER_LINE)
            y += step
        if paper == "ruled":
            d.line([tr(px + 90, py), tr(px + 90, py + ph)], fill=(243, 180, 180),
                   width=max(1, int(1.5 * scale)))
    if paper == "dots":
        r = max(1, 1.6 * scale)
        y = py
        while y <= py + ph:
            x = px
            while x <= px + pw:
                cx, cy = tr(x, y)
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(195, 204, 214))
                x += step
            y += step


def _draw_stroke(img, d, hl, model, s, tr, scale, media_path):
    xy = [tr(s["points"][i], s["points"][i + 1]) for i in range(0, len(s["points"]), 2)]
    width = max(1, int(round(s["width"] * scale)))
    color = s["color"]
    tool = s["tool"]
    if tool in ("pen", "highlighter"):
        draw, wd = (d, width) if tool == "pen" else (hl, width * 3)
        fill = color if tool == "pen" else _rgba(color, 110)
        if len(xy) == 1:
            x, y = xy[0]
            draw.ellipse([x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2], fill=fill)
        else:
            draw.line(xy, fill=fill, width=wd, joint="curve")
            for x, y in (xy[0], xy[-1]):
                draw.ellipse([x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2], fill=fill)
    elif tool in ("line", "arrow"):
        d.line(xy, fill=color, width=width)
        if tool == "arrow":
            _arrow_head(d, xy[0], xy[1], color, width)
    elif tool in ("rect", "ellipse"):
        (x0, y0), (x1, y1) = xy
        box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        (d.rectangle if tool == "rect" else d.ellipse)(box, outline=color, width=width,
                                                        fill=s.get("fill"))
    elif tool == "polygon":
        d.polygon(xy, outline=color, fill=s.get("fill"), width=width)
    elif tool == "node":
        box = G.norm_box(s["points"])
        shape = s.get("shape", "rounded")
        fill = s.get("fill") or "#ffffff"
        if shape == "ellipse":
            (x0, y0), (x1, y1) = tr(box[0], box[1]), tr(box[2], box[3])
            d.ellipse([x0, y0, x1, y1], outline=color, fill=fill, width=width)
        else:
            poly = G.node_outline(shape, box)
            d.polygon([tr(poly[i], poly[i + 1]) for i in range(0, len(poly), 2)],
                      outline=color, fill=fill, width=width)
        if s.get("text"):
            cx, cy = tr((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            font = _font(max(8, int(16 * scale)))
            text = _wrap(d, s["text"], font, (box[2] - box[0]) * scale * 0.85)
            d.multiline_text((cx, cy), text, fill="#1f2933", font=font, anchor="mm",
                             align="center")
    elif tool == "connector":
        geo = G.connector_geometry(model, s)
        if geo:
            path, mid = geo
            style = s.get("style", "arrow")
            if style == "curve":
                pts = [tr(x, y) for x, y in G.bezier_points(path)]
            else:
                pts = [tr(path[0], path[1]), tr(path[2], path[3])]
            d.line(pts, fill=color, width=width, joint="curve")
            if style in ("arrow", "curve"):
                _arrow_head(d, pts[-2], pts[-1], color, width)
            if s.get("label"):
                font = _font(max(8, int(12 * scale)))
                mx, my = tr(*mid)
                tb = d.textbbox((mx, my), s["label"], font=font, anchor="mm")
                d.rectangle([tb[0] - 4, tb[1] - 2, tb[2] + 4, tb[3] + 2], fill="white",
                            outline="#dde3ea")
                d.text((mx, my), s["label"], fill="#1f2933", font=font, anchor="mm")
    elif tool == "image":
        (x0, y0), (x1, y1) = xy
        src = media_path(s["media_id"]) if media_path else None
        box = (int(min(x0, x1)), int(min(y0, y1)), int(max(x0, x1)), int(max(y0, y1)))
        try:
            with Image.open(src) as im:
                im = im.convert("RGBA").resize((max(1, box[2] - box[0]), max(1, box[3] - box[1])))
                img.paste(im, box[:2], im)
        except Exception:
            d.rectangle(box, outline="#9aa3ad")
    elif tool in ("text", "sticky"):
        x, y = xy[0]
        font = _font(int(text_font_size(s["width"]) * scale))
        if tool == "sticky":
            text = _wrap(d, s.get("text", ""), font, 216 * scale)
            tb = d.multiline_textbbox((x + 12 * scale, y + 12 * scale), text, font=font)
            d.rectangle([x, y, max(tb[2] + 12 * scale, x + 240 * scale),
                         max(tb[3] + 12 * scale, y + 120 * scale)],
                        fill=s.get("fill") or STICKY_COLOR, outline=color,
                        width=max(1, int(2 * scale)))
            d.multiline_text((x + 12 * scale, y + 12 * scale), text, fill="#1f2328", font=font)
        else:
            text = _wrap(d, s.get("text", ""), font, 900 * scale)
            d.multiline_text((x, y), text, fill=color, font=font)


def _wrap(d, text, font, max_w):
    """Greedy word wrap for Pillow text."""
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            test = (line + " " + word).strip()
            if line and d.textlength(test, font=font) > max_w:
                out.append(line)
                line = word
            else:
                line = test
        out.append(line)
    return "\n".join(out)


def render_png(model, path, scale=1.0, media_path=None, votes=None, box=None, paper=None,
               pages=0):
    img = render_image(model, box or content_box(model), scale, media_path, votes, paper, pages)
    img.save(path, "PNG")
    return img.size


def render_pdf(model, path, pages, media_path=None, paper=None, votes=None):
    """All pages as a multi-page PDF (A4 landscape-ish proportions of the page)."""
    images = []
    for i in range(max(1, pages)):
        x, y, w, h = G.page_rect(i)
        images.append(render_image(model, (x, y, x + w, y + h), 1.25, media_path, votes, paper,
                                   pages).convert("RGB"))
    images[0].save(path, "PDF", resolution=150, save_all=True, append_images=images[1:])
    return len(images)


def _rgba(hex_color, alpha):
    return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16), alpha)


def _arrow_head(d, p0, p1, color, width):
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    size = max(14, width * 4)
    left = (p1[0] - size * math.cos(ang - 0.4), p1[1] - size * math.sin(ang - 0.4))
    right = (p1[0] - size * math.cos(ang + 0.4), p1[1] - size * math.sin(ang + 0.4))
    d.polygon([p1, left, right], fill=color)


# ====================================================================== SVG (no packages)
def render_svg(model, path, votes=None):
    x0, y0, x1, y1 = content_box(model)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.0f} {y0:.0f} '
           f'{x1 - x0:.0f} {y1 - y0:.0f}" width="{x1 - x0:.0f}" height="{y1 - y0:.0f}" '
           'font-family="Helvetica, Arial, sans-serif">',
           '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
           'markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
           'fill="context-stroke"/></marker></defs>',
           f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="white"/>']
    for s in model.visible_strokes():
        out.append(_svg_stroke(model, s))
    for sid, voters in (votes or {}).items():
        if voters and model.is_visible(sid):
            bx = model.bounds(sid)
            out.append(f'<g><circle cx="{bx[2]}" cy="{bx[1]}" r="13" fill="#0f766e"/>'
                       f'<text x="{bx[2]}" y="{bx[1] + 5}" font-size="14" fill="white" '
                       f'text-anchor="middle">+{len(voters)}</text></g>')
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(o for o in out if o))


def _svg_stroke(model, s):
    tool, c, w = s["tool"], s["color"], s["width"]
    pts = s["points"]
    fill = s.get("fill") or "none"
    pairs = " ".join(f"{pts[i]},{pts[i + 1]}" for i in range(0, len(pts), 2))
    if tool in ("pen", "highlighter"):
        op = ' stroke-opacity="0.45"' if tool == "highlighter" else ""
        ww = w * 3 if tool == "highlighter" else w
        if len(pts) == 2:
            return f'<circle cx="{pts[0]}" cy="{pts[1]}" r="{ww / 2}" fill="{c}"/>'
        return (f'<polyline points="{pairs}" fill="none" stroke="{c}" stroke-width="{ww}" '
                f'stroke-linecap="round" stroke-linejoin="round"{op}/>')
    if tool in ("line", "arrow"):
        m = ' marker-end="url(#ah)"' if tool == "arrow" else ""
        return (f'<line x1="{pts[0]}" y1="{pts[1]}" x2="{pts[2]}" y2="{pts[3]}" stroke="{c}" '
                f'stroke-width="{w}" stroke-linecap="round"{m}/>')
    if tool == "rect":
        x0, y0, x1, y1 = G.norm_box(pts)
        return (f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="{fill}" '
                f'stroke="{c}" stroke-width="{w}"/>')
    if tool == "ellipse":
        x0, y0, x1, y1 = G.norm_box(pts)
        return (f'<ellipse cx="{(x0 + x1) / 2}" cy="{(y0 + y1) / 2}" rx="{(x1 - x0) / 2}" '
                f'ry="{(y1 - y0) / 2}" fill="{fill}" stroke="{c}" stroke-width="{w}"/>')
    if tool == "polygon":
        return f'<polygon points="{pairs}" fill="{fill}" stroke="{c}" stroke-width="{w}"/>'
    if tool == "node":
        box = G.norm_box(pts)
        shape = s.get("shape", "rounded")
        nf = s.get("fill") or "white"
        if shape == "ellipse":
            body = (f'<ellipse cx="{(box[0] + box[2]) / 2}" cy="{(box[1] + box[3]) / 2}" '
                    f'rx="{(box[2] - box[0]) / 2}" ry="{(box[3] - box[1]) / 2}" fill="{nf}" '
                    f'stroke="{c}" stroke-width="{w}"/>')
        else:
            poly = G.node_outline(shape, box)
            pp = " ".join(f"{poly[i]:.1f},{poly[i + 1]:.1f}" for i in range(0, len(poly), 2))
            body = f'<polygon points="{pp}" fill="{nf}" stroke="{c}" stroke-width="{w}"/>'
        txt = _svg_text(s.get("text", ""), (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 16,
                        "#1f2933", "middle", centre=True)
        return f"<g>{body}{txt}</g>"
    if tool == "connector":
        geo = G.connector_geometry(model, s)
        if not geo:
            return ""
        path, mid = geo
        style = s.get("style", "arrow")
        m = ' marker-end="url(#ah)"' if style in ("arrow", "curve") else ""
        if style == "curve":
            dpath = (f"M{path[0]},{path[1]} C{path[2]},{path[3]} {path[4]},{path[5]} "
                     f"{path[6]},{path[7]}")
        else:
            dpath = f"M{path[0]},{path[1]} L{path[2]},{path[3]}"
        out = f'<path d="{dpath}" fill="none" stroke="{c}" stroke-width="{w}"{m}/>'
        if s.get("label"):
            out += _svg_text(s["label"], mid[0], mid[1], 12, "#1f2933", "middle", centre=True,
                             bg=True)
        return f"<g>{out}</g>"
    if tool == "image":
        x0, y0, x1, y1 = G.norm_box(pts)
        return (f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="#f4f5f7" '
                f'stroke="#9aa3ad" stroke-dasharray="4 3"/>')
    if tool in ("text", "sticky"):
        size = text_font_size(w)
        if tool == "sticky":
            lines = s.get("text", "").count("\n") + 1
            hgt = max(120, 24 + lines * size * 1.25)
            return (f'<g><rect x="{pts[0]}" y="{pts[1]}" width="240" height="{hgt}" '
                    f'fill="{s.get("fill") or STICKY_COLOR}" stroke="{c}" stroke-width="2"/>'
                    + _svg_text(s.get("text", ""), pts[0] + 12, pts[1] + 12, size, "#1f2328",
                                "start") + "</g>")
        return _svg_text(s.get("text", ""), pts[0], pts[1], size, c, "start")
    return ""


def _svg_text(text, x, y, size, color, anchor, centre=False, bg=False):
    if not text:
        return ""
    lines = text.split("\n")
    lh = size * 1.25
    top = y - lh * (len(lines) - 1) / 2 if centre else y + size * 0.9
    base = ' dominant-baseline="middle"' if centre else ""
    spans = "".join(f'<tspan x="{x}" y="{top + i * lh:.1f}"{base}>{escape(t)}</tspan>'
                    for i, t in enumerate(lines))
    box = ""
    if bg:
        wid = max(len(t) for t in lines) * size * 0.6 + 8
        box = (f'<rect x="{x - wid / 2}" y="{y - lh / 2}" width="{wid}" height="{lh}" '
               'fill="white" stroke="#dde3ea"/>')
    return f'{box}<text font-size="{size}" fill="{color}" text-anchor="{anchor}">{spans}</text>'


# ====================================================================== board files
def save_board_file(model, path, room="", settings=None):
    """The visible board as a file the teacher can restore later (objects at their
    current positions, author names kept)."""
    keep = ("id", "tool", "color", "width", "points", "text", "fill", "media_id", "shape",
            "style", "label", "src", "dst", "user")
    data = {"format": BOARD_FILE_FORMAT, "version": 1, "room": room,
            "saved_at": time.strftime("%Y-%m-%d %H:%M"), "settings": settings or {},
            "strokes": [{k: s[k] for k in keep if k in s} for s in model.visible_strokes()]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return len(data["strokes"])


def load_board_file(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("format") != BOARD_FILE_FORMAT:
        raise ValueError("this is not a ClassBoard board file")
    if not isinstance(data.get("strokes"), list):
        raise ValueError("the file has no objects")
    return data


def export_postscript(canvas, path):
    """Fallback without Pillow: Tk's own vector export of the board area in view."""
    bx = canvas.bbox("stroke") or (0, 0, canvas.winfo_width(), canvas.winfo_height())
    canvas.postscript(file=path, colormode="color", x=bx[0] - 20, y=bx[1] - 20,
                      width=bx[2] - bx[0] + 40, height=bx[3] - bx[1] + 40)
