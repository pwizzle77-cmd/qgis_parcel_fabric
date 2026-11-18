# -*- coding: utf-8 -*-
"""Run a least-squares adjustment on fabric lines and points, show the
results, and (when asked) apply them."""

import math
from dataclasses import dataclass, field

from qgis.core import (
    QgsCompoundCurve,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsRectangle,
    QgsVectorLayer,
)

from . import lsa
from .build import _ends, _single_curve, build_parcels
from .fabric import CURRENT, EditSession, Fabric, quote
from .schema import _mt

SOURCE_FACTOR = {"Measured": 0.5, "Record": 1.0, "Computed": 3.0, "Estimated": 10.0}


@dataclass
class Network:
    points: dict = field(default_factory=dict)      # point fid -> lsa.Point
    obs: list = field(default_factory=list)
    lines: dict = field(default_factory=dict)       # (layer id, fid) -> (layer, feature, a fid, b fid)
    skipped: list = field(default_factory=list)     # messages
    extent: QgsRectangle = None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect(fabric, scope, record_gid=None, extent=None, parcel_feats=None, use_control=True,
            sd_dist=0.05, sd_ppm=100.0, sd_dir_sec=20.0):
    """Points and observations for the lines in ``scope``:
    "view" (``extent``), "record" (lines created by ``record_gid``) or
    "parcels" (lines on the boundaries of ``parcel_feats``)."""
    net = Network()
    tol = fabric.tolerance
    reqs = []
    if scope == "record":
        reqs = [QgsFeatureRequest().setFilterExpression('"CreatedByRecord" = %s AND %s' % (quote(record_gid), CURRENT))]
    elif scope == "parcels":
        box = None
        for p in parcel_feats:
            b = p.geometry().boundingBox()
            box = b if box is None else (box.combineExtentWith(b) or box)
        if box is None:
            return net
        box.grow(tol * 4)
        reqs = [QgsFeatureRequest().setFilterRect(box).setFilterExpression(CURRENT)]
        rings = [QgsGeometry(QgsGeometry(p.geometry().constGet().segmentize()).constGet().boundary())
                 for p in parcel_feats]
    else:
        reqs = [QgsFeatureRequest().setFilterRect(extent).setFilterExpression(CURRENT)]
    feats = []
    for lyr in (fabric.lines, fabric.connections):
        for req in reqs:
            for f in lyr.getFeatures(req):
                if scope == "parcels":
                    seg = QgsGeometry(f.geometry().constGet().segmentize())
                    mid = seg.interpolate(seg.length() / 2)
                    if not any(r.distance(mid) <= tol for r in rings):
                        continue
                feats.append((lyr, f))
    if not feats:
        return net
    box = None
    for _l, f in feats:
        b = f.geometry().boundingBox()
        if box is None:
            box = QgsRectangle(b)
        else:
            box.combineExtentWith(b)
    box.grow(tol * 4)
    net.extent = box
    # fabric points around the lines, on a grid for quick lookup
    cell = max(tol, 1e-6)
    grid = {}
    pmap = {}
    for p in fabric.points.getFeatures(QgsFeatureRequest().setFilterRect(box).setFilterExpression(CURRENT)):
        q = p.geometry().asPoint()
        grid.setdefault((round(q.x() / cell), round(q.y() / cell)), []).append(p)
        pmap[p.id()] = p

    def point_at(xy):
        kx, ky = round(xy[0] / cell), round(xy[1] / cell)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for p in grid.get((kx + dx, ky + dy), ()):
                    q = p.geometry().asPoint()
                    d = math.hypot(q.x() - xy[0], q.y() - xy[1])
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, p)
        return best[1] if best else None

    for lyr, f in feats:
        curve = _single_curve(f.geometry())
        if curve is None:
            continue
        s, e = _ends(curve)
        pa, pb = point_at(s), point_at(e)
        if pa is None or pb is None:
            net.skipped.append("Line #%d in %s has no fabric point at an end" % (f.id(), lyr.name()))
            continue
        c = Fabric.course_from_feature(f)
        if c is None:
            net.skipped.append("Line #%d in %s has no COGO dimensions" % (f.id(), lyr.name()))
            continue
        az = c.azimuth + math.radians(_num(f["Rotation"]) or 0.0)
        dist = c.chord_length if c.is_curve else c.distance
        dist *= _num(f["Scale"]) or 1.0
        if dist <= 0:
            continue
        factor = SOURCE_FACTOR.get(str(f["QPF_DimSource"] or "Record"), 1.0)
        key = (lyr.id(), f.id())
        net.obs += lsa.observations_for_line(key, pa.id(), pb.id(), az, dist, sd_dist, sd_ppm, sd_dir_sec, factor)
        net.lines[key] = (lyr, f, pa.id(), pb.id())
        for p in (pa, pb):
            if p.id() in net.points:
                continue
            q = p.geometry().asPoint()
            mode, sd = lsa.FREE, 0.05
            if use_control:
                cons = int(_num(p["AdjustmentConstraint"]) or 0)
                if _num(p["IsFixed"]) == 1 or cons == 1:
                    mode = lsa.FIXED
                elif cons == 2:
                    mode, sd = lsa.WEIGHTED, _num(p["QPF_StdDevXY"]) or 0.05
            net.points[p.id()] = lsa.Point(p.id(), q.x(), q.y(), mode, sd)
    return net


