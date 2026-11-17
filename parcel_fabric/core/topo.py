# -*- coding: utf-8 -*-
"""Small pure-Python topology helpers (no QGIS imports)."""

import math


def _close(a, b, tol):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def chain_ring(segments, tol):
    """Order line ends into one closed ring.

    ``segments`` is a list of (key, start_xy, end_xy).  Returns a list of
    (key, reversed) in walking order, or None when the lines do not form
    exactly one closed ring.
    """
    if not segments:
        return None
    remaining = list(segments)
    key, s, e = remaining.pop(0)
    ring = [(key, False)]
    first, cur = s, e
    while remaining:
        hit = None
        for i, (k, a, b) in enumerate(remaining):
            if _close(a, cur, tol):
                hit = (i, k, False, b)
                break
            if _close(b, cur, tol):
                hit = (i, k, True, a)
                break
        if hit is None:
            return None
        i, k, rev, nxt = hit
        remaining.pop(i)
        ring.append((k, rev))
        cur = nxt
    if not _close(cur, first, tol):
        return None
    return ring


def dangles(endpoints, tol):
    """Ends touched by only one line.

    ``endpoints`` is a list of (key, xy).  Returns the (key, xy) that have no
    other endpoint within ``tol``.  Uses a grid so it stays fast.
    """
    cell = max(tol, 1e-9) * 2.0
    grid = {}
    for i, (_k, (x, y)) in enumerate(endpoints):
        grid.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append(i)
    out = []
    for i, (k, (x, y)) in enumerate(endpoints):
        gx, gy = math.floor(x / cell), math.floor(y / cell)
        found = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j != i and _close(endpoints[j][1], (x, y), tol):
                        found = True
                        break
                if found:
                    break
            if found:
                break
        if not found:
            out.append((k, (x, y)))
    return out
