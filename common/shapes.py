"""
Smart shape recognition: turn a rough hand-drawn stroke into a perfect
line, circle/ellipse, rectangle, triangle or quadrilateral.

Purely geometric (no machine learning, no extra packages), so it runs instantly
on the client when the pen is lifted. The recognised shape is what gets sent,
so every participant sees the clean version.

Algorithm
---------
1. Resample the stroke to N points equally spaced along its length, so fast
   and slow drawing look the same.
   If the stroke comes back near its start, any overshoot past that point is
   trimmed (people rarely close a shape exactly).
2. Open stroke (ends far apart): it is a LINE if no point deviates from the
   chord between the ends by more than 8% of the chord length.
3. Closed stroke (ends close together):
   a. Ellipse fit: take the axis-aligned bounding box as the ellipse
      (centre cx, cy, radii a, b). For every point compute its normalised
      radius r = sqrt(((x-cx)/a)^2 + ((y-cy)/b)^2). A true ellipse gives r = 1
      everywhere, a rectangle gives up to sqrt(2) at the corners. Mean |r - 1|
      below a threshold => ELLIPSE (a circle when a ~ b).
   b. Otherwise find the corners with Ramer-Douglas-Peucker simplification on
      the closed curve, then drop near-straight vertices (> 150 degrees).
      3 corners => TRIANGLE; 4 corners whose angles are all ~90 degrees =>
      RECTANGLE (axis-aligned if the sides are within 15 degrees of horizontal,
      otherwise a rotated 4-sided polygon).
4. Anything else stays a freehand stroke.
"""

import math

RESAMPLE = 64
LINE_TOL = 0.08          # max deviation / chord length for a straight line
CLOSED_GAP = 0.25        # end gap / bounding-box diagonal for a closed shape
ELLIPSE_TOL = 0.085      # mean |r - 1| for an ellipse
RDP_EPS = 0.06           # RDP tolerance as a fraction of the bbox diagonal
STRAIGHT_ANGLE = 150.0   # vertices flatter than this are not corners
RIGHT_ANGLE_TOL = 25.0
AXIS_TOL = 15.0