# --------------------------------------------------------------------------
# Result layers
# --------------------------------------------------------------------------
def result_layers(fabric, net, res, ellipse_scale=100.0):
    crs = fabric.points.crs().authid()

    def mem(geom, name, fields):
        lyr = QgsVectorLayer("%s?crs=%s" % (geom, crs), name, "memory")
        lyr.dataProvider().addAttributes([QgsField(n, _mt(t)) for n, t in fields])
        lyr.updateFields()
        return lyr

    pts = mem("Point", "Adjusted points", [("point_fid", "Int"), ("shift_ft", "Double"), ("dx", "Double"),
                                            ("dy", "Double"), ("sd_major", "Double"), ("sd_minor", "Double"),
                                            ("mode", "QString")])
    ell = mem("Polygon", "Error ellipses (x%g)" % ellipse_scale, [("point_fid", "Int"), ("sd_major", "Double"),
                                                                  ("sd_minor", "Double")])
    vec = mem("LineString", "Point movements", [("point_fid", "Int"), ("shift_ft", "Double")])
    lns = mem("LineString", "Line residuals", [("layer", "QString"), ("line_fid", "Int"), ("dist_res_ft", "Double"),
                                               ("dir_res_sec", "Double"), ("worst_norm", "Double"), ("flag", "Int")])
    mode_name = {lsa.FREE: "free", lsa.FIXED: "fixed", lsa.WEIGHTED: "weighted"}
    pf, ef, vf = [], [], []
    for k, pr in res.points.items():
        f = QgsFeature(pts.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pr.x, pr.y)))
        f.setAttributes([k, math.hypot(pr.dx, pr.dy), pr.dx, pr.dy, pr.a, pr.b,
                         "held" if k == res.held else mode_name[net.points[k].mode]])
        pf.append(f)
        if pr.a > 0:
            g = QgsFeature(ell.fields())
            g.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(*xy) for xy in lsa.ellipse_points(
                pr.x, pr.y, pr.a, pr.b, pr.theta, ellipse_scale)]]))
            g.setAttributes([k, pr.a, pr.b])
            ef.append(g)
        if abs(pr.dx) + abs(pr.dy) > 1e-6:
            v = QgsFeature(vec.fields())
            v.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(pr.x - pr.dx, pr.y - pr.dy), QgsPointXY(pr.x, pr.y)]))
            v.setAttributes([k, math.hypot(pr.dx, pr.dy)])
            vf.append(v)
    by_line = {}
    for r in res.obs:
        by_line.setdefault(r.obs.key, []).append(r)
    lf = []
    for key, rs in by_line.items():
        lyr, feat, _a, _b = net.lines[key]
        d = next((r.residual for r in rs if r.obs.kind == "dist"), 0.0)
        a = next((r.residual for r in rs if r.obs.kind == "dir"), 0.0)
        worst = max((abs(r.normalized) for r in rs), default=0.0)
        f = QgsFeature(lns.fields())
        f.setGeometry(QgsGeometry(feat.geometry().constGet().segmentize()))
        f.setAttributes([lyr.name(), feat.id(), d, math.degrees(a) * 3600, worst, 1 if worst > 3 else 0])
        lf.append(f)
    for lyr, feats in ((pts, pf), (ell, ef), (vec, vf), (lns, lf)):
        lyr.dataProvider().addFeatures(feats)
        lyr.updateExtents()
    _style(pts, ell, vec, lns)
    return [lns, vec, ell, pts]


