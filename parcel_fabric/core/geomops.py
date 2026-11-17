# -*- coding: utf-8 -*-
"""Plane geometry for Phase 2 edits: splitting lines and arcs, offsets,
and prorating COGO dimensions.  Pure Python (no QGIS imports).

Points are (x, y) tuples.  Arcs are given by three points (start, any
point on the arc, end), the way QGIS stores a CircularString.
"""

import math

TWO_PI = 2 * math.pi


def _ang(c, p):
    return math.atan2(p[1] - c[1], p[0] - c[0])


def arc_center(s, m, e):
    """Centre and radius of the circle through three points (None if straight)."""
    bx, by = m[0] - s[0], m[1] - s[1]
    cx, cy = e[0] - s[0], e[1] - s[1]
    d = 2.0 * (bx * cy - by * cx)
    if abs(d) < 1e-12 * max(1.0, bx * bx + by * by, cx * cx + cy * cy):
        return None
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    ux = (cy * b2 - by * c2) / d
    uy = (bx * c2 - cx * b2) / d
    return (s[0] + ux, s[1] + uy), math.hypot(ux, uy)


def arc_sweep(s, m, e):
    """(centre, radius, start angle, signed sweep) of an arc; sweep > 0 = CCW."""
    cr = arc_center(s, m, e)
    if cr is None:
        return None
    c, r = cr
    a0, am, a1 = _ang(c, s), _ang(c, m), _ang(c, e)
    ccw = (am - a0) % TWO_PI
    tot = (a1 - a0) % TWO_PI
    if ccw <= tot:            # m lies between s and e going counter-clockwise
        return c, r, a0, tot
    return c, r, a0, -((a0 - a1) % TWO_PI)


def _on_circle(c, r, a):
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))


def arc_param(s, m, e, p):
    """Fraction (0-1) along the arc of the point on it nearest ``p``."""
    c, r, a0, sw = arc_sweep(s, m, e)
    ap = _ang(c, p)
    d = (ap - a0) % TWO_PI if sw > 0 else (a0 - ap) % TWO_PI
    return max(0.0, min(1.0, d / abs(sw)))


def arc_point(s, m, e, t):
    c, r, a0, sw = arc_sweep(s, m, e)
    return _on_circle(c, r, a0 + sw * t)


def split_arc(s, m, e, p):
    """Split an arc at (the point on it nearest) ``p``.

    Returns ((s, m1, q), (q, m2, e)) where q is p moved onto the arc.
    """
    c, r, a0, sw = arc_sweep(s, m, e)
    t = arc_param(s, m, e, p)
    q = _on_circle(c, r, a0 + sw * t)
    m1 = _on_circle(c, r, a0 + sw * t / 2.0)
    m2 = _on_circle(c, r, a0 + sw * (1.0 + t) / 2.0)
    return (s, m1, q), (q, m2, e)


def arc_length(s, m, e):
    sw = arc_sweep(s, m, e)
    if sw is None:
        return math.hypot(e[0] - s[0], e[1] - s[1])
    return abs(sw[3]) * sw[1]


def split_polyline(pts, p):
    """Split a polyline at the point on it nearest ``p``.

    Returns (first part, second part, fraction of length) or None if ``p``
    is at an end.
    """
    best = None
    run = 0.0
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
        q = (a[0] + t * dx, a[1] + t * dy)
        d = math.hypot(q[0] - p[0], q[1] - p[1])
        if best is None or d < best[0]:
            best = (d, i, q, run + t * math.sqrt(L2))
        run += math.sqrt(L2)
    _d, i, q, along = best
    if total <= 0 or along <= 1e-9 or along >= total - 1e-9:
        return None
    first = list(pts[: i + 1]) + [q]
    second = [q] + list(pts[i + 1:])
    return first, second, along / total


def offset_segment(a, b, dist):
    """Offset a straight segment; positive ``dist`` = to the RIGHT of a->b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L == 0:
        raise ValueError("Zero-length line")
    nx, ny = dy / L, -dx / L       # right-hand normal
    return (a[0] + nx * dist, a[1] + ny * dist), (b[0] + nx * dist, b[1] + ny * dist)


def offset_arc(s, m, e, dist):
    """Offset an arc; positive ``dist`` = to the RIGHT of the direction of travel."""
    c, r, a0, sw = arc_sweep(s, m, e)
    # travelling CCW the centre is on the left, so "right" moves away from it
    nr = r + dist if sw > 0 else r - dist
    if nr <= 0:
        raise ValueError("Offset is larger than the curve radius")
    return (_on_circle(c, nr, a0), _on_circle(c, nr, a0 + sw / 2.0), _on_circle(c, nr, a0 + sw))


def chord_azimuth(a, b):
    return math.atan2(b[0] - a[0], b[1] - a[1]) % TWO_PI


def prorate(total_record, total_geom, part_geom):
    """Record dimension for a part of a line, keeping the record/geometry ratio."""
    if total_record is None or not total_geom:
        return part_geom
    return total_record * (part_geom / total_geom)
