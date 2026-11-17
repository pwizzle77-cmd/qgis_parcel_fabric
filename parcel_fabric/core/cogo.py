# -*- coding: utf-8 -*-
"""COGO courses, traverses, curves, misclose and area.

Pure Python (no QGIS imports).  Units are whatever linear unit the fabric
uses (US survey feet by default); angles are radians internally.

Curve sign convention follows Esri: a NEGATIVE radius is a curve to the
LEFT (counter-clockwise), a positive radius curves RIGHT.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .units import normalize_azimuth, inverse

LINE = "line"
CURVE = "curve"

# How the direction of a curve was given
DIR_CHORD = "chord"
DIR_TANGENT = "tangent"    # tangent bearing at the start of the curve
DIR_RADIAL = "radial"      # bearing from the start point to the centre
DIR_TANGENT_PREV = "tangent_prev"  # tangent to the previous course

TWO_PI = 2 * math.pi


@dataclass
class Course:
    """One call of a traverse.

    For a line: ``azimuth`` and ``distance``.
    For a curve: ``radius`` (signed, negative = left) and ``arc_length``,
    plus ``azimuth`` interpreted according to ``dir_kind``.
    """
    kind: str = LINE
    azimuth: Optional[float] = None
    distance: Optional[float] = None
    radius: Optional[float] = None
    arc_length: Optional[float] = None
    dir_kind: str = DIR_CHORD
    note: str = ""

    # ------------------------------------------------------------------
    @property
    def is_curve(self):
        return self.kind == CURVE

    @property
    def delta(self):
        """Central angle (radians, always positive)."""
        if not self.is_curve:
            return 0.0
        return abs(self.arc_length) / abs(self.radius)

    @property
    def chord_length(self):
        if not self.is_curve:
            return self.distance
        return 2.0 * abs(self.radius) * math.sin(self.delta / 2.0)

    @property
    def side(self):
        """+1 right, -1 left."""
        return -1 if (self.radius or 0) < 0 else 1

    def validate(self):
        if self.is_curve:
            if not self.radius:
                raise ValueError("Curve needs a radius")
            if not self.arc_length or self.arc_length <= 0:
                raise ValueError("Curve needs an arc length (or delta/chord)")
            if self.delta >= TWO_PI:
                raise ValueError("Arc length is longer than the full circle")
            if self.azimuth is None and self.dir_kind != DIR_TANGENT_PREV:
                raise ValueError("Curve needs a bearing")
        else:
            if self.azimuth is None:
                raise ValueError("Line needs a bearing")
            if self.distance is None or self.distance < 0:
                raise ValueError("Line needs a distance")


# --------------------------------------------------------------------------
# Curve helpers
# --------------------------------------------------------------------------
def arc_length_from_delta(radius, delta):
    return abs(radius) * abs(delta)


def arc_length_from_chord(radius, chord):
    r = abs(radius)
    if chord > 2 * r + 1e-9:
        raise ValueError("Chord is longer than the diameter")
    return 2.0 * r * math.asin(min(1.0, chord / (2.0 * r)))


def arc_length_from_tangent(radius, tangent):
    return 2.0 * abs(radius) * math.atan(tangent / abs(radius))


def chord_azimuth(course, prev_out_tangent=None):
    """Chord azimuth of a curve, from however its direction was given."""
    half = course.side * course.delta / 2.0
    k = course.dir_kind
    if k == DIR_CHORD:
        return normalize_azimuth(course.azimuth)
    if k == DIR_TANGENT_PREV:
        if prev_out_tangent is None:
            raise ValueError("Tangent curve has no previous course to follow")
        return normalize_azimuth(prev_out_tangent + half)
    if k == DIR_TANGENT:
        return normalize_azimuth(course.azimuth + half)
    if k == DIR_RADIAL:
        # centre lies to the right of the tangent for a right curve
        tangent_in = course.azimuth - course.side * math.pi / 2.0
        return normalize_azimuth(tangent_in + half)
    raise ValueError("Unknown curve direction type %r" % k)


def tangents(course, chord_az):
    """(tangent in, tangent out) azimuths for a course given its chord az."""
    if not course.is_curve:
        return chord_az, chord_az
    half = course.side * course.delta / 2.0
    return normalize_azimuth(chord_az - half), normalize_azimuth(chord_az + half)


def curve_points(x1, y1, chord_az, course):
    """Return (centre, mid-arc point, end point) of a curve."""
    t_in, _ = tangents(course, chord_az)
    r = abs(course.radius)
    c_az = t_in + course.side * math.pi / 2.0
    cx, cy = x1 + r * math.sin(c_az), y1 + r * math.cos(c_az)
    c = course.chord_length
    x2, y2 = x1 + c * math.sin(chord_az), y1 + c * math.cos(chord_az)
    # mid-arc: from the centre, along the bisector of the two radials
    start_rad = normalize_azimuth(c_az + math.pi)        # centre -> start
    mid_rad = start_rad + course.side * course.delta / 2.0
    mx, my = cx + r * math.sin(mid_rad), cy + r * math.cos(mid_rad)
    return (cx, cy), (mx, my), (x2, y2)


def segment_area(course):
    """Area between a curve and its chord."""
    if not course.is_curve:
        return 0.0
    d = course.delta
    return 0.5 * course.radius ** 2 * (d - math.sin(d))


# --------------------------------------------------------------------------
# Traverse
# --------------------------------------------------------------------------
@dataclass
class Leg:
    """A computed course: where it starts and ends, and its geometry."""
    course: Course
    start: Tuple[float, float]
    end: Tuple[float, float]
    chord_az: float
    mid: Optional[Tuple[float, float]] = None      # mid-arc point for curves
    center: Optional[Tuple[float, float]] = None

    @property
    def length(self):
        return self.course.arc_length if self.course.is_curve else self.course.distance


@dataclass
class TraverseResult:
    legs: List[Leg] = field(default_factory=list)
    misclose_dx: float = 0.0
    misclose_dy: float = 0.0
    perimeter: float = 0.0
    area: float = 0.0          # only meaningful for closed loops

    @property
    def misclose(self):
        return math.hypot(self.misclose_dx, self.misclose_dy)

    @property
    def misclose_azimuth(self):
        return normalize_azimuth(math.atan2(self.misclose_dx, self.misclose_dy))

    @property
    def ratio(self):
        return self.perimeter / self.misclose if self.misclose > 1e-9 else float("inf")

    @property
    def end_point(self):
        return self.legs[-1].end if self.legs else None


def compute(start, courses, close_to=None, rotation=0.0, scale=1.0):
    """Walk ``courses`` from ``start``.

    ``close_to`` is the point the traverse should end on (defaults to
    ``start`` for a closed loop; pass ``False`` for an open traverse).
    ``rotation`` (radians, clockwise) and ``scale`` apply ground-to-grid.
    Misclose is (computed end - close_to).
    """
    x, y = start
    res = TraverseResult()
    prev_out = None
    for c in courses:
        c.validate()
        if c.is_curve:
            caz = chord_azimuth(c, prev_out) + rotation
            sc = Course(CURVE, caz, None, c.radius * scale, c.arc_length * scale, DIR_CHORD)
            centre, mid, end = curve_points(x, y, caz, sc)
            leg = Leg(c, (x, y), end, normalize_azimuth(caz), mid, centre)
            _, prev_out = tangents(sc, caz)
            res.perimeter += sc.arc_length
        else:
            caz = c.azimuth + rotation
            d = c.distance * scale
            end = (x + d * math.sin(caz), y + d * math.cos(caz))
            leg = Leg(c, (x, y), end, normalize_azimuth(caz))
            prev_out = normalize_azimuth(caz)
            res.perimeter += d
        res.legs.append(leg)
        x, y = leg.end

    if close_to is None:
        close_to = start
    if close_to is not False and res.legs:
        res.misclose_dx = x - close_to[0]
        res.misclose_dy = y - close_to[1]
        if close_to == start:
            res.area = loop_area(res.legs)
    return res


def loop_area(legs):
    """Area of a closed loop of legs, curves included (always positive)."""
    pts = [l.start for l in legs]
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    signed = s / 2.0          # positive = counter-clockwise in x/y space
    for l in legs:
        if l.course.is_curve:
            # A right curve bulges to the LEFT of its chord, a left curve to
            # the right.  With the signed area positive for a counter-
            # clockwise ring (interior on the left), a left bulge eats into
            # the interior (subtract) and a right bulge adds to it.  The same
            # rule holds for clockwise rings because the sign flips too.
            seg = segment_area(l.course)
            signed += seg if l.course.side < 0 else -seg
    return abs(signed)


def compass_rule(result, close_to):
    """Distribute misclose by the compass (Bowditch) rule.

    Returns new courses (inverse of the adjusted points). Curves keep their
    radius; their arc length is recomputed from the adjusted chord.
    """
    total = sum(l.length for l in result.legs)
    if total <= 0:
        return [l.course for l in result.legs]
    dx, dy = result.misclose_dx, result.misclose_dy
    run = 0.0
    adj = [result.legs[0].start]
    for l in result.legs:
        run += l.length
        f = run / total
        adj.append((l.end[0] - dx * f, l.end[1] - dy * f))
    # the last point now lands exactly on close_to
    adj[-1] = tuple(close_to)
    out = []
    for i, l in enumerate(result.legs):
        az, dist = inverse(adj[i][0], adj[i][1], adj[i + 1][0], adj[i + 1][1])
        if l.course.is_curve:
            r = l.course.radius
            arc = 2.0 * abs(r) * math.asin(min(1.0, dist / (2.0 * abs(r))))
            out.append(Course(CURVE, az, None, r, arc, DIR_CHORD, l.course.note))
        else:
            out.append(Course(LINE, az, dist, note=l.course.note))
    return out


# --------------------------------------------------------------------------
# Loops built from existing fabric lines
# --------------------------------------------------------------------------
def loop_misclose(cogo_legs):
    """Misclose of a loop of lines using their COGO dimensions.

    ``cogo_legs`` is a list of (course, reversed) in loop order where course
    has CHORD azimuth (Esri convention).  A reversed line is walked from its
    end to its start.  Returns (dx, dy, perimeter, area) or None when any
    line has no COGO dimensions.
    """
    x = y = 0.0
    legs = []
    per = 0.0
    for c, rev in cogo_legs:
        if c is None or c.azimuth is None:
            return None
        if c.is_curve:
            if not c.arc_length or not c.radius:
                return None
            az = c.azimuth + (math.pi if rev else 0.0)
            radius = -c.radius if rev else c.radius
            cc = Course(CURVE, az, None, radius, c.arc_length, DIR_CHORD)
            ln = cc.chord_length
            per += c.arc_length
        else:
            if c.distance is None:
                return None
            az = c.azimuth + (math.pi if rev else 0.0)
            cc = Course(LINE, az, c.distance)
            ln = c.distance
            per += c.distance
        nx, ny = x + ln * math.sin(az), y + ln * math.cos(az)
        legs.append(Leg(cc, (x, y), (nx, ny), normalize_azimuth(az)))
        x, y = nx, ny
    if not legs:
        return None
    return x, y, per, loop_area(legs)
