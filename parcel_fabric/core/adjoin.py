# -*- coding: utf-8 -*-
"""Fit a traverse to the lines already in the fabric.

Adjoining parcels share one line.  Corners within the fit distance of an existing corner
or line are held to it, courses that run along existing lines reuse them, and only the
rest becomes new lines.  A neighbour's line is split where a corner falls partway along it.
"""

import math
from copy import copy
from dataclasses import dataclass, field

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsLineString, QgsPoint, QgsPolygon, QgsRectangle

from .build import _single_curve
from .cogo import LINE, Course
from .edits import EditError, _xy
from .fabric import CURRENT, type_expr


@dataclass
class Fit:
    legs: list = field(default_factory=list)        # what still has to be written
    nodes: list = field(default_factory=list)       # points where existing lines may need splitting
    along: int = 0                                  # courses, or parts of them, that reuse a line
    moved: float = 0.0                              # largest adjustment, map units
    outline: QgsGeometry = None                     # the fitted parcel (closed traverses)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _near(curve, xy):
    """(distance, nearest point) on the true curve."""
    d2, p = curve.closestSegment(QgsPoint(*xy))[:2]
    return math.sqrt(max(d2, 0.0)), _xy(p)


def _points(geom):
    """Vertices of a (curved) geometry, curves as short chords."""
    flat = QgsGeometry(geom.constGet().segmentize())        # kept alive while it is walked
    return [QgsPoint(v.x(), v.y()) for v in flat.vertices()]


def fit_traverse(fabric, legs, ptype, fit, closed):
    """``legs`` (map units) fitted to the current ``ptype`` lines. ``fit`` is in map units."""
    tol = fabric.tolerance
    box = QgsRectangle()
    for leg in legs:
        box.combineExtentWith(fabric.leg_geometry(leg).boundingBox())
    box.grow(fit + tol)
    curves = []
    for f in fabric.lines.getFeatures(QgsFeatureRequest().setFilterRect(box).setFilterExpression(
            "%s AND %s" % (CURRENT, type_expr(ptype)))):
        curve = _single_curve(f.geometry())
        if curve is not None and not curve.isEmpty() and not f["RetiredByRecord"]:
            curves.append(curve.clone())
    ends = [_xy(p) for c in curves for p in (c.startPoint(), c.endPoint())]
    out = Fit()

    def held(xy):
        if not curves:
            return xy
        corner = min(ends, key=lambda e: _dist(e, xy))
        if _dist(corner, xy) <= fit:
            to = corner
        else:
            d, p = min((_near(c, xy) for c in curves), key=lambda r: r[0])
            to = p if d <= fit else xy
        out.moved = max(out.moved, _dist(to, xy))
        return to

    corners = [held(leg.start) for leg in legs]
    corners.append(corners[0] if closed else held(legs[-1].end))
    out.nodes += corners
    ring = []

    def write(leg, a, b):
        leg.start, leg.end = a, b
        out.legs.append(leg)
        ring.extend(_points(fabric.leg_geometry(leg)))

    for k, leg in enumerate(legs):
        a, b = corners[k], corners[k + 1]
        if _dist(a, b) <= tol:
            raise EditError("Course %d is shorter than the fit distance. Use a smaller fit distance." % (k + 1))
        new = copy(leg)
        new.record_az = getattr(leg, "record_az", leg.chord_az)
        if leg.course.is_curve:
            new.mid = tuple(leg.mid[i] + (a[i] - leg.start[i] + b[i] - leg.end[i]) / 2 for i in (0, 1))
            same = next((c for c in curves if _near(c, new.mid)[0] <= fit and any(
                _dist(_xy(p), a) <= tol and _dist(_xy(q), b) <= tol
                for p, q in ((c.startPoint(), c.endPoint()), (c.endPoint(), c.startPoint())))), None)
            if same is None:
                write(new, a, b)
            else:
                out.along += 1
                pts = _points(QgsGeometry(same.clone()))
                ring.extend(pts if _dist(_xy(pts[0]), a) <= tol else reversed(pts))
            continue
        # a straight course: stop at every existing corner along it
        length = _dist(a, b)
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        stops = []
        for e in ends:
            t = (e[0] - a[0]) * ux + (e[1] - a[1]) * uy
            off = abs((e[0] - a[0]) * uy - (e[1] - a[1]) * ux)
            if off <= fit and _dist(e, a) > tol and _dist(e, b) > tol and 0 < t < length:
                stops.append((t, off, e))
        pts = [a]
        for _t, off, e in sorted(stops):
            if _dist(e, pts[-1]) > tol:
                pts.append(e)
                out.moved = max(out.moved, off)
        pts.append(b)
        out.nodes += pts[1:-1]
        for p, q in zip(pts, pts[1:]):
            mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
            if any(_near(c, p)[0] <= tol and _near(c, q)[0] <= tol and _near(c, mid)[0] <= fit for c in curves):
                out.along += 1
                ring.extend((QgsPoint(*p), QgsPoint(*q)))
                continue
            part = copy(new)
            if len(pts) > 2:            # part of the call: its record distance in proportion
                part.course = Course(LINE, leg.course.azimuth, leg.course.distance * _dist(p, q) / length)
            write(part, p, q)
    if closed:
        clean = [p for i, p in enumerate(ring) if i == 0 or _dist(_xy(p), _xy(ring[i - 1])) > 1e-9]
        clean.append(clean[0].clone())
        out.outline = QgsGeometry(QgsPolygon(QgsLineString(clean)))
        if not out.outline.isGeosValid():
            raise EditError("The courses cross each other once they are fitted to the existing lines. "
                            "Use a smaller fit distance.")
    return out