def _style(pts, ell, vec, lns):
    from qgis.core import (QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsRuleBasedRenderer,
                           QgsSingleSymbolRenderer)
    pts.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
        {"name": "cross2", "size": "2.5", "color": "40,40,40"})))
    ell.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
        {"color": "31,95,168,40", "outline_color": "#1f5fa8", "outline_width": "0.3"})))
    vec.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple(
        {"color": "#b5541b", "width": "0.6"})))
    root = QgsRuleBasedRenderer.Rule(None)
    root.appendChild(QgsRuleBasedRenderer.Rule(QgsLineSymbol.createSimple({"color": "#c82828", "width": "1.2"}),
                                               0, 0, '"flag" = 1', "Disagrees (normalized residual > 3)"))
    root.appendChild(QgsRuleBasedRenderer.Rule(QgsLineSymbol.createSimple({"color": "#2f7d32", "width": "0.6"}),
                                               0, 0, '"flag" = 0', "Fits"))
    lns.setRenderer(QgsRuleBasedRenderer(root))


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------
def _similarity(a0, b0, a1, b1):
    """Map points so that a0 -> a1 and b0 -> b1 (rotate, scale, move)."""
    zx0, zy0 = b0[0] - a0[0], b0[1] - a0[1]
    zx1, zy1 = b1[0] - a1[0], b1[1] - a1[1]
    den = zx0 * zx0 + zy0 * zy0
    if den == 0:
        return lambda p: (p[0] + a1[0] - a0[0], p[1] + a1[1] - a0[1])
    # complex division (z1 / z0)
    cr = (zx1 * zx0 + zy1 * zy0) / den
    ci = (zy1 * zx0 - zx1 * zy0) / den

    def f(p):
        dx, dy = p[0] - a0[0], p[1] - a0[1]
        return (a1[0] + cr * dx - ci * dy, a1[1] + ci * dx + cr * dy)
    return f


def _moved_geometry(geom, new_start, new_end):
    curve = _single_curve(geom)
    s, e = _ends(curve)
    tf = _similarity(s, e, new_start, new_end)
    cc = QgsCompoundCurve()
    parts = [curve.curveAt(i) for i in range(curve.nCurves())] if isinstance(curve, QgsCompoundCurve) else [curve]
    for part in parts:
        c = part.clone()
        for i in range(c.numPoints()):
            p = c.pointN(i)
            x, y = tf((p.x(), p.y()))
            c.moveVertex(_vid(c, i), QgsPoint(x, y))
        cc.addCurve(c)
    return QgsGeometry(cc)


def _vid(curve, i):
    from qgis.core import QgsVertexId
    return QgsVertexId(0, 0, i)


def apply(fabric, net, res):
    """Move points and lines to the adjusted coordinates and rebuild parcels.
    Returns (points moved, lines changed, parcels rebuilt)."""
    moved = {k: pr for k, pr in res.points.items() if math.hypot(pr.dx, pr.dy) > 1e-6}
    if not moved:
        return 0, 0, 0
    lyrs = [fabric.points, fabric.lines, fabric.connections]
    nlines = 0
    with EditSession(lyrs, "Apply adjustment") as ed:
        for k, pr in moved.items():
            ed.set_geometry(fabric.points, k, QgsGeometry(QgsPoint(pr.x, pr.y)))
            ed.change(fabric.points, k, "X", pr.x)
            ed.change(fabric.points, k, "Y", pr.y)
        for (lid, fid), (lyr, feat, a, b) in net.lines.items():
            if a not in moved and b not in moved:
                continue
            pa, pb = res.points.get(a), res.points.get(b)
            ed.set_geometry(lyr, fid, _moved_geometry(feat.geometry(), (pa.x, pa.y), (pb.x, pb.y)))
            nlines += 1
    rebuilt = 0
    for t in fabric.parcel_types:
        rep = build_parcels(fabric, t, None, net.extent, number=False)
        rebuilt += rep.rebuilt
    return len(moved), nlines, rebuilt


def mark_control(fabric, point_fids, constraint=1, sd=None):
    with EditSession([fabric.points], "Mark control") as ed:
        for fid in point_fids:
            ed.change(fabric.points, fid, "AdjustmentConstraint", constraint)
            ed.change(fabric.points, fid, "QPF_IsControl", 1 if constraint else 0)
            if sd is not None:
                ed.change(fabric.points, fid, "QPF_StdDevXY", sd)
