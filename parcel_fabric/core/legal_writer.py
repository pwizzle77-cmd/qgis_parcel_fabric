# -*- coding: utf-8 -*-
"""Write a metes-and-bounds legal description from a polygon.

Pure Python (no QGIS imports) so it can be tested on its own.  A ring is a
list of segments, each ("line", a, b) or ("arc", s, m, e) with (x, y)
points, in order, end of one = start of the next.

Bearings are grid bearings in the polygon's coordinate system.
"""

import math

from . import geomops as G
from . import units as U

TWO_PI = 2 * math.pi
COMPASS8 = ["North", "Northeast", "East", "Southeast", "South", "Southwest", "West", "Northwest"]


# --------------------------------------------------------------------------
# Ring preparation
# --------------------------------------------------------------------------
def _len(seg):
    if seg[0] == "arc":
        return G.arc_length(*seg[1:])
    a, b = seg[1], seg[2]
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _start(seg):
    return seg[1]


def _end(seg):
    return seg[-1]


def reverse_seg(seg):
    if seg[0] == "arc":
        return ("arc", seg[3], seg[2], seg[1])
    return ("line", seg[2], seg[1])


def signed_area(ring):
    """Signed area (positive = counter-clockwise), curves included."""
    s = 0.0
    for seg in ring:
        a, b = _start(seg), _end(seg)
        s += a[0] * b[1] - b[0] * a[1]
    area = s / 2.0
    for seg in ring:
        if seg[0] == "arc":
            sw = G.arc_sweep(*seg[1:])
            if sw is None:
                continue
            _c, r, _a0, sweep = sw
            d = abs(sweep)
            # a counter-clockwise arc bulges to the right of its chord
            seg_area = 0.5 * r * r * (d - math.sin(d))
            area += seg_area if sweep > 0 else -seg_area
    return area


def orient(ring, clockwise=True):
    ccw = signed_area(ring) > 0
    if ccw == clockwise:
        return [reverse_seg(s) for s in reversed(ring)]
    return list(ring)


def rotate_to(ring, index):
    return ring[index:] + ring[:index]


def northwest_index(ring):
    """Index of the segment starting at the north-westernmost corner."""
    pts = [_start(s) for s in ring]
    return max(range(len(pts)), key=lambda i: pts[i][1] - pts[i][0])


def nearest_index(ring, xy):
    pts = [_start(s) for s in ring]
    return min(range(len(pts)), key=lambda i: math.hypot(pts[i][0] - xy[0], pts[i][1] - xy[1]))


def merge_collinear(ring, angle_tol_sec=1.0, min_len=0.0):
    """Join consecutive straight courses whose bearings differ by less than
    ``angle_tol_sec`` arc-seconds.  Straight courses shorter than ``min_len``
    are absorbed into the course before them.
    """
    tol = math.radians(angle_tol_sec / 3600.0)

    def same_dir(a, b):
        az1 = G.chord_azimuth(a[1], a[2])
        az2 = G.chord_azimuth(b[1], b[2])
        return abs((az2 - az1 + math.pi) % TWO_PI - math.pi) <= tol

    out = []
    for seg in ring:
        if out and seg[0] == "line" and out[-1][0] == "line" and (
                same_dir(out[-1], seg) or _len(seg) < min_len):
            out[-1] = ("line", out[-1][1], seg[2])
            continue
        out.append(seg)
    if len(out) > 2 and out[0][0] == "line" and out[-1][0] == "line" and same_dir(out[-1], out[0]):
        out[0] = ("line", out[-1][1], out[0][2])
        out.pop()
    return out