def _pairs(flat):
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _resample(pts, n):
    """n points equally spaced along the polyline."""
    total = sum(_dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    if total == 0:
        return [pts[0]] * n
    step = total / (n - 1)
    out = [pts[0]]
    acc = 0.0
    prev = pts[0]
    i = 1
    while i < len(pts) and len(out) < n:
        d = _dist(prev, pts[i])
        if acc + d >= step and d > 0:
            t = (step - acc) / d
            q = (prev[0] + t * (pts[i][0] - prev[0]), prev[1] + t * (pts[i][1] - prev[1]))
            out.append(q)
            prev = q
            acc = 0.0
        else:
            acc += d
            prev = pts[i]
            i += 1
    while len(out) < n:
        out.append(pts[-1])
    return out


def _point_line_dist(p, a, b):
    if a == b:
        return _dist(p, a)
    (x, y), (x1, y1), (x2, y2) = p, a, b
    return abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / _dist(a, b)


def _rdp(pts, eps):
    """Ramer-Douglas-Peucker simplification of an open polyline."""
    if len(pts) < 3:
        return list(pts)
    dmax, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = _point_line_dist(pts[i], pts[0], pts[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax <= eps:
        return [pts[0], pts[-1]]
    left = _rdp(pts[:idx + 1], eps)
    right = _rdp(pts[idx:], eps)
    return left[:-1] + right


def _angle_at(prev, cur, nxt):
    """Interior angle in degrees at `cur`."""
    v1 = (prev[0] - cur[0], prev[1] - cur[1])
    v2 = (nxt[0] - cur[0], nxt[1] - cur[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 180.0
    c = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(c))


def _corners(pts, diag):
    """Corner vertices of a closed curve (RDP + removal of flat vertices)."""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    # Start the open-curve RDP at the point farthest from the centroid, which is
    # almost certainly a real corner (avoids splitting a side in two).
    start = max(range(len(pts)), key=lambda i: _dist(pts[i], (cx, cy)))
    ring = pts[start:] + pts[:start] + [pts[start]]
    verts = _rdp(ring, RDP_EPS * diag)[:-1]
    # merge vertices that are very close together
    merged = []
    for v in verts:
        if not merged or _dist(v, merged[-1]) > 0.12 * diag:
            merged.append(v)
    if len(merged) > 2 and _dist(merged[0], merged[-1]) <= 0.12 * diag:
        merged.pop()
    # drop vertices that are not real corners (nearly straight)
    changed = True
    while changed and len(merged) > 3:
        changed = False
        for i in range(len(merged)):
            a = _angle_at(merged[i - 1], merged[i], merged[(i + 1) % len(merged)])
            if a > STRAIGHT_ANGLE:
                merged.pop(i)
                changed = True
                break
    return merged


def recognize(flat_points):
    """Classify a freehand stroke.

    Returns (tool, points) with tool in {"line", "ellipse", "rect", "polygon"}
    and points in the stroke format (flat [x, y, ...] list), or None if the
    stroke should stay freehand.
    """
    pts = _pairs(flat_points)
    if len(pts) < 6:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    diag = math.hypot(w, h)
    if diag < 25:
        return None                     # too small to be a deliberate shape
    rs = _resample(pts, RESAMPLE)
    # People overshoot when closing a shape: find where the stroke comes back
    # closest to its start (in its last 40%) and cut the overshoot off there.
    tail = range(int(RESAMPLE * 0.6), RESAMPLE)
    close_i = min(tail, key=lambda i: _dist(rs[0], rs[i]))
    if _dist(rs[0], rs[close_i]) <= CLOSED_GAP * diag:
        rs = _resample(rs[:close_i + 1], RESAMPLE)
        xs, ys = [p[0] for p in rs], [p[1] for p in rs]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
    gap = _dist(rs[0], rs[-1])

    # ---------------------------------------------------------------- open: line?
    if gap > CLOSED_GAP * diag:
        a, b = rs[0], rs[-1]
        chord = _dist(a, b)
        if chord > 0 and max(_point_line_dist(p, a, b) for p in rs) / chord < LINE_TOL:
            return "line", _r([a[0], a[1], b[0], b[1]])
        return None

    # ---------------------------------------------------------------- closed shapes
    loop = rs[:-1] if gap < 1e-9 else rs
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if w > 8 and h > 8:
        cx, cy, a, b = (x0 + x1) / 2, (y0 + y1) / 2, w / 2, h / 2
        err = sum(abs(math.hypot((p[0] - cx) / a, (p[1] - cy) / b) - 1) for p in loop) / len(loop)
        if err < ELLIPSE_TOL:
            return "ellipse", _r([x0, y0, x1, y1])

    corners = _corners(loop, diag)
    if len(corners) == 3:
        return "polygon", _r([c for p in corners for c in p])
    if len(corners) == 4:
        angles = [_angle_at(corners[i - 1], corners[i], corners[(i + 1) % 4]) for i in range(4)]
        if all(abs(a - 90) <= RIGHT_ANGLE_TOL for a in angles):
            e = corners[1][0] - corners[0][0], corners[1][1] - corners[0][1]
            tilt = math.degrees(math.atan2(e[1], e[0])) % 90
            if min(tilt, 90 - tilt) <= AXIS_TOL:
                cx0, cy0 = min(p[0] for p in corners), min(p[1] for p in corners)
                cx1, cy1 = max(p[0] for p in corners), max(p[1] for p in corners)
                return "rect", _r([cx0, cy0, cx1, cy1])
            return "polygon", _r(_rectify(corners))
    return None


def _rectify(c):
    """Turn a rough rotated quadrilateral into an exact rectangle (same centre,
    orientation of its longest side, average side lengths)."""
    cx = sum(p[0] for p in c) / 4
    cy = sum(p[1] for p in c) / 4
    sides = [(_dist(c[i], c[(i + 1) % 4]), i) for i in range(4)]
    _, i = max(sides)
    ang = math.atan2(c[(i + 1) % 4][1] - c[i][1], c[(i + 1) % 4][0] - c[i][0])
    L = (_dist(c[0], c[1]) + _dist(c[2], c[3])) / 2
    W = (_dist(c[1], c[2]) + _dist(c[3], c[0])) / 2
    if _dist(c[i], c[(i + 1) % 4]) != _dist(c[0], c[1]) and i % 2 == 1:
        L, W = W, L
    ux, uy = math.cos(ang), math.sin(ang)
    vx, vy = -uy, ux
    out = []
    for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        out += [cx + su * L / 2 * ux + sv * W / 2 * vx, cy + su * L / 2 * uy + sv * W / 2 * vy]
    return out


def _r(vals):
    return [round(v, 1) for v in vals]
