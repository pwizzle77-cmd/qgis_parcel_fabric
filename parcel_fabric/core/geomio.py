# -*- coding: utf-8 -*-
"""QGIS geometry <-> plain segments used by the legal writer and reports."""

from qgis.core import (
    Qgis,
    QgsCircularString,
    QgsCompoundCurve,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLineString,
    QgsPointXY,
    QgsProject,
)


def _xy(p):
    return (p.x(), p.y())


def curve_segments(curve):
    """('line', a, b) / ('arc', s, m, e) segments of any QGIS curve."""
    out = []
    if isinstance(curve, QgsCompoundCurve):
        for i in range(curve.nCurves()):
            out += curve_segments(curve.curveAt(i))
    elif isinstance(curve, QgsCircularString):
        n = curve.numPoints()
        for i in range(0, n - 2, 2):
            out.append(("arc", _xy(curve.pointN(i)), _xy(curve.pointN(i + 1)), _xy(curve.pointN(i + 2))))
    elif isinstance(curve, QgsLineString):
        pts = [_xy(curve.pointN(i)) for i in range(curve.numPoints())]
        for a, b in zip(pts, pts[1:]):
            if abs(a[0] - b[0]) > 1e-9 or abs(a[1] - b[1]) > 1e-9:
                out.append(("line", a, b))
    else:
        seg = curve.segmentize()
        return curve_segments(seg)
    return out


def parts_of(geom):
    """The polygons of a parcel, curves kept."""
    g = geom.constGet()
    if hasattr(g, "numGeometries"):
        return [QgsGeometry(g.geometryN(i).clone()) for i in range(g.numGeometries())]
    return [QgsGeometry(geom)]


def polygon_rings(geom):
    """[(exterior segments, [hole segments...]), ...] for every part."""
    g = geom.constGet()
    parts = []
    if hasattr(g, "numGeometries"):
        polys = [g.geometryN(i) for i in range(g.numGeometries())]
    else:
        polys = [g]
    for p in polys:
        ext = curve_segments(p.exteriorRing())
        holes = [curve_segments(p.interiorRing(i)) for i in range(p.numInteriorRings())]
        parts.append((ext, holes))
    return parts


def to_crs(geom, src_crs, dst_crs):
    g = QgsGeometry(geom)
    if src_crs != dst_crs:
        g.transform(QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance()))
    return g


def is_mercator(crs):
    """Mercator / Web Mercator stretches distances (about 15% in north Florida)."""
    return crs.projectionAcronym() in ("merc", "webmerc")


def unit_word(crs):
    """'feet' or 'meters' for a projected CRS, None for ones that can't measure land."""
    if crs.isGeographic() or is_mercator(crs):
        return None
    u = crs.mapUnits()
    feet = {Qgis.DistanceUnit.Feet, Qgis.DistanceUnit.FeetUSSurvey}
    for name in ("FeetBritish1865", "FeetBritish1936", "FeetBritishBenoit1895A", "FeetBritishBenoit1895B",
                 "FeetBritishSears1922Truncated", "FeetBritishSears1922", "FeetClarkes", "FeetGoldCoast",
                 "FeetIndian", "FeetIndian1937", "FeetIndian1962", "FeetIndian1975"):
        v = getattr(Qgis.DistanceUnit, name, None)
        if v is not None:
            feet.add(v)
    if u in feet:
        return "feet"
    if u == Qgis.DistanceUnit.Meters:
        return "meters"
    return None


def _segmentized_polygons(geom):
    seg = QgsGeometry(geom.constGet().segmentize())
    # asMultiPolygon throws on a single polygon in QGIS 4
    multi = []
    one = []
    try:
        multi = seg.asMultiPolygon()
    except TypeError:
        multi = []
    if multi:
        return multi
    try:
        one = seg.asPolygon()
    except TypeError:
        one = []
    return [one] if one else []


def _exterior_line(geom):
    """The outer rings as a line. Hole edges are not part of this line."""
    lines = []
    for poly in _segmentized_polygons(geom):
        if not poly or len(poly[0]) < 2:
            continue
        lines.append(QgsGeometry.fromPolylineXY(poly[0]))
    if not lines:
        return None
    return lines[0] if len(lines) == 1 else QgsGeometry.collectGeometry(lines)


def exterior_vertices(geom):
    pts = []
    for poly in _segmentized_polygons(geom):
        if not poly:
            continue
        pts.extend((p.x(), p.y()) for p in poly[0])
    return pts


def boundary_distance(geom, points):
    """Largest distance from points to the outer boundary, not the polygon fill."""
    line = _exterior_line(geom)
    if line is None or line.isEmpty():
        return float("inf")
    worst = 0.0
    for xy in points:
        d = line.distance(QgsGeometry.fromPointXY(QgsPointXY(xy[0], xy[1])))
        if d > worst:
            worst = d
    return worst


def fit_gap(geom, legs):
    """How far written courses sit from the outer boundary, in map units.

    Samples each course's ends, its midpoint, and a curve's mid-arc point.
    Also measures outer-ring corners against the written line, so a bend the
    writer joined into one course still shows up.
    """
    if not legs:
        return 0.0
    samples = []
    path = [legs[0].start]
    for leg in legs:
        samples.append(leg.start)
        samples.append(leg.end)
        if leg.mid:
            samples.append(leg.mid)
            path.append(leg.mid)
        else:
            samples.append(((leg.start[0] + leg.end[0]) / 2.0, (leg.start[1] + leg.end[1]) / 2.0))
        path.append(leg.end)
    worst = boundary_distance(geom, samples)
    if len(path) >= 2:
        line = QgsGeometry.fromPolylineXY([QgsPointXY(p[0], p[1]) for p in path])
        for xy in exterior_vertices(geom):
            d = line.distance(QgsGeometry.fromPointXY(QgsPointXY(xy[0], xy[1])))
            if d > worst:
                worst = d
    return worst