def find_arcs(ring, tol=0.02, min_points=4, max_seg=None):
    """Turn runs of short straight courses that lie on one circle (a curve
    that was densified into vertices) back into true arcs.

    ``tol`` is how far (in map units) a vertex may sit off the circle.
    """
    out = []
    i = 0
    n = len(ring)
    while i < n:
        best = None
        if ring[i][0] == "line":
            j = i
            pts = [ring[i][1], ring[i][2]]
            while j + 1 < n and ring[j + 1][0] == "line" and (
                    max_seg is None or _len(ring[j + 1]) <= max_seg):
                cand = pts + [ring[j + 1][2]]
                if len(cand) >= min_points and _on_one_circle(cand, tol):
                    best = (j + 1, cand)
                elif len(cand) >= min_points and best is not None:
                    break
                pts = cand
                j += 1
        if best is not None:
            last, pts = best
            out.append(("arc", pts[0], pts[len(pts) // 2], pts[-1]))
            i = last + 1
        else:
            out.append(ring[i])
            i += 1
    return out


def _on_one_circle(pts, tol):
    cr = G.arc_center(pts[0], pts[len(pts) // 2], pts[-1])
    if cr is None:
        return False
    (cx, cy), r = cr
    if r > 1e6:
        return False
    # same turning direction all along, and every vertex on the circle
    signs = set()
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-12:
            signs.add(cross > 0)
    if len(signs) != 1:
        return False
    return all(abs(math.hypot(p[0] - cx, p[1] - cy) - r) <= tol for p in pts)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
def _b(az, sec_dec):
    return U.azimuth_to_bearing(az, sec_dec)


def _d(x, dec):
    return ("{:,.%df}" % dec).format(x)


def concave_word(seg):
    """'concave Southeasterly' style word for which way the curve opens."""
    c, _r, _a0, _sw = G.arc_sweep(*seg[1:])
    m = ((seg[1][0] + seg[3][0]) / 2.0, (seg[1][1] + seg[3][1]) / 2.0)
    az = G.chord_azimuth(m, c)
    return COMPASS8[int(((math.degrees(az) + 22.5) % 360) // 45)] + "erly"


def tangent_in(seg):
    if seg[0] == "line":
        return G.chord_azimuth(seg[1], seg[2])
    c, r, a0, sw = G.arc_sweep(*seg[1:])
    # tangent direction at the start: perpendicular to the radius, turning with the sweep
    rad = G.chord_azimuth(c, seg[1])
    return (rad + (math.pi / 2 if sw < 0 else -math.pi / 2)) % TWO_PI


def tangent_out(seg):
    if seg[0] == "line":
        return G.chord_azimuth(seg[1], seg[2])
    c, r, a0, sw = G.arc_sweep(*seg[1:])
    rad = G.chord_azimuth(c, seg[3])
    return (rad + (math.pi / 2 if sw < 0 else -math.pi / 2)) % TWO_PI


def describe_course(seg, prev, unit="feet", sec_dec=0, dist_dec=2, tangent_tol_sec=5.0):
    if seg[0] == "line":
        az, dist = U.inverse(seg[1][0], seg[1][1], seg[2][0], seg[2][1])
        return "%s, a distance of %s %s" % (_b(az, sec_dec), _d(dist, dist_dec), unit)
    c, r, a0, sw = G.arc_sweep(*seg[1:])
    side = "left" if sw > 0 else "right"
    delta = math.degrees(abs(sw))
    arc = abs(sw) * r
    caz, chord = U.inverse(seg[1][0], seg[1][1], seg[3][0], seg[3][1])
    tangent = ""
    if prev is not None:
        diff = abs((tangent_in(seg) - tangent_out(prev) + math.pi) % TWO_PI - math.pi)
        tangent = "tangent " if math.degrees(diff) * 3600 <= tangent_tol_sec else "non-tangent "
    return ("along the arc of a %scurve to the %s, concave %s, having a radius of %s %s, a central "
            "angle of %s, an arc length of %s %s, a chord bearing of %s and a chord distance of %s %s" % (
                tangent, side, concave_word(seg), _d(r, dist_dec), unit,
                U.format_angle(delta, sec_dec), _d(arc, dist_dec), unit, _b(caz, sec_dec),
                _d(chord, dist_dec), unit))


def describe_ring(ring, unit="feet", sec_dec=0, dist_dec=2):
    """'thence ...;' clauses for a ring that starts at the point of beginning."""
    out = []
    prev = ring[-1]
    for i, seg in enumerate(ring):
        text = "thence " + describe_course(seg, prev, unit, sec_dec, dist_dec)
        if i == len(ring) - 1:
            text += " to the Point of Beginning"
        out.append(text)
        prev = seg
    return out


def write(ring, unit="feet", sec_dec=0, dist_dec=2, header="", commence=None, holes=(),
          pob_text="the most northwesterly corner of the herein described parcel"):
    """The full description.

    ``commence`` is (text describing the commencement point, (x, y) of that
    point) or None.  ``holes`` are rings to except, each already oriented
    and started where they should be described from.
    """
    parts = []
    if header:
        parts.append(header.strip())
    pob = _start(ring[0])
    if commence:
        ctext, cxy = commence
        az, dist = U.inverse(cxy[0], cxy[1], pob[0], pob[1])
        parts.append("Commence at %s; thence %s, a distance of %s %s to the Point of Beginning; %s." % (
            ctext, _b(az, sec_dec), _d(dist, dist_dec), unit,
            "; ".join(describe_ring(ring, unit, sec_dec, dist_dec))))
    else:
        parts.append("Beginning at %s; %s." % (pob_text, "; ".join(
            describe_ring(ring, unit, sec_dec, dist_dec))))
    area = abs(signed_area(ring))
    for k, h in enumerate(holes, 1):
        hpob = _start(h[0])
        clauses = describe_ring(h, unit, sec_dec, dist_dec)
        clauses[-1] = clauses[-1].replace(" to the Point of Beginning", " to the point of beginning of this exception")
        parts.append("LESS AND EXCEPT%s: Begin at a point (%s); %s." % (
            " %d" % k if len(holes) > 1 else "", _xy_text(hpob), "; ".join(clauses)))
        area -= abs(signed_area(h))
    if unit == "feet":
        parts.append("Containing %s acres (%s square feet), more or less." % (
            _d(area / 43560.0, 3), _d(area, 0)))
    else:
        parts.append("Containing %s hectares (%s square meters), more or less." % (
            _d(area / 10000.0, 4), _d(area, 0)))
    return "\n\n".join(parts), area


def _xy_text(p):
    return "N %s, E %s" % (_d(p[1], 2), _d(p[0], 2))


def calls(ring):
    """(azimuth radians, distance) or curve data for a closure self-check."""
    out = []
    for seg in ring:
        if seg[0] == "line":
            az, d = U.inverse(seg[1][0], seg[1][1], seg[2][0], seg[2][1])
            out.append(("line", az, d))
        else:
            c, r, a0, sw = G.arc_sweep(*seg[1:])
            caz, chord = U.inverse(seg[1][0], seg[1][1], seg[3][0], seg[3][1])
            out.append(("arc", caz, r if sw < 0 else -r, abs(sw) * r))
    return out
