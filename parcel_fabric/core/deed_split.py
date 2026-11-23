# -*- coding: utf-8 -*-
"""Split a parcel by the courses of a deed that conveys part of it.

The parent's boundary is senior: deed corners within the fit distance of it are
held to its corners or lines, and deed courses that run along it reuse its lines.
Only the courses inside the parent become new lines, with the deed's dimensions.
"""

import math
from copy import copy
from dataclasses import dataclass, field

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsLineString, QgsPoint, QgsPointXY, QgsPolygon, QgsRectangle

from .build import _single_curve
from .edits import EditError, _require_current, _seg, _xy, replace_parcel
from .fabric import CURRENT, type_expr

DEED_FIELDS = ("Distance", "COGOType", "IsCOGOGround", "Rotation", "Scale", "QPF_DimSource")


@dataclass
class DeedPlan:
    parent: object
    piece: QgsGeometry = None           # the land conveyed, fitted to the parent
    remainders: list = field(default_factory=list)  # cores of the pieces left over, largest first
    left: float = 0.0                   # area left over (map units)
    inside: list = field(default_factory=list)      # legs that become new lines
    along: list = field(default_factory=list)       # legs on the parent's boundary
    nodes: list = field(default_factory=list)       # corners on the parent's boundary
    moved: list = field(default_factory=list)       # (corner number, distance in map units)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _geom(xy):
    return QgsGeometry.fromPointXY(QgsPointXY(*xy))


def _boundary_curves(fabric, parent, seg):
    """Current lines on the parent's boundary, as curves."""
    ring = QgsGeometry(seg.constGet().boundary())
    tol = fabric.tolerance
    req = QgsFeatureRequest().setFilterRect(seg.boundingBox().buffered(tol)).setFilterExpression(
        "%s AND %s" % (CURRENT, type_expr(parent["ParcelType"])))
    out = []
    for f in fabric.lines.getFeatures(req):
        curve = _single_curve(f.geometry())
        if curve is None or curve.isEmpty():
            continue
        g = QgsGeometry(curve.segmentize())
        pts = (_xy(curve.startPoint()), _xy(curve.endPoint()), _xy(g.interpolate(g.length() / 2).asPoint()))
        if all(ring.distance(_geom(q)) <= tol for q in pts):
            out.append(curve.clone())
    return out


