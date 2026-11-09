"""
Pure geometry shared by the on-screen board and the exporters (PNG/PDF/SVG):
flowchart node outlines, connector routing between nodes, page rectangles,
and the shape-tool outlines (triangle, diamond).
"""

import math

from common import protocol as P


# ------------------------------------------------------------------ pages
def page_rect(i):
    """World rectangle (x, y, w, h) of page i (0-based). Pages sit side by side."""
    return i * (P.PAGE_W + P.PAGE_GAP), 0, P.PAGE_W, P.PAGE_H


def page_of(x, y):
    """Index of the page whose column contains world point (x, y) (may be <0 or >= pages)."""
    return int(math.floor((x + P.PAGE_GAP / 2) / (P.PAGE_W + P.PAGE_GAP)))


# ------------------------------------------------------------------ shape tool outlines
def shape_polygon(kind, x0, y0, x1, y1):
    """Flat point list for the triangle / diamond shape tools (from a drag box)."""
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    if kind == "triangle":
        pts = [mx, y0, x1, y1, x0, y1]
    else:                                         # diamond
        pts = [mx, y0, x1, my, mx, y1, x0, my]
    return [round(v, 1) for v in pts]


# ------------------------------------------------------------------ nodes
def norm_box(pts):
    x0, y0, x1, y1 = pts[:4]
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def node_outline(shape, box):
    """Polygon (flat list) of a node outline; None for an ellipse (draw it natively)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if shape == "ellipse":
        return None
    if shape == "rect":
        return [x0, y0, x1, y0, x1, y1, x0, y1]
    if shape == "diamond":
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return [mx, y0, x1, my, mx, y1, x0, my]
    if shape == "parallelogram":
        k = min(w * 0.18, h * 0.6)
        return [x0 + k, y0, x1, y0, x1 - k, y1, x0, y1]
    # rounded rectangle: corner arcs sampled every 22.5 degrees
    r = max(2.0, min(16.0, w / 4, h / 4))
    out = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90),
                       (x0 + r, y0 + r, 180)):
        for k in range(5):
            a = math.radians(a0 + k * 22.5)
            out += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return out


def boundary_point(box, shape, tx, ty, gap=3.0):
    """Where the line from the node centre towards (tx, ty) leaves the node outline."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    a, b = max((x1 - x0) / 2, 1e-6), max((y1 - y0) / 2, 1e-6)
    dx, dy = tx - cx, ty - cy
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return cx, cy
    if shape == "ellipse":
        t = 1 / math.hypot(dx / a, dy / b)
    elif shape == "diamond":
        t = 1 / (abs(dx) / a + abs(dy) / b)
    else:
        t = 1 / max(abs(dx) / a, abs(dy) / b)
    d = math.hypot(dx, dy)
    t += gap / d
    return cx + dx * t, cy + dy * t


def connector_path(box_a, shape_a, box_b, shape_b, style):
    """Polyline for a connector. 'curve' gives a smooth mind-map S-curve
    (draw it with spline smoothing); the others a straight segment."""
    ca = ((box_a[0] + box_a[2]) / 2, (box_a[1] + box_a[3]) / 2)
    cb = ((box_b[0] + box_b[2]) / 2, (box_b[1] + box_b[3]) / 2)
    if style == "curve":
        horizontal = abs(cb[0] - ca[0]) >= abs(cb[1] - ca[1])
        if horizontal:
            sx = 1 if cb[0] >= ca[0] else -1
            p0 = (box_a[2] + 3 if sx > 0 else box_a[0] - 3, ca[1])
            p3 = (box_b[0] - 3 if sx > 0 else box_b[2] + 3, cb[1])
            mx = (p0[0] + p3[0]) / 2
            pts = [p0, (mx, p0[1]), (mx, p3[1]), p3]
        else:
            sy = 1 if cb[1] >= ca[1] else -1
            p0 = (ca[0], box_a[3] + 3 if sy > 0 else box_a[1] - 3)
            p3 = (cb[0], box_b[1] - 3 if sy > 0 else box_b[3] + 3)
            my = (p0[1] + p3[1]) / 2
            pts = [p0, (p0[0], my), (p3[0], my), p3]
        return [v for p in pts for v in p]
    p0 = boundary_point(box_a, shape_a, *cb)
    p1 = boundary_point(box_b, shape_b, *ca)
    return [p0[0], p0[1], p1[0], p1[1]]


def bezier_points(path, steps=24):
    """Sample the cubic Bezier through a 4-point 'curve' path (for exporters)."""
    (x0, y0, x1, y1, x2, y2, x3, y3) = path
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
                    u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3))
    return out


def path_midpoint(path, style):
    if style == "curve" and len(path) == 8:
        pts = bezier_points(path, 2)
        return pts[1]
    return (path[0] + path[-2]) / 2, (path[1] + path[-1]) / 2


def connector_geometry(model, stroke):
    """(path, mid) for a placed connector stroke, or None if an end is not visible."""
    src, dst = stroke.get("src"), stroke.get("dst")
    if not (model.is_visible(src) and model.is_visible(dst)):
        return None
    a, b = model.placed(src), model.placed(dst)
    path = connector_path(norm_box(a["points"]), a.get("shape", "rounded"),
                          norm_box(b["points"]), b.get("shape", "rounded"),
                          stroke.get("style", "arrow"))
    return path, path_midpoint(path, stroke.get("style", "arrow"))