def plan_deed_split(fabric, parent, legs, fit):
    """Fit a closed deed traverse (map-unit ``legs``) to ``parent``. ``fit`` is in map units."""
    _require_current(fabric, parent)
    seg = _seg(parent.geometry())
    curves = _boundary_curves(fabric, parent, seg)
    if not curves:
        raise EditError("The parcel's boundary lines aren't in the fabric, so a deed can't be tied to it.")
    plan = DeedPlan(parent)
    factor = fabric.map_units_per_foot
    ends = [_xy(p) for c in curves for p in (c.startPoint(), c.endPoint())]

    def held(xy):
        """The corner, or point on the boundary, that this deed corner is held to."""
        corner = min(ends, key=lambda e: _dist(e, xy))
        if _dist(corner, xy) <= fit:
            return corner
        near = min((c.closestSegment(QgsPoint(*xy))[:2] for c in curves), key=lambda r: r[0])
        return _xy(near[1]) if math.sqrt(near[0]) <= fit else None

    corners, on = [], []
    for k, leg in enumerate(legs, 1):
        xy = held(leg.start)
        on.append(xy is not None)
        if xy is None:
            xy = leg.start
            if not seg.contains(_geom(xy)):
                raise EditError(
                    "Deed corner %d is %.2f ft outside the parcel. Check the start point and the calls; the deed "
                    "may describe land the parent doesn't own." % (k, seg.distance(_geom(xy)) / factor))
        corners.append(xy)
        if _dist(xy, leg.start) > 1e-9:
            plan.moved.append((k, _dist(xy, leg.start)))

    ring = QgsGeometry(seg.constGet().boundary())
    reach = seg.buffer(max(fabric.tolerance, seg.length() * 1e-9), 8)
    outline = []
    n = len(legs)
    for k, leg in enumerate(legs):
        j = (k + 1) % n
        new = copy(leg)
        new.record_az = getattr(leg, "record_az", leg.chord_az)
        new.start, new.end = corners[k], corners[j]
        if _dist(new.start, new.end) <= fabric.tolerance:
            raise EditError("Deed course %d is shorter than the fit distance. Use a smaller fit distance." % (k + 1))
        if leg.course.is_curve:      # carry the middle of the arc along with its ends
            new.mid = tuple(leg.mid[i] + (new.start[i] - leg.start[i] + new.end[i] - leg.end[i]) / 2 for i in (0, 1))
        g = _seg(fabric.leg_geometry(new))
        outline += [QgsPoint(v.x(), v.y()) for v in g.vertices()][:-1]
        if on[k] and on[j] and all(ring.distance(g.interpolate(g.length() * t)) <= fit for t in (.25, .5, .75)):
            plan.along.append(new)
            continue
        if not reach.contains(g):
            raise EditError("Deed course %d leaves the parcel." % (k + 1))
        plan.inside.append(new)
    plan.nodes = [c for c, held_on in zip(corners, on) if held_on]
    if not plan.inside:
        raise EditError("The deed describes the whole parcel, so there's nothing to split.")

    outline.append(outline[0].clone())
    poly = QgsGeometry(QgsPolygon(QgsLineString(outline)))
    if not poly.isGeosValid():
        raise EditError("The deed's courses cross each other once they are fitted to the parcel.")
    plan.piece = poly.intersection(seg)

    plan.left = seg.area() - plan.piece.area()
    # shrunk, so hairlines between the deed's courses and the parent's lines neither count nor join pieces
    core = seg.difference(poly).buffer(-max(fit, fabric.tolerance) / 2, 4)
    plan.remainders = sorted((p for p in core.asGeometryCollection() if p.area() > 0), key=lambda p: -p.area())
    if not plan.remainders:
        raise EditError("The deed takes the whole parcel, so there's nothing to split.")
    return plan


def apply_deed_split(fabric, plan, record, remainder_keeps=True, dim_source="Record", rotation=0.0, scale=1.0):
    """Split the parent: all saved, or nothing."""
    ptype = plan.parent["ParcelType"]
    lyr = fabric.lines

    def feature(leg):
        return fabric.line_feature(lyr, leg, record, dim_source, "Chord", rotation, scale, ptype)

    def deed_dimensions(ed):
        """Boundary halves that match a deed course take the deed's dimensions."""
        tol = fabric.tolerance
        for leg in plan.along:
            if leg.course.is_curve:
                continue
            f = feature(leg)
            box = QgsRectangle(QgsPointXY(*leg.start), QgsPointXY(*leg.end))
            box.normalize()
            box.grow(tol)
            for old in lyr.getFeatures(QgsFeatureRequest().setFilterRect(box)):
                if old["CreatedByRecord"] != record or old["RetiredByRecord"] or not old["ParentLineID"] \
                        or old["ParcelType"] != ptype:
                    continue
                curve = _single_curve(old.geometry())
                a, b = _xy(curve.startPoint()), _xy(curve.endPoint())
                same = _dist(a, leg.start) <= tol and _dist(b, leg.end) <= tol
                if not same and not (_dist(b, leg.start) <= tol and _dist(a, leg.end) <= tol):
                    continue
                for name in DEED_FIELDS:
                    ed.change(lyr, old.id(), name, f[name])
                ed.change(lyr, old.id(), "Direction", (f["Direction"] + (0 if same else 180)) % 360)

    # what's left is one parcel, however many polygons it has (a single-polygon fabric: the largest)
    left = [p.pointOnSurface() for p in (plan.remainders if fabric.multipart else plan.remainders[:1])]
    return replace_parcel(fabric, plan.parent, plan.nodes, [feature(leg) for leg in plan.inside], record, "Split",
                          left if remainder_keeps else None, deed_dimensions, None if remainder_keeps else [left])
